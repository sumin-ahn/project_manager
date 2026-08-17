"""Canonical 엔진의 `os.fsync` 는 쓰기 가능 fd 위에서만 호출한다 (T-0716).

읽기 전용 fd 의 fsync 는 POSIX 에서 성공하고 Windows `_commit()` 은 쓰기 가능 핸들을 요구해
`[Errno 9] Bad file descriptor` 로 거부한다. 리눅스 개발 트리에서 항상 green 인 플랫폼 비대칭이라
정적으로 막는다 — 판정은 fsync 인자를 그 fd/handle 을 만든 open 호출까지 역산해서 내린다
(`os.open` 은 플래그, `open`/`os.fdopen`/`_fdopen_*` 는 mode). 내구성은 쓰기와 같은 자리에서
수행한다 — append 는 `file_lock.append_atomic` 이 자기 쓰기 fd 위에서 sync 한다.

가드 경계: 판정 불능(비리터럴 mode·미해소 플래그·추적 불가 인자)도 red 다. `_ALLOWED` 로
예외 처리하려면 파일·행·사유를 함께 등재해야 하며, 예외가 검사 없는 우회로가 되어서는 안 된다.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest


# (파일명, 행번호, 사유) — 현재 canonical 엔진에는 예외가 없다.
_ALLOWED: set[tuple[str, int, str]] = set()

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 쓰기 접근을 여는 `os.open` 플래그. 이 중 하나도 없으면 그 fd 는 읽기 전용이다.
_WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_APPEND"})
_READ_ONLY_FLAG = "O_RDONLY"
# 엔진의 공용 fdopen 래퍼 — mode 를 호출자에게서 받아 그대로 전달한다.
_FDOPEN_WRAPPERS = frozenset({"_fdopen_text", "_fdopen_binary"})
_WRITE_MODE_CHARS = "wax+"


def _module_aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str], dict[str, str]]:
    """(`os` 모듈 별칭, `fsync` 별칭, `fdopen` 별칭, os 상수 별칭→원래 이름)."""
    os_aliases = {"os"}
    fsync_aliases: set[str] = set()
    fdopen_aliases: set[str] = set()
    flag_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "os"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "fsync":
                    fsync_aliases.add(bound)
                elif alias.name == "fdopen":
                    fdopen_aliases.add(bound)
                elif alias.name.startswith("O_"):
                    flag_aliases[bound] = alias.name
    return os_aliases, fsync_aliases, fdopen_aliases, flag_aliases


def _flag_names(node: ast.AST, flag_aliases: dict[str, str]) -> set[str]:
    """플래그 식에 등장하는 식별자를 모은다 (`os.O_WRONLY` 는 attribute 이름으로)."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(flag_aliases.get(child.id, child.id))
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _flags_writability(node: ast.AST, flag_aliases: dict[str, str]) -> bool | None:
    names = _flag_names(node, flag_aliases)
    if names & _WRITE_FLAGS:
        return True
    if _READ_ONLY_FLAG in names or not names:
        return False    # `O_RDONLY` 는 0 이라 리터럴 0 도 읽기 전용이다.
    return None


def _literal_mode(call: ast.Call, positional_index: int) -> str | None:
    mode_node = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "mode"),
        call.args[positional_index] if len(call.args) > positional_index else None,
    )
    if mode_node is None:
        return "r"
    try:
        mode = ast.literal_eval(mode_node)
    except (ValueError, TypeError):
        return None
    return mode if isinstance(mode, str) else None


def _mode_writability(mode: str | None) -> bool | None:
    if mode is None:
        return None
    return any(flag in mode for flag in _WRITE_MODE_CHARS)


