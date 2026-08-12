"""T-0653 Codex live-payload spawn delegation-channel guard contract."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import time
from copy import deepcopy
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
GUARD_PY = REPO / ".project_manager" / "tools" / "delegate_channel_guard.py"
HOOKS_JSON = REPO / "templates" / "codex" / ".codex" / "hooks.json"
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


def _payload(
    role: str | None, cwd: str = "/fixture/worktree"
) -> dict[str, object]:
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
    conf = {"delegate_enabled": "true"}

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
    cwd = "/fixture/work tree"
    conf = {
        "delegate_enabled": "true",
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
    assert "python3 .project_manager/tools/pm_delegate.py" in result["reason"]
    assert "프롬프트를 파일로 저장" in result["reason"]
    assert (
        "--prompt-file "
        ".project_manager/.local/delegate/manual-developer-normal-prompt.md"
    ) in result["reason"]
    assert "<파일>" not in result["reason"]
    assert "--cwd '/fixture/work tree'" in result["reason"]
    assert "<worktree>" not in result["reason"]


def test_codex_cross_mapping_cli_emits_exact_measured_json_bytes(
    guard, monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEX_CI", "1")
    payload = _payload("developer", "/fixture/worktree")
    conf = {
        "delegate_enabled": "true",
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
        "delegate_enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }
    payload = _payload("developer", "/fixture/worktree")
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


def test_codex_native_mapping_allows_with_recorded_reason(guard):
    conf = {
        "delegate_enabled": "true",
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
        "delegate_enabled": "true",
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
        "delegate_enabled": "true",
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
    guard,
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
    groups = events["PreToolUse"]
    assert len(groups) == 1
    assert re.fullmatch(groups[0]["matcher"], spawn["tool_name"])
    assert not re.fullmatch(groups[0]["matcher"], _live_wait_payload()["tool_name"])
    assert not re.fullmatch(
        groups[0]["matcher"], subagent_call["tool_name"]
    )
    handlers = groups[0]["hooks"]
    assert len(handlers) == 1 and handlers[0]["type"] == "command"
    assert handlers[0]["timeout"] == 10
    assert "delegate_channel_guard.py codex-hook" in handlers[0]["command"]
    assert "delegate_channel_guard.py' codex-hook" in handlers[0]["commandWindows"]
    assert "timeout=8" in handlers[0]["command"]
    assert "timeout=8" in handlers[0]["commandWindows"]
    for command_key in ("command", "commandWindows"):
        wrapper = handlers[0][command_key]
        assert '"decision":"approve"' not in wrapper
        assert '"permissionDecision":"allow"' not in wrapper
        assert "candidate=={}" in wrapper
        assert "set(candidate)==" in wrapper

    observer_groups = events["SubagentStart"]
    assert len(observer_groups) == 1
    assert re.fullmatch(observer_groups[0]["matcher"], "any-agent-name")
    observer = observer_groups[0]["hooks"]
    assert len(observer) == 1 and observer[0]["type"] == "command"
    assert observer[0]["timeout"] == 10
    assert "codex-subagent-observe" in observer[0]["command"]
    assert "codex-subagent-observe" in observer[0]["commandWindows"]
    assert "timeout=8" in observer[0]["command"]
    assert "timeout=8" in observer[0]["commandWindows"]
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
        "delegate_enabled": "true",
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
        "delegate_enabled": "true",
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


def test_posix_hook_wrapper_missing_guard_emits_valid_allow_json(tmp_path):
    result, elapsed = _run_posix_hook(_pretooluse_handler()["command"], tmp_path)

    assert elapsed < 2
    assert set(result) == {"systemMessage", "suppressOutput"}
    assert result["suppressOutput"] is False


@pytest.mark.parametrize(
    "candidate",
    (
        {},
        {
            "decision": "block",
            "reason": "fixture deny",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "fixture deny",
            },
            "systemMessage": "fixture deny",
            "suppressOutput": False,
        },
    ),
    ids=("empty-allow", "measured-deny"),
)
def test_posix_hook_wrapper_preserves_supported_envelope_exactly(
    tmp_path, candidate
):
    guard_path = tmp_path / ".project_manager" / "tools" / "delegate_channel_guard.py"
    guard_path.parent.mkdir(parents=True)
    encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
    guard_path.write_text(f"print({encoded!r})\n", encoding="utf-8")

    result, elapsed = _run_posix_hook(_pretooluse_handler()["command"], tmp_path)

    assert elapsed < 2
    assert result == candidate


def test_posix_hook_wrapper_times_out_guard_before_outer_hook_timeout(tmp_path):
    guard_path = tmp_path / ".project_manager" / "tools" / "delegate_channel_guard.py"
    guard_path.parent.mkdir(parents=True)
    guard_path.write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )

    result, elapsed = _run_posix_hook(_pretooluse_handler()["command"], tmp_path)

    assert 7 <= elapsed < _pretooluse_handler()["timeout"]
    assert set(result) == {"systemMessage", "suppressOutput"}
    assert "timed out" in result["systemMessage"]
