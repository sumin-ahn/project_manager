"""Canonical 엔진의 텍스트 쓰기는 플랫폼과 무관하게 개행 정책을 명시한다."""

# 가드 경계: `_ALLOWED`로 공용 래퍼 정의를 예외 처리하면 해당 래퍼의 호출부 검사를
# 반드시 함께 둔다. 예외가 검사 없는 우회로가 되어서는 안 된다.
# `NamedTemporaryFile(mode=...)`은 임시 파일 생명주기까지, `partial(open, ...)`은 생성된
# callable의 흐름까지 추적해야 해 이 단순 AST 가드의 대상이 아니다. 2026-08-17 독립 AST/grep
# 실측에서 canonical 엔진의 두 형태 사용은 모두 0건이므로, 오탐 위험이 큰 흐름 분석은 넣지 않는다.
# 같은 이유로 미대상인 형태가 셋 더 있다(T-0691 R3 실측 · canonical 엔진 사용 0건):
#   - 대입 별칭 `_x = os.fdopen` 뒤 `_x(fd, "w")` — 값 흐름 추적이 필요하다(from-import·모듈 별칭은 잡는다).
#   - `codecs.open(path, "w", encoding=...)` — mode 가 두 번째 위치 인자라 이 스캐너의 mode 해소와 다르고
#     엔진의 codecs 사용은 lookup/decoder 뿐이다.
#   - 다른 모듈에서 attribute 로 부르는 공용 래퍼(`pu._fdopen_text(...)`)는 `_call_name` 이 잡는다(아래).
# "주석에 없으면 잡힌다" 로 읽지 마라 — 새 형태를 엔진에 들이면 이 목록과 스캐너를 함께 갱신한다.

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


_ALLOWED: set[tuple[str, int, str]] = {
    (
        "pm_import.py",
        2496,
        "_fdopen_text는 mode/newline을 호출자에게서 받아 전달하는 공용 래퍼다.",
    ),
    (
        "pm_import.py",
        2505,
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
    if call.func.attr in {"_fdopen_text", "_fdopen_binary"}:
        # `import pm_import as pu; pu._fdopen_text(...)` — 공용 래퍼의 attribute 호출도 호출부 검사 대상.
        return call.func.attr
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


def _load_engine_module(name: str):
    spec = importlib.util.spec_from_file_location(f"_newline_probe_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 개행 **표기 판정**은 엔진 세 모듈에 각자 작은 원시 함수로 있다(모듈 간 import 를 늘리지 않으려는
# 선택). 규칙이 갈리면 같은 파일을 pm_import 는 CRLF 로, pm_update 는 LF 로 되써 표기 보존이
# 반쪽이 되므로, 규칙 일치를 이 가드가 못박는다 — 다수결, 동수면 첫 등장, 개행 0 이면 LF.
_NEWLINE_CASES = {
    "": "\n",
    "개행 없는 한 줄": "\n",
    "a\nb\n": "\n",
    "a\r\nb\r\n": "\r\n",
    "a\r\nb\nc\r\n": "\r\n",
    "a\nb\r\nc\n": "\n",
    "a\r\nb\n": "\r\n",   # 동수 → 첫 등장이 CRLF
    "a\nb\r\n": "\n",     # 동수 → 첫 등장이 LF
    "a\rb\r": "\n",       # lone CR 은 개행 표기로 세지 않는다
}


def test_newline_notation_primitives_agree_across_engine_modules():
    implementations = {
        "pm_import.dominant_newline": _load_engine_module("pm_import").dominant_newline,
        "pm_update._dominant_newline": _load_engine_module("pm_update")._dominant_newline,
        "board._dominant_newline": _load_engine_module("board")._dominant_newline,
    }
    for text, expected in _NEWLINE_CASES.items():
        for label, implementation in implementations.items():
            assert implementation(text) == expected, (
                f"{label} 이 표기 규칙에서 갈렸다: {text!r} → {implementation(text)!r} "
                f"(기대 {expected!r})")


def test_newline_preserving_round_trip_keeps_bytes(tmp_path):
    """`read_text_preserving_newline` → `write_text_preserving_newline` 왕복은 bytes 를 보존한다."""
    pm_import = _load_engine_module("pm_import")
    for payload in (b"a\r\nb\r\n", b"a\nb\n", b"a\r\nb\r\nc", b""):
        path = tmp_path / "sample.txt"
        path.write_bytes(payload)
        text, newline = pm_import.read_text_preserving_newline(path)
        assert "\r\n" not in text, "판정용 본문은 LF 정규화여야 한다"
        pm_import.write_text_preserving_newline(path, text, newline)
        assert path.read_bytes() == payload, f"왕복이 bytes 를 바꿨다: {payload!r}"
