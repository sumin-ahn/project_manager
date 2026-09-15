"""슬롯 쓰기 가드 — 세션이 연 task 가 잡은 슬롯에만 쓴다.

판정 단일 진실은 board(`judge_tool_input`·`judge_git_anchor_command`)다. 이 파일은 파일 쓰기·편집
도구와 패치 도구의 대상 판정, 셸 명령의 슬롯 경고, 부트스트랩 세션 기록, 위임 task 환경변수를 본다.
git 쓰기 주인 판정은 실제 linked worktree 가 필요해 `test_git_anchor_guard.py` 에 있다.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOARD_PY = TOOLS / "board.py"
CLAUDE_DRIVER = REPO / "templates" / "claude_code" / ".claude" / "pm_orch_claude.py"
CODEX_DISPATCHER = REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py"
OPEN_CORE = REPO / "templates" / "opencode" / ".opencode" / "lib" / "git-anchor-core.cjs"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def board():
    return _load("slot_write_guard_board", BOARD_PY)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """PM 홈 장부 — 슬롯 1(task alpha) · 2(빈 슬롯) · 3(readonly) · 4(닫힌 task beta)."""
    monkeypatch.delenv("PM_DELEGATE_TASK", raising=False)
    root = tmp_path / "pm-home"
    for number in (1, 2, 3, 4):
        (root / "work" / f"app_{number}").mkdir(parents=True)
    ledger = root / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({
        "leases": [
            {"slot": "work/app_1", "repo": "app", "session": "alpha", "state": "leased"},
            {"slot": "work/app_2", "repo": "app", "session": "", "state": "idle"},
            {"slot": "work/app_3", "repo": "app", "session": "", "state": "leased", "role": "readonly"},
            {"slot": "work/app_4", "repo": "app", "session": "beta", "state": "leased"},
        ],
        "tasks": [{"name": "alpha", "session": "S-alpha"}, {"name": "beta", "session": ""}],
    }) + "\n", encoding="utf-8")
    return root


def _action(board, case: str) -> str:
    return board._GIT_GUARD_ACTION_LINES[case]


@pytest.mark.parametrize(
    ("tool", "key"),
    [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path"),
     ("write", "filePath"), ("edit", "filePath")],
)
def test_write_tools_pass_only_in_the_slot_of_the_sessions_task(board, home, tool, key):
    def judge(session: str, target: Path) -> dict:
        return board.judge_tool_input(home, session, str(home), tool, {key: str(target)})

    assert judge("S-alpha", home / "work" / "app_1" / "src" / "a.py")["verdict"] == "ok"
    for number in (2, 3, 4):
        got = judge("S-alpha", home / "work" / f"app_{number}" / "a.py")
        lines = got["reason"].splitlines()
        assert got["verdict"] == "deny", number
        assert lines[:2] == [board.GIT_GUARD_STOP_LINE, _action(board, board._GIT_GUARD_CASE_SLOT_NOT_OWNED)]
    stranger = judge("S-other", home / "work" / "app_1" / "a.py")
    assert stranger["verdict"] == "deny"
    assert stranger["reason"].splitlines()[1] == _action(board, board._GIT_GUARD_CASE_SLOT_NO_TASK)
    # 슬롯 밖(PM 홈 기록·다른 폴더)은 판정하지 않는다.
    assert judge("S-other", home / ".project_manager" / "wiki" / "domain" / "x.md")["verdict"] == "ok"
    assert judge("", home.parent / "elsewhere" / "a.py")["verdict"] == "ok"


def test_relative_write_target_resolves_from_the_tool_cwd(board, home):
    slot = home / "work" / "app_1"
    assert board.judge_tool_input(home, "S-alpha", str(slot), "Write", {"file_path": "a.py"})["verdict"] == "ok"
    escaped = board.judge_tool_input(home, "S-alpha", str(slot), "Write", {"file_path": "../app_2/a.py"})
    assert escaped["verdict"] == "deny"


def test_patch_tools_judge_every_file_header(board, home):
    slot_4 = home / "work" / "app_4"
    codex_patch = (
        "*** Begin Patch\n*** Update File: notes.md\n@@\n-a\n+b\n"
        f"*** Add File: {slot_4}/x.py\n+print(1)\n*** End Patch\n"
    )
    got = board.judge_tool_input(home, "S-alpha", str(home), "apply_patch", {"command": codex_patch})
    assert got["verdict"] == "deny"
    assert "work/app_4" in got["reason"] and "notes.md" not in got["reason"]
    moved = "*** Begin Patch\n*** Update File: a.py\n*** Move to: ../app_2/a.py\n*** End Patch\n"
    slot_1 = home / "work" / "app_1"
    assert board.judge_tool_input(
        home, "S-alpha", str(slot_1), "apply_patch", {"patchText": moved},
    )["verdict"] == "deny"
    inside = "*** Begin Patch\n*** Delete File: old.py\n*** End Patch\n"
    assert board.judge_tool_input(
        home, "S-alpha", str(slot_1), "apply_patch", {"patchText": inside},
    )["verdict"] == "ok"


def test_delegated_harness_writes_as_the_open_task_it_was_given(board, home, monkeypatch):
    slot_1 = str(home / "work" / "app_1" / "a.py")
    slot_4 = str(home / "work" / "app_4" / "a.py")
    monkeypatch.setenv("PM_DELEGATE_TASK", "alpha")
    assert board.judge_tool_input(home, "child", str(home), "Write", {"file_path": slot_1})["verdict"] == "ok"
    assert board.judge_tool_input(home, "child", str(home), "Write", {"file_path": slot_4})["verdict"] == "deny"
    # 닫힌 task 이름은 위임 근거가 되지 않는다.
    monkeypatch.setenv("PM_DELEGATE_TASK", "beta")
    closed = board.judge_tool_input(home, "child", str(home), "Write", {"file_path": slot_4})
    assert closed["verdict"] == "deny"
    assert closed["reason"].splitlines()[1] == _action(board, board._GIT_GUARD_CASE_SLOT_NO_TASK)


def test_shell_command_naming_a_foreign_slot_warns_once_per_slot(board, home):
    slot_4 = home / "work" / "app_4"
    got = board.judge_git_anchor_command(
        home, "S-alpha", str(home), f"sed -i s/a/b/ {slot_4}/a.txt {slot_4}/b.txt",
    )
    assert got["verdict"] == "warn"
    assert got["reason"].count("work/app_4") == 1
    own = board.judge_git_anchor_command(
        home, "S-alpha", str(home / "work" / "app_1"), f"sed -i s/a/b/ {home}/work/app_1/a.txt",
    )
    assert own["verdict"] == "ok"


def test_corrupt_ledger_is_raised_not_read_as_empty(board, home):
    (home / ".project_manager" / ".local" / "worktree-leases.json").write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        board.judge_tool_input(home, "S-alpha", str(home), "Write", {"file_path": str(home / "work" / "app_1" / "a.py")})


@pytest.fixture
def engine_home(home):
    shutil.copytree(TOOLS, home / ".project_manager" / "tools")
    return home


def test_bootstrap_shell_call_records_the_session_for_its_engine_copy(board, engine_home):
    claims = engine_home / ".project_manager" / ".local" / "task-session-claims"
    got = board.judge_git_anchor_command(
        engine_home, "S-new", str(engine_home), "python3 .project_manager/tools/pm_bootstrap.py --task gamma",
    )
    assert got["verdict"] != "deny"
    assert json.loads((claims / "gamma.json").read_text(encoding="utf-8")) == {"task": "gamma", "session": "S-new"}
    board.judge_git_anchor_command(
        engine_home, "S-new", str(engine_home), f"python3 {engine_home}/.project_manager/tools/pm_bootstrap.py --task=delta",
    )
    assert (claims / "delta.json").is_file()
    # 인자를 정적으로 못 정하거나 세션 ID 가 없으면 남기지 않는다 — 부트스트랩이 기록 없음으로 멈춘다.
    board.judge_git_anchor_command(
        engine_home, "S-new", str(engine_home), 'python3 .project_manager/tools/pm_bootstrap.py --task "$NAME"',
    )
    board.judge_git_anchor_command(
        engine_home, "", str(engine_home), "python3 .project_manager/tools/pm_bootstrap.py --task epsilon",
    )
    assert sorted(path.name for path in claims.iterdir()) == ["delta.json", "gamma.json"]


def test_denied_bootstrap_command_records_nothing(board, engine_home):
    marker = "git " + "허용" + ":"
    got = board.judge_git_anchor_command(
        engine_home, "S-new", str(engine_home),
        f"python3 .project_manager/tools/pm_bootstrap.py --task gamma; echo '{marker} x'",
    )
    assert got["verdict"] == "deny"
    assert not (engine_home / ".project_manager" / ".local" / "task-session-claims").exists()


def test_delegate_env_names_the_task_that_holds_the_slot(board, home, monkeypatch):
    pm_delegate = _load("slot_write_guard_pm_delegate", TOOLS / "pm_delegate.py")
    monkeypatch.setattr(board, "REPO", home)
    monkeypatch.setattr(pm_delegate, "_load_board", lambda: board)
    assert pm_delegate._delegate_task_env(home / "work" / "app_1") == {"PM_DELEGATE_TASK": "alpha"}
    assert pm_delegate._delegate_task_env(home / "work" / "app_4" / "src") == {"PM_DELEGATE_TASK": "beta"}
    assert pm_delegate._delegate_task_env(home / "work" / "app_2") == {}
    assert pm_delegate._delegate_task_env(home) == {}


def test_claude_and_codex_hooks_route_write_tools_to_the_slot_judgment(board, home, monkeypatch):
    claude = _load("slot_write_guard_claude", CLAUDE_DRIVER)
    monkeypatch.setattr(claude, "_load_board", lambda _root: board)
    target = str(home / "work" / "app_2" / "a.py")
    output = claude.git_anchor_hook_evaluate({
        "session_id": "S-alpha", "hook_event_name": "PreToolUse", "tool_name": "Write",
        "cwd": str(home), "tool_input": {"file_path": target, "content": "x"},
    }, home)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert _action(board, board._GIT_GUARD_CASE_SLOT_NOT_OWNED) in output["permissionDecisionReason"]

    codex = _load("slot_write_guard_codex", CODEX_DISPATCHER)
    monkeypatch.setattr(codex, "_load_board", lambda _root: board)
    patch = f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch\n"
    envelope = codex.git_anchor_hook_evaluate({
        "session_id": "S-alpha", "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
        "cwd": str(home), "tool_input": {"command": patch},
    }, home)
    assert envelope["decision"] == "block"
    own = f"*** Begin Patch\n*** Add File: {home}/work/app_1/a.py\n+x\n*** End Patch\n"
    assert codex.git_anchor_hook_evaluate({
        "session_id": "S-alpha", "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
        "cwd": str(home), "tool_input": {"command": own},
    }, home) == {}


def test_opencode_write_tools_reach_the_board_with_session_and_cwd():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    script = r'''
const assert = require("node:assert");
const m = require("./git-anchor-core.cjs");
for (const tool of ["write", "edit", "apply_patch"]) assert.strictEqual(m.toolInputNeedsJudgment(tool, {}), true);
assert.strictEqual(m.toolInputNeedsJudgment("read", {filePath: "/x"}), false);
const seen = [];
const spy = (py, argv, opts) => {
  seen.push([argv.slice(1), opts.input]);
  return {status: 0, stdout: JSON.stringify({verdict: "deny", cwd_identity: "slot", reason: "x"}) + "\n", stderr: ""};
};
const got = m.judgeToolInput("/engine", "write", {filePath: "/h/work/app_2/a.py"}, {session: "S", cwd: "/h", spawnSync: spy});
assert.strictEqual(got.verdict, "deny");
assert.deepStrictEqual(seen[0], [["git-tool-input", "--session", "S", "--cwd", "/h", "--tool", "write"], JSON.stringify({filePath: "/h/work/app_2/a.py"})]);
console.log("OPENCODE_SLOT_WRITE_OK");
'''
    result = subprocess.run(
        [node, "-e", script], cwd=OPEN_CORE.parent, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert "OPENCODE_SLOT_WRITE_OK" in result.stdout


def test_opencode_child_session_is_judged_as_its_root_session():
    """native task 자식 세션(parentID 있음)은 task 를 연 최상위 세션 ID 로 판정받는다."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    script = r'''
