"""추가 리뷰어 산출 회수와 라운드 마감 (층: app)."""
from __future__ import annotations

from typing import NamedTuple
from pathlib import Path
from pm_engine.infra import clock
import datetime
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_errors
from pm_engine.infra import fs
from pm_engine.infra import pm_paths
from pm_engine.domain import review_contract
from pm_engine.domain import review_form
from pm_engine.infra import review_rounds
from pm_engine.domain import review_state
from pm_engine.domain import reviewer_budget
from pm_engine.app import reviewer_prompt
from pm_engine.app import reviewer_reservation
from pm_engine.app import reviewer_run
from pm_engine.app import round_maintenance
from pm_engine.domain import round_rules
from pm_engine.app import round_seed
from pm_engine.infra import round_store
import sys
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store


# ── 결과 요약 출력 ────────────────────────────────────────────────────────


def _format_verdict(ok: bool, verdict: dict | None) -> str:
    if not ok:
        return "실패"
    if verdict is None:
        return "미실행"
    if verdict["has_must_fix"]:
        return "성공 → 반려 (must-fix 감지)"
    if verdict["has_pass"]:
        return "성공 → 통과"
    return "성공 → 판정 불명확 (PM 확인 필요)"


def print_summary(result: dict, gate: str | None = None) -> None:
    """결과 요약을 stdout 에 출력한다.

    게이트 줄은 **항상** 나온다 — 게이트 없이 끝난 실행은 그 사실이 회계 상태(장부 미기록)라
    stderr 경고 하나에만 맡기지 않는다(오염 진단·실패 사유와 같은 근거: 판정 블록은 읽힌다).
    이 줄은 실행이 끝난 뒤라 확정형으로 말할 수 있다."""
    sep = "=" * 60
    name = result.get("reviewer", "reviewer")
    print(sep)
    print(f"추가 리뷰어 코드리뷰 결과 요약 [{name}]")
    print(f"게이트: {gate if gate else reviewer_budget._SUMMARY_UNACCOUNTED_GATE}")
    print(sep)
    print(f"\n[{name}] {_format_verdict(result['ok'], result.get('verdict'))}")
    if result.get("file"):
        print(f"  원문: {result['file']}")
    # 오염 진단은 판정 라인과 같은 자리에 둔다 — stderr 경고는 로그를 안 읽으면 사라지지만 PM 은
    # 판정 블록을 반드시 읽는다. 판정 자체를 뒤집지는 않고(엇갈린 판정만 run_review 가 불명확 처리),
    # 이번 판정이 리뷰어 자신의 것인지 PM 이 확인할 근거를 남긴다.
    for marker in result.get("contamination") or ():
        print(f"  ⚠ 회신 품질 의심(이번 판정 확정 불가): {marker}")
    if result.get("reply_extraction_failed"):
        # 구조화 대상이 rc=0 으로 끝났는데 회신을 못 뽑았다 — wire 를 판정에 흘리지 않았으므로
        # 이 실행은 리뷰를 못 받은 것이고, 실패 축(FALLBACK_INTERNAL)으로 내려간다. 원인(형식
        # 변경·비-문자열 회신·빈 turn)은 raw 원문에서만 보이니 그리로 보낸다.
        print("  ⚠ 회신 추출 실패 — 하네스 wire 에서 최종 회신을 찾지 못했습니다"
              "(리뷰 미수신 처리 · 원문 파일 확인).")
    print()
    if result["failed"]:
        # 실패 사유 1줄을 판정 라인에 병기 — 타임아웃 안내(`--timeout`/conf 키) 같은 실패
        # 본문이 원문 파일에만 남으면 PM 이 못 본다(판정 라인은 반드시 읽힌다).
        reason = reviewer_budget._first_output_line(result)
        if result.get("reply_extraction_failed"):
            # 이 실패의 출력은 wire 원문이라 첫 줄(이벤트 JSON)이 사유가 되지 못한다 — 사유를
            # 직접 말한다.
            print("  사유: 하네스 wire 에서 최종 회신 텍스트를 추출하지 못했습니다 "
                  "(회신 이벤트 부재 · 비-문자열 회신 · 형식 붕괴) — 원문 파일에 wire 전문 보존.")
        elif reason:
            print(f"  사유: {reason}")
        print(f"종합 판정: {name} 실패")
        print("FALLBACK_INTERNAL")  # 내부 code-reviewer 서브에이전트로 폴백 신호
    elif result["any_must_fix"]:
        print("종합 판정: 비-통과 (must-fix 감지 — PM 검토 필요)")
    elif result["all_pass"]:
        print("종합 판정: 통과")
    else:
        print("종합 판정: 판정 불명확 (PM 확인 필요)")
    print(sep)


