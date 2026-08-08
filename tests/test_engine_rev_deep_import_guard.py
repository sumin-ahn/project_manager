"""Central repo-owned loader boundary and rev-verification invariant.

The scanner deliberately reads only the canonical ``.project_manager/tools/*.py`` tree.  The
``templates/{claude_code,codex,opencode}`` trees are vendor snapshots shipped by the import/update
pipeline, not additional sources of truth; scanning both would duplicate every boundary and make a
stale generated copy block canonical development.  Template parity belongs to the manifest/update
tests.  A mutation test below locks this scope decision.

All repo-owned imports must pass through ``repo_owned_files.load_module``; the only direct
``spec_from_file_location`` call lives inside that seam.  The AST guard links every caller to an
effective verifier or a code-owned exemption and keeps stamped-module and literal-gate policy
auditable.  ``collect_central_guard_report`` owns that central seam exclusivity and caller policy;
``collect_guard_report`` retains raw-loader target/stamp/verifier dataflow sensitivity.  Comments
and string literals are never searched.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
import traceback
import types
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
_CENTRAL_LOADER = ("repo_owned_files.py", "load_module")


@dataclass(frozen=True)
class Boundary:
    loader: str
    function: str
    line: int
    target: str | None
    verified: bool


@dataclass(frozen=True)
class GuardReport:
    boundaries: tuple[Boundary, ...]
    targets: frozenset[str]
    violations: tuple[str, ...]


@dataclass(frozen=True)
class CentralGuardReport:
    spec_calls: tuple[tuple[str, str, int], ...]
    loader_calls: tuple[tuple[str, str, int], ...]
    violations: tuple[str, ...]


def collect_central_guard_report(tools: Path) -> CentralGuardReport:
    """Own the central file-location seam and every caller's verifier/exemption policy."""
    spec_calls: list[tuple[str, str, int]] = []
    loader_calls: list[tuple[str, str, int]] = []
    violations: list[str] = []
    engine_rev = _load_module(tools, "engine_rev")
    stamped = set(engine_rev.STAMPED_MODULES)
    module_exemptions = dict(engine_rev.EXEMPT_FROM_STAMP)
    call_exemptions = dict(engine_rev.EXEMPT_UNVERIFIED_DEEP_IMPORTS)

    violations.extend(
        f"empty deep-import exemption reason: {source}:{scope}"
        for (source, scope), reason in call_exemptions.items()
        if not isinstance(reason, str) or not reason.strip()
    )

    for source in sorted(tools.glob("*.py"), key=lambda path: path.name):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        spec_aliases = _spec_aliases(tree)
        loader_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "repo_owned_files"
            for alias in node.names
            if alias.name == "load_module"
        }
        loader_aliases.update(
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "load_module"
        )
        module_bindings_visitor = _Bindings(tree)
        module_bindings_visitor.visit(tree)
        module_bindings = module_bindings_visitor.values
        definitions = _module_definitions(tree)
        central_loader = (
            _top_level_function(tree, _CENTRAL_LOADER[1])
            if source.name == _CENTRAL_LOADER[0]
            else None
        )
        literal_gate = _literal_string_set(tree, "_STAMPED_SIBLINGS")
        if literal_gate is not None:
            for target in sorted(literal_gate - stamped):
                violations.append(
                    f"{source.name}: gate target {target} is not in STAMPED_MODULES"
                )
        for scope in _scope_functions(tree):
            function = (
                scope
                if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
                else None
            )
            scope_name = getattr(scope, "name", "<module>")
            local_bindings_visitor = _Bindings(scope)
            local_bindings_visitor.visit(scope)
            bindings = {
                name: [
                    *module_bindings.get(name, ()),
                    *local_bindings_visitor.values.get(name, ()),
                ]
                for name in module_bindings.keys() | local_bindings_visitor.values.keys()
            }
            parameters = frozenset(
                arg.arg for arg in function.args.args
            ) if function is not None else frozenset()
            calls = _Calls(scope)
            calls.visit(scope)
            for call in calls.calls:
                if _is_unsupported_deep_import_call(call):
                    violations.append(
                        f"{source.name}:{call.lineno}: unsupported deep-import API bypass"
                    )
                if _is_spec_call(call, spec_aliases):
                    location = (source.name, scope_name, call.lineno)
                    spec_calls.append(location)
                    if source.name != _CENTRAL_LOADER[0] or scope is not central_loader:
                        violations.append(
                            f"{source.name}:{call.lineno}: spec_from_file_location outside central loader"
                        )
                called_loader = (
                    isinstance(call.func, ast.Name) and call.func.id in loader_aliases
                ) or (
                    isinstance(call.func, ast.Attribute) and call.func.attr == "load_module"
                )
                if not called_loader:
                    continue
                loader_calls.append((source.name, scope_name, call.lineno))
                verifier_keywords = [kw for kw in call.keywords if kw.arg == "verifier"]
                exemption_keywords = [kw for kw in call.keywords if kw.arg == "allow_unverified"]
                has_verifier = (
                    len(verifier_keywords) == 1
                    and (
                        isinstance(verifier_keywords[0].value, ast.Name)
                        and verifier_keywords[0].value.id == "_verify_engine_rev"
                        or (
                            source.name == "engine_rev.py"
                            and scope_name == "load_repo_owned_files"
                            and isinstance(verifier_keywords[0].value, ast.Name)
                            and verifier_keywords[0].value.id == "verifier"
                        )
                    )
                )
                has_exemption = (
                    len(exemption_keywords) == 1
                    and isinstance(exemption_keywords[0].value, ast.Constant)
                    and exemption_keywords[0].value.value is True
                )
                if has_verifier == has_exemption:
                    violations.append(
                        f"{source.name}:{call.lineno}: loader requires exactly one explicit "
                        "policy with an effective verifier or exemption"
                    )
                if any(kw.arg is None for kw in call.keywords):
                    violations.append(
                        f"{source.name}:{call.lineno}: unpacked loader policy is not auditable"
                    )
                if len(call.args) < 2:
                    violations.append(
                        f"{source.name}:{call.lineno}: expected filename is missing"
                    )
                    continue
                template = _literal_piece(call.args[1], bindings, parameters)
                targets, unresolved = _targets_for_template(
                    template, function, tree, module_bindings,
                )
                canonical_anchor = bool(call.args) and _depends_on_canonical_anchor(
                    call.args[0], bindings, definitions,
                )
                target_exemption = bool(targets) and targets <= module_exemptions.keys()
                explicit_exemption = (
                    source.name in module_exemptions
                    or (source.name, scope_name) in call_exemptions
                    or target_exemption
                )
                gated_exemption = bool(
                    has_exemption
                    and literal_gate is not None
                    and not unresolved
                    and ((targets - module_exemptions.keys()) & stamped) <= literal_gate
                )
                if has_exemption and not explicit_exemption and not gated_exemption:
                    violations.append(
                        f"{source.name}:{call.lineno}: allow_unverified call is not covered "
                        "by a code-owned exemption or complete stamped gate"
                    )
                if unresolved and canonical_anchor and not explicit_exemption:
                    violations.append(
                        f"{source.name}:{call.lineno}: loader target is statically unresolved"
                    )
                if has_verifier:
                    for target in sorted(targets - stamped - module_exemptions.keys()):
                        violations.append(
                            f"{source.name}:{call.lineno}: target {target} is not in "
                            "STAMPED_MODULES"
                        )
                if has_exemption and literal_gate is not None and not unresolved:
                    for target in sorted((targets & stamped) - literal_gate):
                        violations.append(
                            f"{source.name}:{call.lineno}: stamped target {target} is missing "
                            "from the verifier gate"
                        )

    if len(spec_calls) != 1:
        violations.append(
            f"central spec_from_file_location call count must be 1, got {len(spec_calls)}"
        )
    return CentralGuardReport(
        tuple(spec_calls), tuple(loader_calls), tuple(sorted(set(violations))),
    )


class _Bindings(ast.NodeVisitor):
    """Assignments in one lexical scope, retaining source order for point-in-time lookup."""

    def __init__(self, root: ast.AST):
        self.root = root
        self.values: dict[str, list[ast.AST]] = {}

    def _record(self, name: str, value: ast.AST) -> None:
        self.values.setdefault(name, []).append(value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._record(target.id, node.value)
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._record(node.target.id, node.value)
        if node.value is not None:
            self.generic_visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        if isinstance(node.target, ast.Name):
            self._record(node.target.id, node.value)
        self.generic_visit(node.value)


class _Calls(ast.NodeVisitor):
    """Calls in one lexical scope, excluding calls in nested definitions."""

    def __init__(self, root: ast.AST):
        self.root = root
        self.calls: list[ast.Call] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)


def _portable_name(value: str) -> str:
    """Return a basename without assuming POSIX separators or case folding."""
    return PureWindowsPath(Path(value).name).name


def _binding_candidates(
    name: str,
    bindings: dict[str, list[ast.AST]],
    before_line: int,
) -> list[ast.AST]:
    """Return assignments visible before one use; later re-use must not rewrite history."""
    return [
        value for value in bindings.get(name, ())
        if getattr(value, "lineno", -1) < before_line
    ]


