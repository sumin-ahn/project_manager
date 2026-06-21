"""PM relay (ADR-0009 · T-0046) 단위 테스트.

엔진 core(루트 `.project_manager/tools/pm_relay.py`)를 importlib 로 직접 검증한다.
실 claude 불요 — FakeDriver DI·tmp_path·StringIO 스트림만 본다(test_handoff_trigger·
test_claude_ctx_guard 패턴: importlib 로드·DI runner·subprocess 폭발 가드).

검증 축 (ticket DoD):
  ① marker-watch 분기(있음/없음) — stop_marker_present stat.
  ② respawn 결정 — marker 시 새 session·없으면 relay 지속(호출 카운트).
  ③ parse_stream_json — sid/result 추출 + JSONDecodeError 라인 skip.
  ④ 직전 입력 재전송 — STOP 유발 입력을 respawn 후 새 PM 에 재전송.
  ⑤ stateless — supervisor 가 대화/작업 상태 필드를 보유하지 않음.
  ⑥ subprocess 폭발 가드 — relay 경로가 실 claude 를 부르지 않음(FakeDriver).

+ 통합 스모크 — 실 claude(`@skipif`·기본 skip·frugal haiku). spawn→relay(resume·연속성
  회상)→marker 강제 생성→swap→relay 완주 + sid==marker 예측 검증.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
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
def orch():
    return _load("pm_relay", TOOLS / "pm_relay.py")


# ── FakeDriver: 실 claude 없이 spawn/relay/respawn 을 기록하는 DI 더블 ──────────

class FakeDriver:
    """SessionDriver 더블 — 모든 호출을 기록하고, marker 를 *주입된 시점* 에 생성한다.

    stop_after_relays: 이 횟수만큼 relay 한 뒤(누적) marker 를 root 에 박는다 → supervisor 가
    다음 stat 에서 STOP 을 관측하게 한다(실 ctx_stop_hook 의 marker write 를 모사).
    always_stop: 매 relay 직후 그 세션에 marker 를 박는다 → fresh 세션마다 즉시 STOP 하는
    *병적* 케이스 모사(연속 respawn 가드 검증용·무한 회전 시뮬).
    stop_predicate: (relay_index, session_id, text) -> bool. True 면 그 relay 직후 marker 박제.
    relay 별 STOP 을 정밀 제어해 "정상 turn 이 끼면 카운터 리셋" 을 검증한다(1-based index).
    relay 가 실 claude 를 부르지 않음을 보장(subprocess 폭발 가드 — 여긴 순수 인메모리).
    """

    def __init__(self, root: Path, *, marker_dir, sanitize,
                 stop_after_relays=None, always_stop=False, stop_predicate=None):
        self.root = root
        self._marker_dir = marker_dir
        self._sanitize = sanitize
        self.stop_after_relays = stop_after_relays
        self.always_stop = always_stop
        self.stop_predicate = stop_predicate
        self.spawns: list[str] = []      # spawn 으로 발급된 session_id 목록.
        self.relays: list[tuple[str, str]] = []  # (session_id, text) relay 기록.
        self.closes: list[str] = []
        self._counter = 0

    def _next_sid(self, requested: str) -> str:
        # 실 claude 가 --session-id 를 존중하듯 요청 sid 를 그대로 쓴다(예측 모사).
        return requested

    def spawn(self, cwd: str, session_id: str, bootstrap: str) -> str:
        sid = self._next_sid(session_id)
        self.spawns.append(sid)
        return sid

    def relay_turn(self, session_id: str, text: str) -> str:
        self.relays.append((session_id, text))
        relay_index = len(self.relays)  # 1-based.
        # always_stop: 매 relay 가 즉시 STOP(병적 케이스) — fresh 세션마다 marker 박제.
        if self.always_stop:
            self._write_marker(session_id)
        elif self.stop_predicate is not None:
            if self.stop_predicate(relay_index, session_id, text):
                self._write_marker(session_id)
        # 누적 relay 가 임계 도달 시 marker 박제(ctx_stop_hook 모사).
        elif self.stop_after_relays is not None and relay_index == self.stop_after_relays:
            self._write_marker(session_id)
        return f"reply:{text}"

    def close(self, session_id: str) -> None:
        self.closes.append(session_id)

    def _write_marker(self, session_id: str) -> None:
        path = self.root / self._marker_dir / f"{self._sanitize(session_id)}.done"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ctx-stop handoff triggered\n", encoding="utf-8")


def _make_driver(orch, tmp_path, **kw):
    return FakeDriver(
        tmp_path,
        marker_dir=orch.MARKER_DIR,
        sanitize=orch._sanitize_session_id,
        **kw,
    )


# ── ① marker-watch 분기 (있음/없음) ──────────────────────────────────────────

def test_stop_marker_present_false_when_absent(orch, tmp_path):
    assert orch.stop_marker_present(tmp_path, "sid-x") is False


def test_stop_marker_present_true_after_write(orch, tmp_path):
    path = tmp_path / orch.MARKER_DIR / "sid-y.done"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    assert orch.stop_marker_present(tmp_path, "sid-y") is True


def test_marker_path_matches_ctx_stop_hook_convention(orch, tmp_path):
    """marker 경로가 ctx_stop_hook 규약(`.project_manager/.local/ctx-stop/<sid>.done`)과 동일.

    supervisor 가 marker 를 *예측* 하려면 hook 이 쓰는 경로와 정확히 일치해야 한다(핵심 가정)."""
    path = orch._marker_path(tmp_path, "abc-123")
    assert path == tmp_path / ".project_manager" / ".local" / "ctx-stop" / "abc-123.done"


def test_clear_marker_removes(orch, tmp_path):
    path = tmp_path / orch.MARKER_DIR / "sid-z.done"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    assert orch.clear_marker(tmp_path, "sid-z") is True
    assert orch.stop_marker_present(tmp_path, "sid-z") is False
    # 없는 marker clear 는 fail-soft(False).
    assert orch.clear_marker(tmp_path, "sid-z") is False


def test_sanitize_session_id_matches_hook_rule(orch):
    # ctx_stop_hook._session_id 와 동일 규칙 — 파일명 안전 문자만·traversal 제거.
    assert "/" not in orch._sanitize_session_id("../../etc/passwd")
    assert orch._sanitize_session_id("a/b") == "ab"
    assert orch._sanitize_session_id("  ") == "unknown"
    # uuid4 형태는 보존(하이픈 안전 문자).
    assert orch._sanitize_session_id("11111111-2222-3333-4444-555555555555") == \
        "11111111-2222-3333-4444-555555555555"


# ── ② respawn 결정 (marker 시 새 session · 없으면 relay 지속 · 호출 카운트) ────

def test_no_marker_relays_persist_same_session(orch, tmp_path):
    """marker 없으면 같은 세션으로 relay 가 지속된다 — respawn 안 함(1 spawn)."""
    driver = _make_driver(orch, tmp_path)  # marker 안 박음.
    sup = orch.Supervisor(driver, root=tmp_path)
    in_stream = io.StringIO("hi\nthere\n")  # 2 turn 후 EOF.
    out_stream = io.StringIO()
    rc = sup.run_loop("/cwd", in_stream, out_stream)
    assert rc == 0
    assert len(driver.spawns) == 1          # respawn 없음.
    assert len(driver.relays) == 2          # 2 turn relay.
    # 모든 relay 가 동일(첫) 세션에서 일어났다.
    sid = driver.spawns[0]
    assert all(s == sid for s, _ in driver.relays)
    assert out_stream.getvalue() == "reply:hi\nreply:there\n"


def test_marker_triggers_respawn_with_new_session(orch, tmp_path):
    """첫 relay 직후 marker → respawn(새 session). 이후 relay 는 새 세션에서."""
    driver = _make_driver(orch, tmp_path, stop_after_relays=1)
    sup = orch.Supervisor(driver, root=tmp_path)
    in_stream = io.StringIO("first\nsecond\n")
    out_stream = io.StringIO()
    rc = sup.run_loop("/cwd", in_stream, out_stream)
    assert rc == 0
    # spawn 2회(초기 + respawn) · 두 세션 id 가 다르다.
    assert len(driver.spawns) == 2
    assert driver.spawns[0] != driver.spawns[1]
    # 떠나는 세션은 close 됐다.
    assert driver.spawns[0] in driver.closes


def test_respawn_clears_old_marker(orch, tmp_path):
    """respawn 시 떠나는 세션의 marker 를 정리한다(회전 후 stale marker 누적 방지)."""
    driver = _make_driver(orch, tmp_path, stop_after_relays=1)
    sup = orch.Supervisor(driver, root=tmp_path)
    sup.run_loop("/cwd", io.StringIO("a\nb\n"), io.StringIO())
    old_sid = driver.spawns[0]
    assert orch.stop_marker_present(tmp_path, old_sid) is False  # 정리됨.


# ── ②b 연속 respawn 가드 (병적 무한 STOP 차단 · 정상 turn 리셋) ────────────────

def test_consecutive_respawn_guard_halts_pathological_loop(orch, tmp_path):
    """병적 케이스 — fresh 세션마다 즉시 STOP(같은 입력 무한 재전송 회전) → max 회 후 종료.

    always_stop 으로 매 relay 가 즉시 marker 를 박는다(임계 오설정·거대 단일 입력 모사).
    가드 없으면 무한 루프. 가드가 max_consecutive_respawns 초과 시 종료해야 한다.
    """
    driver = _make_driver(orch, tmp_path, always_stop=True)
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=5)
    # 입력 1줄만 — 첫 turn 이 STOP 유발, 이후 재전송이 매번 또 STOP(병적). 무한 루프 위험.
    in_stream = io.StringIO("poison\n")
    out_stream = io.StringIO()
    rc = sup.run_loop("/cwd", in_stream, out_stream)
    # sentinel rc(정상 0 과 구분) — 가드 발동 종료.
    assert rc == orch.GUARD_TRIPPED_RC
    assert rc != 0
    # 무한 루프 아님 — spawn 횟수가 max 근방에서 멈춘다(여유 포함 ≤ max+2).
    assert len(driver.spawns) <= sup.max_consecutive_respawns + 2
    # 진단 1줄이 out_stream 에 쓰였다(병적 상황 알림).
    assert "relay" in out_stream.getvalue()
    assert "respawn" in out_stream.getvalue().lower() or "STOP" in out_stream.getvalue()


def test_consecutive_respawn_guard_respects_custom_max(orch, tmp_path):
    """max_consecutive_respawns 가 작으면 더 일찍 종료(상수 존중)."""
    driver = _make_driver(orch, tmp_path, always_stop=True)
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=2)
    rc = sup.run_loop("/cwd", io.StringIO("poison\n"), io.StringIO())
    assert rc == orch.GUARD_TRIPPED_RC
    # max=2 면 max=5 보다 spawn 이 적다(더 빨리 멈춤).
    assert len(driver.spawns) <= 2 + 2


def test_default_max_consecutive_respawns_constant(orch, tmp_path):
    """기본값 = 모듈 상수(생성자 미지정 시)."""
    driver = _make_driver(orch, tmp_path)
    sup = orch.Supervisor(driver, root=tmp_path)
    assert sup.max_consecutive_respawns == orch.MAX_CONSECUTIVE_RESPAWNS
    assert orch.MAX_CONSECUTIVE_RESPAWNS == 5


def test_normal_rotation_does_not_trip_guard(orch, tmp_path):
    """정상 회전(긴 작업→자연 STOP→재전송 성공→새 입력으로 계속)은 가드 비발동.

    건강한 회전: 각 입력이 처음엔 STOP(ctx 한계 도달) 하나, 재전송(fresh 세션)은 성공한다 →
    respawn 없이 진행 = 진전 → 카운터 리셋. max(5) 보다 많은 10개의 *서로 다른* 입력을 매 입력
    1회 STOP→재전송 성공 패턴으로 흘려도 가드가 발동하면 안 된다(병적 아님).
    """
    seen: set[str] = set()  # 각 text 의 첫 등장(=fresh-read turn)만 STOP, 재전송은 성공.

    def stop_first_time(idx, sid, text):
        if text in seen:
            return False  # 재전송 turn — 성공(STOP 없음·진전).
        seen.add(text)
        return True       # fresh-read turn — 1회 STOP(자연 ctx 한계 모사).

    driver = _make_driver(orch, tmp_path, stop_predicate=stop_first_time)
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=5)
    lines = "".join(f"input{i}\n" for i in range(10))
    rc = sup.run_loop("/cwd", io.StringIO(lines), io.StringIO())
    # 모든 입력을 소비하고 EOF 로 정상 종료 — 가드 비발동(rc=0).
    assert rc == 0
    # 모든 사용자 입력이 (재전송 후) relay 됐다.
    relayed_texts = [t for _, t in driver.relays]
    for i in range(10):
        assert f"input{i}" in relayed_texts


def test_non_respawn_turn_resets_counter_below_trip(orch, tmp_path):
    """진전(respawn 없이 끝난 turn)이 끼면 가드가 발동하지 않는다 — 사용자-체감 동작 검증.

    같은 입력을 재전송하다 *한 번이라도* STOP 없이 통과하면(진전) 가드는 trip 하지 않는다.
    max=2 에서 idx3 relay 만 STOP 을 빼(진전) → counter1 에서 멈추고 다음 readline 이 EOF →
    정상 종료(rc=0). (이 흐름은 line 270 의 reset 분기를 *거치나*, 직후 EOF 라 그 reset 값이
    후속 trip 에 닿지 못해 효과는 관측 불가 — 실 trip-관련 리셋은 새 chain 의 fresh-STOP[line 255].
    여기선 "진전 turn → 비-trip" 이라는 동작만 단언한다.)
    """
    # relay idx3 만 STOP 을 빼고 나머지는 STOP(병적). idx3 의 비-STOP turn 이 카운터를 리셋.
    driver = _make_driver(
        orch, tmp_path,
        stop_predicate=lambda idx, sid, text: idx != 3,
    )
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=2)
    # 입력 1줄: t1 read→STOP(reset,counter0), t2 resend→STOP(counter1), t3 resend→NO STOP
    # (counter0·진전) → pending 비고 다음 readline 이 EOF → 정상 종료. trip 안 함.
    rc = sup.run_loop("/cwd", io.StringIO("p\n"), io.StringIO())
    assert rc == 0  # 진전 리셋 덕에 가드 비발동·정상 종료.


def test_consecutive_respawn_guard_no_reset_would_trip(orch, tmp_path):
    """대조군 — 진전(비-STOP turn)이 전혀 없으면 max 초과로 trip(리셋 분기의 필요성 입증)."""
    driver = _make_driver(orch, tmp_path, always_stop=True)  # 진전 0(매 turn STOP).
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=2)
    rc = sup.run_loop("/cwd", io.StringIO("p\n"), io.StringIO())
    assert rc == orch.GUARD_TRIPPED_RC  # 진전 없음 → trip.


# ── ③ parse_stream_json (sid/result 추출 + JSONDecodeError 라인 skip) ────────

def test_parse_stream_json_extracts_sid_and_result(orch):
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid-init"}),
        json.dumps({"type": "assistant", "session_id": "sid-init"}),
        json.dumps({"type": "result", "result": "the answer", "session_id": "sid-init"}),
    ]
    sid, result = orch.parse_stream_json(lines)
    assert sid == "sid-init"
    assert result == "the answer"


def test_parse_stream_json_skips_malformed_lines(orch):
    lines = [
        "not json at all",
        "",
        "{broken",
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid-2"}),
        "another bad line {",
        json.dumps({"type": "result", "result": "ok"}),
    ]
    sid, result = orch.parse_stream_json(lines)
    assert sid == "sid-2"
    assert result == "ok"


def test_parse_stream_json_falls_back_to_result_sid(orch):
    """system/init 없으면 result 이벤트의 session_id 로 폴백."""
    lines = [json.dumps({"type": "result", "result": "r", "session_id": "sid-from-result"})]
    sid, result = orch.parse_stream_json(lines)
    assert sid == "sid-from-result"
    assert result == "r"


def test_parse_stream_json_empty_and_no_result(orch):
    assert orch.parse_stream_json([]) == (None, None)
    # init 만 있고 result 없음 → sid 만.
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "s"})]
    assert orch.parse_stream_json(lines) == ("s", None)


def test_parse_stream_json_ignores_non_dict_events(orch):
    """JSON 배열/스칼라 라인은 dict 가 아니라 skip(robust)."""
    lines = ["[1, 2, 3]", "42", json.dumps({"type": "result", "result": "x"})]
    sid, result = orch.parse_stream_json(lines)
    assert sid is None and result == "x"


# ── ④ 직전 입력 재전송 ───────────────────────────────────────────────────────

def test_pending_input_resent_to_new_session(orch, tmp_path):
    """STOP 을 유발한(차단된) 입력이 respawn 후 새 PM 에 재전송된다(in-flight 의도 보존)."""
    driver = _make_driver(orch, tmp_path, stop_after_relays=1)
    sup = orch.Supervisor(driver, root=tmp_path)
    # "trigger" 입력이 첫 relay → marker → respawn. "trigger" 가 새 세션에 재전송돼야.
    in_stream = io.StringIO("trigger\nfollowup\n")
    out_stream = io.StringIO()
    sup.run_loop("/cwd", in_stream, out_stream)

    new_sid = driver.spawns[1]
    # 새 세션에 재전송된 첫 입력이 "trigger"(차단된 입력) 다.
    new_session_relays = [text for sid, text in driver.relays if sid == new_sid]
    assert new_session_relays[0] == "trigger"
    # 그 뒤에 사용자 새 입력 "followup" 이 이어진다(재전송이 입력 소비를 안 함).
    assert "followup" in new_session_relays


def test_pending_resent_not_user_input_consumed(orch, tmp_path):
    """재전송 turn 은 사용자 새 입력을 읽지 않는다 — 입력 스트림 라인 수 보존."""
    driver = _make_driver(orch, tmp_path, stop_after_relays=1)
    sup = orch.Supervisor(driver, root=tmp_path)
    # 입력 2줄. 첫 turn(=trigger) → marker → 재전송 1 turn + 둘째 입력 1 turn = relay 3회.
    sup.run_loop("/cwd", io.StringIO("trigger\nsecond\n"), io.StringIO())
    # relay 총 3회: trigger(old) · trigger(재전송·new) · second(new).
    assert len(driver.relays) == 3
    texts = [t for _, t in driver.relays]
    assert texts == ["trigger", "trigger", "second"]


# ── ⑤ stateless (상태 미보유) ────────────────────────────────────────────────

def test_supervisor_holds_no_conversation_state(orch, tmp_path):
    """stateless 불변식 — supervisor 인스턴스는 협력자(driver)·고정 config 만 보유,
    대화/작업 상태 필드는 0(메시지/히스토리/세션 누적 없음)."""
    driver = _make_driver(orch, tmp_path)
    sup = orch.Supervisor(driver, root=tmp_path)
    # 협력자 + 고정 config 뿐 — max_consecutive_respawns 는 불변 임계(config)지 대화/작업 상태가
    # 아니다(연속 respawn 카운터 자체는 run_loop 지역 변수로 인스턴스에 없다).
    allowed = {"driver", "root", "bootstrap", "max_consecutive_respawns"}
    actual = set(vars(sup).keys())
    assert actual == allowed, f"예상 밖 상태 필드: {actual - allowed}"


def test_supervisor_state_unchanged_across_turns(orch, tmp_path):
    """relay 를 여러 turn 돌려도 supervisor __dict__ 가 불변(메시지 누적 없음)."""
    driver = _make_driver(orch, tmp_path)
    sup = orch.Supervisor(driver, root=tmp_path)
    before = dict(vars(sup))
    sup.run_loop("/cwd", io.StringIO("a\nb\nc\n"), io.StringIO())
    after = dict(vars(sup))
    # driver 는 같은 객체(기록은 driver 에 쌓임), supervisor 필드 자체는 불변.
    assert before.keys() == after.keys()
    assert before["root"] == after["root"]
    assert before["bootstrap"] == after["bootstrap"]
    assert before["driver"] is after["driver"]


def test_new_session_id_unique_uuid(orch):
    a, b = orch.new_session_id(), orch.new_session_id()
    assert a != b
    assert len(a) == 36 and a.count("-") == 4  # uuid4 형태.


# ── ⑥ subprocess 폭발 가드 (relay 경로가 실 claude 안 부름) ───────────────────

def test_run_loop_does_not_spawn_real_subprocess(orch, tmp_path, monkeypatch):
    """run_loop 전 경로가 subprocess 를 호출하지 않는다 — 호출 시 폭발(FakeDriver 만 씀)."""
    import subprocess as _sp

    def _boom(*a, **k):  # noqa: ANN001
        raise AssertionError("relay 가 실 subprocess 를 호출했다")

    monkeypatch.setattr(_sp, "run", _boom)
    monkeypatch.setattr(_sp, "Popen", _boom)
    driver = _make_driver(orch, tmp_path, stop_after_relays=1)
    sup = orch.Supervisor(driver, root=tmp_path)
    # respawn 까지 도는 시나리오도 subprocess 0 — marker 는 FakeDriver 가 인메모리로 박음.
    rc = sup.run_loop("/cwd", io.StringIO("x\ny\n"), io.StringIO())
    assert rc == 0


def test_quit_command_ends_loop(orch, tmp_path):
    driver = _make_driver(orch, tmp_path)
    sup = orch.Supervisor(driver, root=tmp_path)
    rc = sup.run_loop("/cwd", io.StringIO("hi\n/quit\nshould-not-relay\n"), io.StringIO())
    assert rc == 0
    # /quit 전 1 turn 만 relay(이후 입력은 안 읽음).
    assert len(driver.relays) == 1
    assert driver.relays[0][1] == "hi"


def test_blank_lines_skipped(orch, tmp_path):
    driver = _make_driver(orch, tmp_path)
    sup = orch.Supervisor(driver, root=tmp_path)
    sup.run_loop("/cwd", io.StringIO("\n  \nreal\n"), io.StringIO())
    assert len(driver.relays) == 1
    assert driver.relays[0][1] == "real"


# ── claude driver (어댑터) 얇은 단위 — subprocess seam DI ────────────────────

@pytest.fixture(scope="module")
def driver_mod():
    return _load(
        "pm_orch_claude",
        REPO / "templates" / "claude_code" / ".claude" / "pm_orch_claude.py",
    )


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def test_claude_driver_spawn_passes_session_id(orch, driver_mod):
    """spawn 이 `--session-id <uuid>` 를 넘기고 cwd 를 격리한다(child cwd 명시)."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(
            json.dumps({"type": "system", "subtype": "init", "session_id": "uuid-123"})
            + "\n"
            + json.dumps({"type": "result", "result": "READY"})
        )

    driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, runner=fake_run)
    observed = driver.spawn("/repo/root", "uuid-123", "bootstrap text")
    assert observed == "uuid-123"
    cmd = captured["cmd"]
    assert "--session-id" in cmd and "uuid-123" in cmd
    assert "--resume" not in cmd  # spawn 은 resume 안 함.
    assert captured["kwargs"]["cwd"] == "/repo/root"  # child cwd 격리.


