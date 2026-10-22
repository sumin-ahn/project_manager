"""부트스트랩 0단계 검증 (⑧·spike §F1b·[[T-0351]]) 단위 테스트.

0단계 = dump 를 뿌리기 *전에* "올바른 자리인가"를 기계 검증한다(사용자: "제일 먼저 체크할 건
내가 올바른 슬롯을 쓰고 있나"). 실패 시 **부분 dump 도 금지**(dump 가 뜨면 PM 이 그것을 세션
진실로 믿는다). worktree_pool 은 **DI mock** 으로 주입해 hermetic 하다(실 리스 장부·git·work/ 풀
미접촉·test_pm_bootstrap_lease 의 DI 패턴 동류).

이 파일이 가진 축(다른 파일과 겹치지 않는 것만):
  - **엔진 앵커**(무조건·task 바인딩보다 먼저): worktree 사본이면 거부하고 부분 dump 를 막는다.
    PM 홈이면 통과하고, 판정은 board 가드에 REPO 로 위임한다. board/가드 부재와 가드 내부
    raise 는 fail-soft 통과(오탐 0).
  - **main-참조 축 2**(보호 브랜치 원격 origin-추적): 보호 원격(`origin/main`)을 추적하면 거부,
    자기 feature 브랜치(`origin/a5`) 추적은 통과(오탐 0).
  - **기록↔live**: 같은 브랜치·head 비후손 사유 표면과 재동기 커맨드 실값. compare 미구현 풀은
    검사 no-op 로 통과.
  - **readonly 공유 슬롯 예외의 구조적 성립**: task 가 대여하지 않아 보유 집합에 들어오지 못한다.

여기서 재지 않는 것(소유 파일이 따로 있다):
  - 보유 슬롯 전수 검증(stale·불완전 생성·보호 브랜치 직접 checkout·기록↔live 브랜치 변경)과 열거
    행렬은 `tests/test_pm_bootstrap_task_slot_set.py` 가 소유한다.
  - task 열린 기록 거부·`--force` 인계는 `tests/test_pm_bootstrap_lease.py` 가 소유한다.
  - main-참조 해소 커맨드의 브랜치 이름 도메인은 `tests/test_pm_bootstrap.py`(T-0412) 가 소유한다.
"""
from __future__ import annotations

from _pool_seams import bind_board, bind_pool

import importlib.util
import json
from pathlib import Path

import pytest
from pm_engine.infra import pm_paths
from pm_engine.infra import pool_store

from pm_engine.infra import areas_store

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
    """board 대역. 엔진 앵커(`_pm_home_worktree_misanchor`)와 보호목록(`_repo_protected`)을 낸다.

    `anchor_pm_home` 이 None 이 아니면 anchor 가 REPO 를 등록 worktree 사본으로 판정한다(거부
    유발). 앵커 호출 인자를 기록해 "REPO 로 소비" 를 검증한다."""

    def __init__(self, *, anchor_pm_home=None, protected=None):
        self._anchor_pm_home = anchor_pm_home
        self._protected = protected if protected is not None else ["main", "master", "develop"]
        self.anchor_calls: list = []

    def _pm_home_worktree_misanchor(self, anchor, **_kw):
        self.anchor_calls.append(anchor)
        return self._anchor_pm_home

    def _repo_protected(self, repo):
        return self._protected



class _RaisingBoard:
    """앵커 가드가 **내부 raise** 하는 board 대역. phase0 fail-soft 백스톱 검증용."""

    def _pm_home_worktree_misanchor(self, anchor, **_kw):
        raise RuntimeError("가드 내부 버그(예: git 호출 예외)")

    def _repo_protected(self, repo):
        return []



class _Lease:
    """장부 엔트리 대역. 0단계는 slot/repo/session/state/role 을 읽는다."""

    def __init__(self, slot, repo, session, *, state="leased", role="work"):
        self.slot = slot
        self.repo = repo
        self.session = session
        self.state = state
        self.role = role
        self.extra = {}


