"""추가 리뷰어 예산 예약과 환불 (층: app)."""
from __future__ import annotations

from pm_engine.infra import clock
from pm_engine.app import completion_check
from pm_engine.app import release
from pm_engine.infra import review_rounds
from pm_engine.domain import reviewer_budget
from pm_engine.domain import reviewer_result
from pm_engine.infra import reviewer_target
import sys
import uuid


def _inherit_legacy_round_entry(home, round_records: dict, review_ticket: str) -> dict | None:
    """diff 대상 저장소 legacy 기록 파일의 리뷰 집계 티켓 항목을 PM 홈 기록 파일로 **1회 승계**한다 (없으면 None).

    기준 저장소 이동(diff_root → 소유 PM 홈)의 마이그레이션이다 — 런타임 대체 동작(두 기록 파일을 계속 합산)이
    아니라 원천 이관이라, 승계 뒤에는 PM 홈 기록 파일이 유일한 진실이다(리뷰 집계 티켓당 1회·호출부가
    즉시 저장한다). 승계 대상은 **PM 홈에 아직 그 리뷰 집계 티켓이 없을 때**뿐이라 이미 이관된 리뷰 집계 티켓의
    카운트를 옛 값으로 되돌리지 않는다. 두 기준 저장소가 같은 실행(수준 낮춤 대체 동작)은 대상이 아니다.
    """
    if review_ticket in round_records:
        return None
    legacy_path = review_rounds._legacy_round_record_path(home)
    if legacy_path == review_rounds._round_ledger_path(home):
        return None
    legacy = review_rounds._read_round_records_at(legacy_path)
    if review_ticket not in legacy:
        return None
    # 손상 필드는 저장과 같은 규칙으로 정규화한다. 이 호출부는 반환 즉시 PM 홈 기록 파일을
    # 저장하므로(위 docstring) 폐기 필드 고지도 여기서 낸다 — 승계 항목도 일반 항목과
    # 같은 "저장 후 1회 고지" 규칙을 받는다.
    entry, retired_ack = _review_ticket_entry_with_retirement(legacy, review_ticket)
    if retired_ack:
        review_rounds.warn_retired_ack_field(review_ticket, retired_ack)
    round_records[review_ticket] = entry
    return round_records[review_ticket]


def _save_round_records(home, round_records: dict) -> None:
    """리뷰 라운드 기록 파일과 shell 이 읽는 남은 일 표시를 fail-closed 순서로 원자 기록한다.

    tmp 이름에 pid+uuid 를 실어 동시 실행 간 고정 `.tmp` 충돌(카운트 유실·write 예외)을 없앤다
    os.replace 는 원자 rename — 독자는 옛 파일 또는 새 파일만 본다(부분기록 없음).
    확인·예약·저장의 원자성은 호출자가 `_round_record_lock()` 임계 구역으로 보장한다.

    두 파일은 한 rename 으로 묶을 수 없으므로 순서가 안전성이다: 새 기록이 blocked 면 표시를 먼저,
    clear 면 기록 파일을 먼저 쓴다. 어느 사이에서 죽어도 false-clear 대신 false-block 만 남는다. live-test
    record/check 도 같은 락과 board 공용 판정을 써 stale clear overwrite 를 막는다."""
    path = review_rounds._round_ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    problems = completion_check._unresolved_must_fix_data(home, round_records, path)
    live_test_flag = path.with_name("live-test.json")
    if problems:
        release._write_release_must_fix_marker(live_test_flag, problems)
    review_rounds.write_records(path, round_records)
    if not problems:
        release._write_release_must_fix_marker(live_test_flag, problems)


def _as_int(value: object) -> int:
    """기록 필드를 int 로 강제 (손상/누락 → 0·fail-soft)."""
    return review_rounds.as_int(value)


def _session_budget_state(round_records: dict) -> dict:
    """세션 호출 예산 절(`{id, started, spent}`)을 정규화해 돌려주고 기록에 넣는다.

    절이 없는 **옛 버전 기록**이나 손상 값은 새 세션 호출 예산(started 미기록·spent 0)으로 정규화한다 —
    `_review_ticket_entry` 와 같은 read→normalize→mutate→write 규약이라 호출부가 그 자리에서 고친 뒤
    저장하면 깨끗한 값이 기록된다.

    `id` 는 **버전 식별자**다. `--reset-session-budget` 리셋은 새 id 를 발급하므로, 리셋 전에 예약한 실행이
    나중에 환불하려 해도 버전이 달라 새 세션 호출 예산을 깎지 못한다(예산 우회 차단). id 가 없는
    옛 버전 절에는 여기서 하나 발급해 넣는다 — 이후 모든 예약/환불이 같은 좌표를 쓴다.

    손상(비-dict 값·옛 리뷰 집계 티켓 항목·비정수/음수 spent)은 **소비량을 0 으로 바꾸지 않는다** — 예산은
    승인(`--reset-session-budget`)으로만 열리는 값이라, 손상을 리셋으로 대접하면 기록 한 줄 편집이 승인을
    대신하게 된다. 대신 기록에 남은 라운드 산출 이력으로 소비량을 다시 세고(범위는 세션 호출 예산 시작 시각
    이후, 그것마저 못 믿으면 전체) stderr 한 줄로 무엇을 왜 재계산했는지 고지한다."""
    raw = round_records.get(reviewer_budget.SESSION_BUDGET_SECTION_KEY)
    corruption = reviewer_budget._session_budget_corruption_note(raw)
    state = raw if isinstance(raw, dict) and reviewer_budget._session_budget_frame_corruption(raw) is None else {}
    started = state.get("started")
    started = started if isinstance(started, str) and started else None
    if corruption is None:
        spent = max(0, _as_int(state.get("spent")))
    else:
        spent = reviewer_budget._recorded_round_outcomes(round_records, since=started)
        print(
            f"경고: 라운드 기록 {corruption} — 세션 호출 예산 소비를 라운드 산출 이력으로 **재계산**했습니다 "
            f"(spent={spent}). 손상은 승인을 대신하지 않습니다 — 세션 호출 예산을 새로 시작하려면 "
            "`--reset-session-budget`.",
            file=sys.stderr,
        )
    session_budget_id = state.get("id")
    normalized = {
        "id": session_budget_id if isinstance(session_budget_id, str) and session_budget_id else reviewer_budget._new_session_budget_id(),
        "started": started,
        "spent": spent,
    }
    round_records[reviewer_budget.SESSION_BUDGET_SECTION_KEY] = normalized
    return normalized


