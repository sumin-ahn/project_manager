"""T-0646 — slot-mode pm_state 초기화 계약 회귀 테스트."""
from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".project_manager" / "tools"
TEMPLATE = ROOT / ".project_manager" / "wiki" / "pm_state.template.md"


# `_load` 가 호출마다 새 모듈을 만들기 때문에 재앵커는 로더 안에서 건다(아래 autouse fixture 가
# 테스트별 tmp 경로를 심는다).
_DASHBOARD_REDIRECT: dict[str, Path] = {}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"t0646_{name}", TOOLS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = _DASHBOARD_REDIRECT.get("path")
    if target is not None and hasattr(module, "_dashboard_file"):
        module._dashboard_file = lambda t=target: t
    return module


@pytest.fixture(autouse=True)
def _hermetic_dashboard(tmp_path, monkeypatch):
    """slot 대시보드 렌더를 tmp 로 재앵커한다(이 모듈의 모든 신규 로드에 적용).

    `_dashboard_file()` 은 모듈 `REPO` 를 따라가므로 재앵커 없이 부트스트랩/핸드오프를 돌리면
    실 작업 트리의 `wiki/log/dashboard.md` 를 갱신한다(tests/conftest.py 의 live board 오염 가드가
    teardown 에서 잡는다). 렌더 내용이 앞선 테스트가 남긴 리스 상태에 좌우돼 단독 실행에서는
    재현되지 않는다. `_load` 는 호출마다 새 모듈 객체를 만들므로 리다이렉트를 로더에 건다."""
    monkeypatch.setitem(_DASHBOARD_REDIRECT, "path", tmp_path / "dashboard.md")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    local = root / ".project_manager" / ".local"
    wiki = root / ".project_manager" / "wiki"
    local.mkdir(parents=True)
    wiki.mkdir(parents=True)
    (root / "work").mkdir()
    (root / ".repos").mkdir()
    shutil.copyfile(TEMPLATE, wiki / "pm_state.template.md")
    return root


@pytest.fixture
def wp(project: Path):
    module = _load("worktree_pool")
    local = project / ".project_manager" / ".local"
    module.REPO = project
    module.LOCAL_DIR = local
    module.LEASES_FILE = local / "worktree-leases.json"
    module.LEASES_LOCK = local / "worktree-leases.lock"
    module.TASKS_DIR = local / "tasks"
    module.WORK_DIR = project / "work"
    module.REPOS_DIR = project / ".repos"
    module.REPO_HOOKS_DIR = local / "repo-hooks"
    return module


def _patch_atomic_replace(monkeypatch, module, fake):
    """엔진이 원자 교체에 **실제로 부르는** seam(`file_lock.atomic_replace`)을 대역으로 바꾼다.

    `os.replace` 는 그 seam 의 POSIX 분기 구현 세부다 — Windows 분기는 Win32 rename 이라
    `os.replace` 에 건 주입을 지나지 않는다. 관측 지점이 엔진의 호출 지점과 같아야 두 OS 에서
    같은 성질이 고정된다. worktree_pool 은 seam 을 import 시점에 전역(`file_lock`)으로 받으므로
    그 객체에 건다. 반환값은 seam 모듈 — 실패 주입이 Windows 분기와 같은 예외 클래스
    (`AtomicReplaceError`·`OSError` 서브클래스)를 쓸 수 있다.
    """
    seam = module.file_lock
    monkeypatch.setattr(seam, "atomic_replace", fake)
    return seam


def _lease(wp, *, slot="work/A_1", session="A_1", state="leased", role="work"):
    return wp.Lease(
        slot=slot,
        repo="A",
        session=session,
        pid=os.getpid(),
        started="before",
        state=state,
        role=role,
    )


def _seed(wp, *leases) -> None:
    with wp._lease_lock():
        wp._write_ledger(list(leases))


def _seed_tasks(wp, *names: str) -> None:
    with wp._lease_lock():
        wp._write_tasks([wp.Task(name=name, pid=1, started="before") for name in names])


def _snapshot_git(argv: list[str]) -> tuple[int, str]:
    if argv == ["rev-parse", "HEAD"]:
        return 0, "a" * 40 + "\n"
    if argv == ["symbolic-ref", "HEAD"]:
        return 0, "refs/heads/A_1\n"
    if argv == ["submodule", "status"]:
        return 0, ""
    return 0, ""


