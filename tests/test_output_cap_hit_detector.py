"""opencode 32k 출력 절단(cap-hit) detector (T-0339) — 결정적 픽스처 단위테스트.

opencode 는 outbound 응답이 출력 cap(32000 토큰·실효 min(limit.output,32000))을 넘으면 응답을
**조용히 절단** 하고 finish 를 "stop" 으로 위장한다(T-0334 라이브 확증) — 수신자(PM)는 절단을
감지하지 못한다. 호출층(pm_relay·출력 소비 지점 Supervisor.run_loop)의 cap-hit detector 가 응답이
cap 근방이면 loud advisory 를 낸다. **advisory·never-block** — 경고+로그만·파이프라인 무중단.

검증 축 (ticket DoD):
  ① 순수 detector `detect_output_cap_hit` — cap 근방 대형 출력 주입 → 발화 / 정상 크기 → 무발화(오탐 0).
  ② char 임계 경계(threshold-1 무발화 · threshold 발화) + 근사 token 보수적 상한.
  ③ env 노브 `PM_OC_CAP_HIT_THRESHOLD` 해소기 + detector override.
  ④ 경고 문구 `cap_hit_warning_message` 에 파일-전달 규약(T-0337) 안내 포함(파일·경로·safe_write·절단).
  ⑤ run_loop 배선 — cap 근방 응답 → cap_hit_log 발화 · 정상 응답 → 무발화 · never-block(전체 응답은
     그대로 out_stream 에 전달·경고는 stdout 무오염).

전부 결정적(라이브·실 subprocess 의존 0) — 실 opencode 없이 대형 출력을 인메모리로 주입한다.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def engine():
    return _load("pm_relay", TOOLS / "pm_relay.py")


# ── ① 순수 detector: cap 근방 발화 / 정상 크기 무발화 (오탐 0) ─────────────────────

def test_detector_fires_on_near_cap_output(engine):
    """cap 근방 대형 출력(임계 이상)을 주입하면 (True, reason) — silent 절단 의심 발화."""
    big = "가" * (engine.CAP_HIT_CHAR_THRESHOLD + 500)  # 결정적 대형 출력(한글-dense).
    hit, reason = engine.detect_output_cap_hit(big)
    assert hit is True
    assert str(len(big)) in reason              # 실측 char 수 보고.
    assert str(engine.CAP_TOKENS) in reason     # cap(32000) 맥락 포함.
    assert "cap" in reason


@pytest.mark.parametrize("normal", [
    "",                              # 빈 응답.
    "PONG",                          # 단발 짧은 응답.
    "정상 PM reply 한 줄." * 40,      # 수백 자 대화 응답.
    "x" * 15000,                     # 15KB — 큰 handoff 요약급이나 임계(34560) 대비 여전히 >2x 아래.
])
def test_detector_no_fire_on_normal_output(engine, normal):
    """정상 크기 응답(빈~15KB)은 무발화 — 오탐 0(임계 34560 대비 >2x 마진)."""
    hit, reason = engine.detect_output_cap_hit(normal)
    assert hit is False
    assert reason == ""


def test_detector_none_text_no_fire(engine):
    """None 응답(driver fail-soft 등)은 무발화(graceful)."""
    hit, reason = engine.detect_output_cap_hit(None)
    assert hit is False and reason == ""


# ── ② char 임계 경계 + 근사 상한 ────────────────────────────────────────────────

def test_detector_threshold_boundary(engine):
    """임계-1 자는 무발화 · 임계 자는 발화(경계 결정성)."""
    thr = engine.CAP_HIT_CHAR_THRESHOLD
    below, _ = engine.detect_output_cap_hit("a" * (thr - 1))
    at, _ = engine.detect_output_cap_hit("a" * thr)
    assert below is False
    assert at is True


def test_detector_char_threshold_override(engine):
    """char_threshold 인자 override — 낮추면 작은 출력도 발화(주입 임계)."""
    hit, reason = engine.detect_output_cap_hit("a" * 100, char_threshold=50)
    assert hit is True and "100 자" in reason
    # 같은 100 자를 기본(34560) 임계로는 무발화.
    assert engine.detect_output_cap_hit("a" * 100)[0] is False


def test_threshold_derivation_constants(engine):
    """임계 산정식 고정 — cap 32000 × 1.2 char/tok(극단 dense CJK 하한) × 0.90 = 34560 char.

    char/tok 하한을 1.2 로 보수 하향해 순수 한글-dominant(1.5 미만 dense) 절단 창까지 잡는다
    (should-fix·false-negative 회피). 설계 근거를 상수로 잠근다."""
    assert engine.CAP_TOKENS == 32000
    assert engine.CAP_HIT_MIN_CHARS_PER_TOKEN == 1.2
    assert engine.CAP_HIT_RATIO == 0.90
    assert engine.CAP_HIT_CHAR_THRESHOLD == 34560
    assert engine.CAP_HIT_CHAR_THRESHOLD == int(
        engine.CAP_TOKENS * engine.CAP_HIT_MIN_CHARS_PER_TOKEN * engine.CAP_HIT_RATIO
    )


def test_detector_reason_reports_conservative_token_approx(engine):
    """reason 의 token 근사는 char/1.5(보수적 상한) — exact 토크나이저 미의존."""
    big = "a" * 60000
    _, reason = engine.detect_output_cap_hit(big)
    assert f"≈{int(60000 / engine.CAP_HIT_MIN_CHARS_PER_TOKEN)} tok" in reason
    assert "근사" in reason  # 근사임을 명시(정확 수치 아님).


# ── ③ env 노브 PM_OC_CAP_HIT_THRESHOLD ─────────────────────────────────────────

def test_env_knob_cap_hit_threshold(engine, monkeypatch):
    monkeypatch.delenv("PM_OC_CAP_HIT_THRESHOLD", raising=False)
    assert engine.cap_hit_char_threshold_default() == 34560        # 기본.
    monkeypatch.setenv("PM_OC_CAP_HIT_THRESHOLD", "1000")
    assert engine.cap_hit_char_threshold_default() == 1000
    monkeypatch.setenv("PM_OC_CAP_HIT_THRESHOLD", "  2500  ")
    assert engine.cap_hit_char_threshold_default() == 2500         # 공백 trim.
    monkeypatch.setenv("PM_OC_CAP_HIT_THRESHOLD", "bogus")
    assert engine.cap_hit_char_threshold_default() == 34560        # 불량 → 기본.
    monkeypatch.setenv("PM_OC_CAP_HIT_THRESHOLD", "0")
    assert engine.cap_hit_char_threshold_default() == 34560        # 비양수 → 기본.


def test_env_knob_drives_detector_default(engine, monkeypatch):
    """env 노브를 낮추면 detector 기본 임계(char_threshold=None 경로)가 그 값을 쓴다."""
    monkeypatch.setenv("PM_OC_CAP_HIT_THRESHOLD", "500")
    hit, _ = engine.detect_output_cap_hit("a" * 600)  # 600 ≥ 500 → 발화.
    assert hit is True
    assert engine.detect_output_cap_hit("a" * 400)[0] is False  # 400 < 500 → 무발화.


# ── ④ 경고 문구에 파일-전달 규약(T-0337) 안내 포함 ──────────────────────────────

def test_warning_message_carries_file_delivery_convention(engine):
    """cap_hit_warning_message 는 파일-전달 규약을 안내한다.

    얕은 단일 문자열 1개가 아니라 규약 키워드 클래스(파일·절대경로·safe_write·절단·요약)를
    검증해 규약 요지가 실려야 우회 재시도로 이어지게 한다.
    """
    _, reason = engine.detect_output_cap_hit("가" * (engine.CAP_HIT_CHAR_THRESHOLD + 10))
    msg = engine.cap_hit_warning_message(reason)
    for keyword in ("파일-전달 규약", "파일", "절대경로", "safe_write", "절단", "요약"):
        assert keyword in msg, f"경고 문구에 규약 키워드 {keyword!r} 누락"
    assert reason in msg           # 진단(char 수·cap 맥락)도 실린다.
    assert msg.startswith("[pm-orch]")  # loud prefix(다른 driver 진단과 결 일치).


# ── ⑤ run_loop 배선 (출력 소비 지점 · never-block · stdout 무오염) ────────────────

class _ReplyDriver:
    """SessionDriver 더블 — scripted reply 를 순서대로 돌려주고 marker 는 안 박는다(회전 없음).

    cap-hit 배선만 격리 검증 — relay/respawn 이 아니라 *출력 소비 지점* 의 detector 발화를 본다.
    실 opencode/subprocess 불요(인메모리)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self._i = 0
        self.spawns: list[str] = []

    def spawn(self, cwd, session_id, bootstrap):
        self.spawns.append(session_id)
        return session_id

    def relay_turn(self, session_id, text):
        reply = self._replies[self._i] if self._i < len(self._replies) else ""
        self._i += 1
        return reply

    def close(self, session_id):
        pass


