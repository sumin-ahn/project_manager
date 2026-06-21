"""worktree 풀 엔진 테스트 (T-0059 · ADR-0013).

repo별 worktree 풀의 슬롯 리스 라이프사이클을 검증한다:

  - alloc: idle 슬롯 리스 · idempotent(같은 세션 재진입) · resume/branch 우선 re-alloc ·
    branch 재할당(같은 슬롯 다른 branch) · 풀 소진 NeedsCreate.
  - release: 작업완료 반납 · dirty+require_clean 거부(ReleaseRefused) · 자동경로 stash.
  - reclaim_stale: pid 죽은 leased 회수(dirty→stash) · pid 살아있으면 미회수.
  - force_release: leased/dirty 무시 강제 idle 화.
  - 리스장부 동시쓰기 안전(자체 파일락) — 부모 monkeypatch 비상속 자식 spawn.
  - sensitivity: stale 판정(pid)·풀소진 핵심 로직 무력화 시 fail 재현.
  - **실 git 통합**(hermetic·임시 git repo): create_slot 이 `git worktree add` 로 실제
    슬롯 생성·branch checkout·submodule init(임시 superproject+submodule)·반납.

**hermetic 필수**: worktree_pool 모듈 전역(`REPO`·`LEASES_FILE`·`LEASES_LOCK`·`WORK_DIR`)은
import 시점에 실 repo 절대경로로 굳는다 — tmp 프로젝트로 재지정해 실 `.project_manager` 를
절대 건드리지 않는다(test_board_concurrency.py 의 monkeypatch hermetic 패턴 동류). git DI
seam(주입 가능 runner)으로 단위테스트는 mock git, 통합테스트만 실 임시 git repo 를 쓴다.
board.py 는 import 하지 않는다(touches 격리·병렬충돌 회피·자체 파일락 검증).
"""
from __future__ import annotations

import importlib.util
import multiprocessing as mp
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
SYNC_TIMEOUT = 60


# ── 모듈 로드 + tmp 재배선 (부모·자식 공용) ─────────────────────────────────


def _load_wp_bound(proj: Path):
    """worktree_pool.py 를 새로 로드하고 경로 전역을 `proj` tmp 루트로 재바인딩한다.

    부모(monkeypatch)와 자식(프로세스 경계로 monkeypatch 미상속) 양쪽이 같은 배선을 쓰도록
    함수로 추출. import 시점에 굳은 실 REPO 경로를 tmp 로 전부 덮어쓴다 — 리스장부·락·work/
    풀 루트 포함(동시성에 관여하는 전역 전부).
    """
    spec = importlib.util.spec_from_file_location("wp_test", TOOLS / "worktree_pool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    local = proj / ".project_manager" / ".local"
    overrides = {
        "REPO": proj,
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "LEASES_LOCK": local / "worktree-leases.lock",
        "WORK_DIR": proj / "work",
        "REPOS_DIR": proj / ".repos",   # worktree 공유 .git 원(bare) tmp 재배선·ADR-0011 §31
        "REPO_HOOKS_DIR": local / "repo-hooks",  # 보호 pre-push 훅 디렉토리 tmp 재배선(T-0076)
    }
    for name, val in overrides.items():
        setattr(mod, name, val)
    return mod


@pytest.fixture
def proj(tmp_path):
    """tmp 프로젝트 루트 — .project_manager/.local + work/ + .repos/ 골격."""
    p = tmp_path / "proj"
    (p / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    (p / "work").mkdir(parents=True, exist_ok=True)
    (p / ".repos").mkdir(parents=True, exist_ok=True)
    return p


def _mk_bare_placeholder(wp, repo: str) -> Path:
    """`.repos/<repo>.git` 자리(디렉토리)를 만든다 — bare 부재 가드 통과용(mock git 경로).

    bare 부재 가드(`create_slot`)는 *경로 존재*만 본다(실 git 무관). mock git_runner 로
    worktree add 를 모킹하는 단위테스트가 가드를 통과하도록 placeholder 디렉토리를 둔다.
    실 git 통합테스트는 `git clone --bare` 로 진짜 bare 를 만든다(이 헬퍼 미사용).
    """
    bare = wp.bare_repo_path(repo)
    bare.mkdir(parents=True, exist_ok=True)
    return bare


@pytest.fixture
def wp(proj):
    """tmp-바인딩 worktree_pool 모듈."""
    return _load_wp_bound(proj)


# ── mock git runner (단위테스트용 DI seam) ───────────────────────────────────


class FakeGit:
    """주입형 git runner — 호출을 기록하고 미리 정한 (rc, out)을 돌려준다.

    `clean` 이면 status --porcelain 이 빈 문자열(=clean), `dirty` 면 변경 1줄을 돌려준다.
    실 git 을 안 쓰고 dirty/stash/checkout/worktree-add/submodule 경로를 결정적으로 검증.

    **live branch 모델(ADR-0013 amend T-0072)**: `head` 가 슬롯 worktree 의 현재 HEAD(브랜치)
    를 모델링한다 — `symbolic-ref --short HEAD` 가 그걸 돌려주고, `checkout <b>`/`-B <b>` 는
    실 git 처럼 head 를 갱신한다(`current_branch(slot)` live 조회·alloc 매칭이 이걸 본다).
    `head=None` 이면 detached(실 git 처럼 `symbolic-ref` 가 rc≠0 → current_branch None).
    """

    def __init__(self, *, dirty: bool = False, head: "str | None" = None):
        self.dirty = dirty
        self.head = head        # 슬롯 worktree 의 현재 브랜치(=HEAD)·checkout 으로 갱신.
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[:2] == ["status", "--porcelain"]:
            return (0, " M file.py\n") if self.dirty else (0, "")
        if argv == ["symbolic-ref", "--short", "HEAD"]:
            # detached(head=None) → 실 git 처럼 rc≠0(symbolic ref 아님·→ current_branch None).
            return (1, "fatal: ref HEAD is not a symbolic ref\n") if self.head is None \
                else (0, self.head + "\n")
        if argv[:1] == ["checkout"]:
            # `checkout <b>` 또는 `checkout -B <b>` — 실 git 처럼 head 를 갱신(브랜치 전환).
            self.head = argv[-1]
            return (0, "")
        return (0, "")

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


def _seed(wp, *leases):
    """리스장부에 엔트리를 직접 심는다(테스트 전제 구성)."""
    with wp._lease_lock():
        wp._write_ledger(list(leases))


def _lease(wp, *, slot, repo, session="s1", pid=None, state="leased"):
    # branch 는 더는 Lease 권위 필드가 아니다(ADR-0013 amend T-0072 — git=진실·장부 저장
    # 폐지). 슬롯의 live 브랜치는 FakeGit(head=...) 으로 모델링한다(current_branch 조회).
    return wp.Lease(slot=slot, repo=repo, session=session,
                    pid=os.getpid() if pid is None else pid, started="t", state=state)


# ════════════════════════════════════════════════════════════════════════
# alloc
# ════════════════════════════════════════════════════════════════════════


def test_alloc_idle_slot_leases_it(wp):
    """idle 슬롯이 있으면 그걸 leased 로 전이해 리스한다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"))
    git = FakeGit()
    lease = wp.alloc("A", session="me", git_runner=git)
    assert lease.slot == "work/A_1"
    assert lease.state == "leased"
    assert lease.session == "me"
    assert lease.pid == os.getpid()
    # 장부에 반영됐는지.
    assert wp.list_leases()[0].state == "leased"


def test_alloc_idempotent_returns_existing_lease(wp):
    """같은 세션이 이미 이 repo 에 leased 슬롯을 가지면 그걸 반환(get-or-create-my-lease)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="me", state="leased"))
    git = FakeGit()
    first = wp.alloc("A", session="me", git_runner=git)
    second = wp.alloc("A", session="me", git_runner=git)
    assert first.slot == second.slot == "work/A_1"
    # 슬롯이 두 개로 늘지 않음(idempotent).
    assert len([l for l in wp.list_leases() if l.repo == "A"]) == 1


def test_alloc_resume_reattaches_same_branch_slot(wp):
    """resume(작업스트림 브랜치)으로 같은 브랜치의 슬롯을 re-alloc(회전 연속성).

    매칭은 슬롯 worktree 의 live HEAD(`current_branch(slot)`·ADR-0013 amend T-0072) —
    FakeGit(head="a5-pay") 가 그 슬롯이 a5-pay 를 체크아웃 중임을 모델링한다(저장 필드 아님).
    """
    # 이전 작업스트림의 슬롯 — branch 매칭이 아니라 슬롯 live HEAD 매칭으로 잡는다.
    _seed(wp, _lease(wp, slot="work/A_2", repo="A",
                     session="old", pid=999999, state="leased"))
    git = FakeGit(head="a5-pay")  # 슬롯 live HEAD = a5-pay
    lease = wp.alloc("A", resume="a5-pay", session="new", git_runner=git)
    assert lease.slot == "work/A_2"
    assert wp.current_branch("work/A_2", git_runner=git) == "a5-pay"  # live HEAD 유지
    assert lease.session == "new"
    # 같은 슬롯 재체크아웃 발생.
    assert git.did("checkout", "a5-pay")


def test_alloc_branch_reassign_same_slot_different_branch(wp):
    """branch 재할당 — 같은 세션 슬롯에서 다른 branch 로 재체크아웃(슬롯 유지·live HEAD 전환).

    슬롯 live HEAD(a1-old)가 요청 branch(a2-new)와 다르면 재체크아웃 — checkout 이 git HEAD
    를 바꾼다(장부엔 branch 를 쓰지 않는다·ADR-0013 amend T-0072·git=진실).
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A",
                     session="me", state="leased"))
    git = FakeGit(head="a1-old")  # 슬롯 live HEAD = a1-old
    lease = wp.alloc("A", branch="a2-new", session="me", git_runner=git)
    assert lease.slot == "work/A_1"          # 같은 슬롯
    assert git.did("checkout", "a2-new")     # 재체크아웃(live HEAD 와 다르므로)
    # checkout 이 슬롯 live HEAD 를 a2-new 로 전환(git=진실).
    assert wp.current_branch("work/A_1", git_runner=git) == "a2-new"


def test_alloc_pool_exhausted_raises_needscreate(wp):
    """idle 슬롯이 없으면 NeedsCreate(호출부 bootstrap 사용자 게이트)."""
    # 같은 세션 아닌 leased 슬롯만 있고 idle 없음 → 풀 소진.
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other",
                     pid=os.getpid(), state="leased"))
    git = FakeGit()
    with pytest.raises(wp.NeedsCreate) as ei:
        wp.alloc("A", session="me", git_runner=git)
    assert ei.value.repo == "A"


def test_alloc_empty_pool_raises_needscreate(wp):
    """장부가 비어있으면(슬롯 0) 풀 소진 → NeedsCreate."""
    git = FakeGit()
    with pytest.raises(wp.NeedsCreate):
        wp.alloc("B", session="me", git_runner=git)


def test_alloc_reclaims_stale_before_leasing(wp):
    """alloc 진입 시 stale(pid 죽음) 슬롯을 회수해 그걸 재리스할 수 있다(풀 가용성 회복)."""
    # pid 죽은 leased 슬롯 하나뿐 — 회수 안 하면 풀 소진이지만 alloc 이 회수 후 리스.
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="dead",
                     pid=999999, state="leased"))
    git = FakeGit()  # clean → stash 안 함
    lease = wp.alloc("A", session="me", git_runner=git)
    assert lease.slot == "work/A_1"
    assert lease.session == "me"


# ── alloc checkout 실패 negative (codex must-fix 2·ADR-0013) ──────────────────
#
# checkout 실패(rc≠0)를 fail-soft 로 무시하면 장부의 branch/state/session 을 성공처럼
# 갱신해 장부↔실제 worktree branch 가 어긋난다. rc 확인 → 실패면 CheckoutFailed raise·
# 기존 리스 상태 보존(부분 갱신 차단)이 fix. 아래 세 케이스가 alloc 의 세 checkout 경로.


class _CheckoutFailGit:
    """checkout(및 -B 재시도) 만 rc≠0 으로 실패하는 주입 runner — 그 외는 성공.

    `git checkout <b>` 와 폴백 `git checkout -B <b>` 둘 다 실패시켜 checkout 자체가
    실패한 상황을 모델링한다(브랜치 충돌·잠긴 worktree 등). `head` = 슬롯의 현재 live
    HEAD(symbolic-ref 가 돌려줌·alloc 의 live 매칭이 본다·ADR-0013 amend T-0072) — checkout 은
    실패하므로 head 를 *바꾸지 못한다*(부분 전이 negative 검증의 핵심).
    """

    def __init__(self, *, head: "str | None" = None):
        self.head = head
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[:1] == ["checkout"]:
            return (1, "fatal: checkout failed")  # head 미갱신(실패).
        if argv[:2] == ["status", "--porcelain"]:
            return (0, "")
        if argv == ["symbolic-ref", "--short", "HEAD"]:
            return (1, "fatal: ref HEAD is not a symbolic ref\n") if self.head is None \
                else (0, self.head + "\n")
        return (0, "")


def test_alloc_idempotent_checkout_failure_preserves_ledger(wp):
    """case1(idempotent·branch 변경) checkout 실패 → CheckoutFailed·리스 state 보존.

    슬롯 live HEAD(a1-old)가 요청 branch(a2-new)와 달라 checkout 시도 → 실패 → raise.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A",
                     session="me", state="leased"))
    git = _CheckoutFailGit(head="a1-old")  # 슬롯 live HEAD = a1-old
    with pytest.raises(wp.CheckoutFailed):
        wp.alloc("A", branch="a2-new", session="me", git_runner=git)
    # 리스 state/session 그대로(성공처럼 갱신 안 됨). branch 는 장부 권위 아님 — live HEAD 도
    # 그대로 a1-old(checkout 실패라 안 바뀜).
    after = wp.list_leases()[0]
    assert after.state == "leased"
    assert wp.current_branch("work/A_1", git_runner=git) == "a1-old"


