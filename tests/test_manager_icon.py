"""앱 아이콘이 세 자리(창·실행 파일·화면 탭)에 실제로 배선돼 있는지.

아이콘 원본은 `manager/assets` 한 자리다. 세 소비자가 각자 자기 파일을 들고 있으면 하나만
바뀌는 날 서로 다른 그림이 나온다 — 그래서 이 테스트는 파일의 존재가 아니라 **자리가 하나임**을
본다.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "manager"


def _build_module():
    location = MANAGER / "build.py"
    spec = importlib.util.spec_from_file_location("_icon_build_rules", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_icon_files_are_real_images_in_one_place():
    build = _build_module()
    assert build.ICON_DIR == "manager/assets"
    for name, magic in ((build.EXECUTABLE_ICON, b"\x00\x00\x01\x00"),
                        (build.WINDOW_ICON, b"\x89PNG")):
        path = build.icon_path(REPO, name)
        assert path.read_bytes()[:4] == magic, f"{name} 이 그 형식이 아니다"


def test_a_missing_icon_fails_loudly_instead_of_shipping_without_one():
    build = _build_module()
    try:
        build.icon_path(REPO, "없는-아이콘.ico")
    except FileNotFoundError:
        return
    raise AssertionError("없는 아이콘이 조용히 통과했다")


def test_the_bundle_carries_the_icon_directory():
    build = _build_module()
    assert build.ICON_DIR in build.BUNDLED_TREES, "실행 파일 안에 아이콘이 실리지 않는다"


def test_the_three_consumers_read_the_same_place():
    spec = (MANAGER / "manager.spec").read_text(encoding="utf-8")
    assert "icon=str(build.icon_path(_REPO_ROOT, build.EXECUTABLE_ICON))" in spec, \
        "실행 파일에 아이콘이 박히지 않는다"

    window = (MANAGER / "window.py").read_text(encoding="utf-8")
    assert "icon=str(window_icon())" in window, "창이 아이콘 없이 뜬다"
    assert 'PACKAGE_ROOT / "assets" / name' in window, "창 아이콘이 자산 자리 밖을 본다"

    page = (MANAGER / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon"' in page and "/icon.svg" in page, "화면 탭에 아이콘이 없다"
    assert (MANAGER / "assets" / "icon.svg").is_file(), "탭 아이콘 원본이 없다"

    config = (MANAGER / "ui" / "vite.config.ts").read_text(encoding="utf-8")
    assert 'publicDir: "../assets"' in config, \
        "화면이 아이콘 사본을 따로 들고 있다 — 자리는 manager/assets 하나다"


def test_the_window_icon_is_an_ico_on_windows():
    """Windows 창 백엔드(WinForms)는 창 아이콘을 .ico 로만 읽는다.

    PNG 를 주면 창을 띄우다 .NET 예외로 죽는다(v1.8.0 Windows 관리자 · `--smoke` 실패). 다른
    백엔드(GTK·Cocoa)는 PNG 를 쓰고, 두 파일 모두 `manager/assets` 한 자리에 있다.
    """
    from manager.window import window_icon

    assert window_icon(system="Windows").name == "icon.ico"
    assert window_icon(system="Linux").name == "icon-256.png"
    assert window_icon(system="Darwin").name == "icon-256.png"
    for system in ("Windows", "Linux", "Darwin"):
        assert window_icon(system=system).parent == MANAGER / "assets"
        assert window_icon(system=system).is_file()


def test_the_build_log_survives_a_non_utf8_console():
    """빌드 로그(한글)가 UTF-8 이 아닌 표준 출력에서도 멈추지 않는다.

    GitHub Windows 빌드 서버의 표준 출력은 cp1252 라, 콘솔 인코딩을 맞추지 않은 빌드는 첫 로그
    줄에서 UnicodeEncodeError 로 멈췄다(v1.8.0 릴리즈 Windows 실행 파일 누락). 관리자 본체와
    같은 엔진 콘솔 설정을 빌드도 먼저 부른다.
    """
    script = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('b', {str(MANAGER / 'build.py')!r})\n"
        "b = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(b)\n"
        "def imprint():\n"
        "    print('[build] 각인 확인', flush=True)\n"
        "    raise SystemExit(0)\n"
        "b.imprint_build_info = imprint\n"
        "sys.exit(b.main())\n"
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, env=env)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert "[build] 각인 확인".encode("utf-8") in completed.stdout
