#!/usr/bin/env python3
"""PM 핸드오프 7단계 자동화 헬퍼 — PM 세션 종료 시 기계 측정·편집 부분을 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/pm_handoff.py --task <이름> --user-ack <이름>

slot/solo 호환 경로:
    venv/bin/python .project_manager/tools/pm_handoff.py \\
      --session-seq <N차> \\
      [--repo <name> [--slot <N>]] \\
      --wave-summary "<wave 1~3 한 줄 요약>" \\
      --user-ack <repo_N|solo> \\
      [--dry-run] [--no-pytest] [--ack-dirty "<사유>"]

    --dry-run 미리보기는 쓰기 0이라 --user-ack 없이 허용한다. 실제 핸드오프는 대상값에
    결속된 사용자 명시 승인 토큰이 필요하다.

동작 순서 (하나라도 실패하면 이후 단계 중단):
  0. dirty-tree 게이트 — 실행 앵커 트리(PM 홈 + 활성 worktree 전수)에 미커밋 잔여가 있으면
     **어떤 mutation 보다 먼저·회귀보다도 먼저** 중단(판정은 git 조회 몇 번). override =
     `--ack-dirty "<사유>"`(사유는 entry 에 박제). `--auto-trigger`는 사용자 명시 핸드오프
     호출부의 호환 신호일 뿐 독자 실행 권한이 아니며, ack 계약을 우회하지 않는다.
  1. 회귀 측정 — pytest tests/ -q. red 면 즉시 중단·핸드오프 불가.
  2. log/current.md handoff entry skeleton append — 세션 중 박제 entry 자동 목록+메타학습·pending intent+회귀/incident.
  3. pm_state.md 세션 식별 표 sliding window 정리 — 신규 entry 추가 + 가장 오래된 entry 제거.
  4. pm_state.md 길이 검증 — wc -l 기준 700 라인 초과 시 warning.
  5. 인계 프롬프트 stdout 출력 — pm_playbook.md §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)"
     의 트리거(역할 framing + /pm-bootstrap)를 채워 stdout. 인계 본문은 log entry 가 이월 —
     부트스트랩이 자동 dump 하므로 프롬프트에 손-채움 안 함.
  6. git status dump — git status -s 출력 + 변경 파일 카운트 + 미push commit 수(ahead) 경고.
  6b. 미마감 raw sweep — 위임/추가 리뷰 raw 장부의 미마감 레코드 표면화(비차단 경고).
  7. 잔여 PM 수동 작업 출력 — checklist(커밋 + push 단계 포함).

결정:
  - subprocess DI: pytest/git subprocess 는 주입 가능한 함수로 감싼다.
  - fail-soft 가 아니다 — 명시적 실패 (비-0 종료 + 명확 메시지).
  - 편집은 정규식 앵커 치환·멱등 — ticket_finish.py 와 동일.
  - LLM 미호출 — stdlib 만.
  - 인계 프롬프트는 stdout 만 — 파일 저장 안 함.
  - pm_state.md 슬라이딩 윈도우 = 3 차 (프로젝트별 조정 — SLIDING_WINDOW_SIZE).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import difflib
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterator

_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise


REPO = Path(__file__).resolve().parents[2]
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
PM_PLAYBOOK_FILE = REPO / ".project_manager" / "wiki" / "pm_playbook.md"  # 정적 — 인계 프롬프트 템플릿 추출용
PM_STATE_FILE = REPO / ".project_manager" / "wiki" / "pm_state.md"       # legacy 솔로 단일 경로 (별칭·아래 _legacy_pm_state_file 가 호출시점 REPO 추종·폴백 원천)
TICKETS_DIR = REPO / ".project_manager" / "wiki" / "tickets"             # board 현황 카운트 legacy 별칭 (아래 _tickets_dir 가 추종)
TOOLS_DIR = REPO / ".project_manager" / "tools"                          # worktree_pool 동적 로드 앵커 (multi-PM 모드)
# 회귀 cwd 자동해소— board.py·pm_bootstrap.py 와 *같은 위치*. _regression_cwd 가
# pm_bootstrap._auto_slot 에 명시 인자로 넘겨 단일 self-host 슬롯을 해소한다. worktree_pool 은
# import 하지 않는다(touches 격리·데이터 결합만) — pm_bootstrap 을 동적로드해 그 판정을 재사용.
AREAS_FILE = REPO / ".project_manager" / "areas.md"                       # legacy 별칭 (아래 _areas_file 가 추종)
LEASES_FILE = REPO / ".project_manager" / ".local" / "worktree-leases.json"

# ── identity_args sibling 로드 ──────────────
# `--repo`/`--slot` 파싱 + 리스 해소를 공용 모듈 identity_args 에서 가져온다. 스크립트-위치 앵커
# (`Path(__file__).resolve().parent`=tools/)에서 `spec_from_file_location` 으로 동적 로드해
# sys.path 를 오염시키지 않는다 (board.py `_load_identity_args`·아래 worktree_pool/pm_bootstrap
# 로더와 동형). 스크립트 직접 실행(sys.path[0]=tools/)이든 테스트 로드(spec_from_file_location·
# 패키지 아님이라 sys.path 미충전)든 어느 쪽이든 `Path(__file__).resolve().parent` 가 정확히
# tools/ 라 동일하게 동작한다 (도구 zero-import 관성은 유지하되 이 leaf util 은
# 예외적으로 sibling 로드).

# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.7.4"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다 (fail-loud·skew→명시 에러).

    불일치/부재(구형 형제는 리터럴 부재=None)면 사본 skew → 명시 에러(어느 파일이 어떤 rev 인지
    지목 + pm-update 안내). self-contained(engine_rev.py 런타임 의존 0)라 부분복사도 정확 검출한다.
    """
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True  # fail-soft 로더가 재-raise 식별
        raise err


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지.

    fail-soft sibling 로더의 `except Exception` 은 로드 실패/부재만 None 으로 흡수하고, 이
    판정이 True 인 예외(중첩 로드에서 검출된 형제 skew)는 재-raise 해 fail-loud 를 보존한다
    (예: 신 pm_handoff→신 worktree_pool→구 형제 검출이 조용히 None 강등되지 않게)."""
    return getattr(exc, "_engine_rev_skew", False)


def _require_engine_sibling(path: Path, filename: str) -> None:
    """load-bearing 형제 모듈의 **부재**를 stale 사본과 같은 진단으로 번역한다 (fail-loud).

    부재는 raw `FileNotFoundError` 로 터져 복구 방법(pm-update 재동기)을 알려주지 않는다 —
    원인이 부분/수동 복사라는 점은 stale 사본과 같으므로 같은 marked skew 로 표출한다
    (board.py `_require_engine_sibling` 동형·self-contained 복제). *옵션* 형제
    (`_load_worktree_pool` 등 fail-soft 대상)에는 쓰지 않는다 — 부재가 정상 경로인 곳이다."""
    if path.exists():
        return
    err = RuntimeError(
        f"엔진 사본 불완전 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 형제 "
        f"{filename} 을(를) 찾지 못했다: {path} (부분/수동 복사). `pm-update`(또는 "
        "pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
    )
    err._engine_rev_skew = True
    raise err


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1


def _load_identity_args():
    """공용 정체성 인자 모듈(identity_args.py)을 같은 tools/ 에서 경로 로드한다 (board.py
    `_load_identity_args`·아래 worktree_pool/pm_bootstrap 로더 동형·sys.path 무오염).

    identity_args 는 `--repo/--slot` 정체성 파싱에 load-bearing 이라(main ingress 가 그 결과로
    액터/리스를 해소) 로드 실패는 엔진 손상이다 — lint 계열의 fail-soft 로 흡수하지 않고 예외를
    그대로 낸다(fail-loud·board.py 동일)."""
    ia_path = Path(__file__).resolve().parent / "identity_args.py"
    return _load_module_from_path(
        ia_path, "identity_args.py", verifier=_verify_engine_rev,
    )


def _load_file_lock():
    """공용 배타 파일락 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다.

    `_load_identity_args` 동형 — 경로-앵커 로더다. **import 시점** 바인딩으로 둔다(아래
    `file_lock = ...`·board.py 동형): 락을 잡을 때마다 형제를 로드하면 pm_handoff 를 fail-soft
    로 소비하는 호출층이 사본 skew 를 조용히 삼키는 경로가 대시보드 write 의 호출 그래프만큼
    늘어난다 — import 경계 단일 fail-loud 로 그 확산을 막는다.
    """
    lock_path = Path(__file__).resolve().parent / "file_lock.py"
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev,
    )


try:
    identity_args = _load_identity_args()
    file_lock = _load_file_lock()
except Exception as exc:  # noqa: BLE001 — 직접 CLI는 import 단계 skew도 복구 안내로 번역.
    if _is_engine_rev_skew(exc):
        if __name__ == "__main__":
            print(
                f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
                "동기화한 뒤 다시 실행하세요.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        raise
    raise
TASK_PM_STATE_EMPTY_MARKER = identity_args.TASK_PM_STATE_EMPTY_MARKER


# ── board root 추종 ───────────────────────
# board(tickets+areas)는 `.project_manager/board/`(submodule)로 분리될 수 있다.
# 그러면 회귀 cwd 자동해소(_auto_slot 의 areas 입력)가 wiki/ legacy 위치를 보면 *stale*(미해소)
# 이다. pm_handoff 는 board.py 를 import 하지 않으므로(touches 격리), board.py 의 graceful 탐지
# 로직을 *동형*으로 최소 복제한다 — board/tickets 가 실 디렉토리면 board/ 루트, 아니면 wiki/(legacy).
# 솔로/미분리/미마이그 adopter 에선 board/tickets 부재 → 현 위치 100% 폴백(회귀 0).
# 상수 TICKETS_DIR·AREAS_FILE 는 hermetic 테스트의 monkeypatch seam·legacy 기본값으로 유지.

def _board_root() -> Path:
    """board(tickets+areas) 루트 — board/ 분리 시 `<REPO>/.project_manager/board`, 아니면
    legacy `<REPO>/.project_manager/wiki` (board.py `board_root` 동형·import 없이 복제)."""
    base = REPO / ".project_manager"
    if (base / "board" / "tickets").is_dir():
        return base / "board"
    return base / "wiki"


def _tickets_dir() -> Path:
    """ticket 디렉토리 — _board_root()/tickets (board/ 분리 추종·legacy=wiki/tickets)."""
    return _board_root() / "tickets"


def _areas_file() -> Path:
    """areas 레지스트리 경로 (board.py `areas_file` 동형·조건분기).

    areas.md 는 legacy 에서 `.project_manager/areas.md`(wiki *밖*)에 산다. board/ 분리 시엔
    board submodule *안*(board/areas.md)으로 옮겨진다 → board/tickets 존재로 가른다.
    """
    if (REPO / ".project_manager" / "board" / "tickets").is_dir():
        return _board_root() / "areas.md"
    return REPO / ".project_manager" / "areas.md"


# ── worktree_pool import seam (multi-PM 모드) ───────────────────────────
# multi-PM 인자(--slot)를 받았을 때만 lease 라이프사이클(release)에 진입한다. 솔로
# 무인자 경로는 이 모듈을 전혀 쓰지 않으므로 import 실패가 무해(fail-soft) — 단
# --done --slot 을 줬는데 worktree_pool 이 없으면 **명시 에러**(침묵 무력화 금지).
def _load_worktree_pool():
    """worktree_pool 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    pm_bootstrap._load_worktree_pool 과 동형 — REPO/tools 스크립트-위치 앵커.
    솔로(multi-PM 미사용·slot 미지정)에선 호출 안 되거나 None 이어도 무해.
    """
    wp_path = TOOLS_DIR / "worktree_pool.py"
    if not wp_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            wp_path, "worktree_pool.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # 중첩 로드에서 검출된 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    # 로드 성공 후 rev skew 는 fail-loud(try 밖이라 fail-soft 로 삼켜지지 않는다).
    # 부재(파일 없음)는 위에서 이미 None 폴백 — 여기선 "present-but-skewed" 만 표출.
    return mod


# ── pm_bootstrap import seam (회귀 cwd 자동해소) ───────────────────────────
# 회귀를 활성 worktree 슬롯에서 돌리려면 단일 self-host 슬롯 판정이 필요하다 —
# pm_bootstrap._auto_slot 이 그 로직(count-based 단일 self-host)을 이미 보유하므로
# 복붙하지 않고 동적 로드해 재사용한다(DRY). _load_worktree_pool·
# pm_bootstrap._load_board 와 동형 — `spec_from_file_location`(스크립트-위치 앵커)·fail-soft.
def _load_pm_bootstrap():
    """pm_bootstrap 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    _load_worktree_pool 과 동형 — REPO/tools 스크립트-위치 앵커. 회귀 cwd 해소에서
    `_auto_slot` 재사용용. 부재/실패는 None 이고 호출부가 `str(REPO)` 로 폴백하므로 무해.
    """
    bp_path = TOOLS_DIR / "pm_bootstrap.py"
    if not bp_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            bp_path, "pm_bootstrap.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # pm_bootstrap(및 그 중첩 형제) skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _load_board():
    """board 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    _load_worktree_pool 과 동형 — REPO/tools 스크립트-위치 앵커. `--task` 이름 검증에서 예약패턴
    (`<repo>_<N>`) 거부용 `registered_repos()` 와 pytest 요약행 파서 seam
    (`_pytest_summary_seam`)이 공유한다(부재면 None → 예약패턴 검증은 traversal·구문 검증만·
    pm_config.cmd_alloc 동형·board.py 직접 import 는 안 함·touches 격리).
    """
    b_path = TOOLS_DIR / "board.py"
    if not b_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            b_path, "board.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 예약패턴 검증만 완화(traversal 유지).
        if _is_engine_rev_skew(exc):
            raise  # board 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _load_pm_relay():
    """pm_relay 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    _load_worktree_pool 과 동형 — REPO/tools 스크립트-위치 앵커. 미마감 raw sweep([6b/7])이
    raw 장부 조회 primitive(`unfinished_raw_records`)를 재사용하려고만 쓴다 — 비차단 surface 라
    부재/실패는 sweep 만 건너뛴다(핸드오프 불변).
    """
    relay_path = TOOLS_DIR / "pm_relay.py"
    if not relay_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            relay_path, "pm_relay.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 비차단 sweep 만 끈다.
        if _is_engine_rev_skew(exc):
            raise  # pm_relay 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _load_pm_delegate():
    """pm_delegate 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    미마감 raw sweep([6b/7])이 **사본 장부 병기**에만 쓴다 — "이 조회가 안 보는 장부가 어디
    있는가"는 위임 조회면(`pm_delegate.py raw`)이 이미 판정하므로(`_peer_engine_ledgers`) 그
    규칙을 재사용한다(같은 판정이 두 곳이면 반드시 갈라진다). 비차단 surface 라 부재/실패는
    병기만 건너뛴다(핸드오프 불변).
    """
    delegate_path = TOOLS_DIR / "pm_delegate.py"
    if not delegate_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            delegate_path, "pm_delegate.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 병기만 끈다.
        if _is_engine_rev_skew(exc):
            raise  # pm_delegate 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


# ── 미마감 raw sweep ([6b/7] · 비차단 경고 — T-0596) ──────────────────────────
#
# 위임/추가 리뷰는 외부 프로세스 실행 **전**에 raw 장부에 미마감 레코드를 예약하고 종료 시 같은
# 레코드를 마감한다 — 그래서 미마감으로 남은 레코드는 "죽은 실행"의 흔적이다. 세션이 그걸 안 보면
# 조용히 누적하고(실측 17건), 나중에 장부는 조회해도 의미를 못 준다. 핸드오프가 세션 끝에서
# 건수 + 최근 몇 건을 표면화한다. **차단하지 않는다** — 잔존 자체는 정상 종료 실패의 결과이지
# 핸드오프의 전제조건이 아니다(차단 상향은 운용 후 판단).
UNFINISHED_RAW_DISPLAY_LIMIT = 5


def _ahead_commit_count(
    runner: Callable[[list[str]], tuple[int, str]],
) -> tuple[int, str] | None:
    """PM 홈의 미push commit 수 — (count, baseline ref). 해소 불가면 None (fail-soft).

    `git rev-list --count <baseline>..HEAD` 를 baseline 후보(`_PENDING_PUSH_BASELINE_REFS` —
    출하 변경 분류와 같은 순서: @{upstream} 우선·origin/main 폴백) 순으로 시도해 첫 성공을 쓴다.
    detached/upstream 미설정/원격 부재/비수치 출력은 전부 None — 판정을 지어내지 않는다.
    """
    for ref in _PENDING_PUSH_BASELINE_REFS:
        try:
            rc, out = runner(["rev-list", "--count", f"{ref}..HEAD"])
        except Exception:  # noqa: BLE001 — fail-soft: git 예외는 rc 실패와 같이 다음 후보로.
            continue       # 첫 후보의 예외로 폴백 후보를 건너뛰면 upstream 미설정이 곧 미해소가 된다.
        if rc != 0:
            continue
        text = out.strip().splitlines()[0].strip() if out.strip() else ""
        if text.isdigit():
            return int(text), ref
    return None


def validate_task_name_engine(task: str) -> None:
    """공유 validator(`worktree_pool._validate_task_name`)로 task 명을 fail-loud 검증한다.

    **엔진층 단일 choke** — main() CLI 뿐 아니라 `PmHandoff.run()`·`build_handoff_prompt_output()`
    직접 호출도 task 를 소비(pm_state 경로·log 태그·dashboard 섹션·트리거 삽입)하기 전 이 함수를
    통과해야 한다. per-surface 이스케이프 대신 단일 validator 로 도메인을 협소화하는
    (직접 소비도 우회 불가)를 handoff 진입점 전체에 적용한다 — `"my task"`(공백)·`../evil`(traversal)·
    `<repo>_<N>`(슬롯 예약패턴) 류가 CLI 를 우회해 트리거 파싱 파손·경로 이탈을 내는 갭을 닫는다.

    registered_repos 는 board 에서 fail-soft 해소(부재/파싱 실패면 None → 예약패턴만 완화하고
    traversal/whitespace/괄호/빈이름 구문 검증은 유지). worktree_pool 로드 실패는 검증 불가라
    RuntimeError(fail-loud) — silent skip 이 갭을 재오픈하지 않게 한다.

    raise: `worktree_pool.InvalidTaskName`(부적합 task 명) · `RuntimeError`(worktree_pool 엔진 부재).
    """
    wp = _load_worktree_pool()
    if wp is None:
        raise RuntimeError(
            "worktree_pool 엔진을 찾을 수 없다 — --task 이름 검증 불가 "
            f"({TOOLS_DIR / 'worktree_pool.py'} 부재/로드 실패·multi-PM 셋업/엔진 전파 확인)."
        )
    board_mod = _load_board()
    registered = None
    _reg_fn = getattr(board_mod, "registered_repos", None) if board_mod else None
    if _reg_fn is not None:
        try:
            registered = _reg_fn()
        except Exception:  # noqa: BLE001 — areas 파싱 실패는 None(예약패턴만 완화·traversal 유지).
            registered = None
    wp._validate_task_name(task, registered)


def _regression_cwd(
    worktree_slot: str | None = None,
    areas_file: Path | None = None,
    leases_file: Path = LEASES_FILE,
    *,
    repo_root: Path | None = None,
) -> str:
    """회귀를 실행할 작업 디렉토리를 해소한다 (분리된 PM 홈+worktree 모델).

    분리된 PM 홈엔 `tests/` 가 없으므로 회귀는 활성 repo 의 worktree cwd 에서
    돌아야 한다. 이 함수가 그 경로를 해소한다.

    해소 순서:
      - `worktree_slot`(명시 `--repo`/`--slot`) 가 있으면 `repo_root / worktree_slot`
        (단 그 디렉토리가 실제로 없으면 stale 슬롯 → `repo_root` 로 폴백·경고 1줄),
      - 없으면 bootstrap `_auto_slot` 으로 단일 self-host 슬롯을 자동해소(`work/<repo>_<N>` ·
        **같은 존재 검사**를 탄다 — 디렉토리가 없으면 명시 분기와 동형으로 `repo_root` 폴백·경고),
      - 그것도 없으면(솔로/모호/부재) **현 `repo_root` 기본** (fail-soft 폴백·솔로 무변경).

    판정 로직은 pm_bootstrap `_auto_slot` 재사용 —
    복붙하지 않고 동적 로드한다(DRY). areas/leases/repo_root 는 명시 인자로 노출해 hermetic
    테스트 가능. `areas_file` 미지정이면 `_areas_file()`(board_root 추종)로 해소한다 —
    legacy 위치를 보면 _auto_slot 이
    등록 repo 를 0개로 세 self-host 슬롯을 미해소한다. `repo_root` 미지정이면 모듈 `REPO`.

    슬롯이 리스 장부 조인은 통과했더라도(명시든 자동해소든)
    실제 worktree 디렉토리가 물리적으로 없을 수 있다(장부-파일시스템 out-of-sync·저빈도 엣지) —
    그대로 `subprocess.run(cwd=...)` 에 넘기면 `FileNotFoundError` 로 크래시한다. 여기서 존재를
    확인해 없으면 **REPO 로 폴백**(비차단·경고 1줄)한다 — (장부 자체가 모순 — 하드 fail-loud)와
    boundary 를 정합시킨다: 장부-불일치는 loud reject, 장부는 맞는데 디스크만 stale 이면 soft 폴백.
    **두 분기가 같은 검사를 탄다** — 같은 상태(장부에는 있고 디스크에는 없음)를 해소 경로에 따라
    다르게 처리하면 자동해소 실행만 크래시한다.
    """
    if repo_root is None:
        repo_root = REPO
    if areas_file is None:
        areas_file = _areas_file()
    if worktree_slot:
        candidate = repo_root / worktree_slot
        if candidate.is_dir():
            return str(candidate)
        print(
            f"  ⚠ 명시 슬롯 '{worktree_slot}' 의 worktree 디렉토리가 없다 ({candidate}) — "
            "REPO 로 폴백한다 (stale 슬롯).",
            file=sys.stderr,
        )
        return str(repo_root)
    bp = _load_pm_bootstrap()
    if bp is not None:
        try:
            auto = bp._auto_slot(areas_file, leases_file)
        except Exception as exc:  # noqa: BLE001 — fail-soft: 판정 실패는 REPO 폴백.
            if _is_engine_rev_skew(exc):
                raise
            auto = None
        if auto:
            repo, n = auto
            # 자동해소도 **명시 슬롯과 같은 존재 검사**를 탄다. 판정은 장부(areas·leases)만 보므로
            #   worktree 디렉토리가 물리적으로 없을 수 있고(장부-파일시스템 out-of-sync), 그대로
            #   `subprocess.run(cwd=...)` 에 넘기면 FileNotFoundError 로 크래시한다 — 같은 상태를
            #   분기마다 다르게 처리할 이유가 없다(폴백도 경고도 동형).
            candidate = repo_root / f"work/{repo}_{n}"
            if candidate.is_dir():
                return str(candidate)
            print(
                f"  ⚠ 자동해소 슬롯 '{repo}_{n}' 의 worktree 디렉토리가 없다 ({candidate}) — "
                "REPO 로 폴백한다 (stale 슬롯).",
                file=sys.stderr,
            )
    return str(repo_root)


# ── per-slot pm_state 경로 해소 (multi-PM 연속성) ─────────
# pm_state 는 *슬롯별*이다 — 여러 PM 슬롯이 한 clone 의 공유 보드 위에서 각자 핸드오프
# 연속성(세션 식별 sliding window·진행 중 결정)을 유지해야 하므로, 슬롯마다 별도
# pm_state 를 둔다. 경로 = `.project_manager/.local/slots/<slot>/pm_state.md`
# (gitignored·per-slot). slot 키 = lease 장부 슬롯과 동형(`<repo>_<N>`) — `_regression_cwd`·
# `_auto_slot`의 단일 self-host 바인딩과 같은 식별자를 재사용한다.
#
# graceful 마이그레이션 / 솔로 하위호환:
#   - 슬롯이 해소되고(`<repo>_<N>`) slot 경로가 아직 없는데 legacy `wiki/pm_state.md` 가
#     있으면, 첫 접근 시 slot 경로로 **이동**(per-slot 화 일회성 마이그레이션).
#   - 슬롯 미해소(솔로 단일 host·모호·미분리·`_auto_slot` None) → legacy `wiki/pm_state.md`
#     를 그대로 read/write(현행 100% 보존·솔로 무변경).
#   - 슬롯 해소돼도 slot 경로 부재 + legacy 부재면(드문 엣지) legacy 경로로 fail-soft 폴백.

# 동적 별칭(_legacy_pm_state_file)으로 PM_STATE_FILE 을 추종 — 테스트가 monkeypatch 로
# REPO 를 바꿔도 import 시점에 굳은 PM_STATE_FILE 대신 *호출 시점* legacy 경로를 본다.
def _legacy_pm_state_file() -> Path:
    """legacy 단일 pm_state 경로 (`wiki/pm_state.md`·clone당 1개·솔로 폴백·마이그레이션 원천)."""
    return REPO / ".project_manager" / "wiki" / "pm_state.md"


def _slots_root() -> Path:
    """per-slot 상태 디렉토리 루트 (`.project_manager/.local/slots/`·gitignored)."""
    return REPO / ".project_manager" / ".local" / "slots"


def _task_pm_state_file(task: str) -> Path:
    """task 서술 공간 pm_state 경로 (`.local/tasks/<task>/pm_state.md`).

    세션 종료(핸드오프)의 연속성 앵커가 slot→task 로 이동한 task 모드에서 pm_state 를 여기
    기록한다. worktree_pool.task_dir(name)/pm_state.md 의 미러 — 모듈 격리라 cross-import
    대신 REPO 파생으로 동형화한다(`_slots_root` 와 같은 관례·monkeypatch(REPO) 추종·hermetic)."""
    return REPO / ".project_manager" / ".local" / "tasks" / task / "pm_state.md"


# canonical 슬롯 키(`<repo>_<N>`)에서 trailing 숫자(`<N>`)를 뽑는다 — divergent bare dir
# (`slots/<N>`) 존재 여부를 판단하는 backfill 마이그레이션에서 재사용.
_SLOT_TRAILING_NUM_RE = re.compile(r"^.+_(\d+)$")


def _backfill_divergent_slot_dir(slot: str) -> None:
    """divergent bare 슬롯 dir(`slots/<N>`)을 canonical `slots/<repo>_<N>` 로 1회 이동한다.

    write-side 가 과거 bare 토큰(`--slot 4`)을 verbatim 슬롯 키로 써 `slots/<N>` 을 만든
    잔재가 있으면, 이번 진입에서 canonical `slots/<repo>_<N>`(`slot`)으로 **backfill**
    한다(prefer-data-migration-over-fallback — 런타임 폴백 누적 대신 원천 정합). guarded:

      - `slot` 이 `<repo>_<N>` 형식이 아니면(trailing 숫자 없음) no-op.
      - bare dir(`slots/<N>`)이 없으면 no-op(정상 케이스·이미 정합).
      - canonical dir(`slots/<repo>_<N>`)이 **이미 있으면** 안전하게 스킵(덮어쓰지 않음 — 두
        dir 이 동시에 있는 드문 엣지는 canonical 이 우선·bare 는 그대로 둬 데이터 유실 방지).
      - bare dir 안에 `pm_state.md` 가 있으면 canonical dir 로 이동(부모 생성)하고, bare dir 이
        비면 정리한다(빈 dir 이 아니면 남겨둔다 — 예상 밖 파일 보존).

    예외/실패는 전부 fail-soft(무해) — 마이그레이션은 편의 backfill 이지 강제 아니다.
    """
    m = _SLOT_TRAILING_NUM_RE.match(slot)
    if not m:
        return
    bare = m.group(1)
    if bare == slot:
        return  # slot 이 그 자체로 bare 숫자(레포 접두어 없음) — 대상 아님.
    slots_root = _slots_root()
    bare_dir = slots_root / bare
    canonical_dir = slots_root / slot
    try:
        if not bare_dir.is_dir() or canonical_dir.exists():
            return
        bare_state = bare_dir / "pm_state.md"
        if not bare_state.exists():
            return
        canonical_dir.mkdir(parents=True, exist_ok=True)
        bare_state.replace(canonical_dir / "pm_state.md")
        try:
            next(bare_dir.iterdir())
        except StopIteration:
            bare_dir.rmdir()  # 비었으면 정리(예상 밖 잔여 파일이 있으면 남겨둠).
    except OSError:  # noqa: BLE001 — fail-soft: 마이그레이션 실패는 무해(다음 접근 시 재시도).
        return


def _resolve_state_slot(
    worktree_slot: str | None = None,
    areas_file: Path | None = None,
    leases_file: Path | None = None,
) -> str | None:
    """pm_state slot 키(`<repo>_<N>`)를 해소한다 — 명시 슬롯 우선·없으면 guarded default-1 자동.

    해소 순서:
      - `worktree_slot`(명시 `--repo`/`--slot`·`work/<repo>_<N>` 또는 `<repo>_<N>`)
        가 있으면 leading `work/` 를 벗긴 `<repo>_<N>` 을 슬롯 키로.
      - 없으면 bootstrap `_resolve_session_slot`(guarded default-1) 으로 자동해소 →
        `<repo>_<N>`. `{1,2}`→slot 1·`{3}`-sole→slot 3·단일 self-host→그것.
      - 그것도 없으면(solo/부재) **None** — 호출부가 legacy `wiki/pm_state.md` 로 폴백.

    `--repo`/`--slot`은 타입이 분리돼 있어(`--slot`
    은 `int`·`--repo` 필수 — `identity_args.parse_identity`) bare 슬롯 번호 자체가 발생하지 않는다
    — `main()` ingress(`_resolve_explicit_identity_slot`)가 이미 repo-qualified(`work/<repo>_<N>`)
    로 조립해 넘기므로, 이 함수는 그 형식만 받는다는 전제로 정규화하지 않는다(입구 단일화가 근본).

    **continuity(세션-window read/write) 정합**: `_auto_slot`
    (exactly-1)은 `{1,2}` 를 None 으로 떨궈 *없는 legacy* 로 새서 slot 1 연속성을 끊었다 —
    run() 가드는 `_resolve_session_slot`(default-1)로 "slot 1" 통과시키는데 write 는 legacy 로
    가는 split. continuity 해소도 같은 `_resolve_session_slot`(default-1)을 경유해 정합시킨다.
    `SlotResolutionError`(진짜 모호·`{2,3}`·repo≥2)는 **catch → None**(display/preview fail-soft
    보존) — write 경로는 run() 가드가 이미 fail-loud 로 막아 도달 안 한다(방어적).

    `_resolve_session_slot` 은 동적 로드해 재사용(DRY·복붙 금지). `areas_file`/`leases_file`
    미지정이면 *호출 시점* `_areas_file()`(board_root 추종)·REPO 기준으로 해소한다 — 모듈 default
    상수가 import 시점에 굳지 않게 None 으로 받아 monkeypatch 된 REPO 를 추종(hermetic).
    """
    if worktree_slot:
        # `work/<repo>_<N>` 또는 `<repo>_<N>` 둘 다 받아 슬롯 키(`<repo>_<N>`)로 정규화.
        return worktree_slot[len("work/"):] if worktree_slot.startswith("work/") else worktree_slot
    if areas_file is None:
        areas_file = _areas_file()
    if leases_file is None:
        # *호출 시점* REPO 기준 — 모듈 상수 LEASES_FILE 은 import 시점에 굳어 monkeypatch
        # 된 REPO 를 안 추종(hermetic 테스트 갭). REPO 에서 재구성해 추종한다(_areas_file 동형).
        leases_file = REPO / ".project_manager" / ".local" / "worktree-leases.json"
    bp = _load_pm_bootstrap()
    if bp is None:
        return None
    try:
        auto = bp._resolve_session_slot(areas_file, leases_file)
    except bp.SlotResolutionError:
        # 진짜 모호(`{2,3}`·repo≥2) — display/preview 는 fail-soft(None→legacy 표기). 실제
        # write 경로는 run() 가드가 이미 fail-loud 로 막았다(여기 도달=방어적).
        return None
    except Exception as exc:  # noqa: BLE001 — fail-soft: 판정 실패는 None(legacy 폴백).
        if _is_engine_rev_skew(exc):
            raise
        return None
    if not auto:
        return None
    repo, n = auto
    return f"{repo}_{n}"


# ── session-entry guarded 슬롯해소 (멀티-PM 모호 → fail-loud + 실행 슬롯 threading) ──
# bare handoff(`--repo`/`--slot` 미지정)는 *어느 슬롯의* 연속성을 이어야 하는지 명확해야 한다.
# 멀티-PM 셋업이 모호(등록 repo ≥2·한 repo 슬롯 ≥2 중 slot1 부재)하면, 없는 legacy `wiki/pm_state.md`
# 명시 에러로 중단한다.
#
# **단일 해소로 통일(codex round2 must-fix)**: 이 함수가 가드 단계서 슬롯을 *한 번* 해소해 실행
# 슬롯(`worktree_slot`)으로 thread 한다 — run() 이 결과를 `self._worktree_slot` 에 박으면
# downstream 전부(pm_state `_pm_state_path`·회귀/출하 cwd `_regression_cwd`·handoff entry `worktree_slot`
# 필드)가 *명시 슬롯 우선* 경로로 **같은** 슬롯을 일관되게 쓴다(이미 다들 explicit slot 우선). 이전의
# "continuity=default-1 / 회귀cwd=REPO 폴백" 비대칭은 self-split(홈엔 tests/ 없음)에서 회귀를 엉뚱한
# REPO 에서 돌려 깨졌다 — 회귀를 *활성 worktree* 서 돌리려던 목적과 정합시킨다.
# 판정은 bootstrap `_resolve_session_slot`(idle 필터·default-1) 재사용(DRY·동적 로드).
def _resolve_session_worktree_slot(
    worktree_slot: str | None = None,
    areas_file: Path | None = None,
    leases_file: Path | None = None,
) -> tuple[str | None, str | None]:
    """bare handoff 의 실행 슬롯을 해소한다 — `(resolved_slot, error_msg)`.

    반환:
      - `(worktree_slot, None)` — `worktree_slot` 인자가 이미 주어져 있으면 그대로(downstream
        explicit 우선). 이 함수엔 `main()` ingress 가 `--repo`/`--slot`을
        `_resolve_explicit_identity_slot` 으로 이미 `work/<repo>_<N>` canonical 화한 값만
        도달한다는 전제 — 여기서 재정규화하지 않는다(정규화를 소비자마다 스레딩하면 새 소비자가
        생길 때마다 재발하는 두더지잡기 — 입구 단일화가 근본).
      - `(None, None)` — solo/미해소(멀티-PM 미셋업·bootstrap 부재·판정 실패). 현행 legacy/REPO
        폴백 유지(자기-호스트 solo 무변경).
      - `(f"work/<repo>_<N>", None)` — default-1/단독/idle-필터 후 활성 슬롯으로 해소. run() 이
        이걸 `self._worktree_slot` 에 박아 pm_state·회귀cwd·entry 가 같은 슬롯을 쓴다.
      - `(None, error_msg)` — 진짜 모호(`{2,3}`·repo≥2). run() 이 surface 하고 중단(fail-loud).

    `_resolve_state_slot`(incidental·display/preview 에서도 쓰임)은 동작 유지(None·fail-soft)하고,
    *오직 session-entry* 인 이 함수만 모호를 loud 로 만들고 실행 슬롯을 thread 한다 — bootstrap 을
    모호함으로 crash 시키지 않는다. `areas_file`/`leases_file` 미지정이면 *호출 시점* REPO 기준
    해소(monkeypatch 추종·hermetic).
    """
    if worktree_slot:
        return worktree_slot, None
    if areas_file is None:
        areas_file = _areas_file()
    if leases_file is None:
        leases_file = REPO / ".project_manager" / ".local" / "worktree-leases.json"
    bp = _load_pm_bootstrap()
    if bp is None:
        return None, None  # bootstrap 부재 → 판정 불가·현행 폴백(fail-soft).
    try:
        auto = bp._resolve_session_slot(areas_file, leases_file)
    except bp.SlotResolutionError as exc:
        return None, str(exc)  # 진짜 모호 → fail-loud.
    except Exception as exc:  # noqa: BLE001 — fail-soft: 판정 실패는 현행 폴백(모호 아님).
        if _is_engine_rev_skew(exc):
            raise
        return None, None
    if not auto:
        return None, None  # solo/미해소 → 현행 폴백.
    repo, n = auto
    return f"work/{repo}_{n}", None


# ── 명시 `--repo`/`--slot` → 실행 슬롯 (라이더) ──────────
# `main()` ingress 가 `identity_args.parse_identity(args)` 로 얻은 discriminated 결과에서
# repo/slot 두 필드만 뽑아 이 함수에 넘긴다 — CLI 파싱과 리스-조인 검증을 분리해 ticket_finish
# 도 (자기 `--repo`/`--slot` 파싱 후) 동일 함수를 재사용할 수 있게 한다(동적 로드·DRY).
def _resolve_explicit_identity_slot(
    repo: str | None,
    slot: int | None,
    leases_file: Path | None = None,
) -> tuple[str | None, str | None]:
    """명시 `--repo`(+`--slot`) 를 실행 worktree 슬롯으로 해소한다 — `(worktree_slot, error_msg)`.

    규칙(actor 연산·handoff/ticket_finish 둘 다 actor):
      - `repo`·`slot` 둘 다 주어짐 → `work/<repo>_<slot>` 조립. (라이더): 리스 장부가
        읽히면(`identity_args.repo_slot_numbers` 가 `None` 아님) 그 슬롯이 실제 **활성(leased)**
        리스에 있는지 검증한다 — 없으면 세션↔repo 조인 불일치로 `(None, error_msg)`(fail-loud).
        장부 미해독(파일 부재/깨짐 → `None`)은 *검증불가*라 fail-soft(그대로 신뢰) — "판정불가"와
        "모순"은 다르게 다룬다(과잉 차단 방지).
      - `repo` 만 주어짐(슬롯 무) → `identity_args.resolve_actor_slot` 로 활성 슬롯 해소.
        활성 슬롯 ≥2(`SlotResolutionError`)나 0개(미해소)는 모두 *명시 요청이 조인 안 된 것*이라
        와 같은 결로 `(None, error_msg)` (fail-loud) — repo 만 명시했는데 조용히 무관한
        auto-resolve 경로로 새는 것을 막는다.
      - 둘 다 없음(`repo is None`) → `(None, None)` — 호출부가 기존 no-flag 자동해소
        (`_resolve_session_worktree_slot(None, ...)`)로 이어간다(이 함수 관여 밖).

    `leases_file` 미지정이면 *호출 시점* REPO 기준으로 재구성한다(monkeypatch 추종·hermetic —
    다른 리스 해소 함수들과 동형).
    """
    if repo is None:
        return None, None
    if leases_file is None:
        leases_file = REPO / ".project_manager" / ".local" / "worktree-leases.json"
    if slot is not None:
        known = identity_args.repo_slot_numbers(repo, leases_file)
        if known is not None and slot not in known:
            listing = ", ".join(str(n) for n in known) if known else "없음"
            return None, (
                f"[M3] 세션↔repo 조인 불일치 — repo '{repo}' 의 활성 슬롯({listing}) 중 "
                f"{slot} 이 없다. `--slot` 을 정확히 지정하라."
            )
        return f"work/{repo}_{slot}", None
    # repo 만(슬롯 무) — actor 활성슬롯 자동해소.
    try:
        resolved = identity_args.resolve_actor_slot(repo, leases_file)
    except identity_args.SlotResolutionError as exc:
        return None, str(exc)
    if resolved is None:
        return None, (
            f"[M3] repo '{repo}' 에 활성(leased) 슬롯이 없다 — 세션↔repo 조인 불가. "
            "`--slot <N>` 으로 명시하거나 셋업을 확인하라."
        )
    return f"work/{resolved}", None


def _pm_state_path(
    worktree_slot: str | None = None,
    areas_file: Path | None = None,
    leases_file: Path | None = None,
    *,
    migrate: bool = True,
) -> Path:
    """활성 슬롯의 pm_state 경로를 해소한다 (+graceful 마이그레이션·솔로 폴백).

    경로 우선순위:
      - 슬롯 미해소(솔로/모호) → legacy `wiki/pm_state.md` (현행·무변경).
      - slot 경로(`.local/slots/<slot>/pm_state.md`) 이미 존재 → 그대로(이미 per-slot).
      - slot 부재 + legacy 존재 → `migrate=True` 면 legacy → slot 으로 *이동* 후 slot 반환,
        `migrate=False` 면 이동 없이 **legacy** 반환(현 읽기 위치·미리보기·부작용 0).
      - slot·legacy 둘 다 부재(드문 엣지·fresh) → **slot 경로** 반환(정식 위치·쓰기 시 생성).

    `migrate` 는 *파일 이동* 만 가른다(경로 우선순위는 동일). run() 은 진입부에서
    `migrate=False`(읽기 위치만)로 호출하고, 모든 중단 게이트 통과 후 pm_state 첫 접촉 직전에만
    `_migrate_legacy_pm_state` 로 실제 이동을 수행한다 — "중단 시 pm_state 무접촉" 보장 보존
    (codex 교차검증 must-fix). dry-run 은 이동을 절대 하지 않는다(미리보기).

    `migrate=True` 일 때만 divergent bare 슬롯 dir(`slots/<N>`) 도 canonical
    `slots/<repo>_<N>` 로 backfill 한다— legacy 마이그레이션과 같은 타이밍(게이트
    통과 후·첫 접촉 직전)이라 "중단 시 pm_state 무접촉" 보장을 그대로 지킨다.
    """
    slot = _resolve_state_slot(worktree_slot, areas_file, leases_file)
    legacy = _legacy_pm_state_file()
    if slot is None:
        # 솔로/모호 — 현행 단일 pm_state(무변경).
        return legacy
    if migrate:
        _backfill_divergent_slot_dir(slot)
    slot_path = _slots_root() / slot / "pm_state.md"
    if slot_path.exists():
        return slot_path
    if legacy.exists():
        if not migrate:
            # 이동 없이 현 읽기 위치(legacy)를 반환(진입부 target·미리보기·부작용 0).
            return legacy
        # graceful 마이그레이션 — legacy → slot 경로 이동(일회성·per-slot 화·atomic·동일 FS).
        slot_path.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(slot_path)
        return slot_path
    # slot 경로·legacy 둘 다 부재 — slot 경로를 정식 위치로 반환(쓰기 시 생성).
    return slot_path


def _migrate_legacy_pm_state(
    worktree_slot: str | None = None,
    areas_file: Path | None = None,
    leases_file: Path | None = None,
) -> Path:
    """legacy `wiki/pm_state.md` 를 활성 슬롯 경로로 이동하고 최종 pm_state 경로를 반환한다.

    `_pm_state_path(..., migrate=True)` 의 명시 별칭 — run() 이 **모든 중단
    게이트(회귀·출하) 통과 후·pm_state 첫 접촉 직전**에 단 한 번 호출한다(트랜잭션 보장:
    중단 시 pm_state 무접촉·codex must-fix). 멱등·비파괴(slot 이미 존재면 이동 안 함)·
    legacy 부재면 이동 없이 slot 경로 반환(쓰기 시 생성). dry-run 경로는 이 함수를 호출하지
    않는다(진입부 migrate=False target 을 그대로 읽음·미리보기·부작용 0).
    """
    return _pm_state_path(worktree_slot, areas_file, leases_file, migrate=True)


def _default_python() -> str:
    """플랫폼-인지 venv 인터프리터 경로 (없으면 sys.executable 폴백).

    Windows 는 venv/Scripts/python.exe, POSIX 는 venv/bin/python. venv 가 없으면
    현재 인터프리터로 폴백한다. 이 머신은 시스템 python3 에 pytest 가 없고 venv 에만
    있으므로, venv 가 있으면 무조건 venv 를 우선해 회귀 측정 인터프리터를 보존한다.
    (이 도구는 board.py 를 import 하지 않으므로 헬퍼 중복 보유 — 도구 간 의존 없음.)
    """
    cand = REPO / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(cand) if cand.exists() else sys.executable

# ── 세션 차수 추론 placeholder (infer_next_session_num 사용) ──────────────────
TRIGGER_SESSION_PLACEHOLDER = "?"            # 세션 차수 추론 불가 시 안전한 placeholder.

# ── 상수 ─────────────────────────────────────────────────────────────────────

# pm_state.md 길이 경고 임계값 (핸드오프 절차 7단계 — 세션 정리 누락 신호)
PM_STATE_LINE_WARNING_THRESHOLD = 700

# log/current.md entry 누적 경고 임계값 — 초과 시 pm_log.py archive 권장 (차단 아님).
LOG_ARCHIVE_SUGGEST_THRESHOLD = 40

# log entry 시작 줄 ("## [YYYY-MM-DD] ...") — 누적 카운트용 (pm_log.split_entries 와 동일 형식).
_LOG_ENTRY_RE = re.compile(r"^## \[\d{4}-\d{2}-\d{2}\]", re.MULTILINE)

# 슬라이딩 윈도우 크기 — 최근 N 차 만 short inline 유지. 프로젝트별 조정 가능.
SLIDING_WINDOW_SIZE = 3

# ── dirty-tree 게이트 상수 ([0/7]) ────────────────────────────────────────────
# 차단/경고 메시지에 열거할 최대 경로 수 — 초과분은 "… 외 N건" 으로 접는다(수백 건 dump 방지).
DIRTY_GATE_LIST_LIMIT = 20

# ── 출하 변경 분류 글롭 (surface) ─────
# 미push diff 가 이 글롭 중 하나라도 건드리면 출하 변경 → 비차단 surface([1b/7]). 채택자
# 산출물을 바꾸는 경로([[smoke-gate-by-output-change]])만 포함한다 — 엔진(.project_manager/
# tools)·출하 템플릿·어댑터(.claude/.opencode)·진입문서·manifest·파사드·요구사항·방법론 wiki.
# NON-SHIPPING(tests/·wiki board/ADR/spike·status/pm_state/log)은 매칭 안 돼 자연 skip.
# fnmatch 글롭 — `**` 는 임의 깊이, 정확 파일명은 그대로. baseline 기준 ref(@{upstream}/
# origin/main) 해소불가나 비분류 경로는 호출부에서 has_unknown(ambiguous) 처리.
SHIPPING_GLOBS = (
    ".project_manager/tools/*",       # canonical 엔진 (한 단계)
    ".project_manager/tools/**",      # canonical 엔진 (중첩)
    "templates/**",                   # 출하 템플릿 (claude_code·opencode)
    ".claude/**",                     # claude 어댑터 (agents·skills·commands)
    ".opencode/**",                   # opencode 어댑터
    "CLAUDE.md",                      # claude 진입문서
    "AGENTS.md",                      # opencode 진입문서
    "**/CLAUDE.md",                   # 중첩 진입문서 (templates 하위 등)
    "**/AGENTS.md",
    "engine.manifest",                # 엔진 동기 매니페스트
    "**/engine.manifest",
    "pm-*.sh",                        # 루트 파사드 (POSIX)
    "pm-*.cmd",                       # 루트 파사드 (Windows)
    "requirements*.txt",              # 런타임/개발 의존성
    ".project_manager/wiki/pm_role.md",       # 방법론 — templates 로 출하
    ".project_manager/wiki/pm_playbook.md",
    ".project_manager/wiki/README.md",
    ".project_manager/wiki/_template/**",
    ".project_manager/wiki/domain/**",
    # ── engine.manifest 정합 갭 (정확 경로 1:1·과잉발동 회피) ──────────
    # manifest 출하 항목 중 위 글롭에 안 잡히던 5경로를 *정확 경로* 글롭으로 1:1 닫는다
    # 포괄 글롭(`**/_template.md`·`**/*.template.md`·`**/.gitignore` 등)은 repo
    # 전체를 매칭해 비-출하(tests/fixtures/_template.md·wiki decisions/foo.template.md 등)
    # 까지 게이트를 false-fire 시킨다 — ticket 결정("정밀·과잉발동 회피")·tests/ non-shipping
    # 원칙과 모순. 1:1 정확 경로라도 미래 manifest 항목 추가는 정합 가드(test)가 잡아 동기화 강제.
    ".project_manager/wiki/tickets/_template.md",     # ticket 스캐폴드 (manifest 갭)
    ".project_manager/wiki/raw/spikes/_template.md",  # spike 스캐폴드 (manifest 갭)
    ".project_manager/wiki/pm_state.template.md",     # pm_state 템플릿 (manifest 갭)
    ".gitattributes",                                 # log union-merge·forwarder EOL (루트만)
    ".project_manager/.gitignore",                    # .project_manager .gitignore (manifest 갭)
)

# ── log/current.md handoff entry skeleton ────────────────────────────────────────────

PENDING_INTENT_PLACEHOLDER = (
    "<PM 손 — 다음 우선순위 + 사용자 결정 대기. board open ticket 재열거 금지.>"
)


def _worktree_line(worktree_slot: str | None, branch: str | None) -> str:
    """handoff entry 의 worktree slot/branch 기록 줄을 빌드한다 (회전 연속성).

    multi-PM 모드에서 worktree_slot 이 주어지면 슬롯/브랜치를 한 줄로 기록한다 — 다음
    bootstrap 이 회전 재부착(같은 슬롯 resume)할 때 연속성 단서가 된다. 솔로(미지정)면
    빈 문자열을 반환해 줄 자체를 생략한다(현행 lean 스키마 100% 보존·하위호환).
    """
    if not worktree_slot:
        return ""
    branch_part = branch if branch else "(미지정)"
    return f"- worktree: slot=`{worktree_slot}` · branch=`{branch_part}` (회전 재부착 단서)\n"


# task 세션 정체성의 log 헤더 태그 sentinel — task 모드는 헤더 태그를 `(task:<name>)` 로
# 박아 서술형 괄호(`PM 4차 (아침 대화)`)·슬롯 태그(`<repo>_<N>`)와 기계적으로 구분한다. task 명은
# 자유 포맷(한글·공백 허용)이라 bare `(<name>)` 로는 서술 괄호와 오탐이 나서 무태그 흡수 회귀
# (`test_handoff_regex_ignores_descriptive_parens_for_solo`)를 깬다 — sentinel 이 그 클래스를
# 닫는다. pm_bootstrap 소비측(`_LOG_HANDOFF_HEADER_RE`·`_session_owns_untagged`)의 `task:` 판별과
# 미러(모듈 격리라 상수를 각 모듈에 inline). dashboard 자기 섹션은 sentinel 없이 verbatim
# `## <task>`(interface 2) — 사람 가독 표면과 기계 파싱 표면의 요구가 달라 값을 분리한다.
_TASK_TAG_PREFIX = "task:"
_UNRESOLVED_BOUNDARY_ENTRY_LIMIT = 10
_ENTRY_TYPE_RE = r"(?:\S+|<!-- feat/fix/verify/… -->)"
# board.py의 발행 문법(`_TICKET_ID_BODY`) 미러: legacy `T-NNNN`와
# namespaced `T-<prefix>-NNN`을 함께 소비한다. prefix는 영숫자로 시작하고 이후
# 영숫자/underscore/hyphen을 허용한다(`T-PAY-001`, `T-service-a-001`).
_TICKET_ID_BODY = r"T-(?:[A-Za-z0-9][A-Za-z0-9_-]*-\d+|\d+)"
_ANY_HANDOFF_RE = re.compile(
    r"^## \[\d{4}-\d{2}-\d{2}\] handoff \| PM \d+차"
    r"(?: \(.+\))? → 다음 PM 세션$"
)
_TASK_ENTRY_TAG_RE = re.compile(r"(?: \(|\| \()task:[^)]+\)")
_SLOT_ENTRY_TAG_RE = re.compile(r" \([^()\s]+_\d+\)$")


def _log_entry_headers(log_text: str) -> list[str]:
    """`_LOG_ENTRY_RE`가 가리키는 entry 헤더만 원문 순서로 반환한다."""
    headers: list[str] = []
    for match in _LOG_ENTRY_RE.finditer(log_text):
        line_end = log_text.find("\n", match.start())
        if line_end < 0:
            line_end = len(log_text)
        headers.append(log_text[match.start():line_end].rstrip("\r"))
    return headers


def _collect_session_entries(
    log_text: str,
    task: str | None,
    session: str | None = None,
) -> tuple[list[str], bool]:
    """직전 자기 handoff 이후 이 세션에 귀속된 완료/checkpoint 헤더를 수집한다.

    task 세션은 같은 ``(task:<name>)`` handoff를 경계로 삼고, ticket_finish/pm_log 가 생산하는
    같은 task 태그의 티켓 완료형/checkpoint만 소비한다. 자기 handoff가 아직 없으면 태그가 귀속
    권위이므로 무관 handoff로 자르지 않고 전체에서 최근 10건만 반환한다. 완료형의 action은
    ``feat``/``fix``/``verify`` 등 임의 단어이며 board 발행 문법의 ticket ID 뒤 ``— …`` 형태로
    판별한다. slot 세션도 같은 canonical session 태그 entry만 소비한다. legacy 무태그 entry는
    동시 slot 혼입 위험 때문에 slot 모드에서 수집하지 않는다. solo 세션만 태그 없는 canonical
    handoff를 경계로 삼고, 자기 경계가 없으면 가장 최근의 다른 handoff로 폴백한다. 본문은
    반환하지 않아 log 원문이 서사의 단일 진실로 남는다.
    """
    headers = _log_entry_headers(log_text)
    if task is not None:
        escaped_task = re.escape(task)
        handoff_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] handoff \| PM \d+차 "
            rf"\({_TASK_TAG_PREFIX}{escaped_task}\) → 다음 PM 세션$"
        )
        ticket_entry_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] {_ENTRY_TYPE_RE} \| {_TICKET_ID_BODY} — .+ "
            rf"\({_TASK_TAG_PREFIX}{escaped_task}\)$"
        )
        checkpoint_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] checkpoint \| "
            rf"\({_TASK_TAG_PREFIX}{escaped_task}\) — .+$"
        )
        own_boundaries = [
            index for index, header in enumerate(headers)
            if handoff_re.fullmatch(header)
        ]
        boundary = own_boundaries[-1] if own_boundaries else -1
        entries = [
            header
            for header in headers[boundary + 1:]
            if ticket_entry_re.fullmatch(header) or checkpoint_re.fullmatch(header)
        ]
        unresolved = boundary < 0
        return (
            entries[-_UNRESOLVED_BOUNDARY_ENTRY_LIMIT:] if unresolved else entries,
            unresolved,
        )

    if session is not None:
        escaped_session = re.escape(session)
        handoff_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] handoff \| PM \d+차 "
            rf"\({escaped_session}\) → 다음 PM 세션$"
        )
        ticket_entry_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] {_ENTRY_TYPE_RE} \| {_TICKET_ID_BODY} — .+ "
            rf"\({escaped_session}\)$"
        )
        checkpoint_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] checkpoint \| "
            rf"\({escaped_session}\) — .+$"
        )
    else:
        handoff_re = re.compile(
            r"^## \[\d{4}-\d{2}-\d{2}\] handoff \| PM \d+차 → 다음 PM 세션$"
        )
        ticket_entry_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] {_ENTRY_TYPE_RE} \| {_TICKET_ID_BODY} — .+$"
        )
        checkpoint_re = re.compile(
            r"^## \[\d{4}-\d{2}-\d{2}\] checkpoint \| .+$"
        )
    own_boundaries = [
        index for index, header in enumerate(headers)
        if handoff_re.fullmatch(header)
    ]
    # 무관 handoff 경계 폴백은 태그 귀속 권위가 없는 solo legacy에만 허용한다.
    fallback_boundaries = (
        [
            index for index, header in enumerate(headers)
            if _ANY_HANDOFF_RE.fullmatch(header)
        ]
        if session is None else []
    )
    boundary = own_boundaries[-1] if own_boundaries else (
        fallback_boundaries[-1] if fallback_boundaries else -1
    )
    entries = [
        header
        for header in headers[boundary + 1:]
        if not _TASK_ENTRY_TAG_RE.search(header)
        and (session is not None or not _SLOT_ENTRY_TAG_RE.search(header))
        and (ticket_entry_re.fullmatch(header) or checkpoint_re.fullmatch(header))
    ]
    unresolved = boundary < 0
    return (
        entries[-_UNRESOLVED_BOUNDARY_ENTRY_LIMIT:] if unresolved else entries,
        unresolved,
    )


def collect_session_entries(
    log_text: str,
    task: str | None,
    session: str | None = None,
) -> list[str]:
    """현재 세션 entry만 반환한다. 경계 미해소 시 최근 10건으로 제한한다."""
    return _collect_session_entries(log_text, task, session)[0]


def _session_entries_text(entries: list[str], boundary_unresolved: bool = False) -> str:
    """handoff skeleton의 자동 박제 목록을 compact한 중첩 목록으로 렌더한다."""
    notice = (
        f" (경계 미해소 — 최근 {_UNRESOLVED_BOUNDARY_ENTRY_LIMIT}건)"
        if boundary_unresolved else ""
    )
    if not entries:
        # 0건이면 경계 표기를 붙이지 않는다 — 빈 목록엔 "최근 N건" 서술이 무의미(혼동만).
        return " (이 세션 박제 entry 없음)"
    return notice + "\n" + "\n".join(f"  - {header}" for header in entries)


def _session_tag(session: str | None) -> str:
    """handoff 헤더의 세션 정체성 태그 조각을 빌드한다 — ` ({session})`.

    멀티-PM 모드에서 세션 정체성(canonical `<repo>_<N>`)이 해소되면 헤더 차수 뒤에 `({session})`
    태그를 붙인다 — per-slot 시퀀스의 감사 단서다(이벤트 메타데이터·상태 저장 아님
    무충돌). 솔로(미해소·None/빈문자)면 빈 문자열을 반환해 태그를 생략한다 — 현행 헤더와
    byte-호환·하위호환. 선행 공백까지 포함해 반환하므로 템플릿은 `PM {session_num}차{session_tag}`
    로 이어 붙인다(태그 없을 땐 `PM {N}차 →` 로 정확히 현행 스키마 보존).
    """
    if not session:
        return ""
    return f" ({session})"


def _normalize_session_num(session_num: int | str) -> str:
    """세션 차수를 bare 숫자 문자열로 정규화한다 — `19`·`'19'`·`'19차'`·`'19차차'` 모두 `'19'`.

    handoff entry 템플릿은 `PM {session_num}차` 로 '차' 를 *붙인다*. skill 문서가
    `--session-num <N차>` 로 안내해 온 탓에 입력에 이미 '차' 가 있으면 이중 부착('19차차')
    됐고(PM 9차에 "사소"로 기록 후 미수정·재발), sliding-window 정규식 `\\d+차` 매칭도 깨졌다.
    후행 '차'/공백을 멱등 제거해 어느 입력이든 안전하게 만든다."""
    return str(session_num).strip().rstrip("차").strip()


HANDOFF_LOG_SKELETON_TEMPLATE = """\
## [{date}] handoff | PM {session_num}차{session_tag} → 다음 PM 세션

