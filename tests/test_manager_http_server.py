"""같은 프로세스 HTTP 서버 하나 — 이름 집합·바인딩·포트·거절 (T-0921 AT-001).

이름 집합은 어디에도 적혀 있지 않다(브리지 public 메서드 파생) — 이 파일은 그 파생을 실제
서버에서 뽑아 파이썬 `Api` 와 **같은 실행에서** 비교한다. 브리지에 메서드가 하나 늘면 HTTP
이름도 자동으로 는다는 것을 하위 클래스로 잰다.

서버는 창 없이 돈다(pywebview 미설치에서도 이 파일은 통과한다).
"""
from __future__ import annotations

import inspect
import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from manager import server as manager_server
from manager.bridge import Api
from manager.server import (
    BROWSER_CLIENT, CLIENT_HEADER, HOST, URL_NOTICE_PREFIX, WINDOW_CLIENT, ManagerServer,
)

REPO = Path(__file__).resolve().parents[1]
MANAGER_SOURCES = sorted((REPO / "manager").glob("*.py"))
# 창이 주입하던 이름 그대로 — 이 목록은 **기대값**이고 구현이 읽는 자리가 아니다.
EXPECTED_NAMES = {
    "ctx_capacity",
    "list_projects", "add_project", "remove_project", "status", "latest_tags", "pick_folder",
    "run", "conf_keys", "conf_values", "conf_save", "delegate_models", "conf_reset", "adapters",
    "adapter_accept", "adapter_matrix", "upstream_show", "upstream_set", "update_preview",
    "update_apply", "apply_defaults", "apply_new",
}
FIXED_PORT = 7391
REQUEST_TIMEOUT_SECONDS = 10


class FakeApi(Api):
    """브리지 대역 — 바깥(사본·git)을 부르지 않고 자기 호출을 기록한다."""

    def __init__(self, registry_file=None) -> None:
        super().__init__(folder_picker=lambda: "/골라진/폴더", registry_file=registry_file)
        self.started = threading.Event()
        self.release = threading.Event()
        self.saved: list = []

    def status(self, path: str, with_board: bool = False, with_adapters: bool = False) -> dict:
        return {"schema": 1, "pm_home": path}

    def conf_save(self, path: str, updates: dict) -> dict:
        self.saved.append((path, updates))
        return {"changed": True, "conf": {}}

    def update_apply(self, path: str) -> dict:
        self.started.set()
        self.release.wait(REQUEST_TIMEOUT_SECONDS)
        return {"rc": 0, "stdout": "적용함", "stderr": ""}


@pytest.fixture
def dist(tmp_path) -> Path:
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("<html>화면</html>", encoding="utf-8")
    return root


@pytest.fixture
def api(tmp_path) -> FakeApi:
    return FakeApi(tmp_path / "config" / "projects.json")


def window_module():
    """창 모듈 — pywebview 가 없으면 이 단언은 돌 자리가 없다."""
    pytest.importorskip("webview")
    from manager import window

    return window


def echo_api(names):
    """이름 집합만 같은 대역 — 이름마다 자기 이름을 돌려주는 메서드 하나(바깥 호출 0)."""
    def make(name):
        def method(_self, *args):
            return {"called": name, "args": list(args)}
        return method

    return type("EchoApi", (), {name: make(name) for name in names})()


@pytest.fixture
def running(api, dist):
    server = ManagerServer(api, dist=dist)
    server.start()
    try:
        yield server
    finally:
        api.release.set()
        server.stop()


