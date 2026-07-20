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
import json
import multiprocessing as mp
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
SYNC_TIMEOUT = 60

# livegate fixture 의 `n` — board.LIVEGATE_RELEASE_PIN(릴리즈 wave 케이스 수)의 미러. 이 파일은
# 격리 원칙상 board.py 를 import 하지 않으므로(모듈 docstring) 값을 여기 둔다. 판정상 무관하다 —
# `_livegate_check` 는 status==pass ∧ head==rev 만 보고 n 은 안 보기 때문(check 채널·green 판정 무관).
# 값 자체는 board.LIVEGATE_RELEASE_PIN(=15·T-0278/T-0309/T-0349/T-0400) 과 schema 충실성 위해 맞춰 둔다(무관하나 혼란 방지).
_LIVEGATE_RELEASE_PIN = 15


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
        "TASKS_DIR": local / "tasks",   # task 서술 공간 tmp 재배선(T-0353·⑮) — 미재배선 시 실
                                        # repo `.local/tasks/` 로 누출(bind_task/end_task hermetic).
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

    bare 가드(`create_slot`)는 *경로 존재*(exists) + *실 bare 검증*(rev-parse·T-0294) 둘 다 본다.
    이 헬퍼는 경로 존재만 채우고, 실 bare 여부는 mock git_runner(FakeGit·is_bare=True 기본)가
    `rev-parse --is-bare-repository`→"true" 로 모델링해 통과시킨다. 실 git 통합테스트는
    `git clone --bare` 로 진짜 bare 를 만든다(이 헬퍼 미사용).
    """
    bare = wp.bare_repo_path(repo)
    bare.mkdir(parents=True, exist_ok=True)
    return bare


@pytest.fixture
def wp(proj):
    """tmp-바인딩 worktree_pool 모듈."""
    return _load_wp_bound(proj)


# ── mock git runner (단위테스트용 DI seam) ───────────────────────────────────


# FakeGit 의 `rev-parse HEAD` 기본 sha — 슬롯 git 스냅(T-0350·`_slot_head`)이 읽는 커밋 tip
# 모델값(40-hex). 실제 값은 판정에 무관(스냅 필드 존재/왕복만 검증)하나 그럴싸한 sha 로 둔다.
_DEFAULT_HEAD_SHA = "a1b2c3d4" * 5


class FakeGit:
    """주입형 git runner — 호출을 기록하고 미리 정한 (rc, out)을 돌려준다.

    `clean` 이면 status --porcelain 이 빈 문자열(=clean), `dirty` 면 변경 1줄을 돌려준다.
    실 git 을 안 쓰고 dirty/stash/checkout/worktree-add/submodule 경로를 결정적으로 검증.

    **live branch 모델(ADR-0013 amend T-0072·T-0377)**: `head` 가 슬롯 worktree 의 현재 HEAD(브랜치)
    를 모델링한다 — `symbolic-ref HEAD` 가 `refs/heads/<head>`(full ref)를 돌려주고, `checkout
    <b>`/`-B <b>` 는 실 git 처럼 head 를 갱신한다(`current_branch(slot)` live 조회·alloc 매칭이 이걸
    본다). `head=None` 이면 detached(실 git 처럼 `symbolic-ref` 가 rc≠0 → current_branch None).

    **git 스냅 모델(T-0350·ADR-0060)**: `head_sha` = `rev-parse HEAD`(슬롯 tip·스냅 head 필드),
    `submodule_status_out` = `submodule status` 원문(빈=submodule 없음·스냅 pin 파싱), `ancestor_ok`
    = `merge-base --is-ancestor` rc(True=조상 rc0 → descendant·False=비조상 rc1 → diverged).
    """

    def __init__(self, *, dirty: bool = False, head: "str | None" = None,
                 fetch_rc: int = 0, origin_has_base: bool = True,
                 is_bare: bool = True, head_resolves: bool = True,
                 head_sha: str = _DEFAULT_HEAD_SHA, submodule_status_out: str = "",
                 ancestor_ok: bool = True):
        self.dirty = dirty
        self.head = head        # 슬롯 worktree 의 현재 브랜치(=HEAD)·checkout 으로 갱신.
        self.head_sha = head_sha                        # `rev-parse HEAD` — 슬롯 tip(T-0350 스냅 head).
        self.submodule_status_out = submodule_status_out  # `submodule status` 원문(T-0350 스냅 pin).
        self.ancestor_ok = ancestor_ok                  # `merge-base --is-ancestor` rc(㉒ head 판정).
        # base 파생 origin-freshness 모델(T-0274): `fetch origin` rc + `refs/remotes/origin/<base>`
        # 해소 여부(show-ref rc). origin_has_base=True = fetch 로 갱신된 최신 remote-tracking ref
        # 존재(→ origin/<base> 파생), False = 미해소(→ 로컬 <base> 폴백).
        self.fetch_rc = fetch_rc
        self.origin_has_base = origin_has_base
        # bare 유효성 모델(T-0294·2조건 AND): (1) `rev-parse --is-bare-repository`=is_bare, (2)
        # `rev-parse --verify HEAD` rc=head_resolves. **유효 bare = is_bare=True AND head_resolves=
        # True** 둘 다. is_bare=True·head_resolves=False = core.bare=true 지만 HEAD 미해소(빈/부분
        # bare·objects 없는 죽은 clone) → 가드가 broken 으로 잡는다(codex must-fix 재현).
        self.is_bare = is_bare
        self.head_resolves = head_resolves
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            # 실 bare 형식 판정(T-0294) — `-C <bare>` 프리픽스 유무 무관 argv 스캔. 유효=rc0/"true".
            return (0, "true\n") if self.is_bare else (0, "false\n")
        if "rev-parse" in argv and "--verify" in argv and argv[-1] == "HEAD":
            # HEAD 해소 판정(T-0294) — 유효 bare 는 rc0, 빈/부분 bare 는 rc128(HEAD 미해소).
            return (0, "0123abc\n") if self.head_resolves else (128, "fatal: Needed a single revision\n")
        if argv == ["rev-parse", "HEAD"]:
            return (0, self.head_sha + "\n")       # 슬롯 tip(T-0350 스냅 head·_slot_head).
        if argv == ["submodule", "status"]:
            return (0, self.submodule_status_out)  # 스냅 pin 파싱(빈="submodule 없음"·T-0350).
        if argv[:2] == ["merge-base", "--is-ancestor"]:
            return (0, "") if self.ancestor_ok else (1, "")  # ㉒ head 후손 판정(rc0=조상).
        if argv[:2] == ["status", "--porcelain"]:
            return (0, " M file.py\n") if self.dirty else (0, "")
        if argv == ["symbolic-ref", "HEAD"]:
            # T-0377: full ref(`--short` 없이) 반환 — current_branch 가 refs/heads/ 접두를 벗긴다
            # (동명 태그 모호성 접두 회피). detached(head=None) → 실 git 처럼 rc≠0(symbolic ref 아님).
            return (1, "fatal: ref HEAD is not a symbolic ref\n") if self.head is None \
                else (0, "refs/heads/" + self.head + "\n")
        if argv[:1] == ["checkout"]:
            # `checkout <b>` 또는 `checkout -B <b>` — 실 git 처럼 head 를 갱신(브랜치 전환).
            self.head = argv[-1]
            return (0, "")
        if argv[:2] == ["fetch", "origin"]:
            return (self.fetch_rc, "" if self.fetch_rc == 0 else "fatal: could not fetch\n")
        if argv[:3] == ["show-ref", "--verify", "--quiet"]:
            # refs/remotes/origin/<base> 해소 판정 — resolvable=rc0, 미해소=rc1(→ 로컬 폴백).
            return (0, "") if self.origin_has_base else (1, "")
        return (0, "")

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


def _seed(wp, *leases):
    """리스장부에 엔트리를 직접 심는다(테스트 전제 구성)."""
    with wp._lease_lock():
        wp._write_ledger(list(leases))


def _seed_tasks(wp, *names):
    """장부의 `tasks` 컬렉션에 명명 task 레코드를 심는다 — reclaim/재부착 조인 제외 전제 구성.

    `_write_ledger`(형제 `leases`)와 별개 컬렉션(`_write_tasks`·같은 파일·같은 락). `_seed` 로
    leases 를 먼저 심은 뒤 이걸 호출하면 둘 다 보존된다(top-level round-trip). pid 는 판정 무관
    (reclaim 조인은 `session ∈ tasks 이름집합` 만 본다)라 임의(1)."""
    with wp._lease_lock():
        wp._write_tasks([wp.Task(name=n, pid=1, started="t") for n in names])


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
    # 같은 슬롯 재체크아웃 발생 (--no-recurse-submodules — ambient recurse override·ADR-0051).
    assert git.did("checkout", "--no-recurse-submodules", "a5-pay")


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
    assert git.did("checkout", "--no-recurse-submodules", "a2-new")  # 재체크아웃(live HEAD 와 다르므로)
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
        if argv == ["symbolic-ref", "HEAD"]:
            # T-0377: full ref 반환 — current_branch 가 refs/heads/ 접두를 벗긴다.
            return (1, "fatal: ref HEAD is not a symbolic ref\n") if self.head is None \
                else (0, "refs/heads/" + self.head + "\n")
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
# task-명의 alloc = 항상 신규 대여 (ADR-0068 I3·④·T-0398)
# owner_task 로 부르면 idempotent 1경로를 건너뛰어 같은 repo 복수 보유를 지원하고, task 소유
# 슬롯(session ∈ tasks 장부)은 reclaim_stale/재부착에서 조인 제외해 즉사 CLI pid 로도 보호한다
# (bound 마커가 아니라 tasks 장부 조인 — 구버전 bound-부재 lease 도 마이그레이션 0 으로 보호·R3).
# ════════════════════════════════════════════════════════════════════════


def test_task_alloc_always_new_lease_two_slots(wp):
    """같은 task 가 같은 repo 를 2연속 alloc(owner_task) → 서로 다른 2슬롯 대여(멱등 폐기·I3).

    idle 슬롯 2개를 심고 owner_task='job' 으로 두 번 alloc — 옛 idempotent 는 첫 슬롯을
    다시 반환(silent aliasing)했으나 I3 는 최소 번호부터 신규 슬롯을 대여한다(복수 보유).
    """
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"),
          _lease(wp, slot="work/A_2", repo="A", session="", pid=0, state="idle"))
    git = FakeGit()
    first = wp.alloc("A", owner_task="job", git_runner=git)
    second = wp.alloc("A", owner_task="job", git_runner=git)
    assert {first.slot, second.slot} == {"work/A_1", "work/A_2"}   # 2개 서로 다른 슬롯
    assert first.slot != second.slot                              # silent aliasing 아님
    # 둘 다 task 명의 leased(session=task·⑥) — slots_for_task 가 둘 다 본다.
    held = {l.slot for l in wp.slots_for_task("job")}
    assert held == {"work/A_1", "work/A_2"}


def test_task_alloc_lease_not_bound_protected_by_tasks_join(wp):
    """task-명의 alloc lease 는 bound 가 아니다 — reclaim/재부착 보호는 tasks 장부 조인이 담당(R3).

    bound 는 사람-bind 축 그대로. task 소유 슬롯의 회수 제외 근거 = `session ∈ tasks 장부`.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"))
    _seed_tasks(wp, "job")
    git = FakeGit()
    lease = wp.alloc("A", owner_task="job", git_runner=git)
    assert lease.session == "job"
    assert lease.bound is False                # bound 축 오염 안 함(사람-bind 전용)
    assert wp.list_leases()[0].bound is False


def test_task_alloc_old_bound_absent_lease_survives_other_alloc_reclaim(wp):
    """**구버전 alloc 이 만든 bound-부재 task lease**(pid 즉사)도 타 alloc reclaim 이 회수 못 한다(R3 핵심).

    실측 시나리오(PM 78·codex R3): tasks 장부엔 'job' 이 있고, job 이 예전에 대여한 slot 1 lease 는
    `bound` 키가 없어 False 로 로드된다(구장부). pid 는 즉사 CLI pid(999999=죽음). 이후 *다른* 세션이
    slot 2 를 alloc 하면 진입 reclaim_stale 이 slot 1 을 stale 오판해 회수하던 구멍 — bound 마커만으론
    안 걸린다. tasks 장부 조인(session ∈ tasks)이 구·신 lease 를 모두 보호한다(마이그레이션 0).
    """
    _seed(wp,
          # 구장부 task lease — bound 키 없음(=False 로드)·pid 죽음.
          wp.Lease(slot="work/A_1", repo="A", session="job", pid=999999,
                   started="t", state="leased", bound=False),
          _lease(wp, slot="work/A_2", repo="A", session="", pid=0, state="idle"))
    _seed_tasks(wp, "job")   # 'job' 이 명명 task 로 장부에 존재(소유 단일 진실)
    git = FakeGit()  # clean
    other = wp.alloc("A", session="other", git_runner=git)
    assert other.slot == "work/A_2"                     # 신규 idle 대여(slot 1 회수 안 됨)
    held = {l.slot for l in wp.slots_for_task("job")}
    assert held == {"work/A_1"}                          # job 명의 유지(tasks 조인 보호)


def test_reclaim_stale_recovers_non_task_dead_pid_unchanged(wp):
    """sensitivity/현행 유지 — 비-task 세션(tasks 장부에 없는)의 pid-죽은 lease 는 그대로 회수된다.

    조인 제외는 **task 소유 슬롯에만** 걸린다 — tasks 장부에 없는 세션(crash 한 슬롯-모드 세션 등)의
    stale lease 는 현행처럼 회수(idle 화)해야 풀 가용성이 유지된다. 위 task 보호와 대조되는 negative.
    """
    _seed(wp,
          wp.Lease(slot="work/A_1", repo="A", session="dead-session", pid=999999,
                   started="t", state="leased", bound=False))
    # tasks 장부 비움 — 'dead-session' 은 명명 task 가 아니다.
    git = FakeGit()
    reclaimed = wp.reclaim_stale(git_runner=git)
    assert reclaimed == ["work/A_1"]                     # 비-task 구장부 lease 는 현행 회수 유지
    assert wp.list_leases()[0].state == "idle"


def test_task_alloc_ignores_branch_reattach_path(wp):
    """owner_task + branch 가 함께 와도 재부착(2경로) 안 탄다 — 항상 신규 idle 대여(I3·codex must-fix ②).

    타 세션(other)이 그 브랜치를 체크아웃 중인 leased 슬롯이 있어도 그 session/pid 를 덮지 않고
    (탈취 차단) idle 슬롯을 신규 대여한다.
    """
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", session="other", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="", pid=0, state="idle"))
    git = FakeGit(head="a5")   # 모든 슬롯 live HEAD = a5(2경로 재부착 후보 조건 성립)
    lease = wp.alloc("A", owner_task="job", branch="a5", git_runner=git)
    assert lease.slot == "work/A_2"          # idle 신규 대여(slot 1 재부착 안 함)
    assert lease.bound is False              # task-명의도 bound 축 오염 안 함(tasks 조인이 보호)
    s1 = next(l for l in wp.list_leases() if l.slot == "work/A_1")
    assert s1.session == "other" and s1.state == "leased"   # 타 lease 탈취 안 됨


def test_slot_session_alloc_cannot_steal_task_slot_via_branch_reattach(wp):
    """슬롯-세션 alloc(branch=X)이 그 브랜치를 체크아웃 중인 **task 소유 슬롯**을 재부착 탈취 못 함(④·R3).

    reclaim 조인과 동형으로 재부착(2경로)도 `session ∈ tasks 장부` lease 를 제외한다 — bound 부재
    구장부 task lease 도 보호. idle 이 없으면 NeedsCreate(정직한 소진).
    """
    _seed(wp,
          # job 소유(bound 부재 구장부) lease 가 브랜치 a5 를 체크아웃 중·pid 살아있음(alive).
          wp.Lease(slot="work/A_1", repo="A", session="job", pid=os.getpid(),
                   started="t", state="leased", bound=False))
    _seed_tasks(wp, "job")
    git = FakeGit(head="a5")   # slot 1 live HEAD = a5(재부착 후보)
    with pytest.raises(wp.NeedsCreate):
        wp.alloc("A", session="other", branch="a5", git_runner=git)   # 슬롯-세션 alloc
    s1 = wp.list_leases()[0]
    assert s1.session == "job" and s1.state == "leased"   # 탈취 안 됨(tasks 조인 재부착 제외)


def test_task_alloc_branch_no_idle_raises_needscreate(wp):
    """owner_task + branch 인데 idle 없음 → 재부착으로 도피하지 않고 NeedsCreate(I3·codex must-fix ②).

    구 버그: 2경로가 타 세션의 브랜치-매칭 leased 슬롯을 job 명의로 재부착(탈취)하며 신규처럼 반환.
    수정 후엔 idle 이 없으면 풀 소진으로 정직하게 NeedsCreate(호출부 사용자 게이트).
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other", state="leased"))
    git = FakeGit(head="a5")   # slot 1 live HEAD = a5(브랜치-매칭 재부착 후보)
    with pytest.raises(wp.NeedsCreate):
        wp.alloc("A", owner_task="job", branch="a5", git_runner=git)
    # slot 1(other)은 그대로 — 탈취 안 됨.
    s1 = wp.list_leases()[0]
    assert s1.session == "other" and s1.state == "leased"


def test_slot_session_alloc_idempotent_path_unchanged(wp):
    """슬롯-세션 도착 alloc(owner_task 없음)은 idempotent 현행 유지 — I3 는 task 축만 바꾼다.

    같은 session='me' 로 두 번 alloc → 같은 슬롯 반환(get-or-create-my-lease)·복수 안 생김.
    task-명의(owner_task) 경로와 대조되는 불변 제약(slot-모드 100% 불변·ADR-0068).
    """
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", session="me", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="", pid=0, state="idle"))
    git = FakeGit()
    first = wp.alloc("A", session="me", git_runner=git)
    second = wp.alloc("A", session="me", git_runner=git)
    assert first.slot == second.slot == "work/A_1"   # idempotent(신규 대여 안 함)
    assert not first.bound                            # slot-세션 도착 alloc 은 bound 아님


# ════════════════════════════════════════════════════════════════════════
# selective submodule 재동기 (ADR-0051 파일럿 T-α · T-0275)
# 브랜치 전환(_checkout) 성공 후 detached(consume) submodule 만 superproject pin 으로
# 재동기 · on-branch(dev) 는 skip(작업 보호·크럭스 A) · dirty 는 skip+경고 · submodule
# 없는 repo 는 no-op. 역할은 submodule 의 live git HEAD 로 판별한다(무장부·ADR-0051 §Decision 1).
# ════════════════════════════════════════════════════════════════════════


class _SubmoduleGit:
    """submodule 시나리오 주입 runner — superproject(슬롯) 바인딩 `git -C <slot>` 모델.

    `subs` = {path: (role, dirty)} — role="branch"(on-branch=dev·`symbolic-ref -q HEAD` rc0)
    또는 "detached"(consume·rc≠0), dirty=bool(sub 의 `status --porcelain` 변경 유무). `submodule
    status` 는 각 sub 를 ` <sha> <path>` 로 나열한다(플래그 무관·경로만 파싱). submodule
    컨텍스트 명령은 엔진의 다중 `-C` 배선대로 `["-C", <sub>, ...]` 로 온다(superproject cwd 기준
    sub 재진입). `status_rc≠0` 이면 `submodule status` 조회 실패(fail-soft no-op 검증용).
    """

    def __init__(self, subs=None, *, head="feat", status_rc=0, existing_branches=(),
                 checkout_b_hard_fail=False):
        self.subs = dict(subs or {})
        self.head = head
        self.status_rc = status_rc
        # `dev` 의 `checkout -b <branch>` 폴백(이미 존재하는 브랜치) 검증용 — 이 집합에 든
        # 브랜치명은 실 git 처럼 `-b` rc≠0(already exists) + `show-ref refs/heads/<b>` rc0(존재)
        # → dev 가 plain `checkout <b>` 로 전환 폴백. 목록 밖은 show-ref rc≠0(미존재).
        self.existing_branches = set(existing_branches)
        # `-b` 가 *기존 브랜치와 무관*하게 실패(충돌/lock 등)하는 경우 모델 — 미존재 브랜치의
        # `-b` 실패는 show-ref rc≠0 라 dev 가 폴백하지 않고 원 rc≠0 를 전파해야(폴백 정밀화 검증).
        self.checkout_b_hard_fail = checkout_b_hard_fail
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv == ["submodule", "status"]:
            if self.status_rc != 0:
                return (self.status_rc, "fatal: not a git repository\n")
            lines = "".join(
                f" 1111111111111111111111111111111111111111 {p}\n" for p in self.subs
            )
            return (0, lines)
        if argv[:2] == ["submodule", "update"]:
            return (0, "")
        if argv[:1] == ["checkout"]:
            self.head = argv[-1]          # 실 git 처럼 head 갱신(브랜치 전환).
            return (0, "")
        if argv == ["symbolic-ref", "HEAD"]:
            # T-0377: full ref 반환 — current_branch 가 refs/heads/ 접두를 벗긴다.
            return (0, "refs/heads/" + self.head + "\n") if self.head else (1, "")
        if argv[:1] == ["-C"] and len(argv) >= 3:
            sub, rest = argv[1], argv[2:]
            role, dirty = self.subs.get(sub, ("detached", False))
            if rest[:3] == ["symbolic-ref", "-q", "HEAD"]:
                # on-branch(dev) → rc0 · detached(consume) → rc≠0 (실 `symbolic-ref -q` 동형).
                return (0, "refs/heads/x\n") if role == "branch" else (1, "")
            if rest[:2] == ["status", "--porcelain"]:
                return (0, " M f\n") if dirty else (0, "")
            if rest[:3] == ["show-ref", "--verify", "--quiet"]:
                # dev 폴백 정밀화 — `refs/heads/<b>` 존재 판정(존재=rc0·미존재=rc≠0).
                ref = rest[3] if len(rest) > 3 else ""
                br = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
                return (0, "") if br in self.existing_branches else (1, "")
            if rest[:1] == ["checkout"]:
                # `dev` — submodule 을 on-branch(dev)로. `-b <b>` 실패 모델: b 가 이미 존재하면
                # rc≠0(already exists·→ dev 가 show-ref 확인 후 plain checkout 폴백), 또는
                # checkout_b_hard_fail 이면 무관하게 rc≠0(충돌/lock·→ 폴백 안 함). 성공 시 role 을
                # "branch"로 뒤집어 이후 selective resync 가 dev 로 판별·skip 하게 한다(실 git 모델).
                if rest[:2] == ["checkout", "-b"] and len(rest) > 2:
                    if rest[2] in self.existing_branches:
                        return (128, f"fatal: a branch named '{rest[2]}' already exists\n")
                    if self.checkout_b_hard_fail:
                        return (1, "fatal: unable to create branch (index lock)\n")
                self.subs[sub] = ("branch", dirty)
                return (0, "")
        return (0, "")

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)

    def resynced(self) -> list:
        """selective 재동기(`submodule update ... -- <sub>`) 된 sub 경로 목록."""
        return [c[-1] for c in self.calls
                if c[:2] == ["submodule", "update"] and "--" in c]


def test_resync_detached_submodule_resyncs_to_pin(wp):
    """detached(consume) + clean submodule → superproject pin 으로 재동기(update 호출)."""
    git = _SubmoduleGit({"vendor/sub": ("detached", False)})
    wp._resync_submodules_selective(wp.slot_path("work/A_1"), git_runner=git)
    assert git.resynced() == ["vendor/sub"]
    assert git.did("submodule", "update", "--init", "--recursive", "--force", "--", "vendor/sub")


def test_resync_on_branch_submodule_skipped(wp):
    """on-branch(dev) submodule → skip(재동기 안 함) — dev 작업 보호(크럭스 A).

    비공허: skip 로직을 없애면 dev submodule 이 detached pin 으로 낚아채여 이 단언이 red.
    """
    git = _SubmoduleGit({"vendor/sub": ("branch", False)})
    wp._resync_submodules_selective(wp.slot_path("work/A_1"), git_runner=git)
    assert git.resynced() == [], "on-branch(dev) submodule 은 재동기하면 안 됨(작업 파괴)"


def test_resync_dirty_detached_submodule_skipped_with_warning(wp, capsys):
    """detached 이나 dirty(미커밋) submodule → skip + 경고(작업 유실 방지)."""
    git = _SubmoduleGit({"vendor/sub": ("detached", True)})
    wp._resync_submodules_selective(wp.slot_path("work/A_1"), git_runner=git)
    assert git.resynced() == [], "dirty submodule 재동기는 미커밋 작업을 날린다"
    err = capsys.readouterr().err
    assert "vendor/sub" in err and "dirty" in err


def test_resync_no_submodules_is_noop(wp):
    """submodule 없는 repo(status rc0·빈 출력) → no-op(update 0회·per-sub 조회 0회·예외 0)."""
    git = _SubmoduleGit({})
    wp._resync_submodules_selective(wp.slot_path("work/A_1"), git_runner=git)
    assert git.resynced() == []
    assert not any(c[:1] == ["-C"] for c in git.calls), "submodule 없는데 per-sub 조회 발생"


def test_resync_status_query_failure_is_noop(wp):
    """`submodule status` rc≠0(조회 불가) → no-op fail-soft(checkout 은 이미 성공·raise 금지)."""
    git = _SubmoduleGit({"vendor/sub": ("detached", False)}, status_rc=1)
    wp._resync_submodules_selective(wp.slot_path("work/A_1"), git_runner=git)
    assert git.resynced() == []
    assert not any(c[:1] == ["-C"] for c in git.calls)


def test_resync_mixed_selective_only_clean_detached(wp, capsys):
    """혼합 — detached-clean=재동기 · on-branch=skip · detached-dirty=skip+경고 (selective 비공허).

    셋 중 detached-clean 하나만 재동기돼야 한다. skip/dirty 가드 중 어느 하나라도 없으면
    다른 sub 가 재동기 목록에 섞여 이 단언이 red(가드 비공허 실증).
    """
    git = _SubmoduleGit({
        "vendor/consume": ("detached", False),   # 재동기 대상
        "libs/dev":       ("branch", False),     # dev → skip
        "vendor/wip":     ("detached", True),    # dirty → skip + 경고
    })
    wp._resync_submodules_selective(wp.slot_path("work/A_1"), git_runner=git)
    assert git.resynced() == ["vendor/consume"]
    assert "vendor/wip" in capsys.readouterr().err


def test_checkout_required_success_triggers_resync(wp):
    """배선 — `_checkout_required` 성공(브랜치 전환) 직후 slot 에 selective 재동기가 실행된다."""
    git = _SubmoduleGit({"vendor/sub": ("detached", False)}, head="old")
    wp._checkout_required("work/A_1", "feat", git_runner=git)
    assert git.did("checkout", "--no-recurse-submodules", "feat")
    assert git.resynced() == ["vendor/sub"], "checkout 성공 후 재동기 배선 안 됨"


def test_checkout_required_failure_skips_resync(wp):
    """배선 negative — checkout 실패면 CheckoutFailed raise·재동기 미실행(성공 후에만·비공허)."""
    git = _CheckoutFailGit(head="old")
    with pytest.raises(wp.CheckoutFailed):
        wp._checkout_required("work/A_1", "feat", git_runner=git)
    assert not any(c[:1] == ["submodule"] for c in git.calls), \
        "checkout 실패인데 재동기(submodule status/update)를 시도함"


# ════════════════════════════════════════════════════════════════════════
# 운영중 관리 backbone: dev / sync (ADR-0049/0051 파일럿 T-γ·T-0277)
# ════════════════════════════════════════════════════════════════════════
# dev: submodule 을 on-branch(dev)로 만들어 이후 selective resync 가 skip 하게 한다("작업 중"
# 선언). sync: `_resync_submodules_selective` 수동 트리거(브랜치 전환 없이 명시 재동기).


def test_dev_makes_submodule_on_branch(wp):
    """dev 가 submodule 을 dev 브랜치로 지정(`-C <sub> checkout -b <branch>`) — on-branch 화."""
    git = _SubmoduleGit({"vendor/sub": ("detached", False)})
    rc, _out = wp.dev("work/A_1", "vendor/sub", "feat-x", git_runner=git)
    assert rc == 0
    assert git.did("-C", "vendor/sub", "checkout", "-b", "feat-x")
    # submodule 이 on-branch(dev)가 됐다 — 역할이 detached → branch 로 전이(실 git 모델).
    assert git.subs["vendor/sub"][0] == "branch"


def test_dev_then_selective_resync_skips_it(wp):
    """비공허 통합 — dev 로 on-branch 화한 submodule 을 이후 selective resync 가 skip(dev 보호).

    dev 가 detached 를 on-branch 로 뒤집으므로, 곧이은 `_resync_submodules_selective` 는 그
    submodule 을 dev 역할로 판별해 재동기하지 않는다. dev 의 on-branch 화가 안 먹으면(role 미전이)
    submodule 이 detached 로 남아 재동기 목록에 섞여 이 단언이 red(비공허).
    """
    git = _SubmoduleGit({"vendor/sub": ("detached", False)})
    wp.dev("work/A_1", "vendor/sub", "feat-x", git_runner=git)
    wp._resync_submodules_selective(wp.slot_path("work/A_1"), git_runner=git)
    assert git.resynced() == [], "dev 지정한 submodule 이 selective resync 에 낚아채임(dev 파괴)"


def test_dev_existing_branch_falls_back_to_plain_checkout(wp):
    """dev — `-b <branch>` 가 rc≠0(이미 존재)이고 show-ref 로 존재 확인되면 `checkout <branch>` 폴백."""
    git = _SubmoduleGit({"vendor/sub": ("detached", False)}, existing_branches={"dev-old"})
    rc, _out = wp.dev("work/A_1", "vendor/sub", "dev-old", git_runner=git)
    assert rc == 0
    assert git.did("-C", "vendor/sub", "checkout", "-b", "dev-old"), "먼저 -b 를 시도해야"
    assert git.did("-C", "vendor/sub", "show-ref", "--verify", "--quiet", "refs/heads/dev-old"), \
        "폴백 전 show-ref 로 브랜치 존재를 확인해야(폴백 정밀화)"
    assert git.did("-C", "vendor/sub", "checkout", "dev-old"), "-b 실패 후 plain checkout 폴백 안 됨"
    assert git.subs["vendor/sub"][0] == "branch"


def test_dev_checkout_b_hard_failure_propagates_without_fallback(wp):
    """dev — 브랜치 미존재인데 `-b` 가 실패(충돌/lock)하면 원 rc≠0 전파·plain checkout 폴백 안 함.

    codex bundle(폴백 정밀화·비공허) — show-ref rc≠0(미존재)이므로 폴백하면 안 된다. `-b` 실패를
    무조건 "기존 브랜치"로 간주해 폴백하던 옛 로직이면 checkout 이 한 번 더 불려 red.
    """
    git = _SubmoduleGit({"vendor/sub": ("detached", False)}, checkout_b_hard_fail=True)
    rc, out = wp.dev("work/A_1", "vendor/sub", "newbranch", git_runner=git)
    assert rc != 0, "미존재 브랜치의 -b 실패는 그대로 전파해야(진단 흐림 방지)"
    assert "lock" in out
    assert not git.did("-C", "vendor/sub", "checkout", "newbranch"), \
        "미존재(show-ref rc≠0)인데 plain checkout 폴백을 실행함"
    assert git.subs["vendor/sub"][0] == "detached", "실패인데 on-branch(dev)로 전이됨"


def test_dev_rejects_submodule_outside_slot(wp):
    """dev — sub 가 슬롯 submodule 목록 밖(절대경로·traversal·오타)이면 거부·checkout 미실행 (must-fix 1).

    비공허: 목록 대조(allowlist)를 없애면 슬롯 밖 경로에 `-C <sub> checkout` 이 실행돼 이 단언들이
    red. 정상 submodule(vendor/sub)은 통과(대조군).
    """
    git = _SubmoduleGit({"vendor/sub": ("detached", False)})
    for bad in ("/etc/evil", "../other-repo", "vendor/unknown"):
        with pytest.raises(wp.SubmoduleNotInSlot):
            wp.dev("work/A_1", bad, "feat", git_runner=git)
    # 거부 케이스는 그 경로에 checkout side-effect 를 내지 않았다(경계 보호·비공허).
    assert not any(c[:2] == ["-C", "/etc/evil"] for c in git.calls)
    assert not any(c[:2] == ["-C", "../other-repo"] for c in git.calls)
    assert not any(c[:2] == ["-C", "vendor/unknown"] for c in git.calls)
    # 정상 submodule 은 통과(대조군) — 검증이 정상 경로를 막지 않음.
    rc, _ = wp.dev("work/A_1", "vendor/sub", "feat", git_runner=git)
    assert rc == 0
    assert git.subs["vendor/sub"][0] == "branch"


def test_dev_rejects_when_status_query_fails(wp):
    """dev — `submodule status` rc≠0(조회 실패)면 빈 목록 → fail-closed 거부(검증 우회 금지·must-fix 1)."""
    git = _SubmoduleGit({"vendor/sub": ("detached", False)}, status_rc=1)
    with pytest.raises(wp.SubmoduleNotInSlot):
        wp.dev("work/A_1", "vendor/sub", "feat", git_runner=git)
    assert not any(c[:1] == ["-C"] and c[2:3] == ["checkout"] for c in git.calls), \
        "status 조회 실패인데 checkout 을 실행함(fail-open)"


def test_sync_detached_submodule_resyncs_to_pin(wp):
    """sync — detached(consume) clean submodule 을 pin 에 수동 재동기(브랜치 전환 없이)."""
    git = _SubmoduleGit({"vendor/sub": ("detached", False)})
    wp.sync("work/A_1", git_runner=git)
    assert git.resynced() == ["vendor/sub"]
    # 브랜치 전환(슬롯 checkout) 없이 재동기만 — sync 는 명시 트리거.
    assert not git.did("checkout", "--no-recurse-submodules"), "sync 는 브랜치 전환을 하지 않는다"


def test_sync_on_branch_submodule_skipped(wp):
    """sync — on-branch(dev) submodule 은 skip(작업 보호·크럭스 A·비공허)."""
    git = _SubmoduleGit({"vendor/sub": ("branch", False)})
    wp.sync("work/A_1", git_runner=git)
    assert git.resynced() == [], "on-branch(dev) submodule 은 sync 로도 재동기하면 안 됨"


def test_sync_dirty_detached_submodule_skipped_with_warning(wp, capsys):
    """sync — detached 이나 dirty(미커밋) submodule 은 skip + 경고(작업 유실 방지)."""
    git = _SubmoduleGit({"vendor/sub": ("detached", True)})
    wp.sync("work/A_1", git_runner=git)
    assert git.resynced() == []
    err = capsys.readouterr().err
    assert "vendor/sub" in err and "dirty" in err


def test_sync_delegates_to_resync_backbone(wp, monkeypatch):
    """배선 — sync 는 `_resync_submodules_selective`(T-0275 백본)를 슬롯 경로로 위임한다(중복 구현 X)."""
    seen = {}

    def spy(slot_path_, *, git_runner=None):
        seen["slot_path"] = slot_path_
        seen["git_runner"] = git_runner

    monkeypatch.setattr(wp, "_resync_submodules_selective", spy)
    sentinel = FakeGit()
    wp.sync("work/A_1", git_runner=sentinel)
    assert seen["slot_path"] == wp.slot_path("work/A_1")
    assert seen["git_runner"] is sentinel


# ════════════════════════════════════════════════════════════════════════
# CLI 진입점 — argv 파싱 + 슬롯 해소 (dev/sync·--repo/--slot·ADR-0049 T-γ·T-0277·ADR-0057·T-0318)
# ════════════════════════════════════════════════════════════════════════


def test_resolve_current_slot_explicit_slot_normalized(wp):
    """`--slot` 명시 — `work/` 접두 유무 무관 정규형(`work/<repo>_<N>`) 반환."""
    assert wp._resolve_current_slot("work/A_1") == "work/A_1"
    assert wp._resolve_current_slot("A_1") == "work/A_1"


def test_resolve_current_slot_from_session_lease(wp, monkeypatch):
    """`--slot` 미지정 — 세션(PM_SESSION_NAME)이 보유한 단일 leased 슬롯으로 해소."""
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="me", state="leased"))
    assert wp._resolve_current_slot(None) == "work/A_1"


def test_resolve_current_slot_no_lease_raises(wp, monkeypatch):
    """`--slot` 미지정·매칭 leased 슬롯 0개 → SlotResolutionError(명시 요구·비공허)."""
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    # cwd 유입 차단 — 실제 cwd 는 tmp WORK_DIR 밖이라 _slot_from_cwd None(기본).
    with pytest.raises(wp.SlotResolutionError):
        wp._resolve_current_slot(None)


def test_resolve_current_slot_ambiguous_raises(wp, monkeypatch):
    """`--slot` 미지정·세션 leased 슬롯 ≥2(모호) → SlotResolutionError(명시 요구)."""
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    _seed(
        wp,
        _lease(wp, slot="work/A_1", repo="A", session="me", state="leased"),
        _lease(wp, slot="work/A_2", repo="A", session="me", state="leased"),
    )
    with pytest.raises(wp.SlotResolutionError):
        wp._resolve_current_slot(None)


def test_resolve_current_slot_rejects_malformed_slot(wp):
    """`--slot` 형식검증 — traversal·빈값·`work/` 단독·경로구분자는 SlotResolutionError (must-fix 2).

    비공허: 형식검증(`_SLOT_ID_RE`)을 없애면 `../x`·`work/../x` 가 `slot_path` 결합으로 슬롯 루트
    밖을 가리켜 side-effect 경계가 깨진다. 정상 `work/A_1`/`A_1` 은 통과(대조군·아래 별도 테스트).
    """
    for bad in ("../x", "work/../x", "work/", "", "work/x/../y", "/abs/path", "work/A_1/sub"):
        with pytest.raises(wp.SlotResolutionError):
            wp._resolve_current_slot(bad)


def test_resolve_current_slot_accepts_valid_slot_forms(wp):
    """`--slot` 정상형(정규형/접두생략/underscore repo)은 통과·정규화 (must-fix 2 대조군)."""
    assert wp._resolve_current_slot("work/A_1") == "work/A_1"
    assert wp._resolve_current_slot("A_1") == "work/A_1"
    assert wp._resolve_current_slot("work/project_manager_2") == "work/project_manager_2"
    assert wp._resolve_current_slot("project_manager_2") == "work/project_manager_2"


def test_slot_from_cwd_derives_slot_when_inside_worktree(wp, monkeypatch):
    """cwd 유입 — cwd 가 `<WORK_DIR>/<repo>_<N>/...` 안이면 그 슬롯, 밖이면 None."""
    slot_dir = wp.WORK_DIR / "A_1" / "vendor" / "sub"
    slot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(slot_dir)
    assert wp._slot_from_cwd() == "work/A_1"
    monkeypatch.chdir(wp.REPO)   # WORK_DIR 밖
    assert wp._slot_from_cwd() is None


def test_main_dev_dispatches_to_dev_backbone(wp, monkeypatch, capsys):
    """CLI — `dev <sub> <branch> --repo A --slot 1` 이 dev 백본을 해소된 슬롯/인자로 호출하고 rc 0."""
    seen = {}

    def spy_dev(slot, sub, branch, *, git_runner=None):
        seen.update(slot=slot, sub=sub, branch=branch)
        return 0, ""

    monkeypatch.setattr(wp, "dev", spy_dev)
    rc = wp.main(["dev", "vendor/sub", "feat-x", "--repo", "A", "--slot", "1"])
    assert rc == 0
    assert seen == {"slot": "work/A_1", "sub": "vendor/sub", "branch": "feat-x"}
    assert "vendor/sub" in capsys.readouterr().out


def test_main_dev_failure_returns_rc1(wp, monkeypatch, capsys):
    """CLI — dev 백본 rc≠0 이면 main 이 rc 1 + stderr 에러 surface(침묵 성공 금지·비공허)."""
    monkeypatch.setattr(wp, "dev", lambda *a, **k: (1, "boom"))
    rc = wp.main(["dev", "vendor/sub", "feat-x", "--repo", "A", "--slot", "1"])
    assert rc == 1
    assert "boom" in capsys.readouterr().err


def test_main_sync_dispatches_to_sync_backbone(wp, monkeypatch, capsys):
    """CLI — `sync --repo A --slot 1` 이 sync 백본을 해소된 슬롯으로 호출하고 rc 0."""
    seen = {}
    monkeypatch.setattr(wp, "sync", lambda slot, *, git_runner=None: seen.update(slot=slot))
    rc = wp.main(["sync", "--repo", "A", "--slot", "1"])
    assert rc == 0
    assert seen == {"slot": "work/A_1"}
    assert "work/A_1" in capsys.readouterr().out


def test_main_sync_unresolvable_slot_returns_rc1(wp, monkeypatch, capsys):
    """CLI — 슬롯 자동해소 실패(SlotResolutionError)면 main 이 rc 1 + 안내(오타깃 금지·비공허)."""
    monkeypatch.setenv("PM_SESSION_NAME", "nobody")
    monkeypatch.setattr(wp, "sync", lambda *a, **k: pytest.fail("슬롯 미해소인데 sync 를 호출함"))
    rc = wp.main(["sync"])   # 인자 전무(kind=none)·매칭 leased 0개
    assert rc == 1
    assert "슬롯" in capsys.readouterr().err


def test_main_rejects_traversal_repo_returns_rc1(wp, monkeypatch):
    """CLI — `--repo ../x --slot 1`(traversal)는 sync 백본 호출 전에 rc 1 로 거부 (must-fix 2·비공허)."""
    monkeypatch.setattr(wp, "sync", lambda *a, **k: pytest.fail("형식 위반 슬롯인데 sync 를 호출함"))
    rc = wp.main(["sync", "--repo", "../x", "--slot", "1"])
    assert rc == 1


def test_main_dev_rejects_out_of_slot_submodule_returns_rc1(wp, monkeypatch, capsys):
    """CLI — dev 가 슬롯 밖 submodule(SubmoduleNotInSlot) 이면 rc 1 + stderr 안내 (must-fix 1)."""
    def boom(*a, **k):
        raise wp.SubmoduleNotInSlot("work/A_1", "/etc/evil", ["vendor/sub"])
    monkeypatch.setattr(wp, "dev", boom)
    rc = wp.main(["dev", "/etc/evil", "feat", "--repo", "A", "--slot", "1"])
    assert rc == 1
    assert "/etc/evil" in capsys.readouterr().err


# ── ADR-0057/T-0318 — --repo/--slot 통일 신규 표면 (구 alias 제거·actor repo-alone·bare --slot) ──


def test_main_bare_slot_without_repo_fails_loud(wp, monkeypatch, capsys):
    """CLI — bare `--slot`(--repo 없음)은 parse_identity 가 ValueError → main 이 rc 1(DoD)."""
    monkeypatch.setattr(wp, "sync", lambda *a, **k: pytest.fail("bare --slot 인데 sync 를 호출함"))
    rc = wp.main(["sync", "--slot", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--repo" in err


def test_main_repo_alone_resolves_single_active_slot(wp, monkeypatch, capsys):
    """CLI — `--repo A`(슬롯 무) 단독은 actor 해소 — 그 repo 활성 슬롯이 1개면 자동 해소."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="someone-else", state="leased"))
    seen = {}
    monkeypatch.setattr(wp, "sync", lambda slot, *, git_runner=None: seen.update(slot=slot))
    rc = wp.main(["sync", "--repo", "A"])
    assert rc == 0
    assert seen == {"slot": "work/A_1"}