class _Compare:
    """compare_slot_git 결과 대역 (T-0350 GitCompareResult 소비 표면·0단계 기록↔live)."""

    def __init__(self, *, fail_loud=False, unrecorded=False, branch_match=True,
                 head_relation="match", recorded=None, live=None):
        self.fail_loud = fail_loud
        self.unrecorded = unrecorded
        self.branch_match = branch_match
        self.head_relation = head_relation
        self.recorded = recorded or {}
        self.live = live or {}


class _SlotStatus:
    """slot_status 결과 대역. main-참조 축 2 는 `upstream_ok`·`upstream` 을 소비한다 (T-0276·§F9).

    `upstream` = `@{upstream}` 해소명(예 `origin/main`·`origin/a5`). 보호 브랜치 원격만 거부한다."""

    def __init__(self, *, upstream_ok=False, upstream=None):
        self.upstream_ok = upstream_ok
        self.upstream = upstream


class _NeedsCreate(Exception):
    def __init__(self, repo):
        self.repo = repo
        super().__init__(repo)


class _TaskActiveElsewhere(Exception):
    def __init__(self, name, started=""):
        self.name = name
        self.started = started
        super().__init__(name)


class _InvalidTaskName(Exception):
    def __init__(self, name, reason):
        self.name = name
        self.reason = reason
        super().__init__(reason)


class _TaskRecord:
    def __init__(self, name, prefix=None, started="2026-07-20T00:00:00+00:00"):
        self.name = name
        self.prefix = prefix
        self.open = True
        self.session = "s-test"
        self.started = started


class _Pool:
    """worktree_pool DI mock. task 보유 슬롯 집합과 그 슬롯들의 git 상태를 모델한다(실 부작용 0).

    `task_slots` = task 명의로 보유 중인 슬롯 식별자(=`slots_for_task` 반환). 각 슬롯 worktree
    폴더는 `slot_root/slot` 에 실제로 만든다(0단계 실재 검사가 실 `.exists()` 를 본다).
    `branches`·`upstreams`·`compare` 는 per-slot 이고, 미지정 슬롯은 정상형(비보호 브랜치·upstream
    미설정·기록↔live ok)이다."""

    NoTaskSessionClaim = type("NoTaskSessionClaim", (Exception,), {})

    def __init__(self, *, slot_root, task_slots=(), branches=None, upstreams=None,
                 compare=None, task_dir_root=None):
        self.NeedsCreate = _NeedsCreate
        self.TaskActiveElsewhere = _TaskActiveElsewhere
        self.InvalidTaskName = _InvalidTaskName
        self._root = Path(slot_root)
        self._task_slots = list(task_slots)
        self._branches = branches or {}
        self._upstreams = upstreams or {}
        self._compare = compare or {}
        self._task_dir_root = task_dir_root or (self._root / "tasks")
        for slot in self._task_slots:
            (self._root / slot).mkdir(parents=True, exist_ok=True)
        self.bind_task_calls: list[dict] = []
        self.slots_for_task_calls: list[str] = []
        self.compare_calls: list[str] = []

    # task 축
    def take_task_session_claim(self, name):
        return "s-test"

    def bind_task(self, name, *, session=None, registered_repos=None):
        self.bind_task_calls.append({"name": name, "registered_repos": registered_repos})
        self.task_dir(name).mkdir(parents=True, exist_ok=True)
        (self.task_dir(name) / "pm_state.md").write_text(
            "## 세션 식별 (현재까지 사용된 이름)\n"
            "  - (아직 완료된 task 세션 없음)\n",
            encoding="utf-8",
        )
        return (_TaskRecord(name), "created")

    def find_task(self, name):
        return _TaskRecord(name)

    def read_task_todos(self, name):
        """열린 할 일 조회 — 부트스트랩 dump 가 읽는 자리(이 대역은 항상 0건)."""
        return []

    def task_dir(self, name):
        return self._task_dir_root / name

    def slots_for_task(self, name):
        self.slots_for_task_calls.append(name)
        return [_Lease(slot, self._repo_of(slot), name) for slot in self._task_slots]

    def slots_for_task_strict(self, name):
        """실 엔진과 같은 짝 — 결과는 같고 장부 손상만 올린다(이 대역은 손상 없음)."""
        return self.slots_for_task(name)

    def _normalize_branch_name(self, branch, *, git_runner):
        """실 엔진과 같은 계약 — 유효한 이름은 고정점(자기 자신)으로 돌아온다."""
        return branch

    def lease_owned_by_task_strict(self, slot, task):
        return slot in self._task_slots

    # 슬롯 조회
    @staticmethod
    def _repo_of(slot):
        return slot.rpartition("/")[2].rsplit("_", 1)[0]

    def slot_path(self, slot):
        return self._root / slot

    def current_branch(self, slot, *, git_runner=None):
        return self._branches.get(slot, "feature-x")

    def slot_status(self, slot, *, git_runner=None):
        upstream = self._upstreams.get(slot)
        return _SlotStatus(upstream_ok=upstream is not None, upstream=upstream)

    def compare_slot_git(self, slot, *, git_runner=None):
        self.compare_calls.append(slot)
        return self._compare.get(slot, _Compare())

    def slot_git_status(self, slot, *, git_runner=None):
        # 이 스위트는 0단계 게이트만 보므로 parent 없음(None)으로 고정한다(신 계약 모양만 맞춤).
        return {"slot": slot, "branch": self._branches.get(slot, "feature-x"), "head": None,
                "parent": None, "children": [], "stashes": 0, "submodules": [], "dirty": False}

    def lease_is_open(self, lease):
        """열림 판정은 엔진 규칙 그대로. 열림 기록(leased + session)이면 열림이다."""
        return getattr(lease, "state", None) == "leased" and bool(getattr(lease, "session", ""))

    def list_leases(self):
        return [_Lease(slot, self._repo_of(slot), "mytask") for slot in self._task_slots]

    def alloc(self, repo, *, branch=None, resume=None, **_kw):
        return _Lease(f"work/{repo}_1", repo, "mytask")

    def find_task_strict(self, value):
        return self.find_task(value)


