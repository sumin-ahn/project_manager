#!/usr/bin/env python3
"""PM 세션 시작 부트스트랩 헬퍼 — 기계 측정 부분을 한 명령으로 dump 한다.

사용:
    venv/bin/python .project_manager/tools/pm_bootstrap.py [--json] [--with-pytest]

동작:
  board list → 상태별 카운트 + open ticket 목록 (claim 가능).
  board lint → clean | N warnings.
  pytest tests/ -q → 회귀 A / B 통과 (--with-pytest opt-in — default skip).
  git log / git status → 브랜치·최근 commit·working tree 상태.
  log/current.md 마지막 entry → date / type / title.

회귀 측정 default skip:
  직전 handoff entry 가 회귀 숫자를 기록한다면 부트스트랩 단계 pytest 재측정은
  중복 안전망에 가깝다. default skip 으로 부트스트랩 ~5초. baseline 의심 시
  --with-pytest 명시. 프로젝트가 별도 QA skill 을 두지 않는다면 default 를
  True 로 바꿔도 된다.

출력:
  기본: markdown (PM 의 첫 turn 보고에 그대로 붙여넣기 가능).
  --json: JSON (slash command skill wrapper 소비용).

결정:
  - fail-soft 가 아니다 — git/board/pytest subprocess 실패 시 즉시 중단 (비-0 종료).
  - subprocess DI — ticket_finish.py 와 동일 패턴 (테스트 결정론).
  - LLM 호출 없음 — stdlib + board.py import 만.
  - 첫 turn 권장 액션의 기계 부분만 자동화.
    직전 세션 요약·"무엇부터 갈까요" 옵션 제시는 PM 손.
  - 타임스탬프 = datetime.datetime.now(tz=ZoneInfo("Asia/Seoul")) (KST).
    프로젝트 timezone 이 다르면 KST 상수만 교체.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, NamedTuple

REPO = Path(__file__).resolve().parents[2]


def _resolve_log_file(repo: Path = REPO) -> Path:
    """공유 `log/current.md` 경로 — worktree 슬롯 cwd 에서도 PM 홈 공유 로그를 가리킨다 (T-0284·ADR-0027).

    자기분리 토폴로지(ADR-0027)에서 board/wiki/log 는 ② PM 홈이 소유하고 코드/tests 는 슬롯
    worktree(`<home>/work/<repo>_<N>`)에 있다. 부트스트랩이 슬롯 cwd 에서 돌면 `REPO` 가 슬롯
    worktree 라 REPO-앵커 로그(`REPO/.project_manager/wiki/log/current.md`)가 *부재*(공유 로그는
    PM 홈 소유)해 log 마지막 entry 가 "미해소"로 뜬다 — `pm_log.py tail`(PM 홈에서 실행)과 비대칭.
    슬롯 형상(`repo.parent.name == "work"`)이면 상위 PM 홈(`repo.parent.parent`)의 공유 로그가
    *실재할 때만* 그걸 가리킨다. 슬롯이 아니거나(솔로·PM 홈 직접 실행) 상위 공유 로그 부재면
    REPO-앵커 그대로 폴백한다 — 회귀 0·fail-soft·fresh 채택자(standalone repo) 무영향.
    """
    default = repo / ".project_manager" / "wiki" / "log" / "current.md"
    if repo.parent.name == "work":
        shared = repo.parent.parent / ".project_manager" / "wiki" / "log" / "current.md"
        if shared.exists():
            return shared
    return default


LOG_FILE = _resolve_log_file()
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
TOOLS_DIR = REPO / ".project_manager" / "tools"
AREAS_FILE = REPO / ".project_manager" / "areas.md"   # legacy 별칭 (아래 _areas_file 가 board_root 추종)
# worktree 리스 장부 (ADR-0013) — worktree_pool.LEASES_FILE 와 *같은 위치*. _auto_slot 이
# 단일 슬롯 자동바인딩 판정에 stdlib json 으로 직접 read 한다(worktree_pool 미import·touches
# 격리·_registered_repos 가 areas.md 를 stdlib 로 읽는 것과 동형·데이터 결합만).
LEASES_FILE = REPO / ".project_manager" / ".local" / "worktree-leases.json"

# `git symbolic-ref HEAD` 의 브랜치 full ref 접두 — `refs/heads/<name>`. 이 접두 **정확히**를
# 제거해야 순수 브랜치명이 된다(동명 태그 존재 시 `heads/<name>` 로 오염되는 `--short` 대신 full
# ref 를 읽는 이유·T-0377 계보·worktree_pool._SYMREF_BRANCH_PREFIX 와 동일 규칙).
_SYMREF_BRANCH_PREFIX = "refs/heads/"

# 커맨드 카드 (ADR-0045) — 도구 호출 접두. 카드는 이 세션이 쓸 전 커맨드를 정체성 채운
# 완성형으로 dump 한다("--help 자체를 안 가게"·사용자 지시). `python3` 은 머신-불변 doc 표면
# 관례(T-0219·Windows 는 `py`·CLAUDE.md 노트). 경로는 multi-PM 공유 루트 기준 상대(도그푸딩
# 관례와 정합·PM 이 공유 루트에서 board/wiki 조작).
_CARD_TOOL_INVOKE = "python3 .project_manager/tools"


# ── 커맨드 카드 공용 정의서 (파서-생성화·T-0362·⑰·§F12) ─────────────────────────
# 카드 커맨드 토큰(도구·서브커맨드·플래그)의 **단일 진실**. 손 문자열 하드코딩(옛 inline
# `cmd("board.py", "list --mine", …)`)을 이 정의서로 대체한다 — "가이드가 실제 옵션과 다른" 게
# 구조적으로 불가능해진다(⑰·⑭ PM 실수 기계 차단·ADR-0057 parse_identity 단일화 선례). 카드
# 렌더(`_build_command_card_markdown`)는 이 정의서를 소비하고, 카드↔CLI 정합 test
# (`test_pm_bootstrap_card_parity.py`)가 정의서 ↔ 실 argparse 를 양방향 대조한다:
#   - `subpath` = 그 도구 `build_parser()` 의 서브커맨드 **leaf 경로**(flag-only 도구는 `()`).
#     정합 test 가 이게 실 등록 leaf 인지 introspection 으로 검증(카드→파서).
#   - `flags`   = 렌더에 등장하는 **비-정체성** 옵션(정체성 `--repo/--slot/--task` 은 identity
#     축으로 별도 검증). 정합 test 가 각 flag 이 그 leaf 에 실 등록됐는지 검증(카드→파서).
#   - `render`  = 카드에 찍히는 정확한 인자 문자열(정체성/task 명 suffix 는 caller 가 보간·ADR-0057
#     "정체성은 실값·사용자 입력만 placeholder"). byte 동일성으로 기존 카드 회귀 무손상.
# 방식 = **공용 정의서(정의서 primary) + test-측 introspection**: 카드는 curated·모드-스코프·
# 주석/⚠/스킬-강등이 섞인 사람-facing 가이드라 순수 introspection 으로는 생성 불가 → 토큰만
# 정의서로 단일화하고, 파서 정합은 introspection(T-0348 `_registered_leaves` 방식 재사용)으로 못박는다.
class _CardCmd(NamedTuple):
    tool: str            # 도구 파일명 (예 "board.py")
    render: str          # 카드에 찍히는 인자 문자열 (정체성 suffix 제외·byte 동일성 원천)
    subpath: tuple       # build_parser() 서브커맨드 leaf 경로 (flag-only 도구는 ())
    flags: tuple         # 렌더의 비-정체성 옵션 (정합 test 가 leaf 실 등록 여부 검증)


# 슬롯/솔로 카드 CLI 커맨드 (현행 표면·byte 동일) — 정체성 suffix 는 render 에 미포함(caller 보간).
_C_BOARD_LIST_MINE = _CardCmd("board.py", "list --mine", ("list",), ("--mine",))
_C_BOARD_LIST = _CardCmd("board.py", "list", ("list",), ())
# `list --all`(ADR-0066·T-0385·ADR-0067) — 무인자 기본이 세션 스코프(내 세션 스트림만·타 세션분
# 완전 비노출)로 바뀌어, 기존 무인자 전체 뷰(모든 세션·타 사용자·경합 가시)는 `--all` 로 이관됐다.
# 타 PM 열람·backlog 확인은 이 명시 조회 몫(기본 뷰엔 카운트 줄도 없음). 평시 불요.
_C_BOARD_LIST_ALL = _CardCmd("board.py", "list --all", ("list",), ("--all",))
_C_BOARD_NEW = _CardCmd("board.py", 'new "<제목>" --prefix <PFX>', ("new",), ("--prefix",))
_C_BOARD_PROMOTE = _CardCmd("board.py", "promote T-NNNN", ("promote",), ())
_C_BOARD_COMPLETE = _CardCmd("board.py", "complete T-NNNN --tests-pass", ("complete",), ("--tests-pass",))
_C_BOARD_SHOW = _CardCmd("board.py", "show T-NNNN", ("show",), ())
_C_BOARD_LINT = _CardCmd("board.py", "lint", ("lint",), ())
_C_BOARD_CLAIM = _CardCmd("board.py", "claim T-NNNN", ("claim",), ())
_C_BOARD_REGRESSION = _CardCmd("board.py", "regression run", ("regression",), ())
_C_TICKET_FINISH = _CardCmd("ticket_finish.py", "<T-NNNN>", (), ())
_C_EXTERNAL_REVIEW = _CardCmd(
    "external_review.py", "--ticket T-NNNN --adr ADR-NNNN", (), ("--ticket", "--adr"))
_C_PM_HANDOFF = _CardCmd(
    "pm_handoff.py", '--session-seq <N> --wave-summary "<요약>"', (),
    ("--session-seq", "--wave-summary"))
_C_BOARD_LIVEGATE = _CardCmd("board.py", "livegate record", ("livegate",), ())
_C_BOARD_PREFIX_LIST = _CardCmd("board.py", "prefix list", ("prefix", "list"), ())
_C_BOARD_PREFIX_RENAME = _CardCmd("board.py", "prefix rename <OLD> <NEW>", ("prefix", "rename"), ())
_C_BOARD_PREFIX_MERGE = _CardCmd("board.py", "prefix merge <SRC> --into <DST>", ("prefix", "merge"), ("--into",))
_C_BOARD_REID = _CardCmd("board.py", "reid <OLD-ID> <NEW-ID>", ("reid",), ())
_C_BOARD_MIGRATE_IDENTITY = _CardCmd("board.py", "migrate-identity --dry-run", ("migrate-identity",), ("--dry-run",))
_C_PM_LOG_TAIL = _CardCmd("pm_log.py", "tail", ("tail",), ())
_C_DOMAIN_AFFECTED = _CardCmd("domain.py", "affected --ticket <T-NNNN>", ("affected",), ("--ticket",))

# task 모드 CLI 커맨드 (F1~F7·T-0353~0359·⑥) — task 명은 정체성 축이라 alloc/release/wave 는
# `--task <실명>` 실값 보간(caller). task end/prefix 의 name 위치인자는 정체성 중간삽입이라
# 정합 대조를 깨므로(render+suffix 비연속) `<이름>` placeholder 로 유지(사용자 입력·헤더가 실명 표기).
_C_PC_ALLOC = _CardCmd("pm_config.py", "alloc <repo>", ("alloc",), ())
_C_PC_RELEASE = _CardCmd("pm_config.py", "release <slot>", ("release",), ())
_C_PC_TASK_END = _CardCmd("pm_config.py", "task end <이름>", ("task", "end"), ())
_C_PC_TASK_PREFIX = _CardCmd("pm_config.py", "task prefix <이름> <p|none>", ("task", "prefix"), ())

# readonly 공유 슬롯(⑬·T-0358) CLI 커맨드 — 조회만.
_C_PC_STATUS = _CardCmd("pm_config.py", "status", ("status",), ())

# 모드별 정의서 (정합 test 의 authoritative 스코프 — 카드 렌더가 소비하는 CLI 커맨드 전량).
# 정합 test 는 각 모드 카드에서 CLI 줄을 추출해 이 목록과 leaf 단위 양방향 대조한다(카드↔정의서)
# + 각 record 를 실 파서로 검증(정의서↔파서). skill(`/pm-…`) 줄은 CLI 가 아니라 대상 밖.
_CARD_SLOT_CLI = (
    _C_BOARD_LIST_MINE, _C_BOARD_LIST, _C_BOARD_LIST_ALL, _C_BOARD_NEW, _C_BOARD_PROMOTE,
    _C_BOARD_COMPLETE, _C_BOARD_SHOW, _C_BOARD_LINT, _C_BOARD_CLAIM, _C_BOARD_REGRESSION,
    _C_TICKET_FINISH, _C_EXTERNAL_REVIEW, _C_PM_HANDOFF, _C_BOARD_LIVEGATE, _C_BOARD_PREFIX_LIST,
    _C_BOARD_PREFIX_RENAME, _C_BOARD_PREFIX_MERGE, _C_BOARD_REID, _C_BOARD_MIGRATE_IDENTITY,
    _C_PM_LOG_TAIL, _C_DOMAIN_AFFECTED,
)
_CARD_TASK_CLI = (
    # `_C_BOARD_LIST` = task-스코프 뷰 `list --task <이름>`(T-0365·[[ADR-0059]] Decision 10) — `--task`
    # 는 정체성 축이라 실값 보간(suffix)이고 base render 는 `list`(슬롯 카드의 slot-scoped 뷰와 동형).
    # `_C_BOARD_LIST_MINE` = user-wide(전 task·직교 렌즈·ADR-0056 불변).
    _C_BOARD_LIST, _C_BOARD_LIST_MINE, _C_PC_ALLOC, _C_PC_RELEASE, _C_PC_TASK_PREFIX,
    _C_PC_TASK_END, _C_BOARD_CLAIM, _C_BOARD_SHOW, _C_BOARD_LINT, _C_BOARD_REGRESSION,
    _C_TICKET_FINISH, _C_PM_HANDOFF, _C_PM_LOG_TAIL,
)
_CARD_READONLY_CLI = (
    _C_PC_STATUS, _C_PM_LOG_TAIL,
)
_CARD_MODE_CLI = {
    "slot": _CARD_SLOT_CLI,
    "task": _CARD_TASK_CLI,
    "readonly": _CARD_READONLY_CLI,
}


# ── codex 하네스 감지 + 카드 codex 절 (ADR-0069/0070 C-v2·spike §3.5) ──────────
# codex 전용 정적 진입 doc 이 없는 C-v2 구조(ADR-0069)에서, 부트스트랩 카드가 codex 실행모델·
# 위임 지침의 전달 채널이다(카드=운영 진실 표면·ADR-0045 — 정적 doc 이 아니라 엔진 발화라
# pm_update 갱신이 도달). 하네스 감지는 codex shell tool env 실측 마커로 기계 판정한다
# (`CODEX_THREAD_ID`=<uuid>·`CODEX_CI`=1·spike §D3 env 프로브·thread 019f8003·exec 경로). env
# 미설정 시 절 부재=정상(다른 하네스 카드 무변·회귀 0). ⚠ 대화형 TUI 세션의 env 마커 존치는
# 미실측(exec 경로만 확인) — T-0407 라이브 항목이 확인하고 불일치 시 predicate 를 그 티켓에서 보강.
def _is_codex_harness() -> bool:
    """codex 하네스면 True — `CODEX_THREAD_ID` 또는 `CODEX_CI` env 마커(기계 판정·추측 아님)."""
    return bool(os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI"))


# codex 절 본문(정적 진입 doc 대체·spike §3.5 3요소). 카드 렌더 끝에 감지 시 append 된다 —
# ① 위임=세션 내 spawn(`.codex/agents` 4축·`codex exec --agent` 부재) ② trust 2단계 힌트
# ③ 방법론 소재(공통 코어 AGENTS.md 자동 로드 + 이 카드 + `.agents/skills`·CLAUDE.md 미로드).
_CODEX_CARD_SECTION = "\n".join((
    "# codex 하네스 (실행모델·위임 — 정적 진입 doc 없음·ADR-0069/0070 C-v2)",
    "- **위임 = 세션 내 spawn** — `.codex/agents/{architect,developer,code-reviewer,researcher}` 를 "
    "codex 가 이 세션 안에서 스폰(부모 sandbox 상속)·`codex exec --agent` 플래그 부재라 외부 프로세스 위임 없음.",
    "- **trust 2단계** — ① 대화형 `codex` 1회 열어 프로젝트 trust 수락 ② `/hooks` 로 hook trust 승인. "
    "`-c projects.<path>.trust_level=trusted` CLI override 는 안 먹음(실측).",
    "- **방법론 소재** — 공통 코어 `AGENTS.md`(codex 자동 로드) + 이 카드 + `.agents/skills`"
    "(`$<스킬명>` 멘션(예 `$pm-bootstrap`)·auto-trigger). `CLAUDE.md` 는 codex 미로드.",
))


# ── board root 추종 (board/ 분리·ADR-0033 ①·T-0162 A6) ───────────────────────
# board(tickets+areas)는 `.project_manager/board/`(submodule)로 분리될 수 있다(ADR-0033 ①).
# 그러면 등록영역 surface(`_registered_repos`)·단일 self-host 자동바인딩(`_auto_slot`)이
# areas.md 의 wiki-밖 legacy 위치를 보면 *stale*(등록 repo 0개·자동바인딩 미해소)이다.
# pm_bootstrap 은 board.py 를 직접 import 하지 않으므로(touches 격리·areas 를 stdlib read),
# board.py 의 graceful 탐지를 *동형*으로 최소 복제한다 — board/tickets 가 실 디렉토리면 areas 가
# board/ 안(board/areas.md), 아니면 legacy `.project_manager/areas.md`. 솔로/미분리면 현 위치
# 100% 폴백(회귀 0). 상수 AREAS_FILE 는 hermetic 테스트 seam·legacy 기본값으로 유지.

def _areas_file() -> Path:
    """areas 레지스트리 경로 (board.py `areas_file` 동형·board_root 추종·T-0162 A6).

    `.project_manager/board/tickets` 가 실 디렉토리면 board 가 submodule 로 분리된 형상
    (ADR-0033 ①) → areas 도 board/ 안(`board/areas.md`). 아니면 legacy wiki-밖 위치
    (`.project_manager/areas.md`·현 위치·무변경).
    """
    base = REPO / ".project_manager"
    if (base / "board" / "tickets").is_dir():
        return base / "board" / "areas.md"
    return base / "areas.md"


def _dashboard_file() -> Path:
    """slot 대시보드 경로 (`wiki/log/dashboard.md`·*호출 시점* REPO 추종·hermetic·ADR-0047·T-0260).

    pm_handoff `_dashboard_file()` 과 *같은 위치* — 핸드오프 write / 부트스트랩 read 의 공유
    채널(log/current.md 와 동형·새 git 기계 0). 섹션 grammar 는 pm_handoff `parse_dashboard_
    sections` 를 동적로드 재사용(DRY) — 경로만 여기서 REPO 기준 해소한다(`_areas_file` 동형).
    """
    return REPO / ".project_manager" / "wiki" / "log" / "dashboard.md"


# ── 엔진 사본 rev 스탬프 (T-0397·형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.4.1"

# _load_tool(generic)이 이름으로 로드하는 것 중 rev 스탬프를 지닌(계측된) 형제만 대조.
# pm_log 등 미계측 도구는 제외(정상 사본에서도 ENGINE_REV 부재라 오탐). 계측 확대 시 추가.
_STAMPED_SIBLINGS = frozenset({"pm_handoff.py", "pm_bootstrap.py"})


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
    (예: 신 pm_bootstrap→신 board→구 identity_args 검출이 None 강등되지 않게)."""
    return getattr(exc, "_engine_rev_skew", False)


