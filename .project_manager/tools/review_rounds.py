#!/usr/bin/env python3
"""리뷰 라운드 장부 공용 seam.

추가 리뷰어(`external_review.py`)와 내부 code-reviewer(`pm_delegate.py`)는 저장 파일은 서로
분리하지만, 장부의 gate entry/예약/산출/수렴 판정 스키마는 같이 쓴다. 이 모듈은 두 축에서
갈리면 안 되는 다음 규칙만 소유한다.

* JSON 장부 read + PID/UUID 임시 파일을 이용한 원자 write
* gate entry 정규화와 단조 sequence 예약/스폰 전 환불
* 예약 순서 기준 must-fix 추이, 진행 중 예약을 포함한 상한, 발산 조기 차단

파일락의 경로와 임계 구역은 호출자가 소유한다. 두 도구의 저장 파일과 부가 부기(external의
livegate/wave, internal의 raw attempt 결속)가 다르기 때문이다. 호출자는 반드시 같은 장부 옆의
파일락 안에서 read-modify-write를 수행한다.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path


ENGINE_REV = "v1.7.5"

CONVERGENCE_DIVERGING = "diverging"
CONVERGENCE_CAP_UNRESOLVED = "cap-unresolved"
CONVERGENCE_CAP_REACHED = "cap-reached"

RESOLUTION_PM_FIXED = "pm-fixed"
PM_FIXED_EVIDENCE_KEY = "pm_fixed_evidence"
PM_FIXED_USAGE_KEY = "pm_fixed"
PM_FIXED_INTERNAL_ROUNDS_LIMIT = 3

_PM_FIXED_RESULT_RE = re.compile(r"^rc=0(?:\s+\([^\r\n;]+\))?$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


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
        data = json.loads(target.read_text(encoding="utf-8"))
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
            json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(str(tmp), str(target))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def normalize_gate_entry(
    ledger: dict,
    gate: str,
    *,
    reserved_keys: Sequence[str] = (),
) -> dict:
    """gate entry를 external/internal 공용 스키마로 정규화해 장부에 심는다."""
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
    normalized = {
        "count": as_int(entry.get("count")),
        "acked_through": as_int(entry.get("acked_through")),
        "sequence": max(
            as_int(entry.get("sequence")),
            *(as_int(row.get("sequence", row.get("number"))) for row in records),
            0,
        ),
        "confirm_fix": max(0, as_int(entry.get("confirm_fix"))),
        # PM 직접 해소는 confirm-fix와 마찬가지로 장부가 소유하는 게이트당 1회 쿼터다.
        # 1 초과 손상값도 그대로 보존해 재사용을 닫는 쪽으로 판정한다.
        PM_FIXED_USAGE_KEY: max(0, as_int(entry.get(PM_FIXED_USAGE_KEY))),
        "resolution": resolution,
        "records": records,
        "rounds": rounds,
    }
    ledger[gate] = normalized
    return normalized


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


def latest_round_outcome(
    entry: dict,
    *,
    order_key: Callable[[dict], object] | None = None,
) -> dict | None:
    """예약 순서상 최신 완료 산출."""
    rounds = [
        row for row in (entry.get("rounds") or []) if isinstance(row, dict)
    ]
    ordered = ordered_round_outcomes(rounds, order_key=order_key)
    return ordered[-1] if ordered else None


def parse_pm_fixed_evidence(
    value: object,
    *,
    repo_root: Path | str | None = None,
) -> dict[str, object]:
    """PM 직접 해소 근거를 구조화하고 선택적으로 실제 변경 지점을 검증한다.

    CLI 문자열 형식은 정확히 다음 세 필드다::

        change=<repo-relative-file>:<line>; regression=<command>; result=rc=0 (<summary>)

    자유 서술 한 줄은 받지 않는다. 회귀 명령과 성공 exit 값(``rc=0``)을 분리해 장부 소비자가
    기계적으로 확인할 수 있게 하고, ``repo_root``가 주어지면 경로 containment·파일 실존·행 범위도
    선언 전에 확인한다. 장부에 이미 구조화된 dict가 들어온 경우에도 같은 정규화를 적용한다.
    """
    if isinstance(value, str):
        parts = value.split("; ")
        if len(parts) != 3:
            raise ValueError(
                "--pm-fixed 근거 형식은 `change=<file>:<line>; "
                "regression=<command>; result=rc=0 (<summary>)` 이어야 합니다"
            )
        change_part, regression_part, result_part = parts
        if not change_part.startswith("change="):
            raise ValueError("--pm-fixed 근거에 `change=<file>:<line>`이 필요합니다")
        if not regression_part.startswith("regression="):
            raise ValueError("--pm-fixed 근거에 `regression=<command>`가 필요합니다")
        if not result_part.startswith("result="):
            raise ValueError("--pm-fixed 근거에 `result=rc=0`이 필요합니다")
        change = change_part.removeprefix("change=").strip()
        regression = regression_part.removeprefix("regression=").strip()
        result = result_part.removeprefix("result=").strip()
    elif isinstance(value, dict):
        change = value.get("change")
        regression = value.get("regression")
        result = value.get("result")
        if not all(isinstance(item, str) for item in (change, regression, result)):
            raise ValueError("pm-fixed 구조화 근거의 change/regression/result는 문자열이어야 합니다")
        change = change.strip()
        regression = regression.strip()
        result = result.strip()
    else:
        raise ValueError("--pm-fixed 근거는 비어 있지 않은 구조화 문자열이어야 합니다")

    location = re.fullmatch(r"(.+):([1-9][0-9]*)", change)
    if location is None:
        raise ValueError("--pm-fixed 변경 지점은 `<repo-relative-file>:<positive-line>` 형식이어야 합니다")
    path_text = location.group(1).strip()
    line = int(location.group(2))
    relative = Path(path_text)
    if (
        not path_text
        or "\\" in path_text
        or relative.is_absolute()
        or _WINDOWS_ABSOLUTE_RE.match(path_text) is not None
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise ValueError("--pm-fixed 변경 파일은 traversal 없는 repo 상대 POSIX 경로여야 합니다")
    if not regression or "\n" in regression or "\r" in regression:
        raise ValueError("--pm-fixed regression에는 실행한 회귀 명령이 필요합니다")
    if _PM_FIXED_RESULT_RE.fullmatch(result) is None:
        raise ValueError("--pm-fixed result는 성공 종료 `rc=0`과 선택적 요약을 기록해야 합니다")

    normalized: dict[str, object] = {
        "change": f"{relative.as_posix()}:{line}",
        "path": relative.as_posix(),
        "line": line,
        "regression": regression,
        "result": result,
    }
    if repo_root is None:
        return normalized

    root = Path(repo_root).resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("--pm-fixed 변경 파일이 repo 경계를 벗어납니다") from exc
    if not target.is_file():
        raise ValueError(f"--pm-fixed 변경 파일을 찾을 수 없습니다: {relative.as_posix()}")
    try:
        with target.open("r", encoding="utf-8") as stream:
            line_count = sum(1 for _line in stream)
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            f"--pm-fixed 변경 파일 행을 검증할 수 없습니다: {relative.as_posix()} "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    if line > line_count:
        raise ValueError(
            f"--pm-fixed 변경 지점이 파일 범위를 벗어납니다: "
            f"{relative.as_posix()}:{line} (총 {line_count}행)"
        )
    return normalized


def _pm_fixed_shape_problem(
    entry: dict,
    limit: int,
    *,
    wall_timeout_sec: int | None = None,
) -> str | None:
    """쿼터를 제외한 PM 직접 해소의 상한/confirm-fix/마지막 반려 형상."""
    completed, inflight = convergence_round_usage(
        entry, wall_timeout_sec=wall_timeout_sec,
    )
    if completed < limit:
        return f"라운드 상한이 미소진입니다(완료 {completed}/{limit})"
    if inflight:
        return f"진행 중 라운드 예약 {inflight}건이 남아 있습니다"
    refusal = convergence_refusal(
        entry, limit, wall_timeout_sec=wall_timeout_sec,
    )
    if refusal == CONVERGENCE_DIVERGING:
        return "마지막 must-fix 추이가 증가(발산)해 PM 직접 해소 대상이 아닙니다"
    confirm_fix = max(0, as_int(entry.get("confirm_fix")))
    if confirm_fix < 1:
        return "confirm-fix 1회가 아직 소진되지 않았습니다"
    latest = latest_round_outcome(entry)
    if latest is None or latest.get("verdict") != 1:
        return "마지막 완료 라운드가 유효 반려(rc=1)가 아닙니다"
    residual = latest.get("must_fix")
    if isinstance(residual, bool) or not isinstance(residual, int) or residual <= 0:
        return "마지막 반려 라운드에 양의 must-fix 잔여가 없습니다"
    return None


def pm_fixed_refusal(
    entry: dict,
    limit: int,
    *,
    wall_timeout_sec: int | None = None,
) -> str | None:
    """새 PM 직접 해소를 열 수 없는 이유(가능하면 ``None``).

    일반 라운드 상한과 confirm-fix 1회를 모두 소비하고 마지막 완료 라운드가 잔여를 가진 유효
    반려일 때만 연다. 진행 중 예약이 있으면 그 결과를 앞질러 닫지 않는다. 쿼터는 장부의
    ``pm_fixed`` 필드가 소유하며 한 번 소비된 뒤에는 처분이 교체돼도 되살아나지 않는다.
    """
    used = max(0, as_int(entry.get(PM_FIXED_USAGE_KEY)))
    if used >= 1:
        return f"게이트당 1회 제한을 이미 소진했습니다(pm-fixed={used})"
    return _pm_fixed_shape_problem(
        entry, limit, wall_timeout_sec=wall_timeout_sec,
    )


def recorded_pm_fixed_problem(
    entry: dict,
    limit: int,
    *,
    wall_timeout_sec: int | None = None,
) -> str | None:
    """기록된 pm-fixed 처분을 소비할 때 쿼터와 발동 형상을 다시 검증한다."""
    used = max(0, as_int(entry.get(PM_FIXED_USAGE_KEY)))
    if used != 1:
        return f"pm-fixed 사용 횟수가 정확히 1이 아닙니다(pm-fixed={used})"
    return _pm_fixed_shape_problem(
        entry, limit, wall_timeout_sec=wall_timeout_sec,
    )


def declare_pm_fixed_resolution(
    entry: dict,
    evidence: object,
    *,
    limit: int,
    repo_root: Path | str,
    wall_timeout_sec: int | None = None,
) -> dict:
    """검증·상한·1회 쿼터 소비와 처분 기록값 생성을 한 공용 축에서 수행한다."""
    normalized_evidence = parse_pm_fixed_evidence(evidence, repo_root=repo_root)
    problem = pm_fixed_refusal(
        entry, limit, wall_timeout_sec=wall_timeout_sec,
    )
    if problem is not None:
        raise ValueError(f"pm-fixed 처분을 사용할 수 없습니다: {problem}")
    rounds = [
        row for row in (entry.get("rounds") or []) if isinstance(row, dict)
    ]
    latest = latest_round_outcome(entry)
    sequence = latest.get("sequence") if latest is not None else None
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = None
    entry[PM_FIXED_USAGE_KEY] = max(
        0, as_int(entry.get(PM_FIXED_USAGE_KEY))
    ) + 1
    return {
        "kind": RESOLUTION_PM_FIXED,
        PM_FIXED_EVIDENCE_KEY: normalized_evidence,
        "ts": utc_now_iso(),
        "must_fix": latest["must_fix"],
        "round_sequence": sequence,
        "rounds": len(rounds),
    }


def describe_pm_fixed_resolution(declared: dict) -> str:
    """보고/완료 출력용 표기. 리뷰 통과와 혼동되지 않는 고정 어휘를 쓴다."""
    evidence = parse_pm_fixed_evidence(declared.get(PM_FIXED_EVIDENCE_KEY))
    return (
        "pm-fixed(PM 직접 해소·리뷰 통과 아님; "
        f"변경 {evidence['change']}; 회귀 {evidence['regression']} => {evidence['result']})"
    )
