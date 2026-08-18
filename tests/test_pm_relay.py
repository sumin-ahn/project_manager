"""PM relay (ADR-0009 · T-0046) 단위 테스트.

엔진 core(루트 `.project_manager/tools/pm_relay.py`)를 importlib 로 직접 검증한다.
실 claude 불요 — FakeDriver DI·tmp_path·StringIO 스트림만 본다(test_handoff_trigger·
test_claude_ctx_guard 패턴: importlib 로드·DI runner·subprocess 폭발 가드).

검증 축 (ticket DoD):
  ① marker-watch 분기(있음/없음) — stop_marker_present stat.
  ② respawn 결정 — marker 시 새 session·없으면 relay 지속(호출 카운트).
  ③ parse_stream_json — sid/result/usage 추출 + JSONDecodeError 라인 skip.
  ④ post-turn 단일 의미론 — marker 회전 후 처리된 입력 재전송 없음.
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
import re
import shutil
import subprocess
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


def test_opencode_prompt_guard_converts_cwd_symlink_loop(orch, tmp_path):
    """resolve symlink loop는 RuntimeError traceback 대신 공용 하네스 계약 예외가 된다."""
    cwd_loop = tmp_path / "cwd-loop"
    cwd_loop.symlink_to(cwd_loop, target_is_directory=True)

    with pytest.raises(orch.HarnessContractError, match="실경로 해소 실패"):
        orch.assert_opencode_prompt_in_cwd(cwd_loop, cwd_loop / "prompt.md")


@pytest.mark.parametrize("role", ("developer", "architect", "code-reviewer", "researcher"))
def test_opencode_runtime_role_config_is_self_contained_and_exact(orch, role):
    """Cross adopter에 agent 카드가 없어도 선택 역할 하나의 mode/permission이 완결된다."""
    first = orch.opencode_runtime_role_config(role)
    assert first == orch.opencode_runtime_role_config(role)  # stable wire/audit bytes
    config = json.loads(first)
    assert set(config) == {"agent"}
    assert set(config["agent"]) == {role}
    agent = config["agent"][role]
    assert agent["mode"] == "all"
    assert agent["permission"]["task"] == "deny"
    assert agent["permission"]["webfetch"] == "deny"
    # edit 는 전 역할 allow — researcher 도 티켓 사본 자기 절을 기록한다(ADR-0089·T-0696 F-014·
    # 출하 카드와 동일 축 · T-0745). researcher 는 bash 만 deny.
    assert agent["permission"]["edit"] == "allow"
    if role == "researcher":
        assert agent["permission"]["bash"] == "deny"
    else:
        assert agent["permission"]["bash"]["*"] == "allow"
        assert agent["permission"]["bash"]["rm *"] == "deny"


def test_opencode_runtime_role_overrides_untrusted_incoming_content_without_mutation(orch):
    """사용자 runtime JSON은 plugin/secret 표면이라 병합하지 않고 입력 dict도 변경하지 않는다."""
    original = {"PATH": "/bin", "OPENCODE_CONFIG_CONTENT": "SECRET_PLUGIN_CONFIG"}
    resolved = orch.with_opencode_runtime_role(original, "code-reviewer")
    assert original["OPENCODE_CONFIG_CONTENT"] == "SECRET_PLUGIN_CONFIG"
    assert resolved["PATH"] == "/bin"
    assert "SECRET_PLUGIN_CONFIG" not in resolved["OPENCODE_CONFIG_CONTENT"]
    assert set(json.loads(resolved["OPENCODE_CONFIG_CONTENT"])["agent"]) == {
        "code-reviewer"
    }


# ── FakeDriver: 실 claude 없이 spawn/relay/respawn 을 기록하는 DI 더블 ──────────

class FakeDriver:
    """SessionDriver 더블 — 모든 호출을 기록하고, marker 를 *주입된 시점* 에 생성한다.

    stop_after_relays: 이 횟수만큼 relay 한 뒤(누적) marker 를 root 에 박는다 → supervisor 가
    다음 stat 에서 STOP 을 관측하게 한다(실 ctx_stop_hook 의 marker write 를 모사).
    always_stop: 매 relay 직후 그 세션에 post-turn marker 를 박는다.
    stop_on_spawn: 매 bootstrap turn 직후 marker 를 박아 병적 spawn-loop 를 모사한다.
    stop_on_even_spawn: 짝수 번째 bootstrap turn 직후에만 marker 를 박는다.
    spawn_reply: bootstrap reply(None이면 구 sid 계약).
    spawn_result_factory: reply 를 선언 계약 타입으로 감싸는 factory(미주입이면 tuple 호환 계약).
    stop_predicate: (relay_index, session_id, text) -> bool. True 면 그 relay 직후 marker 박제.
    relay 별 marker 를 정밀 제어한다(1-based index).
    relay 가 실 claude 를 부르지 않음을 보장(subprocess 폭발 가드 — 여긴 순수 인메모리).
    """

    def __init__(self, root: Path, *, marker_dir, sanitize,
                 stop_after_relays=None, always_stop=False, stop_predicate=None,
                 stop_on_spawn=False, stop_on_even_spawn=False, spawn_reply=None,
                 spawn_result_factory=None):
        self.root = root
        self._marker_dir = marker_dir
        self._sanitize = sanitize
        self.stop_after_relays = stop_after_relays
        self.always_stop = always_stop
        self.stop_predicate = stop_predicate
        self.stop_on_spawn = stop_on_spawn
        self.stop_on_even_spawn = stop_on_even_spawn
        self.spawn_reply = spawn_reply
        self.spawn_result_factory = spawn_result_factory
        self.spawn_results: list[object] = []
        self.spawns: list[str] = []      # spawn 으로 발급된 session_id 목록.
        self.relays: list[tuple[str, str]] = []  # (session_id, text) relay 기록.
        self.closes: list[str] = []
        self._counter = 0

    def _next_sid(self, requested: str) -> str:
        # 실 claude 가 --session-id 를 존중하듯 요청 sid 를 그대로 쓴다(예측 모사).
        return requested

    def spawn(self, cwd: str, session_id: str, bootstrap: str) -> str | tuple[str, str]:
        sid = self._next_sid(session_id)
        self.spawns.append(sid)
        if self.stop_on_spawn or (self.stop_on_even_spawn and len(self.spawns) % 2 == 0):
            self._write_marker(sid)
        if self.spawn_reply is not None:
            if self.spawn_result_factory is not None:
                spawned = self.spawn_result_factory(sid, self.spawn_reply)
                self.spawn_results.append(spawned)
                return spawned
            return sid, self.spawn_reply
        return sid

    def relay_turn(self, session_id: str, text: str) -> str:
        self.relays.append((session_id, text))
        relay_index = len(self.relays)  # 1-based.
        # always_stop: 매 완료 relay 직후 post-turn marker 박제.
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


def test_mark_ctx_post_turn_if_over_boundary_and_under(orch, tmp_path):
    calls = []

    def mark(root, sid):
        calls.append((root, sid))
        return True

    assert orch.mark_ctx_post_turn_if_over(
        tmp_path, "under", 79_999, 100_000, 20, mark=mark,
    ) is False
    assert calls == []
    assert orch.mark_ctx_post_turn_if_over(
        tmp_path, "boundary", 80_000, 100_000, 20, mark=mark,
    ) is True
    assert calls == [(tmp_path, "boundary")]


@pytest.mark.parametrize(
    ("used_tokens", "budget"),
    [(0, 100_000), (-1, 100_000), (90_000, 0), (90_000, -1)],
)
def test_mark_ctx_post_turn_if_over_noop_without_signal(
    orch, tmp_path, used_tokens, budget,
):
    def unexpected_mark(*args):
        raise AssertionError("no-op 조건에서 marker writer 호출")

    assert orch.mark_ctx_post_turn_if_over(
        tmp_path, "sid", used_tokens, budget, 20, mark=unexpected_mark,
    ) is False


def test_mark_ctx_post_turn_if_over_returns_writer_result(orch, tmp_path):
    assert orch.mark_ctx_post_turn_if_over(
        tmp_path, "sid", 90_000, 100_000, 20, mark=lambda *_: False,
    ) is False


def test_relay_budget_effective_threshold_boundary_and_unset(orch):
    assert orch.RELAY_BOOTSTRAP_COST_TOKENS == 40_400
    with pytest.raises(ValueError, match="40400 tok"):
        orch.validate_relay_budget(50_500, 20)  # 유효 임계 40,400: 같으면 거부.
    orch.validate_relay_budget(50_501, 20)      # 유효 임계 40,400.8: 초과면 통과.
    with pytest.raises(ValueError, match="40400 tok"):
        orch.validate_relay_budget(44_888, 10)  # stop_pct 변화도 산식에 반영.
    orch.validate_relay_budget(44_889, 10)
    orch.validate_relay_budget(None, 20)        # 미주입이면 가드 비활성.


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


def test_spawn_reply_is_written_to_out_stream(orch, tmp_path):
    driver = _make_driver(
        orch, tmp_path, spawn_reply="READY", spawn_result_factory=orch.SpawnResult,
    )
    out_stream = io.StringIO()
    rc = orch.Supervisor(driver, root=tmp_path).run_loop(
        "/cwd", io.StringIO(""), out_stream,
    )
    assert rc == 0
    assert len(driver.spawn_results) == 1
    assert isinstance(driver.spawn_results[0], orch.SpawnResult)
    assert out_stream.getvalue() == "READY\n"


def test_respawn_announces_rotation_and_forwards_spawn_reply(orch, tmp_path):
    driver = _make_driver(
        orch, tmp_path, stop_after_relays=1, spawn_reply="READY",
    )
    out_stream = io.StringIO()
    orch.Supervisor(driver, root=tmp_path).run_loop(
        "/cwd", io.StringIO("first\n"), out_stream,
    )
    assert out_stream.getvalue().splitlines() == [
        "READY",
        "reply:first",
        "[relay] ctx 임계 도달 — 세션 회전 (turn 초과)",
        "READY",
    ]


# ── ②b bootstrap 직후 연속 회전 가드 ─────────────────────────

def test_consecutive_respawn_guard_halts_pathological_loop(orch, tmp_path):
    """bootstrap 만으로 매 fresh 세션이 marker 를 남겨도 max 후 종료."""
    driver = _make_driver(orch, tmp_path, stop_on_spawn=True)
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=5)
    in_stream = io.StringIO("poison\n")
    out_stream = io.StringIO()
    rc = sup.run_loop("/cwd", in_stream, out_stream)
    assert rc == orch.GUARD_TRIPPED_RC
    assert len(driver.spawns) == sup.max_consecutive_respawns + 1
    assert driver.relays == []
    assert "무한 회전 차단" in out_stream.getvalue()
    assert "세션 회전 (bootstrap 초과)" in out_stream.getvalue()


def test_consecutive_respawn_guard_respects_custom_max(orch, tmp_path):
    """max_consecutive_respawns 가 작으면 더 일찍 종료(상수 존중)."""
    driver = _make_driver(orch, tmp_path, stop_on_spawn=True)
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=2)
    rc = sup.run_loop("/cwd", io.StringIO("poison\n"), io.StringIO())
    assert rc == orch.GUARD_TRIPPED_RC
    assert len(driver.spawns) == 3


def test_default_max_consecutive_respawns_constant(orch, tmp_path):
    """기본값 = 모듈 상수(생성자 미지정 시)."""
    driver = _make_driver(orch, tmp_path)
    sup = orch.Supervisor(driver, root=tmp_path)
    assert sup.max_consecutive_respawns == orch.MAX_CONSECUTIVE_RESPAWNS
    assert orch.MAX_CONSECUTIVE_RESPAWNS == 5


def test_normal_rotation_does_not_trip_guard(orch, tmp_path):
    """매 완료 turn 후 정상 회전해도 입력을 한 번씩 처리하며 가드는 비발동."""
    driver = _make_driver(orch, tmp_path, always_stop=True)
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=5)
    lines = "".join(f"input{i}\n" for i in range(10))
    rc = sup.run_loop("/cwd", io.StringIO(lines), io.StringIO())
    # 모든 입력을 소비하고 EOF 로 정상 종료 — 가드 비발동(rc=0).
    assert rc == 0
    assert [text for _, text in driver.relays] == [f"input{i}" for i in range(10)]


def test_non_respawn_turn_resets_counter_between_intermittent_spawn_markers(
    orch, tmp_path,
):
    """정상 turn 사이의 간헐 bootstrap marker는 누적되지 않고 매번 리셋된다."""
    driver = _make_driver(
        orch, tmp_path, always_stop=True, stop_on_even_spawn=True,
    )
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=2)
    inputs = [f"input{i}" for i in range(sup.max_consecutive_respawns + 3)]
    rc = sup.run_loop("/cwd", io.StringIO("\n".join(inputs) + "\n"), io.StringIO())
    assert rc == 0
    assert [text for _, text in driver.relays] == inputs


def test_consecutive_respawn_guard_no_reset_would_trip(orch, tmp_path):
    """대조군 — bootstrap marker 가 연속되면 리셋 없이 trip."""
    driver = _make_driver(orch, tmp_path, stop_on_spawn=True)
    sup = orch.Supervisor(driver, root=tmp_path, max_consecutive_respawns=2)
    rc = sup.run_loop("/cwd", io.StringIO("p\n"), io.StringIO())
    assert rc == orch.GUARD_TRIPPED_RC


# ── ③ parse_stream_json (sid/result/usage 추출 + JSONDecodeError 라인 skip) ──

def test_parse_stream_json_extracts_sid_and_result(orch):
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid-init"}),
        json.dumps({"type": "assistant", "session_id": "sid-init"}),
        json.dumps({"type": "result", "result": "the answer", "session_id": "sid-init"}),
    ]
    sid, result, used_tokens = orch.parse_stream_json(lines)
    assert sid == "sid-init"
    assert result == "the answer"
    assert used_tokens is None


def test_parse_stream_json_usage_includes_output_tokens(orch):
    """회전 판정 usage 는 입력 계열 + output 합 — 다음 turn 컨텍스트 기준(codex 게이트 회귀).

    입력 계열이 임계 직하이고 출력이 임계를 넘기는 케이스에서 output 미합산이면 marker 없이
    다음 입력이 전달된다 — output 포함을 고정한다.
    """
    import json as _json
    line = _json.dumps({
        "type": "assistant", "session_id": "s1",
        "message": {"usage": {
            "input_tokens": 100, "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 300, "output_tokens": 50,
        }},
    })
    sid, _reply, used = orch.parse_stream_json([line])
    assert used == 650


def test_parse_stream_json_skips_malformed_lines(orch):
    lines = [
        "not json at all",
        "",
        "{broken",
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid-2"}),
        "another bad line {",
        json.dumps({"type": "result", "result": "ok"}),
    ]
    sid, result, used_tokens = orch.parse_stream_json(lines)
    assert sid == "sid-2"
    assert result == "ok"
    assert used_tokens is None


def test_parse_stream_json_falls_back_to_result_sid(orch):
    """system/init 없으면 result 이벤트의 session_id 로 폴백."""
    lines = [json.dumps({"type": "result", "result": "r", "session_id": "sid-from-result"})]
    sid, result, used_tokens = orch.parse_stream_json(lines)
    assert sid == "sid-from-result"
    assert result == "r"
    assert used_tokens is None


def test_parse_stream_json_empty_and_no_result(orch):
    assert orch.parse_stream_json([]) == (None, None, None)
    # init 만 있고 result 없음 → sid 만.
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "s"})]
    assert orch.parse_stream_json(lines) == ("s", None, None)


def test_parse_stream_json_ignores_non_dict_events(orch):
    """JSON 배열/스칼라 라인은 dict 가 아니라 skip(robust)."""
    lines = ["[1, 2, 3]", "42", json.dumps({"type": "result", "result": "x"})]
    sid, result, used_tokens = orch.parse_stream_json(lines)
    assert sid is None and result == "x" and used_tokens is None


def test_parse_stream_json_extracts_measured_assistant_usage(orch):
    """Claude 2.1.222 실측 wire의 입력·cache 생성·cache 읽기 합을 반환한다."""
    lines = [json.dumps({
        "type": "assistant",
        "message": {"usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 31_641,
            "cache_read_input_tokens": 0,
        }},
    })]
    assert orch.parse_stream_json(lines) == (None, None, 31_643)


def test_parse_stream_json_usage_absent_is_none(orch):
    lines = [
        json.dumps({"type": "assistant", "message": {}}),
        json.dumps({"type": "result", "result": "ok"}),
    ]
    assert orch.parse_stream_json(lines) == (None, "ok", None)


def test_parse_stream_json_uses_last_assistant_partial_usage(orch):
    """마지막 assistant가 권위이며 부분 wire는 존재하는 음이 아닌 정수만 합산한다."""
    lines = [
        json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 90_000,
        }}}),
        json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 7,
        }}}),
    ]
    assert orch.parse_stream_json(lines) == (None, None, 7)


# ── ④ post-turn marker 단일 의미론(재전송 없음) ────────────────

def test_marker_turn_is_not_resent_to_new_session(orch, tmp_path):
    """marker 를 남긴 완료 turn 은 회전 후 재전송하지 않는다."""
    driver = _make_driver(orch, tmp_path, stop_after_relays=1)
    sup = orch.Supervisor(driver, root=tmp_path)
    in_stream = io.StringIO("trigger\nfollowup\n")
    out_stream = io.StringIO()
    sup.run_loop("/cwd", in_stream, out_stream)

    new_sid = driver.spawns[1]
    new_session_relays = [text for sid, text in driver.relays if sid == new_sid]
    assert new_session_relays == ["followup"]
    assert [text for _, text in driver.relays] == ["trigger", "followup"]


def test_marker_payload_is_ignored_and_input_is_not_resent(orch, tmp_path):
    """구 pre-turn payload도 존재만으로 회전하며 처리된 입력은 재전송하지 않는다."""
    driver = _make_driver(orch, tmp_path, stop_after_relays=1)
    sup = orch.Supervisor(driver, root=tmp_path)
    sup.run_loop("/cwd", io.StringIO("trigger\nsecond\n"), io.StringIO())
    assert [text for _, text in driver.relays] == ["trigger", "second"]


def test_run_loop_has_no_resend_or_payload_classification_path(orch):
    import inspect

    source = inspect.getsource(orch.Supervisor.run_loop)
    # 아래 문자열은 구현 identifier rename 시 이 부재 검증과 함께 갱신한다.
    assert "pending" not in source
    assert "is_resend" not in source
    assert "stop_marker_is_post_turn" not in source


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


# ── task 정체성 (b) 명시 전달 (F7·T-0356) — 재진입 프롬프트 --task 주입·respawn forward ──

def test_build_bootstrap_prompt_task_injects_flag(orch):
    """build_bootstrap_prompt(task) 가 재진입 프롬프트에 `/pm-bootstrap --task <name>` 실값을 박는다.

    task 없으면 bare `/pm-bootstrap`(현행 BOOTSTRAP_PROMPT 와 byte-동일·하위호환)."""
    assert orch.build_bootstrap_prompt() == orch.BOOTSTRAP_PROMPT
    assert "/pm-bootstrap" in orch.BOOTSTRAP_PROMPT and "--task" not in orch.BOOTSTRAP_PROMPT
    assert "/pm-bootstrap --task mytask" in orch.build_bootstrap_prompt("mytask")


def test_supervisor_no_task_bare_bootstrap_byte_compat(orch, tmp_path):
    """task 미지정 Supervisor 는 bare `/pm-bootstrap`(현행 byte-호환·슬롯/솔로)."""
    driver = _make_driver(orch, tmp_path)
    sup = orch.Supervisor(driver, root=tmp_path)
    assert sup.bootstrap == orch.BOOTSTRAP_PROMPT
    assert "--task" not in sup.bootstrap


def test_supervisor_explicit_bootstrap_override_wins(orch, tmp_path):
    """명시 bootstrap override(테스트/커스텀)가 task 파생보다 우선 — 하위호환 seam 보존."""
    driver = _make_driver(orch, tmp_path)
    sup = orch.Supervisor(driver, root=tmp_path, bootstrap="CUSTOM", task="mytask")
    assert sup.bootstrap == "CUSTOM"


def test_supervisor_task_bakes_and_forwards_on_respawn(orch, tmp_path):
    """Supervisor(task=...) 가 재진입 프롬프트에 --task 를 baked-in 하고 spawn/respawn 이 forward 한다.

    (b) 명시 전달 — 회전된 새 PM 이 같은 task 를 resume. spawn·respawn 모두 self.bootstrap 을 쓰므로
    respawn 후에도 task 가 유지된다(cwd 추론 없이·stateless). task 는 별도 인스턴스 필드로 retain 하지
    않는다(stateless 불변식 — bootstrap 에 흡수)."""
    seen: list[str] = []

    class _RecordDriver(FakeDriver):
        def spawn(self, cwd, session_id, bootstrap):
            seen.append(bootstrap)
            return super().spawn(cwd, session_id, bootstrap)

    driver = _RecordDriver(
        tmp_path, marker_dir=orch.MARKER_DIR, sanitize=orch._sanitize_session_id,
        stop_after_relays=1,
    )
    sup = orch.Supervisor(driver, root=tmp_path, task="mytask")
    assert "--task mytask" in sup.bootstrap
    # stateless 불변식 — task 는 인스턴스 필드로 안 남는다(bootstrap 에만 흡수).
    assert "task" not in vars(sup)
    # 한 입력 → relay 1회 후 marker → respawn(재전송 없음). 두 spawn 모두 task 포함.
    sup.run_loop("/cwd", io.StringIO("hello\n"), io.StringIO())
    assert len(seen) >= 2
    assert all("/pm-bootstrap --task mytask" in b for b in seen)


# ── claude driver CLI --task 수용/forward (F7·T-0356·4곳 중 claude) ─────────────

def test_claude_driver_parser_accepts_task(driver_mod):
    """claude driver build_parser 가 `--task <이름>` 을 수용(기본 None)."""
    parser = driver_mod.build_parser()
    assert parser.parse_args(["--task", "mytask"]).task == "mytask"
    assert parser.parse_args([]).task is None


def test_claude_main_resolves_and_injects_ctx_config(driver_mod, monkeypatch, tmp_path):
    """main 이 claude local.conf 예산/임계를 해소·검증하고 driver/Supervisor 에 주입한다."""
    captured: dict = {}

    class _FakeSup:
        def __init__(self, driver, *, root, task=None):
            captured["task"] = task
            captured["driver"] = driver
            captured["events"].append("supervisor")

        def run_loop(self, cwd, in_stream, out_stream):
            return 0

    class _FakeEngine:
        Supervisor = _FakeSup
        SpawnResult = staticmethod(lambda sid, reply: (sid, reply))

        @staticmethod
        def parse_stream_json(lines):
            return None, None, None

        @staticmethod
        def mark_ctx_post_turn_if_over(*args):
            captured["marked"] = args

        @staticmethod
        def validate_relay_budget(budget, stop_pct):
            captured["validated"] = (budget, stop_pct)
            captured["events"].append("validate")

    captured["events"] = []
    monkeypatch.setattr(driver_mod, "_load_engine", lambda: (_FakeEngine(), tmp_path))
    monkeypatch.setattr(driver_mod.ctx_guard, "load_local_config", lambda root: {
        "ctx_window_tokens_claude": "123456", "ctx_stop_pct": "17",
    })
    monkeypatch.setattr(
        driver_mod.ctx_guard, "resolve_budget",
        lambda conf, harness: 123_456 if harness == "claude" else 0,
    )
    monkeypatch.setattr(
        driver_mod.ctx_guard, "ctx_thresholds",
        lambda conf: {"nudge_pct": 30, "stop_pct": 17},
    )
    rc = driver_mod.main(["--task", "mytask", "--cwd", str(tmp_path)])
    assert rc == 0 and captured["task"] == "mytask"
    assert captured["validated"] == (123_456, 17)
    assert captured["events"] == ["validate", "supervisor"]
    driver = captured["driver"]
    assert driver._ctx_budget == 123_456 and driver._stop_pct == 17
    assert driver._root == tmp_path
    assert driver._mark_stop is _FakeEngine.mark_ctx_post_turn_if_over


def test_new_session_id_unique_uuid(orch):
    a, b = orch.new_session_id(), orch.new_session_id()
    assert a != b
    assert len(a) == 36 and a.count("-") == 4  # uuid4 형태.


def test_watched_popen_defaults_stdin_to_devnull(orch, monkeypatch):
    """input_text 없는 공용 watchdog child 는 supervisor stdin 을 상속하지 않는다."""
    captured = {}

    class _FakeProc:
        pid = 4321
        returncode = 0
        stdin = None

        def __init__(self):
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    def fake_popen(argv, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    proc = orch._WatchedPopen(["child"], input_text=None)
    proc.communicate(timeout=1)
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL


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


# ── ADR-0057/T-0318 — 정체성 인자(--repo/--slot) 통일 감사 (pm-internal) ────────


def test_engine_core_exposes_no_identity_cli_surface():
    """pm_relay.py(엔진 core)는 정체성(`--repo`/`--slot`/구 `--session`/`--worktree-slot`) CLI 플래그를
    전혀 노출하지 않는다(T-0318 감사 결론) — `argparse` 자체를 import 하지 않는 순수 라이브러리다
    (하니스별 CLI 는 어댑터 `pm_orch_claude.py`/`pm_orch_opencode.py`(templates/) 몫 — 엔진은
    `Supervisor.run_loop(cwd, ...)` 로 호출자가 이미 해소한 cwd 를 받는다). ADR-0057 의 "구 alias
    제거"·"bare --slot fail-loud" DoD 는 이 파일에 **적용 대상이 없다**(제거할 identity alias 가
    처음부터 없음) — 재발 방지 가드로 이 감사 결론을 고정한다.

    `--session-id`(claude/opencode 대화-연속성 id·`new_session_id`/`_sanitize_session_id`)는
    multi-PM repo/slot 정체성과 무관한 별개 개념이라 이 감사 대상에서 명시 제외한다(음의
    lookahead 로 `--session-id` 는 건드리지 않고 `--session`(단독)·`--worktree-slot`·
    `--session-num` 만 스캔).
    """
    text = (TOOLS / "pm_relay.py").read_text(encoding="utf-8")
    assert "import argparse" not in text, "엔진 core 에 argparse CLI 가 생기면 어댑터/엔진 경계가 흐려진다."
    assert re.search(r"--session(?!-id)\b", text) is None, \
        "정체성 --session 플래그가 있으면 안 된다(구 alias·어댑터 몫)."
    for legacy in ("--worktree-slot", "--session-num"):
        assert legacy not in text, f"엔진 core 에 구 alias {legacy!r} 가 있으면 안 된다."


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
    spawned = driver.spawn("/repo/root", "uuid-123", "bootstrap text")
    assert isinstance(spawned, orch.SpawnResult)
    assert spawned == orch.SpawnResult("uuid-123", "READY")
    cmd = captured["cmd"]
    assert "--session-id" in cmd and "uuid-123" in cmd
    assert "--resume" not in cmd  # spawn 은 resume 안 함.
    assert captured["kwargs"]["cwd"] == "/repo/root"  # child cwd 격리.


def test_claude_driver_turn_closes_supervisor_stdin(orch, driver_mod):
    """파이프 입력 relay 에서 claude child 가 supervisor 입력을 상속·소비하지 않는다."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCompleted(json.dumps({"type": "result", "result": "ok"}))

    driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, runner=fake_run)
    assert driver.relay_turn("uuid", "prompt") == "ok"
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL


