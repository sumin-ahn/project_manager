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
  ④ pm_import `--fill auto` 세 하네스 공용 워치독 경유 · opencode startup 재시도 불변.
   ⑤ **결정적 e2e**(skipif opencode 부재) — 무응답 소켓 서버(연결 수락·응답 0)를 baseURL 로 물린
      스크래치 config·실 opencode 바이너리로 워치독이 kill+재시도+fail-loud 하는지(first-event
      timeout 을 5초로 낮춰 수십 초 내 검증).

T-opencode-003 부터 이 파일은 제2축도 함께 소유한다: stall-watchdog **플러그인**(세션 idle
미완료 감지·처방 넛지 자동 주입 — templates/opencode/.opencode/{plugins/stall-watchdog.js,
lib/stall-watchdog-core.cjs}). T-0336 축은 *프로세스* 첫-이벤트 hang, 플러그인 축은 *모델 턴*
미완료 종료로 정반대 멈춤이다. 파일명을 공유하는 건 티켓 touches 가 이 경로를 지정하기 때문이며,
플러그인 축 검증은 하단 `T-opencode-003` 섹션이 담당한다(기존 ①~⑤ 검증과 독립).
"""
from __future__ import annotations

import importlib.util
import errno
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from _textio import normalize_newlines

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
    stderr = normalize_newlines(result.stderr)
    assert "ERR0\n" in stderr                        # stderr 드레인 성공(비블로킹).
    assert stderr.count("ERR") == 4000               # 전량 수집(버퍼 데드락 없음).


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
    relay = _load_engine()
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
        def resolve_harness_profile(harness, conf):
            return relay.resolve_harness_profile(harness, conf)

        @staticmethod
        def idle_timeout_for_signal(signal, configured):
            return relay.idle_timeout_for_signal(signal, configured)

        TIMEOUT_AXIS_WALL = relay.TIMEOUT_AXIS_WALL
        TIMEOUT_AXIS_IDLE = relay.TIMEOUT_AXIS_IDLE

        @staticmethod
        def run_with_first_event_watchdog(argv, **kw):
            called["argv"] = argv
            called["kw"] = kw
            raise FakeEngine.StallWatchdogError("stall!")

    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: FakeEngine)
    monkeypatch.setattr(
        pm_import.subprocess, "Popen",
        lambda *args, **kwargs: pytest.fail("테스트가 실 opencode를 spawn하려 함"),
    )
    ok, out = pm_import._real_harness_runner(
        ["opencode", "run", "hi", "--format", "json"], "prompt")
    assert ok is False
    assert "stall" in out and "재시도 소진" in out
    assert called["argv"] == ["opencode", "run", "hi", "--format", "json"]
    profile = relay.HARNESS_PROFILES["opencode"]
    assert called["kw"]["overall_timeout"] == profile.wall_timeout
    assert called["kw"]["idle_timeout"] == profile.idle_timeout


def test_pm_import_claude_fill_routes_shared_watchdog(monkeypatch):
    """claude fill도 공용 워치독 경유 — startup 재시도만 선언대로 꺼진다."""
    pm_import = _load_pm_import()
    relay = _load_engine()
    called = {}

    class _FakeRelay:
        def __getattr__(self, name):
            return getattr(relay, name)

        def run_with_first_event_watchdog(self, argv, **kwargs):
            called.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "draft text", "")

    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: _FakeRelay())
    monkeypatch.setattr(
        pm_import.subprocess, "Popen",
        lambda *args, **kwargs: pytest.fail("테스트가 실 claude를 spawn하려 함"),
    )
    ok, out = pm_import._real_harness_runner(["claude", "-p", "hi"], "prompt")
    assert ok is True and "draft text" in out
    profile = relay.HARNESS_PROFILES["claude"]
    assert called["first_event_timeout"] is None
    assert called["retries"] == 0
    assert called["overall_timeout"] == profile.wall_timeout
    assert called["idle_timeout"] is None


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


# ── T-opencode-003: stall-watchdog 플러그인 (세션 idle 미완료 감지·처방 넛지) ──────────────
#
# 검증 축 (ticket DoD):
#   ㉠ 파일 존재 + 인스턴스 사본 byte-identical + ESM 로드 규약(shim = 팩토리 단일 named-export,
#     core 는 lib/ CJS — safe-write 이중구조 선례).
#   ㉡ core 정적 계약 — @opencode-ai/plugin 비의존(node require 보존)·env 노브 3종·session.idle
#     구독 + client.session.prompt 주입 배선.
#   ㉢ (node 있으면) core 순수 로직 자가검증 — classifyStall 3종 양성/음성(선언+tool 파트 쌍·
#     interleaved [선언,tool,완료] 완료 턴 F-001 회귀·truncated 단일 신호 억제·결론형 종료 정상)·
#     isAbortOutcome(abort 직후 넛지 금지)·isSelfNudgeEntry(F-002 자기 넛지 판별)·buildNudge
#     3종 문어체·shouldNudge 게이트 경계(연속 무진행 3 차단·진행 리셋·백스톱 20·사용자 해제)·env
#     override. node 부재 시 skip.
#   ㉣ (node 있으면) 팩토리 배선 자가검증 — fake client 로 session.idle 구동: 넛지 주입(sessionID+
#     text parts)·interleaved 완료 턴 넛지 없음(F-001)·연속 차단·abort 면제·todo 진행 리셋·넛지
#     user 엔트리 비해제(F-002)·사용자 메시지 영구 해제·주입 실패 흡수(never-block)·durable marker
#     영속 확인. node 부재 시 skip.
#   ㉤ (node 있으면) 게이트 상태 프로세스 간 이어짐 — headless one-shot 모델 대응의 핵심 계약을
#     별개 node 프로세스 3개(save→load+nudge→load)로 실측. node 부재 시 skip.

STALL_OPENCODE = REPO / "templates" / "opencode" / ".opencode"
STALL_PLUGIN_FILE = STALL_OPENCODE / "plugins" / "stall-watchdog.js"  # ESM 진입점 shim.
STALL_CORE_FILE = STALL_OPENCODE / "lib" / "stall-watchdog-core.cjs"  # 순수 로직·팩토리 본체(CJS).
STALL_INSTANCE_PLUGIN = REPO / ".opencode" / "plugins" / "stall-watchdog.js"
STALL_INSTANCE_CORE = REPO / ".opencode" / "lib" / "stall-watchdog-core.cjs"

# node 자가검증 대상(core 순수 로직) — node 부재 환경은 정적 검증만 적용하고 skip(safe-write 선례).
_NODE = shutil.which("node")


def _stall_core_src() -> str:
    return STALL_CORE_FILE.read_text(encoding="utf-8")


def _stall_shim_src() -> str:
    return STALL_PLUGIN_FILE.read_text(encoding="utf-8")


def _run_stall_node_script(script: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """stall core 자가검증용 node 실행 (safe-write _run_node_check 과 동일 cwd·env 확장만 허용)."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [_NODE, "-e", script],
        cwd=str(STALL_CORE_FILE.parent),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_stall_watchdog_plugin_files_exist_and_instance_copies_match():
    """templates 정본(plugins shim + lib core)과 루트 인스턴스 사본이 존재하고 byte-identical 하다."""
    assert STALL_PLUGIN_FILE.exists(), f"stall-watchdog 진입점 shim 없음: {STALL_PLUGIN_FILE}"
    assert STALL_CORE_FILE.exists(), f"stall-watchdog core 모듈 없음: {STALL_CORE_FILE}"
    assert STALL_INSTANCE_PLUGIN.exists(), f"인스턴스 shim 없음: {STALL_INSTANCE_PLUGIN}"
    assert STALL_INSTANCE_CORE.exists(), f"인스턴스 core 없음: {STALL_INSTANCE_CORE}"
    assert STALL_INSTANCE_PLUGIN.read_bytes() == STALL_PLUGIN_FILE.read_bytes(), (
        "인스턴스 plugins/stall-watchdog.js 가 templates 정본과 byte 달라짐 (전파 drift)"
    )
    assert STALL_INSTANCE_CORE.read_bytes() == STALL_CORE_FILE.read_bytes(), (
        "인스턴스 lib/stall-watchdog-core.cjs 가 templates 정본과 byte 달라짐 (전파 drift)"
    )


