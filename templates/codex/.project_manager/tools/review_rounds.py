#!/usr/bin/env python3
"""리뷰 라운드 장부 공용 seam.

추가 리뷰어(`additional_reviewer.py`) 라운드 장부의 gate entry/예약/산출/수렴 판정 스키마를
소유한다(board 는 같은 장부를 읽기만 한다). 이 모듈은 장부를 쓰고 읽는 쪽이 갈리면 안 되는
다음 규칙만 소유한다.

* JSON 장부 read + PID/UUID 임시 파일을 이용한 원자 write (교체 자체는 공용 `fs` seam —
  플랫폼별 rename 의미 차이를 이 모듈이 복제하지 않는다)
* gate entry 정규화와 단조 sequence 예약/스폰 전 환불
* 예약 순서 기준 must-fix 추이, 진행 중 예약을 포함한 상한, 발산 조기 차단

파일락의 경로와 임계 구역은 호출자가 소유한다. 저장 파일과 부가 기록(livegate/wave)은 호출
도구가 정한다. 호출자는 반드시 같은 장부 옆의 파일락 안에서 read-modify-write를 수행한다.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.infra import fs  # noqa: E402


# baked 엔진 rev — engine_rev.py --bump가 기계 일괄 재작성한다.
ENGINE_REV = "v1.8.1"

CONVERGENCE_DIVERGING = "diverging"
CONVERGENCE_CAP_UNRESOLVED = "cap-unresolved"
CONVERGENCE_CAP_REACHED = "cap-reached"

# 폐지된 라운드 연장 승인이 장부에 남긴 필드. 스키마에서 뺐고 **값을 승계하지도 않는다** —
# 승인 수위만큼 집계 창을 잘라 판정 수를 줄이던 값이라, 남아 있으면 그 게이트의 집계가 전체
# 레코드 기준과 달라진다. 버리는 사실은 게이트·값과 함께 loud 로 알린다(정규화가 키를 떨구므로
# 장부가 다시 기록되면 알릴 대상이 없어 자연히 1회다 — 별도 상태 파일 없음).
RETIRED_ACK_FIELD = "acked_through"

# 기계 확인(pm-review-confirmation-v1) 증거로 여는 게이트 처분. 상한·쿼터 대신
# 증거를 요구한다(`pm_delegate.pm_verified_evidence_problem` 이
# 선언·완료 재검증 공용으로 판정한다). 이 모듈은 표기 문구만 소유한다.
RESOLUTION_PM_VERIFIED = "pm-verified"


def utc_now_iso() -> str:
    """UTC ISO-8601 시각 문자열."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def as_int(value: object) -> int:
    """장부 정수 필드 정규화(손상/누락은 0)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def read_ledger(
    path: Path | str,
    *,
    warning_sink: Callable[[str], None] | None = None,
) -> dict:
    """장부를 읽는다. 부재·손상·비매핑 최상위는 빈 장부로 fail-soft 한다.

    파일 부재는 첫 실행의 정상 형상이라 조용하다. 반면 읽기 권한/IO 실패·깨진 UTF-8·빈 파일을
    포함한 JSON 파싱 실패는 호출자가 ``warning_sink``를 주면 loud 진단을 남긴다. 어느 형상이든
    손상된 바이트가 라운드 호출을 예외로 막지 않도록 빈 장부로 복구한다.
    """
    target = Path(path)
    try:
        data = json.loads(fs.read_text_shared(target, encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if warning_sink is not None:
            warning_sink(
                f"{target}: {type(exc).__name__}: {exc}"
            )
        return {}
    return data if isinstance(data, dict) else {}


def write_ledger(path: Path | str, ledger: dict) -> None:
    """장부 JSON을 같은 디렉터리의 고유 임시 파일을 거쳐 원자 교체한다.

    read-modify-write 직렬화는 호출자의 파일락이 보장한다. 여기서는 독자가 부분 JSON을 보지
    않는 원자 교체와 실패한 임시 파일 정리만 소유한다.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(
        f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n"
        )
        fs.atomic_replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def warn_retired_ack_field(gate: str, value: int) -> None:
    """폐지 필드를 버린다는 사실을 그 게이트·값과 함께 알린다(승계 없음)."""
    print(
        f"경고: 라운드 장부의 폐지 필드 `{RETIRED_ACK_FIELD}` 를 버립니다 — 게이트 {gate} · "
        f"{RETIRED_ACK_FIELD}={value}. 폐지된 라운드 연장 승인이 남긴 값이라 승계하지 "
        "않으므로, 이 게이트의 집계는 이제 전체 레코드 기준입니다. 장부가 다시 기록되면 이 "
        "안내는 사라집니다.",
        file=sys.stderr,
    )


