"""OpenCode 일반 도구 판정 경계와 native ctx 전달 계약.

OpenCode 의 등록 before 전체가 Python 을 한 번(`board.py hook-tool`)만 띄워 git·삭제·도구 입력·ctx·원칙
recall 을 판정하고, assistant 관측·압축 경계는 `board.py hook-context` 로 엔진 ctx 판정을 받는지를 실제
CLI·실제 plugin factory·marker 파일로 검증한다. 엔진은 임시 설치 사본이며 압축 한도 기록은 고정 모델
한도 fixture(사용 한도 200,000·중간 기록 권고 70%·압축 임박 경고 80%)다. 하네스·네트워크는 부르지 않고
SDK client 는 `client.session.get` 형식의 대역이다.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _textio import utf8_child_env
from pm_engine.cli import hooks as cli_hooks
from pm_engine.domain import ctx_recovery
from test_harness_ctx_limits import install_ctx_record
from test_opencode_ctx_guard import CORE_DIR, _make_ctx_guard_project

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "templates"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node 없음")

RESPONSE_KEYS = {"version", "git", "context", "context_error", "recall", "recall_error"}
CONTEXT_KEYS = {"version", "status", "measured", "used_pct", "level", "notice", "guidance", "emit",
                "compaction_new", "compaction_guidance", "compaction_boundary_id"}
# 사용자 허용 표식 — 판정 대상 문자열로만 쓴다.
GRANT_MARK = "git " + "\uD5C8\uC6A9" + ":"
RULES = "#### 번들\n- `[shell: git\\s+status]` 상태 확인 규칙 본문.\n"
# 판정 경계가 세 하네스 공통으로 원문 전달해야 하는 셸 문자열(선필터가 놓치던 형태 포함).
UNFILTERED = ["echo ok", "g\\it status", "g\\it restore -- x", "G=git; $G restore -- x", "x=1; echo ok"]
# 등록 plugin 6개의 lib 모듈.
PLUGIN_CORES = ("safe-write-core.cjs", "stall-watchdog-core.cjs")

# node 스크립트 머리 — 모든 Python 실행(spawnSync)을 기록한 뒤 실제로 실행한다. core 를 require 하기
# 전에 바꿔야 plugin 이 만든 판정 경계도 이 함수를 쓴다.
SPAWN_SPY = r"""
const childProcess = require("node:child_process");
const realSpawn = childProcess.spawnSync;
const spawned = [];
childProcess.spawnSync = (command, args, options) => {
  spawned.push({command, args: [...args], input: options && typeof options.input === "string" ? options.input : null});
  return realSpawn(command, args, options);
};
const sdk = (sessions = {}) => ({session: {get: async ({path: {id}}) => ({data: sessions[id] || {id}})},
  tui: {showToast: async () => {}}});
