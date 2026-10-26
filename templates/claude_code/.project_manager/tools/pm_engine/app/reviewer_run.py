"""추가 리뷰어 실행과 결과 수집 (층: app)."""
from __future__ import annotations

from typing import Callable
from pathlib import Path
from pm_engine.infra.harness import contract
import pm_engine.infra.harness.reviewer_run
from pm_engine.infra import raw_output
from pm_engine.infra import reviewer_raw
from pm_engine.domain import reviewer_result
from pm_engine.infra import reviewer_target
import subprocess
import sys
import time


def run_review(
    home, prompt: str,
    target: reviewer_target.ReviewerTarget,
    timeout: int | None = None,
    output_dir: Path | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    idle_timeout: float | None = None, local_conf_path: Path | None = None, resolved_profile: str | None = None,
    cwd: Path | str | None = None, env: dict[str, str] | None = None,
    on_spawn_attempt: Callable[[], None] | None = None,
    on_no_spawn: Callable[[], None] | None = None,
) -> dict:
    """추가 리뷰어를 실행하고 결과를 수합한다.

    반환 dict: reviewer / ok / output / verdict / contamination / file / failed / started /
    any_must_fix / all_pass.
    `target` = 해소된 추가 리뷰어(**필수**·구조화 tuple). 커맨드 문자열만 받아 그대로 실행하던
    경로는 폐지됐다 — 모델을 고정하지 않는 실행은 "어느 모델이 이 판정을 냈는가"를 사후에
    확정할 수 없다. 실행 커맨드는 검증된 `target.command` 에서만 파생한다. argv 를
    직접 조립하고 회신을 **공용 wire 파서**로 추출해 판정에 넣는다 — JSON/스트림 원문을 그대로
    판정 파서에 넣으면 이벤트 필드에 실린 문구가 판정으로 오독된다. raw 박제는 추출 전 wire
    (stdout+stderr)를 그대로 보존한다.
    `started` = 자식 프로세스가 스폰됐는가(호출·과금 가능성) — 라운드 카운트 환불
    판정에 쓴다(False = 확실히 호출 전 실패 → 예약 환불). `idle_timeout` = 무진행 상한(None=공유
    기본) — 타임아웃 시에도 `output` 에 부분 산출물이 실려 `_write_reserved_output` 이 그대로 박제한다.
    `cwd`/`env` = 리뷰어 실행 조건(None=호출 프로세스 상속). 정상 경로는 `_main` 이 검토 대상
    저장소(diff_root)와 위임 채널과 같은 seam 이 조립한 env 를 넘긴다 — 채널이 다르다고 다른
    cwd·다른 홈을 주지 않는다.

    `contamination` = 회신 본문 품질 진단(엇갈린 판정 라인·옛 raw 파일명·전사 경로 인용).
    **하나라도 잡히면** `all_pass` 를 내려 '판정 불명확'(보수적 exit 1)로 만든다 — 이번 판정이
    이 리뷰의 것이라는 보장이 없는 회신에서 통과가 나가는 false-green 을 기계가 막는다.

    `on_spawn_attempt` = 라운드 예약 소유권 이전 seam(기본 None = 종전 동작). 이 함수의 **앞부분**
    (raw 선점·장부 시작 레코드·구조화 transport·argv/kwargs 준비)과 러너 안의 준비 구간
    (프로필 해소·워치독 셋업)은 아직 확실히 호출 전이라,
    거기서 예외로 나가면 콜백은 돌지 않는다 — 호출부의 스폰 전 seam 이 예약을 한 번 환불하고
    예외는 원본 그대로 다시 던져진다. 나가는 길에 이 함수가 남긴 raw 선점/미마감 레코드도
    함께 닫는다(`_abort_pre_spawn_raw`).

    `on_no_spawn` = 그 이전의 **대칭 반납** seam(기본 None = 종전 동작). 러너가 `started=False`
    로 돌아오면 자식은 뜬 적이 없다고 판명된 것이라, 회신 파싱·raw 박제·장부 마감 **어느 것도
    하기 전에** 한 번 호출해 소유권을 호출부로 돌려준다. 그 뒤 이 함수의 수합 구간에서 예외로
    나가면 (1) 호출부의 스폰 전 seam 이 예약을 한 번 환불하고 (2) 여기서 raw 선점/미마감 레코드를
    같은 보상 경로로 닫는다 — 스폰이 없던 실행이 "떠 있을지 모르는 자식"으로 장부에 남지 않는다.
    이미 정상 마감된 레코드는 다시 닫지 않는다(이중 마감 없음). 스폰된 실행(started=True)의 수합
    실패는 종전 보수 규칙 그대로다 — 환불도 보상도 없이 미완으로 남는다.
    """
    # 실행 커맨드의 소유자는 **대상 하나**다 — 두 입력이 갈리면 시간 프로필·argv·장부 헤더가
    # 서로 다른 것을 말한다.
    reviewer_cmd = target.command
    name = target.name
    raw_path = reviewer_raw._reserve_output(home, name, output_dir)
    # ── 여기부터 스폰 시도 seam 까지가 "확실히 호출 전" 구간이다 ──────────
    # raw 선점(mkdir/open)·장부 시작 레코드·구조화 transport·argv/kwargs 준비가 전부 이 안이고,
    # 자식은 아직 없다. 이 구간에서 예외로 나가면 (1) 예약 소유권을 넘기지 않아 호출부의 스폰 전
    # seam 이 한 번 환불하고, (2) 이 함수가 남긴 raw 선점/미마감 레코드를 정직하게 닫는다.
    # 소유권 이전 시점 자체는 watchdog 워치독이 `Popen` 을 부르기 한 줄 앞이다(그보다 앞이면
    # "스폰할 수도 있는 함수에 들어갔다"는 이유만으로 환불 권리가 사라진다).
    record_id: str | None = None
    ledger_path: Path | None = None
    spawn_attempted = False
    # 러너가 "자식 없음"으로 **판명**해 돌아온 뒤인가 — 스폰 시도 창이 다시 열린 상태다.
    no_spawn_proven = False
    # 장부 레코드를 정상 마감했는가 — 마감 뒤의 실패까지 보상 경로가 다시 닫으면 이중 마감이 된다.
    raw_finalized = False
    started_at = time.monotonic()

    def _spawn_attempt() -> None:
        """실 스폰 경계 1회 — 스폰 전 롤백 창을 닫고 예약 소유권을 호출부 계약대로 넘긴다."""
        nonlocal spawn_attempted
        spawn_attempted = True
        if on_spawn_attempt is not None:
            on_spawn_attempt()

    try:
        _raw_dir, ledger_path = reviewer_raw._raw_storage(home, output_dir)
        model = target.ledger_model
        # 장부 레코드는 raw 헤더·stderr provenance·dry-run 과 **같은 해소 축**을 싣는다 — 그래야
        # raw 파일이 지워지거나 stderr 를 못 읽어도 "무엇이 어떤 설정으로 나갔는가"가 장부 하나로
        # 닫힌다. local_conf 는 그 축의 앵커라(어느 conf 가 이 대상을 골랐나) 함께 남긴다.
        record_id = raw_output.start_raw_record(
            ledger_path,
            surface="additional-reviewer",
            harness=name,
            model=model,
            role=reviewer_result.REVIEWER_ROLE,
            raw_path=raw_path,
            attempt="primary",
            extra={
                **({"local_conf": str(local_conf_path)}
                   if local_conf_path is not None else {}),
                "reviewer_source": target.source,
                "reasoning": target.reasoning,
                "command": target.command,
            },
        )
        metrics: dict[str, object] = {"rc": 1, "silence_sec": None}
        started_at = time.monotonic()
        run_env = env
        if target.structured:
            # 공용 표가 OpenCode 카드 부재의 runtime role을 주입하고 다른 하네스는 env만 복사한다.
            run_env = contract.with_harness_runtime_role(
                env, target.harness, reviewer_result.REVIEWER_ROLE,
            )
        with reviewer_raw._structured_transport(target, prompt, cwd) as (argv, stdin_text):
            ok, output, started = pm_engine.infra.harness.reviewer_run._run_reviewer_ex(
                home, prompt, reviewer_cmd, timeout, run_fn, idle_timeout, metrics,
                cwd=cwd, env=run_env, argv=argv, stdin_text=stdin_text,
                on_spawn_attempt=_spawn_attempt,
            )
            if not started:
                # ── 스폰 없음 확정 seam ──────────────────────────────────
                # 러너가 결론적으로 "자식 없음"을 돌려줬다(빈 커맨드·실행 파일 부재·seam 계약
                # 오류). 스폰 시도 직전에 넘긴 소유권을 **회신 파싱·raw 박제·장부 마감 어느 것도
                # 하기 전에** 되돌려 놓는다 — 그 뒤 어디서 죽든(요약 출력의 BrokenPipeError 가
                # 실측 축이다) 호출 0·과금 0 인 실행이 예약을 미완으로 남기지 않는다.
                no_spawn_proven = True
                if on_no_spawn is not None:
                    on_no_spawn()
        elapsed = time.monotonic() - started_at
        # 판정과 오염 검출은 **회신 채널**만 본다(진행 로그는 프롬프트·diff 원문을 그대로 싣는다).
        # 경계는 구조(필드)라서 표시 문자열에 무엇이 섞여 와도 되찾을 필요가 없다.
        output = pm_engine.infra.harness.reviewer_run._as_reviewer_output(output)
        # 구조화 대상의 회신 채널은 하네스 wire(JSONL/stream)다 — 공용 추출기를 통과한 **최종 회신
        # 텍스트만** 판정·오염 검출에 넣는다. 추출 실패(회신 이벤트 부재·비-문자열·형식 붕괴)는 wire 를
        # 판정에 흘리지 않고 빈 회신으로 둔다 — 타입 계약은 공용 seam 이 소유하므로 어떤 wire 형상도
        # 여기서 예외로 터지지 않는다(터지면 raw 박제·장부 마감 전이라 진단 근거가 함께 사라진다).
        #
        # 그리고 **회신이 없으면 리뷰가 없다** — 프로세스가 rc=0 으로 끝났어도 이 실행은 리뷰어의
        # 판정을 하나도 받지 못했다. 이것을 '성공 → 판정 불명확'으로 두면 rc 는 1 이어도 폴백 신호
        # (FALLBACK_INTERNAL)가 없어, PM 이 내부 code-reviewer 로 갈아탈 근거를 못 받는다. 그래서
        # 실패 축으로 내린다: 요약은 실패 + 사유를 말하고 raw 에 wire 전문이 남는다. 라운드 장부에서의
        # 취급은 종전과 같다(판정 없음 = 미완 라운드 · 호출은 있었으므로 환불 없음).
        answer = output.answer
        reply_extraction_failed = False
        # usage 관측은 회신 판정과 **분리한 축**이다 — 실패한 리뷰(ok=False)도 토큰을 썼으므로
        # ok/rc 에 걸지 않는다. structured 대상에서만 관측한다 — legacy 대상은 임의 실행기라
        # wire 계약이 없다.
        review_usage: dict[str, int] | None = None
        if target.structured:
            observed = contract.extract_harness_result(target.harness, output.answer)
            review_usage = observed.usage
            if ok:
                reply_extraction_failed = observed.reply is None
                answer = observed.reply or ""
                ok = not reply_extraction_failed
        verdict = reviewer_result.parse_verdict(answer)
        contamination = reviewer_result.detect_output_contamination(answer)
        reviewer_raw._write_reserved_output(
            raw_path,
            output.combined,
            local_conf_path=local_conf_path,
            resolved_profile=resolved_profile,
            target=target,
            )
        try:
            raw_output.finish_raw_record(
                ledger_path,
                record_id,
                rc=int(metrics["rc"]),
                elapsed_sec=elapsed,
                silence_sec=metrics.get("silence_sec"),
                extra={"usage": review_usage} if review_usage else None,
            )
        except raw_output.RawRecordAlreadyFinished as exc:
            # 수동 `raw close`(--force) 가 먼저 마감한 충돌 — 첫 마감을 보존하고 리뷰
            # 결과는 실패로 바꾸지 않는다(회신은 raw 파일에 이미 박제됨).
            print(f"경고: 장부 마감 충돌 — {exc} (수동 마감 보존·회신은 raw 파일 참조)",
                  file=sys.stderr)
        raw_finalized = True
        return {
            "reviewer": name,
            "ok": ok,
            # wire 원문(stdout+stderr)은 그대로 보존한다 — raw 박제·사람 진단 입력.
            "output": output.combined,
            # 판정에 실제로 들어간 회신(구조화 대상은 추출된 최종 텍스트).
            "answer": answer,
            "log": output.log,
            "reply_extraction_failed": reply_extraction_failed,
            "verdict": verdict,
            "contamination": contamination.markers,
            "file": raw_path,
            "failed": not ok,
            "started": started,
            # 오염된 출력은 통과도 반려도 아니다. 반려만 남겨 두면 옛 반려 블록 echo 가 **이번 리뷰의
            # 반려**로 기록돼(라운드 장부·PM 판단 모두) 리뷰어가 하지 않은 지적을 근거로 일이 돌아간다.
            "any_must_fix": (
                ok and verdict["has_must_fix"] and not contamination.contaminated
            ),
            "all_pass": (
                ok and verdict["has_pass"] and not verdict["has_must_fix"]
                and not contamination.contaminated
            ),
        }
    except BaseException as exc:
        # 보상 조건은 두 가지뿐이다. (1) 스폰 시도 **전** — 자식이 아직 없다. (2) 러너가 "자식
        # 없음"으로 판명해 돌아온 **뒤** — 자식이 뜬 적이 없다. 그 사이(스폰 시도 후·판정 전)의
        # 실패는 종전 보수 규칙 그대로다: 호출됐을 수 있으니 예약도 미마감 레코드도 건드리지
        # 않는다(미마감이 곧 "확인 필요" 진단이다). 정상 마감이 이미 끝난 뒤라면 다시 닫지
        # 않는다 — 닫힌 레코드를 실패 축으로 덮으면 장부가 두 번째 거짓말을 한다.
        if not spawn_attempted or (no_spawn_proven and not raw_finalized):
            reviewer_raw._abort_pre_spawn_raw(
                ledger_path, record_id, raw_path,
                reason=f"{type(exc).__name__}: {exc}",
                elapsed=time.monotonic() - started_at,
                local_conf_path=local_conf_path, resolved_profile=resolved_profile,
                target=target,
            )
        raise
