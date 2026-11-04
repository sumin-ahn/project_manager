"""추가 리뷰 라운드 조회와 처분 (층: cli)."""
from __future__ import annotations

from pathlib import Path
import argparse
from pm_engine.infra import clock
from pm_engine.app import completion_check
from pm_engine.infra import conf_store
from pm_engine.infra import pm_paths
from pm_engine.infra import review_rounds
from pm_engine.domain import reviewer_budget
from pm_engine.app import reviewer_reservation
from pm_engine.domain import reviewer_result
from pm_engine.infra import reviewer_target
import sys


# ── 기록 조회면 (--rounds-report) ───────────────────────────────────────────
# 비용 적정성 판단(리뷰 집계 티켓 심도 대비)을 PM 자기보고 대신 기록 근거로 하게 만드는 읽기 전용 표다.
# 호출·diff 추출 없이 기록 파일만 읽는다.

# rc 판정 표기 — 1 은 반려뿐 아니라 실패·불명확도 포함하므로 '비통과'로 읽는다(결함 수 '미상'이
# 그 라운드가 판정을 내지 못했음을 함께 알린다).
_ROUND_VERDICT_LABELS: dict[int, str] = {0: "통과", 1: "비통과"}


def _format_round_field(value: object) -> str:
    """산출 필드(결함 수) 표기 — 셀 근거가 없던 null 은 '미상'으로 구분해 보인다."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "미상"
    return str(value)


def _format_round_verdict(value: object) -> str:
    """판정 rc 표기 — `0(통과)` / `1(비통과)`; 기록이 없거나 손상이면 '미상'."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "미상"
    label = _ROUND_VERDICT_LABELS.get(value)
    return f"{value}({label})" if label else str(value)


def _round_sequence(outcome: dict) -> int | None:
    """산출 레코드가 실은 **예약 순번** (없거나 손상이면 None)."""
    value = outcome.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _ordered_round_outcomes(rounds: list) -> list[dict]:
    """산출을 **예약 순서**로 정렬한다 — append 순서는 *완료* 순서라 라운드 번호가 아니다.

    같은 리뷰 집계 티켓의 두 라운드가 역순으로 끝나면(느린 1라운드·빠른 2라운드) append 순서는 실제 라운드
    순서를 뒤집는다. 순번이 없는 옛 버전/미연결 산출은 **앞**(오래된 쪽)에 원래 순서대로 남긴다
    (안정 정렬) — 뒤에 두면 업그레이드 후 새 통과가 쌓여도 옛 반려가 영원히 '최신'이 된다.

    정렬 규칙은 `review_rounds.round_outcome_order_key` 가 소유한다 — 같은 기록을 읽는 수준 낮춤
    판정(`review_rounds._last_round_verdict`)과 사본으로 갈리면 두 명령이 서로 다른 라운드를
    '최신'이라고 답한다."""
    return sorted(rounds, key=review_rounds.round_outcome_order_key)


def _format_review_ticket_resolution(entry: dict) -> str:
    """리뷰 집계 티켓 처분 열 — `미처분` / `pm-verified` / `무대상`.

    판정은 `review_rounds` 의 리뷰 집계 티켓 판정(`review_ticket_resolution`·`review_ticket_residual_must_fix`)이 소유한다 —
    릴리즈 차단이 보는 것과 **같은 사실**을 그대로 보여야 이 표로 차단을 미리 예측할 수 있다.
    `무대상` 은 남은 must-fix 가 없어 처분이 필요 없는 리뷰 집계 티켓이다(통과·suggestion 만 남은 리뷰 집계 티켓)."""
    declared = review_rounds.review_ticket_resolution(entry)
    if declared is None:
        return "미처분" if review_rounds.review_ticket_has_residual(entry) else "무대상"
    return _describe_resolution(declared)


