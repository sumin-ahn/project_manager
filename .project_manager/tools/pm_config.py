#!/usr/bin/env python3
"""single-user multi-repo (N×M) 셋업/관리 파사드 — 가벼운 디스패처 (ADR-0011·ADR-0014·ADR-0016).

multi-PM = N 세션 × M repo 한 개념(ADR-0016) — *수가 1이냐 더냐*. 이 파사드는 single
user 가 여러 repo(multi-PM 셋업)를 도는 토폴로지의 *셋업·조회·진단* 전용이다(다중-사람
협업 아님). 런타임 worktree alloc/release 자동화(bootstrap/handoff)는 여기 비관여 —
이 도구는 사람이 손으로 하는 셋업·관리·진단만 한다(sealed spike §8-5·§3e).

각 서브커맨드는 엔진(board.py / worktree_pool.py / pm_update.py)을 호출하는 *얇은
배선*이다 — 자체 로직 0. 디스패처가 엔진 호출의 단일 지점이다.

사용:
    pm-config init [<board init 인자>]                     # clone 당 1회 셋업 (board.py init 흡수·T-0065)
    pm-config repo add <name> [--git <url>] [--test "<cmd>"] # repo 등록 + .repos clone (신규=--git 필수 / 기등록 hydrate=areas URL·T-0291)
    pm-config worktree add <repo>                          # 새 슬롯 생성 + submodule init
    pm-config status | whoami                              # 풀/리스 + 이 세션 repo/슬롯/branch
    pm-config release <slot> [--force]                     # 작업완료 반납 / 수동 강제(백스톱)
    pm-config update [--from <upstream>]                   # 엔진 갱신 (pm-update 흡수·T-0054)
    pm-config upstream show | set <url|path>               # upstream 조회/전환 (T-0145·검증·fail-closed)
    pm-config add-harness <harness> [--from <src>] [--dry-run]  # 라이브 인스턴스에 두 번째 harness 어댑터 추가 (ADR-0048·T-0270·T-0282)

서브커맨드별 엔진 배선:
  - init      → board.main(["init", ...]) verbatim forward (clone 당 1회 셋업·N=1·M=1[solo] ~ N×M 공용).
  - repo add  → board.areas_append(per-repo 레지스트리 줄·ADR-0014) + `git clone --bare`
                로 `.repos/<name>.git`(worktree 풀 공유 .git 원·ADR-0011).
  - worktree add → worktree_pool.create_slot(새 슬롯 + `git submodule update --init`).
  - status|whoami → worktree_pool.list_leases() + 이 세션 식별(repo/슬롯/branch surface).
  - release → worktree_pool.release(--force 면 force_release) — 수동 반납/강제만.
  - update → pm_update.main(argv) verbatim forward (rename 비용 0·중복 구현 금지).
  - add-harness → pm_import.add_harness_cli(dest, harness, dry_run=, source_root=) verbatim forward
                  (ADR-0048 Decision 3·복사 스코프+인터페이스 예외 번역+소스 해소는 pm_import 단일 진실·
                  중복 0·T-0270·T-0282). `--from` 생략 시 dest local.conf upstream 자동 해소(imported 인스턴스).

결정 (ADR-0011·ADR-0014·spike §8-5):
  - thin forwarder(`pm-config.sh/.cmd`)는 로직 0 — 이 디스패처가 엔진 배선의 단일 지점.
  - 브랜치 할당은 파사드 아님 — `pm-bootstrap <repo> --branch`(T-0060) 소관(명령표 외).
  - update 는 pm_update 로직을 *위임*(import 호출) — pm_update.py 는 그대로 두고 흡수만.
  - 엔진 호출은 DI seam(주입 가능 callable) — 테스트는 mock 주입으로 hermetic(실 clone/
    worktree 부작용 없이 배선만 검증·pm_bootstrap 의 DI 패턴 동류).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py 와 동일 앵커 관례
# (ADR-0011 — 어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).
REPO = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / ".project_manager" / "tools"
REPOS_DIR = REPO / ".repos"   # worktree 풀이 공유하는 bare .git 원 (ADR-0011)

GIT_TIMEOUT_SECONDS = 600   # clone 은 네트워크 — 부트스트랩/worktree git 보다 넉넉히.

# git argv → (returncode, stdout). DI seam 의 타입 (pm_import.GitRunner·worktree_pool 선례).
GitRunner = Callable[[list], "tuple[int, str]"]


# ── 엔진 모듈 동적 로드 (스크립트-위치 앵커·pm_bootstrap 선례) ──────────────────
# board.py·worktree_pool.py 는 같은 tools/ 에 있다. spec_from_file_location 으로
# 로드한다 — 패키지 설치 없이 동작(board.py·pm_*.py 와 같은 로드 관례). 부재/로드
# 실패는 해당 서브커맨드 경로에서만 명시 에러(침묵 무력화 금지·ADR-0013).


def _load_module(name: str, filename: str):
    """tools/<filename> 를 모듈로 로드한다. 부재/실패 → None (호출부가 명시 에러)."""
    path = TOOLS_DIR / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — 로드 실패는 호출부가 명시 에러로 보고.
        return None


def _real_clone_runner() -> GitRunner:
    """`git clone ...` 을 실행하는 GitRunner (fail-soft). argv 는 clone 인자 그대로.

    clone 은 `-C <dir>` 가 아니라 `git clone <url> <dest>` 형태라 별도 runner 로 둔다.
    git 바이너리 부재(shutil.which)·예외는 (1, stderr-or-"") 로 감싼다 — 호출부가 rc 로
    판정. 인코딩은 엔진 관례대로 UTF-8(한글 경로·메시지 안전).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            result = subprocess.run(
                [git_binary, *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001 — fail-soft: rc!=0 로 호출부에 위임.
            return 1, str(exc)

    return runner


def _local_conf_session() -> str | None:
    """`.project_manager/local.conf` 의 `session=` (없거나 OSError → None).

    board.py 를 import 하지 않으므로(ADR-0013 isolation·touches 격리) `board.local_config()
    .get("session")` 와 *동일 의미*를 stdlib 로 자체 구현한다 — plain `KEY=value`·`#`
    주석/빈 줄 무시. 부재/읽기실패는 None(폴백).
    """
    conf_file = REPO / ".project_manager" / "local.conf"
    try:
        text = conf_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "session":
            return val.strip() or None
    return None


def _default_session(*, identity=None) -> str | None:
    """세션 식별자 — board.py `session_name()` 과 *동형* 우선순위 (ADR-0040 D1·T-0073):
    `$PM_SESSION_NAME` > `$CLAUDE_SESSION_NAME`(deprecated alias·silent) > lease 장부
    state=="leased" 행이 정확히 1개면 그 session (단일-lease 유도) > (장부 부재·leased 0 =
    solo) `local.conf session=` > None.

    `PM_SESSION_NAME` 이 정식 엔진 변수(하니스 무관)·`CLAUDE_SESSION_NAME` 은 구 alias(둘 다면
    PM 승·조용히 동작). **leased ≥2 (모호)면 local.conf 층을 건너뛴다**(board 와 동형 — silent
    오귀속 차단). 미해소면 None — `cmd_status`/`whoami` surface(required=False)가 "(비바인딩)"
    으로 표시한다(Windows 4슬롯 홈에서 비바인딩 세션이 남의 리스로 self-identify 하던 직접 증상
    수정·ADR-0040). `<host>-<pid>` 최종 폴백은 세션-귀속 아닌 국소 용처(worktree_pool lease
    취득)에만 잔존 — 여기(surface 해소)선 제거.

    lease 장부(`state=="leased"` 행의 session 목록) 읽기는 공용 `identity_args.leased_sessions`
    로 위임한다(ADR-0057 §Consequences B-1 — pm_config 로컬 `_leased_sessions` 사본을 흡수·
    board.py/worktree_pool 의 동형 사본과 단일 진실로 수렴). board.py 를 직접 import 하지
    않는 관성([[ADR-0013]] isolation·touches 격리)은 여기서도 유지한다 — `_load_module`
    (spec_from_file_location) 로 `identity_args` 를 로드한다(board.py·worktree_pool 로딩과
    동형 패턴·스크립트 직접 실행/테스트 양쪽에서 동일하게 동작). `identity` 주입으로 hermetic
    테스트 가능(미주입 시 실 모듈 로드 — `identity_args` 는 파일 IO 0 순수 모듈이라 실 로드도
    안전·부작용 0). 장부 경로는 호출 시점 `REPO` 에서 구성한다(monkeypatch 존중·
    `_local_conf_session` 과 동형).

    저장측(worktree_pool)과 매칭측(여기)이 어긋나면 "이 세션의 리스" surface 가 board 매칭과
    어긋난다(T-0066 must-fix) — 세 모듈을 같은 우선순위로 통일한다.
    """
    env = os.environ.get("PM_SESSION_NAME") or os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    identity_mod = identity or _load_module("identity_args", "identity_args.py")
    leases_file = REPO / ".project_manager" / ".local" / "worktree-leases.json"
    leased = identity_mod.leased_sessions(leases_file) if identity_mod is not None else []
    if len(leased) == 1:
        return leased[0]
    if not leased:
        # 장부 부재·leased 0 = solo → legacy local.conf 폴백 (후방호환).
        conf_sess = _local_conf_session()
        if conf_sess:
            return conf_sess
    # leased ≥2 (모호) 또는 solo 무바인딩 → 미해소(surface 는 "(비바인딩)").
    return None


def _local_conf_user() -> str | None:
    """`.project_manager/local.conf` 의 `user=` (없거나 OSError → None) (T-0161·ADR-0033 ③).

    `_local_conf_session` 과 동형 — board.py 를 import 하지 않으므로(ADR-0013 isolation·
    touches 격리) `board.local_config().get("user")` 와 *동일 의미*를 stdlib 로 자체 구현한다.
    plain `KEY=value`·`#` 주석/빈 줄 무시. 부재/읽기실패는 None(폴백).
    """
    conf_file = REPO / ".project_manager" / "local.conf"
    try:
        text = conf_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "user":
            return val.strip() or None
    return None


def _git_config_email() -> str | None:
    """`git config user.email` (미설정/git 부재/실패 → None·fail-soft) (T-0161·ADR-0033 ③).

    board.py `_git_config_email` 와 *동형* — board 직접 import 금지(ADR-0013 isolation)라
    `_default_user` 의 폴백 레이어를 stdlib subprocess 로 자체 구현한다. UTF-8 고정(한글
    이름·메시지 안전)·짧은 timeout. git 바이너리 부재·rc≠0·예외는 None 으로 강등(크래시 0).
    """
    git_binary = shutil.which("git")
    if git_binary is None:
        return None
    try:
        r = subprocess.run(
            [git_binary, "-C", str(REPO), "config", "user.email"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
    except Exception:  # noqa: BLE001 — fail-soft: git 호출 실패는 None(미상)으로 강등.
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _default_user() -> str | None:
    """user 식별자 — board.py `user_name()` 과 *동형* 우선순위 (T-0161·ADR-0033 ③):
    `local.conf user=` > `git config user.email` > None (graceful·user 미상 허용).

    `pm`(슬롯·`_default_session`)과 직교하는 **누가**(사람) 차원이다. `repo add` 의 areas.md
    `area_owner` 칼럼(그 area 의 user 소유·`--mine` 풀 입력) 기본값으로 쓴다. solo 는 보통
    `local.conf user=` 미설정 → `git config user.email` 폴백·그마저 없으면 None(빈 area_owner).
    """
    conf_user = _local_conf_user()
    if conf_user:
        return conf_user
    return _git_config_email()


# areas.md canonical 칼럼 순서상 `area_owner` 의 위치(마지막·index 7·board `_AREAS_COLUMNS`).
# 구 헤더(area_owner 이전) + 신 canonical row 가 append 된 업그레이드 행을 이 위치로 폴백 read
# 한다(board `_parse_areas` 의 wider-row 관용과 동형·유실 방지).
_CANONICAL_AREA_OWNER_IDX = 7


def _distinct_area_owners() -> int:
    """areas.md 의 distinct non-empty `area_owner` 수 — pm_config 자체 파싱(다중사용자 최소 신호·ADR-0053).

    `cmd_status` 의 isolation posture(strict/degrade/solo) 판정용 *coarse* 신호다. board.py 를
    import 하지 않으므로([[ADR-0013]] isolation·touches 격리) `_local_conf_session` 동형으로
    areas.md 를 stdlib 로 직접 읽는다 — board 의 격리 *판정*(`_ticket_is_mine`·티켓 스캔
    `_distinct_ticket_users`)은 복제하지 않고, 공유 레지스트리(areas.md)의 `area_owner` 칼럼만
    헤더-인식으로 센다. 실 strict-exclude 여부는 `board list --mine` loud-warn(ADR-0053 #4)이
    authoritative — 여긴 정체성·모드 posture 만.

    헤더에서 `area_owner` 칼럼을 찾는다(ADR-0014·신 스키마). 헤더에 없어도 데이터 행이 canonical
    폭(≥8 셀)이면 마지막(`_CANONICAL_AREA_OWNER_IDX`)에서 읽어 구-헤더+신-row 업그레이드의 유실을
    막는다. 구분선(`|---|`) skip. 파일/칼럼 부재·파싱 실패는 0(fail-soft·solo 취급·크래시 0).
    """
    af = REPO / ".project_manager" / "areas.md"
    try:
        lines = af.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return 0    # 부재/읽기실패/손상 UTF-8 → 0(solo 취급·크래시 0·docstring fail-soft 계약).
    owners: set[str] = set()
    header_idx: int | None = None
    header_seen = False
    for line in lines:
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and all(c and set(c) <= {"-", ":"} for c in cells):
            continue  # 구분선(|---|:--:|) skip.
        if not header_seen:
            header_seen = True
            lowered = [c.lower() for c in cells]
            if "area_owner" in lowered:
                header_idx = lowered.index("area_owner")
            continue
        # 데이터 행: 헤더에 area_owner 가 있으면 그 위치, 없어도 canonical 폭이면 마지막에서
        # 읽는다(구 헤더 + 신 canonical row 업그레이드·board `_parse_areas` 동형).
        if header_idx is not None and header_idx < len(cells):
            val = cells[header_idx]
        elif header_idx is None and len(cells) > _CANONICAL_AREA_OWNER_IDX:
            val = cells[_CANONICAL_AREA_OWNER_IDX]
        else:
            val = ""
        if val:
            owners.add(val)
    return len(owners)


def _local_conf_test_cmd() -> str | None:
    """`.project_manager/local.conf` 의 `test_cmd=` (없거나 OSError → None).

    `_local_conf_session` 과 동형 — board.py 를 import 하지 않으므로(ADR-0013 isolation·
    touches 격리) `board.local_config().get("test_cmd")` 와 *동일 의미*를 stdlib 로 자체
    구현한다. worktree add 빌드명령 프롬프트의 기본값(`board._test_cmd` 솔로 폴백 레이어와
    동형 — 미지정 시 `pytest -q`)을 제시하는 데 쓴다.
    """
    conf_file = REPO / ".project_manager" / "local.conf"
    try:
        text = conf_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "test_cmd":
            return val.strip() or None
    return None


def _default_test_cmd() -> str:
    """worktree add 빌드명령 프롬프트의 솔로 폴백값 — `local.conf test_cmd` 또는 `pytest -q`.

    board._test_cmd 의 솔로 폴백 레이어(`local_config().get("test_cmd") or "pytest -q"`)와
    동형. `_resolve_repo_test_cmd` 의 마지막 레이어(areas 미등록·빈 값일 때)다 —
    프롬프트 표시값 resolve 의 폴백.
    """
    return _local_conf_test_cmd() or "pytest -q"


def _resolve_repo_test_cmd(repo: str, *, board=None) -> str:
    """프롬프트 `[기본: <X>]` 의 X — 그 repo 가 Enter 시 실제로 폴백할 test_cmd.

    board._test_cmd 의 해소 체인을 *그 repo 한정으로* 재현한다 (board 직접 import 금지·
    `_load_module` DI + areas 파서 `_parse_areas`/`_areas_row_for_prefix` 재사용):
      1. **활성 repo 의 areas.md test_cmd** — 그 repo(=prefix)의 레지스트리 행에 비어
         있지 않은 `test_cmd` 가 있으면 그것(per-repo 스택·`go test ./...` 등).
      2. **솔로 폴백** — areas 미등록·빈 값이면 `local.conf test_cmd` 또는 `pytest -q`.
    (활성 슬롯 레이어는 새 슬롯 생성 *전* 시점이라 표시에 무의미 — 생략.) board 부재/파서
    부재면 솔로 폴백만. 빈입력(Enter) 시 슬롯에 안 박고(None) 이 체인으로 폴백함을 투명하게
    보여주는 게 목적이다(must-fix 1 — 슬롯이 areas 보다 우선이라 잘못 덮으면 안 됨).
    """
    board_mod = board or _load_module("board", "board.py")
    row_for_prefix = getattr(board_mod, "_areas_row_for_prefix", None) if board_mod else None
    if row_for_prefix is not None:
        try:
            row = row_for_prefix(repo)
        except Exception:  # noqa: BLE001 — areas 파싱 실패는 솔로 폴백으로 강등(크래시 0).
            row = None
        if row and row.get("test_cmd"):
            return row["test_cmd"]
    return _default_test_cmd()


def _resolve_repo_base(repo: str, *, board=None) -> str | None:
    """그 repo 의 areas.md `base` 브랜치 (T-0075). 미지정/미등록/구 스키마/board 부재 → None.

    `cmd_worktree_add` 가 `create_slot(base=)` 로 전달할 값을 resolve 한다 — areas.md 의 그
    repo base(`pm-config repo add --base`/clone-time bare HEAD 가 기록)를 읽어 슬롯 브랜치
    `<repo>_<N>` 가 그 base 에서 파생되게 한다. None 폴백이면 create_slot 이 현행 bare HEAD
    동작(회귀 0).

    board 직접 import 금지(ADR-0013 isolation·touches 격리) — `_resolve_repo_test_cmd` 와
    동형으로 `_load_module` DI + board 의 `_repo_base` 헬퍼를 getattr 로 쓴다. board/헬퍼
    부재(구 board)·파싱 실패는 None 으로 강등(크래시 0·현행 동작 폴백).
    """
    board_mod = board or _load_module("board", "board.py")
    repo_base = getattr(board_mod, "_repo_base", None) if board_mod else None
    if repo_base is None:
        return None
    try:
        return repo_base(repo)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 None 폴백(현행 bare HEAD 동작).
        return None


# 보호 브랜치 default (T-0076) — board.DEFAULT_PROTECTED 와 *동형* 폴백. board/헬퍼 부재(구
# board)·파싱 실패 시 board 를 못 읽으므로 여기서 같은 안전 기본값을 보장한다(보호는 안전
# 기본값이 있어야 — 미해소여도 main 류를 막는다). board 가 있으면 board._repo_protected 가
# 권위(areas override 반영)이고, 이 상수는 board 부재 폴백 전용이다.
_DEFAULT_PROTECTED = ("main", "master", "develop")


def _resolve_repo_protected(repo: str, *, board=None) -> list[str]:
    """그 repo 의 보호 브랜치 목록 (T-0076). board 부재/파싱 실패 → `_DEFAULT_PROTECTED`.

    `cmd_repo_add`(sidecar 채움·훅 설치)·`cmd_worktree_add`(재설치)가 `install_protected_hook`
    에 전달할 목록을 resolve 한다 — areas.md 의 그 repo `protected` 칼럼(`board._repo_protected`)
    을 읽어 미지정/구 스키마면 default(main/master/develop) 폴백. board 가 권위(areas override
    반영)이고, board/헬퍼 부재·파싱 실패만 여기 default 로 강등한다(크래시 0).

    board 직접 import 금지(ADR-0013 isolation·touches 격리) — `_resolve_repo_base` 와 동형으로
    `_load_module` DI + board 의 `_repo_protected` 헬퍼를 getattr 로 쓴다.
    """
    board_mod = board or _load_module("board", "board.py")
    repo_protected = getattr(board_mod, "_repo_protected", None) if board_mod else None
    if repo_protected is None:
        return list(_DEFAULT_PROTECTED)
    try:
        return repo_protected(repo)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 default 폴백(보호 기본값 보장).
        return list(_DEFAULT_PROTECTED)


def _install_protected_hook(repo: str, *, board=None, worktree_pool=None) -> bool:
    """그 repo 의 보호 브랜치 pre-push 훅을 (재)설치한다 — repo add·worktree add 공용 (T-0076).

    보호목록을 `_resolve_repo_protected`(areas `protected`→default)로 해소해
    `worktree_pool.install_protected_hook(repo, protected)` 에 전달한다 — 훅+sidecar+bare
    `core.hooksPath` wiring(멱등·자가치유). **회사 repo 무영향** — 모든 write 는 `.project_manager
    /.local` + bare config 1줄(client-side).

    **fail-soft·best-effort** — worktree_pool 부재/`install_protected_hook` 미존재(구 엔진)/
    예외는 조용히 False(보호훅은 *추가 가드*이지 repo add/worktree add 의 핵심 부작용이 아니다
    → 훅 설치 실패가 등록/슬롯 생성을 깨면 안 된다). bare 부재 시 install 이 no-op False.
    설치 성공 시 True. board 직접 import 금지(ADR-0013) — `_load_module` DI.
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    install = getattr(wp, "install_protected_hook", None) if wp else None
    if install is None:
        return False  # 구 엔진(헬퍼 부재)/wp 부재 — fail-soft(보호훅은 추가 가드).
    protected = _resolve_repo_protected(repo, board=board)
    try:
        return bool(install(repo, protected))
    except Exception:  # noqa: BLE001 — 훅 설치 실패가 등록/슬롯 생성을 깨면 안 됨(best-effort).
        return False


def _stdin_is_tty() -> bool:
    """무인자 분기·프롬프트 게이트용 tty 판정 — stdin·stdout 둘 다 tty 일 때만 True.

    파이프/CI(둘 중 하나라도 비-tty)면 False → 콘솔/프롬프트로 안 멈춘다(input() 블록
    회피·pm_import 비-tty 폴백 패턴 동류). 테스트는 이 헬퍼를 monkeypatch 해 분기를
    결정적으로 친다(라이브 tty 없이).
    """
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


# ── repo name 입력 검증 (T-0078) ─────────────────────────────────────────────

# 허용 repo name = prefix = ticket ID 네임스페이스(ADR-0011)·areas.md 공백구분 칼럼값.
# 영숫자로 시작(leading `-` = 옵션 오인·빈 문자열 배제), 이후 영숫자/`_`/`-` 만 허용.
# 경로분리자(`/`)·`.`(`..` 폴더탈출)·공백(areas.md 줄 corruption)을 전부 배제한다.
# board._FAMILY_SCOPE_RE(`^[A-Za-z0-9_-]+$`)는 leading `-` 를 막지 않아 재사용 부적합 —
# repo name 은 영숫자 시작을 강제하므로 별도 패턴을 둔다(reuse 확인·중복 정의 회피).
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validate_repo_name(name: str) -> bool:
    """repo name 이 허용 패턴(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)에 맞는가 (T-0078).

    `cmd_repo_add` 가 **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 부른다 —
    `../x` 폴더탈출·슬래시/공백/`.` 의 areas.md 줄 corruption·leading `-` 옵션 오인·빈
    문자열을 입구에서 막는다(fail-closed). 모듈수준 헬퍼라 테스트 가능(주입 불필요).
    """
    # fullmatch — `re.match` 의 `$` 는 trailing 개행 직전에도 매칭해 `"billing\n"` 이
    # 가드를 통과(bare 폴더명 개행·areas.md 줄 corruption — 막으려던 결함 클래스)한다.
    return bool(_REPO_NAME_RE.fullmatch(name))


# ── base 브랜치 해소 (T-0075) ─────────────────────────────────────────────────

# `--base` 검증 실패(없는 브랜치)를 빈문자열("미해소")과 구별하는 sentinel. None 폴백
# (bare HEAD 해소 실패)은 빈 문자열로 surface 하지만, *명시 base 가 검증 실패*하면 등록을
# 막아야 하므로(잘못된 base 기록 방지) 이 sentinel 로 호출부에 신호한다.
_BASE_INVALID = object()


def _resolve_base(base_arg: str | None, bare_path: Path, *, runner: GitRunner):
    """repo add base 브랜치를 해소한다 (T-0075). bare(`.repos/<name>.git`)는 존재 전제.

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·
    `_real_clone_runner` 가 `git <argv>` 형태라 `-C` 를 argv 로 넣으면 그 repo 컨텍스트).

      - `base_arg` 미지정(None) → bare HEAD 해소(`git -C <bare> symbolic-ref --short HEAD`
        = 원격 default 브랜치)를 base 로 명시값화한다. 해소 실패(rc≠0/빈 출력)는 빈 문자열
        ("미해소"·worktree add 가 bare HEAD 폴백·현행 동작) — repo 등록 자체는 막지 않는다.
      - `base_arg` 지정 → **로컬 브랜치** 검증(`git -C <bare> show-ref --verify --quiet
        refs/heads/<b>` rc==0). `show-ref --verify` 는 exact-ref primitive(revision 문법
        미적용)라 태그·SHA·`HEAD`·원격 ref 는 물론 `main~0`·`main^{}` 같은 revision 표현도
        통과하지 못한다(T-0078 — worktree 슬롯 파생[T-0075]은 로컬 브랜치 base 가 전제). 통과면
        반환값은 **기존대로 bare 브랜치명(`base_arg`)**(areas.md base 칼럼 규격 불변), 실패면
        `_BASE_INVALID`(호출부가 명확한 에러 rc 1 로 surface·등록 차단).

    반환: 해소된 base 문자열(빈 문자열 = 미해소·None 동등) 또는 `_BASE_INVALID`(검증 실패).
    """
    if base_arg is None:
        rc, out = runner(["-C", str(bare_path), "symbolic-ref", "--short", "HEAD"])
        if rc != 0:
            return ""  # bare HEAD 해소 실패 → 미해소(worktree add 가 bare HEAD 폴백·현행).
        return out.strip()
    # refs/heads/<b> exact-ref 검증 — 태그·SHA·HEAD·원격 ref·revision 문법(main~0·main^{}) 거부,
    # 로컬 브랜치만 통과(T-0078). show-ref --verify 는 revision 문법을 적용 안 하는 exact-ref primitive.
    rc, _out = runner(
        ["-C", str(bare_path), "show-ref", "--verify", "--quiet", f"refs/heads/{base_arg}"]
    )
    if rc != 0:
        return _BASE_INVALID  # 비-로컬-브랜치(태그/SHA/HEAD/부재) → 등록 차단(잘못된 base 기록 방지).
    return base_arg


# ── bare clone fetch refspec 보정 (T-0152) ───────────────────────────────────

# `git clone --bare` 가 설정하지 않는 fetch refspec — 일반(non-bare) clone 은 이 줄을
# remote.origin.fetch 에 박지만 `--bare` 는 생략한다. 그 결과 bare 에 origin/* remote-tracking
# ref(origin/main 등)가 안 생겨, 그 bare 를 공유하는 worktree 슬롯이 핸드오프 라이브-게이트
# (T-0151)의 baseline(@{upstream}/origin/main)을 해소 못 한다. multi-PM 패밀리 공통 결함(T-0152).
_BARE_FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def _set_bare_fetch_refspec(bare_path: Path, *, runner: GitRunner) -> None:
    """bare repo 에 fetch refspec 을 박고 origin/* remote-tracking ref 를 채운다 (T-0152·fail-soft).

    `git clone --bare` 는 일반 clone 과 달리 `remote.origin.fetch` 를 설정하지 않아 bare 에
    `refs/remotes/origin/*` 가 영영 안 생긴다 → 그 bare 를 공유하는 worktree 슬롯이 핸드오프
    라이브-게이트(T-0151)의 baseline(origin/main)을 해소 못 한다(ambiguous→surface). 이를:
      1. `git -C <bare> config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'`
         (set=덮어쓰기·멱등 — 재실행/refspec-없는 과거 bare 보정 안전).
      2. `git -C <bare> fetch origin` (origin/* remote-tracking ref 채움 → origin/main 생성).
    로 근절한다(`--mirror` 가 아니라 명시 refspec — 국소·안전·push --mirror 부작용 없음·결정).

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·base
    해소[T-0075]가 symbolic-ref/show-ref 를 `-C <bare>` 로 호출하는 패턴과 동일). **fail-soft**:
    refspec set 실패·fetch 실패(네트워크)는 경고를 surface 하되 repo add 자체는 막지 않는다
    (refspec 은 박혔으니 이후 fetch 로 복구 가능). bare 는 존재 전제(clone 성공/재사용 후 호출).
    """
    rc, out = runner(
        ["-C", str(bare_path), "config", "remote.origin.fetch", _BARE_FETCH_REFSPEC]
    )
    if rc != 0:
        print(
            f"[경고] bare fetch refspec 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config remote.origin.fetch '{_BARE_FETCH_REFSPEC}'` "
            "후 `git -C <bare> fetch origin` 하라 (origin/* remote-tracking ref·baseline 해소·T-0152).",
            file=sys.stderr,
        )
        return  # refspec 미설정이면 fetch 해도 origin/* 안 채워짐 — skip.
    rc, out = runner(["-C", str(bare_path), "fetch", "origin"])
    if rc != 0:
        print(
            f"[경고] bare `git -C {bare_path} fetch origin` 실패 (rc={rc}): {out.strip()[:200]}\n"
            "  refspec 은 설정됨 — 네트워크 복구 후 `git -C <bare> fetch origin` 으로 origin/* 채우면 "
            "라이브-게이트 baseline(origin/main)이 해소된다 (T-0152).",
            file=sys.stderr,
        )
        return
    print("✓ bare fetch refspec 설정 + fetch origin — origin/* remote-tracking ref 채움 (T-0152).")


def _ensure_bare_branch_tracking(bare_path: Path, *, runner: GitRunner) -> None:
    """bare repo 의 기본 브랜치에 origin tracking(branch.<d>.remote/merge)을 박는다 (T-0273·fail-soft).

    `git clone --bare` 는 일반(non-bare) clone 과 달리 로컬 브랜치 tracking config
    (`branch.<n>.remote`/`branch.<n>.merge`)를 설정하지 않는다 → 그 bare 를 공유하는 worktree
    슬롯의 **기본 브랜치가 `@{upstream}`(origin/<default>)을 해소 못 한다**(livegate baseline·
    handoff freshness·`git status` ahead/behind·`git pull` 무인자 영향). T-0152
    (`_set_bare_fetch_refspec`)가 tracking 의 한 절반(origin/* remote-tracking ref)을 채웠고,
    이 함수가 나머지 절반(로컬 `branch.<d>.*` tracking config)을 채운다. bare 에 설정하면
    worktree 가 공유 common config(`branch.*`)로 **상속**한다(worktree-local config.worktree 아님).

    동작:
      1. 기본 브랜치 = bare HEAD 해소(`git -C <bare> symbolic-ref --short HEAD`, 실패 시
         `git -C <bare> rev-parse --abbrev-ref HEAD` 폴백).
      2. `git -C <bare> config branch.<d>.remote origin` (set=덮어쓰기·멱등).
      3. `git -C <bare> config branch.<d>.merge refs/heads/<d>`.
    비-bare `git clone` 이 checked-out 기본 브랜치만 tracking 거는 동작을 미러 — **기본 브랜치만**
    설정한다(전 브랜치 일괄 tracking 아님·로컬-전용 의도 브랜치를 잘못 tracking 걸지 않기 위해).

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·
    `_set_bare_fetch_refspec`·base 해소[T-0075] 동형). **fail-soft**: bare HEAD 미해소·config
    실패는 경고를 surface 하되 repo add 자체는 막지 않는다(tracking 은 *추가 가드*이지 repo add
    핵심 부작용 아님). bare 는 존재 전제(clone 성공/재사용 후 호출).
    """
    rc, out = runner(["-C", str(bare_path), "symbolic-ref", "--short", "HEAD"])
    if rc != 0:
        # detached·비-symbolic HEAD 폴백 — 현재 브랜치명을 rev-parse 로 시도.
        rc, out = runner(["-C", str(bare_path), "rev-parse", "--abbrev-ref", "HEAD"])
    default_branch = out.strip()
    if rc != 0 or not default_branch or default_branch == "HEAD":
        print(
            f"[경고] bare 기본 브랜치 해소 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config branch.<d>.remote origin` + "
            f"`git -C {bare_path} config branch.<d>.merge refs/heads/<d>` 하라 "
            "(기본 브랜치 @{upstream} 해소·T-0273).",
            file=sys.stderr,
        )
        return  # 기본 브랜치 미해소면 tracking 을 걸 대상이 없음 — skip(fail-soft).
    rc, out = runner(
        ["-C", str(bare_path), "config", f"branch.{default_branch}.remote", "origin"]
    )
    if rc != 0:
        print(
            f"[경고] bare `branch.{default_branch}.remote` 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config branch.{default_branch}.remote origin` + "
            f"`git -C {bare_path} config branch.{default_branch}.merge refs/heads/{default_branch}` "
            "하라 (기본 브랜치 @{upstream} 해소·T-0273).",
            file=sys.stderr,
        )
        return
    rc, out = runner(
        ["-C", str(bare_path), "config",
         f"branch.{default_branch}.merge", f"refs/heads/{default_branch}"]
    )
    if rc != 0:
        print(
            f"[경고] bare `branch.{default_branch}.merge` 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  remote 는 설정됨 — 수동으로 `git -C {bare_path} config "
            f"branch.{default_branch}.merge refs/heads/{default_branch}` 로 보완하라 (T-0273).",
            file=sys.stderr,
        )
        return
    print(
        f"✓ bare 기본 브랜치 origin tracking 설정: branch.{default_branch}.remote=origin + "
        f"merge=refs/heads/{default_branch} (T-0273·슬롯 @{{upstream}} 해소)."
    )


# ── 위임 forward 시 usage prog 정합 (T-0249·ADR-0043) ────────────────────────

# `pm-config init`/`update` 는 board.py / pm_update.py 의 main() 으로 verbatim forward
# 한다(cmd_init·cmd_update). 그 두 main 은 argparse prog 를 파일명으로 하드코딩하고
# (prog="board.py"/"pm_update.py") main(argv) 에 prog 파라미터가 없어, `--help`·인자 에러
# 시 usage 줄에 그 파일명이 새어 나온다 — 에이전트가 칠 실제 커맨드(pm-config init/update)와
# 불일치해 오인을 부른다(ADR-0043 부수 CLI 위생·facade[pm-config.sh/.cmd]가 진입점이므로
# usage 도 facade 이름이어야 카드[ADR-0045]↔실행 표기가 일치). 두 main 을 수정하지 않고
# (touches 격리·CLI 규격 단일 진실 = board/pm_update 보존) 이 파사드에서 위임 동안만 파서
# 생성 시점의 top-level prog 를 치환한다.
_FACADE_PROG = "pm-config"   # facade 이름 — build_parser 의 prog 와 동일.


@contextlib.contextmanager
def _forwarded_prog(prog_map: "dict[str, str]"):
    """위임 동안 argparse ArgumentParser 의 지정된 top-level prog 만 치환한다 (T-0249).

    board.py·pm_update.py 는 prog 를 파일명으로 하드코딩하고 main() 에 prog 인자가 없어,
    pm-config 가 위임할 때 usage 줄에 파일명이 새어 나온다. 두 엔진을 수정하지 않고(touches
    격리·CLI 규격 단일 진실 보존) 파서 *생성 시점*에 명시 `prog=` kwarg 가 매핑 키와 정확히
    일치할 때만 값을 갈아끼운다:
      - board: "board.py"→"pm-config" — init 서브파서 usage 는 부모 prog 에서 "pm-config init"
        로 **자동 파생**(argparse add_parser 관례)되므로 이 top-level 치환 하나로 forward 된
        전 usage 줄이 정합한다.
      - pm_update: "pm_update.py"→"pm-config update"(플랫 파서·서브커맨드 없음).
    파생된 subparser prog(예: "pm-config init")·매핑 밖 prog 는 그대로 통과한다. 위임 종료
    후 `__init__` 를 원복한다 — `--help`/인자 에러의 SystemExit·기타 예외에도 finally 로 복구.
    """
    original_init = argparse.ArgumentParser.__init__

    def _patched_init(self, *args, **kwargs):
        prog = kwargs.get("prog")
        if prog in prog_map:
            kwargs["prog"] = prog_map[prog]
        original_init(self, *args, **kwargs)

    argparse.ArgumentParser.__init__ = _patched_init
    try:
        yield
    finally:
        argparse.ArgumentParser.__init__ = original_init


# ── 서브커맨드 핸들러 ─────────────────────────────────────────────────────────


def _resolve_clone_git_url(name, cli_git, already_registered, board_mod):
    """bare clone 소스 git URL 을 해소한다 (T-0291·multi-user hydrate). 실패 → None(fail-loud 신호).

    `--git` 미제공 시 areas.md `git` 칼럼(레지스트리·git-tracked·공유)에서 URL 을 읽어,
    already-registered repo 의 `.repos/<repo>.git` bare mirror 를 재제공 없이 hydrate 한다 —
    하나의 채택 폴더를 여러 사람이 clone 할 때 mirror(`.repos/`·gitignore·per-clone)가 없는
    2번째 사용자 시나리오. 반환 `None` 은 호출자(cmd_repo_add)가 rc 1 로 바꾸는 fail-loud
    신호다(에러/경고 문구는 이 함수가 이미 print — 부작용 0·clone 전에 호출됨).

      - already_registered:
          - `--git` 미제공          → areas URL 로 해소(hydrate). areas 에도 URL 없으면 fail-loud.
          - `--git` 제공·areas 값과 다름 → 경고 + **areas 값 우선**(등록=단일 진실·mirror origin 은
                                        등록과 일치해야). CLI URL 로 바꾸려면 areas.md 를 직접 수정.
          - `--git` 제공·일치/areas 빔  → `--git` 값.
      - 미등록:
          - `--git` 제공            → 그 값(신규 repo 등록).
          - `--git` 미제공          → fail-loud(신규 repo 는 clone 원 URL 을 areas 에서 해소 불가).
    """
    areas_url = board_mod._areas_git_url(name) if already_registered else None

    if cli_git:
        if already_registered and areas_url and areas_url != cli_git:
            print(
                f"[경고] `--git {cli_git}` 가 areas.md 등록 URL({areas_url}) 과 다르다 — "
                "등록이 단일 진실이므로 areas URL 로 bare mirror 를 만든다(mirror origin 은 "
                "등록과 일치해야 한다). CLI URL 로 바꾸려면 areas.md 를 직접 수정하라.",
                file=sys.stderr,
            )
            return areas_url
        return cli_git

    # `--git` 미제공
    if already_registered:
        if areas_url:
            print(f"✓ `--git` 미제공 — areas.md 등록 URL 로 hydrate: {areas_url} (T-0291·2번째 사용자).")
            return areas_url
        print(
            f"[중단] repo {name!r} 는 areas.md 등록됐으나 `git` 칼럼이 비어 clone 원 URL 을 "
            "해소할 수 없다 — `--git <url>` 로 명시하라 (clone/등록 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return None
    print(
        f"[중단] `--git <url>` 필수 — repo {name!r} 는 areas.md 미등록이라 clone 원 URL 을 "
        "areas 에서 해소할 수 없다(신규 repo 등록). multi-user 2번째 사용자 hydrate 라면 먼저 "
        "등록(areas.md)을 공유받아야 한다 (clone/등록 전혀 하지 않았다).",
        file=sys.stderr,
    )
    return None


def cmd_repo_add(
    args: argparse.Namespace,
    *,
    board=None,
    clone_runner: GitRunner | None = None,
    repos_dir: Path | None = None,
    worktree_pool=None,
) -> int:
    """`repo add <name> [--git <url>] [--test "<cmd>"] [--base <branch>]` — 패밀리에 repo 등록 (ADR-0014·T-0075).

    **`--git` 은 optional (T-0291)**: 미제공 시 이미 등록된 repo 는 areas.md `git` 칼럼 URL 로
    bare mirror 를 hydrate 한다(하나의 채택 폴더를 여러 사람이 clone 할 때 `.repos/` mirror 가
    없는 2번째 사용자 시나리오·재제공 없이 재사용). 미등록 신규 repo 는 `--git` 필수(부작용 0
    fail-loud). clone 소스 URL 해소는 `_resolve_clone_git_url` 이 담당한다(불일치 시 areas 우선).

    1. areas.md 레지스트리에 per-repo 줄을 기록한다(board.areas_append — repo/prefix/
       git/test_cmd/owner/base 칼럼·ADR-0014·T-0075). **prefix 는 빈 값으로 등록**한다 —
       repo명 자동시드 폐지(ADR-0042 §Decision 2·prefix=작업 카테고리이지 repo명이 아님).
       카테고리가 필요하면 `board.py new --prefix <cat>` 로 티켓별 명시하거나 `board.py
       prefix` 로 사후 관리한다. owner = 등록 식별자(registrant·협업 소유자 아님·ADR-0016) —
       기본 현 세션.
    2. `.repos/<name>.git` 로 bare clone 한다 — worktree 풀이 공유하는 .git 원(ADR-0011·
       worktree add 가 이 bare 를 base 로 슬롯을 만든다).

    **base 브랜치 해소 (T-0075)** — clone 후, areas 등록 *전*(등록 줄에 base 를 박아야 하므로):
      - `--base` 미지정 → bare HEAD(`git -C .repos/<name>.git symbolic-ref --short HEAD`
        = 원격 default 브랜치)를 해소해 명시값화·기록. worktree add 가 슬롯 브랜치를 그
        base 에서 파생한다(T-0075). HEAD 해소 실패 시 base 빈 값(현행 bare HEAD 동작 폴백).
      - `--base <b>` 지정 → 로컬 브랜치 검증(`git -C .repos/<name>.git show-ref --verify --quiet refs/heads/<b>` rc==0).
        없으면 명확한 에러 rc 1(clone 은 됐으나 areas 등록은 막아 잘못된 base 기록 방지).

    **멱등·재시도 가능** (두 부작용 — bare clone + areas 등록 — 이 부분 성공할 수 있음):
      - 이미 등록 + bare 존재     → 완전 no-op rc0 (둘 다 이미 됨·친절 메시지).
      - 이미 등록 + bare 부재     → clone *진행*(재시도) 후 등록 건너뜀(append-only).
                                    첫 실행이 등록만 남기고 clone 실패한 경우의 복구 경로다.
      - 미등록                    → clone → base 해소·검증 → areas_append.
    base 해소가 clone 에 의존하므로(bare HEAD 해소/존재 검증) **clone 을 먼저** 한다 —
    미등록 경로는 clone→base 해소→areas_append. 이미 등록 경로는 base 가 이미 박혀 있어
    재해소하지 않는다(append-only·중복 등록 금지). T-0075 이전엔 areas 등록이 clone 앞이라
    clone 실패해도 등록이 남았지만, 이제 clone 성공 후에만 등록한다(재실행이 둘 다 다시 함).

    board / clone_runner / repos_dir 주입으로 hermetic 테스트(실 등록·clone 없이 배선 검증).
    base 해소 git 호출(symbolic-ref/show-ref)은 같은 `clone_runner` 를 `-C <bare>` 로 재사용한다.
    """
    board_mod = board or _load_module("board", "board.py")
    if board_mod is None:
        print(
            "[중단] board.py 엔진을 찾을 수 없다 — areas.md 등록 불가 "
            f"({TOOLS_DIR / 'board.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    # repos_dir 미주입 시 모듈 전역(REPOS_DIR)을 *호출 시점*에 해소한다 — 함수 default 로
    # 굳히면 테스트의 monkeypatch(REPOS_DIR)가 안 먹는다.
    repos_dir = repos_dir if repos_dir is not None else REPOS_DIR

    name = args.name

    # name 가드 (T-0078) — **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 검증한다.
    # `../x` 폴더탈출·슬래시/공백/`.` 의 areas.md 줄 corruption·leading `-`·빈 문자열을 입구에서
    # 막는다(fail-closed·부분 부작용 0). CLI·콘솔 두 경로가 결국 이 sink 로 수렴하므로 여기 한 곳.
    if not _validate_repo_name(name):
        print(
            f"[중단] repo 이름 {name!r} 형식 위반 — 허용: 영숫자로 시작, 이후 영숫자/`_`/`-` "
            "(정규식 `^[A-Za-z0-9][A-Za-z0-9_-]*$`). 경로분리자(`/`)·`.`·공백·leading `-`·빈 "
            "이름은 금지(폴더탈출·areas.md 줄 corruption 방지). clone/등록/훅 전혀 하지 않았다.",
            file=sys.stderr,
        )
        return 1

    # case-only 중복 거부 (ADR-0055·repo명=prefix 동일성=case-insensitive fold) — 정확-case 는
    # 아래 already_registered 재사용(멱등) 경로로 빠지지만, case 만 다른 근접 중복(`svc` vs 등록
    # `SVC`)은 레지스트리에 fold-충돌하는 두 행을 만든다(네임스페이스 분할). **어떤 부작용(clone·
    # mkdir·등록·훅)보다 앞에서** 등록 canonical case 로 안내하고 fail-loud(부작용 0).
    registered_names = board_mod.registered_repos()
    if name not in registered_names:
        fold = name.lower()
        conflict = next((r for r in registered_names if r.lower() == fold), None)
        if conflict is not None:
            print(
                f"[중단] repo {name!r} 은 이미 등록된 {conflict!r} 과 대소문자만 다르다 "
                f"(repo명·prefix 동일성은 case-insensitive·ADR-0055). 등록된 case {conflict!r} 를 "
                "그대로 쓰라 (clone/등록/훅 전혀 하지 않았다·부작용 0).",
                file=sys.stderr,
            )
            return 1

    # owner = areas.md 등록 식별자(registrant·**귀속 쓰기**) — 미해소면 fail-loud
    # (**어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서**·board.cmd_init owner 와 동형·
    # ADR-0040 D1). _default_session 이 미바인딩(leased ≥2·무바인딩)에서 None 을 돌려주므로,
    # 그대로 areas_append 에 넘기면 board 가 owner 를 문자열 "None" 으로 areas.md 에 누출한다 —
    # 그 전에 차단한다. `repo add` 의 정체성 인자는 `--owner <id>` 뿐(T-0313 findings·ADR-0057 —
    # 존재하지 않는 `--session` 을 안내하던 오안내를 제거) — 명시하거나 세션을 바인딩하라.
    owner = args.owner or _default_session()
    if not owner:
        print(
            "[중단] 등록 owner 미해소 — 활성 슬롯이 여럿이거나 세션 바인딩이 없다. "
            "`--owner <id>` 로 등록 식별자를 명시하거나 세션을 바인딩(단일 활성 슬롯/"
            "`$PM_SESSION_NAME`)하라 (clone/등록/훅 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return 1
    # area_owner = 그 area 의 *user* 소유(`--mine` 풀 입력·ADR-0033 ③·T-0161) — registrant
    # `owner`(슬롯/세션)와 별개 칼럼(overload 금지·codex sug). `--user` 명시 > local.conf user=
    # > git config user.email > None(빈 칼럼·_repo_area_owner None 폴백·현행 동작).
    area_owner = getattr(args, "user", None) or _default_user()
    base_arg = getattr(args, "base", None)
    bare_path = repos_dir / f"{name}.git"
    # 멱등 재등록 판별은 **repo명**으로 한다(prefix 로 세지 않는다) — 자동시드 폐지(ADR-0042)
    # 후 prefix 칼럼이 비므로 `registered_prefixes()` 는 이 repo 를 못 센다. `registered_repos()`
    # 는 repo 칼럼을 직접 세어 중복 append(같은 repo 두 줄)를 막는다. 위 case-only 가드에서 이미
    # 조회한 `registered_names` 를 재사용한다(같은 areas 스냅샷·중복 조회 dedupe).
    already_registered = name in registered_names
    runner = clone_runner or _real_clone_runner()

    # bare 는 *경로 존재*(exists)만이 아닌 *실 bare git repo* 인지로 판정한다 (T-0294) — 중단된
    # `git clone --bare`(하네스 타임아웃·Ctrl-C)가 남긴 부분/빈/깨진 `.repos/<name>.git` 도
    # exists()=True 라, 경로 존재만 보면 "존재 → 재사용"으로 조용히 통과시켜 무효 bare 를 재사용한다
    # (repo add 는 rc0 인데 invariant 깨진 채·나중 worktree add 가 날 git 에러로 죽음·audit #1→#4).
    # `_is_valid_bare`(rev-parse --is-bare-repository·worktree_pool)로 실 bare 를 판정해 그 통과를
    # fail-loud 로 닫는다. **파괴적 재clone 신중(§결정)**: 깨진 bare 자동삭제는 위험(사용자 데이터
    # 오판)이라 하지 않고, 정확 진단 + 수동 삭제 위임으로 안내한다(삭제는 사용자 위임 원칙). fail-soft:
    # worktree_pool 로드 실패/헬퍼 부재(구 엔진)면 존재-검증 폴백(구 동작 보존·크래시 0).
    wp_mod = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    valid_bare_fn = getattr(wp_mod, "_is_valid_bare", None) if wp_mod is not None else None
    bare_present = bare_path.exists()
    if bare_present and valid_bare_fn is not None and not valid_bare_fn(bare_path, runner=runner):
        print(
            f"[중단] `.repos/{name}.git` 경로는 있으나 유효한 bare git repo 가 아니다 — 부분/깨진 "
            f"bare (중단된 `git clone --bare` 잔존 가능성·하네스 타임아웃/Ctrl-C). 경로 존재만 보면 "
            f"재사용으로 조용히 통과하지만 `git worktree add` 의 base 로 못 써 나중 날 git 에러로 죽는다. "
            f"자동 삭제는 하지 않는다(사용자 데이터 오판 위험·삭제는 사용자 위임) — `.repos/{name}.git` 를 "
            f"수동 삭제 후 `pm-config repo add {name}`(--git 불요·areas 등록 URL 로 재hydrate·미등록이면 "
            f"`--git <url>` 재제공)로 재생성하라 (T-0294·부작용 0·clone/등록/훅 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return 1
    # 여기 도달 = bare 부재이거나 유효 bare (무효는 위에서 return 1) — 유효 bare 존재면 clone 건너뛰고
    # 재사용, 부재면 clone (재)시도. 종전 `bare_exists` 의미(존재 → 재사용)를 실 bare 로 좁힌 것.
    bare_exists = bare_present

    # clone 소스 git URL 해소 (T-0291) — `--git` 미제공 시 areas.md `git` 칼럼에서 해소해
    # multi-user 2번째 사용자의 bare mirror hydrate 를 완성한다(재제공 없이). None 이면
    # fail-loud(미등록+`--git` 없음 등) — 어떤 부작용(clone·mkdir·등록)보다 앞에서 걸러
    # 부작용 0 을 보장한다. 해소된 URL 을 이하 clone/areas_append 가 일관되게 쓴다.
    # URL 은 **실제로 필요할 때만** 해소한다 — clone(bare 부재) 또는 신규 등록(미등록)일 때. 이미
    # 등록 + bare 존재면 순수 no-op(refspec/tracking/보호훅 자가치유·아래 이미 등록 분기)라 URL 이
    # 불필요하니, areas `git` 칼럼이 빈 레거시/부분 등록이어도 fail-loud 하지 않는다(codex 지적·
    # `repo add` 재실행=훅 자가치유 경로 보존·`--git` optional 계약 완결).
    git_url = None
    if (not bare_exists) or (not already_registered):
        git_url = _resolve_clone_git_url(name, args.git, already_registered, board_mod)
        if git_url is None:
            return 1

    # 두 부작용(bare clone + areas 등록)을 멱등화한다 — base 해소(T-0075)가 bare 에 의존
    # 하므로(bare HEAD/존재 검증) **clone 을 먼저**, 그 다음 base 해소·areas 등록 순서다.

    # 1) bare clone → .repos/<name>.git (worktree 풀 공유 .git 원·ADR-0011). bare 가 이미
    #    있으면 건너뛴다(재사용·중복 clone/클로버 방지). bare 부재면 clone(재)시도.
    if bare_exists:
        print(f"✓ .repos/{name}.git 이미 존재 — clone 건너뜀 (재사용).")
    else:
        repos_dir.mkdir(parents=True, exist_ok=True)
        rc, out = runner(["clone", "--bare", git_url, str(bare_path)])
        if rc != 0:
            print(
                f"[경고] `git clone --bare {git_url}` 실패 (rc={rc}):\n{out}\n"
                f"  네트워크/URL 확인 후 수동으로 `git clone --bare {git_url} {bare_path}` 하거나 "
                "재시도하라(등록은 clone 성공 후·멱등).",
                file=sys.stderr,
            )
            return 1
        print(f"✓ .repos/{name}.git bare clone 완료.")

    # 1b) bare fetch refspec 보정 (T-0152) — clone 성공/기존 bare 재사용 *둘 다* 에서 수행한다.
    #     `git clone --bare` 가 remote.origin.fetch 를 설정하지 않아 origin/* remote-tracking
    #     ref(origin/main)가 안 생기는 결함을 근절한다(멱등 — refspec-없는 과거 bare 도 보정).
    #     clone 실패 시엔 위에서 이미 return 1 했으므로 여기 도달 = bare 존재 전제(fail-soft).
    _set_bare_fetch_refspec(bare_path, runner=runner)

    # 1c) bare 기본 브랜치 origin tracking 보정 (T-0273 — T-0152 나머지 절반). refspec 이 origin/*
    #     remote-tracking ref 를 채웠고, 이 헬퍼가 로컬 branch.<d>.remote/merge tracking config 를
    #     박아 그 bare 슬롯 기본 브랜치의 @{upstream}(origin/<default>) 해소를 닫는다. refspec 보정과
    #     같은 위치(clone 성공/기존 bare 재사용 둘 다·already_registered early-return 이전)라 과거
    #     tracking-없는 bare 도 다음 repo add 에 자가치유된다(멱등·fail-soft).
    _ensure_bare_branch_tracking(bare_path, runner=runner)

    # 2) 이미 등록돼 있으면 base 재해소/등록을 건너뛴다(append-only·중복 등록 금지). base 는
    #    첫 등록 때 박힌 값 그대로(clone 만 실패했던 재시도 경로는 위 clone 으로 복구됨).
    #    보호 훅(T-0076)은 멱등 자가치유라 *재등록 경로에서도* (재)설치한다 — 엔진 update 후
    #    기존 repo 도 다음 repo add/worktree add 에 훅을 얻는다(별도 명령 불요).
    if already_registered:
        print(f"✓ repo {name!r} 이미 areas.md 등록됨 — 등록 건너뜀.")
        if _install_protected_hook(name, board=board_mod, worktree_pool=worktree_pool):
            print(f"✓ 보호 브랜치 pre-push 훅 (재)설치: {name} (T-0076).")
        return 0

    # 3) base 브랜치 해소 (T-0075) — bare 가 존재하는 지금 시점에 해소·검증한다.
    #    --base 지정 → 존재 검증(없으면 rc 1·areas 등록 막음). 미지정 → bare HEAD 명시값화.
    base = _resolve_base(base_arg, bare_path, runner=runner)
    if base is _BASE_INVALID:
        print(
            f"[중단] --base {base_arg!r} 가 `.repos/{name}.git` 에 없다 "
            f"(`git -C {bare_path} show-ref --verify --quiet refs/heads/{base_arg}` 실패). "
            "브랜치명을 확인하거나 `--base` 를 생략(기본 브랜치 사용)하라.",
            file=sys.stderr,
        )
        return 1

    # 4) areas.md 등록 — repo/prefix/git/test_cmd/owner/base/protected 칼럼(ADR-0014·T-0075·T-0076).
    #    prefix(2번째 positional)는 **빈 값**으로 등록 — repo명 자동시드 폐지(ADR-0042): repo명은
    #    작업 카테고리가 아니다. 이전엔 `name`(repo명)을 prefix 로 박아 다음 티켓이 `T-<repo>-NNN`
    #    으로 튀게 했다. protected 도 빈 값 — `_repo_protected` 가 DEFAULT_PROTECTED(main/master/
    #    develop) 폴백한다(per-repo override 는 areas.md 를 직접 편집·후속 `--protected` 플래그 여지).
    board_mod.areas_append(
        "", "", owner, repo=name, git=git_url, test_cmd=args.test, base=base,
        protected="", area_owner=area_owner,
    )
    # --test 미지정(None) 이면 areas test_cmd 빈 값 — 해소 체인이 슬롯/local.conf 로
    # 폴백한다(T-0066). 빌드명령은 worktree add 프롬프트·콘솔 [b] 에서 채울 수 있다.
    test_surface = args.test if args.test else "(미지정 — worktree add/콘솔 [b] 에서 설정)"
    base_surface = base if base else "(미해소 — worktree add 가 bare HEAD 사용)"
    area_owner_surface = area_owner if area_owner else "(미상 — local.conf user= / git user.email 미설정)"
    print(
        f"✓ areas.md 등록: {name} | git={git_url} | test_cmd={test_surface} | "
        f"owner={owner} | base={base_surface} | area_owner={area_owner_surface}"
    )
    # 5) 보호 브랜치 pre-push 훅 설치 (T-0076·멱등 자가치유) — 보호목록(areas protected→
    #    default) sidecar + bare core.hooksPath wiring. 회사 repo 무영향(.project_manager/.local).
    if _install_protected_hook(name, board=board_mod, worktree_pool=worktree_pool):
        print(f"✓ 보호 브랜치 pre-push 훅 설치: {name} (T-0076·기본 main/master/develop).")
    return 0


def _prompt_test_cmd(input_fn: Callable[[str], str], *, default: str) -> str | None:
    """worktree add 빌드명령 프롬프트 — `빌드/테스트 명령? [Enter=repo 기본값 유지 <default>]:`.

    **빈 입력(엔터만) → None(슬롯 미바인딩)** — 슬롯 리스 test_cmd 가 board 의 해소 체인서
    areas per-repo test_cmd 보다 *우선*([[T-0066]])이라, 빈입력에 기본값을 박으면 areas 의
    그 repo test_cmd(예: `go test ./...`)를 잘못 덮는다(must-fix 1·codex). 빈입력은 None 으로
    슬롯을 비워 해소 체인이 areas/local.conf 로 폴백하게 한다(기존 동작 보존). `default` 는
    *표시 전용* — Enter 시 적용될 repo 폴백값(areas→local.conf→pytest-q)을 투명하게 보일 뿐
    슬롯엔 안 박는다. 비어있지 않은 입력만 그 값으로 슬롯에 바인딩된다.

    `EOFError`/`KeyboardInterrupt` 는 우아하게 None 으로 흡수(미지정 폴백·크래시 0). input_fn
    주입으로 hermetic 테스트(라이브 input 블록 0·주입 시퀀스로 결정적).
    """
    try:
        raw = input_fn(f"빌드/테스트 명령? [Enter=repo 기본값 유지: {default}]: ")
    except (EOFError, KeyboardInterrupt):
        return None
    cmd = raw.strip()
    return cmd if cmd else None


def cmd_worktree_add(
    args: argparse.Namespace,
    *,
    worktree_pool=None,
    board=None,
    input_fn: Callable[[str], str] = input,
    is_tty: Callable[[], bool] | None = None,
) -> int:
    """`worktree add <repo> [--test "<cmd>"]` — 새 슬롯 생성 + submodule init (ADR-0013).

    worktree_pool.create_slot(repo, base=) 를 호출한다 — `<repo>_<N>`(브랜치 무관 재사용
    컨테이너) 슬롯을 `git worktree add` 로 만들고 `git submodule update --init` 한다.
    브랜치 무관(명령표 외 — 브랜치 할당은 pm-bootstrap 소관·spike §8-5).

    **base 브랜치 (T-0075)**: areas.md 의 그 repo base(`pm-config repo add --base`/clone-time
    bare HEAD 가 기록)를 `_resolve_repo_base` 로 읽어 `create_slot(base=)` 로 전달한다 — 슬롯
    브랜치 `<repo>_<N>` 가 그 base(develop 등)에서 파생된다. areas 에 base 없으면(구 스키마/
    솔로/미지정) None → create_slot 이 현행 bare HEAD 동작(회귀 0).

    test_cmd(슬롯 리스 바인딩·T-0066·ADR-0014 amend) 해소:
      - `--test "<cmd>"` 명시 → 그 값을 바인딩(현행·CLI 정확작업·CI).
      - `--test` 미지정 + **tty** → 슬롯 생성 후 빌드명령 프롬프트. **빈입력(Enter) → None
        (슬롯 미바인딩)** → 해소 체인이 areas/local.conf 로 폴백(기존 동작 보존·must-fix 1).
        프롬프트 `[Enter=repo 기본값 유지: …]` 표시값은 그 repo 의 *실제 폴백*(areas test_cmd
        → local.conf → pytest -q·`_resolve_repo_test_cmd`)이라 Enter 가 무엇을 적용하는지 투명.
      - `--test` 미지정 + 비-tty(CI/파이프) → 프롬프트 생략·None(현행·repo areas/local.conf 로 해소).
    board._test_cmd 가 활성 슬롯의 이 값을 areas 위 레이어로 읽으므로, 빈입력에 기본값을 박으면
    areas per-repo test_cmd 를 잘못 덮는다 → 빈입력은 반드시 None(슬롯 미바인딩)이어야 한다.

    **성공 출력 다음스텝 (T-0296·audit #6)**: 슬롯 fs 생성만으로 끝나지 않고, 다음 필수 스텝인
    슬롯을 세션에 바인딩(`/pm-bootstrap <repo> --slot <N>`·정체성 선언)으로 이어준다. N 은 이미
    보유한 `lease.slot`(`work/<repo>_<N>`)에서 파싱(신규 조회 0). **자동바인딩 안 함** — 바인딩은
    여전히 사용자 명시 스텝(정체성=대화 맥락·lean multi-PM). 솔로/단일 슬롯은 무인자 부트스트랩 힌트.

    worktree_pool/board/input_fn/is_tty 주입으로 hermetic 테스트(실 worktree add·라이브 input
    없이 배선·분기 검증). board 는 프롬프트 표시값 areas 해소 재사용용(콘솔이 로드한 board 전달).
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 슬롯 생성 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    # test_cmd 해소: --test 명시면 그 값. 미지정 + tty 면 프롬프트(빌드명령). 비-tty 면 None.
    # 프롬프트 표시 기본값은 그 repo 의 실제 폴백(areas→local.conf→pytest-q)을 resolve 해
    # 보여준다(Enter 시 적용될 값 투명화) — 빈입력은 그래도 None 으로 슬롯을 비운다(must-fix 1).
    tty_check = is_tty if is_tty is not None else _stdin_is_tty
    test_cmd = getattr(args, "test", None)
    if test_cmd is None and tty_check():
        display_default = _resolve_repo_test_cmd(args.repo, board=board)
        test_cmd = _prompt_test_cmd(input_fn, default=display_default)

    # base 해소 (T-0075) — areas.md 의 그 repo base 를 읽어 create_slot(base=) 로 전달한다.
    # 슬롯 브랜치 `<repo>_<N>` 가 그 base(repo add 가 기록·develop 등)에서 파생된다. areas 에
    # base 없으면(구 스키마/솔로/미지정) None → create_slot 이 현행 bare HEAD 동작(회귀 0).
    # board 직접 import 금지(ADR-0013 isolation) — 주입/로드된 board 의 `_repo_base` 만 쓴다.
    base = _resolve_repo_base(args.repo, board=board)

    try:
        lease = wp.create_slot(args.repo, base=base, test_cmd=test_cmd)
    except RuntimeError as exc:
        print(f"[중단] worktree 슬롯 생성 실패: {exc}", file=sys.stderr)
        return 1
    slot_path = wp.slot_path(lease.slot)
    test_line = f"\n  test_cmd 바인딩: {lease.test_cmd!r} (이 슬롯 회귀명령)" if lease.test_cmd else ""
    # 슬롯 번호 N (lease.slot = `work/<repo>_<N>`) 파싱 — /pm-bootstrap --slot <N> 바인딩 안내용
    # (T-0296·audit #6). 이미 보유한 lease 에서만 뽑는다(신규 조회 0). 형식 이탈이면(prefix 불일치/
    # 비숫자) 슬롯 식별자 그대로 fallback surface — 안내가 침묵하지 않게.
    slot_prefix = f"work/{lease.repo}_"
    slot_tail = lease.slot[len(slot_prefix):] if lease.slot.startswith(slot_prefix) else ""
    slot_num = slot_tail if slot_tail.isdigit() else lease.slot
    print(
        f"✓ worktree 슬롯 생성: {lease.slot} (repo={lease.repo}) → {slot_path}{test_line}\n"
        "  코드 작업은 이 슬롯 cwd 에서 — 보드/wiki 는 multi-PM 공유 `.project_manager`.\n"
        f"  다음 스텝 — 이 슬롯을 세션에 바인딩: `/pm-bootstrap {lease.repo} --slot {slot_num}` "
        "(정체성 선언·자동 아님). 솔로/단일 슬롯이면 무인자 `/pm-bootstrap` 가 자동바인딩."
    )
    # 보호 브랜치 pre-push 훅 (재)설치 (T-0076·멱등 자가치유) — 슬롯 op 마다 (재)설치해 엔진
    # update 후 기존 repo 도 다음 worktree add 에 훅을 얻는다(별도 명령 불요·회사 repo 무영향).
    if _install_protected_hook(args.repo, board=board, worktree_pool=wp):
        print(f"✓ 보호 브랜치 pre-push 훅 (재)설치: {args.repo} (T-0076).")
    return 0


def cmd_status(
    args: argparse.Namespace,
    *,
    worktree_pool=None,
) -> int:
    """`status | whoami` — 풀/리스 + 이 세션 repo/슬롯/branch surface (ADR-0011·0013).

    worktree_pool.list_leases() 로 전체 리스 장부를 surface 하고, 이 세션($CLAUDE_
    SESSION_NAME)이 보유한 leased 슬롯을 별도로 강조한다(whoami 의 "나" 표면).
    status·whoami 는 같은 데이터·같은 핸들러 — whoami 는 이 세션 줄을 머리에 둔다.

    브랜치는 `worktree_pool.current_branch(slot)` 로 슬롯 worktree 의 git HEAD 에서 **live**
    조회한다(ADR-0013 amend T-0072 — git=진실·장부 저장 폐지). 사용자가 슬롯서 직접 `git
    checkout` 해도 즉시 반영·드리프트 없음. detached/조회불가는 "(detached/조회불가)".

    worktree_pool 주입으로 hermetic 테스트.
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 리스 상태 조회 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    leases = wp.list_leases()
    sess = _default_session()
    mine = [l for l in leases if l.state == "leased" and l.session == sess]

    # 브랜치는 슬롯 worktree 의 git HEAD 에서 live 조회한다(ADR-0013 amend T-0072 —
    # git=진실·장부 저장 폐지). 사용자가 슬롯서 직접 `git checkout` 해도 즉시 반영.
    # detached/조회불가는 None → "(detached/조회불가)" 로 surface.
    def _live_branch(slot: str) -> str:
        return wp.current_branch(slot) or "(detached/조회불가)"

    print(f"# pm-config {args.command} — 세션: {sess or '(비바인딩)'}")

    # 정체성·세션격리 posture surface(ADR-0053 #4·anti-degrade 진단): resolved user + isolation
    # 상태(strict/degrade/solo) + remedy. board.py 를 import 하지 않고([[ADR-0013]] isolation)
    # user 는 `_default_user`(자체 해소·`_local_conf_session` 동형), 다중사용자 여부는 areas.md
    # `area_owner` 자체 파싱(`_distinct_area_owners`)으로 판정한다 — board 의 격리 *판정*은 복제
    # 하지 않고 최소 신호만.
    #
    # **정직화(should-fix·오안심 방지)**: 여기 posture 는 areas.md `area_owner`(registry) *coarse*
    # 신호다. board `list --mine` 의 실 strict-exclude 는 티켓 귀속(created_by/claimed_by·
    # `_distinct_ticket_users`)으로 판정하며 — 부분마이그레이션 보드(티켓은 2명·area_owner 미채움/
    # 1개)에서 두 신호가 갈린다(그게 ADR-0053 이 겨냥하는 degrade-risk 케이스). 그래서 무조건 "정상"
    # 단언을 금하고, 신호 출처·한계를 문구에 노출하고 authoritative 신호로 `board list --mine`
    # (strict-exclude loud-warn)을 가리킨다([[robustness-value-connections-before-ship]] silent-degrade
    # 근절 취지). board 티켓 스캔 복제는 하지 않는다([[ADR-0013]] 유지).
    resolved_user = _default_user()
    multi_user = _distinct_area_owners() > 1
    _remedy = "`board init --owner <you>` 또는 `board migrate-identity`"
    _authoritative = "실 격리는 `board list --mine`(strict-exclude loud-warn)이 authoritative"
    print(f"## 정체성(user): {resolved_user or '(미해소 — local.conf user= / git config user.email 미설정)'}")
    if not multi_user:
        print("## 세션격리(registry/area_owner 기준): solo (단일/미등록 registry) — 단, 티켓 귀속"
              "(created_by/claimed_by)이 다중이면 세션 뷰가 strict-exclude 될 수 있다. "
              f"{_authoritative}.")
    elif resolved_user is not None:
        print("## 세션격리(registry/area_owner 기준): strict (다중사용자·정체성 해소 — 세션 뷰 = "
              f"내 소유 open + claim). {_authoritative}.")
    else:
        print("## 세션격리(registry/area_owner 기준): ⚠ degrade-risk (다중사용자·정체성 미해소 — "
              f"소유 미해소 open 이 `board list --mine` 에서 strict-exclude). remedy: {_remedy}")

    if mine:
        print("## 이 세션의 리스:")
        for l in mine:
            print(f"  - {l.slot} (repo={l.repo} · branch={_live_branch(l.slot)})")
    else:
        print("## 이 세션의 리스: (없음)")

    print("## 풀 전체 리스 장부:")
    if not leases:
        print("  (리스 없음 — 아직 worktree 슬롯이 생성되지 않음)")
    for l in leases:
        print(
            f"  - {l.slot} · repo={l.repo} · branch={_live_branch(l.slot)} · "
            f"state={l.state} · session={l.session or '-'} · pid={l.pid}"
        )

    # git worktree × 장부 정합(reconcile·T-0295) — 실 git worktree 와 장부를 대조해 drift 를
    # surface 한다. **조회 전용·부작용 0**: 삭제/prune/이동 안 함 — 판정·복구 안내만(자동삭제는
    # 사용자 위임·파일 삭제 원칙). reconcile 실패는 status 를 막지 않는다(fail-soft·advisory).
    #   - orphan = git worktree 존재·장부 미등록(중단된 create/수동 add 잔존·audit #2·다음 create
    #     번호 충돌·audit #4 의 근원). status 가 이걸 못 보던 게 audit #3.
    #   - stale = 장부 등록·worktree 없음(dir 삭제/prune).
    #   - incomplete = provisional("creating") — worktree add 후 확정 전 중단된 create 흔적.
    try:
        recon = wp.reconcile_worktrees()
    except Exception:  # noqa: BLE001 — reconcile 실패가 status 를 막지 않는다(조회 전용).
        recon = None
    if recon is not None and (recon.orphans or recon.stale or recon.incomplete):
        print("## ⚠ worktree × 장부 drift (조회 전용 — 자동삭제 안 함·정리는 사용자):")
        for w in recon.orphans:
            print(
                f"  - [orphan] {w.slot} · git worktree 존재·장부 미등록 "
                "(중단된 create/수동 add 잔존) — 다음 create 번호 충돌·status blind 원인"
            )
        for l in recon.stale:
            print(
                f"  - [stale] {l.slot} · 장부 등록(state={l.state})·git worktree 없음 "
                "(dir 삭제/prune) — 장부 잔여"
            )
        for l in recon.incomplete:
            print(
                f"  - [incomplete] {l.slot} · 중단된 생성(state=creating·provisional) "
                "— worktree add 후 확정 전 중단(SIGKILL 등) 가능성"
            )
        print(
            "  복구:\n"
            "    - orphan worktree(git 측·disk 에 존재·작업 있을 수 있음) → 확인 후 "
            "`git -C .repos/<repo>.git worktree remove <경로>` 로 사용자가 삭제(위임 원칙).\n"
            "    - stale/incomplete 중 worktree dir 이 사라진 dangling 장부 엔트리 → "
            "`pm-config worktree prune-stale`(안전·이미 없는 worktree 의 부기만 정리).\n"
            "    - incomplete 인데 worktree 가 남아있으면 위 `git worktree remove` 후 prune-stale."
        )
    return 0


def cmd_worktree_prune_stale(
    args: argparse.Namespace,
    *,
    worktree_pool=None,
) -> int:
    """`worktree prune-stale` — worktree dir 이 사라진 dangling 장부 엔트리를 안전 정리 (T-0295).

    status reconcile 이 surface 한 stale/incomplete 중 **worktree 가 물리적으로 부재**한 장부
    엔트리를 제거한다(worktree_pool.prune_stale_leases). **안전**: 지울 worktree 파일이 없는
    dangling 부기만 정리 — 사용자 데이터/worktree 삭제가 아니라 삭제-위임 원칙 위반이 아니다.
    orphan *worktree*(git 측·disk 존재·작업 가능)는 손대지 않는다(그건 `git worktree remove` 로
    사용자가). reconcile(status)이 조회-전용인 것과 분리된 **명시 user-invoked 정리 진입점**이다.

    worktree_pool 주입으로 hermetic 테스트(실 장부 쓰기 없이 배선 검증).
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — dangling 엔트리 정리 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    pruned = wp.prune_stale_leases()
    if pruned:
        print(
            f"✓ dangling 장부 엔트리 {len(pruned)}개 정리 (worktree 부재·이미 사라진 부기): "
            f"{', '.join(pruned)}"
        )
    else:
        print("정리할 dangling 엔트리 없음 (worktree 부재 장부 엔트리 0).")
    return 0


def cmd_worktree_remove(
    args: argparse.Namespace,
    *,
    worktree_pool=None,
) -> int:
    """`worktree remove <slot> [--force]` — 슬롯 통째 제거(원자·user-invoked·T-0333).

    worktree_pool.remove_slot(slot, force=) 로 리스 확인 → `git worktree remove` → 슬롯 전용
    브랜치 정리 → 장부 엔트리 제거를 한 번에 한다. PM 69 footgun 체인(수동 remove → dangling
    장부 → `add` 가 번호 skip → 뒤늦은 prune)을 원천 종결한다 — 장부 엔트리 제거로 `add` 가
    빈 번호를 재사용한다.

    **사용자 명시 호출 전제** — PM 이 자율 실행하지 않는다(삭제-위임 원칙). `prune-stale`
    (worktree 부재 장부만 정리)과 달리 **실 worktree 를 지운다**. orphan worktree(장부 미등록)는
    여전히 `git worktree remove` 로 사용자가.

    - dirty/활성 리스(leased·사용 중) → 거부(RemoveRefused·rc1). `--force` = stash 보존 후 강제.
    - 장부에 슬롯 없으면 무해 종료(rc0·이미 정리됨).
    - 전용 브랜치(`<repo>_<N>`): 머지 완료면 삭제·미머지면 보존(1줄 보고)·공유 브랜치면 스킵.
    - `git worktree remove` 실패(RuntimeError) → rc1(장부/브랜치 미변경·원자).

    worktree_pool 주입으로 hermetic 테스트(실 worktree remove·장부 쓰기 없이 배선 검증).
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 슬롯 제거 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    try:
        result = wp.remove_slot(args.slot, force=args.force)
    except wp.RemoveRefused as exc:
        if getattr(exc, "reason", None) == "active-lease":
            print(
                f"[중단] 슬롯 {args.slot!r} 이 활성(사용 중·state={exc.state}) — 제거 거부. "
                "먼저 `release` 로 반납하거나 `worktree remove --force`(사용 중 무시).",
                file=sys.stderr,
            )
        else:
            print(
                f"[중단] 슬롯 {args.slot!r} 이 dirty — 제거 거부(작업 유실 방지). "
                "수동 정리 후 재시도하거나 `worktree remove --force`(stash 보존 강제).",
                file=sys.stderr,
            )
        return 1
    except RuntimeError as exc:
        print(f"[중단] worktree 슬롯 제거 실패: {exc}", file=sys.stderr)
        return 1

    if result is None:
        print(
            f"✓ 슬롯 {args.slot!r} 장부에 없음 — 이미 정리됨(무해). "
            "orphan worktree(git 측·장부 미등록)는 `git worktree remove` 로 사용자가."
        )
        return 0

    # ⚠ 활성 리스 강제 회수 경고 — `--force` 로 leased/creating(사용 중) 슬롯을 override 했을 때
    # (reviewer should-fix·티켓 결정 §활성 리스 문구 정합). 다른 세션이 쓰던 슬롯일 수 있음을 stderr 로.
    if getattr(result, "forced_state", None):
        print(
            f"⚠ 활성(state={result.forced_state}) 슬롯 {args.slot!r} 을 강제 회수 "
            "(--force override — 다른 세션이 사용 중이었을 수 있다).",
            file=sys.stderr,
        )

    # 성공 surface — 슬롯 전용 브랜치 처리 결과 1줄(결정 §브랜치 정리).
    if result.branch_action == "deleted":
        branch_line = f" · 전용 브랜치 {result.branch!r} 삭제(머지 완료)"
    elif result.branch_action == "preserved-unmerged":
        branch_line = f" · 브랜치 {result.branch} 보존(미머지)"
    elif result.branch_action == "skipped-shared":
        branch_line = f" · 브랜치 {result.branch!r} 보존(공유/전용 아님·삭제 스킵)"
    else:
        branch_line = ""
    stash_line = " · dirty stash 보존" if result.stashed else ""
    print(
        f"✓ 슬롯 {args.slot!r} 제거 — worktree remove + 장부 엔트리 삭제(빈 번호 재사용 가능)"
        f"{branch_line}{stash_line}."
    )
    # stash 복구 UX(suggestion 1) — 슬롯 worktree 가 사라져도 stash 는 공유 refs/stash 에 남는다.
    if result.stashed:
        print("  복구: 아무 worktree 에서 `git stash list` / `git stash pop` (공유 refs/stash).")
    # 미머지 보존 브랜치 캐비앗(reviewer emergent gap) — 같은 번호 base-경로 재생성이 막힌다.
    if result.branch_action == "preserved-unmerged":
        print(
            f"  ⚠ 미머지 브랜치 {result.branch} 보존됨 — 같은 번호 슬롯의 base-경로 재생성"
            "(worktree add)이 'already exists' 로 막힌다. 재사용하려면 이 브랜치 정리(머지/삭제) 후 재시도."
        )
    return 0


def cmd_set_test_cmd(
    slot: str,
    cmd: str | None,
    *,
    worktree_pool=None,
) -> int:
    """슬롯 빌드/테스트 명령 설정·변경 — 콘솔 `[b]`·"나중에 변경" (T-0069·ADR-0014 amend).

    worktree_pool.set_test_cmd(slot, cmd) 로 기존 슬롯 리스의 test_cmd 를 갱신한다(flock +
    atomic write·worktree_pool 책임). 별도 CLI 서브커맨드는 없다 — 콘솔 `[b]` 와 worktree
    add 프롬프트가 변경 경로를 흡수(결정 §setter 단순화). 장부에 슬롯이 없으면(`KeyError`)
    명시 에러 rc 1(침묵 무력화 금지). `cmd=None`/빈 문자열이면 바인딩 해제(폴백·현행).

    worktree_pool 주입으로 hermetic 테스트(실 장부 쓰기 없이 배선 검증).
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 슬롯 빌드명령 변경 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    normalized = (cmd.strip() or None) if cmd else None
    try:
        lease = wp.set_test_cmd(slot, normalized)
    except KeyError:
        print(
            f"[중단] 슬롯 {slot!r} 에 대한 리스가 없다 — 먼저 `worktree add` 로 슬롯을 만들라.",
            file=sys.stderr,
        )
        return 1
    if lease.test_cmd:
        print(f"✓ 슬롯 {slot} 빌드/테스트 명령 설정: {lease.test_cmd!r} (이 슬롯 회귀명령).")
    else:
        print(f"✓ 슬롯 {slot} 빌드/테스트 명령 해제 — repo areas/local.conf 로 폴백(현행).")
    return 0


def cmd_release(
    args: argparse.Namespace,
    *,
    worktree_pool=None,
) -> int:
    """`release <slot> [--force]` — 작업완료 반납 / 수동 강제 백스톱 (ADR-0013).

    - 기본: worktree_pool.release(slot) — dirty 면 ReleaseRefused(수동 정리 요구).
    - --force: worktree_pool.force_release(slot) — dirty/leased 무시 강제 idle 화
      (dirty 는 stash 보존 시도). 장부에 슬롯 없으면 무해 종료.

    런타임 alloc/release 자동화는 파사드 비관여(bootstrap/handoff) — 여기는 수동
    반납/강제만(spike §8-5·§3e). worktree_pool 주입으로 hermetic 테스트.
    """
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")
    if wp is None:
        print(
            "[중단] worktree_pool.py 엔진을 찾을 수 없다 — 슬롯 반납 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    if args.force:
        lease = wp.force_release(args.slot)
        if lease is None:
            print(f"✓ 슬롯 {args.slot!r} 장부에 없음 — 이미 정리됨(무해).")
        else:
            print(f"✓ 슬롯 {args.slot!r} 강제 반납(idle 화) — dirty 는 stash 보존 시도.")
        return 0

    try:
        wp.release(args.slot)
    except KeyError:
        print(f"[중단] 슬롯 {args.slot!r} 에 대한 리스가 없다.", file=sys.stderr)
        return 1
    except wp.ReleaseRefused:
        print(
            f"[중단] 슬롯 {args.slot!r} 이 dirty — 반납 거부(작업 유실 방지). "
            "수동 정리 후 재시도하거나 `release --force`(stash 보존 강제).",
            file=sys.stderr,
        )
        return 1
    print(f"✓ 슬롯 {args.slot!r} 작업완료 반납(idle 화) — 풀에 재사용 컨테이너로 반환.")
    return 0


def cmd_update(
    forward_args: list[str],
    *,
    pm_update=None,
) -> int:
    """`update [--from ...]` — 엔진 갱신 (pm-update 흡수·T-0054).

    pm_update.main(forward_args) 로 verbatim forward 한다 — pm_update 가 CLI 규격의
    단일 진실이고, 이 서브커맨드는 그 main 으로 위임만 한다(중복 구현 0·rename 비용 0).
    forward_args 는 `update` 뒤의 raw 토큰을 *그대로*(argparse 미가공) 넘긴다 —
    `--from`·`--dry-run` 등 option-like 플래그를 디스패처가 가로채지 않게 `pm_config.main`
    이 (raw[0]=="update" special-case 로) argparse 를 우회해 이 핸들러로 raw 토큰을 넘긴다.
    pm_update.main 은 자체 argparse 로 `--from` 등을 직접 받는다(update 서브커맨드 개념 없음·
    우회 주체는 pm_update 가 아니라 pm_config.main 이다).

    pm_update 주입으로 hermetic 테스트(실 동기화 없이 forward 배선 검증).
    """
    pm_update_mod = pm_update or _load_module("pm_update", "pm_update.py")
    if pm_update_mod is None:
        print(
            "[중단] pm_update.py 엔진을 찾을 수 없다 — 엔진 갱신 불가 "
            f"({TOOLS_DIR / 'pm_update.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    # usage prog 정합 (T-0249·ADR-0043) — pm_update.main 은 prog="pm_update.py" 하드코딩·prog
    # 인자 없음 → 위임 동안만 "pm_update.py"→"pm-config update" 로 치환한다(에이전트가 칠 실
    # 커맨드와 정합·파일명 leak 0). 실 배선 검증 테스트가 mock 을 주입하면 fake.main 은 argparse
    # 를 만들지 않으므로 이 래핑은 무해(패치 후 즉시 원복).
    with _forwarded_prog({"pm_update.py": f"{_FACADE_PROG} update"}):
        return pm_update_mod.main(forward_args)


# ── upstream show/set (T-0145·ADR-0032 D4) ───────────────────────────────────
# upstream 값(URL|경로 self-describing)을 조회/전환한다. set 은 검증(URL→ls-remote 도달성·
# 경로→존재+checkout)을 통과해야 local.conf 에 atomic 재기록(타 키 보존)된다 — fail-closed
# (나쁜 값 silently 기록 금지). URL 안전 계약(scheme allowlist·credential 거부·leading-dash·
# argv-list·timeout)·local.conf set-or-replace 는 pm_import 헬퍼를 재사용한다(중복 구현 0).


def _validate_upstream_reachable(
    value: str,
    *,
    pm_import_mod,
    git_runner: GitRunner | None = None,
) -> tuple[bool, str]:
    """upstream 값 도달성 검증 — URL→`git ls-remote`·경로→존재+git checkout (T-0145·fail-closed).

    pm_import.validate_upstream_value(순수 형태 안전·scheme/credential/dash)를 먼저 통과시킨 뒤
    *도달성*을 본다:
      - URL  → `git ls-remote <url>` rc==0(원격 reachable·argv-list·timeout·GIT_TERMINAL_PROMPT=0).
      - 경로 → 디렉토리 존재 + `git -C <path> rev-parse --is-inside-work-tree` rc==0(git checkout).
    git 호출은 pm_import._real_upstream_git_runner(URL 안전 계약)를 기본으로, 테스트는 git_runner
    주입(라이브 git 0). (ok, reason). 실패는 모두 거부(fail-closed) — silently 기록 금지.
    """
    ok, reason = pm_import_mod.validate_upstream_value(value)
    if not ok:
        return False, reason
    runner = git_runner if git_runner is not None else pm_import_mod._real_upstream_git_runner()
    kind = pm_import_mod.classify_upstream(value)
    if kind == "url":
        # `--` 로 위치인자 종결(value 가 옵션으로 오인되지 않게·leading-dash 는 이미 거부됨이나 방어).
        rc, out = runner(["ls-remote", "--", value])
        if rc != 0:
            return False, (
                f"URL upstream 도달 불가 (`git ls-remote {value}` rc={rc}): {out.strip()[:200]} "
                "— URL/네트워크/접근권한 확인."
            )
        return True, ""
    # 경로 — 존재 + git work tree 검증.
    path = Path(value).expanduser()
    if not path.is_dir():
        return False, f"경로 upstream 이 디렉토리가 아니거나 존재하지 않음: {path}"
    rc, _out = runner(["-C", str(path), "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return False, (
            f"경로 upstream 이 git checkout 이 아님: {path} "
            "(`git rev-parse --is-inside-work-tree` 실패)."
        )
    return True, ""


def cmd_upstream(
    args: argparse.Namespace,
    *,
    pm_import=None,
    git_runner: GitRunner | None = None,
) -> int:
    """`upstream show | set <value>` — upstream 값 조회/전환 (T-0145·ADR-0032 D4).

    - show: local.conf 의 현재 `upstream=` 값을 surface(미등록이면 안내).
    - set <value>: 검증(URL→ls-remote 도달성·경로→존재+checkout·fail-closed) 통과 후 local.conf
      `upstream=` atomic 재기록(타 키 보존). 나쁜 값은 거부(rc 1·기록 안 함). 값 self-describing
      이라 path↔URL 전환이 자동(스킬 freshness 분기가 모양으로 적응).

    local.conf set-or-replace(pm_import._set_conf_keys·타 키·주석 보존)·URL 안전 검증(pm_import.
    validate_upstream_value)을 재사용한다(중복 0). pm_import/git_runner 주입으로 hermetic 테스트
    (라이브 git/실 conf-write 없이 배선·검증 분기 검증).
    """
    pm_import_mod = pm_import or _load_module("pm_import", "pm_import.py")
    if pm_import_mod is None:
        print(
            "[중단] pm_import.py 엔진을 찾을 수 없다 — upstream 조회/전환 불가 "
            f"({TOOLS_DIR / 'pm_import.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    local_conf = REPO / ".project_manager" / "local.conf"
    action = getattr(args, "upstream_action", None)

    if action == "show":
        if not local_conf.is_file():
            print(f"upstream: (local.conf 없음 — {local_conf})")
            return 0
        conf = pm_import_mod._parse_conf_keys(local_conf.read_text(encoding="utf-8"))
        value = conf.get("upstream", "").strip()
        if not value:
            print("upstream: (미등록) — `pm-config upstream set <url|path>` 로 설정하라.")
            return 0
        kind = pm_import_mod.classify_upstream(value)
        print(f"upstream: {value}  ({kind})")
        return 0

    # set <value> — 검증 후 atomic 재기록(타 키 보존). fail-closed.
    value = args.value

    # suggestion(codex): local.conf 부재 확인을 *네트워크(reachability) 전* 으로 옮긴다 —
    #   기록 불가 상태(conf 없음)에서 굳이 ls-remote 로 네트워크를 치지 않게(낭비·지연 회피).
    if not local_conf.is_file():
        print(
            f"[중단] local.conf 없음 ({local_conf}) — 먼저 `pm-config init` 으로 셋업하라.",
            file=sys.stderr,
        )
        return 1

    ok, reason = _validate_upstream_reachable(
        value, pm_import_mod=pm_import_mod, git_runner=git_runner)
    if not ok:
        print(f"[중단] upstream 값 거부 (기록 안 함) — {reason}", file=sys.stderr)
        return 1

    text = local_conf.read_text(encoding="utf-8")
    new_text = pm_import_mod._set_conf_keys(text, {"upstream": value})
    if new_text != text:
        # atomic write — 같은 디렉토리 임시파일에 쓰고 os.replace 로 교체(부분쓰기·crash 중 손상 방지).
        tmp = local_conf.with_suffix(local_conf.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, local_conf)
    kind = pm_import_mod.classify_upstream(value)
    print(f"✓ upstream 설정: {value}  ({kind}) — pm_update 가 --from 생략 시 이 값을 쓴다.")
    return 0


def cmd_init(
    forward_args: list[str],
    *,
    board=None,
) -> int:
    """`init [<board init 인자>]` — clone 당 1회 셋업 (board.py init 흡수·T-0065).

    board.main(["init", *forward_args]) 로 verbatim forward 한다 — board.py init 이
    CLI 규격의 단일 진실이고, 이 서브커맨드는 그 main 으로 위임만 한다(중복 구현 0).
    forward_args 는 `init` 뒤의 raw 토큰을 *그대로*(argparse 미가공) 넘긴다 —
    `--prefix`·`--area`·`--owner`·`--session` 등 option-like 플래그를 디스패처가
    가로채지 않게 `pm_config.main` 이 (raw[0]=="init" special-case 로) argparse 를
    우회해 이 핸들러로 raw 토큰을 넘긴다. board.main 은 자체 argparse 의 `init`
    서브커맨드로 그 플래그를 직접 받는다(우회 주체는 board 가 아니라 pm_config.main 이다).

    init 은 N=1·M=1(solo) ~ N×M 공용 보편 셋업 — pm-config init 은 그걸 single-user
    multi-repo front door 로 노출만 한다(동작 불변·새 동작 0·cmd_update 의 위임 패턴 동형).

    board 주입으로 hermetic 테스트(실 셋업 부작용 없이 forward 배선 검증).
    """
    board_mod = board or _load_module("board", "board.py")
    if board_mod is None:
        print(
            "[중단] board.py 엔진을 찾을 수 없다 — clone 셋업 불가 "
            f"({TOOLS_DIR / 'board.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    # usage prog 정합 (T-0249·ADR-0043) — board.main 은 prog="board.py" 하드코딩·prog 인자
    # 없음 → 위임 동안만 "board.py"→"pm-config" 로 치환한다. init 서브파서 usage 는 부모 prog
    # 에서 "pm-config init" 로 자동 파생돼 에이전트가 칠 실 커맨드와 정합(파일명 leak 0).
    with _forwarded_prog({"board.py": _FACADE_PROG}):
        return board_mod.main(["init", *forward_args])


def cmd_add_harness(
    args: argparse.Namespace,
    *,
    pm_import=None,
    dest_root: Path | None = None,
) -> int:
    """`add-harness <harness> [--dry-run]` — 라이브 인스턴스에 두 번째 harness 어댑터 추가 (ADR-0048·T-0270).

    pm_import.add_harness_cli(dest, harness, dry_run=) 로 verbatim 위임한다 — 복사 스코프(어댑터
    네임스페이스만)·비파괴 백업·토큰 치환은 pm_import 의 add_harness 가(T-0269), 인터페이스 예외의 친화
    번역(rc 1)은 그 main-style 래퍼 add_harness_cli 가 단일 진실로 소유한다(pm_import 가 CLI 규격·
    로직/에러경계 중복 0). harness 는 choices 로 재검증하지 않고 verbatim 으로 넘긴다 — 미지원
    harness('both'/오타)는 add_harness_cli 가 ValueError→rc 1 로 거른다(pm_config 자체 검증 0).

    dest 해소는 기존 pm_config 관례(REPO=스크립트-위치 앵커·cwd 무관·cmd_upstream 과 동형) —
    pm_config.py 가 사는 이 인스턴스 루트가 곧 harness 를 추가할 라이브 인스턴스다. dest_root
    미주입 시 *호출 시점*에 REPO 로 해소한다(테스트 monkeypatch/주입 존중·cmd_repo_add repos_dir
    동형 — 함수 default 로 굳히면 주입이 안 먹는다).

    pm_import/dest_root 주입으로 hermetic 테스트(실 복사 부작용 없이 위임 배선 검증).
    """
    pm_import_mod = pm_import or _load_module("pm_import", "pm_import.py")
    if pm_import_mod is None:
        print(
            "[중단] pm_import.py 엔진을 찾을 수 없다 — harness 추가 불가 "
            f"({TOOLS_DIR / 'pm_import.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    dest = dest_root if dest_root is not None else REPO
    # --from(source_root) 은 optional — 생략 시 pm_import 가 dest local.conf upstream 에서 어댑터
    # 소스를 자동 해소한다(T-0282·imported 인스턴스 갭). 기존 Namespace(테스트/구 호출)에 source 가
    # 없어도 getattr 로 안전하게 None 폴백(하위호환·verbatim forward).
    source_root = getattr(args, "source", None)
    # usage prog 정합 (T-0249·ADR-0043) — init/update forward 와 동형 위임-경계 가드. add_harness_cli
    # 는 자체 argparse 를 만들지 않고 main-style 로 출력·rc 반환하지만, 위임 경계를 init/update 와
    # 균일하게 감싸 pm_import 가 어떤 argparse usage 를 surface 하더라도 파일명("pm_import.py")이 새지
    # 않게 한다(에이전트가 칠 실 커맨드 pm-config add-harness 와 정합·경계 leak 0). rc 는 그대로 전파.
    with _forwarded_prog({"pm_import.py": f"{_FACADE_PROG} add-harness"}):
        return pm_import_mod.add_harness_cli(
            dest, args.harness, dry_run=args.dry_run, source_root=source_root)


# ── 대화형 콘솔 (T-0069) ──────────────────────────────────────────────────────
# 무인자(tty) `pm-config` 의 휴먼 프론트엔드. 상태를 렌더하고 메뉴로 액션을 받고
# 입력마다 바뀐 상태를 재렌더한다. 액션은 모두 *기존 핸들러*(cmd_repo_add·
# cmd_worktree_add·cmd_set_test_cmd)로 위임한다 — 콘솔은 얇은 셸(동작 분기 0·중복 0).
# 커맨드형 CLI 와 공존(같은 핸들러). 엔진/입력은 DI seam → hermetic 테스트.


def _render_repos(board_mod) -> None:
    """areas.md per-repo 레지스트리 행을 surface 한다 (board._parse_areas 재사용).

    board 가 없거나 areas 파서가 없으면(부재·로드실패) 안내만 출력(크래시 0). areas.md
    부재(솔로/미배선)면 빈 안내. 중복 파싱 구현 0 — board 의 헤더-인식 파서를 그대로 쓴다.
    """
    print("## repos (areas.md per-repo 레지스트리):")
    parse_areas = getattr(board_mod, "_parse_areas", None) if board_mod else None
    if parse_areas is None:
        print("  (board.py 엔진/areas 파서 없음 — repo 등록 상태 조회 불가)")
        return
    _header, rows = parse_areas()
    if not rows:
        print("  (등록된 repo 없음 — [r] 로 추가하라)")
        return
    for row in rows:
        name = row.get("repo") or row.get("prefix") or "(?)"
        print(
            f"  - {name} · prefix={row.get('prefix') or '-'} · "
            f"git={row.get('git') or '-'} · test_cmd={row.get('test_cmd') or '(미지정)'} · "
            f"owner={row.get('owner') or '-'} · base={row.get('base') or '(bare HEAD)'}"
        )


def _render_slots(wp) -> None:
    """worktree 풀 리스 장부를 slot·repo·build(test_cmd)·state·session 으로 surface.

    worktree_pool.list_leases() 재사용. wp 가 없으면 안내만(크래시 0). cmd_status 의 풀
    surface 와 같은 데이터 — 콘솔은 build(test_cmd) 칼럼을 강조한다(이 콘솔의 1급 관심사).
    """
    print("## slots (worktree 풀 리스):")
    if wp is None:
        print("  (worktree_pool.py 엔진 없음 — 슬롯 상태 조회 불가)")
        return
    leases = wp.list_leases()
    if not leases:
        print("  (슬롯 없음 — [w] 로 추가하라)")
        return
    for l in leases:
        print(
            f"  - {l.slot} · repo={l.repo} · build={l.test_cmd or '(미지정)'} · "
            f"state={l.state} · session={l.session or '-'}"
        )


def _render_state(board_mod, wp) -> None:
    """콘솔 상태 1회 렌더 — repos(areas) + slots(리스). 액션마다 재호출(재렌더)."""
    print()
    print(f"# pm-config 콘솔 — 세션: {_default_session() or '(비바인딩)'}")
    _render_repos(board_mod)
    _render_slots(wp)


_CONSOLE_MENU = (
    "\n메뉴: [r] repo 추가 · [w] worktree 추가 · [b] 슬롯 빌드명령 설정/변경 · "
    "[u] 엔진 갱신 · [s] 새로고침 · [q] 종료"
)

# 콘솔 프롬프트 중단(EOF/Ctrl-C) sentinel — `_console_input` 이 예외 대신 이걸 돌려준다.
# 호출부(메뉴 루프·각 액션)는 `is _CONSOLE_ABORT` 로 판정해 일관되게 취소/종료한다.
# `None`/`""` 과 구별돼야(그건 정상 빈입력 의미) — 고유 sentinel 객체로 둔다.
_CONSOLE_ABORT = object()


def _console_input(input_fn: Callable[[str], str], prompt: str):
    """콘솔 공유 입력 헬퍼 — `EOFError`/`KeyboardInterrupt` 를 `_CONSOLE_ABORT` 로 흡수.

    **메뉴 입력뿐 아니라 모든 액션 내부 프롬프트**(`[r]` 이름/git/test·`[w]` repo·`[b]` slot/
    빌드명령)가 이 헬퍼를 거친다 — 어느 프롬프트서 Ctrl-C/EOF 가 나도 예외가 전파돼 크래시
    하는 것을 막는다(must-fix 2·codex — "우아 종료/크래시 0" 보장). 반환:
      - 정상 입력 → `str`(strip 안 함 — 호출부가 의미에 맞게 strip; 빈입력 보존).
      - EOF/Ctrl-C → `_CONSOLE_ABORT`(호출부가 액션 취소/메뉴 복귀 또는 루프 종료).
    input_fn 주입으로 hermetic(라이브 input 블록 0).
    """
    try:
        return input_fn(prompt)
    except (EOFError, KeyboardInterrupt):
        return _CONSOLE_ABORT


def _console_repo_add(input_fn, board_mod):
    """`[r]` — repo 이름/git/test/base 를 프롬프트로 받아 cmd_repo_add 위임 (기존 핸들러 재사용).

    각 프롬프트가 `_console_input` 을 거쳐 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 를 받는다 —
    그 경우 이 액션 자체가 `_CONSOLE_ABORT` 를 *반환*해 run_console 이 루프를 우아 종료한다
    (must-fix 2 — 액션 내부 중단도 traceback 0·rc 0). 빈입력은 취소(None 반환·메뉴 복귀).
    """
    name = _console_input(input_fn, "repo 이름 (= prefix): ")
    if name is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    name = name.strip()
    if not name:
        print("  (repo 이름 비어 있음 — 취소)")
        return None
    git = _console_input(input_fn, "git URL: ")
    if git is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    git = git.strip()
    if not git:
        print("  (git URL 비어 있음 — 취소)")
        return None
    test = _console_input(
        input_fn, "test 명령 (빈 입력 = 미지정·나중에 worktree/[b] 에서 설정): "
    )
    if test is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    test = test.strip()
    base = _console_input(input_fn, "base 브랜치 (빈 입력 = 기본 브랜치 사용): ")
    if base is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    base = base.strip()
    # 빈입력 → base=None(= CLI `--base` 생략 = bare HEAD 기본·기존 동작). 비어있지 않으면
    # 그 브랜치명을 cmd_repo_add 로 — 형식/존재 검증은 콘솔이 따로 안 하고 `_resolve_base`
    # 의 `show-ref --verify`(T-0078) 단일 sink 가 거른다(중복 검사 0).
    args = argparse.Namespace(
        name=name, git=git, test=(test or None), owner=None, base=(base or None),
        user=None,  # area_owner 는 cmd_repo_add 가 local.conf user= / git email 로 해소(T-0161).
    )
    cmd_repo_add(args, board=board_mod)
    return None


def _console_worktree_add(input_fn, wp, board_mod=None):
    """`[w]` — repo 를 받아 cmd_worktree_add 위임. 빌드명령은 그 핸들러가 프롬프트(tty 경로).

    repo 프롬프트가 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 반환(루프 우아 종료). 빈입력은 취소.
    board_mod 를 cmd_worktree_add 에 전달해 빌드명령 프롬프트의 표시 기본값(areas 해소)을
    콘솔이 이미 로드한 board 로 재사용한다(중복 로드 0). 빌드명령 프롬프트 내부 중단은
    cmd_worktree_add → _prompt_test_cmd 가 None 으로 흡수(크래시 0·기존 폴백).
    """
    repo = _console_input(input_fn, "슬롯을 만들 repo 이름: ")
    if repo is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    repo = repo.strip()
    if not repo:
        print("  (repo 이름 비어 있음 — 취소)")
        return None
    # 콘솔은 항상 대화형(tty 전제) → cmd_worktree_add 가 빌드명령 프롬프트를 띄우게
    # is_tty=lambda: True 로 강제(콘솔 진입 자체가 tty 보장·main 분기). --test 는 미지정.
    args = argparse.Namespace(repo=repo, test=None)
    cmd_worktree_add(
        args, worktree_pool=wp, board=board_mod, input_fn=input_fn, is_tty=lambda: True
    )
    return None


def _console_set_test_cmd(input_fn, wp):
    """`[b]` — slot·새 빌드명령을 받아 cmd_set_test_cmd 위임 (worktree_pool.set_test_cmd).

    slot·빌드명령 프롬프트가 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 반환(루프 우아 종료). slot 빈입력은
    취소. 빌드명령 빈입력은 None(바인딩 해제·현행).
    """
    slot = _console_input(input_fn, "빌드명령을 바꿀 슬롯 (work/<repo>_<N>): ")
    if slot is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    slot = slot.strip()
    if not slot:
        print("  (슬롯 비어 있음 — 취소)")
        return None
    cmd = _console_input(input_fn, "새 빌드/테스트 명령 (빈 입력 = 바인딩 해제): ")
    if cmd is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    cmd = cmd.strip()
    cmd_set_test_cmd(slot, cmd or None, worktree_pool=wp)
    return None


def _console_update(pm_update=None) -> None:
    """`[u]` — 엔진 갱신을 `cmd_update([])` 로 위임 (= pm_update.main verbatim·T-0061 흡수).

    `[r]`/`[w]`/`[b]` 와 동형의 얇은 래퍼 — 입력 프롬프트 없이(인자 없는 갱신) 기존 핸들러로
    위임만 한다(중복 0). 갱신은 repos/slots 장부를 안 바꿔도(엔진 파일 동기화) 호출부가
    상태를 재렌더한다(무해·일관). pm_update 주입으로 hermetic(실 동기화 없이 배선 검증).
    """
    cmd_update([], pm_update=pm_update)
    return None


def run_console(
    *,
    input_fn: Callable[[str], str] = input,
    board=None,
    worktree_pool=None,
) -> int:
    """대화형 콘솔 루프 — 상태 렌더 + 메뉴 + 액션 + 재렌더 (T-0069·tty 전용).

    무인자 `pm-config`(tty)가 진입한다(비-tty 는 main 이 help 로 분기). 흐름:
      1. 상태 렌더(repos via areas · slots via 리스) — cmd_status/list_leases/areas 파서 재사용.
      2. 메뉴 프롬프트 → 키 1자.
      3. 액션(`[r]`·`[w]`·`[b]`·`[u]`)은 *기존 핸들러*로 위임 → 상태 재렌더.
      4. `[s]` 새로고침(재렌더만)·`[q]` 종료. (`[u]` 엔진 갱신 = cmd_update·T-0071.)

    입력 견고성: 빈 입력/오타 메뉴키 → 재프롬프트(크래시 0)·`EOFError`/`KeyboardInterrupt`
    → 우아 종료(메시지 + rc 0). **메뉴 입력뿐 아니라 모든 액션 내부 프롬프트**(`[r]`·`[w]`·
    `[b]` 의 이름/git/repo/slot 입력)가 공유 `_console_input` 헬퍼를 거친다 — 어느 프롬프트서
    중단해도 액션이 `_CONSOLE_ABORT` 를 반환하고 루프가 우아 종료한다(must-fix 2·codex —
    "우아 종료/크래시 0" 보장·traceback 0·rc 0). 엔진(board·worktree_pool)/입력(input_fn)
    주입으로 hermetic 테스트(실 clone/worktree·라이브 input 블록 0 — 입력 시퀀스 주입 + 핸들러 mock).
    """
    board_mod = board or _load_module("board", "board.py")
    wp = worktree_pool or _load_module("worktree_pool", "worktree_pool.py")

    _render_state(board_mod, wp)
    while True:
        print(_CONSOLE_MENU)
        # 메뉴 선택도 액션 프롬프트와 *같은* 공유 헬퍼로 통일 — 중단(EOF/Ctrl-C) → 우아 종료.
        choice = _console_input(input_fn, "선택: ")
        if choice is _CONSOLE_ABORT:
            print("\n콘솔 종료.")
            return 0
        choice = choice.strip().lower()

        if choice == "q":
            print("콘솔 종료.")
            return 0
        if choice == "s":
            _render_state(board_mod, wp)
            continue
        if choice == "r":
            result = _console_repo_add(input_fn, board_mod)
        elif choice == "w":
            result = _console_worktree_add(input_fn, wp, board_mod)
        elif choice == "b":
            result = _console_set_test_cmd(input_fn, wp)
        elif choice == "u":
            # 엔진 갱신(T-0071) — 입력 프롬프트 없이 cmd_update([]) 위임 후 재렌더.
            result = _console_update()
        else:
            # 빈 입력/오타 메뉴키 → 재프롬프트(크래시 0). 액션 안 함·상태 재렌더 안 함.
            if choice:
                print(f"  (알 수 없는 선택 {choice!r} — r/w/b/u/s/q 중 하나)")
            continue
        # 액션 내부 프롬프트가 중단(EOF/Ctrl-C)됐으면 _CONSOLE_ABORT 를 반환 — 루프 우아 종료
        # (must-fix 2 — 메뉴뿐 아니라 액션 프롬프트 중단도 traceback 0·rc 0).
        if result is _CONSOLE_ABORT:
            print("\n콘솔 종료.")
            return 0
        # 액션 수행 후 바뀐 상태를 재렌더(입력마다 상태 변화 확인·T-0069 핵심).
        _render_state(board_mod, wp)


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """서브커맨드 디스패처 파서 (argparse subparsers·pm_update epilog 단일소스 계승)."""
    parser = argparse.ArgumentParser(
        prog="pm-config",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "각 서브커맨드는 엔진(board.py / worktree_pool.py / pm_update.py)으로 위임하는 "
            "얇은 배선이다. 브랜치 할당은 이 파사드가 아니라 `pm-bootstrap <repo> --branch <B>` "
            "소관(T-0060·idle 슬롯 리스 + checkout). 런타임 worktree alloc/release 자동화는 "
            "bootstrap/handoff 가 하고, 여기 `release` 는 수동 반납/강제(백스톱)만이다."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # repo add <name> [--git <url>] [--test "<cmd>"]  (--test optional·clone 우선·--git 은 신규 repo 필수/기등록 hydrate 는 areas URL·T-0291)
    p_repo = sub.add_parser("repo", help="repo 등록 관리 (add)")
    repo_sub = p_repo.add_subparsers(dest="repo_command", metavar="<repo-command>")
    p_repo_add = repo_sub.add_parser(
        "add", help="패밀리에 repo 등록 + .repos/<name>.git bare clone (ADR-0014)"
    )
    p_repo_add.add_argument("name", help="repo 이름 (= prefix · per-repo ID 네임스페이스)")
    p_repo_add.add_argument("--git", metavar="URL", default=None,
                            help="repo git URL (bare clone 원). 미제공 시 — already-registered "
                                 "repo 는 areas.md `git` 칼럼 URL 로 hydrate(bare mirror clone·"
                                 "multi-user 2번째 사용자·T-0291), 미등록 신규 repo 는 명확 에러"
                                 "(URL 필수·부작용 0).")
    p_repo_add.add_argument("--test", metavar="CMD", default=None,
                            help="per-repo 테스트 명령 (areas.md test_cmd·회귀 게이트가 worktree 에서 실행). "
                                 "미지정 시 areas test_cmd 빈 값 — 해소 체인이 슬롯/local.conf 로 폴백(T-0066). "
                                 "빌드명령은 worktree add 프롬프트·콘솔 [b] 에서도 설정 가능.")
    p_repo_add.add_argument("--owner", metavar="이름", default=None,
                            help="등록 owner = registrant (기본: 현 세션·ADR-0014)")
    p_repo_add.add_argument("--user", metavar="이름", default=None,
                            help="area_owner = 그 area 의 user 소유 (`--mine` 풀 입력·ADR-0033 ③·T-0161). "
                                 "미지정 시 local.conf user= / git config user.email 로 해소(없으면 빈 값).")
    p_repo_add.add_argument("--base", metavar="BRANCH", default=None,
                            help="worktree 슬롯 브랜치가 파생될 base 브랜치 (T-0075·develop 등). "
                                 "미지정 시 clone 된 bare 의 기본 브랜치(원격 default)로 해소·기록. "
                                 "지정 시 존재 검증(없는 base 거부). worktree add 가 이 base 에서 슬롯 브랜치를 판다.")
    p_repo_add.set_defaults(func=cmd_repo_add)

    # worktree add <repo>
    p_wt = sub.add_parser("worktree", help="worktree 슬롯 관리 (add)")
    wt_sub = p_wt.add_subparsers(dest="worktree_command", metavar="<worktree-command>")
    p_wt_add = wt_sub.add_parser(
        "add", help="새 슬롯 생성(<repo>_<N>) + git submodule update --init (ADR-0013)"
    )
    p_wt_add.add_argument("repo", help="슬롯을 만들 repo 이름 (areas.md 등록된 것)")
    p_wt_add.add_argument(
        "--test", metavar="<cmd>", default=None,
        help="이 슬롯에 바인딩할 회귀/빌드명령 (T-0066·같은 repo 슬롯별 빌드변형·HIL config). "
             "미지정 시 repo areas/local.conf 로 해소(현행).",
    )
    p_wt_add.set_defaults(func=cmd_worktree_add)
    # worktree prune-stale — worktree 가 사라진 dangling 장부 엔트리 안전 정리 (T-0295).
    # status reconcile 의 stale/incomplete(worktree 부재) 정리 진입점(조회-전용 reconcile 과 분리·
    # 명시 user-invoked). orphan worktree(git 측)는 `git worktree remove` 로 사용자가.
    p_wt_prune = wt_sub.add_parser(
        "prune-stale",
        help="worktree dir 이 사라진 dangling 장부 엔트리 정리 (stale/incomplete·안전·T-0295)",
    )
    p_wt_prune.set_defaults(func=cmd_worktree_prune_stale)
    # worktree remove <slot> [--force] — 슬롯 통째 제거(원자·user-invoked·T-0333).
    # 리스 확인→git worktree remove→전용 브랜치 정리→장부 엔트리 삭제를 한 번에. prune-stale
    # (worktree 부재 장부만 정리·안전)과 달리 **실 worktree 를 지운다** — 사용자 명시 호출 전제
    # (PM 자율 실행 아님·삭제-위임). 장부 엔트리 제거로 `add` 가 빈 번호 재사용(번호 skip footgun 종결).
    p_wt_remove = wt_sub.add_parser(
        "remove",
        help="슬롯 통째 제거 — worktree remove + 전용 브랜치 정리 + 장부 엔트리 삭제 (원자·사용자 "
             "명시 호출·prune-stale 과 달리 실 worktree 삭제·번호 재사용·T-0333). ⚠ 미머지 전용 "
             "브랜치는 보존되며 같은 번호 base-경로 재생성을 막는다(브랜치 정리 후 재시도).",
    )
    p_wt_remove.add_argument("slot", help="제거할 슬롯 (work/<repo>_<N>·release 와 동일 식별 방식)")
    p_wt_remove.add_argument(
        "--force", action="store_true",
        help="dirty/활성 리스(사용 중) 무시 강제 제거 (dirty 는 stash 보존 시도)",
    )
    p_wt_remove.set_defaults(func=cmd_worktree_remove)

    # status | whoami (같은 핸들러)
    p_status = sub.add_parser("status", help="풀/리스 상태 + 이 세션 repo/슬롯/branch")
    p_status.set_defaults(func=cmd_status)
    p_whoami = sub.add_parser("whoami", help="status 의 별칭 — 이 세션 리스를 머리에 표면")
    p_whoami.set_defaults(func=cmd_status)

    # upstream show | set <value> — upstream 값 조회/전환 (T-0145·ADR-0032 D4)
    p_upstream = sub.add_parser(
        "upstream", help="upstream(URL|경로) 조회/전환 — show | set <value> (T-0145)")
    up_sub = p_upstream.add_subparsers(dest="upstream_action", metavar="<upstream-action>")
    up_sub.add_parser("show", help="현재 upstream 값 surface (local.conf)")
    p_up_set = up_sub.add_parser(
        "set", help="upstream 값 설정 (검증: URL→ls-remote 도달성·경로→존재+checkout·fail-closed)")
    p_up_set.add_argument("value", help="upstream 값 (git URL 또는 로컬 경로·self-describing)")
    p_upstream.set_defaults(func=cmd_upstream)

    # release <slot> [--force]
    p_release = sub.add_parser("release", help="작업완료 반납 / 수동 강제(백스톱)")
    p_release.add_argument("slot", help="반납할 슬롯 (work/<repo>_<N>)")
    p_release.add_argument("--force", action="store_true",
                           help="dirty/leased 무시 강제 idle 화 (dirty 는 stash 보존 시도)")
    p_release.set_defaults(func=cmd_release)

    # add-harness <harness> [--dry-run] — 라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가
    # (ADR-0048·T-0270). pm_import.add_harness_cli 로 verbatim 위임(cmd_add_harness) — harness 는
    # choices 로 재검증하지 않고(pm_import 가 CLI 규격 단일 진실·미지원은 add_harness_cli 가
    # ValueError→rc 1) 그대로 넘긴다. repo/worktree/release 와 동형의 func-dispatch 서브커맨드다.
    p_add_harness = sub.add_parser(
        "add-harness",
        help="라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가 (pm_import.add_harness 위임·ADR-0048)",
    )
    p_add_harness.add_argument(
        "harness",
        help="추가할 harness 어댑터 (claude|opencode·pm_import 가 검증). 어댑터 네임스페이스만 복사(비파괴).",
    )
    p_add_harness.add_argument(
        "--from", dest="source", metavar="SOURCE", default=None,
        help="어댑터 소스 프레임워크 checkout (생략 시 local.conf upstream 에서 자동 해소·"
             "imported 인스턴스 갭·T-0282). URL upstream 이면 로컬 checkout 경로를 명시해야 한다.",
    )
    p_add_harness.add_argument(
        "--dry-run", action="store_true",
        help="적용 없이 복사 plan 만 출력 (파일시스템 미변경).",
    )
    p_add_harness.set_defaults(func=cmd_add_harness)

    # update [--from ...] — pm-update 흡수. 실제 forward 는 main 이 argparse 우회로
    # 처리한다(아래 special-case) — 여기 등록은 `--help` 목록 surface(발견성)용이다.
    # option-like 플래그(--from·--dry-run)를 이 디스패처가 가로채면 안 되므로 forward
    # 토큰을 subparser 로 파싱하지 않는다.
    sub.add_parser(
        "update",
        help="엔진 갱신 (pm-update 흡수·T-0054) — 뒤 인자는 pm_update 로 verbatim forward",
        add_help=False,
    )

    # init [<board init 인자>] — board.py init 흡수. 실제 forward 는 main 이 argparse
    # 우회로 처리한다(아래 special-case) — 여기 등록은 `--help` 목록 surface(발견성)용이다.
    # option-like 플래그(--prefix·--area·--owner·--session)를 이 디스패처가 가로채면
    # 안 되므로 forward 토큰을 subparser 로 파싱하지 않는다(update 와 동형).
    sub.add_parser(
        "init",
        help="clone 당 1회 셋업 (board.py init 흡수·T-0065) — 뒤 인자는 board 로 verbatim forward",
        add_help=False,
    )

    return parser


def _set_console_codepage_utf8() -> None:
    # Windows 한정 — 콘솔 코드페이지를 UTF-8(65001)로 맞춘다. cp949(한국어) 콘솔에서
    # stdout reconfigure(utf-8)만으로는 콘솔이 UTF-8 바이트를 cp949 로 디코드해 한글이
    # mojibake 되므로, 콘솔 입출력 codepage 자체를 65001 로 설정해 정합시킨다 (T-0068).
    # best-effort: 콘솔 핸들 없음·권한·예외 시 조용히 통과(reconfigure 와 동형 try/except).
    # idempotent — 이미 UTF-8 콘솔엔 65001 재설정이 무해. POSIX 는 진입하지 않는다.
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # 먼저 Windows 콘솔 codepage 를 UTF-8 로 맞춘 뒤(T-0068) 스트림을 reconfigure 한다.
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    _set_console_codepage_utf8()
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    raw = list(sys.argv[1:] if argv is None else argv)

    # 무인자 분기 (T-0069): 인자 0 일 때
    #   - tty(stdin·stdout 둘 다)  → 대화형 콘솔(run_console). multi-PM이 복잡(다수 repo·슬롯·
    #     빌드변형)해 CLI 플래그를 외우기 어려운 걸 상태 가시화 + 메뉴로 해소.
    #   - 비-tty(파이프/CI)         → 현행 help(아래 argparse 경로). input() 으로 안 멈춘다
    #     (pm_import 비-tty 폴백 패턴 동류·CI 안전). 서브커맨드를 주면 기존 CLI 경로 그대로
    #     (이 분기 미진입) — 커맨드형 동작 0 변경.
    if not raw and _stdin_is_tty():
        return run_console()

    # `update` 는 argparse 를 우회해 뒤 인자를 pm_update 로 *verbatim* forward 한다 —
    # `--from`·`--dry-run` 같은 option-like 플래그를 디스패처가 가로채지 않게 한다
    # (pm_update 가 CLI 규격의 단일 진실·중복 파싱 0). `update` 가 첫 토큰일 때만.
    if raw and raw[0] == "update":
        return cmd_update(raw[1:])

    # `init` 도 동형 — 뒤 인자를 board.py init 으로 *verbatim* forward 한다.
    # `--prefix`·`--area`·`--owner`·`--session` 같은 option-like 플래그를 디스패처가
    # 가로채지 않게 한다(board.py init 이 CLI 규격의 단일 진실·중복 파싱 0). `init` 이
    # 첫 토큰일 때만.
    if raw and raw[0] == "init":
        return cmd_init(raw[1:])

    parser = build_parser()
    args = parser.parse_args(argv)

    # 서브커맨드 미지정 — 등록 안내 surface(--help 단일 소스).
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 1

    # `repo`/`worktree` 만 주고 하위 동작(add)을 안 줬으면 그 그룹 도움말 surface.
    if args.command == "repo" and getattr(args, "repo_command", None) is None:
        parser.parse_args(["repo", "--help"])
        return 1
    if args.command == "worktree" and getattr(args, "worktree_command", None) is None:
        parser.parse_args(["worktree", "--help"])
        return 1
    # `upstream` 만 주고 하위 동작(show/set)을 안 줬으면 그 그룹 도움말 surface(repo/worktree 동형).
    if args.command == "upstream" and getattr(args, "upstream_action", None) is None:
        parser.parse_args(["upstream", "--help"])
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
