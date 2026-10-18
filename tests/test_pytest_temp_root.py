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
from _engine_tree import engine_python_files
from pm_engine.infra import raw_output

ROOT = Path(__file__).resolve().parents[1]
ROOT_CONFTEST = ROOT / "conftest.py"
EXPECTED_TEMP_ROOT = ROOT / ".project_manager" / ".local" / "tmp"
TOOLS = ROOT / ".project_manager" / "tools"

# 실제로 디렉터리/파일을 **만드는** tempfile 표면 — 이 축은 `dir=` 로 부모를 받았는지만 본다.
_TEMP_CREATORS = frozenset(
    {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile"}
)

# OS 임시 폴더를 **자리로 해소**하는 호출. 생성 호출이 아니라고 이 축을 빼면(T-0890 F-008 의
# 결정) 자리를 `gettempdir` 로 잡고 `Path.mkdir`/`os.mkdir` 로 여는 형태가 감시 밖에 남는다 —
# 자리 규약을 세운 커밋이 자기 안에 판 예외(read 역할 격리 자리)가 그렇게 통과했다.
_SYSTEM_TEMP_RESOLVERS = frozenset({"gettempdir", "_gettempdir"})
# 남기기로 한 **하나뿐인** 자리 — 자식 하네스에게 넘길 env 값이다(엔진이 만드는 임시물이 아니고
# 그 자식 임시물의 회수 책임도 엔진에 없다). 파일:줄 예외 목록이 아니라 그 값을 쓰는 함수
# 이름으로 선언한다(래퍼 자신 + 유일한 소비자).
_SYSTEM_TEMP_ALLOWED_FUNCTIONS = frozenset({"_gettempdir", "_with_resolved_child_env"})

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
        for path in engine_python_files(tools):
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
    보이지 않는다. 자리 하나를 돌려주는 소유자는 `raw_output.temp_root` 이고, 원자적 쓰기는
    대상 파일의 부모를 준다 — 어느 쪽이든 **호출 형태가 선언**이라 예외 목록이 필요 없다.
    """
    offenders: list[str] = []
    for copy_root in _ENGINE_COPY_ROOTS:
        tools = ROOT / copy_root / ".project_manager" / "tools"
        assert tools.is_dir(), f"엔진 사본 없음: {tools}"
        for path in engine_python_files(tools):
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


def _out_of_seam_system_temp_calls(source: str) -> list[str]:
    """선언된 자리 밖에서 OS 임시 폴더를 해소하는 호출 — `함수:줄` 목록."""
    tree = ast.parse(source)
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner[id(child)] = node.name    # 가장 안쪽 함수가 마지막에 덮는다
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name not in _SYSTEM_TEMP_RESOLVERS:
            continue
        enclosing = owner.get(id(node), "<module>")
        if enclosing in _SYSTEM_TEMP_ALLOWED_FUNCTIONS:
            continue
        offenders.append(f"{enclosing}:{node.lineno}")
    return offenders


def test_engine_copies_resolve_the_os_temp_dir_only_in_the_declared_seam():
    """엔진 사본 전수에서 OS 임시 폴더 해소는 선언된 자리 하나뿐이다(자식 env 의 임시 축).

    `dir=` 축은 `tempfile.*` 생성 호출만 본다. 자리를 `gettempdir` 로 잡고 `mkdir` 로 여는
    형태는 그 축에 걸리지 않으므로 **해소 자체**를 여기서 센다.
    """
    offenders: list[str] = []
    for copy_root in _ENGINE_COPY_ROOTS:
        tools = ROOT / copy_root / ".project_manager" / "tools"
        assert tools.is_dir(), f"엔진 사본 없음: {tools}"
        for path in engine_python_files(tools):
            for site in _out_of_seam_system_temp_calls(
                path.read_text(encoding="utf-8")
            ):
                offenders.append(f"{copy_root}/{path.name}:{site}")
    assert not offenders, "선언된 자리 밖에서 OS 임시 폴더를 해소함: " + ", ".join(offenders)

    # 스캔이 공허하지 않다는 증거 — 선언된 그 자리는 실재한다.
    canonical = (ROOT / ".project_manager" / "tools" / "pm_delegate.py").read_text(
        encoding="utf-8")
    assert "tempfile.gettempdir()" in canonical
    # 민감도 양방향 — 같은 호출이 자리 밖이면 잡고, 선언된 자리 안이면 통과한다.
    assert _out_of_seam_system_temp_calls(
        "import tempfile\n"
        "def _read_tmp_parent():\n"
        "    return tempfile.gettempdir()\n"
    ) == ["_read_tmp_parent:3"]
    assert _out_of_seam_system_temp_calls(
        "import tempfile\n"
        "def _with_resolved_child_env():\n"
        "    return tempfile.gettempdir()\n"
    ) == []


def test_read_role_temp_place_passes_the_project_anchor(monkeypatch, tmp_path):
    """`mkdir` 로 여는 유일한 임시 자리(read 역할 격리)도 앵커 seam 을 지나야 통과한다.

    이 자리는 `tempfile.*` 을 타지 않아 `dir=` 축이 원리적으로 볼 수 없다 — 그래서 부모가
    `raw_output.temp_root` 를 지나는지를 값으로 잰다(세 하네스 전부). 앵커는 **주입한 PM 홈**
    이고 OS 임시 폴더는 그 홈 밖으로 돌린다. 기본 실행의 `tempfile.gettempdir()` 는 회귀 자신이
    저장소 안으로 돌려놓은 값이라(`conftest._forbid_real_tempdir_project_outputs`), 실제 저장소를
    앵커로 쓰면 옛 형태(`Path(_gettempdir())`)로 되돌려도 단언이 참으로 남아 아무것도 지키지
    못한다. 주입은 실제 저장소 트리에 쓰지 않는다는 요건도 함께 만족한다.
    """
    delegate = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location(
            "pm_delegate_temp_place_contract", TOOLS / "pm_delegate.py")
    )
    delegate.__spec__.loader.exec_module(delegate)

    pm_home = tmp_path / "pmhome"
    (pm_home / ".project_manager" / "tools").mkdir(parents=True)
    outside_the_pm_home = tmp_path / "os-temp"
    outside_the_pm_home.mkdir()
    monkeypatch.setattr(delegate, "REPO", pm_home)
    monkeypatch.setattr(delegate, "_CONFIG_REPO_OVERRIDE", None)
    monkeypatch.setattr(delegate, "_gettempdir", lambda: str(outside_the_pm_home))
    anchor = raw_output.temp_root(pm_home).resolve()

    for harness in ("codex", "claude", "opencode"):
        temp_parent, _component = delegate._read_tmp_parent(harness)
        assert anchor == temp_parent or anchor in temp_parent.parents, (
            f"{harness} 격리 자리가 앵커 밖이다: {temp_parent}")

    # 민감도 — 옛 형태(`Path(_gettempdir())`)가 내는 값은 이 앵커 밖이다. 그 형태로 되돌리면
    # 위 단언이 red 라는 뜻이고, 그래서 위 통과가 공허하지 않다.
    reverted = Path(delegate._gettempdir()).resolve()
    assert anchor != reverted and anchor not in reverted.parents


def test_engine_temp_root_is_the_project_local_tmp(tmp_path):
    """`raw_output.temp_root` 이 그 clone 의 `.project_manager/.local/tmp` 를 만들어 돌려준다."""

    root = raw_output.temp_root(tmp_path)

    assert root == tmp_path / ".project_manager" / ".local" / "tmp"
    assert root.is_dir()
    # 두 번 불러도 같은 자리다 — 자리 규약이지 매번 새로 잡는 예약이 아니다.
    assert raw_output.temp_root(tmp_path) == root
