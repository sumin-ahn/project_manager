"""opencode 첫-이벤트 stall 워치독 (T-0336) — 단위(hermetic) + 결정적 픽스처 e2e.

opencode `run` 이 스타트업 network fetch stall 에 빠지면 `--format json` stdout 이벤트가 0바이트로
**영원히** 멈춘다(PM 70 라이브 실측·upstream #13841). 호출층 워치독이 이를 닫는다: 첫 이벤트가 N초 내
안 오면 프로세스 그룹째 kill → 재시도(M회) → 소진 시 fail-loud. 무한 hang → 유한 재시도.

검증 축 (ticket DoD):
  ① 엔진 헬퍼 `run_with_first_event_watchdog` — 주입 popen/clock hermetic(바이너리 불요·CI 상시):
     stall 소진→StallWatchdogError · 재시도 성공 경로 · overall(mid-turn) drain 백스톱 · kill · loud.
  ② env 노브 `PM_OC_FIRST_EVENT_TIMEOUT`/`PM_OC_STALL_RETRIES` 해소기 동작.
  ③ opencode driver 배선 — `_make_watchdog_runner` 가 엔진 워치독 호출 · `_turn` 이 stall 소진에
     loud + fail-soft(stall_error 주입 시) · 미주입 시 전파(sensitivity).
  ④ pm_import `--fill auto` opencode 경로 워치독 경유 · claude 경로는 미경유.
  ⑤ **결정적 e2e**(skipif opencode 부재) — 무응답 소켓 서버(연결 수락·응답 0)를 baseURL 로 물린
     스크래치 config·실 opencode 바이너리로 워치독이 kill+재시도+fail-loud 하는지(first-event
     timeout 을 5초로 낮춰 수십 초 내 검증).
"""
from __future__ import annotations

import importlib.util
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
OPENCODE_DRIVER = REPO / "templates" / "opencode" / ".opencode" / "pm_orch_opencode.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_engine():
    return _load("pm_relay", TOOLS / "pm_relay.py")


def _load_driver():
    return _load("pm_orch_opencode", OPENCODE_DRIVER)


def _load_pm_import():
    return _load("pm_import", TOOLS / "pm_import.py")


# ── hermetic fakes (스레드·실 subprocess·실 clock 없이 워치독 로직만 결정적으로 구동) ──

