"""T-0653 Codex live-payload spawn delegation-channel guard contract."""

from __future__ import annotations

from pm_engine.app import codex_hook
from pm_engine.app import delegate_channel

from pm_engine.domain import codex_envelope
from pm_engine.infra import codex_observation
from pm_engine.infra.harness import contract
from pm_engine.domain import delegate_channel_rules
from pm_engine.infra.harness import delegate_env
from pm_engine.infra import pm_paths

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import time
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from _win_skip import posix_bash_supported
from _hook_commands import inline_script_payloads
from pm_engine.cli import delegate_channel_guard as cli_delegate_channel_guard


REPO = Path(__file__).resolve().parents[1]
GUARD_PY = REPO / ".project_manager" / "tools" / "delegate_channel_guard.py"
HOOKS_JSON = REPO / "templates" / "codex" / ".codex" / "hooks.json"
DISPATCHER_REL = ".codex/pm_orch_codex.py"
DISPATCHER_PY = REPO / "templates" / "codex" / DISPATCHER_REL
README = REPO / "templates" / "codex" / "README.md"
LIVE_PAYLOADS_FIXTURE = (
    REPO / "tests" / "fixtures" / "codex_0_147_0_live_hook_payloads.json"
)
# spawn 훅 입력의 실측 근거(codex-cli 0.166.0). multi-agent v1·v2 각각의 spawn·wait·SubagentStart.
SPAWN_PAYLOADS_FIXTURE = (
    REPO / "tests" / "fixtures" / "codex_0_166_0_spawn_hook_payloads.json"
)
SPAWN_TOOL_SETS = ("v1", "v2")


