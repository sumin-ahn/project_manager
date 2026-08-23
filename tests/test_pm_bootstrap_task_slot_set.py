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
from pathlib import Path, PureWindowsPath

import pytest
from _pytest_summary import pytest_summary

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
        (self.task_dir(name) / "pm_state.md").write_text(
            "## 세션 식별 (현재까지 사용된 이름)\n"
            "  - (아직 완료된 task 세션 없음)\n",
            encoding="utf-8",
        )
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

    def lease_owned_by_task_strict(self, slot, task):
        return (
            slot in self._task_slots
            and self._states.get(slot, "leased") == "leased"
            and task == self._task_name
        )

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


def _make(bootstrap, tmp_path, pool, *, board=None, board_fn=None, pytest_fn=None, git_fn=None):
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
        run_board_fn=board_fn or fake_board,
        run_pytest_fn=pytest_fn or (lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨"))),
        run_git_fn=git_fn or fake_git,
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
    assert "신규 task 는 슬롯 0개로 시작 가능" in out  # generic 안내는 0개일 때만
    assert "전수 검증" not in out                    # 열거 행렬 헤더 없음


def test_task_board_lens_excludes_other_task_claim(bootstrap, tmp_path, capsys):
    """task bootstrap의 두 board 조회는 현재 task 렌즈라 타 task claim이 dump에 섞이지 않는다."""
    calls: list[list[str]] = []

    def board_fn(args):
        calls.append(list(args))
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        if args == ["list", "--task", "mytask"]:
            return 0, "  [claimed] T-0471  own task claim  pm  tag\n"
        if args == ["list", "--status", "done", "--task", "mytask"]:
            return 0, ""
        if "--mine" in args:
            return 0, "  [claimed] T-9999  other task claim  pm  tag\n"
        raise AssertionError(f"예상하지 못한 board 호출: {args}")

    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=())
    inst = _make(bootstrap, tmp_path, pool, board_fn=board_fn)
    assert inst.run(task="mytask") == 0
    out = capsys.readouterr().out

    assert [c for c in calls if c[:1] == ["list"]] == [
        ["list", "--task", "mytask"],
        ["list", "--status", "done", "--task", "mytask"],
    ]
    assert "T-9999" not in out
    assert "claimed: 1 (task mytask)" in out


def test_task_slot_resolution_exception_blocks_bootstrap_dump(bootstrap, tmp_path, capsys):
    """slots_for_task 예외는 0슬롯 진입이 아니라 rc1·부분 dump 없는 fail-loud다."""

    class BrokenPool(_TaskPool):
        def slots_for_task(self, name):
            raise OSError("ledger unreadable")

    board_calls: list[list[str]] = []

    def board_fn(args):
        board_calls.append(list(args))
        return 0, ""

    pool = BrokenPool(slot_root=tmp_path / "wt", task_slots=())
    inst = _make(bootstrap, tmp_path, pool, board_fn=board_fn)
    assert inst.run(task="mytask") == 1
    captured = capsys.readouterr()
    assert "보유 슬롯 장부 조회 실패" in captured.err
    assert "실제 0슬롯으로 간주하지 않고" in captured.err
    assert "PM bootstrap dump" not in captured.out
    assert board_calls == []
    assert pool.bind_task_calls == [], "슬롯 장부 해소 실패 뒤 task 장부/state write가 일어났다"


def test_corrupt_real_ledger_blocks_bootstrap_without_rewrite(bootstrap, tmp_path, capsys):
    """실 strict 파서가 손상 JSON을 0슬롯으로 오인하지 않고 rc1·장부 무재작성으로 막는다."""
    wp = _load("worktree_pool")
    local = tmp_path / "real-local"
    ledger = local / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"leases": [', encoding="utf-8")
    for name, value in {
        "LEASES_FILE": ledger,
        "LEASES_LOCK": local / "worktree-leases.lock",
        "TASKS_DIR": local / "tasks",
        "WORK_DIR": tmp_path / "real-work",
    }.items():
        setattr(wp, name, value)
    before = ledger.read_bytes()
    board_calls: list[list[str]] = []
    inst = _make(
        bootstrap,
        tmp_path,
        wp,
        board_fn=lambda args: board_calls.append(list(args)) or (0, ""),
    )

    assert inst.run(task="mytask") == 1

    captured = capsys.readouterr()
    assert "보유 슬롯 장부 조회 실패" in captured.err
    assert "실제 0슬롯으로 간주하지 않고" in captured.err
    assert ledger.read_bytes() == before
    assert board_calls == []
    assert "PM bootstrap dump" not in captured.out


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
    assert pool.slots_for_task_calls == ["mytask"], "검증 lease 스냅샷 대신 surface가 장부를 재조회했다"


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


# ── 1b. task-only cwd 해소 — 전역 auto-slot 유입 금지 ─────────────────────────


def test_task_zero_slots_cwd_stops_at_pm_home(bootstrap, tmp_path, monkeypatch):
    """0개 = PM 홈. 다른 task의 전역 auto-slot을 절대 소비하지 않는다."""
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=())
    inst = _make(bootstrap, tmp_path, pool)
    inst._task_name = "mytask"
    inst._task_workspace_slots = ()
    monkeypatch.setattr(
        bootstrap,
        "_auto_slot",
        lambda: (_ for _ in ()).throw(AssertionError("task-only가 전역 auto-slot을 호출함")),
    )
    assert inst._worktree_cwd() == str(bootstrap.REPO)


