"""T-0633 native Agent delegation-channel guard tests."""

from __future__ import annotations

import importlib.util
import io
import json
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
                "`pm_delegate.py --role researcher` 로 위임하라"
            ),
        }
    }
    assert stderr.getvalue() == ""


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
