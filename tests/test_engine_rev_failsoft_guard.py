"""Repository-wide guard for fail-soft boundaries around marked engine skew.

The scanner follows top-level functions, same-class methods, loader-proven module aliases passed
through parameters or lexical free variables, and literal-name ``getattr`` callable aliases through
conditional/boolean expressions and lexical closures.  Catch recognition covers builtin
``RuntimeError``/``Exception``/``BaseException`` plus repository ``RuntimeError`` subclasses that
are constructed by a marker-preserving transform.  Handlers that consume ``_is_engine_rev_skew``
directly remain explicit roots as a defense against incomplete provenance.

Deliberate exclusions include dynamic ``getattr`` member names, arbitrary duck-typed objects with
same-named methods, callable instance attributes (``self.<attr>``), and invocation links to nested
definitions (their own bodies are still scanned).

An affected handler must re-raise the marked exception, invoke the dedicated recovery marker, or
invoke the dedicated terminal-report marker.  Markers are code-owned, per-boundary intent; there is
no filename/function allow-list.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
_RECOVERY_MARKER = "_absorb_engine_rev_skew_for_recovery"
_RECOVERY_REASONS = "_ENGINE_REV_SKEW_RECOVERY_REASONS"
_TERMINAL_MARKER = "_report_engine_rev_skew_at_terminal"


@dataclass(frozen=True)
class FailSoftBoundary:
    source: str
    function: str
    line: int
    disposition: str


@dataclass(frozen=True)
class FailSoftReport:
    boundaries: tuple[FailSoftBoundary, ...]
    violations: tuple[str, ...]


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _scope_nodes(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    scopes: list[tuple[str, ast.AST]] = [("<module>", tree)]

    def descend(nodes: list[ast.stmt], prefix: str = "") -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{node.name}" if prefix else node.name
                scopes.append((name, node))
                descend(node.body, name)
            elif isinstance(node, ast.ClassDef):
                name = f"{prefix}.{node.name}" if prefix else node.name
                descend(node.body, name)

    descend(tree.body)
    return scopes


class _OwnScopeVisitor(ast.NodeVisitor):
    """Visit one lexical body without treating nested definitions as executed statements."""

    def __init__(self, root: ast.AST):
        self.root = root
        self.calls: list[ast.Call] = []
        self.tries: list[ast.Try | ast.TryStar] = []
        self.assignments: list[ast.Assign | ast.AnnAssign | ast.NamedExpr] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            for statement in node.body:
                self.visit(statement)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node is self.root:
            for statement in node.body:
                self.visit(statement)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        if node is self.root:
            self.visit(node.body)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.assignments.append(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.assignments.append(node)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.assignments.append(node)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.tries.append(node)
        self.generic_visit(node)

    visit_TryStar = visit_Try


def _parents(scope: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    visitor = _OwnScopeVisitor(scope)
    visitor.visit(scope)
    owned = {scope, *visitor.calls, *visitor.tries}
    for node in ast.walk(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if node is not scope:
                continue
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    # Callers use only paths rooted in this scope; nested-definition parents cannot be reached from
    # an owned call because those calls were excluded by ``_OwnScopeVisitor``.
    return parents


def _runtime_exception_names(parsed: dict[str, ast.Module]) -> set[str]:
    """Names of builtins and repository classes that may bear the skew marker."""
    runtime_names = {"RuntimeError"}
    class_bases = {
        node.name: {
            base.id if isinstance(base, ast.Name) else base.attr
            for base in node.bases
            if isinstance(base, (ast.Name, ast.Attribute))
        }
        for tree in parsed.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    changed = True
    while changed:
        changed = False
        for name, bases in class_bases.items():
            if name not in runtime_names and bases & runtime_names:
                runtime_names.add(name)
                changed = True
    return runtime_names | {"Exception", "BaseException"}


def _marker_preserving_exception_names(parsed: dict[str, ast.Module]) -> set[str]:
    """Concrete exception classes created by handlers that copy the skew marker."""
    names: set[str] = set()
    for tree in parsed.values():
        for handler in (
            node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
        ):
            marked_values = {
                target.value.id
                for node in ast.walk(handler)
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if isinstance(target, ast.Attribute)
                and target.attr == "_engine_rev_skew"
                and isinstance(target.value, ast.Name)
                and isinstance(node.value, ast.Constant)
                and node.value.value is True
            }
            for node in ast.walk(handler):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not any(
                    isinstance(target, ast.Name) and target.id in marked_values
                    for target in targets
                ) or not isinstance(node.value, ast.Call):
                    continue
                name = _called_name(node.value)
                if name is not None:
                    names.add(name)
    return names


def _handler_catches_marked_runtime(
    handler: ast.ExceptHandler, runtime_names: set[str],
) -> bool:
    if handler.type is None:
        return True
    names = {
        node.id for node in ast.walk(handler.type) if isinstance(node, ast.Name)
    }
    return bool(names & runtime_names)


def _catching_handler(
    try_node: ast.Try | ast.TryStar,
    runtime_names: set[str],
) -> ast.ExceptHandler | None:
    return next(
        (
            handler for handler in try_node.handlers
            if _handler_catches_marked_runtime(handler, runtime_names)
        ),
        None,
    )


def _call_uses_verifier(call: ast.Call) -> bool:
    if _called_name(call) == "_verify_engine_rev":
        return True
    if isinstance(call.func, ast.Attribute) and call.func.attr == "_load_module":
        return True
    return any(
        keyword.arg == "verifier"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "_verify_engine_rev"
        for keyword in call.keywords
    )


def _call_in_try_body(call: ast.Call, try_node: ast.Try | ast.TryStar,
                      parents: dict[ast.AST, ast.AST]) -> bool:
    child: ast.AST = call
    node = parents.get(child)
    while node is not None and node is not try_node:
        child, node = node, parents.get(node)
    return node is try_node and child in try_node.body


def _marker_call(handler: ast.ExceptHandler, name: str) -> ast.Call | None:
    if not isinstance(handler.name, str):
        return None
    return next(
        (
            node for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and _called_name(node) == name
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == handler.name
        ),
        None,
    )


def _handler_disposition(handler: ast.ExceptHandler) -> str | None:
    marker = _marker_call(handler, "_is_engine_rev_skew")
    if marker is not None:
        for node in ast.walk(handler):
            if not isinstance(node, ast.If) or node.test is not marker:
                continue
            if any(
                isinstance(statement, ast.Raise)
                and (
                    statement.exc is None
                    or (
                        isinstance(statement.exc, ast.Name)
                        and statement.exc.id == handler.name
                    )
                )
                for statement in node.body
            ):
                return "reraises"
            terminal_marker = next(
                (
                    child
                    for statement in node.body
                    for child in ast.walk(statement)
                    # marker 는 `return _report_…(exc)` 형태(Return 의 값)일 때만 인정한다 —
                    # 반환 없는 호출·무진단 helper 가 terminal 로 위장하는 것을 막는다.
                    if isinstance(child, ast.Return)
                    and isinstance(child.value, ast.Call)
                    and _called_name(child.value) == _TERMINAL_MARKER
                    and child.value.args
                    and isinstance(child.value.args[0], ast.Name)
                    and child.value.args[0].id == handler.name
                ),
                None,
            )
            if terminal_marker is not None:
                return "terminal-report"

        marked_names = {
            target.value.id
            for node in ast.walk(handler)
            if isinstance(node, ast.If) and node.test is marker
            for statement in node.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            for target in (
                statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            )
            if isinstance(target, ast.Attribute)
            and target.attr == "_engine_rev_skew"
            and isinstance(target.value, ast.Name)
            and (
                isinstance(statement.value, ast.Constant)
                and statement.value.value is True
            )
        }
        if any(
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Name)
            and node.exc.id in marked_names
            for node in handler.body
        ):
            return "marker-preserving-transform"

        # Once a handler branches on the marker, an unrelated top-level conversion raise must not
        # mask a deleted marked-exception branch.  This ordering is the sensitivity-critical rule.
        return None

    if any(
        isinstance(raised, ast.Raise)
        and (
            raised.exc is None
            or (isinstance(raised.exc, ast.Name) and raised.exc.id == handler.name)
        )
        for raised in handler.body
    ):
        return "reraises"
    if _marker_call(handler, _RECOVERY_MARKER) is not None:
        return "recovery-absorb"
    return None


def _literal_module_names(call: ast.Call, sources: dict[str, str]) -> set[str]:
    """Literal sibling filenames supplied to a loader call."""
    return {
        value.value
        for value in (*call.args, *(keyword.value for keyword in call.keywords))
        if isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and value.value.endswith(".py")
        and value.value in sources
    }


def _assignment_targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AnnAssign):
        return [node.target]
    if isinstance(node, ast.NamedExpr):
        return [node.target]
    return []


def _assignment_value(node: ast.AST) -> ast.expr | None:
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        return node.value
    return None


def _class_prefix(qualname: str) -> str | None:
    return qualname.rsplit(".", 1)[0] if "." in qualname else None


def _enclosing_catches(
    call: ast.Call,
    tries: list[ast.Try | ast.TryStar],
    parents: dict[ast.AST, ast.AST],
    runtime_names: set[str],
) -> list[tuple[ast.Try | ast.TryStar, ast.ExceptHandler]]:
    catches = []
    for try_node in tries:
        handler = _catching_handler(try_node, runtime_names)
        if handler is not None and _call_in_try_body(call, try_node, parents):
            catches.append((try_node, handler))

    def parent_depth(node: ast.AST) -> int:
        depth = 0
        while node in parents:
            depth += 1
            node = parents[node]
        return depth

    return sorted(catches, key=lambda pair: parent_depth(pair[0]), reverse=True)


def collect_failsoft_report(sources: dict[str, str]) -> FailSoftReport:
    parsed = {
        name: ast.parse(source, filename=name) for name, source in sources.items()
    }
    repository_runtime_names = _runtime_exception_names(parsed)
    runtime_names = {"RuntimeError", "Exception", "BaseException"} | (
        _marker_preserving_exception_names(parsed) & repository_runtime_names
    )
    scope_data: dict[tuple[str, str], tuple[_OwnScopeVisitor, dict[ast.AST, ast.AST]]] = {}
    scope_nodes: dict[tuple[str, str], ast.AST] = {}
    simple_scopes: dict[str, dict[str, str]] = {}
    for source_name, tree in parsed.items():
        simple_scopes[source_name] = {}
        for qualname, scope in _scope_nodes(tree):
            visitor = _OwnScopeVisitor(scope)
            visitor.visit(scope)
            scope_data[(source_name, qualname)] = (visitor, _parents(scope))
            scope_nodes[(source_name, qualname)] = scope
            if qualname != "<module>" and "." not in qualname:
                simple_scopes[source_name].setdefault(qualname.rsplit(".", 1)[-1], qualname)

    # A loader's result type is derived from literal sibling filenames at its load seam.  This is
    # deliberately provenance based: an arbitrary object with a same-named method is not linked.
    loader_targets: dict[tuple[str, str], set[str]] = {
        key: set() for key in scope_data
    }
    for key, (visitor, _parents_map) in scope_data.items():
        if not key[1].rsplit(".", 1)[-1].startswith("_load"):
            continue
        for call in visitor.calls:
            if _called_name(call) in {"_load_module", "_load_module_from_path"}:
                loader_targets[key].update(_literal_module_names(call, sources))

    changed = True
    while changed:
        changed = False
        for key, (visitor, _parents_map) in scope_data.items():
            source_name, _qualname = key
            before = len(loader_targets[key])
            for call in visitor.calls:
                if isinstance(call.func, ast.Name):
                    callee = simple_scopes[source_name].get(call.func.id)
                    if callee is not None:
                        loader_targets[key].update(loader_targets[(source_name, callee)])
            if len(loader_targets[key]) != before:
                changed = True

    aliases: dict[tuple[str, str], dict[str, set[str]]] = {
        key: {} for key in scope_data
    }
    callable_aliases: dict[
        tuple[str, str], dict[str, set[tuple[str, str]]]
    ] = {key: {} for key in scope_data}
    class_attrs: dict[tuple[str, str], set[str]] = {}
    parameter_sources: dict[tuple[str, str], dict[str, set[str]]] = {
        key: {} for key in scope_data
    }

    def argument_names(scope: ast.AST) -> list[str]:
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return []
        return [
            argument.arg
            for argument in (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs)
        ]

    bound_names: dict[tuple[str, str], set[str]] = {}
    for key, (visitor, _parents_map) in scope_data.items():
        names = set(argument_names(scope_nodes[key]))
        for assignment in visitor.assignments:
            for target in _assignment_targets(assignment):
                names.update(
                    node.id for node in ast.walk(target) if isinstance(node, ast.Name)
                )
        bound_names[key] = names

    def lexical_ancestors(key: tuple[str, str]):
        source_name, qualname = key
        prefix = qualname
        while "." in prefix:
            prefix = prefix.rsplit(".", 1)[0]
            candidate = (source_name, prefix)
            if candidate in scope_data:
                yield candidate
        module_key = (source_name, "<module>")
        if key != module_key:
            yield module_key

    def name_sources(name: str, key: tuple[str, str]) -> set[str]:
        for candidate in (key, *lexical_ancestors(key)):
            found = set(aliases[candidate].get(name, ()))
            found.update(parameter_sources[candidate].get(name, ()))
            if found:
                return found
            if name in bound_names[candidate]:
                return set()
        return set()

    def callable_name_sources(
        name: str, key: tuple[str, str],
    ) -> set[tuple[str, str]]:
        for candidate in (key, *lexical_ancestors(key)):
            found = set(callable_aliases[candidate].get(name, ()))
            if found:
                return found
            if name in bound_names[candidate]:
                return set()
        return set()

    def module_sources(expr: ast.expr, key: tuple[str, str]) -> set[str]:
        source_name, qualname = key
        if isinstance(expr, ast.Call):
            found = _literal_module_names(expr, sources)
            if isinstance(expr.func, ast.Name):
                callee = simple_scopes[source_name].get(expr.func.id)
                if callee is not None:
                    found.update(loader_targets[(source_name, callee)])
            elif (
                isinstance(expr.func, ast.Attribute)
                and isinstance(expr.func.value, ast.Name)
                and expr.func.value.id in {"self", "cls"}
            ):
                prefix = _class_prefix(qualname)
                callee_key = (source_name, f"{prefix}.{expr.func.attr}")
                if prefix is not None and callee_key in loader_targets:
                    found.update(loader_targets[callee_key])
            return found
        if isinstance(expr, ast.Name):
            return name_sources(expr.id, key)
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in {"self", "cls"}:
                prefix = _class_prefix(qualname)
                return set(class_attrs.get((source_name, f"{prefix}.{expr.attr}"), ()))
            return set()
        if isinstance(expr, (ast.BoolOp, ast.Tuple, ast.List, ast.Set)):
            found: set[str] = set()
            values = expr.values if isinstance(expr, ast.BoolOp) else expr.elts
            for value in values:
                found.update(module_sources(value, key))
            return found
        if isinstance(expr, ast.IfExp):
            return module_sources(expr.body, key) | module_sources(expr.orelse, key)
        if isinstance(expr, ast.NamedExpr):
            return module_sources(expr.value, key)
        return set()

    def callable_sources(
        expr: ast.expr, key: tuple[str, str],
    ) -> set[tuple[str, str]]:
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "getattr"
            and len(expr.args) >= 2
            and isinstance(expr.args[1], ast.Constant)
            and isinstance(expr.args[1].value, str)
        ):
            member = expr.args[1].value
            return {
                (module_name, simple_scopes[module_name][member])
                for module_name in module_sources(expr.args[0], key)
                if member in simple_scopes.get(module_name, {})
            }
        if isinstance(expr, ast.Name):
            return callable_name_sources(expr.id, key)
        if isinstance(expr, (ast.BoolOp, ast.Tuple, ast.List, ast.Set)):
            found: set[tuple[str, str]] = set()
            values = expr.values if isinstance(expr, ast.BoolOp) else expr.elts
            for value in values:
                found.update(callable_sources(value, key))
            return found
        if isinstance(expr, ast.IfExp):
            return callable_sources(expr.body, key) | callable_sources(expr.orelse, key)
        if isinstance(expr, ast.NamedExpr):
            return callable_sources(expr.value, key)
        return set()

    # Resolve local aliases (``domain = _load_domain_module()``) and class attributes that retain
    # loaded modules.  A fixed point covers ``wp = self._worktree_pool or _load_worktree_pool()``.
    def propagate_aliases() -> bool:
        any_changed = False
        for key, (visitor, _parents_map) in scope_data.items():
            source_name, qualname = key
            for node in visitor.assignments:
                value = _assignment_value(node)
                if value is None:
                    continue
                found = module_sources(value, key)
                if found:
                    for target in _assignment_targets(node):
                        if isinstance(target, ast.Name):
                            bucket = aliases[key].setdefault(target.id, set())
                        elif (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id in {"self", "cls"}
                            and _class_prefix(qualname) is not None
                        ):
                            bucket = class_attrs.setdefault(
                                (source_name, f"{_class_prefix(qualname)}.{target.attr}"), set()
                            )
                        else:
                            continue
                        before = len(bucket)
                        bucket.update(found)
                        if len(bucket) != before:
                            any_changed = True

                resolved = callable_sources(value, key)
                if resolved:
                    for target in _assignment_targets(node):
                        if not isinstance(target, ast.Name):
                            continue
                        call_bucket = callable_aliases[key].setdefault(target.id, set())
                        before = len(call_bucket)
                        call_bucket.update(resolved)
                        if len(call_bucket) != before:
                            any_changed = True
        return any_changed

    while propagate_aliases():
        pass

    def callees(call: ast.Call, key: tuple[str, str]) -> set[tuple[str, str]]:
        source_name, qualname = key
        found: set[tuple[str, str]] = set()
        if isinstance(call.func, ast.Name):
            local = simple_scopes[source_name].get(call.func.id)
            if local is not None:
                found.add((source_name, local))
            found.update(callable_name_sources(call.func.id, key))
            return found
        if not isinstance(call.func, ast.Attribute):
            return found
        attr = call.func.attr
        if isinstance(call.func.value, ast.Name) and call.func.value.id in {"self", "cls"}:
            prefix = _class_prefix(qualname)
            method = f"{prefix}.{attr}" if prefix is not None else ""
            if (source_name, method) in scope_data:
                found.add((source_name, method))
        for module_name in module_sources(call.func.value, key):
            target = simple_scopes.get(module_name, {}).get(attr)
            if target is not None:
                found.add((module_name, target))
        if isinstance(call.func.value, ast.Call) and isinstance(call.func.value.func, ast.Attribute):
            constructor = call.func.value.func
            for module_name in module_sources(constructor.value, key):
                method = f"{constructor.attr}.{attr}"
                if (module_name, method) in scope_data:
                    found.add((module_name, method))
        return found

    # Link module-valued actual arguments to their formal parameters.  Alias and parameter flow
    # share a fixed point because a parameter may be retained in another local or class attribute.
    changed = True
    while changed:
        changed = propagate_aliases()
        for caller_key, (visitor, _parents_map) in scope_data.items():
            for call in visitor.calls:
                for callee_key in callees(call, caller_key):
                    scope = scope_nodes[callee_key]
                    if not isinstance(
                        scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
                    ):
                        continue
                    positional = [
                        argument.arg
                        for argument in (*scope.args.posonlyargs, *scope.args.args)
                    ]
                    if (
                        positional
                        and positional[0] in {"self", "cls"}
                        and isinstance(call.func, ast.Attribute)
                    ):
                        positional = positional[1:]
                    actuals = {
                        name: value for name, value in zip(positional, call.args)
                    }
                    actuals.update(
                        (keyword.arg, keyword.value)
                        for keyword in call.keywords
                        if keyword.arg is not None
                    )
                    valid_parameters = set(argument_names(scope))
                    for parameter, actual in actuals.items():
                        if parameter not in valid_parameters:
                            continue
                        found = module_sources(actual, caller_key)
                        if not found:
                            continue
                        bucket = parameter_sources[callee_key].setdefault(parameter, set())
                        before = len(bucket)
                        bucket.update(found)
                        if len(bucket) != before:
                            changed = True

    may_raise: set[tuple[str, str]] = set()
    boundary_map: dict[tuple[str, str, int], FailSoftBoundary] = {}
    violations: set[str] = set()

    changed = True
    while changed:
        changed = False
        for key, (visitor, parents) in scope_data.items():
            source_name, qualname = key
            for call in visitor.calls:
                is_source = (
                    _call_uses_verifier(call)
                    or bool(callees(call, key) & may_raise)
                )
                if not is_source:
                    continue
                escapes = True
                for _try_node, handler in _enclosing_catches(
                    call, visitor.tries, parents, runtime_names,
                ):
                    disposition = _handler_disposition(handler)
                    boundary_key = (source_name, qualname, handler.lineno)
                    boundary_map[boundary_key] = FailSoftBoundary(
                        source_name, qualname, handler.lineno,
                        disposition or "unmarked-absorb",
                    )
                    if disposition is None:
                        violations.add(
                            f"{source_name}:{handler.lineno}: {qualname} absorbs marked "
                            "engine skew without a re-raise or recovery marker"
                        )
                        escapes = False
                        break
                    if disposition in {"recovery-absorb", "terminal-report"}:
                        escapes = False
                        break
                if escapes and key not in may_raise:
                    may_raise.add(key)
                    changed = True

    # Marker-consuming handlers remain in scope even when their source is a call through a loaded
    # module and therefore cannot be linked by the local call graph.
    for (source_name, qualname), (visitor, _parents_map) in scope_data.items():
        for try_node in visitor.tries:
            for handler in try_node.handlers:
                consumes = _marker_call(handler, "_is_engine_rev_skew") is not None
                recovery = _marker_call(handler, _RECOVERY_MARKER) is not None
                if not consumes and not recovery:
                    continue
                disposition = _handler_disposition(handler)
                boundary_map[(source_name, qualname, handler.lineno)] = FailSoftBoundary(
                    source_name, qualname, handler.lineno,
                    disposition or "unmarked-absorb",
                )
                if disposition is None:
                    violations.add(
                        f"{source_name}:{handler.lineno}: {qualname} consumes marked "
                        "engine skew without a re-raise or recovery marker"
                    )

    for source_name, tree in parsed.items():
        recovery_reasons: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(target, ast.Name) and target.id == _RECOVERY_REASONS
                       for target in targets):
                continue
            if isinstance(node.value, ast.Dict):
                recovery_reasons = {
                    key.value: value.value
                    for key, value in zip(node.value.keys, node.value.values)
                    if isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                }
        helpers = [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == _RECOVERY_MARKER
        ]
        for helper in helpers:
            if not any(
                isinstance(node, ast.Call)
                and _called_name(node) == "_is_engine_rev_skew"
                for node in ast.walk(helper)
            ):
                violations.add(
                    f"{source_name}:{helper.lineno}: recovery marker does not inspect "
                    "_is_engine_rev_skew"
                )
            uses = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and _called_name(node) == _RECOVERY_MARKER
            ]
            if not uses:
                violations.add(
                    f"{source_name}:{helper.lineno}: recovery marker is orphaned; "
                    "the intentional absorption boundary lost its code marker"
                )
            for use in uses:
                boundary = (
                    use.args[1].value
                    if len(use.args) >= 2
                    and isinstance(use.args[1], ast.Constant)
                    and isinstance(use.args[1].value, str)
                    else None
                )
                if boundary is None or not recovery_reasons.get(boundary, "").strip():
                    violations.add(
                        f"{source_name}:{use.lineno}: recovery marker requires a registered "
                        "non-empty boundary reason"
                    )

    return FailSoftReport(
        tuple(sorted(boundary_map.values(), key=lambda item: (
            item.source, item.function, item.line,
        ))),
        tuple(sorted(violations)),
    )


def _canonical_sources(tools: Path = TOOLS) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(tools.glob("*.py"), key=lambda item: item.name)
    }


def test_no_failsoft_boundary_silently_absorbs_marked_engine_skew():
    report = collect_failsoft_report(_canonical_sources())
    assert report.boundaries, "scanner found no marked-skew boundaries"
    assert len(report.boundaries) == 141, "propagation sweep boundary ratchet changed"
    assert not report.violations, "\n".join(report.violations)


def _mutate_one_reraise(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        marker = _marker_call(node, "_is_engine_rev_skew")
        if marker is None:
            continue
        for branch in ast.walk(node):
            if isinstance(branch, ast.If) and branch.test is marker:
                raises = [stmt for stmt in branch.body if isinstance(stmt, ast.Raise)]
                if raises:
                    branch.body = [ast.copy_location(ast.Pass(), raises[0])]
                    ast.fix_missing_locations(tree)
                    return ast.unparse(tree) + "\n"
    raise AssertionError("no skew re-raise found to mutate")


def test_guard_turns_red_when_one_reraise_is_removed():
    sources = _canonical_sources()
    sources["pm_import.py"] = _mutate_one_reraise(sources["pm_import.py"])
    report = collect_failsoft_report(sources)
    assert any("pm_import.py" in violation for violation in report.violations)


def test_guard_turns_red_when_recovery_marker_is_removed():
    sources = _canonical_sources()
    tree = ast.parse(sources["pm_update.py"])
    call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == _RECOVERY_MARKER
    )
    call.func = ast.copy_location(ast.Name(id="_is_engine_rev_skew", ctx=ast.Load()), call.func)
    call.args = call.args[:1]
    sources["pm_update.py"] = ast.unparse(ast.fix_missing_locations(tree)) + "\n"
    report = collect_failsoft_report(sources)
    assert any("pm_update.py" in violation for violation in report.violations)


def test_guard_excludes_shipped_snapshot_tree(tmp_path):
    canonical = tmp_path / ".project_manager" / "tools"
    canonical.mkdir(parents=True)
    (canonical / "good.py").write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = tmp_path / "templates" / "sample" / ".project_manager" / "tools"
    snapshot.mkdir(parents=True)
    (snapshot / "bad.py").write_text("this is not valid python !\n", encoding="utf-8")
    assert _canonical_sources(canonical) == {"good.py": "VALUE = 1\n"}


def _scope_handler(
    source: str, function: str, occurrence: int = 0,
) -> tuple[ast.Module, ast.ExceptHandler]:
    tree = ast.parse(source)
    scope = dict(_scope_nodes(tree))[function]
    visitor = _OwnScopeVisitor(scope)
    visitor.visit(scope)
    handlers = sorted(
        (
            handler for try_node in visitor.tries for handler in try_node.handlers
            if _marker_call(handler, "_is_engine_rev_skew") is not None
        ),
        key=lambda handler: handler.lineno,
    )
    return tree, handlers[occurrence]


def _remove_marker_branch(source: str, function: str, occurrence: int = 0) -> str:
    tree, handler = _scope_handler(source, function, occurrence)
    marker = _marker_call(handler, "_is_engine_rev_skew")
    branch = next(
        node for node in ast.walk(handler)
        if isinstance(node, ast.If) and node.test is marker
    )
    branch.body = [ast.copy_location(ast.Pass(), branch.body[0])]
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


def _blank_recovery_reason(source: str) -> str:
    tree = ast.parse(source)
    assignment = next(
        node for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == _RECOVERY_REASONS
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    )
    assert isinstance(assignment.value, ast.Dict)
    assignment.value.values[0] = ast.copy_location(ast.Constant(value=""), assignment.value.values[0])
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


@pytest.mark.parametrize(
    ("label", "source_name", "function", "occurrence"),
    [
        ("S1", "pm_import.py", "_load_repo_owned_files", 0),
        ("S2", "pm_bootstrap.py", "PmBootstrap._slot_scope_fetched", 0),
        ("S3", "pm_handoff.py", "_regression_cwd", 0),
        ("S4", "pm_config.py", "cmd_repo_protected", 0),
        ("S5", "pm_bootstrap.py", "main", 0),
    ],
)
def test_guard_sensitivity_marker_branches_turn_red(
    label, source_name, function, occurrence,
):
    sources = _canonical_sources()
    sources[source_name] = _remove_marker_branch(
        sources[source_name], function, occurrence,
    )
    report = collect_failsoft_report(sources)
    assert report.violations, f"{label} mutation stayed green"


def test_guard_sensitivity_s6_recovery_marker_turns_red():
    sources = _canonical_sources()
    tree = ast.parse(sources["pm_update.py"])
    call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == _RECOVERY_MARKER
    )
    call.func = ast.copy_location(ast.Name(id="_is_engine_rev_skew", ctx=ast.Load()), call.func)
    call.args = call.args[:1]
    sources["pm_update.py"] = ast.unparse(ast.fix_missing_locations(tree)) + "\n"
    assert collect_failsoft_report(sources).violations


def test_guard_sensitivity_s7_blank_recovery_reason_turns_red():
    sources = _canonical_sources()
    sources["pm_update.py"] = _blank_recovery_reason(sources["pm_update.py"])
    assert collect_failsoft_report(sources).violations


@pytest.mark.parametrize(
    ("source_name", "function", "occurrence"),
    [
        ("delegate_scope.py", "_load_repo_coordinates", 0),
        ("delegate_scope.py", "ticket_touches", 0),
        ("delegate_scope.py", "ticket_touches", 1),
        ("domain.py", "_load_repo_owned_files", 0),
        ("pm_import.py", "_load_repo_owned_files", 0),
        ("worktree_pool.py", "_load_repo_owned_files", 0),
        ("worktree_pool.py", "switch", 0),
    ],
)
def test_guard_sensitivity_s1b_each_transforming_handler_turns_red(
    source_name, function, occurrence,
):
    sources = _canonical_sources()
    sources[source_name] = _remove_marker_branch(
        sources[source_name], function, occurrence,
    )
    report = collect_failsoft_report(sources)
    assert any(source_name in violation for violation in report.violations)


@pytest.mark.parametrize(
    ("source_name", "function"),
    [
        ("pm_bootstrap.py", "PmBootstrap._phase0_main_reference_reason"),
        ("ticket_finish.py", "affected_domain_titles.runner_for_page"),
        ("worktree_pool.py", "_cmd_switch"),
    ],
)
def test_guard_sensitivity_argument_closure_and_runtime_subclass_axes_turn_red(
    source_name, function,
):
    sources = _canonical_sources()
    sources[source_name] = _remove_marker_branch(sources[source_name], function)
    report = collect_failsoft_report(sources)
    assert any(source_name in violation for violation in report.violations)


_SYNTHETIC_LEAF = """\
def risky():
    _verify_engine_rev(None, "leaf.py")