class _OldPool(_Pool):
    """compare_slot_git 미구현 풀(구버전). 기록↔live 검사가 getattr None 으로 no-op 함을 본다."""

    compare_slot_git = None  # 속성 자체를 제거(getattr → None → 정합 검사 skip)


def _make(bootstrap, tmp_path, *, board, worktree_pool=None, monkeypatch):
    """격리된 PmBootstrap. board/git/log/areas/pm_state 를 hermetic stub 으로 두고 pool 을 주입한다."""
    monkeypatch.setattr(bootstrap, "REPO", tmp_path)
    monkeypatch.setattr(areas_store, "_repo_base", lambda _home, _repo: "main")
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("| repo | prefix |\n|---|---|\n| A | A |\n", encoding="utf-8")
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
            # remedy 브랜치 후보 존재 검사(T-0412). 이 대역 슬롯엔 브랜치가 없다(rc≠0).
            return 1, ""
        return 0, ""

    bind_pool(monkeypatch, worktree_pool)
    return bootstrap.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨")),
        run_git_fn=fake_git,
        log_file=log_file,
        areas_file=areas_file,

        board=board,
        pm_state_file=pm_state_file,
    )


# ── 1. 엔진 앵커 (무조건·task 바인딩보다 먼저) ────────────────────────────────


def test_anchor_worktree_copy_rejects_before_dump(bootstrap, monkeypatch, tmp_path, capsys):
    """엔진 앵커가 worktree 사본(등록 pm_home 하위)이면 거부한다. rc 1 · **부분 dump 금지**(stdout 0)."""
    board = bind_board(monkeypatch, _FakeBoard(anchor_pm_home="/home/x/pm_home"))
    pool = bind_pool(monkeypatch, _Pool(slot_root=tmp_path / "wt"))
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=bind_pool(monkeypatch, pool), monkeypatch=monkeypatch)
    rc = inst.run(task="mytask")
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == "", "0단계 거부인데 부분 dump 가 떴다(PM 이 세션 진실로 오신뢰)"
    assert "0단계" in cap.err and "worktree" in cap.err
    assert "/home/x/pm_home" in cap.err  # 해소 안내(PM 홈 경로)
    assert pool.bind_task_calls == [], "앵커 거부인데 task 바인딩이 돌았다(거부가 dump 이전이 아님)"


