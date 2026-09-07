"""창 의존 확인과 `--smoke` — 진입점을 격리 서브프로세스로 실제 실행해 잰다 (T-0910·T-0914).

웹뷰가 없으면 설치 명령 두 줄을 찍고 rc 3 이다(대체 렌더러로 갈아타지 않는다). `--smoke` 는
창을 열어 화면과 브리지를 한 번 확인하고 닫는다 — Linux headless 는 `Gtk.init_check` 가 False 라
`xvfb-run -a` 아래에서만 통과한다.

Windows 분기(T-0914·AT-001)는 import 가 아니라 **WebView2 런타임의 레지스트리 키**로 갈린다.
`winreg` 는 Windows 에만 있으므로 이 상자에서는 레지스트리를 읽는 자리를 매개변수로 갈아 끼워
판정 자체를 실제로 돌린다(가짜 모듈 주입 아님). 실제 Windows 실측은 QEMU VM 몫이다.
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


def _absent_key(_hive: str, _subkey: str) -> str:
    """레지스트리에 그 키가 없는 상자 — `winreg.OpenKey` 가 내는 그 예외 그대로."""
    raise FileNotFoundError(2, "지정된 파일을 찾을 수 없습니다")


def test_a_missing_webview2_runtime_prints_the_download_line_and_stops():
    """Windows 도 같은 계약이다 — 안내 두 줄 + rc 3, 대체 렌더러 0."""
    from manager import deps

    message = deps.missing_renderer_message(
        system="Windows", read_registry_version=_absent_key)

    assert message is not None
    lines = message.splitlines()
    assert len(lines) == 3, lines
    assert "https://developer.microsoft.com/microsoft-edge/webview2/" in lines[1]
    assert "winget install" in lines[2]
    assert deps.MISSING_RENDERER_RC == 3


@pytest.mark.parametrize("installed_at", range(3))
def test_an_installed_runtime_passes_from_any_of_the_three_registry_places(installed_at):
    """시스템(64비트·32비트 뷰)·사용자 어느 자리에 설치돼도 통과한다."""
    from manager import deps

    places = deps.WEBVIEW2_REGISTRY_LOCATIONS
    assert len(places) == 3, places

    def read(hive: str, subkey: str) -> str:
        if (hive, subkey) == places[installed_at]:
            return "142.0.3595.94"
        raise FileNotFoundError(2, "지정된 파일을 찾을 수 없습니다")

    assert deps.missing_renderer_message(system="Windows", read_registry_version=read) is None


def test_the_uninstalled_marker_version_is_not_an_installed_runtime():
    """런타임을 지우면 키는 남고 값만 `0.0.0.0` 이 된다 — 설치로 세지 않는다."""
    from manager import deps

    message = deps.missing_renderer_message(
        system="Windows",
        read_registry_version=lambda hive, subkey: deps.WEBVIEW2_UNINSTALLED_VERSION)

    assert message is not None


def test_every_registry_place_is_looked_at_before_the_absence_is_declared():
    from manager import deps

    looked_at: list[tuple[str, str]] = []

    def read(hive: str, subkey: str) -> str:
        looked_at.append((hive, subkey))
        raise FileNotFoundError(2, "지정된 파일을 찾을 수 없습니다")

    deps.missing_renderer_message(system="Windows", read_registry_version=read)

    assert looked_at == list(deps.WEBVIEW2_REGISTRY_LOCATIONS)
    hives = {hive for hive, _ in looked_at}
    assert hives == {"HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER"}
    assert any("WOW6432Node" in subkey for _, subkey in looked_at), "64비트 뷰만 본다"


def test_the_windows_branch_reads_the_registry_and_not_an_import():
    """import 로 갈리면 WebView2 는 항상 '있음' 이 된다(백엔드 모듈은 런타임과 무관하다)."""
    from manager import deps

    source = Path(deps.__file__).read_text(encoding="utf-8")
    windows_branch = source[source.index("def missing_webview2_message"):
                            source.index("def missing_renderer_message")]

    assert "import_module" not in windows_branch
    assert deps.WEBVIEW2_VERSION_VALUE == "pv"


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