"""


def test_scanner_parameter_axis_has_an_independent_synthetic_fixture():
    fn = "consume"
    caller = """\
def _load_leaf():
    return _load_module("leaf.py")

def consume(mod):
    try:
        mod.risky()
    except Exception:
        return None

def entry():
    consume(_load_leaf())
"""
    report = collect_failsoft_report({"leaf.py": _SYNTHETIC_LEAF, "caller.py": caller})
    assert any("caller.py" in violation and f": {fn} " in violation
               for violation in report.violations)


def test_scanner_freevar_module_axis_has_an_independent_synthetic_fixture():
    fn = "outer.inner"
    caller = """\
def _load_leaf():
    return _load_module("leaf.py")

def outer():
    mod = _load_leaf()
    def inner():
        try:
            mod.risky()
        except Exception:
            return None
    inner()
"""
    report = collect_failsoft_report({"leaf.py": _SYNTHETIC_LEAF, "caller.py": caller})
    assert any("caller.py" in violation and f": {fn} " in violation
               for violation in report.violations)


def test_scanner_runtime_subclass_axis_has_an_independent_synthetic_fixture():
    fn = "consume"
    caller = """\
class Wrapped(RuntimeError):
    pass

def transform():
    try:
        _verify_engine_rev(None, "caller.py")
    except Exception as exc:
        marked = Wrapped("skew")
        if _is_engine_rev_skew(exc):
            marked._engine_rev_skew = True
        raise marked

