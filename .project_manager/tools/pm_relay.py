#!/usr/bin/env python3
"""PM relay — 상태 없는 thin supervisor 세션 자동-회전 (ADR-0009 · 엔진 core).

세션당 200K 한계를 *이음매 없이* 회전해 **연속 PM 운영**을 주는 바깥 루프. supervisor 는
LLM 이 아니라 dumb pipe 인 코드 프로세스다 — user↔PM 메시지를 그냥 지나보내고 컨텍스트를
누적하지 않는다(stateless). 연속성은 **file**(board=작업상태 + ADR-0008 handoff entry)이
담당하고 supervisor 는 무기억으로 회전만 한다.

루프 (run_loop):
  spawn PM(fresh ctx + bootstrap 프롬프트로 file 재유도)
    → (user 입력 ↔ relay_turn) 반복
    → 매 turn 직후 stop_marker_present(sid) 1회 stat
        → marker 있으면: 떠나는 세션은 ctx_stop_hook 이 이미 차단(harvest 안 함) →
           새 sid 로 respawn + **직전(차단된) 입력 재전송** → 계속
  EOF / `/quit` → 종료.

STOP 관측 = ctx_stop_hook 이 박는 marker(`.project_manager/.local/ctx-stop/<sid>.done`).
marker 파일명 예측 = 결정적 `--session-id`(supervisor 가 uuid4 발급 → driver 가 child 에 전달).
hook·pm_handoff·pm_bootstrap 는 **무수정**(읽기만) — supervisor 는 그 marker 를 stat 만 한다.

이 모듈은 **하니스 무관**(claude/opencode 공통). driver 는 SessionDriver Protocol 뒤로 주입
(DI 경계) — 테스트는 FakeDriver, 실 구동은 claude driver(`pm_orch_claude.py`).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Protocol, TextIO

# marker 디렉토리 — ctx_stop_hook.py 의 _MARKER_DIR 와 동일해야 한다(읽기 측·hook 이 쓰는 측).
MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"

# child 에 줄 bootstrap 프롬프트 — 새 PM 이 file(board+handoff)에서 맥락을 재유도하게 유도.
# 맥락 자체를 주입하지 않는다(stateless·file-as-memory) — 새 세션이 직접 읽게 한다.
def build_bootstrap_prompt(task: str | None = None) -> str:
    """재진입 부트스트랩 프롬프트를 빌드한다 — task 명시 시 `/pm-bootstrap --task <name>` 로 주입.

    **relay task 전달 = (b) 명시 전달**(sealed·PM 73·T-0356·F7): supervisor 가 받은 task 정체성을
    재진입 프롬프트에 실값으로 박아, 컨텍스트 한계로 회전된 새 PM 세션이 같은 task 를 재바인딩
    (resume)하게 한다. cwd/env 추론(a)은 기각(F6 "cwd 는 해소에 참여하지 않는다"·결정론 ⓐ "cwd/env
    추론 금지"와 모순·ADR-0057 불변) — 정체성은 per-call 명시 전달이다. task 슬롯 0개 엣지에서도 (b)만
    동작한다. task 없으면(슬롯/솔로) bare `/pm-bootstrap`(현행·byte-동일)."""
    cmd = f"/pm-bootstrap --task {task}" if task else "/pm-bootstrap"
    return (
        "너는 이 프로젝트의 PM 세션이다. 이전 PM 세션이 컨텍스트 한계로 회전됐다. "
        f"먼저 `{cmd}` 을 수행하고 `log/current.md` 의 최신 handoff entry 를 읽어 "
        "직전 세션의 작업을 이어받아라. 준비되면 'READY' 라고만 답하라."
    )


# 기본(task 무·슬롯/솔로) 재진입 프롬프트 — 현행 bare `/pm-bootstrap` 프롬프트와 byte-동일.
BOOTSTRAP_PROMPT = build_bootstrap_prompt()

# 종료 명령 — supervisor 루프를 끝낸다(EOF 와 동치).
QUIT_COMMANDS = frozenset({"/quit", "/exit"})

# 연속 respawn 가드 기본값 — 같은(차단된) 입력을 fresh 세션마다 재전송했는데 매번 즉시
# ctx-STOP 을 유발하면 marker→respawn→재전송→또 STOP 무한 회전(토큰 무한 소모). 진전 없는
# 연속 respawn 이 이 횟수를 넘으면 명시 중단한다. 보수적 기본(드문 병적 케이스 방어용).
MAX_CONSECUTIVE_RESPAWNS = 5

# run_loop 가드 발동 종료 코드 — 정상 종료(0·EOF/quit)와 구분되는 sentinel.
GUARD_TRIPPED_RC = 1


class SessionDriver(Protocol):
    """하니스별 세션 구동 경계 (DI). claude=ClaudeCliDriver, 테스트=FakeDriver.

    supervisor 는 이 Protocol 뒤만 알고 실 claude 호출은 driver 에 갇힌다 →
    단위테스트가 실 subprocess 없이 relay/respawn 로직만 검증할 수 있다.
    """

    def spawn(self, cwd: str, session_id: str, bootstrap: str) -> str:
        """새 세션을 띄운다 — child 에 결정적 session_id 를 부여하고 bootstrap 프롬프트를
        첫 turn 으로 보낸다. 실제 사용된 세션 id 를 반환(보통 입력 session_id 와 같음)."""
        ...

    def relay_turn(self, session_id: str, text: str) -> str:
        """기존 세션을 resume 해 한 turn 중계한다 — text 를 보내고 reply 를 반환."""
        ...

    def close(self, session_id: str) -> None:
        """세션 정리(필요 시). `-p` 1회성 turn 은 자동 exit 라 보통 no-op."""
        ...


def new_session_id() -> str:
    """결정적 marker-matching 용 세션 id 발급(uuid4). supervisor 가 발급 →
    child 에 `--session-id` 로 전달 → marker 파일명 `<uuid>.done` 을 예측한다."""
    return str(uuid.uuid4())


def stop_marker_present(root: Path, session_id: str) -> bool:
    """ctx_stop_hook 이 박은 STOP marker 가 있는지 1회 stat. 폴 스레드 없음(thin)."""
    return _marker_path(root, session_id).exists()


def clear_marker(root: Path, session_id: str) -> bool:
    """떠난 세션의 marker 정리(best-effort). 지웠으면 True. 회전 후 누적 방지용."""
    path = _marker_path(root, session_id)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def parse_stream_json(lines) -> tuple[str | None, str | None]:
    """`claude -p --output-format stream-json` 출력에서 (session_id, result) 추출.

    - session_id: `system/init` 이벤트의 `session_id`(이후 모든 이벤트에도 실리나 init 우선).
      init 가 없으면 `result` 이벤트의 session_id 로 폴백.
    - result: `result` 이벤트의 `result` 필드(= 최종 reply 텍스트).
    - JSONDecodeError 라인은 skip(부분/비-JSON 라인에 robust).

    PoC(`scratch/poc/orchestrator_claude_relay_swap.py`)의 run_turn 파싱 골격을
    순수 함수로 추출 — driver 가 호출하고 테스트가 직접 검증한다.
    """
    import json  # 지연 import — 순수 헬퍼만 쓰는 경로의 import 비용 회피.

    session_id: str | None = None
    result: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue  # 비-JSON / 부분 라인 skip.
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id") or session_id
        if event.get("type") == "result":
            result = event.get("result")
            session_id = session_id or event.get("session_id")
    return session_id, result


def parse_opencode_json(lines) -> tuple[str | None, str | None]:
    """`opencode run --format json` 출력에서 (session_id, reply) 추출.

    claude `parse_stream_json` 과 **대칭** 위치의 opencode 어댑터용 순수 헬퍼 —
    하니스가 다른 한 줄=한 이벤트 JSON 스트림을 같은 (sid, reply) 규격으로 흡수한다.
    opencode driver(`pm_orch_opencode.py`)가 DI 로 주입받아 쓴다(엔진은 파싱만 보유).

    - session_id: 모든 이벤트 top-level `sessionID`(실측 — 매 이벤트에 실린다). 첫 등장값
      을 잡는다(opencode 가 sid 를 발급 — claude 와 달리 사전지정 불가, 출력 파싱으로 획득).
    - reply: `type:"text"` 이벤트의 `part.text` 를 등장 순서대로 누적(멀티-part 답변 대응).
      reply 가 없으면(text part 0) None.
    - 비-JSON / 비-dict 라인은 skip(부분/노이즈 라인에 robust — claude 파서와 동일 정책).

    실측 이벤트 형식(opencode 1.17.6):
      {"type":"text","sessionID":"ses_...","part":{"type":"text","text":"PONG",...}}
    """
    import json  # 지연 import — 순수 헬퍼만 쓰는 경로의 import 비용 회피(claude 파서 대칭).

    session_id: str | None = None
    reply_parts: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue  # 비-JSON / 부분 라인 skip.
        if not isinstance(event, dict):
            continue
        sid = event.get("sessionID")
        if session_id is None and isinstance(sid, str) and sid:
            session_id = sid
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    reply_parts.append(text)
    reply = "".join(reply_parts) if reply_parts else None
    return session_id, reply


# ── opencode 첫-이벤트 stall 워치독 (T-0336 · 하니스-무관 순수 헬퍼·parse_opencode_json 동거) ──
# opencode `run` 이 스타트업 network fetch stall 에 빠지면 `--format json` stdout 이벤트가 0바이트로
# **영원히** 멈춘다(PM 70 라이브 실측·정상 run 은 첫 이벤트 ~0.2–2초·헝 run 은 240초+ 창 지나도 자체
# 회복 없음·upstream #13841 진단 일치). 호출층에서 이를 닫는다: 첫 stdout 이벤트가 N초 내 안 오면
# 프로세스 그룹째 kill → 재시도(M회) → 소진 시 fail-loud. provider/원인 무관(내부 어느 fetch 가
# 멈추든 동작). mid-turn(정상 긴 생성) 침묵은 이번 범위 밖 — overall_timeout(호출부의 기존 hard
# 가드·예: pm_orch_opencode.TURN_TIMEOUT_SEC=600)이 그대로 백스톱. provider 노브(headerTimeout 등)는
# stall 이 provider 스트림 fetch *밖*에서 발생해 무효 실측(PM 70) — 워치독이 클래스를 커버한다.

# env 노브 (worktree_pool 의 PM_GIT_TIMEOUT 네이밍 결). 세 표면(opencode driver·pm_import fill·
# release 라이브 헬퍼)이 아래 두 해소기로 이 기본값을 공유한다. 값을 바꾸려면 export 후 재실행.
FIRST_EVENT_TIMEOUT_ENV = "PM_OC_FIRST_EVENT_TIMEOUT"   # 첫-이벤트 대기 상한(초).
STALL_RETRIES_ENV = "PM_OC_STALL_RETRIES"               # stall 시 재시도 횟수.
# 기본 90초 = 느린 cloud 시작 대비 보수적(정상 첫 이벤트 ~0.2–2초·PM 70). 재시도 기본 2회.
DEFAULT_FIRST_EVENT_TIMEOUT_SEC = 90.0
DEFAULT_STALL_RETRIES = 2
# 워치독 폴 간격·kill 후 grace(자식 잔존 방지·짧게). 매직넘버 회피 상수.
_WATCHDOG_POLL_INTERVAL_SEC = 0.1
_KILL_GRACE_SEC = 5.0


class StallWatchdogError(RuntimeError):
    """첫-이벤트 워치독이 모든 재시도를 소진(반복 startup stall) → fail-loud.

    헬퍼는 raise 만 하고 *정책은 호출부가* 정한다:
      - opencode driver `_turn`: catch → loud stderr + fail-soft turn(None,None·relay 루프 생존).
      - pm_import fill(`_real_harness_runner`): catch → (False, 에러텍스트)(기존 fail-soft 계약).
      - release 라이브 헬퍼: uncatch → 테스트 fail-loud(라이브 환경 문제 가시화).
    """


def _env_positive_float(name: str, default: float) -> float:
    """env 노브를 양수 float 로 해소(빈/불량/비양수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_nonneg_int(name: str, default: int) -> int:
    """env 노브를 음이 아닌 int 로 해소(빈/불량/음수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_positive_int(name: str, default: int) -> int:
    """env 노브를 양의 int 로 해소(빈/불량/비양수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def first_event_timeout_default() -> float:
    """PM_OC_FIRST_EVENT_TIMEOUT env(초) 또는 기본 90. 세 표면 공유 해소기."""
    return _env_positive_float(FIRST_EVENT_TIMEOUT_ENV, DEFAULT_FIRST_EVENT_TIMEOUT_SEC)


def stall_retries_default() -> int:
    """PM_OC_STALL_RETRIES env 또는 기본 2. 세 표면 공유 해소기."""
    return _env_nonneg_int(STALL_RETRIES_ENV, DEFAULT_STALL_RETRIES)


def _kill_process_group(proc) -> None:
    """proc 를 프로세스 그룹째 kill(자식 잔존 방지) + 짧은 grace. 이미 종료면 no-op.

    POSIX: `os.killpg(getpgid, SIGKILL)`(Popen 이 start_new_session 으로 새 그룹장). Windows:
    `proc.kill()`(CREATE_NEW_PROCESS_GROUP). 예외는 삼킨다(best-effort — 이미 죽었을 수 있음)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=_KILL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        pass


class _WatchedPopen:
    """실 subprocess.Popen 을 감싸 첫-stdout-이벤트 관측 + 프로세스그룹 kill 을 제공.

    reader 스레드가 stdout 을 줄단위로 읽어 누적하고 첫 *비어있지-않은* 라인에서 first_event 를
    set 한다(startup stall = 첫 라인조차 영원히 안 옴). stall 시 메인 루프가 kill 하면 reader 의
    blocking readline 이 EOF 로 풀린다. stderr 도 별도 스레드로 드레인(파이프 버퍼 데드락 방지).
    fake(테스트)로 대체 가능한 얇은 어댑터 — 워치독 로직은 run_with_first_event_watchdog 이 쥔다."""

    def __init__(self, argv, *, cwd=None, env=None, text=True):
        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
        )
        if text:
            popen_kwargs.update(text=True, encoding="utf-8", errors="replace")
        # 프로세스 그룹 분리 — kill 시 자식(모델 fetch 서브프로세스 등)까지 그룹째 정리.
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover — POSIX 회귀 환경
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(argv, **popen_kwargs)
        self._first_event = threading.Event()
        self._stdout_chunks: list[str] = []
        self._stderr_chunks: list[str] = []
        self._stdout_reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_reader.start()
        self._stderr_reader.start()

    def _pump_stdout(self) -> None:
        stream = self._proc.stdout
        if stream is None:
            self._first_event.set()
            return
        try:
            for line in iter(stream.readline, ""):
                self._stdout_chunks.append(line)
                if line.strip():
                    self._first_event.set()
        except (ValueError, OSError):
            pass
        finally:
            self._first_event.set()  # EOF(빈 출력 포함) — 대기 루프를 풀어준다.

    def _pump_stderr(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                self._stderr_chunks.append(line)
        except (ValueError, OSError):
            pass

    def first_event_ready(self) -> bool:
        return self._first_event.is_set()

    def poll(self):
        return self._proc.poll()

    def kill(self) -> None:
        _kill_process_group(self._proc)

    def communicate(self, timeout=None):
        """프로세스 종료를 기다려 (stdout, stderr) 누적을 반환. timeout 초과 시 TimeoutExpired."""
        self._proc.wait(timeout=timeout)  # TimeoutExpired 를 그대로 전파(overall 백스톱).
        self._stdout_reader.join(timeout=_KILL_GRACE_SEC)
        self._stderr_reader.join(timeout=_KILL_GRACE_SEC)
        return "".join(self._stdout_chunks), "".join(self._stderr_chunks)

    @property
    def returncode(self):
        return self._proc.returncode


def _default_watchdog_log(message: str) -> None:
    """워치독 loud 1줄 sink(기본 stderr — stdout 은 PM 대화 채널 보존)."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def run_with_first_event_watchdog(
    argv,
    *,
    first_event_timeout: float,
    overall_timeout: float,
    retries: int,
    cwd=None,
    env=None,
    text: bool = True,
    popen=None,
    clock=None,
    sleep=None,
    log=None,
    poll_interval: float = _WATCHDOG_POLL_INTERVAL_SEC,
):
    """argv 를 첫-이벤트 워치독으로 실행 — startup stall 을 유한 재시도로 닫는다 (T-0336).

    각 시도: 프로세스 시작 → stdout 첫 이벤트를 first_event_timeout 초 내 관측하는지 감시.
      - 관측(또는 첫 이벤트 없이 빠른 종료) → 완료까지 드레인(overall_timeout 백스톱) 후
        subprocess.CompletedProcess(returncode·stdout·stderr) 반환.
      - 미관측(stall) → 프로세스 그룹째 kill·loud 1줄·다음 시도.
    모든 시도(= retries+1) 소진 → StallWatchdogError(fail-loud·호출부가 정책 결정).

    DI seam(hermetic 테스트·바이너리 불요):
      popen : argv -> proc(first_event_ready()/poll()/kill()/communicate(timeout)/returncode).
              기본 = _WatchedPopen(cwd/env/text 바인딩).
      clock : () -> 초(단조). 기본 time.monotonic.
      sleep : (초) -> None. 기본 time.sleep(폴 간격 양보). fake 는 여기서 clock 을 전진시킨다.
      log   : (str) -> None. 기본 stderr 1줄.
    overall_timeout 은 호출부의 기존 hard 가드(예: TURN_TIMEOUT_SEC=600)를 그대로 받아 mid-turn
    침묵의 백스톱으로 쓴다 — 워치독은 그 *안쪽*에 startup 첫-이벤트 감시를 더한다.
    """
    if popen is None:
        def popen(_argv):  # 기본 실 Popen 어댑터(cwd/env/text 클로저).
            return _WatchedPopen(_argv, cwd=cwd, env=env, text=text)
    clock = clock if clock is not None else time.monotonic
    sleep = sleep if sleep is not None else time.sleep
    log = log if log is not None else _default_watchdog_log

    attempts = retries + 1  # retries=재시도 횟수 → 총 시도 = retries+1(최초 1 + 재시도 M).
    last_reason = ""
    for attempt in range(1, attempts + 1):
        proc = popen(argv)
        start = clock()
        first_deadline = start + first_event_timeout
        overall_deadline = start + overall_timeout
        stalled = False
        while True:
            if proc.first_event_ready():
                break  # 첫 이벤트 관측 → 드레인 단계로.
            if proc.poll() is not None:
                break  # 첫 이벤트 없이 종료(빠른 exit·에러) → 드레인이 결과 수습.
            now = clock()
            if now >= first_deadline or now >= overall_deadline:
                stalled = True  # 첫-이벤트 창 초과(또는 극단적 overall 초과) = startup stall.
                break
            sleep(poll_interval)
        if stalled:
            proc.kill()
            last_reason = f"{first_event_timeout:.0f}s 무이벤트"
            log(
                f"[pm-orch] stall watchdog: {last_reason} kill·재시도 {attempt}/{attempts}"
            )
            continue
        remaining = overall_deadline - clock()
        if remaining < 0:
            remaining = 0.0
        try:
            stdout, stderr = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise  # overall(mid-turn) 백스톱 — 기존 TimeoutExpired 계약을 그대로 유지.
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

    raise StallWatchdogError(
        f"opencode 첫-이벤트 stall 이 {attempts}회 연속 발생({last_reason}) — 재시도 소진. "
        "startup network fetch stall 의심(PM 70·upstream #13841)."
    )


# ── opencode 출력 cap-hit(32k 절단) detector (T-0339 · 하니스-무관 순수 헬퍼) ──────────────────
# opencode 는 outbound 응답이 출력 cap(32000 토큰·`opencode.jsonc` 실효 = min(limit.output,32000))을
# 넘으면 응답을 **조용히 절단** 하고 finish 를 "stop" 으로 위장한다(T-0334 라이브 확증) — 수신자(PM)는
# 절단을 감지하지 못한다. T-0337 파일-전달 규약이 우회책이나, *절단이 실제로 일어났는지* 알 장치가
# 없으면 우회 실패가 조용히 지나간다. 이 detector 가 출력 소비 지점(Supervisor.run_loop)에서 응답이
# cap 근방인지 보고 loud advisory 를 낸다. **advisory·never-block** — 경고+로그만·파이프라인 무중단
# (오탐이 relay 를 죽이면 안 됨). claude 는 범위 밖: claude 는 truncation 을 stop_reason=max_tokens 로
# *네이티브 노출* 하므로 silent 절단 클래스가 아니다(T-0334 는 opencode 한정 실측) — run_loop 배선은
# 하니스-무관 크기 advisory 라 claude 응답도 지나가나 임계가 정상 응답보다 한참 위라 무영향.

CAP_TOKENS = 32000                   # opencode 실효 출력 cap(T-0334·opencode.jsonc limit.output).
# 정확 토크나이저는 쓰지 않는다(무거운 의존·하니스-무관 유지·ticket §결정). char 수를 보수적 token
# 근사로 환산한다: char↔token 비는 내용마다 다르나(영어/코드 ≈3.5–4·한글 ≈1.5–2.5 char/token), 순수
# 한글은 실 토크나이저에서 그보다 dense(<1.5)일 수 있어 32000~43200자 절단을 놓칠 위험이 있다. 그
# 한글-dominant false-negative 창을 닫기 위해 char 길이 하한을 **극단 dense CJK(~1.2 char/token)**
# 기준으로 보수 하향한다 — genuine 32k-token 응답은 이 밀도에서도 ≈38400 char 다.
CAP_HIT_MIN_CHARS_PER_TOKEN = 1.2    # char/token 보수적 하한(극단 dense CJK 기준·상한 token 근사).
CAP_HIT_RATIO = 0.90                 # cap 근방 밴드 = char 하한의 90% 이상이면 절단 의심.
# char 임계 = 32000 × 1.2 × 0.90 = 34560 char. 이 값은 (a) 내용 혼합 전반의 genuine 절단 char 길이
# (영어 128k·코드 112k·혼합 80k·한글 48k·극단 dense CJK 38k) *아래* 라 절단을 놓치지 않고
# (false-negative 회피 — 순수 한글 dense 창 포함), (b) 정상 PM relay 응답(수백~수 KB·verbose handoff
# ~15KB)보다 여전히 >2x 위라 오탐 0(false-positive 회피). 두 경계 폭이 넓어 단일 char 임계로 양쪽을
# 만족한다(정확 token 수 불필요·advisory 목적엔 절단 *의심* 표면화면 충분).
CAP_HIT_CHAR_THRESHOLD = int(CAP_TOKENS * CAP_HIT_MIN_CHARS_PER_TOKEN * CAP_HIT_RATIO)  # 34560
CAP_HIT_THRESHOLD_ENV = "PM_OC_CAP_HIT_THRESHOLD"  # char 임계 override(필요 시만·양의 정수).


def cap_hit_char_threshold_default() -> int:
    """PM_OC_CAP_HIT_THRESHOLD env(양의 char 수) 또는 기본 34560. cap-hit 감지 char 임계 해소기."""
    return _env_positive_int(CAP_HIT_THRESHOLD_ENV, CAP_HIT_CHAR_THRESHOLD)


def detect_output_cap_hit(text, *, char_threshold: int | None = None,
                          cap_tokens: int = CAP_TOKENS) -> tuple[bool, str]:
    """outbound 응답이 opencode 출력 cap(≈32k 토큰) 근방인지 감지 — silent 절단 의심 (T-0339).

    반환 (hit, reason). hit=True 면 응답이 cap-hit 임계(char) 이상이라 절단 의심 → 호출부가 loud
    advisory 를 낸다(never-block). hit=False 면 reason="". 정확 token 수는 근사(보수적 상한) —
    char↔token 비가 내용마다 달라 exact 는 못 주나 절단 *의심* 표면화엔 충분(advisory 목적).

    char_threshold=None 이면 env 노브(PM_OC_CAP_HIT_THRESHOLD) 또는 기본을 쓴다. text 가 비거나
    None 이면 무발화(정상 크기 = 무조건 통과·오탐 0 보장의 하한 경로).
    """
    if char_threshold is None:
        char_threshold = cap_hit_char_threshold_default()
    length = len(text) if text else 0
    if length < char_threshold:
        return False, ""
    approx_tokens = int(length / CAP_HIT_MIN_CHARS_PER_TOKEN)  # 보수적 상한 근사(token).
    reason = (
        f"응답 {length} 자(≈{approx_tokens} tok 보수적 근사) ≥ 임계 {char_threshold} 자 — "
        f"opencode 출력 cap {cap_tokens} tok 근방"
    )
    return True, reason


def cap_hit_warning_message(reason: str) -> str:
    """cap-hit loud advisory 1줄 — T-0337 파일-전달 규약 안내 포함(경고 문구 요구·ticket §결정).

    run_loop 배선은 하니스-무관이라 claude 대형 응답에도 발화할 수 있다 — opencode 한정으로 단정하지
    않고 **조건부**('opencode 하니스라면')로 문구를 중립화해 오해 소지를 없앤다(claude 는 truncation 을
    stop_reason 으로 네이티브 노출하므로 이 silent-절단 클래스가 아님·§메모). 규약 안내는 유지한다.
    stdout(=PM 대화 채널)은 오염하지 않는다 — 호출부가 이 문자열을 stderr/log 로 낸다."""
    return (
        f"[pm-orch] ⚠ 출력 상한(32k tok) 근방: {reason}. **opencode 하니스라면** 이 응답이 silent "
        "절단됐을 가능성이 있다 — opencode 는 32k 출력 토큰에서 응답을 조용히 자르고 finish 를 'stop' "
        "으로 위장한다(T-0334·수신자 감지 불가). 잘렸다면 파일-전달 규약(T-0337)으로 재시도하라: 대형 "
        "산출물은 파일로 쓰고(opencode 는 safe_write 8KB 청크·write 는 16KB 초과 거부), 응답엔 절대경로 "
        "+ 핵심 요약 ≤10줄만 반환."
    )


def _sanitize_session_id(session_id: str) -> str:
    """marker 파일명 안전화 — ctx_stop_hook._session_id 와 동일 규칙(파일명 안전 문자만·64자).

    hook 이 child 의 session_id 를 이 규칙으로 sanitize 해 marker 를 쓰므로, marker 경로를
    *예측* 하려면 supervisor 도 같은 변환을 적용해야 한다(uuid4 는 본래 안전하나 방어적 일치)."""
    safe = "".join(c for c in session_id.strip() if c.isalnum() or c in "-_")[:64]
    return safe or "unknown"


def _marker_path(root: Path, session_id: str) -> Path:
    return root / MARKER_DIR / f"{_sanitize_session_id(session_id)}.done"


class Supervisor:
    """상태 없는 thin supervisor (ADR-0009).

    **stateless 불변식**: 인스턴스 상태는 *주입된 협력자*(driver)와 *고정 config*(root·
    marker_dir)뿐 — 대화/작업 상태 필드는 0. user↔PM 메시지는 누적하지 않고 지나보낸다
    (직전 입력은 run_loop *지역 변수* 로만 들고 가는 transient 1-turn 버퍼). 연속성은 file.
    """

    def __init__(self, driver: SessionDriver, *, root: Path,
                 bootstrap: str | None = None, task: str | None = None,
                 max_consecutive_respawns: int = MAX_CONSECUTIVE_RESPAWNS) -> None:
        # 협력자·고정 config 만 — 대화/작업 상태 필드 없음(stateless 단언의 근거).
        # max_consecutive_respawns 는 *config* 상수(불변 임계)지 작업/대화 상태가 아니다.
        self.driver = driver
        self.root = Path(root)
        # task 정체성(F7·T-0356·(b) 명시 전달)은 재진입 프롬프트에 baked-in 되어 `self.bootstrap` 에
        # 흡수된다 — 별도 인스턴스 필드로 retain 하지 않는다(stateless 불변식 유지·respawn 은 같은
        # bootstrap 을 재사용해 task 를 자동 forward). bootstrap 명시 override(테스트/커스텀)가 우선,
        # 없으면 task 로 빌드(task None 이면 현행 bare BOOTSTRAP_PROMPT 와 byte-동일).
        self.bootstrap = bootstrap if bootstrap is not None else build_bootstrap_prompt(task)
        self.max_consecutive_respawns = max_consecutive_respawns

    def stop_marker_present(self, session_id: str) -> bool:
        return stop_marker_present(self.root, session_id)

    def run_loop(self, cwd: str, in_stream: TextIO, out_stream: TextIO,
                 cap_hit_log=None) -> int:
        """바깥 루프 — spawn → relay → STOP 감지 → respawn(+직전 입력 재전송) → repeat.

        - in_stream: 사용자 입력 라인 소스(stdin·테스트는 StringIO).
        - out_stream: PM reply 출력 sink(stdout·테스트는 StringIO).
        - cap_hit_log: cap-hit(32k 절단 의심) loud advisory sink(기본 stderr·테스트는 list.append).
          출력 소비 지점에서 응답이 cap 근방이면 경고만 낸다(never-block·stdout 무오염·T-0339).
        - 반환 = exit code(0=정상 종료 EOF/quit · GUARD_TRIPPED_RC=연속 respawn 가드 발동).

        직전 입력 재전송: STOP 을 유발한(차단된) turn 의 사용자 입력을 `pending` 지역 변수에
        들고 respawn 후 새 PM 에 재전송한다(in-flight 의도 보존). transient 1-turn 버퍼라
        컨텍스트 누적이 아니다 — stateless 불변식 유지.

        연속 respawn 가드: 한 입력이 fresh 세션마다 *즉시* ctx-STOP 을 유발하면 respawn→재전송
        →또 STOP 무한 회전(토큰 무한 소모). **같은(차단된) 입력을 재전송했는데 또 respawn** 한
        횟수를 `consecutive_respawns` 지역 카운터로 센다 — 정상 turn(사용자 새 입력을 소비한
        turn)이 한 번이라도 끼면 0 리셋. 카운터가 max 초과면 진단 1줄 쓰고 종료(병적 케이스만
        발동·정상 회전은 영향 0). 카운터는 *지역 변수* — 인스턴스 상태 아님(stateless 유지).
        """
        cap_hit_log = cap_hit_log if cap_hit_log is not None else _default_watchdog_log
        session_id = self._spawn(cwd, out_stream)
        pending: str | None = None  # respawn 후 재전송할 직전(차단된) 입력.
        consecutive_respawns = 0    # 같은 입력 재전송이 연속 STOP 한 횟수(지역·병적 케이스 감지).

        while True:
            if pending is not None:
                text, pending = pending, None  # 재전송 — 사용자 새 입력을 읽지 않는다.
                is_resend = True
            else:
                line = in_stream.readline()
                if line == "":  # EOF.
                    break
                text = line.rstrip("\n")
                if text.strip() in QUIT_COMMANDS:
                    break
                if text.strip() == "":
                    continue
                is_resend = False

            reply = self.driver.relay_turn(session_id, text)
            if reply is not None:
                out_stream.write(reply + "\n")
                out_stream.flush()
                # 출력 소비 지점 — 응답이 출력 상한(32k tok) 근방이면 silent 절단 의심 loud advisory
                # (never-block·stdout 은 이미 위에서 그대로 전달·경고는 별도 sink·T-0339). advisory 는
                # relay 를 절대 못 죽인다 — detect/message/sink write 전 경로를 try/except 로 감싼다
                # (병적 sink·근사 예외가 파이프라인을 중단시키면 안 됨·never-block 을 코드로 못박음).
                try:
                    cap_hit, cap_reason = detect_output_cap_hit(reply)
                    if cap_hit:
                        cap_hit_log(cap_hit_warning_message(cap_reason))
                except Exception:  # noqa: BLE001 — advisory 는 어떤 이유로도 relay 를 막지 않는다.
                    pass

            # 매 turn 직후 1회 stat — marker 있으면 떠나는 세션은 hook 이 이미 차단됨
            # (harvest 안 함) → 회전. 이 turn 의 입력은 차단됐을 수 있으니 새 PM 에 재전송.
            if self.stop_marker_present(session_id):
                # 카운터는 "같은 입력의 연속 재전송-STOP" 횟수만 센다. 매 resend-chain 은 *fresh
                # 입력의 STOP*(is_resend=False)으로 시작하고 그 분기가 0 으로 리셋하므로(아래),
                # trip 판정에 닿는 리셋은 **이 line(255)** 이 담당한다 — 새 chain 은 항상 0 부터.
                # (정상 회전: 긴 작업→자연 STOP→다음 *새* 입력 = 여기서 리셋되어 병적 아님.)
                if is_resend:
                    consecutive_respawns += 1
                else:
                    consecutive_respawns = 0
                if consecutive_respawns > self.max_consecutive_respawns:
                    out_stream.write(
                        f"[relay] 같은 입력이 연속 {consecutive_respawns}회 즉시 "
                        f"ctx-STOP 을 유발 — 무한 respawn 회전 차단(max="
                        f"{self.max_consecutive_respawns}). 종료. 입력 크기·ctx 임계 점검.\n"
                    )
                    out_stream.flush()
                    self.driver.close(session_id)
                    clear_marker(self.root, session_id)
                    return GUARD_TRIPPED_RC
                pending = text
                session_id = self._respawn(cwd, session_id, out_stream)
            else:
                # 이 turn 이 respawn 없이 끝났다 = 진전(성공 turn). 카운터를 0 으로 pin 해 둔다 —
                # 카운터가 실제 상태를 반영하게 유지하는 정돈용(tidy). trip 판정엔 redundant 다:
                # 다음 resend-chain 은 어차피 fresh-STOP(line 255)이 0 으로 리셋하고 시작하므로,
                # 이 줄이 없어도 동작은 동일하다(reviewer sensitivity 확인). 명시성 위해 보존.
                consecutive_respawns = 0

        self.driver.close(session_id)
        return 0

    # ── 내부 회전 헬퍼 (상태 없음 — 인자만으로 동작) ───────────────────────────

    def _spawn(self, cwd: str, out_stream: TextIO) -> str:
        """결정적 session_id 발급 → driver.spawn(bootstrap). 실 사용 sid 반환."""
        session_id = new_session_id()
        return self.driver.spawn(cwd, session_id, self.bootstrap)

    def _respawn(self, cwd: str, old_session_id: str, out_stream: TextIO) -> str:
        """떠나는 세션 정리 후 새 세션 spawn. 새 sid 반환."""
        self.driver.close(old_session_id)
        clear_marker(self.root, old_session_id)
        return self._spawn(cwd, out_stream)