def test_anchor_consumes_pm_home_guard_with_repo(bootstrap, monkeypatch, tmp_path, capsys):
    """앵커는 board T-0345 가드(`_pm_home_worktree_misanchor`)를 REPO 로 **소비**한다(재구현 아님)."""
    board = bind_board(monkeypatch, _FakeBoard(anchor_pm_home="/home/x/pm_home"))
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=bind_pool(monkeypatch, _Pool(slot_root=tmp_path / "wt")), monkeypatch=monkeypatch)
    inst.run(task="mytask")
    assert board.anchor_calls == [bootstrap.REPO], "앵커 판정을 REPO 로 board 가드에 위임하지 않음"


def test_anchor_pm_home_passes(bootstrap, monkeypatch, tmp_path, capsys):
    """엔진 앵커가 PM 홈(misanchor None)이면 통과한다(rc 0·정상 dump)."""
    board = bind_board(monkeypatch, _FakeBoard(anchor_pm_home=None))
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=bind_pool(monkeypatch, _Pool(slot_root=tmp_path / "wt")), monkeypatch=monkeypatch)
    rc = inst.run(task="mytask")
    assert rc == 0
    assert capsys.readouterr().out != ""  # 정상 dump.


def test_anchor_board_absent_failsoft_passes(bootstrap, tmp_path, monkeypatch, capsys):
    """board(또는 헬퍼) 부재면 앵커 검사를 생략한다(fail-soft·오탐 0·standalone 무영향)."""
    monkeypatch.setattr(bootstrap, "_load_board", lambda: None)
    inst = _make(bootstrap, tmp_path, board=None,   # self._board None → _load_board None → skip.
                 worktree_pool=bind_pool(monkeypatch, _Pool(slot_root=tmp_path / "wt")), monkeypatch=monkeypatch)
    assert inst.run(task="mytask") == 0


def test_anchor_guard_internal_raise_failsoft_passes(bootstrap, monkeypatch, tmp_path, capsys):
    """앵커 가드(board 헬퍼)가 **내부 raise** 하면 fail-soft 통과한다(부재만이 아니라 raise 경로도).

    phase0 헬퍼의 `except Exception: return <통과값>`(의도적·오탐 0)이 헬퍼 내부 버그를 silent-pass
    로 잡아먹지 않고 *통과값*을 돌려주는지 백스톱한다. dump 정상·rc 0(거부 아님)."""
    inst = _make(bootstrap, tmp_path, board=_RaisingBoard(),
                 worktree_pool=bind_pool(monkeypatch, _Pool(slot_root=tmp_path / "wt")), monkeypatch=monkeypatch)
    rc = inst.run(task="mytask")
    assert rc == 0, "가드 raise 를 fail-soft(통과)로 흡수하지 못함"
    assert capsys.readouterr().out != ""  # 정상 dump(거부 아님).


# ── 2. main-참조 축 2 — 보호 브랜치 원격 origin-추적 (T-0276·§F9) ──────────────
#
# 축 1(보호 브랜치 직접 checkout)과 해소 커맨드 실값은 test_pm_bootstrap_task_slot_set.py 가
# 소유한다. 여기서는 그 파일이 태우지 않는 upstream 축(`slot_status.upstream_ok`)만 잰다.


