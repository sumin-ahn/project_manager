"""창을 열 수 있는지 — 운영체제 웹뷰 의존 확인.

Linux·macOS 의 확인은 **실제로 쓸 백엔드 모듈을 import 해 보는 것** 하나다(pywebview 가 기동
때 하는 것과 같은 import). Windows 는 import 로 갈리지 않는다 — WebView2 백엔드 모듈은 런타임
설치 여부와 무관하게 import 되고, 실제로 없는 것은 시스템에 설치되는 **Evergreen 런타임**이라
EdgeUpdate 가 남기는 레지스트리 키로 본다.

어느 쪽이든 없으면 사람이 그대로 실행할 설치 명령을 찍고 rc 3 으로 멈춘다 — 대체 렌더러로
갈아타는 경로는 두지 않는다.

Linux 는 백엔드 모듈이 있어도 창을 붙일 화면이 없을 수 있다(DISPLAY·WAYLAND_DISPLAY 모두 비어
있는 세션 — 이 경우는 라이브러리가 예외 없이 프로세스를 끝내므로 여기서 미리 걸러야 한다).
그 판정은 `Gtk.init_check()` 로 한다 — rc 1(화면 산출물 없음과 같은 값 · 설치 문제가 아니다).
"""
from __future__ import annotations

import importlib
import platform

MISSING_RENDERER_RC = 3
NO_DISPLAY_RC = 1
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
# WebView2 Evergreen 런타임의 설치 표식 — EdgeUpdate 가 이 제품 GUID 키의 `pv` 에 버전을 쓴다.
# EdgeUpdate 자체가 32비트라 64비트 Windows 의 시스템 설치는 WOW6432Node 쪽에 남고, 사용자
# 단위 설치는 HKCU 에 남는다. 세 자리를 다 보고 나서 한 번 판정한다.
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_VERSION_VALUE = "pv"
WEBVIEW2_REGISTRY_LOCATIONS = (
    ("HKEY_LOCAL_MACHINE", rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ("HKEY_LOCAL_MACHINE", rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ("HKEY_CURRENT_USER", rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
)
# 런타임을 지우면 키는 남고 값만 이 버전이 된다(설치 아님).
WEBVIEW2_UNINSTALLED_VERSION = "0.0.0.0"
WINDOWS_INSTALL_LINES = (
    "  다운로드: https://developer.microsoft.com/microsoft-edge/webview2/ (Evergreen 런타임)",
    "  또는:     winget install --id Microsoft.EdgeWebView2Runtime",
)


def _registry_version(hive: str, subkey: str) -> str:
    """레지스트리 한 자리의 WebView2 버전 문자열. 키·값이 없으면 `OSError` 로 올라온다."""
    import winreg  # Windows 에만 있는 표준 모듈이다 (Windows 분기에서만 부른다).

    with winreg.OpenKey(getattr(winreg, hive), subkey) as key:
        value, _ = winreg.QueryValueEx(key, WEBVIEW2_VERSION_VALUE)
    return str(value)


def missing_webview2_message(read_registry_version=None) -> str | None:
    """Windows 분기 — WebView2 런타임이 없으면 안내 문구, 있으면 None.

    `read_registry_version` 은 레지스트리를 읽는 자리를 갈아 끼우는 매개변수다(Windows 아닌
    상자의 회귀가 이 판정을 실제로 돌리는 유일한 길 · `winreg` 는 거기 없다).
    """
    read_registry_version = (
        _registry_version if read_registry_version is None else read_registry_version)
    for hive, subkey in WEBVIEW2_REGISTRY_LOCATIONS:
        try:
            version = read_registry_version(hive, subkey).strip()
        except OSError:
            continue  # 이 자리에 키가 없을 뿐이다 — 부재 판정은 세 자리를 다 본 뒤에 한 번 한다.
        if version and version != WEBVIEW2_UNINSTALLED_VERSION:
            return None
    return "\n".join([
        "[중단] 창을 열 WebView2 런타임이 없다 "
        f"(EdgeUpdate 클라이언트 키 {WEBVIEW2_CLIENT_GUID} 부재). 아래 중 하나로 설치하라:",
        *WINDOWS_INSTALL_LINES,
    ])


def _linux_has_display() -> bool:
    """`Gtk.init_check()` — 붙일 화면이 있으면 True (백엔드 모듈 import 뒤에만 부른다)."""
    from gi.repository import Gtk  # LINUX_BACKEND_MODULE import 가 이미 버전을 정해 두었다.

    ok, _ = Gtk.init_check([])
    return ok


def missing_renderer_message(*, system: str | None = None,
                             read_registry_version=None) -> tuple[str | None, int]:
    """웹뷰를 못 쓰거나 창을 붙일 화면이 없으면 (안내 문구, rc), 정상이면 (None, 0)."""
    system = platform.system() if system is None else system
    if system == "Windows":
        message = missing_webview2_message(read_registry_version)
        return (message, MISSING_RENDERER_RC) if message is not None else (None, 0)
    if system == "Linux":
        module_name, install_lines = LINUX_BACKEND_MODULE, LINUX_INSTALL_LINES
    elif system == "Darwin":
        module_name, install_lines = MACOS_BACKEND_MODULE, MACOS_INSTALL_LINES
    else:
        return None, 0
    try:
        importlib.import_module(module_name)
    except (ImportError, ValueError) as exc:
        # ValueError 는 `gi.require_version` 이 없는 typelib 을 만났을 때 나온다.
        message = "\n".join([
            f"[중단] 창을 열 웹뷰가 없다 ({module_name}: {exc}). 아래 중 하나로 설치하라:",
            *install_lines,
        ])
        return message, MISSING_RENDERER_RC
    if system == "Linux" and not _linux_has_display():
        message = "\n".join([
            "[중단] 창을 붙일 화면이 없다 (DISPLAY·WAYLAND_DISPLAY 모두 비어 있음).",
            "  X 서버가 있는 세션에서 실행하거나, 확인만 할 때는 `xvfb-run -a` 아래에서 돌려라.",
        ])
        return message, NO_DISPLAY_RC
    return None, 0
