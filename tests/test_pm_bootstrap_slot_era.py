"""board 읽기 freshness + 슬롯 시대차 경고 (T-0341·PM 69 stale-read).

두 축을 검증한다:
  1. **board/PM 홈 freshness offline fail-soft** — fetch 실패(offline)면 remote-tracking
     스냅샷을 "최신" 으로 오신뢰하지 않고 "판정불가 — 스냅샷일 수 있음" 으로 표기
     (`_format_freshness`). PM 69 slot-2 가 stale board 를 정상처럼 신뢰한 사고를 닫는다.
  2. **슬롯 시대차 경고** — 슬롯 worktree 의 HEAD 가 base(main) 대비 behind N 커밋이면
     identity surface 에 경고 줄을 표면화(`_slot_era_info`/`_format_slot_era_warning`).
     경고 발화 / 최신이면 무발화(오탐 0) / offline fail-soft(판정불가) 3형상.

git-network 은 기존 freshness 채널(T-0217)의 fetch 결과를 재사용한다 — 신규 fetch 없음
(§결정). 대부분 mock git_fn(DI)로 rev-list 출력을 재현하되, 슬롯 behind 계산의 실 git
의미(`rev-list --count HEAD..origin/<base>`)는 임시 git repo 픽스처로 end-to-end 확증한다.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


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


# ── board 대역: 시대차 base 해소용(`_repo_base`/`_repo_protected`) ────────────

class _EraBoard:
    """board 모듈 대역 — 시대차 base(main) 해소용. `_repo_base`/`_repo_protected` 를 돌려준다.

    `base` 미지정이면 `_repo_base` None → `_resolve_slot_base` 가 보호목록 폴백을 탄다.
    `protected` 미지정이면 빈 목록(protected 경고 끔 — 시대차만 격리 검증).
    """

    def __init__(self, *, base: str | None = None, protected: list[str] | None = None):
        self._base = base
        self._protected = protected

    def _repo_base(self, repo):
        return self._base

    def _repo_protected(self, repo):
        return list(self._protected) if self._protected is not None else []


class _FakeLease:
    def __init__(self, slot, repo):
        self.slot = slot
        self.repo = repo
        self.state = "leased"
        self.session = slot[len("work/"):] if slot.startswith("work/") else slot


class _EraPool:
    """worktree_pool mock — lean(bind_slot)/alloc 양 경로 지원(실 git/장부 미접촉)."""

    def __init__(self, *, slot="work/A_1", branch="A_1"):
        self._slot = slot
        self._branch = branch

    def bind_slot(self, slot, repo, session, *, git_runner=None):
        return _FakeLease(slot, repo)

    def alloc(self, repo, *, branch=None, resume=None, **_kw):
        return _FakeLease(self._slot, repo)

    def list_leases(self):
        # 0단계 실재 검사(T-0351) 통과용 idle 시드(회귀 0·idle=점유 아님).
        return [_idle_lease(self._slot)]

    def read_lease_strict(self, slot):
        # 기록 base가 없는 lease는 areas 기본으로 폴백한다. 실제 장부와 lock은 읽지 않는다.
        return SimpleNamespace(slot=slot, repo="A", git=None)

    def current_branch(self, slot, *, git_runner=None):
        return self._branch

    def slot_path(self, slot):
        return Path("/tmp/multipm") / slot

    def slot_status(self, slot, *, git_runner=None):
        return None  # 슬롯 상태는 이 테스트 무관(절 생략).


def _make_bootstrap(bootstrap, tmp_path, *, git_fn, board, worktree_pool):
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
        board=board,
        worktree_pool=worktree_pool,
    )


def _era_git_fn(bootstrap, *, slot_behind=0, fetch_rc=0, calls=None):
    """freshness probe(-C <dir> …) + `_collect_git`(무 -C) + 시대차 rev-list 를 dispatch.

    슬롯 자신의 @{u} freshness 는 behind 0(최신)로 고정해 시대차 신호(`HEAD..origin/main`)만
    격리한다. `slot_behind` = 슬롯 HEAD 가 origin/main 대비 뒤처진 커밋 수(시대차 재현).
    `fetch_rc=1` = offline(모든 fetch 실패) → 시대차 판정불가 경로. `calls` 리스트를 주면 전
    git argv 를 기록(시대차 rev-list 미호출 assert 용).
    """
    def _fn(args):
        if calls is not None:
            calls.append(args)
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
            if sub == ["rev-list", "--count", "HEAD..origin/main"]:
                return (0, f"{slot_behind}\n")
            if sub[:1] in (["pull"], ["checkout"]):
                return (0, "")
            return (0, "")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "A_1\n")
        if args[:2] == ["log", "--oneline"]:
            return (0, "abc123 subj\n")
        if args[:1] == ["status"]:
            return (0, "")
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
    assert line.startswith("⚠ fetch 실패")


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

def test_format_slot_era_warning_behind_positive(bootstrap):
    line = bootstrap._format_slot_era_warning({"base": "main", "behind": 5})
    assert line is not None
    assert "슬롯 시대차" in line and "behind 5 커밋" in line and "`main`" in line


def test_format_slot_era_warning_latest_is_none(bootstrap):
    """behind 0(최신) → None(줄 생략·오탐 0)."""
    assert bootstrap._format_slot_era_warning({"base": "main", "behind": 0}) is None


def test_format_slot_era_warning_undetermined_offline(bootstrap):
    line = bootstrap._format_slot_era_warning({"base": "main", "undetermined": True})
    assert line is not None
    assert "판정불가" in line and "`main`" in line


def test_format_slot_era_warning_none_input(bootstrap):
    """info None(base 미해소) → None(줄 생략)."""
    assert bootstrap._format_slot_era_warning(None) is None


# ══════════════════════════════════════════════════════════════════════════════
# C. _resolve_slot_base — areas base 우선 · 보호목록 폴백 · fail-soft
# ══════════════════════════════════════════════════════════════════════════════

def test_resolve_slot_base_uses_registered_base(bootstrap):
    """areas.md `base` 칼럼(명시 등록)을 base 로 쓴다."""
    inst = bootstrap.PmBootstrap(
        board=_EraBoard(base="develop", protected=["main"]),
        worktree_pool=_EraPool(),
    )
    assert inst._resolve_slot_base("A").branch == "develop"


def test_resolve_slot_base_ignores_protected_default(bootstrap):
    """`_repo_base` 미등록(None) → None — 보호목록(default 'main')으로 폴백하지 않는다 (codex must-fix).

    비공허: `_repo_protected` 는 미등록 repo 에도 default('main')를 돌려주므로, 그걸 base 폴백에
    쓰면 "미해소=생략" 이 잘못된 origin/main 판정으로 바뀐다(오탐). base 미등록은 진짜 생략(None).
    """
    inst = bootstrap.PmBootstrap(
        board=_EraBoard(base=None, protected=["main", "develop"]),
        worktree_pool=_EraPool(),
    )
    assert inst._resolve_slot_base("A") is None


def test_resolve_slot_base_none_when_base_empty(bootstrap):
    """base 칼럼 빈 값 → None(시대차 판정 생략)."""
    inst = bootstrap.PmBootstrap(
        board=_EraBoard(base="", protected=["main"]),
        worktree_pool=_EraPool(),
    )
    assert inst._resolve_slot_base("A") is None


def test_resolve_slot_base_none_when_board_lacks_helpers(bootstrap):
    """헬퍼 없는 board 대역 → getattr None → None(fail-soft·crash 0)."""
    inst = bootstrap.PmBootstrap(board=object(), worktree_pool=_EraPool())
    assert inst._resolve_slot_base("A") is None


# ══════════════════════════════════════════════════════════════════════════════
# D. _slot_scope_fetched — 슬롯 cwd scope 의 fetch 상태 재사용
# ══════════════════════════════════════════════════════════════════════════════

def test_slot_scope_fetched_matches_slot_cwd(bootstrap):
    inst = bootstrap.PmBootstrap()
    inst._bound_slot = "work/A_1"
    wt = inst._worktree_cwd("work/A_1")
    freshness = [
        {"label": "② PM 홈", "dir": str(bootstrap.REPO), "fetched": True},
        {"label": "① worktree", "dir": wt, "fetched": False},
    ]
    assert inst._slot_scope_fetched(freshness) is False


def test_slot_scope_fetched_none_when_no_match(bootstrap):
    inst = bootstrap.PmBootstrap()
    inst._bound_slot = "work/A_1"
    freshness = [{"label": "other", "dir": "/nonexistent/path/xyz", "fetched": True}]
    assert inst._slot_scope_fetched(freshness) is None


# ══════════════════════════════════════════════════════════════════════════════
# E. _slot_era_info — behind 계산 / 최신 / offline / 미해소 (mock git_fn)
# ══════════════════════════════════════════════════════════════════════════════

def _era_info_inst(bootstrap, *, board, rev_list_ret):
    """`_slot_era_info` 단위 검증용 — `_worktree_cwd` 를 고정 슬롯 경로로 대체."""
    calls: list[list[str]] = []

    def git_fn(args):
        calls.append(args)
        if args[2:4] == ["rev-list", "--count"]:
            return rev_list_ret
        return (0, "")

    inst = bootstrap.PmBootstrap(
        board=board,
        run_git_fn=git_fn,
        worktree_pool=_EraPool(),
    )
    inst._bound_slot = "work/A_1"
    inst._worktree_cwd = lambda slot=None: "/slot/A_1"  # 고정(rev-list -C 대상)
    inst._era_calls = calls
    return inst


def test_slot_era_info_computes_behind(bootstrap):
    inst = _era_info_inst(bootstrap, board=_EraBoard(base="main"), rev_list_ret=(0, "3\n"))
    freshness = [{"dir": "/slot/A_1", "fetched": True}]
    assert inst._slot_era_info("A", freshness) == {"base": "main", "behind": 3}
    # 신규 fetch 남발 금지(§결정) — 시대차 계산은 rev-list 만·fetch 호출 안 함.
    assert ["-C", "/slot/A_1", "fetch", "origin"] not in inst._era_calls


def test_slot_era_info_latest_behind_zero(bootstrap):
    inst = _era_info_inst(bootstrap, board=_EraBoard(base="main"), rev_list_ret=(0, "0\n"))
    freshness = [{"dir": "/slot/A_1", "fetched": True}]
    assert inst._slot_era_info("A", freshness) == {"base": "main", "behind": 0}


def test_slot_era_info_offline_undetermined(bootstrap):
    """슬롯 cwd scope fetch 실패(offline) → 판정불가·rev-list 조차 안 부른다(stale 무의미)."""
    inst = _era_info_inst(bootstrap, board=_EraBoard(base="main"), rev_list_ret=(0, "9\n"))
    freshness = [{"dir": "/slot/A_1", "fetched": False}]
    assert inst._slot_era_info("A", freshness) == {"base": "main", "undetermined": True}
    assert not any(a[2:4] == ["rev-list", "--count"] for a in inst._era_calls)


def test_slot_era_info_none_when_scope_unmatched(bootstrap):
    """freshness scope 매칭 실패(fetch 미증명·fetched None) → None·rev-list 미호출 (codex suggestion).

    비공허: fetch 성공을 증명 못 한 상태에서 rev-list 를 돌리면 stale origin/<base> 를 오신뢰
    한다 — 슬롯 cwd(/slot/A_1)와 dir 이 다른 scope 만 주어 매칭 실패를 강제하고 rev-list 미호출 확인.
    """
    inst = _era_info_inst(bootstrap, board=_EraBoard(base="main"), rev_list_ret=(0, "3\n"))
    freshness = [{"dir": "/other/dir", "fetched": True}]  # 슬롯 cwd 와 불일치 → fetched None
    assert inst._slot_era_info("A", freshness) is None
    assert not any(a[2:4] == ["rev-list", "--count"] for a in inst._era_calls)


def test_slot_era_info_none_when_base_unresolved(bootstrap):
    inst = _era_info_inst(bootstrap, board=_EraBoard(base=None, protected=["main"]),
                          rev_list_ret=(0, "3\n"))
    freshness = [{"dir": "/slot/A_1", "fetched": True}]
    assert inst._slot_era_info("A", freshness) is None
    # base 미해소 → rev-list 판정 시도조차 안 한다(오탐 0).
    assert not any(a[2:4] == ["rev-list", "--count"] for a in inst._era_calls)


def test_slot_era_info_none_when_revlist_fails_online(bootstrap):
    """online 인데 rev-list rc≠0(로컬 이상) → None(조용히 생략·판정불가 아님)."""
    inst = _era_info_inst(bootstrap, board=_EraBoard(base="main"), rev_list_ret=(128, ""))
    freshness = [{"dir": "/slot/A_1", "fetched": True}]
    assert inst._slot_era_info("A", freshness) is None


# ══════════════════════════════════════════════════════════════════════════════
# F. run() 통합 — lean/alloc identity surface 에 시대차 표면화 (behind 재현)
# ══════════════════════════════════════════════════════════════════════════════

def test_run_lean_surfaces_slot_era_behind(bootstrap, tmp_path, capsys):
    """lean 경로: 슬롯 HEAD 가 base(main) 대비 behind 4 → identity surface 에 시대차 경고."""
    inst = _make_bootstrap(
        bootstrap, tmp_path,
        git_fn=_era_git_fn(bootstrap, slot_behind=4),
        board=_EraBoard(base="main"),
        worktree_pool=_EraPool(),
    )
    assert inst.run(repo="A", slot=1) == 0
    out = capsys.readouterr().out
    assert "슬롯 시대차" in out
    assert "behind 4 커밋" in out


def test_run_lean_no_era_warning_when_latest(bootstrap, tmp_path, capsys):
    """lean 경로: 슬롯이 base 최신(behind 0) → 시대차 경고 무발화(오탐 0)."""
    inst = _make_bootstrap(
        bootstrap, tmp_path,
        git_fn=_era_git_fn(bootstrap, slot_behind=0),
        board=_EraBoard(base="main"),
        worktree_pool=_EraPool(),
    )
    assert inst.run(repo="A", slot=1) == 0
    out = capsys.readouterr().out
    assert "슬롯 시대차" not in out


def test_run_lean_era_undetermined_when_offline(bootstrap, tmp_path, capsys):
    """lean 경로: offline(fetch 실패) → 시대차 판정불가 fail-soft(경고는 판정불가로)."""
    inst = _make_bootstrap(
        bootstrap, tmp_path,
        git_fn=_era_git_fn(bootstrap, slot_behind=4, fetch_rc=1),
        board=_EraBoard(base="main"),
        worktree_pool=_EraPool(),
    )
    assert inst.run(repo="A", slot=1) == 0
    out = capsys.readouterr().out
    assert "슬롯 시대차 판정불가" in out
    # offline 이면 behind 숫자는 못 신뢰 — 커밋 수를 단정하지 않는다.
    assert "behind 4 커밋" not in out


def test_run_alloc_surfaces_slot_era_behind(bootstrap, tmp_path, capsys):
    """alloc 경로(--repo without --slot)도 시대차를 표면화(양 identity 경로 배선)."""
    inst = _make_bootstrap(
        bootstrap, tmp_path,
        git_fn=_era_git_fn(bootstrap, slot_behind=7),
        board=_EraBoard(base="main"),
        worktree_pool=_EraPool(slot="work/A_1"),
    )
    assert inst.run(repo="A", branch="A_1") == 0
    out = capsys.readouterr().out
    assert "슬롯 시대차" in out and "behind 7 커밋" in out


def test_run_lean_json_includes_slot_era(bootstrap, tmp_path, capsys):
    """--json 출력의 worktree.slot_era 에 behind 판정이 실린다(markdown 과 병행 surface)."""
    import json as _json
    inst = _make_bootstrap(
        bootstrap, tmp_path,
        git_fn=_era_git_fn(bootstrap, slot_behind=2),
        board=_EraBoard(base="main"),
        worktree_pool=_EraPool(),
    )
    assert inst.run(repo="A", slot=1, output_json=True) == 0
    data = _json.loads(capsys.readouterr().out)
    assert data["worktree"]["slot_era"] == {"base": "main", "behind": 2}


def test_run_lean_no_era_when_base_unregistered(bootstrap, tmp_path, capsys):
    """base 미등록(areas `base` 칼럼 부재)이면 보호목록 default('main')로 폴백하지 않고 시대차 생략 (codex must-fix).

    비공허: `_repo_protected` 가 default('main')를 돌려주는 미등록 repo 형상에서도 시대차 절이
    안 뜨고 `HEAD..origin/main` rev-list 를 *시도조차 안 한다*(오탐 0·잘못된 origin/main 판정 금지).
    """
    calls: list[list[str]] = []
    inst = _make_bootstrap(
        bootstrap, tmp_path,
        git_fn=_era_git_fn(bootstrap, slot_behind=4, calls=calls),
        board=_EraBoard(base=None, protected=["main", "develop"]),  # 미등록 repo 의 default 형상
        worktree_pool=_EraPool(),
    )
    assert inst.run(repo="A", slot=1) == 0
    out = capsys.readouterr().out
    assert "슬롯 시대차" not in out
    assert not any(a[2:] == ["rev-list", "--count", "HEAD..origin/main"] for a in calls)


# ══════════════════════════════════════════════════════════════════════════════
# G. run() 통합 — board/PM 홈 freshness offline fail-soft (req #1·솔로)
# ══════════════════════════════════════════════════════════════════════════════

def test_run_freshness_offline_says_undetermined_not_latest(bootstrap, tmp_path, capsys):
    """솔로 offline: freshness 절이 '최신' 이 아니라 '판정불가 — 스냅샷일 수 있음' (PM 69 stale-read)."""
    repo_dir = str(bootstrap.REPO)

    def _fn(args):
        if args[:2] == ["-C", repo_dir]:
            sub = args[2:]
            if sub == ["fetch", "origin"]:
                return (1, "fatal: could not read remote\n")  # offline
            if sub == ["symbolic-ref", "HEAD"]:  # full ref (T-0377).
                return (0, "refs/heads/main\n")
            if sub == ["status", "-s"]:
                return (0, "")
            if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
                return (0, "0\t0\n")  # stale local: behind 0 (실측 아님)
            return (0, "")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "main\n")
        if args[:2] == ["log", "--oneline"]:
            return (0, "abc123 subj\n")
        return (0, "")

    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    pm_state_file = tmp_path / "pm_state.md"
    pm_state_file.write_text("", encoding="utf-8")
    inst = bootstrap.PmBootstrap(
        run_board_fn=lambda a: (0, "✓ no lint issues\n") if a[:1] == ["lint"]
        else (0, "  [open   ] T-0001  x  pm  tag\n"),
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 미호출")),
        run_git_fn=_fn,
        log_file=log_file,
        areas_file=tmp_path / "areas.md",
        board_dir=tmp_path / "board",  # 미생성 → 솔로
        pm_state_file=pm_state_file,
    )
    assert inst.run() == 0
    out = capsys.readouterr().out
    # freshness 줄이 offline 을 최신으로 오신뢰하지 않는다.
    freshness_lines = [ln for ln in out.splitlines() if "freshness (" in ln]
    assert freshness_lines, "freshness 줄이 출력에 없음"
    assert any("판정불가 — 스냅샷일 수 있음" in ln for ln in freshness_lines)
    assert not any(ln.rstrip().endswith("최신") for ln in freshness_lines)


# ══════════════════════════════════════════════════════════════════════════════
# H. 실 git 픽스처 — 슬롯 behind base 계산 end-to-end (임시 repo·rev-list 실측)
# ══════════════════════════════════════════════════════════════════════════════

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _rev(cwd):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd),
                          check=True, capture_output=True, text=True).stdout.strip()


def _build_origin_with_main(tmp_path, *, commits):
    """bare origin + main 에 `commits` 개 커밋을 push 하고, 각 커밋 sha 목록을 반환한다."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    shas: list[str] = []
    for i in range(commits):
        (seed / "f.txt").write_text(str(i), encoding="utf-8")
        _git(seed, "add", ".")
        _git(seed, "commit", "-m", f"c{i}")
        shas.append(_rev(seed))
    _git(seed, "push", "origin", "main")
    return origin, shas


