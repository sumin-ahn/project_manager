"""위임 실행과 결과 수집 조정 (층: app)."""
from __future__ import annotations

from typing import Callable
from pathlib import Path
from collections.abc import Sequence
import argparse
from pm_engine.domain import conf_schema
from pm_engine.infra.harness import contract
from pm_engine.infra import delegate_config
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_errors
from pm_engine.app import delegate_harvest
from pm_engine.app import delegate_prompt
from pm_engine.domain import delegate_prompt_rules
from pm_engine.domain import delegate_rounds
from pm_engine.infra.harness import delegate_run
from pm_engine.domain import delegate_run_rules
from pm_engine.infra.harness import delegate_temp
from pm_engine.infra.harness import delegate_transport
from pm_engine.app import hooks
import os
from pm_engine.infra import pm_paths
from pm_engine.infra import raw_output
from pm_engine.domain import review_form
from pm_engine.infra import reviewer_target
import sys
import time
from pm_engine.infra.harness import watchdog



def fail_loud(message: str, *, rc: int = 1) -> int:
    """위임 실패 종료의 **단일 깔때기** — 사유 + 무음 대체 금지 안내를 stderr 에 내고 rc 를 돌려준다.

    실패 종료 지점은 여러 곳(타임아웃·비정상 rc·정리 실패·reply 미추출·폴백 소진)인데, 안내를
    지점마다 손으로 이어붙이면 **새 종료 경로가 조용히 안내를 빠뜨린다**. 그래서 `NO_SILENT_SUBSTITUTE_NOTE` 의 소비자는 이 함수 하나뿐이고(테스트가 그 불변을 못박는다),
    실패 경로는 `return fail_loud(...)` 로만 끝난다.
    """
    print(f"{message}\n{delegate_run_rules.NO_SILENT_SUBSTITUTE_NOTE}", file=sys.stderr)
    return rc


def _delegate_task_env(home, cwd: Path) -> dict[str, str]:
    """위임 하네스에 실을 슬롯 주인 task — 위임 세션은 세션 ID 가 따로라 슬롯 쓰기 가드가 이 값으로 판정한다.

    `cwd` 가 task 가 잡은 슬롯이면 `{PM_DELEGATE_TASK: <task>}` 다. 슬롯 밖이거나 task 가 잡지 않은
    슬롯이면 빈 dict 다 — 그 위임의 슬롯 쓰기는 가드가 거부한다(`cli.hooks.judge_tool_input`)."""
    found = hooks.slot_containing(home.root, cwd)
    if found is None:
        return {}
    row, tasks = found
    owner = row.get("session")
    if row.get("state") != "leased" or not any(task.get("name") == owner for task in tasks):
        return {}
    return {hooks.SLOT_WRITE_DELEGATE_TASK_ENV: owner}


def _local_conf_path(home) -> Path:
    """해소된 PM 소유자의 local.conf 절대경로."""
    return home.local_conf.resolve()


def check_local_conf_divergence(
    cwd: Path, conf: dict[str, str], role: str, tier: str, *,
    config_repo: Path,
    cli_override: bool = False,
    compare_fallback: bool = False,
) -> tuple[Path | None, object | None]:
    """기존 `--cwd` repo축으로 이번 role/tier의 **유효 primary/fallback 프로필** 분기를 검사한다.

    reviewer_target의 repo-root/conf 비교 seam을 재사용해 두 표면이 별도 판정 규칙을 갖지 않게 한다.
    양쪽 모두 `resolve_delegate`를 거쳐 reasoning 미지정(None)까지 포함한 완전 tuple을 비교하므로,
    한쪽만 키를 생략한 실제 수신 프로필 차이도 잡는다. 대상 역할/티어가 불완전하거나 잘못돼
    해소되지 않으면 실행 가능한 비교 대상이 아니므로 skip한다. CLI 완전지정은 local.conf를 primary
    프로필 입력으로 쓰지 않는 원자 override라 divergence만 명시 skip한다. fallback은 CLI override와
    primary 동일 skip을 해소한 뒤 실제 발동 가능한 실행에서만 같은 role/tier를
    비교한다. 또한 양쪽 `resolve_fallback` 결과가 모두 tuple이고 각 후보의 primary와 harness/model이
    다를 때만 값 차이를 판정한다. 한쪽 미설정·primary 동일은 실제 발동 불가능하므로 무소음이며,
    불완전/잘못된 대상 tuple도 실행 가능한 비교값이 아니어서 skip한다. 반환 target_repo는
    이 skip들과 무관하게 write-target 재앵커에도
    재사용한다.
    """
    target_repo = pm_paths.repo_root_from_cwd(cwd)
    if cli_override:
        return target_repo, None

    prefix = f"delegate.{role}" + (".hard" if tier == "hard" else "")

    def effective_profile(candidate: dict[str, str]) -> dict[str, object]:
        try:
            harness, model, reasoning = delegate_config.resolve_delegate(
                candidate, role, tier, None, None, None,
            )
        except delegate_errors.DelegateError:
            return {}
        values: dict[str, object] = {
            f"{prefix}.harness": harness,
            f"{prefix}.model": model,
            f"{prefix}.reasoning": reasoning,
        }
        if compare_fallback:
            try:
                fallback = delegate_config.resolve_fallback(candidate, role, tier)
            except delegate_errors.DelegateError:
                return values
            # main의 비발동 규칙과 후보별로 대칭이다. reasoning만 달라도 같은 harness/model 한도를
            # 재타격하므로 유효 fallback이 아니며, 한쪽만 유효하면 공통 비교 키가 생기지 않는다.
            if (
                fallback is not None
                and (fallback[0], fallback[1]) != (harness, model)
            ):
                for field, value in zip(
                    ("harness", "model", "reasoning"), fallback,
                ):
                    values[f"{prefix}.fallback.{field}"] = value
        return values

    divergence = reviewer_target.local_conf_divergence(
        engine_repo=config_repo,
        engine_conf=conf,
        target_repo=target_repo,
        selector=effective_profile,
        engine_label="pm-home",
        target_label="cwd-worktree",
    )
    return target_repo, divergence


