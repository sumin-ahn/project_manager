"""PM relay — 상태 없는 thin supervisor 세션 자동 회전 (층: app).

세션당 컨텍스트 한계를 이음매 없이 회전해 연속 PM 운영을 준다. supervisor 는 user↔PM 메시지를 지나보내고
컨텍스트를 누적하지 않는다. 연속성은 파일(board)이 맡는다. 회전 관측은 ctx 가드가 박는 marker
(`.project_manager/.local/ctx-stop/<sid>.done`)다. 이 모듈은 하네스 무관이고 driver 는 `SessionDriver` 뒤로
주입한다. 하네스 CLI 계약은 `pm_engine.infra.harness.contract`, 워치독은 `pm_engine.infra.harness.watchdog` 이 소유한다.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import NamedTuple, Protocol, TextIO

from pm_engine.infra import conf_store, fs
from pm_engine.infra.harness import watchdog


def ctx_budget(root, harness, session_model):
    """relay·훅이 쓰는 (적용 한도 E, 압축 임박 경고 %, 모델 불일치 경고 또는 None).

    압축 한도 기록 부재·손상·입력 불일치는 `conf_schema.CtxBudgetError` 로 올린다(기본값 대체 없음).
    회전 임계가 bootstrap 상한 이하면 `validate_relay_budget` 이 멈춘다."""
    values = conf_store.load_checked(Path(root) / ".project_manager" / "local.conf")
    budget = conf_store.read_ctx_budget(root, values, harness, session_model)
    validate_relay_budget(budget["E"], budget["precompact_at_pct"])
    return budget["E"], budget["precompact_at_pct"], budget["warning"]


# marker 디렉토리 — ctx_stop_hook.py 의 _MARKER_DIR 와 동일해야 한다(읽기 측·hook 이 쓰는 측).
MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"

# 이 marker 디렉토리는 여러 생산자가 공유한다 — `<세션>.<종류>` 자리를 남이 이미 쓰고 있으면
# 멤버십 파일이 그 신호를 *위조* 한다(namespace "done" → `<sid>.done` = post-turn 회전 신호).
#   done                                  · 이 파일 `_marker_path` (회전)
#   nudge · nudge2 · final                · ctx_stop_hook.py:51,83,87 (사이클 넛지)
#   compact-* (checkpoint·boundary·snapshot·snapshot-receipt)
#                                         · infra.compaction_store._compaction_marker_path · opencode ctx-guard-core.cjs:315,323
RESERVED_MARKER_NAMESPACES = frozenset({"done", "nudge", "nudge2", "final"})
RESERVED_MARKER_NAMESPACE_PREFIX = "compact-"


def _runtime_skill_entry(skill: str) -> str:
    """현재 실행 하네스의 사용자 호출 표기(Codex env marker 외 slash)."""
    prefix = "$" if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI") else "/"
    return f"{prefix}{skill}"


# child 에 줄 bootstrap 프롬프트 — 새 PM 이 file(board+handoff)에서 맥락을 재유도하게 유도.
# 맥락 자체를 주입하지 않는다(stateless·file-as-memory) — 새 세션이 직접 읽게 한다.
def build_bootstrap_prompt(task: str, *, entry: str | None = None) -> str:
    """재진입 부트스트랩 프롬프트를 빌드한다 — `/pm-bootstrap --task <name>` 로 주입.

    **relay task 전달 명시 전달**: supervisor 가 받은 task 정체성을
    재진입 프롬프트에 실값으로 박아, 컨텍스트 한계로 회전된 새 PM 세션이 같은 task 를 재바인딩
    (resume)하게 한다. cwd/env 추론은 기각("cwd 는 해소에 참여하지 않는다"·"cwd/env
    추론 금지"와 모순) — 정체성은 per-call 명시 전달이다. 부트스트랩은 `--task` 필수라
    task 없는 프롬프트는 만들지 않는다(빈 task 는 `ValueError`)."""
    if not task:
        raise ValueError("재진입 프롬프트에는 task 가 필요하다(부트스트랩은 --task 필수)")
    cmd = f"{entry or _runtime_skill_entry('pm-bootstrap')} --task {task}"
    return (
        "너는 이 프로젝트의 PM 세션이다. 이전 PM 세션이 컨텍스트 한계로 회전됐다. "
        f"먼저 `{cmd}` 을 수행하고 `log/current.md` 의 최신 handoff entry 를 읽어 "
        "직전 세션의 작업을 이어받아라. 준비되면 'READY' 라고만 답하라."
    )


# 종료 명령 — supervisor 루프를 끝낸다(EOF 와 동치).
QUIT_COMMANDS = frozenset({"/quit", "/exit"})

# 연속 respawn 가드 기본값 — bootstrap turn 만으로 marker 가 박히면
# marker→respawn→또 marker 무한 회전(토큰 무한 소모). 연속 즉시-회전이 이 횟수를
# 넘으면 명시 중단한다. 보수적 기본(드문 병적 케이스 방어용).
MAX_CONSECUTIVE_RESPAWNS = 5

# run_loop 가드 발동 종료 코드 — 정상 종료(0·EOF/quit)와 구분되는 sentinel.
GUARD_TRIPPED_RC = 1

# 격리 스모크에서 관측한 relay bootstrap turn 실측 상한(31.8k~40.4k tok).
RELAY_BOOTSTRAP_COST_TOKENS = 40_400


def validate_relay_budget(budget: int, precompact_at_pct: int) -> None:
    """relay ctx 예산의 유효 회전 임계가 bootstrap 상한보다 큰지 검증한다.

    회전 임계 ``budget × precompact_at_pct / 100``(압축 임박 경고 = 쓴 %)이 실측 bootstrap 상한을
    엄격히 초과해야 한다.
    """
    effective_threshold_x100 = budget * precompact_at_pct
    if effective_threshold_x100 <= RELAY_BOOTSTRAP_COST_TOKENS * 100:
        effective_threshold = effective_threshold_x100 / 100
        raise ValueError(
            f"[relay] ctx 예산 {budget} tok·압축 임박 경고 {precompact_at_pct}%의 유효 회전 임계 "
            f"{effective_threshold:g} tok은 bootstrap 실측 상한 "
            f"{RELAY_BOOTSTRAP_COST_TOKENS} tok 이하여서 즉시 세션 회전 위험. "
            "적용 한도는 사용 한도(`harness.<harness>.ctx_window_tokens`)와 모델 가용 한도 중 작은 값입니다. "
            "더 큰 모델을 쓰거나 `ctx.precompact_at_pct` 를 높인 뒤 `pm_config.py apply` 를 다시 실행하십시오."
        )


class SpawnResult(NamedTuple):
    """spawn/bootstrap turn 결과 — 권위 session id와 사용자에게 전달할 reply."""

    session_id: str
    reply: str | None


class SessionDriver(Protocol):
    """하니스별 세션 구동 경계 (DI). claude=ClaudeCliDriver, 테스트=FakeDriver.

    supervisor 는 이 Protocol 뒤만 알고 실 claude 호출은 driver 에 갇힌다 →
    단위테스트가 실 subprocess 없이 relay/respawn 로직만 검증할 수 있다.
    """

    def spawn(self, cwd: str, session_id: str, bootstrap: str) -> str | SpawnResult:
        """새 세션을 띄운다 — child 에 결정적 session_id 를 부여하고 bootstrap 프롬프트를
        첫 turn 으로 보낸다. 실제 사용된 session id와 spawn turn reply 를
        ``SpawnResult`` 로 반환한다. 문자열 sid 반환은 driver 전환 기간의 하위호환."""
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
    """ctx 가드가 박은 회전 marker가 있는지 1회 stat. 폴 스레드 없음(thin)."""
    return _marker_path(root, session_id).exists()


def clear_marker(root: Path, session_id: str) -> bool:
    """떠난 세션의 marker 정리(best-effort). 지웠으면 True. 회전 후 누적 방지용."""
    path = _marker_path(root, session_id)
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ── post-turn marker 계약 ────────────────────────────────────────
# marker 파일 존재 = "이 세션을 다음 입력 전에 회전하라" 는 단일 post-turn 신호.
# payload는 진단용일 뿐 소비자는 존재만 판정한다.
# marker 생산자는 turn 완료 후에 신호를 박제한다.
# Supervisor 는 payload 를 판독하지 않는다.
def write_post_turn_marker(root: Path, session_id: str) -> bool:
    """post-turn 회전 marker 박제(best-effort).

    파일명/경로는 호환 유지하며 Supervisor는 payload를 판독하지
    않고 존재만을 post-turn 신호로 소비한다. 실패는 fail-soft(relay 무중단)."""
    path = _marker_path(root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "post-turn rotation requested\n", encoding="utf-8", newline="\n"
        )
        return True
    except OSError:
        return False


def mark_ctx_post_turn_if_over(
    root: Path,
    session_id: str,
    used_tokens: int,
    budget: int,
    precompact_at_pct: int,
    mark=write_post_turn_marker,
) -> bool:
    """turn 사용량이 회전 임계에 닿았으면 post-turn marker 를 박제한다.

    임계는 ``budget * precompact_at_pct / 100``(압축 임박 경고 = 쓴 %)이며 경계값을 포함한다.
    ``used_tokens <= 0`` 또는 ``budget <= 0`` 은 신호 미주입으로 보아 no-op(False).
    precompact_at_pct 범위 판정은 호출부 책임이다.
    marker writer 가 성공한 경우에만 True 를 반환한다.
    """
    if used_tokens <= 0 or budget <= 0:
        return False
    if used_tokens * 100 < budget * precompact_at_pct:
        return False
    return bool(mark(root, session_id))


def _sanitize_session_id(session_id: str) -> str:
    """marker 파일명 안전화 — ctx_stop_hook._session_id 와 동일 규칙(파일명 안전 문자만·64자).

    hook 이 child 의 session_id 를 이 규칙으로 sanitize 해 marker 를 쓰므로, marker 경로를
    *예측* 하려면 supervisor 도 같은 변환을 적용해야 한다(uuid4 는 본래 안전하나 방어적 일치)."""
    safe = "".join(c for c in session_id.strip() if c.isalnum() or c in "-_")[:64]
    return safe or "unknown"


def _marker_path(root: Path, session_id: str) -> Path:
    return root / MARKER_DIR / f"{_sanitize_session_id(session_id)}.done"


def _session_once_path(root: Path, session_id: str, namespace: str) -> Path:
    """세션·namespace 당 멤버십 파일 **1개** 경로.

    키마다 파일을 만들지 않는다 — 이 디렉토리를 일괄 정리하는 코드가 없어서
    (`_rearm_cycle` 은 nudge 3종만 unlink) 키별 파일은 세션당 수백 개가 영구 잔존한다.
    marker 디렉토리도 새로 만들지 않고 기존 `MARKER_DIR` 을 그대로 쓴다
    (`.project_manager/.gitignore` 의 `.local/` 커버를 상속 — 채택자 `git add -A` 안전).
    namespace 도 세션키와 같은 파일명 안전화를 거쳐 traversal 표기를 경로로 만들지 않는다.
    """
    safe_session = _sanitize_session_id(session_id if isinstance(session_id, str) else "")
    safe_namespace = _sanitize_session_id(namespace if isinstance(namespace, str) else "")
    return root / MARKER_DIR / f"{safe_session}.{safe_namespace}"


def claim_session_once(
    root: Path, session_id: str, namespace: str, key: str,
) -> bool | None:
    """``(세션, namespace)`` 스코프에서 ``key`` 를 처음 보는지 선점한다.

    반환은 3상태다(`infra.compaction_store.claim_compaction_checkpoint` 와 동형) — ``True`` 는 이번 호출이
    첫 선점, ``False`` 는 같은 세션·namespace 에서 이미 본 키, ``None`` 은 **장부를 쓸 수 없음**
    (멤버십 파일 I/O 실패 또는 예약 marker 이름 요청)이다. 호출부가 ``None`` 을 자기 fail
    방향으로 해석한다(git-anchor 는 fail-open=발화).

    ``namespace`` 가 `RESERVED_MARKER_NAMESPACES`(또는 `compact-` 계열)면 멤버십 파일이 같은
    디렉토리의 다른 생산자 신호를 위조하므로 **선점하지 않고** ``None`` 을 돌린다 — 예외를 던지지
    않는 이유는 이 헬퍼의 소비자가 전부 훅 경로라 raise 가 곧 훅 파손이고, 3상태 계약 안에서
    "장부 사용 불가"로 표현하면 모든 호출부가 이미 가진 fail 방향을 그대로 타기 때문이다.

    ``key`` 는 불투명한 한 줄 토큰이며 의미(해시·문구)는 호출부가 정한다 — 여기서는 공백류를
    한 칸으로 접어 한 줄 멤버십을 보장하는 것 외에 해석하지 않는다.

    append 는 프로세스 간 원자적이지 않아 경합 시 같은 키가 두 줄 남을 수 있는데, 그 결과는
    "한 번 더 선점 성공(=발화)" 방향이라 fail-open 과 같다.
    """
    safe_namespace = _sanitize_session_id(namespace if isinstance(namespace, str) else "")
    if (safe_namespace in RESERVED_MARKER_NAMESPACES
            or safe_namespace.startswith(RESERVED_MARKER_NAMESPACE_PREFIX)):
        return None  # 남의 marker 이름 — 장부를 만들지 않는다(파일도 디렉토리도 미생성).
    path = _session_once_path(root, session_id, safe_namespace)
    entry = " ".join(str(key).split()) or "unknown"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            seen = fs.read_bytes_shared(path).decode(
                "utf-8", errors="replace",
            ).splitlines()
        except FileNotFoundError:
            seen = []
        if entry in seen:
            return False
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{entry}\n")
    except OSError:
        return None
    return True


class Supervisor:
    """상태 없는 thin supervisor.

    **stateless 불변식**: 인스턴스 상태는 *주입된 협력자*(driver)와 *고정 config*(root·
    marker_dir)뿐 — 대화/작업 상태 필드는 0. user↔PM 메시지는 누적하지 않고 지나보낸다
    (입력 반복 버퍼 없음). 연속성은 file.
    """

    def __init__(self, driver: SessionDriver, *, root: Path,
                 bootstrap: str | None = None, task: str | None = None,
                 max_consecutive_respawns: int = MAX_CONSECUTIVE_RESPAWNS) -> None:
        # 협력자·고정 config 만 — 대화/작업 상태 필드 없음(stateless 단언의 근거).
        # max_consecutive_respawns 는 *config* 상수(불변 임계)지 작업/대화 상태가 아니다.
        self.driver = driver
        self.root = Path(root)
        # task 정체성(명시 전달)은 재진입 프롬프트에 baked-in 되어 `self.bootstrap` 에
        # 흡수된다 — 별도 인스턴스 필드로 retain 하지 않는다(stateless 불변식 유지·respawn 은 같은
        # bootstrap 을 재사용해 task 를 자동 forward). bootstrap 명시 override(테스트/커스텀)가 우선,
        # 없으면 task 로 빌드한다(둘 다 없으면 `build_bootstrap_prompt` 가 ValueError).
        self.bootstrap = bootstrap if bootstrap is not None else build_bootstrap_prompt(task)
        self.max_consecutive_respawns = max_consecutive_respawns

    def stop_marker_present(self, session_id: str) -> bool:
        return stop_marker_present(self.root, session_id)

    def run_loop(self, cwd: str, in_stream: TextIO, out_stream: TextIO,
                 cap_hit_log=None) -> int:
        """바깥 루프 — spawn → relay → post-turn marker 감지 → respawn → repeat.

        - in_stream: 사용자 입력 라인 소스(stdin·테스트는 StringIO).
        - out_stream: PM reply 출력 sink(stdout·테스트는 StringIO).
        - cap_hit_log: cap-hit(32k 절단 의심) loud advisory sink(기본 stderr·테스트는 list.append).
          출력 소비 지점에서 응답이 cap 근방이면 경고만 낸다(never-block·stdout 무오염).
        - 반환 = exit code(0=정상 종료 EOF/quit · GUARD_TRIPPED_RC=연속 respawn 가드 발동).

        marker 존재는 payload 구분 없이 "다음 입력 전 회전" 단일 규칙으로 소비한다.
        이미 완료된 turn 의 입력은 다시 보내지 않는다.

        연속 respawn 가드: bootstrap turn 만으로 marker 가 박히는 fresh 세션이 연속되면
        spawn-loop 가 된다. 이 연속 즉시-회전 횟수를 지역 카운터로 세고 max 초과 시
        진단 1줄 후 종료한다. 사용자 turn 처리 가능한 세션에 도달하면 0으로 리셋한다.
        """
        cap_hit_log = cap_hit_log if cap_hit_log is not None else watchdog._default_watchdog_log
        session_id = self._spawn(cwd, out_stream)
        consecutive_respawns = 0  # bootstrap 직후 연속 즉시-회전 횟수(지역).

        while True:
            # 불변식: **marker 를 지닌 세션엔 추가 입력을 relay 하지 않는다.**
            # spawn/respawn 의 bootstrap turn 이 예산을 넘겨 marker 를 남겼으면
            # 첫 입력 처리 *전* 여기서 회전한다 — 안 그러면 과예산 세션에 입력 1회가 추가 실행된다
            # (지연 회전). bootstrap 은 이미 실행됐으니 반복 대상이 아니다. 병적
            # spawn-loop(예산이 bootstrap turn 보다도 작음)는 연속 즉시-회전 카운터로 막는다.
            if self.stop_marker_present(session_id):
                consecutive_respawns += 1
                if consecutive_respawns > self.max_consecutive_respawns:
                    out_stream.write(
                        f"[relay] spawn 직후 연속 {consecutive_respawns}회 ctx 회전 — 무한 회전 "
                        f"차단(max={self.max_consecutive_respawns}). 종료. ctx 예산이 bootstrap "
                        "turn 보다 작은지 점검.\n"
                    )
                    out_stream.flush()
                    self.driver.close(session_id)
                    clear_marker(self.root, session_id)
                    return GUARD_TRIPPED_RC
                session_id = self._respawn(
                    cwd, session_id, out_stream, reason="bootstrap 초과",
                )
                continue  # 새 sid 로 재-loop — 입력은 회전된 세션이 받는다.

            # marker 없는 fresh 세션에 도달 = 연속 즉시-회전 chain 종료.
            consecutive_respawns = 0
            line = in_stream.readline()
            if line == "":  # EOF.
                break
            text = line.rstrip("\n")
            if text.strip() in QUIT_COMMANDS:
                break
            if text.strip() == "":
                continue

            reply = self.driver.relay_turn(session_id, text)
            if reply is not None:
                out_stream.write(reply + "\n")
                out_stream.flush()
                # 출력 소비 지점 — 응답이 출력 상한(32k tok) 근방이면 silent 절단 의심 loud advisory
                # (never-block·stdout 은 이미 위에서 그대로 전달·경고는 별도 sink). advisory 는
                # relay 를 절대 못 죽인다 — detect/message/sink write 전 경로를 try/except 로 감싼다
                # (병적 sink·근사 예외가 파이프라인을 중단시키면 안 됨·never-block 을 코드로 못박음).
                try:
                    cap_hit, cap_reason = watchdog.detect_output_cap_hit(reply)
                    if cap_hit:
                        cap_hit_log(watchdog.cap_hit_warning_message(cap_reason))
                except Exception:  # noqa: BLE001 — advisory 는 어떤 이유로도 relay 를 막지 않는다.
                    pass

            # 매 turn 직후 1회 stat — marker 있으면 다음 입력을 받기 전에 회전.
            # 이 turn 은 이미 처리·응답됐으므로 입력을 다시 보내지 않는다.
            if self.stop_marker_present(session_id):
                session_id = self._respawn(
                    cwd, session_id, out_stream, reason="turn 초과",
                )

        self.driver.close(session_id)
        return 0

    # ── 내부 회전 헬퍼 (상태 없음 — 인자만으로 동작) ───────────────────────────

    def _spawn(self, cwd: str, out_stream: TextIO) -> str:
        """결정적 session_id 발급 → driver.spawn(bootstrap) → reply 출력."""
        session_id = new_session_id()
        spawned = self.driver.spawn(cwd, session_id, self.bootstrap)
        if isinstance(spawned, tuple):
            session_id, reply = spawned
            if reply is not None:
                out_stream.write(reply + "\n")
                out_stream.flush()
            return session_id
        # 기존 driver 는 sid 만 반환한다. driver 계약 전환 동안 호환 유지.
        return spawned

    def _respawn(self, cwd: str, old_session_id: str, out_stream: TextIO,
                 *, reason: str) -> str:
        """떠나는 세션 정리 후 새 세션 spawn. 새 sid 반환."""
        out_stream.write(f"[relay] ctx 임계 도달 — 세션 회전 ({reason})\n")
        out_stream.flush()
        self.driver.close(old_session_id)
        clear_marker(self.root, old_session_id)
        return self._spawn(cwd, out_stream)
