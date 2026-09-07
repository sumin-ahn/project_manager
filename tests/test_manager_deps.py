"""창 의존 확인과 `--smoke` — 진입점을 격리 서브프로세스로 실제 실행해 잰다 (T-0910).

웹뷰가 없으면 설치 명령 두 줄을 찍고 rc 3 이다(대체 렌더러로 갈아타지 않는다). `--smoke` 는
창을 열어 화면과 브리지를 한 번 확인하고 닫는다 — Linux headless 는 `Gtk.init_check` 가 False 라
`xvfb-run -a` 아래에서만 통과한다.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DIST_INDEX = REPO / "manager" / "ui" / "dist" / "index.html"

_webview_installed = importlib.util.find_spec("webview") is not None
_needs_webview = pytest.mark.skipif(not _webview_installed, reason="pywebview 미설치")


def _run_manager(*argv: str, extra_path: Path | None = None,
                 launcher: list[str] | None = None) -> subprocess.CompletedProcess:
    """`python3 -m manager` 를 격리 서브프로세스로 실행한다."""
    search_path = str(REPO) if extra_path is None else os.pathsep.join(
        [str(extra_path), str(REPO)])
    return subprocess.run(
        [*(launcher or []), sys.executable, "-m", "manager", *argv],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONPATH": search_path}, timeout=120)


def test_version_needs_no_window():
    completed = _run_manager("--version")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("pm-manager v")


@pytest.mark.skipif(sys.platform != "linux", reason="GTK 웹킷 안내는 Linux 분기")
def test_a_missing_gtk_webkit_prints_two_install_lines_and_stops(tmp_path):
    """실 진입점 + 실 import 실패로 잰다 — `gi` 를 못 여는 상자를 PYTHONPATH 로 재현한다."""
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "gi.py").write_text('raise ImportError("gi typelib 없음")\n', encoding="utf-8")

    completed = _run_manager("--smoke", "--registry", str(tmp_path / "projects.json"),
                             extra_path=stub)

    assert completed.returncode == 3, completed.stdout + completed.stderr
    lines = completed.stderr.strip().splitlines()
    assert len(lines) == 3, lines
    assert "sudo apt install" in lines[1]
    assert "sudo dnf install" in lines[2]
    assert not (tmp_path / "projects.json").exists(), "멈춘 실행이 등록부를 만들었다"


@pytest.mark.skipif(sys.platform == "darwin", reason="이 상자에 macOS 웹뷰가 실제로 있다")
def test_the_macos_branch_names_the_missing_binding():
    from manager import deps

    message = deps.missing_renderer_message(system="Darwin")

    assert message is not None
    assert "pip install" in message


def test_windows_is_not_judged_here():
    """WebView2 분기는 T-0914 소관이다 — 여기서 흉내 내지 않는다."""
    from manager import deps

    assert deps.missing_renderer_message(system="Windows") is None


@_needs_webview
def test_smoke_stops_when_the_screen_bundle_is_missing(tmp_path, monkeypatch):
    from manager import smoke, window

    monkeypatch.setattr(window, "dist_index", lambda: tmp_path / "dist" / "index.html")

    assert smoke.run(registry_file=tmp_path / "projects.json") == 1


@pytest.mark.integration
@_needs_webview
@pytest.mark.skipif(not DIST_INDEX.is_file(), reason="`npm run build` 산출(dist) 없음")
@pytest.mark.skipif(sys.platform == "linux" and shutil.which("xvfb-run") is None,
                    reason="headless Linux 는 xvfb-run 필요")
def test_smoke_opens_the_window_and_answers(tmp_path):
    registry_file = tmp_path / "projects.json"
    launcher = ["xvfb-run", "-a"] if sys.platform == "linux" else []

    completed = _run_manager("--smoke", "--registry", str(registry_file), launcher=launcher)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "smoke ok" in completed.stdout