def test_claude_driver_rc0_empty_stdout_warns(orch, driver_mod, capsys):
    driver = driver_mod.ClaudeCliDriver(
        orch.parse_stream_json, runner=lambda *args, **kwargs: _FakeCompleted("")
    )
    assert driver.relay_turn("uuid", "prompt") == ""
    assert capsys.readouterr().err == (
        "[pm-orch] claude turn 무출력(rc 0) — stdin/파싱 점검\n"
    )


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


def test_claude_driver_marks_ctx_after_spawn_and_relay(orch, driver_mod, tmp_path):
    """usage 임계 초과 turn은 spawn/relay 양쪽에서 엔진 회전 헬퍼로 전달된다."""
    calls = []

    def mark_stop(root, sid, used_tokens, budget, stop_pct):
        calls.append((root, sid, used_tokens, budget, stop_pct))
        return True

    def fake_run(cmd, **kwargs):
        sid = "uuid-high" if "--session-id" in cmd else None
        events = [json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 80_000,
                "cache_read_input_tokens": 0,
            }},
        })]
        if sid:
            events.insert(0, json.dumps({
                "type": "system", "subtype": "init", "session_id": sid,
            }))
        events.append(json.dumps({"type": "result", "result": "ok"}))
        return _FakeCompleted("\n".join(events))

    driver = driver_mod.ClaudeCliDriver(
        orch.parse_stream_json,
        ctx_budget=100_000,
        stop_pct=20,
        root=tmp_path,
        mark_stop=mark_stop,
        spawn_result=orch.SpawnResult,
        runner=fake_run,
    )
    spawned = driver.spawn("/repo/root", "uuid-high", "boot")
    assert spawned == orch.SpawnResult("uuid-high", "ok")
    assert driver.relay_turn("uuid-high", "next") == "ok"
    assert calls == [
        (tmp_path, "uuid-high", 80_002, 100_000, 20),
        (tmp_path, "uuid-high", 80_002, 100_000, 20),
    ]


