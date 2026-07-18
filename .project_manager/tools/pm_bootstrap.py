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
from typing import Callable

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

# 커맨드 카드 (ADR-0045) — 도구 호출 접두. 카드는 이 세션이 쓸 전 커맨드를 정체성 채운
# 완성형으로 dump 한다("--help 자체를 안 가게"·사용자 지시). `python3` 은 머신-불변 doc 표면
# 관례(T-0219·Windows 는 `py`·CLAUDE.md 노트). 경로는 multi-PM 공유 루트 기준 상대(도그푸딩
# 관례와 정합·PM 이 공유 루트에서 board/wiki 조작).
_CARD_TOOL_INVOKE = "python3 .project_manager/tools"


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
        return mod
    except Exception:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        return None


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
        return mod
    except Exception:  # noqa: BLE001 — fail-soft: 보호 경고는 소프트(로드 실패=경고 생략).
        return None


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
        return mod
    except Exception:  # noqa: BLE001 — fail-soft: 차수/dump 는 소프트(로드 실패=placeholder).
        return None


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
        return mod
    except Exception:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        return None


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


# claimed 행 파서 — 정체성 토큰 앵커 (T-0331·codex must-fix 1). board.py cmd_list 행 형식:
# `  [{status:7s}] {id}  {title:60s}  {claimed:18s}  {tags}`. claimed_by 는 `<user>/<session>`
# (ADR-0033 ③) 또는 legacy 슬롯-only `<session>`(구형 `pm-2` 류 비-`_N` 값 포함 — board 는 여전히
# 수용). 파싱은 **고정폭 컬럼 위치 기반**(codex R1 처방·R3 채택): board.py cmd_list 의 출력 포맷
# `  [{status:7s}] {id}  {title[:60]:60s}  {claimed:18s}  {tags}` 에서 제목 컬럼은 60폭 고정
# (사전 절단·패딩)이라, id 뒤 2공백 다음 60자를 건너뛴 위치의 첫 토큰이 곧 claimed_by 다.
# 내용 기반 매칭(EOL-앵커 정체성 토큰)은 세 결함 클래스를 연쇄로 냈다(codex R1~R3): 이중공백
# 제목 오추출 → `/` tag 행 통째 누락 → `_N`-꼬리 tag/legacy 세션형 오인. 위치 기반은 제목·tags
# 내용을 아예 읽지 않아 그 클래스 전체가 소멸한다. 포맷 drift 백스톱 = board.py cmd_list 를
# 실제 실행해 stdout 을 먹이는 통합 테스트(should-fix·format 복사본 coupling 제거).
_CLAIMED_LINE_HEAD_RE = re.compile(
    rf"^\s*\[claimed\s*\]\s+({_TICKET_ID})  "  # status + id + 2공백(포맷 리터럴) — 이후 60폭 제목 컬럼
)
_TITLE_COL_WIDTH = 60  # board.py cmd_list `{title[:60]:60s}` 와 정합(통합 가드가 drift 시 red)


def parse_other_session_claims(
    board_output: str, my_session: str | None
) -> dict[str, list[str]]:
    """board list 출력에서 *내 세션이 아닌* 세션의 claimed 티켓을 세션별로 묶는다 (T-0331).

    부트스트랩 대시보드가 이미 "다른 활성 PM" 을 보여주듯(조정용 표면·ADR-0056 위반 아님),
    공유 backlog 오독(PM 69: 타 슬롯이 방금 claim 한 티켓을 "내 몫" 으로 착각)을 막는 조정
    신호다. claimed 행의 `claimed_by`(`<user>/<session>`·ADR-0033 ③ 또는 legacy 슬롯-only)에서
    세션(마지막 `/` 뒤·`/` 없으면 값 전체)을 뽑아 내 바운드 세션(`my_session`·`<repo>_<N>`)과
    같은 것은 뺀다.

    `my_session` 미해소(진짜 솔로·None)면 "내 것" 을 세션으로 못 가려 전부 뺀다(빈 dict) — 이
    조정 신호는 슬롯 바운드(멀티-PM/self-host)에서만 낸다.

    **데이터 출처**: `_collect_board` 가 넘기는 `board_output` 은 무렌즈 full board `list`
    (`--status claimed`·전 세션·사용자 무관·codex must-fix 2)다 — 스코프/유저 뷰가 아니므로 동일
    사용자 타 슬롯 + 타 사용자 claim 이 모두 담겨, 슬롯-바인딩 경로에서도 타 세션이 확실히 병기된다.

    반환: {session: [ticket_id, ...]} — 등장 순서 보존. 내 세션·미해소는 미포함.
    """
    if not my_session:
        return {}
    claims: dict[str, list[str]] = {}
    for line in board_output.splitlines():
        match = _CLAIMED_LINE_HEAD_RE.match(line)
        if not match:
            continue
        ticket_id = match.group(1)
        # 고정폭 제목 컬럼(60) + 2공백 구분자 건너뛴 위치의 첫 토큰 = claimed_by (제목·tags 내용 불독).
        after_title = line[match.end() + _TITLE_COL_WIDTH:]
        claimed_tokens = after_title.split()
        if not claimed_tokens:
            continue  # claimed 컬럼 부재(포맷 밖 행) — skip
        claimed_by = claimed_tokens[0]
        if claimed_by == "-":
            continue  # 누락/placeholder claimed_by 방어(tags 오인 차단·codex R4 suggestion)
        session = claimed_by.rsplit("/", 1)[-1]
        if session == my_session:
            continue
        claims.setdefault(session, []).append(ticket_id)
    return claims


