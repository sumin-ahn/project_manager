"""board._detect_py() Windows 견고화 단위 테스트 (T-0022).

`_detect_py()` 가 한국어 Windows 류 환경에서 *실제로 작동하는* 인터프리터를 고르는지
검증한다: Windows 는 `python` 1순위(직접 인터프리터라 스크립트 shebang 무시 — `py`
런처는 `py board.py` 에서 shebang 을 읽어 엉뚱한 버전으로 디스패치), 각 후보는
`shutil.which` 존재 + `--version` 실행검증을 모두 통과해야 채택(죽은 shim 회피),
POSIX 는 현행 `python3` 보존.

board.py 는 패키지가 아니므로 importlib 로 경로 로드하고(test_portability 와 동일),
모듈 전역(os·shutil)과 `_interp_runs` 를 monkeypatch 해 분기를 deterministic 하게 강제한다.
라이브 subprocess·실 인터프리터 비의존 — 순수 분기 로직만.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def board():
    return _load("board")


def _patch(monkeypatch, board, *, name: str, present: set[str], runs: set[str]):
    """os.name·shutil.which·_interp_runs 를 deterministic 하게 흔든다.

    present = `shutil.which` 가 경로를 돌려주는 명령 집합(존재).
    runs    = `_interp_runs` 가 True 를 돌려주는 명령 집합(실행 성공).
    둘의 차집합(존재하지만 실행 실패)이 '죽은 shim'.
    """
    monkeypatch.setattr(board.os, "name", name)
    monkeypatch.setattr(
        board.shutil, "which",
        lambda cmd: f"/fake/{cmd}" if cmd in present else None,
    )
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: cmd in runs)


# ── 1. Windows: python 1순위 (모두 존재·실행 → "python", 옛 코드는 "python3") ─
#     python 은 직접 인터프리터라 shebang 무시 → py 런처보다 일관·안전.

def test_nt_prefers_python(monkeypatch, board):
    _patch(monkeypatch, board, name="nt",
           present={"py", "python3", "python"}, runs={"py", "python3", "python"})
    assert board._detect_py() == "python"


# ── 2a. Windows: python 부재 → 차선 `py` 런처 (python3 보다 우선) ────────────

def test_nt_falls_to_py_when_python_absent(monkeypatch, board):
    _patch(monkeypatch, board, name="nt",
           present={"py", "python3"},  # python 부재
           runs={"py", "python3"})
    assert board._detect_py() == "py"


# ── 2b. Windows: python 이 죽은 shim(실행 실패) → py 로 건너뜀 ───────────────

def test_nt_skips_dead_python_shim(monkeypatch, board):
    _patch(monkeypatch, board, name="nt",
           present={"python", "py", "python3"},  # python 존재하나
           runs={"py", "python3"})               # 실행 실패(죽은 shim) → py 채택
    assert board._detect_py() == "py"


# ── 2c. Windows: python·py 부재, python3 만 → "python3" (최후 후보) ──────────

def test_nt_python3_last_resort(monkeypatch, board):
    _patch(monkeypatch, board, name="nt",
           present={"python3"}, runs={"python3"})
    assert board._detect_py() == "python3"


# ── 3. POSIX: python3 작동 → "python3" (리눅스 현행 보존·py 시도조차 안 함) ──

def test_posix_prefers_python3(monkeypatch, board):
    tried: list[str] = []

    def _which(cmd):
        tried.append(cmd)
        return f"/usr/bin/{cmd}" if cmd in {"python3", "python"} else None

    monkeypatch.setattr(board.os, "name", "posix")
    monkeypatch.setattr(board.shutil, "which", _which)
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: cmd in {"python3", "python"})

    assert board._detect_py() == "python3"
    assert "py" not in tried  # POSIX 후보에 py 가 아예 없음


# ── 4. 아무 후보도 통과 못 함 (which 전부 None) → "python3" 리터럴 폴백 ──────

def test_literal_fallback_when_nothing_passes(monkeypatch, board):
    _patch(monkeypatch, board, name="nt", present=set(), runs=set())
    assert board._detect_py() == "python3"


def test_literal_fallback_when_present_but_all_dead(monkeypatch, board):
    """존재는 하나 전부 실행 실패(죽은 shim) → 폴백 'python3'."""
    _patch(monkeypatch, board, name="nt",
           present={"py", "python3", "python"}, runs=set())
    assert board._detect_py() == "python3"


# ── _interp_runs 자체: rc!=0·예외는 False, rc==0 은 True ────────────────────

def test_interp_runs_false_on_nonzero_rc(monkeypatch, board):
    class _R:
        returncode = 1

    monkeypatch.setattr(board.subprocess, "run", lambda *a, **k: _R())
    assert board._interp_runs("python3") is False


def test_interp_runs_false_on_exception(monkeypatch, board):
    def _boom(*a, **k):
        raise FileNotFoundError("no such interpreter")

    monkeypatch.setattr(board.subprocess, "run", _boom)
    assert board._interp_runs("python3") is False


def test_interp_runs_true_on_zero_rc(monkeypatch, board):
    class _R:
        returncode = 0

    monkeypatch.setattr(board.subprocess, "run", lambda *a, **k: _R())
    assert board._interp_runs("py") is True