def check_write_target_reanchor(role: str, cwd: Path, prompt: str) -> Path | None:
    """write 역할이 PM 홈 cwd 에서 엔진 코드(import 사본)를 write 타깃하면 재앵커 대상 worktree 반환.

    재앵커는 cwd 자체가 아니라 **쓰기-타깃 axis** 로 판정 — PM-doc(wiki/ADR/spike) 작업은 PM 홈 cwd
    정당. 판정 = pm_paths._pm_home_reanchor(실 board 소유 + `work/*` canonical 보유·파일 존재
    휴리스틱 금지) 재사용. read 역할·비-엔진-코드 타깃·PM 홈 아닌 cwd 는 None(통과)."""
    if role not in delegate_run_rules.WRITE_ROLES:
        return None
    if not delegate_prompt_rules._prompt_targets_engine_code(prompt):
        return None
    return pm_paths._pm_home_reanchor(cwd)


def _resume_unavailable(reason: str) -> None:
    """재사용 불가 사유를 1줄로 알리고 fresh 로 간다(조용한 강등 금지·차단은 아님)."""
    print(
        f"세션 재사용 미적용: {reason} — fresh 스폰 + full payload 로 진행합니다.",
        file=sys.stderr,
    )


def resolve_resume_plan(
    home, selector: str | None,
    *,
    harness: str,
    role: str,
    task_text: str,
    output_dir: Path | None,
) -> delegate_run_rules.ResumePlan | None:
    """`--resume-from` 지시자를 확정된 재사용 1건으로 해소한다(불가하면 None + loud 1줄).

    막히는 자리는 넷이고 전부 **비차단**이다: 재개 미지원 하네스, 장부 부재, 후보 부재(보존 창
    밖·다른 role·미마감/실패 레코드), 세션 id 형식 불일치. 어느 쪽이든 fresh + full payload 로
    진행한다.

    장부 부재는 **락을 잡기 전에** 끝낸다 — 조회는 읽기인데 락 획득이 장부 상위 디렉터리와
    `.lock` 파일을 만든다(`fs.exclusive_file_lock`). 한 번도 위임하지 않은 트리에서
    `--dry-run` 이 `.project_manager/.local/` 을 새로 만드는 건 미리보기의 부작용 0 원칙 위반이다.
    """
    if not selector:
        return None
    if not contract.harness_supports_resume(harness):
        _resume_unavailable(f"{harness} 하네스는 재개 argv 가 미검증(선언표 미지원)")
        return None
    _raw_dir, ledger_path = raw_output._raw_storage(home, output_dir)
    if not ledger_path.is_file():
        _resume_unavailable(f"raw 장부 없음({ledger_path}) — 이 위치에 아직 위임 기록이 없음")
        return None
    try:
        rows = raw_output.raw_records(ledger_path)
    except (OSError, ValueError) as exc:
        _resume_unavailable(f"raw 장부를 읽지 못함({exc})")
        return None
    record = delegate_run_rules.select_resume_record(
        rows, selector=selector, role=role, harness=harness,
    )
    if record is None:
        # 사유를 갈라 낸다 — "정말 없다"와 "있는데 이어받을 수 없다"는 처방이 다르다.
        _resume_unavailable(
            delegate_run_rules.resume_unusable_reason(rows, selector=selector, role=role, harness=harness)
            or (f"{selector!r} 의 성공 마감 위임 레코드(role={role}·harness={harness}) 없음 — "
                "장부 보존 창 밖이거나 아직 없는 라운드")
        )
        return None
    session_id = record.get(delegate_run_rules.RESUME_FIELD_SESSION_ID)
    if not contract.is_resumable_session_id(session_id):
        _resume_unavailable(
            f"레코드 {record.get('id')} 의 세션 id 형식 불일치({session_id!r})"
        )
        return None
    must_fix_items = record.get(delegate_run_rules.RESUME_FIELD_MUST_FIX)
    base_rev = record.get(delegate_run_rules.RESUME_FIELD_BASE_REV)
    record_ticket = record.get(delegate_run_rules.RESUME_FIELD_TICKET)
    return delegate_run_rules.ResumePlan(
        session_id=session_id,
        record_id=str(record.get("id")),
        ticket=record_ticket if isinstance(record_ticket, str) and record_ticket else None,
        delta_prompt=delegate_run_rules.build_resume_delta_payload(
            must_fix_items=(
                must_fix_items if isinstance(must_fix_items, list) else ()
            ),
            base_rev=base_rev if isinstance(base_rev, str) else None,
            task_text=task_text,
        ),
    )