@pytest.mark.parametrize("operation", ["spawn", "relay"])
def test_claude_driver_real_marker_integration_over_threshold(
    orch, driver_mod, tmp_path, operation,
):
    """실 엔진 mark_ctx_post_turn_if_over DI와 임계 초과 wire usage가 spawn/relay
    양쪽에서 supervisor가 stat 할 수 있는 post-turn marker를 박는다.
    """
    sid = f"uuid-real-{operation}"

    def fake_run(cmd, **kwargs):
        events = []
        if "--session-id" in cmd:
            events.append(json.dumps({
                "type": "system", "subtype": "init", "session_id": sid,
            }))
        events.extend([
            json.dumps({
                "type": "assistant",
                "message": {"usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 8_000,
                    "cache_read_input_tokens": 0,
                }},
            }),
            json.dumps({"type": "result", "result": "ok"}),
        ])
        return _FakeCompleted("\n".join(events))

    driver = driver_mod.ClaudeCliDriver(
        orch.parse_stream_json,
        ctx_budget=10_000,
        stop_pct=20,
        root=tmp_path,
        mark_stop=orch.mark_ctx_post_turn_if_over,
        spawn_result=orch.SpawnResult,
        runner=fake_run,
    )
    if operation == "spawn":
        assert driver.spawn("/repo/root", sid, "boot") == orch.SpawnResult(sid, "ok")
    else:
        assert driver.relay_turn(sid, "work") == "ok"
    assert orch.stop_marker_present(tmp_path, sid) is True


