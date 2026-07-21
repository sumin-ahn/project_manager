#!/usr/bin/env python3
"""PM 부기 자동화 헬퍼 — ticket 완료 시 기계적 부기를 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/ticket_finish.py T-NNNN [--section "<섹션명>"] [--dry-run]

동작 순서 (하나라도 실패하면 이후 단계 중단):
  1. 회귀 실행 — pytest tests/ -q. red 면 즉시 중단.
  2. log/current.md 스켈레톤 append — 표준 형식 entry 골격.
  3. board.py complete 호출 — 회귀를 이미 통과했으므로 --tests-pass.
  4. git add -A — 스테이징. commit 은 PM 이 한다.
  5. 잔여 PM 수동 작업 출력.

결정 (T-0064 / T-0103):
  - subprocess DI: pytest/git/board.py subprocess 는 주입 가능한 함수로 감싼다.
  - red 면 중단: log/current.md / board / git 어떤 것도 건드리지 않는다.
  - status.md 는 더 이상 건드리지 않는다 (ADR-0023 a안 — status.md = judgment-only).
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
from pathlib import Path
from typing import Callable

# ── REPO 앵커 (상향 탐색·board_root() graceful 탐지 동형·ADR-0033 ①) ──────────
# 하드코딩 `parents[2]` 는 tools 가 `<root>/.project_manager/tools/` 정확히 2단 깊이에 있다고
# 가정한다 — 채택자 형상(PM 홈/worktree 구조 상이·다른 깊이)에선 어긋난다(finance_dev 제보 D2).
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
LEASES_FILE = REPO / ".project_manager" / ".local" / "worktree-leases.json"  # 리스 장부 (ADR-0013·F6 task 해소 read-only·T-0355)
TOOLS_DIR = REPO / ".project_manager" / "tools"  # pm_handoff 동적 로드 앵커 (회귀 cwd 해소·T-0149)
# areas.md 경로는 상수로 굳히지 않는다(T-0162 A6) — board/ 분리(ADR-0033 ①) 시 board/ 안으로
# 옮겨가므로, `_resolve_per_repo_test_cmd` 가 board 모듈의 `areas_file()`(board_root 추종)에 위임.

# ── identity_args sibling 로드 (ADR-0057·T-0322 공용 정체성 모듈) ──────────────
# `--repo`/`--slot` 파싱을 공용 모듈 identity_args 에서 가져온다. 스크립트-위치 앵커
# (`Path(__file__).resolve().parent`=tools/)에서 `spec_from_file_location` 으로 동적 로드해
# sys.path 를 오염시키지 않는다 (board.py `_load_identity_args`·pm_handoff 와 동형). 스크립트
# 직접 실행(sys.path[0]=tools/)이든 테스트 로드(spec_from_file_location·패키지 아님이라 sys.path
# 미충전)든 어느 쪽이든 `Path(__file__).resolve().parent` 가 정확히 tools/ 라 동일하게 동작한다
# (T-0322 결정·pm_handoff 동일 관용구).

# ── 엔진 사본 rev 스탬프 (T-0397·형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.4.1"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다 (T-0397·fail-loud·skew→명시 에러).

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
        err._engine_rev_skew = True  # T-0397 — fail-soft 로더가 재-raise 식별
        raise err


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지 (T-0397).

    fail-soft sibling 로더의 `except Exception` 은 로드 실패/부재만 None 으로 흡수하고, 이
    판정이 True 인 예외(중첩 로드에서 검출된 형제 skew)는 재-raise 해 fail-loud 를 보존한다
    (예: 신 ticket_finish→신 pm_handoff→구 identity_args 검출이 None 강등되지 않게)."""
    return getattr(exc, "_engine_rev_skew", False)


