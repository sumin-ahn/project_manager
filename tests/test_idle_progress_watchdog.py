"""무진행(idle) 판정 — 벽시계 단독 판정의 정상-진행 false-kill 폐쇄 (T-0489).

**왜 판정 기준을 바꾸나**: 외부 프로세스를 "시작 후 경과 시간"으로 죽이면 임계값이 정상 작업의
분산 대역 *안*에 놓인다. 1차 증거는 통계가 아니라 같은 작업 2회다 — PM 17차가 `external_review` 로
같은 diff·같은 모델을 두 번 돌렸는데 1차는 900초 초과 kill(raw **138바이트**·회신 0), 2차는
`--timeout 1500` 만 붙여 성공(raw 271,713바이트)했다. 어떤 값을 골라도 그 분산 때문에 정상 완주가
잘리고, 잘리면 **산출물이 전량 폐기**된다.

**이 파일이 잠그는 것** (ticket DoD 축):
  ① `_WatchedPopen` 이 마지막 진행(chunk) 도착 시각을 노출 — stdout **과 stderr** 양쪽. 후자가
     load-bearing 이다: 리뷰어 축(`codex exec` 평문)은 진행 로그가 전부 stderr 로 흐르고 stdout 은
     최종 회신뿐이라(실측 498~759바이트 vs stderr 617KB), stdout 만 보면 정상 진행을 전량 오판한다.
  ② `run_with_first_event_watchdog(idle_timeout=...)` — 무진행 초과 시 중단, `None` 이면 현행 불변.
  ③ 위임 3 드라이버(codex·claude·opencode)가 **전부** 워치독 경로.
  ④ 드라이버 능력 **선언**(분기 특례 아님) + 신호 없는 축은 벽시계 유지.
  ⑤ claude `--output-format stream-json` 전환 후에도 회신 추출 동일(파서 동치).
  ⑥ 감사 헤더에 최종 이벤트 이후 침묵 초.
  ⑧ `external_review` 도 **같은 공용 seam**(pm_relay)으로 전환 — 복붙 구현 0.
  ⑨ kill 시점까지 받은 출력 보존(위임·외부리뷰·fill 세 표면).

**벽시계의 위치**: DoD 는 "이벤트가 계속 흐르면 벽시계 초과해도 안 죽음"을 요구하고 §결정은
"무제한 금지 — 유한 백스톱 유지"를 요구한다. 둘의 해소는 *역할 교체* 다 — 옛 벽시계(정상 작업
대역과 겹치던 값)는 더 이상 1차 판정이 아니고(테스트: 무진행 상한의 10배 경과에도 진행 중이면
생존), 벽시계는 감지기 자체가 고장난 경우의 유한 상한으로만 남는다(테스트: 진행 중이어도 백스톱은
결국 닫고 부분 산출물을 보존).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# pytest parameter values are fixed at collection time, before the pm_import fixture exists.
# Load the production registry here so a newly declared fill runner expands this case automatically.
FILL_CAPABLE_HARNESSES = _load(
    "pm_import_fill_parametrize", TOOLS / "pm_import.py"
).FILL_CAPABLE_HARNESSES


@pytest.fixture(scope="module")
def relay():
    return _load("pm_relay", TOOLS / "pm_relay.py")


@pytest.fixture(scope="module")
def pd():
    return _load("pm_delegate", TOOLS / "pm_delegate.py")


@pytest.fixture(scope="module")
def external():
    return _load("external_review", TOOLS / "external_review.py")


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import", TOOLS / "pm_import.py")


# ── hermetic fakes (실 subprocess·실 clock 없이 판정 로직만 결정적으로 구동) ──────────

class _FakeClock:
    """단조 fake clock — sleep 이 advance 해 폴 루프를 결정적으로 전진시킨다."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _BlindProc:
    """진행을 **노출하지 않는** fake proc — `last_event_at`/`partial_output` 이 아예 없다.

    "신호 없는 축" 을 실제 형상으로 태운다(getattr 판정이 vacuous 하지 않게)."""

    def __init__(self, clock, *, exit_at=None, rc=0):
        self._clock = clock
        self._start = clock()
        self._exit_at = exit_at
        self._rc = rc
        self.kill_count = 0
        self._killed = False

    def _elapsed(self) -> float:
        return self._clock() - self._start

    def first_event_ready(self) -> bool:
        return True

    def poll(self):
        if self._killed:
            return -9
        if self._exit_at is not None and self._elapsed() >= self._exit_at:
            return self._rc
        return None

    def kill(self) -> None:
        self.kill_count += 1
        self._killed = True

    def communicate(self, timeout=None):
        return _blocking_communicate(self, timeout, ("", ""))

    @property
    def returncode(self):
        return self.poll()


def _blocking_communicate(proc, timeout, payload):
    """실 `communicate` 의 블로킹 의미론을 fake clock 위에서 재현한다.

    종료 예정 시각이 timeout 안이면 clock 을 그 시점으로 전진시키고 결과를 돌려주고, 아니면
    TimeoutExpired 를 던진다(실 프로세스가 timeout 까지 기다렸다 터지는 것과 동형)."""
    if proc.poll() is not None:
        return payload
    exit_at = proc._exit_at
    remaining = None if exit_at is None else exit_at - proc._elapsed()
    if remaining is not None and (timeout is None or remaining <= timeout):
        proc._clock.advance(remaining)
        return payload
    if timeout is not None:
        proc._clock.advance(timeout)
    raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)


class _ScriptedProc:
    """이벤트 도착 시각이 스크립트된 fake proc(진행 관측 seam 구현).

    - event_times: 이 시각(시작 기준 상대초)에 chunk 가 도착한다 → last_event_at 갱신·부분 출력 누적.
    - exit_at: 이 시각에 종료(None = 영원히 alive).
    """

    def __init__(self, clock, *, event_times=(), exit_at=None, rc=0, chunk="EVENT\n"):
        self._clock = clock
        self._start = clock()
        self._event_times = sorted(event_times)
        self._exit_at = exit_at
        self._rc = rc
        self._chunk = chunk
        self.kill_count = 0
        self._killed = False

    # 관측 seam ────────────────────────────────────────────────
    def _elapsed(self) -> float:
        return self._clock() - self._start

    def _delivered(self) -> list[float]:
        return [t for t in self._event_times if t <= self._elapsed()]

    def last_event_at(self):
        delivered = self._delivered()
        return self._start + delivered[-1] if delivered else None

    def partial_output(self):
        return self._chunk * len(self._delivered()), ""

    def first_event_ready(self) -> bool:
        return bool(self._delivered()) or self.poll() is not None

    def poll(self):
        if self._killed:
            return -9
        if self._exit_at is not None and self._elapsed() >= self._exit_at:
            return self._rc
        return None

    def kill(self) -> None:
        self.kill_count += 1
        self._killed = True

    def communicate(self, timeout=None):
        return _blocking_communicate(self, timeout, self.partial_output())

    @property
    def returncode(self):
        return self.poll()


def _scripted_popen(procs):
    it = iter(procs)
    return lambda _argv: next(it)


_HARNESS_NAMES = frozenset({"codex", "claude", "opencode"})

# 하네스 이름을 소유할 수밖에 없는 **표현/전달 어댑터**만 함수 단위로 면제한다. 판정·timeout·
# 진행신호 함수는 하나도 면제하지 않는다. 값은 리뷰 가능한 근거이며 빈 근거는 아래 가드가 red 낸다
# (T-0493 EXEMPT_FROM_STAMP와 같은 "면제에는 사유" 형식).
_HARNESS_LITERAL_EXEMPTIONS = {
    # argv 조립·wire 파서 선택은 위임/추가 리뷰어 공용 계약이라 pm_relay 가 소유한다(T-0590).
    # pm_delegate 의 같은 이름 함수는 이제 리터럴 없는 wrapper 라 스캔에 걸리지 않는다.
    ("pm_relay", "build_codex_argv"):
        "codex 실행 파일 토큰을 만드는 전용 argv 빌더",
    ("pm_relay", "build_claude_argv"):
        "claude 실행 파일 토큰을 만드는 전용 argv 빌더",
    ("pm_relay", "build_opencode_argv"):
        "opencode 실행 파일 토큰을 만드는 전용 argv 빌더",
    ("pm_relay", "extract_harness_reply"):
        "하네스별 wire format 파서를 고르는 reply 디코더",
    ("pm_delegate", "_build_target_argv"):
        "세 전용 argv 빌더 중 하나를 고르는 전달 어댑터",
    ("pm_delegate", "_prepare_attempt_transport"):
        "opencode만 요구하는 prompt-file wire transport 전용 어댑터(timeout 비소유)",
    ("external_review", "_structured_reviewer_argv"):
        "추가 리뷰어 쪽에서 세 전용 argv 빌더 중 하나를 고르는 전달 어댑터"
        "(pm_delegate._build_target_argv 와 같은 역할·timeout 비소유)",
    ("external_review", "_structured_transport"):
        "opencode만 요구하는 prompt-file wire transport 전용 어댑터(timeout 비소유)",
    ("pm_delegate", "_dry_run_harness_annotations"):
        "dry-run 표시 문구만 만드는 표현 어댑터(timeout 비소유)",
    ("pm_import", "_build_runner_argv"):
        "fill 하네스별 wire argv를 만드는 전달 어댑터(timeout 비소유)",
    ("pm_import", "_real_models_runner"):
        "opencode 모델 목록 wire 명령 전용 runner(LLM fill 판정과 별도)",
    ("pm_import", "run_fill"):
        "하네스별 wire 출력 parser를 고르는 fill reply 디코더(timeout 비소유)",
    ("pm_import", "add_harness"):
        "추가할 어댑터 namespace의 안내 문구 소유(fill 실행 판정과 무관)",
    ("pm_import", "main"):
        "import CLI의 어댑터 선택·안내 표현 소유(timeout 판정은 _real_harness_runner 단독)",
}


def _module_harness_literal_hits(module_name: str, source: str) -> dict[str, list[tuple[int, str]]]:
    """모듈의 **모든 함수/메서드/중첩함수**에서 정확한 하네스 이름 리터럴을 찾는다.

    특정 함수 튜플을 순회하지 않고 AST 구조 자체가 스캔 대상을 정한다. 주석은 AST에 없고
    docstring만 명시적으로 건너뛴다. 따라서 새 호출자/헬퍼를 추가하면 별도 목록 갱신 없이 즉시
    검사 대상이 되며, 정당한 소유자라면 사유가 있는 면제를 리뷰로 추가해야 한다.

    커버 범위는 함수 본문의 **정확한 문자열 리터럴**이다. 모듈 상수 간접참조, 문자열 결합,
    `startswith`, 인덱싱, 모듈레벨 lambda처럼 정확 리터럴 비교가 아닌 우회는 이 가드가 의미를
    추론하지 않는다. 현재 프로덕션에 그런 표기 0이라는 전제의 회귀 가드이지 완전한 taint 분석이
    아니다.
    """
    import ast

    hits: dict[str, list[tuple[int, str]]] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scopes: list[str] = []
            self.functions: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scopes.append(node.name)
            self.generic_visit(node)
            self.scopes.pop()

        def _visit_function(self, node) -> None:
            qualname = ".".join([*self.scopes, node.name])
            self.scopes.append(node.name)
            self.functions.append(qualname)
            # decorators/defaults/annotations도 실행 계약의 일부라 검사한다.
            for decorator in node.decorator_list:
                self.visit(decorator)
            self.visit(node.args)
            if node.returns is not None:
                self.visit(node.returns)
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            for statement in body:
                self.visit(statement)
            self.functions.pop()
            self.scopes.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_Constant(self, node: ast.Constant) -> None:
            if self.functions and node.value in _HARNESS_NAMES:
                hits.setdefault(self.functions[-1], []).append((node.lineno, node.value))

    Visitor().visit(ast.parse(source, filename=f"{module_name}.py"))
    return hits


def _unexpected_harness_literal_hits(
        sources: dict[str, str] | None = None) -> dict[tuple[str, str], list[tuple[int, str]]]:
    sources = sources or {
        name: (TOOLS / f"{name}.py").read_text(encoding="utf-8")
        for name in ("pm_delegate", "external_review", "pm_import", "pm_relay")
    }
    unexpected = {}
    for module_name, source in sources.items():
        for qualname, locations in _module_harness_literal_hits(module_name, source).items():
            key = (module_name, qualname)
            if key not in _HARNESS_LITERAL_EXEMPTIONS:
                unexpected[key] = locations
    return unexpected


# ── ① _WatchedPopen 진행 관측 (실 subprocess·python3 자식·바이너리 불요) ─────────────

def test_watched_popen_tracks_last_event_from_stdout(relay):
    """stdout chunk 도착이 마지막 진행 시각을 세운다 + 부분 출력이 즉시 회수 가능."""
    argv = [sys.executable, "-c", "print('EVENT', flush=True); import time; time.sleep(0.3)"]
    proc = relay._WatchedPopen(argv, cwd=None, env=None, text=True)
    try:
        stdout, _stderr = proc.communicate(timeout=15)
        assert "EVENT" in stdout
        assert proc.last_event_at() is not None
        assert proc.partial_output()[0] == stdout
    finally:
        proc.kill()


def test_watched_popen_tracks_last_event_from_stderr_only(relay):
    """**stderr 로만** 뿜는 자식도 진행으로 관측된다 — 리뷰어 평문 축(진행 로그=stderr) 실측 형상.

    stdout 단독 관측이면 이 축은 "무진행"으로 오판돼 정상 리뷰가 전량 false-kill 된다."""
    argv = [sys.executable, "-c",
            "import sys; sys.stderr.write('PROGRESS\\n'); sys.stderr.flush()"]
    proc = relay._WatchedPopen(argv, cwd=None, env=None, text=True)
    try:
        stdout, stderr = proc.communicate(timeout=15)
        assert stdout == "" and "PROGRESS" in stderr
        assert proc.last_event_at() is not None, (
            "stderr 만 흐르는 진행이 관측되지 않는다 — 평문 리뷰어 축 전량 false-kill")
    finally:
        proc.kill()


def test_watched_popen_first_event_stays_stdout_only(relay):
    """`first_event_ready` 는 **stdout 전용 그대로**(T-0336 startup stall 의미론 보존).

    진행 관측이 stderr 를 포함한다고 첫-이벤트 판정까지 stderr 를 먹으면, opencode startup stall 이
    stderr 노이즈 한 줄로 무력화된다(워치독 사멸)."""
    argv = [sys.executable, "-c",
            "import sys, time\n"
            "sys.stderr.write('NOISE\\n'); sys.stderr.flush(); time.sleep(1.0)"]
    proc = relay._WatchedPopen(argv, cwd=None, env=None, text=True)
    try:
        deadline = __import__("time").monotonic() + 5
        while proc.last_event_at() is None and __import__("time").monotonic() < deadline:
            pass
        assert proc.last_event_at() is not None       # stderr 는 진행으로 관측되고
        assert proc.first_event_ready() is False      # 첫-이벤트(=stdout)는 아직 아니다
    finally:
        proc.kill()