def _spend_session_budget_round(state: dict) -> None:
    """실 호출 1회를 세션 호출 예산에서 쓴다 — 첫 호출이 세션 호출 예산 시작 시각을 찍는다.

    세션 호출 예산 경계는 명시 리셋(`--reset-session-budget`)까지 누적한다. 세션 자동 감지는 하지 않는다 — 세션 식별이
    이 도구 밖 개념이라 명시 리셋이 단순하고 예측 가능하다."""
    if not state.get("started"):
        state["started"] = clock.now_utc()
    state["spent"] = max(0, _as_int(state.get("spent"))) + 1


def _refund_session_budget_round(state: dict, session_budget_id: str | None) -> bool:
    """예약과 **같은 버전**의 세션 호출 예산 소비만 되돌린다 (라운드 count 환불과 같은 조건).

    버전 확인이 없으면 `--reset-session-budget` 으로 새로 연 세션 호출 예산을 옛 실행의 실패가 깎아 예산이 조용히
    늘어난다(승인 1회로 예산 +N). 버전이 다르면 아무것도 하지 않고 False 를 돌려준다.

    소비가 0 으로 돌아가면 시작 시각도 지운다 — `started` 는 **첫 실 호출** 시각이라, 호출이 하나도
    없는 세션 호출 예산에 시각만 남으면 조회 표가 있지도 않은 세션 호출 예산을 진행 중으로 보여주고 다음 첫 호출이
    자기 시각을 못 찍는다(그 세션 호출 예산의 시작이 남의 실패 시각이 된다)."""
    if session_budget_id is None or state.get("id") != session_budget_id:
        return False
    state["spent"] = max(0, _as_int(state.get("spent")) - 1)
    if not state["spent"]:
        state["started"] = None
    return True


def _review_ticket_entry_with_retirement(round_records: dict, review_ticket: str) -> tuple[dict, int]:
    """`_review_ticket_entry` 와 같은 정규화 + 폐기 필드(`RETIRED_ACK_FIELD`) 감지 여부.

    반환 둘째 값은 이 호출이 방금 떨군 폐기 필드 원값(0=미검출)이다. 알림·영속 정책은
    호출부 소유다 — mutation/차단 경로(`_reserve_round_budget`)는 감지 즉시 저장 후 1회
    고지하고, 읽기 전용 조회(`render_rounds_report`)는 영속할 수 없는 고지를 하지 않는다."""
    return review_rounds.normalize_review_ticket_entry(
        round_records, review_ticket, reserved_keys=tuple(reviewer_budget._RESERVED_RECORD_KEYS),
    )


def _review_ticket_entry(round_records: dict, review_ticket: str) -> dict:
    """리뷰 집계 티켓 항목을 호출 레코드를 포함한 현 스키마로 정규화한다.

    항목이 없거나 손상(비-dict·비정수 필드)이면 0/0 으로 정규화해 저장한다 — 반환 dict 를 그 자리에서
    수정한 뒤 `_save_round_records(round_records)` 하면 깨끗한 값이 기록된다(read→normalize→mutate→write).

    `rounds`(라운드별 산출 이력)가 없는 **옛 버전 항목**은 빈 배열로 정규화된다 — 옛 기록을 그대로
    읽어 상한 판정을 이어가고, 새 산출은 그 뒤부터 쌓인다(마이그레이션 불요).

    예약 키(세션 호출 예산 절)를 리뷰 집계 티켓으로 정규화하려는 호출은 **fail-loud** 다 — 그 자리에 리뷰 집계 티켓 항목을 쓰면
    같은 항목을 세션 호출 예산이 덮어써 상한·예산이 둘 다 조용히 무력화된다. 정상 경로는 `_main` 이
    `--review-ticket` 을 이미 거른 뒤라 여기 오지 않는다(불변식을 주석이 아니라 기계로 지킨다).

    폐기 필드 감지 여부가 필요한 호출부는 `_review_ticket_entry_with_retirement` 를 대신 쓴다(이
    함수는 그 감지를 조용히 버린다 — 대부분의 호출부는 저장 여부를 스스로 정하지 않는
    읽기 경로라 알림 대상이 아니다)."""
    entry, _retired_ack = _review_ticket_entry_with_retirement(round_records, review_ticket)
    return entry