def _call_writability(
    call: ast.Call,
    *,
    os_aliases: set[str],
    fdopen_aliases: set[str],
    flag_aliases: dict[str, str],
) -> bool | None:
    """fd/handle 을 만드는 호출이 쓰기 가능 접근을 여는지 판정한다 (미해소는 None)."""
    func = call.func
    if isinstance(func, ast.Name):
        if func.id in fdopen_aliases or func.id in _FDOPEN_WRAPPERS:
            return _mode_writability(_literal_mode(call, 1))
        if func.id == "open":
            return _mode_writability(_literal_mode(call, 1))
        return None
    if not isinstance(func, ast.Attribute):
        return None
    if isinstance(func.value, ast.Name) and func.value.id in os_aliases:
        if func.attr == "open":
            flags_node = call.args[1] if len(call.args) > 1 else None
            if flags_node is None:
                return None
            return _flags_writability(flags_node, flag_aliases)
        if func.attr == "fdopen":
            return _mode_writability(_literal_mode(call, 1))
        return None
    if func.attr == "open":
        if isinstance(func.value, ast.Name) and func.value.id == "io":
            return _mode_writability(_literal_mode(call, 1))
        return _mode_writability(_literal_mode(call, 0))    # Path.open
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_scopes(
    node: ast.AST, parents: dict[ast.AST, ast.AST],
) -> list[ast.AST]:
    """fsync 호출을 감싸는 함수/모듈 스코프를 안쪽부터 나열한다."""
    scopes: list[ast.AST] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)
        ):
            scopes.append(current)
        current = parents.get(current)
    return scopes


def _scope_bindings(scope: ast.AST) -> dict[str, list[ast.Call]]:
    """스코프 안에서 이름이 어떤 호출 결과에 묶이는지 모은다 (대입·with as)."""
    bindings: dict[str, list[ast.Call]] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.withitem):
            if isinstance(node.optional_vars, ast.Name) and isinstance(
                node.context_expr, ast.Call
            ):
                bindings.setdefault(
                    node.optional_vars.id, []
                ).append(node.context_expr)
    return bindings


class _SourceAnalysis:
    """한 모듈의 fsync 호출을 그 fd/handle 의 open 호출까지 역산한다."""

    def __init__(self, source: str, filename: str) -> None:
        self.filename = filename
        self.tree = ast.parse(source, filename=filename)
        (
            self.os_aliases,
            self.fsync_aliases,
            self.fdopen_aliases,
            self.flag_aliases,
        ) = _module_aliases(self.tree)
        self.parents = _parents(self.tree)
        self._bindings: dict[int, dict[str, list[ast.Call]]] = {}

    def _bindings_of(self, scope: ast.AST) -> dict[str, list[ast.Call]]:
        cached = self._bindings.get(id(scope))
        if cached is None:
            cached = _scope_bindings(scope)
            self._bindings[id(scope)] = cached
        return cached

    def _call_writability(self, call: ast.Call) -> bool | None:
        return _call_writability(
            call,
            os_aliases=self.os_aliases,
            fdopen_aliases=self.fdopen_aliases,
            flag_aliases=self.flag_aliases,
        )

    def _name_writability(self, name: str, node: ast.AST) -> bool | None:
        for scope in _enclosing_scopes(node, self.parents):
            calls = self._bindings_of(scope).get(name)
            if not calls:
                continue
            verdicts = [self._call_writability(call) for call in calls]
            if any(verdict is None for verdict in verdicts):
                return None
            return all(verdicts)
        return None

    def _argument_writability(self, argument: ast.AST, node: ast.AST) -> bool | None:
        if isinstance(argument, ast.Call):
            if (
                isinstance(argument.func, ast.Attribute)
                and argument.func.attr == "fileno"
            ):
                receiver = argument.func.value
                if isinstance(receiver, ast.Call):
                    return self._call_writability(receiver)
                if isinstance(receiver, ast.Name):
                    return self._name_writability(receiver.id, node)
                return None
            return self._call_writability(argument)
        if isinstance(argument, ast.Name):
            return self._name_writability(argument.id, node)
        return None

    def _is_fsync_call(self, node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name):
            return node.func.id in self.fsync_aliases
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "fsync"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.os_aliases
        )

    def calls(self) -> list[tuple[str, int, bool | None]]:
        """(파일명, 행번호, 쓰기 가능 판정) — None 은 판정 불능."""
        found: list[tuple[str, int, bool | None]] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call) or not self._is_fsync_call(node):
                continue
            argument = node.args[0] if node.args else None
            verdict = (
                None if argument is None
                else self._argument_writability(argument, node)
            )
            found.append((self.filename, node.lineno, verdict))
        return found


