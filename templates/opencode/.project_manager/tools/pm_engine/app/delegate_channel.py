"""위임 채널 해결 방법 문구 (층: app)."""
from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from pm_engine.domain import codex_envelope
from pm_engine.infra import conf_store
from pm_engine.infra.harness import contract
from pm_engine.infra import delegate_config
from pm_engine.domain import home
from pm_engine.infra import native_agent_card

from pm_engine.domain import delegate_channel_rules
from pm_engine.infra.harness import delegate_env
from pm_engine.app.session import relay


def _remediation(role: str, tier: str) -> str:
    if role == delegate_channel_rules._REVIEW_ROLE:
        return (
            f"{relay._runtime_skill_entry('pm-dev-delegate')} 스킬로 위임하라 "
            f"(실행형 해결 방법: backbone `{delegate_env._prescribed_interpreter()} "
            f".project_manager/tools/pm_delegate.py --role {role} "
            f"--cluster {delegate_channel_rules._CLUSTER_PLACEHOLDER} --cwd {delegate_channel_rules._WORKTREE_PLACEHOLDER}`)"
        )
    tier_arg = " --tier hard" if tier == "hard" else ""
    prompt_file = delegate_channel_rules._CODEX_PROMPT_DIR / f"manual-{role}-{tier}-prompt.md"
    return (
        f"{relay._runtime_skill_entry('pm-dev-delegate')} 스킬로 위임하라 "
        f"(실행형 해결 방법: 1) 프롬프트를 파일로 저장한 뒤"
        f"(경로: `{prompt_file.as_posix()}`) "
        f"2) backbone `{delegate_env._prescribed_interpreter()} .project_manager/tools/pm_delegate.py "
        f"--role {role}{tier_arg} --prompt-file {prompt_file.as_posix()} "
        f"--cwd {delegate_channel_rules._WORKTREE_PLACEHOLDER} --ticket {delegate_channel_rules._TICKET_PLACEHOLDER}`)"
    )


def _claude_native_model_warning(home, role: str, tier: str, model: str) -> str:
    """Return an allow+loud warning for card drift, otherwise an empty string.

    A card that cannot be read raises; the adapter boundary blocks the delegation call.
    """
    card_model, relative = native_agent_card._read_claude_native_agent_model(home, role, tier)
    if card_model is None or delegate_channel_rules._is_unrendered_model_token(card_model):
        # 카드가 지금 설정으로 렌더되지 않았다 — 렌더 소스 트리(프레임워크 루트·templates)의 토큰이거나,
        # 설정에 모델이 없을 때 렌더돼 model 줄이 주석으로 남은 카드다. conf 를 고칠 일이 아니라
        # `pm-update` 로 카드를 다시 렌더할 일이므로 "불일치" 와 다른 해결 방법을 낸다.
        return (
            f"[delegate-channel/warn] Claude native model 카드 미렌더"
            f"(role={role}, tier={tier}, conf_model={model}, "
            f"card={relative.as_posix()}, card_model={card_model or '(없음)'}) — "
            "카드가 지금 설정으로 렌더되지 않았다: 채택자 트리면 `pm-update` 로 카드를 다시 렌더하라. "
            "위임은 막지 않는다"
        )
    if card_model != model:
        return (
            f"[delegate-channel/warn] Claude native model 불일치"
            f"(role={role}, tier={tier}, conf_model={model}, "
            f"card={relative.as_posix()}, card_model={card_model}) — "
            "위임은 막지 않는다"
        )
    return ""


def _delegate_is_enabled(conf: Mapping[str, object]) -> bool:
    """위임 마스터 스위치 — **키 부재·빈값은 허용**(기본 ON)이고 명시 거부만 차단한다.

    빈값을 거부로 읽으면 `delegate.enabled=` 한 줄이 위임 전체를 조용히 끈다 — conf 파싱의
    "마지막 값이 비면 미설정" 의미를 그대로 이어받아 기본값으로 간다.
    엔진(`delegate_config._is_enabled`)과 같은 기본값·같은 참 낱말 집합이다.
    """
    raw = str(conf.get(delegate_channel_rules._DELEGATE_SWITCH_KEY, "")).strip().lower()
    if not raw:
        return True
    return raw in ("true", "1", "yes", "on")