def _round_counts(entry: dict) -> tuple[int, int, int]:
    """이 리뷰 집계 티켓의 (전체, 판정, 미완) 호출 수; 옛 기록 count는 판정으로 보수 승계한다."""
    count = max(0, _as_int(entry.get("count")))
    records = [row for row in entry.get("records", []) if isinstance(row, dict)]
    # sequence/number는 identity일 뿐 count 좌표가 아니다. 환불로 sequence에 gap이 생겨도
    # 기록의 순서와 count만으로 집계 창을 잡아 조기 차단/예산 우회를 모두 막는다.
    logical_records = records[-min(len(records), count):] if count else []
    # 레코드가 없던 시절의 호출(count 만 있는 옛 버전 항목)은 판정으로 세는 쪽이 보수적이다.
    legacy_verdicts = max(0, count - len(logical_records))
    verdicts = legacy_verdicts + sum(
        bool(row.get("verdict")) for row in logical_records
    )
    return count, verdicts, max(0, count - verdicts)


def _reserve_round(
    entry: dict, record_id: str, *, wall_timeout_sec: int | None = None,
    target_rev: str | None = None,
) -> dict:
    """단조 sequence identity로 호출을 예약하고 레코드를 반환한다.

    **이 실행의 벽시계 백스톱을 `deadline` 으로 레코드에 새긴다** — 진행 중 예약이 언제까지
    살아 있을 수 있는지는 그 예약을 만든 실행의 timeout 이 정하는 값이지, 나중에 기록을 읽는
    호출자의 timeout 이 아니다(짧은 timeout 의 후속 호출이 긴 timeout 으로 도는 라운드를 stale
    로 바꾸면 마무리 상한을 넘겨 예약할 수 있다). 백스톱이 없는 실행은 만료 시각도 없다(null)."""
    return review_rounds.reserve_round(
        entry, record_id, wall_timeout_sec=wall_timeout_sec,
        target_rev=target_rev,
    )


def _refund_round(entry: dict, record_id: str) -> bool:
    """스폰 전 실패 예약만 제거한다; sequence는 재사용하지 않는다.

    반환값은 **실제로 환불했는지**다 — 같은 조건으로 세션 호출 예산도 되돌려야 하는데, 되돌릴 예약이
    없는데 spent 만 깎으면 두 예산의 소비가 갈린다."""
    return review_rounds.refund_round(entry, record_id)


def _round_outcome(result: dict, *, record: dict | None = None) -> dict:
    """리뷰 결과 → 라운드 산출(`{ts, started_at, target_rev, id, sequence, ...}`).

    verdict 는 이 실행이 돌려주는 **rc 판정**(0=통과·1=반려)이라 기록과 종료 코드가 갈리지 않는다.
    suggestions 는 응답에 suggestion 판별기가 아직 없어 null 로 시작한다 — 자리를 먼저 두어 파서가
    생기면 스키마 변경 없이 채워진다(파서 확장은 후속).

    `id`/`sequence`/`started_at`/`target_rev` 는 이 산출을 낸 **예약 레코드**의 좌표다. 같은 리뷰 집계 티켓에서
    여러 실행이 동시에 끝나면 append 순서나 완료 ts 만으로 어느 실행이 반려 뒤 시작했는지 확정할
    수 없어, 예약 identity 와 실제 검토 diff를 그대로 실어 라운드↔결과 연결을 잠근다(예약 레코드를
    못 찾은 기록 밖 직접 호출만 null)."""
    return {
        "ts": clock.now_utc(),
        "id": (record or {}).get("id"),
        "sequence": (record or {}).get("sequence"),
        "started_at": (record or {}).get("started_at"),
        "target_rev": (record or {}).get("target_rev"),
        "verdict": reviewer_budget.determine_exit_code(result),
        "must_fix": reviewer_budget._must_fix_count(result),
        "suggestions": None,
    }


def _append_round_outcome(entry: dict, outcome: dict) -> dict:
    """이미 만들어 둔 산출 레코드를 리뷰 집계 티켓 이력에 append 한다 (승인으로 비워지지 않는 값).

    산출 **계산**(응답 파싱·시각)은 호출부가 락 밖에서 끝낸다 — 임계 구역은 기록 파일 read-modify-write
    만 담당한다(파싱이 길어져도 다른 리뷰 집계 티켓 실행을 붙잡지 않는다)."""
    return review_rounds.append_round_outcome(entry, outcome)


# ── 마무리 조건 검사 (리뷰 라운드 기록 위의 판정) ────────────────────────────────
# 라운드 수만 보는 상한은 "라운드를 몇 번 썼나"만 막고 "닫히고 있나"는 묻지 않는다. 실측 두 경우가
# 그 공백이다: must_fix 가 줄지 않은 채 라운드만 늘거나(3→2→2), 라운드마다 새 지적이 늘어난다.
# 판정 입력은 **이미 있는 기록 필드**(`rounds[].must_fix`)뿐이고 LLM 판단은 0 이다.


