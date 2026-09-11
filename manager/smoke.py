"""`--smoke` — 창이 실제로 열리는지 사람 없이 확인한다.

창 생성 → 같은 프로세스 서버의 로컬 주소 로드(pywebview 의 `loaded` 신호) → 브리지
`list_projects` 1회 →
창 닫고 rc 0. 등록부는 호출자가 준 파일만 쓴다(사용자 등록부를 건드리지 않는다). Linux
headless 는 `Gtk.init_check` 가 False 라 `xvfb-run -a` 아래에서 돌려야 한다.

화면 쪽 `evaluate_js` 는 쓰지 않는다 — 페이지가 브리지를 부르는 중에 겹쳐 부르면 GTK 백엔드가
세마포어에서 멈추는 것을 실측했다(창이 닫힌 뒤에도 풀리지 않는다). 로드 확인은 pywebview 가
이미 갖고 있는 `events.loaded` 로 한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import window

# 창이 뜨고 화면이 로드되기를 기다리는 한도. 이 값은 앱이 얼마나 빨라야 하는가가 아니라
# **얼마나 기다렸다가 실패라고 말할 것인가**다 — 신호가 오면 즉시 돌아오므로 넉넉해도 느려지지
# 않는다. 한가한 기계에서는 창이 0.2초에 뜬다(실측). 5초가 짧았던 자리는 회귀가 워커 여럿으로
# 돌 때다 — 그 워커들이 저마다 가상 화면과 웹뷰를 띄우는 동안 25초가 걸렸고, 이 확인은 하필
# 그때 돈다. 즉 한도는 한가할 때가 아니라 붐빌 때에 맞춘다.
SMOKE_TIMEOUT_SECONDS = 60


def _close(created, outcome: dict) -> None:
    """창을 닫는다 — 닫지 못하면 그 사실을 말하고 프로세스를 끝낸다.

    창이 끝내 뜨지 않으면 이 라이브러리의 닫기가 "창이 시작되지 못했다" 로 실패한다. 그
    예외가 이 스레드 밖으로 새면 창 루프가 그대로 남아 `--smoke` 가 **아무 말 없이 매달린다**
    (바깥 타임아웃이 끊어 줄 때까지). 그래서 여기서 받아 사유를 내고 프로세스를 끝낸다 —
    닫을 창이 없는 상태라 정상 종료 경로가 남아 있지 않다.
    """
    try:
        created.destroy()
    except Exception as exc:  # noqa: BLE001
        # 창이 열리지 않은 사유는 표준 출력으로 낸다(T-0922 결정) — 실행 방식에 따라
        # 오류 출력이 사라지는 자리가 있어 그 결정이 섰다.
        print(f"[중단] 창을 닫지 못했다: {type(exc).__name__}: {exc}")
        if not outcome.get("shown"):
            print(f"[중단] {SMOKE_TIMEOUT_SECONDS}초 안에 창이 뜨지 않았다.")
        sys.stdout.flush()
        os._exit(1)


def run(*, registry_file: Path | str | None = None, port: int = 0) -> int:
    """rc 0 = 창이 열리고 화면이 로드됐고 브리지가 답했다."""
    message = window.missing_screen_message()
    if message is not None:
        print(message)
        return 1

    manager_window = window.ManagerWindow(registry_file=registry_file, port=port)
    outcome: dict = {}

    def probe(created) -> None:
        try:
            outcome["shown"] = created.events.shown.wait(SMOKE_TIMEOUT_SECONDS)
            outcome["loaded"] = (created.events.loaded.wait(SMOKE_TIMEOUT_SECONDS)
                                 if outcome["shown"] else False)
            if outcome["loaded"]:
                outcome["projects"] = manager_window.api.list_projects()
        except Exception as exc:  # noqa: BLE001
            # 삼키지 않는다 — 창 스레드에서 올라온 원문을 rc≠0 의 사유로 그대로 낸다.
            outcome["failure"] = f"{type(exc).__name__}: {exc}"
        _close(created, outcome)

    manager_window.start(probe)

    if "failure" in outcome:
        print(f"[중단] 창은 열렸으나 확인에 실패했다: {outcome['failure']}", file=sys.stderr)
        return 1
    if not outcome.get("shown"):
        print(f"[중단] {SMOKE_TIMEOUT_SECONDS}초 안에 창이 뜨지 않았다.")
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
