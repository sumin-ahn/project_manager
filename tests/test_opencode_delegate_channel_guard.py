"""T-0641 OpenCode native task delegation-channel adapter tests."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
OPEN_LIB = REPO / "templates" / "opencode" / ".opencode" / "lib"
OPEN_CORE = OPEN_LIB / "delegate-channel-core.cjs"
OPEN_WARNING = OPEN_LIB / "warning-channel-core.cjs"
OPEN_GIT_ANCHOR = OPEN_LIB / "git-anchor-core.cjs"
OPEN_PLUGIN = (
    REPO / "templates" / "opencode" / ".opencode" / "plugins" / "delegate-channel.js"
)
GUARD_PY = REPO / ".project_manager" / "tools" / "delegate_channel_guard.py"
ROLES = ("developer", "code-reviewer", "researcher", "architect")


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable unavailable; OpenCode adapter coverage skipped")
    return node


def _install_core(root: Path, *, with_engine: bool) -> Path:
    """core 사본을 픽스처의 `<root>/.opencode/lib/` 에 둔다 — 설치 형상 그대로.

    core 는 엔진 루트를 자기 위치(`path.resolve(__dirname, "..", "..")`)에서 내므로, 판정
    엔진의 존재 여부는 이 픽스처 안에서 정해진다.
    """
    lib = root / ".opencode" / "lib"
    lib.mkdir(parents=True)
    for name in ("delegate-channel-core.cjs", "warning-channel-core.cjs"):
        shutil.copyfile(OPEN_LIB / name, lib / name)
    if with_engine:
        tools = root / ".project_manager" / "tools"
        tools.mkdir(parents=True)
        (tools / GUARD_PY.name).write_text("", encoding="utf-8")
    return lib


def _load_guard():
    spec = importlib.util.spec_from_file_location("delegate_guard_opencode", GUARD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opencode_plugin_shape_defaults_and_no_javascript_role_truth():
    assert OPEN_CORE.is_file() and OPEN_WARNING.is_file() and OPEN_PLUGIN.is_file()
    plugin = OPEN_PLUGIN.read_text(encoding="utf-8")
    core = OPEN_CORE.read_text(encoding="utf-8")
    git_anchor = OPEN_GIT_ANCHOR.read_text(encoding="utf-8")
    assert plugin.count("export const") == 1
    assert "DelegateChannelPlugin" in plugin
    assert 'toolName: "task"' in core
    assert 'roleArgKey: "subagent_type"' in core
    assert 'roleValuePrefix: ""' in core
    assert "enforceDeny: true" in core
    assert "OpenCode 1.18.12" in core and "1.18.5" in core
    assert 'worktree="/"' in core
    assert "directory || worktree" in core
    assert 'require("./warning-channel-core.cjs")' in core
    assert 'require("./warning-channel-core.cjs")' in git_anchor
    for role in ROLES:
        assert f'"{role}"' not in core, f"JS must not duplicate Python role literal {role}"


def test_opencode_core_spawn_contract_deny_and_warn_fail_open():
    script = r'''
const assert = require("node:assert");
const m = require("./delegate-channel-core.cjs");

let spawned;
const fakeSpawn = (py, argv, options) => {
  if (argv[argv.length - 1].endsWith("python_floor.py")) {
    return {status: 0, stdout: "Python 3.12\n", stderr: ""};
  }
  spawned = {py, argv, options};
  return {
    status: 0,
    stdout: JSON.stringify({
      verdict: "deny", reason: "[delegate-channel/deny] fixture",
      harness: "claude", model: "opus",
    }) + "\n",
    stderr: "",
  };
};
const denied = m.judgeDelegation("/repo", "/repo with space", "developer", null, fakeSpawn);
assert.strictEqual(denied.verdict, "deny");
assert.strictEqual(spawned.options.input, "");
assert.strictEqual(spawned.options.timeout, m.DECIDE_TIMEOUT_MS);
assert.deepStrictEqual(
  spawned.argv.slice(1),
  ["decide", "--role", "developer", "--harness", "opencode", "--cwd", "/repo with space"],
);

for (const result of [
  {status: 7, stdout: "", stderr: "bad rc"},
  {status: 0, stdout: "not-json\n", stderr: ""},
  {status: 0, stdout: "{}\n", stderr: ""},
  {status: null, stdout: "", stderr: "", error: {code: "ETIMEDOUT", message: "timeout"}},
]) {
  const got = m.judgeDelegation(
    "/repo", "/repo", "developer", null,
    (_py, argv) => argv[argv.length - 1].endsWith("python_floor.py")
      ? {status: 0, stdout: "Python 3.12\n", stderr: ""}
      : result,
  );
  assert.strictEqual(got.verdict, "allow");
  assert.strictEqual(got.warning, true);
  assert.match(got.reason, /fail-open/);
}

(async () => {
  const toasts = [];
  const seenAgents = [];
  const judge = (_root, _cwd, agent) => {
    seenAgents.push(agent);
    return {
      verdict: agent === "developer" ? "deny" : "allow",
      reason: agent === "developer"
        ? "[delegate-channel/deny] cross fixture"
        : "[delegate-channel/allow] self fixture",
      harness: "claude", model: "fixture",
    };
  };
  const hooks = await m.makeDelegateChannelPlugin(judge)({
    directory: "/repo",
    client: {tui: {showToast: async (value) => toasts.push(value)}},
  });
  await hooks["tool.execute.before"](
    {tool: "bash", sessionID: "ignored"},
    {args: {subagent_type: "developer"}},
  );
  await hooks["tool.execute.before"](
    {tool: "task", sessionID: "missing-role"},
    {args: {}},
  );
  await assert.rejects(
    hooks["tool.execute.before"](
      {tool: "task", sessionID: "deny"},
      {args: {subagent_type: "developer"}},
    ),
    /delegate-channel\/deny/,
  );
  assert.deepStrictEqual(seenAgents, ["", "developer"]);

  const warningHooks = await m.makeDelegateChannelPlugin(() => {
    throw new Error("fixture boom");
  })({
    directory: "/repo",
    client: {tui: {showToast: async (value) => toasts.push(value)}},
  });
  await warningHooks["tool.execute.before"](
    {tool: "task", sessionID: "warn"},
    {args: {subagent_type: "researcher"}},
  );
  const context = {system: []};
  await warningHooks["experimental.chat.system.transform"](
    {sessionID: "warn"}, context,
  );
  assert.strictEqual(context.system.length, 1);
  assert.match(context.system[0], /판정 인프라 예외\(fixture boom\)/);
  assert.strictEqual(toasts.length, 1);

  let injectedAgent;
  const injected = await m.makeDelegateChannelPlugin(
    (_root, _cwd, agent) => {
      injectedAgent = agent;
      return {
        verdict: "deny", reason: "[delegate-channel/deny] advisory fixture",
        harness: "claude", model: "fixture",
      };
    },
    {
      toolName: "measured-task",
      roleArgKey: "measured_agent",
      roleValuePrefix: "prefix:",
      enforceDeny: false,
    },
  )({directory: "/repo", client: {tui: {showToast: async () => {}}}});
  await injected["tool.execute.before"](
    {tool: "measured-task", sessionID: "advisory"},
    {args: {measured_agent: "prefix:developer"}},
  );
  assert.strictEqual(injectedAgent, "developer");
  const advisoryContext = {system: []};
  await injected["experimental.chat.system.transform"](
    {sessionID: "advisory"}, advisoryContext,
  );
  assert.match(advisoryContext.system[0], /advisory 강등/);
  console.log("DELEGATE_CHANNEL_CORE_OK");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [_node(), "-e", script],
        cwd=OPEN_LIB,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "DELEGATE_CHANNEL_CORE_OK" in result.stdout


def test_opencode_non_delegation_agent_record_does_not_toast_or_inject_context():
    """Unknown built-ins such as general are recorded by Python without alert fatigue."""
    judgment = _load_guard().decide("general", "normal", {}, "opencode")
    assert judgment["verdict"] == "allow"
    assert "delegate-channel/record" in judgment["reason"]
    assert "delegate-channel/warn" not in judgment["reason"]

    script = r'''
const assert = require("node:assert");
const m = require("./delegate-channel-core.cjs");
const judgment = JSON.parse(process.env.QUIET_JUDGMENT);
(async () => {
  const toasts = [];
  const hooks = await m.makeDelegateChannelPlugin(() => judgment)({
    directory: "/repo",
    client: {tui: {showToast: async (value) => toasts.push(value)}},
  });
  await hooks["tool.execute.before"](
    {tool: "task", sessionID: "general-session"},
    {args: {subagent_type: "general"}},
  );
  const context = {system: []};
  await hooks["experimental.chat.system.transform"](
    {sessionID: "general-session"}, context,
  );
  assert.deepStrictEqual(toasts, []);
  assert.deepStrictEqual(context.system, []);
  console.log("QUIET_GENERAL_OK");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [_node(), "-e", script],
        cwd=OPEN_LIB,
        env={**os.environ, "QUIET_JUDGMENT": json.dumps(judgment)},
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "QUIET_GENERAL_OK" in result.stdout


def test_opencode_windows_py_only_environment_uses_launcher_after_floor_probe():
    script = r'''
const assert = require("node:assert");
const m = require("./delegate-channel-core.cjs");
const calls = [];
const fakeSpawn = (command, argv, options) => {
  calls.push({command, argv, options});
  assert.strictEqual(command, "py", "py-only environment must not need a shim");
  if (argv[argv.length - 1].endsWith("python_floor.py")) {
    return {status: 0, stdout: "Python 3.12\n", stderr: ""};
  }
  return {
    status: 0,
    stdout: JSON.stringify({
      verdict: "deny", reason: "fixture deny", harness: "claude", model: "opus",
    }) + "\n",
    stderr: "",
  };
};
const got = m.judgeDelegation(
  "C:\\repo", "C:\\repo", "developer", null, fakeSpawn, "win32",
);
assert.strictEqual(got.verdict, "deny");
assert.strictEqual(calls.length, 2);
assert.deepStrictEqual(calls.map((item) => item.command), ["py", "py"]);
assert.strictEqual(calls[0].argv[0], "-3");
assert.ok(calls[0].argv[1].endsWith("python_floor.py"));
assert.strictEqual(calls[1].argv[0], "-3");
assert.ok(calls[1].argv[1].endsWith("delegate_channel_guard.py"));
assert.ok(calls.every((item) => item.options.input === ""));
'''
    subprocess.run(
        [_node(), "-e", script], cwd=OPEN_LIB, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )


def test_opencode_windows_broken_first_shim_falls_back_to_next_candidate():
    script = r'''
const assert = require("node:assert");
const m = require("./delegate-channel-core.cjs");
const calls = [];
const fakeSpawn = (command, argv) => {
  const probe = argv[argv.length - 1].endsWith("python_floor.py");
  calls.push(`${command}:${probe ? "probe" : "guard"}`);
  if (command === "py") {
    return {status: 126, stdout: "", stderr: "Permission denied"};
  }
  assert.strictEqual(command, "python3");
  if (probe) return {status: 0, stdout: "Python 3.11\n", stderr: ""};
  return {
    status: 0,
    stdout: JSON.stringify({
      verdict: "allow", reason: "fixture allow", harness: "opencode", model: "m",
    }) + "\n",
    stderr: "",
  };
};
const got = m.judgeDelegation(
  "C:\\repo", "C:\\repo", "developer", null, fakeSpawn, "win32",
);
assert.strictEqual(got.verdict, "allow");
assert.strictEqual(got.warning, undefined);
assert.deepStrictEqual(calls, ["py:probe", "python3:probe", "python3:guard"]);
'''
    subprocess.run(
        [_node(), "-e", script], cwd=OPEN_LIB, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )


@pytest.mark.parametrize(
    "scenario",
    ("first_missing", "first_guard_rc", "all_missing"),
)
def test_opencode_python_candidate_retry_boundary_matrix(scenario):
    """후보 부재/guard rc 실패는 재시도하고 전 후보 부재만 fail-open 한다."""
    script = r'''
const assert = require("node:assert");
const m = require("./delegate-channel-core.cjs");
const scenario = process.env.RETRY_SCENARIO;
const calls = [];
const judgment = {
  status: 0,
  stdout: JSON.stringify({
    verdict: "deny", reason: "fixture deny", harness: "claude", model: "opus",
  }) + "\n",
  stderr: "",
};
const missing = (command) => ({
  status: null,
  stdout: "",
  stderr: "",
  error: {code: "ENOENT", message: `${command} missing`},
});
const fakeSpawn = (command, argv) => {
  const probe = argv[argv.length - 1].endsWith("python_floor.py");
  calls.push(`${command}:${probe ? "probe" : "guard"}`);

  if (scenario === "all_missing") return missing(command);
  if (scenario === "first_missing" && command === "py") return missing(command);
  if (scenario === "first_guard_rc" && command === "py" && !probe) {
    return {status: 126, stdout: "", stderr: "Permission denied"};
  }
  if (probe) return {status: 0, stdout: "Python 3.12\n", stderr: ""};
  return judgment;
};

const got = m.judgeDelegation(
  "C:\\repo", "C:\\repo", "developer", null, fakeSpawn, "win32",
);
const expectedCalls = {
  first_missing: ["py:probe", "python3:probe", "python3:guard"],
  first_guard_rc: ["py:probe", "py:guard", "python3:probe", "python3:guard"],
  all_missing: ["py:probe", "python3:probe", "python:probe"],
};
assert.deepStrictEqual(calls, expectedCalls[scenario]);
if (scenario === "all_missing") {
  assert.strictEqual(got.verdict, "allow");
  assert.strictEqual(got.warning, true);
  assert.match(got.reason, /Python 판정 후보 소진/);
} else {
  assert.strictEqual(got.verdict, "deny");
  assert.strictEqual(got.warning, undefined);
}
'''
    subprocess.run(
        [_node(), "-e", script], cwd=OPEN_LIB,
        env={**os.environ, "RETRY_SCENARIO": scenario},
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


def test_opencode_all_python_candidates_fail_open_with_fixed_attempt_reason():
    script = r'''
const assert = require("node:assert");
const m = require("./delegate-channel-core.cjs");
const calls = [];
const fakeSpawn = (command) => {
  calls.push(command);
  if (command === "py") {
    return {
      status: null, stdout: "", stderr: "",
      error: {code: "ENOENT", message: "py missing"},
    };
  }
  if (command === "python3") {
    return {status: 126, stdout: "", stderr: "Permission denied"};
  }
  return {status: 0, stdout: "Python 3.10\n", stderr: ""};
};
const got = m.judgeDelegation(
  "C:\\repo", "C:\\repo", "developer", null, fakeSpawn, "win32",
);
assert.strictEqual(got.verdict, "allow");
assert.strictEqual(got.warning, true);
assert.deepStrictEqual(calls, ["py", "python3", "python"]);
assert.strictEqual(
  got.reason,
  "[delegate-channel/warn] Python 판정 후보 소진(" +
    "시도 후보: py -3, python3, python; 실패: " +
    "py -3=버전 검증 실행 불가(ENOENT: py missing); " +
    "python3=버전 검증 rc=126(Permission denied); " +
    "python=Python 3.10 < 3.11) — 통과(fail-open)",
);
'''
    subprocess.run(
        [_node(), "-e", script], cwd=OPEN_LIB, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )


def test_opencode_real_python_cli_parity_same_matrix(tmp_path):
    """CJS subprocesses the copied real Python CLI over the Python unit matrix."""
    guard = _load_guard()
    engine_root = tmp_path / "adopter"
    tools = engine_root / ".project_manager" / "tools"
    shutil.copytree(REPO / ".project_manager" / "tools", tools)
    conf_path = engine_root / ".project_manager" / "local.conf"

    cases = []
    for role in ROLES:
        for enabled in (True, False):
            for mapping in ("self", "cross", "absent"):
                conf = {"delegate.enabled": "true" if enabled else "false"}
                if mapping != "absent":
                    conf[f"delegate.{role}.harness"] = (
                        "opencode" if mapping == "self" else "claude"
                    )
                    conf[f"delegate.{role}.model"] = "fixture-model"
                expected = guard.decide(role, "normal", conf, "opencode")["verdict"]
                cases.append({"role": role, "conf": conf, "expected": expected})

    script = r'''
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const core = require(process.env.DELEGATE_CORE);
const root = process.env.ENGINE_ROOT;
const cases = JSON.parse(process.env.PARITY_CASES);
for (const item of cases) {
  const text = Object.entries(item.conf).map(([key, value]) => `${key}=${value}`).join("\n") + "\n";
  fs.writeFileSync(path.join(root, ".project_manager", "local.conf"), text, "utf8");
  const got = core.judgeDelegation(root, root, item.role, null);
  assert.strictEqual(got.verdict, item.expected, JSON.stringify(item));
  assert.strictEqual(got.warning, undefined, got.reason);
  // 스위치 off 의 처방은 conf 한 줄이다 — CLI 재실행을 권하면 그 실행이 다시 rc=3 이라 순환이다.
  if (got.verdict === "deny" && item.conf["delegate.enabled"] !== "false") {
    assert.match(got.reason, new RegExp(`--cwd ${root.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
    assert.ok(!got.reason.includes("<worktree>"));
  }
  if (got.verdict === "deny" && item.conf["delegate.enabled"] === "false") {
    assert.match(got.reason, /delegate\.enabled/);
  }
}
console.log(`REAL_PYTHON_PARITY_OK:${cases.length}`);
'''
    env = {
        **os.environ,
        "DELEGATE_CORE": str(OPEN_CORE),
        "ENGINE_ROOT": str(engine_root),
        "PARITY_CASES": json.dumps(cases),
    }
    result = subprocess.run(
        [_node(), "-e", script],
        cwd=OPEN_LIB,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert f"REAL_PYTHON_PARITY_OK:{len(cases)}" in result.stdout


def test_opencode_delegate_channel_cwd_axis_prefers_directory_over_root_worktree(tmp_path):
    """판정 대상 cwd 축은 `directory || worktree || process.cwd()` 순 그대로다.

    라이브 1.18.12/1.18.5 프로브에서 worktree="/" 가 관측됐고 1.18.25 바이너리도 worktree!=="/"
    를 특례로 갖는다 — worktree 를 앞세우면 판정이 "/" 를 받는다. 엔진 루트를 자기 위치에서
    받도록 바꾼 뒤에도 이 축은 payload 그대로여야 한다.
    """
    lib = _install_core(tmp_path / "install", with_engine=True)
    script = r"""
