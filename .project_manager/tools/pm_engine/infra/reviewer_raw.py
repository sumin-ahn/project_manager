"""추가 리뷰 원문과 운반 파일 저장 (층: infra)."""
from __future__ import annotations

from typing import Iterator
from pathlib import Path
import contextlib
import datetime
from pm_engine.infra.harness import delegate_temp
import os
from pm_engine.infra import raw_output
from pm_engine.infra import review_rounds
from pm_engine.domain import reviewer_result
from pm_engine.infra import reviewer_target
import sys
import uuid


# ── 결과 저장 ─────────────────────────────────────────────────────────────


def _raw_storage(home, output_dir: Path | None = None) -> tuple[Path, Path]:
    """추가 리뷰 raw/공유 장부 위치 — 앵커는 해소된 소유 PM 홈(미해소는 fail-loud).

    호출부가 확정한 Home의 PM 소유자 아래에 기록한다. 리뷰 코드 트리와 PM 데이터
    소유자가 달라도 통합 raw 조회가 같은 장부를 읽는다.

    delegate의 raw 장부는 표현 축이 아닌 저장 경로 축이므로 포맷터 통합에서 제외하고,
    두 소비처 모두 raw_output.raw_storage_paths를 쓰는 현재 경계를 유지한다.
    """
    return raw_output.raw_storage_paths(
        home.local_dir.parent.parent,
        "review",
        output_dir,
    )


def _reserve_output(home, reviewer: str, output_dir: Path | None = None) -> Path:
    """실행 전 장부가 가리킬 추가 리뷰 raw를 충돌 없이 선점한다."""
    base_dir, _ledger_path = _raw_storage(home, output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = base_dir / (
        f"additional_reviewer_{reviewer}_{ts}_{os.getpid()}_{uuid.uuid4().hex}.txt"
    )
    fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return dest


def _write_reserved_output(
    dest: Path,
    content: str,
    *,
    local_conf_path: Path | None,
    resolved_profile: str | None,
    target: reviewer_target.ReviewerTarget | None = None,
) -> None:
    with dest.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(reviewer_target._review_raw_content(
            content, local_conf_path, resolved_profile, target))


# ── 실행 + 수합 ────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _structured_transport(
    target: reviewer_target.ReviewerTarget, prompt: str, cwd: Path | str | None,
) -> Iterator[tuple[list[str] | None, str | None]]:
    """구조화 대상의 (argv, stdin) 을 준비하고 임시 자원을 **모든 종료 경로**에서 정리한다.

    codex/claude 는 프롬프트를 stdin 으로 받으므로 준비할 자원이 없다. opencode 만 `--file` 첨부라
    리뷰 sandbox 하위에 검토 맥락과 입력 좌표를 담은 0600 프롬프트 파일을 만들고
    소유자 전용 접근을 적용한 뒤 실행 성공·실패·예외 무관하게 지운다. 생성·containment·
    자기-은닉·정리는 공용 `delegate_temp` seam을 그대로 쓴다. legacy 대상은 이 경로를
    타지 않는다."""
    if not target.structured:
        yield None, None
        return
    if target.harness != "opencode":
        yield reviewer_target._structured_reviewer_argv(
            target.harness, target.model, target.reasoning,
            cwd=str(cwd) if cwd is not None else reviewer_result.REVIEWER_CWD_PLACEHOLDER,
            prompt_file=reviewer_result.REVIEWER_PROMPT_FILE_PLACEHOLDER,
        ), None
        return
    sandbox = Path(cwd) if cwd is not None else Path.cwd()
    prompt_file = delegate_temp._save_opencode_transport_prompt(sandbox, prompt)
    try:
        # 생성 뒤 argv 조립 직전에도 위임 축과 같은 containment guard를 다시 건다. 생성 seam이
        # fd로 고정한 실 sandbox를 그대로 써 사용자 입력 cwd symlink 재해소 창을 열지 않는다.
        delegate_temp._assert_opencode_transport_path(prompt_file.sandbox, prompt_file)
        # 생성 seam의 `0600`은 **Windows에서 아무 제한도 걸지 않는다**(실측 `S_IMODE`=0o666).
        # 이 파일에는 검토 맥락이 들어가므로 플랫폼 등가 수단(ACL)으로 소유자 전용
        # 접근을 실제로 건다 — containment 재확인과 같은 자리에서 접근 경계도 재확인한다.
        transport_dir = Path(prompt_file.path).parent
        for artifact in (transport_dir, Path(prompt_file.path),
                         transport_dir / delegate_temp._OPENCODE_TRANSPORT_IGNORE):
            if artifact.exists():
                review_rounds._restrict_to_owner(artifact)
        yield reviewer_target._structured_reviewer_argv(
            target.harness, target.model, target.reasoning,
            cwd=str(prompt_file.sandbox),
            prompt_file=str(prompt_file),
        ), ""
    finally:
        delegate_temp._cleanup_attempt_transport(prompt_file)