class _FakeClock:
    """단조 fake clock — sleep 이 advance 해 폴 루프를 결정적으로 전진시킨다."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeProc:
    """스크립트 fake 프로세스. first_at=None → 영원히 stall(첫 이벤트 안 옴·alive 유지)."""

    def __init__(self, clock: _FakeClock, *, first_at, rc: int = 0,
                 stdout: str = "", stderr: str = "", communicate_raises=None) -> None:
        self._clock = clock
        self._start = clock()
        self._first_at = first_at
        self._rc = rc
        self._stdout = stdout
        self._stderr = stderr
        self._communicate_raises = communicate_raises
        self.kill_count = 0

    def first_event_ready(self) -> bool:
        if self._first_at is None:
            return False
        return (self._clock() - self._start) >= self._first_at

    def poll(self):
        if self._first_at is None:
            return None  # stall — 영원히 alive.
        return self._rc if self.first_event_ready() else None

    def kill(self) -> None:
        self.kill_count += 1

    def communicate(self, timeout=None):
        if self._communicate_raises is not None:
            raise self._communicate_raises
        return self._stdout, self._stderr

    @property
    def returncode(self) -> int:
        return self._rc


def _scripted_popen(procs):
    """popen seam — 시도마다 다음 스크립트 proc 를 돌려준다."""
    it = iter(procs)
    return lambda _argv: next(it)


# ── ① 엔진 헬퍼 (hermetic·주입 popen/clock) ─────────────────────────────────────

def test_watchdog_stall_exhausts_to_error():
    """모든 시도가 첫 이벤트 없이 stall → StallWatchdogError(fail-loud) + 시도당 kill·loud 1줄."""
    engine = _load_engine()
    clock = _FakeClock()
    logs: list[str] = []
    procs = [_FakeProc(clock, first_at=None), _FakeProc(clock, first_at=None)]
    with pytest.raises(engine.StallWatchdogError) as excinfo:
        engine.run_with_first_event_watchdog(
            ["opencode", "run"],
            first_event_timeout=5.0, overall_timeout=600.0, retries=1,
            popen=_scripted_popen(procs), clock=clock,
            sleep=clock.advance, log=logs.append, poll_interval=1.0,
        )
    assert "재시도 소진" in str(excinfo.value)
    assert [p.kill_count for p in procs] == [1, 1]   # 두 시도 모두 kill.
    assert len(logs) == 2                             # 시도당 loud 1줄.
    assert "재시도 1/2" in logs[0] and "재시도 2/2" in logs[1]
    assert all("stall watchdog" in m for m in logs)


def test_watchdog_retry_success_after_one_stall():
    """1회 stall 후 성공 stub → 결과 정상 반환(재시도 성공 경로·kill 은 stall 것만)."""
    engine = _load_engine()
    clock = _FakeClock()
    logs: list[str] = []
    stall = _FakeProc(clock, first_at=None)
    ok = _FakeProc(clock, first_at=0.0, rc=0,
                   stdout='{"type":"text","part":{"text":"PONG"}}\n', stderr="")
    result = engine.run_with_first_event_watchdog(
        ["opencode", "run"],
        first_event_timeout=5.0, overall_timeout=600.0, retries=2,
        popen=_scripted_popen([stall, ok]), clock=clock,
        sleep=clock.advance, log=logs.append, poll_interval=1.0,
    )
    assert result.returncode == 0
    assert result.stdout == '{"type":"text","part":{"text":"PONG"}}\n'
    assert result.args == ["opencode", "run"]
    assert stall.kill_count == 1 and ok.kill_count == 0
    assert len(logs) == 1 and "재시도 1/3" in logs[0]  # stall 한 번만 loud.


def test_watchdog_first_event_within_window_returns():
    """첫 이벤트가 창 안(지연 도착)이면 정상 완료 — 폴 루프가 clock 전진으로 도달을 관측."""
    engine = _load_engine()
    clock = _FakeClock()
    # first_at=3s < first_event_timeout=5s → 창 안에서 관측(stall 아님).
    proc = _FakeProc(clock, first_at=3.0, rc=0, stdout="event\n")
    result = engine.run_with_first_event_watchdog(
        ["opencode", "run"],
        first_event_timeout=5.0, overall_timeout=600.0, retries=0,
        popen=_scripted_popen([proc]), clock=clock,
        sleep=clock.advance, log=[].append, poll_interval=1.0,
    )
    assert result.returncode == 0 and result.stdout == "event\n"
    assert proc.kill_count == 0


def test_watchdog_overall_timeout_during_drain_raises():
    """첫 이벤트는 왔으나 완료 drain 이 overall 초과 → TimeoutExpired 전파 + kill(mid-turn 백스톱)."""
    engine = _load_engine()
    clock = _FakeClock()
    proc = _FakeProc(
        clock, first_at=0.0,
        communicate_raises=subprocess.TimeoutExpired(["opencode"], 600),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        engine.run_with_first_event_watchdog(
            ["opencode", "run"],
            first_event_timeout=5.0, overall_timeout=600.0, retries=0,
            popen=_scripted_popen([proc]), clock=clock,
            sleep=clock.advance, log=[].append, poll_interval=1.0,
        )
    assert proc.kill_count == 1  # drain timeout 도 kill 로 정리.


def test_watchdog_fast_exit_without_event_drains():
    """첫 이벤트 없이 빠른 종료(rc≠0·에러)는 stall 아님 — drain 이 결과를 수습해 반환."""
    engine = _load_engine()
    clock = _FakeClock()
    # first_at=0 이면 즉시 poll()이 rc 반환(빠른 exit) — 워치독은 이를 stall 로 안 보고 drain.
    proc = _FakeProc(clock, first_at=0.0, rc=7, stdout="", stderr="boom")
    result = engine.run_with_first_event_watchdog(
        ["opencode", "run"],
        first_event_timeout=5.0, overall_timeout=600.0, retries=1,
        popen=_scripted_popen([proc]), clock=clock,
        sleep=clock.advance, log=[].append, poll_interval=1.0,
    )
    assert result.returncode == 7 and result.stderr == "boom"
    assert proc.kill_count == 0  # 자연 종료라 kill 불요.


# ── ①b 실 어댑터 (opencode-무관·python3 자식·CI 상시·주입 없는 실 _WatchedPopen/kill 경로) ──
# hermetic 12건은 전부 주입 popen 이라 실 _WatchedPopen/_kill_process_group 은 opencode-gated e2e
# 에서만 돌았다(reviewer should-fix). 여기서는 opencode 불요·항상 있는 python3 자식으로 실 어댑터를
# 낮은 타임아웃(1~2초)에 커버한다.

def test_real_popen_first_event_passthrough():
    """즉시 출력하는 실 자식 → 첫-이벤트 관측 → 완료 드레인 → 결과 정상 반환(실 _WatchedPopen)."""
    engine = _load_engine()
    argv = [sys.executable, "-c", "print('EVENT', flush=True)"]
    result = engine.run_with_first_event_watchdog(
        argv, first_event_timeout=2.0, overall_timeout=10.0, retries=1,
    )  # 주입 없음 — 기본 popen(_WatchedPopen)·기본 clock(time.monotonic).
    assert result.returncode == 0
    assert "EVENT" in result.stdout


def test_real_popen_stall_kills_and_fails_loud():
    """무출력 장수명 실 자식(stall) → 실 _kill_process_group 로 kill + 재시도 + fail-loud.

    잔존 프로세스 없음까지 단언(실 kill 검증) — 워치독이 죽인 자식이 모두 reap 된다."""
    engine = _load_engine()
    argv = [sys.executable, "-c", "import time; time.sleep(999)"]  # 첫 이벤트 0·영원히 alive.
    captured: list = []
    logs: list[str] = []

    def watched(a):  # 실 _WatchedPopen 을 쓰되 proc 참조를 잡아 잔존검사.
        proc = engine._WatchedPopen(a, cwd=None, env=None, text=True)
        captured.append(proc)
        return proc

    with pytest.raises(engine.StallWatchdogError):
        engine.run_with_first_event_watchdog(
            argv, first_event_timeout=1.0, overall_timeout=10.0, retries=1,
            popen=watched, log=logs.append,
        )
    assert len(logs) == 2                          # 2 시도(최초+재시도1)·시도당 loud 1줄.
    assert len(captured) == 2                       # 매 시도 실 자식을 띄웠다.
    for proc in captured:
        assert proc.poll() is not None, "워치독 kill 후에도 실 자식이 살아있다(잔존)"
        if os.name == "posix":                      # pid 자체가 사라졌는지(실 kill) 확인.
            with pytest.raises(ProcessLookupError):
                os.kill(proc._proc.pid, 0)


def test_real_popen_stderr_only_drains_nonblocking():
    """stderr 로만 뿜는 실 자식 → 별도 stderr 펌프 스레드가 비블로킹으로 드레인(파이프 데드락 없음)."""
    engine = _load_engine()
    # stdout 무출력·stderr 로 다수 라인 뿜고 종료 → 첫-stdout-이벤트 없이 빠른 종료 경로 +
    # stderr 펌프가 파이프 버퍼를 막지 않고 전부 수집하는지.
    argv = [
        sys.executable, "-c",
        "import sys\n"
        "for i in range(4000): sys.stderr.write('ERR%d\\n' % i)\n"
        "sys.stderr.flush()",
    ]
    result = engine.run_with_first_event_watchdog(
        argv, first_event_timeout=2.0, overall_timeout=10.0, retries=1,
    )
    assert result.returncode == 0
    assert result.stdout == ""                       # stdout 무출력.
    assert "ERR0\n" in result.stderr                 # stderr 드레인 성공(비블로킹).
    assert result.stderr.count("ERR") == 4000        # 전량 수집(버퍼 데드락 없음).


# ── ② env 노브 해소기 ──────────────────────────────────────────────────────────

def test_env_knob_first_event_timeout(monkeypatch):
    engine = _load_engine()
    monkeypatch.delenv("PM_OC_FIRST_EVENT_TIMEOUT", raising=False)
    assert engine.first_event_timeout_default() == 90.0            # 기본.
    monkeypatch.setenv("PM_OC_FIRST_EVENT_TIMEOUT", "5")
    assert engine.first_event_timeout_default() == 5.0
    monkeypatch.setenv("PM_OC_FIRST_EVENT_TIMEOUT", "  10.5  ")
    assert engine.first_event_timeout_default() == 10.5           # 공백 trim.
    monkeypatch.setenv("PM_OC_FIRST_EVENT_TIMEOUT", "bogus")
    assert engine.first_event_timeout_default() == 90.0           # 불량 → 기본.
    monkeypatch.setenv("PM_OC_FIRST_EVENT_TIMEOUT", "0")
    assert engine.first_event_timeout_default() == 90.0           # 비양수 → 기본.


def test_env_knob_stall_retries(monkeypatch):
    engine = _load_engine()
    monkeypatch.delenv("PM_OC_STALL_RETRIES", raising=False)
    assert engine.stall_retries_default() == 2                    # 기본.
    monkeypatch.setenv("PM_OC_STALL_RETRIES", "1")
    assert engine.stall_retries_default() == 1
    monkeypatch.setenv("PM_OC_STALL_RETRIES", "0")
    assert engine.stall_retries_default() == 0                    # 0 재시도 허용(음이 아님).
    monkeypatch.setenv("PM_OC_STALL_RETRIES", "-3")
    assert engine.stall_retries_default() == 2                    # 음수 → 기본.
    monkeypatch.setenv("PM_OC_STALL_RETRIES", "x")
    assert engine.stall_retries_default() == 2                    # 불량 → 기본.


# ── ③ opencode driver 배선 ─────────────────────────────────────────────────────

def test_driver_watchdog_runner_calls_engine():
    """`_make_watchdog_runner` 가 엔진 워치독을 env 노브·overall_timeout 로 호출한다."""
    engine = _load_engine()
    driver_mod = _load_driver()
    calls: dict = {}

    class FakeEngine:
        StallWatchdogError = engine.StallWatchdogError

        @staticmethod
        def first_event_timeout_default():
            return 7.0

        @staticmethod
        def stall_retries_default():
            return 3

        @staticmethod
        def run_with_first_event_watchdog(cmd, **kw):
            calls["cmd"] = cmd
            calls["kw"] = kw
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

    runner = driver_mod._make_watchdog_runner(FakeEngine)
    result = runner(["opencode", "run", "x"], capture_output=True, text=True, timeout=600)
    assert result.returncode == 0 and result.stdout == "ok"
    assert calls["cmd"] == ["opencode", "run", "x"]
    assert calls["kw"]["first_event_timeout"] == 7.0
    assert calls["kw"]["overall_timeout"] == 600      # driver 의 600s hard 가드 전달.
    assert calls["kw"]["retries"] == 3


def test_driver_turn_failsoft_on_stall():
    """runner 가 StallWatchdogError 를 던지면 driver `_turn` 은 loud + fail-soft(빈 reply)."""
    engine = _load_engine()
    driver_mod = _load_driver()

    def stall_runner(cmd, **kw):
        raise engine.StallWatchdogError("stall 소진 테스트")

    driver = driver_mod.OpencodeCliDriver(
        engine.parse_opencode_json, runner=stall_runner,
        stall_error=engine.StallWatchdogError,
    )
    assert driver.relay_turn("ses", "hi") == ""   # 루프 안 죽고 빈 reply.


def test_driver_turn_without_stall_error_propagates():
    """stall_error 미주입이면 `except ()` 가 아무것도 안 잡아 StallWatchdogError 전파(sensitivity)."""
    engine = _load_engine()
    driver_mod = _load_driver()

    def stall_runner(cmd, **kw):
        raise engine.StallWatchdogError("stall")

    driver = driver_mod.OpencodeCliDriver(engine.parse_opencode_json, runner=stall_runner)
    with pytest.raises(engine.StallWatchdogError):
        driver.relay_turn("ses", "hi")


# ── ④ pm_import `--fill auto` opencode 경로 라우팅 ─────────────────────────────

def test_pm_import_opencode_fill_routes_watchdog(monkeypatch):
    """opencode fill argv 는 워치독 경유 — stall 소진은 (False, 안내)로 fail-soft."""
    pm_import = _load_pm_import()
    called: dict = {}

    class FakeEngine:
        class StallWatchdogError(RuntimeError):
            pass

        @staticmethod
        def first_event_timeout_default():
            return 5.0

        @staticmethod
        def stall_retries_default():
            return 1

        @staticmethod
        def run_with_first_event_watchdog(argv, **kw):
            called["argv"] = argv
            called["kw"] = kw
            raise FakeEngine.StallWatchdogError("stall!")

    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: FakeEngine)
    ok, out = pm_import._real_harness_runner(["opencode", "run", "hi"], "prompt")
    assert ok is False
    assert "stall" in out and "재시도 소진" in out
    assert called["argv"] == ["opencode", "run", "hi"]
    assert called["kw"]["overall_timeout"] == pm_import.FILL_TIMEOUT_SECONDS


def test_pm_import_claude_fill_skips_watchdog(monkeypatch):
    """claude fill argv 는 워치독 미경유 — 기존 subprocess.run 경로(무변경)."""
    pm_import = _load_pm_import()

    def _boom():
        raise AssertionError("claude 경로가 워치독을 로드하면 안 된다")

    monkeypatch.setattr(pm_import, "_load_watchdog", _boom)

    class _Fake:
        returncode = 0
        stdout = "draft text"
        stderr = ""

    monkeypatch.setattr(pm_import.subprocess, "run", lambda *a, **k: _Fake())
    ok, out = pm_import._real_harness_runner(["claude", "-p", "hi"], "prompt")
    assert ok is True and "draft text" in out


# ── ⑤ 결정적 픽스처 e2e (무응답 소켓 서버 + 실 opencode·skipif 부재) ─────────────

def _loopback_socket_capability(socket_factory=socket.socket) -> tuple[bool, str | None]:
    """AF_INET loopback bind 가능 여부만 probe한다(외부 송신 0).

    일부 network-off sandbox 는 socket() 자체 또는 loopback bind 를 EPERM 으로 막는다.
    그 환경에서는 실 socket E2E를 skip 하되, 아래의 hermetic watchdog 계약 검증은 계속 실행한다.
    ``socket_factory`` seam 은 capability 허용/거부 분기를 소켓 없이 고정한다.
    """
    srv = None
    try:
        srv = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.EACCES}:
            return False, f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if srv is not None:
            srv.close()
    return True, None


def _require_loopback_socket_capability() -> None:
    available, reason = _loopback_socket_capability()
    if not available:
        pytest.skip(
            "loopback AF_INET capability unavailable "
            f"({reason}); real opencode stall E2E skipped. "
            "Run the integration test in an environment that permits loopback sockets."
        )


def test_loopback_socket_capability_probe_allows_bind_without_network():
    """허용 분기: probe 는 loopback bind 뒤 close만 하며 외부 연결/송신을 하지 않는다."""
    calls: list[object] = []

    class FakeSocket:
        def bind(self, address):
            calls.append(address)

        def listen(self, backlog):
            calls.append(("listen", backlog))

        def close(self):
            calls.append("close")

    available, reason = _loopback_socket_capability(lambda *_args: FakeSocket())
    assert (available, reason) == (True, None)
    assert calls == [("127.0.0.1", 0), ("listen", 1), "close"]


def test_loopback_socket_capability_probe_reports_permission_denied():
    """거부 분기: network-off EPERM 을 fail이 아닌 이유 있는 E2E skip 대상으로 식별한다."""
    def denied_socket(*_args):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    available, reason = _loopback_socket_capability(denied_socket)
    assert available is False
    assert reason is not None
    assert "PermissionError" in reason and "Operation not permitted" in reason


def test_loopback_capability_requirement_skips_with_integration_guidance(monkeypatch):
    """capability 부재 E2E는 명확한 사유와 권한 있는 실행 경로를 출력하고 skip 한다."""
    monkeypatch.setitem(
        globals(), "_loopback_socket_capability",
        lambda: (False, "PermissionError: [Errno 1] Operation not permitted"),
    )
    with pytest.raises(pytest.skip.Exception, match="loopback AF_INET capability unavailable") as excinfo:
        _require_loopback_socket_capability()
    assert "Run the integration test" in str(excinfo.value)


def _dead_server():
    """연결은 수락하되 응답을 절대 보내지 않는 소켓 서버 — opencode startup fetch stall 재현.

    accept 루프가 연결을 받아 참조만 유지(응답 0). opencode 가 이 baseURL 로 fetch 하면 응답이
    없어 영원히 hang(PM 70 브라운아웃 재현). (port, close-fn) 반환."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    conns: list = []

    def accept_loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            conns.append(conn)  # 수락만·응답 없음.

    threading.Thread(target=accept_loop, daemon=True).start()

    def close():
        srv.close()
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass

    return port, close