def _scan_source(source: str, filename: str = "<snippet>") -> list[
    tuple[str, int, bool | None]
]:
    return _SourceAnalysis(source, filename).calls()


def _scan_tools() -> list[tuple[str, int, bool | None]]:
    calls: list[tuple[str, int, bool | None]] = []
    for path in sorted(TOOLS.glob("*.py")):
        calls.extend(
            _scan_source(path.read_text(encoding="utf-8"), path.name)
        )
    return calls


def _fsync_call_count_by_token(source: str) -> int:
    """AST 와 독립된 대조 — 주석·문자열을 뺀 코드 토큰에서 `fsync(` 를 센다."""
    tokens = [
        token for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type in {tokenize.NAME, tokenize.OP, tokenize.NUMBER}
    ]
    return sum(
        1 for index, token in enumerate(tokens[:-1])
        if token.type == tokenize.NAME
        and token.string == "fsync"
        and tokens[index + 1].string == "("
    )


def test_engine_fsync_calls_target_writable_descriptors():
    calls = _scan_tools()
    assert calls, "canonical .project_manager/tools/*.py 의 fsync 스캔 대상이 0건이다"

    allowed_locations = {(filename, lineno) for filename, lineno, _reason in _ALLOWED}
    read_only = [
        f"{filename}:{lineno}"
        for filename, lineno, writable in calls
        if writable is False and (filename, lineno) not in allowed_locations
    ]
    assert not read_only, (
        "읽기 전용 fd 의 fsync 다 (Windows `[Errno 9] Bad file descriptor`); "
        "쓰기 fd 위에서 sync 하라:\n" + "\n".join(read_only)
    )
    unresolved = [
        f"{filename}:{lineno}"
        for filename, lineno, writable in calls
        if writable is None and (filename, lineno) not in allowed_locations
    ]
    assert not unresolved, (
        "fsync 대상 fd 의 접근 모드를 판정할 수 없다; 리터럴 플래그/mode 로 열거나 "
        "_ALLOWED 에 사유와 함께 등재하라:\n" + "\n".join(unresolved)
    )


def test_fsync_guard_sees_every_engine_call_site():
    """가드 시야 == 실제 표면 — AST 방문 수를 토큰 실측과 파일별로 대조한다."""
    by_ast: dict[str, int] = {}
    for filename, _lineno, _writable in _scan_tools():
        by_ast[filename] = by_ast.get(filename, 0) + 1
    by_token: dict[str, int] = {}
    for path in sorted(TOOLS.glob("*.py")):
        count = _fsync_call_count_by_token(path.read_text(encoding="utf-8"))
        if count:
            by_token[path.name] = count

    assert by_ast == by_token, "AST 시야와 토큰 실측이 어긋난다"
    assert by_ast, "엔진에 fsync 호출이 한 건도 없다 — 내구성 표면 소실"
    # append 내구성은 공용 seam 이 소유한다 (호출부 재-open 금지·T-0716).
    assert by_ast.get("file_lock.py") == 1


