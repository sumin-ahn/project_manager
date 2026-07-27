"""부트스트랩 시대차 기준은 slot lease git.base를 areas repo base보다 우선한다."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP_PY = REPO / ".project_manager" / "tools" / "pm_bootstrap.py"


@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("pm_bootstrap_slot_base", BOOTSTRAP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def worktree_pool():
    path = REPO / ".project_manager" / "tools" / "worktree_pool.py"
    spec = importlib.util.spec_from_file_location("worktree_pool_slot_base", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Board:
    def __init__(self, base):
        self.base = base
        self.calls = []

    def _repo_base(self, repo):
        self.calls.append(repo)
        return self.base


class _Pool:
    def __init__(self, lease=None, *, error=None):
        self.lease = lease
        self.error = error
        self.calls = []

    def read_lease_strict(self, slot):
        self.calls.append(slot)
        if self.error is not None:
            raise self.error
        return self.lease


def _lease(base=..., *, role="work"):
    git = {} if base is ... else {"base": base}
    return SimpleNamespace(slot="work/A_3", repo="A", git=git, role=role)


def _inst(bootstrap, *, lease=None, board_base="main", error=None, git_fn=None):
    board = _Board(board_base)
    pool = _Pool(lease, error=error)
    kwargs = {"board": board, "worktree_pool": pool}
    if git_fn is not None:
        kwargs["run_git_fn"] = git_fn
    inst = bootstrap.PmBootstrap(**kwargs)
    inst._bound_slot = "work/A_3"
    inst._worktree_cwd = lambda slot=None: "/slot/A_3"
    return inst, board, pool


def test_slot_record_wins_over_repo_default_and_uses_branch_tip(bootstrap):
    """명시 slot base가 areas base보다 우선하며 commit breadcrumb는 비교 target이 아니다."""
    breadcrumb = "a692bb61234567890"
    inst, board, pool = _inst(
        bootstrap,
        lease=_lease({"branch": "task/main", "commit": breadcrumb}),
        board_base="main",
    )

    resolved = inst._resolve_slot_base("A")

    assert resolved.branch == "task/main"
    assert resolved.source == "slot-record"
    assert resolved.target == "task/main"
    assert resolved.needs_fetch is False
    assert pool.calls == ["work/A_3"]
    assert board.calls == []  # slot 명시 기록이 areas 조회보다도 먼저 이긴다.


def test_unrecorded_slot_falls_back_to_repo_default(bootstrap):
    """lease는 있으나 git.base 미기록이면 기존 areas base를 그대로 폴백한다."""
    inst, board, _pool = _inst(bootstrap, lease=_lease(), board_base="develop")

    resolved = inst._resolve_slot_base("A")

    assert resolved.branch == "develop"
    assert resolved.source == "repo-default"
    assert resolved.target == "origin/develop"
    assert board.calls == ["A"]


def test_both_slot_record_and_repo_default_absent_returns_none(bootstrap):
    """slot base와 areas base가 모두 없으면 기존 시대차 생략(None) 계약이다."""
    inst, _board, _pool = _inst(bootstrap, lease=_lease(), board_base=None)
    assert inst._resolve_slot_base("A") is None


def test_lease_without_git_snapshot_falls_back_to_repo_default(bootstrap):
    """lease.git 기본값이 None이어도 base 미기록으로 보고 areas 기본을 사용한다."""
    lease = SimpleNamespace(slot="work/A_3", repo="A", git=None, role="work")
    inst, board, pool = _inst(bootstrap, lease=lease, board_base="develop")

    resolved = inst._resolve_slot_base("A")

    assert resolved.branch == "develop"
    assert resolved.source == "repo-default"
    assert board.calls == ["A"]
    assert pool.calls == ["work/A_3"]


def test_strict_ledger_failure_is_fail_soft_without_repo_masking(bootstrap):
    """strict 장부 오류는 crash하지 않고 None; areas 값으로 손상을 '미기록'처럼 숨기지 않는다."""
    inst, board, _pool = _inst(
        bootstrap,
        lease=None,
        board_base="main",
        error=ValueError("broken ledger"),
    )
    assert inst._resolve_slot_base("A") is None
    assert board.calls == []


@pytest.mark.parametrize(
    "bad_base",
    [["not", "an", "object"], {"commit": "abc123"}, {"branch": 123, "commit": "abc123"}],
)
def test_malformed_recorded_base_is_fail_soft(bootstrap, bad_base):
    """git.base 스키마 이상도 None으로 강등하고 잘못된 축의 rev-list를 만들지 않는다."""
    inst, board, _pool = _inst(bootstrap, lease=_lease(bad_base))
    assert inst._resolve_slot_base("A") is None
    assert board.calls == []


def test_local_slot_branch_compares_exact_ref_without_origin_even_offline(bootstrap):
    """commit 없는 구 slot 기록의 로컬 branch는 origin prefix/fetch 없이 기록 ref 그대로 비교한다."""
    calls = []

    def git_fn(args):
        calls.append(args)
        return 0, "2\n"

    inst, _board, _pool = _inst(
        bootstrap,
        lease=_lease({"branch": "task/main"}),
        git_fn=git_fn,
    )
    info = inst._slot_era_info("A", [{"dir": "/slot/A_3", "fetched": False}])

    assert info == {
        "base": "task/main",
        "behind": 2,
        "source": "slot-record",
        "target": "task/main",
    }
    assert calls == [["-C", "/slot/A_3", "rev-list", "--count", "HEAD..task/main"]]


def test_recorded_commit_is_ignored_and_moving_branch_tip_is_compared(bootstrap):
    """자동 기록된 commit이 있어도 시대차는 base.branch의 현재 tip으로 계산한다."""
    breadcrumb = "a692bb61234567890"
    calls = []

    def git_fn(args):
        calls.append(args)
        return 0, "5\n"

    inst, _board, _pool = _inst(
        bootstrap,
        lease=_lease({"branch": "origin/main", "commit": breadcrumb}),
        git_fn=git_fn,
    )
    info = inst._slot_era_info("A", [{"dir": "/slot/A_3", "fetched": True}])

    assert info["behind"] == 5
    assert info["target"] == "origin/main"
    assert calls == [["-C", "/slot/A_3", "rev-list", "--count", "HEAD..origin/main"]]


def test_missing_recorded_commit_does_not_block_branch_comparison(bootstrap):
    """로컬에 없는 commit breadcrumb는 해소하지 않고 유효한 branch tip만 비교한다."""
    calls = []

    def git_fn(args):
        calls.append(args)
        if args[-1] == "HEAD..missing-commit":
            return 128, ""
        return 0, "4\n"

    inst, _board, _pool = _inst(
        bootstrap,
        lease=_lease({"branch": "task/main", "commit": "missing-commit"}),
        git_fn=git_fn,
    )
    info = inst._slot_era_info("A", [{"dir": "/slot/A_3", "fetched": False}])

    assert info["behind"] == 4
    assert info["target"] == "task/main"
    assert calls == [["-C", "/slot/A_3", "rev-list", "--count", "HEAD..task/main"]]


def test_unpinned_remote_slot_ref_keeps_offline_fail_soft(bootstrap):
    """commit 없는 origin/* 구 기록은 기존 fetch 증명 계약을 유지한다."""
    calls = []
    inst, _board, _pool = _inst(
        bootstrap,
        lease=_lease({"branch": "origin/release"}),
        git_fn=lambda args: calls.append(args) or (0, "9\n"),
    )
    info = inst._slot_era_info("A", [{"dir": "/slot/A_3", "fetched": False}])

    assert info == {
        "base": "origin/release",
        "undetermined": True,
        "source": "slot-record",
        "target": "origin/release",
    }
    assert calls == []


def test_warning_marks_slot_or_repo_base_source_with_one_token(bootstrap):
    """경고에서 어느 기준축인지 [slot-record]/[repo-default] 한 토큰으로 관측된다."""
    slot_line = bootstrap._format_slot_era_warning(
        {"base": "task/main", "behind": 2, "source": "slot-record", "target": "task/main"}
    )
    repo_line = bootstrap._format_slot_era_warning({"base": "main", "behind": 2})

    assert "[slot-record]" in slot_line
    assert "[repo-default]" in repo_line
    assert "rebase task/main" in slot_line
    assert "rebase origin/main" in repo_line


def _git(cwd, *args, capture=False):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=capture,
        stdout=None if capture else subprocess.DEVNULL,
        stderr=None if capture else subprocess.DEVNULL,
        text=True,
    )


def _commit_file(repo, value):
    (repo / "f.txt").write_text(value, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", value)
    return _git(repo, "rev-parse", "HEAD", capture=True).stdout.strip()


def test_real_git_recorded_base_branch_advance_surfaces_behind(bootstrap, tmp_path):
    """실 git에서 기록 당시 commit이 남아 있어도 전진한 base.branch tip 대비 경고가 발화한다."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    recorded_commit = _commit_file(seed, "c1")
    _git(seed, "push", "origin", "main")

    slot = tmp_path / "slot"
    _git(tmp_path, "clone", str(origin), str(slot))
    _git(slot, "checkout", "-b", "A_3", recorded_commit)

    _commit_file(seed, "c2")
    _commit_file(seed, "c3")
    _git(seed, "push", "origin", "main")
    _git(slot, "fetch", "origin")

    lease = _lease({"branch": "origin/main", "commit": recorded_commit})
    inst, _board, _pool = _inst(bootstrap, lease=lease)
    inst._worktree_cwd = lambda slot_name=None: str(slot)

    pinned_behind = int(
        _git(slot, "rev-list", "--count", f"HEAD..{recorded_commit}", capture=True).stdout
    )
    info = inst._slot_era_info(
        "A",
        [{"dir": str(slot), "fetched": True}],
    )

    assert pinned_behind == 0
    assert info == {
        "base": "origin/main",
        "behind": 2,
        "source": "slot-record",
        "target": "origin/main",
    }
    warning = bootstrap._format_slot_era_warning(info)
    assert warning is not None
    assert "behind 2 커밋" in warning


def test_readonly_slot_uses_same_real_strict_ledger_resolution(
    bootstrap, worktree_pool, tmp_path, monkeypatch
):
    """실 strict 장부 파서로 readonly Lease를 읽어도 별도 role 분기 없이 slot 기록이 우선한다."""
    ledger = tmp_path / "worktree-leases.json"
    ledger.write_text(
        json.dumps(
            {
                "leases": [
                    {
                        "slot": "work/A_3",
                        "repo": "A",
                        "session": "",
                        "pid": 0,
                        "started": "2026-07-27T00:00:00+09:00",
                        "state": "idle",
                        "test_cmd": None,
                        "role": "readonly",
                        "git": {
                            "base": {"branch": "release/1", "commit": "c0ffee"},
                            "branch": None,
                            "head": "c0ffee",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(worktree_pool, "LEASES_FILE", ledger)
    monkeypatch.setattr(worktree_pool, "LEASES_LOCK", tmp_path / "worktree-leases.lock")
    board = _Board("main")
    inst = bootstrap.PmBootstrap(board=board, worktree_pool=worktree_pool)
    inst._bound_slot = "work/A_3"

    resolved = inst._resolve_slot_base("A")

    assert resolved.branch == "release/1"
    assert resolved.target == "release/1"
    assert resolved.source == "slot-record"
    assert board.calls == []