def decide(
    home, role: object,
    tier: object,
    conf: Mapping[str, object],
    self_harness: object,
) -> dict[str, str]:
    """Apply decision-table rows 0.5 through 5 without a hook-specific envelope.

    The entry normalises its input first: a known harness and a framework role and
    tier.  Anything else is not a decision input and raises, like every failure
    outside these rows; the adapter boundary that ran the guard blocks that
    delegation call.  Row 0.5 (master switch) sits before mapping resolution:
    ``delegate.enabled=false`` denies every role spawn regardless of channel.
    """
    self_name = self_harness.strip().lower() if isinstance(self_harness, str) else ""
    if self_name not in delegate_env._known_harnesses():
        raise ValueError(f"위임 채널 판정 입력의 하네스를 알 수 없다: {self_harness!r}")
    role_name = role.strip() if isinstance(role, str) else ""
    tier_name = tier.strip().lower() if isinstance(tier, str) else ""
    if (
        role_name not in delegate_channel_rules.DELEGATE_ROLES
        or tier_name not in delegate_channel_rules.DELEGATE_TIERS
        or (tier_name == "hard" and role_name != "developer")
    ):
        raise ValueError(f"위임 채널 판정 입력의 역할/티어가 프레임워크 역할이 아니다: {role!r}/{tier!r}")
    if not isinstance(conf, Mapping):
        raise TypeError("delegate config must be a mapping")

    # Row 0.5: the master switch is decided **before** any mapping resolution.
    # ``delegate.enabled`` is channel-agnostic (default on), so an explicit off
    # denies native and cross alike — a mapping-shaped remediation would be the
    # wrong prescription because the mapping is not what is blocking.
    if not _delegate_is_enabled(conf):
        return delegate_channel_rules._result(
            "deny",
            f"[delegate-channel/deny] 위임이 꺼져 있습니다 — {native_agent_card._ENABLED_REMEDIATION}",
        )

    # Row 2: a role (tier) without a harness/model mapping keeps native delegation;
    # its card runs on the harness default model.  Hard never inherits normal.
    if delegate_channel_rules._mapping_is_unset(role_name, tier_name, conf):
        key = f"delegate.{role_name}" + (".hard" if tier_name == "hard" else "")
        return delegate_channel_rules._result(
            "allow", f"[delegate-channel/allow] 역할 매핑 미설정({key}.harness/.model)"
        )

    # Row 3: a mapping the engine cannot resolve (harness or model missing, an
    # unknown harness) is a configuration error.  Deny with the engine's own reason,
    # the same words ``pm_delegate`` prints for this setting, so the operator knows
    # which key to fix; never prescribe a redirect the engine itself would reject.
    harness, model, resolution_error = delegate_config._resolved_mapping(role_name, tier_name, conf)
    if resolution_error:
        return delegate_channel_rules._result(
            "deny", f"[delegate-channel/deny] 위임 설정 오류: {resolution_error}"
        )

    # Row 4: the native surface matches configuration.
    if harness == self_name:
        if self_name == "claude":
            warning = _claude_native_model_warning(home, role_name, tier_name, model)
            if warning:
                return delegate_channel_rules._result(
                    "allow", warning, harness=harness, model=model
                )
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/allow] conf 와 native harness 일치({harness})",
            harness=harness,
            model=model,
        )

    # Row 5: cross-harness native spawn redirects to the recorded CLI.
    return delegate_channel_rules._result(
        "deny",
        f"[delegate-channel/deny] conf 는 {harness}/{model} — "
        f"{_remediation(role_name, tier_name)}",
        harness=harness,
        model=model,
    )


def _is_absolute_path(value: str) -> bool:
    """Whether the hook-supplied cwd is absolute **by the running platform's rules**.

    The payload comes from the host process on this machine, so the rule is the
    local one: POSIX needs a leading ``/`` while Windows needs a drive or UNC
    root (``C:\\work`` is absolute there, ``/work`` is drive-relative and
    ``C:\\work`` is not absolute on POSIX).  Anything else is not a decision
    input: the Codex adapter raises and the ``decide`` CLI rejects ``--cwd``.  Kept
    as one injectable override so the other platform's branch is testable here.
    """
    return Path(value).is_absolute()