def test_protected_upstream_tracking_blocks_entry(bootstrap, monkeypatch, tmp_path, capsys):
    """보호 브랜치 원격(origin/main) origin-추적은 **진입 차단**이다(rc 1·부분 dump 금지).

    브랜치 자체는 비보호(feature-x)여도 upstream 이 보호 브랜치 원격을 가리키면 main-참조다
    (`main`+`origin/main` 슬롯 concern)."""
    board = bind_board(monkeypatch, _FakeBoard(anchor_pm_home=None, protected=["main"]))
    pool = bind_pool(monkeypatch, _Pool(slot_root=tmp_path / "wt", task_slots=("work/A_1",),
                 branches={"work/A_1": "feature-x"},
                 upstreams={"work/A_1": "origin/main"}))
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=bind_pool(monkeypatch, pool), monkeypatch=monkeypatch)
    rc = inst.run(task="mytask")
    assert rc == 1, "보호 브랜치 원격(origin/main) 추적 슬롯이 거부되지 않았다(§F9 축 2 미적용)"
    cap = capsys.readouterr()
    assert cap.out == "", "거부인데 부분 dump 가 떴다(0단계 계약 위반)"
    assert "main-참조" in cap.err and "origin-추적" in cap.err
    assert "origin/main" in cap.err, "판정 근거에 추적 중인 upstream 실값이 없다"


def test_feature_branch_upstream_passes(bootstrap, monkeypatch, tmp_path, capsys):
    """자기 feature 브랜치 추적(origin/a5)은 정상 작업 슬롯이라 거부하지 않는다(오탐 0·sensitivity).

    T-0273/0274 로 슬롯이 자기 workstream(`origin/<branch>`)을 추적하는 건 정상이다. §F9 concern 은
    `origin/main` 추적뿐이라 임의 upstream 을 막으면 정상 슬롯이 깨진다(회귀 방어)."""
    board = bind_board(monkeypatch, _FakeBoard(anchor_pm_home=None, protected=["main"]))
    pool = bind_pool(monkeypatch, _Pool(slot_root=tmp_path / "wt", task_slots=("work/A_1",),
                 branches={"work/A_1": "a5"}, upstreams={"work/A_1": "origin/a5"}))
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=bind_pool(monkeypatch, pool), monkeypatch=monkeypatch)
    rc = inst.run(task="mytask")
    cap = capsys.readouterr()
    assert rc == 0, f"자기 feature 브랜치 추적(origin/a5) 슬롯을 잘못 거부했다(오탐): {cap.err}"
    assert "origin-추적" not in cap.err
    # 그 슬롯이 실제로 검증·열거를 통과했음을 값으로 확인한다(검사 자체를 건너뛴 게 아니다).
    assert "work/A_1 · repo=A · branch=a5" in cap.out


# ── 3. 기록↔live 정합 (compare_slot_git 소비·㉒) ──────────────────────────────
#
# 브랜치 변경(branch_match False) 사유와 해소 커맨드는 test_pm_bootstrap_task_slot_set.py 가
# 소유한다. 여기서는 같은 브랜치·head 비후손 갈래와 compare 미구현 풀만 잰다.


def test_diverged_head_not_descendant_surfaces_reason_and_record_cmd(bootstrap, monkeypatch, tmp_path, capsys):
    """같은 브랜치인데 live HEAD 가 기록 커밋의 후손이 아니면 그 사유와 재동기 커맨드를 싣는다.

    `head_relation`/`branch_match` 원값만 보여 주면 PM 이 코드를 정독해야 "왜 diverged 인지" 를
    안다(PM 78 낭비). 근거는 문장으로, 해소는 사용자용 record 커맨드 실값으로 나가야 한다
    (감지는 기계·해소는 사용자·자동 실행 없음)."""
    board = bind_board(monkeypatch, _FakeBoard(anchor_pm_home=None, protected=[]))
    pool = bind_pool(monkeypatch, _Pool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1",), branches={"work/A_1": "feat"},
        compare={"work/A_1": _Compare(fail_loud=True, branch_match=True, head_relation="diverged",
                                      recorded={"branch": "feat", "head": "aaa"},
                                      live={"branch": "feat", "head": "bbb"})},
    ))
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=bind_pool(monkeypatch, pool), monkeypatch=monkeypatch)
    rc = inst.run(task="mytask")
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "기록↔live diverged" in cap.err
    assert "후손이 아님" in cap.err, "head 비후손 사유가 문장으로 나오지 않았다"
    assert "브랜치가 바뀜" not in cap.err, "브랜치 변경 사유로 오분기했다(두 갈래가 안 갈린다)"
    assert "worktree_pool.py record work/A_1" in cap.err  # 재동기 커맨드 실값(정확한 slot_id)
    assert pool.compare_calls == ["work/A_1"], "compare_slot_git 을 소비(호출)하지 않았다"


