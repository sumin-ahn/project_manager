"""부트스트랩 0단계 검증 (⑧·spike §F1b·[[T-0351]]) 단위 테스트.

0단계 = dump/alloc 을 뿌리기 *전에* "올바른 슬롯/위치인가"를 기계 검증(사용자: "제일 먼저 체크할
건 내가 올바른 슬롯을 쓰고 있나"). 실패 시 **부분 dump 도 금지**(dump 가 뜨면 PM 이 그것을 세션
진실로 믿는다). worktree_pool 은 **DI mock** 으로 주입해 hermetic — 실 리스 장부·git·work/ 풀
미접촉(test_pm_bootstrap_lease 의 DI 패턴 동류).

검증 축:
  - 엔진 앵커(무조건): worktree 사본이면 거부 + 부분 dump 금지 · PM 홈이면 통과 · REPO 로 소비.
  - solo(슬롯 없음) 자연 no-op: 앵커만 · 슬롯 검사(풀) 미진입.
  - 작업공간 실재: 장부·폴더 부재면 거부.
  - 타 점유자: 다른 세션 leased 면 거부 · idle/내 세션이면 통과 · readonly(⑬)는 bind(점유) 거부(should-fix).
  - main-참조(보호브랜치 직접 checkout / origin-추적 upstream): **진입 거부**(rc 1·부분 dump 금지) +
    해소 2택 실값(readonly 생성 / 작업 브랜치 전환) · readonly role 예외(§F11).
  - 기록 vs live: compare_slot_git 소비 — fail_loud=거부 / 미기록=loud+통과 / ok=통과 / submodule drift=warn.
  - 재구현 아님: compare_slot_git 을 실제로 호출(소비)한다.
  - sensitivity: 각 거부 배선을 무력화하면 통과로 뒤집힌다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from _home_slot import seed_home_slot

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bootstrap():
    return _load("pm_bootstrap")


# ── 대역 (board·pool·lease·compare) ──────────────────────────────────────────


class _FakeBoard:
    """board 대역 — 엔진 앵커(`_pm_home_worktree_misanchor`) + 보호브랜치(`_repo_protected`).

    `anchor_pm_home` 이 None 이 아니면 anchor 가 REPO 를 등록 worktree 사본으로 판정(거부 유발).
    anchor 호출 인자를 기록해 "REPO 로 소비"를 검증한다."""

    def __init__(self, *, anchor_pm_home=None, protected=None):
        self._anchor_pm_home = anchor_pm_home
        self._protected = protected if protected is not None else ["main", "master", "develop"]
        self.anchor_calls: list = []

    def _pm_home_worktree_misanchor(self, anchor, **_kw):
        self.anchor_calls.append(anchor)
        return self._anchor_pm_home

    def _repo_protected(self, repo):
        return self._protected


class _FakeLease:
    def __init__(self, slot, repo, branch=None):
        self.slot = slot
        self.repo = repo
        self.branch = branch


class _LeaseEntry:
    """장부 엔트리 대역 — 0단계는 slot/state/session/role 을 읽는다(실재·점유·readonly).

    `role` = canonical 슬롯 role(T-0358 이 `Lease.role` 로 승격) — `_phase0_is_readonly` 가
    **`lease.role`** 을 직접 읽는다(extra 아님·extra 승격 파급 seam #1)."""

    def __init__(self, slot, *, state="idle", session="", role="work", extra=None):
        self.slot = slot
        self.state = state
        self.session = session
        self.role = role
        self.extra = extra or {}


class _FakeCompare:
    """compare_slot_git 결과 대역 (T-0350 GitCompareResult 소비 표면·0단계 record-vs-live)."""

    def __init__(self, *, fail_loud=False, unrecorded=False, head_relation="match",
                 submodule_drift=None, recorded=None, live=None, branch_match=True):
        self.fail_loud = fail_loud
        self.unrecorded = unrecorded
        self.head_relation = head_relation
        self.branch_match = branch_match
        self.submodule_drift = submodule_drift or []
        self.recorded = recorded or {}
        self.live = live or {}


class _FakeSlotStatus:
    """slot_status 결과 대역 — 0단계 origin-추적 거부는 `upstream_ok`·`upstream` 을 소비 (T-0276·§F9 축 2).

    `upstream` = `@{upstream}` 해소명(예 `origin/main`·`origin/a5`) — 보호 브랜치 원격만 거부한다."""

    def __init__(self, *, upstream_ok=False, upstream=None):
        self.upstream_ok = upstream_ok
        self.upstream = upstream


class _NeedsCreate(Exception):
    def __init__(self, repo):
        self.repo = repo
        super().__init__(repo)


class _FakePool:
    """worktree_pool DI mock — 0단계 슬롯 검사(실재·점유·기록정합)를 구동한다(실 부작용 0).

    `HOME_SLOT` 은 실 풀이 이름 붙인 상수의 미러(`NeedsCreate` 동형) — 작업 슬롯 전제 검사
    (main-참조·기록↔live)가 PM 홈 행을 이 값으로 배제한다."""

    HOME_SLOT = "."

    def __init__(self, *, leases=None, branch="feature-x", compare_result=None,
                 upstream_ok=False, upstream=None):
        self.NeedsCreate = _NeedsCreate
        self._leases = list(leases or [])
        self._branch = branch
        self._compare_result = compare_result
        self._upstream_ok = upstream_ok
        self._upstream = upstream
        self.bind_calls: list[str] = []
        self.compare_calls: list[str] = []
        self.list_leases_calls = 0

    def list_leases(self):
        self.list_leases_calls += 1
        return list(self._leases)

    def slot_path(self, slot):
        return Path("/tmp/multipm-phase0-absent") / slot  # 미존재(실재 검사=장부 시드로만 통과)

    def current_branch(self, slot, *, git_runner=None):
        return self._branch

    def bind_slot(self, slot, repo, session, *, git_runner=None):
        self.bind_calls.append(slot)
        return _FakeLease(slot, repo, self._branch)

    def alloc(self, repo, *, branch=None, resume=None, **_kw):
        return _FakeLease("work/%s_1" % repo, repo, self._branch)

    def slot_status(self, slot, *, git_runner=None):
        return _FakeSlotStatus(upstream_ok=self._upstream_ok, upstream=self._upstream)

    def compare_slot_git(self, slot, *, git_runner=None):
        self.compare_calls.append(slot)
        return self._compare_result


class _OldPool(_FakePool):
    """compare_slot_git 미구현 풀(구버전) — record-vs-live 가 getattr None 으로 no-op 함을 검증."""

    compare_slot_git = None  # 속성 자체를 제거(getattr → None → 정합 검사 skip)


def _make(bootstrap, tmp_path, *, board, worktree_pool=None, git_fn=None):
    """격리된 PmBootstrap — board/git/log/pm_state/areas hermetic stub, board/pool 주입.

    `git_fn` 주입 시 그 git 러너를 쓴다(미기록 후보 merge-base 모델·T-0352). None 이면 기본
    fake_git(merge-base 는 (0,"") → 후보 미해소·기존 테스트 무영향)."""
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("| repo | prefix |\n|---|---|\n| X | X |\n", encoding="utf-8")
    pm_state_file = tmp_path / "pm_state.md"
    pm_state_file.write_text("", encoding="utf-8")

    def fake_board(args):
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        return 0, "  [open   ] T-0001  x  pm  tag\n"

    def fake_git(args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 subj\n"
        if "show-ref" in args:
            # remedy 브랜치 후보 존재 검사(T-0412) — 이 대역 슬롯엔 브랜치가 없다(rc≠0).
            return 1, ""
        return 0, ""

    return bootstrap.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨")),
        run_git_fn=git_fn or fake_git,
        log_file=log_file,
        areas_file=areas_file,
        worktree_pool=worktree_pool,
        board=board,
        pm_state_file=pm_state_file,
    )


# ── 1. 엔진 앵커 (무조건) ─────────────────────────────────────────────────────


def test_anchor_worktree_copy_rejects_before_dump(bootstrap, tmp_path, capsys):
    """엔진 앵커가 worktree 사본(등록 pm_home 하위)이면 거부 — rc 1 · **부분 dump 금지**(stdout 0)."""
    board = _FakeBoard(anchor_pm_home="/home/x/pm_home")
    inst = _make(bootstrap, tmp_path, board=board)
    rc = inst.run()  # solo — 앵커는 무조건이라 solo 도 검사된다.
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == "", "0단계 거부인데 부분 dump 가 떴다(PM 이 세션 진실로 오신뢰)"
    assert "0단계" in cap.err and "worktree" in cap.err
    assert "/home/x/pm_home" in cap.err  # 해소 안내(PM 홈 경로)


def test_anchor_consumes_pm_home_guard_with_repo(bootstrap, tmp_path, capsys):
    """앵커는 board T-0345 가드(`_pm_home_worktree_misanchor`)를 REPO 로 **소비**한다(재구현 아님)."""
    board = _FakeBoard(anchor_pm_home="/home/x/pm_home")
    inst = _make(bootstrap, tmp_path, board=board)
    inst.run()
    assert board.anchor_calls == [bootstrap.REPO], "앵커 판정을 REPO 로 board 가드에 위임하지 않음"


def test_anchor_pm_home_passes(bootstrap, tmp_path, capsys):
    """엔진 앵커가 PM 홈(misanchor None)이면 통과(rc 0·정상 dump)."""
    board = _FakeBoard(anchor_pm_home=None)
    inst = _make(bootstrap, tmp_path, board=board)
    rc = inst.run()  # solo
    assert rc == 0
    assert capsys.readouterr().out != ""  # 정상 dump.


def test_anchor_board_absent_failsoft_passes(bootstrap, tmp_path, monkeypatch, capsys):
    """board(또는 헬퍼) 부재면 앵커 검사 생략(fail-soft·오탐 0) — 솔로/standalone 무영향."""
    monkeypatch.setattr(bootstrap, "_load_board", lambda: None)
    inst = _make(bootstrap, tmp_path, board=None)  # self._board None → _load_board None → skip.
    assert inst.run() == 0


class _RaisingBoard:
    """앵커 가드가 **내부 raise** 하는 board 대역 — phase0 fail-soft(오탐 0) 백스톱 검증용."""

    def _pm_home_worktree_misanchor(self, anchor, **_kw):
        raise RuntimeError("가드 내부 버그(예: git 호출 예외)")

    def _repo_protected(self, repo):
        return []


def test_anchor_guard_internal_raise_failsoft_passes(bootstrap, tmp_path, capsys):
    """앵커 가드(board 헬퍼)가 **내부 raise** 하면 fail-soft 통과(board 부재만이 아니라 raise 경로도).

    phase0 헬퍼의 `except Exception: return <통과값>`(의도적·오탐 0)이 헬퍼 내부 버그를 silent-pass
    로 잡아먹지 않고 *통과값*을 돌려주는지 백스톱한다 — dump 정상·rc 0(거부 아님)."""
    inst = _make(bootstrap, tmp_path, board=_RaisingBoard())
    rc = inst.run()  # solo — 앵커만.
    assert rc == 0, "가드 raise 를 fail-soft(통과)로 흡수하지 못함"
    assert capsys.readouterr().out != ""  # 정상 dump(거부 아님).


# ── 2. solo 자연 no-op (앵커만 무조건·슬롯 검사 미진입) ────────────────────────


def test_solo_no_op_skips_slot_checks(bootstrap, tmp_path, capsys):
    """solo(repo 없음)는 슬롯 검사(풀) 미진입 — 앵커만. 주입 풀의 list_leases 가 안 불린다."""
    board = _FakeBoard(anchor_pm_home=None)
    pool = _FakePool()
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run()  # solo — repo/slot None.
    assert rc == 0
    assert pool.list_leases_calls == 0, "solo 인데 슬롯 검사(list_leases)가 돌았다(자연 no-op 위반)"
    assert pool.compare_calls == []


# ── 3. 작업공간 실재 ──────────────────────────────────────────────────────────


def test_missing_workspace_rejects(bootstrap, tmp_path, capsys):
    """lean 슬롯이 장부에도 폴더에도 없으면 거부(phantom 바인딩 방지) — rc 1 · dump 없음."""
    board = _FakeBoard(anchor_pm_home=None)
    pool = _FakePool(leases=[])  # 장부 비어있음 + slot_path 미존재.
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "0단계" in cap.err and "work/X_2" in cap.err
    assert pool.bind_calls == [], "실재 거부인데 bind_slot 이 불렸다(phantom 바인딩)"


def test_present_idle_workspace_passes(bootstrap, tmp_path, capsys):
    """슬롯이 idle 리스로 장부에 실재하면 통과(bind 진행)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")])
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    assert pool.bind_calls == ["work/X_2"]


# ── 4. 타 점유자 (readonly carve-out) ─────────────────────────────────────────


def test_other_session_holder_rejects(bootstrap, tmp_path, capsys):
    """다른 세션이 그 슬롯을 leased 로 점유 중이면 거부(결정 ③) — rc 1 · dump 없음 · 점유자 안내."""
    board = _FakeBoard(anchor_pm_home=None)
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="leased", session="X_9")])
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "X_9" in cap.err and "점유" in cap.err
    assert pool.bind_calls == []


def test_my_session_holder_passes(bootstrap, tmp_path, capsys):
    """내 세션(`X_2`)이 이미 leased(crash 후 재개)면 점유 아님 — 통과."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="leased", session="X_2")])
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    assert inst.run(repo="X", slot=2) == 0


def test_readonly_slot_bind_refused(bootstrap, tmp_path, capsys):
    """readonly 슬롯(⑬·role="readonly")은 **바인딩(점유) 거부** — 무소유 공유 자산(should-fix·T-0358).

    0단계 carve-out(F6)은 *조회 지칭*만 허용하고 bind 는 *점유*라 의미가 다르다 — `/pm-bootstrap
    --slot N` 오지정을 fail-loud 로 막는다(bind_slot 엔진 `ReadonlySlotNotLeasable` 의 user-facing 짝)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    pool = _FakePool(leases=[
        _LeaseEntry("work/X_2", state="leased", session="", role="readonly")
    ])
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1, "readonly 슬롯 바인딩이 통과됐다(점유 거부 미적용)"
    assert "readonly" in capsys.readouterr().err
    assert pool.bind_calls == [], "readonly bind 거부인데 bind_slot 이 불렸다"


# ── 4b. 불완전 생성(creating·T-0295) — 세션·readonly 무관 차단 (must-fix·codex) ──


@pytest.mark.parametrize("session", ["X_2", "X_9", ""])
def test_creating_slot_rejects_regardless_of_session(bootstrap, tmp_path, capsys, session):
    """creating(불완전 생성) 슬롯은 **세션 동일 여부 무관** FAIL-LOUD — 부분 dump 금지·bind 안 함.

    내-세션 creating(내 중단 흔적)·타-세션 creating(in-flight)·미상 session 모두 차단(슬롯이 불완전한
    건 동일). bind_slot 이 creating 을 무조건 leased 로 덮어 훼손하는 클래스를 진입 전에 막는다."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="creating", session=session)])
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1, f"creating(session={session!r}) 을 통과시켰다(불완전 슬롯 진입 허용)"
    cap = capsys.readouterr()
    assert cap.out == "", "creating 거부인데 부분 dump 가 떴다"
    assert "creating" in cap.err and "0단계" in cap.err
    assert pool.bind_calls == [], "creating 거부인데 bind_slot 이 불렸다(in-flight create 훼손)"


def test_creating_slot_not_exempted_by_readonly(bootstrap, tmp_path, capsys):
    """readonly 라도 creating 이면 차단 — readonly 는 점유/보호 예외지 *불완전 생성* 예외가 아니다.

    반쯤 만들어진 슬롯은 readonly 여도 못 쓴다(codex 지적) — carve-out 이 creating 을 살려주면 안 된다."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    pool = _FakePool(leases=[
        _LeaseEntry("work/X_2", state="creating", session="X_9", role="readonly")
    ])
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    assert inst.run(repo="X", slot=2) == 1, "readonly carve-out 이 creating(불완전 생성)을 통과시켰다"
    assert capsys.readouterr().out == ""


def test_leased_and_idle_states_not_treated_as_creating(bootstrap, tmp_path, capsys):
    """creating 검사 sensitivity — idle·leased(내 세션 재개)는 creating 아님(기존 거동 회귀 유지)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    for state, session in (("idle", ""), ("leased", "X_2")):
        pool = _FakePool(leases=[_LeaseEntry("work/X_2", state=state, session=session)])
        inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
        assert inst.run(repo="X", slot=2) == 0, f"{state} 를 creating 으로 오차단"


# ── 5. main-참조(보호브랜치/origin-추적) = 진입 거부 (T-0360·⑧·§F9·부분 dump 금지) ──


def test_protected_branch_rejects_before_dump(bootstrap, tmp_path, capsys):
    """보호 브랜치(main) 직접 체크아웃 = **진입 거부**(rc 1·부분 dump 금지) — warn→거부 승격(T-0360)."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main"])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], branch="main")
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1, "보호브랜치 main 직접 checkout 이 거부되지 않았다(warn→거부 승격 미적용)"
    cap = capsys.readouterr()
    assert cap.out == "", "거부인데 부분 dump 가 떴다(0단계 계약 위반)"
    assert "보호 브랜치" in cap.err and "main" in cap.err
    assert pool.bind_calls == [], "거부인데 bind_slot 이 불렸다(부분 dump)"


def test_protected_upstream_tracking_rejects_before_dump(bootstrap, tmp_path, capsys):
    """보호브랜치 원격(origin/main) origin-추적 = **진입 거부**(rc 1·§F9 축 2) — `main`+`origin/main` 슬롯 concern.

    브랜치는 비보호(feature-x)여도 upstream 이 보호 브랜치 원격(origin/main)을 가리키면 main-참조로 거부."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main"])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")],
                     branch="feature-x", upstream_ok=True, upstream="origin/main")
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1, "보호브랜치 원격(origin/main) origin-추적 슬롯이 거부되지 않았다(§F9 축 2 미적용)"
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "origin-추적" in cap.err and "origin/main" in cap.err
    assert pool.bind_calls == []


