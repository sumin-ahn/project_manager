import importlib.util
import json
from pathlib import Path

import pytest

from pm_engine.infra import areas_store, branch_store, pm_paths, pool_store, task_store
from pm_engine.app import task_lifecycle


def _seed(root):
    home = pm_paths.home_at(root)
    with pool_store._lease_lock(home):
        pool_store._write_ledger_raw(home, {
            "leases": [], "tasks": [{"name": "job", "open": True}],
            "unknown": {"keep": 1},
        })
    return home


def _tool(name):
    tools = Path(__file__).resolve().parents[1] / ".project_manager/tools"
    spec = importlib.util.spec_from_file_location("s8b_probe_" + name, tools / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_two_home_ledger_and_todo_isolation(tmp_path):
    left = _seed(tmp_path / "left")
    right = _seed(tmp_path / "right")
    task_lifecycle.add_task_todo(left, "job", "left task")
    assert [row["text"] for row in task_store.read_task_todos(left, "job")] == ["left task"]
    assert task_store.read_task_todos(right, "job") == []
    task_store.record_task_session_claim(left, "job", "sid")
    with pytest.raises(task_store.NoTaskSessionClaim):
        task_store.take_task_session_claim(right, "job")
    assert task_store.take_task_session_claim(left, "job") == "sid"
    with pool_store._lease_lock(left):
        pool_store._write_ledger(left, [])
        assert pool_store._read_ledger_raw_strict(left)["unknown"] == {"keep": 1}


def test_missing_todo_has_no_writes(tmp_path):
    home = pm_paths.home_at(tmp_path)
    assert task_store.read_task_todos(home, "job") == []
    assert not home.local_dir.exists()


def test_corrupt_todo_and_strict_ledger_raise(tmp_path):
    home = _seed(tmp_path)
    todo = pm_paths.task_todo_file(home, "job")
    todo.parent.mkdir(parents=True)
    todo.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        task_store.read_task_todos(home, "job")
    pm_paths.leases_file(home).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        pool_store._read_ledger_raw_strict(home)


def test_board_todo_uses_execution_code(tmp_path):
    home = _seed(tmp_path)
    task_lifecycle.add_task_todo(home, "job", "local data")
    foreign = tmp_path / ".project_manager/tools/worktree_pool.py"
    foreign.parent.mkdir()
    foreign.write_text("raise AssertionError('foreign code executed')", encoding="utf-8")
    assert _tool("board")._open_todo_count(tmp_path, "job") == 1
    foreign.unlink()
    assert _tool("board")._open_todo_count(tmp_path, "job") == 1


def test_snapshot_todo_uses_target_home_without_flat_engine(tmp_path):
    home = _seed(tmp_path)
    task_lifecycle.add_task_todo(home, "job", "visible todo")
    assert "visible todo" in _tool("pm_log")._todo_section(tmp_path, "job")


def test_registered_protection_overrides_default(tmp_path):
    home = pm_paths.home_at(tmp_path)
    assert branch_store._resolve_protected(home, "repo") == ["main", "master", "develop"]
    path = pm_paths.areas_file(home)
    path.parent.mkdir(parents=True)
    path.write_text("| repo | protected |\n|---|---|\n| repo | release |\n", encoding="utf-8")
    assert areas_store._repo_protected(home, "repo") == ["release"]
    assert branch_store._resolve_protected(home, "repo") == ["release"]
