"""pywebview 창 — `webview` 를 import 하는 **유일한** 모듈.

뒷단(등록부·실행기·최신 태그·브리지)은 창 없이 테스트된다. 창이 여는 것은 Vite 가 만든
정적 산출물 `manager/ui/dist/index.html` 하나다(dev 서버 없음).
"""
from __future__ import annotations

import sys
from pathlib import Path

import webview

from . import PACKAGE_ROOT
from .bridge import Api

WINDOW_TITLE = "Project Manager"
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 760


def dist_index() -> Path:
    """창이 여는 화면 산출물 — `npm run build` 산출(git-ignored)."""
    return PACKAGE_ROOT / "ui" / "dist" / "index.html"


def missing_screen_message() -> str | None:
    """화면 산출물이 없으면 빌드 명령 안내, 있으면 None."""
    index = dist_index()
    if index.is_file():
        return None
    return (f"[중단] 화면 산출물이 없다: {index}\n"
            f"  {PACKAGE_ROOT / 'ui'} 에서 `npm ci && npm run build` 를 먼저 돌려라.")


class ManagerWindow:
    """창 하나 + 그 창에 붙는 브리지 하나."""

    def __init__(self, *, registry_file: Path | str | None = None) -> None:
        self._window = None
        self.api = Api(folder_picker=self.pick_folder, registry_file=registry_file)

    def pick_folder(self) -> str | None:
        """OS 폴더 선택 대화 — 취소면 None."""
        selected = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not selected:
            return None
        return selected[0]

    def create(self):
        self._window = webview.create_window(
            WINDOW_TITLE, str(dist_index()), js_api=self.api,
            width=WINDOW_WIDTH, height=WINDOW_HEIGHT)
        return self._window

    def start(self, func=None) -> None:
        """창을 띄우고 닫힐 때까지 돈다(메인 스레드)."""
        self.create()
        webview.start(func, self._window)


def open_window(*, registry_file: Path | str | None = None) -> int:
    """관리자 창 — 화면 산출물이 없으면 rc 1."""
    message = missing_screen_message()
    if message is not None:
        print(message, file=sys.stderr)
        return 1
    ManagerWindow(registry_file=registry_file).start()
    return 0
