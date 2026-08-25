"""Repository-wide guard for fail-soft boundaries around marked engine skew.

The scanner follows top-level functions, same-class methods, loader-proven module aliases passed
through parameters or lexical free variables, literal-name ``getattr`` callable aliases through
conditional/boolean expressions and lexical closures, and same-class callable instance attributes
(``self.<attr> = <callable>`` retained for a later ``self.<attr>(...)`` call).  Catch recognition
covers builtin ``RuntimeError``/``Exception``/``BaseException`` plus repository ``RuntimeError``
subclasses that are constructed by a marker-preserving transform.  Handlers that consume
``_is_engine_rev_skew`` directly remain explicit roots as a defense against incomplete provenance.

Deliberate exclusions include dynamic ``getattr`` member names, arbitrary duck-typed objects with
same-named methods, and invocation links to nested definitions (their own bodies are still
scanned).  Two exclusions on the instance-attribute axis are worth naming because they look linked
but are not:

* ``setattr(self, "<attr>", <callable>)`` — the retained callable is only recognised through an
  assignment statement (``self.<attr> = <callable>``), so a dynamic write leaves the later
  ``self.<attr>(...)`` call unlinked (same rule as dynamic ``getattr`` member names).
* inheritance — retained callables are keyed by the *lexically enclosing* class, with no MRO
  resolution.  A subclass storing ``self.<attr>`` while the base class calls ``self.<attr>()``
  stays unlinked in both directions.

Both classes still hold whenever the handler itself consumes ``_is_engine_rev_skew`` (explicit
root), which is why that root exists.

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

from _textio import utf8_child_env


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
    # Callables retained on an instance attribute (``self.<attr> = <callable>``), keyed by
    # ``(source, "Class.attr")`` so a later ``self.<attr>(...)`` call reaches the bound function.
    class_callable_attrs: dict[tuple[str, str], set[tuple[str, str]]] = {}
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
        source_name, qualname = key
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
        if isinstance(expr, ast.Attribute):
            # ``self.<method>`` bound-method reference and ``self.<attr>`` retained callable.  A
            # non-``self`` receiver stays unlinked (same provenance rule as duck-typed objects).
            if not (isinstance(expr.value, ast.Name) and expr.value.id in {"self", "cls"}):
                return set()
            prefix = _class_prefix(qualname)
            if prefix is None:
                return set()
            member = (source_name, f"{prefix}.{expr.attr}")
            resolved = set(class_callable_attrs.get(member, ()))
            if member in scope_data:
                resolved.add(member)
            return resolved
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
                        if isinstance(target, ast.Name):
                            call_bucket = callable_aliases[key].setdefault(target.id, set())
                        elif (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id in {"self", "cls"}
                            and _class_prefix(qualname) is not None
                        ):
                            call_bucket = class_callable_attrs.setdefault(
                                (source_name, f"{_class_prefix(qualname)}.{target.attr}"), set()
                            )
                        else:
                            continue
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
            found.update(class_callable_attrs.get((source_name, method), ()))
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
    # 158 = 155 + T-0591 완료 게이트의 원장 기록·쓰기 후 재판정·pm_config 판정 unavailable
    #   세 경계. 모두 마킹된 skew 는 re-raise 하고 그 밖만 rc1 결과로 번역한다.
    # 160 = 158 + T-0590 스폰 전 롤백의 두 경계. `run_review` 의 스폰 전 구간은 주 예외를 그대로
    #   re-raise 하고(환불 판정은 호출부 seam 이 소유), 그 정리 경로
    #   (`_abort_pre_spawn_raw` 의 raw 장부 마감)만 등록된 사유로 흡수한다 — 정리 실패가 중단
    #   사유를 덮으면 이 실행이 왜 죽었는지가 사라지기 때문이다.
    # 162 = 160 + T-0590 local.conf 공유락의 두 경계. pm_import 는 일반 형제 모듈 손상을
    #   무락으로 복구하되 마킹된 엔진 skew 를 다시 올리고, pm_config 도 그 skew 를 사용자
    #   입력 RuntimeError 로 번역하지 않고 그대로 전파한다.
    # 163 = 162 + T-0590 R6 부분 업그레이드 호환의 한 경계. pm_import 의 락 경로 유도가
    #   `conf_lock_path` 없는 구세대 file_lock 사본에서 같은 규칙의 인라인 폴백으로 물러나되,
    #   마킹된 skew(rev 자체가 다른 사본)는 삼키지 않고 그대로 올린다.
    # 164 = 163 + T-0593 diff 서킷브레이커의 한 경계. ticket_finish 가 external_review 를
    #   형제 로드해 상한 정책·측정식을 빌려 쓰되, 부재/손상은 가드 off 로 물러나고 마킹된
    #   skew 는 그대로 올린다(다른 형제 로더와 같은 규칙).
    # 166 = 164 + T-0595 위임 원장 보강의 두 경계. 회신 구조화 관측과 must_fix 항목 추출은
    #   감사 보강이라 실패해도 위임을 죽이지 않고 필드 부재로 물러나되(회수 fail-loud 는 종전
    #   소유자 몫), 마킹된 skew 는 삼키지 않는다 — 사본이 갈린 사실이 원장 누락으로 위장되면 안 된다.
    # 169 = 166 + T-0596 핸드오프 미마감 raw sweep 의 세 경계(pm_relay 형제 로더 + 장부 위치
    #   해소 + 미마감 조회). sweep 은 비차단 표면이라 부재·손상·조회 실패를 사유 1줄로 접지만,
    #   조회는 deep 형제(`file_lock`)까지 들어가므로 마킹된 skew 만은 그대로 올린다 — 부분 복사
    #   사실이 "미마감 raw 조회 실패" 한 줄에 묻히면 안 된다.
    # 172 = 169 + T-0600 사본 장부 병기의 세 경계(pm_delegate 형제 로더 + 사본 판정 + 사본 미마감
    #   조회). 병기는 가시성 보조라 부재·손상을 사유 1줄로 접지만, 판정을 빌려 오는 대상이 형제
    #   엔진 사본이므로 마킹된 skew 는 같은 규칙으로 그대로 올린다.
    # 173 = 172 + T-0601 DoD preflight 의 한 경계. 완료 기록이 board 의 DoD 판정을 형제 로드해
    #   **log 스켈레톤 append 앞에서** 한 번 더 묻되, 티켓 부재/손상은 preflight 를 조용히 끄고
    #   (권위 있는 게이트가 뒤에 있다) 마킹된 skew 는 그대로 올린다.
    # 174 = 173 + T-0602 estimate 해석 단일화의 한 경계. 리뷰쪽 estimate 조회가 board 의
    #   frontmatter 로더를 형제 로드해 완료 게이트와 **같은 값**을 읽되, 티켓 부재/손상은 상한
    #   가드를 조용히 끄고(엔진이 상한을 지어내지 않는다) 마킹된 skew 는 그대로 올린다.
    # 173 = 174 − T-0603 티켓 조회 seam 통일로 **사라진** 한 경계. `get_ticket_title` 이 자체
    #   board 로드를 두지 않고 `_ticket_frontmatter` 한 지점을 쓰므로(제목과 게이트 입력이 같은
    #   파일을 본다) 그 함수의 경계가 통째로 없어졌다 — 흡수 규칙이 느슨해진 게 아니라 경계가
    #   하나로 합쳐진 것이고, 남은 지점은 종전대로 마킹된 skew 를 re-raise 한다.
    # 175 = 173 + T-0606 훅 세트 세대 정합의 두 경계. 판정 채널(`check_adapter_hook_sets`)은
    #   형제 pm_import 판정을 빌려 쓰되 부재/손상은 unavailable 경고로 접고 마킹된 skew 는 그대로
    #   올린다(형제 config 채널과 같은 규칙). 반면 원자 write 판정자 해소
    #   (`resolve_hook_set_predicate`)는 **상류/형제 pm_import 사본**을 읽어 apply 에 넘기는
    #   지점이라 등록된 복구 경계로 흡수한다 — 그 사본의 rev 가 실행 중 엔진과 갈리는 것이
    #   업그레이드의 정상 경로이고, 여기서 올리면 엔진이 반쯤 적용된 채 죽는다. 후보를 모두
    #   잃으면 copy2 폴백(종전 동작)을 loud 로 알린다.
    # 176 = 175 + T-0606 경로 스코프 반쪽 갱신 가드의 한 경계. `refuse_partial_hook_set_scope` 도
    #   같은 형제 선언을 빌려 판정하되, 판정 채널 부재/손상은 가드를 끄고(복구 전파가 자기잠금하면
    #   안 된다) 마킹된 skew 는 그대로 올린다 — 다른 형제 로더와 같은 규칙이다.
    # 178 = 176 + T-0607 동기 실행 중 rev 혼합 흡수의 **새** 두 경계(어댑터 config 한 파일 수용 ·
    #   upstream_rev baseline 기록). 둘 다 apply 이후에 형제 pm_import → 락/원장 seam 까지 들어가는
    #   자리라, 실행 중 혼합(정상 과도 상태)을 올리면 이미 착지한 엔진 파일 위에서 동기가 죽는다.
    #   기존 여섯 경계(`_installed_entry_notation_manifests` · `sync_adapter_configs` 판정/원장 ·
    #   `check_adapter_hook_sets` · `refuse_partial_hook_set_scope`)는 re-raise 에서 등록된 흡수로
    #   **처분만** 바뀌어 개수가 그대로다. 흡수의 짝인 종료 시 수렴 검증은 pm_update 가 소유한다.
    # 179 = 178 + T-0611 원자 write 판정자의 **구세대 형제 강등** 한 경계. 선언 해소가 실패해도
    #   그 세대가 이미 제공하던 판정(`is_live_hook_set_path` 단일 인자)을 살리려 형제 사본을 한 번
    #   더 로드하는데, 그 로드는 혼합 트리에서 정확히 발화하는 지점이라 등록된 사유로 흡수한다 —
    #   여기서 올리면 강등 사다리를 얹으려다 동기 자체를 죽인다(판정자만 무판정으로 내려간다).
    # 181 = 179 + T-0617 인스턴스 소유 template 세대 요약의 두 경계. pm-update 동기 경계는 실행
    #   시작의 구 pm_import 사본이 낸 marked skew를 등록된 복구 사유로 흡수하고 종료 시 수렴을
    #   검증한다. 독립 `sync-adapter-config --check` 경계는 복구 실행이 아니므로 같은 skew를 그대로
    #   re-raise한다. 일반 판독 실패만 전량 확인 advisory로 내린다.
    # 183 = 181 + T-0625 raw close 수동 마감 충돌 경계 둘. 원 실행(pm_delegate
    #   `_execute_attempt` · external_review run_review)의 종료 마감이 수동 `raw close --force`
    #   선행 마감과 충돌하면 전용 타입(`RawRecordAlreadyFinished`)만 잡아 경고로 강등한다 —
    #   첫 마감 보존이 계약이고 회신은 raw 파일에 이미 박제돼 있어 rc 를 뒤집지 않는다.
    #   marked skew 는 그 타입이 아니므로 이 경계가 흡수하지 않는다.
    # 184 = 183 + T-0633 delegate_channel_guard 훅 fail-open 경계 하나. PreToolUse 훅은 가드
    #   자신의 고장(사본 skew 포함)으로 정상 위임을 막지 않는 것이 계약이라, 등록 사유
    #   (`hook_fail_open`) 기반 recovery marker 로 흡수하고 skew 진단(pm-update 처방)은
    #   stderr 로 남긴다. 여기서 fail-loud 로 올리면 부분 동기 하나가 모든 Agent 호출을 막는다.
    # 186 = 184 + T-0635 opencode sandbox 내부 prompt-file의 정리 경계 둘. transport 준비 뒤
    #   argv 조립 실패와 raw 예약/장부 시작 실패 모두 전달 사본을 지운 뒤 **같은 주 예외를
    #   다시 올린다**. 일반 실패나 marked skew를 흡수하지 않는 정리 전용 경계다.
    # 193 = 186 + 이번 wave 신설 경계 일곱. 라운드 장부 분리(review_rounds seam)·게이트 스냅샷
    #   앵커·diff 귀속 스냅샷·codex 관측 append 가 각자 정리/기록 전용 경계를 두며, 관측 두
    #   경계는 등록 사유(`observation_append_fail_open`) 기반 recovery marker 로 흡수한다 —
    #   관측 append 는 기록일 뿐이라 장부 쓰기 실패로 이미 내려진 allow/deny 를 뒤집지 않고,
    #   대신 matcher drift 관측이 불완전하다는 경고를 결과 envelope 에 실어 표면화한다.
    # 194 = 193 + T-0676 cross ticket harvest 후처리 한 경계. 단독 marked engine skew는
    # 즉시 재전파하고, runner 원예외가 이미 pending이면 그 원예외를 보존하면서 skew도 진단한다.
    # 195 = 194 + T-0677 PM-direct finish advisory 한 경계. git 변경 조회 실패는
    # never-block 경고로 내리되 marked engine skew는 종전 불변식대로 재전파한다.
    # 196 = 195 + T-0677 directory touches repo-owned 전개 한 경계. 일반 열거
    # 실패는 미해소 상향 신호로 보존하고 marked skew는 재전파한다.
    # 197 = 196 + T-0677 h1/h2/docs-only 공유 OWNED 스냅샷 해소 한 경계.
    # 일반 열거 실패는 unresolved로 내리되 marked skew는 재전파한다.
    # 198 = 197 + growth-seal 전역 lint advisory 한 경계. active ticket 한 건의 읽기·문법
    # 실패나 optional delegate 로드 실패는 visibility-only/never-block 축을 생략해 board 조회를
    # 보존하되, marked engine skew는 부분 동기를 숨기지 않고 그대로 재전파한다.
    # 199 = 198 + T-0698 lease 실경로 보강 한 경계. 해소 실패는 canonical `work/<name>`으로
    # 폴백하되, marked engine skew는 부분 동기를 숨기지 않고 그대로 재전파한다.
    # 209 = 199 + v1.7.6 Windows 이식의 열 경계. 종전 POSIX 전용 원시 호출(`shutil.rmtree(dir_fd=)`
    # · `os.chmod` · `fcntl`)이 Windows 등가 수단으로 갈리면서, 그 수단을 **형제 모듈**
    # (`file_lock.force_rmtree`·`restrict_to_owner`)이 갖게 돼 정리·생성 경로가 새로 사본 불일치
    # 표면을 얻었다. 내역:
    #   · `pm_delegate._create_read_role_temp_owner_acl` 두 경계 — ACL 제한 실패는 격리 미성립이라
    #     재-raise 하고, 조회 실패만 흡수한다.
    #   · `pm_delegate._portable_exclusive_write` · `_save_opencode_transport_prompt` 재-raise 두 경계.
    #   · `pm_delegate._create_read_role_temp` · `_cleanup_read_role_temp` 흡수 두 경계 — 둘 다
    #     `_ENGINE_REV_SKEW_RECOVERY_REASONS` 에 사유를 등록하고 경고 문구로 원인을 구분한다
    #     (정리 실패가 성공한 실행을 뒤집지 않는다는 계약).
    #   · `external_review.create_reviewer_workspace` 재-raise · `_remove_partial_container` 흡수.
    #   · `pm_config._protected_push_gate_config` 재-raise 한 경계.
    #   · `delegate_channel_guard._record_supervisor_fallback` 흡수 한 경계 — PowerShell 인용
    #     삼킴으로 래퍼가 폴백할 때 그 사실 기록이 판정을 막지 않는다.
    # 210 = 209 + T-0728 처방 인터프리터 해소의 흡수 한 경계
    #   (`delegate_channel_guard._prescribed_interpreter`). 처방 표기는 deny 판정의 부속이라
    #   형제 로드 실패(skew 포함)로 엔벨로프 키가 무너지면 안 된다 — 등록 사유
    #   `prescription_interpreter_fail_open` 으로 흡수하되 stderr 로 원인을 구분해 남긴다.
    # 212 = 210 + T-0729 원자 교체 seam 의 **등재된 부트스트랩 예외** 두 경계. 두 곳 모두 교체
    #   프리미티브를 형제 `file_lock` 에서 가져오는데, 그 형제가 없거나 손상인 트리가 정확히
    #   이 두 경로의 정상 입력이다(§결정 · 선택지 A).
    #   · `pm_update._atomic_replace_or_degrade` 흡수 한 경계 — `_predeploy_central_loader` 의
    #     부트스트랩 쓰기가 여기를 지나므로 이 쓰기는 **그 형제를 설치하는 쪽**이다. 등록 사유
    #     `atomic_copy_replace_seam` 으로 흡수하고 강등을 stderr 로 남긴다(여기서 올리면 중단된
    #     업데이트를 채택자가 스스로 못 고친다).
    #   · `pm_import._atomic_replace_conf` 재-raise 한 경계 — 부재/손상 사본은 무락 복구 계약대로
    #     loud 강등으로 흡수하되, rev 자체가 갈린 사본은 조용한 오작동이 아니라 재동기 안내로
    #     표출해야 하므로 종전 규칙대로 그대로 올린다.
    # 213 = 212 + T-0730 회귀 게이트 해소 위임의 한 경계. `ticket_finish._resolve_per_repo_test_cmd`
    #   는 해소 체인 사본을 두지 않고 형제 `pm_handoff._resolve_gate_cmd` 에 위임한다(체인 단일
    #   사본 — 해소 함수가 한쪽에만 있던 미러 이탈이 무prefix 채택자 결함의 절반이었다). 그
    #   형제가 없거나 손상이면 게이트를 해소하지 못해도 솔로 `pytest tests/ -q` 폴백으로 완주해야
    #   하므로 흡수하되(`_regression_cwd`·`_resolve_finish_slot` 의 같은 위임과 동형), 마킹된
    #   skew 는 그대로 올린다 — 사본이 갈린 사실이 "게이트 미해소"로 위장되면 안 된다.
    # 218 = 213 + T-0729 **공유 읽기 강등**의 다섯 경계(`_shared_read_api` — external_review ·
    #   pm_import · pm_log · pm_update · review_rounds). 판독이 공용 seam 을 지나게 되면서 그
    #   형제 로드가 판독 경로에 닿는데, 이 다섯은 "판독은 형제 없이도 떠야 한다" 를 각자 로더
    #   주석에 명시한 채널이다(복구/도입 채널 둘 · pm_bootstrap 이 재사용하는 로그 판독 ·
    #   부분 동기 트리에서도 살아야 하는 라운드 판정 · `--gate` 밖 진단 경로). 판독은 아무것도
    #   커밋하지 않고 종전 읽기와 바이트가 같아 흡수 비용이 "Windows 에서 그 판독 중의 원자 교체
    #   한 번" 뿐이고, 올리면 복구 채널 자신이 막힌다. 다섯 모두 등록 사유 `shared_read_seam` +
    #   프로세스당 1회 stderr 강등 알림과 짝이다([[T-0729]] §결정 선택지 A 의 판독 쪽 확장).
    # 222 = 218 + T-0729 B조(로더 없던 모듈)의 판독 전환이 만든 재-raise 네 경계. 판독이 공용
    #   seam 을 지나면서 종전에 형제를 안 건드리던 fail-soft 자리가 형제 로드 경로를 얻었다 —
    #   `pm_bootstrap._collect_handoff_context`·`_read_log_text`·`_safe_command_card` 와
    #   `pm_config.cmd_task_end`. 넷 다 그 모듈의 기존 규칙(`_load_tool`·`_load_module` 의
    #   "중첩 로드 형제 skew 는 fail-loud")을 그대로 따라 marked skew 만 재전파하고 나머지는
    #   종전대로 접는다(surface 생략·게이트 graceful skip).
    # 224 = 222 + T-0696 추가 리뷰어 산출 **회수 경계**의 두 자리(`_load_pm_delegate`/board 재앵커
    #   로드 · `_reserve_external_review_round` 쓰기). 회수는 이미 끝나고 과금된 라운드의 기록이라,
    #   여기서 사본 불일치를 그대로 올리면 판정·요약을 출력한 뒤 traceback 으로 죽어 채택자에게는
    #   "리뷰 실패"로만 보인다. 등록 사유 `ticket_harvest` 로 흡수하되 설계된 회수 실패 처방
    #   (재동기 안내 + rc≠0 + raw 경로)으로 접고, 표시 없는 RuntimeError 는 그대로 전파한다.
    # 225 = 224 + T-0753 rounds stage 후보 형제 로더 한 경계(`ticket_finish._load_ticket_rounds`).
    #   `engine_written_paths` 가 라운드 사이드카 경로·임시 파일 규약(`ticket_rounds.py`)을
    #   형제 로드해 stage 후보를 내는데, 그 형제가 부재/구버전이면 round 후보만 생략하고 티켓
    #   파일 stage 는 그대로 진행한다(`_load_repo_coordinates` 동형) — 단 마킹된 skew 는
    #   그대로 올린다(다른 형제 로더와 같은 규칙).
    # 226 = 225 + board lint 라운드 판정의 **티켓 단위** fail-soft 한 경계. 순회 전체를 감싸던
    #   경계가 티켓 하나로 좁혀지면서(한 티켓의 읽기 실패가 그때까지 모은 판정을 통째로 버리지
    #   않는다) 같은 규칙의 경계가 하나 늘었다 — 둘 다 advisory 축이라 흡수하되 마킹된 skew 는
    #   그대로 올리고, 흡수한 티켓은 `round-unreadable` 로 표면에 남는다(조용한 생략 아님).
    # 228 = 226 + T-0738 claim 코드 트리 해소 seam 통일의 두 경계. `board._load_pm_handoff`
    #   (형제 pm_handoff 동적 로드 — `ticket_finish._load_pm_handoff` 동형)와 `board._claim_code_tree`
    #   의 `--repo`/`--slot` 해소 분기(`pm_handoff._resolve_explicit_identity_slot` 위임) 모두
    #   부재/로드 실패는 claimed_rev 미박제(측정 보조 필드 생략)로 접되, 마킹된 skew 는 그대로
    #   올린다 — 형제 사본이 갈린 사실이 "코드 트리 미해소 경고" 한 줄에 묻히면 안 된다.
    # 229 = 228 + 컴팩션 snapshot 진행 중 작업 절의 장부 조회 경계(`pm_log._inflight_section`).
    #   in-process 장부 조회(위임 라운드·raw·claimed·WIP) 실패를 절 1줄로 접는 fail-soft 지만,
    #   마킹된 skew 는 등록 경계(`inflight_ledger_query`)의 복구 마커를 핸들러가 직접 호출해
    #   "엔진 사본 불일치" 라벨로 표면화한다 — 조용한 흡수 아님.
    # 231 = 229 + 단일-등록 유도의 두 `_default_session` 래퍼 경계(worktree_pool·pm_config).
    #   공유 술어(identity_args.single_registration_session) 유도 실패는 미발화로 접어 각 모듈의
    #   기존 tail(<host>-<pid>/None)로 물러나되, 마킹된 엔진 skew 는 그대로 re-raise 한다 —
    #   세 해소 사본 통일 층이 사본 불일치를 tail 폴백 한 줄로 삼키면 안 된다.
    # 230 = 231 − 세션-entry 실행 슬롯 해소가 pm_log 를 형제 로드하던 **한 경계**. 그 경계는
    #   canonical 세션명으로 `pm_log.resolved_lease_slot_path` 를 불러 실 슬롯 경로를 되찾고
    #   실패를 canonical 조립으로 접던 자리였는데, 슬롯 경로가 **장부 행 값**으로 직접 해소되면서
    #   (`pm_bootstrap._session_slot_identity`) 형제 로드 자체가 사라졌다 — 흡수 규칙이 느슨해진 게
    #   아니라 그 사본을 읽을 이유가 없어진 것이고(같은 장부를 두 번 해석하던 중복), 남은 세션-entry
    #   경계들은 종전대로 마킹된 skew 를 re-raise 한다.
    # 231 = 230 − 1(등록 유도가 사라지며 `pm_config._registered_repos_for_session` 소멸)
    #   + 2(엔진 흡수 말미 홈 슬롯 등록 `pm_update.register_home_slot` 의 dest 사본 로드·호출
    #   두 경계). 등록은 파일 동기가 이미 끝난 뒤의 부수 이행이라 실패를 안내 한 줄로 접되,
    #   마킹된 사본 skew 는 두 경계 모두 그대로 re-raise 한다(동기 성공이 skew 를 덮지 않는다).
    # 232 = 231 + T-0776 promote 내용 검토 게이트의 좌표 정규화 형제 로더 한 경계
    #   (`board._load_repo_coordinates`). touches/인용 좌표를 소유 트리 기준으로 접는 규칙이
    #   `repo_coordinates.py` 에 있고 board 는 그 형제를 지연 로드해 소비만 한다(규칙 사본 0) —
    #   부재/손상은 그 축만 판정불능으로 접되(`_load_pm_bootstrap_module` 동형 — 통과로
    #   위장하지 않는다), 마킹된 엔진 skew 는 그대로 re-raise 한다. (pm_delegate.py 는 같은
    #   구간에서 `_recalculate_internal_review_rounds` 인라인 판독을
    #   `_internal_recorded_reply` 로 추출했을 뿐 — 같은 재-raise 경계가 자리만 옮겨 net 0.)
    # 235 = 232 + 위임 라운드 예약을 단일 임계구역으로 모으며 생긴 세 경계
    #   (`pm_delegate._refund_gate_rejected_ticket_copy` recovery-absorb ·
    #   `pm_delegate.prepare_ticket_copy` recovery-absorb·reraises). 예약 실패·거부 뒤 정리는
    #   원 결과를 덮지 않아야 하므로 정리 실패를 복구 좌표와 함께 흡수하되, 마킹된 사본 skew 는
    #   그대로 올린다. 그 변경이 이 래칫을 갱신하지 않아 통합 브랜치에 red 로 남아 있던 것을
    #   여기서 값과 서사로 함께 닫는다.
    # 239 = 235 + local.conf 읽기를 공용 로더 하나로 모으면서 **마커를 새로 얻은** 네 경계
    #   (`board._freshness_owner_repo`·`domain._page_owner_repo`·`pm_bootstrap._current_user`·
    #   `pm_handoff._resolve_gate_cmd`). 넷 다 conf 해소 실패를 "미해소/줄 생략"으로 접던 자리인데,
    #   그 해소가 이제 rev-검증 형제(local_conf)를 지나므로 마킹된 skew 를 접으면 사본 불일치가
    #   폴백 한 줄에 묻힌다 — 흡수는 유지하되 마킹된 skew 만 그대로 올린다.
    # 240 = 239 + 실 conf 관측 조회면(`board.lint_local_conf`). 판독 실패를 관측 0 으로 접는
    #   advisory 지만(조회면이 멈추면 무엇을 고칠지 보여 줄 표면이 사라진다) 마킹된 skew 는 그대로
    #   올린다 — 이 조회도 rev-검증 형제(local_conf)를 지난다.
    # 245 = 240 + 6 − 1. 통합 브랜치에 래칫 갱신 없이 쌓여 있던 경계와, 그중 마킹된 skew 를 조용히
    #   흡수하던 세 자리의 처분 정정을 함께 값으로 닫는다(파일:함수:줄·처분):
    #   + `pm_delegate.py:_reject_cross_role_prepare:12118` reraises — cross 역할 수동 prepare 채널
    #     게이트. 가드 로드/실행 실패는 종전대로 fail-open 통과지만, 형제 사본 불일치는 판정불능이
    #     아니라 엔진 손상이라 그대로 올린다(갈린 사본으로 위임이 계속되면 안 된다).
    #   + `ticket_finish.py:TicketFinisher._default_self_axis_block:2503` reraises — 자기 축 회귀의
    #     baseline materialize. 실패는 "판정 skip" 한 줄로 접되 마킹된 skew 만 올린다(다른 형제
    #     로더와 같은 규칙 — 사본이 갈린 사실이 skip 경고에 묻히면 안 된다).
    #   + `ticket_finish.py:TicketFinisher._home_state_prefixes:2275`·`:2281` reraises 2건 — PM 홈
    #     dev-state 접두 해소가 board·pm_log 형제를 지나며 얻은 두 경계. 해소 실패는 제외 없이
    #     (판정 인구에 남겨 더 엄격한 쪽으로) 접고 마킹된 skew 는 올린다.
    #   + `pm_log.py:_status_dirs:1072` reraises — census 버킷을 board `STATUS_DIRS` 단일 진실에서
    #     승계하며 생긴 경계. 로드 실패는 빈 튜플(소비측 "미해소" 표기)로 접고 skew 는 올린다.
    #   + `pm_update.py:_resolve_engine_sync_plan:4158` reraises / − `pm_update.py:_main` reraises —
    #     같은 경계가 계획 해소 함수로 **자리만 옮겼다**(net 0 · 흡수 규칙 불변).
    #   경계가 **사라진** 두 자리도 여기 남긴다(개수에는 안 잡히므로):
    #   · `pm_update.py:drift_changes` — 마킹된 skew 를 판정 불능(`None`)으로 접던 분기를 지웠다.
    #     `None` 은 호출부에서 "이 형상엔 게이트 미적용(무차단)" 으로 읽히므로, 사본끼리 rev 가 갈린
    #     트리 — 이 게이트가 잡으라고 만들어진 그 형상 — 에서만 릴리즈 drift 게이트가 조용히
    #     통과했다(false-green). 이제 흡수 없이 올라 재동기 안내가 그대로 보인다.
    #   · `pm_principles.py:_write_json` — 기계 출력 한 줄이 형제 로드 실패를 통째로 삼키고 stdout
    #     폴백으로 내려가던 자리. `pm_log._write_machine_line` 과 같은 공용 seam 관용구로 바꿔
    #     경계 자체가 없어졌다(형제 손상은 fail-loud · 부모는 비영 rc 를 이미 fail-open 으로 다룬다).
    # 246 = 245 + 1. 통합 브랜치(T-0761)에 래칫 갱신 없이 쌓여 있던 경계 하나:
    #   + `board.py:lint_local_conf_keys:17850` reraises — local.conf 레지스트리 밖 키 advisory
    #     조회면. 판독 실패(`local_conf.load` 예외)를 빈 목록(관측 0)으로 접는 lint 관용구지만,
    #     이 조회도 rev-검증 형제(`local_conf`)를 지나므로 다른 conf 조회면(`lint_local_conf`)과
    #     같은 규칙으로 마킹된 skew 만 그대로 올린다.
    # 247 = 246 + 1. livegate drift 게이트 seam 이 marked skew 를 판정 불능으로 잘못 흘리던
    #   결함(F-012)을 닫으며 얻은 경계:
    #   + `board.py:_refuse_release_for_engine_drift:8833` terminal-report — 이전엔 `drift_changes`
    #     호출을 감싸는 핸들러가 아예 없어 마킹된 skew 가 무보호 traceback 으로 죽으며 사전 pass
    #     기록을 그대로 남겼다(false-green 잔존). 이제 마킹된 skew 를 판정 불능이 아니라 이 게이트의
    #     가장 강한 확정 양성으로 번역한다 — must-fix 축과 같은 원자 fail 기록
    #     (status=fail·reason=engine-drift·n=0·rc=null, 사전 pass 를 덮어쓴다) 뒤
    #     `_report_engine_rev_skew_at_terminal` 로 rc1 을 반환해 라이브 wave 를 돌리지 않는다.
    #     마킹 안 된 다른 `RuntimeError`(예 `EmptyShippingInventoryError`)는 그 핸들러 밖 `raise` 로
    #     종전대로 전파한다(non-skew 판정 경로 불변).
    # 250 = 247 + 3. 통합 브랜치에 합류한 두 티켓이 형제 로더 위에 새로 연 경계:
    #   + `ticket_finish.py:_load_private_refs:377` reraises — 완료 기록 preflight 가 사설 참조
    #     판정식(`private_refs.py`)을 공용 로더(`cache=True` · 재유입 가드와 같은 cache key)로
    #     올리는 자리. 부재·파손은 None(가드 off)으로 접되 마킹된 skew 만 그대로 올린다
    #     (`_load_external_review` 와 같은 관용구).
    #   + `board.py:_round_pending_ledger_owner:11178` reraises — draft discard 의 round-pending
    #     안내가 미회수 장부의 PM 홈을 `external_review.resolve_pm_home_for_repo` 로 해소하는
    #     자리. 해소 실패는 "안내를 못 낸다"로 접고(discard 비차단) 마킹된 skew 는 올린다.
    #   + `board.py:_round_pending_abandon_command:11199` reraises — 같은 안내가
    #     `pm_delegate.ticket_copy_records` 로 실 `--copy`/`--cwd` 를 읽는 자리. 장부 손상은
    #     `ticket copies --unharvested` 처방으로 접고 마킹된 skew 는 올린다. 두 board 경계는
    #     병합 직후 `unmarked-absorb` 로 들어왔다가 이 관용구로 마킹됐다(이 래칫이 잡은 첫 사례).
    # 265 = 250 + 15. 묶음 종결(close) 파이프라인과 통합 브랜치 기준 판정이 형제 seam 위에 새로
    #   연 경계들. 전부 같은 관용구다 — 조회·관측 실패는 그 축을 판정 불능으로 접되(종결을 벽돌로
    #   만들지 않는다) 마킹된 skew 만 그대로 올린다:
    #   + `ticket_finish.py:_board_module_at` · `_cluster_integration_branch` reraises — 통합
    #     브랜치 선언(묶음 장부 `base_branch`)을 board 사본에서 읽는 자리. 선언 부재·조회 실패는
    #     None 이고, 그 값을 받는 소비자가 멈춘다(접는 갈래 없음).
    #   + `ticket_finish.py:TicketFinisher._line_reached_integration` reraises — 줄의 도입
    #     커밋이 통합 브랜치에 있는지 묻는(`merge-base --is-ancestor`) 판정. 판정 실패는 신규
    #     취급(차단 방향)이다.
    #   + `ticket_finish.py:ClusterCloser` 의 관측·seam 자리 11 — `_default_release`(반납 거부
    #     사유를 값으로) · `_slot_state`·`_ticket_status`·`_pending_paths`·`_is_ancestor`·
    #     `_dirty_paths`·`_integration_worktree`·`_board_pointer_path`(단계 건너뛰기 관측) ·
    #     `_delegate_supports_cluster`(처분 표면 조회) · `_board_git_paths`(티켓 경로 조회) ·
    #     `_write_progress`(장부 진행 기록 — 기록 실패가 종결을 되돌리지 않는다).
    # 267 = 265 + 2. 재실행 중복 방지와 리뷰 송신 폭 기준의 두 경계다:
    #   + `ticket_finish.py:TicketFinisher._log_has_entry` reraises — 이미 남은 완료 기록
    #     스켈레톤을 log 에서 관측하는 자리(재실행이 같은 스켈레톤을 다시 쌓지 않게). 읽기 실패는
    #     '없음'(중복 방지 판정 불능 — 기록을 막지 않는다)이고 마킹된 skew 만 올린다.
    #   + `external_review.py:cluster_integration_tip` reraises — 리뷰 송신 폭의 기준점(묶음 장부
    #     통합 브랜치)을 형제 완료 기록 엔진의 해소 seam 으로 읽는 자리. 부재/손상은 사유를 돌려
    #     호출부가 거부하게 하고 마킹된 skew 는 그대로 올린다(다른 형제 로더와 같은 규칙).
    # 269 = 267 + 2. 잔여 판정 인구가 커밋분까지 넓어지며 연 두 경계다:
    #   + `ticket_finish.py:_cluster_member_ids` reraises — 묶음 멤버 목록을 board 술어
    #     (`cluster_tickets`)로 읽는 자리. 장부 부재·조회 실패는 빈 목록(제외 없이 인구 유지 —
    #     선언 누락을 숨기지 않는 쪽)이고 마킹된 skew 만 올린다.
    #   + `ticket_finish.py:TicketFinisher._committed_out_of_scope` reraises — 통합 tip 기준
    #     커밋 인구를 `git diff` 로 세는 자리.
    # 270 = 269 + 1. 묶음 리뷰 백그라운드 실행 장부의 마감 경계:
    #   + `pm_delegate.py:main` reraises — CLI 진입을 감싸 자식 프로세스가 rc 반환·`SystemExit`·
    #     전파 예외 어느 경로로 끝나든 실행 장부 행을 자기 rc 로 마감하는 자리. 예외는 마감만
    #     기록하고 그대로 다시 올린다(흡수 0 — 마킹된 skew 포함). 통합 브랜치 합류 뒤 형제 티켓의
    #     래칫 서사에 빠져 있던 것을 여기서 보충한다.
    # 268 = 270 − 2. 판정 기준(통합 브랜치)을 다른 기준으로 접던 갈래가 사라지며 두 경계가 없어
    #   졌다 — `TicketFinisher._integration_tip`(해소 실패 → 정지) ·
    #   `TicketFinisher._committed_out_of_scope`(조회 실패 → 정지). 접을 곳이 없으면 흡수 경계도
    #   없다. 래칫은 제거 방향으로만 움직인다.
    # 269 = 268 + T-0871 Python scaffold test_cmd의 한 경계. `pm_import._default_test_cmd`가
    #   board의 병렬 pytest 기본값 단일 소유자를 소비한다. 부분 사본에서 board 로드가 실패하면
    #   같은 `-n auto` 값으로 폴백하되, marked skew는 다른 형제 로더와 같이 그대로 올려 사본
    #   불일치를 기본값 해소로 숨기지 않는다.
    assert len(report.boundaries) == 269, "propagation sweep boundary ratchet changed"
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


def test_scanner_instance_callable_attribute_axis_has_an_independent_synthetic_fixture():
    fn = "Holder.consume"
    caller = """\