# ── worktree_pool import seam (multi-PM 모드·ADR-0013) ───────────────────────────
# multi-PM 인자(--repo)를 받았을 때만 alloc 경로에 진입한다. 솔로 무인자 경로는 이
# 모듈을 전혀 쓰지 않으므로 import 실패가 무해(fail-soft) — 단 --repo 를 줬는데
# worktree_pool 이 없으면 **명시 에러**(침묵 무력화 금지·ADR-0013).
def _load_worktree_pool():
    """worktree_pool 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    REPO/tools 경로 기준 `spec_from_file_location` — board.py·pm_*.py 와 같은
    스크립트-위치 앵커 관례. 솔로(multi-PM 미사용)에선 호출 안 되거나 None 이어도
    무인자 경로가 이 모듈을 안 쓰므로 무해. --repo 경로만 None 을 명시 에러로 처리.
    """
    import importlib.util

    wp_path = TOOLS_DIR / "worktree_pool.py"
    if not wp_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("worktree_pool", wp_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — 중첩 로드 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    _verify_engine_rev(mod, "worktree_pool.py")  # T-0397 — 로드 성공 후 skew 는 fail-loud(try 밖)
    return mod


def _load_board():
    """board 모듈을 동적 로드한다 — 보호 브랜치 surface(`_repo_protected`)용 (T-0076·fail-soft).

    `_load_worktree_pool` 과 동형 — `spec_from_file_location`(스크립트-위치 앵커). board 를
    *직접 import* 하지 않는 이유(touches 격리·병렬충돌 회피)는 동적 로드로 보존된다. 보호
    브랜치 경고는 *소프트*(추가 인지)라 board 부재/로드 실패는 None(경고 생략·정체성 선언
    자체는 깨지 않음). --repo 경로(multi-PM lean identity)에서만 호출된다.
    """
    import importlib.util

    if not BOARD_PY.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("board", BOARD_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 — fail-soft: 보호 경고는 소프트(로드 실패=경고 생략).
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — board 가 중첩 로드한 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    _verify_engine_rev(mod, "board.py")  # T-0397 — 로드 성공 후 skew 는 fail-loud(try 밖)
    return mod


def _load_tool(name: str):
    """이름의 엔진 도구 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    `_load_worktree_pool`/`_load_board` 와 동형 — `spec_from_file_location`(스크립트-위치
    앵커). 부트스트랩이 차수 추론(`pm_handoff.infer_next_session_num`)·log 마지막 entry 본문
    파싱(`pm_log.split_entries`)을 **DRY** 로 재사용하기 위한 역방향 동적 로드(T-0124 의
    `pm_handoff._load_pm_bootstrap` 동형·복붙 금지). 직접 import 하지 않는 이유(touches 격리·
    순환 회피)는 동적 로드로 보존된다. 차수/인계 dump 는 *소프트*(부재/실패 시 placeholder)라
    None 이어도 부트스트랩 본체는 깨지지 않는다.
    """
    import importlib.util

    tool_path = TOOLS_DIR / f"{name}.py"
    if not tool_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, tool_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 — fail-soft: 차수/dump 는 소프트(로드 실패=placeholder).
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — 중첩 로드 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    # T-0397 불변식: stamped sibling(pm_handoff·pm_bootstrap …)을 로드하는 지점은 verify.
    if f"{name}.py" in _STAMPED_SIBLINGS:
        _verify_engine_rev(mod, f"{name}.py")
    return mod


def _load_identity_args():
    """공용 `identity_args` 모듈을 동적 로드한다 (ADR-0057 canonical 정체성 인자·T-0322/T-0315).

    `_load_worktree_pool`/`_load_board`/`_load_tool` 과 동형 — `spec_from_file_location`
    (스크립트-위치 앵커·sys.path 무오염) — 스크립트 직접실행(`__main__`)·테스트
    `spec_from_file_location` 로드 양쪽에서 똑같이 동작한다(sibling import 관성 회피). 부재/
    로드 실패 시 None(fail-soft) — `_repo_slot_numbers`/`_auto_slot` 는 이를 "장부를 읽을 수
    없음"과 동일하게 흡수해 bare-bootstrap 솔로 경로를 깨지 않는다(B-1).
    """
    import importlib.util

    ia_path = TOOLS_DIR / "identity_args.py"
    if not ia_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("identity_args", ia_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        if _is_engine_rev_skew(exc):
            raise  # T-0397 — 중첩 로드 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    _verify_engine_rev(mod, "identity_args.py")  # T-0397 — 로드 성공 후 skew 는 fail-loud(try 밖)
    return mod


def _registered_repos(areas_file: Path | None = None) -> list[str]:
    """areas.md 레지스트리에서 등록된 repo 이름 목록 (identity surface '등록영역' 표면용).

    board.py 를 import 하지 않는다(touches 격리·병렬충돌 회피) — areas.md 의 `repo`/`prefix`
    칼럼을 stdlib 로 가볍게 읽는다. 파일 부재/스키마 불일치 → 빈 목록(fail-soft·솔로 무해).
    `areas_file` 미지정이면 `_areas_file()`(board_root 추종·T-0162 A6)로 *호출 시점* 해소.
    """
    if areas_file is None:
        areas_file = _areas_file()
    if not areas_file.exists():
        return []
    rows: list[str] = []
    header: list[str] | None = None
    sep_pattern = re.compile(r"^\|[\s:|-]+\|?$")
    for line in areas_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or sep_pattern.match(stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if not any(cells):
            continue
        row = dict(zip(header, cells))
        name = row.get("repo") or row.get("prefix")
        if name:
            rows.append(name)
    return rows


def _repo_slot_numbers(repo: str, leases_file: Path) -> list[int] | None:
    """`leases_file` 장부에서 `repo` 의 **활성(leased)** worktree 슬롯 번호(`work/<repo>_<N>`→N)를 반환한다.

    **공용 `identity_args.repo_slot_numbers` 위임**(ADR-0057·T-0322 흡수·T-0315 채택) — 리스
    읽기 코어(state=leased 필터·state 키 부재 back-compat·dedup·fail-soft None)는 그 모듈이
    단일 진실로 소유한다(byte-for-byte 동형 보존). 이 로컬 wrapper 는 `_auto_slot`·
    `_resolve_session_slot` 의 기존 호출 시그니처를 무손실 보존하기 위한 얇은 위임이다(touches
    격리 관성은 유지 — `_load_identity_args` 의 동적 로드로 sys.path 무오염).

    파일 부재/JSON 깨짐/스키마 불일치·`identity_args` 로드 실패 → **None**(fail-soft·"읽을 수
    없음" 신호); 정상 read 인데 그 repo 의 leased 슬롯이 0개면 빈 리스트 `[]`("읽었으나 활성
    슬롯 없음"). 호출부는 두 경우를 구분할 수 있다.
    """
    ia = _load_identity_args()
    if ia is None:
        return None
    return ia.repo_slot_numbers(repo, leases_file)


def _auto_slot(
    areas_file: Path | None = None,
    leases_file: Path = LEASES_FILE,
) -> tuple[str, int] | None:
    """단일 self-host 자동바인딩 판정 — 정확히 1 repo + 그 repo **활성(leased)** 슬롯 정확히 1개면 `(repo, N)`.

    솔로 무인자 bootstrap(`--repo`/`--slot` 둘 다 없음)에서, 등록 repo 가 정확히 1개이고
    그 repo 의 활성 worktree 슬롯(leased lease 엔트리)이 정확히 1개면 모호함이 없으므로 그 슬롯에
    자동으로 bind 한다(세션=`<repo>_<N>`·기존 `--slot` bind 경로 재사용). 그 외는 None
    (현행 솔로 유지) — repo 0개/≥2개 또는 활성 슬롯 0개/≥2개면 사용자가 `--repo --slot` 명시.

    판정:
      1. `_registered_repos` 재사용 — areas.md 등록 repo 가 정확히 1개인가(아니면 None).
      2. **공용 `identity_args.resolve_actor_slot` 위임**(ADR-0057·T-0322 흡수·T-0315 채택) —
         그 repo 의 **leased** 슬롯이 정확히 1개면 그 세션을, 0개면 None 을 돌려준다. ≥2개(모호)
         는 그 프리미티브가 `identity_args.SlotResolutionError` 로 raise 하지만, 이 함수는
         **순수 resolver 계약**(정확히 1 아니면 항상 None·fail-soft)이라 그 예외를 여기서 흡수해
         orchestration 을 보존한다 — guarded fail-loud 는 별도 `_resolve_session_slot` 전용.

    **idle 필터 영향(codex must-fix)**: 공용 프리미티브가 leased 만 세므로, `{1:idle, 2:leased}`
    는 이전 None(2개→폴백) 대신 leased={2}→exactly-1→슬롯2 로 해소된다 — incidental(`_regression_cwd`·
    display)이 *활성* 슬롯을 찾는 것이라 오히려 정합·개선(의도된 변화). solo `{1:leased}`→1 은 불변.

    파일 부재/스키마 불일치/JSON 깨짐/`identity_args` 로드 실패 → None(fail-soft — 자동바인딩은
    *추가 편의*이지 강제 아님·실패는 현행 솔로로 폴백). `areas_file` 미지정(None)이면
    `_registered_repos` 가 `_areas_file()`(board_root 추종·T-0162 A6)로 해소한다.

    **순수 resolver·반환 규격 불변(`(repo,N)` | None)** — 모든 incidental 호출부(`_worktree_cwd`·
    `_pm_state_display_path`·handoff `_regression_cwd`)가 이 fail-soft None 폴백에 기댄다.
    session-entry 의 guarded default-1 + fail-loud 규칙은 별도 `_resolve_session_slot` 가 처리한다.
    """
    repos = _registered_repos(areas_file)
    if len(repos) != 1:
        return None
    repo = repos[0]
    ia = _load_identity_args()
    if ia is None:
        return None
    try:
        session = ia.resolve_actor_slot(repo, leases_file)
    except ia.SlotResolutionError:
        return None  # ≥2 활성 슬롯(모호) — 순수 resolver 계약상 fail-soft None(raise 아님).
    if session is None:
        return None
    _, _, slot_tail = session.rpartition("_")
    return repo, int(slot_tail)


# ── guarded session-entry 슬롯해소 (default-1 + fail-loud·T-0178·ADR-0035) ─────
# session-entry 경로(bare `/pm-bootstrap` 무인자·bare handoff)는 *어느 슬롯의* 연속성을
# 이어야 하는지 명확해야 한다. `_auto_slot`(순수 resolver·"정확히 1 슬롯") 의 None 은
# **두 경우**를 합친다 — (1) solo(멀티-PM 미셋업·등록 repo 0개) = 정상 솔로 도그푸딩,
# (2) ambiguous(멀티-PM 셋업 존재하나 under-specified). (1)은 fail-soft 유지(bare bootstrap
# 무변경), (2)만 fail-loud 여야 1→2 슬롯 경계의 *침묵 동작변경*(없는 legacy 폴백·조용한
# 연속성 단절)을 명시 에러로 대체한다(spike §2 D2(b)·§3). 이 함수가 그 둘을 가른다.
class SlotResolutionError(Exception):
    """멀티-PM 셋업이 모호(under-specified)해 슬롯을 자동해소할 수 없을 때 — session-entry fail-loud.

    solo(멀티-PM 미셋업)와 구분된다 — 등록 repo 0개/슬롯 0개는 이 에러가 아니라 None(fail-soft·
    현행 솔로 유지). 메시지는 repo/slot 개수 + `--repo`/`--slot` 안내를 담는다(호출부가 그대로
    사용자에게 surface).
    """


def _resolve_session_slot(
    areas_file: Path | None = None,
    leases_file: Path = LEASES_FILE,
) -> tuple[str, int] | None:
    """session-entry 용 guarded 슬롯해소 — repo-안 default-1 + `slot1>단독>fail-loud`.

    해소 규칙(spike §3·repo 해소 후 repo 안에서):
      ```
      repo:  등록 repo 정확히 1개 > (≥2) → FAIL-LOUD
      slot:  slot 1 존재하면 1 > 슬롯 정확히 1개면 그것 > (모호/부재) → FAIL-LOUD
      ```
    `slot 1 존재 > 단독 슬롯` 순서 = 현행 단일슬롯 보존(회귀 0): `_1`-only→1, `_3`-only(단일·1
    아님)→그것(단독 규칙), `{1,2}`→1(slot1 default), `{2,3}`(1 부재·비단독)→FAIL-LOUD.

    반환:
      - `(repo, N)` — 해소됨(default-1·단독·단일 self-host).
      - **None** — solo(멀티-PM 미셋업): 등록 repo 0개, 또는 등록 repo 1개인데 장부를 읽을 수
        없거나(부재/깨짐) 그 repo 슬롯이 0개. 현행 솔로 fail-soft(bare bootstrap 무변경).
      - **raise `SlotResolutionError`** — ambiguous(멀티-PM 셋업 존재하나 under-specified):
        등록 repo ≥2(no `--repo`), 또는 repo 1개인데 슬롯 ≥2 이고 slot 1 부재한 비단독.

    `_auto_slot`(순수 resolver) 와 같은 stdlib json 파싱(`_repo_slot_numbers`)을 공유하되,
    "정확히 1개"가 아니라 슬롯 *집합*을 보고 default-1/단독/fail-loud 를 가른다. worktree_pool
    미import(touches 격리·ADR-0013) 유지. `areas_file` 미지정이면 `_registered_repos` 가
    `_areas_file()`(board_root 추종)로 해소.
    """
    repos = _registered_repos(areas_file)
    if len(repos) == 0:
        return None  # solo — 멀티-PM 미셋업(등록 repo 없음). 현행 솔로 fail-soft.
    if len(repos) > 1:
        raise SlotResolutionError(
            f"등록 repo {len(repos)}개({', '.join(repos)}) — 어느 repo 인지 모호하다. "
            f"`--repo <name> --slot <N>` 으로 명시하라."
        )
    repo = repos[0]
    slot_nums = _repo_slot_numbers(repo, leases_file)
    if not slot_nums:
        # 장부 부재/깨짐(None) 또는 그 repo 슬롯 0개([]) — 멀티-PM 셋업 미완(슬롯 미생성).
        # 현행 솔로 fail-soft(bare bootstrap 무변경·`_auto_slot` None 과 동형).
        return None
    if 1 in slot_nums:
        return repo, 1  # slot 1 존재 → default-1(`{1}`·`{1,2}` 등).
    if len(slot_nums) == 1:
        return repo, slot_nums[0]  # 단독 슬롯(`_3`-only) → 그것(현행 단일슬롯 보존).
    raise SlotResolutionError(
        f"repo '{repo}' 슬롯 {len(slot_nums)}개"
        f"({', '.join(f'work/{repo}_{n}' for n in sorted(slot_nums))}) 중 slot 1 부재 — "
        f"어느 슬롯인지 모호하다. `--slot <N>` 으로 명시하라."
    )


# ── per-slot pm_state 경로 안내 (multi-PM 연속성·ADR-0033 §3.1·T-0166) ─────────
# pm_state 는 *슬롯별*이다(spike §1.3·§3.1) — pm_handoff 가 활성 슬롯의 pm_state 를
# read/write 하므로(`.local/slots/<slot>/pm_state.md`·솔로 legacy 폴백), 부트스트랩의
# "첫 turn" 안내도 PM 이 *어느* pm_state 를 읽어야 하는지 같은 경로로 가리켜야 한다.
# 부트스트랩은 pm_state 를 편집하지 않으므로(read/write 주체는 pm_handoff) 경로 *문자열*만
# 해소한다 — slot 키는 `_auto_slot`(단일 self-host 자동바인딩·T-0123) 동형으로 재사용한다.
def _pm_state_display_path(
    slot: tuple[str, int] | None = None,
    areas_file: Path | None = None,
    leases_file: Path | None = None,
) -> str:
    """첫-turn 안내에 쓸 pm_state 경로 문자열 (per-slot·솔로 legacy 폴백·T-0166).

    슬롯이 해소되면(`(repo, N)`·명시 또는 `_auto_slot` 단일 self-host) per-slot 경로
    `.project_manager/.local/slots/<repo>_<N>/pm_state.md`, 미해소(솔로/모호)면 legacy
    `pm_state.md`(현행 안내 무변경·짧은 표기). `_auto_slot` 은 같은 모듈 함수라 직접
    호출(동적로드 불요·`_worktree_cwd` 동형). 예외/None 은 흡수해 legacy 표기로 폴백.
    `leases_file` 미지정이면 *호출 시점* REPO 기준 재구성(monkeypatch 추종·hermetic).
    """
    if leases_file is None:
        leases_file = REPO / ".project_manager" / ".local" / "worktree-leases.json"
    resolved = slot
    if resolved is None:
        try:
            resolved = _auto_slot(areas_file, leases_file)
        except Exception:  # noqa: BLE001 — fail-soft: 판정 실패는 legacy 표기 폴백.
            resolved = None
    if resolved is None:
        return "pm_state.md"
    repo, n = resolved
    return f".project_manager/.local/slots/{repo}_{n}/pm_state.md"


def _default_python() -> str:
    """플랫폼-인지 venv 인터프리터 경로 (없으면 sys.executable 폴백).

    Windows 는 venv/Scripts/python.exe, POSIX 는 venv/bin/python. venv 가 없으면
    현재 인터프리터로 폴백한다. 이 머신은 시스템 python3 에 pytest 가 없고 venv 에만
    있으므로, venv 가 있으면 무조건 venv 를 우선해 회귀 측정 인터프리터를 보존한다.
    """
    cand = REPO / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(cand) if cand.exists() else sys.executable

# 프로젝트 timezone — 부트스트랩 타임스탬프 표기용. 필요 시 교체.
# zoneinfo 부재(3.8-) 또는 시스템 tz DB·tzdata 부재(Windows) 시 고정 오프셋 폴백.
# 한국은 1988 이후 서머타임 없음 → 고정 UTC+9 가 항상 정확 (타임스탬프 의미 동일).
try:
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except Exception:  # ImportError(zoneinfo 부재) + ZoneInfoNotFoundError(tzdata 부재) 모두 포섭
    KST = datetime.timezone(datetime.timedelta(hours=9))


# 네트워크 git(fetch/pull) 1회당 timeout 상한 (초) — T-0217 freshness.
# 부트스트랩은 네트워크 I/O 를 처음 도입했다(fetch·pull·최대 ②+①+board). 원격이
# present-but-unresponsive(VPN 미접속·captive portal)면 각 호출이 OS TCP 타임아웃(수 분)까지
# 세션 시작을 막아 ticket 의 fail-soft 결정("offline/원격 불가에서도 부트스트랩 동작")을 위반한다.
# 이 값이 곧 네트워크 git 1회당 세션 시작 지연 상한이다(worst-case = 활성 스코프 수 × 이 값).
# 초과 시 `subprocess.TimeoutExpired` 를 fail-soft rc≠0 로 흡수 → `fetched=False` 경로 합류.
GIT_NETWORK_TIMEOUT = 20


# ── board 카운트 파서 ────────────────────────────────────────────────────

def parse_board_counts(board_output: str) -> dict[str, int]:
    """board list 출력에서 status 별 카운트를 파싱한다.

    board list 출력 행 형식:
      "  [open   ] T-NNNN  title...  claimed_by  tags"
    status 필드는 7자 width 로 패딩된다.

    반환: {"done": N, "open": M, "claimed": K, "blocked": L}
    """
    counts: dict[str, int] = {"done": 0, "open": 0, "claimed": 0, "blocked": 0}
    line_pattern = re.compile(r"^\s+\[(\w+)\s*\]")
    for line in board_output.splitlines():
        match = line_pattern.match(line)
        if match:
            status = match.group(1).strip()
            if status in counts:
                counts[status] += 1
    return counts


# 티켓 ID grammar — board.py `_TICKET_PREFIX_RE`(`_TICKET_PREFIX_BODY`·발행측 `_next_id` 의 역,
# 등록측 `pm_config._REPO_NAME_RE` 와 정합)와 *같은 문법*이어야 한다. board list --mine(T-0164·
# 기본 입력)이 multi-repo 보드를 surface 하면 정상 open 티켓은 prefixed ID(`T-PAY-001`·
# `T-service-a-001`·`T-P0-001`·`T-123-001`)다. 옛 `T-\d+` 만 잡으면 prefixed 가 전부 누락된다.
# 두 형태를 다 잡는다:
#   prefixed = `T-<PREFIX>-NNN`  — PREFIX 는 `[A-Za-z0-9][A-Za-z0-9_-]*`(등록 grammar 와 정합·
#                                  순수 숫자 `123` 포함), 끝 `-NNN` 은 숫자.
#   legacy   = `T-NNNN`          — `T-` 다음이 순수 숫자(하이픈 1개·prefix 마디 없음).
# legacy 와 순수-숫자 prefix 의 구분은 **구조적**이다(board.py 와 동일): prefixed 분기는 *내부
# 하이픈*(`PREFIX-NNN`)을 요구해 `T-123-001`(하이픈 2개)을 잡고, `T-0164`(하이픈 1개)는 legacy
# 분기(`\d+`)로 떨어진다 — 둘이 충돌하지 않는다.
# board.py 를 직접 import 하지 않는 이유(touches 격리·deep-import seam·순환 회피)는 parse_board_
# counts 등 다른 파서가 board 미import 인 것과 동형 — grammar 만 board.py 와 정합시킨다(가드
# 테스트 `test_parse_open_tickets_grammar_matches_board` 가 board `_ticket_prefix` 와 대칭 확인).
_TICKET_ID = r"T-(?:[A-Za-z0-9][A-Za-z0-9_-]*-\d+|\d+)"


def parse_open_tickets(board_output: str) -> list[str]:
    """board list 출력에서 open status 의 ticket ID 목록을 반환한다.

    claim 가능한 open ticket 만 추출한다 (claimed/blocked/done 제외). prefixed(multi-repo
    `T-PAY-001`)·legacy(`T-0164`) ID 를 둘 다 파싱한다 (board.py grammar 정합·T-0164).
    """
    tickets: list[str] = []
    line_pattern = re.compile(rf"^\s+\[open\s*\]\s+({_TICKET_ID})\b")
    for line in board_output.splitlines():
        match = line_pattern.match(line)
        if match:
            tickets.append(match.group(1))
    return tickets


# (ADR-0067) 세션 기본 뷰가 타 세션분을 board.py 층에서 완전 비노출하도록 바뀌어, 부트스트랩
# 층의 "그 외 open 접힘 카운트"(task-prefix 스트림 판정용 `_open_ticket_prefix`)와 "타 세션 claim
# 현황"(claimed 행 위치기반 파서 `parse_other_session_claims`·`_format_other_session_claims_line`)은
# 폐기됐다 — 기본 dump 는 세션 렌즈 조회(`list --repo X --slot N`/`--mine`)의 open_tickets 를 그대로
# 쓰고, 전-세션/전체는 명시 `board.py list --all` 이 담당한다. "다른 활성 PM" 슬롯 레지스트리는
# 별도 메커니즘(`_collect_dashboard_others`·leases 유래)이라 유지된다.


def parse_lint_result(lint_output: str) -> str:
    """board lint 출력에서 결과 요약을 반환한다.

    "✓ no lint issues" 이면 "clean" 반환.
    경고가 있으면 해당 줄 수를 "N warnings" 형식으로 반환.

    `--gate` 출력은 헤더 `⚠️  N lint issue(s) (M blocking 차단):` 다음에 각 issue
    줄(`✗`/공백 마크 + `[kind] …`)이 온다. 헤더 줄은 issue 가 아니므로 카운트에서
    제외한다 — 그러지 않으면 off-by-one 으로 1 더 세어진다(T-0038).
    """
    if "no lint issues" in lint_output:
        return "clean"
    # issue 라인 수를 세어 반환 — 요약 헤더("lint issue(s)" 줄)·clean 마크(✓)는 제외.
    warning_lines = [
        line for line in lint_output.splitlines()
        if line.strip()
        and not line.startswith("✓")
        and "lint issue(s)" not in line
    ]
    count = len(warning_lines)
    if count == 0:
        return "clean"
    return f"{count} warnings"


# ── pytest 파서 ──────────────────────────────────────────────────────────

def parse_pytest_counts(pytest_output: str) -> tuple[int, int] | None:
    """pytest -q 출력에서 (passed, total) 을 파싱한다.

    반환: (passed, total) — total = passed + failed.
    파싱 실패 시 None.
    """
    passed_match = re.search(r"(\d+) passed", pytest_output)
    if passed_match is None:
        return None
    passed = int(passed_match.group(1))
    failed_match = re.search(r"(\d+) failed", pytest_output)
    failed = int(failed_match.group(1)) if failed_match else 0
    total = passed + failed
    return passed, total


# ── git 파서 ─────────────────────────────────────────────────────────────

def parse_git_log(log_output: str) -> list[tuple[str, str]]:
    """git log --oneline 출력에서 (sha, subject) 목록을 반환한다.

    반환: [(sha, subject), ...]
    """
    commits: list[tuple[str, str]] = []
    for line in log_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            commits.append((parts[0], parts[1]))
        else:
            commits.append((parts[0], ""))
    return commits


def parse_git_branch(branch_output: str) -> str:
    """git rev-parse --abbrev-ref HEAD 출력에서 브랜치명을 반환한다."""
    return branch_output.strip()


def parse_git_status(status_output: str) -> str:
    """git status --short 출력에서 working tree 상태를 반환한다.

    변경 없으면 "clean", 변경 있으면 "N files modified" 형식 반환.
    """
    lines = [line for line in status_output.splitlines() if line.strip()]
    if not lines:
        return "clean"
    return f"{len(lines)} files modified"


def parse_git_ahead_behind(rev_list_output: str) -> tuple[int, int] | None:
    """`git rev-list --left-right --count HEAD...@{u}` 출력에서 (ahead, behind) 를 파싱한다.

    출력 형식: `"<ahead>\\t<behind>"` (좌=로컬만·ahead, 우=upstream만·behind). 형식
    불일치(빈 문자열·탭 없음 등) → None(파싱 실패 — 호출부가 upstream 미설정과 동일하게
    graceful 취급).
    """
    stripped = rev_list_output.strip()
    if not stripped:
        return None
    parts = stripped.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


# ── git freshness 판정 + 재부착 단서 (T-0217·ADR-0035·ADR-0013) ────────────────
# 머신 이동 부트스트랩이 stale 로컬(origin 보다 behind)을 그대로 dump 하던 연속성 사고를
# 막는다 — fetch 로 freshness 를 실측하고, clean·ff 가능한 안전 형상만 자동 동기(ff-pull)
# 한다. dirty·diverged·detached 는 표면화만(자동 pull 안 함). 아래는 그 판정의 순수 함수.

def freshness_decision(
    *, fetched: bool, detached: bool, dirty: bool | None, ahead: int | None, behind: int | None
) -> tuple[str, bool]:
    """fetch 후 scope 상태 문자열 + ff-pull 수행 여부를 판정한다 (T-0217·순수 함수·자동 pull 안전 게이트).

    - detached HEAD → ("detached", False) — 재부착은 PM 판단(ADR-0013)·자동 pull 안 함.
    - behind None (upstream 미설정/조회불가) → ("upstream 없음", False).
    - behind 0 · ahead 0 → ("최신", False).
    - behind 0 · ahead>0 → ("ahead-only", False) — 로컬만 앞섬(push 대기·pull 불요).
    - behind>0 · **안전조건 전부 충족** → ("동기", True) — 자동 ff-pull.
    - behind>0 · 안전조건 위반 → ("수동 동기 필요", False) — 표면화만.

    **자동 ff-pull 안전조건**(codex must-fix — 하나라도 어기면 자동 pull 금지):
      ① `fetched is True` — freshness 를 *실측*(fetch 성공)한 상태여야 한다. fetch 실패면
         behind/ahead 는 stale 원격 데이터라 그 위에서 pull 하면 "실측 후 clean·ff" 전제가 거짓.
      ② `dirty is False` — working tree 가 *확정 clean*. `git status` 조회 실패(dirty=None·
         미확인)는 clean 을 증명 못 하므로 자동 pull 불가(경고만).
      ③ `ahead == 0` — 로컬 커밋 없음 *확정*(ff 가능·diverged 아님). `ahead=None`(미확인)은
         0 취급하지 않는다 — 증명 없는 pull 금지(codex round-2 must-fix).
    """
    if detached:
        return "detached", False
    if behind is None:
        return "upstream 없음", False
    ahead_n = ahead or 0
    if behind == 0:
        return ("ahead-only", False) if ahead_n > 0 else ("최신", False)
    # behind > 0 — 안전조건 전부 충족(fetch 성공·확정 clean·ff 확정)일 때만 자동 동기.
    # `ahead == 0` 엄격 비교: None(미확인)은 False — ahead_n 폴백을 여기 쓰면 미확인이 0 으로 위장.
    if fetched and dirty is False and ahead == 0:
        return "동기", True
    return "수동 동기 필요", False


def _behind_warning(scope: dict) -> str:
    """behind>0 인데 자동 pull 불가한 scope 의 경고 문자열 + 차단 사유 (T-0217).

    사유: fetch 실패(stale) · status 미확인(clean 미증명) · dirty · diverged(ahead>0).
    """
    behind = scope.get("behind")
    reasons: list[str] = []
    if not scope.get("fetched"):
        reasons.append("fetch 실패")
    if scope.get("dirty") is None:
        reasons.append("status 미확인")
    elif scope.get("dirty"):
        reasons.append("dirty")
    if scope.get("ahead") is None:
        reasons.append("ahead 미확인")
    elif scope["ahead"] > 0:
        reasons.append(f"ahead {scope['ahead']} diverged")
    reason = f" ({', '.join(reasons)})" if reasons else ""
    return f"⚠ behind {behind} — 수동 동기 필요{reason}"


def _format_freshness(scope: dict) -> str:
    """freshness scope 한 줄 표기 — fetch/behind/ahead/상태 + 동기·경고 노트 (T-0217·T-0341).

    offline(fetch 실패)이면 origin 대비 실측을 못 하므로 remote-tracking 스냅샷을 "최신" 으로
    주장하지 않는다 — behind 0·upstream 미상은 "판정불가 — 스냅샷일 수 있음" fail-soft
    (T-0341·PM 69 stale-read: stale board 를 "최신" 으로 오신뢰하는 사고 차단). behind>0 은
    fetch 성공/실패 무관하게 로컬이 이미 아는 뒤처짐이라 그대로 표기(경고 note 는 별도 병기).
    """
    parts: list[str] = []
    fetched = scope.get("fetched")
    if not fetched:
        parts.append("⚠ fetch 실패 (offline·현행 유지)")
    if scope.get("detached"):
        parts.append("detached HEAD")
    else:
        behind = scope.get("behind")
        ahead = scope.get("ahead") or 0
        if behind is None:
            # upstream 미설정/조회불가 — offline 이면 판정불가로 감싼다(fail-soft).
            parts.append("판정불가 — 스냅샷일 수 있음" if not fetched else "upstream 없음")
        elif behind == 0 and ahead == 0:
            # online 이면 진짜 최신, offline 이면 실측 못 한 stale 스냅샷일 뿐(판정불가).
            parts.append("판정불가 — 스냅샷일 수 있음" if not fetched else "최신")
        else:
            parts.append(f"behind {behind} / ahead {ahead}")
    if scope.get("note"):
        parts.append(scope["note"])
    return " · ".join(parts)


# 직전 handoff entry 의 worktree 줄 grammar — pm_handoff `_worktree_line` 과 정합
# (`- worktree: slot=`...` · branch=`<branch>` (…)`). 재부착 단서(ADR-0013) 비교의 단일 소스.
_HANDOFF_WORKTREE_RE = re.compile(r"- worktree: slot=`[^`]*` · branch=`([^`]*)`")


def parse_handoff_worktree_branch(log_body: str | None) -> str | None:
    r"""직전 handoff entry 본문에서 worktree branch 를 추출한다 (재부착 단서·T-0217·ADR-0013).

    pm_handoff `_worktree_line` 형식(`- worktree: slot=... · branch=`<branch>` (…)`)을
    파싱한다. body 부재·줄 부재·`(미지정)` placeholder 는 None(비교 생략). 줄 뒤에
    부가 주석(예: `(릴리즈 ff 후 상태 · …)`)이 붙어도 branch 백틱 구간만 잡는다.
    """
    if not log_body:
        return None
    m = _HANDOFF_WORKTREE_RE.search(log_body)
    if m is None:
        return None
    branch = m.group(1).strip()
    if not branch or branch == "(미지정)":
        return None
    return branch


def reattach_warning(current_branch: str | None, log_body: str | None) -> str | None:
    """현 worktree 브랜치가 직전 handoff 의 worktree 브랜치와 다르면 경고 문자열 (T-0217·ADR-0013).

    같거나·둘 중 하나라도 미상이면 None(경고 생략). 자동 checkout 은 하지 않는다 —
    회전 재부착은 PM 판단(ADR-0013).
    """
    expected = parse_handoff_worktree_branch(log_body)
    if not expected or not current_branch:
        return None
    if current_branch == expected:
        return None
    return (
        f"⚠ worktree 브랜치 불일치 — 현재 `{current_branch}` · "
        f"직전 handoff `{expected}` (재부착은 PM 판단·자동 checkout 안 함·ADR-0013)"
    )


# ── log/current.md 파서 ──────────────────────────────────────────────────────────

def parse_log_last_entry(log_text: str) -> dict[str, str] | None:
    """log/current.md 에서 마지막 ## 항목의 date/type/title 을 파싱한다.

    log/current.md 포맷: `## [YYYY-MM-DD] type | title`

    반환: {"date": "...", "type": "...", "title": "..."} 또는 None.
    """
    pattern = re.compile(
        r"^## \[(\d{4}-\d{2}-\d{2})\]\s+(\S+)\s+\|\s+(.+)$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(log_text))
    if not matches:
        return None
    last = matches[-1]
    return {
        "date": last.group(1),
        "type": last.group(2),
        "title": last.group(3).strip(),
    }


def extract_last_log_entry_body(log_text: str) -> str | None:
    """log/current.md 의 마지막 `## [..]` entry **본문 전체**를 반환한다 (T-0179·인계 dump).

    부트스트랩은 그간 마지막 entry 의 제목(date·type·title)만 표시하고 본문은 PM 이
    `pm_log.py tail` 로 따로 읽었다 — self-sufficient 부트스트랩(인계 컨텍스트 자동 dump)을
    위해 본문 전체를 surface 한다. **단일 진실 = `pm_log.split_entries`**(tail 추출 로직)를
    동적 로드해 재사용한다(중복 파서 금지·DRY) — `cmd_tail` 의 `entries[-1][1]` 과 동형.

    pm_log 부재/로드 실패(fail-soft) 또는 entry 0개면 None — 호출부가 제목만 표시하던
    현행으로 폴백한다(소프트·본체는 안 깨짐).
    """
    pm_log = _load_tool("pm_log")
    if pm_log is None:
        return None
    try:
        _preamble, entries = pm_log.split_entries(log_text)
    except Exception:  # noqa: BLE001 — fail-soft: 파싱 실패는 None(제목만 표시 폴백).
        return None
    if not entries:
        return None
    return entries[-1][1].rstrip()


def infer_session_num(pm_state_text: str) -> int | str | None:
    """per-slot pm_state 텍스트에서 다음 PM 세션 차수를 추론한다 (T-0179·차수 announce).

    **단일 진실 = `pm_handoff.infer_next_session_num`**(세션 식별 절 최고차+1)을 동적 로드해
    재사용한다(복붙 금지·DRY·T-0124 역방향). pm_handoff 부재/로드 실패면 None(소프트
    placeholder) — handoff 의 `infer_next_session_num` 은 entry 부재 시 `"?"`(placeholder)를
    돌려주므로 이 함수도 그 규격을 그대로 전달한다(정수 N / `"?"` / None).
    """
    pm_handoff = _load_tool("pm_handoff")
    if pm_handoff is None:
        return None
    try:
        return pm_handoff.infer_next_session_num(pm_state_text)
    except Exception:  # noqa: BLE001 — fail-soft: 추론 실패는 None(placeholder).
        return None


# ── 차수 log-폴백 + stale 교차검증 (T-0208·ADR-0035) ──────────────────────────
# per-slot pm_state 는 git-ignored 라 머신 간 미동기 — fresh clone(머신 이동)에서 차수가
# per-slot 리셋으로 오표기된다(라이브 실증). log/current.md 는 git 추적(freshness=[[T-0217]] 가
# 최신 담보)이라 handoff entry 제목 `PM N차` 가 차수의 상보 진실이다. handoff entry 로 한정
# 한다 — complete/fix/note 제목은 `PM N차` 부재/불규칙이라 오파싱을 막는다.
# 형식: `## [YYYY-MM-DD] handoff | PM N차 (<repo>_<M>) → 다음 PM 세션`(솔로는 태그 생략 —
# `… PM N차 → …`·`… PM N차 인계 — …`). optional `(?P<session>...)` = 세션 정체성 태그
# (ADR-0044·pm_handoff `_session_tag`) — 슬롯 필터(자기 슬롯 태그 entry 만)의 캡처 그룹.
# 캡처는 두 형식: **canonical 슬롯 `<repo>_<N>`**(`[A-Za-z0-9][A-Za-z0-9_-]*_\d+`·pm_config
# `_REPO_NAME_RE`+`_N`) 또는 **task 태그 `task:<name>`**(F7·T-0356·sentinel `task:` + `[^)]+`).
# 슬롯 형식의 `_\d+` 종결·task 형식의 `task:` 접두 둘 다 서술형 괄호(`PM 4차 (아침 대화)`·`(회의 3)`)를
# 배제한다(should-fix) — 솔로에서 서술 괄호를 세션 태그로 오인해 entry 를 drop 하지 않게. task 명은
# 자유 포맷(한글·공백 허용)이라 bare `(<name>)` 로는 서술 괄호와 오탐이 나므로, pm_handoff 가 태그를
# `(task:<name>)` sentinel 로 박고 여기서 `task:` 로 판별한다(무태그 흡수 회귀 불변). 비-canonical·
# 비-task 괄호는 `.*` 로 흡수돼 session=None(무태그 폴백).
_TASK_TAG_PREFIX = "task:"   # pm_handoff `_TASK_TAG_PREFIX` 미러 (ADR-0013 모듈 격리라 각 모듈 inline).
_LOG_HANDOFF_HEADER_RE = re.compile(
    r"^(?P<line>## \[\d{4}-\d{2}-\d{2}\]\s+handoff\s*\|\s*PM\s+(?P<num>\d+)차"
    r"(?:\s*\((?P<session>[A-Za-z0-9][A-Za-z0-9_-]*_\d+|task:[^)]+)\))?.*)$",
    re.MULTILINE,
)


def _session_owns_untagged(bound_session: str | None) -> bool:
    """무태그 handoff entry 가 이 세션 소유인지 판정한다 — 솔로(None)/slot-1 만 True (ADR-0044).

    무태그 entry = 태그 도입(ADR-0044) 이전 로그 또는 솔로 핸드오프 → 솔로/slot-1 귀속
    (연속성 보존·제로 마이그레이션). **slot-2+ 는 무태그를 무시**하고 자기 태그 entry 만 센다
    (핵심 회귀 가드·codex 제언). bound_session 이 None(솔로)이거나 canonical `<repo>_1`(slot-1)이면
    True, 그 외(slot-2+·비정형 non-None)면 False.

    **task 세션(`task:<name>`·F7·T-0356)은 항상 False** — task 는 자기 태그(`(task:<name>)`) entry 만
    소유하고 태그-도입 이전 무태그 legacy(솔로/slot-1 계보)는 자기 것이 아니다. sentinel 판별을
    trailing `_N` 검사보다 **앞에** 둔다: task 명이 우연히 `_1` 로 끝나면(`task:foo_1`) trailing 검사가
    slot-1 로 오판(True)하기 때문(회귀 가드)."""
    if bound_session is None:
        return True
    if bound_session.startswith(_TASK_TAG_PREFIX):
        return False
    m = re.search(r"_(\d+)$", bound_session)
    return m is not None and int(m.group(1)) == 1


def _handoff_entry_owned(match: re.Match, bound_session: str | None,
                         owns_untagged: bool) -> bool:
    """handoff 헤더 match 가 bound_session 슬롯 소유인지 판정한다 (ADR-0044·슬롯 필터 공용).

    태그 있는 entry → 태그 == bound_session 일 때만 소유(타 슬롯 entry 유입 차단). 태그 없는
    entry → 솔로/slot-1(`owns_untagged`)만 소유. 차수 유도(`parse_last_handoff_session_num`)와
    본문 dump(`extract_slot_handoff_entry`)가 같은 규칙을 공유하게 한 단일 sink.
    """
    tag = match.group("session")
    tag = tag.strip() if tag else None
    if tag:
        return bound_session is not None and tag == bound_session
    return owns_untagged


def parse_last_handoff_session_num(
    log_text: str | None, *, bound_session: str | None = None
) -> int | None:
    """log/current.md 에서 **자기 슬롯** handoff entry 제목의 최고 `PM N차` N 을 반환한다 (T-0253·ADR-0044).

    handoff type entry 로 한정(complete/fix/note 는 제목 `PM N차` 부재/불규칙 — 오파싱 차단).
    `bound_session`(`<repo>_<N>`)이 **양성 해소**되면 자기 슬롯 태그 entry 만 필터해 그 중 최고차
    (append-only 로그라 max=최신)를 취한다 — 전역 max 가 아니다("두 슬롯 같은 N차" 해소·slot-2+ 는
    무태그/타 슬롯 무시). **bound 가 None(사유 무관 — genuine solo 든 미해소든)이면 전역 tag-agnostic
    파싱**(원 T-0208 동작 보존)으로 폴백한다: fresh clone(lease 부재)에 tracked 로그가 태그를 가져도
    log-derived 차수를 잃지 않게(codex R4·본문 dump·user 연속성 surface 와 동일 원리 — 양성 슬롯일 때만
    필터). handoff entry 부재·log 부재/파싱 실패 → None(fail-soft).
    """
    if not log_text:
        return None
    owns_untagged = _session_owns_untagged(bound_session)
    nums: list[int] = []
    for m in _LOG_HANDOFF_HEADER_RE.finditer(log_text):
        # 양성 슬롯 해소 시에만 소유 필터 — bound None 은 전역(태그 무관·차수 유실 방지).
        if bound_session is None or _handoff_entry_owned(m, bound_session, owns_untagged):
            nums.append(int(m.group("num")))
    if not nums:
        return None
    return max(nums)


def extract_slot_handoff_entry(
    log_text: str | None, *, bound_session: str | None = None
) -> dict[str, str] | None:
    """**자기 슬롯**의 마지막 handoff entry(date/type/title + 본문 전체)를 반환한다 (T-0253·ADR-0047 ③).

    부트스트랩 인계 dump 를 전역 마지막 entry 가 아니라 *자기 슬롯의 마지막 handoff* 로 좁힌다
    — 타 슬롯 entry 본문이 컨텍스트에 유입되면 자기 복원이 흐려진다(ADR-0047). 슬롯 필터는
    `parse_last_handoff_session_num` 과 같은 규칙(`_handoff_entry_owned`·무태그=솔로/slot-1).
    단일 진실 = `pm_log.split_entries`(entry 분할)를 동적 로드 재사용(DRY). 자기 슬롯 handoff
    entry 부재·pm_log 부재/파싱 실패 → None(호출부가 전역 마지막으로 graceful 폴백).
    """
    if not log_text:
        return None
    pm_log = _load_tool("pm_log")
    if pm_log is None:
        return None
    try:
        _preamble, entries = pm_log.split_entries(log_text)
    except Exception:  # noqa: BLE001 — fail-soft: 파싱 실패는 None(전역 마지막 폴백).
        return None
    owns_untagged = _session_owns_untagged(bound_session)
    selected: str | None = None
    for _date, entry_text in entries:
        # 각 entry_text 는 `## [..]` 헤더 줄로 시작(split_entries) — 첫 줄만 handoff 헤더다.
        m = _LOG_HANDOFF_HEADER_RE.search(entry_text)
        if m is None:
            continue  # handoff type 아님(complete/note 등) — skip.
        if _handoff_entry_owned(m, bound_session, owns_untagged):
            selected = entry_text  # append-only 라 마지막 매치가 자기 슬롯 최신 handoff.
    if selected is None:
        return None
    entry = parse_log_last_entry(selected)  # 단일 헤더 → 그 entry 의 date/type/title.
    if entry is None:
        return None
    entry = dict(entry)
    entry["body"] = selected.rstrip()
    return entry


def last_handoff_header_line(
    log_text: str | None, *, bound_session: str | None = None
) -> str | None:
    """**자기 슬롯** 마지막 handoff entry 의 헤더 줄 전체를 반환한다 (T-0208·user 연속성 pickaxe needle).

    `parse_last_handoff_session_num` 과 같은 grammar·같은 소유 sink(`_handoff_entry_owned`)로
    헤더 줄을 잡는다 — 그 줄을 담은 commit 을 `git log -S<line>`(pickaxe)로 찾아 author email 을
    얻기 위함. `bound_session`(`<repo>_<N>`·솔로면 None)으로 **자기 슬롯 태그 entry 만 필터**한다
    (T-0253·ADR-0047 "타 슬롯 최소 유입") — slot-2 부트스트랩이 전역 마지막(=slot-1) handoff 로
    "직전 작성자" 를 오판정하지 않게. 무태그=솔로/slot-1 귀속. 부재/파싱 실패 → None.
    """
    if not log_text:
        return None
    owns_untagged = _session_owns_untagged(bound_session)
    last: re.Match | None = None
    for m in _LOG_HANDOFF_HEADER_RE.finditer(log_text):
        # 솔로(bound 진짜 미해소)는 슬롯 개념이 없다 — 전역 마지막 handoff(원 동작 보존·타입/태그 무관).
        # bound 해소 시에만 자기 슬롯 소유 entry 로 필터(slot-2 가 전역 slot-1 작성자를 오판정 안 하게).
        if bound_session is not None and not _handoff_entry_owned(m, bound_session, owns_untagged):
            continue
        last = m
    if last is None:
        return None
    return last.group("line")


def reconcile_session_num(
    state_num: int | str | None, log_next: int | None
) -> tuple[int | str | None, bool]:
    """pm_state-derived 차수와 log-derived *다음* 차수(N+1)를 교차검증한다 (T-0208).

    반환 `(final, stale)` — final 은 int / `"?"` / None(현행 동작 보존), stale 은 pm_state 가
    log 보다 뒤처졌을 때만 True(머신 간 미동기 경고 신호).

    규칙(spike·티켓 인터페이스):
      - state int · log_next int:
          `log_next > state` → `(log_next, True)`   # pm_state stale — log 우선(max) + 경고
          그 외              → `(state, False)`      # 현행(state 우선·회귀 0)
      - state int · log_next None → `(state, False)`      # 현행(log 폴백 없음)
      - state 미해소(`"?"`/None) · log_next int → `(log_next, False)`  # 차수 log-폴백
      - 둘 다 미해소 → `(state, False)`  # `"?"`/None 그대로(placeholder·crash 금지)

    순수 함수 — stale 교차검증(1축)과 폴백(1축)을 한 곳에 모아 단위테스트로 못박는다.
    """
    if isinstance(state_num, int) and isinstance(log_next, int):
        if log_next > state_num:
            return log_next, True
        return state_num, False
    if isinstance(state_num, int):
        return state_num, False
    if isinstance(log_next, int):
        return log_next, False
    return state_num, False


# pm_state "남은 작업 전체 그림" 절 앵커 — 부트스트랩 인계 surface 의 단일 진실(`## ` 헤더).
# pm_handoff 의 세션-window 앵커(`## 세션 식별 …`)와 같은 `## ` 레벨이라, 다음 `## ` 헤더
# 직전(또는 파일 끝)까지가 절 범위다. 형식이 바뀌면 이 상수만 교체.
_REMAINING_WORK_ANCHOR = "## 남은 작업 전체 그림"


def extract_remaining_work_section(pm_state_text: str) -> str | None:
    """pm_state 의 "남은 작업 전체 그림" 절(`### 🔴 다음 세션 — 사용자 발의` 포함) 텍스트를 반환한다.

    T-0179 — 인계 컨텍스트 dump 의 일부. 앵커(`## 남은 작업 전체 그림`)부터 다음 `## ` 헤더
    직전(또는 파일 끝)까지를 한 절로 surface 한다 — 그 안의 `### 🔴 다음 세션 — 사용자 발의`·
    `### 🟡 DEFER`·`### 🔵 장기 이월` 하위 절을 통째로 담는다(다음 세션이 "무엇부터" 를
    바로 보게). pm_handoff `_extract_session_section` 과 동형(앵커 → 다음 동급 헤더)이되
    *읽기 전용 surface* 라 위치 offset 은 불필요해 텍스트만 반환한다.

    앵커 불일치(형식 변경·절 부재) → None — 호출부가 명시 포인터로 graceful 폴백한다.
    """
    anchor_idx = pm_state_text.find(_REMAINING_WORK_ANCHOR)
    if anchor_idx == -1:
        return None
    after_anchor = pm_state_text[anchor_idx + len(_REMAINING_WORK_ANCHOR):]
    next_header = re.search(r"^## ", after_anchor, re.MULTILINE)
    if next_header is None:
        end_offset = len(pm_state_text)
    else:
        end_offset = anchor_idx + len(_REMAINING_WORK_ANCHOR) + next_header.start()
    return pm_state_text[anchor_idx:end_offset].rstrip()


# 차수 announce 머리표 placeholder — pm_state 미해소/추론불가 시 `PM <?>차` (crash 금지).
_SESSION_LABEL_PLACEHOLDER = "PM <?>차"

# fresh 슬롯(첫 바인딩·pm_state·자기 슬롯 handoff 둘 다 부재) 명시 배너 (T-0284). fresh 는
# "복구할 게 없음"이 명확하니, 스크램블 유발 "미해소/직접 확인" placeholder 대신 "새로 시작"을
# 명시해 PM 이 legacy pm_state·git log 를 손으로 뒤지는 토큰 낭비를 차단한다(read/report만·
# 자동 pm_state 생성은 pm_handoff 소관·ADR-0035 경계 유지).
_FRESH_SLOT_BANNER = (
    "🆕 첫 바인딩 슬롯 · 이전 세션 맥락 없음 · 차수=1(fresh) · 폴백 스캔 불요 — "
    "새 PM 세션으로 시작하고 첫 /pm-handoff 가 pm_state 를 생성한다."
)

# task 첫세션(신규 task·pm_state·자기 task handoff 둘 다 부재) 표면 (T-0391). task 는 슬롯 축과
# 직교(⑥)라 슬롯 fresh 배너/1차 강제 대상이 아니어서, 첫세션이 차수 placeholder(`PM <?>차`)+"log
# 없음/파싱 실패"로 나 오류처럼 읽혔다(PM 78 실측·접힘은 설계인데 사유 미표기). 신규 task 사유를
# 명시해 PM 이 없는 인계/pm_state 를 손으로 뒤지는 스크램블을 막는다(fresh 슬롯 표면과 동형·surface-only).
_TASK_FIRST_SESSION_LABEL = "task 1차"
_TASK_FIRST_SESSION_LOG_NOTICE = (
    "(🆕 신규 task — 복구할 인계 없음 · 첫 /pm-handoff 가 pm_state 를 생성)"
)
_TASK_FIRST_SESSION_STATE_NOTICE = (
    "(🆕 신규 task — pm_state 없음 · 첫 /pm-handoff 가 생성 · 복구할 남은작업 없음)"
)


def _slot_count_label(session: str) -> str:
    """`<repo>_<N>` 세션 키 → 카운트 스코프 라벨 `"slot N"` (ADR-0056 #6·T-0312).

    말단 `_<N>` 의 숫자 N 을 뽑아 `"slot N"` 으로 라벨한다 — bootstrap 카운트가 `--mine`(user·전
    슬롯)이 아니라 *그 슬롯 정체성*(`list --session <repo>_<N>`)으로 조회됐음을 announce 한다(S1
    mislabel 근절). 비-슬롯형(말단이 숫자 아님·커스텀 세션명)이면 전체 세션명으로 폴백한다.
    """
    tail = session.rsplit("_", 1)[-1]
    return f"slot {tail}" if tail.isdigit() else f"slot {session}"


def _format_board_counts_line(counts: dict[str, int], scope_label: str = "mine") -> str:
    """board 카운트 한 줄을 만든다 — 수집 스코프 라벨 명확화 (T-0194·T-0312·ADR-0067).

    `counts` 는 `_collect_board` 가 뽑은 스코프 값이다 — status 별로 "내 세션 생성 open" 또는 "내
    세션 claim" 만 센 값이라 실측(예 done 25)이 전체 done(184) 과 크게 다를 수 있다(done/open/
    claimed/blocked 모두 전체보다 훨씬 작을 소지가 큼). `scope_label` 로 그 스코프를 명시해 "전체
    done" 처럼 오독하지 않게 한다 — 솔로/무바인딩은 `"mine"`(user·전 슬롯), 명시 슬롯 바인딩
    (`--repo`/`--slot`·multi-PM)은 `"slot N"`(그 슬롯 정체성으로 조회·ADR-0056 S1·카운트 정합).

    **open 도 세션 스코프**(ADR-0067): 세션 기본 뷰가 open 을 내 세션 생성분만 보이므로(ADR-0066 의
    슬롯무관 공유 backlog 전량·접힘 카운트 폐기) 네 status 모두 같은 `scope_label` 을 붙인다 —
    옛 `_OPEN_SCOPE_LABEL`(공유 backlog 정정) 축이 소멸(open 이 더는 전역 대기열 수가 아님).
    """
    parts = [
        f"{label}: {counts[key]} ({scope_label})"
        for key, label in (
            ("done", "done"), ("open", "open"), ("claimed", "claimed"), ("blocked", "blocked")
        )
    ]
    return "- " + " / ".join(parts)


def _format_board_git_freshness(board_git: dict) -> str:
    """board submodule freshness 한 줄을 만든다 — HEAD·dirty·ahead/behind (T-0195).

    `board_git` = `_collect_board_git()` 반환(`head`·`dirty`·`ahead`·`behind`). ahead/behind
    가 둘 다 None(upstream 미설정/조회불가)이면 그 구간을 생략 — dirty·head 만 있어도
    유의미(부분 degrade). dirty=False 면 "clean", True 면 "dirty".
    """
    parts = [f"HEAD {board_git['head']}", "dirty" if board_git["dirty"] else "clean"]
    ahead, behind = board_git.get("ahead"), board_git.get("behind")
    if ahead is not None and behind is not None:
        parts.append(f"{ahead} ahead / {behind} behind")
    return " · ".join(parts)


def _format_session_label(handoff_ctx: dict | None) -> str:
    """차수 announce 머리표를 만든다 — `PM <N>차` / placeholder (T-0179·crash 금지).

    `handoff_ctx["session_num"]` 가 정수면 `PM <N>차`, `"?"`(entry 부재 placeholder)·None·
    handoff_ctx 부재(pm_state 미해소)면 `PM <?>차` placeholder. self-surface 헤더라 미해소도
    graceful — 부트스트랩 본체를 깨지 않는다(spike §3·T-0178 fail-soft 정합).
    """
    if handoff_ctx is None:
        return _SESSION_LABEL_PLACEHOLDER
    num = handoff_ctx.get("session_num")
    if isinstance(num, int):
        return f"PM {num}차"
    return _SESSION_LABEL_PLACEHOLDER


def _format_stale_warning(handoff_ctx: dict | None) -> str | None:
    """pm_state stale 교차검증 경고 1줄 — log-derived 차수가 pm_state 보다 앞설 때 (T-0208).

    `handoff_ctx["session_stale"]` 가 True(=`reconcile_session_num` 이 log 우선 max 를 택함)면
    경고 문자열, 아니면 None(줄 생략). per-slot pm_state 가 git-ignored 라 머신 간 미동기임을
    표면화한다 — final(log 우선) vs state(뒤처진 pm_state 값) 둘 다 담아 진단 가능하게.
    """
    if not handoff_ctx or not handoff_ctx.get("session_stale"):
        return None
    final = handoff_ctx.get("session_num")
    state = handoff_ctx.get("state_session_num")
    return (
        f"⚠ pm_state stale (머신 간 미동기) — log 기준 PM {final}차 우선 "
        f"(pm_state 는 PM {state}차)"
    )


# ── 슬롯 상태 surface (upstream·submodule 역할·ADR-0051 파일럿 T-β·T-0276) ─────
# 부트스트랩이 현재 슬롯의 상태(branch·upstream·submodule pin/dev-ahead/drift)를 1회
# surface 한다(self-sufficient dump·ADR-0035). 판정은 worktree_pool.slot_status(백본·T-0275
# 역할 판별 재사용)가 하고, 여기선 그 결과(JSON-safe dict)를 markdown 줄로 렌더한다.
# **drift(경고) vs dev-ahead(정보) 구별**(ADR-0051 §Decision 4)이 핵심 — dev 작업 중인
# submodule 을 "문제"로 오표시하면 안 된다.

# submodule kind → 표시 라벨. dev-ahead/pinned = 정보(경고 아님)·drift/uninitialized = 경고(⚠).
_SUBMODULE_KIND_LABEL = {
    "dev-ahead": "dev-ahead(정보)",
    "drift": "drift(pin≠working)",
    "pinned": "pinned",
    "uninitialized": "uninitialized",
}


def slot_status_to_dict(status: object | None) -> dict | None:
    """worktree_pool.SlotStatus(백본 반환)를 JSON-safe dict 로 변환한다 (T-0276·markdown/JSON 공용).

    부트스트랩은 worktree_pool 을 직접 import 하지 않으므로(touches 격리·주입/동적로드된
    풀의 반환을 duck-typing 으로 소비) `getattr` 로 필드를 읽는다 — Lease 를 `.slot`/`.state`
    로 소비하는 것과 동형. None(백본 미제공/실패)은 None 그대로(호출부가 절 생략). identity
    dict 에 이 dict 를 실어 markdown 빌더(`_format_slot_status_lines`)와 JSON 이 함께 쓴다.
    """
    if status is None:
        return None
    return {
        "slot": getattr(status, "slot", None),
        "branch": getattr(status, "branch", None),
        "upstream": getattr(status, "upstream", None),
        "upstream_ok": bool(getattr(status, "upstream_ok", False)),
        "submodules": [
            {
                "path": getattr(sub, "path", None),
                "kind": getattr(sub, "kind", None),
                "warning": bool(getattr(sub, "warning", False)),
                "dirty": bool(getattr(sub, "dirty", False)),
            }
            for sub in (getattr(status, "submodules", None) or [])
        ],
    }


def _format_submodule_token(sub: dict) -> str:
    """submodule 하나를 `` `path` <라벨> `` 토큰으로 렌더한다 (T-0276·경고면 ⚠·dirty 면 ·dirty)."""
    kind = sub.get("kind") or "?"
    label = _SUBMODULE_KIND_LABEL.get(kind, kind)
    mark = "⚠ " if sub.get("warning") else ""
    dirty = " ·dirty" if sub.get("dirty") else ""
    return f"`{sub.get('path')}` {mark}{label}{dirty}"


def _format_slot_status_lines(status: dict | None) -> list[str]:
    """슬롯 상태 dict → markdown 줄 (T-0276·branch·upstream + submodule 역할·submodule 없으면 줄 생략).

    `status` = `slot_status_to_dict` 반환(또는 None). None 이면 빈 리스트(절 생략·백본 미제공).
    - branch·upstream 한 줄 — upstream 미해소면 ⚠ 경고(T-0273/0274 로 슬롯 tracking 설정돼야 정상).
    - submodule 이 있으면 역할 요약 한 줄(dev-ahead=정보·drift=⚠ 경고 구별) — **없으면 줄 생략**.
    """
    if status is None:
        return []
    lines: list[str] = []
    branch = status.get("branch") or "(미지정)"
    if status.get("upstream_ok"):
        upstream = f"`{status.get('upstream')}`"
    else:
        upstream = "⚠ 미해소 (`@{upstream}` 없음 — 슬롯 tracking 미설정·T-0273/0274 확인)"
    lines.append(f"- branch: `{branch}` · upstream: {upstream}")
    submodules = status.get("submodules") or []
    if submodules:
        tokens = " · ".join(_format_submodule_token(sub) for sub in submodules)
        lines.append(f"- submodule: {tokens}")
    return lines


# ── 슬롯 시대차 경고 (T-0341·PM 69 stale-read) ────────────────────────────────
# 슬롯 worktree 의 HEAD 가 base(main) 대비 behind N 커밋이면 옛-시대 코드로 작업할 위험이
# 있다(PM 69 slot-2 실측 — v1.0.6 시대 코드로 작업 직전). identity surface 에 경고 줄을
# 표면화한다. offline(base fetch 실패)면 판정불가 fail-soft. 없으면(최신·base 미해소) 줄 생략.

# 미기록 슬롯(v1.3.0 이전)의 base 후보 — 0단계 미기록 경로가 `git merge-base HEAD <cand>` 로 제시
# **만** 한다(자동 채택 없음·추론 금지·결정 ⑪·T-0352·spike §F9). 흔한 base 브랜치를 훑어 merge-base
# 가 해소되는 것만 후보로 보여주고, 사용자가 `set-base` 로 명시 지정한다(엔진=surface·사용자=결정).
_UNRECORDED_BASE_CANDIDATE_BRANCHES = ("origin/main", "origin/master", "origin/develop")

# main-참조 해소 커맨드가 제시할 **신규 브랜치명** 파생 규칙 (T-0412).
# 세션/task 명을 브랜치명으로 그대로 보간하면 그 이름이 보호브랜치(`main`)이거나 이미 존재하는
# 브랜치일 때 **실행 불가능한 자기모순 커맨드**가 된다(PM 4차 실측: task `main` 진입 시
# `git -C work/project_manager_1 switch -c main` — 지금 체크아웃된 그 보호브랜치를 새로 만들라는 안내).
# 접두는 `task/` 고정 — 슬롯 전용 브랜치 `<repo>_<N>`(worktree_pool 관례)과 네임스페이스가 겹치지
# 않고, PM 4차가 실측 해소에 실제로 쓴 이름이다(`task/main`).
_REMEDY_BRANCH_PREFIX = "task/"
# `task/<preferred>` 까지 충돌하면 `-2`, `-3` … 로 첫 미충돌을 고른다. 상한은 안내 문자열 생성이
# 무한 루프에 빠지지 않기 위한 방어(전부 충돌하면 마지막 후보를 그대로 제시·fail-soft).
_REMEDY_BRANCH_SUFFIX_LIMIT = 20


def _phase0_diverge_reason(result) -> str:
    """0단계 record-vs-live FAIL-LOUD 의 판정 근거 한 줄 (T-0391·surface-only).

    `compare_slot_git` 의 `fail_loud`(branch 변경 또는 head diverged)를 사람이 읽을 수 있는
    사유로 푼다 — `head_relation`/`branch_match` 원값만으로는 PM 이 "왜 diverged 인지"를 코드
    정독으로 재구성해야 했다(PM 78 실측·컨텍스트 낭비). 판정/거동은 안 바꾸고 근거만 표면화한다.
      - branch_match False → 브랜치가 바뀜(세션 중 checkout·외부 개입).
      - branch 동일·head_relation diverged → 같은 브랜치인데 live HEAD 가 기록 커밋의 후손이
        아님(reset·force·rebase 등 되감기/divergent — 내 커밋 위 진행이 아님).
    """
    recorded = getattr(result, "recorded", None) or {}
    live = getattr(result, "live", None) or {}
    if not getattr(result, "branch_match", True):
        return (
            f"기록 branch `{recorded.get('branch')}` ≠ live branch `{live.get('branch')}` — "
            "브랜치가 바뀜(세션 중 checkout 또는 외부 개입)"
        )
    return (
        "같은 브랜치인데 live HEAD 가 기록 커밋의 후손이 아님 — "
        "reset·force·rebase 등 되감기/divergent(내 커밋 위 진행이 아님)"
    )


def _format_slot_era_warning(info: dict | None) -> str | None:
    """슬롯 시대차 info(`_slot_era_info` 반환) → 경고 줄 또는 None (T-0341).

    - `undetermined` True → offline fail-soft("판정불가 — 스냅샷일 수 있음").
    - `behind` > 0 → loud 경고(behind N·권장 액션 1줄).
    - behind 0/None·info None → None(최신/미해소 → 줄 생략·오탐 0).
    """
    if not info:
        return None
    base = info.get("base")
    if info.get("undetermined"):
        return (
            f"- ⚠ **슬롯 시대차 판정불가** — base `{base}` 최신 fetch 실패(offline) · "
            "슬롯 HEAD 가 stale 스냅샷일 수 있음 (온라인 시 재부트스트랩 권장)"
        )
    behind = info.get("behind") or 0
    if behind <= 0:
        return None
    return (
        f"- ⚠ **슬롯 시대차** — HEAD 가 base `{base}` 대비 **behind {behind} 커밋** — "
        f"옛-시대 코드로 작업할 위험 (`git -C <슬롯> rebase origin/{base}` 또는 최신 base 재분기 검토)"
    )


# ── 핵심 흐름 ──────────────────────────────────────────────────────────────

class PmBootstrap:
    """PM 세션 부트스트랩 기계 측정 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    ticket_finish.py 의 TicketFinisher DI 패턴과 동일.
    """

    def __init__(
        self,
        *,
        run_board_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        log_file: Path = LOG_FILE,
        board_py: Path = BOARD_PY,
        areas_file: Path | None = None,
        venv_python: str | Path = _default_python(),
        worktree_pool=None,
        board=None,
        pm_state_file: Path | None = None,
        board_dir: Path | None = None,
    ) -> None:
        self._log_file = log_file
        self._board_py = board_py
        # board submodule 디렉토리(T-0195) — 명시(hermetic 테스트) 없으면 *호출 시점*
        # `REPO / ".project_manager" / "board"` 로 해소한다(board 감지와 동일 앵커·
        # `_areas_file()` 의 `board/tickets` 판정과 동형). freshness 수집이 이 경로가
        # 실 디렉토리(`.git` 존재)인지로 분리(submodule) 여부를 판정한다.
        self._board_dir = board_dir if board_dir is not None else REPO / ".project_manager" / "board"
        # pm_state seam (T-0179·차수 announce + 인계 dump) — bound slot 의 per-slot pm_state.
        # 명시(hermetic 테스트)면 그 경로를 read. None 이면 run() 진입부에서 bound slot 으로
        # 해소(`pm_handoff._pm_state_path` 동적로드·migrate=False·read-only·DRY). 부트스트랩은
        # pm_state 를 *편집하지 않으므로*(read/write 주체는 pm_handoff) 경로만 잡고 텍스트만 읽는다.
        self._pm_state_file = pm_state_file
        # areas_file 미지정이면 `_areas_file()`(board_root 추종·T-0162 A6)로 해소 — board/
        # 분리(ADR-0033 ①) 후 등록영역 surface(_registered_repos)가 stale 안 보게. 명시
        # 인자(hermetic 테스트)는 그대로 존중.
        self._areas_file = areas_file if areas_file is not None else _areas_file()
        self._venv_python = venv_python
        # worktree_pool seam — 테스트는 mock 모듈을 주입(hermetic). None 이면 --repo
        # 경로 진입 시에만 동적 로드(multi-PM 모드)·솔로 무인자 경로는 안 건드린다.
        self._worktree_pool = worktree_pool
        # board seam (T-0076) — 보호 브랜치 surface(`_repo_protected`)용. 테스트는 mock
        # 모듈 주입. None 이면 lean identity(--repo --slot) 경로에서만 동적 로드(소프트
        # 경고·board 부재면 경고 생략). board.py *직접 import* 는 안 함(touches 격리).
        self._board = board

        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_git_fn = run_git_fn or self._default_run_git

        # 바운드 슬롯 식별자(`work/<repo>_<N>`) — git/pytest 러너의 worktree cwd 해소용
        # (T-0125·T-0124 동형). run() 진입부에서 명시 multi-PM 모드면 세팅, 솔로면 None
        # 유지(→ `_worktree_cwd` 가 `_auto_slot` 으로 자동해소). board 러너는 무관(REPO 고정).
        self._bound_slot: str | None = None

        # task 세션 식별자(F7·T-0374) — `--task <이름>` 바인딩 성공 시 run() 진입부에서 세팅
        # (task=슬롯과 직교 축·⑥). log/pm_state 귀속을 task 태그(`task:<이름>`)·task 서술 pm_state
        # (`.local/tasks/<이름>/pm_state.md`)로 좁혀 resume-read(차수·남은작업)를 완결한다(F7 루프).
        # None(무-task)이면 모든 task 분기가 no-op → slot/solo dump 100% 불변(회귀 가드).
        self._task_name: str | None = None
        self._task_pm_state_file: Path | None = None

    # ── git/pytest 러너 cwd 해소 (worktree·T-0125) ───────────────────────

    def _worktree_cwd(self, slot: str | None = None) -> str:
        """git/pytest 를 돌릴 작업 디렉토리를 해소한다 (T-0125·분리된 PM 홈+worktree 모델).

        자기분리(ADR-0027) 토폴로지: 코드/tests=① worktree·board/wiki=② PM 홈. 분리된 PM
        홈(②) 루트에서 bootstrap 을 돌리면 그 자리엔 코드/tests 가 없으므로, git dump·pytest
        회귀는 활성 worktree 슬롯 cwd 에서 돌아야 한다. 이 함수가 그 경로를 해소한다.
        (board 러너는 ② 홈 소유라 이 경로를 쓰지 않는다 — `_default_run_board` 는 REPO 고정.)

        해소 순서 (pm_handoff `_regression_cwd`·T-0124 동형):
          - `slot`(명시 multi-PM `--slot` → `work/<repo>_<N>`) 가 있으면 `REPO / slot`,
          - 없으면 `_auto_slot()` 으로 단일 self-host 슬롯 자동해소(`work/<repo>_<N>`),
          - 그것도 없으면(솔로/모호/부재) **현 `REPO` 폴백** (fail-soft·솔로 무변경).

        `_auto_slot` 은 같은 모듈 함수라 직접 호출한다(동적로드 불요·DRY — 복붙 금지).
        예외/None 은 흡수해 REPO 로 폴백한다(자동해소는 *추가 편의*·강제 아님).
        """
        if slot:
            return str(REPO / slot)
        try:
            auto = _auto_slot()
        except Exception:  # noqa: BLE001 — fail-soft: 판정 실패는 REPO 폴백.
            auto = None
        if auto:
            repo, n = auto
            return str(REPO / f"work/{repo}_{n}")
        return str(REPO)

    def _pm_state_display_path(self) -> str:
        """첫-turn 안내에 쓸 pm_state 경로 (per-slot·솔로 legacy 폴백·T-0166).

        명시 multi-PM 모드(`_bound_slot` = `work/<repo>_<N>`)면 그 슬롯의 per-slot 경로,
        솔로 무인자면 `_auto_slot()` 단일 self-host 자동해소(인스턴스 `_areas_file` 추종),
        둘 다 미해소면 legacy `pm_state.md` 표기(현행 무변경). 모듈 `_pm_state_display_path`
        에 위임한다 — slot 키는 pm_handoff 의 read/write 경로와 동형(`<repo>_<N>`).
        """
        bound = None
        if self._bound_slot and self._bound_slot.startswith("work/"):
            rest = self._bound_slot[len("work/"):]
            m = re.match(r"^(.+)_(\d+)$", rest)
            if m:
                bound = (m.group(1), int(m.group(2)))
        return _pm_state_display_path(bound, self._areas_file)

    # ── 기본 subprocess 구현 ─────────────────────────────────────────────

    def _default_run_board(self, args: list[str]) -> tuple[int, str]:
        # encoding 명시 — board.py 의 한글/이모지 출력을 부모가 cp949 로 디코딩해
        # 크래시하지 않도록 utf-8 고정 (Windows CP949 콘솔 회피).
        result = subprocess.run(
            [str(self._venv_python), str(self._board_py)] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        return result.returncode, result.stdout + result.stderr

    def _default_run_pytest(self) -> tuple[int, str]:
        # encoding 명시 — pytest 의 한글 테스트명 출력을 부모가 cp949 로 디코딩해
        # 크래시하지 않도록 utf-8 고정 (Windows CP949 회피).
        # cwd 는 _worktree_cwd 가 해소한다(T-0125) — 분리된 PM 홈(②)엔 tests/ 가 없으므로
        # 회귀는 활성 worktree 슬롯에서 돌아야 한다(솔로/미세팅이면 REPO 폴백).
        result = subprocess.run(
            [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self._worktree_cwd(self._bound_slot),
        )
        return result.returncode, result.stdout + result.stderr

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        # encoding 명시 — git 의 한글 커밋 메시지/상태 출력을 부모가 cp949 로
        # 디코딩해 크래시하지 않도록 utf-8 고정 (Windows CP949 회피).
        # cwd 는 _worktree_cwd 가 해소한다(T-0125) — 분리된 PM 홈(②)엔 코드 git 이 없으므로
        # git dump 는 활성 worktree 슬롯에서 돌아야 한다(솔로/미세팅이면 REPO 폴백).
        # 네트워크 계열(fetch/pull)만 timeout(T-0217) — present-but-unresponsive 원격이 세션
        # 시작을 수 분 막는 hang 방지. 초과 시 TimeoutExpired 를 fail-soft rc≠0 로 흡수(abort
        # 아님) → freshness 의 `fetched=False`/pull 실패 경고 경로 합류. 로컬 git(rev-parse·
        # log·status·rev-list)은 즉시 끝나므로 timeout 미부여(=None·현행 무변경).
        is_network = bool({"fetch", "pull"} & set(args))
        timeout = GIT_NETWORK_TIMEOUT if is_network else None
        try:
            result = subprocess.run(
                ["git"] + args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self._worktree_cwd(self._bound_slot),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # fail-soft — 원격 무응답을 rc≠0 로 흡수해 부트스트랩이 계속 진행하게 한다.
            return 1, f"[timeout] git {' '.join(args)} 가 {timeout}s 초과 — 원격 무응답(offline?)"
        return result.returncode, result.stdout + result.stderr

    # ── 데이터 수집 ──────────────────────────────────────────────────────

    def _collect_board(self) -> dict:
        """board list(내 것 렌즈·+ done 별도 조회) + lint 결과를 수집한다. `list` 실패만 여전히 sys.exit(1).

        렌즈는 슬롯 정체성에 따라 갈린다 (ADR-0056 S1·T-0312·ADR-0057):
          - **명시 슬롯 바인딩**(`--repo`/`--slot`·multi-PM·`self._bound_slot` 세팅)이면 **그 슬롯
            정체성**으로 조회한다(`list --repo <repo> --slot <N>` = 현재-사용자 ∩ 그 슬롯). 예전엔
            무조건 `list --mine`(user·전 슬롯)로 뽑아 "claimed 4 (mine)" 이 `list --repo REPO --slot 3`
            과 어긋나던 mislabel(S1)을 근절 — 카운트 = 그 세션 뷰와 정합. 라벨 "(slot N)".
          - **솔로/무바인딩**(`_bound_slot` None)이면 현행 `--mine`(내 area open + 내 claim·전
            슬롯) 유지. 솔로(user 미상)는 board 가 전체 open + 내 슬롯 claim 으로 graceful 폴백
            하므로 현행과 사실상 동등(spike §2.D). 라벨 "(mine)".
        전체 보드(contention 가시·backlog 확인·타 세션 claim)는 `board list --all` 로 PM 이 명시
        조회한다(ADR-0067·무인자 기본은 내 세션 스트림만·타 세션 티켓 정보는 기본 dump 에서 완전 제거).

        반환 `counts` 는 이 렌즈 스코프 값이다(T-0194 — 실측 done 25(mine) vs 전체 184 오해).
        라벨 명확화는 빌더(`_format_board_counts_line`)가 `counts_scope` 로 담당한다.

        **done 카운트는 default 뷰(무-`--status`) 비의존**(T-0198 — done-count 0 회귀 fix):
        `board.py list`(무-status)가 done 을 접어(T-0197) 활성 상태(open/claimed/blocked)만
        보여주므로, 위 default 뷰 호출 출력엔 done 행이 아예 없어 `counts["done"]` 이 항상
        0 이었다(T-0194 가 예고한 done surface 를 T-0197 이 무력화). 그래서 done 전용으로
        `["list", "--status", "done", *lens]` 을 **별도 호출**해 그 출력만 파싱한 done 카운트로
        덮어쓴다 — open/claimed/blocked 는 첫 호출(default 뷰) 그대로(그 상태들은 default 뷰에
        이미 있으므로 재조회 불요). `list` 호출은 2회다(default·done).
        """
        # 렌즈 선택 — 명시 슬롯 바인딩이면 그 슬롯 정체성(`--repo <repo> --slot <N>`·ADR-0057)·
        # 아니면 `--mine`. `slot_session`(`<repo>_<N>`)의 말단 `_<N>` 만 잘라 분해한다 — repo 이름에
        # `_` 가 있어도(예: `project_manager`) rpartition 이 *마지막* `_` 에서만 갈라 정확하다.
        slot_session = self._slot_session_name() if self._bound_slot else None
        if slot_session:
            repo_part, _, num_part = slot_session.rpartition("_")
            if repo_part and num_part.isdigit():
                lens = ["--repo", repo_part, "--slot", num_part]
            else:
                # 비-슬롯형(커스텀 세션명·말단이 숫자 아님) — decomposed 표면엔 대응 불가한
                # 드문 엣지라 `--mine` 으로 안전 폴백한다(라벨은 그대로 slot_session 기반 유지).
                lens = ["--mine"]
            counts_scope = _slot_count_label(slot_session)
        else:
            lens = ["--mine"]
            counts_scope = "mine"

        rc, output = self._run_board_fn(["list", *lens])
        if rc != 0:
            print(f"[중단] board.py list 실패 (rc={rc}):\n{output}", file=sys.stderr)
            sys.exit(1)

        # 세션 기본 뷰(ADR-0067): 이 렌즈 조회(`list --repo X --slot N` 또는 `--mine`)가 곧 **내 세션
        # 스트림**(내 세션 생성 open + 내 세션 claim)이다 — board.py 가 타 세션분을 완전 비노출한다.
        # `open_tickets`·`counts` 는 그 스트림 모수 그대로다(ADR-0066 의 `--all` 전량 정렬·접힘 카운트·
        # 타 세션 claim 현황은 폐기 — 타 세션 티켓 정보는 기본 dump 에서 완전 제거·명시 `--all` 몫).
        counts = parse_board_counts(output)
        open_tickets = parse_open_tickets(output)

        # done 전용 재조회 — default 뷰가 done 을 접어(T-0197) 위 counts["done"] 이 항상 0
        # 이 되는 회귀를 막는다. 이 호출이 실패해도(구버전 board.py 등) done=0 으로 fail-soft
        # 하고 abort 하지 않는다(핵심 list 는 이미 성공했으므로 done 카운트만 저하 없는 선에서).
        done_rc, done_output = self._run_board_fn(["list", "--status", "done", *lens])
        if done_rc == 0:
            counts["done"] = parse_board_counts(done_output)["done"]

        # `--gate` 로 호출 — 차단 카테고리에만 rc=1, advisory(status drift·
        # unstable-ref-advice)는 rc=0 (board.cmd_lint). dump-then-warn(T-0195) —
        # blocking 이어도 여기서 abort 하지 않고 플래그만 실어 반환한다.
        lint_rc, lint_output = self._run_board_fn(["lint", "--gate"])
        lint_result = parse_lint_result(lint_output)

        return {
            "counts": counts,
            "counts_scope": counts_scope,
            "open_tickets": open_tickets,
            "lint": lint_result,
            "lint_blocking": lint_rc != 0,
            "lint_gate_output": lint_output,
        }

    def _collect_pytest(self) -> dict | None:
        """pytest 회귀를 실행하고 결과를 반환한다.

        default 는 skip — 호출자가 with_pytest=True 일 때만 호출한다.
        실패 시 sys.exit(1).
        """
        rc, output = self._run_pytest_fn()
        parsed = parse_pytest_counts(output)
        if parsed is None:
            print(
                f"[중단] pytest 출력 파싱 실패 (rc={rc}):\n{output}",
                file=sys.stderr,
            )
            sys.exit(1)
        passed, total = parsed
        return {"passed": passed, "total": total, "output": output}

    def _collect_git(self) -> dict:
        """git 브랜치·최근 3 commit·working tree 상태를 수집한다.

        빈 repo(커밋 0 — fresh clone `pm_import --new` 직후)는 **정상 케이스**로
        degrade 한다: branch 는 `symbolic-ref` 폴백, commits 는 빈 목록 +
        `no_commits=True`. **진짜 git repo 가 아닐 때만** (symbolic-ref·status 전부
        실패) sys.exit 로 중단한다. 빈-repo 판정은 commits==[] 기준 (로캘 독립 —
        git 메시지 텍스트를 파싱하지 않는다).
        """
        # ── branch: rev-parse 실패 시 symbolic-ref 폴백 (빈 repo서 동작 실측) ──
        branch_rc, branch_out = self._run_git_fn(["rev-parse", "--abbrev-ref", "HEAD"])
        if branch_rc == 0:
            branch = parse_git_branch(branch_out)
        else:
            # 빈 repo(커밋 0)서 rev-parse rc≠0 — symbolic-ref 로 HEAD 가 가리키는
            # 브랜치명을 얻는다 (커밋 없이도 rc 0). 둘 다 실패해야 git repo 아님.
            sym_rc, sym_out = self._run_git_fn(["symbolic-ref", "--short", "HEAD"])
            if sym_rc != 0:
                print(
                    f"[중단] git repo 아님 — rev-parse(rc={branch_rc})·symbolic-ref(rc={sym_rc}) 모두 실패:\n{sym_out}",
                    file=sys.stderr,
                )
                sys.exit(1)
            branch = parse_git_branch(sym_out)

        # ── commits: log 실패(빈 repo "아직 커밋 없음" 포함)는 빈 목록으로 degrade ──
        log_rc, log_out = self._run_git_fn(["log", "--oneline", "-5"])
        # rc≠0 면 빈 repo로 보고 commits=[] — 별도 메시지 파싱 불요(로캘 독립).
        commits = parse_git_log(log_out) if log_rc == 0 else []
        no_commits = commits == []

        # ── status: 빈 repo서도 rc 0 동작 — 실패는 git repo 부재 신호 ──
        status_rc, status_out = self._run_git_fn(["status", "--short"])
        if status_rc != 0:
            print(
                f"[중단] git repo 아님 — git status 실패 (rc={status_rc}):\n{status_out}",
                file=sys.stderr,
            )
            sys.exit(1)
        working_tree = parse_git_status(status_out)

        return {
            "branch": branch,
            "commits": commits,
            "no_commits": no_commits,
            "working_tree": working_tree,
        }

    def _collect_board_git(self) -> dict | None:
        """board submodule(`.project_manager/board`) 의 HEAD·dirty·ahead/behind 를 수집한다 (T-0195).

        board 는 `ignore=all` git submodule(ADR-0033 ①) 이라 부모 repo `git status` 가
        board 의 git 상태를 통째로 숨긴다 — multi-PM 에서 board = claim 즉시-sync 공유채널
        (T-0163) 이라 board 가 stale 하면 남의 claim 을 놓칠 수 있다(freshness load-bearing).

        `self._board_dir`(`.project_manager/board`) 가 **실 디렉토리가 아니면**(솔로/board
        미분리) None 을 반환해 graceful skip 한다(빌더가 Git 섹션에 이 줄을 생략). 실
        디렉토리여도 `-C <board_dir>` git 호출이 전부 실패하면(예: `.git` 없음·손상) 마찬가지로
        None(fail-soft — board freshness 는 *추가 인지*이지 부트스트랩 본체를 막지 않는다).

        반환: {"head": "<sha7>", "dirty": bool, "ahead": int|None, "behind": int|None} 또는 None.
        ahead/behind 는 upstream 미설정/조회불가 시 둘 다 None(dirty·head 는 여전히 유효할 수
        있음 — 부분 degrade).
        """
        if not self._board_dir.is_dir():
            return None

        head_rc, head_out = self._run_git_fn(
            ["-C", str(self._board_dir), "rev-parse", "--short", "HEAD"]
        )
        if head_rc != 0:
            return None
        head = head_out.strip()

        status_rc, status_out = self._run_git_fn(
            ["-C", str(self._board_dir), "status", "-s"]
        )
        dirty = status_rc == 0 and bool(status_out.strip())

        ahead: int | None = None
        behind: int | None = None
        ab_rc, ab_out = self._run_git_fn(
            ["-C", str(self._board_dir), "rev-list", "--left-right", "--count", "HEAD...@{u}"]
        )
        if ab_rc == 0:
            parsed = parse_git_ahead_behind(ab_out)
            if parsed is not None:
                ahead, behind = parsed

        return {"head": head, "dirty": dirty, "ahead": ahead, "behind": behind}

    # ── git freshness: fetch + behind/ahead + clean·ff 자동 ff-pull (T-0217) ────

    def _freshness_scopes(self) -> list[tuple[str, Path, bool]]:
        """freshness 대상 git 컨텍스트 목록 — (label, dir, is_home) (T-0217).

        ②(PM 홈·REPO)·①(worktree·`_worktree_cwd`)를 각각 fetch/pull 대상으로 한다.
        자기분리(ADR-0027)가 아닌 솔로(둘이 같은 디렉토리)면 하나로 접는다 — 같은 repo 를
        두 번 fetch/pull 하지 않게. ②(is_home)만 board 서브모듈 rider 를 붙인다.
        """
        repo_dir = REPO
        wt_dir = Path(self._worktree_cwd(self._bound_slot))
        scopes: list[tuple[str, Path, bool]] = [("② PM 홈", repo_dir, True)]
        if wt_dir.resolve() != repo_dir.resolve():
            scopes.append(("① worktree", wt_dir, False))
        return scopes

    def _probe_git_freshness(self, label: str, scope_dir: Path) -> dict:
        """한 git 컨텍스트를 fetch origin 후 branch/dirty/ahead·behind 로 측정한다 (T-0217·pull 없음).

        모든 호출은 `-C <dir>` 로 명시(러너 cwd 무관·`_collect_board_git` 동형·DI 보존).
        fetch 실패는 fail-soft(fetched=False·이후 측정은 stale local 기준). `state` 는
        `freshness_decision` — 호출부가 `state == "동기"` 로 clean·ff 인지 판정해 pull 한다.
        """
        scope: dict = {
            "label": label, "dir": str(scope_dir), "fetched": None,
            "branch": None, "detached": False, "dirty": None,
            "ahead": None, "behind": None, "pulled": False,
            "note": None, "state": None,
        }
        d = str(scope_dir)
        f_rc, _f_out = self._run_git_fn(["-C", d, "fetch", "origin"])
        scope["fetched"] = f_rc == 0
        # full ref(`--short` 아님) → `refs/heads/` 접두 정확 제거로 순수 브랜치명 (T-0377 계보·동명
        # 태그 존재 시 `--short` 가 `heads/<name>` 로 표시를 오염시키던 모호성 회피).
        b_rc, b_out = self._run_git_fn(["-C", d, "symbolic-ref", "HEAD"])
        b_ref = b_out.strip()
        if b_rc == 0 and b_ref.startswith(_SYMREF_BRANCH_PREFIX):
            scope["branch"] = b_ref[len(_SYMREF_BRANCH_PREFIX):]
        else:
            # symbolic-ref 실패(rc≠0)·비-브랜치 ref = detached HEAD — 자동 pull 대상 아님.
            scope["detached"] = True
        # dirty 는 tri-state (codex must-fix): True(변경 있음)·False(확정 clean)·None(status
        # 조회 실패=clean 미확인). None 은 자동 pull 을 막는다(freshness_decision `dirty is False`).
        s_rc, s_out = self._run_git_fn(["-C", d, "status", "-s"])
        scope["dirty"] = bool(s_out.strip()) if s_rc == 0 else None
        ab_rc, ab_out = self._run_git_fn(
            ["-C", d, "rev-list", "--left-right", "--count", "HEAD...@{u}"]
        )
        if ab_rc == 0:
            parsed = parse_git_ahead_behind(ab_out)
            if parsed is not None:
                scope["ahead"], scope["behind"] = parsed
        scope["state"], _do_pull = freshness_decision(
            fetched=scope["fetched"], detached=scope["detached"], dirty=scope["dirty"],
            ahead=scope["ahead"], behind=scope["behind"],
        )
        return scope

    def _sync_scope(self, label: str, scope_dir: Path, *, is_home: bool = False) -> dict:
        """한 git 컨텍스트를 측정하고 clean·ff 면 `git pull --ff-only` 로 자동 동기한다 (T-0217).

        pull 실패는 fail-soft(경고 노트 실어 계속·abort 안 함). dirty·diverged·detached 는
        pull 하지 않고 `⚠ behind N — 수동 동기 필요` 경고만. ②(is_home)는 board 서브모듈
        rider(fetch + branch 유지 pull)를 함께 돌려 `scope["board_sync"]` 로 반환한다.
        """
        scope = self._probe_git_freshness(label, scope_dir)
        d = str(scope_dir)
        # state=="동기" 는 안전조건(fetch 성공·확정 clean·로컬 ahead 0)을 전부 통과했다는
        # 뜻이다(freshness_decision — 단일 안전 게이트). 그 밖은 pull 하지 않고 경고만.
        if scope["state"] == "동기":
            was_behind = scope["behind"]
            p_rc, _p_out = self._run_git_fn(["-C", d, "pull", "--ff-only"])
            if p_rc == 0:
                scope["pulled"] = True
                scope["behind"] = 0
                scope["note"] = f"ff-pull 동기 완료 (behind {was_behind}→0)"
            else:
                scope["note"] = f"⚠ pull --ff-only 실패 (rc={p_rc}) — 수동 확인"
        elif scope["behind"] and scope["behind"] > 0:
            scope["note"] = _behind_warning(scope)
        if is_home:
            scope["board_sync"] = self._sync_board_submodule()
        return scope

    def _sync_board_submodule(self) -> dict | None:
        """board 서브모듈 fetch + branch 유지 ff-pull (T-0217·② rider).

        board 미분리(솔로·`_board_dir` 비-디렉토리)면 None(skip). fetch 상시(fail-soft),
        clean·ff 면 `checkout <branch> && pull --ff-only` 로 branch 를 유지한 채 동기한다 —
        `git submodule update` 의 detached HEAD 를 피한다(그러면 이후 board mutation 부기가
        [[T-0203]] sentinel 로 스킵된다). dirty·diverged·detached 는 표면화만(pull 안 함).
        """
        board_dir = self._board_dir
        if not board_dir.is_dir():
            return None
        scope = self._probe_git_freshness("② board 서브모듈", board_dir)
        d = str(board_dir)
        # state=="동기" = 안전조건(fetch 성공·확정 clean·ff) 통과(freshness_decision 단일 게이트).
        if scope["state"] == "동기" and scope["branch"]:
            was_behind = scope["behind"]
            # branch 유지 동기 — detached HEAD 회피(T-0203 sentinel·ticket memo). submodule
            # update 대신 그 branch 를 checkout 하고 ff-pull 한다.
            co_rc, _co = self._run_git_fn(["-C", d, "checkout", scope["branch"]])
            p_rc, _p = self._run_git_fn(["-C", d, "pull", "--ff-only"])
            if co_rc == 0 and p_rc == 0:
                scope["pulled"] = True
                scope["behind"] = 0
                scope["note"] = f"board ff-pull 동기 완료 (branch 유지·behind {was_behind}→0)"
            else:
                scope["note"] = (
                    f"⚠ board 동기 실패 (checkout rc={co_rc}·pull rc={p_rc}) — 수동 확인"
                )
        elif scope["behind"] and scope["behind"] > 0:
            scope["note"] = _behind_warning(scope)
        return scope

    def _collect_freshness(self) -> list[dict]:
        """②·① 각 scope 를 fetch → behind/ahead → clean·ff 면 ff-pull 한다 (T-0217).

        솔로(②=①)면 한 scope 로 접힌다. 반환 목록을 Git 절 freshness surface 에 쓴다.
        부트스트랩 *첫 단계*로 돌려(run() 순서) 이후 git/board 수집이 pull 반영 상태를
        dump 하게 한다. 실 fetch/pull 은 DI 러너(`_run_git_fn`) 경유 — 테스트는 mock.
        """
        return [
            self._sync_scope(label, scope_dir, is_home=is_home)
            for label, scope_dir, is_home in self._freshness_scopes()
        ]

    def _bound_session_name(self) -> str | None:
        """차수 유도/본문 dump 귀속용 bound 세션 키 — **log/pm_state 귀속 필터** 전용 (ADR-0044·F7·T-0374).

        **task 모드(`--task <이름>`·F7)면 `task:<이름>` 태그**를 반환한다 — handoff write 측이
        task 세션 종료를 `(task:<이름>)` sentinel 태그로 박고 task 서술 pm_state 를 쓰므로
        (T-0356), 이 read 측이 같은 키로 자기 태그 entry(차수·본문)를 되읽어 F7 resume 루프를
        닫는다(write↔read 대칭). task 는 슬롯 축과 직교(⑥)라 `--task X --repo Y --slot N` 조합도
        log/pm_state 귀속은 task 가 앵커다(handoff write 측 `session_identity=task` 와 정합).

        task 무설정이면 슬롯/auto 정체성으로 폴백한다(`_slot_session_name`·slot/solo 반환 불변).
        board/lease/대시보드 스코프는 task 와 무관한 슬롯 축이라 그쪽은 `_slot_session_name` 을
        직접 쓴다 — 이 함수는 log/pm_state 귀속(연속성)만 담당한다.
        """
        if self._task_name is not None:
            return f"{_TASK_TAG_PREFIX}{self._task_name}"
        return self._slot_session_name()

    def _slot_session_name(self) -> str | None:
        """슬롯/auto 정체성 키(`<repo>_<N>`) — board/lease/대시보드 스코프 전용 (task-무관·ADR-0044).

        명시 multi-PM 모드(`_bound_slot`=`work/<repo>_<N>`)면 `work/` 접두를 벗긴 `<repo>_<N>`.
        무인자(솔로)면 handoff **write 측**(`_resolve_session_worktree_slot`)과 **같은 경로로**
        자동해소한다 — 단일 self-host 무인자 부트스트랩이 그 슬롯(`<repo>_<N>`)으로 바인딩되어,
        handoff 가 무인자로 write 한 자기 태그 entry(+무태그 히스토리)를 **둘 다** 자기 것으로
        되읽는다. 이 write↔read 대칭이 없으면(구: 무인자→None→솔로 read) 단일 self-host 에서
        handoff 가 쓴 `(<repo>_1)` 태그 entry 를 부트스트랩이 버려 차수 유실·T-0208 stale 침묵
        무력화가 났다(adopter#0 실증 버그). 등록 repo 0개(진짜 솔로)·모호·판정불가 → None.

        **task 와 직교** (T-0374): board 카운트 렌즈·lease/대시보드 자기-제외는 task 태그가 아니라
        슬롯 정체성으로 가른다(task+슬롯 조합에서 자기 슬롯을 "타 PM" 으로 오표시하는 것 방지). task
        무설정이면 `_bound_session_name` 과 값이 같아 slot/solo 경로 100% 불변이다.
        """
        if self._bound_slot:
            if self._bound_slot.startswith("work/"):
                return self._bound_slot[len("work/"):]
            return self._bound_slot
        return self._auto_bound_session()

    def _auto_bound_session(self) -> str | None:
        """무인자 단일 self-host 자동바인딩 세션 키 — handoff `_resolve_session_slot` 재사용 (MF-1·DRY).

        handoff write 측이 태그를 만들 때 쓰는 바로 그 resolver(`_resolve_session_slot`·guarded
        default-1·T-0178)를 재사용해 **write↔read 대칭**을 보장한다(`{1,2}`→slot-1 등 default-1
        규칙까지 동일). leases 는 *호출 시점* REPO 기준 재구성(monkeypatch 추종·hermetic·
        `_pm_state_display_path` 동형). 부트스트랩 READ 는 모호(`SlotResolutionError`)·판정불가를
        **crash 없이** 솔로(None)로 폴백한다 — 모호를 fail-loud 로 막는 건 handoff WRITE 만
        (bootstrap 은 read-only surface·`_resolve_pm_state_file` 폴백 동형).
        """
        leases_file = REPO / ".project_manager" / ".local" / "worktree-leases.json"
        try:
            auto = _resolve_session_slot(self._areas_file, leases_file)
        except SlotResolutionError:
            return None  # 진짜 모호(멀티-PM under-specified) → 솔로 폴백(read crash 금지).
        except Exception:  # noqa: BLE001 — fail-soft: 판정 실패는 솔로 폴백(현행 무변경).
            return None
        if not auto:
            return None
        repo, n = auto
        return f"{repo}_{n}"

    def _collect_log_entry(self) -> dict | None:
        """**자기 슬롯**의 마지막 handoff entry 제목(date·type·title) + **본문 전체**를 수집한다.

        T-0179·T-0253(ADR-0047 ③) — 인계 컨텍스트 dump 를 전역 마지막 entry 가 아니라 *자기
        슬롯의 마지막 handoff*(태그 필터·무태그 폴백=솔로/slot-1)로 좁혀 자기 컨텍스트를 복원한다
        (타 슬롯 entry 본문 유입 차단). **MF-2**: bound 세션이 해소됐는데 자기 handoff 가 0개
        (fresh 슬롯)면 **전역 마지막으로 폴백하지 않는다** — 그러면 타 슬롯 handoff 본문·branch 가
        유입되고 `reattach_warning` 이 타 슬롯로 오경고한다("타 슬롯 최소 유입" 정면 위반). fresh
        슬롯은 이 슬롯의 첫 세션이라 인계 dump 없음(None). 전역 폴백은 bound 가 **진짜 미해소**
        (솔로·식별 자체 없음)일 때만 — 그땐 현행 표시(전역 마지막 제목+본문)를 잃지 않는다.
        """
        if not self._log_file.exists():
            return None
        log_text = self._log_file.read_text(encoding="utf-8")
        bound_session = self._bound_session_name()
        # 솔로(bound 진짜 미해소)는 슬롯 개념이 없다 — T-0179 규칙대로 **마지막 entry(모든 타입)**를
        # 그대로 dump 한다(handoff·complete·decide 무관). handoff-우선 필터를 태우면 최신 complete
        # ("wave 진행 중" 신호)를 과거 handoff 로 가려 현행 표시가 깨진다(codex R2·타입 무관 동작 보존).
        if bound_session is None:
            entry = parse_log_last_entry(log_text)
            if entry is None:
                return None
            entry = dict(entry)
            entry["body"] = extract_last_log_entry_body(log_text)
            return entry
        # bound 해소됨(단일 self-host·명시 multi-PM) — 자기 슬롯 handoff entry 우선(ADR-0047 ③).
        # complete/decide 는 슬롯 태그가 없어 슬롯 귀속 불가라, 슬롯 인계 연속성은 마지막 자기-슬롯
        # handoff 가 최선이다(전역 complete 를 dump 하면 타 슬롯 산출이 유입).
        slot_entry = extract_slot_handoff_entry(log_text, bound_session=bound_session)
        if slot_entry is not None:
            return slot_entry
        # 자기 handoff 0개(fresh 슬롯) → 전역 폴백 금지(MF-2·타 슬롯 유입 차단). 이 슬롯 첫 세션이라
        # 인계 dump 없음 — reattach 도 None(타 슬롯 branch 오경고 차단).
        return None

    def _resolve_pm_state_file(self) -> Path | None:
        """bound slot 의 per-slot pm_state 경로를 해소한다 (read-only·T-0179).

        명시 주입(`self._pm_state_file`·hermetic 테스트)이 있으면 그대로. 없으면
        `pm_handoff._pm_state_path`(per-slot·솔로 legacy 폴백·T-0166)를 **동적 로드**해
        해소한다(복붙 금지·DRY). `migrate=False` — 부트스트랩은 pm_state 를 *읽기만* 하므로
        legacy → slot 이동(부작용)을 절대 하지 않는다(읽기 위치만). 명시 multi-PM 모드면
        `self._bound_slot`(`work/<repo>_<N>`)을 슬롯 인자로 넘겨 그 슬롯 pm_state 를 본다.

        **양성 슬롯 바인딩(`self._bound_slot`)이면 legacy 폴백을 금지**한다 (T-0253·codex R5·
        ADR-0047 "타 슬롯 최소 유입"): 자기 슬롯 pm_state 가 없을 때 `_pm_state_path` 는
        legacy `wiki/pm_state.md`(솔로/slot-1 상태)로 폴백하는데, 그러면 fresh slot-2 가 타 슬롯
        차수·남은작업을 가져와 "fresh=1차" 를 깬다. 해소 경로가 자기 슬롯 디렉토리
        (`.local/slots/<slot>/`) 밖(=legacy 폴백)이면 자기 pm_state 부재로 보고 None 을 반환한다
        (fresh 슬롯·surface 생략·1차 규칙은 `_collect_handoff_context` 층). 솔로(bound None)는
        legacy 가 자기 것(slot-1 계보)이라 현행 폴백을 유지한다(로그 surface 와 동일 원리).
        pm_handoff 부재/해소 실패 → None(소프트 — 차수/남은작업 surface 생략).
        """
        if self._pm_state_file is not None:
            return self._pm_state_file
        # task 모드(F7·T-0374) — 연속성 앵커가 slot→task 로 이동한 세션은 task 서술 pm_state
        # (`.local/tasks/<이름>/pm_state.md`)를 읽는다(handoff write 측 `_task_pm_state_file` 미러).
        # 슬롯 축과 직교(⑥)라 슬롯 해소·legacy 폴백 앞단에 둔다(`--task X --repo Y --slot N` 조합도
        # log/pm_state 는 task 앵커). 파일 부재(첫 세션)는 그대로 반환 — `_collect_handoff_context` 가
        # `.exists()` 로 걸러 차수/남은작업 surface 를 생략하고 현행 포인터 fallback(task identity)만 남긴다.
        if self._task_name is not None:
            return self._task_pm_state_file
        pm_handoff = _load_tool("pm_handoff")
        if pm_handoff is None:
            return None
        try:
            path = pm_handoff._pm_state_path(self._bound_slot, migrate=False)
        except Exception:  # noqa: BLE001 — fail-soft: 해소 실패는 None(surface 생략).
            return None
        # 양성 슬롯인데 해소가 per-slot 디렉토리(`.local/slots/<slot>/pm_state.md`)가 아니면 =
        # 자기 pm_state 부재로 legacy 폴백된 것 → 타 슬롯/솔로 상태 유입 금지·None(fresh).
        if self._bound_slot is not None and path.parent.parent.name != "slots":
            return None
        return path

    def _collect_handoff_context(self, log_text: str | None = None) -> dict | None:
        """bound slot pm_state 차수(+ log 교차검증) + "남은 작업/사용자발의" 절을 수집한다 (T-0179·T-0208).

        반환: {"session_num": int|str|None, "session_stale": bool,
               "state_session_num": int|str|None, "remaining_work": str|None,
               "state_path": str} 또는 None(pm_state·log 둘 다 미해소 — surface 생략·graceful).
        - `session_num`: pm_state(`infer_session_num`) 와 log(`parse_last_handoff_session_num`+1)
          을 `reconcile_session_num` 으로 교차검증한 *최종* 차수. pm_state 해소 경로가 우선이되
          (현행 무변경), pm_state 미해소(template/부재)면 log-derived N+1 로 **폴백**하고, pm_state
          가 해소돼도 log-derived 가 더 크면 **log 우선(max)** + stale 표시(T-0208·머신 간 미동기).
        - `session_stale`: pm_state 가 log 보다 뒤처져 log 를 택했으면 True(경고 1줄).
        - `state_session_num`: pm_state-derived 원값(stale 경고 메시지의 진단용).
        - `remaining_work`: `extract_remaining_work_section`(절 통째) 또는 None(명시 포인터 폴백).
        pm_state·log 둘 다 미해소면 None — 호출부가 현행(placeholder/명시 포인터)로 폴백한다.
        """
        state_path = self._resolve_pm_state_file()
        state_text: str | None = None
        if state_path is not None and state_path.exists():
            try:
                state_text = state_path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — fail-soft: read 실패는 pm_state 미해소로 취급.
                state_text = None

        # log-derived 차수 폴백/교차검증 (T-0208·T-0253) — 자기 슬롯 handoff `PM N차` → N+1.
        # bound 세션 태그 필터로 전역 max 가 아니라 슬롯 max 를 쓴다(ADR-0044·"두 슬롯 같은 N차").
        bound_session = self._bound_session_name()
        state_num = infer_session_num(state_text) if state_text is not None else None
        log_num = parse_last_handoff_session_num(log_text, bound_session=bound_session)
        log_next = log_num + 1 if log_num is not None else None
        session_num, session_stale = reconcile_session_num(state_num, log_next)

        # task 세션(`task:<이름>`·F7·T-0374)은 fresh 슬롯 배너/1차 강제 대상이 **아니다** —
        # DoD "첫 세션(파일 부재)은 현행 포인터 fallback". task 첫 세션(pm_state·log 둘 다 미해소)은
        # 슬롯-first 1차/fresh 배너(슬롯 용어) 대신 handoff_ctx=None 으로 접혀 task identity surface
        # (T-0353 포인터)만 fallback 으로 남는다. task resume 은 log 태그(`task:<이름>`) entry 로 차수를
        # 추론하므로(log_num→N+1=int) 아래 int 강제 분기가 불필요해 이 gate 는 resume 을 안 깬다.
        is_task_session = (
            bound_session is not None and bound_session.startswith(_TASK_TAG_PREFIX)
        )

        # fresh 슬롯 규칙 (ADR-0044): 명시 슬롯 바인딩인데 자기 슬롯 차수가 전혀 안 잡히면
        # (pm_state·log 둘 다 미해소) 1차부터 — slot-2+ 는 무태그 기존 로그를 무시하므로 fresh
        # 슬롯이 placeholder(`?`) 대신 슬롯-first(1차)로 announce 된다. 솔로(bound None)는 현행
        # placeholder 보존(회귀 0) — 이 규칙은 명시 슬롯(bound_session 해소·task 제외)에서만 발동한다.
        if bound_session is not None and not is_task_session and not isinstance(session_num, int):
            session_num = 1

        # fresh 슬롯 판정 (T-0284): 명시 슬롯이 바인딩됐는데 자기 pm_state 도(파일 부재) 자기 슬롯
        # handoff 도(log_num None) 전혀 없으면 = 이 슬롯의 첫 세션이라 복구할 컨텍스트가 없다. 이땐
        # "미해소/직접 확인" placeholder(스크램블 유발) 대신 명시 "fresh" 배너를 dump하도록 빌더에
        # 신호한다(surface-only·자동 pm_state 생성 안 함·ADR-0035). 솔로(bound None)·task(F7·포인터
        # fallback)는 슬롯 fresh 배너 대상 아님(현행 placeholder/포인터 보존·회귀 0).
        fresh_slot = (
            bound_session is not None and not is_task_session
            and state_text is None and log_num is None
        )

        remaining_work = (
            extract_remaining_work_section(state_text) if state_text is not None else None
        )

        # pm_state·log 둘 다 아무 신호도 없으면(차수 미해소 + 남은작업 부재 + pm_state 부재) None
        # — 현행 graceful(surface 생략)과 동형. log-derived 만 있어도 dict 를 내 폴백을 살린다.
        if session_num is None and remaining_work is None and state_text is None:
            return None
        return {
            "session_num": session_num,
            "session_stale": session_stale,
            "state_session_num": state_num,
            "remaining_work": remaining_work,
            "state_path": str(state_path) if state_path is not None else "pm_state.md",
            "fresh_slot": fresh_slot,
        }

    # ── user 연속성 surface (T-0208·ADR-0033 ③) ──────────────────────────

    def _read_log_text(self) -> str | None:
        """log/current.md 원문을 읽는다 (T-0208·차수 log-폴백/user 연속성 공용·fail-soft None)."""
        if not self._log_file.exists():
            return None
        try:
            return self._log_file.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 — fail-soft: read 실패는 None(surface 생략).
            return None

    def _current_user(self) -> str | None:
        """현재 user 식별자 — `board.user_name()`(local.conf user > git config email) (T-0208).

        board 모듈(주입/동적로드)의 `user_name` 을 getattr 로 쓴다(직접 import 금지·touches 격리·
        `_protected_warning` 동형·DI 보존). board 부재/헬퍼 부재/예외 → None(fail-soft·줄 생략).
        """
        board_mod = self._board or _load_board()
        user_name = getattr(board_mod, "user_name", None) if board_mod else None
        if user_name is None:
            return None
        try:
            return user_name()
        except Exception:  # noqa: BLE001 — fail-soft: 해소 실패는 None(줄 생략).
            return None

    def _handoff_commit_author(self, handoff_header: str) -> str | None:
        """직전 handoff entry 헤더 줄을 담은 commit 의 author email (T-0208·pickaxe).

        `git log -1 --format=%ae -S<header> -- <log_file>` — 그 헤더 줄을 추가한 commit 을
        pickaxe(-S)로 찾아 author email 을 얻는다. log/current.md 는 REPO(② PM 홈)가 소유하므로
        `-C <REPO>` 로 명시(러너 cwd=worktree 무관·`_collect_board_git` 동형). rc≠0·빈 출력·git
        실패 → None(fail-soft·줄 생략). DI 러너(`_run_git_fn`) 경유 — 테스트는 mock.
        """
        rc, out = self._run_git_fn([
            "-C", str(REPO), "log", "-1", "--format=%ae",
            f"-S{handoff_header}", "--", str(self._log_file),
        ])
        if rc != 0:
            return None
        stripped = out.strip()
        if not stripped:
            return None
        return stripped.splitlines()[0].strip() or None

    def _collect_user_continuity(self, log_text: str | None) -> str | None:
        """직전 handoff 작성자(commit author) vs 현재 user 비교 → 연속성 1줄 (T-0208·ADR-0033 ③).

        일치 → `사용자: <email> (직전 handoff 작성자와 동일 — 연속)`.
        불일치 → `⚠ 직전 handoff 는 다른 사용자(<email>) — pending intent 는 프로젝트 상태로 취급`.
        어느 쪽이든 미해소(handoff 부재·author 조회불가·user 미상) → None(줄 생략·fail-soft).
        """
        # 자기 슬롯 마지막 handoff 헤더로 좁힌다 (T-0253·codex R3) — slot-2 부트스트랩이 전역
        # 마지막(=slot-1) handoff 작성자로 "직전 handoff 는 다른 사용자" 오경고를 내지 않게.
        header = last_handoff_header_line(log_text, bound_session=self._bound_session_name())
        if header is None:
            return None
        author = self._handoff_commit_author(header)
        if not author:
            return None
        current = self._current_user()
        if not current:
            return None
        if current == author:
            return f"사용자: {current} (직전 handoff 작성자와 동일 — 연속)"
        return (
            f"⚠ 직전 handoff 는 다른 사용자({author}) — "
            "pending intent 는 프로젝트 상태로 취급"
        )

    # ── 다른 활성 PM 대시보드 light dump (수정형·ADR-0047 ③·T-0260) ──────────

    def _read_leased_sessions_slots(self) -> list[tuple[str, str]] | None:
        """lease 장부에서 leased 엔트리의 `(session, slot)` 목록을 직접 read 한다 (touches 격리).

        session 은 엔트리 `session` 또는 슬롯(`work/<repo>_<N>`→`<repo>_<N>`)에서 유도(대시보드
        키와 동형). worktree_pool 미import(`_repo_slot_numbers` 동형·stdlib json). **장부 부재/
        깨짐/스키마 불일치 → None**("활성 슬롯을 알 수 없음"·MF-1 필터가 no-op 폴백에 씀); 정상
        read 인데 leased 0개면 `[]`. `_lease_others`(폴백 dump)·`_active_leased_sessions`(교집합
        필터) 가 공유한다.
        """
        leases_file = REPO / ".project_manager" / ".local" / "worktree-leases.json"
        if not leases_file.exists():
            return None
        try:
            data = json.loads(leases_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("leases"), list):
            return None
        pairs: list[tuple[str, str]] = []
        for row in data["leases"]:
            if not isinstance(row, dict) or row.get("state", "leased") != "leased":
                continue
            slot = str(row.get("slot") or "")
            session = row.get("session") or (
                slot[len("work/"):] if slot.startswith("work/") else slot
            )
            if session:
                pairs.append((session, slot or "(미상)"))
        return pairs

    def _active_leased_sessions(self) -> set[str] | None:
        """활성(leased) 슬롯 세션 키 집합 (MF-1 대시보드 교집합 필터·codex).

        장부 read 성공 → leased 세션 집합. 장부 부재/불가(None) → **None**(활성 여부 판정
        불가 → 필터 no-op·현행 표시 보존). `--done` release 로 idle 된 슬롯을 이 집합이 배제하므로
        대시보드에 stale 섹션이 남아도 부트스트랩 dump 에서 사라진다 — release 누락/크래시에도
        idle 노출 0(옵션 b·display-robust).
        """
        pairs = self._read_leased_sessions_slots()
        return {s for s, _ in pairs} if pairs is not None else None

    def _lease_others(self) -> list[dict]:
        """자기 제외 leased 슬롯 목록을 lease 장부에서 직접 read 한다 (대시보드 부재 폴백).

        `wiki/log/dashboard.md` 부재 시 폴백 — leased 엔트리에서 자기 슬롯(`_slot_session_name`·
        task-무관)제외분을 `[{"session","slot"}]` 로 반환한다. 장부 부재/깨짐/leased 0개는 빈 목록.
        """
        pairs = self._read_leased_sessions_slots()
        if not pairs:
            return []
        bound = self._slot_session_name()
        return [{"session": s, "slot": slot} for s, slot in pairs if s != bound]

    def _collect_dashboard_others(self) -> dict | None:
        """타 슬롯 대시보드 섹션(자기 제외) light dump 데이터를 수집한다 (ADR-0047 ③·T-0260).

        - `wiki/log/dashboard.md` 존재 → `pm_handoff.parse_dashboard_sections`(동적로드·DRY)로
          파싱해 자기 슬롯(`_slot_session_name`·task-무관) 제외 + **활성(leased) 슬롯과 교집합** 섹션들을
          `{"mode":"dashboard","others":[{"session","body"}]}` 로 반환. 자기 섹션은 표시 안 함
          (자기 컨텍스트는 pm_state/log 몫·ADR-0047). `--done` 으로 release 된 idle 슬롯 섹션은
          활성 집합에 없어 배제된다(MF-1·codex·stale idle 노출 0). 활성 집합 판정불가(장부 부재)면
          필터 no-op(현행 표시 보존). 남는 타 슬롯 0개면 None(절 생략).
        - 대시보드 부재/파싱 실패 → `_lease_others` 폴백(`{"mode":"lease","others":[…]}`).
        - 어느 쪽도 타 PM 0개(솔로) → None(절 생략·graceful).
        """
        dash_file = _dashboard_file()
        if dash_file.exists():
            try:
                text = dash_file.read_text(encoding="utf-8")
            except OSError:
                text = None
            pm_handoff = _load_tool("pm_handoff") if text is not None else None
            parser = getattr(pm_handoff, "parse_dashboard_sections", None)
            if parser is not None:
                try:
                    sections = parser(text)
                except Exception:  # noqa: BLE001 — fail-soft: 파싱 실패는 lease 폴백.
                    sections = None
                if sections is not None:
                    bound = self._slot_session_name()
                    # MF-1: 활성(leased) 슬롯과 교집합 — release/idle 슬롯의 stale 섹션 배제.
                    # 활성 집합 None(장부 판정불가)이면 필터 no-op(현행 표시 보존).
                    active = self._active_leased_sessions()
                    others = [
                        {"session": key, "body": body}
                        for key, body in sections
                        if key != bound and (active is None or key in active)
                    ]
                    # 대시보드가 진실원 — 타 슬롯 없으면 절 생략(lease 폴백 안 함·현재-상태 우선).
                    return {"mode": "dashboard", "others": others} if others else None
        # 대시보드 부재/파싱 불가 → 현행 lease 목록 폴백.
        lease_others = self._lease_others()
        return {"mode": "lease", "others": lease_others} if lease_others else None

    # ── 출력 빌드 ────────────────────────────────────────────────────────

    def _build_markdown(
        self,
        board: dict,
        pytest_result: dict | None,
        git: dict,
        log_entry: dict | None,
        timestamp: str,
        handoff_ctx: dict | None = None,
        dashboard_others: dict | None = None,
    ) -> str:
        counts = board["counts"]
        open_tickets = board["open_tickets"]
        lint = board["lint"]

        # task 첫세션 (T-0391) — 신규 task 로 바인딩됐는데 인계 컨텍스트가 전무(pm_state·자기 task
        # handoff 둘 다 부재 → `_collect_handoff_context` 가 None). 슬롯 축과 직교(⑥)라 fresh 슬롯
        # 배너/1차 강제 대상이 아니어서, 아래 차수/log/pm_state 섹션이 placeholder 로 나 오류처럼
        # 읽혔다 — 신규 task 사유를 명시 분기한다(surface-only·판정 무변경). task resume 은
        # handoff_ctx 가 해소돼(log 태그 entry→차수) 이 분기에 안 걸린다(현행 보존).
        task_first_session = self._task_name is not None and handoff_ctx is None
        # 차수 announce (T-0179) — bound slot pm_state 에서 추론한 `PM <N>차`. 미해소/추론불가는
        # placeholder(`?`) — self-surface 헤더이지 강제 아님(crash 금지). task 첫세션은 슬롯 차수
        # placeholder 대신 `task 1차` 로 명시(T-0391).
        session_label = (
            _TASK_FIRST_SESSION_LABEL if task_first_session
            else _format_session_label(handoff_ctx)
        )
        # fresh 슬롯 (T-0284) — 첫 바인딩(pm_state·자기 handoff 둘 다 부재). log/pm_state 섹션의
        # "미해소/직접 확인" placeholder 를 명시 "fresh·이전맥락없음" 배너로 분기(스크램블 낭비 차단).
        fresh_slot = bool(handoff_ctx and handoff_ctx.get("fresh_slot"))

        lines: list[str] = []
        lines.append(f"## {session_label} 부트스트랩 ({timestamp})")
        if fresh_slot:
            lines.append(_FRESH_SLOT_BANNER)
        # 차수 stale 교차검증 (T-0208) — pm_state 가 log 보다 뒤처졌으면(머신 간 미동기) 경고 1줄.
        stale_warning = _format_stale_warning(handoff_ctx)
        if stale_warning:
            lines.append(stale_warning)
        # user 연속성 (T-0208) — 직전 handoff 작성자와 현재 사용자 동일성 1줄(미해소면 생략).
        user_continuity = git.get("user_continuity")
        if user_continuity:
            lines.append(user_continuity)
        lines.append("")

        # Board 섹션 — 카운트 스코프 라벨 명확화(T-0194·T-0312). 명시 슬롯 바인딩이면 "(slot N)"
        # (그 슬롯 정체성으로 조회·ADR-0056 S1)·솔로/무바인딩이면 "(mine)".
        lines.append("### Board")
        lines.append(_format_board_counts_line(counts, board.get("counts_scope", "mine")))
        if pytest_result is not None:
            lines.append(
                f"- 회귀: {pytest_result['passed']} / {pytest_result['total']} 통과"
            )
        else:
            lines.append("- 회귀: (skip — handoff entry 참조 · --with-pytest 로 재측정)")
        lines.append(f"- lint: {lint}")
        # open 상세 = **내 세션 스트림**만(세션 기본 뷰·ADR-0067). `open_tickets` 는 이미 세션 렌즈
        # (`_collect_board`)로 뽑혀 내 세션 생성 open 만 담는다 — 타 세션분(그 외 open 접힘 카운트·타
        # 세션 claim 현황)은 기본 dump 에서 완전 제거(명시 `board.py list --all` 몫·ADR-0067). 슬롯
        # 레지스트리("다른 활성 PM"·환경 정보)는 dashboard 섹션에서 별도 유지(조정용·티켓 정보 아님).
        if open_tickets:
            lines.append(f"- open ticket (claim 가능): {', '.join(open_tickets)}")
        else:
            lines.append("- open ticket (claim 가능): (없음)")
        lines.append("")

        # Git 섹션
        lines.append("### Git")
        lines.append(f"- branch: {git['branch']}")
        if git.get("no_commits"):
            lines.append("- commit: (초기 커밋 없음 — fresh clone)")
        elif git["commits"]:
            head_sha, head_subject = git["commits"][0]
            lines.append(f"- commit: {head_sha} {head_subject}")
            lines.append("- 마지막 3 commit:")
            for sha, subject in git["commits"][:3]:
                lines.append(f"  - {sha} {subject}")
        lines.append(f"- working tree: {git['working_tree']}")
        # git freshness (T-0217) — ②·① 각 scope fetch 후 behind/ahead + clean·ff 자동
        # ff-pull 결과. ②(is_home)엔 board 서브모듈 rider 를 하위 줄로 붙인다.
        for scope in git.get("freshness", []):
            lines.append(f"- freshness ({scope['label']}): {_format_freshness(scope)}")
            board_sync = scope.get("board_sync")
            if board_sync is not None:
                lines.append(f"  - {board_sync['label']}: {_format_freshness(board_sync)}")
        # 재부착 단서 (T-0217·ADR-0013) — 현 브랜치가 직전 handoff worktree 브랜치와 다르면.
        reattach = git.get("reattach")
        if reattach:
            lines.append(f"- {reattach}")
        # board submodule freshness (T-0195) — `ignore=all` 이 부모 `git status` 에서 board
        # git 상태를 숨기므로 별도 1줄로 surface. board 미분리(솔로)면 생략(graceful skip).
        board_git = git.get("board_git")
        if board_git is not None:
            lines.append(f"- board: {_format_board_git_freshness(board_git)}")
        lines.append("")

        # log/current.md 섹션 — 제목 + **본문 전체** dump (T-0179·인계 컨텍스트 self-surface).
        # 그간 제목(date·type·title)만 표시하고 PM 이 `pm_log.py tail` 로 따로 읽던 것을,
        # 부트스트랩만으로 인계 컨텍스트를 알도록 마지막 handoff entry 본문을 통째로 surface 한다.
        lines.append("### log/current.md 마지막 entry")
        if log_entry:
            lines.append(f"- date: {log_entry['date']}")
            lines.append(f"- type: {log_entry['type']}")
            lines.append(f"- title: {log_entry['title']}")
            body = log_entry.get("body")
            if body:
                lines.append("")
                lines.append("<details><summary>본문 (인계 컨텍스트)</summary>")
                lines.append("")
                lines.append(body)
                lines.append("")
                lines.append("</details>")
            else:
                lines.append("- (본문 파싱 실패 — `pm_log.py tail` 로 직접 확인)")
        elif fresh_slot:
            # fresh 슬롯 (T-0284) — 이 슬롯의 이전 handoff 없음. 전역 마지막 entry 유입은 타 슬롯
            # 컨텍스트 오염(MF-2·ADR-0047)이라 금지 — "복구할 게 없음"을 명시(스크램블 금지).
            lines.append("- (🆕 첫 바인딩 슬롯 — 이 슬롯의 이전 handoff 없음 · 복구할 컨텍스트 없음)")
        elif task_first_session:
            # task 첫세션 (T-0391) — 신규 task 라 자기 task handoff entry 부재. "log 없음/파싱 실패"
            # 는 신규 task 접힘을 오류처럼 보이게 하므로 신규 task 사유를 명시(스크램블 차단).
            lines.append(f"- {_TASK_FIRST_SESSION_LOG_NOTICE}")
        else:
            lines.append("- (log/current.md 없음 또는 entry 파싱 실패)")
        lines.append("")

        # pm_state 인계 surface (T-0179) — bound slot 의 "남은 작업/사용자발의" 절. 부트스트랩만으로
        # 다음 세션이 "무엇부터" 를 바로 보도록 절을 통째로 dump. 미해소/절 부재면 명시 포인터 폴백.
        lines.append("### pm_state — 남은 작업 / 사용자 발의")
        remaining_work = handoff_ctx.get("remaining_work") if handoff_ctx else None
        if remaining_work:
            lines.append("")
            lines.append(remaining_work)
        elif fresh_slot:
            # fresh 슬롯 (T-0284) — pm_state 파일 부재(첫 /pm-handoff 가 생성). "미해소 — 직접 확인"
            # 은 없는 legacy pm_state 를 뒤지게 만드는 스크램블이라 금지·복구할 남은작업 없음을 명시.
            lines.append(
                "- (🆕 첫 바인딩 슬롯 — pm_state 없음 · 첫 /pm-handoff 가 생성 · 복구할 남은작업 없음)"
            )
        elif task_first_session:
            # task 첫세션 (T-0391) — task 서술 pm_state 부재(첫 /pm-handoff 가 생성·T-0356). 없는
            # pm_state 를 "직접 확인"으로 뒤지게 만드는 스크램블 대신 신규 task 사유를 명시.
            lines.append(f"- {_TASK_FIRST_SESSION_STATE_NOTICE}")
        else:
            ptr_path = handoff_ctx.get("state_path") if handoff_ctx else self._pm_state_display_path()
            lines.append(
                f"- (pm_state \"남은 작업 전체 그림\" 절 미해소 — {ptr_path} 직접 확인)"
            )
        lines.append("")

        # 다른 활성 PM (수정형 대시보드 light dump·ADR-0047 ③·T-0260) — 자기 컨텍스트 복원 뒤
        # 타 슬롯 현재-상태(3~5줄)만 가볍게 dump(자기 섹션 제외·상세는 log/current.md 몫). 대시보드
        # 부재면 현행 lease 목록 폴백. 솔로·타 PM 0개면 절 자체 생략(graceful·솔로 무노이즈).
        if dashboard_others and dashboard_others.get("others"):
            lines.append("### 다른 활성 PM")
            if dashboard_others.get("mode") == "dashboard":
                lines.append(
                    "(수정형 대시보드 — 각 슬롯 자기 기록·상세는 log/current.md·ADR-0047)"
                )
                for other in dashboard_others["others"]:
                    lines.append("")
                    lines.append(f"**{other['session']}**")
                    body = other.get("body")
                    if body:
                        lines.append(body)
            else:
                lines.append("(대시보드 부재 — 현행 lease 목록 폴백)")
                for other in dashboard_others["others"]:
                    lines.append(f"- `{other['session']}` · `{other['slot']}`")
            lines.append("")

        # 권장 첫 turn 섹션
        lines.append("### 권장 첫 turn")
        lines.append("PM 세션 시작합니다.")
        # 카운트 스코프 라벨 — 위 `_format_board_counts_line` 과 동일 데이터·스코프(mine/slot N·
        # T-0194·T-0312·ADR-0067)를 요약 문장체로 표기(선두 "- " 없이·마침표로 마감). open 도 이제
        # 세션 스코프(내 세션 생성분·ADR-0067)라 네 status 모두 같은 `_scope` 라벨을 쓴다(옛 open
        # 전용 `_OPEN_SCOPE_LABEL` 축 소멸 — open 이 더는 슬롯무관 공유 backlog 전량이 아님).
        _scope = board.get("counts_scope", "mine")
        board_summary = (
            f"done {counts['done']} ({_scope}) / open {counts['open']} ({_scope}) / "
            f"claimed {counts['claimed']} ({_scope}) / blocked {counts['blocked']} ({_scope})."
        )
        if pytest_result is not None:
            regression_summary = (
                f" 회귀 {pytest_result['passed']} / {pytest_result['total']}, lint {lint}."
            )
        else:
            regression_summary = f" 회귀 (handoff entry 참조), lint {lint}."
        lines.append(f"- board: {board_summary}{regression_summary}")
        # pm_state 는 슬롯별(T-0166) — PM 이 *자기 슬롯* pm_state 를 읽도록 per-slot 경로를
        # 안내한다(pm_handoff 의 read/write 경로와 동형). 솔로/모호면 legacy `pm_state.md` 표기.
        state_path = self._pm_state_display_path()
        lines.append(
            f"- (직전 세션 요약은 PM 손 — {state_path} \"세션 식별\" 절 + log/current.md 마지막 handoff entry 참조)"
        )
        lines.append(
            f"- 무엇부터 갈까요? (PM 손 — {state_path} \"남은 작업 전체 그림\" 절 + open ticket"
        )
        lines.append("  목록 보고 옵션 제시)")

        return "\n".join(lines)

    def _build_json(
        self,
        board: dict,
        pytest_result: dict | None,
        git: dict,
        log_entry: dict | None,
        timestamp: str,
        handoff_ctx: dict | None = None,
        dashboard_others: dict | None = None,
    ) -> dict:
        counts = board["counts"]
        return {
            "timestamp": timestamp,
            # 차수 + 인계 컨텍스트 (T-0179) — session_num(정수/`?`/None)·remaining_work(절 텍스트/
            # None)·state_path. log_last_entry 는 body(본문 전체) 포함. 미해소면 None.
            "session_num": handoff_ctx.get("session_num") if handoff_ctx else None,
            "handoff_context": handoff_ctx,
            "board": {
                # 하위호환(top-level) — 값은 여전히 `--mine` 스코프(변경 없음). T-0194: 컨슈머가
                # 이 카운트를 "전체" 로 오독하지 않도록 `counts_mine`(동일 값의 명시 별칭)을
                # 병기한다 — 스키마 상에서도 mine-scoped 임이 드러나게(라벨 명확화·옵션 a).
                "done": counts["done"],
                "open": counts["open"],
                "claimed": counts["claimed"],
                "blocked": counts["blocked"],
                "counts_mine": dict(counts),
                "open_tickets": board["open_tickets"],
                "lint": board["lint"],
            },
            "pytest": (
                {
                    "passed": pytest_result["passed"],
                    "total": pytest_result["total"],
                }
                if pytest_result is not None
                else None
            ),
            "git": {
                "branch": git["branch"],
                "commits": [
                    {"sha": sha, "subject": subject}
                    for sha, subject in git["commits"]
                ],
                "no_commits": git.get("no_commits", False),
                "working_tree": git["working_tree"],
                # board submodule freshness(T-0195) — None(솔로/board 미분리)이면 생략된 것과
                # 동일 정보량(graceful skip). 있으면 head/dirty/ahead/behind.
                "board_git": git.get("board_git"),
                # git freshness(T-0217) — ②·① 각 scope 의 fetch/behind·ahead/pull 결과 목록
                # (`board_sync` 하위 포함). None/[] 면 freshness 미수집(빌더 직접 호출 등).
                "freshness": git.get("freshness"),
                # 재부착 단서(T-0217·ADR-0013) — 브랜치 불일치 경고 문자열 또는 None.
                "reattach": git.get("reattach"),
                # user 연속성(T-0208·ADR-0033 ③) — 직전 handoff 작성자 vs 현재 user 1줄 또는 None.
                "user_continuity": git.get("user_continuity"),
            },
            "log_last_entry": log_entry,
            # 다른 활성 PM (수정형 대시보드·ADR-0047 ③·T-0260) — 자기 제외 타 슬롯 섹션 light
            # dump({"mode":"dashboard"|"lease","others":[…]}) 또는 None(솔로·타 PM 0개).
            "dashboard_others": dashboard_others,
        }

    # ── 메인 흐름 ────────────────────────────────────────────────────────

    def run(
        self,
        *,
        output_json: bool = False,
        with_pytest: bool = False,
        repo: str | None = None,
        branch: str | None = None,
        resume: str | None = None,
        slot: int | None = None,
        task: str | None = None,
    ) -> int:
        """부트스트랩 정보를 수집해 출력한다.

        with_pytest: True 면 pytest 회귀 실행, False (default) 면 skip.
                     default skip 인 이유는 모듈 docstring 참조.
        repo:        multi-PM 모드(ADR-0013) — 주면 worktree 슬롯 alloc/bind + identity surface
                     를 *추가* 출력한다. 무인자(솔로)면 None — 현행 동작 100% 보존
                     (alloc/bind 경로 미진입).
        slot:        multi-PM lean 모드(T-0074) — `--repo` 와 함께 주면 `alloc` 대신
                     `bind_slot("work/<repo>_<N>", repo, "<repo>_<N>")` 로 **직접 바인딩**
                     하고, 다른 활성 PM 현황(상태점검)도 surface 한다. None 이면 기존
                     `--repo` alloc 경로(현행 보존).

        반환: 0=성공, 1=실패 (sys.exit 로 중단할 수도 있음).
        """
        now = datetime.datetime.now(tz=KST)
        timestamp = now.strftime("%Y-%m-%d %H:%M KST")

        # 세션 정체성(`_bound_slot`)을 **모든 수집보다 먼저** 확정한다 (T-0125·codex R4). 이후 모든
        # cwd-의존(freshness/pytest/git — `_worktree_cwd`)·slot-의존(log_entry/handoff_ctx/dashboard —
        # `_bound_session_name`) 수집이 확정된 정체성을 본다. 확정 안 하면 alloc 모드 새 세션이
        # `_worktree_cwd(None)` 자동해소로 **기존 단일 leased 슬롯 cwd** 에서 fetch/pull·pytest·
        # branch/status 를 보고하고(cwd 유입), 기존 슬롯 log/pm_state/차수를 자기로 표시한다(slot
        # 유입·T-0253 가 없앤 cross-slot 유입의 alloc-경로 재현). 새 슬롯은 fresh 라 자기 슬롯 cwd·
        # "없음/1차"가 정답.
        #   - lean(`--slot`): 슬롯 식별자(`work/<repo>_<N>`)를 직접 세팅.
        #   - alloc(`--repo` without `--slot`): 새 슬롯을 alloc 해 정체성(`lease.slot`)을 확정하고 아래
        #     출력에서 재사용한다(재-alloc 금지·`alloc_calls==1`). NeedsCreate(풀 소진)면 맨 앞에서
        #     조기중단 — 슬롯 없어 세션 시작 불가(정당).
        #   - 솔로/무인자: alloc 미진입 → `_bound_slot` None → `_worktree_cwd` 가 `_auto_slot()` 로
        #     자동해소(현행 보존·순서 무관). board 러너는 슬롯 무관(REPO 고정).
        multipm_lean = repo is not None and slot is not None
        multipm_alloc = repo is not None and slot is None

        # ── 0단계 검증 (⑧·spike §F1b·[[T-0351]]) — dump/alloc 을 뿌리기 *전에* "내가 올바른
        # 슬롯/위치를 쓰고 있나"를 기계로 검증한다. 실패 시 **부분 dump 도 금지**(dump 가 뜨면 PM 이
        # 그것을 세션의 진실로 믿는다·결정 ⑧) — preflight 가 비-0 을 돌려주면 여기서 alloc/수집/dump
        # 이전에 즉시 중단한다. 엔진 앵커(무조건)+슬롯 실재·점유·기록정합(lean 조건)+main-참조 거부(T-0360).
        # solo(repo None)/alloc(slot None)는 슬롯 검사 자연 no-op·앵커만 무조건(결정 2·
        # [[solo-is-subset-of-multipm]]). alloc 앞단에 둬 앵커 거부 시 신규 lease 잔존을 예방한다.
        preflight_rc = self._phase0_preflight(repo, slot, task)
        if preflight_rc != 0:
            return preflight_rc

        # ── F1 task 바인딩 (spike §3b F1·⑥·㉑·[[T-0353]]) — 0단계 통과 *후* 진입. `--task <이름>`
        # 이면 신규/resume 바인딩하고 동시 세션(살아있는 다른 pid)은 거부한다. 거부는 dump 이전에
        # 중단(㉑·부분 dump 금지 = 0단계와 동형 — dump 가 뜨면 PM 이 그것을 세션 진실로 믿는다).
        # task 는 슬롯 축과 직교(⑥)라 아래 alloc/lean(--repo/--slot)·솔로 경로는 그대로 돈다.
        task_info: dict | None = None
        if task is not None:
            task_info = self._bind_task_or_reject(task)
            if task_info is None:
                return 1  # 살아있는 다른 세션 점유(㉑) — 거부(부분 dump 금지).
            # task 정체성을 인스턴스에 확정한다 (F7·T-0374) — 이후 모든 수집(log_entry/handoff_ctx/
            # user_continuity)이 `_bound_session_name`=`task:<이름>`·task 서술 pm_state 를 귀속으로
            # 본다(resume-read 소비 배선). 슬롯 축과 직교(⑥)라 `_bound_slot` 은 아래 alloc/lean 이
            # 그대로 채운다. 수집(freshness 이하) *앞단*에 둬 확정된 정체성을 전 수집이 본다.
            self._task_name = task_info["name"]
            self._task_pm_state_file = Path(task_info["pm_state_path"])
            # ── W3 진입 검증 (I2·ⓐC·[[ADR-0068]]) — task 보유 슬롯 **전수** 0단계 검증. 슬롯별
            # fault(stale·불완전생성·main-참조·기록↔live diverged) 를 모아 하나라도 있으면 **진입
            # 차단**(rc 1·부분 dump 금지) — 전 fault 를 한 번에 표시(순차 발견 금지·해소 전 진행 없음·
            # ⓐC). 0개 보유=검증 no-op(진입). `--repo/--slot` 편입 슬롯의 검증 관계(bind 는 이 gate
            # *뒤*·아래 dump 절에서 일어남):
            #   - **최초 편입**: 아직 task 명의 lease 가 없어 `slots_for_task` 집합에 미포함 →
            #     이 gate 는 안 본다. 대신 위 `_phase0_preflight` 가 lean 경로(`--repo/--slot`)로
            #     그 슬롯을 이미 검증했다(빈틈 없음).
            #   - **멱등 재진입**(이미 task 명의 leased): 집합에 포함돼 여기서 재검증된다 — 같은
            #     프리미티브라 무해하고, stale 등 엣지에선 gate 가 preflight 보다 더 엄격(의도).
            # 차단 후 재진입은 dead-pid reclaim 멱등이라 안전하다. dump/alloc 이전(0단계와 동형 —
            # dump 가 뜨면 PM 이 그것을 세션 진실로 믿는다).
            if self._validate_task_slot_set(task_info["name"]) != 0:
                return 1

        alloc_identity: dict | None = None
        if multipm_lean:
            self._bound_slot = f"work/{repo}_{slot}"
        elif multipm_alloc:
            alloc_identity = self._alloc_and_identity(repo, branch, resume)
            self._bound_slot = alloc_identity["slot"]  # 새 슬롯 정체성 → 모든 수집 기준.

        # alloc 로 갓 잡은 **신규 lease 를 실패에 안전하게** 만든다 (R5·codex). alloc 을 앞단에 둔
        # 건 cwd/정체성 정확성(R4) 때문이라 유지하되, 이후 fail-fast 수집(board/pytest/git — parse
        # 실패 시 `sys.exit(1)`)이 abort 하면 방금 잡은 lease 가 stale leased 로 남아 풀 고갈·"다른
        # 활성 PM" 오표시를 낳는다. 예외/SystemExit 시 그 신규 lease 를 release 후 re-raise 한다.
        # 정상 완료(return 0·lint-blocking return 1 = dump 완료)면 lease 유지(세션이 그 슬롯을 쓴다) —
        # except 는 예외/SystemExit 에만 발동한다. lean/솔로는 이 경로로 신규 lease 를 안 잡으므로
        # cleanup 대상 아님(`alloc_identity is not None` 가드).
        try:
            # git freshness (T-0217) — 부트스트랩 *첫 단계*로 ②·① fetch + clean·ff 자동 ff-pull
            # (+ ② board 서브모듈). stale 로컬로 세션을 시작하는 연속성 사고(차수 오표기·완료된
            # 릴리즈 재추진)를 막는다. fetch/pull 실패는 fail-soft(경고 노트 실어 계속·abort 안
            # 함) — offline 환경도 부트스트랩은 동작한다. git/board 수집보다 *먼저* 돌려 이후
            # dump 가 pull 반영 상태를 보게 한다.
            freshness = self._collect_freshness()

            board = self._collect_board()
            pytest_result = self._collect_pytest() if with_pytest else None
            git = self._collect_git()
            # board submodule freshness(T-0195) — HEAD·dirty·ahead/behind. board 미분리(솔로)면
            # None(graceful skip) — `git` dict 에 실어 빌더로 전달(시그니처 변경 최소화).
            git["board_git"] = self._collect_board_git()
            git["freshness"] = freshness
            log_text = self._read_log_text()
            log_entry = self._collect_log_entry()
            # 재부착 단서 (T-0217·ADR-0013) — 현 worktree 브랜치가 직전 handoff 의 worktree
            # 브랜치와 다르면 경고(자동 checkout 안 함·재부착은 PM 판단). log 마지막 entry 본문의
            # worktree 줄(`- worktree: slot=… · branch=…`)에서 직전 브랜치를 읽는다.
            git["reattach"] = reattach_warning(
                git.get("branch"), log_entry.get("body") if log_entry else None
            )
            # user 연속성 (T-0208·ADR-0033 ③) — 직전 handoff 작성자(commit author) vs 현재 user.
            # 일치=연속·불일치=다른 사용자 경고·미해소=줄 생략(fail-soft). git dict 에 실어 빌더 전달.
            git["user_continuity"] = self._collect_user_continuity(log_text)
            # 차수 + 인계 컨텍스트 (T-0179·T-0208) — bound slot pm_state 에서 차수·"남은 작업/사용자발의"
            # 절. _bound_slot 세팅 후(lean 진입부·alloc 앞단)라 명시 multi-PM 모드면 그 슬롯 pm_state 를,
            # 솔로면 자동해소(legacy 폴백). log_text 를 넘겨 차수 log-폴백 + stale 교차검증(T-0208)을 함께
            # 해소한다. 미해소/부재면 None → 빌더가 placeholder/명시 포인터로 graceful.
            handoff_ctx = self._collect_handoff_context(log_text)
            # 다른 활성 PM (수정형 대시보드 light dump·ADR-0047 ③·T-0260) — 자기 섹션 제외 타 슬롯
            # 현재-상태(3~5줄). 대시보드 부재면 lease 목록 폴백·솔로/타 PM 0개면 None(절 생략).
            # lean 진입부·alloc 앞단으로 `_bound_slot` 이 확정된 뒤라 자기 세션 제외가 정확하다.
            dashboard_others = self._collect_dashboard_others()

            if output_json:
                data = self._build_json(
                    board, pytest_result, git, log_entry, timestamp, handoff_ctx, dashboard_others
                )
                if multipm_lean:
                    # task 동반이면 슬롯을 task 명의로 bind(T-0390·⑥) — F6/`list --task` 즉시 해소.
                    bind_session = task_info["name"] if task_info is not None else None
                    data["worktree"] = self._bind_and_identity(repo, slot, session=bind_session)
                elif multipm_alloc:
                    data["worktree"] = alloc_identity  # 위 앞단 alloc 결과 재사용(재-alloc 금지).
                if data.get("worktree") is not None:
                    # 슬롯 시대차 (T-0341) — freshness fetch 뒤라 origin/<base> 최신 재사용.
                    data["worktree"]["slot_era"] = self._slot_era_info(repo, freshness)
                if task_info is not None:
                    # task+slot(T-0390): 이 부트스트랩서 리스한 작업공간을 task identity 에 접합
                    # (alloc 후속 불요임을 문면화). slot 미동반 task-only 는 미접합(현행·⑥).
                    if multipm_lean and data.get("worktree") is not None:
                        task_info["workspace_slot"] = data["worktree"]["slot"]
                    # 보유 슬롯 전수 열거 행렬(I2·ADR-0068) — bind(위 편입 포함) 뒤라 편입 슬롯도
                    # 합류한다. 진입 검증(_validate_task_slot_set)을 이미 통과한 집합의 surface.
                    task_info["slot_set"] = self._task_slot_set_rows(task_info["name"])
                    data["task"] = task_info  # F1 task identity(신규/resume·prefix 상태·T-0353).
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                markdown = self._build_markdown(
                    board, pytest_result, git, log_entry, timestamp, handoff_ctx, dashboard_others
                )
                print(markdown)
                identity: dict | None = None
                if multipm_lean:
                    # lean 정체성 선언(T-0074) — bind + identity surface + 다른 활성 PM 상태점검.
                    # task 동반이면 슬롯을 task 명의로 bind(T-0390·⑥) — 슬롯 점유 주체=task.
                    bind_session = task_info["name"] if task_info is not None else None
                    identity = self._bind_and_identity(repo, slot, session=bind_session)
                    # 슬롯 시대차 (T-0341) — freshness fetch 뒤라 origin/<base> 최신 재사용.
                    identity["slot_era"] = self._slot_era_info(repo, freshness)
                    if task_info is not None:
                        # task+slot(T-0390): 슬롯 점유 주체가 task 명의라 슬롯-세션 전제의 lean surface
                        # (`multi-PM identity surface`·세션=`<repo>_<N>`)는 생략하고, 작업공간을 아래
                        # task identity 절에 접합한다(⑥·이중 정체성 표기 회피·alloc 후속 불요 문면화).
                        task_info["workspace_slot"] = identity["slot"]
                    else:
                        print()
                        print(self._build_slot_identity_markdown(identity))
                elif multipm_alloc:
                    # 기존 --repo alloc + identity surface (위 앞단 alloc 결과 재사용).
                    identity = alloc_identity
                    identity["slot_era"] = self._slot_era_info(repo, freshness)
                    print()
                    print(self._build_identity_markdown(identity))
                # 커맨드 카드 (ADR-0045) — identity surface 뒤. 이 세션이 쓸 전 커맨드를 정체성
                # (`--repo <repo> --slot <N>`·ADR-0057) 채운 완성형으로 dump("--help 자체를 안
                # 가게"). lean 은 정체성 채운 형태·솔로(identity None/session 부재)는 정체성
                # 인자 없는 형태로 분기. 렌더 실패는 fail-soft(카드 절 생략·위 dump 는 유지).
                card = self._safe_command_card(identity)
                if card:
                    print()
                    print(card)
                # F1 task identity surface (T-0353) — 신규/resume·prefix 상태(기본 없음·①ⓑ)·
                # 서술 pm_state 포인터. 슬롯 identity(있으면) 뒤에 얹는다(task=직교 축·⑥).
                if task_info is not None:
                    # 보유 슬롯 전수 열거 행렬(I2·ADR-0068) — bind(위 편입 포함) 뒤라 편입 슬롯도
                    # 합류한다. 진입 검증을 이미 통과한 집합의 surface.
                    task_info["slot_set"] = self._task_slot_set_rows(task_info["name"])
                    print()
                    print(self._build_task_identity_markdown(task_info))

            # blocking lint — dump-then-warn(T-0195·abort-before-dump 제거). 위에서 이미
            # markdown/JSON 전체(board/git/log/pm_state)를 dump 했으니, 여기서 마지막으로
            # 경고 + 비-0 종료한다 — mid-wave 세션 진입이 dump 0 으로 손 재구성하던 것을 막는다.
            if board.get("lint_blocking"):
                print(
                    f"[경고] board lint 차단(blocking) 이슈 — 위 dump 는 정상 출력됨:\n"
                    f"{board['lint_gate_output']}",
                    file=sys.stderr,
                )
                return 1

            return 0
        except BaseException:  # noqa: BLE001 — SystemExit 포함: abort 시 신규 lease 반납 후 re-raise.
            if alloc_identity is not None:
                self._release_alloc_lease_failsoft(alloc_identity["slot"])
            raise

    # ── 0단계 검증 (⑧·spike §F1b·[[T-0351]]·dump 이전 · 실패 시 부분 dump 금지) ─────────
    # 부트스트랩이 dump 를 뿌리기 전에 "올바른 슬롯/위치인가"를 기계 검증한다(사용자: "제일 먼저
    # 체크할 건 내가 올바른 슬롯을 쓰고 있나"). 엔진 앵커(무조건)+슬롯 실재·점유·기록정합(lean 조건)
    # +main-참조 거부(보호브랜치/origin-추적·T-0360). 판정 근거는 전부 기계(장부·git·compare 프리미티브)이고 로직 중복은 없다 —
    # 엔진 앵커는 board T-0345 가드를, 기록 vs live 는 worktree_pool T-0350 compare(㉒)를 **소비만**한다.

    def _phase0_preflight(self, repo: str | None, slot: int | None, task: str | None = None) -> int:
        """0단계 — dump/alloc 이전 위치·소유·상태 검증 (⑧·spike §F1b). 0=통과·비-0=거부(FAIL-LOUD).

        검사:
          1. **엔진 앵커** (무조건·cwd 무관·F6) — REPO(엔진 파일 위치)가 PM 홈이 아니라 worktree
             사본이면 거부 (T-0345 클래스·`board._pm_home_worktree_misanchor` 소비·부트스트랩은 현행
             이 클래스에 무방비였다).
          2~5 **슬롯 검사** — 슬롯이 명시된 lean 경로(`--repo --slot`·무인자 자동해소 포함)만 돈다.
             solo(repo None)·alloc(slot None)는 슬롯이 없어 자연 no-op(결정 2·[[solo-is-subset-of-multipm]]).
            2. **작업공간 실재** — 장부 lease 도 없고 폴더도 없으면 거부(phantom 슬롯 바인딩 방지).
            3. **타 점유자** — 다른 세션이 그 슬롯을 leased 로 보유하면 거부(결정 ③·readonly ⑬ 예외).
            4. **보호브랜치/origin-추적** — main-참조(보호브랜치 직접 checkout 또는 upstream 설정)면
               **거부**(T-0360·부분 dump 금지·⑧·§F9·readonly 예외).
            5. **기록 vs live** — `compare_slot_git` 소비(㉒·불일치=FAIL-LOUD·미기록=loud+질의 훅·T-0352).

        **task 동반 소유 축(T-0390·⑥)**: `--task <이름> --repo X --slot N` 이면 슬롯 점유 주체가
        task 명의(bind_slot session=task명)라 3(타 점유자) 검사의 '내 것' 판정도 task 명의 축이다 —
        같은 task 명의로 이미 leased 인 슬롯 재진입은 멱등('내 것'·거부 아님)이고, `<repo>_<N>` 세션
        명의나 타 task 명의가 잡은 슬롯은 타 점유로 거부(뺏지 않음·현행 거동 유지). task 미동반이면
        현행 `<repo>_<N>` 축(불변).
        """
        # 1. 엔진 앵커 (무조건) — worktree 사본에서 부트스트랩하면 거부.
        if self._reject_worktree_copy_anchor():
            return 1
        # 1b. 보호목록 sidecar drift-only reconcile (ADR-0072 트리거 ②) — *다른 clone/사용자*의
        #     보호목록 변경을 세션 시작 시점(그 세션의 첫 커밋보다 앞)에 흡수한다. 판정만 하는
        #     0단계의 유일한 예외적 *부작용*이지만, 파생 캐시를 단일 진실에 맞추는 것뿐이고
        #     드리프트가 있을 때만 돈다(정합이면 subprocess 0). 전면 fail-soft — 실패해도 진입을
        #     막지 않는다(보호 훅은 추가 가드이지 부트스트랩의 핵심 부작용이 아니다).
        if repo is not None:
            self._reconcile_protected_sidecar(repo)
        # 2~5 슬롯 검사 — lean(명시 슬롯)만. solo/alloc 은 슬롯이 없어 자연 no-op(결정 2).
        if repo is None or slot is None:
            return 0
        slot_id = f"work/{repo}_{slot}"
        session = f"{repo}_{slot}"
        # 타-점유 판정의 '내 것' 명의 — task 동반이면 task 명의(위 §task 동반 소유 축·T-0390). 브랜치
        # 전환 제안 등 다른 메시지의 `session`(=`<repo>_<N>`·슬롯 표기)은 그대로다.
        self_session = task if task else session
        wp = self._resolve_worktree_pool()  # multi-PM 인데 풀 부재면 명시 에러(SystemExit·dump 이전).
        lease = self._phase0_find_lease(wp, slot_id)
        # 2. 작업공간 실재 (장부·폴더).
        if lease is None and not self._phase0_slot_folder_exists(wp, slot_id):
            print(
                f"[중단·0단계] 슬롯 {slot_id} 이(가) 장부·폴더 어디에도 없습니다 — 미생성 슬롯을 "
                f"바인딩하면 phantom 작업공간에 세션을 dump 합니다.\n"
                f"  → 먼저 슬롯을 생성하세요:  {_CARD_TOOL_INVOKE}/pm_config.py worktree add {repo}\n"
                f"    (또는 `--slot` 번호를 실재하는 슬롯으로 맞추세요.)",
                file=sys.stderr,
            )
            return 1
        # 2b. 불완전 생성(creating·T-0295) — **세션 동일 여부·role 무관** 차단(별도 조건). readonly 든
        #     아니든 반쯤 만든 슬롯은 못 쓴다. bind_slot 이 기존 엔트리를 무조건 leased 로 덮어(T-0295
        #     "점유 메타만 갱신·reclaim 안 거침") in-flight/중단 create 를 훼손하고, reclaim_stale 은
        #     creating 을 무시·alloc 은 creating 재부착 제외 — 아무도 안전히 진입 불가한 불완전 상태다.
        if self._phase0_incomplete_create(lease):
            print(
                f"[중단·0단계] 슬롯 {slot_id} 이(가) 생성 중/중단된 불완전 상태입니다(state=creating) — "
                f"이대로 바인딩하면 진행 중이거나 중단된 슬롯 생성을 훼손합니다.\n"
                f"  → 다른 세션이 생성 중이면 완료를 기다리세요. 중단 흔적이면 상태 확인 후 정리하세요:\n"
                f"     {_CARD_TOOL_INVOKE}/pm_config.py worktree status   "
                f"(incomplete → git worktree remove + `worktree prune-stale`).",
                file=sys.stderr,
            )
            return 1
        # 2c. readonly 공유 슬롯(⑬·T-0358·should-fix)은 **바인딩(점유) 대상이 아니다** — `/pm-bootstrap
        #     --slot N` 오지정 방어. readonly 는 무소유 공유 자산(session/pid 없음·배타 대여 없음)이고,
        #     0단계 carve-out(F6)이 readonly 를 *조회 지칭*엔 허용하지만 bind 는 *점유*라 의미가 다르다.
        #     bind_slot 엔진 불변식(`ReadonlySlotNotLeasable`)과 동형이되 여기서 fail-loud user-facing 으로
        #     닫는다(엔진 raise 가 부트스트랩 flow 로 새어 traceback 나는 것 방지). 이 거부가 있으면 아래
        #     타-점유(3)·보호브랜치(4) 검사는 readonly 를 볼 일이 없다(모두 work 슬롯).
        if self._phase0_is_readonly(lease):
            print(
                f"[중단·0단계] 슬롯 {slot_id} 은(는) readonly 공유 슬롯(⑬)이라 바인딩(점유)할 수 없습니다 "
                f"— 무소유 공유 자산(배타 대여 없음)이라 세션 정체성을 선언하는 대상이 아닙니다.\n"
                f"  → 코드를 읽어 참조하는 용도면 bind 없이 그 worktree 를 읽으세요. 최신 갱신은 "
                f"`/pm-worktree refresh {session}`(fetch→detach). 작업 슬롯이 필요하면 `--slot` 번호를 "
                f"작업 슬롯으로 맞추거나 새 슬롯을 alloc 하세요.",
                file=sys.stderr,
            )
            return 1
        # 3. 타 점유자 (결정 ③) — readonly 는 2c 에서 이미 거부돼 여기 도달 시 항상 work 슬롯.
        # 소유 판정 명의 = task 동반이면 task 명의(T-0390·같은 task 재진입=멱등·타 명의=거부).
        holder = self._phase0_other_holder(lease, self_session)
        if holder is not None:
            print(
                f"[중단·0단계] 슬롯 {slot_id} 을(를) 다른 세션 `{holder}` 이(가) 점유 중입니다 "
                f"(leased) — 남의 작업공간에 바인딩할 수 없습니다(결정 ③).\n"
                f"  → 그 세션의 완료를 기다리거나, 다른 슬롯을 쓰거나, 새 슬롯을 alloc 하세요.",
                file=sys.stderr,
            )
            return 1
        # 4. main-참조(보호브랜치 직접 checkout / origin-추적 upstream) = 진입 거부 (T-0360·⑧·§F9).
        if self._phase0_protected_reject(wp, repo, slot_id, session, lease):
            return 1
        # 5. 기록 vs live 정합 (compare 소비·㉒).
        return self._phase0_record_vs_live(wp, slot_id)

    def _reject_worktree_copy_anchor(self) -> bool:
        """엔진 앵커(REPO=엔진 파일 위치)가 PM 홈이 아니라 worktree 사본이면 거부 (T-0345 클래스·무조건).

        board mutation 의 PM-홈 강제(T-0345)와 **같은 클래스** — `board._pm_home_worktree_misanchor
        (REPO)` 를 소비한다(판정 로직 재구현 금지). 부트스트랩은 현행 이 클래스에 무방비였다: worktree
        (코드 전용) 트리에서 부트스트랩하면 그 트리를 세션 진실로 dump 한다. **cwd 는 판정에 불참**
        (F6)·앵커는 REPO 만. board 직접 import 금지(touches 격리)라 주입/동적로드 board 의 헬퍼를
        `getattr` 로 쓴다(DI 보존·`_protected_warning` 동형) — board/헬퍼 부재·예외는 fail-soft(검사
        생략·솔로/standalone 무영향·오탐 0). True=거부(REPO 가 PM 홈 등록 worktree)."""
        board_mod = self._board or _load_board()
        guard = getattr(board_mod, "_pm_home_worktree_misanchor", None) if board_mod else None
        if guard is None:
            return False
        try:
            pm_home = guard(REPO)
        except Exception:  # noqa: BLE001 — fail-soft: 판정 실패는 통과(오탐 0).
            return False
        if pm_home is None:
            return False
        print(
            f"[중단·0단계] 부트스트랩을 worktree(코드 전용) 트리에서 실행했습니다 — 세션 상태는 "
            f"PM 홈이 소유합니다(ADR-0027). 이대로면 이 worktree 를 세션 진실로 잘못 dump 합니다.\n"
            f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
            f"  (현재 엔진 앵커: {REPO})",
            file=sys.stderr,
        )
        return True

    def _phase0_find_lease(self, wp, slot_id: str):
        """장부에서 `slot_id` lease 를 찾는다 — 없거나 조회 실패면 None (fail-soft)."""
        try:
            for lease in wp.list_leases():
                if getattr(lease, "slot", None) == slot_id:
                    return lease
        except Exception:  # noqa: BLE001 — fail-soft: 장부 조회 실패는 None(실재/점유 판정 보수적).
            return None
        return None

    def _phase0_slot_folder_exists(self, wp, slot_id: str) -> bool:
        """슬롯 worktree 폴더가 실재하는가 (`slot_path(slot).exists()`·조회 실패는 False)."""
        try:
            return wp.slot_path(slot_id).exists()
        except Exception:  # noqa: BLE001 — fail-soft: 경로 해소 실패는 미실재로 취급(보수적).
            return False

    def _phase0_is_readonly(self, lease) -> bool:
        """그 슬롯이 readonly 공유 자산(⑬·`role="readonly"`)인가 — 0단계 **바인딩 거부** 판별 (spike §F11·T-0358).

        readonly 슬롯은 무소유 공유 자산(session/pid 없음·배타 대여 없음)이라 `/pm-bootstrap --slot N`
        바인딩(점유) 대상이 아니다 — 0단계가 이를 fail-loud 로 거부한다(should-fix·bind_slot 엔진 불변식
        `ReadonlySlotNotLeasable` 의 user-facing 짝). 0단계 carve-out(F6·identity_args)이 readonly 를
        *조회 지칭*엔 허용하지만 bind 는 *점유*라 의미가 다르다. 판별 축은 canonical `lease.role`(T-0358 이
        additive 1급 필드로 승격) — 구 장부(role 부재)는 `from_dict` 가 "work" 로 read 하므로 항상
        non-readonly(회귀 0·하위호환). **`lease.extra` 가 아니라 `lease.role` 을 읽는다**: T-0358 이 role 을
        `_LEASE_CANONICAL_KEYS` 로 승격하며 extra 에서 빠졌으므로(canonical=각 필드로 소비), extra 경유
        훅은 조용히 무력화된다 — 그 파급을 canonical 필드 read 로 닫는다. lease None/미상 스키마는 False."""
        if lease is None:
            return False
        return getattr(lease, "role", "work") == "readonly"

    def _phase0_incomplete_create(self, lease) -> bool:
        """그 슬롯이 불완전 생성(`state="creating"`·provisional/중단 마커·T-0295)인가 — 0단계 차단 대상.

        creating 은 create_slot 의 provisional 마커(worktree add *전* 선기록·확정 시 leased·중단 시
        흔적). 이 상태 슬롯은 아무도 안전히 진입 불가하다: bind_slot 은 기존 엔트리를 무조건 leased 로
        덮고(in-flight/중단 create 훼손)·reclaim_stale 는 creating 을 무시(leased 만)·alloc 은 creating
        재부착 제외. 그래서 **세션 동일 여부·readonly 무관** 차단한다(내 중단 흔적이든 타 세션 in-flight
        든 슬롯이 불완전한 건 동일). lease None/미상 state 는 False(fail-soft·`_phase0_other_holder` 와
        분리 — 그 함수는 'leased by other' 의미를 유지한다)."""
        if lease is None:
            return False
        return getattr(lease, "state", None) == "creating"

    def _phase0_other_holder(self, lease, session: str) -> str | None:
        """그 슬롯을 **다른 세션**이 leased 로 점유 중이면 그 세션명, 아니면 None (결정 ③).

        내 세션(`session`)이 이미 잡은 것(crash 후 재개)·idle·미기록은 점유 아님(None). state 가
        `leased` 이고 session 이 나와 다를 때만 타 점유자다. **creating 은 여기서 다루지 않는다** —
        불완전 생성은 `_phase0_incomplete_create` 가 세션·readonly 무관으로 별도 차단한다(2b)."""
        if lease is None:
            return None
        if getattr(lease, "state", None) != "leased":
            return None
        holder = getattr(lease, "session", None)
        if holder and holder != session:
            return holder
        return None

    def _phase0_protected_reject(self, wp, repo: str, slot_id: str, session: str, lease) -> int:
        """슬롯 live 상태가 main-참조(보호브랜치 직접 checkout 또는 origin-추적 upstream)면 **진입 거부** (⑧·§F9·T-0360).

        T-0351 이 깐 warn 골격을 **거부로 승격**한다(BREAKING). 근거(§F9): `--no-track`(ADR-0051 D3·
        T-0274)은 신규 파생 경로에만 upstream 자동설정을 억제하므로 **main 직접 checkout·수동 tracking
        은 무방비** — 커밋이 다 된 뒤 pre-push 훅(T-0076)이 마지막에 잡는 구멍(PM 71 "① 오염 커밋")을
        진입 시점으로 앞당긴다. 실패 시 **부분 dump 도 금지**(0단계 계약 — dump 가 뜨면 PM 이 그것을
        세션 진실로 믿는다). 판정 두 축은 `_phase0_main_reference_reason` 가 소비만 한다(중복 로직 0).

        **readonly 예외(§F11·⑬)**: readonly 공유 슬롯(role="readonly")은 detached(브랜치 없음·upstream
        없음)라 두 축에 자연 미해당하지만, role 로도 명시 carve-out 한다 — main-참조 역할을 이전받는
        슬롯이 곧 readonly 이므로 그 자체가 거부되면 자기충돌(§F1b 이행 순서). (bind flow 에선 2c 가
        readonly 를 이미 거부해 여기 도달 전이지만, 판정 함수의 self-consistency 를 위해 방어한다.)

        반환 0=통과·1=거부(FAIL-LOUD·부분 dump 금지). 거부 메시지엔 해소 2택(readonly 생성 / 작업
        브랜치 전환)을 **실값**으로 싣는다(spike §F9)."""
        # readonly 예외 (§F11·⑬) — 무브랜치 공유 자산은 main-참조 판정 대상이 아니다(역할 이전 대상 자기보호).
        if self._phase0_is_readonly(lease):
            return 0
        reason = self._phase0_main_reference_reason(wp, repo, slot_id)
        if reason is None:
            return 0
        remedy = self._remedy_switch_command(wp, repo, slot_id, session)
        print(
            f"[중단·0단계] 슬롯 {slot_id} 이(가) {reason} — main-참조 상태는 진입 거부입니다(T-0360). "
            f"이 슬롯에서 작업하면 커밋이 canonical/보호 브랜치에 얹혀 ① 오염으로 이어집니다(방어를 "
            f"pre-push 훅에서 진입 시점으로 앞당김·§F9).\n"
            f"  → 해소 (택1):\n"
            f"     (a) 코드 읽기 기준면이 필요하면 readonly 슬롯을 만드세요:\n"
            f"         {_CARD_TOOL_INVOKE}/pm_config.py worktree add {repo} --readonly\n"
            f"     (b) 이 슬롯을 작업 브랜치로 전환하세요(전환+장부 스냅 재기록 원자·T-0414 —\n"
            f"         raw `git switch` 는 스냅을 안 남겨 곧바로 diverged 2차 차단을 부릅니다):\n"
            f"         {remedy}",
            file=sys.stderr,
        )
        return 1

    def _phase0_main_reference_reason(self, wp, repo: str, slot_id: str) -> str | None:
        """슬롯이 main-참조 상태인지 판정 — 사유 문구(거부용) or None(통과) (§F9·두 축).

        두 축(어느 하나라도 해당 시 거부) — spike §F9 의 concern 은 `main`+`origin/main` 슬롯이다:
          1. **보호브랜치 직접 checkout** — 슬롯 HEAD 브랜치가 보호목록(main 등)이면. `_protected_warning`
             (T-0076) 재사용(보호목록=`board._repo_protected`).
          2. **보호브랜치 원격 origin-추적** — `@{upstream}` 이 보호 브랜치 원격(`origin/main` 등)을 가리키면.
             ⚠️ *임의* upstream 이 아니다 — 정상 작업 슬롯은 자기 feature 브랜치(`origin/a5` 등)를 추적하는
             게 정상(T-0273/0274·미해소=경고)이라 그건 통과시킨다. 보호브랜치 원격을 추적할 때만 main-참조.
        보호브랜치 직접 checkout 이 우선(더 구체적 사유). detached(브랜치 None·upstream 없음·readonly
        등)·조회불가·풀 미지원은 None(fail-soft·오탐 0)."""
        try:
            branch = wp.current_branch(slot_id)
        except Exception:  # noqa: BLE001 — fail-soft: 브랜치 조회불가는 판정 생략(오탐 0).
            branch = None
        protected = self._protected_warning(repo, branch)
        if protected is not None:
            return f"보호 브랜치 `{protected}` 를 직접 체크아웃한 상태입니다"
        upstream = self._phase0_protected_upstream(wp, repo, slot_id)
        if upstream is not None:
            return f"보호 브랜치 원격(`{upstream}`)을 origin-추적하는 상태입니다"
        return None

    def _phase0_protected_upstream(self, wp, repo: str, slot_id: str) -> str | None:
        """슬롯 `@{upstream}` 이 **보호 브랜치 원격**(`origin/main` 등)을 추적하면 그 upstream 이름, 아니면 None (§F9 축 2).

        판정 로직 재구현 금지 — worktree_pool 의 `slot_status`(T-0276·`_upstream_status` 흡수)를 호출만
        한다. upstream `<remote>/<branch>` 의 branch 부분(첫 `/` 이후·`feature/x` 도 보존)이 보호목록
        (`_protected_warning`)이면 main-참조로 판정한다. **자기 feature 브랜치 추적(`origin/a5`)은 정상
        작업 슬롯이라 제외**(오탐 0 — §F9 concern 은 `origin/main` 추적). slot_status 미지원(구 풀)·None·
        미해소 upstream·`/` 없는 이름·예외는 None(fail-soft·보수 판정)."""
        getter = getattr(wp, "slot_status", None)
        if getter is None:
            return None
        try:
            status = getter(slot_id)
        except Exception:  # noqa: BLE001 — fail-soft: 상태 조회 실패는 판정 생략(오탐 0).
            return None
        if status is None or not getattr(status, "upstream_ok", False):
            return None
        upstream = getattr(status, "upstream", None)
        if not upstream or "/" not in upstream:
            return None
        tracked_branch = upstream.split("/", 1)[1]  # origin/main → main · origin/feature/x → feature/x
        return upstream if self._protected_warning(repo, tracked_branch) is not None else None

    def _remedy_branch_name(self, wp, repo: str, slot_id: str, preferred: str) -> str | None:
        """main-참조 해소 커맨드가 제시할 **안전한 신규 브랜치명** (T-0412·소비처 2곳 공용).

        해소 커맨드가 실제로 실행 가능한 값만 돌려준다 — 세션/task 명을 그대로 보간하면 그 이름이
        보호브랜치이거나 이미 존재하는 브랜치일 때 자기모순 안내가 된다(PM 4차 실측).
        규칙(순서·`_REMEDY_BRANCH_PREFIX`·`_REMEDY_BRANCH_SUFFIX_LIMIT`) — 후보 필터 3종:
          1. `preferred` 가 보호목록(`board._repo_protected`·`_protected_warning` 축) 밖이고,
             **git 브랜치명으로 유효**(`_remedy_branch_ref_ok` — 실행 쪽 `switch` 와 같은 판정)하며,
             그 슬롯에 **미존재** 브랜치면 그대로.
          2. 아니면 `task/<preferred>`(세 검사 재적용).
          3. 그것도 충돌하면 `task/<preferred>-2`, `-3` … 첫 미충돌.
        전 후보 탈락(상한 도달·전부 무효)이면 **None**(T-0414·T-0412 리뷰 이관분) — 옛 코드는 마지막
        후보(정의상 충돌하는 이름)를 그대로 제시해 안내가 *다시* 실행 불가가 됐다. 이름을 억지로
        짜내는 대신 호출부가 "브랜치명을 직접 지정하라" 분기로 안내한다(`_remedy_switch_command`).

        ⚠ **ref-format 검사가 후보 생성에도 있어야 한다**(codex 게이트 must-fix·T-0414): task 명
        검증(`identity_args.validate_task_name`)은 `fix:bug`·`fix~1`·`a.lock` 을 통과시키지만
        `git check-ref-format --branch` 는 거부한다 — 제안 쪽에 이 검사가 없으면 remedy 가
        `switch <slot> fix:bug` 를 안내하고 실행 쪽이 `invalid-ref` 로 튕겨 "실행 가능한 단일 remedy"
        라는 이 티켓의 목표가 그 입력에서 깨진다(2차 차단 재발). 접두 후보도 같은 검사를 탄다
        (`task/fix:bug` 도 여전히 무효).
        **판정만 한다** — 엔진이 브랜치를 만들지 않는다(0단계=판정·해소=사용자·ADR-0068 I2)."""
        prefixed = f"{_REMEDY_BRANCH_PREFIX}{preferred}"
        candidates = [preferred, prefixed]
        candidates += [f"{prefixed}-{n}" for n in range(2, _REMEDY_BRANCH_SUFFIX_LIMIT + 1)]
        for cand in candidates:
            if self._protected_warning(repo, cand) is not None:
                continue
            if not self._remedy_branch_ref_ok(wp, slot_id, cand):
                continue
            if self._slot_branch_exists(wp, slot_id, cand):
                continue
            return cand
        return None

    def _remedy_branch_ref_ok(self, wp, slot_id: str, branch: str) -> bool:
        """후보가 **`switch` 가 받아들일** 브랜치명인가 — worktree_pool 판정 재사용 (T-0414·규칙 중복 0).

        제안 쪽(여기)과 실행 쪽(`worktree_pool.switch`)의 수용 규칙이 갈리면 안내가 곧바로 실행 불가가
        된다(codex 게이트 must-fix — 같은 검사의 절반 적용). 그래서 **판정을 다시 구현하지 않고**
        worktree_pool 의 `_normalize_branch_name`(그 규칙의 단일 진실 — `git check-ref-format --branch`
        + 빈/다중줄/공백 방어 + 고정점 확인)을 **슬롯 worktree 바인딩 러너**로 호출한다. 주입/동적로드된
        풀 모듈을 쓰는 기존 seam(`_slot_branch_exists`·`_phase0_protected_upstream` 동형·직접 import 금지).

        통과 조건은 `normalize(cand) == cand` — 정규화 결과가 원문과 다르면(revspec 확장·`@{-1}`)
        제안 이름이 다른 브랜치로 해소되어 안내가 거짓말이 되므로 후보에서 배제한다.

        판정기 부재(구 풀·mock)·경로 해소 실패·**git 호출 자체가 터진 경우**는 True(검사 생략·현행
        동작 유지·fail-soft) — 후보 판정 실패가 0단계 안내를 막지 않는다(`_slot_branch_exists` 동형
        규율). `_normalize_branch_name` 은 runner 예외를 자기 안에서 None(=거부)으로 흡수하므로,
        "이름이 무효" 와 "git 이 못 돌았다" 를 가르기 위해 러너가 예외를 표시(`failed`)한다.
        (rc≠0 로 조용히 실패하는 git 은 무효와 구분 불가 — 그 경우 후보가 소진돼 "브랜치명 직접
        지정" 안내로 수렴한다. 여전히 실행 가능한 안내다.)"""
        normalize = getattr(wp, "_normalize_branch_name", None)
        if not callable(normalize):
            return True    # 구 풀 — 검사 비활성(fail-soft·안내는 계속).
        try:
            slot_dir = str(wp.slot_path(slot_id))
        except Exception:  # noqa: BLE001 — fail-soft: 경로 해소 실패는 검사 생략.
            return True
        failed: list[bool] = []

        def runner(argv):
            """슬롯 worktree 바인딩 git 러너 — DI `_run_git_fn` 에 `-C <slot_dir>` 를 붙인다."""
            try:
                return self._run_git_fn(["-C", slot_dir, *argv])
            except Exception:
                failed.append(True)   # git 호출 자체 실패 — 무효 판정과 구분(위 fail-soft).
                raise

        try:
            ok = normalize(branch, git_runner=runner) == branch
        except Exception:  # noqa: BLE001 — fail-soft: 판정기 예외는 검사 생략(안내 계속).
            return True
        return True if failed else ok

    def _remedy_switch_command(self, wp, repo: str, slot_id: str, preferred: str) -> str:
        """main-참조 해소 커맨드 문자열 — **엔진-매개 단일 커맨드** (T-0414·소비처 2곳 공용).

        raw `git switch -c <b>` 는 브랜치는 바꾸지만 장부 스냅을 안 남겨서, 사용자가 안내대로
        해소하면 **곧바로** 0단계 "기록↔live diverged" 2차 차단이 뜬다(PM 4차 실측·왕복 2회 강제).
        엔진이 매개하는 전환은 전부 스냅을 재기록하는데 remedy 만 엔진 밖 raw git 이던 비대칭을
        `worktree_pool.py switch`(전환+스냅 재기록 원자·T-0414)로 닫는다.

        브랜치명은 `_remedy_branch_name`(T-0412)이 산출한다(규칙 중복 구현 0). 후보 소진(None)이면
        이름을 제안하지 않고 **직접 지정**을 안내한다 — 상한 후보는 정의상 충돌하므로 그대로 실으면
        실행 불가 안내를 재생산한다(T-0412 리뷰 이관분).

        복합 문자열(`git … && python3 … record`)은 쓰지 않는다 — **PowerShell 5.x 가 `&&` 미지원**
        (Windows 채택자에서 깨진다·실측)."""
        branch = self._remedy_branch_name(wp, repo, slot_id, preferred)
        cmd = f"{_CARD_TOOL_INVOKE}/worktree_pool.py switch {slot_id}"
        if branch is None:
            return (
                f"{cmd} <새-브랜치명>  (제안 후보 소진 — `{preferred}`·`{_REMEDY_BRANCH_PREFIX}"
                f"{preferred}`·`-2`…`-{_REMEDY_BRANCH_SUFFIX_LIMIT}` 가 모두 보호목록/기존 브랜치와 "
                f"충돌하거나 git 브랜치명으로 부적합합니다. 브랜치명을 직접 지정하세요.)"
            )
        return f"{cmd} {branch}"

    def _slot_branch_exists(self, wp, slot_id: str, branch: str) -> bool:
        """그 슬롯 worktree 에 로컬 브랜치 `branch` 가 이미 있는가 (T-0412·remedy 후보 충돌 검사).

        `git -C <slot_dir> show-ref --verify --quiet refs/heads/<branch>`(argv-list·shell 미경유)를
        DI 러너(`_run_git_fn`)로 돌린다 — rc 0 = 존재. 경로 해소 실패·git 호출 실패/예외는
        **미존재로 간주**한다(fail-soft — 안내는 계속 나가야 하고, 후보 판정 실패로 진입 검증을
        깨뜨리지 않는다)."""
        try:
            slot_dir = str(wp.slot_path(slot_id))
        except Exception:  # noqa: BLE001 — fail-soft: 경로 해소 실패는 미존재(안내 계속).
            return False
        try:
            rc, _out = self._run_git_fn(
                ["-C", slot_dir, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
            )
        except Exception:  # noqa: BLE001 — fail-soft: git 호출 실패는 미존재(안내 계속).
            return False
        return rc == 0

    def _phase0_record_vs_live(self, wp, slot_id: str) -> int:
        """기록된 lease.git(기대) vs 슬롯 live 정합 — `compare_slot_git`(T-0350·㉒) 소비 (결정 ⑪·㉒).

        T-0350 의 compare 프리미티브를 **호출만** 한다(compare/판정 로직 재구현 금지). 판정 정책만 여기서:
          - `fail_loud`(브랜치 변경·head diverged=리셋/비후손·㉒) → **FAIL-LOUD**(거부·1). 두고 간
            상태와 다르다 = 외부 개입 신호(submodule drift 재동기 동형·감지=기계·해소=사용자).
          - `unrecorded`(구 슬롯·git 필드 부재) → **차단 아님·loud 표시**("기준점 미기록 — drift 감지
            비활성") + 질의 훅(사용자 `set-base` 질의는 [[T-0352]]·⑪ 이 같은 0단계 지점에서 채운다).
          - `ok`(기록 있고 fail 아님·match/descendant[㉒ crash 후 재개 notice]) → 통과. submodule pin
            drift 는 비차단 경고(재동기 검토).
        ⚠️ **base 대비 drift 는 여기서 감지 안 함** — `compare_slot_git` 의 `fail_loud` 는 branch+head+
        submodule 만 본다(T-0350 서 base=recorded-only breadcrumb). base 불일치 FAIL·"base 대비 N behind"
        판정은 F10 rebase(wave-2d)·후속 T-0360 으로 이월된다(reviewer 추적성).
        `compare_slot_git` 미구현 풀(구버전·mock)·예외는 fail-soft(정합 검사 생략·통과) — 소프트 진단.
        반환 0=통과·1=FAIL-LOUD 거부."""
        compare = getattr(wp, "compare_slot_git", None)
        if compare is None:
            return 0  # 구버전 풀 — 정합 검사 비활성(fail-soft·소프트 진단).
        try:
            result = compare(slot_id)
        except Exception:  # noqa: BLE001 — fail-soft: compare 실패는 정합 검사 생략(통과).
            return 0
        if getattr(result, "fail_loud", False):
            recorded = getattr(result, "recorded", None) or {}
            live = getattr(result, "live", None) or {}
            print(
                f"[중단·0단계] 슬롯 {slot_id} 의 git 상태가 두고 간 기록과 다릅니다 — 외부 개입 가능성.\n"
                f"  기록(기대): branch={recorded.get('branch')!r} head={recorded.get('head')!r}\n"
                f"  실제(live): branch={live.get('branch')!r} head={live.get('head')!r}\n"
                f"  판정 근거: {_phase0_diverge_reason(result)} "
                f"(head_relation={getattr(result, 'head_relation', None)!r}·"
                f"branch_match={getattr(result, 'branch_match', None)!r})\n"
                f"  정당한 외부 변경(의도한 브랜치 전환·릴리즈 등)이면 사용자 판단 후 아래 커맨드로 도착 스냅을 "
                f"live 로 재동기하세요(감지=기계·해소=사용자·자동 실행 안 함):\n"
                f"    {_CARD_TOOL_INVOKE}/worktree_pool.py record {slot_id}\n"
                f"  (submodule pin drift 만이면 별도 — `worktree_pool.py sync`·기준점 변경은 `set-base`.)",
                file=sys.stderr,
            )
            return 1
        if getattr(result, "unrecorded", False):
            # 미기록(구 슬롯) — 차단 아님·loud 표시 + **후보 제시**(자동 채택 없음·T-0352·결정 ⑪).
            # 후보는 merge-base 로 계산해 *보여주기만* 하고 자동 기록하지 않는다(추론 금지 — 틀려도
            # 조용한 base 위에서 drift 감지가 도는 것을 원천 차단·[[mechanize-dont-instruct-llm]]).
            cand_line = self._unrecorded_base_candidate_line(wp, slot_id)
            print(
                f"[알림·0단계] 슬롯 {slot_id} 의 기준점이 미기록입니다 — drift 감지 비활성(구 슬롯).\n"
                f"  기준점을 지정하면(`set-base {slot_id} <branch>[@<commit>]`) 그때부터 정합 감지가 "
                f"작동합니다.{cand_line}",
                file=sys.stderr,
            )
            return 0  # loud 표시만·차단 아님(결정 ⑪).
        drift = getattr(result, "submodule_drift", None) or []
        if drift:
            print(
                f"[경고·0단계] 슬롯 {slot_id} submodule pin drift: {', '.join(drift)} — 재동기 검토.",
                file=sys.stderr,
            )
        return 0

    def _unrecorded_base_candidates(self, slot_dir: str) -> list[tuple[str, str]]:
        """미기록 슬롯의 base 후보 — `git merge-base HEAD <cand>` 가 해소되는 (branch, sha) 목록 (T-0352·⑪).

        **제시용일 뿐 자동 채택 없음**(추론 금지·결정 ⑪). 흔한 base 브랜치
        (`_UNRECORDED_BASE_CANDIDATE_BRANCHES`)와 HEAD 의 merge-base 를 계산해, 해소되는 것만 후보로
        모은다(예: "후보: `origin/main`(merge-base `df10dc6`)"). merge-base 실패/미해소 후보는 제외,
        전부 fail-soft(빈 목록). 기존 freshness fetch 를 재사용하지 않고 로컬 remote-tracking ref 로만
        계산한다 — *제시*라 stale 여부는 무관(사용자가 `set-base` 로 최종 지정)."""
        cands: list[tuple[str, str]] = []
        for br in _UNRECORDED_BASE_CANDIDATE_BRANCHES:
            try:
                rc, out = self._run_git_fn(["-C", slot_dir, "merge-base", "HEAD", br])
            except Exception:  # noqa: BLE001 — fail-soft: 후보 계산 실패는 그 후보만 건너뛴다.
                continue
            sha = (out or "").strip()
            if rc == 0 and sha:
                cands.append((br, sha))
        return cands

    def _unrecorded_base_candidate_line(self, wp, slot_id: str) -> str:
        """미기록 0단계 후보 제시 줄(`\\n  후보: …`) 또는 빈 문자열 (T-0352·자동 채택 없음).

        슬롯 경로(`wp.slot_path`)를 얻어 `_unrecorded_base_candidates` 로 후보를 계산하고, 있으면
        spike §F9 형식("후보: `origin/main`(merge-base `<sha>`)")으로 포맷한다. 후보 없음·경로 해소
        실패는 빈 문자열(줄 생략·오탐 0). fail-soft(loud 알림 자체를 막지 않는다)."""
        try:
            slot_dir = str(wp.slot_path(slot_id))
        except Exception:  # noqa: BLE001 — fail-soft: 경로 해소 실패는 후보 줄 생략.
            return ""
        cands = self._unrecorded_base_candidates(slot_dir)
        if not cands:
            return ""
        shown = " · ".join(f"`{br}`(merge-base `{sha[:12]}`)" for br, sha in cands)
        return f"\n  후보: {shown} — 자동 채택 안 함(사용자가 `set-base` 로 결정·결정 ⑪)."

    # ── multi-PM 모드: worktree 슬롯 alloc + identity surface (ADR-0013·0011) ────

    def _resolve_worktree_pool(self):
        """worktree_pool 모듈을 해소한다 — 주입분 우선·없으면 동적 로드 (multi-PM 모드 전용).

        --repo 경로에서만 호출된다. 주입(테스트 mock)이 있으면 그걸, 없으면 동적
        로드한다. 둘 다 None 이면 **명시 에러**(SystemExit) — multi-PM 인자를 줬는데
        worktree_pool 이 없으면 침묵 무력화 금지(ADR-0013).
        """
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            print(
                "[중단] --repo multi-PM 모드인데 worktree_pool 엔진을 찾을 수 없다 "
                f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패). "
                "multi-PM 셋업(pm-config) 또는 엔진 전파를 확인하라.",
                file=sys.stderr,
            )
            sys.exit(1)
        return wp

    def _release_alloc_lease_failsoft(self, slot: str) -> None:
        """alloc 로 갓 잡은 신규 lease 를 best-effort release 한다 (R5·실패 cleanup·원 예외 안 가림).

        부트스트랩이 alloc(앞단·R4) 후 fail-fast 수집(board/pytest/git)에서 abort 하면 이 신규
        lease 가 stale leased 로 남아 풀 고갈·"다른 활성 PM" 오표시를 낳는다. `worktree_pool.release`
        로 반납한다 — `require_clean=False`(갓 alloc 한 슬롯은 clean 이나 방어적). 여기서는
        `_resolve_worktree_pool`(부재 시 SystemExit) 대신 fail-soft 로드(부재→skip)를 쓰고, release
        자체 실패도 삼킨다 — cleanup 이 원 abort 예외를 **가리지 않게**(re-raise 는 호출부 몫).
        """
        try:
            wp = self._worktree_pool or _load_worktree_pool()
            if wp is not None:
                wp.release(slot, require_clean=False)
        except Exception:  # noqa: BLE001 — best-effort: cleanup 실패가 원 abort 를 가리지 않는다.
            pass

    def _bind_task_or_reject(self, task: str) -> dict | None:
        """`--task` 신규/resume 바인딩 (F1·⑥·㉑·T-0353). 동시 세션(살아있는 다른 pid)이면 None(거부).

        `worktree_pool.bind_task` 로 신규/resume/reclaim 을 처리한다 — 살아있는 다른 세션 점유는
        `TaskActiveElsewhere` → 여기서 stderr 안내 + None(caller 가 dump 이전 중단·㉑). 성공 시
        surface 용 dict(name·prefix·action·서술 pm_state 경로/존재)를 반환한다. task 는 슬롯 축과
        직교(⑥)라 이 바인딩은 `.local/slots/` 를 건드리지 않는다(마이그레이션 0·DoD).
        """
        wp = self._resolve_worktree_pool()
        try:
            # 등록 repo 집합을 넘겨 예약명(<repo>_<N>)을 엔진 진입점에서도 방어(primitive 자기완결·
            # should-fix) — CLI 의 빠른 거부(main)와 이중화. 명 검증(traversal/절대경로/빈 이름·
            # must-fix)은 registered_repos 무관하게 엔진에서 항상 돈다.
            record, action, reclaimed_from = wp.bind_task(
                task, registered_repos=_registered_repos(self._areas_file)
            )
        except wp.TaskActiveElsewhere as exc:
            print(
                f"[중단·F1] task {task!r} 이(가) 다른 살아있는 세션(pid {exc.pid})에서 열려 "
                f"있습니다 — 같은 task 를 두 창에서 동시에 열 수 없습니다(㉑).\n"
                f"  → 그 창을 쓰거나, 그 세션이 끝난 뒤 다시 여세요. (그 세션이 비정상 종료됐다면 "
                f"pid 가 죽어 자동 회수되니 잠시 후 재시도.)",
                file=sys.stderr,
            )
            return None
        except wp.InvalidTaskName as exc:
            print(
                f"[중단·F1] task 명 {task!r} 이(가) 부적합합니다 — {exc.reason}.\n"
                f"  → task 명은 경로 문자(`/`·`\\`·`..`·선행 `.`) 없는 단일 이름이어야 하고, "
                f"슬롯 세션 예약 패턴(<등록 repo>_<N>)은 쓸 수 없습니다(⑥).",
                file=sys.stderr,
            )
            return None
        pm_state = wp.task_dir(task) / "pm_state.md"
        return {
            "name": record.name,
            "prefix": record.prefix,          # None = prefix 없음(기본·①ⓑ)
            "action": action,                 # created | resumed | reclaimed
            "started": getattr(record, "started", None),
            # 회수한 이전 pid(>0 이면 loud notice — 다른 창이 아직 작업 중일 수 있음·㉑ 정직화).
            "reclaimed_from_pid": reclaimed_from,
            "pm_state_path": str(pm_state),
            "pm_state_exists": pm_state.exists(),
        }

    def _build_task_identity_markdown(self, task_info: dict) -> str:
        """F1 task identity surface — 정체성·prefix 상태(기본 없음·①ⓑ)·서술 pm_state 포인터 (T-0353).

        prefix 는 이 task 세션의 board prefix 상태를 surface 한다(기본 None → "(없음)") → PM 이
        사용자와 확인. 변경 명령은 `task prefix`(T-0357). 작업공간(슬롯) 연결은 F2 alloc(T-0354)이
        채우므로 여기선 task 정체성·prefix·서술 상태만 보인다(신규는 슬롯 0개로 시작 가능·⑥).
        """
        name = task_info["name"]
        prefix = task_info.get("prefix")
        prefix_display = f"`{prefix}`" if prefix else "(없음)"
        action = task_info.get("action")
        action_label = {
            "created": "신규 task",
            "resumed": "재개(resume)",
            "reclaimed": "재개(회수·이전 세션 crash)",
        }.get(action, action or "")
        pm_state_path = task_info.get("pm_state_path")
        pm_state_exists = task_info.get("pm_state_exists")

        lines: list[str] = []
        lines.append("### task identity surface (T-0353)")
        lines.append(
            f"- 당신은 **task `{name}`** PM · prefix={prefix_display} · 상태={action_label}."
        )
        # ㉑ 정직화 loud notice — dead-pid 회수(reclaimed)는 crash 재개가 다수지만, 기록 pid=부트스트랩
        # 프로세스라 "다른 창이 아직 살아 작업 중"인 경우도 못 가른다(pid 는 이미 죽음). 감지=기계·
        # 해소=사용자로 명시 경고한다(차단 아님·조상추적 비채택 근거는 bind_task docstring).
        reclaimed_from = task_info.get("reclaimed_from_pid")
        if action == "reclaimed" and reclaimed_from:
            started = task_info.get("started") or "(미상)"
            lines.append(
                f"- ⚠️ **회수 진입** — 이 task 는 다른 프로세스(pid {reclaimed_from})가 열어 두고 "
                f"종료(핸드오프) 기록 없이 회수됐습니다 (task 시작: {started}). **다른 창에서 아직 "
                f"작업 중일 수 있습니다** — 그 창이 살아 있으면 한쪽을 닫으세요(중복 작업 방지)."
            )
        lines.append(
            f"- **prefix 상태 = {prefix_display}** (기본=없음·①ⓑ) — 사용자와 확인. "
            "변경은 `task prefix`(T-0357)."
        )
        # 작업공간(슬롯) — I2 보유 집합 **전수 열거**(ADR-0068). 진입 검증(_validate_task_slot_set)을
        # 통과한 집합의 surface(슬롯·repo·branch·head·기록↔live·dirty 행렬). `--repo/--slot` 편입
        # 슬롯도 bind 뒤 조회라 같은 행렬에 합류한다(T-0390). generic "F2 alloc 에서 연결" 안내는
        # 0개 보유일 때만(spike §3a). slots_for_task 미제공 구 풀은 편입 슬롯 단일 표기로 graceful.
        workspace_slot = task_info.get("workspace_slot")
        slot_set = task_info.get("slot_set") or []
        if slot_set:
            self._append_task_slot_matrix(lines, name, slot_set, workspace_slot)
        elif workspace_slot:
            # graceful fallback (slots_for_task 미제공 풀) — 편입 슬롯 단일 표기.
            lines.append(
                f"- 작업공간: `{workspace_slot}` (이 부트스트랩서 task 명의 리스·F2 alloc 후속 불요·T-0390)."
            )
        else:
            # 0개 보유 — 진입(검증 no-op) + generic 안내는 여기서만(spike §3a).
            lines.append("- 작업공간: (없음)")
            lines.append(
                "  작업공간(슬롯): F2 alloc(T-0354)에서 연결 — 신규 task 는 슬롯 0개로 시작 가능(⑥)."
            )
        if pm_state_path:
            suffix = "" if pm_state_exists else " (아직 없음 — 첫 핸드오프가 생성·T-0356)"
            lines.append(f"- pm_state (이 task·서술): `{pm_state_path}`{suffix}")
        return "\n".join(lines)

    # ── W3: task 보유 슬롯 집합 — 진입 전수 검증(I2·ⓐC) + 열거 행렬 (ADR-0068·spike §3a) ──

    # 기록↔live 상태 → 행렬 표기(재열거 surface·전수 검증 통과분). diverged 는 진입 검증이
    # 차단하므로 정상 열거엔 나타나지 않으나 방어적으로 매핑한다(fail-soft 열거는 크래시 0).
    _RECORD_LIVE_MARK = {"ok": "✓", "unrecorded": "미기록", "unknown": "미상", "diverged": "⚠diverged"}

    def _validate_task_slot_set(self, task: str) -> int:
        """task 보유 슬롯 **전수** 0단계 검증 — fault 1+ = 진입 차단 (I2·ⓐC·ADR-0068 W3).

        `slots_for_task(task)` 보유 집합 각각에 기존 0단계 프리미티브(folder 실재·불완전 생성·
        보호브랜치·기록↔live compare — `_phase0_*`·`compare_slot_git` **재사용**·판정 재구현 0)를
        돌려 fault 를 모은다. 하나라도 있으면 **전 fault 를 한 번에** stderr 로 표시하고(각 슬롯·
        판정 근거·해소 커맨드 실값·순차 발견 금지) rc 1 로 진입을 차단한다 — 부분 dump 금지(0단계
        동형·"해소 전 진행 없음"·ⓐC). 감지=기계·해소=사용자(부트스트랩 밖 record/prune-stale 등
        실행 후 재진입). 0개 보유 = no-op(검증 대상 없음·진입). slots_for_task 미제공(구 풀)·조회
        실패는 fail-soft 통과(대상 해소 불가·소프트 진단). 반환 0=통과·1=차단(FAIL-LOUD)."""
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            return 0
        slots_fn = getattr(wp, "slots_for_task", None)
        if not callable(slots_fn):
            return 0  # 구 풀 — 전수 검증 비활성(fail-soft·소프트 진단).
        try:
            leases = list(slots_fn(task) or [])
        except Exception:  # noqa: BLE001 — fail-soft: 집합 조회 실패는 검증 no-op(진입).
            return 0
        faults: list[tuple[str, tuple[str, str, str]]] = []
        for lease in sorted(leases, key=lambda x: getattr(x, "slot", "")):
            fault = self._task_slot_fault(wp, lease, task)
            if fault is not None:
                faults.append((getattr(lease, "slot", "(미상)"), fault))
        if not faults:
            return 0
        lines = [
            f"[중단·진입검증] task {task!r} 보유 슬롯 {len(faults)}개가 0단계 검증에 실패했습니다 — "
            f"진입 차단(부분 dump 금지·ADR-0068 I2·ⓐC). **아래 전부를** 해소한 뒤 다시 부트스트랩하세요 "
            f"(해소 전 진행 없음·순차 발견 금지):",
        ]
        for slot_id, (label, reason, resolve) in faults:
            lines.append(f"  - {slot_id}: {label}")
            lines.append(f"      근거: {reason}")
            lines.append(f"      해소: {resolve}")
        print("\n".join(lines), file=sys.stderr)
        return 1

    def _task_slot_fault(self, wp, lease, task: str) -> "tuple[str, str, str] | None":
        """task 보유 슬롯 1개의 0단계 fault → (label, 근거, 해소커맨드) 또는 None(통과) (W3·재사용).

        기존 0단계 프리미티브를 per-slot 재사용한다(판정 재구현 0) — 각 검사는 **print 없이** 사유+
        해소만 돌려줘 caller 가 전 fault 를 일괄 표시하게 한다(순차 발견 금지). 검사 순서(우선):
          1. **stale** (장부 有·worktree 폴더 無) — T-0393 이 회귀 경로서 fail-loud 로 정한 클래스.
             `slots_for_task` 로 이미 장부엔 있으므로 폴더 부재만 보면 stale(해소=prune-stale).
          2. **불완전 생성** (state=creating·T-0295).
          3. **main-참조** (보호브랜치 직접 checkout / origin-추적·§F9·`_phase0_main_reference_reason`).
          4. **기록↔live diverged** (`compare_slot_git` fail_loud·㉒·`_phase0_diverge_reason` 근거).
        task 자기 명의 슬롯이라 타-점유(결정 ③)는 자연 무해(session==task)라 대상 아님(생략)."""
        slot_id = getattr(lease, "slot", None)
        if not slot_id:
            return None
        repo = getattr(lease, "repo", None) or slot_id.rpartition("/")[2].rsplit("_", 1)[0]
        # 1. stale (장부 有·폴더 無).
        if not self._phase0_slot_folder_exists(wp, slot_id):
            return (
                "stale (장부에 lease 존재·worktree 폴더 부재)",
                "장부엔 이 슬롯의 task 명의 lease 가 있으나 worktree 디렉터리가 없습니다 — "
                "외부 삭제/미생성(T-0393 클래스·엉뚱한 트리 vacuous 진입 방지).",
                f"{_CARD_TOOL_INVOKE}/pm_config.py worktree prune-stale",
            )
        # 2. 불완전 생성.
        if self._phase0_incomplete_create(lease):
            return (
                "불완전 생성 (state=creating)",
                "생성 중/중단된 슬롯이라 안전히 진입할 수 없습니다(T-0295).",
                f"{_CARD_TOOL_INVOKE}/pm_config.py worktree status",
            )
        # 3. main-참조(보호브랜치 직접/원격추적).
        reason = self._phase0_main_reference_reason(wp, repo, slot_id)
        if reason is not None:
            return (
                "main-참조 (보호브랜치 직접 checkout / origin-추적)",
                f"{reason} — 이 슬롯 커밋이 canonical/보호 브랜치로 새면 ① 오염(§F9).",
                self._remedy_switch_command(wp, repo, slot_id, task),
            )
        # 4. 기록↔live diverged.
        compare = getattr(wp, "compare_slot_git", None)
        if callable(compare):
            try:
                result = compare(slot_id)
            except Exception:  # noqa: BLE001 — fail-soft: compare 실패는 fault 아님(통과).
                result = None
            if result is not None and getattr(result, "fail_loud", False):
                return (
                    "기록↔live diverged (외부 개입 가능성)",
                    _phase0_diverge_reason(result),
                    f"{_CARD_TOOL_INVOKE}/worktree_pool.py record {slot_id}",
                )
        return None

    def _task_slot_set_rows(self, task: str) -> list[dict]:
        """task 보유 슬롯 전수 열거 행렬 데이터 (I2 surface·fail-soft·부작용 0).

        `slots_for_task(task)` → 슬롯별 {slot·repo·branch·head·record_live·dirty} dict 리스트를
        돌려준다 — 진입 검증(_validate_task_slot_set) **통과 후** surface 렌더(markdown 행렬·JSON)가
        소비한다. slots_for_task 미제공(구 풀)/조회 실패는 빈 리스트(→ "(없음)" 분기·graceful).
        매 필드 조회는 fail-soft(재열거는 크래시 0)."""
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            return []
        slots_fn = getattr(wp, "slots_for_task", None)
        if not callable(slots_fn):
            return []
        try:
            leases = list(slots_fn(task) or [])
        except Exception:  # noqa: BLE001 — fail-soft: 조회 실패는 빈 열거(surface 줄 축약).
            return []
        rows: list[dict] = []
        for lease in sorted(leases, key=lambda x: getattr(x, "slot", "")):
            slot_id = getattr(lease, "slot", None)
            if not slot_id:
                continue
            rows.append(self._task_slot_row(wp, lease, slot_id))
        return rows

    def _task_slot_row(self, wp, lease, slot_id: str) -> dict:
        """슬롯 1개의 열거 행 — branch/head/dirty(`slot_git_status`) + 기록↔live(`compare_slot_git`)."""
        repo = getattr(lease, "repo", None) or slot_id.rpartition("/")[2].rsplit("_", 1)[0]
        status = self._task_slot_git_status(wp, slot_id)
        head = status.get("head")
        head_disp = head[:12] if isinstance(head, str) and head else "(미상)"
        return {
            "slot": slot_id,
            "repo": repo,
            "branch": status.get("branch") or "(detached)",
            "head": head_disp,
            "dirty": bool(status.get("dirty")),
            "record_live": self._task_slot_record_live(wp, slot_id),
        }

    def _task_slot_git_status(self, wp, slot_id: str) -> dict:
        """`slot_git_status(slot)`(branch·head·dirty·T-0359) fail-soft 조회 — 미제공/실패는 {}."""
        fn = getattr(wp, "slot_git_status", None)
        if not callable(fn):
            return {}
        try:
            return fn(slot_id) or {}
        except Exception:  # noqa: BLE001 — fail-soft: 상태 조회 실패는 빈 dict(행 표시 흡수).
            return {}

    def _task_slot_record_live(self, wp, slot_id: str) -> str:
        """기록↔live 상태 문자열 — `compare_slot_git` 소비 (ok|unrecorded|diverged|unknown·fail-soft)."""
        fn = getattr(wp, "compare_slot_git", None)
        if not callable(fn):
            return "unknown"
        try:
            result = fn(slot_id)
        except Exception:  # noqa: BLE001 — fail-soft: compare 실패는 unknown(표시만).
            return "unknown"
        if result is None:
            return "unknown"
        if getattr(result, "unrecorded", False):
            return "unrecorded"
        if getattr(result, "fail_loud", False):
            return "diverged"
        return "ok"

    def _append_task_slot_matrix(
        self, lines: list, task: str, slot_set: list, workspace_slot: "str | None"
    ) -> None:
        """보유 슬롯 전수 열거 행렬을 `lines` 에 추가 (I2·spike §3a·T-0398 렌더 문법과 동형).

        헤더 `작업공간 (task 'X' 보유 N — 전수 검증):` + 슬롯별 `slot · repo · branch · head ·
        기록↔live <mark> · <dirty>` 행(`_render_task_slots`[pm_config·T-0398]과 같은 ` · ` bullet
        문법). `--repo/--slot` 편입 슬롯(workspace_slot)은 행 끝에 표기를 단다(T-0390 합류 문면)."""
        lines.append(f"- 작업공간 (task {task!r} 보유 {len(slot_set)} — 전수 검증):")
        for row in slot_set:
            tag = " ·이 부트스트랩 편입(T-0390)" if workspace_slot and row["slot"] == workspace_slot else ""
            dirty_disp = "dirty" if row["dirty"] else "clean"
            mark = self._RECORD_LIVE_MARK.get(row["record_live"], row["record_live"])
            lines.append(
                f"    - {row['slot']} · repo={row['repo']} · branch={row['branch']} · "
                f"head={row['head']} · 기록↔live {mark} · {dirty_disp}{tag}"
            )

    def _alloc_and_identity(
        self, repo: str, branch: str | None, resume: str | None
    ) -> dict:
        """worktree 슬롯을 alloc 하고 identity surface 데이터를 반환한다 (multi-PM 모드).

        - `worktree_pool.alloc(repo, branch=, resume=)` 호출 → Lease.
        - `NeedsCreate` (풀 소진) → 사용자 게이트 안내 후 sys.exit(1). **자동
          `git worktree add` 안 함**(ADR-0013 — fs 행위는 사용자 게이트).
        - 성공 시 cwd=슬롯 경로·branch·등록영역을 dict 로 반환(markdown/JSON 빌더가 소비).
          branch 는 `worktree_pool.current_branch(slot)` live 조회(ADR-0013 amend T-0072 —
          git=진실·장부 저장 폐지). detached/조회불가는 None → surface 가 "(미지정)".
        """
        wp = self._resolve_worktree_pool()
        try:
            lease = wp.alloc(repo, branch=branch, resume=resume)
        except wp.NeedsCreate as exc:
            print(
                f"\n[사용자 게이트] repo {exc.repo!r} worktree 풀 소진 — idle 슬롯이 없다.\n"
                f"  자동 `git worktree add` 는 하지 않는다(ADR-0013 — fs 행위·사용자 게이트).\n"
                f"  새 슬롯이 필요하면 수동으로 추가하라:\n"
                f"    pm-config worktree add {exc.repo}"
                f"{f' --branch {branch}' if branch else ''}\n"
                f"  (또는 진행 중인 다른 슬롯을 작업완료 후 release.)",
                file=sys.stderr,
            )
            sys.exit(1)

        slot_path = wp.slot_path(lease.slot)
        # 세션 정체성 = 슬롯키(`work/<repo>_<N>` → `<repo>_<N>`) — alloc 도 명시 multi-PM 이므로
        # identity 에 `session` 을 채운다(T-0250 codex·ADR-0045·ADR-0057). 이게 없으면 커맨드
        # 카드가 `session` 키 부재를 솔로로 오판해 `--repo/--slot` 빠진 카드를 dump → claim(required)
        # fail-loud.
        session = lease.slot[len("work/"):] if lease.slot.startswith("work/") else lease.slot
        # 브랜치는 슬롯 worktree 의 git HEAD 에서 live 조회(ADR-0013 amend T-0072 —
        # git=진실·장부 저장 폐지). detached/조회불가는 None → identity surface 가 "(미지정)".
        return {
            "repo": repo,
            "slot": lease.slot,
            "session": session,
            "slot_path": str(slot_path),
            "branch": wp.current_branch(lease.slot),
            "registered_repos": _registered_repos(self._areas_file),
            # 슬롯 상태 surface (T-0276·ADR-0051 T-β) — upstream + submodule 역할(pin/dev-ahead/
            # drift). 백본 미제공/실패는 None(절 생략·fail-soft). JSON-safe dict 로 실어 빌더/JSON 공용.
            "slot_status": self._safe_slot_status(wp, lease.slot),
        }

    def _bind_and_identity(self, repo: str, slot: int, *, session: str | None = None) -> dict:
        """슬롯을 직접 bind 하고 lean identity + 상태점검 데이터를 반환한다 (multi-PM lean·T-0074).

        - 세션 = `session`(주면 그 명의·task 동반 시 task 명·T-0390) 또는 `f"{repo}_{slot}"`(기본·
          슬롯-only 불변)·슬롯 식별자 = `f"work/{repo}_{slot}"`.
        - `worktree_pool.bind_slot(slot_id, repo, session)` 호출 → Lease. **pool alloc 아님**
          (직접 바인딩·`NeedsCreate` 게이트 없음·`reclaim_stale` 안 거침·ADR-0013).
        - branch 는 `worktree_pool.current_branch(slot_id)` live 조회(git=진실·ADR-0013 amend
          T-0072). detached/조회불가/슬롯 폴더 부재는 None → surface 가 "(미지정)".
        - **상태점검**: `list_leases()` 에서 *이 세션(=bind 명의) 제외* 다른 활성(leased) 리스를 모아
          각 줄 `세션 · 슬롯 · 브랜치(live)` 로 반환한다(다른 활성 PM 현황 surface).

        **session override(T-0390·⑥)**: `--task <이름>` 동반이면 caller 가 task 명을 넘겨 슬롯을
        task 명의로 리스한다(F3 소유검사·F6 해소·`list --task` 가 이 슬롯을 즉시 본다). 미지정
        (기본)이면 현행 슬롯 세션 `<repo>_<N>`(슬롯-only 경로 100% 불변).
        """
        wp = self._resolve_worktree_pool()
        if session is None:
            session = f"{repo}_{slot}"
        slot_id = f"work/{repo}_{slot}"
        lease = wp.bind_slot(slot_id, repo, session)

        slot_path = wp.slot_path(lease.slot)
        # 다른 활성 PM 현황 — 이 세션 제외 leased 리스(상태점검 surface).
        others: list[dict] = []
        for other in wp.list_leases():
            if other.state != "leased" or other.session == session:
                continue
            others.append({
                "session": other.session,
                "slot": other.slot,
                "branch": wp.current_branch(other.slot),
            })

        live_branch = wp.current_branch(lease.slot)
        return {
            "repo": repo,
            "session": session,
            "slot": lease.slot,
            "slot_path": str(slot_path),
            "branch": live_branch,
            "others": others,
            # 보호 브랜치 경고 (T-0076·소프트) — 라이브 브랜치가 그 repo 보호목록에 있으면
            # 🚫 경고를 surface 한다. 미보호/조회불가/board 부재면 None(경고 생략).
            "protected_branch": self._protected_warning(repo, live_branch),
            # 슬롯 상태 surface (T-0276·ADR-0051 T-β) — upstream + submodule 역할(pin/dev-ahead/
            # drift). 백본 미제공/실패는 None(절 생략·fail-soft). JSON-safe dict 로 실어 빌더/JSON 공용.
            "slot_status": self._safe_slot_status(wp, lease.slot),
        }

    def _safe_slot_status(self, wp, slot: str) -> dict | None:
        """worktree_pool.slot_status(slot) 를 fail-soft 로 JSON-safe dict 화한다 (T-0276).

        슬롯 상태 surface 는 *소프트*(추가 인지)라 백본 부재(구버전 풀·mock 미구현)·조회 실패는
        None(절 생략·부트스트랩/identity 자체는 안 깨짐). `slot_status` 미구현 풀은 `getattr`
        None 으로 걸러 AttributeError 없이 graceful — 기존 mock 풀(slot_status 없음)도 통과.
        """
        fn = getattr(wp, "slot_status", None)
        if fn is None:
            return None
        try:
            return slot_status_to_dict(fn(slot))
        except Exception:  # noqa: BLE001 — fail-soft: 슬롯 상태는 소프트(실패=절 생략).
            return None

    def _slot_status_block(self, identity: dict | None) -> list[str]:
        """identity 의 슬롯 상태를 `### 슬롯 상태` 서브섹션 줄로 렌더한다 (T-0276·없으면 빈 리스트).

        `identity["slot_status"]`(`_safe_slot_status` dict·또는 None)이 있으면 upstream +
        submodule 역할(drift 경고 vs dev-ahead 정보 구별)을 dump 한다. 백본 미제공(None)이면
        빈 리스트(절 생략) — 기존 identity dump 무변경(fail-soft).
        """
        status = identity.get("slot_status") if identity else None
        lines = _format_slot_status_lines(status)
        if not lines:
            return []
        return ["", "### 슬롯 상태 (upstream·submodule·ADR-0051 T-β·T-0276)"] + lines

    def _protected_warning(self, repo: str, branch: str | None) -> str | None:
        """라이브 브랜치가 그 repo 보호목록(`board._repo_protected`)이면 그 브랜치명 (T-0076·소프트).

        보호목록이 아니거나 브랜치 조회불가(detached)·board 부재/헬퍼 부재면 None(경고 생략).
        board 직접 import 금지(touches 격리) — 주입/동적로드된 board 의 `_repo_protected` 를
        getattr 로 쓴다(DI 보존). 파싱 실패는 fail-soft None(소프트 경고는 깨지지 않는다).
        """
        if not branch:
            return None
        board_mod = self._board or _load_board()
        repo_protected = getattr(board_mod, "_repo_protected", None) if board_mod else None
        if repo_protected is None:
            return None
        try:
            protected = repo_protected(repo)
        except Exception:  # noqa: BLE001 — fail-soft: 파싱 실패는 경고 생략(소프트).
            return None
        return branch if branch in protected else None

    def _reconcile_protected_sidecar(self, repo: str) -> bool:
        """훅 sidecar 가 areas.md 보호목록과 다를 때만 재설치한다 (ADR-0072 트리거 ②·T-0417).

        훅이 실제로 읽는 건 areas.md 가 아니라 설치 시점에 복사된 sidecar
        (`.local/repo-hooks/<repo>/protected`)다 — *다른 clone/사용자*가 목록을 바꾸면 이 clone 의
        훅은 옛 값으로 계속 동작한다(silent). 세션 시작(그 세션의 첫 커밋보다 앞)에 그 드리프트를
        흡수한다.

        **비교 우선**: sidecar 를 읽어 resolve 된 목록과 같고 **훅이 배선돼 있으면** 아무것도 하지
        않는다(subprocess 0 — 정합이 정상 상태이므로 매 부트스트랩이 git config 를 때리지 않는다).
        다를 때만 `worktree_pool.install_protected_hook(repo, protected)` 를 부른다(훅 본문·sidecar·
        bare `core.hooksPath` 멱등 재설치). 훅 *본문* 신버전 배포는 `pm_update` 축(ADR-0071) 소유라
        여기선 드리프트만 본다.

        **배선 축을 함께 본다(should-fix)**: `install_protected_hook` 은 sidecar 기록 뒤 bare
        `core.hooksPath` 배선을 하는데, 뒤가 실패하면 **sidecar 는 최신인데 훅은 안 걸린** 상태가
        된다. 내용만 비교하면 이 상태에서 영구 침묵한다(보호가 꺼진 채). 판정은 `pm_config.
        protected_hook_wired`(읽기 전용·`_load_tool` DRY 소비 — 복붙 금지)를 쓰고, `None`("모름")은
        드리프트로 치지 않는다(오탐 0).

        보호목록 해소는 기존 `_protected_warning` 과 **같은 seam**(`board._repo_protected`·getattr
        DI)을 쓴다. board/worktree_pool 부재·헬퍼 부재는 fail-soft False(진입 무영향·drift 판정 자체를
        못 하므로 조용히 생략).

        **fail-soft 는 조용하지 않다(must-fix)**: drift 를 *발견한 뒤* 재설치가 실패하면
        (`install_protected_hook` 은 bare 부재·`core.hooksPath` 설정 실패 시 **예외 없이 False**),
        sidecar 는 여전히 stale 인데 "정합화했습니다" 를 내면 이 티켓이 닫으려던 값-연결 끊김을
        *정합하다고 거짓 보고*하는 것이 된다(사용자가 확인할 이유를 없앤다). 그래서 반환 False /
        예외 둘 다 **실패 안내 + 재실행 커맨드**를 loud 하게 내고 False 를 돌려준다. 진입은 여전히
        막지 않는다(보호 훅은 추가 가드).

        True = 재설치를 실제로 수행하고 **성공**함.
        """
        board_mod = self._board or _load_board()
        repo_protected = getattr(board_mod, "_repo_protected", None) if board_mod else None
        if repo_protected is None:
            return False
        wp = self._worktree_pool or _load_worktree_pool()
        install = getattr(wp, "install_protected_hook", None) if wp else None
        hooks_dir = getattr(wp, "REPO_HOOKS_DIR", None) if wp else None
        if install is None or hooks_dir is None:
            return False
        try:
            protected = list(repo_protected(repo))
            sidecar = Path(hooks_dir) / repo / "protected"
            if not sidecar.is_file():
                return False  # 훅 미설치 — 설치는 repo add/worktree add 축(여기선 drift 만).
            current = [line.strip()
                       for line in sidecar.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
            content_ok = current == protected
            # 배선 축 — sidecar 내용이 최신이어도 hooksPath 가 끊겼으면 훅은 발화하지 않는다.
            # `False`(명확히 미배선)만 드리프트로 친다; `None`(모름)·헬퍼 부재는 현행 유지.
            wired = self._protected_hook_wired(repo)
            if content_ok and wired is not False:
                return False  # 정합 — subprocess 0.
        except Exception:  # noqa: BLE001 — fail-soft: 판정 실패는 조용히 생략(오탐 0·drift 미확정).
            return False
        # 여기부터는 **drift 확정** — 성공/실패 어느 쪽도 조용히 넘기지 않는다. 원인 2종을
        # 구별해 보고한다: 목록이 바뀌었나(내용 drift) / 훅이 안 걸려 있나(배선 끊김·부분성공).
        cause = ("보호 브랜치 목록이 바뀌어" if not content_ok
                 else "훅이 bare `core.hooksPath` 에 배선돼 있지 않아(목록은 최신)")
        stale_state = (f"옛 목록({', '.join(current) or '(빈 목록)'})으로 동작"
                       if not content_ok else "아예 발화하지 않아 보호가 꺼진 상태")
        try:
            installed = bool(install(repo, protected))
        except Exception as exc:  # noqa: BLE001 — 실패해도 진입은 막지 않되 반드시 loud.
            installed = False
            reason = f"{exc.__class__.__name__}: {exc}"
        else:
            reason = ("install_protected_hook 이 실패를 보고 (bare 부재 또는 "
                      "`core.hooksPath` 설정 실패)")
        if not installed:
            print(
                f"[경고·0단계] {repo} {cause} 이 clone 의 훅을 **정합화하지 못했습니다** — "
                f"{reason}. 훅은 아직 {stale_state} 이며, 현재 목록"
                f"({', '.join(protected)})은 강제되지 않습니다(ADR-0072).\n"
                f"  → 재실행:  {self._protected_retry_command(repo)}"
                f"   (멱등·bare 부재면 `repo add {repo}` 먼저)",
                file=sys.stderr,
            )
            return False
        print(
            f"[알림·0단계] {repo} {cause} 이 clone 의 훅을 정합화했습니다 — "
            f"{', '.join(current) or '(빈 목록)'} → {', '.join(protected)} (ADR-0072).",
            file=sys.stderr,
        )
        return True

    def _protected_retry_command(self, repo: str) -> str:
        """보호목록 재실행 안내 커맨드 — `pm_config.protected_retry_command` 소비 (T-0417).

        안내가 실제 상태를 반영해야 한다(폴백이면 `default`·명시면 그 목록) — 그 분기를 여기서
        재구현하면 두 벌이 된다(`_protected_hook_wired` 동형·`_load_tool` DRY). pm_config/헬퍼
        부재면 **구체 값 없는** 안내로 떨어진다 — 상태를 모르는 채 목록을 추측해 안내하면 그걸
        실행한 사용자가 출처를 바꾸게 되므로, 값을 지어내지 않고 조회부터 안내한다.
        """
        pm_config = _load_tool("pm_config")
        retry_fn = getattr(pm_config, "protected_retry_command", None) if pm_config else None
        if retry_fn is not None:
            try:
                return retry_fn(repo, board=self._board or _load_board())
            except Exception:  # noqa: BLE001 — fail-soft: 아래 일반 안내로 강등.
                pass
        return (f"{_CARD_TOOL_INVOKE}/pm_config.py repo protected {repo}"
                "   (현재 상태 확인 후 재설정)")

    def _protected_hook_wired(self, repo: str) -> bool | None:
        """bare `core.hooksPath` 배선 여부 — `pm_config.protected_hook_wired` 소비 (T-0417).

        판정 로직을 재구현하지 않는다(`_load_tool` DRY 관용구 — 복붙 금지·ADR-0072 공용 헬퍼
        원칙 동형). 주입된 worktree_pool 을 그대로 넘겨 hermetic 테스트에서도 같은 대역을 본다.
        pm_config/헬퍼 부재·예외는 `None`("모름") — 호출부가 현행 동작(내용 비교만)으로 떨어진다.
        """
        pm_config = _load_tool("pm_config")
        wired_fn = getattr(pm_config, "protected_hook_wired", None) if pm_config else None
        if wired_fn is None:
            return None
        try:
            return wired_fn(repo, worktree_pool=self._worktree_pool or _load_worktree_pool())
        except Exception:  # noqa: BLE001 — fail-soft: 판정 실패는 "모름"(오탐 0).
            return None

    def _resolve_slot_base(self, repo: str) -> str | None:
        """그 repo 의 base 브랜치 — areas.md `base` 칼럼 (T-0341·명시 등록만·codex must-fix).

        슬롯 시대차(behind base) 비교 대상. board 직접 import 금지(touches 격리·`_protected_warning`
        동형) — 주입/동적로드된 board 의 `_repo_base`(areas.md `base` 칼럼)를 getattr 로 쓴다.
        **명시 등록된 base 만** 신뢰한다 — 미등록/areas 부재/`base` 칼럼 부재면 None(시대차 진짜
        생략·오탐 0). `_repo_protected` 는 폴백에 쓰지 않는다: 그 헬퍼는 미등록 repo 에도
        default(`main`)를 돌려줘 "미해소=생략" 을 잘못된 origin/main 판정으로 바꾼다(codex must-fix).
        board 부재/헬퍼 부재/파싱 실패도 None(fail-soft).
        """
        board_mod = self._board or _load_board()
        if not board_mod:
            return None
        repo_base = getattr(board_mod, "_repo_base", None)
        if repo_base is None:
            return None
        try:
            base = repo_base(repo)
        except Exception:  # noqa: BLE001 — fail-soft: 시대차는 소프트(실패=판정 생략).
            return None
        return base or None

    def _slot_scope_fetched(self, freshness: list[dict] | None) -> bool | None:
        """freshness 목록에서 현재 슬롯 cwd scope 의 `fetched` 상태 (T-0341 시대차 판정용).

        슬롯 cwd(`_worktree_cwd`)와 dir 이 일치하는 freshness scope 의 fetch 성공 여부를
        돌려준다 — 시대차 판정이 origin/<base> stale(offline) 여부를 *기존 freshness fetch
        결과에서 재사용*(신규 fetch 남발 금지·§결정). 매칭 scope 부재/경로 해소 실패 → None
        (호출부가 best-effort 로 계산·fetch 실패만 판정불가 처리).
        """
        try:
            wt = Path(self._worktree_cwd(self._bound_slot)).resolve()
        except Exception:  # noqa: BLE001 — fail-soft: cwd 해소 실패는 None(best-effort).
            return None
        for scope in freshness or []:
            try:
                if Path(scope.get("dir", "")).resolve() == wt:
                    return bool(scope.get("fetched"))
            except Exception:  # noqa: BLE001 — 경로 비교 실패는 다음 scope.
                continue
        return None

    def _slot_era_info(self, repo: str, freshness: list[dict] | None) -> dict | None:
        """슬롯 HEAD 가 base(main) 대비 behind N 커밋인지 판정한다 (T-0341·PM 69 stale-read).

        *기존 freshness fetch*(T-0217·슬롯 cwd scope)로 갱신된 `origin/<base>` 를 재사용해
        `git rev-list --count HEAD..origin/<base>` 로 behind 를 센다 — **신규 fetch 안 함**
        (§결정). **fetch 성공을 증명한 경우에만** behind 를 계산한다(stale ref 오신뢰 금지). 반환:
          - None — base 미해소(areas 미등록) · freshness scope 매칭 실패(fetch 미증명·codex
            suggestion) · online 인데 rev-list 실패(조용히 생략).
          - {"base": <br>, "undetermined": True} — 슬롯 cwd scope fetch 실패(offline)라
            origin/<base> 가 stale → 판정불가(fail-soft).
          - {"base": <br>, "behind": N} — behind 확정(fetch 증명·N==0 이면 최신·경고 무발화).
        """
        base = self._resolve_slot_base(repo)
        if not base:
            return None
        fetched = self._slot_scope_fetched(freshness)
        if fetched is False:
            # offline — origin/<base> 가 stale 스냅샷이라 behind 실측 불가(§결정 offline fail-soft).
            return {"base": base, "undetermined": True}
        if fetched is not True:
            # freshness scope 매칭 실패(None) — fetch 성공을 *증명 못 함*. stale origin/<base>
            # 위에서 behind 를 계산하면 오신뢰(false behind/최신)라 계산 안 하고 생략(codex suggestion).
            return None
        d = self._worktree_cwd(self._bound_slot)
        rc, out = self._run_git_fn(["-C", d, "rev-list", "--count", f"HEAD..origin/{base}"])
        if rc != 0:
            return None
        try:
            behind = int((out or "").strip())
        except (ValueError, TypeError):
            return None
        return {"base": base, "behind": behind}

    def _build_slot_identity_markdown(self, identity: dict) -> str:
        """lean identity + 상태점검 markdown — 세션명·라이브 브랜치·`--repo/--slot` 안내·다른 PM 현황.

        라이브 브랜치가 보호목록(T-0076)이면 🚫 경고 줄을 정체성 선언 직후 surface 한다
        (소프트 인지 — 하드 강제는 pre-push 훅).
        """
        repo = identity["repo"]
        session = identity["session"]
        slot = identity["slot"]
        slot_path = identity["slot_path"]
        branch = identity["branch"] or "(미지정)"
        others = identity["others"]
        protected_branch = identity.get("protected_branch")

        # pm_state 경로 병기 (T-0298) — worktree(=slot 상대경로)/cwd(절대)/pm_state(.local/slots)
        # 3항을 함께 실어 정체성 표기 혼선(`<slot>` placeholder / `work/<repo>_<N>` identity /
        # `.local/slots/<repo>_<N>/` 실경로)을 닫는다. 경로 산출은 기존 `_pm_state_display_path`
        # 재사용(중복 금지) — identity 슬롯키(`work/<repo>_<N>`)에서 `(repo, N)` 유도(_pm_state_
        # display_path 인스턴스 메서드와 동형 파싱·출력 표기만·해소 로직 무변경).
        slot_key: tuple[str, int] | None = None
        if slot.startswith("work/"):
            m = re.match(r"^(.+)_(\d+)$", slot[len("work/"):])
            if m:
                slot_key = (m.group(1), int(m.group(2)))
        pm_state_path = _pm_state_display_path(slot_key, self._areas_file)

        lines: list[str] = []
        lines.append("### multi-PM identity surface (lean·T-0074)")
        lines.append(
            f"- 당신은 **{repo} PM** · 세션=`{session}` · worktree=`{slot}` · "
            f"브랜치=`{branch}` · 보드=multi-PM 공유."
        )
        lines.append(
            f"- **보드/리스 조작은 `--repo {repo} --slot {session.rsplit('_', 1)[-1]}` 을 명시**한다 "
            "(정체성 = 에이전트 맥락·도구엔 명시 전달·ADR-0057)."
        )
        lines.append(f"- cwd (작업 슬롯): `{slot_path}`")
        lines.append(f"- pm_state (이 슬롯): `{pm_state_path}`")
        # 보호 브랜치 경고 (T-0076·소프트) — 라이브 브랜치가 보호목록이면.
        if protected_branch:
            lines.append(
                f"- 🚫 **보호 브랜치 `{protected_branch}`** — 여기서 커밋/푸시 금지. "
                "feature 브랜치를 checkout 후 작업하고, main 갱신이 필요하면 사용자에게 맡긴다 "
                "(pre-push 훅이 하드 차단·T-0076)."
            )
        # 슬롯 시대차 경고 (T-0341·PM 69 stale-read) — HEAD 가 base(main) 대비 behind N 이면
        # 옛-시대 코드로 작업할 위험을 경고(offline 이면 판정불가 fail-soft·최신/미해소면 줄 생략).
        era_line = _format_slot_era_warning(identity.get("slot_era"))
        if era_line:
            lines.append(era_line)
        # 슬롯 상태 (T-0276) — upstream + submodule 역할(drift 경고 vs dev-ahead 정보). 백본
        # 미제공/submodule 없으면 해당 줄 생략(fail-soft·submodule 줄 조건부).
        lines.extend(self._slot_status_block(identity))
        lines.append("")
        # 상태점검 — 다른 활성 PM 현황.
        lines.append("### 다른 활성 PM (상태점검)")
        if others:
            for other in others:
                other_branch = other["branch"] or "(미지정)"
                lines.append(
                    f"- `{other['session']}` · `{other['slot']}` · 브랜치=`{other_branch}`"
                )
        else:
            lines.append("- (다른 활성 PM 없음)")
        return "\n".join(lines)

    def _safe_command_card(self, identity: dict | None) -> str | None:
        """커맨드 카드를 fail-soft 로 렌더한다 — 실패하면 None(카드 절 생략·부트스트랩 유지).

        카드 렌더 실패(정체성 dict 결손·예기치 못한 예외)는 부트스트랩 자체를 깨뜨리면 안
        된다(ADR-0045 Consequences) — identity surface·board/git dump 는 이미 나갔으므로
        카드만 조용히 생략한다.
        """
        try:
            return self._build_command_card_markdown(identity)
        except Exception:  # noqa: BLE001 — fail-soft: 카드 렌더 실패는 절 생략(부트스트랩 유지·ADR-0045).
            return None

    def _build_command_card_markdown(self, identity: dict | None) -> str:
        """이 세션이 쓸 전 커맨드를 정체성 채운 완성형으로 dump 하는 커맨드 카드 (ADR-0045).

        identity surface 뒤에 코드 생성해, PM 이 --help 왕복 없이 세션 전체를 운영하게 한다
        ("--help 자체를 안 가게"·사용자 지시). 정체성(`--repo <repo> --slot <N>`·ADR-0057
        canonical — 구 ADR-0043 `--session <repo>_<N>` 을 supersede)은 **실값으로 보간**하고,
        사용자 입력(`T-NNNN`·`<PFX>`·`<요약>` 등)만 placeholder 로 남긴다.

        identity: lean(멀티-PM) 모드면 `slot`(`work/<repo>_<N>`)+`repo` 키를 담은 dict → 슬롯
                  식별자에서 슬롯 번호를 분리해 `--repo <repo> --slot <N>` 을 채운다(session 은
                  task+slot 에서 task명일 수 있어 번호 원천으로 안 씀·T-0390). None 또는
                  `slot`/`repo` 부재(솔로/legacy)면 정체성 인자 없는 현행 형태로 분기.

        숨은 전제 4대장(claim=promote 선행·prefix rename/merge=홈 git clean·livegate record=
        release-marked pin·migrate-identity=단일세션) + reid=홈 git clean 을 해당 커맨드 줄 바로
        아래 1줄 ⚠ 경고로 인접 배치한다(ADR-0045 Decision 2 — 별도 절 금지·인접성이 학습 보장).

        wave 운영(claim·regression·finish·qa·dev 위임·handoff·엔진 갱신)은 **스킬(`/pm-…`) 진입을
        primary** 로 올리고 감싸는 backbone 은 강등한다(ADR-0052 Decision 2 — boundary=래핑 스킬
        유무). 강등 줄은 pm_role skill 카탈로그의 "감싸는 내부 엔진" 열과 정확히 일치시킨다(카드=
        pm_role 표기·ADR-0045). **엔진이 CLI(`python3 tools/*.py` — board.py/ticket_finish.py/
        pm_handoff.py)일 때만** "직접 금지" 강등 줄로 그리고, 엔진이 Agent 툴(`/pm-dev-delegate`)·
        facade 셸(`/pm-update`=pm-update.sh)이면 python3 줄을 지어내지 않고 skill-only + 평문 note
        로 둔다. external_review 는 래핑 스킬 없는 별도 codex 게이트라 강등이 아니라 직접-CLI 예외
        (`board.py complete` 직접완료 경로·new/promote 도 동일). 강등 = 제거 아님: CLI backbone
        줄은 정체성 보간·⚠ 인접·argparse 정합 가드를 위해 남긴다. 규칙·why 는 재설명하지 않고
        (ADR-0045 비중복) 카드 상단 1줄 pointer 로 pm_role 규율 절을 가리킨다.
        """
        session = identity.get("session") if identity else None
        repo_name = identity.get("repo") if identity else None
        # 슬롯 **번호**는 슬롯 식별자(`identity["slot"]`=`work/<repo>_<N>`)의 *마지막* `_` 로 분리한다
        # (repo 내부 언더스코어와 무관·항상 마지막 세그먼트가 N). session 이 아니라 slot 을 번호 원천
        # 으로 쓰는 이유(T-0390·codex must-fix): task+slot 경로에선 session 이 task명(`job1`)이라
        # `session.rsplit` 전제가 깨져 `--slot job1` 류 오염 명령을 낳는다 — 슬롯 식별자는 명의와
        # 무관하게 항상 실 슬롯 번호를 담으므로 번호 단일 진실로 삼는다.
        slot_id = identity.get("slot") if identity else None
        slot_num = slot_id.rsplit("_", 1)[-1] if slot_id else None
        # 정체성 인자 — 멀티-PM 여부 게이트는 **session 존재**로 판정한다(종전 유지): session 결손
        # (불완전 dict)은 솔로 방어 렌더로 폴백(fail-soft·카드 절 무손상). 게이트 통과 시 번호는 위
        # slot_num(슬롯 식별자 파생)으로 채운다. lean=` --repo <repo> --slot <N>`·솔로=빈 문자열.
        sess = f" --repo {repo_name} --slot {slot_num}" if session and repo_name and slot_num else ""

        def cmd(rec: _CardCmd, comment: str = "", suffix: str = "", prefix: str = "") -> str:
            """공용 정의서 record → `python3 .project_manager/tools/<tool> <prefix><render><suffix>` 한 줄.

            커맨드 토큰(도구·서브커맨드·플래그)은 `rec`(공용 정의서·T-0362) 단일 진실에서 온다 —
            손 문자열 하드코딩 제거. `suffix`/`prefix` = 정체성(`--repo/--slot`·task 명) 등 caller 가
            실값 보간하는 꼬리/머리(ADR-0057 "정체성은 실값·사용자 입력만 placeholder"; pm_handoff 는
            정체성이 앞에 오므로 `prefix`).
            """
            line = f"{_CARD_TOOL_INVOKE}/{rec.tool} {prefix}{rec.render}{suffix}".rstrip()
            return f"{line}  # {comment}" if comment else line

        def skill(invocation: str, comment: str = "") -> str:
            """`/pm-…` 스킬 진입 줄(wave 운영 primary·ADR-0052).

            `python3` 로 시작하지 않으므로 카드↔CLI argparse 정합 가드·정체성 `--repo/--slot`
            검사(불변식 3)의 대상이 아니다 — backbone 은 아래 `engine()` 줄로 종속화한다.
            """
            return f"{invocation}  # {comment}" if comment else invocation

        def engine(rec: _CardCmd, note: str = "스킬이 부르는 내부 엔진·직접 금지",
                   suffix: str = "", prefix: str = "") -> str:
            """스킬에 종속된 backbone 줄 — 2-스페이스 들여쓰기 + '직접 금지' 주석(강등 표기).

            `python3 …` 로 시작해(들여쓰기는 strip 됨) 정체성 `--repo/--slot` 보간·카드↔CLI
            argparse 정합 가드의 대상으로 남는다(불변식 1·3 무손상). 스킬 줄(`/pm-…`)만 그
            가드 밖이다.
            """
            return "  " + cmd(rec, f"↳ {note}", suffix, prefix)

        # ── 모드별 렌더 (T-0362·§F12·⑰) — 현재 모드분만 dump(신호 대 잡음·컨텍스트 잠식 방지).
        # task 세션(`--task`·F1~F7)이면 task 커맨드, readonly 공유 슬롯(⑬·role=readonly)이면
        # 조회만, 그 외(슬롯/솔로)면 현행 슬롯 카드(+ task 모드 발견성 1줄). 안 쓸 커맨드는 안 뿌린다.
        task_name = getattr(self, "_task_name", None)
        role = (identity.get("role") if identity else None)
        if task_name:
            return self._task_command_card_lines(task_name, cmd, skill, engine) + self._codex_card_section()
        if role == "readonly":
            return self._readonly_command_card_lines(identity, cmd, skill) + self._codex_card_section()

        lines: list[str] = []
        lines.append("### 이 세션 커맨드 카드 (정체성 채움·--help 불요·단일 진실·ADR-0045)")
        # 정체성 헤더 — 실값 보간(placeholder 0). 솔로는 정체성 인자 불요 명시.
        if session:
            branch = (identity.get("branch") if identity else None) or "(미지정)"
            lines.append(
                f"정체성: 세션=`{session}` · 브랜치=`{branch}` — 보드/리스 조작은 "
                f"`--repo {repo_name} --slot {slot_num}` 명시(정체성=에이전트 맥락·도구엔 명시 전달)."
            )
            slot_path = identity.get("slot_path") if identity else None
            if slot_path:
                lines.append(
                    "실행 위치: 아래 커맨드는 multi-PM 공유 루트(`.project_manager` 있는 곳)에서 "
                    f"실행 — 코드 작업만 슬롯 cwd(`{slot_path}`)."
                )
        else:
            lines.append(
                "정체성: 솔로(단일 세션) — `--repo`/`--slot` 명시 불요(env `PM_SESSION_NAME` / "
                "local.conf `session=` 로 자동 해소)."
            )
        # 스킬-우선 운영 pointer — 규칙/why 는 pm_role 이 단일 진실(재설명 금지·ADR-0045 비중복·ADR-0052).
        lines.append(
            "> wave 운영은 스킬로 invoke·backbone 직접호출 금지 → pm_role §스킬 우선 운영 규율"
        )
        # task 모드 발견성 1줄 (T-0362·§F12·인터페이스 ③) — 슬롯 카드는 슬롯 커맨드만 뿌리되,
        # task 모드의 *존재*만 표기해 발견성과 컨텍스트 누수 방지를 양립(상세 커맨드는 그 모드에서).
        lines.append(
            "> 여러 repo 묶음 작업(task) = `--task <이름>` 모드 — 상세 커맨드는 그 모드 카드에서"
            "(부트스트랩 `--task <이름>` 로 진입)."
        )
        lines.append("")

        # 내 작업 보기 (read-only 조회·직접 — 래핑 스킬 없음·ADR-0047 자기 공간 우선).
        # 두 렌즈의 스코프 구분(ADR-0067) — --mine=user 축(내 것 전 슬롯) / --repo/--slot=세션 뷰
        # (그 세션 스트림 = 그 세션 생성 open + 그 세션 claim·session 라벨 축). "내 슬롯 작업"은
        # --repo/--slot 이 정확한 커맨드다(ADR-0057 신 표기).
        lines.append("# 내 작업 보기 (read-only 조회·직접 — ADR-0047 자기 공간 우선)")
        lines.append(cmd(
            _C_BOARD_LIST_MINE,
            "내 것 전 슬롯(내 open + 모든 슬롯의 내 claim)·user-wide 기본 조회",
        ))
        if session:
            lines.append(cmd(
                _C_BOARD_LIST,
                "이 세션 스트림(이 세션 생성 open + 이 세션 claim)·이 슬롯 작업 조회·조회 전용",
                suffix=sess,
            ))
        # ADR-0067: 무인자 `list` 는 이제 세션 기본 뷰(내 세션 스트림만·타 세션분 완전 비노출)다. 전체
        # 보드(모든 세션·타 사용자·경합 가시·backlog 확인)는 명시 `--all` — 기존 무인자 전체 뷰의 이관.
        lines.append(cmd(
            _C_BOARD_LIST_ALL, "전체 보드(모든 세션·타 사용자 포함) — 타 PM 열람·경합 가시·평시 불요",
        ))
        lines.append("")

        # 티켓 lifecycle 직접 (직접 — 래핑 스킬 없음·ADR-0052 예외). new/promote authoring +
        # complete 는 스킬 없는 fresh-adopter/concept(--allow-untested) 직접완료 경로(정상 wave
        # 종료=/pm-wave-finish→ticket_finish 가 complete 를 내부 수행·중복 실행 말 것).
        lines.append("# 티켓 lifecycle 직접 (래핑 스킬 없음·ADR-0052 예외)")
        lines.append(cmd(
            _C_BOARD_NEW, "draft 발행(본문은 board 밖에서 채움)",
        ))
        lines.append(cmd(
            _C_BOARD_PROMOTE, "draft → open(본문 채운 뒤·claim 선행조건)",
        ))
        lines.append(cmd(
            _C_BOARD_COMPLETE,
            "직접 완료 — fresh-adopter/concept(--allow-untested)·정상 wave 는 /pm-wave-finish",
        ))
        lines.append("")

        # wave 운영 (스킬 primary — CLI 엔진(board.py/ticket_finish.py/pm_handoff.py)만 backbone
        # 강등 줄·직접 금지). Agent 툴·facade 셸 엔진은 python3 줄을 지어내지 않고 skill-only +
        # 평문 note. 강등 줄은 pm_role skill 카탈로그의 "감싸는 내부 엔진" 열과 정확히 일치한다
        # (카드=pm_role 표기·ADR-0045). 숨은전제 ⚠ 는 강등 backbone claim 줄 바로 아래 인접(불변식 2).
        lines.append(
            "# wave 운영 (스킬로 invoke — backbone CLI 엔진은 직접 금지·ADR-0052)"
        )
        # /pm-wave-claim 엔진 = board.py show/lint/claim (pm_role 카탈로그 순서 — show/lint 는
        # DoD self-containment 검증 단계·read-only·⚠ 없음, claim 이 mutating·전제 ⚠ 인접).
        lines.append(skill("/pm-wave-claim T-NNNN", "ticket claim — DoD 자족 검증 + claim"))
        lines.append(engine(_C_BOARD_SHOW))
        lines.append(engine(_C_BOARD_LINT))
        lines.append(engine(_C_BOARD_CLAIM, suffix=sess))
        lines.append("  ⚠ claim 은 draft 티켓 거부 — 먼저 `promote T-NNNN`(본문 채운 뒤) 필요.")
        lines.append(skill("/pm-regression", "비차단 백그라운드 회귀 pre-warm + 완료 알림"))
        lines.append(engine(_C_BOARD_REGRESSION, suffix=sess))
        lines.append(skill("/pm-wave-finish T-NNNN", "ticket 완료 부기 — 회귀+log+board+stage"))
        lines.append(engine(
            _C_TICKET_FINISH,
            "스킬이 부르는 내부 엔진·직접 금지 — 내부서 board.py complete 수행",
        ))
        lines.append(skill("/pm-qa", "통합 검증 게이트 — 회귀+lint+git 단일 report"))
        lines.append(engine(_C_BOARD_REGRESSION, suffix=sess))
        lines.append(engine(_C_BOARD_LINT))
        lines.append(skill(
            "/pm-dev-delegate T-NNNN --role developer|code-reviewer",
            "orchestrator 위임 표준 프롬프트(dev / reviewer)",
        ))
        lines.append("  ↳ 엔진=Agent 툴(위임)·직접 CLI 아님 — skill-only.")
        # external_review = 래핑 스킬 없는 별도 codex 게이트(직접 OK 예외·reviewer 병행). 위임 직후 sibling.
        lines.append(cmd(
            _C_EXTERNAL_REVIEW,
            "codex 외부 교차검증 게이트 — 직접(래핑 스킬 없음)·reviewer 병행",
        ))
        lines.append(skill("/pm-handoff", "세션 종료 7단계 자동화"))
        handoff_prefix = f"--repo {repo_name} --slot {slot_num} " if session else ""
        lines.append(engine(_C_PM_HANDOFF, prefix=handoff_prefix))
        lines.append(skill("/pm-update", "엔진 갱신 — upstream freshness·manifest reconcile"))
        lines.append("  ↳ 엔진=pm-update.sh 파사드(freshness+reconcile)·직접 CLI 아님 — skill-only.")
        lines.append("")

        # 릴리즈 (직접 — 래핑 스킬 없음) — livegate record 는 release-marked pin(4대장 ③·인접 ⚠).
        # 정체성(`sess`)을 실어 실행가능 형태로 emit — multi-lease 홈에서 정체성 인자 없는 record 는
        # cwd 모호 fail-loud 이므로(T-0298), 이 세션 슬롯을 명시해 안내 명령이 dead-end 가 아니게 한다
        # (솔로는 `sess`="" → 무인자·현행 형태·leased <2 라 폴백 무변경).
        lines.append("# 릴리즈 (직접 — 래핑 스킬 없음)")
        lines.append(cmd(_C_BOARD_LIVEGATE, suffix=sess))
        lines.append(
            "  ⚠ record 는 `pytest -m release` 수집 pin 강제 — "
            "release-marked 0 수집이면 fail(릴리즈 차단)."
        )
        lines.append("")

        # ID·카테고리 유지보수 (드묾·전제 주의) — prefix rename/merge·reid=홈 git clean(4대장 ②·
        # reid 추가)·migrate-identity=단일세션(4대장 ④). 각 커맨드 줄 바로 아래 1줄 ⚠.
        lines.append("# ID·카테고리 유지보수 (드묾·전제 주의)")
        lines.append(cmd(_C_BOARD_PREFIX_LIST, "카테고리 현황(read-only)"))
        lines.append(cmd(_C_BOARD_PREFIX_RENAME))
        lines.append(
            "  ⚠ rename 은 홈 git working tree clean 필수 — "
            "wiki/log 참조 rewrite 라 미커밋 있으면 거부."
        )
        lines.append(cmd(_C_BOARD_PREFIX_MERGE))
        lines.append("  ⚠ merge 도 홈 git clean 전제(참조 rewrite·미커밋 있으면 거부).")
        lines.append(cmd(
            _C_BOARD_REID, "오발행 ID 교정(번호·prefix 무손실)",
        ))
        lines.append("  ⚠ reid 도 홈 git clean 전제(참조 rewrite 원자성·미커밋 있으면 거부).")
        lines.append(cmd(
            _C_BOARD_MIGRATE_IDENTITY, "ADR-0033 이전 데이터 일회성 backfill",
        ))
        lines.append(
            "  ⚠ migrate-identity 는 단일-세션 op — "
            "다른 세션이 claim/complete 중이면 실행 말 것(조용한 창에서 1회)."
        )
        lines.append("")

        # 정체성 불요 조회 (read-only·직접·cwd/conf/env 자동 해소·ADR-0045 Decision 3) — 래핑
        # 스킬 없는 read-only op 만. ticket_finish/external_review/pm_update 는 위 wave 운영서
        # 각 스킬의 강등 backbone 으로 이미 표기(ADR-0052).
        lines.append("# 정체성 불요 (read-only 조회·직접 — cwd/conf/env 자동 해소)")
        lines.append(cmd(_C_PM_LOG_TAIL))
        lines.append(cmd(_C_DOMAIN_AFFECTED))
        lines.append("")

        # 찾아가기 (부트스트랩 dump 에 없는 것 — 포인터만·정식 서술은 pm_role·T-0251).
        # 각 항목 "평시 안 읽음·필요할 때만"(ADR-0047 자기 공간 우선).
        self_tag = f"`({session})`" if session else "`(솔로/slot-1=무태그)`"
        lines.append("# 찾아가기 (부트스트랩에 없는 것 — 평시 안 읽음·필요할 때만)")
        lines.append(
            f"- 내 티켓 상세: `{_CARD_TOOL_INVOKE}/board.py show T-NNNN`"
        )
        lines.append(
            f"- 내 과거 세션: `wiki/log/current.md` 에서 자기 슬롯 태그 {self_tag} 검색(핸드오프 entry)"
        )
        lines.append("- 타 PM 현황: 부트스트랩 대시보드(상세는 그 슬롯 태그 log entry)")
        lines.append("- 현재-아키텍처: `wiki/architecture.md`(충돌 시 단일 진실)")
        lines.append("- 결정 히스토리: `wiki/decisions/README.md` 색인(ADR 상한)")
        lines.append("- 방법론·규율: `wiki/pm_role.md`")
        return "\n".join(lines) + self._codex_card_section()

    @staticmethod
    def _card_navigation_lines(self_tag: str) -> list[str]:
        """찾아가기 포인터 절 (모드 공용·부트스트랩 dump 에 없는 것 — 평시 안 읽음·필요할 때만)."""
        return [
            "# 찾아가기 (부트스트랩에 없는 것 — 평시 안 읽음·필요할 때만)",
            f"- 내 티켓 상세: `{_CARD_TOOL_INVOKE}/board.py show T-NNNN`",
            f"- 내 과거 세션: `wiki/log/current.md` 에서 자기 태그 {self_tag} 검색(핸드오프 entry)",
            "- 타 PM 현황: 부트스트랩 대시보드(상세는 그 슬롯 태그 log entry)",
            "- 현재-아키텍처: `wiki/architecture.md`(충돌 시 단일 진실)",
            "- 결정 히스토리: `wiki/decisions/README.md` 색인(ADR 상한)",
            "- 방법론·규율: `wiki/pm_role.md`",
        ]

    @staticmethod
    def _codex_card_section() -> str:
        """codex 하네스면 카드 codex 절(앞 공백 1줄 포함)을, 아니면 빈 문자열을 돌려준다.

        모든 모드 카드(슬롯·솔로·task·readonly) 렌더 끝에 append 되며, 호출이
        `_build_command_card_markdown`(→ `_safe_command_card` try/except) 안이라 감지/문자열화가
        터져도 fail-soft(절 생략·부트스트랩 무손상·ADR-0045). 비-codex 하네스는
        빈 문자열 → 카드 byte 무변(env 미설정=절 부재=정상·회귀 0).
        """
        return f"\n\n{_CODEX_CARD_SECTION}" if _is_codex_harness() else ""

    def _task_command_card_lines(self, task_name: str, cmd, skill, engine) -> str:
        """task 모드 커맨드 카드 (T-0362·§F12·F1~F7·⑥) — task 세션이 쓸 task-스코프 커맨드만 dump.

        task 는 슬롯 축과 직교(⑥)라 정체성 앵커가 task 명(`--task <name>`)이다(log/pm_state 귀속·
        board/lease 소유검사). 슬롯-전용 커맨드(worktree 관리·prefix rename/merge·livegate)는 안
        뿌린다(신호 대 잡음·컨텍스트 잠식 방지) — task 작업공간 대여/반납·task 정체성·task-스코프
        wave 운영만. 커맨드 토큰은 공용 정의서(`_CARD_TASK_CLI`) 단일 진실에서 온다(손 하드코딩 0).
        """
        # task 정체성 suffix/prefix — 실값 보간(ADR-0057). `<name>` 은 실 task 명(placeholder 아님).
        ti = f" --task {task_name}"
        lines: list[str] = []
        lines.append(
            "### 이 세션 커맨드 카드 (task 모드·정체성 채움·--help 불요·단일 진실·ADR-0045·T-0362)"
        )
        lines.append(
            f"정체성: task=`{task_name}` — 슬롯 축과 직교(⑥)·log/pm_state 는 task 앵커. "
            f"보드/리스 조작은 `--task {task_name}` 명시(정체성=에이전트 맥락·도구엔 명시 전달)."
        )
        lines.append(
            "> wave 운영은 스킬로 invoke·backbone 직접호출 금지 → pm_role §스킬 우선 운영 규율"
        )
        lines.append("")

        lines.append("# 내 작업 보기 (read-only 조회·직접 — ADR-0047 자기 공간 우선)")
        # task 스코프 렌즈(`list --task <이름>`·T-0365·[[ADR-0059]] Decision 10) — 이 task 명의
        # claim(claimed_by==<user>/<task>·⑲) + 내 소유 open backlog. `--task` 는 정체성 축이라 실값
        # 보간(suffix·ti)이고 base render 는 `list`(_C_BOARD_LIST) — 슬롯 카드의 slot-scoped 뷰와 동형.
        lines.append(cmd(
            _C_BOARD_LIST,
            "이 task 스코프(내 open + 이 task 명의 claim)·task 작업 조회·조회 전용",
            suffix=ti,
        ))
        lines.append(cmd(
            _C_BOARD_LIST_MINE,
            "내 것 전 슬롯·전 task(user-wide·직교 렌즈·ADR-0056)·기본 user 조회",
        ))
        lines.append("")

        # task 작업공간 (F2·⑥) — task 명의로 idle 슬롯 대여/반납. 자동 생성 안 함(풀 소진=사용자 게이트).
        lines.append("# task 작업공간 (F2·⑥ — task 명의 슬롯 대여/반납)")
        lines.append(cmd(
            _C_PC_ALLOC, "idle 최소번호 슬롯 대여(자동 생성 안 함·풀 소진 시 add 요청)", suffix=ti,
        ))
        lines.append(cmd(
            _C_PC_RELEASE, "작업완료 반납(소유검사 F3 — 이 task 명의 슬롯만)", suffix=ti,
        ))
        lines.append("")

        # task 정체성 (F5·F7) — prefix 지정/해제 + task 종료(일괄 반납·아카이브). name 은 위치인자(보간).
        lines.append("# task 정체성 (F5·F7 — prefix·종료)")
        lines.append(cmd(
            _C_PC_TASK_PREFIX, "ticket prefix 지정/변경/해제(`none`=해제·중간 변경 자유)",
        ))
        lines.append(cmd(
            _C_PC_TASK_END, "task 종료 — 일괄 idle 반납 + 서술 폴더 _ended 아카이브(worktree 미삭제)",
        ))
        lines.append(
            "  ⚠ task end 는 claimed 소진(⑲) + 전 슬롯 clean 전제 — 미완 claim/dirty 있으면 거부."
        )
        lines.append("")

        # wave 운영 (스킬 primary·ADR-0052) — 강등 backbone 은 task 정체성(`--task`) 보간. 슬롯 카드와
        # 동형이나 정체성 축만 slot→task. 숨은전제 ⚠ 는 claim 강등 줄 바로 아래 인접(불변식 2).
        lines.append("# wave 운영 (스킬로 invoke — backbone CLI 엔진은 직접 금지·ADR-0052)")
        lines.append(skill("/pm-wave-claim T-NNNN", "ticket claim — DoD 자족 검증 + claim"))
        lines.append(engine(_C_BOARD_SHOW))
        lines.append(engine(_C_BOARD_LINT))
        lines.append(engine(_C_BOARD_CLAIM, suffix=ti))
        lines.append("  ⚠ claim 은 draft 티켓 거부 — 먼저 `promote T-NNNN`(본문 채운 뒤) 필요.")
        lines.append(skill("/pm-regression", "비차단 백그라운드 회귀 pre-warm + 완료 알림"))
        lines.append(engine(_C_BOARD_REGRESSION, suffix=ti))
        lines.append(skill("/pm-wave-finish T-NNNN", "ticket 완료 부기 — 회귀+log+board+stage"))
        # ticket_finish 는 identity.task 있을 때만 task 작업공간 F6 로 회귀 cwd 를 해소한다 —
        # task 세션은 `--task` 를 실어야 slot/solo 로 오해소하지 않는다(다른 task backbone 과 동형).
        lines.append(engine(
            _C_TICKET_FINISH,
            "스킬이 부르는 내부 엔진·직접 금지 — 내부서 board.py complete 수행",
            suffix=ti,
        ))
        lines.append(skill("/pm-handoff", "task 세션 종료 7단계 자동화"))
        lines.append(engine(_C_PM_HANDOFF, prefix=f"--task {task_name} "))
        lines.append("")

        lines.append("# 정체성 불요 (read-only 조회·직접 — cwd/conf/env 자동 해소)")
        lines.append(cmd(_C_PM_LOG_TAIL))
        lines.append("")

        lines.extend(self._card_navigation_lines(f"`(task:{task_name})`"))
        return "\n".join(lines)

    def _readonly_command_card_lines(self, identity: dict | None, cmd, skill) -> str:
        """readonly 공유 슬롯(⑬·T-0358·role=readonly) 커맨드 카드 (T-0362·§F12) — 조회/갱신만 dump.

        readonly 는 무소유 공유 자산(session/pid 없음·배타 대여 없음·detached HEAD)이라 board
        claim/complete·set-base/rebase/dev/sync 는 의미가 없다(거부) — 조회(`status`)와 released
        최신화(`refresh`)만 뿌린다. worktree_pool 엔진은 build_parser 가 없어 CLI 강등 줄을 짓지
        않고 `/pm-worktree` 스킬 + 평문 note 로만 표기(카드↔CLI 정합 대상은 파서-backed 도구만).
        """
        lines: list[str] = []
        slot = (identity.get("slot") if identity else None) or "<slot>"
        lines.append(
            "### 이 세션 커맨드 카드 (readonly 공유 슬롯·조회 전용·⑬·T-0362)"
        )
        lines.append(
            f"정체성: readonly 공유 슬롯(`{slot}`·role=readonly·무소유 공유 자산·⑬) — 코드 읽기 "
            "기준면. 배타 대여/세션 바인딩 없음(claim/complete 대상 아님)."
        )
        lines.append(
            "> readonly 는 조회·갱신(refresh)만 — set-base/rebase/dev/sync·claim/complete 는 거부(⑬)."
        )
        lines.append("")

        lines.append("# 조회 (read-only·직접)")
        lines.append(cmd(_C_PC_STATUS, "풀/리스 상태 + 이 세션 repo/슬롯/branch"))
        lines.append(cmd(_C_PM_LOG_TAIL, "최근 log entry"))
        lines.append(skill(
            f"/pm-worktree status {slot}",
            "슬롯 git 구성 조회(role·base·head·submodule pin/drift·dirty)",
        ))
        lines.append("  ↳ 엔진=worktree_pool.py status (backbone·직접 CLI 아님).")
        lines.append("")

        lines.append("# 갱신 (readonly 슬롯 최신화·⑬)")
        lines.append(skill(
            f"/pm-worktree refresh {slot}",
            "released 최신 tip 으로 fetch → detached HEAD 이동(dirty=거부·조용한 reset 안 함)",
        ))
        lines.append("  ↳ 엔진=worktree_pool.py refresh (backbone·직접 CLI 아님).")
        lines.append("")

        lines.extend(self._card_navigation_lines("`(readonly 공유 슬롯)`"))
        return "\n".join(lines)

    def _build_identity_markdown(self, identity: dict) -> str:
        """identity surface markdown — "당신은 <repo> PM · worktree=… · branch=… · …"."""
        repo = identity["repo"]
        slot = identity["slot"]
        slot_path = identity["slot_path"]
        branch = identity["branch"] or "(미지정)"
        registered = identity["registered_repos"]
        areas = ", ".join(registered) if registered else f"{repo} (areas.md 미등록)"

        lines: list[str] = []
        lines.append("### multi-PM identity surface (ADR-0013·0011)")
        lines.append(
            f"- 당신은 **{repo} PM** · worktree=`{slot}` · branch=`{branch}` · "
            f"보드=multi-PM 공유 · 등록영역: {areas}"
        )
        lines.append(f"- cwd (작업 슬롯): `{slot_path}`")
        lines.append(
            "- 코드 작업은 이 슬롯 cwd 에서 — 보드/wiki 는 multi-PM 공유 `.project_manager`."
        )
        # 슬롯 시대차 경고 (T-0341·PM 69 stale-read) — HEAD 가 base(main) 대비 behind N 이면
        # 옛-시대 코드로 작업할 위험을 경고(offline 이면 판정불가 fail-soft·최신/미해소면 줄 생략).
        era_line = _format_slot_era_warning(identity.get("slot_era"))
        if era_line:
            lines.append(era_line)
        # 슬롯 상태 (T-0276) — upstream + submodule 역할(drift 경고 vs dev-ahead 정보). 백본
        # 미제공/submodule 없으면 해당 줄 생략(fail-soft·submodule 줄 조건부).
        lines.extend(self._slot_status_block(identity))
        return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────

def resolve_repo_arg(positional: str | None, flag: str | None) -> str | None:
    """positional `repo` 와 `--repo` alias 를 단일 repo 값으로 정합한다 (ADR-0043).

    handoff rewriter 산출(`/pm-bootstrap <repo> --slot N`·positional)과 기존 `--repo` 를 둘 다
    수용하되, 둘 다 주고 값이 다르면 추측하지 않고 fail-loud(`ValueError`) 한다. 한쪽만 주면 그
    값을, 둘 다 None(미지정)이면 None 을 흘려 무인자 자동바인딩(T-0178) 경로를 보존한다.
    """
    if positional is not None and flag is not None and positional != flag:
        raise ValueError(
            f"positional repo({positional}) 와 --repo({flag}) 값이 다르다 — "
            "하나만 주거나 같은 값으로 맞춰라 (추측 금지)."
        )
    # positional 우선(일치 시 동일값·한쪽만이면 준 쪽) — 둘 다 None 이면 None(자동바인딩 보존).
    return positional if positional is not None else flag


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_bootstrap.py",
        description="PM 세션 시작 부트스트랩 헬퍼 — 기계 측정 부분을 한 명령으로 dump 한다.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="기계 파싱용 JSON 출력 (기본: markdown).",
    )
    parser.add_argument(
        "--with-pytest",
        action="store_true",
        help="pytest 회귀 측정 opt-in (default skip — handoff entry 가 숫자를 기록한다고 가정).",
    )
    # ── multi-PM 모드 (ADR-0013·0011) — 무인자(솔로)면 미사용·현행 보존 ──
    # positional `repo`(nargs="?") — handoff rewriter 산출 `/pm-bootstrap <repo> --slot N`
    # (pm_handoff.py `_inject_slot_into_template`)을 raw CLI 가 그대로 수용하기 위한 흡수구
    # (ADR-0043 §2 기능 잔존·rewriter↔CLI 정합) — ADR-0057 로 canonical 이 분해형 `--repo/--slot`
    # 로 굳어진 뒤에도 **positional 은 유지**한다(진입 명령 관행·`--repo` 도 여전히 동작·불일치
    # fail-loud·사용자 결정). `--repo` 와 alias 관계(dest 는 분리해 불일치 감지) — 정합은 parse 후
    # `resolve_repo_arg` 가 한다(둘 다 주고 값 다르면 fail-loud). 미지정이면 None →
    # 무인자 자동바인딩(T-0178) 경로 보존.
    parser.add_argument(
        "repo_positional",
        nargs="?",
        metavar="repo",
        default=None,
        help=(
            "multi-PM 모드 — repo 이름(positional). `--repo` 의 alias — handoff rewriter 산출 "
            "`/pm-bootstrap <repo> --slot N` 정합용 (ADR-0043 §2). `--repo` 와 둘 다 주면 값 일치 필수."
        ),
    )
    # `--repo`/`--slot` — 공용 `identity_args.add_identity_args` 채택(ADR-0057 canonical·
    # T-0322 흡수·T-0315 정합). bare `--slot` fail-loud·slot<1 거부는 `parse_identity`(main())
    # 가 담당 — 로컬 중복 검증 제거(identity_args 문서의 slot≥1 계약 보존 참고).
    ia = _load_identity_args()
    if ia is None:
        raise RuntimeError(
            "identity_args 모듈을 로드할 수 없다 — .project_manager/tools/identity_args.py "
            "확인 (ADR-0057 canonical 정체성 인자·엔진 필수 peer 모듈)."
        )
    ia.add_identity_args(parser)
    parser.add_argument(
        "--branch",
        metavar="브랜치",
        default=None,
        help="multi-PM 모드 — alloc 할 작업스트림 브랜치 (--repo 전용·idle 슬롯 리스 후 checkout).",
    )
    parser.add_argument(
        "--resume",
        metavar="브랜치",
        default=None,
        help="multi-PM 모드 — 회전 재부착할 이전 작업스트림 브랜치 (--repo 전용·같은 슬롯 연속성).",
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
    args = build_parser().parse_args(argv)
    # positional `repo` 를 `--repo` alias 로 정합한다 (ADR-0043·rewriter↔CLI). 둘 다 주고 값이
    # 다르면 fail-loud, 한쪽만/일치면 그 값을 `args.repo` 로 접어 downstream(자동바인딩·alloc)이
    # 단일 필드만 보게 한다. 이후 로직은 positional 존재를 몰라도 된다.
    try:
        args.repo = resolve_repo_arg(args.repo_positional, args.repo)
    except ValueError as exc:
        build_parser().error(str(exc))
    # --branch/--resume 은 --repo multi-PM 모드 전용 — repo 없이 주면 오용 신호로 거부. 이 검사는
    # auto-resolve **앞**에 둔다 (T-0327): 기준은 파싱 직후의 사용자 명시값(`args.repo is None`
    # as-parsed)이다. auto-resolve 뒤였다면 자동바인딩이 args.repo 를 채운 뒤 이 가드를 통과해
    # branch/resume 이 자동바인딩된 슬롯에 silent 부착됐다. (--slot 단독 오용은 아래 parse_identity
    # 가 담당.)
    if args.repo is None and (args.branch is not None or args.resume is not None):
        build_parser().error("--branch/--resume 은 --repo multi-PM 모드 전용이다.")
    # guarded 자동바인딩 (T-0178·ADR-0035) — `--repo`/`--slot` 둘 다 없는 bare 무인자 호출에서,
    # `_resolve_session_slot` 으로 repo-안 default-1 규칙(slot1>단독>fail-loud)으로 해소한다.
    #   - 해소(`(repo,N)`) → 그 슬롯에 자동 bind(기존 `--slot` bind 경로 재사용·세션=`<repo>_<N>`).
    #   - None(solo·멀티-PM 미셋업) → 현행 솔로(무변경·자동바인딩 없음).
    #   - SlotResolutionError(멀티-PM 셋업 모호) → 침묵 폴백 대신 명시 에러(`--repo`/`--slot` 안내).
    # 명시 `--repo`/`--slot` 경로는 이 분기를 타지 않아 무변경. **`--task` 단독은 자동바인딩 제외**
    # (T-0353·⑥) — task 는 슬롯 0개로 시작 가능해야 하므로 슬롯 자동해소를 태우지 않는다(auto-task
    # 없음의 대칭 — task 는 auto-slot 도 안 한다). `--task X --repo Y [--slot N]` 은 repo 가 명시라
    # 어차피 이 분기 밖.
    if args.repo is None and args.slot is None and getattr(args, "task", None) is None:
        try:
            auto = _resolve_session_slot()
        except SlotResolutionError as exc:
            build_parser().error(str(exc))
        if auto is not None:
            args.repo, args.slot = auto
            # default-1(`{1,2}`→1)·단독·idle-필터 해소도 포함하므로 "단일 슬롯" 한정 문구 제거.
            print(f"슬롯 자동 해소: repo={args.repo} · slot={args.slot}",
                  file=sys.stderr)
    # 정체성 인자 fail-loud 검증(ADR-0057 canonical) — bare `--slot`(--repo 없이)·slot<1 은
    # 공용 `identity_args.parse_identity` 가 한 곳에서 담당한다(로컬 중복 검증 제거·메시지도
    # 전 도구 동일·카드↔CLI 정합). `build_parser()` 가 이미 `identity_args` 로드를 검증했으므로
    # (실패 시 RuntimeError) 여기서 재로드는 항상 성공한다.
    ia = _load_identity_args()
    if ia is not None:
        try:
            ia.parse_identity(args)
        except ValueError as exc:
            build_parser().error(str(exc))
    # --slot(직접 바인딩·lean)은 --branch/--resume(alloc 경로)과 배타 — 둘은 다른 경로다.
    if args.slot is not None and (args.branch is not None or args.resume is not None):
        build_parser().error("--slot 은 --branch/--resume 과 함께 쓸 수 없다 (bind vs alloc 경로).")
    # task 명 예약 패턴 거부 (⑥·T-0353) — `<등록 repo>_<N>` 슬롯 세션명과 시각적·기계적 충돌 방지.
    # 등록 repo 집합(areas 유래)을 넘겨 순수 검증(`is_reserved_task_name`)을 IO 층 밖에서 한다.
    if getattr(args, "task", None) is not None and ia is not None:
        if ia.is_reserved_task_name(args.task, _registered_repos()):
            build_parser().error(
                f"task 명 {args.task!r} 은 슬롯 세션 예약 패턴(<등록 repo>_<N>)과 충돌한다 — "
                "슬롯 정체성과 헷갈리지 않게 다른 이름을 쓰라 (⑥ 예약)."
            )
    bootstrap = PmBootstrap()
    return bootstrap.run(
        output_json=args.output_json,
        with_pytest=args.with_pytest,
        repo=args.repo,
        branch=args.branch,
        resume=args.resume,
        slot=args.slot,
        task=getattr(args, "task", None),
    )


if __name__ == "__main__":
    sys.exit(main())