def test_watched_popen_writes_stdin_and_closes(relay):
    """`input_text` 주입 — 전문을 쓰고 **닫아** EOF 를 준다(codex/claude 는 EOF 까지 읽는다)."""
    argv = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
    proc = relay._WatchedPopen(argv, cwd=None, env=None, text=True, input_text="prompt-body")
    try:
        stdout, _stderr = proc.communicate(timeout=15)
        assert stdout == "PROMPT-BODY"
    finally:
        proc.kill()


# ── ①b 실 경로 e2e (주입 0 · 실 _WatchedPopen/실 clock/실 kill · python3 자식) ────────
# hermetic 케이스는 전부 주입 popen/clock 이라 실 어댑터를 안 태운다. 아래 둘은 낮은 임계(1~2초)로
# **실제로 죽는지 / 실제로 안 죽는지** 를 실 subprocess 에서 본다(opencode·codex 바이너리 불요).

def test_real_process_idle_kill_preserves_partial_and_leaves_no_orphan(relay):
    """한 줄 뿜고 침묵하는 실 자식 → 무진행 kill · 부분 산출물 보존 · 잔존 프로세스 0."""
    argv = [sys.executable, "-c",
            "import time; print('FIRST', flush=True); time.sleep(999)"]
    captured: list = []

    def watched(a):
        proc = relay._WatchedPopen(a, cwd=None, env=None, text=True)
        captured.append(proc)
        return proc

    logs: list[str] = []
    with pytest.raises(relay.IdleTimeoutExpired) as excinfo:
        relay.run_with_first_event_watchdog(
            argv, first_event_timeout=None, overall_timeout=60.0, retries=0,
            idle_timeout=1.0, popen=watched, log=logs.append, poll_interval=0.05,
        )
    assert "FIRST" in (excinfo.value.output or ""), "실 경로에서 부분 산출물이 유실됐다"
    assert excinfo.value.idle_seconds >= 1.0
    assert len(logs) == 1 and "무진행" in logs[0]
    proc = captured[0]
    assert proc.poll() is not None, "무진행 kill 후에도 실 자식이 살아있다(잔존)"
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(proc._proc.pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="process-group 잔존 자식 실증은 POSIX 전용")
def test_parent_exit_with_live_child_is_not_success_and_child_group_is_killed(relay):
    """부모 rc=0 뒤 자식이 상속 파이프를 쥔 형상도 전체 deadline에서 실패하고 실제 정리된다."""
    parent_code = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
    )
    started = __import__("time").monotonic()
    with pytest.raises(relay.WallTimeoutExpired) as excinfo:
        relay.run_with_first_event_watchdog(
            [sys.executable, "-c", parent_code],
            first_event_timeout=None,
            overall_timeout=2.0,
            retries=0,
            idle_timeout=None,
            poll_interval=0.05,
        )
    elapsed = __import__("time").monotonic() - started
    assert elapsed < 8.0, "부모 종료 뒤 reader를 overall deadline이 아니라 고정 grace만큼 기다렸다"
    child_pid = int((excinfo.value.output or "").strip().splitlines()[0])

    deadline = __import__("time").monotonic() + 3.0
    while __import__("time").monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        __import__("time").sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="stdin writer·process-group 실증은 POSIX 전용")
def test_large_stdin_writer_keeps_run_open_and_live_child_is_killed(relay, tmp_path):
    """4MiB writer가 막힌 채 부모만 rc=0이어도 성공 반환하지 않고 wall kill로 자식을 닫는다."""
    pid_file = tmp_path / "stdin-holder.pid"
    parent_code = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'], stdin=sys.stdin, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
    )
    with pytest.raises(relay.WallTimeoutExpired):
        relay.run_with_first_event_watchdog(
            [sys.executable, "-c", parent_code, str(pid_file)],
            first_event_timeout=None,
            overall_timeout=0.25,
            retries=0,
            idle_timeout=None,
            input_text="x" * (4 * 1024 * 1024),
            poll_interval=0.01,
        )

    child_pid = int(pid_file.read_text(encoding="ascii"))
    deadline = __import__("time").monotonic() + 3.0
    while __import__("time").monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        __import__("time").sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="killpg 오류 계약은 POSIX 전용")
def test_process_group_cleanup_failure_is_loud_even_after_parent_exit(relay, monkeypatch):
    """부모 rc=0이어도 저장 pgid kill 실패를 best-effort 성공으로 삼키지 않는다."""
    class _ExitedParent:
        pid = 12345

        @staticmethod
        def poll():
            return 0

    def denied(_pgid, _signal):
        raise PermissionError(1, "not permitted")

    monkeypatch.setattr(relay.os, "killpg", denied)
    with pytest.raises(relay.ProcessCleanupError, match="pgid=777"):
        relay._kill_process_group(_ExitedParent(), process_group_id=777)


@pytest.mark.skipif(os.name != "posix", reason="분리 session 손자·killpg 경계 실증은 POSIX 전용")
def test_cleanup_failure_preserves_partial_raw_and_forbids_fallback_e2e(
        relay, pd, monkeypatch, tmp_path, capsys):
    """분리 session 손자가 drain을 막으면 raw 보존+fail-loud, 설정 폴백은 **전송 0회**다."""
    pid_file = tmp_path / "detached-grandchild.pid"
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'], start_new_session=True)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
        "print('PARTIAL-BEFORE-CLEANUP', flush=True)\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(relay, "_KILL_GRACE_SEC", 0.2)
    # pm_delegate는 자기 loader로 별도 relay module을 만들므로 같은 grace를 쓰도록 주입한다.
    monkeypatch.setattr(pd, "_load_relay", lambda: relay)

    calls = []

    def run_detached(_argv, **kwargs):
        calls.append(kwargs["harness"])
        if len(calls) > 1:
            return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        return pd._default_run_fn(
            [sys.executable, "-c", parent_code, str(pid_file)],
            stdin_text=None,
            cwd=kwargs["cwd"],
            env=kwargs["env"],
            timeout=0.2,
            harness="codex",
        )

    try:
        rc = pd._execute_and_collect(
            args=pd.argparse.Namespace(role="developer"),
            harness="codex", model="test-model", reasoning=None,
            fallback=("claude", "fallback-model", None), fallback_skip=None,
            cwd=tmp_path, prompt="test", timeout=1, fallback_timeout=1,
            output_dir=tmp_path, run_fn=run_detached,
            secret_scan_ack_digest=None, secret_scan_ack_hits=(),
        )
        assert rc == 1
        assert calls == ["codex"], "정리 실패 뒤 fallback 프롬프트가 중복 전송됐다"
        raw_path = sorted(tmp_path.glob("pm_delegate_codex_*"))[0]
        raw = raw_path.read_text(encoding="utf-8")
        assert "PARTIAL-BEFORE-CLEANUP" in raw
        assert "프로세스 정리 실패" in raw
        assert "잔존 프로세스 가능성" in raw
        err = capsys.readouterr().err
        assert "자동 폴백을 실행하지 않습니다" in err
        assert "아직 살아 있을 수 있어" in err
        assert "폴백:" not in err
    finally:
        if pid_file.exists():
            child_pid = int(pid_file.read_text(encoding="ascii"))
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


def test_real_process_progressing_on_stderr_survives_idle_threshold(relay):
    """stdout 은 종료 직전 한 번, 진행은 stderr 로만 흐르는 실 자식 → 무진행 kill 없이 완주.

    리뷰어 평문 축의 실제 형상이다(진행 로그=stderr·stdout=최종 회신). stdout 단독 관측이었다면
    임계 1초에서 죽는다."""
    argv = [sys.executable, "-c",
            "import sys, time\n"
            "for _ in range(12):\n"
            "    sys.stderr.write('tick\\n'); sys.stderr.flush(); time.sleep(0.2)\n"
            "sys.stdout.write('판정: 통과\\n')"]
    result = relay.run_with_first_event_watchdog(
        argv, first_event_timeout=None, overall_timeout=60.0, retries=0,
        idle_timeout=1.0, poll_interval=0.05,
    )
    assert result.returncode == 0
    assert "판정: 통과" in result.stdout
    assert result.stderr.count("tick") == 12


def test_real_process_unterminated_stdout_chunks_survive_idle_threshold(relay):
    """개행 없이 flush 되는 byte stream 도 실제 chunk 도착으로 진행 판정한다.

    readline 기반이면 줄이 완성되지 않아 reader가 반환하지 않고 1초 idle 에 false-kill 된다.
    총 실행은 임계의 2배이므로 단순 빠른 종료로 green 될 수 없다.
    """
    argv = [sys.executable, "-c",
            "import sys, time\n"
            "for _ in range(10):\n"
            "    sys.stdout.write('x'); sys.stdout.flush(); time.sleep(0.2)"]
    result = relay.run_with_first_event_watchdog(
        argv, first_event_timeout=None, overall_timeout=10.0, retries=0,
        idle_timeout=1.0, poll_interval=0.05,
    )
    assert result.returncode == 0
    assert result.stdout == "x" * 10


# ── ② 무진행 판정 (hermetic·주입 popen/clock) ────────────────────────────────────

def test_progressing_stream_survives_far_beyond_idle_threshold(relay):
    """진행 이벤트가 계속 흐르면 **무진행 상한의 10배 경과** 에도 안 죽는다(판정 기준 교체 실증)."""
    clock = _FakeClock()
    proc = _ScriptedProc(clock, event_times=[i * 5.0 for i in range(20)], exit_at=100.0,
                         rc=0)
    logs: list[str] = []
    result = relay.run_with_first_event_watchdog(
        ["drv"], first_event_timeout=None, overall_timeout=1000.0, retries=0,
        idle_timeout=10.0, popen=_scripted_popen([proc]), clock=clock,
        sleep=clock.advance, log=logs.append, poll_interval=1.0,
    )
    assert result.returncode == 0
    assert proc.kill_count == 0, "진행 중인 프로세스를 죽였다(false-kill 재발)"
    assert logs == []


def test_idle_stream_is_killed_with_loud_and_partial_output(relay):
    """무진행이면 죽는다 — kill + loud 1줄 + **kill 시점까지의 출력 보존**(전량 폐기 폐쇄)."""
    clock = _FakeClock()
    proc = _ScriptedProc(clock, event_times=[0.0], exit_at=None, chunk="PARTIAL\n")
    logs: list[str] = []
    with pytest.raises(relay.IdleTimeoutExpired) as excinfo:
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=1000.0, retries=0,
            idle_timeout=10.0, popen=_scripted_popen([proc]), clock=clock,
            sleep=clock.advance, log=logs.append, poll_interval=1.0,
        )
    exc = excinfo.value
    assert proc.kill_count == 1
    assert exc.idle_seconds >= 10.0
    assert exc.output == "PARTIAL\n", "kill 시점 부분 산출물이 버려졌다(17차 138바이트 재발)"
    assert len(logs) == 1 and "무진행" in logs[0] and "idle watchdog" in logs[0]


def test_idle_timeout_expired_is_a_timeout_expired(relay):
    """`IdleTimeoutExpired` 는 `subprocess.TimeoutExpired` 하위 — 기존 분류/폴백 경로 회귀 0."""
    exc = relay.IdleTimeoutExpired(["x"], 10.0, idle_seconds=12.0)
    assert isinstance(exc, subprocess.TimeoutExpired)
    assert exc.idle_seconds == 12.0
    assert getattr(subprocess.TimeoutExpired(["x"], 10.0), "idle_seconds", None) is None


def test_idle_none_keeps_legacy_single_drain(relay):
    """`idle_timeout=None` = 현행 동작 불변 — 폴 루프 없이 단일 블로킹 드레인."""
    clock = _FakeClock()

    class _LegacyProc(_ScriptedProc):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            return "OUT", "ERR"

    proc = _LegacyProc(clock, event_times=[0.0], exit_at=0.0, rc=0)
    result = relay.run_with_first_event_watchdog(
        ["drv"], first_event_timeout=5.0, overall_timeout=600.0, retries=0,
        idle_timeout=None, popen=_scripted_popen([proc]), clock=clock,
        sleep=clock.advance, log=[].append, poll_interval=1.0,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "OUT", "ERR")
    assert proc.communicate_calls == 1 and proc.kill_count == 0


def test_idle_none_wall_timeout_is_wrapped_with_actual_diagnostics(relay):
    """단일 blocking communicate 경로도 raw TimeoutExpired를 그대로 흘리지 않고 진단을 싣는다."""
    clock = _FakeClock()
    proc = _ScriptedProc(clock, event_times=[10.0], exit_at=None, chunk="PROGRESS\n")
    with pytest.raises(relay.WallTimeoutExpired) as excinfo:
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=50.0, retries=0,
            idle_timeout=None, popen=_scripted_popen([proc]), clock=clock,
            sleep=clock.advance, log=[].append, poll_interval=1.0,
        )
    exc = excinfo.value
    assert exc.timeout_axis == relay.TIMEOUT_AXIS_WALL
    assert exc.threshold_seconds == 50.0
    assert exc.silence_seconds == 40.0
    assert exc.output == "PROGRESS\n"
    assert proc.kill_count == 1


def test_signalless_adapter_is_not_idle_judged(relay):
    """진행을 노출 못 하는 어댑터는 **무진행 판정 대상이 아니다** — 벽시계만 남는다.

    모르는 드라이버를 무진행으로 죽이지 않는다(false-kill 방향 금지). 같은 스크립트라도 관측
    seam 이 없으면 idle 로 죽지 않고 완주한다."""
    clock = _FakeClock()
    blind = _BlindProc(clock, exit_at=100.0)
    assert not hasattr(blind, "last_event_at")
    result = relay.run_with_first_event_watchdog(
        ["drv"], first_event_timeout=None, overall_timeout=1000.0, retries=0,
        idle_timeout=10.0, popen=_scripted_popen([blind]), clock=clock,
        sleep=clock.advance, log=[].append, poll_interval=1.0,
    )
    assert result.returncode == 0 and blind.kill_count == 0


def test_signalless_adapter_still_bounded_by_wall_clock(relay):
    """신호 없는 축도 **유한** 하다 — 벽시계 백스톱이 그대로 닫는다."""
    clock = _FakeClock()
    blind = _BlindProc(clock, exit_at=None)
    with pytest.raises(subprocess.TimeoutExpired):
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=50.0, retries=0,
            idle_timeout=10.0, popen=_scripted_popen([blind]), clock=clock,
            sleep=clock.advance, log=[].append, poll_interval=1.0,
        )
    assert blind.kill_count == 1


