"""부트스트랩 0단계 검증 (⑧·spike §F1b·[[T-0351]]) 단위 테스트.

0단계 = dump/alloc 을 뿌리기 *전에* "올바른 슬롯/위치인가"를 기계 검증(사용자: "제일 먼저 체크할
건 내가 올바른 슬롯을 쓰고 있나"). 실패 시 **부분 dump 도 금지**(dump 가 뜨면 PM 이 그것을 세션
진실로 믿는다). worktree_pool 은 **DI mock** 으로 주입해 hermetic — 실 리스 장부·git·work/ 풀
미접촉(test_pm_bootstrap_lease 의 DI 패턴 동류).

검증 축:
  - 엔진 앵커(무조건): worktree 사본이면 거부 + 부분 dump 금지 · PM 홈이면 통과 · REPO 로 소비.
  - solo(슬롯 없음) 자연 no-op: 앵커만 · 슬롯 검사(풀) 미진입.
  - 작업공간 실재: 장부·폴더 부재면 거부.
  - 타 점유자: 다른 세션 leased 면 거부 · idle/내 세션이면 통과 · readonly(⑬)는 carve-out(비적용).
  - 보호브랜치: **warn 만**(거부 아님·rc 0) + T-0360 안내 · readonly 예외.
  - 기록 vs live: compare_slot_git 소비 — fail_loud=거부 / 미기록=loud+통과 / ok=통과 / submodule drift=warn.
  - 재구현 아님: compare_slot_git 을 실제로 호출(소비)한다.
  - sensitivity: 각 거부 배선을 무력화하면 통과로 뒤집힌다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

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
    """장부 엔트리 대역 — 0단계는 slot/state/session/extra 만 읽는다(실재·점유·readonly)."""

    def __init__(self, slot, *, state="idle", session="", extra=None):
        self.slot = slot
        self.state = state
        self.session = session
        self.extra = extra or {}


class _FakeCompare:
    """compare_slot_git 결과 대역 (T-0350 GitCompareResult 소비 표면·0단계 record-vs-live)."""

    def __init__(self, *, fail_loud=False, unrecorded=False, head_relation="match",
                 submodule_drift=None, recorded=None, live=None):
        self.fail_loud = fail_loud
        self.unrecorded = unrecorded
        self.head_relation = head_relation
        self.submodule_drift = submodule_drift or []
        self.recorded = recorded or {}
        self.live = live or {}


class _NeedsCreate(Exception):
    def __init__(self, repo):
        self.repo = repo
        super().__init__(repo)


class _FakePool:
    """worktree_pool DI mock — 0단계 슬롯 검사(실재·점유·기록정합)를 구동한다(실 부작용 0)."""

    def __init__(self, *, leases=None, branch="feature-x", compare_result=None):
        self.NeedsCreate = _NeedsCreate
        self._leases = list(leases or [])
        self._branch = branch
        self._compare_result = compare_result
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
        return None

    def compare_slot_git(self, slot, *, git_runner=None):
        self.compare_calls.append(slot)
        return self._compare_result


class _OldPool(_FakePool):
    """compare_slot_git 미구현 풀(구버전) — record-vs-live 가 getattr None 으로 no-op 함을 검증."""

    compare_slot_git = None  # 속성 자체를 제거(getattr → None → 정합 검사 skip)


def _make(bootstrap, tmp_path, *, board, worktree_pool=None):
    """격리된 PmBootstrap — board/git/log/pm_state/areas hermetic stub, board/pool 주입."""
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
        return 0, ""

    return bootstrap.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨")),
        run_git_fn=fake_git,
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


def test_readonly_slot_skips_occupancy(bootstrap, tmp_path, capsys):
    """readonly 슬롯(⑬·role="readonly")은 타 점유 검사 **비적용**(carve-out) — 공유가 정상."""
    board = _FakeBoard(anchor_pm_home=None, protected=[])
    # 다른 세션 leased 이지만 role=readonly → 점유 거부하지 않는다.
    pool = _FakePool(leases=[
        _LeaseEntry("work/X_2", state="leased", session="X_9", extra={"role": "readonly"})
    ])
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0, "readonly 슬롯인데 타-점유로 거부됐다(carve-out 미적용)"


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
        _LeaseEntry("work/X_2", state="creating", session="X_9", extra={"role": "readonly"})
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


# ── 5. 보호브랜치 = warn 만 (거부 아님·T-0360 안내) ──────────────────────────


def test_protected_branch_warns_but_passes(bootstrap, tmp_path, capsys):
    """보호 브랜치(main) 직접 체크아웃 = **경고만**(rc 0·거부 아님) + 후속 T-0360 안내."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main"])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], branch="main")
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0, "보호브랜치를 이 티켓에서 거부했다(warn-only 위반·거부는 T-0360)"
    err = capsys.readouterr().err
    assert "보호 브랜치" in err and "main" in err
    assert "T-0360" in err  # 후속 거부 활성 티켓 안내.
    assert pool.bind_calls == ["work/X_2"]  # dump/bind 는 정상 진행.


def test_non_protected_branch_no_warning(bootstrap, tmp_path, capsys):
    """비보호 브랜치는 경고 없음(sensitivity — warn 이 브랜치 조건부임을 입증)."""
    board = _FakeBoard(anchor_pm_home=None, protected=["main"])
    pool = _FakePool(leases=[_LeaseEntry("work/X_2", state="idle")], branch="feature-x")
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=pool)
    inst.run(repo="X", slot=2)
    assert "보호 브랜치" not in capsys.readouterr().err


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