def test_feature_branch_upstream_not_rejected(bootstrap, tmp_path, capsys):
    """자기 feature 브랜치 추적(origin/a5)은 정상 작업 슬롯 — 거부 안 함 (오탐 0·sensitivity).

    T-0273/0274 로 슬롯이 자기 workstream(origin/<branch>)을 추적하는 건 정상이다 — §F9 concern 은
    `origin/main` 추적뿐이라 임의 upstream 을 막으면 정상 슬롯을 깬다(회귀 방어)."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main"])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")],
                     branch="a5", upstream_ok=True, upstream="origin/a5")
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0, "자기 feature 브랜치 추적(origin/a5) 슬롯을 잘못 거부했다(오탐)"
    assert "origin-추적" not in capsys.readouterr().err
    assert pool.bind_calls == ["work/X_2"]


def test_reject_message_has_two_resolution_choices(bootstrap, tmp_path, capsys):
    """거부 메시지 = 해소 2택 **실값**(readonly 생성 커맨드 / 작업 브랜치 전환 커맨드) — sensitivity.

    안내 부재 시 채택자가 BREAKING 거부를 어떻게 푸는지 알 수 없다(§F9·DoD sensitivity)."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main"])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], branch="main")
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    inst.run(repo="X", slot=2)
    err = capsys.readouterr().err
    # (a) readonly 슬롯 생성 실값 — repo 치환 포함.
    assert "worktree add X --readonly" in err
    # (b) 작업 브랜치 전환 실값 — slot_id·session 치환 포함. 엔진-매개 단일 커맨드(전환+스냅
    #     재기록 원자·T-0414) — raw `git switch` 는 diverged 2차 차단을 부르므로 안내하지 않는다.
    assert "worktree_pool.py switch work/X_2 X_2" in err
    assert "git -C work/X_2 switch -c" not in err