def _execute_attempt(
    home, *,
    harness: str,
    model: str,
    reasoning: str | None,
    role: str,
    cwd: Path,
    prompt: str,
    timeout: int,
    output_dir: Path | None,
    run_fn: Callable,
    attempt: str,
    primary_raw: str | None = None,
    local_conf_path: Path | None = None,
    profile_source: str = "local-conf",
    resume_session_id: str | None = None,
    ticket: str | None = None,
    fresh_reason: str | None = None,
    base_rev: str | None = None,
    cluster_run_dir: Path | None = None,
    run_id: str | None = None,
    round_copy_path: str | None = None,
) -> delegate_run.DelegateAttempt:
    """하네스 1회를 실행하고 raw를 박제한다.

    폴백도 같은 드라이버/권한축/env allowlist/timeout 계약을 탄다(그래서 폴백이 발동한 실행의 최악
    소요는 두 시도의 하네스별 예산 합이고, 세션 재사용 라운드는 primary 축을 두 번 쓸 수
    있다·_harness_timeout_budget). opencode의 합성 prompt-file은
    attempt마다 만들고 즉시 정리한다. DI run_fn이 예외를 직접 raise해도 _default_run_fn과 **같은
    분류 신호**로 정규화한다 — 스폰 단계 예외(_LAUNCH_STAGE_ERRORS)만 RUN_RESULT_LAUNCH_FAILED 이고,
    그 밖의 OSError는 호출 후일 수 있어 미분류 실패로 남긴다(폴백 금지·중복 호출 차단).

    `resume_session_id`/`ticket`/`base_rev` 는 이 실행의 **장부 구조화 필드**다. 재개 id 는 지원
    선언표를 통과한 축에서만 받는다(미지원 축으로 미검증 argv 가 나가지 않는다).

    `run_id`/`round_copy_path` 는 cross 라운드 준비가 있었던 실행에서만 채워진다 — 있으면
    delegate-rounds 장부의 같은 이름 필드와 값이 같다. native 위임은 라운드 준비 자체가 없어
    이 실행 경로를 타지 않는다.

    이 함수 호출부(`_execute_and_collect`)는 한 run 에서 최대 3회 이 함수를 부른다(primary·
    세션 재사용 불일치 뒤 fresh 재실행·인프라 실패 폴백) — 셋 다 같은 `run_id`/`round_copy_path`
    를 싣는다(raw 행 → 예약은 many-to-one). 종료 판정 입력은 "결속된 행 중 아무 하나의 마감"이
    아니라 **가장 최신 attempt 행의 마감**이다 — primary 가 실패로 마감되고 폴백이 진행 중인
    창에서 전자를 쓰면 아직 도는 run 을 끝난 것으로 오판한다.
    """
    if resume_session_id is not None and not contract.harness_supports_resume(harness):
        raise delegate_errors.DelegateError(
            f"{harness} 하네스는 세션 재사용 미지원인데 재개 id 가 전달됐다 — "
            "미검증 argv 를 만들지 않는다."
        )
    # 슬롯 주인 task 는 준비 산출물을 만들기 전에 해소한다 — 장부 판독 실패가 되감을 산출물을 남기지 않는다.
    delegate_task_env = _delegate_task_env(home, cwd)
    argv, stdin_text, prompt_path, read_tmp = delegate_transport._prepare_attempt_transport(
        home, harness, model, reasoning, role, cwd, prompt, resume_session_id,
        cluster_run_dir,
    )
    # env 해소도 실패할 수 있다(하네스 필수 키 누락은 fail-loud) — 그 실패가 attempt 사본과
    # read tmp 를 남기지 않도록 준비 산출물과 같은 소유권 seam 에서 되감는다.
    try:
        env = contract.with_harness_runtime_role(
            delegate_transport._apply_read_tmp_env(delegate_env.build_env(harness), harness, read_tmp), harness, role,
        )
        env.update(delegate_task_env)
    except BaseException:
        delegate_temp._cleanup_attempt_transport(prompt_path, read_tmp)
        raise

    # raw 경로와 미마감 장부는 자식 프로세스 실행 전에 확정한다. 이 순서가 하네스 Bash/stdout
    # 유실 시에도 경로를 남기는 핵심 계약이다. 이후 BaseException으로 마감 경로를 건너뛰면
    # 레코드가 의도적으로 미마감 상태로 남아 kill/비정상 종료의 조회 입력이 된다.
    try:
        raw_path = raw_output._reserve_raw_output(home, harness, output_dir)
        _raw_dir, ledger_path = raw_output._raw_storage(home, output_dir)
        record_id = raw_output.start_raw_record(
            ledger_path,
            surface=delegate_run_rules.DELEGATE_RAW_SURFACE,
            harness=harness,
            model=model,
            role=role,
            raw_path=raw_path,
            attempt=attempt,
            # 실행 **전**에 아는 구조화 필드 — 다음 라운드의 후보 선택(ticket)과 delta 기준(rev),
            # 그리고 이 실행이 어떤 세션을 이어받으려 했는지가 장부 한 행에 남는다. opencode는
            # PM 홈 감사 raw와 cwd wire 사본 좌표를 같은 행에 두어 조인 없이 회수한다.
            extra={
                key: value for key, value in (
                    (delegate_run_rules.RESUME_FIELD_TICKET, ticket),
                    (delegate_run_rules.RESUME_FIELD_BASE_REV, base_rev),
                    (delegate_run_rules.RESUME_FIELD_RESUME_FROM, resume_session_id),
                    (delegate_run_rules.FRESH_REASON_FIELD, fresh_reason),
                    # cross 라운드 준비가 있었던 실행만 채운다 — delegate-rounds 장부 행과
                    # 값이 같아야 조인이 성립한다.
                    (delegate_run_rules.RESUME_FIELD_RUN_ID, run_id),
                    (delegate_run_rules.RESUME_FIELD_TICKET_COPY, round_copy_path),
                    (
                        delegate_run_rules.OPENCODE_TRANSPORT_PROMPT_FIELD,
                        str(prompt_path) if prompt_path is not None else None,
                    ),
                ) if value is not None
            },
        )
    except BaseException:
        # transport 준비 뒤 raw 예약/장부 시작이 실패해도 지시 사본을 남기지 않는다.
        delegate_temp._cleanup_attempt_transport(prompt_path, read_tmp)
        raise
    started = time.monotonic()

    def _record_pre_spawn_rejection(exc: delegate_errors.DelegateError) -> None:
        """예측 가능한 runner 전 거부를 kill/비정상 종료와 구분되는 마감 레코드로 남긴다."""
        rejection_elapsed = time.monotonic() - started
        rejection_stderr = (
            f"pm_delegate pre-spawn rejection: {type(exc).__name__}: {exc}"
        )
        if prompt_path is not None and prompt_path.launch_binding_mode is None:
            prompt_path.launch_binding_mode = "fail-closed"
        # raw 쓰기 자체가 실패해도 finally에서 장부는 반드시 마감한다. 그래야 0-byte raw가
        # 남더라도 finished_at/rc가 kill 증거와 pre-spawn 거부를 기계적으로 구분한다.
        try:
            raw_output._write_reserved_raw(
                raw_path,
                delegate_config._format_meta(
                    argv, 1, harness, model, rejection_elapsed, "", rejection_stderr,
                    attempt=attempt,
                    primary_raw=primary_raw,
                    reasoning=reasoning,
                    local_conf_path=local_conf_path,
                    profile_source=profile_source,
                    silence_sec=None,
                    idle_killed=False,
                    transport_sandbox_path=(
                        str(prompt_path.sandbox) if prompt_path is not None else None
                    ),
                    transport_prompt_path=(
                        str(prompt_path) if prompt_path is not None else None
                    ),
                    transport_binding_mode=(
                        prompt_path.launch_binding_mode
                        if prompt_path is not None else None
                    ),
                ),
            )
        finally:
            try:
                raw_output.finish_raw_record(
                    ledger_path,
                    record_id,
                    rc=1,
                    elapsed_sec=rejection_elapsed,
                    silence_sec=None,
                    finish_note="pre-spawn rejection",
                    extra={"pre_spawn_rejected": True},
                )
            except raw_output.RawRecordAlreadyFinished as finish_exc:
                print(
                    f"경고: 장부 마감 충돌 — {finish_exc} "
                    "(수동 마감 보존·pre-spawn 거부 raw는 기록 시도됨)",
                    file=sys.stderr,
                )

    try:
        try:
            launch_argv = argv
            launch_cwd = str(
                read_tmp.path
                if (
                    read_tmp is not None
                    and delegate_run_rules._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness]
                )
                else cwd
            )
            if (
                read_tmp is not None
                and delegate_run_rules._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness]
            ):
                # codex read 역할만 -C를 격리 tmp로 재앵커한다 — 그 tmp는 절대 git 저장소가
                # 아니므로 여기서 다시 rev-parse해 봐야 무의미하다. 재앵커 **이전**의 --cwd 자신을
                # 스폰 전에 검증한다(`_preflight_codex_read_exec_root` 선언부 주석 참고).
                try:
                    delegate_transport._preflight_codex_read_exec_root(cwd)
                except delegate_errors.DelegateError as exc:
                    _record_pre_spawn_rejection(exc)
                    raise
            if prompt_path is not None:
                # raw 예약/장부 시작처럼 준비 뒤 실행 전에 낀 작업까지 포함해 lexical
                # containment와 symlink 아닌 entry를 다시 확인한다.
                try:
                    launch_argv, launch_cwd = delegate_transport._opencode_transport_launch_target(
                        prompt_path, argv,
                    )
                except delegate_errors.DelegateError as exc:
                    _record_pre_spawn_rejection(exc)
                    raise
                # raw 감사와 DelegateAttempt도 runner가 실제 받은 argv를 기록한다. 준비 lexical
                # 경로는 OPENCODE_TRANSPORT_PROMPT_FIELD에 별도로 남아 cleanup 진단과 조인된다.
                argv = launch_argv
            result = run_fn(
                argv, stdin_text=stdin_text, cwd=launch_cwd,
                env=env,
                timeout=timeout, harness=harness,
            )
        except delegate_run._LAUNCH_STAGE_ERRORS as exc:
            result = delegate_run._launch_failure_result(harness, exc)
        except OSError as exc:
            result = delegate_run._midrun_failure_result(harness, exc)
    finally:
        elapsed = time.monotonic() - started
        # 이 finally는 runner 예외와 watchdog timeout/child-kill을 모두 회수한다. 다만 OS kill -9,
        # host crash 등 pm_delegate 프로세스 자체가 강제 종료되면 사용자 공간 finally는 실행될 수
        # 없으므로 그 경우의 잔여 0까지 보장하지 않는다.
        cleanup_residue = delegate_temp._cleanup_attempt_transport(prompt_path, read_tmp)

    rc = result.get("returncode", 1)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    if cleanup_residue is not None:
        # 잔존 트리를 남긴 실행은 실패다 — 경고만 찍고 rc=0 으로 끝내면 200MB 단위 잔재가
        # 저장소에 쌓이는 동안 호출부는 정상으로 읽는다. 위 `finally` 안에서 raise 하면 주
        # 결과와 전파 중인 예외를 덮으므로, 여기서 결과 자체를 실패로 내린다(분류 신호는
        # 붙이지 않는다 — 인프라 실패가 아니므로 폴백 대상이 아니다).
        rc = rc if rc != 0 else delegate_run_rules._READ_TMP_RESIDUE_RC
        stderr = f"{stderr}\n{cleanup_residue}" if stderr else cleanup_residue
        result = {**result, "returncode": rc, "stderr": stderr}
    raw_output._write_reserved_raw(
        raw_path,
        delegate_config._format_meta(
            argv, rc, harness, model, elapsed, stdout, stderr, attempt=attempt,
            primary_raw=primary_raw, reasoning=reasoning, local_conf_path=local_conf_path,
            profile_source=profile_source,
            silence_sec=result.get(delegate_run_rules.RUN_RESULT_SILENCE_SEC),
            idle_killed=bool(result.get(delegate_run_rules.RUN_RESULT_IDLE_KILLED, False)),
            transport_sandbox_path=(
                str(prompt_path.sandbox) if prompt_path is not None else None
            ),
            transport_prompt_path=(
                str(prompt_path) if prompt_path is not None else None
            ),
            transport_binding_mode=(
                prompt_path.launch_binding_mode
                if prompt_path is not None else None
            ),
        ),
    )
    observed = delegate_run._observe_harness_result(harness, stdout)
    try:
        raw_output.finish_raw_record(
            ledger_path,
            record_id,
            rc=rc,
            elapsed_sec=elapsed,
            silence_sec=result.get(delegate_run_rules.RUN_RESULT_SILENCE_SEC),
            extra=delegate_run._finished_ledger_fields(observed, resume_session_id),
        )
    except raw_output.RawRecordAlreadyFinished as exc:
        # 수동 `raw close`(--force) 가 먼저 마감한 충돌 — 첫 마감을 보존하고 실행 결과는
        # 실패로 바꾸지 않는다(회신은 raw 파일에 이미 박제됨).
        print(f"경고: 장부 마감 충돌 — {exc} (수동 마감 보존·회신은 raw 파일 참조)",
              file=sys.stderr)
    return delegate_run.DelegateAttempt(
        harness, model, argv, result, raw_path, observed.session_id,
    )


