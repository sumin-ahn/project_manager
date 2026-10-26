"""위임 채널 처방 문구 (층: app)."""
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
            f"(실행형 처방: backbone `{delegate_env._prescribed_interpreter()} "
            f".project_manager/tools/pm_delegate.py --role {role} "
            f"--cluster {delegate_channel_rules._CLUSTER_PLACEHOLDER} --cwd {delegate_channel_rules._WORKTREE_PLACEHOLDER}`)"
        )
    tier_arg = " --tier hard" if tier == "hard" else ""
    prompt_file = delegate_channel_rules._CODEX_PROMPT_DIR / f"manual-{role}-{tier}-prompt.md"
    return (
        f"{relay._runtime_skill_entry('pm-dev-delegate')} 스킬로 위임하라 "
        f"(실행형 처방: 1) 프롬프트를 파일로 저장한 뒤"
        f"(경로: `{prompt_file.as_posix()}`) "
        f"2) backbone `{delegate_env._prescribed_interpreter()} .project_manager/tools/pm_delegate.py "
        f"--role {role}{tier_arg} --prompt-file {prompt_file.as_posix()} "
        f"--cwd {delegate_channel_rules._WORKTREE_PLACEHOLDER} --ticket {delegate_channel_rules._TICKET_PLACEHOLDER}`)"
    )


def _claude_native_model_warning(home, role: str, tier: str, model: str) -> str:
    """Return an allow+loud warning for card drift, otherwise an empty string."""
    relative = native_agent_card.CLAUDE_NATIVE_AGENT_CARDS.get((role, tier))
    shown_path = relative.as_posix() if relative is not None else "(명시 매핑 없음)"
    try:
        card_model, relative = native_agent_card._read_claude_native_agent_model(home, role, tier)
    except Exception as exc:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        return (
            f"[delegate-channel/warn] Claude native model 검사 실패"
            f"(role={role}, tier={tier}, conf_model={model}, card={shown_path}, "
            f"error={type(exc).__name__}: {detail}) — native 통과(fail-open)"
        )
    if delegate_channel_rules._is_unrendered_model_token(card_model):
        # 카드가 아직 렌더되지 않았다 — 렌더 소스 트리(프레임워크 루트·templates)이거나 채택자
        # 렌더가 실패한 상태다. 이건 conf 를 고칠 일이 아니라 `pm-update` 로 카드를 다시 렌더할
        # 일이므로 "불일치" 와 다른 처방을 낸다. 판정은 여전히 fail-open.
        return (
            f"[delegate-channel/warn] Claude native model 카드 미렌더"
            f"(role={role}, tier={tier}, conf_model={model}, "
            f"card={relative.as_posix()}, card_model={card_model}) — "
            "카드가 렌더 소스 상태다: 채택자 트리면 `pm-update` 로 카드를 다시 렌더하라. "
            "native 통과(fail-open)"
        )
    if card_model != model:
        return (
            f"[delegate-channel/warn] Claude native model 불일치"
            f"(role={role}, tier={tier}, conf_model={model}, "
            f"card={relative.as_posix()}, card_model={card_model}) — "
            "native 통과(fail-open)"
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
    """Apply decision-table rows 0 through 5 without a hook-specific envelope.

    Row 0.5 (master switch) sits between name normalisation and mapping
    resolution: ``delegate.enabled=false`` denies every role spawn regardless of
    channel.  The guard's own failures stay fail-open — a broken guard must not
    lock delegation out, because the recovery path itself needs delegation.
    """
    known_harnesses = delegate_env._known_harnesses()
    self_name = self_harness.strip().lower() if isinstance(self_harness, str) else ""

    # Row 0: a broken/misidentified adapter must never false-deny delegation.
    if not self_name or self_name not in known_harnesses:
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/warn] self_harness={self_name or '(empty)'} 미상 — 통과(fail-open)",
        )

    role_name = role.strip() if isinstance(role, str) else ""
    tier_name = tier.strip().lower() if isinstance(tier, str) else ""

    # Row 1: unknown names are recorded instead of becoming a silent no-op.
    # ``record`` is intentionally not ``warn``: adapters reserve user-facing
    # warnings/system-context injection for decision infrastructure failures.
    if role_name not in delegate_channel_rules.DELEGATE_ROLES or tier_name not in delegate_channel_rules.DELEGATE_TIERS:
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/record] 역할/티어 정규화 실패({role_name or '(empty)'}/{tier_name or '(empty)'}) — 통과(fail-open)",
        )
    if tier_name == "hard" and role_name != "developer":
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/record] 미지원 역할/티어({role_name}/{tier_name}) — 통과(fail-open)",
        )
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

    if delegate_channel_rules._delegate_mapping_config_is_absent(conf):
        return delegate_channel_rules._result("allow", "[delegate-channel/allow] 역할 매핑 미설정")
    mapping_unset = delegate_channel_rules._mapping_is_unset(role_name, tier_name, conf)
    harness, model, resolution_error = delegate_config._resolved_mapping(role_name, tier_name, conf)

    # The engine rejects an absent/incomplete profile (hard included) instead
    # of inheriting another tier.  Keep native available because this guard's
    # infrastructure boundary is fail-open, but surface the same engine fact
    # and never prescribe an execution-impossible ``--tier hard`` redirect.
    if resolution_error:
        channel = "record" if mapping_unset else "warn"
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/{channel}] 설정 해소 실패"
            f"({resolution_error}) — native 통과(fail-open)",
        )

    # Row 3: no configured destination leaves native delegation available.
    if not harness:
        return delegate_channel_rules._result("allow", "[delegate-channel/allow] 역할 harness 미설정")

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

    # Row 5: cross-harness native spawn redirects to the ledgered CLI.
    shown_model = model or "(model 미설정)"
    return delegate_channel_rules._result(
        "deny",
        f"[delegate-channel/deny] conf 는 {harness}/{shown_model} — "
        f"{_remediation(role_name, tier_name)}",
        harness=harness,
        model=model,
    )


