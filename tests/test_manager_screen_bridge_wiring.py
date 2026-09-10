"""화면이 파이썬을 부르는 자리는 하나다 — HTTP (T-0921 AT-003).

창 주입 배선(`window.pywebview`·`pywebviewready`)은 남지 않는다. `api.ts` 의 `Bridge` 타입이
내는 이름과 파이썬 `Api` 의 public 메서드를 **같은 실행에서** 비교해, 브리지에 메서드가 늘면
화면도 따라오도록 강제한다. 화면 쪽 동작 단언은 `manager/ui/src/__tests__/api.test.tsx` 가
`npm --prefix manager/ui test` 로 잰다.
"""
from __future__ import annotations

import re
from pathlib import Path

from manager.bridge import Api
from manager.server import api_names

REPO = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO / "manager" / "ui" / "src"
API_TS = SOURCE_DIR / "api.ts"
MAIN_TSX = SOURCE_DIR / "main.tsx"
APP_TSX = SOURCE_DIR / "App.tsx"
SCREEN_TEST = SOURCE_DIR / "__tests__" / "api.test.tsx"
# 창 주입 배선의 흔적 — 한 자리라도 남으면 데이터 경로가 둘이 된다.
INJECTION_MARKERS = ("pywebview", "pywebviewBridge", "pywebviewready")


def _screen_sources() -> list[Path]:
    """화면 소스 전부 — 재귀 순회 대신 화면이 쓰는 폴더 넷을 그대로 센다."""
    folders = (SOURCE_DIR, SOURCE_DIR / "components", SOURCE_DIR / "tabs", SOURCE_DIR / "lib",
               SOURCE_DIR / "__tests__")
    return [source for folder in folders for source in sorted(folder.glob("*.ts*"))]


def _bridge_type_names() -> set[str]:
    """`export type Bridge = {...}` 안의 메서드 이름 — 화면이 내보내는 집합."""
    text = API_TS.read_text(encoding="utf-8")
    start = text.index("export type Bridge = {")
    block = text[start:text.index("\n};", start)]
    return set(re.findall(r"^  (\w+)\(", block, flags=re.MULTILINE))


def _bridge_wiring_names() -> set[str]:
    """`httpBridge()` 가 실제로 배선한 이름 — 타입과 배선이 갈리면 여기서 드러난다."""
    text = API_TS.read_text(encoding="utf-8")
    start = text.index("export function httpBridge()")
    block = text[start:text.index("\n}", start)]
    return set(re.findall(r"^    (\w+): ", block, flags=re.MULTILINE))


# ── 창 주입 경로가 남지 않는다 ───────────────────────────────────────────────


def test_the_injected_bridge_is_gone_from_every_screen_file():
    for source in (API_TS, MAIN_TSX, APP_TSX):
        text = source.read_text(encoding="utf-8")
        for marker in INJECTION_MARKERS:
            assert marker not in text, f"{source.name} 에 창 주입 배선 {marker!r} 이 남아 있다"
    assert "declare global" not in API_TS.read_text(encoding="utf-8")


def test_the_screen_has_exactly_one_wiring_function():
    text = API_TS.read_text(encoding="utf-8")

    assert len(re.findall(r"export function \w*[Bb]ridge\(", text)) == 1
    assert "httpBridge()" in MAIN_TSX.read_text(encoding="utf-8")


def test_the_call_goes_through_one_http_place():
    text = API_TS.read_text(encoding="utf-8")

    assert text.count("await fetch(") == 1
    assert '`/api/${name}`' in text


# ── 이름 집합이 두 벌이 아니다 ───────────────────────────────────────────────


def test_the_screen_names_are_the_python_bridge_names_measured_in_one_run():
    python_names = set(api_names(Api(folder_picker=lambda: None)))

    assert _bridge_type_names() == python_names
    assert _bridge_wiring_names() == python_names
    assert len(python_names) == 21


def test_a_new_python_method_makes_the_screen_red_until_it_follows():
    """브리지에 이름이 하나 늘면 화면 집합과 갈린다 — 그 갈림이 이 회귀의 red 다."""
    class WithOneMore(Api):
        def board_open(self, path: str) -> dict:
            return {"path": path}

    grown = set(api_names(WithOneMore(folder_picker=lambda: None)))

    assert len(grown) == 22
    assert _bridge_type_names() != grown


# ── 창 표식이 갈리는 자리는 하나 ─────────────────────────────────────────────


def test_the_window_marker_decides_one_thing_only():
    """표식으로 갈리는 동작이 폴더 고르기 말고 또 생기면 그것이 곧 결함이다."""
    screen = API_TS.read_text(encoding="utf-8")
    others = [source.name for source in _screen_sources()
              if source not in (API_TS, SCREEN_TEST)
              and "isWindowClient" in source.read_text(encoding="utf-8")]
    assert others == [], f"창 표식이 {others} 에서도 갈린다"
    # 선언 1 + 요청 머리 1 + 폴더 고르기 갈림 1.
    assert screen.count("isWindowClient") == 3
    assert screen.count("if (isWindowClient())") == 1
    assert "chooseFolder" in screen[screen.index("if (isWindowClient())") - 400:]

    server = (REPO / "manager" / "server.py").read_text(encoding="utf-8")
    deciding = [line for line in server.splitlines()
                if "WINDOW_CLIENT" in line and not line.startswith("WINDOW_CLIENT")]
    assert len(deciding) == 1, deciding
    assert "pick_folder" in deciding[0]


# ── 쓰기 계수는 서버로 옮겼다 ────────────────────────────────────────────────


def test_the_background_query_failure_is_not_shown_to_the_person():
    """배경 조회(주기·창 복귀·서버 알림)는 실패해도 마지막 값만 남는다 — 화면 단언은 App.test.tsx."""
    text = APP_TSX.read_text(encoding="utf-8")

    background = re.findall(r"refresh(?:Light|Heavy|Settings)\([^;\n]*?, false\)", text)
    # 주기 · 창 복귀 · 서버 알림 셋(알림은 가벼운 절·무거운 절·설정 셋) = 다섯 자리.
    assert len(background) == 5, f"배경 조회로 표시된 자리가 다섯이 아니다: {background}"
    assert "if (!explicit)" in text


def test_the_screen_no_longer_counts_in_flight_writes():
    """손님이 둘이면 화면 계수는 틀린다 — 거절은 서버가 낸다."""
    text = APP_TSX.read_text(encoding="utf-8")

    assert "trackWrites" not in text
    assert "inFlight" not in text


# ── 화면 쪽 단언의 자리 ──────────────────────────────────────────────────────


def test_the_screen_regression_measures_the_http_place_without_a_fake_injected_bridge():
    text = SCREEN_TEST.read_text(encoding="utf-8")

    assert "/api/conf_save" in text
    assert "/events" in text
    for marker in INJECTION_MARKERS:
        assert marker not in text