def test_stall_watchdog_entry_is_esm_single_function_export():
    """opencode 로드 규약(T-0283 실측): shim 은 ESM 으로 팩토리 하나만 export 한다.

    (1) CJS module.exports 부재 (2) core import (3) export 문 정확히 1개(StallWatchdogPlugin)
    (4) custom tool 불요로 @opencode-ai/plugin import 도 없다(client 만 주입).
    """
    shim = _stall_shim_src()
    code = "\n".join(ln for ln in shim.splitlines() if not ln.lstrip().startswith("//"))
    assert "module.exports" not in code, "진입점이 CJS module.exports 사용 (T-0283 회귀)"
    assert re.search(
        r'import\s+core\s+from\s+["\']\.\./lib/stall-watchdog-core\.cjs["\']', shim
    ), "진입점이 ../lib/stall-watchdog-core.cjs 를 import 하지 않음"
    assert "@opencode-ai/plugin" not in code, (
        "event 전용 플러그인에 @opencode-ai/plugin import 는 불필요 (core 순수성 계약과 충돌)"
    )
    export_lines = [ln for ln in shim.splitlines() if re.match(r"\s*export\s", ln)]
    assert len(export_lines) == 1, f"export 문이 정확히 1개가 아님: {export_lines}"
    assert "StallWatchdogPlugin" in export_lines[0], (
        f"단일 export 가 StallWatchdogPlugin 팩토리가 아님: {export_lines[0]!r}"
    )
    assert "makeStallWatchdogPlugin" in code and "client" in code, (
        "shim 이 ctx.client 를 core 커링 팩토리에 주입하지 않음"
    )


