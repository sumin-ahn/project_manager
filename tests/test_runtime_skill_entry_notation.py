"""런타임 사용자 안내의 PM 스킬 표기가 현재 하네스를 따르는지 검증한다."""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _exposes_runtime_skill_entry(tree: ast.AST) -> bool:
    """로컬 정의든 공용 helper alias든 런타임 표기 seam을 노출하는 모듈인지 판정한다."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_runtime_skill_entry":
                return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name)
                and target.id == "_runtime_skill_entry"
                for target in targets
            ):
                return True
    return False


def _runtime_modules() -> tuple[str, ...]:
    return tuple(sorted(
        path.stem
        for path in TOOLS.glob("*.py")
        if _exposes_runtime_skill_entry(
            ast.parse(path.read_text(encoding="utf-8"))
        )
    ))


# 손-열거하지 않는다. 새 출력 모듈이 runtime seam을 정의/alias하면 이 파라미터 축에 자동 편입된다.
RUNTIME_MODULES = _runtime_modules()
_SLASH_SKILL_ENTRY = re.compile(
    r"(?<![A-Za-z0-9_.>/\-])/pm-[a-z0-9-]+(?![A-Za-z0-9_.>/\-])"
)


def _load(name: str):
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"runtime_entry_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("marker", ("CODEX_THREAD_ID", "CODEX_CI"))
@pytest.mark.parametrize("module_name", RUNTIME_MODULES)
def test_runtime_skill_entry_uses_codex_notation(module_name, marker, monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_CI", raising=False)
    monkeypatch.setenv(marker, "probe")
    module = _load(module_name)
    assert module._runtime_skill_entry("pm-bootstrap") == "$pm-bootstrap"


@pytest.mark.parametrize("module_name", RUNTIME_MODULES)
def test_runtime_skill_entry_defaults_to_slash(module_name, monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_CI", raising=False)
    module = _load(module_name)
    assert module._runtime_skill_entry("pm-bootstrap") == "/pm-bootstrap"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _allowed_canonical_literal(node: ast.Constant, tree: ast.AST) -> bool:
    """출력 전 runtime 변환이 보장된 authoring 리터럴과 파서 oracle만 허용한다.

    ``skill(<첫 인자>)``는 ``_runtime_skill_entry``를 반드시 거치는 카드 authoring seam이다.
    ``_BARE_BOOTSTRAP_TRIGGER``는 사용자에게 출력하지 않고 양 접두사를 읽는 파서의 canonical
    fixture다. 일반 상수·help·print·return 문자열에는 이 예외가 적용되지 않는다.
    """
    for candidate in ast.walk(tree):
        if (
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Name)
            and candidate.func.id == "skill"
            and candidate.args
            and any(descendant is node for descendant in ast.walk(candidate.args[0]))
        ):
            return True
        if isinstance(candidate, (ast.Assign, ast.AnnAssign)):
            targets = (
                candidate.targets
                if isinstance(candidate, ast.Assign)
                else [candidate.target]
            )
            if not any(
                isinstance(target, ast.Name)
                and target.id == "_BARE_BOOTSTRAP_TRIGGER"
                for target in targets
            ):
                continue
            if any(descendant is node for descendant in ast.walk(candidate.value)):
                return True
    return False


def test_all_tool_modules_have_no_hardcoded_slash_skill_output_string():
    failures = {}
    # runtime seam 보유 목록과 별개로 tools 전체를 스캔한다. 따라서 helper를 추가하지 않은 새
    # 출력 모듈도 slash 리터럴을 넣는 순간 red가 된다(목록 누락으로 숨을 수 없음).
    for path in sorted(TOOLS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        rows = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _SLASH_SKILL_ENTRY.search(node.value)
            and id(node) not in docstrings
            and not _allowed_canonical_literal(node, tree)
        ]
        if rows:
            failures[path.name] = rows
    assert not failures, f"runtime 출력 후보에 slash 스킬 문자열 하드코딩: {failures}"


def test_bootstrap_is_in_mechanically_derived_runtime_modules():
    assert "pm_bootstrap" in RUNTIME_MODULES


def test_bootstrap_fresh_surfaces_use_codex_notation(monkeypatch):
    monkeypatch.setenv("CODEX_CI", "1")
    bootstrap = _load("pm_bootstrap")
    inst = bootstrap.PmBootstrap(run_git_fn=lambda _args: (0, ""))
    context = {
        "session_num": 1,
        "session_stale": False,
        "state_session_num": 1,
        "remaining_work": None,
        "state_path": "pm_state.md",
        "fresh_slot": True,
    }
    markdown = inst._build_markdown(
        {
            "counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0},
            "open_tickets": [],
            "lint": "clean",
        },
        None,
        {"branch": "main", "commits": [], "working_tree": "clean"},
        None,
        "ts",
        context,
        None,
    )
    assert "첫 $pm-handoff 가 pm_state 를 생성" in markdown
    assert "pm_state 없음 · 첫 $pm-handoff 가 생성" in markdown
    assert "첫 /pm-handoff" not in markdown


def test_bootstrap_task_pytest_stop_uses_codex_notation(monkeypatch, capsys):
    monkeypatch.setenv("CODEX_CI", "1")
    bootstrap = _load("pm_bootstrap")
    inst = bootstrap.PmBootstrap(run_git_fn=lambda _args: (0, ""))
    inst._task_name = "probe"
    inst._task_workspace_slots = ()
    with pytest.raises(SystemExit):
        inst._collect_pytest_for_scope()
    error = capsys.readouterr().err
    assert "`$pm-env alloc <repo> --task <이름>`" in error
    assert "`/pm-env" not in error


def test_bootstrap_readonly_stop_uses_codex_notation(monkeypatch, capsys):
    monkeypatch.setenv("CODEX_CI", "1")
    bootstrap = _load("pm_bootstrap")
    inst = bootstrap.PmBootstrap(run_git_fn=lambda _args: (0, ""))
    lease = object()
    monkeypatch.setattr(inst, "_reject_worktree_copy_anchor", lambda: False)
    monkeypatch.setattr(inst, "_reconcile_protected_sidecar", lambda _repo: None)
    monkeypatch.setattr(inst, "_resolve_worktree_pool", lambda: object())
    monkeypatch.setattr(inst, "_phase0_find_lease", lambda _wp, _slot: lease)
    monkeypatch.setattr(inst, "_phase0_incomplete_create", lambda _lease: False)
    monkeypatch.setattr(inst, "_phase0_is_readonly", lambda _lease: True)
    assert inst._phase0_preflight("repo", 2) == 1
    error = capsys.readouterr().err
    assert "`$pm-worktree refresh repo_2`" in error
    assert "`/pm-worktree" not in error


def test_relay_child_bootstrap_prompt_uses_runtime_notation(monkeypatch):
    monkeypatch.setenv("CODEX_CI", "1")
    relay = _load("pm_relay")
    prompt = relay.build_bootstrap_prompt("alpha")
    assert "`$pm-bootstrap --task alpha`" in prompt
    assert "`/pm-bootstrap" not in prompt