def test_non_main_reference_slot_passes(bootstrap, tmp_path, capsys):
    """비보호 브랜치 + upstream 미설정(작업 슬롯 정상형)은 거부 없이 통과 (sensitivity — 조건부성 입증)."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main"])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")],
                     branch="feature-x", upstream_ok=False)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    err = capsys.readouterr().err
    assert "보호 브랜치" not in err and "origin-추적" not in err
    assert pool.bind_calls == ["work/X_2"]


def test_readonly_role_exempt_from_main_reference_reject(bootstrap, tmp_path):
    """readonly 슬롯(role="readonly")은 main-참조 거부 예외(§F11·⑬) — main 브랜치여도 거부하지 않는다.

    readonly 는 main-참조 역할을 이전받는 대상이라 그 자체가 거부되면 자기충돌(§F1b 이행 순서). bind
    flow 에선 2c 가 먼저 거부하지만, 판정 함수의 self-consistency 를 직접 검증한다(role carve-out)."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main"])
    pool = _FakePool(branch="main", upstream_ok=True)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    ro_lease = _LeaseEntry("work/X_2", state="idle", role="readonly")
    assert inst._phase0_protected_reject(pool, "X", "work/X_2", "X_2", ro_lease) == 0
    work_lease = _LeaseEntry("work/X_2", state="idle", role="work")
    assert inst._phase0_protected_reject(pool, "X", "work/X_2", "X_2", work_lease) == 1