def _literal_piece(node: ast.AST, bindings: dict[str, list[ast.AST]],
                   parameters: frozenset[str], seen: frozenset[str] = frozenset()):
    """Resolve a filename expression to ``str`` or one ``(parameter, prefix, suffix)`` template."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in parameters:
            return (node.id, "", "")
        candidates = _binding_candidates(node.id, bindings, node.lineno)
        if len(candidates) == 1 and node.id not in seen:
            return _literal_piece(
                candidates[0], bindings, parameters, seen | {node.id},
            )
        return None
    if isinstance(node, ast.JoinedStr):
        parameter = None
        prefix = ""
        suffix = ""
        before_parameter = True
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if before_parameter:
                    prefix += value.value
                else:
                    suffix += value.value
                continue
            if not isinstance(value, ast.FormattedValue):
                return None
            piece = _literal_piece(value.value, bindings, parameters, seen)
            if not isinstance(piece, tuple) or parameter is not None:
                return None
            parameter = piece[0]
            prefix += piece[1]
            suffix = piece[2] + suffix
            before_parameter = False
        return (parameter, prefix, suffix) if parameter is not None else prefix
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        # pathlib composition: the rightmost component owns the sibling filename.
        return _literal_piece(node.right, bindings, parameters, seen)
    if isinstance(node, ast.Attribute) and node.attr == "name":
        return _literal_piece(node.value, bindings, parameters, seen)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "with_name" and node.args:
            return _literal_piece(node.args[0], bindings, parameters, seen)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
            return _literal_piece(node.func.value, bindings, parameters, seen)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "join" and node.args:
            return _literal_piece(node.args[-1], bindings, parameters, seen)
        if isinstance(node.func, ast.Name) and node.func.id == "Path" and node.args:
            return _literal_piece(node.args[0], bindings, parameters, seen)
    return None


def _depends_on_canonical_anchor(
    node: ast.AST,
    bindings: dict[str, list[ast.AST]],
    definitions: dict[str, list[ast.AST]],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Whether an expression depends on this module's canonical ``__file__`` tools anchor.

    Besides direct ``Path(__file__)`` spelling, module-level helper calls and class-owned anchors
    are definitions, not opaque values.  A helper/class body that references ``__file__`` therefore
    keeps the path fail-loud.  Ambiguous reassignments are conservative: any anchored reaching
    candidate makes the unresolved expression anchored.
    """
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return True
        if node.id not in seen:
            return any(
                _depends_on_canonical_anchor(
                    candidate, bindings, definitions, seen | {node.id},
                )
                for candidate in _binding_candidates(node.id, bindings, node.lineno)
            )
        return False
    if isinstance(node, ast.Attribute) and node.attr == "__file__":
        # Includes sys.modules[__name__].__file__, whose AST contains no Name("__file__").
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in seen:
            helper_is_anchored = any(
                any(
                    _depends_on_canonical_anchor(child, bindings, definitions,
                                                 seen | {node.func.id})
                    for child in ast.walk(definition)
                )
                for definition in definitions.get(node.func.id, ())
            )
            if helper_is_anchored:
                return True
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id not in seen:
            class_is_anchored = any(
                any(
                    _depends_on_canonical_anchor(child, bindings, definitions,
                                                 seen | {node.value.id})
                    for child in ast.walk(definition)
                )
                for definition in definitions.get(node.value.id, ())
            )
            if class_is_anchored:
                return True
    return any(
        _depends_on_canonical_anchor(child, bindings, definitions, seen)
        for child in ast.iter_child_nodes(node)
    )


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _spec_aliases(tree: ast.Module) -> frozenset[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module != "importlib.util":
            continue
        for alias in node.names:
            if alias.name == "spec_from_file_location":
                aliases.add(alias.asname or alias.name)
    return frozenset(aliases)


def _is_spec_call(call: ast.Call, aliases: frozenset[str] = frozenset()) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "spec_from_file_location"
    ) or (
        isinstance(call.func, ast.Name) and call.func.id in aliases
    )


def _spec_location(call: ast.Call) -> tuple[ast.AST | None, bool]:
    """Return ``location`` and whether the API argument shape is statically ambiguous."""
    keyword_locations = [
        keyword.value for keyword in call.keywords if keyword.arg == "location"
    ]
    has_unpacking = any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    )
    positional = call.args[1] if len(call.args) >= 2 else None
    if has_unpacking or len(keyword_locations) > 1:
        return None, True
    if positional is not None and keyword_locations:
        return None, True
    if positional is not None:
        return positional, False
    if keyword_locations:
        return keyword_locations[0], False
    return None, True


def _is_common_repo_loader_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "load_repo_owned_files"
    )


def _common_repo_loader_location(call: ast.Call) -> tuple[ast.AST | None, bool]:
    keyword_paths = [keyword.value for keyword in call.keywords if keyword.arg == "path"]
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        return None, True
    if call.args and keyword_paths:
        return None, True
    if call.args:
        return call.args[0], False
    if len(keyword_paths) == 1:
        return keyword_paths[0], False
    return None, True


def _common_repo_loader_has_verifier(call: ast.Call) -> bool:
    return any(
        keyword.arg == "verifier"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "_verify_engine_rev"
        for keyword in call.keywords
    )


def _is_unsupported_deep_import_call(call: ast.Call) -> bool:
    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "load_module"
        and isinstance(call.func.value, ast.Call)
    ):
        constructor = call.func.value.func
        if (
            isinstance(constructor, ast.Name)
            and constructor.id == "SourceFileLoader"
        ) or (
            isinstance(constructor, ast.Attribute)
            and constructor.attr == "SourceFileLoader"
        ):
            return True
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "exec"
        and bool(call.args)
        and isinstance(call.args[0], ast.Call)
        and _called_name(call.args[0]) == "compile"
    )


def _is_verifier_call(call: ast.Call) -> bool:
    return (
        (isinstance(call.func, ast.Name) and call.func.id == "_verify_engine_rev")
        or (isinstance(call.func, ast.Attribute) and call.func.attr == "_verify_engine_rev")
    )


def _verifier_module_is_same_scope_module_from_spec(
    first_arg: ast.AST,
    scope_bindings: dict[str, list[ast.AST]],
) -> bool:
    """Bounded link: first arg is a same-scope name assigned from module_from_spec."""
    if not isinstance(first_arg, ast.Name):
        return False
    candidates = _binding_candidates(first_arg.id, scope_bindings, first_arg.lineno)
    return (
        len(candidates) == 1
        and isinstance(candidates[0], ast.Call)
        and _called_name(candidates[0]) == "module_from_spec"
    )


def _scope_functions(tree: ast.Module) -> list[ast.AST]:
    scopes = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    return [tree, *sorted(scopes, key=lambda node: (node.lineno, node.col_offset))]