# ── native 단락 advisory (never-block 백스톱) ────────────────────────────

# 라이브 실측(2026년 7월 29일): Codex 세션에서 ``python3 -c``로 CODEX/CLAUDE/OPENCODE
# 이름만 dump했다 — CODEX_THREAD_ID=<set>, CODEX_CI=<set>. 세션 판정 근거는 이 두 키다.
# Claude Code는 이전 실측(2026년 7월 28일)에서 CLAUDECODE=<set>,
# CLAUDE_CODE_SESSION_ID/ENTRYPOINT/EXECPATH/CHILD_SESSION=<set>였고, 판정 근거는
# CLAUDECODE다.
#
# 라이브 실측(2026년 7월 29일): Claude 부모 셸과 그 셸에서
# ``opencode run -m ollama/glm-5.2:cloud``로 띄운 OpenCode 세션 안의 env를 diff했다.
# 부모에는 AI_AGENT/CLAUDECODE/CLAUDE_CODE_CHILD_SESSION/CLAUDE_CODE_ENTRYPOINT/
# CLAUDE_CODE_EXECPATH/CLAUDE_CODE_SESSION_ID/CLAUDE_EFFORT/CLAUDE_PID 및
# OPENCODE_CONFIG_DIR/ORCA_OPENCODE_CONFIG_DIR가 이미 있었고, OpenCode 세션은 이를 상속한
# 뒤 OPENCODE=<set>, OPENCODE_PID=<set>만 추가했다. 따라서 OPENCODE(보조로
# OPENCODE_PID)는 런타임 주입 세션 마커다. 반대로 OPENCODE_CONFIG_DIR는 세션 없는 부모에도
# 있으므로 설정 경로로 확정했고, OPENCODE_CONFIG는 양쪽 어디에도 없어 판정에서 제외한다.
def _session_harnesses(env: dict[str, str]) -> tuple[str, ...]:
    """실측 세션 마커와 일치하는 하네스를 모두 반환한다."""
    session_markers = watchdog.HARNESS_SESSION_MARKERS
    return tuple(
        name for name, keys in session_markers.items()
        if any(env.get(key) for key in keys)
    )


