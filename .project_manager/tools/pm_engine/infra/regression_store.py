"""회귀·라이브 게이트 기록 파일 (층: infra).

회귀 기록 쓰기·직전 수집수 승계·차단 기록 앵커 승계와 기록 경로 해소(보호 훅 engine-root 정렬)를 소유한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from pm_engine.domain import identity_args
from pm_engine.infra import fs, git, pm_paths


def _write_regression_flag(flag: Path, record: dict) -> None:
    """회귀 기록의 **단일 쓰기 지점** — 직전 수집수를 이어 붙이고 원자로 교체한다.

    run 경로(단일-슬롯·M>1 슬롯·platform 유무·실패 조기반환)마다 dict 리터럴을 따로 쓰던 아홉
    자리를 여기로 모았다. 승계 규칙이 한 자리에만 있어야 경로마다 기준점이 갈리지 않는다.

    승계는 직전 기록의 `collected`(정수일 때)이고, 직전이 정수를 못 담았으면(실패 기록·파싱
    실패) 그 기록의 `previous_collected` 를 그대로 잇는다 — 안 그러면 실패 한 번에 기준점이
    영구히 사라진다. 어느 쪽도 없으면 None 이다(0 으로 지어내지 않는다).

    `previous_collected` 는 **항상** 실린다(값이 None 이어도 키는 있다) — platform aggregate 의
    스키마 완전일치 검사가 키 집합을 그대로 대조한다. `record` 는 제자리에서 갱신되므로
    호출부가 승계값을 그대로 읽어 증감 문장에 쓴다.
    """
    record["previous_collected"] = _previous_collected(flag)
    flag.parent.mkdir(parents=True, exist_ok=True)
    fs.write_json_atomic(flag, record)


def _previous_collected(flag: Path) -> int | None:
    """덮어쓰기 전 기록에서 이어받을 수집수 (없거나 손상이면 None)."""
    try:
        data = json.loads(fs.read_text_shared(flag, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    carried = _flag_collected(data)
    if carried is not None:
        return carried
    inherited = data.get("previous_collected")
    return inherited if isinstance(inherited, int) and not isinstance(inherited, bool) else None


def _flag_conf_anchor(data: dict, fallback: str | None) -> str | None:
    """플래그에 기록된 **conf 앵커**(실행 당시 cwd) — 미기록/손상이면 `fallback`.

    선언(test_cmd·platform 목록)은 *실행 트리* 의 것이다. `run --cwd <tree>` 로 핀해 돈 결과를
    pre-push 훅이 `check`(훅은 `--cwd` 를 못 넘긴다)로 검증할 때 check 가 **자기** cwd 로
    선언을 재해소하면 실행 때와 다른 local.conf 를 읽는다(false-green/false-RED). 그래서 run 이
    앵커를 기록하고 check 는 그 앵커로 해소한다. 미기록(옛 플래그)은 호출부 해소값으로 폴백.
    """
    anchor = data.get("conf_anchor")
    return anchor if isinstance(anchor, str) and anchor else fallback


def _flag_collected(data: dict) -> int | None:
    """플래그의 `collected` 를 정수로 정규화 — 손상값(문자열·리스트·bool)은 None.

    None 은 "수집수 모름" 이다 — 증감 문장이 비고(지어내지 않는다) 선언 platform aggregate
    에서는 검증 불가로 fail 이 된다. 정규화 없이 손상값이 산술에 들어가면 TypeError 로
    **게이트 자체가 죽는다** — 장부 손상이 회귀 해소를 깨면 안 된다는 fail-soft 규율과 같은 축이다.
    """
    value = data.get("collected")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag_blocks_push(data: dict) -> bool:
    """이 기록이 push 를 막는 상태인가 — fail 기록(RED).

    `check` 의 차단 갈래와 **같은 판정 프리미티브**를 쓴다(판정 두 벌 금지). HEAD 불일치는
    여기 없다 — 그건 커밋마다 생기는 정상 재실행 사유다.
    """
    return data.get("status") != "pass"


def _inherit_flag_anchor(home, default_cwd: str, flag: Path | None = None) -> str:
    """차단 기록이 있으면 그 기록의 conf 앵커를 이어받는다 — 없으면 `default_cwd`.

    pre-push 훅은 `check || run` 이고 `--cwd` 를 못 넘긴다. 앵커 A(핀된 트리)의 기록으로
    check 가 RED/stale 을 냈는데 이어지는 재실행이 기본 트리 B 에서 돌면, **B 의 green 이 A 의
    차단 기록을 덮어** 그대로 push 된다(게이트 우회). 그래서 차단 기록의 재실행은 그 기록이
    가리키는 트리에서 돈다.

    **HEAD-stale 은 이어받지 않는다** — 커밋마다 생기는 정상 재실행이라 여기서 이어받으면
    릴리즈 때 한 번 쓴 `--cwd <readonly>` 핀이 이후 모든 훅 재실행에 눌러붙는다(그 트리는
    현 작업 커밋이 아니다). 앵커 디렉토리가 사라졌으면(worktree 정리) 기본 해소로 돌아간다.
    """
    flag = flag or pm_paths.regression_flag(home)
    if not flag.exists():
        return default_cwd
    try:
        data = json.loads(fs.read_text_shared(flag, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default_cwd
    if not isinstance(data, dict):
        return default_cwd
    anchor = _flag_conf_anchor(data, None)
    if not anchor or os.path.abspath(anchor) == default_cwd:
        return default_cwd
    if not _flag_blocks_push(data):
        return default_cwd
    if not Path(anchor).is_dir():
        print(f"regression: 직전 차단 기록의 앵커({anchor})가 없어 기본 트리에서 재실행 — "
              "그 기록은 이 실행 결과로 덮인다.", file=sys.stderr)
        return default_cwd
    print(f"regression: 직전 차단 기록의 앵커를 이어받아 실행 → {anchor} "
          "(다른 트리 green 으로 덮이는 게이트 우회 차단)")
    return os.path.abspath(anchor)


def _regression_flag_for(home, session: str | None, *, local_dir: Path | None = None, workspace: str | None = None) -> Path:
    """작업공간은 정규 slot의 SHA-256, 단일 실행은 기존 기록 좌표를 사용한다."""
    if workspace is not None:
        slot = identity_args.normalize_slot_value(workspace)
        key = hashlib.sha256(slot.encode("utf-8")).hexdigest()
        return (local_dir or home.local_dir) / f"regression-workspace-{key}.json"
    if local_dir is None and not session:
        return pm_paths.regression_flag(home)
    base = local_dir or home.local_dir
    if not session:
        return base / "regression.json"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", session)
    return base / f"regression-{slug}.json"


# ── livegate 기록 위치 = 훅 read 위치 정렬 (two-git 단일 소스) ─────────────
# `livegate record` 는 push 보호훅이 읽는 **바로 그** livegate.json 에 기록해야 한다. 훅은 repo 의
# git `core.hooksPath` 에 설치돼(worktree_pool.install_protected_hook), 같은 디렉토리의 sidecar
# `engine-root`(PM 홈 REPO 절대경로 1줄)로 `<engine-root>/.project_manager/tools/board.py` 를 해소하고
# 그 사본의 `.local/livegate.json` 을 `livegate check` 로 읽는다(worktree_pool `_PROTECTED_PRE_PUSH_HOOK`).
# two-git 토폴로지(PM 홈+worktree)에서 record 를 호출된 사본의 `REPO/.local` 에 그냥 쓰면,
# worktree board.py 로 record 할 때 훅이 안 읽는 worktree `.local` 에 조용히 기록→pass 위장→push
# 순간에야 불일치로 드러난다. 그래서 record 도 훅과 **동일한 engine-root
# sidecar 해소**를 공유해 같은 파일에 기록한다(단일 소스). livegate 훅이 없는 홈이면 현행
# `REPO/.local` 폴백이라 채택자 무변경.
_LG_ENGINE_ROOT = "engine-root"  # 프레임워크 push 보호훅 활성 → PM 홈 .local (단일 소스).
_LG_NO_HOOK = "no-hook"          # livegate 훅 없음 → 호출된 사본 REPO/.local (현행 폴백 무변경).
_LG_BROKEN = "broken"            # 훅 sidecar 존재하나 engine-root 무효 → fail-loud(false-green 차단).


def _resolve_livegate_flag(home, cwd: str) -> tuple[Path, str]:
    """record 가 기록할 livegate.json 경로 = **push 보호훅이 읽는 파일**로 정렬한다.

    보호훅(`_PROTECTED_PRE_PUSH_HOOK`)과 동일 해소를 공유한다 — repo(`cwd`)의 git
    `core.hooksPath` 옆 sidecar `engine-root`(PM 홈 REPO 절대경로 1줄)를 읽어, 그 root 에
    board.py 가 실재하면(훅의 `[ -f "$engine_root/.../board.py" ]` fail-closed 게이트와 동일
    검증) `<engine-root>/.project_manager/.local/livegate.json` 을 돌려준다. 반환:
      - (PM 홈 .local livegate.json, `_LG_ENGINE_ROOT`) — 훅 활성·단일 소스.
      - (`pm_paths.livegate_flag`, `_LG_NO_HOOK`) — livegate 훅 없음(현행 폴백 무변경).
      - (`pm_paths.livegate_flag`, `_LG_BROKEN`) — 훅 sidecar 는 있으나 engine-root 무효(빈값/board.py
        부재). 이땐 훅 read 위치와 기록 위치가 갈릴 수 있어 호출부가 fail-loud 로 거부한다.
    """
    hooks_path = git._git_config_get(cwd, "core.hooksPath")
    if not hooks_path:
        return pm_paths.livegate_flag(home), _LG_NO_HOOK
    # 상대 `core.hooksPath` 는 git 이 worktree root(=`cwd`) 기준으로 해소한다(프로세스 cwd 아님).
    # 프레임워크 설치는 절대경로라 평시 무영향이나, 수동/상대 설정 방어로 git 시맨틱을 미러.
    hp = Path(hooks_path)
    if not hp.is_absolute():
        hp = Path(cwd) / hp
    sidecar = hp / "engine-root"
    if not sidecar.is_file():
        # `core.hooksPath` 는 있으나 engine-root sidecar 부재 = livegate 보호훅 아님(예: PM 홈
        # 자신의 회귀 훅·채택자 custom 훅) → 훅 부재 폴백(오탐 fail-loud 방지).
        return pm_paths.livegate_flag(home), _LG_NO_HOOK
    lines = fs.read_text_shared(sidecar, encoding="utf-8").splitlines()
    engine_root = lines[0].strip() if lines else ""
    if not engine_root:
        return pm_paths.livegate_flag(home), _LG_BROKEN
    root = Path(engine_root)
    if not (root / ".project_manager" / "tools" / "board.py").is_file():
        return pm_paths.livegate_flag(home), _LG_BROKEN
    return root / ".project_manager" / ".local" / "livegate.json", _LG_ENGINE_ROOT


def _resolve_regression_flag(home, cwd: str) -> tuple[Path, str]:
    """regression 증거를 livegate와 같은 engine-root `.local` 좌표로 해소한다.

    보호훅 two-git 형상에서는 regression과 livegate가 서로 다른 clone-local 파일을 읽으면
    platform 증거가 있어도 release가 legacy no-platform으로 통과할 수 있다. 훅이 없는 솔로
    형상은 기존 `LOCAL_DIR`을 그대로 쓴다. broken sidecar 판정도 livegate와 공유한다.
    """
    # 단위/채택 트리 중 `.git` 자체가 없는 경로는 hooksPath도 가질 수 없다. git subprocess를
    # 불필요하게 늘리지 않고 기존 clone-local 좌표를 그대로 쓴다.
    if not (Path(cwd) / ".git").exists():
        return _regression_flag_for(home, None), _LG_NO_HOOK
    livegate_flag, mode = _resolve_livegate_flag(home, cwd)
    local_dir = livegate_flag.parent if mode == _LG_ENGINE_ROOT else home.local_dir
    return _regression_flag_for(home, None, local_dir=local_dir), mode
