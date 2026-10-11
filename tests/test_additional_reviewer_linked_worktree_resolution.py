"""T-0657 linked-worktree PM 홈 해소의 실제 Git 형상 회귀."""
from __future__ import annotations

import json

import pytest

from test_gate_anchor_resolution import (
    _git,
    _load,
    _managed_worktree,
    _unregistered_worktree,
)


def test_shape_a_pm_home_and_registered_worktree_keep_existing_owner(tmp_path):
    """형상 A: PM 홈 직접 실행과 lease 등록 슬롯은 기존 소유자 해소를 그대로 유지한다."""
    home, worktree, _ticket = _managed_worktree(tmp_path)
    external = _load("additional_reviewer_shape_a_registered_owner")

    assert external.resolve_pm_home_for_repo(home) == home.resolve()
    assert external.resolve_pm_home_for_repo(worktree) == home.resolve()


def test_shape_b_unregistered_gate_worktree_uses_pm_home_for_delegate(
    tmp_path, monkeypatch, capsys,
):
    """형상 B: 같은 저장소 미등록 linked worktree도 PM 홈 config/board 앵커를 공유한다."""
    home, worktree, ticket = _managed_worktree(tmp_path)
    local_conf = home / ".project_manager" / "local.conf"
    local_conf.write_text(
        local_conf.read_text(encoding="utf-8")
        + "delegate.researcher.harness=codex\n"
        + "delegate.researcher.model=gpt-test\n",
        encoding="utf-8",
    )
    linked = tmp_path / "scratch" / "linked-X"
    linked.parent.mkdir()
    _git(worktree, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    prompt = linked / "prompt.md"
    prompt.write_text("investigate the linked worktree", encoding="utf-8")

    external = _load("additional_reviewer_shape_b_linked_owner")
    assert external.resolve_pm_home_for_repo(linked) == home.resolve()

    delegate = _load("pm_delegate_shape_b_linked_owner")
    monkeypatch.setattr(delegate, "REPO", worktree)
    assert delegate.main([
        "--role", "researcher", "--prompt-file", str(prompt),
        "--cwd", str(linked), "--dry-run", "--ticket", ticket,
    ]) == 0
    captured = capsys.readouterr()
    assert f"local_conf={local_conf}" in captured.err
    assert "PM 홈 해소 실패" not in captured.err
    assert delegate._CONFIG_REPO_OVERRIDE == home.resolve()


def test_shape_b_bare_common_dir_uses_registered_checkout_owner(tmp_path):
    """형상 B-bare: main checkout 없는 공용 bare repo도 같은 저장소의 등록 슬롯으로 잇는다."""
    seed = tmp_path / "seed-repo"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "test@example.invalid")
    _git(seed, "config", "user.name", "test")
    (seed / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "seed.txt")
    _git(seed, "commit", "-qm", "seed")

    home = tmp_path / "pm-bare"
    bare = home / ".repos" / "project.git"
    bare.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    canonical = home / "work" / "project_1"
    canonical.parent.mkdir()
    _git(bare, "worktree", "add", "-q", "--detach", str(canonical), "HEAD")
    tickets = home / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    (tickets / "T-9003-bare.md").write_text(
        "---\nid: T-9003\ntitle: bare fixture\nstatus: open\n---\n",
        encoding="utf-8",
    )
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"leases": [{"slot": "work/project_1", "state": "leased"}]}),
        encoding="utf-8",
    )
    snapshot = tmp_path / "bare-scratch" / "gate-X"
    snapshot.parent.mkdir()
    _git(canonical, "worktree", "add", "-q", "--detach", str(snapshot), "HEAD")

    external = _load("additional_reviewer_shape_b_bare_gate_owner")
    assert external.resolve_pm_home_for_repo(snapshot) == home.resolve()


def test_shape_c_unrelated_repository_cannot_reuse_pm_home(tmp_path):
    """형상 C: PM 홈 아래 별도 Git 저장소의 미등록 worktree를 같은 소유자로 오해소하지 않는다."""
    home, _worktree, _ticket = _managed_worktree(tmp_path)
    unrelated = home / "unrelated"
    unrelated.mkdir()
    _git(unrelated, "init", "-q")
    _git(unrelated, "config", "user.email", "test@example.invalid")
    _git(unrelated, "config", "user.name", "test")
    (unrelated / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    _git(unrelated, "add", "foreign.txt")
    _git(unrelated, "commit", "-qm", "foreign")
    snapshot = tmp_path / "scratch" / "foreign-gate"
    snapshot.parent.mkdir()
    _git(unrelated, "worktree", "add", "-q", "--detach", str(snapshot), "HEAD")

    external = _load("additional_reviewer_shape_c_unrelated")
    with pytest.raises(external.AnchorResolutionError, match="PM 홈을 찾지 못했습니다"):
        external.resolve_pm_home_for_repo(snapshot)


def test_code_reviewer_cluster_and_ticket_share_required_owner_failure(
    tmp_path, monkeypatch, capsys,
):
    """무관 linked repo의 --cluster/--ticket은 모두 self-demotion 없이 같은 해소 오류를 낸다."""
    _source, linked = _unregistered_worktree(tmp_path)
    prompt = linked / "prompt.md"
    prompt.write_text("review unrelated repository", encoding="utf-8")
    delegate = _load("pm_delegate_cluster_required_owner")
    monkeypatch.setattr(delegate, "REPO", linked)

    assert delegate.main([
        "--role", "code-reviewer", "--cluster", "C-9191", "--cwd", str(linked), "--dry-run",
    ]) == 1
    cluster_error = capsys.readouterr().err
    assert delegate.main([
        "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(linked),
        "--dry-run", "--ticket", "T-9191",
    ]) == 1
    ticket_error = capsys.readouterr().err

    assert cluster_error == ticket_error
    assert "오류: --cwd 소유 PM 홈 해소 실패" in cluster_error
    assert "PM 홈을 찾지 못했습니다" in cluster_error
    assert "board가 필요 없는 실행" not in cluster_error