# ── 6. 기록 vs live 정합 (compare_slot_git 소비·㉒) ──────────────────────────


def test_record_vs_live_fail_loud_rejects(bootstrap, tmp_path, capsys):
    """compare fail_loud(브랜치 변경·head diverged·㉒)면 FAIL-LOUD 거부 — rc 1 · dump 없음."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(fail_loud=True, head_relation="diverged",
                       recorded={"branch": "feat", "head": "aaa"},
                       live={"branch": "feat", "head": "bbb"})
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "0단계" in cap.err and "다릅니다" in cap.err
    assert pool.compare_calls == ["work/X_2"], "compare_slot_git 을 소비(호출)하지 않았다"
    assert pool.bind_calls == []


def test_fail_loud_output_surfaces_reason_and_resync_command(bootstrap, tmp_path, capsys):
    """T-0391 ②: fail_loud 출력이 판정 근거 + 재동기 커맨드 실값(`worktree_pool.py record <slot>`)을 담는다.

    head diverged(같은 브랜치·비후손)면 "후손이 아님" 근거, 그리고 자동 실행 아닌 사용자용 record
    커맨드 실값이 정확한 slot_id 로 제시돼야 한다(PM 78 코드 정독 낭비 폐쇄·감지=기계·해소=사용자)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(fail_loud=True, head_relation="diverged", branch_match=True,
                       recorded={"branch": "feat", "head": "aaa"},
                       live={"branch": "feat", "head": "bbb"})
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1
    err = capsys.readouterr().err
    # 판정 근거 — 같은 브랜치·head 비후손(리셋/되감기) 사유 명시.
    assert "판정 근거" in err
    assert "후손이 아님" in err
    assert "head_relation='diverged'" in err
    # 재동기 커맨드 실값 — CLI 로 노출된 record 서브커맨드·정확한 slot_id·자동 실행 아님.
    assert "worktree_pool.py record work/X_2" in err
    assert "자동 실행 안 함" in err


