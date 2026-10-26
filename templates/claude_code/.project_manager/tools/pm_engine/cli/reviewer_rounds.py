"""추가 리뷰 라운드 조회와 처분 (층: cli)."""
from __future__ import annotations

from pathlib import Path
import argparse
from pm_engine.infra import clock
from pm_engine.app import completion_gate
from pm_engine.infra import conf_store
from pm_engine.infra import pm_paths
from pm_engine.infra import review_rounds
from pm_engine.domain import reviewer_budget
from pm_engine.app import reviewer_reservation
from pm_engine.domain import reviewer_result
from pm_engine.infra import reviewer_target
import sys


# ── 장부 조회면 (--rounds-report) ───────────────────────────────────────────
# 비용 적정성 판단(게이트 심도 대비)을 PM 자기보고 대신 장부 근거로 하게 만드는 읽기 전용 표다.
# 호출·diff 추출 없이 장부만 읽는다.

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

    같은 게이트의 두 라운드가 역순으로 끝나면(느린 1라운드·빠른 2라운드) append 순서는 실제 라운드
    순서를 뒤집는다. 순번이 없는 구세대/미연결 산출은 **앞**(오래된 쪽)에 원래 순서대로 남긴다
    (안정 정렬) — 뒤에 두면 업그레이드 후 새 통과가 쌓여도 옛 반려가 영원히 '최신'이 된다.

    정렬 규칙은 `review_rounds.round_outcome_order_key` 가 소유한다 — 같은 장부를 읽는 강등
    판정(`review_rounds._last_round_verdict`)과 사본으로 갈리면 두 표면이 서로 다른 라운드를
    '최신'이라고 답한다."""
    return sorted(rounds, key=review_rounds.round_outcome_order_key)


def _format_gate_resolution(entry: dict) -> str:
    """게이트 처분 열 — `미처분` / `pm-verified` / `무대상`.

    판정은 `review_rounds` 의 게이트 판정(`gate_resolution`·`gate_residual_must_fix`)이 소유한다 —
    릴리즈 차단이 보는 것과 **같은 사실**을 그대로 보여야 이 표로 차단을 미리 예측할 수 있다.
    `무대상` 은 잔여 must-fix 가 없어 처분이 필요 없는 게이트다(통과·suggestion 만 남은 게이트)."""
    declared = review_rounds.gate_resolution(entry)
    if declared is None:
        return "미처분" if review_rounds.gate_has_residual(entry) else "무대상"
    return _describe_resolution(declared)


def render_rounds_report(
    ledger: dict,
    *,
    ledger_path: Path | str | None = None,
    gate: str | None = None,
    wave_budget: int | None = None,
    title: str = "추가 리뷰 라운드 장부",
    include_wave: bool = True,
) -> str:
    """라운드 장부를 조회 표로 렌더한다 (게이트별 라운드 수 · 라운드별 산출 · wave spent).

    `gate` 를 주면 그 게이트만 본다. 순수 함수라 장부 dict 만 있으면 렌더가 재현된다 — 파일/앵커
    해소는 호출부(`_print_rounds_report`)가 한다.

    라운드 번호와 나열 순서는 **예약 순번**(`sequence`)이다 — 장부의 append 순서는 완료 순서라
    동시 리뷰가 역순으로 끝나면 라운드가 뒤바뀌어 보인다.

    `wave_budget` 미지정은 레지스트리 기본값이다(호출부가 conf 해소를 못 하는 조회 경로용)."""
    if wave_budget is None:
        wave_budget = reviewer_result._conf_default_int(reviewer_budget.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY)
    snapshot = dict(ledger)                # 사본 정규화 — 조회가 장부를 고치지 않는다
    wave = reviewer_reservation._wave_state(snapshot)
    lines = [
        f"{title}: {ledger_path if ledger_path is not None else '(미해소)'}",
        "범례: 판정 0=통과 · 1=비통과(반려·실패·불명확) · '미상'=판정이 무효했던 라운드",
        "처분: 미처분=잔여 must-fix 선언 없음(릴리즈 차단) · "
        "pm-verified=현재 티켓 fix의 기계 확인 완료 · 무대상=잔여 없음",
    ]
    if include_wave:
        lines.insert(
            1,
            f"wave: spent={wave['spent']} / 예산 {wave_budget} · "
            f"시작 {wave['started'] or '미시작'}",
        )
    names = [name for name in reviewer_budget._gate_names(snapshot) if gate is None or name == gate]
    if not names:
        lines.append(
            f"게이트 {gate}: 장부에 기록 없음" if gate else "기록된 게이트 없음"
        )
        return "\n".join(lines)
    for name in names:
        entry = reviewer_reservation._gate_entry(snapshot, name)
        rounds = entry["rounds"]
        lines.append(
            f"게이트 {name}: count={entry['count']} · 산출 {len(rounds)}건 · "
            f"처분={_format_gate_resolution(entry)}"
        )
        if not rounds:
            lines.append("  (라운드 산출 기록 없음 — 산출 장부 이전의 호출)")
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
    home, *, gate: str | None, ) -> int:
    """`--rounds-report` 실행면 — 소유 PM 홈 장부를 해소해 조회 표를 출력한다 (rc 0).

    기록 경로와 같은 PM 소유자 Home을 받아 장부와 예산 설정을 조회한다.
    selector 유무에 따른 소유자 해소는 호출부가 담당한다.
    """
    try:
        wave_budget = reviewer_target._wave_budget(conf_store.local_config(home))
    except (OSError, UnicodeError) as exc:
        # 조회는 호출이 없어 conf 판독 실패로 막을 이유가 없다(호출 경로의 fail-closed 와
        # 다른 축) — 예산 표기만 기본값으로 낮추고 그 사실을 알린다.
        print(
            f"경고: local.conf 읽기 실패 ({type(exc).__name__}: {exc}) — wave 예산 표기를 "
            f"기본값 {reviewer_result._conf_default_int(reviewer_budget.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY)} 로 대체합니다.",
            file=sys.stderr,
        )
        wave_budget = reviewer_result._conf_default_int(reviewer_budget.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY)
    print(render_rounds_report(
        review_rounds._load_round_ledger(home),
        ledger_path=review_rounds._round_ledger_path(home),
        gate=gate,
        wave_budget=wave_budget,
    ))
    return 0


# ── 게이트 처분 선언면 (--resolve-gate) ─────────────────────────────────────
# 라운드 상한으로 종결된 게이트의 잔여 must-fix가 현재 티켓의 fix에서 기계적으로
# 닫혔음을 장부에 남기는 기록면이다(호출 없음). 후속 티켓·후속 게이트로 넘기는
# 처분은 고정 5단계 수렴 계약과 충돌하므로 제공하지 않는다.


def _describe_resolution(declared: dict) -> str:
    """현재 유일한 처분 선언을 사람용 한 줄로 표기한다."""
    return review_rounds.describe_pm_verified_resolution(declared)


def _resolve_gate_ignored_flags(args: argparse.Namespace) -> str:
    """처분 선언 실행이 무시하는 플래그 목록 (없으면 빈 문자열)."""
    return ", ".join(
        flag for flag, given in (
            ("--gate", bool(args.gate)),        # 처분 대상은 `--resolve-gate` 가 지정한다
            # 회계 opt-out 도 선언면에선 뜻이 없다 — 이 실행은 호출도 예약도 하지 않는다.
            ("--no-gate", bool(getattr(args, "no_gate", False))),
            ("--rounds-report", args.rounds_report),
            ("--ack-wave", args.ack_wave),
        ) if given
    )


def _resolve_gate_command(home, args: argparse.Namespace, ) -> int:
    """`--resolve-gate` 실행면 — 게이트 처분을 장부에 선언한다 (rc 0 = 기록됨).

    거부는 전부 **장부 변경 전**이다 — 선언되지 않은 실행은 장부에 아무 흔적도 남기지 않는다.
    확인→기록은 라운드 예약과 같은 배타락 임계 구역 안에서 한다(동시 실행이 서로의 선언을
    덮어쓰지 않게)."""
    engine_repo = home.root
    gate = args.resolve_gate
    reserved = reviewer_budget._reserved_gate_error(gate, flag="--resolve-gate")
    if reserved is not None:
        print(reserved, file=sys.stderr)
        return 1
    pm_verified = args.pm_verified
    if not pm_verified:
        print(reviewer_budget._RESOLVE_GATE_MODE_GUIDANCE.format(gate=gate), file=sys.stderr)
        return 1
    if args.dry_run:
        # 선언은 기록이 목적인 실행이라 "미리보기"가 성립하지 않는다 — 경고 후 기록하면
        # `--dry-run` 의 부작용 0 계약이 이 표면에서만 깨진다. 거부가 정직하다.
        print(reviewer_budget._RESOLVE_GATE_DRY_RUN_GUIDANCE.format(gate=gate), file=sys.stderr)
        return 1
    ignored = _resolve_gate_ignored_flags(args)
    if ignored:
        print(f"경고: --resolve-gate 는 장부 기록 전용이라 다음 플래그를 무시합니다: {ignored}.",
              file=sys.stderr)
    owner = pm_paths.resolve_pm_home_for_repo(engine_repo)
    home = pm_paths.home_at(home.root, pm_home=owner, board_root=pm_paths._board_root_at(owner))
    with review_rounds._round_ledger_lock(home):
        ledger = review_rounds._load_round_ledger(home)
        if gate not in ledger:
            print(reviewer_budget._RESOLVE_GATE_UNKNOWN_GUIDANCE.format(
                gate=gate, ledger=review_rounds._round_ledger_path(home)), file=sys.stderr)
            return 1
        entry = reviewer_reservation._gate_entry(ledger, gate)
        if not review_rounds.gate_has_residual(entry):
            # 잔여 없음만 거부한다 — **미상**(판정 무효 라운드)은 릴리즈가 차단하는 축이라
            # 선언도 받아야 한다(둘이 갈리면 차단은 되는데 처분은 못 하는 데드락).
            print(reviewer_budget._RESOLVE_GATE_NO_RESIDUAL_GUIDANCE.format(
                gate=gate, residual=review_rounds.gate_residual_label(entry)), file=sys.stderr)
            return 1
        residual = review_rounds.gate_residual_must_fix(entry)
        # 추가 리뷰어 채널로 스코프해 판정한다 — board 는 채널과 그 게이트의 장부 항목을
        # 받아 다른 채널의 accepted 잔여·기계 확인은 보지 않는다.
        detail = completion_gate._pm_verified_evidence_problem(
            home, gate, channel=completion_gate.GATE_CHANNEL_ADDITIONAL, entry=entry,
        )
        if detail is not None:
            print(reviewer_budget._RESOLVE_GATE_PM_VERIFIED_GUIDANCE.format(
                gate=gate, detail=detail), file=sys.stderr)
            return 1
        residual_label = review_rounds.gate_residual_label(entry)
        previous = review_rounds.gate_resolution(entry)
        declared = {
            "kind": review_rounds.RESOLUTION_PM_VERIFIED,
            "ts": clock.now_utc(),
            "must_fix": residual,
            **review_rounds.gate_round_binding(entry),
        }
        entry["resolution"] = declared
        reviewer_reservation._save_round_ledger(home, ledger)
    if previous is not None:
        print(f"이전 처분 선언을 교체합니다: {_describe_resolution(previous)}", file=sys.stderr)
    print(f"게이트 처분 선언: {gate} · 잔여 must_fix {residual_label} · "
          f"{_describe_resolution(declared)}")
    print(f"  · 장부: {review_rounds._round_ledger_path(home)}")
    return 0