def _top_level_function(
    tree: ast.Module,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return one direct module child, never a same-named method or nested function."""
    matches = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    return matches[0] if len(matches) == 1 else None


def _function_calls(
    tree: ast.Module,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list[ast.Call], bool]:
    definitions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function.name
    ]
    if len(definitions) != 1:
        return [], True
    found: list[ast.Call] = []
    for scope in _scope_functions(tree):
        calls = _Calls(scope)
        calls.visit(scope)
        found.extend(call for call in calls.calls if _called_name(call) == function.name)
    return found, False


def _targets_for_template(
    template, function: ast.FunctionDef | ast.AsyncFunctionDef | None,
    tree: ast.Module, module_bindings: dict[str, list[ast.AST]],
) -> tuple[set[str], bool]:
    if isinstance(template, str):
        name = _portable_name(template)
        return ({name} if name.endswith(".py") else set()), False
    if not isinstance(template, tuple) or function is None:
        return set(), True
    parameter, prefix, suffix = template
    parameters = [arg.arg for arg in function.args.args]
    if parameter not in parameters:
        return set(), True
    index = parameters.index(parameter)
    targets: set[str] = set()
    unresolved = False
    function_calls, ambiguous_function = _function_calls(tree, function)
    for call in function_calls:
        if len(call.args) <= index:
            unresolved = True
            continue
        piece = _literal_piece(call.args[index], module_bindings, frozenset())
        if not isinstance(piece, str):
            unresolved = True
            continue
        name = _portable_name(f"{prefix}{piece}{suffix}")
        if name.endswith(".py"):
            targets.add(name)
    return targets, ambiguous_function or unresolved or not targets


def _literal_string_set(tree: ast.Module, name: str) -> set[str] | None:
    """Read one module-level ``name = frozenset({...})``-shaped literal."""
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            value = value.args[0]
        if not isinstance(value, (ast.Set, ast.Tuple, ast.List)):
            return None
        if not all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in value.elts
        ):
            return None
        return {item.value for item in value.elts}
    return None


def _module_definitions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    definitions: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.setdefault(node.name, []).append(node)
    return definitions


def _handler_reraises_verifier_skew(handler: ast.ExceptHandler) -> bool:
    """Whether a try handler structurally preserves marked verifier failures."""
    if not isinstance(handler.name, str):
        return False
    for node in ast.walk(handler):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Call):
            continue
        if _called_name(node.test) != "_is_engine_rev_skew":
            continue
        if not node.test.args or not (
            isinstance(node.test.args[0], ast.Name)
            and node.test.args[0].id == handler.name
        ):
            continue
        if any(isinstance(statement, ast.Raise) for statement in ast.walk(node)):
            return True
    return False


def _conditional_tests(
    call: ast.Call,
    scope: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> tuple[list[ast.AST], bool]:
    """Return positive conditions controlling a call and whether a non-positive shape exists."""
    tests: list[ast.AST] = []
    unsupported = False
    child: ast.AST = call
    node = parents.get(child)
    while node is not None and node is not scope:
        if isinstance(node, (ast.If, ast.IfExp)):
            if child is node.test:
                pass
            elif child is node.body or (
                isinstance(node, ast.If) and child in node.body
            ):
                tests.append(node.test)
            else:
                unsupported = True
        elif isinstance(node, (ast.Try, ast.TryStar)):
            # A fail-soft loader try is safe only when every handler explicitly re-raises
            # marked skew. ``try: verify(); except: pass`` and verifier calls in else/finally
            # remain statically non-guaranteed and therefore loud.
            if (
                child not in node.body
                or not node.handlers
                or not all(_handler_reraises_verifier_skew(h) for h in node.handlers)
            ):
                unsupported = True
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            unsupported = True
        elif isinstance(node, ast.comprehension) and call is not node.iter:
            # Iteration/filter cardinality is not an unconditional verifier guarantee.
            unsupported = True
        elif isinstance(node, ast.BoolOp) and child is not node.values[0]:
            unsupported = True
        child = node
        node = parents.get(node)
    return tests, unsupported


def _membership_condition_targets(
    test: ast.AST,
    *,
    tree: ast.Module,
    function: ast.FunctionDef | ast.AsyncFunctionDef | None,
    bindings: dict[str, list[ast.AST]],
    parameters: frozenset[str],
    module_bindings: dict[str, list[ast.AST]],
) -> tuple[set[str], str | None]:
    """Resolve ``target-expression in MODULE_LITERAL_SET`` without caring about its name."""
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.In)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Name)
    ):
        return set(), "condition is not literal-set membership"
    gate_name = test.comparators[0].id
    gate = _literal_string_set(tree, gate_name)
    if gate is None:
        return set(), f"{gate_name} is not a module-level literal string set"
    template = _literal_piece(test.left, bindings, parameters)
    targets, unresolved = _targets_for_template(
        template, function, tree, module_bindings,
    )
    if unresolved:
        return set(), f"{gate_name} membership target is statically unresolved"
    return targets & gate, None


def collect_guard_report(tools: Path) -> GuardReport:
    """Own raw-loader target resolution plus stamp/verifier dataflow sensitivity."""
    engine_rev = _load_module(tools, "engine_rev")
    stamped = set(engine_rev.STAMPED_MODULES)
    exemptions = dict(engine_rev.EXEMPT_FROM_STAMP)
    loader_exemptions = dict(engine_rev.EXEMPT_UNVERIFIED_DEEP_IMPORTS)
    violations = [
        f"empty exemption reason: {name}"
        for name, reason in exemptions.items()
        if not isinstance(reason, str) or not reason.strip()
    ]
    violations.extend(
        f"empty deep-import exemption reason: {loader}:{function}"
        for (loader, function), reason in loader_exemptions.items()
        if not isinstance(reason, str) or not reason.strip()
    )
    boundaries: list[Boundary] = []
    existing_modules = {path.name for path in tools.glob("*.py")}

    for source in sorted(tools.glob("*.py"), key=lambda path: path.name):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        aliases = _spec_aliases(tree)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        definitions = _module_definitions(tree)
        central_loader = (
            _top_level_function(tree, _CENTRAL_LOADER[1])
            if source.name == _CENTRAL_LOADER[0]
            else None
        )
        module_bindings_visitor = _Bindings(tree)
        module_bindings_visitor.visit(tree)
        module_bindings = module_bindings_visitor.values
        source_has_boundary = False

        for scope in _scope_functions(tree):
            function = scope if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
            scope_name = function.name if function is not None else "<module>"
            local_visitor = _Bindings(scope)
            local_visitor.visit(scope)
            bindings = {
                name: [*module_bindings.get(name, ()), *local_visitor.values.get(name, ())]
                for name in module_bindings.keys() | local_visitor.values.keys()
            }
            parameters = frozenset(
                arg.arg for arg in function.args.args
            ) if function is not None else frozenset()
            calls = _Calls(scope)
            calls.visit(scope)
            scope_has_loader_boundary = any(
                _is_spec_call(call, aliases) or _is_common_repo_loader_call(call)
                for call in calls.calls
            )
            verified_targets: set[str] = set()
            has_valid_verifier = False
            for verifier in (call for call in calls.calls if _is_verifier_call(call)):
                if len(verifier.args) < 2:
                    continue
                if not _verifier_module_is_same_scope_module_from_spec(
                    verifier.args[0], local_visitor.values,
                ):
                    if scope_has_loader_boundary:
                        violations.append(
                            f"{source.name}:{scope_name}:{verifier.lineno}: verifier first "
                            "argument is not a same-scope module_from_spec result"
                        )
                    continue
                verifier_template = _literal_piece(
                    verifier.args[1], bindings, parameters,
                )
                verifier_targets, _ = _targets_for_template(
                    verifier_template, function, tree, module_bindings,
                )
                conditional_tests, unsupported_condition = _conditional_tests(
                    verifier, scope, parents,
                )
                allowed = set(verifier_targets)
                condition_errors: list[str] = []
                if unsupported_condition:
                    condition_errors.append("unsupported conditional control flow")
                for test in conditional_tests:
                    condition_targets, error = _membership_condition_targets(
                        test,
                        tree=tree,
                        function=function,
                        bindings=bindings,
                        parameters=parameters,
                        module_bindings=module_bindings,
                    )
                    if error is not None:
                        condition_errors.append(error)
                    else:
                        allowed &= condition_targets
                if condition_errors:
                    violations.append(
                        f"{source.name}:{scope_name}:{verifier.lineno}: conditional verifier "
                        "is unresolved "
                        f"({'; '.join(condition_errors)})"
                    )
                else:
                    has_valid_verifier = True
                    verified_targets.update(allowed)

            for call in calls.calls:
                if _is_unsupported_deep_import_call(call):
                    violations.append(
                        f"{source.name}:{scope_name}:{call.lineno}: unsupported deep-import "
                        "API bypass"
                    )
                    continue
                is_spec = _is_spec_call(call, aliases)
                is_common_repo_loader = _is_common_repo_loader_call(call)
                if not is_spec and not is_common_repo_loader:
                    continue
                if (
                    is_spec
                    and source.name == _CENTRAL_LOADER[0]
                    and scope is central_loader
                ):
                    # 중앙 seam 내부 구현은 collect_central_guard_report가 유일 호출 지점과
                    # verifier/exemption 계약을 함께 검사한다. legacy sibling oracle의 경계가 아니다.
                    continue
                location, ambiguous_args = (
                    _spec_location(call)
                    if is_spec
                    else _common_repo_loader_location(call)
                )
                if ambiguous_args or location is None:
                    boundaries.append(Boundary(
                        source.name, scope_name, call.lineno, None, False,
                    ))
                    violations.append(
                        f"{source.name}:{scope_name}:{call.lineno}: unresolved "
                        "spec_from_file_location "
                        "argument shape"
                    )
                    continue
                template = _literal_piece(location, bindings, parameters)
                targets, unresolved = _targets_for_template(
                    template, function, tree, module_bindings,
                )
                anchored = _depends_on_canonical_anchor(
                    location, bindings, definitions,
                )
                targets &= existing_modules
                # Calls to hooks, ticket files, or destination/adopter copies are not canonical
                # sibling engine boundaries. This function-scoped exemption is deliberately a
                # separate *dest/adopter path* axis from whole-module stamp exemptions: a literal
                # dest filename can equal a stamped sibling without becoming a canonical boundary.
                # Any unresolved path rooted at this module's canonical anchor is nevertheless
                # loud; expression shape must not turn detection off.
                adopter_path_exemption = (
                    source.name, scope_name
                ) in loader_exemptions
                if not anchored and adopter_path_exemption:
                    continue
                if unresolved and anchored and not adopter_path_exemption:
                    violations.append(
                        f"{source.name}:{scope_name}:{call.lineno}: unresolved canonical "
                        "sibling path"
                    )
                if not targets:
                    if unresolved and adopter_path_exemption:
                        continue
                    if unresolved:
                        boundaries.append(Boundary(
                            source.name,
                            scope_name,
                            call.lineno,
                            None,
                            has_valid_verifier,
                        ))
                        if (
                            not anchored
                            and not has_valid_verifier
                            and source.name not in exemptions
                        ):
                            violations.append(
                                f"{source.name}:{scope_name}:{call.lineno}: unresolved "
                                "external loader "
                                "has no verifier or code-owned exemption"
                            )
                    continue
                for target in sorted(targets):
                    source_has_boundary = True
                    target_verified = (
                        target in verified_targets
                        or (
                            is_common_repo_loader
                            and _common_repo_loader_has_verifier(call)
                        )
                    )
                    boundary = Boundary(
                        source.name,
                        scope_name,
                        call.lineno,
                        target,
                        target_verified,
                    )
                    boundaries.append(boundary)
                    if target not in stamped and target not in exemptions:
                        violations.append(
                            f"{source.name}:{scope_name}:{call.lineno}: target {target} "
                            "is not stamped"
                        )
                    if (
                        source.name not in exemptions
                        and target not in exemptions
                        and not target_verified
                    ):
                        violations.append(
                            f"{source.name}:{scope_name}:{call.lineno}: target {target} "
                            "is not verified"
                        )
        if source_has_boundary and source.name not in stamped and source.name not in exemptions:
            violations.append(f"{source.name}: loader is not stamped")

    return GuardReport(
        tuple(boundaries),
        frozenset(boundary.target for boundary in boundaries if boundary.target),
        tuple(sorted(set(violations))),
    )


def _load_module(tools: Path, name: str):
    path = tools / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"t0493_{name}_{id(path)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_tools(tmp_path: Path, *names: str) -> Path:
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(TOOLS / "repo_owned_files.py", tools / "repo_owned_files.py")
    for name in names:
        if name == "repo_owned_files":
            continue
        shutil.copy2(TOOLS / f"{name}.py", tools / f"{name}.py")
    return tools


def _stale_source() -> str:
    return 'ENGINE_REV = "v0.0.0-stale"\n'


def _make_target_stale(path: Path) -> None:
    """Keep the central seam executable while changing only its baked rev."""
    if path.name == "repo_owned_files.py":
        source = path.read_text(encoding="utf-8")
        source, count = re.subn(
            r'^ENGINE_REV = "[^"]+"',
            'ENGINE_REV = "v0.0.0-stale"',
            source,
            count=1,
            flags=re.MULTILINE,
        )
        assert count == 1
        path.write_text(source, encoding="utf-8")
    else:
        path.write_text(_stale_source(), encoding="utf-8")


def _module_assignment(tree: ast.Module, name: str) -> ast.Assign | ast.AnnAssign:
    matches = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            matches.append(node)
    assert len(matches) == 1, f"module assignment lookup failed: {name}"
    return matches[0]


def _bootstrap_block_hashes(tools: Path) -> dict[str, str]:
    """실측 bootstrap 소비자의 공통 블록을 source byte 범위로 해시한다."""
    hashes: dict[str, str] = {}
    for source in sorted(tools.glob("*.py"), key=lambda path: path.name):
        text = source.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(source))
        starts = []
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "_TOOLS_BOOTSTRAP"
                for target in targets
            ):
                starts.append(node)
        if not starts:
            continue
        assert len(starts) == 1, source
        bootstrap_try = next(
            node for node in tree.body
            if isinstance(node, ast.Try) and node.lineno > starts[0].lineno
        )
        lines = text.splitlines(keepends=True)
        block = "".join(lines[starts[0].lineno - 1:bootstrap_try.end_lineno])
        hashes[source.name] = hashlib.sha256(block.encode("utf-8")).hexdigest()
    return hashes


def _assert_bootstrap_blocks_identical(tools: Path) -> None:
    hashes = _bootstrap_block_hashes(tools)
    report = collect_central_guard_report(tools)
    consumers = {source for source, _function, _line in report.loader_calls}
    assert set(hashes) == consumers
    assert len(set(hashes.values())) == 1, hashes


def _set_literal_mapping_value(path: Path, name: str, key, value) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignment = _module_assignment(tree, name)
    assert isinstance(assignment.value, ast.Dict)
    for index, key_node in enumerate(assignment.value.keys):
        if ast.literal_eval(key_node) == key:
            assignment.value.values[index] = ast.Constant(value=value)
            break
    else:
        raise AssertionError(f"mapping key lookup failed: {name}[{key!r}]")
    ast.fix_missing_locations(tree)
    path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")


def _rename_literal_gate_and_remove_member(
    path: Path, old_name: str, new_name: str, member: str | None,
) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignment = _module_assignment(tree, old_name)
    value = assignment.value
    if isinstance(value, ast.Call):
        assert len(value.args) == 1
        value = value.args[0]
    assert isinstance(value, (ast.Set, ast.Tuple, ast.List))
    if member is not None:
        value.elts = [
            item for item in value.elts
            if not (isinstance(item, ast.Constant) and item.value == member)
        ]

    class Rename(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name):
            if node.id == old_name:
                return ast.copy_location(ast.Name(id=new_name, ctx=node.ctx), node)
            return node

    tree = Rename().visit(tree)
    ast.fix_missing_locations(tree)
    path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")


def _move_central_spec_call_to_same_named_scope(path: Path, nested_source: str) -> None:
    """Move the sole file-location call out of the direct module-level loader node."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _spec_aliases(tree)
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_spec_call(node, aliases)
    ]
    assert len(matches) == 1
    original = matches[0]

    class RemoveOriginal(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call):
            if node is original:
                return ast.copy_location(ast.Constant(value=None), node)
            return self.generic_visit(node)

    tree = RemoveOriginal().visit(tree)
    tree.body.extend(ast.parse(nested_source).body)
    ast.fix_missing_locations(tree)
    path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")