def test_main_repo_alone_ambiguous_active_slots_fails_loud(wp, monkeypatch, capsys):
    """CLI — `--repo A` 단독인데 활성 슬롯이 ≥2개면 rc 1 + `--slot` 안내(모호 거부)."""
    _seed(
        wp,
        _lease(wp, slot="work/A_1", repo="A", session="s1", state="leased"),
        _lease(wp, slot="work/A_2", repo="A", session="s2", state="leased"),
    )
    monkeypatch.setattr(wp, "sync", lambda *a, **k: pytest.fail("모호한데 sync 를 호출함"))
    rc = wp.main(["sync", "--repo", "A"])
    assert rc == 1
    assert "--slot" in capsys.readouterr().err


def test_main_repo_alone_no_active_slot_fails_loud(wp, monkeypatch, capsys):
    """CLI — `--repo A` 단독인데 그 repo 의 활성 슬롯이 0개면 rc 1(오타깃 금지)."""
    monkeypatch.setattr(wp, "sync", lambda *a, **k: pytest.fail("활성 슬롯 없는데 sync 를 호출함"))
    rc = wp.main(["sync", "--repo", "A"])
    assert rc == 1
    assert "--slot" in capsys.readouterr().err


def test_main_sync_rejects_legacy_session_flag(wp):
    """CLI `sync` 파서에 구 `--session` alias 가 없다 — argparse 가 미등록 인자로 거부 (ADR-0057 B-2)."""
    with pytest.raises(SystemExit):
        wp.main(["sync", "--session", "A_1"])


def test_main_dev_rejects_legacy_worktree_slot_flag(wp):
    """CLI `dev` 파서에 구 `--worktree-slot` alias 가 없다 — argparse 가 미등록 인자로 거부 (ADR-0057 B-2)."""
    with pytest.raises(SystemExit):
        wp.main(["dev", "vendor/sub", "feat", "--worktree-slot", "A_1"])


def test_resolve_actor_slot_for_repo_translates_identity_args_ambiguous(wp):
    """`_resolve_actor_slot_for_repo` — identity_args.SlotResolutionError(모호)를 이 모듈의
    SlotResolutionError 로 번역해 전파(main 의 단일 except 로 수렴·B-1)."""
    _seed(
        wp,
        _lease(wp, slot="work/A_1", repo="A", session="s1", state="leased"),
        _lease(wp, slot="work/A_2", repo="A", session="s2", state="leased"),
    )
    with pytest.raises(wp.SlotResolutionError):
        wp._resolve_actor_slot_for_repo("A")


def test_resolve_actor_slot_for_repo_single_active_normalizes(wp):
    """`_resolve_actor_slot_for_repo` — 활성 슬롯 1개면 정규형 `work/<repo>_<N>` 반환."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s1", state="leased"))
    assert wp._resolve_actor_slot_for_repo("A") == "work/A_1"


def test_resolve_actor_slot_for_repo_none_active_fails_loud(wp):
    """`_resolve_actor_slot_for_repo` — 그 repo 활성 슬롯이 0개면 SlotResolutionError."""
    with pytest.raises(wp.SlotResolutionError):
        wp._resolve_actor_slot_for_repo("A")


# 실 git 백스톱(dev/sync)은 `_git_required`·`_git`·`_init_repo` 헬퍼 정의 이후에 둔다(아래
# "실 git 통합" 구역·test_real_git_dev_and_sync_selective / test_real_git_dev_existing_branch_switches).


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


def test_create_slot_owner_task_leases_under_task(wp):
    """create_slot(owner_task) → 생성분을 그 task 명의로 leased(ⓓB·ADR-0068).

    `worktree add <repo> --task <이름>` 경로 — 생성 직후 그 슬롯을 task 명의로 대여한다
    (min-idle 재탐색 없이 생성분 직결). session=task(⑥). reclaim/재부착 보호는 tasks 장부
    조인이 담당하고(bound 아님·R3), owner_task 는 기바인딩 task 라 항상 tasks 장부에 있다.
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", base="develop", owner_task="job", git_runner=git)
    assert lease.state == "leased"
    assert lease.session == "job"     # task 명의(⑥)
    assert lease.bound is False       # bound 축 오염 안 함(사람-bind 전용·tasks 조인이 보호)
    # slots_for_task 가 생성분을 본다(생성+대여 한 흐름).
    assert [l.slot for l in wp.slots_for_task("job")] == [lease.slot]


def test_create_slot_owner_task_and_readonly_mutually_exclusive(wp):
    """create_slot(owner_task, readonly) → 엔진 fail-loud(무소유 vs task 명의 모순·codex suggestion).

    CLI 가드(cmd_worktree_add)만 믿지 않고 엔진 자체가 부작용(worktree add) 이전에 ValueError 로
    막는다 — 타 호출부/미래 경로 방어. 장부에 아무 것도 안 남는다.
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    with pytest.raises(ValueError):
        wp.create_slot("A", owner_task="job", readonly=True, git_runner=git)
    assert wp.list_leases() == []   # 부작용 이전 raise(provisional 도 안 남김)


def test_create_slot_picks_next_free_number(wp):
    """create_slot — 기존 슬롯 번호를 회피해 다음 빈 번호를 쓴다(`<repo>_<N>`)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="x", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="y", state="idle"))
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", session="me", git_runner=git)
    assert lease.slot == "work/A_3"


def test_create_slot_skip_submodule_when_disabled(wp):
    """init_submodules=False → submodule *init(update)* 호출 안 함.

    (git 스냅[T-0350]의 read-only `submodule status` 는 별개 축 — init 이 아니라 pin 기록 조회라
    disabled 여도 호출된다. 여기선 init 억제만 검사한다.)
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.create_slot("A", session="me", init_submodules=False, git_runner=git)
    assert not git.did("submodule", "update")


def test_create_slot_worktree_add_failure_raises(wp):
    """worktree add 가 비0 → RuntimeError(불완전 슬롯 등록 방지)."""
    _mk_bare_placeholder(wp, "A")
    def failing(argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과 → worktree add 까지 도달(T-0294).
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
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과(T-0294).
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
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과(T-0294).
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
    # git 스냅(T-0350)의 read-only `submodule status` 는 제외하고 *init(update)* 명령만 검사한다.
    sub_calls = [c for c in git.calls if c[:2] == ["submodule", "update"]]
    assert sub_calls == [["submodule", "update", "--init", "--recursive", "--force"]]


def test_create_slot_submodule_init_failure_message_surfaces_rc(wp):
    """rc≠0 + 빈 out(Windows 인코딩 캡처 유실) 에도 에러 메시지에 rc + argv 가 실린다(T-0067).

    plain 메시지가 `out` 만 실으면 빈 에러(`git submodule init failed: ''`)로 다음 사람이
    막힌다 — rc 와 실행한 git argv 를 surface 해 진단 가능하게 한다.
    """
    _mk_bare_placeholder(wp, "A")
    def failing_empty(argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과(T-0294).
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
# create_slot bare 실검증 — 부분/깨진 bare 조용히 통과 근절 (T-0294)
# ════════════════════════════════════════════════════════════════════════


def test_create_slot_valid_bare_passes_guard(wp):
    """DoD(1) 유효 bare → 통과 — 경로 존재 + rev-parse "true" 면 슬롯 정상 생성 (T-0294).

    _is_valid_bare 가드가 유효 bare 를 막지 않음을 확증한다. sensitivity: 가드가 실제로
    `rev-parse --is-bare-repository` 를 물었는지(경로 존재만 안 봄) 호출 기록으로 확인한다.
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit(is_bare=True)
    lease = wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert lease.slot == "work/A_1"
    assert lease.state == "leased"
    # 가드가 rev-parse 로 실 bare 를 판정했다(경로 존재만이 아님).
    assert git.did("-C", str(wp.bare_repo_path("A")), "rev-parse", "--is-bare-repository")
    assert git.did("worktree", "add")   # 유효 → worktree add 까지 도달


def test_create_slot_broken_bare_fails_loud(wp):
    """DoD(2) 경로존재 but 무효(rev-parse "false") → BareRepoMissing(broken=True) fail-loud (T-0294).

    중단된 clone 이 남긴 부분/깨진 bare 는 exists()=True 지만 rev-parse 가 "true" 가 아니다 →
    조용한 통과(worktree add 가 날 git 에러로 죽음) 대신 broken=True 진단으로 fail-loud 한다.
    worktree add 는 불리지 않고(가드가 앞) 장부에 슬롯 0(불완전 슬롯 0).
    """
    _mk_bare_placeholder(wp, "A")          # 경로는 존재 (부분 bare 잔존 모사)
    git = FakeGit(is_bare=False)           # rev-parse "false" = 무효 bare
    with pytest.raises(wp.BareRepoMissing) as exc:
        wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert exc.value.broken is True
    msg = str(exc.value)
    assert "부분/깨진 bare" in msg          # exists-but-broken 진단(경로부재 메시지와 구별)
    assert "재hydrate" in msg or "repo add" in msg   # 다음 행동 힌트(수동 삭제 후 재생성)
    assert not git.did("worktree", "add"), "무효 bare 인데 worktree add 가 불림(조용한 통과 위험)"
    assert wp.list_leases() == []


def test_create_slot_bare_valid_format_but_no_head_fails_loud(wp):
    """DoD(2·codex must-fix) is-bare "true" 지만 HEAD 미해소(빈/부분 bare) → fail-loud (T-0294).

    `git init --bare`(또는 objects fetch 전 죽은 clone)가 남긴 빈 bare 는 core.bare=true(is-bare
    "true")지만 `rev-parse --verify HEAD` rc≠0 — worktree add 의 base 로 못 쓴다(HEAD 체크아웃 실패).
    is-bare 형식만 보면 통과시키던 갭(audit #1 잔존)을 HEAD 해소 검사가 닫는다. sensitivity: HEAD
    검사를 빼면 이 케이스가 통과해 red — is_bare=True 인데 head_resolves=False 만으로 broken 판정.
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit(is_bare=True, head_resolves=False)   # 형식은 bare 지만 HEAD 미해소(빈 bare)
    with pytest.raises(wp.BareRepoMissing) as exc:
        wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert exc.value.broken is True
    assert not git.did("worktree", "add"), "HEAD 없는 bare 인데 worktree add 가 불림(조용한 통과)"
    assert wp.list_leases() == []
    # 가드가 HEAD 해소를 실제로 물었다(is-bare 만 아님).
    assert git.did("-C", str(wp.bare_repo_path("A")), "rev-parse", "--verify", "HEAD")


def test_create_slot_missing_vs_broken_bare_distinct(wp):
    """DoD(3) 경로부재 → BareRepoMissing(broken=False) — 부재와 exists-but-broken 을 구별한다 (T-0294).

    경로부재(placeholder 미생성)는 종전 broken=False(hydrate 안내), 경로존재+무효는 broken=True
    (재생성 안내)로 갈린다 — 같은 예외 타입이지만 broken 플래그·메시지가 다르다.
    """
    # 경로부재: placeholder 안 만듦 → exists()=False → broken=False.
    git = FakeGit(is_bare=True)
    with pytest.raises(wp.BareRepoMissing) as exc_missing:
        wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert exc_missing.value.broken is False
    assert "hydrate" in str(exc_missing.value)   # 종전 부재 안내(T-0291)


def test_create_slot_bare_validation_uses_injected_runner(wp):
    """DoD(4) DI mock 경로 무영향 — 판정은 주입 runner 로만(실 git 안 탐), mock 이 verdict 을 뒤집는다 (T-0294).

    같은 경로(placeholder 존재)에서 주입 mock 의 is_bare 만 True/False 로 갈려 통과/fail-loud 가
    갈린다 → 가드가 실 git 이 아닌 *주입 runner* 의 rev-parse 로 판정함을 보인다(DI seam 보존).
    """
    _mk_bare_placeholder(wp, "A")
    # 유효 mock → 통과.
    ok = FakeGit(is_bare=True)
    lease = wp.create_slot("A", branch="a1", session="me", git_runner=ok)
    assert lease.state == "leased"
    # 같은 placeholder, 무효 mock → fail-loud (판정이 mock 에서 옴·경로는 동일).
    bad = FakeGit(is_bare=False)
    with pytest.raises(wp.BareRepoMissing):
        wp.create_slot("A", branch="a2", session="me", git_runner=bad)


# ════════════════════════════════════════════════════════════════════════
# create_slot base 브랜치 — 슬롯 브랜치를 base 에서 파생 (T-0075)
# ════════════════════════════════════════════════════════════════════════


def test_create_slot_base_derives_slot_branch(wp):
    """create_slot(base=) → fetch origin 후 `worktree add -b <repo>_<N> <path> origin/<base>` (T-0075·T-0274).

    base 가 주어지면 먼저 `fetch origin`(T-0274) 후 슬롯 브랜치 `<repo>_<N>`(슬롯 식별자·
    T-0072 정합)를 *`origin/<base>` 최신에서 파생*한다 — bare HEAD 도 로컬 동결 head 도 아닌
    origin 최신에서 슬롯 작업 브랜치를 판다. 주입 git_runner 로 정확한 argv 를 검증한다.
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()   # fetch_rc=0·origin_has_base=True → origin/develop 해소
    lease = wp.create_slot("A", base="develop", session="me", git_runner=git)
    assert lease.slot == "work/A_1"
    # fetch 가 worktree add 전에 불렸다(origin 최신 갱신).
    assert git.did("fetch", "origin")
    # 정확한 argv — 슬롯 브랜치 이름은 `A_1`(work/ 접두 없음·슬롯 식별자), 파생 기준=origin/develop.
    # `--no-track` = remote-tracking ref 파생 시 git autoSetupMerge 자동 upstream 설정 억제(슬롯=작업스트림).
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    assert add_calls == [["worktree", "add", "--no-track", "-b", "A_1",
                          str(wp.slot_path("work/A_1")), "origin/develop"]]


def test_create_slot_base_fetches_before_worktree_add(wp):
    """base 파생 — `fetch origin` 이 worktree add *보다 먼저* 불린다 (T-0274·origin 최신 선반영)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.create_slot("A", base="develop", session="me", git_runner=git)
    fetch_idx = next(i for i, c in enumerate(git.calls) if c[:2] == ["fetch", "origin"])
    add_idx = next(i for i, c in enumerate(git.calls) if c[:2] == ["worktree", "add"])
    assert fetch_idx < add_idx


def test_create_slot_base_fetch_failure_falls_back_to_local(wp, capsys):
    """base 파생 — fetch 실패(오프라인)면 경고 + 로컬 `<base>` 폴백·슬롯 생성 성공 (T-0274·fail-soft).

    fetch rc≠0 이면 origin/<base> 를 신뢰하지 않고(stale/부재 가능) 로컬 `<base>`(동결 head)에서
    슬롯을 판다 — worktree add 핵심 부작용은 보존한다(슬롯 생성 계속).
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit(fetch_rc=1)   # fetch 실패
    lease = wp.create_slot("A", base="develop", session="me", git_runner=git)
    assert lease.slot == "work/A_1"          # 슬롯 생성은 계속(fail-soft)
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    assert add_calls == [["worktree", "add", "--no-track", "-b", "A_1",
                          str(wp.slot_path("work/A_1")), "develop"]]   # 로컬 <base> 폴백(origin/develop 아님)
    assert not git.did("show-ref")            # fetch 실패면 origin ref 판정 건너뜀
    assert "fetch origin" in capsys.readouterr().err


def test_create_slot_base_origin_ref_unresolvable_falls_back_to_local(wp):
    """base 파생 — fetch 성공해도 origin/<base> 미해소면 로컬 `<base>` 폴백 (T-0274).

    refs/remotes/origin/<base> 가 없으면(구 bare·refspec 미보정 등) origin/<base> 파생이
    불가하므로 로컬 `<base>` 에서 판다 — 슬롯 생성은 계속(회귀 0).
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit(fetch_rc=0, origin_has_base=False)   # fetch ok·origin ref 미해소
    wp.create_slot("A", base="develop", session="me", git_runner=git)
    assert git.did("fetch", "origin")
    assert git.did("show-ref", "--verify", "--quiet", "refs/remotes/origin/develop")
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    assert add_calls == [["worktree", "add", "--no-track", "-b", "A_1",
                          str(wp.slot_path("work/A_1")), "develop"]]   # 로컬 폴백


def test_create_slot_base_uses_no_track(wp):
    """base 파생 — worktree add 에 `--no-track` 을 넣어 슬롯 브랜치 upstream 자동설정을 억제한다 (T-0274 결정·codex).

    슬롯 브랜치 `<repo>_<N>` 는 작업스트림 컨테이너 — origin/<base>(remote-tracking ref)에서 `-b`
    로 파면 git 기본 `branch.autoSetupMerge=true` 가 upstream 을 *자동* 설정한다. 엔진은 명시
    `config` 를 안 하지만 그것만으론 자동설정을 못 막는다(mock argv 맹점·false green) → `--no-track`
    이 유일한 방어선이다. 여기선 argv 에 `--no-track` 존재 + 명시 config 부재만 본다(git 의 실제
    자동설정 억제는 실 git 백스톱 `test_real_git_create_slot_base_no_upstream_on_slot_branch` 가 검증).
    """
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    wp.create_slot("A", base="develop", session="me", git_runner=git)
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    assert add_calls and "--no-track" in add_calls[0]      # 자동 upstream 억제(유일 방어선)
    assert not any(c[:1] == ["config"] for c in git.calls)  # 명시 tracking config 도 없음


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
    # bare-HEAD 경로는 fetch/show-ref 무관 — origin 파생(T-0274)은 base 경로 전용(회귀 0).
    assert not git.did("fetch")
    assert not git.did("show-ref")


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
    # branch 경로는 fetch/origin 파생을 안 탄다 — origin 파생(T-0274)은 base 전용 경로(회귀 0).
    assert not git.did("fetch")
    assert not git.did("show-ref")


def test_create_slot_base_picks_next_free_number_in_branch_name(wp):
    """base 파생 슬롯 브랜치 이름이 다음 빈 슬롯 번호를 따른다(`<repo>_<N>`·T-0075·T-0274)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="x", state="leased"))
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", base="develop", session="me", git_runner=git)
    assert lease.slot == "work/A_2"
    add_calls = [c for c in git.calls if c[:2] == ["worktree", "add"]]
    # 슬롯 번호 2 → 브랜치 이름 `A_2`, 파생 기준=origin/develop(T-0274), `--no-track` 명시.
    assert add_calls == [["worktree", "add", "--no-track", "-b", "A_2",
                          str(wp.slot_path("work/A_2")), "origin/develop"]]


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
    """create_slot 의 worktree add 러너가 `.repos/<repo>.git` bare 를 가리키는지(컨텍스트 배선·ADR-0011 §31).

    DI seam — git_runner 미주입이면 worktree add = `_real_git_runner_interactive(bare, ...)`(T-0292
    console-visible). 그 팩토리가 어떤 cwd 로 바인딩되는지 캡처해 family bare(multi-PM 루트 REPO 아님)
    인지 결정적으로 검증한다(`git -C <cwd>` 로 실행하므로 cwd=bare 면 add 가 bare 컨텍스트에서 일어난다).
    init_submodules=False 라 인터랙티브 팩토리는 worktree add 로만 불린다.
    """
    _mk_bare_placeholder(wp, "A")
    captured = []

    def spy_interactive(cwd, *, timeout=None):
        captured.append(cwd)
        return FakeGit()  # 모든 git 호출 성공 stub(add 성공)

    monkeypatch.setattr(wp, "_real_git_runner_interactive", spy_interactive)
    # bare 실검증 가드(T-0294)는 captured 러너(`_real_git_runner`)로 rev-parse 한다 — placeholder
    # 는 실 bare 가 아니므로 실 러너면 무효 판정된다. FakeGit(is_bare=True)로 stub 해 가드 통과.
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: FakeGit())
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


def test_bare_repo_missing_message_guides_hydrate(wp):
    """BareRepoMissing 메시지가 hydrate 경로를 안내한다 (T-0291·오해 회귀 가드).

    2번째 사용자(등록됨·`.repos/` mirror 부재·multi-user 공유 채택 폴더)에게 `--git` 없는
    `repo add <repo>` 가 areas 등록 URL 로 mirror 를 hydrate 함을 안내해야 한다 — 진짜 원인은
    bare mirror 부재인데 "repo add 먼저"(URL 재제공 강제 오해)만 던지면 안 된다. 메시지 내용을
    단언해 재약화/오해 회귀를 가드한다.
    """
    exc = wp.BareRepoMissing("svc", Path("/x/.repos/svc.git"))
    msg = str(exc)
    assert "svc" in msg and ".repos/svc.git" in msg   # 진짜 원인=bare mirror 부재 명시
    assert "hydrate" in msg                            # hydrate 경로 안내(핵심·T-0291)
    assert "--git 불요" in msg                          # URL 재제공 불요(오해 해소)
    assert "areas" in msg.lower()                      # areas 등록 URL 로 hydrate


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
# _default_session — board.session_name 과 동형 count-based 유도 (ADR-0040 D1·T-0073)
# env > lease 장부 leased 1개면 그 session(단일-lease 유도) > (장부 부재·leased 0 = solo)
# local.conf session= > <host>-<pid>. leased ≥2 면 local.conf 건너뜀. board 와 tail 만
# 다르다 — 여기는 lease *취득*의 국소 임시 명명이라 미해소를 <host>-<pid> 로 폴백한다
# (host-pid 는 세션-귀속 아닌 국소 용처에만 잔존·ADR-0040). 저장측(이 모듈)과 매칭측
# (board.session_name)이 어긋나면 per-slot test_cmd 가 미스된다(T-0066 must-fix).
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


def test_default_session_single_lease_derives_session(wp, proj, monkeypatch):
    """env·conf 없음·leased 슬롯 1개 → 그 session 유도 (ADR-0040 count-based·board 동형)."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _seed(wp, _lease(wp, slot="work/project_manager_1", repo="project_manager",
                     session="project_manager_1", state="leased"))
    assert wp._default_session() == "project_manager_1"


def test_default_session_single_lease_wins_over_local_conf(wp, proj, monkeypatch):
    """단일-lease 값과 local.conf 값이 다르면 유도값(lease) 승 (저장 쪽지보다 파생 진실)."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _write_local_conf(proj, "session=stale-conf\n")
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="derived-1", state="leased"))
    assert wp._default_session() == "derived-1"


def test_default_session_idle_leases_not_counted(wp, proj, monkeypatch):
    """idle 행은 count 대상 아님 — leased 1개(+idle)면 그 leased session 유도."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="live_1", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="", pid=0, state="idle"))
    assert wp._default_session() == "live_1"


def test_default_session_two_leases_skips_conf_falls_back_host_pid(wp, proj, monkeypatch):
    """leased ≥2 (모호) → local.conf 건너뜀 → `<host>-<pid>` (board 는 None·여긴 국소 폴백).

    host-pid 최종 폴백은 세션-귀속 아닌 국소 용처(lease 취득 임시 명명)에만 잔존한다(ADR-0040)
    — board.session_name 은 같은 조건에서 None(surface)/fail-loud(required)로 간다(tail 상이).
    """
    import socket
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _write_local_conf(proj, "session=some-conf\n")   # 있어도 무시(모호 → 건너뜀)
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="a_1", state="leased"),
          _lease(wp, slot="work/B_1", repo="B", session="b_1", state="leased"))
    assert wp._default_session() == f"{socket.gethostname()}-{os.getpid()}"


def test_default_session_falls_back_to_host_pid(wp, proj, monkeypatch):
    """env(둘 다)·lease·local.conf session= 모두 없음 → `<host>-<pid>` (국소 폴백 잔존)."""
    import socket
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # 장부 없음(leased 0)·local.conf 없음.
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
def test_real_git_is_valid_bare_requires_head_resolution(wp, tmp_path):
    """실 git — `_is_valid_bare`: 빈 bare(HEAD 미해소)=False · 커밋 있는 bare=True (T-0294·codex must-fix·#3).

    전 검증이 mock(FakeGit/GitFake)이라 codex 가 잡은 실 케이스(is-bare "true" 지만 HEAD 없는 빈
    bare)를 mock 가정이 놓쳤다 — 그 갭을 실 git 로 고정한다. `git init --bare` 는 core.bare=true
    (is-bare "true")지만 objects/HEAD 가 없어 `worktree add` base 로 못 쓴다(실측: `rev-parse
    --verify HEAD` rc128). is-bare 형식만 보던 판정을 HEAD 해소(rc0) 검사가 닫음을 실 git 로 증명.
    비네트워크·짧은 로컬 op(기존 실-git 테스트 관례). sensitivity: HEAD 검사를 빼면 (1) 이 True 로
    뒤집혀 red(빈-bare 통과 회귀 재현).
    """
    # (1) 빈 bare — `git init --bare`(커밋/HEAD 없음) → is-bare "true" 지만 HEAD 미해소 → False.
    empty_bare = tmp_path / "empty.git"
    _git(tmp_path, "init", "--bare", "-q", str(empty_bare))
    assert wp._is_valid_bare(empty_bare, runner=wp._real_git_runner(empty_bare)) is False, \
        "빈 bare(HEAD 미해소)를 유효로 오판(is-bare 형식만 봄·codex must-fix)"
    # (2) 커밋 있는 real repo → `clone --bare` → is-bare "true" + HEAD rc0 → True.
    valid_bare = _mk_real_bare(wp, "A", tmp_path)
    assert wp._is_valid_bare(valid_bare, runner=wp._real_git_runner(valid_bare)) is True, \
        "정상 bare(HEAD 해소)를 broken 오판"


@_git_required
def test_real_git_dev_and_sync_selective(proj, tmp_path):
    """실 git 백스톱 — dev 가 submodule 을 on-branch 로 만들고, sync 는 detached=재동기·dev=skip (T-0277).

    실 superproject + 2 submodule(consume·dev)로: (1) dev 로 dev submodule 을 브랜치화 →
    on-branch 확인, (2) consume submodule 을 pin 과 어긋난 detached 로 만든 뒤 sync → consume 은
    pin 으로 재동기되고 dev(on-branch)는 그대로(skip)임을 검증한다. mock 이 못 잡는 다중 `-C`
    배선·실 `symbolic-ref`/`submodule update` 거동을 백스톱한다.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)

    # 두 submodule origin(각 2커밋 — pin 을 뒤로 되돌려 detached drift 를 만들 수 있게).
    def _mk_sub_origin(name):
        o = _init_repo(tmp_path / f"{name}-origin")
        (o / "v.txt").write_text("v1\n", encoding="utf-8")
        _git(o, "add", "v.txt"); _git(o, "commit", "-q", "-m", "v1")
        c1 = _git(o, "rev-parse", "HEAD").stdout.strip()
        (o / "v.txt").write_text("v2\n", encoding="utf-8")
        _git(o, "add", "v.txt"); _git(o, "commit", "-q", "-m", "v2")
        return o, c1

    consume_origin, consume_c1 = _mk_sub_origin("consume")
    dev_origin, _dev_c1 = _mk_sub_origin("dev")

    # superproject 슬롯(실 git·submodule 은 로컬 file:// origin·-c protocol.file.allow=always).
    slot = proj / "work" / "A_1"
    slot.mkdir(parents=True, exist_ok=True)
    _git(slot, "init", "-q", "-b", "main")
    (slot / "README.md").write_text("super\n", encoding="utf-8")
    _git(slot, "add", "README.md"); _git(slot, "commit", "-q", "-m", "super init")
    allow = ["-c", "protocol.file.allow=always"]
    _git(slot, *allow, "submodule", "add", str(consume_origin), "vendor/consume")
    _git(slot, *allow, "submodule", "add", str(dev_origin), "libs/dev")
    _git(slot, "commit", "-q", "-m", "add submodules")

    # (1) dev — libs/dev 를 dev 브랜치로 지정 → on-branch.
    rc, _out = wp.dev("work/A_1", "libs/dev", "mywork")
    assert rc == 0
    dev_head = _git(slot / "libs" / "dev", "symbolic-ref", "--short", "HEAD").stdout.strip()
    assert dev_head == "mywork", f"dev submodule on-branch 아님: {dev_head!r}"

    # consume 을 pin 과 어긋난 detached 로 — 워킹트리를 c1(구 커밋)으로 checkout(detached).
    _git(slot / "vendor" / "consume", "checkout", "-q", consume_c1)
    assert _git(slot / "vendor" / "consume", "rev-parse", "HEAD").stdout.strip() == consume_c1

    # (2) sync — consume(detached)은 pin(v2)으로 재동기·dev(on-branch)는 skip(mywork 유지).
    wp.sync("work/A_1")
    consume_after = _git(slot / "vendor" / "consume", "rev-parse", "HEAD").stdout.strip()
    assert consume_after != consume_c1, "detached consume 이 pin 으로 재동기 안 됨(sync 실패)"
    dev_after = _git(slot / "libs" / "dev", "symbolic-ref", "--short", "HEAD").stdout.strip()
    assert dev_after == "mywork", "on-branch dev submodule 이 sync 로 낚아채임(skip 실패)"