def test_run_loop_warns_on_near_cap_reply(engine, tmp_path):
    """cap 근방 응답을 driver 가 돌려주면 run_loop 이 cap_hit_log 로 loud advisory 발화."""
    big = "가" * (engine.CAP_HIT_CHAR_THRESHOLD + 200)  # 결정적 대형 출력 주입.
    driver = _ReplyDriver([big])
    sup = engine.Supervisor(driver, root=tmp_path)
    logs: list[str] = []
    out = io.StringIO()
    rc = sup.run_loop("/cwd", io.StringIO("hi\n"), out, cap_hit_log=logs.append)
    assert rc == 0
    # advisory 정확히 1회 · 하니스-중립 문구 + 파일-전달 규약 안내 실림.
    assert len(logs) == 1
    assert "출력 상한" in logs[0] and "opencode 하니스라면" in logs[0]
    assert "파일-전달 규약" in logs[0] and "safe_write" in logs[0]
    # never-block — 전체(잘렸을 수 있는) 응답은 그대로 out_stream(PM 채널)에 전달된다.
    assert big in out.getvalue()
    # stdout(PM 대화 채널)은 경고로 오염되지 않는다 — 경고는 별도 sink 로만.
    assert "파일-전달 규약" not in out.getvalue()


def test_run_loop_no_warn_on_normal_reply(engine, tmp_path):
    """정상 크기 응답은 무발화 — 오탐 0(사용자-체감 배선 검증)."""
    driver = _ReplyDriver(["짧은 정상 응답입니다."])
    sup = engine.Supervisor(driver, root=tmp_path)
    logs: list[str] = []
    out = io.StringIO()
    rc = sup.run_loop("/cwd", io.StringIO("hi\n"), out, cap_hit_log=logs.append)
    assert rc == 0
    assert logs == []                                   # 무발화.
    assert out.getvalue() == "짧은 정상 응답입니다.\n"    # 응답은 정상 전달.


