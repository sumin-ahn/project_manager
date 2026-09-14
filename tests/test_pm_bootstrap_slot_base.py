"""부트스트랩 시대차 기준은 슬롯의 부모 브랜치(`worktree_pool.slot_git_status` 의 parent)다."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP_PY = REPO / ".project_manager" / "tools" / "pm_bootstrap.py"


@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("pm_bootstrap_slot_base", BOOTSTRAP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Pool:
    """`slot_git_status` 만 돌려주는 풀 대역 — 반환 모양은 엔진 계약 그대로다."""

    def __init__(self, parent):
        self.parent = parent
        self.calls = []

    def slot_git_status(self, slot):
        self.calls.append(slot)
        return {"slot": slot, "branch": "task/x", "head": "abc", "parent": self.parent,
                "children": [], "stashes": 0, "submodules": [], "dirty": False}


def _parent(ref, *, behind=2):
    return {"name": "main", "fork": "f00d", "ref": ref, "ahead": 1, "behind": behind}


def _inst(bootstrap, parent):
    pool = _Pool(parent)
    inst = bootstrap.PmBootstrap(worktree_pool=pool)
    inst._bound_slot = "work/A_3"
    inst._worktree_cwd = lambda slot=None: "/slot/A_3"
    return inst, pool


def test_local_parent_ref_is_compared_without_fetch_proof(bootstrap):
    """로컬 ref 가 비교 대상이면 fetch 성공 여부와 무관하게 엔진이 센 뒤 커밋 수를 쓴다."""
    inst, pool = _inst(bootstrap, _parent("main"))

    info = inst._slot_era_info([{"dir": "/slot/A_3", "fetched": False}])

    assert info == {"base": "main", "source": "slot-parent", "target": "main", "behind": 2}
    assert pool.calls == ["work/A_3"]


def test_remote_parent_ref_uses_behind_when_fetch_is_proven(bootstrap):
    inst, _pool = _inst(bootstrap, _parent("origin/main", behind=5))
    assert inst._slot_era_info([{"dir": "/slot/A_3", "fetched": True}])["behind"] == 5


def test_remote_parent_ref_offline_is_undetermined(bootstrap):
    inst, _pool = _inst(bootstrap, _parent("origin/main"))

    info = inst._slot_era_info([{"dir": "/slot/A_3", "fetched": False}])

    assert info == {"base": "main", "source": "slot-parent", "target": "origin/main",
                    "undetermined": True}


def test_remote_parent_ref_without_matching_scope_is_skipped(bootstrap):
    """fetch 를 증명할 scope 가 없으면 remote-tracking 비교를 믿지 않고 생략한다."""
    inst, _pool = _inst(bootstrap, _parent("origin/main"))
    assert inst._slot_era_info([{"dir": "/elsewhere", "fetched": True}]) is None


@pytest.mark.parametrize("parent", [
    None,
    {"name": "gone", "fork": "f00d", "ref": None, "ahead": None, "behind": None},
])
def test_missing_parent_record_or_deleted_parent_is_skipped(bootstrap, parent):
    """부모 기록이 없거나 부모 브랜치가 지워졌으면 경고를 생략한다 — repo 기본 base 로 대신 비교하지 않는다."""
    inst, _pool = _inst(bootstrap, parent)
    assert inst._slot_era_info([]) is None


def test_warning_names_the_parent_and_the_engine_rebase(bootstrap):
    line = bootstrap._format_slot_era_warning(
        {"base": "main", "source": "slot-parent", "target": "origin/main", "behind": 3})

    assert "부모 `main`" in line and "behind 3 커밋" in line
    assert "worktree_pool.py rebase" in line
    assert bootstrap._format_slot_era_warning(
        {"base": "main", "source": "slot-parent", "target": "main", "behind": 0}) is None
