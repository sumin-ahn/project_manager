"""Codex 훅 관측 (층: app)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from pm_engine.domain import codex_envelope
from pm_engine.infra import codex_observation
from pm_engine.domain import delegate_channel_rules


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
    except OSError as exc:  # 관측 기록 실패는 판정을 바꾸지 않는다(차단 판정 유지 경고).
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
    except OSError as exc:  # 관측 기록 실패는 판정을 바꾸지 않는다(차단 판정 유지 경고).
        return _codex_observation_failure(result, exc)
    return codex_envelope._validated_codex_envelope(result)