def consume():
    try:
        transform()
    except Wrapped:
        return None
"""
    report = collect_failsoft_report({"caller.py": caller})
    assert any("caller.py" in violation and f": {fn} " in violation
               for violation in report.violations)


def test_scanner_closure_callable_axis_has_an_independent_synthetic_fixture():
    fn = "outer.inner"
    caller = """\
def _load_leaf():
    return _load_module("leaf.py")

def outer():
    mod = _load_leaf()
    fn = getattr(mod, "risky", None)
    def inner():
        try:
            fn()
        except Exception:
            return None
    inner()
"""
    report = collect_failsoft_report({"leaf.py": _SYNTHETIC_LEAF, "caller.py": caller})
    assert any("caller.py" in violation and f": {fn} " in violation
               for violation in report.violations)


def test_scanner_ifexp_boolop_callable_axis_has_an_independent_synthetic_fixture():
    fn = "consume"
    caller = """\
def _load_leaf():
    return _load_module("leaf.py")

def consume():
    mod = _load_leaf()
    fn = (getattr(mod, "risky", None) if mod else None) or None
    try:
        fn()
    except Exception:
        return None
"""
    report = collect_failsoft_report({"leaf.py": _SYNTHETIC_LEAF, "caller.py": caller})
    assert any("caller.py" in violation and f": {fn} " in violation
               for violation in report.violations)


def test_scanner_rejects_unmarked_stderr_nonzero_helper_as_terminal():
    caller = """\