@pytest.fixture()
def guard():
    spec = importlib.util.spec_from_file_location("delegate_guard_codex", GUARD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def dispatcher():
    """codex 훅 디스패처(T-0777) — 진입점 뒤 분기 판별식이 여기 산다."""
    spec = importlib.util.spec_from_file_location("pm_orch_codex", DISPATCHER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _feature_by_id(dispatcher_module, feature_id: str):
    """디스패처 registry 의 그 기능 1건 (없으면 red — 등록이 사라진 것)."""
    matches = [feature for feature in dispatcher_module.CODEX_HOOK_FEATURES
               if feature.feature_id == feature_id]
    assert len(matches) == 1, dispatcher_module.CODEX_HOOK_FEATURES
    return matches[0]


def _delegate_feature(dispatcher_module):
    """PreToolUse 스폰 판별을 쥔 위임 채널 기능."""
    return _feature_by_id(dispatcher_module, "delegate-channel")


def _live_evidence() -> dict[str, object]:
    return json.loads(LIVE_PAYLOADS_FIXTURE.read_text(encoding="utf-8"))


def _live_events() -> list[dict[str, object]]:
    events = _live_evidence()["events"]
    assert isinstance(events, list) and len(events) == 4
    return events


def _spawn_evidence() -> dict[str, object]:
    return json.loads(SPAWN_PAYLOADS_FIXTURE.read_text(encoding="utf-8"))


def _spawn_events(tool_set: str) -> list[dict[str, object]]:
    events = _spawn_evidence()["tool_sets"][tool_set]["events"]
    assert isinstance(events, list) and len(events) == 3
    return events


def _live_spawn_payload(tool_set: str = "v2") -> dict[str, object]:
    return deepcopy(_spawn_events(tool_set)[0])


def _live_wait_payload(tool_set: str = "v2") -> dict[str, object]:
    return deepcopy(_spawn_events(tool_set)[1])


def _live_subagent_tool_payload() -> dict[str, object]:
    return deepcopy(_live_events()[2])


def _live_subagent_start(tool_set: str = "v2") -> dict[str, object]:
    return deepcopy(_spawn_events(tool_set)[2])


def _live_role_item(tool_set: str = "v2") -> tuple[str, str]:
    """spawn 입력 중 시작된 서브에이전트의 역할(SubagentStart.agent_type)과 같은 값을 실은 필드."""
    started_role = _live_subagent_start(tool_set)["agent_type"]
    tool_input = _live_spawn_payload(tool_set)["tool_input"]
    assert isinstance(tool_input, dict) and tool_input
    items = [(field, value) for field, value in tool_input.items() if value == started_role]
    assert len(items) == 1, tool_input
    return items[0]


def _live_correlation_fields() -> tuple[str, str, str]:
    fields = tuple(
        field for field in _live_subagent_start() if field.endswith("_id")
    )
    assert len(fields) == 3
    return fields


# 훅 payload 의 cwd 는 **그 호스트가 실행 중인 플랫폼의 절대경로**다 (POSIX `/fixture/…` ·
# Windows `C:\fixture\…`). 한쪽 표기를 고정하면 다른 플랫폼에서 가드가 절대경로 미해소로
# fail-open 하고, deny 엔벨로프 자체가 생성되지 않는다(T-0715 Windows 실측 KeyError 원인).
_PLATFORM_ROOT = Path(Path(__file__).resolve().anchor)


def _fixture_cwd(*parts: str) -> str:
    return str(_PLATFORM_ROOT.joinpath(*parts))


def _quoted_for_local_shell(path: str) -> str:
    """이 플랫폼 셸이 요구하는 인용 (POSIX 단일따옴표 · Windows 큰따옴표·공백 없으면 무인용)."""
    if os.name != "nt":
        return f"'{path}'"
    return f'"{path}"' if " " in path else path


def _payload(
    role: str | None, cwd: str | None = None
) -> dict[str, object]:
    if cwd is None:
        cwd = _fixture_cwd("fixture", "worktree")
    payload = _live_spawn_payload()
    field, _ = _live_role_item()
    tool_input = payload["tool_input"]
    assert isinstance(tool_input, dict)
    if role is None:
        tool_input.pop(field)
    else:
        tool_input[field] = role
    payload["cwd"] = cwd
    return payload


def _subagent_start() -> dict[str, object]:
    return _live_subagent_start()


def test_live_spawn_payload_uses_t0641_decide_cli(guard_home, guard, monkeypatch):
    evidence = _spawn_evidence()
    spawn = _live_spawn_payload()
    role_field, role = _live_role_item()
    assert evidence["captured_on_version"] == "codex-cli 0.166.0"
    # 실측 기록의 cwd 는 경로를 가린 `<worktree>` 다. 판정 입력은 절대경로라야 하므로 이 플랫폼의 절대경로로 바꾼다.
    assert spawn["cwd"] == "<worktree>"
    spawn["cwd"] = _fixture_cwd("fixture", "worktree")
    assert spawn["hook_event_name"] == "PreToolUse"
    # multi-agent v1·v2 모두 역할을 같은 필드에 싣는다(v2 의 task_name 은 작업 이름표다).
    assert {_live_role_item(tool_set)[0] for tool_set in SPAWN_TOOL_SETS} == {role_field}

    cli_calls = []

    def tracked_decision(home, agent_name, self_harness, *, tier=None, cwd=None, config_loader):
        cli_calls.append((home, agent_name, self_harness, tier, cwd, config_loader(home)))
        return delegate_channel_rules._result("allow", "[delegate-channel/allow] fixture")

    monkeypatch.setattr(delegate_channel, "_decide_agent_input", tracked_decision)
    conf = {"delegate.enabled": "true"}

    result = delegate_channel.evaluate_codex_hook(
        guard_home, spawn, config_loader=lambda _home: conf
    )

    assert delegate_channel_rules.CODEX_ROLE_INPUT_FIELD == role_field
    envelope_evidence = codex_envelope._codex_decision_envelope.__doc__ or ""
    for measured in (
        "--dangerously-bypass-hook-trust",
        "five-field object",
        "SubagentStart=0",
        "empty allow object",
        "SubagentStart=1",
        "exact bytes are shipped",
        "documentation",
        "measured 0.147.0 host",
    ):
        assert measured in envelope_evidence
    assert cli_calls == [(guard_home, spawn["tool_input"][role_field], "codex", None, spawn["cwd"], conf)]
    assert result == {}


def test_codex_cross_mapping_denies_with_both_measured_fields_and_real_cwd(
    guard_home, guard, monkeypatch
):
    monkeypatch.setenv("CODEX_CI", "1")
    cwd = _fixture_cwd("fixture", "work tree")
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }

    result = delegate_channel.evaluate_codex_hook(
        guard_home, _payload("developer", cwd), config_loader=lambda _home: conf
    )
    hook = result["hookSpecificOutput"]

    assert result["decision"] == "block"
    assert hook == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": result["reason"],
    }
    assert result == {
        "decision": "block",
        "reason": result["reason"],
        "hookSpecificOutput": hook,
        "systemMessage": result["reason"],
        "suppressOutput": False,
    }
    assert "$pm-dev-delegate" in result["reason"]
    assert "backbone" in result["reason"]
    # 해결 방법의 인터프리터 표기는 실행 플랫폼 해소값이다(Windows 는 런처) — 기대값을 리터럴로
    # 재타이핑하지 않고 가드의 해소 함수에서 얻는다.
    assert (
        f"{delegate_env._prescribed_interpreter()} .project_manager/tools/pm_delegate.py"
        in result["reason"]
    )
    assert "프롬프트를 파일로 저장" in result["reason"]
    assert (
        "--prompt-file "
        ".project_manager/.local/delegate/manual-developer-normal-prompt.md"
    ) in result["reason"]
    assert "<파일>" not in result["reason"]
    assert f"--cwd {_quoted_for_local_shell(cwd)}" in result["reason"]
    assert "<worktree>" not in result["reason"]


def _inject_platform(guard, monkeypatch, platform: str) -> None:
    """다른 플랫폼의 경로/인용 규칙을 이 인터프리터에 주입한다 (Linux 에서 Windows 분기 재현)."""
    rule = PureWindowsPath if platform == "windows" else PurePosixPath
    monkeypatch.setattr(
        delegate_channel, "_is_absolute_path", lambda value: rule(value).is_absolute()
    )
    monkeypatch.setattr(contract, 'running_on_windows', lambda: platform == "windows")


@pytest.mark.parametrize(
    "platform,cwd",
    (
        ("windows", r"C:\fixture\work tree"),
        ("posix", "/fixture/work tree"),
    ),
)
def test_native_cwd_notation_yields_the_full_deny_envelope_on_each_platform(
    guard_home, guard, monkeypatch, platform, cwd
):
    """T-0715 — Windows 호스트가 보내는 `C:\\…` payload 로도 deny 엔벨로프가 온전해야 한다."""
    _inject_platform(guard, monkeypatch, platform)
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }

    result = delegate_channel.evaluate_codex_hook(
        guard_home, _payload("developer", cwd), config_loader=lambda _home: conf
    )

    assert set(result) == set(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS)
    assert set(result["hookSpecificOutput"]) == set(
        delegate_channel_rules.CODEX_DENY_HOOK_OUTPUT_KEYS
    )
    expected = f'--cwd "{cwd}"' if platform == "windows" else f"--cwd '{cwd}'"
    assert expected in result["reason"]
    assert "<worktree>" not in result["reason"]


