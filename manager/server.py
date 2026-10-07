"""같은 프로세스 HTTP 서버 하나 — 화면 산출물과 브리지 호출 한 자리.

창도 브라우저도 **이 서버의 손님**이다. 창 전용 데이터 경로는 없다(둘로 갈리면 값이 갈린다).
경로는 넷뿐이다:

  - `GET /` · `GET /<정적파일>` — `manager/ui/dist` 산출물.
  - `POST /api/<이름>` — 요청 `{"args": [...]}` · 응답은 브리지 반환 JSON 그대로.
  - `GET /events` — 저장·적용이 끝났다는 알림 하나(다른 손님이 다시 읽는 신호).

내보내는 이름 집합은 **선언하지 않는다** — `Api` 인스턴스의 public 메서드에서 파생한다
(목록을 따로 적으면 두 벌이 되어 갈린다).

동시 변경: 저장·적용이 도는 동안 **같은 프로젝트**의 다른 요청은 거절한다(사유 포함). 새 상태
축이 아니라 거절 하나이고, 엔진 호출 직렬화는 `manager.ENGINE_LOCK` 이 이미 한다 — 여기서
두 번째 잠금을 만들지 않는다(프로젝트별 진행 표시는 그 잠금과 층이 다르다).
"""
from __future__ import annotations

import inspect
import json
import queue
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import PACKAGE_ROOT

# 바인딩 주소 — 밖으로 열지 않는다(주소를 받는 인자가 없다).
HOST = "127.0.0.1"
API_PREFIX = "/api/"
EVENTS_PATH = "/events"
# 창이 자기를 밝히는 표식 — 폴더 고르기 대화가 창 전용이라 그 한 가지에만 쓴다. 창이 여는
# 주소의 질의 `client=window` 를 화면이 읽어 요청 머리에 그대로 싣는다.
CLIENT_QUERY = "client"
CLIENT_HEADER = "X-Manager-Client"
WINDOW_CLIENT = "window"
BROWSER_CLIENT = "browser"
# `/api/` 를 부를 수 있는 표식 값 — 화면이 싣는 두 가지뿐이다(창·브라우저). 표식이 없거나 다른
# 값이면 메서드를 부르기 전에 거절한다: 커스텀 머리는 다른 출처에서 preflight 를 부르고 이
# 서버는 CORS 응답을 내지 않으므로, 아무 웹페이지가 열어 둔 채로 이 서버의 이름을 부르는 길이
# 닫힌다(표식을 읽기만 하면 `run`·`conf_save` 가 그대로 돈다).
API_CLIENTS = frozenset({WINDOW_CLIENT, BROWSER_CLIENT})
# 저장·적용 — 도는 동안 같은 프로젝트의 다른 요청을 거절하는 호출.
WRITE_NAMES = frozenset({
    "update_apply", "apply_new", "conf_save", "conf_reset", "adapter_accept", "run"})
# 프로젝트 폴더를 첫 인자로 받는 메서드의 그 인자 이름 — 거절 단위(프로젝트)를 여기서 읽는다.
PROJECT_PARAMETERS = ("path", "dest")
# 거절의 rc — 엔진을 부르지 않았다는 뜻의 관리자 자신의 값이다(`bridge_run.REJECT_RC` 와 같다).
REJECT_RC = 2
EVENT_POLL_SECONDS = 0.5
# 서버가 뜬 자리를 사람에게 알리는 한 줄의 머리 — 주소를 내는 자리는 `start()` 하나다.
URL_NOTICE_PREFIX = "관리자 주소: "


def api_names(api) -> tuple[str, ...]:
    """그 브리지가 내보내는 이름 — public 메서드 파생(목록 상수 없음)."""
    return tuple(sorted(name for name in dir(api)
                        if not name.startswith("_") and callable(getattr(api, name))))


def project_key(method, args: list) -> str | None:
    """그 호출이 건드리는 프로젝트 폴더 — 첫 인자가 폴더인 메서드에서만 값이 있다."""
    parameters = list(inspect.signature(method).parameters)
    if not parameters or parameters[0] not in PROJECT_PARAMETERS or not args:
        return None
    return str(args[0])


def _rejected(message: str) -> dict:
    """부르기를 거절한 자리 — 화면은 `CallError` 와 같은 모양으로 받는다(stderr 는 없다)."""
    return {"error": {"kind": "not-allowed", "message": message, "stderr": "", "rc": REJECT_RC}}