def test_claude_driver_relay_uses_resume(orch, driver_mod):
    """relay_turn 이 `--resume <uuid>` 로 같은 세션을 잇고 reply 를 반환."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(json.dumps({"type": "result", "result": "hello back"}))

    driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, runner=fake_run)
    reply = driver.relay_turn("uuid-abc", "ping")
    assert reply == "hello back"
    cmd = captured["cmd"]
    assert "--resume" in cmd and "uuid-abc" in cmd
    assert "--session-id" not in cmd  # resume 은 새 id 안 발급.
    assert "ping" in cmd


def test_claude_driver_relay_reuses_spawn_cwd(orch, driver_mod):
    """relay 가 spawn 때의 cwd 에서 resume 한다 — claude 세션은 cwd-scoped(다른 cwd 면
    'No conversation found'). spawn cwd 기억→재사용이 깨지면 live resume 이 실패한다(실측 발).
    """
    cwds = []

    def fake_run(cmd, **kwargs):
        cwds.append(kwargs.get("cwd"))
        if "--session-id" in cmd:
            return _FakeCompleted(
                json.dumps({"type": "system", "subtype": "init", "session_id": "uuid-cwd"})
                + "\n" + json.dumps({"type": "result", "result": "READY"})
            )
        return _FakeCompleted(json.dumps({"type": "result", "result": "ok"}))

    driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, runner=fake_run)
    driver.spawn("/repo/root", "uuid-cwd", "boot")
    driver.relay_turn("uuid-cwd", "msg")
    # spawn cwd 와 relay cwd 가 동일(세션 scope 일치).
    assert cwds == ["/repo/root", "/repo/root"]
    # close 는 세션 cwd 메타를 정리한다.
    driver.close("uuid-cwd")
    assert "uuid-cwd" not in driver._session_cwd


def test_claude_driver_timeout_returns_empty(orch, driver_mod):
    """subprocess timeout 은 fail-soft — 빈 reply(루프 안 죽음)."""
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        raise _sp.TimeoutExpired(cmd, 1)

    driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, runner=fake_run)
    assert driver.relay_turn("uuid", "x") == ""


def test_claude_driver_close_is_noop(orch, driver_mod):
    driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, runner=lambda *a, **k: None)
    assert driver.close("uuid") is None


def test_claude_driver_parser_flags(driver_mod):
    parser = driver_mod.build_parser()
    ns = parser.parse_args(["--cwd", "/some/repo", "--model", "opus"])
    assert ns.cwd == "/some/repo" and ns.model == "opus"
    ns2 = parser.parse_args([])
    assert ns2.cwd is None  # 기본 = 실행 dir(main 에서 os.getcwd()).


# ── 통합 스모크 (실 claude · 기본 skip · frugal haiku) ────────────────────────

PM_ORCH_LIVE = os.environ.get("PM_ORCH_LIVE") == "1"


@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("claude"),
    reason="통합 스모크 — PM_ORCH_LIVE=1 + claude CLI 필요(기본 skip·CI green 불변).",
)
def test_live_spawn_relay_swap_smoke(orch, driver_mod, tmp_path):
    """실 claude 1회 e2e: spawn(`--session-id <uuid>`) → relay(≥2턴 resume·turn2 가 turn1
    사실 회상) → marker 강제 생성 → swap → relay 완주. frugal haiku·단발 프롬프트.

    핵심 sid 검증: spawn 에 준 uuid == stream-json system/init session_id 인지(같으면 hook
    도 그 uuid 로 marker 를 쓸 것 — marker 예측 가정 확증). 루트 `.claude/` 엔 ctx hook 이
    없어 실 ctx-STOP 은 못 트리거하므로 marker 는 강제 생성으로 swap 만 검증(deferred 부분).
    """
    driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, model="claude-haiku-4-5")
    requested_sid = orch.new_session_id()

    # ── spawn: --session-id 로 세션 id 지정 + 사실 심기 ──
    observed = driver.spawn(
        str(tmp_path),
        requested_sid,
        "Remember this code word for our chat: MANGO77. Reply with exactly: STORED",
    )
    assert observed, "spawn 이 session_id 를 관측하지 못함"
    # *** sid 예측 가능성 핵심 검증 ***
    assert observed == requested_sid, (
        f"sid 불일치 — 내가 준 uuid={requested_sid!r} != system/init session_id={observed!r}. "
        "marker 예측 불가 → stream-json sid 파싱 환원 경로 필요."
    )

    # ── relay turn2: resume 같은 세션 → turn1 사실 회상(연속성) ──
    reply = driver.relay_turn(
        observed, "What was the code word? Reply with only the code word."
    )
    assert "MANGO77" in reply.upper(), f"resume 연속성 실패 — reply={reply!r}"

    # ── marker 강제 생성(ctx_stop_hook 모사) → supervisor STOP 관측 ──
    marker = orch._marker_path(tmp_path, observed)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ctx-stop handoff triggered\n", encoding="utf-8")
    assert orch.stop_marker_present(tmp_path, observed) is True

    # ── swap: 새 세션 spawn(다른 sid) → relay 완주 ──
    new_sid = orch.new_session_id()
    assert new_sid != requested_sid
    new_observed = driver.spawn(
        str(tmp_path),
        new_sid,
        "Context handoff: the prior code word was MANGO77. Reply with exactly: CONTINUED",
    )
    assert new_observed == new_sid, "swap 후 새 세션 sid 예측 실패"
    swap_reply = driver.relay_turn(new_observed, "Reply with only the prior code word.")
    assert "MANGO77" in swap_reply.upper(), f"swap 후 relay 실패 — reply={swap_reply!r}"