@_git_required
def test_real_git_dev_existing_branch_switches(proj, tmp_path):
    """실 git 백스톱 — dev 가 *이미 존재하는* submodule 브랜치엔 `-b` 실패 후 plain checkout 전환 (폴백 정밀화·T-0277).

    submodule 에 브랜치 b1·b2 를 만든 뒤 b1 로 다시 dev — `checkout -b b1` 이 실 git 에서 rc≠0
    (already exists)이고 `show-ref refs/heads/b1` rc0(존재)이라 plain `checkout b1` 로 전환된다.
    mock-only 이던 폴백 경로를 실 git 으로 백스톱한다(reviewer suggestion).
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)

    sub_origin = _init_repo(tmp_path / "sub-origin")
    (sub_origin / "v.txt").write_text("v1\n", encoding="utf-8")
    _git(sub_origin, "add", "v.txt"); _git(sub_origin, "commit", "-q", "-m", "v1")

    slot = proj / "work" / "A_1"
    slot.mkdir(parents=True, exist_ok=True)
    _git(slot, "init", "-q", "-b", "main")
    (slot / "README.md").write_text("super\n", encoding="utf-8")
    _git(slot, "add", "README.md"); _git(slot, "commit", "-q", "-m", "super init")
    _git(slot, "-c", "protocol.file.allow=always", "submodule", "add",
         str(sub_origin), "vendor/sub")
    _git(slot, "commit", "-q", "-m", "add submodule")

    # b1·b2 생성(각각 on-branch 로 전이) → 현재 b2.
    assert wp.dev("work/A_1", "vendor/sub", "b1")[0] == 0
    assert wp.dev("work/A_1", "vendor/sub", "b2")[0] == 0
    assert _git(slot / "vendor" / "sub", "symbolic-ref", "--short", "HEAD").stdout.strip() == "b2"

    # b1 은 이미 존재 → `-b` 실패·show-ref 존재확인 후 plain checkout 전환.
    rc, _out = wp.dev("work/A_1", "vendor/sub", "b1")
    assert rc == 0, "기존 브랜치 전환 폴백이 실패(rc≠0)"
    head = _git(slot / "vendor" / "sub", "symbolic-ref", "--short", "HEAD").stdout.strip()
    assert head == "b1", f"기존 브랜치로 전환 안 됨: {head!r}"


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
def test_real_git_list_git_worktrees_and_reconcile(proj, tmp_path):
    """실 git 백스톱 — list_git_worktrees porcelain 파싱 + reconcile orphan/stale (T-0295).

    porcelain 파싱은 mock 이 포맷을 틀리게 인코딩할 수 있는 파서라 실 git 출력으로 백스톱한다.
    실 bare + 실 `worktree add` 로: (1) list_git_worktrees 가 슬롯을 branch 와 함께 열거(bare 원은
    slot=None), (2) 장부만 비우면 그 worktree 를 orphan 으로, (3) worktree 를 실제 remove 하고
    장부만 남기면 stale 로 reconcile 이 잡는지 검증한다.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)
    lease = wp.create_slot("A", branch="a1", session="me", init_submodules=False)
    assert lease.slot == "work/A_1"

    # (1) list_git_worktrees — 슬롯 worktree 를 branch 와 함께 열거(bare 원은 slot=None).
    wts = wp.list_git_worktrees("A")
    by_slot = {w.slot: w for w in wts if w.slot}
    assert "work/A_1" in by_slot, f"실 worktree 가 열거 안 됨: {[w.path for w in wts]!r}"
    assert by_slot["work/A_1"].branch == "a1"
    assert by_slot["work/A_1"].bare is False
    assert any(w.bare and w.slot is None for w in wts), "bare 원 엔트리(slot=None) 누락"

    # (2) 장부만 비우면(worktree 는 disk 에 남음) → orphan.
    _seed(wp)   # 빈 장부
    recon = wp.reconcile_worktrees()
    assert {w.slot for w in recon.orphans} == {"work/A_1"}, "disk worktree·장부 없음이 orphan 미판정"
    assert recon.stale == []

    # (3) worktree 를 실제 remove(disk+등록 정리) 후 장부만 남김 → stale.
    wp._rollback_worktree("A", wp.slot_path("work/A_1"))   # git worktree remove --force
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    recon2 = wp.reconcile_worktrees()
    assert {l.slot for l in recon2.stale} == {"work/A_1"}, "장부만 남고 worktree 없음이 stale 미판정"
    assert recon2.orphans == []


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
def test_real_git_create_slot_base_derives_from_origin_when_advanced(proj, tmp_path):
    """실 git — origin 이 clone 이후 앞서면 create_slot(base=) 이 *origin 최신*에서 슬롯을 판다 (T-0274).

    T-0152 refspec 은 origin/* 만 갱신하고 로컬 `refs/heads/<base>` 는 clone 시점 동결이라,
    로컬 base 에서 파면 슬롯이 stale 하게 시작한다. 이 테스트는 clone 후 origin develop 을
    앞서게(origin-only 커밋 D2) 만들고, create_slot 이 fetch 로 origin/develop 을 D2 로 갱신한 뒤
    거기서 슬롯을 파 — 슬롯 HEAD == origin develop tip 이고 origin-only 파일이 보이는지 고정한다
    (동결 로컬 develop=D1 에서 팠다면 D2 커밋/파일이 안 보임 → 회귀 포착).
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)

    # family origin: main + develop(초기 커밋 D1).
    origin = _init_repo(tmp_path / "A-origin")
    _git(origin, "checkout", "-q", "-b", "develop")
    (origin / "DEV1.txt").write_text("d1\n", encoding="utf-8")
    _git(origin, "add", "DEV1.txt")
    _git(origin, "commit", "-q", "-m", "develop D1")
    _git(origin, "checkout", "-q", "main")   # origin HEAD = main

    # bare = clone --bare + refspec 보정(T-0152 동형) + fetch → origin/develop = D1(로컬도 D1 동결).
    bare = wp.bare_repo_path("A")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")

    # origin develop 앞서기 — D2(origin-only 커밋). bare 로컬 refs/heads/develop 는 D1 동결 유지.
    _git(origin, "checkout", "-q", "develop")
    (origin / "DEV2.txt").write_text("d2\n", encoding="utf-8")
    _git(origin, "add", "DEV2.txt")
    _git(origin, "commit", "-q", "-m", "develop D2 (origin-only)")
    develop_tip = _git(origin, "rev-parse", "develop").stdout.strip()
    # 전제 확인 — 갱신 전 bare 로컬 refs/heads/develop 는 아직 D2 가 아니다(동결).
    local_frozen = _git(bare, "rev-parse", "refs/heads/develop").stdout.strip()
    assert local_frozen != develop_tip, "전제 위반 — 로컬 develop 이 이미 origin tip"

    # create_slot(base=develop) — fetch 로 origin/develop→D2 갱신 후 거기서 슬롯 파생.
    lease = wp.create_slot("A", base="develop", session="me", init_submodules=False)
    assert lease.slot == "work/A_1"
    slot_dir = wp.slot_path("work/A_1")

    # 슬롯 HEAD == origin develop tip(D2) — 동결 로컬(D1) 이 아니다.
    slot_head = _git(slot_dir, "rev-parse", "HEAD").stdout.strip()
    assert slot_head == develop_tip, \
        f"슬롯이 origin 최신(D2)에서 안 시작: HEAD={slot_head!r} origin_tip={develop_tip!r} frozen={local_frozen!r}"
    # origin-only 커밋의 파일이 슬롯에 보인다(동결 로컬 D1엔 없음).
    assert (slot_dir / "DEV2.txt").exists(), \
        "origin-only 파일 DEV2 가 슬롯에 없음 — 동결 로컬 develop(D1)에서 팠다(T-0274 회귀)"


@_git_required
def test_real_git_create_slot_base_no_upstream_on_slot_branch(proj, tmp_path):
    """실 git — origin/<base> 파생 슬롯 브랜치에 upstream 이 자동 설정되지 않는다 (T-0274 결정·codex 백스톱).

    `worktree add -b <slot> <path> origin/<base>` 는 remote-tracking ref 에서 브랜치를 파므로
    git 기본 `branch.autoSetupMerge=true` 가 슬롯 브랜치에 upstream 을 *자동* 설정한다(슬롯=작업
    스트림 결정 위반). 엔진의 `--no-track` 이 그걸 억제하는지 — mock argv 단언(명시 config 부재)이
    못 잡는 git 의 자동설정을 실 git 로 백스톱한다: 슬롯 브랜치 `<slot>@{upstream}` 미해소(rc≠0) +
    `branch.<slot>.remote` config 미설정.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)

    # family origin: main + develop, bare = clone --bare + refspec 보정 + fetch(→ origin/develop 존재).
    origin = _init_repo(tmp_path / "A-origin")
    _git(origin, "checkout", "-q", "-b", "develop")
    (origin / "DEV.txt").write_text("d\n", encoding="utf-8")
    _git(origin, "add", "DEV.txt")
    _git(origin, "commit", "-q", "-m", "develop")
    _git(origin, "checkout", "-q", "main")
    bare = wp.bare_repo_path("A")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")
    # autoSetupMerge 기본 활성(git 기본값)임을 명시 — --no-track 이 없으면 upstream 이 걸릴 조건.
    _git(bare, "config", "branch.autoSetupMerge", "true")

    lease = wp.create_slot("A", base="develop", session="me", init_submodules=False)
    slot_dir = wp.slot_path("work/A_1")
    assert lease.slot == "work/A_1"
    # 파생은 origin/develop 에서 됐다(파일 존재).
    assert (slot_dir / "DEV.txt").exists()

    # 슬롯 브랜치 `A_1@{upstream}` 이 미해소 — upstream 자동설정 안 됨(--no-track 효과).
    up = subprocess.run(
        [_GIT, "-C", str(slot_dir), "rev-parse", "--abbrev-ref", "A_1@{upstream}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert up.returncode != 0, \
        f"슬롯 브랜치 A_1 에 upstream 이 자동 설정됨(슬롯=작업스트림 위반): {up.stdout.strip()!r}"
    # branch.A_1.remote config 도 미설정(자동 tracking 없음).
    remote = subprocess.run(
        [_GIT, "-C", str(bare), "config", "--get", "branch.A_1.remote"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert remote.returncode != 0, \
        f"branch.A_1.remote 가 설정됨(슬롯 tracking 위반): {remote.stdout.strip()!r}"


@_git_required
def test_real_git_create_slot_base_fetch_offline_falls_back_to_local(proj, tmp_path):
    """실 git — origin remote 소실(오프라인 시뮬)이면 fetch 실패 후 로컬 `<base>` 폴백·슬롯 생성 성공 (T-0274·fail-soft).

    origin repo 를 삭제해 `git fetch origin` 이 실패하게 만들고, create_slot 이 경고 후
    로컬 develop(동결 head)에서 슬롯을 파 — 슬롯이 실제로 생성되고 로컬 develop 내용을 갖는지
    고정한다(네트워크 실패가 슬롯 생성을 막지 않는다).
    """
    import shutil as _sh
    _init_repo(proj)
    wp = _load_wp_bound(proj)

    origin = _init_repo(tmp_path / "A-origin")
    _git(origin, "checkout", "-q", "-b", "develop")
    (origin / "DEV_LOCAL.txt").write_text("local\n", encoding="utf-8")
    _git(origin, "add", "DEV_LOCAL.txt")
    _git(origin, "commit", "-q", "-m", "develop local")
    _git(origin, "checkout", "-q", "main")
    bare = wp.bare_repo_path("A")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))

    # origin 삭제 → `git fetch origin` 이 실패한다(오프라인/소실 시뮬).
    _sh.rmtree(origin)

    lease = wp.create_slot("A", base="develop", session="me", init_submodules=False)
    assert lease.slot == "work/A_1"           # fetch 실패해도 슬롯 생성 계속(fail-soft)
    slot_dir = wp.slot_path("work/A_1")
    assert slot_dir.is_dir()
    # 로컬 develop(동결 head)에서 팜 — develop 내용이 보인다.
    assert (slot_dir / "DEV_LOCAL.txt").exists()
    head = _git(slot_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "A_1"                       # 슬롯 브랜치 식별자 유지


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


def _allow_file_submodules(monkeypatch):
    """모든 git 호출에 `protocol.file.allow=always` 주입 — file:// submodule clone 차단 해제.

    git 은 보안상(CVE-2022-39253) file 프로토콜 submodule clone 을 기본 차단한다. 실 git
    submodule 테스트 픽스처는 로컬 file:// 경로를 쓰므로 GIT_CONFIG_* 환경으로 그 차단을
    푼다(테스트-전용·엔진 코드는 `-c` 를 안 박아 실전 동작에 영향 0·기존 submodule 테스트 정합).
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def _mk_family_versioned_submodule(wp, tmp_path):
    """family repo(main→sub v1 pin·feature→sub v2 pin) + bare `.repos/A.git` 를 만든다.

    ADR-0051 selective 재동기 실 git 테스트 공용 픽스처 — submodule origin 에 v1→v2 두 커밋을
    두고 superproject `main` 은 v1, `feature` 는 v2 를 pin 한다. 호출부가 슬롯을 main 에서
    만들고 feature 로 전환하면 detached submodule 은 v2 로 재동기(consume)·on-branch 는
    skip(dev)이어야 한다. 반환: sub_origin 경로(submodule URL — 테스트 존속 필요).
    호출 전제: 이미 `_init_repo(proj)` + `wp = _load_wp_bound(proj)` 수행됨.
    """
    # submodule origin: v1(→ main pin), 이후 v2(→ feature pin).
    sub_origin = _init_repo(tmp_path / "sub-origin")
    (sub_origin / "lib.txt").write_text("v1\n", encoding="utf-8")
    _git(sub_origin, "add", "lib.txt")
    _git(sub_origin, "commit", "-q", "-m", "lib v1")

    # family main: submodule add(현재 sub HEAD=v1 pin) + 커밋.
    family = _init_repo(tmp_path / "A-origin")
    _git(family, "submodule", "add", str(sub_origin), "vendor/sub")
    _git(family, "commit", "-q", "-m", "main pins sub v1")

    # sub-origin 에 v2 추가 → family feature 가 v2 를 pin.
    (sub_origin / "lib.txt").write_text("v2\n", encoding="utf-8")
    _git(sub_origin, "add", "lib.txt")
    _git(sub_origin, "commit", "-q", "-m", "lib v2")
    _git(family, "checkout", "-q", "-b", "feature")
    _git(family / "vendor" / "sub", "fetch", "-q", "origin")
    _git(family / "vendor" / "sub", "checkout", "-q", "origin/main")  # sub 최신(v2)
    _git(family, "add", "vendor/sub")
    _git(family, "commit", "-q", "-m", "feature pins sub v2")
    _git(family, "checkout", "-q", "main")

    bare = wp.bare_repo_path("A")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(family), str(bare))
    return sub_origin


@_git_required
def test_real_git_resync_detached_submodule_follows_new_branch_pin(proj, tmp_path, monkeypatch):
    """실 git — 브랜치 전환 시 detached(consume) submodule 이 새 브랜치 pin 으로 재동기된다 (ADR-0051).

    슬롯을 main(sub v1 pin)에서 만들면 submodule 은 v1·detached 다. feature(sub v2 pin)로
    `_checkout_required` 전환하면 detached=consume 이라 selective 재동기가 submodule 을 v2 로
    올린다. 재동기 없이 plain checkout 만이면 submodule 워킹트리는 v1 그대로(drift)라 red.
    """
    _allow_file_submodules(monkeypatch)
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_family_versioned_submodule(wp, tmp_path)

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=True)
    slot_dir = wp.slot_path(lease.slot)
    sub_lib = slot_dir / "vendor" / "sub" / "lib.txt"
    assert sub_lib.read_text(encoding="utf-8") == "v1\n", "슬롯 초기 submodule 이 main pin(v1) 아님"

    # feature 로 전환(브랜치 전환 경로) → detached submodule 이 feature pin(v2)로 재동기.
    wp._checkout_required("work/A_1", "feature")
    assert sub_lib.read_text(encoding="utf-8") == "v2\n", \
        "detached submodule 이 새 브랜치 pin(v2)로 재동기 안 됨(drift·ADR-0051)"


@_git_required
def test_real_git_resync_on_branch_submodule_preserved(proj, tmp_path, monkeypatch):
    """실 git — 브랜치 전환 시 on-branch(dev) submodule 은 skip·작업이 보존된다 (ADR-0051·크럭스 A).

    슬롯 submodule 을 브랜치(devwork)로 전환해 dev 역할로 만든 뒤 superproject 를 feature(sub
    v2 pin)로 바꾼다. on-branch=dev 라 selective 재동기가 skip 하므로 submodule 은 v1·devwork
    그대로 보존된다(전역 submodule.recurse 였다면 detached pin 으로 낚아채 파괴됐을 것).
    """
    _allow_file_submodules(monkeypatch)
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_family_versioned_submodule(wp, tmp_path)

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=True)
    slot_dir = wp.slot_path(lease.slot)
    sub_dir = slot_dir / "vendor" / "sub"
    sub_lib = sub_dir / "lib.txt"
    assert sub_lib.read_text(encoding="utf-8") == "v1\n"

    # submodule 을 브랜치로 전환(dev 역할·on-branch) — 여전히 v1.
    _git(sub_dir, "checkout", "-q", "-b", "devwork")

    # feature 로 전환 → on-branch(dev) submodule 은 skip → v1·devwork 유지.
    wp._checkout_required("work/A_1", "feature")
    assert sub_lib.read_text(encoding="utf-8") == "v1\n", \
        "on-branch(dev) submodule 이 재동기돼 작업이 파괴됨(크럭스 A·ADR-0051 위반)"
    head = _git(sub_dir, "symbolic-ref", "--short", "HEAD").stdout.strip()
    assert head == "devwork", "dev 브랜치가 detached 로 낚아채짐(재동기 skip 안 됨)"


@_git_required
def test_real_git_resync_on_branch_submodule_preserved_under_recurse_true(proj, tmp_path, monkeypatch):
    """실 git — `submodule.recurse=true` ambient config 여도 on-branch(dev) submodule 보존 (ADR-0051 크럭스 A·codex 게이트).

    사용자 전역 `~/.gitconfig`(또는 repo config)에 `submodule.recurse=true` 가 있으면 plain
    `git checkout` 이 selective resync *전에* submodule 을 재귀 갱신해 dev 브랜치를 detached pin
    으로 낚아챈다. `_checkout` 이 양 checkout 에 `--no-recurse-submodules` 를 박아 이 ambient
    recurse 를 override → selective resync 가 submodule 상태의 유일 권위가 된다. dev submodule 의
    devwork 브랜치·v1 작업이 보존돼야 한다.

    비공허: `--no-recurse-submodules` 를 빼면 recurse=true 가 dev submodule 을 detached(v2)로
    낚아채 이 단언이 red(크럭스 A 회귀 재현) — mutation 으로 확인.
    """
    _allow_file_submodules(monkeypatch)
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_family_versioned_submodule(wp, tmp_path)

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=True)
    slot_dir = wp.slot_path(lease.slot)
    sub_dir = slot_dir / "vendor" / "sub"
    sub_lib = sub_dir / "lib.txt"
    assert sub_lib.read_text(encoding="utf-8") == "v1\n"

    # ambient `submodule.recurse=true` — 사용자 전역/repo config 재현(슬롯 worktree config 에 설정).
    _git(slot_dir, "config", "submodule.recurse", "true")

    # submodule 을 브랜치로 전환(dev 역할·on-branch) — 여전히 v1.
    _git(sub_dir, "checkout", "-q", "-b", "devwork")

    # feature 로 전환 → recurse=true 여도 `--no-recurse-submodules` 로 checkout 이 submodule 무건드림,
    # selective resync 는 on-branch=dev 를 skip → v1·devwork 보존(recurse=true 낚아챔 차단).
    wp._checkout_required("work/A_1", "feature")
    assert sub_lib.read_text(encoding="utf-8") == "v1\n", \
        "recurse=true 가 dev submodule 을 detached pin 으로 낚아챔(--no-recurse-submodules 누락·크럭스 A 회귀)"
    head = _git(sub_dir, "symbolic-ref", "--short", "HEAD").stdout.strip()
    assert head == "devwork", "dev 브랜치가 detached 로 낚아채짐(ambient recurse override 실패)"


# ════════════════════════════════════════════════════════════════════════
# T-0070 — submodule 인터랙티브 러너 + 원자적 롤백 + 런너 stderr surface
# 실 Windows multi-PM 파일럿 블로커 3종(submodule clone 600s 타임아웃·댕글링 worktree·
# stderr 유실로 빈 에러). 단위테스트는 git_runner 주입(mock)이라 실 인터랙티브 안 탐.
# ════════════════════════════════════════════════════════════════════════


# ── (1) submodule init = 인터랙티브 러너 (seam·실행 없이) ─────────────────────


def test_create_slot_worktree_add_and_submodule_use_interactive_on_real_path(wp, monkeypatch):
    """git_runner 미주입 실경로면 worktree add(T-0292)·submodule(T-0070) 둘 다 console-visible 러너.

    seam 으로 검증 — `_real_git_runner_interactive`/`_real_git_runner` 팩토리를 (cwd, timeout)
    캡처 spy 로 교체해 *어느 러너가 어느 cwd·timeout 으로 불렸는지* 본다. 실 인터랙티브
    subprocess(stdin 블록)는 절대 실행하지 않는다 — spy 가 FakeGit 을 돌려줘 rc0 으로 흐른다.
    """
    _mk_bare_placeholder(wp, "A")
    interactive_calls: list = []   # (cwd, timeout)
    capture_cwds: list = []

    def spy_interactive(cwd, *, timeout=None):
        interactive_calls.append((cwd, timeout))
        return FakeGit()  # 모든 git 호출 성공 stub(실 인터랙티브 subprocess 안 탐)

    def spy_capture(cwd):
        capture_cwds.append(cwd)
        return FakeGit()

    monkeypatch.setattr(wp, "_real_git_runner_interactive", spy_interactive)
    monkeypatch.setattr(wp, "_real_git_runner", spy_capture)

    wp.create_slot("A", branch="a1", session="me")  # git_runner 미주입 = 실경로

    bare = wp.bare_repo_path("A")
    slot_p = wp.slot_path("work/A_1")
    # worktree add(자체) = bare 컨텍스트 + GIT_TIMEOUT_SECONDS (T-0292 console-visible).
    assert (bare, wp.GIT_TIMEOUT_SECONDS) in interactive_calls, \
        "worktree add 가 console-visible 러너(GIT_TIMEOUT_SECONDS)를 안 탐"
    # submodule = 슬롯 경로 + 기본 timeout(=SUBMODULE_TIMEOUT·미지정) (T-0070 인터랙티브).
    assert (slot_p, None) in interactive_calls, \
        "submodule 단계가 인터랙티브 러너(기본 timeout)를 안 탐"
    # 이 pin 의 **의도 = interactive vs capture 러너 구분**(worktree add/submodule=console-visible
    # 인터랙티브·짧은 read-only op=capture) — 그 의도를 보존하며 프로파일만 갱신한다. branch 경로엔
    # base 파생 prep(fetch/show-ref)이 없고, capture 러너는 bare 컨텍스트에서 (a) bare 실검증 가드
    # (T-0294·rev-parse) + (b) 슬롯번호용 `git worktree list --porcelain`(T-0295·orphan 병합) +
    # (c) branch-존재 검사 `git branch --list <branch>`(T-0343·기존 브랜치면 checkout·부재면 -B)만
    # 만든다 — 셋 다 bare 컨텍스트·짧은 read-only op(인터랙티브 아님). worktree add/submodule 은
    # 인터랙티브다(위 두 단언).
    assert capture_cwds == [bare, bare, bare], \
        f"capture 러너는 bare 실검증(T-0294)+worktree list(T-0295)+branch --list(T-0343)만이어야 하는데: {capture_cwds!r}"


def test_create_slot_worktree_add_injected_runner_preserves_di_seam(wp, monkeypatch):
    """git_runner 주입 시 worktree add 도 그 주입 runner — 인터랙티브 러너 안 탐(DI seam·T-0292).

    인터랙티브 팩토리를 호출하면 즉시 실패하는 trap 으로 바꿔 — 주입 모드에서 worktree add 가
    그걸 안 타는지(현행 테스트 무영향·seam 보존) 결정적으로 입증한다.
    """
    _mk_bare_placeholder(wp, "A")

    def trap(cwd, *, timeout=None):  # 주입 모드에서 인터랙티브가 불리면 안 됨.
        raise AssertionError("git_runner 주입인데 worktree add 가 인터랙티브 러너를 탐(DI seam 깨짐)")

    monkeypatch.setattr(wp, "_real_git_runner_interactive", trap)
    git = FakeGit()
    lease = wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert lease.state == "leased"
    # 주입 runner 가 worktree add·submodule 둘 다 처리(인터랙티브 우회).
    assert git.did("worktree", "add", "-B", "a1")
    assert git.did("submodule", "update", "--init", "--recursive", "--force")


def test_create_slot_worktree_add_failure_trip_message(wp):
    """worktree add rc≠0 → 트립 안내(PM_GIT_TIMEOUT·터미널 직접 실행·`none` 무제한) surface (T-0292).

    인터랙티브 실패(rc≠0·빈 out)를 주입 runner 로 재현 — 하네스 자동 호출이 죽었을 때
    사용자에게 다음 행동을 주는 메시지 shape 을 본다(rc 기반·out 은 인터랙티브면 빈 문자열).
    """
    _mk_bare_placeholder(wp, "A")

    def failing(argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과 → worktree add 도달(T-0294).
        if argv[:2] == ["worktree", "add"]:
            return (1, "")  # 인터랙티브 실패 재현: rc≠0·빈 out
        return (0, "")

    with pytest.raises(RuntimeError) as exc:
        wp.create_slot("A", branch="a1", session="me", git_runner=failing)
    msg = str(exc.value)
    assert "rc=1" in msg                       # rc 기반 진단(빈 out 에도)
    assert "PM_GIT_TIMEOUT" in msg             # env 노브 안내
    assert "pm-config worktree add A" in msg   # repo 이름 실린 직접-실행 안내
    assert "PM_GIT_TIMEOUT=none" in msg        # 무제한 opt-in 안내
    # 실패 시 장부 미등록(기존 동작 유지).
    assert wp.list_leases() == []


# ── T-0292: _resolve_git_timeout env override (PM_GIT_TIMEOUT·_resolve_submodule_timeout 미러) ──


def test_resolve_git_timeout_default_when_unset(wp, monkeypatch):
    """env 미설정 → 기본 1800(30분·유한 관대)."""
    monkeypatch.delenv("PM_GIT_TIMEOUT", raising=False)
    assert wp._resolve_git_timeout() == 1800


def test_resolve_git_timeout_positive_int(wp, monkeypatch):
    """양의 정수 → 그 초."""
    monkeypatch.setenv("PM_GIT_TIMEOUT", "600")
    assert wp._resolve_git_timeout() == 600


def test_resolve_git_timeout_strips_whitespace_and_case(wp, monkeypatch):
    """앞뒤 공백 strip + 대소문자 무시 후 파싱."""
    monkeypatch.setenv("PM_GIT_TIMEOUT", "  900  ")
    assert wp._resolve_git_timeout() == 900
    monkeypatch.setenv("PM_GIT_TIMEOUT", "  NONE  ")
    assert wp._resolve_git_timeout() is None


def test_resolve_git_timeout_unlimited_sentinels(wp, monkeypatch):
    """`0`/`none`/`unlimited`/빈값 → None(무제한·env opt-in·console-visible 이라 안전)."""
    for raw in ("0", "none", "unlimited", ""):
        monkeypatch.setenv("PM_GIT_TIMEOUT", raw)
        assert wp._resolve_git_timeout() is None, f"{raw!r} → None 이어야"


def test_resolve_git_timeout_non_numeric_falls_back_to_default(wp, monkeypatch):
    """비정상(비숫자) env → 기본 1800 폴백(무해)."""
    monkeypatch.setenv("PM_GIT_TIMEOUT", "soon")
    assert wp._resolve_git_timeout() == 1800


def test_git_timeout_seconds_reflects_env_at_module_load(proj, monkeypatch):
    """모듈 로드 시 `GIT_TIMEOUT_SECONDS = _resolve_git_timeout()` — env 를 로드시점에 반영(SUBMODULE 동형)."""
    monkeypatch.setenv("PM_GIT_TIMEOUT", "300")
    mod = _load_wp_bound(proj)
    assert mod.GIT_TIMEOUT_SECONDS == 300
    monkeypatch.setenv("PM_GIT_TIMEOUT", "none")
    mod2 = _load_wp_bound(proj)
    assert mod2.GIT_TIMEOUT_SECONDS is None


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
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과(T-0294) → worktree add·submodule 도달.
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
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과(T-0294).
        if argv[:1] == ["submodule"]:
            return (1, "submodule failed")
        if argv[:2] == ["worktree", "remove"]:
            return (1, "remove failed")  # 롤백 실패(rc≠0)
        return (0, "")
    with pytest.raises(RuntimeError, match="submodule init failed"):
        wp.create_slot("A", branch="a1", session="me", git_runner=remove_rc_fail)
    assert wp.list_leases() == []

    def remove_raises(argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과(T-0294).
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
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")   # 유효 bare 로 가드 통과(T-0294) → worktree add 실패 경로 도달.
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


# ════════════════════════════════════════════════════════════════════════
# T-0295: list_git_worktrees · reconcile · 슬롯번호 git 병합 · provisional lease
# ════════════════════════════════════════════════════════════════════════


def _porcelain(*entries) -> str:
    """`git worktree list --porcelain` 텍스트 픽스처.

    각 entry = (path, branch_or_None, *flags). flags 에 "bare" 있으면 bare 엔트리, branch=None
    이면 detached(HEAD 만), 아니면 `branch refs/heads/<branch>`. 실 git 포맷(빈 줄 구분)을 모사.
    """
    blocks = []
    for path, branch, *flags in entries:
        lines = [f"worktree {path}"]
        if "bare" in flags:
            lines.append("bare")
        elif branch is None:
            lines.append("HEAD " + "0" * 40)
            lines.append("detached")
        else:
            lines.append("HEAD " + "1" * 40)
            lines.append(f"branch refs/heads/{branch}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


class _WorktreeListGit:
    """`worktree list --porcelain` 에 미리 정한 porcelain 을 돌려주는 mock runner (T-0295)."""

    def __init__(self, porcelain: str, *, list_rc: int = 0):
        self.porcelain = porcelain
        self.list_rc = list_rc
        self.calls: list[list] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["worktree", "list", "--porcelain"]:
            return (self.list_rc, self.porcelain)
        return (0, "")


def test_list_git_worktrees_parses_porcelain(wp):
    """list_git_worktrees — porcelain 파싱: branch·detached·bare + WORK_DIR 하위 slot 파생 (T-0295)."""
    _mk_bare_placeholder(wp, "A")
    porc = _porcelain(
        (str(wp.bare_repo_path("A")), None, "bare"),        # bare 원 → slot None
        (str(wp.slot_path("work/A_1")), "A_1"),             # on-branch
        (str(wp.slot_path("work/A_2")), None),              # detached
    )
    git = _WorktreeListGit(porc)
    wts = wp.list_git_worktrees("A", git_runner=git)
    by_slot = {w.slot: w for w in wts}
    assert by_slot["work/A_1"].branch == "A_1"
    assert by_slot["work/A_1"].bare is False and by_slot["work/A_1"].detached is False
    assert by_slot["work/A_2"].detached is True and by_slot["work/A_2"].branch is None
    bare_entry = next(w for w in wts if w.bare)
    assert bare_entry.slot is None, "bare 원(WORK_DIR 밖)은 slot=None 이어야 함"


def test_list_git_worktrees_failsoft_on_rc_and_raise(wp):
    """list_git_worktrees fail-soft — rc≠0·runner 예외는 빈 리스트(크래시 0·T-0295)."""
    _mk_bare_placeholder(wp, "A")
    assert wp.list_git_worktrees("A", git_runner=_WorktreeListGit("", list_rc=1)) == []

    def boom(argv):
        raise OSError("git gone")
    assert wp.list_git_worktrees("A", git_runner=boom) == []


def test_reconcile_flags_orphan(wp):
    """DoD(1) — git worktree 있고 장부 없음 → orphan (T-0295·audit #2/#3)."""
    _mk_bare_placeholder(wp, "A")
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="leased"))
    porc = _porcelain(
        (str(wp.bare_repo_path("A")), None, "bare"),
        (str(wp.slot_path("work/A_1")), "A_1"),
        (str(wp.slot_path("work/A_2")), "A_2"),   # orphan(장부 미등록)
    )
    recon = wp.reconcile_worktrees(git_runner=_WorktreeListGit(porc))
    assert {w.slot for w in recon.orphans} == {"work/A_2"}
    assert recon.stale == [] and recon.incomplete == []


def test_reconcile_flags_stale(wp):
    """DoD(2) — 장부 있고 git worktree 없음 → stale (T-0295·audit #3)."""
    _mk_bare_placeholder(wp, "A")
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", state="idle"))
    porc = _porcelain(
        (str(wp.bare_repo_path("A")), None, "bare"),
        (str(wp.slot_path("work/A_1")), "A_1"),   # A_2 는 dir 삭제/prune → 목록에 없음
    )
    recon = wp.reconcile_worktrees(git_runner=_WorktreeListGit(porc))
    assert {l.slot for l in recon.stale} == {"work/A_2"}
    assert recon.orphans == [] and recon.incomplete == []


def test_reconcile_flags_incomplete_creating(wp):
    """provisional('creating') 리스는 incomplete 로 잡히고 stale 로 이중계상 안 함 (T-0295)."""
    _mk_bare_placeholder(wp, "A")
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="creating"))
    porc = _porcelain(
        (str(wp.bare_repo_path("A")), None, "bare"),
        (str(wp.slot_path("work/A_1")), "A_1"),   # add 성공 후 확정 전 SIGKILL 재현(worktree 존재)
    )
    recon = wp.reconcile_worktrees(git_runner=_WorktreeListGit(porc))
    assert {l.slot for l in recon.incomplete} == {"work/A_1"}
    assert recon.stale == []       # creating 은 stale 로 이중계상 안 함
    assert recon.orphans == []     # 장부에 있으니 orphan 도 아님


