"""부트스트랩 출력 파서와 세션·슬롯 진입의 순수 판정."""
from __future__ import annotations
from pm_engine.domain import pytest_report
from pm_engine.domain import ticket_rules
import re
from typing import Callable

_SYMREF_BRANCH_PREFIX = "refs/heads/"
_CARD_TOOL_INVOKE = "python3 .project_manager/tools"


def parse_board_counts(board_output: str) -> dict[str, int]:
    """board list 출력에서 status 별 카운트를 파싱한다.

    board list 출력 행 형식:
      "  [open   ] T-NNNN  title...  claimed_by  tags"
    status 필드는 7자 width 로 패딩된다.

    버킷 골격 = `ticket_rules.STATUS_DIRS` 파생이므로 board 가 아는 상태는 전부
    키로 존재하고(행이 없으면 0), board 가 모르는 토큰(예 `archived`)만 무시된다. 반환 키 순서는
    `STATUS_DIRS` 순서다.
    """
    counts: dict[str, int] = {status: 0 for status in ticket_rules.STATUS_DIRS}
    line_pattern = re.compile("^\\s+\\[(\\w+)\\s*\\]")
    for line in board_output.splitlines():
        match = line_pattern.match(line)
        if match:
            status = match.group(1).strip()
            if status in counts:
                counts[status] += 1
    return counts


# `T-service-a-001`·`T-P0-001`·`T-123-001`)다. 옛 `T-\d+` 만 잡으면 prefixed 가 전부 누락된다.
_TICKET_ID = "T-(?:[A-Za-z0-9][A-Za-z0-9_-]*-\\d+|\\d+)"


def parse_open_tickets(board_output: str) -> list[str]:
    """board list 출력에서 open status 의 ticket ID 목록을 반환한다.

    claim 가능한 open ticket 만 추출한다 (claimed/blocked/done 제외). prefixed(multi-repo
    `T-PAY-001`)·legacy(숫자형 legacy ID) ID 를 둘 다 파싱한다 (board.py grammar 정합).
    """
    tickets: list[str] = []
    line_pattern = re.compile(f"^\\s+\\[open\\s*\\]\\s+({_TICKET_ID})\\b")
    for line in board_output.splitlines():
        match = line_pattern.match(line)
        if match:
            tickets.append(match.group(1))
    return tickets


_LINT_GATE_HEADER = re.compile(
    "^\\s*⚠️\\s+(?P<total>\\d+)\\s+lint issue\\(s\\)\\s+\\((?P<blocking>\\d+)\\s+blocking 차단\\):\\s*$"
)
_LINT_ISSUE_LINE = re.compile("^(?:\\s*✗\\s*\\[[^\\]]+\\]|\\s+\\[[^\\]]+\\])")
_LINT_BLOCKING_LINE = re.compile("^\\s*✗\\s*\\[[^\\]]+\\]")
_LINT_REPRESENTATIVE_LIMIT = 3
_LINT_WARNING_BYTE_LIMIT = 2048
_LINT_WARNING_LINE_BYTE_LIMIT = 384
_LINT_ERROR_EXCERPT_LIMIT = 3


def _lint_issue_lines(lint_output: str) -> list[str]:
    """`board.py lint --gate` 상세에서 실제 issue 행만 반환한다."""
    return [line for line in lint_output.splitlines() if _LINT_ISSUE_LINE.match(line)]


def parse_lint_counts(lint_output: str) -> tuple[int, int] | None:
    """gate 헤더의 (총계, blocking)를 파싱한다.

    비정형 헤더라도 issue 행이 있으면 그 행으로 보수 폴백한다. clean marker도 명시적인
    ``(0, 0)`` 이다. 어느 신호도 없는 traceback/usage error/빈 출력은 ``None`` 으로
    반환해 "lint 이슈 0건"과 실행·파싱 실패를 구분한다.
    """
    for line in lint_output.splitlines():
        match = _LINT_GATE_HEADER.match(line)
        if match:
            try:
                return (int(match.group("total")), int(match.group("blocking")))
            except ValueError:
                break
    issues = _lint_issue_lines(lint_output)
    if issues:
        return (
            len(issues),
            sum((bool(_LINT_BLOCKING_LINE.match(line)) for line in issues)),
        )
    if any(
        (
            re.match("^\\s*✓\\s*no lint issues\\s*$", line)
            for line in lint_output.splitlines()
        )
    ):
        return (0, 0)
    return None


_LINT_HOOK_GENERATION_RESULT = "pre-push 훅 세대 차단 (board.py init 재실행)"


def _clip_utf8(text: str, byte_limit: int) -> str:
    """문자 경계를 깨지 않고 UTF-8 byte 상한 안으로 한 줄을 clip한다."""
    if byte_limit < 0:
        raise ValueError("byte_limit은 0 이상이어야 한다")
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    ellipsis = "…".encode("utf-8")
    if byte_limit < len(ellipsis):
        return encoded[:byte_limit].decode("utf-8", errors="ignore")
    prefix = encoded[: byte_limit - len(ellipsis)].decode("utf-8", errors="ignore")
    return prefix + "…"