def test_alloc_resume_realloc_checkout_failure_preserves_ledger(wp):
    """case2(resume/branch re-alloc) checkout 실패 → CheckoutFailed·state/session 미갱신.

    살아있는 pid 의 idle 슬롯(live HEAD 가 그 브랜치)으로 re-alloc 을 유도한다 —
    reclaim_stale(진입 회수)이 끼어들지 않게 idle 로 seed(live HEAD 매칭만으로 case2 진입).
    """
    _seed(wp, _lease(wp, slot="work/A_2", repo="A",
                     session="", pid=0, state="idle"))
    git = _CheckoutFailGit(head="a5-pay")  # 슬롯 live HEAD = a5-pay (매칭)
    with pytest.raises(wp.CheckoutFailed):
        wp.alloc("A", resume="a5-pay", session="new", git_runner=git)
    # case2 가 state/session/pid 를 성공처럼 갱신하지 않음(checkout 선행·실패 시 raise).
    after = wp.list_leases()[0]
    assert after.session == ""        # "new" 로 갱신 안 됨
    assert after.state == "idle"      # leased 전이 안 됨


def test_alloc_idle_lease_checkout_failure_preserves_idle(wp):
    """case3(idle 리스·branch 변경) checkout 실패 → CheckoutFailed·idle 상태 보존(leased 전이 안 됨)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A",
                     session="", pid=0, state="idle"))
    git = _CheckoutFailGit(head="a1-old")  # 슬롯 live HEAD = a1-old
    with pytest.raises(wp.CheckoutFailed):
        wp.alloc("A", branch="a2-new", session="me", git_runner=git)
    # idle 그대로 — 부분 leased 전이 차단.
    after = wp.list_leases()[0]
    assert after.state == "idle"
    assert after.session == ""


def test_alloc_checkout_success_updates_ledger_sensitivity(wp):
    """sensitivity 대조 — checkout 성공(FakeGit rc0)이면 state/live HEAD 정상 갱신.

    위 실패 negative 와 대조: rc 확인 가드를 제거하면 실패 case 도 이렇게 갱신돼버려
    (부분 전이) negative 들이 fail 한다. checkout 이 슬롯 live HEAD 를 a2-new 로 전환(git=진실).
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A",
                     session="", pid=0, state="idle"))
    git = FakeGit(head="a1-old")  # 슬롯 live HEAD = a1-old·checkout rc0
    lease = wp.alloc("A", branch="a2-new", session="me", git_runner=git)
    assert lease.state == "leased"
    assert lease.session == "me"
    # checkout 이 슬롯 live HEAD 를 a2-new 로 전환(장부 저장 아님·ADR-0013 amend T-0072).
    assert wp.current_branch("work/A_1", git_runner=git) == "a2-new"


# ════════════════════════════════════════════════════════════════════════
# release
# ════════════════════════════════════════════════════════════════════════


def test_release_clean_slot_goes_idle(wp):
    """clean 슬롯 release → idle 전이·session/pid 비움(재사용 컨테이너로 풀 반납)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="me", state="leased"))
    git = FakeGit(dirty=False)
    lease = wp.release("work/A_1", git_runner=git)
    assert lease.state == "idle"
    assert lease.session == ""
    assert lease.pid == 0


def test_release_dirty_require_clean_refused(wp, proj):
    """dirty + require_clean=True → ReleaseRefused(작업 유실 방지). 슬롯 폴더 존재 전제."""
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)  # _is_dirty 가 path.exists 봄
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="me", state="leased"))
    git = FakeGit(dirty=True)
    with pytest.raises(wp.ReleaseRefused):
        wp.release("work/A_1", require_clean=True, git_runner=git)
    # 거부됐으니 여전히 leased.
    assert wp.list_leases()[0].state == "leased"


def test_release_dirty_auto_path_stashes(wp, proj):
    """require_clean=False(자동경로) + dirty → stash 보존 후 idle 화."""
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="me", state="leased"))
    git = FakeGit(dirty=True)
    lease = wp.release("work/A_1", require_clean=False, git_runner=git)
    assert lease.state == "idle"
    assert git.did("stash", "push")


def test_release_unknown_slot_raises(wp):
    """장부에 없는 슬롯 release → KeyError."""
    git = FakeGit()
    with pytest.raises(KeyError):
        wp.release("work/Z_9", git_runner=git)


# ════════════════════════════════════════════════════════════════════════
# reclaim_stale
# ════════════════════════════════════════════════════════════════════════


def test_reclaim_stale_recovers_dead_pid(wp):
    """pid 죽은 leased 슬롯을 회수해 idle 화하고 슬롯 리스트를 반환한다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="dead",
                     pid=999999, state="leased"))
    git = FakeGit(dirty=False)
    reclaimed = wp.reclaim_stale(git_runner=git)
    assert reclaimed == ["work/A_1"]
    assert wp.list_leases()[0].state == "idle"


def test_reclaim_stale_keeps_alive_pid(wp):
    """pid 살아있는 leased 슬롯은 회수하지 않는다(조용하지만 작업 중 오판 방지)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="me",
                     pid=os.getpid(), state="leased"))
    git = FakeGit()
    reclaimed = wp.reclaim_stale(git_runner=git)
    assert reclaimed == []
    assert wp.list_leases()[0].state == "leased"


def test_reclaim_stale_stashes_dirty_before_idle(wp, proj):
    """stale 회수 시 dirty 면 stash 로 작업 보존 후 idle 화."""
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="dead",
                     pid=999999, state="leased"))
    git = FakeGit(dirty=True)
    reclaimed = wp.reclaim_stale(git_runner=git)
    assert reclaimed == ["work/A_1"]
    assert git.did("stash", "push")


def test_reclaim_stale_pid_logic_sensitivity(wp, monkeypatch):
    """sensitivity — pid 생존 판정을 무력화(항상 살아있음)하면 stale 가 회수 안 된다.

    `_pid_alive` 가 stale 판정의 load-bearing 로직임을 박제한다: 죽은 pid 도 살아있다고
    오판하면(항상 True) 회수가 0 이 된다 = 풀이 영영 안 풀림. 정상 로직은 죽은 pid 를 회수한다.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="dead",
                     pid=999999, state="leased"))
    git = FakeGit()
    # 정상: 죽은 pid 회수.
    assert wp.reclaim_stale(git_runner=git) == ["work/A_1"]

    # 무력화: pid 가 항상 살아있다고 보면 회수 0(같은 죽은 pid 라도).
    _seed(wp, _lease(wp, slot="work/A_2", repo="A", session="dead",
                     pid=999999, state="leased"))
    monkeypatch.setattr(wp, "_pid_alive", lambda pid: True)
    assert wp.reclaim_stale(git_runner=git) == [], "pid 판정 무력화 시 stale 회수돼선 안 됨"


# ════════════════════════════════════════════════════════════════════════
# force_release
# ════════════════════════════════════════════════════════════════════════


def test_force_release_idles_leased_slot(wp):
    """force_release — leased 슬롯을 강제로 idle 화(수동 백스톱)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="stuck", state="leased"))
    git = FakeGit()
    lease = wp.force_release("work/A_1", git_runner=git)
    assert lease is not None and lease.state == "idle"


def test_force_release_dirty_still_idles_with_stash(wp, proj):
    """force_release — dirty 라도 거부 없이 idle 화하되 stash 로 작업 보존 시도."""
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="stuck", state="leased"))
    git = FakeGit(dirty=True)
    lease = wp.force_release("work/A_1", git_runner=git)
    assert lease.state == "idle"
    assert git.did("stash", "push")


def test_force_release_unknown_slot_returns_none(wp):
    """장부에 없는 슬롯 force_release → None(이미 정리됨·무해)."""
    git = FakeGit()
    assert wp.force_release("work/Z_9", git_runner=git) is None


# ════════════════════════════════════════════════════════════════════════
# create_slot (풀 확장 — NeedsCreate 게이트 통과 후·mock git)
# ════════════════════════════════════════════════════════════════════════


def test_create_slot_adds_worktree_and_submodule_init(wp):
    """create_slot — worktree add + submodule init + 장부 leased 등록(번호 자동)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert lease.slot == "work/A_1"
    assert lease.state == "leased"
    # branch 파라미터는 worktree add `-B <branch>` 를 구동(checkout)하지만 장부엔 저장하지
    # 않는다(ADR-0013 amend T-0072 — git=진실). worktree add 가 `-B a1` 로 불렸는지 검증.
    assert git.did("worktree", "add", "-B", "a1")
    assert git.did("worktree", "add")
    # `--force`: bare 에서 만든 fresh 슬롯의 worktree+submodule edge 강제 init(T-0067).
    assert git.did("submodule", "update", "--init", "--recursive", "--force")
    assert wp.list_leases()[0].slot == "work/A_1"


def test_create_slot_picks_next_free_number(wp):
    """create_slot — 기존 슬롯 번호를 회피해 다음 빈 번호를 쓴다(`<repo>_<N>`)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="x", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="y", state="idle"))
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", session="me", git_runner=git)
    assert lease.slot == "work/A_3"


def test_create_slot_skip_submodule_when_disabled(wp):
    """init_submodules=False → submodule init 호출 안 함."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.create_slot("A", session="me", init_submodules=False, git_runner=git)
    assert not git.did("submodule")


def test_create_slot_worktree_add_failure_raises(wp):
    """worktree add 가 비0 → RuntimeError(불완전 슬롯 등록 방지)."""
    _mk_bare_placeholder(wp, "A")
    def failing(argv):
        if argv[:2] == ["worktree", "add"]:
            return (1, "fatal: ...")
        return (0, "")
    with pytest.raises(RuntimeError):
        wp.create_slot("A", session="me", git_runner=failing)
    # 실패 시 장부에 슬롯 등록 안 됨.
    assert wp.list_leases() == []


def test_create_slot_submodule_init_failure_raises_before_register(wp):
    """submodule init 이 비0 → leased 장부 등록 *전에* RuntimeError(불완전 슬롯 차단·ADR-0013).

    negative(codex must-fix 3): rc 무시(fail-soft)면 submodule 미초기화 슬롯이 leased 로
    등록돼 장부에 불완전 슬롯이 박힌다. rc 확인 → 등록 전 raise 가 그걸 막는다.
    """
    _mk_bare_placeholder(wp, "A")
    def failing(argv):
        if argv[:1] == ["submodule"]:
            return (1, "fatal: submodule init failed")
        return (0, "")  # worktree add 등은 성공
    with pytest.raises(RuntimeError):
        wp.create_slot("A", branch="a1", session="me", git_runner=failing)
    # 등록 전 raise → 장부에 슬롯 0 (불완전 슬롯 미등록).
    assert wp.list_leases() == []


def test_create_slot_submodule_init_success_registers(wp):
    """sensitivity 대조 — submodule rc0(성공)이면 슬롯이 정상 leased 등록된다.

    위 failure negative 와 대조: 같은 경로에서 rc 만 0/1 로 갈려 등록/raise 가 갈린다 →
    rc 확인이 유일한 방어선임을 보인다(가드 제거 시 failure case 도 등록돼 negative fail).
    """
    _mk_bare_placeholder(wp, "A")
    def succeeding(argv):
        return (0, "")  # worktree add·submodule 모두 성공
    lease = wp.create_slot("A", branch="a1", session="me", git_runner=succeeding)
    assert lease.state == "leased"
    assert len(wp.list_leases()) == 1


def test_create_slot_submodule_init_uses_force(wp):
    """create_slot 의 submodule 명령은 정확히 `--init --recursive --force` 다(T-0067).

    bare 에서 만든 fresh 슬롯의 worktree+submodule edge 서 plain `--init` 이 체크아웃 못 하는
    상태를 강제 init(실 Windows multi-PM 파일럿 블로커·spike §8-4(d)). did() prefix 매칭이 아니라
    *정확한* argv 를 검사해 `--force` 누락이 회귀로 잡히게 한다.
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.create_slot("A", branch="a1", session="me", git_runner=git)
    sub_calls = [c for c in git.calls if c[:1] == ["submodule"]]
    assert sub_calls == [["submodule", "update", "--init", "--recursive", "--force"]]


def test_create_slot_submodule_init_failure_message_surfaces_rc(wp):
    """rc≠0 + 빈 out(Windows 인코딩 캡처 유실) 에도 에러 메시지에 rc + argv 가 실린다(T-0067).

    plain 메시지가 `out` 만 실으면 빈 에러(`git submodule init failed: ''`)로 다음 사람이
    막힌다 — rc 와 실행한 git argv 를 surface 해 진단 가능하게 한다.
    """
    _mk_bare_placeholder(wp, "A")
    def failing_empty(argv):
        if argv[:1] == ["submodule"]:
            return (1, "")  # 비0 + 빈 out (Windows 캡처 유실 재현)
        return (0, "")
    with pytest.raises(RuntimeError) as exc:
        wp.create_slot("A", branch="a1", session="me", git_runner=failing_empty)
    msg = str(exc.value)
    assert "rc=1" in msg
    assert "submodule" in msg  # 실행한 argv 가 메시지에 노출
    # 등록 전 raise → 불완전 슬롯 미등록(기존 계약 유지).
    assert wp.list_leases() == []


# ════════════════════════════════════════════════════════════════════════
# create_slot base 브랜치 — 슬롯 브랜치를 base 에서 파생 (T-0075)
# ════════════════════════════════════════════════════════════════════════


def test_create_slot_base_derives_slot_branch(wp):
    """create_slot(base=) → `git worktree add -b <repo>_<N> <path> <base>` (T-0075).

    base 가 주어지면 슬롯 브랜치 `<repo>_<N>`(슬롯 식별자·T-0072 정합)를 *그 base 에서
    파생*한다 — bare HEAD 가 아닌 의도한 base(develop 등)에서 슬롯 작업 브랜치를 판다.
    주입 git_runner 로 정확한 argv 를 검증한다.
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", base="develop", session="me", git_runner=git)
    assert lease.slot == "work/A_1"
    # 정확한 argv — 슬롯 브랜치 이름은 `A_1`(work/ 접두 없음·슬롯 식별자), base=develop.
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    assert add_calls == [["worktree", "add", "-b", "A_1", str(wp.slot_path("work/A_1")),
                          "develop"]]