def test_reconcile_clean_when_ledger_matches_git(wp):
    """sensitivity 대조 — 장부와 git 이 일치하면 drift 0(orphan/stale/incomplete 전부 빈 리스트)."""
    _mk_bare_placeholder(wp, "A")
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="leased"))
    porc = _porcelain(
        (str(wp.bare_repo_path("A")), None, "bare"),
        (str(wp.slot_path("work/A_1")), "A_1"),
    )
    recon = wp.reconcile_worktrees(git_runner=_WorktreeListGit(porc))
    assert recon.orphans == [] and recon.stale == [] and recon.incomplete == []


def test_create_slot_avoids_orphan_slot_number(wp):
    """DoD(3) — 슬롯번호가 git worktree(orphan) 번호를 병합 회피한다 (T-0295·audit #4).

    장부는 비었지만 git 엔 orphan work/A_1 이 등록돼 있다 — ledger 만 보면 A_1 재사용 →
    `git worktree add` "already exists" 충돌. git 병합으로 A_2 를 판다.
    """
    _mk_bare_placeholder(wp, "A")
    orphan_porc = _porcelain(
        (str(wp.bare_repo_path("A")), None, "bare"),
        (str(wp.slot_path("work/A_1")), "A_1"),   # orphan(장부엔 없음)
    )

    def git(argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")
        if "rev-parse" in argv and "--verify" in argv and argv and argv[-1] == "HEAD":
            return (0, "abc\n")
        if argv[:3] == ["worktree", "list", "--porcelain"]:
            return (0, orphan_porc)
        return (0, "")   # worktree add·submodule 성공
    lease = wp.create_slot("A", session="me", git_runner=git)
    assert lease.slot == "work/A_2", "orphan A_1 을 회피해 A_2 를 파야 함(git 병합·audit #4)"


def test_create_slot_writes_provisional_before_worktree_add(wp):
    """DoD(4)·SIGKILL 커버 — provisional('creating')이 worktree add *전에* disk 에 기록된다 (T-0295).

    add 시점에 장부를 (create_slot 이 이미 락 보유·같은 스레드라 무락) 읽어 provisional 유무를
    캡처한다 — 이게 SIGKILL(except/finally 안 도는 kill -9)에도 흔적이 남아 reconcile 이 청소할 수
    있는 근거다. 성공 시 leased 로 확정(2차 write).
    """
    _mk_bare_placeholder(wp, "A")
    seen = {}

    def git(argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")
        if "rev-parse" in argv and "--verify" in argv and argv and argv[-1] == "HEAD":
            return (0, "abc\n")
        if argv[:2] == ["worktree", "add"]:
            seen["at_add"] = [(l.slot, l.state) for l in wp._read_ledger()]
            return (0, "")
        return (0, "")
    lease = wp.create_slot("A", session="me", git_runner=git)
    assert seen["at_add"] == [("work/A_1", "creating")], \
        "provisional('creating')이 worktree add 전에 disk 장부에 없음(SIGKILL 흔적 상실)"
    assert lease.state == "leased"
    assert [(l.slot, l.state) for l in wp.list_leases()] == [("work/A_1", "leased")]


def test_create_slot_keyboard_interrupt_rolls_back_and_removes_provisional(wp):
    """KeyboardInterrupt(add 성공 후 submodule 중) → worktree 롤백 + provisional 제거 + 재raise (T-0295).

    except 가 **BaseException**(KeyboardInterrupt 포함)을 잡아 청소함을 입증 — `except Exception`
    만이면 Ctrl-C 가 orphan worktree + provisional 을 남긴다(finally-rollback 보조 경로).
    """
    _mk_bare_placeholder(wp, "A")
    removed = []

    def git(argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")
        if "rev-parse" in argv and "--verify" in argv and argv and argv[-1] == "HEAD":
            return (0, "abc\n")
        if argv[:1] == ["submodule"]:
            raise KeyboardInterrupt()               # add 성공 후 submodule 중 Ctrl-C
        if argv[:2] == ["worktree", "remove"]:
            removed.append(list(argv))
            return (0, "")
        return (0, "")                              # worktree add·list 성공
    with pytest.raises(KeyboardInterrupt):
        wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert removed, "KeyboardInterrupt 인데 worktree 롤백(remove)이 안 불림(orphan 잔존)"
    assert wp.list_leases() == [], "provisional('creating')이 제거 안 됨(Ctrl-C 후 장부 잔존)"


def test_create_slot_already_exists_error_hints_orphan(wp):
    """DoD(4) — worktree add "already exists" 실패에 orphan 진단 힌트가 실린다 (T-0295·#4-충돌)."""
    _mk_bare_placeholder(wp, "A")

    def git(argv):
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")
        if "rev-parse" in argv and "--verify" in argv and argv and argv[-1] == "HEAD":
            return (0, "abc\n")
        if argv[:2] == ["worktree", "add"]:
            return (1, "fatal: '.../work/A_1' already exists")
        return (0, "")
    with pytest.raises(RuntimeError) as exc:
        wp.create_slot("A", branch="a1", session="me", git_runner=git)
    msg = str(exc.value)
    assert "orphan" in msg
    assert "pm-config status" in msg
    assert wp.list_leases() == []   # provisional 제거(불완전 슬롯 미등록)


# ── T-0295 리뷰: 안전 prune · alloc 위험차단 · resume creating 제외 · 이중계상 가드 ──


def test_reconcile_creating_without_worktree_is_incomplete_not_stale(wp):
    """should-fix(3) — creating lease 인데 worktree 부재(add 전 SIGKILL): incomplete 1·stale 0 (T-0295).

    `l.state != "creating"` 이중계상 가드의 load-bearing 시나리오 — worktree 없는 creating 은
    git_slots 에 없어(slot not in git_slots) 가드 없으면 stale 로도 잡혀 이중 surface 된다. 이
    케이스가 이전엔 무테스트였다(reviewer).
    """
    _mk_bare_placeholder(wp, "A")
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="creating"))
    # git worktree 목록에 A_1 없음(add 전 SIGKILL = worktree 미생성).
    porc = _porcelain((str(wp.bare_repo_path("A")), None, "bare"))
    recon = wp.reconcile_worktrees(git_runner=_WorktreeListGit(porc))
    assert {l.slot for l in recon.incomplete} == {"work/A_1"}
    assert recon.stale == []       # 가드가 stale 이중계상 차단(load-bearing)
    assert recon.orphans == []


def test_prune_stale_leases_removes_absent_worktree_entries(wp):
    """must-fix(1) cleanup — worktree dir 확정 부재(stale idle + worktree 없는 creating) 엔트리 제거 (T-0295).

    이미 사라진 worktree 의 dangling 부기만 정리(사용자 데이터 삭제 아님). worktree dir 이 존재하는
    엔트리는 손대지 않는다. sensitivity: prune 이 no-op 이면 A_1/A_2 잔여로 red.
    """
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", state="idle"),        # dir 부재 → 제거
          _lease(wp, slot="work/A_2", repo="A", state="creating"),    # dir 부재 → 제거
          _lease(wp, slot="work/A_3", repo="A", state="leased"))      # dir 존재 → 유지
    wp.slot_path("work/A_3").mkdir(parents=True, exist_ok=True)
    pruned = wp.prune_stale_leases()
    assert set(pruned) == {"work/A_1", "work/A_2"}
    assert {l.slot for l in wp.list_leases()} == {"work/A_3"}


def test_prune_stale_leases_keeps_present_worktree(wp):
    """sensitivity — worktree dir 이 존재하면 어떤 state(creating 포함)든 prune 이 안 건드린다 (T-0295)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="creating"))
    wp.slot_path("work/A_1").mkdir(parents=True, exist_ok=True)
    assert wp.prune_stale_leases() == []
    assert {l.slot for l in wp.list_leases()} == {"work/A_1"}


def test_alloc_skips_idle_slot_with_missing_worktree(wp):
    """must-fix(1) 위험차단 — worktree dir 없는 idle 슬롯을 alloc 이 leased 로 안 넘긴다 (T-0295·실경로).

    stale 엔트리가 force_release 로 idle 이 되면 이 재사용 루프가 *없는 worktree* 를 leased 로
    할당하던 위험(codex 실측). 실경로(git_runner 미주입) fs 가드가 부재 슬롯을 skip → NeedsCreate.
    sensitivity: 가드 제거 시 leased 승격돼 red(alloc-nonexistent 재현).
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"))
    # work/A_1 dir 미생성 = worktree 부재.
    with pytest.raises(wp.NeedsCreate):
        wp.alloc("A", session="me")   # git_runner 미주입 = 실경로 fs 가드
    assert [(l.slot, l.state) for l in wp.list_leases()] == [("work/A_1", "idle")], \
        "부재 worktree idle 이 leased 로 승격됨(위험차단 실패)"


def test_alloc_reuses_idle_slot_with_present_worktree(wp):
    """sensitivity 대조 — worktree dir 이 존재하는 idle 슬롯은 실경로에서도 정상 재사용 (T-0295)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"))
    wp.slot_path("work/A_1").mkdir(parents=True, exist_ok=True)   # worktree 존재 모델
    lease = wp.alloc("A", session="me")   # 실경로·target_branch 없음 → checkout 불요·git 안 탐
    assert lease.slot == "work/A_1" and lease.state == "leased"


def test_alloc_resume_does_not_reattach_creating_orphan(wp):
    """should-fix(2) — creating orphan(worktree 존재·브랜치 체크아웃)을 alloc(resume=)이 재부착 안 함 (T-0295).

    worktree add 성공 후~submodule init 전 SIGKILL 로 남은 creating lease 는 worktree 가 그 브랜치를
    체크아웃 중이라 live HEAD 로 매칭되지만, resume 재부착 루프가 state=="creating" 을 제외해 조용한
    creating→leased 승격(submodule 미초기화·incomplete surface 우회)을 막는다 → NeedsCreate.
    sensitivity: 제외 가드 없으면 leased 로 재부착돼 red.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="creating"))
    git = FakeGit(head="a5-pay")   # 슬롯 live HEAD = a5-pay (creating 이지만 브랜치 체크아웃됨)
    with pytest.raises(wp.NeedsCreate):
        wp.alloc("A", resume="a5-pay", session="new", git_runner=git)
    assert [(l.slot, l.state) for l in wp.list_leases()] == [("work/A_1", "creating")], \
        "creating orphan 이 resume 으로 leased 재부착됨(should-fix 실패)"


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


def test_real_git_runner_interactive_forwards_explicit_timeout(wp, monkeypatch):
    """`_real_git_runner_interactive(cwd, timeout=X)` 가 X 를 subprocess.run(timeout=X) 로 넘긴다 (T-0292).

    sensitivity(reviewer should-fix): 명시 timeout forwarding 이 없으면(예 line 을 항상
    SUBMODULE_TIMEOUT 로) worktree-add 가 GIT_TIMEOUT_SECONDS 대신 기본값을 써도 안 잡힌다
    (false-green 갭). 기본값 forwarding 은 위 테스트가 덮으므로 여기선 *명시값*(999)을 덮는다.
    """
    captured = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(wp.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wp.subprocess, "run", fake_run)
    runner = wp._real_git_runner_interactive(wp.REPO, timeout=999)
    rc, out = runner(["worktree", "add", "x"])
    assert rc == 0
    assert out == ""
    assert captured["kwargs"].get("timeout") == 999, "명시 timeout 이 subprocess 로 forwarding 안 됨"
    assert "capture_output" not in captured["kwargs"], "인터랙티브가 capture 함(stdio 상속 깨짐)"


def test_real_git_runner_interactive_none_timeout_forwards_none(wp, monkeypatch):
    """console-visible 러너는 timeout=None(무제한)도 그대로 넘긴다 — 진행 가시라 안전(T-0292).

    captured 러너와 대조: worktree-add 인터랙티브 러너는 GIT_TIMEOUT_SECONDS=None(PM_GIT_TIMEOUT=
    none)이면 무제한이어야 한다(캡 안 함). captured 는 유한 캡(아래 대조 테스트)·visible 은 none 허용.
    """
    captured = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(wp.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wp.subprocess, "run", fake_run)
    runner = wp._real_git_runner_interactive(wp.REPO, timeout=None)
    rc, out = runner(["worktree", "add", "x"])
    assert rc == 0
    assert captured["kwargs"].get("timeout") is None, "console-visible 러너가 None(무제한)을 캡함"


# ── T-0292: captured 러너는 절대 무제한이 안 됨 (codex 게이트·silent-hang 차단) ──


def test_real_git_runner_caps_none_timeout_to_finite(wp, monkeypatch):
    """captured 러너는 GIT_TIMEOUT_SECONDS=None(PM_GIT_TIMEOUT=none)이어도 유한 캡(silent-hang 차단·codex).

    captured 러너는 콘솔에 진행이 안 보여(silent) 무제한이면 network stall(base 파생 `fetch origin`)
    시 silent hang 한다. None → `_GIT_TIMEOUT_DEFAULT`(유한)로 폴백-캡한다. 무제한은 진행이 보이는
    console-visible worktree-add 러너에만 허용(위 대조 테스트).
    """
    captured = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(wp.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wp.subprocess, "run", fake_run)
    monkeypatch.setattr(wp, "GIT_TIMEOUT_SECONDS", None)  # PM_GIT_TIMEOUT=none 재현
    runner = wp._real_git_runner(wp.REPO)
    rc, out = runner(["fetch", "origin"])
    assert rc == 0
    assert captured["kwargs"].get("timeout") is not None, \
        "captured 러너가 None(무제한)으로 감 — silent hang 위험(codex 게이트)"
    assert captured["kwargs"].get("timeout") == wp._GIT_TIMEOUT_DEFAULT


def test_real_git_runner_honors_finite_timeout(wp, monkeypatch):
    """captured 러너가 유한 GIT_TIMEOUT_SECONDS 는 그대로 쓴다(캡은 None 만·env 값 존중·sensitivity).

    None 만 캡하고 유한값은 통과시킨다 — 항상 `_GIT_TIMEOUT_DEFAULT` 로 하드코딩하면(env 무시)
    이 테스트가 fail 한다(양방향 존중 회귀 가드).
    """
    captured = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(wp.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wp.subprocess, "run", fake_run)
    monkeypatch.setattr(wp, "GIT_TIMEOUT_SECONDS", 300)  # 유한 env 값
    runner = wp._real_git_runner(wp.REPO)
    rc, out = runner(["status", "--porcelain"])
    assert rc == 0
    assert captured["kwargs"].get("timeout") == 300, "유한 GIT_TIMEOUT_SECONDS 가 존중 안 됨(캡 오작동)"


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
    """`symbolic-ref HEAD`(full ref·`--short` 없이)를 (rc, out) 으로 모델링하는 주입 runner (T-0072·T-0377·codex 게이트).

    detached(rc≠0)/조회불가/정상·unborn 브랜치(rc0+`refs/heads/<name>`)를 결정적으로 친다 —
    current_branch 의 분기(rc≠0→None·refs/heads/ 접두 strip·비-브랜치 ref→None)를 hermetic
    하게 검증한다(실 git 없이·DI seam). 그 외 git 호출은 (0, ""). `out` 은 full ref 를 준다
    (`--short` 를 안 쓰는 이유=동명 태그 모호성 접두 회피·T-0377).
    """

    def __init__(self, *, rc: int = 0, out: str = "refs/heads/main\n"):
        self.rc = rc
        self.out = out
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv == ["symbolic-ref", "HEAD"]:
            return (self.rc, self.out)
        return (0, "")


def test_current_branch_returns_live_head(wp):
    """정상 — symbolic-ref 가 full ref 를 돌려주면 refs/heads/ 접두를 벗겨 브랜치명 반환(live 조회)."""
    git = _SymbolicRefGit(rc=0, out="refs/heads/a5-pay\n")
    assert wp.current_branch("work/A_1", git_runner=git) == "a5-pay"
    # symbolic-ref HEAD(full ref·`--short` 없이·T-0377)를 실제로 호출했다(live·저장 복사본 아님).
    assert ["symbolic-ref", "HEAD"] in git.calls


def test_current_branch_detached_head_returns_none(wp):
    """detached HEAD — symbolic-ref 가 rc≠0(symbolic ref 아님)이면 None(브랜치 아님)."""
    git = _SymbolicRefGit(rc=1, out="fatal: ref HEAD is not a symbolic ref\n")
    assert wp.current_branch("work/A_1", git_runner=git) is None


def test_current_branch_non_branch_ref_returns_none(wp):
    """`refs/heads/` 로 시작 안 하는 이상 출력(브랜치 아님) → 보수적 None (T-0377)."""
    git = _SymbolicRefGit(rc=0, out="refs/tags/v1.3.0\n")
    assert wp.current_branch("work/A_1", git_runner=git) is None


def test_current_branch_unborn_branch_returns_name(wp):
    """unborn 브랜치(아직 커밋 0) — symbolic-ref HEAD 가 `refs/heads/<name>` 을 rc0 으로 준다 → 이름 반환.

    codex T-0072 게이트의 must-fix 회귀: rev-parse --abbrev-ref 는 unborn 을 rc≠0 으로 줘
    detached 로 *오판*(→ None="미지정")했으나, symbolic-ref HEAD 는 unborn 도 full ref 를 그대로
    준다(git=진실·ADR-0013 amend — 이름이 있으면 보여야 한다·`--short` 없어도 unborn 거동 보존).
    """
    git = _SymbolicRefGit(rc=0, out="refs/heads/main\n")
    assert wp.current_branch("work/A_1", git_runner=git) == "main"


def test_current_branch_rc_nonzero_returns_none(wp):
    """git 호출 실패(rc≠0) → None(fail-soft·예외 raise 금지·손상/락/git부재 흡수)."""
    git = _SymbolicRefGit(rc=128, out="fatal: not a git repository\n")
    assert wp.current_branch("work/A_1", git_runner=git) is None


def test_current_branch_empty_output_returns_none(wp):
    """빈 출력(rc0 이지만 브랜치명 없음) → None(보수적·이상 출력 흡수)."""
    git = _SymbolicRefGit(rc=0, out="\n")
    assert wp.current_branch("work/A_1", git_runner=git) is None


def test_current_branch_tag_collision_returns_pure_name(wp):
    """동명 태그+브랜치(릴리즈 `v1.3.0` 브랜치+태그) → `symbolic-ref HEAD` 는 full ref 라
    태그 존재와 무관하게 `refs/heads/v1.3.0` 을 주고, 접두를 벗겨 순수명 반환 (T-0377·가짜 외부개입 해소).

    `--short` 는 동명 태그 시 모호성 회피로 `heads/v1.3.0` 을 돌려줘 장부 기록(`v1.3.0`)과 불일치→
    0단계 가짜 diverged FAIL-LOUD 로 차단됐다(PM 76 실측). full ref 로 바꾸면 모호성 자체가 사라져
    (`refs/heads/<정확한명>` 고정) 태그와 무관하게 순수명이 나온다.
    """
    git = _SymbolicRefGit(rc=0, out="refs/heads/v1.3.0\n")
    assert wp.current_branch("work/A_1", git_runner=git) == "v1.3.0"


def test_current_branch_real_heads_prefixed_branch_preserved(wp):
    """진짜 이름이 `heads/x` 인 브랜치(합법·`git check-ref-format --branch heads/v1.3.0` 통과)를
    `x` 로 오인하지 않는다 — full ref `refs/heads/heads/v1.3.0` → `heads/v1.3.0` (T-0377·codex must-fix 회귀).

    사후 `heads/` strip 방식은 이 브랜치를 `v1.3.0` 으로 오인했다 — `refs/heads/` 접두만 정확히
    벗기면 나머지 `heads/v1.3.0` 이 온전히 보존된다.
    """
    git = _SymbolicRefGit(rc=0, out="refs/heads/heads/v1.3.0\n")
    assert wp.current_branch("work/A_1", git_runner=git) == "heads/v1.3.0"


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


@_git_required
def test_current_branch_real_git_tag_collision_returns_pure_name(proj, tmp_path):
    """실 git — 브랜치명과 같은 이름의 태그가 있어도 current_branch 는 순수 브랜치명 반환 (T-0377·PM 76 재현).

    릴리즈가 `v1.3.0` 브랜치를 그대로 `v1.3.0` 태그로 찍은 상황을 실제로 만들어(`git tag v1.3.0`),
    `symbolic-ref HEAD`(full ref) 방식이 `--short` 의 모호성 접두(`heads/v1.3.0`) 없이 순수명
    `v1.3.0` 을 준다는 걸 실 git 으로 백스톱한다 — 부트스트랩 0단계 가짜 "외부 개입" 차단 해소.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)
    lease = wp.create_slot("A", branch="v1.3.0", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)

    # 동명 태그 생성 — 이 상태에서 `symbolic-ref --short HEAD` 는 heads/v1.3.0 을 준다(모호성 회피).
    _git(slot_dir, "tag", "v1.3.0")
    # full ref 방식이라 태그와 무관하게 순수 브랜치명(장부 기록과 일치).
    assert wp.current_branch(lease.slot) == "v1.3.0"


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
    assert git.did("checkout", "--no-recurse-submodules", "a5-pay")
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


def _run_hook(hook_path: Path, stdin: str, *, env_override: bool = False,
              skip_live_gate: bool = False) -> int:
    """생성된 pre-push 훅을 `sh` 로 직접 실행하고 종료코드를 반환한다 (T-0076·T-0223).

    훅은 stdin 으로 `<localref> <localsha> <remoteref> <remotesha>` 줄들을 받는다(실 git
    pre-push 계약). `env_override` 면 `PM_ALLOW_PROTECTED_PUSH=1`, `skip_live_gate` 면
    `PM_SKIP_LIVE_GATE=1` 을 환경에 둔다. 라이브 게이트 board.py 는 훅 옆 sidecar `engine-root`
    로 해소되므로(cwd 무관·codex r2) 게이트 분기는 sidecar 유무/내용으로 제어한다.
    """
    env = dict(os.environ)
    if env_override:
        env["PM_ALLOW_PROTECTED_PUSH"] = "1"
    else:
        env.pop("PM_ALLOW_PROTECTED_PUSH", None)
    if skip_live_gate:
        env["PM_SKIP_LIVE_GATE"] = "1"
    else:
        env.pop("PM_SKIP_LIVE_GATE", None)
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
    # engine-root sidecar = PM 홈 절대경로 1줄 (T-0223 — 훅의 board.py 해소 단일 진실).
    engine_root = hook_dir / "engine-root"
    assert engine_root.read_text(encoding="utf-8").strip() == str(wp.REPO.resolve())
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
    # engine-root 도 재설치마다 갱신(멱등 — stale PM 홈 경로 잔존 방지·T-0223).
    engine_root = wp.REPO_HOOKS_DIR / "A" / "engine-root"
    assert engine_root.read_text(encoding="utf-8").strip() == str(wp.REPO.resolve())
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
def test_generated_hook_pm_allow_with_skip_passes_protected(wp):
    """생성 훅 직접 실행 — PM_ALLOW_PROTECTED_PUSH=1 + PM_SKIP_LIVE_GATE=1 이면 보호목록(main)도
    통과 (T-0223 — 라이브 게이트 승격 후 우회 경로·라이브-무관·긴급 변경 한정).

    skip 은 라이브 check 만 생략 → 훅이 board.py 를 아예 호출하지 않아 hermetic(cwd 무관).
    """
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main"], git_runner=FakeGit())
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    assert _run_hook(hook, _push_line("refs/heads/main"),
                     env_override=True, skip_live_gate=True) == 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_pm_allow_engine_absent_fail_closed(wp):
    """생성 훅 직접 실행 — PM_ALLOW=1·PM_SKIP 없음인데 engine-root 가 가리키는 PM 홈에 board.py 가
    없으면 fail-closed 거부 (T-0223·codex r2).

    라이브 게이트 승격: PM_ALLOW 만으론 더는 보호 push 를 통과시키지 않는다. engine-root sidecar 는
    PM 홈(=proj)을 가리키는데 이 fixture proj 엔 `.project_manager/tools/board.py` 가 없으므로 게이트를
    못 돌려 fail-closed(rc≠0)한다 — 무력화 방지. (board.py 는 슬롯 아닌 PM 홈 소유임을 반영·codex r2.)
    """
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main"], git_runner=FakeGit())
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    # engine-root sidecar 는 PM 홈(proj)을 가리키지만 proj 엔 board.py 없음.
    assert (wp.REPO_HOOKS_DIR / "A" / "engine-root").exists()
    assert not (wp.REPO / ".project_manager" / "tools" / "board.py").exists()
    assert _run_hook(hook, _push_line("refs/heads/main"), env_override=True) != 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_engine_root_sidecar_absent_fail_closed(wp):
    """생성 훅 직접 실행 — engine-root sidecar 자체가 없으면 PM_ALLOW=1 이어도 fail-closed 거부 (T-0223 DoD ③).

    sidecar 부재/경로 무효 = board.py 미해소 → 기존 fail-closed 2분기 메시지 유지. 설치 후 sidecar 를
    지워 재현(설치자가 안 썼거나 유실된 경우) — 게이트를 못 돌리면 무력화 방지로 거부한다.
    """
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main"], git_runner=FakeGit())
    (wp.REPO_HOOKS_DIR / "A" / "engine-root").unlink()   # sidecar 제거.
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    assert _run_hook(hook, _push_line("refs/heads/main"), env_override=True) != 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_skip_alone_without_pm_allow_hard_blocks(wp):
    """생성 훅 직접 실행 — PM_SKIP_LIVE_GATE=1 단독(PM_ALLOW 없음)은 여전히 T-0076 하드 차단 (reviewer 권고 가드).

    승인(PM_ALLOW)과 검증 스킵(PM_SKIP)은 별개 스위치 — skip 만으론 보호 push 를 열지 못한다. 이 가드가
    없으면 향후 PM_SKIP 체크가 하드차단보다 앞으로 이동해도(게이트 조용히 무력화) 안 잡힌다. skip 만 켜고
    PM_ALLOW 를 안 주면 하드 차단(rc≠0)을 직접 단언한다.
    """
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main"], git_runner=FakeGit())
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    assert _run_hook(hook, _push_line("refs/heads/main"),
                     env_override=False, skip_live_gate=True) != 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_generated_hook_tag_push_unaffected(wp):
    """생성 훅 직접 실행 — tag push 는 보호 브랜치가 아니라 통과(rc 0)·PM_ALLOW/라이브 게이트 무관 (T-0076·T-0223).

    remote ref 가 `refs/tags/*` 면 보호 판정 대상이 아니다 → PM_ALLOW=1 이어도 라이브 게이트를
    타지 않고 통과(board.py 미호출·hermetic). tag 시맨틱 회귀 무변경 단언.
    """
    _mk_bare_placeholder(wp, "A")
    wp.install_protected_hook("A", ["main"], git_runner=FakeGit())
    hook = wp.REPO_HOOKS_DIR / "A" / "pre-push"
    assert _run_hook(hook, _push_line("refs/tags/v1.0"), env_override=True) == 0


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
def test_real_git_protected_push_pm_allow_with_skip_allowed(proj, tmp_path):
    """실 git e2e — PM_ALLOW_PROTECTED_PUSH=1 + PM_SKIP_LIVE_GATE=1 이면 hooksPath 훅을 거쳐도
    보호 main push 허용·전진 (T-0096·T-0223 DoD ③).

    T-0223 승격 후 PM_ALLOW 만으론 보호 push 가 라이브 게이트에 막힌다 — PM_SKIP_LIVE_GATE=1
    (라이브-무관·긴급 변경 우회)이 붙어야 통과한다. skip 은 board.py 호출 자체를 생략하므로
    라이브 게이트 기록이 없어도 전진함을 실 push 로 단언한다.
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
    (slot_dir / "change.txt").write_text("skip work on main\n", encoding="utf-8")
    _git(slot_dir, "add", "change.txt")
    _git(slot_dir, "commit", "-q", "-m", "skip change on main")
    slot_main = _git(slot_dir, "rev-parse", "main").stdout.strip()

    # PM_ALLOW + PM_SKIP — _git 헬퍼가 env 를 merge 한다(check=True·통과 기대).
    _git(slot_dir, "push", "server", "main",
         env={"PM_ALLOW_PROTECTED_PUSH": "1", "PM_SKIP_LIVE_GATE": "1"})
    # server bare 의 main 이 슬롯 main 으로 전진(승인+skip 이 차단을 풀었다).
    server_main = _git(server, "rev-parse", "main").stdout.strip()
    assert server_main == slot_main, "PM_ALLOW+PM_SKIP push 후 server main 이 전진 안 됨"


# ════════════════════════════════════════════════════════════════════════
# 라이브 게이트 승격 — protected push 에 livegate check green 요구 (T-0223·ADR-0039 D2/D3)
# ════════════════════════════════════════════════════════════════════════
# 위 e2e 는 board.py 를 안 태우는 경로(차단·override+skip)만 본다. 여기선 승격 경로를 실 발화시킨다:
# 훅이 sidecar `engine-root`(설치자가 쓴 PM 홈 REPO 절대경로)에서 board.py 를 해소 → `livegate check
# --rev <push sha>`. **슬롯 worktree(family/회사 checkout)엔 PM 엔진 파일이 없다**(T-0076 무영향) —
# board.py·livegate.json 은 **PM 홈(proj)** 소유다. 그래서 표준 family bare(`_mk_real_bare`·엔진 없음)를
# 쓰고, PM 홈에 board.py 를 심고(_install_engine), livegate.json 은 PM 홈 .local(_write_livegate)에 쓴다.
#   ① 라이브 게이트 pass 기록 + PM_ALLOW → 통과(전진)   ② 기록 부재 / rev 불일치 → 거부(server 무변경)
#   ④ 비보호 브랜치 → 라이브 게이트 무관(기록 없어도 통과)   codex r2: 슬롯엔 엔진 없음(회사 repo 무영향)
# ⚠️ livegate record 자체는 실 LLM 릴리즈 wave(pytest -m release)라 여기서 안 돌린다 — board 테스트가
#    mock 으로 record 를 검증한다. 여기선 결과 JSON(livegate.json)을 직접 심어 훅↔check 통합만 태운다
#    (check 채널=T-0221·[[dcf3abf]]).


def _install_engine(engine_root: Path) -> None:
    """PM 홈(engine_root=wp.REPO)에 실 board.py 를 심는다 — 훅이 sidecar 로 해소하는 엔진 (T-0223 codex r2).

    슬롯 worktree(family/회사 checkout)엔 엔진 파일이 없다(회사 repo 무영향) — board.py 는 PM 홈에만
    존재하고 훅은 sidecar `engine-root`(=이 engine_root)로 그걸 찾는다. board.py 는 단일 파일이라
    그것만 복사하면 `livegate check` 가 standalone 동작(yaml=런타임 의존·테스트 env 보유). board.py
    REPO=Path(__file__).parents[2]=engine_root → livegate.json 을 engine_root/.project_manager/.local 에서 읽는다.

    `identity_args.py`(ADR-0057·T-0322)도 함께 심는다 — board.py 가 같은 tools/ 디렉토리에서
    경로-앵커 로드(`_load_identity_args`)하는 **load-bearing sibling**이라, 빠지면 이 최소-격리
    엔진(board.py 단일 파일)이 import 시점에 fail-loud 로 죽는다(전 서브에 정체성 파싱 필수).
    """
    tools = engine_root / ".project_manager" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    shutil.copy(str(TOOLS / "board.py"), str(tools / "board.py"))
    shutil.copy(str(TOOLS / "identity_args.py"), str(tools / "identity_args.py"))


def _write_livegate(engine_root: Path, *, head: str, status: str = "pass",
                    rc: int = 0) -> None:
    """PM 홈(engine_root)의 `.project_manager/.local/livegate.json` 에 게이트 결과를 심는다 (T-0223 e2e).

    `board.py livegate record` 가 쓰는 것과 같은 스키마·같은 위치(board.py REPO=engine_root →
    LOCAL_DIR=engine_root/.project_manager/.local). record 는 실 LLM 이라 안 돌리고 JSON 만 직접 심어
    훅↔check 통합을 hermetic 히 태운다(head 가 push sha 와 일치하면 green). `n` = board.LIVEGATE_RELEASE_PIN
    (릴리즈 wave 케이스 수)로 스키마 충실 — 단 `_livegate_check` 는 status==pass ∧ head==rev 만 보고 n 은
    안 보므로(check 채널) green 판정엔 무관하다.
    """
    local_dir = engine_root / ".project_manager" / ".local"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "livegate.json").write_text(
        json.dumps({"head": head, "status": status, "n": _LIVEGATE_RELEASE_PIN,
                    "rc": rc, "ts": "t"}),
        encoding="utf-8")


