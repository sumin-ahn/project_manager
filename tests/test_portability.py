"""Windows portability — 인터프리터 해석 + init py 탐지 단위 테스트 (T-0016).

크로스플랫폼 분기를 deterministic 하게 본다: os.name·venv 경로 존재·shutil.which 를
monkeypatch 로 흔들어 4조합(_default_python)·3분기(_detect_py)를 검증한다.
라이브 subprocess·외부 호출 0 — 순수 경로 로직만.

도구들은 패키지가 아니므로 importlib 로 경로 로드하고(test_engine_smoke 와 동일),
모듈 전역(REPO·os·sys·shutil)을 monkeypatch 해 분기를 강제한다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# _default_python() 을 동일 헬퍼로 보유하는 세 도구 — 공통적으로 4조합을 검증.
DEFAULT_PYTHON_TOOLS = ("pm_bootstrap", "pm_handoff", "ticket_finish")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_repo(tmp_path: Path, *, venv: bool, nt: bool) -> Path:
    """tmp REPO 를 만들고, venv=True 면 해당 플랫폼 인터프리터 파일을 생성한다."""
    repo = tmp_path / "repo"
    repo.mkdir()
    if venv:
        rel = "Scripts/python.exe" if nt else "bin/python"
        target = repo / "venv" / rel
        target.parent.mkdir(parents=True)
        target.write_text("", encoding="utf-8")
    return repo


# ── _default_python() — 4조합 (os.name × venv 존재) ────────────────────────

@pytest.mark.parametrize("name", DEFAULT_PYTHON_TOOLS)
def test_default_python_posix_venv_present(monkeypatch, tmp_path, name):
    """posix + venv 존재 → venv/bin/python (이 머신 현행 보존)."""
    mod = _load(name)
    repo = _make_repo(tmp_path, venv=True, nt=False)
    monkeypatch.setattr(mod, "REPO", repo)
    monkeypatch.setattr(mod.os, "name", "posix")
    assert mod._default_python() == str(repo / "venv" / "bin" / "python")


@pytest.mark.parametrize("name", DEFAULT_PYTHON_TOOLS)
def test_default_python_nt_venv_present(monkeypatch, tmp_path, name):
    """nt + venv 존재 → venv/Scripts/python.exe."""
    mod = _load(name)
    repo = _make_repo(tmp_path, venv=True, nt=True)
    monkeypatch.setattr(mod, "REPO", repo)
    monkeypatch.setattr(mod.os, "name", "nt")
    assert mod._default_python() == str(repo / "venv" / "Scripts" / "python.exe")


@pytest.mark.parametrize("name", DEFAULT_PYTHON_TOOLS)
def test_default_python_posix_venv_absent_falls_back(monkeypatch, tmp_path, name):
    """posix + venv 부재 → sys.executable 폴백."""
    mod = _load(name)
    repo = _make_repo(tmp_path, venv=False, nt=False)
    monkeypatch.setattr(mod, "REPO", repo)
    monkeypatch.setattr(mod.os, "name", "posix")
    monkeypatch.setattr(mod.sys, "executable", "/fake/sys/python")
    assert mod._default_python() == "/fake/sys/python"


@pytest.mark.parametrize("name", DEFAULT_PYTHON_TOOLS)
def test_default_python_nt_venv_absent_falls_back(monkeypatch, tmp_path, name):
    """nt + venv 부재 → sys.executable 폴백 (Scripts/python.exe 없음)."""
    mod = _load(name)
    repo = _make_repo(tmp_path, venv=False, nt=True)
    monkeypatch.setattr(mod, "REPO", repo)
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setattr(mod.sys, "executable", "/fake/sys/python")
    assert mod._default_python() == "/fake/sys/python"


# ── board._detect_py() — which monkeypatch 3분기 ───────────────────────────

@pytest.fixture(scope="module")
def board():
    return _load("board")


def test_detect_py_prefers_python3(monkeypatch, board):
    """python3 가 PATH 에 있으면 bare 명령 'python3' 채택 (which 절대경로 아님·리눅스 현행 보존).

    T-0022 후 _detect_py 는 후보를 `_interp_runs` 로 실행검증하고 Windows 는 `py` 1순위다.
    이 which 는 python3 에만 truthy(py·python 은 None)라 OS 무관하게 python3 만 통과한다.
    _interp_runs 를 True 로 stub 해 실 인터프리터 비의존. (os.name 은 안 건드림 — posix
    강제는 Windows pathlib 을 깨뜨림.)
    """
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: True)
    monkeypatch.setattr(
        board.shutil, "which",
        lambda cmd: "/usr/bin/python3" if cmd == "python3" else None,
    )
    assert board._detect_py() == "python3"


def test_detect_py_falls_back_to_python(monkeypatch, board):
    """py·python3 부재·python 존재 → bare 'python' (which 가 python 에만 truthy·_interp_runs stub)."""
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: True)
    monkeypatch.setattr(
        board.shutil, "which",
        lambda cmd: r"C:\Python\python.exe" if cmd == "python" else None,
    )
    assert board._detect_py() == "python"


def test_detect_py_literal_fallback_when_neither(monkeypatch, board):
    """python3·python 둘 다 부재 → 'python3' 리터럴 폴백."""
    monkeypatch.setattr(board.shutil, "which", lambda cmd: None)
    assert board._detect_py() == "python3"


# ── board init 이 탐지된 py 를 local.conf 에 실제로 기록하는지 ─────────────

def test_init_writes_detected_py_to_local_conf(monkeypatch, tmp_path, board):
    """init 이 local.conf 의 py= 에 _detect_py() 결과를 박는지 (tmp REPO + which monkeypatch)."""
    local_conf = tmp_path / "local.conf"
    monkeypatch.setattr(board, "LOCAL_CONF", local_conf)
    # 부수 파일 생성·훅 설치·외부리뷰 프롬프트는 이 단언과 무관 — 무력화.
    monkeypatch.setattr(board, "PM_STATE_FILE", tmp_path / "pm_state.md")
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE", tmp_path / "no_such_template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "prompt_external_review_optin", lambda: None)
    # python3 가 PATH 에 있는 환경 → _detect_py() 는 bare 'python3' 를 기록한다.
    # (which 가 python3 에만 truthy + _interp_runs stub 으로 머신·OS 무관 결정화 — T-0022.)
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: True)
    monkeypatch.setattr(
        board.shutil, "which",
        lambda cmd: "/usr/bin/python3" if cmd == "python3" else None,
    )

    import argparse
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="tester")
    rc = board.cmd_init(args)

    assert rc == 0
    written = local_conf.read_text(encoding="utf-8")
    assert "py=python3\n" in written