def test_create_slot_base_none_is_current_behavior(wp):
    """create_slot(base 미지정) → `git worktree add <path>`(bare HEAD·현행 회귀 0·T-0075).

    base=None 이면 -b/-B 어느 ref 도 안 주고 bare HEAD 에서 따는 현행 동작 그대로 —
    base 도입이 기존 경로를 안 건드림을 정확한 argv 로 박는다.
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.create_slot("A", session="me", git_runner=git)
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    assert add_calls == [["worktree", "add", str(wp.slot_path("work/A_1"))]]


def test_create_slot_branch_takes_precedence_over_base(wp):
    """branch 와 base 둘 다 주면 branch 우선(`-B <branch>`) — base 무시 (T-0075).

    branch 는 명시 작업스트림 할당(create-or-reset)이고 base 는 슬롯 자동 브랜치 파생용 —
    branch 가 지정되면 그 의미가 우선한다(상호배타·branch 분기가 먼저).
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.create_slot("A", branch="feat-x", base="develop", session="me", git_runner=git)
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    assert add_calls == [["worktree", "add", "-B", "feat-x",
                          str(wp.slot_path("work/A_1"))]]


def test_create_slot_base_picks_next_free_number_in_branch_name(wp):
    """base 파생 슬롯 브랜치 이름이 다음 빈 슬롯 번호를 따른다(`<repo>_<N>`·T-0075)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="x", state="leased"))
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", base="develop", session="me", git_runner=git)
    assert lease.slot == "work/A_2"
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    # 슬롯 번호 2 → 브랜치 이름 `A_2`.
    assert add_calls == [["worktree", "add", "-b", "A_2", str(wp.slot_path("work/A_2")),
                          "develop"]]


# ════════════════════════════════════════════════════════════════════════
# Lease.test_cmd — 슬롯 바인딩 회귀명령 (T-0066 · ADR-0014 amend)
# ════════════════════════════════════════════════════════════════════════


def test_lease_test_cmd_serialization_round_trip(wp):
    """Lease.test_cmd 가 to_dict/from_dict round-trip 으로 보존된다(장부 직렬화 포함)."""
    lease = wp.Lease(slot="work/A_1", repo="A", session="me",
                     pid=123, started="t", state="leased", test_cmd="ctest -R hil1")
    d = lease.to_dict()
    assert d["test_cmd"] == "ctest -R hil1"
    assert "branch" not in d  # branch 는 장부 직렬화 안 함(ADR-0013 amend T-0072·git=진실)
    restored = wp.Lease.from_dict(d)
    assert restored.test_cmd == "ctest -R hil1"
    assert restored == lease  # __eq__ = to_dict 동등 → test_cmd 포함


def test_lease_test_cmd_default_none(wp):
    """test_cmd 미지정 시 None(기존 호출 무영향) · to_dict 에 None 으로 직렬화."""
    lease = wp.Lease(slot="work/A_1", repo="A", session="me",
                     pid=1, started="t", state="leased")
    assert lease.test_cmd is None
    assert lease.to_dict()["test_cmd"] is None


def test_lease_from_dict_legacy_ledger_test_cmd_none(wp):
    """**하위호환** — test_cmd 필드 없는 구 장부 dict 로드 시 None(스키마 진화 graceful)."""
    legacy = {"slot": "work/A_1", "repo": "A", "branch": "a1", "session": "me",
              "pid": 7, "started": "t", "state": "leased"}  # test_cmd 키 없음
    lease = wp.Lease.from_dict(legacy)
    assert lease.test_cmd is None


def test_read_ledger_legacy_file_without_test_cmd_loads_none(wp):
    """**하위호환**(파일 레벨) — test_cmd 없는 기존 장부 *파일* 을 _read_ledger → None."""
    import json
    legacy = {"leases": [
        {"slot": "work/A_1", "repo": "A", "branch": "a1", "session": "me",
         "pid": 7, "started": "t", "state": "leased"},  # test_cmd 필드 부재(구 장부)
    ]}
    wp.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    wp.LEASES_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    with wp._lease_lock():
        leases = wp._read_ledger()
    assert len(leases) == 1
    assert leases[0].test_cmd is None


def test_create_slot_binds_test_cmd_to_lease(wp):
    """create_slot(test_cmd=) → 생성 Lease 에 저장되고 장부에 직렬화된다(T-0066)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", session="me", git_runner=git, test_cmd="make hil2")
    assert lease.test_cmd == "make hil2"
    # 장부에 영속화 — list_leases 가 같은 슬롯의 test_cmd 를 돌려준다.
    persisted = next(l for l in wp.list_leases() if l.slot == lease.slot)
    assert persisted.test_cmd == "make hil2"


def test_create_slot_test_cmd_default_none(wp):
    """create_slot 기본 호출(test_cmd 미지정) → None(기존 호출 무영향·하위호환)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", session="me", git_runner=git)
    assert lease.test_cmd is None


def test_create_slot_round_trips_test_cmd_through_ledger(wp):
    """create_slot(test_cmd=) 후 장부 파일을 다시 read → 같은 test_cmd(직렬화 왕복·하위호환)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.create_slot("A", session="me", git_runner=git, test_cmd="ninja test")
    # 새 _read_ledger 로 파일에서 재로드 — to_dict→파일→from_dict 전 경로 검증.
    with wp._lease_lock():
        reloaded = wp._read_ledger()
    assert reloaded[0].test_cmd == "ninja test"


def test_create_slot_worktree_add_runs_in_bare_context(wp, monkeypatch):
    """create_slot 의 add_runner 가 `.repos/<repo>.git` bare 를 가리키는지(컨텍스트 배선·ADR-0011 §31).

    DI seam — git_runner 미주입이면 add_runner = `_real_git_runner(bare)`. `_real_git_runner(cwd)`
    가 어떤 cwd 로 바인딩되는지 캡처해 그게 family bare(multi-PM 루트 REPO 아님)인지 결정적으로 검증한다
    (`git -C <cwd>` 로 실행하므로 cwd=bare 면 add 가 bare 컨텍스트에서 일어난다).
    """
    _mk_bare_placeholder(wp, "A")
    captured = []

    def spy_real_git_runner(cwd):
        captured.append(cwd)
        return FakeGit()  # 모든 git 호출 성공 stub(add 성공)

    monkeypatch.setattr(wp, "_real_git_runner", spy_real_git_runner)
    wp.create_slot("A", branch="a1", session="me", init_submodules=False)
    assert captured[0] == wp.bare_repo_path("A"), \
        f"worktree add 컨텍스트가 family bare 가 아님: {captured[0]!r}"


def test_create_slot_missing_bare_raises_guard(wp):
    """bare 부재 가드 — `.repos/<repo>.git` 없으면 BareRepoMissing(multi-PM 폴백 금지·ADR-0011 §31).

    placeholder 를 안 만들고 create_slot → 명시 에러. mock git 이라도 가드(*경로 존재* 계약)가
    먼저 걸려 worktree add 호출 전에 막힌다 → 장부에 슬롯 0(침묵 폴백·불완전 슬롯 0).
    """
    git = FakeGit()
    with pytest.raises(wp.BareRepoMissing):
        wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert not git.did("worktree", "add"), "가드 전에 worktree add 가 불림(폴백 위험)"
    assert wp.list_leases() == []


# ════════════════════════════════════════════════════════════════════════
# set_test_cmd — 기존 슬롯 리스의 test_cmd 갱신 (T-0069 · ADR-0014 amend)
# flock(_lease_lock) + atomic write(_write_ledger) 재사용 · slot 부재 KeyError.
# 콘솔 [b]·"나중에 변경" 의 엔진 진입점.
# ════════════════════════════════════════════════════════════════════════


def _lease_tc(wp, *, slot, repo, test_cmd=None, session="s1", state="leased"):
    """test_cmd 를 실은 Lease 시드 헬퍼(_lease 는 test_cmd 미노출)."""
    return wp.Lease(slot=slot, repo=repo, session=session,
                    pid=os.getpid(), started="t", state=state, test_cmd=test_cmd)


def test_set_test_cmd_updates_existing_lease(wp):
    """set_test_cmd(slot, cmd) → 그 슬롯 리스의 test_cmd 갱신 + 갱신된 Lease 반환."""
    _seed(wp, _lease_tc(wp, slot="work/A_1", repo="A", test_cmd="pytest -q"))
    updated = wp.set_test_cmd("work/A_1", "ctest -R hil2")
    assert updated.slot == "work/A_1"
    assert updated.test_cmd == "ctest -R hil2"


def test_set_test_cmd_persists_atomically_through_ledger(wp):
    """갱신이 장부에 atomic 영속화 — 새 _read_ledger 가 바뀐 test_cmd 를 본다(flock+atomic).

    set_test_cmd 는 create_slot 의 lease test_cmd 바인딩과 같은 `_lease_lock` +
    `_write_ledger`(tmp→os.replace) 경로를 재사용한다 — 파일에서 재로드해 영속을 확인한다.
    """
    _seed(wp, _lease_tc(wp, slot="work/A_1", repo="A", test_cmd=None))
    wp.set_test_cmd("work/A_1", "ninja test")
    with wp._lease_lock():
        reloaded = wp._read_ledger()
    assert next(l for l in reloaded if l.slot == "work/A_1").test_cmd == "ninja test"


def test_set_test_cmd_leaves_other_slots_untouched(wp):
    """다른 슬롯의 test_cmd 는 안 건드린다(타깃 슬롯만 갱신·read-modify-write 격리)."""
    _seed(
        wp,
        _lease_tc(wp, slot="work/A_1", repo="A", test_cmd="a-cmd"),
        _lease_tc(wp, slot="work/A_2", repo="A", test_cmd="b-cmd"),
    )
    wp.set_test_cmd("work/A_1", "new-cmd")
    by_slot = {l.slot: l.test_cmd for l in wp.list_leases()}
    assert by_slot["work/A_1"] == "new-cmd"
    assert by_slot["work/A_2"] == "b-cmd"  # 미변경


def test_set_test_cmd_none_clears_binding(wp):
    """cmd=None → 바인딩 해제(repo areas/local.conf 폴백·현행)."""
    _seed(wp, _lease_tc(wp, slot="work/A_1", repo="A", test_cmd="old"))
    updated = wp.set_test_cmd("work/A_1", None)
    assert updated.test_cmd is None
    assert next(l for l in wp.list_leases() if l.slot == "work/A_1").test_cmd is None


def test_set_test_cmd_idle_slot_updatable(wp):
    """idle 슬롯(미점유 컨테이너)도 test_cmd 갱신 가능(state 무관)."""
    _seed(wp, _lease_tc(wp, slot="work/A_1", repo="A", test_cmd=None,
                        session="", state="idle"))
    updated = wp.set_test_cmd("work/A_1", "make hil")
    assert updated.test_cmd == "make hil"
    assert updated.state == "idle"  # state 는 안 건드림


def test_set_test_cmd_missing_slot_raises_keyerror(wp):
    """장부에 슬롯이 없으면 KeyError(침묵 무력화 금지 — 호출부가 명시 안내)."""
    _seed(wp, _lease_tc(wp, slot="work/A_1", repo="A"))
    with pytest.raises(KeyError):
        wp.set_test_cmd("work/Z_9", "whatever")


def test_set_test_cmd_empty_ledger_raises_keyerror(wp):
    """빈 장부(슬롯 0)에서도 KeyError(슬롯 부재)."""
    with pytest.raises(KeyError):
        wp.set_test_cmd("work/A_1", "cmd")


# ════════════════════════════════════════════════════════════════════════
# bind_slot — 사람 발의 멀티-PM 정체성 직접 바인딩 (T-0074 · lean)
# find-or-create · pool alloc 아님 · reclaim_stale 절대 미호출(R4 근원 제거) ·
# flock(_lease_lock) + atomic write(_write_ledger) · branch 미변경(git=진실).
# ════════════════════════════════════════════════════════════════════════