def test_stall_watchdog_core_static_contract():
    """core 정적 계약 — plugin 패키지 비의존·env 노브 3종·상한 기본값·idle/prompt 배선."""
    src = _stall_core_src()
    # node 자가검증 보존 — core 는 @opencode-ai/plugin 을 require/import 하지 않는다(주석 언급 허용).
    assert not re.search(r'(?:require|import)\s*\(?\s*["\']@opencode-ai/plugin["\']', src), (
        "core 가 @opencode-ai/plugin 을 직접 require/import — node 자가검증 깨짐"
    )
    assert "MAX_CONSEC_DEFAULT = 3" in src, "연속 무진행 기본 3 상수 없음"
    assert "MAX_TOTAL_DEFAULT = 20" in src, "절대 백스톱 기본 20 상수 없음"
    for key in (
        "PM_STALL_WATCHDOG_MAX_CONSEC",
        "PM_STALL_WATCHDOG_MAX_TOTAL",
        "PM_STALL_WATCHDOG_DISABLED",
    ):
        assert key in src, f"env 노브 {key} 없음"
    assert '"session.idle"' in src, "session.idle 이벤트 구독 없음"
    assert "session.prompt" in src, "client.session.prompt 주입 배선 없음"
    assert "makeStallWatchdogPlugin" in src, "커링 팩토리(makeStallWatchdogPlugin) 없음"


def test_stall_watchdog_core_requires_cleanly_in_node():
    """node 가 core 모듈을 깨끗이 require 한다 (@opencode-ai/plugin 미설치여도). node 부재 skip."""
    if _NODE is None:
        pytest.skip("node 없음 — require 검증 skip")
    out = _run_stall_node_script('require("./stall-watchdog-core.cjs"); console.log("REQUIRE_OK");').stdout
    assert "REQUIRE_OK" in out, f"core 모듈 require 실패: {out!r}"


