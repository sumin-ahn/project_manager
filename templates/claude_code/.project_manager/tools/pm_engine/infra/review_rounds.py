#!/usr/bin/env python3
"""리뷰 라운드 장부 공용 seam.

추가 리뷰어(`additional_reviewer.py`) 라운드 장부의 gate entry/예약/산출/수렴 판정 스키마를
소유한다(board 는 같은 장부를 읽기만 한다). 이 모듈은 장부를 쓰고 읽는 쪽이 갈리면 안 되는
다음 규칙만 소유한다.

* JSON 장부 read + PID/UUID 임시 파일을 이용한 원자 write (교체 자체는 공용 `fs` seam —
  플랫폼별 rename 의미 차이를 이 모듈이 복제하지 않는다)
* gate entry 정규화와 단조 sequence 예약/스폰 전 환불
* 예약 순서 기준 must-fix 추이, 진행 중 예약을 포함한 상한, 발산 조기 차단
* 게이트 처분 선언(`resolution`) 판정, 마지막 라운드 판정과 잔여 must-fix

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

from pm_engine.infra import fs


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


def ordered_round_outcomes(rounds: Sequence[dict]) -> list[dict]:
    """append 완료 순서가 아니라 예약 sequence 순으로 산출을 정렬한다."""
    return sorted(rounds, key=round_outcome_order_key)


def recorded_must_fix_series(entry: dict) -> tuple[int | None, ...]:
    """예약 순서의 must-fix 추이. 셀 근거가 없으면 None을 보존한다."""
    rounds = [
        row for row in (entry.get("rounds") or []) if isinstance(row, dict)
    ]
    series: list[int | None] = []
    for outcome in ordered_round_outcomes(rounds):
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
) -> tuple[int, int]:
    """(완료 산출 수, 실행 중 예약 수)."""
    return (
        len(recorded_must_fix_series(entry)),
        inflight_reservations(entry, wall_timeout_sec=wall_timeout_sec),
    )


def convergence_refusal(
    entry: dict,
    limit: int,
    *,
    wall_timeout_sec: int | None = None,
) -> str | None:
    """must-fix 증가 또는 완료+진행 중 라운드 상한의 거부 사유."""
    series = recorded_must_fix_series(entry)
    if (
        len(series) >= 2
        and series[-1] is not None
        and series[-2] is not None
        and series[-1] > series[-2]
    ):
        return CONVERGENCE_DIVERGING
    completed, inflight = convergence_round_usage(
        entry, wall_timeout_sec=wall_timeout_sec
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


# ── 장부 위치와 게이트 판정 (회귀 강등·릴리즈 차단·조회 표 공용) ─────────────
REVIEW_ROUNDS_LEDGER_NAME = "review_rounds.json"
_REVIEW_LEDGER_WAVE_KEY = "wave"       # 게이트가 아닌 예약 키 (additional_reviewer WAVE_SECTION_KEY)


def _review_rounds_ledger(home) -> Path:
    """추가 리뷰 라운드 장부 경로 — 호출 시점 `LOCAL_DIR` 파생(테스트 격리 추종)."""
    return home.local_dir / REVIEW_ROUNDS_LEDGER_NAME


def gate_last_round(entry: dict) -> dict | None:
    """게이트의 **마지막 라운드 산출** — 기록이 없으면 None (공용 seam·사본 금지).

    "마지막이 어느 라운드인가"를 이 한 곳이 정한다 — append 순서가 아니라 예약 순번이다
    (`round_outcome_order_key`). 이 판정을 세 표면이 쓴다: 회귀 강등의 최신 판정, 릴리즈 게이트의
    잔여 must-fix, 처분 선언의 근거 게이트 통과 여부. 사본을 두면 그 사본이 서로 다른 라운드를
    '최신'이라고 답한다. `rounds` 가 리스트가 아닌 손상 항목은 None 이다 — 이 seam 은 판정을
    돌려줄 뿐이라 손상의 **처분**은 호출부가 정한다(회귀 강등은 fail-open·릴리즈는 fail-closed·
    `gate_entry_corruption`)."""
    rounds = entry.get("rounds")
    if not isinstance(rounds, list):
        return None
    rounds = [row for row in rounds if isinstance(row, dict)]
    if not rounds:
        return None
    return sorted(rounds, key=round_outcome_order_key)[-1]


def _last_round_verdict(entry: dict) -> int | None:
    """게이트의 **마지막 라운드** 판정 rc (기록 없음/손상이면 None).

    나열 순서는 append 순서가 아니라 **예약 순번**이다(공용 seam `gate_last_round` —
    additional_reviewer 조회 표와 같은 한 규칙)."""
    outcome = gate_last_round(entry)
    verdict = outcome.get("verdict") if outcome is not None else None
    return verdict if isinstance(verdict, int) and not isinstance(verdict, bool) else None


def gate_residual_must_fix(entry: dict) -> int | None:
    """마지막 라운드가 남긴 must-fix 잔여 — 3-상태 (공용 seam).

    반환은 `0`(잔여 없음) · `N>0`(N 건) · **`None`(미상)** 이다. 미상은 "호출은 됐는데 판정이
    무효했다"(타임아웃·오염·섹션 부재로 셀 근거가 없던 라운드)이고, 그건 "잔여가 없다"가 아니라
    **확인하지 못했다**이다 — 0 으로 접으면 판정 무효 라운드로 끝난 게이트에서 릴리즈 차단이
    조용히 풀린다(실장부 실례). 형제 seam(additional_reviewer 의 수렴 판정)도 '미상'을 해소로 세지
    않는 같은 규칙이다.

    다만 **통과(rc 0)로 끝난 라운드의 미상은 비대상(0)** 이다 — 그쪽은 판정이 났고 must-fix 섹션이
    없었을 뿐이라 반려 사실이 없다. suggestion 은 어느 경우에도 이 축에 들어오지 않는다(이월 허용).
    """
    outcome = gate_last_round(entry)
    if outcome is None:
        return 0
    value = outcome.get("must_fix")
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return 0 if _last_round_verdict(entry) == 0 else None


def gate_has_residual(entry: dict) -> bool:
    """처분이 필요한 잔여가 있는가 — N>0 **또는 미상** (공용 seam).

    릴리즈 차단과 처분 선언(`--resolve-gate`)이 이 한 술어를 공유한다. 두 표면이 갈리면 차단은
    되는데 선언은 거부되는 데드락이 난다(미상 게이트가 정확히 그 형상이다)."""
    residual = gate_residual_must_fix(entry)
    return residual is None or residual > 0


def gate_residual_label(entry: dict) -> str:
    """잔여 표기 — `N건` / `미상(판정 무효 라운드)` (차단 사유·선언 응답 공용)."""
    residual = gate_residual_must_fix(entry)
    return "미상(판정 무효 라운드)" if residual is None else f"{residual}건"


# ── 게이트 처분 선언 (장부 `resolution` 절) ────────────────────────────────
# 라운드 상한으로 종결된 게이트의 **잔여 must-fix 를 어떻게 소화했는가**를 장부에 남기는 절이다.
# 쓰는 쪽은 additional_reviewer(`--resolve-gate`)지만 **읽는 규칙은 여기 하나**다 — 릴리즈 차단과
# 조회 표가 각자 해석하면 같은 장부를 놓고 "처분됐다/아니다"가 갈린다.
# 기계 확인 증거로 여는 게이트 처분(`RESOLUTION_PM_VERIFIED`)은 장부마다 `allow_pm_verified` 명시
# 시에만 인정된다(추가 리뷰어 release 축이 스코프 인자로 채널 격리해 허용 · 기본 호출은 허용하지 않는다).

# 처분 표기 어휘 — 선언 응답(additional_reviewer)과 차단 사유(여기)가 같은 말을 쓰게.
GATE_RESOLUTION_LABELS: dict[str, str] = {
    RESOLUTION_PM_VERIFIED: "pm-verified(PM 기계 확인 해소·reviewer 재투입 없음)",
}

# 처분이 결속하는 **라운드 좌표** 필드 — 선언 시점의 마지막 라운드 순번과 산출 수.
_GATE_RESOLUTION_BINDING_KEYS: tuple[str, ...] = ("round_sequence", "rounds")
_MISSING_BINDING = object()   # `round_sequence` 키 부재와 명시 null(정상 값)을 구분하는 표식.


def gate_entry_corruption(entry: object) -> str | None:
    """게이트 항목이 **해석 가능한 형상**인지 — 아니면 사유 (정상이면 None·공용 seam).

    "존재하는데 읽을 수 없음"은 "잔여 없음"이 아니다. 손상 형상마다 조용한 통과 조각이 생기는 것이
    이 축의 실패 모드였다 — 비-dict 항목은 순회에서 건너뛰고, 문자열 `rounds` 는 '기록 없음'으로,
    정수 `rounds` 는 예외로 갈렸다(각각 통과·통과·중단). 셋을 한 술어로 모아 호출부가 **한 처분**
    (릴리즈 차단)으로 수렴시킨다."""
    if not isinstance(entry, dict):
        return f"게이트 항목 형식 오류({type(entry).__name__}) — 해석 불가"
    rounds = entry.get("rounds")
    if rounds is not None and not isinstance(rounds, list):
        return f"`rounds` 형식 오류({type(rounds).__name__}) — 라운드 이력 해석 불가"
    if isinstance(rounds, list) and any(not isinstance(row, dict) for row in rounds):
        return "`rounds` 에 매핑이 아닌 원소 — 라운드 이력 해석 불가"
    resolution = entry.get("resolution")
    if resolution is not None and not isinstance(resolution, dict):
        return f"`resolution` 형식 오류({type(resolution).__name__}) — 처분 선언 해석 불가"
    return None


def gate_round_binding(entry: dict) -> dict:
    """게이트의 **현재 라운드 좌표** — `{round_sequence, rounds}` (선언·검증 공용 seam).

    처분은 "그때 그 잔여"에 대한 선언이다. 좌표 없이 게이트 단위로만 남기면 처분 뒤에 새 반려
    라운드가 와도 옛 선언이 잔여를 계속 지운다(처분의 무기한 유효화). 좌표는 둘을 함께 본다 —
    마지막 라운드의 예약 순번(정렬 좌표)과 산출 수. 순번 없는 구세대 산출만 있는 장부에서도
    산출 수가 증가를 잡는다."""
    rounds = entry.get("rounds")
    rounds = [row for row in rounds if isinstance(row, dict)] if isinstance(rounds, list) else []
    last = gate_last_round(entry)
    sequence = last.get("sequence") if last is not None else None
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = None
    return {"round_sequence": sequence, "rounds": len(rounds)}


def gate_resolution(entry: dict) -> dict | None:
    """게이트 처분 선언의 해석 — 미선언·형식 불명이면 None(=미처분·공용 seam).

    읽을 수 없는 값은 처분이 아니다(fail-closed) — 손상된 한 줄이 릴리즈 차단을 **여는** 방향으로
    작동하면 그 게이트는 기계가 아니다. 라운드 좌표(`round_sequence`·`rounds`)도 같은 축이다:
    좌표 없는 선언은 어느 잔여를 처분했는지 확인할 수 없으므로 처분으로 인정하지 않는다.
    반환은 정규화된 `{kind, round_sequence, rounds, ts?}` 사본이다. 폐지 kind는 None으로
    읽혀 잔여 게이트를 그대로 차단한다.
    """
    raw = entry.get("resolution")
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind != RESOLUTION_PM_VERIFIED:
        return None
    sequence = raw.get("round_sequence", _MISSING_BINDING)
    count = raw.get("rounds")
    if sequence is _MISSING_BINDING:
        return None                      # 좌표 없는 선언 — 어느 잔여를 처분했는지 확인 불가.
    if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int)):
        return None
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    declared = {
        "kind": kind,
        "round_sequence": sequence,
        "rounds": count,
    }
    ts = raw.get("ts")
    if isinstance(ts, str) and ts.strip():
        declared["ts"] = ts.strip()
    return declared


def gate_resolution_is_stale(entry: dict, declared: dict) -> bool:
    """처분이 결속한 라운드 좌표가 **지금 장부와 다른가** (다르면 미처분 취급·공용 seam).

    선언 이후 새 라운드가 기록됐다면 그 잔여는 처분되지 않은 새 사실이다. 좌표가 뒤로 간 경우
    (장부 손편집)도 같은 처분이다 — 어느 쪽이든 선언이 가리키던 상태가 아니다."""
    current = gate_round_binding(entry)
    return any(current[key] != declared.get(key) for key in _GATE_RESOLUTION_BINDING_KEYS)


def _gate_disposition_problem(
    gate: str,
    entry: object,
    *,
    allow_pm_verified: bool = False,
    pm_verified_problem: Callable[[], str | None] | None = None,
) -> str | None:
    """게이트 하나의 처분 판정 — 차단 사유 1줄 (통과면 None).

    순서대로 본다: **항목 해석 가능성**(손상이면 잔여를 셀 수 없으니 차단) → 잔여 must_fix(0 이면
    비대상·suggestion 만 남은 게이트 포함·**미상은 대상**) → 처분 선언(미선언·좌표 stale 이면 차단)
    → `pm-verified` 발동 증거의 라이브 재검증. 폐지 kind는 `gate_resolution=None`이라
    미처분으로 fail-loud 한다."""
    corruption = gate_entry_corruption(entry)
    if corruption is not None:
        return f"  · {gate}: {corruption} — 잔여 must-fix 를 확인할 수 없어 차단합니다"
    if not gate_has_residual(entry):
        return None
    prefix = f"  · {gate}: 최종 라운드 must_fix {gate_residual_label(entry)}"
    declared = gate_resolution(entry)
    if declared is None:
        return f"{prefix} · 처분 선언 없음"
    label = f"{GATE_RESOLUTION_LABELS[declared['kind']]} 선언"
    if gate_resolution_is_stale(entry, declared):
        current = gate_round_binding(entry)
        return (f"{prefix} · {label} 이후 새 라운드가 기록됐습니다 "
                f"(선언 시점 #{declared['round_sequence']}/{declared['rounds']}건 ≠ 현재 "
                f"#{current['round_sequence']}/{current['rounds']}건) · 새 잔여로 다시 선언하세요")
    if not allow_pm_verified:
        return f"{prefix} · {label} — 이 장부에서는 pm-verified 처분을 허용하지 않습니다"
    if pm_verified_problem is None:
        return f"{prefix} · {label} — 발동 조건을 재검증할 수 없습니다(호출부 설정 누락)"
    problem = pm_verified_problem()
    return None if problem is None else f"{prefix} · {label} — 발동 조건 재검증 실패: {problem}"
