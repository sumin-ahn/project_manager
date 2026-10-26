"""위임 채널 이름과 판정 값 (층: domain)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re



DELEGATE_ROLES = frozenset(
    {"developer", "code-reviewer", "researcher", "architect"}
)
DELEGATE_TIERS = frozenset({"normal", "hard"})
_WORKTREE_PLACEHOLDER = "<worktree>"
# 훅은 티켓·묶음 컨텍스트를 모른다(Agent 도구 입력엔 ticket 필드가 없다) — 실값을 채울 수
# 없으므로 사람이 채워 넣을 자리만 처방한다. 대상 인자가 없으면 라운드 파일이 안 생겨 PM 이 수동
# prepare 로 우회하는데 그 우회가 cross 역할에서 거부된다(두 처방이 서로를 막는 교착). 리뷰는
# 묶음 단위(`--cluster`)이고 엔진이 프롬프트를 조립하므로 프롬프트 파일을 받지 않는다.
_TICKET_PLACEHOLDER = "<T-NNNN>"
_CLUSTER_PLACEHOLDER = "C-<ticket-group>"
_REVIEW_ROLE = "code-reviewer"
_CODEX_PROMPT_DIR = Path(".project_manager/.local/delegate")

# codex-cli 0.147.0 live hook payload: native delegation exposes this exact
# separator-free tool name and carries the selected custom-agent name in
# ``tool_input.task_name``.  ``SubagentStart.agent_type`` is merely ``default``;
# it is never used as a delegation role or correlation value.
CODEX_SPAWN_TOOL_NAME = "collaborationspawn_agent"
CODEX_ROLE_INPUT_FIELD = "task_name"
CODEX_CORRELATION_FIELDS = ("session_id", "turn_id", "agent_id")

# Host envelope shapes.  The hook must never emit a partially built
# object: a consumer that reads ``hookSpecificOutput``/``reason`` gets a
# ``KeyError`` and the block reason never reaches the operator.  These sets are
# the single truth for both the emitters and their regression guards.
CODEX_DENY_ENVELOPE_KEYS = frozenset({
    "decision", "reason", "hookSpecificOutput", "systemMessage", "suppressOutput",
})
CODEX_DENY_HOOK_OUTPUT_KEYS = frozenset({
    "hookEventName", "permissionDecision", "permissionDecisionReason",
})
CODEX_WARNING_ENVELOPE_KEYS = frozenset({"systemMessage", "suppressOutput"})
CODEX_OBSERVATION_ENVELOPE_KEYS = frozenset({"suppressOutput"})
# ``suppressOutput`` is decided by the measured host protocol, not by the
# operating system: the deny/warning envelopes set ``False`` everywhere, and
# only the quiet SubagentStart observation carries ``True``.  The Windows
# measurement that showed ``{"suppressOutput": true}`` alone was the adapter's
# fail-soft fallback firing because no interpreter had been resolved (the
# ``python3`` WindowsApps shim exits rc 9009), not a platform-dependent value.
CODEX_DENY_SUPPRESS_OUTPUT = False
CODEX_WARNING_SUPPRESS_OUTPUT = False
CODEX_OBSERVATION_SUPPRESS_OUTPUT = True

# Hook supervision.  The adapter used to inline this as a ``python -c``
# script, but PowerShell 5.1 does not preserve double quotes in native command
# arguments (measured on Windows 11: ``py -3.12 -c 'print("quoted")'`` raises
# ``NameError: name 'quoted' is not defined``), so every quoted literal in that
# script was destroyed and the wrapper fell back on every single call — the deny
# guard never fired.  Supervision therefore lives here, in an already-shipped
# file that the hook invokes by path, and the adapter carries no quoted script.
CODEX_SUPERVISOR_TIMEOUT_SECONDS = 8
CODEX_SUPERVISOR_EVENTS = ("PreToolUse", "SubagentStart")
# Which layer answered is part of the answer: a fallback envelope must never be
# indistinguishable from a guard that actually ran (as seen in the earlier fallback incident).
CODEX_SUPERVISOR_FALLBACK_MARKER = "supervisor-fallback"
CODEX_ADAPTER_FALLBACK_MARKER = "adapter-fallback"
CODEX_SUPERVISOR_FALLBACK_STATUS = "wrapper_fallback"

# Agent names are harness-owned surface literals.  Keep their translation in
# this Python truth rather than duplicating the role set in JavaScript adapters.
# Every harness now ships a hard developer card, so all three encode that tier
# in the agent name.  The name label and the card mapping below are registered
# together: a half registration either strands the card (unknown agent name) or
# leaves the guard permanently warning about a missing card mapping.
HARD_TIER_AGENT_NAME = "developer-hard"
AGENT_NAME_PROFILES: dict[str, dict[str, tuple[str, str]]] = {
    harness: {
        **{role: (role, "normal") for role in DELEGATE_ROLES},
        HARD_TIER_AGENT_NAME: ("developer", "hard"),
    }
    for harness in ("claude", "opencode", "codex")
}
_FRONTMATTER_MODEL_RE = re.compile(r"^model\s*:\s*(.*)$")


def _result(
    verdict: str,
    reason: str,
    *,
    harness: str = "",
    model: str = "",
) -> dict[str, str]:
    """Build the stable one-line-CLI payload shape."""
    return {
        "verdict": verdict,
        "reason": reason,
        "harness": harness,
        "model": model,
    }


def normalize_agent_name(
    agent_name: object,
    self_harness: object,
    tier: object | None = None,
) -> tuple[str, str] | None:
    """Normalize a harness agent literal to ``(role, tier)``.

    An explicit tier is accepted for adapters whose tool surface carries it in
    a separate field.  Conflicts with an encoded tier fail open at the caller.
    """
    if not isinstance(agent_name, str) or not isinstance(self_harness, str):
        return None
    name = agent_name.strip()
    harness = self_harness.strip().lower()
    profile = AGENT_NAME_PROFILES.get(harness, {})
    normalized = profile.get(name)
    if normalized is None:
        return None

    if tier is None:
        return normalized
    if not isinstance(tier, str):
        return None
    explicit_tier = tier.strip().lower()
    if explicit_tier not in DELEGATE_TIERS:
        return None
    encoded_role, encoded_tier = normalized
    if encoded_tier != "normal" and explicit_tier != encoded_tier:
        return None
    if explicit_tier == "hard" and encoded_role != "developer":
        return None
    return encoded_role, explicit_tier


def _mapping_is_unset(
    role: str,
    tier: str,
    conf: Mapping[str, object],
) -> bool:
    """Whether the engine-owned atomic harness/model tuple is wholly absent."""
    key = f"delegate.{role}" + (".hard" if tier == "hard" else "")
    return not any(
        str(conf.get(f"{key}.{field}") or "").strip()
        for field in ("harness", "model")
    )


# The master switch shares the ``delegate.`` prefix with the role mapping but is
# not a mapping key — counting it as one would make a switch-only conf look
# "configured" and turn Row 5's quiet allow into a resolution warning.
_DELEGATE_SWITCH_KEY = "delegate.enabled"


def _delegate_mapping_config_is_absent(conf: Mapping[str, object]) -> bool:
    """Whether this clone has no primary/fallback delegation mapping at all."""
    return not any(
        str(key).startswith("delegate.") and str(key) != _DELEGATE_SWITCH_KEY
        for key in conf
    )


def _frontmatter_model_scalar(value: str) -> str:
    """Parse one model scalar with the project's PyYAML frontmatter grammar."""
    # Hook interpreters may lack the optional runtime dependency even though
    # project management commands normally install it.  Keep module/CLI/hook
    # startup alive; the caller converts this local import failure to the same
    # allow+loud warning as any other native-card inspection failure.
    import yaml

    parsed = yaml.safe_load(f"model: {value}\n")
    if not isinstance(parsed, dict) or set(parsed) != {"model"}:
        raise ValueError("model frontmatter 파싱 결과가 mapping 하나가 아님")
    candidate = parsed["model"]
    if candidate is None:
        raise ValueError("model 값이 비어 있음")
    if not isinstance(candidate, str):
        raise ValueError("model 값이 string scalar가 아님")
    candidate = candidate.strip()
    if not candidate:
        raise ValueError("model 값이 비어 있음")
    return candidate


# 렌더 토큰 형태. 카드 소스(프레임워크 루트·templates)는 `model: "{{DELEGATE_MODEL_<ROLE>}}"` 이고
# 채택자 트리는 pm_import/pm_update 가 이를 conf 해소값으로 렌더한다. 미해소면 `# model: "<model>"
# # TODO: …` 로 중화되어 `model` 키 자체가 사라진다(그때는 "frontmatter model 없음" 경로).
_UNRENDERED_MODEL_TOKEN_RE = re.compile(r"^\{\{[A-Z][A-Z0-9_]*\}\}$")


def _is_unrendered_model_token(value: str) -> bool:
    """카드 model 값이 렌더되지 않은 operational 토큰인지."""
    return _UNRENDERED_MODEL_TOKEN_RE.fullmatch(value.strip()) is not None