def _harvest_target_ticket(
    home, args, ) -> tuple[str | None, str | None]:
    """회수 대상 티켓과 (대상이 없을 때의) 사유 — `--ticket` 우선, 없으면 ticket 형상 `--gate`.

    문서화된 설계 리뷰 형상은 `--paths … --gate T-NNNN` 이라 `--ticket` 이 비어 있다. 그 실행의
    산출도 그 티켓의 것이므로 같은 회수 규칙을 탄다(자유 문자열 게이트는 대상이 아니다).
    """
    if args.ticket:
        return args.ticket, None
    gate = getattr(args, "gate", None)
    if not gate:
        return None, None
    if not ticket_rules._is_valid_ticket_id(gate):
        return None, None                     # 자유 문자열 게이트 — 회수 대상 아님(조용).
    try:
        round_store._find_ticket_file(home, gate, )
    except pm_paths.AnchorResolutionError as exc:
        return None, f"게이트 {gate} 를 보드에서 찾지 못해 회수하지 않았습니다: {exc}"
    return gate, None


class _HarvestTargetState(NamedTuple):
    """회수 대상 티켓의 명세 전문과 라운드 목록 — 프롬프트 실값 두 소비자가 쓰는 한 벌."""

    text: str
    rounds: list


def _harvest_target_ticket_state(
    home, args, *, degraded: str,
) -> _HarvestTargetState | None:
    """회수 대상 티켓의 명세 + 라운드 (대상 없거나 읽기 실패면 None · 실패는 loud).

    프롬프트에 실린 티켓 본문에서 파생하면 문서화된 설계 리뷰 형상(`--paths … --gate T-NNNN`)은
    본문이 실리지 않아 티켓을 못 본다. 대상 해소는 회수 경로와 **같은 함수**를 써서 두 표면이
    갈리지 않게 하고, 프롬프트 실값·확인 근거 대조가 **같은 읽기**를 쓴다.

    finding ID 는 라운드 파일에만 선언되므로 명세만 읽어서는 다음 ID·확인 대상을 알 수 없다 —
    두 축을 함께 돌려준다.
    """
    ticket, _problem = _harvest_target_ticket(home, args, )
    if not ticket:
        return None
    try:
        ticket_text = round_store._load_ticket_text_and_body(
            round_store._find_ticket_file(home, ticket, )
        )[0]
        return _HarvestTargetState(
            ticket_text,
            reviewer_prompt._load_ticket_rounds_for(
                home, ticket, ticket_text=ticket_text,
            ),
        )
    except (pm_paths.AnchorResolutionError, OSError, UnicodeError) as exc:
        print(
            f"경고: {ticket} 본문을 읽지 못했습니다({exc}) — {degraded}",
            file=sys.stderr,
        )
        return None


def _next_external_finding_id(home, args, ) -> str | None:
    """이 채널이 이번 라운드에 쓸 첫 finding ID 실값 (회수 대상 없으면 None).

    라운드마다 fresh 인 리뷰어 세션이 스스로 알 수 없는 값이다 — 안 실으면 2라운드가 같은 ID 를
    재선언해 회수가 거부된다. 시야는 **명세 + 모든 라운드**다(넓은 스캔) — 한 라운드만 보면
    지난 라운드가 쓴 번호를 다시 지시한다.
    """
    state = _harvest_target_ticket_state(
        home, args, degraded="이 라운드는 첫 finding ID 로 안내합니다.",
    )
    if state is None:
        return None
    return review_state.next_review_finding_id(
        state.text, review_form.ADDITIONAL_REVIEWER_ROLE, state.rounds,
    )


