"""창을 열 수 있는지 — 운영체제 웹뷰 의존 확인.

확인은 **실제로 쓸 백엔드 모듈을 import 해 보는 것** 하나다(pywebview 가 기동 때 하는 것과
같은 import). 없으면 사람이 그대로 실행할 설치 명령을 찍고 rc 3 으로 멈춘다 — 대체 렌더러로
갈아타는 경로는 두지 않는다.

Windows(WebView2) 분기는 이 함수에 없다 — T-0914 소관이다.
"""
from __future__ import annotations

import importlib
import platform

MISSING_RENDERER_RC = 3
LINUX_BACKEND_MODULE = "webview.platforms.gtk"
MACOS_BACKEND_MODULE = "webview.platforms.cocoa"
LINUX_INSTALL_LINES = (
    "  Debian·Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1",
    "  Fedora·RHEL:   sudo dnf install python3-gobject gtk3 webkit2gtk4.1",
)
MACOS_INSTALL_LINES = (
    "  python3 -m pip install --upgrade pywebview",
    "  (macOS 웹뷰는 내장이다 — 빠진 것은 pyobjc 쪽 바인딩이다)",
)


def missing_renderer_message(*, system: str | None = None) -> str | None:
    """웹뷰를 못 쓰면 안내 문구, 쓸 수 있으면 None."""
    system = platform.system() if system is None else system
    if system == "Linux":
        module_name, install_lines = LINUX_BACKEND_MODULE, LINUX_INSTALL_LINES
    elif system == "Darwin":
        module_name, install_lines = MACOS_BACKEND_MODULE, MACOS_INSTALL_LINES
    else:
        return None
    try:
        importlib.import_module(module_name)
    except (ImportError, ValueError) as exc:
        # ValueError 는 `gi.require_version` 이 없는 typelib 을 만났을 때 나온다.
        return "\n".join([
            f"[중단] 창을 열 웹뷰가 없다 ({module_name}: {exc}). 아래 중 하나로 설치하라:",
            *install_lines,
        ])
    return None