def _load_identity_args():
    """공용 정체성 인자 모듈(identity_args.py)을 같은 tools/ 에서 경로 로드한다 (board.py
    `_load_identity_args`·pm_handoff 동형·sys.path 무오염). `--repo/--slot` 정체성 파싱에
    load-bearing 이라 로드 실패는 엔진 손상 — 예외를 그대로 낸다(fail-loud·board.py 동일)."""
    import importlib.util

    ia_path = Path(__file__).resolve().parent / "identity_args.py"
    spec = importlib.util.spec_from_file_location("identity_args", ia_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _verify_engine_rev(mod, "identity_args.py")  # T-0397 — 사본 skew fail-loud
    return mod


identity_args = _load_identity_args()


# ── 회귀 cwd 자동해소 (self-host·T-0149 — pm_handoff `_regression_cwd` 재사용·DRY) ────
# 분리된 PM 홈(②·ADR-0027)엔 `tests/` 가 없으므로, ② 홈 cwd 에서 ticket_finish 를 돌리면
# 회귀가 활성 worktree 슬롯(①·tests/ 보유)에서 돌아야 한다. pm_handoff 가 이미 이 판정을
# 모듈-레벨 `_regression_cwd(worktree_slot, areas_file, leases_file)` 로 해결했고(T-0124·
# pm_bootstrap `_auto_slot` 동적로드 재사용·self-host 해소 검증됨), board.py·pm_bootstrap 과
# *같은 위치*에 산다 — 복붙 대신 동적 로드해 그 함수를 그대로 위임한다(`_auto_slot` 복제 0).

def _load_pm_handoff():
    """pm_handoff 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    `_load_board_module`·`_load_domain_module` 과 동형 — `spec_from_file_location`
    (스크립트-위치 앵커). 부재/실패는 None 이고 호출부가 `str(REPO)` 로 폴백하므로 무해.
    """
    import importlib.util

    hp_path = TOOLS_DIR / "pm_handoff.py"
    if not hp_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("pm_handoff", hp_path)
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — pm_handoff 가 중첩 로드한 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    _verify_engine_rev(mod, "pm_handoff.py")  # T-0397 — 로드 성공 후 skew 는 fail-loud(try 밖)
    return mod


def _regression_cwd(worktree_slot: str | None = None) -> str:
    """회귀를 실행할 작업 디렉토리를 해소한다 (T-0149 — pm_handoff `_regression_cwd` 위임).

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
        except Exception:  # noqa: BLE001 — fail-soft: 위임 실패는 REPO 폴백.
            pass
    return str(REPO)


# ── --repo/--slot 슬롯 disambiguation (다중슬롯 finish·ADR-0027·pm_handoff 미러·T-0285·ADR-0057) ──
# ADR-0027 다중슬롯 형상(PM 홈 ② + worktree 슬롯 여럿)에선 `_regression_cwd(None)` 의
# `_auto_slot` 자동해소가 모호(슬롯 2+)해져 ② 홈으로 폴백하고, ②엔 `tests/` 가 없어 회귀가
# red("no tests ran")→finish 가 *조용히* 차단된다(라이브 발견·PM 59차). pm_handoff 는 이미
# `--repo`/`--slot`(ADR-0057)로 슬롯을 disambiguate 하는데 ticket_finish 엔 그 수단이 없던 게
# 근본이다. pm_handoff `_resolve_explicit_identity_slot`(명시 identity → M3 조인검증 해소)·
# `_resolve_session_worktree_slot`(session-entry 슬롯 해소·default-1/단독/idle-필터·진짜 모호면
# fail-loud·T-0178)을 동적 로드해 재사용한다(DRY·해소 로직 복제 0). ticket_finish 는 이미
# pm_handoff 동적 로드 seam 보유.

def _resolve_finish_slot(repo: str | None, slot: int | None) -> tuple[str | None, str | None]:
    """`--repo`/`--slot`(또는 둘 다 부재)에서 회귀 worktree 슬롯을 해소한다 — `(worktree_slot, error_msg)`.

    pm_handoff 를 동적 로드해 재사용한다(DRY). 반환:
      - `(work/<repo>_<N>, None)` — `--repo`(+`--slot`) 명시 → pm_handoff
        `_resolve_explicit_identity_slot` 로 **M3**(세션↔repo 조인 검증) 통과 후 결정론적 해소.
      - `(work/<repo>_<N>, None)` — `--repo`/`--slot` 둘 다 부재인데 default-1/단독/idle-필터로
        자동해소됨(no-flag 기본·ADR-0040 불변).
      - `(None, None)` — solo/미해소(멀티-PM 미셋업) → 호출부 REPO 런타임 폴백(현행 100% 보존).
      - `(None, error_msg)` — **진짜 모호**(멀티-PM under-specified·repo≥2·slot1 부재 비단독) 또는
        **M3**(명시 repo/slot 이 리스 장부와 조인 불일치) → 호출부 fail-loud.

    `repo`/`slot` 둘 다 `None`(kind='none')이면 기존 no-flag 자동해소로 위임 — pm_handoff
    부재/로드 실패는 fail-soft `(None, None)`(현행 REPO 폴백·솔로 무변경). `repo`/`slot` 명시인데
    pm_handoff 부재면 M3 검증(리스 조인)을 할 수 없으므로 단순 조립만 해 신뢰한다(현행 폴백 패턴).
    """
    hp = _load_pm_handoff()
    if repo is None and slot is None:
        if hp is None:
            return None, None
        try:
            return hp._resolve_session_worktree_slot(None)
        except Exception:  # noqa: BLE001 — fail-soft: 해소 실패는 현행 폴백(모호 아님).
            return None, None
    if hp is None:
        # pm_handoff 부재 — M3(리스 조인) 검증 불가하니 단순 조립만(현행 폴백 패턴·솔로 무변경).
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
    for line in LOCAL_CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


# ── 회귀 명령 해소 (per-repo·ADR-0014) ──────────────────────────────────
#
# multi-PM(multi-PM) 모델에선 활성 repo 가 비-Python(Go 등)일 수 있어 `pytest tests/ -q`
# 가 틀린다 — 회귀는 **활성 repo 의 per-repo test_cmd**(areas.md 레지스트리)를 써야
# 한다(ADR-0014). board.py 의 `_test_cmd` 가 그 우선순위(override > areas.md 활성 prefix
# 행 > local.conf > 기본)의 단일 진실이므로 import 해 재사용한다.
#
# **솔로/프레임워크 자기 회귀(=현행 `pytest tests/ -q` venv 실행)는 반드시 보존**한다:
# areas.md 없음 / 활성 prefix 없음 / 그 행의 test_cmd 빈 값이면 *multi-PM 오버라이드가
# 아니므로* None 을 돌려, 호출부가 현행 하드코딩 argv 를 그대로 쓰게 한다(board 의
# 솔로 폴백 `pytest -q` 와 달리 venv 인터프리터·`tests/` 경로를 보존 — 도그푸딩 불변).

def _load_board_module():
    """board.py 를 경로 import 해 모듈로 반환한다 (실패 시 None).

    별도 함수로 둔 건 테스트가 areas.md/local.conf 해소를 hermetic 하게 가로채는 seam —
    board 의 areas/local 경로 전역을 tmp 로 재바인딩한 모듈을 주입할 수 있게 한다.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_test_cmd", BOARD_PY)
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _verify_engine_rev(mod, "board.py")  # T-0397 불변식: stamped sibling 로드 지점은 verify
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


# ── PM-홈 worktree 오실행 가드 (T-0345·쓰기-경로 전용) ─────────────────────────
# ticket_finish 를 PM 홈(②)의 등록 worktree cwd 에서 오실행하면 REPO 가 worktree 로 착지해
# stray `wiki/log/current.md` append(+ board.py complete 오실행)를 낸다(PM 71 실측). board.py
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
        "board·git)는 PM 홈이 소유합니다(ADR-0027). 이대로면 이 worktree 에 stray log/티켓을 "
        "잘못 만듭니다.\n"
        f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
        f"  (현재 앵커: {REPO})",
        file=sys.stderr,
    )
    return True


