"""핸드오프 로그의 단일 락 기록."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import handoff_text
from pm_engine.infra import log_store


def _commit_handoff_log_change(log_file: Path, *, session_num: int | str, date: str, session: str | None, task: str | None, dirty_ack_note: str | None=None) -> tuple[bool, str, str | None]:
    """handoff log 판정과 append/replace를 공유 lock 한 구간에서 원자화한다.

    반환은 ``(same_session_rerun, preview, warning)``이다. 판독·append·교체는
    infra.log_store의 같은 OS lock 안에서 수행한다.
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_store.log_write_lock(log_file):
        log_text = log_store._read_text_exact(log_file) if log_file.exists() else ""
        same_session_rerun, payload, preview, warning = handoff_text._prepare_handoff_log_change(
            log_text,
            session_num=session_num,
            date=date,
            session=session,
            task=task,
            dirty_ack_note=dirty_ack_note,
        )
        if same_session_rerun:
            log_store._replace_atomic(log_file, payload)
        else:
            log_store.append_log_locked(log_file, payload)
    return same_session_rerun, preview, warning