const assert = require("node:assert");
const m = require("./delegate-channel-core.cjs");
(async () => {
  const seen = [];
  const judge = (_root, cwd) => {
    seen.push(cwd);
    return {
      verdict: "allow", reason: "[delegate-channel/allow] fixture",
      harness: "opencode", model: "fixture",
    };
  };
  const hooks = await m.makeDelegateChannelPlugin(judge)({
    directory: "/repo",
    worktree: "/",
    client: {tui: {showToast: async () => {}}},
  });
  await hooks["tool.execute.before"](
    {tool: "task", sessionID: "cwd-axis"},
    {args: {subagent_type: "prober"}},
  );
  assert.deepStrictEqual(seen, ["/repo"]);
  console.log("DELEGATE_CWD_AXIS_OK");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [_node(), "-e", script], cwd=lib, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert "DELEGATE_CWD_AXIS_OK" in result.stdout


def test_opencode_delegate_channel_blocks_when_guard_engine_absent(tmp_path):
    """설치 트리에 판정 엔진이 없으면 fail-open 경고가 아니라 위임 자체를 막는다.

    payload `directory` 와 조상(이 저장소)에는 `delegate_channel_guard.py` 가 있다 — 그 둘 중
    하나로 엔진 루트를 잡는 코드라면 판정이 호출돼 통과한다. enforceDeny 는 역할 판정 deny 의
    스위치라 설치 무결성에는 적용하지 않는다.
    """
    lib = _install_core(tmp_path / "install", with_engine=False)
    assert not (tmp_path / "install" / ".project_manager").exists()
    script = r"""
const assert = require("node:assert");
const m = require("./delegate-channel-core.cjs");
const repoWithEngine = process.argv[1];
(async () => {
  const toasts = [];
  const judge = () => { throw new Error("설치가 깨졌는데 판정을 호출함"); };
  const hooks = await m.makeDelegateChannelPlugin(judge)({
    directory: repoWithEngine,
    worktree: repoWithEngine,
    client: {tui: {showToast: async (value) => toasts.push(value)}},
  });
  // 설치 확인은 toolName 선필터 뒤다 — 다른 도구는 그대로 지나간다.
  await hooks["tool.execute.before"]({tool: "bash", sessionID: "S"}, {args: {}});
  await assert.rejects(
    hooks["tool.execute.before"](
      {tool: "task", sessionID: "S"}, {args: {subagent_type: "developer"}},
    ),
    /\[delegate-channel\/설치\]/,
  );
  const context = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID: "S"}, context);
  assert.deepStrictEqual(context.system, [], "차단 대신 경고로 강등됨");
  assert.deepStrictEqual(toasts, [], "차단 대신 toast 로 강등됨");
  console.log("DELEGATE_INSTALL_BLOCK_OK");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [_node(), "-e", script, str(REPO)], cwd=lib, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert "DELEGATE_INSTALL_BLOCK_OK" in result.stdout
