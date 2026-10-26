"""세션 식별 절과 task 로그의 차수 복구 규칙."""
from __future__ import annotations
from pm_engine.domain import identity_args
import re

TRIGGER_SESSION_PLACEHOLDER = "?"
# 공백·괄호·여백이 다른 세션 식별 헤더도 정규화해 판독한다.
_SESSION_SECTION_ANCHOR = "## 세션 식별 (현재까지 사용된 이름)"
_SESSION_ANCHOR_NORMALIZED = "세션식별"
_ANCHOR_STRIP_CHARS = frozenset("#()（）")
_PM_SESSION_ANCHOR_RE = re.compile("^  - \\*\\*(\\d+차)\\*\\*[^\\n]*$", re.MULTILINE)


def _normalize_h2_header(line: str) -> str:
    """h2 헤더 줄에서 '#'·공백(전각/반각/탭/CR)·괄호(전각/반각)를 제거한 정규화 문자열."""
    return "".join(
        (ch for ch in line if not ch.isspace() and ch not in _ANCHOR_STRIP_CHARS)
    )


def _extract_session_section(pm_state_text: str) -> tuple[str, int, int] | None:
    """pm_state.md 에서 세션 식별 절 텍스트와 그 시작·끝 위치를 반환한다.

    앵커 탐색은 정확 str.find 이 아니라 정규화 부분일치다 — '##' 로 시작하는 h2 헤더 줄
    (### 이상 제외)을 라인 스캔해, '#'·공백·괄호를 제거한 정규화 문자열이 '세션식별' 을
    포함하는 줄을 절 시작 후보로 본다(공백/괄호/여백 변형 흡수). 매치 실패 시 None.

    후보가 여럿이면 **pm 세션 entry(`- **N차**`)를 가진 첫 절을 우선**하고, 전부 없으면
    첫 후보로 폴백한다 — '## 세션 식별 규칙' 같은 설명-절이 실제 window 절보다 앞에 있을
    때 빈 절을 오선택해 window 미갱신(fail-soft 스킵)·오염되는 것을 막는다
    reviewer should-fix).

    반환: (section_text, start_offset, end_offset) 또는 None (앵커 불일치).
    end_offset 는 다음 ## 또는 ### 헤더 직전 위치 (혹은 파일 끝).
    """

    def _section_bounds(m: re.Match) -> tuple[str, int, int]:
        after_anchor = pm_state_text[m.end() :]
        next_header = re.search("^###? ", after_anchor, re.MULTILINE)
        end_offset = (
            len(pm_state_text) if next_header is None else m.end() + next_header.start()
        )
        return (pm_state_text[m.start() : end_offset], m.start(), end_offset)

    candidates = [
        m
        for m in re.finditer("^##(?!#).*$", pm_state_text, re.MULTILINE)
        if _SESSION_ANCHOR_NORMALIZED in _normalize_h2_header(m.group(0))
    ]
    if not candidates:
        return None
    for m in candidates:
        section = _section_bounds(m)
        if _PM_SESSION_ANCHOR_RE.search(section[0]):
            return section
    return _section_bounds(candidates[0])


def _find_pm_session_entries(section_text: str) -> list[re.Match]:
    """세션 식별 절에서 개별 pm 세션 entry 줄 (- **N차** ...) 의 match 목록을 반환한다.

    차수 순으로 정렬해 반환한다.
    """
    matches = list(_PM_SESSION_ANCHOR_RE.finditer(section_text))

    def _session_num(m: re.Match) -> int:
        return int(m.group(1).replace("차", ""))

    return sorted(matches, key=_session_num)


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
        if identity_args.TASK_PM_STATE_EMPTY_MARKER in section_text:
            return 1
        return TRIGGER_SESSION_PLACEHOLDER
    highest = max((int(m.group(1).replace("차", "")) for m in entries))
    return highest + 1


_TASK_HANDOFF_HEADER_RE = re.compile(
    "^## \\[\\d{4}-\\d{2}-\\d{2}\\]\\s+handoff\\s*\\|\\s*PM\\s+(?P<num>\\d+)차\\s+\\(task:(?P<task>[^)]+)\\).*$",
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
    본다. 정상 handoff가 task 장부에 종료 기록(`session` 비움)을 남긴 뒤 bootstrap 없이 재실행된 경우
    (`task_released=True`)에는 마지막 log 차수를 그대로 반환해 같은 세션 갱신 모드로 들어간다.
    release가 log보다 먼저 영속되므로 log append 뒤 state 기록 전에 중단된 반쪽 상태도 같은
    경로로 복구된다. 다음 정상 세션은 bootstrap의 `bind_task`가 기록을 다시 열므로 N+1이다.
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
        if task_released:
            return log_highest
        candidates.append(log_highest + 1)
    if candidates:
        return max(candidates)
    return state_next
