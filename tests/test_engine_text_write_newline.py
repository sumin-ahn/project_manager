"""Canonical 엔진의 텍스트 쓰기는 플랫폼과 무관하게 개행 정책을 명시한다."""

# 가드 경계: `_ALLOWED`로 공용 래퍼 정의를 예외 처리하면 해당 래퍼의 호출부 검사를
# 반드시 함께 둔다. 예외가 검사 없는 우회로가 되어서는 안 된다.
# `NamedTemporaryFile(mode=...)`은 임시 파일 생명주기까지, `partial(open, ...)`은 생성된
# callable의 흐름까지 추적해야 해 이 단순 AST 가드의 대상이 아니다. 2026-08-17 독립 AST/grep
# 실측에서 canonical 엔진의 두 형태 사용은 모두 0건이므로, 오탐 위험이 큰 흐름 분석은 넣지 않는다.

from __future__ import annotations

import ast
from pathlib import Path


_ALLOWED: set[tuple[str, int, str]] = {
    (
        "pm_import.py",
        2434,
        "_fdopen_text는 mode/newline을 호출자에게서 받아 전달하는 공용 래퍼다.",
    ),
    (
        "pm_import.py",
        2443,
        "_fdopen_binary는 binary mode 전용 공용 래퍼다.",
    ),
}

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _call_name(
    call: ast.Call,
    *,
    fdopen_aliases: set[str] | frozenset[str] = frozenset(),
    os_aliases: set[str] | frozenset[str] = frozenset({"os"}),
) -> str | None:
    if isinstance(call.func, ast.Name):
        if call.func.id == "open":
            return "open"
        if call.func.id in {"_fdopen_text", "_fdopen_binary"}:
            return call.func.id
        if call.func.id in fdopen_aliases:
            return "os.fdopen"
        return None
    if not isinstance(call.func, ast.Attribute):
        return None
    if isinstance(call.func.value, ast.Name):
        if call.func.value.id in os_aliases and call.func.attr == "fdopen":
            return "os.fdopen"
        if (call.func.value.id, call.func.attr) == ("io", "open"):
            return "io.open"
        if call.func.value.id in os_aliases and call.func.attr == "open":
            return None
    if call.func.attr == "open":
        return "Path.open"
    return None


def _literal_mode(call: ast.Call, call_name: str) -> str | None:
    positional_index = 0 if call_name == "Path.open" else 1
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


def _is_text_write_mode(mode: str) -> bool:
    return "b" not in mode and (any(flag in mode for flag in "wax") or "+" in mode)


def _import_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    fdopen_aliases: set[str] = set()
    os_aliases = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            fdopen_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "fdopen"
            )
        elif isinstance(node, ast.Import):
            os_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "os"
            )
    return fdopen_aliases, os_aliases


def _valid_newline(call: ast.Call) -> bool:
    newline_node = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "newline"),
        None,
    )
    if newline_node is None:
        return False
    try:
        newline = ast.literal_eval(newline_node)
    except (ValueError, TypeError):
        return False
    return newline in {"\n", ""}


def _parsed_call(source: str) -> ast.Call:
    expression = ast.parse(source, mode="eval").body
    assert isinstance(expression, ast.Call)
    return expression


def _scan_text_writes() -> tuple[
    list[tuple[str, int, str, bool]],
    list[tuple[str, int, str]],
]:
    calls: list[tuple[str, int, str, bool]] = []
    unresolved_modes: list[tuple[str, int, str]] = []
    for path in sorted(TOOLS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fdopen_aliases, os_aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kind: str | None = None
            if isinstance(node.func, ast.Attribute) and node.func.attr == "write_text":
                kind = "write_text"
            else:
                call_name = _call_name(
                    node,
                    fdopen_aliases=fdopen_aliases,
                    os_aliases=os_aliases,
                )
                if call_name is None:
                    continue
                mode = _literal_mode(node, call_name)
                if mode is None:
                    unresolved_modes.append((path.name, node.lineno, call_name))
                    continue
                if _is_text_write_mode(mode):
                    kind = call_name
            if kind is not None:
                calls.append((
                    path.name,
                    node.lineno,
                    kind,
                    _valid_newline(node),
                ))
    return calls, unresolved_modes


def test_engine_text_writes_declare_newline():
    path_open = _parsed_call('dest.open("w", encoding="utf-8")')
    assert _call_name(path_open) == "Path.open"
    assert _literal_mode(path_open, "Path.open") == "w"
    assert _call_name(_parsed_call('os.open("dest", os.O_WRONLY)')) is None
    assert _call_name(
        _parsed_call('open_fd(fd, "w", encoding="utf-8")'),
        fdopen_aliases={"open_fd"},
    ) == "os.fdopen"
    fdopen_aliases, os_aliases = _import_aliases(ast.parse(
        "from os import fdopen as open_fd\nimport os as operating_system\n"
    ))
    assert fdopen_aliases == {"open_fd"}
    assert os_aliases == {"os", "operating_system"}
    assert _call_name(
        _parsed_call('operating_system.open("dest", os.O_WRONLY)'),
        os_aliases=os_aliases,
    ) is None
    assert _call_name(_parsed_call('_fdopen_text(fd, "w")')) == "_fdopen_text"
    assert _is_text_write_mode("r+")
    assert not _is_text_write_mode("rb+")
    assert not _valid_newline(path_open)
    assert not _valid_newline(
        _parsed_call('dest.open("w", encoding="utf-8", newline=None)')
    )
    assert _valid_newline(
        _parsed_call('dest.open("w", encoding="utf-8", newline="")')
    )
    assert not _valid_newline(_parsed_call('_fdopen_text(fd, "w")'))
    unresolved_mode = _parsed_call(
        'open("dest", mode=WRITE_MODE, encoding="utf-8", newline="\\n")'
    )
    assert _literal_mode(unresolved_mode, "open") is None

    calls, unresolved_modes = _scan_text_writes()
    assert calls, "canonical .project_manager/tools/*.py 텍스트 쓰기 스캔 대상이 0건이다"

    allowed_locations = {(filename, lineno) for filename, lineno, _reason in _ALLOWED}
    unresolved = [
        f"{filename}:{lineno} ({kind})"
        for filename, lineno, kind in unresolved_modes
        if (filename, lineno) not in allowed_locations
    ]
    assert not unresolved, (
        "텍스트 쓰기 여부를 판정할 수 없는 비리터럴 mode 호출이다; "
        "리터럴 mode로 바꾸거나 _ALLOWED에 사유와 함께 등재하라:\n"
        + "\n".join(unresolved)
    )
    missing = [
        f"{filename}:{lineno} ({kind})"
        for filename, lineno, kind, valid_newline in calls
        if not valid_newline and (filename, lineno) not in allowed_locations
    ]
    assert not missing, (
        '텍스트 쓰기의 newline은 리터럴 "\\n" 또는 ""여야 한다:\n'
        + "\n".join(missing)
    )