import sys

def risky():
    _verify_engine_rev(None, "caller.py")

def helper():
    try:
        risky()
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            print("skew", file=sys.stderr)
            return 1

def caller():
    helper()
"""
    report = collect_failsoft_report({"caller.py": caller})
    assert any(": helper " in violation for violation in report.violations)


@pytest.mark.parametrize(
    ("source_name", "function"),
    [
        ("ticket_finish.py", "TicketFinisher._notify_affected_domain"),
        ("pm_config.py", "cmd_status._slot_git_line"),
    ],
)
def test_guard_sensitivity_new_caller_boundaries_turn_red(source_name, function):
    sources = _canonical_sources()
    sources[source_name] = _remove_marker_branch(sources[source_name], function)
    report = collect_failsoft_report(sources)
    assert any(source_name in violation and function in violation
               for violation in report.violations)


def _runtime_tool(filename: str):
    path = TOOLS / filename
    module_name = f"_failsoft_runtime_{path.stem}_{id(path)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _marked_skew() -> RuntimeError:
    exc = RuntimeError("injected marked engine skew")
    exc._engine_rev_skew = True
    return exc


def _raiser(exc):
    def raise_it(*_args, **_kwargs):
        raise exc
    return raise_it


@pytest.mark.parametrize(
    "case",
    [
        "bootstrap_same_method",
        "handoff_auto_slot",
        "handoff_session_slot",
        "finish_regression_cwd",
        "board_domain_pages",
        "board_domain_lint",
    ],
)
def test_runtime_loaded_sibling_and_same_method_boundaries_rethrow_marked_skew(
    case, monkeypatch,
):
    skew = _marked_skew()
    if case == "bootstrap_same_method":
        module = _runtime_tool("pm_bootstrap.py")
        instance = object.__new__(module.PmBootstrap)
        instance._bound_slot = None
        instance._worktree_cwd = _raiser(skew)
        invoke = lambda: instance._slot_scope_fetched([])
    elif case == "handoff_auto_slot":
        module = _runtime_tool("pm_handoff.py")
        monkeypatch.setattr(
            module, "_load_pm_bootstrap",
            lambda: SimpleNamespace(_auto_slot=_raiser(skew)),
        )
        invoke = lambda: module._regression_cwd()
    elif case == "handoff_session_slot":
        module = _runtime_tool("pm_handoff.py")
        monkeypatch.setattr(
            module, "_load_pm_bootstrap",
            lambda: SimpleNamespace(
                SlotResolutionError=type("SlotResolutionError", (RuntimeError,), {}),
                _resolve_session_slot=_raiser(skew),
            ),
        )
        invoke = lambda: module._resolve_session_worktree_slot()
    elif case == "finish_regression_cwd":
        module = _runtime_tool("ticket_finish.py")
        monkeypatch.setattr(
            module, "_load_pm_handoff",
            lambda: SimpleNamespace(_regression_cwd=_raiser(skew)),
        )
        invoke = lambda: module._regression_cwd()
    else:
        module = _runtime_tool("board.py")
        domain = SimpleNamespace(DOMAIN_DIR=Path("domain"), load_pages=_raiser(skew))
        if case == "board_domain_lint":
            domain.lint_pages = _raiser(skew)
        monkeypatch.setattr(module, "_load_domain_module", lambda: domain)
        invoke = (
            (lambda: module.lint_domain_freshness())
            if case == "board_domain_pages" else (lambda: module.lint_domain())
        )
    with pytest.raises(RuntimeError) as raised:
        invoke()
    assert raised.value is skew


def test_runtime_module_argument_boundary_rethrows_marked_skew():
    module = _runtime_tool("pm_bootstrap.py")
    instance = object.__new__(module.PmBootstrap)
    skew = _marked_skew()
    worktree_pool = SimpleNamespace(current_branch=_raiser(skew))
    with pytest.raises(RuntimeError) as raised:
        instance._phase0_main_reference_reason(worktree_pool, "repo", "work/repo_1")
    assert raised.value is skew


def test_runtime_closure_module_alias_boundary_rethrows_marked_skew(monkeypatch):
    module = _runtime_tool("ticket_finish.py")
    skew = _marked_skew()
    page = {"title": "P1"}
    domain = SimpleNamespace(
        load_pages=lambda: [page],
        pages_for_touches=lambda _touches, pages: pages,
        page_stale=lambda _page, *, git_runner: git_runner is None,
        _real_git_runner=_raiser(skew),
    )
    monkeypatch.setattr(module, "_load_domain_module", lambda: domain)
    monkeypatch.setattr(module, "get_ticket_touches", lambda *_args: ["src/file.py"])
    monkeypatch.setattr(module, "load_board_module", lambda *_args: None)
    with pytest.raises(RuntimeError) as raised:
        module.affected_domain_titles("ticket", Path("board.py"))
    assert raised.value is skew


def test_runtime_affected_domain_caller_rethrows_marked_skew(monkeypatch):
    module = _runtime_tool("ticket_finish.py")
    skew = _marked_skew()
    domain = SimpleNamespace(
        load_pages=_raiser(skew),
        pages_for_touches=lambda _touches, pages: pages,
    )
    monkeypatch.setattr(module, "_load_domain_module", lambda: domain)
    monkeypatch.setattr(module, "get_ticket_touches", lambda *_args: ["src/file.py"])
    instance = object.__new__(module.TicketFinisher)
    instance._board_py = Path("board.py")
    instance._affected_domain_fn = instance._default_affected_domain
    with pytest.raises(RuntimeError) as raised:
        instance._notify_affected_domain("ticket")
    assert raised.value is skew


def test_template_mirror_missing_pm_update_render_capability_is_drift(
    monkeypatch, tmp_path,
):
    module = _runtime_tool("board.py")
    source = tmp_path / "source" / "SKILL.md"
    target_root = tmp_path / "templates" / "legacy"
    target = target_root / ".claude" / "skills" / "demo" / "SKILL.md"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text("same\n", encoding="utf-8")
    target.write_text("same\n", encoding="utf-8")
    monkeypatch.setattr(
        module, "_load_pm_update_module", lambda: SimpleNamespace(),
    )

    state, drifted = module._template_mirror_report(
        source,
        ".claude/skills/demo/SKILL.md",
        [(target_root, {".claude/skills"})],
    )

    assert state == module._TEMPLATE_MIRROR_DIFFERS
    assert drifted == ["legacy"]


def test_runtime_status_slot_git_callable_rethrows_marked_skew(monkeypatch):
    module = _runtime_tool("pm_config.py")
    skew = _marked_skew()
    lease = SimpleNamespace(
        slot="work/repo_1", repo="repo", state="idle", session=None, pid=0, role="work",
    )
    worktree_pool = SimpleNamespace(
        list_leases=lambda: [lease],
        current_branch=lambda _slot: "feature",
        list_tasks=lambda: [],
        slots_for_task=lambda _task: [],
        slot_git_status=_raiser(skew),
    )
    monkeypatch.setattr(module, "_default_session", lambda: "session")
    monkeypatch.setattr(module, "_default_user", lambda: "user")
    monkeypatch.setattr(module, "_distinct_area_owners", lambda: 1)
    with pytest.raises(RuntimeError) as raised:
        module.cmd_status(SimpleNamespace(command="status"), worktree_pool=worktree_pool)
    assert raised.value is skew


def test_runtime_repository_exception_catch_rethrows_marked_skew(monkeypatch):
    module = _runtime_tool("worktree_pool.py")
    skew = module.SwitchRefused("work/repo_1", "record-failed")
    skew._engine_rev_skew = True
    monkeypatch.setattr(module, "_normalize_slot", lambda slot: slot)
    monkeypatch.setattr(module, "switch", _raiser(skew))
    args = SimpleNamespace(slot="work/repo_1", branch="feature")
    with pytest.raises(module.SwitchRefused) as raised:
        module._cmd_switch(args)
    assert raised.value is skew


@pytest.mark.parametrize(
    ("filename", "arguments"),
    [
        ("board.py", ["list"]),
        ("pm_adr.py", ["new", "--title", "title", "--slug", "slug", "--dry-run"]),
        ("pm_bootstrap.py", ["--help"]),
        ("pm_handoff.py", []),
        ("ticket_finish.py", []),
        ("pm_config.py", ["status"]),
        ("worktree_pool.py", ["--help"]),
    ],
)
def test_entrypoint_translates_marked_skew_to_korean_guidance_and_rc(
    filename, arguments, tmp_path,
):
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, tools / source.name)
    identity = tools / "identity_args.py"
    mutated, replacements = re.subn(
        r'^ENGINE_REV = "[^"]+"$',
        'ENGINE_REV = "stale-cli-copy"',
        identity.read_text(encoding="utf-8"),
        count=1,
        flags=re.MULTILINE,
    )
    assert replacements == 1
    identity.write_text(mutated, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(tools / filename), *arguments],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stderr.startswith("[중단] 엔진 사본 불일치")
    assert "pm-update" in completed.stderr
    assert "Traceback" not in completed.stderr
