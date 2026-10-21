"""엔진 패키지(`pm_engine/**`) 구조 규칙 (T-0963 · spike engine-layered-refactor §3).

R1 층 순위(허용 import 대상·경로 적재 금지), R2 도메인 층 순수성, R3 순환 없음,
R4 패키지 안은 모듈 단위 import 만, R7 모듈 1,500줄 이하.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from _engine_tree import engine_python_files

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
