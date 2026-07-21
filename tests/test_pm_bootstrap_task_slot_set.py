"""부트스트랩 task 진입: 보유 슬롯 집합 전수 열거 + 0단계 전수 검증 (W3·I2·ⓐC·[[ADR-0068]]).

task 모드 부트스트랩(F1 bind/resume 후)이 `slots_for_task` 보유 집합을 **전수 열거**(슬롯·repo·
branch·head·기록↔live·dirty 행렬)하고 **슬롯별 0단계 검증**(stale·main-참조·기록↔live diverged —
기존 `_phase0_*`/`compare_slot_git` 프리미티브 재사용)을 돈다. fault 1+ = 진입 차단(부분 dump 금지·
전 fault 일괄 표시·해소 커맨드 실값). 0개 = "작업공간: (없음)"+진입. `--repo/--slot` 편입은 편입 후
같은 열거 합류. slot-모드/솔로 경로는 gate 미진입(100% 불변).

worktree_pool 은 **DI mock**(실 장부/git 미접촉·folder 존재는 tmp 실디렉터리로 모델). test_pm_
bootstrap_phase0.py 의 hermetic DI 패턴 동류.
"""
from __future__ import annotations

import importlib.util
import json as _json
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


# ── 대역 ─────────────────────────────────────────────────────────────────────


class _FakeBoard:
    """board 대역 — 엔진 앵커(anchor=None→통과) + 보호브랜치 목록(`_repo_protected`)."""

    def __init__(self, *, protected=None):
        self._protected = protected if protected is not None else ["main", "master", "develop"]

    def _pm_home_worktree_misanchor(self, anchor, **_kw):
        return None  # PM 홈(통과) — 앵커 거부 안 함.

    def _repo_protected(self, repo):
        return self._protected


class _TaskRecord:
    def __init__(self, name, prefix=None, started="2026-07-20T00:00:00+00:00"):
        self.name = name
        self.prefix = prefix
        self.started = started


class _Lease:
    def __init__(self, slot, repo, session, *, state="leased", role="work"):
        self.slot = slot
        self.repo = repo
        self.session = session
        self.state = state
        self.role = role
        self.extra = {}


class _Compare:
    def __init__(self, *, fail_loud=False, unrecorded=False, branch_match=True,
                 head_relation="match", recorded=None, live=None):
        self.fail_loud = fail_loud
        self.unrecorded = unrecorded
        self.branch_match = branch_match
        self.head_relation = head_relation
        self.recorded = recorded or {}
        self.live = live or {}


class _SlotStatus:
    def __init__(self, *, upstream_ok=False, upstream=None):
        self.upstream_ok = upstream_ok
        self.upstream = upstream


class _NeedsCreate(Exception):
    def __init__(self, repo):
        self.repo = repo


class _TaskActiveElsewhere(Exception):
    def __init__(self, name, pid):
        self.name = name
        self.pid = pid


class _InvalidTaskName(Exception):
    def __init__(self, name, reason):
        self.name = name
        self.reason = reason


