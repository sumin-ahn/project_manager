"""`pm-config task new <이름>` — task 정체성만 생성하는 CLI (T-0909).

판정은 **실 장부 파일**로 한다 — tmp PM 홈에 실 `.local/worktree-leases.json`·
`.local/tasks/<이름>/pm_state.md` 를 만들게 하고 그 파일의 내용·바이트를 읽어 단언한다(장부/락을
흉내 내는 대역 금지). 커버:

  - 생성 성공: 장부 행 `pid == 0`·`prefix is None` · pm_state 즉시 생성 · 다음 단계 안내 출력.
  - pm_state 동일성: `create_task` 산출이 `bind_task` 신규 생성분과 **바이트 동일**(같은
    `ensure_task_pm_state`).
  - 이어받기: 생성 뒤 `bind_task(같은 이름)` 이 **resumed**(신규 생성 아님·회수 경고 없음).
  - 거부(각각 rc≠0·장부 바이트 불변): 중복 이름 · 동명 종료 archive(`task reopen` 안내) ·
    부적합 이름(traversal) · 슬롯 예약패턴(`<repo>_<N>`).
  - 실 서브프로세스: `python <tmp 홈>/.project_manager/tools/pm_config.py task new <이름>` 의
    rc·stdout·장부(파서 배선까지 실제로 태운다) + 재실행 rc1·장부 불변.
  - 표면 상한: `--prefix` 는 두지 않는다(새 prefix 신설은 `task prefix --user-ack` 게이트 단일 지점).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_STATE_TEMPLATE = REPO / ".project_manager" / "wiki" / "pm_state.template.md"


# ── tmp PM 홈 + 모듈 로드 (test_task_cycle_e2e `_load_wp_bound` 동형) ─────────────


def _make_home(root: Path) -> Path:
    """실 장부를 쓸 tmp PM 홈 골격 — `.local/` + task pm_state template."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    wiki = root / ".project_manager" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PM_STATE_TEMPLATE, wiki / "pm_state.template.md")
    return root


def _load_wp_bound(home: Path):
    """worktree_pool 을 새로 로드하고 경로 전역을 `home` tmp 루트로 재바인딩한다."""
    spec = importlib.util.spec_from_file_location("wp_task_new", TOOLS / "worktree_pool.py")
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
    spec = importlib.util.spec_from_file_location("pm_config_task_new", TOOLS / "pm_config.py")
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


class FakeBoard:
    """board 대역 — 예약패턴 판별 근거(`registered_repos`)만 준다(areas 실파일 무접촉)."""

    def __init__(self, registered=("app",)):
        self._registered = registered

    def registered_repos(self):
        return list(self._registered)


def _run_new(pc, wp, name, *, board=None):
    """`task new <이름>` 을 실 엔진(tmp 바인딩)으로 1회 실행 — rc 반환."""
    return pc.cmd_task_new(
        argparse.Namespace(name=name),
        worktree_pool=wp,
        board=board if board is not None else FakeBoard(),
    )


def _ledger_rows(wp) -> list[dict]:
    """실 장부 파일에서 tasks 컬렉션 원본 dict 를 읽는다(엔진 API 우회·파일 진실)."""
    return json.loads(wp.LEASES_FILE.read_text(encoding="utf-8"))["tasks"]


# ── 생성 성공 ────────────────────────────────────────────────────────────────


def test_task_new_writes_unoccupied_ledger_row_and_pm_state(pc, wp, capsys):
    """`task new` = 정체성만 — 장부 행 `pid 0`·`prefix None` + pm_state 즉시 생성 + 슬롯 0."""
    rc = _run_new(pc, wp, "job")
    assert rc == 0

    rows = _ledger_rows(wp)
    assert [row["name"] for row in rows] == ["job"]
    assert rows[0]["pid"] == 0, "세션에 묶지 않았는데 pid 가 0 이 아니다"
    assert rows[0]["prefix"] is None, "`--prefix` 없는 생성인데 prefix 가 붙었다"
    assert rows[0]["started"]

    state = wp.task_pm_state_file("job")
    assert state.exists(), "task 생성 시 pm_state 즉시 생성 계약 위반"
    assert wp.TASK_PM_STATE_EMPTY_MARKER in state.read_text(encoding="utf-8")
    assert wp.list_leases() == [], "슬롯 대여는 `alloc` 소관 — task new 가 대여했다"

    out = capsys.readouterr().out
    assert "✓ task 생성" in out and "job" in out
    assert "pid 0" in out and "슬롯 0" in out
    assert "pm-bootstrap --task job" in out, "다음 진입(resume) 안내 없음"
    assert "task prefix job" in out, "prefix 지정 경로 안내 없음(`--prefix` 미제공의 짝)"