const assert = require("node:assert");
const m = require("./git-anchor-core.cjs");
(async () => {
  const sessions = {C: {id: "C", parentID: "B"}, B: {id: "B", parentID: "A"}, A: {id: "A"}};
  const lookups = [];
  const judged = [];
  const hooks = await m.makeGitAnchorPlugin(
    (_root, cwd, command, options) => { judged.push(options.session); return {verdict: "ok", cwd_identity: "slot", reason: "x"}; },
    (_root, tool, args, options) => { judged.push(options.session); return {verdict: "ok", cwd_identity: "slot", reason: "x"}; },
  )({directory: "/h", worktree: "/h", client: {
    tui: {showToast: async () => {}},
    session: {get: async ({path}) => { lookups.push(path.id); return {data: sessions[path.id]}; }},
  }});
  await hooks["tool.execute.before"]({tool: "write", sessionID: "C"}, {args: {filePath: "/h/work/app_1/a.py"}});
  await hooks["tool.execute.before"]({tool: "bash", sessionID: "C"}, {args: {command: "git commit -m x"}});
  assert.deepStrictEqual(judged, ["A", "A"]);
  assert.deepStrictEqual(lookups, ["C", "B", "A"]);

  const toasts = [];
  const failing = await m.makeGitAnchorPlugin(
    () => { throw new Error("판정까지 오면 안 됨"); },
    () => { throw new Error("판정까지 오면 안 됨"); },
  )({directory: "/h", worktree: "/h", client: {
    tui: {showToast: async (value) => toasts.push(value)},
    session: {get: async () => { throw new Error("SDK down"); }},
  }});
  await failing["tool.execute.before"]({tool: "write", sessionID: "C"}, {args: {filePath: "/h/work/app_1/a.py"}});
  const context = {system: []};
  await failing["experimental.chat.system.transform"]({sessionID: "C"}, context);
  assert.match(context.system[0], /판정 인프라 예외\(SDK down\)/);
  console.log("OPENCODE_ROOT_SESSION_OK");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", script], cwd=OPEN_CORE.parent, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert "OPENCODE_ROOT_SESSION_OK" in result.stdout
