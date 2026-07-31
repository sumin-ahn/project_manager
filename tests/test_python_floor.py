"""T-0482 — Python 3.11 런타임 하한 선언·탐지·파사드 미러 가드."""
from __future__ import annotations

import ast
import io
import importlib.util
import os
import re
import shutil
import subprocess
import tokenize
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"t0482_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_python(path: Path, *, version: str, supported: bool, selected: str) -> None:
    """--version / 구 -c / 동형 script probe / 실제 엔진 실행을 구분하는 가짜 인터프리터."""
    guard_rc = "0" if supported else "1"
    path.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        f"  --version) echo 'Python {version}'; exit 0 ;;\n"
        f"  -c) exit {guard_rc} ;;\n"
        f"  */python_floor.py) echo 'Python {version}'; exit {guard_rc} ;;\n"
        "esac\n"
        f"echo 'selected={selected}'\n"
        "if [ -n \"$SELECT_LOG\" ]; then echo "
        f"'{selected}' >> \"$SELECT_LOG\"; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_min_python_single_source_and_tomllib_rationale():
    engine_rev = _load("engine_rev")
    floor_tree = ast.parse((TOOLS / "python_floor.py").read_text(encoding="utf-8"))
    floor_value = next(
        ast.literal_eval(stmt.value)
        for stmt in floor_tree.body
        if isinstance(stmt, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MIN_PYTHON"
                for target in stmt.targets)
    )
    source = (TOOLS / "engine_rev.py").read_text(encoding="utf-8")
    assert engine_rev.MIN_PYTHON == (3, 11)
    assert floor_value == engine_rev.MIN_PYTHON
    assert "tomllib" in source
    assert "지배 제약" in source


def test_python_floor_remains_python27_parse_safe_ascii_bootstrap():
    """probe가 구 런타임에서 엔진보다 먼저 파싱된다는 정적 계약을 durable하게 고정한다."""
    path = TOOLS / "python_floor.py"
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()[:2]
    assert any(re.search(r"coding[:=]\s*utf-8", line) for line in lines)

    tree = ast.parse(source)
    assert any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "__future__"
        and any(alias.name == "print_function" for alias in stmt.names)
        for stmt in tree.body
    )
    assert not any(
        isinstance(node, (ast.JoinedStr, ast.NamedExpr, ast.AnnAssign))
        for node in ast.walk(tree)
    )
    assert not any(
        (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.returns is not None
        )
        or (isinstance(node, ast.arg) and node.annotation is not None)
        for node in ast.walk(tree)
    )

    docstring_starts = set()
    for owner in [tree, *(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )]:
        if (
            owner.body
            and isinstance(owner.body[0], ast.Expr)
            and isinstance(owner.body[0].value, ast.Constant)
            and isinstance(owner.body[0].value.value, str)
        ):
            docstring_starts.add(
                (owner.body[0].value.lineno, owner.body[0].value.col_offset)
            )
    non_ascii_code = [
        (token.start, token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type != tokenize.COMMENT
        and token.start not in docstring_starts
        and not token.string.isascii()
    ]
    assert not non_ascii_code, f"docstring·주석 밖 non-ASCII: {non_ascii_code}"


def test_board_interp_runs_rejects_old_but_executable_python(monkeypatch):
    board = _load("board")
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Result(0 if argv[1] == "--version" else 1)

    monkeypatch.setattr(board.subprocess, "run", fake_run)
    assert board._interp_runs("py") is False
    assert calls[0][1] == "--version"
    assert calls[1][1].endswith("python_floor.py")
    assert "-c" not in calls[1]  # py -c 기본 버전과 py <script> shebang 디스패치의 괴리 폐쇄.


def test_board_missing_floor_probe_degrades_to_inline_floor_check(
    tmp_path, monkeypatch, capsys,
):
    """python_floor.py가 빠진 부분 사본도 3.12는 채택하고 2.7은 거르며 원인을 진단한다."""
    isolated = tmp_path / "tools"
    shutil.copytree(TOOLS, isolated)
    (isolated / "python_floor.py").unlink()
    spec = importlib.util.spec_from_file_location(
        "t0482_board_without_python_floor", isolated / "board.py"
    )
    board = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(board)

    versions = {"python3": (2, 7), "python": (3, 12)}

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(argv, **kwargs):
        version = versions[argv[0]]
        if argv[1] == "--version":
            return Result(stdout=f"Python {version[0]}.{version[1]}\n")
        assert argv[1] == "-c"
        if "sys.exit" in argv[2]:
            return Result(returncode=0 if version >= (3, 11) else 1)
        return Result(stdout=f"Python {version[0]}.{version[1]}\n")

    monkeypatch.setattr(board.shutil, "which", lambda cmd: f"/fake/{cmd}")
    monkeypatch.setattr(board.subprocess, "run", fake_run)
    assert board._detect_py() == "python"

    versions["python"] = (2, 7)
    board._detect_py.cache_clear()
    assert board._detect_py() == "python3"  # 기존 fail-soft 리터럴 폴백.
    err = capsys.readouterr().err
    assert "하한 probe 부재" in err
    assert "python3=2.7" in err
    assert "python=2.7" in err


def test_board_detect_all_old_falls_back_and_reports_versions(monkeypatch, capsys):
    board = _load("board")
    monkeypatch.setattr(board.os, "name", "nt")
    monkeypatch.setattr(board.shutil, "which", lambda cmd: f"/fake/{cmd}")
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: False)
    versions = {"python": "python=3.9", "py": "py=2.7", "python3": "python3=3.10"}
    monkeypatch.setattr(board, "_interp_version_label", versions.get)

    assert board._detect_py() == "python3"
    err = capsys.readouterr().err
    assert "Python 3.11+ 필요" in err
    assert "py=2.7" in err
    assert "python=3.9" in err