- 이 세션 박제 entries:{entries}
- 메타 학습: <PM 손 — ticket 상태에서 도출 불가한 교훈만. 없으면 "없음".>
- pending user intent: {pending_intent}
{worktree_line}{dirty_ack_line}- 회귀/incident: <PM 손 — 회귀 "N passed / 상태" 1줄(green 도 — baseline) + 비-자명 incident. (회귀는 1줄 load-bearing 이라 항상 적음 — board/git/log 대량 재열거만 금지.)>
"""

# dirty-tree ack 박제 줄의 라벨 — `incident` 줄 관례를 따르되 기계-소유임을 라벨로 구분한다
# (PM 손-채움 `- 회귀/incident:` 줄과 별개 줄·grep 가능한 고정 접두). 같은-세션 재실행의 멱등
# upsert 도 이 라벨을 앵커로 쓴다(기계 소유 구획 = 이 줄 하나).
DIRTY_ACK_ENTRY_LABEL = "- incident (dirty-tree ack)"

# 기존 entry 안의 ack 줄(멱등 upsert 대상). 라벨로 시작하는 한 줄 전체를 잡되 **줄 종결자의 CR 은
# 따로 캡처**한다 — `[^\n]*` 로 뭉뚱그리면 CR 까지 소비한 자리에 CR 없는 본문을 넣어 CRLF 로그에
# bare LF 를 섞고(줄 종결자 변조) 같은 사유 재실행이 byte-identical 하지 않게 된다.
_DIRTY_ACK_LINE_RE = re.compile(
    r"^" + re.escape(DIRTY_ACK_ENTRY_LABEL) + r":[^\r\n]*(?P<cr>\r?)$", re.MULTILINE
)

# ack 줄 삽입 앵커 — PM 손-채움 `- 회귀/incident:` 줄 **바로 앞**(신규 skeleton 과 같은 자리).
_INCIDENT_LINE_RE = re.compile(r"^- 회귀/incident:", re.MULTILINE)


class DirtyAckStampError(RuntimeError):
    """기존 handoff entry 에 ack 사유를 박제할 자리를 찾지 못했다 (부작용 0 상태에서 발생)."""


def _dirty_ack_line(note: str | None) -> str:
    """handoff entry 의 dirty-tree ack 박제 줄을 빌드한다 (미ack 이면 줄 자체 생략).

    dirty 를 override 하고 나간 세션(`--ack-dirty` 또는 사용자 명시 핸드오프 호환 신호)만
    이 줄을 갖는다 —
    다음 세션 부트스트랩이 log entry 에서 "왜 미커밋인 채 넘어왔나"를 그대로 읽는다.
    """
    if not note:
        return ""
    return f"{DIRTY_ACK_ENTRY_LABEL}: {note}\n"


def upsert_dirty_ack_line(entry_text: str, note: str, newline: str = "\n") -> str:
    """기존 handoff entry 에 ack 줄을 **멱등 upsert** 한 entry 텍스트를 반환한다.

    같은-세션 재실행은 기존 entry 본문을 보존하는 경로라 skeleton 을 다시 만들지 않는다. 그렇다고
    ack 를 콘솔 경고로 흘리면 "왜 미커밋인 채 넘어왔나"가 log 에 남지 않는다 — 기계 소유 줄
    하나(`DIRTY_ACK_ENTRY_LABEL`)만 골라 갱신한다:
      - 이미 있으면 그 줄을 **교체**(재삽입 없음·같은 입력이면 byte 동일 → 멱등). 기존 줄의
        종결자(CRLF/LF)를 그대로 되살려 CRLF 로그에 bare LF 를 섞지 않는다.
      - 없으면 `- 회귀/incident:` 줄 바로 앞에 삽입(신규 skeleton 과 같은 자리).
      - 그 앵커도 없으면 entry 끝에 붙인다.
    entry 가 비어 앵커도 끝줄도 없으면 `DirtyAckStampError` — 조용히 통과시키지 않는다
    (박제 불가는 차단이 기본).
    """
    line = f"{DIRTY_ACK_ENTRY_LABEL}: {note}"
    existing = _DIRTY_ACK_LINE_RE.search(entry_text)
    if existing is not None:
        replacement = line + existing.group("cr")
        return entry_text[:existing.start()] + replacement + entry_text[existing.end():]
    anchor = _INCIDENT_LINE_RE.search(entry_text)
    if anchor is not None:
        return entry_text[:anchor.start()] + line + newline + entry_text[anchor.start():]
    if not entry_text.strip():
        raise DirtyAckStampError(
            "기존 handoff entry 가 비어 ack 사유를 박제할 자리가 없다"
        )
    suffix = "" if entry_text.endswith(("\n", "\r")) else newline
    return entry_text + suffix + line + newline


def build_handoff_log_skeleton(
    session_num: int | str,
    date: str | None = None,
    worktree_slot: str | None = None,
    branch: str | None = None,
    session: str | None = None,
    log_text: str = "",
    task: str | None = None,
    dirty_ack_note: str | None = None,
) -> str:
    """log/current.md 에 append 할 handoff entry skeleton 을 반환한다.

    log_text/task로 직전 자기 handoff 이후 complete/checkpoint 헤더를 자동 채운다.
    worktree_slot 주입 시(multi-PM 모드) slot/branch 기록 줄을 추가한다 — 회전 재부착
    연속성 단서. 미지정(솔로)이면 줄 생략(현행 스키마 보존).
    session 주입 시(multi-PM 정체성 해소·canonical `<repo>_<N>`) 헤더 차수 뒤에 `({session})`
    정체성 태그를 박는다 — per-slot 시퀀스의 감사 단서(이벤트 메타·상태 저장 아님).
    미지정(솔로)이면 태그를 생략해 현행 헤더와 byte-호환이다.
    dirty_ack_note 주입 시(dirty-tree 게이트 override) ack 사유 줄을 추가한다 — 미지정이면
    줄 생략(clean 핸드오프는 현행 스키마 byte-호환).
    """
    if date is None:
        date = datetime.date.today().isoformat()
    entries, boundary_unresolved = _collect_session_entries(log_text, task, session)
    return HANDOFF_LOG_SKELETON_TEMPLATE.format(
        date=date,
        session_num=_normalize_session_num(session_num),
        session_tag=_session_tag(session),
        entries=_session_entries_text(entries, boundary_unresolved),
        pending_intent=PENDING_INTENT_PLACEHOLDER,
        worktree_line=_worktree_line(worktree_slot, branch),
        dirty_ack_line=_dirty_ack_line(dirty_ack_note),
    )


# 같은 세션에서 핸드오프를 재실행할 때 기존 entry 중 기계 소유 구획만 갱신한다. 후보
# 정규식은 일부러 관대하게 두어 차수 오형식도 "기존 handoff는 있으나 파싱 실패"로 loud
# surface 하고, 실제 갱신은 canonical 헤더 정규식이 성공한 경우에만 허용한다.
_HANDOFF_HEADER_CANDIDATE_RE = re.compile(
    r"^## \[[^\]\n]+\]\s+handoff\s*\|\s*PM\s+.*→ 다음 PM 세션\s*$",
    re.MULTILINE,
)
_HANDOFF_HEADER_SESSION_RE = re.compile(
    r"^## \[\d{4}-\d{2}-\d{2}\] handoff \| PM (?P<num>\d+)차"
    r"(?: \((?P<session>[^()]+)\))? → 다음 PM 세션$"
)
_SESSION_ENTRIES_BLOCK_RE = re.compile(
    r"^- 이 세션 박제 entries:[^\n]*"
    r"(?:\n  - ## \[[^\n]*)*",
    re.MULTILINE,
)
_NESTED_SESSION_ENTRY_RE = re.compile(r"^  - (## \[[^\n]+)$", re.MULTILINE)


def _read_log_text_exact(path: Path) -> str:
    """UTF-8 log를 universal-newline 변환 없이 읽어 원문 bytes를 보존한다."""
    return Path(path).read_bytes().decode("utf-8")


def _handoff_header_belongs_to_session(header: str, session: str | None) -> bool:
    """handoff 후보 헤더가 현재 task/slot/solo 정체성에 속하는지 보수적으로 판정한다.

    차수는 per-slot 시퀀스라 다른 슬롯의 같은 N차는 정상이다. 따라서 "같은 차수" 판정
    전에 현재 정체성 태그를 먼저 좁힌다. solo는 태그 없는 헤더만 자기 것으로 본다.
    """
    if session is not None:
        return header.rstrip().endswith(f" ({session}) → 다음 PM 세션")
    return "(" not in header and header.rstrip().endswith(" → 다음 PM 세션")


def _prepare_handoff_log_change(
    log_text: str,
    *,
    session_num: int | str,
    date: str,
    worktree_slot: str | None,
    branch: str | None,
    session: str | None,
    task: str | None,
    dirty_ack_note: str | None = None,
) -> tuple[bool, str, str, str | None]:
    """handoff log 변경 계획을 만든다.

    반환은 ``(update_mode, payload, preview, warning)`` 이다. update_mode면 payload는
    기존 log 전체를 비파괴 갱신한 값이고, 아니면 append할 문자열이다. 같은 정체성의 마지막
    handoff 차수가 이번 차수와 같을 때만 갱신한다. 헤더 차수 파싱 실패나 기계 구획 경계
    모호(marker 0/2+)는 현행 append로 보수적 폴백하며 warning 한 줄을 돌려준다.

    dirty_ack_note 는 신규 entry(append) skeleton 에만 박힌다 — 같은-세션 갱신 모드는 기계
    소유 구획('이 세션 박제 entries')만 건드리는 byte-멱등 경로라 본문 줄을 새로 끼우지 않는다.
    """
    normalized_num = _normalize_session_num(session_num)
    candidates = [
        match
        for match in _HANDOFF_HEADER_CANDIDATE_RE.finditer(log_text)
        if _handoff_header_belongs_to_session(match.group(0), session)
    ]

    warning: str | None = None
    target: re.Match | None = None
    if candidates:
        candidate = candidates[-1]
        parsed = _HANDOFF_HEADER_SESSION_RE.fullmatch(candidate.group(0).rstrip())
        if parsed is None:
            warning = (
                "같은-세션 판정 실패 — 마지막 handoff 헤더 차수 파싱 실패; "
                "보수적으로 새 entry를 append합니다 (이중 부기 가능)."
            )
        elif not normalized_num.isdigit():
            warning = (
                f"같은-세션 판정 실패 — 이번 실행 차수 {normalized_num!r} 파싱 실패; "
                "보수적으로 새 entry를 append합니다 (이중 부기 가능)."
            )
        elif int(parsed.group("num")) == int(normalized_num):
            target = candidate

    if target is not None:
        # entry 경계는 다음 canonical log entry 직전. 중첩 박제 목록의 `  - ## [...]`는
        # 두 칸 들여써 `_LOG_ENTRY_RE` 경계가 되지 않는다.
        next_entry = _LOG_ENTRY_RE.search(log_text, target.end())
        entry_end = next_entry.start() if next_entry is not None else len(log_text)
        entry_text = log_text[target.start():entry_end]
        blocks = list(_SESSION_ENTRIES_BLOCK_RE.finditer(entry_text))
        if len(blocks) == 1:
            original_block = blocks[0].group(0)
            block_newline = (
                "\r\n"
                if "\r\n" in original_block or original_block.endswith("\r")
                else "\n"
            )
            old_entries = [
                header.rstrip("\r")
                for header in _NESTED_SESSION_ENTRY_RE.findall(original_block)
            ]
            fresh_entries, _boundary_unresolved = _collect_session_entries(
                log_text, task, session
            )
            # 기존 기계 목록 + 조기 handoff 뒤 새 complete/checkpoint를 원문 순서로 합친다.
            # dict는 insertion order를 보존해 재실행도 byte-멱등이다.
            entries = list(dict.fromkeys([*old_entries, *fresh_entries]))
            refreshed_block = (
                "- 이 세션 박제 entries:" + _session_entries_text(entries)
            )
            if block_newline != "\n":
                refreshed_block = refreshed_block.replace("\n", block_newline)
            # regex는 마지막 줄의 CR은 block에, LF는 suffix에 남긴다. CRLF block이면
            # replacement 끝의 CR도 되살려 경계 개행이 bare LF로 변하지 않게 한다.
            if original_block.endswith("\r") and not refreshed_block.endswith("\r"):
                refreshed_block += "\r"
            new_entry_text = (
                entry_text[:blocks[0].start()]
                + refreshed_block
                + entry_text[blocks[0].end():]
            )
            # ack 사유는 기존 entry 안에서 **기계 소유 줄 하나**로 멱등 upsert 한다 — 재실행이
            # 줄을 쌓지 않고, 박제할 자리가 없으면 DirtyAckStampError 로 fail-loud(무접촉 상태에서
            # 발생 — 이 함수는 계획만 만들고 write 는 호출부가 뒤에 한다).
            if dirty_ack_note:
                new_entry_text = upsert_dirty_ack_line(
                    new_entry_text, dirty_ack_note, block_newline
                )
            updated = (
                log_text[:target.start()] + new_entry_text + log_text[entry_end:]
            )
            return True, updated, refreshed_block, None
        warning = (
            "같은-세션 판정 실패 — 기존 handoff의 '이 세션 박제 entries' 구획 경계가 "
            f"모호함(marker {len(blocks)}개); 보수적으로 새 entry를 append합니다 "
            "(이중 부기 가능)."
        )

    skeleton = build_handoff_log_skeleton(
        session_num=normalized_num,
        date=date,
        worktree_slot=worktree_slot,
        branch=branch,
        session=session,
        log_text=log_text,
        task=task,
        dirty_ack_note=dirty_ack_note,
    )
    return False, "\n" + skeleton, skeleton, warning


def _commit_handoff_log_change(
    log_file: Path,
    *,
    session_num: int | str,
    date: str,
    worktree_slot: str | None,
    branch: str | None,
    session: str | None,
    task: str | None,
    pm_log_module=None,
    dirty_ack_note: str | None = None,
) -> tuple[bool, str, str | None]:
    """handoff log 판정과 append/replace를 공유 lock 한 구간에서 원자화한다.

    반환은 ``(same_session_rerun, preview, warning)``. ``pm_log_module``은 lock 보유 중
    append가 실행되는지를 결정적으로 검증하는 테스트 DI seam이다.
    """
    pm_log = pm_log_module or _load_pm_log()
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with pm_log.log_write_lock(log_file):
        log_text = _read_log_text_exact(log_file) if log_file.exists() else ""
        same_session_rerun, payload, preview, warning = _prepare_handoff_log_change(
            log_text,
            session_num=session_num,
            date=date,
            worktree_slot=worktree_slot,
            branch=branch,
            session=session,
            task=task,
            dirty_ack_note=dirty_ack_note,
        )
        if same_session_rerun:
            pm_log._replace_atomic(log_file, payload)
        else:
            pm_log.append_log_locked(log_file, payload)
    return same_session_rerun, preview, warning


# ── pm_state.md sliding window 편집 ──────────────────────────────────────────

# 세션 식별 절 앵커: pm_state.md 의 "## 세션 식별 (현재까지 사용된 이름)" 로 시작하는 h2 절.
# 매칭은 정확 str.find 이 아니라 정규화 부분일치로 관대화한다 — 채택자 pm_state 의 h2 헤더가
# 공백/괄호/여백 변형(괄호 내용 다름·2칸 공백·trailing space 등)이어도 매치한다(
# finance_dev D3: 미세 변형에 ValueError→핸드오프가 9세션 연속 죽던 회귀). 이 상수 문자열
# 자체는 ValueError 메시지의 canonical 앵커 표기로만 남는다.
_SESSION_SECTION_ANCHOR = "## 세션 식별 (현재까지 사용된 이름)"

# 앵커 정규화 부분일치 대상: h2 헤더에서 '#'·공백(전각/반각)·괄호(전각/반각)를 제거한
# 정규화 문자열이 이 값을 포함하면 세션 식별 절로 본다.
_SESSION_ANCHOR_NORMALIZED = "세션식별"

# 정규화에서 제거하는 비-공백 문자(괄호 전각/반각 + 헤더 마커). 공백류(반각/전각/탭/CR)는
# str.isspace() 로 함께 제거한다.
_ANCHOR_STRIP_CHARS = frozenset("#()（）")

# pm 세션 entry 줄: "  - **N차** (..." 형식
# 각 줄은 반드시 두 칸 들여쓰기 + "- **N차**" 로 시작한다.
_PM_SESSION_ANCHOR_RE = re.compile(
    r"^  - \*\*(\d+차)\*\*[^\n]*$",
    re.MULTILINE,
)

# "이전 차" 포인터 줄
_PREV_SESSIONS_POINTER = "  - 이전 차"

# 오형식 `**N차차+**` → `**N차**` 정규화 대상.
# 세션 entry bold 토큰에서 '차' 가 2회 이상 연속 반복된 것만 잡는다 — 단일 '차'=정상이라
# 미매치(멱등: 재실행 무변화). skill 문서가 `--session-num <N차>` 로 안내한 탓에 '차' 가 이미
# 붙은 입력에 handoff 가 다시 '차' 를 부착해 `**N차차**` 가 만들어졌고, 그러면
# `_PM_SESSION_ANCHOR_RE`(`\d+차` 1회)가 미매치해 pm_state derive 실패→log 폴백 은닉 의존.
# 파서 관대화(fallback 누적)가 아니라 원천 데이터 정규화로 해소한다(
# prefer-data-migration-over-fallback).
_MALFORMED_SESSION_ANCHOR_RE = re.compile(r"\*\*(\d+)차{2,}\*\*")

# 정상(`**N차**`) + 오형식(`**N차차+**`) anchor 를 모두 매치하는 entry 판정 정규식 — 세션 식별
# *절 선택* 전용(normalize_session_anchors). 절 선택에 `_PM_SESSION_ANCHOR_RE`(정상만)를 쓰면
# entry 가 전부 오형식인 실 타깃(finance: `88차차/87차차/89차차`·well-formed 0)에서 window 절이
# "entry 없음"으로 판정→앞선 설명 절(`## 세션 식별 규칙`)로 폴백→**silent no-op**(마이그레이션
# 도구가 정작 필요한 상황에서 조용히 실패·최악)이 된다. `차+`(1회 이상)로 오형식 절도
# entry-bearing 으로 인정해 그 함정을 막는다(codex must-fix). handoff write 경로는 이 정규식을
# 쓰지 않으므로(default=_PM_SESSION_ANCHOR_RE) 무영향.
_ANY_SESSION_ANCHOR_RE = re.compile(r"^  - \*\*\d+차+\*\*[^\n]*$", re.MULTILINE)


def _normalize_h2_header(line: str) -> str:
    """h2 헤더 줄에서 '#'·공백(전각/반각/탭/CR)·괄호(전각/반각)를 제거한 정규화 문자열."""
    return "".join(
        ch for ch in line
        if not ch.isspace() and ch not in _ANCHOR_STRIP_CHARS
    )


def _extract_session_section(
    pm_state_text: str,
    entry_re: re.Pattern[str] = _PM_SESSION_ANCHOR_RE,
) -> tuple[str, int, int] | None:
    """pm_state.md 에서 세션 식별 절 텍스트와 그 시작·끝 위치를 반환한다.

    앵커 탐색은 정확 str.find 이 아니라 정규화 부분일치다 — '##' 로 시작하는 h2 헤더 줄
    (### 이상 제외)을 라인 스캔해, '#'·공백·괄호를 제거한 정규화 문자열이 '세션식별' 을
    포함하는 줄을 절 시작 후보로 본다(공백/괄호/여백 변형 흡수). 매치 실패 시 None.

    후보가 여럿이면 **pm 세션 entry(`- **N차**`)를 가진 첫 절을 우선**하고, 전부 없으면
    첫 후보로 폴백한다 — '## 세션 식별 규칙' 같은 설명-절이 실제 window 절보다 앞에 있을
    때 빈 절을 오선택해 window 미갱신(fail-soft 스킵)·오염되는 것을 막는다
    reviewer should-fix).

    `entry_re` = "entry 보유 절" 판정에 쓰는 정규식(기본 `_PM_SESSION_ANCHOR_RE`·정상 anchor
    만). handoff write 경로는 기본값을 그대로 써 무변경이다. **정규화 도구**(normalize_session_
    anchors)만 `_ANY_SESSION_ANCHOR_RE`(정상+오형식)를 넘겨, entry 가 전부 오형식(`**N차차**`)인
    실 타깃 window 절도 entry-bearing 으로 인식하게 한다 — 그러지 않으면 설명-절로 폴백해
    정규화가 silent no-op 이 된다(codex must-fix).

    반환: (section_text, start_offset, end_offset) 또는 None (앵커 불일치).
    end_offset 는 다음 ## 또는 ### 헤더 직전 위치 (혹은 파일 끝).
    """
    def _section_bounds(m: re.Match) -> tuple[str, int, int]:
        # 앵커 줄 이후에서 다음 헤더(## 또는 ###)를 찾아 절 경계를 계산한다.
        after_anchor = pm_state_text[m.end():]
        next_header = re.search(r"^###? ", after_anchor, re.MULTILINE)
        end_offset = len(pm_state_text) if next_header is None else m.end() + next_header.start()
        return pm_state_text[m.start():end_offset], m.start(), end_offset

    candidates = [
        m for m in re.finditer(r"^##(?!#).*$", pm_state_text, re.MULTILINE)
        if _SESSION_ANCHOR_NORMALIZED in _normalize_h2_header(m.group(0))
    ]
    if not candidates:
        return None
    for m in candidates:  # entry 보유 절 우선 — 빈 설명-절 오선택 방지.
        section = _section_bounds(m)
        if entry_re.search(section[0]):
            return section
    return _section_bounds(candidates[0])  # 전부 entry 없음 → 첫 후보(종전 동작 보존).


def _find_pm_session_entries(section_text: str) -> list[re.Match]:
    """세션 식별 절에서 개별 pm 세션 entry 줄 (- **N차** ...) 의 match 목록을 반환한다.

    차수 순으로 정렬해 반환한다.
    """
    matches = list(_PM_SESSION_ANCHOR_RE.finditer(section_text))
    # 차수를 숫자로 변환해 정렬
    def _session_num(m: re.Match) -> int:
        return int(m.group(1).replace("차", ""))
    return sorted(matches, key=_session_num)


def _build_new_session_entry(
    session_num: int | str,
    date_str: str,
    wave_summary: str,
) -> str:
    """새 pm 세션 entry 줄을 빌드한다 (줄바꿈 포함).

    형식: "  - **N차** (YYYY-MM-DD · <wave_summary>): <wave_summary>."
    """
    return (
        f"  - **{_normalize_session_num(session_num)}차** ({date_str} · {wave_summary}): {wave_summary}.\n"
    )


def update_session_window(
    pm_state_text: str,
    session_num: int | str,
    date_str: str,
    wave_summary: str,
) -> str:
    """pm_state.md 의 세션 식별 절에 sliding window 를 적용한 새 텍스트를 반환한다.

    - 신규 세션 entry 추가
    - 기존 entry가 이미 3개일 때만 가장 오래된 세션 entry 제거 (3 차 sliding window)
    - "이전 차 (PM N차~M차)" 포인터 줄 갱신

    앵커 불일치 시 ValueError (추측 편집 금지).
    """
    result = _extract_session_section(pm_state_text)
    if result is None:
        raise ValueError(
            f"앵커 불일치: '{_SESSION_SECTION_ANCHOR}' 가 pm_state.md 에서 발견되지 않았다."
        )
    section_text, start_offset, end_offset = result

    existing_entries = _find_pm_session_entries(section_text)

    if len(existing_entries) == 0:
        # task 생성 시점 state는 완료 세션 0개가 정상이다. worktree_pool이 심은 명시 marker가
        # 있을 때만 첫 핸드오프를 1차 entry로 초기화한다. marker 없는 임의 변형은 
        # fail-loud해 추측 편집 금지 경계를 보존한다.
        if TASK_PM_STATE_EMPTY_MARKER not in section_text:
            raise ValueError(
                "앵커 불일치: 세션 식별 절에 기존 pm 세션 entry (- **N차** ...) 가 없다."
            )
        new_entry_line = _build_new_session_entry(session_num, date_str, wave_summary)
        new_section = section_text.replace(
            TASK_PM_STATE_EMPTY_MARKER,
            new_entry_line.rstrip("\n"),
            1,
        )
        return (
            pm_state_text[:start_offset]
            + new_section
            + pm_state_text[end_offset:]
        )

    # 멱등성 검사 — 이미 해당 session_num entry 가 존재하면 no-op 으로 early-return.
    # 동일 session_num 재실행 시 entry 중복 추가 + 오래된 entry 의 이중 제거를 방지.
    target_num = int(str(session_num).replace("차", ""))
    existing_nums = [int(m.group(1).replace("차", "")) for m in existing_entries]
    if target_num in existing_nums:
        return pm_state_text

    # 창이 아직 차지 않았으면 기존 entry를 제거하지 않고 신규 entry만 붙인다. task 첫 handoff가
    # marker→1차로 바뀐 직후 2차 handoff에서 1차를 제거하면 window가 영원히 1개로 고정되어
    # 연속성이 끊긴다. 1→2→3차까지 공존시키고, **기존 3개 + 신규 1개**가 되는 4차부터만
    # 최고령을 밀어낸다.
    if len(existing_entries) < SLIDING_WINDOW_SIZE:
        new_entry_line = _build_new_session_entry(session_num, date_str, wave_summary)
        last_entry = existing_entries[-1]
        insert_pos = last_entry.end()
        if insert_pos < len(section_text) and section_text[insert_pos] == "\n":
            insert_pos += 1
        new_section = (
            section_text[:insert_pos]
            + new_entry_line
            + section_text[insert_pos:]
        )

        # legacy/solo 템플릿은 첫 window entry와 같은 차수를 "이전 차" 포인터에도
        # 남긴 채 배포된 적이 있다. 아직 eviction 전이라도 새 entry를 추가한 뒤에는
        # 포인터가 현재 window와 겹치면 안 된다. 이 시점에는 창의 최소 차수보다 앞선
        # 세션이 없으므로, stale pointer 줄을 제거한다. 4차에서 첫 eviction이 발생하면
        # 아래의 기존 분기가 1차를 다시 이전 차 포인터로 만든다.
        new_window_min = min(existing_nums + [target_num])
        prev_pointer_match = re.search(
            r"^  - 이전 차 \(PM (.+?)\) = `.+?`[^\n]*\n?",
            new_section,
            re.MULTILINE,
        )
        if prev_pointer_match is not None:
            range_match = re.fullmatch(
                r"(\d+)차~(\d+)차", prev_pointer_match.group(1)
            )
            # 파싱할 수 있는 표준 범위만 손댄다. 비표준 포인터를 추측 편집하지 않는다.
            if range_match and int(range_match.group(2)) >= new_window_min:
                new_section = (
                    new_section[:prev_pointer_match.start()]
                    + new_section[prev_pointer_match.end():]
                )
        return (
            pm_state_text[:start_offset]
            + new_section
            + pm_state_text[end_offset:]
        )

    # 가장 오래된 entry (최소 차수) 를 제거 대상으로 선정
    oldest_entry = existing_entries[0]
    oldest_num = int(oldest_entry.group(1).replace("차", ""))

    # "이전 차 (PM N차~M차)" 포인터 줄 탐색
    prev_pointer_match = re.search(
        r"^  - 이전 차 \(PM (.+?)\) = `.+?`[^\n]*$",
        section_text,
        re.MULTILINE,
    )

    new_section = section_text

    # 1. 가장 오래된 entry 줄 제거
    # 줄 전체를 제거 (줄바꿈 포함)
    oldest_line_start = oldest_entry.start()
    oldest_line_end = oldest_entry.end()
    # 줄바꿈까지 포함
    if oldest_line_end < len(new_section) and new_section[oldest_line_end] == "\n":
        oldest_line_end += 1
    new_section = new_section[:oldest_line_start] + new_section[oldest_line_end:]

    # 2. 이전 차 포인터 줄 갱신 — 제거한 차수를 포함하도록 범위 확장
    if prev_pointer_match is not None:
        old_range_str = prev_pointer_match.group(1)  # 예: "11차~24차"
        # 기존 범위에서 끝 차수 파싱
        range_match = re.match(r"(\d+차)~(\d+차)", old_range_str)
        if range_match:
            old_end_str = range_match.group(2)  # 예: "24차"
            # 새 포인터: 범위는 그대로, 끝은 제거된 오래된 entry 차수로
            new_end_str = f"{oldest_num}차"
            new_range_str = f"{range_match.group(1)}~{new_end_str}"
        else:
            # 단순 케이스: 범위가 하나의 숫자인 경우
            new_range_str = f"{old_range_str}·{oldest_num}차"

        # 포인터 줄 치환 (이전 범위 → 새 범위)
        # 재탐색 필요 (section 이 변경됐으므로)
        new_pointer_match = re.search(
            r"^  - 이전 차 \(PM .+?\) = `.+?`[^\n]*$",
            new_section,
            re.MULTILINE,
        )
        if new_pointer_match is not None:
            old_pointer_line = new_pointer_match.group(0)
            # 괄호 전체 `(PM N차~M차)` 를 매치해 치환 — 괄호 없이 "PM .+? = " 를 쓰면
            # 닫힘 괄호까지 삼켜 `) ` 가 사라지는 버그가 생긴다.
            new_pointer_line = re.sub(
                r"\(PM .+?\)", f"(PM {new_range_str})", old_pointer_line
            )
            new_section = (
                new_section[:new_pointer_match.start()]
                + new_pointer_line
                + new_section[new_pointer_match.end():]
            )
    else:
        # 포인터 줄이 없는 경우 — 제거된 entry 가 있는 자리에 포인터 추가
        # "이전 차 (PM N차~N차) = log/current.md handoff entry 단일 진실." 형식으로 추가
        pointer_line = (
            f"  - 이전 차 (PM {oldest_num}차~{oldest_num}차) = "
            f"`log/current.md` handoff entry 단일 진실.\n"
        )
        # 섹션의 기존 entry 목록 마지막 위치 뒤에 추가
        new_section += pointer_line

    # 3. 신규 세션 entry 추가 — 기존 entry 목록의 마지막 줄 이후에 삽입
    new_entry_line = _build_new_session_entry(session_num, date_str, wave_summary)

    # 현재 entry 목록 마지막 위치 찾기 (재탐색)
    updated_entries = list(_PM_SESSION_ANCHOR_RE.finditer(new_section))
    if updated_entries:
        last_entry = max(updated_entries, key=lambda m: int(m.group(1).replace("차", "")))
        insert_pos = last_entry.end()
        # 줄바꿈 뒤에 삽입
        if insert_pos < len(new_section) and new_section[insert_pos] == "\n":
            insert_pos += 1
        new_section = new_section[:insert_pos] + new_entry_line + new_section[insert_pos:]
    else:
        # 기존 entry 가 모두 제거된 경우 (새 entry 만 있는 경우) — 포인터 줄 앞에 추가
        pointer_search = re.search(
            r"^  - 이전 차 ", new_section, re.MULTILINE
        )
        if pointer_search:
            new_section = (
                new_section[:pointer_search.start()]
                + new_entry_line
                + new_section[pointer_search.start():]
            )
        else:
            new_section += new_entry_line

    # 섹션을 pm_state.md 에 교체
    new_pm_state_text = (
        pm_state_text[:start_offset]
        + new_section
        + pm_state_text[end_offset:]
    )
    return new_pm_state_text


# ── task 세션 차수 자동 추론 ────────────────────────────────────────

def infer_next_session_num(pm_state_text: str) -> int | str:
    """pm_state.md 세션 식별 절에서 다음 PM 세션 차수를 추론한다.

    가장 높은 기존 차수 + 1 을 반환. entry 가 없으면 안전한 placeholder.
    (task 경로에서는 사람 입력 차수 대신 task state를 단일 진실로 쓴다.)
    """
    result = _extract_session_section(pm_state_text)
    if result is None:
        return TRIGGER_SESSION_PLACEHOLDER
    section_text, _, _ = result
    entries = _find_pm_session_entries(section_text)
    if not entries:
        if TASK_PM_STATE_EMPTY_MARKER in section_text:
            return 1
        return TRIGGER_SESSION_PLACEHOLDER
    highest = max(int(m.group(1).replace("차", "")) for m in entries)
    return highest + 1


_TASK_HANDOFF_HEADER_RE = re.compile(
    r"^## \[\d{4}-\d{2}-\d{2}\]\s+handoff\s*\|\s*PM\s+(?P<num>\d+)차"
    r"\s+\(task:(?P<task>[^)]+)\).*$",
    re.MULTILINE,
)


def infer_next_task_session_num(
    pm_state_text: str | None,
    log_text: str | None,
    task: str,
    *,
    task_released: bool = False,
) -> int | str:
    """task의 다음 차수를 state와 task-tagged log에서 함께 복구한다.

    state 세션 window의 최고차+1과 `log/current.md`의 동일 `(task:<name>)` handoff를 함께
    본다. 정상 handoff가 task 장부 pid를 0으로 release한 뒤 bootstrap 없이 재실행된 경우
    (`task_released=True`)에는 마지막 log 차수를 그대로 반환해 같은 세션 갱신 모드로 들어간다.
    release가 log보다 먼저 영속되므로 log append 뒤 state 기록 전에 중단된 반쪽 상태도 같은
    경로로 복구된다. 다음 정상 세션은 bootstrap의 `bind_task`가 pid를 다시 채우므로 N+1이다.
    따라서 `task_released=False`인 bootstrap/read 경로는 state가 뒤처져도 log N을 새 세션
    N+1로 해석한다. 둘 다 미해소면 state 추론의 placeholder 규격을 보존한다.

    pm_handoff write 경로와 pm_bootstrap read 경로가 함께 쓰는 task 차수 복구 단일 규칙이다.
    """
    state_next = (
        infer_next_session_num(pm_state_text)
        if pm_state_text is not None
        else TRIGGER_SESSION_PLACEHOLDER
    )
    log_nums = [
        int(match.group("num"))
        for match in _TASK_HANDOFF_HEADER_RE.finditer(log_text or "")
        if match.group("task") == task
    ]
    candidates = [value for value in (state_next,) if isinstance(value, int)]
    if log_nums:
        log_highest = max(log_nums)
        # 정상 종료 또는 log 전 intent 기록(pid=0) 뒤 bootstrap을 다시 거치지 않은 호출은
        # 같은 세션 재실행이다. bootstrap/read 경로(task_released=False)는 언제나 log N의
        # 다음 차수 N+1로 전진해야 한다.
        if task_released:
            return log_highest
        candidates.append(log_highest + 1)
    if candidates:
        return max(candidates)
    return state_next


# ── 오형식 차수 정규화 (`**N차차+**` → `**N차**`·멱등·비파괴) ─────────

def normalize_session_anchors(pm_state_text: str) -> str:
    """세션 식별 절의 오형식 `**N차차+**`(2회 이상 반복 차) 를 `**N차**` 로
    멱등·비파괴 정규화한 새 텍스트를 반환한다.

    - 세션 식별 절만 손댄다 — 절 밖·정상 단일 '차' 토큰은 무변경. 절 부재면 원문 그대로.
    - 멱등: 정규화 후 토큰은 `**N차**` 라 재실행해도 `차{2,}` 미매치 → 결과 동일.
    - 비파괴: 오형식 bold 토큰의 잉여 '차' 만 제거하고 그 밖의 문자(날짜·요약·포인터 줄)는
      한 글자도 건드리지 않는다.

    절 선택은 `_ANY_SESSION_ANCHOR_RE`(정상+오형식)로 한다 — window 절의 entry 가 전부
    오형식(`**N차차**`·well-formed 0)인 실 타깃(finance)에서도 그 절을 entry-bearing 으로
    인식하게 해, 앞선 설명 절(`## 세션 식별 규칙`)로 폴백해 조용히 no-op 되는 함정을 막는다
    (codex must-fix). 정상 anchor 만 보는 handoff write 경로는 무영향.

    변경이 없으면 입력 텍스트를 그대로(동일 객체) 반환한다 — 호출부가 no-op 을 값 비교로 감지.
    """
    result = _extract_session_section(pm_state_text, entry_re=_ANY_SESSION_ANCHOR_RE)
    if result is None:
        return pm_state_text
    section_text, start_offset, end_offset = result
    fixed_section = _MALFORMED_SESSION_ANCHOR_RE.sub(r"**\1차**", section_text)
    if fixed_section == section_text:
        return pm_state_text
    return pm_state_text[:start_offset] + fixed_section + pm_state_text[end_offset:]


# ── pm_playbook.md 인계 프롬프트 추출 ────────────────────────────────────────

_HANDOFF_PROMPT_SECTION_ANCHOR = "## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)"

# 코드블록 추출
_CODE_BLOCK_RE = re.compile(r"```\n(.+?)```", re.DOTALL)


def extract_handoff_prompt_template(pm_playbook_text: str) -> str | None:
    """pm_playbook.md 에서 인계 프롬프트 템플릿 코드블록을 추출한다.

    반환: 코드블록 내용 문자열 또는 None (앵커 불일치).
    """
    anchor_idx = pm_playbook_text.find(_HANDOFF_PROMPT_SECTION_ANCHOR)
    if anchor_idx == -1:
        return None

    # 섹션 이후에서 다음 ## 헤더 전까지
    after_anchor = pm_playbook_text[anchor_idx + len(_HANDOFF_PROMPT_SECTION_ANCHOR):]
    next_header = re.search(r"^## ", after_anchor, re.MULTILINE)
    if next_header:
        section_text = after_anchor[:next_header.start()]
    else:
        section_text = after_anchor

    match = _CODE_BLOCK_RE.search(section_text)
    if match is None:
        return None
    return match.group(1)


# canonical 기본값은 외부 테스트/호출자의 fixture seam으로 유지하되, 소비는 아래 regex만 쓴다.
_BARE_BOOTSTRAP_TRIGGER = "/pm-bootstrap"

_runtime_skill_entry_prefix = identity_args._runtime_skill_entry_prefix
_runtime_skill_entry = identity_args._runtime_skill_entry

# bare 스킬 트리거 — canonical(`/`)과 렌더 사본(`$`) 및 prefix 없는 구 사본을 모두 받는다.
# 경로(`skills/pm-bootstrap`)·접미 변형(`pm-bootstrap-extra`)은 호출 토큰이 아니므로 제외한다.
_BARE_BOOTSTRAP_TRIGGER_RE = re.compile(
    r"(?<![A-Za-z0-9_.>/\-])(?P<trigger>(?:[/$])?pm-bootstrap)"
    r"(?![A-Za-z0-9_.>/\-])"
)

_MULTI_HARNESS_BOOTSTRAP_RE = re.compile(
    r"`(?P<first_prefix>[/$])pm-bootstrap`\((?P<first_labels>[^)]+)\)"
    r"(?: / `(?P<second_prefix>[/$])pm-bootstrap`\((?P<second_labels>[^)]+)\))"
)


def _select_runtime_bootstrap_notation(template: str) -> str:
    """공유 wiki의 다중-harness 병기를 현재 하네스 표기 하나로 축약한다."""
    prefix = _runtime_skill_entry_prefix()

    def select(match: re.Match[str]) -> str:
        for key in ("first", "second"):
            if match.group(f"{key}_prefix") == prefix:
                return f"{prefix}pm-bootstrap"
        return f"{prefix}pm-bootstrap"

    selected = _MULTI_HARNESS_BOOTSTRAP_RE.sub(select, template)
    return _BARE_BOOTSTRAP_TRIGGER_RE.sub(
        lambda _match: f"{prefix}pm-bootstrap",
        selected,
    )


def _parse_worktree_slot(worktree_slot: str | None) -> tuple[str, int] | None:
    """`work/<repo>_<N>` 슬롯을 `(repo, N)` 로 파싱한다 — 비정형이면 None.

    파싱: leading `work/` 제거 후 `<repo>_<N>` 을 rsplit("_", 1) → repo·N. N 은 정수여야
    하고 repo 는 비어 있지 않아야 한다. None·prefix 불일치·underscore 부재·N 비정수는 모두
    None 반환(호출부가 bare 폴백·fail-soft·현행 유지).
    """
    if not worktree_slot or not worktree_slot.startswith("work/"):
        return None
    rest = worktree_slot[len("work/"):]
    if "_" not in rest:
        return None
    repo, n_str = rest.rsplit("_", 1)
    if not repo or not n_str.isdigit():
        return None
    return repo, int(n_str)


def _inject_slot_into_template(template: str, worktree_slot: str | None) -> str:
    """복사 블록 템플릿의 bare ``pm-bootstrap`` 호출을 slot-qualified 로 치환한다.

    `worktree_slot` 이 `work/<repo>_<N>` 형식이면 템플릿 내 모든 `/pm-bootstrap` 을
    `/pm-bootstrap <repo> --slot <N>` 로 치환한다 — 멀티-PM 다음 세션이 슬롯 disambiguator
    없이 fail-loud 하는 갭 보완. 여러 등장 전부 같은 커맨드라 모두 치환 OK.
    None·비정형(파싱 실패)이면 bare 유지(fail-soft·현행).
    """
    parsed = _parse_worktree_slot(worktree_slot)
    if parsed is None:
        return template
    repo, n = parsed
    return _BARE_BOOTSTRAP_TRIGGER_RE.sub(
        lambda match: f"{match.group('trigger')} {repo} --slot {n}",
        template,
    )


def _inject_task_into_template(template: str, task: str) -> str:
    """복사 블록 템플릿의 bare ``pm-bootstrap`` 호출을 task-qualified 로 치환한다.

    task 모드 핸드오프면 트리거를 `/pm-bootstrap --task <task>` 로 치환한다 — 다음 세션이
    task 앵커로 재개(차수 추론·per-task pm_state 포인터·clean resume 실링크)하게 연속성을
    보존한다. task가 진입 정체성의 단일 축이고
    보유 슬롯 집합을 자동 수령하므로 트리거에 슬롯을 열거하지 않는다
    (트리거 = 재개 명령 1:1).
    task 이름은 호출부(`build_handoff_prompt_output`)가 `validate_task_name_engine` 로 삽입 전
    검증하므로 단일 안전 토큰(공백·괄호·path 문자·예약패턴 불가)이 보장돼 quoting 이 불필요하다.
    슬롯/솔로 모드는 이 경로를 타지 않는다(호출부에서 task None).
    """
    return _BARE_BOOTSTRAP_TRIGGER_RE.sub(
        lambda match: f"{match.group('trigger')} --task {task}",
        template,
    )


def build_handoff_prompt_output(
    pm_playbook_text: str,
    session_num: int | str,
    wave_summary: str,
    date_str: str,
    worktree_slot: str | None = None,
    task: str | None = None,
) -> str:
    """인계 프롬프트 stdout 출력 문자열을 빌드한다.

    pm_playbook.md §부트스트랩 프롬프트 템플릿(역할 framing + `/pm-bootstrap` 트리거)을 그대로
    포함한다. 부트스트랩이 인계 본문(박제 entries·메타 학습·pending intent·회귀/incident)을
    log handoff entry 에서 자동 dump 하므로, 프롬프트는 더 이상 `<핵심 인계 사항>` 손-채움을
    싣지 않는다

    `task`(기본 None)가 set 이면 task 모드 핸드오프 — 트리거를 `/pm-bootstrap --task <task>` 로
    주입한다. task 는 slot 보다 우선(직교 앵커)이라 슬롯 열거 없이 task-only 로
    출력하고, 다음 세션은 진입 시 보유 슬롯 집합을 자동 수령한다. task None 일
    때만 `worktree_slot`(`work/<repo>_<N>`·기본 None)이 set 이면 복사 블록의 bare `/pm-bootstrap` 을
    `/pm-bootstrap <repo> --slot <N>` 로 주입해, 멀티-PM 다음 세션이 슬롯을 정확히 바인딩하게
    한다. 둘 다 None·비정형이면 bare 유지(하위호환·fail-soft). pm_playbook.md 템플릿 파일은
    건드리지 않는다 — 치환은 추출된 텍스트 안에서만 contained.
    """
    template = extract_handoff_prompt_template(pm_playbook_text)
    if template is None:
        return (
            "[경고] pm_playbook.md 에서 인계 프롬프트 템플릿을 찾지 못했다. "
            f"앵커: '{_HANDOFF_PROMPT_SECTION_ANCHOR}'\n"
            "pm_playbook.md §'다음 PM 세션 부트스트랩 프롬프트 (템플릿)' 을 직접 복사하라."
        )
    template = _select_runtime_bootstrap_notation(template)
    if task is not None:
        # 삽입 전 공유 validator 로 fail-loud — build_handoff_prompt_output() 직접 호출도 우회 못 하게
        # 트리거 경계에서 닫는다. run() 은 진입에서 이미 검증하므로 멱등 재검증.
        validate_task_name_engine(task)
        template = _inject_task_into_template(template, task)
    else:
        template = _inject_slot_into_template(template, worktree_slot)

    header = (
        f"=== 인계 프롬프트 (PM {session_num}차 → 다음 PM 세션) ===\n"
        f"--- 아래를 복사해 다음 PM 세션에 붙여넣기 ---\n"
        f"(인계 본문은 {_runtime_skill_entry('pm-bootstrap')} 이 log entry 에서 자동 dump — "
        f"프롬프트는 트리거만)\n\n"
    )
    footer = (
        f"\n--- 복사 끝 ---\n"
        f"[참고] 날짜: {date_str}, wave summary: {wave_summary}\n"
    )
    return header + "```\n" + template + "```" + footer


# ── pytest 출력 파서 ─────────────────────────────────────────────────────────
# 요약행 탐색은 board.py 의 공용 seam(`_pytest_summary_line` — 끝에서-탐색)이 소유한다.
# 출력 전체를 `re.search` 하면 캡처된 로그의 `3 passed`/`1 failed` 를 요약으로 먼저 만나
# 인계 게이트를 뒤집거나 인계문에 엉뚱한 수를 싣는다(자기 사본 금지).

# 이 도구가 실제로 호출하는 seam 함수 — **전부** 있어야 쓴다. 하나만 확인하고 통과시키면
# 부분 동기된 혼합 사본에서 나머지 호출이 AttributeError 로 터진다(진단 없는 죽음).
_PYTEST_SEAM_FUNCTIONS = ("_pytest_summary_line", "_pytest_outcome_count",
                          "_pytest_summary_tail")
# seam 부재 진단 — 뒤따르는 "회귀 red" 중단이 원인을 테스트 실패로 오도하지 않도록 구제책을 낸다.
_PYTEST_SEAM_MISSING = ("pytest 파서: board.py 사본에 요약행 파서 seam 부재 — "
                        "pm_update 로 엔진 전체를 재동기하라.")


def _pytest_summary_seam():
    """pytest 요약행 파서 공용 seam(board.py) — 부재/불완전 사본이면 None (fail-soft).

    seam 이 없으면 red 판정·요약 미해소로 흘린다 — 로컬 첫-매칭 폴백을 두면 엔진 사본이
    불완전할 때만 조용히 오판이 되살아나 진단이 더 어려워진다(인계는 fail-closed).
    대신 중단 **바로 앞줄**에 구제책을 stderr 로 실어 red 사유를 잘못 읽지 않게 한다.
    """
    board = _load_board()
    if board is not None and all(
            hasattr(board, name) for name in _PYTEST_SEAM_FUNCTIONS):
        return board
    print(_PYTEST_SEAM_MISSING, file=sys.stderr)
    return None


def is_pytest_green(output: str, returncode: int = 0) -> bool:
    """pytest -q 출력이 green (passed 존재, failed 없음) 이면 True.

    passed/failed 는 **요약행 안에서만** 읽는다 — 캡처 로그의 `1 failed` 로 green 회귀를
    red 로 뒤집지 않는다.
    """
    if returncode != 0:
        return False
    seam = _pytest_summary_seam()
    line = seam._pytest_summary_line(output) if seam is not None else None
    if line is None:
        return False
    if seam._pytest_outcome_count(line, "failed") is not None:
        return False
    return seam._pytest_outcome_count(line, "passed") is not None


def parse_pytest_summary(output: str) -> str:
    """pytest -q 출력에서 요약 라인을 추출한다 (`N passed` 부터 줄 끝까지).

    요약행/파서 seam 이 없으면 현행대로 출력 꼬리 200자, 출력이 비면 빈 문자열.
    """
    seam = _pytest_summary_seam()
    summary = seam._pytest_summary_tail(output) if seam is not None else None
    if summary is not None:
        return summary
    return output.strip()[-200:] if output.strip() else ""


# ── 출하 테스트 출하 변경 발동 ──────────────────────────────

# baseline 기준 ref 후보 — push 대상 기준(미push diff). 첫 해소 가능한 것을 쓴다.
#   @{upstream}: 현 브랜치의 추적 upstream (가장 정확한 "미push" 경계).
#   origin/main: upstream 미설정 시 폴백.
_PENDING_PUSH_BASELINE_REFS = ("@{upstream}", "origin/main")


def _path_is_shipping(path: str) -> bool:
    """경로가 SHIPPING_GLOBS 중 하나라도 매칭하면 True (fnmatch 글롭)."""
    return any(fnmatch.fnmatch(path, glob) for glob in SHIPPING_GLOBS)


def _resolve_pending_baseline(
    worktree: str,
    git_runner: Callable[[list[str]], tuple[int, str]],
) -> str | None:
    """미push diff 기준 baseline ref 를 해소한다. 해소 불가 시 None (ambiguous).

    `git -C <worktree> rev-parse --verify <ref>` 로 후보 ref(@{upstream}·origin/main)를
    순서대로 시도해 첫 성공 ref 문자열을 반환한다. detached/upstream 미설정/원격부재면
    모두 비-0 → None → 호출부가 has_unknown(ambiguous→surface) 처리. fail-soft.
    """
    for ref in _PENDING_PUSH_BASELINE_REFS:
        rc, _ = git_runner(["-C", worktree, "rev-parse", "--verify", "--quiet", ref])
        if rc == 0:
            return ref
    return None


def _uncommitted_and_untracked_paths(
    worktree: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> list[str] | None:
    """작업트리 미커밋(staged+unstaged tracked) + untracked 신규파일 경로를 union 반환.

    push 시 확실히 올라갈 변경(커밋만 하면 됨). 두 git 호출을 합친다:
      - `git -C <wt> diff --name-only --ignore-submodules=none HEAD`
                                                         → staged+unstaged tracked 변경 + gitlink drift
      - `git -C <wt> ls-files --others --exclude-standard` → untracked 신규파일(.gitignore 제외)
    tracked 축의 `--ignore-submodules=none` 은 `.gitmodules`/로컬 config 의 `ignore = all` 을
    **이 판정에 한해서만** override 한다. 인스턴스 소유 설정은 건드리지 않으면서 submodule HEAD 와
    상위 pin 이 다른 gitlink 를 놓치지 않는다.
    둘 중 하나라도 비-0 종료면 작업트리 상태 불명 → None (호출부가 ambiguous 처리).
    runner DI seam 경유(hermetic). 예외는 호출부에서 흡수.
    """
    rc_diff, out_diff = runner(
        ["-C", worktree, "diff", "--name-only", "--ignore-submodules=none", "HEAD"]
    )
    if rc_diff != 0:
        return None
    rc_others, out_others = runner(
        ["-C", worktree, "ls-files", "--others", "--exclude-standard"]
    )
    if rc_others != 0:
        return None
    paths: list[str] = []
    for out in (out_diff, out_others):
        paths.extend(line.strip() for line in out.splitlines() if line.strip())
    return paths


def _shipping_paths_in_pending_push(
    worktree: str,
    *,
    git_runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> tuple[list[str], bool]:
    """"지금 push 하면 올라갈 변경" ∩ SHIPPING_GLOBS 를 해소한다 (비차단 surface).

    pm_handoff [7/7] 체크리스트는 핸드오프 *후* `git commit` 을 안내한다 — 정상 핸드오프
    시점엔 출하 변경이 대개 **working tree(staged/unstaged·미커밋·untracked)** 에 있다.
    따라서 커밋된-미push 만 보면(diff <baseline>..HEAD) 정상 핸드오프 시 게이트가 발동하지
    않는다(must-fix). "지금 push 하면 올라갈 변경 전체"를 union 한다:
      - 커밋된 미push: `git -C <wt> diff --name-only <baseline>..HEAD` (baseline 해소된 경우만)
      - 작업트리 vs HEAD(staged+unstaged tracked·gitlink drift):
        `git -C <wt> diff --name-only --ignore-submodules=none HEAD`
      - untracked 신규파일: `git -C <wt> ls-files --others --exclude-standard`
    이 union ∩ SHIPPING_GLOBS 가 `shipping_hits`.

    ambiguous 정련: uncommitted/untracked 출하 hit 이 있으면 **baseline 해소 여부와 무관하게
    발동**(그 변경은 확실히 올라간다). baseline 해소불가(또는 커밋된-미push diff 실패)
    **그리고** 출하 hit 이 전혀 없을 때만 has_unknown=True(커밋된-미push 출하분을 못 봐서
    불명). diff/ls-files 명령 실패·예외는 fail-soft(has_unknown=True) — silent skip 금지
    (false-skip = 미검증 출하 위험 > false-fire 낭비).

    반환: (shipping_hits, has_unknown).
      - shipping_hits 비어있지 않음 → 발동.
      - shipping_hits 비어있고 has_unknown False → 명확한 비-출하(또는 push 없음) → skip.
      - shipping_hits 비어있고 has_unknown True → ambiguous (호출부 surface).

    fail-soft: git 미설치·worktree 부재 등 예외는 has_unknown=True 로 흡수(크래시 금지).
    git_runner DI seam — hermetic 테스트는 결정론 stub 주입. 모든 git 호출은 이 seam 경유.
    """
    runner = git_runner if git_runner is not None else _module_run_git

    # 1. 확실히 올라갈 변경 — uncommitted(작업트리 vs HEAD)·untracked 신규파일.
    #    baseline 해소 여부와 무관하게 진실(이 변경은 push 시 반드시 올라간다).
    try:
        uncommitted_paths = _uncommitted_and_untracked_paths(worktree, runner)
    except Exception:  # noqa: BLE001 — fail-soft: git 예외는 ambiguous 로 흡수.
        return [], True
    if uncommitted_paths is None:
        # diff HEAD/ls-files 명령 실패 — 작업트리 상태를 모른다 → ambiguous.
        return [], True

    # 2. 커밋된 미push 변경 — baseline 해소된 경우만(detached/upstream 미설정/원격부재면 못 봄).
    committed_paths: list[str] = []
    committed_unknown = False
    try:
        baseline = _resolve_pending_baseline(worktree, runner)
        if baseline is None:
            # 커밋된-미push 경계를 모른다 → 그 부분만 불명(uncommitted 는 이미 확보).
            committed_unknown = True
        else:
            rc, out = runner(
                ["-C", worktree, "diff", "--name-only", f"{baseline}..HEAD"]
            )
            if rc != 0:
                # diff 자체 실패 — 커밋된-미push 가 무엇인지 모른다 → 그 부분만 불명.
                committed_unknown = True
            else:
                committed_paths = [
                    line.strip() for line in out.splitlines() if line.strip()
                ]
    except Exception:  # noqa: BLE001 — fail-soft: git 예외는 커밋된-미push 불명으로 흡수.
        committed_unknown = True

    all_paths = set(uncommitted_paths) | set(committed_paths)
    shipping_hits = sorted(p for p in all_paths if _path_is_shipping(p))

    if shipping_hits:
        # uncommitted/untracked·또는 커밋된-미push 에서 확실한 출하 hit → 발동.
        # 발동할 변경이 확정됐으므로 baseline 해소불가여도 ambiguous 아님.
        return shipping_hits, False
    # 출하 hit 이 전혀 없을 때만 — 커밋된-미push 출하분을 못 봤다면(committed_unknown)
    # ambiguous(surface), 아니면 명확한 비-출하(skip).
    return [], committed_unknown


def _module_run_git(args: list[str]) -> tuple[int, str]:
    """모듈-레벨 git 실행 (DI 미주입 시 기본). (returncode, stdout+stderr)."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout + result.stderr