@pytest.mark.parametrize(
    ("label", "snippet", "expected"),
    (
        (
            "readonly-open",
            "import os\ndef f(p):\n"
            "    fd = os.open(p, os.O_RDONLY)\n    os.fsync(fd)\n",
            False,
        ),
        (
            "literal-zero-flags",
            "import os\ndef f(p):\n    fd = os.open(p, 0)\n    os.fsync(fd)\n",
            False,
        ),
        (
            "append-open",
            "import os\ndef f(p):\n"
            "    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
            "    os.fsync(fd)\n",
            True,
        ),
        (
            "rdwr-open",
            "import os\ndef f(p):\n"
            "    fd = os.open(p, os.O_RDWR)\n    os.fsync(fd)\n",
            True,
        ),
        (
            "aliased-flag-import",
            "import os\nfrom os import O_WRONLY as _W\ndef f(p):\n"
            "    fd = os.open(p, _W)\n    os.fsync(fd)\n",
            True,
        ),
        (
            "aliased-os-module",
            "import os as system\ndef f(p):\n"
            "    fd = system.open(p, system.O_RDONLY)\n    system.fsync(fd)\n",
            False,
        ),
        (
            "from-imported-fsync",
            "import os\nfrom os import fsync\ndef f(p):\n"
            "    fd = os.open(p, os.O_RDONLY)\n    fsync(fd)\n",
            False,
        ),
        (
            "write-handle-fileno",
            "import os\ndef f(fd):\n"
            '    with os.fdopen(fd, "wb", closefd=False) as handle:\n'
            "        os.fsync(handle.fileno())\n",
            True,
        ),
        (
            "read-handle-fileno",
            "import os\ndef f(p):\n"
            '    with open(p, "r", encoding="utf-8") as handle:\n'
            "        os.fsync(handle.fileno())\n",
            False,
        ),
        (
            "fdopen-wrapper-binary",
            "import os\ndef f(p):\n"
            '    with _fdopen_binary(_open_nofollow(p), "wb") as handle:\n'
            "        os.fsync(handle.fileno())\n",
            True,
        ),
        (
            "path-open-write",
            "import os\ndef f(p):\n"
            '    with p.open("w", encoding="utf-8", newline="") as handle:\n'
            "        os.fsync(handle.fileno())\n",
            True,
        ),
        (
            "unresolved-parameter",
            "import os\ndef f(fd):\n    os.fsync(fd)\n",
            None,
        ),
        (
            "unresolved-flags-variable",
            "import os\ndef f(p, flags):\n"
            "    fd = os.open(p, flags)\n    os.fsync(fd)\n",
            None,
        ),
    ),
)
def test_fsync_guard_classifies_descriptor_shapes(label, snippet, expected):
    calls = _scan_source(snippet)
    assert len(calls) == 1, label
    assert calls[0][2] is expected, label


def test_fsync_guard_ignores_mentions_in_comments_and_strings():
    """주석·문자열의 fsync 언급은 세지 않는다 (AST/토큰 판정·문서화 자유)."""
    prose = (
        '"""임시 파일에 write→flush→os.fsync(fd) 후 replace 한다."""\n'
        "# os.fsync(sync_fd)\n"
        'DOC = "os.fsync(fd)"\n'
    )
    assert _scan_source(prose) == []
    assert _fsync_call_count_by_token(prose) == 0


def test_reintroduced_readonly_ledger_sync_is_red():
    """장부 append 뒤 읽기 전용 재-open sync 를 되살리면 가드가 red 로 잡는다 (감도 실증)."""
    source = (TOOLS / "pm_delegate.py").read_text(encoding="utf-8")
    ledger_append = source.split("def _append_ticket_copy_ledger", 1)[1].split(
        "\ndef ", 1
    )[0]
    assert "fsync" not in ledger_append, (
        "장부 append 호출부가 자체 sync 를 들고 있다 — 내구성은 append seam 소유다"
    )
    anchor = "        file_lock.append_atomic(path, payload, mode=0o600)\n"
    mutated = source.replace(
        anchor,
        anchor
        + "        sync_fd = os.open(path, os.O_RDONLY)\n"
        + "        try:\n"
        + "            os.fsync(sync_fd)\n"
        + "        finally:\n"
        + "            os.close(sync_fd)\n",
        1,
    )
    assert mutated != source, "변이 앵커 소실"
    assert all(
        writable is True
        for _filename, _lineno, writable in _scan_source(source, "pm_delegate.py")
    ), "pm_delegate.py 는 쓰기 fd sync 만 남은 상태여야 한다"
    assert any(
        writable is False
        for _filename, _lineno, writable in _scan_source(mutated, "pm_delegate.py")
    )
