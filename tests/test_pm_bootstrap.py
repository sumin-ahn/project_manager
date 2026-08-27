"""worktree 슬롯 상태 surface + bootstrap 배선 (T-0276 · ADR-0051 파일럿 T-β) 단위 테스트.

두 축을 hermetic 하게 검증한다:

  1. **backbone `worktree_pool.slot_status`** — mock `git_runner` 로 submodule 역할 판별
     (dev-ahead vs drift vs pinned)·upstream 해소/미해소·submodule 없음을 구동한다. 실 git·
     리스 장부·work/ 풀은 절대 건드리지 않는다(T-0275 판별 재사용의 표시층).

  2. **pm_bootstrap 배선** — mock worktree_pool 을 DI 로 주입해 identity surface 뒤 `### 슬롯
     상태` 서브섹션이 dev-ahead(정보) vs drift(경고 ⚠) 를 구별 표시하고, submodule 없는 슬롯은
     submodule 줄을 생략하며, upstream 미해소면 경고를 내는지 실 출력으로 확인한다.

검증 포인트(비공허·DoD): dev-ahead vs drift 구별 · submodule 없는 슬롯 = submodule 줄 생략 ·
upstream 미해소 = 경고 · slot_status 미구현 풀 = graceful(절 생략·crash 0).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _idle_lease(slot: str):
    """0단계(T-0351) 실재 검사 통과용 idle 리스 시드 — phase-0 는 slot/state/session/extra 만 읽는다."""
    return SimpleNamespace(slot=slot, repo="", session="", state="idle", extra={})

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wp():
    return _load("worktree_pool")


@pytest.fixture(scope="module")
def bootstrap():
    return _load("pm_bootstrap")


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


# ── backbone slot_status: mock git_runner 로 역할/upstream/submodule 판별 구동 ────


def _make_runner(
    *,
    branch_rc: int = 0,
    branch_out: str = "refs/heads/a5\n",   # full ref — current_branch 가 refs/heads/ strip (T-0377).
    upstream_rc: int = 0,
    upstream_out: str = "origin/a5\n",
    submodule_status: str = "",
    on_branch: set[str] | None = None,
    dirty: set[str] | None = None,
):
    """`_real_git_runner` 대역 — argv 별로 결정론적 (rc, out) 을 돌려주는 mock git_runner.

    on_branch = `symbolic-ref -q HEAD` rc0(=on-branch/dev) 인 submodule 경로 집합.
    dirty = `status --porcelain` 이 변경을 보고할 submodule 경로 집합.
    """
    on_branch = on_branch or set()
    dirty = dirty or set()

    def runner(argv: list) -> tuple[int, str]:
        # 슬롯 자신의 브랜치 (current_branch — symbolic-ref HEAD full ref·T-0377).
        if argv == ["symbolic-ref", "HEAD"]:
            return branch_rc, branch_out
        # 슬롯 브랜치의 upstream 추적 브랜치.
        if argv[:2] == ["rev-parse", "--abbrev-ref"]:
            return upstream_rc, upstream_out
        # submodule 목록.
        if argv[:2] == ["submodule", "status"]:
            return 0, submodule_status
        # submodule 당 on-branch/detached 판별 (git -C <sub> symbolic-ref -q HEAD).
        if len(argv) >= 4 and argv[0] == "-C" and argv[2] == "symbolic-ref" and argv[3] == "-q":
            sub = argv[1]
            return (0, "refs/heads/dev\n") if sub in on_branch else (1, "")
        # submodule 당 dirty 판정 (git -C <sub> status --porcelain).
        if len(argv) >= 4 and argv[0] == "-C" and argv[2] == "status":
            sub = argv[1]
            return (0, " M file.py\n") if sub in dirty else (0, "")
        return 1, ""

    return runner


# 40-hex sha placeholder — `git submodule status` 라인 sha 자리(파서는 값 무관·flag+path 만).
_SHA = "a" * 40


def test_slot_status_submodule_dev_ahead_vs_drift(wp):
    """on-branch=dev-ahead(정보) · detached+`+`(pin≠working)=drift(경고) · detached+공백=pinned 구별.

    비공허: dev-ahead 는 warning False·drift 는 warning True. 판별을 뒤집으면(예 on-branch 를
    drift 로) 이 단언이 red — T-0275 역할 판별 재사용의 핵심(ADR-0051 §Decision 4).
    """
    submodule_status = (
        f"+{_SHA} libs/drift (v1)\n"     # detached & pin≠working → drift(경고)
        f" {_SHA} libs/pinned (v2)\n"    # detached & pin==working → pinned(정상)
        f"+{_SHA} libs/dev (v3)\n"       # on-branch(아래) → dev-ahead(정보·flag 무관)
    )
    runner = _make_runner(submodule_status=submodule_status, on_branch={"libs/dev"})
    status = wp.slot_status("work/A_1", git_runner=runner)

    by_path = {s.path: s for s in status.submodules}
    assert set(by_path) == {"libs/drift", "libs/pinned", "libs/dev"}
    # dev-ahead = 정보(경고 아님) — on-branch 는 flag 가 `+` 여도 dev 역할이라 경고 아님.
    assert by_path["libs/dev"].kind == "dev-ahead"
    assert by_path["libs/dev"].warning is False
    # drift = 경고 — detached & pin≠working.
    assert by_path["libs/drift"].kind == "drift"
    assert by_path["libs/drift"].warning is True
    # pinned = 정상 — detached & pin==working.
    assert by_path["libs/pinned"].kind == "pinned"
    assert by_path["libs/pinned"].warning is False


def test_slot_status_drift_dirty_surfaced(wp):
    """detached+dirty submodule 은 drift + dirty=True — T-0275 가 dirty detached 를 재동기 skip(drift 잔존)."""
    runner = _make_runner(
        submodule_status=f"+{_SHA} libs/drift\n", on_branch=set(), dirty={"libs/drift"}
    )
    status = wp.slot_status("work/A_1", git_runner=runner)
    assert len(status.submodules) == 1
    sub = status.submodules[0]
    assert sub.kind == "drift"
    assert sub.dirty is True


def test_slot_status_uninitialized_is_warning(wp):
    """미초기화 submodule(flag `-`) → uninitialized(경고) — 슬롯 init 비정상.

    dirty 는 미초기화엔 무의미(워킹트리 부재 → `_submodule_dirty` 보수적 True 오표시)라 skip(False)
    — `⚠ uninitialized ·dirty` 잉여 방지(reviewer polish). 이 mock 은 uninit 를 dirty 로도
    보고하지만(dirty={"libs/uninit"}) kind 가 uninitialized 면 dirty 계산을 안 타야 한다.
    """
    runner = _make_runner(submodule_status=f"-{_SHA} libs/uninit\n", dirty={"libs/uninit"})
    status = wp.slot_status("work/A_1", git_runner=runner)
    assert status.submodules[0].kind == "uninitialized"
    assert status.submodules[0].warning is True
    assert status.submodules[0].dirty is False


def test_slot_status_absorbs_runner_exception(wp):
    """runner 가 예외를 던져도 slot_status 는 raise 하지 않고 보수적 SlotStatus 반환 (fail-soft 계약·codex).

    docstring 이 "예외 raise 안 함" 을 약속한다 — branch 조회 후 upstream/submodule 단계에서
    git_runner 가 raise 하면 upstream 미해소·submodule 빈목록으로 흡수해야 한다(미래 호출부
    [on-demand status·sync]가 이 계약에 기댄다). 비공허: slot_status 본문 try/except 를 제거하면
    이 호출이 RuntimeError 로 전파돼 red.
    """
    def raising(argv):
        # 브랜치 조회는 정상(current_branch 자체 흡수) — 그 이후 단계에서 raise.
        if argv == ["symbolic-ref", "HEAD"]:   # full ref (T-0377).
            return 0, "refs/heads/a5\n"
        raise RuntimeError("git exploded")

    status = wp.slot_status("work/A_1", git_runner=raising)  # raise 하면 이 줄에서 터짐.
    assert status.branch == "a5"          # current_branch 는 정상 조회.
    assert status.upstream is None        # upstream 조회 raise → 미해소로 흡수.
    assert status.upstream_ok is False
    assert status.submodules == []        # submodule 조회 raise → 빈목록으로 흡수.


def test_slot_status_no_submodule_empty_list(wp):
    """submodule 없는 슬롯 → `git submodule status` 빈 출력 → submodules 빈 리스트(부트스트랩 줄 생략 근거)."""
    runner = _make_runner(submodule_status="")
    status = wp.slot_status("work/A_1", git_runner=runner)
    assert status.submodules == []


def test_slot_status_upstream_resolved(wp):
    """`@{upstream}` 해소(rc0) → upstream 이름 + upstream_ok True."""
    runner = _make_runner(upstream_rc=0, upstream_out="origin/a5\n")
    status = wp.slot_status("work/A_1", git_runner=runner)
    assert status.upstream == "origin/a5"
    assert status.upstream_ok is True


def test_slot_status_upstream_unresolved_is_warning(wp):
    """`@{upstream}` 미해소(rc≠0·fatal 메시지) → upstream None + upstream_ok False(경고).

    비공허: `_real_git_runner` 결합출력(stdout+stderr)이라 미해소 시 out 에 fatal 문구가 있어
    비어있지 않다 — rc 를 먼저 안 보면 fatal 문구를 upstream 이름으로 오인한다(T-0273/0274 회귀).
    """
    runner = _make_runner(
        upstream_rc=128,
        upstream_out="fatal: no upstream configured for branch 'a5'\n",
    )
    status = wp.slot_status("work/A_1", git_runner=runner)
    assert status.upstream is None
    assert status.upstream_ok is False


# ── pm_bootstrap 배선: mock worktree_pool 주입 → 슬롯 상태 서브섹션 실 출력 ────


class _FakeLease:
    def __init__(self, slot: str, repo: str):
        self.slot = slot
        self.repo = repo


class _FakeBoard:
    """board 대역 — 보호 브랜치 판정 없음(빈 목록)으로 protected 경고를 끈다(슬롯 상태만 검증)."""

    def _repo_protected(self, repo):
        return []


class FakePool:
    """worktree_pool 인터페이스 mock — slot_status 를 주입값으로 돌려준다(실 git/장부 미접촉)."""

    def __init__(self, *, slot: str = "work/A_1", branch: str = "a5",
                 slot_status_ret=None):
        self._slot = slot
        self._branch = branch
        self._slot_status_ret = slot_status_ret
        self.slot_status_calls: list[str] = []

    def bind_slot(self, slot, repo, session, *, git_runner=None):
        return _FakeLease(slot, repo)

    def alloc(self, repo, *, branch=None, resume=None, **_kw):
        return _FakeLease(self._slot, repo)

    def list_leases(self):
        # 0단계 실재 검사(T-0351)가 통과하도록 이 슬롯을 idle 리스로 시드(회귀 0·idle=점유 아님).
        return [_idle_lease(self._slot)]

    def current_branch(self, slot, *, git_runner=None):
        return self._branch

    def slot_path(self, slot):
        return Path("/tmp/multipm") / slot

    def slot_status(self, slot, *, git_runner=None):
        self.slot_status_calls.append(slot)
        return self._slot_status_ret


class FakePoolNoStatus:
    """slot_status 미구현 풀(구버전) — `getattr(wp, "slot_status", None)` None 폴백 검증용."""

    def bind_slot(self, slot, repo, session, *, git_runner=None):
        return _FakeLease(slot, repo)

    def list_leases(self):
        # 0단계 실재 검사(T-0351) 통과용 idle 시드 — 이 풀은 lean(work/A_1) 테스트가 쓴다.
        return [_idle_lease("work/A_1")]

    def current_branch(self, slot, *, git_runner=None):
        return "a5"

    def slot_path(self, slot):
        return Path("/tmp/multipm") / slot


def _slot_status_obj(wp, *, upstream="origin/a5", upstream_ok=True, submodules=None):
    """실 worktree_pool.SlotStatus/SubmoduleStatus 로 슬롯 상태 대역을 만든다(duck-typing 정합)."""
    subs = []
    for path, kind, warning, dirty in (submodules or []):
        subs.append(wp.SubmoduleStatus(path, kind, warning=warning, dirty=dirty))
    return wp.SlotStatus(
        "work/A_1", branch="a5", upstream=upstream, upstream_ok=upstream_ok, submodules=subs
    )


def _make_bootstrap(bootstrap, tmp_path, *, worktree_pool, board_fn=None):
    """격리된 PmBootstrap — board/git/log/pm_state stub, worktree_pool/board mock 주입(lease 테스트 동형).

    `board_fn` 미지정이면 무해한 기본 board 러너(open 1건·lint clean)를 쓴다 — 슬롯-스코프 카운트
    조회(argv/라벨)를 검증하는 테스트는 커스텀 러너를 주입해 board list argv 를 캡처한다(T-0312).
    """
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("| repo | prefix |\n|---|---|\n| A | A |\n", encoding="utf-8")
    pm_state_file = tmp_path / "pm_state.md"
    pm_state_file.write_text("", encoding="utf-8")

    board_output = "  [open   ] T-0001  something  pm  tag\n"

    def default_board(args):
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        return 0, board_output

    fake_board = board_fn if board_fn is not None else default_board

    def fake_git(args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 commit subject\n"
        if args[:1] == ["status"]:
            return 0, ""
        return 0, ""

    return bootstrap.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨")),
        run_git_fn=fake_git,
        log_file=log_file,
        areas_file=areas_file,
        worktree_pool=worktree_pool,
        board=_FakeBoard(),
        pm_state_file=pm_state_file,
    )


def test_bind_task_rejects_archived_name_with_reopen_prescription(
    bootstrap, tmp_path, capsys,
):
    archive = tmp_path / "tasks" / "_ended" / "job-20260827"

    class ArchivedPool:
        class TaskArchived(Exception):
            def __init__(self):
                self.archives = (archive,)

        class TaskActiveElsewhere(Exception):
            pass

        class InvalidTaskName(Exception):
            pass

        def bind_task(self, name, registered_repos=None):
            raise self.TaskArchived()

    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=ArchivedPool())
    assert inst._bind_task_or_reject("job") is None
    err = capsys.readouterr().err
    assert "task reopen job" in err and "job-20260827" in err
    assert f"{bootstrap._runtime_skill_entry('pm-bootstrap')} --task job" in err


def test_bootstrap_lean_surfaces_dev_ahead_vs_drift(bootstrap, wp, tmp_path, capsys):
    """lean bind 경로 부트스트랩이 `### 슬롯 상태` 절에서 dev-ahead(정보) vs drift(경고 ⚠) 를 구별 표시한다.

    비공허: drift 토큰엔 ⚠ 가 붙고 dev-ahead 토큰엔 안 붙는다 — 구별을 없애면(둘 다 경고/둘 다
    정보) 이 단언이 red(ADR-0051 §Decision 4 의 핵심 — dev 작업을 문제로 오표시 금지).
    """
    ret = _slot_status_obj(wp, submodules=[
        ("libs/dev", "dev-ahead", False, False),
        ("libs/drift", "drift", True, False),
    ])
    pool = FakePool(slot_status_ret=ret)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=pool)
    rc = inst.run(repo="A", slot=1)
    assert rc == 0
    out = capsys.readouterr().out
    assert "### 슬롯 상태" in out
    assert "dev-ahead(정보)" in out
    assert "⚠ drift(pin≠working)" in out
    # dev-ahead 는 경고가 아니다 — ⚠ 가 dev 토큰 앞에 붙지 않는다(구별 확증).
    assert "⚠ dev-ahead" not in out
    # slot_status 는 이 슬롯에 대해 소비된다 — 0단계 main-참조 게이트(origin/main upstream 판정·T-0360)
    # 와 표시 surface 가 각각 1회씩 호출하므로 2회(둘 다 work/A_1·비공허: 표시가 slot_status 로 구동).
    assert pool.slot_status_calls == ["work/A_1", "work/A_1"]


def test_bootstrap_omits_submodule_line_when_none(bootstrap, wp, tmp_path, capsys):
    """submodule 없는 슬롯 = submodule 줄 생략(upstream 줄은 유지) — 비공허 omission."""
    ret = _slot_status_obj(wp, submodules=[])
    pool = FakePool(slot_status_ret=ret)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=pool)
    inst.run(repo="A", slot=1)
    out = capsys.readouterr().out
    # 슬롯 상태 절과 upstream 줄은 있으나 submodule 줄은 없다.
    assert "### 슬롯 상태" in out
    assert "upstream: `origin/a5`" in out
    assert "- submodule:" not in out


def test_bootstrap_warns_upstream_unresolved(bootstrap, wp, tmp_path, capsys):
    """upstream 미해소 슬롯 = 경고 표시(T-0273/0274 확인 안내) — 비공허 경고."""
    ret = _slot_status_obj(wp, upstream=None, upstream_ok=False, submodules=[])
    pool = FakePool(slot_status_ret=ret)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=pool)
    inst.run(repo="A", slot=1)
    out = capsys.readouterr().out
    assert "upstream: ⚠ 미해소" in out


def test_bootstrap_alloc_path_surfaces_slot_status(bootstrap, wp, tmp_path, capsys):
    """alloc 경로(--repo without --slot)도 슬롯 상태 서브섹션을 surface 한다(양 identity 경로 배선)."""
    ret = _slot_status_obj(wp, submodules=[("libs/drift", "drift", True, True)])
    pool = FakePool(slot_status_ret=ret)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=pool)
    inst.run(repo="A", branch="a5")
    out = capsys.readouterr().out
    assert "### 슬롯 상태" in out
    # dirty detached drift 는 ⚠ drift + ·dirty 로 *왜* 안 풀렸는지 함께 surface.
    assert "⚠ drift(pin≠working) ·dirty" in out


def test_bootstrap_slot_status_absent_pool_graceful(bootstrap, tmp_path, capsys):
    """slot_status 미구현 풀(구버전) = graceful — 절 생략·crash 0(fail-soft·기존 dump 무변경).

    비공허: `_safe_slot_status` 가 getattr None 폴백을 안 하면 AttributeError 로 run 이 깨진다.
    """
    pool = FakePoolNoStatus()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=pool)
    rc = inst.run(repo="A", slot=1)
    assert rc == 0
    out = capsys.readouterr().out
    # identity 는 나오되 슬롯 상태 절은 없다(백본 부재 fail-soft).
    assert "당신은 **A PM**" in out
    assert "### 슬롯 상태" not in out


# ── S1 (ADR-0056·T-0312): 슬롯 정체성 부트스트랩 = 슬롯-스코프 카운트 ─────────────


def test_bootstrap_slot_identity_counts_are_slot_scoped(bootstrap, wp, tmp_path, capsys):
    """**S1**: 슬롯 정체성(--repo/--slot) 부트스트랩은 카운트를 `list --repo <repo> --slot <N>`
    (ADR-0057·내 것 ∩ 그 슬롯)로 뽑고 라벨 "(slot N)" 로 announce 한다 — user-wide `list --mine`
    (전 슬롯) mislabel 근절.

    비공허: board list 호출 argv 가 `--repo A --slot 1`(≠`--mine`)이고, 렌더 카운트 라벨이
    "(slot 1)"(≠"(mine)")임을 확증한다 — S1 증상("claimed 4 (mine)" 인데 `list --repo/--slot` 은
    0)의 근본.
    """
    calls: list[list[str]] = []
    # slot 뷰(내 세션 스트림·ADR-0067) = 내 세션 생성 open 1 + 내 세션 claim 2. board.py 층이 타
    # 세션분을 완전 비노출하므로 이 렌즈 출력이 곧 스트림이고, `--all` 재조회(옛 접힘 모수·타 세션
    # claim)는 폐기됐다.
    slot_list_out = (
        "  [open   ] T-A-001  backlog     -           -\n"
        "  [claimed] T-A-010  wip a       alice/A_1   -\n"
        "  [claimed] T-A-011  wip b       alice/A_1   -\n"
    )
    slot_done_out = "  [done   ] T-A-009  done a       alice/A_1   -\n"

    def fake_board(args):
        calls.append(args)
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        if args == ["list", "--status", "done", "--repo", "A", "--slot", "1"]:
            return 0, slot_done_out
        if args == ["list", "--repo", "A", "--slot", "1"]:
            return 0, slot_list_out
        raise AssertionError(f"슬롯 정체성인데 슬롯-스코프 아님(또는 폐기된 --all): {args}")

    pool = FakePool(slot_status_ret=_slot_status_obj(wp, submodules=[]))
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=pool, board_fn=fake_board)
    rc = inst.run(repo="A", slot=1)
    assert rc == 0
    out = capsys.readouterr().out

    # 1) 카운트는 슬롯 정체성(--repo A --slot 1)으로만 조회(default + done) — `--all` 재조회 폐기(ADR-0067).
    list_calls = [c for c in calls if c[:1] == ["list"]]
    assert list_calls == [
        ["list", "--repo", "A", "--slot", "1"],
        ["list", "--status", "done", "--repo", "A", "--slot", "1"],
    ]
    assert not any("--mine" in c for c in calls), "슬롯 정체성인데 --mine 로 조회(S1 mislabel 재현)"
    assert not any("--all" in c for c in calls), "폐기된 --all 재조회(ADR-0067·타 세션 정보 노출)"
    # 2) 카운트 라벨 = "(slot 1)" — open 도 세션 스코프(ADR-0067·옛 open 전용 backlog 라벨 폐기).
    assert "claimed: 2 (slot 1)" in out
    assert "open: 1 (slot 1)" in out
    assert "done: 1 (slot 1)" in out
    assert "(mine)" not in out
    assert "backlog·기본 접힘" not in out
    # 3) open 상세 = 내 세션 스트림(생성 open) — 접힘 카운트/타 세션 줄 없이 그대로 나열(ADR-0067).
    assert "- open ticket (claim 가능): T-A-001" in out
    assert "그 외 open" not in out
    assert "타 세션 진행" not in out


# ── T-0284: fresh 슬롯 self-sufficiency (스크램블 낭비 제거) ──────────────────


def _make_fresh_bootstrap(bootstrap, wp, tmp_path):
    """fresh 슬롯 부트스트랩 대역 — pm_state 파일 *부재* + 공유 로그엔 무태그(=타 슬롯) handoff 만.

    fresh slot-2 라이브 시나리오 재현: 공유 로그(`pm_log.py tail` 이 보는 것)엔 무태그 handoff 가
    있지만 slot-2 는 그걸 자기 것으로 안 봐(MF-2·ADR-0047) 자기 컨텍스트가 0 이다. per-slot
    pm_state 도 아직 없다(첫 /pm-handoff 산물). 이 상태에서 부트스트랩이 "미해소" 스크램블 대신
    명시 fresh surface 를 내야 한다.
    """
    log_file = tmp_path / "current.md"
    # 무태그 handoff(솔로/slot-1 귀속) — slot-2 는 MF-2 로 무시. 본문에 표식 문구를 넣어 유입 여부 검증.
    log_file.write_text(
        "# log\n\n"
        "## [2026-07-12] handoff | PM 5차 → 다음 PM 세션\n"
        "- 남은작업: 타-슬롯-산출-표식\n",
        encoding="utf-8",
    )
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("| repo | prefix |\n|---|---|\n| A | A |\n", encoding="utf-8")
    # pm_state 파일 부재 — 첫 바인딩 슬롯. 존재하지 않는 경로를 주입(_resolve_pm_state_file 그대로 반환).
    pm_state_file = tmp_path / "slots" / "A_2" / "pm_state.md"  # 미존재(mkdir 안 함).

    board_output = "  [open   ] T-0001  something  pm  tag\n"

    def fake_board(args):
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        return 0, board_output

    def fake_git(args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 commit subject\n"
        return 0, ""

    ret = _slot_status_obj(wp, upstream="origin/a5", upstream_ok=True, submodules=[])
    pool = FakePool(slot="work/A_2", slot_status_ret=ret)  # slot-2 시나리오 — 0단계 실재 시드도 A_2.
    return bootstrap.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨")),
        run_git_fn=fake_git,
        log_file=log_file,
        areas_file=areas_file,
        worktree_pool=pool,
        board=_FakeBoard(),
        pm_state_file=pm_state_file,
    )


def test_fresh_slot_dumps_explicit_surface_not_placeholder(bootstrap, wp, tmp_path, capsys):
    """fresh 슬롯(pm_state 부재·자기 handoff 없음) → 명시 "fresh" surface · "미해소" placeholder 부재.

    비공허(DoD): (a) `🆕 첫 바인딩 슬롯`+`차수=1(fresh)`+`폴백 스캔 불요` 명시 배너 존재 (b) "미해소"
    스크램블 placeholder 부재. 또한 무태그 전역 handoff(`PM 5차`·본문 표식)를 자기 것으로 오인하지
    않아야 한다(MF-2) — 차수는 1차·본문 유입 0. fresh 분기를 없애면(placeholder 로 되돌리면) red.
    """
    inst = _make_fresh_bootstrap(bootstrap, wp, tmp_path)
    rc = inst.run(repo="A", slot=2)
    assert rc == 0
    out = capsys.readouterr().out
    # (a) 명시 fresh surface 존재.
    assert "🆕 첫 바인딩 슬롯" in out
    assert "차수=1(fresh)" in out
    assert "폴백 스캔 불요" in out
    # (b) "미해소" placeholder 부재 — fresh 는 복구할 게 없으니 스크램블 유발 표현 금지.
    assert "미해소" not in out
    # 차수는 1차(fresh 규칙) — 무태그 전역 "PM 5차" 를 자기 것으로 오인하지 않는다.
    assert "## PM 1차 부트스트랩" in out
    # 타 슬롯(무태그) handoff 본문이 유입되지 않는다(MF-2).
    assert "타-슬롯-산출-표식" not in out


def test_resolve_log_file_slot_cwd_finds_shared_home(bootstrap, tmp_path):
    """worktree 슬롯 cwd(REPO=`<home>/work/<repo>_N`)에서 상위 PM 홈 공유 로그를 해소한다 (T-0284 이슈2).

    비공허: 슬롯 REPO 밑엔 로그가 없어도(공유 로그는 PM 홈 소유) 상위 공유 로그가 실재하면 그걸
    가리킨다 — `pm_log.py tail`(PM 홈 실행)과 대칭. 해소를 REPO-앵커로 되돌리면 이 단언이 red.
    """
    home = tmp_path / "pm_home"
    shared_log = home / ".project_manager" / "wiki" / "log" / "current.md"
    shared_log.parent.mkdir(parents=True)
    shared_log.write_text("# log\n", encoding="utf-8")
    slot = home / "work" / "project_manager_2"
    slot.mkdir(parents=True)
    assert bootstrap._resolve_log_file(slot) == shared_log


def test_resolve_log_file_slot_without_shared_falls_back(bootstrap, tmp_path):
    """슬롯 형상이라도 상위 공유 로그 부재면 REPO-앵커 폴백 — false redirect 방지·fresh 채택자 무영향."""
    slot = tmp_path / "work" / "A_1"
    slot.mkdir(parents=True)  # 상위(tmp_path)에 공유 로그 없음.
    assert (
        bootstrap._resolve_log_file(slot)
        == slot / ".project_manager" / "wiki" / "log" / "current.md"
    )


def test_resolve_log_file_standalone_uses_repo_anchor(bootstrap, tmp_path):
    """슬롯이 아닌 REPO(PM 홈 직접·standalone 채택자)는 상위 탐색 없이 REPO-앵커 로그 그대로 (회귀 0)."""
    repo = tmp_path / "myrepo"  # parent.name != "work".
    assert (
        bootstrap._resolve_log_file(repo)
        == repo / ".project_manager" / "wiki" / "log" / "current.md"
    )


# ── main() 인자 가드: --branch/--resume repo-필수 검사가 auto-resolve **앞**이다 (T-0327) ──


def _spy_never_called(msg: str):
    """호출되면 AssertionError 로 즉시 실패하는 `_resolve_session_slot` 대역 + 호출 플래그."""
    flag = {"called": False}

    def _spy():
        flag["called"] = True
        raise AssertionError(msg)

    return _spy, flag


def test_branch_without_repo_errors_before_auto_resolve(bootstrap, monkeypatch):
    """`--branch`(무 `--repo`) 는 auto-resolve 가 args.repo 를 채우기 **전에** 즉시 거부된다.

    회귀 표적(T-0327): 가드가 auto-resolve 뒤에 있으면 auto-resolve 가 자동바인딩으로 args.repo 를
    채운 뒤 가드를 통과 → branch 가 그 슬롯에 silent 부착됐다. 가드를 앞으로 옮겨
    `_resolve_session_slot` 이 호출되기도 전에 error(rc 2)로 끝나는지 확인한다.

    비공허: spy 로 auto-resolve **미호출**을 단언한다 — 가드가 정말 auto-resolve 앞이라는 증거.
    가드를 다시 뒤로 옮기면 spy 가 호출돼 AssertionError 로 red.
    """
    spy, flag = _spy_never_called("auto-resolve 는 branch 가드 뒤라 호출되면 안 된다")
    monkeypatch.setattr(bootstrap, "_resolve_session_slot", spy)
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(["--branch", "wip-x"])
    assert exc.value.code == 2
    assert flag["called"] is False


def test_resume_without_repo_errors_before_auto_resolve(bootstrap, monkeypatch):
    """`--resume`(무 `--repo`)도 branch 와 동형 — auto-resolve 앞 가드에서 rc 2·auto-resolve 미호출 (T-0327)."""
    spy, flag = _spy_never_called("auto-resolve 는 resume 가드 뒤라 호출되면 안 된다")
    monkeypatch.setattr(bootstrap, "_resolve_session_slot", spy)
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(["--resume", "prev-wip"])
    assert exc.value.code == 2
    assert flag["called"] is False


def test_repo_with_branch_unchanged_reaches_run(bootstrap, monkeypatch):
    """`--repo R --branch X` 는 가드 이동에 불변 — 통과해 `PmBootstrap.run` 에 그대로 전달된다.

    repo 명시라 auto-resolve 는 원래 skip 이고, repo 가 있으니 branch 가드도 통과해야 한다. 가드
    이동이 이 정상 경로를 오탐 거부하지 않음을 확인한다.

    비공허: run 이 repo="A"·branch="wip-x" 로 정확히 전달됨을 단언 — 가드가 오탐 거부하면 SystemExit 로 red.
    """
    captured = {}

    def _fake_run(self, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bootstrap.PmBootstrap, "run", _fake_run)
    rc = bootstrap.main(["--repo", "A", "--branch", "wip-x"])
    assert rc == 0
    assert captured.get("repo") == "A"
    assert captured.get("branch") == "wip-x"


# ── F1 --task 배선 (main·⑥ 예약 거부·auto-slot 제외·T-0353) ─────────────────


def test_task_reserved_name_rejected_at_cli(bootstrap, monkeypatch):
    """`--task <등록 repo>_<N>` = 슬롯 세션 예약 패턴 → rc2 거부(⑥·run 미도달)."""
    monkeypatch.setattr(bootstrap, "_registered_repos", lambda *a, **k: ["project_manager"])
    monkeypatch.setattr(bootstrap.PmBootstrap, "run",
                        lambda self, **kw: (_ for _ in ()).throw(
                            AssertionError("run 도달 금지 — 예약 거부여야")))
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(["--task", "project_manager_1"])
    assert exc.value.code == 2


def test_task_free_name_reaches_run(bootstrap, monkeypatch):
    """자유 포맷 task 명은 통과해 run(task=…) 으로 전달된다(비공허)."""
    monkeypatch.setattr(bootstrap, "_registered_repos", lambda *a, **k: ["project_manager"])
    captured = {}
    monkeypatch.setattr(bootstrap.PmBootstrap, "run",
                        lambda self, **kw: captured.update(kw) or 0)
    rc = bootstrap.main(["--task", "payments-refactor"])
    assert rc == 0
    assert captured.get("task") == "payments-refactor"


@pytest.mark.parametrize(
    "argv",
    [
        ["--task", "job1", "--repo", "A"],
        ["--task", "job1", "--repo", "A", "--slot", "2"],
        ["--task", "job1", "--slot", "2"],
        ["--task", "job1", "--branch", "feature"],
        ["--task", "job1", "--resume", "feature"],
    ],
)
def test_task_cli_rejects_slot_identity_mixing_before_run(bootstrap, monkeypatch, argv):
    """Python CLI의 task 진입점도 repo/slot/branch/resume 없이 `--task` 하나만 허용한다."""
    monkeypatch.setattr(
        bootstrap.PmBootstrap,
        "run",
        lambda self, **kw: (_ for _ in ()).throw(
            AssertionError("혼합 identity가 run에 도달하면 안 된다")
        ),
    )
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(argv)
    assert exc.value.code == 2


def test_task_slot_only_surfaces_task_contract_before_repo_hint(
    bootstrap, monkeypatch, capsys
):
    """task+bare slot은 slot-mode `--repo` 처방보다 task 독립 정체성 계약을 먼저 표면화한다(SF)."""
    monkeypatch.setattr(
        bootstrap.PmBootstrap,
        "run",
        lambda self, **kw: (_ for _ in ()).throw(AssertionError("run 도달 금지")),
    )
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(["--task", "job1", "--slot", "2"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--task 는 단독 정체성" in err
    assert "--slot 은 --repo 필수" not in err


def test_task_alone_does_not_trigger_slot_auto_resolve(bootstrap, monkeypatch):
    """`--task` 단독은 슬롯 자동해소를 태우지 않는다(⑥·task=슬롯 0개 시작 가능·T-0353)."""
    monkeypatch.setattr(bootstrap, "_registered_repos", lambda *a, **k: [])
    spy, flag = _spy_never_called("--task 단독은 auto-slot 해소를 호출하면 안 된다(⑥)")
    monkeypatch.setattr(bootstrap, "_resolve_session_slot", spy)
    captured = {}
    monkeypatch.setattr(bootstrap.PmBootstrap, "run",
                        lambda self, **kw: captured.update(kw) or 0)
    rc = bootstrap.main(["--task", "job1"])
    assert rc == 0
    assert flag["called"] is False
    assert captured.get("task") == "job1" and captured.get("repo") is None


# ── task 신규 state의 완료 세션 0개 marker surface ───────────────────────────
# 신규 task도 bind 시 pm_state가 존재한다. 완료 세션이 아직 없는 marker는 1차로 추론되어야 하며,
# pm_state 부재 전용 특례나 slot 상태 폴백 없이 task state 자체가 연속성 앵커가 된다.

_T0391_BOARD = {"counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0},
                "open_tickets": [], "lint": "clean"}
_T0391_GIT = {"branch": "main", "commits": [], "working_tree": "clean"}


def test_task_empty_session_marker_labels_pm_1cha_not_placeholder(bootstrap):
    """task 생성 시 state marker → 헤더 `PM 1차` · placeholder 부재."""
    inst = bootstrap.PmBootstrap(run_git_fn=lambda a: (0, ""))
    inst._task_name = "payments-refactor"
    inst._pm_state_file = Path("/tmp/task-state")
    handoff_ctx = {
        "session_num": 1,
        "session_stale": False,
        "state_session_num": 1,
        "remaining_work": "## 남은 작업 / 사용자 발의\n\n- 아직 없음",
        "state_path": str(inst._pm_state_file),
        "fresh_slot": False,
    }
    md = inst._build_markdown(
        _T0391_BOARD, None, _T0391_GIT, None, "ts", handoff_ctx, None
    )
    assert "## PM 1차 부트스트랩" in md
    assert "PM <?>차" not in md


def test_non_task_first_session_keeps_placeholder(bootstrap):
    """비-task(솔로·_task_name None) + handoff_ctx None → 현행 placeholder/포인터 보존(회귀 0)."""
    inst = bootstrap.PmBootstrap(run_git_fn=lambda a: (0, ""))
    # _task_name 은 기본 None — task 분기 미발동.
    md = inst._build_markdown(_T0391_BOARD, None, _T0391_GIT, None, "ts", None, None)
    assert "## PM <?>차 부트스트랩" in md
    assert "log/current.md 없음 또는 entry 파싱 실패" in md
    assert "task 1차" not in md


def test_task_resume_not_forced_to_task_first_branch(bootstrap):
    """task resume(handoff_ctx 해소·session_num int) → task 첫세션 분기 미발동(현행 `PM N차` 보존)."""
    inst = bootstrap.PmBootstrap(run_git_fn=lambda a: (0, ""))
    inst._task_name = "payments-refactor"
    ctx = {"session_num": 3, "session_stale": False, "state_session_num": 2,
           "remaining_work": None, "state_path": "pm_state.md", "fresh_slot": False}
    md = inst._build_markdown(_T0391_BOARD, None, _T0391_GIT, None, "ts", ctx, None)
    assert "## PM 3차 부트스트랩" in md
    assert "task 1차" not in md
    assert "신규 task — 복구할 인계 없음" not in md


# ── T-0412: main-참조 해소 커맨드의 브랜치 도메인 검증 (`_remedy_branch_name`) ──────
# 0단계 main-참조 fault 의 해소 커맨드가 세션/task 명을 브랜치명으로 그대로 보간해, 그 이름이
# 보호브랜치이거나 이미 존재하는 브랜치면 **실행 불가능한 자기모순 안내**가 됐다(PM 4차 실측:
# task `main` 진입 → `git -C work/project_manager_1 switch -c main`). 파생 지점(remedy 문자열)에서
# 안전한 신규 브랜치명(`task/<preferred>`·`-2`…)으로 해소한다 — task 명 도메인은 안 넓힌다.


class _RemedyBoard:
    """board 대역 — `_repo_protected` 만 소비된다(`_protected_warning` 경유·DI 보존)."""

    def __init__(self, protected=("main", "master", "develop")):
        self._protected = list(protected)

    def _repo_protected(self, repo):
        return self._protected


class _RemedyPool:
    """worktree_pool 대역 — remedy 경로는 slot_path·current_branch·slot_status 만 읽는다.

    **브랜치명 수용 판정은 대역이 아니라 실 엔진**을 단다(T-0414 must-fix — 제안 쪽과 실행 쪽이
    같은 판정을 쓰는지가 요구사항이라, 대역이 자기 규칙을 흉내내면 그 요구를 검증하지 못한다):
    `_normalize_branch_name` 은 실 `worktree_pool` 모듈 함수를 그대로 붙이고 호출만 센다."""

    def __init__(self, slot_dir, *, branch="main", normalize=None):
        self._slot_dir = Path(slot_dir)
        self._branch = branch
        self.normalize_calls: list[str] = []
        if normalize is not None:
            self._normalize = normalize

    _normalize = None      # 기본은 판정기 부재(구 풀) — fail-soft 경로 테스트용.

    def slot_path(self, slot):
        return self._slot_dir

    def current_branch(self, slot, *, git_runner=None):
        return self._branch

    def slot_status(self, slot, *, git_runner=None):
        return SimpleNamespace(upstream_ok=False, upstream=None)

    def _normalize_branch_name(self, branch, *, git_runner):
        self.normalize_calls.append(branch)
        return self._normalize(branch, git_runner=git_runner)


def _remedy_pool(wp_mod, slot_dir, *, branch="main"):
    """실 worktree_pool 의 브랜치명 판정을 단 remedy 풀 대역(제안↔실행 규칙 동일성 보장)."""
    return _RemedyPool(slot_dir, branch=branch, normalize=wp_mod._normalize_branch_name)


# git 브랜치명으로 무효한 형태(실측·`git check-ref-format --branch` 거부): `:`·`~`·`^`·공백·`..`·
# `@{`·`.lock` 접미. task 명 검증(`validate_task_name`)은 이것들을 통과시키므로 remedy 후보 생성이
# 따로 걸러야 한다(T-0414 codex must-fix).
def _git_ref_name_ok(name: str) -> bool:
    if not name or name.endswith(".lock") or ".." in name or "@{" in name:
        return False
    return not any(ch in name for ch in ":~^? *[\\\t")


def _remedy_git(*existing_branches, raises=False, expand=None):
    """`show-ref`(존재) + `check-ref-format --branch`(유효성/정규화)를 모델하는 git 러너 대역.

    `existing_branches` 에 든 브랜치만 rc 0(존재). `check-ref-format --branch <name>` 은 실 git 처럼
    **정규화된 이름을 stdout 으로** 돌려준다(무효면 rc≠0). `expand` = revspec 확장 모델(원문 →
    다른 이름). `raises=True` 면 호출이 예외를 던져 fail-soft 경로(= 검사 생략·미존재 간주)를 구동."""
    calls: list[list] = []
    expand = dict(expand or {})

    def runner(args):
        calls.append(list(args))
        if raises:
            raise OSError("git 실행 실패(대역)")
        if args[2:5] == ["show-ref", "--verify", "--quiet"]:
            name = args[5][len("refs/heads/"):]
            return (0, "") if name in existing_branches else (1, "")
        if args[2:4] == ["check-ref-format", "--branch"]:
            name = args[4]
            if not _git_ref_name_ok(name):
                return 1, "fatal: invalid branch name\n"
            return 0, expand.get(name, name) + "\n"
        return 0, ""

    return runner, calls


def _remedy_inst(bootstrap, *, git_fn, protected=("main", "master", "develop")):
    return bootstrap.PmBootstrap(run_git_fn=git_fn, board=_RemedyBoard(protected))


def test_remedy_branch_prefixes_protected_preferred(bootstrap, tmp_path):
    """(a) preferred 가 보호브랜치(`main`)면 그대로 쓰지 않고 `task/main` 을 제안한다."""
    git_fn, _calls = _remedy_git()          # 기존 브랜치 없음 — 보호목록만이 배제 사유.
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    pool = _RemedyPool(tmp_path)
    assert inst._remedy_branch_name(pool, "X", "work/X_1", "main") == "task/main"


def test_remedy_branch_suffixes_when_prefixed_exists(bootstrap, tmp_path):
    """(b) `task/main` 까지 이미 존재하면 `task/main-2` 로 첫 미충돌을 고른다."""
    git_fn, _calls = _remedy_git("task/main")
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    assert inst._remedy_branch_name(_RemedyPool(tmp_path), "X", "work/X_1", "main") == "task/main-2"


def test_remedy_branch_suffix_skips_existing_numbered(bootstrap, tmp_path):
    """`task/main-2` 도 있으면 `task/main-3` — 첫 미충돌까지 계속 센다(비공허)."""
    git_fn, _calls = _remedy_git("task/main", "task/main-2")
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    assert inst._remedy_branch_name(_RemedyPool(tmp_path), "X", "work/X_1", "main") == "task/main-3"


def test_remedy_branch_keeps_plain_preferred(bootstrap, tmp_path):
    """(c) 평범한 task 명(`foo`)은 보호목록 밖 + 미존재 → 접두 없이 그대로 (오탐 0)."""
    git_fn, calls = _remedy_git("main")
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    assert inst._remedy_branch_name(_RemedyPool(tmp_path), "X", "work/X_1", "foo") == "foo"
    # 존재 확인은 그 슬롯 worktree 에서 argv-list 로 (shell 미경유·`-C <slot_dir>`).
    assert calls[0][:2] == ["-C", str(tmp_path)]
    assert calls[0][2:] == ["show-ref", "--verify", "--quiet", "refs/heads/foo"]


def test_remedy_branch_prefixes_existing_plain_name(bootstrap, tmp_path):
    """보호목록 밖이라도 **이미 존재하는** 브랜치면 그대로 쓰지 않는다(`switch -c` 실패 방지)."""
    git_fn, _calls = _remedy_git("foo")
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    assert inst._remedy_branch_name(_RemedyPool(tmp_path), "X", "work/X_1", "foo") == "task/foo"


def test_remedy_branch_fail_soft_on_git_error(bootstrap, tmp_path):
    """(e) git 호출 실패는 fail-soft — 미존재로 간주하고 안내는 계속 나간다(크래시 0)."""
    git_fn, _calls = _remedy_git(raises=True)
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    assert inst._remedy_branch_name(_RemedyPool(tmp_path), "X", "work/X_1", "foo") == "foo"
    # 보호브랜치 축은 git 무관이라 그대로 살아 있다(fail-soft ≠ 판정 포기).
    assert inst._remedy_branch_name(_RemedyPool(tmp_path), "X", "work/X_1", "main") == "task/main"


def test_remedy_branch_task_fault_consumes_helper(bootstrap, tmp_path):
    """task 모드 fault(`_task_slot_fault`)의 해소 커맨드가 헬퍼 결과를 싣는다 — task `main` 실측 케이스."""
    slot_dir = tmp_path / "work" / "X_1"
    slot_dir.mkdir(parents=True)
    git_fn, _calls = _remedy_git()
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    pool = _RemedyPool(slot_dir, branch="main")
    lease = SimpleNamespace(slot="work/X_1", repo="X", session="main", state="leased", role="work")
    fault = inst._task_slot_fault(pool, lease, "main")
    assert fault is not None
    label, _reason, resolve = fault
    assert "main-참조" in label
    # T-0414 — 엔진-매개 단일 커맨드(전환+장부 스냅 재기록 원자). 브랜치명은 T-0412 헬퍼 산출.
    assert resolve == "python3 .project_manager/tools/worktree_pool.py switch work/X_1 task/main"


def test_remedy_branch_slot_mode_consumes_helper(bootstrap, tmp_path, capsys):
    """(d) 슬롯 모드 `<repo>_<N>` 안내도 같은 헬퍼를 탄다 — 세션명 브랜치가 이미 있으면 접두형."""
    git_fn, _calls = _remedy_git("X_2")     # 슬롯 세션명과 같은 브랜치가 이미 존재.
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    pool = _RemedyPool(tmp_path, branch="main")
    lease = SimpleNamespace(slot="work/X_2", repo="X", session="", state="idle", role="work")
    rc = inst._phase0_protected_reject(pool, "X", "work/X_2", "X_2", lease)
    assert rc == 1
    err = capsys.readouterr().err
    assert "worktree_pool.py switch work/X_2 task/X_2" in err
    assert "switch work/X_2 X_2" not in err  # 충돌하는 옛 안내가 남아 있지 않다.


# ── T-0414: remedy 를 엔진-매개 단일 커맨드로 + 후보 상한 안내 (`_remedy_switch_command`) ──
# raw `git switch -c` 는 장부 스냅을 안 남겨 해소 **직후** 0단계 '기록↔live diverged' 2차 차단을
# 부른다(remedy-유발 상태전이·PM 4차 실측 왕복 2회). 안내를 `worktree_pool.py switch`(전환+스냅
# 재기록 원자)로 바꾸고, 후보 상한 도달 시엔 충돌 이름을 제시하는 대신 직접 지정을 안내한다.


def test_remedy_switch_command_is_engine_mediated(bootstrap, tmp_path):
    """해소 커맨드 = 엔진-매개 단일 커맨드(raw git 아님·복합 `&&` 아님 — PowerShell 5.x 미지원)."""
    git_fn, _calls = _remedy_git()
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    cmd = inst._remedy_switch_command(_RemedyPool(tmp_path), "X", "work/X_1", "main")
    assert cmd == "python3 .project_manager/tools/worktree_pool.py switch work/X_1 task/main"
    assert "&&" not in cmd and not cmd.startswith("git ")


def test_remedy_branch_name_returns_none_when_candidates_exhausted(bootstrap, tmp_path):
    """(i-1) 후보 상한 도달 = None — 충돌하는 마지막 후보를 그대로 돌려주지 않는다(옛 자기모순)."""
    limit = bootstrap._REMEDY_BRANCH_SUFFIX_LIMIT
    exhausted = ["foo", "task/foo"] + [f"task/foo-{n}" for n in range(2, limit + 1)]
    git_fn, _calls = _remedy_git(*exhausted)
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    assert inst._remedy_branch_name(_RemedyPool(tmp_path), "X", "work/X_1", "foo") is None


@pytest.mark.parametrize("task_name", ["fix:bug", "fix~1"])
def test_remedy_branch_rejects_ref_format_invalid_candidates(bootstrap, wp, tmp_path, task_name):
    """task 명이 git 브랜치명으로 무효면 후보에서 배제 — 접두/번호형도 여전히 무효면 None.

    `identity_args.validate_task_name` 은 `fix:bug`/`fix~1` 을 통과시키지만 `git check-ref-format
    --branch` 는 거부한다(실측 rc 128 — `:`·`~` 는 어떤 후보 형태에도 남는다). 제안 쪽에 이 검사가
    없으면 remedy 가 `switch <slot> fix:bug` 를 안내하고 실행 쪽(`worktree_pool.switch`)이
    `invalid-ref` 로 튕겨 "실행 가능한 단일 remedy" 가 그 입력에서 깨진다(codex 게이트 must-fix)."""
    git_fn, _calls = _remedy_git()
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    pool = _remedy_pool(wp, tmp_path)
    assert inst._remedy_branch_name(pool, "X", "work/X_1", task_name) is None


def test_remedy_branch_skips_invalid_then_takes_valid_variant(bootstrap, wp, tmp_path):
    """무효가 *일부* 후보에만 걸리면 유효한 첫 후보를 고른다 — `.lock` 실측 케이스.

    git 은 슬래시-구분 **컴포넌트가 `.lock` 으로 끝나는** 것만 막는다(실측: `a.lock` rc 128 ·
    `task/a.lock` rc 128 · `task/a.lock-2` rc 0). 필터가 과잉이면 실행 가능한 이름이 있는데도
    "직접 지정" 으로 떨어진다(오탐 0 sensitivity)."""
    git_fn, _calls = _remedy_git()
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    got = inst._remedy_branch_name(_remedy_pool(wp, tmp_path), "X", "work/X_1", "a.lock")
    assert got == "task/a.lock-2"


def test_remedy_switch_command_invalid_task_name_asks_explicit_branch(bootstrap, wp, tmp_path):
    """무효 task 명은 "브랜치명 직접 지정" 분기로 수렴한다 — 실행 불가 커맨드를 안내하지 않는다."""
    git_fn, _calls = _remedy_git()
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    cmd = inst._remedy_switch_command(_remedy_pool(wp, tmp_path), "X", "work/X_1", "fix:bug")
    assert "worktree_pool.py switch work/X_1 <새-브랜치명>" in cmd
    assert "직접 지정" in cmd
    assert "fix:bug" not in cmd.split("(")[0]     # 커맨드 인자로는 안 실린다(안내 문구 설명은 별개)


def test_remedy_branch_valid_name_unaffected_by_ref_filter(bootstrap, wp, tmp_path):
    """정상 task 명은 종전대로 제안된다 — ref-format 필터가 회귀를 안 만든다(sensitivity)."""
    git_fn, _calls = _remedy_git()
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    pool = _remedy_pool(wp, tmp_path)
    assert inst._remedy_branch_name(pool, "X", "work/X_1", "foo") == "foo"
    assert inst._remedy_branch_name(pool, "X", "work/X_1", "main") == "task/main"


def test_remedy_branch_skips_candidate_that_expands(bootstrap, wp, tmp_path):
    """정규화가 원문과 다르면(revspec 확장) 후보에서 배제 — 제안 이름이 다른 브랜치로 해소되면 거짓말."""
    git_fn, _calls = _remedy_git(expand={"weird": "other"})
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    got = inst._remedy_branch_name(_remedy_pool(wp, tmp_path), "X", "work/X_1", "weird")
    assert got == "task/weird"          # 확장되는 원문 후보는 건너뛰고 다음 후보


def test_remedy_branch_filter_reuses_worktree_pool_judgment(bootstrap, wp, tmp_path):
    """후보 필터가 **worktree_pool 의 판정**을 그대로 쓴다 — 판정 두 벌 재유입 방지(규칙 중복 0).

    ① 대역 풀의 판정기는 실 `worktree_pool._normalize_branch_name`(=`switch` 가 쓰는 그 함수)이고
    ② 부트스트랩이 후보마다 그걸 호출했음을 spy 로 확인하며 ③ pm_bootstrap 소스에 `check-ref-format`
    argv 를 **직접 짓는 코드가 없음**(두 번째 구현 부재)을 정적으로 못 박는다."""
    git_fn, _calls = _remedy_git("foo")
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    pool = _remedy_pool(wp, tmp_path)
    assert pool._normalize is wp._normalize_branch_name          # ① 실행 쪽과 같은 판정 함수
    assert inst._remedy_branch_name(pool, "X", "work/X_1", "foo") == "task/foo"
    assert pool.normalize_calls[:2] == ["foo", "task/foo"]       # ② 후보마다 그 판정을 탄다
    source = (TOOLS / "pm_bootstrap.py").read_text(encoding="utf-8")
    assert '"check-ref-format"' not in source                    # ③ 자체 구현 없음(argv 미생성)


def test_remedy_switch_command_exhausted_asks_explicit_branch(bootstrap, tmp_path):
    """(i-2) 후보 소진 시 안내는 이름 제안 대신 **직접 지정** — 실행 불가 안내 재생산 금지."""
    limit = bootstrap._REMEDY_BRANCH_SUFFIX_LIMIT
    exhausted = ["foo", "task/foo"] + [f"task/foo-{n}" for n in range(2, limit + 1)]
    git_fn, _calls = _remedy_git(*exhausted)
    inst = _remedy_inst(bootstrap, git_fn=git_fn)
    cmd = inst._remedy_switch_command(_RemedyPool(tmp_path), "X", "work/X_1", "foo")
    assert "worktree_pool.py switch work/X_1 <새-브랜치명>" in cmd
    assert "직접 지정" in cmd
    # 충돌이 확정된 후보(마지막 이름)를 커맨드 인자로 싣지 않는다.
    assert f"switch work/X_1 task/foo-{limit}" not in cmd
