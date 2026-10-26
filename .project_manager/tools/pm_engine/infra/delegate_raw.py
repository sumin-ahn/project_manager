"""위임 원출력 첨부와 재주입 (층: infra)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from pm_engine.domain import conf_schema
from pm_engine.infra.harness import contract
from pm_engine.domain import delegate_errors
from pm_engine.infra.harness import delegate_run
from pm_engine.domain import delegate_run_rules
from pm_engine.infra import fs
from pm_engine.infra import raw_output
import sys



def cold_reinjection_record(
    home, ticket: str,
    role: str,
    output_dir: Path | None,
) -> dict | None:
    """같은 ticket+role의 최근 **마감** 레코드, 없거나 장부를 못 읽으면 None.

    장부 부재·손상은 비용 가드만 fail-open한다. raw 감사 기록 자체의 쓰기 계약은 별도 안전
    경계이므로 여기서 장부를 고치거나 초기화하지 않는다.
    """
    _raw_dir, ledger_path = raw_output._raw_storage(home, output_dir)
    if not ledger_path.is_file():
        print(
            f"경고: cold 재투입 비용 가드가 raw 장부를 찾지 못해 fail-open 합니다: "
            f"{ledger_path}",
            file=sys.stderr,
        )
        return None
    try:
        rows = raw_output.raw_records(ledger_path)
    except (OSError, UnicodeError, ValueError) as exc:
        print(
            "경고: cold 재투입 비용 가드가 raw 장부를 읽지 못해 fail-open 합니다: "
            f"{ledger_path} ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return None
    matches = [
        row for row in rows
        if (
            isinstance(row, dict)
            and row.get(delegate_run_rules.RESUME_FIELD_TICKET) == ticket
            and row.get("role") == role
            and row.get("finished_at") is not None
        )
    ]
    if not matches:
        return None
    return max(matches, key=lambda row: str(row.get("started_at", "")))


def cold_reinjection_rejection(
    ticket: str,
    role: str,
    harness: str,
    record: Mapping[str, object],
) -> str:
    """cold 재투입 거부와 하네스 capability에 맞는 재호출 처방."""
    lines = [
        f"오류: cold 재투입 거부 — ticket={ticket} · role={role}의 완료 레코드가 "
        f"이미 있습니다(id={record.get('id')}).",
    ]
    if contract.harness_supports_resume(harness):
        lines.append(f"  · 이어서 재실행: --resume-from {ticket}")
    lines.append("  · 의도적 fresh 재실행: --fresh <사유>")
    return "\n".join(lines)


def _attached_record_reply(record: Mapping[str, object], raw_path: Path) -> str:
    """장부가 가리키는 실제 raw에서 최종 reply를 추출한다(요약·발췌 없음)."""
    try:
        content = fs.read_text_shared(raw_path, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(
            f"첨부 raw 읽기 실패: {raw_path} ({type(exc).__name__}: {exc})"
        ) from exc

    harness = record.get("harness")
    if delegate_run_rules._DELEGATE_RAW_STDOUT_MARKER in content:
        if not isinstance(harness, str):
            raise delegate_errors.DelegateError(f"첨부 레코드 harness 형식 오류: {record.get('id')}")
        reply = delegate_run.extract_reply(harness, delegate_run_rules._delegate_raw_stdout(content, raw_path))
    else:
        answer = delegate_run_rules._additional_reviewer_raw_answer(content)
        if isinstance(harness, str) and harness in conf_schema.HARNESS_CHOICES:
            reply = delegate_run.extract_reply(harness, answer)
        else:
            # legacy/custom reviewer의 raw 회신 채널은 이미 최종 reply 텍스트다.
            reply = answer
    if not isinstance(reply, str) or not reply.strip():
        raise delegate_errors.DelegateError(
            f"첨부 raw에서 최종 reply를 추출하지 못함: record={record.get('id')} · raw={raw_path}"
        )
    return reply


def resolve_attached_raw(
    home, selector: str | None,
    *,
    output_dir: Path | None,
) -> delegate_run_rules.AttachedRaw | None:
    """`--attach-raw`를 실제 마감 장부 행+raw 최종 reply로 해소한다(fail-loud)."""
    if not selector:
        return None
    _raw_dir, ledger_path = raw_output._raw_storage(home, output_dir)
    if not ledger_path.is_file():
        raise delegate_errors.DelegateError(f"--attach-raw raw 장부가 없음: {ledger_path}")
    try:
        rows = raw_output.raw_records(ledger_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise delegate_errors.DelegateError(
            f"--attach-raw raw 장부 읽기 실패: {ledger_path} "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    record = delegate_run_rules.select_attached_raw_record(rows, selector)
    raw_value = record.get("raw_path")
    if not isinstance(raw_value, str) or not raw_value:
        raise delegate_errors.DelegateError(f"첨부 레코드 raw_path 형식 오류: {record.get('id')}")
    raw_path = Path(raw_value)
    if not raw_path.is_absolute():
        raise delegate_errors.DelegateError(
            f"첨부 레코드 raw_path가 절대경로가 아님: {record.get('id')} · {raw_value!r}"
        )
    return delegate_run_rules.AttachedRaw(
        record_id=str(record.get("id")),
        reply=_attached_record_reply(record, raw_path),
    )


def append_attached_raw(task_text: str, attached: delegate_run_rules.AttachedRaw | None) -> str:
    """첨부 reply를 합성 프롬프트 말미에 byte-for-byte 붙인다."""
    if attached is None:
        return task_text
    return task_text + "\n\n" + delegate_run_rules.ATTACH_RAW_SECTION_TITLE + "\n\n" + attached.reply