class _EventStream:
    """저장·적용이 끝났다는 알림 하나 — 손님마다 큐 하나."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: list[queue.Queue] = []
        self._closed = False

    def subscribe(self) -> queue.Queue:
        listener: queue.Queue = queue.Queue()
        with self._lock:
            self._listeners.append(listener)
        return listener

    def unsubscribe(self, listener: queue.Queue) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def publish(self, event: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener.put(event)

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class ManagerServer:
    """브리지 하나에 붙는 서버 하나 — 손님(창·브라우저)이 여럿이어도 값은 한 벌이다."""

    def __init__(self, api, *, port: int = 0, dist: Path | str | None = None) -> None:
        self.api = api
        self.dist = Path(dist) if dist is not None else PACKAGE_ROOT / "ui" / "dist"
        self.events = _EventStream()
        # 프로젝트 폴더 → (잠금, 지금 도는 저장·적용 이름). 화면에 보이는 값이 아니라 거절의 근거다.
        self._writes: dict[str, list] = {}
        self._writes_lock = threading.Lock()
        self._httpd = ThreadingHTTPServer((HOST, port), _handler_class(self))
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    # ── 주소 ──────────────────────────────────────────────────────────────
    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    # ── 수명 ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        """창을 띄우기 **전에** 부른다 — GTK 메인 루프는 돌아오지 않는다(데몬 스레드).

        주소 한 줄을 표준 출력에 낸다 — 포트가 빈 자리 자동 선택이라 이 줄이 없으면 브라우저로
        같은 화면에 붙을 방법이 값으로 없다. 앱이 곧 서버라 창이 뜨는 실행에서도 낸다.
        """
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="manager-http", daemon=True)
        self._thread.start()
        # 창이 뜨는 실행은 그대로 서 있으므로 버퍼가 비워지지 않는다 — 파이프·기록으로 받는
        # 사람에게 주소가 늦게 도착하면 그 한 줄이 없는 것과 같다.
        print(f"{URL_NOTICE_PREFIX}{self.url}", flush=True)

    def stop(self) -> None:
        """소켓과 알림을 닫는다 — 아직 돌지 않는 서버는 `shutdown()` 이 돌아오지 않으므로 뺀다."""
        self.events.close()
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._httpd.server_close()

    # ── 호출 ──────────────────────────────────────────────────────────────
    def call(self, name: str, args: list, *, client: str = "") -> dict:
        """이름 하나를 부른다 — 없는 이름·거절도 같은 `{"error": {...}}` 모양이다."""
        if client not in API_CLIENTS:
            return _rejected(
                f"{CLIENT_HEADER} 머리가 없거나 모르는 값이라 부르지 않았다 — 이 서버의 "
                f"이름은 관리자 화면(창·브라우저)만 부른다.")
        if name not in api_names(self.api):
            return _rejected(f"화면이 부르는 브리지 이름이 아니다: {name!r}")
        method = getattr(self.api, name)
        if name == "pick_folder" and client != WINDOW_CLIENT:
            return _rejected("폴더 고르기 대화상자는 창에서만 열린다 — 브라우저에서는 경로를 "
                             "직접 입력하라.")
        key = project_key(method, args)
        if key is None:
            return method(*args)
        if name in WRITE_NAMES:
            return self._call_write(name, method, args, key)
        running = self._running(key)
        if running is not None:
            return _rejected(self._busy_message(key, running))
        return method(*args)

    def _call_write(self, name: str, method, args: list, key: str) -> dict:
        with self._writes_lock:
            entry = self._writes.setdefault(key, [threading.Lock(), ""])
            if not entry[0].acquire(blocking=False):
                return _rejected(self._busy_message(key, entry[1]))
            entry[1] = name
        try:
            return method(*args)
        finally:
            with self._writes_lock:
                entry[1] = ""
                entry[0].release()
            self.events.publish({"name": name, "path": key})

    def _running(self, key: str) -> str | None:
        with self._writes_lock:
            entry = self._writes.get(key)
            return entry[1] or None if entry is not None else None

    @staticmethod
    def _busy_message(key: str, running: str) -> str:
        return (f"{key} 에 저장·적용({running or '진행 중'})이 도는 중이라 이 요청은 부르지 "
                f"않았다 — 끝난 뒤 다시 불러라.")


def _handler_class(server: ManagerServer):
    """그 서버 하나에 묶인 요청 처리기 — 정적 파일은 표준 처리기가 그대로 낸다."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, directory=str(server.dist), **kwargs)

        def log_message(self, *_args) -> None:  # 요청 로그는 콘솔 안내와 섞이므로 내지 않는다
            pass

        def do_GET(self) -> None:  # noqa: N802 (표준 라이브러리 계약)
            if self.path.split("?")[0] == EVENTS_PATH:
                self._stream_events()
                return
            # 창이 자기를 밝히는 표식은 질의 문자열로 온다 — 정적 파일 이름에서 떼어 낸다.
            self.path = self.path.split("?")[0]
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802 (표준 라이브러리 계약)
            path = self.path.split("?")[0]
            if not path.startswith(API_PREFIX):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            args = json.loads(body.decode("utf-8")).get("args", [])
            response = server.call(path[len(API_PREFIX):], list(args),
                                   client=self.headers.get(CLIENT_HEADER, ""))
            self._send_json(response)

        def _send_json(self, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream_events(self) -> None:
            listener = server.events.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while not server.events.closed:
                    try:
                        event = listener.get(timeout=EVENT_POLL_SECONDS)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        payload = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                # 손님이 창을 닫으면 소켓이 끊긴다 — 그 자체가 구독 종료다.
                pass
            finally:
                server.events.unsubscribe(listener)

    return Handler