def _bounded_warning_with_details(
    candidates: list[str], build_lines: Callable[[list[str]], list[str]]
) -> str:
    """행 clip 후 전체 byte 예산에 들어오는 대표 행만 순서대로 채운다."""
    clipped = [_clip_utf8(line, _LINT_WARNING_LINE_BYTE_LIMIT) for line in candidates]

    def bounded_lines(selected: list[str]) -> list[str]:
        return [
            _clip_utf8(line, _LINT_WARNING_LINE_BYTE_LIMIT)
            for line in build_lines(selected)
        ]

    selected: list[str] = []
    for line in clipped:
        trial = [*selected, line]
        if (
            len("\n".join(bounded_lines(trial)).encode("utf-8"))
            > _LINT_WARNING_BYTE_LIMIT
        ):
            break
        selected = trial
    warning = "\n".join(bounded_lines(selected))
    if len(warning.encode("utf-8")) > _LINT_WARNING_BYTE_LIMIT:
        raise AssertionError("lint warning 고정 문구가 byte 상한을 초과했다")
    return warning


def parse_pytest_counts(pytest_output: str) -> tuple[int, int] | None:
    """pytest -q 출력에서 (passed, total) 을 파싱한다.

    반환: (passed, total) — total = passed + failed.
    파싱 실패 시 None (요약행 부재 포함).
    """
    line = pytest_report._pytest_summary_line(pytest_output)
    if line is None:
        return None
    passed = pytest_report._pytest_outcome_count(line, "passed")
    if passed is None:
        return None
    failed = pytest_report._pytest_outcome_count(line, "failed") or 0
    return (passed, passed + failed)


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


_TASK_TAG_PREFIX = "task:"
# 슬롯 형식의 `_\d+` 종결·task 형식의 `task:` 접두 둘 다 서술형 괄호(`PM 4차 (아침 대화)`·`(회의 3)`)를
_LOG_HANDOFF_HEADER_RE = re.compile(
    "^(?P<line>## \\[\\d{4}-\\d{2}-\\d{2}\\]\\s+handoff\\s*\\|\\s*PM\\s+(?P<num>\\d+)차(?:\\s*\\((?P<session>[A-Za-z0-9][A-Za-z0-9_-]*_\\d+|task:[^)]+)\\))?.*)$",
    re.MULTILINE,
)


def _handoff_entry_owned(
    match: re.Match, bound_session: str | None, owns_untagged: bool
) -> bool:
    """handoff 헤더 match 가 bound_session 슬롯 소유인지 판정한다 (슬롯 필터 공용).

    태그 있는 entry → 태그 == bound_session 일 때만 소유(타 슬롯 entry 유입 차단). 태그 없는
    entry → slot-1(`owns_untagged`)만 소유. 차수 유도(`parse_last_handoff_session_num`)와
    본문 dump(`extract_slot_handoff_entry`)가 같은 규칙을 공유하게 한 단일 sink.
    """
    tag = match.group("session")
    tag = tag.strip() if tag else None
    if tag:
        return bound_session is not None and tag == bound_session
    return owns_untagged


def reconcile_session_num(
    state_num: int | str | None, log_next: int | None
) -> tuple[int | str | None, bool]:
    """pm_state-derived 차수와 log-derived *다음* 차수(N+1)를 교차검증한다.

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
            return (log_next, True)
        return (state_num, False)
    if isinstance(state_num, int):
        return (state_num, False)
    if isinstance(log_next, int):
        return (log_next, False)
    return (state_num, False)


_REMEDY_BRANCH_PREFIX = "task/"
_REMEDY_BRANCH_SUFFIX_LIMIT = 20


def _phase0_diverge_reason(result) -> str:
    """0단계 record-vs-live FAIL-LOUD 의 판정 근거 한 줄 (surface-only).

    `compare_slot_git` 의 `fail_loud`(branch 변경 또는 head diverged)를 사람이 읽을 수 있는
    사유로 푼다 — `head_relation`/`branch_match` 원값만으로는 PM 이 "왜 diverged 인지"를 코드
    판정/거동은 안 바꾸고 근거만 표면화한다.
      - branch_match False → 브랜치가 바뀜(세션 중 checkout·외부 개입).
      - branch 동일·head_relation diverged → 같은 브랜치인데 live HEAD 가 기록 커밋의 후손이
        아님(reset·force·rebase 등 되감기/divergent — 내 커밋 위 진행이 아님).
    """
    recorded = getattr(result, "recorded", None) or {}
    live = getattr(result, "live", None) or {}
    if not getattr(result, "branch_match", True):
        return f"기록 branch `{recorded.get('branch')}` ≠ live branch `{live.get('branch')}` — 브랜치가 바뀜(세션 중 checkout 또는 외부 개입)"
    return "같은 브랜치인데 live HEAD 가 기록 커밋의 후손이 아님 — reset·force·rebase 등 되감기/divergent(내 커밋 위 진행이 아님)"


def _phase0_incomplete_create(lease) -> bool:
    """그 슬롯이 불완전 생성(`state="creating"`·provisional/중단 마커)인가 — 0단계 차단 대상.

    creating 은 create_slot 의 provisional 마커(worktree add *전* 선기록·확정 시 leased·중단 시
    흔적). 이 상태 슬롯은 아무도 안전히 진입할 수 없다(alloc 은 creating 을 재부착하지 않는다). 그래서 **세션 동일 여부·readonly 무관** 차단한다(내 중단 흔적이든 타 세션 in-flight
    든 슬롯이 불완전한 건 동일). lease None/미상 state 는 False(fail-soft·열림 기록 검사
    와 분리 — 열린 기록 판정은 task 장부 축이 따로 본다)."""
    if lease is None:
        return False
    return getattr(lease, "state", None) == "creating"
