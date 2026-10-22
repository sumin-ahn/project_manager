"""세션 살아있음 판정 = 시작 기록과 종료 기록 (T-0920 AT-002).

프로세스 조회로 세션 생존을 판정하던 축을 폐기하고, 판정을 장부의 두 값으로 옮겼다. 이 파일이
재는 것은 셋이다:

  1. **열림/닫힘 두 값** — 작업 폴더는 `state=="leased"` + 점유 `session` 이 열림이고, 그에 대응
     하는 종료 기록(`idle` + 빈 session)이 닫힘이다. 중간 상태·만료 시각은 없다.
  2. **산출 계약** — `status --json` 의 슬롯/task 행에 `pid` 키가 없고 `alive` 는 위 판정 그대로다.
  3. **되살아나지 않음** — 세션 pid 해소 식별자가 소스 전역에서 0이고, `pid_is_alive` 는 엔진이
     직접 띄운 위임 자식 프로세스를 보는 `pm_delegate` 안에서만 불린다(세션 축 0).
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
from pathlib import Path

import pytest

from _repo_owned_inventory import OWNED, repo_owned_paths
from pm_engine.infra import pm_paths
from pm_engine.infra import pool_store
from pm_engine.app import worktree_pool
from pm_engine.domain import worktree_records
from pm_engine.app import worktree_status

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 폐기된 세션 pid 해소 축의 식별자 — 소스 어디에도 남으면 안 된다(다른 이름으로도 살아나지
# 않게, 판정 함수 이름을 그대로 텍스트로 잰다).
_RETIRED_IDENTIFIERS = (
    "session_pid",
    "_posix_process_table",
    "_windows_process_table",
    "SessionPidUnresolved",
    "_session_pid",
)
# `pid_is_alive` 가 남아도 되는 유일한 축 — 엔진이 스스로 띄운 위임 자식 프로세스.
_CHILD_PROCESS_OWNER = "pm_delegate.py"
# 이 파일 자신은 그 식별자들을 문자열로 들고 있으므로 스캔 대상에서 뺀다.
_SELF = Path(__file__).name


def _sources() -> list[Path]:
    """스캔 대상 — repo-owned 열거 seam 이 낸 `.py` 전부(직접 tree-walk 금지)."""
    files: list[Path] = []
    for subtree in (".project_manager/tools", "manager", "tests"):
        files += [path for path in repo_owned_paths(REPO, subtree, mode=OWNED)
                  if path.suffix == ".py" and path.name != _SELF]
    assert files, "스캔 대상 소스가 없다(경로 오류)"
    return files


@pytest.fixture(scope="module")
def wp():
    specification = importlib.util.spec_from_file_location(
        "worktree_pool_alive_records", TOOLS / "worktree_pool.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _lease(wp, row: dict):
    return worktree_records.Lease.from_dict(row)


# ── ① 열림 기록 / 종료 기록 두 값 ────────────────────────────────────────────


def test_an_open_record_without_a_close_record_reads_open(wp):
    """시작만 하고 종료 안 한 기록은 열림이다 — 그 명령 프로세스는 이미 죽었어도."""
    lease = _lease(wp, {"slot": "work/proj_1", "repo": "proj",
                        "session": "proj_1", "state": "leased"})

    assert worktree_records.lease_is_open(lease) is True


def test_a_close_record_reads_closed(wp):
    """종료 기록(release/force_release/end_task 의 idle + 빈 session)은 닫힘이다."""
    lease = _lease(wp, {"slot": "work/proj_1", "repo": "proj",
                        "session": "", "state": "idle"})

    assert worktree_records.lease_is_open(lease) is False


def test_a_readonly_row_is_closed_because_it_has_no_session(wp):
    """readonly 는 죽은 세션이 아니라 세션이 *없는* 행이다 — 역할 분기 없이 닫힘으로 읽힌다."""
    lease = _lease(wp, {"slot": "work/proj_3", "repo": "proj", "session": None,
                        "state": "leased", "role": "readonly"})

    assert worktree_records.lease_is_open(lease) is False


def test_the_round_trip_of_a_real_release_flips_the_record(wp, tmp_path, monkeypatch):
    """실 장부 왕복 — 대여가 열고 반납이 닫는다(값은 두 개뿐)."""
    monkeypatch.setattr(wp, "REPO", tmp_path)
    local_dir = tmp_path / ".project_manager" / ".local"
    assert pm_paths.slot_path(pm_paths.home_at(wp.REPO), "work/proj_1") == tmp_path / "work" / "proj_1"
    expected_lock = tmp_path / ".project_manager" / ".local" / "worktree-leases.lock"
    real_lock = pool_store.fs.exclusive_file_lock
    lock_paths = []

    @contextlib.contextmanager
    def observed_lock(path, *args, **kwargs):
        assert path == expected_lock
        lock_paths.append(path)
        with real_lock(path, *args, **kwargs):
            yield

    monkeypatch.setattr(pool_store.fs, "exclusive_file_lock", observed_lock)
    pm_paths.leases_file(pm_paths.home_at(wp.REPO)).parent.mkdir(parents=True, exist_ok=True)
    pm_paths.leases_file(pm_paths.home_at(wp.REPO)).write_text(json.dumps({"leases": [{
        "slot": "work/proj_1", "repo": "proj", "session": "", "started": "t",
        "state": "idle", "test_cmd": None,
    }], "tasks": []}), encoding="utf-8")

    leased = worktree_pool.alloc(pm_paths.home_at(wp.REPO), "proj", owner_task="proj_1", git_runner=lambda _argv: (0, ""))
    assert worktree_records.lease_is_open(leased) is True

    released = worktree_pool.release(pm_paths.home_at(wp.REPO), "work/proj_1", git_runner=lambda _argv: (0, ""))
    assert worktree_records.lease_is_open(released) is False
    assert lock_paths == [expected_lock, expected_lock]
    assert expected_lock.is_file()




def test_the_task_record_has_only_two_values(wp):
    """task 행의 열림은 연 세션 ID 에서 파생한다 — 옛 `pid`·`open` 은 어느 세션인지 몰라 닫힘으로 읽는다."""
    migrated = worktree_records.Task.from_dict({"name": "job", "prefix": None,
                                  "pid": 1613552, "open": True, "started": "t"})
    bound = worktree_records.Task.from_dict({"name": "job", "prefix": None, "session": "ses-1", "started": "t"})

    assert migrated.open is False and bound.open is True
    assert "pid" not in migrated.to_dict() and "open" not in migrated.to_dict()
    assert migrated.to_dict()["session"] == "" and bound.to_dict()["session"] == "ses-1"


# ── ② 산출 계약 — `status --json` 에 pid 키가 없다 ───────────────────────────


def test_the_status_json_rows_have_no_pid_key(wp, tmp_path, monkeypatch):
    """산출 dict 의 키 집합으로 잰다 — 문구가 아니라 값으로 부재를 확인한다."""
    specification = importlib.util.spec_from_file_location(
        "pm_config_alive_records", TOOLS / "pm_config.py")
    pc = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(pc)

    lease = _lease(wp, {"slot": "work/proj_1", "repo": "proj",
                        "session": "proj_1", "state": "leased"})
    # 슬롯 git 조회 대역 — 엔진이 worktree 없는 슬롯에 돌려주는 모양 그대로(부모 기록 없음).
    monkeypatch.setattr(worktree_status, "slot_git_status", lambda _home, slot, *, repo_reads: {
        "slot": slot, "branch": None, "head": None, "parent": None, "children": [],
        "stashes": 0, "dirty": False, "folders": None,
    }, raising=False)
    monkeypatch.setattr(pool_store, "slots_for_task", lambda _home, _name: [], raising=False)
    task = worktree_records.Task(name="proj_1", session="s-test", started="t")

    slot_rows = pc._status_json_slots( [lease], {"proj_1"})
    task_rows = pc._status_json_tasks( [task])

    assert slot_rows[0]["alive"] is True and "pid" not in slot_rows[0]
    assert task_rows[0]["alive"] is True and "pid" not in task_rows[0]


def test_the_screen_rule_table_has_no_pid_row():
    """화면 판정표에도 `pid` 를 보는 규칙이 없다(영원히 안 맞는 규칙을 남기지 않는다)."""
    rules = json.loads(
        (REPO / "manager" / "ui" / "src" / "lib" / "status-dots.json").read_text(encoding="utf-8")
    )["rules"]

    assert [rule for rule in rules if rule["field"] == "pid"] == []


# ── ③ 같은 개념이 다른 이름으로 살아나지 않는다 ─────────────────────────────


def test_no_source_resolves_a_session_pid():
    """세션 pid 해소 식별자가 소스 전역에 0 — 텍스트 단언(되살아남 차단)."""
    offenders: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for identifier in _RETIRED_IDENTIFIERS:
            if identifier in text:
                offenders.append(f"{path.relative_to(REPO)}: {identifier}")

    assert offenders == [], "폐기한 세션 pid 해소가 되살아났다: " + ", ".join(offenders)


def test_pid_liveness_is_only_asked_about_delegate_children():
    """`pid_is_alive` 호출은 위임 자식 프로세스 축 하나뿐 — 세션 축 호출 0."""
    callers: list[str] = []
    for path in repo_owned_paths(REPO, ".project_manager/tools", mode=OWNED):
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "pid_is_alive":
                    callers.append(path.name)

    assert set(callers) == {_CHILD_PROCESS_OWNER}, f"세션 축에서 생존을 조회한다: {sorted(set(callers))}"
    assert callers.count(_CHILD_PROCESS_OWNER) == 3, "위임 자식 조회 자리 수가 바뀌었다"


def test_no_engine_source_reintroduces_a_pool_liveness_helper():
    """`worktree_pool` 에 생존 판정 헬퍼(`_pid_alive`)를 되살리지 않는다."""
    text = (TOOLS / "worktree_pool.py").read_text(encoding="utf-8")

    assert "_pid_alive" not in text
    assert "def reclaim_stale" not in text, "자동 회수는 대체하지 않고 지웠다"
