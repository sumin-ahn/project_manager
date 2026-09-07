"""`--smoke` — 창이 실제로 열리는지 사람 없이 확인한다.

창 생성 → `dist/index.html` 로드(pywebview 의 `loaded` 신호) → 브리지 `list_projects` 1회 →
창 닫고 rc 0. 등록부는 호출자가 준 파일만 쓴다(사용자 등록부를 건드리지 않는다). Linux
headless 는 `Gtk.init_check` 가 False 라 `xvfb-run -a` 아래에서 돌려야 한다.

화면 쪽 `evaluate_js` 는 쓰지 않는다 — 페이지가 브리지를 부르는 중에 겹쳐 부르면 GTK 백엔드가
세마포어에서 멈추는 것을 실측했다(창이 닫힌 뒤에도 풀리지 않는다). 로드 확인은 pywebview 가
이미 갖고 있는 `events.loaded` 로 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import window

SMOKE_TIMEOUT_SECONDS = 5


def run(*, registry_file: Path | str | None = None) -> int:
    """rc 0 = 창이 열리고 화면이 로드됐고 브리지가 답했다."""
    message = window.missing_screen_message()
    if message is not None:
        print(message, file=sys.stderr)
        return 1

    manager_window = window.ManagerWindow(registry_file=registry_file)
    outcome: dict = {}

    def probe(created) -> None:
        try:
            outcome["loaded"] = created.events.loaded.wait(SMOKE_TIMEOUT_SECONDS)
            if outcome["loaded"]:
                outcome["projects"] = manager_window.api.list_projects()
        except Exception as exc:  # noqa: BLE001
            # 삼키지 않는다 — 창 스레드에서 올라온 원문을 rc≠0 의 사유로 그대로 낸다.
            # (여기서 다시 던지면 창이 닫히지 않아 `--smoke` 가 매달린다.)
            outcome["failure"] = f"{type(exc).__name__}: {exc}"
        finally:
            created.destroy()

    manager_window.start(probe)

    if "failure" in outcome:
        print(f"[중단] 창은 열렸으나 확인에 실패했다: {outcome['failure']}", file=sys.stderr)
        return 1
    if not outcome.get("loaded"):
        print(f"[중단] {SMOKE_TIMEOUT_SECONDS}초 안에 화면이 로드되지 않았다.", file=sys.stderr)
        return 1
    projects = outcome["projects"]
    if "error" in projects:
        print(f"[중단] 브리지가 오류를 냈다: {projects['error']}", file=sys.stderr)
        return 1
    print(f"smoke ok — 화면 로드 · 등록 프로젝트 {len(projects['projects'])}개")
    return 0