class _TaskPool:
    """task 보유 슬롯 집합을 모델하는 DI mock — folder 존재는 tmp 실디렉터리로.

    `task_slots` = task 명의로 보유 중인 슬롯 식별자 리스트(slots_for_task 반환). 각 슬롯의
    worktree 폴더는 `slot_root/slot` 에 실제로 만든다(`missing_dirs` 에 든 슬롯만 제외 = stale).
    branch/head/dirty 는 per-slot dict, compare(기록↔live)도 per-slot(_Compare) — 미지정은
    정상(ok·clean·match)."""

    def __init__(self, *, slot_root, task_name="mytask", task_slots=(), missing_dirs=(),
                 present_slots=(), branches=None, heads=None, dirty=None, compare=None,
                 states=None, task_dir_root=None):
        self.NeedsCreate = _NeedsCreate
        self.TaskActiveElsewhere = _TaskActiveElsewhere
        self.InvalidTaskName = _InvalidTaskName
        self._root = Path(slot_root)
        self._task_name = task_name
        self._task_slots = list(task_slots)
        self._missing = set(missing_dirs)
        self._branches = branches or {}
        self._heads = heads or {}
        self._dirty = dirty or {}
        self._compare = compare or {}
        self._states = states or {}
        self._task_dir_root = task_dir_root or (self._root / "tasks")
        # 실 폴더 생성(stale 제외) — _phase0_slot_folder_exists 가 실 .exists() 를 본다.
        # present_slots = 아직 task 보유는 아니나 실재하는 슬롯(slot-모드/T-0390 편입 대상 등).
        for s in list(self._task_slots) + list(present_slots):
            if s not in self._missing:
                (self._root / s).mkdir(parents=True, exist_ok=True)
        self.bind_task_calls: list[dict] = []
        self.bind_calls: list[dict] = []
        self.slots_for_task_calls: list[str] = []
        # T-0390 편입용 — bind_slot 이 잡은 슬롯을 이후 slots_for_task/list_leases 에 합류.
        self._bound_extra: list[str] = []

    # task 축
    def bind_task(self, name, *, pid=None, registered_repos=None):
        self.bind_task_calls.append({"name": name, "registered_repos": registered_repos})
        self.task_dir(name).mkdir(parents=True, exist_ok=True)
        return (_TaskRecord(name), "created", None)

    def task_dir(self, name):
        return self._task_dir_root / name

    def slots_for_task(self, name):
        self.slots_for_task_calls.append(name)
        slots = list(self._task_slots) + [s for s in self._bound_extra if s not in self._task_slots]
        out = []
        for s in slots:
            repo = s.rpartition("/")[2].rsplit("_", 1)[0]
            out.append(_Lease(s, repo, name, state=self._states.get(s, "leased")))
        return out

    # 슬롯 조회
    def slot_path(self, slot):
        return self._root / slot

    def current_branch(self, slot, *, git_runner=None):
        return self._branches.get(slot)

    def slot_status(self, slot, *, git_runner=None):
        return _SlotStatus()

    def compare_slot_git(self, slot, *, git_runner=None):
        return self._compare.get(slot, _Compare())

    def slot_git_status(self, slot, *, git_runner=None):
        return {"slot": slot, "branch": self._branches.get(slot),
                "head": self._heads.get(slot), "dirty": self._dirty.get(slot, False)}

    # T-0390 편입 경로
    def list_leases(self):
        leased = []
        for s in self._task_slots:
            repo = s.rpartition("/")[2].rsplit("_", 1)[0]
            leased.append(_Lease(s, repo, self._task_name, state=self._states.get(s, "leased")))
        for c in self.bind_calls:
            repo = c["slot"].rpartition("/")[2].rsplit("_", 1)[0]
            leased.append(_Lease(c["slot"], repo, c["session"], state="leased"))
        return leased

    def bind_slot(self, slot, repo, session, *, git_runner=None):
        self.bind_calls.append({"slot": slot, "repo": repo, "session": session})
        if slot not in self._bound_extra:
            self._bound_extra.append(slot)
        (self._root / slot).mkdir(parents=True, exist_ok=True)
        return _Lease(slot, repo, session, state="leased")

    def alloc(self, repo, *, branch=None, resume=None, **_kw):
        return _Lease(f"work/{repo}_1", repo, self._task_name)


def _make(bootstrap, tmp_path, pool, *, board=None):
    """격리 PmBootstrap — board/git/log/areas/pm_state hermetic stub + pool 주입."""
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("| repo | prefix |\n|---|---|\n| A | A |\n| B | B |\n", encoding="utf-8")
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
        run_git_fn=fake_git,
        log_file=log_file,
        areas_file=areas_file,
        worktree_pool=pool,
        board=board or _FakeBoard(),
        pm_state_file=pm_state_file,
    )


# ── 1. 열거 — 0 / 1 / N 슬롯 ─────────────────────────────────────────────────


def test_zero_slots_shows_none_and_enters(bootstrap, tmp_path, capsys):
    """0개 보유 = "작업공간: (없음)" + generic 안내 + 진입(rc 0·검증 no-op)."""
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=())
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 0
    out = capsys.readouterr().out
    assert "작업공간: (없음)" in out
    assert "F2 alloc(T-0354)에서 연결" in out       # generic 안내는 0개일 때만
    assert "전수 검증" not in out                    # 열거 행렬 헤더 없음


def test_single_slot_enumerated_as_matrix(bootstrap, tmp_path, capsys):
    """1개 보유 = 행렬 1행(slot·repo·branch·head·기록↔live·dirty) surface + 진입."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1",),
        branches={"work/A_1": "a5"}, heads={"work/A_1": "abcdef1234567890"},
        dirty={"work/A_1": True},
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 0
    out = capsys.readouterr().out
    assert "작업공간 (task 'mytask' 보유 1 — 전수 검증):" in out
    assert "work/A_1 · repo=A · branch=a5 · head=abcdef123456 · 기록↔live ✓ · dirty" in out
    assert "F2 alloc" not in out                     # generic 안내는 0개일 때만(억제)


def test_multiple_slots_all_enumerated(bootstrap, tmp_path, capsys):
    """N개 보유 = 전 슬롯이 행렬에 열거된다."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1", "work/B_2"),
        branches={"work/A_1": "a5", "work/B_2": "b3"},
        heads={"work/A_1": "aaaaaaaaaaaa", "work/B_2": "bbbbbbbbbbbb"},
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 0
    out = capsys.readouterr().out
    assert "보유 2 — 전수 검증" in out
    assert "work/A_1 · repo=A · branch=a5" in out
    assert "work/B_2 · repo=B · branch=b3" in out
    assert "clean" in out                            # dirty 미지정 → clean


