"""부트스트랩 첫 실행 순서 — 인계 컨텍스트 수집 전에 task 서술 상태를 만든다.

슬롯별 pm_state 축(`.local/slots/<slot>/pm_state.md`)은 삭제됐고, 여기 남은 것은 그
축과 무관한 task 상태·부트스트랩 순서 계약이다.
"""
from __future__ import annotations

from _pool_seams import bind_pool

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from pm_engine.infra import pm_paths


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


def test_first_bootstrap_run_binds_before_context_and_renders_seeded_state(
    wp, monkeypatch, project, capsys
):
    """run() 이 인계 컨텍스트 수집 **전에** task 서술 상태를 만들고 그 내용을 dump 한다."""
    bootstrap = _load("pm_bootstrap")
    bootstrap.REPO = project
    # 가드 훅이 남기는 세션 기록 — 부트스트랩은 이 기록으로만 자기 세션 ID 를 안다.
    claims = project / ".project_manager" / ".local" / "task-session-claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / "mytask.json").write_text(
        json.dumps({"task": "mytask", "session": "S-1"}), encoding="utf-8"
    )
    state = pm_paths.task_pm_state_file(pm_paths.home_at(wp.REPO), "mytask")
    bind_pool(monkeypatch, wp)
    instance = bootstrap.PmBootstrap(

        board=SimpleNamespace(_repo_protected=lambda _repo: []),
    )
    instance._phase0_preflight = lambda: 0
    instance._collect_freshness = lambda: []
    instance._collect_board = lambda: {
        "counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0},
        "counts_scope": "task mytask",
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
    instance._safe_command_card = lambda: None

    observed: list[dict] = []
    collect_handoff_context = instance._collect_handoff_context

    def collect_after_bind(log_text=None):
        assert state.exists(), "run()이 handoff context보다 먼저 task state를 만들지 않았다"
        context = collect_handoff_context(log_text)
        observed.append(context)
        return context

    instance._collect_handoff_context = collect_after_bind

    assert instance.run(task="mytask") == 0

    assert len(observed) == 1
    context = observed[0]
    assert context is not None
    assert context["session_num"] == 1
    assert context["state_session_num"] == 1
    assert context["session_stale"] is False
    assert context["todos"] == []   # 새 task — 열린 todo 0건(등록 경로를 안내한다)
    markdown = capsys.readouterr().out
    assert "## PM 1차 부트스트랩" in markdown
    assert "### 열린 todo" in markdown
    assert "🆕 첫 바인딩 슬롯" not in markdown
    assert "log 기준" not in markdown