# ── dirty-tree 게이트 ([0/7]·조기 중단) ───────────────────────────────────────
#
# 불변식: **핸드오프 시작 시점의 실행 앵커 트리는 clean 이다(gitignored 제외).** 세션 관례상
# 티켓 산출물은 wave-finish 에서 티켓별로 커밋되고 핸드오프 커밋은 부기(log·pm_state)만 담으므로,
# 시작 시점의 dirty 는 전부 "부기 누락" 신호다. 실측 사례: 세션 산출 11파일이 미커밋인 채 핸드오프가
# 완결·커밋·push 됐고 다음 세션 부트스트랩이 뒤늦게 발견했다 — [6/7] git status dump 가 그 사실을
# *보여주기만* 하고 차단하지 않았기 때문이다.
#
# 그래서 판정을 **어떤 mutation 보다 앞**([2/7] log append·task state 보장·task pid=0 기록 앞)으로
# 끌어올려 조기 중단으로 승격한다. [6/7] 시점 차단은 log entry 가 이미 append 된 뒤라 재실행이
# 중복 entry 를 만든다(위임 부기 게이트에서 확인된 순서 문제와 같은 클래스·같은 해법). 회귀
# ([1/7])보다도 앞에 두는 이유는 비용이다 — 판정은 git 조회 몇 번인데 회귀는 수 분이다.
# 자기-산출물 allowlist 는 필요 없다 — 시점이 allowlist 를 대체한다(log/current.md·pm_state 는
# 게이트 통과 *후에* 써진다).