def _create_git(argv: list[str]) -> tuple[int, str]:
    if "--is-bare-repository" in argv:
        return 0, "true\n"
    if "--verify" in argv and argv[-1] == "HEAD":
        return 0, "a" * 40 + "\n"
    if argv == ["rev-parse", "HEAD"]:
        return 0, "a" * 40 + "\n"
    if argv == ["symbolic-ref", "HEAD"]:
        return 1, ""
    return 0, ""


def test_ensure_slot_pm_state_renders_first_session_with_atomic_replace(
    wp, monkeypatch
):
    _seed(wp, _lease(wp))
    target = wp.slot_pm_state_file("work/A_1")
    real_replace = wp.file_lock.atomic_replace
    replacements: list[tuple[Path, Path]] = []

    def replace(src, dst):
        replacements.append((Path(src), Path(dst)))
        real_replace(src, dst)

    _patch_atomic_replace(monkeypatch, wp, replace)
    result = wp.ensure_slot_pm_state("A_1")

    assert result == target
    assert replacements == [(target.with_suffix(".md.tmp"), target)]
    assert not target.with_suffix(".md.tmp").exists()
    text = target.read_text(encoding="utf-8")
    assert "{{DATE}}" not in text
    assert wp.TASK_PM_STATE_EMPTY_MARKER in text
    assert "  - **1차**" not in text
    assert "## 남은 작업 전체 그림" in text


def test_ensure_slot_pm_state_existing_file_is_byte_invariant_without_template(wp):
    _seed(wp, _lease(wp))
    target = wp.slot_pm_state_file("work/A_1")
    target.parent.mkdir(parents=True)
    original = b"\x00existing-slot-state\xff"
    target.write_bytes(original)
    (wp.REPO / ".project_manager" / "wiki" / "pm_state.template.md").unlink()

    assert wp.ensure_slot_pm_state("work/A_1") == target
    assert target.read_bytes() == original


@pytest.mark.parametrize("template_text", [None, "# damaged template\n"])
def test_ensure_slot_pm_state_template_absent_or_damaged_fails_loud_without_partial(
    wp, template_text
):
    _seed(wp, _lease(wp))
    template = wp.REPO / ".project_manager" / "wiki" / "pm_state.template.md"
    if template_text is None:
        template.unlink()
        expected = FileNotFoundError
    else:
        template.write_text(template_text, encoding="utf-8")
        expected = ValueError

    target = wp.slot_pm_state_file("work/A_1")
    with pytest.raises(expected):
        wp.ensure_slot_pm_state("work/A_1")
    assert not target.exists()
    assert not target.with_suffix(".md.tmp").exists()


def test_ensure_slot_pm_state_predicate_accepts_work_session_and_excludes_non_slot_modes(wp):
    _seed(
        wp,
        _lease(wp, slot="work/A_1", session="host-1234"),
        _lease(wp, slot="work/A_2", session="task-alpha"),
        _lease(wp, slot="work/A_3", session="", role="readonly"),
    )
    _seed_tasks(wp, "task-alpha")

    assert wp.ensure_slot_pm_state("work/A_1").exists()
    assert not wp.ensure_slot_pm_state("work/A_2").exists()
    assert not wp.ensure_slot_pm_state("work/A_3").exists()
    assert list(inspect.signature(wp.ensure_slot_pm_state).parameters) == ["slot"]


def test_bind_slot_creates_state_and_preserves_lease_bound_contract(wp):
    lease = wp.bind_slot("work/A_1", "A", "A_1", git_runner=_snapshot_git)

    state = wp.slot_pm_state_file(lease.slot)
    assert state.exists()
    assert lease.slot == "work/A_1"
    assert lease.repo == "A"
    assert lease.session == "A_1"
    assert lease.state == "leased"
    assert lease.pid == os.getpid()
    assert lease.bound is True
    recorded = wp.read_lease_strict("work/A_1")
    assert recorded is not None
    assert recorded.state == "leased"
    assert recorded.session == "A_1" and recorded.bound is True


def test_bind_slot_template_failure_leaves_ledger_and_state_unchanged(wp):
    existing = _lease(wp, session="", state="idle")
    existing.pid = 0
    _seed(wp, existing)
    ledger_before = wp.LEASES_FILE.read_bytes()
    (wp.REPO / ".project_manager" / "wiki" / "pm_state.template.md").unlink()
    target = wp.slot_pm_state_file("work/A_1")

    with pytest.raises(FileNotFoundError):
        wp.bind_slot("work/A_1", "A", "A_1", git_runner=_snapshot_git)

    assert wp.LEASES_FILE.read_bytes() == ledger_before
    recorded = wp.read_lease_strict("work/A_1")
    assert recorded is not None
    assert recorded.state == "idle" and recorded.session == ""
    assert recorded.bound is False
    assert not target.exists()
    assert not target.with_suffix(".md.tmp").exists()


