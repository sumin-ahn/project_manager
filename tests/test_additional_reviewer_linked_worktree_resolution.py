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

    assert external.resolve_pm_home_for_repo(home, required=True) == home.resolve()
    assert external.resolve_pm_home_for_repo(worktree, required=True) == home.resolve()


def test_shape_b_unregistered_gate_worktree_uses_pm_home_for_delegate(
    tmp_path, monkeypatch, capsys,
):
    """형상 B: 같은 저장소 미등록 gate worktree도 PM 홈 config/board 앵커를 공유한다."""
    home, worktree, ticket = _managed_worktree(tmp_path)
    local_conf = home / ".project_manager" / "local.conf"
    local_conf.write_text(
        local_conf.read_text(encoding="utf-8")
        + "delegate.code-reviewer.harness=codex\n"
        + "delegate.code-reviewer.model=gpt-test\n",
        encoding="utf-8",
    )
    snapshot = tmp_path / "scratch" / "gate-X"
    snapshot.parent.mkdir()
    _git(worktree, "worktree", "add", "-q", "--detach", str(snapshot), "HEAD")
    prompt = snapshot / "prompt.md"
    prompt.write_text("review the isolated gate snapshot", encoding="utf-8")

    external = _load("additional_reviewer_shape_b_gate_owner")
    assert external.resolve_pm_home_for_repo(snapshot, required=True) == home.resolve()

    delegate = _load("pm_delegate_shape_b_gate_owner")
    monkeypatch.setattr(delegate, "REPO", worktree)
    base_args = [
        "--role", "code-reviewer", "--prompt-file", str(prompt),
        "--cwd", str(snapshot), "--dry-run",
    ]
    assert delegate.main([*base_args, "--gate", ticket]) == 0
    first = capsys.readouterr()
    assert f"local_conf={local_conf}" in first.err
    assert "PM 홈 해소 실패" not in first.err
    assert delegate._CONFIG_REPO_OVERRIDE == home.resolve()

    assert delegate.main([*base_args, "--ticket", ticket]) == 0
    second = capsys.readouterr()
    assert f"local_conf={local_conf}" in second.err
    assert f"내부 리뷰 게이트: {ticket}" in second.out
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
    assert external.resolve_pm_home_for_repo(snapshot, required=True) == home.resolve()


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
        external.resolve_pm_home_for_repo(snapshot, required=True)


def test_same_repo_multiple_checkout_owners_fail_loud(tmp_path):
    """같은 common-dir의 checkout들이 다른 PM 홈에 등록되면 어느 conf도 선택하지 않는다."""
    home_a = tmp_path / "pm-a"
    home_a.mkdir()
    _git(home_a, "init", "-q")
    _git(home_a, "config", "user.email", "test@example.invalid")
    _git(home_a, "config", "user.name", "test")
    (home_a / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(home_a, "add", "seed.txt")
    _git(home_a, "commit", "-qm", "seed")
    tickets_a = home_a / ".project_manager" / "wiki" / "tickets" / "open"
    tickets_a.mkdir(parents=True)
    (tickets_a / "T-9101-a.md").write_text(
        "---\nid: T-9101\ntitle: owner a\nstatus: open\n---\n",
        encoding="utf-8",
    )
    ledger_a = home_a / ".project_manager" / ".local" / "worktree-leases.json"
    ledger_a.parent.mkdir(parents=True)
    ledger_a.write_text(json.dumps({"leases": []}), encoding="utf-8")

    home_b = tmp_path / "pm-b"
    checkout_b = home_b / "work" / "repo_1"
    checkout_b.parent.mkdir(parents=True)
    _git(home_a, "worktree", "add", "-q", "--detach", str(checkout_b), "HEAD")
    tickets_b = home_b / ".project_manager" / "wiki" / "tickets" / "open"
    tickets_b.mkdir(parents=True)
    (tickets_b / "T-9102-b.md").write_text(
        "---\nid: T-9102\ntitle: owner b\nstatus: open\n---\n",
        encoding="utf-8",
    )
    ledger_b = home_b / ".project_manager" / ".local" / "worktree-leases.json"
    ledger_b.parent.mkdir(parents=True)
    ledger_b.write_text(
        json.dumps({"leases": [{"slot": "work/repo_1", "state": "leased"}]}),
        encoding="utf-8",
    )
    snapshot = tmp_path / "ambiguous" / "gate-X"
    snapshot.parent.mkdir()
    _git(home_a, "worktree", "add", "-q", "--detach", str(snapshot), "HEAD")

    external = _load("additional_reviewer_same_repo_multiple_owners")
    with pytest.raises(external.AnchorResolutionError) as caught:
        external.resolve_pm_home_for_repo(snapshot, required=True)
    message = str(caught.value)
    assert "여러 PM 홈의 worktree lease 장부에 등록" in message
    assert "소유자가 모호" in message
    assert str(home_a.resolve()) in message
    assert str(home_b.resolve()) in message


def test_code_reviewer_gate_and_ticket_share_required_owner_failure(
    tmp_path, monkeypatch, capsys,
):
    """무관 linked repo의 --gate/--ticket은 모두 self-demotion 없이 같은 해소 오류를 낸다."""
    _source, snapshot = _unregistered_worktree(tmp_path)
    prompt = snapshot / "prompt.md"
    prompt.write_text("review unrelated repository", encoding="utf-8")
    delegate = _load("pm_delegate_gate_required_owner")
    monkeypatch.setattr(delegate, "REPO", snapshot)
    base_args = [
        "--role", "code-reviewer", "--prompt-file", str(prompt),
        "--cwd", str(snapshot), "--dry-run",
    ]

    assert delegate.main([*base_args, "--gate", "T-9191"]) == 1
    gate_error = capsys.readouterr().err
    assert delegate.main([*base_args, "--ticket", "T-9191"]) == 1
    ticket_error = capsys.readouterr().err

    assert gate_error == ticket_error
    assert "오류: --cwd 소유 PM 홈 해소 실패" in gate_error
    assert "PM 홈을 찾지 못했습니다" in gate_error
    assert "board가 필요 없는 실행" not in gate_error
