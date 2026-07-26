"""엔진 subprocess 텍스트 디코딩 회귀 가드 (T-0468)."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
# 배포본(templates/*)의 엔진 사본도 스캔한다 — parity 가드(byte-identical)로 전이 커버되지만,
# 이 가드 단독으로도 출하물의 로캘 의존 호출 유입을 잡도록 자족화(T-0468 codex 게이트 must-fix).
ENGINE_TOOL_DIRS = [TOOLS, *sorted((REPO / "templates").glob("*/.project_manager/tools"))]


def _load_board():
    spec = importlib.util.spec_from_file_location("board_t0468", TOOLS / "board.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_engine_text_subprocess_calls_declare_encoding():
    """`text=True` 호출은 로캘 기본값 대신 디코딩 인코딩을 반드시 명시한다."""
    missing: list[str] = []
    for path in sorted(p for d in ENGINE_TOOL_DIRS for p in d.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
            text_arg = keywords.get("text")
            if (
                isinstance(text_arg, ast.Constant)
                and text_arg.value is True
                and "encoding" not in keywords
            ):
                missing.append(f"{path.relative_to(REPO)}:{node.lineno}")

    assert not missing, (
        "text=True subprocess 호출에 encoding 명시가 없습니다:\n"
        + "\n".join(missing)
    )


def test_git_rev_parse_decodes_utf8_path_under_cp949_locale_harness(tmp_path):
    """CP949 기본 디코딩은 깨지는 UTF-8 경로도 명시 UTF-8로 정상 반환한다."""
    board = _load_board()
    expected = "worktrees/한글-경로"
    raw_stdout = f"{expected}\n".encode("utf-8")

    with pytest.raises(UnicodeDecodeError):
        raw_stdout.decode("cp949")

    calls: list[dict[str, object]] = []

    def cp949_locale_runner(argv, **kwargs):
        calls.append(kwargs)
        encoding = kwargs.get("encoding") or "cp949"
        errors = kwargs.get("errors") or "strict"
        stdout = raw_stdout.decode(str(encoding), str(errors))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    result = board._git_rev_parse(
        tmp_path, "--git-common-dir", runner=cp949_locale_runner
    )

    assert result == expected
    assert calls == [{
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
    }]


def test_git_rev_parse_does_not_swallow_unicode_decode_error(tmp_path):
    """예상 밖 디코딩 오류는 misanchor 가드를 조용히 건너뛰지 않는다."""
    board = _load_board()

    def broken_decoder(argv, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    with pytest.raises(UnicodeDecodeError):
        board._git_rev_parse(tmp_path, "--git-dir", runner=broken_decoder)