def _unborn_head_dirty_paths(
    tree: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> list[str] | None:
    """커밋 0(unborn HEAD) 트리의 미커밋 경로 전량 — 조회 실패면 None.

    unborn 에서도 동작하는 두 축을 union 한다. **둘 다 필요하다**:
      - `git -C <tree> diff --cached --name-only` → index 에 올라간 것(staged). `git add` 를
        마친 트리는 이 축만 답한다.
      - `git -C <tree> ls-files --others --exclude-standard` → 아직 add 안 한 것(untracked).
    untracked 만 보면 **전량 staged 인 커밋 0 저장소가 clean 으로 오판**된다(그 트리야말로
    아무것도 커밋되지 않은 상태다). gitignored 는 `--exclude-standard` 가 뺀다 — `--cached` 는
    이미 index 에 있는 것만 보므로 ignore 규칙과 무관하다.
    """
    rc_staged, out_staged = runner(["-C", tree, "diff", "--cached", "--name-only"])
    if rc_staged != 0:
        return None
    rc_untracked, out_untracked = runner(
        ["-C", tree, "ls-files", "--others", "--exclude-standard"]
    )
    if rc_untracked != 0:
        return None
    paths: list[str] = []
    for out in (out_staged, out_untracked):
        paths.extend(line.strip() for line in out.splitlines() if line.strip())
    return list(dict.fromkeys(paths))


def _dirty_paths_in_tree(
    tree: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> list[str] | None:
    """트리의 미커밋(tracked modified) ∪ untracked-unignored 경로 — 판정 불가면 None.

    정상 경로의 판정 축은 [1b/7] 출하 surface 와 **같은 seam**(`_uncommitted_and_untracked_paths`):
      - `git -C <tree> diff --name-only --ignore-submodules=none HEAD`
                                                            → tracked 변경 + gitlink drift
      - `git -C <tree> ls-files --others --exclude-standard` → untracked(gitignored 제외)
    `--exclude-standard` 가 "clean 제외 gitignored" 판정 기준을 그대로 만족한다.

    그 seam 이 접어버리는 실패 하나를 여기서 정련한다 — **unborn HEAD**(커밋 0). `diff HEAD` 는
    비교 ref 가 없어 실패하지만 그 트리는 "전량 미커밋"이라 게이트가 가장 잡아야 할 상태다.
    커밋 유무를 `rev-parse --verify HEAD` 로 확인해, 커밋이 없으면 staged ∪ untracked 로 판정한다
    (`_unborn_head_dirty_paths` — 전량 staged 인 트리를 clean 으로 오판하지 않는다).
    커밋이 있는데 diff 가 실패했거나(git 이상) unborn 조회까지 실패(비-git 트리·git 부재)면 None —
    호출부가 비차단 경고로 처리한다(판정 못 한 것을 dirty 로 단정해 정상 핸드오프를 막지 않는다).
    """
    try:
        paths = _uncommitted_and_untracked_paths(tree, runner)
        if paths is not None:
            return paths
        rc_head, _out = runner(["-C", tree, "rev-parse", "--verify", "--quiet", "HEAD"])
        if rc_head == 0:
            return None      # 커밋은 있는데 tracked diff 실패 — 판정 불가.
        return _unborn_head_dirty_paths(tree, runner)
    except Exception:  # noqa: BLE001 — 판정 실패는 크래시가 아니라 '불명'(호출부가 surface).
        return None


def _submodule_paths_in_tree(
    tree: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> list[str] | None:
    """등록된 재귀 submodule 경로(superproject 상대)를 반환 — 열거 불가면 None.

    `git -C <tree> submodule status --recursive` 하나를 권위로 쓰고, 출력 해석은 동적 로드 seam
    `_load_worktree_pool` 로 공용 `_parse_submodule_entries`(flag+공백 경로 보존)를 재사용한다.
    flag `-` 는 미초기화라 working tree 가 없으므로 제외한다. 부재 경로에서 `git -C` 를 돌리면
    상위 repo 로 올라가 상위 dirty 목록을 중복 반환할 수 있기 때문이다. 빈 출력은 등록된
    submodule 없음(`[]`)이다. git/파서 로드 실패·해석 불가능한 비어있지 않은 출력은 None 으로
    흡수해 호출부가 **비차단 경고**로 surface 한다 — 판정 못 한 것을 dirty 로 단정하지 않는다.
    """
    try:
        rc, output = runner(["-C", tree, "submodule", "status", "--recursive"])
    except Exception:  # noqa: BLE001 — git 부재 등은 판정 불가로 강등.
        return None
    if rc != 0:
        return None

    wp = _load_worktree_pool()
    parser = getattr(wp, "_parse_submodule_entries", None) if wp is not None else None
    if not callable(parser):
        return None
    try:
        entries = parser(output)
    except Exception:  # noqa: BLE001 — 공용 파서/동적 모듈 이상도 판정 불가로 강등.
        return None
    if output.strip() and not entries:
        return None
    paths = [path for flag, path in entries if flag != "-"]
    return list(dict.fromkeys(paths))


def flatten_ack_reason(reason: str) -> str:
    """ack 사유를 **단일행**으로 평탄화한다 — 개행/탭/연속 공백을 단일 공백으로.

    사유는 handoff entry 의 한 줄로 박히므로, CR/LF 가 살아 있으면 사유 문자열 하나로 가짜 entry
    줄(심지어 `## [날짜] handoff …` 헤더)을 위조할 수 있다. 값을 거부하는 대신 무해화해 CLI·엔진
    직접 호출 어느 경로로 들어와도 단일행 보장이 깨지지 않게 한다(단일 choke).
    """
    return " ".join(reason.split())


def _format_dirty_paths(
    paths: list[str],
    limit: int = DIRTY_GATE_LIST_LIMIT,
) -> list[str]:
    """차단/경고 메시지의 경로 열거 줄 목록 — limit 초과분은 '… 외 N건' 으로 접는다."""
    ordered = sorted(paths)
    lines = [f"    {path}" for path in ordered[:limit]]
    if len(ordered) > limit:
        lines.append(f"    … 외 {len(ordered) - limit}건")
    return lines


def build_dirty_ack_note(reason: str, dirty_count: int, *, auto: bool) -> str:
    """handoff entry 에 박제할 dirty-tree ack 사유 문장을 만든다 (**단일행 보장**).

    `auto`(사용자 명시 핸드오프 호출부 호환 신호)면 별도 dirty 사유가 없으므로 그 신호로
    차단을 강등했다는 사실 자체를 박는다. 명시 `--ack-dirty` 면 사람이 준 사유를 평탄화해
    그대로 싣고 override 수단을 병기한다. 둘 다 잔존 건수를 함께 남겨 다음 세션이 "몇 개를
    못 보고 넘어왔나"를 log 만 읽고 안다.
    """
    if auto:
        return (
            "사용자 명시 핸드오프 호환 신호 — "
            f"dirty {dirty_count}건 잔존 (`--auto-trigger`·차단하지 않음)."
        )
    return f"{flatten_ack_reason(reason)} (미커밋 {dirty_count}건 — `--ack-dirty` override)."


# ── slot 대시보드 (수정형) ─────────────────────────────────────
# multi-PM 슬롯 간 *가벼운* 공유 — 슬롯당 고정 섹션 1개(헤딩 키 = 세션 정체성 `## <repo>_<N>`)를
# 핸드오프가 **자기 섹션만 overwrite** 한다(append 아님·타 슬롯 섹션 byte 불변). 저장 위치 =
# `wiki/log/dashboard.md`(log/current.md 와 같은 공유 채널·새 git 기계 0). 히스토리는
# log/current.md 몫 — 대시보드는 현재-상태 스냅샷(3~5줄 상한·중복 서술 금지). read-modify-write 는
# **파일락(`_dashboard_lock`)으로 직렬화**한다 — cross-slot 동시 핸드오프의 lost update 차단
# (MF-2·codex"파일락 불요" 정정). 섹션 경계는 헤딩 토큰(`## `) 기반이라 타 슬롯 섹션은
# offset splice 로 byte 불변(테스트로 못박는다).


def _dashboard_file() -> Path:
    """slot 대시보드 경로 (`wiki/log/dashboard.md`·*호출 시점* REPO 추종·hermetic)."""
    return REPO / ".project_manager" / "wiki" / "log" / "dashboard.md"


def _dashboard_lock_file() -> Path:
    """대시보드 read-modify-write 직렬화 락 파일 (`.local/dashboard.lock`·*호출 시점* REPO 추종)."""
    return REPO / ".project_manager" / ".local" / "dashboard.lock"


# ── 대시보드 파일락 (경로 규약만 소유·플랫폼 분기는 공용 `file_lock` seam) ────────────
# 대시보드는 슬롯-공유 파일이라 read-modify-write 를 직렬화하지 않으면 **두 다른 슬롯이 동시
# 핸드오프**할 때 lost update 가 난다(둘 다 같은 이전 파일을 읽고 나중 write 가 상대 슬롯 섹션
# 갱신을 덮음"타 슬롯 byte 불변/현재 스냅샷" 위반). "슬롯=단일 세션" 은 *같은 슬롯*
# 동시성만 배제하지 cross-slot 을 못 막는다 — 그래서 락이 필요하다(codex MF-2).


@contextlib.contextmanager
def _dashboard_lock() -> Iterator[None]:
    """대시보드 파일 read-modify-write 를 직렬화하는 OS 파일락 컨텍스트매니저 (MF-2).

    `.project_manager/.local/dashboard.lock` 에 배타 OS 락. 프로세스가 죽으면 OS 가 자동
    해제(stale-lock 없음·worktree_pool `_lease_lock` 동형). **재진입 금지** — 대시보드의 모든
    read→upsert→write 가 이 한 구간 안에서 일어나 cross-slot lost update 를 막는다.

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 `file_lock` seam 이 소유하고
    (모듈 상단 import 시점 바인딩) 이 도구는 *어느 파일에 거는지*(경로 규약)만 정한다.
    """
    with file_lock.exclusive_file_lock(_dashboard_lock_file()):
        yield


# 자기 섹션 본문 최대 줄 수 (헤딩 제외) — 3~5줄 상한(차수·wave·claimed·다음). 초과분 truncate.
DASHBOARD_SECTION_MAX_LINES = 5

# 본문 한 줄 char cap — "가벼운 대시보드" 방어(거대 단일 줄 유입 차단).
DASHBOARD_LINE_MAX_CHARS = 200

# lazy 생성 시 파일 머리말 (대시보드 부재 → 첫 write 에서 헤더 + 자기 섹션).
_DASHBOARD_HEADER = (
    "# slot 대시보드 (수정형·현재-상태 스냅샷·타 슬롯 byte 불변)\n"
)

# 섹션 헤딩 grammar — `## <key>` (두 해시 + 공백). preamble(`# …`)·하위 헤딩(`### …`)은
# 두 번째 문자가 공백이 아니라 미매치. write(upsert)·read(parse) 가 같은 경계를 공유한다.
_DASHBOARD_HEADING_RE = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)

# ticket frontmatter 라인 파서 (claimed 티켓 스캔용·YAML 미의존·board 미import·touches 격리).
_TICKET_FRONT_ID_RE = re.compile(r"^id:\s*(\S+)", re.MULTILINE)
_TICKET_FRONT_STATUS_RE = re.compile(r"^status:\s*(\S+)", re.MULTILINE)
_TICKET_FRONT_CLAIMED_BY_RE = re.compile(r"^claimed_by:\s*(\S+)", re.MULTILINE)


def _strip_yaml_scalar(value: str) -> str:
    """frontmatter 스칼라 값에서 surrounding 따옴표(`"`·`'`)를 벗긴다 (YAML 따옴표 값 방어)."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "'"):
        return v[1:-1]
    return v


def _flatten_dashboard_value(value: object) -> str:
    """대시보드 본문 한 줄 값으로 안전화 — 개행 평탄화·trim·char cap (섹션 위조 방어).

    render_dashboard_section 인자는 공개라 다중행 입력(wave_summary·next_plan)이 `## ` 헤딩을
    위조하거나 상한 줄수를 우회할 수 있다 — 개행을 공백으로 접고(single line) char cap 을 씌워
    자기 규격(줄단위·경량)을 엔진이 직접 방어한다.
    """
    flat = " ".join(part.strip() for part in str(value).splitlines() if part.strip())
    flat = flat.strip()
    if len(flat) > DASHBOARD_LINE_MAX_CHARS:
        flat = flat[: DASHBOARD_LINE_MAX_CHARS - 1].rstrip() + "…"
    return flat


def _sanitize_dashboard_key(session: object) -> str:
    """대시보드 섹션 헤딩 키(`## <session>`)를 안전화 — 개행·`#`(헤딩 마커) 제거·공백 접기.

    session 키는 `## <session>` 헤딩으로 쓰이고 upsert 의 경계 정규식이 그걸 매칭한다 —
    개행이나 `#` 가 섞이면 가짜 헤딩/섹션 경계를 위조할 수 있다(render/upsert 인자는 공개).
    `#` 를 공백으로 바꾸고 whitespace(개행 포함)를 단일 공백으로 접어 한 줄 안전 토큰으로 만든다.
    render(헤딩 출력)·upsert(경계 검색)가 **같은 정규화**를 써 같은 raw 키에 일관 동작한다.
    정상 키(`<repo>_<N>`)는 공백·`#` 가 없어 항등(무변경).
    """
    return " ".join(str(session).replace("#", " ").split())


def _claimed_tickets_for_session(
    session: str, tickets_dir: Path | None = None
) -> list[str]:
    """활성 슬롯(`<repo>_<N>`)이 claim 한 티켓 ID 목록을 board tickets 에서 가볍게 스캔한다 (fail-soft).

    board tickets(`_tickets_dir()`)가 status-subdir 레이아웃이면 `claimed/` 하위를, flat
    레이아웃이면 `status: claimed` frontmatter 를 대상으로, `claimed_by:`(`<email>/<session>`)의
    `/` 뒤 세션 파트가 `session` 과 같은 티켓의 `id:` 를 모은다. board 를 import 하지 않고
    (touches 격리·`_registered_repos`/leases 직접 read 동형) frontmatter 를 라인 스캔한다.
    부재/스키마 불일치/예외는 fail-soft 빈 목록 — 대시보드는 편의 surface 이지 강제 아니다.
    """
    if tickets_dir is None:
        tickets_dir = _tickets_dir()
    try:
        if not tickets_dir.is_dir():
            return []
        claimed_dir = tickets_dir / "claimed"
        if claimed_dir.is_dir():
            candidates = sorted(claimed_dir.glob("*.md"))
            require_status_claimed = False  # subdir 자체가 status 신호.
        else:
            candidates = sorted(tickets_dir.glob("*.md"))
            require_status_claimed = True
        result: list[str] = []
        for f in candidates:
            try:
                head = f.read_text(encoding="utf-8")[:2000]  # frontmatter 만 필요.
            except OSError:
                continue
            if require_status_claimed:
                sm = _TICKET_FRONT_STATUS_RE.search(head)
                # status 값도 따옴표 strip — `status: "claimed"`(YAML 따옴표) 인식 일관성.
                if sm is None or _strip_yaml_scalar(sm.group(1)) != "claimed":
                    continue
            cb = _TICKET_FRONT_CLAIMED_BY_RE.search(head)
            if cb is None:
                continue
            # YAML 따옴표 값(`claimed_by: "…/project_manager_1"`) 방어 — surrounding 따옴표를
            # 벗겨 `/` 뒤 세션 파트를 비교한다(현행 무해하나 견고화·codex suggestion).
            if _strip_yaml_scalar(cb.group(1)).rsplit("/", 1)[-1] != session:
                continue
            idm = _TICKET_FRONT_ID_RE.search(head)
            result.append(_strip_yaml_scalar(idm.group(1)) if idm else f.stem)
        return sorted(set(result))
    except Exception:  # noqa: BLE001 — fail-soft: 스캔 실패는 빈 목록(대시보드 줄 생략).
        return []


def render_dashboard_section(
    session: str,
    *,
    session_num: int | str,
    wave_summary: str,
    claimed_tickets: list[str] | None = None,
    next_plan: str | None = None,
    date: str | None = None,
    max_lines: int | None = None,
) -> str:
    """대시보드 자기 섹션(헤딩 `## <session>` + 본문 3~5줄)을 빌드한다.

    본문 줄(순서·상한 `max_lines`=DASHBOARD_SECTION_MAX_LINES):
      - 차수: PM <N>차 (<date>)
      - wave: <wave_summary 1줄 평탄화>
      - claimed: <T-…, …>   (claimed_tickets 비면 줄 생략)
      - 다음: <next_plan>    (없으면 줄 생략)
    각 값은 `_flatten_dashboard_value`(개행 평탄화·char cap)로 경량화하고, 본문을 `max_lines`
    줄로 truncate 한다(초과분 drop). 반환은 헤딩 + 빈 줄 + 본문 + 후행 개행 1개 —
    upsert_dashboard_section 이 파일에 끼워 넣는 단위 블록이다.
    """
    if date is None:
        date = datetime.date.today().isoformat()
    if max_lines is None:
        max_lines = DASHBOARD_SECTION_MAX_LINES
    body_lines = [
        f"- 차수: PM {_normalize_session_num(session_num)}차 ({date})",
        f"- wave: {_flatten_dashboard_value(wave_summary)}",
    ]
    if claimed_tickets:
        body_lines.append(
            f"- claimed: {_flatten_dashboard_value(', '.join(claimed_tickets))}"
        )
    if next_plan and str(next_plan).strip():
        body_lines.append(f"- 다음: {_flatten_dashboard_value(next_plan)}")
    body = "\n".join(body_lines[:max_lines])
    # 헤딩 키도 안전화 — 개행/`#` 위조로 섹션 경계를 오염시키지 못하게(upsert 검색과 동일 정규화).
    return f"## {_sanitize_dashboard_key(session)}\n\n{body}\n"


def upsert_dashboard_section(
    dashboard_text: str, session: str, section: str
) -> str:
    """대시보드에서 `session` 섹션을 `section` 으로 교체하고, 없으면 append 한다 (수정형).

    - 기존 섹션 교체: `## <session>` 헤딩부터 다음 `## ` 헤딩 직전(또는 파일 끝)까지를 `section`
      으로 offset splice 치환한다 — 헤딩 앞/다음 섹션 이후 bytes 를 그대로 복사하므로 **타 슬롯
      섹션은 byte 불변**(테스트로 못박음). 다음 섹션이 있으면 빈 줄 1개로 구분한다.
    - 신규(섹션 부재): 파일 끝에 append(lazy). 파일이 비면 `_DASHBOARD_HEADER` + 자기 섹션.
    - 멱등: 같은 `section` 재실행은 같은 결과. 순수 텍스트 변환 — 직렬화(파일락)는 파일 I/O 를
      쥔 호출부(`_write_dashboard_section` 의 `_dashboard_lock`)가 read→upsert→write 전체에
      건다(cross-slot lost update 차단·MF-2).

    헤딩 매칭은 정확 줄(`^## <session>$`·trailing 공백 허용)이라 `project_manager_1` 이
    `project_manager_10` 섹션을 오매치하지 않는다. `session` 은 `_sanitize_dashboard_key` 로
    정규화해 검색한다 — render 가 헤딩에 쓰는 것과 **같은 정규화**라 같은 raw 키에 일관 매칭한다.
    """
    core = section if section.endswith("\n") else section + "\n"
    session = _sanitize_dashboard_key(session)
    heading_re = re.compile(rf"^## {re.escape(session)}[ \t]*$", re.MULTILINE)
    m = heading_re.search(dashboard_text)
    if m is not None:
        start = m.start()
        after = dashboard_text[m.end():]
        nxt = re.search(r"^## ", after, re.MULTILINE)
        if nxt is None:
            # 자기 섹션이 마지막(또는 유일) — 파일 끝까지 교체(후행 단일 개행).
            return dashboard_text[:start] + core
        end = m.end() + nxt.start()
        # 다음 섹션 이후는 verbatim 복사(byte 불변) — 사이에 빈 줄 1개 삽입.
        return dashboard_text[:start] + core + "\n" + dashboard_text[end:]
    # 섹션 부재 → append(lazy 생성).
    if not dashboard_text.strip():
        return _DASHBOARD_HEADER + "\n" + core
    return dashboard_text.rstrip("\n") + "\n\n" + core


def parse_dashboard_sections(dashboard_text: str) -> list[tuple[str, str]]:
    """대시보드 텍스트를 `[(session_key, body), ...]` 로 파싱한다 (헤딩 `## <key>` 경계).

    각 섹션 = `## <key>` 헤딩부터 다음 `## ` 헤딩 직전까지. body 는 헤딩 줄 제외 본문에서
    surrounding 빈 줄을 벗긴 것(read-only surface·부트스트랩 light dump 용). preamble(첫 `## `
    앞)은 무시한다. write 측(upsert)과 같은 경계 grammar 를 공유해 read/write 대칭을 보장한다.
    """
    sections: list[tuple[str, str]] = []
    matches = list(_DASHBOARD_HEADING_RE.finditer(dashboard_text))
    for i, mt in enumerate(matches):
        key = mt.group(1).strip()
        body_start = mt.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(dashboard_text)
        body = dashboard_text[body_start:body_end].strip("\n").rstrip()
        sections.append((key, body))
    return sections


# ── PmHandoff 핵심 클래스 ─────────────────────────────────────────────────────

class PmHandoff:
    """PM 핸드오프 7단계 자동화 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    ticket_finish.py 의 TicketFinisher DI 패턴과 동일.
    """

    def __init__(
        self,
        *,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        log_file: Path = LOG_FILE,
        pm_playbook_file: Path = PM_PLAYBOOK_FILE,
        pm_state_file: Path | None = None,
        dashboard_file: Path | None = None,
        venv_python: str | Path = _default_python(),
        worktree_pool=None,
        raw_ledger_file: Path | None = None,
        peer_raw_ledger_files: tuple[Path, ...] | None = None,
    ) -> None:
        self._log_file = log_file
        self._pm_playbook_file = pm_playbook_file
        # 미마감 raw sweep([6b/7]) 장부 seam — 명시(hermetic 테스트)면 그 경로, None(프로덕션)이면
        # sweep 시점에 pm_relay 의 기본 위치(`.project_manager/.local/raw_outputs.json`)로 해소한다.
        self._raw_ledger_file = raw_ledger_file
        # 사본 장부 병기 seam — 명시(hermetic 테스트)면 그 목록(`()` = 사본 없음), None(프로덕션)
        # 이면 sweep 시점에 위임 조회면 판정(`pm_delegate._peer_engine_ledgers`)으로 해소한다.
        self._peer_raw_ledger_files = peer_raw_ledger_files
        # slot 대시보드 seam — 자기 섹션 overwrite 대상. 명시(hermetic 테스트)면
        # 그 경로, None(프로덕션)이면 write 시 `_dashboard_file()`(호출 시점 REPO 추종)로 해소한다.
        self._dashboard_file = dashboard_file
        # pm_state 는 *슬롯별* — 명시 주입(테스트/override)이 있으면
        # 그 경로 고정, 미지정(None·프로덕션)이면 run() 진입부에서 활성 슬롯을
        # 해소해 `_pm_state_path` 로 per-slot 경로(또는 솔로 legacy 폴백)를 세팅한다. 명시 주입
        # 여부를 기억해 per-slot 재해소가 hermetic 테스트의 명시 경로를 덮지 않게 한다.
        self._pm_state_file_explicit = pm_state_file is not None
        self._pm_state_file = pm_state_file if pm_state_file is not None else _legacy_pm_state_file()
        self._venv_python = venv_python
        # worktree_pool seam — 테스트는 mock 모듈을 주입(hermetic). None 이면 --done
        # --slot 경로 진입 시에만 동적 로드(multi-PM 모드)·솔로 무인자 경로는 안 건드린다.
        self._worktree_pool = worktree_pool
        # 회귀 cwd 해소용 worktree 슬롯— run() 진입부에서 worktree_slot 인자로 세팅.
        # _default_run_pytest 가 _regression_cwd 에 넘긴다. 솔로/미세팅이면 None → REPO 폴백.
        self._worktree_slot: str | None = None
        # run() 경계에서 슬롯 해소가 끝났는지(해소 결과 None도 값이다). True 뒤에는 downstream이
        # None을 "미해소"로 오인해 lease를 재조회하지 않고 같은 solo 스냅샷을 REPO에 결속한다.
        self._worktree_slot_resolution_frozen = False
        # task handoff 시작 시 영속 변경 전에 한 번 해소한 보유 슬롯 스냅샷. 회귀/출하/재스냅이
        # 같은 집합을 재사용해 후반 재조회 실패가 이미 기록된 handoff를 false-green으로 끝내지 않게 한다.
        self._task_slots_snapshot: tuple[str, tuple[str, ...]] | None = None
        # membership gate가 읽은 task record 스냅샷. 정상 handoff가 pid=0으로 release한 뒤
        # bootstrap 없이 같은 CLI를 재실행했는지 차수 추론에서 구분한다.
        self._task_record_snapshot: tuple[str, object] | None = None

        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_git_fn = run_git_fn or self._default_run_git

    # ── multi-PM 모드: 작업완료 release ────────────────────────────────

    def _resolve_worktree_pool(self):
        """worktree_pool 모듈을 해소한다 — 주입분 우선·없으면 동적 로드 (multi-PM 모드 전용).

        --done --slot 경로에서만 호출된다. 둘 다 None 이면 **명시 에러**(SystemExit) —
        multi-PM 인자를 줬는데 worktree_pool 이 없으면 침묵 무력화 금지.
        """
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            print(
                "[중단] --done --slot multi-PM 모드인데 worktree_pool 엔진을 찾을 수 없다 "
                f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패). "
                "multi-PM 셋업(pm-config) 또는 엔진 전파를 확인하라.",
                file=sys.stderr,
            )
            return None
        return wp

    def _release_slot(self, slot: str) -> int:
        """작업완료 시 worktree 슬롯을 release 한다 (--done).

        dirty 면 require_clean=False 자동경로로 stash 보존 후 idle 화(자동화에서 막힘
        방지). worktree_pool 부재(multi-PM 미배선)면 명시 에러로 중단(rc 1). 반환: 0=성공.
        """
        wp = self._resolve_worktree_pool()
        if wp is None:
            return 1
        try:
            lease = wp.release(slot, require_clean=False)
        except KeyError:
            print(
                f"  ⚠ slot {slot!r} 리스 장부에 없음 — 이미 release 됐거나 미등록 슬롯. "
                "release 스킵(무해).",
                file=sys.stderr,
            )
            return 0
        # 브랜치는 슬롯 worktree 의 git HEAD 에서 live 조회
        # Lease.branch 권위 제거·git=진실). detached/조회불가는 "(detached/조회불가)".
        live_branch = wp.current_branch(slot) or "(detached/조회불가)"
        print(f"  ✓ worktree 슬롯 release: {slot} → idle (작업완료 반납·branch={live_branch})")
        return 0

    # ── 핸드오프 완료: bound 슬롯 git 재스냅 ("여기 두고 간다") ───────────────

    def _record_slot_snapshot(self, slot: str, *, task: str | None = None) -> bool:
        """bound 슬롯의 live git 을 `lease.git` 에 재기록한다 — "여기 두고 간다".

        핸드오프 부기(log·pm_state) 완료 후, 슬롯의 현재 branch/HEAD 를 리스 장부에 재스냅해
        차기 부트스트랩 0단계 record-vs-live 정합(`compare_slot_git`)이 보는 *도착 스냅* 을
        갱신한다. 세션 중 브랜치/HEAD 가 바뀌면(예: 릴리즈 v1.3.2→v1.3.3) bind 의 옛 도착 스냅만
        남아 0단계가 `diverged` FAIL-LOUD 로 정당한 자기 진행을 외부-개입 오경보로 차단하기
        때문이다.

        write 프리미티브 `worktree_pool.record_git_snapshot(slot)` 만 호출한다 — base 는
        미전달(기존 보존·arrival 동형)·판정 로직 재구현 없음. fail-soft: worktree_pool 부재
        (솔로/미셋업)·슬롯 미바인딩/장부 부재(record None)·스냅 예외는 무해 skip(핸드오프
        차단 안 함). release(--done·git 정리) 경로는 호출부에서 제외한다.

        **실갱신 vs 무변경 구분 출력(dual-gate suggestion)**: `record_git_snapshot` 은 슬롯
        live 스냅이 불가하면(worktree 경로 부재 등) 기존 `lease.git` 을 clobber 하지 않고 그대로
        둔다(`_apply_git_snapshot` no-op·silent 손실 방지). 그럴 때 옛 branch/head 를 "✓ 기록"으로
        내면 갱신 안 됐는데 성공처럼 읽힌다 — 재스냅 *전* lease.git(`read_lease`)과 *후* 값을 비교해
        실제 바뀌었을 때만 "재스냅 기록", 무변경이면 "스냅 불가·기존 유지"로 구분 출력한다."""
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            print("  worktree_pool 미배선(솔로/미셋업) — git 재스냅 skip(무해).")
            return True
        if task is not None:
            checker = getattr(wp, "lease_owned_by_task_strict", None)
            try:
                owned = bool(checker(slot, task)) if callable(checker) else False
            except Exception as exc:  # noqa: BLE001
                if _is_engine_rev_skew(exc):
                    raise
                print(f"  [중단·소유권 재검증] task 슬롯 {slot!r} 장부 조회 실패 — {exc}. "
                      "git 재스냅을 실행하지 않습니다.", file=sys.stderr)
                return False
            if not owned:
                print(f"  [중단·소유권 재검증] task 슬롯 {slot!r}은 더 이상 "
                      f"state=leased && session={task!r}가 아닙니다 — git 재스냅 중단. "
                      "새 소유자의 lease.git을 덮어쓰지 않습니다.", file=sys.stderr)
                return False
        before_git = self._lease_git_before(wp, slot)
        try:
            lease = wp.record_git_snapshot(slot)
        except Exception as exc:  # noqa: BLE001 — fail-soft: 재스냅 실패가 핸드오프를 막지 않는다.
            if _is_engine_rev_skew(exc):
                raise
            print(f"  ⚠ git 재스냅 실패 — {exc} (skip·무해).", file=sys.stderr)
            return True
        if lease is None:
            print(f"  ⚠ slot {slot!r} 리스 장부에 없음 — git 재스냅 skip(무해).", file=sys.stderr)
            return True
        after_git = lease.git if isinstance(lease.git, dict) else None
        if after_git is not None and after_git != before_git:
            print(
                f"  ✓ git 재스냅 기록: {slot} → "
                f"branch=`{after_git.get('branch')}` head=`{after_git.get('head')}` "
                "(실갱신·여기 두고 간다)"
            )
        else:
            # 무변경 — 슬롯 live 스냅 불가(worktree 경로 부재 등)로 기존 기록 유지(clobber 방지·무해).
            print(
                f"  · git 재스냅 무변경: {slot} — 슬롯 live 스냅 불가·기존 기록 유지"
                "(스냅 불가·차기 0단계는 기존 스냅 기준·무해)."
            )
        return True

    def _lease_git_before(self, wp, slot: str) -> "dict | None":
        """재스냅 *전* 슬롯 lease.git 을 조회한다 — 실갱신/무변경 판별용 (fail-soft).

        worktree_pool `read_lease`(순수 장부 read·`record_git_snapshot` 짝)로 현재 스냅을 읽는다.
        구버전 풀(`read_lease` 부재)·조회 실패·미기록은 None(→ 후 값이 dict 면 실갱신으로 취급·
        보수적: 판별 불가는 '기록'으로 표기해 무변경 오표기보다 정보 손실이 적다)."""
        reader = getattr(wp, "read_lease", None)
        if reader is None:
            return None
        try:
            lease = reader(slot)
        except Exception as exc:  # noqa: BLE001 — 일반 조회 실패는 None(판별 보수적 폴백).
            if _is_engine_rev_skew(exc):
                raise
            return None
        if lease is None:
            return None
        git = getattr(lease, "git", None)
        return git if isinstance(git, dict) else None

    # ── task 퇴장: 보유 슬롯 집합 열거·변경 판정·전 슬롯 재스냅 ──────

    def _task_held_slots(self, task: str) -> list[str]:
        """task(session==name) 명의로 leased 인 슬롯 식별자 리스트 — `slots_for_task` 재사용 (조회 전용).

        퇴장 의미론: task 세션은 보유 슬롯 **집합**을 두고 나간다. `worktree_pool.
        slots_for_task(task)`(tasks 장부 조인·session==task 이고 leased 인 슬롯)를 소비해
        보유 집합을 열거한다(재열거 로직 재구현 없음). run()이 영속 변경 전에 한 번 성공적으로
        해소한 스냅샷은 회귀/출하/재스냅 전 단계가 공유한다. worktree_pool/slots_for_task 부재,
        None 반환, 예외, 슬롯 식별자 없는 행은 **해소 실패**로 raise한다. 실제 `[]`만 정당한
        0슬롯이다."""
        if self._task_slots_snapshot is not None and self._task_slots_snapshot[0] == task:
            return list(self._task_slots_snapshot[1])
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            raise RuntimeError("worktree_pool 엔진 부재")
        # 실 엔진은 strict API를 우선 사용해 손상/읽기 오류를 실제 0슬롯과 구분한다.
        # DI/구버전 엔진 호환은 기존 API로 유지한다.
        fn = getattr(wp, "slots_for_task_strict", None)
        if not callable(fn):
            fn = getattr(wp, "slots_for_task", None)
        if not callable(fn):
            raise RuntimeError("worktree_pool.slots_for_task 부재")
        try:
            resolved = fn(task)
            if resolved is None:
                raise RuntimeError("slots_for_task가 None을 반환")
            leases = list(resolved)
        except Exception as exc:
            if _is_engine_rev_skew(exc):
                raise
            raise RuntimeError(f"slots_for_task({task!r}) 조회 실패: {exc}") from exc
        slots: list[str] = []
        for lease in leases:
            slot = getattr(lease, "slot", None)
            if not slot:
                raise RuntimeError("slots_for_task 결과에 slot 식별자 없는 행 존재")
            slots.append(str(slot))
        return slots

    def _slot_has_changes(self, slot: str) -> tuple[bool, str]:
        """슬롯이 도착/직전 스냅(`lease.git`) 대비 변경 흔적(head 전진 또는 dirty)이 있는지.

        반환 `(changed, reason)` — reason 은 출력용 사유 문구. 판정("변경 흔적 있는
        보유 슬롯만 회귀"):
          - 스냅 미기록(`unrecorded`) → 보수적으로 changed(직전 green 근거가 없으니 회귀 포함).
          - branch 변경(`not branch_match`) 또는 head 가 match 아님(descendant/diverged/unknown) →
            changed(세션 중 커밋 전진/리셋).
          - dirty(미커밋 변경) → changed(compare 는 head 만 봐 커밋 안 한 작업을 못 잡는다).
          - head match + branch match + clean → **unchanged**(직전 green 불변·회귀 신호 0·skip).
        판정 프리미티브는 재구현하지 않고 worktree_pool `compare_slot_git`(도착 스냅 vs live)+
        `slot_git_status`(dirty)를 소비한다. fail-soft **보수**: worktree_pool 부재·조회 예외는
        changed(놓치기보다 도는 게 낫다 — 무변경 오판이 회귀 누락으로 이어지는 게 더 위험)."""
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            return True, "worktree_pool 미배선·보수적 변경 취급"
        try:
            cmp = wp.compare_slot_git(slot)
        except Exception as exc:  # noqa: BLE001 — fail-soft 보수: 판정 불가는 변경 취급(회귀 포함).
            if _is_engine_rev_skew(exc):
                raise
            return True, "compare 예외·보수적 변경 취급"
        if getattr(cmp, "unrecorded", False):
            return True, "스냅 미기록·보수적 변경 취급"
        head_match = getattr(wp, "HEAD_MATCH", "match")
        if not getattr(cmp, "branch_match", False):
            return True, "branch 변경"
        if getattr(cmp, "head_relation", None) != head_match:
            return True, f"head {getattr(cmp, 'head_relation', None)}(전진/재설정)"
        try:
            dirty = bool(wp.slot_git_status(slot).get("dirty", False))
        except Exception as exc:  # noqa: BLE001 — fail-soft 보수: dirty 조회 불가는 변경 취급.
            if _is_engine_rev_skew(exc):
                raise
            return True, "dirty 조회 예외·보수적 변경 취급"
        if dirty:
            return True, "dirty(미커밋 변경)"
        return False, "head match·clean"

    def _record_task_slot_snapshots(self, task: str, dry_run: bool) -> bool:
        """task 보유 **전 슬롯**의 live git 을 `lease.git` 에 재스냅한다 — "집합 전체 두고 간다".

        현행 1슬롯 한정(`_record_slot_snapshot` 단일 호출)을 폐지하고, task 가 보유한 leased 슬롯
        전수(`_task_held_slots`·slots_for_task)를 루프로 재스냅한다. 각 슬롯은 프리미티브
        (`_record_slot_snapshot`)를 그대로 재사용(per-slot fail-soft·판정 재구현 없음). 보유 0개면
        명시 skip. dry_run 은 슬롯별 예고만."""
        slots = self._task_held_slots(task)
        print("\n[재스냅] task 보유 슬롯 git 재스냅 (집합 전체 두고 간다)...")
        if not slots:
            print(f"  · task {task!r} 보유 슬롯 0개 — 재스냅 대상 없음(무해 skip).")
            return True
        for slot in slots:
            if dry_run:
                print(f"  [dry-run] git 재스냅 예고: {slot} (실행 생략).")
            else:
                if not self._record_slot_snapshot(slot, task=task):
                    return False
        return True

    def _release_task_pid(self, task: str) -> bool:
        """task handoff intent를 장부 `pid=0`으로 기록한다 — 성공 여부 반환.

        핸드오프 부기(log·pm_state) 전 task 모드에서 호출한다. task 장부 pid 는 dump 후 즉사하는
        bootstrap subprocess pid라, 종료를 안 기록하면 **정상 인계 후 재개도** dead-pid →
        `bind_task` 가 `reclaimed`("재개(회수·이전 세션 crash)" + "⚠️ 회수 진입")로 상시 오탐한다
        write 프리미티브 `worktree_pool.release_task_pid(task)` 만 호출해 pid 를 0 으로
        비워, 차기 부트스트랩이 clean `resumed`(경고 없음)로 재개하게 한다 — 진짜 crash(핸드오프 없이
        죽어 pid>0 잔존)만 회수 경고를 받는다.

        같은-session 재실행 판정이 이 durable 신호에 의존하므로 더는 보조 fail-soft 부기가 아니다.
        step2 log write **전**에 성공해야 하며, 엔진/primitive/record 부재·예외는 fail-loud(False)다.
        성공 뒤 후속 단계가 중단돼도 pid=0이 남아 재실행이 같은 차수로 복구된다."""
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            print("  [중단] worktree_pool 미배선 — task handoff intent를 기록할 수 없다.",
                  file=sys.stderr)
            return False
        primitive = getattr(wp, "release_task_pid", None)
        if primitive is None:
            print("  [중단] worktree_pool 구버전(release_task_pid 부재) — "
                  "멱등성 신호를 기록할 수 없다.",
                  file=sys.stderr)
            return False
        try:
            task_rec = primitive(task)
        except Exception as exc:  # noqa: BLE001 — load-bearing intent 기록 실패는 차단.
            if _is_engine_rev_skew(exc):
                raise
            print(f"  [중단] task handoff intent(pid=0) 기록 실패 — {exc}.", file=sys.stderr)
            return False
        if task_rec is None:
            print(f"  [중단] task {task!r} 장부에 없음 — 멱등성 신호를 기록하지 못했다.",
                  file=sys.stderr)
            return False
        print(
            f"  ✓ task handoff intent 기록: {task} → pid=0(미점유) "
            "(재실행=같은 차수·다음 bootstrap=새 pid/N+1)"
        )
        return True

    # ── 기본 subprocess 구현 (실제 실행) ──────────────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """pytest tests/ -q 를 실행해 (returncode, stdout+stderr) 반환.

        cwd 는 _regression_cwd 가 해소한다— 분리된 PM 홈엔 tests/ 가 없으므로
        활성 worktree 슬롯에서 돌린다. 솔로/미해소면 REPO 폴백(현행 보존).
        """
        result = subprocess.run(
            [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self._run_regression_cwd(self._worktree_slot),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _run_regression_cwd(self, worktree_slot: str | None) -> str:
        """run()의 단일 슬롯 스냅샷에 결속된 cwd를 반환한다.

        run() 경계에서 해소된 None은 legacy solo라는 확정값이다. 그 뒤 `_regression_cwd(None)`을
        호출하면 lease를 다시 읽어 새 슬롯으로 바뀔 수 있으므로 REPO를 바로 반환한다. run() 밖
        단위 seam은 기존 자동해소 동작을 보존한다.
        """
        if self._worktree_slot_resolution_frozen and worktree_slot is None:
            return str(REPO)
        return _regression_cwd(worktree_slot)

    # ── task 퇴장: 변경 흔적 있는 보유 슬롯 각각에서 회귀 ──────

    def _run_regression_for_slot(self, slot: str) -> tuple[int, str]:
        """지정 슬롯 worktree 에서 회귀를 1회 돌린다 — task 다중슬롯 회귀.

        `self._worktree_slot` 을 그 슬롯으로 잠시 세팅해 `_default_run_pytest` 의 `_regression_cwd`
        해소가 해당 worktree 를 cwd 로 보게 하고 원복한다(다른 downstream 이 `self._worktree_slot` 을
        재사용하므로 scope 밖 오염 방지). 주입 seam(`run_pytest_fn`)도 그대로 소비해 hermetic
        테스트와 정합(injected runner 는 세팅된 슬롯을 관찰 가능)."""
        prev = self._worktree_slot
        self._worktree_slot = slot
        try:
            return self._run_pytest_fn()
        finally:
            self._worktree_slot = prev

    def _slot_worktree_missing(self, slot: str) -> bool:
        """슬롯 worktree 디렉터리가 stale(장부엔 있으나 dir 부재)인지 — task 슬롯별 회귀 vacuous-pass 가드.

        `_regression_cwd(slot)` 는 stale 슬롯(장부 조인 통과·dir 부재)을 **soft 하게 REPO 로 폴백**한다
        슬롯 모드에선 무해하나, **task 슬롯별 회귀**에선 그 REPO 에서 pytest 가 돌아 그
        슬롯이 green 처럼 보인다(vacuous-pass·엉뚱한 트리 green). 그래서 이 경로에선 REPO
        폴백을 금지하고 부재를 fail-loud(그 슬롯 red)로 올린다. 실재 판별은 worktree_pool `slot_path(slot).
        exists()`(엔진 소유 축·`_regression_cwd` 의 `REPO/slot` 과 동형). 판별 불가(worktree_pool 부재·
        구버전 pool·slot_path 부재·예외)는 **False**(=차단 안 함·fail-soft — 기존 `_regression_cwd` 해소에
        위임)."""
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            return False
        resolver = getattr(wp, "slot_path", None)
        if resolver is None:
            return False
        try:
            return not resolver(slot).exists()
        except Exception as exc:  # noqa: BLE001 — fail-soft: 판별 실패는 차단 안 함(기존 해소에 위임).
            if _is_engine_rev_skew(exc):
                raise
            return False

    def _classify_task_changed_slots(self, task: str) -> list[tuple[str, str]]:
        """task 보유 슬롯을 변경/무변경 분류해 **변경-슬롯 [(slot, reason)] 리스트**를 돌려준다.

        회귀([1/7])와 출하 변경 surface([1b])가 공유하는 '변경-슬롯 집합' 계산의 **단일 지점**. 회귀
        실행 여부(--no-pytest)와 무관하게 이 열거는 수행한다 — 비용은 슬롯당 git 조회 몇 회(회귀와
        무관·저렴)뿐이고, skip_pytest 여도 [1b] 가 REPO 폴백 단일 검사로 후퇴하지 않게 변경 집합을
        확보해야 하기 때문이다.
          - 보유 0개 → 명시 skip(대여 안내)·빈 리스트.
          - 변경 흔적(`_slot_has_changes`: lease 스냅 대비 head 전진/dirty·미기록=보수적 변경) 있는
            슬롯만 집합에 넣고 — 무변경 슬롯은 신호 0(직전 green 불변)이라 skip+사유 출력.
          - 변경 0개 → "변경 슬롯 없음" 명시(회귀·surface 대상 0)."""
        slots = self._task_held_slots(task)
        if not slots:
            print(
                f"  · task {task!r} 보유 슬롯 0개 — 대상 없음(skip). "
                f"회귀가 필요하면 `{_runtime_skill_entry('pm-env')} alloc <repo> --task <이름>` "
                "을 먼저 시도하라. 풀이 소진됐다면 사용자에게 task-aware 슬롯 생성 승인을 "
                "요청하고, 승인한 사용자만 worktree add를 직접 실행한 뒤 task-only handoff를 "
                "다시 실행한다(세션 자동 생성 금지)."
            )
            return []
        changed: list[tuple[str, str]] = []
        for slot in slots:
            is_changed, reason = self._slot_has_changes(slot)
            if is_changed:
                changed.append((slot, reason))
            else:
                print(f"  · {slot} — 변경 흔적 없음({reason})·skip(직전 green 불변·신호 0).")
        if not changed:
            print(
                f"  · task {task!r} 보유 {len(slots)}슬롯 전부 무변경 — 변경 슬롯 없음"
                "(회귀·출하 surface 대상 0·신호 0·push 게이트=pre-push 훅 별도)."
            )
        return changed

    def _run_task_regressions(
        self, changed: list[tuple[str, str]], dry_run: bool
    ) -> "list[str] | None":
        """변경-슬롯 각각에서 회귀를 돌린다.

        `changed` = `_classify_task_changed_slots` 결과([(slot, reason)]·변경 슬롯만). 반환: 회귀가
        돈 **변경-슬롯 식별자 리스트**(green·비어있으면 회귀 실행 없음) 또는 **None**(한 슬롯이라도
        red → 호출부가 핸드오프 차단). 변경 슬롯 전부 green 이어야 통과. push 게이트는 pre-push 훅
        전체 회귀가 별도 재검증(이중 안전·무변경 슬롯 부담 제거)."""
        ran: list[str] = []
        for slot, reason in changed:
            # stale 슬롯(장부엔 있으나 worktree dir 부재) = fail-loud(REPO 폴백 vacuous-pass 금지·
            # 그 슬롯을 red 로 차단하고 해소 커맨드를 안내한다.
            if self._slot_worktree_missing(slot):
                print(
                    f"\n[중단] task 슬롯 {slot!r} 의 worktree 디렉터리 부재(stale — 장부엔 존재·"
                    f"{REPO / slot}) — REPO 폴백 회귀는 vacuous-pass(엉뚱한 트리 green)이므로 "
                    f"그 슬롯을 red 로 차단한다. `{_runtime_skill_entry('pm-worktree')} "
                    "prune-stale`(장부 정리) 또는 "
                    "사용자에게 슬롯 재생성 승인을 요청하라. 승인한 사용자만 "
                    "`worktree add <repo> --task <이름> --user-ack <repo>`를 직접 실행한 뒤 "
                    "재시도한다(세션 자동 실행 금지). "
                    "log/current.md·pm_state.md 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                return None
            print(f"  ▷ {slot} 회귀 ({reason})...")
            if dry_run:
                print("    [dry-run] pytest tests/ -q 실행 중 (파일 편집만 생략)...")
            returncode, output = self._run_regression_for_slot(slot)
            print(output.rstrip())
            if not is_pytest_green(output, returncode):
                print(
                    f"\n[중단] {slot} 회귀 red — 핸드오프 불가. "
                    "log/current.md·pm_state.md 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                return None
            summ = parse_pytest_summary(output)
            print(f"  ✓ green: {slot} — {summ}")
            ran.append(slot)
        return ran

    def _shipping_surface_for_slots(self, slots: list[str]) -> None:
        """회귀가 돈 **변경-슬롯 각각**의 worktree 에서 출하 변경을 surface 한다 (codex must-fix).

        task 다중슬롯 회귀는 변경-슬롯 집합을 돌므로, 출하 변경 surface([1b])도 **같은 집합**을 슬롯별
        로 돌려야 한다 — 단일 `_regression_cwd(None)` 자동해소가 엉뚱한 트리를 보거나 일부 변경-슬롯의
        SHIPPING_GLOBS 변경을 놓치는 것을 막는다(집합 1급화 일관·회귀·재스냅과 같은 슬롯 목록 공유).
        각 슬롯은 회귀와 동일하게 `_regression_cwd(slot)` 로 cwd 를 해소한다(stale 슬롯은 그쪽에서 REPO
        폴백·동형). 0개(변경 슬롯 없음)면 미push 변경도 없으니 surface 대상 없음."""
        if not slots:
            print("  · task 변경 슬롯 0개 — 출하 변경 surface 대상 없음(미push 변경 없음).")
            return
        for slot in slots:
            print(f"  ▷ {slot} 출하 변경:")
            self._shipping_surface_step(_regression_cwd(slot))

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        """git 명령을 실행해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    # ── 출하 변경 surface step ────────────────────

    def _shipping_surface_step(self, worktree: str) -> None:
        """[1b/7] 미검증 출하 변경을 **비차단 1줄로 surface** 한다.

        미push diff ∩ SHIPPING_GLOBS([[smoke-gate-by-output-change]])를 분류해 출하 변경
        (hits)·분류불명(unknown)이 있으면 "릴리즈 전 라이브 필요" 경고 1줄을 출력한다 —
        **핸드오프를 지연·차단하지 않는다**(rc 무영향). 라이브 LLM 검증(실 하네스 smoke)은
        릴리즈 단일 지점(release wave)으로 모았으므로, 고빈도 지점인
        핸드오프에서는 가시성만 보존한다. hits·unknown 모두 없으면(push 없음·명확한 비-출하)
        skip 사유만 출력.

        분류기(`_shipping_paths_in_pending_push`·`SHIPPING_GLOBS`)는 존치 — surface 의 기반이자
        향후 게이트 복원 가능성의 가역 지점.
        """
        shipping_hits, has_unknown = _shipping_paths_in_pending_push(
            worktree, git_runner=self._run_git_fn
        )
        if shipping_hits:
            print(
                f"  ⚠ 미검증 출하 변경 {len(shipping_hits)}파일 — 릴리즈 전 "
                "라이브(release wave) 필요: `board.py livegate record`."
            )
            return
        if has_unknown:
            print(
                "  ⚠ 미검증 출하 변경 가능성 (미push diff 분류 불명·baseline 해소불가) — "
                "릴리즈 전 라이브(release wave) 확인 필요."
            )
            return
        print("  출하 변경 없음 (미push diff 가 비-출하·또는 push 없음) — release 라이브 불요.")

    # ── dirty-tree 게이트 step ([0/7]·조기 중단) ────────────────

    def _dirty_gate_trees(self, task: str | None) -> list[str]:
        """게이트가 판정할 실행 앵커 트리 목록 — PM 홈 + **활성 worktree 전수**.

        모드별 특례를 두지 않는다(solo = multi-PM 의 부분집합):
          - task 모드: 보유 슬롯 전수. run() 진입부가 영속 변경 전에 한 번 해소해 둔 스냅샷
            (`_task_slots_snapshot`)을 그대로 쓴다 — 게이트가 장부를 다시 조회해 다른 집합을
            보는 비대칭을 만들지 않는다.
          - slot/솔로 모드: 활성 worktree 1개 — [1b] 출하 surface 가 쓰는 것과 **같은 해소**
            (`_regression_cwd(self._worktree_slot)`·솔로는 단일 self-host 자동해소).
        PM 홈 `.gitignore` 는 보통 `work/` 를 ignore 하므로 슬롯 트리는 PM 홈 판정에 **절대**
        안 잡힌다 — 슬롯을 따로 넣지 않으면 그 트리의 미커밋 산출은 게이트를 통과한다.
        슬롯 → 트리 변환은 회귀/출하와 같은 `_regression_cwd`(stale 슬롯은 REPO 폴백)라 중복이
        생길 수 있어 순서 보존 dedup 한다.

        반환한 각 앵커 아래 등록된 submodule working tree 전수도 `_dirty_tree_gate` 가
        `submodule status --recursive` 로 확장 판정한다. 앵커 목록 자체는 기존 list[str] 계약을
        유지해 task/slot 해소와 submodule 열거 책임을 섞지 않는다.
        """
        trees = [str(REPO)]
        snapshot = self._task_slots_snapshot
        if task is not None:
            if snapshot is not None and snapshot[0] == task:
                trees.extend(_regression_cwd(slot) for slot in snapshot[1])
        else:
            trees.append(self._run_regression_cwd(self._worktree_slot))
        ordered: list[str] = []
        for tree in trees:
            if tree not in ordered:
                ordered.append(tree)
        return ordered

    def _dirty_tree_gate(
        self,
        task: str | None,
        *,
        ack_dirty: str | None,
        auto_trigger: bool,
        dry_run: bool,
    ) -> tuple[int, str | None]:
        """[0/7] 미커밋 잔여를 **첫 mutation 전에** 판정한다. 반환: (rc, ack 박제 사유).

        rc 1 = 차단(어떤 파일도 건드리지 않은 상태). 차단 조건은 "판정 트리 중 하나라도 dirty
        ∧ override 없음" 하나뿐이다:
          - `--ack-dirty "<사유>"` → 통과 + 사유를 handoff entry 에 박제.
          - `--auto-trigger`(사용자 명시 핸드오프 호출부 호환 신호) → 차단 대신 loud 경고 +
            사유 자동 박제. 이 플래그 자체는 실행 승인이나 자동 핸드오프 트리거가 아니다.
          - `--dry-run` → 판정 결과 미리보기만(차단 없음·기존 dry-run 의미 유지).
        판정 불가 트리(비-git·git 부재)는 비차단 경고 1줄로 stderr surface 한다 — 판정을 못 한
        것을 dirty 로 단정해 정상 핸드오프를 막지 않는다(false-block 회피). 커밋 0(unborn HEAD)은
        판정 불가가 아니라 **전량 미커밋**이라 untracked 목록으로 정상 판정한다.
        """
        trees = self._dirty_gate_trees(task)
        dirty: list[tuple[str, list[str]]] = []
        unresolved: list[str] = []
        unresolved_submodules: list[str] = []
        judged_tree_count = 0
        for tree in trees:
            tree_paths = _dirty_paths_in_tree(tree, self._run_git_fn)
            if tree_paths is None:
                unresolved.append(tree)
                paths: list[str] = []
            else:
                judged_tree_count += 1
                paths = list(tree_paths)

            submodules = _submodule_paths_in_tree(tree, self._run_git_fn)
            if submodules is None:
                # 앵커 자체도 판정 불가면 기존 경고 한 줄이 이미 전체 트리를 대표한다. 앵커는
                # 판정됐는데 열거만 실패한 경우에만 별도 경고해 silent skip 을 막는다.
                if tree_paths is not None:
                    unresolved_submodules.append(tree)
            else:
                for submodule in submodules:
                    sub_tree = str(Path(tree) / submodule)
                    sub_paths = _dirty_paths_in_tree(sub_tree, self._run_git_fn)
                    if sub_paths is None:
                        unresolved.append(sub_tree)
                        continue
                    judged_tree_count += 1
                    prefix = submodule.rstrip("/")
                    paths.extend(
                        f"{prefix}/{path.lstrip('/')}" for path in sub_paths
                    )

            if paths:
                dirty.append((tree, list(dict.fromkeys(paths))))
        for tree in unresolved:
            # 게이트의 다른 판정 메시지(차단·강등 경고)와 같은 채널(stderr)로 낸다 — "게이트가
            # 못 본 트리" 는 성공 로그가 아니라 경고다.
            print(
                f"  ⚠ dirty 판정 불가 — {tree} (비-git 트리·git 부재). "
                "이 트리는 게이트가 보지 못했다.",
                file=sys.stderr,
            )
        for tree in unresolved_submodules:
            print(
                f"  ⚠ dirty 판정 불가 — {tree} (submodule status --recursive 실패). "
                "이 앵커의 서브모듈은 게이트가 보지 못했다.",
                file=sys.stderr,
            )
        dirty_count = sum(len(paths) for _tree, paths in dirty)
        if not dirty:
            if ack_dirty:
                print("  --ack-dirty 무시 — 판정 결과 clean(박제할 잔여 없음).")
            print(f"  ✓ clean — 미커밋/untracked 없음 (판정 트리 {judged_tree_count}개).")
            return 0, None

        print(f"  미커밋 잔여 {dirty_count}건 (판정 트리 {judged_tree_count}개):")
        for tree, paths in dirty:
            print(f"  ▷ {tree} — {len(paths)}건:")
            for line in _format_dirty_paths(paths):
                print(line)

        if dry_run:
            # 미리보기 — dry-run 은 아무것도 쓰지 않으므로 차단할 이유가 없다(기존 의미 유지).
            print("  [dry-run] 판정 결과 미리보기 — 차단하지 않는다.")
            if ack_dirty:
                return 0, build_dirty_ack_note(ack_dirty, dirty_count, auto=False)
            if auto_trigger:
                return 0, build_dirty_ack_note("", dirty_count, auto=True)
            return 0, None

        if ack_dirty:
            note = build_dirty_ack_note(ack_dirty, dirty_count, auto=False)
            print(f"  ⚠ --ack-dirty override — 사유를 handoff entry 에 박제한다: {note}")
            return 0, note

        if auto_trigger:
            note = build_dirty_ack_note("", dirty_count, auto=True)
            print(
                "  ⚠ 사용자 명시 핸드오프 호환 신호(--auto-trigger) — dirty를 차단하지 않고 "
                "사유를 자동 박제한다: "
                f"{note} 다음 세션이 이 잔여를 먼저 처리해야 한다.",
                file=sys.stderr,
            )
            return 0, note

        print(
            f"\n[중단] 핸드오프 시작 시점에 미커밋 잔여 {dirty_count}건 — 핸드오프 불가. "
            "세션 산출물을 먼저 커밋하라(티켓 산출은 티켓별 커밋이 관례). "
            "의도적으로 두고 나간다면 `--ack-dirty \"<사유>\"` 로 사유를 남겨라. "
            "log/current.md·pm_state.md 어떤 것도 건드리지 않는다.",
            file=sys.stderr,
        )
        return 1, None

    # ── 미마감 raw sweep step ────────────────

    def _resolve_raw_ledger(self, relay=None) -> Path | None:
        """조회할 raw 장부 경로 — 명시 주입 우선, 없으면 pm_relay 기본 위치. 해소 실패는 None.

        `relay` = 이미 로드한 pm_relay 모듈(미전달이면 여기서 로드). sweep step 은 조회에도
        같은 모듈이 필요하므로 한 번만 로드해 넘긴다.
        """
        if self._raw_ledger_file is not None:
            return self._raw_ledger_file
        relay = relay if relay is not None else _load_pm_relay()
        if relay is None:
            return None
        try:
            _raw_dir, ledger = relay.raw_storage_paths(
                REPO, "delegate", None, temp_dir=Path(tempfile.gettempdir()),
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft: 위치 해소 실패는 sweep 만 건너뛴다.
            if _is_engine_rev_skew(exc):
                raise  # 사본 skew 는 삼키지 않는다(`_load_pm_relay` 와 같은 규칙).
            return None
        return ledger

    def _unfinished_raw_step(self) -> None:
        """[6b/7] 미마감 raw 레코드를 **비차단 경고로 surface** 한다 (건수 + 최근 N건 식별자).

        미마감 = 실행 전 예약된 레코드가 마감되지 않은 것 = 그 위임/리뷰가 비정상 종료했다는 뜻.
        누적되면 장부 조회가 무력해지므로 세션 끝에서 건수를 보여주고 정리 판단을 PM 에게 남긴다.
        **차단하지 않는다**(rc 무영향) — 장부 부재·로드 실패·조회 실패는 전부 사유 1줄 후 skip.

        판정 범위(어느 장부를 봤는지)를 항상 1줄로 밝힌다 — 위임은 소유 PM 홈
        (`pm_delegate._CONFIG_REPO_OVERRIDE`)이나 명시 `--output-dir` 기준으로 장부를 고르므로
        이 sweep 이 안 보는 사본 장부(예: worktree 쪽)가 있을 수 있다. 표기가 없으면 "0건"이
        "전 장부 0건"으로 읽힌다. 그 사본이 **실재하면** 경로와 건수도 병기한다(가시성만 —
        전 장부 통합 조회는 이 표면의 범위 밖).
        """
        relay = _load_pm_relay()
        if relay is None:
            print("  pm_relay 부재/로드 실패 — sweep skip.")
            return
        ledger = self._resolve_raw_ledger(relay)
        if ledger is None:
            print("  raw 장부 위치 미해소 — sweep skip.")
            return
        print(f"  이 장부 기준: {ledger}")
        self._report_unfinished_raw(relay, ledger)
        self._report_peer_raw_ledgers(relay, ledger)

    def _report_unfinished_raw(self, relay, ledger: Path) -> None:
        """결정 공급 장부 1개의 미마감 판정 — 건수 + 최근 N건 (비차단·사유 1줄 후 skip)."""
        if not ledger.exists():
            print("  raw 장부 없음 — 미마감 raw 0건.")
            return
        try:
            rows = relay.unfinished_raw_records(ledger)
        except Exception as exc:  # noqa: BLE001 — fail-soft: 손상 장부가 핸드오프를 막지 않는다.
            if _is_engine_rev_skew(exc):
                raise  # 조회는 deep 형제(`file_lock`)까지 들어간다 — 사본 skew 를 "조회 실패"로
                       # 접으면 부분 복사 사실이 비차단 1줄에 묻힌다(`_load_pm_relay` 와 같은 규칙).
            print(f"  ⚠ 미마감 raw 조회 실패 ({exc}) — sweep skip.")
            return
        if not rows:
            print("  미마감 raw 없음.")
            return
        print(
            f"  ⚠ 미마감 raw {len(rows)}건 — 비정상 종료 잔존. 전체 조회: "
            "`pm_delegate.py raw --unfinished`"
        )
        for row in rows[:UNFINISHED_RAW_DISPLAY_LIMIT]:
            print(
                f"    - {row.get('started_at')} · {row.get('surface')} · "
                f"{row.get('harness')} · role={row.get('role')} · id={row.get('id')}"
            )
        if len(rows) > UNFINISHED_RAW_DISPLAY_LIMIT:
            print(f"    … 외 {len(rows) - UNFINISHED_RAW_DISPLAY_LIMIT}건")

    def _peer_raw_ledgers(self, primary: Path) -> tuple[Path, ...]:
        """이 sweep 이 **안 보는** 사본 장부(worktree 등) — 판정은 위임 조회면 것을 재사용."""
        try:
            resolved_primary = primary.resolve()
            if self._peer_raw_ledger_files is not None:
                peers: tuple[Path, ...] = tuple(self._peer_raw_ledger_files)
            else:
                delegate = _load_pm_delegate()
                if delegate is None:
                    return ()
                peers = tuple(delegate._peer_engine_ledgers())
        except Exception as exc:  # noqa: BLE001 — fail-soft: 병기 실패가 핸드오프를 막지 않는다.
            if _is_engine_rev_skew(exc):
                raise  # 사본 skew 는 삼키지 않는다(`_load_pm_delegate` 와 같은 규칙).
            return ()
        return tuple(peer for peer in peers if peer != resolved_primary)

    def _report_peer_raw_ledgers(self, relay, primary: Path) -> None:
        """사본 장부가 실재하면 **경로와 미마감 건수**를 1줄씩 병기한다 (가시성만·비차단).

        통합 조회는 범위 밖이다 — 이 sweep 은 결정 공급 장부 하나만 판정하고, 다른 사본이
        있다는 사실과 그 건수만 알린다. 사본이 없으면(솔로 형상) 아무 줄도 내지 않는다.
        """
        for peer in self._peer_raw_ledgers(primary):
            try:
                count = len(relay.unfinished_raw_records(peer))
            except Exception as exc:  # noqa: BLE001 — fail-soft: 손상 사본이 핸드오프를 막지 않는다.
                if _is_engine_rev_skew(exc):
                    raise
                print(f"  ⚠ 사본 장부 조회 실패 ({peer}: {exc}) — 건수 미상.")
                continue
            print(
                f"  {'⚠ ' if count else ''}사본 장부 {peer} — 미마감 {count}건 "
                "(이 sweep 밖·통합 조회는 범위 밖)."
            )

    # ── 구 task state 호환 backfill (정상 생성은 bind_task 시점) ─────────────────────

    def _task_is_registered(self, task: str) -> bool:
        """task 장부 membership을 조회 전용으로 검증한다.

        task 생성 권한은 bootstrap(`bind_task`)에만 있다. handoff는 기존 task의 연속성만
        기록하므로, state backfill·회귀·log/dashboard write 전에 장부 membership을 확인해
        오타 task가 고아 state/log/dashboard를 만드는 것을 막는다.
        """
        wp = self._worktree_pool or _load_worktree_pool()
        # membership도 같은 strict 장부 파서를 우선 사용한다. 그렇지 않으면 손상 JSON을
        # "미등록 task"로 오인해 held-slot strict gate에 도달하기 전에 원인을 숨긴다.
        finder = getattr(wp, "find_task_strict", None) if wp is not None else None
        if not callable(finder):
            finder = getattr(wp, "find_task", None) if wp is not None else None
        if not callable(finder):
            print(
                "\n[중단] task 장부 membership을 검증할 수 없다 — "
                "worktree_pool.find_task 부재. task 생성/재개는 bootstrap에서 먼저 수행하라.",
                file=sys.stderr,
            )
            return False
        try:
            registered = finder(task)
        except Exception as exc:  # noqa: BLE001 — membership 판정 불가는 생성보다 중단이 안전.
            print(
                f"\n[중단] task 장부 membership 조회 실패 — {exc}. "
                "task 상태를 만들거나 기록하지 않는다.",
                file=sys.stderr,
            )
            return False
        if registered is None:
            print(
                f"\n[중단] 미등록 task {task!r} — handoff는 task를 생성하지 않는다. "
                "bootstrap으로 task를 생성/재개한 뒤 다시 시도하라. "
                "(log/current.md·pm_state·dashboard 어떤 것도 건드리지 않는다.)",
                file=sys.stderr,
            )
            return False
        self._task_record_snapshot = (task, registered)
        return True

    def _ensure_task_pm_state(self, task: str) -> bool:
        """task pm_state가 존재함을 보장한다.

        정상 경로에서는 bootstrap의 `worktree_pool.bind_task`가 task 생성/재개 시 이미 state를
        만든다. 여기서는 구 엔진에서 state 없이 남은 task를 handoff 직전 호환 backfill한다.
        생성 로직을 복제하지 않고 worktree_pool의 단일 primitive만 소비한다.
        """
        if self._pm_state_file.exists():
            return True
        wp = self._worktree_pool or _load_worktree_pool()
        primitive = getattr(wp, "ensure_task_pm_state", None) if wp is not None else None
        if not callable(primitive):
            print(
                "\n[중단] task pm_state가 없고 worktree_pool.ensure_task_pm_state도 사용할 수 없다 — "
                "task state 없이 handoff하지 않는다.",
                file=sys.stderr,
            )
            return False
        try:
            self._pm_state_file = Path(primitive(task))
        except Exception as exc:  # noqa: BLE001 — state 생성 실패는 연속성 손실이므로 fail-loud.
            print(
                f"\n[중단] task pm_state 생성 실패 — {exc}. task state 없이 handoff하지 않는다.",
                file=sys.stderr,
            )
            return False
        print(f"  ✓ 구 task pm_state backfill ({self._pm_state_file})")
        return True

    # ── slot 대시보드 자기 섹션 overwrite step ───────────

    def _write_dashboard_section(
        self,
        session_identity: str,
        session_num: int | str,
        wave_summary: str,
        date_str: str,
    ) -> Path:
        """대시보드(`wiki/log/dashboard.md`)의 자기 섹션(`## <session_identity>`)을 overwrite 한다.

        차수·wave 요약·claimed 티켓을 3~5줄로 렌더(`render_dashboard_section`)해 자기 섹션만
        교체한다(`upsert_dashboard_section`·타 슬롯 byte 불변). 파일/섹션은 lazy 생성. claimed
        티켓은 board tickets 스캔(fail-soft·부재면 줄 생략). read→upsert→write 는 **파일락
        (`_dashboard_lock`) 안에서 직렬화**한다 — cross-slot 동시 핸드오프의 lost update 차단
        (MF-2·codex). 반환: 실제 write 한 대시보드 경로.
        """
        dash_file = (
            self._dashboard_file if self._dashboard_file is not None else _dashboard_file()
        )
        section = render_dashboard_section(
            session_identity,
            session_num=session_num,
            wave_summary=wave_summary,
            claimed_tickets=_claimed_tickets_for_session(session_identity),
            date=date_str,
        )
        # read-modify-write 전체를 락 안에서 — 다른 슬롯이 사이에 write 해도 최신 파일을 다시
        # 읽어 그 슬롯 섹션을 보존한다(직렬화·lost update 0·타 슬롯 byte 불변). write 는
        # tmp 파일 → `os.replace` 원자 교체 — crash/동시 read 시 빈·부분 파일 노출 차단
        # (worktree_pool `_write_ledger` 동형·codex suggestion).
        with _dashboard_lock():
            existing = dash_file.read_text(encoding="utf-8") if dash_file.exists() else ""
            updated = upsert_dashboard_section(existing, session_identity, section)
            dash_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = dash_file.with_suffix(dash_file.suffix + ".tmp")
            tmp.write_text(updated, encoding="utf-8")
            os.replace(str(tmp), str(dash_file))
        return dash_file

    # ── 메인 흐름 ─────────────────────────────────────────────────────────────

    def run(
        self,
        session_num: int | str | None,
        wave_summary: str,
        dry_run: bool,
        skip_pytest: bool,
        worktree_slot: str | None = None,
        branch: str | None = None,
        done: bool = False,
        task: str | None = None,
        ack_dirty: str | None = None,
        auto_trigger: bool = False,
        user_ack: str | None = None,
    ) -> int:
        """PM 핸드오프 7단계 자동화 전체 흐름을 실행한다.

        worktree_slot/branch: multi-PM 모드— handoff entry 에 slot/branch 를
            기록해 회전 재부착 연속성 단서를 남긴다. 미지정(솔로)이면 현행 lean 스키마 보존.
        done: 작업완료(--done) — worktree 슬롯을 release(idle 반납). worktree_slot
            필요. 미지정이면 release 안 함(세션종료/회전 ≠ release).
        task: task 모드 — 세션 종료의 연속성 앵커를 slot→task 로 이동한다. pm_state 를
            `.local/tasks/<task>/` 에 기록(task 생성 시 이미 존재)·dashboard 자기 섹션은
            `## <task>`·log 헤더 태그는 `(task:<task>)`. lease 는 유지한다(세션 종료 ≠ task 종료).
            repo/slot 입력 없이 보유 작업공간 집합을 자동 수령한다. task-only(슬롯 0개)도 정상이다.
        ack_dirty: dirty-tree 게이트([0/7]) override 사유. 비어있지 않은 문자열이어야 하며
            (빈 사유는 부작용 0 로 중단) 사유는 단일행으로 평탄화돼 handoff entry 에 박제된다.
        auto_trigger: 사용자 명시 핸드오프 호출부의 호환 신호. dirty-tree 게이트를 차단 대신
            loud 경고 + 사유 자동 박제로 강등하지만, 독자 트리거나 승인으로 취급하지 않으며
            ``user_ack`` 값-결속 계약을 그대로 적용한다.
        user_ack: 사용자 발화에서 받은 핸드오프 대상 승인값. 세션이 만들거나 자동 부착하면
            안 되며, 실제 실행은 해소된 task/slot 대상값과 정확히 일치해야 한다.

        반환: 0=성공, 1=실패 (중단).
        """
        # ack 사유는 **박제가 목적**이라 빈 문자열이면 override 를 받아들이지 않는다. CLI(argparse)
        # 뿐 아니라 run() 직접 호출도 같은 경계를 탄다 — 어떤 파일도 건드리기 전에 판정한다.
        if ack_dirty is not None and not ack_dirty.strip():
            print(
                "\n[중단] --ack-dirty 는 사유가 필수다 — 빈 사유로는 dirty 를 override 할 수 없다 "
                "(log/current.md·pm_state.md 어떤 것도 건드리지 않는다).",
                file=sys.stderr,
            )
            return 1
        # task 차수는 caller 입력을 신뢰하지 않는다. CLI는 명시 `--session-seq`를 usage error로
        # 거부하고, 엔진 직접 호출은 전달값을 폐기한 뒤 회귀 게이트 통과 후 task pm_state에서만
        # 추론한다. 따라서 어떤 호출 경로도 임의 차수를 log/state/dashboard에 영속할 수 없다.
        if task is not None:
            session_num = None

        # task는 독립 정체성이다. 보유 작업공간 집합은 slots_for_task(task)로 자동 수령하며,
        # repo/slot/branch/done 혼합 경로는 허용하지 않는다. Python CLI와 run() 직접 호출 모두
        # 같은 경계를 적용한다.
        if task is not None and (
            worktree_slot is not None or branch is not None or done
        ):
            print(
                "\n[중단] task handoff는 `--task <이름>` 정체성만 받는다 — "
                "`--repo/--slot/--branch/--done`과 함께 쓸 수 없다.",
                file=sys.stderr,
            )
            return 1

        # run() 직접 호출도 CLI ingress와 같은 canonical slot 도메인을 강제한다. 파싱 실패를
        # `solo`로 강등하면 malformed 경로에 solo ack가 결속되므로, ack 판정과 모든 pipeline
        # 진입보다 앞에서 부작용 0으로 거부한다.
        if worktree_slot is not None and _parse_worktree_slot(worktree_slot) is None:
            print(
                f"\n[중단] worktree_slot {worktree_slot!r} 이(가) 부적합 — "
                "canonical `work/<repo>_<N>` 형식이어야 한다 "
                "(log/current.md·pm_state.md 어떤 것도 건드리지 않는다).",
                file=sys.stderr,
            )
            return 1

        # task 이름 검증 — main() CLI 뿐 아니라 run() 직접 호출도 우회 못 하게 엔진층 단일 choke
        # (validate_task_name_engine)를 *어떤 task 소비(pm_state 경로·log 태그·dashboard·트리거)
        # 이전에* 통과시킨다. traversal(`../evil`)·whitespace(`my task`)·슬롯 예약패턴이 pm_state 디렉토리
        # 부적합/엔진부재는 부작용 0 로 중단(1).
        if task is not None:
            try:
                validate_task_name_engine(task)
            except Exception as exc:  # noqa: BLE001 — InvalidTaskName(.reason)·RuntimeError(엔진 부재)
                if _is_engine_rev_skew(exc):
                    raise
                print(
                    f"\n[중단] --task 이름 {task!r} 이(가) 부적합 — {getattr(exc, 'reason', exc)} "
                    "(log/current.md·pm_state 어떤 것도 건드리지 않는다.)",
                    file=sys.stderr,
                )
                return 1
            # 이름이 유효해도 장부에 없는 task는 handoff가 만들 수 없다. 생성 권한은 bootstrap
            # (`bind_task`) 전용이며, 이 read-only membership gate는 회귀·state backfill 및 모든
            # 영속 write보다 앞선다(오타로 인한 고아 state/log/dashboard 부작용 0).
            if not self._task_is_registered(task):
                return 1
            # task 보유 슬롯은 log/pm_state/dashboard 및 재스냅 어떤 영속 변경보다 먼저 한 번
            # 해소한다. 실패를 실제 0슬롯으로 바꾸면 회귀·출하 검사를 전부 skip한 뒤 기록까지
            # 완료하는 false-green이 되므로 fail-loud. 성공 스냅샷(빈 tuple 포함)은 전 단계가 재사용한다.
            self._task_slots_snapshot = None
            try:
                held_slots = self._task_held_slots(task)
            except Exception as exc:  # noqa: BLE001 — 해소 실패 ≠ 실제 빈 집합.
                if _is_engine_rev_skew(exc):
                    raise
                print(
                    f"\n[중단] task {task!r} 보유 슬롯 장부 해소 실패 — {exc}. "
                    "실제 0슬롯으로 간주하지 않습니다. "
                    "log/current.md·pm_state·dashboard·git 재스냅 어떤 것도 기록하지 않습니다.",
                    file=sys.stderr,
                )
                return 1
            self._task_slots_snapshot = (task, tuple(held_slots))
        date_str = datetime.date.today().isoformat()
        # task 모드 — 명시 pm_state 주입(hermetic 테스트) 없이 `--task` 가 오면 연속성 앵커를
        # task 로 잡는다. 명시 주입은 hermetic 경로 보존(주입 pm_state 를 그대로 씀).
        task_mode = task is not None and not self._pm_state_file_explicit
        # release(--done)는 *명시* 슬롯만 반납한다(비가역) — 자동해소 슬롯을 release 하지 않게
        # 원래 명시 인자를 별도 보존. 슬롯 자동해소(아래)는 read/write 연속성 경로에만 적용.
        explicit_worktree_slot = worktree_slot
        # session-entry 슬롯 해소 + 실행 슬롯 threading (codex round2 must-fix) — bare
        # handoff(`--repo`/`--slot` 미지정)에서 default-1/단독/idle-필터 슬롯을 *한 번* 해소해
        # 실행 슬롯에 박는다. 그러면 downstream 전부(pm_state·회귀cwd·handoff entry)가 *명시
        # 슬롯 우선* 경로로 같은 슬롯을 일관되게 쓴다(self-split 에서 회귀를 활성 worktree 서
        # 돌림·continuity/회귀cwd 비대칭 제거). 멀티-PM 모호면 fail-loud(slot 안 박고 중단).
        # 명시 주입(테스트·_pm_state_file_explicit)은 슬롯 해소를 건너뛴다(hermetic 경로 보존).
        # task-only면 슬롯 자동해소를 우회한다 — task는 슬롯 0개로도 동작하고, 보유 집합은
        # slots_for_task(task)가 별도로 해소한다.
        if not self._pm_state_file_explicit and not task_mode:
            resolved_slot, ambiguity = _resolve_session_worktree_slot(worktree_slot)
            if ambiguity is not None:
                print(
                    f"\n[중단] 슬롯 해소 모호 — {ambiguity} "
                    "(log/current.md·pm_state.md 어떤 것도 건드리지 않는다.)",
                    file=sys.stderr,
                )
                return 1
            if resolved_slot is not None and _parse_worktree_slot(resolved_slot) is None:
                print(
                    f"\n[중단] 해소된 worktree_slot {resolved_slot!r} 이(가) 부적합 — "
                    "canonical `work/<repo>_<N>` 형식이어야 한다 "
                    "(log/current.md·pm_state.md 어떤 것도 건드리지 않는다).",
                    file=sys.stderr,
                )
                return 1
            # 해소 결과를 실행 슬롯으로 thread한다. None도 legacy solo라는 **확정 스냅샷**이므로
            # 그대로 결속하고 downstream이 lease를 다시 조회하지 못하게 한다.
            worktree_slot = resolved_slot

        # 사용자 ack는 CLI가 아니라 핵심 엔진 경계에서 검증한다. bare slot은 바로 위에서 딱 한 번
        # 해소한 값을 실제 실행 전체에 thread하고, **그 동일 값**에 ack를 결속한다. 여기서 재조회하면
        # 두 조회 사이 lease 변화로 승인 대상과 실행 대상이 갈리는 TOCTOU가 되므로 금지한다.
        if not require_handoff_user_ack(
            user_ack,
            task=task,
            worktree_slot=worktree_slot,
            dry_run=dry_run,
        ):
            return 1
        # 회귀 cwd 해소용 — _default_run_pytest 가 _regression_cwd 에 넘긴다.
        # 명시/해소 슬롯이 있으면 그 worktree, 해소값 None이면 이 실행 내내 REPO(solo)로 고정.
        self._worktree_slot = worktree_slot
        self._worktree_slot_resolution_frozen = True
        # per-slot pm_state 경로 해소— 명시 주입(테스트)이 없을 때만. 진입부에선
        # **읽기 위치(target 경로)만 정하고 파일은 옮기지 않는다**(migrate=False) — 회귀/출하
        # 게이트(아래)가 red 면 "중단 시 pm_state 무접촉" 보장을 지켜야 하므로, legacy→slot
        # 이동은 *모든 중단 게이트 통과 후·pm_state 첫 접촉 직전*([3/7] 앞)에 1회 수행한다.
        # task 모드면 슬롯 대신 task 서술 공간(`.local/tasks/<task>/pm_state.md`)을 앵커로 쓴다.
        if not self._pm_state_file_explicit:
            if task_mode:
                self._pm_state_file = _task_pm_state_file(task)
            elif worktree_slot is None:
                # run()의 1회 해소 결과 None을 legacy solo에 직접 결속한다. `_pm_state_path(None)`은
                # lease를 재조회하므로 호출하지 않는다(MF-2/NEW-1의 TOCTOU 공통 뿌리).
                self._pm_state_file = _legacy_pm_state_file()
            else:
                self._pm_state_file = _pm_state_path(worktree_slot, migrate=False)
        print(
            f"[pm_handoff] PM {'task-state 자동 추론' if task is not None else str(session_num) + '차'} "
            "핸드오프 시작 "
            f"(dry_run={dry_run}, skip_pytest={skip_pytest}, "
            f"worktree_slot={worktree_slot}, done={done})"
        )

        # ── 0. dirty-tree 게이트 (조기 중단) ──────────────────────────────────
        # "핸드오프 시작 시점 tree 는 clean" 불변식의 기계 차단. 위치가 load-bearing 이다 —
        # 어떤 mutation(task state 보장·task pid=0 기록·[2/7] log append)보다도 앞이라 차단돼도
        # 재실행이 중복 entry 를 만들지 않는다. 회귀([1/7]) **앞**에 두는 이유는 비용이다:
        # 판정은 git 조회 몇 번(초 단위)인데 회귀는 수 분이라, 뒤에 두면 커밋만 하면 될 PM 이
        # 회귀 한 판을 통째로 기다린 뒤에야 차단 사유를 본다.
        print("\n[0/7] dirty-tree 게이트 (미커밋 잔여 — 첫 mutation 전 판정)...")
        dirty_rc, dirty_ack_note = self._dirty_tree_gate(
            task, ack_dirty=ack_dirty, auto_trigger=auto_trigger, dry_run=dry_run,
        )
        if dirty_rc != 0:
            return dirty_rc

        # ── 1. 회귀 측정 ───────────────────────────────────────────────────────
        # task 회귀 모드: task-only 정체성으로 변경 흔적(lease 스냅 대비
        # head 전진/dirty) 있는 **보유 슬롯 각각**에서 회귀한다(0개면 명시 skip).
        # slot/솔로 모드(task None)는 기존 단일 cwd 경로다.
        task_regression = task is not None
        # task 변경-슬롯 집합 — [1b] 출하 변경 surface 가 같은 집합을 공유한다(codex must-fix). None =
        # task 모드 미진입(slot/솔로 → 아래 단일 cwd surface). task 모드면 **회귀 skip(--no-pytest)
        # 여도** 변경 슬롯을 열거해 [1b] 에 전달한다(열거는 비용 미미한 git 조회뿐이라
        # skip_pytest 여도 수행·[1b] REPO 폴백 단일 검사로의 후퇴 방지).
        task_shipping_slots: list[str] | None = None
        print("\n[1/7] 회귀 측정...")
        if dry_run:
            # ack 면제의 전제는 쓰기 0이다. pytest는 프로젝트 코드가 아니어도 cacheprovider 등
            # 플러그인이 파일을 만들 수 있으므로 dry-run에서는 runner 자체를 호출하지 않는다.
            print("  [dry-run] 순수 미리보기 — pytest 실행 생략(캐시 생성 없음).")
            pytest_summary = "(dry-run 미실행)"
            if task_regression:
                changed = self._classify_task_changed_slots(task)
                task_shipping_slots = [slot for slot, _reason in changed]
        elif skip_pytest:
            print("  [--no-pytest] 회귀 측정 skip.")
            pytest_summary = "(skip)"
            if task_regression:
                # 회귀는 skip 하되 변경-슬롯 열거는 수행([1b] 출하 surface 가 같은 집합을 공유).
                changed = self._classify_task_changed_slots(task)
                task_shipping_slots = [slot for slot, _reason in changed]
        elif task_regression:
            changed = self._classify_task_changed_slots(task)
            ran_slots = self._run_task_regressions(changed, dry_run)
            if ran_slots is None:   # 변경 슬롯 중 하나라도 red — 핸드오프 차단.
                return 1
            task_shipping_slots = ran_slots
            pytest_summary = "(task 슬롯별 회귀·위 참조)"
        else:
            returncode, output = self._run_pytest_fn()
            print(output.rstrip())
            if not is_pytest_green(output, returncode):
                print(
                    "\n[중단] 회귀 red — 핸드오프 불가. log/current.md·pm_state.md 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                return 1
            pytest_summary = parse_pytest_summary(output)
            print(f"  ✓ green: {pytest_summary}")

        # ── 1b. 출하 변경 surface ───────────────
        # 기계회귀 green 직후 미push diff ∩ SHIPPING_GLOBS 를 분류해 미검증 출하 변경이
        # 있으면 "릴리즈 전 라이브 필요" 1줄을 surface 한다 — **비차단**(rc 무영향·핸드오프
        # 지연 0). 라이브 LLM 검증은 릴리즈 단일 지점(release wave)으로 모았다
        # dry_run 은 git 분류도 건너뛴다(미리보기). worktree 는 회귀와 같은 cwd.
        # task 회귀 모드(task_shipping_slots 비-None): 회귀가 돈 **변경-슬롯 각각**에서 surface —
        # 단일 _regression_cwd(None) 자동해소가 엉뚱한 트리를 보거나 일부 슬롯의 출하 변경을 놓치는
        # 것을 막는다(집합 1급화 일관·codex must-fix). slot/솔로/skip_pytest 는 기존 단일 cwd.
        print("\n[1b/7] 출하 변경 surface (비차단·릴리즈 라이브는 release wave)...")
        if dry_run:
            print("  [dry-run] 출하 변경 surface skip (미리보기).")
        elif task_shipping_slots is not None:
            self._shipping_surface_for_slots(task_shipping_slots)
        else:
            worktree = self._run_regression_cwd(self._worktree_slot)
            self._shipping_surface_step(worktree)

        # task state 보장/차수 추론은 회귀·출하 게이트를 통과한 뒤, log/dashboard 등 첫 영속 변경
        # **전**에 끝낸다(MF-b). 구 task backfill 실패 시 외부 상태는 아직 무접촉이라 재시도해도
        # log 중복이 생기지 않는다. dry-run은 state를 만들지 않되 기존 state에서 같은 추론을 한다.
        if task is not None:
            if not dry_run and task_mode and not self._ensure_task_pm_state(task):
                return 1
            if not self._pm_state_file.exists():
                print(
                    f"\n[중단] task pm_state가 없다: {self._pm_state_file} — "
                    "차수를 임의 지정하지 않고 task state를 먼저 복구하라.",
                    file=sys.stderr,
                )
                return 1
            inferred = infer_next_task_session_num(
                self._pm_state_file.read_text(encoding="utf-8"),
                _read_log_text_exact(self._log_file) if self._log_file.exists() else "",
                task,
                task_released=(
                    self._task_record_snapshot is not None
                    and self._task_record_snapshot[0] == task
                    and getattr(self._task_record_snapshot[1], "pid", None) == 0
                ),
            )
            if not isinstance(inferred, int):
                print(
                    f"\n[중단] task {task!r} pm_state에서 현재 차수를 추론할 수 없다 — "
                    "task state를 복구하라.",
                    file=sys.stderr,
                )
                return 1
            session_num = inferred
            print(f"  ✓ task state+log 차수 자동 복구: {session_num}차")

        # task의 pid=0은 이제 같은-session 재실행을 판별하는 load-bearing handoff intent다.
        # log/state/dashboard 어떤 handoff write보다 먼저 기록해, 이후 단계가 중단돼도 재실행이
        # 같은 차수로 복구되게 한다. dry-run은 예고만 하고 장부를 쓰지 않는다.
        if task_mode:
            print("\n[task] handoff intent pid=0 기록 (log write 전)...")
            if dry_run:
                print(f"  [dry-run] task pid=0(미점유) 기록 예고: {task} (실행 생략).")
            elif not self._release_task_pid(task):
                return 1

        # ── 2. log/current.md handoff entry append / 같은-세션 갱신 ────────────────────
        print("\n[2/7] log/current.md handoff entry append/갱신...")
        # 세션 정체성 태그— 해소된 슬롯(`work/<repo>_<N>`)에서 canonical `<repo>_<N>`
        # 를 유도해 헤더에 박는다(감사 메타·상태 저장 아님·무충돌·태그 값에 `work/`
        # 프리픽스 없음). 솔로(미해소)면 None → 태그 생략·현행 헤더 byte-호환.
        # task 모드: 연속성 앵커 = task. dashboard 자기 섹션 = `## <task>`(verbatim·interface 2),
        # log 헤더 태그 = `(task:<name>)`(sentinel·서술괄호/슬롯태그와 기계 구분·interface 3). 두 표면의
        # 요구가 달라(사람 가독 vs 기계 파싱) 값을 분리한다. slot 모드는 둘 다 canonical `<repo>_<N>`.
        if task is not None:
            session_identity = task
            log_session_tag = f"{_TASK_TAG_PREFIX}{task}"
        else:
            _parsed_slot = _parse_worktree_slot(worktree_slot)
            session_identity = f"{_parsed_slot[0]}_{_parsed_slot[1]}" if _parsed_slot else None
            log_session_tag = session_identity
        normalized_session_num = _normalize_session_num(session_num)
        # ack 박제 자리를 못 찾으면(`DirtyAckStampError`) 통과시키지 않는다 — 사유를 잃은 채
        # 미커밋 세션이 넘어가는 것이 게이트가 막으려는 바로 그 상태다. 예외는 계획 단계에서
        # 나므로(write 앞) log 는 무접촉이다.
        try:
            if dry_run:
                log_text = (
                    _read_log_text_exact(self._log_file)
                    if self._log_file.exists() else ""
                )
                same_session_rerun, _payload, preview, handoff_warning = (
                    _prepare_handoff_log_change(
                        log_text,
                        session_num=normalized_session_num,
                        date=date_str,
                        worktree_slot=worktree_slot,
                        branch=branch,
                        session=log_session_tag,
                        task=task,
                        dirty_ack_note=dirty_ack_note,
                    )
                )
            else:
                # 판정+read-modify-write/append는 helper가 pm_log 공유 락 한 임계구역에서 원자화한다.
                same_session_rerun, preview, handoff_warning = _commit_handoff_log_change(
                    self._log_file,
                    session_num=normalized_session_num,
                    date=date_str,
                    worktree_slot=worktree_slot,
                    branch=branch,
                    session=log_session_tag,
                    task=task,
                    dirty_ack_note=dirty_ack_note,
                )
        except DirtyAckStampError as exc:
            print(
                f"\n[중단] dirty-tree ack 사유를 handoff entry 에 박제할 수 없다 — {exc}. "
                f"기존 entry 를 복구하거나 `{DIRTY_ACK_ENTRY_LABEL}: {dirty_ack_note}` 를 "
                "손으로 적은 뒤 재실행하라 (log/current.md 는 건드리지 않았다).",
                file=sys.stderr,
            )
            return 1

        if handoff_warning is not None:
            print(f"  ⚠ {handoff_warning}", file=sys.stderr)
        if same_session_rerun:
            print(
                f"[모드] 같은 차수({normalized_session_num}) 재실행 — "
                "entry 갱신·윈도 shift 생략"
            )
            if dirty_ack_note:
                print(f"  ✓ dirty-tree ack 사유 멱등 upsert: {dirty_ack_note}")
            if dry_run:
                print("  [dry-run] 기존 entry 본문 유지·기계 소유 구획 갱신 미리보기:")
                print("  " + preview.replace("\n", "\n  "))
            else:
                print(
                    f"  ✓ 기존 handoff entry 유지·박제 목록 갱신 "
                    f"(PM {normalized_session_num}차)"
                )
        elif dry_run:
            print("  [dry-run] log/current.md 에 append 할 skeleton:")
            print("  " + preview.replace("\n", "\n  "))
        else:
            print(
                f"  ✓ log/current.md handoff entry skeleton append "
                f"(PM {normalized_session_num}차)"
            )

        # log/current.md entry 누적 점검 — 임계 초과 시 archive 권장 (차단 아님).
        cur_log_text = _read_log_text_exact(self._log_file) if self._log_file.exists() else ""
        entry_count = len(_LOG_ENTRY_RE.findall(cur_log_text))
        if entry_count > LOG_ARCHIVE_SUGGEST_THRESHOLD:
            print(
                f"  ⚠ log/current.md entry {entry_count}개 > {LOG_ARCHIVE_SUGGEST_THRESHOLD} — "
                f"`pm_log.py archive --before <날짜>` 로 오래된 entry 봉인 권장 "
                f"(부트스트랩 읽기 비용 ↓).",
                file=sys.stdout,
            )

        # ── 2b. slot 대시보드 자기 섹션 overwrite ──────────
        # 세션 정체성 해소 시(멀티-PM 슬롯·`session_identity`)만 자기 섹션을 overwrite 한다 —
        # log entry append 와 같은 write 패스(타 슬롯 byte 불변·append 아님). 솔로(정체성 미해소
        # ·session_identity None)는 skip(무회귀·섹션 1개뿐이라 무의미). dry_run 은 write
        # 안 함(미리보기). 명시 pm_state 주입(hermetic 테스트)이어도 session_identity 가 있으면 쓴다
        # (대시보드 파일은 별도 seam `self._dashboard_file` 로 격리).
        print("\n[2b/7] slot 대시보드 자기 섹션 overwrite (수정형)...")
        if session_identity is None:
            print("  솔로(세션 정체성 미해소) — 대시보드 skip(무회귀).")
        elif dry_run:
            print(f"  [dry-run] 대시보드 자기 섹션 overwrite 예고: ## {session_identity}")
        else:
            dash_file = self._write_dashboard_section(
                session_identity,
                _normalize_session_num(session_num),
                wave_summary,
                date_str,
            )
            print(f"  ✓ 대시보드 자기 섹션 overwrite: {dash_file} (## {session_identity})")

        # ── per-slot 마이그레이션 (트랜잭션 보장) ─────────────────────────
        # 모든 중단 게이트(회귀 [1/7]·출하 [1b/7])를 통과한 *뒤*·pm_state 첫 접촉([3/7])
        # 직전에 legacy → slot 이동을 1회 수행한다. 게이트 red 면 여기 못 와 legacy 무접촉
        # (codex must-fix — "중단 시 pm_state 무접촉" 보존). dry_run 은 이동 안 함(미리보기 —
        # 진입부 migrate=False target 을 그대로 읽음). 명시 주입(테스트)은 재해소 안 함.
        # task 모드는 slot legacy 마이그레이션 대상이 아니다. task state 보장/차수 추론은
        # 첫 영속 변경 전 위에서 이미 끝냈다.
        if not dry_run and not self._pm_state_file_explicit:
            if not task_mode:
                if worktree_slot is None:
                    self._pm_state_file = _legacy_pm_state_file()
                else:
                    self._pm_state_file = _migrate_legacy_pm_state(worktree_slot)

        # ── 3·4. pm_state.md sliding window 정리 + 길이 검증 ───────────────────
        # pm_state.md 부재(board.py init 미실행 clone)는 치명 아님 — fail-soft.
        # 경고 후 3·4단계(세션 window 정리·길이 검증)를 skip 하고 나머지 진행.
        if not self._pm_state_file.exists():
            print(
                "\n[3-4/7] ⚠ pm_state.md 없음 — `board.py init` 미실행 clone. "
                "세션 식별 sliding window 갱신 skip. 핸드오프 계속.",
                file=sys.stderr,
            )
        else:
            # ── 3. pm_state.md sliding window 정리 ─────────────────────────────
            print("\n[3/7] pm_state.md 세션 식별 sliding window 정리...")
            state_text = self._pm_state_file.read_text(encoding="utf-8")

            try:
                # update_session_window 자체가 같은 차수면 byte no-op이다. 재실행에서도 이
                # primitive를 태워 step2(log) 뒤 step3(state) 전 중단된 반쪽 상태만 정확히
                # 복구하고, 이미 shift된 정상 재실행은 실제 write 없이 건너뛴다.
                new_state_text = update_session_window(
                    pm_state_text=state_text,
                    session_num=normalized_session_num,
                    date_str=date_str,
                    wave_summary=wave_summary,
                )
            except ValueError as exc:
                # fail-soft: 앵커/entry 불일치는 step3(sliding
                # window) *한정* 스킵하고 핸드오프는 완주한다. `return 1` 로 전체를 죽이면
                # 채택자 pm_state 의 미세 변형에 매 핸드오프가 무너진다(9세션 연속 수동 우회).
                # 추측 편집 금지 원칙은 유지 — window 를 지어내지 않되(원본 보존) 죽지도 않는다.
                print(
                    f"\n[3/7] ⚠ 세션 식별 sliding window 스킵 — {exc} "
                    "나머지 단계(4~7) 계속 진행.",
                    file=sys.stderr,
                )
                new_state_text = state_text  # window 미편집 — 원본 유지(step4 길이검증은 원본으로).
            else:
                if same_session_rerun:
                    if new_state_text == state_text:
                        print(
                            f"  ↷ 같은 차수({normalized_session_num}) 재실행 — "
                            "세션 window shift 생략(write 0·이미 반영)."
                        )
                    elif dry_run:
                        print(
                            f"  [dry-run] 같은 차수({normalized_session_num}) log 선행 반쪽 상태 — "
                            "세션 window 미반영분 복구 예고."
                        )
                    else:
                        self._pm_state_file.write_text(new_state_text, encoding="utf-8")
                        print(
                            f"  ✓ 같은 차수({normalized_session_num}) log 선행 반쪽 상태 — "
                            "세션 window 미반영분 복구."
                        )
                elif dry_run:
                    # diff 미리보기: 변경된 줄 출력
                    old_lines = state_text.splitlines()
                    new_lines = new_state_text.splitlines()
                    added = [l for l in new_lines if l not in set(old_lines)]
                    removed = [l for l in old_lines if l not in set(new_lines)]
                    print("  [dry-run] pm_state.md 세션 식별 절 변경 예고:")
                    for line in removed[:5]:
                        print(f"  - {line}")
                    for line in added[:5]:
                        print(f"  + {line}")
                else:
                    self._pm_state_file.write_text(new_state_text, encoding="utf-8")
                    print(
                        f"  ✓ pm_state.md 세션 식별 sliding window 정리 완료 "
                        f"(PM {session_num}차 추가·최근 최대 3개 유지)"
                    )

            # ── 4. pm_state.md 길이 검증 ────────────────────────────────────────
            print("\n[4/7] pm_state.md 길이 검증...")
            text_to_check = new_state_text if not dry_run else state_text
            line_count = len(text_to_check.splitlines())
            if line_count > PM_STATE_LINE_WARNING_THRESHOLD:
                print(
                    f"  ⚠ pm_state.md 길이 {line_count} 라인 > {PM_STATE_LINE_WARNING_THRESHOLD} 라인 임계값.",
                    file=sys.stdout,
                )
                print(
                    f"  ⚠ 과거 세션 정리 누락 신호 — §세션 식별 sliding window 정리를 점검하라.",
                    file=sys.stdout,
                )
            else:
                print(f"  ✓ pm_state.md {line_count} 라인 (임계값 {PM_STATE_LINE_WARNING_THRESHOLD} 이하).")

        # ── 5. 인계 프롬프트 stdout 출력 ───────────────────────────────────────
        # 템플릿(정적)은 pm_playbook.md 에서 추출한다 — sliding window 편집 대상(pm_state.md)과 분리.
        print("\n[5/7] 인계 프롬프트 출력...")
        playbook_text = self._pm_playbook_file.read_text(encoding="utf-8")
        prompt_output = build_handoff_prompt_output(
            pm_playbook_text=playbook_text,
            session_num=_normalize_session_num(session_num),
            wave_summary=wave_summary,
            date_str=date_str,
            worktree_slot=self._worktree_slot,
            # task 모드면 트리거를 task-only(`--task <task>`)로
            # 갭 보완(트리거=재개 명령 1:1). 정체성 판정 축은 이 run() 전체에서
            # `task is not None` 로 단일화한다(log 헤더 태그·dashboard 섹션·:2295 와 동축) — 명시
            # pm_state 주입(hermetic·_pm_state_file_explicit) 경로도 로그/대시보드가 task 로 처리하는
            # 한 트리거도 task 여야 일관(codex must-fix). slot/솔로(task None)는 100% 불변.
            task=task,
        )
        print(prompt_output)

        # ── 6. git status dump ─────────────────────────────────────────────────
        print("\n[6/7] git status dump...")
        git_rc, git_out = self._run_git_fn(["status", "-s"])
        if git_rc != 0:
            print(f"  ⚠ git status 실패 (rc={git_rc}): {git_out.rstrip()}")
        else:
            changed_files = [l for l in git_out.splitlines() if l.strip()]
            print(f"  변경 파일 수: {len(changed_files)}")
            if changed_files:
                print("  git status -s 출력:")
                for line in changed_files:
                    print(f"    {line}")
            else:
                print("  (변경 없음)")
        # 미push commit(ahead) — 커밋만 하고 push 를 안 한 세션이 그대로 넘어가면 다음 세션은
        # 원격에 없는 부기를 기준으로 판단한다(실측: PM 홈 2커밋 미push). 경고 1줄로만 세운다.
        ahead = _ahead_commit_count(self._run_git_fn)
        if ahead is None:
            print("  (미push commit 수 미해소 — upstream 미설정·detached·원격 부재)")
        elif ahead[0] > 0:
            print(
                f"  ⚠ 미push commit {ahead[0]}개 (기준 {ahead[1]}) — 아래 [7/7] push 단계에서 "
                "올려라(미push 부기는 다음 세션에 안 보인다)."
            )
        else:
            print(f"  미push commit 없음 (기준 {ahead[1]}).")

        # ── 6b. 미마감 raw sweep ───────────────────────────────────────────────
        print("\n[6b/7] 미마감 raw sweep (비차단·경고)...")
        self._unfinished_raw_step()

        # ── 7. 잔여 PM 수동 작업 출력 ──────────────────────────────────────────
        print("\n[7/7] PM 이 손으로 할 잔여 작업:")
        print(
            "  [ ] log/current.md handoff entry 본문 채우기 — lean 3섹션"
            "(읽기범위·메타학습·다음intent)+회귀/incident(회귀 1줄 baseline). "
            "board/git/log 대량 재열거 금지("
            f"{_runtime_skill_entry('pm-bootstrap')} 라이브)."
        )
        print("  [ ] domain capture 검토 — `domain.py capture --tickets \"T-0001,T-0002\"`(이 세션 done ticket ID·콤마분리 또는 공백 나열) 출력 보고 ⚠/gap 페이지 갱신/신설(채록·surface-only).")
        print("        갱신 = 현재-진실 *교체* — 세션별 delta 를 페이지에 덧붙이지 마라"
              "('언제 왜 바뀌었나'는 log/ADR 몫·domain lint `history` 축).")
        print("  [ ] pm_state.md '진행 중인 의사결정' 표 갱신")
        print("  [ ] pm_state.md '남은 작업 전체 그림' 갱신")
        # 커밋 지시는 **경로 명시형** 이다 (공유 워킹트리 mutation 은 선언된 경로만).
        # bare `git commit` 은 다른 슬롯이 index 에 올려둔 남의 변경까지 함께 싣는다. 이 도구가
        # 실제로 쓰는 산출물은 `log/current.md` 하나다(pm_state 는 gitignored 라 대상 밖) —
        # PM 이 이번 세션에 손으로 고친 wiki 문서가 있으면 그 경로를 뒤에 덧붙인다.
        print("  [ ] git commit — **경로를 명시**하라: "
              "`git commit -m \"<메시지>\" -- .project_manager/wiki/log/current.md "
              "<이번 세션에 고친 wiki 문서 경로들>` (Co-Authored-By: Claude 트레일러 포함)")
        # commit 다음 단계가 빠져 있으면 부기는 로컬에만 남는다 — 위 [6/7] ahead 경고와 짝이다.
        print("  [ ] PM 홈 push(private 자율) — commit 후 `git push`. "
              "보호 브랜치(공개 제품 repo)는 사용자 승인 게이트라 여기서 밀지 마라.")

        # ── 핸드오프 완료: 보유 슬롯 git 재스냅 ────
        # 부기(log·pm_state) 완료 후 슬롯의 live git 을 lease.git 에 재기록한다 — 세션 중 브랜치/HEAD
        # 변경(예: 릴리즈 v1.3.2→v1.3.3)이 차기 부트스트랩 0단계 record-vs-live 정합(compare_slot_git·
        # ㉒)을 `diverged` FAIL-LOUD 로 오탐시켜 정당한 자기 진행을 외부-개입 오경보로 차단하는 것을
        # 막는다. base 미전달=기존 보존(arrival 동형)·판정 재구현 없이 write
        # 프리미티브만 호출. --done(release→idle·git 정리)은 대상 아님 — idle 슬롯은 활성 git 기대가
        # 없다(다음 alloc 이 arrival 재스냅). dry_run 은 예고만. worktree_pool 부재·장부 부재는
        # _record_slot_snapshot 내부에서 fail-soft(무해 skip).
        #
        # task 모드: 세션은 보유 슬롯 **집합**을 두고 나가므로 재스냅은 **보유 전
        # 슬롯**(현행 1슬롯 한정 폐지·_record_task_slot_snapshots→slots_for_task 루프). slot/솔로 모드
        # (task None)는 단일 bound 슬롯 재스냅으로 100% 불변.
        if not done:
            if task is not None:
                if not self._record_task_slot_snapshots(task, dry_run):
                    return 1
            elif self._worktree_slot is not None:
                print("\n[재스냅] bound 슬롯 git 재스냅 (여기 두고 간다)...")
                if dry_run:
                    print(f"  [dry-run] git 재스냅 예고: {self._worktree_slot} (실행 생략).")
                else:
                    self._record_slot_snapshot(self._worktree_slot)

        # ── multi-PM 모드: --done 작업완료 슬롯 release ─────────────────
        # 세션종료/회전 ≠ release — --done 명시 시에만 슬롯을 idle 반납한다. release 는 비가역
        # 이라 *명시* `--repo`/`--slot`(explicit_worktree_slot)만 반납한다 — 자동해소(default-1)
        # 슬롯은 read/write 연속성에만 쓰고 release 하지 않는다(의도치 않은 반납 차단).
        if done:
            if not explicit_worktree_slot:
                print(
                    "\n[중단] --done 은 --repo <name> [--slot <N>] 이 필요하다 (어느 슬롯을 반납할지).",
                    file=sys.stderr,
                )
                return 1
            print("\n[multi-PM] --done 작업완료 — worktree 슬롯 release...")
            if dry_run:
                print(f"  [dry-run] worktree 슬롯 release 예고: {explicit_worktree_slot} (실행 생략).")
            else:
                rc = self._release_slot(explicit_worktree_slot)
                if rc != 0:
                    return rc

        if dry_run:
            print("\n[dry-run] 완료 — 실제 파일 편집은 실행하지 않았다.")
        else:
            print(f"\n[완료] PM {session_num}차 핸드오프 자동화 완료.")

        return 0


# ── 오형식 차수 정규화 CLI 진입 (--normalize-session-anchors) ────────────

def _run_normalize_session_anchors(worktree_slot: str | None, dry_run: bool) -> int:
    """`--normalize-session-anchors` 진입 — 활성 슬롯 pm_state.md 의 오형식 `**N차차+**` 를
    `**N차**` 로 멱등·비파괴 정규화한다.

    - 대상 = `_pm_state_path(..., migrate=False)` (읽기 위치·마이그레이션 안 함·부작용 0).
      슬롯 미해소(솔로)면 legacy `wiki/pm_state.md`, 슬롯 해소면 per-slot 경로.
    - 변경 preview(unified diff)를 항상 출력한다(비파괴 선검토).
    - `--dry-run` 이면 파일을 교체하지 않는다(선검토). 없으면 diff 출력 후 교체 write.
    - 파일 부재·변경 없음(이미 정합·멱등)은 rc0 no-op.

    반환: 0 (정규화 적용/미리보기/no-op 전부 성공).
    """
    target = _pm_state_path(worktree_slot, migrate=False)
    print(f"[normalize-session-anchors] 대상 pm_state: {target}")
    if not target.exists():
        print("  대상 파일이 없다 — 정규화할 것이 없다(no-op).")
        return 0

    original = target.read_text(encoding="utf-8")
    normalized = normalize_session_anchors(original)
    if normalized == original:
        print("  오형식 `**N차차+**` 없음 — 이미 정합(변경 없음·멱등 no-op).")
        return 0

    print("  변경 preview (오형식 `**N차차+**` → `**N차**`·세션 식별 절만):")
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        normalized.splitlines(keepends=True),
        fromfile="pm_state.md (before)",
        tofile="pm_state.md (after)",
    )
    for line in diff:
        # unified_diff 는 컨텍스트 줄에 keepends 로 개행을 보존하지만 헤더/eof-no-newline
        # 줄엔 개행이 없을 수 있어 end 를 조건부로 붙인다.
        print(line, end="" if line.endswith("\n") else "\n")

    if dry_run:
        print(
            "  [dry-run] 파일 미변경 — 위 diff 를 검토한 뒤 --dry-run 없이 재실행해 적용하라 "
            "(비파괴·멱등)."
        )
        return 0

    target.write_text(normalized, encoding="utf-8")
    print(f"  ✓ 정규화 적용 완료 — {target} 교체 (멱등: 재실행 시 무변화).")
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def _handoff_user_ack_target(
    task: str | None,
    worktree_slot: str | None,
) -> tuple[str, str]:
    """핸드오프 승인 토큰의 canonical 대상값과 사람이 읽는 대상 설명을 반환한다."""
    if task is not None:
        return task, f"task {task!r}"
    parsed = _parse_worktree_slot(worktree_slot)
    if parsed is not None:
        repo, slot = parsed
        target = f"{repo}_{slot}"
        return target, f"slot {target!r}"
    # legacy solo에는 task/물리 slot 식별자가 없다. 기존 solo 동작을 보존하면서도 값-결속
    # 게이트를 우회하지 않도록 논리적 단일 slot sentinel을 공개 대상값으로 쓴다.
    return "solo", "legacy solo slot 'solo'"


def require_handoff_user_ack(
    user_ack: str | None,
    *,
    task: str | None,
    worktree_slot: str | None,
    dry_run: bool,
) -> bool:
    """기존 값-결속 ack 관용구로 실제 핸드오프만 사용자 승인 게이트한다."""
    target, description = _handoff_user_ack_target(task, worktree_slot)
    if dry_run:
        print(
            f"[dry-run] 쓰기 0 미리보기는 사용자 ack 없이 허용한다. 실제 {description} "
            "핸드오프를 실행하려면 사용자 명시 승인이 필요하다 — 먼저 사용자에게 핸드오프 "
            f"여부를 확인하라. 승인한 사용자만 `--user-ack {target}`를 대상값 그대로 "
            "붙여 실행하라(세션 자동 부착 금지)."
        )
        return True
    if user_ack == target:
        print(
            f"[승인 감사] pm_handoff: 사용자 승인 토큰이 {description} 대상값 {target!r}에 "
            "값-결속됨."
        )
        return True
    mismatch = (
        f" 제공된 값 {user_ack!r}은 대상값 {target!r}에 결속되지 않았다."
        if user_ack is not None
        else ""
    )
    print(
        f"[중단] pm_handoff {description}: 세션 핸드오프에는 사용자 명시 승인이 필요하다."
        f"{mismatch}\n"
        "  1순위: 사용자에게 핸드오프 여부를 확인하라.\n"
        f"  부차 수단: 승인한 사용자만 `--user-ack {target}`를 대상값 그대로 붙여 "
        "실행하라(세션 자동 부착 금지).",
        file=sys.stderr,
    )
    return False

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_handoff.py",
        description="PM 핸드오프 7단계 자동화 헬퍼.",
    )
    # slot/solo 경로에서는 session-seq·wave-summary 필수. task 경로는 task pm_state에서 차수를
    # 추론하고 기본 요약을 생성하므로 `--task <이름>` 하나로 실행 가능하다.
    # 차수는 "세션 정체성"이 아니라 "슬롯 시퀀스" — 정체성(--repo/--slot)과는 별개
    # — actor `--session` 삭제로 이름충돌 없이 `--session-seq` 유지).
    parser.add_argument(
        "--session-seq",
        metavar="N",
        default=None,
        help="떠나는 PM 세션 차수 (예: 28). slot/solo 경로 필수, task 경로에서는 지정 금지·state 자동 추론.",
    )
    parser.add_argument(
        "--wave-summary",
        metavar="요약",
        help="떠나는 PM 세션의 wave 종합 1~2 줄 요약. slot/solo 필수, task 경로 기본값 자동.",
    )
    # ── multi-PM 모드 정체성 — 솔로 미지정이면 미사용·현행 보존 ──
    # canonical = 분해형 `--repo <name> [--slot <N>]`(전 CLI 통일·구 alias --session/
    # --worktree-slot/--session-num 은 즉시 삭제). --repo 단독은 활성(leased) 슬롯 1개면
    # 자동해소, 0개/≥2개는 fail-loud(`_resolve_explicit_identity_slot`).
    identity_args.add_identity_args(parser)
    parser.add_argument(
        "--branch",
        metavar="브랜치",
        default=None,
        help="multi-PM 모드 — 이 세션의 작업스트림 브랜치 (--repo/--slot 과 함께·handoff entry 기록).",
    )
    parser.add_argument(
        "--done",
        action="store_true",
        help=(
            "multi-PM 모드 — 작업완료 시 worktree 슬롯을 release(idle 반납). "
            "--repo <name> [--slot <N>] 필요. 미지정이면 세션종료/회전 ≠ release(리스 유지)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="파일 편집 없이 변경 미리보기.",
    )
    parser.add_argument(
        "--user-ack",
        metavar="<task 또는 slot>",
        default=None,
        help=(
            "사용자가 명시 승인한 핸드오프 대상값과 정확히 결속하는 토큰"
            "(task 이름 또는 canonical <repo>_<N>; legacy solo slot은 `solo`). "
            "--dry-run 미리보기는 생략 가능."
        ),
    )
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help="회귀 측정 skip (기본 측정·대화형 경로).",
    )
    parser.add_argument(
        "--ack-dirty",
        metavar="사유",
        default=None,
        help=(
            "dirty-tree 게이트([0/7]) override — 미커밋 잔여를 두고 나가는 **사유**가 필수다"
            "(bare 플래그·빈 사유 거부). 사유는 단일행으로 평탄화돼 handoff entry 에 박제된다."
        ),
    )
    parser.add_argument(
        "--auto-trigger",
        action="store_true",
        help=(
            "사용자가 핸드오프를 명시 지시한 호출부를 위한 호환 신호. dirty-tree 게이트를 "
            "차단 대신 loud 경고 + 사유 자동 박제로 강등한다. 독자 트리거·승인이 아니며 "
            "--user-ack 값-결속 계약을 우회하지 않는다."
        ),
    )
    # ── 유지보수 모드 (핸드오프 7단계와 독립) ──────────────────────────
    parser.add_argument(
        "--normalize-session-anchors",
        action="store_true",
        help=(
            "유지보수 모드 — pm_state.md 세션 식별 절의 오형식 `**N차차+**`(잔재) 를 "
            "`**N차**` 로 멱등·비파괴 정규화한다. session-seq/wave-summary 불요. "
            "먼저 `--dry-run` 으로 diff 를 선검토한 뒤 재실행해 적용하길 권장. --repo/--slot "
            "로 슬롯별 pm_state 지정 가능(솔로는 미지정→legacy)."
        ),
    )
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handoff = PmHandoff()

    # task 혼합 계약을 공용 parse_identity의 `--slot은 --repo 필수`보다 먼저 판정한다(SF).
    # 그래야 `--task X --slot 1`이 slot-mode 해결책(`--repo` 추가)을 잘못 안내하지 않는다.
    if args.task is not None and (
        args.repo is not None
        or args.slot is not None
        or args.branch
        or args.done
        or args.normalize_session_anchors
    ):
        parser.error(
            "--task 는 단독 정체성이다 — --repo/--slot/--branch/--done/"
            "--normalize-session-anchors 와 함께 쓸 수 없다."
        )
    if args.task is not None and args.session_seq is not None:
        parser.error(
            "task 모드에서는 --session-seq를 지정할 수 없다 — "
            "차수는 task pm_state에서만 자동 추론한다."
        )

    # 세션 정체성 해소 (identity_args) — 분해형 `--repo/--slot` canonical.
    # `--slot` 단독(--repo 없음)·slot<1 은 parse_identity 가 ValueError 로 판정(fail-loud).
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as exc:
        parser.error(str(exc))

    # 명시 --repo(+--slot) → 실행 슬롯(`work/<repo>_<N>`) 해소(세션↔repo 조인 검증·
    # fail-loud). 정체성 인자 전무(kind='none')면 (None, None) — run() 의 기존 no-flag
    # 자동해소(session-entry guarded·default-1/idle-필터·진짜 모호는 fail-loud)로 이어간다.
    args.worktree_slot, identity_err = _resolve_explicit_identity_slot(identity.repo, identity.slot)
    if identity_err is not None:
        parser.error(identity_err)

    # --task 이름 검증 — **공유 엔진 validator**(`worktree_pool._validate_task_name`·pm_config.cmd_alloc
    # 동형)로 traversal/절대경로/빈 이름/whitespace/괄호 + `<repo>_<N>` 예약을 fail-loud 한다. handoff
    # 는 bind_task 를 우회하는 별도 CLI 진입점이라 여기서 닫는다 — per-surface 이스케이프 대신 단일
    # validator 로 도메인을 협소화해 whitespace/괄호 거부까지 자동 상속한다. 예약명
    # (`--task project_manager_1`)도 거부해 dashboard `## project_manager_1` 가 실 slot-1 섹션과 충돌하는
    # 것을 막는다(reviewer). registered_repos 는 board 에서 fail-soft 해소(부재/실패면 None → 구문 검증만).
    if identity.task is not None:
        # 공유 엔진 validator(validate_task_name_engine) — main()·run()·prompt builder 공통 choke.
        # traversal/절대경로/빈이름/whitespace/괄호 + `<repo>_<N>` 예약을 fail-loud 한다. handoff 는
        # bind_task 를 우회하는 별도 CLI 진입점이라 여기서 닫는다. 예약명
        # (`--task project_manager_1`)도 거부해 dashboard `## project_manager_1` 가 실 slot-1 섹션과
        # 충돌하는 것을 막는다(reviewer). InvalidTaskName 은 `.reason` 진단 노출, 엔진 부재는 RuntimeError.
        try:
            validate_task_name_engine(identity.task)
        except RuntimeError as exc:
            if _is_engine_rev_skew(exc):
                raise
            parser.error(str(exc))
        except Exception as exc:  # noqa: BLE001 — worktree_pool.InvalidTaskName(.reason 진단)
            parser.error(
                f"--task 이름 {identity.task!r} 이(가) 부적합 — {getattr(exc, 'reason', exc)}. "
                "안전한 단일 이름(공백·괄호·path 문자·슬롯 예약패턴 `<repo>_<N>` 불가)이어야 한다."
            )
    # --branch 는 슬롯 정체성 동반 필요 — 슬롯 없는 브랜치는 회전 재부착 단서로 불완전
    # (어느 슬롯에 재부착할지 모름)하므로 조용히 무시하지 않고 거부한다(오용 축소).
    if args.branch and not args.worktree_slot:
        parser.error(
            "--branch 는 --repo <name> [--slot <N>] 과 함께 써야 한다 "
            "(multi-PM 모드 회전 재부착 단서)."
        )

    # --ack-dirty 는 **사유 박제**가 목적이라 빈/공백 사유를 받지 않는다 (사유 없는 bare 플래그는
    # argparse 가 "expected one argument" 로 이미 usage error). 엔진 run() 도 같은 경계를 갖는다.
    # 개행이 살아 있으면 사유 하나로 가짜 entry 줄을 위조할 수 있어 여기서 단일행으로 평탄화한다
    # (엔진 `build_dirty_ack_note` 도 같은 choke 를 다시 태운다 — 직접 호출 경로 보호).
    if args.ack_dirty is not None:
        if not args.ack_dirty.strip():
            parser.error(
                "--ack-dirty 는 사유가 필수다 — 빈 사유로는 dirty-tree 게이트를 override 할 수 없다."
            )
        args.ack_dirty = flatten_ack_reason(args.ack_dirty)

    # ── 오형식 차수 정규화 모드 (--normalize-session-anchors) ──────
    # 세션 식별 절의 `**N차차+**` → `**N차**` 멱등·비파괴 정규화. 핸드오프
    # 7단계와 독립된 유지보수 모드라 session-seq/wave-summary required 체크 *앞에서* 분기해
    # 조기 반환한다(위 ingress 파이프라인으로 해소된 슬롯을 per-slot 대상 해소에 재사용).
    if args.normalize_session_anchors:
        return _run_normalize_session_anchors(args.worktree_slot, args.dry_run)

    if identity.task is not None:
        # 엔진 run()이 회귀 게이트 뒤 task state를 보장한 다음 차수를 직접 추론한다. CLI가 미리
        # 읽어 숫자를 전달하지 않아 직접 호출과 같은 단일 봉인을 탄다.
        if not args.wave_summary:
            args.wave_summary = f"task {identity.task} 세션 핸드오프"
    else:
        # slot/solo 호환 경로 — 기존처럼 사람이 차수와 wave summary를 명시한다.
        missing = [
            flag
            for flag, val in (
                ("--session-seq", args.session_seq),
                ("--wave-summary", args.wave_summary),
            )
            if not val
        ]
        if missing:
            parser.error(f"{', '.join(missing)} 가 필수다.")

    # CLI는 승인값과 아직 미해소일 수 있는 정체성을 엔진에 그대로 전달한다. 실제 slot 해소와
    # 값-결속 ack 검증은 PmHandoff.run()의 단일 경계가 한 번만 수행한다(TOCTOU 방지).
    return handoff.run(
        session_num=args.session_seq,
        wave_summary=args.wave_summary,
        dry_run=args.dry_run,
        skip_pytest=args.no_pytest,
        worktree_slot=args.worktree_slot,
        branch=args.branch,
        done=args.done,
        task=identity.task,
        ack_dirty=args.ack_dirty,
        auto_trigger=args.auto_trigger,
        user_ack=args.user_ack,
    )


def _load_pm_log():
    """공유 log writer seam을 같은 tools/의 pm_log.py에서 로드한다."""
    return _load_module_from_path(
        TOOLS_DIR / "pm_log.py", "pm_log.py", verifier=_verify_engine_rev,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 복구 안내로 번역한다."""
    try:
        _console_encoding = _load_module_from_path(
            Path(__file__).resolve().with_name("console_encoding.py"),
            "console_encoding.py",
            verifier=_verify_engine_rev,
        )
        _console_encoding.configure_console_utf8()
        return _main(argv)
    except Exception as exc:  # noqa: BLE001 — marked skew만 사용자 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return _report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