@pytest.mark.parametrize("control", ["helper-missing", "under-threshold"])
def test_claude_driver_real_marker_integration_old_red_controls(
    orch, driver_mod, tmp_path, control,
):
    """old-red 대조: 헬퍼 미주입 또는 임계 미달이면 실 marker는 없다."""
    sid = f"uuid-control-{control}"
    used = 8_001 if control == "helper-missing" else 7_999
    runner = lambda *args, **kwargs: _FakeCompleted("\n".join([
        json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": used,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }},
        }),
        json.dumps({"type": "result", "result": "ok"}),
    ]))
    driver = driver_mod.ClaudeCliDriver(
        orch.parse_stream_json,
        ctx_budget=10_000,
        stop_pct=20,
        root=tmp_path,
        mark_stop=(None if control == "helper-missing" else orch.mark_ctx_post_turn_if_over),
        runner=runner,
    )
    assert driver.relay_turn(sid, "work") == "ok"
    assert orch.stop_marker_present(tmp_path, sid) is False


def test_claude_driver_warns_once_when_usage_missing_with_complete_ctx_di(
    orch, driver_mod, tmp_path, capsys,
):
    """ctx DI는 완비됐는데 usage만 소실되면 never-block advisory를 1회 표면화한다."""
    driver = driver_mod.ClaudeCliDriver(
        orch.parse_stream_json,
        ctx_budget=100_000,
        stop_pct=20,
        root=tmp_path,
        mark_stop=orch.mark_ctx_post_turn_if_over,
    )
    driver._maybe_mark_ctx("sid", None)
    driver._maybe_mark_ctx("sid", None)
    assert capsys.readouterr().err == (
        "[pm-orch] claude usage 신호 소실 — ctx post-turn 가드를 판정할 수 없음\n"
    )


