#!/usr/bin/env python3
"""리뷰 라운드 기록 파일 공용 모듈.

추가 리뷰어(`additional_reviewer.py`) 리뷰 라운드 기록 파일의 리뷰 집계 티켓 항목/예약/산출/마무리 판정 스키마를
소유한다(board 는 같은 기록 파일을 읽기만 한다). 이 모듈은 기록 파일을 쓰고 읽는 쪽이 갈리면 안 되는
다음 규칙만 소유한다.

* JSON 기록 파일 read + PID/UUID 임시 파일을 이용한 원자 write (교체 자체는 공용 `fs` 함수 —
  플랫폼별 rename 의미 차이를 이 모듈이 복제하지 않는다)
* 리뷰 집계 티켓 항목 정규화와 단조 sequence 예약/스폰 전 환불
* 예약 순서 기준 must-fix 추이, 진행 중 예약을 포함한 상한, 발산 조기 차단
* 리뷰 집계 티켓 처분 선언(`resolution`) 판정, 마지막 라운드 판정과 남은 must-fix

파일락의 경로와 임계 구역은 호출자가 소유한다. 저장 파일과 부가 기록(live-test/세션 호출 예산)은 호출
도구가 정한다. 호출자는 반드시 같은 기록 파일 옆의 파일락 안에서 read-modify-write를 수행한다.
"""

from __future__ import annotations

from typing import Iterator
import contextlib

import datetime
import json
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from pm_engine.domain import reviewer_budget
from pm_engine.infra import fs


CONVERGENCE_DIVERGING = "diverging"
CONVERGENCE_CAP_UNRESOLVED = "cap-unresolved"
CONVERGENCE_CAP_REACHED = "cap-reached"

# 폐지된 라운드 연장 승인이 기록 파일에 남긴 필드. 스키마에서 뺐고 **값을 승계하지도 않는다** —
# 승인 수위만큼 집계 창을 잘라 판정 수를 줄이던 값이라, 남아 있으면 그 리뷰 집계 티켓의 집계가 전체
# 레코드 기준과 달라진다. 버리는 사실은 리뷰 집계 티켓·값과 함께 loud 로 알린다(정규화가 키를 떨구므로
# 기록 파일이 다시 쓰이면 알릴 대상이 없어 자연히 1회다 — 별도 상태 파일 없음).
RETIRED_ACK_FIELD = "acked_through"

# 기계 확인(pm-review-confirmation-v1) 증거로 여는 리뷰 집계 티켓 처분. 상한·쿼터 대신
# 증거를 요구한다(`review_state.pm_verified_evidence_problem` 이
# 선언·완료 재검증 공용으로 판정한다). 이 모듈은 표기 문구만 소유한다.
RESOLUTION_PM_VERIFIED = "pm-verified"


def utc_now_iso() -> str:
    """UTC ISO-8601 시각 문자열."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def as_int(value: object) -> int:
    """기록 정수 필드 정규화(손상/누락은 0)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def read_records(
    path: Path | str,
    *,
    warning_sink: Callable[[str], None] | None = None,
) -> dict:
    """기록 파일을 읽는다. 부재·손상·비매핑 최상위는 빈 기록으로 fail-soft 한다.

    파일 부재는 첫 실행의 정상 경우라 조용하다. 반면 읽기 권한/IO 실패·깨진 UTF-8·빈 파일을
    포함한 JSON 파싱 실패는 호출자가 ``warning_sink``를 주면 loud 진단을 남긴다. 어느 경우든
    손상된 바이트가 라운드 호출을 예외로 막지 않도록 빈 기록으로 복구한다.
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


def write_records(path: Path | str, records: dict) -> None:
    """기록 JSON을 같은 디렉터리의 고유 임시 파일을 거쳐 원자 교체한다.

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
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n"
        )
        fs.atomic_replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def warn_retired_ack_field(review_ticket: str, value: int) -> None:
    """폐지 필드를 버린다는 사실을 그 리뷰 집계 티켓·값과 함께 알린다(승계 없음)."""
    print(
        f"경고: 라운드 기록의 폐지 필드 `{RETIRED_ACK_FIELD}` 를 버립니다 — 리뷰 집계 티켓 {review_ticket} · "
        f"{RETIRED_ACK_FIELD}={value}. 폐지된 라운드 연장 승인이 남긴 값이라 승계하지 "
        "않으므로, 이 리뷰 집계 티켓의 집계는 이제 전체 레코드 기준입니다. 기록 파일이 다시 쓰이면 이 "
        "안내는 사라집니다.",
        file=sys.stderr,
    )


