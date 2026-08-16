"""tests/의 Path 텍스트 I/O가 locale 기본 encoding에 의존하지 않는지 검사한다."""
from __future__ import annotations

import ast
from pathlib import Path


TESTS = Path(__file__).resolve().parent


def _missing_encoding_lines(source: str, *, filename: str) -> list[int]:
    """한 Python 소스에서 locale 기본값에 의존하는 Path 텍스트 I/O 행을 찾는다."""
    offenders: list[int] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"read_text", "write_text"}:
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        positional_encoding = (
            node.func.attr == "read_text" and len(node.args) >= 1
        ) or (
            node.func.attr == "write_text" and len(node.args) >= 2
        )
        if not positional_encoding:
            offenders.append(node.lineno)
    return offenders


def test_path_read_write_text_calls_declare_encoding():
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(TESTS.parent)}:{lineno}"
            for lineno in _missing_encoding_lines(source, filename=str(path))
        )
    assert offenders == [], "encoding= 누락 Path 텍스트 I/O:\n" + "\n".join(offenders)


def test_path_text_io_guard_is_sensitive_to_missing_encoding():
    source = "from pathlib import Path\nPath('x').write_text('payload')\n"
    assert _missing_encoding_lines(source, filename="synthetic_violation.py") == [2]