# ── 2. 진입 검증 — fault = 차단(부분 dump 금지·일괄 표시) ─────────────────────


def test_stale_slot_blocks_entry_with_prune_stale(bootstrap, tmp_path, capsys):
    """stale 슬롯(장부 有·폴더 無) = fault → 진입 차단 + prune-stale 해소 안내 + 부분 dump 금지."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1",),
        missing_dirs=("work/A_1",),                  # 폴더 부재 → stale
        branches={"work/A_1": "a5"},
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == "", "진입 차단인데 부분 dump 가 떴다(세션 진실 오신뢰)"
    assert "진입검증" in cap.err and "work/A_1" in cap.err
    assert "stale" in cap.err
    assert "worktree prune-stale" in cap.err          # 해소 커맨드 실값


def test_diverged_slot_blocks_with_reason_and_record_cmd(bootstrap, tmp_path, capsys):
    """기록↔live diverged = fault → 차단 + 판정 근거(head 관계) + record 해소 커맨드."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1",),
        branches={"work/A_1": "a5"},
        compare={"work/A_1": _Compare(
            fail_loud=True, branch_match=False,
            recorded={"branch": "a5", "head": "old"}, live={"branch": "main", "head": "new"},
        )},
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "diverged" in cap.err
    assert "브랜치가 바뀜" in cap.err                  # _phase0_diverge_reason 근거(head 관계)
    assert "worktree_pool.py record work/A_1" in cap.err


def test_creating_slot_blocks_with_worktree_status_cmd(bootstrap, tmp_path, capsys):
    """불완전 생성(state=creating·T-0295) = fault → 차단 + worktree status 해소 안내."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1",),
        states={"work/A_1": "creating"},             # 생성 중/중단 슬롯
        branches={"work/A_1": "a5"},
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "불완전 생성" in cap.err and "creating" in cap.err
    assert "worktree status" in cap.err               # 해소 커맨드 실값


def test_protected_branch_slot_blocks_with_switch_cmd(bootstrap, tmp_path, capsys):
    """main-참조(보호브랜치 직접 checkout) = fault → 차단 + git switch 해소 커맨드."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1",),
        branches={"work/A_1": "main"},               # 보호브랜치 직접 checkout
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "main-참조" in cap.err
    assert "git -C work/A_1 switch -c mytask" in cap.err


def test_all_faults_reported_at_once(bootstrap, tmp_path, capsys):
    """전 fault 일괄 표시(순차 발견 금지) — 2슬롯 각각 다른 fault 가 **한 번에** 나온다."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1", "work/B_2"),
        missing_dirs=("work/B_2",),                  # B_2 = stale
        branches={"work/A_1": "main", "work/B_2": "b3"},  # A_1 = main-참조
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 1
    err = capsys.readouterr().err
    # 두 fault 슬롯이 같은 출력에 모두 등장(첫 fault 서 멈추지 않음).
    assert "work/A_1" in err and "main-참조" in err
    assert "work/B_2" in err and "stale" in err
    assert "2개가 0단계 검증에 실패" in err


# ── 3. 전부 정상 = 진입 (record-vs-live ✓) ───────────────────────────────────


def test_all_ok_enters_and_marks_record_live_ok(bootstrap, tmp_path, capsys):
    """전부 정상(fault 0) = 진입 + 기록↔live ✓ 표기."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1",),
        branches={"work/A_1": "a5"}, heads={"work/A_1": "cafebabe0000"},
        compare={"work/A_1": _Compare()},            # ok
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 0
    out = capsys.readouterr().out
    assert "기록↔live ✓" in out