def test_task_new_pm_state_is_byte_identical_to_bind_task_output(pc, tmp_path, capsys):
    """pm_state 는 `bind_task` 신규 생성분과 **바이트 동일** — 같은 `ensure_task_pm_state`."""
    home_new = _make_home(tmp_path / "home_new")
    home_bind = _make_home(tmp_path / "home_bind")
    wp_new = _load_wp_bound(home_new)
    wp_bind = _load_wp_bound(home_bind)

    assert _run_new(pc, wp_new, "twin") == 0
    record, action, _ = wp_bind.bind_task("twin")
    assert action == "created"

    assert (
        wp_new.task_pm_state_file("twin").read_bytes()
        == wp_bind.task_pm_state_file("twin").read_bytes()
    ), "task new 와 bootstrap 신규 생성의 pm_state 가 갈렸다(템플릿 복제 의심)"
    assert record.prefix is None


def test_task_new_then_bind_task_is_clean_resume(pc, wp, capsys):
    """생성 뒤 bootstrap 진입(`bind_task`)은 **resumed** — 신규 생성도, 회수 경고도 아니다."""
    assert _run_new(pc, wp, "job") == 0
    capsys.readouterr()

    record, action, reclaimed_from = wp.bind_task("job")

    assert action == "resumed", "pid 0(미점유) 재개인데 created/reclaimed 로 분류됐다"
    assert reclaimed_from is None, "정상 인계인데 crash 회수 경고 대상이 됐다"
    assert record.pid == os.getpid()
    assert len(_ledger_rows(wp)) == 1, "재개가 두 번째 행을 만들었다"


# ── 거부 (rc≠0 · 장부 불변) ──────────────────────────────────────────────────


def test_task_new_duplicate_name_refused_without_ledger_change(pc, wp, capsys):
    """같은 이름이 이미 있으면 rc1 — 장부 바이트 불변(재개는 bootstrap 몫)."""
    assert _run_new(pc, wp, "job") == 0
    capsys.readouterr()
    before = wp.LEASES_FILE.read_bytes()

    rc = _run_new(pc, wp, "job")

    assert rc == 1
    assert wp.LEASES_FILE.read_bytes() == before, "중복 거부인데 장부가 변했다"
    err = capsys.readouterr().err
    assert "이미 있다" in err and "pm-bootstrap --task job" in err


def test_task_new_live_task_pid_is_not_touched_by_duplicate_refusal(pc, wp, capsys):
    """살아 있는 task(pid>0) 이름도 rc1 이고 그 pid 를 만지지 않는다."""
    wp.bind_task("busy")            # pid = 이 프로세스(살아 있음)
    capsys.readouterr()
    before = wp.LEASES_FILE.read_bytes()

    assert _run_new(pc, wp, "busy") == 1
    assert wp.LEASES_FILE.read_bytes() == before
    assert wp.find_task("busy").pid == os.getpid()


def test_task_new_archived_name_refused_with_reopen_hint(pc, wp, capsys):
    """동명 종료 archive → rc1 + 기존 `TaskArchived` 사유 + `task reopen` 안내(새 사유 문구 0)."""
    archive = wp.TASKS_DIR / "_ended" / "job-20260901"
    archive.mkdir(parents=True)
    (archive / "pm_state.md").write_text("old-state", encoding="utf-8")

    rc = _run_new(pc, wp, "job")

    assert rc == 1
    assert not wp.LEASES_FILE.exists(), "거부인데 장부 파일이 생겼다"
    assert not wp.task_dir("job").exists(), "거부인데 서술 폴더가 생겼다"
    assert (archive / "pm_state.md").read_text(encoding="utf-8") == "old-state"
    err = capsys.readouterr().err
    assert "종료 archive로 보관됨" in err          # 기존 TaskArchived 사유 그대로
    assert "task reopen job" in err


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".hidden", ""])
def test_task_new_unsafe_name_refused_before_any_write(pc, wp, bad, capsys):
    """부적합 이름 → rc1 · 장부/서술 폴더 무생성(엔진 validator 재사용)."""
    rc = _run_new(pc, wp, bad)

    assert rc == 1
    assert not wp.LEASES_FILE.exists()
    assert not wp.TASKS_DIR.exists()
    assert "부적합 task 명" in capsys.readouterr().err


def test_task_new_reserved_slot_pattern_refused_via_registered_repos(pc, wp, capsys):
    """`<등록 repo>_<N>` 예약패턴 → rc1 (board 에서 해소한 등록 repo 가 판별 근거)."""
    rc = _run_new(pc, wp, "app_2", board=FakeBoard(registered=("app",)))

    assert rc == 1
    assert not wp.LEASES_FILE.exists()
    assert "부적합 task 명" in capsys.readouterr().err


def test_task_new_survives_board_absence_with_traversal_still_blocked(pc, wp, capsys):
    """board 부재(registered 해소 불가)여도 생성은 되고 traversal 검증은 유지된다(fail-soft)."""

    class BoardWithoutRegistry:
        pass

    assert _run_new(pc, wp, "job", board=BoardWithoutRegistry()) == 0
    assert _ledger_rows(wp)[0]["pid"] == 0
    capsys.readouterr()
    assert _run_new(pc, wp, "../evil", board=BoardWithoutRegistry()) == 1