def _recorded_must_fix_series(entry: dict) -> tuple[int | None, ...]:
    """예약 순번 순 must_fix 추이 — 기록된 라운드 산출만 (셀 근거가 없던 라운드는 None).

    나열 순서는 조회 표와 **같은 정렬**(`_ordered_round_outcomes`)이다 — append 순서는 완료
    순서라 동시 실행이 역순으로 끝나면 추이가 뒤바뀐다."""
    return review_rounds.recorded_must_fix_series(entry)


def _convergence_round_usage(
    entry: dict, *, wall_timeout_sec: int | None = None,
) -> tuple[int, int]:
    """(완료 산출 수, 미마감 예약 수) — 상한 판정과 차단 안내가 **같은 수**를 쓴다."""
    return review_rounds.convergence_round_usage(entry, wall_timeout_sec=wall_timeout_sec)


def _convergence_refusal(
    entry: dict, limit: int, *, wall_timeout_sec: int | None = None,
) -> str | None:
    """이번 라운드를 거부할 마무리 조건 사유 (통과면 None).

    세 조건을 이 순서로 본다:
      (b) **발산** — 직전 라운드 대비 must_fix 증가. 상한 도달을 기다리지 않는다(조기 차단).
      (a)(c) **상한** — 기록된 라운드 수 + **진행 중 예약**(벽시계 백스톱 안의 것만)이 상한 이상.
          마지막 must_fix 가 0 이 아니면(미해소·'미상' 포함) 사유를 나눠 표기한다. 미상을 해소로
          0 으로 바꾸지 않는 건 보수 방향이다 — 셀 수 없던 라운드를 '0건'으로 읽으면 발산 경우가 통과한다.
    """
    return review_rounds.convergence_refusal(entry, limit, wall_timeout_sec=wall_timeout_sec)


def _derive_review_ticket_from_ticket(args) -> reviewer_budget.ReviewTicketDerivation:
    """`--ticket` 실행의 리뷰 집계 티켓을 티켓으로 유도한다 (거부 사유가 있으면 그 안내를 돌려준다).

    유도는 **리뷰 실행면에서만** 한다 — 조회(`--rounds-report`)와 기록(`--resolve-review`) 면은
    같은 `--review-ticket` 을 각각 '한 리뷰 집계 티켓만 보기' 필터와 '무시 목록'으로 읽어, 유도값이 사용자가 주지
    않은 선택으로 둔갑한다(그 두 면은 호출도 예약도 하지 않아 여기서 닫을 구멍이 없다).

    명시 `--review-ticket` 이 항상 이기고, `--no-review-ticket` 은 유도를 끄는 명시 opt-out 이다. 리뷰
    실행면에서 둘을 함께 주면 한 실행이 "기록한다"와 "기록하지 않는다"를 동시에
    뜻하므로 거부한다. 조회·처분면에서는 둘 다 필터/무시 입력이라 충돌이 아니다.

    유도한 이름도 **예약 키 검사를 지난다** — `_review_ticket_entry` 는 예약 키를 hard 예외로 거부하므로,
    거르지 않으면 자유 문자열 티켓 하나가 예약 임계 구역에서 크래시로 나타난다(불변식을 주석이
    아니라 기계로 지킨다)."""
    opt_out = getattr(args, "no_review_ticket", False)
    # 조회·처분면을 **충돌 판정보다 먼저** 가른다. 이 두 명령은 실 리뷰를
    # 호출·예약하지 않아 `--review-ticket`은 필터/무시 입력, `--no-review-ticket`은 무시 경고 대상이다.
    # 여기서 닫아야 호출부의 각 명령 무시-플래그 처리까지 정상 도달한다.
    if args.rounds_report or args.resolve_review:
        return reviewer_budget.ReviewTicketDerivation()
    if args.review_ticket and opt_out:
        return reviewer_budget.ReviewTicketDerivation(refusal=reviewer_budget._REVIEW_TICKET_OPT_OUT_CONFLICT_GUIDANCE.format(review_ticket=args.review_ticket))
    if args.review_ticket or opt_out or not args.ticket:
        return reviewer_budget.ReviewTicketDerivation()
    reserved = reviewer_budget._reserved_review_ticket_error(args.ticket, flag="--ticket")
    if reserved is not None:
        return reviewer_budget.ReviewTicketDerivation(refusal=reserved)
    args.review_ticket = args.ticket
    template = (
        reviewer_budget._REVIEW_TICKET_DERIVED_DRY_RUN_NOTICE if getattr(args, "dry_run", False)
        else reviewer_budget._REVIEW_TICKET_DERIVED_NOTICE
    )
    return reviewer_budget.ReviewTicketDerivation(notice=template.format(review_ticket=args.review_ticket))