def test_canonical_deep_import_invariant_is_green():
    report = collect_central_guard_report(TOOLS)
    assert not report.violations, "\n".join(report.violations)
    assert [
        (source, function) for source, function, _line in report.spec_calls
    ] == [_CENTRAL_LOADER]
    assert report.loader_calls


def test_legacy_deep_import_oracle_is_clean_on_canonical_tree():
    report = collect_guard_report(TOOLS)
    assert not report.violations, "\n".join(report.violations)


def test_ast_target_set_is_measured_not_frozen_to_ticket_snapshot():
    report = collect_central_guard_report(TOOLS)
    consumers = {source for source, _function, _line in report.loader_calls}
    bootstrap_consumers = set()
    for source in TOOLS.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        if any(
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name)
                and target.id == "_TOOLS_BOOTSTRAP_KEY"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
            for node in tree.body
        ):
            bootstrap_consumers.add(source.name)
    assert consumers == bootstrap_consumers
    assert "repo_owned_files.py" not in consumers
    assert {"pm_delegate.py", "pm_update.py", "worktree_pool.py"} <= consumers


def test_all_measured_bootstrap_blocks_are_byte_identical():
    _assert_bootstrap_blocks_identical(TOOLS)


def test_bootstrap_identity_guard_detects_one_consumer_drift(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    board = tools / "board.py"
    text = board.read_text(encoding="utf-8")
    needle = "_TOOLS_BOOTSTRAP_SENTINEL = object()"
    assert needle in text
    board.write_text(
        text.replace(needle, needle + "  # local drift", 1), encoding="utf-8"
    )

    with pytest.raises(AssertionError):
        _assert_bootstrap_blocks_identical(tools)


def test_bootstrap_ignores_preloaded_same_name_module(monkeypatch):
    fake = types.ModuleType("repo_owned_files")
    fake.__file__ = "/unrelated/worktree/repo_owned_files.py"

    def fail_if_reused(*_args, **_kwargs):
        raise AssertionError("preloaded same-name module was reused")

    fake.load_module = fail_if_reused
    monkeypatch.setitem(sys.modules, "repo_owned_files", fake)
    board = _load_module(TOOLS, "board")
    assert callable(board._load_module_from_path)
    assert sys.modules["repo_owned_files"] is fake
    bootstrap_key = (
        "_project_manager_repo_owned_files_bootstrap:"
        f"{(TOOLS / 'repo_owned_files.py').resolve()}"
    )
    assert Path(sys.modules[bootstrap_key].__file__).resolve() == (
        TOOLS / "repo_owned_files.py"
    ).resolve()


def test_bootstrap_cache_is_isolated_by_resolved_tools_path(tmp_path):
    modules = []
    keys = []
    try:
        for label in ("left", "right"):
            tools = tmp_path / label / ".project_manager" / "tools"
            tools.mkdir(parents=True)
            # board는 import 시점에 identity_args·file_lock을 바인딩한다(load-bearing 형제).
            for name in (
                "board.py", "identity_args.py", "file_lock.py", "repo_owned_files.py",
            ):
                shutil.copy2(TOOLS / name, tools / name)
            module = _load_module(tools, "board")
            modules.append(module)
            key = (
                "_project_manager_repo_owned_files_bootstrap:"
                f"{(tools / 'repo_owned_files.py').resolve()}"
            )
            keys.append(key)
            assert Path(module._load_module_from_path.__code__.co_filename).resolve() == (
                tools / "repo_owned_files.py"
            ).resolve()
        assert keys[0] != keys[1]
        assert sys.modules[keys[0]] is not sys.modules[keys[1]]
    finally:
        for key in keys:
            sys.modules.pop(key, None)


def test_bootstrap_fallback_restores_path_and_same_name_module_on_error(
    tmp_path, monkeypatch,
):
    tools = _copy_tools(tmp_path, "board", "identity_args", "file_lock")
    seam = tools / "repo_owned_files.py"
    seam.write_text("def broken(:\n", encoding="utf-8")
    fake = types.ModuleType("repo_owned_files")
    fake.__file__ = "/unrelated/repo_owned_files.py"
    monkeypatch.setitem(sys.modules, "repo_owned_files", fake)
    before_path = list(sys.path)
    board = _load_module(tools, "board")

    with pytest.raises(RuntimeError, match="pm-update로.*재동기화"):
        board._load_module_from_path(
            seam, "repo_owned_files.py", allow_unverified=True,
        )

    assert sys.path == before_path
    assert sys.modules["repo_owned_files"] is fake
    assert board._TOOLS_BOOTSTRAP_KEY not in sys.modules


def test_manifest_ships_central_loader_before_every_consumer():
    pm_update = _load_module(TOOLS, "pm_update")
    seam = ".project_manager/tools/repo_owned_files.py"
    consumers = []
    for source in TOOLS.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        if any(
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name)
                and target.id == "_TOOLS_BOOTSTRAP_KEY"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
            for node in tree.body
        ):
            consumers.append(f".project_manager/tools/{source.name}")
    manifests = [
        REPO / ".project_manager" / "engine.manifest",
        *(REPO / "templates" / flavor / ".project_manager" / "engine.manifest"
          for flavor in ("claude_code", "codex", "opencode")),
    ]
    for path in manifests:
        manifest = [str(entry) for entry in pm_update.read_manifest(path)]
        seam_index = manifest.index(seam)
        assert all(seam_index < manifest.index(consumer) for consumer in consumers), path


@pytest.mark.parametrize(
    "seam_state", ("old", "missing", "syntax_error", "empty", "partial_api")
)
def test_interrupted_update_new_pm_update_with_unusable_seam_recovers(
    tmp_path, seam_state,
):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source_tools = source / ".project_manager" / "tools"
    dest_tools = dest / ".project_manager" / "tools"
    source_tools.mkdir(parents=True)
    dest_tools.mkdir(parents=True)
    manifest_text = (
        ".project_manager/tools/repo_owned_files.py\n"
        ".project_manager/tools/pm_update.py\n"
        ".project_manager/tools/console_encoding.py\n"
    )
    for root in (source, dest):
        (root / ".project_manager" / "engine.manifest").write_text(
            manifest_text, encoding="utf-8"
        )
    for name in ("repo_owned_files.py", "pm_update.py", "console_encoding.py"):
        shutil.copy2(TOOLS / name, source_tools / name)
    shutil.copy2(TOOLS / "pm_update.py", dest_tools / "pm_update.py")
    shutil.copy2(TOOLS / "console_encoding.py", dest_tools / "console_encoding.py")
    dest_seam = dest_tools / "repo_owned_files.py"
    if seam_state == "old":
        old_tree = ast.parse(
            (TOOLS / "repo_owned_files.py").read_text(encoding="utf-8")
        )
        old_tree.body = [
            node
            for node in old_tree.body
            if not isinstance(node, ast.FunctionDef) or node.name != "load_module"
        ]
        ast.fix_missing_locations(old_tree)
        dest_seam.write_text(ast.unparse(old_tree) + "\n", encoding="utf-8")
    elif seam_state == "syntax_error":
        dest_seam.write_text("def broken(:\n", encoding="utf-8")
    elif seam_state == "empty":
        dest_seam.write_text("", encoding="utf-8")
    elif seam_state == "partial_api":
        source_text = (TOOLS / "repo_owned_files.py").read_text(encoding="utf-8")
        source_tree = ast.parse(source_text)
        load_module = next(
            node for node in source_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "load_module"
        )
        partial = "".join(source_text.splitlines(keepends=True)[:load_module.end_lineno])
        ast.parse(partial)
        assert "def load_module(" in partial
        assert "def _real_git_runner(" not in partial
        dest_seam.write_text(partial, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(dest_tools / "pm_update.py"),
            "--from",
            str(source),
        ],
        cwd=dest,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "[recovery-first]" in result.stdout
    assert "def load_module(" in (
        dest_tools / "repo_owned_files.py"
    ).read_text(encoding="utf-8")