def test_wall_clock_backstop_closes_even_while_progressing(relay):
    """진행 중이어도 벽시계 백스톱은 유한하게 닫는다(무제한 금지) + 부분 산출물 보존.

    감지기 자체가 고장나면(진행 신호가 계속 오는데 영원히 안 끝남) 무제한은 silent hang 이 된다."""
    clock = _FakeClock()
    proc = _ScriptedProc(clock, event_times=[i * 5.0 for i in range(100)], exit_at=None,
                         chunk="X\n")
    logs: list[str] = []
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=50.0, retries=0,
            idle_timeout=1000.0, popen=_scripted_popen([proc]), clock=clock,
            sleep=clock.advance, log=logs.append, poll_interval=1.0,
        )
    assert not isinstance(excinfo.value, relay.IdleTimeoutExpired)  # 무진행이 아니라 벽시계
    assert isinstance(excinfo.value, relay.WallTimeoutExpired)
    assert excinfo.value.timeout_axis == relay.TIMEOUT_AXIS_WALL
    assert excinfo.value.threshold_seconds == 50.0
    assert excinfo.value.silence_seconds == 0.0
    assert excinfo.value.output, "벽시계 kill 도 부분 산출물을 보존해야 한다"
    assert proc.kill_count == 1
    assert len(logs) == 1 and "벽시계 백스톱" in logs[0]


def test_silence_audit_clamps_reader_clock_race_to_zero(relay):
    """`now` 뒤 reader가 최신 시각을 쓰는 경합은 음수 감사값이 아니라 0초다."""
    class _ReaderWonRace:
        @staticmethod
        def last_event_at():
            return 10.25

    assert relay._silence_seconds(_ReaderWonRace(), now=10.0, start=1.0) == 0.0


def test_first_event_stall_behaviour_unchanged_with_idle(relay):
    """첫-이벤트 stall(유한 재시도 → StallWatchdogError) 기존 동작은 idle 도입 후에도 불변."""
    clock = _FakeClock()
    procs = [_ScriptedProc(clock, event_times=[], exit_at=None),
             _ScriptedProc(clock, event_times=[], exit_at=None)]
    logs: list[str] = []
    with pytest.raises(relay.StallWatchdogError) as excinfo:
        relay.run_with_first_event_watchdog(
            ["opencode", "run"], first_event_timeout=5.0, overall_timeout=600.0, retries=1,
            idle_timeout=900.0, popen=_scripted_popen(procs), clock=clock,
            sleep=clock.advance, log=logs.append, poll_interval=1.0,
        )
    assert [p.kill_count for p in procs] == [1, 1]
    assert len(logs) == 2 and all("stall watchdog" in m for m in logs)
    assert excinfo.value.timeout_axis == relay.TIMEOUT_AXIS_FIRST_EVENT
    assert excinfo.value.threshold_seconds == 5.0


def test_first_event_stall_preserves_output_received_before_kill(relay):
    """startup stall도 stdout/stderr를 빈 값으로 고정하지 않고 예외에 싣는다."""
    clock = _FakeClock()

    class _StderrOnlyStartup(_ScriptedProc):
        def first_event_ready(self) -> bool:
            return self.poll() is not None  # stderr 진행은 startup stdout 이벤트가 아니다.

        def partial_output(self):
            delivered = len(self._delivered())
            return "", "BOOT-DIAGNOSTIC\n" * delivered

    proc = _StderrOnlyStartup(clock, event_times=[0.0], exit_at=None)
    with pytest.raises(relay.StallWatchdogError) as excinfo:
        relay.run_with_first_event_watchdog(
            ["opencode", "run"], first_event_timeout=2.0, overall_timeout=30.0,
            retries=0, idle_timeout=10.0, popen=_scripted_popen([proc]),
            clock=clock, sleep=clock.advance, log=[].append, poll_interval=1.0,
        )
    assert excinfo.value.output == ""
    assert "BOOT-DIAGNOSTIC" in excinfo.value.stderr


def test_startup_wait_wall_axis_stops_after_first_attempt(relay):
    """startup 대기 중 wall이 먼저 발화하면 첫 시도에서 종료하고 외부 전송을 반복하지 않는다."""
    clock = _FakeClock()
    procs = [_ScriptedProc(clock, event_times=[], exit_at=None) for _ in range(3)]
    launched = []
    logs = []

    def popen(_argv):
        proc = procs[len(launched)]
        launched.append(proc)
        return proc

    with pytest.raises(relay.WallTimeoutExpired) as excinfo:
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=90.0, overall_timeout=30.0, retries=2,
            idle_timeout=900.0, popen=popen, clock=clock,
            sleep=clock.advance, log=logs.append, poll_interval=1.0,
        )
    exc = excinfo.value
    assert len(launched) == 1
    assert procs[0].kill_count == 1
    assert [proc.kill_count for proc in procs[1:]] == [0, 0]
    assert exc.timeout_axis == relay.TIMEOUT_AXIS_WALL
    assert exc.threshold_seconds == 30.0
    assert len(logs) == 1 and "자동 재시도 안 함" in logs[0]


def test_coarse_poll_uses_earlier_first_event_deadline_and_retries(relay):
    """한 poll이 first=5s·wall=6s를 함께 넘겨도 실제로 먼저 지난 startup 축을 고른다."""
    clock = _FakeClock()
    launched = []

    def popen(_argv):
        proc = (
            _ScriptedProc(clock, event_times=[], exit_at=None)
            if not launched
            else _ScriptedProc(clock, event_times=[0.0], exit_at=0.0)
        )
        launched.append(proc)
        return proc

    result = relay.run_with_first_event_watchdog(
        ["drv"], first_event_timeout=5.0, overall_timeout=6.0, retries=1,
        idle_timeout=None, popen=popen, clock=clock,
        sleep=clock.advance, log=[].append, poll_interval=10.0,
    )

    assert result.returncode == 0
    assert len(launched) == 2, "더 이른 startup 축이 wall로 오분류되어 재시도가 사라짐"
    assert [proc.kill_count for proc in launched] == [1, 0]


