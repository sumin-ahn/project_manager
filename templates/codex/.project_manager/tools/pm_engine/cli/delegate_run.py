"""일반 위임 CLI 실행과 회수 조정 (층: cli)."""
from __future__ import annotations

import os
import sys
from functools import partial
from pathlib import Path
from typing import Callable
from pm_engine.cli import delegate_cmd
from pm_engine.app import delegate_cluster_review
from pm_engine.app import delegate_execute
from pm_engine.app import delegate_harvest
from pm_engine.app import delegate_prepare
from pm_engine.app import delegate_prompt
from pm_engine.domain import delegate_rounds
from pm_engine.app import delegate_scope_audit
from pm_engine.infra import conf_store
from pm_engine.infra import delegate_config
from pm_engine.infra.harness import delegate_env
from pm_engine.infra import delegate_raw
from pm_engine.infra.harness import delegate_run
from pm_engine.domain import delegate_run_rules
from pm_engine.infra.harness import delegate_temp
from pm_engine.infra.harness import delegate_transport
from pm_engine.domain import reviewer_result
from pm_engine.domain import conf_schema, delegate_errors, install_rules, review_form
from pm_engine.infra import delegate_ledger, fs, pm_paths
from pm_engine.infra.harness import contract


def main(home, argv: list[str] | None = None, run_fn: Callable | None = None,
         git_run_fn: Callable | None = None) -> int:
    engine_home = home
    # `lint` 서브커맨드 — flat 위임 옵션(--role/--prompt-file/--cwd required)과 분리한 별도 경로.
    # 위임과 인자 형상이 다르므로 build_arg_parser 앞에서 분기(subparsers 로 위임 required 를 흩지
    # 않는다). never-block(항상 rc=0).
    resolved = list(sys.argv[1:] if argv is None else argv)
    if resolved and resolved[0] == "lint":
        return delegate_cmd._cmd_lint(home, resolved[1:])
    if resolved and resolved[0] == "raw":
        return delegate_cmd._cmd_raw(home, resolved[1:])
    if resolved and resolved[0] == "rounds":
        return delegate_cmd._cmd_rounds(home, resolved[1:])
    if resolved and resolved[0] == "ticket":
        return delegate_cmd._cmd_ticket(home, resolved[1:])
    if resolved and resolved[0] == "review":
        return delegate_cmd._cmd_review(home, resolved[1:])
    if resolved and resolved[0] == "changelog":
        return delegate_cmd._cmd_changelog(home, resolved[1:], git_run_fn=git_run_fn)
    parser = delegate_cmd.build_arg_parser()
    args = parser.parse_args(resolved)
    try:
        cwd = delegate_cmd._validate_args(parser, args)
    except delegate_errors.DelegateError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    effective_ticket = args.ticket

    tier = args.tier if (args.role in conf_schema.TIERED_ROLES and args.tier) else "normal"
    profile_source = "cli-override" if (args.harness and args.model) else "local-conf"

    # --cwd는 실행 타깃뿐 아니라 config/board 소유자를 정하는 명시 입력이다. 먼저 git 루트를
    # 확정한 뒤 worktree lease 장부로 PM 홈을 해소해, 어느 엔진 사본을 호출했는지와 분리한다.
    if not delegate_env._cwd_in_git_repo(cwd, git_run_fn):
        print(
            f"오류: --cwd 는 git 저장소 루트이거나 그 하위여야 합니다: {cwd}\n"
            "  광범위 경로(홈 디렉토리 등 non-repo)는 신뢰 작업공간이 아닙니다 — worktree/repo 를 지정하세요.",
            file=sys.stderr,
        )
        return 1
    cwd_repo = pm_paths._repo_root_for_cwd(cwd)
    try:
        config_repo = pm_paths.resolve_pm_home_for_repo(cwd_repo)
    except pm_paths.AnchorResolutionError as exc:
        print(f"오류: --cwd 소유 PM 홈 해소 실패 — {exc}", file=sys.stderr)
        return 1
    home = pm_paths.home_at(engine_home.root, pm_home=config_repo,
                            board_root=pm_paths.home_at(config_repo).board_root)
    board_repo = config_repo
    if (
        effective_ticket
        and pm_paths._owns_real_board(engine_home.root / ".project_manager")
        and engine_home.root.resolve() != config_repo.resolve()
    ):
        print(
            "오류: 실행 엔진 board와 --cwd 소유 PM 홈이 다릅니다 — 같은 ticket id를 다른 "
            f"board에서 읽을 수 없어 중단합니다: engine={engine_home.root.resolve()} owner={config_repo.resolve()}",
            file=sys.stderr,
        )
        return 1
    conf_path = delegate_execute._local_conf_path(home)
    try:
        conf = conf_store.local_config(home)
    except (OSError, UnicodeError) as exc:
        print(
            f"오류: 해소된 local.conf 읽기 실패 — 호출 전에 중단합니다: "
            f"{conf_path} ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return 1

    # 위임 마스터 스위치 (비-dry-run·기본 ON·명시 off = rc=3). **매핑 해소보다 앞** — 위임 자체가
    # 비허용인 형상은 "매핑 미설정"(rc=1)이 아니라 disabled(rc=3)로 응답해야 진단이 정확하다.
    # dry-run 은 항상 미리보기 허용(미호출)이라 이 게이트를 통과시킨다.
    if not args.dry_run and not delegate_config._is_enabled(conf):
        print(
            "위임이 꺼져 있습니다 — 채널(native/cross) 무관 "
            f"(local.conf: {conf_path} · {delegate_run_rules.DELEGATE_ENABLED_KEY}=false).\n"
            f"켜기: local.conf 에서 `{delegate_run_rules.DELEGATE_ENABLED_KEY}=true` (또는 그 줄을 지우면 기본 허용). "
            "미리보기는 `--dry-run`.",
            file=sys.stderr,
        )
        return 3  # 명시적 비성공(rc=0 no-op 금지·빈 stdout 성공 오인 차단)

    # config 해소 (원자 tuple·fail-loud rc=1). 추론 값 확인은 해소 뒤 실행 cwd·env 로 한다(dry-run 포함).
    try:
        harness, model, reasoning = delegate_config.resolve_delegate(
            conf, args.role, tier, args.harness, args.model, args.reasoning)
        fallback = delegate_config.resolve_fallback(conf, args.role, tier)
        delegate_config._validate_reasoning(harness, reasoning, model=model, cwd=cwd)
        if fallback is not None:
            fallback_harness, fallback_model, fallback_reasoning = fallback
            delegate_config._validate_reasoning(
                fallback_harness, fallback_reasoning, model=fallback_model, cwd=cwd)
    except delegate_errors.DelegateError as exc:
        print(f"오류: {exc}\n  · local.conf: {conf_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else None
    # [R] cold 재투입 비용 가드. write 역할만 코드 재섭취 중복을 막는다. read 역할은
    # generate≠evaluate 독립성을 위해 라운드마다 cold 판정하는 것이 정상이라 무마찰 통과시킨다.
    # resume/dry-run은 대상이 아니고, `--fresh <사유>`는 명시 우회다.
    # 장부 부재·손상은 이 가드만 fail-open하며, 안전 경계인 raw 기록 자체를 복구/초기화하지 않는다.
    if (
        effective_ticket
        and args.role in delegate_run_rules.WRITE_ROLES
        and args.resume_from is None
        and not args.dry_run
        and args.fresh is None
    ):
        completed = delegate_raw.cold_reinjection_record(home, effective_ticket, args.role, output_dir)
        if completed is not None:
            return delegate_execute.fail_loud(delegate_raw.cold_reinjection_rejection(
                effective_ticket, args.role, harness, completed,
            ))

    # 폴백 **비발동** 판정(loud skip·설정은 그대로 두고 이번 실행만 끈다). 설정 자체는 위에서 해소해
    # 불완전 폴백 설정을 fail-loud 로 잡되(설정 정합은 override 와 무관), 아래 사유면 이번 실행에선
    # 쓰지 않는다 — "설정돼 있는데 안 썼다"를 정확히 말할 수 있어야 loud skip 이 성립한다:
    #   CLI 완전지정(--harness AND --model) = 설정 미참조 원자 override(resolve_delegate 불변) —
    #      일회성 명시 실행이 요청 밖 하네스로 넘어가면 그 불변이 깨진다.
    #   폴백 tuple 의 하네스/모델이 primary 와 동일 — 한도 소진된 같은 채널을 유료로 재타격할 뿐이다
    #      (reasoning 만 다른 경우도 같은 계정/모델 한도라 skip 한다).
    fallback_skip: str | None = None
    if fallback is not None:
        if args.harness and args.model:
            fallback_skip = ("CLI 완전지정(--harness/--model)은 설정 미참조 원자 override — "
                             "설정 폴백을 쓰지 않는다")
            fallback = None
        elif (fallback[0], fallback[1]) == (harness, model):
            fallback_skip = (f"폴백 tuple 이 primary 와 동일({harness}/{model}) — "
                             "한도 소진된 같은 채널 재타격 금지")
            fallback = None

    # prompt-file 존재 확인 — 없으면 읽을 것이 없다.
    prompt_file = Path(args.prompt_file or "")
    task_text = ""
    if args.cluster is None and not prompt_file.is_file():
        print(f"오류: --prompt-file 이 없음: {prompt_file}", file=sys.stderr)
        return 1
    if args.cluster is None:
        try:
            read_target = prompt_file.resolve()
        except OSError:
            read_target = prompt_file
        try:
            task_text = fs.read_text_shared(read_target, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print(f"오류: --prompt-file 읽기 실패: {exc}", file=sys.stderr)
            return 1

    # [A] 실제 공유 장부와 그 행이 가리키는 raw 파일에서 최종 reply를 읽는다. 첨부는 task 원문
    # 말미에 붙으므로 full payload와 resume delta 어느 쪽에서도 같은 원문 1회가 실제로 넘어간다.
    try:
        attached_raw = delegate_raw.resolve_attached_raw(home, args.attach_raw, output_dir=output_dir)
    except delegate_errors.DelegateError as exc:
        return delegate_execute.fail_loud(f"오류: {exc}")
    if args.cluster is None:
        task_text = delegate_raw.append_attached_raw(task_text, attached_raw)

    # cross 실위임의 라운드 준비. dry-run은 미호출/무부수효과 계약이라 만들지 않고, 하네스로
    # 위임되는 역할의 실제 실행만 prepare한다(ticket 없는 legacy 호출은 종전 형상 유지).
    # 묶음 리뷰는 native 준비와 같은 함수(`prepare_cluster_review`)로 준비 트리 조건·리뷰 입력 판독 뒤
    # 예약한다 — dry-run 은 같은 입력을 읽기만 한다(`cluster_review_sources`).
    ticket_copy: delegate_rounds.TicketCopyPlan | None = None
    cluster_plan: delegate_rounds.ClusterCopyPlan | None = None
    cluster_sources: delegate_cluster_review.ClusterReviewSources | None = None
    if args.cluster is not None:
        focus_path = Path(args.focus) if args.focus else None
        try:
            if args.dry_run:
                cluster_sources = delegate_cluster_review.cluster_review_sources(
                    pm_paths.home_at(Path(config_repo).resolve()), args.cluster, repo=cwd_repo,
                    focus_path=focus_path, git_run_fn=git_run_fn,
                )
            else:
                cluster_sources, cluster_plan = delegate_cluster_review.prepare_cluster_review(
                    home, cluster=args.cluster, cwd=cwd_repo, focus_path=focus_path, owner_pid=os.getpid(), git_run_fn=git_run_fn,
                )
                # 쓰기 자리·run_id·환불 좌표는 run 단위라 대표 자리 하나로 잡아도 같은 값이다.
                # 티켓별로 갈리는 회수는 아래에서 `cluster_plan.rounds` 를 그대로 순회한다.
                ticket_copy = cluster_plan.rounds[0]
        except delegate_errors.DelegateError as exc:
            return delegate_execute.fail_loud(f"오류: 묶음 리뷰 준비 실패: {exc}")
    elif not args.dry_run and effective_ticket and args.role in (delegate_ledger.TICKET_COPY_ROLES - {review_form.ADDITIONAL_REVIEWER_ROLE, "researcher"}):
        try:
            ticket_copy = delegate_prepare.prepare_ticket_copy(
                home, ticket=effective_ticket, role=args.role, cwd=cwd_repo, owner_pid=os.getpid(),
            )
        except delegate_errors.DelegateError as exc:
            return delegate_execute.fail_loud(f"오류: 위임 티켓 라운드 준비 실패: {exc}")

    # 예약(prepare_ticket_copy) 이후 ~ 실행 인계(_execute_and_collect) 이전 구간
    # 전체를 하나의 정리 경계로 감싼다. 이 구간을 벗어나는 모든 경로(명시 return · 전파
    # 예외)가 이 finally 하나로 수렴한다 — 지점마다 호출을 꽂지 않는다(리뷰 라운드가 꽂았던
    # 6개 지점 삽입을 대체한다). `_ticket_copy_handed_off` 는 `_execute_and_collect` 호출
    # 직전 단 한 곳에서만 True 로 바뀐다 — 그 전에 return 이든 전파 예외든 이 경계를
    # 빠져나가면 전부 환불(abandon) 로 정리한다.
    _ticket_copy_handed_off = False
    try:
        # 프롬프트 합성과 회수 판정이 같은 실행-전 등록부 스냅샷을 쓴다. 위임 중 등록부 값이
        # 바뀌어도 전달한 금지 루트와 다른 현재값으로 재판정하지 않는다.
        adapter_roots = install_rules.adapter_directories()
        if cluster_sources is not None:
            # native 준비가 프롬프트 파일에 쓰는 것과 같은 조립이다(`cluster_review_prompt`).
            task_text = delegate_raw.append_attached_raw(delegate_cluster_review.build_cluster_review_prompt(
                home, cluster_sources.review, cluster_sources.specs,
                tree=cwd_repo, focus=cluster_sources.focus,
            ), attached_raw)
            prompt = delegate_raw.append_attached_raw(delegate_cluster_review.cluster_review_prompt(
                home, cluster_sources, tree=cwd_repo, plan=cluster_plan,
                adapter_roots=adapter_roots,
            ), attached_raw)
        else:
            preamble = delegate_prompt._role_preamble(args.role, adapter_roots)
            round_note = delegate_prompt._round_copy_preamble(ticket_copy, None)
            if round_note is not None:
                preamble += "\n" + round_note
            prompt = preamble + "\n\n" + task_text

        # 세션 재사용 해소 — **호출 전 게이트보다 앞**이다. 재사용 라운드가 실제로 내보내는 건 delta
        # payload 라, 게이트(재앵커·시크릿 스캔)가 그걸 못 보면 검사받지 않은 본문이 나간다. 재사용
        # 실패 시 full payload 로 돌아가므로 두 본문 **모두**를 게이트 입력으로 합친다.
        resume = delegate_execute.resolve_resume_plan(
            home, args.resume_from, harness=harness, role=args.role,
            task_text=task_text, output_dir=output_dir,
        )
        outgoing_text = (
            prompt if resume is None else "\n".join((resume.delta_prompt, prompt))
        )

        # 쓰기-타깃 axis 재앵커 게이트 (엔진 코드 write + PM 홈 cwd·dry-run 전 = 미리보기서 노출)
        # divergence와 같은 cwd repo 해소 입력을 기존 write-target 가드에도 재사용한다. 하위 디렉토리
        # `--cwd`가 PM 홈 가드를 우회하는 별도 판정축을 만들지 않는다.
        reanchor = delegate_execute.check_write_target_reanchor(args.role, cwd_repo or cwd, outgoing_text)
        if reanchor is not None:
            print(
                "오류: 엔진 코드(.project_manager/tools/) write 위임을 adopter#0 PM 홈 cwd 에서 실행했습니다 —\n"
                "  import 사본을 수정하면 canonical worktree 와 갈려 stale·false-green 이 납니다.\n"
                f"  · canonical worktree 로 재앵커하세요:  --cwd {reanchor}\n"
                "  · PM-doc(wiki/ADR/spike) 작업이면 PM 홈 cwd 가 정당합니다 — 그 경우 프롬프트가 엔진 코드\n"
                "    경로를 write 타깃으로 지목하지 않게 하세요.",
                file=sys.stderr,
            )
            return 1

        profile = delegate_config.resolved_delegate_profile(harness, model, reasoning)
        print(
            f"[pm-delegate] config provenance: local_conf={conf_path} "
            f"· pm_home={config_repo.resolve()} · source={profile_source} · resolved_profile={profile}",
            file=sys.stderr,
        )
        # 드라이버 argv 준비 (dry-run·실행 공용). opencode 는 실행 시 합성 프롬프트를 ``--dir`` 아래
        # wire 사본으로 만들어 --file 전달하므로, dry-run argv 는 사용자 prompt-file 경로로만 표시한다.
        argv_display = delegate_run._build_target_argv(
            harness, model, reasoning, args.role, cwd, prompt_file,
            None if resume is None else resume.session_id,
        )

        # 타임아웃은 dry-run 미리보기(폴백 시간 예산 표시)와 실행이 같은 값을 쓴다. 값이 하네스별이라
        # **시도마다 자기 하네스의 예산**을 쓴다(폴백이 다른 축이면 예산도 그 축의 것).
        timeout = delegate_env._resolve_timeout(home, args, conf, harness)
        fallback_timeout = (delegate_env._resolve_timeout(home, args, conf, fallback[0])
                            if fallback is not None else timeout)
        execution_budget = delegate_env._harness_timeout_budget(home, harness, timeout)
        if resume is not None:
            # 재사용 라운드는 primary 축을 두 번 쓸 수 있다(재사용 turn → 세션 불일치 시 fresh
            # 재실행). 그 뒤 인프라 폴백까지 겹치면 이 실행의 최악 스폰은 3회다 — 호출층 상한
            # advisory 가 두 시도만 요구하면 하네스가 엔진 진단보다 먼저 kill 한다.
            execution_budget += delegate_env._harness_timeout_budget(home, harness, timeout)
        if fallback is not None:
            execution_budget += delegate_env._harness_timeout_budget(home, fallback[0], fallback_timeout)
        cap_warning = delegate_execute.harness_cap_advisory(home, execution_budget=execution_budget)
        if cap_warning is not None:
            print(cap_warning, file=sys.stderr)

        # 기존 `--cwd` 신뢰 repo축을 파생 config_repo와 대조한다. 양쪽 모두 설정된 fallback 값만
        # 공용 seam이 비교한다.
        compare_fallback = fallback is not None
        try:
            _resolved_cwd_repo, divergence = delegate_execute.check_local_conf_divergence(
                cwd, conf, args.role, tier,
                config_repo=config_repo,
                cli_override=(profile_source == "cli-override"),
                compare_fallback=compare_fallback,
            )
        except reviewer_result.TargetLocalConfReadError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        if divergence is not None:
            print(
                reviewer_result.format_local_conf_divergence(
                    divergence,
                    surface="pm_delegate",
                    cwd_label="--cwd worktree",
                    source_label="해소된 PM 홈",
                    resolution_note=(
                        "이번 실행에서는 --cwd의 lease 소유자로 해소된 PM 홈 conf가 적용됩니다: "
                        f"{divergence.engine_conf_path}\n"
                        "  차단하지 않고 계속합니다. 같은 프로필을 원하면 --cwd worktree conf의 위 "
                        "키 값을 PM 홈 conf와 맞추세요. 다른 PM 홈 conf를 선택하려면 --cwd가 "
                        "가리키는 worktree와 lease 소유 관계를 확인하세요."
                    ),
                ),
                file=sys.stderr,
            )

        # 병렬 위임 touches 교집합 사전 경고 — 호출-전 게이트를 모두 통과한 뒤, **dry-run 미리보기
        # 에도** 낸다(띄우기 전에 보는 것이 이 경고의 값이다). rc 는 바꾸지 않는다. 리뷰어는
        # 트리를 바꾸지 않으므로(돌려받기 트리 조건) 쓰기 역할만 대상이다.
        # 이 경고는 **정보성**이다 — 차단 게이트(재앵커)와 독립이라 뒤따르는 게이트가
        # 실행을 막아도 출력은 그대로 남는다(rc·상태 불변). 출력 위치는 미리보기 요구가 정한다.
        touch_overlaps: tuple[delegate_scope_audit.TicketTouchOverlap, ...] = ()
        if effective_ticket and args.role in delegate_run_rules.WRITE_ROLES:
            try:
                touch_overlaps = delegate_scope_audit.claimed_touch_overlaps(
                    effective_ticket, pm_root=board_repo, workspace=cwd_repo,
                )
            except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
                # 조용히 삼키지 않는다 — 계산이 죽으면 "겹침 0" 과 구분되지 않는다.
                print(
                    f"경고: 병렬 위임 touches 교집합 계산 실패({type(exc).__name__}: {exc}) "
                    "— 비차단 진행.",
                    file=sys.stderr,
                )
            else:
                overlap_warning = delegate_scope_audit.format_touch_overlap_warning(
                    effective_ticket, touch_overlaps,
                )
                if overlap_warning:
                    print(overlap_warning, file=sys.stderr)

        # dry-run — 합성 프롬프트 요약 + argv 출력·미실행 (비활성이어도 허용·rc=0)
        if args.dry_run:
            print("=== [dry-run] pm_delegate 미리보기 (미실행) ===")
            print(f"role: {args.role} · tier: {tier} · 권한축: {contract.perm_axis(args.role)}")
            if cluster_sources is not None:
                review = cluster_sources.review
                print(
                    f"리뷰 대상: {review.cluster} 멤버 {len(review.members)}건 "
                    f"({', '.join(review.members)})"
                )
                print(
                    f"리뷰 입력: {review.base_branch} 와 {review.branch} 의 "
                    f"merge-base({review.merge_base[:12]}) 이후 · 변경 파일 "
                    f"{len(review.paths)}건"
                )
                print("리뷰 라운드 자리: dry-run 은 만들지 않는다(실행 시 준비·부작용 0 계약)")
            print(f"해소: harness={harness} model={model} reasoning={reasoning}")
            if fallback_skip is not None:
                print(f"폴백: 비발동 — {fallback_skip}")
            elif fallback is None:
                print("폴백: 미설정 (인프라 실패 시 기존 fail-loud)")
            else:
                fallback_harness, fallback_model, fallback_reasoning = fallback
                primary_budget = delegate_env._harness_timeout_budget(home, harness, timeout)
                fallback_budget = delegate_env._harness_timeout_budget(home, fallback_harness, fallback_timeout)
                note, _transport_note = delegate_cmd._dry_run_harness_annotations(
                    harness, fallback_harness
                )
                print(
                    "폴백: "
                    f"harness={fallback_harness} model={fallback_model} "
                    f"reasoning={fallback_reasoning} (인프라 실패에만 1회)"
                )
                print(
                    f"폴백 시간 예산: 최악 primary {primary_budget}s + 폴백 {fallback_budget}s = "
                    f"{primary_budget + fallback_budget}s (2차 폴백 없음{note})"
                )
                print(
                    "  (실행+정리 예산 — 부모 wait/pipe drain을 startup 재시도마다 산입; "
                    f"진단·raw 박제 여유 {delegate_run_rules._HARNESS_CAP_MARGIN_SEC}s는 호출층 상한에 별도)"
                )
            print(f"cwd: {cwd}")
            argv_display = delegate_transport._apply_workspace_git_dirs(
                argv_display, delegate_run_rules._READ_TMP_ARGV_MODE_BY_HARNESS[harness], cwd,
            )
            print(f"argv: {' '.join(argv_display)}")
            _budget_note, transport_note = delegate_cmd._dry_run_harness_annotations(harness)
            if transport_note is not None:
                print(transport_note)
            if args.resume_from is not None:
                # 재사용을 **요청한** 실행만 이 줄을 낸다(미요청 실행의 미리보기 형상은 불변).
                print(
                    "세션 재사용: 미적용 — fresh + full payload (사유는 stderr 안내)"
                    if resume is None else
                    f"세션 재사용: session_id={resume.session_id} "
                    f"· 장부 레코드={resume.record_id} · delta payload 전달 "
                    f"(세션 불일치 시 fresh 재실행 1회 = primary 예산 "
                    f"{delegate_env._harness_timeout_budget(home, harness, timeout)}s 추가)"
                )
            print("--- 합성 프롬프트 ---")
            dry_run_prompt = prompt if resume is None else resume.delta_prompt
            # read 역할 + 재앵커 하네스(현재 codex 단독)는 실행과 같은 3-절대경로 preamble을 미리보기
            # 에도 낸다 — 조건은 재앵커 플래그 단일이다(claude·opencode 특례
            # 없음). dry-run은 부작용 0 계약이라 read_tmp를 만들지 않고 `_predict_read_tmp_paths`로
            # 같은 이름 규칙의 대표 경로만 계산한다.
            #
            # 리뷰 must-fix(F-001): 이 조건은 실행 경로가 `read_tmp is not None`(=
            # `_read_tmp_strategy()` 가 전략을 낸다)으로 판정하는 것과 **같은 사실**을 봐야 한다 —
            # 예측 전용 사본을 새로 만들지 않고, 실행이 `_create_read_role_temp`를 통해 소비하는 바로
            # 그 함수(`_read_tmp_strategy()`)를 여기서도 그대로 부른다. 전략이 없는 플랫폼(fd 결속도
            # 소유자 ACL 도 없음)은 실행도 재앵커를 안 하므로(`read_tmp=None` → `_apply_read_tmp_argv`
            # 가 argv 를 그대로 반환 → `-C`는 원 `--cwd`) dry-run 도 존재하지 않을 좌표를 예고하지
            # 않는다(실재하지 않는 좌표를 주는 것은 좌표를 아예 안 주는 것보다 나쁘다 — PM 판정).
            if (
                args.role in delegate_run_rules.READ_ROLES
                and delegate_run_rules._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness]
                and delegate_temp._read_tmp_strategy() is not None
            ):
                predicted_exec_root, predicted_writable = delegate_transport._predict_read_tmp_paths(home, harness)
                dry_run_prompt = dry_run_prompt + "\n\n" + delegate_transport._reanchor_exec_root_preamble(
                    cwd, predicted_exec_root, predicted_writable,
                )
            print(dry_run_prompt)
            print("=== [dry-run] 위임 호출 생략 ===")
            return 0

        # env allowlist 정제 + 실행. 인프라 실패일 때만 명시 폴백을 같은 드라이버 계약으로
        # 1회 실행한다(최악 소요 = 두 시도의 하네스별 예산 합·2차 폴백 없음). 보안/재앵커 게이트는 이
        # 지점보다 앞이라 폴백 대상이 될 수 없다. (`output_dir` 은 재사용 후보 조회에 먼저 쓰였다.)
        _run = run_fn or partial(delegate_run._default_run_fn, home)

        # 범위 판정 캡처 — **위임 전체 단위**로 1회. 폴백 attempt 도 같은 위임의 일부라
        # attempt 마다 재캡처하지 않는다(PM 이 회수 시점에 "이 위임이 범위 밖을 만졌나"를 본다). 아래
        # 실행·회수 블록의 모든 종료 경로(성공·폴백·fail-loud·예외)에서 finally 가 정확히 1회 보고한다.
        scope_audit = delegate_scope_audit.begin_scope_audit(
            home, effective_ticket, cwd, pm_root=board_repo, adapter_roots=adapter_roots,
        )
        if touch_overlaps:
            # 실행 전에 이미 경고한 겹침 경로를 회수 시점 판정 입력으로 싣는다. 캡처 함수의
            # 입력이 아니라 이 위임이 무엇을 겹친다고 알렸는지의 기록이다.
            scope_audit = scope_audit._replace(
                overlap_paths=delegate_scope_audit.overlap_touch_paths(touch_overlaps),
            )
        try:
            target_rev = scope_audit.before.head or None
            print(
                f"하네스 {harness}(model={model}) 실행 중 (유료 호출).",
                file=sys.stderr,
            )
            advisory = delegate_execute.native_advisory(harness, args.role)
            if advisory is not None:
                print(advisory, file=sys.stderr)
            _ticket_copy_handed_off = True  # 여기부터 harvest 가 정리를 넘겨받는다
            return delegate_execute._execute_and_collect(
                home, args=args, harness=harness, model=model, reasoning=reasoning,
                fallback=fallback, fallback_skip=fallback_skip, cwd=cwd,
                prompt=prompt,
                timeout=timeout, fallback_timeout=fallback_timeout,
                output_dir=output_dir, run_fn=_run,
                local_conf_path=conf_path,
                profile_source=profile_source,
                resume=resume,
                # 재개 라운드의 티켓 식별자 **계승** — 명시 `--ticket` 이 우선이고, 없으면 이어받은
                # 레코드의 티켓을 그대로 싣는다. 안 실으면 이 라운드 레코드에 ticket 이 없어 **다음**
                # 재개의 티켓 지시자 선택(최신 1건)에서 빠지고, 재개 사슬이 한 라운드 만에 끊긴다.
                # 범위 판정(scope audit)은 계승하지 않는다 — 그건 선언 touches 의 축이라 이 실행이
                # 무엇을 선언했는지가 기준이다.
                ticket=effective_ticket or (resume.ticket if resume is not None else None),
                fresh_reason=(args.fresh.strip() if args.fresh is not None else None),
                # 이 위임의 기준 rev — 이미 캡처한 실행-전 worktree 상태를 그대로 쓴다(추가 git 호출 0).
                base_rev=target_rev,
                ticket_copy=ticket_copy,
                cluster_plan=cluster_plan,
            )
        finally:
            pending_exception = sys.exception()
            delegate_scope_audit.report_scope_audit(scope_audit, args.role)
            if ticket_copy is not None and _ticket_copy_handed_off:
                try:
                    if cluster_plan is not None:
                        # 묶음은 자리마다 독립 회수다 — 한 자리의 거부가 다른 자리의 교체를
                        # 되돌리지 않는다(회수 판정식은 단일 경로와 같은 하나다).
                        delegate_execute._report_cluster_harvest(delegate_harvest.harvest_cluster_copy(
                            home, run_dir=cluster_plan.run_dir, cwd=cwd_repo,
                            ))
                    else:
                        result = delegate_harvest.harvest_ticket_copy(
                            home, copy_path=ticket_copy.path, cwd=cwd_repo, )
                        print(
                            "[pm-delegate] ticket harvest: "
                            f"{'applied' if result.changed else 'unchanged'} · "
                            f"sync_ready={str(result.sync_ready).lower()} · "
                            f"copy={ticket_copy.path}",
                            file=sys.stderr,
                        )
                except delegate_errors.DelegateError as exc:
                    # 정상 return rc에는 harvest 실패 rc=1이 더 강하다. 반면 runner/실행 예외가 이미
                    # 전파 중이면 finally return으로 삼키지 않고 원 예외를 그대로 보존하며 두 원인을
                    # 모두 stderr에 남긴다.
                    residue = (
                        cluster_plan.run_dir if cluster_plan is not None
                        else ticket_copy.path
                    )
                    recovery = delegate_execute._delegation_harvest_failure_message(exc, residue)
                    if pending_exception is not None:
                        print(
                            "오류: 위임 실행 예외도 그대로 전파합니다: "
                            f"{type(pending_exception).__name__}: {pending_exception}",
                            file=sys.stderr,
                        )
                        delegate_execute.fail_loud(recovery)
                    else:
                        return delegate_execute.fail_loud(recovery)
    finally:
        if ticket_copy is not None and not _ticket_copy_handed_off:
            seats = (
                tuple(cluster_plan.rounds) if cluster_plan is not None
                else (ticket_copy,)
            )
            for seat in seats:
                delegate_prepare._refund_gate_rejected_ticket_copy(
                    home, seat, cwd=cwd_repo, )
