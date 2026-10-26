"""추가 리뷰어 공개 CLI — 인자 파서·게이트 회계·리뷰 실행 배선 (층: cli).

진입 파일 additional_reviewer.py 가 설치 앵커 Home 을 넘긴다. 엔진 루트 비교·표시는 그 Home 의
root 를 기준으로 한다.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

import pm_engine.domain.reviewer_prompt
import pm_engine.infra.harness.reviewer_run
from pm_engine.app import reviewer_harvest
from pm_engine.app import reviewer_prompt
from pm_engine.app import reviewer_reservation
from pm_engine.app import reviewer_scope
from pm_engine.cli import reviewer_rounds
from pm_engine.domain import delegate_errors
from pm_engine.domain import reviewer_budget
from pm_engine.domain import reviewer_result
from pm_engine.infra import conf_store, pm_paths, review_diff, reviewer_target, round_store


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="추가 리뷰어 래퍼 — 추가 리뷰어 하네스 어댑터 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 기본 (HEAD 기준 변경, local.conf additional_reviewer.paths/기본 경로) — 회계 밖 실행을 명시
  python3 .project_manager/tools/additional_reviewer.py --no-gate

  # ticket 의 touches 로 경로 결정 (--gate 미지정이면 그 티켓으로 게이트 자동 유도·장부 기록)
  python3 .project_manager/tools/additional_reviewer.py --ticket T-0259

  # 게이트 회계 밖 자문 실행 (명시 opt-out · 장부 미기록·예산 미소모)
  python3 .project_manager/tools/additional_reviewer.py --ticket T-NNNN --no-gate

  # 특정 base 와 경로·게이트 지정
  python3 .project_manager/tools/additional_reviewer.py --base main --paths src/ tests/ --gate T-NNNN

  # dry-run (좌표·프롬프트만 출력, 호출 안 함 — 비허용이어도 허용)
  python3 .project_manager/tools/additional_reviewer.py --dry-run

  # 라운드 장부 조회 (호출 없음 — 게이트별 라운드 수·라운드별 산출·처분·wave spent)
  python3 .project_manager/tools/additional_reviewer.py --rounds-report --gate T-NNNN

  # 게이트 처분 선언 (호출 없음 — 릴리즈 게이트가 읽는 잔여 must-fix 소화 기록)
  python3 .project_manager/tools/additional_reviewer.py --resolve-gate T-NNNN --pm-verified
  # --pm-verified 는 PM 판정 표면의 기계 확인 증거로 해소한다(재호출 없음).
  # --resolve-gate 와 --dry-run 조합은 기록 목적과 모순이라 rc1로 거부.

추가 리뷰어 대상(원자 tuple · 정상 경로):
        additional_reviewer.harness=codex
        additional_reviewer.model=gpt-5.6-sol
        additional_reviewer.reasoning=max     (선택 · 하네스별 허용집합 검증)
        harness/model 은 동반 필수다. 이 tuple 이 없으면 대상이 없어 fail-loud 다
        (모델을 고정하지 않는 실행 경로는 폐지됐다).
과금 상한: 호출마다 비용을 다시 묻지 않는다 — 무한 라운드는 라운드/wave 예산이 기계로 막는다.
""",
    )
    parser.add_argument("--base", default="HEAD",
                        help=("git diff 기준 ref (기본: HEAD — 작업트리"
                              "(스테이징+언스테이징), 비면 직전 커밋 HEAD~1..HEAD)"))
    parser.add_argument("--paths", nargs="+", default=None,
                        help="검토 대상 경로 (기본: local.conf additional_reviewer.paths / src tests scripts ...)")
    parser.add_argument("--ticket", default=None, metavar="T-NNNN",
                        help="ticket ID — touches 로 검토 경로 결정. --gate 미지정이면 이 값이 "
                             "게이트로 자동 유도돼 라운드가 장부에 기록·집계된다")
    parser.add_argument("--gate", default=None, metavar="T-NNNN",
                        help="게이트 ticket 표식 (로깅 + 라운드 상한 장부 키) — 미지정이면 "
                             "--ticket 에서 자동 유도한다(명시 값이 항상 우선)")
    parser.add_argument("--no-gate", action="store_true",
                        help="게이트 회계 opt-out — --ticket 자동 유도를 끄고 라운드 장부 기록·"
                             "라운드/wave 예산 집계 없이 실행한다 (회계 밖 자문 실행·loud 표기 · "
                             "--gate 와 함께 쓸 수 없음)")
    parser.add_argument("--ack-rounds", action="store_true",
                        help="(폐지됨) 라운드 연장 승인 — 호출하면 아무것도 하지 않고 거부한다. "
                             "현재 티켓의 고정 fix 단계에서 종결해야 한다.")
    parser.add_argument("--ack-wave", action="store_true",
                        help="wave 예산 재개 — wave spent 를 0 으로 리셋 후 재개 "
                             "(게이트 지정 필수 — --gate 또는 --ticket 유도 · 같은 범위의 "
                             "정상 수렴이면 PM 이 자율 판단)")
    parser.add_argument("--rounds-report", action="store_true",
                        help="라운드 장부 조회 — 게이트별 라운드 수·라운드별 판정/결함 수·처분·wave "
                             "spent 를 출력하고 종료 (호출 없음·--gate 로 한 게이트만)")
    parser.add_argument("--resolve-gate", default=None, metavar="T-NNNN",
                        help="게이트 처분 선언 — 반려로 끝난 게이트의 잔여 must-fix 를 어떻게 "
                             "소화했는지 장부에 남기고 종료 (호출 없음). `--pm-verified` "
                             "필수. 릴리즈 게이트(`board.py livegate "
                             "record`)가 이 선언을 읽어 미처분 잔여를 차단한다")
    parser.add_argument("--pm-verified", action="store_true",
                        help="--resolve-gate 처분: PM 판정 표면(리뷰 delta)의 기계 확인 증거로 "
                             "해소 — 재호출 없음(`pm_delegate rounds resolve --pm-verified` "
                             "와 같은 이름·같은 의미). 발동 조건: 이 채널 accepted 잔여 0·이 채널 "
                             "잔여 must-fix 건수만큼 판정 표면 전건 처분(미판정 0)·이 채널에 "
                             "accepted 가 있었을 때만 기계 확인 1건 이상")
    parser.add_argument("--dry-run", action="store_true",
                        help="좌표·프롬프트만 출력, 리뷰어를 부르지 않음 (빈 diff 면 "
                             "exit 1). --resolve-gate 는 기록 명령이므로 함께 쓰면 exit 1")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="리뷰 원문 저장 디렉토리"
                             " (기본: .project_manager/.local/review)."
                             " 실제로 리뷰어를 부르는 실행에서만 생성된다"
                             " (미리보기·게이트 거부는 만들지 않음)")
    parser.add_argument("--timeout", type=reviewer_target._timeout_seconds_arg, default=None,
                        metavar="SEC",
                        help="리뷰어 실행 벽시계 백스톱(초) — 기본은 해소 대상의 하네스 프로필. "
                             "local.conf harness.<reviewer>.wall_timeout 또는 "
                             f"{reviewer_budget.ADDITIONAL_REVIEWER_TIMEOUT_KEY} 로 조정")
    parser.add_argument("--idle-timeout", type=reviewer_target._timeout_seconds_arg, default=None, metavar="SEC",
                        help="무진행 상한(초) — 마지막 진행 출력 이후 이 시간 침묵하면 중단(주 판정). "
                             "local.conf harness.<reviewer>.idle_timeout 또는 "
                             f"{reviewer_budget.ADDITIONAL_REVIEWER_IDLE_TIMEOUT_KEY} 로 조정")
    parser.add_argument("--adr", nargs="+", default=None, metavar="ADR-NNNN",
                        help="관련 ADR 목록 (프롬프트에 포함)")
    return parser