def test_bind_slot_rebind_keeps_existing_state_bytes_and_lease_markers(wp):
    existing = _lease(wp, session="old", state="idle")
    existing.pid = 0
    existing.test_cmd = "pytest -q"
    _seed(wp, existing)
    target = wp.slot_pm_state_file("work/A_1")
    target.parent.mkdir(parents=True)
    original = b"handwritten continuity\n"
    target.write_bytes(original)

    rebound = wp.bind_slot("work/A_1", "A", "A_1", git_runner=lambda _argv: (1, ""))

    assert target.read_bytes() == original
    assert rebound.session == "A_1"
    assert rebound.state == "leased"
    assert rebound.pid == os.getpid()
    assert rebound.started != "before"
    assert rebound.bound is True
    assert rebound.test_cmd == "pytest -q"


def test_alloc_slot_mode_creates_state(wp):
    _seed(wp, _lease(wp, session="", state="idle"))
    lease = wp.alloc("A", session="A_1", owner_task=None, git_runner=_snapshot_git)

    state = wp.slot_pm_state_file(lease.slot)
    assert state.exists()
    assert lease.state == "leased" and lease.session == "A_1"
    assert lease.bound is False
    recorded = wp.read_lease_strict(lease.slot)
    assert recorded is not None
    assert recorded.state == "leased" and recorded.session == "A_1"
    assert recorded.bound is False


def test_alloc_template_failure_leaves_idle_ledger_and_state_unchanged(wp):
    existing = _lease(wp, session="", state="idle")
    existing.pid = 0
    _seed(wp, existing)
    ledger_before = wp.LEASES_FILE.read_bytes()
    (wp.REPO / ".project_manager" / "wiki" / "pm_state.template.md").unlink()
    target = wp.slot_pm_state_file("work/A_1")

    with pytest.raises(FileNotFoundError):
        wp.alloc("A", session="A_1", owner_task=None, git_runner=_snapshot_git)

    assert wp.LEASES_FILE.read_bytes() == ledger_before
    recorded = wp.read_lease_strict("work/A_1")
    assert recorded is not None
    assert recorded.state == "idle" and recorded.session == ""
    assert recorded.bound is False
    assert not target.exists()
    assert not target.with_suffix(".md.tmp").exists()


def test_alloc_slot_mode_releases_and_reuses_without_overwriting_state(wp):
    _seed(wp, _lease(wp, session="", state="idle"))
    target = wp.slot_pm_state_file("work/A_1")
    target.parent.mkdir(parents=True)
    original = b"continued slot history\n"
    target.write_bytes(original)

    lease = wp.alloc("A", owner_task=None, git_runner=_snapshot_git)

    assert lease.state == "leased" and lease.session
    assert lease.bound is False
    assert target.read_bytes() == original


def test_alloc_task_owner_does_not_create_false_slot_state(wp):
    _seed(wp, _lease(wp, session="", state="idle"))
    _seed_tasks(wp, "task-alpha")
    lease = wp.alloc("A", owner_task="task-alpha", git_runner=_snapshot_git)

    assert lease.session == "task-alpha"
    assert not wp.slot_pm_state_file(lease.slot).exists()


def test_create_task_owned_slot_does_not_create_false_slot_state(wp):
    wp.bare_repo_path("A").mkdir(parents=True)
    lease = wp.create_slot(
        "A", owner_task="task-alpha", init_submodules=False, git_runner=_create_git
    )

    assert lease.session == "task-alpha"
    assert not wp.slot_pm_state_file(lease.slot).exists()


def test_create_readonly_slot_does_not_create_slot_state(wp):
    wp.bare_repo_path("A").mkdir(parents=True)
    lease = wp.create_slot(
        "A", readonly=True, init_submodules=False, git_runner=_create_git
    )

    assert lease.role == "readonly"
    assert lease.session == "" and lease.pid == 0
    assert not wp.slot_pm_state_file(lease.slot).exists()