def render_rounds_report(
    round_records: dict,
    *,
    record_path: Path | str | None = None,
    review_ticket: str | None = None,
    session_budget: int | None = None,
    title: str = "추가 리뷰 라운드 기록",
    include_session_budget: bool = True,
) -> str:
    """리뷰 라운드 기록을 조회 표로 렌더한다 (리뷰 집계 티켓별 라운드 수 · 라운드별 산출 · 세션 호출 예산 spent).

    `review_ticket` 을 주면 그 리뷰 집계 티켓만 본다. 순수 함수라 기록 dict 만 있으면 렌더가 재현된다 — 파일/대상 저장소
    해소는 호출부(`_print_rounds_report`)가 한다.

    라운드 번호와 나열 순서는 **예약 순번**(`sequence`)이다 — 기록의 append 순서는 완료 순서라
    동시 리뷰가 역순으로 끝나면 라운드가 뒤바뀌어 보인다.

    `session_budget` 미지정은 레지스트리 기본값이다(호출부가 conf 해소를 못 하는 조회 경로용)."""
    if session_budget is None:
        session_budget = reviewer_result._conf_default_int(reviewer_budget.ADDITIONAL_REVIEWER_SESSION_BUDGET_KEY)
    snapshot = dict(round_records)                # 사본 정규화 — 조회가 기록을 고치지 않는다
    session_budget_state = reviewer_reservation._session_budget_state(snapshot)
    lines = [
        f"{title}: {record_path if record_path is not None else '(미해소)'}",
        "범례: 판정 0=통과 · 1=비통과(반려·실패·불명확) · '미상'=판정이 무효했던 라운드",
        "처분: 미처분=남은 must-fix 선언 없음(릴리즈 차단) · "
        "pm-verified=현재 티켓 fix의 기계 확인 완료 · 무대상=남은 지적 없음",
    ]
    if include_session_budget:
        lines.insert(
            1,
            f"세션 호출 예산: spent={session_budget_state['spent']} / 예산 {session_budget} · "
            f"시작 {session_budget_state['started'] or '미시작'}",
        )
    names = [name for name in reviewer_budget._review_ticket_names(snapshot) if review_ticket is None or name == review_ticket]
    if not names:
        lines.append(
            f"리뷰 집계 티켓 {review_ticket}: 기록 없음" if review_ticket else "기록된 리뷰 집계 티켓 없음"
        )
        return "\n".join(lines)
    for name in names:
        entry = reviewer_reservation._review_ticket_entry(snapshot, name)
        rounds = entry["rounds"]
        lines.append(
            f"리뷰 집계 티켓 {name}: count={entry['count']} · 산출 {len(rounds)}건 · "
            f"처분={_format_review_ticket_resolution(entry)}"
        )
        if not rounds:
            lines.append("  (라운드 산출 기록 없음 — 산출 기록 이전의 호출)")
            continue
        for outcome in _ordered_round_outcomes(rounds):
            sequence = _round_sequence(outcome)
            lines.append(
                f"  #{sequence if sequence is not None else '미상'} "
                f"{outcome.get('ts') or '시각 미상'} "
                f"판정={_format_round_verdict(outcome.get('verdict'))} "
                f"must_fix={_format_round_field(outcome.get('must_fix'))} "
                f"suggestions={_format_round_field(outcome.get('suggestions'))}"
            )
    return "\n".join(lines)


def _print_rounds_report(
    home, *, review_ticket: str | None, ) -> int:
    """`--rounds-report` 실행면 — 소유 PM 홈 기록 파일을 해소해 조회 표를 출력한다 (rc 0).

    기록 경로와 같은 PM 소유자 Home을 받아 기록 파일과 예산 설정을 조회한다.
    selector 유무에 따른 소유자 해소는 호출부가 담당한다.
    """
    try:
        session_budget = reviewer_target._wave_budget(conf_store.local_config(home))
    except (OSError, UnicodeError) as exc:
        # 조회는 호출이 없어 conf 판독 실패로 막을 이유가 없다(호출 경로의 fail-closed 와
        # 다른 기준) — 예산 표기만 기본값으로 낮추고 그 사실을 알린다.
        print(
            f"경고: local.conf 읽기 실패 ({type(exc).__name__}: {exc}) — 세션 호출 예산 표기를 "
            f"기본값 {reviewer_result._conf_default_int(reviewer_budget.ADDITIONAL_REVIEWER_SESSION_BUDGET_KEY)} 로 대체합니다.",
            file=sys.stderr,
        )
        session_budget = reviewer_result._conf_default_int(reviewer_budget.ADDITIONAL_REVIEWER_SESSION_BUDGET_KEY)
    print(render_rounds_report(
        review_rounds._load_round_records(home),
        record_path=review_rounds._round_ledger_path(home),
        review_ticket=review_ticket,
        session_budget=session_budget,
    ))
    return 0


# ── 리뷰 집계 티켓 처분 선언면 (--resolve-review) ─────────────────────────────────────
# 라운드 상한으로 종결된 리뷰 집계 티켓의 남은 must-fix가 현재 티켓의 fix에서 기계적으로
# 닫혔음을 기록 파일에 남기는 기록면이다(호출 없음). 후속 티켓·후속 리뷰 집계 티켓으로 넘기는
# 처분은 고정 5단계 마무리 계약과 충돌하므로 제공하지 않는다.


def _describe_resolution(declared: dict) -> str:
    """현재 유일한 처분 선언을 사람용 한 줄로 표기한다."""
    return review_rounds.describe_pm_verified_resolution(declared)