def _reserve_round_budget(
    home, args, conf: dict[str, str], *, wall_timeout_sec: int | None = None,
    target_rev: str | None = None,
) -> reviewer_budget.RoundBudget:
    """라운드 상한·세션 호출 예산을 한 임계 구역에서 확인하고 이번 호출을 예약한다.

    여기까지 왔으면 dry-run·빈-diff 거부를 모두 통과해 *실 호출*이 일어난다 — 그것들은 리뷰어를
    부르지 않아 라운드가 아니므로(카운트 제외) 이 앞의 조기 return 뒤에
    리뷰 집계 티켓을 둔다. 리뷰 집계 티켓이 해소된 실행에서만 리뷰 집계 티켓별 기록을 대조한다(리뷰 집계 티켓 없는 실행 = 상한
    대상 밖).

    리뷰 집계 티켓이 있는지는 이 함수보다 앞에서 정해진다 — `--ticket` 실행은 `_derive_review_ticket_from_ticket`
    이 리뷰 집계 티켓을 유도하고, selector 에 티켓이 없으면 실 호출 경로가 명시 `--no-review-ticket`을
    요구한다. 따라서 여기 리뷰 집계 티켓 없이 오는 실행은 모두 명시 opt-out 이며, loud 표기 뒤
    집계 없이 지난다(조용한 무기록은 반려 must-fix 를 릴리즈 차단 명령에서 지운다).

    MF-A(예약-후-환불): count 를 *호출 전에* +1 예약한다 — 타임아웃·비정상 종료도 프롬프트가 이미
    호출·과금됐을 수 있는데 성공시에만 세면 반복 타임아웃으로 상한을 무한 우회한다. 자식 프로세스가
    확실히 시작되지 않은 경우(스폰 실패·started=False, 그리고 예약 뒤 스폰 전 중단)만 환불한다.
    MF-B(원자성): 확인→예약→저장을 `_round_record_lock()` 한 임계 구역으로 묶어 동시 실행이 같은
    남은 슬롯을 통과 못 하게 한다. 초과면 리뷰어 호출 전에 거부(전용 rc·과금 없음).

    상한 기준은 셋이고 이 순서로 본다: **마무리 조건**(기록 must_fix 추이) → 판정/미완 라운드 상한 →
    세션 호출 예산. 마무리 기준을 먼저 보는 이유는 그쪽이 더 좁고 정지 사유가 구체적이기 때문이다.
    세션 호출 예산 승인(`--reset-session-budget`)은 **먼저 적용한 뒤** 남은 기준을 다시 본다 — 적용해 놓고 저장
    없이 되돌아가면 PM 이 적용한 승인이 조용히 사라진다.

    `wall_timeout_sec` 는 이 실행이 해소한 하네스 벽시계 백스톱이다 — 진행 중 예약 합산의 정리
    기준으로 그대로 넘긴다(그 시각을 넘긴 미마감 예약은 실행 중일 수 없다)."""
    if not args.review_ticket:
        # 세션 호출 예산 승인은 리뷰 집계 티켓 단위 예산 리셋이므로 대상이 없으면 무시한다.
        if args.reset_session_budget:
            print("경고: --reset-session-budget 은 --review-ticket 과 함께 써야 합니다 (리뷰 집계 티켓 단위 기록) — 무시.",
                  file=sys.stderr)
        # 호출이 확정된 구간이지만 **아직 스폰 전**이다(환경 준비가 남아 있다) — 그래서 문구는
        # 조건형이고("이 실행이 호출되면"), 확정 표기는 실행이 끝난 뒤 판정 블록의 리뷰 집계 티켓 줄이
        # 낸다. 그래도 여기서 한 번 말하는 이유는 침묵이 이 함정의 본체였기 때문이다(기록 0건·
        # 릴리즈 차단 미도달) — 호출 전에 멈출 기회를 PM 에게 준다.
        print(reviewer_budget._UNACCOUNTED_OPT_OUT_NOTICE, file=sys.stderr)
        return reviewer_budget.RoundBudget()

    incomplete_limit = reviewer_target._incomplete_round_limit(conf)
    session_budget = reviewer_target._wave_budget(conf)
    rounds_max = reviewer_target._review_rounds_max(conf)
    try:
        with review_rounds._round_record_lock(home):
            round_records = review_rounds._load_round_records(home)
            # 기준 저장소 이동 1회 승계 — legacy(diff 대상 저장소) 기록 파일의 차단 상태를 그대로 이관한다.
            # 승계분은 판정 *이전에* 저장한다: 차단으로 조기 return 해도 마이그레이션은
            # 남아야 다음 실행이 다시 승계하지 않는다(리뷰 집계 티켓당 1회).
            if _inherit_legacy_round_entry(home, round_records, args.review_ticket) is not None:
                _, legacy_verdicts, legacy_incomplete = _round_counts(
                    round_records[args.review_ticket]
                )
                print(
                    f"legacy 라운드 기록 승계: {review_rounds._legacy_round_record_path(home)} → "
                    f"{review_rounds._round_ledger_path(home)} · review-ticket={args.review_ticket} "
                    f"verdicts={legacy_verdicts} incomplete={legacy_incomplete}",
                    file=sys.stderr,
                )
                _save_round_records(home, round_records)
            entry, retired_ack = _review_ticket_entry_with_retirement(round_records, args.review_ticket)
            # F-002 — 폐기 필드 정리는 이 실행이 뒤에서 거부되든 승인되든 **여기서 즉시**
            # 저장하고 1회만 고지한다. 아래 각 차단 분기의 `approved or session_budget_repaired`
            # 조건에 태우면(그 값은 다른 손상 복구 전용) 이 정리만 저장을 놓쳐 거부가
            # 반복될 때마다 같은 경고가 되풀이된다 — 그 자체가 F-002 결함이었다.
            if retired_ack:
                _save_round_records(home, round_records)
                review_rounds.warn_retired_ack_field(args.review_ticket, retired_ack)
            # 손상 복구(재계산된 spent)는 거부되는 실행에서도 저장한다 — 안 그러면 손상값이
            # 남아 매 실행 같은 경고를 반복하고 기록 파일이 계속 거짓말을 한다. 저장 판정은 정규화와
            # **같은 술어**를 쓴다.
            session_budget_repaired = reviewer_budget._session_budget_corruption_note(round_records.get(reviewer_budget.SESSION_BUDGET_SECTION_KEY)) is not None
            session_budget_state = _session_budget_state(round_records)
            # 세션 호출 예산 승인 적용 (판정 전). **고지는 아직 하지 않는다** — 재개인지 거부인지는
            # 남은 상한을 다시 본 뒤에야 정해지고, 거부되는 실행이 "재개"를 말하면 rc 4 와
            # 어긋난 loud 오보가 된다.
            session_budget_reset = False
            if args.reset_session_budget:
                session_budget_state = reviewer_budget._reset_session_budget(round_records)
                session_budget_reset = True
            approved = session_budget_reset

            def announce(resumed: bool) -> None:
                for note in reviewer_budget._approval_notes(
                    session_budget_reset=session_budget_reset, session_budget=session_budget, resumed=resumed,
                ):
                    print(note, file=sys.stderr)

            # (1) 마무리 조건 — 기록 must_fix 추이로 "닫히고 있나"를 본다.
            convergence = _convergence_refusal(
                entry, rounds_max, wall_timeout_sec=wall_timeout_sec)
            if convergence is not None:
                if approved or session_budget_repaired:
                    _save_round_records(home, round_records)
                announce(resumed=False)
                series = _recorded_must_fix_series(entry)
                completed, inflight = _convergence_round_usage(
                    entry, wall_timeout_sec=wall_timeout_sec)
                print(reviewer_budget._CONVERGENCE_GUIDANCE.format(
                    review_ticket=args.review_ticket, reason=reviewer_budget._CONVERGENCE_REASONS[convergence],
                    rounds=completed + inflight, completed=completed, inflight=inflight,
                    limit=rounds_max,
                    series=reviewer_budget._format_must_fix_series(series),
                    knob=reviewer_budget.REVIEW_ROUNDS_MAX_KEY,
                    default=reviewer_result._conf_default_int(reviewer_budget.REVIEW_ROUNDS_MAX_KEY),
                    record_path=review_rounds._round_ledger_path(home)), file=sys.stderr)
                return reviewer_budget.RoundBudget(refused_rc=reviewer_budget.EXIT_ROUND_LIMIT_EXCEEDED)

            # (2) 미완 라운드 상한 (판정 없이 끝난 호출 기준). 호출 횟수만 세던 판정 상한은
            #     마무리 기준과 같은 범위라 제거됐다. 세션 호출 예산은 이와 독립 기준이라 한쪽 승인이
            #     다른 쪽을 열지 않는다.
            count = entry["count"]
            total, verdicts, incomplete = _round_counts(entry)
            if incomplete >= incomplete_limit:
                if approved or session_budget_repaired:
                    _save_round_records(home, round_records)      # 승인·손상 복구는 거부돼도 남긴다
                announce(resumed=False)
                print(reviewer_budget._ROUND_LIMIT_GUIDANCE.format(
                    review_ticket=args.review_ticket, unacked=total, verdicts=verdicts,
                    incomplete=incomplete,
                    incomplete_limit=incomplete_limit,
                    record_path=review_rounds._round_ledger_path(home),
                    count=count), file=sys.stderr)
                # 예약 없음 (호출 전 거부) — raw 자리도 아직 없다(이 검사가 그보다 앞이다).
                return reviewer_budget.RoundBudget(refused_rc=reviewer_budget.EXIT_ROUND_LIMIT_EXCEEDED)
            if session_budget_state["spent"] >= session_budget:
                if approved or session_budget_repaired:
                    _save_round_records(home, round_records)
                announce(resumed=False)
                print(reviewer_budget._SESSION_BUDGET_GUIDANCE.format(
                    review_ticket=args.review_ticket, spent=session_budget_state["spent"], budget=session_budget,
                    started=session_budget_state["started"] or "미기록",
                    record_path=review_rounds._round_ledger_path(home)), file=sys.stderr)
                return reviewer_budget.RoundBudget(refused_rc=reviewer_budget.EXIT_ROUND_LIMIT_EXCEEDED)
            announce(resumed=True)                  # 세 기준을 모두 통과한 뒤에만 "재개"
            round_id = uuid.uuid4().hex
            # 예약 sequence 는 **여기서** 잡는다 — 마감 시점에 레코드를 되찾아 읽으면 그 사이
            # 집계 창이 비워진 실행만 순번을 잃는다. 만료 시각도 같은 자리에서 새긴다 — 이 예약이
            # 언제까지 살아 있을 수 있는지는 **이 실행의** 백스톱이 정한다.
            record = _reserve_round(                                # 호출 전 라운드 예약
                entry, round_id, wall_timeout_sec=wall_timeout_sec,
                target_rev=target_rev,
            )
            _spend_session_budget_round(session_budget_state)                   # 같은 호출을 세션 호출 예산에서도 차감
            _save_round_records(home, round_records)
            return reviewer_budget.RoundBudget(
                review_ticket=args.review_ticket, round_id=round_id, sequence=record["sequence"],
                started_at=record["started_at"], target_rev=record["target_rev"],
                session_budget_id=session_budget_state["id"],                   # 환불은 이 버전에만 유효
            )
    except OSError as exc:
        # 락 획득/기록 파일 write 실패 — 상한을 확인하지 못한 채 호출하면 과금 검사가 무력화되므로
        # **호출 전에** 멈춘다(과금 0). 가장 흔한 원인은 동시 실행의 락 보유다(Windows
        # `msvcrt.locking` 은 재시도 소진 시 OSError·POSIX 는 블로킹이라 여기 안 온다).
        print(
            f"오류: 라운드 기록 임계 구역 진입 실패 ({type(exc).__name__}: {exc}) — 다른 "
            "리뷰 집계 티켓 실행이 기록 락을 보유 중일 수 있습니다. 잠시 후 다시 실행하세요 "
            f"(기록 파일: {review_rounds._round_ledger_path(home)}).",
            file=sys.stderr,
        )
        return reviewer_budget.RoundBudget(refused_rc=1)