def test_bind_slot_new_slot_appends_leased(wp):
    """장부에 없는 슬롯 → 새 leased Lease 를 append 한다(직접 바인딩·풀 탐색 없음)."""
    lease = wp.bind_slot("work/A_2", "A", "A_2")
    assert lease.slot == "work/A_2"
    assert lease.repo == "A"
    assert lease.session == "A_2"
    assert lease.state == "leased"
    assert lease.pid == os.getpid()
    # 장부에 정확히 한 엔트리 등록.
    leases = wp.list_leases()
    assert len(leases) == 1
    assert leases[0].slot == "work/A_2"
    assert leases[0].state == "leased"


def test_bind_slot_existing_slot_updates_session_state(wp):
    """기존 슬롯(idle·다른 세션) → session/state/started/pid 갱신(새 엔트리 안 만듦)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"))
    lease = wp.bind_slot("work/A_1", "A", "A_1")
    assert lease.slot == "work/A_1"
    assert lease.session == "A_1"     # 갱신됨
    assert lease.state == "leased"    # idle → leased 전이
    assert lease.pid == os.getpid()
    # 슬롯이 두 개로 늘지 않음(update-in-place).
    leases = wp.list_leases()
    assert len(leases) == 1
    assert leases[0].session == "A_1"
    assert leases[0].state == "leased"


def test_bind_slot_existing_leased_other_session_reclaims_for_human(wp):
    """다른 세션이 leased 중이어도 사람이 그 슬롯을 선언하면 직접 바인딩한다(pid-회수 아님).

    bind 는 풀 골라잡기/회수가 아니라 '내가 이 슬롯'이라는 사람의 선언이라, 기존 점유
    세션을 그대로 자기 세션으로 덮는다(명시 release 만이 반납). 새 엔트리는 안 생긴다.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="old",
                     pid=os.getpid(), state="leased"))
    lease = wp.bind_slot("work/A_1", "A", "A_1")
    assert lease.session == "A_1"
    assert len(wp.list_leases()) == 1


def test_bind_slot_preserves_test_cmd_and_does_not_touch_branch(wp):
    """기존 슬롯 갱신 시 test_cmd 보존·branch 는 git=진실이라 장부에 안 씀(ADR-0013 amend T-0072)."""
    _seed(wp, _lease_tc(wp, slot="work/A_1", repo="A", test_cmd="ctest -R hil1",
                        session="", state="idle"))
    lease = wp.bind_slot("work/A_1", "A", "A_1")
    assert lease.test_cmd == "ctest -R hil1"  # 점유 메타만 갱신·test_cmd 보존
    # 장부에 branch 키를 쓰지 않는다(slot live HEAD 가 권위·current_branch 조회).
    assert "branch" not in lease.to_dict()


def test_bind_slot_persists_atomically_through_ledger(wp):
    """bind 가 장부에 atomic 영속화 — 새 _read_ledger 가 바뀐 session/state 를 본다(flock+atomic)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"))
    wp.bind_slot("work/A_1", "A", "A_1")
    with wp._lease_lock():
        reloaded = wp._read_ledger()
    target = next(l for l in reloaded if l.slot == "work/A_1")
    assert target.session == "A_1"
    assert target.state == "leased"


def test_bind_slot_never_calls_reclaim_stale_spy(wp, monkeypatch):
    """**reclaim_stale 미호출 입증(spy)** — bind 는 pid-회수 경로를 절대 타지 않는다(R4 근원 제거).

    `reclaim_stale` 를 spy 로 감싸 호출 횟수를 세고, bind 후 0 임을 단언한다. alloc 은 진입
    시 reclaim 을 부르지만(풀 가용성 회복) bind 는 직접 바인딩이라 회수가 필요 없다(사람 경로).
    """
    calls: list[bool] = []
    real_reclaim = wp.reclaim_stale

    def spy_reclaim(*args, **kwargs):
        calls.append(True)
        return real_reclaim(*args, **kwargs)

    monkeypatch.setattr(wp, "reclaim_stale", spy_reclaim)
    wp.bind_slot("work/A_1", "A", "A_1")
    assert calls == [], "bind_slot 이 reclaim_stale 을 호출함(사람 경로 pid-회수 금지·R4)"


def test_bind_slot_does_not_reclaim_dead_pid_slot(wp):
    """pid 죽은 leased 슬롯이 장부에 있어도 bind 는 그걸 회수(idle 화)하지 않는다.

    alloc 이라면 진입 reclaim 으로 그 슬롯을 idle 화하지만, bind 는 *다른* 슬롯을 직접
    바인딩하면서 죽은-pid 슬롯을 그대로 둔다(reclaim 미호출의 관측 가능한 결과·R4 근원 제거).
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="dead",
                     pid=999999, state="leased"))  # pid 죽음
    wp.bind_slot("work/A_2", "A", "A_2")  # 다른 슬롯을 바인딩
    by_slot = {l.slot: l for l in wp.list_leases()}
    # 죽은-pid 슬롯은 회수 안 됨 — 여전히 leased·session 유지(reclaim 미호출 입증).
    assert by_slot["work/A_1"].state == "leased"
    assert by_slot["work/A_1"].session == "dead"
    # 사람이 선언한 슬롯만 leased 로 바인딩됨.
    assert by_slot["work/A_2"].session == "A_2"


def test_bind_slot_leaves_other_slots_untouched(wp):
    """타깃 슬롯만 갱신 — 다른 슬롯의 점유 메타는 안 건드린다(read-modify-write 격리)."""
    _seed(
        wp,
        _lease(wp, slot="work/A_1", repo="A", session="other", state="leased"),
        _lease(wp, slot="work/A_2", repo="A", session="", pid=0, state="idle"),
    )
    wp.bind_slot("work/A_2", "A", "A_2")
    by_slot = {l.slot: l for l in wp.list_leases()}
    assert by_slot["work/A_2"].session == "A_2"     # 갱신됨
    assert by_slot["work/A_1"].session == "other"   # 미변경
    assert by_slot["work/A_1"].state == "leased"


# ════════════════════════════════════════════════════════════════════════
# _default_session — board.session_name 과 동형 우선순위 (T-0066 must-fix)
# env > local.conf session= > <host>-<pid>. local.conf 레이어가 빠지면 저장측
# (이 모듈)과 매칭측(board.session_name)이 어긋나 per-slot test_cmd 가 미스된다.
# ════════════════════════════════════════════════════════════════════════

def _write_local_conf(proj, text):
    (proj / ".project_manager" / "local.conf").write_text(text, encoding="utf-8")


def test_default_session_prefers_pm_env(wp, proj, monkeypatch):
    """`$PM_SESSION_NAME` 가 최우선 — alias·local.conf session= 무시 (T-0073)."""
    monkeypatch.setenv("PM_SESSION_NAME", "from-pm-env")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    _write_local_conf(proj, "session=from-conf\n")
    assert wp._default_session() == "from-pm-env"


def test_default_session_claude_env_is_alias(wp, proj, monkeypatch):
    """`$CLAUDE_SESSION_NAME` 단독 → deprecated alias 로 조용히 동작 (T-0073 back-compat).

    `PM_SESSION_NAME` 미설정·구 변수만 설정된 기존 환경(dogfooding·채택자)이 깨지지
    않아야 한다 — alias 우선순위 2번, local.conf 보다 우선.
    """
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    _write_local_conf(proj, "session=from-conf\n")
    assert wp._default_session() == "from-alias"


def test_default_session_pm_wins_over_claude(wp, proj, monkeypatch):
    """둘 다 설정 시 `PM_SESSION_NAME` 승 (T-0073 마이그레이션 중 명시 우선)."""
    monkeypatch.setenv("PM_SESSION_NAME", "new")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "old")
    assert wp._default_session() == "new"


def test_default_session_reads_local_conf_session(wp, proj, monkeypatch):
    """env 없음 → local.conf `session=` (board.session_name 의 3층과 동형).

    이 레이어가 빠지면 일반 운영(board init 이 local.conf session= 기록·env 미설정)에서
    lease.session 이 `<host>-<pid>` 로 저장돼 board 매칭(local.conf session)과 어긋난다.
    """
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _write_local_conf(proj, "session=foo\n")
    assert wp._default_session() == "foo"


def test_default_session_falls_back_to_host_pid(wp, proj, monkeypatch):
    """env(둘 다)·local.conf session= 모두 없음 → `<host>-<pid>` (4층 폴백)."""
    import socket
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # local.conf 없음(또는 session= 없음).
    assert wp._default_session() == f"{socket.gethostname()}-{os.getpid()}"


def test_local_conf_session_ignores_comments_and_blanks(wp, proj):
    """헬퍼가 `#` 주석/빈 줄/무관 키를 무시하고 session= 만 집는다(board.local_config 동형)."""
    _write_local_conf(proj, "# comment\n\nprefix=PAY\nsession=bar\n# trailing\n")
    assert wp._local_conf_session() == "bar"


def test_local_conf_session_absent_returns_none(wp, proj):
    """local.conf 부재 → None (OSError 폴백)."""
    assert wp._local_conf_session() is None


def test_create_slot_default_session_uses_local_conf(wp, proj, monkeypatch):
    """END-TO-END: env 없음·local.conf session=foo → create_slot 이 lease.session=foo 로 저장.

    must-fix 회귀 핀(저장측). session 인자 미지정이면 _default_session() 으로 해소되는데,
    옛 코드(local.conf 미반영)면 `<host>-<pid>` 로 저장돼 board 매칭측(foo)과 어긋난다.
    """
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _write_local_conf(proj, "session=foo\n")
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", git_runner=git, test_cmd="make hil2")  # session 미지정
    assert lease.session == "foo", \
        "create_slot 이 local.conf session= 을 안 읽음(저장측 host-pid·board 매칭 미스)"


# ════════════════════════════════════════════════════════════════════════
# 리스장부 동시쓰기 안전 (자체 파일락) — spawn 워커
# ════════════════════════════════════════════════════════════════════════


def _worker_create(proj_str, idx, ready, go, out_q):
    """각 워커가 고유 repo(R{idx}) 슬롯을 create_slot — 동시 장부 write 안전 검증."""
    proj = Path(proj_str)
    wp = _load_wp_bound(proj)
    _mk_bare_placeholder(wp, f"R{idx}")  # bare 부재 가드 통과(ADR-0011 §31)
    git = FakeGit()
    ready.put(idx)
    go.wait()
    try:
        lease = wp.create_slot(f"R{idx}", session=f"s{idx}", git_runner=git)
        out_q.put(("OK", lease.slot))
    except BaseException as e:  # noqa: BLE001
        import traceback
        out_q.put(("EXC", f"{e!r}\n{traceback.format_exc()}"))


def test_concurrent_create_slot_no_lost_ledger_writes(proj):
    """N 워커가 동시에 create_slot(고유 repo) → 모든 엔트리 보존(lost update 0·자체 락).

    리스장부 read-modify-write 는 자체 _lease_lock(OS 파일락)으로 직렬화된다. 락이 없으면
    동시 write 가 서로의 엔트리를 덮어써 일부 슬롯이 유실된다 → 자체 락으로 전 엔트리 보존.
    """
    n = 4
    ctx = mp.get_context("spawn")  # 부모 monkeypatch 비상속 — 자식이 명시 재배선
    ready = ctx.Queue()
    go = ctx.Event()
    out_q = ctx.Queue()
    procs = [ctx.Process(target=_worker_create, args=(str(proj), i, ready, go, out_q))
             for i in range(n)]
    for p in procs:
        p.start()
    for _ in range(n):
        ready.get(timeout=SYNC_TIMEOUT)
    go.set()
    results = []
    try:
        for _ in range(n):
            results.append(out_q.get(timeout=SYNC_TIMEOUT))
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=SYNC_TIMEOUT)

    excs = [d for tag, d in results if tag == "EXC"]
    assert not excs, "create_slot 워커 예외:\n" + "\n".join(excs)

    # 모든 워커의 슬롯이 장부에 보존됐는지(lost update 0).
    wp = _load_wp_bound(proj)
    slots = {l.slot for l in wp.list_leases()}
    expected = {f"work/R{i}_1" for i in range(n)}
    assert slots == expected, f"lost ledger writes: {expected - slots}"


# ════════════════════════════════════════════════════════════════════════
# 실 git 통합 (hermetic·임시 git repo) — DI seam 미주입·실 git 경로
# ════════════════════════════════════════════════════════════════════════

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")


def _git(cwd, *argv, env=None):
    """테스트용 실 git 헬퍼 — check=True·UTF-8 캡처."""
    e = dict(os.environ)
    e.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    if env:
        e.update(env)
    return subprocess.run([_GIT, "-C", str(cwd), *argv], check=True,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e)