def normalize_review_ticket_entry(
    round_records: dict,
    review_ticket: str,
    *,
    reserved_keys: Sequence[str] = (),
) -> tuple[dict, int]:
    """리뷰 집계 티켓 항목을 external/internal 공용 스키마로 정규화해 기록에 넣고, 폐기 필드
    (`RETIRED_ACK_FIELD`) 감지 여부를 호출부에 돌려준다 — `(entry, retired_ack)`.

    폐지 필드는 여기서 떨어진다(승계 없음). `retired_ack` 는 버린 원값(0=미검출)이다 —
    **알림·영속은 이 함수가 하지 않는다**: 순수 조회(report/preview)는 정규화 결과를 저장할
    수 없어 '1회' 고지를 지킬 수 없고, mutation/차단 경로는 저장 시점을 스스로 정한다(거부
    직전에라도 저장해야 알림이 실제로 1회가 된다). 호출부가 `retired_ack` 를 보고 자기
    계약에 맞는 저장·고지 정책을 고른다(`warn_retired_ack_field` 재사용). 폐지 필드를 떨구는 자리를
    정규화로 둬야 조회·기록·legacy 승계가 한 규칙이다(승계 항목은 기록 파일 스캔이 아니라 이
    경로로 들어온다)."""
    if review_ticket in reserved_keys:
        raise ValueError(
            f"라운드 기록 예약 키를 리뷰 집계 티켓으로 쓸 수 없습니다: {review_ticket!r} "
            f"(예약 키: {', '.join(sorted(reserved_keys))})"
        )
    entry = round_records.get(review_ticket)
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
    round_records[review_ticket] = normalized
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
    """라운드 산출의 **예약 순번** 정렬 키 — 이 판정의 공용 함수 (사본 금지).

    append 순서는 *완료* 순서라 라운드 번호가 아니다(동시 라운드가 역순으로 끝나면 뒤바뀐다).
    그래서 나열은 예약 순번(`sequence`)을 따른다.

    **순번이 없는 옛 버전 기록은 새 기록보다 앞(오래된 쪽)이다.** 뒤에 두면 업그레이드 후 새 통과가
    쌓여도 옛 반려가 영원히 '최신'이 되어, 이미 닫힌 리뷰 집계 티켓이 계속 활성으로 읽히고(회귀 수준 낮춤)
    확인 전용 라운드 자격도 영구히 열린다. 구기록은 정의상 순번 도입 *이전* 이라 앞이 맞다 —
    같은 키의 안정 정렬로 구기록끼리는 append 순서를 보존한다.

    이 판정을 쓰는 곳이 여럿이라(회귀 수준 낮춤·추가 리뷰어의 조회 표·마무리 추이·확인 전용 라운드
    자격) 이 모듈 한 곳에 둔다 — 사본을 만들면 그 사본이 다시 뒤집힌 '최신'을 답한다.
    """
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


# ── 기록 파일 위치와 리뷰 집계 티켓 판정 (회귀 수준 낮춤·릴리즈 차단·조회 표 공용) ─────────────
REVIEW_ROUNDS_RECORD_NAME = "review_rounds.json"
_REVIEW_RECORD_SESSION_BUDGET_KEY = reviewer_budget.SESSION_BUDGET_SECTION_KEY  # 리뷰 집계 티켓이 아닌 예약 키


def _review_rounds_records(home) -> Path:
    """추가 리뷰 라운드 기록 파일 경로 — `home.local_dir` 파생(테스트 격리 추종)."""
    return home.local_dir / REVIEW_ROUNDS_RECORD_NAME