def _refund_reserved_round(round_records: dict, reservation: reviewer_budget.RoundBudget) -> bool:
    """예약 하나를 라운드·세션 호출 예산에서 같은 조건으로 되돌린다 (호출이 확실히 없던 실행).

    호출부가 이미 `_round_record_lock()` 안에서 로드한 기록을 그 자리에서 고치고 저장은 호출부가
    한다 — 마감 경로는 같은 임계 구역에서 산출 기록까지 함께 쓰기 때문이다. 환불 조건이 한 군데라
    "환경 준비 실패로 되돌린 예약"과 "스폰 실패로 되돌린 예약"이 서로 다른 규칙을 갖지 않는다.

    세션 호출 예산은 **예약 시점 버전**만 깎는다: 그 사이 `--reset-session-budget` 으로 새 세션 호출 예산이 열렸으면 이 실패는 그
    예산과 무관하다(깎으면 승인 1회로 예산이 늘어난다). 라운드 count 를 되돌리지 못했으면(레코드가
    그 사이 정리된 경우) 나머지 예산도 건드리지 않는다 — 한쪽만 깎으면 예산들의 소비가 갈린다."""
    entry = _review_ticket_entry(round_records, reservation.review_ticket)
    if not _refund_round(entry, reservation.round_id):
        return False
    if not _refund_session_budget_round(_session_budget_state(round_records), reservation.session_budget_id):
        print(
            "경고: 예약 시점 세션 호출 예산이 이미 리셋돼 세션 호출 예산은 환불하지 않았습니다 "
            "(라운드 count 만 환불).",
            file=sys.stderr,
        )
    return True