async function observe(hooks, sessionID, input) {
  await hooks.event({event: {type: "message.updated", properties: {info: {sessionID, role: "assistant",
    tokens: {input}}}}});
}
async function deliver(hooks, sessionID) {
  const output = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  return output.system;
}
const requestOf = (launch) => JSON.parse(launch.input);
"""


def _project(tmp_path: Path) -> Path:
    """실제 엔진·lib 사본·OpenCode 압축 한도 기록·원칙 규칙을 갖춘 설치 형상 루트."""
    root = _make_ctx_guard_project(tmp_path)
    for name in PLUGIN_CORES:
        shutil.copyfile(CORE_DIR / name, root / ".opencode" / "lib" / name)
    install_ctx_record(root, harness="opencode")
    wiki = root / ".project_manager" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "pm_principles.md").write_text(RULES, encoding="utf-8")
    return root


def _node(root: Path, script: str, *args: str) -> dict:
    result = subprocess.run([NODE, "-e", SPAWN_SPY + script, str(root), *args],
                            cwd=root / ".opencode" / "lib", capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _markers(root: Path) -> list[str]:
    directory = root / ".project_manager" / ".local" / "ctx-stop"
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def _guidance(band: str, used: int) -> str:
    return ctx_recovery.build_ctx_guard_guidance(band, used_pct=used, precompact_at_pct=80)


def _hook_tool(root: Path, payload) -> subprocess.CompletedProcess:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return subprocess.run([sys.executable, str(root / ".project_manager" / "tools" / "board.py"), "hook-tool",
                           "--session", "ROOT"], input=body, capture_output=True, env=utf8_child_env(),
                          timeout=120)


def test_hook_tool_json_contract_and_failures(tmp_path):
    """8키 요청·6키 응답·ctx 11키·UTF-8/LF·rc 와 형식 오류·설정 오류·거부 우선, JS 소비자의 형식 검사."""
    root = _project(tmp_path)
    context = {"session": "native", "tokens": 156_000, "model": None, "main_thread": True, "boundary": None}
    request = {"version": 1, "cwd": str(root), "tool_name": "bash", "tool_input": {"command": "git status"},
               "shell_command": "git status", "main_thread": True, "context": context,
               "recall": {"session": "native", "on": "shell", "text": "git status", "main_thread": True}}
    ok = _hook_tool(root, request)
    assert ok.returncode == 0, ok.stderr.decode("utf-8", "replace")
    assert ok.stdout.endswith(b"\n") and ok.stdout.count(b"\n") == 1
    response = json.loads(ok.stdout.decode("utf-8"))
    assert set(response) == RESPONSE_KEYS and set(response["context"]) == CONTEXT_KEYS
    assert (response["context"]["level"], response["context"]["emit"]) == ("nudge2", True)
    assert response["context"]["guidance"] == _guidance("nudge2", 78)
    assert response["recall"]["keys"] == ["shell:git\\s+status"]
    assert (response["context_error"], response["recall_error"]) == (None, None)

    wire_errors = [
        {**request, "version": True},
        {**request, "extra": 1},
        {**request, "shell_command": "git log"},
        {**request, "main_thread": "yes"},
        {**request, "context": {**context, "session": "wire", "tokens": True}},
        {**request, "context": {**context, "session": "wire", "x": 1}},
        {**request, "recall": {**request["recall"], "x": 1}},
        {key: value for key, value in request.items() if key != "context"},
        json.dumps({**request, "context": {**context, "session": "wire"}}).encode("utf-8").replace(
            b'"git status"', b'"git \xff status"', 1),
    ]
    for bad in wire_errors:
        refused = _hook_tool(root, bad)
        assert (refused.returncode, refused.stdout) == (2, b""), bad
        assert "[hook-tool] 입력 형식 오류" in refused.stderr.decode("utf-8")
    assert not [name for name in _markers(root) if name.startswith("wire.")]

    conf = root / ".project_manager" / "local.conf"
    conf_text = conf.read_text(encoding="utf-8")
    conf.write_text(conf_text + "ctx_window_tokens=12345\n", encoding="utf-8")
    legacy = json.loads(_hook_tool(root, {**request, "context": {**context, "session": "legacy"},
                                          "recall": None}).stdout)
    assert legacy["git"]["verdict"] == response["git"]["verdict"] and legacy["context"] is None
    assert legacy["context_error"].startswith("LegacyConfKeyError: ")
    conf.write_text(conf_text, encoding="utf-8")

    denied = json.loads(_hook_tool(root, {
        "version": 1, "cwd": str(root), "tool_name": "write",
        "tool_input": {"filePath": "notes.md", "content": f"{GRANT_MARK} git push\n"}, "shell_command": None, "main_thread": True,
        "context": {**context, "session": "deny"},
        "recall": {"session": "deny", "on": "edit", "text": "notes.md", "main_thread": True}}).stdout)
    assert denied["git"]["verdict"] == "deny"
    assert [denied[key] for key in ("context", "context_error", "recall", "recall_error")] == [None] * 4
    assert not [name for name in _markers(root) if name.startswith("deny.")]
    assert not (root / ".project_manager" / ".local" / "principle-recall" / "deny.json").exists()

    script = r"""