def test_stall_watchdog_core_pure_logic_node_selfcheck():
    """node 로 core 순수 로직 자가검증 — 분류기·처방문·게이트 경계·env override (node 부재 skip).

    classifyStall 3종 양성 + 음성(선언 후 tool 파트 있음·결론형 종료=사용자 대기·truncated 단일
    신호 억제)·abort 면제·buildNudge 3종 문어체·게이트(연속 무진행 3 도달 차단·진행 리셋·백스톱
    20·사용자 해제·오염 상태 정규화)·env 노브 해소.
    """
    if _NODE is None:
        pytest.skip("node 없음 — 순수 로직 자가검증 skip")

    script = r"""
const m = require("./stall-watchdog-core.cjs");
const assert = require("node:assert");

// export 표면.
for (const fn of ["classifyStall","buildNudge","shouldNudge","makeStallWatchdogPlugin",
                  "isAbortOutcome","freshStallState","normalizeStallState","recordProgress",
                  "releaseWatchdog","recordNudgeFired","saveStallState","loadStallState",
                  "resolveMaxConsec","resolveMaxTotal","isDisabled"]) {
  assert.strictEqual(typeof m[fn], "function", "missing export: " + fn);
}
assert.strictEqual(m.MAX_CONSEC_DEFAULT, 3);
assert.strictEqual(m.MAX_TOTAL_DEFAULT, 20);
assert.strictEqual(m.MAX_CONSEC_ENV, "PM_STALL_WATCHDOG_MAX_CONSEC");
assert.strictEqual(m.MAX_TOTAL_ENV, "PM_STALL_WATCHDOG_MAX_TOTAL");
assert.strictEqual(m.DISABLED_ENV, "PM_STALL_WATCHDOG_DISABLED");

// ── env override (>0 정수만·아니면 기본 / DISABLED truthy 집합) ──────────────────
assert.strictEqual(m.resolveMaxConsec({}), 3);
assert.strictEqual(m.resolveMaxConsec({PM_STALL_WATCHDOG_MAX_CONSEC:"7"}), 7);
assert.strictEqual(m.resolveMaxConsec({PM_STALL_WATCHDOG_MAX_CONSEC:" 9 "}), 9);
assert.strictEqual(m.resolveMaxConsec({PM_STALL_WATCHDOG_MAX_CONSEC:"0"}), 3);
assert.strictEqual(m.resolveMaxConsec({PM_STALL_WATCHDOG_MAX_CONSEC:"-2"}), 3);
assert.strictEqual(m.resolveMaxConsec({PM_STALL_WATCHDOG_MAX_CONSEC:"1.5"}), 3);
assert.strictEqual(m.resolveMaxConsec({PM_STALL_WATCHDOG_MAX_CONSEC:"x"}), 3);
assert.strictEqual(m.resolveMaxTotal({PM_STALL_WATCHDOG_MAX_TOTAL:"50"}), 50);
assert.strictEqual(m.resolveMaxTotal({PM_STALL_WATCHDOG_MAX_TOTAL:"0"}), 20);
assert.strictEqual(m.resolveMaxTotal({}), 20);
assert.strictEqual(m.isDisabled({}), false);
assert.strictEqual(m.isDisabled({PM_STALL_WATCHDOG_DISABLED:"1"}), true);
assert.strictEqual(m.isDisabled({PM_STALL_WATCHDOG_DISABLED:"true"}), true);
assert.strictEqual(m.isDisabled({PM_STALL_WATCHDOG_DISABLED:"ON"}), true);
assert.strictEqual(m.isDisabled({PM_STALL_WATCHDOG_DISABLED:"0"}), false);

const LIM = {maxConsec:3, maxTotal:20};

// ── classifyStall 양성 3종 ────────────────────────────────────────────────────
let v = m.classifyStall("새 설정 파일을 생성하겠습니다.", [], false);
assert.deepStrictEqual(v, {stall:true, kind:"declare-no-action"});
v = m.classifyStall("I'll create the config now.", [], false);
assert.deepStrictEqual(v, {stall:true, kind:"declare-no-action"});
v = m.classifyStall("이제 파일을 수정하고", [{status:"pending",content:"a"},{status:"completed",content:"b"}], false);
assert.deepStrictEqual(v, {stall:true, kind:"open-todos"});
// in_progress 도 미완료 산입.
v = m.classifyStall("계속 진행 중", [{status:"in_progress"}], false);
assert.deepStrictEqual(v, {stall:true, kind:"open-todos"});
// truncated 신호 + 미완료 todo 결합 시에만 truncated 처방 (매달린 괄호).
v = m.classifyStall("코드는 다음과 같다 (", [{status:"pending"}], false);
assert.deepStrictEqual(v, {stall:true, kind:"truncated"});
// 미종결 코드블록(홀수 펜스) + 미완료 todo → truncated.
v = m.classifyStall("예제:\n```python\nprint(1)\n", [{status:"pending"}], false);
assert.deepStrictEqual(v, {stall:true, kind:"truncated"});

// ── classifyStall 음성 (보수적 트리거 — 오검출 억제) ──────────────────────────
// todo 부재 + 결론형 종료 = 정상(사용자 대기).
v = m.classifyStall("작업을 완료했습니다.", [{status:"pending"}], false);
assert.strictEqual(v.stall, false);
// 선언 후 tool 파트 존재 → 실행된 것 (architect should-fix 오판 차단).
v = m.classifyStall("파일을 생성하겠습니다.", [], true);
assert.strictEqual(v.stall, false);
// F-001 회귀 — [text(선언), tool, text(완료)] 결합 텍스트는 선언에 매칭돼도 이미 실행을 마친
// 완료 턴이다. 핸들러와 동일하게 마지막 tool 이후 text 구간을 declCandidate 로 주입해야 한다.
const interleave = [
  {type:"text", text:"설정 파일을 생성하겠습니다."},
  {type:"tool"},
  {type:"text", text:"생성을 완료했습니다."},
];
v = m.classifyStall(
  m.extractAssistantText(interleave), [],
  m.hasToolPartAfterText(interleave),
  m.extractAssistantTextAfterLastTool(interleave),
);
assert.strictEqual(v.stall, false, "interleaved 완료 턴을 declare-no-action 으로 오검출 (F-001)");
// 보존 — 단일 text 선언 NO-WRITE 는 여전히 양성 (핸들러 4인자 경로).
const soloDecl = [{type:"text", text:"새 파일을 생성하겠습니다."}];
v = m.classifyStall(
  m.extractAssistantText(soloDecl), [],
  m.hasToolPartAfterText(soloDecl),
  m.extractAssistantTextAfterLastTool(soloDecl),
);
assert.deepStrictEqual(v, {stall:true, kind:"declare-no-action"});
// 보존 — 마지막 tool 이후 꼬리 발화의 선언(실행 없음)도 양성.
const tailDecl = [{type:"tool"},{type:"text", text:"이제 결과 파일을 생성하겠습니다."}];
v = m.classifyStall(
  m.extractAssistantText(tailDecl), [],
  m.hasToolPartAfterText(tailDecl),
  m.extractAssistantTextAfterLastTool(tailDecl),
);
assert.deepStrictEqual(v, {stall:true, kind:"declare-no-action"});
// truncated 단일 신호(todo 부재)는 stall 아님.
v = m.classifyStall("설정은 아래와 같다 (", [], false);
assert.strictEqual(v.stall, false);
// 빈/무효 텍스트 → 관측 불량, 보수적 정상.
assert.strictEqual(m.classifyStall("", [{status:"pending"}], false).stall, false);
assert.strictEqual(m.classifyStall(null, [], false).stall, false);

// ── isAbortOutcome (abort 직후 넛지 금지) ─────────────────────────────────────
assert.strictEqual(m.isAbortOutcome({error:{name:"MessageAbortedError"}}), true);
assert.strictEqual(m.isAbortOutcome({finish:"abort"}), true);
assert.strictEqual(m.isAbortOutcome({finish:"stop"}), false);
assert.strictEqual(m.isAbortOutcome({}), false);
assert.strictEqual(m.isAbortOutcome(null), false);

// ── isSelfNudgeEntry (F-002 — 자기 넛지 user 엔트리는 실사용자 도착이 아님) ────
assert.strictEqual(m.isSelfNudgeEntry(
  {info:{role:"user"}, parts:[{type:"text", text:m.NUDGE_MARKER_PREFIX + " 응답이 잘렸다"}]}), true);
assert.strictEqual(m.isSelfNudgeEntry(
  {info:{role:"user"}, parts:[{type:"text", text:"  " + m.NUDGE_MARKER_PREFIX + " x"}]}), true);
assert.strictEqual(m.isSelfNudgeEntry(
  {info:{role:"user"}, parts:[{type:"text", text:"다음은 어떻게 하나요?"}]}), false);
assert.strictEqual(m.isSelfNudgeEntry({info:{role:"user"}, parts:[]}), false);
assert.strictEqual(m.isSelfNudgeEntry(
  {info:{role:"user"}, parts:[{type:"tool"}]}), false);
assert.strictEqual(m.isSelfNudgeEntry(
  {info:{role:"assistant"}, parts:[{type:"text", text:m.NUDGE_MARKER_PREFIX + " x"}]}), false);
assert.strictEqual(m.isSelfNudgeEntry(null), false);

// ── buildNudge (3종 문어체 고정·unknown null) ─────────────────────────────────
assert.ok(m.buildNudge("declare-no-action").includes("safe_write"), "선언 처방이 safe_write 청크 유도 아님");
assert.ok(m.buildNudge("declare-no-action").includes("create→append"));
assert.ok(m.buildNudge("declare-no-action").includes("bash"), "반복 내용 bash 생성 안내 누락");
assert.ok(m.buildNudge("truncated").includes("잘렸"), "truncated 처방 문어체 훼손");
assert.ok(m.buildNudge("truncated").includes("파일로 써라"));
assert.ok(m.buildNudge("open-todos").includes("가장 작은 것"), "open-todos 처방 문어체 훼손");
assert.strictEqual(m.buildNudge("bogus"), null);

// ── shouldNudge 게이트 경계 ───────────────────────────────────────────────────
let s = m.freshStallState();
assert.deepStrictEqual(s, {consecutiveIdle:0,totalNudges:0,released:false,lastCompletedCount:0});
assert.strictEqual(m.shouldNudge(s, LIM), true);
s.consecutiveIdle = 2;
assert.strictEqual(m.shouldNudge(s, LIM), true);      // 연속 무진행 2 — 아직 통과.
s.consecutiveIdle = 3;
assert.strictEqual(m.shouldNudge(s, LIM), false);     // 연속 무진행 3 도달 — 차단(스트릭당 최대 3회 넛지).
// 절대 백스톱 20.
s = m.freshStallState(); s.totalNudges = 19;
assert.strictEqual(m.shouldNudge(s, LIM), true);
s.totalNudges = 20;
assert.strictEqual(m.shouldNudge(s, LIM), false);
// 사용자 해제 — 다른 조건과 무관하게 차단.
s = m.freshStallState();
assert.strictEqual(m.shouldNudge(m.releaseWatchdog(s), LIM), false);
// 진행 감지 리셋 — 연속 카운터만 리셋(누적 백스톱은 유지).
s = m.freshStallState(); s.consecutiveIdle = 5; s.totalNudges = 7;
s = m.recordProgress(s, 4);
assert.strictEqual(s.consecutiveIdle, 0);
assert.strictEqual(s.lastCompletedCount, 4);
assert.strictEqual(s.totalNudges, 7);
assert.strictEqual(m.shouldNudge(s, LIM), true);
// 넛지 발화 전이 — 두 카운터 동시 증가.
s = m.recordNudgeFired(m.freshStallState());
assert.strictEqual(s.consecutiveIdle, 1);
assert.strictEqual(s.totalNudges, 1);
// limits 미주입 → process.env 해소.
process.env.PM_STALL_WATCHDOG_MAX_TOTAL = "2";
s = m.freshStallState(); s.totalNudges = 2;
assert.strictEqual(m.shouldNudge(s), false);
delete process.env.PM_STALL_WATCHDOG_MAX_TOTAL;
s.totalNudges = 1;
assert.strictEqual(m.shouldNudge(s), true);
// 오염 영속 값 정규화 — 부분 JSON 이 게이트를 오동작 시키지 않는다.
const n = m.normalizeStallState({consecutiveIdle:-5, totalNudges:"x", released:"yes", lastCompletedCount:null});
assert.strictEqual(n.consecutiveIdle, 0);
assert.strictEqual(n.totalNudges, 0);
assert.strictEqual(n.released, false);
assert.strictEqual(n.lastCompletedCount, 0);

console.log("STALL_PURE_SELFCHECK_OK");
"""
    out = _run_stall_node_script(script).stdout
    assert "STALL_PURE_SELFCHECK_OK" in out, f"core 순수 로직 자가검증 실패. out={out!r}"