def _session_harness(env: dict[str, str]) -> str | None:
    """실측 세션 마커가 정확히 한 하네스와 일치할 때만 그 하네스를 반환한다."""
    matches = _session_harnesses(env)
    return matches[0] if len(matches) == 1 else None


def native_advisory(harness: str | None, role: str) -> str | None:
    """설계·구현·리뷰·조사를 target 하네스 == PM 하네스로 CLI 실행하면 native 규칙 안내 1줄(never-block).

    규칙: 모든 역할이 같은 하네스면 native 위임, 다르면 `pm_delegate.py` cross 다. 판정 입력은
    하네스뿐이다(역할 예외 없음). PM 하네스 env 마커(codex CODEX_THREAD_ID·claude CLAUDECODE·
    opencode OPENCODE)를 감지해 안내가 필요하면
    경고 문자열을 반환한다(호출부가 stderr 로 냄). 1차 판정은 어댑터 스킬 카드·이건 백스톱."""
    # 직접 호출 seam 에도 공개 하네스 도메인을 적용한다. 이 진단은 하네스 이름별 표현을 소유한다.
    if harness not in conf_schema.HARNESS_CHOICES:
        return None
    pm_harness = _session_harness(os.environ)
    if pm_harness == harness:
        return (f"[pm-delegate] target 하네스({harness}) == PM 하네스 — 설계·구현·리뷰·조사는 "
                "native 위임이 규칙이다(어댑터 스킬 카드 transport 절 · advisory).")
    return None


def _ticket_cli_owner(home, cwd: Path) -> Path:
    if not cwd.is_absolute():
        raise delegate_errors.DelegateError("--cwd 는 절대경로여야 합니다")
    resolved = cwd.resolve()
    if resolved == resolved.parent or not delegate_env._cwd_in_git_repo(resolved):
        raise delegate_errors.DelegateError(f"--cwd 는 파일시스템 루트가 아닌 git 작업공간이어야 합니다: {resolved}")
    try:
        owner = Path(pm_paths.resolve_pm_home_for_repo(resolved)).resolve()
    except pm_paths.AnchorResolutionError as exc:
        raise delegate_errors.DelegateError(f"--cwd 소유 PM 홈 해소 실패: {exc}") from exc
    if pm_paths._owns_real_board(home.root / ".project_manager") and home.root.resolve() != owner:
        raise delegate_errors.DelegateError(
            "실행 엔진 board와 --cwd 소유 PM 홈이 달라 ticket 사본 좌표를 확정할 수 없습니다: "
            f"engine={home.root.resolve()} · owner={owner}"
        )
    return owner