def _format_other_session_claims_line(other_claims: dict[str, list[str]]) -> str | None:
    """타 세션 claimed 현황 1줄을 만든다 — 1건 이상이면 문자열, 0건이면 None(줄 생략) (T-0331).

    형식: `- 타 세션 진행(claimed): N건 — <세션>: T-a, T-b · <세션2>: T-c` (티켓 제목 무노출·ID
    만). N = 타 세션 claimed 티켓 총수(격리 뷰 아님·조정용·ADR-0056 위반 아님).
    """
    if not other_claims:
        return None
    total = sum(len(tickets) for tickets in other_claims.values())
    groups = " · ".join(
        f"{session}: {', '.join(tickets)}" for session, tickets in other_claims.items()
    )
    return f"- 타 세션 진행(claimed): {total}건 — {groups}"


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
# 캡처는 **canonical `<repo>_<N>` 만**(`[A-Za-z0-9][A-Za-z0-9_-]*_\d+`·pm_config `_REPO_NAME_RE`+`_N`)
# 으로 제약한다(should-fix) — 서술형 괄호(`PM 4차 (아침 대화)`)를 세션 태그로 오인해 솔로에서
# entry 를 drop 하지 않게. 비-canonical 괄호는 `.*` 로 흡수돼 session=None(무태그 폴백).
_LOG_HANDOFF_HEADER_RE = re.compile(
    r"^(?P<line>## \[\d{4}-\d{2}-\d{2}\]\s+handoff\s*\|\s*PM\s+(?P<num>\d+)차"
    r"(?:\s*\((?P<session>[A-Za-z0-9][A-Za-z0-9_-]*_\d+)\))?.*)$",
    re.MULTILINE,
)


def _session_owns_untagged(bound_session: str | None) -> bool:
    """무태그 handoff entry 가 이 세션 소유인지 판정한다 — 솔로(None)/slot-1 만 True (ADR-0044).

    무태그 entry = 태그 도입(ADR-0044) 이전 로그 또는 솔로 핸드오프 → 솔로/slot-1 귀속
    (연속성 보존·제로 마이그레이션). **slot-2+ 는 무태그를 무시**하고 자기 태그 entry 만 센다
    (핵심 회귀 가드·codex 제언). bound_session 이 None(솔로)이거나 canonical `<repo>_1`(slot-1)이면
    True, 그 외(slot-2+·비정형 non-None)면 False.
    """
    if bound_session is None:
        return True
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


def _slot_count_label(session: str) -> str:
    """`<repo>_<N>` 세션 키 → 카운트 스코프 라벨 `"slot N"` (ADR-0056 #6·T-0312).

    말단 `_<N>` 의 숫자 N 을 뽑아 `"slot N"` 으로 라벨한다 — bootstrap 카운트가 `--mine`(user·전
    슬롯)이 아니라 *그 슬롯 정체성*(`list --session <repo>_<N>`)으로 조회됐음을 announce 한다(S1
    mislabel 근절). 비-슬롯형(말단이 숫자 아님·커스텀 세션명)이면 전체 세션명으로 폴백한다.
    """
    tail = session.rsplit("_", 1)[-1]
    return f"slot {tail}" if tail.isdigit() else f"slot {session}"


# open 카운트만의 스코프 라벨 — done/claimed/blocked 의 슬롯-스코프 라벨과 다른 축 (T-0331).
# open 은 미claim 이라 슬롯이 없다(board.py `_ticket_in_view`): slot-scoped 뷰에서도 (a) 는
# 슬롯무관 backlog 로 `--mine` 과 동일한 전역 대기열 수다(ADR-0056 #3·산출 불변). slot-scoped
# 라벨("(slot N)")을 그대로 붙이면 그 공유 대기열(그중 타 슬롯이 방금 claim 한 것 포함)을
# "내 슬롯 몫" 으로 오독한다(PM 69 slot-2 실증) → open 만 이 라벨로 정정한다.
_OPEN_SCOPE_LABEL = "공유 backlog·슬롯무관"