def test_recovery_first_rejects_symlink_source(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source_tools = source / ".project_manager" / "tools"
    dest_tools = dest / ".project_manager" / "tools"
    source_tools.mkdir(parents=True)
    dest_tools.mkdir(parents=True)
    manifest_text = (
        ".project_manager/tools/repo_owned_files.py\n"
        ".project_manager/tools/pm_update.py\n"
        ".project_manager/tools/console_encoding.py\n"
    )
    for root in (source, dest):
        (root / ".project_manager" / "engine.manifest").write_text(
            manifest_text, encoding="utf-8"
        )
    payload = tmp_path / "external_payload.py"
    payload.write_text((TOOLS / "repo_owned_files.py").read_text(encoding="utf-8"))
    (source_tools / "repo_owned_files.py").symlink_to(payload)
    for name in ("pm_update.py", "console_encoding.py"):
        shutil.copy2(TOOLS / name, source_tools / name)
        shutil.copy2(TOOLS / name, dest_tools / name)
    dest_seam = dest_tools / "repo_owned_files.py"
    dest_seam.write_text("def broken(:\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(dest_tools / "pm_update.py"),
            "--from",
            str(source),
        ],
        cwd=dest,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 1
    assert "중앙 로더 선복구 실패" in result.stderr
    assert dest_seam.read_text(encoding="utf-8") == "def broken(:\n"


def test_pm_update_main_preserves_original_error_when_seam_reload_fails(monkeypatch):
    pm_update = _load_module(TOOLS, "pm_update")
    original = RuntimeError("original update failure")

    def fail_update(_argv):
        raise original

    def fail_seam_reload():
        raise SyntaxError("broken central loader")

    monkeypatch.setattr(pm_update, "_main", fail_update)
    monkeypatch.setattr(pm_update, "_load_repo_owned_files", fail_seam_reload)

    with pytest.raises(RuntimeError, match="original update failure") as caught:
        pm_update.main([])

    assert caught.value is original
    assert caught.value.__context__ is None


def test_pm_update_main_preserves_original_error_when_seam_lacks_error_type(
    monkeypatch,
):
    pm_update = _load_module(TOOLS, "pm_update")
    original = RuntimeError("original update failure")
    stub = types.SimpleNamespace(
        ENGINE_REV="stub-rev",
        load_module=lambda *args, **kwargs: None,
        _real_git_runner=lambda _cwd: None,
        list_repo_owned_entries=lambda *args, **kwargs: [],
        list_repo_owned_files=lambda *args, **kwargs: [],
        TRACKED_ONLY="tracked_only",
        OWNED="owned",
    )
    assert pm_update._missing_repo_owned_files_api(stub) == ["RepoFilesGitError"]
    original_load = pm_update._load_module_from_path

    def load_stub(path, expected_filename, **kwargs):
        if expected_filename == _CENTRAL_LOADER[0]:
            return stub
        return original_load(path, expected_filename, **kwargs)

    def fail_update(_argv):
        raise original

    monkeypatch.setattr(pm_update, "_load_module_from_path", load_stub)
    monkeypatch.setattr(pm_update, "_main", fail_update)

    with pytest.raises(RuntimeError, match="original update failure") as caught:
        pm_update.main([])

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value is original
    assert caught.value.__context__ is None
    assert rendered.count("Traceback (most recent call last):") == 1
    assert "During handling of the above exception" not in rendered


def test_mutation_unregistered_unverified_call_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    engine_path = tools / "engine_rev.py"
    tree = ast.parse(engine_path.read_text(encoding="utf-8"))
    assignment = _module_assignment(tree, "EXEMPT_UNVERIFIED_DEEP_IMPORTS")
    assert isinstance(assignment.value, ast.Dict)
    assignment.value.keys = []
    assignment.value.values = []
    ast.fix_missing_locations(tree)
    engine_path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")

    report = collect_central_guard_report(tools)
    assert any(
        "board.py" in item and "code-owned exemption" in item
        for item in report.violations
    )


def test_mutation_noop_verifier_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    path = tools / "pm_delegate.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    replaced = False
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        for keyword in call.keywords:
            if (
                keyword.arg == "verifier"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "_verify_engine_rev"
            ):
                keyword.value = ast.parse("lambda *_: None", mode="eval").body
                replaced = True
                break
        if replaced:
            break
    assert replaced
    ast.fix_missing_locations(tree)
    path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")

    report = collect_central_guard_report(tools)
    assert any(
        "pm_delegate.py" in item and "effective verifier" in item
        for item in report.violations
    )


def test_mutation_target_missing_from_stamped_modules_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    engine_path = tools / "engine_rev.py"
    tree = ast.parse(engine_path.read_text(encoding="utf-8"))
    assignment = _module_assignment(tree, "STAMPED_MODULES")
    assert isinstance(assignment.value, (ast.Tuple, ast.List, ast.Set))
    assignment.value.elts = [
        node
        for node in assignment.value.elts
        if not (isinstance(node, ast.Constant) and node.value == "external_review.py")
    ]
    ast.fix_missing_locations(tree)
    engine_path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")

    report = collect_central_guard_report(tools)
    assert any(
        "external_review.py" in item and "STAMPED_MODULES" in item
        for item in report.violations
    )


def test_mutation_class_body_deep_import_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    path = tools / "pm_delegate.py"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\nclass _ClassBodyDeepImportProbe:\n"
        "    leaked = importlib.util.spec_from_file_location(\n"
        "        'leak', Path(__file__)\n"
        "    )\n",
        encoding="utf-8",
    )

    report = collect_central_guard_report(tools)
    assert any(
        "pm_delegate.py" in item and "outside central loader" in item
        for item in report.violations
    )


def test_mutation_stamped_sibling_gate_omission_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    _rename_literal_gate_and_remove_member(
        tools / "domain.py", "_STAMPED_SIBLINGS", "_STAMPED_SIBLINGS", "board.py"
    )

    report = collect_central_guard_report(tools)
    assert any(
        "domain.py" in item and "board.py" in item and "verifier gate" in item
        for item in report.violations
    )


def test_exemptions_are_code_owned_and_nonempty():
    engine_rev = _load_module(TOOLS, "engine_rev")
    assert "pm_update.py" in engine_rev.EXEMPT_FROM_STAMP
    assert all(reason.strip() for reason in engine_rev.EXEMPT_FROM_STAMP.values())


def test_mutation_empty_exemption_reason_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    engine_path = tools / "engine_rev.py"
    _set_literal_mapping_value(engine_path, "EXEMPT_FROM_STAMP", "pm_update.py", "")
    report = collect_guard_report(tools)
    assert "empty exemption reason: pm_update.py" in report.violations


def test_bump_round_trip_rewrites_every_newly_measured_target(tmp_path):
    current = _load_module(TOOLS, "engine_rev")
    tools = _copy_tools(
        tmp_path,
        "engine_rev",
        *[Path(filename).stem for filename in current.STAMPED_MODULES],
    )
    isolated = _load_module(tools, "engine_rev")
    next_rev = "v99.0.1"
    newly_measured = {
        "pm_relay.py", "external_review.py", "pm_render.py", "pm_import.py",
        "pm_log.py", "repo_owned_files.py",
    }

    assert isolated.main(["--bump", next_rev]) == 0
    assert {
        filename: isolated.read_literal(tools / filename)
        for filename in newly_measured
    } == dict.fromkeys(newly_measured, next_rev)

    bumped = _load_module(tools, "engine_rev")
    assert bumped.main(["--bump", current.ENGINE_REV]) == 0
    assert {
        filename: bumped.read_literal(tools / filename)
        for filename in newly_measured
    } == dict.fromkeys(newly_measured, current.ENGINE_REV)


def test_unresolved_dynamic_sibling_path_fails_loud(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    source = tools / "dynamic_loader.py"
    source.write_text(
        "import importlib.util\n"
        "from pathlib import Path\n"
        "def _verify_engine_rev(module, filename):\n"
        "    return None\n"
        "def load(filename):\n"
        "    target = Path(__file__).resolve().parent / filename\n"
        "    spec = importlib.util.spec_from_file_location('x', target)\n"
        "    _verify_engine_rev(None, filename)\n"
        "    return spec\n",
        encoding="utf-8",
    )
    report = collect_guard_report(tools)
    assert any("unresolved canonical sibling path" in item for item in report.violations)


def test_mutation_new_unverified_deep_import_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    (tools / "new_sibling.py").write_text("VALUE = 1\n", encoding="utf-8")
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\ndef _t0493_mutant():\n"
          "    path = Path(__file__).resolve().parent / 'new_sibling.py'\n"
          "    return importlib.util.spec_from_file_location('new_sibling', path)\n",
        encoding="utf-8",
    )
    report = collect_guard_report(tools)
    assert any("target new_sibling.py is not stamped" in item for item in report.violations)
    assert any("target new_sibling.py is not verified" in item for item in report.violations)


@pytest.mark.parametrize(
    "loader_source",
    (
        "class Loader:\n"
        "    def load(self):\n"
        "        path = Path(__file__).resolve().parent / 'new_sibling.py'\n"
        "        return importlib.util.spec_from_file_location('new_sibling', path)\n",
        "def outer():\n"
        "    def load():\n"
        "        path = Path(__file__).resolve().parent / 'new_sibling.py'\n"
        "        return importlib.util.spec_from_file_location('new_sibling', path)\n"
        "    return load\n",
    ),
    ids=("class-method", "nested-function"),
)
def test_mutation_new_nested_scope_boundary_is_red(tmp_path, loader_source):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    (tools / "new_sibling.py").write_text("VALUE = 1\n", encoding="utf-8")
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\n"
        + loader_source,
        encoding="utf-8",
    )

    report = collect_guard_report(tools)

    assert any(
        boundary.function == "load" and boundary.target == "new_sibling.py"
        for boundary in report.boundaries
    )
    assert any("target new_sibling.py is not verified" in item for item in report.violations)