def _is_absolute_path(value: str) -> bool:
    """Whether the hook-supplied cwd is absolute **by the running platform's rules**.

    The payload comes from the host process on this machine, so the rule is the
    local one: POSIX needs a leading ``/`` while Windows needs a drive or UNC
    root (``C:\\work`` is absolute there, ``/work`` is drive-relative and
    ``C:\\work`` is not absolute on POSIX).  Anything else keeps the caller on
    the measured fail-open path instead of prescribing an unpastable cwd.  Kept
    as one injectable seam so the other platform's branch is testable here.
    """
    return Path(value).is_absolute()


def _rendered_prescription_token(value: str) -> str:
    """Quote one prescription argument with the engine's single shell-render seam.

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

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, Mapping):
        return None
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
) -> dict[str, object] | None:
    """Adapt Codex native spawn input to the shared decision function."""
    if payload.get("tool_name") != delegate_channel_rules.CODEX_SPAWN_TOOL_NAME:
        return None

    tool_input = payload.get("tool_input") or {}
    agent_name = (
        tool_input.get(delegate_channel_rules.CODEX_ROLE_INPUT_FIELD)
        if isinstance(tool_input, Mapping)
        else None
    )
    cwd = payload.get("cwd")
    result = _decide_agent_input(
        home, agent_name if isinstance(agent_name, str) else "", "codex",
        cwd=cwd.strip() if isinstance(cwd, str) and cwd.strip() and _is_absolute_path(cwd.strip()) else None,
        config_loader=config_loader,
    )

    if result["verdict"] == "deny":
        if (
            not isinstance(cwd, str)
            or not cwd.strip()
            or not _is_absolute_path(cwd.strip())
        ):
            result = delegate_channel_rules._result(
                "allow",
                "[delegate-channel/warn] Codex PreToolUse cwd 절대경로 누락 — 통과(fail-open)",
            )
        else:
            result = _materialize_cwd(result, cwd)
    return result


def evaluate_codex_hook(
    home, payload: Mapping[str, object],
    *,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, object] | None:
    """Return only the host envelope; keep allow decisions out of host output."""
    result = _evaluate_codex_decision(home, payload, config_loader=config_loader)
    return None if result is None else codex_envelope._codex_decision_envelope(result)


def _decide_agent_input(
    home, agent_name: str, self_harness: str, *, tier: str | None = None,
    cwd: object | None = None,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, str]:
    """CLI와 Codex 입력의 역할 정규화·설정 판독·채널 판정."""
    normalized = delegate_channel_rules.normalize_agent_name(agent_name, self_harness, tier)
    if normalized is None:
        # 미지 하네스 판정을 역할 정규화보다 먼저 적용한다.
        if self_harness.strip().lower() not in delegate_env._known_harnesses():
            result = decide(home, agent_name, tier or "normal", {}, self_harness)
        else:
            result = delegate_channel_rules._result(
                "allow",
                f"[delegate-channel/record] 에이전트명 정규화 실패({self_harness}/{agent_name}) — 통과(fail-open)",
            )
    else:
        role, tier = normalized
        result = decide(home, role, tier, config_loader(home), self_harness)
    return _materialize_cwd(result, cwd)