def test_run_loop_default_cap_hit_log_is_stderr(engine, tmp_path, capsys):
    """cap_hit_log 미주입 시 기본 sink = stderr(stdout=PM 채널 보존)."""
    big = "가" * (engine.CAP_HIT_CHAR_THRESHOLD + 50)
    driver = _ReplyDriver([big])
    sup = engine.Supervisor(driver, root=tmp_path)
    out = io.StringIO()
    sup.run_loop("/cwd", io.StringIO("hi\n"), out)  # cap_hit_log 미주입 → 기본 stderr.
    captured = capsys.readouterr()
    assert "출력 상한" in captured.err and "파일-전달 규약" in captured.err
    assert "파일-전달 규약" not in out.getvalue()   # 주입 out_stream 은 무오염.


def test_run_loop_cap_hit_never_blocks_rotation(engine, tmp_path):
    """cap-hit 발화가 이후 turn 진행을 막지 않는다 — 다음 입력도 정상 relay(never-block)."""
    big = "가" * (engine.CAP_HIT_CHAR_THRESHOLD + 100)
    driver = _ReplyDriver([big, "다음 turn 정상 응답"])
    sup = engine.Supervisor(driver, root=tmp_path)
    logs: list[str] = []
    out = io.StringIO()
    rc = sup.run_loop("/cwd", io.StringIO("first\nsecond\n"), out, cap_hit_log=logs.append)
    assert rc == 0
    assert len(logs) == 1                       # 첫 turn 만 cap-hit.
    assert "다음 turn 정상 응답" in out.getvalue()  # 둘째 turn 도 완주(무중단).


def test_run_loop_advisory_never_blocks_on_pathological_sink(engine, tmp_path):
    """병적 sink(경고 write 가 예외)여도 relay 는 안 죽는다 — never-block 을 코드로 못박음(try/except).

    advisory emission 전 경로(detect/message/sink write)를 try/except 로 감쌌으므로, sink 가
    폭발해도 응답 전달·이후 turn 진행이 끊기면 안 된다(파이프라인 무중단 단언)."""
    big = "가" * (engine.CAP_HIT_CHAR_THRESHOLD + 100)
    driver = _ReplyDriver([big, "다음 turn 정상 응답"])
    sup = engine.Supervisor(driver, root=tmp_path)

    def boom_sink(_msg):
        raise RuntimeError("병적 sink — advisory write 폭발")

    out = io.StringIO()
    rc = sup.run_loop("/cwd", io.StringIO("first\nsecond\n"), out, cap_hit_log=boom_sink)
    assert rc == 0                                    # sink 폭발에도 정상 종료.
    assert big in out.getvalue()                      # 첫(cap-hit) 응답도 그대로 전달.
    assert "다음 turn 정상 응답" in out.getvalue()      # 둘째 turn 도 완주(무중단).
