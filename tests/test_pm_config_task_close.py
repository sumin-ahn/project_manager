"""`pm-config task close <이름>` — 열린 채 남은 세션 기록을 사용자가 닫는다 (T-0920 AT-006).

세션이 핸드오프 없이 끝나면 장부 task 행이 열린 채 남아 다음 부트스트랩이 거부한다. 그것을
닫는 것은 **사용자 행위**다(엔진 자동 회수·만료 시각 없음). 이 명령은 **기록만** 닫는다 —
복원(claim 티켓·라운드 단계 등)은 부트스트랩 `--force` 인계 몫이라 여기서 내지 않는다.
작업 폴더 쪽 짝은 기존 `pm-config release <slot> --force` 이고 새로 만들지 않았다.

판정은 실 PM 홈 + 실 서브프로세스로 한다(장부 파일 바이트를 그대로 읽는다).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import git_required, load_pool, make_pm_home, pm_config

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

pytestmark = git_required


@pytest.fixture(scope="module")
def pc():
    specification = importlib.util.spec_from_file_location(
        "pm_config_task_close", TOOLS / "pm_config.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path):
    return make_pm_home(tmp_path / "home")


def _ledger(home: Path) -> dict:
    """장부 원본 — 아직 아무 op 도 없으면 파일 자체가 없다(빈 장부로 읽는다)."""
    path = home / ".project_manager" / ".local" / "worktree-leases.json"
    if not path.is_file():
        return {"leases": [], "tasks": []}
    document = json.loads(path.read_text(encoding="utf-8"))
    return {"leases": document.get("leases", []), "tasks": document.get("tasks", [])}


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_config.py"), *args],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace")


def _open_task(home: Path, name: str) -> None:
    """세션이 연 채 끝난 상태 — 실 엔진으로 열고 종료 기록을 남기지 않는다."""
    pool = load_pool(home)
    pool.bind_task(name, session="s-test")


# ── ① 기록만 닫는다 ─────────────────────────────────────────────────────────


def test_close_flips_the_open_record_of_a_real_session(home):
    """실 서브프로세스로 닫는다 — 열림에서 닫힘으로, 그 행 하나만."""
    pm_config(home, "task", "new", "sibling")
    _open_task(home, "job")
    assert [bool(t["session"]) for t in _ledger(home)["tasks"] if t["name"] == "job"] == [True]

    done = _cli(home, "task", "close", "job")

    assert done.returncode == 0, done.stderr
    rows = {task["name"]: task for task in _ledger(home)["tasks"]}
    assert rows["job"]["session"] == ""
    assert rows["sibling"]["session"] == ""       # 형제 행 무접촉
    assert "복원" not in done.stdout or "복원 없음" in done.stdout


def test_close_does_not_report_what_the_session_was_doing(home):
    """이 경로는 기록만 닫는다 — claim 티켓·라운드 단계 같은 복원 정보를 함께 내지 않는다."""
    _open_task(home, "job")

    out = _cli(home, "task", "close", "job").stdout

    for forbidden in ("claim 중인 티켓", "미회수 위임 준비", "라운드 단계", "마지막 기록"):
        assert forbidden not in out, f"닫기가 복원 정보를 냈다: {forbidden}"


def test_close_touches_nothing_but_that_row(home):
    """장부의 그 행 말고는 1바이트도 바꾸지 않는다 — 서술·log·board·슬롯 lease 전부."""
    pm_config(home, "task", "new", "job")
    _open_task(home, "job")
    watched = [
        home / ".project_manager" / ".local" / "tasks" / "job" / "pm_state.md",
        home / ".project_manager" / "wiki" / "log" / "current.md",
    ]
    before = {path: path.read_bytes() for path in watched if path.is_file()}
    assert before, "감시할 파일이 없으면 이 단언은 무의미하다"
    leases_before = _ledger(home)["leases"]

    assert _cli(home, "task", "close", "job").returncode == 0

    for path, blob in before.items():
        assert path.read_bytes() == blob, f"닫기가 {path.name} 를 건드렸다"
    assert _ledger(home)["leases"] == leases_before


def test_closing_a_closed_record_is_harmless(home):
    """이미 닫힌 기록은 바꿀 것이 없다 — rc 0 이고 장부 바이트가 그대로다."""
    pm_config(home, "task", "new", "job")
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    before = ledger.read_bytes()

    done = _cli(home, "task", "close", "job")

    assert done.returncode == 0
    assert "이미 닫혀" in done.stdout
    assert ledger.read_bytes() == before


def test_closing_an_unknown_task_is_refused(home):
    """등록 task 가 아니면 rc1 — 없는 이름으로 행을 만들지 않는다."""
    done = _cli(home, "task", "close", "absent")

    assert done.returncode == 1
    assert _ledger(home)["tasks"] == []


# ── ② 값은 열림/닫힘 둘뿐 · 새 축 0 ─────────────────────────────────────────


def test_close_writes_through_the_existing_primitive(pc, monkeypatch, capsys):
    """장부 write 를 재구현하지 않는다 — 기존 `close_task_session` 하나만 부른다."""
    calls: list[str] = []

    class _Pool:
        InvalidTaskName = type("InvalidTaskName", (Exception,), {})

        def find_task(self, name):
            return type("_T", (), {"name": name, "session": "s-test", "open": True})()

        def close_task_session(self, name):
            calls.append(name)
            return type("_T", (), {"name": name, "session": "", "open": False})()

    rc = pc.cmd_task_close(argparse.Namespace(name="job"), worktree_pool=_Pool())

    assert rc == 0
    assert calls == ["job"], "기존 프리미티브 한 번이 아니다"
    assert "닫" in capsys.readouterr().out


def test_close_takes_no_expiry_or_grace_argument(pc):
    """만료 시각·유예 시간 같은 새 축을 인자로도 받지 않는다."""
    parser = pc.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["task", "close", "job", "--after", "10m"])
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "close", "job", "--grace", "10"])
    assert parser.parse_args(["task", "close", "job"]).name == "job"


def test_the_record_has_only_two_values(home):
    """세 번째 상태값(closed·stale 같은)을 쓰지 않는다 — 값은 true/false 뿐이다."""
    _open_task(home, "job")
    _cli(home, "task", "close", "job")

    row = _ledger(home)["tasks"][0]

    assert row["session"] == ""
    assert "pid" not in row and "state" not in row and "expires_at" not in row


def test_the_work_folder_side_uses_the_existing_release_force(home, pc):
    """작업 폴더 쪽 짝은 기존 명령이다 — 이 티켓이 슬롯용 새 명령을 만들지 않았다."""
    commands = pc.build_parser()._subparsers._group_actions[0].choices

    assert "release" in commands
    assert "--force" in commands["release"].format_help()
    assert not any(name.startswith("slot-close") or name == "close" for name in commands)