def _resolve_per_repo_test_cmd() -> str | None:
    """multi-PM 모드 활성 repo 의 per-repo test_cmd(문자열)를 해소한다. 솔로면 None.

    board.py 를 import 해 areas.md 레지스트리 해소(`id_prefix`·`_areas_row_for_prefix`)를
    재사용한다 — areas.md 가 있고 활성 prefix 의 행에 비어 있지 않은 `test_cmd` 가 있을
    때만 그 문자열을 반환한다. 그 외(솔로·미등록·빈 값·import 실패)는 None(현행 보존).

    areas.md 존재 가드는 board 의 `areas_file()`(board_root 추종)로 위임한다 — board/ 분리
    (ADR-0033 ①) 시 areas.md 가 board/ 안(submodule)으로 옮겨가므로 legacy 위치(wiki 밖
    `.project_manager/areas.md`)를 보면 stale 이다. board 로드 후 그 함수를 부른다(`id_prefix`
    해소와 동일 루트).
    """
    mod = _load_board_module()
    if mod is None:
        return None
    if not mod.areas_file().exists():
        return None
    try:
        prefix = mod.id_prefix()
        if not prefix:
            return None
        row = mod._areas_row_for_prefix(prefix)
        if row and row.get("test_cmd"):
            return row["test_cmd"]
    except Exception:
        return None
    return None


