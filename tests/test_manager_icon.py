"""앱 아이콘이 세 자리(창·실행 파일·화면 탭)에 실제로 배선돼 있는지.

아이콘 원본은 `manager/assets` 한 자리다. 세 소비자가 각자 자기 파일을 들고 있으면 하나만
바뀌는 날 서로 다른 그림이 나온다 — 그래서 이 테스트는 파일의 존재가 아니라 **자리가 하나임**을
본다.
"""
from __future__ import annotations

import importlib.util
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
    assert '"assets" / "icon-256.png"' in window, "창 아이콘이 자산 자리 밖을 본다"

    page = (MANAGER / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon"' in page and "/icon.svg" in page, "화면 탭에 아이콘이 없다"
    assert (MANAGER / "assets" / "icon.svg").is_file(), "탭 아이콘 원본이 없다"

    config = (MANAGER / "ui" / "vite.config.ts").read_text(encoding="utf-8")
    assert 'publicDir: "../assets"' in config, \
        "화면이 아이콘 사본을 따로 들고 있다 — 자리는 manager/assets 하나다"