def test_task_new_engine_missing_errors(pc, monkeypatch, capsys):
    """worktree_pool 부재(None) → 명시 에러 rc1(침묵 무력화 금지)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_task_new(argparse.Namespace(name="job"), worktree_pool=None, board=None)
    assert rc == 1
    assert "worktree_pool" in capsys.readouterr().err


# ── 예약패턴 해소 규칙 단일화 (판정 사본 0) ──────────────────────────────────


def test_registered_repos_resolution_is_shared_by_task_new_and_prebound_validation(
        pc, wp, monkeypatch, capsys):
    """예약패턴 판별 근거의 해소가 `_registered_repos` 한 자리다 — 두 호출부가 같은 값을 본다.

    board 모듈 부재·`registered_repos` 속성 부재·조회 예외는 셋 다 None(예약패턴만 완화되고
    traversal 검증은 유지)이고, 실 레지스트리가 있으면 같은 목록이다. 규칙이 두 자리로 갈리면
    같은 이름이 `task new` 에서는 거부되고 `alloc`/`worktree add --task` 에서는 통과한다(또는
    반대)."""

    class BoardWithoutRegistry:
        pass

    class RaisingBoard:
        def registered_repos(self):
            raise RuntimeError("areas broken")

    board = FakeBoard(registered=("app",))
    assert pc._registered_repos(None) is None
    assert pc._registered_repos(BoardWithoutRegistry()) is None
    assert pc._registered_repos(RaisingBoard()) is None
    assert pc._registered_repos(board) == ["app"]

    calls: list = []
    resolve = pc._registered_repos

    def _record(board_mod):
        resolved = resolve(board_mod)
        calls.append((board_mod, resolved))
        return resolved

    monkeypatch.setattr(pc, "_registered_repos", _record)

    # 생성 경로(기바인딩 미요구)와 기바인딩 요구 경로가 같은 이름을 같은 근거로 거부한다.
    assert _run_new(pc, wp, "app_2", board=board) == 1
    assert pc._validate_prebound_task(wp, board, "app_2") == 1
    assert "부적합 task 명" in capsys.readouterr().err

    assert [seen for seen, _ in calls] == [board, board], \
        "두 호출부가 같은 해소 자리를 부르지 않았다(사본이 남아 있다)"
    assert [resolved for _, resolved in calls] == [["app"], ["app"]]
    assert not wp.LEASES_FILE.exists(), "거부 경로가 장부를 건드렸다"


# ── 파서 표면 (상한: 새 서브커맨드 하나 · 새 플래그 0) ────────────────────────


def test_task_new_parser_leaf_is_wired_to_cmd_task_new(pc):
    """`task new <이름>` 이 파서 leaf 로 등록되고 `cmd_task_new` 로 배선된다."""
    args = pc.build_parser().parse_args(["task", "new", "job"])
    assert args.command == "task" and args.task_command == "new"
    assert args.name == "job"
    assert args.func is pc.cmd_task_new


def test_task_new_has_no_prefix_flag(pc):
    """`--prefix` 는 두지 않는다 — 새 prefix 신설 승인 게이트는 `task prefix` 단일 지점."""
    with pytest.raises(SystemExit) as exc_info:
        pc.build_parser().parse_args(["task", "new", "job", "--prefix", "pay"])
    assert exc_info.value.code == 2


def test_task_group_help_lists_new(pc):
    """`task` 그룹 help 가 `new` 를 표면화한다(진입점이 둘이 된 사실)."""
    help_text = pc.build_parser().format_help()
    assert "task 정체성 관리 (new·prefix·end·reopen)" in help_text


# ── 실 서브프로세스 (파서 배선 + 엔진 로드까지 실제로 태운다) ─────────────────


def _copy_engine_home(root: Path) -> Path:
    """tmp PM 홈에 엔진 사본 + task pm_state template — 실 CLI 를 그 홈에서 돌린다."""
    home = _make_home(root)
    shutil.copytree(
        TOOLS,
        home / ".project_manager" / "tools",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    return home


def _run_cli(home: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_config.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(home), timeout=180,
    )


def test_task_new_real_cli_creates_identity_and_refuses_rerun(tmp_path):
    """실 CLI 1회차 rc0(장부·pm_state 생성) · 2회차 rc1(장부 바이트 불변)."""
    home = _copy_engine_home(tmp_path / "cli_home")

    first = _run_cli(home, "task", "new", "demo")
    assert first.returncode == 0, f"rc={first.returncode}\nstdout={first.stdout}\nstderr={first.stderr}"
    assert "✓ task 생성" in first.stdout and "demo" in first.stdout
    assert "pm-bootstrap --task demo" in first.stdout

    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    rows = json.loads(ledger.read_text(encoding="utf-8"))["tasks"]
    assert [row["name"] for row in rows] == ["demo"]
    assert rows[0]["pid"] == 0 and rows[0]["prefix"] is None
    state = home / ".project_manager" / ".local" / "tasks" / "demo" / "pm_state.md"
    assert state.exists()

    before = ledger.read_bytes()
    second = _run_cli(home, "task", "new", "demo")
    assert second.returncode == 1
    assert "이미 있다" in second.stderr
    assert ledger.read_bytes() == before, "중복 거부인데 실 장부가 변했다"
