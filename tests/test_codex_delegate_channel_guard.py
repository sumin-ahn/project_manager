"""T-0653 Codex live-payload spawn delegation-channel guard contract."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from _win_skip import posix_bash_supported
from _hook_commands import inline_script_payloads


REPO = Path(__file__).resolve().parents[1]
GUARD_PY = REPO / ".project_manager" / "tools" / "delegate_channel_guard.py"
HOOKS_JSON = REPO / "templates" / "codex" / ".codex" / "hooks.json"
DISPATCHER_REL = ".codex/pm_orch_codex.py"
DISPATCHER_PY = REPO / "templates" / "codex" / DISPATCHER_REL
README = REPO / "templates" / "codex" / "README.md"
LIVE_PAYLOADS_FIXTURE = (
    REPO / "tests" / "fixtures" / "codex_0_147_0_live_hook_payloads.json"
)


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
    """디스패처 registry 의 그 기능 1건 (없으면 red — 배선이 사라진 것)."""
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


def _live_spawn_payload() -> dict[str, object]:
    return deepcopy(_live_events()[0])


def _live_wait_payload() -> dict[str, object]:
    return deepcopy(_live_events()[1])


def _live_subagent_tool_payload() -> dict[str, object]:
    return deepcopy(_live_events()[2])


def _live_subagent_start() -> dict[str, object]:
    return deepcopy(_live_events()[3])


def _live_role_item() -> tuple[str, str]:
    tool_input = _live_spawn_payload()["tool_input"]
    assert isinstance(tool_input, dict) and tool_input
    field, value = next(iter(tool_input.items()))
    assert isinstance(field, str) and isinstance(value, str)
    return field, value


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


def test_live_spawn_payload_uses_t0641_decide_cli_seam(guard, monkeypatch):
    evidence = _live_evidence()
    spawn = _live_spawn_payload()
    role_field, role = _live_role_item()
    assert evidence["captured_on_version"] == "codex-cli 0.147.0"
    assert spawn["hook_event_name"] == "PreToolUse"
    assert "agent_type" not in spawn["tool_input"]

    cli_calls = []

    def tracked_cli(argv, *, stdout, config_loader):
        cli_calls.append((argv, config_loader()))
        json.dump(
            guard._result("allow", "[delegate-channel/allow] fixture"),
            stdout,
        )
        stdout.write("\n")
        return 0

    monkeypatch.setattr(guard, "_run_decide_cli", tracked_cli)
    conf = {"delegate.enabled": "true"}

    result = guard.evaluate_codex_hook(
        spawn, config_loader=lambda: conf
    )

    assert guard.CODEX_SPAWN_TOOL_NAME == spawn["tool_name"]
    assert guard.CODEX_ROLE_INPUT_FIELD == role_field
    envelope_evidence = guard._codex_decision_envelope.__doc__ or ""
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
    assert cli_calls == [
        ([
            "decide", "--role", role, "--harness", "codex",
        ], conf)
    ]
    assert result == {}


def test_codex_cross_mapping_denies_with_both_measured_fields_and_real_cwd(
    guard, monkeypatch
):
    monkeypatch.setenv("CODEX_CI", "1")
    cwd = _fixture_cwd("fixture", "work tree")
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }

    result = guard.evaluate_codex_hook(
        _payload("developer", cwd), config_loader=lambda: conf
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
    # 처방의 인터프리터 표기는 실행 플랫폼 해소값이다(Windows 는 런처) — 기대값을 리터럴로
    # 재타이핑하지 않고 가드의 해소 seam 에서 얻는다.
    assert (
        f"{guard._prescribed_interpreter()} .project_manager/tools/pm_delegate.py"
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
        guard, "_is_absolute_path", lambda value: rule(value).is_absolute()
    )
    monkeypatch.setattr(guard, "_running_on_windows", lambda: platform == "windows")


@pytest.mark.parametrize(
    "platform,cwd",
    (
        ("windows", r"C:\fixture\work tree"),
        ("posix", "/fixture/work tree"),
    ),
)
def test_native_cwd_notation_yields_the_full_deny_envelope_on_each_platform(
    guard, monkeypatch, platform, cwd
):
    """T-0715 — Windows 호스트가 보내는 `C:\\…` payload 로도 deny 엔벨로프가 온전해야 한다."""
    _inject_platform(guard, monkeypatch, platform)
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }

    result = guard.evaluate_codex_hook(
        _payload("developer", cwd), config_loader=lambda: conf
    )

    assert set(result) == set(guard.CODEX_DENY_ENVELOPE_KEYS)
    assert set(result["hookSpecificOutput"]) == set(
        guard.CODEX_DENY_HOOK_OUTPUT_KEYS
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
def test_foreign_cwd_notation_takes_the_measured_fail_open_allow_shape(
    guard, monkeypatch, platform, foreign_cwd
):
    """다른 플랫폼 표기의 cwd 는 절대경로가 아니다 — 빈 allow 는 부분 엔벨로프가 아니라 측정된 형태다.

    소비자가 `hookSpecificOutput`/`reason` 에서 KeyError 를 만나는 이유가 검증 우회가 아니라
    이 앞단 분기임을 고정한다 (실측 판별 근거).
    """
    _inject_platform(guard, monkeypatch, platform)
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer", foreign_cwd)

    decision = guard._evaluate_codex_decision(payload, config_loader=lambda: conf)
    envelope = guard.evaluate_codex_hook(payload, config_loader=lambda: conf)

    assert decision["verdict"] == "allow"
    assert "cwd 절대경로 누락" in decision["reason"]
    assert envelope == {}
    assert guard._validated_codex_envelope(envelope) == {}


def test_windows_rule_observation_failure_keeps_every_deny_key(guard, monkeypatch):
    """Windows 규칙을 주입한 deny 에서 관측 실패를 주입해도 블록 형태가 그대로다."""
    _inject_platform(guard, monkeypatch, "windows")
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer", r"C:\fixture\worktree")
    decision = guard._evaluate_codex_decision(payload, config_loader=lambda: conf)
    envelope = guard._codex_decision_envelope(decision)

    def broken_append(*_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(guard, "_append_codex_observation", broken_append)
    result = guard.observe_codex_pretooluse(payload, envelope, decision=decision)

    assert set(result) == set(guard.CODEX_DENY_ENVELOPE_KEYS)
    assert result["reason"] == result["systemMessage"]
    assert "관측 기록 실패" in result["reason"]
    assert r"--cwd C:\fixture\worktree" in result["reason"]


def test_codex_cross_mapping_cli_emits_exact_measured_json_bytes(
    guard, monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEX_CI", "1")
    payload = _payload("developer")
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    expected = guard.evaluate_codex_hook(payload, config_loader=lambda: conf)
    stdout = io.StringIO()

    assert guard.main(
        ["codex-hook"],
        stdin=io.StringIO(json.dumps(payload)),
        stdout=stdout,
        config_loader=lambda: conf,
        state_dir=tmp_path,
    ) == 0

    expected_bytes = json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    ) + "\n"
    assert stdout.getvalue() == expected_bytes


def test_codex_deny_observation_failure_preserves_exact_block_shape(
    guard, monkeypatch
):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer")
    decision = guard._evaluate_codex_decision(
        payload, config_loader=lambda: conf
    )
    envelope = guard._codex_decision_envelope(decision)

    def broken_append(*_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(guard, "_append_codex_observation", broken_append)
    result = guard.observe_codex_pretooluse(
        payload, envelope, decision=decision
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


def test_deny_envelope_key_sets_come_from_engine_constants(guard):
    """T-0715 — 스키마는 엔진 상수가 단일 진실이다 (테스트 재타이핑 금지)."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    decision = guard._evaluate_codex_decision(
        _payload("developer"), config_loader=lambda: conf
    )
    envelope = guard._codex_decision_envelope(decision)

    assert set(envelope) == set(guard.CODEX_DENY_ENVELOPE_KEYS)
    assert set(envelope["hookSpecificOutput"]) == set(
        guard.CODEX_DENY_HOOK_OUTPUT_KEYS
    )
    assert envelope["suppressOutput"] is guard.CODEX_DENY_SUPPRESS_OUTPUT
    assert guard.CODEX_DENY_SUPPRESS_OUTPUT is False
    assert guard.CODEX_OBSERVATION_SUPPRESS_OUTPUT is True