def review_ticket_last_round(entry: dict) -> dict | None:
    """리뷰 집계 티켓의 **마지막 라운드 산출** — 기록이 없으면 None (공용 함수·사본 금지).

    "마지막이 어느 라운드인가"를 이 한 곳이 정한다 — append 순서가 아니라 예약 순번이다
    (`round_outcome_order_key`). 이 판정을 세 곳이 쓴다: 회귀 수준 낮춤의 최신 판정, 릴리즈 통과 조건의
    남은 must-fix, 처분 선언의 근거 리뷰 집계 티켓 통과 여부. 사본을 두면 그 사본이 서로 다른 라운드를
    '최신'이라고 답한다. `rounds` 가 리스트가 아닌 손상 항목은 None 이다 — 이 함수는 판정을
    돌려줄 뿐이라 손상의 **처분**은 호출부가 정한다(회귀 수준 낮춤은 fail-open·릴리즈는 fail-closed·
    `review_ticket_entry_corruption`)."""
    rounds = entry.get("rounds")
    if not isinstance(rounds, list):
        return None
    rounds = [row for row in rounds if isinstance(row, dict)]
    if not rounds:
        return None
    return sorted(rounds, key=round_outcome_order_key)[-1]


def _last_round_verdict(entry: dict) -> int | None:
    """리뷰 집계 티켓의 **마지막 라운드** 판정 rc (기록 없음/손상이면 None).

    나열 순서는 append 순서가 아니라 **예약 순번**이다(공용 함수 `review_ticket_last_round` —
    additional_reviewer 조회 표와 같은 한 규칙)."""
    outcome = review_ticket_last_round(entry)
    verdict = outcome.get("verdict") if outcome is not None else None
    return verdict if isinstance(verdict, int) and not isinstance(verdict, bool) else None


def review_ticket_residual_must_fix(entry: dict) -> int | None:
    """마지막 라운드가 남긴 must-fix 수 — 3-상태 (공용 함수).

    반환은 `0`(남은 것 없음) · `N>0`(N 건) · **`None`(미상)** 이다. 미상은 "호출은 됐는데 판정이
    무효했다"(타임아웃·오염·섹션 부재로 셀 근거가 없던 라운드)이고, 그건 "남은 것이 없다"가 아니라
    **확인하지 못했다**이다 — 0 으로 바꾸면 판정 무효 라운드로 끝난 리뷰 집계 티켓에서 릴리즈 차단이
    조용히 풀린다(실제 기록 파일 실례). 형제 함수(additional_reviewer 의 마무리 판정)도 '미상'을 해소로 세지
    않는 같은 규칙이다.

    다만 **통과(rc 0)로 끝난 라운드의 미상은 비대상(0)** 이다 — 그쪽은 판정이 났고 must-fix 섹션이
    없었을 뿐이라 반려 사실이 없다. suggestion 은 어느 경우에도 이 수에 들어오지 않는다(이월 허용).
    """
    outcome = review_ticket_last_round(entry)
    if outcome is None:
        return 0
    value = outcome.get("must_fix")
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return 0 if _last_round_verdict(entry) == 0 else None


def review_ticket_has_residual(entry: dict) -> bool:
    """처분이 필요한 남은 must-fix 가 있는가 — N>0 **또는 미상** (공용 함수).

    릴리즈 차단과 처분 선언(`--resolve-review`)이 이 한 술어를 공유한다. 두 곳이 갈리면 차단은
    되는데 선언은 거부되는 데드락이 난다(미상 리뷰 집계 티켓이 정확히 그 경우다)."""
    residual = review_ticket_residual_must_fix(entry)
    return residual is None or residual > 0


def review_ticket_residual_label(entry: dict) -> str:
    """남은 must-fix 표기 — `N건` / `미상(판정 무효 라운드)` (차단 사유·선언 응답 공용)."""
    residual = review_ticket_residual_must_fix(entry)
    return "미상(판정 무효 라운드)" if residual is None else f"{residual}건"