def test_mutation_verifier_is_paired_with_its_own_target(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\ndef _t0493_two_targets():\n"
          "    board_path = Path(__file__).resolve().parent / 'board.py'\n"
          "    board_spec = importlib.util.spec_from_file_location('board', board_path)\n"
          "    board_mod = importlib.util.module_from_spec(board_spec)\n"
          "    _verify_engine_rev(board_mod, 'board.py')\n"
          "    log_path = Path(__file__).resolve().parent / 'pm_log.py'\n"
          "    log_spec = importlib.util.spec_from_file_location('pm_log', log_path)\n"
          "    return board_spec, log_spec\n",
        encoding="utf-8",
    )

    report = collect_guard_report(tools)
    paired = {
        boundary.target: boundary.verified
        for boundary in report.boundaries
        if boundary.function == "_t0493_two_targets"
    }

    assert paired == {"board.py": True, "pm_log.py": False}
    assert any("target pm_log.py is not verified" in item for item in report.violations)


def test_mutation_verifier_first_argument_must_be_loaded_module(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\ndef _t0493_wrong_verifier_module():\n"
          "    path = Path(__file__).resolve().parent / 'board.py'\n"
          "    spec = importlib.util.spec_from_file_location('board', path)\n"
          "    mod = importlib.util.module_from_spec(spec)\n"
          "    _verify_engine_rev(None, 'board.py')\n"
          "    return mod\n",
        encoding="utf-8",
    )

    report = collect_guard_report(tools)

    assert any(
        "pm_delegate.py:_t0493_wrong_verifier_module:" in item
        and "verifier first argument is not a same-scope module_from_spec result" in item
        for item in report.violations
    )
    assert any(
        boundary.function == "_t0493_wrong_verifier_module"
        and boundary.target == "board.py"
        and not boundary.verified
        for boundary in report.boundaries
    )


@pytest.mark.parametrize(
    "control_flow",
    (
        "    try:\n"
        "        _verify_engine_rev(mod, 'board.py')\n"
        "    except Exception:\n"
        "        pass\n",
        "    for _ in _maybe_empty():\n"
        "        _verify_engine_rev(mod, 'board.py')\n",
    ),
    ids=("try-except-pass", "possibly-zero-iteration-loop"),
)
def test_mutation_verifier_under_unsupported_control_flow_is_red(
    tmp_path, control_flow,
):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\ndef _t0493_control_flow_bypass():\n"
          "    path = Path(__file__).resolve().parent / 'board.py'\n"
          "    spec = importlib.util.spec_from_file_location('board', path)\n"
          "    mod = importlib.util.module_from_spec(spec)\n"
        + control_flow
        + "    return mod\n",
        encoding="utf-8",
    )

    report = collect_guard_report(tools)

    assert any(
        "conditional verifier is unresolved (unsupported conditional control flow)"
        in item
        for item in report.violations
    )
    assert any(
        boundary.function == "_t0493_control_flow_bypass"
        and boundary.target == "board.py"
        and not boundary.verified
        for boundary in report.boundaries
    )


@pytest.mark.parametrize(
    "path_expression",
    (
        "str(Path(__file__).resolve().parent / 'x.py')",
        "os.fspath(Path(__file__).resolve().parent / 'x.py')",
        "Path(__file__).resolve().parent.joinpath('x.py')",
        "Path(__file__).resolve().parent / ('x' + '_c.py')",
    ),
    ids=("str", "os-fspath", "joinpath", "string-concat"),
)
def test_mutation_unresolved_anchored_expression_is_red(tmp_path, path_expression):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    source = tools / "dynamic_loader.py"
    source.write_text(
        "import importlib.util\n"
        "import os\n"
        "from pathlib import Path\n"
        "def load():\n"
        f"    target = {path_expression}\n"
        "    return importlib.util.spec_from_file_location('x', target)\n",
        encoding="utf-8",
    )

    report = collect_guard_report(tools)

    assert any("unresolved canonical sibling path" in item for item in report.violations)


def test_sensitivity_new_outside_file_location_call_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\ndef _outside_boundary(path):\n"
          "    return importlib.util.spec_from_file_location('outside', path)\n",
        encoding="utf-8",
    )
    report = collect_central_guard_report(tools)
    assert any("outside central loader" in item for item in report.violations)


@pytest.mark.parametrize(
    "nested_source",
    (
        "class X:\n"
        "    @staticmethod\n"
        "    def load_module(path):\n"
        "        return importlib.util.spec_from_file_location('x', path)\n",
        "def outer():\n"
        "    def load_module(path):\n"
        "        return importlib.util.spec_from_file_location('x', path)\n"
        "    return load_module\n",
    ),
    ids=("same-named-class-method", "same-named-nested-function"),
)
def test_same_named_nested_scope_cannot_impersonate_central_loader(
    tmp_path, nested_source,
):
    tools = _copy_tools(tmp_path, "engine_rev", "repo_owned_files")
    _move_central_spec_call_to_same_named_scope(
        tools / _CENTRAL_LOADER[0], nested_source,
    )

    central = collect_central_guard_report(tools)
    legacy = collect_guard_report(tools)

    assert len(central.spec_calls) == 1
    assert central.spec_calls[0][1] == _CENTRAL_LOADER[1]
    assert any("outside central loader" in item for item in central.violations)
    assert any(
        boundary.loader == _CENTRAL_LOADER[0]
        and boundary.function == _CENTRAL_LOADER[1]
        for boundary in legacy.boundaries
    )
    assert any(
        item.startswith(f"{_CENTRAL_LOADER[0]}:{_CENTRAL_LOADER[1]}:")
        for item in legacy.violations
    )


def test_literal_gate_is_name_coupled_and_rename_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    _rename_literal_gate_and_remove_member(
        tools / "domain.py", "_STAMPED_SIBLINGS", "_STAMPED_KIN", None
    )

    report = collect_central_guard_report(tools)

    assert len(report.violations) == 1
    violation = report.violations[0]
    assert "domain.py" in violation
    assert "allow_unverified call is not covered" in violation


@pytest.mark.parametrize(
    "condition",
    (
        ast.Constant(value=False),
        ast.parse("os.environ.get('PM_STRICT_REV')").body[0].value,
    ),
    ids=("if-false", "environment-gate"),
)
def test_mutation_nonliteral_conditional_verifier_is_red(tmp_path, condition):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    source = loader.read_text(encoding="utf-8")
    needle = "path, \"pm_relay.py\", verifier=_verify_engine_rev,"
    assert needle in source
    loader.write_text(source.replace(needle, "path, \"pm_relay.py\",", 1), encoding="utf-8")
    report = collect_central_guard_report(tools)
    assert any("exactly one explicit policy" in item for item in report.violations)


def test_mutation_find_repo_root_anchors_fail_loud_in_all_four_modules(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loaders = (
        "pm_delegate.py",
        "external_review.py",
        "ticket_finish.py",
        "contradiction_lint.py",
    )
    mutation = (
        "\ndef _t0493_repo_anchor_mutant(name):\n"
        "    target = REPO / '.project_manager' / 'tools' / (name + '.py')\n"
        "    return importlib.util.spec_from_file_location('x', target)\n"
    )
    for loader_name in loaders:
        path = tools / loader_name
        path.write_text(path.read_text(encoding="utf-8") + mutation, encoding="utf-8")

    report = collect_guard_report(tools)

    for loader_name in loaders:
        assert any(
            item.startswith(f"{loader_name}:")
            and "unresolved canonical sibling path" in item
            for item in report.violations
        )


@pytest.mark.parametrize(
    "anchor_declaration, anchor_expression",
    (
        (
            "def _t0493_anchor_helper():\n"
            "    module_file = __file__\n"
            "    return Path(module_file).resolve().parents[2]\n"
            "_T0493_ROOT = _t0493_anchor_helper()\n",
            "_T0493_ROOT",
        ),
        (
            "class _T0493Anchor:\n"
            "    ROOT = Path(__file__).resolve().parents[2]\n",
            "_T0493Anchor.ROOT",
        ),
        (
            "_T0493_ROOT = Path(sys.modules[__name__].__file__).resolve().parents[2]\n",
            "_T0493_ROOT",
        ),
    ),
    ids=("helper-local", "class-attribute", "sys-modules-file"),
)
def test_mutation_equivalent_canonical_anchor_spellings_are_red(
    tmp_path, anchor_declaration, anchor_expression,
):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\n"
        + anchor_declaration
        + "def _t0493_anchor_spelling(name):\n"
          f"    target = {anchor_expression} / '.project_manager' / 'tools' / (name + '.py')\n"
          "    return importlib.util.spec_from_file_location('x', target)\n",
        encoding="utf-8",
    )

    report = collect_guard_report(tools)

    assert any(
        "pm_delegate.py" in item and "unresolved canonical sibling path" in item
        for item in report.violations
    )


def test_mutation_unstamped_loader_of_stamped_target_is_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    (tools / "unstamped_loader.py").write_text(
        "import importlib.util\n"
        "from pathlib import Path\n"
        "def _verify_engine_rev(module, filename):\n"
        "    return None\n"
        "def load():\n"
        "    path = Path(__file__).resolve().parent / 'board.py'\n"
        "    spec = importlib.util.spec_from_file_location('board', path)\n"
        "    _verify_engine_rev(None, 'board.py')\n"
        "    return spec\n",
        encoding="utf-8",
    )

    report = collect_guard_report(tools)

    assert "unstamped_loader.py: loader is not stamped" in report.violations


def test_mutation_path_reuse_after_spec_does_not_rewrite_call_history(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\ndef _t0493_reused_path():\n"
          "    path = Path(__file__).resolve().parent / 'board.py'\n"
          "    spec = importlib.util.spec_from_file_location('board', path)\n"
          "    path = Path('/tmp/adopter.py')\n"
          "    return spec, path\n",
        encoding="utf-8",
    )

    report = collect_guard_report(tools)

    assert any(
        boundary.function == "_t0493_reused_path"
        and boundary.target == "board.py"
        for boundary in report.boundaries
    )
    assert any("target board.py is not verified" in item for item in report.violations)