@pytest.mark.parametrize(
    "missing_key", ("systemMessage", "suppressOutput", "reason", "decision")
)
def test_partial_deny_envelope_is_rejected_before_return(guard, missing_key):
    """T-0715 — 필수 키가 빠진 엔벨로프는 호스트에 나가기 전에 fail-loud."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    decision = guard._evaluate_codex_decision(
        _payload("developer"), config_loader=lambda: conf
    )
    partial = dict(guard._codex_decision_envelope(decision))
    partial.pop(missing_key)

    with pytest.raises(ValueError) as exc:
        guard._validated_codex_envelope(partial)

    assert missing_key in str(exc.value)


def test_partial_hook_specific_output_is_rejected_before_return(guard):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    decision = guard._evaluate_codex_decision(
        _payload("developer"), config_loader=lambda: conf
    )
    partial = dict(guard._codex_decision_envelope(decision))
    hook = dict(partial["hookSpecificOutput"])
    hook.pop("permissionDecisionReason")
    partial["hookSpecificOutput"] = hook

    with pytest.raises(ValueError) as exc:
        guard._validated_codex_envelope(partial)

    assert "permissionDecisionReason" in str(exc.value)


def test_observation_failure_keeps_every_deny_key_from_the_constant(
    guard, monkeypatch
):
    """관측 실패를 직접 주입해도 deny 엔벨로프 키가 하나도 빠지지 않는다."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer")
    decision = guard._evaluate_codex_decision(payload, config_loader=lambda: conf)
    envelope = guard._codex_decision_envelope(decision)

    def broken_append(*_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(guard, "_append_codex_observation", broken_append)
    result = guard.observe_codex_pretooluse(payload, envelope, decision=decision)

    assert set(result) == set(guard.CODEX_DENY_ENVELOPE_KEYS)
    assert set(result["hookSpecificOutput"]) == set(
        guard.CODEX_DENY_HOOK_OUTPUT_KEYS
    )
    assert "관측 기록 실패" in result["reason"]
    assert result["suppressOutput"] is guard.CODEX_DENY_SUPPRESS_OUTPUT


def test_observation_failure_on_partial_envelope_fails_loud(guard, monkeypatch):
    """부분 엔벨로프가 관측 실패 경로로 들어와도 조용히 강등되지 않는다."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer")
    decision = guard._evaluate_codex_decision(payload, config_loader=lambda: conf)
    partial = dict(guard._codex_decision_envelope(decision))
    partial.pop("systemMessage")

    with pytest.raises(ValueError):
        guard._codex_observation_failure(partial, OSError("audit unavailable"))


def test_codex_hook_never_writes_a_partial_envelope_to_the_host(
    guard, monkeypatch, tmp_path
):
    """엔벨로프 조립이 깨져도 호스트에는 사유를 담은 완전한 엔벨로프만 나간다."""
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    real_envelope = guard._codex_decision_envelope

    def partial_envelope(result):
        envelope = dict(real_envelope(result))
        envelope.pop("hookSpecificOutput", None)
        return envelope

    monkeypatch.setattr(guard, "_codex_decision_envelope", partial_envelope)
    stdout = io.StringIO()
    stderr = io.StringIO()

    rc = guard.main(
        ["codex-hook"],
        stdin=io.StringIO(json.dumps(_payload("developer"))),
        stdout=stdout,
        stderr=stderr,
        config_loader=lambda: conf,
        state_dir=tmp_path,
    )
    emitted = json.loads(stdout.getvalue())

    assert rc == 0
    assert set(emitted) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert "delegate-channel/warn" in emitted["systemMessage"]
    assert emitted["suppressOutput"] is False
    assert "fail-open" in stderr.getvalue()


def test_codex_native_mapping_allows_with_recorded_reason(guard):
    conf = {
        "delegate.enabled": "true",
        "delegate.researcher.harness": "codex",
        "delegate.researcher.model": "gpt-fixture",
    }
    decision = guard._evaluate_codex_decision(
        _payload("researcher"), config_loader=lambda: conf
    )
    result = guard.evaluate_codex_hook(
        _payload("researcher"), config_loader=lambda: conf
    )

    assert result == {}
    assert "native harness 일치(codex)" in decision["reason"]


@pytest.mark.parametrize("agent_type", ("general", None))
def test_codex_unknown_or_missing_agent_is_quiet_allow_without_config_load(
    guard, agent_type
):
    def unexpected_loader():
        raise AssertionError("non-delegation agent must not load local.conf")

    decision = guard._evaluate_codex_decision(
        _payload(agent_type), config_loader=unexpected_loader
    )

    assert guard._codex_decision_envelope(decision) == {}
    assert "delegate-channel/record" in decision["reason"]
    assert "delegate-channel/warn" not in decision["reason"]


def test_codex_hard_profile_missing_allows_without_normal_fallback_or_warning(guard):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "normal-only",
    }
    decision = guard._evaluate_codex_decision(
        _payload("developer-hard"), config_loader=lambda: conf
    )

    assert guard._codex_decision_envelope(decision) == {}
    assert "hard 프로필 미설정" in decision["reason"]
    assert "normal 강등 금지" in decision["reason"]
    assert "delegate-channel/record" in decision["reason"]
    assert "delegate-channel/warn" not in decision["reason"]
    assert "--tier hard" not in decision["reason"]


def test_codex_missing_absolute_cwd_fails_open_instead_of_emitting_placeholder_deny(
    guard,
):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    decision = guard._evaluate_codex_decision(
        _payload("developer", "relative/path"), config_loader=lambda: conf
    )

    assert guard._codex_decision_envelope(decision) == {}
    assert "delegate-channel/warn" in decision["reason"]
    assert "cwd 절대경로 누락" in decision["reason"]
    assert "<worktree>" not in decision["reason"]


def test_codex_hook_cli_infrastructure_failure_is_rc0_allow_and_user_warning(guard):
    stdout = io.StringIO()
    stderr = io.StringIO()

    def broken_config():
        raise OSError("broken\nlocal.conf")

    rc = guard.main(
        ["codex-hook"],
        stdin=io.StringIO(json.dumps(_payload("developer"))),
        stdout=stdout,
        stderr=stderr,
        config_loader=broken_config,
    )
    result = json.loads(stdout.getvalue())

    assert rc == 0
    assert stdout.getvalue().count("\n") == 1
    assert set(result) == {"systemMessage", "suppressOutput"}
    assert "delegate-channel/warn" in result["systemMessage"]
    assert result["suppressOutput"] is False
    assert stderr.getvalue().count("\n") == 1
    assert "fail-open" in stderr.getvalue()


def test_codex_pretooluse_and_subagentstart_wiring_uses_live_payload_fixture(
    guard, dispatcher,
):
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    events = data["hooks"]
    spawn = _live_spawn_payload()
    subagent_call = _live_subagent_tool_payload()
    subagent_start = _live_subagent_start()
    role_field, _ = _live_role_item()
    correlation_fields = _live_correlation_fields()

    assert tuple(subagent_call[field] for field in correlation_fields) == tuple(
        subagent_start[field] for field in correlation_fields
    )
    assert subagent_call["agent_type"] == subagent_start["agent_type"]
    assert guard.CODEX_CORRELATION_FIELDS == correlation_fields

    assert "PreToolUse" in events
    assert "SubagentStart" in events
    # T-0872 — PreToolUse native 진입점은 실제 안전 판정 대상 둘만 연다.
    groups = events["PreToolUse"]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "^(Bash|collaborationspawn_agent)$"
    assert re.fullmatch(groups[0]["matcher"], spawn["tool_name"])
    assert re.fullmatch(groups[0]["matcher"], subagent_call["tool_name"])
    assert not re.fullmatch(groups[0]["matcher"], _live_wait_payload()["tool_name"])
    # 스폰 판별은 config 가 아니라 디스패처 registry 가 한다 — 값은 그대로 옮겨 왔다.
    feature = _delegate_feature(dispatcher)
    assert feature.event == "PreToolUse"
    assert re.fullmatch(feature.tool_pattern, spawn["tool_name"])
    assert not re.fullmatch(feature.tool_pattern, _live_wait_payload()["tool_name"])
    assert not re.fullmatch(feature.tool_pattern, subagent_call["tool_name"])
    assert dispatcher._feature_matches(feature, spawn)
    assert not dispatcher._feature_matches(feature, _live_wait_payload())
    assert not dispatcher._feature_matches(feature, subagent_call)
    # 진입점 뒤에서 실행되는 커맨드는 옛 배선과 같은 감독자 호출이다(동작 보존).
    assert list(feature.argv) == [
        "{py}", "{tools}/delegate_channel_guard.py", "supervise", "PreToolUse",
        "{py}", "{tools}/delegate_channel_guard.py", "codex-hook",
    ]
    handlers = groups[0]["hooks"]
    assert len(handlers) == 1 and handlers[0]["type"] == "command"
    # 시간 상한 3층: 엔진 감독자 < 디스패처 예산 < 호스트 훅 timeout.
    assert (guard.CODEX_SUPERVISOR_TIMEOUT_SECONDS
            < dispatcher.CODEX_HOOK_DISPATCH_BUDGET_SEC < handlers[0]["timeout"])
    # 감독(시간 상한·엔벨로프 검증)은 엔진 소유다 — 어댑터엔 가드 인라인 스크립트가 없다(T-0720).
    for command_key in ("command", "commandWindows"):
        wrapper = handlers[0][command_key]
        assert '"decision":"approve"' not in wrapper
        assert '"permissionDecision":"allow"' not in wrapper
        assert DISPATCHER_REL in wrapper
        assert f"{dispatcher.CODEX_HOOK_DISPATCH_FLAG} PreToolUse" in wrapper
        # 배선이 진입점 하나로 접혔다 — 기능별 커맨드가 config 에 남아 있으면 원 결함 재발이다.
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
        expected = "^(Bash|collaborationspawn_agent)$" if event == "PreToolUse" else ".*"
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
    # T-0806 — 관측 배선도 진입점 뒤 registry 항목이다(옛 직결 커맨드 값이 그대로 옮겨 왔다).
    for command_key in ("command", "commandWindows"):
        assert "codex-subagent-observe" not in observer[0][command_key], command_key
    observe_feature = _feature_by_id(dispatcher, "delegate-channel-subagent")
    assert observe_feature.event == "SubagentStart"
    # 옛 matcher 는 `.*`(agent_type 값 공간 전수)이었다 — 판별 없음이 그 값의 이관이다.
    assert observe_feature.tool_pattern is None
    assert list(observe_feature.argv) == [
        "{py}", "{tools}/delegate_channel_guard.py", "supervise", "SubagentStart",
        "{py}", "{tools}/delegate_channel_guard.py", "codex-subagent-observe",
    ]
    assert (guard.CODEX_SUPERVISOR_TIMEOUT_SECONDS
            < dispatcher.CODEX_HOOK_DISPATCH_BUDGET_SEC < observer[0]["timeout"])
    assert "Execpolicy" in data["description"] and "argv-only" in data["description"]

    readme = README.read_text(encoding="utf-8")
    for measured in (
        "codex-cli 0.147.0",
        str(spawn["tool_name"]),
        f"tool_input.{role_field}",
        "spawn payload에는 `tool_input.agent_type`이 없다",
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
        "host 측 감지 표면",
        "--dangerously-bypass-hook-trust",
        "PreToolUse` 4건",
        "`error` 2건",
        "`SubagentStart`는 0건",
        "allow-only tee hook",
        "`SubagentStart` 1건",
        "exact deny 5필드·빈 allow·2필드 fail-open 경고",
        "Operation not permitted",
        "`PreToolUse=0`, `SubagentStart=0` 각각",
        "공식 문서",
        "0.147.0 host 실동작",
    ):
        assert measured in readme


@pytest.mark.parametrize(
    "payload_factory", (_live_wait_payload, _live_subagent_tool_payload)
)
def test_live_non_spawn_pretooluse_events_are_not_intercepted(
    guard, payload_factory
):
    payload = payload_factory()

    def unexpected_loader():
        raise AssertionError("live non-spawn event must not load config")

    assert payload["hook_event_name"] == "PreToolUse"
    assert guard.evaluate_codex_hook(
        payload, config_loader=unexpected_loader
    ) is None


def test_append_only_scan_finds_miss_and_is_idempotent_without_consumption(
    guard, tmp_path
):
    unmatched = _subagent_start()
    result = guard.observe_codex_subagent_start(
        unmatched, state_dir=tmp_path
    )

    assert result == {"suppressOutput": True}
    first = guard.scan_codex_observation_misses(state_dir=tmp_path)
    second = guard.scan_codex_observation_misses(state_dir=tmp_path)
    assert first == second
    correlation_fields = _live_correlation_fields()
    assert [tuple(row[field] for field in correlation_fields) for row in first] == [
        tuple(unmatched[field] for field in correlation_fields)
    ]
    records = [
        json.loads(line)
        for line in guard._codex_audit_path(tmp_path)
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
    guard, monkeypatch, tmp_path
):
    monkeypatch.setattr(guard, "_CODEX_OBSERVATION_MAX_BYTES", 400)
    monkeypatch.setattr(guard, "_CODEX_OBSERVATION_MAX_FILES", 3)
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
    assert guard.main(
        ["codex-hook"],
        stdin=io.StringIO(json.dumps(spawn)),
        stdout=stdout,
        config_loader=lambda: conf,
        state_dir=tmp_path,
    ) == 0
    assert json.loads(stdout.getvalue()) == {}

    observed = io.StringIO()
    assert guard.main(
        ["codex-subagent-observe"],
        stdin=io.StringIO(json.dumps(start)),
        stdout=observed,
        state_dir=tmp_path,
    ) == 0
    assert json.loads(observed.getvalue()) == {"suppressOutput": True}
    audit_paths = guard._codex_audit_paths(guard._codex_audit_path(tmp_path))
    assert len(audit_paths) == 2
    assert all(path.stat().st_size <= 400 for path in audit_paths)
    assert guard.scan_codex_observation_misses(state_dir=tmp_path) == []
    assert guard.scan_codex_observation_misses(state_dir=tmp_path) == []
    assert not list(tmp_path.glob("*.pending.*.json"))
    assert not list(tmp_path.glob("*.matched.*.json"))


def test_incomplete_config_warning_uses_quiet_jsonl_record_channel(guard, tmp_path):
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "opencode",
        # An incomplete atomic profile is a core [warn] allow.
    }
    decision = guard._evaluate_codex_decision(
        _payload("developer"), config_loader=lambda: conf
    )
    result = guard._codex_decision_envelope(decision)
    recorded = guard.observe_codex_pretooluse(
        _payload("developer"), result, decision=decision, state_dir=tmp_path
    )

    assert recorded == {}
    audit = guard._codex_audit_path(tmp_path).read_text(encoding="utf-8")
    assert "delegate-channel/warn" in audit
    assert '"status":"decision_allow"' in audit


def _pretooluse_handler() -> dict[str, object]:
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    return hooks["PreToolUse"][0]["hooks"][0]


@pytest.mark.parametrize("event", ("PreToolUse", "SubagentStart"))
@pytest.mark.parametrize("command_key", ("command", "commandWindows"))
def test_wrapper_fallback_envelope_matches_engine_warning_constant(
    guard, event, command_key
):
    """훅이 엔진을 못 돌릴 때 내는 폴백도 완전한 엔벨로프여야 한다 (스키마=엔진 상수)."""
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    wrapper = hooks[event][0]["hooks"][0][command_key]
    match = re.search(r'\{"systemMessage".*?\}', wrapper)

    assert match is not None, (event, command_key)
    fallback = json.loads(match.group(0))
    assert set(fallback) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert fallback["suppressOutput"] is guard.CODEX_WARNING_SUPPRESS_OUTPUT
    assert "fail-open" in fallback["systemMessage"]
    # 어느 층이 답했는지 구별된다 — 셸 폴백과 supervisor 폴백은 다른 마커를 단다.
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in fallback["systemMessage"]
    assert guard.CODEX_SUPERVISOR_FALLBACK_MARKER not in fallback["systemMessage"]
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER != guard.CODEX_SUPERVISOR_FALLBACK_MARKER


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
def test_posix_hook_wrapper_missing_guard_emits_valid_allow_json(guard, tmp_path):
    result, elapsed = _run_posix_hook(_pretooluse_handler()["command"], tmp_path)

    assert elapsed < 2
    assert set(result) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert result["suppressOutput"] is False
    # 폴백으로 답한 사실이 출력에 남는다 — 정상 통과와 구별되지 않으면 결함이 다시 숨는다.
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in result["systemMessage"]


def _adopter_tree(tmp_path: Path, conf_lines: tuple[str, ...]) -> Path:
    """실 엔진 사본 + 디스패처 + local.conf 로 채택자 형상을 만든다 (훅 커맨드를 그대로 태운다).

    훅 진입점이 부르는 건 manifest 등재 디스패처(`.codex/pm_orch_codex.py`)이므로 그 파일이
    채택자 형상의 일부다 — 빼면 이 게이트가 가드 대신 어댑터 폴백만 재게 된다(T-0777)."""
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
    """래퍼가 **실제로 발화**했는지를 엔진 산출 엔벨로프의 존재로 판정한다 (T-0720 DoD).

    폴백은 2필드 경고 + 폴백 마커라 정상 경로와 관측치가 다르다.
    """
    root = _adopter_tree(tmp_path, ("delegate.enabled=true", *mapping))

    result, elapsed = _run_posix_hook(_pretooluse_handler()["command"], root)

    assert elapsed < _pretooluse_handler()["timeout"]
    if expected_keys == "deny":
        assert set(result) == set(guard.CODEX_DENY_ENVELOPE_KEYS)
        assert "pm_delegate.py" in result["reason"]
    else:
        assert result == {}
    rendered = json.dumps(result, ensure_ascii=False)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER not in rendered
    assert guard.CODEX_SUPERVISOR_FALLBACK_MARKER not in rendered


@pytest.mark.skipif(
    not posix_bash_supported(), reason="POSIX bash wrapper 실행 환경이 아님"
)
def test_posix_hook_wrapper_marks_the_fallback_when_the_guard_cannot_answer(
    guard, tmp_path
):
    """엔진 사본이 깨진 채택자에서도 완전한 엔벨로프가 나가고, 폴백 사실이 남는다."""
    root = _adopter_tree(tmp_path, ("delegate.enabled=true",))
    (root / ".project_manager" / "tools" / "delegate_channel_guard.py").write_text(
        "raise SystemExit(3)\n", encoding="utf-8", newline="\n"
    )

    result, _elapsed = _run_posix_hook(_pretooluse_handler()["command"], root)

    assert set(result) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in result["systemMessage"]


def test_supervisor_bounds_a_hung_guard_before_the_outer_hook_timeout(guard):
    """시간 상한은 엔진 감독자가 소유한다 — 훅 timeout 안에서 완전한 엔벨로프로 끝난다."""
    started = time.monotonic()
    result = guard.supervise_codex_hook(
        "PreToolUse",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        payload_bytes=json.dumps(_payload("developer")).encode("utf-8"),
    )
    elapsed = time.monotonic() - started

    assert guard.CODEX_SUPERVISOR_TIMEOUT_SECONDS <= elapsed
    assert elapsed < _pretooluse_handler()["timeout"]
    assert set(result) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_SUPERVISOR_FALLBACK_MARKER in result["systemMessage"]
    assert "TimeoutExpired" in result["systemMessage"]


@pytest.mark.parametrize(
    "candidate",
    (
        "{}",
        json.dumps({
            "decision": "block",
            "reason": "fixture deny",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "fixture deny",
            },
            "systemMessage": "fixture deny",
            "suppressOutput": False,
        }),
    ),
    ids=("empty-allow", "measured-deny"),
)
def test_supervisor_passes_supported_envelopes_through_unchanged(guard, candidate):
    result = guard.supervise_codex_hook(
        "PreToolUse",
        [sys.executable, "-c", f"print({candidate!r})"],
        payload_bytes=b"{}",
    )

    assert result == json.loads(candidate)


@pytest.mark.parametrize(
    "child_body,marker",
    (
        ("print('not json')", "JSONDecodeError"),
        ("print('[]')", "JSON 객체가 아님"),
        ("print('{\"decision\": \"block\"}')", "필드 불일치"),
        ("import sys; sys.exit(3)", "rc=3"),
    ),
    ids=("garbage", "not-object", "partial-deny", "nonzero-rc"),
)
def test_supervisor_replaces_unusable_output_with_a_marked_fallback(
    guard, child_body, marker
):
    """부분·손상 엔벨로프는 호스트에 닿지 않고, 폴백이라는 사실이 사유에 남는다."""
    result = guard.supervise_codex_hook(
        "PreToolUse",
        [sys.executable, "-c", child_body],
        payload_bytes=b"{}",
    )

    assert set(result) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_SUPERVISOR_FALLBACK_MARKER in result["systemMessage"]
    assert marker in result["systemMessage"]
    assert "fail-open" in result["systemMessage"]


def test_supervisor_fallback_leaves_a_durable_observation_row(guard, tmp_path):
    """폴백은 turn 과 함께 사라지지 않는다 — 장부 행이 남아 lint 스캔이 볼 수 있다."""
    spawn = _payload("developer")
    result = guard.supervise_codex_hook(
        "PreToolUse",
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        payload_bytes=json.dumps(spawn).encode("utf-8"),
        state_dir=tmp_path,
    )
    rows = [
        json.loads(line)
        for line in guard._codex_audit_path(tmp_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert guard.CODEX_SUPERVISOR_FALLBACK_MARKER in result["systemMessage"]
    assert [row["status"] for row in rows] == [
        guard.CODEX_SUPERVISOR_FALLBACK_STATUS
    ]
    assert rows[0]["hook_event_name"] == "PreToolUse"
    assert rows[0]["session_id"] == spawn["session_id"]
    # 판정 없이 통과한 spawn 은 allow 로 세지 않으므로 뒤따르는 start 가 miss 로 보인다.
    guard.observe_codex_subagent_start(_subagent_start(), state_dir=tmp_path)
    assert len(guard.scan_codex_observation_misses(state_dir=tmp_path)) == 1


def test_supervisor_cli_emits_one_json_line_and_rc0(guard, tmp_path):
    stdout = io.StringIO()

    rc = guard.main(
        ["supervise", "SubagentStart", sys.executable, "-c", "import sys; sys.exit(4)"],
        stdin=io.StringIO(json.dumps(_subagent_start())),
        stdout=stdout,
        state_dir=tmp_path,
    )
    emitted = json.loads(stdout.getvalue())

    assert rc == 0
    assert stdout.getvalue().count("\n") == 1
    # 콘솔 코드페이지 무관하게 실려야 하므로 ASCII 이스케이프로 나간다.
    assert stdout.getvalue().isascii()
    assert set(emitted) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert "SubagentStart" in emitted["systemMessage"]
    assert guard.CODEX_SUPERVISOR_FALLBACK_MARKER in emitted["systemMessage"]


def test_supervisor_cli_without_a_child_command_is_still_a_full_envelope(
    guard, tmp_path
):
    stdout = io.StringIO()

    rc = guard.main(
        ["supervise", "PreToolUse"],
        stdin=io.StringIO("{}"),
        stdout=stdout,
        state_dir=tmp_path,
    )
    emitted = json.loads(stdout.getvalue())

    assert rc == 0
    assert set(emitted) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_SUPERVISOR_FALLBACK_MARKER in emitted["systemMessage"]
