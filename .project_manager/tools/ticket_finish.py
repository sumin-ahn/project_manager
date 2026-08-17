#!/usr/bin/env python3
"""PM 부기 자동화 헬퍼 — ticket 완료 시 기계적 부기를 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/ticket_finish.py T-NNNN [--section "<섹션명>"] [--dry-run]

동작 순서 (하나라도 실패하면 이후 단계 중단):
  1. 회귀 실행 — 게이트는 areas.md(prefix 행 > repo 행) > local.conf test_cmd 로 해소하고,
     해소 실패면 pytest tests/ -q. red 면 즉시 중단(비-pytest 게이트는 exit code 로 판정
     — §게이트 종류).
  2. log/current.md 스켈레톤 append — 표준 형식 entry 골격.
  3. board.py complete 호출 — 회귀를 이미 통과했으므로 --tests-pass.
  4. git stage — **선언 경로만**(ticket `touches` ∪ *이 실행이 쓴* 산출물) 스테이징 + 스코프
     밖 잔여 dirty loud 보고. commit 은 PM 이 한다.
  5. 잔여 PM 수동 작업 출력.

결정:
  - subprocess DI: pytest/git/board.py subprocess 는 주입 가능한 함수로 감싼다.
  - red 면 중단: log/current.md / board / git 어떤 것도 건드리지 않는다.
  - status.md 는 더 이상 건드리지 않는다 (status.md = judgment-only).
    테스트 수·합계·소계·회귀 실측은 derivable(pytest/board.py regression 실측·log history)이라
    status.md 에 손으로 박제하지 않는다. 이 도구는 status.md 미접촉.
  - 모듈 행·서술·commit 은 자동화하지 않는다 (v1 축소판 — §배경).
  - fail-soft 가 아니다 — 명시적 실패 (비-0 종료 + 명확한 메시지).
  - LLM 미호출 — stdlib + board.py import 만.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Callable, NamedTuple

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


# ── REPO 앵커 (상향 탐색·board_root() graceful 탐지 동형) ──────────
# 하드코딩 `parents[2]` 는 tools 가 `<root>/.project_manager/tools/` 정확히 2단 깊이에 있다고
# 가정한다 — 채택자 형상(다른 깊이)에선 어긋난다().
# external_review 와 *동형*(각 파일 self-contained·공유 import 미도입)으로 상향 탐색해 견고화한다:
# `.project_manager` 마커를 품은 첫(최근접) 조상을 REPO 로, 못 찾으면 현행 `parents[2]` 폴백(회귀 0).

def _find_repo_root() -> Path:
    """스크립트 위치에서 부모 체인을 상향 탐색해 `.project_manager` 를 품은 첫 조상을 반환한다.

    `Path(__file__).resolve()` 부모 체인을 최근접부터 훑어 `.project_manager` 디렉토리를 자식으로
    가진 첫 조상을 REPO 로 반환한다(worktree/PM 홈 등 다른 깊이여도 마커로 견고 해소). 마커를
    못 찾으면 현행 `parents[2]` 로 폴백한다 — board_root() 동형의 graceful 폴백(회귀 0·additive).
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / ".project_manager").is_dir():
            return ancestor
    return here.parents[2]


REPO = _find_repo_root()
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored)
LEASES_FILE = REPO / ".project_manager" / ".local" / "worktree-leases.json"  # 리스 장부 (task 해소 read-only)
TOOLS_DIR = REPO / ".project_manager" / "tools"  # pm_handoff 동적 로드 앵커 (회귀 cwd 해소)
# areas.md 경로는 상수로 굳히지 않는다 — board/ 분리 시 board/ 안으로
# 옮겨가므로, `_resolve_per_repo_test_cmd` 가 board 모듈의 `areas_file()`(board_root 추종)에 위임.

# ── identity_args sibling 로드 ──────────────
# `--repo`/`--slot` 파싱을 공용 모듈 identity_args 에서 가져온다. 스크립트-위치 앵커
# (`Path(__file__).resolve().parent`=tools/)에서 `spec_from_file_location` 으로 동적 로드해
# sys.path 를 오염시키지 않는다 (board.py `_load_identity_args`·pm_handoff 와 동형). 스크립트
# 직접 실행(sys.path[0]=tools/)이든 테스트 로드(spec_from_file_location·패키지 아님이라 sys.path
# 미충전)든 어느 쪽이든 `Path(__file__).resolve().parent` 가 정확히 tools/ 라 동일하게 동작한다
# (pm_handoff 동일 관용구).

# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.7.5"


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
    (예: 신 ticket_finish→신 pm_handoff→구 identity_args 검출이 None 강등되지 않게)."""
    return getattr(exc, "_engine_rev_skew", False)


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
    `_load_identity_args`·pm_handoff 동형·sys.path 무오염). `--repo/--slot` 정체성 파싱에
    load-bearing 이라 로드 실패는 엔진 손상 — 예외를 그대로 낸다(fail-loud·board.py 동일)."""
    ia_path = Path(__file__).resolve().parent / "identity_args.py"
    return _load_module_from_path(
        ia_path, "identity_args.py", verifier=_verify_engine_rev,
    )


def _load_pm_log():
    """공유 log writer seam을 같은 tools/의 pm_log.py에서 로드한다."""
    return _load_module_from_path(
        TOOLS_DIR / "pm_log.py", "pm_log.py", verifier=_verify_engine_rev,
    )


def _load_file_lock():
    """공용 파일 프리미티브 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다.

    원자 교체 대상 파일을 읽는 지점은 이 seam 의 공유 읽기를 지난다([[T-0729]]) — 일반 `open`
    리더가 하나라도 잡고 있으면 Windows 는 그 파일의 원자 교체를 WinError 32 로 막는다. 부재/
    손상/rev 불일치는 엔진 사본 손상이므로 흡수하지 않는다(fail-loud·재동기 안내).
    """
    return _load_module_from_path(
        Path(__file__).resolve().parent / "file_lock.py", "file_lock.py",
        verifier=_verify_engine_rev, cache=True,
    )


try:
    identity_args = _load_identity_args()
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
_runtime_skill_entry = identity_args._runtime_skill_entry


# ── 회귀 cwd 자동해소 (pm_handoff `_regression_cwd` 재사용·DRY) ────
# 분리된 PM 홈엔 `tests/` 가 없으므로, 홈 cwd 에서 ticket_finish 를 돌리면
# 회귀가 활성 worktree 슬롯(tests/ 보유)에서 돌아야 한다. pm_handoff 가 이미 이 판정을
# 모듈-레벨 `_regression_cwd(worktree_slot, areas_file, leases_file)` 로 해결했고(
# pm_bootstrap `_auto_slot` 동적로드 재사용·self-host 해소 검증됨), board.py·pm_bootstrap 과
# *같은 위치*에 산다 — 복붙 대신 동적 로드해 그 함수를 그대로 위임한다(`_auto_slot` 복제 0).

def _load_pm_handoff():
    """pm_handoff 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    `_load_board_module`·`_load_domain_module` 과 동형 — `spec_from_file_location`
    (스크립트-위치 앵커). 부재/실패는 None 이고 호출부가 `str(REPO)` 로 폴백하므로 무해.
    """

    hp_path = TOOLS_DIR / "pm_handoff.py"
    if not hp_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            hp_path, "pm_handoff.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # pm_handoff 가 중첩 로드한 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _load_external_review():
    """external_review 모듈을 동적 로드한다 (부재/로드 실패 시 None·fail-soft).

    diff 서킷브레이커의 **정책과 측정식**은 external_review 가 소유한다(그쪽이 diff 산정 로직의
    단일 진실이다). 완료 부기는 그 판정을 빌려 쓸 뿐이라 사본을 두지 않는다 — 두 표면이 서로 다른
    상한/산정식을 쓰면 "리뷰는 통과했는데 완료가 막힌다"가 규칙이 아니라 사고가 된다."""
    er_path = TOOLS_DIR / "external_review.py"
    if not er_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            er_path, "external_review.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패가 완료를 막지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _regression_cwd(worktree_slot: str | None = None) -> str:
    """회귀를 실행할 작업 디렉토리를 해소한다 (pm_handoff `_regression_cwd` 위임).

    해소 순서(pm_handoff 와 동형):
      - `worktree_slot`(multi-PM 명시) 가 있으면 `REPO / worktree_slot`,
      - 없으면 pm_handoff `_regression_cwd` 가 bootstrap `_auto_slot` 으로 단일 self-host
        슬롯을 자동해소(`work/<repo>_<N>`),
      - 그것도 없으면(솔로/모호/부재) **현 `REPO` 기본** (fail-soft 폴백·솔로 무변경).

    pm_handoff 를 동적 로드해 그 함수에 위임한다(DRY — `_auto_slot` 복제 0). pm_handoff
    부재/로드 실패는 `str(REPO)` 폴백(현행 100% 보존·additive). pm_handoff.REPO 는 같은
    `tools/` 위치 기준이라 ticket_finish.REPO 와 동일 경로다.
    """
    hp = _load_pm_handoff()
    if hp is not None:
        try:
            return hp._regression_cwd(worktree_slot)
        except Exception as exc:  # noqa: BLE001 — fail-soft: 위임 실패는 REPO 폴백.
            if _is_engine_rev_skew(exc):
                raise
            pass
    return str(REPO)


# ── --repo/--slot 슬롯 disambiguation (다중슬롯 finish·pm_handoff 미러) ──
# 다중슬롯 형상에선 `_regression_cwd(None)` 의
# `_auto_slot` 자동해소가 모호(슬롯 2+)해져 홈으로 폴백하고, `tests/` 가 없어 회귀가
# red("no tests ran")→finish 가 *조용히* 차단된다. pm_handoff 는 이미
# `--repo`/`--slot`로 슬롯을 disambiguate 하는데 ticket_finish 엔 그 수단이 없던 게
# 근본이다. pm_handoff `_resolve_explicit_identity_slot`(명시 identity → 조인검증 해소)·
# `_resolve_session_worktree_slot`(session-entry 슬롯 해소·default-1/단독/idle-필터·진짜 모호면
# fail-loud)을 동적 로드해 재사용한다(DRY·해소 로직 복제 0). ticket_finish 는 이미
# pm_handoff 동적 로드 seam 보유.

def _resolve_finish_slot(repo: str | None, slot: int | None) -> tuple[str | None, str | None]:
    """`--repo`/`--slot`(또는 둘 다 부재)에서 회귀 worktree 슬롯을 해소한다 — `(worktree_slot, error_msg)`.

    pm_handoff 를 동적 로드해 재사용한다(DRY). 반환:
      - `(work/<repo>_<N>, None)` — `--repo`(+`--slot`) 명시 → pm_handoff
        `_resolve_explicit_identity_slot` 로 (세션↔repo 조인 검증) 통과 후 결정론적 해소.
      - `(work/<repo>_<N>, None)` — `--repo`/`--slot` 둘 다 부재인데 default-1/단독/idle-필터로
        자동해소됨(no-flag 기본 불변).
      - `(None, None)` — solo/미해소(멀티-PM 미셋업) → 호출부 REPO 런타임 폴백(현행 100% 보존).
      - `(None, error_msg)` — **진짜 모호**(멀티-PM under-specified·repo≥2·slot1 부재 비단독) 또는
        (명시 repo/slot 이 리스 장부와 조인 불일치) → 호출부 fail-loud.

    `repo`/`slot` 둘 다 `None`(kind='none')이면 기존 no-flag 자동해소로 위임 — pm_handoff
    부재/로드 실패는 fail-soft `(None, None)`(현행 REPO 폴백·솔로 무변경). `repo`/`slot` 명시인데
    pm_handoff 부재면 검증(리스 조인)을 할 수 없으므로 단순 조립만 해 신뢰한다(현행 폴백 패턴).
    """
    hp = _load_pm_handoff()
    if repo is None and slot is None:
        if hp is None:
            return None, None
        try:
            return hp._resolve_session_worktree_slot(None)
        except Exception as exc:  # noqa: BLE001 — fail-soft: 해소 실패는 현행 폴백(모호 아님).
            if _is_engine_rev_skew(exc):
                raise
            return None, None
    if hp is None:
        # pm_handoff 부재 — (리스 조인) 검증 불가하니 단순 조립만.
        if slot is not None:
            return f"work/{repo}_{slot}", None
        return None, None
    return hp._resolve_explicit_identity_slot(repo, slot)


def _default_python() -> str:
    """플랫폼-인지 venv 인터프리터 경로 (없으면 sys.executable 폴백).

    Windows 는 venv/Scripts/python.exe, POSIX 는 venv/bin/python. **venv 후보가 존재하면 그대로
    우선**한다 — 이 도그푸딩 머신은 시스템 python3 에 pytest 가 없고 venv 에만 있어, 회귀 측정
    인터프리터를 보존하려면 venv-first 가 불변이어야 한다(솔로/프레임워크 경로 회귀 0·우선순위 불변).

    venv 가 **없는** 건 에러가 아니라 정상 채택자 경로다 — 시스템 인터프리터에 pytest 가 깔린
    형상에선 venv/ 를 안 만든다. 그때는 `sys.executable`(현재 인터프리터)로 폴백해 그 환경의
    pytest 를 쓴다(폴백 분기·additive). 즉 존재 시 venv 우선, 부재 시 sys.executable 두 갈래다.
    """
    cand = REPO / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(cand) if cand.exists() else sys.executable