def _release_round_reservation(home, reservation: reviewer_budget.RoundBudget, *, reason: str) -> bool:
    """호출 **전에** 끝난 실행의 예약을 그 자리에서 원자 환불한다 (락→로드→환불→저장).

    예약 뒤·스폰 전에 중단하는 구간의 소유자(`_PreSpawnReservation`)가 쓴다. 락과 환불 기계는 마감
    경로와 같은 것을 재사용하므로 동시 실행 직렬화와 버전 규칙이 두 경로에서 갈리지 않는다.

    락/저장 실패는 loud 경고만 남기고 삼킨다 — 이 실행의 rc(또는 전파할 예외)는 중단 사유가 소유하고,
    환불하지 못한 예약은 finished_at 없는 미완 레코드로 남아 다음 실행의 재시도 예산에서
    보수적으로 세어진다(기록이 실제보다 헐거워지는 방향으로는 틀리지 않는다).

    반환값은 **이번 호출이 실제로 되돌려 저장까지 했는지**다 — 정상 return 하는 호출부(마감 저장이
    실패한 no-spawn 경로)가 뒤따르는 안내 문구를 그 사실에 맞추는 데 쓴다. 되돌릴 예약이 없거나
    (이미 반영·다른 버전) 락/저장이 실패하면 False 다."""
    if not reservation.reserved:
        return False
    try:
        with review_rounds._round_record_lock(home):
            round_records = review_rounds._load_round_records(home)
            refunded = _refund_reserved_round(round_records, reservation)
            _save_round_records(home, round_records)
    except OSError as exc:
        print(
            f"경고: 라운드 예약 환불 실패 ({type(exc).__name__}: {exc}) — {reason}(으)로 호출 "
            "없이 중단한 실행의 예약이 기록에 남았습니다. 다음 실행의 미완 재시도 예산에서 "
            f"세어집니다 (기록 파일: {review_rounds._round_ledger_path(home)}).",
            file=sys.stderr,
        )
        return False
    if refunded:
        print(
            f"라운드 예약 환불: 리뷰 집계 티켓 {reservation.review_ticket} — {reason}(으)로 호출 없이 "
            "중단했습니다(상한·세션 호출 예산 미소진).",
            file=sys.stderr,
        )
    return refunded


