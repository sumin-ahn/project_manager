#!/usr/bin/env python3
"""PM 핸드오프 7단계 자동화 헬퍼 — PM 세션 종료 시 기계 측정·편집 부분을 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/pm_handoff.py \\
      --session-num <N차> \\
      --wave-summary "<wave 1~3 한 줄 요약>" \\
      [--dry-run] [--no-pytest]

동작 순서 (하나라도 실패하면 이후 단계 중단):
  1. 회귀 측정 — pytest tests/ -q. red 면 즉시 중단·핸드오프 불가.
  2. log/current.md handoff entry skeleton append — lean 3섹션(읽기범위·메타학습·다음intent)+회귀/incident(1줄 baseline).
  3. pm_state.md 세션 식별 표 sliding window 정리 — 신규 entry 추가 + 가장 오래된 entry 제거.
  4. pm_state.md 길이 검증 — wc -l 기준 700 라인 초과 시 warning.
  5. 인계 프롬프트 stdout 출력 — pm_playbook.md §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)"
     의 고정부 채워 stdout. <핵심 인계 사항> 절은 PM 손.
  6. git status dump — git status -s 출력 + 변경 파일 카운트.
  7. 잔여 PM 수동 작업 출력 — checklist.

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
import datetime
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
PM_PLAYBOOK_FILE = REPO / ".project_manager" / "wiki" / "pm_playbook.md"  # 정적 — 인계 프롬프트 템플릿 추출용
PM_STATE_FILE = REPO / ".project_manager" / "wiki" / "pm_state.md"       # 동적 — 세션 식별 sliding window 편집 대상
TICKETS_DIR = REPO / ".project_manager" / "wiki" / "tickets"             # board 현황 카운트용 (trigger wave-summary)
TOOLS_DIR = REPO / ".project_manager" / "tools"                          # worktree_pool 동적 로드 앵커 (multi-PM 모드)
# 회귀 cwd 자동해소(T-0124) — board.py·pm_bootstrap.py 와 *같은 위치*. _regression_cwd 가
# pm_bootstrap._auto_slot 에 명시 인자로 넘겨 단일 self-host 슬롯을 해소한다. worktree_pool 은
# import 하지 않는다(touches 격리·데이터 결합만) — pm_bootstrap 을 동적로드해 그 판정을 재사용.
AREAS_FILE = REPO / ".project_manager" / "areas.md"
LEASES_FILE = REPO / ".project_manager" / ".local" / "worktree-leases.json"


# ── worktree_pool import seam (multi-PM 모드·ADR-0013) ───────────────────────────
# multi-PM 인자(--slot)를 받았을 때만 lease 라이프사이클(release)에 진입한다. 솔로
# 무인자 경로는 이 모듈을 전혀 쓰지 않으므로 import 실패가 무해(fail-soft) — 단
# --done --slot 을 줬는데 worktree_pool 이 없으면 **명시 에러**(침묵 무력화 금지).
def _load_worktree_pool():
    """worktree_pool 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    pm_bootstrap._load_worktree_pool 과 동형 — REPO/tools 스크립트-위치 앵커.
    솔로(multi-PM 미사용·slot 미지정)에선 호출 안 되거나 None 이어도 무해.
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


# ── pm_bootstrap import seam (회귀 cwd 자동해소·T-0124) ───────────────────────────
# 회귀를 활성 worktree 슬롯에서 돌리려면 단일 self-host 슬롯 판정이 필요하다 —
# pm_bootstrap._auto_slot 이 그 로직(count-based 단일 self-host·T-0123)을 이미 보유하므로
# 복붙하지 않고 동적 로드해 재사용한다(DRY·ADR-0013 isolation). _load_worktree_pool·
# pm_bootstrap._load_board 와 동형 — `spec_from_file_location`(스크립트-위치 앵커)·fail-soft.
def _load_pm_bootstrap():
    """pm_bootstrap 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    _load_worktree_pool 과 동형 — REPO/tools 스크립트-위치 앵커. 회귀 cwd 해소(T-0124)에서
    `_auto_slot` 재사용용. 부재/실패는 None 이고 호출부가 `str(REPO)` 로 폴백하므로 무해.
    """
    import importlib.util

    bp_path = TOOLS_DIR / "pm_bootstrap.py"
    if not bp_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("pm_bootstrap", bp_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        return None


def _regression_cwd(
    worktree_slot: str | None = None,
    areas_file: Path = AREAS_FILE,
    leases_file: Path = LEASES_FILE,
) -> str:
    """회귀를 실행할 작업 디렉토리를 해소한다 (T-0124·분리된 PM 홈+worktree 모델).

    분리된 PM 홈(②·ADR-0027)엔 `tests/` 가 없으므로 회귀는 활성 repo 의 worktree cwd 에서
    돌아야 한다. 이 함수가 그 경로를 해소한다.

    해소 순서:
      - `worktree_slot`(multi-PM `--worktree-slot` 명시) 가 있으면 `REPO / worktree_slot`,
      - 없으면 bootstrap `_auto_slot` 으로 단일 self-host 슬롯을 자동해소(`work/<repo>_<N>`),
      - 그것도 없으면(솔로/모호/부재) **현 `REPO` 기본** (fail-soft 폴백·솔로 무변경).

    판정 로직은 pm_bootstrap `_auto_slot` 재사용(count-based 단일 self-host·T-0123 동형) —
    복붙하지 않고 동적 로드한다(DRY). areas/leases 는 명시 인자로 노출해 hermetic 테스트 가능.
    """
    if worktree_slot:
        return str(REPO / worktree_slot)
    bp = _load_pm_bootstrap()
    if bp is not None:
        try:
            auto = bp._auto_slot(areas_file, leases_file)
        except Exception:  # noqa: BLE001 — fail-soft: 판정 실패는 REPO 폴백.
            auto = None
        if auto:
            repo, n = auto
            return str(REPO / f"work/{repo}_{n}")
    return str(REPO)


def _default_python() -> str:
    """플랫폼-인지 venv 인터프리터 경로 (없으면 sys.executable 폴백).

    Windows 는 venv/Scripts/python.exe, POSIX 는 venv/bin/python. venv 가 없으면
    현재 인터프리터로 폴백한다. 이 머신은 시스템 python3 에 pytest 가 없고 venv 에만
    있으므로, venv 가 있으면 무조건 venv 를 우선해 회귀 측정 인터프리터를 보존한다.
    (이 도구는 board.py 를 import 하지 않으므로 헬퍼 중복 보유 — 도구 간 의존 없음.)
    """
    cand = REPO / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(cand) if cand.exists() else sys.executable

# ── ctx 정지-핸드오프 비대화 트리거 (T-0013) ─────────────────────────────────
# 어댑터 훅(opencode·claude)이 ctx 임계 도달 시 호출하는 빠른 경로의 기본값.
TRIGGER_DEFAULT_REASON = "ctx-stop"          # --reason 미지정 시 기본 사유.
TRIGGER_SESSION_PLACEHOLDER = "?"            # 세션 차수 추론 불가 시 안전한 placeholder.
_BOARD_STATUS_DIRS = ("open", "claimed", "blocked", "done")  # board.py STATUS_DIRS 동기.

# ── 상수 ─────────────────────────────────────────────────────────────────────

# pm_state.md 길이 경고 임계값 (핸드오프 절차 7단계 — 세션 정리 누락 신호)
PM_STATE_LINE_WARNING_THRESHOLD = 700

# log/current.md entry 누적 경고 임계값 — 초과 시 pm_log.py archive 권장 (차단 아님).
LOG_ARCHIVE_SUGGEST_THRESHOLD = 40

# log entry 시작 줄 ("## [YYYY-MM-DD] ...") — 누적 카운트용 (pm_log.split_entries 와 동일 형식).
_LOG_ENTRY_RE = re.compile(r"^## \[\d{4}-\d{2}-\d{2}\]", re.MULTILINE)

# 슬라이딩 윈도우 크기 — 최근 N 차 만 short inline 유지. 프로젝트별 조정 가능.
SLIDING_WINDOW_SIZE = 3

# ── 라이브-게이트 발동 출하 경로 (A tier·spike harness-test-two-level-gate §3.3) ─────
# 미push diff 가 이 글롭 중 하나라도 건드리면 출하 변경 → 라이브 게이트 발동. 채택자
# 산출물을 바꾸는 경로([[smoke-gate-by-output-change]])만 포함한다 — 엔진(.project_manager/
# tools)·출하 템플릿·어댑터(.claude/.opencode)·진입문서·manifest·파사드·요구사항·방법론 wiki.
# NON-SHIPPING(tests/·② wiki board/ADR/spike·status/pm_state/log)은 매칭 안 돼 자연 skip.
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
    ".project_manager/wiki/_template/**",
    ".project_manager/wiki/domain/**",
)