def normalize_gate_entry(
    ledger: dict,
    gate: str,
    *,
    reserved_keys: Sequence[str] = (),
) -> tuple[dict, int]:
    """gate entry를 external/internal 공용 스키마로 정규화해 장부에 심고, 폐기 필드
    (`RETIRED_ACK_FIELD`) 감지 여부를 호출부에 돌려준다 — `(entry, retired_ack)`.

    폐지 필드는 여기서 떨어진다(승계 없음). `retired_ack` 는 버린 원값(0=미검출)이다 —
    **알림·영속은 이 함수가 하지 않는다**: 순수 조회(report/preview)는 정규화 결과를 저장할
    수 없어 '1회' 고지를 지킬 수 없고, mutation/차단 경로는 저장 시점을 스스로 정한다(거부
    직전에라도 저장해야 알림이 실제로 1회가 된다). 호출부가 `retired_ack` 를 보고 자기
    계약에 맞는 저장·고지 정책을 고른다(`warn_retired_ack_field` 재사용). 접는 자리를
    정규화로 둬야 조회·기록·legacy 승계가 한 규칙이다(승계 항목은 장부 스캔이 아니라 이
    경로로 들어온다)."""
    if gate in reserved_keys:
        raise ValueError(
            f"라운드 장부 예약 키를 게이트로 쓸 수 없습니다: {gate!r} "
            f"(예약 키: {', '.join(sorted(reserved_keys))})"
        )
    entry = ledger.get(gate)
    if not isinstance(entry, dict):
        entry = {}
    records = entry.get("records")
    records = [row for row in records if isinstance(row, dict)] \
        if isinstance(records, list) else []
    rounds = entry.get("rounds")
    rounds = [row for row in rounds if isinstance(row, dict)] \
        if isinstance(rounds, list) else []
    resolution = entry.get("resolution")
    if not isinstance(resolution, dict):
        resolution = None
    retired_ack = max(0, as_int(entry.get(RETIRED_ACK_FIELD)))
    normalized = {
        "count": as_int(entry.get("count")),
        "sequence": max(
            as_int(entry.get("sequence")),
            *(as_int(row.get("sequence", row.get("number"))) for row in records),
            0,
        ),
        "resolution": resolution,
        "records": records,
        "rounds": rounds,
    }
    ledger[gate] = normalized
    return normalized, retired_ack


def reservation_deadline(wall_timeout_sec: int | None) -> str | None:
    """예약이 실행 중일 수 있는 마지막 시각(백스톱 부재 시 None)."""
    if not wall_timeout_sec or wall_timeout_sec <= 0:
        return None
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=wall_timeout_sec)
    ).isoformat()


def reserve_round(
    entry: dict,
    record_id: str,
    *,
    wall_timeout_sec: int | None = None,
    target_rev: str | None = None,
) -> dict:
    """단조 sequence로 한 라운드를 예약하고 예약 레코드를 반환한다."""
    entry["sequence"] = max(0, as_int(entry.get("sequence"))) + 1
    entry["count"] = max(0, as_int(entry.get("count"))) + 1
    record = {
        "id": record_id,
        "number": entry["sequence"],
        "sequence": entry["sequence"],
        "started_at": utc_now_iso(),
        "target_rev": target_rev,
        "deadline": reservation_deadline(wall_timeout_sec),
    }
    entry.setdefault("records", []).append(record)
    return record


def refund_round(entry: dict, record_id: str) -> bool:
    """스폰 전 실패한 예약을 제거한다. sequence identity는 재사용하지 않는다."""
    before = len(entry.get("records", []))
    entry["records"] = [
        row for row in entry.get("records", [])
        if isinstance(row, dict) and row.get("id") != record_id
    ]
    refunded = len(entry["records"]) != before
    if refunded and entry.get("count", 0) > 0:
        entry["count"] -= 1
    return refunded