def test_stall_watchdog_factory_wiring_node_selfcheck(tmp_path):
    """node 로 fake client 팩토리 배선 검증 — idle 구독→분류→게이트→prompt 주입 (opencode 없이).

    session.idle 이벤트를 직접 구동해 넛지 주입 형태({path:{id},body:{parts:[{type:"text"}]}}) — SDK v2 런타임은 path 형태만 유효(2026-08-25 프로브 실측)·연속 무진행
    3 도달 차단·abort 면제·todo 진행 리셋·사용자 메시지 영구 해제·주입 실패 흡수(never-block)·
    durable marker 영속까지 확인한다. node 부재 시 skip.
    """
    if _NODE is None:
        pytest.skip("node 없음 — 팩토리 배선 자가검증 skip")

    script = r"""
const m = require("./stall-watchdog-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const td = process.env.STALL_WIRING_ROOT;
function msg(role, parts, extra) {
  return {info: Object.assign({role, sessionID:"ses-1"}, extra || {}), parts: parts || []};
}

(async () => {
  // 엔진 root 흉내 — findEngineRoot 가 .project_manager/tools/pm_log.py 를 발견해 영속화가 켜진다.
  fs.mkdirSync(path.join(td, ".project_manager", "tools"), {recursive:true});
  fs.writeFileSync(path.join(td, ".project_manager", "tools", "pm_log.py"), "");

  const prompts = [];
  let promptShouldThrow = false;
  let currentMessages = [];
  let currentTodos = [];
  const fakeClient = {
    session: {
      messages: async () => ({data: currentMessages}),
      todo: async () => ({data: currentTodos}),
      prompt: async (args) => {
        if (promptShouldThrow) throw new Error("주입 실패 실측");
        prompts.push(args);
        return {data:{}};
      },
    },
  };

  const hooks = await m.makeStallWatchdogPlugin(fakeClient)({directory: td});
  assert.strictEqual(typeof hooks.event, "function");
  const idle = () => hooks.event({event:{type:"session.idle", properties:{sessionID:"ses-1"}}});
  const persisted = () => {
    const dir = path.join(td, ".project_manager", ".local", "stall-watchdog");
    const files = fs.readdirSync(dir).filter((f) => f.startsWith("state."));
    assert.strictEqual(files.length, 1, "세션 마커는 SID 키로 정확히 1개");
    return JSON.parse(fs.readFileSync(path.join(dir, files[0]), "utf-8"));
  };

  // 무관 이벤트 무시.
  await hooks.event({event:{type:"message.updated", properties:{}}});
  assert.strictEqual(prompts.length, 0);

  // ① declare-no-action stall → 넛지 주입 ({path:{id}, body:{parts:[text]}}).
  currentMessages = [msg("assistant", [{type:"text", text:"새 파일을 생성하겠습니다."}])];
  currentTodos = [];
  await idle();
  assert.strictEqual(prompts.length, 1);
  assert.strictEqual(prompts[0].path.id, "ses-1");
  assert.strictEqual(prompts[0].body.parts[0].type, "text");
  assert.ok(prompts[0].body.parts[0].text.includes("safe_write"));

  // 선언 후 tool 파트가 있으면 정상 — 넛지 증가 없음.
  currentMessages = [msg("assistant", [{type:"text", text:"새 파일을 생성하겠습니다."},{type:"tool"}])];
  await idle();
  assert.strictEqual(prompts.length, 1);

  // F-001 회귀 — interleaved [선언, tool, 완료] 다중 text 파트 정상 턴은 넛지하지 않는다.
  currentMessages = [msg("assistant", [
    {type:"text", text:"새 파일을 생성하겠습니다."},
    {type:"tool"},
    {type:"text", text:"생성을 완료했습니다."},
  ])];
  await idle();
  assert.strictEqual(prompts.length, 1, "interleaved 완료 턴에 넛지함 (F-001 오검출)");

  // ② 연속 무진행 게이트 — 같은 멈춤 반복: 스트릭당 3회 넛지 후 차단.
  currentMessages = [msg("assistant", [{type:"text", text:"이어서 작업하고"}])];
  currentTodos = [{content:"a", status:"pending"}];
  await idle();   // open-todos #2 (누적 2).
  await idle();   // #3 (누적 3).
  assert.strictEqual(prompts.length, 3);
  await idle();   // 연속 무진행 3 도달 — 차단.
  assert.strictEqual(prompts.length, 3);
  let st = persisted();
  assert.strictEqual(st.consecutiveIdle, 3);
  assert.strictEqual(st.totalNudges, 3);

  // ③ abort 직후 종료 — 넛지 금지·게이트 소모 없음.
  currentMessages = [msg("assistant", [{type:"text", text:"이어서 작업하고"}], {error:{name:"MessageAbortedError"}})];
  await idle();
  assert.strictEqual(prompts.length, 3);

  // ④ 진행 감지(todo 완료 수 변동) → 연속 카운터 리셋 후 재무장.
  currentTodos = [{content:"a", status:"completed"},{content:"b", status:"pending"}];
  currentMessages = [msg("assistant", [{type:"text", text:"다음 단계를 시작하고"}])];
  await idle();
  assert.strictEqual(prompts.length, 4);
  st = persisted();
  assert.strictEqual(st.consecutiveIdle, 1);
  assert.strictEqual(st.lastCompletedCount, 1);

  // F-002 회귀 — 자기 넛지 직후 assistant 회신 없는 idle: [stall-watchdog] 접두 user 엔트리는
  // 실사용자 도착이 아니므로 워치독이 영구 해제되지 않는다.
  currentMessages = [
    msg("assistant", [{type:"text", text:"작업을 완료했습니다."}]),
    msg("user", [{type:"text", text:"[stall-watchdog] 남은 todo 중 가장 작은 것 하나부터 수행하라."}]),
  ];
  currentTodos = [];
  await idle();
  assert.strictEqual(persisted().released, false, "자기 넛지를 사용자 도착으로 오판해 영구 해제함 (F-002)");
  assert.strictEqual(prompts.length, 4);   // 해제 아님 — 결론형 종료라 stall 도 아니어서 넛지 없음.

  // ⑤ 사용자 메시지 도착(마지막 assistant 이후 user) → 영구 해제.
  currentMessages = [
    msg("assistant", [{type:"text", text:"다음 단계를 시작하고"}]),
    msg("user", []),
  ];
  await idle();
  assert.strictEqual(persisted().released, true);
  await idle();
  assert.strictEqual(prompts.length, 4);   // 해제 상태에선 stall 이어도 넛지 없음.

  // ⑥ 주입 실패도 이벤트 처리를 막지 않고(never-block) 게이트는 소모된다.
  promptShouldThrow = true;
  fs.rmSync(path.join(td, ".project_manager", ".local", "stall-watchdog"), {recursive:true, force:true});
  currentMessages = [msg("assistant", [{type:"text", text:"새 파일을 생성하겠습니다."}])];
  currentTodos = [];
  await idle();                              // firePrompt 예외가 핸들러 밖으로 전파되지 않는다.
  await new Promise((r) => setTimeout(r, 30)); // 비동기 거부 흡수 시점 대기.
  st = persisted();
  assert.strictEqual(st.totalNudges, 1);     // delivery 불확실도 게이트 소모(소음 상한 우선).

  console.log("STALL_WIRING_OK");
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
"""
    out = _run_stall_node_script(script, {"STALL_WIRING_ROOT": str(tmp_path)})
    combined = out.stdout + out.stderr
    assert "STALL_WIRING_OK" in out.stdout, f"팩토리 배선 자가검증 실패. out={combined!r}"