# ── 리뷰 집계 티켓 처분 선언 (기록 파일 `resolution` 절) ────────────────────────────────
# 라운드 상한으로 종결된 리뷰 집계 티켓의 **남은 must-fix 를 어떻게 소화했는가**를 기록 파일에 남기는 절이다.
# 쓰는 쪽은 additional_reviewer(`--resolve-review`)지만 **읽는 규칙은 여기 하나**다 — 릴리즈 차단과
# 조회 표가 각자 해석하면 같은 기록 파일을 놓고 "처분됐다/아니다"가 갈린다.
# 기계 확인 증거로 여는 리뷰 집계 티켓 처분(`RESOLUTION_PM_VERIFIED`)은 기록 파일마다 `allow_pm_verified` 명시
# 시에만 인정된다(추가 리뷰어 release 경로가 스코프 인자로 채널 격리해 허용 · 기본 호출은 허용하지 않는다).

# 처분 표기 어휘 — 선언 응답(additional_reviewer)과 차단 사유(여기)가 같은 말을 쓰게.
REVIEW_TICKET_RESOLUTION_LABELS: dict[str, str] = {
    RESOLUTION_PM_VERIFIED: "pm-verified(PM 기계 확인 해소·reviewer 재투입 없음)",
}

# 처분이 연결되는 **라운드 좌표** 필드 — 선언 시점의 마지막 라운드 순번과 산출 수.
_REVIEW_TICKET_RESOLUTION_BINDING_KEYS: tuple[str, ...] = ("round_sequence", "rounds")
_MISSING_BINDING = object()   # `round_sequence` 키 부재와 명시 null(정상 값)을 구분하는 표시.


def review_ticket_entry_corruption(entry: object) -> str | None:
    """리뷰 집계 티켓 항목이 **해석 가능한 구성**인지 — 아니면 사유 (정상이면 None·공용 함수).

    "존재하는데 읽을 수 없음"은 "남은 것 없음"이 아니다. 손상 구성마다 조용한 통과 조각이 생기는 것이
    이 판정의 실패 모드였다 — 비-dict 항목은 순회에서 건너뛰고, 문자열 `rounds` 는 '기록 없음'으로,
    정수 `rounds` 는 예외로 갈렸다(각각 통과·통과·중단). 셋을 한 술어로 모아 호출부가 **한 처분**
    (릴리즈 차단)으로 모은다."""
    if not isinstance(entry, dict):
        return f"리뷰 집계 티켓 항목 형식 오류({type(entry).__name__}) — 해석 불가"
    rounds = entry.get("rounds")
    if rounds is not None and not isinstance(rounds, list):
        return f"`rounds` 형식 오류({type(rounds).__name__}) — 라운드 이력 해석 불가"
    if isinstance(rounds, list) and any(not isinstance(row, dict) for row in rounds):
        return "`rounds` 에 매핑이 아닌 원소 — 라운드 이력 해석 불가"
    resolution = entry.get("resolution")
    if resolution is not None and not isinstance(resolution, dict):
        return f"`resolution` 형식 오류({type(resolution).__name__}) — 처분 선언 해석 불가"
    return None


def review_ticket_round_binding(entry: dict) -> dict:
    """리뷰 집계 티켓의 **현재 라운드 좌표** — `{round_sequence, rounds}` (선언·검증 공용 함수).

    처분은 "그때 남은 must-fix"에 대한 선언이다. 좌표 없이 리뷰 집계 티켓 단위로만 남기면 처분 뒤에 새 반려
    라운드가 와도 옛 선언이 남은 must-fix 를 계속 지운다(처분의 무기한 유효화). 좌표는 둘을 함께 본다 —
    마지막 라운드의 예약 순번(정렬 좌표)과 산출 수. 순번 없는 옛 버전 산출만 있는 기록에서도
    산출 수가 증가를 잡는다."""
    rounds = entry.get("rounds")
    rounds = [row for row in rounds if isinstance(row, dict)] if isinstance(rounds, list) else []
    last = review_ticket_last_round(entry)
    sequence = last.get("sequence") if last is not None else None
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = None
    return {"round_sequence": sequence, "rounds": len(rounds)}


