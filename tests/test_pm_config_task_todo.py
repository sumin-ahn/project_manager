"""`pm-config task todo <add|list|done>` — task 의 열린 할 일 CLI.

판정은 **실 할 일 파일**로 한다 — tmp PM 홈에 실 `.local/tasks/<이름>/todo.json` 을 만들게 하고 그
파일 내용을 읽어 단언한다(저장/락을 흉내 내는 대역 금지·`test_pm_config_task_new` 동형). 커버:

  - `add`: 파일에 열린 todo 가 쌓이고 등록 시각·ID 를 엔진이 박는다 · 출력이 ID·등록일을 보인다.
  - `list`: ID·등록일·묵은 일수 표시 · 빈 목록은 등록 경로를 안내한다.
  - `done`: 목록에서 제거하고 완료 표시를 남기지 않는다 · 마지막 하나를 지우면 쓰기에 경고가 붙는다고 알린다.
  - 거부(각각 rc1·파일 바이트 불변): 부재 task · 빈 내용 · 없는 ID · 부적합 이름(traversal).
  - 엔진 부재: worktree_pool 로드 실패는 rc1(조용한 성공 없음).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_STATE_TEMPLATE = REPO / ".project_manager" / "wiki" / "pm_state.template.md"


# ── tmp PM 홈 + 모듈 로드 (test_pm_config_task_new 동형) ──────────────────────


def _make_home(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    wiki = root / ".project_manager" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PM_STATE_TEMPLATE, wiki / "pm_state.template.md")
    return root


def _load_wp_bound(home: Path):
    spec = importlib.util.spec_from_file_location("wp_task_todo", TOOLS / "worktree_pool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    local = home / ".project_manager" / ".local"
    for name, val in {
        "REPO": home,
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "LEASES_LOCK": local / "worktree-leases.lock",
        "TASKS_DIR": local / "tasks",
        "WORK_DIR": home / "work",
        "REPOS_DIR": home / ".repos",
        "REPO_HOOKS_DIR": local / "repo-hooks",
    }.items():
        setattr(mod, name, val)
    return mod


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config_task_todo", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pc():
    return _load_pm_config()


@pytest.fixture
def home(tmp_path):
    return _make_home(tmp_path / "home")


@pytest.fixture
def wp(home):
    return _load_wp_bound(home)


@pytest.fixture
def task(wp):
    """todo 를 달 대상 task 하나 — todo 는 기존 레코드에만 달린다."""
    wp.bind_task("job", session="s-test")
    return "job"


def _add(pc, wp, name, text):
    return pc.cmd_task_todo_add(
        argparse.Namespace(name=name, text=text), worktree_pool=wp)


def _list(pc, wp, name):
    return pc.cmd_task_todo_list(argparse.Namespace(name=name), worktree_pool=wp)


def _done(pc, wp, name, todo_id):
    return pc.cmd_task_todo_done(
        argparse.Namespace(name=name, todo_id=todo_id), worktree_pool=wp)


def _todo_rows(wp, name="job") -> list[dict]:
    """실 할 일 파일에서 그 task 의 목록 원본을 읽는다(엔진 API 우회·파일 진실)."""
    return json.loads(wp.task_todo_file(name).read_text(encoding="utf-8"))["todos"]


# ── add ──────────────────────────────────────────────────────────────────────


def test_todo_add_writes_the_task_file_and_shows_the_id_line(pc, wp, task, capsys):
    """`todo add` = 열린 todo 적재. 등록 시각·ID 는 엔진이 박고 출력이 ID 로 보인다."""
    assert _add(pc, wp, task, "todo 저장소 만들기") == 0
    rows = _todo_rows(wp)
    assert [row["text"] for row in rows] == ["todo 저장소 만들기"]
    assert rows[0]["id"] == 1
    assert rows[0]["created"], "등록 시각이 비었다 — 묵은 정도를 잴 근거가 없다"

    out = capsys.readouterr().out
    assert "열린 todo 1건" in out
    assert "#1 todo 저장소 만들기" in out
    assert "등록 " in out, "등록일 표시 없음"


def test_todo_add_accumulates_in_order(pc, wp, task, capsys):
    """여러 건은 넣은 순서대로 쌓이고 ID 도 그 순서다(같은 내용 중복도 막지 않는다)."""
    for text in ("첫째", "둘째", "첫째"):
        assert _add(pc, wp, task, text) == 0
    rows = _todo_rows(wp)
    assert [row["text"] for row in rows] == ["첫째", "둘째", "첫째"]
    assert [row["id"] for row in rows] == [1, 2, 3]
    out = capsys.readouterr().out
    assert "열린 todo 3건" in out


def test_todo_add_missing_task_is_rc1_without_creating(pc, wp, capsys):
    """부재 task → rc1 + 생성 경로 안내. todo 가 task 를 만들지 않는다."""
    assert _add(pc, wp, "absent", "x") == 1
    assert wp.list_tasks() == []
    err = capsys.readouterr().err
    assert "task new absent" in err and "pm-bootstrap" in err


def test_todo_add_empty_text_is_rc1_without_write(pc, wp, task, capsys):
    """빈 내용 → rc1(파일 불변) — 빈 줄 하나로 쓰기 경고가 꺼지면 안 된다."""
    assert _add(pc, wp, task, "   ") == 1
    assert not wp.task_todo_file(task).exists()
    assert "비어 있다" in capsys.readouterr().err


def test_todo_add_invalid_task_name_is_rc1(pc, wp, capsys):
    """traversal 이름 → rc1(장부 write 이전 거부·형제 write 진입점 동형)."""
    assert _add(pc, wp, "../evil", "x") == 1
    assert capsys.readouterr().err.startswith("[중단] 부적합 task 명")


# ── list ─────────────────────────────────────────────────────────────────────


def test_todo_list_shows_id_date_and_age(pc, wp, task, capsys):
    """`todo list` 는 ID·내용·등록일·묵은 일수를 보인다(표시는 엔진 단일 함수)."""
    wp.add_task_todo(task, "묵은 일", now=lambda: "2026-01-02T00:00:00+00:00")
    capsys.readouterr()
    assert _list(pc, wp, task) == 0
    out = capsys.readouterr().out
    assert "#1 묵은 일 (등록 2026-01-02 · " in out and "일째)" in out


def test_todo_list_empty_points_at_add(pc, wp, task, capsys):
    """열린 todo 가 없으면 등록 경로를 그 자리에서 안내한다(rc 0)."""
    assert _list(pc, wp, task) == 0
    assert f"task todo add {task}" in capsys.readouterr().out


def test_todo_list_missing_task_is_rc1(pc, wp, capsys):
    assert _list(pc, wp, "absent") == 1
    assert "task new absent" in capsys.readouterr().err


# ── done ─────────────────────────────────────────────────────────────────────


def test_todo_done_removes_by_id_and_leaves_no_marker(pc, wp, task, capsys):
    """완료 = 목록에서 제거. 완료 표시를 파일에 남기지 않는다(단일 진실은 log)."""
    _add(pc, wp, task, "첫째")
    _add(pc, wp, task, "둘째")
    capsys.readouterr()

    assert _done(pc, wp, task, 1) == 0
    rows = _todo_rows(wp)
    assert [row["text"] for row in rows] == ["둘째"]
    assert [row["id"] for row in rows] == [2], "남은 줄의 ID 가 밀렸다 — 번호가 다른 일을 가리킨다"
    assert all("done" not in row and "status" not in row for row in rows), \
        "완료 표시 필드가 생겼다 — 상태 필드를 두지 않는 설계 위반"

    out = capsys.readouterr().out
    assert "완료: 첫째" in out and "열린 todo 1건" in out


def test_todo_done_last_one_warns_writes_are_flagged(pc, wp, task, capsys):
    """마지막 하나를 지우면 그 상태에서 파일 수정에 경고가 붙는다고 그 자리에서 알린다."""
    _add(pc, wp, task, "하나")
    capsys.readouterr()
    assert _done(pc, wp, task, 1) == 0
    out = capsys.readouterr().out
    assert "열린 todo 없음" in out and "경고" in out


def test_todo_done_unknown_id_is_rc1_without_write(pc, wp, task, capsys):
    """없는 ID → rc1(파일 불변) — 무시하면 지운 줄 알고 넘어간다."""
    _add(pc, wp, task, "하나")
    before = wp.task_todo_file(task).read_bytes()
    capsys.readouterr()
    for bad in (0, 2, -1):
        assert _done(pc, wp, task, bad) == 1
    assert wp.task_todo_file(task).read_bytes() == before
    assert "없다" in capsys.readouterr().err


def test_todo_done_missing_task_is_rc1(pc, wp, capsys):
    assert _done(pc, wp, "absent", 1) == 1
    assert "task new absent" in capsys.readouterr().err


# ── 엔진 부재 ────────────────────────────────────────────────────────────────


def test_todo_commands_without_engine_are_rc1(pc, monkeypatch, capsys):
    """worktree_pool 로드 실패는 세 명령 모두 rc1 — 조용한 성공 없음."""
    monkeypatch.setattr(pc, "_load_module", lambda *a, **k: None)
    assert pc.cmd_task_todo_add(
        argparse.Namespace(name="job", text="x"), worktree_pool=None) == 1
    assert pc.cmd_task_todo_list(argparse.Namespace(name="job"), worktree_pool=None) == 1
    assert pc.cmd_task_todo_done(
        argparse.Namespace(name="job", todo_id=1), worktree_pool=None) == 1
    err = capsys.readouterr().err
    assert err.count("worktree_pool.py 엔진을 찾을 수 없다") == 3


# ── 파서 배선 ────────────────────────────────────────────────────────────────


def test_parser_wires_three_todo_subcommands(pc):
    """`task todo <add|list|done>` 이 각 핸들러로 배선된다(인자 모양 포함)."""
    parser = pc.build_parser()
    add = parser.parse_args(["task", "todo", "add", "job", "할 일"])
    assert (add.func, add.name, add.text) == (pc.cmd_task_todo_add, "job", "할 일")
    listed = parser.parse_args(["task", "todo", "list", "job"])
    assert (listed.func, listed.name) == (pc.cmd_task_todo_list, "job")
    done = parser.parse_args(["task", "todo", "done", "job", "3"])
    assert (done.func, done.name, done.todo_id) == (pc.cmd_task_todo_done, "job", 3)
