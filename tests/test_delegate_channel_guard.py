"""T-0641 harness-neutral native delegation-channel guard tests."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
GUARD_PY = REPO / ".project_manager" / "tools" / "delegate_channel_guard.py"
ROLES = ("developer", "code-reviewer", "researcher", "architect")


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("delegate_channel_guard", GUARD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(role: str, tool_name: str = "Agent") -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"subagent_type": role},
        "cwd": "/fixture/worktree",
    }


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("mapping", ("claude", "other", "absent"))
def test_four_role_mapping_matrix(guard, role, mapping):
    conf = {"delegate_enabled": "true"}
    if mapping == "claude":
        conf = {
            "delegate_enabled": "true",
            f"delegate.{role}.harness": "claude",
            f"delegate.{role}.model": "opus",
        }
    elif mapping == "other":
        conf = {
            "delegate_enabled": "true",
            f"delegate.{role}.harness": "codex",
            f"delegate.{role}.model": "gpt-5.6",
        }

    result = guard.evaluate_hook(_payload(role), config_loader=lambda: conf)

    if mapping == "other":
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert result is None


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("mapping", ("self", "cross", "absent"))
@pytest.mark.parametrize("enabled", (True, False))
def test_neutral_core_decision_table_matrix(guard, role, mapping, enabled):
    """4 roles × self/cross/unset × opt-in on/off follows rows ②~⑤."""
    conf = {"delegate_enabled": "true" if enabled else "false"}
    if mapping != "absent":
        conf[f"delegate.{role}.harness"] = (
            "opencode" if mapping == "self" else "claude"
        )
        conf[f"delegate.{role}.model"] = "fixture-model"

    result = guard.decide(role, "normal", conf, "opencode")

    expected = "deny" if enabled and mapping == "cross" else "allow"
    assert result["verdict"] == expected
    assert set(result) == {"verdict", "reason", "harness", "model"}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("mapping", ("claude", "opencode", "", None))
@pytest.mark.parametrize("enabled", (True, False))
def test_claude_hook_is_core_specialization_equivalent(guard, role, mapping, enabled):
    """The pre-T-0641 Claude truth table equals decide(..., self_harness='claude')."""
    conf = {"delegate_enabled": "true" if enabled else "false"}
    if mapping is not None:
        conf[f"delegate.{role}.harness"] = mapping
        conf[f"delegate.{role}.model"] = "fixture-model"

    core = guard.decide(role, "normal", conf, "claude")
    hook = guard.evaluate_hook(_payload(role), config_loader=lambda: conf)
    legacy_denied = bool(enabled and mapping and mapping != "claude")

    assert (core["verdict"] == "deny") is legacy_denied
    assert (hook is not None) is legacy_denied


@pytest.mark.parametrize(
    ("agent_name", "harness", "tier", "expected"),
    (
        ("developer", "claude", None, ("developer", "normal")),
        ("code-reviewer", "opencode", None, ("code-reviewer", "normal")),
        ("developer-hard", "codex", None, ("developer", "hard")),
        ("developer", "opencode", "hard", ("developer", "hard")),
        ("researcher", "opencode", "hard", None),
        ("unknown-agent", "opencode", None, None),
    ),
)
def test_agent_name_role_tier_normalization(
    guard, agent_name, harness, tier, expected
):
    assert guard.normalize_agent_name(agent_name, harness, tier) == expected


def test_tier_mapping_matches_engine_base_or_hard_without_inheritance(guard):
    conf = {
        "delegate_enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "normal-model",
        # resolve_delegate does not define this legacy/nonstandard namespace.
        "delegate.developer.normal.harness": "claude",
        "delegate.developer.normal.model": "must-be-ignored",
        "delegate.developer.hard.harness": "claude",
        "delegate.developer.hard.model": "hard-model",
    }
    normal = guard.decide("developer", "normal", conf, "opencode")
    assert normal["verdict"] == "allow"
    assert (normal["harness"], normal["model"]) == (
        "opencode", "normal-model"
    )

    hard = guard.decide("developer", "hard", conf, "opencode")
    assert hard["verdict"] == "deny"
    assert (hard["harness"], hard["model"]) == ("claude", "hard-model")

    del conf["delegate.developer.hard.harness"]
    del conf["delegate.developer.hard.model"]
    missing = guard.decide("developer", "hard", conf, "opencode")
    assert missing["verdict"] == "allow"
    assert (missing["harness"], missing["model"]) == ("", "")
    assert "hard 프로필 미설정" in missing["reason"]
    assert "normal 강등 금지" in missing["reason"]
    assert "--tier hard" not in missing["reason"]
    assert "delegate-channel/record" in missing["reason"]
    assert "delegate-channel/warn" not in missing["reason"]


@pytest.mark.parametrize("tier", ("normal", "hard"))
@pytest.mark.parametrize("configured", (False, True))
@pytest.mark.parametrize("nonstandard_normal_keys", (False, True))
@pytest.mark.parametrize("fallback_configured", (False, True))
def test_mapping_resolution_matches_real_engine_full_boundary_matrix(
    guard, tier, configured, nonstandard_normal_keys, fallback_configured
):
    """tier x 표준 설정 x 비표준 normal 키 x fallback 설정의 16셀을 엔진과 대조한다."""
    conf = {"delegate_enabled": "true"}
    key = "delegate.developer" + (".hard" if tier == "hard" else "")
    if configured:
        conf[f"{key}.harness"] = "opencode"
        conf[f"{key}.model"] = f"{tier}-standard"
    if nonstandard_normal_keys:
        conf["delegate.developer.normal.harness"] = "claude"
        conf["delegate.developer.normal.model"] = "must-be-ignored"
    if fallback_configured:
        conf[f"{key}.fallback.harness"] = "claude"
        conf[f"{key}.fallback.model"] = f"{tier}-fallback-must-be-ignored"

    pm_delegate = guard._load_pm_delegate()
    try:
        harness, model, _reasoning = pm_delegate.resolve_delegate(
            conf, "developer", tier, None, None, None
        )
        expected = (harness, model, "")
    except pm_delegate.DelegateError as exc:
        expected = ("", "", str(exc))

    assert guard._resolved_mapping("developer", tier, conf) == expected

    judgment = guard.decide("developer", tier, conf, "opencode")
    assert judgment["verdict"] == "allow"
    assert (judgment["harness"], judgment["model"]) == expected[:2]
    if configured:
        assert expected[:2] == ("opencode", f"{tier}-standard")
    else:
        assert expected[0] == expected[1] == ""
        assert expected[2]


def test_mapping_resolution_calls_pm_delegate_single_truth(guard, monkeypatch):
    calls = []

    class FakeDelegateError(Exception):
        pass

    def resolve_delegate(conf, role, tier, cli_harness, cli_model, cli_reasoning):
        calls.append(
            (conf, role, tier, cli_harness, cli_model, cli_reasoning)
        )
        return "codex", "fixture-model", "high"

    fake = SimpleNamespace(
        DelegateError=FakeDelegateError,
        resolve_delegate=resolve_delegate,
    )
    monkeypatch.setattr(guard, "_load_pm_delegate", lambda: fake)

    conf = {"delegate.developer.harness": "codex"}
    assert guard._resolved_mapping("developer", "normal", conf) == (
        "codex", "fixture-model", ""
    )
    assert calls == [(conf, "developer", "normal", None, None, None)]


def test_unknown_self_warns_but_unknown_agent_is_quietly_recorded(guard):
    cross = {
        "delegate_enabled": "true",
        "delegate.developer.harness": "claude",
    }
    unknown_self = guard.decide("developer", "normal", cross, "")
    unknown_role = guard.decide("not-an-agent", "normal", cross, "opencode")
    assert unknown_self["verdict"] == unknown_role["verdict"] == "allow"
    assert "self_harness" in unknown_self["reason"] and "warn" in unknown_self["reason"]
    assert "정규화 실패" in unknown_role["reason"]
    assert "delegate-channel/record" in unknown_role["reason"]
    assert "delegate-channel/warn" not in unknown_role["reason"]


@pytest.mark.parametrize(
    "payload",
    (
        _payload("developer", tool_name="Bash"),
        _payload("general-purpose"),
    ),
)
def test_non_agent_or_non_delegate_role_passes_without_loading_conf(guard, payload):
    def unexpected_loader():
        raise AssertionError("irrelevant tool/role must not read local.conf")

    assert guard.evaluate_hook(payload, config_loader=unexpected_loader) is None


def test_config_loader_reuses_pm_delegate_seam(guard, monkeypatch):
    """중앙 로더로 pm_delegate.py 를 (engine-rev 검증 포함) 로드해 local_config seam 을 재사용한다."""
    expected = {"delegate.developer.harness": "claude"}
    calls = []

    def fake_loader(path, expected_filename, **kwargs):
        calls.append(
            (Path(path).name, expected_filename, kwargs.get("verifier") is not None)
        )
        return SimpleNamespace(local_config=lambda: expected)

    monkeypatch.setattr(guard, "_load_module_from_path", fake_loader)
    assert guard.load_local_config() is expected
    assert calls == [("pm_delegate.py", "pm_delegate.py", True)]


def test_config_error_is_one_line_fail_open(guard):
    stdin = io.StringIO(json.dumps(_payload("developer")))
    stdout = io.StringIO()
    stderr = io.StringIO()

    def broken_config():
        raise OSError("broken\nlocal.conf")

    assert guard.main(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        config_loader=broken_config,
    ) == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue().count("\n") == 1
    assert "fail-open" in stderr.getvalue()


def test_malformed_json_is_one_line_fail_open(guard):
    stdout = io.StringIO()
    stderr = io.StringIO()
    # pytest.fail 은 BaseException 계열이라 fail-open except 에 삼켜져 단언이 무력화된다 —
    # sentinel 기록 + 사후 assert 로 load-bearing 하게 판정한다(내부 리뷰 지적).
    loader_calls = []

    def sentinel_loader():
        loader_calls.append(True)
        return {}

    assert guard.main(
        stdin=io.StringIO("{not-json"),
        stdout=stdout,
        stderr=stderr,
        config_loader=sentinel_loader,
    ) == 0
    assert loader_calls == []
    assert stdout.getvalue() == ""
    assert stderr.getvalue().count("\n") == 1
    assert "fail-open" in stderr.getvalue()


def test_deny_json_schema_and_remediation(guard):
    stdout = io.StringIO()
    stderr = io.StringIO()
    conf = {
        "delegate_enabled": "true",
        "delegate.researcher.harness": "opencode",
        "delegate.researcher.model": "qwen3-coder",
    }

    assert guard.main(
        stdin=io.StringIO(json.dumps(_payload("researcher"))),
        stdout=stdout,
        stderr=stderr,
        config_loader=lambda: conf,
    ) == 0
    result = json.loads(stdout.getvalue())
    assert result == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "[delegate-channel/deny] conf 는 opencode/qwen3-coder — "
                "/pm-dev-delegate 스킬로 위임하라 (실행형 처방: 1) 프롬프트를 "
                "파일로 저장한 뒤(경로: "
                "`.project_manager/.local/delegate/manual-researcher-normal-prompt.md`) "
                "2) backbone `python3 .project_manager/tools/pm_delegate.py --role researcher "
                "--prompt-file .project_manager/.local/delegate/manual-researcher-normal-prompt.md "
                "--cwd /fixture/worktree`)"
            ),
        }
    }
    assert stderr.getvalue() == ""


def test_hook_deny_remediation_materializes_payload_cwd(guard, tmp_path):
    payload = _payload("developer")
    payload["cwd"] = str(tmp_path)
    conf = {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt",
    }
    result = guard.evaluate_hook(payload, config_loader=lambda: conf)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert f"--cwd {tmp_path}" in reason
    assert "프롬프트를 파일로 저장" in reason
    assert (
        "--prompt-file "
        ".project_manager/.local/delegate/manual-developer-normal-prompt.md"
    ) in reason
    assert "<파일>" not in reason
    assert "<worktree>" not in reason


@pytest.mark.parametrize(
    "argv",
    (
        ["decide", "--role", "developer", "--harness", "opencode"],
        ["decide", "--bogus"],
        ["unknown-command"],
        ["decide", "--role", "developer", "--harness", "opencode", "--cwd", "relative"],
    ),
)
def test_decide_cli_always_one_json_line_rc0_and_never_reads_stdin(
    guard, argv
):
    class UnreadableInput(io.StringIO):
        def read(self, *args, **kwargs):
            raise AssertionError("decide CLI must not read stdin")

    stdout = io.StringIO()
    assert guard.main(
        argv,
        stdin=UnreadableInput("must-not-be-read"),
        stdout=stdout,
        config_loader=lambda: {},
    ) == 0
    assert stdout.getvalue().count("\n") == 1
    payload = json.loads(stdout.getvalue())
    assert set(payload) == {"verdict", "reason", "harness", "model"}
    assert payload["verdict"] in {"allow", "deny"}


def test_decide_cli_internal_error_is_one_line_allow_reason(guard):
    def broken_config():
        raise RuntimeError("broken\nconfig")

    stdout = io.StringIO()
    assert guard.main(
        ["decide", "--role", "developer", "--harness", "opencode"],
        stdout=stdout,
        config_loader=broken_config,
    ) == 0
    assert stdout.getvalue().count("\n") == 1
    payload = json.loads(stdout.getvalue())
    assert payload["verdict"] == "allow"
    assert "RuntimeError" in payload["reason"] and "fail-open" in payload["reason"]


@pytest.mark.parametrize(
    "argv",
    (
        ["decide", "--bogus"],
        ["decide", "--role", "developer", "--harness", "opencode", "--bogus"],
        ["bogus"],
    ),
)
def test_real_decide_process_usage_errors_are_rc0_one_line_json(argv):
    proc = subprocess.run(
        [sys.executable, str(GUARD_PY), *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.count("\n") == 1
    assert json.loads(proc.stdout)["verdict"] == "allow"


@pytest.mark.parametrize("enabled", (None, "false", "0"))
def test_cross_mapping_passes_when_delegate_optin_is_off(guard, enabled):
    """opt-in OFF 형상은 pm_delegate 가 rc3 로 거부라 deny 처방이 교착 — 통과가 정답."""
    conf = {
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-5.6",
    }
    if enabled is not None:
        conf["delegate_enabled"] = enabled

    assert guard.evaluate_hook(_payload("developer"), config_loader=lambda: conf) is None