def test_fail_loud_output_branch_change_reason(bootstrap, tmp_path, capsys):
    """T-0391 ②: branch_match False(브랜치 변경)면 근거가 "브랜치가 바뀜"으로 분기(head 비후손과 구분)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(fail_loud=True, head_relation="diverged", branch_match=False,
                       recorded={"branch": "v1.3.2", "head": "aaa"},
                       live={"branch": "v1.3.3", "head": "bbb"})
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1
    err = capsys.readouterr().err
    assert "브랜치가 바뀜" in err
    assert "v1.3.2" in err and "v1.3.3" in err
    assert "worktree_pool.py record work/X_2" in err


def test_record_vs_live_unrecorded_loud_but_passes(bootstrap, tmp_path, capsys):
    """compare unrecorded(구 슬롯)는 **차단 아님**(rc 0) + loud 표시("미기록") — 질의 훅(T-0352 자리)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(unrecorded=True)
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0, "미기록은 차단이 아니라 loud 표시여야 한다(결정 ⑪)"
    err = capsys.readouterr().err
    assert "미기록" in err and "drift 감지 비활성" in err
    assert pool.compare_calls == ["work/X_2"]


# ── 6b. 미기록 경로 후보 제시 (자동 채택 없음·차단 아님·T-0352·⑪) ─────────────