def _resolve_review_ignored_flags(args: argparse.Namespace) -> str:
    """처분 선언 실행이 무시하는 플래그 목록 (없으면 빈 문자열)."""
    return ", ".join(
        flag for flag, given in (
            ("--review-ticket", bool(args.review_ticket)),        # 처분 대상은 `--resolve-review` 가 지정한다
            # 집계 opt-out 도 선언면에선 뜻이 없다 — 이 실행은 호출도 예약도 하지 않는다.
            ("--no-review-ticket", bool(getattr(args, "no_review_ticket", False))),
            ("--rounds-report", args.rounds_report),
            ("--reset-session-budget", args.reset_session_budget),
        ) if given
    )


def _resolve_review_command(home, args: argparse.Namespace, ) -> int:
    """`--resolve-review` 실행면 — 리뷰 집계 티켓 처분을 기록 파일에 선언한다 (rc 0 = 기록됨).

    거부는 전부 **기록 파일 변경 전**이다 — 선언되지 않은 실행은 기록 파일에 아무 흔적도 남기지 않는다.
    확인→기록은 라운드 예약과 같은 배타락 임계 구역 안에서 한다(동시 실행이 서로의 선언을
    덮어쓰지 않게)."""
    engine_repo = home.root
    review_ticket = args.resolve_review
    reserved = reviewer_budget._reserved_review_ticket_error(review_ticket, flag="--resolve-review")
    if reserved is not None:
        print(reserved, file=sys.stderr)
        return 1
    pm_verified = args.pm_verified
    if not pm_verified:
        print(reviewer_budget._RESOLVE_REVIEW_MODE_GUIDANCE.format(review_ticket=review_ticket), file=sys.stderr)
        return 1
    if args.dry_run:
        # 선언은 기록이 목적인 실행이라 "미리보기"가 성립하지 않는다 — 경고 후 기록하면
        # `--dry-run` 의 부작용 0 계약이 이 명령에서만 깨진다. 거부가 정직하다.
        print(reviewer_budget._RESOLVE_REVIEW_DRY_RUN_GUIDANCE.format(review_ticket=review_ticket), file=sys.stderr)
        return 1
    ignored = _resolve_review_ignored_flags(args)
    if ignored:
        print(f"경고: --resolve-review 는 라운드 기록 전용이라 다음 플래그를 무시합니다: {ignored}.",
              file=sys.stderr)
    owner = pm_paths.resolve_pm_home_for_repo(engine_repo)
    home = pm_paths.home_at(home.root, pm_home=owner, board_root=pm_paths._board_root_at(owner))
    with review_rounds._round_record_lock(home):
        round_records = review_rounds._load_round_records(home)
        if review_ticket not in round_records:
            print(reviewer_budget._RESOLVE_REVIEW_UNKNOWN_GUIDANCE.format(
                review_ticket=review_ticket, record_path=review_rounds._round_ledger_path(home)), file=sys.stderr)
            return 1
        entry = reviewer_reservation._review_ticket_entry(round_records, review_ticket)
        if not review_rounds.review_ticket_has_residual(entry):
            # 남은 것 없음만 거부한다 — **미상**(판정 무효 라운드)은 릴리즈가 차단하는 경우라
            # 선언도 받아야 한다(둘이 갈리면 차단은 되는데 처분은 못 하는 데드락).
            print(reviewer_budget._RESOLVE_REVIEW_NO_RESIDUAL_GUIDANCE.format(
                review_ticket=review_ticket, residual=review_rounds.review_ticket_residual_label(entry)), file=sys.stderr)
            return 1
        residual = review_rounds.review_ticket_residual_must_fix(entry)
        # 추가 리뷰어 채널로 스코프해 판정한다 — board 는 채널과 그 리뷰 집계 티켓의 기록 항목을
        # 받아 다른 채널의 accepted 남은 지적·기계 확인은 보지 않는다.
        detail = completion_check._pm_verified_evidence_problem(
            home, review_ticket, channel=completion_check.CHECK_CHANNEL_ADDITIONAL, entry=entry,
        )
        if detail is not None:
            print(reviewer_budget._RESOLVE_REVIEW_PM_VERIFIED_GUIDANCE.format(
                review_ticket=review_ticket, detail=detail), file=sys.stderr)
            return 1
        residual_label = review_rounds.review_ticket_residual_label(entry)
        previous = review_rounds.review_ticket_resolution(entry)
        declared = {
            "kind": review_rounds.RESOLUTION_PM_VERIFIED,
            "ts": clock.now_utc(),
            "must_fix": residual,
            **review_rounds.review_ticket_round_binding(entry),
        }
        entry["resolution"] = declared
        reviewer_reservation._save_round_records(home, round_records)
    if previous is not None:
        print(f"이전 처분 선언을 교체합니다: {_describe_resolution(previous)}", file=sys.stderr)
    print(f"리뷰 집계 티켓 처분 선언: {review_ticket} · 남은 must_fix {residual_label} · "
          f"{_describe_resolution(declared)}")
    print(f"  · 기록 파일: {review_rounds._round_ledger_path(home)}")
    return 0