# ── log/current.md handoff entry skeleton ────────────────────────────────────────────

# "다음 intent" 세분(ADR-0008 재검토 트리거·T-0047): 한 줄 → 두 줄.
#   - 대화 thread-tail: 어댑터(claude ctx 훅)가 정지 직전 사용자 발화를 transcript 에서 추출해
#     자동 채운다. 미주입 시 아래 placeholder 유지(하위호환).
#   - pending user intent: PM 손 — 다음 우선순위 + 사용자 결정 대기.
THREAD_TAIL_PLACEHOLDER = (
    "<자동 — 정지 직전 사용자 발화. 어댑터 미주입 시 비움.>"
)
PENDING_INTENT_PLACEHOLDER = (
    "<PM 손 — 다음 우선순위 + 사용자 결정 대기. board open ticket 재열거 금지.>"
)


THREAD_TAIL_MAX_CHARS = 600  # 엔진 레벨 방어 cap (어댑터 추출 캡과 동일·CLI 직접 호출 대비).


def _flatten_thread_tail(thread_tail: str) -> str:
    """thread_tail 을 한 줄 슬롯에 안전하게 — 개행 평탄화·trim·cap.

    `--thread-tail` 은 공개 CLI 라 다중행 입력이 후속 섹션(`- 회귀/incident:` 등)을 위조하거나
    lean 줄단위 handoff 스키마를 깰 수 있다. 엔진이 *자기* 계약(줄단위 슬롯)을 직접 방어한다 —
    어댑터(ctx_guard)가 이미 평탄화해도 defense-in-depth (엔진 인터페이스는 공개라 신뢰 안 함).
    """
    flat = " / ".join(part.strip() for part in thread_tail.splitlines() if part.strip())
    flat = flat.strip()
    if len(flat) > THREAD_TAIL_MAX_CHARS:
        flat = flat[: THREAD_TAIL_MAX_CHARS - 1].rstrip() + "…"
    return flat


def _next_intent_lines(thread_tail: str | None) -> str:
    """"다음 intent" 두 줄(대화 thread-tail / pending user intent)을 빌드한다.

    thread_tail 이 주어지면(어댑터 자동 주입) 첫 줄 슬롯에 *평탄화·trim·cap 한* 텍스트를 넣고,
    None/빈/공백뿐이면 placeholder 를 유지한다(하위호환). 엔진은 transcript 를 보지 않고 받은
    string 을 *자기 줄단위 계약에 맞게 sanitize 해* 슬롯에 넣는다(harness-agnostic seam·CLI 방어).
    """
    tail = _flatten_thread_tail(thread_tail) if thread_tail else ""
    if not tail:
        tail = THREAD_TAIL_PLACEHOLDER
    return (
        f"- 대화 thread-tail: {tail}\n"
        f"- pending user intent: {PENDING_INTENT_PLACEHOLDER}"
    )


def _worktree_line(worktree_slot: str | None, branch: str | None) -> str:
    """handoff entry 의 worktree slot/branch 기록 줄을 빌드한다 (회전 연속성·ADR-0013).

    multi-PM 모드에서 worktree_slot 이 주어지면 슬롯/브랜치를 한 줄로 기록한다 — 다음
    bootstrap 이 회전 재부착(같은 슬롯 resume)할 때 연속성 단서가 된다. 솔로(미지정)면
    빈 문자열을 반환해 줄 자체를 생략한다(현행 lean 스키마 100% 보존·하위호환).
    """
    if not worktree_slot:
        return ""
    branch_part = branch if branch else "(미지정)"
    return f"- worktree: slot=`{worktree_slot}` · branch=`{branch_part}` (회전 재부착 단서·ADR-0013)\n"


def _normalize_session_num(session_num: int | str) -> str:
    """세션 차수를 bare 숫자 문자열로 정규화한다 — `19`·`'19'`·`'19차'`·`'19차차'` 모두 `'19'`.

    handoff entry 템플릿은 `PM {session_num}차` 로 '차' 를 *붙인다*. skill 문서가
    `--session-num <N차>` 로 안내해 온 탓에 입력에 이미 '차' 가 있으면 이중 부착('19차차')
    됐고(PM 9차에 "사소"로 기록 후 미수정·재발), sliding-window 정규식 `\\d+차` 매칭도 깨졌다.
    후행 '차'/공백을 멱등 제거해 어느 입력이든 안전하게 만든다 (T-0100)."""
    return str(session_num).strip().rstrip("차").strip()


HANDOFF_LOG_SKELETON_TEMPLATE = """\
## [{date}] handoff | PM {session_num}차 → 다음 PM 세션

- 읽기 범위: <PM 손 — 이 entry + 인용할 과거 entry/ADR. 라인수·전체Read 아님. board/git/log 는 /pm-bootstrap 라이브 — 적지 마라.>
- 메타 학습: <PM 손 — ticket 상태에서 도출 불가한 교훈만. 없으면 "없음".>
{next_intent}
{worktree_line}- 회귀/incident: <PM 손 — 회귀 "N passed / 상태" 1줄(green 도 — baseline) + 비-자명 incident. (회귀는 1줄 load-bearing 이라 항상 적음 — board/git/log 대량 재열거만 금지.)>
"""


def build_handoff_log_skeleton(
    session_num: int | str,
    date: str | None = None,
    thread_tail: str | None = None,
    worktree_slot: str | None = None,
    branch: str | None = None,
) -> str:
    """log/current.md 에 append 할 handoff entry skeleton 을 반환한다.

    thread_tail 주입 시 "다음 intent" 의 대화 thread-tail 슬롯을 자동 채운다.
    worktree_slot 주입 시(multi-PM 모드) slot/branch 기록 줄을 추가한다 — 회전 재부착
    연속성 단서(ADR-0013). 미지정(솔로)이면 줄 생략(현행 스키마 보존).
    """
    if date is None:
        date = datetime.date.today().isoformat()
    return HANDOFF_LOG_SKELETON_TEMPLATE.format(
        date=date,
        session_num=_normalize_session_num(session_num),
        next_intent=_next_intent_lines(thread_tail),
        worktree_line=_worktree_line(worktree_slot, branch),
    )