def _candidate_git(merge_bases: dict):
    """미기록 후보 merge-base 모델 git fn — `merge-base HEAD <br>` → sha(dict) (T-0352 후보 제시).

    `merge_bases` = 후보브랜치→sha. 그 외 표준 fake_git 응답(abbrev-ref·log)도 준다."""
    def git_fn(args):
        if "merge-base" in args and args[-2:-1] == ["HEAD"]:
            br = args[-1]
            sha = merge_bases.get(br)
            return (0, sha + "\n") if sha else (1, "")     # 미등록 후보 → rc≠0(미해소)
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 subj\n"
        return 0, ""
    return git_fn


def test_unrecorded_presents_base_candidates(bootstrap, tmp_path, capsys):
    """미기록 0단계 — merge-base 로 해소되는 후보를 제시(spike §F9 형식·자동 채택 없음·T-0352·⑪)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(unrecorded=True)
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    git_fn = _candidate_git({"origin/main": "df10dc6abc999", "origin/develop": "aa11bb22cc33"})
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool, git_fn=git_fn)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0, "후보 제시는 차단이 아니다(loud 표시만·결정 ⑪)"
    err = capsys.readouterr().err
    assert "미기록" in err and "후보" in err
    assert "origin/main" in err and "df10dc6" in err            # spike §F9 형식(merge-base sha)
    assert "origin/develop" in err                              # 다중 후보 join
    assert "자동 채택 안 함" in err                              # 추론 금지 명시


def test_unrecorded_no_candidate_line_when_none_resolve(bootstrap, tmp_path, capsys):
    """merge-base 가 아무 후보도 못 해소하면 후보 줄 생략(오탐 0·sensitivity) — 미기록 알림은 유지."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(unrecorded=True)
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)  # 기본 fake_git → merge-base (0,"")
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    err = capsys.readouterr().err
    assert "미기록" in err
    assert "후보" not in err, "해소된 후보가 없는데 후보 줄이 떴다(오탐)"