def review_ticket_resolution(entry: dict) -> dict | None:
    """리뷰 집계 티켓 처분 선언의 해석 — 미선언·형식 불명이면 None(=미처분·공용 함수).

    읽을 수 없는 값은 처분이 아니다(fail-closed) — 손상된 한 줄이 릴리즈 차단을 **여는** 방향으로
    작동하면 그 검사는 기계가 아니다. 라운드 좌표(`round_sequence`·`rounds`)도 같은 기준이다:
    좌표 없는 선언은 어느 남은 must-fix 를 처분했는지 확인할 수 없으므로 처분으로 인정하지 않는다.
    반환은 정규화된 `{kind, round_sequence, rounds, ts?}` 사본이다. 폐지 kind는 None으로
    읽혀 남은 must-fix 가 있는 리뷰 집계 티켓을 그대로 차단한다.
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
        return None                      # 좌표 없는 선언 — 어느 남은 must-fix 를 처분했는지 확인 불가.
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


def review_ticket_resolution_is_stale(entry: dict, declared: dict) -> bool:
    """처분이 연결된 라운드 좌표가 **지금 기록과 다른가** (다르면 미처분 취급·공용 함수).

    선언 이후 새 라운드가 기록됐다면 그 남은 must-fix 는 처분되지 않은 새 사실이다. 좌표가 뒤로 간 경우
    (기록 파일 손편집)도 같은 처분이다 — 어느 쪽이든 선언이 가리키던 상태가 아니다."""
    current = review_ticket_round_binding(entry)
    return any(current[key] != declared.get(key) for key in _REVIEW_TICKET_RESOLUTION_BINDING_KEYS)


def _review_ticket_disposition_problem(
    review_ticket: str,
    entry: object,
    *,
    allow_pm_verified: bool = False,
    pm_verified_problem: Callable[[], str | None] | None = None,
) -> str | None:
    """리뷰 집계 티켓 하나의 처분 판정 — 차단 사유 1줄 (통과면 None).

    순서대로 본다: **항목 해석 가능성**(손상이면 남은 must-fix 를 셀 수 없으니 차단) → 남은 must_fix(0 이면
    비대상·suggestion 만 남은 리뷰 집계 티켓 포함·**미상은 대상**) → 처분 선언(미선언·좌표 stale 이면 차단)
    → `pm-verified` 발동 증거의 라이브 재검증. 폐지 kind는 `review_ticket_resolution=None`이라
    미처분으로 fail-loud 한다."""
    corruption = review_ticket_entry_corruption(entry)
    if corruption is not None:
        return f"  · {review_ticket}: {corruption} — 남은 must-fix 를 확인할 수 없어 차단합니다"
    if not review_ticket_has_residual(entry):
        return None
    prefix = f"  · {review_ticket}: 최종 라운드 must_fix {review_ticket_residual_label(entry)}"
    declared = review_ticket_resolution(entry)
    if declared is None:
        return f"{prefix} · 처분 선언 없음"
    label = f"{REVIEW_TICKET_RESOLUTION_LABELS[declared['kind']]} 선언"
    if review_ticket_resolution_is_stale(entry, declared):
        current = review_ticket_round_binding(entry)
        return (f"{prefix} · {label} 이후 새 라운드가 기록됐습니다 "
                f"(선언 시점 #{declared['round_sequence']}/{declared['rounds']}건 ≠ 현재 "
                f"#{current['round_sequence']}/{current['rounds']}건) · 새 남은 지적으로 다시 선언하세요")
    if not allow_pm_verified:
        return f"{prefix} · {label} — 이 기록에서는 pm-verified 처분을 허용하지 않습니다"
    if pm_verified_problem is None:
        return f"{prefix} · {label} — 발동 조건을 재검증할 수 없습니다(호출부 설정 누락)"
    problem = pm_verified_problem()
    return None if problem is None else f"{prefix} · {label} — 발동 조건 재검증 실패: {problem}"


# ── 라운드 상한 기록 ─────────────
# 추가 리뷰어 호출은 유료 호출이라 라운드가 무한정 이어지면 비용이 쌓인다(PM 10차 실측: 한
# 리뷰 집계 티켓 클러스터 25라운드). PM 자의 라운드 집계를 기계 기록으로 대체한다
# ([[mechanize-dont-instruct-llm]]): `--review-ticket <T-NNNN>` 별로 실 호출 횟수(count)와 라운드별 산출을
# per-clone·git-ignored 기록 파일에 기록하고, 상한을 넘기면 실행 전에 거부한다. 폐지된 라운드 연장
# 승인이 남긴 필드는 정규화가 승계 없이 떨어뜨린다(`RETIRED_ACK_FIELD` — 그 사실은
# 리뷰 집계 티켓·값과 함께 한 번 알린다). 기록 파일은 세션/클론 로컬 현상이라
# `.project_manager/.local/`(regression/live-test sidecar 와 같은 위치·board 상태 아님)에 둔다. 경로는
# 호출부가 넘긴 Home의 PM 소유자에서 기록 파일 경로를 해소한다.
# 한다(_tickets_dir 와 같은 구조). 손상 기록 파일은 빈 기록으로 fail-soft(회귀해소·regression flag 와 같은 구조).
#
# 기록 파일은 두 부분을 함께 싣는다:
#   · 리뷰 집계 티켓 부분 — 최상위 키가 리뷰 집계 티켓 이름이고 값이 `_review_ticket_entry` 스키마다. 상한 집계용
#     `records`(예약/마감 레코드) 옆에 **라운드별 산출** `rounds`(판정 rc·결함 수)를 append 한다.
#     `rounds` 는 지워지지 않는 이력이라 "그 라운드가 실결함을 냈는가"를 나중에도 기계로 확인할 수
#     있고, 마무리 조건 검사(`_convergence_refusal`)의 판정 입력이기도 하다. 리뷰 집계 티켓 종결 시점의
#     **처분 선언**(`resolution` — `--resolve-review`)도 같은 항목에 실린다. 처분 선언은 릴리즈
#     차단(`board.py live-test record`)이 읽는 절이고,
#     그 값의 해석은 `review_ticket_resolution` 이 소유한다(여기선 구조만 보존).
#   · 세션 호출 예산 부분 — 예약 키(`SESSION_BUDGET_SECTION_KEY`) 하나에 {started, spent} 를 둔다. 리뷰 집계 티켓 상한만
#     있으면 비용이 티켓 수 × 상한으로 확장되므로 전 리뷰 집계 티켓 합계를 이 예산이 묶는다. 두 부분이 한
#     dict 를 공유하므로 **예약 키를 리뷰 집계 티켓 이름으로 쓰는 것은 기계로 막는다**(`_reserved_review_ticket_error`
#     가 `--review-ticket` 을 거르고 `_review_ticket_entry` 가 그 키를 fail-loud 로 거부·집계 순회는 건너뛴다).
# 두 부분의 적용 범위는 같다 — 기록을 타는 실행은 리뷰 집계 티켓이 해소된 실행(명시 `--review-ticket` 또는 `--ticket`
# 자동 유도)뿐이라 세션 호출 예산도 그 라운드만 센다. 리뷰 집계 티켓이 없는 실행(selector 에 티켓이 없거나 명시
# `--no-review-ticket` opt-out)은 기록 밖이고 어느 예산도 쓰지 않으며, 그 사실을 loud 로 표기한다.


def _round_record_path(home) -> Path:
    """리뷰 라운드 기록 파일 경로 — 해소된 **소유 PM 홈**의 `.local/json`.

    Home.local_dir는 호출부가 확정한 PM 소유자의 기록 파일 디렉터리다.
    코드 트리와 PM 소유자가 다른 실행에서도 같은 리뷰 집계 티켓 예산을 공유한다.

    diff 대상 저장소(diff_root)에 매달면 **같은 리뷰 집계 티켓의 과금 상한이 diff_root 가 바뀔 때마다 조용히
    리셋된다** — 새로 판 슬롯에서 같은 `--review-ticket T-NNNN` 을 돌리면
    count 가 0 부터 다시 세어져 상한이 무력화된다. 라운드는 이미 `--review-ticket` 키로 분리되므로 슬롯별
    기록 파일 분리가 주는 추가 격리는 없다(옛 "슬롯별 리뷰 집계 티켓 수명 분리" 근거 대체).
    """
    return (
        (home.local_dir.parent.parent) / ".project_manager" / ".local" / "review_rounds.json"
    )


def _legacy_round_record_path(home) -> Path:
    """옛 규칙(diff 대상 저장소)의 리뷰 라운드 기록 파일 경로 — 1회 승계(backfill)의 입력.

    기록 위치를 소유 PM 홈으로 옮기기 전의 실행들은 이 경로에 카운트를 쌓아 뒀다. 그 중에는 상한을
    이미 넘겨 **차단 중인 리뷰 집계 티켓**(rc 4·ack 대기)이 있고, 새 기록 위치에서 0 부터 다시 세면
    그 차단이 무통보로 열린다.
    """
    return home.root / ".project_manager" / ".local" / "review_rounds.json"


def _read_round_records_at(path: Path) -> dict:
    """지정 경로의 리뷰 라운드 기록 파일을 읽는다 — 없거나 손상 시 빈 dict(fail-soft)."""
    return read_records(path)


def _load_round_records(home) -> dict:
    """리뷰 라운드 기록 파일(리뷰 집계 티켓→{count, records, rounds})을 읽는다 — 없거나 손상 시 빈 dict(fail-soft)."""
    return _read_round_records_at(_round_record_path(home))


def _restrict_to_owner(path: Path) -> None:
    """호출 산출물을 소유자 전용 접근으로 제한한다 (플랫폼 수단은 공용 함수 소유).

    `os.chmod(0o600/0o700)` 을 직접 부르지 않는 이유는 그 호출이 **Windows 에서 아무 제한도
    걸지 않기 때문**이다(실측 `S_IMODE`=0o666). opencode transport 샌드박스에는 검토 대상 diff
    원문이 들어가므로 "다른 사용자에게 읽히지 않는다" 는 이 도구의 보안 경계다 —
    수단(퍼미션/ACL)은 `fs` 가 소유하고 여기서는 보장만 요구한다.
    """
    fs.restrict_to_owner(path)


def _round_record_lock_path(home) -> Path:
    """리뷰 라운드 기록 파일 배타락 파일 — 기록 파일 **옆** `lock`(기록 파일 경로에서 파생).

    락 파일 위치를 기록 파일 경로에서 파생한다 — 락과 기록 파일이 각자 기록 위치를 계산하면 그 규칙이 갈릴 때 서로 다른
    파일을 잠근 두 실행이 같은 기록 파일을 동시에 read-modify-write 한다(상한 우회 창).
    """
    return _round_record_path(home).with_name("review_rounds.lock")


@contextlib.contextmanager
def _round_record_lock(home) -> Iterator[None]:
    """리뷰 라운드 기록 파일 read-modify-write 를 직렬화하는 OS 파일락.

    확인(상한 대조)→예약(count+1)→저장을 하나의 임계 구역으로 묶어, 동시 실행 2개가 같은 남은
    슬롯을 통과해 상한을 우회하는 것을 막는다. 프로세스가 죽으면 OS 가 락을 자동 해제(stale-lock
    없음). **재진입 금지**(flock 관례·board_lock 과 같은 구조) — 예약과 환불은 *각자 독립* 락 구간이다
    (중첩 아님).

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 대체 동작)는 공용 `fs` 함수가 소유하고 이
    도구는 *어느 파일에 거는지*(경로 규약)만 정한다.
    """
    with fs.exclusive_file_lock(_round_record_lock_path(home)):
        yield
