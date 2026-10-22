"""리뷰 확인 명령 실행 (층: infra)."""
from __future__ import annotations

from typing import Callable
from pathlib import Path
from pm_engine.domain import review_contract
from pm_engine.domain import review_form
from pm_engine.domain import review_state
import shlex
import subprocess



def _run_required_test(command: str, expected: str | None, *, cwd: Path) -> str | None:
    """필수 테스트 한 건을 실행하고 실패 사유만 반환한다."""
    if expected is None:
        try:
            review_form._pm_review_assert_verify_command_shape(command, "full-regression.command")
            result = subprocess.run(
                shlex.split(command), cwd=str(cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=review_contract.PM_REVIEW_CONFIRMATION_COMMAND_TIMEOUT_SEC, check=False,
            )
        except (review_form.PMReviewError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return f"실행할 수 없습니다: {exc}"
        if result.returncode != 0:
            observed = (result.stdout or "") + (result.stderr or "")
            return f"green이 아닙니다: `{command}` · rc={result.returncode} · {observed[-2000:]}"
        return None
    try:
        status, observed = run_pm_review_confirmation_command(
            command, cwd=cwd, expected=expected,
        )
    except (review_form.PMReviewError, ValueError) as exc:
        return f"실행할 수 없습니다: {exc}"
    if status != review_form.PM_REVIEW_CONFIRMATION_RESOLVED:
        return f"green이 아닙니다: `{command}` · expected={expected!r} · 관측: {observed}"
    return None


def run_pm_review_confirmation_command(
    command: str, *, cwd: Path, expected: str,
    timeout: int = review_contract.PM_REVIEW_CONFIRMATION_COMMAND_TIMEOUT_SEC,
    run_fn: Callable | None = None,
) -> tuple[str, str]:
    """확인 커맨드 1회 실행 → `(status, observed)`.

    실행 실패(스폰 불가·타임아웃·비-0 rc)도 관측값에 실어 `unresolved` 로 남긴다 — 실행이
    안 됐다는 사실을 통과로 접으면 기계 확인이 확인이 아니게 된다.
    """
    review_form._pm_review_assert_verify_command_shape(command, "confirmation.command")
    argv = shlex.split(command)
    if not argv:
        raise review_form.PMReviewError("malformed", f"확인 커맨드가 비어 있습니다: {command!r}")
    runner = run_fn or subprocess.run
    try:
        result = runner(
            argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return "unresolved", f"rc=timeout({timeout}s)"
    except OSError as exc:
        return "unresolved", f"rc=실행 실패({type(exc).__name__}: {exc})"
    returncode = int(getattr(result, "returncode", 1))
    output = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    status = "resolved" if returncode == 0 and expected and expected in output else "unresolved"
    return status, f"rc={returncode}\n" + review_contract._pm_review_observed_excerpt(output, expected)


def run_pm_review_confirmations(
    template: review_state.PMReviewVerifyTemplate, *, cwd: Path,
    run_fn: Callable | None = None,
) -> tuple[tuple[int, review_form.PMReviewMachineConfirmation], ...]:
    """확인 가능한 행 전부를 실행해 확인 행으로 만든다(source round 오름차순 보존)."""
    rows: list[tuple[int, review_form.PMReviewMachineConfirmation]] = []
    for source_round, verify_row in template.machine_rows:
        status, observed = run_pm_review_confirmation_command(
            verify_row.command, cwd=cwd, expected=verify_row.expected,
            timeout=review_contract.PM_REVIEW_CONFIRMATION_COMMAND_TIMEOUT_SEC, run_fn=run_fn,
        )
        rows.append((source_round, review_form.PMReviewMachineConfirmation(
            verify_row.id, status, verify_row.command, observed, source_round,
        )))
    for source_round, verify_row in template.pm_owned_rows:
        rows.append((source_round, review_form.PMReviewMachineConfirmation(
            verify_row.id,
            "resolved",
            review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND,
            verify_row.expected,
            source_round,
        )))
    rows.sort(key=lambda item: item[0])
    return tuple(rows)