# ── pytest 출력 파서 ────────────────────────────────────────────────────

def parse_pytest_output(output: str) -> tuple[int, int] | None:
    """pytest -q 출력에서 (passed, deselected) 를 파싱한다.

    반환: (passed, deselected) — 파싱 실패 시 None.

    pytest -q 요약 라인 형식 예:
      "1472 passed, 24 deselected in 12.34s"
      "1472 passed in 12.34s"
      "5 failed, 1467 passed, 24 deselected in 10.00s"

    red (failed > 0) 여부 판단은 호출 측이 한다 (failed 수 포함 파싱은 하지 않음).
    반환값 (passed, deselected) 만 추출한다.
    """
    passed_match = re.search(r"(\d+) passed", output)
    deselected_match = re.search(r"(\d+) deselected", output)

    if passed_match is None:
        return None

    passed = int(passed_match.group(1))
    deselected = int(deselected_match.group(1)) if deselected_match else 0
    return passed, deselected


def is_pytest_green(output: str, returncode: int = 0) -> bool:
    """pytest -q 출력이 green (passed 존재, failed 없음) 이면 True.

    returncode 도 함께 검사한다 — returncode != 0 이면(인터럽트·부분 출력 등)
    명확한 'N passed' 가 있어도 green 으로 오판하지 않는다.
    """
    if returncode != 0:
        return False
    if re.search(r"\d+ failed", output):
        return False
    if re.search(r"\d+ passed", output):
        return True
    return False


# ── board.py 연동 ───────────────────────────────────────────────────────

def count_board_done(board_py: Path) -> int:
    """board.md 의 done 티켓 수를 반환한다 (board.py 를 import 해서).

    board.py 를 직접 import 해 find_ticket / STATUS_DIRS 를 활용한다.
    실패 시 -1 반환.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_helper", board_py)
    if spec is None:
        return -1
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _verify_engine_rev(mod, "board.py")  # T-0397 불변식: stamped sibling 로드 지점은 verify
        # board_root() 추종 — board/ 분리(ADR-0033 ①) 시 ticket 이 board/tickets 로 빠지므로
        # legacy 별칭 상수(mod.TICKETS_DIR)가 아니라 함수를 부른다(분리 후 stale wiki/ 안 봄).
        done_dir = mod.tickets_dir() / "done"
        return len(list(done_dir.glob("T-*.md")))
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — 사본 skew 는 fail-loud(삼키지 않는다).
        return -1


def get_ticket_title(board_py: Path, ticket_id: str) -> str:
    """ticket_id 의 title 을 board.py 를 import 해서 읽어온다.

    실패 시 빈 문자열 반환.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_helper2", board_py)
    if spec is None:
        return ""
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _verify_engine_rev(mod, "board.py")  # T-0397 불변식: stamped sibling 로드 지점은 verify
        _status, path = mod.find_ticket(ticket_id)
        fm, _body = mod.load_ticket(path)
        return fm.get("title") or ""
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — 사본 skew 는 fail-loud(삼키지 않는다).
        return ""