class _PreSpawnReservation:
    """예약~스폰 사이 구간의 **단일 소유 함수** — 그 구간에서 끝난 실행의 예약을 한 번만 환불한다.

    예산 검사가 앞에 서면서 "예약은 잡혔는데 리뷰어는 아직 못 떴다"는 구간이 생겼다
    (리뷰어 환경/커맨드 준비). 이 구간에서 어떤 이유로 빠져나가든 호출은
    확실히 없으므로 예약은 되돌아가야 한다. 환불 조건을 예외 **종류**로 잡으면 등재되지 않은
    예외가 예약을 finished_at 없는 미완 레코드로 남겨, 호출도
    과금도 없던 실행이 다음 실행의 미완 재시도 예산을 깎는다 —
    `additional_reviewer.incomplete_rounds_max=1` 이면 다음 **정상** 호출이 곧바로 차단된다. 그래서
    조건은 종류가 아니라 **구간**이다: 여기서 나가는 모든 `BaseException`
    (`KeyboardInterrupt`·`SystemExit` 포함)이 환불을 부른다.

    환불은 하되 예외는 그대로 다시 던진다 — 예상 못 한 실패를 판정 rc 로 바꾸면 진단이 사라진다.

    소유권은 **실 스폰 경계**의 `hand_off()` 로 넘어간다 — 스폰할 *수도* 있는 함수의 입구가
    아니라, 인자·교체 지점 검증과 러너 준비(프로필 해소·워치독 셋업)가 모두 끝나 다음
    문장이 `Popen` 을 부르는 그 한 줄 앞이다(`run_review` → `_run_reviewer_ex` →
    `_watchdog_reviewer_run` → watchdog 워치독의 `on_spawn_attempt`). 그 뒤의 실패는 호출이 이미
    일어났을 수 있어 조건이 다르고, 마감 경로(`started` 판정)가 본다. `_settled` 는 한 번만
    서므로 여러 층이 같은 예외를 잡아도 이중 환불(상한이 조용히 늘어남)이 되지 않는다.

    이전은 **대칭 반납**을 갖는다(`reclaim_no_spawn()`). 러너가 `started=False` 로 돌아온 실행은
    자식이 확실히 없었다고 **판명**된 것이라(확정 기동 실패·경계 전 준비 실패), 스폰 직전에 넘긴
    소유권이 그 자리에서 되돌아온다 —
    그러지 않으면 그 뒤의 요약 출력·진단·마감이 죽었을 때(닫힌 stdout 파이프의 `BrokenPipeError`
    가 실측 근거다) 예약이 finished_at 없는 미완 레코드로 남아, 호출 0·과금 0 인 실행이
    `incomplete_limit=1` 경우에서 다음 **정상** 호출을 곧바로 막는다. 반납 조건은 판명된
    `started=False` 하나뿐이다 — 타임아웃·불확실 예외(started=True)는 이미 나갔을 수 있어
    그대로 마감 경로가 소비한다."""

    def __init__(self, home, budget: reviewer_budget.RoundBudget) -> None:
        self._home = home
        self.budget = budget
        self._settled = False

    def __enter__(self) -> "_PreSpawnReservation":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            self.release(reason=f"스폰 전 예외 {exc_type.__name__}")
        return False                                  # 예외는 삼키지 않는다

    def release(self, *, reason: str) -> bool:
        """아직 소유 중인 예약을 환불한다 (이미 환불·이전됐으면 no-op).

        반환값은 **이번 호출이 실제로 되돌렸는지**다 — 예외로 나가는 구간은 쓰지 않지만, 정상
        return 하는 마감 실패 경로는 뒤따르는 안내 문구("미완으로 남는다" vs "되돌렸다")를 그
        사실에 맞춰야 한다. no-op(이미 정산)·환불 실패는 둘 다 False 다."""
        if self._settled:
            return False
        self._settled = True
        return bool(_release_round_reservation(self._home, self.budget, reason=reason))

    def hand_off(self) -> None:
        """스폰 구간으로 소유권 이전 — 이후 실패는 이 함수가 아니라 마감 경로가 판정한다."""
        self._settled = True

    def reclaim_no_spawn(self) -> None:
        """스폰이 **없었다고 판명된** 뒤 소유권을 되찾는다 (`hand_off` 의 대칭).

        호출 자격은 러너가 결론적으로 `started=False` 로 돌아온 직후 한 자리뿐이다 — 그 시점의
        사실은 "자식이 뜰 수도 있었다"가 아니라 "뜬 적 없다"이므로, 스폰 직전에 넘긴 권리가 그대로
        되돌아온다. 되찾은 뒤의 실패(요약·진단·마감)는 다시 이 함수가 한 번 환불한다."""
        self._settled = False

    def settle_refunded(self) -> None:
        """마감 경로가 이 예약을 이미 환불했음을 기록한다 — 같은 예약을 두 번 되돌리지 않는다.

        `hand_off` 와 상태 변화는 같지만 사실이 다르다: 저쪽은 "스폰 구간이 판정한다", 이쪽은
        "환불이 끝났다"이다. 되돌린 뒤 다시 되돌리는 건 상한이 조용히 늘어나는 방향이라 이름으로
        구분해 둔다."""
        self._settled = True
