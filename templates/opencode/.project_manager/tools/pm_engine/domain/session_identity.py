"""세션 정체성 source와 미해소 사유."""
from __future__ import annotations

from collections.abc import Sequence



IDENTITY_SOURCE_CWD_LEASE = "cwd→lease"


IDENTITY_SOURCE_SINGLE_ACTIVE_TASK = "단일 활성 task"


IDENTITY_SOURCE_UNRESOLVED = "정체성 미해소"


SNAPSHOT_IDENTITY_SOURCES = frozenset({
    IDENTITY_SOURCE_CWD_LEASE,
    IDENTITY_SOURCE_SINGLE_ACTIVE_TASK,
    IDENTITY_SOURCE_UNRESOLVED,
})


HANDOFF_TASK_MODE_IDENTITY_SOURCES = frozenset({
    IDENTITY_SOURCE_CWD_LEASE,
    IDENTITY_SOURCE_SINGLE_ACTIVE_TASK,
})


SESSION_TASK_REASON_NO_SESSION = "세션 ID 없음"


SESSION_TASK_REASON_NO_TASK = "이 세션이 연 task 없음 (세션 {session})"


SESSION_TASK_REASON_MANY_TASKS = "이 세션이 연 task 여럿 (세션 {session}: {names})"


def session_task(rows: Sequence[object], session_id: str | None) -> tuple[str | None, str | None]:
    """task 기록 행들에서 이 하네스 세션이 연 task 를 찾는다 — 세션 task 를 찾는 규칙은 이것 하나다.

    `session` 값(앞뒤 공백 제거)이 세션 ID 와 같은 행이 정확히 1개일 때만 해소한다. 반환은 해소면
    ``(task, None)``, 미해소면 ``(None, 사유)`` 이고 사유는 `SESSION_TASK_REASON_*` 세 상수다.
    이름이나 세션 값이 문자열이 아닌 행은 세지 않는다."""
    wanted = session_id.strip() if isinstance(session_id, str) else ""
    if not wanted:
        return None, SESSION_TASK_REASON_NO_SESSION
    names = sorted(
        row["name"] for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("name"), str) and row["name"].strip()
        and isinstance(row.get("session"), str)
        and row["session"].strip() == wanted
    )
    if not names:
        return None, SESSION_TASK_REASON_NO_TASK.format(session=wanted)
    if len(names) > 1:
        return None, SESSION_TASK_REASON_MANY_TASKS.format(session=wanted, names=", ".join(names))
    return names[0], None