def _init_repo(path):
    """초기 커밋 있는 git repo 를 만든다(worktree add 가 가능하도록)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _mk_real_bare(wp, repo: str, tmp_path: Path, *, marker: str = "FAMILY") -> Path:
    """실 bare repo `.repos/<repo>.git` 를 만든다 — `pm-config repo add` 가 만든 것과 동형.

    family repo origin(고유 marker 파일 커밋) → `git clone --bare` 로 `.repos/<repo>.git`
    (ADR-0011 §31·T-0061 규약). 슬롯이 *family repo 내용*(multi-PM이 아닌)을 체크아웃하는지
    검증할 수 있게 multi-PM README 와 구별되는 marker 파일을 둔다.
    """
    origin = _init_repo(tmp_path / f"{repo}-origin")
    (origin / "FAMILY_MARKER.txt").write_text(f"{marker}:{repo}\n", encoding="utf-8")
    _git(origin, "add", "FAMILY_MARKER.txt")
    _git(origin, "commit", "-q", "-m", f"family {repo}")
    bare = wp.bare_repo_path(repo)
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))
    return bare


@_git_required
def test_real_git_create_slot_branch_checkout_and_release(proj, tmp_path):
    """실 git — create_slot 이 `.repos/<repo>.git` bare 컨텍스트로 슬롯 생성·branch checkout·반납.

    family bare(`.repos/A.git`)를 실제로 만들고(ADR-0011 §31), create_slot 이 그 bare 의
    worktree 로 work/A_1 을 실제 만든다 — 폴더 존재·HEAD 가 요청 branch·**family 내용(multi-PM
    아님)** 체크아웃·반납 후 idle 을 검증.
    """
    _init_repo(proj)  # proj = REPO(multi-PM) — bare 가 따로라 multi-PM은 worktree base 가 아님
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)  # .repos/A.git bare = worktree base

    lease = wp.create_slot("A", branch="a1-feature", session="me", init_submodules=False)
    assert lease.slot == "work/A_1"
    slot_dir = wp.slot_path("work/A_1")
    assert slot_dir.is_dir(), "worktree 폴더가 실제로 안 생김"

    # 그 worktree 의 현재 브랜치가 요청한 branch.
    head = _git(slot_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "a1-feature", f"worktree HEAD branch={head!r}"

    # 슬롯이 *family repo*(bare) 내용을 체크아웃했는지 — multi-PM이 아닌 family marker 가 보여야 한다.
    marker = slot_dir / "FAMILY_MARKER.txt"
    assert marker.exists(), "슬롯이 family bare 의 worktree 가 아님(multi-PM 폴백·ADR-0011 §31 위반)"
    assert marker.read_text(encoding="utf-8") == "FAMILY:A\n"

    # 슬롯 = work/A_1 (브랜치 폴더명에 안 박힘·ADR-0013).
    assert slot_dir.name == "A_1"

    # 그 worktree 의 .git 원이 family bare 인지(공유 .git 원 = .repos/A.git·ADR-0011 §31).
    common = _git(slot_dir, "rev-parse", "--git-common-dir").stdout.strip()
    assert Path(common).resolve() == wp.bare_repo_path("A").resolve(), \
        f"worktree 공유 .git 원이 family bare 가 아님: {common!r}"

    # clean 반납 → idle.
    released = wp.release("work/A_1")
    assert released.state == "idle"


@_git_required
def test_real_git_create_slot_base_derives_slot_branch_from_base(proj, tmp_path):
    """실 git — create_slot(base=develop) 이 슬롯 브랜치 `A_1` 를 *develop tip 에서* 판다 (T-0075).

    family origin 에 `develop` 브랜치(main 보다 앞선 고유 커밋·DEV_ONLY 파일)를 만들고 bare
    로 clone 한다. create_slot(base="develop") 후:
      - 슬롯 브랜치는 `A_1`(슬롯 식별자) 이고
      - 그 브랜치가 develop tip 에서 갈렸다 — `merge-base A_1 develop == develop tip`(develop 의
        조상이 곧 A_1·즉 A_1 가 develop 에서 파생) 이고 main 의 develop-only 커밋이 슬롯에 보인다.
    base 가 무시되면(bare HEAD=main) DEV_ONLY 가 안 보이고 merge-base 가 main 일 것 → 회귀 포착.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)

    # family origin: main + develop(앞선 고유 커밋).
    origin = _init_repo(tmp_path / "A-origin")
    (origin / "FAMILY_MARKER.txt").write_text("FAMILY:A\n", encoding="utf-8")
    _git(origin, "add", "FAMILY_MARKER.txt")
    _git(origin, "commit", "-q", "-m", "family A main")
    _git(origin, "checkout", "-q", "-b", "develop")
    (origin / "DEV_ONLY.txt").write_text("on develop\n", encoding="utf-8")
    _git(origin, "add", "DEV_ONLY.txt")
    _git(origin, "commit", "-q", "-m", "develop-only commit")
    develop_tip = _git(origin, "rev-parse", "develop").stdout.strip()
    _git(origin, "checkout", "-q", "main")   # origin HEAD = main(develop 아님)
    bare = wp.bare_repo_path("A")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))

    lease = wp.create_slot("A", base="develop", session="me", init_submodules=False)
    assert lease.slot == "work/A_1"
    slot_dir = wp.slot_path("work/A_1")

    # 슬롯 브랜치 = A_1(슬롯 식별자·base 는 develop 만 바뀜).
    head = _git(slot_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "A_1", f"슬롯 브랜치명={head!r}(슬롯 식별자 A_1 여야)"

    # develop 에서 파생 — A_1 의 시작점이 develop tip(merge-base == develop tip).
    mb = _git(slot_dir, "merge-base", "HEAD", "develop").stdout.strip()
    assert mb == develop_tip, f"A_1 가 develop tip 에서 안 갈림: merge-base={mb!r} develop={develop_tip!r}"
    # develop-only 파일이 슬롯에 보인다(bare HEAD=main 이면 안 보임 → base 무시 회귀 포착).
    assert (slot_dir / "DEV_ONLY.txt").exists(), \
        "develop-only 파일이 슬롯에 없음 — base=develop 이 무시되고 main(bare HEAD)에서 땄다"


@_git_required
def test_real_git_create_slot_missing_bare_raises(proj, tmp_path):
    """실 git — `.repos/<repo>.git` bare 가 없으면 BareRepoMissing(multi-PM 루트 worktree 침묵 폴백 금지).

    bare 부재 가드(ADR-0011 §31·ADR-0013 fail-soft): bare 없이 create_slot 하면 명시 에러로
    `pm-config repo add` 선행을 안내해야 한다 — multi-PM 루트 worktree 를 조용히 만들면 안 된다.
    """
    _init_repo(proj)  # multi-PM은 git repo 지만 .repos/A.git 은 없음
    wp = _load_wp_bound(proj)
    with pytest.raises(wp.BareRepoMissing):
        wp.create_slot("A", branch="a1", session="me", init_submodules=False)
    # 가드가 worktree add 전에 막아 슬롯 폴더도·장부도 안 생김(침묵 폴백 0).
    assert not wp.slot_path("work/A_1").exists()
    assert wp.list_leases() == []


@_git_required
def test_real_git_reattach_resume_preserves_dirty(proj, tmp_path):
    """실 git — resume re-alloc 이 같은 슬롯에 재부착하고 dirty 작업이 보존된다(회전 연속성).

    create_slot 후 dirty 파일을 남기고 stale 회수 없이 resume 으로 재부착하면 같은 슬롯이고
    dirty 파일이 그대로 있어야 한다(file-handoff 보다 강한 연속성·ADR-0013 §6-6).
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)

    lease = wp.create_slot("A", branch="a1", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)
    # dirty 작업 — 미커밋 파일.
    (slot_dir / "wip.txt").write_text("work in progress\n", encoding="utf-8")

    # 같은 세션 resume(branch a1) → 같은 슬롯 재부착(dirty 그대로·checkout 은 같은 브랜치라 무해).
    reattached = wp.alloc("A", resume="a1", session="me")
    assert reattached.slot == lease.slot
    assert (slot_dir / "wip.txt").exists(), "dirty 작업이 재부착 후 유실됨"
    assert (slot_dir / "wip.txt").read_text(encoding="utf-8") == "work in progress\n"


@_git_required
def test_real_git_submodule_init_in_new_slot(proj, tmp_path, monkeypatch):
    """실 git — create_slot 이 worktree add 후 submodule 을 init 한다(ADR-0013 §8-4(d)).

    임시 superproject(submodule 포함)를 만들고 worktree 슬롯을 생성하면, `git worktree add`
    는 submodule 을 자동 init 안 하므로 create_slot 이 `submodule update --init --recursive`
    로 채운다 — 슬롯 worktree 의 submodule 작업트리에 파일이 실제로 채워졌는지 검증.

    테스트 픽스처는 *로컬 file:// 경로* submodule 을 쓴다 — git 은 보안상(CVE-2022-39253)
    file 프로토콜 submodule clone 을 기본 차단하므로, GIT_CONFIG_* 환경으로 모든 git 호출에
    `protocol.file.allow=always` 를 주입해 그 차단을 푼다(실 ssh/https submodule 엔 무관한
    테스트-전용 우회 — 엔진 코드는 `-c` 를 안 박아 실전 동작에 영향 0).
    """
    # 모든 git 호출(엔진의 un-injected 실 runner 포함)에 file 프로토콜 허용 주입.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

    # 1) submodule 이 될 별도 repo (한 파일 커밋).
    sub_origin = _init_repo(tmp_path / "sub-origin")
    (sub_origin / "lib.txt").write_text("shared lib\n", encoding="utf-8")
    _git(sub_origin, "add", "lib.txt")
    _git(sub_origin, "commit", "-q", "-m", "lib")

    # 2) family repo origin 에 submodule 추가 + 커밋 → bare clone(.repos/A.git) = worktree base.
    #    (multi-PM이 아니라 family repo 가 submodule 을 갖는다 — ADR-0011 §31·spike §8-4(d) 패밀리
    #    repo *내부* 컴포넌트용 submodule.)
    family_origin = _init_repo(tmp_path / "A-origin")
    _git(family_origin, "submodule", "add", str(sub_origin), "vendor/sub")
    _git(family_origin, "commit", "-q", "-m", "add submodule")
    _init_repo(proj)  # multi-PM도 git repo(전역 일관)지만 worktree base 는 family bare
    wp = _load_wp_bound(proj)
    bare = wp.bare_repo_path("A")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(family_origin), str(bare))

    lease = wp.create_slot("A", session="me", init_submodules=True)
    slot_dir = wp.slot_path(lease.slot)

    # worktree add 만 했다면 vendor/sub 가 비어있다 — create_slot 의 submodule init(슬롯 cwd)
    # 후엔 그 안에 submodule 파일(lib.txt)이 채워져 있어야 한다(family bare 의 worktree 내부).
    sub_file = slot_dir / "vendor" / "sub" / "lib.txt"
    assert sub_file.exists(), "submodule 이 슬롯 worktree 에 init 안 됨(ADR-0013 §8-4(d) 위반)"
    assert sub_file.read_text(encoding="utf-8") == "shared lib\n"


# ════════════════════════════════════════════════════════════════════════
# T-0070 — submodule 인터랙티브 러너 + 원자적 롤백 + 런너 stderr surface
# 실 Windows multi-PM 파일럿 블로커 3종(submodule clone 600s 타임아웃·댕글링 worktree·
# stderr 유실로 빈 에러). 단위테스트는 git_runner 주입(mock)이라 실 인터랙티브 안 탐.
# ════════════════════════════════════════════════════════════════════════


# ── (1) submodule init = 인터랙티브 러너 (seam·실행 없이) ─────────────────────


def test_create_slot_submodule_uses_interactive_runner_on_real_path(wp, monkeypatch):
    """git_runner 미주입 실경로면 submodule 단계가 `_real_git_runner_interactive` 를 탄다(T-0070).

    seam 으로 검증 — `_real_git_runner_interactive`/`_real_git_runner` 를 cwd 캡처 spy 로
    교체해 *어느 러너 팩토리가 submodule cwd(슬롯 경로)에 대해 불렸는지* 본다. 실 인터랙티브
    subprocess(stdin 블록)는 절대 실행하지 않는다 — spy 가 FakeGit 을 돌려줘 rc0 으로 흐른다.
    """
    _mk_bare_placeholder(wp, "A")
    interactive_cwds: list = []
    capture_cwds: list = []

    def spy_interactive(cwd):
        interactive_cwds.append(cwd)
        return FakeGit()  # 모든 git 호출 성공 stub(실 인터랙티브 subprocess 안 탐)

    def spy_capture(cwd):
        capture_cwds.append(cwd)
        return FakeGit()

    monkeypatch.setattr(wp, "_real_git_runner_interactive", spy_interactive)
    monkeypatch.setattr(wp, "_real_git_runner", spy_capture)

    wp.create_slot("A", branch="a1", session="me")  # git_runner 미주입 = 실경로

    # submodule cwd = 슬롯 경로. 인터랙티브 러너가 그 슬롯 경로로 만들어졌어야 한다.
    slot_p = wp.slot_path("work/A_1")
    assert slot_p in interactive_cwds, \
        "submodule 단계가 인터랙티브 러너를 안 탐(_real_git_runner_interactive 미사용)"
    # worktree add(짧은 git)는 capture 러너(bare 컨텍스트) — 인터랙티브로 가면 안 됨.
    assert wp.bare_repo_path("A") in capture_cwds, "worktree add 가 capture 러너를 안 탐"
    assert wp.bare_repo_path("A") not in interactive_cwds, \
        "worktree add 가 인터랙티브 러너로 감(submodule 만 인터랙티브여야)"


def test_create_slot_submodule_injected_runner_preserves_di_seam(wp, monkeypatch):
    """git_runner 주입 시 submodule 도 그 주입 runner — 인터랙티브 러너 안 탐(DI seam 보존·현행).

    인터랙티브 팩토리를 호출하면 즉시 실패하는 trap 으로 바꿔 — 주입 모드에서 그게 안
    불리는지(현행 테스트 무영향) 결정적으로 입증한다.
    """
    _mk_bare_placeholder(wp, "A")

    def trap(cwd):  # 주입 모드에서 인터랙티브가 불리면 안 됨.
        raise AssertionError("git_runner 주입인데 인터랙티브 러너가 불림(DI seam 깨짐)")

    monkeypatch.setattr(wp, "_real_git_runner_interactive", trap)
    git = FakeGit()
    lease = wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert lease.state == "leased"
    # 주입 runner 가 submodule 도 처리(인터랙티브 우회).
    assert git.did("submodule", "update", "--init", "--recursive", "--force")


# ── (2) create_slot 원자적 롤백 ─────────────────────────────────────────────


class _SubmoduleFailRollbackGit:
    """worktree add 성공·submodule 실패(rc≠0)·worktree remove 기록 — 롤백 호출 검증용.

    submodule 만 실패시켜 worktree add *성공 후* 롤백 경로를 유도한다. `worktree remove
    ... --force` 가 불렸는지 호출 기록으로 확인한다(같은 주입 runner 가 add·submodule·
    remove 전부 처리).
    """

    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[:1] == ["submodule"]:
            return (1, "fatal: submodule clone failed")
        return (0, "")  # worktree add·remove 등은 성공

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


def test_create_slot_submodule_failure_rolls_back_worktree(wp):
    """worktree add 성공 + submodule 실패 → `worktree remove --force` 롤백·리스 0·raise(T-0070).

    add 성공 후 단계 실패는 *부분 슬롯* — 롤백 안 하면 댕글링 worktree("슬롯 없음"+재시도
    "이미 존재")가 남는다(ADR-0013 "불완전 슬롯 차단"의 fs 완성). 주입 runner 의 remove
    호출 기록으로 롤백을, list_leases==[] 로 등록 0 을 검증한다.
    """
    _mk_bare_placeholder(wp, "A")
    git = _SubmoduleFailRollbackGit()
    with pytest.raises(RuntimeError):
        wp.create_slot("A", branch="a1", session="me", git_runner=git)
    # 롤백: `git worktree remove <slot> --force` 가 불렸다(주입 runner 경로).
    assert git.did("worktree", "remove"), "submodule 실패 후 worktree 롤백(remove)이 안 불림(댕글링)"
    remove_calls = [c for c in git.calls if c[:2] == ["worktree", "remove"]]
    assert any("--force" in c for c in remove_calls), "롤백 remove 가 --force 없이 불림"
    # 등록 0 — 불완전 슬롯 미등록(기존 계약 유지).
    assert wp.list_leases() == []


def test_create_slot_rollback_failure_still_raises_original(wp):
    """롤백 자체가 실패(remove rc≠0/예외)해도 원래 submodule 에러로 raise(2차 예외 삼킴 금지·T-0070).

    `_rollback_worktree` 는 best-effort — remove 가 실패해도 raise 하지 않아 원래
    RuntimeError(submodule init failed)가 호출부로 전파된다. remove 가 rc≠0 또는 예외를
    던지는 두 케이스 모두 원래 에러가 살아남는지 본다.
    """
    _mk_bare_placeholder(wp, "A")

    def remove_rc_fail(argv):
        if argv[:1] == ["submodule"]:
            return (1, "submodule failed")
        if argv[:2] == ["worktree", "remove"]:
            return (1, "remove failed")  # 롤백 실패(rc≠0)
        return (0, "")
    with pytest.raises(RuntimeError, match="submodule init failed"):
        wp.create_slot("A", branch="a1", session="me", git_runner=remove_rc_fail)
    assert wp.list_leases() == []

    def remove_raises(argv):
        if argv[:1] == ["submodule"]:
            return (1, "submodule failed")
        if argv[:2] == ["worktree", "remove"]:
            raise OSError("remove blew up")  # 롤백이 예외
        return (0, "")
    with pytest.raises(RuntimeError, match="submodule init failed"):
        wp.create_slot("A", branch="a1", session="me", git_runner=remove_raises)
    assert wp.list_leases() == []


def test_create_slot_worktree_add_failure_does_not_rollback(wp):
    """worktree add *실패* 면 만들어진 worktree 가 없으니 롤백(remove) 안 함(T-0070).

    롤백은 worktree add *성공 후* 단계 실패에만 — add 자체가 실패하면 지울 게 없다.
    remove 가 안 불리는지로 롤백 범위(add 성공 후만)를 박제한다.
    """
    _mk_bare_placeholder(wp, "A")
    calls: list = []

    def add_fail(argv):
        calls.append(list(argv))
        if argv[:2] == ["worktree", "add"]:
            return (1, "fatal: add failed")
        return (0, "")
    with pytest.raises(RuntimeError):
        wp.create_slot("A", branch="a1", session="me", git_runner=add_fail)
    assert not any(c[:2] == ["worktree", "remove"] for c in calls), \
        "worktree add 실패인데 롤백 remove 가 불림(지울 worktree 없음)"
    assert wp.list_leases() == []


def test_rollback_worktree_uses_bare_context(wp, monkeypatch):
    """`_rollback_worktree` 의 remove 컨텍스트가 `.repos/<repo>.git` bare 다(add 와 동일·ADR-0011 §31).

    add 가 bare 컨텍스트에서 일어나므로 remove 도 같은 컨텍스트라야 한다 —
    `_real_git_runner(cwd)` 가 어떤 cwd 로 만들어지는지 캡처해 family bare 인지 본다.
    """
    _mk_bare_placeholder(wp, "A")
    captured = []

    def spy(cwd):
        captured.append(cwd)
        return FakeGit()

    monkeypatch.setattr(wp, "_real_git_runner", spy)
    wp._rollback_worktree("A", wp.slot_path("work/A_1"))
    assert captured and captured[0] == wp.bare_repo_path("A"), \
        f"롤백 remove 컨텍스트가 family bare 가 아님: {captured!r}"


# ── (3) _real_git_runner stdout+stderr surface + except → (1, str(exc)) ──────


def test_real_git_runner_combines_stdout_and_stderr(wp, monkeypatch):
    """`_real_git_runner` 가 stdout + stderr 를 합쳐 반환한다(T-0070·pm_config 정합).

    옛 코드는 stdout 만 돌려 stderr(에러 본문)를 버렸다 — 빈 에러로 진단 불가. mock
    subprocess.run 으로 stdout/stderr 를 갖춘 결과를 주고 합쳐지는지 본다.
    """
    class _Result:
        returncode = 0
        stdout = "out-line\n"
        stderr = "warning: detached HEAD\n"

    monkeypatch.setattr(wp.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wp.subprocess, "run", lambda *a, **k: _Result())
    runner = wp._real_git_runner(wp.REPO)
    rc, out = runner(["status"])
    assert rc == 0
    assert "out-line" in out and "warning: detached HEAD" in out, \
        f"stdout+stderr 결합 안 됨: {out!r}"


def test_real_git_runner_exception_surfaces_message(wp, monkeypatch):
    """`_real_git_runner` 의 except 가 (1, str(exc)) — 타임아웃/예외 메시지 surface(T-0070).

    옛 코드는 (1, "")로 삼켰다(침묵 무력화). TimeoutExpired 를 시뮬해 메시지가 out 에
    실리는지 본다 — 그래야 다음 사람이 "왜 죽었는지" 안다.
    """
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git submodule", timeout=600)

    monkeypatch.setattr(wp.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wp.subprocess, "run", boom)
    runner = wp._real_git_runner(wp.REPO)
    rc, out = runner(["submodule", "update"])
    assert rc == 1
    assert out != "", "예외 메시지가 빈 문자열로 삼켜짐(침묵 무력화 회귀)"
    assert "TimeoutExpired" in out or "600" in out, f"타임아웃 메시지가 surface 안 됨: {out!r}"


def test_real_git_runner_missing_git_returns_message(wp, monkeypatch):
    """git 바이너리 부재 → (1, 안내 메시지)(빈 문자열 아님·진단 가능·T-0070)."""
    monkeypatch.setattr(wp.shutil, "which", lambda _name: None)
    runner = wp._real_git_runner(wp.REPO)
    rc, out = runner(["status"])
    assert rc == 1
    assert out.strip() != "", "git 부재가 빈 out 으로 삼켜짐"


# ── (1) _real_git_runner_interactive 자체 단위테스트 (stdin 블록 없음) ─────────


@_git_required
def test_real_git_runner_interactive_runs_short_git(wp, proj):
    """`_real_git_runner_interactive` 가 짧은 비-네트워크 git 을 실행·rc 반환(stdin 블록 없음·T-0070).

    submodule clone(stdin 블록·네트워크)은 절대 안 돌리고, `git rev-parse --git-dir`
    같은 즉시 끝나는 명령으로 인터랙티브 러너 자체가 동작하고 (rc, "")를 돌려주는지 본다.
    stdio 콘솔 상속이라 캡처 문자열은 빈 문자열이다.
    """
    _init_repo(proj)
    runner = wp._real_git_runner_interactive(proj)
    rc, out = runner(["rev-parse", "--git-dir"])
    assert rc == 0
    assert out == "", "인터랙티브 러너는 출력을 콘솔로 보내 캡처 문자열이 빈 문자열이어야 함"
    # 실패 경로도 rc 로(존재하지 않는 ref).
    rc2, _ = runner(["rev-parse", "--verify", "no-such-branch-xyz"])
    assert rc2 != 0


def test_real_git_runner_interactive_missing_git_failsoft(wp, monkeypatch):
    """git 부재 → (1, 메시지)(fail-soft·실 subprocess 안 탐·T-0070)."""
    monkeypatch.setattr(wp.shutil, "which", lambda _name: None)
    runner = wp._real_git_runner_interactive(wp.REPO)
    rc, out = runner(["submodule", "update"])
    assert rc == 1
    assert out.strip() != ""


def test_real_git_runner_interactive_uses_submodule_timeout(wp, monkeypatch):
    """인터랙티브 러너가 SUBMODULE_TIMEOUT 을 subprocess timeout 으로 쓴다(capture 안 함·T-0070).

    mock subprocess.run 으로 호출 kwargs 를 캡처 — capture_output 을 주지 않고(stdio 상속)
    timeout=SUBMODULE_TIMEOUT 를 넘기는지 본다(짧은 GIT_TIMEOUT_SECONDS 가 아니라).
    """
    captured = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(wp.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wp.subprocess, "run", fake_run)
    runner = wp._real_git_runner_interactive(wp.REPO)
    rc, out = runner(["submodule", "update", "--init"])
    assert rc == 0
    assert out == ""
    assert captured["kwargs"].get("timeout") == wp.SUBMODULE_TIMEOUT
    # stdio 상속 = capture_output 미지정(콘솔로 직접).
    assert "capture_output" not in captured["kwargs"], "인터랙티브가 capture 함(stdio 상속 깨짐)"


# ── (3-가드) _is_dirty stderr 오탐 회귀 0 ────────────────────────────────────


def test_is_dirty_ignores_stderr_warning_lines(wp):
    """status 출력에 stderr 경고가 섞여도 dirty 오탐 0(T-0070·_real_git_runner stderr surface 회귀).

    `_real_git_runner` 가 stdout+stderr 를 합치게 바뀌어, clean worktree 인데 git 경고
    (`warning: ...`)가 status 출력에 섞이면 옛 `out.strip()!=""` 판정이 dirty 오탐을 냈다.
    porcelain 엔트리 형식 라인만 보는 가드로 경고에 안 흔들리는지 본다.
    """
    class _WarnGit:
        def __call__(self, argv):
            if argv[:2] == ["status", "--porcelain"]:
                # clean(엔트리 0) + stderr 경고가 섞인 출력.
                return (0, "warning: CRLF will be replaced by LF in file.txt\n"
                           "warning: in the working copy of 'x'\n")
            return (0, "")
    git = _WarnGit()
    assert wp._is_dirty(wp.slot_path("work/A_1"), git_runner=git) is False, \
        "stderr 경고만 있는데 dirty 오탐(porcelain 라인 필터 안 됨)"


def test_is_dirty_detects_real_change_amid_stderr_warning(wp):
    """sensitivity 대조 — 진짜 porcelain 엔트리가 있으면(경고 섞여도) dirty 로 본다(T-0070).

    위 오탐 가드와 대조: 경고를 거르되 *실제 변경 라인*(` M file`)은 dirty 로 잡아야 한다 —
    경고 필터가 진짜 dirty 까지 삼키면(false clean) stash 없이 작업이 날아간다.
    """
    class _DirtyWithWarnGit:
        def __call__(self, argv):
            if argv[:2] == ["status", "--porcelain"]:
                return (0, "warning: CRLF will be replaced by LF\n M file.py\n")
            return (0, "")
    git = _DirtyWithWarnGit()
    assert wp._is_dirty(wp.slot_path("work/A_1"), git_runner=git) is True, \
        "경고 필터가 진짜 변경 라인까지 삼킴(false clean·작업 유실 위험)"


def test_is_dirty_rc_nonzero_is_conservatively_dirty(wp):
    """status rc≠0 → 보수적으로 dirty(상태 불명·기존 계약 불변·T-0070 회귀 0)."""
    class _StatusFailGit:
        def __call__(self, argv):
            if argv[:2] == ["status", "--porcelain"]:
                return (1, "fatal: not a git repository")
            return (0, "")
    assert wp._is_dirty(wp.slot_path("work/A_1"), git_runner=_StatusFailGit()) is True


def test_porcelain_status_lines_filters_warnings(wp):
    """`_porcelain_status_lines` 가 porcelain 엔트리만 추리고 경고/빈 줄을 거른다(T-0070)."""
    out = (
        "warning: CRLF will be replaced by LF\n"
        " M modified.py\n"
        "\n"
        "?? untracked.txt\n"
        "warning: trailing\n"
    )
    lines = wp._porcelain_status_lines(out)
    assert lines == [" M modified.py", "?? untracked.txt"], \
        f"porcelain 필터가 경고를 안 거름/엔트리를 빠뜨림: {lines!r}"


# ── hermetic 입증 ────────────────────────────────────────────────────────


def test_real_root_local_untouched_by_tmp(wp):
    """tmp-바인딩 wp 가 실 루트 .local 을 안 건드리는지 가드(경로 재배선 확인)."""
    real_leases = REPO / ".project_manager" / ".local" / "worktree-leases.json"
    assert wp.LEASES_FILE != real_leases, "LEASES_FILE 가 tmp 로 재배선 안 됨"
    assert wp.REPO != REPO, "REPO 가 tmp 로 재배선 안 됨"


def test_does_not_import_board(wp):
    """worktree_pool 은 board.py 를 import 하지 않는다(touches 격리·자체 파일락·병렬충돌 회피)."""
    import sys
    # 모듈 로드 후에도 board 가 sys.modules 에 없거나, 적어도 wp 가 board 심볼에 의존하지 않음.
    assert not hasattr(wp, "board_lock"), "board.board_lock 을 들고 있으면 안 됨(import 금지)"
    assert not hasattr(wp, "board"), "board 모듈을 참조하면 안 됨"
    # 소스에 board import 가 없음을 직접 확인.
    src = (TOOLS / "worktree_pool.py").read_text(encoding="utf-8")
    assert "import board" not in src and "from board" not in src, "board.py import 금지 위반"


# ════════════════════════════════════════════════════════════════════════
# current_branch — 슬롯 worktree 의 git HEAD live 조회 (T-0072 · ADR-0013 amend)
# 브랜치는 git 단일 진실 — 장부 저장 폐지. DI seam(git_runner) 으로 hermetic·실경로는
# slot_path 부재 가드 + _real_git_runner. detached/rc≠0/경로부재 → None(fail-soft).
# ════════════════════════════════════════════════════════════════════════


class _SymbolicRefGit:
    """`symbolic-ref --short HEAD` 를 (rc, out) 으로 모델링하는 주입 runner (T-0072·codex 게이트).

    detached(rc≠0)/조회불가/정상·unborn 브랜치(rc0+이름)를 결정적으로 친다 — current_branch
    의 분기를 hermetic 하게 검증한다(실 git 없이·DI seam). 그 외 git 호출은 (0, "").
    """

    def __init__(self, *, rc: int = 0, out: str = "main\n"):
        self.rc = rc
        self.out = out
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv == ["symbolic-ref", "--short", "HEAD"]:
            return (self.rc, self.out)
        return (0, "")


def test_current_branch_returns_live_head(wp):
    """정상 — symbolic-ref 가 브랜치명을 돌려주면 그 브랜치(strip)를 반환한다(live 조회)."""
    git = _SymbolicRefGit(rc=0, out="a5-pay\n")
    assert wp.current_branch("work/A_1", git_runner=git) == "a5-pay"
    # symbolic-ref --short HEAD 를 실제로 호출했다(live·저장 복사본 아님).
    assert ["symbolic-ref", "--short", "HEAD"] in git.calls


def test_current_branch_detached_head_returns_none(wp):
    """detached HEAD — symbolic-ref 가 rc≠0(symbolic ref 아님)이면 None(브랜치 아님)."""
    git = _SymbolicRefGit(rc=1, out="fatal: ref HEAD is not a symbolic ref\n")
    assert wp.current_branch("work/A_1", git_runner=git) is None


def test_current_branch_unborn_branch_returns_name(wp):
    """unborn 브랜치(아직 커밋 0) — symbolic-ref 가 이름을 rc0 으로 준다 → 그 이름 반환.

    codex T-0072 게이트의 must-fix 회귀: rev-parse --abbrev-ref 는 unborn 을 rc≠0 으로 줘
    detached 로 *오판*(→ None="미지정")했으나, symbolic-ref 는 unborn 브랜치명을 그대로 준다
    (git=진실·ADR-0013 amend — 이름이 있으면 보여야 한다).
    """
    git = _SymbolicRefGit(rc=0, out="main\n")
    assert wp.current_branch("work/A_1", git_runner=git) == "main"


def test_current_branch_rc_nonzero_returns_none(wp):
    """git 호출 실패(rc≠0) → None(fail-soft·예외 raise 금지·손상/락/git부재 흡수)."""
    git = _SymbolicRefGit(rc=128, out="fatal: not a git repository\n")
    assert wp.current_branch("work/A_1", git_runner=git) is None


def test_current_branch_empty_output_returns_none(wp):
    """빈 출력(rc0 이지만 브랜치명 없음) → None(보수적·이상 출력 흡수)."""
    git = _SymbolicRefGit(rc=0, out="\n")
    assert wp.current_branch("work/A_1", git_runner=git) is None


def test_current_branch_missing_slot_path_returns_none_real_path(wp):
    """실경로(git_runner 미주입) + 슬롯 폴더 부재 → None (slot_path 부재 가드·fail-soft).

    git_runner 미주입이라 실경로 가드를 탄다 — tmp proj 에 work/A_9 폴더가 없으므로 git
    호출 전에 None(실 git 미접촉). hermetic — 실 git 안 부른다(폴더 부재로 단락).
    """
    assert not wp.slot_path("work/A_9").exists()
    assert wp.current_branch("work/A_9") is None


def test_current_branch_never_raises_failsoft(wp):
    """fail-soft 계약 — git_runner 가 (1,...) 을 돌려주거나 *예외를 던져도* current_branch 는
    raise 하지 않는다(둘 다 None).

    실 `_real_git_runner` 는 예외를 (1, str(exc)) 로 감싸 rc≠0 으로 흡수하지만, 주입 runner
    가 직접 raise 하는 경우(codex T-0072 suggestion)까지 current_branch 가 try/except 로
    흡수함을 박제한다 — DI seam 까지 "raise 금지" 계약 보장.
    """
    def rc_fail(argv):
        return (1, "boom: timeout")
    assert wp.current_branch("work/A_1", git_runner=rc_fail) is None

    def raiser(argv):
        raise RuntimeError("git exploded")
    # 주입 runner 가 raise 해도 current_branch 는 raise 하지 않고 None.
    assert wp.current_branch("work/A_1", git_runner=raiser) is None


@_git_required
def test_current_branch_real_git_reads_checked_out_branch(proj, tmp_path):
    """실 git — 슬롯 worktree 의 실제 체크아웃 브랜치를 live 로 읽는다(미주입 실경로·통합).

    family bare 의 worktree 슬롯을 실제로 만들고(branch=a1-feature) current_branch 가 실
    git symbolic-ref 로 그 브랜치를 돌려주는지 검증. 그 뒤 슬롯서 직접 `git checkout` 으로
    브랜치를 바꾸면 current_branch 가 *즉시* 새 브랜치를 반영한다(드리프트 0·git=진실).
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)
    lease = wp.create_slot("A", branch="a1-feature", session="me", init_submodules=False)

    # 미주입(실경로) current_branch 가 실 worktree HEAD 를 읽는다.
    assert wp.current_branch(lease.slot) == "a1-feature"

    # 사용자가 슬롯서 직접 git checkout — current_branch 가 즉시 반영(장부 갱신 없이·드리프트 0).
    slot_dir = wp.slot_path(lease.slot)
    _git(slot_dir, "checkout", "-q", "-b", "a2-hotfix")
    assert wp.current_branch(lease.slot) == "a2-hotfix"