def harness_cap_advisory(home, env: dict[str, str] | None = None,
                         *, execution_budget: int | None = None) -> str | None:
    """기존 채택자의 런타임 Bash 상한이 엔진 실행 경로보다 낮으면 loud advisory.

    adapter settings 는 engine.manifest 밖 인스턴스 소유라 pm_update 로 정적 상향할 수 없다. 실행
    시 실제 env 를 읽어 조용한 무력화를 막는다. 기본 선언 최악과 이번 실행의 해소 예산 중 큰 쪽을
    요구치로 삼아 local.conf/CLI 상향도 놓치지 않는다. 알 수 없는/불량 값 역시 조용히 통과시키지
    않고 설정 표면을 명시한다(advisory·실행은 차단하지 않음).
    """
    env = os.environ if env is None else env
    if not _session_harnesses(env):
        return None
    session_markers = watchdog.HARNESS_SESSION_MARKERS
    cap_env = watchdog.HARNESS_CAP_ENV
    required = delegate_env.max_declared_execution_path_budget(home)
    if execution_budget is not None:
        required = max(required, execution_budget)
    return watchdog.harness_cap_advisory(
        env, execution_budget=required,
        session_markers=session_markers, cap_env=cap_env,
        render_missing=lambda pm_harness, cap_key, required: (
                f"[pm-delegate] 경고: {pm_harness} 하네스 상한 {cap_key} 미해소 — "
                f"엔진 최악 실행 경로+정리 여유 {required}s 이상으로 설정해야 하네스 선행 kill을 막습니다."
        ),
        render_invalid=lambda pm_harness, cap_key, raw, required: (
                f"[pm-delegate] 경고: {pm_harness} 하네스 상한 {cap_key}={raw!r} 해석 불가 — "
                f"유한한 정수 ms로 {required}s 이상 설정하세요."
        ),
        render_low=lambda pm_harness, cap_key, cap_sec, required: (
                f"[pm-delegate] 경고: {pm_harness} 하네스 상한 {cap_sec:g}s < "
                f"엔진 최악 실행 경로+정리 여유 {required}s — 엔진 무진행 진단/부분 산출물 보존 전에 "
                f"하네스가 kill할 수 있습니다. {cap_key}를 최소 {required * 1000}ms로 상향하세요."
        ),
    )


def _delegation_harvest_failure_message(exc: Exception, residue: Path) -> str:
    """cross 위임 종료의 회수 실패 처방 — terminal fix만 복구 루프를 열지 않는다."""
    if isinstance(exc, delegate_rounds.TerminalFixHarvestError):
        return (
            "오류: 최종 fix ticket harvest 실패 — terminal stop. "
            "board 라운드와 slot run-dir 증거를 그대로 보존했습니다. "
            "현재 티켓 상태와 실패 근거를 사용자에게 보고하세요. "
            f"copy={residue} · {exc}"
        )
    return (
        "오류: 위임 종료 ticket harvest 실패 — board 라운드와 slot run-dir 을 "
        "그대로 보존했습니다. run-dir 을 진단한 뒤 같은 경로로 "
        "`ticket harvest` 를 다시 부르거나 새 prepare/위임으로 복구하세요. "
        f"copy={residue} · {exc}"
    )


def _report_cluster_harvest(outcomes: Sequence[delegate_harvest.ClusterHarvestOutcome]) -> None:
    """묶음 회수 결과 요약 — 자리마다 한 줄. 거부가 하나라도 있으면 실패로 올린다.

    자리별 독립 회수라 성공한 자리는 그대로 두되, 남은 거부를 성공 rc 로 덮지 않는다
    (단일 경로의 회수 실패 처리와 같은 축 — 호출부의 복구 안내가 그대로 붙는다).
    """
    refused: list[str] = []
    for outcome in outcomes:
        if outcome.refusal is not None:
            refused.append(outcome.ticket)
            print(
                f"오류: 위임 종료 ticket harvest 거부: {outcome.ticket}: "
                f"{outcome.refusal}",
                file=sys.stderr,
            )
            continue
        print(
            "[pm-delegate] ticket harvest: "
            f"{'applied' if outcome.result.changed else 'unchanged'} · "
            f"sync_ready={str(outcome.result.sync_ready).lower()} · "
            f"copy={outcome.copy}",
            file=sys.stderr,
        )
    if refused:
        message = f"묶음 회수 거부 {len(refused)}건: {', '.join(refused)}"
        if any(outcome.terminal for outcome in outcomes):
            raise delegate_rounds.TerminalFixHarvestError(message)
        raise delegate_errors.DelegateError(message)


def _round_write_scope(
    ticket_copy: "delegate_rounds.TicketCopyPlan | None", role: str,
) -> Path | None:
    """이 실행에 열어 줄 쓰기 자리 — 라운드 준비가 있었던 리뷰 실행만 실값이다.

    값은 준비 계획이 실은 **묶음 run-dir**(`ClusterCopyPlan.run_dir`) 그대로다. 좌표가 없는
    계획으로 여기 오면 쓰기 자리를 조용히 좁히지 않고 멈춘다 — 권한 표면을 말없이 강등하면
    "실행은 됐는데 산출을 못 쓴" 라운드가 위임 실패로만 보인다.
    """
    if ticket_copy is None or role != review_form.INTERNAL_REVIEW_ROLE:
        return None
    if ticket_copy.cluster_run_dir is None:
        raise delegate_errors.DelegateError(
            f"라운드 준비 계획에 묶음 run-dir 좌표가 없습니다: {ticket_copy.path} — "
            "쓰기 자리를 좁히지 않고 중단합니다."
        )
    return ticket_copy.cluster_run_dir