def test_slot_state_path_matches_pm_handoff_read_write_resolution(wp, project):
    handoff = _load("pm_handoff")
    handoff.REPO = project

    expected = handoff._slots_root() / "A_1" / "pm_state.md"
    assert wp.slot_pm_state_file("work/A_1") == expected
    assert wp.slot_pm_state_file("A_1") == expected
    assert handoff._pm_state_path("work/A_1", migrate=False) == expected


def test_ensure_slot_pm_state_backfills_bare_slot_before_template(wp):
    _seed(wp, _lease(wp, session="host-1234"))
    bare = wp.LOCAL_DIR / "slots" / "1" / "pm_state.md"
    bare.parent.mkdir(parents=True)
    original = b"bare-slot continuity\n"
    bare.write_bytes(original)

    target = wp.ensure_slot_pm_state("work/A_1")

    assert target == wp.slot_pm_state_file("work/A_1")
    assert target.read_bytes() == original
    assert not bare.exists()
    assert not bare.parent.exists()


def test_ensure_slot_pm_state_migrates_legacy_before_template(wp):
    _seed(wp, _lease(wp, session="host-1234"))
    legacy = wp.REPO / ".project_manager" / "wiki" / "pm_state.md"
    original = b"legacy continuity\n"
    legacy.write_bytes(original)

    target = wp.ensure_slot_pm_state("work/A_1")

    assert target == wp.slot_pm_state_file("work/A_1")
    assert target.read_bytes() == original
    assert not legacy.exists()


def test_locked_ensure_call_sites_are_public_ensure_bind_and_alloc_only():
    tree = ast.parse((TOOLS / "worktree_pool.py").read_text(encoding="utf-8"))
    calls: dict[str, list[ast.Call]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls[node.name] = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_ensure_slot_pm_state_locked"
        ]

    assert len(calls["ensure_slot_pm_state"]) == 1
    assert len(calls["bind_slot"]) == 1
    assert len(calls["alloc"]) == 1
    assert calls["create_slot"] == []
    assert {
        name for name, found in calls.items() if found
    } == {"ensure_slot_pm_state", "bind_slot", "alloc"}
    for call in calls["ensure_slot_pm_state"] + calls["bind_slot"] + calls["alloc"]:
        assert len(call.args) == 3
        assert call.keywords == []


def test_first_bootstrap_run_binds_before_context_and_renders_seeded_state(
    wp, project, capsys
):
    bootstrap = _load("pm_bootstrap")
    bootstrap.REPO = project
    wp.current_branch = lambda _slot, git_runner=None: None
    wp.slot_status = lambda _slot, git_runner=None: None
    state = wp.slot_pm_state_file("work/A_1")
    instance = bootstrap.PmBootstrap(
        worktree_pool=wp,
        board=SimpleNamespace(_repo_protected=lambda _repo: []),
        pm_state_file=state,
    )
    instance._phase0_preflight = lambda _repo, _slot: 0
    instance._collect_freshness = lambda: []
    instance._collect_board = lambda: {
        "counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0},
        "counts_scope": "slot 1",
        "open_tickets": [],
        "lint": "clean",
    }
    instance._collect_git = lambda: {
        "branch": None,
        "commits": [],
        "working_tree": "clean",
    }
    instance._collect_board_git = lambda: None
    instance._read_log_text = lambda: None
    instance._collect_log_entry = lambda: None
    instance._collect_user_continuity = lambda _log_text: None
    instance._collect_dashboard_others = lambda: None
    instance._slot_era_info = lambda _repo, _freshness: None
    instance._safe_command_card = lambda _identity: None

    observed: list[dict] = []
    collect_handoff_context = instance._collect_handoff_context

    def collect_after_bind(log_text=None):
        assert state.exists(), "run()이 handoff context보다 먼저 slot state를 만들지 않았다"
        context = collect_handoff_context(log_text)
        observed.append(context)
        return context

    instance._collect_handoff_context = collect_after_bind

    assert instance.run(repo="A", slot=1) == 0

    assert len(observed) == 1
    context = observed[0]
    assert context is not None
    assert context["session_num"] == 1
    assert context["state_session_num"] == 1
    assert context["session_stale"] is False
    assert context["fresh_slot"] is False
    assert context["remaining_work"].startswith("## 남은 작업 전체 그림")
    markdown = capsys.readouterr().out
    assert "## PM 1차 부트스트랩" in markdown
    assert "## 남은 작업 전체 그림" in markdown
    assert "🆕 첫 바인딩 슬롯" not in markdown
    assert "log 기준" not in markdown