@pytest.mark.skipif(
    shutil.which("opencode") is None,
    reason="결정적 stall e2e — 실 opencode 바이너리 필요(CI 상시 경로는 hermetic 단위테스트).",
)
@pytest.mark.integration
def test_opencode_stall_watchdog_e2e_kill_retry_failloud(tmp_path, monkeypatch):
    """무응답 서버를 baseURL 로 물린 실 opencode 를 워치독이 kill+재시도+fail-loud 하는지 e2e.

    first-event timeout 을 env 로 5초로 낮춰(총 소요 짧게) 무한 hang 이 아니라 유한 재시도 후
    StallWatchdogError 로 끝나는지 실측. 실 subprocess·실 소켓·주입 없는 기본 popen/clock 경로."""
    engine = _load_engine()
    _require_loopback_socket_capability()
    port, close = _dead_server()
    try:
        cfg = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "stall": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "stall",
                    "options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                    "models": {"stall-model": {"name": "stall-model"}},
                }
            },
        }
        (tmp_path / "opencode.json").write_text(
            json.dumps(cfg, indent=2), encoding="utf-8"
        )
        argv = [
            "opencode", "run", "--format", "json", "--dir", str(tmp_path),
            "--dangerously-skip-permissions", "-m", "stall/stall-model",
            "Reply with exactly: PONG",
        ]
        # env 로 첫-이벤트 상한을 5초로 낮춰 kill+재시도+fail-loud 를 수십 초 내 검증.
        monkeypatch.setenv("PM_OC_FIRST_EVENT_TIMEOUT", "5")
        monkeypatch.setenv("PM_OC_STALL_RETRIES", "1")
        logs: list[str] = []
        started = time.monotonic()
        with pytest.raises(engine.StallWatchdogError):
            engine.run_with_first_event_watchdog(
                argv,
                first_event_timeout=engine.first_event_timeout_default(),
                overall_timeout=1800.0,   # turn 상한(백스톱) — 여기 도달 전에 stall 이 잡혀야.
                retries=engine.stall_retries_default(),
                cwd=str(tmp_path),
                log=logs.append,
            )
        elapsed = time.monotonic() - started
        # 무한 hang 이 아님 — 2 시도(최초+재시도1)×5s stall + 기동/kill grace 로 유한 종료.
        assert elapsed < 90, f"워치독이 유한 시간에 안 끝남: {elapsed:.1f}s"
        assert len(logs) == 2, f"시도당 loud 1줄(=2) 기대, got {logs}"
        assert all("stall watchdog" in m for m in logs)
    finally:
        close()