def _format_board_counts_line(counts: dict[str, int], scope_label: str = "mine") -> str:
    """board 카운트 한 줄을 만든다 — 수집 스코프 라벨 명확화 (T-0194·T-0312·T-0331).

    `counts` 는 `_collect_board` 가 뽑은 스코프 값이다 — status 별로 "내 area open" 또는 "내
    claim" 만 센 값이라 실측(예 done 25)이 전체 done(184) 과 크게 다를 수 있다(done/claimed/
    blocked 는 전체보다 훨씬 작을 소지가 큼). `scope_label` 로 그 스코프를 명시해 "전체 done"
    처럼 오독하지 않게 한다 — 솔로/무바인딩은 `"mine"`(user·전 슬롯), 명시 슬롯 바인딩(`--repo`/
    `--slot`·multi-PM)은 `"slot N"`(그 슬롯 정체성으로 조회·ADR-0056 S1·`list --session`↔카운트 정합).

    **open 만 예외**(T-0331): done/claimed/blocked 는 슬롯-스코프 카운트라 `scope_label` 을
    붙이지만, open 은 슬롯무관 공유 backlog(전역 수)라 `_OPEN_SCOPE_LABEL` 로 정정한다 —
    slot-scoped 라벨을 붙여 공유 대기열을 "내 슬롯 몫" 으로 오독하는 것(PM 69)을 못박아 막는다.
    """
    parts = [
        f"{label}: {counts[key]} ({_OPEN_SCOPE_LABEL if key == 'open' else scope_label})"
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
        전체 보드(contention 가시)는 무플래그 `board list` 로 PM 이 명시 조회한다.

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
        slot_session = self._bound_session_name() if self._bound_slot else None
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

        counts = parse_board_counts(output)
        open_tickets = parse_open_tickets(output)

        # done 전용 재조회 — default 뷰가 done 을 접어(T-0197) 위 counts["done"] 이 항상 0
        # 이 되는 회귀를 막는다. 이 호출이 실패해도(구버전 board.py 등) done=0 으로 fail-soft
        # 하고 abort 하지 않는다(핵심 list 는 이미 성공했으므로 done 카운트만 저하 없는 선에서).
        done_rc, done_output = self._run_board_fn(["list", "--status", "done", *lens])
        if done_rc == 0:
            counts["done"] = parse_board_counts(done_output)["done"]

        # 타 세션 claim 현황 (T-0331·codex must-fix 2) — **전용 무렌즈 full board 조회**. 위
        # default 뷰(--repo/--slot·--mine)는 board.py `_ticket_in_view` 상 내 claim 만 담아 타
        # 세션(동일 사용자 타 슬롯·타 사용자)이 안 나오므로, 슬롯-바인딩 경로에서도 확실히
        # 병기하려면 렌즈 없는 전-세션 조회가 필요하다(dormant 출하 금지·PM 결정). fail-soft:
        # 실패(rc≠0·구버전 board.py)면 빈 dict → 현황 줄 생략(핵심 list 는 이미 성공·abort 안 함).
        claimed_rc, claimed_output = self._run_board_fn(["list", "--status", "claimed"])
        if claimed_rc == 0:
            other_claims = parse_other_session_claims(
                claimed_output, self._bound_session_name()
            )
        else:
            other_claims = {}

        # `--gate` 로 호출 — 차단 카테고리에만 rc=1, advisory(status drift·
        # unstable-ref-advice)는 rc=0 (board.cmd_lint). dump-then-warn(T-0195) —
        # blocking 이어도 여기서 abort 하지 않고 플래그만 실어 반환한다.
        lint_rc, lint_output = self._run_board_fn(["lint", "--gate"])
        lint_result = parse_lint_result(lint_output)

        return {
            "counts": counts,
            "counts_scope": counts_scope,
            "open_tickets": open_tickets,
            "other_claims": other_claims,
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
        b_rc, b_out = self._run_git_fn(["-C", d, "symbolic-ref", "--short", "HEAD"])
        if b_rc == 0 and b_out.strip():
            scope["branch"] = b_out.strip()
        else:
            # symbolic-ref 실패(rc≠0·빈 출력) = detached HEAD — 자동 pull 대상 아님.
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
        """차수 유도/본문 dump 슬롯 필터용 bound 세션 키(`<repo>_<N>`) (ADR-0044·MF-1 write↔read 대칭).

        명시 multi-PM 모드(`_bound_slot`=`work/<repo>_<N>`)면 `work/` 접두를 벗긴 `<repo>_<N>`.
        무인자(솔로)면 handoff **write 측**(`_resolve_session_worktree_slot`)과 **같은 경로로**
        자동해소한다 — 단일 self-host 무인자 부트스트랩이 그 슬롯(`<repo>_<N>`)으로 바인딩되어,
        handoff 가 무인자로 write 한 자기 태그 entry(+무태그 히스토리)를 **둘 다** 자기 것으로
        되읽는다. 이 write↔read 대칭이 없으면(구: 무인자→None→솔로 read) 단일 self-host 에서
        handoff 가 쓴 `(<repo>_1)` 태그 entry 를 부트스트랩이 버려 차수 유실·T-0208 stale 침묵
        무력화가 났다(adopter#0 실증 버그). 등록 repo 0개(진짜 솔로)·모호·판정불가 → None.
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

        # fresh 슬롯 규칙 (ADR-0044): 명시 슬롯 바인딩인데 자기 슬롯 차수가 전혀 안 잡히면
        # (pm_state·log 둘 다 미해소) 1차부터 — slot-2+ 는 무태그 기존 로그를 무시하므로 fresh
        # 슬롯이 placeholder(`?`) 대신 슬롯-first(1차)로 announce 된다. 솔로(bound None)는 현행
        # placeholder 보존(회귀 0) — 이 규칙은 명시 슬롯(bound_session 해소)에서만 발동한다.
        if bound_session is not None and not isinstance(session_num, int):
            session_num = 1

        # fresh 슬롯 판정 (T-0284): 명시 슬롯이 바인딩됐는데 자기 pm_state 도(파일 부재) 자기 슬롯
        # handoff 도(log_num None) 전혀 없으면 = 이 슬롯의 첫 세션이라 복구할 컨텍스트가 없다. 이땐
        # "미해소/직접 확인" placeholder(스크램블 유발) 대신 명시 "fresh" 배너를 dump하도록 빌더에
        # 신호한다(surface-only·자동 pm_state 생성 안 함·ADR-0035). 솔로(bound None)는 슬롯 개념이
        # 없어 fresh 배너 대상 아님(현행 placeholder 보존·회귀 0).
        fresh_slot = bound_session is not None and state_text is None and log_num is None

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

        `wiki/log/dashboard.md` 부재 시 폴백 — leased 엔트리에서 자기 세션(`_bound_session_name`)
        제외분을 `[{"session","slot"}]` 로 반환한다. 장부 부재/깨짐/leased 0개는 빈 목록.
        """
        pairs = self._read_leased_sessions_slots()
        if not pairs:
            return []
        bound = self._bound_session_name()
        return [{"session": s, "slot": slot} for s, slot in pairs if s != bound]

    def _collect_dashboard_others(self) -> dict | None:
        """타 슬롯 대시보드 섹션(자기 제외) light dump 데이터를 수집한다 (ADR-0047 ③·T-0260).

        - `wiki/log/dashboard.md` 존재 → `pm_handoff.parse_dashboard_sections`(동적로드·DRY)로
          파싱해 자기 세션(`_bound_session_name`) 제외 + **활성(leased) 슬롯과 교집합** 섹션들을
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
                    bound = self._bound_session_name()
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

        # 차수 announce (T-0179) — bound slot pm_state 에서 추론한 `PM <N>차`. 미해소/추론불가는
        # placeholder(`?`) — self-surface 헤더이지 강제 아님(crash 금지).
        session_label = _format_session_label(handoff_ctx)
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
        # open = 내 claim-가능 backlog(미claim·슬롯무관·ADR-0056 #3) — 위 카운트의 claimed(=이
        # 슬롯 진행분)와 다른 축이다(open 은 슬롯 스코프 아님). 헷갈리지 않게 "backlog·슬롯무관" 명시.
        if open_tickets:
            lines.append(f"- open ticket (claim 가능·backlog·슬롯무관): {', '.join(open_tickets)}")
        else:
            lines.append("- open ticket (claim 가능·backlog·슬롯무관): (없음)")
        # 타 세션 claim 현황 (T-0331) — 공유 backlog 중 타 세션이 이미 붙든 것을 같은 화면에 병기해
        # PM 69 오독(공유 대기열을 "내 몫" 으로 착각)을 막는다. 0건이면 줄 생략(None).
        other_claims_line = _format_other_session_claims_line(board.get("other_claims") or {})
        if other_claims_line:
            lines.append(other_claims_line)
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
        # T-0194·T-0312)를 요약 문장체로 표기(선두 "- " 없이·마침표로 마감). open 만 슬롯무관 공유
        # backlog 라벨(`_OPEN_SCOPE_LABEL`·T-0331·codex must-fix 3) — 요약부도 카운트 라인과 동일
        # 규칙으로 정정해 "open N (slot 1)" 오독을 양쪽에서 못박는다.
        _scope = board.get("counts_scope", "mine")
        board_summary = (
            f"done {counts['done']} ({_scope}) / open {counts['open']} ({_OPEN_SCOPE_LABEL}) / "
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
        # 이전에 즉시 중단한다. 엔진 앵커(무조건)+슬롯 실재·점유·기록정합(lean 조건)+보호브랜치 warn.
        # solo(repo None)/alloc(slot None)는 슬롯 검사 자연 no-op·앵커만 무조건(결정 2·
        # [[solo-is-subset-of-multipm]]). alloc 앞단에 둬 앵커 거부 시 신규 lease 잔존을 예방한다.
        preflight_rc = self._phase0_preflight(repo, slot)
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
                    data["worktree"] = self._bind_and_identity(repo, slot)
                elif multipm_alloc:
                    data["worktree"] = alloc_identity  # 위 앞단 alloc 결과 재사용(재-alloc 금지).
                if data.get("worktree") is not None:
                    # 슬롯 시대차 (T-0341) — freshness fetch 뒤라 origin/<base> 최신 재사용.
                    data["worktree"]["slot_era"] = self._slot_era_info(repo, freshness)
                if task_info is not None:
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
                    identity = self._bind_and_identity(repo, slot)
                    # 슬롯 시대차 (T-0341) — freshness fetch 뒤라 origin/<base> 최신 재사용.
                    identity["slot_era"] = self._slot_era_info(repo, freshness)
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
    # +보호브랜치 warn. 판정 근거는 전부 기계(장부·git·compare 프리미티브)이고 로직 중복은 없다 —
    # 엔진 앵커는 board T-0345 가드를, 기록 vs live 는 worktree_pool T-0350 compare(㉒)를 **소비만**한다.

    def _phase0_preflight(self, repo: str | None, slot: int | None) -> int:
        """0단계 — dump/alloc 이전 위치·소유·상태 검증 (⑧·spike §F1b). 0=통과·비-0=거부(FAIL-LOUD).

        검사:
          1. **엔진 앵커** (무조건·cwd 무관·F6) — REPO(엔진 파일 위치)가 PM 홈이 아니라 worktree
             사본이면 거부 (T-0345 클래스·`board._pm_home_worktree_misanchor` 소비·부트스트랩은 현행
             이 클래스에 무방비였다).
          2~5 **슬롯 검사** — 슬롯이 명시된 lean 경로(`--repo --slot`·무인자 자동해소 포함)만 돈다.
             solo(repo None)·alloc(slot None)는 슬롯이 없어 자연 no-op(결정 2·[[solo-is-subset-of-multipm]]).
            2. **작업공간 실재** — 장부 lease 도 없고 폴더도 없으면 거부(phantom 슬롯 바인딩 방지).
            3. **타 점유자** — 다른 세션이 그 슬롯을 leased 로 보유하면 거부(결정 ③·readonly ⑬ 예외).
            4. **보호브랜치/origin-추적** — **warn 만**(거부는 후속 T-0360·⑧ 이행 순서·readonly 예외).
            5. **기록 vs live** — `compare_slot_git` 소비(㉒·불일치=FAIL-LOUD·미기록=loud+질의 훅·T-0352).
        """
        # 1. 엔진 앵커 (무조건) — worktree 사본에서 부트스트랩하면 거부.
        if self._reject_worktree_copy_anchor():
            return 1
        # 2~5 슬롯 검사 — lean(명시 슬롯)만. solo/alloc 은 슬롯이 없어 자연 no-op(결정 2).
        if repo is None or slot is None:
            return 0
        slot_id = f"work/{repo}_{slot}"
        session = f"{repo}_{slot}"
        wp = self._resolve_worktree_pool()  # multi-PM 인데 풀 부재면 명시 에러(SystemExit·dump 이전).
        lease = self._phase0_find_lease(wp, slot_id)
        # readonly 공유 슬롯(⑬) carve-out — 타-task 점유·보호브랜치 검사 비적용 예외 자리(§F11·T-0358
        # 이 채운다). 지금은 lease.extra 의 role 만 읽는 훅 — 현행 슬롯엔 role 이 없어 항상 False(회귀 0).
        readonly = self._phase0_is_readonly(lease)
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
        # 2b. 불완전 생성(creating·T-0295) — **세션 동일 여부·readonly 무관** 차단(별도 조건). readonly
        #     carve-out 미적용: readonly 는 *점유/보호브랜치* 예외지 *불완전 생성* 예외가 아니다(반쯤
        #     만든 슬롯은 readonly 여도 못 쓴다). bind_slot 이 기존 엔트리를 무조건 leased 로 덮어(T-0295
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
        # 3. 타 점유자 (readonly ⑬ 예외 — 공유가 정상).
        if not readonly:
            holder = self._phase0_other_holder(lease, session)
            if holder is not None:
                print(
                    f"[중단·0단계] 슬롯 {slot_id} 을(를) 다른 세션 `{holder}` 이(가) 점유 중입니다 "
                    f"(leased) — 남의 작업공간에 바인딩할 수 없습니다(결정 ③).\n"
                    f"  → 그 세션의 완료를 기다리거나, 다른 슬롯을 쓰거나, 새 슬롯을 alloc 하세요.",
                    file=sys.stderr,
                )
                return 1
        # 4. 보호브랜치/origin-추적 = warn 만 (거부는 T-0360·readonly 예외·detached).
        if not readonly:
            self._phase0_protected_warn(wp, repo, slot_id)
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
        """그 슬롯이 readonly 공유 자산(⑬·`role="readonly"`)인가 — 0단계 타-점유·보호브랜치 검사의
        carve-out 예외 자리 (spike §F11·⑬=T-0358 이 채운다).

        readonly 슬롯은 배타 대여 없이 **공유**하는 research 전용 자산이라(§F11) 타-task 점유·보호브랜치
        (detached=브랜치 없음) 검사가 **비적용**이다. 지금은 lease.extra(additive·T-0350 미지키 보존)의
        `role` 만 읽는 훅 — 현행 슬롯엔 role 이 없어 항상 False(회귀 0). readonly 도입(T-0358) 후
        이 훅이 활성된다. lease None/미지 스키마는 False(fail-soft)."""
        if lease is None:
            return False
        extra = getattr(lease, "extra", None) or {}
        try:
            return extra.get("role") == "readonly"
        except Exception:  # noqa: BLE001 — fail-soft: 미지 extra 스키마는 non-readonly.
            return False

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

    def _phase0_protected_warn(self, wp, repo: str, slot_id: str) -> None:
        """슬롯 live 브랜치가 보호목록(main 등)이면 **경고만** 낸다 (⑧·이 티켓=warn·거부는 T-0360).

        ⑧의 보호브랜치/origin-추적 *거부*는 채택자-facing BREAKING 급 — main-checkout 슬롯을 쓰던
        채택자를 갱신 즉시 깨뜨리고, adopter#0 slot-1 도 현재 main+origin/main(릴리즈 livegate·codex
        `--paths` 기준). → 이 티켓은 **warn 만 출하**하고, 거부 활성은 readonly 슬롯(⑬)이 main-참조
        역할을 이전한 뒤 후속 T-0360 이 한다(spike §F1b 이행 순서·[[cross-cutting-breaking-blast-radius]]).
        `_protected_warning`(T-0076) 헬퍼를 재사용해 보호 판정만 하고, 경고에 T-0360/readonly 해소를
        안내한다. detached/조회불가/board 부재는 조용히 생략(fail-soft·soft 경고는 안 깨진다)."""
        try:
            branch = wp.current_branch(slot_id)
        except Exception:  # noqa: BLE001 — fail-soft: 브랜치 조회불가는 경고 생략.
            return
        protected = self._protected_warning(repo, branch)
        if protected is None:
            return
        print(
            f"[경고·0단계] 슬롯 {slot_id} 이(가) 보호 브랜치 `{protected}` 를 직접 체크아웃한 상태입니다 "
            f"— 지금은 경고만 하지만 향후(T-0360) 진입 거부로 전환됩니다. 작업은 전용 브랜치에서 하거나, "
            f"main-참조 역할은 readonly 슬롯(T-0358)으로 옮기세요.",
            file=sys.stderr,
        )

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
                f"  실제(live): branch={live.get('branch')!r} head={live.get('head')!r}"
                f"  (head_relation={getattr(result, 'head_relation', None)!r})\n"
                f"  정당한 외부 변경이면 사용자 판단 후 명시 재동기하세요(submodule drift 재동기 동형).",
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
        # 작업공간(슬롯) 연결 = F2 alloc(T-0354) 스코프 — 이 티켓은 task 정체성 primitive 만.
        lines.append(
            "- 작업공간(슬롯): F2 alloc(T-0354)에서 연결 — 신규 task 는 슬롯 0개로 시작 가능(⑥)."
        )
        if pm_state_path:
            suffix = "" if pm_state_exists else " (아직 없음 — 첫 핸드오프가 생성·T-0356)"
            lines.append(f"- pm_state (이 task·서술): `{pm_state_path}`{suffix}")
        return "\n".join(lines)

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

    def _bind_and_identity(self, repo: str, slot: int) -> dict:
        """슬롯을 직접 bind 하고 lean identity + 상태점검 데이터를 반환한다 (multi-PM lean·T-0074).

        - 세션 = `f"{repo}_{slot}"`·슬롯 식별자 = `f"work/{repo}_{slot}"`.
        - `worktree_pool.bind_slot(slot_id, repo, session)` 호출 → Lease. **pool alloc 아님**
          (직접 바인딩·`NeedsCreate` 게이트 없음·`reclaim_stale` 안 거침·ADR-0013).
        - branch 는 `worktree_pool.current_branch(slot_id)` live 조회(git=진실·ADR-0013 amend
          T-0072). detached/조회불가/슬롯 폴더 부재는 None → surface 가 "(미지정)".
        - **상태점검**: `list_leases()` 에서 *이 세션 제외* 다른 활성(leased) 리스를 모아
          각 줄 `세션 · 슬롯 · 브랜치(live)` 로 반환한다(다른 활성 PM 현황 surface).
        """
        wp = self._resolve_worktree_pool()
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

        identity: lean(멀티-PM) 모드면 `session`+`repo` 키를 담은 dict → 세션 문자열(`<repo>_<N>`)
                  에서 슬롯 번호를 분리해 `--repo <repo> --slot <N>` 을 채운다. None 또는
                  `session`/`repo` 부재(솔로/legacy alloc)면 정체성 인자 없는 현행 형태로 분기.

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
        # 세션 문자열(`<repo>_<N>`)의 *마지막* `_` 로 슬롯 번호를 분리 — repo 이름 내부 언더스코어와
        # 무관하다(항상 마지막 세그먼트가 N·`_build_slot_identity_markdown` 의 동형 파싱 관용구).
        slot_num = session.rsplit("_", 1)[-1] if session else None
        # 정체성 인자 — lean 이면 ` --repo <repo> --slot <N>`, 솔로면 빈 문자열(현행 형태·ADR-0057 신 표기).
        sess = f" --repo {repo_name} --slot {slot_num}" if session and repo_name else ""

        def cmd(name: str, args: str, comment: str = "") -> str:
            """`python3 .project_manager/tools/<name> <args>` (+ ` # 주석`) 한 줄 렌더."""
            line = f"{_CARD_TOOL_INVOKE}/{name} {args}".rstrip()
            return f"{line}  # {comment}" if comment else line

        def skill(invocation: str, comment: str = "") -> str:
            """`/pm-…` 스킬 진입 줄(wave 운영 primary·ADR-0052).

            `python3` 로 시작하지 않으므로 카드↔CLI argparse 정합 가드·정체성 `--repo/--slot`
            검사(불변식 3)의 대상이 아니다 — backbone 은 아래 `engine()` 줄로 종속화한다.
            """
            return f"{invocation}  # {comment}" if comment else invocation

        def engine(name: str, args: str,
                   note: str = "스킬이 부르는 내부 엔진·직접 금지") -> str:
            """스킬에 종속된 backbone 줄 — 2-스페이스 들여쓰기 + '직접 금지' 주석(강등 표기).

            `python3 …` 로 시작해(들여쓰기는 strip 됨) 정체성 `--repo/--slot` 보간·카드↔CLI
            argparse 정합 가드의 대상으로 남는다(불변식 1·3 무손상). 스킬 줄(`/pm-…`)만 그
            가드 밖이다.
            """
            return "  " + cmd(name, args, f"↳ {note}")

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
        lines.append("")

        # 내 작업 보기 (read-only 조회·직접 — 래핑 스킬 없음·ADR-0047 자기 공간 우선).
        # user-first (ADR-0056): 두 렌즈의 스코프를 명확히 구분 — --mine=내 것 전 슬롯 /
        # --repo/--slot=내 것 ∩ 이 슬롯. "내 슬롯 작업"은 --repo/--slot 이 정확한 커맨드다
        # (타 사용자 무유출·ADR-0057 신 표기).
        lines.append("# 내 작업 보기 (read-only 조회·직접 — ADR-0047 자기 공간 우선)")
        lines.append(cmd(
            "board.py", "list --mine",
            "내 것 전 슬롯(내 open + 모든 슬롯의 내 claim)·user-wide 기본 조회",
        ))
        if session:
            lines.append(cmd(
                "board.py", f"list --repo {repo_name} --slot {slot_num}",
                "내 것 ∩ 이 슬롯(내 open + 이 슬롯 claim만)·이 슬롯 작업 조회·조회 전용",
            ))
        lines.append(cmd(
            "board.py", "list", "전체 보드(모든 세션·타 사용자 포함) — 타 PM 열람용·평시 불요",
        ))
        lines.append("")

        # 티켓 lifecycle 직접 (직접 — 래핑 스킬 없음·ADR-0052 예외). new/promote authoring +
        # complete 는 스킬 없는 fresh-adopter/concept(--allow-untested) 직접완료 경로(정상 wave
        # 종료=/pm-wave-finish→ticket_finish 가 complete 를 내부 수행·중복 실행 말 것).
        lines.append("# 티켓 lifecycle 직접 (래핑 스킬 없음·ADR-0052 예외)")
        lines.append(cmd(
            "board.py", 'new "<제목>" --prefix <PFX>', "draft 발행(본문은 board 밖에서 채움)",
        ))
        lines.append(cmd(
            "board.py", "promote T-NNNN", "draft → open(본문 채운 뒤·claim 선행조건)",
        ))
        lines.append(cmd(
            "board.py", "complete T-NNNN --tests-pass",
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
        lines.append(engine("board.py", "show T-NNNN"))
        lines.append(engine("board.py", "lint"))
        lines.append(engine("board.py", f"claim T-NNNN{sess}"))
        lines.append("  ⚠ claim 은 draft 티켓 거부 — 먼저 `promote T-NNNN`(본문 채운 뒤) 필요.")
        lines.append(skill("/pm-regression", "비차단 백그라운드 회귀 pre-warm + 완료 알림"))
        lines.append(engine("board.py", f"regression run{sess}"))
        lines.append(skill("/pm-wave-finish T-NNNN", "ticket 완료 부기 — 회귀+log+board+stage"))
        lines.append(engine(
            "ticket_finish.py", "<T-NNNN>",
            "스킬이 부르는 내부 엔진·직접 금지 — 내부서 board.py complete 수행",
        ))
        lines.append(skill("/pm-qa", "통합 검증 게이트 — 회귀+lint+git 단일 report"))
        lines.append(engine("board.py", f"regression run{sess}"))
        lines.append(engine("board.py", "lint"))
        lines.append(skill(
            "/pm-dev-delegate T-NNNN --role developer|code-reviewer",
            "orchestrator 위임 표준 프롬프트(dev / reviewer)",
        ))
        lines.append("  ↳ 엔진=Agent 툴(위임)·직접 CLI 아님 — skill-only.")
        # external_review = 래핑 스킬 없는 별도 codex 게이트(직접 OK 예외·reviewer 병행). 위임 직후 sibling.
        lines.append(cmd(
            "external_review.py", "--ticket T-NNNN --adr ADR-NNNN",
            "codex 외부 교차검증 게이트 — 직접(래핑 스킬 없음)·reviewer 병행",
        ))
        lines.append(skill("/pm-handoff", "세션 종료 7단계 자동화"))
        handoff_args = '--session-seq <N> --wave-summary "<요약>"'
        if session:
            handoff_args = f"--repo {repo_name} --slot {slot_num} {handoff_args}"
        lines.append(engine("pm_handoff.py", handoff_args))
        lines.append(skill("/pm-update", "엔진 갱신 — upstream freshness·manifest reconcile"))
        lines.append("  ↳ 엔진=pm-update.sh 파사드(freshness+reconcile)·직접 CLI 아님 — skill-only.")
        lines.append("")

        # 릴리즈 (직접 — 래핑 스킬 없음) — livegate record 는 release-marked pin(4대장 ③·인접 ⚠).
        # 정체성(`sess`)을 실어 실행가능 형태로 emit — multi-lease 홈에서 정체성 인자 없는 record 는
        # cwd 모호 fail-loud 이므로(T-0298), 이 세션 슬롯을 명시해 안내 명령이 dead-end 가 아니게 한다
        # (솔로는 `sess`="" → 무인자·현행 형태·leased <2 라 폴백 무변경).
        lines.append("# 릴리즈 (직접 — 래핑 스킬 없음)")
        lines.append(cmd("board.py", f"livegate record{sess}"))
        lines.append(
            "  ⚠ record 는 `pytest -m release` 수집 pin 강제 — "
            "release-marked 0 수집이면 fail(릴리즈 차단)."
        )
        lines.append("")

        # ID·카테고리 유지보수 (드묾·전제 주의) — prefix rename/merge·reid=홈 git clean(4대장 ②·
        # reid 추가)·migrate-identity=단일세션(4대장 ④). 각 커맨드 줄 바로 아래 1줄 ⚠.
        lines.append("# ID·카테고리 유지보수 (드묾·전제 주의)")
        lines.append(cmd("board.py", "prefix list", "카테고리 현황(read-only)"))
        lines.append(cmd("board.py", "prefix rename <OLD> <NEW>"))
        lines.append(
            "  ⚠ rename 은 홈 git working tree clean 필수 — "
            "wiki/log 참조 rewrite 라 미커밋 있으면 거부."
        )
        lines.append(cmd("board.py", "prefix merge <SRC> --into <DST>"))
        lines.append("  ⚠ merge 도 홈 git clean 전제(참조 rewrite·미커밋 있으면 거부).")
        lines.append(cmd(
            "board.py", "reid <OLD-ID> <NEW-ID>", "오발행 ID 교정(번호·prefix 무손실)",
        ))
        lines.append("  ⚠ reid 도 홈 git clean 전제(참조 rewrite 원자성·미커밋 있으면 거부).")
        lines.append(cmd(
            "board.py", "migrate-identity --dry-run", "ADR-0033 이전 데이터 일회성 backfill",
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
        lines.append(cmd("pm_log.py", "tail"))
        lines.append(cmd("domain.py", "affected --ticket <T-NNNN>"))
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