def test_stall_watchdog_state_persists_across_processes_node_selfcheck(tmp_path):
    """게이트 상태가 별개 node 프로세스 3개(save→load+넛지→load)에서 이어진다 (headless one-shot 핵심).

    opencode run 은 프로세스-당-턴 one-shot 이므로 in-memory 카운터는 매 턴 소멸한다 — sessionID 키
    durable marker 로 연속 무진행·백스톱이 프로세스 경계를 넘어 유지되는지 실측. 오염 marker 는
    fail-open(기본 상태)으로 흡수된다. node 부재 시 skip.
    """
    if _NODE is None:
        pytest.skip("node 없음 — 프로세스 간 영속 검증 skip")

    root = tmp_path / "engroot"
    (root / ".project_manager" / "tools").mkdir(parents=True)
    (root / ".project_manager" / "tools" / "pm_log.py").write_text("", encoding="utf-8")
    env = {"STALL_PERSIST_ROOT": str(root)}

    proc_a = r"""
const m = require("./stall-watchdog-core.cjs");
m.saveStallState(process.env.STALL_PERSIST_ROOT, "ses-x",
                 {consecutiveIdle:2, totalNudges:5, released:false, lastCompletedCount:1});
console.log("PERSIST_A_OK");
"""
    proc_b = r"""
const m = require("./stall-watchdog-core.cjs");
const root = process.env.STALL_PERSIST_ROOT;
const s = m.loadStallState(root, "ses-x");
if (s.consecutiveIdle !== 2 || s.totalNudges !== 5 || s.lastCompletedCount !== 1) {
  console.error("프로세스 A 가 저장한 상태가 B 에서 다름: " + JSON.stringify(s)); process.exit(1);
}
m.saveStallState(root, "ses-x", m.recordNudgeFired(s));
console.log("PERSIST_B_OK");
"""
    proc_c = r"""
const m = require("./stall-watchdog-core.cjs");
const root = process.env.STALL_PERSIST_ROOT;
const s = m.loadStallState(root, "ses-x");
if (s.consecutiveIdle !== 3 || s.totalNudges !== 6) {
  console.error("프로세스 B 의 넛지 기록이 C 에서 다름: " + JSON.stringify(s)); process.exit(1);
}
console.log("PERSIST_C_OK");
"""

    out_a = _run_stall_node_script(proc_a, env)
    out_b = _run_stall_node_script(proc_b, env)
    out_c = _run_stall_node_script(proc_c, env)
    assert "PERSIST_A_OK" in out_a.stdout, f"A 저장 실패: {out_a.stdout!r} {out_a.stderr!r}"
    assert "PERSIST_B_OK" in out_b.stdout, f"B 적재·재저장 실패: {out_b.stdout!r} {out_b.stderr!r}"
    assert "PERSIST_C_OK" in out_c.stdout, f"C 적재 실패: {out_c.stdout!r} {out_c.stderr!r}"

    # 오염 marker — fail-open(기본 상태) 흡수.
    marker_dir = root / ".project_manager" / ".local" / "stall-watchdog"
    markers = list(marker_dir.glob("state.*.json"))
    assert len(markers) == 1, f"SID 키 마커가 1개가 아님: {markers}"
    markers[0].write_text("{not-json", encoding="utf-8")
    proc_d = r"""
const m = require("./stall-watchdog-core.cjs");
const s = m.loadStallState(process.env.STALL_PERSIST_ROOT, "ses-x");
if (s.consecutiveIdle !== 0 || s.totalNudges !== 0) {
  console.error("오염 marker 가 기본 상태로 흡수되지 않음: " + JSON.stringify(s)); process.exit(1);
}
console.log("PERSIST_D_OK");
"""
    out_d = _run_stall_node_script(proc_d, env)
    assert "PERSIST_D_OK" in out_d.stdout, f"D 오염 흡수 실패: {out_d.stdout!r} {out_d.stderr!r}"
