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
    branch_out: str = "a5\n",
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
        # 슬롯 자신의 브랜치 (current_branch — symbolic-ref --short HEAD).
        if argv[:2] == ["symbolic-ref", "--short"]:
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
        if argv[:2] == ["symbolic-ref", "--short"]:
            return 0, "a5\n"
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
    # slot 뷰(내 것 ∩ 슬롯) = open 1(슬롯무관 backlog) + claimed 2(이 슬롯 진행분).
    slot_list_out = (
        "  [open   ] T-A-001  backlog     -           -\n"
        "  [claimed] T-A-010  wip a       alice/A_1   -\n"
        "  [claimed] T-A-011  wip b       alice/A_1   -\n"
    )
    slot_done_out = "  [done   ] T-A-009  done a       alice/A_1   -\n"
    # 타 세션 현황용 무렌즈 full-board claimed 조회(T-0331·must-fix 2) — 여기선 내 세션(A_1) claim
    # 만 담아 타 세션 0건(현황 줄 생략)으로 둔다(이 테스트 초점=슬롯-스코프 카운트).
    full_claimed_out = (
        "  [claimed] T-A-010  wip a       alice/A_1   -\n"
        "  [claimed] T-A-011  wip b       alice/A_1   -\n"
    )

    def fake_board(args):
        calls.append(args)
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        if args == ["list", "--status", "done", "--repo", "A", "--slot", "1"]:
            return 0, slot_done_out
        if args == ["list", "--status", "claimed"]:  # 무렌즈 전-세션 조회(T-0331).
            return 0, full_claimed_out
        if args == ["list", "--repo", "A", "--slot", "1"]:
            return 0, slot_list_out
        raise AssertionError(f"슬롯 정체성인데 슬롯-스코프 아님: {args}")

    pool = FakePool(slot_status_ret=_slot_status_obj(wp, submodules=[]))
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=pool, board_fn=fake_board)
    rc = inst.run(repo="A", slot=1)
    assert rc == 0
    out = capsys.readouterr().out

    # 1) 카운트는 슬롯 정체성(--repo A --slot 1)으로 조회(default + done) + 타 세션 현황은 무렌즈
    #    full-board claimed 로 별도 조회(T-0331 must-fix 2·전 세션·사용자 무관·슬롯-바인딩에서도 병기).
    list_calls = [c for c in calls if c[:1] == ["list"]]
    assert list_calls == [
        ["list", "--repo", "A", "--slot", "1"],
        ["list", "--status", "done", "--repo", "A", "--slot", "1"],
        ["list", "--status", "claimed"],
    ]
    assert not any("--mine" in c for c in calls), "슬롯 정체성인데 --mine 로 조회(S1 mislabel 재현)"
    # 2) 카운트 라벨 = "(slot 1)" (그 슬롯 정체성으로 조회) — user-wide "(mine)" mislabel 근절.
    assert "claimed: 2 (slot 1)" in out
    assert "open: 1 (공유 backlog·슬롯무관)" in out
    assert "done: 1 (slot 1)" in out
    assert "(mine)" not in out
    # 3) open ticket 은 슬롯무관 backlog 로 명시(카운트 slot-scope 와 혼동 방지).
    assert "open ticket (claim 가능·backlog·슬롯무관): T-A-001" in out


def test_bootstrap_slot_identity_renders_other_session_claims(bootstrap, wp, tmp_path, capsys):
    """**T-0331 must-fix 2c (positive-render)**: 슬롯 정체성(--repo A --slot 1) 부트스트랩이 무렌즈
    full-board claimed 조회로 *타 세션* claim 을 실 markdown 에 병기하고 내 세션 claim 은 뺀다.

    dormant 출하 금지(PM 결정): 스코프 뷰가 타 세션을 숨겨도, 전용 무렌즈 조회가 동일 사용자 타
    슬롯(alice/A_2) + 타 사용자(bob/A_3) claim 을 "타 세션 진행(claimed)" 줄로 실제 렌더한다.
    """
    calls: list[list[str]] = []
    slot_list_out = (
        "  [open   ] T-A-001  backlog     -           -\n"
        "  [claimed] T-A-010  wip mine    alice/A_1   -\n"
    )
    # 무렌즈 full-board claimed = 내 세션(A_1) + 타 세션(A_2 동일 사용자·A_3 타 사용자).
    # board.py cmd_list 실 포맷(고정폭 60/18) — 위치 파서(codex R3)가 요구하는 컬럼 정렬.
    full_claimed_out = "".join(
        f"  [{'claimed':7s}] {tid}  {title[:60]:60s}  {claimed:18s}  -\n"
        for tid, title, claimed in [
            ("T-A-010", "wip mine", "alice/A_1"),
            ("T-A-020", "wip a2", "alice/A_2"),
            ("T-A-030", "wip a3", "bob/A_3"),
        ]
    )

    def fake_board(args):
        calls.append(args)
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        if args == ["list", "--status", "done", "--repo", "A", "--slot", "1"]:
            return 0, ""
        if args == ["list", "--status", "claimed"]:
            return 0, full_claimed_out
        if args == ["list", "--repo", "A", "--slot", "1"]:
            return 0, slot_list_out
        raise AssertionError(f"예상 밖 board 호출: {args}")

    pool = FakePool(slot_status_ret=_slot_status_obj(wp, submodules=[]))
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=pool, board_fn=fake_board)
    rc = inst.run(repo="A", slot=1)
    assert rc == 0
    out = capsys.readouterr().out

    # 타 세션 현황 줄이 실제 markdown 에 렌더됐다 — 타 세션(A_2·A_3) 2건·내 것(A_1) 제외.
    other_line = next(ln for ln in out.splitlines() if ln.startswith("- 타 세션 진행(claimed):"))
    assert "2건" in other_line
    assert "A_2: T-A-020" in other_line and "A_3: T-A-030" in other_line
    # 내 세션 claim(A_1/T-A-010)은 타 세션 줄에 안 들어간다(오귀속 회귀 가드).
    assert "A_1" not in other_line and "T-A-010" not in other_line


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