def test_unrecorded_candidate_not_auto_recorded(bootstrap, tmp_path, capsys):
    """후보는 **제시만** — 자동 기록/채택 안 함(set_base·record_git_snapshot 미호출·추론 금지 못박음).

    _FakePool 은 set_base/record_git_snapshot 을 정의하지 않으므로, 0단계가 자동 기록을 시도하면
    AttributeError 로 죽는다 — 통과(rc 0)가 곧 '자동 채택 없음'의 구조적 증거다. 추가로 후보 줄이
    '자동 채택 안 함' 을 명시하는지 확인한다(사용자 결정 위임·결정 ⑪)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(unrecorded=True)
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    git_fn = _candidate_git({"origin/main": "df10dc6abc999"})
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool, git_fn=git_fn)
    assert inst.run(repo="X", slot=2) == 0            # 자동 기록 시도했으면 AttributeError 로 죽었을 것
    assert "자동 채택 안 함" in capsys.readouterr().err
    assert pool.bind_calls == ["work/X_2"]            # 정상 진행(차단 아님)


def test_record_vs_live_ok_passes(bootstrap, tmp_path, capsys):
    """compare ok(match/descendant)면 통과(rc 0·정합 확인)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(head_relation="descendant")  # 후손(㉒ crash 후 재개 notice) = 통과.
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    assert inst.run(repo="X", slot=2) == 0


def test_record_vs_live_submodule_drift_warns_not_blocks(bootstrap, tmp_path, capsys):
    """compare submodule_drift 는 비차단 경고(rc 0 + 재동기 검토 안내)."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(submodule_drift=["sub/a", "sub/b"])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    err = capsys.readouterr().err
    assert "submodule" in err and "sub/a" in err


def test_record_vs_live_old_pool_no_compare_failsoft(bootstrap, tmp_path, capsys):
    """compare_slot_git 미구현 풀(구버전)은 정합 검사 no-op(getattr None·fail-soft) — 통과."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    pool = _OldPool(leases=[_LeaseEntry("work/X_2", state="idle")])
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    assert inst.run(repo="X", slot=2) == 0


# ── 7. 홈 슬롯 예외 — 작업 슬롯 전제 검사(4·5)는 PM 홈에 걸지 않는다 ─────────────
#
# 슬롯을 하나만 쓰는 홈도 장부의 N=1 행(`slot="."`)이라 0단계가 그 행을 검사 대상으로 본다.
# 4(main-참조 거부)·5(기록↔live)는 "작업 슬롯에서 보호 브랜치에 커밋" 을 막는 검사인데, PM 홈은
# board/wiki 가 사는 트리라 그 브랜치가 main/master 인 것이 정상 채택자 형상이다(홈의 보호는 push
# 보호 훅). 배제하지 않으면 채택자의 **첫 부트스트랩**이 rc 1 로 막힌다. 배제 판별은 풀이 이름
# 붙인 상수(`HOME_SLOT`)와의 값 비교 하나뿐이고, 작업 슬롯엔 두 검사가 그대로 발화한다(과배제 0).