# 스폰 전 중단으로 마감하는 raw 레코드의 rc — 리뷰를 하나도 받지 못한 실행이라 실패 축(1)이다
# (빈 커맨드·실행 파일 부재 같은 다른 '스폰 없음' 마감과 **같은 축**이라 조회면의 뜻이 갈리지
# 않는다). "왜 실패했나"는 그 레코드가 가리키는 raw 파일의 중단 사유 줄이 소유한다 — 장부 스키마에
# 새 상태 값을 만들지 않는다(마감 API 는 rc/경과/침묵만 받는다).
_PRE_SPAWN_ABORT_RC = 1


def _abort_pre_spawn_raw(
    ledger_path: Path | None, record_id: str | None, raw_path: Path, *,
    reason: str, elapsed: float, local_conf_path: Path | None,
    resolved_profile: str | None, target: reviewer_target.ReviewerTarget | None,
) -> None:
    """스폰 **전에** 끊긴 실행이 남긴 raw 선점/미마감 레코드를 그 자리에서 정직하게 닫는다.

    raw 장부의 미마감 레코드는 "떠 있을지 모르는 자식"(고아 프로세스 조회면 `--unfinished` 의
    입력)이라는 뜻이다. 스폰이 확실히 없던 실행이 그 상태로 남으면 조회면이 없는 프로세스를
    가리키고, 0바이트 raw 파일은 "리뷰어가 아무 말도 안 했다"로 읽힌다. 그래서 두 경우로 닫는다:

    · 장부가 이 raw 를 가리키기 전에 끊겼으면(레코드 없음) 선점 파일만 지운다 — 되돌릴 원자 단위가
      그것 하나다.
    · 레코드가 있으면 raw 에 중단 사유를 박제하고 레코드를 실패 축으로 마감한다.

    raw 박제·레코드 마감 실패는 전파한다 — 호출부가 다시 던지려던 주 예외는 traceback 의
    `__context__` 로 남는다. 선점 파일 unlink 의 OSError 만 경고로 남긴다."""
    if record_id is None or ledger_path is None:
        try:
            raw_path.unlink()
        except OSError as exc:
            print(
                f"경고: 스폰 전 중단의 raw 선점 정리 실패 ({type(exc).__name__}: {exc}) — "
                f"빈 파일이 남았습니다: {raw_path}",
                file=sys.stderr,
            )
        return
    _write_reserved_output(
        raw_path,
        f"[스폰 전 중단 — 자식 프로세스 시작 없음(호출 0·과금 0): {reason}]",
        local_conf_path=local_conf_path,
        resolved_profile=resolved_profile,
        target=target,
    )
    raw_output.finish_raw_record(
        ledger_path, record_id,
        rc=_PRE_SPAWN_ABORT_RC, elapsed_sec=elapsed, silence_sec=None,
    )