def test_task_only_first_turn_uses_current_task_pm_state_path(
    bootstrap, tmp_path, monkeypatch, capsys
):
    """task-only 첫-turn은 전역 auto-slot이 가리키는 타 task 슬롯 대신 현재 task state를 안내한다."""
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=())
    inst = _make(bootstrap, tmp_path, pool)
    monkeypatch.setattr(bootstrap, "_auto_slot", lambda *_args: ("other_task_repo", 9))

    assert inst.run(task="mytask") == 0
    first_turn = capsys.readouterr().out.split("### 권장 첫 turn", 1)[1]
    # 표기는 엔진 직렬화(POSIX 단일)로 만든다 — `str(Path)` 로 만들면 Windows 에서만 갈린다.
    task_state = bootstrap._display_path_text(pool.task_dir("mytask") / "pm_state.md")

    assert task_state in first_turn
    assert ".project_manager/.local/slots/other_task_repo_9/pm_state.md" not in first_turn


def test_task_single_slot_becomes_git_cwd_before_collection(
    bootstrap, tmp_path, capsys
):
    """1개 = 그 task 슬롯이 top Git cwd. `_auto_slot`/타 task 슬롯 유입 없음."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt",
        task_slots=("work/A_1",),
        branches={"work/A_1": "a5"},
    )
    inst = _make(bootstrap, tmp_path, pool)
    git_cwds = []

    def capture_git(args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            git_cwds.append(inst._worktree_cwd(inst._bound_slot))
            return 0, "a5\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 subj\n"
        return 0, ""

    inst._run_git_fn = capture_git
    assert inst.run(task="mytask") == 0
    capsys.readouterr()
    assert inst._task_workspace_slots == ("work/A_1",)
    assert git_cwds == [str(bootstrap.REPO / "work/A_1")]


def test_task_multiple_slots_use_deterministic_git_and_all_freshness(
    bootstrap, tmp_path, capsys
):
    """N개 = 정렬 첫 task 슬롯이 대표 Git cwd, freshness는 task 슬롯 전수."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt",
        task_slots=("work/B_2", "work/A_1"),
        branches={"work/A_1": "a5", "work/B_2": "b3"},
    )
    inst = _make(bootstrap, tmp_path, pool)
    git_cwds = []
    fetched_dirs = []

    def capture_git(args):
        if len(args) >= 4 and args[0] == "-C" and args[2:4] == ["fetch", "origin"]:
            fetched_dirs.append(args[1])
            return 0, ""
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            git_cwds.append(inst._worktree_cwd(inst._bound_slot))
            return 0, "a5\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 subj\n"
        return 0, ""

    inst._run_git_fn = capture_git
    assert inst.run(task="mytask") == 0
    out = capsys.readouterr().out
    assert inst._task_workspace_slots == ("work/A_1", "work/B_2")
    assert git_cwds == [str(bootstrap.REPO / "work/A_1")]
    assert str(bootstrap.REPO / "work/A_1") in fetched_dirs
    assert str(bootstrap.REPO / "work/B_2") in fetched_dirs
    assert "task Git 대표 cwd: `work/A_1`" in out
    assert "freshness/회귀는 전수" in out