def _slot_commit_on(slot_dir: Path, branch: str, marker: str) -> str:
    """슬롯에서 새 커밋을 만들어 ref 를 전진시키고 그 sha 를 돌려준다 (pre-push 발화 전제).

    pre-push 훅은 *실제 ref 갱신* push 에만 발화한다 — 각 push 전 새 커밋으로 ref 를 전진시킨다.
    """
    (slot_dir / "change.txt").write_text(marker, encoding="utf-8")
    _git(slot_dir, "add", "change.txt")
    _git(slot_dir, "commit", "-q", "-m", marker.strip() or "change")
    return _git(slot_dir, "rev-parse", branch).stdout.strip()


@_git_required
def test_real_git_livegate_green_allows_protected_push(proj, tmp_path):
    """실 git e2e — 라이브 게이트 pass 기록(head=push sha) + PM_ALLOW → 보호 main push 통과·전진 (T-0223 DoD ①).

    훅이 engine-root sidecar 로 PM 홈 board.py 를 해소 → `livegate check --rev <push sha>` rc0 → 통과.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _install_engine(proj)             # PM 홈 엔진 board.py (슬롯엔 없음·codex r2)
    _mk_real_bare(wp, "A", tmp_path)   # 표준 family bare — 엔진 파일 없음(회사 repo 무영향)
    server = tmp_path / "A-server.git"
    _git(tmp_path, "clone", "--bare", "-q", str(wp.bare_repo_path("A")), str(server))

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)
    assert wp.install_protected_hook("A", ["main"]) is True
    _git(slot_dir, "remote", "add", "server", str(server))

    slot_main = _slot_commit_on(slot_dir, "main", "release change\n")
    _write_livegate(proj, head=slot_main)   # PM 홈 .local 에 라이브 게이트 pass @ push sha.

    # PM_ALLOW + 라이브 게이트 green — check=True(통과 기대).
    _git(slot_dir, "push", "server", "main", env={"PM_ALLOW_PROTECTED_PUSH": "1"})
    assert _git(server, "rev-parse", "main").stdout.strip() == slot_main, \
        "라이브 게이트 green 인데 보호 push 가 전진 안 됨"


@_git_required
def test_real_git_livegate_record_absent_blocks_protected_push(proj, tmp_path):
    """실 git e2e — 라이브 게이트 기록 부재면 PM_ALLOW 여도 보호 main push 거부·server 무변경 (T-0223 DoD ②).

    board.py livegate check 가 "기록 없음"(rc1) → 훅 fail·거부. record 없이 릴리즈 못 나감을 못박는다.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _install_engine(proj)             # PM 홈 엔진 board.py (슬롯엔 없음·codex r2)
    _mk_real_bare(wp, "A", tmp_path)   # 표준 family bare — 엔진 파일 없음(회사 repo 무영향)
    server = tmp_path / "A-server.git"
    _git(tmp_path, "clone", "--bare", "-q", str(wp.bare_repo_path("A")), str(server))

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)
    assert wp.install_protected_hook("A", ["main"]) is True
    _git(slot_dir, "remote", "add", "server", str(server))

    slot_main = _slot_commit_on(slot_dir, "main", "unrecorded change\n")
    server_before = _git(server, "rev-parse", "main").stdout.strip()
    # 라이브 게이트 기록을 심지 않음 → check "기록 없음" rc1.

    rc = subprocess.run(
        [_GIT, "-C", str(slot_dir), "push", "server", "main"],
        env={**os.environ, "PM_ALLOW_PROTECTED_PUSH": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).returncode
    assert rc != 0, "라이브 게이트 기록 부재인데 보호 push 가 차단 안 됨(rc=0)"
    server_after = _git(server, "rev-parse", "main").stdout.strip()
    assert server_after == server_before, "차단됐는데 server main 이 갱신됨"
    assert server_after != slot_main, "server main 이 슬롯 main 으로 전진함(차단 실패)"


@_git_required
def test_real_git_livegate_rev_mismatch_blocks_protected_push(proj, tmp_path):
    """실 git e2e — 라이브 게이트 기록 head ≠ push sha 면 PM_ALLOW 여도 거부·server 무변경 (T-0223 DoD ②).

    check 가 "rev 불일치"(rc1) → 훅 fail. 다른(stale) 커밋에서 돈 게이트로 새 커밋을 밀 수 없음.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _install_engine(proj)             # PM 홈 엔진 board.py (슬롯엔 없음·codex r2)
    _mk_real_bare(wp, "A", tmp_path)   # 표준 family bare — 엔진 파일 없음(회사 repo 무영향)
    server = tmp_path / "A-server.git"
    _git(tmp_path, "clone", "--bare", "-q", str(wp.bare_repo_path("A")), str(server))

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)
    assert wp.install_protected_hook("A", ["main"]) is True
    _git(slot_dir, "remote", "add", "server", str(server))

    slot_main = _slot_commit_on(slot_dir, "main", "new change\n")
    server_before = _git(server, "rev-parse", "main").stdout.strip()
    # 게이트 기록은 pass 지만 head 가 엉뚱한 sha(=push sha 아님) → rev 불일치.
    _write_livegate(proj, head="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    rc = subprocess.run(
        [_GIT, "-C", str(slot_dir), "push", "server", "main"],
        env={**os.environ, "PM_ALLOW_PROTECTED_PUSH": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).returncode
    assert rc != 0, "라이브 게이트 rev 불일치인데 보호 push 가 차단 안 됨(rc=0)"
    server_after = _git(server, "rev-parse", "main").stdout.strip()
    assert server_after == server_before, "차단됐는데 server main 이 갱신됨"
    assert server_after != slot_main, "server main 이 슬롯 main 으로 전진함(차단 실패)"


@_git_required
def test_real_git_livegate_non_protected_push_unaffected(proj, tmp_path):
    """실 git e2e — 비보호 브랜치 push 는 라이브 게이트 기록이 없어도 통과·전진 (T-0223 DoD ④).

    PM 홈 엔진+engine-root sidecar 로 승격 경로가 배선돼 있어도 비보호 브랜치 push 는 게이트를 타지
    않는다 — 라이브 게이트 무관을 실 push 로 단언(비보호 시맨틱 회귀 무변경).
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _install_engine(proj)             # PM 홈 엔진 board.py (슬롯엔 없음·codex r2)
    _mk_real_bare(wp, "A", tmp_path)   # 표준 family bare — 엔진 파일 없음(회사 repo 무영향)
    server = tmp_path / "A-server.git"
    _git(tmp_path, "clone", "--bare", "-q", str(wp.bare_repo_path("A")), str(server))

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)
    assert wp.install_protected_hook("A", ["main"]) is True   # main 만 보호
    _git(slot_dir, "remote", "add", "server", str(server))

    # 비보호 브랜치 work/x — 라이브 게이트 기록 없음.
    _git(slot_dir, "checkout", "-q", "-b", "work/x")
    slot_feat = _slot_commit_on(slot_dir, "work/x", "feature change\n")

    _git(slot_dir, "push", "server", "work/x")   # check=True·통과 기대(게이트 무관).
    assert _git(server, "rev-parse", "work/x").stdout.strip() == slot_feat, \
        "비보호 브랜치 push 가 server 에 반영 안 됨(라이브 게이트가 비보호를 막음)"


@_git_required
def test_real_git_livegate_multi_protected_ref_all_or_nothing(proj, tmp_path):
    """실 git e2e — 한 push 에 보호 ref 여러 개(main green·release 기록 불일치)면 push 전체가 거부되고
    server 가 **둘 다** 미갱신 (T-0223 codex must-fix — all-or-nothing·미검증 ref 편승 차단).

    pre-push 는 push 전체에 한 번 발화한다 → 훅이 보호 ref 를 *전부* 검증한다. livegate.json 은
    main 커밋에서만 green(head=main sha)이라 main check 는 통과하지만 release check 는 rev 불일치로
    fail → `git push server main release` 한 번이 통째로 거부되고 server main 도 release 도 안 올라간다.
    (첫 ref 만 보고 break 하는 옛 구현이면 main green 이 release 를 편승시켜 이 단언이 깨진다.)
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _install_engine(proj)             # PM 홈 엔진 board.py (슬롯엔 없음·codex r2)
    _mk_real_bare(wp, "A", tmp_path)   # 표준 family bare — 엔진 파일 없음(회사 repo 무영향)
    server = tmp_path / "A-server.git"
    _git(tmp_path, "clone", "--bare", "-q", str(wp.bare_repo_path("A")), str(server))

    lease = wp.create_slot("A", branch="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path(lease.slot)
    assert wp.install_protected_hook("A", ["main", "release"]) is True   # main·release 둘 다 보호
    _git(slot_dir, "remote", "add", "server", str(server))

    # main 전진(새 커밋) + release 브랜치(main 에서 갈라 별도 커밋 — main sha 와 다른 tip).
    main_new = _slot_commit_on(slot_dir, "main", "main release change\n")
    _git(slot_dir, "checkout", "-q", "-b", "release")
    release_tip = _slot_commit_on(slot_dir, "release", "release-only change\n")
    _git(slot_dir, "checkout", "-q", "main")
    assert release_tip != main_new

    server_main_before = _git(server, "rev-parse", "main").stdout.strip()
    # 라이브 게이트 pass @ main sha 만 → main check green·release check 는 rev 불일치.
    _write_livegate(proj, head=main_new)

    # 한 push 로 두 보호 ref — release 가 미green 이라 push 전체 거부(rc≠0).
    rc = subprocess.run(
        [_GIT, "-C", str(slot_dir), "push", "server", "main", "release"],
        env={**os.environ, "PM_ALLOW_PROTECTED_PUSH": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).returncode
    assert rc != 0, "release 미green 인데 다중 보호 ref push 가 차단 안 됨(rc=0)"

    # server main 무갱신(green 이던 main 도 release 실패에 all-or-nothing 으로 함께 거부).
    server_main_after = _git(server, "rev-parse", "main").stdout.strip()
    assert server_main_after == server_main_before, "차단됐는데 server main 이 갱신됨(편승)"
    assert server_main_after != main_new, "server main 이 슬롯 main 으로 전진함(all-or-nothing 실패)"
    # server 에 release ref 미생성(미검증 ref 가 안 올라감).
    release_on_server = subprocess.run(
        [_GIT, "-C", str(server), "rev-parse", "--verify", "--quiet", "refs/heads/release"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).returncode
    assert release_on_server != 0, "미검증 release ref 가 server 에 올라감(편승 차단 실패)"


# ════════════════════════════════════════════════════════════════════════
# Lease git 필드 additive + 미지 키 보존 라운드트립 (T-0350 · ADR-0060 · spike §F9)
# 슬롯 git 상태를 *기대* 축으로 기계 기록 + additive 스키마 클래스 폐쇄(구·신 엔진 왕복 무손실).
# ════════════════════════════════════════════════════════════════════════

_OLD_SHA = "a" * 40      # 기록된 head(과거 스냅).
_NEW_SHA = "b" * 40      # live head(전진/변경).


def _seed_git_lease(wp, *, slot="work/A_1", repo="A", branch="a1", head=_OLD_SHA,
                    submodules=None, base=None, session="s1", state="leased", pid=None):
    """git 필드가 실린 leased 엔트리를 장부에 심는다(compare 전제 구성). pid=None → 살아있는 os.getpid()."""
    git = {"branch": branch, "head": head,
           "submodules": submodules if submodules is not None else [], "recorded_at": "t"}
    if base is not None:
        git = {"base": base, **git}
    _seed(wp, wp.Lease(slot=slot, repo=repo, session=session,
                       pid=os.getpid() if pid is None else pid,
                       started="t", state=state, git=git))


def test_from_dict_preserves_unknown_keys_round_trip(wp):
    """미지 최상위 키(task·future)를 `extra` 로 보존해 to_dict 가 재방출한다 — 구·신 엔진 왕복 무손실(T-0350).

    핵심: 신규 키를 모르는 엔진 버전이 read-modify-write(from_dict→to_dict) 하나만 돌려도
    *모든 슬롯*의 신규 키(git·task)가 소실되면 drift 감지가 가짜 기준 위에서 돈다. extra 보존이
    그 왕복을 무손실로 닫는다(additive 스키마 클래스 폐쇄).
    """
    payload = {
        "slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
        "started": "t", "state": "leased", "test_cmd": None,
        "git": {"base": {"branch": "main", "commit": "abc"}, "branch": "A_1",
                "head": "def", "submodules": [{"path": "v", "pin": "p"}],
                "recorded_at": "2026-07-17T14:27:00+09:00"},
        "task": "myjob",                    # T-035x 미구현 키(이 엔진 버전이 모름)
        "future_key": {"nested": [1, 2]},   # 향후 additive 키
    }
    lease = wp.Lease.from_dict(payload)
    # 구 키(canonical)+신규 키(git)+미지 키(task/future) 전부 무손실 재방출.
    assert lease.to_dict() == payload
    assert lease.extra == {"task": "myjob", "future_key": {"nested": [1, 2]}}
    assert lease.git == payload["git"]


def test_read_ledger_preserves_unknown_keys_through_rewrite(wp):
    """파일 레벨 — 미지 키 포함 장부를 _read_ledger→_write_ledger 왕복해도 키 무손실(silent drop 0·T-0350).

    장부 지속은 이 read-modify-write 왕복 하나로 수렴하므로(모든 생명주기 op 이 전 리스를 이렇게
    다룸), 이 왕복이 무손실이어야 버전 skew(adopter#0 import 사본 lag)에서 신규 키가 안 날아간다.
    """
    ledger = {"leases": [{
        "slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
        "started": "t", "state": "leased", "test_cmd": None,
        "git": {"branch": "A_1", "head": "def", "submodules": [], "recorded_at": "t"},
        "task": "myjob",
    }]}
    wp.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    wp.LEASES_FILE.write_text(json.dumps(ledger), encoding="utf-8")
    with wp._lease_lock():
        leases = wp._read_ledger()
        wp._write_ledger(leases)   # read-modify-write 왕복(모든 op 이 이 형태)
        rewritten = json.loads(wp.LEASES_FILE.read_text(encoding="utf-8"))
    entry = rewritten["leases"][0]
    assert entry["task"] == "myjob"           # 미지 키 생존(구 엔진이 op 해도 안 날아감)
    assert entry["git"]["head"] == "def"      # git 필드 생존


def test_from_dict_old_ledger_without_git_round_trips_lossless(wp):
    """하위호환 — git 필드 없는 구 장부(T-0072 이후·7 canonical) 로드·재기록 무손실(`git: null` 미삽입·T-0350)."""
    old = {"slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
           "started": "t", "state": "leased", "test_cmd": None}
    lease = wp.Lease.from_dict(old)
    assert lease.git is None
    d = lease.to_dict()
    assert "git" not in d      # git=None 이면 키 자체를 안 넣는다(구 장부 왕복 byte-무손실)
    assert d == old            # 추가/손실 0


def test_read_ledger_old_file_without_git_rewrite_lossless(wp):
    """파일 레벨 하위호환 — git 없는 구 장부 *파일* 을 read→write 왕복해도 git 키가 안 생긴다(T-0350)."""
    ledger = {"leases": [{"slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
                          "started": "t", "state": "leased", "test_cmd": None}]}
    wp.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    wp.LEASES_FILE.write_text(json.dumps(ledger), encoding="utf-8")
    with wp._lease_lock():
        wp._write_ledger(wp._read_ledger())
        rewritten = json.loads(wp.LEASES_FILE.read_text(encoding="utf-8"))
    assert "git" not in rewritten["leases"][0]


def test_lease_git_field_round_trip_including_unknown_subkeys(wp):
    """git 필드 자체가 to_dict/from_dict 왕복 보존된다 — 미지 *서브키* 까지 raw dict blob 으로 무손실(T-0350)."""
    git = {"base": {"branch": "main", "commit": "c"}, "branch": "A_1", "head": "h",
           "submodules": [{"path": "v", "pin": "p"}], "recorded_at": "t",
           "future_subkey": 1}     # git 안의 미지 서브키도 blob 으로 살아남는다
    lease = wp.Lease(slot="work/A_1", repo="A", session="me", pid=1, started="t",
                     state="leased", git=git)
    restored = wp.Lease.from_dict(lease.to_dict())
    assert restored.git == git
    assert restored == lease       # __eq__(to_dict 동등)이 git 을 포함


def test_from_dict_still_drops_legacy_top_level_branch_with_extra_preservation(wp):
    """extra 보존을 심어도 legacy 최상위 `branch` 는 여전히 드롭한다(T-0072 무시 유지·git 안 branch 서브키와 별개)."""
    payload = {"slot": "work/A_1", "repo": "A", "branch": "stale-copy", "session": "me",
               "pid": 7, "started": "t", "state": "leased", "task": "myjob"}
    lease = wp.Lease.from_dict(payload)
    d = lease.to_dict()
    assert "branch" not in d                 # legacy 최상위 branch = 드롭(extra 로도 안 실림)
    assert "branch" not in lease.extra
    assert d["task"] == "myjob"               # 다른 미지 키는 보존


# ════════════════════════════════════════════════════════════════════════
# git 스냅 write 프리미티브 — _snapshot_slot_git / record_git_snapshot / bind·alloc·create 배선 (T-0350)
# ════════════════════════════════════════════════════════════════════════


def test_snapshot_slot_git_records_branch_head_submodules(wp):
    """_snapshot_slot_git — live branch(symbolic-ref)·head(rev-parse)·submodule pin 을 스냅한다."""
    git = FakeGit(head="a5-pay", head_sha=_NEW_SHA,
                  submodule_status_out=" 47da353aa vendor/lib\n")
    snap = wp._snapshot_slot_git("work/A_1", git_runner=git)
    assert snap["branch"] == "a5-pay"
    assert snap["head"] == _NEW_SHA
    assert snap["submodules"] == [{"path": "vendor/lib", "pin": "47da353aa"}]
    assert snap["recorded_at"]          # ISO 타임스탬프 존재
    assert "base" not in snap           # base 는 스냅이 안 넣는다(호출부가 결정)


def test_snapshot_slot_git_absent_slot_returns_none(wp):
    """실경로(runner 미주입)에서 슬롯 worktree 부재면 None(스냅 불가·기존 git 유지 위임)."""
    assert wp._snapshot_slot_git("work/A_404", git_runner=None) is None


def test_snapshot_submodule_pins_strips_flag(wp):
    """`submodule status` 선두 flag(공백/+/-/U)를 제거하고 sha 만 pin 으로 뽑는다."""
    git = FakeGit(submodule_status_out=" aaa111 vendor/clean\n+bbb222 vendor/drift\n-ccc333 vendor/uninit\n")
    pins = wp._snapshot_submodule_pins(git)
    assert pins == [{"path": "vendor/clean", "pin": "aaa111"},
                    {"path": "vendor/drift", "pin": "bbb222"},
                    {"path": "vendor/uninit", "pin": "ccc333"}]


def test_record_git_snapshot_writes_to_ledger(wp):
    """record_git_snapshot(standalone·핸드오프) — 슬롯 git 을 기록하고 장부에 영속한다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = FakeGit(head="a1", head_sha=_NEW_SHA)
    lease = wp.record_git_snapshot("work/A_1", git_runner=git)
    assert lease.git["branch"] == "a1"
    assert lease.git["head"] == _NEW_SHA
    assert wp.list_leases()[0].git["head"] == _NEW_SHA     # 장부 영속


def test_record_git_snapshot_base_branch_sets_base(wp):
    """base_branch 주면 base 를 새로 기록(commit=방금 스냅한 head·create/set-base 경로)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = FakeGit(head="A_1", head_sha=_NEW_SHA)
    lease = wp.record_git_snapshot("work/A_1", base_branch="develop", git_runner=git)
    assert lease.git["base"] == {"branch": "develop", "commit": _NEW_SHA}


def test_record_git_snapshot_preserves_existing_base(wp):
    """base_branch 미지정(alloc/bind arrival)이면 기존 base 를 보존하고 head 만 갱신(rebase 로만 변경·결정 ⑨)."""
    _seed_git_lease(wp, base={"branch": "main", "commit": _OLD_SHA}, head=_OLD_SHA)
    git = FakeGit(head="a1", head_sha=_NEW_SHA)
    lease = wp.record_git_snapshot("work/A_1", git_runner=git)
    assert lease.git["base"] == {"branch": "main", "commit": _OLD_SHA}   # base 보존
    assert lease.git["head"] == _NEW_SHA                                 # head 갱신


def test_record_git_snapshot_unknown_slot_returns_none(wp):
    """장부에 없는 슬롯 → None(무해)."""
    assert wp.record_git_snapshot("work/A_9", git_runner=FakeGit()) is None


# ── read_lease + record CLI (0단계 diverged 정당 판단 시 명시 재동기·T-0391) ─────


def test_read_lease_returns_matching_lease_or_none(wp):
    """read_lease — 슬롯 lease 조회(순수 장부 read·record_git_snapshot 짝)·미등록은 None (T-0391)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    lease = wp.read_lease("work/A_1")
    assert lease is not None and lease.slot == "work/A_1"
    assert wp.read_lease("work/A_9") is None


def test_cmd_record_success_reports_updated_snapshot(wp, monkeypatch, capsys):
    """record CLI — 스냅 갱신 성공 시 재기록 branch/head surface·rc 0 (감지=기계·해소=사용자·T-0391)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    before = type("_L", (), {"git": {"branch": "old", "head": "aaaaaaaa"}})()
    after = type("_L", (), {"git": {"branch": "v1.3.3", "head": "bbbbbbbbbbbb"}})()
    monkeypatch.setattr(wp, "read_lease", lambda slot: before)
    monkeypatch.setattr(wp, "record_git_snapshot", lambda slot: after)
    rc = wp.main(["record", "A_1"])   # 접두 생략도 _normalize_slot 이 work/ 붙임.
    assert rc == 0
    out = capsys.readouterr().out
    assert "도착 스냅 재기록" in out and "v1.3.3" in out


def test_cmd_record_ledger_missing_slot_fails(wp, monkeypatch, capsys):
    """record CLI — 장부 미등록(record None)이면 rc 1 명시 실패(silent 무변경 방지)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    monkeypatch.setattr(wp, "read_lease", lambda slot: None)
    monkeypatch.setattr(wp, "record_git_snapshot", lambda slot: None)
    rc = wp.main(["record", "A_1"])
    assert rc == 1
    assert "리스 장부에 없다" in capsys.readouterr().err


def test_cmd_record_no_change_fails_loud(wp, monkeypatch, capsys):
    """record CLI — 스냅 불가로 before==after(무변경)면 rc 1(성공 위장 금지·기존 git 보존 인지)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    same = {"branch": "v1.3.3", "head": "bbbbbbbb"}
    monkeypatch.setattr(wp, "read_lease", lambda slot: type("_L", (), {"git": same})())
    monkeypatch.setattr(wp, "record_git_snapshot", lambda slot: type("_L", (), {"git": same})())
    rc = wp.main(["record", "A_1"])
    assert rc == 1
    assert "스냅할 수 없다" in capsys.readouterr().err


def test_cmd_record_bad_slot_rejected(wp, capsys):
    """record CLI — traversal/부적격 슬롯 형식은 rc 1(슬롯 경계 보호·_normalize_slot)."""
    rc = wp.main(["record", "../evil"])
    assert rc == 1
    assert "형식 오류" in capsys.readouterr().err


def test_alloc_records_arrival_git_snapshot(wp):
    """alloc(idle 리스 경로) 이 arrival git 스냅을 기록한다(부트스트랩 bind/alloc 시 스냅·interface)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"))
    git = FakeGit(head="a1", head_sha=_NEW_SHA)
    lease = wp.alloc("A", session="new", git_runner=git)
    assert lease.git is not None
    assert lease.git["branch"] == "a1" and lease.git["head"] == _NEW_SHA
    assert wp.list_leases()[0].git["head"] == _NEW_SHA


def test_bind_slot_records_arrival_git_snapshot(wp):
    """bind_slot 이 arrival git 스냅을 additive 로 기록한다(git_runner 주입 경로)."""
    git = FakeGit(head="A_1", head_sha=_NEW_SHA)
    lease = wp.bind_slot("work/A_1", "A", "A_1", git_runner=git)
    assert lease.git["branch"] == "A_1" and lease.git["head"] == _NEW_SHA


def test_bind_slot_without_runner_absent_slot_is_noop_snapshot(wp):
    """git_runner 미주입 + 슬롯 worktree 부재(hermetic) → 스냅 fail-soft no-op(git=None·bind 는 성공)."""
    lease = wp.bind_slot("work/A_1", "A", "A_1")
    assert lease.session == "A_1" and lease.git is None   # bind 성공·git 미기록(부재 슬롯)


def test_create_slot_records_git_base_when_base_given(wp):
    """create_slot(base=) → git.base 를 기록한다(base 를 아는 유일 지점·commit=fresh 슬롯 tip)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit(head="A_1", head_sha=_NEW_SHA)   # 파생 슬롯은 브랜치 A_1 (head 모델)
    lease = wp.create_slot("A", base="develop", session="me", git_runner=git)
    assert lease.git["base"] == {"branch": "develop", "commit": _NEW_SHA}
    assert lease.git["head"] == _NEW_SHA


def test_create_slot_without_base_leaves_base_unrecorded(wp):
    """create_slot(base 미지정) → git 스냅은 있으나 base 미기록(drift 감지는 set-base 후·결정 ⑪)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit(head="a1", head_sha=_NEW_SHA)
    lease = wp.create_slot("A", branch="a1", session="me", git_runner=git)
    assert lease.git is not None
    assert "base" not in lease.git       # base 미기록(자동 추론 금지)


def test_release_clears_git_snapshot(wp):
    """release → idle 전이 시 git 을 정리(None)한다 — idle 슬롯은 활성 git 기대가 없다(interface: release 시 정리)."""
    _seed_git_lease(wp, head=_OLD_SHA)
    lease = wp.release("work/A_1")   # slot 경로 부재(hermetic) → clean 경로 → idle
    assert lease.state == "idle"
    assert lease.git is None
    assert wp.list_leases()[0].git is None


def test_force_release_clears_git_snapshot(wp):
    """force_release 백스톱도 git 을 정리(None)한다."""
    _seed_git_lease(wp, head=_OLD_SHA)
    lease = wp.force_release("work/A_1")
    assert lease.state == "idle" and lease.git is None


# ════════════════════════════════════════════════════════════════════════
# merge-base --is-ancestor 판정 헬퍼 + head relation (㉒ crash 후 재개 완화 · T-0350)
# ════════════════════════════════════════════════════════════════════════


def test_is_ancestor_rc0_true_rc1_false(wp):
    """_is_ancestor — `merge-base --is-ancestor` rc0=조상(True)·rc1=아님(False)."""
    assert wp._is_ancestor(FakeGit(ancestor_ok=True), _OLD_SHA, _NEW_SHA) is True
    assert wp._is_ancestor(FakeGit(ancestor_ok=False), _OLD_SHA, _NEW_SHA) is False


def test_head_relation_match_same_head(wp):
    assert wp._head_relation("a1", _OLD_SHA, "a1", _OLD_SHA, git_runner=None) == wp.HEAD_MATCH


def test_head_relation_unknown_when_head_missing(wp):
    assert wp._head_relation("a1", None, "a1", _OLD_SHA, git_runner=None) == wp.HEAD_UNKNOWN
    assert wp._head_relation("a1", _OLD_SHA, "a1", None, git_runner=None) == wp.HEAD_UNKNOWN


def test_head_relation_branch_change_is_diverged(wp):
    """브랜치 변경 = 사고(FAIL-LOUD) — head 후손 여부 안 봄."""
    assert wp._head_relation("a1", _OLD_SHA, "a2", _NEW_SHA,
                             git_runner=FakeGit(ancestor_ok=True)) == wp.HEAD_DIVERGED


def test_head_relation_descendant_is_notice(wp):
    """같은 branch·head 다름·live 가 기록 head 의 후손 → descendant(crash 후 재개·통과·㉒)."""
    assert wp._head_relation("a1", _OLD_SHA, "a1", _NEW_SHA,
                             git_runner=FakeGit(ancestor_ok=True)) == wp.HEAD_DESCENDANT


def test_head_relation_nonancestor_is_diverged(wp):
    """같은 branch·head 다름·비후손(리셋·되감기·divergent) → diverged(FAIL-LOUD)."""
    assert wp._head_relation("a1", _OLD_SHA, "a1", _NEW_SHA,
                             git_runner=FakeGit(ancestor_ok=False)) == wp.HEAD_DIVERGED


# ════════════════════════════════════════════════════════════════════════
# compare 프리미티브 — 기록(기대) vs live (0단계 소비·T-0351 · T-0350)
# ════════════════════════════════════════════════════════════════════════


def test_compare_slot_git_unrecorded_when_no_git_field(wp):
    """git 미기록(구 슬롯) → unrecorded(drift 감지 비활성·ok/fail 아닌 별도 상태·결정 ⑪)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="leased"))   # git=None
    res = wp.compare_slot_git("work/A_1", git_runner=FakeGit(head="a1"))
    assert res.unrecorded is True
    assert res.ok is False and res.fail_loud is False


def test_compare_slot_git_match_passes(wp):
    """기록 branch@head == live → match·통과(ok·not fail)."""
    _seed_git_lease(wp, branch="a1", head=_OLD_SHA)
    res = wp.compare_slot_git("work/A_1", git_runner=FakeGit(head="a1", head_sha=_OLD_SHA))
    assert res.branch_match is True and res.head_relation == wp.HEAD_MATCH
    assert res.ok is True and res.fail_loud is False


def test_compare_slot_git_head_descendant_is_notice_not_fail(wp):
    """live 가 기록 head 의 후손(같은 branch) → descendant = 통과(crash 후 재개를 경보로 안 만든다·㉒)."""
    _seed_git_lease(wp, branch="a1", head=_OLD_SHA)
    res = wp.compare_slot_git(
        "work/A_1", git_runner=FakeGit(head="a1", head_sha=_NEW_SHA, ancestor_ok=True))
    assert res.head_relation == wp.HEAD_DESCENDANT
    assert res.fail_loud is False and res.ok is True


def test_compare_slot_git_branch_change_is_fail_loud(wp):
    """브랜치 변경 = 사고 → FAIL-LOUD."""
    _seed_git_lease(wp, branch="a1", head=_OLD_SHA)
    res = wp.compare_slot_git("work/A_1", git_runner=FakeGit(head="a2", head_sha=_OLD_SHA))
    assert res.branch_match is False
    assert res.fail_loud is True and res.ok is False


def test_compare_slot_git_head_nonancestor_is_fail_loud(wp):
    """같은 branch·head 비후손(리셋·되감기) → diverged = FAIL-LOUD."""
    _seed_git_lease(wp, branch="a1", head=_OLD_SHA)
    res = wp.compare_slot_git(
        "work/A_1", git_runner=FakeGit(head="a1", head_sha=_NEW_SHA, ancestor_ok=False))
    assert res.head_relation == wp.HEAD_DIVERGED
    assert res.fail_loud is True


def test_compare_slot_git_submodule_drift_is_warning_not_fail(wp):
    """기록 pin ≠ live pin → submodule_drift 목록에 오르되 fail_loud 는 아니다(경고 축·T-0275/0276 대칭)."""
    _seed_git_lease(wp, branch="a1", head=_OLD_SHA,
                    submodules=[{"path": "vendor/lib", "pin": "OLDPIN"}])
    res = wp.compare_slot_git(
        "work/A_1",
        git_runner=FakeGit(head="a1", head_sha=_OLD_SHA,
                           submodule_status_out=" NEWPIN vendor/lib\n"))
    assert res.submodule_drift == ["vendor/lib"]
    assert res.fail_loud is False    # submodule drift 는 경고(branch/head 만 FAIL 축)


def test_compare_slot_git_no_submodule_drift_when_pins_match(wp):
    """기록 pin == live pin → drift 없음(sensitivity — drift 판정이 공허하지 않다)."""
    _seed_git_lease(wp, branch="a1", head=_OLD_SHA,
                    submodules=[{"path": "vendor/lib", "pin": "SAMEPIN"}])
    res = wp.compare_slot_git(
        "work/A_1",
        git_runner=FakeGit(head="a1", head_sha=_OLD_SHA,
                           submodule_status_out=" SAMEPIN vendor/lib\n"))
    assert res.submodule_drift == []


def test_compare_slot_git_tag_collision_branch_is_not_fail_loud(wp):
    """동명 태그+브랜치 — live current_branch 가 full ref 로 순수명 `v1.3.0` 을 주므로 match·통과 (T-0377).

    릴리즈가 `v1.3.0` 브랜치를 그대로 `v1.3.0` 태그로 찍은 슬롯: current_branch 가 `symbolic-ref
    HEAD`(full ref)로 태그와 무관하게 순수명 `v1.3.0` 을 돌려줘 장부 기록(`v1.3.0`)과 일치 → 가짜
    diverged FAIL-LOUD 미발화(PM 76 0단계 차단 회귀). 정규화는 current_branch 단일 지점에서만
    일어나고 compare 는 손대지 않는다(비교측 fallback 없음).
    """
    _seed_git_lease(wp, branch="v1.3.0", head=_OLD_SHA)
    # FakeGit(head="v1.3.0") → symbolic-ref HEAD 가 refs/heads/v1.3.0 → current_branch "v1.3.0".
    res = wp.compare_slot_git(
        "work/A_1", git_runner=FakeGit(head="v1.3.0", head_sha=_OLD_SHA))
    assert res.branch_match is True and res.head_relation == wp.HEAD_MATCH
    assert res.fail_loud is False and res.ok is True


def test_compare_slot_git_polluted_recorded_heads_prefix_stays_mismatch(wp):
    """오염 장부(`heads/v1.3.0` 기록) vs live 순수명(`v1.3.0`) → branch mismatch 유지·FAIL-LOUD (T-0377).

    compare 는 비교측 fallback 정규화를 하지 않는다 — recorded 의 `heads/v1.3.0` 은 live `v1.3.0`
    과 진짜 다른 이름일 수 있어(진짜 `heads/` 브랜치) mismatch 로 남아야 한다. 정규화 배포 전
    오염된 장부는 비교측이 조용히 삼키지 않고, PM 이 `record_git_snapshot` 일회 재기록으로 원천
    정정한다([[prefer-data-migration-over-fallback]] — fallback 누적 금지·codex must-fix).
    """
    _seed_git_lease(wp, branch="heads/v1.3.0", head=_OLD_SHA)   # 정규화 전 오염된 recorded.
    res = wp.compare_slot_git("work/A_1", git_runner=FakeGit(head="v1.3.0", head_sha=_OLD_SHA))
    assert res.branch_match is False
    assert res.fail_loud is True and res.ok is False


# ════════════════════════════════════════════════════════════════════════
# reclaim_stale git 보존 (crash-resume 계약·release/force_release 와 의도적 비대칭 · T-0350 dual-review)
# ════════════════════════════════════════════════════════════════════════


def test_reclaim_stale_preserves_git_for_crash_resume(wp):
    """reclaim_stale 은 git 을 **보존**한다 — release/force_release(정리)와 의도적 비대칭(crash-resume 계약·T-0350).

    죽은 pid 의 leased 슬롯을 회수하면 idle 로 전이하되 `lease.git`(base/head)은 살아야, 다음
    부트스트랩 0단계 compare 가 live 를 "내 crash 커밋의 후손(descendant=notice·정상 재개)" vs
    "외부 개입(diverged=FAIL-LOUD)"으로 가른다. 지우면 unrecorded 로 무력화 → crash-resume 이
    조용히 깨진다. 여기 `git=None`(release 식 정리)을 넣는 '일관성 fix' 회귀를 하드 차단한다
    (codex must-fix override·reviewer 채택·PM 판정).
    """
    _seed_git_lease(wp, base={"branch": "main", "commit": _OLD_SHA}, head=_OLD_SHA,
                    session="dead", state="leased", pid=999999)   # dead pid → reclaim 대상
    reclaimed = wp.reclaim_stale(git_runner=FakeGit())
    assert reclaimed == ["work/A_1"]
    lease = wp.list_leases()[0]
    assert lease.state == "idle"                     # 회수됨(idle 전이)
    assert lease.git is not None                     # ← git 보존(정리 안 함·핵심 회귀 차단)
    assert lease.git["base"] == {"branch": "main", "commit": _OLD_SHA}
    assert lease.git["head"] == _OLD_SHA
    # crash-resume 취지: 보존 head(H1) vs live 후손(H2·같은 branch) → descendant(notice·not fail).
    res = wp.compare_slot_git(
        "work/A_1", git_runner=FakeGit(head="a1", head_sha=_NEW_SHA, ancestor_ok=True))
    assert res.head_relation == wp.HEAD_DESCENDANT and res.fail_loud is False


def test_apply_git_snapshot_preserves_existing_git_when_snap_fails(wp):
    """_apply_git_snapshot — 스냅 불가(슬롯 부재 → _snapshot None)면 기존 git 을 clobber 안 하고 보존(T-0350·silent 손실 방지)."""
    existing = {"base": {"branch": "main", "commit": _OLD_SHA}, "branch": "a1",
                "head": _OLD_SHA, "submodules": [], "recorded_at": "t"}
    lease = wp.Lease(slot="work/A_404", repo="A", session="s", pid=1, started="t",
                     state="leased", git=dict(existing))
    # git_runner 미주입 + 슬롯 worktree 부재 → _snapshot_slot_git None → 기존 git 유지(no-op).
    wp._apply_git_snapshot(lease, git_runner=None)
    assert lease.git == existing


# ════════════════════════════════════════════════════════════════════════
# set-base / rebase 기준-gate 계약 / status (기준점 미기록 flow — 자동 추론 금지·T-0352)
# ════════════════════════════════════════════════════════════════════════

_BASE_TIP = "c" * 40      # set-base 로 지정한 base 브랜치 tip(슬롯 HEAD 와 구별되는 값).


class _BaseGit:
    """set-base/status/rebase-gate 커스텀 runner — FakeGit 이 안 다루는 rev-parse verify·rev-list count 모델.

    `tips` = ref→sha(`rev-parse --verify <ref>^{commit}` 해소·미등록 ref=rc128), `behind` = base→N
    (`rev-list --count HEAD..<base>`·미등록=rc128), `head`/`head_sha`/`subs` = 스냅 필드. 슬롯 tip
    (`rev-parse HEAD`)과 base 브랜치 tip(`--verify`)을 **다른 sha** 로 두어 set-base 가 base.commit 을
    슬롯 HEAD 가 아니라 브랜치 tip 으로 기록하는지 구별한다."""

    def __init__(self, *, tips=None, behind=None, head="feat", head_sha=_NEW_SHA, subs=""):
        self.tips = tips or {}
        self.behind = behind or {}
        self.head = head
        self.head_sha = head_sha
        self.subs = subs
        self.calls: list[list] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["rev-parse", "--verify"]:
            ref = argv[-1]
            key = ref[: -len("^{commit}")] if ref.endswith("^{commit}") else ref
            sha = self.tips.get(key)
            return (0, sha + "\n") if sha else (128, "fatal: bad revision\n")
        if argv == ["rev-parse", "HEAD"]:
            return (0, self.head_sha + "\n")
        if argv[:2] == ["rev-list", "--count"]:
            spec = argv[-1]                              # "HEAD..<base>"
            base = spec.split("..", 1)[1] if ".." in spec else spec
            n = self.behind.get(base)
            return (0, f"{n}\n") if n is not None else (128, "fatal: unknown revision\n")
        if argv == ["symbolic-ref", "HEAD"]:
            # T-0377: full ref 반환 — current_branch 가 refs/heads/ 접두를 벗긴다.
            return (0, "refs/heads/" + self.head + "\n") if self.head else (1, "detached\n")
        if argv == ["submodule", "status"]:
            return (0, self.subs)
        return (0, "")

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


# ── _parse_base_ref / _resolve_base_commit ──────────────────────────────────


def test_parse_base_ref_splits_on_first_at(wp):
    """`<branch>[@<commit>]` — 첫 @ 에서 분해(브랜치 only·@commit·빈 인자)."""
    assert wp._parse_base_ref("origin/main") == ("origin/main", None)
    assert wp._parse_base_ref("origin/main@df10dc6") == ("origin/main", "df10dc6")
    assert wp._parse_base_ref("main@") == ("main", None)   # 빈 commit → None(브랜치 tip)
    assert wp._parse_base_ref("") == ("", None)


def test_resolve_base_commit_uses_rev_parse_verify(wp):
    """_resolve_base_commit — `rev-parse --verify <ref>^{commit}` 로 브랜치 tip sha 를 해소."""
    git = _BaseGit(tips={"origin/main": _BASE_TIP})
    assert wp._resolve_base_commit("work/A_1", "origin/main", git_runner=git) == _BASE_TIP
    assert git.did("rev-parse", "--verify", "origin/main^{commit}")


def test_resolve_base_commit_unresolvable_ref_returns_none(wp):
    """해소 불가 ref(rc≠0)면 None(fail-soft → 상위가 slot HEAD 폴백 또는 `-`)."""
    git = _BaseGit(tips={})
    assert wp._resolve_base_commit("work/A_1", "nope", git_runner=git) is None


# ── _apply_git_snapshot base_commit (T-0352 확장·하위호환) ────────────────────


def test_apply_git_snapshot_explicit_base_commit_used(wp):
    """base_commit 명시(set-base) → base.commit 이 그 값(슬롯 HEAD 가 아님)."""
    lease = wp.Lease(slot="work/A_1", repo="A", session="s", pid=1, started="t", state="leased")
    git = FakeGit(head="feat", head_sha=_NEW_SHA)
    wp._apply_git_snapshot(lease, base_branch="origin/main", base_commit=_BASE_TIP, git_runner=git)
    assert lease.git["base"] == {"branch": "origin/main", "commit": _BASE_TIP}   # 슬롯 HEAD(_NEW_SHA) 아님


def test_apply_git_snapshot_base_commit_none_falls_back_to_head(wp):
    """base_commit None(create 기존 거동) → base.commit = 방금 스냅한 head(하위호환·회귀 차단)."""
    lease = wp.Lease(slot="work/A_1", repo="A", session="s", pid=1, started="t", state="leased")
    git = FakeGit(head="A_1", head_sha=_NEW_SHA)
    wp._apply_git_snapshot(lease, base_branch="develop", git_runner=git)   # base_commit 미지정
    assert lease.git["base"] == {"branch": "develop", "commit": _NEW_SHA}


# ── set_base (사용자 명시 base 기록·자동 추론 금지) ──────────────────────────


def test_set_base_records_branch_tip_as_commit(wp):
    """set_base — commit 생략 시 base.commit = 그 브랜치 tip(rev-parse verify·슬롯 HEAD 아님)·장부 영속."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={"origin/main": _BASE_TIP}, head="feat", head_sha=_NEW_SHA)
    lease = wp.set_base("work/A_1", "origin/main", git_runner=git)
    assert lease.git["base"] == {"branch": "origin/main", "commit": _BASE_TIP}
    assert wp.list_leases()[0].git["base"]["commit"] == _BASE_TIP     # 장부 영속


def test_set_base_explicit_commit_pins_that_commit(wp):
    """set_base(commit=명시 `@<commit>`) → 그 커밋을 base.commit 으로 verify 후 기록."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={"df10dc6": _BASE_TIP}, head="feat", head_sha=_NEW_SHA)
    lease = wp.set_base("work/A_1", "origin/main", commit="df10dc6", git_runner=git)
    assert lease.git["base"] == {"branch": "origin/main", "commit": _BASE_TIP}


def test_set_base_only_records_user_branch_no_inference(wp):
    """set_base 는 **사용자가 준 브랜치만** base.branch 로 기록한다(자동 추론 0·결정 ⑪).

    merge-base·origin/main 추측을 절대 안 한다 — base.branch 는 인자 그대로."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={"develop": _BASE_TIP})
    lease = wp.set_base("work/A_1", "develop", git_runner=git)
    assert lease.git["base"]["branch"] == "develop"
    # merge-base 를 통한 추론 흔적이 없다(사용자 지정만·verify 만 부른다).
    assert not any(c[:1] == ["merge-base"] for c in git.calls)


def test_set_base_unknown_slot_returns_none(wp):
    """장부에 없는 슬롯 → None(record_git_snapshot 위임·무해)."""
    assert wp.set_base("work/A_9", "origin/main", git_runner=_BaseGit(tips={"origin/main": _BASE_TIP})) is None


def test_set_base_records_live_branch_head_alongside_base(wp):
    """set_base 는 base 만이 아니라 live branch/head/submodule 스냅도 함께 기록(미기록 슬롯 초기화)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={"origin/main": _BASE_TIP}, head="feat-x", head_sha=_NEW_SHA,
                   subs=" aaa111 vendor/lib\n")
    lease = wp.set_base("work/A_1", "origin/main", git_runner=git)
    assert lease.git["branch"] == "feat-x" and lease.git["head"] == _NEW_SHA
    assert lease.git["submodules"] == [{"path": "vendor/lib", "pin": "aaa111"}]


# ── set_base FAIL-LOUD on unresolvable ref (codex must-fix ① — 조용히 틀린 base 차단) ──


def test_set_base_unresolvable_ref_fail_loud(wp):
    """set_base — base ref 해소 불가(오타·미fetch)면 `BaseRefUnresolvable` FAIL-LOUD·**기록 안 함**(must-fix ①).

    옛 코드는 slot HEAD 로 조용히 폴백해 `base=origin/typo@<슬롯HEAD>`(무관한 커밋)를 기록했고 drift
    감지가 garbage baseline 위에서 돌았다 — 이 티켓 중심 계약("조용히 틀린 base 차단") 위반. 이제 record
    이전에 거부한다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={}, head="feat", head_sha=_NEW_SHA)   # origin/typo 미해소
    with pytest.raises(wp.BaseRefUnresolvable) as exc:
        wp.set_base("work/A_1", "origin/typo", git_runner=git)
    assert exc.value.ref == "origin/typo"
    assert wp._read_recorded_base("work/A_1") is None          # 조용히 기록되지 않음(계약 못박음)