def test_spec_keyword_and_unpacking_argument_shapes_are_fail_loud(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\ndef _t0493_keyword_location():\n"
          "    path = Path(__file__).resolve().parent / 'board.py'\n"
          "    return importlib.util.spec_from_file_location(name='x', location=path)\n"
          "def _t0493_unpacking(args):\n"
          "    return importlib.util.spec_from_file_location(*args)\n",
        encoding="utf-8",
    )

    report = collect_guard_report(tools)

    assert any(
        boundary.function == "_t0493_keyword_location"
        and boundary.target == "board.py"
        for boundary in report.boundaries
    )
    assert any(
        "_t0493_unpacking" == boundary.function and boundary.target is None
        for boundary in report.boundaries
    )
    assert any(
        "unresolved spec_from_file_location argument shape" in item
        for item in report.violations
    )


def test_spec_alias_import_and_other_loader_api_bypasses_are_red(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    loader = tools / "pm_delegate.py"
    loader.write_text(
        loader.read_text(encoding="utf-8")
        + "\nfrom importlib.util import spec_from_file_location as _t0493_sfl\n"
          "def _t0493_alias():\n"
          "    path = Path(__file__).resolve().parent / 'board.py'\n"
          "    return _t0493_sfl('board', path)\n"
          "def _t0493_source_loader():\n"
          "    return importlib.machinery.SourceFileLoader('board', 'board.py').load_module()\n"
          "def _t0493_exec_compile(source):\n"
          "    return exec(compile(source, 'board.py', 'exec'))\n",
        encoding="utf-8",
    )

    report = collect_central_guard_report(tools)
    assert any("outside central loader" in item for item in report.violations)
    assert sum(
        "unsupported deep-import API bypass" in item for item in report.violations
    ) == 2


def test_central_loader_requires_exactly_one_policy_and_expected_basename(tmp_path):
    loader = _load_module(TOOLS, "repo_owned_files")
    target = tmp_path / "sample.py"
    target.write_text("VALUE = 510\n", encoding="utf-8")

    with pytest.raises(ValueError, match="requires verifier"):
        loader.load_module(target, "sample.py")
    with pytest.raises(ValueError, match="not both"):
        loader.load_module(
            target,
            "sample.py",
            verifier=lambda _module, _filename: None,
            allow_unverified=True,
        )
    with pytest.raises(ValueError, match="filename mismatch"):
        loader.load_module(target, "other.py", allow_unverified=True)


def test_central_loader_verification_is_load_bearing(tmp_path):
    loader = _load_module(TOOLS, "repo_owned_files")
    target = tmp_path / "verified.py"
    target.write_text("VALUE = 510\n", encoding="utf-8")
    observed = []

    def verify(module, filename):
        observed.append((module.VALUE, filename))

    loaded = loader.load_module(target, "verified.py", verifier=verify)
    assert loaded.VALUE == 510
    assert observed == [(510, "verified.py")]

    def reject(_module, _filename):
        raise RuntimeError("verification red")

    with pytest.raises(RuntimeError, match="verification red"):
        loader.load_module(target, "verified.py", verifier=reject)


def test_arbitrary_name_isolated_copy_bootstraps_without_pythonpath(tmp_path):
    tools = tmp_path / "isolated" / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for name in ("pm_log.py", "repo_owned_files.py", "console_encoding.py"):
        shutil.copy2(TOOLS / name, tools / name)
    probe = (
        "import importlib.util\n"
        "from pathlib import Path\n"
        f"path = Path({str(tools / 'pm_log.py')!r})\n"
        "spec = importlib.util.spec_from_file_location('arbitrary_isolated_name', path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "console = module._load_module_from_path(\n"
        "    path.with_name('console_encoding.py'),\n"
        "    'console_encoding.py',\n"
        "    verifier=module._verify_engine_rev,\n"
        ")\n"
        "assert callable(console.configure_console_utf8)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_parameter_loaders_are_measured_and_hook_exemption_is_code_owned():
    report = collect_central_guard_report(TOOLS)
    measured = {(source, function) for source, function, _line in report.loader_calls}
    assert {
        ("delegate_scope.py", "ticket_touches"),
        ("ticket_finish.py", "count_board_done"),
        ("ticket_finish.py", "get_ticket_title"),
        ("ticket_finish.py", "_ticket_frontmatter"),
        ("ticket_finish.py", "_load_tool_module"),
    } <= measured
    assert ("board.py", "_run_lint_hooks") in measured
    engine_rev = _load_module(TOOLS, "engine_rev")
    assert engine_rev.EXEMPT_UNVERIFIED_DEEP_IMPORTS[
        ("board.py", "_run_lint_hooks")
    ].strip()


def test_ast_ignores_comments_strings_and_vendor_template_copies(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    decoy = tools / "decoy.py"
    decoy.write_text(
        "# spec_from_file_location('fake', Path(__file__).parent / 'fake.py')\n"
        "TEXT = \"spec_from_file_location('fake', 'fake.py')\"\n",
        encoding="utf-8",
    )
    vendor = (
        tmp_path / "templates" / "codex" / ".project_manager" / "tools" / "bad.py"
    )
    vendor.parent.mkdir(parents=True)
    vendor.write_text(
        "import importlib.util\n"
        "importlib.util.spec_from_file_location('x', unknown)\n",
        encoding="utf-8",
    )
    assert not collect_central_guard_report(tools).violations


def test_guard_is_portable_and_needs_no_git_or_wiki(tmp_path):
    tools = _copy_tools(tmp_path, *[path.stem for path in TOOLS.glob("*.py")])
    assert _portable_name(r"C:\project\.project_manager\tools\pm_relay.py") == "pm_relay.py"
    assert not (tmp_path / ".git").exists()
    assert not (tmp_path / ".project_manager" / "wiki").exists()
    assert not collect_central_guard_report(tools).violations


_LOADER_CASES = (
    (
        "pm_delegate", "external_review", lambda mod, _tools: mod._load_external_review(),
        "_load_external_review",
    ),
    (
        "pm_delegate", "pm_relay", lambda mod, _tools: mod._load_relay(),
        "_load_relay",
    ),
    (
        "external_review", "pm_relay", lambda mod, _tools: mod._load_relay(),
        "_load_relay",
    ),
    (
        "pm_import", "board", lambda mod, _tools: mod._detected_py(),
        "_detected_py",
    ),
    (
        "pm_import", "pm_render", lambda mod, _tools: mod._load_pm_render_module(),
        "_load_pm_render_module",
    ),
    (
        "pm_import", "pm_relay", lambda mod, _tools: mod._load_watchdog(),
        "_load_watchdog",
    ),
    (
        "pm_import", "repo_owned_files", lambda mod, _tools: mod._load_repo_owned_files(),
        "_load_repo_owned_files",
    ),
    (
        "pm_bootstrap", "pm_log", lambda mod, _tools: mod._load_tool("pm_log"),
        "_load_tool",
    ),
    (
        "pm_bootstrap", "pm_config", lambda mod, _tools: mod._load_tool("pm_config"),
        "_load_tool",
    ),
    (
        "pm_config", "pm_import", lambda mod, _tools: mod._load_module("pm_import", "pm_import.py"),
        "_load_module",
    ),
    (
        "domain", "repo_owned_files", lambda mod, _tools: mod._load_repo_owned_files(),
        "_load_repo_owned_files",
    ),
    (
        "worktree_pool", "repo_owned_files", lambda mod, _tools: mod._load_repo_owned_files(),
        "_load_repo_owned_files",
    ),
)


@pytest.mark.parametrize(
    ("loader_name", "target_name", "call", "_loader_function"),
    _LOADER_CASES,
)
def test_each_added_loader_guard_rejects_stale_sibling(
    tmp_path, loader_name, target_name, call, _loader_function,
):
    names = {loader_name, target_name}
    if target_name == "repo_owned_files":
        names.add("engine_rev")
    if loader_name == "worktree_pool":
        # import 시점 바인딩 형제 — identity_args(ADR-0057)·file_lock(T-0565·리스 장부 락).
        names.update(("identity_args", "file_lock"))
    tools = _copy_tools(tmp_path, *sorted(names))
    _make_target_stale(tools / f"{target_name}.py")
    loader = _load_module(tools, loader_name)

    with pytest.raises(RuntimeError) as exc:
        call(loader, tools)

    assert getattr(exc.value, "_engine_rev_skew", False) is True
    assert f"{target_name}.py" in str(exc.value)


def _remove_verifier_from_function(path: Path, function_name: str) -> None:
    """AST-mutate one loader verifier without depending on source formatting."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    assert len(functions) == 1, f"loader function lookup failed: {function_name}"
    verifier_statements = [
        node for node in ast.walk(functions[0])
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and _is_verifier_call(node.value)
    ]
    verifier_keywords = [
        keyword
        for node in ast.walk(functions[0])
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "verifier"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "_verify_engine_rev"
    ]
    assert verifier_statements or verifier_keywords, f"verifier lookup failed: {function_name}"
    for statement in verifier_statements:
        statement.value = ast.Constant(value=None)
    for keyword in verifier_keywords:
        keyword.value = ast.Constant(value=None)
    ast.fix_missing_locations(tree)
    path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")


def _remove_skew_reraise_from_function(path: Path, function_name: str) -> None:
    """AST-mutate the marked-skew branch without depending on except formatting/comments."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    assert len(functions) == 1
    matches = [
        node for node in ast.walk(functions[0])
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Call)
        and _called_name(node.test) == "_is_engine_rev_skew"
        and any(isinstance(statement, ast.Raise) for statement in ast.walk(node))
    ]
    assert matches, f"skew re-raise lookup failed: {function_name}"
    for node in matches:
        node.body = [ast.copy_location(ast.Pass(), node.body[0])]
    ast.fix_missing_locations(tree)
    path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("loader_name", "target_name", "call", "loader_function"),
    _LOADER_CASES,
)
def test_sensitivity_removing_each_loader_guard_defeats_the_red_oracle(
    tmp_path, loader_name, target_name, call, loader_function,
):
    """Each production guard is load-bearing: deleting it makes its stale-sibling oracle fail."""
    names = {loader_name, target_name}
    if target_name == "repo_owned_files":
        names.add("engine_rev")
    if loader_name == "worktree_pool":
        # import 시점 바인딩 형제 — identity_args(ADR-0057)·file_lock(T-0565·리스 장부 락).
        names.update(("identity_args", "file_lock"))
    tools = _copy_tools(tmp_path, *sorted(names))
    loader_path = tools / f"{loader_name}.py"
    _remove_verifier_from_function(loader_path, loader_function)
    _make_target_stale(tools / f"{target_name}.py")
    loader = _load_module(tools, loader_name)

    try:
        call(loader, tools)
    except Exception as exc:  # the domain wrapper remains loud, but must lose the skew marker
        assert getattr(exc, "_engine_rev_skew", False) is not True


def test_pm_import_cached_repo_owned_files_is_verified(monkeypatch):
    pm_import = _load_module(TOOLS, "pm_import")
    path = Path(pm_import.__file__).resolve().with_name("repo_owned_files.py").resolve()
    module_name = f"_project_manager_repo_owned_files:{path}"
    stale = types.ModuleType(module_name)
    stale.ENGINE_REV = "v0.0.0-stale"
    monkeypatch.setitem(sys.modules, module_name, stale)

    with pytest.raises(RuntimeError) as exc:
        pm_import._load_repo_owned_files()

    assert getattr(exc.value, "_engine_rev_skew", False) is True
    assert "repo_owned_files.py" in str(exc.value)


@pytest.mark.parametrize(
    "helper_state",
    ("old", "missing", "syntax_error", "incompatible_signature"),
)
def test_pm_update_cli_recovery_survives_unusable_engine_rev(
    tmp_path, helper_state,
):
    root = tmp_path / helper_state
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for name in ("pm_update", "repo_owned_files", "console_encoding"):
        shutil.copy2(TOOLS / f"{name}.py", tools / f"{name}.py")
    if helper_state == "old":
        (tools / "engine_rev.py").write_text(
            "MIN_PYTHON = (3, 11)\n",
            encoding="utf-8",
        )
    elif helper_state == "syntax_error":
        (tools / "engine_rev.py").write_text(
            "def broken(:\n",
            encoding="utf-8",
        )
    elif helper_state == "incompatible_signature":
        (tools / "engine_rev.py").write_text(
            "def load_repo_owned_files(path):\n    return None\n",
            encoding="utf-8",
        )
    (root / "README.md").write_text("recovery fixture\n", encoding="utf-8")
    (root / ".project_manager" / "engine.manifest").write_text(
        "README.md\n",
        encoding="utf-8",
    )

    help_result = subprocess.run(
        [sys.executable, str(tools / "pm_update.py"), "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    dry_run_result = subprocess.run(
        [
            sys.executable,
            str(tools / "pm_update.py"),
            "--from",
            str(root),
            "--dry-run",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert help_result.returncode == 0, help_result.stderr
    assert dry_run_result.returncode == 0, dry_run_result.stderr


def test_pm_import_old_engine_rev_seam_failure_has_resync_message(tmp_path):
    tools = _copy_tools(tmp_path, "pm_import", "repo_owned_files")
    (tools / "engine_rev.py").write_text(
        "MIN_PYTHON = (3, 11)\n",
        encoding="utf-8",
    )
    pm_import = _load_module(tools, "pm_import")

    with pytest.raises(
        RuntimeError,
        match="repo_owned_files.py를 로드할 수 없음.*pm-update로 재동기화",
    ) as exc:
        pm_import._load_repo_owned_files()

    assert isinstance(exc.value.__cause__, AttributeError)


def test_pm_update_exempt_cache_is_reverified_by_domain(tmp_path):
    tools = _copy_tools(
        tmp_path,
        "engine_rev",
        "pm_update",
        "pm_import",
        "domain",
        "repo_owned_files",
    )
    target = tools / "repo_owned_files.py"
    _make_target_stale(target)
    path = target.resolve()
    module_name = f"_project_manager_repo_owned_files:{path}"
    sys.modules.pop(module_name, None)
    try:
        pm_update = _load_module(tools, "pm_update")
        cached = pm_update._load_repo_owned_files()
        assert cached.ENGINE_REV == "v0.0.0-stale"
        assert sys.modules[module_name] is cached

        pm_import = _load_module(tools, "pm_import")
        with pytest.raises(RuntimeError) as import_exc:
            pm_import._load_repo_owned_files()

        assert getattr(import_exc.value, "_engine_rev_skew", False) is True
        assert "repo_owned_files.py" in str(import_exc.value)
        assert module_name not in sys.modules

        cached = pm_update._load_repo_owned_files()
        assert cached.ENGINE_REV == "v0.0.0-stale"
        assert sys.modules[module_name] is cached

        domain = _load_module(tools, "domain")
        with pytest.raises(RuntimeError) as exc:
            domain._load_repo_owned_files()

        assert getattr(exc.value, "_engine_rev_skew", False) is True
        assert "repo_owned_files.py" in str(exc.value)
        assert module_name not in sys.modules
    finally:
        sys.modules.pop(module_name, None)


def test_pm_import_exec_failure_removes_partial_cache(tmp_path):
    tools = _copy_tools(tmp_path, "engine_rev", "pm_import", "repo_owned_files")
    target = tools / "repo_owned_files.py"
    pm_import = _load_module(tools, "pm_import")
    target.write_text("raise RuntimeError('exec boom')\n", encoding="utf-8")
    path = target.resolve()
    module_name = f"_project_manager_repo_owned_files:{path}"
    sys.modules.pop(module_name, None)

    with pytest.raises(
        RuntimeError,
        match="repo_owned_files.py를 로드할 수 없음.*pm-update로 재동기화",
    ) as exc:
        pm_import._load_repo_owned_files()

    assert isinstance(exc.value.__cause__, RuntimeError)
    assert str(exc.value.__cause__) == "exec boom"
    assert module_name not in sys.modules


def _handler_reraises_marked_skew(handler: ast.ExceptHandler) -> bool:
    return _handler_reraises_verifier_skew(handler)


@pytest.mark.parametrize(
    ("function_name", "loader_name"),
    (
        ("_release_alloc_lease_failsoft", "_load_worktree_pool"),
        ("_protected_retry_command", "_load_board"),
        ("_protected_hook_wired", "_load_worktree_pool"),
    ),
)
def test_pm_bootstrap_failsoft_consumers_reraise_marked_skew(
    function_name, loader_name,
):
    tree = ast.parse(
        (TOOLS / "pm_bootstrap.py").read_text(encoding="utf-8"),
        filename=str(TOOLS / "pm_bootstrap.py"),
    )
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    assert len(functions) == 1
    guarded_handlers: list[ast.ExceptHandler] = []
    for try_node in (
        node for node in ast.walk(functions[0]) if isinstance(node, ast.Try)
    ):
        try_calls = [
            call
            for statement in try_node.body
            for call in ast.walk(statement)
            if isinstance(call, ast.Call)
        ]
        if any(_called_name(call) == loader_name for call in try_calls):
            guarded_handlers.extend(try_node.handlers)

    assert guarded_handlers, f"{function_name}: {loader_name} consumer try not found"
    assert all(_handler_reraises_marked_skew(handler) for handler in guarded_handlers)


@pytest.mark.parametrize(
    ("loader_name", "target_name", "call", "function_name"),
    (
        (
            "pm_import", "board", lambda mod: mod._detected_py(),
            "_detected_py",
        ),
        (
            "pm_import", "pm_render", lambda mod: mod._load_pm_render_module(),
            "_load_pm_render_module",
        ),
        (
            "domain", "repo_owned_files", lambda mod: mod._load_repo_owned_files(),
            "_load_repo_owned_files",
        ),
    ),
)
def test_sensitivity_removing_each_failsoft_reraise_loses_skew(
    tmp_path, loader_name, target_name, call, function_name,
):
    names = {loader_name, target_name}
    if loader_name == "pm_import" or target_name == "repo_owned_files":
        names.add("engine_rev")
    tools = _copy_tools(tmp_path, *sorted(names))
    loader_path = tools / f"{loader_name}.py"
    _remove_skew_reraise_from_function(loader_path, function_name)
    _make_target_stale(tools / f"{target_name}.py")
    loader = _load_module(tools, loader_name)

    try:
        call(loader)
    except Exception as exc:
        assert getattr(exc, "_engine_rev_skew", False) is not True


def test_external_review_failsoft_consumer_reraises_only_skew(monkeypatch):
    external_review = _load_module(TOOLS, "external_review")
    skew = RuntimeError("nested relay skew")
    skew._engine_rev_skew = True

    def raise_skew(*_args, **_kwargs):
        raise skew

    monkeypatch.setattr(external_review, "_reviewer_idle_timeout", raise_skew)
    with pytest.raises(RuntimeError, match="nested relay skew") as exc:
        external_review._run_reviewer_ex(
            "prompt", "reviewer", 1, lambda *_a, **_kw: None,
        )
    assert getattr(exc.value, "_engine_rev_skew", False) is True


def test_sensitivity_removing_external_review_reraise_swallows_skew(tmp_path, monkeypatch):
    tools = _copy_tools(tmp_path, "external_review")
    path = tools / "external_review.py"
    source = path.read_text(encoding="utf-8")
    block = (
        "        if _is_engine_rev_skew(exc):\n"
        "            raise\n"
        "        if getattr(exc, \"process_cleanup_failed\", False) is True:\n"
    )
    replacement = "        if getattr(exc, \"process_cleanup_failed\", False) is True:\n"
    assert block in source
    path.write_text(source.replace(block, replacement, 1), encoding="utf-8")
    mutant = _load_module(tools, "external_review")
    skew = RuntimeError("nested relay skew")
    skew._engine_rev_skew = True

    def raise_skew(*_args, **_kwargs):
        raise skew

    monkeypatch.setattr(mutant, "_reviewer_idle_timeout", raise_skew)
    # 산출물은 두 채널 구조다(T-0563) — 진단 본문은 회신 채널에 실린다. started=False 는 이
    # 실패 지점이 kwargs 준비(=스폰 경계 앞)라서다(T-0590 R3) — 이 테스트가 보는 것은 skew 가
    # 일반 실행 오류로 **삼켜졌다**는 사실이고, started 는 그 지점의 사실을 그대로 싣는다.
    assert mutant._run_reviewer_ex(
        "prompt", "reviewer", 1, lambda *_a, **_kw: None,
    ) == (False, mutant.ReviewerOutput("[리뷰어 실행 오류: nested relay skew]", ""), False)