def _harvest_additional_reviewer_section(
    home, ticket: str, result: dict, ) -> str | None:
    """추가 리뷰어 산출을 게이트 티켓의 새 `additional-reviewer` 라운드 파일로 회수한다.

    회수 주체는 **엔진**이다 — 리뷰어에게 티켓/보드 편집 권한을 주지 않는다. 라운드 본문은 첫 줄
    헤더 + 산문 회신 전문(그 안에 versioned 블록 하나)이고, 내용 검증(`review_contract` 소유)을
    통과할 때만 파일이 생긴다. 통과하지 못하면 **파일을 만들지 않고** 사유를 돌려준다(호출부가
    rc 로 표면화하고 산출 원문은 raw 에 남는다) — 라운드가 파일 하나라 "거부 표식을 얹어 절에
    남기고 판정 표면에서 빼는" 보정이 필요 없다.

    채번+생성은 준비(`delegate_prepare.prepare_ticket_copy`)와 **같은 seam**(`round_store.reserve_round`)을
    지난다 — 인자가 시드냐 실내용이냐만 다르다.

    실행 엔진의 round_store와 round_maintenance를 사용하고 데이터는 Home의 PM 소유자에 쓴다.
    """
    reply = result.get("answer") or result.get("output") or ""
    if not reply.strip():
        return "리뷰어 회신이 비어 회수할 산출이 없습니다"
    try:
        return _reserve_additional_reviewer_round(
            home, ticket, reply, )
    # 라운드 규약·파일 IO의 기존 오류를 회수 실패 사유로 보고한다.
    except (delegate_errors.DelegateError, round_rules.RoundsError,
            OSError, UnicodeError) as exc:
        return f"티켓 라운드 기록 실패: {exc}"


def _reserve_additional_reviewer_round(
    home, ticket: str, reply: str, ) -> str | None:
    """내용 검증 → 통과 시 라운드 예약 + board 부분 커밋 (위반 사유 또는 None).

    검증 입력(명세 + 라운드)은 예약 **직전**에 읽는다 — finding ID 재선언·confirmation 대상
    판정이 실제로 쓰이는 상태와 같은 스냅샷을 봐야 한다.
    """
    role = review_form.ADDITIONAL_REVIEWER_ROLE
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
    status, ticket_path = found
    if status not in ("open", "claimed"):
        raise delegate_errors.DelegateError(
            f"additional-reviewer 라운드 기록은 open/claimed 티켓만 허용: "
            f"{ticket} in {status}/"
        )
    with fs.open_shared(
        ticket_path, binary=False, encoding="utf-8", newline="",
    ) as handle:
        ticket_text = handle.read()
    tickets_dir = pm_paths.tickets_dir(home)
    rounds = round_seed.load_rounds(
        tickets_dir, ticket, ticket_text=ticket_text,
    )
    problem = review_contract.review_harvest_problem(
        reply, ticket_text=ticket_text, rounds=rounds,
    )
    if problem is not None:
        return problem      # 파일을 만들지 않는다 — 산출 원문은 raw 에만 남는다.

    # 회신 bytes 는 그대로 둔다(개행 표기 포함) — 라운드 파일은 산출 하나의 원문이다.
    content = (
        round_rules.render_round_header(
            role, today=datetime.date.today().isoformat(),
        )
        + "\n\n" + (reply if reply.endswith("\n") else reply + "\n")
    )
    round_path = round_store.reserve_round(
        tickets_dir, ticket, role, content=content, lock=fs.board_lock(home),
    )
    ordinal, _role = round_rules.parse_round_filename(round_path.name)
    message = f"additional-reviewer {ticket} {role}"
    # board 부분 커밋 seam 은 직접 부른다 — 이름을 더듬어 찾으면 그 이름이 갈렸을 때 라운드
    # 파일만 만들어지고 board 커밋은 조용히 빠진 rc0 이 된다(AttributeError 로 죽는 편이 낫다).
    sync_ready = bool(round_maintenance._rounds_mutation_sync_paths(home, message, [round_path]))
    print(
        f"[additional-reviewer] 티켓 회수: {ticket} {role}[{ordinal}] → {round_path}"
        + ("" if sync_ready else " (board-git 동기 미준비)"),
        file=sys.stderr,
    )
    return None


