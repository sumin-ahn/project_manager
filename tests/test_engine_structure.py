"""엔진 패키지(`pm_engine/**`) 구조 규칙 (T-0963 · spike engine-layered-refactor §3).

R1 층 순위(허용 import 대상·경로 적재 금지), R2 도메인 층 순수성, R3 순환 없음,
R4 패키지 안은 모듈 단위 import 만, R7 모듈 1,500줄 이하.
R5 평면 진입 파일은 하나의 문법(경로 삽입·cli 모듈 하나 import·콘솔 설정 뒤 패키지 main 호출)만,
R6 어댑터는 `from pm_engine.cli import hooks` 외 엔진 import·평면 엔진 import·경로 적재 금지.
R8 넓은 처리부(Exception·BaseException·맨 except)는 모든 경로가 재전파하거나(P1) 훅 진입 fail-open
(P2·엔진)·원인 보고(P3·어댑터)일 때만 허용하고, 넓은 suppress 는 금지한다.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _engine_tree import engine_python_files
from pm_engine.infra import repo_files

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / ".project_manager" / "tools" / "pm_engine"
RANK = {"domain": 0, "infra": 1, "app": 2, "cli": 3}
DOMAIN_FORBIDDEN_MODULES = frozenset({"os", "subprocess", "shutil", "tempfile", "socket", "urllib", "http"})
DOMAIN_FORBIDDEN_CALLS = frozenset({"open", "print"})
PATH_LOAD_CALLS = frozenset({"spec_from_file_location", "SourceFileLoader", "run_path",
                             "load_module", "_load_module_from_path"})
# 경로 적재 허용 지점: (모듈, 함수, 호출). 엔진 경로 적재 경계 하나와 엔진 밖 채택자 lint 훅 적재 하나.
PATH_LOAD_ALLOWED = frozenset({
    ("pm_engine.infra.repo_files", "load_module", "spec_from_file_location"),
    ("pm_engine.app.lint_hooks", "_run_lint_hooks", "load_module"),
})
MAX_LINES = 1500
TOOLS = PACKAGE.parent
# R5 의 유일한 대상 밖 파일: 엔진을 import 하지 않고 하한 미만 Python 에서 도는 하한 probe.
R5_EXCLUDED_ENTRY = "python_floor.py"
R5_MIN_ENTRY_FILES = 17
R5_TOOLS_ASSIGN = "_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))"
R5_TOOLS_IF = "if _TOOLS_DIR not in sys.path:\n    sys.path.insert(0, _TOOLS_DIR)"
R5_REPO_ASSIGN = "REPO = Path(__file__).resolve().parents[2]"
R5_FLOOR_TEST = "sys.version_info[:2] < MIN_PYTHON"
R5_CONSOLE_CALL = "console.configure_console_utf8()"
R5_GUARD = "if __name__ == '__main__':\n    sys.exit(main(sys.argv[1:]))"
R5_INFRA_NAMES = frozenset({"console", "pm_paths"})
R6_PATH_LOAD_CALLS = frozenset({"spec_from_file_location", "SourceFileLoader", "run_path", "load_module",
                                "module_from_spec", "exec_module", "import_module", "__import__"})
R6_JS_SUFFIXES = (".js", ".cjs", ".mjs")
R6_ADAPTER_DIRS = (".claude", ".codex", ".opencode")
R6_JS_ADAPTER_DIR = ".opencode"
R6_MIN_PYTHON_ADAPTERS = 6
R6_MIN_JS_ADAPTERS = 16
R6_JS_PYTHON_IMPORT = re.compile(r"\b(?:from|import)\s+(pm_engine(?:\.\w+)*)(?:\s+import\s+([\w, ]+))?")
R6_JS_PATH_LOAD = re.compile(r"\b(spec_from_file_location|SourceFileLoader|run_path)\b")
R8_BROAD_NAMES = frozenset({"Exception", "BaseException"})
R8_ENTRY_PACKAGE = "pm_engine.cli"
R8_MESSAGE = "넓은 처리부가 예외를 삼킨다"


def package_modules(package: Path) -> dict[str, Path]:
    out = {}
    for path in engine_python_files(package):
        rel = path.relative_to(package.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join(parts)] = path
    return out


def layer_rank(module: str):
    parts = module.split(".")
    return RANK.get(parts[1]) if len(parts) > 1 else None


def collect(package: Path) -> dict[str, list[str]]:
    modules = package_modules(package)
    violations: dict[str, list[str]] = {"R1": [], "R2": [], "R3": [], "R4": [], "R7": []}
    graph: dict[str, set[str]] = {name: set() for name in modules}
    stdlib = set(sys.stdlib_module_names)
    for name, path in modules.items():
        text = path.read_text(encoding="utf-8")
        if len(text.splitlines()) > MAX_LINES:
            violations["R7"].append(f"{name}: {len(text.splitlines())}줄")
        tree = ast.parse(text, filename=str(path))
        rank = layer_rank(name)
        is_domain = name.startswith("pm_engine.domain")
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    violations["R4"].append(f"{name}:{node.lineno}: 상대 import")
                    continue
                base = node.module or ""
                if base == "pm_engine" or base.startswith("pm_engine."):
                    for alias in node.names:
                        full = f"{base}.{alias.name}"
                        if full not in modules:
                            violations["R4"].append(f"{name}:{node.lineno}: 이름 import {full}")
                        else:
                            targets.append(full)
                    if not node.names:
                        continue
                else:
                    targets = [base]
            elif isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                if is_domain and isinstance(func, ast.Name) and called in DOMAIN_FORBIDDEN_CALLS:
                    violations["R2"].append(f"{name}:{node.lineno}: {called}() 호출")
                if called in PATH_LOAD_CALLS:
                    scope = node
                    while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        scope = parents[scope]
                    inside = getattr(scope, "name", None)
                    if (name, inside, called) not in PATH_LOAD_ALLOWED:
                        violations["R1"].append(f"{name}:{node.lineno}: 경로 적재 {called}()")
                continue
            else:
                continue
            for target in targets:
                top = target.split(".")[0]
                if top == "pm_engine":
                    if target not in modules:
                        violations["R1"].append(f"{name}:{node.lineno}: 없는 모듈 {target}")
                        continue
                    target_rank = layer_rank(target)
                    if rank is None or (target_rank is not None and target_rank > rank):
                        violations["R1"].append(f"{name}:{node.lineno}: 순위 역전 {target}")
                    graph[name].add(target)
                elif top == "yaml" or top in stdlib:
                    if is_domain and top in DOMAIN_FORBIDDEN_MODULES:
                        violations["R2"].append(f"{name}:{node.lineno}: import {target}")
                else:
                    violations["R1"].append(f"{name}:{node.lineno}: 허용 밖 import {target}")
    # R3 순환: 반복 DFS 색칠
    state: dict[str, int] = {}
    for start in graph:
        if state.get(start):
            continue
        stack = [(start, iter(sorted(graph[start])))]
        state[start] = 1
        path = [start]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
                path.pop()
                continue
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt):] + [nxt]
                violations["R3"].append(" -> ".join(cycle))
            elif not state.get(nxt):
                state[nxt] = 1
                stack.append((nxt, iter(sorted(graph.get(nxt, ())))))
                path.append(nxt)
    return violations


def test_real_tree_is_scanned():
    assert package_modules(PACKAGE), "pm_engine 모듈이 0개다(공허 검사)"


@pytest.mark.parametrize("rule", ["R1", "R2", "R3", "R4", "R7"])
def test_real_tree_rule(rule):
    assert collect(PACKAGE)[rule] == []


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("rule,rel,text", [
    ("R1", "pm_engine/domain/bad.py", "from pm_engine.infra import fs\n"),
    ("R1", "pm_engine/infra/bad.py", "import board\n"),
    ("R1", "pm_engine/infra/bad.py", "import importlib.util\nimportlib.util.spec_from_file_location('x', 'y')\n"),
    ("R1", "pm_engine/app/lint_hooks.py", "from pm_engine.infra import repo_files\ndef other():\n    repo_files.load_module(1, 'x')\n"),
    ("R2", "pm_engine/domain/bad.py", "import os\n"),
    ("R2", "pm_engine/domain/bad.py", "print('x')\n"),
    ("R3", "pm_engine/infra/bad.py", "from pm_engine.infra import fs\n"),
    ("R4", "pm_engine/infra/bad.py", "from pm_engine.infra.fs import atomic_replace\n"),
    ("R7", "pm_engine/infra/bad.py", "x = 1\n" * 1501),
])
def test_rule_detects_synthetic_violation(tmp_path, rule, rel, text):
    _write(tmp_path, "pm_engine/__init__.py", "")
    _write(tmp_path, "pm_engine/domain/__init__.py", "")
    _write(tmp_path, "pm_engine/infra/__init__.py", "")
    _write(tmp_path, "pm_engine/infra/fs.py", "from pm_engine.infra import bad\n" if rule == "R3" else "")
    _write(tmp_path, rel, text)
    assert collect(tmp_path / "pm_engine")[rule], rule


def test_entry_file_bootstraps_package_from_isolated_copy(tmp_path):
    """`python -I` 격리 실행에서 임의 이름으로 적재한 진입 파일이 자기 형제 pm_engine 을 쓴다."""
    tools = tmp_path / "isolated" / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(PACKAGE.parent / "pm_log.py", tools / "pm_log.py")
    shutil.copytree(PACKAGE, tools / PACKAGE.name, ignore=shutil.ignore_patterns("__pycache__"))
    probe = (
        "import importlib.util\n"
        "from pathlib import Path\n"
        f"path = Path({str(tools / 'pm_log.py')!r})\n"
        "spec = importlib.util.spec_from_file_location('arbitrary_isolated_name', path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "assert callable(module.console.configure_console_utf8)\n"
        "print(Path(module.console.__file__).resolve())\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    loaded = Path(result.stdout.strip())
    assert loaded == (tools / PACKAGE.name / "infra" / "console.py").resolve(), loaded


def _r5_head(node: ast.stmt | None) -> str:
    return ast.unparse(node).splitlines()[0][:80] if node is not None else "(파일 끝)"


def _r5_is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _r5_is_floor_exit(node: ast.stmt) -> bool:
    if isinstance(node, ast.Raise):
        call = node.exc
        return (isinstance(call, ast.Call) and ast.unparse(call.func) == "SystemExit"
                and len(call.args) == 1 and not call.keywords)
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "sys.exit" and len(node.value.args) == 1)


def _r5_is_floor_block(node: ast.stmt) -> bool:
    if not (isinstance(node, ast.If) and not node.orelse and ast.unparse(node.test) == R5_FLOOR_TEST):
        return False
    *head, last = node.body
    messages_only = all(
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
        and ast.unparse(stmt.value.func) in ("print", "sys.stderr.write")
        for stmt in head
    )
    return messages_only and _r5_is_floor_exit(last)


def _r5_is_floor_value(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Assign) and len(node.targets) == 1
            and ast.unparse(node.targets[0]) == "MIN_PYTHON"
            and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2
            and all(isinstance(elt, ast.Constant) and type(elt.value) is int for elt in node.value.elts))


def r5_violations(source_text: str) -> list[str]:
    """평면 진입 파일 최상위 문장을 하나의 문법으로 차례로 소비하고 어긋난 곳을 돌려준다.

    순서: docstring → `from __future__` → os·sys·Path import → (MIN_PYTHON 하한 블록) →
    `_TOOLS_DIR` 대입 → sys.path 삽입 → cli 모듈 하나와 infra console[·pm_paths] import →
    (REPO 식) → `def main(argv=None)` → `__main__` 가드. os·sys·Path import 는 하한 블록 앞뒤와
    sys.path 삽입 뒤에 올 수 있다. 모듈을 import·실행하지 않고 AST 만 읽는다.
    """
    body = ast.parse(source_text).body
    out: list[str] = []
    index = 0
    seen_std: set[str] = set()

    def current() -> ast.stmt | None:
        return body[index] if index < len(body) else None

    def take(predicate) -> bool:
        nonlocal index
        if index < len(body) and predicate(body[index]):
            index += 1
            return True
        return False

    def report(expected: str) -> None:
        out.append(f"문장 {index + 1}: {_r5_head(current())} — 기대 {expected}")

    def std_import(node: ast.stmt) -> bool:
        if isinstance(node, ast.Import) and len(node.names) == 1:
            alias = node.names[0]
            if alias.asname is None and alias.name in ("os", "sys") and alias.name not in seen_std:
                seen_std.add(alias.name)
                return True
        if (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "pathlib"
                and [(a.name, a.asname) for a in node.names] == [("Path", None)] and "Path" not in seen_std):
            seen_std.add("Path")
            return True
        return False

    take(_r5_is_docstring)
    while take(lambda n: isinstance(n, ast.ImportFrom) and n.module == "__future__"):
        pass
    while take(std_import):
        pass
    if take(_r5_is_floor_value):
        if not take(_r5_is_floor_block):
            report(f"`if {R5_FLOOR_TEST}:` 안 print·sys.stderr.write 뒤 raise SystemExit/sys.exit 하나")
            return out
        while take(std_import):
            pass
    if not take(lambda n: ast.unparse(n) == R5_TOOLS_ASSIGN):
        report(f"`{R5_TOOLS_ASSIGN}`")
        return out
    if not take(lambda n: ast.unparse(n) == R5_TOOLS_IF):
        report("`if _TOOLS_DIR not in sys.path: sys.path.insert(0, _TOOLS_DIR)`")
        return out
    while take(std_import):
        pass
    cli_modules: list[str] = []
    infra_names: set[str] = set()

    def package_import(node: ast.stmt) -> bool:
        if not (isinstance(node, ast.ImportFrom) and node.level == 0
                and all(alias.asname is None for alias in node.names)):
            return False
        if node.module == "pm_engine.cli" and len(node.names) == 1:
            cli_modules.append(node.names[0].name)
            return True
        if node.module == "pm_engine.infra" and {alias.name for alias in node.names} <= R5_INFRA_NAMES:
            infra_names.update(alias.name for alias in node.names)
            return True
        return False

    while take(package_import):
        pass
    if len(cli_modules) != 1:
        out.append(f"pm_engine.cli 모듈 import {len(cli_modules)}개 — 기대 별칭 없는 1개")
    if "console" not in infra_names:
        out.append("`from pm_engine.infra import console` 없음 — 기대 console import")
    has_repo = take(lambda n: ast.unparse(n) == R5_REPO_ASSIGN)
    if has_repo and "Path" not in seen_std:
        out.append("REPO 식이 있는데 `from pathlib import Path` 없음")
    if has_repo != ("pm_paths" in infra_names):
        out.append("REPO 식과 pm_paths import 는 함께 있거나 함께 없어야 한다")
    module = cli_modules[0] if cli_modules else "<cli 모듈>"
    expected_return = (f"return {module}.main(pm_paths.home_at(REPO), argv)" if has_repo
                       else f"return {module}.main(argv)")

    def entry_main(node: ast.stmt) -> bool:
        if not (isinstance(node, ast.FunctionDef) and node.name == "main"
                and not node.decorator_list and node.returns is None):
            return False
        args = node.args
        if (args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg
                or [arg.arg for arg in args.args] != ["argv"] or args.args[0].annotation is not None
                or len(args.defaults) != 1
                or not (isinstance(args.defaults[0], ast.Constant) and args.defaults[0].value is None)):
            return False
        return [ast.unparse(stmt) for stmt in node.body] == [R5_CONSOLE_CALL, expected_return]

    if not take(entry_main):
        report(f"`def main(argv=None):` 본문 `{R5_CONSOLE_CALL}` 다음 `{expected_return}`")
        return out
    if not take(lambda n: ast.unparse(n) == R5_GUARD):
        report('`if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`')
        return out
    if index != len(body):
        report("파일 끝(허용 형태 밖 문장)")
    return out


def r5_entry_files(tools: Path) -> list[Path]:
    return [path for path in sorted(tools.glob("*.py")) if path.name != R5_EXCLUDED_ENTRY]


def test_r5_entry_files_have_the_single_form():
    targets = r5_entry_files(TOOLS)
    assert len(targets) >= R5_MIN_ENTRY_FILES, [path.name for path in targets]
    violations = {
        path.name: found
        for path in targets
        if (found := r5_violations(path.read_text(encoding="utf-8")))
    }
    assert violations == {}


_R5_VALID_ENTRY = '''"""합성 진입."""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import log
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return log.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

_R5_VALID_FLOOR_ENTRY = '''"""하한 블록이 있는 합성 진입."""
import sys

MIN_PYTHON = (3, 11)

if sys.version_info[:2] < MIN_PYTHON:
    print("하한 미만", file=sys.stderr)
    sys.exit(1)

import os

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import local_conf
from pm_engine.infra import console


def main(argv=None):
    console.configure_console_utf8()
    return local_conf.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

_R5_MAIN_DEF = "def main(argv=None):\n"
_R5_MAIN_BODY = "    console.configure_console_utf8()\n    return log.main(pm_paths.home_at(REPO), argv)\n"


@pytest.mark.parametrize("source", [
    _R5_VALID_ENTRY.replace("\n\ndef main", "\n\ndef helper():\n    return 1\n\n\ndef main"),
    _R5_VALID_ENTRY.replace("import os\n", "import os\nimport json\n"),
    _R5_VALID_ENTRY.replace("from pm_engine.cli import log\n", "import board\nfrom pm_engine.cli import log\n"),
    _R5_VALID_ENTRY.replace("REPO = Path(__file__).resolve().parents[2]\n",
                            "REPO = Path(__file__).resolve().parents[2]\n"
                            "SPEC = importlib.util.spec_from_file_location('board', 'board.py')\n"),
    _R5_VALID_ENTRY.replace(_R5_MAIN_BODY, "    return log.main(pm_paths.home_at(REPO), argv)\n"),
    _R5_VALID_ENTRY.replace(_R5_MAIN_BODY, "    argv = argv\n" + _R5_MAIN_BODY),
    _R5_VALID_ENTRY.replace("from pm_engine.cli import log\n", "from pm_engine.cli import log\nfrom pm_engine.cli import hooks\n"),
    _R5_VALID_ENTRY.replace(_R5_MAIN_DEF, "def main(argv=None, run_fn=None):\n"),
    _R5_VALID_ENTRY.replace(_R5_MAIN_DEF, "def main(argv: list[str] | None = None) -> int:\n"),
    _R5_VALID_ENTRY.replace("REPO = Path(__file__).resolve().parents[2]\n", "REPO = Path(__file__).parent.parent.parent\n"),
    _R5_VALID_ENTRY.replace("    sys.exit(main(sys.argv[1:]))\n", "    sys.exit(main())\n"),
    _R5_VALID_FLOOR_ENTRY.replace('    print("하한 미만", file=sys.stderr)\n', '    reason = "하한 미만"\n'),
], ids=[
    "extra-function", "import-json", "import-flat-board", "path-load-call", "console-missing",
    "console-not-first", "two-cli-imports", "main-extra-argument", "main-annotation",
    "repo-other-expression", "guard-without-argv", "floor-block-assignment",
])
def test_r5_reports_synthetic_entry_violations(source):
    # 기준 형태 두 개는 위반 0 이다 — 검출이 변형 한 곳에서 나온다는 대조군.
    assert r5_violations(_R5_VALID_ENTRY) == [] and r5_violations(_R5_VALID_FLOOR_ENTRY) == []
    assert source not in (_R5_VALID_ENTRY, _R5_VALID_FLOOR_ENTRY)
    assert r5_violations(source), source


def _r6_python_violations(source_text: str, flat_names: frozenset[str]) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(source_text)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top == "pm_engine" or top in flat_names:
                    out.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            names = [(alias.name, alias.asname) for alias in node.names]
            if top == "pm_engine" and not (node.module == "pm_engine.cli" and names == [("hooks", None)]):
                out.append(f"{node.lineno}: from {node.module} import {', '.join(n for n, _ in names)}")
            elif top in flat_names:
                out.append(f"{node.lineno}: from {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if called in R6_PATH_LOAD_CALLS:
                out.append(f"{node.lineno}: 경로 적재 {called}()")
    return out


def _r6_js_violations(source_text: str) -> list[str]:
    out: list[str] = []
    for match in R6_JS_PYTHON_IMPORT.finditer(source_text):
        module, names = match.group(1), (match.group(2) or "").replace(" ", "")
        if not (module == "pm_engine.cli" and names == "hooks"):
            out.append(f"{source_text.count(chr(10), 0, match.start()) + 1}: 내장 Python {match.group(0)}")
    for match in R6_JS_PATH_LOAD.finditer(source_text):
        out.append(f"{source_text.count(chr(10), 0, match.start()) + 1}: 내장 경로 적재 {match.group(1)}")
    return out


def r6_violations(path: Path, source_text: str, flat_names: frozenset[str]) -> list[str]:
    """어댑터 파일 하나의 엔진 import 경계 위반. Python 은 AST, JS 는 내장 Python 원문을 본다."""
    if path.suffix == ".py":
        return _r6_python_violations(source_text, flat_names)
    return _r6_js_violations(source_text)


def r6_adapter_files(root: Path) -> list[Path]:
    """templates 타깃과 저장소 루트의 repo 소유 어댑터 Python·JS (git ignore 대상인 npm 의존성 제외)."""
    bases = [Path("."), *sorted(path.relative_to(root) for path in (root / "templates").iterdir() if path.is_dir())]
    found: list[Path] = []
    for base in bases:
        for directory in R6_ADAPTER_DIRS:
            if not (root / base / directory).is_dir():
                continue
            for relative in repo_files.list_repo_owned_files(root, base / directory, mode=repo_files.OWNED):
                if relative.suffix == ".py" or (directory == R6_JS_ADAPTER_DIR and relative.suffix in R6_JS_SUFFIXES):
                    found.append(root / relative)
    return sorted(found)


def _flat_engine_names() -> frozenset[str]:
    return frozenset(path.stem for path in TOOLS.glob("*.py"))


def test_r6_adapters_import_only_public_hooks():
    targets = r6_adapter_files(REPO)
    python_files = [path for path in targets if path.suffix == ".py"]
    js_files = [path for path in targets if path.suffix in R6_JS_SUFFIXES]
    assert len(python_files) >= R6_MIN_PYTHON_ADAPTERS, python_files
    assert len(js_files) >= R6_MIN_JS_ADAPTERS, js_files
    flat_names = _flat_engine_names()
    violations = {
        path.relative_to(REPO).as_posix(): found
        for path in targets
        if (found := r6_violations(path, path.read_text(encoding="utf-8"), flat_names))
    }
    assert violations == {}


@pytest.mark.parametrize("name,source", [
    ("adapter.py", "from pm_engine.app import principle_recall\n"),
    ("adapter.py", "from pm_engine.cli import hooks as h\n"),
    ("adapter.py", "import board\n"),
    ("adapter.py", "import importlib.util\nimportlib.util.spec_from_file_location('x', 'board.py')\n"),
    ("adapter.py", "import importlib\nimportlib.import_module('pm_engine.app.hooks')\n"),
    ("adapter.py", "import runpy\nrunpy.run_path('board.py')\n"),
    ("adapter.cjs", "const code = `from pm_engine.infra import conf_store`;\n"),
    ("adapter.js", "const code = 'importlib.util.spec_from_file_location(name, path)';\n"),
], ids=[
    "app-module-import", "hooks-alias", "flat-board-import", "spec-from-file-location",
    "import-module", "runpy-run-path", "js-embedded-engine-import", "js-embedded-path-load",
])
def test_r6_reports_synthetic_adapter_violations(name, source):
    flat_names = _flat_engine_names()
    # 공개 hooks import 와 CLI subprocess 호출은 위반 0 이다 — 검출이 금지 형태에서 나온다는 대조군.
    assert r6_violations(Path("adapter.py"), "from pm_engine.cli import hooks\n", flat_names) == []
    assert r6_violations(Path("adapter.js"), "spawn(python, ['board.py', 'hook-tool'])\n", flat_names) == []
    assert r6_violations(Path(name), source, flat_names), source


# ── R8 예외 삼킴 ─────────────────────────────────────────────────────────────


def _r8_names(node: ast.expr) -> list[str]:
    elements = node.elts if isinstance(node, ast.Tuple) else [node]
    return [element.id if isinstance(element, ast.Name) else element.attr
            for element in elements if isinstance(element, (ast.Name, ast.Attribute))]


def _r8_is_broad_handler(handler: ast.ExceptHandler) -> bool:
    return handler.type is None or bool(R8_BROAD_NAMES & set(_r8_names(handler.type)))


def _r8_is_suppress_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    called = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    return called == "suppress"


def _r8_is_broad_suppress(item: ast.withitem) -> bool:
    call = item.context_expr
    return _r8_is_suppress_call(call) and any(
        R8_BROAD_NAMES & set(_r8_names(argument)) for argument in call.args)


def _r8_is_exit(statement: ast.stmt) -> bool:
    return (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func) == "sys.exit")


def _r8_own_nodes(statements: list[ast.stmt]):
    """문장 목록 아래 노드 — 중첩 함수·클래스·lambda 본문은 보지 않는다."""
    stack = list(statements)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)))


def _r8_flow(statements: list[ast.stmt], return_allowed) -> tuple[bool, bool]:
    """(정상 종료 가능, raise 아닌 탈출 가능). `return_allowed(ret)` 가 참인 return 은 허용 종결이다."""
    for statement in statements:
        falls, escapes = _r8_flow_statement(statement, return_allowed)
        if escapes:
            return True, True
        if not falls:
            return False, False
    return True, False


def _r8_flow_statement(statement: ast.stmt, return_allowed) -> tuple[bool, bool]:
    if isinstance(statement, ast.Raise) or _r8_is_exit(statement):
        return False, False
    if isinstance(statement, ast.Return):
        return False, not return_allowed(statement)
    if isinstance(statement, (ast.Break, ast.Continue)):
        return False, True
    if isinstance(statement, ast.If):
        body_falls, body_escapes = _r8_flow(statement.body, return_allowed)
        else_falls, else_escapes = (_r8_flow(statement.orelse, return_allowed)
                                    if statement.orelse else (True, False))
        return body_falls or else_falls, body_escapes or else_escapes
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        falls, escapes = _r8_flow(statement.body, return_allowed)
        if any(_r8_is_suppress_call(item.context_expr) for item in statement.items):
            return True, escapes
        return falls, escapes
    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While, ast.Match)):
        escapes = any(isinstance(node, ast.Return) and not return_allowed(node)
                      for node in _r8_own_nodes([statement]))
        return True, escapes
    if isinstance(statement, ast.Try):
        falls, escapes = _r8_flow(statement.body + statement.orelse, return_allowed)
        for handler in statement.handlers:
            handler_falls, handler_escapes = _r8_flow(handler.body, return_allowed)
            falls, escapes = falls or handler_falls, escapes or handler_escapes
        if statement.finalbody:
            final_falls, final_escapes = _r8_flow(statement.finalbody, return_allowed)
            if not final_falls:
                return False, final_escapes
            escapes = escapes or final_escapes
        return falls, escapes
    return True, False


def _paths_all_raise(stmts: list[ast.stmt]) -> bool:
    """처리부 본문의 모든 경로가 raise·`sys.exit(...)` 로 끝나는가 (P1)."""
    falls, escapes = _r8_flow(stmts, lambda _return: False)
    return not falls and not escapes


def _r8_reads_bound_name(handler: ast.ExceptHandler) -> bool:
    return bool(handler.name) and any(
        isinstance(node, ast.Name) and node.id == handler.name and isinstance(node.ctx, ast.Load)
        for node in _r8_own_nodes(handler.body))


def _r8_is_default_literal(value: ast.expr | None) -> bool:
    if value is None:
        return True
    if isinstance(value, ast.Constant):
        return value.value is None or value.value is True or value.value is False or value.value == ""
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return not value.elts
    return isinstance(value, ast.Dict) and not value.keys


def _r8_returns_zero(statement: ast.Return) -> bool:
    value = statement.value
    return isinstance(value, ast.Constant) and type(value.value) is int and value.value == 0


def _r8_form(handler: ast.ExceptHandler) -> str:
    """3.1항 형태 이름 — 위반 문구가 정리할 모양을 가리킨다."""
    nodes = list(_r8_own_nodes(handler.body))
    returns = [node for node in nodes if isinstance(node, ast.Return)]
    if all(isinstance(statement, ast.Pass) for statement in handler.body):
        return "무시"
    if any(isinstance(node, (ast.Break, ast.Continue)) for node in nodes):
        return "건너뛰고 계속"
    if returns:
        if any(_r8_is_default_literal(node.value) for node in returns):
            return "기본값 반환"
        if all(isinstance(node.value, ast.Constant) and type(node.value.value) is int for node in returns):
            return "문구 뒤 rc 반환"
        return "다른 값 반환"
    if any(isinstance(node, ast.Raise) for node in nodes):
        return "일부만 재전파"
    if any(isinstance(node, ast.Call) and ast.unparse(node.func) == "print" for node in nodes):
        return "경고로 강등"
    return "대입 뒤 진행"


def _r8_hook_entry(handler: ast.ExceptHandler, parents: dict, tree: ast.Module) -> bool:
    """P2 — 진입 cli 모듈 최상위 `main` 본문의 직접 try, 예외 이름을 읽고 비 raise 경로는 `return 0`."""
    try_node = parents.get(handler)
    function = parents.get(try_node)
    if not (isinstance(function, ast.FunctionDef) and function.name == "main"
            and try_node in function.body and parents.get(function) is tree):
        return False
    falls, escapes = _r8_flow(handler.body, _r8_returns_zero)
    return _r8_reads_bound_name(handler) and not falls and not escapes


def _r8_adapter_reports(handler: ast.ExceptHandler) -> bool:
    """P3 — 어댑터 처리부가 예외 이름을 읽어 보고하고 기본값을 돌려주지 않는다."""
    return (_r8_reads_bound_name(handler)
            and not all(isinstance(statement, ast.Pass) for statement in handler.body)
            and not any(isinstance(node, ast.Return) and _r8_is_default_literal(node.value)
                        for node in _r8_own_nodes(handler.body)))


def _r8_entry_module(rel_path: str) -> str | None:
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[-3:-1] == R8_ENTRY_PACKAGE.split("."):
        return parts[-1].removesuffix(".py")
    return None


def r8_violations(rel_path: str, source_text: str, *, adapter: bool = False,
                  entry_cli_modules: frozenset[str] = frozenset()) -> list[str]:
    """파일 하나의 R8 위반 — `<경로>:<줄>: 넓은 처리부가 예외를 삼킨다(<형태>)` 목록."""
    tree = ast.parse(source_text, filename=rel_path)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    is_entry = _r8_entry_module(rel_path) in entry_cli_modules
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)) and any(_r8_is_broad_suppress(item) for item in node.items):
            out.append(f"{rel_path}:{node.lineno}: {R8_MESSAGE}(넓은 suppress)")
            continue
        if not isinstance(node, ast.ExceptHandler) or not _r8_is_broad_handler(node):
            continue
        if _paths_all_raise(node.body):
            continue
        if adapter and _r8_adapter_reports(node):
            continue
        if not adapter and is_entry and _r8_hook_entry(node, parents, tree):
            continue
        out.append(f"{rel_path}:{node.lineno}: {R8_MESSAGE}({_r8_form(node)})")
    return sorted(out, key=lambda line: int(line.split(":")[1]))


def entry_cli_modules(tools: Path) -> frozenset[str]:
    """평면 진입 파일이 `from pm_engine.cli import X` 로 싣는 cli 모듈 이름 집합."""
    modules: set[str] = set()
    for path in sorted(tools.glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module == R8_ENTRY_PACKAGE:
                modules.update(alias.name for alias in node.names)
    return frozenset(modules)


def test_r8_engine_has_no_swallow():
    entries = entry_cli_modules(TOOLS)
    assert entries, "평면 진입 파일의 cli 모듈 import 가 0개다(공허 검사)"
    files = engine_python_files(TOOLS)
    assert files, "엔진 스캔 파일이 0개다(공허 검사)"
    violations = [
        line
        for path in files
        for line in r8_violations(path.relative_to(TOOLS).as_posix(), path.read_text(encoding="utf-8"),
                                  entry_cli_modules=entries)
    ]
    assert violations == []


def test_r8_adapters_report_every_broad_catch():
    files = [path for path in r6_adapter_files(REPO) if path.suffix == ".py"]
    assert files, "어댑터 Python 스캔 파일이 0개다(공허 검사)"
    violations = [
        line
        for path in files
        for line in r8_violations(path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8"),
                                  adapter=True)
    ]
    assert violations == []


_R8_INFRA = "pm_engine/infra/sample.py"
_R8_ENTRY = "pm_engine/cli/hookmod.py"
_R8_ADAPTER = "templates/claude_code/.claude/hooks/sample.py"
_R8_ENTRY_SET = frozenset({"hookmod"})

_R8_SWALLOW_CASES = {
    "default-return": (_R8_INFRA, "def f():\n    try:\n        g()\n    except Exception:\n        return None\n",
                       False, _R8_ENTRY_SET, "기본값 반환"),
    "other-value": (_R8_INFRA, "def f():\n    try:\n        g()\n    except Exception as exc:\n        return str(exc)\n",
                    False, _R8_ENTRY_SET, "다른 값 반환"),
    "pass": (_R8_INFRA, "def f():\n    try:\n        g()\n    except BaseException:\n        pass\n",
             False, _R8_ENTRY_SET, "무시"),
    "continue": (_R8_INFRA, "def f(xs):\n    for x in xs:\n        try:\n            g(x)\n        except Exception:\n            continue\n",
                 False, _R8_ENTRY_SET, "건너뛰고 계속"),
    "warn-only": (_R8_INFRA, "import sys\ndef f():\n    try:\n        g()\n    except Exception as exc:\n        print(exc, file=sys.stderr)\n",
                  False, _R8_ENTRY_SET, "경고로 강등"),
    "assign": (_R8_INFRA, "def f():\n    try:\n        value = g()\n    except Exception:\n        value = None\n    return value\n",
               False, _R8_ENTRY_SET, "대입 뒤 진행"),
    "partial-reraise": (_R8_INFRA, "def f(flag):\n    try:\n        g()\n    except Exception as exc:\n        if flag:\n            raise\n        return None\n",
                        False, _R8_ENTRY_SET, "기본값 반환"),
    "bare": (_R8_INFRA, "def f():\n    try:\n        g()\n    except:\n        return []\n",
             False, _R8_ENTRY_SET, "기본값 반환"),
    "tuple": (_R8_INFRA, "def f():\n    try:\n        g()\n    except (OSError, Exception):\n        return None\n",
              False, _R8_ENTRY_SET, "기본값 반환"),
    "early-return-before-raise": (
        _R8_INFRA, "def f():\n    try:\n        g()\n    except Exception as exc:\n        if isinstance(exc, KeyError):\n            return 1\n        raise\n",
        False, _R8_ENTRY_SET, "문구 뒤 rc 반환"),
    "suppress-attr": (_R8_INFRA, "import contextlib\ndef f():\n    with contextlib.suppress(Exception):\n        g()\n",
                      False, _R8_ENTRY_SET, "넓은 suppress"),
    "suppress-name": (_R8_INFRA, "from contextlib import suppress\ndef f():\n    with suppress(BaseException):\n        g()\n",
                      False, _R8_ENTRY_SET, "넓은 suppress"),
    "entry-not-imported": (
        _R8_ENTRY, "def main():\n    try:\n        return run()\n    except BaseException as exc:\n        report(exc)\n        return 0\n",
        False, frozenset(), "문구 뒤 rc 반환"),
    "entry-not-outermost": (
        _R8_ENTRY, "def main(flag):\n    if flag:\n        try:\n            return run()\n        except BaseException as exc:\n            report(exc)\n            return 0\n    return 1\n",
        False, _R8_ENTRY_SET, "문구 뒤 rc 반환"),
    "entry-unbound": (_R8_ENTRY, "def main():\n    try:\n        return run()\n    except BaseException:\n        return 0\n",
                      False, _R8_ENTRY_SET, "문구 뒤 rc 반환"),
    "entry-nonzero-rc": (
        _R8_ENTRY, "def main():\n    try:\n        return run()\n    except BaseException as exc:\n        print(exc)\n        return 1\n",
        False, _R8_ENTRY_SET, "문구 뒤 rc 반환"),
    "adapter-default": (_R8_ADAPTER, "def f():\n    try:\n        g()\n    except Exception as exc:\n        log(exc)\n        return ''\n",
                        True, frozenset(), "기본값 반환"),
    "adapter-pass": (_R8_ADAPTER, "def f():\n    try:\n        g()\n    except BaseException:\n        pass\n",
                     True, frozenset(), "무시"),
}

_R8_ALLOWED_CASES = {
    "cleanup-reraise": (_R8_INFRA, "def f():\n    try:\n        g()\n    except BaseException:\n        cleanup()\n        raise\n",
                        False, _R8_ENTRY_SET),
    "convert": (_R8_INFRA, "def f():\n    try:\n        g()\n    except Exception as exc:\n        raise RuntimeError('x') from exc\n",
                False, _R8_ENTRY_SET),
    "both-branches-raise": (
        _R8_INFRA, "def f(flag):\n    try:\n        g()\n    except Exception as exc:\n        if flag:\n            raise\n        else:\n            raise RuntimeError('x') from exc\n",
        False, _R8_ENTRY_SET),
    "narrow": (_R8_INFRA, "def f():\n    try:\n        g()\n    except OSError:\n        return None\n",
               False, _R8_ENTRY_SET),
    "narrow-suppress": (_R8_INFRA, "import contextlib\ndef f():\n    with contextlib.suppress(OSError):\n        g()\n",
                        False, _R8_ENTRY_SET),
    "hook-entry": (_R8_ENTRY, "def main():\n    try:\n        return run()\n    except BaseException as exc:\n        report(exc)\n        return 0\n",
                   False, _R8_ENTRY_SET),
    "adapter-report": (_R8_ADAPTER, "def f():\n    try:\n        return g()\n    except BaseException as exc:\n        return envelope(f'{exc}')\n",
                       True, frozenset()),
}


def _r8_scan_written(tmp_path: Path, rel: str, text: str, adapter: bool, entries: frozenset[str]) -> list[str]:
    _write(tmp_path, rel, text)
    return r8_violations(rel, (tmp_path / rel).read_text(encoding="utf-8"),
                         adapter=adapter, entry_cli_modules=entries)


@pytest.mark.parametrize("case", list(_R8_SWALLOW_CASES))
def test_r8_reports_synthetic_swallow(tmp_path, case):
    rel, text, adapter, entries, form = _R8_SWALLOW_CASES[case]
    found = _r8_scan_written(tmp_path, rel, text, adapter, entries)
    assert found, case
    assert all(line.startswith(f"{rel}:") and R8_MESSAGE in line for line in found), found
    assert f"({form})" in found[0], found


@pytest.mark.parametrize("case", list(_R8_ALLOWED_CASES))
def test_r8_accepts_allowed_forms(tmp_path, case):
    rel, text, adapter, entries = _R8_ALLOWED_CASES[case]
    assert _r8_scan_written(tmp_path, rel, text, adapter, entries) == [], case