def post(server: ManagerServer, name: str, *args, client: str | None = BROWSER_CLIENT,
         host: str | None = None, content_type: str = "application/json",
         origin: str | None = None):
    """화면이 보내는 모양 그대로 — 표식 머리를 뺄 때만 `client=None`."""
    headers = {"Content-Type": content_type}
    if client is not None:
        headers[CLIENT_HEADER] = client
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"http://{host or HOST}:{server.port}/api/{name}",
        data=json.dumps({"args": list(args)}).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def get(server: ManagerServer, path: str, host: str | None = None):
    with urllib.request.urlopen(f"http://{host or HOST}:{server.port}{path}",
                                timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.status, response.read().decode("utf-8")


# ── 이름 집합 (선언 0 · 파생) ────────────────────────────────────────────────


def test_the_http_names_are_the_bridge_methods_measured_in_one_run(api, dist):
    """양쪽을 같은 실행에서 잰다 — 목록을 적어 둔 자리가 없다는 것이 이 단언의 뜻이다."""
    derived = set(manager_server.api_names(api))
    assert derived == EXPECTED_NAMES
    assert len(derived) == 22

    server = ManagerServer(echo_api(derived), dist=dist)
    server.start()
    try:
        over_http = {post(server, name, client=WINDOW_CLIENT)["called"] for name in sorted(derived)}
    finally:
        server.stop()

    assert over_http == derived


def test_a_new_bridge_method_shows_up_without_touching_the_server(dist):
    class WithOneMore(FakeApi):
        def board_open(self, path: str) -> dict:
            return {"path": path}

    server = ManagerServer(WithOneMore(), dist=dist)
    server.start()
    try:
        assert len(manager_server.api_names(server.api)) == 23
        assert post(server, "board_open", "/어디") == {"path": "/어디"}
    finally:
        server.stop()


def test_the_name_set_is_not_written_down_anywhere_in_the_manager(running):
    """이름 목록을 파이썬·JSON 어디에도 적지 않는다 — 적으면 두 벌이 되어 갈린다."""
    listed = []
    for source in MANAGER_SOURCES:
        text = source.read_text(encoding="utf-8")
        if all(f'"{name}"' in text for name in ("list_projects", "conf_keys", "adapter_matrix")):
            listed.append(source.name)

    assert listed == []


# ── 표식 요구 (화면만 부른다) ────────────────────────────────────────────────


def test_a_post_without_the_client_marker_is_refused_before_the_method_runs(running, api):
    """머리 없는 요청 — 브라우저가 preflight 없이 보내는 모양(text/plain · 다른 출처)."""
    refused = post(running, "conf_save", "/projects/demo", {"delegate.model": "opus"},
                   client=None, content_type="text/plain",
                   origin="http://evil.example")

    assert refused["error"]["kind"] == "not-allowed"
    assert CLIENT_HEADER in refused["error"]["message"]
    assert api.saved == [], "표식이 없는데 브리지를 불렀다"


def test_an_unknown_client_marker_is_refused_too(running, api):
    refused = post(running, "conf_save", "/projects/demo", {}, client="curl")

    assert refused["error"]["kind"] == "not-allowed"
    assert api.saved == []


def test_the_two_markers_the_screen_sends_are_the_ones_that_pass(running, api):
    """창과 브라우저 둘 다 그대로 통과한다 — 화면이 싣는 값이 그 둘이다."""
    for client in (WINDOW_CLIENT, BROWSER_CLIENT):
        assert post(running, "status", "/projects/demo",
                    client=client)["pm_home"] == "/projects/demo"
    assert post(running, "conf_save", "/projects/demo", {}, client=BROWSER_CLIENT) == {
        "changed": True, "conf": {}}
    assert api.saved == [("/projects/demo", {})]


def test_the_folder_dialog_stays_window_only_under_the_marker_requirement(running):
    """표식 요구가 창 전용 판정을 덮지 않는다 — 브라우저는 여전히 폴더 고르기를 못 연다."""
    refused = post(running, "pick_folder", client=BROWSER_CLIENT)

    assert refused["error"]["kind"] == "not-allowed"
    assert "창에서만" in refused["error"]["message"]
    assert post(running, "pick_folder", client=WINDOW_CLIENT) == {"path": "/골라진/폴더"}


# ── 쓰기·읽기 분류 (손 목록이 조용히 뒤처지지 않게) ──────────────────────────


def test_every_bridge_name_is_classified_as_a_write_or_a_read(api):
    """이름이 늘면 분류를 강제로 고르게 한다 — 분류 누락이 red 다.

    쓰기(저장·적용) 집합만 구현이 소유하고(`WRITE_NAMES` — 동시 변경 거절이 그 값을 읽는다),
    읽기 쪽은 이 파일의 **기대값**이다(`EXPECTED_NAMES` 와 같은 층). 구현 안에 두 벌째 목록을
    두지 않는다 — 이름 목록을 manager 안에 적지 않는다는 계약이 그 위에 있다.
    """
    names = set(manager_server.api_names(api))
    writes = set(manager_server.WRITE_NAMES)
    reads = EXPECTED_NAMES - writes

    assert writes <= names, f"쓰기 목록에만 있는 이름: {sorted(writes - names)}"
    assert writes & reads == set()
    unclassified = names - writes - reads
    assert unclassified == set(), f"쓰기·읽기 어느 쪽에도 없는 이름: {sorted(unclassified)}"


def test_a_new_bridge_method_that_nobody_classified_turns_the_guard_red(dist):
    """브리지에 쓰기 메서드가 하나 늘면 그 이름은 어느 분류에도 없다 — 그 자리가 red 다."""
    class WithOneMore(FakeApi):
        def board_write(self, path: str) -> dict:
            return {"path": path}

    names = set(manager_server.api_names(WithOneMore()))

    assert names - set(manager_server.WRITE_NAMES) - EXPECTED_NAMES == {"board_write"}


def test_an_unknown_name_is_refused_with_a_reason(running):
    response = post(running, "rm_rf")

    assert response["error"]["kind"] == "not-allowed"
    assert "rm_rf" in response["error"]["message"]


# ── 바인딩과 포트 ────────────────────────────────────────────────────────────


def test_the_manager_never_binds_anything_but_the_loopback_address():
    """주소는 상수 하나이고 그것을 받는 인자가 없다 — 소켓을 여는 자리도 `server.py` 하나다."""
    assert manager_server.HOST == "127.0.0.1"
    binding = [source.name for source in MANAGER_SOURCES
               if "HTTPServer(" in source.read_text(encoding="utf-8")]
    assert binding == ["server.py"], f"소켓을 여는 자리가 여럿이다: {binding}"
    assert "0.0.0.0" not in (REPO / "manager" / "server.py").read_text(encoding="utf-8")

    for entry in (ManagerServer.__init__, window_module().open_window,
                  window_module().ManagerWindow.__init__):
        taken = set(inspect.signature(entry).parameters)
        assert not taken & {"host", "bind", "address"}, f"{entry} 가 바인딩 주소를 받는다"
    flags = [line for line in (REPO / "manager" / "__main__.py").read_text(
        encoding="utf-8").splitlines() if "add_argument(" in line]
    assert [flag for flag in flags if "host" in flag or "bind" in flag] == []


def test_the_lan_address_of_this_box_does_not_reach_the_server(running):
    lan = socket.gethostbyname(socket.gethostname())
    if lan.startswith("127."):
        pytest.skip("이 상자의 이름이 loopback 으로만 풀린다")

    with pytest.raises((urllib.error.URLError, OSError)):
        get(running, "/", host=lan)


def test_a_given_port_is_the_port_and_an_omitted_one_is_chosen_free(api, dist):
    fixed = ManagerServer(api, port=FIXED_PORT, dist=dist)
    try:
        assert fixed.port == FIXED_PORT
        assert fixed.url == f"http://127.0.0.1:{FIXED_PORT}"
    finally:
        fixed.stop()

    first = ManagerServer(FakeApi(), dist=dist)
    second = ManagerServer(FakeApi(), dist=dist)
    try:
        assert first.port != 0 and second.port != 0
        assert first.port != second.port, "자동 선택이 고정값이다"
    finally:
        first.stop()
        second.stop()


def test_the_running_server_says_its_address_once_on_stdout(api, dist, capsys):
    """주소를 내는 자리는 `start()` 하나다 — 없으면 브라우저로 붙을 방법이 값으로 없다."""
    server = ManagerServer(api, dist=dist)
    server.start()
    try:
        printed = capsys.readouterr().out.strip().splitlines()
    finally:
        server.stop()

    assert printed == [f"{URL_NOTICE_PREFIX}{server.url}"]
    assert f":{server.port}" in printed[0]


# ── 화면 산출물 ──────────────────────────────────────────────────────────────


def test_the_screen_bundle_comes_out_of_the_same_server(running):
    status, body = get(running, "/")

    assert status == 200
    assert "화면" in body


def test_the_window_marker_in_the_query_does_not_break_the_screen_bundle(running):
    status, body = get(running, "/?client=window")

    assert status == 200
    assert "화면" in body


# ── 저장·적용 중의 거절 ──────────────────────────────────────────────────────


def _apply_in_background(server: ManagerServer, path: str) -> threading.Thread:
    thread = threading.Thread(target=post, args=(server, "update_apply", path), daemon=True)
    thread.start()
    return thread


def test_another_request_for_the_same_project_is_refused_with_a_reason(running, api):
    thread = _apply_in_background(running, "/projects/demo")
    assert api.started.wait(REQUEST_TIMEOUT_SECONDS)

    refused = post(running, "conf_save", "/projects/demo", {"delegate.model": "opus"})

    assert refused["error"]["kind"] == "not-allowed"
    assert "/projects/demo" in refused["error"]["message"]
    assert "update_apply" in refused["error"]["message"]
    assert api.saved == [], "거절했는데 브리지를 불렀다"

    api.release.set()
    thread.join(REQUEST_TIMEOUT_SECONDS)
    assert post(running, "conf_save", "/projects/demo", {}) == {"changed": True, "conf": {}}


def test_another_project_keeps_working_while_one_is_busy(running, api):
    thread = _apply_in_background(running, "/projects/demo")
    assert api.started.wait(REQUEST_TIMEOUT_SECONDS)

    assert post(running, "conf_save", "/projects/other", {}) == {"changed": True, "conf": {}}
    assert post(running, "status", "/projects/other")["pm_home"] == "/projects/other"

    api.release.set()
    thread.join(REQUEST_TIMEOUT_SECONDS)


def test_a_read_of_the_busy_project_is_refused_instead_of_calling_a_half_replaced_copy(
        running, api):
    thread = _apply_in_background(running, "/projects/demo")
    assert api.started.wait(REQUEST_TIMEOUT_SECONDS)

    refused = post(running, "status", "/projects/demo")

    assert refused["error"]["kind"] == "not-allowed"

    api.release.set()
    thread.join(REQUEST_TIMEOUT_SECONDS)