# ════════════════════════════════════════════════════════════════════════
# alloc live 매칭 — 저장 필드 없이 live HEAD 로 resume 재부착 (T-0072 · ADR-0013 amend)
# ════════════════════════════════════════════════════════════════════════


def test_alloc_resume_matches_on_live_head_not_stored_field(wp):
    """resume re-alloc 매칭이 *저장 필드*가 아니라 슬롯 live HEAD 로 일어난다(드리프트 불가능).

    장부엔 branch 가 없다(권위 제거·ADR-0013 amend T-0072). 같은 슬롯이 a5-pay 를 체크아웃
    중(FakeGit head)이면 resume="a5-pay" 가 그 슬롯을 live HEAD 매칭으로 재부착한다.
    """
    _seed(wp, _lease(wp, slot="work/A_2", repo="A",
                     session="", pid=0, state="idle"))
    # 장부엔 branch 필드가 없다(직렬화 폐지) — 매칭은 오직 live HEAD.
    assert "branch" not in wp.list_leases()[0].to_dict()
    git = FakeGit(head="a5-pay")  # 슬롯 worktree 가 a5-pay 를 체크아웃 중(live)
    lease = wp.alloc("A", resume="a5-pay", session="new", git_runner=git)
    assert lease.slot == "work/A_2"
    assert lease.state == "leased"
    assert lease.session == "new"
    # live HEAD 가 a5-pay 그대로(같은 브랜치 재부착·checkout 무해).
    assert wp.current_branch("work/A_2", git_runner=git) == "a5-pay"