# ── 비대화 트리거 handoff entry skeleton (reason·ctx% 기록) ────────────────────

TRIGGER_HANDOFF_LOG_SKELETON_TEMPLATE = """\
## [{date}] handoff ({trigger_label}) | PM {session_num}차 → 다음 PM 세션

- 트리거: {trigger_desc} (reason={reason}{ctx_part}) — 어댑터 훅이 정지 직전 박제.
- 읽기 범위: <PM 손 — 이 entry + 인용할 과거 entry/ADR. 라인수·전체Read 아님. board/git/log 는 /pm-bootstrap 라이브 — 적지 마라.>
- 메타 학습: <PM 손 — ticket 상태에서 도출 불가한 교훈만. 없으면 "없음".>
{next_intent}
{worktree_line}- 회귀/incident: <PM 손 — 회귀 "N passed / 상태" 1줄(green 도 — baseline) + 비-자명 incident. (회귀는 1줄 load-bearing 이라 항상 적음 — board/git/log 대량 재열거만 금지.)>
"""


# 트리거 reason 별 marker (헤더 라벨·트리거 서술). 기본 = ctx-STOP 회전.
# precompact(네이티브 압축이 수동 handoff 보다 먼저 터질 때의 방어 폴백·ADR-0020)은
# ctx-임계 회전과 *시각적으로 구별*돼야 한다 — 다음 세션이 "수동 handoff 미완 가능"을
# 즉시 알도록 별도 라벨·⚠ 문구. reason 미등록 시 default(ctx-trigger).
_TRIGGER_MARKERS = {
    "precompact": ("precompact-flush", "⚠ 네이티브 압축 직전 durable flush — 수동 handoff 미완 가능"),
}
_TRIGGER_MARKER_DEFAULT = ("ctx-trigger", "ctx 임계 자동 핸드오프")


def build_trigger_handoff_log_skeleton(
    session_num: int | str,
    reason: str,
    ctx_pct: int | str | None,
    date: str | None = None,
    thread_tail: str | None = None,
    worktree_slot: str | None = None,
    branch: str | None = None,
) -> str:
    """비대화 트리거가 log/current.md 에 append 할 handoff entry skeleton.

    대화형 skeleton 과 달리 reason·ctx% 를 권위 최종 상태로 기록한다. **reason 별 marker**
    (헤더 라벨·트리거 서술)로 ctx-STOP 회전과 precompact 폴백을 구별한다(ADR-0020) —
    reason=precompact 면 "precompact-flush"·⚠ 문구, 그 외엔 default(ctx-trigger).
    thread_tail 주입 시 "다음 intent" 의 대화 thread-tail 슬롯을 자동 채운다
    (어댑터 훅이 정지 직전 사용자 발화를 transcript 에서 추출해 전달).
    worktree_slot 주입 시(multi-PM 모드) slot/branch 기록 줄을 추가한다 — ctx-STOP
    회전은 release 아님(리스 유지)이므로 다음 bootstrap 의 같은 슬롯 resume 단서.
    """
    if date is None:
        date = datetime.date.today().isoformat()
    ctx_part = f"·ctx={ctx_pct}%" if ctx_pct is not None else ""
    trigger_label, trigger_desc = _TRIGGER_MARKERS.get(reason, _TRIGGER_MARKER_DEFAULT)
    return TRIGGER_HANDOFF_LOG_SKELETON_TEMPLATE.format(
        date=date,
        session_num=_normalize_session_num(session_num),
        reason=reason,
        ctx_part=ctx_part,
        trigger_label=trigger_label,
        trigger_desc=trigger_desc,
        next_intent=_next_intent_lines(thread_tail),
        worktree_line=_worktree_line(worktree_slot, branch),
    )


# ── pm_state.md sliding window 편집 ──────────────────────────────────────────

# 세션 식별 절 앵커: pm_state.md 의 "## 세션 식별 (현재까지 사용된 이름)" 로 시작하는 섹션
_SESSION_SECTION_ANCHOR = "## 세션 식별 (현재까지 사용된 이름)"

# pm 세션 entry 줄: "  - **N차** (..." 형식
# 각 줄은 반드시 두 칸 들여쓰기 + "- **N차**" 로 시작한다.
_PM_SESSION_ANCHOR_RE = re.compile(
    r"^  - \*\*(\d+차)\*\*[^\n]*$",
    re.MULTILINE,
)

# "이전 차" 포인터 줄
_PREV_SESSIONS_POINTER = "  - 이전 차"


def _extract_session_section(pm_state_text: str) -> tuple[str, int, int] | None:
    """pm_state.md 에서 세션 식별 절 텍스트와 그 시작·끝 위치를 반환한다.

    반환: (section_text, start_offset, end_offset) 또는 None (앵커 불일치).
    end_offset 는 다음 ## 또는 ### 헤더 직전 위치 (혹은 파일 끝).
    """
    anchor_idx = pm_state_text.find(_SESSION_SECTION_ANCHOR)
    if anchor_idx == -1:
        return None

    # 앵커 이후에서 다음 헤더(## 또는 ###)를 찾는다
    after_anchor = pm_state_text[anchor_idx + len(_SESSION_SECTION_ANCHOR):]
    next_header = re.search(r"^###? ", after_anchor, re.MULTILINE)
    if next_header is None:
        end_offset = len(pm_state_text)
    else:
        end_offset = anchor_idx + len(_SESSION_SECTION_ANCHOR) + next_header.start()

    section_text = pm_state_text[anchor_idx:end_offset]
    return section_text, anchor_idx, end_offset


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
    - 가장 오래된 세션 entry 제거 (3 차 sliding window)
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
        raise ValueError(
            "앵커 불일치: 세션 식별 절에 기존 pm 세션 entry (- **N차** ...) 가 없다."
        )

    # 멱등성 검사 — 이미 해당 session_num entry 가 존재하면 no-op 으로 early-return.
    # 동일 session_num 재실행 시 entry 중복 추가 + 오래된 entry 의 이중 제거를 방지.
    target_num = int(str(session_num).replace("차", ""))
    existing_nums = [int(m.group(1).replace("차", "")) for m in existing_entries]
    if target_num in existing_nums:
        return pm_state_text

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


# ── 비대화 트리거 자동 채움 (ctx 정지-핸드오프 — T-0013) ─────────────────────

def infer_next_session_num(pm_state_text: str) -> int | str:
    """pm_state.md 세션 식별 절에서 다음 PM 세션 차수를 추론한다.

    가장 높은 기존 차수 + 1 을 반환. entry 가 없으면 안전한 placeholder.
    (대화형 경로의 사람-작성 --session-num 을 비대화에서 대신 채운다.)
    """
    result = _extract_session_section(pm_state_text)
    if result is None:
        return TRIGGER_SESSION_PLACEHOLDER
    section_text, _, _ = result
    entries = _find_pm_session_entries(section_text)
    if not entries:
        return TRIGGER_SESSION_PLACEHOLDER
    highest = max(int(m.group(1).replace("차", "")) for m in entries)
    return highest + 1