def test_claude_driver_ctx_guard_noop_when_any_signal_missing(
    orch, driver_mod, tmp_path,
):
    """usage 또는 ctx DI 신호 하나라도 없으면 회전 헬퍼를 호출하지 않는다."""
    def unexpected_mark(*args):
        raise AssertionError("불완전한 ctx 신호에서 mark_stop 호출")

    complete = {
        "ctx_budget": 100_000,
        "stop_pct": 20,
        "root": tmp_path,
        "mark_stop": unexpected_mark,
    }
    for missing in complete:
        kwargs = {**complete, missing: None}
        driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, **kwargs)
        driver._maybe_mark_ctx("sid", 90_000)
    driver = driver_mod.ClaudeCliDriver(orch.parse_stream_json, **complete)
    driver._maybe_mark_ctx("sid", None)


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

PM_RELAY_LIVE = os.environ.get("PM_RELAY_LIVE") == "1"


@pytest.mark.skipif(
    not PM_RELAY_LIVE or not shutil.which("claude"),
    reason="통합 스모크 — PM_RELAY_LIVE=1 + claude CLI 필요(기본 skip·CI green 불변).",
)
def test_live_spawn_relay_swap_smoke(orch, driver_mod, tmp_path):
    """실 claude 1회 e2e: spawn(`--session-id <uuid>`) → relay(≥2턴 resume·turn2 가 turn1
    사실 회상) → marker 강제 생성 → swap → relay 완주. frugal haiku·단발 프롬프트.

    핵심 sid 검증: spawn 에 준 uuid == stream-json system/init session_id 인지(같으면 hook
    도 그 uuid 로 marker 를 쓸 것 — marker 예측 가정 확증). 루트 `.claude/` 엔 ctx hook 이
    없어 실 ctx-STOP 은 못 트리거하므로 marker 는 강제 생성으로 swap 만 검증(deferred 부분).
    """
    driver = driver_mod.ClaudeCliDriver(
        orch.parse_stream_json,
        spawn_result=orch.SpawnResult,
        model="claude-haiku-4-5",
    )
    requested_sid = orch.new_session_id()

    # ── spawn: --session-id 로 세션 id 지정 + 사실 심기 ──
    spawned = driver.spawn(
        str(tmp_path),
        requested_sid,
        "Remember this code word for our chat: MANGO77. Reply with exactly: STORED",
    )
    observed = spawned.session_id
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
    new_spawned = driver.spawn(
        str(tmp_path),
        new_sid,
        "Context handoff: the prior code word was MANGO77. Reply with exactly: CONTINUED",
    )
    new_observed = new_spawned.session_id
    assert new_observed == new_sid, "swap 후 새 세션 sid 예측 실패"
    swap_reply = driver.relay_turn(new_observed, "Reply with only the prior code word.")
    assert "MANGO77" in swap_reply.upper(), f"swap 후 relay 실패 — reply={swap_reply!r}"
