"""pytest 임시 루트가 프로젝트 안에 선언되는지 기계로 고정한다 (T-0888).

위치가 판정을 바꾸지 않는다는 이 저장소의 계약은 회귀 자신에게도 적용된다 — 임시물을
프로젝트 밖에 두면 "위에 아무것도 없어서" 통과하는 것이지 판정이 옳아서 통과하는 것이
아니다. 그래서 임시 루트는 프로젝트 안 per-clone 스크래치 하나이고, 예외 목록을 두지 않는다.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
from pathlib import Path

from _repo_owned_inventory import OWNED, repo_owned_paths

ROOT = Path(__file__).resolve().parents[1]
ROOT_CONFTEST = ROOT / "conftest.py"
EXPECTED_TEMP_ROOT = ROOT / ".project_manager" / ".local" / "tmp"
TOOLS = ROOT / ".project_manager" / "tools"

# 실제로 디렉터리/파일을 **만드는** tempfile 표면. `gettempdir` 은 경로 문자열만 돌려주므로
# 이 검사의 대상이 아니다 — 자리 규약은 "무엇이 어디에 만들어지나" 의 규약이다.
_TEMP_CREATORS = frozenset(
    {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile"}
)

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


def _temp_creator_calls_without_dir(source: str) -> list[int]:
    """`dir=` 없이 임시물을 만드는 호출의 줄번호 — 자리를 OS 임시 폴더에 맡기는 형태다."""
    offenders: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name not in _TEMP_CREATORS:
            continue
        if not any(keyword.arg == "dir" for keyword in node.keywords):
            offenders.append(node.lineno)
    return offenders


def test_engine_copies_create_temp_only_under_an_explicit_dir():
    """엔진 사본 전수에서 임시물 생성 호출은 전부 부모를 명시한다(자리 규약·예외 목록 0).

    `dir=` 를 빼면 그 자리는 OS 임시 폴더로 가고, 재는 대상 트리 밖이라 무엇이 언제 쌓였는지
    보이지 않는다. 자리 하나를 돌려주는 소유자는 `pm_relay.temp_root` 이고, 원자적 쓰기는
    대상 파일의 부모를 준다 — 어느 쪽이든 **호출 형태가 선언**이라 예외 목록이 필요 없다.
    """
    offenders: list[str] = []
    for copy_root in _ENGINE_COPY_ROOTS:
        tools = ROOT / copy_root / ".project_manager" / "tools"
        assert tools.is_dir(), f"엔진 사본 없음: {tools}"
        for path in sorted(tools.glob("*.py")):
            for lineno in _temp_creator_calls_without_dir(
                path.read_text(encoding="utf-8")
            ):
                offenders.append(f"{copy_root}/{path.name}:{lineno}")
    assert not offenders, "부모를 명시하지 않은 임시물 생성 호출: " + ", ".join(offenders)


def test_tests_create_temp_only_under_an_explicit_dir():
    """회귀 자신도 같은 규약을 받는다 — `tests/` 의 임시물 생성 호출은 전부 부모를 명시한다.

    수집 시점(`skipif` 인자 평가)은 픽스처가 없어 `dir=` 로 자리를 주고, 실행 시점은 `tmp_path`
    가 준다(그건 tempfile 생성 호출이 아니라 이 검사의 대상이 아니다). `dir=` 없는 호출은 자리를
    OS 임시 폴더에 맡기고, 그 자리는 이 저장소가 재는 대상 밖이라 무엇이 언제 쌓였는지 보이지
    않는다 — 회귀가 만든 것이 가장 많이 쌓이는 쪽이다.
    """
    # 열거는 repo-owned seam 으로 한다 — 재귀 tree-walk 는 이 저장소의 열거 규약 밖이다.
    scanned = sorted(
        path for path in repo_owned_paths(ROOT, "tests", mode=OWNED)
        if path.is_file() and path.suffix == ".py"
    )
    # 스캔 입력이 비면 이 단언은 아무것도 재지 않는다 — 대상 수를 먼저 세운다.
    assert len(scanned) > 1, f"스캔 입력이 비었다: {ROOT / 'tests'}"
    offenders: list[str] = []
    for path in scanned:
        for lineno in _temp_creator_calls_without_dir(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    assert not offenders, "부모를 명시하지 않은 임시물 생성 호출: " + ", ".join(offenders)


def test_engine_temp_root_is_the_project_local_tmp(tmp_path):
    """`pm_relay.temp_root` 이 그 clone 의 `.project_manager/.local/tmp` 를 만들어 돌려준다."""
    spec = importlib.util.spec_from_file_location(
        "pm_relay_temp_root_contract", TOOLS / "pm_relay.py"
    )
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)

    root = relay.temp_root(tmp_path)

    assert root == tmp_path / ".project_manager" / ".local" / "tmp"
    assert root.is_dir()
    # 두 번 불러도 같은 자리다 — 자리 규약이지 매번 새로 잡는 예약이 아니다.
    assert relay.temp_root(tmp_path) == root
