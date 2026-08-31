"""pytest 임시 루트가 프로젝트 안에 선언되는지 기계로 고정한다 (T-0888).

위치가 판정을 바꾸지 않는다는 이 저장소의 계약은 회귀 자신에게도 적용된다 — 임시물을
프로젝트 밖에 두면 "위에 아무것도 없어서" 통과하는 것이지 판정이 옳아서 통과하는 것이
아니다. 그래서 임시 루트는 프로젝트 안 per-clone 스크래치 하나이고, 예외 목록을 두지 않는다.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_CONFTEST = ROOT / "conftest.py"
EXPECTED_TEMP_ROOT = ROOT / ".project_manager" / ".local" / "tmp"

# 엔진 사본 전수 — 한 벌이라도 프로젝트 밖 임시 경로를 값으로 들고 있으면 채택자에게 그대로 나간다.
_ENGINE_COPY_ROOTS = (
    Path("."),
    Path("templates/claude_code"),
    Path("templates/codex"),
    Path("templates/opencode"),
)

# 프로젝트 밖 임시 경로 리터럴 — POSIX 관례 자리와 Windows 시스템 Temp.
_OUT_OF_PROJECT_TEMP = re.compile(
    r"(^|[\s\"'=:(,])(/tmp|/var/tmp|/var/folders|/private/var/folders)(/|$|[\s\"'),])"
    r"|[A-Za-z]:\\+(Temp|Windows\\+Temp)",
    re.IGNORECASE,
)


def _docstring_constants(tree: ast.AST) -> set[int]:
    """docstring 노드의 id 집합 — 산문은 코드가 값으로 쓰는 문자열이 아니다."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def test_pytest_temp_root_is_declared_inside_the_project(tmp_path):
    """루트 conftest 가 임시 루트를 프로젝트 안으로 선언하고, 실제 임시물도 그 아래 생긴다."""
    assert ROOT_CONFTEST.is_file(), f"루트 conftest 없음: {ROOT_CONFTEST}"
    declared = Path(os.environ["PYTEST_DEBUG_TEMPROOT"]).resolve()
    assert declared == EXPECTED_TEMP_ROOT.resolve()

    # 선언이 아니라 실물 — 이번 실행의 임시물이 실제로 저장소 안에 있다.
    assert EXPECTED_TEMP_ROOT.resolve() in tmp_path.resolve().parents


def test_root_conftest_declares_temp_root_without_resetting_basetemp():
    """선언은 모듈 로드 시점이고, `pytest_configure` 는 basetemp 를 재설정하지 않는다.

    xdist 워커도 루트 conftest 를 각자 로드한다. `pytest_configure` 에서
    `config.option.basetemp` 를 다시 주면 `TempPathFactory.getbasetemp` 가 워커마다 공유
    루트를 `rm_rf` 해 서로의 폴더를 지운다(실측 `FileNotFoundError` 1517건).
    """
    source = ROOT_CONFTEST.read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert "PYTEST_TEMP_ROOT" in module_level, "임시 루트 선언이 모듈 로드 시점이 아니다"
    assert "PYTEST_DEBUG_TEMPROOT" in source
    hooks = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("pytest_")
    ]
    assert not hooks, (
        "루트 conftest 는 선언만 한다 — 훅은 워커에서도 돌아 basetemp 를 재설정하면 "
        f"워커들이 공유 임시 루트를 서로 지운다: {hooks}"
    )


def test_engine_copies_have_no_out_of_project_temp_path_literals():
    """canonical + 템플릿 3벌의 엔진 코드가 값으로 쓰는 프로젝트 밖 임시 경로는 0건이다."""
    offenders: list[str] = []
    for copy_root in _ENGINE_COPY_ROOTS:
        tools = ROOT / copy_root / ".project_manager" / "tools"
        assert tools.is_dir(), f"엔진 사본 없음: {tools}"
        for path in sorted(tools.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            docstrings = _docstring_constants(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue
                if not isinstance(node.value, str) or id(node) in docstrings:
                    continue
                if _OUT_OF_PROJECT_TEMP.search(node.value):
                    offenders.append(
                        f"{copy_root}/{path.name}:{node.lineno}: {node.value[:60]!r}"
                    )
    assert not offenders, "프로젝트 밖 임시 경로 리터럴: " + ", ".join(offenders)