def local_config() -> dict[str, str]:
    """per-clone local.conf 를 KEY=value 로 읽는다 (없으면 빈 dict). board.py 와 동일 포맷."""
    conf: dict[str, str] = {}
    if not LOCAL_CONF.exists():
        return conf
    for line in _load_file_lock().read_text_shared(LOCAL_CONF, encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


# ── 회귀 명령 해소 (per-repo) ──────────────────────────────────
#
# 활성 repo 가 비-Python(Go 등)이거나 `tests/` 자체가 없을 수 있어 `pytest tests/ -q` 가
# 틀린다 — 회귀는 그 repo 의 `test_cmd`(areas.md 레지스트리 prefix 행/repo 행 > local.conf)를
# 써야 한다. 해소 체인의 단일 사본은 pm_handoff `_resolve_gate_cmd` 이고 이 도구는 자기 board
# 로드 seam 만 얹어 위임한다(아래 `_resolve_per_repo_test_cmd`).
#
# **솔로/프레임워크 자기 회귀(=현행 `pytest tests/ -q` venv 실행)는 반드시 보존**한다:
# 어느 층도 값을 주지 못하면 None 을 돌려, 호출부가 현행 argv 를 그대로 쓰게 한다(board 의
# 솔로 폴백 `pytest -q` 와 달리 venv 인터프리터·`tests/` 경로를 보존 — 도그푸딩 불변).

def _load_board_module():
    """board.py 를 경로 import 해 모듈로 반환한다 (실패 시 None).

    별도 함수로 둔 건 테스트가 areas.md/local.conf 해소를 hermetic 하게 가로채는 seam —
    board 의 areas/local 경로 전역을 tmp 로 재바인딩한 모듈을 주입할 수 있게 한다.
    per-repo test_cmd 해소·PM-홈 오실행 detector·pytest 요약행 파서 seam 이 공유한다.
    """
    try:
        mod = _load_module_from_path(
            BOARD_PY, "board.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


# ── PM-홈 worktree 오실행 가드 (쓰기-경로 전용) ─────────────────────────
# ticket_finish 를 PM 홈의 등록 worktree cwd 에서 오실행하면 REPO 가 worktree 로 착지해
# stray `wiki/log/current.md` append(+ board.py complete 오실행)를 낸다. board.py
# 와 *동일 detector*(`_pm_home_worktree_misanchor`·단일 진실)를 deep-import seam 으로 재사용해
# main() 진입에서 fail-loud 한다 — 부기 어떤 단계(회귀/log append/complete/stage)도 착지 전에
# 중단한다. detector 로드 실패/미해소는 fail-soft(현행 동작·오탐 0).

def _pm_home_misanchor() -> Path | None:
    """이 도구 앵커(REPO·*호출 시점* module-global — hermetic monkeypatch 추종)가 PM 홈의
    등록 worktree 면 그 PM 홈 경로를, 아니면 None. board.py 의 detector 를 재사용(DRY·단일
    진실)한다 — board 로드 실패면 None(fail-soft)."""
    mod = _load_board_module()
    if mod is None or not hasattr(mod, "_pm_home_worktree_misanchor"):
        return None
    try:
        return mod._pm_home_worktree_misanchor(REPO)
    except Exception:
        return None


def _guard_worktree_misanchor() -> bool:
    """쓰기-경로 진입 가드 — 오실행이면 fail-loud 후 True(차단), 아니면 False(통과)."""
    pm_home = _pm_home_misanchor()
    if pm_home is None:
        return False
    print(
        "[중단] `ticket_finish` 를 worktree(코드 전용) 트리에서 실행했습니다 — 완료 부기(log·"
        "board·git)는 PM 홈이 소유합니다. 이대로면 이 worktree 에 stray log/티켓을 "
        "잘못 만듭니다.\n"
        f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
        f"  (현재 앵커: {REPO})",
        file=sys.stderr,
    )
    return True


def _resolve_per_repo_test_cmd() -> str | None:
    """활성 회귀 게이트 명령(문자열)을 해소한다. 해소 실패면 None(솔로 폴백).

    해소 체인 — 앞선 층이 비어 있지 않은 값을 주면 거기서 멈춘다:

      1. areas.md 활성 **prefix** 행의 `test_cmd`   (multi-repo 네임스페이스 형상)
      2. areas.md 활성 **repo** 행의 `test_cmd`     (prefix 칼럼이 빈 무prefix 형상)
      3. `local.conf` 의 `test_cmd`                 (per-clone 명시 설정)
      4. None → 호출부가 솔로 `pytest tests/ -q` venv argv (도그푸딩 불변)

    **체인 자체는 pm_handoff `_resolve_gate_cmd` 가 소유한다** — 사본을 두지 않고 동적 로드해
    위임한다(`_regression_cwd` 위임과 같은 방향·DRY). 해소 함수가 이 도구에만 있고 pm_handoff
    엔 없던 미러 이탈이 무prefix 채택자 결함의 절반이었다: 사본 둘을 만들면 다음 갱신에서
    다시 갈린다. board 모듈은 **이 도구의 seam**(`_load_board_module`)이 준다 — areas/
    local.conf 해소를 hermetic 하게 가로채는 기존 테스트 seam 이 그대로 살아 있다.

    pm_handoff 부재/로드 실패는 None(솔로 폴백·fail-soft·현행 보존)이고, 형제 사본 skew 는
    fail-loud 로 올린다(`_regression_cwd` 와 동형).
    """
    mod = _load_board_module()
    if mod is None:
        return None
    hp = _load_pm_handoff()
    if hp is None:
        return None
    try:
        return hp._resolve_gate_cmd(mod)
    except Exception as exc:  # noqa: BLE001 — fail-soft: 위임 실패는 솔로 폴백.
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None


# ── 게이트 종류 (pytest 스위트인가 임의 명령인가) ────────────────────────
#
# 해소된 test_cmd 가 pytest 스위트라는 보장은 없다 — `go test ./...`·`viewer bind` 같은 임의
# 명령이다. 그런데 green 판정을 pytest 요약행(`N passed`)에만 걸어 두면 비-pytest 게이트는
# **항상 red** 로 오판돼 완료 부기가 통째로 막힌다(→ `--no-pytest` 상시 우회 → 진짜 red 를
# 놓친다). 그래서 판정 기준을 게이트 종류로 가른다:
#   - pytest 게이트 — 기존 요약행 판정 + 테스트 수 파싱 (100% 불변).
#   - 비-pytest 게이트 — exit code 0 만 green. 테스트 수는 측정 불가라 "?"(로그 스켈레톤이
#     이미 지원하는 값·`--no-pytest` 경로와 동형).
# **fail-soft 가 아니다** — 비-pytest 게이트도 rc != 0 이면 그대로 중단한다(red 무시 없음).
#
# 이 세 wrapper 는 pm_handoff 와 **동형 미러**다(해소 체인과 달리 위임하지 않는다) — 각 도구가
# 자기 `is_pytest_green`(요약행 seam 을 자기 board 로드에 묶는 기존 미러)을 판정에 쓰기
# 때문이다. 두 미러가 갈리면 `tests/test_regression_gate_resolution.py` 파리티 가드가 red 다.
_PYTEST_GATE_TOKEN = "pytest"

# 솔로/프레임워크 자기 회귀가 실제로 실행하는 argv 의 표시용 라벨 (dry-run 안내 문구).
_SOLO_GATE_LABEL = "pytest tests/ -q"


def _gate_is_pytest(gate_cmd: str | None) -> bool:
    """해소된 회귀 명령이 pytest 스위트면 True.

    `None`(해소 실패) = 솔로/프레임워크 자기 회귀 = venv pytest argv 이므로 True.
    """
    return gate_cmd is None or _PYTEST_GATE_TOKEN in gate_cmd


def _gate_label(gate_cmd: str | None) -> str:
    """회귀 게이트를 사람이 읽는 한 줄로 (안내 출력용)."""
    return gate_cmd if gate_cmd else _SOLO_GATE_LABEL


def _regression_is_green(output: str, returncode: int, gate_cmd: str | None) -> bool:
    """회귀가 green 이면 True — 판정 기준은 게이트 종류가 정한다(`_gate_is_pytest`).

    - pytest 게이트 — 기존 요약행 판정(`is_pytest_green`) 100% 불변.
    - 비-pytest 게이트 — exit code 0 만 green(요약행 개념이 없다).
    """
    if _gate_is_pytest(gate_cmd):
        return is_pytest_green(output, returncode)
    return returncode == 0


# ── pytest 출력 파서 ────────────────────────────────────────────────────
# 요약행 탐색은 board.py 의 공용 seam(`_pytest_summary_line` — 끝에서-탐색)이 소유한다.
# 출력 전체를 `re.search` 하면 캡처된 로그의 `3 passed`/`1 failed` 를 요약으로 먼저 만나
# 마감 판정을 뒤집는다(false-green·false-RED 양방향·자기 사본 금지).

# 이 도구가 실제로 호출하는 seam 함수 — **전부** 있어야 쓴다. 하나만 확인하고 통과시키면
# 부분 동기된 혼합 사본에서 나머지 호출이 AttributeError 로 터진다(진단 없는 죽음).
_PYTEST_SEAM_FUNCTIONS = ("_pytest_summary_line", "_pytest_outcome_count")
# seam 부재 진단 — 뒤따르는 "회귀 red" 중단이 원인을 테스트 실패로 오도하지 않도록 구제책을 낸다.
_PYTEST_SEAM_MISSING = ("pytest 파서: board.py 사본에 요약행 파서 seam 부재 — "
                        "pm_update 로 엔진 전체를 재동기하라.")


def _pytest_summary_seam():
    """pytest 요약행 파서 공용 seam(board.py) — 부재/불완전 사본이면 None (fail-soft).

    seam 이 없으면 파싱 실패(None)·red 판정으로 흘린다 — 로컬 첫-매칭 폴백을 두면 엔진
    사본이 불완전할 때만 조용히 오판이 되살아나 진단이 더 어려워진다(마감은 fail-closed).
    대신 중단 **바로 앞줄**에 구제책을 stderr 로 실어 red 사유를 잘못 읽지 않게 한다.
    """
    board = _load_board_module()
    if board is not None and all(
            hasattr(board, name) for name in _PYTEST_SEAM_FUNCTIONS):
        return board
    print(_PYTEST_SEAM_MISSING, file=sys.stderr)
    return None


def parse_pytest_output(output: str) -> tuple[int, int] | None:
    """pytest -q 출력에서 (passed, deselected) 를 파싱한다.

    반환: (passed, deselected) — 파싱 실패 시 None (요약행 부재·파서 seam 부재 포함).

    pytest -q 요약 라인 형식 예:
      "1472 passed, 24 deselected in 12.34s"
      "1472 passed in 12.34s"
      "5 failed, 1467 passed, 24 deselected in 10.00s"

    red (failed > 0) 여부 판단은 호출 측이 한다 (failed 수 포함 파싱은 하지 않음).
    반환값 (passed, deselected) 만 추출한다.
    """
    seam = _pytest_summary_seam()
    line = seam._pytest_summary_line(output) if seam is not None else None
    if line is None:
        return None
    passed = seam._pytest_outcome_count(line, "passed")
    if passed is None:
        return None
    deselected = seam._pytest_outcome_count(line, "deselected") or 0
    return passed, deselected


def is_pytest_green(output: str, returncode: int = 0) -> bool:
    """pytest -q 출력이 green (passed 존재, failed 없음) 이면 True.

    returncode 도 함께 검사한다 — returncode != 0 이면(인터럽트·부분 출력 등)
    명확한 'N passed' 가 있어도 green 으로 오판하지 않는다. passed/failed 는 **요약행
    안에서만** 읽는다 — 캡처 로그의 `1 failed` 로 green 회귀를 red 로 뒤집지 않는다.
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


# ── board.py 연동 ───────────────────────────────────────────────────────

def count_board_done(board_py: Path) -> int:
    """board.md 의 done 티켓 수를 반환한다 (board.py 를 import 해서).

    board.py 를 직접 import 해 find_ticket / STATUS_DIRS 를 활용한다.
    실패 시 -1 반환.
    """
    try:
        mod = _load_module_from_path(
            Path(board_py), Path(board_py).name, verifier=_verify_engine_rev,
        )
        # board_root() 추종 — board/ 분리시 ticket 이 board/tickets 로 빠지므로
        # legacy 별칭 상수(mod.TICKETS_DIR)가 아니라 함수를 부른다(분리 후 stale wiki/ 안 봄).
        done_dir = mod.tickets_dir() / "done"
        return len(list(done_dir.glob("T-*.md")))
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return -1


def get_ticket_title(board_py: Path, ticket_id: str) -> str:
    """ticket_id 의 title 을 board.py 를 import 해서 읽어온다 (실패 시 빈 문자열).

    조회는 `_ticket_frontmatter` 한 지점을 쓴다 — 제목과 estimate/touches 가 서로 다른 파일을
    보면 마감 로그가 가리키는 티켓과 게이트가 판정한 티켓이 갈린다.
    """
    title = _ticket_frontmatter(board_py, ticket_id).get("title")
    return title if isinstance(title, str) and title else ""


class TicketFrontmatterSnapshot(NamedTuple):
    """현재 티켓 frontmatter 읽기 결과 — 미발견과 board 로드 실패를 분리한다."""
    frontmatter: dict
    error: str | None


def _ticket_frontmatter_snapshot(
    board_py: Path, ticket_id: str,
) -> TicketFrontmatterSnapshot:
    """ticket frontmatter 와 board 모듈/파일 읽기 실패 진단을 함께 돌려준다.

    touches·estimate 소비처의 단일 해소 지점 — 같은 티켓을 두 번 다르게 찾지 않는다.
    조회는 board 의 공용 정확-일치 seam(`find_ticket_exact`)이다: `{id}-*.md` prefix glob 의
    첫 매칭을 믿으면 `T-NNNN` 과 `T-NNNN-001` 공존 시 **다른 티켓의 estimate/touches** 로 완료
    게이트가 판정한다(조용한 오판). 정확 일치가 없으면 폴백 없이 {} 다 — 판정 입력이 없는 것과
    무관 티켓으로 판정하는 것 중 전자가 안전하다(게이트는 off 로 떨어질 뿐이다)."""
    try:
        mod = _load_module_from_path(
            Path(board_py), Path(board_py).name, verifier=_verify_engine_rev,
        )
        found = mod.find_ticket_exact(ticket_id)
        if found is None:
            return TicketFrontmatterSnapshot({}, None)
        fm, _body = mod.load_ticket(found[1])
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        detail = " ".join(str(exc).split()) or type(exc).__name__
        return TicketFrontmatterSnapshot({}, f"{board_py}: {detail}")
    return TicketFrontmatterSnapshot(fm if isinstance(fm, dict) else {}, None)


def _ticket_frontmatter(board_py: Path, ticket_id: str) -> dict:
    """기존 fail-soft 조회 표면 — 상세 실패는 diff 전용 snapshot 소비자가 보존한다."""
    return _ticket_frontmatter_snapshot(board_py, ticket_id).frontmatter


def get_ticket_estimate(board_py: Path, ticket_id: str) -> str | None:
    """ticket_id 의 frontmatter `estimate` (없으면 None) — diff 서킷브레이커 상한 선택 입력."""
    estimate = _ticket_frontmatter(board_py, ticket_id).get("estimate")
    return estimate.strip() or None if isinstance(estimate, str) else None


def _clean_touches(value: object) -> list[str]:
    """frontmatter touches 값을 정규 문자열 목록으로 접는다."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value
                if isinstance(item, str) and item.strip()]
    return []


_PM_DIRECT_CODE_SUFFIXES: frozenset[str] = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".go", ".java", ".js", ".jsx", ".kt", ".kts",
    ".php", ".py", ".rb", ".rs", ".sh", ".swift", ".ts", ".tsx",
})