def _load_leaf():
    return _load_module("leaf.py")

class Holder:
    def __init__(self, injected=None):
        self._run_fn = injected or self._default_run

    def _default_run(self):
        _load_leaf().risky()

    def consume(self):
        try:
            self._run_fn()
        except Exception:
            return None
"""
    report = collect_failsoft_report({"leaf.py": _SYNTHETIC_LEAF, "caller.py": caller})
    assert any("caller.py" in violation and f": {fn} " in violation
               for violation in report.violations)


# 수신자 규칙 twin — 두 fixture 는 **수신자 이름 한 글자**(`self` ↔ `sel`)만 다르다. 저장 형태·
# 호출 형태·로더 provenance 가 전부 같아야 "링크는 수신자가 `self`/`cls` 일 때만"이라는 규칙
# 하나만 시험된다. 옛 음성 fixture 는 저장 대상(`other._run_fn`)과 기본 구현 유무까지 달라서,
# 판정을 가른 게 수신자인지 다른 차이인지 fixture 만 봐선 알 수 없었다.
_INSTANCE_CALLABLE_RECEIVER_TWIN = """\
def _load_leaf():
    return _load_module("leaf.py")

class Holder:
    def __init__(self, sel):
        self._run_fn = {receiver}._default_run

    def _default_run(self):
        _load_leaf().risky()

    def consume(self):
        try:
            self._run_fn()
        except Exception:
            return None