def _era_inst_at(bootstrap, slot_dir, *, base="main"):
    """실 슬롯 경로 slot_dir 을 가리키는 PmBootstrap(실 git 러너·시대차 계산 대상)."""
    inst = bootstrap.PmBootstrap(
        board=_EraBoard(base=base),
        worktree_pool=_EraPool(),
    )  # 기본 실 git 러너
    inst._bound_slot = "work/A_1"
    inst._worktree_cwd = lambda slot=None: str(slot_dir)
    return inst


def test_slot_era_info_real_git_behind(bootstrap, tmp_path):
    """실 git: 슬롯 HEAD 가 C1 에 있고 origin/main 은 C3 → behind 2 를 rev-list 로 실측."""
    origin, shas = _build_origin_with_main(tmp_path, commits=3)
    slot = tmp_path / "slot"
    _git(tmp_path, "clone", str(origin), str(slot))
    _git(slot, "checkout", "-b", "A_1", shas[0])  # 슬롯을 C1(시대 뒤처짐)에 고정
    inst = _era_inst_at(bootstrap, slot)
    freshness = [{"dir": str(slot), "fetched": True}]  # freshness 채널 fetch 성공 재사용
    assert inst._slot_era_info("A", freshness) == {"base": "main", "behind": 2}


def test_slot_era_info_real_git_latest(bootstrap, tmp_path):
    """실 git: 슬롯이 origin/main 최신에 있으면 behind 0(경고 무발화 경로·오탐 0)."""
    origin, shas = _build_origin_with_main(tmp_path, commits=3)
    slot = tmp_path / "slot"
    _git(tmp_path, "clone", str(origin), str(slot))
    _git(slot, "checkout", "-b", "A_1", shas[-1])  # 최신 커밋
    inst = _era_inst_at(bootstrap, slot)
    freshness = [{"dir": str(slot), "fetched": True}]
    info = inst._slot_era_info("A", freshness)
    assert info == {"base": "main", "behind": 0}
    # behind 0 → 경고 줄 생략(오탐 0).
    assert bootstrap._format_slot_era_warning(info) is None