def test_set_base_explicit_bad_commit_fail_loud(wp):
    """set_base(@bad-commit) — 명시 커밋이 해소 안 되면 fail-loud(slot HEAD 폴백 금지·기록 안 함)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={"origin/main": _BASE_TIP})   # 'deadbad' 미등록
    with pytest.raises(wp.BaseRefUnresolvable):
        wp.set_base("work/A_1", "origin/main", commit="deadbad", git_runner=git)
    assert wp._read_recorded_base("work/A_1") is None


def test_set_base_valid_ref_still_records_after_failloud_guard(wp):
    """회귀 — 유효 ref 는 정상 기록(fail-loud 가드가 정상 경로를 막지 않는다)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={"origin/main": _BASE_TIP}, head="feat", head_sha=_NEW_SHA)
    lease = wp.set_base("work/A_1", "origin/main", git_runner=git)
    assert lease.git["base"] == {"branch": "origin/main", "commit": _BASE_TIP}


def test_apply_git_snapshot_create_path_slot_head_fallback_untouched(wp):
    """create 경로(base_commit=None→slot HEAD 폴백)는 must-fix ① 에 무손상 — fresh 슬롯 tip==base 정답.

    fail-loud 는 set_base 레벨에서만. `_apply_git_snapshot`/`record_git_snapshot` 의 base_commit=None
    폴백은 create(fresh 슬롯) 정당 하위호환이라 그대로 유지된다(회귀 차단)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = FakeGit(head="A_1", head_sha=_NEW_SHA)
    lease = wp.record_git_snapshot("work/A_1", base_branch="develop", git_runner=git)  # base_commit 미지정
    assert lease.git["base"] == {"branch": "develop", "commit": _NEW_SHA}   # slot HEAD 폴백 유지


# ── _read_recorded_base ──────────────────────────────────────────────────────


def test_read_recorded_base_returns_dict_when_recorded(wp):
    _seed_git_lease(wp, base={"branch": "origin/main", "commit": _BASE_TIP})
    assert wp._read_recorded_base("work/A_1") == {"branch": "origin/main", "commit": _BASE_TIP}


def test_read_recorded_base_none_when_unrecorded(wp):
    """git 필드가 있어도 base 서브키 없으면 None(미기록)."""
    _seed_git_lease(wp)   # base 미지정
    assert wp._read_recorded_base("work/A_1") is None


def test_read_recorded_base_none_when_no_git_field(wp):
    """구 슬롯(git 필드 자체 부재) → None."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    assert wp._read_recorded_base("work/A_1") is None


def test_read_recorded_base_unknown_slot_none(wp):
    assert wp._read_recorded_base("work/A_9") is None


# ── resolve_rebase_base — rebase 기준-gate 계약(본체 wave-2d) ─────────────────


def test_resolve_rebase_base_refuses_when_unrecorded_no_onto(wp):
    """기준 미기록 + --onto 없음 → RebaseBaseRequired 거부(기준 없이 rebase 불가·추론 금지·계약)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    with pytest.raises(wp.RebaseBaseRequired) as exc:
        wp.resolve_rebase_base("work/A_1")
    assert exc.value.slot == "work/A_1"
    assert "set-base" in str(exc.value)         # 해소 경로 안내


def test_resolve_rebase_base_returns_recorded_base_branch(wp):
    """기준 기록됨 + --onto 없음 → 기록된 base.branch 반환(그 최신으로 rebase)."""
    _seed_git_lease(wp, base={"branch": "origin/main", "commit": _BASE_TIP})
    assert wp.resolve_rebase_base("work/A_1") == "origin/main"


def test_resolve_rebase_base_onto_records_and_returns(wp):
    """--onto 명시 → 그 값을 base 로 **기록**(1회 해소)하고 반환(진행)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={"develop": _BASE_TIP})
    result = wp.resolve_rebase_base("work/A_1", onto="develop", git_runner=git)
    assert result == "develop"
    # 기록 확인 — 미기록이 --onto 1회로 해소됨.
    assert wp._read_recorded_base("work/A_1") == {"branch": "develop", "commit": _BASE_TIP}


def test_resolve_rebase_base_onto_overrides_existing_base(wp):
    """--onto 는 기존 기록 base 도 덮어 기록(사용자 명시 우선)."""
    _seed_git_lease(wp, base={"branch": "origin/main", "commit": _OLD_SHA})
    git = _BaseGit(tips={"release/2": _BASE_TIP})
    assert wp.resolve_rebase_base("work/A_1", onto="release/2", git_runner=git) == "release/2"
    assert wp._read_recorded_base("work/A_1")["branch"] == "release/2"


def test_resolve_rebase_base_onto_unresolvable_propagates(wp):
    """--onto 해소 불가 → `set_base` 의 `BaseRefUnresolvable` 전파 (must-fix ②·silent onto 반환 아님·기록 안 됨).

    옛 코드는 set_base 반환값을 무시하고 항상 onto 를 반환해, 해소 실패에도 gate 가 통과했다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(tips={})   # onto 미해소
    with pytest.raises(wp.BaseRefUnresolvable):
        wp.resolve_rebase_base("work/A_1", onto="origin/typo", git_runner=git)
    assert wp._read_recorded_base("work/A_1") is None   # 기록 안 됨(진행 계약 미충족 → 반환 없음)


def test_resolve_rebase_base_onto_unregistered_slot_raises(wp):
    """--onto 인데 슬롯이 장부 미등록 → `set_base` None → `RebaseBaseRequired` raise(silent onto 반환 금지·must-fix ②)."""
    git = _BaseGit(tips={"develop": _BASE_TIP})   # ref 는 해소되나 슬롯이 장부에 없음
    with pytest.raises(wp.RebaseBaseRequired):
        wp.resolve_rebase_base("work/A_9", onto="develop", git_runner=git)


# ── base_behind_count / slot_git_status (조회·미기록 N-behind `-`) ────────────


def test_base_behind_count_parses_rev_list(wp):
    """base_behind_count — `rev-list --count HEAD..<base>` 정수 파싱."""
    git = _BaseGit(behind={"origin/main": 3})
    assert wp.base_behind_count("work/A_1", "origin/main", git_runner=git) == 3


def test_base_behind_count_failsoft_none(wp):
    """rev-list 실패(미해소 ref·rc≠0) → None(계산 불가)."""
    git = _BaseGit(behind={})
    assert wp.base_behind_count("work/A_1", "origin/main", git_runner=git) is None


def test_slot_git_status_unrecorded_behind_is_none_with_reason(wp):
    """미기록 슬롯 → behind None + reason(미기록 — CLI `-` 표기·자동 추론 금지·결정 ⑪)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    git = _BaseGit(head="feat", head_sha=_NEW_SHA)
    st = wp.slot_git_status("work/A_1", git_runner=git)
    assert st["base"] is None
    assert st["behind"] is None
    assert "미기록" in st["behind_reason"]


def test_slot_git_status_recorded_computes_behind(wp):
    """기록 슬롯 → base.branch 대비 N behind 계산 + branch/head 표시."""
    _seed_git_lease(wp, base={"branch": "origin/main", "commit": _BASE_TIP})
    git = _BaseGit(behind={"origin/main": 5}, head="feat", head_sha=_NEW_SHA)
    st = wp.slot_git_status("work/A_1", git_runner=git)
    assert st["base"] == {"branch": "origin/main", "commit": _BASE_TIP}
    assert st["behind"] == 5 and st["behind_reason"] is None
    assert st["branch"] == "feat" and st["head"] == _NEW_SHA


def test_slot_git_status_recorded_but_base_unresolvable_behind_none(wp):
    """기록됐지만 base.branch 해소 실패(rev-list rc≠0) → behind None + reason(해소 실패·`-`)."""
    _seed_git_lease(wp, base={"branch": "origin/gone", "commit": _BASE_TIP})
    git = _BaseGit(behind={}, head="feat")   # origin/gone 미등록 → rc≠0
    st = wp.slot_git_status("work/A_1", git_runner=git)
    assert st["behind"] is None and "해소 실패" in st["behind_reason"]


# ── CLI (main) — set-base / status 진입 ──────────────────────────────────────


def _mk_slot_dir(wp, slot="work/A_1"):
    """tmp 슬롯 worktree 디렉토리를 만든다(실경로 CLI 가 slot_path.exists() 로 스냅 진입하도록)."""
    p = wp.slot_path(slot)
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_cli_set_base_records_and_reports(wp, proj, monkeypatch, capsys):
    """CLI `set-base <slot> <branch>` — 실경로 wiring 으로 base 기록 + rc 0 + stdout(장부 영속)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    _mk_slot_dir(wp)
    git = _BaseGit(tips={"origin/main": _BASE_TIP}, head="feat", head_sha=_NEW_SHA)
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: git)   # 실경로 → 커스텀 runner
    rc = wp.main(["set-base", "work/A_1", "origin/main"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "기준점 기록" in out and "origin/main" in out
    assert wp._read_recorded_base("work/A_1") == {"branch": "origin/main", "commit": _BASE_TIP}


def test_cli_set_base_prefix_omitted_slot_form(wp, proj, monkeypatch, capsys):
    """CLI set-base 는 접두 생략 `<repo>_<N>` 슬롯 형식도 정규화해 받는다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    _mk_slot_dir(wp)
    git = _BaseGit(tips={"origin/main": _BASE_TIP})
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: git)
    assert wp.main(["set-base", "A_1", "origin/main"]) == 0        # 접두 생략


def test_cli_set_base_unknown_slot_rc1(wp, proj, monkeypatch, capsys):
    """CLI set-base — worktree 는 있으나 장부 미등록(orphan) 슬롯이면 rc 1 + 안내(등록 슬롯에만 기록).

    ref 는 해소되게(worktree 존재+monkeypatch runner) 두고 장부에만 없게 해 "장부에 없다" 경로를 태운다
    (must-fix ① fail-loud 는 ref/worktree 해소 실패이므로 그 앞단을 통과시켜야 이 분기 도달)."""
    _mk_slot_dir(wp, "work/A_9")   # worktree 존재 → ref 해소 통과 · 장부엔 미등록
    git = _BaseGit(tips={"origin/main": _BASE_TIP})
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: git)
    rc = wp.main(["set-base", "work/A_9", "origin/main"])
    assert rc == 1
    assert "장부에 없다" in capsys.readouterr().err


def test_cli_set_base_bad_slot_format_rc1(wp, capsys):
    """CLI set-base — traversal/부적격 슬롯 형식은 rc 1 거부(슬롯 경계 보호·_normalize_slot)."""
    rc = wp.main(["set-base", "../evil", "origin/main"])
    assert rc == 1
    assert "형식 오류" in capsys.readouterr().err


def test_cli_set_base_absent_worktree_fail_loud_rc1(wp, capsys):
    """CLI set-base — 슬롯 worktree 부재면 ref 해소 불가로 fail-loud rc 1·**기록 안 됨**(must-fix ①·silent 오기록 차단)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    # 슬롯 디렉토리 미생성 → _resolve_base_commit None → BaseRefUnresolvable → rc 1.
    rc = wp.main(["set-base", "work/A_1", "origin/main"])
    assert rc == 1
    assert "해소할 수 없습니다" in capsys.readouterr().err
    assert wp._read_recorded_base("work/A_1") is None      # 조용히 기록되지 않음


def test_cli_set_base_unresolvable_ref_rc1(wp, proj, monkeypatch, capsys):
    """CLI set-base — worktree 는 있으나 ref 해소 불가(오타)면 rc 1 fail-loud·**기록 안 됨**(must-fix ①)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    _mk_slot_dir(wp)
    git = _BaseGit(tips={})   # 어떤 ref 도 해소 안 됨
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: git)
    rc = wp.main(["set-base", "work/A_1", "origin/typo"])
    assert rc == 1
    assert "해소할 수 없습니다" in capsys.readouterr().err
    assert wp._read_recorded_base("work/A_1") is None


def test_cli_status_unrecorded_shows_dash(wp, capsys):
    """CLI `status <slot>` — 미기록 슬롯이면 "base 대비 behind" 가 `-`(계산 불가·이유·DoD)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="s", state="leased"))
    rc = wp.main(["status", "work/A_1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "base 대비 behind: -" in out and "미기록" in out


def test_cli_status_recorded_shows_n(wp, proj, monkeypatch, capsys):
    """CLI status — 기록 슬롯이면 base 대비 N 커밋 표기(실경로 wiring)."""
    _seed_git_lease(wp, base={"branch": "origin/main", "commit": _BASE_TIP})
    _mk_slot_dir(wp)
    git = _BaseGit(behind={"origin/main": 2}, head="feat", head_sha=_NEW_SHA)
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: git)
    rc = wp.main(["status", "work/A_1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "base 대비 behind: 2 커밋" in out
    assert "origin/main" in out


def test_cli_status_no_arg_resolves_session_slot(wp, monkeypatch, capsys):
    """CLI `status`(무인자) → cwd/세션 leased 슬롯으로 해소(`_resolve_current_slot(None)`·suggestion ③).

    위치인자 생략 경로(무인자=내 슬롯)를 직접 커버한다 — 세션 해소를 고정하고 그 슬롯이 조회됨을 확인."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="A_1", state="leased"))
    monkeypatch.setattr(wp, "_default_session", lambda: "A_1")   # 세션 해소 고정
    monkeypatch.setattr(wp, "_slot_from_cwd", lambda: None)      # cwd 유입 없음 → 세션 경로
    rc = wp.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "work/A_1" in out and "미기록" in out    # 세션 슬롯 해소 + 미기록 base(`-`)


# ════════════════════════════════════════════════════════════════════════
# top-level `tasks` 컬렉션 + task 바인딩 (⑥·㉑·T-0353)
# ════════════════════════════════════════════════════════════════════════


def test_task_serialization_round_trip(wp):
    """Task 가 to_dict/from_dict 왕복 보존(prefix·pid·started + 미지 키 extra·Lease 동형)."""
    t = wp.Task(name="job1", prefix="PAY", pid=42, started="t",
                extra={"future": {"k": 1}})
    d = t.to_dict()
    assert d["name"] == "job1" and d["prefix"] == "PAY" and d["pid"] == 42
    assert d["future"] == {"k": 1}          # 미지 키 재방출
    restored = wp.Task.from_dict(d)
    assert restored == t                    # __eq__ = to_dict 동등


def test_task_default_prefix_none(wp):
    """prefix 미지정 = None(기본 없음·①ⓑ)."""
    t = wp.Task(name="job1", pid=1, started="t")
    assert t.prefix is None
    assert t.to_dict()["prefix"] is None


def test_write_ledger_preserves_sibling_tasks_collection(wp):
    """`_write_ledger`(leases 만 쓰는 모든 생명주기 op)가 형제 top-level `tasks` 를 보존한다.

    옛 `{"leases": [...]}` 통짜 쓰기는 tasks 를 조용히 드롭했다(silent drop) — top-level round-trip
    (T-0353)이 이를 닫는다. alloc/release/reclaim 등 어떤 leases op 도 tasks 를 안 날려야 한다."""
    ledger = {
        "leases": [{"slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
                    "started": "t", "state": "leased", "test_cmd": None}],
        "tasks": [{"name": "job1", "prefix": "PAY", "pid": 111, "started": "t"}],
        "future_top_level": {"x": 1},   # 미지 최상위 키도 보존돼야 한다.
    }
    wp.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    wp.LEASES_FILE.write_text(json.dumps(ledger), encoding="utf-8")
    # leases-only 재기록(모든 리스 op 이 이 형태) — tasks·미지 키가 살아남아야.
    with wp._lease_lock():
        wp._write_ledger(wp._read_ledger())
        rewritten = json.loads(wp.LEASES_FILE.read_text(encoding="utf-8"))
    assert rewritten["tasks"][0]["name"] == "job1"
    assert rewritten["tasks"][0]["prefix"] == "PAY"
    assert rewritten["future_top_level"] == {"x": 1}
    assert rewritten["leases"][0]["slot"] == "work/A_1"