def test_keyboard_interrupt_during_post_spawn_initialization_kills_and_drains(
        relay, monkeypatch):
    """실 자식 생성 직후 KeyboardInterrupt가 나도 생성자 트랜잭션이 kill·drain 후 재전파한다."""
    real_popen = relay.subprocess.Popen
    spawned = []

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    interrupt = KeyboardInterrupt("post-spawn interrupt")

    def interrupt_event():
        raise interrupt

    monkeypatch.setattr(relay.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(relay.threading, "Event", interrupt_event)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        relay._WatchedPopen(
            [sys.executable, "-c", "import time; time.sleep(999)"],
            cwd=None, env=None, text=True,
        )

    assert excinfo.value is interrupt
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert spawned[0].stdout.closed and spawned[0].stderr.closed


def test_thread_start_failure_during_post_spawn_initialization_kills_and_drains(
        relay, monkeypatch):
    """reader Thread.start 실패도 이미 생성된 실 자식을 남기지 않고 원래 오류를 재전파한다."""
    real_popen = relay.subprocess.Popen
    spawned = []

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    start_error = RuntimeError("thread start failed")

    def fail_start(_thread):
        raise start_error

    monkeypatch.setattr(relay.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(relay.threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="thread start failed") as excinfo:
        relay._WatchedPopen(
            [sys.executable, "-c", "import time; time.sleep(999)"],
            cwd=None, env=None, text=True,
        )

    assert excinfo.value is start_error
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert spawned[0].stdout.closed and spawned[0].stderr.closed


def test_keyboard_interrupt_kills_and_drains_real_process_group(relay):
    """Ctrl-C가 startup 대기 중 들어와도 새 세션 자식을 kill+drain한 뒤 그대로 재전파한다."""
    argv = [sys.executable, "-c", "import time; time.sleep(999)"]
    captured = []

    def watched(command):
        proc = relay._WatchedPopen(command, cwd=None, env=None, text=True)
        captured.append(proc)
        return proc

    def interrupt(_seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        relay.run_with_first_event_watchdog(
            argv, first_event_timeout=90.0, overall_timeout=600.0, retries=2,
            popen=watched, sleep=interrupt,
        )

    assert len(captured) == 1
    proc = captured[0]
    assert proc.poll() is not None
    assert proc.drain_complete(), "Ctrl-C 정리 뒤 stdout/stderr reader가 남음"
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(proc._proc.pid, 0)


def test_keyboard_interrupt_during_blocking_drain_is_cleaned_and_repropagated(relay):
    """startup 창 없는 소비처의 blocking drain Ctrl-C도 그룹 정리 후 같은 예외를 통과시킨다."""
    clock = _FakeClock()

    class _InterruptingDrain(_ScriptedProc):
        communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise KeyboardInterrupt
            return super().communicate(timeout=timeout)

    proc = _InterruptingDrain(clock, event_times=[], exit_at=None)
    with pytest.raises(KeyboardInterrupt):
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=600.0, retries=0,
            popen=_scripted_popen([proc]), clock=clock,
            sleep=clock.advance, log=[].append,
        )

    assert proc.kill_count == 1
    assert proc.communicate_calls == 2, "Ctrl-C 뒤 파이프 drain을 수행하지 않음"


def test_first_event_timeout_none_skips_startup_window(relay):
    """`first_event_timeout=None` = startup 창 미적용 — 첫 stdout 이 늦어도 stall 로 안 죽는다.

    리뷰어 평문 축(첫 stdout = 종료 직전)이 startup 창에 걸려 죽는 새 false-kill 을 만들지 않는다."""
    clock = _FakeClock()
    late = _ScriptedProc(clock, event_times=[500.0], exit_at=500.0, rc=0)
    result = relay.run_with_first_event_watchdog(
        ["codex", "exec"], first_event_timeout=None, overall_timeout=1000.0, retries=0,
        idle_timeout=None, popen=_scripted_popen([late]), clock=clock,
        sleep=clock.advance, log=[].append, poll_interval=1.0,
    )
    assert result.returncode == 0 and late.kill_count == 0


def test_completed_process_carries_silence_observation(relay):
    """완주 결과에 관측 침묵 초가 실린다 — 감사 헤더(⑥)의 입력."""
    clock = _FakeClock()
    proc = _ScriptedProc(clock, event_times=[0.0, 10.0], exit_at=30.0, rc=0)
    result = relay.run_with_first_event_watchdog(
        ["drv"], first_event_timeout=None, overall_timeout=1000.0, retries=0,
        idle_timeout=100.0, popen=_scripted_popen([proc]), clock=clock,
        sleep=clock.advance, log=[].append, poll_interval=1.0,
    )
    silence = getattr(result, relay.SILENCE_SEC_ATTR)
    assert silence is not None and silence >= 20.0   # 마지막 이벤트 t=10 · 종료 t≥30


def test_drain_oserror_kills_process_group(relay):
    """드레인 중 OSError(전송 후 I/O 오류)도 자식 잔존 없이 정리 후 전파(정책은 호출부)."""
    clock = _FakeClock()

    class _BrokenProc(_ScriptedProc):
        def communicate(self, timeout=None):
            raise OSError(32, "Broken pipe")

    proc = _BrokenProc(clock, event_times=[0.0], exit_at=0.0)
    with pytest.raises(OSError):
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=100.0, retries=0,
            idle_timeout=None, popen=_scripted_popen([proc]), clock=clock,
            sleep=clock.advance, log=[].append, poll_interval=1.0,
        )
    assert proc.kill_count == 1


# ── ③ 값 축: 하네스별 선언 + local.conf override (값만 갈리고 코드는 하나) ──────────

def test_no_dead_global_idle_timeout_knob(relay, monkeypatch):
    """프로필보다 우선하지 못하던 죽은 PM_IDLE_TIMEOUT 노브/주장/API 를 함께 제거한다."""
    monkeypatch.setenv("PM_IDLE_TIMEOUT", "42")
    assert not hasattr(relay, "IDLE_TIMEOUT_ENV")
    assert not hasattr(relay, "idle_timeout_default")
    assert relay.idle_timeout_for_signal(relay.PROGRESS_SIGNAL_EVENT_STREAM) == \
        relay.DEFAULT_IDLE_TIMEOUT_SEC


def test_cloud_axis_values_are_grounded_in_measurement(relay):
    """클라우드 축(codex·claude) 값은 실측 위에 선다 — 관측 최대 침묵/완주를 넘는다.

    근거 수치(주석 박제): 도구 실행 침묵 p99 124.9s·**max 254.6s**(N=860/89파일), 이벤트 간
    비둘기집 하한 max 17.7s, 총 완주 p99 1036.6s·**max 1429.1s**(rc=0 153건)."""
    assert relay.CLOUD_IDLE_TIMEOUT_SEC > 254.6      # 관측 최대 침묵 위
    assert relay.CLOUD_WALL_TIMEOUT_SEC > 1429.1     # 관측 최대 완주 위
    source = (TOOLS / "pm_relay.py").read_text(encoding="utf-8")
    for figure in ("254.6s", "1429.1s"):
        assert figure in source, f"시간 예산의 실측 근거 수치 {figure} 가 주석에서 사라졌다"


def test_local_gpu_axis_survives_three_hour_delegation(relay):
    """로컬 GPU 축(opencode)은 **3시간 완주 + 긴 침묵**을 견딘다 — 사용자 실측 증언이 근거.

    증언: 회사 환경(로컬 GPU 부족)의 opencode 위임이 **3시간까지** 걸리고 진행 양상은 *"길게
    멈췄다 가끔 움직임"*. 클라우드 측정치로 값을 잡으면 그 정상 작업을 죽인다(1차 구현의 전제
    붕괴). 벽시계는 3시간 **위**, 무진행 상한은 긴 침묵을 견디는 값이어야 한다."""
    three_hours = 3 * 3600.0
    profile = relay.HARNESS_PROFILES["opencode"]
    assert profile.wall_timeout > three_hours, "3시간 완주가 벽시계 백스톱에 잘린다"
    assert profile.idle_timeout > relay.CLOUD_IDLE_TIMEOUT_SEC, (
        "로컬 GPU 축의 무진행 상한이 클라우드 축과 같거나 낮다 — 긴 침묵을 못 견딘다")
    source = (TOOLS / "pm_relay.py").read_text(encoding="utf-8")
    assert "길게 멈췄다 가끔 움직임" in source, "로컬 GPU 축 값의 근거(사용자 증언)가 주석에 없다"


def test_harness_values_actually_differ_by_axis(relay):
    """축이 실제로 갈린다 — 클라우드와 로컬 GPU 가 같은 값이면 하네스별 선언이 무의미하다."""
    cloud = relay.HARNESS_PROFILES["codex"]
    local_gpu = relay.HARNESS_PROFILES["opencode"]
    assert relay.HARNESS_PROFILES["claude"] == cloud._replace()   # 같은 축 = 같은 값
    assert local_gpu.idle_timeout > cloud.idle_timeout
    assert local_gpu.wall_timeout > cloud.wall_timeout


def test_unset_adopter_is_safe_by_default(relay):
    """**설정 안 해도 안 죽는다** — 미설정(빈 conf) 채택자도 3시간 로컬 위임을 완주한다.

    미설정 채택자의 false-kill 은 이 판정 전환의 실패 조건이다(설정을 알아야만 안전하면 안 된다)."""
    profile = relay.resolve_harness_profile("opencode", {})
    assert profile.wall_timeout > 3 * 3600.0
    assert profile.idle_timeout >= 3600.0
    # 미지 하네스/리뷰어도 관대한 쪽으로 떨어진다(모르는 축을 죽이지 않는다).
    assert relay.resolve_harness_profile("brand-new", {}).wall_timeout >= profile.wall_timeout
    assert relay.UNKNOWN_HARNESS_PROFILE.progress_signal == relay.PROGRESS_SIGNAL_NONE
    assert relay.REVIEWER_FALLBACK_PROFILE.progress_signal == relay.PROGRESS_SIGNAL_NONE


def test_harness_conf_override_resolution_order(relay, capsys):
    """해소 순서: 선언 기본 → 표면-flat legacy 키 → 하네스별 키(뒤가 이긴다)."""
    declared = relay.HARNESS_PROFILES["codex"]
    assert relay.resolve_harness_profile("codex", {}) == declared
    legacy = relay.resolve_harness_profile(
        "codex", {"delegate_timeout": "1234", "delegate_idle_timeout": "77"},
        legacy_idle_key="delegate_idle_timeout", legacy_wall_key="delegate_timeout")
    assert (legacy.wall_timeout, legacy.idle_timeout) == (1234.0, 77.0)
    specific = relay.resolve_harness_profile(
        "codex", {"delegate_timeout": "1234", "harness.codex.wall_timeout": "5555",
                  "harness.codex.idle_timeout": "999"},
        legacy_idle_key="delegate_idle_timeout", legacy_wall_key="delegate_timeout")
    assert (specific.wall_timeout, specific.idle_timeout) == (5555.0, 999.0)
    # 다른 하네스 키는 이 하네스에 영향 없음(축 격리).
    isolated = relay.resolve_harness_profile("claude", {"harness.opencode.idle_timeout": "1"})
    assert isolated.idle_timeout == declared.idle_timeout
    # 깨진 값은 경고 후 선언 기본으로 fail-soft(설정 파일 문제로 실행이 죽지 않는다).
    broken = relay.resolve_harness_profile(
        "codex", {"harness.codex.idle_timeout": "abc", "harness.codex.wall_timeout": "-5"})
    assert broken == declared
    err = capsys.readouterr().err
    assert "harness.codex.idle_timeout" in err and "harness.codex.wall_timeout" in err


def test_idle_timeout_for_signal_declares_axes(relay):
    """선언 테이블 — 신호 있는 축은 상한 적용, 신호 없는/미지의 축은 None(벽시계 유지)."""
    assert relay.idle_timeout_for_signal(relay.PROGRESS_SIGNAL_EVENT_STREAM) == \
        relay.DEFAULT_IDLE_TIMEOUT_SEC
    assert relay.idle_timeout_for_signal(relay.PROGRESS_SIGNAL_PLAINTEXT, 33.0) == 33.0
    assert relay.idle_timeout_for_signal(relay.PROGRESS_SIGNAL_NONE) is None
    assert relay.idle_timeout_for_signal("made-up-axis") is None  # 보수 기본


def test_profile_table_covers_every_declared_harness(pd, relay):
    """프로필 테이블이 위임 하네스 전부를 덮고, 관측 선언이 엔진 값과 정합."""
    assert set(relay.HARNESS_PROFILES) == set(pd.HARNESS_CHOICES)
    for harness, profile in relay.HARNESS_PROFILES.items():
        assert profile.progress_signal == relay.PROGRESS_SIGNAL_EVENT_STREAM, harness
        assert profile.idle_timeout < profile.wall_timeout, harness  # 주 판정이 먼저 울린다
    # startup stall 워치독은 실측된 축(opencode)만 — 신호 축 전부에 켜면 새 false-kill 이 생긴다.
    assert relay.HARNESS_PROFILES["opencode"].startup_watchdog is True
    assert relay.HARNESS_PROFILES["codex"].startup_watchdog is False
    assert relay.HARNESS_PROFILES["claude"].startup_watchdog is False
    assert pd.harness_profile("nope", {}).progress_signal == relay.PROGRESS_SIGNAL_NONE


def test_unknown_defaults_are_derived_from_most_permissive_profile(relay):
    """호환 alias 가 특정 축을 가리키지 않고 선언 테이블 최댓값에서 파생된다.

    더 관대한 미래 축을 실제 소스 테이블에 주입해 모듈을 다시 실행한다. 오늘 값이 우연히
    opencode==max 라는 비교만으로 green 되는 가짜 게이트를 피한다.
    """
    assert relay.DEFAULT_IDLE_TIMEOUT_SEC == max(
        profile.idle_timeout for profile in relay.HARNESS_PROFILES.values())
    assert relay.DEFAULT_WALL_TIMEOUT_SEC == max(
        profile.wall_timeout for profile in relay.HARNESS_PROFILES.values())
    source = (TOOLS / "pm_relay.py").read_text(encoding="utf-8")
    marker = "\n}\n\n# 프로필을 모르는 축의 기본"
    assert source.count(marker) == 1
    source = source.replace(
        marker,
        '\n    "future": HarnessProfile(PROGRESS_SIGNAL_EVENT_STREAM, False, 6000.0, 16000.0),'
        + marker,
    )
    namespace = {"__file__": str(TOOLS / "pm_relay.py"), "__name__": "mutated_pm_relay"}
    exec(compile(source, namespace["__file__"], "exec"), namespace)
    assert namespace["DEFAULT_IDLE_TIMEOUT_SEC"] == 6000.0
    assert namespace["DEFAULT_WALL_TIMEOUT_SEC"] == 16000.0


def test_all_orchestration_functions_never_branch_on_harness_name_without_reason():
    """**값은 갈려도 판정 코드는 하나** — 네 모듈의 모든 함수가 자동으로 검사 대상이다."""
    empty_reasons = [key for key, reason in _HARNESS_LITERAL_EXEMPTIONS.items()
                     if not reason.strip()]
    assert not empty_reasons, f"하네스 리터럴 면제 사유가 비어 있음: {empty_reasons}"
    unexpected = _unexpected_harness_literal_hits()
    assert not unexpected, (
        f"비면제 함수가 하네스 이름으로 갈린다: {unexpected} — 선언 테이블로 옮기거나 "
        "표현/전달 어댑터임을 입증하는 면제 사유 필요")

    # stale 면제는 검토 없이 범위만 넓힌 흔적이므로 제거한다.
    live = {
        (module_name, qualname)
        for module_name in ("pm_delegate", "external_review", "pm_import", "pm_relay")
        for qualname in _module_harness_literal_hits(
            module_name, (TOOLS / f"{module_name}.py").read_text(encoding="utf-8"))
    }
    stale = set(_HARNESS_LITERAL_EXEMPTIONS) - live
    assert not stale, f"하네스 리터럴 stale 면제(실소유 없음): {sorted(stale)}"


@pytest.mark.parametrize(
    ("module_name", "function_name", "needle", "replacement"),
    [
        # 내부 리뷰어가 실제로 통과시킨 우회: 공용 워치독에 들어갈 idle_timeout을 호출자에서 무력화.
        (
            "external_review",
            "_run_reviewer_ex",
            "idle_timeout=_reviewer_idle_timeout(reviewer_cmd, idle_timeout),",
            'idle_timeout=(None if reviewer_name(reviewer_cmd) == "opencode"\n'
            "                          else _reviewer_idle_timeout(reviewer_cmd, idle_timeout)),",
        ),
        # 추가 적대 지점: 수합 호출자가 내부 호출로 넘기기 직전에 claude 축만 무진행 판정을 제거.
        (
            "external_review",
            "run_review",
            "ok, output, started = _run_reviewer_ex(\n"
            "                prompt, reviewer_cmd, timeout, run_fn, idle_timeout, metrics,\n"
            "                cwd=cwd, env=env, argv=argv, stdin_text=stdin_text,\n"
            "                on_spawn_attempt=_spawn_attempt,\n"
            "            )",
            'ok, output, started = _run_reviewer_ex(\n'
            '                prompt, reviewer_cmd, timeout, run_fn,\n'
            '                None if reviewer_name(reviewer_cmd) == "claude" else idle_timeout,\n'
            '                metrics,\n'
            '                cwd=cwd, env=env, argv=argv, stdin_text=stdin_text,\n'
            '                on_spawn_attempt=_spawn_attempt,\n'
            '            )',
        ),
        # S(b): 값이 아니라 signal on/off를 쥔 판정 함수 선두에서 특정 CLI만 NONE으로 강등.
        (
            "external_review",
            "_reviewer_progress_signal",
            "    if not argv:\n"
            "        return relay.PROGRESS_SIGNAL_NONE\n",
            "    if argv and argv[0] == \"opencode\":\n"
            "        return relay.PROGRESS_SIGNAL_NONE\n"
            "    if not argv:\n"
            "        return relay.PROGRESS_SIGNAL_NONE\n",
        ),
        # 내부 리뷰 B: 과거 함수 통째 면제였던 timeout 소유 함수 안의 하네스별 강제값.
        (
            "pm_delegate",
            "_execute_attempt",
            "    started = time.monotonic()\n",
            '    if harness == "claude":\n'
            "        timeout = 1\n"
            "    started = time.monotonic()\n",
        ),
        # 내부 리뷰 C: dry-run 표시 사유로 함수 통째 면제됐던 main 안의 timeout 강제값.
        (
            "pm_delegate",
            "main",
            "    timeout = _resolve_timeout(args, conf, harness)\n",
            '    if harness == "opencode":\n'
            "        timeout = 60\n"
            "    else:\n"
            "        timeout = _resolve_timeout(args, conf, harness)\n",
        ),
        # 세 번째 표면: fill runner가 공용 profile을 받은 뒤 특정 하네스만 idle 축을 끄는 우회.
        (
            "pm_import",
            "_real_harness_runner",
            "        retries = engine.stall_retries_default() if profile.startup_watchdog else 0\n",
            '        if harness == "claude":\n'
            "            idle_timeout = None\n"
            "        retries = engine.stall_retries_default() if profile.startup_watchdog else 0\n",
        ),
    ],
)
def test_structural_guard_rejects_new_timeout_bypass_callers(
        module_name, function_name, needle, replacement):
    """열거 튜플에 없던 호출자에 새 우회를 심어도 구조 스캔이 자동 red 낸다."""
    sources = {
        name: (TOOLS / f"{name}.py").read_text(encoding="utf-8")
        for name in ("pm_delegate", "external_review", "pm_import", "pm_relay")
    }
    assert sources[module_name].count(needle) == 1
    sources[module_name] = sources[module_name].replace(needle, replacement)
    unexpected = _unexpected_harness_literal_hits(sources)
    assert (module_name, function_name) in unexpected


# ── ④ 위임 표면 배선 ────────────────────────────────────────────────────────────

class _RecordingRelay:
    """공용 seam 호출을 기록하는 대역 — **선언 테이블/해소기는 실 pm_relay 것을 그대로 쓴다**.

    대역이 값 규칙(프로필·해소 순서)을 자체 구현하면 "하네스별 값이 실제로 갈리는가"를 보는
    테스트가 거짓이 된다. 그래서 미정의 속성은 전부 실 모듈로 위임하고 워치독 호출만 가로챈다."""

    def __init__(self, *, completed=None, exc=None, silence=None):
        self.calls: list[dict] = []
        self._completed = completed
        self._exc = exc
        self._silence = silence
        self._relay = _load("pm_relay", TOOLS / "pm_relay.py")

    def __getattr__(self, name):
        return getattr(self._relay, name)

    def first_event_timeout_default(self):
        return 90.0

    def stall_retries_default(self):
        return 2

    def run_with_first_event_watchdog(self, argv, **kw):
        self.calls.append({"argv": argv, **kw})
        if self._exc is not None:
            raise self._exc
        completed = self._completed or subprocess.CompletedProcess(argv, 0, "", "")
        if self._silence is not None:
            setattr(completed, "silence_sec", self._silence)
        return completed


def _wire_relay(pd, monkeypatch, fake, conf=None):
    """위임 표면을 대역 relay + 격리 conf 로 배선(실 local.conf 비의존 hermetic)."""
    monkeypatch.setattr(pd, "_load_relay", lambda: fake)
    monkeypatch.setattr(pd, "local_config", lambda: dict(conf or {}))
    return fake


@pytest.mark.parametrize("harness", FILL_CAPABLE_HARNESSES)
def test_fill_routes_every_driver_through_shared_watchdog(
        pm_import, relay, monkeypatch, harness):
    """세 번째 표면 fill도 3 드라이버 전부 같은 profile+watchdog 판정을 탄다."""
    fake = _RecordingRelay()
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)
    argv = pm_import._build_runner_argv(harness, "prompt")

    ok, _output = pm_import._real_harness_runner(argv, "prompt")

    assert ok is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    profile = relay.HARNESS_PROFILES[harness]
    assert call["overall_timeout"] == profile.wall_timeout
    emits_progress = next(
        driver[1] for driver in pm_import.FILL_DRIVER_BY_CMD.values()
        if driver[0] == harness
    )
    expected_idle = profile.idle_timeout if emits_progress else None
    assert call["idle_timeout"] == expected_idle
    assert call["first_event_timeout"] == (
        90.0 if profile.startup_watchdog else None)
    assert call["retries"] == (2 if profile.startup_watchdog else 0)


def test_fill_reads_target_repo_profile_overrides(pm_import, monkeypatch, tmp_path):
    """채택 대상 local.conf의 하네스별 override가 fill 공용 판정까지 도달한다."""
    local = tmp_path / ".project_manager"
    local.mkdir()
    (local / "local.conf").write_text(
        "harness.codex.idle_timeout=41\n"
        "harness.codex.wall_timeout=401\n",
        encoding="utf-8",
    )
    fake = _RecordingRelay()
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    pm_import._real_harness_runner(["codex", "exec", "--json", "prompt"], "prompt", cwd=tmp_path)

    assert fake.calls[0]["idle_timeout"] == 41.0
    assert fake.calls[0]["overall_timeout"] == 401.0


def test_fill_claude_plain_blob_disables_idle_from_actual_argv(
        pm_import, relay, monkeypatch):
    """평문 `claude -p`는 event-stream 프로필을 소비하지 않아 정상 실행을 idle kill하지 않는다."""
    fake = _RecordingRelay()
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)
    argv = pm_import._build_runner_argv("claude", "prompt")

    pm_import._real_harness_runner(argv, "prompt")

    assert tuple(argv[:2]) == pm_import.CLAUDE_FILL_CMD
    assert fake.calls[0]["idle_timeout"] is None
    assert relay.HARNESS_PROFILES["claude"].progress_signal == relay.PROGRESS_SIGNAL_EVENT_STREAM