def board_status_counts(tickets_dir: Path = TICKETS_DIR) -> dict[str, int]:
    """board 의 status 디렉토리별 ticket 수를 센다 (stdlib glob — board.py 미import).

    반환: {"open": N, "claimed": N, "blocked": N, "done": N}.
    """
    counts: dict[str, int] = {}
    for status in _BOARD_STATUS_DIRS:
        status_dir = tickets_dir / status
        if status_dir.is_dir():
            counts[status] = len(list(status_dir.glob("*.md")))
        else:
            counts[status] = 0
    return counts


def build_trigger_wave_summary(
    reason: str,
    ctx_pct: int | str | None,
    tickets_dir: Path = TICKETS_DIR,
) -> str:
    """비대화 트리거용 wave-summary 를 자동 생성한다 (사람 작성 대체).

    형식: "ctx 임계 자동 핸드오프 (reason=<reason>·ctx=<N>%) — board <현황 1줄>"
    ctx_pct 가 None 이면 ctx 표기를 생략.
    """
    ctx_part = f"·ctx={ctx_pct}%" if ctx_pct is not None else ""
    counts = board_status_counts(tickets_dir)
    board_line = (
        f"board done {counts['done']} / open {counts['open']} / "
        f"claimed {counts['claimed']} / blocked {counts['blocked']}"
    )
    return f"ctx 임계 자동 핸드오프 (reason={reason}{ctx_part}) — {board_line}"


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


def build_handoff_prompt_output(
    pm_playbook_text: str,
    session_num: int | str,
    wave_summary: str,
    date_str: str,
) -> str:
    """인계 프롬프트 stdout 출력 문자열을 빌드한다.

    pm_playbook.md 의 고정부를 그대로 포함하고 <핵심 인계 사항> 절은 PM 손임을 명시.
    """
    template = extract_handoff_prompt_template(pm_playbook_text)
    if template is None:
        return (
            "[경고] pm_playbook.md 에서 인계 프롬프트 템플릿을 찾지 못했다. "
            f"앵커: '{_HANDOFF_PROMPT_SECTION_ANCHOR}'\n"
            "pm_playbook.md §'다음 PM 세션 부트스트랩 프롬프트 (템플릿)' 을 직접 복사하라."
        )

    header = (
        f"=== 인계 프롬프트 (PM {session_num}차 → 다음 PM 세션) ===\n"
        f"--- 아래를 복사해 다음 PM 세션에 붙여넣기 ---\n"
        f"(⚠️  '<핵심 인계 사항>' 절은 PM 손 — 직접 채워 넣을 것)\n\n"
    )
    footer = (
        f"\n--- 복사 끝 ---\n"
        f"[참고] 날짜: {date_str}, wave summary: {wave_summary}\n"
    )
    return header + "```\n" + template + "```" + footer


# ── pytest 출력 파서 ─────────────────────────────────────────────────────────

def is_pytest_green(output: str, returncode: int = 0) -> bool:
    """pytest -q 출력이 green (passed 존재, failed 없음) 이면 True."""
    if returncode != 0:
        return False
    if re.search(r"\d+ failed", output):
        return False
    if re.search(r"\d+ passed", output):
        return True
    return False


def parse_pytest_summary(output: str) -> str:
    """pytest -q 출력에서 요약 라인을 추출한다. 없으면 빈 문자열."""
    match = re.search(r"(\d+ passed.*)", output)
    if match:
        return match.group(1).strip()
    return output.strip()[-200:] if output.strip() else ""


# ── 라이브-게이트 출하 변경 발동 (A tier·spike §3.3) ──────────────────────────────

# baseline 기준 ref 후보 — push 대상 기준(미push diff). 첫 해소 가능한 것을 쓴다.
#   @{upstream}: 현 브랜치의 추적 upstream (가장 정확한 "미push" 경계).
#   origin/main: upstream 미설정 시 폴백 (공개 제품 ①의 push 대상).
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
      - `git -C <wt> diff --name-only HEAD`              → staged+unstaged tracked 변경
      - `git -C <wt> ls-files --others --exclude-standard` → untracked 신규파일(.gitignore 제외)
    둘 중 하나라도 비-0 종료면 작업트리 상태 불명 → None (호출부가 ambiguous 처리).
    runner DI seam 경유(hermetic). 예외는 호출부에서 흡수.
    """
    rc_diff, out_diff = runner(["-C", worktree, "diff", "--name-only", "HEAD"])
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
    """"지금 push 하면 올라갈 변경" ∩ SHIPPING_GLOBS 를 해소한다 (라이브-게이트 발동·spike §3.3).

    pm_handoff [7/7] 체크리스트는 핸드오프 *후* `git commit` 을 안내한다 — 정상 핸드오프
    시점엔 출하 변경이 대개 **working tree(staged/unstaged·미커밋·untracked)** 에 있다.
    따라서 커밋된-미push 만 보면(diff <baseline>..HEAD) 정상 핸드오프 시 게이트가 발동하지
    않는다(must-fix·T-0151). "지금 push 하면 올라갈 변경 전체"를 union 한다:
      - 커밋된 미push: `git -C <wt> diff --name-only <baseline>..HEAD` (baseline 해소된 경우만)
      - 작업트리 vs HEAD(staged+unstaged tracked): `git -C <wt> diff --name-only HEAD`
      - untracked 신규파일: `git -C <wt> ls-files --others --exclude-standard`
    이 union ∩ SHIPPING_GLOBS 가 `shipping_hits`.

    ambiguous 정련: uncommitted/untracked 출하 hit 이 있으면 **baseline 해소 여부와 무관하게
    발동**(그 변경은 확실히 올라간다). baseline 해소불가(또는 커밋된-미push diff 실패)
    **그리고** 출하 hit 이 전혀 없을 때만 has_unknown=True(커밋된-미push 출하분을 못 봐서
    불명). diff/ls-files 명령 실패·예외는 fail-soft(has_unknown=True) — silent skip 금지
    (false-skip = 미검증 출하 위험 > false-fire 낭비·spike §6).

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


