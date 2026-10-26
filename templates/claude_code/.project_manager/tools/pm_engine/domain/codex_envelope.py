"""Codex 훅 엔벨로프 문법 (층: domain)."""
from __future__ import annotations

from collections.abc import Mapping
from pm_engine.domain import delegate_channel_rules



def _validated_codex_envelope(envelope: Mapping[str, object]) -> dict[str, object]:
    """Fail loud on a partial host envelope instead of emitting it silently.

    Every emitter routes its return value through here, so a missing key is a
    guard failure (absorbed by the fail-open boundary into a complete warning
    envelope) instead of a host-side ``KeyError`` that drops the block reason.
    """
    if not isinstance(envelope, Mapping):
        raise ValueError(
            f"Codex 훅 엔벨로프가 mapping 이 아님: {type(envelope).__name__}"
        )
    keys = set(envelope)
    if not keys:
        # The measured allow shape is an empty object; the tool proceeds untouched.
        return {}

    deny_markers = delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS - delegate_channel_rules.CODEX_WARNING_ENVELOPE_KEYS
    if keys & deny_markers:
        missing = sorted(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS - keys)
        unknown = sorted(keys - delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS)
        hook_output = envelope.get("hookSpecificOutput")
        hook_keys = set(hook_output) if isinstance(hook_output, Mapping) else set()
        hook_missing = sorted(delegate_channel_rules.CODEX_DENY_HOOK_OUTPUT_KEYS - hook_keys)
        hook_unknown = sorted(hook_keys - delegate_channel_rules.CODEX_DENY_HOOK_OUTPUT_KEYS)
        if missing or unknown or hook_missing or hook_unknown:
            raise ValueError(
                "Codex deny 엔벨로프 필드 불일치 — 누락 "
                f"{missing or hook_missing}, 예상 밖 {unknown or hook_unknown}"
            )
        reason = envelope["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Codex deny 엔벨로프 reason 이 비었다")
        if (
            envelope["decision"] != "block"
            or hook_output["hookEventName"] != "PreToolUse"
            or hook_output["permissionDecision"] != "deny"
        ):
            raise ValueError("Codex deny 엔벨로프 판정 필드가 measured 값과 다르다")
        if (
            hook_output["permissionDecisionReason"] != reason
            or envelope["systemMessage"] != reason
        ):
            raise ValueError("Codex deny 엔벨로프 사유 3필드가 서로 다르다")
        if envelope["suppressOutput"] is not delegate_channel_rules.CODEX_DENY_SUPPRESS_OUTPUT:
            raise ValueError(
                "Codex deny 엔벨로프 suppressOutput 이 measured 값과 다르다: "
                f"{envelope['suppressOutput']!r}"
            )
        return dict(envelope)

    if keys == set(delegate_channel_rules.CODEX_WARNING_ENVELOPE_KEYS):
        message = envelope["systemMessage"]
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Codex 경고 엔벨로프 systemMessage 가 비었다")
        if envelope["suppressOutput"] is not delegate_channel_rules.CODEX_WARNING_SUPPRESS_OUTPUT:
            raise ValueError(
                "Codex 경고 엔벨로프 suppressOutput 이 measured 값과 다르다: "
                f"{envelope['suppressOutput']!r}"
            )
        return dict(envelope)

    if keys == set(delegate_channel_rules.CODEX_OBSERVATION_ENVELOPE_KEYS):
        if not isinstance(envelope["suppressOutput"], bool):
            raise ValueError("Codex 관측 엔벨로프 suppressOutput 이 bool 이 아니다")
        return dict(envelope)

    raise ValueError(f"알 수 없는 Codex 훅 엔벨로프 형태: {sorted(keys)}")


def _codex_decision_envelope(result: Mapping[str, str]) -> dict[str, object]:
    """Render the exact host-verified Codex 0.147.0 PreToolUse deny envelope.

    Isolated ``CODEX_HOME`` + ``--dangerously-bypass-hook-trust`` evidence:
    the five-field object below (including ``systemMessage`` and
    ``suppressOutput:false``) produced ``PreToolUse=4``, ``error=2``, and
    ``SubagentStart=0``; the same isolated control with an empty allow object
    produced ``SubagentStart=1``.  These exact bytes are shipped and asserted
    by the wrapper regression test.  A 2026-08-12 rerun of both
    cases was blocked before hook execution by the executor network sandbox
    (WebSocket/HTTPS ``Operation not permitted``; rc=1, ``PreToolUse=0`` and
    ``SubagentStart=0`` for each), so those zeroes are not enforcement evidence.
    The current hooks documentation says unsupported PreToolUse
    ``suppressOutput`` should fail the hook and continue the tool, but the
    measured 0.147.0 host applied the deny before reporting those errors.

    2026-08-12 three-cell re-measurement (codex-cli 0.147.0, isolated
    ``CODEX_HOME`` + ``--dangerously-bypass-hook-trust`` + tee hook, one live
    spawn attempt per cell) settles the repeated review objection:

    ==========================================  ================  ==============
    variant                                     PreToolUse spawn  SubagentStart
    ==========================================  ================  ==============
    deny WITH ``suppressOutput: false``                        1               0
    deny WITHOUT ``suppressOutput``                            1               0
    allow control (hook returns nothing)                       1               1
    ==========================================  ================  ==============

    The field is irrelevant to enforcement: the deny blocks the spawn either
    way, and only the allow control reaches ``SubagentStart``.  The shipped
    five-field object is therefore kept because those exact bytes are the
    measured ones; dropping a field would ship bytes no probe has covered.
    Re-probe end to end on a newer host before changing this; stdout schema
    inspection alone is not evidence of enforcement.
    """
    if result["verdict"] != "deny":
        return {}
    reason = result["reason"]
    return _validated_codex_envelope({
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
        "suppressOutput": delegate_channel_rules.CODEX_DENY_SUPPRESS_OUTPUT,
    })


def _codex_spawn_role(payload: Mapping[str, object]) -> str:
    tool_input = payload.get("tool_input")
    value = (
        tool_input.get(delegate_channel_rules.CODEX_ROLE_INPUT_FIELD)
        if isinstance(tool_input, Mapping)
        else None
    )
    return value.strip() if isinstance(value, str) else ""
