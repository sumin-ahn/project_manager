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

import re
import uuid
from pathlib import Path
from typing import Protocol, TextIO

# marker 디렉토리 — ctx_stop_hook.py 의 _MARKER_DIR 와 동일해야 한다(읽기 측·hook 이 쓰는 측).
MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"

# child 에 줄 bootstrap 프롬프트 — 새 PM 이 file(board+handoff)에서 맥락을 재유도하게 유도.
# 맥락 자체를 주입하지 않는다(stateless·file-as-memory) — 새 세션이 직접 읽게 한다.
BOOTSTRAP_PROMPT = (
    "너는 이 프로젝트의 PM 세션이다. 이전 PM 세션이 컨텍스트 한계로 회전됐다. "
    "먼저 `/pm-bootstrap` 을 수행하고 `log/current.md` 의 최신 handoff entry 를 읽어 "
    "직전 세션의 작업을 이어받아라. 준비되면 'READY' 라고만 답하라."
)

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
    하니스가 다른 한 줄=한 이벤트 JSON 스트림을 같은 (sid, reply) 계약으로 흡수한다.
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
                 bootstrap: str = BOOTSTRAP_PROMPT,
                 max_consecutive_respawns: int = MAX_CONSECUTIVE_RESPAWNS) -> None:
        # 협력자·고정 config 만 — 대화/작업 상태 필드 없음(stateless 단언의 근거).
        # max_consecutive_respawns 는 *config* 상수(불변 임계)지 작업/대화 상태가 아니다.
        self.driver = driver
        self.root = Path(root)
        self.bootstrap = bootstrap
        self.max_consecutive_respawns = max_consecutive_respawns

    def stop_marker_present(self, session_id: str) -> bool:
        return stop_marker_present(self.root, session_id)

    def run_loop(self, cwd: str, in_stream: TextIO, out_stream: TextIO) -> int:
        """바깥 루프 — spawn → relay → STOP 감지 → respawn(+직전 입력 재전송) → repeat.

        - in_stream: 사용자 입력 라인 소스(stdin·테스트는 StringIO).
        - out_stream: PM reply 출력 sink(stdout·테스트는 StringIO).
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