def test_task_multiple_slots_with_pytest_runs_all_owned_workspaces(
    bootstrap, tmp_path, capsys
):
    """--with-pytest = task 보유 슬롯 전수 회귀. 합산 결과와 scope를 surface한다."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt",
        task_slots=("work/B_2", "work/A_1"),
        branches={"work/A_1": "a5", "work/B_2": "b3"},
    )
    inst = _make(bootstrap, tmp_path, pool)
    pytest_cwds = []

    def capture_pytest():
        pytest_cwds.append(inst._worktree_cwd(inst._bound_slot))
        return 0, "1 passed in 0.01s\n"

    inst._run_pytest_fn = capture_pytest
    assert inst.run(task="mytask", with_pytest=True) == 0
    out = capsys.readouterr().out
    assert pytest_cwds == [
        str(bootstrap.REPO / "work/A_1"),
        str(bootstrap.REPO / "work/B_2"),
    ]
    assert "회귀: 2 / 2 통과 (task 작업공간 2개 전수)" in out


@pytest.mark.parametrize(
    "failed_result",
    [
        (1, "5 passed in 0.01s\n"),
        (0, "1 failed, 4 passed in 0.01s\n"),
    ],
    ids=["nonzero-rc", "failed-count"],
)
def test_task_pytest_failure_in_any_slot_blocks_immediately(
    bootstrap, tmp_path, capsys, failed_result
):
    """task 전수 회귀는 슬롯 하나의 rc/실패 건수만 비정상이면 합산 dump 전에 즉시 차단한다."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt",
        task_slots=("work/A_1", "work/B_2", "work/C_3"),
        branches={"work/A_1": "a5", "work/B_2": "b3", "work/C_3": "c2"},
    )
    inst = _make(bootstrap, tmp_path, pool)
    pytest_cwds = []
    outcomes = iter([(0, "5 passed in 0.01s\n"), failed_result])

    def capture_pytest():
        pytest_cwds.append(inst._worktree_cwd(inst._bound_slot))
        return next(outcomes)

    inst._run_pytest_fn = capture_pytest
    with pytest.raises(SystemExit) as exc:
        inst.run(task="mytask", with_pytest=True)

    assert exc.value.code == 1
    assert pytest_cwds == [
        str(bootstrap.REPO / "work/A_1"),
        str(bootstrap.REPO / "work/B_2"),
    ], "실패 뒤 다음 슬롯까지 실행해 합산했다"
    captured = capsys.readouterr()
    assert "pytest 회귀 실패 (slot=work/B_2)" in captured.err
    assert "PM bootstrap dump" not in captured.out


def test_task_multiple_slots_json_surfaces_representative_and_full_pytest_scope(
    bootstrap, tmp_path, capsys
):
    """JSON도 대표 Git cwd와 전수 pytest scope를 구분해 기계 소비자가 오해하지 않는다."""
    pool = _TaskPool(
        slot_root=tmp_path / "wt",
        task_slots=("work/B_2", "work/A_1"),
        branches={"work/A_1": "a5", "work/B_2": "b3"},
    )
    inst = _make(bootstrap, tmp_path, pool)
    inst._run_pytest_fn = lambda: (0, "1 passed in 0.01s\n")

    assert inst.run(task="mytask", with_pytest=True, output_json=True) == 0
    data = _json.loads(capsys.readouterr().out)

    assert data["git"]["task_cwd_slot"] == "work/A_1"
    assert data["git"]["task_workspace_count"] == 2
    assert data["pytest"] == {
        "passed": 2,
        "total": 2,
        "scopes": ["work/A_1", "work/B_2"],
    }
    assert data["board"]["counts_scope"] == "task mytask"
    # 하위호환 top-level 네 키는 고정 스키마고, 전량 dict(`counts_task`)는 board `STATUS_DIRS`
    # 파생이다(T-0839) — 별칭은 그 네 키 값을 보존하고 새 상태를 더 싣는다.
    assert set(data["board"]["counts_task"]) == set(_load("board").STATUS_DIRS)
    for key in ("done", "open", "claimed", "blocked"):
        assert data["board"]["counts_task"][key] == data["board"][key]
    assert "counts_mine" not in data["board"]


def test_task_zero_slots_with_pytest_fails_without_fallback(
    bootstrap, tmp_path, capsys
):
    """0개 + --with-pytest = 대상 없음 fail-loud. PM 홈/다른 task에서 vacuous 실행하지 않는다."""
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=())
    inst = _make(bootstrap, tmp_path, pool)
    with pytest.raises(SystemExit) as exc:
        inst.run(task="mytask", with_pytest=True)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "보유 작업공간이 0개" in err
    assert "전역 auto-slot으로 대체하지 않습니다" in err


def test_task_pytest_rechecks_owner_before_execution(bootstrap, tmp_path, capsys):
    """초기 슬롯 스냅 뒤 realloc되면 pytest를 새 소유자 worktree에서 실행하지 않고 loud 중단한다."""
    slot = "work/A_1"
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=(slot,), branches={slot: "a5"})
    calls = []

    def pytest_fn():
        calls.append("pytest")
        return 0, pytest_summary()

    inst = _make(bootstrap, tmp_path, pool, pytest_fn=pytest_fn)
    # freshness가 끝난 뒤(초기 스냅과 pytest 실행 사이) ownership 변경을 주입한다.
    original_collect = inst._collect_freshness

    def reallocate_after_freshness():
        result = original_collect()
        pool._task_name = "new-owner"
        return result

    inst._collect_freshness = reallocate_after_freshness
    with pytest.raises(SystemExit) as exc:
        inst.run(task="mytask", with_pytest=True)
    assert exc.value.code == 1
    assert calls == []
    assert "소유권 재검증" in capsys.readouterr().err