def _is_changed_test_path(path: str) -> bool:
    """repo-relative 변경 경로가 신규/수정 테스트인지 판정."""
    canonical = path.strip().replace("\\", "/")
    parts = tuple(part for part in canonical.split("/") if part and part != ".")
    name = parts[-1] if parts else ""
    return ("tests" in parts or "test" in parts or name.startswith("test_")
            or name.endswith("_test.py") or name.endswith(".test.js")
            or name.endswith(".test.ts") or name.endswith(".spec.js")
            or name.endswith(".spec.ts"))


def pm_direct_finish_warnings(
    tier: object, touches: Sequence[str], changed_paths: Sequence[str], *,
    touched_file_count: int | None = None,
    unresolved_directories: Sequence[str] = (),
) -> tuple[str, ...]:
    """PM-direct 조건 a·b 재검 경고. 반환값은 오직 안내이며 차단은 없다."""
    if tier != "pm-direct":
        return ()
    warnings: list[str] = []
    file_count = len(touches) if touched_file_count is None else touched_file_count
    if unresolved_directories:
        warnings.append(
            "PM-direct 조건 (a) 재확인: directory touches 파일 수를 해소하지 "
            f"못했습니다(상향 기본값): {', '.join(unresolved_directories)}."
        )
    elif file_count > 2:
        warnings.append(
            f"PM-direct 조건 (a) 위반: touches가 {file_count}개 파일입니다"
            "(2개 이하 필요)."
        )
    code_changed = any(
        Path(path.replace("\\", "/")).suffix.lower() in _PM_DIRECT_CODE_SUFFIXES
        for path in changed_paths
    )
    test_changed = any(_is_changed_test_path(path) for path in changed_paths)
    if code_changed and not test_changed:
        warnings.append(
            "PM-direct 조건 (b) 재확인: 코드 파일 diff가 있지만 "
            "신규/수정 테스트가 보이지 않습니다."
        )
    return tuple(warnings)


class ClaimedTouchesSnapshot(NamedTuple):
    """claimed 티켓 touches 읽기 결과 — 정상 빈 목록과 읽기 실패를 분리한다."""
    tickets: dict[str, list[str]]
    error: str | None


class DiffTicketInputs(NamedTuple):
    """서킷브레이커용 현재 티켓 입력과 board 모듈 실패 진단."""
    touches: list[str]
    estimate: str | None
    board_error: str | None


def get_ticket_touches(board_py: Path, ticket_id: str) -> list[str]:
    """ticket_id 의 frontmatter `touches`(파일/디렉토리 경로 목록)를 board.py 로 읽는다.

    문자열 원소만 취한다(비-문자열 오기는 버림). board 미로드·ticket 부재/깨짐 →
    [](graceful·crash 0 — soft 알림은 막지 않는다). domain soft 알림 step 이 쓴다.
    """
    # --touches CLI 와 동형: 각 원소 strip·빈 값/비-문자열 drop (silent-miss 방어).
    return _clean_touches(_ticket_frontmatter(board_py, ticket_id).get("touches"))


def get_claimed_ticket_touches(board_py: Path) -> ClaimedTouchesSnapshot:
    """보드의 claimed 티켓별 touches 와 읽기 실패 진단.

    diff 귀속 입력은 이 스냅샷뿐이다. 새 장부나 설정을 만들지 않고, 보드가 권위 있게 정한
    `tickets_dir()/claimed` 와 각 티켓 frontmatter `touches` 만 읽는다. 디렉터리와 frontmatter
    status 가 모두 claimed 인 행만 인정한다. 정상적인 빈 touches/빈 claimed 는 `error=None`이고,
    모듈·디렉터리·티켓 읽기/파싱 실패는 error 에 담아 호출부가 loud 하게 보정 skip 을 알린다.
    """
    try:
        board = _load_module_from_path(
            Path(board_py), Path(board_py).name, verifier=_verify_engine_rev,
        )
        claimed_dir = board.tickets_dir() / "claimed"
        if not claimed_dir.is_dir():
            raise FileNotFoundError(f"claimed 티켓 디렉터리 부재: {claimed_dir}")
        claimed_paths = sorted(claimed_dir.glob("T-*.md"))
    except Exception as exc:  # noqa: BLE001 — 보드 미로드면 타 티켓 제외 없이 과다 측정 쪽 유지.
        if _is_engine_rev_skew(exc):
            raise
        detail = " ".join(str(exc).split()) or type(exc).__name__
        return ClaimedTouchesSnapshot({}, f"{board_py}: {detail}")

    claimed: dict[str, list[str]] = {}
    errors: list[str] = []
    for path in claimed_paths:
        try:
            fm, _body = board.load_ticket(path)
        except Exception as exc:  # noqa: BLE001 — 호출부가 전체 보정을 보수적으로 skip 한다.
            detail = " ".join(str(exc).split()) or type(exc).__name__
            errors.append(f"{path}: {detail}")
            continue
        ticket_id = fm.get("id") if isinstance(fm, dict) else None
        if (not isinstance(ticket_id, str) or not ticket_id.strip()
                or fm.get("status") != "claimed"):
            errors.append(f"{path}: claimed frontmatter(id/status) 손상")
            continue
        claimed[ticket_id.strip()] = _clean_touches(fm.get("touches"))
    return ClaimedTouchesSnapshot(claimed, "; ".join(errors) or None)


def _fallback_frontmatter_scalar(text: str, key: str) -> str | None:
    """board 모듈 불능 때만 쓰는 보수적 단일-line scalar 복구."""
    match = re.search(rf"(?m)^{re.escape(key)}\s*:\s*(.*?)\s*$", text)
    if match is None:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    if raw[0] in "\"'" and raw.endswith(raw[0]) and len(raw) >= 2:
        return raw[1:-1].strip() or None
    return raw.split("#", 1)[0].strip() or None


