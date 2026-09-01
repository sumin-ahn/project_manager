#!/usr/bin/env python3
"""PM 기록 자동화 헬퍼 — ticket 완료 시 기계적 기록을 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/ticket_finish.py T-NNNN [--section "<섹션명>"] [--dry-run]
    venv/bin/python .project_manager/tools/ticket_finish.py --cluster C-<이름> [--dry-run]
        묶음 종결(close) — 아래 다섯 단계를 티켓마다 돌리고 그 앞뒤로 기계 확인·게이트 처분·
        커밋·재배치·머지·슬롯 반납·board 기록까지 고정 순서로 닫는다(재실행은 재개).

동작 순서 (하나라도 실패하면 이후 단계 중단):
  1. 회귀 실행 — 게이트는 areas.md(prefix 행 > repo 행) > local.conf test.cmd 로 해소하고,
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
import contextlib
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import tokenize
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
                    # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                    # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                    # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                    # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                    # ModuleNotFoundError). 블록은 stdlib-only 라 지역 import 로 두되 sys.path 에
                    # parent 를 넣기 전에 가져와 그 트리의 동명 파일이 stdlib 를 가리지 않게 한다.
                    import importlib as _bootstrap_importlib
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        _bootstrap_importlib.invalidate_caches()
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


# ── REPO 앵커 (설치 깊이 고정 · hermetic 테스트 monkeypatch seam) ──────────
# 설치 깊이는 `pm_import` 가 못 박는다 — 도구는 `<root>/.project_manager/tools/` 에만 놓인다.
# REPO 는 module-level 상수로 유지해 hermetic 테스트가 monkeypatch 할 수 있게 한다
# (각 파일 self-contained — 공유 import 미도입).

def _find_repo_root() -> Path:
    """엔진 루트 — 도구는 언제나 `<root>/.project_manager/tools/` 에 설치된다.

    상향 탐색을 하지 않는다. 설치 경로를 만드는 쪽(`pm_import`)이 그 깊이를 못 박으므로
    다른 깊이는 나올 수 없고, 탐색은 합성 트리에서 **자기 위 실 인스턴스**를 답으로 주는
    부작용만 낸다(실측: 실 PM 홈 log 오염·등록 안 된 worktree 가 등록으로 판정).
    나머지 도구 20곳과 같은 규칙이다.
    """
    return Path(__file__).resolve().parents[2]


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
ENGINE_REV = "v1.7.12"


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


def _engine_temp_root(repo: Path) -> Path:
    """그 clone 이 소유한 작업용 임시 루트 — 단일 소유자는 `pm_relay.temp_root` 다.

    자리 규약(`.project_manager/.local/tmp`)을 이 도구가 다시 적지 않는다. 로더는 형제 모듈
    경로 로드 관례 그대로다(`_load_pm_log` 동형·sys.path 무오염).
    """
    relay = _load_module_from_path(
        TOOLS_DIR / "pm_relay.py", "pm_relay.py", verifier=_verify_engine_rev, cache=True,
    )
    return relay.temp_root(repo)


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
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 기본 경로를 깨지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # pm_handoff 가 중첩 로드한 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _load_additional_reviewer():
    """additional_reviewer 모듈을 동적 로드한다 (부재/로드 실패 시 None·fail-soft).

    diff 서킷브레이커의 **정책과 측정식**은 additional_reviewer 가 소유한다(그쪽이 diff 산정 로직의
    단일 진실이다). 완료 기록은 그 판정을 빌려 쓸 뿐이라 사본을 두지 않는다 — 두 표면이 서로 다른
    상한/산정식을 쓰면 "리뷰는 통과했는데 완료가 막힌다"가 규칙이 아니라 사고가 된다."""
    er_path = TOOLS_DIR / "additional_reviewer.py"
    if not er_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            er_path, "additional_reviewer.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패가 완료를 막지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


# 사설 참조 판정식 모듈의 **공용** 캐시 key 접두 — 소비자 이름을 넣지 않는다. 재유입 가드
# (`tests/test_private_context_guard.py`)도 같은 key 로 올리므로 두 소비자가 한 모듈 객체를
# 공유하고, "판정식 사본 0" 이 함수 객체 동일성으로 성립한다.
PRIVATE_REFS_CACHE_KEY_PREFIX = "_project_manager_private_refs:"


def _load_private_refs():
    """사설 참조 판정식 모듈을 동적 로드한다 (부재/로드 실패 시 None·fail-soft).

    판정식(`prose_token_spans`·`_actionable_matches`·`shipping_paths` 등)의 단일 진실은
    `private_refs.py` 다 — 완료 기록은 그 함수를 그대로 호출할 뿐 사본을 두지 않는다.
    `scripts/strip_private_refs.py`·`tests/test_private_context_guard.py` 와 같은 소스를 본다.

    `cache=True` 가 **load-bearing** 이다 — `private_refs.py` 는 postponed annotation
    (`from __future__ import annotations`) 아래 `@dataclass(frozen=True)` 를 쓰는데, 공용
    로더가 `cache=False` 로 실행하면(기본값) `exec_module` 이 돌기 **전에** 모듈이
    `sys.modules` 에 없어 `dataclasses._is_type`(3.12)이 `cls.__module__` 을 못 찾고
    `AttributeError` 로 죽는다(모듈 자기 자신을 찾으려는 dataclass 내부 조회일 뿐, 이 사본이
    다른 소비자와 공유되는 것과는 무관하다)."""
    pr_path = TOOLS_DIR / "private_refs.py"
    if not pr_path.exists():
        return None
    try:
        mod = _load_module_from_path(
            pr_path, "private_refs.py", verifier=_verify_engine_rev,
            cache=True, cache_key=f"{PRIVATE_REFS_CACHE_KEY_PREFIX}{pr_path.resolve()}",
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
      - 그것도 없으면(판정 불능 — 모호·엔진 사본 부재) **현 `REPO`**. 등록된 홈은 홈 자신이
        슬롯 행이라 위 층에서 해소된다.

    pm_handoff 를 동적 로드해 그 함수에 위임하되(DRY — `_auto_slot` 복제 0) **앵커는 이 도구의
    `REPO` 를 넘긴다**(`repo_root=`). 그 인자를 생략하면 해소가 pm_handoff 모듈의 `REPO` 를 따라
    가는데, 두 값이 갈리는 형상(호출자가 앵커를 재지정한 경우)에서는 해소된 코드 트리가 이 도구의
    PM 홈과 무관한 트리를 가리켜 stage/측정이 엉뚱한 repo 로 간다. pm_handoff 부재/로드 실패는
    `str(REPO)` 폴백(현행 100% 보존·additive).
    """
    hp = _load_pm_handoff()
    if hp is not None:
        try:
            return hp._regression_cwd(worktree_slot, repo_root=REPO)
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
      - `(None, None)` — 판정 불능(pm_handoff 사본 부재) → 호출부 REPO 런타임 폴백.
      - `(None, error_msg)` — **진짜 모호**(멀티-PM under-specified·repo≥2·slot1 부재 비단독) 또는
        (명시 repo/slot 이 리스 장부와 조인 불일치) → 호출부 fail-loud.

    `repo`/`slot` 둘 다 `None`(kind='none')이면 기존 no-flag 자동해소로 위임 — pm_handoff
    부재/로드 실패는 fail-soft `(None, None)`(현행 REPO 폴백). `repo`/`slot` 명시인데
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


def _board_identity_argv(identity, slot_identity=None) -> list[str]:
    """`board.py` 하위 호출에 넘길 정체성 인자 (`--repo/--slot`) — 없으면 빈 목록.

    board 의 lifecycle mutation 은 `claimed_by` 와 실행 세션을 대조하므로, 여기서
    해소한 정체성을 인자로 넘겨야 같은 세션으로 판정된다. 우선순위:

      1. **명시 `--repo X --slot N`** — 사람이 claim 할 때 쓴 좌표 그대로 넘긴다.
      2. 장부가 준 슬롯 좌표(`--repo X` 단독으로 슬롯이 유도된 경우) — `SlotIdentity` 의
         `repo`/`number` 는 장부 행에서 온 **재접속 좌표**다(키 문자열을 뜯지 않는다).
      3. `--repo` 만 알면 그것만 넘긴다(board 가 같은 규칙으로 다시 유도한다).
      4. 아무것도 없으면 빈 목록 — board 의 기존 해소 체인(env·단일 lease)이 그대로 돈다.
    """
    repo, number = getattr(identity, "repo", None), getattr(identity, "slot", None)
    if not (repo and number):
        ledger_repo = getattr(slot_identity, "repo", None)
        ledger_number = getattr(slot_identity, "number", None)
        if ledger_repo and ledger_number:
            repo, number = ledger_repo, ledger_number
    if repo and number:
        return ["--repo", str(repo), "--slot", str(number)]
    if repo:
        return ["--repo", str(repo)]
    return []


def _probe_os_name() -> str:
    """Read the interpreter OS family through one injectable seam.

    Tests that need `_default_python()` to take the Windows branch on any host
    cannot rebind the global `os.name` directly — `pathlib` consults that same
    global to pick its flavour, and rebinding it mid-test breaks every path
    operation in the process. Routing the read through this function keeps the
    injection point inside this module instead of on the `os` global (this
    tool holds its own copy of the seam — see `_default_python` for why).
    """
    return os.name


def _default_python() -> str:
    """플랫폼-인지 venv 인터프리터 경로 (없으면 sys.executable 폴백).

    Windows 는 venv/Scripts/python.exe, POSIX 는 venv/bin/python. **venv 후보가 존재하면 그대로
    우선**한다 — 이 도그푸딩 머신은 시스템 python3 에 pytest 가 없고 venv 에만 있어, 회귀 측정
    인터프리터를 보존하려면 venv-first 가 불변이어야 한다(프레임워크 자기 회귀 0·우선순위 불변).

    venv 가 **없는** 건 에러가 아니라 정상 채택자 경로다 — 시스템 인터프리터에 pytest 가 깔린
    형상에선 venv/ 를 안 만든다. 그때는 `sys.executable`(현재 인터프리터)로 폴백해 그 환경의
    pytest 를 쓴다(폴백 분기·additive). 즉 존재 시 venv 우선, 부재 시 sys.executable 두 갈래다.
    """
    cand = REPO / "venv" / ("Scripts/python.exe" if _probe_os_name() == "nt" else "bin/python")
    return str(cand) if cand.exists() else sys.executable


def _load_local_conf():
    """공용 local.conf 로더(`local_conf.py`)를 같은 tools/ 에서 경로 로드한다 (board 사본 동형)."""
    return _load_module_from_path(
        Path(__file__).resolve().parent / "local_conf.py", "local_conf.py",
        verifier=_verify_engine_rev, cache=True,
        cache_key=f"_project_manager_local_conf:{Path(__file__).resolve().parent}",
    )


def local_config() -> dict[str, str]:
    """per-clone local.conf 를 KEY=value 로 읽는다 (없으면 빈 dict). board.py 와 동일 포맷."""
    return _load_local_conf().load_checked_readable(LOCAL_CONF)


# ── 회귀 명령 해소 (per-repo) ──────────────────────────────────
#
# 활성 repo 가 비-Python(Go 등)이거나 `tests/` 자체가 없을 수 있어 `pytest tests/ -q` 가
# 틀린다 — 회귀는 그 repo 의 `test_cmd`(areas.md 레지스트리 prefix 행/repo 행 > local.conf)를
# 써야 한다. 해소 체인의 단일 사본은 pm_handoff `_resolve_gate_cmd` 이고 이 도구는 자기 board
# 로드 seam 만 얹어 위임한다(아래 `_resolve_per_repo_test_cmd`).
#
# **프레임워크 자기 회귀(=현행 `pytest tests/ -q` venv 실행)는 반드시 보존**한다:
# 어느 층도 값을 주지 못하면 None 을 돌려, 호출부가 현행 argv 를 그대로 쓰게 한다(board 의
# 기본 폴백 `pytest -q` 와 달리 venv 인터프리터·`tests/` 경로를 보존 — 도그푸딩 불변).

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
# main() 진입에서 fail-loud 한다 — 기록의 어떤 단계(회귀/log append/complete/stage)도 착지 전에
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
    except Exception as exc:  # noqa: BLE001 — 판정 실패는 통과(오탐 0·standalone 무영향).
        if _is_engine_rev_skew(exc):
            raise  # 형제 사본 skew 는 앵커 판정 생략으로 접지 않는다(fail-loud · 재동기 안내).
        return None


def _guard_worktree_misanchor() -> bool:
    """쓰기-경로 진입 가드 — 오실행이면 fail-loud 후 True(차단), 아니면 False(통과)."""
    pm_home = _pm_home_misanchor()
    if pm_home is None:
        return False
    print(
        "[중단] `ticket_finish` 를 worktree(코드 전용) 트리에서 실행했습니다 — 완료 기록(log·"
        "board·git)는 PM 홈이 소유합니다. 이대로면 이 worktree 에 stray log/티켓을 "
        "잘못 만듭니다.\n"
        f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
        f"  (현재 앵커: {REPO})",
        file=sys.stderr,
    )
    return True


def _resolve_per_repo_test_cmd() -> str | None:
    """활성 회귀 게이트 명령(문자열)을 해소한다. 해소 실패면 None(기본 폴백).

    해소 체인 — 앞선 층이 비어 있지 않은 값을 주면 거기서 멈춘다:

      1. areas.md 활성 **prefix** 행의 `test_cmd`   (multi-repo 네임스페이스 형상)
      2. areas.md 활성 **repo** 행의 `test_cmd`     (prefix 칼럼이 빈 무prefix 형상)
      3. `local.conf` 의 `test.cmd`                 (per-clone 명시 설정)
      4. None → 호출부가 병렬 기본 `pytest tests/ -q -n auto` venv argv

    **체인 자체는 pm_handoff `_resolve_gate_cmd` 가 소유한다** — 사본을 두지 않고 동적 로드해
    위임한다(`_regression_cwd` 위임과 같은 방향·DRY). 해소 함수가 이 도구에만 있고 pm_handoff
    엔 없던 미러 이탈이 무prefix 채택자 결함의 절반이었다: 사본 둘을 만들면 다음 갱신에서
    다시 갈린다. board 모듈은 **이 도구의 seam**(`_load_board_module`)이 준다 — areas/
    local.conf 해소를 hermetic 하게 가로채는 기존 테스트 seam 이 그대로 살아 있다.

    pm_handoff 부재/로드 실패는 None(기본 폴백·fail-soft·현행 보존)이고, 형제 사본 skew 는
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
    except Exception as exc:  # noqa: BLE001 — fail-soft: 위임 실패는 기본 폴백.
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        if getattr(exc, "_legacy_conf_key", False):
            raise  # 구표기 conf 잔존도 같은 규칙 — pm_handoff 동형.
        return None


# ── 게이트 종류 (pytest 스위트인가 임의 명령인가) ────────────────────────
#
# 해소된 test_cmd 가 pytest 스위트라는 보장은 없다 — `go test ./...`·`viewer bind` 같은 임의
# 명령이다. 그런데 green 판정을 pytest 요약행(`N passed`)에만 걸어 두면 비-pytest 게이트는
# **항상 red** 로 오판돼 완료 기록이 통째로 막힌다(→ `--no-pytest` 상시 우회 → 진짜 red 를
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

# 해소 실패 시 실제로 실행하는 기본 argv 의 표시용 라벨 (dry-run 안내 문구).
_DEFAULT_GATE_LABEL = "pytest tests/ -q -n auto"


def _gate_is_pytest(gate_cmd: str | None) -> bool:
    """해소된 회귀 명령이 pytest 스위트면 True.

    `None`(해소 실패) = 프레임워크 자기 회귀 기본값 = venv pytest argv 이므로 True.
    """
    return gate_cmd is None or _PYTEST_GATE_TOKEN in gate_cmd


def _gate_label(gate_cmd: str | None) -> str:
    """회귀 게이트를 사람이 읽는 한 줄로 (안내 출력용)."""
    return gate_cmd if gate_cmd else _DEFAULT_GATE_LABEL


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


def get_ticket_claimed_rev(board_py: Path, ticket_id: str) -> str | None:
    """ticket_id 의 frontmatter `claimed_rev` (없으면 None) — 자기 축 회귀 baseline 입력.

    `board.py claim` 이 claim 시점 코드 트리 HEAD 를 박제한 값이다. 구 티켓(박제 이전)엔
    없으므로 None 이 정상 형상이고, 그때 그 축을 접는 판정은 소비자가 소유한다."""
    claimed_rev = _ticket_frontmatter(board_py, ticket_id).get("claimed_rev")
    return claimed_rev.strip() or None if isinstance(claimed_rev, str) else None


def get_ticket_claimed_at(board_py: Path, ticket_id: str) -> str | None:
    """ticket_id 의 frontmatter `claimed_at` (없으면 None) — diff 귀속 **창**의 시작점.

    `board.py claim` 이 박제하는 UTC ISO 시각이다. 같은 wave 에서 이 시각 이후 완료된 티켓은
    `done/` 으로 옮겨졌어도 아직 같은 트리에 자기 diff 를 남기고 있으므로 귀속 창 안이다.
    구 티켓(박제 이전)엔 없으므로 None 이 정상 형상이고, 그때 창을 못 정하는 판정은 소비자가
    소유한다(종전 `claimed/` 만 보는 폭으로 접는다)."""
    claimed_at = _ticket_frontmatter(board_py, ticket_id).get("claimed_at")
    return claimed_at.strip() or None if isinstance(claimed_at, str) else None


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
    """귀속 창 안 티켓 touches 읽기 결과 — 정상 빈 목록과 읽기 실패를 분리한다."""
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


def _frontmatter_head(text: str) -> str:
    """티켓 원문에서 frontmatter 블록만 잘라낸다 (본문 제외 · YAML 파싱 없음)."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", len("---"))
    return text if end == -1 else text[:end]


def _done_paths_in_window(board, since: str | None) -> list[Path]:
    """`done/` 중 `completed_at >= since` 인 티켓 경로 (창 밖·판정 불능은 제외).

    판독은 frontmatter **머리**의 스칼라 한 개뿐이다 — `done/` 은 수백 건이라 완료 기록마다
    전문 YAML 파싱을 돌리지 않는다. 부수 효과로 창 **밖** 구 티켓의 손상 frontmatter 가 귀속
    보정을 깨뜨리지 않는다(파싱하지 않으므로).
    시각 비교는 board 의 writer-형식 파서(`_parse_utc_iso`)를 그대로 부른다(판정 사본 0).
    그 seam 이 없는 구 사본이거나 `since` 가 형식이 아니면 창을 정할 수 없으므로 `done/` 을 보지
    않는다 — 종전 폭(과다 측정) 으로 접는 쪽이 안전하다.
    """
    parse_utc_iso = getattr(board, "_parse_utc_iso", None)
    if since is None or parse_utc_iso is None:
        return []
    window_start = parse_utc_iso(since)
    if window_start is None:
        return []
    done_dir = board.tickets_dir() / "done"
    if not done_dir.is_dir():
        return []
    read_text_shared = _load_file_lock().read_text_shared
    in_window: list[Path] = []
    for path in sorted(done_dir.glob("T-*.md")):
        try:
            head = _frontmatter_head(
                read_text_shared(path, encoding="utf-8", errors="replace")
            )
        except OSError:
            continue  # 읽기 실패는 창 밖으로 접는다(제외 근거를 못 쓰니 과다 측정 유지).
        completed_at = parse_utc_iso(
            _fallback_frontmatter_scalar(head, "completed_at")
        )
        if completed_at is not None and completed_at >= window_start:
            in_window.append(path)
    return in_window


def get_in_window_ticket_touches(
    board_py: Path, *, since: str | None = None,
) -> ClaimedTouchesSnapshot:
    """귀속 창 안 티켓별 touches 와 읽기 실패 진단.

    창 = `claimed/` ∪ (`done/` 중 `completed_at >= since`)이고 `since` 는 현재 티켓의
    `claimed_at` 이다. `claimed/` 만 보면 같은 wave 에서 **먼저 완료된** 티켓이 `done/` 으로
    옮겨져 보이지 않고, 그 전용 diff 가 뒤 티켓 몫으로 전부 흡수된다(실측: v1.7.8 wave 6장 —
    마지막 티켓이 측정할 때 나머지 넷은 이미 done). `since` 가 없으면 창을 정할 수 없으므로
    `claimed/` 만 보는 종전 동작으로 접는다.

    diff 귀속 입력은 이 스냅샷뿐이다. 새 장부나 설정을 만들지 않고, 보드가 권위 있게 정한
    `tickets_dir()/{claimed,done}` 과 각 티켓 frontmatter `touches` 만 읽는다. 디렉터리와
    frontmatter status 가 모두 같은 행만 인정한다. 정상적인 빈 touches/빈 창은 `error=None`이고,
    모듈·디렉터리·티켓 읽기/파싱 실패는 error 에 담아 호출부가 loud 하게 보정 skip 을 알린다.
    """
    try:
        board = _load_module_from_path(
            Path(board_py), Path(board_py).name, verifier=_verify_engine_rev,
        )
        claimed_dir = board.tickets_dir() / "claimed"
        if not claimed_dir.is_dir():
            raise FileNotFoundError(f"claimed 티켓 디렉터리 부재: {claimed_dir}")
        window_paths = [("claimed", path)
                        for path in sorted(claimed_dir.glob("T-*.md"))]
        window_paths += [("done", path)
                         for path in _done_paths_in_window(board, since)]
    except Exception as exc:  # noqa: BLE001 — 보드 미로드면 타 티켓 제외 없이 과다 측정 쪽 유지.
        if _is_engine_rev_skew(exc):
            raise
        detail = " ".join(str(exc).split()) or type(exc).__name__
        return ClaimedTouchesSnapshot({}, f"{board_py}: {detail}")

    in_window: dict[str, list[str]] = {}
    errors: list[str] = []
    for status, path in window_paths:
        try:
            fm, _body = board.load_ticket(path)
        except Exception as exc:  # noqa: BLE001 — 호출부가 전체 보정을 보수적으로 skip 한다.
            detail = " ".join(str(exc).split()) or type(exc).__name__
            errors.append(f"{path}: {detail}")
            continue
        ticket_id = fm.get("id") if isinstance(fm, dict) else None
        if (not isinstance(ticket_id, str) or not ticket_id.strip()
                or fm.get("status") != status):
            errors.append(f"{path}: {status} frontmatter(id/status) 손상")
            continue
        in_window[ticket_id.strip()] = _clean_touches(fm.get("touches"))
    return ClaimedTouchesSnapshot(in_window, "; ".join(errors) or None)


def _fallback_frontmatter_scalar(text: str, key: str) -> str | None:
    """보수적 단일-line scalar 복구 — board 모듈 불능 폴백과 창 판정의 머리 판독이 함께 쓴다."""
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
    additional_reviewer 의 기존 raw frontmatter parser를 재사용하고 estimate는 알려진 diff-cap 키만
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
        # additional_reviewer의 원문 frontmatter parser는 손상된 opener를 이 타입으로
        # 알린다. 이 fallback은 측정 가드용 입력 복구라 그 경우에도 가드 off로
        # 접어 완료 기록을 막지 않는다. 다른 예외는 삼키지 않는다.
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
# domain.py 부재(신규 clone·구버전)·로드 실패 → None (호출부가 graceful skip).

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
    domain.py 부재·로드 실패 → None (호출부가 조용히 skip — 신규 clone 무영향).
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