@pytest.mark.parametrize(
    "platform,foreign_cwd",
    (
        ("windows", "/fixture/worktree"),
        ("posix", r"C:\fixture\worktree"),
    ),
)
def test_foreign_cwd_notation_is_not_a_verdict_input(
    guard_home, guard, monkeypatch, platform, foreign_cwd
):
    """다른 플랫폼 표기의 cwd 는 그 플랫폼에서 절대경로가 아니다. 판정 입력이 아니므로 예외로 올라간다."""
    _inject_platform(guard, monkeypatch, platform)
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }

    with pytest.raises(ValueError, match="cwd 가 절대경로가 아니다"):
        delegate_channel._evaluate_codex_decision(
            guard_home, _payload("developer", foreign_cwd), config_loader=lambda _home: conf)


def test_windows_rule_observation_failure_keeps_every_deny_key(guard_home, guard, monkeypatch):
    """Windows 규칙을 주입한 deny 에서 관측 실패를 주입해도 블록 형태가 그대로다."""
    _inject_platform(guard, monkeypatch, "windows")
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer", r"C:\fixture\worktree")
    decision = delegate_channel._evaluate_codex_decision(guard_home, payload, config_loader=lambda _home: conf)
    envelope = codex_envelope._codex_decision_envelope(decision)

    def broken_append(_home, *_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(codex_observation, '_append_codex_observation', broken_append)
    result = codex_hook.observe_codex_pretooluse(guard_home, payload, envelope, decision=decision)

    assert set(result) == set(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS)
    assert result["reason"] == result["systemMessage"]
    assert "관측 기록 실패" in result["reason"]
    assert r"--cwd C:\fixture\worktree" in result["reason"]


def test_codex_hook_cli_judges_the_agent_type_role_in_both_multi_agent_versions(
    guard_home, guard, monkeypatch, tmp_path
):
    """multi-agent v1·v2 의 실측 spawn 입력 그대로 `codex-hook` 이 `agent_type` 을 역할로 판정한다.

    v2 의 `task_name`(작업 이름표)은 역할이 아니다. 역할이 비면 Codex 는 `default` 역할로 띄우므로 통과다."""
    monkeypatch.setenv("CODEX_CI", "1")
    cross = {"delegate.enabled": "true", "delegate.developer.harness": "opencode",
             "delegate.developer.model": "qwen3-coder"}
    off = {"delegate.enabled": "false"}

    def run(payload, conf, state_dir):
        stdout = io.StringIO()
        assert cli_delegate_channel_guard.main(
            pm_paths.home_at(guard.REPO), ["codex-hook"],
            stdin=io.StringIO(json.dumps(payload)), stdout=stdout,
            config_loader=lambda _home: conf, state_dir=state_dir,
        ) == 0
        return json.loads(stdout.getvalue())

    for tool_set in SPAWN_TOOL_SETS:
        spawn = _live_spawn_payload(tool_set)
        spawn["cwd"] = _fixture_cwd("fixture", "worktree")
        assert spawn["tool_input"]["agent_type"] == "developer", tool_set
        crossed = run(spawn, cross, tmp_path / tool_set / "cross")
        assert set(crossed) == set(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS), tool_set
        assert "conf 는 opencode/qwen3-coder" in crossed["reason"], tool_set
        assert "위임이 꺼져 있습니다" in run(spawn, off, tmp_path / tool_set / "off")["reason"], tool_set
        spawn["tool_input"].pop("agent_type")
        assert run(spawn, off, tmp_path / tool_set / "default") == {}, tool_set
        rows = codex_observation._codex_audit_path(
            guard_home, tmp_path / tool_set / "default").read_text(encoding="utf-8").splitlines()
        assert [json.loads(row)["role"] for row in rows] == [""], tool_set


def test_codex_cross_mapping_cli_emits_exact_measured_json_bytes(
    guard_home, guard, monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEX_CI", "1")
    payload = _payload("developer")
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    expected = delegate_channel.evaluate_codex_hook(guard_home, payload, config_loader=lambda _home: conf)
    stdout = io.StringIO()

    assert cli_delegate_channel_guard.main(pm_paths.home_at(guard.REPO),
        ["codex-hook"],
        stdin=io.StringIO(json.dumps(payload)),
        stdout=stdout,
        config_loader=lambda _home: conf,
        state_dir=tmp_path,
    ) == 0

    expected_bytes = json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    ) + "\n"
    assert stdout.getvalue() == expected_bytes


def test_codex_deny_observation_failure_preserves_exact_block_shape(
    guard_home, guard, monkeypatch
):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer")
    decision = delegate_channel._evaluate_codex_decision(
        guard_home, payload, config_loader=lambda _home: conf
    )
    envelope = codex_envelope._codex_decision_envelope(decision)

    def broken_append(_home, *_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(codex_observation, '_append_codex_observation', broken_append)
    result = codex_hook.observe_codex_pretooluse(
        guard_home, payload, envelope, decision=decision
    )

    reason = result["reason"]
    assert set(result) == {
        "decision", "reason", "hookSpecificOutput", "systemMessage",
        "suppressOutput",
    }
    assert result["decision"] == "block"
    assert result["suppressOutput"] is False
    assert result["systemMessage"] == reason
    assert result["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    assert "관측 기록 실패" in reason


def test_deny_envelope_key_sets_come_from_engine_constants(guard_home, guard):
    """T-0715 — 스키마는 엔진 상수가 단일 진실이다 (테스트 재타이핑 금지)."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    decision = delegate_channel._evaluate_codex_decision(
        guard_home, _payload("developer"), config_loader=lambda _home: conf
    )
    envelope = codex_envelope._codex_decision_envelope(decision)

    assert set(envelope) == set(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS)
    assert set(envelope["hookSpecificOutput"]) == set(
        delegate_channel_rules.CODEX_DENY_HOOK_OUTPUT_KEYS
    )
    assert envelope["suppressOutput"] is delegate_channel_rules.CODEX_DENY_SUPPRESS_OUTPUT
    assert delegate_channel_rules.CODEX_DENY_SUPPRESS_OUTPUT is False
    assert delegate_channel_rules.CODEX_OBSERVATION_SUPPRESS_OUTPUT is True


@pytest.mark.parametrize(
    "missing_key", ("systemMessage", "suppressOutput", "reason", "decision")
)
def test_partial_deny_envelope_is_rejected_before_return(guard_home, guard, missing_key):
    """T-0715 — 필수 키가 빠진 엔벨로프는 호스트에 나가기 전에 fail-loud."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    decision = delegate_channel._evaluate_codex_decision(
        guard_home, _payload("developer"), config_loader=lambda _home: conf
    )
    partial = dict(codex_envelope._codex_decision_envelope(decision))
    partial.pop(missing_key)

    with pytest.raises(ValueError) as exc:
        codex_envelope._validated_codex_envelope(partial)

    assert missing_key in str(exc.value)


def test_partial_hook_specific_output_is_rejected_before_return(guard_home, guard):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    decision = delegate_channel._evaluate_codex_decision(
        guard_home, _payload("developer"), config_loader=lambda _home: conf
    )
    partial = dict(codex_envelope._codex_decision_envelope(decision))
    hook = dict(partial["hookSpecificOutput"])
    hook.pop("permissionDecisionReason")
    partial["hookSpecificOutput"] = hook

    with pytest.raises(ValueError) as exc:
        codex_envelope._validated_codex_envelope(partial)

    assert "permissionDecisionReason" in str(exc.value)


def test_observation_failure_keeps_every_deny_key_from_the_constant(
    guard_home, guard, monkeypatch
):
    """관측 실패를 직접 주입해도 deny 엔벨로프 키가 하나도 빠지지 않는다."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer")
    decision = delegate_channel._evaluate_codex_decision(guard_home, payload, config_loader=lambda _home: conf)
    envelope = codex_envelope._codex_decision_envelope(decision)

    def broken_append(_home, *_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(codex_observation, '_append_codex_observation', broken_append)
    result = codex_hook.observe_codex_pretooluse(guard_home, payload, envelope, decision=decision)

    assert set(result) == set(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS)
    assert set(result["hookSpecificOutput"]) == set(
        delegate_channel_rules.CODEX_DENY_HOOK_OUTPUT_KEYS
    )
    assert "관측 기록 실패" in result["reason"]
    assert result["suppressOutput"] is delegate_channel_rules.CODEX_DENY_SUPPRESS_OUTPUT


def test_observation_failure_on_partial_envelope_fails_loud(guard_home, guard, monkeypatch):
    """부분 엔벨로프가 관측 실패 경로로 들어와도 말없이 수준이 낮아지지 않는다."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer")
    decision = delegate_channel._evaluate_codex_decision(guard_home, payload, config_loader=lambda _home: conf)
    partial = dict(codex_envelope._codex_decision_envelope(decision))
    partial.pop("systemMessage")

    with pytest.raises(ValueError):
        codex_hook._codex_observation_failure(partial, OSError("audit unavailable"))


def test_codex_hook_never_writes_a_partial_envelope_to_the_host(
    guard, monkeypatch, tmp_path
):
    """엔벨로프 조립이 깨지면 예외로 올라가고 호스트에는 아무것도 쓰지 않는다(막는 응답은 디스패처 몫)."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    real_envelope = codex_envelope._codex_decision_envelope

    def partial_envelope(result):
        envelope = dict(real_envelope(result))
        envelope.pop("hookSpecificOutput", None)
        return envelope

    monkeypatch.setattr(codex_envelope, '_codex_decision_envelope', partial_envelope)
    stdout = io.StringIO()

    with pytest.raises(ValueError):
        cli_delegate_channel_guard.main(pm_paths.home_at(guard.REPO),
            ["codex-hook"],
            stdin=io.StringIO(json.dumps(_payload("developer"))),
            stdout=stdout,
            config_loader=lambda _home: conf,
            state_dir=tmp_path,
        )

    assert stdout.getvalue() == ""


def test_codex_native_mapping_allows_with_recorded_reason(guard_home, guard):
    conf = {
        "delegate.enabled": "true",
        "delegate.researcher.harness": "codex",
        "delegate.researcher.model": "gpt-fixture",
    }
    decision = delegate_channel._evaluate_codex_decision(
        guard_home, _payload("researcher"), config_loader=lambda _home: conf
    )
    result = delegate_channel.evaluate_codex_hook(
        guard_home, _payload("researcher"), config_loader=lambda _home: conf
    )

    assert result == {}
    assert "native harness 일치(codex)" in decision["reason"]


@pytest.mark.parametrize("agent_type", ("general", None))
def test_codex_unknown_or_missing_agent_is_quiet_allow_without_config_load(
    guard_home, guard, agent_type
):
    def unexpected_loader():
        raise AssertionError("non-delegation agent must not load local.conf")

    decision = delegate_channel._evaluate_codex_decision(
        guard_home, _payload(agent_type), config_loader=unexpected_loader
    )

    assert codex_envelope._codex_decision_envelope(decision) == {}
    assert "delegate-channel/record" in decision["reason"]
    assert "delegate-channel/warn" not in decision["reason"]


def test_codex_hard_profile_missing_allows_without_normal_fallback_or_warning(guard_home, guard):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "normal-only",
    }
    decision = delegate_channel._evaluate_codex_decision(
        guard_home, _payload("developer-hard"), config_loader=lambda _home: conf
    )

    assert codex_envelope._codex_decision_envelope(decision) == {}
    assert "hard 프로필 미설정" in decision["reason"]
    assert "normal 로 수준 낮춤 금지" in decision["reason"]
    assert "delegate-channel/record" in decision["reason"]
    assert "delegate-channel/warn" not in decision["reason"]
    assert "--tier hard" not in decision["reason"]


def test_codex_hook_input_without_an_absolute_cwd_is_not_a_verdict_input(
    guard_home, guard, tmp_path,
):
    """cwd 가 절대경로가 아닌 훅 입력은 판정을 낼 입력이 아니다. 판정이 거부든 허용이든 예외로 올라간다.

    `codex-hook` CLI 도 그 예외를 그대로 올려(rc≠0) 디스패처가 그 spawn 을 막는다. 거부일 때만 막고
    허용은 통과시키는 갈래를 두지 않는다."""
    confs = {
        "off": {"delegate.enabled": "false"},
        "cross": {"delegate.enabled": "true", "delegate.developer.harness": "claude",
                  "delegate.developer.model": "opus"},
        "native": {"delegate.enabled": "true", "delegate.developer.harness": "codex",
                   "delegate.developer.model": "gpt-6.1-sol"},
    }
    for label, conf in confs.items():
        for cwd in ("relative/path", "", "   ", None):
            payload = _payload("developer")
            if cwd is None:
                payload.pop("cwd")
            else:
                payload["cwd"] = cwd
            with pytest.raises(ValueError, match="cwd 가 절대경로가 아니다"):
                delegate_channel._evaluate_codex_decision(
                    guard_home, payload, config_loader=lambda _home, conf=conf: conf)
            stdout = io.StringIO()
            with pytest.raises(ValueError, match="cwd 가 절대경로가 아니다"):
                cli_delegate_channel_guard.main(
                    pm_paths.home_at(guard.REPO), ["codex-hook"],
                    stdin=io.StringIO(json.dumps(payload)), stdout=stdout,
                    config_loader=lambda _home, conf=conf: conf, state_dir=tmp_path / label,
                )
            assert stdout.getvalue() == "", (label, cwd)


def test_codex_pretooluse_and_subagentstart_wiring_uses_live_payload_fixture(
    guard, dispatcher,
):
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    events = data["hooks"]
    spawn = _live_spawn_payload()
    subagent_call = _live_subagent_tool_payload()
    role_field, _ = _live_role_item()
    correlation_fields = _live_correlation_fields()

    # 서브에이전트 자신의 도구 호출은 그 SubagentStart 와 같은 상관 키를 싣는다(0.147.0 실측).
    subagent_call_start = deepcopy(_live_events()[3])
    assert tuple(subagent_call[field] for field in correlation_fields) == tuple(
        subagent_call_start[field] for field in correlation_fields
    )
    assert subagent_call["agent_type"] == subagent_call_start["agent_type"]
    assert delegate_channel_rules.CODEX_CORRELATION_FIELDS == correlation_fields

    assert "PreToolUse" in events
    assert "SubagentStart" in events
    # PreToolUse native 진입점은 실제 안전 판정 대상만 연다. 서브에이전트 생성 도구는 훅 이름이
    #   `spawn_agent` 로 끝난다(multi-agent v1 `spawn_agent`, v2 `collaborationspawn_agent`).
    groups = events["PreToolUse"]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "^(Bash|apply_patch|.*spawn_agent)$"
    assert re.fullmatch(groups[0]["matcher"], subagent_call["tool_name"])
    feature = _delegate_feature(dispatcher)
    assert feature.event == "PreToolUse"
    assert not re.fullmatch(feature.tool_pattern, subagent_call["tool_name"])
    assert not dispatcher._feature_matches(feature, subagent_call)
    # 스폰 판별은 config 가 아니라 디스패처 registry 가 한다. v1·v2 의 spawn 만 걸리고 wait 은 걸리지 않는다.
    for tool_set in SPAWN_TOOL_SETS:
        tool_spawn, tool_wait = _live_spawn_payload(tool_set), _live_wait_payload(tool_set)
        assert re.fullmatch(groups[0]["matcher"], tool_spawn["tool_name"]), tool_set
        assert not re.fullmatch(groups[0]["matcher"], tool_wait["tool_name"]), tool_set
        assert re.fullmatch(feature.tool_pattern, tool_spawn["tool_name"]), tool_set
        assert not re.fullmatch(feature.tool_pattern, tool_wait["tool_name"]), tool_set
        assert dispatcher._feature_matches(feature, tool_spawn), tool_set
        assert not dispatcher._feature_matches(feature, tool_wait), tool_set
    # 진입점 뒤에서 디스패처가 판정 자식(`codex-hook`)을 직접 부른다(감독 단계 없음).
    assert list(feature.argv) == [
        "{py}", "{tools}/delegate_channel_guard.py", "codex-hook",
    ]
    handlers = groups[0]["hooks"]
    assert len(handlers) == 1 and handlers[0]["type"] == "command"
    # 시간 상한 두 층: 디스패처 예산 < 호스트 훅 timeout.
    assert dispatcher.CODEX_HOOK_DISPATCH_BUDGET_SEC < handlers[0]["timeout"]
    # 시간 상한·엔벨로프 검증은 디스패처와 엔진 소유다 — 어댑터엔 가드 인라인 스크립트가 없다(T-0720).
    for command_key in ("command", "commandWindows"):
        wrapper = handlers[0][command_key]
        assert '"decision":"approve"' not in wrapper
        assert '"permissionDecision":"allow"' not in wrapper
        assert DISPATCHER_REL in wrapper
        assert f"{dispatcher.CODEX_HOOK_DISPATCH_FLAG} PreToolUse" in wrapper
        # 등록이 진입점 하나로 합쳐졌다 — 기능별 커맨드가 config 에 남아 있으면 원 결함 재발이다.
        assert "delegate_channel_guard" not in wrapper
        for payload in inline_script_payloads(wrapper):
            assert "delegate_channel_guard" not in payload
            assert "candidate" not in payload
    # 진입점은 출하 config 의 이벤트 전부에 하나씩 열려 있고 전부 같은 디스패처를 부른다.
    assert tuple(dispatcher.CODEX_HOOK_ENTRYPOINT_EVENTS) == (
        "PreToolUse", "UserPromptSubmit", "PostToolUse",
        "SubagentStart", "PreCompact", "PostCompact")
    assert set(events) == set(dispatcher.CODEX_HOOK_ENTRYPOINT_EVENTS)
    for event in dispatcher.CODEX_HOOK_ENTRYPOINT_EVENTS:
        entry_groups = events[event]
        expected = "^(Bash|apply_patch|.*spawn_agent)$" if event == "PreToolUse" else ".*"
        assert len(entry_groups) == 1 and entry_groups[0]["matcher"] == expected, event
        entry = entry_groups[0]["hooks"][0]
        for command_key in ("command", "commandWindows"):
            assert DISPATCHER_REL in entry[command_key], (event, command_key)
            assert f"{dispatcher.CODEX_HOOK_DISPATCH_FLAG} {event}" in entry[command_key]

    observer_groups = events["SubagentStart"]
    assert len(observer_groups) == 1
    assert re.fullmatch(observer_groups[0]["matcher"], "any-agent-name")
    observer = observer_groups[0]["hooks"]
    assert len(observer) == 1 and observer[0]["type"] == "command"
    # T-0806 — 관측 등록도 진입점 뒤 registry 항목이다(옛 직결 커맨드 값이 그대로 옮겨 왔다).
    for command_key in ("command", "commandWindows"):
        assert "codex-subagent-observe" not in observer[0][command_key], command_key
    observe_feature = _feature_by_id(dispatcher, "delegate-channel-subagent")
    assert observe_feature.event == "SubagentStart"
    # 옛 matcher 는 `.*`(agent_type 값 공간 전수)이었다 — 판별 없음이 그 값의 이관이다.
    assert observe_feature.tool_pattern is None
    assert list(observe_feature.argv) == [
        "{py}", "{tools}/delegate_channel_guard.py", "codex-subagent-observe",
    ]
    assert dispatcher.CODEX_HOOK_DISPATCH_BUDGET_SEC < observer[0]["timeout"]
    assert "Execpolicy" in data["description"] and "argv-only" in data["description"]

    readme = README.read_text(encoding="utf-8")
    for tool_set in SPAWN_TOOL_SETS:
        assert str(_live_spawn_payload(tool_set)["tool_name"]) in readme, tool_set
    for measured in (
        "codex-cli 0.147.0",
        "codex-cli 0.166.0",
        f"tool_input.{role_field}",
        "작업 이름표라 역할로 읽지 않는다",
        'hookSpecificOutput.permissionDecision="deny"',
        "permissionDecisionReason",
        'decision="block"',
        "정상 allow는 빈 객체 `{}`",
        "SubagentStart",
        "관측 전용",
        "(session_id, turn_id, agent_id)",
        ".project_manager/.local/delegate-channel/codex-observations.jsonl",
        "execpolicy는 argv-only",
        "현재 hook 정의의 hash",
        "시작 시 경고",
        "host 측 감지 수단",
        "--dangerously-bypass-hook-trust",
        "PreToolUse` 4건",
        "`error` 2건",
        "`SubagentStart`는 0건",
        "allow-only tee hook",
        "`SubagentStart` 1건",
        "exact deny 5필드·빈 allow·2필드 경고",
        "[hook-dispatch/판정 실패]",
        "Operation not permitted",
        "`PreToolUse=0`, `SubagentStart=0` 각각",
        "공식 문서",
        "0.147.0 host 실동작",
    ):
        assert measured in readme


def test_append_only_scan_finds_miss_and_is_idempotent_without_consumption(
    guard_home, guard, tmp_path
):
    unmatched = _subagent_start()
    result = codex_hook.observe_codex_subagent_start(
        guard_home, unmatched, state_dir=tmp_path
    )

    assert result == {"suppressOutput": True}
    first = codex_observation.scan_codex_observation_misses(guard_home, state_dir=tmp_path)
    second = codex_observation.scan_codex_observation_misses(guard_home, state_dir=tmp_path)
    assert first == second
    correlation_fields = _live_correlation_fields()
    assert [tuple(row[field] for field in correlation_fields) for row in first] == [
        tuple(unmatched[field] for field in correlation_fields)
    ]
    records = [
        json.loads(line)
        for line in codex_observation._codex_audit_path(guard_home, tmp_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["hook_event_name"] for record in records] == ["SubagentStart"]
    assert records[0]["status"] == "observed"
    assert tuple(records[0][field] for field in correlation_fields) == tuple(
        unmatched[field] for field in correlation_fields
    )
    assert records[0]["role"] == ""
    assert "agent_type" not in records[0]
    assert not list(tmp_path.glob("*.pending.*.json"))
    assert not list(tmp_path.glob("*.matched.*.json"))


def test_matching_events_survive_rotation_without_false_positive(
    guard_home, guard, monkeypatch, tmp_path
):
    monkeypatch.setattr(codex_observation, '_CODEX_OBSERVATION_MAX_BYTES', 400)
    monkeypatch.setattr(codex_observation, '_CODEX_OBSERVATION_MAX_FILES', 3)
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-fixture",
    }
    spawn = _payload("developer")
    start = _subagent_start()
    correlation_fields = _live_correlation_fields()
    assert [field for field in correlation_fields if field not in spawn] == [
        correlation_fields[-1]
    ]
    assert spawn["turn_id"] != start["turn_id"]
    stdout = io.StringIO()
    assert cli_delegate_channel_guard.main(pm_paths.home_at(guard.REPO),
        ["codex-hook"],
        stdin=io.StringIO(json.dumps(spawn)),
        stdout=stdout,
        config_loader=lambda _home: conf,
        state_dir=tmp_path,
    ) == 0
    assert json.loads(stdout.getvalue()) == {}

    observed = io.StringIO()
    assert cli_delegate_channel_guard.main(pm_paths.home_at(guard.REPO),
        ["codex-subagent-observe"],
        stdin=io.StringIO(json.dumps(start)),
        stdout=observed,
        state_dir=tmp_path,
    ) == 0
    assert json.loads(observed.getvalue()) == {"suppressOutput": True}
    audit_paths = codex_observation._codex_audit_paths(codex_observation._codex_audit_path(guard_home, tmp_path))
    assert len(audit_paths) == 2
    assert all(path.stat().st_size <= 400 for path in audit_paths)
    assert codex_observation.scan_codex_observation_misses(guard_home, state_dir=tmp_path) == []
    assert codex_observation.scan_codex_observation_misses(guard_home, state_dir=tmp_path) == []
    assert not list(tmp_path.glob("*.pending.*.json"))
    assert not list(tmp_path.glob("*.matched.*.json"))


def test_incomplete_config_warning_uses_quiet_jsonl_record_channel(guard_home, guard, tmp_path):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        # An incomplete atomic profile is a core [warn] allow.
    }
    decision = delegate_channel._evaluate_codex_decision(
        guard_home, _payload("developer"), config_loader=lambda _home: conf
    )
    result = codex_envelope._codex_decision_envelope(decision)
    recorded = codex_hook.observe_codex_pretooluse(
        guard_home, _payload("developer"), result, decision=decision, state_dir=tmp_path
    )

    assert recorded == {}
    audit = codex_observation._codex_audit_path(guard_home, tmp_path).read_text(encoding="utf-8")
    assert "delegate-channel/warn" in audit
    assert '"status":"decision_allow"' in audit


def _pretooluse_handler() -> dict[str, object]:
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    return hooks["PreToolUse"][0]["hooks"][0]


@pytest.mark.parametrize("event", ("SubagentStart",))
@pytest.mark.parametrize("command_key", ("command", "commandWindows"))
def test_wrapper_fallback_envelope_matches_engine_warning_constant(
    guard, event, command_key
):
    """도구 호출을 판정하지 않는 이벤트의 래퍼 대체 출력도 완전한 경고 엔벨로프다 (스키마=엔진 상수).

    PreToolUse 래퍼의 대체 출력은 막는 응답이다(`test_codex_hook_dispatch.py`)."""
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    wrapper = hooks[event][0]["hooks"][0][command_key]
    match = re.search(r'\{"systemMessage".*?\}', wrapper)

    assert match is not None, (event, command_key)
    fallback = json.loads(match.group(0))
    assert set(fallback) == set(delegate_channel_rules.CODEX_WARNING_ENVELOPE_KEYS)
    assert fallback["suppressOutput"] is delegate_channel_rules.CODEX_WARNING_SUPPRESS_OUTPUT
    assert "fail-open" in fallback["systemMessage"]
    # 어느 층이 답했는지 구별된다 — 셸 대체 출력은 어댑터 마커를 단다.
    assert delegate_channel_rules.CODEX_ADAPTER_FALLBACK_MARKER in fallback["systemMessage"]


@pytest.mark.parametrize("event", ("PreToolUse", "SubagentStart"))
def test_windows_guard_hook_resolves_interpreter_by_launcher_probe(event):
    """T-0715 — Windows `python3`는 실행 시 rc 9009로 죽는 가짜 shim일 수 있다.

    존재 확인만으로 인터프리터를 고르면 가드가 실행되지 못하고 deny가 fail-open으로 조용히
    통과한다. 런처 `py`를 먼저 보고, 후보를 실제로 실행해 본 뒤에만 채택해야 한다.
    """
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    windows = hooks[event][0]["hooks"][0]["commandWindows"]

    assert "@('py','python3','python')" in windows
    assert "$probe = & $cand -c" in windows
    assert "if ($probe -eq 'True') { $py = $cand" in windows
    assert "if (Get-Command python3" not in windows
    assert "&&" not in windows


def _run_posix_hook(
    command: str, cwd: Path, timeout: float = 12
) -> tuple[dict, float]:
    started = time.monotonic()
    completed = subprocess.run(
        ["bash", "-c", command],
        cwd=cwd,
        input=json.dumps(_payload("developer")),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    elapsed = time.monotonic() - started
    return json.loads(completed.stdout), elapsed


@pytest.mark.skipif(
    not posix_bash_supported(), reason="POSIX bash wrapper 실행 환경이 아님"
)
def test_posix_hook_wrapper_missing_dispatcher_blocks_the_call(guard, tmp_path):
    """디스패처가 없는 설치에서 PreToolUse 래퍼는 그 호출을 막는 5필드 응답을 낸다."""
    result, elapsed = _run_posix_hook(_pretooluse_handler()["command"], tmp_path)

    assert elapsed < 2
    assert set(result) == set(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result["reason"].startswith("[hook-dispatch/판정 실패] ")


def _adopter_tree(tmp_path: Path, conf_lines: tuple[str, ...]) -> Path:
    """실 엔진 사본 + 디스패처 + local.conf 로 채택자 구성을 만든다 (훅 커맨드를 그대로 태운다).

    훅 진입점이 부르는 건 manifest 등재 디스패처(`.codex/pm_orch_codex.py`)이므로 그 파일이
    채택자 구성의 일부다 — 빼면 이 검사가 가드 대신 어댑터 대체 출력만 재게 된다(T-0777)."""
    tools = tmp_path / ".project_manager" / "tools"
    shutil.copytree(REPO / ".project_manager" / "tools", tools)
    (tmp_path / ".codex").mkdir(parents=True, exist_ok=True)
    shutil.copy2(DISPATCHER_PY, tmp_path / DISPATCHER_REL)
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "\n".join(conf_lines) + "\n", encoding="utf-8", newline="\n"
    )
    return tmp_path


@pytest.mark.parametrize(
    "mapping,expected_keys",
    (
        (("delegate.developer.harness=opencode", "delegate.developer.model=qwen3-coder"),
         "deny"),
        (("delegate.developer.harness=codex", "delegate.developer.model=gpt-fixture"),
         "allow"),
    ),
    ids=("cross-deny", "native-allow"),
)
@pytest.mark.skipif(
    not posix_bash_supported(), reason="POSIX bash wrapper 실행 환경이 아님"
)
def test_posix_hook_wrapper_runs_the_real_guard_and_passes_its_envelope(
    guard, tmp_path, mapping, expected_keys
):
    """래퍼가 **실제로 실행**했는지를 엔진 산출 엔벨로프의 존재로 판정한다 (T-0720 DoD).

    대체 출력은 2필드 경고 + 대체 마커라 정상 경로와 관측치가 다르다.
    """
    root = _adopter_tree(tmp_path, ("delegate.enabled=true", *mapping))

    result, elapsed = _run_posix_hook(_pretooluse_handler()["command"], root)

    assert elapsed < _pretooluse_handler()["timeout"]
    if expected_keys == "deny":
        assert set(result) == set(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS)
        assert "pm_delegate.py" in result["reason"]
    else:
        assert result == {}
    rendered = json.dumps(result, ensure_ascii=False)
    assert delegate_channel_rules.CODEX_ADAPTER_FALLBACK_MARKER not in rendered


@pytest.mark.skipif(
    not posix_bash_supported(), reason="POSIX bash wrapper 실행 환경이 아님"
)
def test_posix_hook_wrapper_blocks_when_the_guard_cannot_answer(
    guard, tmp_path
):
    """엔진 사본이 깨진 채택자에서는 디스패처가 그 위임 호출을 5필드 막는 응답으로 막는다."""
    root = _adopter_tree(tmp_path, ("delegate.enabled=true",))
    (root / ".project_manager" / "tools" / "delegate_channel_guard.py").write_text(
        "raise SystemExit(3)\n", encoding="utf-8", newline="\n"
    )

    result, _elapsed = _run_posix_hook(_pretooluse_handler()["command"], root)

    assert set(result) == set(delegate_channel_rules.CODEX_DENY_ENVELOPE_KEYS)
    assert result["decision"] == "block"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result["reason"].startswith("[hook-dispatch/판정 실패] ")
    assert "delegate-channel 판정 불가" in result["reason"]
    assert delegate_channel_rules.CODEX_ADAPTER_FALLBACK_MARKER not in result["systemMessage"]


@pytest.fixture
def guard_home(tmp_path):
    return pm_paths.home_at(tmp_path / "rendered")