def test_alloc_resume_no_live_match_falls_through_to_idle_or_needscreate(wp):
    """슬롯 live HEAD 가 resume 브랜치와 다르면 live 매칭 안 됨 → idle 리스 경로로 폴백.

    저장 필드라면 어긋난 복사본으로 잘못 매칭할 수 있으나, live HEAD(다른 브랜치)면 분기2
    가 안 잡고 분기3(idle 리스 + 재체크아웃)으로 간다 — 드리프트 매칭 0 의 sensitivity.
    """
    _seed(wp, _lease(wp, slot="work/A_2", repo="A",
                     session="", pid=0, state="idle"))
    git = FakeGit(head="other-branch")  # 슬롯 live HEAD ≠ resume 브랜치
    lease = wp.alloc("A", resume="a5-pay", session="new", git_runner=git)
    # 분기3(idle 리스) 경로 — 슬롯을 leased 로 잡고 a5-pay 로 재체크아웃(live HEAD 전환).
    assert lease.slot == "work/A_2"
    assert lease.state == "leased"
    assert git.did("checkout", "a5-pay")
    assert wp.current_branch("work/A_2", git_runner=git) == "a5-pay"


# ════════════════════════════════════════════════════════════════════════
# Lease.from_dict — 구 장부 legacy `branch` 키 관용 무시 (T-0072 · ADR-0013 amend)
# branch 는 권위 필드 아님 — 정확성은 git 에서만 온다. 하위호환 read(로드 무파손).
# ════════════════════════════════════════════════════════════════════════


def test_from_dict_ignores_legacy_branch_key(wp):
    """구 장부의 legacy `branch` 키를 관용적으로 무시한다(하위호환·권위 필드 아님)."""
    legacy = {"slot": "work/A_1", "repo": "A", "branch": "stale-copy",
              "session": "me", "pid": 7, "started": "t", "state": "leased"}
    lease = wp.Lease.from_dict(legacy)
    # 로드는 깨지지 않고(하위호환), branch 는 Lease 권위 상태에 없다.
    assert lease.slot == "work/A_1"
    assert not hasattr(lease, "branch"), "legacy branch 키가 Lease 권위 필드로 들어옴(무시 위반)"
    # 재직렬화에도 branch 가 안 실린다(장부 저장 폐지·드리프트 원천 제거).
    assert "branch" not in lease.to_dict()


def test_read_ledger_legacy_file_with_branch_key_loads_clean(wp):
    """파일 레벨 하위호환 — branch 키 있는 구 장부 *파일* 을 _read_ledger 로 무파손 로드."""
    import json
    legacy = {"leases": [
        {"slot": "work/A_1", "repo": "A", "branch": "stale-copy", "session": "me",
         "pid": 7, "started": "t", "state": "leased", "test_cmd": None},
    ]}
    wp.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    wp.LEASES_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    with wp._lease_lock():
        leases = wp._read_ledger()
    assert len(leases) == 1
    assert leases[0].slot == "work/A_1"
    assert "branch" not in leases[0].to_dict()  # 재직렬화에도 legacy branch 안 실림


# ════════════════════════════════════════════════════════════════════════
# 보호 브랜치 pre-push 훅 (T-0076) — 설치(idempotent·core.hooksPath·sidecar·bare 부재 no-op)
#   + 생성된 훅을 직접 실행(보호 ref 거부 / feature 허용 / override 통과 / sidecar 읽기).
# ════════════════════════════════════════════════════════════════════════


def _run_hook(hook_path: Path, stdin: str, *, env_override: bool = False) -> int:
    """생성된 pre-push 훅을 `sh` 로 직접 실행하고 종료코드를 반환한다 (T-0076).

    훅은 stdin 으로 `<localref> <localsha> <remoteref> <remotesha>` 줄들을 받는다(실 git
    pre-push 계약). `env_override` 면 `PM_ALLOW_PROTECTED_PUSH=1` 을 환경에 둔다(사용자 명시
    OK 경로). 보호 ref 면 rc≠0(거부), feature/override 면 rc 0(통과).
    """
    env = dict(os.environ)
    if env_override:
        env["PM_ALLOW_PROTECTED_PUSH"] = "1"
    else:
        env.pop("PM_ALLOW_PROTECTED_PUSH", None)
    result = subprocess.run(
        ["sh", str(hook_path)],
        input=stdin, capture_output=True, text=True, env=env,
    )
    return result.returncode