@pytest.mark.parametrize(
    ("argv", "expected_harness"),
    [
        (["codex", "exec", "prompt"], ""),
        (["codex", "exec", "--json", "prompt"], "codex"),
        (["opencode", "run", "prompt"], ""),
        (["opencode", "run", "prompt", "--format", "json"], "opencode"),
        (["opencode", "run", "prompt", "--format=json"], "opencode"),
    ],
)
def test_fill_driver_requires_actual_incremental_output_flag(
        pm_import, argv, expected_harness):
    """접두사가 같아도 JSON 출력 플래그가 없으면 증분 드라이버로 선언하지 않는다."""
    harness, emits_progress, _input_text = pm_import._fill_driver(argv)
    assert harness == expected_harness
    assert emits_progress is bool(expected_harness)


def test_fill_prints_effective_limits_before_spawn(pm_import, monkeypatch, capsys):
    """채택자가 호출 전에 실효 idle/wall과 Ctrl-C 중단 방법을 본다."""
    fake = _RecordingRelay()
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    pm_import._real_harness_runner(["claude", "-p", "prompt"], "prompt")

    err = capsys.readouterr().err
    assert "idle=비활성(증분 신호 없음)" in err
    assert "wall=3600초" in err
    assert "Ctrl-C" in err


def test_fill_timeout_preserves_partial_stdout_and_stderr(
        pm_import, relay, monkeypatch):
    """idle kill 시점의 양쪽 스트림을 fill 실패 결과에 싣는다(전량 폐기 회귀 방지)."""
    exc = relay.IdleTimeoutExpired(
        ["codex"], 900, idle_seconds=901,
        output="PARTIAL-OUT", stderr="PARTIAL-ERR",
    )
    fake = _RecordingRelay(exc=exc)
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    ok, output = pm_import._real_harness_runner(["codex", "exec", "prompt"], "prompt")

    assert ok is False
    assert "무진행 임계 900초" in output
    assert "PARTIAL-OUT" in output and "PARTIAL-ERR" in output
    assert "stdout" in output and "stderr" in output
    assert "수동 재시도" in output


def test_fill_startup_stall_preserves_partial_output(pm_import, relay, monkeypatch):
    """opencode startup 재시도 소진도 누적 부분 산출물을 버리지 않는다."""
    fake = _RecordingRelay()
    exc = fake.StallWatchdogError(
        "startup stall", timeout_axis=relay.TIMEOUT_AXIS_FIRST_EVENT,
        threshold_seconds=90, silence_seconds=90,
        output="START-OUT", stderr="START-ERR",
    )
    fake._exc = exc
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    ok, output = pm_import._real_harness_runner(
        ["opencode", "run", "prompt"], "prompt")

    assert ok is False
    assert "재시도 소진" in output
    assert "START-OUT" in output and "START-ERR" in output


def test_fill_stall_error_reports_wall_axis(pm_import, relay, monkeypatch):
    """startup 창보다 wall deadline이 먼저 발화하면 첫-이벤트 stall로 오진하지 않는다."""
    fake = _RecordingRelay()
    exc = fake.StallWatchdogError(
        "wall first", timeout_axis=relay.TIMEOUT_AXIS_WALL,
        threshold_seconds=30, silence_seconds=30,
        output="WALL-OUT", stderr="WALL-ERR",
    )
    fake._exc = exc
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    ok, output = pm_import._real_harness_runner(
        ["opencode", "run", "prompt", "--format", "json"], "prompt")

    assert ok is False
    assert "벽시계 백스톱 30초" in output
    assert "첫-이벤트 stall" not in output
    assert "WALL-OUT" in output and "WALL-ERR" in output


def test_fill_watchdog_load_failure_is_soft(pm_import, monkeypatch):
    """relay 로드/프로필 해소 실패도 run_fill 계약처럼 예외를 전파하지 않는다."""
    monkeypatch.setattr(
        pm_import, "_load_watchdog",
        lambda: (_ for _ in ()).throw(RuntimeError("relay unavailable")),
    )
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    ok, output = pm_import._real_harness_runner(["claude", "-p", "prompt"], "prompt")

    assert ok is False
    assert "relay unavailable" in output


def test_fill_watchdog_rev_skew_is_not_absorbed(pm_import, monkeypatch):
    """워치독 로드의 marked rev skew는 fill fail-soft 결과로 강등하지 않는다."""
    stale = type("StaleRelay", (), {"ENGINE_REV": "stale"})()
    monkeypatch.setattr(
        pm_import, "_load_watchdog",
        lambda: pm_import._verify_engine_rev(stale, "pm_relay.py"),
    )
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    with pytest.raises(RuntimeError) as excinfo:
        pm_import._real_harness_runner(["claude", "-p", "prompt"], "prompt")

    assert getattr(excinfo.value, "_engine_rev_skew", False) is True


def test_fill_watchdog_missing_file_is_loader_diagnostic(pm_import, monkeypatch):
    """pm_relay.py 부재는 하니스 바이너리 부재가 아니라 워치독 로드 실패로 진단한다."""
    monkeypatch.setattr(
        pm_import, "_load_watchdog",
        lambda: (_ for _ in ()).throw(FileNotFoundError("pm_relay.py missing")),
    )
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    ok, output = pm_import._real_harness_runner(["claude", "-p", "prompt"], "prompt")

    assert ok is False
    assert "워치독 로드 오류" in output
    assert "pm_relay.py missing" in output
    assert "하니스 명령" not in output
    assert "PATH 확인" not in output


def test_fill_profile_failure_is_soft_and_stage_specific(pm_import, monkeypatch):
    """프로필 준비 실패는 spawn 진단과 섞지 않고 해당 단계에서 fail-soft한다."""
    fake = _RecordingRelay()
    fake.resolve_harness_profile = lambda *args: (_ for _ in ()).throw(
        RuntimeError("profile unavailable"))
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    ok, output = pm_import._real_harness_runner(["claude", "-p", "prompt"], "prompt")

    assert ok is False
    assert "프로필 준비 오류" in output
    assert "profile unavailable" in output
    assert not fake.calls


@pytest.mark.parametrize("stage", ["profile", "spawn", "result"])
def test_fill_marked_skew_passes_other_fail_soft_boundaries(
        pm_import, monkeypatch, stage):
    """조정한 프로필·spawn·결과 경계도 marked skew만은 반드시 바깥으로 통과시킨다."""
    skew = RuntimeError(f"{stage} skew")
    skew._engine_rev_skew = True
    fake = _RecordingRelay()
    if stage == "profile":
        fake.resolve_harness_profile = lambda *args: (_ for _ in ()).throw(skew)
    elif stage == "spawn":
        fake._exc = skew
    else:
        class _SkewResult:
            @property
            def stdout(self):
                raise skew

        fake._completed = _SkewResult()
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    with pytest.raises(RuntimeError) as excinfo:
        pm_import._real_harness_runner(["claude", "-p", "prompt"], "prompt")

    assert excinfo.value is skew


def test_fill_result_normalization_failure_is_soft(pm_import, monkeypatch):
    """성공 응답 객체 정규화의 일반 예외는 기존 fill fail-soft 계약을 유지한다."""
    class _BrokenResult:
        @property
        def stdout(self):
            raise RuntimeError("broken result")

    fake = _RecordingRelay(completed=_BrokenResult())
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    ok, output = pm_import._real_harness_runner(["claude", "-p", "prompt"], "prompt")

    assert ok is False
    assert "결과 처리 오류" in output
    assert "broken result" in output


def test_fill_spawn_missing_file_is_binary_diagnostic(pm_import, monkeypatch):
    """실 워치독 spawn의 FileNotFoundError만 하니스 바이너리 부재로 진단한다."""
    fake = _RecordingRelay(exc=FileNotFoundError(2, "missing", "claude"))
    monkeypatch.setattr(pm_import, "_load_watchdog", lambda: fake)
    monkeypatch.setattr(pm_import.subprocess, "Popen", _forbidden_popen)

    ok, output = pm_import._real_harness_runner(["claude", "-p", "prompt"], "prompt")

    assert ok is False
    assert "하니스 명령 'claude'" in output
    assert "설치/PATH 확인" in output
    assert "워치독 로드 오류" not in output


@pytest.mark.parametrize("harness", ["codex", "claude", "opencode"])
def test_delegate_routes_every_driver_through_shared_watchdog(pd, relay, monkeypatch, harness):
    """3 드라이버 **전부** 워치독 경유 — codex/claude 도 증분 관측 대상(옛 구조는 벽시계 단독)."""
    fake = _wire_relay(pd, monkeypatch, _RecordingRelay())
    monkeypatch.setattr(pd.subprocess, "Popen", _forbidden_popen)
    pd._default_run_fn(["bin"], stdin_text="prompt", cwd="/tmp", env={}, timeout=600,
                       harness=harness)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # 무진행 상한은 **그 하네스 축의 선언값**이다(단일 기본값이 아니다).
    assert call["idle_timeout"] == relay.HARNESS_PROFILES[harness].idle_timeout
    assert call["overall_timeout"] == 600         # 벽시계는 호출부가 준 백스톱
    # stdin 프롬프트 주입은 codex/claude 축의 load-bearing 경로(opencode 는 --file).
    assert call["input_text"] == "prompt"


def test_delegate_idle_value_differs_by_axis(pd, relay, monkeypatch):
    """같은 판정 코드에 **다른 값**이 흐른다 — 로컬 GPU 축이 클라우드 축보다 관대하다."""
    seen = {}
    for harness in ("codex", "opencode"):
        fake = _wire_relay(pd, monkeypatch, _RecordingRelay())
        pd._default_run_fn(["bin"], stdin_text=None, cwd="/tmp", env={}, timeout=600,
                           harness=harness)
        seen[harness] = fake.calls[0]["idle_timeout"]
    assert seen["opencode"] > seen["codex"], (
        "축이 갈리지 않았다 — 로컬 GPU 위임(3시간·긴 침묵)이 클라우드 값에 죽는다")
    assert seen["codex"] == relay.CLOUD_IDLE_TIMEOUT_SEC
    assert seen["opencode"] == relay.LOCAL_GPU_IDLE_TIMEOUT_SEC


def test_delegate_harness_conf_override_reaches_the_watchdog(pd, monkeypatch):
    """local.conf `harness.<name>.idle_timeout` 이 실제 판정까지 도달한다(배포별 조정 배선)."""
    fake = _wire_relay(pd, monkeypatch, _RecordingRelay(),
                       conf={"harness.opencode.idle_timeout": "60"})
    pd._default_run_fn(["opencode", "run"], stdin_text=None, cwd="/tmp", env={}, timeout=600,
                       harness="opencode")
    assert fake.calls[0]["idle_timeout"] == 60.0


def _forbidden_popen(*a, **k):
    raise AssertionError("워치독을 우회한 직접 Popen — 3드라이버 통일 위반")


@pytest.mark.parametrize("harness,startup", [("codex", False), ("claude", False),
                                             ("opencode", True)])
def test_delegate_startup_window_follows_declaration(pd, monkeypatch, harness, startup):
    """첫-이벤트 창/재시도는 **선언** 을 따른다 — 분기 특례가 아니라 테이블."""
    fake = _wire_relay(pd, monkeypatch, _RecordingRelay())
    pd._default_run_fn(["bin"], stdin_text=None, cwd="/tmp", env={}, timeout=600,
                       harness=harness)
    call = fake.calls[0]
    if startup:
        assert call["first_event_timeout"] == 90.0 and call["retries"] == 2
    else:
        assert call["first_event_timeout"] is None and call["retries"] == 0


def test_delegate_wall_timeout_resolution_is_per_harness(pd, relay):
    """벽시계 백스톱도 하네스별 — CLI > 하네스 키 > flat legacy > 선언 순으로 해소된다."""
    unset = type("NS", (), {"timeout": None})()
    assert pd._resolve_timeout(unset, {}, "codex") == int(relay.CLOUD_WALL_TIMEOUT_SEC)
    assert pd._resolve_timeout(unset, {}, "opencode") == int(relay.LOCAL_GPU_WALL_TIMEOUT_SEC)
    assert pd._resolve_timeout(unset, {"harness.opencode.wall_timeout": "20000"},
                               "opencode") == 20000
    cli = type("NS", (), {"timeout": 11})()
    assert pd._resolve_timeout(cli, {"harness.opencode.wall_timeout": "20000"}, "opencode") == 11


def test_delegate_timeout_preserves_partial_output_and_silence(pd, monkeypatch):
    """무진행 kill 시 **부분 산출물 + 침묵 초** 가 RunResult 로 회수된다."""
    exc = subprocess.TimeoutExpired(["bin"], 900.0, output="HALF DONE", stderr="warn")
    exc.idle_seconds = 931.0
    _wire_relay(pd, monkeypatch, _RecordingRelay(exc=exc))
    res = pd._default_run_fn(["bin"], stdin_text=None, cwd="/tmp", env={}, timeout=3600,
                             harness="codex")
    assert res["stdout"] == "HALF DONE", "kill 시점 산출물이 버려졌다(전량 폐기 재발)"
    assert res["timed_out"] is True
    assert res[pd.RUN_RESULT_SILENCE_SEC] == 931.0
    assert res[pd.RUN_RESULT_IDLE_KILLED] is True
    assert "무진행" in res["stderr"]
    # false-kill 자기-진단 — 올릴 노브를 사유에 박아 채택자가 헤매지 않게 한다.
    assert "harness.codex.idle_timeout" in res["stderr"]
    assert pd.classify_infrastructure_failure(res) == pd.FAILURE_CLASS_TIMEOUT


def test_delegate_wall_clock_timeout_is_labelled_apart_from_idle(
        pd, relay, monkeypatch):
    """벽시계 백스톱 kill 은 실제 임계·실측 침묵을 싣고 무진행과 구분한다."""
    exc = relay.WallTimeoutExpired(
        ["bin"], 3700.0, silence_seconds=12.5, output="PART"
    )
    _wire_relay(pd, monkeypatch, _RecordingRelay(exc=exc))
    res = pd._default_run_fn(["bin"], stdin_text=None, cwd="/tmp", env={}, timeout=3600,
                             harness="codex")
    assert "벽시계 백스톱 3700s" in res["stderr"] and "무진행" not in res["stderr"]
    assert "실측 침묵 12s" in res["stderr"]
    assert "harness.codex.wall_timeout" in res["stderr"]
    assert pd.RUN_RESULT_IDLE_KILLED not in res
    assert res[pd.RUN_RESULT_SILENCE_SEC] == 12.5
    assert res[pd.RUN_RESULT_TIMEOUT_AXIS] == relay.TIMEOUT_AXIS_WALL
    assert res[pd.RUN_RESULT_TIMEOUT_THRESHOLD_SEC] == 3700.0
    assert res["stdout"] == "PART"