def _load_ticket_rounds():
    """라운드 사이드카 경로·임시 파일 규약의 단일 진실(`ticket_rounds.py`)을 경로 로드한다.

    부재는 None(fail-soft) — round stage 후보만 생략하고 티켓 파일 stage 는 그대로 진행한다
    (`_load_repo_coordinates` 동형). 엔진 사본 skew 는 흡수하지 않는다(fail-loud) — 호출부인
    `engine_written_paths` 는 이 함수를 자신의 넓은 try 밖에서 불러, skew 가 조용히 삼켜지지
    않고 그대로 표면화되게 한다.
    """
    path = TOOLS_DIR / "ticket_rounds.py"
    if not path.exists():
        return None
    try:
        mod = _load_module_from_path(
            path, "ticket_rounds.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 round 후보만 생략한다.
        if _is_engine_rev_skew(exc):
            raise
        return None
    return mod


def _round_stage_candidates(
        rounds_module, tickets_dir: Path, ticket_id: str) -> list[Path]:
    """티켓 라운드 디렉터리에 **실존하는 라운드 파일만** stage 후보로 낸다 (디렉터리 자체는
    넣지 않는다 — untracked 신규 라운드도 실 파일 목록이라야 `git_scope_stage_pathspec` 이
    add 한다).

    라운드 판정은 문법 단일 소유자 `parse_round_filename` 한 곳에 위임한다 — 접미/접두를
    이 함수가 다시 조합하면(`.md` 접미 + 점-접두 제외 등) 라운드가 아닌 `.md`(예: `notes.md`·
    `01-dev.md`)까지 후보에 실린다(reviewer 실측 — 9개 이름 배치 중 진짜 라운드 2건만
    통과해야 하는데 4건이 통과). 점-접두 임시 잔여(원자 교체 중간 산출)도 이 문법을 벗어나
    자연히 걸러지므로 별도 조건이 필요 없다.
    """
    directory = rounds_module.rounds_dir_for_ticket(ticket_id, tickets_dir)
    if not directory.is_dir():
        return []
    return sorted(
        entry for entry in directory.iterdir()
        if entry.is_file()
        and rounds_module.parse_round_filename(entry.name) is not None
    )


def engine_written_paths(board, ticket_id: str, log_file: Path) -> list[Path]:
    """**이 실행이 실제로 쓴** 산출물 경로.

      - `log/current.md` — 선작성 entry가 있으면 append하지 않더라도 2단계의 stage 스코프에
        항상 포함한다.
      - 티켓 파일의 옛/새 경로 · 라운드 사이드카(`tickets/rounds/<id>/*.md`) — 3단계
        `board.py complete` 가 티켓을 `claimed/`→`done/` 로 옮기고, 라운드는
        `ticket_rounds.rounds_dir_for_ticket` 가 정하는 고정 위치라 상태 이동을 따라가지
        않지만 그 실행이 새로 쓴 파일이라 함께 stage 대상이다. **board-git 분리 형상에선
        티켓과 함께 라운드도 제외** 한다: 티켓이 서브모듈(`.project_manager/board/`) 안이라
        상위 repo 의 `git add` 가 `fatal: … is in submodule`(rc=128)로 죽고, 그 이동은
        board-git 이 자기 커밋으로 이미 기록한다(라운드도 같은 서브모듈 트리 안).
        legacy(board 미분리·**출하 템플릿 기본 형상**)에선 그 이동이 홈 git 에 떨어지므로
        반드시 실어야 한다 — 안 그러면 채택자가 매 finish 마다 손으로 `git add` 해야 한다
        (reviewer 실측).

    옛 경로는 상태 디렉토리를 몰라도 된다 — 후보(모든 STATUS_DIRS/같은 파일명)를 넣어두면
    실존/추적되지 않는 후보는 `git_scope_stageable` 이 거른다(추적 중인 *삭제* 경로만 남아
    이동이 커밋으로 완성된다). 라운드는 디렉터리 통째가 아니라 존재하는 파일만 넣는다(위
    `_round_stage_candidates`). board 미로드면 log 만.
    """
    paths: list[Path] = [Path(log_file)]
    if board is None:
        return paths
    try:
        board_git_separated = board._board_git_enabled()
    except Exception:  # noqa: BLE001 — 판정 실패도 board 미로드와 같은 취급(log 만).
        return paths
    if board_git_separated:
        return paths
    # 분리 형상이면 여기 도달하지 않는다 — round 형제 로드도 그때만 한다(불필요한 로드 skip).
    rounds = _load_ticket_rounds()
    try:
        _status, ticket_path = board.find_ticket(ticket_id)
        paths.append(ticket_path)
        paths.extend(board.tickets_dir() / status / ticket_path.name
                     for status in board.STATUS_DIRS)
        if rounds is not None:
            paths.extend(
                _round_stage_candidates(rounds, board.tickets_dir(), ticket_id))
    except Exception:  # noqa: BLE001 — 티켓 조회 실패(부재·손상)는 log 만으로 진행(잔여 보고가 알린다).
        pass
    return paths


class StageScope(NamedTuple):
    """stage 스코프 산출 결과 — `pathspec` + `error`(산출 불능 사유·None 이면 정상).

    빈 `pathspec` 을 두 상태로 갈라 쓴다: **선언이 비었다**(error=None·정상)와 **판정기가
    죽었다**(error=사유). 후자를 조용히 no-op 으로 흘리면 stage 0 인데 아무 말이 없어
    "기록 끝" 으로 보인다(reviewer 실측 — board 모듈 로드 실패 형상).
    """
    pathspec: tuple[str, ...]
    error: str | None


class RepoStagePlan(NamedTuple):
    """한 git repo 에서 실행할 좁은 stage 계획 (두-git task-mode 용)."""
    label: str
    cwd: Path
    scope: StageScope


class DiffAttribution(NamedTuple):
    """티켓 귀속 보정 뒤 diff 총량과 실제 제외 근거.

    `unattributed_total` 은 **디렉터리 양보**로 어느 티켓 몫에도 싣지 않은 양이다 — 창 안 타
    티켓과 겹친 디렉터리 선언분이라 티켓별 분리 증거가 없다. 조용히 사라지면 측정 축의
    no-green-by-disabling 위반이므로 차단/통과 안내가 이 수치를 함께 보고한다.
    """
    total: int
    excluded_total: int
    excluded_ticket_ids: tuple[str, ...]
    unattributed_total: int = 0


class DiffPathStat(NamedTuple):
    """numstat 한 행의 논리 경로들(rename이면 source·destination)과 추가+삭제량."""
    paths: tuple[str, ...]
    amount: int


# ── 완료 대상 작업 트리 자기 축 회귀 판정 helper (순수 함수) ────────────────
_SELF_AXIS_FAILED_NODE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

# 전-트리 ratchet 가드 — 이 목록의 파일은 대상 집합에 항상 들어간다(파일별 매핑이 아니라
# "완료마다 항상 도는 가드" 정책 목록이다). 실 wave 사고에서 신규 red 를 잡아낸 실측으로
# 고정된 4개다. 새 항목을 빠뜨리면 tests/test_ticket_finish.py 의 목록 커버리지 회귀가 red 로
# 잡는다 — 추가 규율을 산문에 적지 않는다.
_SELF_AXIS_RATCHET_GUARD_FILES = (
    "tests/test_pm_review_delta.py",
    "tests/test_private_context_guard.py",
    "tests/test_public_reference_lint.py",
    "tests/test_upgrade_adopter_e2e.py",
)


def _self_axis_target_paths(path_list_stdout: str) -> set[str]:
    """git 경로 목록 출력(개행 구분)에서 `tests/` 바로 아래 flat `*.py` 만 남긴다.

    서브디렉터리(`tests/fixtures/…`)는 pytest 수집 대상이 아니라 제외한다. `git diff
    --name-only HEAD`·`git ls-files -o --exclude-standard` 양쪽 출력에 공용으로 쓴다.
    """
    files: set[str] = set()
    for line in path_list_stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("tests/") or not candidate.endswith(".py"):
            continue
        if "/" in candidate[len("tests/"):]:
            continue
        files.add(candidate)
    return files


def _self_axis_failed_node_ids(pytest_output: str) -> set[str]:
    """pytest `-q` 출력의 `FAILED`/`ERROR` 요약 줄에서 실패한 **테스트 노드 ID** 전체(파일이
    아니라 함수 단위) — base·dev 양쪽에 실패가 있는 파일에서도 신규분만 비교하려면 함수
    단위가 필요하다(파일 단위 비교는 그 신규분을 지운다).

    노드 ID 의 파일 경로 부분은 POSIX 표기로 고정한다. 자식 pytest 는 rootdir 해소에 따라
    같은 실패를 `os.sep` 표기로 낸다(Windows 실측: 상위 `pytest.ini` 를 rootdir 로 잡으면
    `tests\\test_a.py::test_x`). 표기가 갈리면 작업 트리와 baseline 이 같은 실패를 서로 다른
    노드 ID 로 보고 없는 신규 실패를 만들어 내고, 진단문도 엔진의 다른 경로 출력(`git diff
    --name-only` 산출·`touches`)과 표기가 어긋난다. 정규화는 이 한 곳뿐이다 — 비교도 진단문도
    전부 이 반환값에서 나온다. `::` 뒤(파라미터 ID)는 경로가 아니라 pytest 가 만든 표기라
    그대로 둔다."""
    node_ids: set[str] = set()
    for match in _SELF_AXIS_FAILED_NODE_RE.finditer(pytest_output):
        path, separator, parameters = match.group(1).partition("::")
        node_ids.add(path.replace("\\", "/") + separator + parameters)
    return node_ids


def stage_scope(ticket_id: str, board_py: Path, log_file: Path,
                run_git: Callable[[list[str]], tuple[int, str]], *,
                repo: Path | None = None, include_touches: bool = True,
                include_engine_outputs: bool = True,
                touches_workspace: Path | None = None,
                touches: Sequence[str] | None = None) -> StageScope:
    """이 완료 기록이 stage 할 pathspec (REPO 상대·실제 `add` 가능한 **파일**).

    선언원 = 티켓 `touches` ∪ `engine_written_paths()`. **판정은 board.py 의 repo-중립
    프리미티브 `git_scope_stage_pathspec` 한 벌을 재사용** 한다 — board-git 이 쓰는 바로 그
    함수다(스코프 산출·디렉토리 전개·서브모듈/미존재/미추적 제거). 복제하면 다음 사람이 한쪽만
    고친다.

    그 필터가 load-bearing 이다: 두-git 형상에서 `touches` 는 코드 worktree 경로(PM 홈엔 없음)
    를, board 분리 형상에선 서브모듈 내부 경로를 가리킬 수 있는데, 그대로 pathspec 에 넣으면
    `git add` 가 **rc=128 fatal** 로 죽어 아무것도 stage 되지 않는다.

    `touches` 를 주면 board 조회 대신 그 목록을 선언원으로 쓴다 — 두-repo 계획이 티켓 touches 를
    repo 별 몫으로 갈라 각 계획에 자기 몫만 넘기는 데 쓴다(주지 않으면 종전대로 board 조회).

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
        declared_touches = (list(touches) if touches is not None
                            else get_ticket_touches(board_py, ticket_id))
        if touches_workspace is not None and any(
                _has_worktree_touch_prefix(path) for path in declared_touches):
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
                declared_touches = coords.normalize_repo_paths(
                    declared_touches,
                    pm_root=REPO,
                    workspace=touches_workspace,
                )
            except getattr(coords, "RepoCoordinateError", RuntimeError) as exc:
                return StageScope((), f"touches 좌표 정규화 실패 ({exc})")
        declared.extend(repo / touch for touch in declared_touches)
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


def _path_exists_in(root: Path, relative: str) -> bool:
    """`root` 안에 이 선언 경로가 실재하는가 (판정 불능·경로 마법은 False).

    touches 는 사람이 적는 값이라 glob/pathspec 마법이 섞일 수 있다. 그런 선언은 이 트리에 있다고
    단정하지 않고 False 로 접는다 — 판정 불능을 존재로 세면 몫이 틀린 계획에 실린다. 절대경로
    선언은 pathlib 규칙상 `root` 가 무시돼 두 트리 검사가 같은 답을 낸다 — 실재하면 호출부의
    우선순위대로 코드 몫이 되고, 실재하지 않으면 양쪽 계획에 들어간다.
    """
    candidate = relative.strip()
    if not candidate or candidate.startswith(":(") or any(ch in candidate for ch in "*?["):
        return False
    try:
        return (root / candidate).exists()
    except OSError:
        return False


def scope_covers(pathspec: Sequence[str], path: str) -> bool:
    """`path` 가 이번 stage 선언 스코프에 덮이는가 (정확 일치 또는 선언 디렉토리 아래)."""
    return any(path == rel or path.startswith(rel.rstrip("/") + "/") for rel in pathspec)


def _dirty_entry_path(line: str) -> str:
    """`split_dirty` 출력 한 줄(`"XY path"`)에서 경로만 뽑는다 — 코드 2자 + 공백 고정폭."""
    return line[3:]


# 잔여 목록에서 **커밋된** 인구를 dirty 줄과 같은 고정폭으로 싣는 표시 — git 의 XY 코드 자리를
# 쓰되 git 이 내지 않는 값이라 두 인구가 한 목록 안에서 구별된다.
_COMMITTED_ENTRY_CODE = "CM"

_LOCAL_RUNTIME_ROOT = ".project_manager/.local"


def _is_local_runtime_path(path: str) -> bool:
    """clone-local 런타임 상태(`.project_manager/.local/`)인가 — 잔여 인구에서 뺀다.

    정상 adopter 에서는 gitignore 지만, 최소·격리 repo 에서도 lock·장부·task 상태가 ticket
    산출물 누락으로 오보되지 않게 접두 클래스 전체를 뺀다. 비슷한 이름의 sibling
    (`.project_manager/.locality/`)은 빼지 않는다. dirty 인구와 커밋 인구가 이 한 규칙을
    공유한다 — 사본을 두면 한쪽만 고쳐진다.
    """
    return path == _LOCAL_RUNTIME_ROOT or path.startswith(_LOCAL_RUNTIME_ROOT + "/")


# ── 사설 참조 완료 기록 preflight 보조 (측정 폭·판정식은 다른 소유자를 그대로 부른다) ──
# 판정식 자체(`prose_token_spans`·`_actionable_matches`)는 private_refs.py 가, 측정 폭
# 원문(0-컨텍스트 unified diff)은 additional_reviewer.measured_diff_text 가 소유한다. 여기 두
# 함수는 그 둘 사이 **형식 변환**만 한다 — 새 판정 로직이 아니다.

_DIFF_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# blame porcelain 첫 필드 — sha1(40)·sha256(64) 두 오브젝트 형식을 다 받는다.
_BLAME_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
# 차단 목록에서 raw 축 유입에 붙는 사유 — actionable(안전 삭제 단위가 잡힌 참조)과 같은 줄
# 형식을 쓰되, 그 줄이 왜 차단감인지를 값으로 남긴다. 도입 커밋이 아예 없는 줄과, 커밋은
# 됐지만 아직 통합 브랜치에 없는 줄은 둘 다 이 작업이 새로 들인 유입이고 사람이 볼 처방이
# 다르다(전자는 그냥 고치면 되고 후자는 어느 커밋을 고쳐야 하는지가 필요하다).
_PRIVATE_REF_UNCOMMITTED_NOTE = " (미커밋 신규)"
_PRIVATE_REF_UNMERGED_NOTE = " (통합 미반영 {sha})"


def _board_module_at(board_py: Path):
    """board 모듈 — `board_py` **자기 위치**의 트리를 본다 (로드 실패는 None).

    `load_board_module` 과 달리 REPO 를 재앵커하지 않는다(`_ticket_frontmatter_snapshot` 과
    같은 로드) — 장부·티켓·게이트 조회는 그 board 사본이 보는 트리를 그대로 봐야 하고,
    재앵커하면 조회가 이 도구의 트리로 끌려가 다른 board 를 읽는다.
    """
    try:
        return _load_module_from_path(
            Path(board_py), Path(board_py).name, verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — 로드 실패는 판정 불능(차단 아님).
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None


def ticket_cluster_ledger(board_py: Path, ticket_id: str) -> tuple[str | None, dict | None]:
    """이 티켓의 `(묶음 id, 묶음 장부 frontmatter)` — 조회 실패는 `(None, None)`.

    소유자는 board 다(`ticket_cluster` = 필드 부재면 크기 1 · `load_cluster` = 장부 읽기) —
    이 도구는 그 두 술어를 **한 번의 board 로드**로 부르고 값을 그대로 돌려준다. 두 소비자가
    같은 해소를 쓴다: 완료 기록 대상 해소(묶음 id)와 판정 기준 해소(장부 `base_branch`).

    장부 파일이 없으면 `(묶음 id, None)` 이다 — 그 티켓은 board 규칙상 크기 1 묶음이고
    (마이그레이션 0) 통합·묶음 브랜치 선언만 없는 상태라, 브랜치를 옮기는 단계만 무대상이다.
    """
    mod = _board_module_at(board_py)
    if mod is None:
        return None, None
    try:
        ticket_cluster_fn = getattr(mod, "ticket_cluster", None)
        load_cluster_fn = getattr(mod, "load_cluster", None)
        if ticket_cluster_fn is None or load_cluster_fn is None:
            return None, None  # 묶음 장부 이전 board 사본 — 묶음 개념이 없다.
        found = mod.find_ticket_exact(ticket_id)
        frontmatter: dict = {}
        if found is not None:
            loaded, _body = mod.load_ticket(found[1])
            frontmatter = loaded if isinstance(loaded, dict) else {}
        cluster = ticket_cluster_fn(ticket_id, frontmatter)
        return cluster, load_cluster_fn(cluster)
    except Exception as exc:  # noqa: BLE001 — 조회 실패는 판정 불능(차단 아님).
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None, None


def _cluster_integration_branch(board_py: Path, ticket_id: str) -> str | None:
    """이 티켓이 속한 묶음의 **통합 브랜치** 이름 (선언 부재·조회 실패는 None).

    값의 소유자는 board 의 묶음 장부(`base_branch`)다 — 이 도구는 그 선언을 읽기만 한다.
    값은 장부를 만들 때 박히므로(발행이 만드는 크기 1 장부 포함) 선언 부재는 판정 입력이
    없다는 사실이고, 이 판독을 쓰는 두 소비자(완료 기록 판정·리뷰 호출 폭)는 그 사실에
    **멈춘다** — 다른 기준으로 접는 갈래가 없다.
    """
    _cluster, ledger = ticket_cluster_ledger(board_py, ticket_id)
    declared = (ledger or {}).get("base_branch")
    if not isinstance(declared, str):
        return None
    return declared.strip() or None


def _cluster_member_ids(board_py: Path, ticket_id: str) -> tuple[str, ...]:
    """이 티켓이 속한 묶음의 멤버 티켓 ID (장부 부재·조회 실패는 빈 튜플).

    장부 해소는 `ticket_cluster_ledger` 한 벌이고 목록을 읽는 술어는 board 소유
    (`cluster_tickets`)다 — 이 도구는 그 둘을 잇기만 한다(멤버 규칙 사본 0).
    """
    _cluster, ledger = ticket_cluster_ledger(board_py, ticket_id)
    if not ledger:
        return ()
    mod = _board_module_at(board_py)
    tickets_fn = getattr(mod, "cluster_tickets", None) if mod is not None else None
    if tickets_fn is None:
        return ()
    try:
        members = tickets_fn(ledger)
    except Exception as exc:  # noqa: BLE001 — 목록 조회 실패는 제외 없이(인구 유지) 접는다.
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return ()
    return tuple(str(member).strip() for member in members if str(member).strip())


def _parsed_diff_added_lines(
    diff_text: str, block_path_fn: Callable[[list[str]], str | None],
) -> tuple[dict[str, set[int]], list[str]]:
    """0-컨텍스트 unified diff 원문 → ({새 파일 상대경로: {추가된 줄 번호}}, 경로 미확정 헤더).

    경로 복원은 `additional_reviewer._diff_block_path` 가 소유한다(사본 0) — 비-ASCII 경로의
    C-quote 8진 표기·rename 목적지·`/dev/null` 을 그 함수가 이미 손실 없이 푼다. 손으로 접두만
    벗기면 quote 된 표기가 그대로 키가 되어 실제 출하 경로와 결합되지 않고, 그 파일의 유입이
    통째로 판정에서 빠진다. 여기서는 block 을 나누고 hunk 헤더의 새-파일 줄 번호만 센다.

    산문 판정 자체는 여기서 하지 않는다 — `prose_token_spans` 는 완전한 파이썬 소스가
    필요해 `+`로 시작하는 diff 조각을 그대로 파싱할 수 없다. 여기서는 **어느 줄이 새로
    추가됐는지**만 뽑고, 판정은 호출부가 작업트리 파일 전문에 건다."""
    blocks: list[list[str]] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    added: dict[str, set[int]] = {}
    unresolved: list[str] = []
    for block in blocks:
        path = block_path_fn(block)
        if path is None:
            unresolved.append(block[0].rstrip("\r\n"))
            continue
        current_line: int | None = None
        for line in block[1:]:
            if line.startswith("@@"):
                match = _DIFF_HUNK_HEADER_RE.match(line)
                current_line = int(match.group(1)) if match else None
                continue
            if current_line is None:
                continue  # hunk 이전 메타데이터(`--- `·`+++ `·mode·index)는 줄이 아니다.
            if line.startswith("\\"):
                continue  # "\ No newline at end of file" — 줄이 아니다.
            if line.startswith("+"):
                added.setdefault(path, set()).add(current_line)
                current_line += 1
            elif not line.startswith("-"):
                current_line += 1  # 컨텍스트 줄(0-컨텍스트 diff 에선 드묾) — 줄 번호를 소비한다.
    return added, unresolved


def _git_blame_short_sha(
    run_git_stdout_at_fn: Callable[[Path, list[str]], tuple[int, str]],
    code_tree: Path, path: str, line: int,
) -> str | None:
    """`path:line` 을 도입한 커밋의 축약 sha (미커밋 줄·조회 실패는 None).

    잔여로 남는 흡수분(claim 이후 다른 커밋이 offender 를 들여온 경우) 경고에만 쓴다 — 이
    조회가 실패해도 경고 자체는 그대로 나가고 sha 만 빠진다(진단 강화일 뿐 차단 축이 아니다).

    **작업트리를 보는 `git blame` 이어야 한다** — `git log -L` 은 커밋된 내용만 따라가므로
    아직 커밋되지 않은 줄에 HEAD 의 같은 줄 번호에 있던 **무관한 커밋** sha 를 붙인다(유입
    커밋을 식별하라는 이 경고의 목적과 반대다). blame 이 미커밋 줄에 내는 all-zero sha 는
    도입 커밋이 없다는 뜻이므로 sha 없이 경고한다."""
    try:
        rc, out = run_git_stdout_at_fn(
            code_tree,
            ["blame", "--porcelain", f"-L{line},{line}", "--", path],
        )
    except Exception:  # noqa: BLE001 — 진단 강화 실패가 경고를 막지 않는다.
        return None
    if rc != 0 or not out.strip():
        return None
    sha = out.split(maxsplit=1)[0]
    if _BLAME_SHA_RE.fullmatch(sha) is None or set(sha) == {"0"}:
        return None  # 미커밋(작업트리에만 있는) 줄 — 도입 커밋이 아직 없다.
    try:
        rc, short = run_git_stdout_at_fn(code_tree, ["rev-parse", "--short", sha])
    except Exception:  # noqa: BLE001 — 축약 실패도 경고를 막지 않는다.
        return None
    short = short.strip()
    return short if rc == 0 and short else None


class StatusObservationError(Exception):
    """워킹트리 상태 **관측 자체가 실패**했다 — `strict` 소비자만 받는다.

    비차단 소비자(stage 후 보고)는 종전대로 빈 목록으로 접는다 — 보고 한 줄이 완료를 막지
    않는다. 그 관측을 **선행 조건**으로 쓰는 소비자(종결의 재배치·머지·반납)는 실패를 clean 과
    구별해야 한다: 빈 목록으로 접으면 dirty 트리가 clean 으로 위장돼 그 위에서 mutation 이 돈다.
    """


def status_entries(run_git: Callable[[list[str]], tuple[int, str]],
                   board=None, *, strict: bool = False) -> tuple[tuple[str, str], ...]:
    """현재 워킹트리 상태 `((XY, 경로), …)` — 조회 실패·비-git 은 `()`(비차단).

    `strict` 면 조회 실패(예외·rc≠0)를 빈 목록으로 접지 않고 그대로 올린다
    (`StatusObservationError`) — "판정 불능" 과 "관측했더니 clean" 을 가르는 축이다. 접는
    자리는 이 seam 이 아니라 소비자다. 값 파싱은 두 갈래가 같다.

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
    except Exception:  # noqa: BLE001 — 보고는 완료 흐름을 막지 않는다(strict 는 그대로 올린다).
        if strict:
            raise
        return ()
    if rc != 0:
        if strict:
            raise StatusObservationError(f"git status 조회 실패 (rc={rc}): {out.strip()}")
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
      - 미스테이지 잔여 (`Y` 가 공백 아님·`??` 포함 ∧ 선언 스코프 밖) — 스코프가 못 덮은
        변경(under-stage).

    **스코프 필터는 두 방향 모두에 걸린다.** stage **후**에 부르면(기존 사후 보고 용법) 스코프
    경로는 이미 `Y == " "` 라 필터가 항등이라 무행동 변화다. stage **전**에 부르면(잔여
    preflight — `TicketFinisher._default_residual_block`) 선언 스코프 안의 미스테이지 변경은
    "곧 stage 될 것"이라 잔여가 아니다 — 필터가 없으면 stage 전 호출마다 스코프 안 변경까지
    거짓 잔여로 잡힌다.
    """
    staged_out: list[str] = []
    unstaged: list[str] = []
    for code, path in entries:
        if _is_local_runtime_path(path):
            continue
        if code[0] not in (" ", "?") and not scope_covers(pathspec, path):
            staged_out.append(f"{code} {path}")
        if code[1] != " " and not scope_covers(pathspec, path):
            unstaged.append(f"{code} {path}")
    return tuple(staged_out), tuple(unstaged)


# ── 로그 스켈레톤 ───────────────────────────────────────────────────────

# 회귀 baseline 은 *실측* new_total 1줄만 남긴다
# 합계는 status.md 에 박제하지 않으므로 delta 는 PM 이 서술로 채운다·history 단일 진실=log).
# task 태그 sentinel은 pm_log.py·pm_handoff.py의 동명 상수와 미러한다.
# 모듈 격리를 유지하려고 각 생산자가 상수를 소유한다.
_TASK_TAG_PREFIX = "task:"


LOG_SKELETON_TEMPLATE = """\
## [{date}] {entry_type} | [[{ticket_id}]] — {title}{task_tag}

- <!-- PM: 무엇을·왜 서술 -->
- 테스트: 회귀 {new_total} / {new_total} (실측 · 직전 대비 delta 는 PM 서술).
- board: done {board_before}→{board_after}.
"""

# 스켈레톤 헤딩 줄에서 그 티켓의 기록을 식별하는 표기 — 새 canonical wikilink와 이미
# 발행된 legacy raw-ID를 함께 읽어 재실행·선작성 모두 중복 append를 만들지 않는다.
_LOG_ENTRY_TICKET_MARKERS = ("| [[{ticket}]] — ", "| {ticket} — ")


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

# 판정 기준(통합 브랜치)을 해소하지 못한 실행은 멈춘다 — 측정 폭·사설 참조·잔여 인구가 모두
# 그 기준을 입력으로 받으므로, 다른 기준으로 접으면 세 판정이 각자 다른 것을 본다.
_INTEGRATION_TIP_UNDECLARED = (
    "{ticket} 묶음 장부가 통합 브랜치(base_branch)를 선언하지 않았다 — 완료 기록의 측정 폭·"
    "사설 참조 판정·잔여 인구가 그 기준을 입력으로 받는다. `board.py cluster show <묶음>` 으로 "
    "장부를 확인하고 `base_branch` 를 채운 뒤 다시 실행하라."
)
_INTEGRATION_TIP_UNRESOLVED = (
    "{ticket} 장부가 선언한 통합 브랜치 {branch} 가 이 코드 트리에 없다 ({tree}) — 판정 기준을 "
    "해소하지 못했다. 장부의 통합 브랜치 이름과 이 트리의 브랜치를 대조하라."
)
_INTEGRATION_ANCHOR_SEAM_ABSENT = (
    "additional_reviewer 사본에 integration_anchor 부재(구형/부분 설치) — 측정 폭의 기준점을 물을 "
    "수 없다. pm-update 로 .project_manager/tools/ 를 재동기하라."
)
_RESIDUAL_POPULATION_UNRESOLVED = (
    "{ticket} 통합 브랜치 tip 으로 커밋된 인구를 읽지 못했다 ({tip}) — 잔여 판정 인구가 성립"
    "하지 않는다. 이 트리에서 그 브랜치가 해소되는지 확인하라."
)
_RESIDUAL_ATTRIBUTION_SKIP_NOTE = (
    "잔여 판정 귀속 보정 skip — {ticket} 창 안 티켓 touches 를 읽지 못했다 ({error}). 같은 "
    "묶음 다른 멤버가 선언한 경로도 잔여로 잡힐 수 있다."
)


class TicketFinisher:
    """PM 기록 자동화 핵심 로직.

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
        private_ref_block_fn: Callable[[str], str | None] | None = None,
        dod_block_fn: Callable[[str], str | None] | None = None,
        residual_block_fn: Callable[[str], str | None] | None = None,
        self_axis_block_fn: Callable[[str], str | None] | None = None,
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
        # 자동해소한다(pm_handoff `_regression_cwd` 재사용·판정 불능은 REPO 폴백).
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
        # additional_reviewer 가 소유하고 여기서는 판정만 소비한다.
        self._diff_cap_block_fn = diff_cap_block_fn or self._default_diff_cap_block
        # 사설 참조 완료 기록 preflight seam — 차단 사유 문자열 또는 None(통과·경고·판정
        # 불가). diff 서킷브레이커 바로 뒤에 둔다 — 둘 다 같은 claim 앵커 측정 폭(touches ∩
        # 출하 표면)을 재는 diff-축 판정이고, 크기(diff_cap)보다 내용(사설 참조)이 먼저
        # 확정 차단감이면 그 뒤의 DoD·잔여 판정을 물을 이유가 없다. 판정식은 private_refs.py
        # 소유(사본 0) — 여기서는 측정 폭 재사용 + 호출만 한다.
        self._private_ref_block_fn = private_ref_block_fn or self._default_private_ref_block
        # DoD 기록 게이트 preflight seam — 차단 사유 문자열 또는 None(통과·판정 불가).
        # 규칙 소유자는 board(`_dod_open_items`)이고 여기서는 **더 앞에서 한 번 더** 물을 뿐이다.
        self._dod_block_fn = dod_block_fn or self._default_dod_block
        # 잔여(코드 트리 dirty ⊄ 선언 스코프) preflight seam — 차단 사유 문자열 또는 None(통과).
        # 판정 인구는 코드 트리뿐이다(PM 홈 dev-state 는 `_home_state_prefixes` 로 구조적 제외).
        self._residual_block_fn = residual_block_fn or self._default_residual_block
        # 완료 대상 작업 트리 자기 축 회귀 preflight seam — 차단 문자열 또는 None(통과·경고·
        # 판정불가). 판정 트리는 이 완료 대상 작업 트리 자체다(합성 병합 트리를 만들지 않는다).
        # 튜플의 **마지막** 원소로만 붙는다(비용순 — diff cap·DoD 는 서브프로세스 0~2회, 이건
        # base 가 red 일 때만 baseline 회귀를 한 번 더 돈다).
        self._self_axis_block_fn = self_axis_block_fn or self._default_self_axis_block
        # (스코프 밖 staged, 미스테이지 잔여) 건수 — `[완료]` 줄 재고지용(loud 강화).
        self._dirty_summary: tuple[int, int] = (0, 0)
        # 이 실행에서 이미 낸 loud 사유 키 — 같은 해소를 여러 판정 표면이 공유하므로 중복
        # 출력을 막되, 사유가 다르면 각각 낸다(`_warn_once`).
        self._loud_notes: set[str] = set()

    # ── 기본 subprocess 구현 (실제 실행) ─────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """회귀를 실행해 (returncode, stdout+stderr) 반환.

        명령 해소:
          - **해소 성공** — `_resolve_per_repo_test_cmd()`(areas prefix 행 > areas repo 행 >
            local.conf)가 준 문자열을 shell 로 실행(board.py 회귀와 동형·비-Python repo 수용).
          - **프레임워크 자기 회귀** — 해소 실패면 현행 그대로
            `[venv_python, -m, pytest, tests/, -q, -n, auto]` venv argv.

        cwd 는 런타임 해소— 명시 주입(`regression_cwd` 인자)이 있으면 그 경로,
        없으면 `_regression_cwd()` 가 self-host 단일 슬롯을 자동해소(홈 cwd 에서도 활성
        worktree 의 tests/ 에서 돌게). 판정 불능은 REPO 폴백(현행 보존·additive).
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
            # 프레임워크 자기 회귀 — xdist 가용 환경의 병렬 기본 argv.
            result = subprocess.run(
                [str(self._venv_python), "-m", "pytest", "tests/", "-q", "-n", "auto"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
            )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _log_has_entry(self, ticket_id: str) -> bool:
        """이 티켓의 완료 entry가 log 에 이미 있는가 (읽기 실패·파일 부재는 False).

        판정은 완료 헤딩 줄의 티켓 표기 하나다 — PM이 선작성했거나 스켈레톤의 서술 불릿을
        채워도 그 표기는 남으므로, 한 번 남은 기록을 재실행이 다시 쌓지 않는다.

        읽기 실패는 '없음'으로 접는다 — 이 관측은 차단 판정이 아니라 중복 방지이고, 못 읽었다고
        기록을 막으면 완료 기록이 벽돌이 된다(중복은 사람이 지우면 되고, 누락은 안 그렇다)."""
        try:
            text = _load_file_lock().read_text_shared(self._log_file, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — 읽기 실패는 중복 방지 판정 불능.
            if _is_engine_rev_skew(exc):
                raise
            return False
        markers = tuple(
            marker.format(ticket=ticket_id) for marker in _LOG_ENTRY_TICKET_MARKERS
        )
        return any(
            line.startswith("## ") and any(marker in line for marker in markers)
            for line in text.splitlines()
        )

    def _code_tree(self) -> Path:
        """코드가 있는 트리 — 회귀 cwd 해소와 **같은 규칙**(task 작업공간 > 명시 > 자동 슬롯).

        분리된 PM 홈엔 코드가 없으므로 diff 측정도 회귀와 같은 트리를 봐야 한다.

        **회귀 skip 여부와 독립**이다 — `--no-pytest` 는 [1/5] 회귀 실행만 건너뛴다. 이 트리는
        diff 서킷브레이커 측정 root·touches 좌표 정규화·PM-direct 재검이 함께 쓰므로, 회귀를
        안 돌린다고 해소를 건너뛰면 그 소비자들이 PM 홈(분리 형상에선 엔진 import 사본)을 잰다."""
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

    def _warn_once(self, key: str, message: str) -> None:
        """같은 사유를 한 실행에서 **한 번만** 낸다 (조용한 강등 금지 · 중복 출력 금지).

        판정 표면 여럿(측정 폭·사설 참조)이 같은 해소를 공유하므로, 해소 실패를 부를 때마다
        알리면 같은 줄이 표면 수만큼 반복된다."""
        if key in self._loud_notes:
            return
        self._loud_notes.add(key)
        print(f"  ⚠ {message}", file=sys.stderr)

    def _measurement_anchor(self, ticket_id: str, external) -> str:
        """측정 폭의 기준점 — 통합 브랜치와의 merge-base. 해소 실패는 정지다.

        두 게이트(diff 서킷브레이커·사설 참조)가 이 한 자리에서 같은 값을 받는다 — 리뷰가 본
        표면과 완료 기록이 보는 표면이 갈리지 않는다. 구형/부분 설치로 측정 seam 이 없으면
        폭을 물을 수 없으므로 그것도 정지다(옛 폭으로 접는 갈래가 없다)."""
        anchor_fn = getattr(external, "integration_anchor", None)
        if anchor_fn is None:
            raise _CloseObservationFailure(_INTEGRATION_ANCHOR_SEAM_ABSENT)
        code_tree = self._code_tree()
        anchor, reason = anchor_fn(
            code_tree, self._integration_tip(ticket_id, code_tree))
        if reason is not None:
            raise _CloseObservationFailure(f"{ticket_id} {reason}")
        return anchor

    def _integration_tip(self, ticket_id: str, code_tree: Path) -> str:
        """판정 기준으로 쓸 통합 브랜치 — 장부 선언 ∩ 이 코드 트리 실재.

        선언이 없거나 이 트리에서 해소되지 않으면 **멈춘다**. 이 값은 측정 폭·사설 참조 신규
        판정·잔여 인구 세 판정의 공통 기준이라, 없는 채로 진행하면 세 판정이 각자 다른 것을
        보고 갈린다(선언과 실재가 갈린 상태를 통과로 위장하지도 않는다)."""
        branch = _cluster_integration_branch(self._board_py, ticket_id)
        if not branch:
            raise _CloseObservationFailure(
                _INTEGRATION_TIP_UNDECLARED.format(ticket=ticket_id))
        rc, _out = self._run_git_stdout_at_fn(
            code_tree,
            ["rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"],
        )
        if rc != 0:
            raise _CloseObservationFailure(_INTEGRATION_TIP_UNRESOLVED.format(
                ticket=ticket_id, branch=branch, tree=code_tree))
        return branch

    @staticmethod
    def _warn_directory_yield(ticket_id: str, amount: int) -> None:
        """디렉터리 양보로 뺀 양의 단일 loud 표면 — 통과가 조용하지 않게 한다."""
        print(
            f"  ⚠ diff 서킷브레이커 디렉터리 양보 — {ticket_id} 창 안 타 티켓과 겹친 "
            f"디렉터리 선언 {amount}줄을 이 티켓 몫에서 뺐습니다(귀속 불명).",
            file=sys.stderr,
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
    def _touch_claims_path_exactly(cls, touch: str, changed_path: str) -> bool:
        """touches 선언이 그 파일을 **정확히 지목**하는가(디렉터리 포함이 아니라).

        디렉터리 양보 규칙의 판정 입력이다. 경로 magic/glob 은 `_touch_claims_path` 와 같은
        이유로 일치(True)로 취급한다 — 양보가 발동하지 않아 과다 측정이 유지되므로 오차 방향이
        가드 약화 쪽으로 가지 않는다.
        """
        canonical = cls._canonical_touch_path(touch)
        if canonical.startswith(":(") or any(char in canonical for char in "*?["):
            return True
        return canonical == cls._canonical_touch_path(changed_path)

    @classmethod
    def _touch_owns_path(cls, touch: str, changed_path: str) -> bool:
        """창 안 타 티켓 선언이 그 파일을 **증명 가능하게** 주장하는가(양보 판정 전용).

        `_touch_claims_path` 는 해소 불능한 magic/glob 을 일치(True)로 접는다 — 그쪽은 "내
        몫으로 유지" 판정이라 오차가 과다 측정 쪽이다. 같은 술어를 **양보**(내 몫에서 빼는
        판정)의 상대 owner 산출에 재사용하면 오차 방향이 뒤집힌다: 무관한 타 티켓 선언
        (`docs/*.md`) 하나만으로 내 디렉터리 변경 400줄 전량이 빠지고 상한이 통과된다(실측).

        그래서 양보 쪽은 반대로 접는다 — 이 귀속기가 Git 과 같게 해석할 수 없는 선언
        (pathspec magic·glob)은 owner 로 인정하지 않고 양보를 발동시키지 않는다. 실제로 그
        파일에 걸리는 glob 도 같은 방향이다: 자체 glob 해석을 owner 근거로 믿는 순간 Git 과의
        해석 차이가 그대로 가드 약화가 되므로, 증명할 수 있는 평문 경로만 owner 로 센다.
        """
        canonical = cls._canonical_touch_path(touch)
        if canonical.startswith(":(") or any(char in canonical for char in "*?["):
            return False
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
        claimed_rev: str | None = None,
    ) -> tuple[DiffPathStat, ...]:
        """additional_reviewer 측정 폭의 numstat 한 벌을 경로별 총량으로 접는다.

        폭(claim 앵커·staged+unstaged+untracked·폴백 단계)은 `measured_numstat_text` 가 소유하고
        `_sum_numstat` 은 binary/machine-mirror 제외를 소유한다. 이 함수는 그 결과 **한 번**을
        경로별로 나눌 뿐이며 claimed 티켓마다 git diff 를 다시 실행하지 않는다.
        """
        required = ("measured_numstat_text", "_sum_numstat", "_numstat_path")
        if not all(hasattr(external, name) for name in required):
            raise AttributeError("additional_reviewer numstat seam 부재")
        text = external.measured_numstat_text(
            root, "HEAD", list(paths), run_fn, claimed_rev=claimed_rev,
        )
        if not text.strip():
            return ()
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

    @classmethod
    def _claim_width_amount(
        cls, root: Path, stat: DiffPathStat, endpoint_claims: Sequence[bool], *,
        run_fn=None,
    ) -> int:
        """그 경로를 **현재 티켓 touches 단독으로** 쟀을 때의 폭.

        Git 은 rename 의 source 만 pathspec 에 주면 삭제 전체, destination 만 주면 추가 전체를
        세지만, 둘 다 주면 rename delta 만 센다. 창 합집합 측정은 항상 두 endpoint 를 넣으므로
        단독 claim 의 종전 폭을 여기서 복원한다(50→0 축소 방지).

        복원은 그 폭을 **어디에 싣든** 선행한다 — 내 몫(`total`)이든 디렉터리 양보
        (`unattributed_total`)든 같은 수치다. 양보가 이 복원을 건너뛰면 400줄 rename 이 귀속
        에도 보고에도 남지 않고 사라진다(실측). 유실은 과다 측정보다 나쁘다.
        """
        if len(stat.paths) != 2 or not any(endpoint_claims) or all(endpoint_claims):
            return stat.amount
        if endpoint_claims[0]:
            source_lines = cls._head_line_count(root, stat.paths[0], run_fn=run_fn)
            return stat.amount if source_lines is None else source_lines
        destination_lines = cls._worktree_line_count(root, stat.paths[1])
        return stat.amount if destination_lines is None else destination_lines

    def _ticket_diff_attribution(
        self, ticket_id: str, external, touches: Sequence[str], *, run_fn=None,
        board_error: str | None = None, claimed_rev: str | None = None,
    ) -> DiffAttribution:
        """창 안 합집합 numstat 한 벌에서 현재 티켓 몫과 타 티켓 전용 몫을 가른다.

        겹침 규칙: 변경 파일을 현재 티켓 touches 가 **파일 또는 상위 디렉터리 어떤 형태로든**
        포함하면 그 파일 전체를 현재 측정에 유지한다. 한 파일의 hunks 를 touches 만으로 티켓별
        분리할 증거가 없기 때문이다. 현재 티켓은 전혀 주장하지 않고 창 안 타 티켓만 주장하는
        파일만 제외한다. 모호함을 유지(과다 측정) 쪽으로 접는 것이 자기 산출을 숨겨 가드를
        약화하는 것보다 안전하다.

        예외가 **디렉터리 양보** 하나다: 현재 티켓이 그 파일을 디렉터리 선언으로만 주장하고
        (`tests` 같은 항목) 창 안 타 티켓이 어떤 형태로든 같은 파일을 주장하면 내 몫에서 뺀다.
        공유 트리 wave 에서 이 규칙이 없으면 디렉터리 선언 하나가 그 아래 전 wave 변경을 자기
        스코프로 흡수해 티켓별 측정이 4~10배로 부푼다(실측). 뺀 양은 어느 티켓에도 싣지 않고
        `unattributed_total` 로 보고한다 — 양쪽 정확-claim 이나 양쪽 디렉터리-claim 같은 나머지
        모호는 종전대로 과다 측정을 유지한다.

        양보의 상대 owner 는 **증명 가능한 선언만** 센다(`_touch_owns_path`) — 해소 불능한
        pathspec magic/glob 은 owner 가 아니라 양보 없음(과다 측정)이다. 그리고 양보로 빼는
        수치는 유지할 때와 같은 단독-claim 폭이다(`_claim_width_amount`) — rename endpoint 폭
        복원을 건너뛰면 전량이 귀속에도 보고에도 남지 않는다.
        """
        root = self._code_tree()
        snapshot = (ClaimedTouchesSnapshot({}, board_error) if board_error is not None
                    else get_in_window_ticket_touches(
                        self._board_py,
                        since=get_ticket_claimed_at(self._board_py, ticket_id),
                    ))
        in_window: dict[str, list[str]] = {}
        attribution_error = snapshot.error
        if attribution_error is None:
            for window_id, raw_touches in snapshot.tickets.items():
                normalized = self._normalize_measured_touches(raw_touches, warn=False)
                if raw_touches and normalized is None:
                    attribution_error = f"{window_id}: touches 좌표 정규화 실패"
                    in_window = {}
                    break
                in_window[window_id] = normalized or []
        if attribution_error is not None and board_error is None:
            self._warn_diff_attribution_failure(attribution_error)

        other_claims = {
            window_id: window_touches
            for window_id, window_touches in in_window.items()
            if window_id != ticket_id and window_touches
        }
        if attribution_error is not None or not other_claims:
            # 보정할 타 티켓이 없거나 보드가 불완전하면 기존 단일-ticket 측정식을 그대로 쓴다.
            # 특히 실패를 가드 off 로 접지 않고 현재 touches 전체의 상한 판정을 계속한다.
            measure_kwargs = {"run_fn": run_fn} if run_fn is not None else {}
            if claimed_rev:
                measure_kwargs["claimed_rev"] = claimed_rev
            total = external.diff_line_total(
                root, "HEAD", list(touches), **measure_kwargs,
            )
            return DiffAttribution(total, 0, ())

        measurement_paths = list(touches)
        measurement_paths.extend(
            touch for window_touches in in_window.values() for touch in window_touches
        )
        measurement_paths = list(dict.fromkeys(
            self._canonical_touch_path(path) for path in measurement_paths
        ))
        try:
            by_path = self._diff_numstat_by_path(
                external, root, measurement_paths, run_fn=run_fn,
                claimed_rev=claimed_rev,
            )
        except AttributeError:
            # 부분 설치/구형 additional_reviewer 에선 귀속 보정을 포기하되 종전 측정은 유지한다.
            total = external.diff_line_total(root, "HEAD", list(touches))
            return DiffAttribution(total, 0, ())

        total = 0
        excluded_total = 0
        unattributed_total = 0
        excluded_ids: set[str] = set()
        for stat in by_path:
            endpoint_claims = tuple(
                any(self._touch_claims_path(touch, path) for touch in touches)
                for path in stat.paths
            )
            exact_endpoint_claims = tuple(
                any(self._touch_claims_path_exactly(touch, path) for touch in touches)
                for path in stat.paths
            )
            owners = {
                window_id for window_id, window_touches in other_claims.items()
                if any(
                    self._touch_claims_path(touch, path)
                    for touch in window_touches for path in stat.paths
                )
            }
            # 양보 판정의 상대 owner 는 **증명 가능한** 선언만 센다(`_touch_owns_path`) —
            # 유지 판정용 `owners` 와 술어가 다르다. 같은 술어를 쓰면 해소 불능 magic 하나가
            # 무관한 티켓을 owner 로 만들어 내 몫을 지운다.
            yield_owners = {
                window_id for window_id, window_touches in other_claims.items()
                if any(
                    self._touch_owns_path(touch, path)
                    for touch in window_touches for path in stat.paths
                )
            }
            # rename 단독 claim 폭 복원은 몫을 어디에 싣든 선행한다(유지든 양보든 같은 수치).
            claim_amount = self._claim_width_amount(
                root, stat, endpoint_claims, run_fn=run_fn,
            )
            if any(endpoint_claims) and not any(exact_endpoint_claims) and yield_owners:
                # 디렉터리 양보 — 내 주장은 디렉터리 선언뿐인데 창 안 타 티켓도 같은 파일을
                # 주장한다. 내 몫으로 세지 않되 조용히 버리지도 않는다(불변식: 뺀 양 보고).
                unattributed_total += claim_amount
            elif any(endpoint_claims) or not owners:
                total += claim_amount  # 겹침/불명은 유지 — 과다 측정이 안전한 방향이다.
            elif stat.amount > 0:
                excluded_total += stat.amount
                excluded_ids.update(owners)
        return DiffAttribution(total, excluded_total, tuple(sorted(excluded_ids)),
                               unattributed_total)

    def _default_diff_cap_block(self, ticket_id: str) -> str | None:
        """diff 서킷브레이커 판정 — 차단 안내 문자열, 통과·가드 off 면 None.

        상한 표·측정식·문구는 additional_reviewer 소유분을 그대로 쓴다(기계 mirror 제외도 그
        측정 seam 이 소유한다 — 여기 사본 없음). 측정 폭의 기준점은 통합 브랜치와의
        merge-base 다(해소 못 하면 claim 시점 rev) — 사설 참조 preflight 와 **같은 폭**이라
        두 판정이 다른 것을 보고 갈리지 않는다. 측정 불가
        (모듈 부재·touches 부재·좌표 정규화 불능·estimate 미선언·비-git 트리)는 **가드 off** 다 —
        이 축의 실패로 완료 기록을 막지 않는다(hard 차단은 상한 초과라는 확정 사실에만 건다)."""
        external = _load_additional_reviewer()
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
        anchor = self._measurement_anchor(ticket_id, external)
        try:
            attribution = self._ticket_diff_attribution(
                ticket_id, external, touches, board_error=inputs.board_error,
                claimed_rev=anchor,
            )
        except OSError:
            return None
        block = external.diff_cap_block(
            attribution.total, cap, ticket=ticket_id, estimate=estimate, scope=touches,
        )
        if block is None:
            # 통과도 조용하지 않다 — 양보로 뺀 양이 있으면 그 사실을 여기서 말한다(차단이면
            # 아래 안내가 같은 수치를 실으므로 두 번 말하지 않는다).
            if attribution.unattributed_total:
                self._warn_directory_yield(ticket_id, attribution.unattributed_total)
            return None
        excluded_ids = ", ".join(attribution.excluded_ticket_ids) or "(없음)"
        lines = [block,
                 f"  타 claimed 티켓 귀속 제외: {attribution.excluded_total}줄"
                 f" · 티켓 {excluded_ids}"]
        if attribution.unattributed_total:
            lines.append(
                f"  디렉터리 양보 보류: {attribution.unattributed_total}줄"
                " (창 안 타 티켓과 겹친 디렉터리 선언 — 티켓별 분리 증거 없음)"
            )
        return "\n".join(lines)

    # ── 사설 참조 완료 기록 preflight ────────────────────────────────────────
    # 판정식·표면 술어는 private_refs.py 소유(사본 0). 측정 폭은 diff 서킷브레이커와 같은
    # 앵커 seam(`_measurement_anchor`)을 재사용한다 — 리뷰가 본 표면과 이 게이트가 보는
    # 표면이 갈리지 않는다. rc 정책의 축은 **그 줄이 통합 브랜치에 이미 있는가**다: 통합
    # 브랜치에 없는 줄(도입 커밋이 없거나, 커밋은 됐지만 그 커밋이 통합 브랜치의 조상이
    # 아니다)의 참조는 이 작업이 새로 들인 유입이라 넓은 recall 의 raw 축 전부를 차단하고,
    # 안전 삭제 단위가 잡히는 actionable 은 그와 무관하게 차단한다. 이미 통합 브랜치에
    # 도달한 줄의 raw 는 흡수분·허용분 재작성이 실측상 걸리는 갈래라 blame sha 를 실은
    # 경고로 남긴다.
    #
    # 이 기준은 **커밋·재배치 순서에 흔들리지 않는다** — 같은 변경이 작업트리에만 있든, 이미
    # 커밋됐든, 재배치 전이든 후든 판정이 같다. 순서를 운영 규칙으로 지키게 하던 자리를 판정
    # 기준 자체가 대체한다. 통합 브랜치를 해소하지 못하면(선언 부재·이 트리에 없음) 이 판정은
    # 다른 기준으로 접지 않고 멈춘다.

    def _private_ref_surface_paths(
        self, code_tree: Path, touches: Sequence[str], private_refs, external,
    ) -> list[Path]:
        """터치 선언 ∩ 사설 참조 출하 python 표면 ∖ 기계 mirror — 절대경로 목록(표면 해소).

        표면 정의는 `private_refs.shipping_paths` 가, 측정 제외는
        `additional_reviewer.is_machine_mirror_path` 가 소유한다(정의 사본 0 · markdown 절반은
        이 게이트 밖 — 비목표). 기계 mirror 를 빼는 이유는 `templates/<타깃>/` 아래 엔진
        사본이 canonical 을 그대로 복사한 결과이기 때문이다 — 빼지 않으면 같은 유입 한 건이
        canonical 1 + 사본 3 으로 네 번 지목되고, 사람이 고칠 자리가 어디인지 흐려진다."""
        python_paths, _markdown_paths = private_refs.shipping_paths(code_tree)
        surface: list[Path] = []
        for path in python_paths:
            relative = path.relative_to(code_tree).as_posix()
            if not scope_covers(touches, relative):
                continue
            if external.is_machine_mirror_path(relative):
                continue
            surface.append(path)
        return surface

    def _private_ref_diff_offenders(
        self, surface_paths: Sequence[Path], code_tree: Path, diff_text: str,
        private_refs, block_path_fn,
    ) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
        """표면 파일 중 `diff_text` 가 새로 추가한 줄에서 (actionable, raw) offender 를 낸다.

        `(경로, 줄, 토큰)` 3-튜플 목록 두 개 — raw ⊇ actionable(같은 프로세 스팬·같은 정규식을
        재사용한다). 판정은 diff 조각이 아니라 **작업트리 파일 전문**에 건다(`prose_token_spans`
        는 완전한 파이썬 소스가 필요하다) — diff 는 추가 줄 **번호**를 뽑는 데만 쓴다.

        판정하지 못한 표면(비-UTF-8 · 구문 오류 · 경로 미확정 diff block)은 차단하지 않되
        **조용히 넘기지도 않는다** — 확정 사실에만 hard 차단을 걸되, 못 본 자리는 값으로 알린다."""
        added_by_path, unresolved = _parsed_diff_added_lines(diff_text, block_path_fn)
        for header in unresolved:
            print(
                f"  ⚠ 사설 참조 판정 불가(차단 아님) — diff 경로 미확정: {header}",
                file=sys.stderr,
            )
        actionable: list[tuple[str, int, str]] = []
        raw: list[tuple[str, int, str]] = []
        for path in surface_paths:
            relative = path.relative_to(code_tree).as_posix()
            added_lines = added_by_path.get(relative)
            if not added_lines:
                continue
            try:
                source = _load_file_lock().read_text_shared(path, encoding="utf-8")
                spans = private_refs.prose_token_spans(source)
            # `TokenError` 는 3.12 미만 토크나이저가 미완성 문자열·괄호에 내는 형식이다
            # (3.12+ 는 같은 입력을 `SyntaxError` 로 낸다) — 지원 하한을 함께 덮는다.
            except (
                OSError, UnicodeError, SyntaxError, tokenize.TokenError,
                private_refs.DataLiteralMarkerError,
            ) as exc:
                print(
                    f"  ⚠ 사설 참조 판정 불가(차단 아님) — {relative}: "
                    f"{type(exc).__name__}",
                    file=sys.stderr,
                )
                continue
            for span in spans:
                # raw 축은 span 원문맥에 건다 — `_specific_matches` 의 인라인 코드 보호(backtick
                # 쌍)는 여러 줄에 걸칠 수 있어 잘린 줄만 보면 보호가 풀린다. 이 축의 match
                # offset 은 span 기준이라 그대로 줄 번호로 환산된다.
                for match in private_refs._specific_matches(span.text):
                    line = span.line + span.text[:match.start()].count("\n")
                    if line in added_lines:
                        raw.append((relative, line, match.group()))
                # actionable 축은 **물리 줄 단위**로 건다 — `_actionable_matches` 는 줄마다 안전
                # 삭제 계획을 세우고 그 **줄 기준** offset 의 match 를 돌려준다. span 전체 offset
                # 으로 읽으면 2행 이후에 들어온 유입이 앞줄로 붙어 추가 줄 집합과 어긋나고,
                # 차단돼야 할 유입이 경고로 강등된다(여러 줄 docstring 실측).
                offset = 0
                for chunk in span.text.splitlines(keepends=True):
                    line = span.line + span.text.count("\n", 0, offset)
                    if line in added_lines:
                        for match in private_refs._actionable_matches(chunk):
                            actionable.append((relative, line, match.group()))
                    offset += len(chunk)
        actionable.sort()
        raw.sort()
        return actionable, raw

    def _line_reached_integration(
        self, code_tree: Path, sha: str, integration_tip: str,
    ) -> bool:
        """그 줄의 도입 커밋이 통합 브랜치에 이미 있는가 — 판정 불능은 False(신규 취급).

        `git merge-base --is-ancestor` 는 조상이면 rc 0, 아니면 rc 1 이고 그 밖의 rc 는 판정
        자체가 안 된 것이다. 도달했다는 증거가 없는 줄을 흡수분으로 접으면 이 게이트가 막으려는
        신규 유입이 경고로 강등돼 나간다 — 반대 방향의 오분류는 사람이 그 자리를 한 번 보는
        비용에서 끝난다."""
        try:
            rc, _out = self._run_git_stdout_at_fn(
                code_tree, ["merge-base", "--is-ancestor", sha, integration_tip],
            )
        except Exception as exc:  # noqa: BLE001 — 판정 실패는 신규 취급(차단 방향).
            if _is_engine_rev_skew(exc):
                raise  # 사본 skew 는 판정으로 접지 않는다(fail-loud).
            return False
        return rc == 0

    def _private_ref_raw_by_novelty(
        self, code_tree: Path, raw_only: Sequence[tuple[str, int, str]],
        integration_tip: str,
    ) -> tuple[list[tuple[str, int, str, str]], list[tuple[str, int, str, str]]]:
        """raw-only offender 를 (신규 유입 + 사유 표기, 흡수분 + 그 줄의 blame sha)로 가른다.

        기준은 **그 줄이 통합 브랜치에 도달했는가** 다: 도입 커밋이 없는 줄(all-zero sha·조회
        불가)과 도입 커밋이 통합 브랜치의 조상이 아닌 줄은 이 작업이 새로 들인 유입이고, 조상인
        줄은 통합 브랜치가 이미 가진 흡수분·허용분이다. 재유입 가드의 대장(allowlist·ratchet)은
        여기서 읽지 않는다 — 채택자 트리에 그 파일이 없어 판정이 영구 강등되고, 대장 재생성이
        곧 게이트 통과가 된다.

        통합 브랜치는 항상 값이다(`_integration_tip` 이 해소하지 못하면 이 판정에 오지 않고
        멈춘다) — 도입 커밋 유무만 보는 기준으로 접는 갈래가 없다."""
        novel: list[tuple[str, int, str, str]] = []
        absorbed: list[tuple[str, int, str, str]] = []
        for path, line, token in raw_only:
            sha = _git_blame_short_sha(
                self._run_git_stdout_at_fn, code_tree, path, line,
            )
            if sha is None:
                novel.append((path, line, token, _PRIVATE_REF_UNCOMMITTED_NOTE))
            elif self._line_reached_integration(code_tree, sha, integration_tip):
                absorbed.append((path, line, token, sha))
            else:
                novel.append(
                    (path, line, token, _PRIVATE_REF_UNMERGED_NOTE.format(sha=sha)))
        return novel, absorbed

    def _default_private_ref_block(self, ticket_id: str) -> str | None:
        """사설 참조 preflight 판정 — 차단감이 있으면 차단 문자열, 아니면 None(경고는
        stderr 로 별도 · rc 는 안 바꾼다).

        차단감은 두 갈래의 합집합이다 — (a) actionable(안전 삭제 단위가 잡히는 참조)은 통합
        브랜치 도달 여부와 무관하게, (b) 통합 브랜치에 아직 없는 줄의 raw 참조는 이 작업이
        새로 들인 유입이라 함께 차단한다. 통합 브랜치가 이미 가진 줄의 raw 는 blame sha 를
        실어 알린다(차단감이 따로 있으면 차단 문구 안에, 없으면 stderr 경고로).

        측정 불가(모듈 부재·touches 부재·표면 교집합 없음)는 **가드 off** 다 — 이 축의 실패로
        완료 기록을 막지 않는다(diff_cap 과 같은 자세 — hard 차단은 확정 사실에만 건다). 판정
        기준(통합 브랜치)이 없는 것은 그 축에 들지 않는다 — 기준 없는 판정은 통과가 아니라
        정지다."""
        private_refs = _load_private_refs()
        if private_refs is None:
            return None
        external = _load_additional_reviewer()
        if external is None:
            return None
        measured_diff_text_fn = getattr(external, "measured_diff_text", None)
        block_path_fn = getattr(external, "_diff_block_path", None)
        if measured_diff_text_fn is None or block_path_fn is None:
            return None  # 구형/부분 additional_reviewer 사본 — 표면 자체가 없다.
        inputs = self._diff_ticket_inputs(ticket_id, external)
        touches = self._normalize_measured_touches(inputs.touches, warn=False)
        if not touches:
            return None
        code_tree = self._code_tree()
        surface_paths = self._private_ref_surface_paths(
            code_tree, touches, private_refs, external,
        )
        if not surface_paths:
            return None
        integration_tip = self._integration_tip(ticket_id, code_tree)
        anchor = self._measurement_anchor(ticket_id, external)
        relative_paths = [path.relative_to(code_tree).as_posix() for path in surface_paths]
        try:
            diff_text = measured_diff_text_fn(
                code_tree, "HEAD", relative_paths, claimed_rev=anchor,
            )
        except OSError:
            return None
        if not diff_text.strip():
            return None
        actionable, raw = self._private_ref_diff_offenders(
            surface_paths, code_tree, diff_text, private_refs, block_path_fn,
        )
        if not actionable and not raw:
            return None
        # actionable 은 raw 의 부분집합이다(같은 정규식·같은 스팬) — 같은 좌표를 두 번 세지
        # 않도록 raw 에서 빼고 남은 것만 커밋 여부로 가른다.
        actionable_keys = set(actionable)
        novel_raw, absorbed_raw = self._private_ref_raw_by_novelty(
            code_tree, [entry for entry in raw if entry not in actionable_keys],
            integration_tip,
        )
        if not actionable and not novel_raw:
            # 통합 브랜치가 이미 가진 줄의 raw 만 남음 — 경고만(흡수분·허용분 재작성 실측상
            # 이 갈래). blame sha 를 실어 1회 읽기로 유입 커밋을 식별할 수 있게 한다.
            for path, line, token, sha in absorbed_raw:
                print(
                    f"  ⚠ 사설 참조 raw 감지(경고만) — {ticket_id} {path}:{line} {token}"
                    f" (git blame {sha})",
                    file=sys.stderr,
                )
            return None
        offenders = [(entry, "") for entry in actionable]
        offenders += [
            ((path, line, token), note) for path, line, token, note in novel_raw
        ]
        offenders.sort()
        lines = [
            f"사설 참조 유입 — {ticket_id} 이번 폭에서 추가한 줄에 채택자가 조회할 수 없는 "
            f"참조가 있다 ({len(offenders)}건):",
        ]
        lines += [
            f"  ✗ {path}:{line} {token}{note}" for (path, line, token), note in offenders
        ]
        if absorbed_raw:
            lines.append(
                f"  같은 폭 흡수분 raw 목록({len(absorbed_raw)}건 · 한 라운드로 정리):"
            )
            lines += [
                f"    · {path}:{line} {token} (git blame {sha})"
                for path, line, token, sha in absorbed_raw
            ]
        return "\n".join(lines)

    def _default_dod_block(self, ticket_id: str) -> str | None:
        """DoD 기록 게이트 preflight 판정 — 차단 사유 문자열, 통과·판정 불가면 None.

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

    def _home_state_prefixes(self, tree: Path) -> tuple[str, ...]:
        """`tree` 안에 PM 홈 dev-state(board 루트·wiki 루트)가 있으면 그 트리-상대 접두를 낸다.

        새 경로를 여기서 하드코딩하지 않는다 — `board.board_root()`·`pm_log.WIKI_DIR` 은 엔진이
        이미 소유한 PM 홈 값이다. 분리 형상에서는 두 루트가 PM 홈(`REPO`)에 있고 `tree` 는 다른
        코드 worktree라 결과가 빈 튜플이다(제외 대상 자체가 없다 — 규칙이 약해지지 않는다).
        임베디드 형상에서는 `tree == REPO` 라 둘 다 그 안에 있어, 다른 PM 세션의 wiki WIP 로 내
        완료가 막히지 않는다. 해소 실패는 빈 튜플(fail-soft·제외 없음 = 판정 인구에 남겨 더
        엄격한 쪽으로 접는다)."""
        try:
            tree_resolved = tree.resolve()
        except OSError:
            return ()
        candidates: list[Path] = []
        board = load_board_module(self._board_py)
        board_root_fn = getattr(board, "board_root", None) if board else None
        if board_root_fn is not None:
            try:
                candidates.append(Path(board_root_fn()))
            except Exception as exc:  # noqa: BLE001 — 인구 계산 실패는 제외 없이(포함 유지) 접는다.
                if _is_engine_rev_skew(exc):
                    raise
        try:
            pm_log = _load_pm_log()
            candidates.append(Path(pm_log.WIKI_DIR))
        except Exception as exc:  # noqa: BLE001
            if _is_engine_rev_skew(exc):
                raise
        prefixes: list[str] = []
        for candidate in candidates:
            try:
                relative = candidate.resolve().relative_to(tree_resolved)
            except (OSError, ValueError):
                continue
            prefixes.append(relative.as_posix())
        return tuple(prefixes)

    def _default_residual_block(self, ticket_id: str) -> str | None:
        """코드 트리 변경 중 누구도 선언하지 않은 것이 있으면 차단 사유 문자열, 통과면 None.

        판정 인구는 `merge-base(<통합 tip>, HEAD)..작업 트리` 다(`_residual_split`) — 커밋
        여부가 판정을 바꾸지 않는다. 인구를 세는 트리는 **코드 트리**(`plan.cwd ==
        self._code_tree()`)뿐이다 — 분리 형상에서 PM 홈 산출물 계획(cwd=`REPO`)은 다른 실행·
        다른 PM 세션의 wiki WIP 가 상주하는 공유 표면이라 구조적으로 제외된다. 임베디드
        형상(코드 트리 자신이 PM 홈)에서는 `_home_state_prefixes` 가 board 루트·wiki 루트만
        같은 방식으로 제외한다.

        방향은 묻지 않는다 — 미스테이지 잔여·스코프 밖 staged·통합 미반영 커밋 어느 쪽이든
        비어 있지 않으면 차단이다(`git add` 나 `git commit` 한 번이 우회로가 되면 안 된다).
        `scope_error`(스코프 산출 실패)도
        차단이다 — 이 실행이 코드 트리에서 아무것도 stage 하지 못한다는 확정 사실이라, 여기서
        '판정 불가'로 접으면 전량 미머지가 조용히 통과한다. 이때 스코프는 **빈 스코프로
        읽는다**(아무 경로도 못 덮으므로) — 코드 트리 dirty 전량이 잔여가 되고, PM 홈
        제외 규칙은 정상 갈래와 동일하게 적용해 잔여 목록·표준 처방을 원인 문장에 동봉한다."""
        code_tree = self._code_tree()
        home_prefixes = self._home_state_prefixes(code_tree)
        for plan in self._stage_plans(ticket_id):
            if plan.cwd != code_tree:
                continue
            scope, scope_error = plan.scope
            if scope_error:
                cause = (
                    f"완료 기록 거부 — {ticket_id} stage 스코프를 산출하지 못했다 "
                    f"({scope_error}). 이 실행은 코드 트리에서 아무것도 stage 하지 못하므로 "
                    "변경 전량이 미머지로 남는다 — 먼저 원인(board 사본·PyYAML 등)을 해소하라."
                )
                staged_out, unstaged, committed = self._residual_split(
                    plan.cwd, (), home_prefixes, ticket_id=ticket_id)
                if not staged_out and not unstaged and not committed:
                    return cause
                return cause + "\n" + self._residual_lines(
                    staged_out, unstaged, committed)
            staged_out, unstaged, committed = self._residual_split(
                plan.cwd, scope, home_prefixes, ticket_id=ticket_id)
            if not staged_out and not unstaged and not committed:
                continue
            return self._residual_block_message(
                ticket_id, staged_out, unstaged, committed)
        return None

    def _residual_split(
        self, cwd: Path, scope: Sequence[str], home_prefixes: Sequence[str], *,
        ticket_id: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """잔여 판정 인구를 (스코프 밖 staged, 미스테이지 잔여, 통합 미반영 커밋)로 가른다.

        인구는 `merge-base(<통합 tip>, HEAD)..작업 트리` = 커밋분 ∪ dirty 다. 산출을 개발자
        라운드가 커밋하는 모델에서 dirty 만 세면 **같은 선언 밖 변경이 커밋 전에는 차단, 커밋
        뒤에는 통과**가 된다 — 조작 순서가 판정의 입력으로 남는다. 세 갈래를 같은 인구에서 보고
        같은 선언으로 대조한다.

        인구에서 빼는 것은 둘이다 — PM 홈 dev-state 접두(다른 세션의 wiki WIP)와 **같은 묶음
        다른 멤버가 선언한 경로**(`_sibling_member_scopes`). 묶음은 브랜치 하나를 공유하므로
        멤버 B 의 커밋이 멤버 A 의 완료 기록을 막으면 크기 N 묶음은 종결 자체가 불가능하다.
        어느 멤버도 선언하지 않은 변경만 차단감이다.

        정상·`scope_error` 두 갈래가 이 함수를 공유한다(후자는 빈 스코프로 부른다).
        """
        staged_out, unstaged = self._dirty_split(scope, cwd=cwd)
        excluded = tuple(home_prefixes) + self._sibling_member_scopes(ticket_id)
        if excluded:
            staged_out = tuple(line for line in staged_out
                               if not scope_covers(excluded, _dirty_entry_path(line)))
            unstaged = tuple(line for line in unstaged
                             if not scope_covers(excluded, _dirty_entry_path(line)))
        dirty_paths = {_dirty_entry_path(line) for line in staged_out + unstaged}
        committed = tuple(
            f"{_COMMITTED_ENTRY_CODE} {path}"
            for path in self._committed_out_of_scope(cwd, ticket_id, scope, excluded)
            if path not in dirty_paths      # 같은 경로를 두 갈래로 두 번 세지 않는다
        )
        return staged_out, unstaged, committed

    def _sibling_member_scopes(self, ticket_id: str) -> tuple[str, ...]:
        """같은 묶음 **다른 멤버**가 선언한 경로 — 잔여 판정 인구에서 뺀다.

        귀속 입력은 diff 서킷브레이커가 이미 쓰는 창 스냅샷 하나다
        (`get_in_window_ticket_touches` · 사본 0) — 멤버 목록은 묶음 장부가, 경로는 그 티켓
        frontmatter `touches` 가 소유한다. 창 밖 멤버(아직 claim 하지 않은 티켓)는 제외 근거가
        없어 인구에 남는다(과다 차단 쪽 — 선언 누락을 숨기지 않는다).

        경로 비교는 `scope_covers` 다(pathspec magic 은 덮지 않는다) — 해소 불능 선언 하나가
        임의의 커밋을 면제하면 이 게이트가 통째로 무력해진다.

        스냅샷 읽기가 실패하면 제외 없이 종전 인구를 쓰고 그 사실을 한 줄로 알린다.
        """
        members = [member for member in _cluster_member_ids(self._board_py, ticket_id)
                   if member != ticket_id]
        if not members:
            return ()
        snapshot = get_in_window_ticket_touches(
            self._board_py, since=get_ticket_claimed_at(self._board_py, ticket_id),
        )
        if snapshot.error is not None:
            self._warn_once(
                f"residual-attribution:{ticket_id}",
                _RESIDUAL_ATTRIBUTION_SKIP_NOTE.format(
                    ticket=ticket_id, error=snapshot.error),
            )
            return ()
        scopes: list[str] = []
        for member in members:
            normalized = self._normalize_measured_touches(
                snapshot.tickets.get(member, []), warn=False)
            scopes.extend(normalized or [])
        return tuple(dict.fromkeys(scopes))

    def _committed_out_of_scope(
        self, cwd: Path, ticket_id: str, scope: Sequence[str],
        excluded: Sequence[str],
    ) -> tuple[str, ...]:
        """`merge-base(<통합 tip>, HEAD)..HEAD` 중 누구도 선언하지 않은 경로.

        커밋된 인구의 대조 기준은 stage pathspec 이 아니라 **선언**(`touches` ∪ 그 pathspec)
        이다: 선언한 파일을 지우고 커밋하면 그 경로는 stage 가능 목록에서 빠지지만
        (`git_scope_stageable` — 미존재·미추적 제거) 선언은 그대로다. pathspec 만 보면 선언한
        삭제가 잔여로 둔갑한다.

        통합 tip 은 판정 인구의 기준이라 해소하지 못하면 멈춘다(`_integration_tip`) — dirty
        인구로 접으면 커밋된 선언 밖 변경이 보이지 않는 채로 통과한다.
        """
        tip = self._integration_tip(ticket_id, cwd)
        rc, out = self._run_git_stdout_at_fn(
            cwd, ["diff", "--name-only", "-z", f"{tip}...HEAD"])
        if rc != 0:
            raise _CloseObservationFailure(_RESIDUAL_POPULATION_UNRESOLVED.format(
                ticket=ticket_id, tip=tip))
        declared = tuple(scope) + tuple(
            self._normalize_measured_touches(
                get_ticket_touches(self._board_py, ticket_id), warn=False) or ())
        return tuple(
            path for path in out.split("\0")
            if path and not _is_local_runtime_path(path)
            and not scope_covers(declared, path)
            and not scope_covers(excluded, path)
        )

    def _residual_block_message(self, ticket_id: str, staged_out: Sequence[str],
                                unstaged: Sequence[str],
                                committed: Sequence[str] = ()) -> str:
        """잔여 차단 안내 — 원인 헤더 + 잔여 목록·처방 본문(`_residual_lines`)."""
        detail = (f"미스테이지 {len(unstaged)}건 · 스코프 밖 staged {len(staged_out)}건")
        if committed:
            detail += f" · 통합 미반영 커밋 {len(committed)}건"
        header = (
            f"완료 기록 거부 — {ticket_id} 코드 트리에 선언 스코프 밖 변경이 남아 있다 "
            f"({detail})."
        )
        return header + "\n" + self._residual_lines(staged_out, unstaged, committed)

    def _residual_lines(self, staged_out: Sequence[str], unstaged: Sequence[str],
                        committed: Sequence[str] = ()) -> str:
        """잔여 목록(기존 20줄 접기 규약 재사용) + 처방 2가지 — 헤더 없는 본문만.

        `_residual_block_message`(정상 갈래)와 `_default_residual_block` 의 `scope_error`
        갈래(원인 문장이 이미 별도 헤더)가 이 본문을 공유한다."""
        lines: list[str] = []
        if unstaged:
            lines.append("  미스테이지 잔여 (이 커밋에 안 실린다):")
            lines += self._fold_residual_lines(unstaged)
        if staged_out:
            lines.append("  스코프 밖 staged (내 변경이 아닌데 커밋에 실린다):")
            lines += self._fold_residual_lines(staged_out)
        if committed:
            lines.append("  통합 미반영 커밋 (커밋됐지만 어느 티켓도 선언하지 않았다):")
            lines += self._fold_residual_lines(committed)
        lines.append(
            "  처방: (1) 내 작업 누락이면 ticket `touches` 를 보강해 재실행하고, (2) 남의 "
            "변경이면 그 변경을 소유한 티켓의 `touches` 에 선언하라 — 커밋은 회피 수단이 "
            "아니다(커밋된 변경도 같은 인구로 판정한다)."
        )
        return "\n".join(lines)

    def _fold_residual_lines(self, lines: Sequence[str]) -> list[str]:
        """차단 안내용 잔여 목록 — `_RESIDUAL_DIRTY_PREVIEW_LINES` 접기 규약을 그대로 쓴다."""
        folded = [f"    {line}" for line in lines[:self._RESIDUAL_DIRTY_PREVIEW_LINES]]
        hidden = len(lines) - self._RESIDUAL_DIRTY_PREVIEW_LINES
        if hidden > 0:
            folded.append(f"    … 외 {hidden}건")
        return folded

    # ── 완료 대상 작업 트리 자기 축 회귀 판정 ────────────────────────────
    # 판정 트리는 완료 대상 작업 트리 자체다. 대상 집합은 이 트리의 변경분(추적+미추적)과
    # 전-트리 ratchet 가드 목록의 합집합이고, 그 대상을 작업 트리와 claim 시점 baseline
    # 양쪽에서 돌려 함수 단위 노드 ID 로 신규 실패만 골라낸다. 존재-판정("돌았다") 이 아니라
    # 값-대조("base 대비 늘었다")로 차단한다.

    def _self_axis_target_files(self, code_tree: Path) -> set[str]:
        """대상 tests/*.py 집합 = tracked(staged+unstaged) 변경 ∪ untracked 신규 ∪ ratchet 목록.

        두 git 명령 모두 순수 읽기다 — index 를 갱신하지 않는다(`git status` 는 stat 캐시를
        건드릴 수 있어 안 쓴다)."""
        _rc_t, tracked_out = self._run_git_stdout_at_fn(
            code_tree, ["diff", "--name-only", "HEAD"])
        _rc_u, untracked_out = self._run_git_stdout_at_fn(
            code_tree, ["ls-files", "-o", "--exclude-standard"])
        changed = _self_axis_target_paths(tracked_out) | _self_axis_target_paths(untracked_out)
        return changed | set(_SELF_AXIS_RATCHET_GUARD_FILES)

    def _run_pytest_subset(self, cwd: Path, files: Sequence[str]) -> tuple[int, str]:
        """대상 파일만 골라 `cwd` 에서 pytest 를 돌린다(작업 트리·baseline 공용).

        `cwd` 에 실재하지 않는 파일은 건너뛴다(삭제분이 집합에 남아도 수집 에러로 안 죽는다) —
        하나도 안 남으면 무비용 통과 (0, "")."""
        existing = sorted(f for f in files if (cwd / f).is_file())
        if not existing:
            return 0, ""
        result = subprocess.run(
            [str(self._venv_python), "-m", "pytest", *existing, "-q"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(cwd),
        )
        return result.returncode, result.stdout + result.stderr

    def _materialize_tree(self, code_tree: Path, ref: str) -> Path:
        """claim 시점 `ref`(commit-ish) 를 scratch 디렉터리에 추출한 baseline 저장소를 만든다.

        `git archive | tar -x` 로 파일만 옮기고 `git init` 을 더한다 — 환경 의존 테스트(git
        저장소 존재를 요구하는 가드 등)가 "git 저장소 부재"로 오탐하지 않게 한다. 곧바로
        `.git/objects/info/alternates` 에 원 저장소 object DB 경로를 적어 원 이력 객체를
        read-only 로 해소한다 — 실 SHA 를 좌표로 쓰는 테스트가 고립된 scratch 저장소에서
        "유효한 객체 이름이 아닙니다" 로 오탐하지 않는다(민감도 — 이 한 줄을 빼면 그 오탐이
        재현된다). `code_tree` 의 ref·index·worktree 등록은 만들지 않는다 — 원 저장소는
        읽기만 당한다(alternates 는 OID 로 객체를 해소할 뿐 이름으로 ref 를 해소하지 않는다).
        """
        scratch = Path(tempfile.mkdtemp(
            prefix="ticket-finish-baseline-", dir=_engine_temp_root(REPO)))
        try:
            archive = subprocess.run(
                ["git", "archive", ref], cwd=str(code_tree), capture_output=True)
            if archive.returncode != 0:
                raise RuntimeError(
                    f"git archive {ref} 실패: "
                    f"{archive.stderr.decode('utf-8', 'replace')}")
            tar = subprocess.run(
                ["tar", "-x"], cwd=str(scratch), input=archive.stdout,
                capture_output=True)
            if tar.returncode != 0:
                raise RuntimeError(f"tar -x 실패: {tar.stderr.decode('utf-8', 'replace')}")
            init = subprocess.run(
                ["git", "init", "-q"], cwd=str(scratch), capture_output=True, text=True,
                encoding="utf-8", errors="replace")
            if init.returncode != 0:
                raise RuntimeError(f"git init 실패: {init.stderr}")

            common_rc, common_out = self._run_git_stdout_at_fn(
                code_tree, ["rev-parse", "--git-common-dir"])
            if common_rc != 0 or not common_out.strip():
                raise RuntimeError("원 저장소 object DB 경로(--git-common-dir) 해소 실패")
            common_dir = Path(common_out.strip().splitlines()[0].strip())
            if not common_dir.is_absolute():
                common_dir = (code_tree / common_dir).resolve()
            alternates_path = scratch / ".git" / "objects" / "info" / "alternates"
            alternates_path.parent.mkdir(parents=True, exist_ok=True)
            alternates_path.write_text(
                f"{common_dir / 'objects'}\n", encoding="utf-8", newline="\n")

            for args in (
                ["add", "-A"],
                ["-c", "user.email=ticket-finish-baseline@local",
                 "-c", "user.name=ticket-finish-baseline",
                 "commit", "-q", "-m", "baseline (materialized — 실 이력 아님)"],
            ):
                result = subprocess.run(
                    ["git", *args], cwd=str(scratch), capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
                if result.returncode != 0:
                    raise RuntimeError(f"git {' '.join(args)} 실패: {result.stderr}")
        except Exception:
            # 정리 실패로 원 예외를 덮지 않는다 — force_rmtree 는 못 지우면 OSError 를 올리는데,
            # 그걸 그대로 두면 여기서 진행 중인 원 예외(git archive/init/commit 실패)를 가린다.
            # 형제 규약(pm_import._force_rmtree 호출부)과 같은 형태: loud 경고 뒤 원 예외 재전파.
            try:
                _load_file_lock().force_rmtree(scratch)
            except OSError as cleanup_exc:
                print(
                    f"경고: baseline scratch 정리 실패 — 직접 지우세요: {scratch} "
                    f"({type(cleanup_exc).__name__}: {cleanup_exc})",
                    file=sys.stderr,
                )
            raise
        return scratch

    def _default_self_axis_block(self, ticket_id: str) -> str | None:
        """완료 대상 작업 트리에서 base 대비 신규 red 를 판정한다 — 차단 문자열, 통과·판정불가면
        None.

        claim 앵커(board `claimed_rev`)가 없으면(채택자 형상·구 티켓) 비교 기준이 없으므로
        완전히 무발화다 — 작업 트리 회귀조차 돌리지 않는다. 앵커가 있어도 작업 트리가 green
        이면 baseline 은 돌리지 않는다(비용은 red 일 때만 낸다). red 면 baseline 을 claim
        시점으로 materialize 해 같은 대상 집합을 돌리고, 함수 단위 노드 ID 차집합(작업 트리 −
        baseline)이 있으면 차단, 없으면(전부 상속 red) loud 경고만 하고 통과시킨다.
        """
        code_tree = self._code_tree()
        target_files = self._self_axis_target_files(code_tree)
        if not target_files:
            return None

        claimed_rev = get_ticket_claimed_rev(self._board_py, ticket_id)
        if not claimed_rev:
            return None

        work_rc, work_out = self._run_pytest_subset(code_tree, sorted(target_files))
        if work_rc == 0:
            return None  # green — baseline 은 말할 것이 없으므로 돌리지 않는다.
        work_failed = _self_axis_failed_node_ids(work_out)

        try:
            baseline_tree = self._materialize_tree(code_tree, claimed_rev)
        except (OSError, RuntimeError) as exc:
            if _is_engine_rev_skew(exc):
                raise  # 사본 불일치는 판정 skip 한 줄로 접지 않는다(다른 형제 로더와 같은 규칙).
            print(
                f"  ⚠ 자기 축 회귀 판정 skip — {ticket_id}: baseline 판정 실패({exc})",
                file=sys.stderr,
            )
            return None
        try:
            base_rc, base_out = self._run_pytest_subset(baseline_tree, sorted(target_files))
        finally:
            # `finally` 안이라 정리 실패를 raise 하면 try 블록의 예외(있다면)를 덮는다 — 형제
            # 규약(pm_import._force_rmtree 호출부)과 같은 형태: loud 경고만 남기고 삼키지 않는다.
            try:
                _load_file_lock().force_rmtree(baseline_tree)
            except OSError as cleanup_exc:
                print(
                    f"경고: baseline 트리 정리 실패 — 직접 지우세요: {baseline_tree} "
                    f"({type(cleanup_exc).__name__}: {cleanup_exc})",
                    file=sys.stderr,
                )
        base_failed = _self_axis_failed_node_ids(base_out) if base_rc != 0 else set()

        new_failed = sorted(work_failed - base_failed)
        if new_failed:
            return self._self_axis_block(ticket_id, new_failed, work_out)

        inherited = sorted(work_failed & base_failed)
        if inherited:
            print(
                f"  ⚠ 자기 축 회귀 경고 — {ticket_id}: base 에서 이미 red 인 실패뿐이다"
                f"({', '.join(inherited)}) — 차단하지 않는다.",
                file=sys.stderr,
            )
        return None

    @staticmethod
    def _self_axis_block(ticket_id: str, new_failed: list[str], output: str) -> str:
        """자기 축 신규 red 차단 문자열 — `run()` 이 `[중단]` 접두로 그대로 낸다."""
        lines = [f"자기 축 회귀 신규 실패 — {ticket_id}"]
        lines += [f"  ✗ {node}" for node in new_failed]
        tail = "\n".join(output.rstrip().splitlines()[-20:])
        if tail:
            lines.append("  · 회귀 출력(꼬리 20줄):")
            lines.append(tail)
        return "\n".join(lines)

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

    def _code_stage_scope(self, ticket_id: str, code_tree: Path,
                          touches: Sequence[str] | None = None) -> StageScope:
        """코드 트리에는 ticket touches만 계획한다 (홈 산출물 유입 금지).

        트리를 인자로 받는다 — 코드 트리는 task 작업공간일 수도, 해소된 슬롯 worktree 일 수도
        있고 둘은 stage 규칙이 같다(`_stage_plans` 가 어느 트리인지 판정한다). `touches` 는
        이 트리 몫으로 갈린 부분집합이다(미지정이면 티켓 touches 전체).
        """
        def run_git(args: list[str]) -> tuple[int, str]:
            return self._run_git_stdout_at_fn(code_tree, args)

        return stage_scope(ticket_id, self._board_py, self._log_file, run_git,
                           repo=code_tree, include_engine_outputs=False,
                           touches_workspace=code_tree, touches=touches)

    def _split_touches_by_tree(self, ticket_id: str,
                               code_tree: Path) -> tuple[list[str], list[str]]:
        """티켓 touches 를 (코드 트리 몫, PM 홈 몫)으로 가른다.

        판정은 **어느 트리에 실재하는가** 다: 코드 트리에 있으면 코드 트리, 없고 PM 홈에 있으면
        PM 홈(wiki·결정 기록·domain 같은 홈-상주 산출물), 어디에도 없으면 두 계획 모두(신규
        파일이거나 추적 중인 삭제 — 어느 쪽인지는 각 트리의 stage 필터가 판정한다). 두-repo 계획이 코드 몫만 계획하면 홈-상주 touches 가 **어느 repo 에서도 stage 되지
        않고** 잔여 보고에만 남는다 — 그 경로들은 이미 선언돼 있어 "touches 를 보강하라"는 처방도
        듣지 않는다. 정규화 불능이면 전부 코드 몫으로 두어 `stage_scope` 가 기존과 같은 fail-loud
        사유를 내게 한다(여기서 삼키지 않는다).
        """
        touches = list(get_ticket_touches(self._board_py, ticket_id))
        if not touches:
            return [], []
        # 코드 트리 좌표 해소는 측정이 쓰는 공용 seam 을 그대로 쓴다(사본 0·같은 규칙).
        # 그 seam 의 workspace 가 곧 `_code_tree()` 이므로 여기 코드 트리와 같은 트리다.
        in_code_tree = self._normalize_measured_touches(touches, warn=False)
        if in_code_tree is None or len(in_code_tree) != len(touches):
            return touches, []
        code_touches: list[str] = []
        home_touches: list[str] = []
        for declared, normalized in zip(touches, in_code_tree):
            in_code = _path_exists_in(code_tree, normalized)
            in_home = _path_exists_in(REPO, declared)
            if in_code:
                code_touches.append(declared)
            elif in_home:
                home_touches.append(declared)
            else:
                # 어디에도 실재하지 않는 선언 — 신규 파일일 수도, **추적 중인 삭제**일 수도 있다.
                # 한쪽으로만 접으면 삭제가 그 트리에서 추적되지 않아 stage 필터에 떨어지고 어느
                # 계획에도 안 실린다. 둘 다에 넣어도 각 계획의 필터가 자기 트리에서 추적되는 것만
                # 남기므로 이중 stage 는 생기지 않는다.
                code_touches.append(declared)
                home_touches.append(declared)
        return code_touches, home_touches

    @staticmethod
    def _is_separated_code_tree(code_tree: Path) -> bool:
        """이 코드 트리가 PM 홈과 분리된 트리인가 — 판정은 `트리 != PM 홈` 하나다.

        "PM 홈 하위인가" 같은 위치 제약은 두지 않는다: 슬롯 worktree 가 심링크거나 다른
        마운트에 있으면 그 제약이 참이 아니고, 그러면 diff 측정은 코드 트리를 보는데 stage 만
        PM 홈으로 내려앉아 **한 값의 두 소비자가 갈린다**. 해소 자체가 이미 이 도구의 `REPO` 를
        앵커로 하므로(`_regression_cwd` 가 `repo_root` 를 forward) 트리가 PM 홈과 다르다는 것은
        곧 분리 형상이라는 뜻이다. 경로 해소 실패는 False(fail-soft·기존 단일 계획 유지)."""
        try:
            return code_tree.resolve() != REPO.resolve()
        except OSError:
            return False

    def _default_status_entries(self) -> tuple[tuple[str, str], ...]:
        """워킹트리 상태 `((XY, 경로), …)` (loud 보고 입력) — 실 해소.

        board 를 넘기는 이유는 **NUL 파서 공유** 뿐이다(비-ASCII 경로 실경로화). 로드 실패는
        보고를 멈추지 않는다 — `status_entries` 가 degraded 판독으로 이어간다.

        조회 실패는 `strict` 로 올린다 — 접는 자리는 이 seam 이 아니라 소비자다. 보고
        (`_dirty_split`)는 종전대로 빈 목록으로 접고, 종결의 선행 조건 관측(`_dirty_paths`)은
        그 실패를 clean 과 구별해 정지한다. 여기서 접으면 두 소비자가 같은 위장을 물려받는다.
        """
        return status_entries(self._run_git_stdout_fn, load_board_module(self._board_py),
                              strict=True)

    def _default_status_entries_at(self, cwd: Path) -> tuple[tuple[str, str], ...]:
        """`cwd` 트리의 워킹트리 상태 — 실패 처리는 `_default_status_entries` 와 같다."""
        return status_entries(lambda args: self._run_git_stdout_at_fn(cwd, args),
                              load_board_module(self._board_py), strict=True)

    def _stage_plans(self, ticket_id: str) -> tuple[RepoStagePlan, ...]:
        """실제 stage 와 dry-run 이 공유하는 repo별 계획 단일 진실.

        분기 축은 task 여부가 아니라 **해소된 코드 트리가 PM 홈인가**다 — 코드 트리 소비자
        (diff 측정·[4/5] stage·PM-direct 재검)는 전부 `_code_tree()` 하나를 본다. 트리가 PM 홈
        자신이면(임베디드) 기존 단일 계획이고, 분리 형상(task 작업공간 또는 해소된 슬롯
        worktree)이면 PM 홈에 log/board 산출물, 코드 트리에 코드 touches 를 각각 둔다. 축을
        task 유무로 두면 슬롯 해소로 온 코드 트리에서 PM 홈의 동명 사본이 stage 된다.

        touches 는 **repo 별 몫으로 갈라** 각 계획에 넘긴다(`_split_touches_by_tree`) — 홈에만
        사는 touches(wiki·결정 기록·domain)를 코드 계획에만 두면 어느 repo 에서도 stage 되지
        않는다. [5/5] 커밋 안내도 이 계획을 그대로 따라 repo 별로 나온다.
        """
        code_tree = self._code_tree()
        if not self._is_separated_code_tree(code_tree):
            return (RepoStagePlan("PM 홈", REPO, self._stage_scope_fn(ticket_id)),)
        code_touches, home_touches = self._split_touches_by_tree(ticket_id, code_tree)
        home_scope = stage_scope(
            ticket_id, self._board_py, self._log_file, self._run_git_stdout_fn,
            include_touches=bool(home_touches), touches=home_touches,
        )
        label = ("task worktree touches" if self._task_workspace is not None
                 else "slot worktree touches")
        return (
            RepoStagePlan("PM 홈 산출물", REPO, home_scope),
            RepoStagePlan(label, code_tree,
                          self._code_stage_scope(ticket_id, code_tree, code_touches)),
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
            code_tree = self._code_tree()
            expanded = expand(code_tree, measured_touches)
            entries = (self._status_entries_at_fn(code_tree)
                       if self._is_separated_code_tree(code_tree)
                       else self._status_entries_fn())
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
        board_identity_args: Sequence[str] = (),
        close: bool = False,
        cluster_close: str | None = None,
    ) -> int:
        """ticket_id 완료 기록 전체 흐름을 실행한다.

        반환: 0=성공, 1=실패 (중단).

        `section` 은 후방호환용으로 받기만 하고 무시한다 — status.md 합계표 섹션 행은
        제거됐다(judgment-only·테스트 수는 박제 안 함).

        `board_identity_args` = 이 실행이 이미 해소한 정체성을 board 하위 호출에 그대로
        넘기는 인자다(`--repo/--slot` 또는 `--task`). board 의 complete 가 `claimed_by` 와
        실행 세션을 대조하므로, 이 forward 가 없으면 다중 슬롯 채택자에서 ticket_finish
        경유 완료가 전부 "남의 티켓" 으로 거부된다 — ticket_finish 는 정체성을 이미 해소해 두고
        board 에 넘기지 않고 있었다.

        `skip_pytest`(--no-pytest) 는 [1/5] 회귀 단계를 건너뛴다 — 측정은 PM 이 /pm-qa
        등으로 별도. board complete 는 `--tests-pass` 를 유지한다(pm_handoff `--no-pytest` 동형·
        회귀 red 아님·skip 로 진행). **회귀 실행만** 건너뛴다 — 코드 트리 해소·diff 측정·
        stage 는 그대로다(`_code_tree`).

        `close` 는 이 실행이 종결 파이프라인 안에 있다는 뜻이다 — [1/5]~[4/5]는 그대로이고
        [5/5]의 손작업 안내만 바뀐다(커밋·재배치·머지·반납·board 기록을 종결이 실행하므로 그
        안내가 남으면 이미 기계가 한 일을 사람에게 시킨다). 판정·부작용은 같다.

        `cluster_close`는 ``ClusterCloser``가 넘기는 exact ledger path-binding이다. 인증
        capability가 아니며 public ``board complete``가 묶음 멤버를 반쪽 완료하지 못하게 한다.
        """
        del section  # status 합계표 제거로 더 이상 쓰지 않음(후방호환 수용만).
        print(
            f"[ticket_finish] {ticket_id} 완료 기록 시작 "
            f"(dry_run={dry_run}, skip_pytest={skip_pytest})"
        )

        # PM 판정은 권위를 유지한다. 이 재검은 경고만 보이고 이후 rc를 바꾸지 않는다.
        self._warn_pm_direct_conditions(ticket_id)

        # ── 0. 진입 게이트(preflight) — diff 서킷브레이커 · 사설 참조 · DoD 기록 ·
        # 코드 트리 잔여 · 자기 축 회귀 ────────────────────────────────────
        # 다섯 다 회귀보다 **앞**이고, 무엇보다 [2/5] log 스켈레톤 append 보다 앞이다: 여기서
        # 막힐 실행은 어떤 부작용(회귀 실행·log append·board·git)도 내지 않아야 한다. 사설
        # 참조는 diff 서킷브레이커 바로 뒤다 — 둘 다 같은 claim 앵커 측정 폭(touches ∩ 출하
        # 표면)을 diff 축에서 재는 판정이고, 크기(diff_cap)든 내용(사설 참조)이든 그 결과가
        # 확정 차단이면 뒤의 DoD·잔여 판정을 물을 이유가 없다. DoD 판정이 [3/5]
        # `board.py complete` 안에만 있던 동안에는 차단마다 stray 스켈레톤이 남고 재실행이
        # 그것을 중복 append 했다(실측) — 순서가 곧 결함이었다. 잔여 판정(코드 트리 dirty ⊄
        # 선언 스코프)도 같은 이유로 여기 있다 — [4/5] stage 뒤라면 board complete 가 이미
        # 기록된 뒤에야 막혀 되돌릴 수 없다. 자기 축 회귀는 **비용순으로 마지막**이다 — 앞
        # 넷은 서브프로세스 0~2회, 이건 작업 트리가 red 일 때만 pytest 를 최대 2번 돈다
        # (green 이면 0회).
        for preflight in (self._diff_cap_block_fn, self._private_ref_block_fn,
                         self._dod_block_fn, self._residual_block_fn,
                         self._self_axis_block_fn):
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

        log_has_entry = self._log_has_entry(ticket_id)
        if log_has_entry:
            # 재실행이 곧 재개다 — 뒤 단계가 실패해 이 자리를 다시 지날 때 관측 없이 append
            # 하면 같은 스켈레톤이 중복으로 쌓인다.
            prefix = "[dry-run] " if dry_run else ""
            print(f"  {prefix}{ticket_id} 스켈레톤 이미 있음 — append 건너뜀")
        elif dry_run:
            print("  [dry-run] log/current.md 에 append 할 스켈레톤:")
            print("  " + skeleton.replace("\n", "\n  "))
        else:
            _load_pm_log().append_log(self._log_file, "\n" + skeleton)
            print(f"  ✓ log/current.md 스켈레톤 append ({ticket_id})")

        # ── 3. board.py complete ──────────────────────────────────────
        print("\n[3/5] board.py complete...")
        board_complete_argv = ["complete", ticket_id, "--tests-pass"]
        if cluster_close is not None:
            board_complete_argv.extend(["--cluster-close", cluster_close])
        board_complete_argv.extend(board_identity_args)
        if dry_run:
            print(f"  [dry-run] board.py {' '.join(board_complete_argv)}")
        else:
            board_rc, board_out = self._run_board_fn(board_complete_argv)
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
        # stage 한다. 코드 트리의 스코프 밖 잔여는 preflight `_residual_block_fn` 이 이미 차단했다
        # (여기 도달했다는 것 자체가 코드 트리는 깨끗하다는 뜻). PM 홈 dev-state(판정 인구 밖) 등
        # 남는 잔여는 아래 loud 보고로 계속 가시화한다(비차단 — 내가 정리할 수 없는 남의 파일).
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
        if close:
            print("  git commit — 종결 파이프라인이 실행한다(커밋·재배치·머지·슬롯 반납·"
                  "board 기록까지 · 손 git 없음)")
        elif not multi_repo:
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
        # domain.py 부재(신규 clone) → None → 조용히 skip(무영향).
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
            print(f"\n[완료] {ticket_id} 기록 완료.{tail}")

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

        두 실패 방향을 함께 본다 — 1) **미스테이지 잔여**(under-stage: 스코프가 못 덮은 변경 —
        내 작업 누락이면 `touches` 를 보강해 다시 stage, 남의 WIP 면 그대로 둔다) 2)
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

# ── close 파이프라인 (묶음 종결) ───────────────────────────────────────────
#
# 종결은 순서가 있는 일곱 단계다: 리뷰 게이트 처분(확인 생성) → 티켓별 완료 기록 → 슬롯 커밋 →
# 통합 브랜치로 재배치 → 머지 → 슬롯 반납 → board·포인터 커밋. 사람이 순서를 규칙으로
# 지키던 자리를 한 커맨드가 가져간다 — 순서가 판정 결과를 바꾸던 축(사설 참조·측정 폭)은 이미
# 판정 기준 교체로 닫혔으므로, 남은 것은 "빠뜨리지 않는" 축이다(반납 누락·포인터 커밋 누락이
# 실측된 잔여다).
#
# **막을 조건은 첫 부작용 전에 전부 모아 한 번에 낸다**(`preflight`). 각 단계가 자기 차례에
# 와서야 자기 조건을 보면 한 실행에 차단 사유가 하나씩만 나오고, 첫 완주까지 사람이 같은
# 명령을 여러 번 친다(실측). 그래서 단계마다 **읽기 전용 선검사**를 선언으로 달고, 실행 전에
# 그것만 전부 돌려 사유를 누적한다. 편입 기준은 "읽기 전용"이 아니라 **단조성**이다 — 앞
# 단계가 해소할 수 있는 조건(작업 트리 dirty·log entry 부재·게이트 미처분)을 선검사에 넣으면
# 정상 실행이 첫 줄에서 막힌다. 실행해야 아는 것(머지 충돌·회귀 red)은 선언에서 `None` 이다.
#
# **재실행이 곧 재개다.** 각 단계는 자기 부작용이 이미 있는지 **관측**해서 건너뛴다(티켓이
# done 인가 · 커밋할 변경이 남았나 · 통합 브랜치의 조상인가 · 슬롯이 아직 대여 중인가). 장부의
# 단계 기록은 그 관측을 대신하지 않는다 — 기록과 실제가 어긋나면(중단·외부 원복) 기록을 믿는
# 재개는 없는 커밋 위에서 진행한다. 기록은 어디까지 갔는지 사람이 읽는 값이다.
#
# 크기 1 묶음도 같은 경로를 탄다(특례 없음) — 티켓 하나짜리 종결이라고 다른 코드가 도는 일은
# 없다. 발행이 만드는 크기 1 장부는 기준 브랜치(`base_branch`)를 싣고 묶음 브랜치(`branch`)만
# 비우므로 머지가 **무대상**이다. 기준 브랜치 미선언은 그 축이 아니라 판정 기준의 부재라 정지다.

_CLOSE_STEP_KEY = "close_step"            # 장부에 남기는 단계 진행 기록(보조 기록)
_CLUSTER_STATUS_OPEN = "open"             # 열거의 소유자는 board — 그 열거 안에 있을 때만 쓴다
_CLUSTER_STATUS_CLOSING = "closing"
_CLUSTER_STATUS_CLOSED = "closed"
_CLOSE_BOARD_POINTER_PATH = ".project_manager/board"
_CLOSE_SUBMODULE_MODE = "160000"          # `ls-files --stage` 가 gitlink 에 쓰는 모드
_CLOSE_MERGE_MESSAGE = "{unit} merge — {title}"
_CLOSE_BOARD_MESSAGE = "{unit} board — {title}"


_CLOSE_INTEGRATION_UNDECLARED = (
    "{cluster} 장부가 통합 브랜치(base_branch)를 선언하지 않았다 — 재배치·머지의 대상이 "
    "그 선언이다. `board.py cluster show {cluster}` 로 장부를 확인하고 `base_branch` 를 채운 "
    "뒤 다시 실행하라."
)

_CLOSE_INTEGRATION_BRANCH_MISSING = (
    "통합 브랜치 {integration} 를 이 코드 트리에서 찾지 못했다 ({tree}) — 장부가 선언한 "
    "이름과 이 트리의 브랜치를 대조하라."
)

_CLOSE_SOURCE_BRANCH_MISSING = "묶음 브랜치 {source} 를 이 코드 트리에서 찾지 못했다 ({tree})."

_CLOSE_INTEGRATION_TREE_MISSING = (
    "{integration} 를 체크아웃한 작업 트리를 찾지 못했다 — 같은 브랜치를 두 트리가 동시에 "
    "체크아웃할 수 없으므로 머지는 그 트리에서만 된다. 통합 브랜치를 가진 트리를 열어 두고 "
    "다시 실행하라."
)

_CLOSE_INTEGRATION_TREE_DIRTY = (
    "{integration} 트리에 남은 변경이 있어 머지하지 않는다 ({count}건 · {tree})."
)

_CLOSE_TICKET_NOT_CLAIMED = (
    "{ticket} 가 claimed 가 아니다({status}) — 완료 기록은 claimed 티켓만 done 으로 옮긴다."
)

_CLOSE_OPEN_ALL_DONE = (
    "{cluster} 장부는 open 인데 멤버 {members} 가 이미 전부 done 이다 — 정상 종결 7단계는 이 "
    "반쪽 상태를 닫지 않는다(그 완료 기록은 이 파이프라인이 만든 것이 아니라 커밋·재배치·머지가 "
    "무엇을 근거로 도는지 알 수 없다). 이미 통합된 commit 을 명시하는 복구 문으로 닫는다:\n"
    "  python3 .project_manager/tools/ticket_finish.py --cluster {cluster} "
    "--reconcile-integrated \\\n"
    "    --integrated-rev T-NNNN=<commit> [--legacy-base-rev T-NNNN=<commit>] \\\n"
    "    --user-ack {cluster} --repo <repo> --slot <N>"
)


class _CloseObservationFailure(Exception):
    """close 단계의 **관측 자체가 실패**했다 — 그 단계에서 정지한다.

    "관측했는데 대상이 없다"(커밋할 변경 없음·리스 없음·서브모듈 아님)와 구별한다: 전자는
    건너뛰기이고 이쪽은 판정 불능이다. 판정 불능을 완료·clean·무대상으로 접으면 그 단계가
    실제로 끝났다는 증거 없이 다음 단계로 넘어가고, 마지막에 남는 것은 근거 없는 종결 기록뿐
    이다. 관측이 신뢰의 뿌리라는 불변식은 이 구분으로만 지켜진다."""


class ClusterCloser:
    """묶음 종결(close) — 고정 일곱 단계·상태 관측 기반 멱등 + 첫 부작용 앞 선검사.

    git·board·위임 호출은 전부 `TicketFinisher` 가 이미 들고 있는 seam 을 그대로 쓴다(사본 0).
    이 클래스가 새로 받는 seam 은 둘뿐이다 — 라운드 처분 CLI 와 슬롯 반납.
    """

    # `(단계 키, 사람이 읽는 이름, 읽기 전용 선검사 메서드 이름 또는 None)`.
    # 세 번째 값이 **선언**이다 — `None` 은 "실행해야 아는 것"(머지 충돌·커밋 rc·반납 거부).
    # `run()`·`preflight()` 가 이 3-튜플을 언팩하므로 선언을 빠뜨린 단계를 추가하면 그 자리에서
    # `ValueError` 다(별도 가드 장치 없음).
    STEPS: tuple[tuple[str, str, str | None], ...] = (
        ("resolve", "기계 확인 생성·리뷰 게이트 처분", "_pre_resolve"),
        ("record", "티켓별 완료 기록", "_pre_record"),
        ("commit", "슬롯 커밋", None),
        ("rebase", "통합 브랜치로 재배치", "_pre_rebase"),
        ("merge", "통합 브랜치 머지", "_pre_merge"),
        ("release", "슬롯 반납", None),
        ("board", "board·포인터 커밋", "_pre_board"),
    )

    def __init__(
        self,
        cluster: str,
        *,
        finisher: "TicketFinisher",
        board_py: Path = BOARD_PY,
        slot: str | None = None,
        task: str | None = None,
        session: str | None = None,
        board_identity_args: Sequence[str] = (),
        dry_run: bool = False,
        skip_pytest: bool = False,
        reconcile_integrated: bool = False,
        integrated_revisions: Sequence[str] = (),
        legacy_base_revisions: Sequence[str] = (),
        user_ack: str | None = None,
        explicit_slot: bool = False,
        run_delegate_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        release_fn: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        self._cluster = cluster
        self._finisher = finisher
        self._board_py = board_py
        self._slot = slot
        self._task = task
        self._session = session
        self._board_identity_args = list(board_identity_args)
        self._dry_run = dry_run
        self._skip_pytest = skip_pytest
        self._reconcile_integrated = reconcile_integrated
        self._integrated_revision_args = tuple(integrated_revisions)
        self._legacy_base_revision_args = tuple(legacy_base_revisions)
        self._user_ack = user_ack
        self._explicit_slot = explicit_slot
        # 라운드 처분 seam — 묶음 표면이 있으면 한 번, 없으면 티켓별로 반복한다(둘 다 같은
        # 커맨드의 인자 차이라 여기서 고르는 것은 argv 뿐이다).
        self._run_delegate_fn = run_delegate_fn or self._default_run_delegate
        self._release_fn = release_fn or self._default_release
        self._board = _board_module_at(board_py)
        self._tickets: tuple[str, ...] = ()

    # ── 기본 seam 구현 ────────────────────────────────────────────────

    def _default_run_delegate(self, args: list[str]) -> tuple[int, str]:
        """pm_delegate.py 를 subprocess 로 호출해 (returncode, stdout+stderr) 반환."""
        delegate_py = Path(self._board_py).with_name("pm_delegate.py")
        result = subprocess.run(
            [str(self._finisher._venv_python), str(delegate_py)] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO),
        )
        return result.returncode, result.stdout + result.stderr

    def _default_release(self, slot: str) -> tuple[bool, str]:
        """슬롯을 반납한다 — `(성공, 사람이 읽는 한 줄)`.

        dirty 슬롯은 거부다(`require_clean=True`) — 자동 경로라고 stash 로 밀어 넣으면 종결이
        산출물을 조용히 치운다. 소유 검사(`owner_task`)도 그대로 태운다.
        """
        pool = _load_tool_module(TOOLS_DIR / "worktree_pool.py")
        release = getattr(pool, "release", None) if pool is not None else None
        if release is None:
            return False, "worktree_pool 반납 seam 을 로드하지 못했다"
        try:
            release(slot, require_clean=True, owner_task=self._task)
        except Exception as exc:  # noqa: BLE001 — 반납 거부는 사유를 그대로 올린다.
            if _is_engine_rev_skew(exc):
                raise
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"슬롯 {slot} 반납됨"

    def _slot_state(self, slot: str) -> str | None:
        """슬롯의 현재 상태 — 리스 장부에 그 슬롯이 없으면 None(관측 성공·대여 아님).

        조회 seam 부재·조회 실패는 판정 불능이라 정지다 — 반납 여부를 모른 채 다음으로 넘어가면
        반납 누락(실측된 잔여)이 그대로 남는다."""
        pool = _load_tool_module(TOOLS_DIR / "worktree_pool.py")
        state_fn = getattr(pool, "slot_state", None) if pool is not None else None
        if state_fn is None:
            raise _CloseObservationFailure(
                f"슬롯 상태 조회 seam 을 로드하지 못했다 ({TOOLS_DIR / 'worktree_pool.py'}) — "
                "반납 여부를 관측할 수 없다. pm-update 로 .project_manager/tools/ 를 재동기하라.")
        try:
            return state_fn(slot)
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 판정 불능(정지).
            if _is_engine_rev_skew(exc):
                raise
            raise _CloseObservationFailure(
                f"슬롯 {slot} 상태를 관측하지 못했다 — {type(exc).__name__}: {exc}") from exc

    # ── 공용 조회 ─────────────────────────────────────────────────────

    def _git(self, cwd: Path, args: list[str]) -> tuple[int, str]:
        """mutation git — 진단(stderr)을 합쳐 돌려주는 seam 쪽이다."""
        if cwd == REPO:
            return self._finisher._run_git_fn(args)
        return self._finisher._run_git_at_fn(cwd, args)

    def _git_stdout(self, cwd: Path, args: list[str]) -> tuple[int, str]:
        """기계 판독 git — stdout 만 보는 seam(경고 한 줄이 값으로 둔갑하지 않게)."""
        if cwd == REPO:
            return self._finisher._run_git_stdout_fn(args)
        return self._finisher._run_git_stdout_at_fn(cwd, args)

    def _ledger(self) -> dict | None:
        """묶음 장부 frontmatter (없으면 None — 크기 1 해석은 멤버 조회가 소유한다)."""
        load = getattr(self._board, "load_cluster", None) if self._board else None
        return load(self._cluster) if load is not None else None

    def _declared_branch(self, key: str) -> str | None:
        """장부가 선언한 브랜치 이름 (`branch`=묶음 브랜치 · `base_branch`=통합 브랜치)."""
        value = (self._ledger() or {}).get(key)
        return value.strip() or None if isinstance(value, str) else None

    def _ticket_status(self, tid: str) -> str | None:
        """그 티켓이 지금 놓인 board 상태 (board 에 없으면 None — 관측 성공).

        조회 seam 부재·조회 실패는 판정 불능이라 정지다 — 완료 기록이 이미 끝났는지 모른 채
        진행하면 앞 단계 부작용을 반복한다."""
        find = getattr(self._board, "find_ticket_exact", None) if self._board else None
        if find is None:
            raise _CloseObservationFailure(
                f"티켓 상태 조회 seam 을 로드하지 못했다 ({self._board_py}) — 완료 기록이 이미 "
                "끝났는지 관측할 수 없다. board 사본을 확인하라.")
        try:
            found = find(tid)
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 판정 불능(정지).
            if _is_engine_rev_skew(exc):
                raise
            raise _CloseObservationFailure(
                f"{tid} 상태를 관측하지 못했다 — {type(exc).__name__}: {exc}") from exc
        return found[0] if found is not None else None

    def _gate_ledger(self) -> dict:
        """내부 리뷰 게이트 장부 — 파일 부재는 빈 dict(라운드 기록 없음 = 대상 없음).

        읽기·파싱 실패는 판정 불능이라 정지다 — 처분이 필요한 게이트가 있는지 모른 채 확인
        단계를 통과시키면, 잔여가 남은 채로 종결이 진행된다."""
        path_fn = (getattr(self._board, "_internal_review_rounds_ledger", None)
                   if self._board else None)
        if path_fn is None:
            raise _CloseObservationFailure(
                f"리뷰 라운드 장부 seam 을 로드하지 못했다 ({self._board_py}) — 처분 잔여를 "
                "관측할 수 없다. board 사본을 확인하라.")
        path = path_fn()
        if not Path(path).exists():
            return {}
        try:
            data = json.loads(
                _load_file_lock().read_text_shared(path, encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise _CloseObservationFailure(
                f"리뷰 라운드 장부를 읽지 못했다 ({path}) — "
                f"{type(exc).__name__}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _pending_gates(self) -> list[str]:
        """처분이 필요한 게이트 티켓 — 잔여가 있고 아직 처분 선언이 없는 것."""
        board = self._board
        has_residual = getattr(board, "gate_has_residual", None) if board else None
        resolution = getattr(board, "gate_resolution", None) if board else None
        if has_residual is None or resolution is None:
            return []
        ledger = self._gate_ledger()
        pending: list[str] = []
        for tid in self._tickets:
            entry = ledger.get(tid)
            if not isinstance(entry, dict) or not has_residual(entry):
                continue
            if resolution(entry) is not None:
                continue
            pending.append(tid)
        return pending

    def _pending_paths(self, cwd: Path, pathspec: Sequence[str]) -> tuple[str, ...]:
        """그 pathspec 안에 아직 커밋되지 않은 경로 (관측 실패는 정지).

        커밋 단계가 자기 부작용의 유무를 묻는 관측 지점이다 — 남은 변경이 없으면 이미 커밋된
        것이고, 재실행은 빈 커밋을 만들지 않는다. 조회가 실패하면 **커밋했는지 모르는** 것이라
        빈 목록(=이미 커밋됨)으로 접지 않는다 — 그렇게 접으면 아무것도 커밋하지 않은 실행이
        커밋한 실행과 같은 모양으로 끝난다.

        **서브모듈 내부 dirty 는 변경으로 세지 않는다**(`--ignore-submodules=dirty`) — 상위
        repo 가 커밋할 수 있는 것은 포인터뿐이라, 내부 dirty 를 변경으로 읽으면 커밋할 것이 없는
        자리에서 커밋을 시도하고 git 이 거부한다(그 dirty 는 그 repo 가 자기 커밋으로 닫는다).
        """
        if not pathspec:
            return ()
        try:
            rc, out = self._git_stdout(
                cwd,
                ["status", "--porcelain", "-z", "--untracked-files=all",
                 "--ignore-submodules=dirty", "--", *pathspec],
            )
        except Exception as exc:  # noqa: BLE001 — 관측 실패는 정지(무대상 위장 금지).
            if _is_engine_rev_skew(exc):
                raise
            raise _CloseObservationFailure(
                f"남은 변경을 관측하지 못했다 ({cwd}) — "
                f"{type(exc).__name__}: {exc}") from exc
        if rc != 0:
            raise _CloseObservationFailure(
                f"남은 변경을 관측하지 못했다 ({cwd} · git status rc={rc}): {out.rstrip()}")
        parse = (getattr(self._board, "git_parse_porcelain_z", None)
                 if self._board else None)
        if parse is not None:
            entries = parse(out)
        else:
            entries = tuple((token[:2], token[3:])
                            for token in out.split("\0") if len(token) >= 4)
        return tuple(path for _code, path in entries)

    def _is_ancestor(self, cwd: Path, ancestor: str, descendant: str) -> bool:
        """`ancestor` 가 `descendant` 의 조상인가 — 판정 불능은 정지.

        `git merge-base --is-ancestor` 는 조상이면 rc 0, 아니면 rc 1 이다. 그 밖의 rc(잘못된
        ref·비-repo)는 답이 아니라 판정 실패이므로 False(=아직 안 들어갔다)로 접지 않는다 —
        그렇게 접으면 이미 끝난 재배치·머지를 다시 시도한다."""
        try:
            rc, out = self._git_stdout(
                cwd, ["merge-base", "--is-ancestor", ancestor, descendant])
        except Exception as exc:  # noqa: BLE001 — 판정 실패는 정지.
            if _is_engine_rev_skew(exc):
                raise
            raise _CloseObservationFailure(
                f"{ancestor} 가 {descendant} 의 조상인지 판정하지 못했다 ({cwd}) — "
                f"{type(exc).__name__}: {exc}") from exc
        if rc not in (0, 1):
            raise _CloseObservationFailure(
                f"{ancestor} 가 {descendant} 의 조상인지 판정하지 못했다 "
                f"({cwd} · rc={rc}): {out.rstrip()}")
        return rc == 0

    def _dirty_paths(self, cwd: Path) -> tuple[str, ...]:
        """그 트리에 남은 변경 전체 (재배치·머지·반납의 선행 조건 관측 · 실패는 정지).

        조회 실패를 빈 목록으로 접으면 **clean 으로 위장**돼 dirty 트리에서 재배치·머지가 돈다."""
        try:
            entries = self._finisher._status_entries_at_fn(cwd)
        except Exception as exc:  # noqa: BLE001 — 관측 실패는 정지(clean 위장 금지).
            if _is_engine_rev_skew(exc):
                raise
            raise _CloseObservationFailure(
                f"작업 트리 상태를 관측하지 못했다 ({cwd}) — "
                f"{type(exc).__name__}: {exc}") from exc
        return tuple(path for _code, path in entries)

    def _integration_worktree(self, branch: str) -> Path | None:
        """그 브랜치를 체크아웃한 작업 트리 (없으면 None).

        머지는 통합 브랜치를 **가진 트리에서** 해야 한다 — 같은 브랜치를 두 트리가 동시에
        체크아웃할 수 없으므로(git 제약) 종결 슬롯에서 체크아웃해 오는 길은 없다.
        """
        try:
            rc, out = self._git_stdout(
                self._code_tree(), ["worktree", "list", "--porcelain"])
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 정지(미해소 위장 금지).
            if _is_engine_rev_skew(exc):
                raise
            raise _CloseObservationFailure(
                f"작업 트리 목록을 관측하지 못했다 ({self._code_tree()}) — "
                f"{type(exc).__name__}: {exc}") from exc
        if rc != 0:
            raise _CloseObservationFailure(
                f"작업 트리 목록을 관측하지 못했다 "
                f"({self._code_tree()} · rc={rc}): {out.rstrip()}")
        target = f"refs/heads/{branch}"
        current: str | None = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                current = line[len("worktree "):].strip()
            elif line.startswith("branch ") and current:
                if line[len("branch "):].strip() == target:
                    return Path(current)
        return None

    def _code_tree(self) -> Path:
        return self._finisher._code_tree()

    def _wrong_branch_reason(self, tree: Path, source: str) -> str | None:
        """이 코드 트리가 묶음 브랜치를 들고 있는가 — 아니면 사유(다른 브랜치를 옮기지 않는다).

        재배치는 HEAD 를, 머지는 장부가 선언한 브랜치를 옮긴다. 둘이 같은 브랜치가 아니면 이
        슬롯의 작업이 아닌 것을 통합 브랜치에 얹게 되므로 첫 mutation 앞에서 거부한다.
        """
        rc, out = self._git_stdout(tree, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        current = (out or "").strip() if rc == 0 else ""
        if current == source:
            return None
        return (f"이 코드 트리가 든 브랜치({current or 'detached HEAD'})가 장부의 묶음 브랜치 "
                f"{source} 와 다르다 ({tree}) — 종결은 묶음 브랜치를 체크아웃한 슬롯에서 "
                "돈다(다른 브랜치를 옮기지 않는다).")

    def _unit(self) -> tuple[str, str]:
        """커밋 문안이 쓰는 `(단위 표기, 제목)` — 크기 1 은 티켓 표기, 묶음은 묶음 표기."""
        if len(self._tickets) == 1:
            tid = self._tickets[0]
            return tid, (self._finisher._ticket_title_fn(tid) or tid)
        name_fn = getattr(self._board, "cluster_name_of", None) if self._board else None
        name = name_fn(self._cluster) if name_fn is not None else self._cluster
        return self._cluster, name

    def _plans(self, tid: str) -> tuple[RepoStagePlan | None, RepoStagePlan | None]:
        """그 티켓의 `(PM 홈 계획, 코드 트리 계획)` — 단일 트리 형상은 같은 계획을 둘 다 준다."""
        plans = self._finisher._stage_plans(tid)
        home = next((plan for plan in plans if plan.cwd == REPO), None)
        code = next((plan for plan in plans if plan.cwd != REPO), None)
        if code is None:
            code = home       # 임베디드 형상 — 코드와 홈이 같은 트리다.
        return home, code

    # ── 단계 ──────────────────────────────────────────────────────────

    def _pre_resolve(self) -> list[str]:
        """resolve가 소비할 final-fix verify 입력을 read-only 검증한다(board 판정 소유).

        게이트 **처분 여부**는 여기 없다 — 처분은 resolve 자신이 하는 일이라 비단조다. 여기서
        묻는 것은 그 처분이 소비할 입력이 지금 갖춰졌는가뿐이다.
        """
        board = self._board
        pending = self._pending_gates()
        if not pending:
            print("  처분할 리뷰 잔여 없음 — 확인 입력 선검사 대상 0")
            return []
        problem_fn = (
            getattr(board, "_pm_verified_resolution_input_problem", None)
            if board else None
        )
        channel = getattr(board, "GATE_CHANNEL_INTERNAL", None) if board else None
        if problem_fn is None or channel is None:
            return ["final-fix 확인 입력 preflight seam 부재 — board 사본을 확인하세요"]
        ledger = self._gate_ledger()
        problems: list[str] = []
        for tid in pending:
            problem = problem_fn(tid, channel=channel, entry=ledger.get(tid) or {})
            if problem is None:
                print(f"  ✓ {tid} final-fix 확인 입력 complete · resolve 실행 가능")
            else:
                problems.append(f"final-fix 확인 입력 미충족 — {tid}: {problem}")
        return problems

    def _step_resolve(self) -> str | None:
        """리뷰 게이트 처분 — 묶음 표면이 있으면 한 번, 없으면 티켓별로 반복한다."""
        pending = self._pending_gates()
        if not pending:
            print("  처분할 게이트 없음 — 건너뜀")
            return None
        for argv in self._resolve_argv_sets(pending):
            if self._dry_run:
                print(f"  [dry-run] pm_delegate.py {' '.join(argv)}")
                continue
            rc, out = self._run_delegate_fn(argv)
            for line in out.rstrip().splitlines():
                print(f"  {line}")
            if rc != 0:
                return (f"리뷰 게이트 처분 실패 (rc={rc}) — `{' '.join(argv)}`. 위 사유를 "
                        "해소한 뒤 다시 실행하라(앞 단계는 반복하지 않는다).")
        return None

    def _resolve_argv_sets(self, pending: Sequence[str]) -> list[list[str]]:
        """이번 처분이 쓸 argv 목록 — 대상과 이미 해소한 코드 트리 정체성을 함께 넘긴다."""
        identity = list(self._board_identity_args)
        if self._delegate_supports_cluster():
            return [[
                "rounds", "resolve", "--cluster", self._cluster, "--pm-verified", *identity,
            ]]
        return [
            ["rounds", "resolve", "--gate", tid, "--pm-verified", *identity]
            for tid in pending
        ]

    def _delegate_supports_cluster(self) -> bool:
        """처분 표면이 묶음 인자를 받는가 — 그 CLI 자신에게 물어본다(사본 판정 없음)."""
        try:
            rc, out = self._run_delegate_fn(["rounds", "resolve", "--help"])
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 티켓별 반복으로 접는다.
            if _is_engine_rev_skew(exc):
                raise
            return False
        return rc == 0 and "--cluster" in out

    def _pre_record(self) -> list[str]:
        """완료 기록의 읽기 전용 차단 사유 전건 — 이미 done 인 티켓은 세지 않는다.

        여기 **없는** 것과 그 이유: [1/5] 회귀 red 와 자기 축 회귀는 판정 입력이 pytest 실행
        결과라 읽기로 답이 나오지 않는다. [2/5] log entry 부재와 내부 리뷰 잔여는 각각 그
        append 와 앞 단계(resolve)가 **해소하는** 조건이라 선검사에 넣으면 정상 실행이 첫 줄에서
        막힌다(비단조).

        판정은 전부 남의 함수다 — 완료 기록이 쓰는 DI 네 개(`_diff_cap_block_fn`·
        `_private_ref_block_fn`·`_dod_block_fn`·`_residual_block_fn`)와 `board complete` 가 쓰는
        판정 셋을 그대로 부른다(사본 0).
        """
        reasons: list[str] = []
        for tid in self._tickets:
            status = self._ticket_status(tid)
            if status == "done":
                continue
            if status != "claimed":
                # 여기서 멈추지 않는다 — `board complete` 는 이 한 줄에서 return 하지만, 그
                # 자리가 곧 "한 번에 하나씩" 의 원인이다. 나머지 판정도 전부 읽기로 답이 난다.
                reasons.append(_CLOSE_TICKET_NOT_CLAIMED.format(
                    ticket=tid, status=status or "board 에 없음"))
            for block_fn in (self._finisher._diff_cap_block_fn,
                             self._finisher._private_ref_block_fn,
                             self._finisher._dod_block_fn,
                             self._finisher._residual_block_fn):
                block = block_fn(tid)
                if block:
                    reasons.append(block)
            reasons.extend(self._board_complete_problems(tid))
        return reasons

    def _board_complete_problems(self, tid: str) -> list[str]:
        """`board complete` 가 첫 거부에서 멈추는 판정 셋을 읽기로 **전건** 모은다(사본 0).

        `cmd_complete` 는 결속 → 소유 → 게이트 순서로 첫 거부에서 return 하므로, 그 셋을 여기서
        각각 불러 누적한다. Namespace 는 `cmd_complete` 가 받는 것과 **같은 파서**로 만든다 —
        손으로 조립하면 소유 판정이 보는 속성이 조용히 빠진다.
        """
        board = self._board
        seams = {
            name: (getattr(board, name, None) if board else None)
            for name in ("find_ticket_exact", "load_ticket", "build_parser",
                         "cluster_complete_binding_problem", "_ticket_ownership",
                         "_ownership_rejection", "_round_completion_problems")
        }
        missing = sorted(name for name, seam in seams.items() if seam is None)
        if missing:
            raise _CloseObservationFailure(
                f"완료 게이트 판정 seam 을 로드하지 못했다 ({self._board_py}: {missing}) — "
                "차단 사유를 관측할 수 없다. board 사본을 확인하라.")
        found = seams["find_ticket_exact"](tid)
        if found is None:
            raise _CloseObservationFailure(
                f"{tid} 명세 파일을 board 에서 찾지 못했다 — 차단 사유를 관측할 수 없다.")
        _status, path = found
        frontmatter, _body = seams["load_ticket"](path)
        spec_text = _load_file_lock().read_text_shared(path, encoding="utf-8")
        problems: list[str] = []
        binding = seams["cluster_complete_binding_problem"](
            tid, frontmatter, self._cluster)
        if binding is not None:
            problems.append(f"{tid}: {binding}")
        namespace = seams["build_parser"]().parse_args(
            ["complete", tid, "--tests-pass", *self._board_identity_args])
        verdict = seams["_ticket_ownership"](frontmatter, namespace)
        if not verdict.ok:
            problems.append(seams["_ownership_rejection"]("complete", tid, verdict))
        problems.extend(
            f"{tid}: {problem}"
            for problem in seams["_round_completion_problems"](tid, spec_text))
        return problems

    def _has_pipeline_work(self) -> bool:
        """이 파이프라인이 아직 낼 부작용이 남아 있는가 — 커밋 대상이 하나라도 남았으면 참.

        "이미 끝난 단계는 차단 사유가 아니다"의 억제 조건이다. 커밋 전에는 묶음 브랜치가
        통합 브랜치의 조상이라 **이미 머지된 것처럼 보이는** 형상이 있는데, 그 관측을 그대로
        믿으면 선검사가 통과하고 커밋 뒤 머지 단계가 통합 트리 부재로 막힌다.
        """
        for tid in self._tickets:
            _home, code = self._plans(tid)
            pathspec = list(code.scope.pathspec) if code is not None else []
            if pathspec and self._pending_paths(code.cwd, pathspec):
                return True
        return False

    def _step_record(self) -> str | None:
        """티켓별 완료 기록 — 현행 완료 기록 그대로다(같은 코드 경로·같은 출력 줄)."""
        for tid in self._tickets:
            if self._ticket_status(tid) == "done":
                print(f"  {tid} 이미 done — 완료 기록 건너뜀")
                continue
            rc = self._finisher.run(
                ticket_id=tid,
                section=None,
                dry_run=self._dry_run,
                skip_pytest=self._skip_pytest,
                task=self._task,
                session=self._session,
                board_identity_args=self._board_identity_args,
                close=True,
                cluster_close=self._cluster,
            )
            if rc != 0:
                return (f"{tid} 완료 기록이 중단됐다 (rc={rc}) — 위 사유를 해소한 뒤 다시 "
                        "실행하라(이미 끝난 티켓은 다시 기록하지 않는다).")
        return None

    def _step_commit(self) -> str | None:
        """슬롯 커밋 — 티켓마다 그 티켓이 stage 한 경로를 제목 문안으로 커밋한다."""
        for tid in self._tickets:
            _home, code = self._plans(tid)
            pathspec = list(code.scope.pathspec) if code is not None else []
            if not pathspec:
                print(f"  {tid} 커밋할 선언 경로 없음")
                continue
            pending = self._pending_paths(code.cwd, pathspec)
            if not pending:
                print(f"  {tid} 커밋할 변경 없음 — 건너뜀")
                continue
            title = self._finisher._ticket_title_fn(tid) or tid
            if self._dry_run:
                print(f"  [dry-run] cwd={code.cwd} git commit -m \"{title}\" "
                      f"-- {' '.join(pathspec)}")
                continue
            rc, out = self._git(code.cwd, ["commit", "-m", title, "--", *pathspec])
            if rc != 0:
                return (f"{tid} 커밋 실패 (rc={rc}): {out.rstrip()}")
            print(f"  ✓ {tid} 커밋 — {len(pending)}개 경로 ({title})")
        return None

    def _pre_rebase(self) -> list[str]:
        """재배치의 읽기 전용 선행 조건 — 선언·존재·이 트리가 든 브랜치(사유 전건).

        작업 트리 dirty 와 재배치 충돌은 여기 없다 — 앞 단계(슬롯 커밋)가 해소하거나 실행해야
        아는 것이라 첫 부작용 앞에서 물으면 정상 실행이 첫 줄에서 막힌다.
        """
        integration = self._declared_branch("base_branch")
        if not integration:
            return [_CLOSE_INTEGRATION_UNDECLARED.format(cluster=self._cluster)]
        reasons: list[str] = []
        tree = self._code_tree()
        rc, _out = self._git_stdout(
            tree, ["rev-parse", "--verify", "--quiet", f"{integration}^{{commit}}"])
        if rc != 0:
            reasons.append(_CLOSE_INTEGRATION_BRANCH_MISSING.format(
                integration=integration, tree=tree))
        source = self._declared_branch("branch")
        if source:
            wrong_branch = self._wrong_branch_reason(tree, source)
            if wrong_branch is not None:
                reasons.append(wrong_branch)
        return reasons

    def _step_rebase(self) -> str | None:
        """통합 브랜치로 재배치 — 충돌은 원상 복구(abort) 후 정지한다.

        기준 브랜치 선언이 없으면 무대상이 아니라 정지다 — 재배치를 건너뛴 종결은 이 작업을
        통합 브랜치 밖에 남긴 채 완료를 선언한다."""
        blocks = self._pre_rebase()
        if blocks:
            return "\n".join(blocks)
        integration = self._declared_branch("base_branch")
        tree = self._code_tree()
        if self._is_ancestor(tree, integration, "HEAD"):
            print(f"  이미 {integration} 위에 있다 — 건너뜀")
            return None
        if self._is_ancestor(tree, "HEAD", integration):
            # 이 작업이 이미 통합 브랜치 안에 있다(머지가 끝난 뒤의 재실행) — 재배치할 것이
            # 남아 있지 않다. 이 관측이 없으면 종결이 끝난 묶음을 다시 돌릴 때마다 브랜치를
            # 통합 tip 으로 끌어올리는 불필요한 mutation 이 반복된다.
            print(f"  이 작업은 이미 {integration} 안에 있다 — 건너뜀")
            return None
        dirty = self._dirty_paths(tree)
        if dirty:
            return (f"작업 트리에 남은 변경이 있어 재배치하지 않는다 ({len(dirty)}건 · "
                    f"{tree}) — 앞 단계가 커밋하지 못한 경로를 확인하라.")
        if self._dry_run:
            print(f"  [dry-run] cwd={tree} git rebase {integration}")
            return None
        rc, out = self._git(tree, ["rebase", integration])
        if rc != 0:
            abort_rc, abort_out = self._git(tree, ["rebase", "--abort"])
            recovered = ("원상 복구했다" if abort_rc == 0
                         else f"원상 복구 실패({abort_out.rstrip()})")
            return (f"재배치 충돌 — {recovered}. 충돌을 해소한 뒤 다시 실행하라: "
                    f"{out.rstrip()}")
        print(f"  ✓ {integration} 위로 재배치")
        return None

    def _pre_merge(self) -> list[str]:
        """머지의 읽기 전용 선행 조건 — 선언·존재·현재 브랜치·통합 트리(사유 전건).

        통합 트리는 이 파이프라인이 커밋하지 않으므로 그 트리의 dirty 도 단조다(앞 단계가
        해소하지 않는다) — 그래서 여기 있다. 머지 충돌만 실행해야 아는 것이다.

        **이미 머지됐다는 관측은 아직 낼 부작용이 없을 때만 믿는다**(`_has_pipeline_work`).
        커밋 전에는 묶음 브랜치가 통합 브랜치의 조상이라 끝난 것처럼 보이는 형상이 있다.
        """
        integration = self._declared_branch("base_branch")
        if not integration:
            return [_CLOSE_INTEGRATION_UNDECLARED.format(cluster=self._cluster)]
        source = self._declared_branch("branch")
        if not source:
            return []                       # 묶음 브랜치 미선언 = 머지 무대상
        tree = self._code_tree()
        rc, _out = self._git_stdout(
            tree, ["rev-parse", "--verify", "--quiet", f"{source}^{{commit}}"])
        if rc != 0:
            return [_CLOSE_SOURCE_BRANCH_MISSING.format(source=source, tree=tree)]
        if not self._has_pipeline_work() and self._is_ancestor(tree, source, integration):
            return []                       # 이미 머지됐다 — 끝난 단계는 차단 사유가 아니다
        reasons: list[str] = []
        wrong_branch = self._wrong_branch_reason(tree, source)
        if wrong_branch is not None:
            reasons.append(wrong_branch)
        target_tree = self._integration_worktree(integration)
        if target_tree is None:
            reasons.append(
                _CLOSE_INTEGRATION_TREE_MISSING.format(integration=integration))
            return reasons
        dirty = self._dirty_paths(target_tree)
        if dirty:
            reasons.append(_CLOSE_INTEGRATION_TREE_DIRTY.format(
                integration=integration, count=len(dirty), tree=target_tree))
        return reasons

    def _step_merge(self) -> str | None:
        """통합 브랜치 머지 — 통합 브랜치를 가진 트리에서 `--no-ff` 로 받는다.

        묶음 브랜치(`branch`)는 사람이 선언한 묶음만 갖는다(크기 1 자동 장부는 없다) — 그
        선언이 없으면 받을 브랜치가 없어 무대상이다. 기준 브랜치 미선언은 그 축이 아니라
        판정 기준의 부재라 정지다."""
        blocks = self._pre_merge()
        if blocks:
            return "\n".join(blocks)
        integration = self._declared_branch("base_branch")
        source = self._declared_branch("branch")
        if not source:
            print("  묶음 브랜치 미선언 — 무대상")
            return None
        tree = self._code_tree()
        if self._is_ancestor(tree, source, integration):
            print(f"  {source} 는 이미 {integration} 에 있다 — 건너뜀")
            return None
        target_tree = self._integration_worktree(integration)
        if target_tree is None:
            # 선검사가 같은 관측으로 이미 막았어야 하는 자리 — 그 사이에 트리가 사라졌다.
            return _CLOSE_INTEGRATION_TREE_MISSING.format(integration=integration)
        unit, title = self._unit()
        message = _CLOSE_MERGE_MESSAGE.format(unit=unit, title=title)
        if self._dry_run:
            print(f"  [dry-run] cwd={target_tree} git merge --no-ff -m \"{message}\" {source}")
            return None
        rc, out = self._git(target_tree, ["merge", "--no-ff", "-m", message, source])
        if rc != 0:
            self._git(target_tree, ["merge", "--abort"])
            return (f"머지 충돌 — 원상 복구했다(merge --abort). 충돌을 해소한 뒤 다시 "
                    f"실행하라: {out.rstrip()}")
        print(f"  ✓ {source} → {integration} ({message})")
        return None

    def _step_release(self) -> str | None:
        """슬롯 반납 — dirty 슬롯은 거부다(자동 경로라고 산출물을 치우지 않는다)."""
        if not self._slot:
            print("  슬롯 미해소 — 무대상")
            return None
        state = self._slot_state(self._slot)
        if state is None:
            # 관측은 됐고 그 슬롯이 장부에 없다 — 대여한 적이 없으니 반납할 것도 없다.
            print(f"  슬롯 {self._slot} 리스 없음 — 무대상")
            return None
        if state != "leased":
            print(f"  슬롯 {self._slot} 이미 반납됨({state}) — 건너뜀")
            return None
        if self._dry_run:
            print(f"  [dry-run] pm-config release {self._slot}")
            return None
        ok, message = self._release_fn(self._slot)
        if not ok:
            return f"슬롯 반납 실패 — {message}"
        print(f"  ✓ {message}")
        return None

    def _pre_board(self) -> list[str]:
        """기록 대상 경로가 해소되는가 — 해소 불가는 차단 사유가 아니라 관측 실패(정지)다.

        단계와 **같은 함수**(`_board_git_paths`)를 부른다. board-git 로컬 커밋 미생성과 PM 홈
        커밋 실패는 실행해야 아는 것이라 여기 없다.
        """
        self._board_git_paths()
        return []

    def _step_board(self) -> str | None:
        """board-git 기록 + PM 홈 포인터·산출물 커밋 — 두 git 을 한 단계가 닫는다.

        종결 표시(`status=closed`)는 커밋 **대상**이다 — 장부 자신이 board-git 커밋에 실리므로,
        표시를 커밋 뒤에 쓰면 board-git 이 그 값을 영영 못 담고 다음 실행마다 그 한 줄을
        커밋하려 든다(재실행 무부작용이 깨진다). 그래서 순서는 표시 → 두 커밋이고, **어느
        커밋이든 실패하면 표시를 되돌린다** — 디스크에 남는 `closed` 는 일곱 단계가 전부
        관측된 뒤에만 존재한다.
        """
        paths = self._board_git_paths()
        previous = self._ledger() or {}
        prior = (previous.get(_CLOSE_STEP_KEY), previous.get("status"))
        self._record_progress(self.STEPS[-1][0])
        try:
            block = self._sync_board_git(paths)
            if block is None:
                block = self._commit_pm_home()
        except _CloseObservationFailure:
            # 관측 실패도 종결 표시의 근거가 아니다 — 되돌리고 그대로 정지한다.
            self._restore_progress(*prior)
            raise
        if block is not None:
            self._restore_progress(*prior)
            return block
        return None

    def _board_git_paths(self) -> list[Path]:
        """board-git 에 기록할 경로 — 묶음 장부 + 멤버 티켓 명세 (해소 실패는 정지).

        여기서 하나라도 못 찾으면 이번 종결이 무엇을 기록해야 하는지 모르는 것이다 — 빈 목록
        으로 접으면 아무것도 기록하지 않은 실행이 성공으로 끝난다."""
        board = self._board
        paths: list[Path] = []
        ledger_path_fn = getattr(board, "cluster_ledger_path", None) if board else None
        if ledger_path_fn is not None and self._ledger() is not None:
            paths.append(ledger_path_fn(self._cluster))
        find = getattr(board, "find_ticket_exact", None) if board else None
        if find is None:
            raise _CloseObservationFailure(
                f"티켓 파일 조회 seam 을 로드하지 못했다 ({self._board_py}) — 무엇을 기록할지 "
                "관측할 수 없다. board 사본을 확인하라.")
        for tid in self._tickets:
            try:
                found = find(tid)
            except Exception as exc:  # noqa: BLE001 — 조회 실패는 정지.
                if _is_engine_rev_skew(exc):
                    raise
                raise _CloseObservationFailure(
                    f"{tid} 명세 경로를 관측하지 못했다 — "
                    f"{type(exc).__name__}: {exc}") from exc
            if found is None:
                raise _CloseObservationFailure(
                    f"{tid} 명세 파일을 board 에서 찾지 못했다 — 기록 대상이 확정되지 않는다.")
            paths.append(found[1])
        if not paths:
            raise _CloseObservationFailure(
                f"{self._cluster} 기록 대상 경로가 하나도 없다 — 멤버 해소를 확인하라.")
        return paths

    def _sync_board_git(self, paths: Sequence[Path]) -> str | None:
        """이번 종결이 만진 board 경로를 board-git 에 기록한다(장부·티켓 명세).

        seam 부재는 정지다(기록 채널이 없다는 사실을 성공으로 만들지 않는다). `False` 는
        **로컬 커밋이 안 생겼다**는 board 의 확정 보고라 그대로 차단이다 — 그 상태로 포인터를
        커밋하면 board 에 없는 내용을 가리키는 포인터가 남는다(원격 push 실패는 `True` 로
        보고되므로 offline 은 여기서 막히지 않는다).
        """
        board = self._board
        sync = getattr(board, "_board_git_sync_best_effort", None) if board else None
        if sync is None:
            raise _CloseObservationFailure(
                f"board-git 기록 seam 을 로드하지 못했다 ({self._board_py}) — 기록 여부를 "
                "관측할 수 없다. board 사본을 확인하라.")
        unit, title = self._unit()
        message = _CLOSE_BOARD_MESSAGE.format(unit=unit, title=title)
        if self._dry_run:
            print(f"  [dry-run] board-git 기록: {message}")
            return None
        if not sync(message, list(paths)):
            return ("board-git 로컬 커밋이 생기지 않았다 — 위 board 진단의 미커밋 경로를 "
                    "해소한 뒤 다시 실행하라(포인터는 아직 커밋하지 않았다).")
        print(f"  ✓ board-git 기록 ({message})")
        return None

    def _board_pointer_path(self) -> str | None:
        """PM 홈이 board 를 서브모듈로 가리키는가 — 아니면 None(포인터 커밋 무대상).

        서브모듈 포인터는 상위 `git status` 가 보여주지 않는 형상(`ignore = all`)이라 사람이
        놓치는 자리다 — 그래서 종결이 명시적으로 add 한다. 서브모듈이 아니면 board 파일은
        홈 계획이 이미 덮는다.

        빈 출력은 **관측 성공**(그 경로가 인덱스에 없다 = 서브모듈 아님)이고, 조회 실패는
        판정 불능이라 정지다 — 포인터가 있는데 못 읽은 것을 '무대상' 으로 접으면 실측된 잔여
        (포인터 커밋 누락)가 그대로 재발한다.
        """
        try:
            rc, out = self._git_stdout(
                REPO, ["ls-files", "--stage", "--", _CLOSE_BOARD_POINTER_PATH])
        except Exception as exc:  # noqa: BLE001 — 판정 실패는 정지.
            if _is_engine_rev_skew(exc):
                raise
            raise _CloseObservationFailure(
                f"board 포인터를 관측하지 못했다 ({REPO}) — "
                f"{type(exc).__name__}: {exc}") from exc
        if rc != 0:
            raise _CloseObservationFailure(
                f"board 포인터를 관측하지 못했다 ({REPO} · rc={rc}): {out.rstrip()}")
        if not out.strip():
            return None
        return (_CLOSE_BOARD_POINTER_PATH
                if out.split(maxsplit=1)[0] == _CLOSE_SUBMODULE_MODE else None)

    def _commit_pm_home(self) -> str | None:
        """PM 홈 커밋 — 이 실행이 쓴 홈 산출물 + board 서브모듈 포인터."""
        pathspec: list[str] = []
        for tid in self._tickets:
            home, _code = self._plans(tid)
            if home is None:
                continue
            for path in home.scope.pathspec:
                if path not in pathspec:
                    pathspec.append(path)
        pointer = self._board_pointer_path()
        if pointer is not None and pointer not in pathspec:
            pathspec.append(pointer)
        if not pathspec:
            print("  PM 홈에 커밋할 경로 없음")
            return None
        unit, title = self._unit()
        message = _CLOSE_BOARD_MESSAGE.format(unit=unit, title=title)
        if self._dry_run:
            print(f"  [dry-run] cwd={REPO} git add -- {pointer or ''}")
            print(f"  [dry-run] cwd={REPO} git commit -m \"{message}\" "
                  f"-- {' '.join(pathspec)}")
            return None
        if pointer is not None:
            rc, out = self._git(REPO, ["add", "--", pointer])
            if rc != 0:
                return f"board 포인터 stage 실패 (rc={rc}): {out.rstrip()}"
        if not self._pending_paths(REPO, pathspec):
            print("  PM 홈에 커밋할 변경 없음 — 건너뜀")
            return None
        rc, out = self._git(REPO, ["commit", "-m", message, "--", *pathspec])
        if rc != 0:
            return f"PM 홈 커밋 실패 (rc={rc}): {out.rstrip()}"
        print(f"  ✓ PM 홈 커밋 ({message})")
        return None

    # ── 진행 기록 ─────────────────────────────────────────────────────

    def _record_progress(self, step_key: str) -> None:
        """장부에 단계 진행을 남긴다 — 관측을 대신하지 않는 보조 기록(실패는 비차단)."""
        status = (_CLUSTER_STATUS_CLOSED if step_key == self.STEPS[-1][0]
                  else _CLUSTER_STATUS_CLOSING)
        self._write_progress(step_key, status)

    def _restore_progress(self, step_key: str | None, status: str | None) -> None:
        """마지막 단계가 실패했을 때 진행 기록을 그 실행 **직전 값**으로 되돌린다.

        종결 표시는 커밋에 실어야 해서 커밋보다 먼저 쓰지만, 커밋이 실패하면 그 표시는 근거가
        없다 — 디스크에 남는 `closed` 는 일곱 단계 전부가 관측된 경우로 제한한다."""
        self._write_progress(step_key, status)

    def _write_progress(self, step_key: str | None, status: str | None) -> None:
        """장부의 진행 기록 두 값을 쓴다 (기록 실패는 비차단 경고 1줄)."""
        if self._dry_run:
            return
        board = self._board
        load = getattr(board, "load_cluster", None) if board else None
        dump = getattr(board, "dump_cluster", None) if board else None
        if load is None or dump is None:
            return
        lock = getattr(board, "board_lock", None)
        try:
            with lock() if callable(lock) else contextlib.nullcontext():
                ledger = load(self._cluster)
                if ledger is None:
                    return
                if step_key is None:
                    ledger.pop(_CLOSE_STEP_KEY, None)
                else:
                    ledger[_CLOSE_STEP_KEY] = step_key
                statuses = getattr(board, "CLUSTER_STATUSES", ())
                if status is None or status in statuses:
                    # 열거의 소유자는 board 다 — 그 열거 안에 있을 때만 쓴다. None 은 되돌리기
                    # 전 값이 비어 있던 형상이라 그대로 되돌린다.
                    ledger["status"] = status
                dump(ledger)
        except Exception as exc:  # noqa: BLE001 — 기록 실패가 종결을 되돌리지 않는다.
            if _is_engine_rev_skew(exc):
                raise
            print(f"  ⚠ 장부 진행 기록 실패 — {self._cluster}: {exc}", file=sys.stderr)

    # ── 실행 ──────────────────────────────────────────────────────────

    def _resolve_members(self) -> str | None:
        """멤버 티켓 해소 — 묶음을 못 찾으면 사유(첫 부작용 앞에서 거부)."""
        members_fn = getattr(self._board, "cluster_members", None) if self._board else None
        if members_fn is None:
            return (f"묶음 장부 seam 을 로드하지 못했다 ({self._board_py}) — "
                    "board 사본을 확인하라.")
        self._tickets = tuple(members_fn(self._cluster))
        if not self._tickets:
            return (f"묶음을 찾지 못했다: {self._cluster} — 멤버 조회는 "
                    "`board.py cluster show <이름>` 이다.")
        return None

    def _open_all_done_block(self) -> str | None:
        """정상 종결의 첫 부작용 앞 read-only 상태 게이트 — open + 전원 done 은 복구 전용이다.

        `open` 장부의 멤버가 전부 done 이면 그 완료 기록은 이 파이프라인이 남긴 것이 아니다
        (1단계가 성공하는 순간 장부는 `closing` 이 된다). 그 상태로 7단계를 시작하면 무엇이
        이미 통합됐는지 모르는 채 커밋·재배치·머지·반납으로 들어간다 — 그래서 여기서 멈추고
        증거를 명시하는 복구 문으로 보낸다. `closing` 재개와 `closed` 멱등 관측은 그대로다.
        """
        ledger = self._ledger()
        if ledger is None:
            return None
        if str(ledger.get("status") or "").strip() != _CLUSTER_STATUS_OPEN:
            return None
        if any(self._ticket_status(tid) != "done" for tid in self._tickets):
            return None
        return _CLOSE_OPEN_ALL_DONE.format(
            cluster=self._cluster, members=", ".join(self._tickets))

    # ── all-done recovery ───────────────────────────────────────────

    @staticmethod
    def _revision_map(values: Sequence[str], label: str) -> tuple[dict[str, str], str | None]:
        """``T-NNNN=<rev>`` 반복 인자를 중복 없이 읽는다."""
        mapped: dict[str, str] = {}
        for value in values:
            ticket, separator, revision = str(value).partition("=")
            ticket, revision = ticket.strip(), revision.strip()
            if not separator or not ticket or not revision:
                return {}, f"{label} 형식은 T-NNNN=<git-rev> 이다: {value!r}"
            if ticket in mapped:
                return {}, f"{label}에 {ticket} mapping이 중복됐다"
            mapped[ticket] = revision
        return mapped, None

    def _cluster_id(self, value: str | None) -> str:
        """이름/장부 id 어느 표기든 board 의 장부 id 하나로 정규화한다 (빈 값은 빈 값).

        정상 7단계는 `load_cluster`·`cluster_members` 안에서 이 정규화를 이미 받는다. recovery
        만 원시 문자열을 비교하면 같은 표기로 부른 호출이 "장부 id 가 다르다"로 멈춘다.
        """
        text = str(value or "").strip()
        if not text:
            return ""
        normalize = getattr(self._board, "cluster_id_for_name", None) if self._board else None
        if normalize is None:
            raise _CloseObservationFailure(
                f"cluster id 정규화 seam 을 로드하지 못했다 ({self._board_py})")
        return str(normalize(text)).strip()

    def _commit_oid(self, tree: Path, revision: str, *, label: str) -> tuple[str | None, str | None]:
        """명시 hex revision을 선택 제품 git의 full commit OID로 정규화한다."""
        if re.fullmatch(r"[0-9A-Fa-f]{7,64}", revision) is None:
            return None, f"{label}는 ref/branch가 아닌 hex commit revision이어야 한다: {revision!r}"
        rc, out = self._git_stdout(
            tree, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
        oid = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
        if rc != 0 or re.fullmatch(r"[0-9a-fA-F]{40,64}", oid) is None:
            return None, f"{label}를 선택 제품 git에서 commit으로 해소하지 못했다: {revision}"
        return oid.lower(), None

    def _reconcile_dirty_paths(self, tree: Path) -> tuple[str, ...]:
        """Recovery용 read-only status — optional index refresh도 금지한다."""
        rc, out = self._git_stdout(
            tree,
            ["--no-optional-locks", "status", "--porcelain", "-z",
             "--untracked-files=all"],
        )
        if rc != 0:
            raise _CloseObservationFailure(f"작업 트리 상태를 read-only 관측하지 못했다 ({tree})")
        parse = getattr(self._board, "git_parse_porcelain_z", None) if self._board else None
        entries = (parse(out) if parse is not None else tuple(
            (token[:2], token[3:]) for token in out.split("\0") if len(token) >= 4
        ))
        return tuple(path for _code, path in entries)

    def _reconcile_ticket_frontmatter(self, tid: str) -> tuple[dict | None, str | None]:
        find = getattr(self._board, "find_ticket_exact", None) if self._board else None
        load = getattr(self._board, "load_ticket", None) if self._board else None
        if find is None or load is None:
            return None, f"{tid} 티켓 조회 seam이 없다 ({self._board_py})"
        try:
            found = find(tid)
            if found is None:
                return None, f"{tid} 티켓을 찾지 못했다"
            status, path = found
            fm, _body = load(path)
        except Exception as exc:  # noqa: BLE001 — recovery 판정 불능은 fail-closed.
            # 동적 board 사본의 marked skew는 일반 판독 실패 문구로 접으면 사본 불일치를 숨긴다.
            # 따라서 그것만 전파하고, 나머지 조회 실패만 값으로 바꿔 첫 write 전 정지한다.
            if _is_engine_rev_skew(exc):
                raise
            return None, f"{tid} 티켓을 읽지 못했다: {type(exc).__name__}: {exc}"
        if status != "done" or str(fm.get("status") or "") != "done":
            return None, f"{tid}가 all-done 상태가 아니다 (directory={status}, frontmatter={fm.get('status')})"
        return fm, None

    def _validate_reconcile(self) -> tuple[dict | None, str | None, str | None]:
        """Recovery의 전 입력과 제품 graph를 읽는다 — write는 절대 하지 않는다.

        반환은 ``(canonical closure, ledger status, problem)``이다. 호출자가 이 스냅샷을
        bookend로 두 번 대조한 뒤에만 장부를 쓴다.
        """
        if not self._explicit_slot or not self._slot or self._task is not None:
            return None, None, "recovery는 명시 --repo와 --slot 둘 다 필요하고 --task를 받지 않는다"
        cluster_id = self._cluster_id(self._cluster)
        if self._cluster_id(self._user_ack) != cluster_id:
            return None, None, (f"--user-ack은 대상 cluster와 정확히 같아야 한다: "
                                f"expected {self._cluster!r}, got {self._user_ack!r}")
        tree = self._code_tree()
        try:
            if tree.resolve() == REPO.resolve():
                return None, None, f"제품 git이 PM 홈으로 강등됐다: {tree}"
        except OSError as exc:
            return None, None, f"선택 제품 경로를 해소하지 못했다: {exc}"
        if not (tree / ".git").is_file():
            return None, None, f"선택 경로가 linked product worktree가 아니다: {tree}"
        dirty = self._reconcile_dirty_paths(tree)
        if dirty:
            return None, None, (f"선택 슬롯이 dirty라 recovery를 거부한다 ({tree} · "
                                f"{len(dirty)}건: {', '.join(dirty[:5])})")

        ledger = self._ledger()
        if ledger is None:
            return None, None, f"묶음 장부를 찾지 못했다: {self._cluster}"
        ledger_id = self._cluster_id(ledger.get("id"))
        if ledger_id != cluster_id:
            return None, None, f"장부 id가 대상과 다르다: {ledger_id!r} != {cluster_id!r}"
        members_fn = getattr(self._board, "cluster_tickets", None) if self._board else None
        strict_catalog_fn = (
            getattr(self._board, "all_clusters_strict", None) if self._board else None)
        board_root_fn = getattr(self._board, "board_root", None) if self._board else None
        if members_fn is None or strict_catalog_fn is None or board_root_fn is None:
            return None, None, "cluster 멤버십 전수 검증 seam이 없다"
        board_root = Path(board_root_fn())
        if not (board_root / ".git").exists():
            return None, None, f"recovery는 분리 board-git이 필요하다: {board_root}"
        members = tuple(members_fn(ledger))
        if not members or len(members) != len(set(members)) or members != self._tickets:
            return None, None, f"cluster 멤버 집합이 비었거나 중복·drift했다: {members}"

        evidence, problem = self._revision_map(
            self._integrated_revision_args, "--integrated-rev")
        if problem:
            return None, None, problem
        legacy, problem = self._revision_map(
            self._legacy_base_revision_args, "--legacy-base-rev")
        if problem:
            return None, None, problem
        member_set = set(members)
        if set(evidence) != member_set:
            missing = sorted(member_set - set(evidence))
            extra = sorted(set(evidence) - member_set)
            return None, None, f"evidence mapping 전수성 위반 (누락={missing}, 여분={extra})"

        head, problem = self._commit_oid(tree, self._head_revision(tree), label="제품 exact HEAD")
        if problem:
            return None, None, problem
        assert head is not None
        normalized_revisions: dict[str, str] = {}
        member_anchors: dict[str, dict[str, str]] = {}
        # 귀속 유일성은 **전 catalog 를 읽었다는 증명** 위에서만 성립한다 — 판독 불능 장부가
        # 하나라도 있으면 그 파일이 중복 귀속일 가능성을 배제하지 못하므로 첫 write 전에 정지한다.
        ledgers, catalog_problem = strict_catalog_fn()
        if catalog_problem is not None:
            return None, None, catalog_problem
        for tid in members:
            fm, problem = self._reconcile_ticket_frontmatter(tid)
            if problem:
                return None, None, problem
            assert fm is not None
            declared = self._cluster_id(fm.get("cluster"))
            owners = [
                self._cluster_id(row.get("id")) for row in ledgers
                if tid in members_fn(row)
            ]
            if declared != cluster_id or owners != [cluster_id]:
                return None, None, (f"{tid} 양방향 cluster 귀속 불일치 "
                                    f"(ticket={declared or '없음'}, ledgers={owners})")
            revision, problem = self._commit_oid(
                tree, evidence[tid], label=f"{tid} evidence")
            if problem:
                return None, None, problem
            assert revision is not None
            claimed = str(fm.get("claimed_rev") or "").strip()
            if claimed:
                if tid in legacy:
                    return None, None, f"{tid}는 claimed_rev가 있어 legacy base를 받을 수 없다"
                anchor, problem = self._commit_oid(
                    tree, claimed, label=f"{tid} claimed_rev anchor")
                source = "claimed_rev"
            else:
                supplied = legacy.get(tid)
                if supplied is None:
                    return None, None, f"{tid}는 claimed_rev가 없어 --legacy-base-rev가 필수다"
                anchor, problem = self._commit_oid(
                    tree, supplied, label=f"{tid} operator legacy anchor")
                source = "operator-supplied"
            if problem:
                return None, None, problem
            assert anchor is not None
            if not self._is_ancestor(tree, anchor, revision):
                return None, None, f"{tid} evidence가 anchor 이후가 아니다: {anchor} !<= {revision}"
            if not self._is_ancestor(tree, revision, head):
                return None, None, f"{tid} evidence가 제품 exact HEAD의 조상이 아니다: {revision} !<= {head}"
            normalized_revisions[tid] = revision
            member_anchors[tid] = {"anchor": anchor, "anchor_source": source}

        expected_legacy = {
            tid for tid in members if member_anchors[tid]["anchor_source"] == "operator-supplied"
        }
        if set(legacy) != expected_legacy:
            return None, None, (f"legacy base mapping 전수성 위반 "
                                f"(필요={sorted(expected_legacy)}, 제공={sorted(legacy)})")
        closure = {
            "mode": "reconciled",
            "integration_head": head,
            "member_revisions": normalized_revisions,
            "member_anchors": member_anchors,
        }
        status = str(ledger.get("status") or "").strip()
        existing = ledger.get("closure")
        if status == "closed":
            if existing != closure:
                return None, status, "closed 장부의 closure evidence가 이번 명시 증거와 다르다"
        elif status == "open":
            if existing not in (None, closure):
                return None, status, "open 장부에 다른 closure evidence가 남아 있다"
        else:
            return None, status, f"recovery는 open all-done 전용이다 (현재 status={status!r})"
        return closure, status, None

    def _head_revision(self, tree: Path) -> str:
        rc, out = self._git_stdout(tree, ["rev-parse", "--verify", "HEAD"])
        if rc != 0 or not out.strip():
            raise _CloseObservationFailure(f"제품 exact HEAD를 읽지 못했다 ({tree})")
        return out.strip().splitlines()[0]

    def _report_other_slots(self) -> None:
        """선택 밖 leased slot은 읽기만 하고 path·branch·dirty를 loud 보고한다."""
        try:
            data = json.loads(
                _load_file_lock().read_text_shared(LEASES_FILE, encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            print(f"  ⚠ 다른 슬롯 상태를 읽지 못했다: {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        rows = data.get("leases", []) if isinstance(data, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("state") != "leased":
                continue
            slot = str(row.get("slot") or "").strip()
            if not slot or slot == self._slot:
                continue
            path = REPO / slot
            rc, out = self._git_stdout(path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
            branch = out.strip() if rc == 0 and out.strip() else "unknown"
            try:
                dirty = self._reconcile_dirty_paths(path)
                dirty_label = f"dirty({len(dirty)})" if dirty else "clean"
            except _CloseObservationFailure:
                dirty_label = "unknown"
            print(f"  · 비선택 슬롯 보존: path={path} branch={branch} state={dirty_label}")

    def _commit_reconcile_pointer(self) -> str | None:
        """Recovery가 허용하는 유일한 PM-home write: board submodule pointer commit."""
        pointer = self._board_pointer_path()
        if pointer is None:
            print("  PM 홈 board pointer 무대상")
            return None
        if not self._pending_paths(REPO, [pointer]):
            print("  PM 홈 board pointer 이미 최신 — 건너뜀")
            return None
        if self._dry_run:
            print(f"  [dry-run] cwd={REPO} git add/commit -- {pointer}")
            return None
        rc, out = self._git(REPO, ["add", "--", pointer])
        if rc != 0:
            return f"board pointer stage 실패 (rc={rc}): {out.rstrip()}"
        unit, title = self._unit()
        message = _CLOSE_BOARD_MESSAGE.format(unit=unit, title=title)
        rc, out = self._git(REPO, ["commit", "-m", message, "--", pointer])
        if rc != 0:
            return f"board pointer commit 실패 (rc={rc}): {out.rstrip()}"
        print(f"  ✓ PM 홈 board pointer 기록 ({message})")
        return None

    def _reconcile_ledger_pending(self) -> bool:
        """Crash 뒤 closed ledger가 board-git에 아직 미기록인지 관측한다."""
        board_root = Path(getattr(self._board, "board_root")())
        ledger_path = Path(getattr(self._board, "cluster_ledger_path")(self._cluster))
        try:
            pathspec = ledger_path.relative_to(board_root).as_posix()
        except ValueError as exc:
            raise _CloseObservationFailure(
                f"cluster ledger가 board-git 밖을 가리킨다: {ledger_path}") from exc
        return bool(self._pending_paths(board_root, [pathspec]))

    def _run_reconcile(self) -> int:
        """이미 통합된 all-done cluster를 git mutation 없이 감사 가능하게 닫는다."""
        try:
            first, first_status, problem = self._validate_reconcile()
        except _CloseObservationFailure as failure:
            first, first_status, problem = None, None, f"관측 실패 — {failure}"
        if problem:
            print(f"\n[중단] reconcile preflight — {problem}", file=sys.stderr)
            return 1
        assert first is not None
        self._report_other_slots()
        try:
            second, second_status, problem = self._validate_reconcile()
        except _CloseObservationFailure as failure:
            second, second_status, problem = None, None, f"관측 실패 — {failure}"
        if problem or second != first or second_status != first_status:
            detail = problem or "제품 HEAD 또는 board 상태가 bookend 사이에 drift했다"
            print(f"\n[중단] reconcile bookend — {detail}", file=sys.stderr)
            return 1
        if self._dry_run:
            print(f"[dry-run] {self._cluster} all-done reconcile: {first}")
            return 0

        prior: dict | None = None
        if first_status == "open":
            lock = getattr(self._board, "board_lock", None) if self._board else None
            load = getattr(self._board, "load_cluster", None) if self._board else None
            dump = getattr(self._board, "dump_cluster", None) if self._board else None
            if load is None or dump is None:
                print("\n[중단] reconcile 장부 persistence seam이 없다", file=sys.stderr)
                return 1
            with lock() if callable(lock) else contextlib.nullcontext():
                third, third_status, problem = self._validate_reconcile()
                if problem or third != first or third_status != "open":
                    detail = problem or "최종 write 직전 board/HEAD가 drift했다"
                    print(f"\n[중단] reconcile final bookend — {detail}", file=sys.stderr)
                    return 1
                prior = dict(load(self._cluster) or {})
                updated = dict(prior)
                updated["closure"] = first
                updated["status"] = _CLUSTER_STATUS_CLOSED
                updated[_CLOSE_STEP_KEY] = self.STEPS[-1][0]
                dump(updated)
            needs_board_sync = True
        else:
            try:
                needs_board_sync = self._reconcile_ledger_pending()
            except _CloseObservationFailure as failure:
                print(f"\n[중단] reconcile board persistence 관측 실패 — {failure}", file=sys.stderr)
                return 1
            if needs_board_sync:
                print("  동일 closure evidence의 미완 board-git 기록을 재개")
            else:
                print("  동일 closure evidence가 이미 board-git에 기록됨 — 장부 write 건너뜀")

        if needs_board_sync:
            ledger_path = getattr(self._board, "cluster_ledger_path")(self._cluster)
            try:
                block = self._sync_board_git([ledger_path])
            except _CloseObservationFailure as failure:
                block = f"board-git 기록 관측 실패 — {failure}"
            if block is not None:
                if prior is not None:
                    with lock() if callable(lock) else contextlib.nullcontext():
                        dump(prior)
                print(f"\n[중단] {block}", file=sys.stderr)
                return 1

        block = self._commit_reconcile_pointer()
        if block is not None:
            print(f"\n[중단] {block} — 같은 증거로 재실행하면 pointer 단계만 재개한다.",
                  file=sys.stderr)
            return 1
        print(f"\n[완료] {self._cluster} all-done reconcile 완료 (제품/slot write 0).")
        return 0

    def preflight(self) -> list[str]:
        """첫 부작용 앞에서 읽기만으로 판정 가능한 차단 사유를 **전건** 모은다.

        단계 선언(`STEPS` 3번째 값)이 대상 여부의 단일 진실이다 — 여기서 단계 목록을 다시
        적지 않는다. 통과한 종결은 **읽기로 알 수 있는 사유로는** 중간에 멈추지 않는다:
        선검사가 통과시켰는데 뒤 단계가 같은 판정 함수로 멈추면 그것이 결함이다.

        `--dry-run` 여부와 무관하게 항상 먼저 돈다.
        """
        labelled: dict[str, str] = {}
        for _key, label, precheck in self.STEPS:
            if precheck is None:
                continue
            for reason in getattr(self, precheck)():
                # 두 단계가 같은 조건을 각자 판정하는 자리가 있다(기준 브랜치 미선언·현재
                # 브랜치 불일치는 재배치와 머지가 둘 다 본다). 그대로 누적하면 목록의 건수가
                # 부풀어 사람이 고칠 것을 두 개로 읽는다 — 사유 **본문**으로 접고 먼저 만난
                # 라벨만 남긴다. `_pre_*` 는 각자 독립 판정 그대로다(단계가 서로를 알지 않는다).
                labelled.setdefault(reason, f"[{label}] {reason}")
        return list(labelled.values())

    def run(self) -> int:
        """종결 파이프라인 실행 — 0=성공, 1=실패(그 단계에서 정지).

        단계는 두 가지로 멈춘다: **차단 사유**(관측은 됐고 조건이 안 맞는다)와 **관측 실패**
        (판정 자체가 안 된다). 후자를 통과시키면 그 단계가 끝났다는 증거가 없는 채로 종결이
        진행되므로, 같은 자리에서 같은 rc 로 멈춘다."""
        try:
            block = self._resolve_members()
            if not block and not self._reconcile_integrated:
                block = self._open_all_done_block()
        except _CloseObservationFailure as failure:
            print(f"\n[중단] 관측 실패 — {failure}", file=sys.stderr)
            return 1
        if block:
            print(f"\n[중단] {block}", file=sys.stderr)
            return 1
        if self._reconcile_integrated:
            return self._run_reconcile()
        print(f"[close] {self._cluster} 종결 시작 "
              f"(dry_run={self._dry_run}, 멤버 {len(self._tickets)}: "
              f"{', '.join(self._tickets)})")
        try:
            reasons = self.preflight()
        except _CloseObservationFailure as failure:
            print(f"\n[중단] 관측 실패 — {failure}", file=sys.stderr)
            return 1
        if reasons:
            print(f"\n[중단] 종결 선검사 — 막는 조건 {len(reasons)}건. 아직 아무 부작용도 "
                  "내지 않았다. 전부 해소한 뒤 다시 실행하라:", file=sys.stderr)
            for index, reason in enumerate(reasons, start=1):
                print(f"  {index}. {reason}", file=sys.stderr)
            return 1
        total = len(self.STEPS)
        for index, (key, label, _precheck) in enumerate(self.STEPS, start=1):
            print(f"\n[{index}/{total}] {label}...")
            try:
                block = getattr(self, f"_step_{key}")()
            except _CloseObservationFailure as failure:
                block = f"관측 실패 — {failure}"
            if block:
                print(f"\n[중단] {block}", file=sys.stderr)
                print(f"  · 종결은 [{index}/{total}] 에서 멈췄다 — 다시 실행하면 끝난 "
                      "단계는 건너뛰고 이 단계부터 이어간다.", file=sys.stderr)
                return 1
            self._record_progress(key)
        if self._dry_run:
            print(f"\n[dry-run] {self._cluster} 종결 계획만 출력했다.")
        else:
            print(f"\n[완료] {self._cluster} 종결 완료.")
        return 0


# ── CLI ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket_finish.py",
        description="ticket 완료 시 PM 기록 자동화 헬퍼 (v1 축소판).",
    )
    parser.add_argument("ticket_id", metavar="T-NNNN", nargs="?", default=None,
                        help="완료 기록할 ticket ID (묶음 종결은 `--cluster`)")
    parser.add_argument(
        "--cluster",
        metavar="C-<이름>",
        default=None,
        help="묶음 종결(close) — 기계 확인·게이트 처분·티켓별 완료 기록·커밋·재배치·머지·"
             "슬롯 반납·board 기록을 고정 순서로 실행한다. 실패 지점에서 멈추고, 다시 "
             "실행하면 끝난 단계는 건너뛴다(재실행이 곧 재개).",
    )
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
    parser.add_argument(
        "--reconcile-integrated", action="store_true",
        help="이미 all-done인 open cluster를 제품 git evidence 검증 뒤 장부만 복구한다.",
    )
    parser.add_argument(
        "--integrated-rev", action="append", default=[], metavar="T-NNNN=<git-rev>",
        help="reconcile 멤버별 통합 evidence commit (멤버마다 정확히 1개).",
    )
    parser.add_argument(
        "--legacy-base-rev", action="append", default=[], metavar="T-NNNN=<git-rev>",
        help="claimed_rev 없는 legacy 멤버의 명시 anchor commit.",
    )
    parser.add_argument(
        "--user-ack", default=None, metavar="C-<이름>",
        help="reconcile 대상 cluster ID와 정확히 같은 사용자 승인값.",
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
             "별도·board complete 는 --tests-pass 유지). 회귀 실행만 skip — 코드 트리 해소·"
             "diff 측정·stage 는 그대로다.",
    )
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # PM-홈 worktree 오실행 가드— 기록의 어떤 부작용(회귀/log append/complete/stage)도
    # 나기 *전에* fail-loud. 읽기 경로 없음(ticket_finish 는 전부 쓰기 기록)이라 진입에서 한 번.
    if _guard_worktree_misanchor():
        return 1

    # 완료 기록 단위는 하나다 — 티켓 하나(현행)이거나 묶음 하나(종결)다. 둘 다 주면 어느
    # 단위로 도는지가 인자에 두 번 적힌 것이고, 둘 다 없으면 대상이 없다.
    if (args.ticket_id is None) == (args.cluster is None):
        parser.error(
            "완료 기록 대상은 하나다 — ticket ID 를 주거나 `--cluster <이름>` 으로 묶음을 "
            "종결하라(둘 다 지정하거나 둘 다 생략할 수 없다)."
        )

    recovery_args_present = bool(
        args.reconcile_integrated or args.integrated_rev or args.legacy_base_rev
        or args.user_ack is not None
    )
    if recovery_args_present and not args.reconcile_integrated:
        parser.error("--integrated-rev/--legacy-base-rev/--user-ack은 --reconcile-integrated 전용이다.")
    if args.reconcile_integrated:
        if args.cluster is None:
            parser.error("--reconcile-integrated는 --cluster <이름> 전용이다.")
        if args.task is not None or args.repo is None or args.slot is None:
            parser.error(
                "--reconcile-integrated는 --repo <repo> --slot <N> 둘 다 필수이고 --task를 받지 않는다."
            )

    # task 실행-위치 핀은 독립 축이다. 혼합을 parse_identity의 bare-slot 오류보다
    # 먼저 거부해 `--repo`를 추가하라는 잘못된 해결책을 안내하지 않는다.
    if args.task is not None and (args.repo is not None or args.slot is not None):
        parser.error(
            "--task 는 독립 정체성이다 — --repo/--slot 과 함께 쓸 수 없다 "
            "(task 보유 작업공간은 장부에서 자동 해소)."
        )

    # 정체성 인자 *검증*(`--slot` 단독·`slot < 1` = uniform fail-loud)은 `--no-pytest` 와
    # 무관하게 **항상** 수행한다. task도 회귀 전용 값이 아니다: stage/status 계획의 cwd 단일
    # 진실이므로 --no-pytest 에서도 반드시 해소한다. slot-mode 코드 트리 해소·모호 게이트도
    # 같다 — 회귀 skip 여부와 독립이다(아래 비-task 경로 주석).
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
    # 이번 실행이 쓰는 슬롯 키(`work/<repo>_<N>`) — 종결의 반납 대상이다. 두 해소 갈래
    # (task 작업공간 · `--repo`/`--slot`)가 같은 값을 이 변수로 내보낸다.
    close_slot: str | None = None
    # board 하위 호출(complete)에 실을 정체성 인자. 명시 인자가 1순위이고, `--repo` 단독처럼
    # 좌표가 덜 찬 경우에만 장부가 해소한 슬롯 좌표로 채운다(아래 slot 분기).
    board_identity: list[str] = []
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
        # task-mode 귀속은 `<user>/<task>` 다 — board 도 같은 축으로 소유를 대조해야 한다.
        board_identity = ["--task", identity.task]
        # 비-task 경로와 동형으로 `--no-pytest` 와 무관하게 해소한다 — 결과 트리는 같지만
        # (`_code_tree` 가 task 작업공간을 먼저 본다) 해소가 stale 슬롯(장부에는 있고 디스크에는
        # 없음) 존재검사를 태워 경고 없이 사라지던 갈래를 없앤다.
        regression_cwd = _regression_cwd(ws.slot)
        close_slot = ws.slot
    else:
        # 코드 트리(=회귀 cwd) 해소는 `--no-pytest` 와 **무관하게** 항상 수행한다. 해소 결과는
        # 회귀 실행 전용 값이 아니다 — diff 서킷브레이커(`_code_tree()`)·[4/5] stage·PM-direct
        # 재검이 같은 트리를 소비한다. 회귀 skip 이 해소까지 우회하면 그 소비자들이 PM 홈(분리
        # 형상에선 엔진 import 사본)을 잰다: 실측에서 dirty 한 PM 홈 import 사본이 small 상한
        # 300 줄 티켓을 2832 줄로 false-block 했고, 같은 호출이 PM 홈의 import 사본을 stage 했다.
        # `--no-pytest` 는 [1/5] 회귀 실행만 건너뛸 뿐 트리 해소의 우회 수단이 아니다 — 모호는
        # `--repo/--slot` 또는 `--task` 명시로만 푼다(기계 판정).
        worktree_slot, ambiguity = _resolve_finish_slot(identity.repo, identity.slot)
        if ambiguity is not None:
            print(f"\n[중단] 코드 트리(회귀 cwd) 해소 모호 — {ambiguity}", file=sys.stderr)
            print(
                "  → `--repo <name> [--slot <N>]`(예: --repo project_manager --slot 1) 으로 슬롯을 "
                "명시하거나, `--task <이름>` 으로 task 작업공간을 쓰라 — 코드 트리는 회귀뿐 아니라 "
                "diff 측정·stage 가 함께 쓰므로 회귀 skip 으로는 풀리지 않는다.",
                file=sys.stderr,
            )
            return 1
        # 해소된 슬롯이 있으면 그 worktree 를 코드 트리(회귀 cwd)로 명시 forward(_regression_cwd 위임).
        # 미해소(None)면 미주입 → 런타임 `_regression_cwd()` 폴백(현행 100% 보존).
        if worktree_slot:
            regression_cwd = _regression_cwd(worktree_slot)
            close_slot = worktree_slot
            # 귀속 세션은 **장부 행이 그 슬롯에 준 정체성**이다 — 경로 basename 을 정체성으로
            # 읽으면 경로에 이름이 없는 슬롯(PM 홈 자신을 가리키는 행 `slot="."`)은 `.` 로,
            # 장부가 다른 session 을 들고 있는 슬롯은 갈린 값으로 귀속된다(리뷰 F-001).
            # 미해소(장부에도 없고 경로도 슬롯 키가 아님)면 명시 `--repo/--slot` 이 준 값을
            # 유지한다(`identity.session` — 종전 폴백 관례).
            resolved = identity_args.resolve_slot_identity(worktree_slot, LEASES_FILE)
            if resolved is not None:
                session = resolved.key
            board_identity = _board_identity_argv(identity, resolved)

    finisher = TicketFinisher(regression_cwd=regression_cwd, task_workspace=task_workspace)
    # 완료 기록 단위는 **묶음 하나**다(특례 없음) — 티켓 하나를 준 호출도 그 티켓의 묶음으로
    # 해소해 같은 파이프라인으로 보낸다. 묶음을 선언하지 않은 티켓은 board 가 크기 1 로 접어
    # 주고(마이그레이션 0), 장부가 통합 브랜치를 선언하지 않았으면 판정 기준이 없어 멈춘다.
    # 티켓용 별도 코드 경로를 남기면 그 경로에서만 장부 기록·반납·포인터 커밋이 빠진다.
    cluster = args.cluster
    if cluster is None:
        cluster, _ledger = ticket_cluster_ledger(BOARD_PY, args.ticket_id)
        if not cluster:
            print(
                f"\n[중단] {args.ticket_id} 의 묶음을 해소하지 못했다 ({BOARD_PY}) — 완료 "
                "기록 단위는 묶음이다(티켓 하나면 크기 1). board 사본을 확인하라.",
                file=sys.stderr,
            )
            return 1
    return ClusterCloser(
        cluster,
        finisher=finisher,
        board_py=BOARD_PY,
        slot=close_slot,
        task=identity.task,
        session=session,
        board_identity_args=board_identity,
        dry_run=args.dry_run,
        skip_pytest=args.no_pytest,
        reconcile_integrated=args.reconcile_integrated,
        integrated_revisions=args.integrated_rev,
        legacy_base_revisions=args.legacy_base_rev,
        user_ack=args.user_ack,
        explicit_slot=(identity.kind == "slot"),
    ).run()


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