const assert = require("node:assert");
const m = require("./git-anchor-core.cjs");
const root = process.argv[1];
const request = JSON.parse(process.argv[2]);
const good = {version: 1, git: {verdict: "ok", cwd_identity: "non-repo", reason: "x"}, context: null,
  context_error: "IsADirectoryError: x", recall: null, recall_error: null};
const reply = (stdout) => () => ({status: 0, stdout, stderr: ""});
const cases = {
  malformed: reply("{bad\n"),
  noLF: reply(JSON.stringify(good)),
  twoLines: reply("진단\n" + JSON.stringify(good) + "\n"),
  missingKey: reply(JSON.stringify({...good, recall_error: undefined}) + "\n"),
  bothNull: reply(JSON.stringify({...good, context_error: null}) + "\n"),
  denyExtras: reply(JSON.stringify({...good, git: {verdict: "deny", cwd_identity: "slot", reason: "x"}}) + "\n"),
  rc: () => ({status: 3, stdout: "", stderr: "엔진 오류"}),
  nullResult: () => null,
  spawnError: () => ({status: null, error: new Error("spawn EACCES")}),
};
const failures = {};
for (const [name, spawnSync] of Object.entries(cases)) {
  try {
    m.runHookTool(root, "ROOT", request, {spawnSync});
    failures[name] = null;
  } catch (error) {
    failures[name] = error.message;
  }
}
assert.deepStrictEqual(m.runHookTool(root, "ROOT", request, {spawnSync: reply(JSON.stringify(good) + "\n")}), good);
const before = spawned.length;
const real = m.runHookTool(root, "ROOT", {...request, context: {...request.context, session: "js-real"}});
const launches = spawned.slice(before);
console.log(JSON.stringify({failures, level: real.context.level, launches: launches.map((launch) => launch.args)}));
"""
    result = _node(root, script, json.dumps({**request, "recall": None}))
    assert all(result["failures"].values()), result["failures"]
    assert "엔진 오류" in result["failures"]["rc"] and "spawn EACCES" in result["failures"]["spawnError"]
    assert result["level"] == "nudge2"
    assert len(result["launches"]) == 1, "실행기 선택에 버전 확인 process 를 띄우지 않는다"
    assert result["launches"][0][-3:] == ["hook-tool", "--session", "ROOT"]
    assert not any(arg.endswith("python_floor.py") for arg in result["launches"][0])


def test_registered_before_starts_python_once(tmp_path):
    """등록 6 plugin 의 before 전체를 도구 5종에 실행하면 관측 전후 모두 Python 은 정확히 한 번이다."""
    root = _project(tmp_path)
    (root / "README.md").write_text("a\n", encoding="utf-8")
    script = r"""