def test_write_tasks_preserves_sibling_leases_collection(wp):
    """대칭 — `_write_tasks` 가 형제 `leases` 를 보존한다(둘은 같은 파일·같은 락·독립 컬렉션)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="me", state="leased"))
    with wp._lease_lock():
        wp._write_tasks([wp.Task(name="job1", pid=1, started="t")])
        data = json.loads(wp.LEASES_FILE.read_text(encoding="utf-8"))
    assert data["leases"][0]["slot"] == "work/A_1"   # 리스 생존
    assert data["tasks"][0]["name"] == "job1"


def test_bind_task_creates_new_record_with_zero_workspaces(wp):
    """`--task` 신규 — 장부에 없던 task 를 생성(prefix=None·pid=내 pid·슬롯 0개 시작 가능·⑥)."""
    record, action, reclaimed_from = wp.bind_task("newjob")
    assert action == "created" and reclaimed_from is None
    assert record.name == "newjob" and record.prefix is None
    assert record.pid == os.getpid()
    # 장부에 영속 + 서술 디렉토리 신설.
    assert wp.find_task("newjob").name == "newjob"
    assert wp.task_dir("newjob").exists()
    # 슬롯 리스는 0개(task 는 슬롯과 직교·⑥).
    assert wp.list_leases() == []


def test_bind_task_resume_same_pid(wp):
    """기존 task 를 내 pid 로 재개 = resumed(crash 전 나·재진입)."""
    wp.bind_task("job1")                             # created (pid=os.getpid())
    record, action, reclaimed_from = wp.bind_task("job1")  # 같은 프로세스 재호출
    assert action == "resumed" and reclaimed_from is None
    assert record.pid == os.getpid()


def test_bind_task_reclaims_dead_pid_reports_prior_pid(wp):
    """기록 pid 가 죽었으면(crash) 회수 후 진입 = reclaimed + reclaimed_from=이전 pid(loud notice·㉑ 정직화)."""
    with wp._lease_lock():
        wp._write_tasks([wp.Task(name="job1", prefix="PAY", pid=999999, started="t")])
    record, action, reclaimed_from = wp.bind_task("job1")
    assert action == "reclaimed"
    assert reclaimed_from == 999999            # 회수한 이전 pid surface(notice 근거)
    assert record.pid == os.getpid()           # 내 것으로 회수
    assert record.prefix == "PAY"              # prefix 는 유지(바인딩이 안 만짐·T-0357 이 변경)


def test_bind_task_rejects_alive_other_session(wp):
    """살아있는 다른 세션이 점유하면 TaskActiveElsewhere(동시 세션 거부·㉑)."""
    with wp._lease_lock():
        # 살아있는 pid(현재 프로세스) 지만 "다른 세션" 을 모델 — 내 pid 와 다른 pid 로 주입 시
        # 거부돼야 하므로, _pid_alive 를 참으로 고정하고 다른 pid 를 기록한다.
        wp._write_tasks([wp.Task(name="job1", pid=os.getpid(), started="t")])
    # 다른 세션(pid≠내 pid)이 살아있는 상황 — pid 를 강제로 다르게 잡아 거부 경로 진입.
    import pytest as _pt
    with _pt.raises(wp.TaskActiveElsewhere):
        wp.bind_task("job1", pid=os.getpid() + 1)  # 내 pid 가 아닌 세션이 들어오려 함(기록 pid 살아있음)


def test_bind_task_pid_liveness_is_load_bearing(wp, monkeypatch):
    """생존 판정(`_pid_alive`)이 거부 로직의 load-bearing — 무력화하면 dead 도 alive 로 봐 거부.

    reclaim_stale 의 sensitivity 핀(pid 판정 무력화 시 회수 안 됨)과 동형."""
    with wp._lease_lock():
        wp._write_tasks([wp.Task(name="job1", pid=888888, started="t")])   # dead pid
    monkeypatch.setattr(wp, "_pid_alive", lambda pid: True)                  # 판정 무력화
    import pytest as _pt
    with _pt.raises(wp.TaskActiveElsewhere):
        wp.bind_task("job1")     # dead 인데 alive 로 오판 → 거부(판정이 load-bearing 임을 입증)


def test_bind_task_does_not_touch_slots_dir(wp):
    """task 바인딩은 `.local/slots/` 를 건드리지 않는다(마이그레이션 0·DoD·⑥ 직교)."""
    slots_dir = wp.LOCAL_DIR / "slots"
    slots_dir.mkdir(parents=True, exist_ok=True)
    (slots_dir / "sentinel").write_text("x", encoding="utf-8")
    wp.bind_task("job1")
    assert (slots_dir / "sentinel").read_text(encoding="utf-8") == "x"   # 무변경
    # 신설된 건 tasks 디렉토리뿐.
    assert wp.task_dir("job1").exists()


def test_list_tasks_and_find_task(wp):
    """list_tasks 전체·find_task 이름 조회(부재 None)."""
    wp.bind_task("a")
    wp.bind_task("b")
    names = {t.name for t in wp.list_tasks()}
    assert names == {"a", "b"}
    assert wp.find_task("a").name == "a"
    assert wp.find_task("nope") is None


# ── set_task_prefix — task prefix 지정/변경/해제 (F5·T-0357·장부 단일 소유·중간 변경 자유) ──


def test_set_task_prefix_sets_and_reads_back(wp):
    """`set_task_prefix` 가 task 레코드 prefix 를 지정하고 find_task 로 되읽힌다 (F5·지정)."""
    wp.bind_task("job1")                             # created·prefix=None(기본 없음)
    updated = wp.set_task_prefix("job1", "pay")
    assert updated is not None and updated.prefix == "pay"
    assert wp.find_task("job1").prefix == "pay"      # 영속(장부 write)


def test_set_task_prefix_none_clears(wp):
    """prefix=None → 해제(무prefix). 지정 후 해제하면 다시 None (F5·`none` 해제)."""
    wp.bind_task("job1")
    wp.set_task_prefix("job1", "pay")
    updated = wp.set_task_prefix("job1", None)
    assert updated.prefix is None
    assert wp.find_task("job1").prefix is None


def test_set_task_prefix_mid_change_is_free(wp):
    """중간 변경 자유(①ⓒ) — 진행 중 prefix 를 pay→acc→해제 로 자유롭게 바꾼다(task 종속 없음)."""
    wp.bind_task("job1")
    assert wp.set_task_prefix("job1", "pay").prefix == "pay"
    assert wp.set_task_prefix("job1", "acc").prefix == "acc"   # 변경
    assert wp.set_task_prefix("job1", None).prefix is None     # 해제
    assert wp.set_task_prefix("job1", "pay").prefix == "pay"   # 재지정


def test_set_task_prefix_missing_task_returns_none(wp):
    """부재 task → None(호출부가 rc1 안내·생성은 F1 단일 지점) — 새 레코드 만들지 않음."""
    assert wp.set_task_prefix("absent", "pay") is None
    assert wp.list_tasks() == []                     # task 생성 안 함(부작용 0)


def test_set_task_prefix_validates_task_name_before_write(wp):
    """write-capable 엔진 진입점 — 불법 task 명은 장부 write 이전 InvalidTaskName(부작용 0·must-fix)."""
    with _pytest_task.raises(wp.InvalidTaskName):
        wp.set_task_prefix("../evil", "pay")
    assert wp.list_tasks() == []


def test_set_task_prefix_preserves_sibling_leases_and_tasks(wp):
    """prefix 변경이 형제 `leases`·다른 task·미지 최상위 키를 보존(top-level round-trip·flock 단일 소유)."""
    ledger = {
        "leases": [{"slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
                    "started": "t", "state": "leased", "test_cmd": None}],
        "tasks": [{"name": "job1", "prefix": None, "pid": 111, "started": "t"},
                  {"name": "job2", "prefix": "acc", "pid": 222, "started": "t"}],
        "future_top_level": {"x": 1},
    }
    wp.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    wp.LEASES_FILE.write_text(json.dumps(ledger), encoding="utf-8")
    wp.set_task_prefix("job1", "pay")
    data = json.loads(wp.LEASES_FILE.read_text(encoding="utf-8"))
    by_name = {t["name"]: t for t in data["tasks"]}
    assert by_name["job1"]["prefix"] == "pay"        # 대상만 변경
    assert by_name["job2"]["prefix"] == "acc"        # 형제 task 보존
    assert data["leases"][0]["slot"] == "work/A_1"   # 리스 보존
    assert data["future_top_level"] == {"x": 1}      # 미지 최상위 키 보존


def test_set_task_prefix_read_by_identity_args_task_prefix(wp):
    """소비측 통합 — `set_task_prefix` 가 쓴 값을 `identity_args.task_prefix`(board.py new F5 소비)가 읽는다."""
    ia_spec = importlib.util.spec_from_file_location("ia_test", TOOLS / "identity_args.py")
    ia = importlib.util.module_from_spec(ia_spec)
    ia_spec.loader.exec_module(ia)          # 순수 point-read(leases_file 인자) — 전역 재배선 불요.
    wp.bind_task("job1")
    wp.set_task_prefix("job1", "pay")
    assert ia.task_prefix("job1", wp.LEASES_FILE) == "pay"
    wp.set_task_prefix("job1", None)
    assert ia.task_prefix("job1", wp.LEASES_FILE) is None   # 해제도 소비측에 반영


# ── release_task_pid — task 정상-종료 pid=0 기록 + bind 재분류 (T-0392·핸드오프 "두고 간다") ──
#
# task 장부 pid = dump 후 즉사하는 bootstrap subprocess pid(㉑·T-0353)라, pm_handoff 가 종료를 안
# 기록하면 정상 인계 후 재개도 dead-pid → bind_task `reclaimed`(crash 회수 loud notice)로 상시 오탐
# 한다(PM 78 실측). release_task_pid 가 pid 를 0(미점유)으로 비우고, bind_task 가 미점유를 `resumed`
# 로 재분류해 정상 인계 상시 경고를 없앤다 — 진짜 crash(pid>0 잔존)만 회수 경고를 받는다.


def test_bind_task_unoccupied_pid_zero_is_resumed_not_reclaimed(wp):
    """장부 pid=0(미점유=정상 인계) 재개 = resumed(reclaimed_from None) — crash 회수 경고 없음(T-0392)."""
    with wp._lease_lock():
        wp._write_tasks([wp.Task(name="job1", prefix="PAY", pid=0, started="t")])
    record, action, reclaimed_from = wp.bind_task("job1")
    assert action == "resumed"                  # 미점유는 clean resume(reclaimed 아님)
    assert reclaimed_from is None               # loud notice 근거 없음(정상 인계)
    assert record.pid == os.getpid()            # 내 것으로 갱신
    assert record.prefix == "PAY"               # prefix 유지(바인딩 무접촉)


def test_release_task_pid_sets_pid_zero_and_returns_record(wp):
    """release_task_pid 가 task 레코드 pid 를 0(미점유)으로 세팅하고 갱신 레코드를 돌려준다(정상-종료 기록)."""
    wp.bind_task("job1")                         # created (pid=os.getpid())
    updated = wp.release_task_pid("job1")
    assert updated is not None and updated.pid == 0
    assert wp.find_task("job1").pid == 0         # 영속(장부 write)


def test_release_task_pid_then_bind_resumes_clean(wp):
    """T-0392 핵심 라운드트립 — 정상-종료(release_task_pid→pid=0) 후 재개 = resumed(경고 없음)·reclaimed 아님.

    핸드오프가 pid 를 비워 두면 다음 부트스트랩이 dead-pid 회수(crash 경고)가 아니라 clean resume 으로
    진입한다(PM 78 정상 인계 상시 crash 경고 해소). 정상 경로 상시 경보를 없애 진짜 경보만 남긴다."""
    wp.bind_task("job1")                         # created (pid=os.getpid())
    released = wp.release_task_pid("job1")        # 정상-종료 → pid=0(미점유)
    assert released is not None and released.pid == 0
    record, action, reclaimed_from = wp.bind_task("job1")   # 재개
    assert action == "resumed" and reclaimed_from is None    # clean resume·회수 경고 없음
    assert record.pid == os.getpid()             # 내 것으로 갱신


def test_release_task_pid_missing_task_returns_none(wp):
    """부재 task → None(fail-soft·무해) — 새 레코드 만들지 않음(부작용 0·솔로/슬롯 모드 무해)."""
    assert wp.release_task_pid("absent") is None
    assert wp.list_tasks() == []                 # task 생성 안 함


def test_release_task_pid_validates_task_name_before_write(wp):
    """write-capable 엔진 진입점 — 불법 task 명은 장부 write 이전 InvalidTaskName(부작용 0·must-fix·set_task_prefix 동형)."""
    with _pytest_task.raises(wp.InvalidTaskName):
        wp.release_task_pid("../evil")
    assert wp.list_tasks() == []


def test_release_task_pid_preserves_sibling_leases_and_tasks(wp):
    """pid 비우기가 형제 `leases`·다른 task·미지 최상위 키를 보존(top-level round-trip·flock 단일 소유)."""
    ledger = {
        "leases": [{"slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
                    "started": "t", "state": "leased", "test_cmd": None}],
        "tasks": [{"name": "job1", "prefix": "PAY", "pid": 111, "started": "t"},
                  {"name": "job2", "prefix": "acc", "pid": 222, "started": "t"}],
        "future_top_level": {"x": 1},
    }
    wp.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    wp.LEASES_FILE.write_text(json.dumps(ledger), encoding="utf-8")
    wp.release_task_pid("job1")
    data = json.loads(wp.LEASES_FILE.read_text(encoding="utf-8"))
    by_name = {t["name"]: t for t in data["tasks"]}
    assert by_name["job1"]["pid"] == 0           # 대상만 비움
    assert by_name["job1"]["prefix"] == "PAY"    # 다른 필드는 무접촉(pid 만 비움)
    assert by_name["job2"]["pid"] == 222         # 형제 task 보존
    assert data["leases"][0]["slot"] == "work/A_1"   # 리스 보존
    assert data["future_top_level"] == {"x": 1}      # 미지 최상위 키 보존


# ── task 명 검증 (엔진층·traversal/절대경로/빈 이름/예약패턴 거부·must-fix·T-0353) ──


import pytest as _pytest_task


@_pytest_task.mark.parametrize("bad", [
    "../../evil", "..", ".hidden", ".", "a/b", "a\\b", "/tmp/x", "", "   ",
    " leading", "trailing ",
    # 문자 도메인 협소화(T-0356 codex 2건) — 하류 구문 표면 파손 방지:
    "my task",      # 내부 공백 → CLI/relay `--task <이름>` 인자 경계 파손.
    "a\tb",         # 탭(임의 whitespace) → 동상.
    "foo)bar",      # `)` → log 헤더 태그 `(task:…)` delimiter 조기 종료 → 파서 bound 불일치.
    "foo(bar",      # `(` → 동상(태그 delimiter).
])
def test_bind_task_rejects_unsafe_name_in_engine(wp, bad):
    """엔진 진입점(bind_task)이 부적합 task 명을 fail-loud 거부 — mkdir·장부 write 미발생(must-fix).

    `task_dir(name)` 무검증 조인이 traversal/절대경로/빈 이름을 작업트리 밖에 만들던 클래스
    (reviewer 실측 `--task ../../evil` → git-tracked). 엔진층 검증이라 CLI 우회(T-0354~0357)도 못 뚫는다.
    문자 도메인은 하류 구문 표면(CLI 인자 경계·log 태그 delimiter)에 맞춰 협소화한다(whitespace·괄호 거부·
    T-0356 codex 2건의 단일 불변식·per-surface 이스케이프 회피)."""
    with _pytest_task.raises(wp.InvalidTaskName):
        wp.bind_task(bad)
    # 부작용 0 — 장부에 tasks 미기록, tasks 디렉토리 미생성.
    assert wp.list_tasks() == []
    assert not (wp.LOCAL_DIR / "tasks").exists() or list((wp.LOCAL_DIR / "tasks").iterdir()) == []


def test_bind_task_valid_name_passes(wp):
    """정상 이름은 통과(오탐 0) — 대시·언더스코어·정수-접미·한글 자유 포맷 허용(협소화가 정상 명을 안 막음)."""
    for ok in ("payments-refactor", "job_v2", "hotfix3", "task2", "한글작업"):
        record, action, _ = wp.bind_task(ok)
        assert record.name == ok and action == "created"


def test_bind_task_rejects_reserved_pattern_when_repos_given(wp):
    """예약명 이중화(should-fix) — 엔진 진입점이 registered_repos 로 `<repo>_<N>` 거부(primitive 자기완결)."""
    with _pytest_task.raises(wp.InvalidTaskName):
        wp.bind_task("project_manager_1", registered_repos=["project_manager"])
    # 미등록 repo 형태는 자유 포맷 허용(실재 슬롯과만 충돌 방지).
    record, _, _ = wp.bind_task("sikdan_2", registered_repos=["project_manager"])
    assert record.name == "sikdan_2"


def test_validate_task_name_is_load_bearing_before_writes(wp):
    """검증이 write/mkdir *이전* — 거부 시 장부·디렉토리 흔적 0(부작용 순서 핀)."""
    with _pytest_task.raises(wp.InvalidTaskName):
        wp.bind_task("../escape")
    assert not (wp.LOCAL_DIR / "tasks" / "..").exists()
    assert wp.list_tasks() == []


# ════════════════════════════════════════════════════════════════════════
# alloc 최소 번호 대여 (결정론 ⓒ·T-0354·F2)
# ════════════════════════════════════════════════════════════════════════


def test_alloc_leases_minimal_idle_number(wp):
    """idle 후보가 여럿이면 **최소 번호**를 대여한다(장부 파일 순서 무관·결정론 ⓒ).

    장부를 일부러 역순(3·1·2)으로 심어 first-in-ledger 선택이 아님을 강제 — 정렬이 없으면
    work/A_3 를 골라 이 테스트가 fail 한다(sensitivity)."""
    _seed(wp,
          _lease(wp, slot="work/A_3", repo="A", session="", pid=0, state="idle"),
          _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"),
          _lease(wp, slot="work/A_2", repo="A", session="", pid=0, state="idle"))
    git = FakeGit()
    lease = wp.alloc("A", session="task-x", git_runner=git)
    assert lease.slot == "work/A_1"     # 최소 번호
    assert lease.session == "task-x"    # task 정체성이 session 축에 실림(⑥)


def test_alloc_task_session_is_release_ownership_key(wp):
    """alloc(session=task) 로 대여한 슬롯은 그 task 의 release 소유검사를 통과한다(왕복·F2↔F3)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="", pid=0, state="idle"))
    git = FakeGit(dirty=False)
    wp.alloc("A", session="job", git_runner=git)
    lease = wp.release("work/A_1", owner_task="job", git_runner=git)  # 같은 task → 통과
    assert lease.state == "idle"


# ════════════════════════════════════════════════════════════════════════
# release --task 소유검사 (F3·T-0354)
# ════════════════════════════════════════════════════════════════════════


def test_release_owner_task_match_releases(wp):
    """release owner_task 일치 → 정상 idle 반납."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="job", state="leased"))
    git = FakeGit(dirty=False)
    lease = wp.release("work/A_1", owner_task="job", git_runner=git)
    assert lease.state == "idle"


def test_release_owner_task_mismatch_raises_not_task_owner(wp):
    """release owner_task 불일치 → NotTaskOwner(다른 task 슬롯 보호·부작용 0)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other", state="leased"))
    git = FakeGit(dirty=False)
    with pytest.raises(wp.NotTaskOwner) as ei:
        wp.release("work/A_1", owner_task="job", git_runner=git)
    assert ei.value.holder == "other"
    assert wp.list_leases()[0].state == "leased"     # 거부 — 여전히 leased


def test_release_owner_task_precedes_dirty_check(wp, proj):
    """소유검사가 dirty 판정보다 먼저 — 남의 dirty 슬롯도 stash 시도 없이 NotTaskOwner(부작용 0)."""
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other", state="leased"))
    git = FakeGit(dirty=True)
    with pytest.raises(wp.NotTaskOwner):
        wp.release("work/A_1", owner_task="job", git_runner=git)
    assert not git.did("stash", "push")      # 소유검사 먼저라 stash 조차 안 함


def test_release_no_owner_task_skips_ownership_check(wp):
    """owner_task=None(백스톱·현행) → 소유검사 우회(session 무관 slot-only 반납)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="whoever", state="leased"))
    git = FakeGit(dirty=False)
    lease = wp.release("work/A_1", git_runner=git)   # owner_task 미지정
    assert lease.state == "idle"


# ════════════════════════════════════════════════════════════════════════
# slots_for_task (조회·소유검사 근거·T-0354)
# ════════════════════════════════════════════════════════════════════════


def test_slots_for_task_returns_only_leased_owned(wp):
    """slots_for_task = session==name 이고 leased 인 슬롯만(idle·타 session 제외)."""
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", session="job", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="job", pid=0, state="idle"),
          _lease(wp, slot="work/B_1", repo="B", session="other", state="leased"))
    slots = sorted(l.slot for l in wp.slots_for_task("job"))
    assert slots == ["work/A_1"]


def test_bind_slot_task_name_immediately_resolves_via_slots_for_task(wp):
    """T-0390 — `bind_slot(session=<task명>)` 하면 `slots_for_task(<task명>)`(F6 축)가 즉시 그 슬롯을 본다.

    부트스트랩 task+slot 경로가 슬롯을 task 명의로 bind 하면(session=task명), F6 해소 프리미티브
    (`slots_for_task`·session==name 축)가 별도 alloc 없이 그 슬롯을 소유로 잡는다 — PM 78 의
    `pm_config alloc` 추가 스텝 마찰이 부트스트랩 한 줄로 닫힌다."""
    lease = wp.bind_slot("work/A_2", "A", "payments")
    assert lease.session == "payments" and lease.bound is True
    resolved = wp.slots_for_task("payments")
    assert [l.slot for l in resolved] == ["work/A_2"]
    # sensitivity — 슬롯-세션 명의(A_2)로는 그 task 로 해소되지 않는다(축 분리 확인).
    assert wp.slots_for_task("A_2") == []


# ════════════════════════════════════════════════════════════════════════
# end_task — task 종료(dirty 게이트·일괄 반납·아카이브 이동·②·T-0354·F4)
# ════════════════════════════════════════════════════════════════════════


def test_end_task_clean_releases_all_removes_record_and_archives(wp, proj):
    """전부 clean → 보유 슬롯 일괄 idle 반납(worktree 미삭제) + task 레코드 제거 + 서술 폴더 _ended 이동(②)."""
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)
    (proj / "work" / "A_2").mkdir(parents=True, exist_ok=True)
    wp.bind_task("job")                                        # task 레코드 + .local/tasks/job/
    (wp.task_dir("job") / "pm_state.md").write_text("state", encoding="utf-8")
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", session="job", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="job", state="leased"))
    git = FakeGit(dirty=False)
    result = wp.end_task("job", git_runner=git)
    assert not result.refused
    assert set(result.released) == {"work/A_1", "work/A_2"}
    # 반납=idle·worktree 폴더는 삭제 안 함(미삭제 확인).
    states = {l.slot: l.state for l in wp.list_leases()}
    assert states == {"work/A_1": "idle", "work/A_2": "idle"}
    assert (proj / "work" / "A_1").exists() and (proj / "work" / "A_2").exists()
    # task 레코드 제거.
    assert wp.find_task("job") is None
    # 서술 폴더 이동(삭제 아님) — 원본 부재·목적지 존재·내용 보존.
    assert not wp.task_dir("job").exists()
    assert result.moved_to is not None and result.moved_to.exists()
    assert (result.moved_to / "pm_state.md").read_text(encoding="utf-8") == "state"


def test_end_task_dirty_refuses_with_no_side_effects(wp, proj):
    """보유 슬롯 dirty → 거부(released/moved 없음·슬롯 leased·task 레코드·서술 폴더 모두 잔존)."""
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)
    wp.bind_task("job")
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="job", state="leased"))
    git = FakeGit(dirty=True)
    result = wp.end_task("job", git_runner=git)
    assert result.refused
    assert result.dirty == ["work/A_1"]
    assert result.released == [] and result.moved_to is None
    # 부작용 0.
    assert wp.list_leases()[0].state == "leased"
    assert wp.find_task("job") is not None
    assert wp.task_dir("job").exists()


def test_end_task_no_descriptor_folder_graceful(wp):
    """서술 폴더 부재(장부 task 레코드만) → 이동 없음(moved_to None)·반납/제거는 수행."""
    with wp._lease_lock():
        wp._write_tasks([wp.Task(name="job", pid=1, started="t")])   # 폴더 없이 레코드만
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="job", state="leased"))
    git = FakeGit(dirty=False)
    result = wp.end_task("job", git_runner=git)
    assert result.moved_to is None
    assert result.released == ["work/A_1"]
    assert wp.find_task("job") is None
    assert wp.list_leases()[0].state == "idle"


def test_end_task_no_owned_slots_still_ends(wp):
    """보유 슬롯 0 이어도 task 레코드 제거 + 서술 폴더 이동은 수행(released 빈 리스트)."""
    wp.bind_task("job")
    git = FakeGit(dirty=False)
    result = wp.end_task("job", git_runner=git)
    assert result.released == [] and not result.refused
    assert wp.find_task("job") is None
    assert result.moved_to is not None and result.moved_to.exists()


def test_end_task_clears_bound_marker(wp, proj):
    """end_task 일괄 idle 반납은 release 동형 lifecycle — bound 마커도 해제한다(codex suggestion·T-0389)."""
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)
    wp.bind_task("job")
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="job", pid=os.getpid(),
                       started="t", state="leased", bound=True))   # 사람 bind 슬롯
    git = FakeGit(dirty=False)
    result = wp.end_task("job", git_runner=git)
    assert result.released == ["work/A_1"]
    released = wp.list_leases()[0]
    assert released.state == "idle" and released.bound is False   # 반납 시 마커 해제


def test_end_task_archive_dir_collision_uniquifies(wp):
    """같은 날 같은 이름 재종료 목적지 충돌 → `-2` 로 유일화(덮어써 기록 유실 방지·②)."""
    import datetime as _dt
    date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
    ended = wp.TASKS_DIR / "_ended"
    (ended / f"job-{date}").mkdir(parents=True, exist_ok=True)      # 선점 목적지
    wp.bind_task("job")
    (wp.task_dir("job") / "x").write_text("y", encoding="utf-8")
    git = FakeGit(dirty=False)
    result = wp.end_task("job", git_runner=git)
    assert result.moved_to == ended / f"job-{date}-2"
    assert result.moved_to.exists()


def test_end_task_rejects_unsafe_name_before_writes(wp, proj):
    """end_task 불법 이름(traversal) → InvalidTaskName·장부/이동 부작용 0 (must-fix ②·bind_task 동형).

    무검증이면 `../evil` 이 `_archive_dest` 파생 후 `.local/tasks` 밖으로 `shutil.move` 한다 —
    엔진 진입점에서 write/이동 이전 fail-loud 로 닫는다(T-0353 클래스 재발 차단)."""
    # 세션이 불법 이름인 leased 슬롯을 심어(현실엔 없지만) 검증이 반납 이전임을 강제.
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="../evil", state="leased"))
    git = FakeGit(dirty=False)
    for bad in ("../evil", "/abs/x", "a/b", "  "):
        with pytest.raises(wp.InvalidTaskName):
            wp.end_task(bad, git_runner=git)
    # 부작용 0 — 슬롯 여전히 leased(반납 안 됨).
    assert wp.list_leases()[0].state == "leased"


def test_end_task_valid_name_still_ends(wp):
    """정상 이름은 검증 통과 후 종료(검증이 정상 경로를 막지 않음·sensitivity 대조)."""
    wp.bind_task("job")
    git = FakeGit(dirty=False)
    result = wp.end_task("job", git_runner=git)
    assert not result.refused and wp.find_task("job") is None


# ════════════════════════════════════════════════════════════════════════
# readonly 공유 슬롯 — role 필드 / detached 생성 / mutation 거부 / refresh (⑬·T-0358·§F11)
# ════════════════════════════════════════════════════════════════════════


def test_lease_role_default_work_omitted_from_dict(wp):
    """role 기본("work")은 to_dict 가 방출하지 않는다 — 구 장부 왕복 byte-무손실(git 필드 동형·T-0358)."""
    lease = wp.Lease(slot="work/A_1", repo="A", session="me", pid=1, started="t", state="leased")
    assert lease.role == "work"
    d = lease.to_dict()
    assert "role" not in d                    # 기본이면 키 자체를 안 넣는다(하위호환).


def test_lease_role_readonly_round_trips(wp):
    """role="readonly"는 to_dict 방출·from_dict read — 왕복 보존(T-0358·additive)."""
    lease = wp.Lease(slot="work/A_1", repo="A", session="", pid=0, started="t",
                     state="leased", role="readonly")
    d = lease.to_dict()
    assert d["role"] == "readonly"
    restored = wp.Lease.from_dict(d)
    assert restored.role == "readonly"
    assert restored == lease                  # __eq__(to_dict 동등)이 role 포함


def test_from_dict_missing_role_defaults_work_backcompat(wp):
    """구 장부(role 부재) 로드 → role="work"(하위호환 read)·재기록해도 role 키 안 생김(T-0358·회귀 유지)."""
    old = {"slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
           "started": "t", "state": "leased", "test_cmd": None}
    lease = wp.Lease.from_dict(old)
    assert lease.role == "work"
    d = lease.to_dict()
    assert "role" not in d                    # role: "work" 를 덧붙이지 않는다(왕복 무손실)
    assert d == old


def test_create_slot_readonly_detached_no_session_role_recorded(wp):
    """create_slot(readonly=True) — `worktree add --detach`·role="readonly"·session/pid 없음·base 기록(⑬·§F11)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit()
    lease = wp.create_slot("A", base="main", session="me", git_runner=git)  # base 있는 대조 세팅 위해 별도
    # 위는 일반 슬롯(대조) — 아래가 readonly 검증.
    lease = wp.create_slot("A", base="main", readonly=True, git_runner=git)
    # detached 생성 — `git worktree add --detach <path> [ref]`(슬롯 전용 브랜치 -b 안 씀).
    assert git.did("worktree", "add", "--detach")
    assert not git.did("worktree", "add", "--no-track", "-b", "A_2")   # readonly 는 슬롯 브랜치 안 판다
    # 무소유 확정 — role="readonly"·session/pid 없음.
    assert lease.role == "readonly"
    assert lease.session == "" and lease.pid == 0
    assert lease.state == "leased"
    # base(released 기준면)를 스냅에 기록 — 문서 검증 기준(§F9).
    assert isinstance(lease.git, dict) and lease.git.get("base", {}).get("branch") == "main"
    # 장부에도 role="readonly" 로 반영.
    ro = next(l for l in wp.list_leases() if l.slot == lease.slot)
    assert ro.role == "readonly" and ro.session == ""


def test_create_slot_readonly_origin_base_fetch(wp):
    """readonly 생성 — fetch origin 후 origin/<base> 해소되면 그 최신에서 detach(작업 슬롯 파생 규율 동형)."""
    _mk_bare_placeholder(wp, "A")
    git = FakeGit(origin_has_base=True)
    wp.create_slot("A", base="main", readonly=True, git_runner=git)
    assert git.did("fetch", "origin")
    assert git.did("worktree", "add", "--detach", str(wp.slot_path("work/A_1")), "origin/main")


def test_readonly_slot_excluded_from_reclaim_stale(wp):
    """readonly 슬롯(pid=0)은 reclaim_stale 회수 대상이 아니다 — role 가드(⑬·회수 시 유실 방지·T-0358)."""
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="", pid=0, started="t",
                       state="leased", role="readonly"))
    git = FakeGit()
    reclaimed = wp.reclaim_stale(git_runner=git)
    assert reclaimed == []                     # pid=0(죽음) 이지만 readonly 라 회수 안 함
    after = wp.list_leases()[0]
    assert after.state == "leased" and after.role == "readonly"   # idle 화 안 됨


def test_readonly_slot_excluded_from_alloc(wp):
    """readonly 슬롯은 alloc idle-탐색 대상이 아니다(idle 아님·무소유) → 풀 소진 NeedsCreate(⑬)."""
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="", pid=0, started="t",
                       state="leased", role="readonly"))
    git = FakeGit()
    with pytest.raises(wp.NeedsCreate):
        wp.alloc("A", session="me", git_runner=git)


# ════════════════════════════════════════════════════════════════════════
# bound 마커 — 사람 bind 슬롯 reclaim 제외 (T-0389 · ADR-0013 Amendment(T-0074))
# bind_slot 이 적는 pid 는 ephemeral bootstrap subprocess pid 라 즉사한다. 방치하면 타 세션
# alloc 의 reclaim_stale 이 `leased && pid 죽음` = stale 로 오판·회수(PM 78 실측). bound
# 마커로 reclaim 대상에서 제외한다. pool 경로(alloc idle-리스·release)는 마커를 해제해
# 현행 pid-회수 거동을 유지. 마커는 additive(구 장부 부재=False·마이그레이션 0).
# ════════════════════════════════════════════════════════════════════════


def test_bound_default_false_omitted_from_dict(wp):
    """bound 기본(False)은 to_dict 가 방출하지 않는다 — 구 장부 왕복 byte-무손실(role 필드 동형·T-0389)."""
    lease = wp.Lease(slot="work/A_1", repo="A", session="me", pid=1, started="t", state="leased")
    assert lease.bound is False
    assert "bound" not in lease.to_dict()      # 기본이면 키 자체를 안 넣는다(하위호환).


def test_bound_true_round_trips(wp):
    """bound=True 는 to_dict 방출·from_dict read — 왕복 보존(T-0389·additive·role 동형)."""
    lease = wp.Lease(slot="work/A_1", repo="A", session="me", pid=1, started="t",
                     state="leased", bound=True)
    d = lease.to_dict()
    assert d["bound"] is True
    restored = wp.Lease.from_dict(d)
    assert restored.bound is True
    assert restored == lease                   # __eq__(to_dict 동등)이 bound 포함


def test_from_dict_missing_bound_defaults_false_backcompat(wp):
    """구 장부(bound 부재) 로드 → bound=False(하위호환 read)·재기록해도 bound 키 안 생김(T-0389)."""
    old = {"slot": "work/A_1", "repo": "A", "session": "me", "pid": 7,
           "started": "t", "state": "leased", "test_cmd": None}
    lease = wp.Lease.from_dict(old)
    assert lease.bound is False
    d = lease.to_dict()
    assert "bound" not in d                     # bound: false 를 덧붙이지 않는다(왕복 무손실)
    assert d == old


def test_bind_slot_sets_bound_marker(wp):
    """bind_slot 은 사람 bind 마커(bound=True)를 박고 장부에 영속화한다(신규·기존 슬롯 공통·T-0389)."""
    # 신규 슬롯 append 경로.
    lease = wp.bind_slot("work/A_1", "A", "A_1")
    assert lease.bound is True
    assert next(l for l in wp.list_leases() if l.slot == "work/A_1").bound is True
    # 기존 슬롯 update-in-place 경로(pool idle 슬롯을 사람이 직접 점유로 승격).
    _seed(wp, wp.Lease(slot="work/A_2", repo="A", session="", pid=0, started="t", state="idle"))
    lease2 = wp.bind_slot("work/A_2", "A", "A_2")
    assert lease2.bound is True
    assert next(l for l in wp.list_leases() if l.slot == "work/A_2").bound is True


def test_bound_lease_excluded_from_reclaim_stale(wp):
    """bound 슬롯(pid 죽음)은 reclaim_stale 회수 대상이 아니다 — 사람 bind 정체성 보호(T-0389).

    `bind_slot` 이 적는 pid 는 즉사하는 ephemeral bootstrap subprocess pid 라, 방치하면
    `leased && pid 죽음` = stale 로 오판된다. bound 마커가 그 회수를 막는다(readonly 가드 동형).
    """
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="A_1", pid=999999,
                       started="t", state="leased", bound=True))   # pid 죽음 + bound
    git = FakeGit()
    reclaimed = wp.reclaim_stale(git_runner=git)
    assert reclaimed == []                      # pid 죽음이지만 bound 라 회수 안 함
    after = wp.list_leases()[0]
    assert after.state == "leased" and after.session == "A_1" and after.bound is True   # idle 화 안 됨


def test_unbound_pool_lease_still_reclaimed(wp):
    """bound 아닌 pool 슬롯(pid 죽음)은 현행대로 reclaim_stale 이 회수한다(T-0389·bound 보호는 bind 한정)."""
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="dead", pid=999999,
                       started="t", state="leased"))   # bound 기본 False
    git = FakeGit()
    reclaimed = wp.reclaim_stale(git_runner=git)
    assert reclaimed == ["work/A_1"]            # bound 아니라 현행 회수
    assert wp.list_leases()[0].state == "idle"


def test_old_ledger_lease_reclaimed_backcompat(wp):
    """구 장부(bound 키 부재) 슬롯은 bound=False 동치라 pid 죽으면 현행대로 회수된다(T-0389 하위호환).

    additive 마커의 부재=False 동치를 reclaim 경로에서 실증한다 — 마이그레이션 없이 구 장부가
    현행 pid-회수 거동을 그대로 유지(bound 보호는 신규 bind_slot 이 명시로 박은 슬롯에만 발동).
    """
    # bound 키가 아예 없는 raw 장부를 파일로 직접 심는다(from_dict 하위호환 read 경로).
    _seed(wp, wp.Lease.from_dict({"slot": "work/A_1", "repo": "A", "session": "dead",
                                  "pid": 999999, "started": "t", "state": "leased"}))
    git = FakeGit()
    assert wp.reclaim_stale(git_runner=git) == ["work/A_1"]
    assert wp.list_leases()[0].state == "idle"


def test_bound_lease_survives_other_session_alloc_pm78(wp):
    """PM 78 재현 — bind 후 타 명의 alloc 이 그 bound lease 를 못 뺏는다 → NeedsCreate.

    F2 task alloc(타 세션)은 진입 시 reclaim_stale 을 부른다. bound 슬롯의 pid 가 죽어 있어도
    (ephemeral bootstrap pid) 회수되지 않으므로, 그 슬롯은 leased 로 남아 alloc idle-탐색에
    안 걸린다(풀 소진). 실측 결함(타 창 세션 bind slot 이 alloc 에 회수됨)의 회귀 차단.
    """
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="A_1", pid=999999,
                       started="t", state="leased", bound=True))   # 타 창 사람 bind(pid 죽음)
    git = FakeGit()
    # 타 명의(task main) alloc — 진입 reclaim_stale 이 bound 슬롯을 회수하지 못하고, 유일 슬롯이
    # leased 라 idle 후보가 없다 → NeedsCreate(사용자 게이트).
    with pytest.raises(wp.NeedsCreate):
        wp.alloc("A", session="main", git_runner=git)
    # bound 슬롯은 유실 없이 그대로(session/state/bound 보존).
    survived = wp.list_leases()[0]
    assert survived.session == "A_1" and survived.state == "leased" and survived.bound is True


def test_alloc_branch_realloc_does_not_hijack_other_session_bound(wp):
    """codex must-fix — resume/branch 재부착(2경로)이 타 세션 bound lease 를 탈취하지 않는다(T-0389).

    `alloc(repo, branch=X)`/`resume=X` 가 그 브랜치를 체크아웃 중인 *타 세션 bound* 슬롯을 만나도
    session/pid/bound 를 덮지 않는다(사람 bind 정체성 보호). 유일 슬롯이 bound 라 재부착 후보에서
    제외 → 풀 소진 NeedsCreate. bound 슬롯은 세션/브랜치/마커 그대로 보존.
    """
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="A_1", pid=999999,
                       started="t", state="leased", bound=True))
    git = FakeGit(head="a5-pay")   # bound 슬롯 live HEAD = a5-pay(요청 branch 와 매칭)
    with pytest.raises(wp.NeedsCreate):
        wp.alloc("A", branch="a5-pay", session="thief", git_runner=git)
    survived = wp.list_leases()[0]
    assert survived.session == "A_1" and survived.state == "leased" and survived.bound is True
    assert wp.current_branch("work/A_1", git_runner=git) == "a5-pay"   # 탈취 재체크아웃 없음


def test_alloc_branch_realloc_reattaches_unbound_same_branch(wp):
    """대조(sensitivity) — bound 아닌 슬롯은 branch-매칭 재부착이 현행대로 동작한다(2경로 회귀 유지·T-0389)."""
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="old", pid=999999,
                       started="t", state="leased"))   # bound 아님
    git = FakeGit(head="a5-pay")
    lease = wp.alloc("A", resume="a5-pay", session="new", git_runner=git)
    assert lease.slot == "work/A_1" and lease.session == "new"   # 정상 재부착
    assert lease.bound is False


def test_alloc_idle_relance_clears_bound_marker(wp, proj):
    """pool alloc 이 idle 슬롯을 재대여할 때 bound 마커를 해제한다 — 이후 현행 pid-회수 거동 복귀(T-0389).

    bind→release 후 남은 idle 슬롯을 pool alloc 이 잡으면 사람 bind 소유가 아니므로 bound 를
    clear 한다(마커가 잔존하면 reclaim 이 영영 안 돼 풀이 안 풀린다).
    """
    (proj / "work" / "A_1").mkdir(parents=True, exist_ok=True)
    # idle 슬롯에 bound 마커가 남아있는 엣지(방어) — alloc 이 재대여하며 해제해야 한다.
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="", pid=0, started="t",
                       state="idle", bound=True))
    git = FakeGit()
    leased = wp.alloc("A", session="me", git_runner=git)
    assert leased.slot == "work/A_1" and leased.bound is False   # pool 대여 = 마커 해제
    assert next(l for l in wp.list_leases() if l.slot == "work/A_1").bound is False


def test_release_clears_bound_marker(wp):
    """명시 release 는 bind 점유를 종료 — bound 마커를 해제한다(git=None 동형 teardown·T-0389)."""
    wp.bind_slot("work/A_1", "A", "A_1")        # bound=True
    released = wp.release("work/A_1", require_clean=False)
    assert released.state == "idle" and released.bound is False
    assert next(l for l in wp.list_leases() if l.slot == "work/A_1").bound is False