"""


@pytest.mark.parametrize(("receiver", "linked"), [("self", True), ("sel", False)])
def test_scanner_instance_callable_attribute_axis_links_self_receivers_only(
    receiver, linked,
):
    """수신자만 한 글자 다른 쌍 — `self` 는 링크, 같은-이름 속성을 든 남의 객체는 링크 안 한다.

    음성 쪽(`sel`)이 provenance 없는 duck-typing 배제다 — 이름이 같다는 이유만으로 임의 객체의
    속성을 엮으면 스캐너가 근거 없는 경계를 만들어 낸다.
    """
    caller = _INSTANCE_CALLABLE_RECEIVER_TWIN.format(receiver=receiver)
    report = collect_failsoft_report({"leaf.py": _SYNTHETIC_LEAF, "caller.py": caller})
    matched = [
        violation for violation in report.violations
        if "caller.py" in violation and ": Holder.consume " in violation
    ]
    assert bool(matched) is linked, (
        f"수신자 {receiver!r} 링크 판정이 예상과 다름 — 검출 {report.violations}"
    )
    if not linked:
        assert not report.violations, report.violations


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


def _remove_marker_statement(source: str, function: str, occurrence: int = 0) -> str:
    """마커 분기를 **문장째** 지운다 — 마커를 아예 안 쓰는 신규 코드 모양(plain-absorb).

    `_remove_marker_branch`(분기 body 만 `pass` 로) 와 다르다: 그 변형은 핸들러가 여전히
    `_is_engine_rev_skew` 를 소비해 명시 root 로 잡히지만, 이쪽은 핸들러가 마커를 아예 안 써서
    호출 그래프 추적만으로 잡아야 한다(축 커버리지의 실제 시험).
    """
    tree, handler = _scope_handler(source, function, occurrence)
    marker = _marker_call(handler, "_is_engine_rev_skew")
    kept = [
        statement for statement in handler.body
        if not (isinstance(statement, ast.If) and statement.test is marker)
    ]
    assert len(kept) < len(handler.body), f"{function}: 마커 분기가 핸들러 최상위에 없다"
    handler.body = kept or [ast.copy_location(ast.Pass(), handler.body[0])]
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


@pytest.mark.parametrize(
    ("source_name", "function"),
    [
        ("ticket_finish.py", "TicketFinisher._notify_affected_domain"),
        ("ticket_finish.py", "TicketFinisher._dirty_split"),
        ("pm_bootstrap.py", "PmBootstrap._slot_branch_exists"),
        ("pm_bootstrap.py", "PmBootstrap._unrecorded_base_candidates"),
        ("pm_bootstrap.py", "PmBootstrap._slot_era_info"),
    ],
)
def test_guard_sensitivity_instance_callable_attribute_plain_absorb_turns_red(
    source_name, function,
):
    """`self.<attr>(...)` 경계는 마커 문장을 통째로 지운 신규 코드 모양에서도 잡힌다."""
    sources = _canonical_sources()
    sources[source_name] = _remove_marker_statement(sources[source_name], function)
    report = collect_failsoft_report(sources)
    assert any(source_name in violation and f": {function} " in violation
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
                # 세션-entry 실행 슬롯 해소가 부르는 형제 경계 — 좌표만 내던
                # `_resolve_session_slot` 에서 좌표→행 경로·정체성까지 해소하는
                # `_session_slot_identity` 로 옮겼다(경계 자체는 같은 자리).
                _session_slot_identity=_raiser(skew),
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


@pytest.mark.parametrize(
    "case",
    [
        "bootstrap_slot_branch_exists",
        "bootstrap_unrecorded_base_candidates",
        "bootstrap_slot_era_info",
        "finish_dirty_split",
    ],
)
def test_runtime_instance_callable_attribute_boundaries_rethrow_marked_skew(case):
    """DI 속성(`self._run_git_fn`·`self._status_entries_fn`) 경계의 런타임 재전파.

    기본 구현이 형제 모듈 로드를 타므로(`_worktree_cwd`·`load_board_module`) marked skew 가
    이 fail-soft 핸들러들에 도달한다 — 미존재/빈 목록/빈 보고로 강등하면 안 된다.
    """
    skew = _marked_skew()
    if case == "finish_dirty_split":
        module = _runtime_tool("ticket_finish.py")
        instance = object.__new__(module.TicketFinisher)
        instance._status_entries_fn = _raiser(skew)
        invoke = lambda: instance._dirty_split(["src/file.py"])
    else:
        module = _runtime_tool("pm_bootstrap.py")
        instance = object.__new__(module.PmBootstrap)
        instance._bound_slot = None
        instance._run_git_fn = _raiser(skew)
        if case == "bootstrap_slot_branch_exists":
            worktree_pool = SimpleNamespace(slot_path=lambda _slot: Path("slot"))
            invoke = lambda: instance._slot_branch_exists(worktree_pool, "work/repo_1", "feature")
        elif case == "bootstrap_unrecorded_base_candidates":
            invoke = lambda: instance._unrecorded_base_candidates("slot")
        else:
            instance._resolve_slot_base = lambda _repo: SimpleNamespace(
                branch="main", source="repo-default", target="origin/main", needs_fetch=False,
            )
            instance._worktree_cwd = lambda _slot: "slot"
            invoke = lambda: instance._slot_era_info("repo", [])
    with pytest.raises(RuntimeError) as raised:
        invoke()
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
        encoding="utf-8",
        capture_output=True,
        env=utf8_child_env(),
        timeout=20,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stderr.startswith("[중단] 엔진 사본 불일치")
    assert "pm-update" in completed.stderr
    assert "Traceback" not in completed.stderr