def _git_call_targets_slot(args, slot: str) -> bool:
    """git 호출(`-C <dir> …`)이 그 슬롯 worktree 를 가리키는가 — 경로 구분자 무관 판정.

    엔진은 `-C` 인자에 **플랫폼 native** 표기(`str(Path)`)를 넘긴다(git 에 그대로 들어가는 실경로라
    표시 경로처럼 POSIX 로 고정하지 않는다). Windows 에선 그 값이 `…\\work\\A_1` 이라 `slot in arg`
    같은 POSIX 전용 부분문자열 판정은 어떤 호출도 못 잡고, 그 판정에 얹은 주입·단언이 조용히 no-op
    이 된다(소유권 차단이 발화하지 않는 게 아니라 **테스트가 그 상황을 못 만든다**·T-0718 축 C).
    """
    return any(slot in str(arg).replace("\\", "/") for arg in args)


def _windows_flavour_worktree_cwd(inst):
    """슬롯 cwd 산출(`_worktree_cwd`)을 Windows 표기로 바꾸는 주입 seam.

    경로 구분자는 OS 가 정하므로 Linux 회귀는 그대로면 Windows 분기를 못 태운다. 슬롯 cwd 는
    엔진의 단일 지점에서 나오므로 여기만 바꾸면 freshness scope → git `-C` 인자까지 Windows 표기가
    흐르고, 그 표기에서도 소유권 재검증이 발화하는지 Linux 에서 확인할 수 있다."""
    base = PureWindowsPath("C:/pmhome")

    def _cwd(slot=None, _inst=inst):
        target = slot or (
            _inst._task_workspace_slots[0] if _inst._task_workspace_slots else None
        )
        return str(base / target) if target else str(base)

    return _cwd


@pytest.mark.parametrize("path_flavour", ["native", "windows"])
def test_task_pull_rechecks_owner_before_mutation(
    bootstrap, tmp_path, capsys, path_flavour
):
    """fetch 뒤 release/realloc되어도 pull은 새 소유자 슬롯에서 실행하지 않고 loud 중단한다.

    슬롯 cwd 표기(native·Windows)는 이 판정의 축이 아니다 — 두 표기 모두에서 같은 차단이
    발화해야 다중 창 동시 push 방어가 OS 를 건너 살아 있다(T-0718 축 C)."""
    slot = "work/A_1"
    pool = _TaskPool(slot_root=tmp_path / "wt", task_slots=(slot,), branches={slot: "a5"})
    calls = []

    def git_fn(args):
        calls.append(args)
        if args[-2:] == ["fetch", "origin"] and _git_call_targets_slot(args, slot):
            pool._task_name = "new-owner"  # probe와 pull 실행 사이의 release/realloc 주입.
            return 0, ""
        if args[-2:] == ["symbolic-ref", "HEAD"]:
            return 0, "refs/heads/a5\n"
        if args[-2:] == ["status", "-s"]:
            return 0, ""
        if "rev-list" in args:
            return 0, "0 1\n"
        return 0, ""

    inst = _make(bootstrap, tmp_path, pool, git_fn=git_fn)
    if path_flavour == "windows":
        inst._worktree_cwd = _windows_flavour_worktree_cwd(inst)
    with pytest.raises(SystemExit) as exc:
        inst.run(task="mytask")
    assert exc.value.code == 1
    # 주입이 실제로 걸렸는지(=fetch 가 그 슬롯을 탔는지) 먼저 단언한다 — 안 걸리면 소유자가
    # 그대로라 차단 없이 통과하는데, 그건 "가드 통과" 가 아니라 "가드 미시험" 이다.
    assert any(
        args[-2:] == ["fetch", "origin"] and _git_call_targets_slot(args, slot)
        for args in calls
    )
    assert not any(
        args[-2:] == ["pull", "--ff-only"] and _git_call_targets_slot(args, slot)
        for args in calls
    )
    assert "소유권 재검증" in capsys.readouterr().err


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
    """main-참조(보호브랜치 직접 checkout) = fault → 차단 + switch 해소 커맨드(엔진-매개·T-0414)."""
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
    # 엔진-매개 단일 커맨드(전환+장부 스냅 재기록 원자·T-0414) — raw git 안내는 해소 직후
    # '기록↔live diverged' 2차 차단을 유발하므로 더는 싣지 않는다.
    assert "worktree_pool.py switch work/A_1 mytask" in cap.err
    assert "git -C work/A_1 switch -c" not in cap.err
    # T-0810 — 스트립이 남긴 빈 괄호·circled marker 잔재(`① 오염()`) 정정 확인
    # (실 CLI 출력 값 단언·조립 문자열 금지).
    assert "()" not in cap.err
    assert "①" not in cap.err
    assert "공유 이력이 오염된다." in cap.err


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


# ── 4. slot-모드 / 솔로 불변 (gate 미진입) ───────────────────────────────────


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