def test_force_release_clears_bound_marker(wp):
    """force_release 도 bind 점유를 종료 — bound 마커 해제(release 동형·백스톱·T-0389)."""
    wp.bind_slot("work/A_1", "A", "A_1")        # bound=True
    released = wp.force_release("work/A_1")
    assert released.state == "idle" and released.bound is False
    assert next(l for l in wp.list_leases() if l.slot == "work/A_1").bound is False


def test_reclaim_bound_guard_sensitivity(wp, monkeypatch):
    """sensitivity — bound 가드가 load-bearing 임을 박제한다.

    bound 가드를 무력화(모든 lease.bound 를 False 로 보면)하면 죽은-pid bound 슬롯이 회수된다
    (=PM 78 결함 재현). 정상 로직은 bound 를 존중해 회수하지 않는다.
    """
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="A_1", pid=999999,
                       started="t", state="leased", bound=True))
    git = FakeGit()
    # 정상: bound 라 회수 안 됨.
    assert wp.reclaim_stale(git_runner=git) == []

    # 무력화: bound 판정을 죽이면(항상 False) 죽은-pid 슬롯이 회수된다(가드 부재 시 결함 재현).
    _seed(wp, wp.Lease(slot="work/A_2", repo="A", session="A_2", pid=999999,
                       started="t", state="leased", bound=True))

    # 가드 무력화: reclaim 루프가 참조하는 bound 를 강제 False 로. _read_ledger 를 감싸
    # bound 를 지운 리스를 돌려준다(가드가 없는 것과 동치 = PM 78 결함 재현).
    real_read = wp._read_ledger

    def stripped_read():
        leases = real_read()
        for l in leases:
            l.bound = False
        return leases

    monkeypatch.setattr(wp, "_read_ledger", stripped_read)
    assert wp.reclaim_stale(git_runner=git) == ["work/A_2"], "bound 가드 무력화 시 죽은-pid 슬롯 회수(결함 재현)"


# ── mutation 거부 (set-base·rebase·dev·sync × readonly = 에러·엔진 경로 한정·결정 ④) ──


def _seed_readonly(wp, slot="work/A_1", repo="A", *, base=True):
    git = {"base": {"branch": "main", "commit": "c0"}, "branch": None, "head": "h0",
           "submodules": [], "recorded_at": "t"} if base else None
    _seed(wp, wp.Lease(slot=slot, repo=repo, session="", pid=0, started="t",
                       state="leased", role="readonly", git=git))


def test_set_base_rejected_on_readonly(wp):
    """set_base 가 readonly 슬롯이면 ReadonlySlotMutation(base 는 released 기준면·mutation 불가·T-0358)."""
    _seed_readonly(wp)
    git = FakeGit()
    with pytest.raises(wp.ReadonlySlotMutation) as ei:
        wp.set_base("work/A_1", "origin/main", git_runner=git)
    assert ei.value.op == "set-base"


def test_resolve_rebase_base_rejected_on_readonly(wp):
    """resolve_rebase_base(rebase gate) 가 readonly 슬롯이면 ReadonlySlotMutation(진입 가드·T-0358)."""
    _seed_readonly(wp)
    git = FakeGit()
    with pytest.raises(wp.ReadonlySlotMutation) as ei:
        wp.resolve_rebase_base("work/A_1", onto="origin/main", git_runner=git)
    assert ei.value.op == "rebase"


def test_dev_rejected_on_readonly(wp):
    """dev(submodule on-branch checkout) 가 readonly 슬롯이면 ReadonlySlotMutation(T-0358)."""
    _seed_readonly(wp)
    git = FakeGit()
    with pytest.raises(wp.ReadonlySlotMutation) as ei:
        wp.dev("work/A_1", "vendor/x", "feat", git_runner=git)
    assert ei.value.op == "dev"


def test_sync_rejected_on_readonly(wp):
    """sync(submodule pin 재동기) 가 readonly 슬롯이면 ReadonlySlotMutation(fail-soft 보다 우선·T-0358)."""
    _seed_readonly(wp)
    git = FakeGit()
    with pytest.raises(wp.ReadonlySlotMutation) as ei:
        wp.sync("work/A_1", git_runner=git)
    assert ei.value.op == "sync"


def test_mutation_allowed_on_work_slot_sensitivity(wp):
    """sensitivity 대조 — work 슬롯(role 기본)엔 mutation 거부가 안 걸린다(readonly 만 예외)."""
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="me", pid=os.getpid(),
                       started="t", state="leased"))   # role="work"
    git = _BaseGit(tips={"origin/main": _BASE_TIP}, head="a1", head_sha=_NEW_SHA)
    # set_base 가 ReadonlySlotMutation 을 던지지 않는다(정상 기록 경로 — 거부 가드 미발동).
    lease = wp.set_base("work/A_1", "origin/main", git_runner=git)
    assert lease is not None and lease.git["base"]["branch"] == "origin/main"


# ── refresh (fetch → detach 이동·dirty=거부+loud·no-base·not-readonly·⑬·§F11) ──


def test_refresh_fast_forwards_detached_head(wp):
    """refresh — clean readonly 슬롯을 fetch 후 origin/<base> 최신 tip 으로 detach 이동(⑬·§F11)."""
    _seed_readonly(wp)
    git = FakeGit(origin_has_base=True)   # clean·fetch rc0·origin/main 해소
    ref = wp.refresh("work/A_1", git_runner=git)
    assert ref == "origin/main"
    assert git.did("fetch", "origin")
    assert git.did("checkout", "--detach", "origin/main")   # detached HEAD 이동


def test_refresh_resyncs_submodules(wp):
    """refresh — checkout 후 submodule 재동기 명시 수행(must-fix ①·gitlink stale/자가잠금 방지·⑬)."""
    _seed_readonly(wp)
    git = FakeGit(origin_has_base=True)
    wp.refresh("work/A_1", git_runner=git)
    # `checkout --detach` 뒤 `submodule update --init --recursive --force`(전체 재동기·readonly=dev 없음).
    assert git.did("submodule", "update", "--init", "--recursive", "--force")


def test_refresh_submodule_failure_fails_loud(wp):
    """refresh — submodule 재동기 rc≠0 이면 RefreshRefused(git-error·반쯤 갱신 성공보고 금지·must-fix ①)."""
    _seed_readonly(wp)

    class _SubFailGit(FakeGit):
        def __call__(self, argv):
            if argv[:2] == ["submodule", "update"]:
                self.calls.append(list(argv))
                return (1, "fatal: submodule sync failed")
            return super().__call__(argv)

    git = _SubFailGit(origin_has_base=True)
    with pytest.raises(wp.RefreshRefused) as ei:
        wp.refresh("work/A_1", git_runner=git)
    assert ei.value.reason == "git-error"


def test_refresh_updates_base_commit_when_onto_omitted(wp):
    """refresh(onto 생략) — base.commit 을 새 head 로 갱신(must-fix ②·옛 커밋 잔존 불일치 방지·⑬).

    기록된 base.commit="c0"(옛). onto 없이 refresh → HEAD 는 origin/main 최신으로 이동했으니 장부
    base.commit 도 새 head(FakeGit head_sha)로 갱신돼야 status "N behind"·기준면 기록 불일치가 안 남는다."""
    _seed_readonly(wp)   # git.base = {branch:"main", commit:"c0"}
    git = FakeGit(origin_has_base=True, head_sha=_NEW_SHA)
    wp.refresh("work/A_1", git_runner=git)
    recorded = next(l for l in wp.list_leases() if l.slot == "work/A_1").git["base"]
    assert recorded["branch"] == "main"        # 논리 branch 보존
    assert recorded["commit"] == _NEW_SHA      # 새 head 로 갱신(옛 "c0" 아님)


def test_refresh_onto_overrides_recorded_base(wp):
    """refresh --onto 명시 → 그 기준으로 이동(기록된 base.branch 무시)·base 도 그 값으로 갱신(⑬)."""
    _seed_readonly(wp)
    git = FakeGit(origin_has_base=True, head_sha=_NEW_SHA)
    ref = wp.refresh("work/A_1", onto="develop", git_runner=git)
    assert ref == "origin/develop"
    assert git.did("checkout", "--detach", "origin/develop")
    recorded = next(l for l in wp.list_leases() if l.slot == "work/A_1").git["base"]
    assert recorded == {"branch": "develop", "commit": _NEW_SHA}   # base branch+commit 둘 다 갱신


def test_refresh_dirty_refused_loud(wp):
    """refresh — dirty(누군가 씀·신호)면 거부 + loud(조용히 reset 안 함·⑬·§F11·결정)."""
    _seed_readonly(wp)
    git = FakeGit(dirty=True)   # 미커밋 변경 있음
    with pytest.raises(wp.RefreshRefused) as ei:
        wp.refresh("work/A_1", git_runner=git)
    assert ei.value.reason == "dirty"
    # 조용히 reset 안 함 — checkout --detach 를 부르지 않았다.
    assert not git.did("checkout", "--detach", "origin/main")


def test_refresh_no_base_no_onto_refused(wp):
    """refresh — base 미기록 + --onto 없음 → 거부(추론 금지·결정 ⑪ 정신·⑬)."""
    _seed_readonly(wp, base=False)   # base 미기록
    git = FakeGit()
    with pytest.raises(wp.RefreshRefused) as ei:
        wp.refresh("work/A_1", git_runner=git)
    assert ei.value.reason == "no-base"


def test_refresh_rejected_on_non_readonly(wp):
    """refresh — 대상이 readonly 가 아니면 거부(작업 슬롯 detach 이동=브랜치 위치 유실·⑬)."""
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="me", pid=1, started="t",
                       state="leased"))   # role="work"
    git = FakeGit()
    with pytest.raises(wp.RefreshRefused) as ei:
        wp.refresh("work/A_1", onto="origin/main", git_runner=git)
    assert ei.value.reason == "not-readonly"


def test_slot_role_helper_defaults_work_for_absent(wp):
    """_slot_role — 미등록 슬롯/구 장부(role 부재)는 "work"(fail-soft·mutation 거부 기본 non-readonly)."""
    assert wp._slot_role("work/absent_9") == "work"
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="me", pid=1, started="t", state="leased"))
    assert wp._slot_role("work/A_1") == "work"
    _seed_readonly(wp, slot="work/A_1")
    assert wp._slot_role("work/A_1") == "readonly"


# ── lease-lifecycle 거부: release / force_release / bind_slot × readonly (should-fix·⑬) ──


def test_release_rejected_on_readonly(wp):
    """release 가 readonly 슬롯이면 ReadonlySlotNotLeasable(무소유 공유 자산·idle 화→alloc 탈취 방지·T-0358)."""
    _seed_readonly(wp)
    git = FakeGit()
    with pytest.raises(wp.ReadonlySlotNotLeasable) as ei:
        wp.release("work/A_1", git_runner=git)
    assert ei.value.op == "release"
    # 장부 미변경 — 여전히 leased·role readonly(idle 화 안 됨).
    after = wp.list_leases()[0]
    assert after.state == "leased" and after.role == "readonly"


def test_force_release_rejected_on_readonly(wp):
    """force_release(강제 백스톱)도 readonly 슬롯이면 거부(release 동형·⑬)."""
    _seed_readonly(wp)
    git = FakeGit()
    with pytest.raises(wp.ReadonlySlotNotLeasable) as ei:
        wp.force_release("work/A_1", git_runner=git)
    assert ei.value.op == "force-release"


def test_bind_slot_rejected_on_readonly(wp):
    """bind_slot 이 기존 readonly 슬롯을 가리키면 거부(점유≠조회 지칭·/pm-bootstrap 오지정 방어·⑬)."""
    _seed_readonly(wp)
    git = FakeGit()
    with pytest.raises(wp.ReadonlySlotNotLeasable) as ei:
        wp.bind_slot("work/A_1", "A", "A_1", git_runner=git)
    assert ei.value.op == "bind"
    # role 유실 없음 — 여전히 readonly·무소유(leased 로 덮이지 않음).
    after = wp.list_leases()[0]
    assert after.role == "readonly" and after.session == ""


def test_lease_ops_allowed_on_work_slot_sensitivity(wp):
    """sensitivity 대조 — work 슬롯엔 release/force_release/bind_slot 거부가 안 걸린다(readonly 만 예외)."""
    # release: work 슬롯 정상 idle 화.
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="me", pid=os.getpid(),
                       started="t", state="leased"))
    git = FakeGit()
    released = wp.release("work/A_1", git_runner=git)
    assert released.state == "idle"
    # bind_slot: work 슬롯 정상 바인딩.
    _seed(wp, wp.Lease(slot="work/A_2", repo="A", session="", pid=0, started="t", state="idle"))
    bound = wp.bind_slot("work/A_2", "A", "A_2", git_runner=git)
    assert bound.state == "leased" and bound.session == "A_2"


# ── CLI: refresh / readonly mutation surface (⑬·T-0358·§F11) ─────────────────


def test_main_refresh_dispatches_to_refresh_backbone(wp, monkeypatch, capsys):
    """CLI — `refresh <slot> --onto <b>` 이 refresh 백본을 정규화 슬롯/onto 로 호출하고 rc 0(⑬)."""
    seen = {}

    def spy_refresh(slot, *, onto=None, git_runner=None):
        seen.update(slot=slot, onto=onto)
        return "origin/main"

    monkeypatch.setattr(wp, "refresh", spy_refresh)
    rc = wp.main(["refresh", "A_1", "--onto", "main"])
    assert rc == 0
    assert seen == {"slot": "work/A_1", "onto": "main"}
    assert "refresh" in capsys.readouterr().out


def test_main_refresh_dirty_refused_returns_rc1(wp, monkeypatch, capsys):
    """CLI — refresh 가 RefreshRefused(dirty) 면 rc 1 + stderr loud(조용히 reset 금지·⑬)."""
    def boom(*a, **k):
        raise wp.RefreshRefused("work/A_1", "dirty")
    monkeypatch.setattr(wp, "refresh", boom)
    rc = wp.main(["refresh", "A_1"])
    assert rc == 1
    assert "미커밋" in capsys.readouterr().err


def test_main_set_base_readonly_surfaces_rc1(wp, capsys):
    """CLI — set-base 가 readonly 슬롯이면 ReadonlySlotMutation → rc 1 + stderr 안내(⑬·T-0358)."""
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="", pid=0, started="t",
                       state="leased", role="readonly"))
    rc = wp.main(["set-base", "A_1", "origin/main"])
    assert rc == 1
    assert "readonly" in capsys.readouterr().err


def test_main_status_surfaces_role(wp, monkeypatch, capsys):
    """CLI — status 가 슬롯 role(readonly)을 surface(⑬·T-0358)."""
    _seed(wp, wp.Lease(slot="work/A_1", repo="A", session="", pid=0, started="t",
                       state="leased", role="readonly"))
    monkeypatch.setattr(wp, "slot_git_status", lambda slot, **k: {
        "slot": slot, "base": {"branch": "main", "commit": "c0"}, "branch": None,
        "head": "h0", "behind": None, "behind_reason": "미기록"})
    rc = wp.main(["status", "A_1"])
    assert rc == 0
    assert "role:" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════════════
# T-0359 — status(submodule/dirty·단일/일괄) + rebase(선-검사·충돌 그대로·장부 원자·§F10·⑩)
# ════════════════════════════════════════════════════════════════════════

_REBASED_BASE_TIP = "d" * 40   # rebase 후 base 브랜치 tip(성공 장부 갱신 검증값).
_REBASED_HEAD = "e" * 40       # rebase 후 슬롯 HEAD sha.


class _RebaseGit:
    """rebase/status 커스텀 runner — fetch·rebase·rev-parse(--verify/--git-path)·submodule·dirty 모델.

    `rebase_rc`(단일) 또는 `rebase_seq`(호출 순서별 rc 리스트·일괄 독립성 검증) 로 rebase 결과를
    모델링한다. `sub_detached` = submodule 이 detached(=drift 후보·symbolic-ref rc≠0)인지."""

    def __init__(self, *, dirty=False, head="feat", head_sha=_REBASED_HEAD,
                 fetch_rc=0, origin_has_base=True, rebase_rc=0, rebase_seq=None,
                 tips=None, subs="", sub_detached=False):
        self.dirty = dirty
        self.head = head
        self.head_sha = head_sha
        self.fetch_rc = fetch_rc
        self.origin_has_base = origin_has_base
        self.rebase_rc = rebase_rc
        self.rebase_seq = list(rebase_seq) if rebase_seq is not None else None
        self.tips = tips or {}
        self.subs = subs
        self.sub_detached = sub_detached
        self.calls: list[list] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:1] == ["-C"]:                       # submodule 컨텍스트(-C <sub> ...).
            sub_argv = argv[2:]
            if sub_argv[:1] == ["symbolic-ref"]:
                return (1, "detached\n") if self.sub_detached else (0, "refs/heads/x\n")
            return (0, "")                           # -C <sub> status --porcelain 등 → clean.
        if argv[:2] == ["rev-parse", "--git-path"]:
            return (0, f".git/{argv[-1]}\n")         # 경로만 반환·실재는 fs(미생성=False).
        if argv[:2] == ["rev-parse", "--verify"]:
            ref = argv[-1]
            key = ref[: -len("^{commit}")] if ref.endswith("^{commit}") else ref
            sha = self.tips.get(key)
            return (0, sha + "\n") if sha else (128, "fatal: bad revision\n")
        if argv == ["rev-parse", "HEAD"]:
            return (0, self.head_sha + "\n")
        if argv == ["symbolic-ref", "HEAD"]:
            # T-0377: full ref 반환 — current_branch 가 refs/heads/ 접두를 벗긴다.
            return (0, "refs/heads/" + self.head + "\n") if self.head else (1, "detached\n")
        if argv == ["submodule", "status"]:
            return (0, self.subs)
        if argv[:2] == ["status", "--porcelain"]:
            return (0, " M f\n") if self.dirty else (0, "")
        if argv[:2] == ["fetch", "origin"]:
            return (self.fetch_rc, "" if self.fetch_rc == 0 else "fatal: could not fetch\n")
        if argv[:3] == ["show-ref", "--verify", "--quiet"]:
            return (0, "") if self.origin_has_base else (1, "")
        if argv[:1] == ["rebase"]:
            if self.rebase_seq is not None:
                rc = self.rebase_seq.pop(0) if self.rebase_seq else 0
            else:
                rc = self.rebase_rc
            return (rc, "" if rc == 0 else "CONFLICT (content): merge conflict in f\n")
        return (0, "")

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


# ── status — submodule pin/drift + dirty 합류(T-0359 조회 확장) ───────────────


def test_slot_git_status_includes_dirty_and_submodules(wp):
    """slot_git_status 가 dirty(bool) + submodules(list) 를 조회에 합류(T-0359 — wave-2d 확장)."""
    _seed_git_lease(wp, base={"branch": "origin/main", "commit": _BASE_TIP})
    git = _RebaseGit(dirty=True, subs="+deadbeef vendor/x\n", sub_detached=True)
    st = wp.slot_git_status("work/A_1", git_runner=git)
    assert st["dirty"] is True
    assert len(st["submodules"]) == 1
    sub = st["submodules"][0]
    assert sub.path == "vendor/x" and sub.kind == "drift" and sub.warning is True


def test_slot_git_status_clean_no_submodules(wp):
    """dirty=False·submodule 없음 → dirty False·submodules []."""
    _seed_git_lease(wp, base={"branch": "origin/main", "commit": _BASE_TIP})
    git = _RebaseGit(dirty=False, subs="")
    st = wp.slot_git_status("work/A_1", git_runner=git)
    assert st["dirty"] is False and st["submodules"] == []


# ── status(단일/일괄/무인자) ─────────────────────────────────────────────────


def test_status_single_slot_returns_one_row(wp):
    """status(slot=…) → 그 슬롯 하나(role 포함)."""
    _seed_git_lease(wp, base={"branch": "origin/main", "commit": _BASE_TIP}, session="job")
    git = _RebaseGit()
    rows = wp.status(slot="work/A_1", git_runner=git)
    assert len(rows) == 1 and rows[0]["slot"] == "work/A_1" and rows[0]["role"] == "work"


def test_status_task_batch_returns_all_owned(wp):
    """status(task=…) → 그 task 보유 leased 슬롯 전부(idle·타 session 제외)."""
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", session="job", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="job", state="leased"),
          _lease(wp, slot="work/A_3", repo="A", session="job", pid=0, state="idle"),
          _lease(wp, slot="work/B_1", repo="B", session="other", state="leased"))
    git = _RebaseGit()
    rows = wp.status(task="job", git_runner=git)
    assert sorted(r["slot"] for r in rows) == ["work/A_1", "work/A_2"]


def test_status_no_arg_resolves_my_task_slots(wp, monkeypatch):
    """status() 무인자 → 내 task(_default_session) 전 슬롯(spike §F10)."""
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", session="job", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="job", state="leased"))
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    git = _RebaseGit()
    rows = wp.status(git_runner=git)
    assert sorted(r["slot"] for r in rows) == ["work/A_1", "work/A_2"]


# ── rebase — 선-검사(스킵+loud) ──────────────────────────────────────────────


def _seed_rebase_lease(wp, *, slot="work/A_1", repo="A", session="job",
                       base=None, role="work"):
    """rebase 전제 leased 엔트리(git.base 기록·role 지정)."""
    git = {"branch": "feat", "head": _OLD_SHA, "submodules": [], "recorded_at": "t"}
    if base is not None:
        git = {"base": base, **git}
    _seed(wp, wp.Lease(slot=slot, repo=repo, session=session, pid=os.getpid(),
                       started="t", state="leased", git=git, role=role))


def test_rebase_skips_not_owned_slot_loud(wp, monkeypatch):
    """선-검사 — 내 세션 소유(leased) 슬롯이 아니면 스킵(not-owner)·rebase 미시도."""
    _seed_rebase_lease(wp, session="other",
                       base={"branch": "origin/main", "commit": _OLD_SHA})
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    git = _RebaseGit()
    [r] = wp.rebase(["work/A_1"], git_runner=git)
    assert r.outcome == wp.REBASE_SKIPPED and r.reason.startswith("not-owner")
    assert not git.did("rebase")   # rebase 자체를 시도하지 않음


def test_rebase_skips_dirty_slot_loud(wp, monkeypatch):
    """선-검사 — dirty(미커밋)면 스킵(dirty·rebase 는 clean 전제)."""
    _seed_rebase_lease(wp, base={"branch": "origin/main", "commit": _OLD_SHA})
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    git = _RebaseGit(dirty=True)
    [r] = wp.rebase(["work/A_1"], git_runner=git)
    assert r.outcome == wp.REBASE_SKIPPED and r.reason == "dirty"
    assert not git.did("rebase")


def test_rebase_skips_in_progress_slot_loud(wp, monkeypatch):
    """선-검사 — rebase 진행 중이면 스킵(in-progress·먼저 해소)."""
    _seed_rebase_lease(wp, base={"branch": "origin/main", "commit": _OLD_SHA})
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    monkeypatch.setattr(wp, "_rebase_in_progress", lambda slot, **k: True)
    git = _RebaseGit()
    [r] = wp.rebase(["work/A_1"], git_runner=git)
    assert r.outcome == wp.REBASE_SKIPPED and r.reason == "in-progress"
    assert not git.did("rebase")


def test_rebase_skips_readonly_slot(wp, monkeypatch):
    """선-검사 — readonly 공유 슬롯은 mutation 불가(스킵 readonly·refresh 로만)."""
    _seed_rebase_lease(wp, session="", role="readonly",
                       base={"branch": "origin/main", "commit": _OLD_SHA})
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    git = _RebaseGit()
    [r] = wp.rebase(["work/A_1"], git_runner=git)
    assert r.outcome == wp.REBASE_SKIPPED and r.reason == "readonly"


def test_rebase_refuses_unrecorded_base_no_onto(wp, monkeypatch):
    """미기록 base + --onto 없음 → 스킵(no-base·거부·추론 금지·⑪)."""
    _seed_rebase_lease(wp)   # base 미기록
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    git = _RebaseGit()
    [r] = wp.rebase(["work/A_1"], git_runner=git)
    assert r.outcome == wp.REBASE_SKIPPED and r.reason == "no-base"
    assert not git.did("rebase")


# ── rebase — 성공(장부 원자 갱신) / 충돌(그대로 + loud·미갱신) ────────────────


def test_rebase_success_atomically_updates_ledger(wp, monkeypatch):
    """성공 → 장부 원자 갱신(base.commit=새 base tip·head=새 tip·recorded_at)."""
    _seed_rebase_lease(wp, base={"branch": "origin/main", "commit": _OLD_SHA})
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    git = _RebaseGit(rebase_rc=0, tips={"origin/main": _REBASED_BASE_TIP},
                     head_sha=_REBASED_HEAD)
    [r] = wp.rebase(["work/A_1"], git_runner=git)
    assert r.outcome == wp.REBASE_REBASED and r.base == "origin/main"
    assert r.new_head == _REBASED_HEAD
    recorded = next(l for l in wp.list_leases() if l.slot == "work/A_1").git
    assert recorded["base"] == {"branch": "origin/main", "commit": _REBASED_BASE_TIP}
    assert recorded["head"] == _REBASED_HEAD
    assert git.did("fetch", "origin") and git.did("rebase", "origin/main")


def test_rebase_conflict_leaves_state_and_does_not_update_ledger(wp, monkeypatch):
    """충돌 → 그 상태 그대로(엔진 abort 안 함) + 장부 base **미갱신**(미완·§F10)."""
    _seed_rebase_lease(wp, base={"branch": "origin/main", "commit": _OLD_SHA})
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    git = _RebaseGit(rebase_rc=1, tips={"origin/main": _REBASED_BASE_TIP})
    [r] = wp.rebase(["work/A_1"], git_runner=git)
    assert r.outcome == wp.REBASE_CONFLICT and "CONFLICT" in (r.reason or "")
    # 엔진이 임의 abort 하지 않았다.
    assert not git.did("rebase", "--abort")
    # 장부 base.commit 미갱신(옛 _OLD_SHA 유지).
    recorded = next(l for l in wp.list_leases() if l.slot == "work/A_1").git
    assert recorded["base"]["commit"] == _OLD_SHA


def test_rebase_onto_conflict_leaves_ledger_fully_unchanged(wp, monkeypatch):
    """--onto + 충돌 → 장부 **완전 불변**(base branch/commit·head·recorded_at) — onto 를 rebase 이전에
    기록하지 않는다(T-0359 must-fix·no-onto 충돌 경로와 대칭·거짓 base 주장 차단)."""
    _seed_rebase_lease(wp, base={"branch": "origin/main", "commit": _OLD_SHA})
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    # onto=develop 는 해소되나(검증 통과) rebase 는 충돌(rc1) — 장부는 옛 origin/main 그대로여야.
    git = _RebaseGit(rebase_rc=1, origin_has_base=True,
                     tips={"develop": _REBASED_BASE_TIP, "origin/develop": _REBASED_BASE_TIP})
    [r] = wp.rebase(["work/A_1"], onto="develop", git_runner=git)
    assert r.outcome == wp.REBASE_CONFLICT
    assert not git.did("rebase", "--abort")   # 엔진 abort 안 함
    recorded = next(l for l in wp.list_leases() if l.slot == "work/A_1").git
    # base 는 develop 로 조용히 기록되지 않았다 — 옛 origin/main@_OLD_SHA 완전 보존.
    assert recorded["base"] == {"branch": "origin/main", "commit": _OLD_SHA}
    assert recorded["head"] == _OLD_SHA and recorded["recorded_at"] == "t"


def test_rebase_onto_resolves_and_records_base(wp, monkeypatch):
    """--onto 명시 → 그 기준으로 진행 + base 기록(gate 소비·resolve_rebase_base)."""
    _seed_rebase_lease(wp)   # base 미기록 — onto 로 해소
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    git = _RebaseGit(rebase_rc=0, origin_has_base=True,
                     tips={"develop": _REBASED_BASE_TIP, "origin/develop": _REBASED_BASE_TIP},
                     head_sha=_REBASED_HEAD)
    [r] = wp.rebase(["work/A_1"], onto="develop", git_runner=git)
    assert r.outcome == wp.REBASE_REBASED and r.base == "develop"
    assert git.did("rebase", "origin/develop")   # origin/<base> 최신 타깃
    recorded = next(l for l in wp.list_leases() if l.slot == "work/A_1").git
    assert recorded["base"]["branch"] == "develop"


# ── rebase — 일괄 독립성(한 충돌이 나머지를 안 막음) ─────────────────────────


def test_rebase_batch_independent_one_conflict_does_not_block_rest(wp, monkeypatch):
    """일괄 — 첫 슬롯 충돌이 나머지 rebase 를 막지 않는다(슬롯 독립·§F10)."""
    def _mk(slot):
        return wp.Lease(slot=slot, repo="A", session="job", pid=os.getpid(),
                        started="t", state="leased",
                        git={"base": {"branch": "origin/main", "commit": _OLD_SHA},
                             "branch": "feat", "head": _OLD_SHA, "submodules": [],
                             "recorded_at": "t"})
    _seed(wp, _mk("work/A_1"), _mk("work/A_2"))
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    # 첫 rebase 호출=충돌(rc1)·두번째=성공(rc0) — 슬롯-무관 순서 모델.
    git = _RebaseGit(rebase_seq=[1, 0], tips={"origin/main": _REBASED_BASE_TIP},
                     head_sha=_REBASED_HEAD)
    results = wp.rebase(["work/A_1", "work/A_2"], git_runner=git)
    outcomes = {r.slot: r.outcome for r in results}
    assert outcomes["work/A_1"] == wp.REBASE_CONFLICT
    assert outcomes["work/A_2"] == wp.REBASE_REBASED   # 앞 충돌이 안 막음
    # A_2 만 장부 갱신(A_1 은 충돌이라 미갱신).
    leases = {l.slot: l.git["base"]["commit"] for l in wp.list_leases()}
    assert leases["work/A_1"] == _OLD_SHA and leases["work/A_2"] == _REBASED_BASE_TIP


# ── _rebase_in_progress (git-path + fs 실재) ─────────────────────────────────


def test_rebase_in_progress_detects_rebase_merge_dir(wp):
    """.git/rebase-merge 실재 → 진행 중(True)·부재 → False."""
    p = wp.slot_path("work/A_1")
    (p / ".git").mkdir(parents=True, exist_ok=True)
    git = _RebaseGit()
    assert wp._rebase_in_progress("work/A_1", git_runner=git) is False
    (p / ".git" / "rebase-merge").mkdir()
    assert wp._rebase_in_progress("work/A_1", git_runner=git) is True


# ── CLI(main) — status 일괄 / rebase ─────────────────────────────────────────


def test_cli_status_task_batch_lists_all(wp, proj, monkeypatch, capsys):
    """CLI `status --task <이름>` — 보유 슬롯 전부 렌더."""
    _seed(wp,
          _lease(wp, slot="work/A_1", repo="A", session="job", state="leased"),
          _lease(wp, slot="work/A_2", repo="A", session="job", state="leased"))
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: _RebaseGit())
    rc = wp.main(["status", "--task", "job"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "work/A_1" in out and "work/A_2" in out


def test_cli_status_slot_and_task_conflict_rc1(wp, capsys):
    """CLI status — `<slot>` + `--task` 동시는 rc 1(배타)."""
    rc = wp.main(["status", "work/A_1", "--task", "job"])
    assert rc == 1
    assert "하나만" in capsys.readouterr().err


def test_cli_rebase_single_success_rc0(wp, proj, monkeypatch, capsys):
    """CLI `rebase <slot>` 성공 → rc 0 + '성공' + 장부 갱신(실경로 wiring)."""
    _seed_rebase_lease(wp, base={"branch": "origin/main", "commit": _OLD_SHA})
    _mk_slot_dir(wp, "work/A_1")
    git = _RebaseGit(rebase_rc=0, tips={"origin/main": _REBASED_BASE_TIP}, head_sha=_REBASED_HEAD)
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: git)
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    rc = wp.main(["rebase", "work/A_1"])
    assert rc == 0
    assert "성공" in capsys.readouterr().out
    recorded = next(l for l in wp.list_leases() if l.slot == "work/A_1").git
    assert recorded["base"]["commit"] == _REBASED_BASE_TIP


def test_cli_rebase_single_conflict_rc1_loud(wp, proj, monkeypatch, capsys):
    """CLI rebase 단일 충돌 → rc 1 + stderr loud(그 상태 그대로·미갱신)."""
    _seed_rebase_lease(wp, base={"branch": "origin/main", "commit": _OLD_SHA})
    _mk_slot_dir(wp, "work/A_1")
    git = _RebaseGit(rebase_rc=1, tips={"origin/main": _REBASED_BASE_TIP})
    monkeypatch.setattr(wp, "_real_git_runner", lambda cwd: git)
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    rc = wp.main(["rebase", "work/A_1"])
    assert rc == 1
    assert "충돌" in capsys.readouterr().err


def test_cli_rebase_no_target_rc1(wp, capsys):
    """CLI rebase — 대상 미지정(슬롯도 --task 도 없음)이면 rc 1 안내."""
    rc = wp.main(["rebase"])
    assert rc == 1
    assert "대상을 지정" in capsys.readouterr().err


def test_cli_rebase_batch_summary_rc0(wp, proj, monkeypatch, capsys):
    """CLI `rebase --task <이름>` — 일괄 성공 요약 + rc 0."""
    def _mk(slot):
        return wp.Lease(slot=slot, repo="A", session="job", pid=os.getpid(),
                        started="t", state="leased",
                        git={"base": {"branch": "origin/main", "commit": _OLD_SHA},
                             "branch": "feat", "head": _OLD_SHA, "submodules": [],
                             "recorded_at": "t"})
    _seed(wp, _mk("work/A_1"), _mk("work/A_2"))
    _mk_slot_dir(wp, "work/A_1")
    _mk_slot_dir(wp, "work/A_2")
    monkeypatch.setattr(wp, "_real_git_runner",
                        lambda cwd: _RebaseGit(rebase_rc=0, tips={"origin/main": _REBASED_BASE_TIP},
                                               head_sha=_REBASED_HEAD))
    monkeypatch.setattr(wp, "_default_session", lambda: "job")
    rc = wp.main(["rebase", "--task", "job"])
    assert rc == 0
    assert "요약" in capsys.readouterr().out


def test_cli_rebase_slot_and_task_conflict_rc1(wp, capsys):
    """CLI rebase — `<slot>` + `--task` 동시는 rc 1(배타)."""
    rc = wp.main(["rebase", "work/A_1", "--task", "job"])
    assert rc == 1
    assert "하나만" in capsys.readouterr().err