def _execute_and_collect(
    home, *,
    args: argparse.Namespace,
    harness: str,
    model: str,
    reasoning: str | None,
    fallback: tuple[str, str, str | None] | None,
    fallback_skip: str | None,
    cwd: Path,
    prompt: str,
    timeout: int,
    output_dir: Path | None,
    run_fn: Callable,
    fallback_timeout: int | None = None,
    local_conf_path: Path | None = None,
    profile_source: str = "local-conf",
    resume: delegate_run_rules.ResumePlan | None = None,
    ticket: str | None = None,
    fresh_reason: str | None = None,
    base_rev: str | None = None,
    ticket_copy: delegate_rounds.TicketCopyPlan | None = None,
    cluster_plan: delegate_rounds.ClusterCopyPlan | None = None,
) -> int:
    """primary(+선택적 폴백) 실행과 결과 회수 — main 의 종료 rc 를 그대로 낸다.

    main 에서 분리한 이유는 위임 범위 판정이 **모든 종료 경로에서 정확히 1회** 돌아야 하기
    때문이다 — 호출부의 try/finally 가 그 경계다.

    `fallback_timeout` = 폴백 하네스의 벽시계 백스톱(미지정이면 primary 값). 값이 하네스별이라
    다른 축으로 폴백하면 예산도 그 축의 것이어야 한다(클라우드→로컬 GPU 폴백에서 로컬 작업이
    클라우드 예산에 잘리는 걸 막는다).

    `resume` 가 있으면 **delta payload 를 그 세션으로** 먼저 보낸다. 성공 판정은 회신 세션 id 가
    요청한 id 와 같은가 하나뿐이다(rc 로 판정하지 않는다). 확정된 "세션 없음"은 delta 미소비라
    write/read 모두 fresh + full payload 재실행이 안전하다. 반면 rc=0 명시적 id 불일치는 turn 이
    이미 실행됐을 수 있어 `RESUME_MUTATING_ROLES`(위임 4역할 전부. write 역할은 작업 트리, read
    역할은 라운드 파일을 쓴다)는 재실행하지 않는다. 그 밖의 역할만 fresh 로 다시 돈다. 미분류
    `rc≠0` 은 **호출 후** 실패일 수 있어 재실행하지 않는다(중복 과금·중복 호출 차단·기존
    fail-loud)."""
    try:
        primary = _execute_attempt(
            home, harness=harness,
            model=model,
            reasoning=reasoning,
            role=args.role,
            cwd=cwd,
            prompt=delegate_prompt._with_ticket_copy_preamble(
                prompt if resume is None else resume.delta_prompt, ticket_copy,
                cluster_plan,
            ),
            timeout=timeout,
            output_dir=output_dir,
            run_fn=run_fn,
            attempt="primary" if resume is None else delegate_run_rules.RESUME_ATTEMPT,
            local_conf_path=local_conf_path,
            profile_source=profile_source,
            resume_session_id=None if resume is None else resume.session_id,
            ticket=ticket,
            fresh_reason=fresh_reason,
            base_rev=base_rev,
            cluster_run_dir=_round_write_scope(ticket_copy, args.role),
            run_id=(ticket_copy.run_id if ticket_copy is not None else None),
            round_copy_path=(
                str(ticket_copy.path) if ticket_copy is not None else None
            ),
        )
        # 재실행은 **재사용 축의 확정된 실패에만** 쓴다(`resume_rerun_reason`) — 깨끗한 완료의
        # 명시적 세션 id 불일치, 또는 확정된 "세션 없음" 오류. 미분류 `rc≠0` 은 **호출 후** 죽은
        # 실행일 수 있어(프롬프트는 이미 나갔다) full payload 재실행이 곧 중복 과금·중복 외부
        # 호출이다 — 그런 실패는 기존 fail-loud/폴백 축이 그대로 소유한다. 인프라 실패(스폰·
        # 타임아웃·한도)도 세션 id 를 못 남겨 형식만 보면 "불일치"라, 분류 결과를 함께 본다.
        rerun_reason = (
            None if resume is None
            else delegate_run.resume_rerun_reason(
                primary.result, primary.session_id, resume.session_id)
        )
        if (
            rerun_reason is not None
            and delegate_run.classify_infrastructure_failure(primary.result) is None
        ):
            # 확정된 세션 부재(rc≠0 양성 오류)는 delta 가 소비되지 않아 모든 역할이 fresh 로
            # 안전하다. rc=0 세션-id 불일치는 turn 이 이미 돌았을 수 있으므로 쓰기 자리가 있는
            # 역할(`RESUME_MUTATING_ROLES`: write 역할은 작업 트리, read 역할은 라운드 파일)은
            # 재실행을 막는다.
            if (
                args.role in delegate_run_rules.RESUME_MUTATING_ROLES
                and rerun_reason == delegate_run.RESUME_RERUN_ID_MISMATCH
            ):
                return fail_loud(
                    f"오류: 세션 재사용 실패({args.role}·{rerun_reason}) — 요청 "
                    f"{resume.session_id} · 회신 {primary.session_id or '없음'}.\n"
                    "  · 회신이 다른 세션에서 왔다. 이 실행이 이미 작업 트리나 라운드 파일을 "
                    "고쳤을 수 있어 fresh 재실행을 자동으로 하지 않는다(중복·충돌 기록 차단).\n"
                    "  · 먼저 작업 트리(`git status` · `git diff`)와 이 역할의 라운드 파일을 "
                    "확인한다. 남은 변경이 없으면 `--resume-from` 없이 같은 위임을 다시 부르고, "
                    "부분 적용이 있으면 그 상태를 반영해 프롬프트를 고쳐 부른다.\n"
                    f"  · resume raw: {primary.raw_path}"
                )
            print(
                f"세션 재사용 실패({rerun_reason}): 요청 {resume.session_id} · 회신 "
                f"{primary.session_id or '없음'} — fresh 스폰 + full payload 로 이 라운드를 "
                f"1회 다시 실행합니다(적재 비용 1회 추가). resume raw: {primary.raw_path}",
                file=sys.stderr,
            )
            primary = _execute_attempt(
                home, harness=harness,
                model=model,
                reasoning=reasoning,
                role=args.role,
                cwd=cwd,
                prompt=delegate_prompt._with_ticket_copy_preamble(prompt, ticket_copy, cluster_plan),
                timeout=timeout,
                output_dir=output_dir,
                run_fn=run_fn,
                attempt=delegate_run_rules.RESUME_FRESH_FALLBACK_ATTEMPT,
                primary_raw=str(primary.raw_path),
                    local_conf_path=local_conf_path,
                profile_source=profile_source,
                ticket=ticket,
                fresh_reason=fresh_reason,
                base_rev=base_rev,
                cluster_run_dir=_round_write_scope(ticket_copy, args.role),
                run_id=(ticket_copy.run_id if ticket_copy is not None else None),
                round_copy_path=(
                    str(ticket_copy.path) if ticket_copy is not None else None
                ),
            )
    except (OSError, delegate_errors.DelegateError) as exc:
        return fail_loud(f"오류: 위임 실행 준비/raw 박제 실패: {exc}")

    # 실패 분류 → 선택적 loud 폴백. rc=0 reply(반려/must-fix 포함)는 분류 함수가 절대 폴백시키지
    # 않는다. 알려지지 않은 rc≠0도 기존 fail-loud로 남는다(오분류 보수 방향).
    result = primary.result
    raw_path = primary.raw_path
    rc = result.get("returncode", 1)
    stdout = result.get("stdout", "")
    timed_out = result.get("timed_out", False)
    failure_class = delegate_run.classify_infrastructure_failure(result)

    if failure_class is not None and fallback is None and fallback_skip is not None:
        # 설정은 있으나 이번 실행에선 폴백을 끈다 — 조용히 지나가지 않는다(loud skip).
        print(
            f"폴백 비발동: 인프라 실패({failure_class})이지만 {fallback_skip}. "
            "기존 fail-loud 로 진행한다.",
            file=sys.stderr,
        )

    if failure_class is not None and fallback is not None:
        fallback_harness, fallback_model, fallback_reasoning = fallback
        loud = (
            f"폴백: {harness}→{fallback_harness}({fallback_model}) — "
            f"사유: {failure_class}"
        )
        print(loud, file=sys.stderr)
        print(
            f"하네스 {fallback_harness}(model={fallback_model}) 로 폴백 프롬프트 실행 중 "
            "(과금·1단 폴백).",
            file=sys.stderr,
        )
        advisory = native_advisory(fallback_harness, args.role)
        if advisory is not None:
            print(advisory, file=sys.stderr)
        try:
            fallback_attempt = _execute_attempt(
                home, harness=fallback_harness,
                model=fallback_model,
                reasoning=fallback_reasoning,
                role=args.role,
                cwd=cwd,
                prompt=delegate_prompt._with_ticket_copy_preamble(prompt, ticket_copy, cluster_plan),
                timeout=fallback_timeout if fallback_timeout is not None else timeout,
                output_dir=output_dir,
                run_fn=run_fn,
                attempt=f"fallback-from-{harness}:{failure_class}",
                primary_raw=str(raw_path),
                local_conf_path=local_conf_path,
                profile_source="local-conf",
                ticket=ticket,
                fresh_reason=fresh_reason,
                base_rev=base_rev,
                cluster_run_dir=_round_write_scope(ticket_copy, args.role),
                run_id=(ticket_copy.run_id if ticket_copy is not None else None),
                round_copy_path=(
                    str(ticket_copy.path) if ticket_copy is not None else None
                ),
            )
        except (OSError, delegate_errors.DelegateError) as exc:
            return fail_loud(
                f"오류: 폴백 실행 준비/raw 박제 실패: {exc}. primary raw: {raw_path}")

        fallback_result = fallback_attempt.result
        fallback_rc = fallback_result.get("returncode", 1)
        fallback_stdout = fallback_result.get("stdout", "")
        if fallback_result.get("timed_out", False):
            actual_timeout = delegate_run._timeout_result_summary(
                fallback_result,
                fallback_timeout=(fallback_timeout if fallback_timeout is not None else timeout),
            )
            return fail_loud(
                f"오류: 폴백 위임 turn 타임아웃({actual_timeout}) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}")
        if fallback_rc != 0:
            return fail_loud(
                f"오류: 폴백 하네스 실패(rc={fallback_rc}) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}")
        try:
            fallback_reply = (
                delegate_run.extract_reply(fallback_harness, fallback_stdout) if fallback_stdout else None
            )
        except (ValueError, UnicodeError, delegate_errors.DelegateError) as exc:
            return fail_loud(
                f"오류: 폴백 reply 추출 실패: {exc}. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}")
        if not fallback_reply or not fallback_reply.strip():
            return fail_loud(
                "오류: 폴백 위임 reply 미추출(빈 출력·파싱 실패) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}")

        # stdout 결과에 provenance를 넣어 PM 회수 reply 자체가 폴백 사실을 보존한다.
        print(
            "[pm-delegate] 실행 provenance: "
            f"fallback={fallback_harness}(model={fallback_model}) · "
            f"primary={harness}(model={model}) · 사유={failure_class}"
        )
        print(fallback_reply)
        print(
            f"[pm-delegate] primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}",
            file=sys.stderr,
        )
        return 0

    # 미설정 또는 비-인프라 실패 → 현행 fail-loud(rc=1 + stderr + raw 경로). 종료는 전부
    # `fail_loud` 단일 깔때기를 거친다 — 실패 안내가 다른 수신자를 권하면 그 권유가 곧 무기록
    # 대행의 입구이고, 지점마다 손으로 안내를 잇는 방식은 새 경로에서 반드시 빠진다.
    if timed_out:
        actual_timeout = delegate_run._timeout_result_summary(result, fallback_timeout=timeout)
        return fail_loud(
            f"오류: 위임 turn 타임아웃({actual_timeout}) — 프로세스그룹 종료. raw: {raw_path}")
    if result.get(delegate_run_rules.RUN_RESULT_CLEANUP_FAILED):
        return fail_loud(
            "오류: 위임 프로세스 정리 실패 — primary가 아직 살아 있을 수 있어 자동 폴백을 "
            f"실행하지 않습니다(중복 실행·과금·worktree 동시 편집 차단). 사람 확인 필요. raw: {raw_path}")
    if rc != 0:
        return fail_loud(f"오류: 위임 하네스 실패(rc={rc}). raw: {raw_path}")

    try:
        reply = delegate_run.extract_reply(harness, stdout) if stdout else None
    except (ValueError, UnicodeError, delegate_errors.DelegateError) as exc:
        return fail_loud(f"오류: reply 추출 실패: {exc}. raw: {raw_path}")
    if not reply or not reply.strip():
        return fail_loud(f"오류: 위임 reply 미추출(빈 출력·파싱 실패). raw: {raw_path}")

    print(reply)
    print(f"[pm-delegate] raw: {raw_path}", file=sys.stderr)
    return 0
