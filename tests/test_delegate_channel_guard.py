"""T-0641 harness-neutral native delegation-channel guard tests."""

from __future__ import annotations

import importlib.util
import io
import json
import stat
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
    """Claude specializes the core verdict/warning without changing its decision."""
    conf = {"delegate_enabled": "true" if enabled else "false"}
    if mapping is not None:
        conf[f"delegate.{role}.harness"] = mapping
        conf[f"delegate.{role}.model"] = (
            "opus" if mapping == "claude" else "fixture-model"
        )

    core = guard.decide(role, "normal", conf, "claude")
    hook = guard.evaluate_hook(_payload(role), config_loader=lambda: conf)
    if core["verdict"] == "deny":
        assert hook["hookSpecificOutput"]["permissionDecision"] == "deny"
    elif core["reason"].startswith("[delegate-channel/warn]"):
        assert set(hook["hookSpecificOutput"]) == {
            "hookEventName", "additionalContext",
        }
    else:
        assert hook is None


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


def test_missing_pyyaml_keeps_module_cli_and_hook_nonblocking(tmp_path):
    """MF4: a hook interpreter without PyYAML degrades only model inspection."""
    (tmp_path / "yaml.py").write_text(
        'raise ModuleNotFoundError("simulated missing PyYAML")\n', encoding="utf-8"
    )
    script = r'''
import importlib.util
import io
import json
import sys

guard_path, blocker_dir = sys.argv[1:]
sys.path.insert(0, blocker_dir)
spec = importlib.util.spec_from_file_location("guard_without_yaml", guard_path)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
conf = {
    "delegate.developer.harness": "claude",
    "delegate.developer.model": "opus",
}
stdout = io.StringIO()
rc = guard.main(
    ["decide", "--role", "developer", "--harness", "claude"],
    stdout=stdout,
    config_loader=lambda: conf,
)
decision = json.loads(stdout.getvalue())
hook = guard.evaluate_hook(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "developer"},
    },
    config_loader=lambda: conf,
)
print(json.dumps({"rc": rc, "decision": decision, "hook": hook}))
'''
    proc = subprocess.run(
        [sys.executable, "-c", script, str(GUARD_PY), str(tmp_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["rc"] == 0
    assert set(result["decision"]) == {"verdict", "reason", "harness", "model"}
    assert result["decision"]["verdict"] == "allow"
    assert "simulated missing PyYAML" in result["decision"]["reason"]
    output = result["hook"]["hookSpecificOutput"]
    assert set(output) == {"hookEventName", "additionalContext"}
    assert "permissionDecision" not in output


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


def test_deny_remediation_uses_windows_quoting_when_running_on_windows(
    guard, monkeypatch
):
    """T-0714 — 처방은 실행 셸 인용 규칙을 따른다 (Windows 렌더러 직접 주입).

    POSIX 단일따옴표를 Windows 셸에 붙여넣으면 `'--cwd'`가 리터럴 인자로 들어가 실행이 깨진다.
    """
    monkeypatch.setattr(guard, "_running_on_windows", lambda: True)
    conf = {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt",
    }
    payload = _payload("developer")
    payload["cwd"] = r"C:\Users\ci\work\project_manager_1"
    spaced = _payload("developer")
    spaced["cwd"] = r"C:\Users\ci\work tree\project_manager_1"

    reason = guard.evaluate_hook(payload, config_loader=lambda: conf)[
        "hookSpecificOutput"
    ]["permissionDecisionReason"]
    spaced_reason = guard.evaluate_hook(spaced, config_loader=lambda: conf)[
        "hookSpecificOutput"
    ]["permissionDecisionReason"]

    assert r"--cwd C:\Users\ci\work\project_manager_1" in reason
    assert "'" not in reason.split("--cwd ", 1)[1]
    assert r'--cwd "C:\Users\ci\work tree\project_manager_1"' in spaced_reason
    assert "&&" not in reason


def test_deny_remediation_keeps_posix_quoting_off_windows(guard, monkeypatch):
    monkeypatch.setattr(guard, "_running_on_windows", lambda: False)
    conf = {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt",
    }
    payload = _payload("developer")
    payload["cwd"] = "/work tree/project_manager_1"

    reason = guard.evaluate_hook(payload, config_loader=lambda: conf)[
        "hookSpecificOutput"
    ]["permissionDecisionReason"]

    assert "--cwd '/work tree/project_manager_1'" in reason


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


def _write_agent_card(root: Path, relative: Path, body: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


def test_tracked_claude_native_card_map_is_finite_and_nonempty(guard):
    """Read the shipped files, not a synthetic list, so the assertion cannot be vacuous."""
    assert set(guard.CLAUDE_NATIVE_AGENT_CARDS) == {
        (role, "normal") for role in ROLES
    }
    for role in ROLES:
        model, relative = guard._read_claude_native_agent_model(role, "normal")
        assert relative == Path(f".claude/agents/{role}.md")
        assert model == "opus"
        assert (REPO / relative).read_bytes()


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("enabled", (True, False))
def test_claude_four_native_cards_match_is_quiet_allow(guard, role, enabled):
    conf = {
        "delegate_enabled": str(enabled).lower(),
        f"delegate.{role}.harness": "claude",
        f"delegate.{role}.model": "opus",
    }
    result = guard.decide(role, "normal", conf, "claude")
    assert result["verdict"] == "allow"
    assert result["reason"].startswith("[delegate-channel/allow]")
    assert guard.evaluate_hook(_payload(role), config_loader=lambda: conf) is None


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("enabled", (True, False))
def test_claude_four_native_model_mismatches_are_nonblocking_hook_warnings(
    guard, role, enabled
):
    conf = {
        "delegate_enabled": str(enabled).lower(),
        f"delegate.{role}.harness": "claude",
        f"delegate.{role}.model": "not-opus",
    }
    result = guard.decide(role, "normal", conf, "claude")
    assert result["verdict"] == "allow"
    assert set(result) == {"verdict", "reason", "harness", "model"}
    for evidence in (
        f"role={role}", "tier=normal", "conf_model=not-opus",
        f"card=.claude/agents/{role}.md", "card_model=opus",
    ):
        assert evidence in result["reason"]

    hook = guard.evaluate_hook(_payload(role), config_loader=lambda: conf)
    output = hook["hookSpecificOutput"]
    assert set(output) == {"hookEventName", "additionalContext"}
    assert "permissionDecision" not in output
    assert output["additionalContext"] == result["reason"]


@pytest.mark.parametrize(
    ("state", "body", "evidence"),
    (
        ("missing", None, "FileNotFoundError"),
        ("invalid-utf8", b"\xff\xfe", "UnicodeDecodeError"),
        ("no-start", b"model: opus\n---\n", "시작 fence 없음"),
        ("unterminated", b"---\nmodel: opus\n", "종료 fence 없음"),
        ("model-missing", b"---\nname: developer\n---\n", "model 없음"),
        ("model-empty", b"---\nmodel:\n---\n", "비어 있음"),
        ("model-duplicate", b"---\nmodel: opus\nmodel: sonnet\n---\n", "model 중복"),
        ("model-sequence", b"---\nmodel: [opus]\n---\n", "scalar"),
    ),
)
@pytest.mark.parametrize("enabled", (True, False))
def test_claude_agent_card_failures_allow_with_additional_context(
    guard, monkeypatch, tmp_path, state, body, evidence, enabled
):
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    if body is not None:
        _write_agent_card(tmp_path, relative, body)
    conf = {
        "delegate_enabled": str(enabled).lower(),
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }

    result = guard.decide("developer", "normal", conf, "claude")
    assert result["verdict"] == "allow"
    assert "[delegate-channel/warn]" in result["reason"]
    assert evidence in result["reason"]
    hook = guard.evaluate_hook(_payload("developer"), config_loader=lambda: conf)
    assert set(hook["hookSpecificOutput"]) == {
        "hookEventName", "additionalContext",
    }


def test_claude_agent_card_quoted_scalar_is_normalized(guard, monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    _write_agent_card(tmp_path, relative, b'---\nmodel: "opus"\n---\n')
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    assert guard.decide("developer", "normal", conf, "claude")["reason"].startswith(
        "[delegate-channel/allow]"
    )


@pytest.mark.parametrize(
    "model_line",
    (
        "model: opus # shipped model",
        'model: "opus" # shipped model',
    ),
)
def test_claude_agent_card_yaml_inline_comment_is_quiet_allow(
    guard, monkeypatch, tmp_path, model_line
):
    """MF1: valid YAML comments do not become part of the configured model."""
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    _write_agent_card(
        tmp_path, relative, f"---\n{model_line}\n---\n".encode("utf-8")
    )
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    result = guard.decide("developer", "normal", conf, "claude")
    assert result["verdict"] == "allow"
    assert result["reason"].startswith("[delegate-channel/allow]")


def test_claude_agent_card_hash_inside_quoted_scalar_is_preserved(
    guard, monkeypatch, tmp_path
):
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    _write_agent_card(
        tmp_path, relative, b'---\nmodel: "opus#pinned" # comment\n---\n'
    )
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus#pinned",
    }
    assert guard.decide("developer", "normal", conf, "claude")["reason"].startswith(
        "[delegate-channel/allow]"
    )


@pytest.mark.parametrize("unsafe_kind", ("symlink", "hardlink", "directory"))
def test_claude_agent_card_unsafe_leaf_is_rejected_before_content_read(
    guard, monkeypatch, tmp_path, unsafe_kind
):
    """MF2: links/non-regular leaves never expose target content in warnings."""
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path / "repo")
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    target = guard._ENGINE_ROOT / relative
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside-card.md"
    outside.write_text("---\nmodel: external-secret\n---\n", encoding="utf-8")
    if unsafe_kind == "symlink":
        target.symlink_to(outside)
    elif unsafe_kind == "hardlink":
        target.hardlink_to(outside)
    else:
        target.mkdir()

    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    result = guard.decide("developer", "normal", conf, "claude")
    assert result["verdict"] == "allow"
    assert result["reason"].startswith("[delegate-channel/warn]")
    assert "symlink/reparse/hardlink/비-regular 거부" in result["reason"]
    assert "external-secret" not in result["reason"]


def test_claude_agent_card_parent_symlink_is_rejected_without_external_read(
    guard, monkeypatch, tmp_path
):
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path / "repo")
    guard._ENGINE_ROOT.mkdir()
    outside = tmp_path / "outside"
    (outside / "agents").mkdir(parents=True)
    (outside / "agents/developer.md").write_text(
        "---\nmodel: parent-link-secret\n---\n", encoding="utf-8"
    )
    (guard._ENGINE_ROOT / ".claude").symlink_to(outside)
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    reason = guard.decide("developer", "normal", conf, "claude")["reason"]
    assert "[delegate-channel/warn]" in reason
    assert "symlink/reparse/비-directory 거부" in reason
    assert "parent-link-secret" not in reason


def test_claude_agent_card_mapping_cannot_escape_repo(
    guard, monkeypatch, tmp_path
):
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path / "repo")
    guard._ENGINE_ROOT.mkdir()
    monkeypatch.setitem(
        guard.CLAUDE_NATIVE_AGENT_CARDS,
        ("developer", "normal"),
        Path("../outside-card.md"),
    )
    (tmp_path / "outside-card.md").write_text(
        "---\nmodel: containment-secret\n---\n", encoding="utf-8"
    )
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    reason = guard.decide("developer", "normal", conf, "claude")["reason"]
    assert "[delegate-channel/warn]" in reason
    assert "상대경로 거부" in reason
    assert "containment-secret" not in reason


@pytest.mark.parametrize("interpreter", ("native", "no_dir_fd", "windows_like"))
def test_claude_agent_card_inode_swap_is_rejected_before_read(
    guard, monkeypatch, tmp_path, interpreter
):
    """The lstat/open identity recheck closes a leaf replacement race on every reader.

    `_secure_dir_fd_supported()` 를 참으로 **위조하면** dir-fd 원시연산이 없는 인터프리터
    (Windows: `os.O_DIRECTORY` 부재·`supports_dir_fd` 공집합)에서 파일을 열기도 전에
    AttributeError 로 죽어 교체 판정이 아예 돌지 않는다. 능력을 위조하지 않고 이 인터프리터가
    실제로 고르는 reader 를 태운다 — 판정은 세 형상 모두에서 성립해야 한다.
    """
    if interpreter != "native":
        monkeypatch.setattr(guard.os, "supports_dir_fd", set())
    if interpreter == "windows_like":
        # Windows CPython 에 없는 POSIX 상수 — 부재 자체가 reader 선택의 입력이다.
        monkeypatch.delattr(guard.os, "O_DIRECTORY", raising=False)
        monkeypatch.delattr(guard.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    _write_agent_card(tmp_path, relative, b"---\nmodel: opus\n---\n")
    target = tmp_path / relative
    replacement = target.with_name("replacement.md")
    replacement.write_text("---\nmodel: race-secret\n---\n", encoding="utf-8")
    real_open = guard.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        dir_fd = kwargs.get("dir_fd")
        opening_leaf = (
            path == target.name if dir_fd is not None else Path(path) == target
        )
        if opening_leaf and not swapped:
            swapped = True
            replacement.replace(target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(guard.os, "open", swapping_open)
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    reason = guard.decide("developer", "normal", conf, "claude")["reason"]
    assert swapped
    assert "inode 교체" in reason
    assert "race-secret" not in reason


def test_file_identity_rejects_volumes_without_a_file_index(guard, tmp_path):
    """st_ino 를 안 주는 볼륨(FAT/exFAT·일부 네트워크)에서는 교체를 구분할 수 없다.

    0 끼리 비교하면 바꿔치기가 **조용히 통과**하므로 판정 불능은 loud 거부여야 한다.
    """
    probe = tmp_path / "probe.md"
    probe.write_text("x\n", encoding="utf-8")
    real = probe.stat()
    degenerate = guard.os.stat_result((
        real.st_mode, 0, real.st_dev, real.st_nlink, real.st_uid,
        real.st_gid, real.st_size, 0, 0, 0,
    ))

    assert guard._file_identity(real, "probe.md") == (real.st_dev, real.st_ino)
    with pytest.raises(ValueError) as exc:
        guard._file_identity(degenerate, "probe.md")
    assert "판정 불능" in str(exc.value)


def test_zero_file_index_card_read_fails_loud_instead_of_matching(
    guard, monkeypatch, tmp_path
):
    """정체성 미제공 볼륨의 card 는 조용한 일치가 아니라 loud 경고로 나간다 (fail-open 유지)."""
    monkeypatch.setattr(guard.os, "supports_dir_fd", set())
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    _write_agent_card(tmp_path, relative, b"---\nmodel: opus\n---\n")
    target = tmp_path / relative
    real_lstat = Path.lstat

    def zero_index_lstat(self, *args, **kwargs):
        observed = real_lstat(self, *args, **kwargs)
        if self == target:
            return guard.os.stat_result((
                observed.st_mode, 0, observed.st_dev, observed.st_nlink,
                observed.st_uid, observed.st_gid, observed.st_size, 0, 0, 0,
            ))
        return observed

    monkeypatch.setattr(Path, "lstat", zero_index_lstat)
    assert target.lstat().st_ino == 0        # 주입 확인: 실제로 정체성 없는 형상이 관측된다
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    result = guard.decide("developer", "normal", conf, "claude")

    assert result["verdict"] == "allow"
    assert "[delegate-channel/warn]" in result["reason"]
    assert "판정 불능" in result["reason"]
    assert "TypeError" not in result["reason"]   # 코드 결함이 같은 문구로 덮이지 않는다
    assert "native 통과(fail-open)" in result["reason"]


@pytest.mark.parametrize("role", ROLES)
def test_portable_reader_normal_tracked_cards_are_quiet_match(
    guard, monkeypatch, role
):
    """MF3: Windows/no-dir-fd must not degrade every healthy card to warning."""
    monkeypatch.setattr(guard.os, "supports_dir_fd", set())
    conf = {
        f"delegate.{role}.harness": "claude",
        f"delegate.{role}.model": "opus",
    }
    result = guard.decide(role, "normal", conf, "claude")
    assert result["verdict"] == "allow"
    assert result["reason"].startswith("[delegate-channel/allow]")


def test_secure_reader_capability_uses_os_support_sets(guard, monkeypatch):
    monkeypatch.setattr(guard.os, "supports_dir_fd", set())
    assert guard._secure_dir_fd_supported() is False


def test_secure_reader_does_not_retry_security_failure_via_portable_path(
    guard, monkeypatch
):
    calls = []
    monkeypatch.setattr(guard, "_secure_dir_fd_supported", lambda: True)

    def failed_strong(*_args):
        calls.append("strong")
        raise NotImplementedError("simulated strong-path failure")

    def forbidden_fallback(*_args):
        calls.append("portable")
        raise AssertionError("security failure must not trigger a weaker retry")

    monkeypatch.setattr(guard, "_read_known_regular_file_dir_fd", failed_strong)
    monkeypatch.setattr(
        guard, "_read_known_regular_file_portable", forbidden_fallback
    )
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    reason = guard.decide("developer", "normal", conf, "claude")["reason"]
    assert calls == ["strong"]
    assert "simulated strong-path failure" in reason


def test_windows_reparse_metadata_is_linklike(guard):
    metadata = SimpleNamespace(
        st_mode=stat.S_IFREG,
        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
    )
    assert guard._metadata_is_linklike(metadata) is True


def _windows_shaped_metadata(observed: object, *, st_ino: int) -> SimpleNamespace:
    """10-tuple 로 만든 Windows ``stat_result`` 형상을 POSIX 에서 재현한다.

    Windows CPython 의 ``st_file_attributes`` 는 structseq 의 **선택 필드**라, 10-tuple 로 만든
    ``stat_result`` 는 그 이름을 가지되 값이 ``None`` 이다(POSIX 빌드는 이름 자체가 없다). 이
    형상을 POSIX 에서 만들려면 stat_result 로는 안 되고 필드를 직접 세운 객체가 필요하다.
    """
    return SimpleNamespace(
        st_mode=observed.st_mode,
        st_ino=st_ino,
        st_dev=observed.st_dev,
        st_nlink=observed.st_nlink,
        st_file_attributes=None,
    )


def test_metadata_without_attribute_value_is_judged_not_type_error(guard):
    """값이 없는 Windows 속성 필드에 비트 연산을 걸어 `TypeError` 로 새지 않는다.

    Windows 실측에서 이 연산이 `TypeError: unsupported operand type(s) for &: 'NoneType' and
    'int'` 를 냈고, 그 타입 오류가 카드 검사 실패 문구를 타고 나가 진짜 판정 불능과 섞였다.
    """
    metadata = SimpleNamespace(
        st_mode=stat.S_IFREG, st_ino=7, st_dev=1, st_nlink=1,
        st_file_attributes=None,
    )
    # 주입 확인: 필드가 **있고** 값이 없는 형상이어야 이 회귀가 그 연산을 태운다.
    assert hasattr(metadata, "st_file_attributes")
    assert metadata.st_file_attributes is None

    assert guard._metadata_is_linklike(metadata) is False


def test_zero_file_index_windows_shaped_card_reports_the_identity_reason(
    guard, monkeypatch, tmp_path
):
    """Windows 형상(속성 값 없음 + st_ino 0)에서도 사유는 정체성 판정 불능 하나다.

    타입 오류가 같은 경고 채널로 새면 코드 결함과 진짜 판정 불능이 한 문구로 덮인다.
    """
    monkeypatch.setattr(guard.os, "supports_dir_fd", set())
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    _write_agent_card(tmp_path, relative, b"---\nmodel: opus\n---\n")
    target = tmp_path / relative
    real_lstat = Path.lstat

    def windows_shaped_lstat(self, *args, **kwargs):
        observed = real_lstat(self, *args, **kwargs)
        if self == target:
            return _windows_shaped_metadata(observed, st_ino=0)
        return observed

    monkeypatch.setattr(Path, "lstat", windows_shaped_lstat)
    # 주입 확인: 카드 경로만 Windows 형상이고 부모 디렉터리는 실 metadata 그대로다.
    injected = target.lstat()
    assert injected.st_ino == 0 and injected.st_file_attributes is None
    assert isinstance(target.parent.lstat(), guard.os.stat_result)

    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    result = guard.decide("developer", "normal", conf, "claude")

    assert result["verdict"] == "allow"
    assert "[delegate-channel/warn]" in result["reason"]
    assert "파일 정체성(st_ino) 미제공 볼륨" in result["reason"]
    assert "TypeError" not in result["reason"]
    assert "native 통과(fail-open)" in result["reason"]


def test_portable_reader_symlink_rejects_before_any_content_read(
    guard, monkeypatch, tmp_path
):
    monkeypatch.setattr(guard.os, "supports_dir_fd", set())
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path / "repo")
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    target = guard._ENGINE_ROOT / relative
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("---\nmodel: windows-symlink-secret\n---\n", encoding="utf-8")
    target.symlink_to(outside)
    reads = []

    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError("unsafe portable path reached content read")

    monkeypatch.setattr(guard.os, "read", forbidden_read)
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    reason = guard.decide("developer", "normal", conf, "claude")["reason"]
    assert reads == []
    assert "[delegate-channel/warn]" in reason
    assert "windows-symlink-secret" not in reason


def test_portable_reader_inode_swap_rejects_before_any_content_read(
    guard, monkeypatch, tmp_path
):
    monkeypatch.setattr(guard.os, "supports_dir_fd", set())
    monkeypatch.setattr(guard, "_ENGINE_ROOT", tmp_path)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    _write_agent_card(tmp_path, relative, b"---\nmodel: opus\n---\n")
    target = tmp_path / relative
    replacement = target.with_name("replacement.md")
    replacement.write_text(
        "---\nmodel: windows-race-secret\n---\n", encoding="utf-8"
    )
    real_open = guard.os.open
    reads = []
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == target and not swapped:
            swapped = True
            replacement.replace(target)
        return real_open(path, flags, *args, **kwargs)

    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError("replaced portable path reached content read")

    monkeypatch.setattr(guard.os, "open", swapping_open)
    monkeypatch.setattr(guard.os, "read", forbidden_read)
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    reason = guard.decide("developer", "normal", conf, "claude")["reason"]
    assert swapped and reads == []
    assert "inode 교체" in reason
    assert "windows-race-secret" not in reason


def test_portable_reader_root_swap_during_resolve_rejects_before_read(
    guard, monkeypatch, tmp_path
):
    """NEW-MF5: bind lexical engine-root identity across lstat -> resolve."""
    monkeypatch.setattr(guard.os, "supports_dir_fd", set())
    repo = tmp_path / "repo"
    monkeypatch.setattr(guard, "_ENGINE_ROOT", repo)
    relative = guard.CLAUDE_NATIVE_AGENT_CARDS[("developer", "normal")]
    _write_agent_card(repo, relative, b"---\nmodel: opus\n---\n")
    outside = tmp_path / "outside"
    _write_agent_card(
        outside, relative, b"---\nmodel: ROOT_SWAP_SENTINEL\n---\n"
    )
    moved = tmp_path / "trusted-root-moved"
    real_resolve = Path.resolve
    swapped = False
    reads = []

    def swapping_resolve(self, *args, **kwargs):
        nonlocal swapped
        if self == repo and not swapped:
            swapped = True
            repo.rename(moved)
            repo.symlink_to(outside, target_is_directory=True)
        return real_resolve(self, *args, **kwargs)

    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError("root-swapped path reached content read")

    monkeypatch.setattr(Path, "resolve", swapping_resolve)
    monkeypatch.setattr(guard.os, "read", forbidden_read)
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    result = guard.decide("developer", "normal", conf, "claude")
    assert swapped and reads == []
    assert result["verdict"] == "allow"
    assert result["reason"].startswith("[delegate-channel/warn]")
    assert "engine root 교체 거부" in result["reason"]
    assert "ROOT_SWAP_SENTINEL" not in result["reason"]


def test_claude_agent_card_reader_exception_is_nonblocking_warning(
    guard, monkeypatch
):
    def broken_reader(_role, _tier):
        raise RuntimeError("reader\nfailed")

    monkeypatch.setattr(guard, "_read_claude_native_agent_model", broken_reader)
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    }
    result = guard.decide("developer", "normal", conf, "claude")
    assert result["verdict"] == "allow"
    assert "RuntimeError: reader failed" in result["reason"]
    hook = guard.evaluate_hook(_payload("developer"), config_loader=lambda: conf)
    assert "permissionDecision" not in hook["hookSpecificOutput"]


def test_claude_unsupported_hard_is_not_guessed_and_warns(guard):
    conf = {
        "delegate.developer.hard.harness": "claude",
        "delegate.developer.hard.model": "opus",
    }
    result = guard.decide("developer", "hard", conf, "claude")
    assert result["verdict"] == "allow"
    assert "명시 agent card 매핑 없음(developer/hard)" in result["reason"]
    assert "developer-hard.md" not in result["reason"]


def test_codex_hard_and_opencode_native_do_not_read_claude_cards(
    guard, monkeypatch
):
    def unexpected(*_args):
        raise AssertionError("non-Claude adapters must not inspect Claude cards")

    monkeypatch.setattr(guard, "_claude_native_model_warning", unexpected)
    codex = {
        "delegate.developer.hard.harness": "codex",
        "delegate.developer.hard.model": "gpt-5.6-sol",
    }
    opencode = {
        "delegate.researcher.harness": "opencode",
        "delegate.researcher.model": "qwen",
    }
    assert guard.decide("developer", "hard", codex, "codex")["verdict"] == "allow"
    assert guard.decide("researcher", "normal", opencode, "opencode")["verdict"] == "allow"


@pytest.mark.parametrize("enabled", (True, False))
def test_unset_is_quiet_but_partial_tuple_is_loud_fail_open(guard, enabled):
    unset = {"delegate_enabled": str(enabled).lower()}
    partial = {
        "delegate_enabled": str(enabled).lower(),
        "delegate.developer.harness": "claude",
    }
    quiet = guard.decide("developer", "normal", unset, "claude")
    warning = guard.decide("developer", "normal", partial, "claude")
    assert quiet["verdict"] == warning["verdict"] == "allow"
    assert quiet["reason"].startswith("[delegate-channel/allow]")
    assert warning["reason"].startswith("[delegate-channel/warn]")


def test_decide_cli_surfaces_native_warning_in_stable_four_fields(guard):
    stdout = io.StringIO()
    conf = {
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "sonnet",
    }
    assert guard.main(
        ["decide", "--role", "developer", "--harness", "claude"],
        stdout=stdout,
        config_loader=lambda: conf,
    ) == 0
    result = json.loads(stdout.getvalue())
    assert set(result) == {"verdict", "reason", "harness", "model"}
    assert result["verdict"] == "allow"
    assert result["reason"].startswith("[delegate-channel/warn]")


def test_delegation_docs_distinguish_common_mapping_from_cross_transport():
    skill = (REPO / ".claude/skills/pm-dev-delegate/SKILL.md").read_text("utf-8")
    readme = (REPO / "README.md").read_text("utf-8")
    playbook = (REPO / ".project_manager/wiki/pm_playbook.md").read_text("utf-8")
    combined = "\n".join((skill, readme, playbook))
    assert "cross-harness 판정과 위임" not in combined
    assert combined.count("native") >= 3
    assert combined.count("cross transport") >= 3
    assert "cross 위임은 코드/프롬프트·worktree 내용을 외부 하네스로 전송" in skill
    assert "delegate_enabled" in readme and "delegate_enabled" in playbook