def append_round_outcome(entry: dict, outcome: dict) -> dict:
    """완성된 라운드 산출을 보존 이력에 추가한다."""
    entry.setdefault("rounds", []).append(outcome)
    return outcome


def round_outcome_order_key(outcome: dict) -> tuple[bool, int]:
    """sequence 없는 구기록을 앞에 두는 안정 정렬 키."""
    sequence = outcome.get("sequence")
    valid = isinstance(sequence, int) and not isinstance(sequence, bool)
    return (valid, sequence if valid else 0)


def ordered_round_outcomes(
    rounds: Sequence[dict],
    *,
    order_key: Callable[[dict], object] | None = None,
) -> list[dict]:
    """append 완료 순서가 아니라 예약 sequence 순으로 산출을 정렬한다."""
    return sorted(rounds, key=order_key or round_outcome_order_key)


def recorded_must_fix_series(
    entry: dict,
    *,
    order_key: Callable[[dict], object] | None = None,
) -> tuple[int | None, ...]:
    """예약 순서의 must-fix 추이. 셀 근거가 없으면 None을 보존한다."""
    rounds = [
        row for row in (entry.get("rounds") or []) if isinstance(row, dict)
    ]
    series: list[int | None] = []
    for outcome in ordered_round_outcomes(rounds, order_key=order_key):
        value = outcome.get("must_fix")
        series.append(
            value if isinstance(value, int) and not isinstance(value, bool) else None
        )
    return tuple(series)


def inflight_reservations(
    entry: dict,
    *,
    wall_timeout_sec: int | None = None,
) -> int:
    """아직 실행 중일 수 있는 미마감 예약 수."""
    now = utc_now_iso()
    legacy_cutoff = None
    if wall_timeout_sec and wall_timeout_sec > 0:
        legacy_cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=wall_timeout_sec)
        ).isoformat()
    total = 0
    for row in entry.get("records") or []:
        if not isinstance(row, dict) or row.get("finished_at"):
            continue
        deadline = row.get("deadline")
        if isinstance(deadline, str) and deadline:
            if deadline < now:
                continue
            total += 1
            continue
        started = row.get("started_at")
        if (
            legacy_cutoff is not None
            and isinstance(started, str)
            and started
            and started < legacy_cutoff
        ):
            continue
        total += 1
    return total


def convergence_round_usage(
    entry: dict,
    *,
    wall_timeout_sec: int | None = None,
    order_key: Callable[[dict], object] | None = None,
) -> tuple[int, int]:
    """(완료 산출 수, 실행 중 예약 수)."""
    return (
        len(recorded_must_fix_series(entry, order_key=order_key)),
        inflight_reservations(entry, wall_timeout_sec=wall_timeout_sec),
    )


def convergence_refusal(
    entry: dict,
    limit: int,
    *,
    wall_timeout_sec: int | None = None,
    order_key: Callable[[dict], object] | None = None,
) -> str | None:
    """must-fix 증가 또는 완료+진행 중 라운드 상한의 거부 사유."""
    series = recorded_must_fix_series(entry, order_key=order_key)
    if (
        len(series) >= 2
        and series[-1] is not None
        and series[-2] is not None
        and series[-1] > series[-2]
    ):
        return CONVERGENCE_DIVERGING
    completed, inflight = convergence_round_usage(
        entry, wall_timeout_sec=wall_timeout_sec, order_key=order_key
    )
    if completed + inflight >= limit:
        last = series[-1] if series else None
        return CONVERGENCE_CAP_REACHED if last == 0 else CONVERGENCE_CAP_UNRESOLVED
    return None


def describe_pm_verified_resolution(declared: dict) -> str:
    """보고/완료 출력용 표기 — 기계 확인 해소임을 명시(리뷰 재투입 없음).

    `declared` 는 보고 호출 관례를 유지하려고 받는다. 이 처분은 별도 근거 blob 을 싣지 않아
    (증거는 매번 라이브로 재검증) 문구는 kind 고정이다.
    """
    del declared
    return "pm-verified(PM 기계 확인 해소·reviewer 재투입 없음)"