def _run_live_gate(
    worktree: str,
    *,
    runner: Callable[[list[str], dict[str, str], str], tuple[int, str]] | None = None,
) -> tuple[int, str]:
    """`pytest -m live_gate -q` 를 worktree cwd·PM_ORCH_LIVE=1 로 돌린다 (A tier enforce).

    라이브 게이트 subset(live_gate marker) 만 선택해 실행한다 — relay 과금 smoke 는 제외
    (spike §3.1). PM_ORCH_LIVE=1 을 env 에 주입해 라이브 테스트의 skipif 를 깨운다. 단
    라이브 바이너리(opencode/claude) 부재면 per-test skipif(shutil.which)가 여전히 skip
    하므로 → 0개 selected/skip → green → fail-soft 통과(게이트 강제 안 함·CI green 불변·
    spike §3.3·ticket 결정).

    반환: (returncode, stdout+stderr). runner DI seam — hermetic 테스트는 결정론 stub.
    """
    if runner is not None:
        env = dict(os.environ)
        env["PM_ORCH_LIVE"] = "1"
        return runner([sys.executable, "-m", "pytest", "-m", "live_gate", "-q"], env, worktree)
    env = dict(os.environ)
    env["PM_ORCH_LIVE"] = "1"
    result = subprocess.run(
        [_default_python(), "-m", "pytest", "-m", "live_gate", "-q"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=worktree,
        env=env,
    )
    return result.returncode, result.stdout + result.stderr


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
        run_live_gate_fn: Callable[[str], tuple[int, str]] | None = None,
        log_file: Path = LOG_FILE,
        pm_playbook_file: Path = PM_PLAYBOOK_FILE,
        pm_state_file: Path = PM_STATE_FILE,
        venv_python: str | Path = _default_python(),
        worktree_pool=None,
    ) -> None:
        self._log_file = log_file
        self._pm_playbook_file = pm_playbook_file
        self._pm_state_file = pm_state_file
        self._venv_python = venv_python
        # worktree_pool seam — 테스트는 mock 모듈을 주입(hermetic). None 이면 --done
        # --slot 경로 진입 시에만 동적 로드(multi-PM 모드)·솔로 무인자 경로는 안 건드린다.
        self._worktree_pool = worktree_pool
        # 회귀 cwd 해소용 worktree 슬롯(T-0124) — run() 진입부에서 worktree_slot 인자로 세팅.
        # _default_run_pytest 가 _regression_cwd 에 넘긴다. 솔로/미세팅이면 None → REPO 폴백.
        self._worktree_slot: str | None = None

        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_git_fn = run_git_fn or self._default_run_git
        # 라이브-게이트 seam (A tier·spike §3.3) — worktree cwd 를 받아 (rc, out) 반환.
        # 테스트는 결정론 stub 주입(실 pytest/LLM 미실행·hermetic). None 이면 모듈
        # _run_live_gate(실 subprocess `pytest -m live_gate -q`·PM_ORCH_LIVE=1).
        self._run_live_gate_fn = run_live_gate_fn or _run_live_gate

    # ── multi-PM 모드: 작업완료 release (ADR-0013) ────────────────────────────────

    def _resolve_worktree_pool(self):
        """worktree_pool 모듈을 해소한다 — 주입분 우선·없으면 동적 로드 (multi-PM 모드 전용).

        --done --slot 경로에서만 호출된다. 둘 다 None 이면 **명시 에러**(SystemExit) —
        multi-PM 인자를 줬는데 worktree_pool 이 없으면 침묵 무력화 금지(ADR-0013).
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
        """작업완료 시 worktree 슬롯을 release 한다 (--done·ADR-0013).

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
        # 브랜치는 슬롯 worktree 의 git HEAD 에서 live 조회(ADR-0013 amend T-0072 —
        # Lease.branch 권위 제거·git=진실). detached/조회불가는 "(detached/조회불가)".
        live_branch = wp.current_branch(slot) or "(detached/조회불가)"
        print(f"  ✓ worktree 슬롯 release: {slot} → idle (작업완료 반납·ADR-0013·branch={live_branch})")
        return 0

    # ── 기본 subprocess 구현 (실제 실행) ──────────────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """pytest tests/ -q 를 실행해 (returncode, stdout+stderr) 반환.

        cwd 는 _regression_cwd 가 해소한다(T-0124) — 분리된 PM 홈(②)엔 tests/ 가 없으므로
        활성 worktree 슬롯에서 돌린다. 솔로/미해소면 REPO 폴백(현행 보존).
        """
        result = subprocess.run(
            [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_regression_cwd(self._worktree_slot),
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

    # ── 라이브-게이트 step (A tier·출하-변경 발동·spike §3.3) ────────────────────

    def _live_gate_step(self, worktree: str, live_gate_override: bool | None) -> int:
        """[1/7] 기계회귀 green 직후 라이브-게이트 step (출하-변경 발동·3-way).

        발동 판단 = 미push diff ∩ SHIPPING_GLOBS([[smoke-gate-by-output-change]]) —
        세션 종류가 아니라 *올리려는 코드*. override 가 주어지면 분류를 건너뛴다
        (--live-gate=강제발동·--no-live-gate=강제skip·사후 사유 log 는 PM 손).

        3-way (override None 일 때):
          - shipping_hits 비어있지 않음 → **발동**: `pytest -m live_gate` red → return 1.
          - shipping/unknown 모두 없음(push 없음·명확한 비-출하) → **skip**(사유 출력).
          - has_unknown(baseline 해소불가·분류불명) → **surface**(불명 신호 나열·기본
            비실행·PM 이 --live-gate/--no-live-gate 로 결정).

        반환: 0=통과/skip/surface(계속), 1=라이브 게이트 red(핸드오프 중단).
        run_trigger(ctx-STOP)는 이 step 을 절대 호출하지 않는다(자동정지·LLM 못 띄움).
        """
        if live_gate_override is False:
            print("  [--no-live-gate] 라이브 게이트 강제 skip (사유 log 는 PM 손).")
            return 0
        if live_gate_override is True:
            print("  [--live-gate] 라이브 게이트 강제 발동...")
            return self._fire_live_gate(worktree)

        shipping_hits, has_unknown = _shipping_paths_in_pending_push(
            worktree, git_runner=self._run_git_fn
        )
        if shipping_hits:
            print(
                f"  출하 변경 감지 ({len(shipping_hits)}개 경로) — 라이브 게이트 발동:"
            )
            for path in shipping_hits[:10]:
                print(f"    · {path}")
            return self._fire_live_gate(worktree)
        if has_unknown:
            # ambiguous → surface (silent skip/fire 금지·대화형 핸드오프라 가능).
            print(
                "  ⚠ 미push diff 분류 불명 (baseline ref 해소불가/분류 불가) — "
                "출하 변경 여부를 자동 판단할 수 없다.",
                file=sys.stdout,
            )
            print(
                "    → 라이브 게이트를 기본 비실행한다. push 코드가 채택자 산출물을 "
                "바꾸는지 PM 이 판단해 `--live-gate`(강제발동) 또는 `--no-live-gate`"
                "(강제skip·사유 log)로 명시하라.",
                file=sys.stdout,
            )
            return 0
        print("  출하 변경 없음 (미push diff 가 비-출하·또는 push 없음) — 라이브 게이트 skip.")
        return 0

    def _fire_live_gate(self, worktree: str) -> int:
        """라이브 게이트 실행 + red→중단 (기계회귀 red 동형). 반환 0=green/skip·1=red.

        fail-soft 허용을 **명시적으로 좁힌다** — pytest 종료코드 `rc in (0, 5)` 만 통과:
          - 0 = all passed(또는 skipped-only — skip 은 rc 0 이라 라이브 미가용 통과 보존).
          - 5 = no tests collected = 라이브 미가용(바이너리 부재·PM_ORCH_LIVE 미주입) fail-soft.
        그 외 모든 비-0(1=failed·2=interrupted·3=internal·4=usage·collection/import error 등)은
        **red → 핸드오프 중단**(return 1). 이전 `re.search("N failed")` 판정은 "failed" 요약이
        없는 collection/import/internal error(rc 2/3/4)를 silently green 처리했다(must-fix·T-0151).
        """
        rc, out = self._run_live_gate_fn(worktree)
        print(out.rstrip())
        if rc not in (0, 5):
            print(
                "\n[중단] 라이브 게이트 red — 핸드오프 불가. log/current.md·pm_state.md "
                f"어떤 것도 건드리지 않는다. (pytest rc={rc} ≠ 0/5 — 라이브 테스트 실패·"
                "collection/import/내부 에러 = 출하 변경이 실 LLM 운영성을 깨거나 게이트가 "
                "정상 실행되지 못했다.)",
                file=sys.stderr,
            )
            return 1
        print("  ✓ 라이브 게이트 통과 (green·또는 라이브 미가용 fail-soft skip·rc∈{0,5}).")
        return 0

    # ── 메인 흐름 ─────────────────────────────────────────────────────────────

    def run(
        self,
        session_num: int | str,
        wave_summary: str,
        dry_run: bool,
        skip_pytest: bool,
        worktree_slot: str | None = None,
        branch: str | None = None,
        done: bool = False,
        live_gate_override: bool | None = None,
    ) -> int:
        """PM 핸드오프 7단계 자동화 전체 흐름을 실행한다.

        worktree_slot/branch: multi-PM 모드(ADR-0013) — handoff entry 에 slot/branch 를
            기록해 회전 재부착 연속성 단서를 남긴다. 미지정(솔로)이면 현행 lean 스키마 보존.
        done: 작업완료(--done) — worktree 슬롯을 release(idle 반납). worktree_slot
            필요. 미지정이면 release 안 함(세션종료/회전 ≠ release·ADR-0013).
        live_gate_override: A tier 라이브-게이트 강제 override (--live-gate=True 강제발동·
            --no-live-gate=False 강제skip). None 이면 미push diff ∩ 출하경로로 3-way 자동
            판단(spike §3.3). 라이브 게이트 red → 핸드오프 중단(기계회귀 red 동형).

        반환: 0=성공, 1=실패 (중단).
        """
        date_str = datetime.date.today().isoformat()
        # 회귀 cwd 해소(T-0124)용 — _default_run_pytest 가 _regression_cwd 에 넘긴다.
        # 명시 슬롯이 있으면 그 worktree, 없으면 _regression_cwd 가 단일 self-host 자동해소.
        self._worktree_slot = worktree_slot
        print(
            f"[pm_handoff] PM {session_num}차 핸드오프 시작 "
            f"(dry_run={dry_run}, skip_pytest={skip_pytest}, "
            f"worktree_slot={worktree_slot}, done={done})"
        )

        # ── 1. 회귀 측정 ───────────────────────────────────────────────────────
        print("\n[1/7] 회귀 측정...")
        if skip_pytest:
            print("  [--no-pytest] 회귀 측정 skip.")
            pytest_summary = "(skip)"
        else:
            if dry_run:
                print("  [dry-run] pytest tests/ -q 실행 중 (파일 편집만 생략)...")
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

        # ── 1b. 라이브-게이트 step (A tier·출하-변경 발동·spike §3.3) ───────────
        # 기계회귀 green 직후·push 직전 1회. 미push diff 가 출하경로를 건드릴 때만
        # `pytest -m live_gate` 발동(3-way: 발동/skip/ambiguous→surface). red → 중단
        # (기계회귀 red 동형·log/pm_state 미접촉). dry_run 은 라이브 LLM 비용/시간을
        # 들이지 않도록 step 자체를 skip(미리보기 경로). worktree 는 회귀와 같은 cwd.
        print("\n[1b/7] 라이브-게이트 (출하-변경 발동)...")
        if dry_run:
            print("  [dry-run] 라이브 게이트 발동 판단·실행 skip (미리보기).")
        else:
            worktree = _regression_cwd(self._worktree_slot)
            gate_rc = self._live_gate_step(worktree, live_gate_override)
            if gate_rc != 0:
                return gate_rc

        # ── 2. log/current.md handoff entry skeleton append ────────────────────────────
        print("\n[2/7] log/current.md handoff entry skeleton append...")
        skeleton = build_handoff_log_skeleton(
            session_num=_normalize_session_num(session_num),
            date=date_str,
            worktree_slot=worktree_slot,
            branch=branch,
        )

        if dry_run:
            print("  [dry-run] log/current.md 에 append 할 skeleton:")
            print("  " + skeleton.replace("\n", "\n  "))
        else:
            log_text = self._log_file.read_text(encoding="utf-8") if self._log_file.exists() else ""
            self._log_file.write_text(log_text + "\n" + skeleton, encoding="utf-8")
            print(f"  ✓ log/current.md handoff entry skeleton append (PM {session_num}차)")

        # log/current.md entry 누적 점검 — 임계 초과 시 archive 권장 (차단 아님).
        cur_log_text = self._log_file.read_text(encoding="utf-8") if self._log_file.exists() else ""
        entry_count = len(_LOG_ENTRY_RE.findall(cur_log_text))
        if entry_count > LOG_ARCHIVE_SUGGEST_THRESHOLD:
            print(
                f"  ⚠ log/current.md entry {entry_count}개 > {LOG_ARCHIVE_SUGGEST_THRESHOLD} — "
                f"`pm_log.py archive --before <날짜>` 로 오래된 entry 봉인 권장 "
                f"(부트스트랩 읽기 비용 ↓).",
                file=sys.stdout,
            )

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
                new_state_text = update_session_window(
                    pm_state_text=state_text,
                    session_num=_normalize_session_num(session_num),
                    date_str=date_str,
                    wave_summary=wave_summary,
                )
            except ValueError as exc:
                print(f"\n[중단] {exc}", file=sys.stderr)
                return 1

            if dry_run:
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
                print(f"  ✓ pm_state.md 세션 식별 sliding window 정리 완료 (PM {session_num}차 추가·최고령 entry 제거)")

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

        # ── 7. 잔여 PM 수동 작업 출력 ──────────────────────────────────────────
        print("\n[7/7] PM 이 손으로 할 잔여 작업:")
        print("  [ ] log/current.md handoff entry 본문 채우기 — lean 3섹션(읽기범위·메타학습·다음intent)+회귀/incident(회귀 1줄 baseline). board/git/log 대량 재열거 금지(/pm-bootstrap 라이브).")
        print("  [ ] domain capture 검토 — `domain.py capture --tickets <이 세션 done>` 출력 보고 ⚠/gap 페이지 갱신/신설(채록·ADR-0018 §7b·surface-only).")
        print("  [ ] pm_state.md '진행 중인 의사결정' 표 갱신")
        print("  [ ] pm_state.md '남은 작업 전체 그림' 갱신")
        print("  [ ] 인계 프롬프트의 '<핵심 인계 사항>' 채우기 (위 [5/7] 출력 참조)")
        print("  [ ] git commit (Co-Authored-By: Claude 트레일러 포함)")

        # ── multi-PM 모드: --done 작업완료 슬롯 release (ADR-0013) ─────────────────
        # 세션종료/회전 ≠ release — --done 명시 시에만 슬롯을 idle 반납한다.
        if done:
            if not worktree_slot:
                print(
                    "\n[중단] --done 은 --worktree-slot 이 필요하다 (어느 슬롯을 반납할지).",
                    file=sys.stderr,
                )
                return 1
            print("\n[multi-PM] --done 작업완료 — worktree 슬롯 release...")
            if dry_run:
                print(f"  [dry-run] worktree 슬롯 release 예고: {worktree_slot} (실행 생략).")
            else:
                rc = self._release_slot(worktree_slot)
                if rc != 0:
                    return rc

        if dry_run:
            print("\n[dry-run] 완료 — 실제 파일 편집은 실행하지 않았다.")
        else:
            print(f"\n[완료] PM {session_num}차 핸드오프 자동화 완료.")

        return 0

    # ── 비대화 트리거 빠른 경로 (ctx 정지-핸드오프 — T-0013) ──────────────────

    def _build_trigger_handoff_prompt_block(
        self,
        session_num: int | str,
        wave_summary: str,
        date_str: str,
    ) -> str:
        """trigger handoff entry 끝에 박제할 인계 프롬프트 블록을 빌드한다 (T-0134·D16).

        대화형 run() 의 [5/7] 와 동일 build_handoff_prompt_output 을 재사용한다 — 자동
        경로는 모델이 정지되어 stdout(휘발)으로 프롬프트를 전달할 수 없으므로, 그 출력을
        durable 채널(log entry)에 박제해 다음 세션이 부트스트랩 때 읽게 한다.

        pm_playbook.md 부재(board.py init 미실행 clone)는 치명 아님 — fail-soft 로
        한 줄 안내만 남기고 trigger handoff 는 계속한다(skeleton 자체는 이미 append).
        반환 문자열은 skeleton 뒤에 이어 붙일 수 있게 선행 개행을 포함한다.
        """
        if not self._pm_playbook_file.exists():
            return (
                "\n[인계 프롬프트] ⚠ pm_playbook.md 없음 — 인계 프롬프트 템플릿 추출 skip. "
                "새 세션에서 pm_playbook.md §부트스트랩 프롬프트를 직접 복사하라.\n"
            )
        playbook_text = self._pm_playbook_file.read_text(encoding="utf-8")
        prompt_output = build_handoff_prompt_output(
            pm_playbook_text=playbook_text,
            session_num=_normalize_session_num(session_num),
            wave_summary=wave_summary,
            date_str=date_str,
        )
        return "\n" + prompt_output + "\n"

    def run_trigger(
        self,
        reason: str = TRIGGER_DEFAULT_REASON,
        ctx_pct: int | str | None = None,
        dry_run: bool = False,
        thread_tail: str | None = None,
        worktree_slot: str | None = None,
        branch: str | None = None,
    ) -> int:
        """ctx 임계 도달 시 어댑터 훅이 호출하는 비대화 트리거 경로.

        사람 입력(session-num·wave-summary) 없이 자동 채워 권위 handoff 를 박제한다:
          1. session-num 추론 (pm_state 세션 window 다음 차수).
          2. wave-summary 자동 생성 (reason·ctx%·board 현황).
          3. 회귀/git status 측정 스킵 (--no-pytest 동등 — 훅 컨텍스트라 빠른 경로).
          4. log/current.md 에 trigger handoff entry skeleton append (reason·ctx% 기록·
             thread_tail 주입 시 "다음 intent" 대화 thread-tail 슬롯 자동 채움) +
             다음 세션용 인계 프롬프트를 같은 entry 끝에 박제 (D16·durable 채널).
          5. pm_state sliding window 정리.
          6. stdout 에 "정지·새 세션 부트스트랩" 안내 + rc 0.

        thread_tail 은 어댑터(훅)가 transcript 에서 추출한 정지 직전 사용자 발화다 —
        엔진은 transcript 포맷을 보지 않고 받은 string 을 슬롯에 넣기만 한다. None 이면
        placeholder 유지(하위호환).

        worktree_slot/branch (multi-PM 모드·ADR-0013): handoff entry 에 slot/branch 를 기록해
        회전 재부착 단서를 남긴다. **ctx-STOP 회전은 release 가 아니다** — 리스는 유지하고
        다음 bootstrap 이 같은 슬롯을 resume 한다(트리거 경로는 release 를 절대 호출 안 함).

        실제 작업 정지·세션 종료는 어댑터(훅) 책임 — 엔진은 박제+안내까지.
        반환: 0=성공, 1=실패 (앵커 불일치 등).
        """
        date_str = datetime.date.today().isoformat()

        # ── 1. session-num 자동 추론 ───────────────────────────────────────────
        # pm_state.md 부재(board.py init 미실행 clone)는 치명 아님 — fail-soft.
        # 빈 문자열 폴백 → infer_next_session_num("")="?" placeholder → 5단계
        # isinstance(int) else 분기가 sliding window 자동 skip. log skeleton append 는 진행.
        if not self._pm_state_file.exists():
            print(
                "  ⚠ pm_state.md 없음 — session-num 추론·sliding window skip. "
                "trigger handoff 계속.",
                file=sys.stderr,
            )
        state_text = self._pm_state_file.read_text(encoding="utf-8") if self._pm_state_file.exists() else ""
        session_num = infer_next_session_num(state_text)

        # ── 2. wave-summary 자동 생성 ──────────────────────────────────────────
        wave_summary = build_trigger_wave_summary(reason=reason, ctx_pct=ctx_pct)

        print(
            f"[pm_handoff --trigger] ctx 임계 자동 핸드오프 "
            f"(reason={reason}, ctx_pct={ctx_pct}, session→{session_num}차, dry_run={dry_run})"
        )

        # ── 3. 회귀 측정 스킵 (빠른 경로) ──────────────────────────────────────
        print("  [trigger] 회귀 측정 skip (훅 컨텍스트 — --no-pytest 동등).")

        # ── 4. log/current.md trigger handoff entry skeleton append ────────────
        # skeleton 뒤에 다음 세션용 인계 프롬프트를 *박제*한다 — 자동 경로는 모델이
        # 정지되므로 stdout(휘발)이 아닌 durable 채널(log)이 권위적이다(decision D16).
        # 대화형 run() 의 [5/7] stdout 와 동일 build_handoff_prompt_output 을 재사용한다.
        skeleton = build_trigger_handoff_log_skeleton(
            session_num=_normalize_session_num(session_num),
            reason=reason,
            ctx_pct=ctx_pct,
            date=date_str,
            thread_tail=thread_tail,
            worktree_slot=worktree_slot,
            branch=branch,
        )
        prompt_block = self._build_trigger_handoff_prompt_block(
            session_num=session_num, wave_summary=wave_summary, date_str=date_str,
        )
        entry = skeleton + prompt_block
        if dry_run:
            print("  [dry-run] log/current.md 에 append 할 trigger entry (skeleton + 인계 프롬프트):")
            print("  " + entry.replace("\n", "\n  "))
        else:
            log_text = self._log_file.read_text(encoding="utf-8") if self._log_file.exists() else ""
            self._log_file.write_text(log_text + "\n" + entry, encoding="utf-8")
            print(
                f"  ✓ log/current.md trigger handoff entry skeleton + 인계 프롬프트 박제 "
                f"(PM {session_num}차)"
            )

        # ── 5. pm_state sliding window 정리 ────────────────────────────────────
        # session_num 이 placeholder("?") 면 sliding window 편집은 스킵 (정수 차수만 안전 편집).
        if isinstance(session_num, int):
            try:
                new_state_text = update_session_window(
                    pm_state_text=state_text,
                    session_num=_normalize_session_num(session_num),
                    date_str=date_str,
                    wave_summary=wave_summary,
                )
            except ValueError as exc:
                print(f"\n[중단] pm_state sliding window: {exc}", file=sys.stderr)
                return 1
            if dry_run:
                print("  [dry-run] pm_state.md 세션 식별 절 갱신 예고 (생략).")
            else:
                self._pm_state_file.write_text(new_state_text, encoding="utf-8")
                print(f"  ✓ pm_state.md 세션 식별 sliding window 정리 완료 (PM {session_num}차 추가)")
        else:
            print(
                f"  ⚠ session-num 추론 불가 (placeholder {session_num!r}) — "
                f"pm_state sliding window 정리 스킵. PM 이 새 세션에서 직접 채울 것.",
                file=sys.stdout,
            )

        # ── 6. 정지·부트스트랩 안내 stdout ─────────────────────────────────────
        # 안내 stdout 은 휘발되므로 인계 프롬프트 본문을 *여기* 다시 찍지 않는다 —
        # log handoff entry 에 박제된 위치를 가리킨다(durable 권위 채널·D16).
        print(
            "\n=== ctx 정지·핸드오프 박제 완료 ===\n"
            f"  reason={reason} · ctx={ctx_pct}% · PM {session_num}차 handoff entry 기록.\n"
            "  → 다음 세션용 인계 프롬프트는 log/current.md 최신 handoff entry 끝에 박제됨.\n"
            "  → 이 세션을 정지하고 새 PM 세션을 부트스트랩하라:\n"
            "     1. log/current.md 최신 handoff entry 의 <PM 손> 절을 채운다.\n"
            "     2. 같은 entry 의 박제된 인계 프롬프트를 새 세션에 붙여넣는다.\n"
            "     3. CLAUDE.md → pm_state.md → board.py list 순으로 새 세션 부트스트랩.\n"
            "  (실제 세션 종료는 어댑터 훅 책임 — 엔진은 권위 handoff 박제까지.)"
        )
        if dry_run:
            print("\n[dry-run] 완료 — 실제 파일 편집은 실행하지 않았다.")
        return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_handoff.py",
        description="PM 핸드오프 7단계 자동화 헬퍼 (+ ctx 정지 비대화 트리거).",
    )
    # 대화형 경로 — --trigger 없을 때 필수 (검증은 main 에서, 트리거와 상호배제 위해
    # argparse required 대신 수동 검증).
    parser.add_argument(
        "--session-num",
        metavar="N",
        help="떠나는 PM 세션 차수 (예: 28). 대화형 경로 필수 (--trigger 면 자동 추론).",
    )
    parser.add_argument(
        "--wave-summary",
        metavar="요약",
        help="떠나는 PM 세션의 wave 종합 1~2 줄 요약 (사람 작성). 대화형 필수 (--trigger 면 자동 생성).",
    )
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="비대화 트리거 모드 — 사람 입력 없이 ctx 임계 자동 핸드오프 박제 (어댑터 훅용).",
    )
    parser.add_argument(
        "--reason",
        metavar="사유",
        default=TRIGGER_DEFAULT_REASON,
        help=f"트리거 사유 (--trigger 전용·기본 {TRIGGER_DEFAULT_REASON!r}).",
    )
    parser.add_argument(
        "--ctx-pct",
        metavar="N",
        type=int,
        default=None,
        help="잔여 컨텍스트 %% (--trigger 전용·handoff entry 에 기록).",
    )
    parser.add_argument(
        "--thread-tail",
        metavar="텍스트",
        default=None,
        help=(
            "정지 직전 사용자 발화 (--trigger 전용·옵션). 어댑터 훅이 transcript 에서 "
            "추출해 전달하면 handoff entry '다음 intent' 의 대화 thread-tail 슬롯에 "
            "삽입한다. 미전달 시 placeholder 유지(하위호환)."
        ),
    )
    # ── multi-PM 모드 (ADR-0013) — 솔로 미지정이면 미사용·현행 보존 ──
    parser.add_argument(
        "--worktree-slot",
        metavar="슬롯",
        default=None,
        help=(
            "multi-PM 모드 — 이 세션의 worktree 슬롯 (`work/<repo>_<N>`). handoff entry 에 "
            "slot/branch 를 기록(회전 재부착 단서). --done 과 함께면 작업완료 release."
        ),
    )
    parser.add_argument(
        "--branch",
        metavar="브랜치",
        default=None,
        help="multi-PM 모드 — 이 세션의 작업스트림 브랜치 (--worktree-slot 과 함께·handoff entry 기록).",
    )
    parser.add_argument(
        "--done",
        action="store_true",
        help=(
            "multi-PM 모드 — 작업완료 시 worktree 슬롯을 release(idle 반납·ADR-0013). "
            "--worktree-slot 필요. 미지정이면 세션종료/회전 ≠ release(리스 유지)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="파일 편집 없이 변경 미리보기.",
    )
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help="회귀 측정 skip (기본 측정·대화형 경로).",
    )
    # ── 라이브-게이트 override (A tier·spike §3.3) — 상호배제. 미지정이면 출하-변경 자동 판단. ──
    live_gate_group = parser.add_mutually_exclusive_group()
    live_gate_group.add_argument(
        "--live-gate",
        dest="live_gate",
        action="store_true",
        default=None,
        help="라이브 게이트 강제 발동 (출하-변경 자동 판단 무시·`pytest -m live_gate` 실행).",
    )
    live_gate_group.add_argument(
        "--no-live-gate",
        dest="live_gate",
        action="store_false",
        help="라이브 게이트 강제 skip (출하-변경 자동 판단 무시·사후 사유 log 는 PM 손).",
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
    handoff = PmHandoff()

    # --branch 는 --worktree-slot 동반 필요 — 슬롯 없는 브랜치는 회전 재부착 단서로
    # 불완전(어느 슬롯에 재부착할지 모름)하므로 조용히 무시하지 않고 거부한다(오용 축소·ADR-0013).
    if args.branch and not args.worktree_slot:
        parser.error("--branch 는 --worktree-slot 과 함께 써야 한다 (multi-PM 모드 회전 재부착 단서·ADR-0013).")

    # --done 은 대화형(--trigger 아님) 경로 전용 — ctx-STOP 회전은 release 아님(ADR-0013).
    if args.done and args.trigger:
        parser.error("--done 은 --trigger 와 함께 쓸 수 없다 (ctx-STOP 회전은 release 아님·ADR-0013).")

    if args.trigger:
        # 비대화 트리거 — session-num·wave-summary 무시(자동 채움). 슬롯/브랜치는
        # handoff entry 기록만 (release 안 함·리스 유지·다음 bootstrap 이 resume).
        return handoff.run_trigger(
            reason=args.reason,
            ctx_pct=args.ctx_pct,
            dry_run=args.dry_run,
            thread_tail=args.thread_tail,
            worktree_slot=args.worktree_slot,
            branch=args.branch,
        )

    # 대화형 경로 — session-num·wave-summary 수동 필수.
    missing = [
        flag
        for flag, val in (("--session-num", args.session_num), ("--wave-summary", args.wave_summary))
        if not val
    ]
    if missing:
        parser.error(f"대화형 경로엔 {', '.join(missing)} 가 필수다 (또는 --trigger 비대화 모드 사용).")

    return handoff.run(
        session_num=args.session_num,
        wave_summary=args.wave_summary,
        dry_run=args.dry_run,
        skip_pytest=args.no_pytest,
        worktree_slot=args.worktree_slot,
        branch=args.branch,
        done=args.done,
        live_gate_override=args.live_gate,
    )


if __name__ == "__main__":
    sys.exit(main())