def _fallback_ticket_frontmatter(
    board_py: Path, ticket_id: str, external,
) -> dict:
    """board.py 자체가 import/read 불능일 때 현재 티켓의 최소 입력만 복구한다.

    정상 경로의 YAML 판정을 대체하지 않는다. board.py 위치에서 PM 루트와 board/legacy tickets
    디렉터리를 결정하고, frontmatter `id`가 정확히 같은 후보가 **하나**일 때만 쓴다. touches는
    external_review 의 기존 raw frontmatter parser를 재사용하고 estimate는 알려진 diff-cap 키만
    인정한다. 후보가 모호하거나 읽히지 않으면 {}라 임의 스코프/상한을 만들지 않는다.
    """
    try:
        repo = Path(board_py).resolve().parents[2]
    except IndexError:
        return {}
    pm_dir = repo / ".project_manager"
    board_tickets = pm_dir / "board" / "tickets"
    tickets_dir = board_tickets if board_tickets.is_dir() else pm_dir / "wiki" / "tickets"
    candidates: list[tuple[Path, str]] = []
    for status in ("open", "claimed", "blocked", "done"):
        for path in sorted((tickets_dir / status).glob(f"{ticket_id}*.md")):
            try:
                text = _load_file_lock().read_text_shared(path, encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if _fallback_frontmatter_scalar(text, "id") == ticket_id:
                candidates.append((path, text))
    if len(candidates) != 1:
        return {}
    path, text = candidates[0]
    parser_error = getattr(external, "AnchorResolutionError", None)
    parser_errors = (AttributeError, OSError, UnicodeError)
    if isinstance(parser_error, type) and issubclass(parser_error, Exception):
        # external_review의 원문 frontmatter parser는 손상된 opener를 이 타입으로
        # 알린다. 이 fallback은 측정 가드용 입력 복구라 그 경우에도 가드 off로
        # 접어 완료 부기를 막지 않는다. 다른 예외는 삼키지 않는다.
        parser_errors += (parser_error,)
    try:
        touches = external._parse_touches_from_file(path)
    except parser_errors:
        return {}
    estimate = _fallback_frontmatter_scalar(text, "estimate")
    if estimate not in {"small", "medium", "large"}:
        estimate = None
    return {"touches": touches, "estimate": estimate}


# ── domain 연동 (soft 알림) ──────────────────────────────────
#
# 순환 없음: domain→board / ticket_finish→board,domain / board 는 둘 다 import 안 함.
# domain.py 부재(솔로/신규 clone·구버전)·로드 실패 → None (호출부가 graceful skip).

DOMAIN_PY = REPO / ".project_manager" / "tools" / "domain.py"


def _load_domain_module():
    """domain.py 를 경로 import 해 모듈로 반환한다 (부재/실패 시 None).

    board.py·areas 해소와 동일한 deep-import seam — 테스트가 hermetic 하게 대역을
    주입하거나 None(부재)을 흉내낼 수 있다.
    """
    if not DOMAIN_PY.exists():
        return None
    try:
        mod = _load_module_from_path(
            DOMAIN_PY, "domain.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # domain 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def affected_domain_titles(ticket_id: str, board_py: Path) -> list[tuple[str, bool | None]] | None:
    """ticket touches ∩ domain covers 로 영향받는 페이지 (title, stale) 목록을 돌려준다.

    각 원소 = `(title, stale)` — stale 은 `domain.page_stale`(True=낡음·False=fresh·
    None=판정불가/unknown). soft step 이 stale True 줄 앞에 ⚠ 를 단다(visibility).
    domain.py 부재·로드 실패 → None (호출부가 조용히 skip — 솔로/신규 clone 무영향).
    touches 부재·영향 0 → [](빈 알림). domain.pages_for_touches 재사용(중복 매칭 0).

    **소유 repo별 git_runner 1회 생성해 공유** — 페이지 `repo:`를 board의 단일 owner
    resolver로 해소하고 checkout마다 runner를 캐시해 page_stale에 넘긴다.
    owner를 해소하지 못하면 domain의 sentinel을 넘겨 기본 owner runner fallback을 막고
    stale 판정을 skip한다.
    구 board(리졸버 부재)는 키 부재/명시 self만 기존 REPO runner로 퇴화하고,
    upstream/null/미지원 owner는 unverifiable(None)로 남긴다. page_stale 은
    그 자체로 fail-soft(예외/git 부재→None)지만, stale 산출 단계 전체를 한 번 더 try 로
    감싸 어떤 예외도 무표시(None)로 흡수한다 — 비차단·graceful 동작 불변.
    """
    domain = _load_domain_module()
    if domain is None:
        return None
    touches = get_ticket_touches(board_py, ticket_id)
    # touches 가 비면 매칭 0 확정 — load_pages 스캔(깨진 페이지 warning 포함) 자체를 건너뛴다.
    if not touches:
        return []

    # 접두 없는 기존 ticket은 normalizer 로드 자체를 건너뛴다. 부분 동기 adopter에
    # repo_coordinates.py가 없어도 기존 repo-relative soft 알림은 그대로 살아야 한다.
    prefixed = [path for path in touches if _has_worktree_touch_prefix(path)]
    try:
        coords = _load_repo_coordinates() if prefixed else None
    except RuntimeError as exc:
        if _is_engine_rev_skew(exc):
            raise
        print(f"ticket_finish: repo 좌표 normalizer skew — {exc}", file=sys.stderr)
        return None
    if prefixed and coords is None:
        for path in prefixed:
            print(
                f"ticket_finish: touch {path!r} 좌표 정규화 경고 — repo 좌표 normalizer "
                f"로드 실패 ({TOOLS_DIR / 'repo_coordinates.py'}); 이 경로는 제외",
                file=sys.stderr,
            )
        touches = [path for path in touches if not _has_worktree_touch_prefix(path)]
    elif coords is not None:
        error_type = getattr(coords, "RepoCoordinateError", RuntimeError)
        normalized: list[str] = []
        for path in touches:
            if not _has_worktree_touch_prefix(path):
                normalized.append(path)
                continue
            try:
                normalized.append(coords.normalize_repo_path(path, pm_root=REPO))
            except error_type as exc:
                print(
                    f"ticket_finish: touch {path!r} 좌표 정규화 경고 — {exc}; "
                    "이 경로는 제외",
                    file=sys.stderr,
                )
        touches = normalized
    if not touches:
        return []

    try:
        # 완료 알림도 domain affected와 같은 touches∩covers 계열이다. task stage와 별개로
        # 같은 normalizer를 거쳐
        pages = domain.pages_for_touches(touches, domain.load_pages())
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise
        return None
    board = load_board_module(board_py)
    resolver = getattr(board, "_freshness_owner_repo", None) if board is not None else None
    runners: dict[Path, object] = {}
    unresolved_runner = getattr(domain, "_UNRESOLVED_GIT_RUNNER", None)

    def runner_for_page(page):
        try:
            if resolver is None:
                owner = page.get("repo", "self")
                if not isinstance(owner, str) or owner.strip() != "self":
                    return None
                owner_repo = REPO
            else:
                owner = page["repo"] if "repo" in page else "self"
                owner_repo, _owner_error = resolver(owner)
                if owner_repo is None:
                    return unresolved_runner
            owner_repo = Path(owner_repo)
            if owner_repo not in runners:
                runners[owner_repo] = domain._real_git_runner(owner_repo)
            return runners[owner_repo]
        except Exception as exc:  # noqa: BLE001 — 일반 owner/runner 해소 실패는 stale unknown.
            if _is_engine_rev_skew(exc):
                raise
            return None

    out: list[tuple[str, bool | None]] = []
    for page in pages:
        try:
            stale = domain.page_stale(page, git_runner=runner_for_page(page))
        except Exception as exc:  # noqa: BLE001 — stale 못 구하면 무표시(None)·비차단.
            if _is_engine_rev_skew(exc):
                raise
            stale = None
        out.append((page["title"], stale))
    return out


# ── 디자인 git stage 스코프 ─────────────────────────────────────
#
# **공유 워킹트리 mutation 은 선언된 경로만 건드린다.**
# 루트에서 `git add -A` 였다 — 멀티-PM 형상에서 다른 슬롯이 편집 중인 wiki 문서·무관 산출물을
# 통째로 쓸어담아 PM 커밋이 서로 꼬였다(Context·사용자 실사용 보고).
#
# 선언원은 둘, 그리고 **둘뿐**이다:
#   티켓 frontmatter `touches` — 사람이 적은 작업 범위 선언. 이미 있는 파서
#      (`get_ticket_touches`)를 그대로 쓴다.
#   **이 실행이 실제로 쓴 산출물** — `log/current.md`(:2단계) + board complete 가 옮긴 티켓
#      파일(:3단계·legacy 형상 한정·아래 `engine_written_paths`).
#
# 를 "엔진이 아는 wiki 산출물 디렉토리"(decisions/·domain/·ideas/…)로 넓히지 **않는다**:
# 그건 `pm_adr`·`domain`·`board idea` 가 **다른 실행** 에서 만든 것이라 이 mutation 의 선언분이
# 아니고, 디렉토리로 넘기는 순간 그 아래 **남의 미완성 편집까지 함께 stage** 되어 좁힌 척하고 안
# 좁힌 상태가 된다(codex must-fix). 그것들이 커밋에서 빠지는 건 정상이고, 빠졌다는 사실은 아래
# **잔여 loud 보고**가 알린다 — 그게 그 보고의 존재 이유다.
#
# gitignored 산출물(per-slot/per-task pm_state·leases·board.md·`.local/`)은 **스코프 산출 단계에서
# 명시적으로 걸러야 한다**(`board.git_scope_stageable`). `git add` 는 *명시 pathspec* 이 ignored 면
# rc=1 에러이기 때문이다 — 조용히 건너뛰는 건 광역 `add -A` 일 때뿐이다. 즉 스코프화가 이 성질을
# 반전시킨다. 안 걸르면 `touches` 에 gitignored 경로가 하나만 있어도
# board complete 이후 stage 가 통째로 죽고 잔여 loud 보고까지 사라진다.


def _load_tool_module(path: Path):
    """같은 `tools/` 의 형제 모듈을 경로 로드한다 — 부재/실패 → None (`_load_domain_module` 동형).

    sys.path 무오염(spec_from_file_location)·rev 스탬프 skew 는 fail-loud. 로드 실패
    자체는 None 으로 돌려 호출부가 **눈에 띄게** 처리하게 한다(조용한 skip 아님 — 아래
    `stage_scope` 는 None 을 stage 불능으로 보고 loud 경고를 낸다).
    """

    path = Path(path)
    if not path.exists():
        return None
    try:
        mod = _load_module_from_path(
            path, path.name, verifier=_verify_engine_rev,
        )
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def load_board_module(board_py: Path):
    """board.py 를 로드하고 그 모듈의 `REPO` 를 **이 도구의 REPO 로 재-앵커** 해서 돌려준다.

    board 모듈은 자기 파일 위치에서 REPO 를 해소하므로, `board_root()`/`tickets_dir()`/
    `_board_git_enabled()` 같은 **함수** 판정이 이 도구가 보는 트리를 따라오게 하려면 재-앵커가
    필요하다(실 형상에선 같은 값이라 항등, REPO 를 치환한 hermetic 테스트에선 tmp 를 따라온다).
    모듈은 `spec_from_file_location` 으로 뜬 **이 호출 전용 사본**이라 전역 오염이 없다.
    """
    board = _load_tool_module(board_py)
    if board is not None and getattr(board, "REPO", None) is not None:
        board.REPO = REPO
    return board


def _load_repo_coordinates():
    """공용 repo 좌표 normalizer를 경로 로드한다. 부재/손상은 stage_scope가 loud error로 바꾼다."""

    path = TOOLS_DIR / "repo_coordinates.py"
    if not path.exists():
        return None
    try:
        mod = _load_module_from_path(
            path, "repo_coordinates.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — 호출부가 stage 불능 사유로 표면화.
        if _is_engine_rev_skew(exc):
            raise
        return None
    return mod


_WORKTREE_TOUCH_PREFIX = re.compile(r"^work/[^/]+_\d+(?:/|$)")


def _has_worktree_touch_prefix(path: str) -> bool:
    """접두 없는 채택자에서 coordinate sibling 로드를 피하기 위한 경량 판정."""
    norm = path.replace(os.sep, "/").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return _WORKTREE_TOUCH_PREFIX.match(norm) is not None


def engine_written_paths(board, ticket_id: str, log_file: Path) -> list[Path]:
    """**이 실행이 실제로 쓴** 산출물 경로.

      - `log/current.md` — 2단계가 스켈레톤을 append 한다(항상).
      - 티켓 파일의 옛/새 경로 — 3단계 `board.py complete` 가 `claimed/`→`done/` 로 옮긴다.
        **board-git 분리 형상에선 제외** 한다: 티켓이 서브모듈(`.project_manager/board/`) 안이라
        상위 repo 의 `git add` 가 `fatal: … is in submodule`(rc=128)로 죽고, 그 이동은 board-git
        이 자기 커밋으로 이미 기록한다. legacy(board 미분리·**출하 템플릿 기본 형상**)에선 그
        이동이 홈 git 에 떨어지므로 반드시 실어야 한다 — 안 그러면 채택자가 매 finish 마다 손으로
        `git add` 해야 한다(reviewer 실측).

    옛 경로는 상태 디렉토리를 몰라도 된다 — 후보(모든 STATUS_DIRS/같은 파일명)를 넣어두면
    실존/추적되지 않는 후보는 `git_scope_stageable` 이 거른다(추적 중인 *삭제* 경로만 남아
    이동이 커밋으로 완성된다). board 미로드면 log 만.
    """
    paths: list[Path] = [Path(log_file)]
    if board is None:
        return paths
    try:
        if board._board_git_enabled():
            return paths
        _status, ticket_path = board.find_ticket(ticket_id)
        paths.append(ticket_path)
        paths.extend(board.tickets_dir() / status / ticket_path.name
                     for status in board.STATUS_DIRS)
    except Exception:  # noqa: BLE001 — 티켓 조회 실패(부재·손상)는 log 만으로 진행(잔여 보고가 알린다).
        pass
    return paths


class StageScope(NamedTuple):
    """stage 스코프 산출 결과 — `pathspec` + `error`(산출 불능 사유·None 이면 정상).

    빈 `pathspec` 을 두 상태로 갈라 쓴다: **선언이 비었다**(error=None·정상)와 **판정기가
    죽었다**(error=사유). 후자를 조용히 no-op 으로 흘리면 stage 0 인데 아무 말이 없어
    "부기 끝" 으로 보인다(reviewer 실측 — board 모듈 로드 실패 형상).
    """
    pathspec: tuple[str, ...]
    error: str | None


class RepoStagePlan(NamedTuple):
    """한 git repo 에서 실행할 좁은 stage 계획 (두-git task-mode 용)."""
    label: str
    cwd: Path
    scope: StageScope


class DiffAttribution(NamedTuple):
    """티켓 귀속 보정 뒤 diff 총량과 실제 제외 근거."""
    total: int
    excluded_total: int
    excluded_ticket_ids: tuple[str, ...]


class DiffPathStat(NamedTuple):
    """numstat 한 행의 논리 경로들(rename이면 source·destination)과 추가+삭제량."""
    paths: tuple[str, ...]
    amount: int


def stage_scope(ticket_id: str, board_py: Path, log_file: Path,
                run_git: Callable[[list[str]], tuple[int, str]], *,
                repo: Path | None = None, include_touches: bool = True,
                include_engine_outputs: bool = True,
                touches_workspace: Path | None = None) -> StageScope:
    """이 완료 부기가 stage 할 pathspec (REPO 상대·실제 `add` 가능한 **파일**).

    선언원 = 티켓 `touches` ∪ `engine_written_paths()`. **판정은 board.py 의 repo-중립
    프리미티브 `git_scope_stage_pathspec` 한 벌을 재사용** 한다 — board-git 이 쓰는 바로 그
    함수다(스코프 산출·디렉토리 전개·서브모듈/미존재/미추적 제거). 복제하면 다음 사람이 한쪽만
    고친다.

    그 필터가 load-bearing 이다: 두-git 형상에서 `touches` 는 코드 worktree 경로(PM 홈엔 없음)
    를, board 분리 형상에선 서브모듈 내부 경로를 가리킬 수 있는데, 그대로 pathspec 에 넣으면
    `git add` 가 **rc=128 fatal** 로 죽어 아무것도 stage 되지 않는다.

    board 모듈을 못 띄우면 **fail-loud** — 빈 pathspec + error 사유를 돌려준다(호출부가 경고 +
    잔여 전체 보고). 여기서 조용한 fail-soft 는 stage 0 을 정상처럼 보이게 해 위험하다.
    """
    board = load_board_module(board_py)
    scope_fn = getattr(board, "git_scope_stage_pathspec", None) if board else None
    if scope_fn is None:
        return StageScope((), f"board 모듈을 로드하지 못했다 ({board_py})")
    repo = Path(repo) if repo is not None else REPO
    declared: list[Path] = []
    if include_touches:
        touches = get_ticket_touches(board_py, ticket_id)
        if touches_workspace is not None and any(
                _has_worktree_touch_prefix(path) for path in touches):
            try:
                coords = _load_repo_coordinates()
            except RuntimeError as exc:
                if _is_engine_rev_skew(exc):
                    raise
                return StageScope((), f"repo 좌표 normalizer skew ({exc})")
            if coords is None:
                return StageScope(
                    (),
                    f"repo 좌표 normalizer를 로드하지 못했다 ({TOOLS_DIR / 'repo_coordinates.py'})",
                )
            try:
                touches = coords.normalize_repo_paths(
                    touches,
                    pm_root=REPO,
                    workspace=touches_workspace,
                )
            except getattr(coords, "RepoCoordinateError", RuntimeError) as exc:
                return StageScope((), f"touches 좌표 정규화 실패 ({exc})")
        declared.extend(repo / touch for touch in touches)
    if include_engine_outputs:
        # PM-home 산출물은 ticket code worktree 가 아니라 이 도구의 설계 repo 에만 있다.
        declared.extend(engine_written_paths(board, ticket_id, log_file))
    try:
        return StageScope(tuple(scope_fn(repo, declared, run_git=run_git)), None)
    except Exception as exc:  # noqa: BLE001 — 산출 실패도 조용히 넘기지 않는다(fail-loud).
        return StageScope((), f"스코프 산출 실패 ({exc.__class__.__name__}: {exc})")


# ── 잔여 보고 (under-stage / 스코프 밖 staged) ──────────────────────────────
#
# 보고 채널은 **board 모듈 로드에 의존하지 않는다** — 로드가 실패한 형상에서 stage 0 인 채
# "잔여 없음"이라는 **거짓 안심**이 나오면 안 된다(reviewer 실측). 그래서 board 가 있으면 그
# NUL 파서(`git_parse_porcelain_z` — 스코프 전개와 **같은 함수**)를 쓰고, 없으면 줄 단위
# degraded 판독으로 **보고만** 이어간다(그 경우 스코프도 비어 있어 전부 스코프 밖이 맞다).


def scope_covers(pathspec: Sequence[str], path: str) -> bool:
    """`path` 가 이번 stage 선언 스코프에 덮이는가 (정확 일치 또는 선언 디렉토리 아래)."""
    return any(path == rel or path.startswith(rel.rstrip("/") + "/") for rel in pathspec)


def status_entries(run_git: Callable[[list[str]], tuple[int, str]],
                   board=None) -> tuple[tuple[str, str], ...]:
    """현재 워킹트리 상태 `((XY, 경로), …)` — 조회 실패·비-git 은 `()`(비차단).

    **`-z` + `--untracked-files=all` 이 둘 다 load-bearing** 이다:
      - `-z` 없으면 git 이 비-ASCII/공백 경로를 인용 + 8진 이스케이프로 낸다. 그 문자열이
        `scope_covers` 비교에 들어가 매칭에 실패하고, *방금 자기가 stage 한 파일* 을 "스코프
        밖 — 빼라" 로 오보한다(reviewer 실측·PM 홈에 한글 경로 실재). 목록도 8진이라 복붙 불가다.
      - `-uall` 없으면 새 untracked 디렉토리가 `?? wiki/raw/` 한 줄로 접혀 **무엇이 빠졌는지
        안 보인다**(legacy 실 구동 실측). 접힌 정보를 주는 loud 채널은 loud 가 아니다.
    NUL 파싱은 board 의 `git_parse_porcelain_z` 한 벌(디렉토리 전개와 공유)을 쓴다.

    board 미로드(=판정기 사망) 시에도 **`-z` 로 조회** 하고 NUL 을 여기서 직접 자른다 — 공유
    파서를 못 쓰는 갈래라고 8진 이스케이프 목록을 내면, 방금 닫은 증상(복붙 불가·경로 왜곡)이
    이 갈래에만 남는다. rename/copy 의 **2토큰**(원본 경로)도 같이 소비한다 — 안 그러면 원본
    토큰이 코드 없는 **가짜 잔여 항목**으로 보고에 뜬다.
    """
    parse_z = getattr(board, "git_parse_porcelain_z", None) if board is not None else None
    try:
        rc, out = run_git(["status", "--porcelain", "-z", "--untracked-files=all"])
    except Exception:  # noqa: BLE001 — 보고는 완료 흐름을 막지 않는다.
        return ()
    if rc != 0:
        return ()
    if parse_z:
        return parse_z(out)
    tokens = out.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        code, path = token[:2], token[3:]
        if code[0] in ("R", "C"):
            index += 1          # 원본 경로 토큰 소비 (가짜 항목 방지·주 파서와 동형)
        entries.append((code, path))
    return tuple(entries)


def split_dirty(entries: Sequence[tuple[str, str]],
                pathspec: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """상태 목록 → (**스코프 밖 staged** 줄, **미스테이지 잔여** 줄) — 두 실패 방향을 함께 본다.

      - 스코프 밖 staged (`X` 가 공백도 `?` 도 아님 ∧ 선언 스코프 밖) — 이 도구는 커밋하지
        않고 PM 에게 넘기므로, 남이 미리 stage 해 둔 변경이 **PM 의 커밋에 그대로 실린다**.
        `add` 를 좁히는 것만으로는 안 닫히고 `commit` 쪽으로 새는 갈래다("add 와
        commit 양쪽").
      - 미스테이지 잔여 (`Y` 가 공백 아님·`??` 포함) — 스코프가 못 덮은 변경(under-stage).
    """
    staged_out: list[str] = []
    unstaged: list[str] = []
    for code, path in entries:
        # `.project_manager/.local/` 전체는 clone-local 런타임 상태다. 정상 adopter에서는
        # gitignore지만, 최소/격리 repo에서도 lock·ledger·task 상태가 ticket 산출물 누락으로
        # 오보되지 않게 접두 클래스 전체를 잔여 보고에서 제외한다. 비슷한 이름의 sibling
        # (`.project_manager/.locality/`)은 제외하지 않는다.
        if path == ".project_manager/.local" or path.startswith(".project_manager/.local/"):
            continue
        if code[0] not in (" ", "?") and not scope_covers(pathspec, path):
            staged_out.append(f"{code} {path}")
        if code[1] != " ":
            unstaged.append(f"{code} {path}")
    return tuple(staged_out), tuple(unstaged)


# ── 로그 스켈레톤 ───────────────────────────────────────────────────────

# 회귀 baseline 은 *실측* new_total 1줄만 남긴다
# 합계는 status.md 에 박제하지 않으므로 delta 는 PM 이 서술로 채운다·history 단일 진실=log).
# task 태그 sentinel은 pm_log.py·pm_handoff.py의 동명 상수와 미러한다.
# 모듈 격리를 유지하려고 각 생산자가 상수를 소유한다.
_TASK_TAG_PREFIX = "task:"


LOG_SKELETON_TEMPLATE = """\
## [{date}] {entry_type} | {ticket_id} — {title}{task_tag}

- <!-- PM: 무엇을·왜 서술 -->
- 테스트: 회귀 {new_total} / {new_total} (실측 · 직전 대비 delta 는 PM 서술).
- board: done {board_before}→{board_after}.
"""


def build_log_skeleton(
    ticket_id: str,
    title: str,
    new_total: int | str,
    board_before: int,
    board_after: int,
    entry_type: str = "<!-- feat/fix/verify/… -->",
    date: str | None = None,
    task: str | None = None,
    session: str | None = None,
) -> str:
    if date is None:
        date = datetime.date.today().isoformat()
    return LOG_SKELETON_TEMPLATE.format(
        date=date,
        entry_type=entry_type,
        ticket_id=ticket_id,
        title=title,
        task_tag=(
            f" ({_TASK_TAG_PREFIX}{task})" if task
            else f" ({session})" if session
            else ""
        ),
        new_total=new_total,
        board_before=board_before,
        board_after=board_after,
    )


# ── 핵심 흐름 ──────────────────────────────────────────────────────────

class TicketFinisher:
    """PM 부기 자동화 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    broker/dispatch.py 의 clock_fn/sleep_fn DI 패턴과 동일.
    """

    def __init__(
        self,
        *,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_board_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        board_count_fn: Callable[[], int] | None = None,
        ticket_title_fn: Callable[[str], str] | None = None,
        affected_domain_fn: Callable[[str], list[tuple[str, bool | None]] | None] | None = None,
        run_git_stdout_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        stage_scope_fn: Callable[[str], StageScope] | None = None,
        status_entries_fn: Callable[[], tuple[tuple[str, str], ...]] | None = None,
        run_git_at_fn: Callable[[Path, list[str]], tuple[int, str]] | None = None,
        run_git_stdout_at_fn: Callable[[Path, list[str]], tuple[int, str]] | None = None,
        status_entries_at_fn: Callable[[Path], tuple[tuple[str, str], ...]] | None = None,
        diff_cap_block_fn: Callable[[str], str | None] | None = None,
        dod_block_fn: Callable[[str], str | None] | None = None,
        log_file: Path = LOG_FILE,
        board_py: Path = BOARD_PY,
        venv_python: str | Path = _default_python(),
        regression_cwd: str | Path | None = None,
        task_workspace: str | Path | None = None,
    ) -> None:
        self._log_file = log_file
        self._board_py = board_py
        self._venv_python = venv_python
        # 회귀 cwd seam — 분리된 PM 홈엔 tests/ 가 없으므로 회귀는 활성
        # worktree 슬롯(tests/)에서 돌아야 한다. **즉시 고정하지 않는다** — `regression_cwd`
        # 명시 주입은 그대로 보존(테스트/명시 override)하되, 미지정이면 `__init__` 시점의 REPO
        # 박제 대신 _default_run_pytest 가 런타임에 _regression_cwd() 로 self-host 슬롯을
        # 자동해소한다(pm_handoff `_regression_cwd` 재사용·솔로는 REPO 폴백 무변경).
        self._regression_cwd = str(regression_cwd) if regression_cwd else None
        self._task_workspace = Path(task_workspace) if task_workspace else None

        # subprocess DI — 기본값은 실제 subprocess 호출
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_git_fn = run_git_fn or self._default_run_git

        # board 조회 DI — 기본값은 실 board.py import 구현
        self._board_count_fn = board_count_fn or self._default_board_count
        self._ticket_title_fn = ticket_title_fn or self._default_ticket_title

        # domain soft 알림 DI — 기본값은 실 domain.py import 구현.
        # None 반환 = domain 부재/로드 실패(조용히 skip). 막지 않음(soft).
        self._affected_domain_fn = affected_domain_fn or self._default_affected_domain

        # git 파싱 전용 러너 — **stdout 만** 돌려준다. `_run_git_fn` 은 진단 메시지를 살리려고
        # stdout+stderr 를 합치는데, 그 값을 `status --porcelain`·`ls-files -z` 파서에 먹이면
        # git 경고 한 줄이 **가짜 잔여 항목**으로 둔갑한다(reviewer). 호출부가 git seam 을 이미
        # 주입했으면(테스트) 그걸 그대로 써 hermetic 을 깨지 않는다.
        self._run_git_stdout_fn = (run_git_stdout_fn or run_git_fn
                                   or self._default_run_git_stdout)

        # git stage 스코프 DI — 기본값은 `touches ∪ 이 실행이 쓴 산출물` 실 해소.
        # 상태 조회도 같은 seam 으로 둬, 테스트가 stage 판정과 보고를 따로 격리한다.
        self._stage_scope_fn = stage_scope_fn or self._default_stage_scope
        self._status_entries_fn = status_entries_fn or self._default_status_entries
        self._run_git_at_fn = run_git_at_fn or self._default_run_git_at
        # task worktree도 홈과 같은 규칙: 기계 판독(`ls-files -z`/`status --porcelain -z`)에는
        # stdout만 쓴다. mutation seam은 stderr 진단을 계속 합쳐 호출자에게 보인다.
        self._run_git_stdout_at_fn = run_git_stdout_at_fn or self._default_run_git_stdout_at
        self._status_entries_at_fn = status_entries_at_fn or self._default_status_entries_at
        # diff 서킷브레이커 seam — 차단 안내 문자열 또는 None(통과·가드 off). 정책·측정식은
        # external_review 가 소유하고 여기서는 판정만 소비한다.
        self._diff_cap_block_fn = diff_cap_block_fn or self._default_diff_cap_block
        # DoD 부기 게이트 preflight seam — 차단 사유 문자열 또는 None(통과·판정 불가).
        # 규칙 소유자는 board(`_dod_open_items`)이고 여기서는 **더 앞에서 한 번 더** 물을 뿐이다.
        self._dod_block_fn = dod_block_fn or self._default_dod_block
        # (스코프 밖 staged, 미스테이지 잔여) 건수 — `[완료]` 줄 재고지용(loud 강화).
        self._dirty_summary: tuple[int, int] = (0, 0)

    # ── 기본 subprocess 구현 (실제 실행) ─────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """회귀를 실행해 (returncode, stdout+stderr) 반환.

        명령 해소:
          - **해소 성공** — `_resolve_per_repo_test_cmd()`(areas prefix 행 > areas repo 행 >
            local.conf)가 준 문자열을 shell 로 실행(board.py 회귀와 동형·비-Python repo 수용).
          - **솔로/프레임워크 자기 회귀** — 해소 실패면 현행 그대로
            `[venv_python, -m, pytest, tests/, -q]` venv argv(도그푸딩 불변·하위호환).

        cwd 는 런타임 해소— 명시 주입(`regression_cwd` 인자)이 있으면 그 경로,
        없으면 `_regression_cwd()` 가 self-host 단일 슬롯을 자동해소(홈 cwd 에서도 활성
        worktree 의 tests/ 에서 돌게). 솔로/모호/부재는 REPO 폴백(현행 보존·additive).
        """
        cwd = self._regression_cwd if self._regression_cwd is not None else _regression_cwd()
        per_repo_cmd = _resolve_per_repo_test_cmd()
        if per_repo_cmd:
            # multi-PM — per-repo test_cmd 문자열을 shell 로(board.py regression run 과 동형).
            result = subprocess.run(
                per_repo_cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
            )
        else:
            # 솔로/프레임워크 자기 회귀 — 현행 venv pytest argv 보존(불변).
            result = subprocess.run(
                [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
            )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _code_tree(self) -> Path:
        """코드가 있는 트리 — 회귀 cwd 해소와 **같은 규칙**(task 작업공간 > 명시 > 자동 슬롯).

        분리된 PM 홈엔 코드가 없으므로 diff 측정도 회귀와 같은 트리를 봐야 한다."""
        if self._task_workspace is not None:
            return self._task_workspace
        if self._regression_cwd is not None:
            return Path(self._regression_cwd)
        return Path(_regression_cwd())

    def _diff_ticket_inputs(self, ticket_id: str, external) -> DiffTicketInputs:
        """현재 티켓 touches/estimate와 board 모듈 실패를 한 번에 해소한다."""
        snapshot = _ticket_frontmatter_snapshot(self._board_py, ticket_id)
        if snapshot.error is None:
            # 기존 함수 표면을 유지해 DI 테스트와 정상 board YAML 의미를 그대로 보존한다.
            return DiffTicketInputs(
                get_ticket_touches(self._board_py, ticket_id),
                get_ticket_estimate(self._board_py, ticket_id),
                None,
            )
        fallback = _fallback_ticket_frontmatter(
            self._board_py, ticket_id, external,
        )
        return DiffTicketInputs(
            _clean_touches(fallback.get("touches")),
            fallback.get("estimate") if isinstance(fallback.get("estimate"), str) else None,
            snapshot.error,
        )

    @staticmethod
    def _warn_diff_attribution_failure(error: str) -> None:
        """현재/claimed board 실패의 단일 loud 진단 표면."""
        print(
            "  ⚠ diff 서킷브레이커 귀속 보정 skip — claimed 보드 읽기 실패 "
            f"({error}). 타 티켓 제외 없이 현재 touches 전체를 측정합니다.",
            file=sys.stderr,
        )

    def _measured_touches(self, ticket_id: str) -> list[str] | None:
        """서킷브레이커가 잴 스코프 — 선언 `touches` 를 **측정 트리 좌표**로 정규화한 것.

        PM 홈 좌표(`work/<repo>_<N>/…`)를 그대로 재면 측정 트리(코드 worktree)에 그 경로가 없어
        diff 가 0 으로 나오고 상한이 조용히 우회된다. 정규화 규칙은 stage 경로가 쓰는 공용
        `repo_coordinates` seam 그대로다 — 좌표계 사본을 두면 한쪽만 고쳐진다.

        정규화 불능(normalizer 부재·슬롯 불일치)은 "이 트리에서는 잴 수 없다"는 뜻이므로
        **loud 1줄 + 가드 off**(None) 다. 조용한 0 으로 접으면 우회 구멍이 그대로 남는다.
        """
        touches = get_ticket_touches(self._board_py, ticket_id)
        return self._normalize_measured_touches(touches, warn=True)

    def _normalize_measured_touches(
        self, touches: Sequence[str], *, warn: bool,
    ) -> list[str] | None:
        """한 티켓의 touches 를 측정 트리 좌표로 옮긴다.

        현재 티켓은 실패를 loud 하게 알리고 가드를 끄지만, 타 티켓은 정규화 실패 시 그 티켓을
        제외 근거로 쓰지 않는다. 후자는 **과다 측정** 쪽으로 남아 귀속 불명 변경이 조용히
        사라지지 않는다.
        """
        if not touches:
            return None
        if not any(_has_worktree_touch_prefix(touch) for touch in touches):
            return list(touches)
        coords = _load_repo_coordinates()
        if coords is None:
            if warn:
                print(f"  ⚠ diff 서킷브레이커 측정 skip — repo 좌표 normalizer 부재 "
                      f"({TOOLS_DIR / 'repo_coordinates.py'}) · PM 홈 좌표 touches 를 측정 트리 "
                      "좌표로 옮길 수 없다.", file=sys.stderr)
            return None
        # 측정 트리 해소는 try **밖**이다 — 그쪽은 형제 엔진 로더를 타므로(회귀 cwd 자동해소)
        # 여기 좌표 예외 처리에 섞이면 사본 skew 가 "정규화 실패" 한 줄로 위장된다.
        workspace = self._code_tree()
        try:
            return list(coords.normalize_repo_paths(
                touches, pm_root=REPO, workspace=workspace,
            ))
        except getattr(coords, "RepoCoordinateError", RuntimeError) as exc:
            if warn:
                print(f"  ⚠ diff 서킷브레이커 측정 skip — touches 좌표 정규화 실패 ({exc}).",
                      file=sys.stderr)
            return None

    @staticmethod
    def _canonical_touch_path(path: str) -> str:
        """touches 경로를 귀속 비교용 POSIX 상대 표기로 접는다."""
        normalized = path.strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.rstrip("/")
        return normalized or "."

    @classmethod
    def _touch_contains(cls, parent: str, child: str) -> bool:
        """parent touches 가 child touches 를 포함하는가(문자열 경계 보존)."""
        parent = cls._canonical_touch_path(parent)
        child = cls._canonical_touch_path(child)
        return parent == "." or child == parent or child.startswith(parent + "/")

    @classmethod
    def _touch_claims_path(cls, touch: str, changed_path: str) -> bool:
        """touches 선언이 변경 파일을 포함하는가.

        경로 magic/glob 은 이 작은 귀속기가 Git 과 완전히 같게 해석할 수 없다. 그런 선언을
        `False` 로 접어 제외하면 현재 티켓 몫을 숨길 수 있으므로, 불확실한 선언은 일치(True)로
        취급한다. 오차는 과다 측정 쪽이고 서킷브레이커를 약화하지 않는다.
        """
        canonical = cls._canonical_touch_path(touch)
        if canonical.startswith(":(") or any(char in canonical for char in "*?["):
            return True
        return cls._touch_contains(canonical, changed_path)

    @classmethod
    def _numstat_paths(cls, external, field: str) -> tuple[str, ...]:
        """numstat 경로 필드의 논리 경로들 — rename은 source와 destination 둘 다."""
        if " => " not in field:
            return (cls._canonical_touch_path(external._numstat_path(field)),)
        source = re.sub(r"\{([^{}]*) => ([^{}]*)\}", r"\1", field)
        destination = re.sub(r"\{([^{}]*) => ([^{}]*)\}", r"\2", field)
        if " => " in source:
            source = source.split(" => ", 1)[0].strip()
        if " => " in destination:
            destination = destination.rsplit(" => ", 1)[-1].strip()
        return (
            cls._canonical_touch_path(source),
            cls._canonical_touch_path(destination),
        )

    @staticmethod
    def _diff_numstat_by_path(
        external, root: Path, paths: Sequence[str], *, run_fn=None,
    ) -> tuple[DiffPathStat, ...]:
        """external_review 측정 단계의 numstat 한 벌을 경로별 총량으로 접는다.

        `_stage_diff_runs` 는 staged+unstaged+untracked 와 HEAD~1 폴백을 소유하고,
        `_sum_numstat` 은 binary/machine-mirror 제외를 소유한다. 이 함수는 그 결과 **한 번**을
        경로별로 나눌 뿐이며 claimed 티켓마다 git diff 를 다시 실행하지 않는다.
        """
        required = ("_diff_bases", "_stage_diff_runs", "_sum_numstat", "_numstat_path")
        if not all(hasattr(external, name) for name in required):
            raise AttributeError("external_review numstat seam 부재")
        for stage_base in external._diff_bases("HEAD"):
            runs = external._stage_diff_runs(
                root, stage_base, list(paths), run_fn=run_fn,
                extra_args=("--numstat",),
            )
            text = "".join(
                result.stdout for result in runs if result.returncode == 0
            )
            if not text.strip():
                continue
            totals: dict[tuple[str, ...], int] = {}
            for line in text.splitlines():
                fields = line.split("\t")
                if len(fields) < 3:
                    continue
                logical_paths = TicketFinisher._numstat_paths(external, fields[2])
                totals[logical_paths] = (
                    totals.get(logical_paths, 0) + external._sum_numstat(line)
                )
            return tuple(
                DiffPathStat(logical_paths, amount)
                for logical_paths, amount in totals.items()
            )
        return ()

    @staticmethod
    def _head_line_count(root: Path, path: str, *, run_fn=None) -> int | None:
        """rename source의 HEAD blob 줄 수(읽기 실패면 None)."""
        runner = run_fn or subprocess.run
        result = runner(
            ["git", "-C", str(root), "show", f"HEAD:{path}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            return None
        return len((result.stdout or "").splitlines())

    @staticmethod
    def _worktree_line_count(root: Path, path: str) -> int | None:
        """rename destination의 현재 파일 줄 수(읽기 실패면 None)."""
        try:
            return len(_load_file_lock().read_text_shared(root / path, encoding="utf-8", errors="replace").splitlines())
        except (OSError, UnicodeError):
            return None

    def _ticket_diff_attribution(
        self, ticket_id: str, external, touches: Sequence[str], *, run_fn=None,
        board_error: str | None = None,
    ) -> DiffAttribution:
        """claimed 합집합 numstat 한 벌에서 현재 티켓 몫과 타 티켓 전용 몫을 가른다.

        겹침 규칙: 변경 파일을 현재 티켓 touches 가 **파일 또는 상위 디렉터리 어떤 형태로든**
        포함하면 그 파일 전체를 현재 측정에 유지한다. 한 파일의 hunks 를 touches 만으로 티켓별
        분리할 증거가 없기 때문이다. 현재 티켓은 전혀 주장하지 않고 타 claimed 티켓만 주장하는
        파일만 제외한다. 모호함을 유지(과다 측정) 쪽으로 접는 것이 자기 산출을 숨겨 가드를
        약화하는 것보다 안전하다.
        """
        root = self._code_tree()
        snapshot = (ClaimedTouchesSnapshot({}, board_error) if board_error is not None
                    else get_claimed_ticket_touches(self._board_py))
        claimed: dict[str, list[str]] = {}
        attribution_error = snapshot.error
        if attribution_error is None:
            for claimed_id, raw_touches in snapshot.tickets.items():
                normalized = self._normalize_measured_touches(raw_touches, warn=False)
                if raw_touches and normalized is None:
                    attribution_error = f"{claimed_id}: touches 좌표 정규화 실패"
                    claimed = {}
                    break
                claimed[claimed_id] = normalized or []
        if attribution_error is not None and board_error is None:
            self._warn_diff_attribution_failure(attribution_error)

        other_claims = {
            claimed_id: claimed_touches
            for claimed_id, claimed_touches in claimed.items()
            if claimed_id != ticket_id and claimed_touches
        }
        if attribution_error is not None or not other_claims:
            # 보정할 타 티켓이 없거나 보드가 불완전하면 기존 단일-ticket 측정식을 그대로 쓴다.
            # 특히 실패를 가드 off 로 접지 않고 현재 touches 전체의 상한 판정을 계속한다.
            measure_kwargs = {"run_fn": run_fn} if run_fn is not None else {}
            total = external.diff_line_total(
                root, "HEAD", list(touches), **measure_kwargs,
            )
            return DiffAttribution(total, 0, ())

        measurement_paths = list(touches)
        measurement_paths.extend(
            touch for claimed_touches in claimed.values() for touch in claimed_touches
        )
        measurement_paths = list(dict.fromkeys(
            self._canonical_touch_path(path) for path in measurement_paths
        ))
        try:
            by_path = self._diff_numstat_by_path(
                external, root, measurement_paths, run_fn=run_fn,
            )
        except AttributeError:
            # 부분 설치/구형 external_review 에선 귀속 보정을 포기하되 종전 측정은 유지한다.
            total = external.diff_line_total(root, "HEAD", list(touches))
            return DiffAttribution(total, 0, ())

        total = 0
        excluded_total = 0
        excluded_ids: set[str] = set()
        for stat in by_path:
            endpoint_claims = tuple(
                any(self._touch_claims_path(touch, path) for touch in touches)
                for path in stat.paths
            )
            owners = {
                claimed_id for claimed_id, claimed_touches in other_claims.items()
                if any(
                    self._touch_claims_path(touch, path)
                    for touch in claimed_touches for path in stat.paths
                )
            }
            if len(stat.paths) == 2 and any(endpoint_claims):
                # Git은 source만 pathspec에 주면 삭제 전체, destination만 주면 추가 전체를 세지만,
                # 둘 다 주면 rename delta만 센다. claimed 합집합 측정은 항상 두 endpoint를 넣으므로
                # source/destination 단독 claim의 종전 폭을 여기서 복원한다(50→0 축소 방지).
                if all(endpoint_claims):
                    total += stat.amount
                elif endpoint_claims[0]:
                    source_lines = self._head_line_count(
                        root, stat.paths[0], run_fn=run_fn,
                    )
                    total += stat.amount if source_lines is None else source_lines
                else:
                    destination_lines = self._worktree_line_count(root, stat.paths[1])
                    total += stat.amount if destination_lines is None else destination_lines
            elif any(endpoint_claims) or not owners:
                total += stat.amount  # 겹침/불명은 유지 — 과다 측정이 안전한 방향이다.
            elif stat.amount > 0:
                excluded_total += stat.amount
                excluded_ids.update(owners)
        return DiffAttribution(total, excluded_total, tuple(sorted(excluded_ids)))

    def _default_diff_cap_block(self, ticket_id: str) -> str | None:
        """diff 서킷브레이커 판정 — 차단 안내 문자열, 통과·가드 off 면 None.

        상한 표·측정식·문구는 external_review 소유분을 그대로 쓴다(기계 mirror 제외도 그
        측정 seam 이 소유한다 — 여기 사본 없음). 측정 불가(모듈 부재·touches 부재·좌표 정규화
        불능·estimate 미선언·비-git 트리)는 **가드 off** 다 — 이 축의 실패로 완료 부기를 막지
        않는다(hard 차단은 상한 초과라는 확정 사실에만 건다)."""
        external = _load_external_review()
        if external is None:
            return None
        inputs = self._diff_ticket_inputs(ticket_id, external)
        if inputs.board_error is not None:
            self._warn_diff_attribution_failure(inputs.board_error)
        touches = self._normalize_measured_touches(inputs.touches, warn=True)
        if not touches:
            return None
        estimate = inputs.estimate
        cap = external._diff_cap(external.local_config(REPO), estimate)
        if cap is None:
            return None
        try:
            attribution = self._ticket_diff_attribution(
                ticket_id, external, touches, board_error=inputs.board_error,
            )
        except OSError:
            return None
        block = external.diff_cap_block(
            attribution.total, cap, ticket=ticket_id, estimate=estimate, scope=touches,
        )
        if block is None:
            return None
        excluded_ids = ", ".join(attribution.excluded_ticket_ids) or "(없음)"
        return (f"{block}\n  타 claimed 티켓 귀속 제외: {attribution.excluded_total}줄"
                f" · 티켓 {excluded_ids}")

    def _default_dod_block(self, ticket_id: str) -> str | None:
        """DoD 부기 게이트 preflight 판정 — 차단 사유 문자열, 통과·판정 불가면 None.

        규칙 소유자는 board(`_dod_open_items`)다 — 판정 사본을 두지 않고 그 함수를 부른다.
        권위 있는 차단은 여전히 [3/5] `board.py complete` 가 하고, 여기서는 **더 앞에서 한 번 더**
        묻는다. 이 preflight 가 없으면 [2/5] 가 log 스켈레톤을 append 한 *뒤에* [3/5] 가 막아,
        차단될 때마다 stray 스켈레톤이 남고 재실행마다 중복 append 된다(실측).

        판정 불가(board 미로드·티켓 부재/손상)는 None 이다 — preflight 는 조기 안내일 뿐이고
        진짜 게이트가 뒤에 있으므로, 여기서 fail-soft 해도 미체크 DoD 가 done 으로 새지 않는다."""
        board = load_board_module(self._board_py)
        gate = getattr(board, "_dod_open_items", None) if board else None
        if gate is None:
            return None
        try:
            _status, path = board.find_ticket(ticket_id)
            _fm, body = board.load_ticket(path)
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 뒤 게이트가 소유(preflight fail-soft).
            if _is_engine_rev_skew(exc):
                raise
            return None
        problems = gate(body)
        if not problems:
            return None
        forms = getattr(board, "_DOD_VALUE_FORMS", "")
        lines = [f"완료 조건(DoD) 미마감 — {ticket_id} ({len(problems)}건)"]
        lines += [f"  ✗ {problem}" for problem in problems]
        lines.append(f"  · 각 항목을 {forms} 로 마감하라." if forms else "")
        lines.append("  · board.py complete 의 게이트와 **같은 판정**이다 — "
                     "log/current.md 스켈레톤을 남기기 전에 미리 묻는다.")
        return "\n".join(line for line in lines if line)

    def _default_run_board(self, args: list[str]) -> tuple[int, str]:
        """board.py 를 subprocess 로 호출해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            [str(self._venv_python), str(self._board_py)] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        """git 명령을 실행해 (returncode, stdout+stderr) 반환 — 실패 진단 메시지 보존용."""
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

    def _default_run_git_stdout(self, args: list[str]) -> tuple[int, str]:
        """git 명령을 실행해 (returncode, **stdout 만**) 반환 — 기계 파싱용.

        `status --porcelain`·`ls-files -z` 출력을 파싱하는 경로는 stderr 가 섞이면 안 된다:
        git 경고(예: CRLF·advice) 한 줄이 그대로 '잔여 항목'·'추적 경로'로 둔갑한다.
        """
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        return result.returncode, result.stdout

    @staticmethod
    def _default_run_git_at(cwd: Path, args: list[str]) -> tuple[int, str]:
        """명시 cwd git 실행 — task worktree 를 PM 홈과 절대 섞지 않는다."""
        result = subprocess.run(
            ["git"] + args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(cwd),
        )
        return result.returncode, result.stdout + result.stderr

    @staticmethod
    def _default_run_git_stdout_at(cwd: Path, args: list[str]) -> tuple[int, str]:
        """task worktree의 기계 파싱용 git 실행 — stderr 경고를 결과에 섞지 않는다."""
        result = subprocess.run(
            ["git"] + args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(cwd),
        )
        return result.returncode, result.stdout

    def _default_board_count(self) -> int:
        """board.md 의 done 티켓 수를 반환한다 (board.py 를 import 해서).

        실패 시 -1 반환.
        """
        return count_board_done(self._board_py)

    def _default_ticket_title(self, ticket_id: str) -> str:
        """ticket_id 의 title 을 board.py 를 import 해서 읽어온다.

        실패 시 빈 문자열 반환.
        """
        return get_ticket_title(self._board_py, ticket_id)

    def _default_affected_domain(self, ticket_id: str) -> list[tuple[str, bool | None]] | None:
        """ticket touches ∩ domain covers 로 영향받는 페이지 (title, stale) 목록 (soft 알림).

        domain.py 부재/로드 실패 → None (조용히 skip). domain.pages_for_touches 재사용.
        """
        return affected_domain_titles(ticket_id, self._board_py)

    def _default_stage_scope(self, ticket_id: str) -> StageScope:
        """stage 할 선언 경로 pathspec (`touches ∪ 이 실행이 쓴 산출물`) — 실 해소."""
        return stage_scope(ticket_id, self._board_py, self._log_file,
                           self._run_git_stdout_fn)

    def _task_stage_scope(self, ticket_id: str) -> StageScope:
        """task worktree 에는 ticket touches만 계획한다 (홈 산출물 유입 금지)."""
        assert self._task_workspace is not None

        def run_git(args: list[str]) -> tuple[int, str]:
            return self._run_git_stdout_at_fn(self._task_workspace, args)

        return stage_scope(ticket_id, self._board_py, self._log_file, run_git,
                           repo=self._task_workspace, include_engine_outputs=False,
                           touches_workspace=self._task_workspace)

    def _default_status_entries(self) -> tuple[tuple[str, str], ...]:
        """워킹트리 상태 `((XY, 경로), …)` (loud 보고 입력) — 실 해소.

        board 를 넘기는 이유는 **NUL 파서 공유** 뿐이다(비-ASCII 경로 실경로화). 로드 실패는
        보고를 멈추지 않는다 — `status_entries` 가 degraded 판독으로 이어간다.
        """
        return status_entries(self._run_git_stdout_fn, load_board_module(self._board_py))

    def _default_status_entries_at(self, cwd: Path) -> tuple[tuple[str, str], ...]:
        return status_entries(lambda args: self._run_git_stdout_at_fn(cwd, args),
                              load_board_module(self._board_py))

    def _stage_plans(self, ticket_id: str) -> tuple[RepoStagePlan, ...]:
        """실제 stage 와 dry-run 이 공유하는 repo별 계획 단일 진실.

        task-mode 는 PM 홈에 log/board 산출물, worktree 에 code touches를 각각 둔다.
        비-task 경로는 기존 단일 PM-home 계획을 그대로 쓴다.
        """
        if self._task_workspace is None:
            return (RepoStagePlan("PM 홈", REPO, self._stage_scope_fn(ticket_id)),)
        home_scope = stage_scope(
            ticket_id, self._board_py, self._log_file, self._run_git_stdout_fn,
            include_touches=False,
        )
        return (
            RepoStagePlan("PM 홈 산출물", REPO, home_scope),
            RepoStagePlan("task worktree touches", self._task_workspace,
                          self._task_stage_scope(ticket_id)),
        )

    # ── 메인 흐름 ────────────────────────────────────────────────────

    def _warn_pm_direct_conditions(self, ticket_id: str) -> None:
        """PM-direct a·b를 재검하되 finish 흐름/반환 코드는 막지 않는다."""
        snapshot = _ticket_frontmatter_snapshot(self._board_py, ticket_id)
        if snapshot.error is not None or snapshot.frontmatter.get("tier") != "pm-direct":
            return
        touches = _clean_touches(snapshot.frontmatter.get("touches"))
        measured_touches = ([] if not touches else
                            self._normalize_measured_touches(touches, warn=False))
        if measured_touches is None:
            print(
                "  ⚠ PM-direct 재검 skip — touches 좌표를 컨텐츠 worktree로 "
                "정규화하지 못했습니다. raw PM-home 경로로 판정하지 않습니다.",
                file=sys.stderr,
            )
            return
        try:
            board = load_board_module(self._board_py)
            expand = getattr(board, "expand_owned_touch_files", None) if board else None
            if expand is None:
                raise RuntimeError("board expand_owned_touch_files seam 부재")
            expanded = expand(self._code_tree(), measured_touches)
            entries = (self._status_entries_at_fn(self._task_workspace)
                       if self._task_workspace is not None else self._status_entries_fn())
            changed_paths = tuple(
                path for _xy, path in entries
                if any(self._touch_claims_path(touch, path) for touch in measured_touches)
            )
        except Exception as exc:  # noqa: BLE001 — advisory 재검은 never-block.
            if _is_engine_rev_skew(exc):
                raise
            detail = " ".join(str(exc).split()) or type(exc).__name__
            print(f"  ⚠ PM-direct 재검 skip — git 변경 경로를 읽지 못했습니다 ({detail}).",
                  file=sys.stderr)
            return
        for warning in pm_direct_finish_warnings(
                snapshot.frontmatter.get("tier"), touches, changed_paths,
                touched_file_count=len(expanded.paths),
                unresolved_directories=expanded.unresolved_directories):
            print(f"  ⚠ {warning}", file=sys.stderr)

    def run(
        self,
        ticket_id: str,
        section: str | None,
        dry_run: bool,
        skip_pytest: bool = False,
        task: str | None = None,
        session: str | None = None,
    ) -> int:
        """ticket_id 완료 부기 전체 흐름을 실행한다.

        반환: 0=성공, 1=실패 (중단).

        `section` 은 후방호환용으로 받기만 하고 무시한다 — status.md 합계표 섹션 행은
        제거됐다(judgment-only·테스트 수는 박제 안 함).

        `skip_pytest`(--no-pytest) 는 [1/5] 회귀 단계를 건너뛴다 — 측정은 PM 이 /pm-qa
        등으로 별도. board complete 는 `--tests-pass` 를 유지한다(pm_handoff `--no-pytest` 동형·
        회귀 red 아님·skip 로 진행). 다중슬롯 형상에서 회귀 cwd 를 정할 수 없을 때 우회 수단.
        """
        del section  # status 합계표 제거로 더 이상 쓰지 않음(후방호환 수용만).
        print(
            f"[ticket_finish] {ticket_id} 완료 부기 시작 "
            f"(dry_run={dry_run}, skip_pytest={skip_pytest})"
        )

        # PM 판정은 권위를 유지한다. 이 재검은 경고만 보이고 이후 rc를 바꾸지 않는다.
        self._warn_pm_direct_conditions(ticket_id)

        # ── 0. 진입 게이트(preflight) — diff 서킷브레이커 · DoD 부기 ─────
        # 둘 다 회귀보다 **앞**이고, 무엇보다 [2/5] log 스켈레톤 append 보다 앞이다: 여기서 막힐
        # 실행은 어떤 부작용(회귀 실행·log append·board·git)도 내지 않아야 한다. DoD 판정이
        # [3/5] `board.py complete` 안에만 있던 동안에는 차단마다 stray 스켈레톤이 남고 재실행이
        # 그것을 중복 append 했다(실측) — 순서가 곧 결함이었다.
        for preflight in (self._diff_cap_block_fn, self._dod_block_fn):
            block = preflight(ticket_id)
            if block:
                print(f"\n[중단] {block}", file=sys.stderr)
                return 1

        # ── 1. 회귀 실행 ──────────────────────────────────────────────
        # dry-run 도 pytest 를 실제 실행한다 — "부작용 없음"이지 "빠름"이 아니다.
        # 파일·board·git 편집만 생략하므로 pytest 실행은 항상 수행. (--no-pytest 는 예외 — 측정 skip.)
        print("\n[1/5] 회귀 실행 중...")
        if skip_pytest:
            # --no-pytest — 회귀 측정은 PM 이 별도(/pm-qa 등). board complete 는 --tests-pass 유지
            # (pm_handoff --no-pytest 동형·red 아님·skip 로 진행). 측정 total 은 log 스켈레톤에서 "?".
            print(
                "  [--no-pytest] 회귀 측정 skip — 측정은 별도("
                f"{_runtime_skill_entry('pm-qa')} 등). board complete 는 --tests-pass 유지."
            )
            new_total: int | str = "?"
        else:
            # 게이트 종류(pytest 스위트 / 해소된 임의 명령)를 먼저 해소한다 — green 판정
            # 기준과 안내 문구가 여기서 갈린다(`_gate_is_pytest`).
            gate_cmd = _resolve_per_repo_test_cmd()
            if dry_run:
                print(
                    f"  [dry-run] {_gate_label(gate_cmd)} 실행 중 "
                    "(파일·board·git 편집만 생략)..."
                )
            returncode, output = self._run_pytest_fn()
            print(output.rstrip())

            if not _regression_is_green(output, returncode, gate_cmd):
                print(
                    "\n[중단] 회귀 red — log/current.md·board·git 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                if _gate_is_pytest(gate_cmd):
                    print(
                        "원인: pytest 가 실패를 보고했거나 출력 파싱 실패.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"원인: 회귀 명령이 실패했다 — `{gate_cmd}` (exit {returncode}).",
                        file=sys.stderr,
                    )
                return 1

            if _gate_is_pytest(gate_cmd):
                parsed = parse_pytest_output(output)
                if parsed is None:
                    print(
                        "\n[중단] pytest 출력 파싱 실패 — passed 수를 읽지 못했다.",
                        file=sys.stderr,
                    )
                    return 1

                new_total, deselected = parsed
                print(f"\n  ✓ green: passed={new_total}, deselected={deselected}")
            else:
                # 비-pytest 게이트 — 테스트 수 개념이 없으므로 측정하지 않는다("?" ·
                # `--no-pytest` 경로와 동형). green 근거는 exit 0 이다.
                new_total = "?"
                print(f"\n  ✓ green: `{gate_cmd}` (exit 0) — 테스트 수 미측정")

        # status.md 는 더 이상 갱신하지 않는다.
        # 테스트 수는 위 pytest 실측이 단일 진실·history 는 아래 log skeleton 으로 남는다.

        # ── 2. log/current.md 스켈레톤 append ────────────────────────────────
        print("\n[2/5] log/current.md 스켈레톤 append...")
        board_before = self._board_count_fn()
        board_after = board_before + 1  # board complete 후 +1

        title = self._ticket_title_fn(ticket_id)
        if not title:
            title = f"<{ticket_id} 제목을 읽지 못했습니다>"

        skeleton = build_log_skeleton(
            ticket_id=ticket_id,
            title=title,
            new_total=new_total,
            board_before=board_before,
            board_after=board_after,
            task=task,
            session=session,
        )

        if dry_run:
            print("  [dry-run] log/current.md 에 append 할 스켈레톤:")
            print("  " + skeleton.replace("\n", "\n  "))
        else:
            _load_pm_log().append_log(self._log_file, "\n" + skeleton)
            print(f"  ✓ log/current.md 스켈레톤 append ({ticket_id})")

        # ── 3. board.py complete ──────────────────────────────────────
        print("\n[3/5] board.py complete...")
        if dry_run:
            print(f"  [dry-run] board.py complete {ticket_id} --tests-pass")
        else:
            board_rc, board_out = self._run_board_fn(
                ["complete", ticket_id, "--tests-pass"]
            )
            print(f"  {board_out.rstrip()}")
            if board_rc != 0:
                print(
                    f"\n[중단] board.py complete 실패 (rc={board_rc}). "
                    "log/current.md 는 이미 편집됐다.",
                    file=sys.stderr,
                )
                return 1
            print(f"  ✓ board: {ticket_id} → done")

        # ── 4. git stage (선언 경로 스코프) ────────────────────
        # blanket `add -A` 가 아니다 — 이 티켓이 선언한 경로(`touches` ∪ *이 실행이 쓴* 산출물)만
        # stage 한다. 좁힘이 만드는 반대편 실패(under-stage)는 아래 잔여 dirty loud 보고로
        # 가시화한다(차단하지 않는다 — `touches` 는 사람이 적는 값이라 누락 가능).
        print("\n[4/5] git stage (선언 경로 스코프)...")
        plans = self._stage_plans(ticket_id)
        multi_repo = len(plans) > 1
        for plan in plans:
            scope, scope_error = plan.scope
            if multi_repo:
                print(f"  [{plan.label}] cwd={plan.cwd}")
            if scope_error:
                # 판정기가 죽은 경우 — 조용한 no-op 금지(stage 0 이 '정상 완료'로 보인다).
                label = f"[{plan.label}] " if multi_repo else ""
                print(f"\n  ⚠ {label}stage 스코프를 산출하지 못했다 — {scope_error}. "
                      "이번 repo에서는 아무것도 stage 하지 않는다. 아래 잔여 목록을 보고 "
                      "PM 이 직접 `git add <경로>` 하라.", file=sys.stderr)
            if dry_run:
                preview = " ".join(scope) if scope else "(선언 경로 없음)"
                cwd = f"cwd={plan.cwd} " if multi_repo else ""
                print(f"  [dry-run] {cwd}git add -A -- {preview} (실제 실행 생략)")
            elif scope:
                if plan.cwd == REPO:
                    git_rc, git_out = self._run_git_fn(["add", "-A", "--", *scope])
                else:
                    git_rc, git_out = self._run_git_at_fn(plan.cwd, ["add", "-A", "--", *scope])
                if git_rc != 0:
                    print(
                        f"\n[중단] {'[' + plan.label + '] ' if multi_repo else ''}git add 실패 (rc={git_rc}): {git_out.rstrip()}",
                        file=sys.stderr,
                    )
                    return 1
                label = f"[{plan.label}] " if multi_repo else ""
                print(f"  ✓ {label}git add — 선언 경로 {len(scope)}개만 stage (commit 은 아직 안 했다):")
                for rel in scope:
                    print(f"      {rel}")
            elif not scope_error:
                label = f"[{plan.label}] " if multi_repo else ""
                print(f"  {label}stage 할 선언 경로 없음")
            if not dry_run:
                self._report_dirty_after_stage(scope, cwd=plan.cwd,
                                               label=plan.label if multi_repo else "")

        # ── 5. 잔여 PM 작업 출력 ─────────────────────────────────────
        print("\n[5/5] PM 이 손으로 할 잔여 작업:")
        print("  log/current.md 서술 불릿 채우기 — <!-- PM: 무엇을·왜 서술 --> 를 실제 내용으로 교체")
        print("  status.md 모듈 행(상태 + 비고) — 변경된 모듈 행 판정을 architect/PM 이 직접 갱신 (테스트 수는 박제 안 함)")
        # 단일 repo 출력은 기존 커밋 안내 문구를 byte-compatible하게 보존한다. 두 repo 계획일 때만
        # cwd별 안내로 확장해 PM 홈 성공이 task worktree 누락을 가리지 못하게 한다.
        if not multi_repo:
            print("  git commit — **경로를 명시**하라: "
                  "`git commit -m \"<메시지>\" -- <위 [4/5] 가 stage 한 경로들>` "
                  "(메시지는 PM 이 작성 · Co-Authored-By: Claude 트레일러 포함)")
        else:
            print("  git commit — **repo별 cwd와 경로를 명시**하라 "
                  "(메시지는 PM 이 작성 · Co-Authored-By: Claude 트레일러 포함):")
            for plan in plans:
                scope, scope_error = plan.scope
                if scope_error or not scope:
                    reason = scope_error or "stage 할 선언 경로 없음"
                    print(f"      [{plan.label}] cwd={plan.cwd}: commit 하지 말 것 ({reason})")
                    continue
                pathspec = " ".join(scope)
                print(f"      [{plan.label}] cwd={plan.cwd}: "
                      f"`git commit -m \"<메시지>\" -- {pathspec}`")

        # ── soft 알림: 영향받는 domain 페이지 (비차단) ──────
        # 정보일 뿐 게이트가 아니다 — 완료 흐름·rc 를 막지 않는다(예외도 삼킨다).
        # domain.py 부재(솔로/신규 clone) → None → 조용히 skip(무영향).
        self._notify_affected_domain(ticket_id)

        if dry_run:
            print("\n[dry-run] 완료 — 실제 편집·board·git 는 실행하지 않았다.")
        else:
            # 잔여 건수를 마지막 줄에 한 번 더 — [4/5] 보고가 이후 출력에 묻히지 않게(loud).
            staged_out, unstaged = self._dirty_summary
            tail = ""
            if unstaged or staged_out:
                tail = (f" ⚠ 미스테이지 잔여 {unstaged}건 · 스코프 밖 staged {staged_out}건 "
                        "— 위 [4/5] 목록 확인.")
            print(f"\n[완료] {ticket_id} 부기 완료.{tail}")

        return 0

    # 잔여 보고 최대 줄 수 — 넘으면 "… 외 N건" 으로 접는다(수백 줄 홍수 방지).
    _RESIDUAL_DIRTY_PREVIEW_LINES = 20

    def _print_dirty_block(self, header: str, lines: Sequence[str]) -> None:
        """보고 블록 1개 출력 (헤더 + 최대 N줄 + 접기) — 두 방향 보고가 같은 모양을 쓴다."""
        print(header)
        for line in lines[:self._RESIDUAL_DIRTY_PREVIEW_LINES]:
            print(f"      {line}")
        hidden = len(lines) - self._RESIDUAL_DIRTY_PREVIEW_LINES
        if hidden > 0:
            print(f"      … 외 {hidden}건")

    def _dirty_split(self, scope: Sequence[str], *, cwd: Path = REPO) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """현재 상태를 (스코프 밖 staged, 미스테이지 잔여)로 가른다 — 조회 실패는 빈 목록."""
        try:
            entries = (self._status_entries_fn() if cwd == REPO
                       else self._status_entries_at_fn(cwd))
        except Exception as exc:  # noqa: BLE001 — 보고는 완료를 막지 않는다.
            if _is_engine_rev_skew(exc):
                raise  # 엔진 사본 불일치는 빈 보고로 강등하지 않는다(fail-loud).
            return (), ()
        return split_dirty(entries, scope)

    # stage **전** 보고는 두지 않는다 — 이 도구의 `add` 는 스코프 경로만 올리므로 사전에 staged
    # 돼 있던 스코프 밖 변경은 stage 후에도 그대로 남고, 아래 사후 보고가 같은 목록을 낸다.
    # 별도 사전 보고는 같은 경고를 두 번 내면서 고유 판정이 없었다(teeth 0·reviewer 실측).

    def _report_dirty_after_stage(self, scope: Sequence[str], *, cwd: Path = REPO,
                                  label: str = "") -> None:
        """stage 후 상태를 양방향으로 **눈에 띄게** 보고한다 (loud·비차단).

        두 실패 방향을 함께 본다 — ① **미스테이지 잔여**(under-stage: 스코프가 못 덮은 변경 —
        내 작업 누락이면 `touches` 를 보강해 다시 stage, 남의 WIP 면 그대로 둔다) ②
        **스코프 밖 staged**(누출: PM 커밋에 실린다). 좁히기가 만드는 실패를 *조용한 유출* 과
        바꾸지 않는 것이 요점이라, 어느 쪽도 침묵하지 않는다. 결과 건수는 `[완료]` 줄에서
        한 번 더 재고지한다(보고가 뒤 출력에 묻히지 않게).
        """
        staged_out, unstaged = self._dirty_split(scope, cwd=cwd)
        old_staged, old_unstaged = self._dirty_summary
        self._dirty_summary = (old_staged + len(staged_out), old_unstaged + len(unstaged))
        prefix = f"[{label}] " if label else ""
        if not staged_out and not unstaged:
            print(f"  ✓ {prefix}스코프 밖 잔여 변경 없음 (staged·미스테이지 모두)")
            return
        if unstaged:
            self._print_dirty_block(
                f"\n  ⚠ {prefix}미스테이지 잔여 {len(unstaged)}건 — **이 커밋에 안 실린다**. 내 작업 "
                "누락이면 ticket `touches` 를 보강해 다시 stage 하라(남의 WIP 면 그대로 둔다):",
                unstaged)
        if staged_out:
            self._print_dirty_block(
                f"\n  ⚠ {prefix}스코프 밖 staged {len(staged_out)}건 — 내 변경이 아닌데 **PM 커밋에 "
                "실린다**. 빼려면 `git restore --staged <경로>`:",
                staged_out)

    def _notify_affected_domain(self, ticket_id: str) -> None:
        """이 ticket 이 건드린 영역의 domain 페이지를 soft 알림으로 출력한다 (비차단).

        영향 페이지가 stale(covers 코드가 page updated 후 커밋)이면 그 줄 앞에
        `⚠` 를 단다 — fresh(False)/unknown(None)은 무표시. 도그푸딩/multi-PM 어디서든 일반 domain
        부재·예외는 조용히 삼켜 완료를 막지 않되, marked engine skew 는 막는다. 영향 0 이면 한 줄
        안내만 낸다. dry-run/실행 동일(정보 출력만). 일반 stale 예외/unknown 은 비차단.
        """
        print("\n[domain] 영향받는 domain 페이지 (soft·비차단):")
        try:
            affected = self._affected_domain_fn(ticket_id)
        except Exception as exc:  # noqa: BLE001 — 일반 soft 알림 실패만 완료를 막지 않는다.
            if _is_engine_rev_skew(exc):
                raise
            affected = None
        if affected is None:
            print("  (domain 레이어 없음 — skip)")
        elif not affected:
            print("  (영향 domain 페이지 없음)")
        else:
            # 각 영향 페이지: stale(True) 줄 앞에 ⚠ — fresh/unknown 은 무표시.
            labels = [f"⚠ {title}" if stale is True else title for title, stale in affected]
            joined = ", ".join(labels)
            print(f"  📝 이 ticket 이 건드린 영역 domain 페이지: [{joined}] — 갱신 확인(soft)")

# ── CLI ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket_finish.py",
        description="ticket 완료 시 PM 부기 자동화 헬퍼 (v1 축소판).",
    )
    parser.add_argument("ticket_id", metavar="T-NNNN", help="완료할 ticket ID")
    parser.add_argument(
        "--section",
        metavar="섹션명",
        default=None,
        help=(
            "(deprecated·no-op) status.md 합계표 섹션 행은 제거됐다 — "
            "받기만 하고 무시한다(후방호환). status.md = judgment-only."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="편집·board·git 없이 무엇을 바꿀지만 출력한다.",
    )
    # ── 두-git 다중슬롯 seam (pm_handoff 미러) ──────────
    # 분리된 PM 홈 + worktree 슬롯 여럿 형상에서 회귀를 어느 worktree(tests/ 보유)에서
    # 돌릴지 disambiguate 한다 — pm_handoff `--repo`/`--slot`/`--no-pytest` 와 동형(canonical
    # 분해형). `--repo` 단독은 활성(leased) 슬롯 1개면 자동해소·0/≥2 는 fail-loud.
    identity_args.add_identity_args(parser)
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help=f"[1/5] 회귀 단계를 skip 한다 (측정은 {_runtime_skill_entry('pm-qa')} 등으로 "
             "별도·board complete 는 --tests-pass 유지).",
    )
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # PM-홈 worktree 오실행 가드— 부기 어떤 부작용(회귀/log append/complete/stage)도
    # 나기 *전에* fail-loud. 읽기 경로 없음(ticket_finish 는 전부 쓰기 부기)이라 진입에서 한 번.
    if _guard_worktree_misanchor():
        return 1

    # task 실행-위치 핀은 독립 축이다. 혼합을 parse_identity의 bare-slot 오류보다
    # 먼저 거부해 `--repo`를 추가하라는 잘못된 해결책을 안내하지 않는다.
    if args.task is not None and (args.repo is not None or args.slot is not None):
        parser.error(
            "--task 는 독립 정체성이다 — --repo/--slot 과 함께 쓸 수 없다 "
            "(task 보유 작업공간은 장부에서 자동 해소)."
        )

    # 정체성 인자 *검증*(`--slot` 단독·`slot < 1` = uniform fail-loud)은 `--no-pytest` 와
    # 무관하게 **항상** 수행한다. task F6도 회귀 전용 값이 아니다: stage/status 계획의 cwd 단일
    # 진실이므로 --no-pytest 에서도 반드시 해소한다. 반면 slot-mode 모호 게이트는 회귀를 실제로
    # 돌 때만 적용한다(기존 --no-pytest escape hatch 보존).
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as exc:
        parser.error(str(exc))

    # task 명 검증(must-fix 게이트) — board 정체성 깔때기와 **동일 공유 validator**
    # (`identity_args.validate_task_name`·worktree_pool 엔진 validator 와 동형·로직 중복 0)를 소비해,
    # 실행-위치 해소 이전 불법 task 명(traversal/공백/`<repo>_<N>` 예약)을 fail-loud 한다. `--slot`
    # 검증과 동형으로 `--no-pytest` 무관 **항상** 수행(회귀 skip 여도 정체성 검증은 우회 안 됨). 예약패턴
    # 판정용 registered_repos 는 board 모듈에서 fail-soft 해소(부재/실패 시 char/traversal 검증만).
    if identity.task:
        registered = None
        _bmod = _load_board_module()
        if _bmod is not None and hasattr(_bmod, "registered_repos"):
            try:
                registered = _bmod.registered_repos()
            except Exception:  # noqa: BLE001 — areas 파싱 실패는 예약패턴 검증만 완화(char/traversal 유지).
                registered = None
        try:
            identity_args.validate_task_name(identity.task, registered)
        except identity_args.InvalidTaskName as exc:
            print(
                f"\n[중단] 부적합 task 명 {identity.task!r} — {exc.reason} "
                "(`--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴 `<repo>_<N>`은 쓸 수 없다).",
                file=sys.stderr,
            )
            return 1

    regression_cwd: str | None = None
    session: str | None = identity.session
    task_workspace: Path | None = None
    if identity.task:
        # task-mode 해소는 regression·stage·잔여 보고가 공유한다. --no-pytest 는 측정만
        # 생략할 뿐, 실제 code touches를 놓치게 해서는 안 된다.
        try:
            ws = identity_args.resolve_task_workspace(identity, LEASES_FILE)
        except identity_args.WorkspaceResolutionError as exc:
            print(f"\n[중단] 작업공간 해소 — {exc}", file=sys.stderr)
            return 1
        print(f"작업공간(task {identity.task}) → {REPO / ws.slot}")
        task_workspace = REPO / ws.slot
        if not args.no_pytest:
            regression_cwd = _regression_cwd(ws.slot)
    elif not args.no_pytest:
        worktree_slot, ambiguity = _resolve_finish_slot(identity.repo, identity.slot)
        if ambiguity is not None:
            print(f"\n[중단] 회귀 슬롯 해소 모호 — {ambiguity}", file=sys.stderr)
            print(
                "  → `--repo <name> [--slot <N>]`(예: --repo project_manager --slot 1) 으로 슬롯을 "
                f"명시하거나, `--no-pytest` 로 회귀를 skip 하라(측정은 "
                f"{_runtime_skill_entry('pm-qa')} 등으로 별도).",
                file=sys.stderr,
            )
            return 1
        # 해소된 슬롯이 있으면 그 worktree 를 회귀 cwd 로 명시 forward(_regression_cwd 위임).
        # solo/미해소(None)면 regression_cwd 미주입 → _default_run_pytest 런타임 폴백(현행 100% 보존).
        if worktree_slot:
            regression_cwd = _regression_cwd(worktree_slot)
            session = Path(worktree_slot).name

    finisher = TicketFinisher(regression_cwd=regression_cwd, task_workspace=task_workspace)
    return finisher.run(
        ticket_id=args.ticket_id,
        section=args.section,
        dry_run=args.dry_run,
        skip_pytest=args.no_pytest,
        task=identity.task,
        session=session,
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