# 실 git pre-push stdin 한 줄 — remote ref 만 보호 판정에 쓰인다(나머지는 sha placeholder).
def _push_line(remote_ref: str) -> str:
    return f"refs/heads/local 0000 {remote_ref} 1111\n"


def test_install_protected_hook_writes_hook_sidecar_and_sets_hookspath(wp):
    """install_protected_hook — 훅+sidecar write + bare core.hooksPath set (T-0076)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    ok = wp.install_protected_hook("A", ["main", "develop"], git_runner=git)
    assert ok is True
    hook_dir = wp.REPO_HOOKS_DIR / "A"
    hook = hook_dir / "pre-push"
    sidecar = hook_dir / "protected"
    assert hook.exists() and hook.read_text(encoding="utf-8").startswith("#!/bin/sh")
    # sidecar = 보호목록(줄당 1브랜치).
    assert sidecar.read_text(encoding="utf-8").splitlines() == ["main", "develop"]
    # core.hooksPath 를 bare 에 set — 절대경로(슬롯 push 게이트 wiring).
    config_calls = [c for c in git.calls if c[:2] == ["config", "core.hooksPath"]]
    assert len(config_calls) == 1
    assert config_calls[0][2] == str(hook_dir.resolve())


def test_install_protected_hook_idempotent_updates_sidecar(wp):
    """재설치(목록 변경) → sidecar 갱신·중복 무해 (멱등 자가치유·T-0076)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.install_protected_hook("A", ["main"], git_runner=git)
    wp.install_protected_hook("A", ["main", "release"], git_runner=git)  # 목록 변경 재설치
    sidecar = wp.REPO_HOOKS_DIR / "A" / "protected"
    assert sidecar.read_text(encoding="utf-8").splitlines() == ["main", "release"]
    # 훅은 단일(중복 파일 0)·core.hooksPath 매 호출 set(멱등).
    assert (wp.REPO_HOOKS_DIR / "A" / "pre-push").exists()
    assert sum(1 for c in git.calls if c[:2] == ["config", "core.hooksPath"]) == 2


def test_install_protected_hook_config_failure_returns_false(wp):
    """core.hooksPath config 실패(rc≠0) → False (설치 성공 오인 차단·codex T-0076 게이트).

    훅/sidecar 가 써졌어도 `git config core.hooksPath` 가 실패하면 슬롯 push 가 훅을 안 타 보호가
    *침묵 무력화* 된다 → install 이 False 를 돌려 호출부(pm-config)가 성공 보고를 안 하게 한다.
    """
    _mk_bare_placeholder(wp, "A")

    def config_fails(argv):
        if argv[:2] == ["config", "core.hooksPath"]:
            return (1, "fatal: config write failed")
        return (0, "")

    ok = wp.install_protected_hook("A", ["main"], git_runner=config_fails)
    assert ok is False, "core.hooksPath config 실패인데 설치 성공(True) 보고"


def test_install_protected_hook_bare_absent_is_noop(wp):
    """bare 부재 → no-op·False (게이트 대상 없음·훅/sidecar 미생성·config 미호출·T-0076)."""
    git = FakeGit()  # bare placeholder 안 만듦
    ok = wp.install_protected_hook("A", ["main"], git_runner=git)
    assert ok is False
    assert not (wp.REPO_HOOKS_DIR / "A").exists()   # 훅 디렉토리 미생성
    assert git.calls == []                          # core.hooksPath 미호출(회사 repo 무영향)


def test_install_protected_hook_no_company_repo_mutation(wp):
    """훅/config 는 `.project_manager/.local` + bare config 에만 — 서버 ref 시뮬 무변경 (T-0076).

    회사 repo 무영향 계약: install 이 건드리는 건 (a) `.local/repo-hooks/<repo>/` 안의 훅·
    sidecar, (b) bare 의 `core.hooksPath` config 1줄(client-side)뿐이다. 가짜 "서버 ref"
    파일을 두고 install 후 무변경임을 단언한다(서버/사용자 클론 무변경 시뮬).
    """
    bare = _mk_bare_placeholder(wp, "A")
    server_ref = bare / "refs" / "heads" / "main"  # 가짜 서버 ref(설치가 절대 안 건드림)
    server_ref.parent.mkdir(parents=True, exist_ok=True)
    server_ref.write_text("deadbeef\n", encoding="utf-8")
    git = FakeGit()
    wp.install_protected_hook("A", ["main"], git_runner=git)
    # 서버 ref 무변경 — install 은 .local 훅 + core.hooksPath config 만(ref 안 만짐).
    assert server_ref.read_text(encoding="utf-8") == "deadbeef\n"
    # git 호출은 config core.hooksPath 하나뿐(push/ref 조작 0).
    assert git.calls == [["config", "core.hooksPath", str((wp.REPO_HOOKS_DIR / "A").resolve())]]


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_rejects_protected_push(wp):
    """생성 훅 직접 실행 — remote ref 가 보호목록(main)이면 거부(rc≠0) (T-0076)."""
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main", "develop"], git_runner=FakeGit())
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    assert _run_hook(hook, _push_line("refs/heads/main")) != 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_allows_feature_push(wp):
    """생성 훅 직접 실행 — feature 브랜치(보호목록 아님)는 통과(rc 0) (T-0076)."""
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main", "develop"], git_runner=FakeGit())
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    assert _run_hook(hook, _push_line("refs/heads/feat-x")) == 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_override_env_passes_protected(wp):
    """생성 훅 직접 실행 — PM_ALLOW_PROTECTED_PUSH=1 이면 보호목록(main)도 통과(사용자 명시 OK·T-0076)."""
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main"], git_runner=FakeGit())
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    assert _run_hook(hook, _push_line("refs/heads/main"), env_override=True) == 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_reads_sidecar_protected_list(wp):
    """생성 훅 직접 실행 — 보호 판정은 sidecar(`protected`)를 읽는다 (generic 훅·T-0076).

    같은 훅 본문이라도 sidecar 목록에 따라 거부/통과가 갈린다 → 훅이 sidecar 를 읽음을 증명.
    `release` 만 보호 목록이면 main push 는 통과(목록에 없음)·release push 는 거부.
    """
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["release"], git_runner=FakeGit())  # main 은 목록에 없음
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    assert _run_hook(hook, _push_line("refs/heads/main")) == 0       # 목록에 없으니 통과
    assert _run_hook(hook, _push_line("refs/heads/release")) != 0    # 목록에 있으니 거부


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_multi_ref_rejects_if_any_protected(wp):
    """생성 훅 직접 실행 — 여러 ref push 중 하나라도 보호목록이면 거부 (T-0076)."""
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main"], git_runner=FakeGit())
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    stdin = _push_line("refs/heads/feat-x") + _push_line("refs/heads/main")
    assert _run_hook(hook, stdin) != 0


# ════════════════════════════════════════════════════════════════════════
# 보호훅 hooksPath 발화 — 실 git push e2e (T-0096·T-0076 후속)
# ════════════════════════════════════════════════════════════════════════
# 위 단위테스트는 훅을 *직접 실행*(_run_hook)하거나 core.hooksPath set 을 *FakeGit 호출
# 기록*으로만 본다 — `install_protected_hook` 의 wiring(bare core.hooksPath)을 거쳐 git 이
# 실 push 때 훅을 *자동 발화*시키는 end-to-end 경로는 단언하지 않는다. 여기선 실 git 으로
# bare(.repos/<repo>.git)+슬롯 worktree 를 만들고 install_protected_hook 후 슬롯에서 별도
# server bare 로 실제 `git push` 를 시도해 — config wiring 경유 발화·차단·override 를 못박는다.
#
# ⚠️ pre-push 훅 발화 전제: push 가 *실제 ref 갱신*을 해야 한다(없으면 "Everything up-to-date"
# 로 훅이 안 탄다). 그래서 각 push 전에 슬롯에 새 커밋을 만들어 ref 를 전진시킨다.


@_git_required
def test_real_git_protected_push_blocked_via_hookspath(proj, tmp_path):
    """실 git e2e — install_protected_hook 의 core.hooksPath wiring 을 거쳐 보호 main push 가
    실제로 차단되고(rc≠0) server bare 의 main 이 무변경임을 단언한다 (T-0096).

    이게 T-0076 의 빈틈을 메운다: 단위테스트는 훅을 직접 호출하거나 config set 을 호출
    기록으로만 봤다 — 여기선 `git push` 가 bare 의 core.hooksPath 를 해석해 훅을 자동
    발화시키는 *진짜 wiring 경로*를 실 push 로 증명한다.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)  # .repos/A.git bare = 슬롯 worktree base

    # push 대상 server bare(별도) — 슬롯이 여기로 push 한다. 무변경 검증 대상.
    server = tmp_path / "A-server.git"
    _git(tmp_path, "clone", "--bare", "-q", str(wp.bare_repo_path("A")), str(server))

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)

    # 보호훅 설치 — bare core.hooksPath 를 훅 디렉토리(절대경로)로 wiring(실 git config).
    assert wp.install_protected_hook("A", ["main"]) is True
    # wiring 단언: bare 의 core.hooksPath 가 훅 디렉토리 절대경로를 가리킨다.
    hooks_path = _git(wp.bare_repo_path("A"), "config", "core.hooksPath").stdout.strip()
    expected = str((wp.REPO_HOOKS_DIR / "A").resolve())
    assert Path(hooks_path) == Path(expected), \
        f"core.hooksPath wiring 안 됨: {hooks_path!r} != {expected!r}"

    # 슬롯에 server remote 추가 + 새 커밋(ref 갱신 — 없으면 훅 미발화).
    _git(slot_dir, "remote", "add", "server", str(server))
    (slot_dir / "change.txt").write_text("slot work on main\n", encoding="utf-8")
    _git(slot_dir, "add", "change.txt")
    _git(slot_dir, "commit", "-q", "-m", "slot change on main")
    slot_main = _git(slot_dir, "rev-parse", "main").stdout.strip()
    server_main_before = _git(server, "rev-parse", "main").stdout.strip()

    # 보호 main push — 훅이 hooksPath 경유 발화해 차단(rc≠0)해야 한다(_git 의 check=True
    # 미사용·rc 를 직접 본다).
    rc = subprocess.run(
        [_GIT, "-C", str(slot_dir), "push", "server", "main"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).returncode
    assert rc != 0, "보호 main push 가 hooksPath 훅에 차단되지 않음(rc=0)"
    # server bare 의 main 무변경(슬롯 새 커밋이 안 올라감).
    server_main_after = _git(server, "rev-parse", "main").stdout.strip()
    assert server_main_after == server_main_before, "차단됐는데 server main 이 갱신됨"
    assert server_main_after != slot_main, "server main 이 슬롯 main 으로 전진함(차단 실패)"


@_git_required
def test_real_git_feature_push_allowed_via_hookspath(proj, tmp_path):
    """실 git e2e — 비보호 브랜치(work/x) push 는 hooksPath 훅을 거쳐도 허용(rc 0)·server 반영 (T-0096)."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)
    server = tmp_path / "A-server.git"
    _git(tmp_path, "clone", "--bare", "-q", str(wp.bare_repo_path("A")), str(server))

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)
    assert wp.install_protected_hook("A", ["main"]) is True

    _git(slot_dir, "remote", "add", "server", str(server))
    # 비보호 브랜치 work/x 에서 새 커밋 → push(허용돼야).
    _git(slot_dir, "checkout", "-q", "-b", "work/x")
    (slot_dir / "feat.txt").write_text("feature work\n", encoding="utf-8")
    _git(slot_dir, "add", "feat.txt")
    _git(slot_dir, "commit", "-q", "-m", "feature commit")
    slot_feat = _git(slot_dir, "rev-parse", "work/x").stdout.strip()

    rc = subprocess.run(
        [_GIT, "-C", str(slot_dir), "push", "server", "work/x"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).returncode
    assert rc == 0, "비보호 work/x push 가 허용되지 않음(rc≠0)"
    # server bare 에 work/x 가 실제로 반영됐다.
    server_feat = _git(server, "rev-parse", "work/x").stdout.strip()
    assert server_feat == slot_feat, "work/x push 가 server 에 반영 안 됨"


@_git_required
def test_real_git_protected_push_override_env_allowed(proj, tmp_path):
    """실 git e2e — PM_ALLOW_PROTECTED_PUSH=1 이면 hooksPath 훅을 거쳐도 보호 main push 허용·전진 (T-0096).

    사용자 명시 OK override 가 wiring 경유 발화 경로에서도 작동함을 실 push 로 단언한다.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)
    server = tmp_path / "A-server.git"
    _git(tmp_path, "clone", "--bare", "-q", str(wp.bare_repo_path("A")), str(server))

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)
    assert wp.install_protected_hook("A", ["main"]) is True

    _git(slot_dir, "remote", "add", "server", str(server))
    (slot_dir / "change.txt").write_text("override work on main\n", encoding="utf-8")
    _git(slot_dir, "add", "change.txt")
    _git(slot_dir, "commit", "-q", "-m", "override change on main")
    slot_main = _git(slot_dir, "rev-parse", "main").stdout.strip()

    # override env — _git 헬퍼가 env 를 merge 한다(check=True·통과 기대).
    _git(slot_dir, "push", "server", "main", env={"PM_ALLOW_PROTECTED_PUSH": "1"})
    # server bare 의 main 이 슬롯 main 으로 전진(override 가 차단을 풀었다).
    server_main = _git(server, "rev-parse", "main").stdout.strip()
    assert server_main == slot_main, "override push 후 server main 이 전진 안 됨"