def _rendered_prescription_token(value: str) -> str:
    """Quote one prescription argument with the engine's single shell-render function.

    The prescription is pasted into the operator's shell, so POSIX quoting on
    Windows turns ``--cwd`` and its path into literal arguments and the command
    fails.  ``delegate_env`` owns the platform rules; this guard never
    assembles quoting of its own.
    """
    return delegate_env.render_shell_token(
        value, windows=contract.running_on_windows()
    )


def _materialize_cwd(result: Mapping[str, str], cwd: object | None) -> dict[str, str]:
    """Replace only the known worktree placeholder with a concrete adapter cwd."""
    materialized = dict(result)
    if not isinstance(cwd, str) or not cwd.strip():
        return materialized
    reason = materialized["reason"]
    if delegate_channel_rules._WORKTREE_PLACEHOLDER not in reason:
        # Allow rows have no prescription; keep the engine load off that path.
        return materialized
    materialized["reason"] = reason.replace(
        delegate_channel_rules._WORKTREE_PLACEHOLDER, _rendered_prescription_token(cwd.strip())
    )
    return materialized


def evaluate_hook(
    home, payload: Mapping[str, object],
    *,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, object] | None:
    """Return a Claude deny/warning envelope, or ``None`` for a quiet allow."""
    tool_name = payload.get("tool_name")
    if tool_name != "Agent":
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        raise TypeError(
            f"Claude PreToolUse 훅 입력 tool_input 이 JSON 객체가 아니다: {type(tool_input).__name__}"
        )
    normalized = delegate_channel_rules.normalize_agent_name(tool_input.get("subagent_type"), "claude")
    if normalized is None:
        return None
    role, tier = normalized

    conf = config_loader(home)
    result = decide(home, role, tier, conf, "claude")
    if result["verdict"] != "deny" and not result["reason"].startswith(
        "[delegate-channel/warn]"
    ):
        return None
    result = _materialize_cwd(result, payload.get("cwd"))
    if result["verdict"] != "deny":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": result["reason"],
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": result["reason"],
        }
    }


def _evaluate_codex_decision(
    home, payload: Mapping[str, object],
    *,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, object]:
    """Adapt Codex native spawn input to the shared decision function.

    The adapter dispatcher routes only spawn payloads here; this function does not
    re-check the tool name.  The hook ``cwd`` fills the ``--cwd`` of a deny
    prescription, so an input without an absolute ``cwd`` is not a decision input:
    it raises and the dispatcher blocks the call."""
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not _is_absolute_path(cwd.strip()):
        raise ValueError(f"Codex PreToolUse 훅 입력 cwd 가 절대경로가 아니다: {cwd!r}")
    # spawn 인자가 객체가 아니거나 agent_type 이 문자열이 아니면 Codex 가 이 훅 뒤 인자 해석에서 그
    # 호출을 거부해 서브에이전트가 뜨지 않는다(0.166.0 실측) — 판정 대상이 아니다.
    tool_input = payload.get("tool_input") or {}
    agent_name = (
        tool_input.get(delegate_channel_rules.CODEX_ROLE_INPUT_FIELD)
        if isinstance(tool_input, Mapping)
        else None
    )
    return _decide_agent_input(
        home, agent_name if isinstance(agent_name, str) else "", "codex",
        cwd=cwd.strip(), config_loader=config_loader,
    )


def evaluate_codex_hook(
    home, payload: Mapping[str, object],
    *,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, object]:
    """Return only the host envelope; keep allow decisions out of host output."""
    return codex_envelope._codex_decision_envelope(
        _evaluate_codex_decision(home, payload, config_loader=config_loader))


def _decide_agent_input(
    home, agent_name: str, self_harness: str, *, tier: str | None = None,
    cwd: object | None = None,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, str]:
    """CLI와 Codex 입력의 역할 정규화·설정 판독·채널 판정."""
    normalized = delegate_channel_rules.normalize_agent_name(agent_name, self_harness, tier)
    if normalized is None:
        # 미지 하네스는 역할 정규화보다 먼저 판정 입력이 아니라고 올린다.
        if self_harness.strip().lower() not in delegate_env._known_harnesses():
            raise ValueError(f"위임 채널 판정 입력의 하네스를 알 수 없다: {self_harness!r}")
        result = delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/record] 프레임워크 역할이 아닌 에이전트({self_harness}/{agent_name}) — 판정 대상 아님",
        )
    else:
        role, tier = normalized
        result = decide(home, role, tier, config_loader(home), self_harness)
    return _materialize_cwd(result, cwd)
