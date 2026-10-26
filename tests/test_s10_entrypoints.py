"""board·pm-config 잔여 CLI 와 어댑터 git 진입의 패키지 소유 계약.

어댑터 공개 진입(`cli.hooks`)의 판정·허용 기록, `pm-config init` 의 board 명령 직접 위임,
board 명령 dispatch 의 읽기 Home 선택과 쓰기 가드 순서를 실제 소유 모듈로 검증한다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest

from pm_engine.cli import board_cmd, board_dispatch, board_init, board_list, board_ticket, config_cmd
from pm_engine.cli import hooks
from pm_engine.domain import git_policy
from pm_engine.infra import pm_paths

_GRANT_DIR = Path(".project_manager") / ".local" / git_policy.GIT_GRANT_DIR_NAME


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")


def _seed_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _grant_files(home: Path) -> list[Path]:
    directory = home / _GRANT_DIR
    return sorted(directory.iterdir()) if directory.is_dir() else []


@pytest.fixture
def topology(tmp_path):
    """PM 홈(실 board) + 공용 저장소의 등록 linked slot. 슬롯 주인 task t1 의 세션은 "S"."""
    home = tmp_path / "pm-home"
    _seed_repo(home)
    ticket = home / ".project_manager" / "wiki" / "tickets" / "done" / "T-0001-x.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("---\nid: T-0001\n---\n", encoding="utf-8")
    source = tmp_path / "product-source"
    _seed_repo(source)
    bare = home / ".repos" / "product.git"
    bare.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    slot = home / "work" / "product_1"
    slot.parent.mkdir(parents=True)
    _git(bare, "worktree", "add", "-q", str(slot))
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"leases": [{"slot": "work/product_1", "repo": "product",
                                               "state": "leased", "session": "t1"}],
                                  "tasks": [{"name": "t1", "session": "S"}]}) + "\n", encoding="utf-8")
    return home, slot


def test_public_git_api_preserves_verdict_and_grant_use(topology):
    home, slot = topology
    anchor = hooks.adapter_home(home)
    assert hooks.adapter_home(str(home)) == anchor
    assert hooks.judge_git_anchor_command(anchor, "S", str(home), "echo ok")["verdict"] == "ok"

    denied = hooks.judge_git_anchor_command(anchor, "S", str(slot), "git branch -D main", grants=True)
    assert denied["verdict"] == "deny" and denied["code"] == git_policy.GIT_DESTRUCTIVE_DENY_CODE
    assert hooks.record_git_grants(anchor, "S", str(slot), "git 허용: git branch -D main") == [
        "git branch -D main"
    ]
    assert hooks.judge_git_anchor_command(anchor, "S", str(slot), "git branch -D main", grants=True)[
        "verdict"
    ] == "warn"
    assert hooks.judge_git_anchor_command(anchor, "S", str(slot), "git branch -D main", grants=True)[
        "verdict"
    ] == "deny"
    assert _grant_files(home) == []

    forged = hooks.judge_tool_input(
        anchor, "S", str(home), "Write",
        {"file_path": str(home / _GRANT_DIR / "x.json"), "content": "{}"},
    )
    assert forged["verdict"] == "deny"
    assert hooks.judge_tool_input(
        anchor, "S", str(home), "Write", {"file_path": str(home / "notes.txt"), "content": "x"},
    )["verdict"] != "deny"


def test_invalid_grant_batch_has_no_partial_write(topology):
    home, slot = topology
    anchor = hooks.adapter_home(home)
    prompt = "git 허용: git branch -D ok\ngit 허용: git branch -D a && git branch -D b\n"
    with pytest.raises(git_policy.GitGrantShapeError):
        hooks.record_git_grants(anchor, "S", str(slot), prompt)
    assert _grant_files(home) == []
    with pytest.raises(ValueError):
        hooks.record_git_grants(anchor, "", str(slot), "git 허용: git branch -D ok")
    assert _grant_files(home) == []


def test_distinct_homes_do_not_cross_write_grants(tmp_path):
    first, second = tmp_path / "alpha", tmp_path / "beta"
    first.mkdir()
    second.mkdir()
    assert hooks.record_git_grants(hooks.adapter_home(first), "S", str(first), "git 허용: git status") == [
        "git status"
    ]
    assert len(_grant_files(first)) == 1 and _grant_files(second) == []
    hooks.record_git_grants(hooks.adapter_home(second), "S", str(second), "git 허용: git log")
    assert len(_grant_files(first)) == 1 and len(_grant_files(second)) == 1
    recorded = json.loads(_grant_files(second)[0].read_text(encoding="utf-8"))
    assert recorded["argv"] == ["git", "log"]


def test_config_init_preserves_raw_tokens_and_prog(tmp_path, monkeypatch, capsys):
    home = pm_paths.home_at(tmp_path)
    seen = []

    def record(actual, args):
        seen.append((actual, args.prefix, args.area, args.owner))
        return 7

    monkeypatch.setattr(board_init, "cmd_init", record)
    monkeypatch.setattr(board_dispatch, "_guard_worktree_misanchor", lambda _home, _action: False)
    argv = ["init", "--prefix", "X", "--area", "a", "--owner", "me"]
    assert config_cmd._main(home, argv, engine_rev="v-test") == 7
    assert seen == [(home, "X", "a", "me")]

    with pytest.raises(SystemExit) as stopped:
        config_cmd.cmd_init(home, ["--help"])
    assert stopped.value.code == 0
    usage = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("usage:"))
    assert usage.startswith("usage: pm-config init") and "board.py" not in usage

    with pytest.raises(SystemExit) as stopped:
        config_cmd.cmd_init(home, ["--no-such-flag"])
    assert stopped.value.code == 2
    error = capsys.readouterr().err
    assert error.startswith("usage: pm-config") and "board.py" not in error
    assert len(seen) == 1


def test_read_dispatch_preserves_code_and_pm_home(tmp_path, monkeypatch, capsys):
    code_root = tmp_path / "slot"
    owner = tmp_path / "owner"
    owner_board = owner / ".project_manager" / "board"
    code_root.mkdir()
    owner_board.mkdir(parents=True)
    base = pm_paths.home_at(code_root)
    resolutions = [pm_paths._ReadBoardResolution(owner_board, owner)]
    monkeypatch.setattr(pm_paths, "_resolve_read_board", lambda _home: resolutions[-1])
    seen = []
    monkeypatch.setattr(board_list, "cmd_list", lambda actual, _args: seen.append(actual) or 0)

    assert board_cmd._main(base, ["list"]) == 0
    read_home = seen[-1]
    assert read_home.root == code_root
    assert read_home.board_root == owner_board
    assert read_home.local_conf == owner / ".project_manager" / "local.conf"
    assert read_home.local_dir == owner / ".project_manager" / ".local"
    assert capsys.readouterr().out.splitlines()[0].startswith(f"repo 앵커: {code_root} (worktree)")

    resolutions.append(pm_paths._ReadBoardResolution(pm_paths._board_root_at(code_root), None))
    assert board_cmd._main(base, ["list"]) == 0
    assert seen[-1] == base
    assert pm_paths.home_at(code_root) == base


def test_mutation_guard_precedes_handler_and_runtime_errors_propagate(tmp_path, monkeypatch, capsys):
    home = pm_paths.home_at(tmp_path)
    calls = []

    def handler(actual, args):
        calls.append(args.title)
        raise OSError("disk full")

    monkeypatch.setattr(board_ticket, "cmd_new", handler)
    guard = []
    monkeypatch.setattr(
        board_dispatch, "_guard_worktree_misanchor",
        lambda actual, action: guard.append((actual, action)) or True,
    )
    assert board_cmd.main(home, ["new", "stray", "--task", "t1"]) == 1
    assert guard == [(home, "board.py new")] and calls == []

    monkeypatch.setattr(board_dispatch, "_guard_worktree_misanchor", lambda _home, _action: False)
    with pytest.raises(OSError, match="disk full"):
        board_cmd.main(home, ["new", "real", "--task", "t1"])
    assert calls == ["real"]

    def unresolved(_home):
        raise pm_paths.PmHomeResolutionError("선언 없음")

    monkeypatch.setattr(pm_paths, "_resolve_read_board", unresolved)
    capsys.readouterr()
    assert board_cmd.main(home, ["list"]) == 1
    assert "[중단] 소유 PM 홈을 확정할 수 없습니다: 선언 없음" in capsys.readouterr().err
    assert isinstance(board_cmd.build_parser(prog="pm-config"), argparse.ArgumentParser)