def _run_review_round(
    home, args, *, conf, prompt, reviewer_cmd, timeout, idle_timeout,
    output_dir, conf_path, profile, target=None,
    reservation: "reviewer_reservation._PreSpawnReservation | None" = None,
    diff_root: Path | None = None,
) -> int:
    """예약 수명 **안**에서 도는 구간 — 리뷰 실행·장부 마감.

    `_main` 에서 떼어낸 이유는 예약 소유권 하나다: 여기서 어떤 경로로 빠져나가도(조기 return·
    예외) 호출부의 단일 `__exit__` 가 스폰 전 예약을 환불한다.

    예산 확인·예약은 이 구간 **밖**에서 이미 끝났다. 넘겨받은 예약(`reservation`)의 앞부분(환경 준비, 그리고
    `run_review` 안의 raw 선점·장부 시작 레코드·transport·argv 준비, 그리고 러너 안의 준비
    구간)은 아직 스폰 전이라 호출부의 환불 seam 이 소유하고, 소유권은 **실 스폰 경계**(watchdog
    워치독의 `Popen` 직전)의 `on_spawn_attempt` 콜백으로 넘어온다 — 그 뒤부터 이 구간이 예약을
    호출 결과로 마감한다.

    이전에는 대칭 반납이 붙는다(`on_no_spawn`). 러너가 `started=False` 로 판명해 돌아오면 소유권이
    스폰 전 seam 으로 되돌아오고, 요약·진단·라운드 환불이 **저장까지 끝난 뒤**에야 반납된다 — 그
    사이의 실패(닫힌 stdout 파이프의 `BrokenPipeError` 가 실측 축이다)는 자식이 없던 실행이므로
    바깥 seam 이 예약을 한 번 되돌려야 한다. 예외로 나가지 **않는** 변종이 하나 있다: 마감 저장이
    `OSError` 로 실패한 경우다. 그 경로는 판정 rc 를 그대로 돌려주며 정상 return 하므로 바깥
    `__exit__` 가 소유를 보지 못해, 되돌림을 그 자리에서 직접 한 번 한다.
    `started=True` 는 종전 그대로 반납하지 않는다.
    """
    if reservation is None:
        reservation = reviewer_reservation._PreSpawnReservation(home, reviewer_budget.RoundBudget())  # 장부 밖 직접 호출
    budget = reservation.budget
    # 추가 리뷰어의 실행 조건은 위임과 같다 — cwd 는 검토 대상 저장소, env 는 위임 채널이 소유한
    # 같은 seam 이 조립한다. 채널이 다르다고 다른 홈·다른 allowlist 를 만들지 않는다(그 차등은
    # 강제력 없이 비용만 받았다: 리뷰어는 절대경로를 그대로 읽을 수 있고, 임시 홈은 세션 재사용을
    # 막아 라운드마다 전체 payload 를 다시 과금했다).
    reviewer_environment = (
        delegate_env.build_env(target.harness) if target is not None else None
    )
    print(
        f"[additional-reviewer] 리뷰어 실행 조건: cwd={diff_root} "
        "· env=위임 채널과 같은 seam(pm_delegate.build_env)",
        file=sys.stderr,
    )

    print(f"추가 리뷰어 실행 중 (유료 호출): {reviewer_cmd}", file=sys.stderr)
    # 예약 소유는 `run_review` **진입**이 아니라 watchdog 워치독이 자식을 띄우기 **직전**에 넘어간다
    # (`on_spawn_attempt`). run_review 의 앞부분(raw 선점·장부 시작 레코드·구조화 transport·
    # argv/kwargs 준비)과 러너 안의 준비 구간(프로필 해소·워치독 셋업)은 자식이 아직
    # 없는 구간이라, 진입에서 넘기면 스폰 0·과금 0 으로 끝난 준비 실패(예: `--output-dir` 이
    # 일반 파일이라 raw 선점이 터지는 실행, 실행 권한 없는 리뷰어 바이너리)가 예약을 미완/소진으로
    # 남긴다 — `incomplete_limit=1` 이면 다음 **정상** 호출이 곧바로 rc=4 로 막힌다.
    # 이전 이후의 실패는 프롬프트가 이미 나갔을 수 있으므로 환불하지 않는다: 되돌림 판정은 아래
    # 마감 경로의 `started` 가 소유한다(타임아웃·비정상 종료까지 환불하면 과금된 호출이 상한을
    # 소비하지 않아 무한 우회가 열린다·MF-A).
    result = reviewer_run.run_review(
        home, prompt=prompt, target=target,
        timeout=timeout, output_dir=output_dir, idle_timeout=idle_timeout,
        local_conf_path=conf_path, resolved_profile=profile,
        cwd=diff_root,
        env=reviewer_environment,
        on_spawn_attempt=reservation.hand_off,
        on_no_spawn=reservation.reclaim_no_spawn,
    )
    started = bool(result.get("started", True))
    if started:
        # 스폰된 실행의 예약은 아래 마감 경로가 소유한다(소유권은 스폰 시도 콜백이 이미 넘겼다 —
        # 여기 한 줄은 그 불변식을 이 자리에서 못 박는 멱등 재확인이다). 이후 어디서 죽어도
        # 환불하지 않는다: 프롬프트가 이미 나갔을 수 있어 되돌리면 과금된 호출이 상한을 소비하지
        # 않는다(무한 우회·MF-A).
        reservation.hand_off()
    # started=False 는 반대다 — 러너가 "자식 없음"으로 판명해 돌아온 실행이라 `on_no_spawn` 이
    # 소유권을 이 seam 으로 되돌려 놨다. 아래 요약·진단·마감이 **끝날 때까지** 그 상태를 유지한다:
    # 여기서 미리 넘기면 요약 출력이 닫힌 stdout 파이프로 죽는 순간(BrokenPipeError) 호출 0·과금 0
    # 인 실행의 예약이 미완으로 남아 `incomplete_limit=1` 형상에서 다음 정상 호출을 막는다.
    print_summary(result, gate=args.gate)

    # 정상 복귀한 호출은 판정 유무와 별개로 종료 마감한다. 프로세스 kill처럼 이 지점에 도달하지
    # 못한 레코드는 finished_at 없이 남아 다음 호출에서 미완 재시도 예산으로 식별된다.
    if budget.reserved:
        # 산출 파싱·시각은 락 **밖**에서 끝낸다 — 임계 구역은 장부 read-modify-write 만 잡는다.
        # 예약 identity(id·sequence)는 예약 시점 값이라 마감 시점 장부 상태에 의존하지 않는다.
        outcome = (
            reviewer_reservation._round_outcome(result, record={
                "id": budget.round_id, "sequence": budget.sequence,
                "started_at": budget.started_at, "target_rev": budget.target_rev,
            })
            if started else None
        )
        # 항목 텍스트를 산출 파싱과 같은 자리·같은 함수에서 남겨 건수와 텍스트가 갈리지 않게 한다.
        # 저장 지점은 예약 레코드 안이라 정규화(`_gate_entry`)에서 살아남는다.
        must_fix_items = reviewer_budget._round_must_fix_items(result) if started else None
        refunded_round = False
        try:
            with review_rounds._round_ledger_lock(home):
                ledger = review_rounds._load_round_ledger(home)
                if not started:
                    # 호출이 확실히 없던 라운드 — 두 예산(게이트 count·wave spent)을 같은 조건으로
                    # 되돌린다(스폰 전 실패 환불과 **같은 기계**). 산출도 남기지 않는다
                    # (리뷰어가 아무 말도 하지 않았다).
                    refunded_round = reviewer_reservation._refund_reserved_round(ledger, budget)
                elif outcome is not None:
                    entry = reviewer_reservation._gate_entry(ledger, budget.gate)
                    matching = next(
                        (row for row in entry["records"]
                         if row.get("id") == budget.round_id),
                        None,
                    )
                    if matching is not None:
                        matching["finished_at"] = clock.now_utc()
                        matching["verdict"] = reviewer_budget._round_has_verdict(result)
                        if must_fix_items is not None:
                            matching["must_fix_items"] = must_fix_items
                    # 산출 기록은 예약 레코드 유무와 무관하다 — 호출된 라운드는 무조건 남긴다
                    # (승인이 집계 창을 비워도 "무엇이 나왔는가"는 이력으로 남아야 한다).
                    # 예약 identity 를 함께 실어 동시 완료에서도 라운드↔산출 연결이 확정된다.
                    reviewer_reservation._append_round_outcome(entry, outcome)
                reviewer_reservation._save_round_ledger(home, ledger)
            if not started:
                # 되돌린 라운드는 **말하고** 되돌린다 — 조용히 환불하면 채택자에게 남는 사실이
                # "장부가 그대로다" 뿐이라, 교차검증이 아예 돌지 않은 실행과 리뷰어를 선언하지
                # 않은 실행이 같은 모양이 된다(Windows 경로 분해 결함이 30여 릴리즈 숨은 경로).
                if refunded_round:
                    print(
                        f"라운드 환불: 게이트 {budget.gate} — 리뷰어 프로세스가 시작되지 않아 "
                        "추가 리뷰어 교차검증이 **실행되지 않았습니다**(호출 0·상한/wave 예산 "
                        f"미소진). 사유: {reviewer_budget._first_output_line(result) or '미상'}",
                        file=sys.stderr,
                    )
                # 환불이 **저장까지 끝난** 뒤에야 소유권을 반납한다 — 이 줄 앞에서 죽으면 바깥
                # seam 이 아직 소유자라 한 번 되돌리고, 이 줄 뒤에는 되돌릴 것이 없다. 저장이
                # 실패한 경로(아래 OSError)는 반납하지 않는다: 되돌리지 못한 예약의 소유자는
                # 여전히 스폰 전 seam 이고, 그 경로는 예외 없이 정상 return 하므로(바깥
                # `__exit__` 가 보지 못한다) 거기서 직접 한 번 되돌린다.
                reservation.settle_refunded()
        except OSError as exc:
            # 마감은 이미 *끝난* 호출의 기록이다 — 여기서 rc 를 바꾸면 리뷰 판정이 락 사정으로
            # 뒤집힌다. loud 경고만 남기고 판정 종료코드를 그대로 돌려준다. 마감 못 한 레코드는
            # finished_at 없이 남아 다음 실행의 미완 재시도 예산으로 보수적으로 집계된다.
            #
            # 단 `started=False` 는 예외다 — 자식이 뜬 적 없다고 **판명된** 실행이라 예약 소유는
            # 아직 스폰 전 seam 에 있다(`on_no_spawn` 이 되돌려 놨고 반납은 저장 성공 뒤에만
            # 선다). 이 경로는 예외를 삼키고 **정상 return** 하므로 `__exit__` 가 그 소유를 보지
            # 못한다: 그대로 두면 호출 0·과금 0 인 실행이 라운드·wave 예산을 먹은 채 미완으로
            # 남아, `incomplete_limit=1` 형상에서 다음 **정상** 재시도가 곧바로 rc=4 로 막힌다.
            # 그래서 같은 환불 기계로 한 번 더 되돌린다 — 락이 그새 풀리면 예산이 복구되고,
            # 저장은 됐는데 락 해제만 실패한 형상이면 되돌릴 레코드가 없어 `_refund_round` 가
            # False 를 내므로 이중 환불이 되지 않는다. 소유는 여전히 한 seam 이다(`release` 는
            # 한 번만 선다). 스폰된 실행(started=True)은 종전대로 건드리지 않는다 — 프롬프트가
            # 이미 나갔을 수 있어 되돌리면 과금된 호출이 상한을 소비하지 않는다(MF-A).
            refunded = not started and reservation.release(
                reason="스폰 없이 끝난 실행의 장부 마감 실패")
            print(
                f"경고: 라운드 장부 마감 실패 ({type(exc).__name__}: {exc}) — 다른 게이트 "
                "실행이 장부 락을 보유 중일 수 있습니다. "
                + ("호출이 없던 라운드라 예약은 되돌렸습니다(상한·wave 예산 미소진)."
                   if refunded else
                   "이 라운드는 미완으로 남아 다음 실행의 재시도 예산에서 세어집니다.")
                + f" (장부: {review_rounds._round_ledger_path(home)})",
                file=sys.stderr,
            )

    rc = reviewer_budget.determine_exit_code(result)
    # 라운드 장부 마감 **뒤**에 회수한다 — 장부는 종전대로 기록되고, 회수 실패는 판정 rc 를
    # 덮지 않고 rc=0 인 실행만 실패로 올린다. 대상은 `--ticket` 또는 보드에 실재하는 ticket
    # 형상 `--gate` 이고, 대상이 있는데 회수하지 않은 실행은 사유를 반드시 말한다(조용한 누락 금지).
    harvest_ticket, target_problem = _harvest_target_ticket(home, args, )
    if target_problem is not None:
        print(f"경고: 추가 리뷰어 산출 미회수 — {target_problem}", file=sys.stderr)
    elif harvest_ticket and not (started and not result.get("failed")):
        print(
            f"경고: 추가 리뷰어 산출 미회수 — {harvest_ticket}: 이번 라운드는 "
            "리뷰어 회신을 받지 못했습니다(호출 실패·타임아웃). 산출 없음이라 라운드 파일을 "
            f"만들지 않습니다 · raw={result.get('file')}",
            file=sys.stderr,
        )
    if harvest_ticket and started and not result.get("failed"):
        problem = _harvest_additional_reviewer_section(
            home, harvest_ticket, result, )
        if problem is not None:
            print(
                f"오류: 추가 리뷰어 산출 회수 문제 — {problem}\n"
                f"  · 라운드 파일은 만들지 않았습니다 · 산출 원문은 raw 에 보존됩니다: "
                f"{result.get('file')}\n"
                "  · PM 판정 전에 리뷰어에게 구조화 블록을 갖춘 회신을 다시 받으세요.",
                file=sys.stderr,
            )
            rc = rc or 1
    return rc