def test_unrecorded_slot_passes_and_marks(bootstrap, tmp_path, capsys):
    """미기록(구 슬롯)은 diverged 아니므로 진입 통과 + '미기록' 표기(차단 아님)."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_slots=("work/A_1",),
        branches={"work/A_1": "a5"},
        compare={"work/A_1": _Compare(unrecorded=True)},
    )
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run(task="mytask")
    assert rc == 0
    assert "기록↔live 미기록" in capsys.readouterr().out


# ── 4. --repo/--slot 편입(T-0390) 합류 ───────────────────────────────────────


def test_incorporated_slot_joins_enumeration(bootstrap, tmp_path, capsys):
    """`--task X --repo Y --slot N` 편입 슬롯은 bind 뒤 같은 열거 행렬에 합류(편입 표기)."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_name="mytask", task_slots=(),  # 사전 보유 0
        present_slots=("work/A_2",),                 # 편입 대상 슬롯 실재(phase0 통과)
        branches={"work/A_2": "a5"}, heads={"work/A_2": "1234abcd5678"},
    )
    inst = _make(bootstrap, tmp_path, pool, board=_FakeBoard(protected=[]))
    rc = inst.run(task="mytask", repo="A", slot=2)
    assert rc == 0
    out = capsys.readouterr().out
    # 편입 슬롯이 매트릭스 행으로 합류하고 편입 표기가 붙는다.
    assert "보유 1 — 전수 검증" in out
    assert "work/A_2 · repo=A · branch=a5" in out
    assert "이 부트스트랩 편입(T-0390)" in out
    # 슬롯은 task 명의로 bind 됐다.
    assert pool.bind_calls == [{"slot": "work/A_2", "repo": "A", "session": "mytask"}]


def test_incorporated_slot_json_slot_set(bootstrap, tmp_path, capsys):
    """--json — task.slot_set 에 편입 슬롯이 행 dict 로 실린다."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt", task_name="mytask", task_slots=(),
        present_slots=("work/A_2",),
        branches={"work/A_2": "a5"}, heads={"work/A_2": "1234abcd5678"},
    )
    inst = _make(bootstrap, tmp_path, pool, board=_FakeBoard(protected=[]))
    inst.run(task="mytask", repo="A", slot=2, output_json=True)
    data = _json.loads(capsys.readouterr().out)
    rows = data["task"]["slot_set"]
    assert [r["slot"] for r in rows] == ["work/A_2"]
    assert rows[0]["repo"] == "A" and rows[0]["branch"] == "a5"
    assert rows[0]["head"] == "1234abcd5678"
    assert rows[0]["record_live"] == "ok"


# ── 5. slot-모드 / 솔로 불변 (gate 미진입) ───────────────────────────────────


def test_slot_mode_does_not_run_task_gate(bootstrap, tmp_path, capsys):
    """`--slot`(task 없음) = slot-모드 — task gate 미진입(slots_for_task 안 불림·100% 불변)."""
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=("work/A_1",),
                     missing_dirs=("work/A_1",), present_slots=("work/X_2",),
                     branches={"work/X_2": "x1"})
    inst = _make(bootstrap, tmp_path, pool, board=_FakeBoard(protected=[]))
    # stale task 슬롯이 있어도 slot-모드는 task 검증을 돌지 않는다(진입 성공).
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    assert pool.slots_for_task_calls == [], "slot-모드인데 task gate(slots_for_task)가 돌았다"


def test_solo_does_not_run_task_gate(bootstrap, tmp_path, capsys):
    """솔로(무인자) = task gate 미진입(slots_for_task 안 불림·100% 불변)."""
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=("work/A_1",),
                     missing_dirs=("work/A_1",))
    inst = _make(bootstrap, tmp_path, pool)
    rc = inst.run()  # 솔로
    assert rc == 0
    assert pool.slots_for_task_calls == []


# ── 6. sensitivity — stale 판정 배선 무력화 시 차단이 통과로 뒤집힌다 ─────────


def test_sensitivity_stale_detection_flips(bootstrap, tmp_path, capsys, monkeypatch):
    """폴더 실재 검사(`_phase0_slot_folder_exists`)를 True 로 무력화하면 stale 차단이 사라진다.

    검증이 실제로 folder 실재 프리미티브를 **소비**함을 반증(무력화하면 rc1→rc0)."""
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=("work/A_1",),
                     missing_dirs=("work/A_1",), branches={"work/A_1": "a5"})
    inst = _make(bootstrap, tmp_path, pool)
    # 배선 무력화 — 폴더가 항상 실재한다고 보면 stale fault 가 사라진다.
    monkeypatch.setattr(inst, "_phase0_slot_folder_exists", lambda wp, slot: True)
    rc = inst.run(task="mytask")
    assert rc == 0, "폴더 실재 검사를 무력화했는데도 차단됐다(stale 판정이 그 프리미티브 소비가 아님)"
