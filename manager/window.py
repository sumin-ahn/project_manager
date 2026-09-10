"""pywebview 창 — `webview` 를 import 하는 **유일한** 모듈.

뒷단(등록부·실행기·최신 태그·브리지)은 창 없이 테스트된다. 창은 화면 파일을 직접 열지 않고
같은 프로세스 서버(`manager/server.py`)의 로컬 주소를 연다 — **창도 서버의 손님**이라 브라우저와
같은 값을 본다(창 전용 데이터 경로 0). 서버가 내는 것은 Vite 산출물 `manager/ui/dist` 다
(dev 서버 없음).
"""
from __future__ import annotations

from pathlib import Path

import webview

from . import PACKAGE_ROOT
from .bridge import Api
from .server import CLIENT_QUERY, WINDOW_CLIENT, ManagerServer

WINDOW_TITLE = "Project Manager"
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 760


def window_icon() -> Path:
    """창 아이콘 — 실행 파일·화면 탭과 같은 자리에서 온다(`manager/assets`)."""
    return PACKAGE_ROOT / "assets" / "icon-256.png"


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

    def __init__(self, *, registry_file: Path | str | None = None, port: int = 0) -> None:
        self._window = None
        self.api = Api(folder_picker=self.pick_folder, registry_file=registry_file)
        self.server = ManagerServer(self.api, port=port)

    def pick_folder(self) -> str | None:
        """OS 폴더 선택 대화 — 취소면 None."""
        selected = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not selected:
            return None
        return selected[0]

    def create(self):
        """서버를 먼저 띄우고(데몬 스레드) 창은 그 주소의 손님으로 붙는다.

        질의 문자열 하나가 창이 자기를 밝히는 표식이다 — 폴더 고르기 대화가 창 전용이라
        화면과 서버가 그 한 가지에만 쓴다.
        """
        self.server.start()
        self._window = webview.create_window(
            WINDOW_TITLE, f"{self.server.url}/?{CLIENT_QUERY}={WINDOW_CLIENT}",
            width=WINDOW_WIDTH, height=WINDOW_HEIGHT)
        return self._window

    def start(self, func=None) -> None:
        """창을 띄우고 닫힐 때까지 돈다(메인 스레드).

        `private_mode=False` — 기본값 True 는 창을 닫을 때 화면 저장소를 버려서, 화면이 기억하기로
        한 선택(설정 안 함 항목 보기 등)이 다음 실행에 남지 않는다(실측). 저장 위치는 이 라이브러리가
        정하는 자리를 그대로 쓴다 — 관리자는 파일을 만들지 않는다.
        """
        self.create()
        try:
            webview.start(func, self._window, private_mode=False,
                          icon=str(window_icon()))
        finally:
            self.server.stop()


def open_window(*, registry_file: Path | str | None = None, port: int = 0) -> int:
    """관리자 창 — 화면 산출물이 없으면 rc 1."""
    message = missing_screen_message()
    if message is not None:
        print(message)
        return 1
    ManagerWindow(registry_file=registry_file, port=port).start()
    return 0
