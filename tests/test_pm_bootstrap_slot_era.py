"""board 읽기 freshness + 슬롯 시대차 경고 (T-0341·PM 69 stale-read).

두 축을 검증한다:
  1. **board/PM 홈 freshness offline fail-soft** — fetch 실패(offline)면 remote-tracking
     스냅샷을 "최신" 으로 오신뢰하지 않고 "판정불가 — 스냅샷일 수 있음" 으로 표기
     (`_format_freshness`). PM 69 slot-2 가 stale board 를 정상처럼 신뢰한 사고를 닫는다.
  2. **슬롯 시대차 경고** — 슬롯 HEAD 가 부모 브랜치 끝보다 behind N 커밋이면 identity surface 에
     경고 줄을 표면화(`_slot_era_info`/`_format_slot_era_warning`). 경고 발화 / 최신이면 무발화
     (오탐 0) / offline fail-soft(판정불가) 3형상.

뒤 커밋 수는 엔진(`worktree_pool.slot_git_status` 의 parent)이 센 값이다 — 부트스트랩은 세지 않고
기존 freshness 채널(T-0217)의 fetch 결과로 remote-tracking 비교를 믿을지만 정한다(신규 fetch 없음).
`_slot_era_info` 단위 판정은 `test_pm_bootstrap_slot_base.py`, 부모 앞/뒤 실측은 `test_worktree_pool.py`
의 실 git 테스트가 맡는다. 여기서는 run() 배선만 본다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from _slot_seed import fake_slot_path


def _idle_lease(slot: str):
    """0단계(T-0351) 실재 검사 통과용 idle 리스 시드 — phase-0 는 slot/state/session/extra 만 읽는다."""
    return SimpleNamespace(slot=slot, repo="", session="", state="idle", extra={})

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOOTSTRAP_PY = TOOLS / "pm_bootstrap.py"


def _load_module(name: str = "pm_bootstrap"):
    """pm_bootstrap 를 경로 로드한다 (도구는 패키지가 아니므로 importlib·타 테스트 관용구)."""
    spec = importlib.util.spec_from_file_location(name, BOOTSTRAP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bootstrap():
    return _load_module()


@pytest.fixture(autouse=True)
def _hermetic_engine_anchor(bootstrap, monkeypatch):
    """0단계 엔진 앵커 검사(T-0351)를 hermetic 무력화한다 (worktree ①에서 로드돼 실 REPO 가 등록
    worktree 사본으로 보이는 문제 회피·test_pm_bootstrap_lease 동형). 실 board 를 로드해
    `_pm_home_worktree_misanchor`→None 만 패치하고 board=None 경로가 그 패치본을 받게 한다."""
    real_board = bootstrap._load_board()
    if real_board is not None:
        monkeypatch.setattr(real_board, "_pm_home_worktree_misanchor",
                            lambda anchor, **_kw: None, raising=False)
    monkeypatch.setattr(bootstrap, "_load_board", lambda: real_board)


# ── board 대역: 보호 브랜치 경고 끔 ────────────────────────────────────────────

class _EraBoard:
    """board 모듈 대역 — 보호목록을 비워 보호 브랜치 경고를 끄고 시대차만 격리한다."""

    def _repo_protected(self, repo):
        return []


class _FakeLease:
    def __init__(self, slot, repo):
        self.slot = slot
        self.repo = repo
        self.state = "leased"
        self.session = slot[len("work/"):] if slot.startswith("work/") else slot


def _parent(*, behind, ref="origin/main"):
    """엔진 `slot_git_status` 의 parent 모양 — 부모 main · 비교 ref · 뒤 커밋 수."""
    return {"name": "main", "fork": "f00d", "ref": ref, "ahead": 0, "behind": behind}


class _EraPool:
    """worktree_pool mock — alloc 경로 지원(실 git/장부 미접촉).

    `parent` = `slot_git_status` 가 돌려줄 부모 행(None = 부모 기록 없음)."""

    def __init__(self, *, slot="work/A_1", branch="A_1", parent=None):
        self._slot = slot
        self._branch = branch
        self._parent = parent

    def alloc(self, repo, *, branch=None, resume=None, **_kw):
        return _FakeLease(self._slot, repo)

    def lease_is_open(self, lease):
        """열림 판정은 엔진 규칙 그대로 — 열림 기록(leased + session)이면 열림."""
        return getattr(lease, "state", None) == "leased" and bool(getattr(lease, "session", ""))

    def list_leases(self):
        # 0단계 실재 검사(T-0351) 통과용 idle 시드(회귀 0·idle=점유 아님).
        return [_idle_lease(self._slot)]

    def read_lease_strict(self, slot):
        return SimpleNamespace(slot=slot, repo="A", git=None)

    def current_branch(self, slot, *, git_runner=None):
        return self._branch

    def slot_path(self, slot):
        return fake_slot_path(slot)

    def slot_status(self, slot, *, git_runner=None):
        return None  # 슬롯 상태는 이 테스트 무관(절 생략).

    def slot_git_status(self, slot, *, git_runner=None):
        return {"slot": slot, "branch": self._branch, "head": "abc", "parent": self._parent,
                "children": [], "stashes": 0, "submodules": [], "dirty": False}


def _make_bootstrap(bootstrap, tmp_path, *, git_fn, worktree_pool):
    """시대차 run() 통합 픽스처 — board/log/pm_state/areas hermetic, git 은 주입 git_fn."""
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("| repo | prefix |\n|---|---|\n| A | A |\n", encoding="utf-8")
    pm_state_file = tmp_path / "pm_state.md"
    pm_state_file.write_text("", encoding="utf-8")
    board_dir = tmp_path / "board"  # 미생성 → board rider None(시대차와 무관)

    def fake_board(args):
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        return 0, "  [open   ] T-0001  x  pm  tag\n"

    return bootstrap.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 미호출")),
        run_git_fn=git_fn,
        log_file=log_file,
        areas_file=areas_file,
        board_dir=board_dir,
        pm_state_file=pm_state_file,
        board=_EraBoard(),
        worktree_pool=worktree_pool,
    )


def _era_git_fn(bootstrap, *, fetch_rc=0):
    """freshness probe(-C <dir> …) + `_collect_git`(무 -C) 를 dispatch.

    슬롯 자신의 @{u} freshness 는 behind 0(최신)로 고정한다. `fetch_rc=1` = offline(모든 fetch
    실패) → 시대차 판정불가 경로.
    """
    def _fn(args):
        if args[:1] == ["-C"]:
            sub = args[2:]
            if sub == ["fetch", "origin"]:
                return (fetch_rc, "" if fetch_rc == 0 else "fatal: could not read remote\n")
            if sub == ["symbolic-ref", "HEAD"]:  # full ref (T-0377).
                return (0, "refs/heads/A_1\n")
            if sub == ["status", "-s"]:
                return (0, "")
            if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
                return (0, "0\t0\n")  # 슬롯 자신 upstream 은 최신
            return (0, "")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "A_1\n")
        if args[:2] == ["log", "--oneline"]:
            return (0, "abc123 subj\n")
        return (0, "")
    return _fn


# ══════════════════════════════════════════════════════════════════════════════
# A. _format_freshness — board/PM 홈 offline fail-soft (T-0341 req #1)
# ══════════════════════════════════════════════════════════════════════════════

def test_format_freshness_offline_behind0_is_undetermined_not_latest(bootstrap):
    """offline(fetch 실패)·behind 0 → '최신' 이 아니라 '판정불가 — 스냅샷일 수 있음' (핵심 fix)."""
    scope = {"fetched": False, "detached": False, "behind": 0, "ahead": 0, "note": None}
    line = bootstrap._format_freshness(scope)
    assert "판정불가 — 스냅샷일 수 있음" in line
    assert "최신" not in line          # stale 스냅샷을 최신으로 오신뢰 금지
    # [[T-0960]] AT-009 — 실패 원인을 offline 으로 단정하지 않고 사용자가 다시 돌려 볼 명령을 말한다
    # (origin 이 없는 새 홈에서도 같은 줄이 나간다).
    assert line.startswith("⚠ fetch 실패")
    assert "`git fetch origin`" in line and "offline" not in line


def test_format_freshness_offline_upstream_none_is_undetermined(bootstrap):
    """offline·upstream 미상(behind None) → 판정불가 (upstream 없음 단정도 금지)."""
    scope = {"fetched": False, "detached": False, "behind": None, "ahead": None, "note": None}
    line = bootstrap._format_freshness(scope)
    assert "판정불가 — 스냅샷일 수 있음" in line


def test_format_freshness_offline_behind_positive_still_shows_behind(bootstrap):
    """offline 이라도 behind>0 은 로컬이 이미 아는 뒤처짐 — 그대로 표기(정보 손실 없음)."""
    scope = {"fetched": False, "detached": False, "behind": 2, "ahead": 0,
             "note": "⚠ behind 2 — 수동 동기 필요 (fetch 실패)"}
    line = bootstrap._format_freshness(scope)
    assert "behind 2 / ahead 0" in line
    assert line.startswith("⚠ fetch 실패")


def test_format_freshness_online_latest_still_says_latest(bootstrap):
    """online·behind 0 은 여전히 '최신' — 오탐 0(offline fail-soft 가 online 을 오염 안 함)."""
    scope = {"fetched": True, "detached": False, "behind": 0, "ahead": 0, "note": None}
    assert bootstrap._format_freshness(scope) == "최신"


def test_format_freshness_online_behind_unchanged(bootstrap):
    """online·behind>0 표기는 기존과 동일(회귀 가드)."""
    scope = {"fetched": True, "detached": False, "behind": 3, "ahead": 0, "note": None}
    assert bootstrap._format_freshness(scope) == "behind 3 / ahead 0"


# ══════════════════════════════════════════════════════════════════════════════
# B. _format_slot_era_warning — 순수 렌더 (경고/무발화/판정불가/None)
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# D. _slot_scope_fetched — 슬롯 cwd scope 의 fetch 상태 재사용
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# F. run() 통합 — lean/alloc identity surface 에 시대차 표면화
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# G. run() 통합 — board/PM 홈 freshness offline fail-soft (req #1·솔로)
# ══════════════════════════════════════════════════════════════════════════════