def test_home_slot_exempt_from_main_reference_reject(bootstrap, tmp_path):
    """홈 행(`HOME_SLOT`)은 보호 브랜치 직접 체크아웃이어도 통과 · 같은 풀의 작업 슬롯은 거부."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main", "master"])
    pool = _FakePool(branch="master")
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    home_lease = _LeaseEntry(pool.HOME_SLOT, state="leased", session="X_1")
    assert inst._phase0_protected_reject(
        pool, "X", pool.HOME_SLOT, "X_1", home_lease) == 0, (
        "보호 브랜치 홈이 거부됐다 — 채택자 첫 부트스트랩이 막힌다")
    work_lease = _LeaseEntry("work/X_2", state="leased", session="X_2")
    assert inst._phase0_protected_reject(pool, "X", "work/X_2", "X_2", work_lease) == 1, (
        "작업 슬롯의 main-참조 거부까지 배제됐다(과배제)")


def test_home_slot_exempt_from_record_vs_live(bootstrap, tmp_path):
    """홈 행은 기록↔live diverged 여도 통과(compare 미호출) · 같은 풀의 작업 슬롯은 FAIL-LOUD."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    cmp = _FakeCompare(fail_loud=True, head_relation="diverged", branch_match=False,
                       recorded={"branch": "a1", "head": "aaa"},
                       live={"branch": "master", "head": "bbb"})
    pool = _FakePool(compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    assert inst._phase0_record_vs_live(pool, pool.HOME_SLOT) == 0, (
        "홈 행이 기록↔live 로 막혔다 — 4 만 배제하면 브랜치를 옮긴 홈이 여기서 다시 막힌다")
    assert pool.compare_calls == [], "배제인데 compare_slot_git 을 호출했다"
    assert inst._phase0_record_vs_live(pool, "work/X_2") == 1, (
        "작업 슬롯의 기록↔live 거부까지 배제됐다(과배제)")
    assert pool.compare_calls == ["work/X_2"]


def test_home_slot_exception_consumes_pool_constant(bootstrap, tmp_path):
    """배제 판별은 **풀이 이름 붙인 상수**와의 값 비교다 — `"."` 리터럴 하드코딩이 아니다.

    상수 값을 바꾼 풀에서는 그 값이 배제되고 `"."` 은 검사 대상이 된다(재구현/추론 0)."""
    class _RenamedHomePool(_FakePool):
        HOME_SLOT = "home-root"

    board = _FakeBoard(anchor_pm_home=None, protected=["master"])
    pool = _RenamedHomePool(branch="master")
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    lease = _LeaseEntry("home-root", state="leased", session="X_1")
    assert inst._phase0_protected_reject(pool, "X", "home-root", "X_1", lease) == 0
    assert inst._phase0_protected_reject(
        pool, "X", ".", "X_1", _LeaseEntry(".", state="leased", session="X_1")) == 1


def test_old_pool_without_home_slot_constant_keeps_gates(bootstrap, tmp_path):
    """상수 부재 풀(구버전·mock)은 배제가 성립하지 않아 두 검사가 종전대로 돈다(과배제 0)."""
    class _NoConstantPool(_FakePool):
        HOME_SLOT = None  # 상수 미노출 풀 — getattr 기본값과 같은 자리.

    board = _FakeBoard(anchor_pm_home=None, protected=["master"])
    cmp = _FakeCompare(fail_loud=True, head_relation="diverged")
    pool = _NoConstantPool(branch="master", compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    lease = _LeaseEntry(".", state="leased", session="X_1")
    assert inst._phase0_protected_reject(pool, "X", ".", "X_1", lease) == 1
    assert inst._phase0_record_vs_live(pool, ".") == 1


def test_old_pool_without_home_slot_constant_keeps_gates_for_none_slot_id(bootstrap, tmp_path):
    """상수 부재 풀 + `slot_id=None` — 상수 부재와 슬롯 값 None 을 접으면 홈으로 오인된다."""
    class _NoConstantPool(_FakePool):
        # 상수를 실제로 미노출 — getattr 이 기본값(None) 으로 떨어지는 구버전 풀 재현.
        @property
        def HOME_SLOT(self):
            raise AttributeError("HOME_SLOT")

    board = _FakeBoard(anchor_pm_home=None, protected=["master"])
    cmp = _FakeCompare(fail_loud=True, head_relation="diverged")
    pool = _NoConstantPool(branch="master", compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    lease = _LeaseEntry(None, state="leased", session="X_1")
    assert inst._phase0_is_home_slot(pool, None) is False
    assert inst._phase0_protected_reject(pool, "X", None, "X_1", lease) == 1
    assert inst._phase0_record_vs_live(pool, None) == 1


def test_registered_home_bare_bootstrap_passes_phase0_and_dumps(
        bootstrap, tmp_path, monkeypatch, capsys):
    """등록된 홈(N=1 행)의 첫 부트스트랩 — 보호 브랜치·미기록 스냅이어도 rc 0 + 정상 dump.

    채택자 pristine 재현의 hermetic 짝: `pm_import` 직후 홈은 `master` 체크아웃이고 슬롯 git 스냅이
    없다. 두 검사가 걸리면 out-of-box 첫 실행이 rc 1 로 막힌다(이 티켓 §PM 비준)."""
    monkeypatch.setattr(bootstrap, "REPO", tmp_path)
    seed_home_slot(tmp_path, repo="X", session="X_1")
    board = _FakeBoard(anchor_pm_home=None, protected=["main", "master"])
    cmp = _FakeCompare(fail_loud=True, head_relation="diverged", branch_match=False)
    pool = _FakePool(leases=[_LeaseEntry(".", state="leased", session="X_1")],
                     branch="master", compare_result=cmp)
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=1)
    cap = capsys.readouterr()
    assert rc == 0, f"등록된 홈의 첫 부트스트랩이 막혔다: {cap.err}"
    assert cap.out != "", "rc 0 인데 dump 가 비었다"
    assert "0단계" not in cap.err
    assert pool.bind_calls == ["."], "홈 행 경로(장부 값)로 바인딩하지 않았다"