def test_board_detect_never_raises_when_floor_source_is_broken(monkeypatch, capsys):
    board = _load("board")
    monkeypatch.setattr(board.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(
        board, "_minimum_python",
        lambda: (_ for _ in ()).throw(OSError("broken engine_rev")),
    )
    assert board._detect_py() == "python3"
    assert "하한 확인 불가" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 부재")
@pytest.mark.parametrize(
    "facade",
    [
        REPO / "pm-config.sh",
        REPO / "pm-import.sh",
        *(REPO / "templates").glob("*/pm-update.sh"),
    ],
)
def test_posix_facade_skips_old_executable_candidate(tmp_path, facade):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_python(bindir / "python3", version="3.9.18", supported=False, selected="python3-old")
    _fake_python(bindir / "python", version="3.11.9", supported=True, selected="python")
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}

    proc = subprocess.run(
        [shutil.which("bash"), str(facade), "--probe"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "selected=python" in proc.stdout
    assert "python3-old" not in proc.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 부재")
@pytest.mark.parametrize(
    "facade",
    [
        REPO / "pm-config.sh",
        REPO / "pm-import.sh",
        *(REPO / "templates").glob("*/pm-update.sh"),
    ],
)
def test_posix_facades_report_floor_before_fail_soft_fallback(tmp_path, facade):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_python(bindir / "python3", version="2.7.18", supported=False, selected="python3-old")
    _fake_python(bindir / "python", version="2.7.18", supported=False, selected="python-old")
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}

    proc = subprocess.run(
        [shutil.which("bash"), str(facade), "--probe"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    assert proc.returncode == 0  # 기존 fail-soft python 폴백 계약.
    assert "Python 3.11+ 필요" in proc.stderr
    assert "selected=python-old" in proc.stdout


def test_cmd_facades_probe_every_candidate_before_selection():
    facades = [
        REPO / "pm-config.cmd",
        REPO / "pm-import.cmd",
        *(REPO / "templates").glob("*/pm-config.cmd"),
        *(REPO / "templates").glob("*/pm-update.cmd"),
    ]
    assert len(facades) == 8
    for path in facades:
        text = path.read_text(encoding="utf-8")
        for candidate in ("python", "py", "python3"):
            assert f"{candidate} --version" in text
            assert (
                f'{candidate} "%~dp0.project_manager\\tools\\python_floor.py"' in text
            )
            assert f"{candidate} -c " not in text
        assert "if not defined PY set" in text  # 전 후보 실패 시 기존 fail-soft 폴백.


def test_shell_floor_mirrors_engine_constant():
    """Python 밖 하드코딩은 engine_rev.MIN_PYTHON 과 반드시 같은 리터럴이어야 한다."""
    engine_rev = _load("engine_rev")
    major, minor = engine_rev.MIN_PYTHON
    literal = f"sys.version_info >= ({major}, {minor})"
    diagnostic_literal = f"Python {major}.{minor}+ 필요"
    # 루트 pm-config.cmd/pm-import.cmd는 출하 template mirror 목록이 아니며 위 cmd 전용 가드가 직접 검사한다.
    mirrors = [
        REPO / "pm-config.sh",
        REPO / "pm-import.sh",
        *(REPO / "templates").glob("*/pm-config.sh"),
        *(REPO / "templates").glob("*/pm-update.sh"),
        *(REPO / "templates").glob("*/pm-config.cmd"),
        *(REPO / "templates").glob("*/pm-update.cmd"),
    ]
    assert mirrors
    missing = [
        str(path.relative_to(REPO))
        for path in mirrors
        if literal not in path.read_text(encoding="utf-8")
        and "python_floor.py" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"engine_rev.MIN_PYTHON 미러 누락/skew: {missing}"
    diagnostic_mirrors = [
        REPO / "pm-config.sh",
        *(REPO / "templates").glob("*/pm-config.sh"),
    ]
    assert all(
        diagnostic_literal in path.read_text(encoding="utf-8")
        for path in diagnostic_mirrors
    )
    posix_facades = [
        REPO / "pm-config.sh",
        REPO / "pm-import.sh",
        *(REPO / "templates").glob("*/pm-config.sh"),
        *(REPO / "templates").glob("*/pm-update.sh"),
    ]
    assert all(
        "shebang 간접 디스패치가 없으므로" in path.read_text(encoding="utf-8")
        for path in posix_facades
    )
    hook_source = (TOOLS / "worktree_pool.py").read_text(encoding="utf-8")
    assert '$_cand" "$python_floor"' in hook_source


def test_pm_import_precheck_is_310_parseable_and_precedes_tomllib():
    source = (TOOLS / "pm_import.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 10))
    require_index = next(
        i for i, stmt in enumerate(tree.body)
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id == "_require_python"
    )
    tomllib_index = next(
        i for i, stmt in enumerate(tree.body)
        if isinstance(stmt, ast.Import)
        and any(alias.name == "tomllib" for alias in stmt.names)
    )
    assert require_index < tomllib_index

    pm_import = _load("pm_import")
    with pytest.raises(SystemExit, match=r"Python 3\.11\+ 필요 · 현재 3\.10"):
        pm_import._require_python((3, 10, 99))


def test_pm_import_missing_floor_source_skips_precheck(tmp_path):
    isolated = tmp_path / "tools"
    isolated.mkdir()
    shutil.copy2(TOOLS / "pm_import.py", isolated / "pm_import.py")

    spec = importlib.util.spec_from_file_location(
        "t0482_pm_import_without_engine_rev", isolated / "pm_import.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._load_min_python() is None
    assert mod._require_python((2, 7, 18)) is None


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재")
def test_embedded_hook_skips_old_python_candidate(tmp_path):
    worktree_pool = _load("worktree_pool")
    hook_dir = tmp_path / "hooks"
    engine_root = tmp_path / "engine"
    bindir = tmp_path / "bin"
    hook_dir.mkdir()
    bindir.mkdir()
    board = engine_root / ".project_manager" / "tools" / "board.py"
    board.parent.mkdir(parents=True)
    board.write_text("# fake board\n", encoding="utf-8")
    shutil.copy2(TOOLS / "python_floor.py", board.with_name("python_floor.py"))
    (hook_dir / "protected").write_text("main\n", encoding="utf-8")
    (hook_dir / "engine-root").write_text(f"{engine_root}\n", encoding="utf-8")
    (hook_dir / "gate-contract").write_text("release\npytest -q\n", encoding="utf-8")
    hook = hook_dir / "pre-push"
    hook.write_text(worktree_pool._PROTECTED_PRE_PUSH_HOOK, encoding="utf-8")
    hook.chmod(0o755)

    _fake_python(bindir / "python3", version="3.9.18", supported=False, selected="python3-old")
    _fake_python(bindir / "python", version="3.11.9", supported=True, selected="python")
    selected = tmp_path / "selected.log"
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PM_ALLOW_PROTECTED_PUSH": "1",
        "SELECT_LOG": str(selected),
    }
    proc = subprocess.run(
        [shutil.which("sh"), str(hook)],
        input="refs/heads/local 0000 refs/heads/main 1111\n",
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert selected.read_text(encoding="utf-8").splitlines() == ["python"]


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재")
def test_embedded_hook_reports_all_old_python_versions(tmp_path):
    worktree_pool = _load("worktree_pool")
    hook_dir = tmp_path / "hooks"
    engine_root = tmp_path / "engine"
    bindir = tmp_path / "bin"
    hook_dir.mkdir()
    bindir.mkdir()
    tools = engine_root / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "board.py").write_text("# fake board\n", encoding="utf-8")
    shutil.copy2(TOOLS / "python_floor.py", tools / "python_floor.py")
    (hook_dir / "protected").write_text("main\n", encoding="utf-8")
    (hook_dir / "engine-root").write_text(f"{engine_root}\n", encoding="utf-8")
    (hook_dir / "gate-contract").write_text("release\npytest -q\n", encoding="utf-8")
    hook = hook_dir / "pre-push"
    hook.write_text(worktree_pool._PROTECTED_PRE_PUSH_HOOK, encoding="utf-8")
    hook.chmod(0o755)

    _fake_python(bindir / "python3", version="3.9.18", supported=False, selected="python3-old")
    _fake_python(bindir / "python", version="2.7.18", supported=False, selected="python-old")
    _fake_python(bindir / "py", version="3.10.14", supported=False, selected="py-old")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PM_ALLOW_PROTECTED_PUSH": "1",
    }
    proc = subprocess.run(
        [shutil.which("sh"), str(hook)],
        input="refs/heads/local 0000 refs/heads/main 1111\n",
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1
    assert "board.py 를 못 찾았다" not in proc.stderr
    assert "Python 3.11+ 필요" in proc.stderr
    assert "python3=3.9" in proc.stderr
    assert "python=2.7" in proc.stderr
    assert "py=3.10" in proc.stderr