def test_delegate_success_carries_silence_observation(pd, monkeypatch):
    """정상 완주도 관측 침묵 초를 실어 감사 헤더에 남긴다."""
    completed = subprocess.CompletedProcess(["bin"], 0, "out", "")
    _wire_relay(pd, monkeypatch, _RecordingRelay(completed=completed, silence=3.5))
    res = pd._default_run_fn(["bin"], stdin_text=None, cwd="/tmp", env={}, timeout=600,
                             harness="codex")
    assert res[pd.RUN_RESULT_SILENCE_SEC] == 3.5 and res["timed_out"] is False


def test_delegate_legacy_flat_conf_keys_still_honoured(pd):
    """기존 채택자의 표면-flat 키(`delegate_timeout`·`delegate_idle_timeout`)는 계속 유효하다."""
    profile = pd.harness_profile("codex", {"delegate_timeout": "1234",
                                           "delegate_idle_timeout": "88"})
    assert (profile.wall_timeout, profile.idle_timeout) == (1234.0, 88.0)


# ── 값 축 e2e: 3시간 로컬 GPU 위임 시나리오 (판정 전 경로를 실 프로필 값으로) ──────────

def _three_hour_local_gpu_run(clock):
    """실측 증언 형상 — 3시간 완주, 진행은 1시간 간격으로 "가끔"(= 긴 침묵 구간)."""
    return _ScriptedProc(clock, event_times=[0.0, 3600.0, 7200.0], exit_at=3 * 3600.0, rc=0)


def test_three_hour_local_delegation_survives_under_local_profile(relay):
    """3시간 완주 + 1시간 침묵이 **opencode 프로필 값**으로는 완주한다(미설정 채택자 안전)."""
    clock = _FakeClock()
    profile = relay.HARNESS_PROFILES["opencode"]
    proc = _three_hour_local_gpu_run(clock)
    logs: list[str] = []
    result = relay.run_with_first_event_watchdog(
        ["opencode", "run"], first_event_timeout=None,
        overall_timeout=profile.wall_timeout, retries=0,
        idle_timeout=profile.idle_timeout, popen=_scripted_popen([proc]), clock=clock,
        sleep=clock.advance, log=logs.append, poll_interval=60.0,
    )
    assert result.returncode == 0
    assert proc.kill_count == 0, "정상 로컬 GPU 위임이 죽었다 — 값 축이 잘못 잡혔다"
    assert logs == []


def test_three_hour_scenario_would_die_under_cloud_profile(relay):
    """**같은 시나리오가 클라우드 축 값이면 죽는다** — 하네스별 값이 load-bearing 임을 직접 증명.

    이 대비가 곧 값 축의 sensitivity 다: opencode 프로필을 codex 값으로 바꾸는 순간 위 완주
    시나리오가 이 경로로 떨어진다(= 사용자가 실측한 3시간 위임이 false-kill)."""
    clock = _FakeClock()
    proc = _three_hour_local_gpu_run(clock)
    with pytest.raises(relay.IdleTimeoutExpired) as excinfo:
        relay.run_with_first_event_watchdog(
            ["opencode", "run"], first_event_timeout=None,
            overall_timeout=relay.CLOUD_WALL_TIMEOUT_SEC, retries=0,
            idle_timeout=relay.CLOUD_IDLE_TIMEOUT_SEC, popen=_scripted_popen([proc]),
            clock=clock, sleep=clock.advance, log=[].append, poll_interval=60.0,
        )
    assert excinfo.value.idle_seconds >= relay.CLOUD_IDLE_TIMEOUT_SEC
    assert proc.kill_count == 1


def test_local_gpu_axis_wall_backstop_still_finite(relay):
    """로컬 GPU 축도 **무제한이 아니다** — 진행이 계속돼도 4시간 백스톱은 닫는다."""
    clock = _FakeClock()
    profile = relay.HARNESS_PROFILES["opencode"]
    forever = _ScriptedProc(clock, event_times=[i * 600.0 for i in range(200)], exit_at=None)
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        relay.run_with_first_event_watchdog(
            ["opencode", "run"], first_event_timeout=None,
            overall_timeout=profile.wall_timeout, retries=0,
            idle_timeout=profile.idle_timeout, popen=_scripted_popen([forever]),
            clock=clock, sleep=clock.advance, log=[].append, poll_interval=60.0,
        )
    assert not isinstance(excinfo.value, relay.IdleTimeoutExpired)
    assert forever.kill_count == 1


# ── local.conf 노출 (채택자가 노브의 존재를 알아야 한다) ──────────────────────────