const assert = require("node:assert");
const path = require("node:path");
const root = process.argv[1];
const ctx = require("./ctx-guard-core.cjs");
const git = require("./git-anchor-core.cjs");
const principle = require("./principle-recall-core.cjs");
const delegate = require("./delegate-channel-core.cjs");
const safe = require("./safe-write-core.cjs");
const stall = require("./stall-watchdog-core.cjs");
const schema = new Proxy(function () {}, {get: () => schema, apply: () => schema});
const tool = Object.assign((definition) => definition, {schema});
(async () => {
  const client = sdk();
  const input = {client, directory: root, worktree: root};
  const plugins = [await ctx.CtxGuardPlugin(input), await git.GitAnchorPlugin(input),
    await principle.PrincipleRecallPlugin(input), await delegate.DelegateChannelPlugin(input),
    await safe.makeSafeWritePlugin(tool)(input), await stall.makeStallWatchdogPlugin(client)(input)];
  const befores = plugins.map((plugin) => plugin["tool.execute.before"]).filter(Boolean);
  const calls = {
    read: {filePath: path.join(root, "README.md")},
    bash: {command: "echo ok"},
    write: {filePath: path.join(root, "notes.md"), content: "hello"},
    edit: {filePath: path.join(root, "README.md"), oldString: "a", newString: "b"},
    apply_patch: {patchText: "*** Begin Patch\n*** Add File: notes2.md\n+x\n*** End Patch\n"},
  };
  const errors = [];
  async function runAll(phase) {
    const rows = {};
    for (const [toolName, args] of Object.entries(calls)) {
      const start = spawned.length;
      for (const before of befores) {
        try {
          await before({tool: toolName, sessionID: "ses_once", callID: "c"}, {args: {...args}});
        } catch (error) {
          errors.push(`${phase}/${toolName}: ${error.message}`);
        }
      }
      const launches = spawned.slice(start);
      rows[toolName] = {count: launches.length,
        floor: launches.some((launch) => launch.args.some((arg) => arg.endsWith("python_floor.py"))),
        context: launches.length ? requestOf(launches[0]).context : "none"};
    }
    return rows;
  }
  const beforeObservation = await runAll("before");
  await observe(plugins[0], "ses_once", 100000);
  const afterObservation = await runAll("after");
  console.log(JSON.stringify({befores: befores.length, errors, beforeObservation, afterObservation}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = _node(root, script)
    assert result["befores"] == 3  # ctx(판정 경계)·delegate(task 전용)·safe-write(native 크기 검사)
    assert result["errors"] == []
    for phase, expected_context in (("beforeObservation", None), ("afterObservation", 100_000)):
        for tool_name, row in result[phase].items():
            assert (row["count"], row["floor"]) == (1, False), (phase, tool_name, row)
            if expected_context is None:
                assert row["context"] is None, (phase, tool_name)
            else:
                assert row["context"] == {"session": "ses_once", "tokens": expected_context, "model": None,
                                          "main_thread": True, "boundary": None}, (phase, tool_name)


def test_native_context_policy_and_delivery(tmp_path):
    """엔진 판정 그대로 — nudge2·final 독립, 0 미재무장·양수 ok 재무장, 재시작·다른 process 억제, 불능 notice."""
    root = _project(tmp_path)
    script = r"""
const fs = require("node:fs");
const path = require("node:path");
const root = process.argv[1];
const ctx = require("./ctx-guard-core.cjs");
(async () => {
  const toasts = [];
  const client = {...sdk(), tui: {showToast: async (toast) => { toasts.push(toast.body.message); }}};
  const make = () => ctx.CtxGuardPlugin({client, directory: root});
  const hooks = await make();
  const sid = "ses_policy";
  const out = {};
  await observe(hooks, sid, 156000); out.nudge2 = await deliver(hooks, sid); out.nudge2Again = await deliver(hooks, sid);
  await observe(hooks, sid, 190000); out.final = await deliver(hooks, sid);
  await observe(hooks, sid, 0); await observe(hooks, sid, 190000); out.afterZero = await deliver(hooks, sid);
  await observe(hooks, sid, 4000); await observe(hooks, sid, 156000); out.afterOk = await deliver(hooks, sid);
  const restarted = await make();
  await observe(restarted, sid, 156000); out.restarted = await deliver(restarted, sid);
  fs.unlinkSync(path.join(root, ".project_manager", ".local", "ctx-budget.json"));
  await observe(hooks, "ses_notice", 150000); await observe(hooks, "ses_notice", 150000);
  out.notice = await deliver(hooks, "ses_notice");
  out.budgetToasts = toasts.filter((text) => text.startsWith("[ctx-budget]"));
  console.log(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    budget = (root / ".project_manager" / ".local" / "ctx-budget.json").read_bytes()
    result = _node(root, script)
    assert result["nudge2"] == [_guidance("nudge2", 78)] and result["nudge2Again"] == []
    assert result["final"] == [_guidance("final", 95)], "nudge2 발화가 final 을 소모했다"
    assert result["afterZero"] == [], "미측정 0 이 밴드를 재무장했다"
    assert result["afterOk"] == [_guidance("nudge2", 78)], "양수 ok 관측이 밴드를 재무장하지 않았다"
    assert result["restarted"] == [], "plugin 재시작이 같은 세션 밴드를 다시 냈다"
    assert result["notice"] == []
    assert len(result["budgetToasts"]) == 1 and "`pm_config.py apply`" in result["budgetToasts"][0]

    (root / ".project_manager" / ".local" / "ctx-budget.json").write_bytes(budget)
    other = _node(root, r"""
const ctx = require("./ctx-guard-core.cjs");
(async () => {
  const hooks = await ctx.CtxGuardPlugin({client: sdk(), directory: process.argv[1]});
  await observe(hooks, "ses_policy", 156000);
  console.log(JSON.stringify({delivered: await deliver(hooks, "ses_policy")}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")
    assert other["delivered"] == [], "다른 process 가 같은 세션 밴드를 다시 냈다"


def test_epoch_and_session_ownership(tmp_path):
    """늦은 SDK·압축 교차와 event-local SID, 자식 면제, git 최상위 SID·recall native SID, SDK 실패 경고."""
    root = _project(tmp_path)
    script = r"""
const ctx = require("./ctx-guard-core.cjs");
const root = process.argv[1];
(async () => {
  const out = {};
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const late = await ctx.CtxGuardPlugin({directory: root, client: {
    session: {get: async ({path: {id}}) => { await gate; return {data: {id}}; }}, tui: {showToast: async () => {}}}});
  let start = spawned.length;
  const message = observe(late, "ses_late", 156000);
  await new Promise((resolve) => setImmediate(resolve));
  const compaction = late.event({event: {type: "session.compacted", properties: {sessionID: "ses_late"}}});
  await new Promise((resolve) => setImmediate(resolve));
  release();
  await message;
  await compaction;
  out.lateContexts = spawned.slice(start).filter((launch) => launch.args.includes("hook-context"))
    .map((launch) => requestOf(launch));
  out.lateDelivered = await deliver(late, "ses_late");

  const pair = await ctx.CtxGuardPlugin({directory: root, client: sdk()});
  await Promise.all([observe(pair, "ses_a", 156000), observe(pair, "ses_b", 150000)]);
  out.pair = {a: await deliver(pair, "ses_a"), b: await deliver(pair, "ses_b")};

  const family = await ctx.CtxGuardPlugin({directory: root, client: sdk({
    ses_child: {id: "ses_child", parentID: "ses_parent"}, ses_parent: {id: "ses_parent"}})});
  start = spawned.length;
  await observe(family, "ses_child", 190000);
  await family.event({event: {type: "session.compacted", properties: {sessionID: "ses_child"}}});
  out.childDelivered = await deliver(family, "ses_child");
  out.childLaunches = spawned.length - start;
  start = spawned.length;
  await family["tool.execute.before"]({tool: "bash", sessionID: "ses_child"}, {args: {command: "git status"}});
  const childTool = spawned.slice(start);
  out.childTool = {count: childTool.length, session: childTool[0].args.slice(-1)[0], request: requestOf(childTool[0])};

  const failures = {};
  const chain = {};
  for (let index = 0; index < 40; index += 1) chain[`deep_${index}`] = {id: `deep_${index}`, parentID: `deep_${index + 1}`};
  const clients = {
    error: {session: {get: async () => { throw new Error("SDK down"); }}},
    cycle: sdk({ses_x: {id: "ses_x", parentID: "ses_y"}, ses_y: {id: "ses_y", parentID: "ses_x"}}),
    deep: sdk(chain),
  };
  const sids = {error: "ses_err", cycle: "ses_x", deep: "deep_0"};
  for (const [name, client] of Object.entries(clients)) {
    const hooks = await ctx.CtxGuardPlugin({directory: root, client: {...client, tui: {showToast: async () => {}}}});
    start = spawned.length;
    await hooks["tool.execute.before"]({tool: "bash", sessionID: sids[name]}, {args: {command: "git status"}});
    await observe(hooks, sids[name], 190000);
    failures[name] = {launches: spawned.length - start, delivered: await deliver(hooks, sids[name])};
  }
  out.failures = failures;
  console.log(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = _node(root, script)
    late = result["lateContexts"]
    assert len(late) == 1 and late[0]["tokens"] == 0 and late[0]["boundary"].endswith("-ses_late"), late
    assert len(result["lateDelivered"]) == 1 and result["lateDelivered"][0].startswith("## PM 정체성")
    assert result["pair"] == {"a": [_guidance("nudge2", 78)], "b": [_guidance("nudge", 75)]}
    assert result["childDelivered"] == [] and result["childLaunches"] == 0
    assert not [name for name in _markers(root) if "ses_child" in name]
    assert result["childTool"]["count"] == 1 and result["childTool"]["session"] == "ses_parent"
    assert result["childTool"]["request"]["context"] is None
    assert result["childTool"]["request"]["recall"] == {"session": "ses_child", "on": "shell", "text": "git status",
                                                        "main_thread": False}
    expected = {"error": "SDK down", "cycle": "세션 부모 고리", "deep": "세션 부모 역조회 32개 초과"}
    for name, needle in expected.items():
        failure = result["failures"][name]
        assert failure["launches"] == 0, (name, failure)
        assert len(failure["delivered"]) == 1 and needle in failure["delivered"][0], (name, failure)
        assert "[ctx-nudge" not in failure["delivered"][0], "SDK 불능을 main 으로 처리했다"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unfiltered_inputs_reach_central_guard(tmp_path, monkeypatch):
    """Claude·Codex native 진입과 OpenCode 경계는 셸 문자열을 거르지 않고 원문을 중앙 판정에 넘긴다."""
    root = _project(tmp_path)
    calls = []

    def judge_git_anchor_command(home, session, cwd, command, *, main_thread, grants=False):
        calls.append((home, cwd, command))
        return {"verdict": "ok", "cwd_identity": "non-repo", "reason": "fixture"}

    monkeypatch.setattr(cli_hooks, "judge_git_anchor_command", judge_git_anchor_command)
    claude = _load(TEMPLATES / "claude_code" / ".claude" / "pm_orch_claude.py", "s10_boundary_claude")
    codex = _load(TEMPLATES / "codex" / ".codex" / "pm_orch_codex.py", "s10_boundary_codex")
    for command in UNFILTERED:
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(root), "session_id": "S",
                   "tool_input": {"command": command}}
        assert claude.git_anchor_hook_evaluate(payload, root) is None
        assert codex.git_anchor_hook_evaluate(payload, root) == {}
    home = cli_hooks.adapter_home(root)
    assert calls == [(home, str(root), command) for command in UNFILTERED for _harness in ("claude", "codex")]

    script = r"""
const assert = require("node:assert");
const ctx = require("./ctx-guard-core.cjs");
const [root, commandsText, grantMark] = process.argv.slice(1);
(async () => {
  const hooks = await ctx.CtxGuardPlugin({client: sdk(), directory: root});
  const shell = [];
  for (const command of JSON.parse(commandsText)) {
    const start = spawned.length;
    await hooks["tool.execute.before"]({tool: "bash", sessionID: "ses_raw"}, {args: {command}});
    const launches = spawned.slice(start);
    shell.push({count: launches.length, shell: requestOf(launches[0]).shell_command});
  }
  const start = spawned.length;
  let denied = null;
  try {
    await hooks["tool.execute.before"]({tool: "task", sessionID: "ses_raw"},
      {args: {subagent_type: "developer", prompt: grantMark.replace(" ", "\t") + " git status"}});
  } catch (error) {
    denied = error.message;
  }
  console.log(JSON.stringify({shell, denied, taskLaunches: spawned.length - start}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = _node(root, script, json.dumps(UNFILTERED), GRANT_MARK)
    assert result["shell"] == [{"count": 1, "shell": command} for command in UNFILTERED]
    assert result["taskLaunches"] == 1
    assert result["denied"] and result["denied"].startswith("[git-anchor/deny]")
    assert "허용 줄·허용 기록은 사용자 입력으로만 만든다" in result["denied"]


def test_snapshot_generation_and_receipt_preserved(tmp_path):
    """압축 1회 = ctx 1 + snapshot 2 + checkpoint 2 실행, 같은 경계 ID, CRLF 원문, generation·receipt 보존."""
    root = _project(tmp_path)
    payload = "## PM 정체성 (compaction 복구)\r\n- task: main\r\n"
    calls_file = root / "checkpoint-calls.jsonl"
    (root / ".project_manager" / "tools" / "pm_log.py").write_text(
        "import json, sys\n"
        "if 'snapshot' in sys.argv:\n"
        f"    sys.stdout.buffer.write({payload.encode('utf-8')!r})\n"
        "elif 'checkpoint' in sys.argv:\n"
        f"    open({str(calls_file)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8", newline="\n")
    script = r"""
const fs = require("node:fs");
const path = require("node:path");
const ctx = require("./ctx-guard-core.cjs");
const root = process.argv[1];
const markers = path.join(root, ".project_manager", ".local", "ctx-stop");
const listing = (prefix) => fs.readdirSync(markers).filter((name) => name.startsWith(prefix)).sort();
(async () => {
  const sid = "ses_snap";
  const hooks = await ctx.CtxGuardPlugin({client: sdk(), directory: root});
  const start = spawned.length;
  await hooks.event({event: {type: "session.compacted", properties: {sessionID: sid}}});
  const launches = spawned.slice(start);
  const contextLaunch = launches.find((launch) => launch.args.includes("hook-context"));
  const own = listing(`compact-snapshot.${sid}.`);
  const foreign = `compact-snapshot.${sid}.29991231T000000000Z-ffffffff-ffff-4fff-8fff-ffffffffffff`;
  fs.writeFileSync(path.join(markers, foreign), "foreign\r\n");
  const first = await deliver(hooks, sid);
  const afterFirst = listing(`compact-snapshot.${sid}.`);
  const restarted = await ctx.CtxGuardPlugin({client: sdk(), directory: root});
  const second = await deliver(restarted, sid);
  const third = await deliver(restarted, sid);
  console.log(JSON.stringify({
    launches: launches.map((launch) => launch.args.slice(-1)[0].endsWith("python_floor.py") ? "floor"
      : launch.args.includes("hook-context") ? "hook-context" : launch.args.includes("snapshot") ? "snapshot"
      : launch.args.includes("checkpoint") ? "checkpoint" : launch.args.join(" ")),
    boundary: requestOf(contextLaunch).boundary, own, foreign, first, afterFirst, second, third,
    receipts: listing(`compact-snapshot-receipt.${sid}.`),
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = _node(root, script)
    assert result["launches"] == ["hook-context", "floor", "snapshot", "floor", "checkpoint"]
    boundary = result["boundary"]
    assert boundary.startswith("opencode-") and boundary.endswith("-ses_snap")
    checkpoint = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()]
    assert checkpoint == [["checkpoint", "--trigger", "compaction", "--phase", "post", "--session-id", "ses_snap",
                           "--boundary-id", boundary]]
    assert len(result["own"]) == 1
    own_marker = root / ".project_manager" / ".local" / "ctx-stop" / result["own"][0]
    assert result["first"] == [payload], "snapshot 원문 bytes 가 바뀌었다"
    assert result["afterFirst"] == [result["foreign"]] and not own_marker.exists()
    assert result["second"] == ["foreign\r\n"] and result["third"] == []
    own_generation = result["own"][0].split(".", 2)[2]
    foreign_generation = result["foreign"].split(".", 2)[2]
    assert result["receipts"] == sorted([f"compact-snapshot-receipt.ses_snap.{own_generation}",
                                         f"compact-snapshot-receipt.ses_snap.{foreign_generation}"])
