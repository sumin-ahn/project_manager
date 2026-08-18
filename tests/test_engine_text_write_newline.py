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
#
# `_ALLOWED`는 (파일, 함수명, 호출 형태) 심볼 키다(T-0748) — 라인 번호가 아니다. `pm_import.py`는
# 22 모듈 동일 부트스트랩 블록의 소비자라 그 블록을 고치면 아래쪽 모든 정의의 라인이 밀린다;
# 라인 핀이었다면 그때마다 이 allowlist가 무관하게 red 였다(T-0746 실측). 함수 소속은 AST로
# 판정하므로 함수 본문 내부에서의 라인 이동엔 무감하다 — `_fdopen_text`/`_fdopen_binary` 자체의
# 이름을 바꾸거나 그 안의 `os.fdopen` 호출 형태(예: attribute 경유)를 바꿀 때만 아래 항목을
# 함께 고친다.

from __future__ import annotations

import ast
import importlib.util
from collections import Counter
from pathlib import Path


_ALLOWED: set[tuple[str, str, str, str]] = {
    (
        "pm_import.py",
        "_fdopen_text",
        "os.fdopen",
        "_fdopen_text는 mode/newline을 호출자에게서 받아 전달하는 공용 래퍼다.",
    ),
    (
        "pm_import.py",
        "_fdopen_binary",
        "os.fdopen",
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


def _enclosing_function_names(tree: ast.AST) -> dict[int, str]:
    """`id(노드)` → 그 노드를 직접 담은 최근접 함수 정의 이름(모듈 최상위는 `"<module>"`).

    라인이 아니라 함수 소속으로 호출부를 식별하기 위한 보조 지도다(T-0748) — 같은 함수 안에서
    코드가 위아래로 밀려도 이 매핑은 바뀌지 않는다. 중첩 함수는 부모가 아니라 자신의 이름을
    받는다(가장 가까운 정의가 실제 호출부 소속이다).

    알려진 근사: `ast.iter_child_nodes(FunctionDef)`는 `decorator_list`·`args`(기본값 표현식)도
    자식으로 내려주므로, 데코레이터 인자나 파라미터 기본값 안의 호출은 실제로는 정의 시점에
    **감싸는 스코프**에서 평가되는데도 이 함수는 그 함수 자신의 이름으로 귀속한다. 스캔 대상
    (`.project_manager/tools/*.py`)에 그런 형태가 0건이라(실측) 하강 제외까지는 넣지 않는다 —
    새로 생기면 이 근사부터 의심한다.
    """
    names: dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_current = current
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_current = child.name
            names[id(child)] = child_current
            visit(child, child_current)

    visit(tree, "<module>")
    return names


def _scan_text_writes() -> tuple[
    list[tuple[str, int, str, str, bool]],
    list[tuple[str, int, str, str]],
]:
    calls: list[tuple[str, int, str, str, bool]] = []
    unresolved_modes: list[tuple[str, int, str, str]] = []
    for path in sorted(TOOLS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fdopen_aliases, os_aliases = _import_aliases(tree)
        function_names = _enclosing_function_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = function_names.get(id(node), "<module>")
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
                    unresolved_modes.append(
                        (path.name, node.lineno, function_name, call_name)
                    )
                    continue
                if _is_text_write_mode(mode):
                    kind = call_name
            if kind is not None:
                calls.append((
                    path.name,
                    node.lineno,
                    function_name,
                    kind,
                    _valid_newline(node),
                ))
    return calls, unresolved_modes


def _allowed_key_match_counts(
    allowed_keys: set[tuple[str, str, str]],
    calls: list[tuple[str, int, str, str, bool]],
    unresolved_modes: list[tuple[str, int, str, str]],
) -> dict[tuple[str, str, str], int]:
    """`_ALLOWED` 각 키가 문제 스캔 결과(unresolved mode·missing newline)와 몇 건 매칭되는지 센다.

    허용은 라인이 아니라 (파일, 함수명, 호출 형태) 심볼이므로, 그 심볼이 원래 가리키던 호출이
    사라지면(리네임·삭제) 0건 죽은 항목이 되고, 같은 함수 안에 같은 형태의 새 미검증 호출이
    늘면(T-0748 리뷰 민감도 B2) 2건 이상으로 벌어진다 — 정합은 항상 정확히 1건이다.
    """
    counts: dict[tuple[str, str, str], int] = {key: 0 for key in allowed_keys}
    for filename, _lineno, function_name, call_name in unresolved_modes:
        key = (filename, function_name, call_name)
        if key in counts:
            counts[key] += 1
    for filename, _lineno, function_name, kind, valid_newline in calls:
        if valid_newline:
            continue
        key = (filename, function_name, kind)
        if key in counts:
            counts[key] += 1
    return counts


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

    allowed_keys = {
        (filename, function_name, call_name)
        for filename, function_name, call_name, _reason in _ALLOWED
    }
    match_counts = _allowed_key_match_counts(allowed_keys, calls, unresolved_modes)
    mismatched = {key: count for key, count in match_counts.items() if count != 1}
    assert not mismatched, (
        "_ALLOWED 항목이 스캔 결과와 정확히 1건씩 매칭되지 않는다 — 0건은 죽은 항목(제거), "
        "2건 이상은 같은 자리에 새 미검증 텍스트 쓰기가 늘었다는 뜻이다(리터럴 mode/newline으로 "
        "고치거나 별도 사유로 새로 등재하라):\n"
        + "\n".join(f"{key}: {count}건" for key, count in sorted(mismatched.items()))
    )
    unresolved = [
        f"{filename}:{lineno} ({function_name}::{call_name})"
        for filename, lineno, function_name, call_name in unresolved_modes
        if (filename, function_name, call_name) not in allowed_keys
    ]
    assert not unresolved, (
        "텍스트 쓰기 여부를 판정할 수 없는 비리터럴 mode 호출이다; "
        "리터럴 mode로 바꾸거나 _ALLOWED에 (파일, 함수명, 호출 형태) 사유와 함께 등재하라:\n"
        + "\n".join(unresolved)
    )
    missing = [
        f"{filename}:{lineno} ({function_name}::{kind})"
        for filename, lineno, function_name, kind, valid_newline in calls
        if not valid_newline and (filename, function_name, kind) not in allowed_keys
    ]
    assert not missing, (
        '텍스트 쓰기의 newline은 리터럴 "\\n" 또는 ""여야 한다:\n'
        + "\n".join(missing)
    )


def test_enclosing_function_names_resolve_by_ast_not_line_shift():
    """함수 소속 판정은 라인이 아니라 AST 구조를 본다 — 앞에 줄을 끼워 넣어도 안 바뀐다."""
    source = (
        "TOP_LEVEL = call_at_module_scope()\n"
        "\n"
        "def outer():\n"
        "    call_in_outer()\n"
        "    def inner():\n"
        "        call_in_inner()\n"
        "    return inner\n"
    )
    tree = ast.parse(source)
    names = _enclosing_function_names(tree)
    calls = {
        node.func.id: names[id(node)]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert calls == {
        "call_at_module_scope": "<module>",
        "call_in_outer": "outer",
        "call_in_inner": "inner",
    }

    # 위에 줄을 끼워 넣어 모든 라인을 밀어도(부트스트랩 블록 편집이 하는 일) 소속은 그대로다.
    shifted_source = "# padding\n" * 5 + source
    shifted_tree = ast.parse(shifted_source)
    shifted_names = _enclosing_function_names(shifted_tree)
    shifted_calls = {
        node.func.id: shifted_names[id(node)]
        for node in ast.walk(shifted_tree)
        if isinstance(node, ast.Call)
    }
    assert shifted_calls == calls


def test_allowed_symbol_key_flags_dead_entry_and_new_sibling_write():
    """리뷰 민감도 B2: 허용된 함수 안에 새 미검증 쓰기가 늘면(또는 사라지면) 매칭 수가 어긋난다."""
    key = ("pm_import.py", "_fdopen_text", "os.fdopen")
    allowed_keys = {key}

    # 정상 상태 — 원래 그 자리 하나만(unresolved mode 경유) 매칭된다.
    exempted = [("pm_import.py", 2493, "_fdopen_text", "os.fdopen")]
    assert _allowed_key_match_counts(allowed_keys, [], exempted) == {key: 1}

    # 같은 함수 안에 새 os.fdopen(fd, "w") 하나가 더 생기면(리터럴 mode·newline 누락) 2건으로
    # 벌어진다 — 예전 라인-무관 필터는 이 경우를 무탐지했다(T-0748 리뷰 F-003).
    sibling_write = [("pm_import.py", 2500, "_fdopen_text", "os.fdopen", False)]
    assert _allowed_key_match_counts(allowed_keys, sibling_write, exempted) == {key: 2}

    # 호출이 사라지면(리네임·삭제) 0건 — 죽은 항목.
    assert _allowed_key_match_counts(allowed_keys, [], []) == {key: 0}


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