@pytest.fixture
def board(tmp_path, monkeypatch):
    """tmp 로 격리된 board 모듈 — init 의 local.conf 효과만 본다(실 git/stdin 부작용 stub)."""
    proj = tmp_path / "proj"
    pm = proj / ".project_manager"
    (pm / "wiki" / "tickets").mkdir(parents=True, exist_ok=True)
    mod = _load("board_harness_seed", TOOLS / "board.py")
    for name, val in {"REPO": proj, "LOCAL_CONF": pm / "local.conf",
                      "AREAS_FILE": pm / "areas.md",
                      "PM_STATE_FILE": pm / "wiki" / "pm_state.md",
                      "PM_STATE_TEMPLATE": pm / "wiki" / "pm_state.template.md"}.items():
        monkeypatch.setattr(mod, name, val)
    monkeypatch.setattr(mod, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(mod, "prompt_external_review_optin", lambda: None)
    monkeypatch.setattr(mod, "_configure_board_submodule", lambda: False)
    monkeypatch.setattr(mod, "_detect_py", lambda: "python3")
    monkeypatch.setattr(mod, "_is_noninteractive", lambda: True)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    return mod


def _init_args():
    import argparse
    return argparse.Namespace(prefix=None, area=None, owner=None, repo=None, slot=None, user=None)


def test_local_conf_seed_documents_harness_budget_keys(board, relay):
    """fresh init 이 하네스별 시간 예산 키를 **시드로 노출**한다 — 존재를 모르면 못 쓴다.

    GPU 부족은 엔진 속성이 아니라 배포 환경 조건이라 per-clone 으로 조여야 하는데, 키가 문서에
    없으면 채택자는 false-kill 을 당하고도 노브를 못 찾는다."""
    assert board.cmd_init(_init_args()) == 0
    conf = board.LOCAL_CONF.read_text(encoding="utf-8")
    assert board._HARNESS_BUDGET_SEED_MARKER in conf
    for harness in relay.HARNESS_PROFILES:
        assert f"harness.{harness}.idle_timeout=" in conf
        assert f"harness.{harness}.wall_timeout=" in conf
    # 전부 주석(활성 key 0) — 시드가 미설정 채택자의 실행 값을 바꾸지 않는다.
    active = [line for line in conf.splitlines()
              if line.strip().startswith("harness.") and not line.strip().startswith("#")]
    assert active == []
    assert "미설정이어도 안전" in conf          # 기본값이 안전하다는 사실도 알린다
    assert "external_review" in conf           # 같은 키를 리뷰 축도 읽는다는 사실


def test_local_conf_seed_reaches_existing_adopters_idempotently(board):
    """기존 local.conf 를 가진 채택자도 재실행에 블록을 받고, 중복 append 는 없다."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text(
        "session=pm\npy=python3\ntest_cmd=pytest -q\n"
        "# ── cross-harness 역할 위임 (pm_delegate·기본 OFF) ──\n", encoding="utf-8")
    board.cmd_init(_init_args())
    once = board.LOCAL_CONF.read_text(encoding="utf-8")
    board.cmd_init(_init_args())
    twice = board.LOCAL_CONF.read_text(encoding="utf-8")
    assert once.count(board._HARNESS_BUDGET_SEED_MARKER) == 1
    assert twice.count(board._HARNESS_BUDGET_SEED_MARKER) == 1   # 멱등
    assert "session=pm" in twice                                 # 비파괴 병합


def test_seeded_values_match_engine_declarations(board, relay):
    """시드와 pm-env 4사본이 엔진/출하 선언과 일치 — 문서 drift 가 오설정을 유도하지 않게."""
    seed = board._HARNESS_BUDGET_CONF_SEED
    for harness, profile in relay.HARNESS_PROFILES.items():
        assert f"harness.{harness}.idle_timeout={int(profile.idle_timeout)}" in seed
        assert f"harness.{harness}.wall_timeout={int(profile.wall_timeout)}" in seed
    settings = json.loads(
        (REPO / "templates/claude_code/.claude/settings.json").read_text(encoding="utf-8"))
    cap_ms = int(settings["env"]["BASH_MAX_TIMEOUT_MS"])
    cards = (
        REPO / ".claude/skills/pm-env/SKILL.md",
        REPO / "templates/claude_code/.claude/skills/pm-env/SKILL.md",
        REPO / "templates/codex/.agents/skills/pm-env/SKILL.md",
        REPO / "templates/opencode/.claude/skills/pm-env/SKILL.md",
    )
    for card in cards:
        text = card.read_text(encoding="utf-8")
        assert f"출하 기본 {cap_ms}=" in text, card
        assert f"OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS={cap_ms}" in text, card


# ── ⑤ claude stream-json 전환 — 회신 추출 동치 ────────────────────────────────────

def test_claude_argv_declares_stream_json_with_required_verbose(pd):
    """claude 는 신호 축으로 승격 — `stream-json` + CLI 강제 `--verbose`(미동반 시 즉시 rc≠0)."""
    argv = pd.build_claude_argv("opus", None, "developer")
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert "json" not in argv[argv.index("--output-format") + 1:argv.index("--verbose")][1:]


def test_claude_reply_extraction_identical_across_formats(pd, relay):
    """전환 전후 회신 추출 동일 — 두 형식이 같은 파서를 통과한다(⑤ 전제 검증).

    구 `--output-format json` = 종료 시 단일 덩어리(`type:result` 한 줄), 신 `stream-json` =
    init/assistant/result 줄 단위 스트림. 둘 다 `parse_stream_json` 이 같은
    (sid, reply, used_tokens) 를 낸다."""
    single_blob = json.dumps({"type": "result", "subtype": "success",
                              "result": "REPLY-BODY", "session_id": "sess-1"})
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text",
                                                                  "text": "부분"}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": "REPLY-BODY",
                    "session_id": "sess-1"}),
    ])
    assert relay.parse_stream_json(single_blob.splitlines()) == ("sess-1", "REPLY-BODY", None)
    assert relay.parse_stream_json(stream.splitlines()) == ("sess-1", "REPLY-BODY", None)
    assert pd.extract_reply("claude", single_blob) == pd.extract_reply("claude", stream)


# ── ⑥ 감사 헤더 (침묵 초) ────────────────────────────────────────────────────────

def test_raw_header_records_silence_seconds(pd):
    """감사 헤더에 `# silence_sec:` — 관측 불가는 `n/a`(0 으로 위장 금지)·무진행 kill 은 사유 병기."""
    meta = pd._format_meta(["codex"], 1, "codex", "m", 12.0, "", "", silence_sec=931.4,
                           idle_killed=True)
    assert "# silence_sec: 931.4 (무진행 판정으로 중단)" in meta
    plain = pd._format_meta(["codex"], 0, "codex", "m", 12.0, "", "", silence_sec=2.0)
    assert "# silence_sec: 2.0" in plain and "무진행" not in plain
    blind = pd._format_meta(["codex"], 0, "codex", "m", 12.0, "", "")
    assert "# silence_sec: n/a (진행 신호 미관측)" in blind


def test_execute_attempt_writes_silence_into_raw(pd, tmp_path):
    """`_execute_attempt` 가 RunResult 의 침묵 관측치를 raw 파일에 실제로 박제한다(감사 종단)."""
    def _run(argv, *, stdin_text, cwd, env, timeout, harness):
        return {"returncode": 1, "stdout": "PART", "stderr": "boom", "timed_out": True,
                pd.RUN_RESULT_SILENCE_SEC: 902.0, pd.RUN_RESULT_IDLE_KILLED: True}

    attempt = pd._execute_attempt(
        harness="codex", model="m", reasoning=None, role="developer", cwd=tmp_path,
        prompt="p", timeout=1700, output_dir=tmp_path, run_fn=_run, attempt="primary",
    )
    raw = attempt.raw_path.read_text(encoding="utf-8")
    assert "# silence_sec: 902.0 (무진행 판정으로 중단)" in raw
    assert "PART" in raw, "부분 산출물이 raw 에 박제되지 않았다"


# ── ⑧ external_review 표면 — 같은 공용 seam ──────────────────────────────────────

def test_external_review_default_runner_uses_shared_relay_seam(external, monkeypatch):
    """리뷰어 기본 러너가 pm_relay 공용 워치독을 탄다 — `subprocess.run` 단일 호출이 아니다."""
    fake = _RecordingRelay(
        completed=subprocess.CompletedProcess(["codex"], 0, "판정: 통과", ""))
    monkeypatch.setattr(external, "_load_relay", lambda: fake)
    monkeypatch.setattr(external.subprocess, "run", _forbidden_subprocess_run)
    ok, output, started = external._run_reviewer_ex("prompt", "codex exec", 1700, None)
    assert (ok, started) == (True, True) and "판정: 통과" in output
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["idle_timeout"] == 900.0          # 평문 축도 같은 선언 테이블을 탄다
    assert call["first_event_timeout"] is None    # codex profile=False → 기존 동작 불변
    assert call["retries"] == 0
    assert call["overall_timeout"] == 1700.0      # 벽시계는 백스톱
    assert call["input_text"] == "prompt"         # 프롬프트 stdin 주입


def test_external_review_opencode_profile_enables_startup_retry(
        external, monkeypatch):
    """리뷰 축도 opencode 프로필의 startup watchdog/재시도를 소비한다."""
    fake = _RecordingRelay(
        completed=subprocess.CompletedProcess(["opencode"], 0, "판정: 통과", ""))
    monkeypatch.setattr(external, "_load_relay", lambda: fake)
    ok, _output, started = external._run_reviewer_ex(
        "prompt", "opencode run --format json", 14400, None
    )
    assert (ok, started) == (True, True)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["first_event_timeout"] == 90.0
    assert call["retries"] == 2
    assert call["overall_timeout"] == 14400.0


def _forbidden_subprocess_run(*a, **k):
    raise AssertionError("리뷰어가 공용 seam 을 우회해 subprocess.run 을 직접 썼다(T-0489 ⑧ 위반)")


def test_external_review_idle_kill_preserves_partial_output(external, monkeypatch):
    """무진행 kill 시 **그때까지 받은 출력**이 결과 본문에 남는다 — 17차 138바이트 폐쇄(⑨)."""
    exc = subprocess.TimeoutExpired(["codex"], 900.0, output="REVIEW SO FAR",
                                    stderr="progress log")
    exc.idle_seconds = 905.0
    monkeypatch.setattr(external, "_load_relay", lambda: _RecordingRelay(exc=exc))
    ok, output, started = external._run_reviewer_ex("p", "codex exec", 1700, None)
    assert (ok, started) == (False, True)
    # 타임아웃 진단은 회신 채널 하나에 담긴다(T-0563 채널 구조 분리 — 구분자 파싱 없음).
    diagnosis = output.answer
    assert "REVIEW SO FAR" in diagnosis and "progress log" in diagnosis
    assert "무진행 임계 900초" in diagnosis and "실측 침묵 905초" in diagnosis
    assert "--idle-timeout" in diagnosis and external.EXTERNAL_IDLE_TIMEOUT_KEY in diagnosis


def test_external_review_wall_timeout_keeps_actual_diagnostics(
        external, relay, monkeypatch):
    """벽시계 안내는 기존 조정 토큰과 실제 임계·실측 침묵을 함께 보존한다."""
    exc = relay.WallTimeoutExpired(
        ["codex"], 1800.0, silence_seconds=7.0, output="PARTIAL"
    )
    monkeypatch.setattr(external, "_load_relay", lambda: _RecordingRelay(exc=exc))
    ok, output, started = external._run_reviewer_ex("p", "codex exec", 1700, None)
    assert (ok, started) == (False, True)
    diagnosis = output.answer
    assert "리뷰어 타임아웃" in diagnosis and "--timeout <초>" in diagnosis
    assert "벽시계 백스톱 1800초" in diagnosis and "실측 침묵 7초" in diagnosis
    assert "external_review_timeout=<초>" in diagnosis
    assert "PARTIAL" in diagnosis


def test_external_review_run_review_saves_partial_output(external, monkeypatch, tmp_path):
    """타임아웃 결과도 원문 파일로 박제된다 — 부분 산출물이 디스크에 남아야 재개가 가능하다."""
    exc = subprocess.TimeoutExpired(["codex"], 900.0, output="HALF REVIEW")
    exc.idle_seconds = 902.0
    monkeypatch.setattr(external, "_load_relay", lambda: _RecordingRelay(exc=exc))
    result = external.run_review("p", reviewer_cmd="codex exec", timeout=1700,
                                 output_dir=tmp_path)
    assert result["failed"] is True and result["started"] is True
    assert result["file"] is not None
    assert "HALF REVIEW" in result["file"].read_text(encoding="utf-8")


def test_external_review_cleanup_failure_saves_partial_output(
        external, relay, tmp_path):
    """리뷰 소비처도 cleanup sentinel을 실패로 수합하고 부분 산출물을 raw에 박제한다."""
    exc = relay.ProcessCleanupError(
        "detached child kept pipe open",
        output="HALF REVIEW BEFORE CLEANUP",
        stderr="cleanup diagnostic",
    )

    def fail_cleanup(*_args, **_kwargs):
        raise exc

    result = external.run_review(
        "p", reviewer_cmd="codex exec", timeout=1700,
        output_dir=tmp_path, run_fn=fail_cleanup,
    )
    assert result["failed"] is True and result["started"] is True
    assert result["file"] is not None
    raw = result["file"].read_text(encoding="utf-8")
    assert "프로세스 정리 실패" in raw
    assert "잔존 프로세스 가능성" in raw
    assert "자동 재시도/폴백 금지" in raw
    assert "HALF REVIEW BEFORE CLEANUP" in raw
    assert "cleanup diagnostic" in raw


def test_external_review_idle_resolution_order(external, relay, capsys):
    """`--idle-timeout` > 하네스 키 > flat legacy > 프로필 선언 — 위임 축과 **같은 해소 순서**."""
    cli = external.argparse.Namespace(idle_timeout=45.0)
    assert external._resolve_idle_timeout(cli, {external.EXTERNAL_IDLE_TIMEOUT_KEY: "77"}) == 45.0
    unset = external.argparse.Namespace(idle_timeout=None)
    assert external._resolve_idle_timeout(
        unset, {external.EXTERNAL_IDLE_TIMEOUT_KEY: "77"}) == 77.0
    assert external._resolve_idle_timeout(
        unset, {external.EXTERNAL_IDLE_TIMEOUT_KEY: "77",
                "harness.codex.idle_timeout": "88"}) == 88.0      # 하네스 키가 더 구체적
    # 미설정이면 리뷰어 커맨드의 하네스 프로필(기본 codex 축) — 별도 상수가 아니다.
    assert external._resolve_idle_timeout(unset, {}) == relay.CLOUD_IDLE_TIMEOUT_SEC
    assert external._resolve_idle_timeout(
        unset, {external.EXTERNAL_IDLE_TIMEOUT_KEY: "x"}) == relay.CLOUD_IDLE_TIMEOUT_SEC
    assert external.EXTERNAL_IDLE_TIMEOUT_KEY in capsys.readouterr().err


def test_external_review_follows_reviewer_harness_profile(external, relay):
    """리뷰어 커맨드의 **하네스 프로필**을 따른다 — 별도 타임아웃 상수 0(값 출처 단일)."""
    unset = external.argparse.Namespace(timeout=None, idle_timeout=None)
    assert external._resolve_timeout(unset, {}, "codex exec --sandbox read-only") == \
        int(relay.CLOUD_WALL_TIMEOUT_SEC)
    # 로컬 GPU 리뷰어(opencode)를 물리면 그 축의 값으로 갈린다 — 표면이 아니라 축이 값을 정한다.
    assert external._resolve_timeout(unset, {}, "opencode run") == \
        int(relay.LOCAL_GPU_WALL_TIMEOUT_SEC)
    # 테이블에 없는 리뷰어 CLI 는 신호 없음 fallback(모르는 리뷰어를 무진행으로 죽이지 않는다).
    unknown = external.reviewer_profile("my-reviewer --flag", {})
    assert unknown.progress_signal == relay.PROGRESS_SIGNAL_NONE
    assert unknown.wall_timeout == relay.DEFAULT_WALL_TIMEOUT_SEC
    # 이 모듈은 자체 타임아웃 상수를 두지 않는다(값이 두 군데면 규칙이 둘).
    source = (TOOLS / "external_review.py").read_text(encoding="utf-8")
    assert "EXTERNAL_TIMEOUT_SECONDS" not in source


@pytest.mark.parametrize("command", ["codex.exe exec", "CODEX.EXE exec", "codex.cmd exec"])
def test_windows_codex_names_share_progress_and_time_profile(
        external, relay, command):
    """Windows 표기도 진행신호·시간값 모두 동일한 정규화 키(codex)를 쓴다."""
    profile = external.reviewer_profile(command, {})
    assert external.reviewer_name(command) == "codex"
    assert profile.progress_signal == relay.PROGRESS_SIGNAL_PLAINTEXT
    assert profile.idle_timeout == relay.HARNESS_PROFILES["codex"].idle_timeout
    assert profile.wall_timeout == relay.HARNESS_PROFILES["codex"].wall_timeout
    assert profile.wall_timeout != relay.REVIEWER_FALLBACK_PROFILE.wall_timeout


def test_reviewer_idle_timeout_uses_resolved_profile_signal(external, relay, monkeypatch):
    """리뷰 표면도 profile.progress_signal 을 소비 — 신호 없는 선언을 평문으로 하드코딩하지 않는다."""
    no_signal = relay.HARNESS_PROFILES["codex"]._replace(
        progress_signal=relay.PROGRESS_SIGNAL_NONE)
    monkeypatch.setattr(external, "reviewer_profile", lambda *_a, **_k: no_signal)
    assert external._reviewer_idle_timeout("future-runner", 123) is None


def test_reviewer_progress_signal_requires_known_executable_and_option_contract(external, relay):
    """basename+CLI별 옵션 계약 매트릭스: 이름만/플래그만 어느 한쪽으로는 추론하지 않는다."""
    absolute_codex = external.reviewer_profile("/usr/local/bin/codex exec", {})
    assert absolute_codex.progress_signal == relay.PROGRESS_SIGNAL_PLAINTEXT
    assert external.reviewer_profile(
        "/usr/local/bin/codex exec --json", {}
    ).progress_signal == relay.PROGRESS_SIGNAL_EVENT_STREAM
    assert external.reviewer_profile(
        "future-reviewer --json", {}
    ).progress_signal == relay.PROGRESS_SIGNAL_NONE

    single = external.reviewer_profile("claude -p --output-format json", {})
    stream = external.reviewer_profile(
        "claude -p --output-format stream-json --verbose", {}
    )
    assert single.progress_signal == relay.PROGRESS_SIGNAL_NONE
    assert external._reviewer_idle_timeout(
        "claude -p --output-format json", 1
    ) is None
    assert stream.progress_signal == relay.PROGRESS_SIGNAL_EVENT_STREAM
    assert external._reviewer_idle_timeout(
        "claude -p --output-format stream-json --verbose", 1
    ) == 1.0
    assert external.reviewer_profile(
        "codex exec --json", {}
    ).progress_signal == relay.PROGRESS_SIGNAL_EVENT_STREAM
    assert external.reviewer_profile(
        "opencode run --format json", {}
    ).progress_signal == relay.PROGRESS_SIGNAL_EVENT_STREAM

    configured = external.reviewer_profile(
        "future-reviewer --json",
        {external.EXTERNAL_PROGRESS_SIGNAL_KEY: relay.PROGRESS_SIGNAL_PLAINTEXT},
    )
    assert configured.progress_signal == relay.PROGRESS_SIGNAL_PLAINTEXT


def test_unknown_reviewer_can_only_opt_into_progress_explicitly(external, relay):
    """미지 기본은 NONE이며 자유 문자열 CLI는 local.conf 명시 선언으로만 idle 축에 들어간다."""
    assert external.reviewer_profile(
        "future-reviewer --quiet", {}
    ).progress_signal == relay.PROGRESS_SIGNAL_NONE
    configured = external.reviewer_profile(
        "future-reviewer --quiet",
        {external.EXTERNAL_PROGRESS_SIGNAL_KEY: relay.PROGRESS_SIGNAL_PLAINTEXT},
    )
    assert configured.progress_signal == relay.PROGRESS_SIGNAL_PLAINTEXT


def test_reviewer_profile_facade_reads_local_conf(external, relay, monkeypatch):
    """conf 생략 facade도 local.conf의 값/신호 override를 읽어 위임 축과 대칭이다."""
    monkeypatch.setattr(
        external,
        "local_config",
        lambda: {
            "harness.claude.wall_timeout": "4321",
            external.EXTERNAL_PROGRESS_SIGNAL_KEY: relay.PROGRESS_SIGNAL_PLAINTEXT,
        },
    )
    profile = external.reviewer_profile("claude -p --output-format json")
    assert profile.wall_timeout == 4321.0
    assert profile.progress_signal == relay.PROGRESS_SIGNAL_PLAINTEXT


def test_external_review_cli_rejects_nonpositive_idle_timeout(external, capsys):
    """CLI `--idle-timeout` 0/음수는 usage error(rc=2) — `--timeout` 과 동일 규칙."""
    for raw in ("0", "-5"):
        with pytest.raises(SystemExit) as exc:
            external.main(["--idle-timeout", raw, "--dry-run"])
        assert exc.value.code == 2
        assert "--idle-timeout" in capsys.readouterr().err


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0.5", "1.5"])
def test_external_review_cli_rejects_nonfinite_or_fractional_idle(external, raw, capsys):
    """idle CLI 는 유한한 정수 초만 허용 — NaN 우회와 int 절단을 함께 폐쇄."""
    with pytest.raises(SystemExit) as exc:
        external.main(["--idle-timeout", raw, "--dry-run"])
    assert exc.value.code == 2
    assert "--idle-timeout" in capsys.readouterr().err


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0.5", "1.5"])
def test_local_conf_timeout_normalization_rejects_nonfinite_and_fractional(
        relay, raw, capsys):
    """local.conf 도 CLI 와 같은 정규화 경계를 탄다."""
    profile = relay.resolve_harness_profile(
        "codex", {"harness.codex.idle_timeout": raw})
    assert profile.idle_timeout == relay.HARNESS_PROFILES["codex"].idle_timeout
    assert "유한한 정수 초" in capsys.readouterr().err


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.5, 1.5, 0])
def test_watchdog_api_rejects_invalid_idle_timeout_at_shared_boundary(relay, value):
    """CLI 를 우회한 직접 API 호출도 같은 정규화 경계에서 fail-loud 한다."""
    with pytest.raises(ValueError, match="유한한 정수 초"):
        relay.run_with_first_event_watchdog(
            ["never-spawned"], first_event_timeout=None, overall_timeout=10,
            retries=0, idle_timeout=value,
            popen=lambda _argv: pytest.fail("검증 전 프로세스를 스폰함"),
        )


def test_external_review_wall_clock_is_a_backstop_not_a_primary(external):
    """벽시계 기본이 백스톱 대역으로 올라갔다 — 옛 900 은 정상 리뷰 분산 대역 *안*이었다.

    실측: 같은 입력 2회 중 하나는 900초 초과 kill / 다른 하나는 `--timeout 1500` 성공. 900 으로
    되돌리면 그 false-kill 구조가 그대로 복원된다."""
    unset = external.argparse.Namespace(timeout=None)
    assert external._resolve_timeout(unset, {}) > 1500


def test_external_review_runtime_harness_cap_advisory(external):
    """명시 timeout 호출은 MAX가 제약: 출하 DEFAULT 1800/MAX 29300 조합은 상시 경고하지 않는다."""
    shipped = {
        "CLAUDECODE": "1",
        "BASH_DEFAULT_TIMEOUT_MS": "1800000",
        "BASH_MAX_TIMEOUT_MS": "29300000",
    }
    codex_budget = external._reviewer_execution_budget("codex exec", 3600)
    assert codex_budget == 3610  # wall + 부모 wait 5s + pipe drain 5s
    assert external.harness_cap_advisory(
        shipped, execution_budget=codex_budget
    ) is None

    warning = external.harness_cap_advisory(
        {
            "CLAUDECODE": "1",
            "BASH_DEFAULT_TIMEOUT_MS": "1800000",
            "BASH_MAX_TIMEOUT_MS": "3600000",
        },
        execution_budget=codex_budget,
    )
    assert warning is not None
    assert "3600s" in warning and "3620s" in warning
    assert "BASH_MAX_TIMEOUT_MS" in warning
    assert external.harness_cap_advisory(
        {
            "CLAUDECODE": "1",
            "BASH_DEFAULT_TIMEOUT_MS": "1800000",
            "BASH_MAX_TIMEOUT_MS": "29300000",
        },
        execution_budget=codex_budget,
    ) is None


def test_both_surfaces_share_retry_cleanup_budget_formula(external, pd, relay):
    """외부리뷰·위임이 startup 재시도마다 wait+drain 10초를 같은 공용 식으로 센다."""
    expected = 14400 + 2 * 90 + 3 * 10
    assert relay.watchdog_execution_budget(
        14400, first_event_timeout=90, retries=2
    ) == expected
    assert pd._harness_timeout_budget("opencode", 14400) == expected
    assert external._reviewer_execution_budget(
        "opencode run --format json", 14400
    ) == expected
    assert relay.harness_cap_required_budget(expected) == expected + 10


def test_wall_before_startup_window_has_single_attempt_budget(relay):
    """wall <= first-event 창이면 자동 재시도가 없으므로 실행·정리도 한 시도만 센다."""
    assert relay.watchdog_execution_budget(
        30, first_event_timeout=90, retries=2
    ) == 40


# ── DoD: 세 표면이 **같은 판정 코드** 를 탄다 (복붙 구현 0) ──────────────────────────

def test_all_surfaces_load_the_same_relay_judgment_code(pd, external, pm_import):
    """위임·외부리뷰·fill이 로드하는 공용 seam 이 **같은 소스 파일의 같은 함수**다.

    한쪽만 고쳐진 상태(복붙 구현·자체 판정)면 이 단언이 무너진다 — 편입의 목적이 "거기도 고쳐야
    해서"가 아니라 "규칙이 둘로 갈리는 걸 막기 위해서"이므로 코드 동일성을 직접 잠근다."""
    delegate_relay = pd._load_relay()
    review_relay = external._load_relay()
    fill_relay = pm_import._load_watchdog()
    canonical = str(TOOLS / "pm_relay.py")
    for module in (delegate_relay, review_relay, fill_relay):
        fn = module.run_with_first_event_watchdog
        assert fn.__code__.co_filename == canonical
        assert module.idle_timeout_for_signal.__code__.co_filename == canonical
    assert delegate_relay.DEFAULT_IDLE_TIMEOUT_SEC == review_relay.DEFAULT_IDLE_TIMEOUT_SEC


def test_idle_judgment_is_implemented_once(relay):
    """무진행 판정 구현은 pm_relay 단독 — 세 표면에 복붙되면 red(규칙 분기 방지)."""
    relay_src = (TOOLS / "pm_relay.py").read_text(encoding="utf-8")
    assert relay_src.count("raise IdleTimeoutExpired(") == 1
    for tool in ("pm_delegate.py", "external_review.py", "pm_import.py"):
        src = (TOOLS / tool).read_text(encoding="utf-8")
        assert "IdleTimeoutExpired(" not in src, (
            f"{tool} 이 무진행 판정을 자체 구현했다 — 공용 seam(pm_relay) 재사용이 DoD")
        assert "run_with_first_event_watchdog" in src, (
            f"{tool} 이 공용 워치독 seam 을 안 탄다 — 한쪽만 고쳐진 상태")


# ── 스폰 경계 seam: 소유권 이전은 `Popen` 직전 한 자리 (T-0590 R3) ───────────
#
# 워치독 진입~`Popen` 사이에는 인자 검증·프로필 해소·워치독 준비가 있고 그 구간엔 자식이 없다.
# 예산 소유권을 워치독 **호출** 지점에서 넘기면 그 준비 실패가 전송 0·과금 0 인데도 예산을 먹는다.
# 경계는 여기 한 자리이고, 어댑터가 exec 에 실패해 던진 OSError 에는 "자식 없음" 표식이 붙는다.


def test_spawn_seam_fires_immediately_before_the_child(relay):
    """콜백은 `popen(argv)` **직전**에 돈다 — 준비가 끝난 뒤, 자식이 생기기 전."""
    clock = _FakeClock()
    order: list[str] = []
    proc = _ScriptedProc(clock, event_times=[0.0], exit_at=1.0)

    def popen(_argv):
        order.append("popen")
        return proc

    result = relay.run_with_first_event_watchdog(
        ["drv"], first_event_timeout=5.0, overall_timeout=30.0, retries=0,
        idle_timeout=None, popen=popen, clock=clock, sleep=clock.advance,
        log=[].append, poll_interval=1.0,
        on_spawn_attempt=lambda: order.append("handoff"),
    )
    assert result.returncode == 0
    assert order == ["handoff", "popen"]


def test_spawn_seam_fires_once_across_stall_retries(relay):
    """stall 재시도로 자식을 여러 번 띄워도 소유권 이전은 실행당 1회다(중복 이전 없음)."""
    clock = _FakeClock()
    procs = [_ScriptedProc(clock, event_times=[], exit_at=None) for _ in range(2)]
    handoffs: list[int] = []
    with pytest.raises(relay.StallWatchdogError):
        relay.run_with_first_event_watchdog(
            ["opencode", "run"], first_event_timeout=5.0, overall_timeout=600.0,
            retries=1, idle_timeout=900.0, popen=_scripted_popen(procs), clock=clock,
            sleep=clock.advance, log=[].append, poll_interval=1.0,
            on_spawn_attempt=lambda: handoffs.append(1),
        )
    assert [p.kill_count for p in procs] == [1, 1]   # 두 번 떴다
    assert handoffs == [1], "재시도마다 소유권을 다시 넘겼다"


@pytest.mark.parametrize(
    "exc_type",
    [FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError],
)
def test_exec_failure_from_the_adapter_is_marked_as_no_child(relay, exc_type):
    """어댑터의 exec 실패는 '자식 없음' 표식과 함께 그대로 올라간다(호출부 환불 판정 입력)."""
    handoffs: list[int] = []

    def popen(_argv):
        raise exc_type("drv")

    with pytest.raises(exc_type) as excinfo:
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=30.0, retries=0,
            idle_timeout=None, popen=popen, clock=_FakeClock(), sleep=lambda _s: None,
            log=[].append, on_spawn_attempt=lambda: handoffs.append(1),
        )
    assert getattr(excinfo.value, relay.SPAWN_FAILED_ATTR) is True
    assert handoffs == [1], "경계 콜백은 exec 시도 앞에서 이미 돌아야 한다"