def main(home, argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    engine_repo = home.root.resolve()
    engine_home = pm_paths.home_at(engine_repo)
    # 게이트 이름 검증이 **가장 먼저**다 — 예약 키 충돌은 호출·장부 접근 뒤에 발견하면 이미
    # 두 상한이 서로를 덮어쓴 뒤다(조회면도 같은 이름을 게이트로 부르지 않게 함께 막는다).
    reserved_gate_error = reviewer_budget._reserved_gate_error(args.gate)
    if reserved_gate_error is not None:
        print(reserved_gate_error, file=sys.stderr)
        return 1
    # 폐지된 연장 승인은 **아무 표면에서도** 통하지 않는다 — 조회면 무시 경고로 흡수하면 "쓰긴
    # 썼는데 안 먹었다"가 남아 규율이 흐려진다. 부작용 0 지점(장부 접근·호출 전)에서 거부한다.
    if args.ack_rounds:
        print(reviewer_budget._ACK_ROUNDS_REMOVED_GUIDANCE, file=sys.stderr)
        return 1
    # 게이트 회계 자동 유도 — `--ticket` 실행의 기본값은 "기록"이다(조용한 무기록 폐지).
    gate_derivation = reviewer_reservation._derive_gate_from_ticket(args)
    if gate_derivation.refusal is not None:
        print(gate_derivation.refusal, file=sys.stderr)
        return 1
    # 처분 인자는 `--resolve-gate` 없이는 뜻이 없다 — 선언할 게이트가 없으면 남길 사실도 없다.
    if args.pm_verified and not args.resolve_gate:
        print(
            reviewer_budget._RESOLVE_GATE_REQUIRED_GUIDANCE.format(flag="--pm-verified", value=""),
            file=sys.stderr,
        )
        return 1
    if args.resolve_gate:
        # 장부 기록 전용면 — 실행 경로(conf 해소·diff 추출)보다 앞에서 끝낸다.
        return reviewer_rounds._resolve_gate_command(engine_home, args)
    if args.rounds_report:
        ignored = ", ".join(
            flag for flag, given in (
                ("--ack-wave", args.ack_wave),
                # 조회면은 호출도 예약도 없어 회계 자체가 없다 — opt-out 도 무시 대상이다.
                ("--no-gate", args.no_gate),
                ("--dry-run", args.dry_run),
            ) if given
        )
        if ignored:
            print(f"경고: --rounds-report 는 조회 전용이라 다음 플래그를 무시합니다: {ignored}.",
                  file=sys.stderr)
        if not (args.paths or args.ticket):
            # selector 없는 조회는 여기서 끝낸다 — 장부만 읽으므로 검토 경로가 없거나 diff 가
            # 비어도(빈-diff 가드) 답을 내야 하고, 그러려면 그 게이트들보다 앞서야 한다.
            # selector 를 준 조회는 기록면과 **같은 해소**를 타야 해서 아래 앵커 해소 뒤로 간다.
            return reviewer_rounds._print_rounds_report(pm_paths.home_at(engine_repo, pm_home=(owner := pm_paths.resolve_pm_home_for_repo(engine_repo)), board_root=pm_paths._board_root_at(owner)), gate=args.gate)
    ticket_selected = bool(args.ticket and not args.paths)
    explicit_paths = bool(args.paths)
    ticket_body_source: Path | None = None
    # plain list는 production parser가 만들지 않는다. 기존 회귀 fixture가 touches seam만 주입하고
    # 실 ticket 파일은 제공하지 않은 형상을 명시적으로 식별하는 테스트 전용 호환 축이다.
    ticket_scope_fixture_injected = False
    try:
        engine_pm_home = pm_paths.resolve_pm_home_for_repo(engine_repo)
        scope_from_initial_pm_home = pm_paths._scope_from_initial_pm_home(
            ticket_selected=ticket_selected, explicit_paths=explicit_paths,
        )
        # **명시 앵커(--paths/--ticket) 실행은 선택 전 config를 읽지 않는다.** 이번 범위가 그
        # config에서 나오지 않아 읽을 이유가 없고, 읽는 행위 자체가(판독 불가 시) diff 대상 해소
        # *전* 중단이 되어 다른 repo를 가리키는 절대 --paths 복구 채널을 자기잠근다. 그 실행의
        # config는 아래에서 **선택된 소유자**로부터 한 번만 읽는다 — 소비처(reviewer_cmd·타임아웃·
        # 라운드 상한)는 모두 해소 뒤라 읽는 시점만 옮겨지고 값의 출처는 종전과 같다.
        # config를 읽는 조건은 아래 **분기 자신**이다(판정 헬퍼 결과가 아니라) — 범위 원천 분기와
        # 로드 조건이 따로 놀 수 없게.
        conf: dict[str, str] | None = None
        if ticket_selected:
            parsed_touches = round_store.parse_ticket_touches(pm_paths.home_at(engine_repo, pm_home=engine_pm_home, board_root=pm_paths._board_root_at(engine_pm_home)), args.ticket, )
            ticket_body_source = getattr(parsed_touches, "source", None)
            ticket_scope_fixture_injected = ticket_body_source is None
            raw_paths = tuple(parsed_touches)
            if not raw_paths:
                raise pm_paths.AnchorResolutionError(
                    f"board anchor {engine_pm_home}: ticket {args.ticket} 의 touches가 비어 있어 "
                    "검토 범위를 확정할 수 없습니다."
                )
        elif args.paths:
            raw_paths = tuple(args.paths)
        else:
            # 인자 없는 실행 — 이 config가 **범위의 원천**이라 파생 전에 읽는다.
            conf = conf_store.local_config(pm_paths.home_at(engine_pm_home))
            raw_paths = tuple(reviewer_target._configured_paths(conf))
        diff_root = reviewer_scope._resolve_diff_root(
            pm_paths.home_at(engine_repo, pm_home=engine_pm_home, board_root=pm_paths._board_root_at(engine_pm_home)), engine_repo,
            paths=raw_paths,
            base=args.base,
            ticket_selected=ticket_selected,
        )
        pm_home = (
            engine_pm_home
            if diff_root.resolve() == engine_repo
            else pm_paths.resolve_pm_home_for_repo(diff_root)
        )
        if ticket_selected and pm_home != engine_pm_home:
            raise pm_paths.AnchorResolutionError(
                "ticket board 소유 PM 홈과 diff worktree의 lease 소유 PM 홈이 다릅니다: "
                f"board={engine_pm_home} diff-owner={pm_home}"
            )
        if scope_from_initial_pm_home and pm_home != engine_pm_home:
            raise pm_paths.AnchorResolutionError(
                "인자 없는 실행의 검토 범위(local.conf additional_reviewer.paths 또는 엔진 고정 기본 경로)를 "
                "최초 PM 홈에서 파생해 diff_root를 선택했지만, 해소된 diff 소유 PM 홈이 다릅니다. "
                "config provenance와 실제 호출 범위를 일치시킬 수 없어 호출 전에 중단합니다: "
                f"initial-pm-home={engine_pm_home} diff-owner-pm-home={pm_home}\n"
                "  · additional_reviewer.paths 선언 유무와 무관한 같은 기준입니다 — 기본 경로도 최초 PM 홈 "
                "config를 읽어 얻은 이번 실행의 범위입니다.\n"
                "  · 절대 `--paths <경로>`로 이번 검토 범위를 직접 지정하거나, 유효한 "
                "`--ticket T-NNNN`(touches 채워진 것)으로 범위를 파생시키세요.\n"
                "  · 두 PM 홈이 같은 슬롯을 등록한 상태라면 lease 장부의 슬롯 등록을 정리하세요."
            )
        selected_paths = reviewer_scope._normalize_review_paths(
            pm_paths.home_at(diff_root, pm_home=pm_home, board_root=pm_paths._board_root_at(pm_home)), raw_paths,
            diff_root=diff_root,
            ticket_selected=ticket_selected,
        )
    except (pm_paths.AnchorResolutionError, OSError, UnicodeError) as exc:
        print(f"오류: 추가 리뷰 앵커 해소 실패 — {exc}", file=sys.stderr)
        return 1

    # 리뷰 코드와 PM 데이터 소유자를 별도 좌표로 보존한다.
    review_home = pm_paths.home_at(
        diff_root, pm_home=pm_home, board_root=pm_paths._board_root_at(pm_home),
    )
    if args.rounds_report:
        # selector 를 준 조회 — 기록면이 쓸 장부(diff 소유 PM 홈)를 그대로 읽는다. 여기서 끝내
        # 실행 경로(conf 해소·diff 추출)는 타지 않는다.
        return reviewer_rounds._print_rounds_report(review_home, gate=args.gate, )
    if conf is None:
        # 명시 앵커 실행의 config는 여기서 **처음이자 유일하게** 읽는다. 인자 없는 실행은 범위 파생
        # 지점에서 이미 읽었다.
        try:
            conf = conf_store.local_config(pm_paths.home_at(pm_home))
        except (OSError, UnicodeError) as exc:
            conf_path = (pm_home / ".project_manager" / "local.conf").resolve()
            print(
                "오류: 해소된 local.conf 읽기 실패 — 실행 전에 중단합니다: "
                f"{conf_path} ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
            return 1
        except pm_paths.AnchorResolutionError as exc:
            print(f"오류: 추가 리뷰 앵커 해소 실패 — {exc}", file=sys.stderr)
            return 1
    # 시간 예산은 **해소된 대상의 하네스 프로필**을 따른다 — 대상 command 를 먼저 해소해야
    # 어떤 축(클라우드/로컬)의 값을 쓸지 정해진다. 깨진 conf의
    # fail-soft 경고는 해소 중 발생하지만 stderr 첫 줄 provenance 계약을 지키도록 잠시 보류한다.
    # 대상 해소가 시간 예산보다 **먼저**다 — 잘못된 프로필(부분 tuple·미지원 값·대상 이중 선언)은
    # output-dir 생성·라운드 예약·raw 예약·과금 문구 어느 것도 만들기 전에 끊는다.
    # 여기까지의 앵커 해소는 읽기 전용이고, 이 함수 자체도 부작용이 없다.
    try:
        target = reviewer_target.resolve_reviewer_target(conf)
        reviewer_target.check_reviewer_reasoning(target, cwd=diff_root)
    except delegate_errors.ReviewerTargetError as exc:
        print(
            f"오류: 추가 리뷰어 대상 해소 실패 — 호출 전에 중단합니다: {exc}\n"
            f"  · 설정 파일: {(pm_home / ".project_manager" / "local.conf").resolve()}",
            file=sys.stderr,
        )
        return 1
    reviewer_cmd = target.command
    resolution_warnings = io.StringIO()
    with contextlib.redirect_stderr(resolution_warnings):
        resolved_time_profile = reviewer_target.reviewer_profile(review_home, reviewer_cmd, conf)
    timeout = (
        args.timeout
        if args.timeout is not None
        else int(resolved_time_profile.wall_timeout)
    )
    idle_timeout = (
        float(args.idle_timeout)
        if args.idle_timeout is not None
        else float(resolved_time_profile.idle_timeout)
    )
    conf_path = (pm_home / ".project_manager" / "local.conf").resolve()
    profile = reviewer_target.resolved_reviewer_profile(target, timeout, idle_timeout)
    target_repo = diff_root
    # 정상 실행·dry-run 공용 첫 provenance. 이후 cap/reanchor/diff 진단이 붙더라도 어느 PM-home conf와
    # reviewer tuple을 해소했는지가 stderr에 남는다.
    print(
        f"[additional-reviewer] config provenance: local_conf={conf_path} "
        f"· diff_root={diff_root} · pm_home={pm_home} · resolved_profile={profile}",
        file=sys.stderr,
    )
    if gate_derivation.notice is not None:
        # 게이트 유도 고지는 **provenance 다음**이다 — 첫 줄 계약(그 자리에서 어느 conf·어느
        # diff_root 로 해소했는지 읽는다)을 이 고지가 밀어내면 안내 문서와 실제 출력이 갈린다.
        print(gate_derivation.notice, file=sys.stderr)
    deferred_warnings = resolution_warnings.getvalue()
    if deferred_warnings:
        print(deferred_warnings, end="", file=sys.stderr)

    try:
        divergence = reviewer_target.local_conf_divergence(
            engine_repo=pm_home,
            engine_conf=conf,
            target_repo=target_repo,
            selector=reviewer_target.reviewer_profile_config,
            engine_label="pm-home",
            target_label="diff-worktree",
        )
    except reviewer_result.TargetLocalConfReadError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if divergence is not None:
        print(
            reviewer_result.format_local_conf_divergence(
                divergence,
                surface="additional_reviewer",
                cwd_label="diff worktree",
                source_label="해소된 PM 홈",
                resolution_note=(
                    "이번 실행에서는 --paths/--ticket에서 해소된 PM 홈 conf가 적용됩니다: "
                    f"{divergence.engine_conf_path}\n"
                    "  차단하지 않고 계속합니다. 같은 프로필을 원하면 diff worktree conf의 위 "
                    "키 값을 PM 홈 conf와 맞추세요. 다른 PM 홈 conf를 선택하려면 --paths/--ticket이 "
                    "가리키는 등록 worktree와 lease 소유 관계를 확인하세요."
                ),
            ),
            file=sys.stderr,
        )
    cap_warning = pm_engine.infra.harness.reviewer_run.harness_cap_advisory(
        execution_budget=pm_engine.infra.harness.reviewer_run._reviewer_execution_budget(review_home, reviewer_cmd, timeout)
    )
    if cap_warning is not None:
        print(cap_warning, file=sys.stderr)

    # `--output-dir` 은 **경로 해소만** 여기서 한다(위임 표면과 같은 규칙). 디렉토리 생성은
    # 부작용이므로 실제로 raw 를 예약하는 지점(`_reserve_output`)이 소유한다 — 그래야 미리보기·
    # 라운드 상한 거부처럼 **리뷰어를 부르지 않고 끝나는 실행**이 요청한
    # 디렉토리를 만들지 않는다(빈 디렉토리를 남기면 "아무 일도 없었다"가 파일시스템에서 거짓이
    # 되고, 사람이 그 자리를 산출물 위치로 오독한다).
    output_dir: Path | None = Path(args.output_dir) if args.output_dir else None

    # 슬롯 선택과 diff 추출은 위에서 확정한 동일 path 집합을 쓴다.
    paths = list(selected_paths)
    print(f"검토 경로: {paths}", file=sys.stderr)
    print(f"base: {args.base}", file=sys.stderr)

    try:
        diff_result = review_diff.extract_diff(review_home, args.base, paths)
        diff = diff_result.text
    except RuntimeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    # 빈-diff fail-loud 가드 (diff 추출 직후·codex invoke 전). 빈 diff(공백-only 포함)를
    # 리뷰하면 가짜 통과(false-green)가 나므로 어떤 형상·모드에서도 무조건 fail 한다 — 우회
    # 플래그 없음. 기존 오류 규약(비-0 = 1)과 정합. dry-run 보다 앞서므로 잘못된 형상(worktree
    # 아닌 곳에서 실행 등)은 리뷰어를 부르지 않고 미리보기 단계에서도 드러난다.
    if not diff.strip():
        print(reviewer_budget._empty_diff_guidance(paths, root=diff_root), file=sys.stderr)
        return 1

    # 티켓 본문은 실제 프롬프트가 필요한 dry-run 또는 호출 확정 구간에서만 읽는다.
    # 빈 diff 조기 종료는 본문 파일이나 byte 상한을 건드리지 않는다. ticket touches를
    # 실제 파일에서 읽은 경우 그 exact source를 재사용하고, 명시 --paths 또는 테스트 fixture의
    # plain-list touches seam은 ticket 파일 부재가 scope 해소를 자기잠그지 않게 본문 없이 진행한다.
    ticket_body: str | None = None
    ticket_body_bytes = 0
    ticket_body_omitted_rounds = 0

    def prepare_ticket_body() -> bool:
        nonlocal ticket_body, ticket_body_bytes, ticket_body_omitted_rounds
        if not args.ticket:
            return True
        try:
            ticket_file = (
                ticket_body_source if ticket_body_source is not None
                else round_store._find_ticket_file(review_home, args.ticket, )
            )
            raw_text, raw_body = round_store._load_ticket_text_and_body(ticket_file)
            rounds = reviewer_prompt._load_ticket_rounds_for(
                review_home, args.ticket, ticket_text=raw_text,
            )
            # 시드 그대로인 developer 라운드 위에서 스폰되는지 loud 표시 — 준비면
            # (`round_seed._warn_unharvested_developer_round`)과 같은 축(이 seam 은
            # dry-run·실 스폰 두 호출을 모두 지난다).
            reviewer_prompt._warn_seed_developer_round(rounds, ticket=args.ticket)
            # 입력 선별 — 명세는 전량, 라운드는 역할별 마지막 산출만(파일 선택).
            selection = pm_engine.domain.reviewer_prompt._select_ticket_body_for_review(raw_body, rounds)
            if selection.omitted_rounds > 0:
                header = round_store._ticket_body_selection_header(ticket_file, args.ticket)
                composed_body = f"{header}\n\n{selection.text}"
            else:
                # 생략된 라운드가 없으면 요약 헤더 없이 원문 그대로 — 회귀 불변(DoD (e)).
                composed_body = selection.text
        except (pm_paths.AnchorResolutionError, OSError, UnicodeError) as exc:
            if args.paths:
                print(
                    "경고: 명시 --paths가 검토 범위를 소유해 "
                    f"{args.ticket} 본문을 읽지 못했지만 본문 없이 계속합니다: {exc}",
                    file=sys.stderr,
                )
                ticket_body = None
                return True
            if ticket_scope_fixture_injected:
                print(
                    "경고: 테스트 fixture가 ticket touches seam만 주입하고 본문 provenance는 "
                    f"제공하지 않아 {args.ticket} 본문 없이 계속합니다: {exc}",
                    file=sys.stderr,
                )
                ticket_body = None
                return True
            print(f"오류: 게이트 티켓 본문 로딩 실패 — {exc}", file=sys.stderr)
            return False
        ticket_body = composed_body
        ticket_body_bytes = len(ticket_body.encode("utf-8"))
        ticket_body_omitted_rounds = selection.omitted_rounds
        return True

    def compose_prompt() -> str:
        """이번 실행의 표준 리뷰 프롬프트."""
        return reviewer_prompt.build_prompt(
            review_home, tree=diff_root, base=diff_result.base, target=diff_result.target,
            changed_paths=diff_result.paths, read_commands=diff_result.read_commands,
            ticket_body=ticket_body, ticket_id=args.ticket,
            adr_refs=args.adr, gate=args.gate,
            next_finding_id=reviewer_harvest._next_external_finding_id(review_home, args, ),
            confirmation_ids=[],
        )

    if args.dry_run:
        if not prepare_ticket_body():
            return 1
        prompt = compose_prompt()
        # 미리보기는 **부작용 0**이다(호출·raw 예약·라운드 예약·`--output-dir`
        # 생성 모두 없음). 여기까지의 준비는 전부 읽기 전용이고(conf 해소·git diff),
        # 본문은 빈 변경 검사에만 쓰며 미리보기에는 실제 전송할 좌표와 검토 맥락을 표시한다.
        # 해소 대상 표시는 stderr 첫 provenance·raw 헤더와 **같은 문자열**을 쓴다 — 세 표면이 서로
        # 다른 말을 하면 "미리보기로 확인한 대상"과 "실제 나간 대상"을 대조할 수 없다.
        print("=== [dry-run] 추가 리뷰어 대상 (호출 없음) ===")
        print(f"local_conf: {conf_path}")
        print(f"resolved_profile: {profile}")
        print(f"command: {target.command}")
        if ticket_body is not None:
            print(
                f"ticket_body_bytes: {ticket_body_bytes} · "
                f"생략 라운드: {ticket_body_omitted_rounds}개"
            )
        else:
            # 본문 미조립은 정상 형상(예: `--paths` 로 검토 범위를 대체하고 `--ticket` 은
            # 안 준 실행)이지만, 침묵하면 "본문이 실렸다고 착각한 채 결과를 읽는" 경로가
            # 열린다 — 정상 형상도 미조립이라고 값으로 말한다.
            print("ticket_body: 미조립 — 이번 리뷰어 입력에 게이트 티켓 본문이 실리지 않습니다")
        print("=== [dry-run] 프롬프트 미리보기 (호출 없음) ===")
        print(prompt)
        print("=== [dry-run] 리뷰어 호출 생략 ===")
        return 0

    # ── 라운드 상한·wave 예산 게이트: 호출 전 예약 ──────────
    # raw 선점보다 **먼저**다 — 이미 상한에 닿은 호출이 raw 자리를 만들었다 지우면 "호출 없이
    # 끝나는 실행은 부작용 0" 규율(dry-run 과 같은 축)을 이 rc 에서만 깨는 것이다.
    # dry-run·조회·처분은 이미 반환했다. 여기까지 온 실행은
    # 리뷰어를 부르므로, `--gate`(또는 `--ticket` 자동 유도) / 명시 `--no-gate` 중 하나가 반드시
    # 있어야 한다. 둘 다 없으면 예약·raw·리뷰어 스폰 전 부작용 0 지점에서 fail-loud 한다.
    if not args.gate and not args.no_gate:
        print(reviewer_budget._GATE_ACCOUNTING_REQUIRED_GUIDANCE, file=sys.stderr)
        return 1

    # 모든 무호출 조기 종료를 지난 뒤, 라운드 예약(부작용) 전 마지막 입력 경계에서 본문을 읽고
    # 상한을 판정한다. 여기서 실패하면 호출·라운드/wave 소비 모두 0이다.
    if not prepare_ticket_body():
        return 1
    prompt = compose_prompt()

    budget = reviewer_reservation._reserve_round_budget(
        review_home, args, conf, wall_timeout_sec=timeout,
        target_rev=reviewer_budget._target_rev_fingerprint(diff),
    )
    if budget.refused_rc is not None:
        return budget.refused_rc

    # 스폰 전에 중단하는 실행은 호출이 없으므로 방금 잡은 예약을 그 자리에서 원자 환불한다
    # (마감 시점 `started=False` 환불과 같은 조건·같은 기계). 환불 소유는 이 seam 하나다.
    with reviewer_reservation._PreSpawnReservation(review_home, budget) as reservation:
        return reviewer_harvest._run_review_round(
            review_home, args, conf=conf, prompt=prompt, reviewer_cmd=reviewer_cmd,
            timeout=timeout, idle_timeout=idle_timeout, output_dir=output_dir,
            conf_path=conf_path, profile=profile,
            target=target, reservation=reservation,
            diff_root=diff_root,
        )