def test_old_pool_without_compare_passes(bootstrap, monkeypatch, tmp_path, capsys):
    """compare_slot_git 미구현 풀(구버전)은 정합 검사를 no-op 한다(getattr None·fail-soft·통과)."""
    board = bind_board(monkeypatch, _FakeBoard(anchor_pm_home=None, protected=[]))
    pool = bind_pool(monkeypatch, _OldPool(slot_root=tmp_path / "wt", task_slots=("work/A_1",),
                    branches={"work/A_1": "a5"}))
    inst = _make(bootstrap, tmp_path, board=board, worktree_pool=bind_pool(monkeypatch, pool), monkeypatch=monkeypatch)
    rc = inst.run(task="mytask")
    cap = capsys.readouterr()
    assert rc == 0, f"compare 미구현 풀이 진입을 막았다: {cap.err}"
    # 판정 불가는 차단이 아니라 표기다(열거 행렬의 '미상').
    assert "기록↔live 미상" in cap.out


# ── 4. readonly 공유 슬롯 예외 — 분기가 아니라 구조로 성립한다 (⑬·§F11) ───────


def test_readonly_slot_never_enters_the_task_slot_set(tmp_path, *, monkeypatch):
    """readonly 공유 슬롯은 task 가 대여하지 않아 0단계 검증 대상 집합에 아예 들어오지 못한다.

    옛 0단계는 readonly 슬롯을 만나면 main-참조 거부를 건너뛰는 예외 분기를 따로 뒀다. 지금
    검증 대상은 `slots_for_task`(state=leased 이고 session==task)가 정하고, readonly 행은 무소유
    (session 빈 값)라 그 집합에 못 들어온다. 예외 분기 없이 같은 결과가 나오는 자리라 분기가 아니라
    이 구조를 잰다. 실 장부 파서로 재고, tmp 장부에만 쓴다(실 풀 미접촉)."""
    wp = bind_pool(monkeypatch, _load("worktree_pool"))
    local = tmp_path / "local"
    ledger = local / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({
            "leases": [
                # task 명의 작업 슬롯(대여 중).
                {"slot": "work/A_1", "repo": "A", "session": "mytask",
                 "started": "2026-08-22T00:00:00+00:00", "state": "leased", "bound": True},
                # readonly 공유 슬롯(무소유·role 이 장부에 남는 유일한 슬롯 종류).
                {"slot": "work/A_9", "repo": "A", "session": "",
                 "started": "2026-08-22T00:00:00+00:00", "state": "leased", "role": "readonly"},
            ],
            "tasks": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    for name, value in {
        "LEASES_FILE": ledger,
        "LEASES_LOCK": local / "worktree-leases.lock",
        "TASKS_DIR": local / "tasks",
        "WORK_DIR": tmp_path / "work",
    }.items():
        setattr(wp, name, value)

    home = pm_paths.home_at(tmp_path)._replace(local_dir=local)
    held = [lease.slot for lease in pool_store.slots_for_task(home, "mytask")]

    assert held == ["work/A_1"], "readonly 공유 슬롯이 task 보유 집합에 섞였다(점유 대상이 아니다)"
    # 빠진 이유가 "장부에 행이 없어서" 가 아님을 같은 장부에서 확인한다(공허한 통과 방지).
    roles = {lease.slot: lease.role for lease in pool_store.list_leases(home)}
    assert roles == {"work/A_1": "work", "work/A_9": "readonly"}