def get_ticket_touches(board_py: Path, ticket_id: str) -> list[str]:
    """ticket_id 의 frontmatter `touches`(파일/디렉토리 경로 목록)를 board.py 로 읽는다.

    문자열 원소만 취한다(비-문자열 오기는 버림). board 미로드·ticket 부재/깨짐 →
    [](graceful·crash 0 — soft 알림은 막지 않는다). domain soft 알림 step 이 쓴다.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_helper3", board_py)
    if spec is None:
        return []
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _verify_engine_rev(mod, "board.py")  # T-0397 불변식: stamped sibling 로드 지점은 verify
        _status, path = mod.find_ticket(ticket_id)
        fm, _body = mod.load_ticket(path)
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — 사본 skew 는 fail-loud(삼키지 않는다).
        return []
    touches = fm.get("touches")
    if isinstance(touches, str):
        return [touches.strip()] if touches.strip() else []
    if isinstance(touches, list):
        # --touches CLI 와 동형: 각 원소 strip·빈 값/비-문자열 drop (silent-miss 방어).
        return [t.strip() for t in touches if isinstance(t, str) and t.strip()]
    return []


# ── domain 연동 (soft 알림·ADR-0018 #2) ──────────────────────────────────
#
# 순환 없음: domain→board / ticket_finish→board,domain / board 는 둘 다 import 안 함.
# domain.py 부재(솔로/신규 clone·구버전)·로드 실패 → None (호출부가 graceful skip).

DOMAIN_PY = REPO / ".project_manager" / "tools" / "domain.py"


def _load_domain_module():
    """domain.py 를 경로 import 해 모듈로 반환한다 (부재/실패 시 None).

    board.py·areas 해소와 동일한 deep-import seam — 테스트가 hermetic 하게 대역을
    주입하거나 None(부재)을 흉내낼 수 있다.
    """
    import importlib.util
    if not DOMAIN_PY.exists():
        return None
    spec = importlib.util.spec_from_file_location("_domain_soft", DOMAIN_PY)
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _verify_engine_rev(mod, "domain.py")  # T-0397 불변식: stamped sibling 로드 지점은 verify
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — domain 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def affected_domain_titles(ticket_id: str, board_py: Path) -> list[tuple[str, bool | None]] | None:
    """ticket touches ∩ domain covers 로 영향받는 페이지 (title, stale) 목록을 돌려준다.

    각 원소 = `(title, stale)` — stale 은 `domain.page_stale`(True=낡음·False=fresh·
    None=판정불가/unknown). soft step 이 stale True 줄 앞에 ⚠ 를 단다(visibility·ADR-0018 #3).
    domain.py 부재·로드 실패 → None (호출부가 조용히 skip — 솔로/신규 clone 무영향).
    touches 부재·영향 0 → [](빈 알림). domain.pages_for_touches 재사용(중복 매칭 0).

    **git_runner 1회 생성해 공유** — 영향 페이지마다 새로 만들지 않고 한 runner 를
    page_stale 에 넘긴다(reviewer suggestion·subprocess 셋업 중복 회피). page_stale 은
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
    try:
        pages = domain.pages_for_touches(touches, domain.load_pages())
    except Exception:
        return None
    # git_runner 를 한 번만 만든다(REPO 컨텍스트) — 페이지마다 subprocess 셋업 반복 방지.
    # 생성 자체가 실패하면 stale 은 전부 unknown(None) 으로 두고 계속(비차단).
    try:
        git_runner = domain._real_git_runner(REPO)
    except Exception:  # noqa: BLE001 — runner 생성 실패는 stale unknown 으로 흡수.
        git_runner = None
    out: list[tuple[str, bool | None]] = []
    for page in pages:
        try:
            stale = domain.page_stale(page, git_runner=git_runner)
        except Exception:  # noqa: BLE001 — stale 못 구하면 무표시(None)·비차단.
            stale = None
        out.append((page["title"], stale))
    return out


# ── 로그 스켈레톤 ───────────────────────────────────────────────────────

# 회귀 baseline 은 *실측* new_total 1줄만 남긴다 (ADR-0008 lean baseline·ADR-0023 — 직전
# 합계는 status.md 에 박제하지 않으므로 delta 는 PM 이 서술로 채운다·history 단일 진실=log).
LOG_SKELETON_TEMPLATE = """\
## [{date}] {entry_type} | {ticket_id} — {title}

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
) -> str:
    if date is None:
        date = datetime.date.today().isoformat()
    return LOG_SKELETON_TEMPLATE.format(
        date=date,
        entry_type=entry_type,
        ticket_id=ticket_id,
        title=title,
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
        log_file: Path = LOG_FILE,
        board_py: Path = BOARD_PY,
        venv_python: str | Path = _default_python(),
        regression_cwd: str | Path | None = None,
    ) -> None:
        self._log_file = log_file
        self._board_py = board_py
        self._venv_python = venv_python
        # 회귀 cwd seam (ADR-0014·T-0149) — 분리된 PM 홈(②)엔 tests/ 가 없으므로 회귀는 활성
        # worktree 슬롯(①·tests/)에서 돌아야 한다. **즉시 고정하지 않는다** — `regression_cwd`
        # 명시 주입은 그대로 보존(테스트/명시 override)하되, 미지정이면 `__init__` 시점의 REPO
        # 박제 대신 _default_run_pytest 가 런타임에 _regression_cwd() 로 self-host 슬롯을
        # 자동해소한다(T-0149 — pm_handoff `_regression_cwd` 재사용·솔로는 REPO 폴백 무변경).
        self._regression_cwd = str(regression_cwd) if regression_cwd else None

        # subprocess DI — 기본값은 실제 subprocess 호출
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_git_fn = run_git_fn or self._default_run_git

        # board 조회 DI — 기본값은 실 board.py import 구현
        self._board_count_fn = board_count_fn or self._default_board_count
        self._ticket_title_fn = ticket_title_fn or self._default_ticket_title

        # domain soft 알림 DI (ADR-0018 #2) — 기본값은 실 domain.py import 구현.
        # None 반환 = domain 부재/로드 실패(조용히 skip). 막지 않음(soft).
        self._affected_domain_fn = affected_domain_fn or self._default_affected_domain

    # ── 기본 subprocess 구현 (실제 실행) ─────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """회귀를 실행해 (returncode, stdout+stderr) 반환.

        명령 해소(ADR-0014 per-repo):
          - **multi-PM 모드** — 활성 repo 의 per-repo test_cmd(areas.md)가 있으면 그 문자열을
            shell 로 실행(board.py 회귀와 동형·비-Python repo 수용).
          - **솔로/프레임워크 자기 회귀** — per-repo cmd 가 없으면 현행 그대로
            `[venv_python, -m, pytest, tests/, -q]` venv argv(도그푸딩 불변·하위호환).

        cwd 는 런타임 해소(T-0149) — 명시 주입(`regression_cwd` 인자)이 있으면 그 경로,
        없으면 `_regression_cwd()` 가 self-host 단일 슬롯을 자동해소(② 홈 cwd 에서도 활성
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

    # ── 메인 흐름 ────────────────────────────────────────────────────

    def run(
        self,
        ticket_id: str,
        section: str | None,
        dry_run: bool,
        skip_pytest: bool = False,
    ) -> int:
        """ticket_id 완료 부기 전체 흐름을 실행한다.

        반환: 0=성공, 1=실패 (중단).

        `section` 은 후방호환용으로 받기만 하고 무시한다 — status.md 합계표 섹션 행은
        ADR-0023(a안) 으로 제거됐다(judgment-only·테스트 수는 박제 안 함).

        `skip_pytest`(--no-pytest·T-0285) 는 [1/5] 회귀 단계를 건너뛴다 — 측정은 PM 이 /pm-qa
        등으로 별도. board complete 는 `--tests-pass` 를 유지한다(pm_handoff `--no-pytest` 동형·
        회귀 red 아님·skip 로 진행). 다중슬롯 형상에서 회귀 cwd 를 정할 수 없을 때 우회 수단.
        """
        del section  # ADR-0023 — status 합계표 제거로 더 이상 쓰지 않음(후방호환 수용만).
        print(
            f"[ticket_finish] {ticket_id} 완료 부기 시작 "
            f"(dry_run={dry_run}, skip_pytest={skip_pytest})"
        )

        # ── 1. 회귀 실행 ──────────────────────────────────────────────
        # dry-run 도 pytest 를 실제 실행한다 — "부작용 없음"이지 "빠름"이 아니다.
        # 파일·board·git 편집만 생략하므로 pytest 실행은 항상 수행. (--no-pytest 는 예외 — 측정 skip.)
        print("\n[1/5] 회귀 실행 중...")
        if skip_pytest:
            # --no-pytest — 회귀 측정은 PM 이 별도(/pm-qa 등). board complete 는 --tests-pass 유지
            # (pm_handoff --no-pytest 동형·red 아님·skip 로 진행). 측정 total 은 log 스켈레톤에서 "?".
            print("  [--no-pytest] 회귀 측정 skip — 측정은 별도(/pm-qa 등). board complete 는 --tests-pass 유지.")
            new_total: int | str = "?"
        else:
            if dry_run:
                print("  [dry-run] pytest tests/ -q 실행 중 (파일·board·git 편집만 생략)...")
            returncode, output = self._run_pytest_fn()
            print(output.rstrip())

            if not is_pytest_green(output, returncode):
                print(
                    "\n[중단] 회귀 red — log/current.md·board·git 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                print(
                    "원인: pytest 가 실패를 보고했거나 출력 파싱 실패.",
                    file=sys.stderr,
                )
                return 1

            parsed = parse_pytest_output(output)
            if parsed is None:
                print(
                    "\n[중단] pytest 출력 파싱 실패 — passed 수를 읽지 못했다.",
                    file=sys.stderr,
                )
                return 1

            new_total, deselected = parsed
            print(f"\n  ✓ green: passed={new_total}, deselected={deselected}")

        # status.md 는 더 이상 갱신하지 않는다 (ADR-0023 a안 — judgment-only).
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
        )

        if dry_run:
            print("  [dry-run] log/current.md 에 append 할 스켈레톤:")
            print("  " + skeleton.replace("\n", "\n  "))
        else:
            log_text = self._log_file.read_text(encoding="utf-8") if self._log_file.exists() else ""
            self._log_file.write_text(log_text + "\n" + skeleton, encoding="utf-8")
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

        # ── 4. git add -A ─────────────────────────────────────────────
        print("\n[4/5] git stage (git add -A)...")
        if dry_run:
            print("  [dry-run] git add -A (실제 실행 생략)")
        else:
            git_rc, git_out = self._run_git_fn(["add", "-A"])
            if git_rc != 0:
                print(
                    f"\n[중단] git add -A 실패 (rc={git_rc}): {git_out.rstrip()}",
                    file=sys.stderr,
                )
                return 1
            print("  ✓ git add -A 완료 (commit 은 아직 안 했다)")

        # ── 5. 잔여 PM 작업 출력 ─────────────────────────────────────
        print("\n[5/5] PM 이 손으로 할 잔여 작업:")
        print("  ① log/current.md 서술 불릿 채우기 — <!-- PM: 무엇을·왜 서술 --> 를 실제 내용으로 교체")
        print("  ② status.md 모듈 행(상태 + 비고) — 변경된 모듈 행 판정을 architect/PM 이 직접 갱신 (테스트 수는 박제 안 함·ADR-0023)")
        print("  ③ git commit — 메시지는 PM 이 작성 (Co-Authored-By: Claude 트레일러 포함)")

        # ── soft 알림: 영향받는 domain 페이지 (ADR-0018 #2·U2·비차단) ──────
        # 정보일 뿐 게이트가 아니다 — 완료 흐름·rc 를 막지 않는다(예외도 삼킨다).
        # domain.py 부재(솔로/신규 clone) → None → 조용히 skip(무영향).
        self._notify_affected_domain(ticket_id)

        if dry_run:
            print("\n[dry-run] 완료 — 실제 편집·board·git 는 실행하지 않았다.")
        else:
            print(f"\n[완료] {ticket_id} 부기 완료.")

        return 0

    def _notify_affected_domain(self, ticket_id: str) -> None:
        """이 ticket 이 건드린 영역의 domain 페이지를 soft 알림으로 출력한다 (비차단).

        영향 페이지가 stale(covers 코드가 page updated 후 커밋·ADR-0018 #3)이면 그 줄 앞에
        `⚠` 를 단다 — fresh(False)/unknown(None)은 무표시. 도그푸딩/multi-PM 어디서든 완료를
        절대 막지 않는다 — domain 부재·예외는 조용히 삼키고(crash 0), 영향 0 이면 한 줄
        안내만 낸다. dry-run/실행 동일(정보 출력만). stale 못 구해도(예외/unknown) 비차단.
        """
        print("\n[domain] 영향받는 domain 페이지 (soft·비차단):")
        try:
            affected = self._affected_domain_fn(ticket_id)
        except Exception:  # noqa: BLE001 — soft 알림은 완료를 막지 않는다.
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
            "(deprecated·no-op) status.md 합계표 섹션 행은 ADR-0023 으로 제거됐다 — "
            "받기만 하고 무시한다(후방호환). status.md = judgment-only."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="편집·board·git 없이 무엇을 바꿀지만 출력한다.",
    )
    # ── 두-git 다중슬롯 seam (ADR-0027·pm_handoff 미러·T-0285·ADR-0057) ──────────
    # 분리된 PM 홈(②) + worktree 슬롯 여럿 형상에서 회귀를 어느 worktree(tests/ 보유)에서
    # 돌릴지 disambiguate 한다 — pm_handoff `--repo`/`--slot`/`--no-pytest` 와 동형(canonical
    # 분해형·ADR-0057). `--repo` 단독은 활성(leased) 슬롯 1개면 자동해소·0/≥2 는 fail-loud(M3).
    identity_args.add_identity_args(parser)
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help="[1/5] 회귀 단계를 skip 한다 (측정은 /pm-qa 등으로 별도·board complete 는 --tests-pass 유지).",
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
    parser = build_parser()
    args = parser.parse_args(argv)

    # PM-홈 worktree 오실행 가드(T-0345) — 부기 어떤 부작용(회귀/log append/complete/stage)도
    # 나기 *전에* fail-loud. 읽기 경로 없음(ticket_finish 는 전부 쓰기 부기)이라 진입에서 한 번.
    if _guard_worktree_misanchor():
        return 1

    # 정체성 인자 *검증*(`--slot` 단독·`slot < 1` = ADR-0057 uniform fail-loud)은 `--no-pytest` 와
    # 무관하게 **항상** 수행한다(pm_handoff 동형·codex 게이트 — 안 그러면 `--no-pytest --slot 4` 같은
    # ADR-0057 위반 입력이 조용히 통과). 반면 두-git 다중슬롯 회귀 cwd 해소·모호 게이트는 **회귀를 실제로
    # 돌 때만**(ADR-0027·pm_handoff 미러·T-0285) — `--no-pytest` 면 regression cwd 가 무의미하고, 모호
    # 에러가 "--no-pytest 로 skip 하라"를 광고하며 정작 --no-pytest 준 사용자를 막는 self-contradiction 회피.
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as exc:
        parser.error(str(exc))

    # task 명 검증(must-fix·T-0355 게이트) — board 정체성 깔때기와 **동일 공유 validator**
    # (`identity_args.validate_task_name`·worktree_pool 엔진 validator 와 동형·로직 중복 0)를 소비해,
    # F6 실행-위치 해소 이전 불법 task 명(traversal/공백/`<repo>_<N>` 예약)을 fail-loud 한다. `--slot`
    # 검증과 동형으로 `--no-pytest` 무관 **항상** 수행(회귀 skip 여도 정체성 검증은 우회 안 됨). 예약패턴
    # (⑥) 판정용 registered_repos 는 board 모듈에서 fail-soft 해소(부재/실패 시 char/traversal 검증만).
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
                "(`--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴 `<repo>_<N>`(⑥)은 쓸 수 없다).",
                file=sys.stderr,
            )
            return 1

    regression_cwd: str | None = None
    if not args.no_pytest and identity.task:
        # task-mode(`--task`) 회귀 작업공간 F6 해소(spike §3b F6·⑦·T-0355) — task 가 보유한 슬롯
        # 중 실행 위치를 특정하고 그 worktree 절대경로를 회귀 cwd 로 고정·surface 한다(cwd 비참여·
        # T-0345 불변). 모호/미보유는 fail-loud. slot-mode(`--repo`/`--slot`) 는 아래 기존 경로.
        try:
            ws = identity_args.resolve_task_workspace(identity, LEASES_FILE)
        except identity_args.WorkspaceResolutionError as exc:
            print(f"\n[중단] 회귀 작업공간 해소 — {exc}", file=sys.stderr)
            return 1
        print(f"작업공간(task {identity.task}) → {REPO / ws.slot}")
        regression_cwd = _regression_cwd(ws.slot)
    elif not args.no_pytest:
        worktree_slot, ambiguity = _resolve_finish_slot(identity.repo, identity.slot)
        if ambiguity is not None:
            print(f"\n[중단] 회귀 슬롯 해소 모호 — {ambiguity}", file=sys.stderr)
            print(
                "  → `--repo <name> [--slot <N>]`(예: --repo project_manager --slot 1) 으로 슬롯을 "
                "명시하거나, `--no-pytest` 로 회귀를 skip 하라(측정은 /pm-qa 등으로 별도).",
                file=sys.stderr,
            )
            return 1
        # 해소된 슬롯이 있으면 그 worktree 를 회귀 cwd 로 명시 forward(_regression_cwd 위임).
        # solo/미해소(None)면 regression_cwd 미주입 → _default_run_pytest 런타임 폴백(현행 100% 보존).
        if worktree_slot:
            regression_cwd = _regression_cwd(worktree_slot)

    finisher = TicketFinisher(regression_cwd=regression_cwd)
    return finisher.run(
        ticket_id=args.ticket_id,
        section=args.section,
        dry_run=args.dry_run,
        skip_pytest=args.no_pytest,
    )


if __name__ == "__main__":
    sys.exit(main())