def test_preparation_failure_never_hands_off_ownership(relay):
    """인자 검증 실패(=`Popen` 전)는 콜백을 태우지 않는다 — 소유권은 호출부에 남는다."""
    handoffs: list[int] = []
    with pytest.raises(ValueError):
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=30.0, retries=0,
            idle_timeout=0.5,                      # 유한 정수 초가 아님 → 검증 실패
            popen=lambda _argv: pytest.fail("검증 전 프로세스를 스폰함"),
            clock=_FakeClock(), sleep=lambda _s: None, log=[].append,
            on_spawn_attempt=lambda: handoffs.append(1),
        )
    assert handoffs == []


def test_post_spawn_marking_wins_over_the_exec_failure_default(relay):
    """이미 '자식 있었음'으로 표식된 예외는 경계 층이 덮어쓰지 않는다(최초 관측이 진실)."""
    marked = PermissionError("초기화 실패 롤백")
    relay._mark_spawn_failed(marked, False)

    def popen(_argv):
        raise marked

    with pytest.raises(PermissionError) as excinfo:
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=None, overall_timeout=30.0, retries=0,
            idle_timeout=None, popen=popen, clock=_FakeClock(), sleep=lambda _s: None,
            log=[].append, on_spawn_attempt=lambda: None,
        )
    assert getattr(excinfo.value, relay.SPAWN_FAILED_ATTR) is False


def test_watched_popen_marks_post_spawn_init_failure_as_spawned(relay, monkeypatch):
    """`Popen` 성공 뒤의 초기화 실패는 자식이 **있었던** 실행이라 표식이 False 다.

    실 자식을 띄운 뒤 리더 스레드 생성을 실패시켜, 롤백(그룹 kill·drain)을 거쳐 올라오는 원
    예외의 표식을 본다. 종류만 보고 환불하면 이 형상이 '전송 0' 으로 오분류된다.
    """
    class _NoThreads:
        """relay 가 쓰는 threading 표면 대역 — Thread 생성만 실패시킨다."""

        Event = threading.Event
        Lock = threading.Lock

        @staticmethod
        def Thread(*args, **kwargs):        # noqa: N802 — 표준 API 이름을 맞춘다
            raise PermissionError("스레드 생성 거부")

    monkeypatch.setattr(relay, "threading", _NoThreads)
    with pytest.raises(PermissionError) as excinfo:
        relay._WatchedPopen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert getattr(excinfo.value, relay.SPAWN_FAILED_ATTR) is False


# ── 스폰 경계 표식: 경계가 종류를 이긴다 (T-0590 R4) ─────────────────────────
#
# "자식이 있었나"는 예외 **종류**로 알 수 없다. 같은 `ValueError` 가 `Popen` 이 fork 전에 argv 를
# 거절한 결과일 수도(전송 0·환불 대상), `Popen` 성공 뒤 초기화가 깨진 결과일 수도(자식 있었음·
# 과금 가능) 있다. 그래서 판정은 경계를 실제로 본 층이 표식으로 남기고, 상위는 그 표식을 읽는다.
# 그리고 표식은 **실행 단위**다 — 재시도 중 한 번이라도 자식이 떴으면 그 뒤 어떤 기동 실패도
# 실행 전체를 '자식 없음'으로 되돌릴 수 없다(앞선 자식이 이미 프롬프트를 보냈을 수 있다).


def test_a_second_attempt_launch_failure_never_unmarks_the_first_child(relay):
    """1회차가 자식을 띄우고 stall → 2회차 기동 실패: 표식은 '자식 있었음'(False)이다.

    시도 단위로만 보면 2회차는 exec 실패라 '자식 없음'이다. 그 표식이 그대로 올라가면 상위가
    실행 전체를 스폰 0 으로 읽어 **1회차에 이미 나갔을 수 있는 전송**을 환불한다(라운드/wave 예산이
    조용히 되살아난다). 실행 단위 사실이 시도 단위 관측을 이겨야 한다."""
    clock = _FakeClock()
    first = _ScriptedProc(clock, event_times=[], exit_at=None)   # 첫 이벤트가 영원히 안 옴
    attempts: list[str] = []
    handoffs: list[int] = []

    def popen(_argv):
        if not attempts:
            attempts.append("spawned")
            return first
        attempts.append("launch-failed")
        raise FileNotFoundError("drv")            # 2회차: exec 실패(이 시도엔 자식 없음)

    with pytest.raises(FileNotFoundError) as excinfo:
        relay.run_with_first_event_watchdog(
            ["drv"], first_event_timeout=5.0, overall_timeout=600.0, retries=1,
            idle_timeout=None, popen=popen, clock=clock, sleep=clock.advance,
            log=[].append, poll_interval=1.0,
            on_spawn_attempt=lambda: handoffs.append(1),
        )

    assert attempts == ["spawned", "launch-failed"]     # 정확히 2회 시도
    assert first.kill_count == 1                        # 1회차 자식은 실제로 떴다가 kill 됐다
    assert getattr(excinfo.value, relay.SPAWN_FAILED_ATTR) is False, (
        "2회차 기동 실패가 1회차 자식을 '자식 없음'으로 되돌렸다 — 나간 전송이 환불된다")
    assert handoffs == [1]                              # 소유권 이전은 실행당 1회 그대로


def test_watched_popen_marks_a_nul_argv_rejection_as_no_child(relay):
    """argv 에 NUL 이 섞이면 `Popen` 이 fork **전에** `ValueError` 로 거절한다 → 자식 없음.

    OSError 만 '자식 없음'으로 표식하면 이 형상은 표식이 없어, 상위가 보수적으로 '전송됐을 수
    있음'으로 읽고 예산을 태운다 — 실제로는 프로세스가 만들어진 적이 없다."""
    with pytest.raises(ValueError) as excinfo:
        relay._WatchedPopen([sys.executable, "-c", "print(1)\x00"])
    assert "null" in str(excinfo.value)
    assert getattr(excinfo.value, relay.SPAWN_FAILED_ATTR) is True


def test_nul_argv_through_the_real_watchdog_is_marked_no_child(relay):
    """같은 형상이 실 어댑터를 쓰는 워치독 경로에서도 '자식 없음' 표식으로 올라온다."""
    handoffs: list[int] = []
    with pytest.raises(ValueError) as excinfo:
        relay.run_with_first_event_watchdog(
            [sys.executable, "-c", "print(1)\x00"],
            first_event_timeout=None, overall_timeout=30.0, retries=0,
            idle_timeout=None, log=[].append,
            on_spawn_attempt=lambda: handoffs.append(1),
        )
    assert getattr(excinfo.value, relay.SPAWN_FAILED_ATTR) is True
    assert handoffs == [1]      # 경계 콜백은 종전 자리 그대로 — 되돌림은 표식이 소유한다


def test_watched_popen_marks_post_spawn_valueerror_as_spawned(relay, monkeypatch):
    """`Popen` **성공 뒤**의 `ValueError` 는 자식이 있었던 실행이다 → 표식 False.

    NUL 거절과 **같은 종류**의 예외로 반대편을 세운다: 종류로 환불을 정하면 이 형상이 '전송 0'
    으로 오분류돼 과금된 실행이 예산에서 사라진다."""
    class _NoThreads:
        Event = threading.Event
        Lock = threading.Lock

        @staticmethod
        def Thread(*args, **kwargs):        # noqa: N802 — 표준 API 이름을 맞춘다
            raise ValueError("스레드 생성 거부")

    monkeypatch.setattr(relay, "threading", _NoThreads)
    with pytest.raises(ValueError) as excinfo:
        relay._WatchedPopen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert getattr(excinfo.value, relay.SPAWN_FAILED_ATTR) is False


def test_cleanup_failure_after_a_post_spawn_valueerror_keeps_the_spawned_mark(
        relay, monkeypatch):
    """자식 회수(정리)까지 실패해도 원 예외의 표식은 '자식 있었음' 그대로다.

    정리 실패는 잔존 프로세스 가능성을 뜻하는 **더 나쁜** 형상이다. 여기서 표식이 흔들려 환불
    쪽으로 넘어가면 과금 가능성이 가장 큰 실행이 예산을 안 먹는다."""
    class _NoThreads:
        Event = threading.Event
        Lock = threading.Lock

        @staticmethod
        def Thread(*args, **kwargs):        # noqa: N802 — 표준 API 이름을 맞춘다
            raise ValueError("스레드 생성 거부")

    def _cleanup_boom(*args, **kwargs):
        raise relay.ProcessCleanupError("정리 실패(주입)")

    monkeypatch.setattr(relay, "threading", _NoThreads)
    monkeypatch.setattr(relay, "_cleanup_failed_watched_spawn", _cleanup_boom)
    with pytest.raises(ValueError) as excinfo:
        relay._WatchedPopen([sys.executable, "-c", "pass"])
    assert getattr(excinfo.value, relay.SPAWN_FAILED_ATTR) is False
    assert isinstance(excinfo.value.__cause__, relay.ProcessCleanupError)
