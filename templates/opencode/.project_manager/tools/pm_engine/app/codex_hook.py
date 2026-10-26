"""Codex 훅 관측과 감독 (층: app)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from collections.abc import Sequence
from pm_engine.domain import codex_envelope
from pm_engine.infra import codex_observation
from pm_engine.domain import delegate_channel_rules
import json
import subprocess


def _codex_observation_failure(
    result: Mapping[str, object], exc: BaseException
) -> dict[str, object]:
    detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
    warning = (
        f"[delegate-channel/warn] Codex 관측 기록 실패({type(exc).__name__}: {detail}); "
        "차단 판정은 유지하고 matcher drift 관측은 불완전함"
    )
    # A recording failure must not be the moment a partial envelope slips out:
    # validate the block shape first so the deny keys are preserved as a set.
    visible = codex_envelope._validated_codex_envelope(result)
    hook_output = visible.get("hookSpecificOutput")
    if visible.get("decision") == "block" and isinstance(hook_output, Mapping):
        combined = f"{visible.get('reason') or ''}; {warning}".lstrip("; ")
        hook = dict(hook_output)
        hook["permissionDecisionReason"] = combined
        visible["reason"] = combined
        visible["hookSpecificOutput"] = hook
        visible["systemMessage"] = combined
        visible["suppressOutput"] = delegate_channel_rules.CODEX_DENY_SUPPRESS_OUTPUT
        return codex_envelope._validated_codex_envelope(visible)
    visible["systemMessage"] = warning
    visible["suppressOutput"] = delegate_channel_rules.CODEX_WARNING_SUPPRESS_OUTPUT
    return codex_envelope._validated_codex_envelope(visible)


def observe_codex_pretooluse(
    home, payload: Mapping[str, object],
    result: Mapping[str, object],
    *,
    decision: Mapping[str, str],
    state_dir: Path | None = None,
) -> dict[str, object]:
    """Append the internal decision while returning the host envelope unchanged.

    ``decision`` is required: the host envelope cannot express an allow verdict
    (allow emits ``{}`` so the tool proceeds untouched), so deriving the recorded
    status from the envelope silently degrades every allow row to
    ``decision_unknown`` and makes the spawn/start pairing scan report false
    matcher misses.
    """
    reason = str(decision.get("reason") or result.get("reason") or "")
    permission = str(decision.get("verdict") or "")
    if not permission:
        hook_output = result.get("hookSpecificOutput")
        if isinstance(hook_output, Mapping):
            permission = str(hook_output.get("permissionDecision") or "")
    try:
        codex_observation._append_codex_observation(
            home, payload,
            hook_event_name="PreToolUse",
            status=f"decision_{permission or 'unknown'}",
            reason=reason,
            state_dir=state_dir,
        )
    except OSError as exc:  # 관측 장부 기록 실패는 판정을 바꾸지 않는다(차단 판정 유지 경고).
        return _codex_observation_failure(result, exc)
    return codex_envelope._validated_codex_envelope(result)


def observe_codex_subagent_start(
    home, payload: Mapping[str, object], *, state_dir: Path | None = None
) -> dict[str, object]:
    """Append an actual start; correlation is deferred to ``board.py lint``."""
    reason = (
        "[delegate-channel/record] SubagentStart 관측 기록 — "
        "PreToolUse 대조는 board.py lint의 멱등 스캔이 수행"
    )
    result: dict[str, object] = {
        "suppressOutput": delegate_channel_rules.CODEX_OBSERVATION_SUPPRESS_OUTPUT
    }

    try:
        codex_observation._append_codex_observation(
            home, payload,
            hook_event_name="SubagentStart",
            status="observed",
            reason=reason,
            state_dir=state_dir,
        )
    except OSError as exc:  # 관측 장부 기록 실패는 판정을 바꾸지 않는다(차단 판정 유지 경고).
        return _codex_observation_failure(result, exc)
    return codex_envelope._validated_codex_envelope(result)


def _codex_supervisor_fallback(event: str, detail: str) -> dict[str, object]:
    """The complete warning envelope a failed/absent guard run is replaced with."""
    reason = (
        f"[delegate-channel/warn] Codex {event} 가드 실행 실패({detail}) — "
        f"{delegate_channel_rules.CODEX_SUPERVISOR_FALLBACK_MARKER}: native 통과(fail-open)"
    )
    return codex_envelope._validated_codex_envelope({
        "systemMessage": reason,
        "suppressOutput": delegate_channel_rules.CODEX_WARNING_SUPPRESS_OUTPUT,
    })


def _record_supervisor_fallback(
    home, event: str, payload_bytes: bytes, detail: str, state_dir: Path | None
) -> None:
    """Leave a durable trace that this hook answered without running the guard.

    stdout alone disappears with the turn, and a fallback that looks like a normal
    pass is what let a permanently-failing wrapper hide.  The row also makes the
    unpaired spawn visible to the existing ``board.py lint`` miss scan, because no
    ``decision_allow`` was ever recorded for it.  A failing ledger write
    propagates; the guard entry point's fail-open reports it with the same
    fallback envelope.
    """
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, Mapping):
            payload = {}
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}
    codex_observation._append_codex_observation(
        home, payload,
        hook_event_name=event,
        status=delegate_channel_rules.CODEX_SUPERVISOR_FALLBACK_STATUS,
        reason=(
            f"[delegate-channel/warn] {delegate_channel_rules.CODEX_SUPERVISOR_FALLBACK_MARKER}: "
            f"{detail} — 가드 판정 없이 통과(fail-open)"
        ),
        state_dir=state_dir,
    )


def supervise_codex_hook(
    home, event: str,
    child_argv: Sequence[str],
    *,
    payload_bytes: bytes,
    state_dir: Path | None = None,
) -> dict[str, object]:
    """Run the guard as a bounded child and answer with a valid envelope either way.

    The guard owns the decision; this layer only bounds its runtime, checks the
    envelope against the engine's own key sets, and substitutes a complete
    warning envelope when the child cannot answer.  Nothing here re-types the
    envelope schema, and nothing here needs shell quoting.

    ``child_argv`` comes from the adapter hook config, which the host already
    executes verbatim, so this adds no authority beyond that trust boundary.
    """
    if event not in delegate_channel_rules.CODEX_SUPERVISOR_EVENTS:
        return _codex_supervisor_fallback(
            delegate_channel_rules.CODEX_SUPERVISOR_EVENTS[0], f"미지 hook 이벤트 {event!r}"
        )
    argv = [str(token) for token in child_argv]
    if not argv:
        detail = "가드 실행 커맨드 없음"
        _record_supervisor_fallback(home, event, payload_bytes, detail, state_dir)
        return _codex_supervisor_fallback(event, detail)
    try:
        completed = subprocess.run(
            argv,
            input=payload_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=delegate_channel_rules.CODEX_SUPERVISOR_TIMEOUT_SECONDS,
        )
        returncode = getattr(completed, "returncode", None)
        if returncode != 0:
            raise ValueError(f"가드 rc={returncode}")
        stdout = getattr(completed, "stdout", b"") or b""
        candidate = json.loads(stdout.decode("utf-8"))
        if not isinstance(candidate, dict):
            raise ValueError("가드 stdout 이 JSON 객체가 아님")
        envelope = codex_envelope._validated_codex_envelope(candidate)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
        detail = f"{type(exc).__name__}: {detail}"
        _record_supervisor_fallback(home, event, payload_bytes, detail, state_dir)
        return _codex_supervisor_fallback(event, detail)
    return envelope
