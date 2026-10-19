"""opencode relay (ADR-0009 · T-0048) 단위 테스트.

엔진 core 의 `parse_opencode_json`(claude `parse_stream_json` 대칭·순수)과 opencode driver
(`pm_orch_opencode.py`·SessionDriver 구현)를 importlib 로 직접 검증한다. 실 opencode 불요 —
FakeRunner DI(claude driver 테스트 패턴) 로 subprocess 폭발 없이 CLI 조립/파싱만 본다.

검증 축 (ticket DoD):
  ① parse_opencode_json 순수 — sessionID→sid · type:"text" part.text 누적→reply · 비-JSON skip · edge.
  ② driver spawn — 엔진 uuid4 **무시** 하고 출력 파싱 sid 반환 · `--agent` · `--dir` 격리 · `--format json`.
  ③ driver relay_turn — `-s <sid>` 로 resume · reply 파싱 · spawn cwd 재사용.
  ④ driver close — no-op(세션 cwd 메타 정리).
  ⑤ subprocess 폭발 가드 — driver 가 실 opencode 를 부르지 않음(FakeRunner).

엔진 Supervisor 재사용(spawn→relay→respawn 회전)은 `test_pm_relay.py` 가 FakeDriver 로
이미 커버 — 여기선 opencode 고유 표면(파서·driver)만. 엔진 운영코드는 무수정(ADR-0009 불변식).

+ live smoke — 실 opencode(`@skipif`·기본 skip·로컬 ollama). spawn→relay(≥2턴)→marker 강제→
  swap→relay 완주 + **driver-파싱 sid == plugin-write marker sid 동일성 실측**(핵심 가정·silent-fail 방어).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pm_engine.app.session import relay
from pm_engine.domain import conf_schema
from pm_engine.infra import raw_output
from pm_engine.infra.harness import contract, watchdog

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
OPENCODE_DRIVER = REPO / "templates" / "opencode" / ".opencode" / "pm_orch_opencode.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def orch():
    return relay


@pytest.fixture(scope="module")
def driver_mod():
    return _load("pm_orch_opencode", OPENCODE_DRIVER)


# ── opencode JSON 이벤트 헬퍼 (실측 형식·opencode 1.17.6) ─────────────────────
import json as _json  # noqa: E402


def _ev(**kw) -> str:
    return _json.dumps(kw)


def _text_ev(sid: str, text: str) -> str:
    return _json.dumps(
        {"type": "text", "sessionID": sid, "part": {"type": "text", "text": text}}
    )


# ── ① parse_opencode_json (순수·sid/reply 추출·비-JSON skip·edge) ─────────────

def test_parse_opencode_extracts_sid_and_reply(orch):
    """실측 형식: 모든 이벤트 top-level sessionID · type:text part.text → reply."""
    sid = "ses_1326c468affeQKslN5GD1FbHRR"
    lines = [
        _ev(type="step_start", sessionID=sid, part={"type": "step-start"}),
        _text_ev(sid, "PONG"),
        _ev(type="step_finish", sessionID=sid, part={"type": "step-finish"}),
    ]
    got_sid, reply, used_tokens = contract.parse_opencode_json(lines)
    assert got_sid == sid
    assert reply == "PONG"
    assert used_tokens is None


def test_parse_opencode_accumulates_multiple_text_parts(orch):
    """멀티 text part 는 등장 순서대로 누적 — 한 답변이 여러 part 로 쪼개져 와도 합친다."""
    sid = "ses_abc"
    lines = [_text_ev(sid, "Hello "), _text_ev(sid, "world"), _text_ev(sid, "!")]
    got_sid, reply, _ = contract.parse_opencode_json(lines)
    assert got_sid == sid
    assert reply == "Hello world!"


def test_parse_opencode_sid_from_first_event(orch):
    """sid 는 첫 등장 이벤트에서 잡는다(text 이벤트 전의 step_start 도 sessionID 보유)."""
    sid = "ses_first"
    lines = [
        _ev(type="step_start", sessionID=sid, part={}),
        _text_ev(sid, "reply"),
    ]
    got_sid, _, _ = contract.parse_opencode_json(lines)
    assert got_sid == sid


def test_parse_opencode_skips_malformed_lines(orch):
    """비-JSON / 부분 라인 skip (claude 파서와 동일 robust 정책)."""
    sid = "ses_x"
    lines = [
        "not json at all",
        "",
        "{broken",
        _ev(type="step_start", sessionID=sid, part={}),
        "noise {",
        _text_ev(sid, "answer"),
    ]
    got_sid, reply, _ = contract.parse_opencode_json(lines)
    assert got_sid == sid
    assert reply == "answer"


def test_parse_opencode_ignores_non_dict_events(orch):
    """JSON 배열/스칼라 라인은 dict 가 아니라 skip(robust)."""
    sid = "ses_y"
    lines = ["[1,2,3]", "42", _text_ev(sid, "x")]
    got_sid, reply, _ = contract.parse_opencode_json(lines)
    assert got_sid == sid and reply == "x"


def test_parse_opencode_empty_and_no_text(orch):
    assert contract.parse_opencode_json([]) == (None, None, None)
    # sid 만 있고 text part 0 → reply None.
    lines = [_ev(type="step_start", sessionID="ses_z", part={})]
    assert contract.parse_opencode_json(lines) == ("ses_z", None, None)


def test_parse_opencode_text_event_without_part_text(orch):
    """type:text 인데 part.text 누락/비-문자열이면 누적 안 함(edge robust)."""
    sid = "ses_w"
    lines = [
        _json.dumps({"type": "text", "sessionID": sid, "part": {"type": "text"}}),
        _json.dumps({"type": "text", "sessionID": sid, "part": None}),
        _json.dumps({"type": "text", "sessionID": sid}),  # part 키 자체 없음.
        _text_ev(sid, "real"),
    ]
    got_sid, reply, _ = contract.parse_opencode_json(lines)
    assert got_sid == sid and reply == "real"


def test_parse_opencode_no_sessionid(orch):
    """sessionID 없는 이벤트만 있으면 sid None(폴백은 driver 가 처리)."""
    lines = [_json.dumps({"type": "text", "part": {"type": "text", "text": "x"}})]
    sid, reply, _ = contract.parse_opencode_json(lines)
    assert sid is None and reply == "x"


def test_parse_opencode_step_finish_tokens_fixture(orch):
    """opencode 1.18.5 실측 fixture는 유효한 total을 정규화된 세부값보다 우선한다."""
    tokens = {
        "total": 8131,
        "input": 128,
        "output": 3,
        "reasoning": 0,
        "cache": {"write": 1000, "read": 7000},
    }
    lines = [_text_ev("ses_wire", "ok"), _ev(
        type="step_finish", sessionID="ses_wire",
        part={"type": "step-finish", "tokens": tokens},
    )]
    assert contract.parse_opencode_json(lines) == ("ses_wire", "ok", 8131)


def test_parse_opencode_step_finish_without_tokens_is_none(orch):
    """step_finish는 있어도 part.tokens가 없으면 usage 신호 없음(None)이다."""
    lines = [_ev(
        type="step_finish", sessionID="ses_none", part={"type": "step-finish"},
    )]
    assert contract.parse_opencode_json(lines) == ("ses_none", None, None)


def test_parse_opencode_last_step_finish_wins_and_fallback_includes_cache(orch):
    """마지막 step_finish가 권위이며 total 부재 fallback은 cache read/write도 합산한다."""
    lines = [
        _ev(type="step_finish", sessionID="ses_last", part={
            "tokens": {"input": 900, "output": 80, "reasoning": 20},
        }),
        _ev(type="step_finish", sessionID="ses_last", part={
            "tokens": {
                "input": 50, "output": 10, "reasoning": 5,
                "cache": {"write": 700, "read": 600},
            },
        }),
    ]
    assert contract.parse_opencode_json(lines) == ("ses_last", None, 1365)


def test_parse_opencode_fallback_rejects_non_integer_token_value(orch):
    """fallback 구성값 하나라도 비정수면 합계를 만들지 않는다(유효성 가드 old-red)."""
    lines = [_ev(type="step_finish", sessionID="ses_bad_int", part={
        "tokens": {
            "input": "50", "output": 10, "reasoning": 5,
            "cache": {"write": 7, "read": 6},
        },
    })]
    assert contract.parse_opencode_json(lines) == ("ses_bad_int", None, None)


def test_parse_opencode_fallback_rejects_non_dict_cache(orch):
    """cache가 dict가 아니면 read/write 누락을 0으로 오인하지 않고 None으로 거부한다."""
    lines = [_ev(type="step_finish", sessionID="ses_bad_cache", part={
        "tokens": {"input": 50, "output": 10, "reasoning": 5, "cache": []},
    })]
    assert contract.parse_opencode_json(lines) == ("ses_bad_cache", None, None)


def test_parse_opencode_last_step_finish_missing_tokens_clears_usage(orch):
    """마지막 step_finish에 tokens가 없으면 앞 turn 값을 재사용하지 않는다."""
    lines = [
        _ev(type="step_finish", sessionID="ses_last", part={
            "tokens": {"input": 100, "output": 10, "reasoning": 1},
        }),
        _ev(type="step_finish", sessionID="ses_last", part={"type": "step-finish"}),
    ]
    assert contract.parse_opencode_json(lines) == ("ses_last", None, None)


# ── opencode driver (FakeRunner DI·실 opencode 무호출) ───────────────────────

class _FakeCompleted:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _json_stream(sid: str, reply: str, *, tokens: dict | None = None) -> str:
    finish = {"type": "step-finish"}
    if tokens is not None:
        finish["tokens"] = tokens
    return "\n".join(
        [
            _ev(type="step_start", sessionID=sid, part={"type": "step-start"}),
            _text_ev(sid, reply),
            _ev(type="step_finish", sessionID=sid, part=finish),
        ]
    )


# ── ② driver spawn (엔진 uuid4 무시·출력 sid 반환·--agent·--dir·--format json) ─

def test_opencode_driver_spawn_ignores_uuid_returns_parsed_sid(orch, driver_mod, tmp_path):
    """spawn 이 엔진 uuid4 인자를 **무시** 하고 opencode 출력에서 파싱한 sid 를 반환한다
    (opencode sid 사전지정 불가·실측 → 출력 파싱이 권위)."""
    captured = {}
    opencode_sid = "ses_OPENCODE_ISSUED"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        captured["prompt"] = Path(cmd[cmd.index("--file") + 1]).read_text(encoding="utf-8")
        return _FakeCompleted(_json_stream(opencode_sid, "READY"))

    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run, spawn_result=orch.SpawnResult,
    )
    engine_uuid = "11111111-2222-3333-4444-555555555555"
    spawned = driver.spawn(str(tmp_path), engine_uuid, "bootstrap text")
    # 엔진 uuid4 가 아니라 opencode 가 발급한 sid 를 반환.
    assert isinstance(spawned, orch.SpawnResult)
    assert spawned.session_id == opencode_sid
    assert spawned.session_id != engine_uuid
    assert spawned.reply == "READY"
    cmd = captured["cmd"]
    prompt_file = Path(cmd[cmd.index("--file") + 1])
    assert cmd[:5] == ["opencode", "run", contract.OPENCODE_ATTACHED_MSG, "--file", str(prompt_file)]
    assert cmd[cmd.index("--format") + 1] == "json"
    assert "--agent" in cmd and "pm" in cmd  # PM primary 기본 spawn 타깃(T-0045).
    assert "--dir" in cmd and str(tmp_path) in cmd  # child cwd 격리.
    assert "-s" not in cmd  # 첫 spawn 은 resume 안 함.
    # 프롬프트는 실행 인자가 아니라 `--dir` 안 프롬프트 파일이다(cross 위임과 같은 `--file` 전달).
    assert captured["prompt"] == "bootstrap text" and "bootstrap text" not in cmd
    assert prompt_file.parent.parent == tmp_path / ".project_manager" / ".local" / "tmp"
    assert not prompt_file.exists(), "turn 뒤 프롬프트 파일이 남았다"


def test_opencode_driver_spawn_agent_arg(orch, driver_mod, tmp_path):
    """`--agent` 인자화 — build 폴백 등 커스텀 agent 지정 가능."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(_json_stream("ses_b", "READY"))

    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, agent="build", runner=fake_run
    )
    driver.spawn(str(tmp_path), "uuid", "boot")
    cmd = captured["cmd"]
    assert "--agent" in cmd
    assert cmd[cmd.index("--agent") + 1] == "build"


def test_opencode_driver_turn_closes_supervisor_stdin(orch, driver_mod):
    """파이프 입력 relay 에서 opencode child 가 supervisor 입력을 상속·소비하지 않는다."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCompleted(_json_stream("ses_stdin", "ok"))

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    assert driver.relay_turn("ses_stdin", "prompt") == "ok"
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL


def test_opencode_driver_rc0_empty_stdout_warns(orch, driver_mod, capsys):
    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=lambda *args, **kwargs: _FakeCompleted("")
    )
    assert driver.relay_turn("ses", "prompt") == ""
    assert capsys.readouterr().err == (
        "[pm-orch] opencode turn 무출력(rc 0) — stdin/파싱 점검\n"
    )


def test_opencode_driver_spawn_sid_parse_failure_raises(orch, driver_mod, tmp_path):
    """sid 파싱 실패 = 치명·명시 중단 (codex T-0048).

    opencode 는 sid 사전지정 불가라 엔진 uuid 폴백 시 그 세션이 존재하지 않아 다음 relay_turn 의
    `-s <uuid>` 가 "Session not found" → 연속성 침묵 파손. 따라서 폴백 대신 RuntimeError 로
    명시 중단한다(relay 는 유효 세션 없이 못 돈다)."""
    import pytest

    def fake_run(cmd, **kwargs):
        return _FakeCompleted("no sessionID here\nplain text")  # 비-JSON → sid None.

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    with pytest.raises(RuntimeError, match="sessionID"):
        driver.spawn(str(tmp_path), "engine-uuid-ignored", "boot")


# ── ③ driver relay_turn (-s resume·reply 파싱·spawn cwd 재사용) ───────────────

def test_opencode_driver_relay_uses_session_flag(orch, driver_mod):
    """relay_turn 이 `-s <sid>` 로 같은 세션을 resume 하고 reply 를 반환."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(_json_stream("ses_r", "hello back"))

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    reply = driver.relay_turn("ses_r", "ping")
    assert reply == "hello back"
    cmd = captured["cmd"]
    assert "-s" in cmd
    assert cmd[cmd.index("-s") + 1] == "ses_r"
    assert "--agent" not in cmd  # resume 은 새 세션 발급 안 함.
    assert "ping" not in cmd and "--file" in cmd


def test_opencode_driver_relay_reuses_spawn_cwd(orch, driver_mod, tmp_path):
    """relay 가 spawn 때의 cwd(--dir)를 재사용한다 — child cwd 격리 일관."""
    dirs = []

    def fake_run(cmd, **kwargs):
        # --dir 다음 토큰을 기록.
        if "--dir" in cmd:
            dirs.append(cmd[cmd.index("--dir") + 1])
        else:
            dirs.append(None)
        if "--agent" in cmd:
            return _FakeCompleted(_json_stream("ses_cwd", "READY"))
        return _FakeCompleted(_json_stream("ses_cwd", "ok"))

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    spawned = driver.spawn(str(tmp_path), "uuid", "boot")
    sid, _ = spawned
    driver.relay_turn(sid, "msg")
    assert dirs == [str(tmp_path), str(tmp_path)]


def test_opencode_driver_relay_unknown_session_no_dir(orch, driver_mod):
    """모르는 세션(메타 없음) relay 는 --dir 없이도 fail-soft(빈 cwd 메타)."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(_json_stream("ses_u", "r"))

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    reply = driver.relay_turn("ses_never_spawned", "x")
    assert reply == "r"
    assert "--dir" not in captured["cmd"]  # cwd 메타 없으면 --dir 생략.


# ── ④ driver close (no-op·세션 cwd 메타 정리) ────────────────────────────────

def test_opencode_driver_close_is_noop_and_clears_meta(orch, driver_mod, tmp_path):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(_json_stream("ses_c", "READY"))

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    spawned = driver.spawn(str(tmp_path), "uuid", "boot")
    sid, _ = spawned
    assert sid in driver._session_cwd
    assert driver.close(sid) is None
    assert sid not in driver._session_cwd  # 메타 정리.
    # 모르는 세션 close 도 fail-soft(예외 없음).
    assert driver.close("ses_never") is None


# ── ⑤ driver fail-soft (timeout·실행실패) ────────────────────────────────────

def test_opencode_driver_relay_timeout_returns_empty(orch, driver_mod):
    """subprocess timeout 은 fail-soft — 빈 reply(루프 안 죽음)."""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    assert driver.relay_turn("ses", "x") == ""


def test_opencode_driver_relay_oserror_returns_empty(orch, driver_mod):
    """opencode 바이너리 부재(OSError)도 fail-soft."""
    def fake_run(cmd, **kwargs):
        raise OSError("opencode not found")

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    assert driver.relay_turn("ses", "x") == ""


# ── ⑥ driver post-turn ctx 판정 (spawn/relay usage → 엔진 헬퍼) ──────────────

def test_opencode_driver_spawn_marks_after_turn_and_returns_spawn_result(
    orch, driver_mod, tmp_path,
):
    calls = []

    def mark_stop(root, sid, used, budget, precompact_at_pct):
        calls.append((root, sid, used, budget, precompact_at_pct))
        return True

    tokens = {"input": 8128, "output": 3, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd,
        ctx_budget=10_000,
        precompact_at_pct=80,
        root=tmp_path,
        mark_stop=mark_stop,
        spawn_result=orch.SpawnResult,
        runner=lambda *args, **kwargs: _FakeCompleted(
            _json_stream("ses_spawn_usage", "READY", tokens=tokens)
        ),
    )
    result = driver.spawn(str(tmp_path), "ignored-uuid", "boot")
    assert result == orch.SpawnResult("ses_spawn_usage", "READY")
    assert calls == [(tmp_path, "ses_spawn_usage", 8131, 10_000, 80)]


def test_opencode_driver_relay_marks_when_usage_reaches_threshold(
    orch, driver_mod, tmp_path,
):
    # old-red: 옛 input+output+reasoning=600은 미발화지만 cache 포함 fallback=8100은 발화한다.
    tokens = {"input": 500, "output": 100, "reasoning": 0, "cache": {"read": 7500, "write": 0}}
    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd,
        ctx_budget=10_000,
        precompact_at_pct=80,
        root=tmp_path,
        mark_stop=orch.mark_ctx_post_turn_if_over,
        runner=lambda *args, **kwargs: _FakeCompleted(
            _json_stream("ses_relay_usage", "done", tokens=tokens)
        ),
    )
    assert driver.relay_turn("ses_relay_usage", "work") == "done"
    assert orch.stop_marker_present(tmp_path, "ses_relay_usage") is True


def test_opencode_driver_relay_under_threshold_does_not_mark(
    orch, driver_mod, tmp_path,
):
    tokens = {
        "total": 7999,
        "input": 7000,
        "output": 999,
        "reasoning": 0,
        "cache": {"read": 9000, "write": 9000},
    }
    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd,
        ctx_budget=10_000,
        precompact_at_pct=80,
        root=tmp_path,
        mark_stop=orch.mark_ctx_post_turn_if_over,
        runner=lambda *args, **kwargs: _FakeCompleted(
            _json_stream("ses_under", "done", tokens=tokens)
        ),
    )
    assert driver.relay_turn("ses_under", "work") == "done"
    assert orch.stop_marker_present(tmp_path, "ses_under") is False


def test_opencode_driver_ctx_guard_noop_when_di_missing(orch, driver_mod):
    """budget/root/mark_stop 미주입 기본 경로는 usage가 있어도 예외 없는 no-op이다."""
    tokens = {"input": 900_000, "output": 1, "reasoning": 0}
    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd,
        runner=lambda *args, **kwargs: _FakeCompleted(
            _json_stream("ses_no_di", "ok", tokens=tokens)
        ),
    )
    assert driver.relay_turn("ses_no_di", "work") == "ok"


def test_opencode_driver_parser_flags(driver_mod):
    parser = driver_mod.build_parser()
    ns = parser.parse_args(["--cwd", "/some/repo", "--agent", "build"])
    assert ns.cwd == "/some/repo" and ns.agent == "build"
    ns2 = parser.parse_args([])
    assert ns2.cwd is None  # 기본 = 실행 dir(main 에서 os.getcwd()).
    assert ns2.agent == "pm"  # PM primary 기본.


def test_opencode_driver_parser_accepts_task(driver_mod, monkeypatch, capsys):
    """opencode driver 가 `--task <이름>` 을 받고, 빠뜨리면 엔진을 적재하기 전에 rc 2 로 거부한다([[T-0960]])."""
    parser = driver_mod.build_parser()
    assert parser.parse_args(["--task", "mytask"]).task == "mytask"
    monkeypatch.setattr(driver_mod, "_load_engine", lambda: pytest.fail("task 없이 엔진 적재"))
    with pytest.raises(SystemExit) as exc:
        driver_mod.main([])
    assert exc.value.code == 2
    assert "relay 에는 --task <task> 가 필요하다" in capsys.readouterr().err


def test_opencode_main_forwards_task_and_budget(driver_mod, monkeypatch, tmp_path):
    """main 이 task와 local.conf 예산을 주입하고 supervisor 전 예산 검증을 호출한다."""
    captured: dict = {}

    def _fake_spawn_result(sid, reply):
        return sid, reply

    class _FakeSup:
        def __init__(self, driver, *, root, task=None):
            captured["task"] = task
            captured["driver"] = driver

        def run_loop(self, cwd, in_stream, out_stream):
            return 0

    class _FakeEngine:
        Supervisor = _FakeSup

        @staticmethod
        def mark_ctx_post_turn_if_over(*args):
            return False

        SpawnResult = staticmethod(_fake_spawn_result)

    (tmp_path / ".project_manager").mkdir()
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "harness.opencode.ctx_window_tokens=333000\nctx.precompact_at_pct=85\n",
        encoding="utf-8",
    )
    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(tmp_path, harness="opencode")
    _FakeEngine.ctx_budget = staticmethod(
        lambda root, harness, model: captured.setdefault("budget", relay.ctx_budget(root, harness, model)))
    monkeypatch.setattr(driver_mod, "_load_engine", lambda: (_FakeEngine(), tmp_path))
    rc = driver_mod.main(["--task", "mytask", "--cwd", str(tmp_path)])
    assert rc == 0 and captured["task"] == "mytask"
    assert captured["budget"] == (333000, 85, None)
    assert captured["driver"]._ctx_budget == 333000
    assert captured["driver"]._precompact_at_pct == 85
    assert captured["driver"]._mark_stop is _FakeEngine.mark_ctx_post_turn_if_over
    assert captured["driver"]._spawn_result is _FakeEngine.SpawnResult


def test_opencode_main_rejects_unsafe_budget_before_supervisor(
    orch, driver_mod, monkeypatch, tmp_path,
):
    class _UnexpectedSup:
        def __init__(self, *args, **kwargs):
            raise AssertionError("예산 검증 실패 전에 supervisor를 만들면 안 됨")

    class _FakeEngine:
        Supervisor = _UnexpectedSup
        SpawnResult = orch.SpawnResult
        validate_relay_budget = staticmethod(orch.validate_relay_budget)
        mark_ctx_post_turn_if_over = staticmethod(orch.mark_ctx_post_turn_if_over)

    (tmp_path / ".project_manager").mkdir()
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "harness.opencode.ctx_window_tokens=50500\nctx.precompact_at_pct=80\n", encoding="utf-8",
    )
    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(tmp_path, harness="opencode")
    _FakeEngine.ctx_budget = staticmethod(orch.ctx_budget)
    monkeypatch.setattr(driver_mod, "_load_engine", lambda: (_FakeEngine(), tmp_path))
    with pytest.raises(ValueError, match="즉시 세션 회전 위험"):
        driver_mod.main(["--task", "demo-task", "--cwd", str(tmp_path)])


def test_opencode_driver_repo_root_is_the_adapter_parent(driver_mod, tmp_path):
    """repo_root 는 driver 설치 자리(.opencode/)의 부모다 — 조상을 훑지 않는다.

    바깥 트리에만 엔진 사본이 있는 중첩 형상에서 조상 훑기는 바깥 루트를 답하고 `.parent`
    는 자기 루트를 답한다. 어댑터가 위에 있는 남의 PM 홈에 착지하지 않게 하는 축이다
    (claude ctx_guard.repo_root 동형).
    """
    outer = tmp_path / "outer"
    (outer / ".project_manager" / "tools").mkdir(parents=True)
    (outer / ".project_manager" / "tools" / "pm_handoff.py").write_text(
        "x", encoding="utf-8"
    )
    root = outer / "nested" / "root"
    nested = root / ".opencode"
    nested.mkdir(parents=True)
    assert driver_mod.repo_root(nested) == root.resolve()


# ── ⑥ subprocess 폭발 가드 (driver 가 실 opencode 안 부름) ────────────────────

def test_opencode_driver_does_not_spawn_real_subprocess(orch, driver_mod, monkeypatch, tmp_path):
    """driver 경로가 (FakeRunner 주입 시) 실 subprocess.run 을 호출하지 않는다."""
    import subprocess as _sp

    def _boom(*a, **k):
        raise AssertionError("driver 가 실 subprocess 를 호출했다")

    monkeypatch.setattr(_sp, "run", _boom)
    monkeypatch.setattr(_sp, "Popen", _boom)

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(_json_stream("ses_safe", "READY"))

    driver = driver_mod.OpencodeCliDriver(contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=fake_run)
    spawned = driver.spawn(str(tmp_path), "uuid", "boot")
    sid, _ = spawned
    assert driver.relay_turn(sid, "x") == "READY"


# ── 엔진 Supervisor + opencode driver 결합 (FakeRunner·회전까지) ──────────────

def test_supervisor_with_opencode_driver_rotation(orch, driver_mod, tmp_path):
    """usage·예산 DI가 driver marker를 실제 생성하고 Supervisor가 회전한다."""
    counts = {"spawn": 0, "relay": 0}

    def fake_run(cmd, **kwargs):
        if "--agent" in cmd:
            counts["spawn"] += 1
            sid = f"ses_{counts['spawn']}"
            return _FakeCompleted(_json_stream(
                sid, "READY", tokens={"total": 100, "input": 100, "output": 0},
            ))
        counts["relay"] += 1
        sid = cmd[cmd.index("-s") + 1]
        tokens = (
            # total 부재 cache-heavy: old 600(미발화), corrected 8100(≥8000, 발화).
            {"input": 500, "output": 100, "reasoning": 0,
             "cache": {"read": 7500, "write": 0}}
            if counts["relay"] == 1 else {"total": 200, "input": 200, "output": 0}
        )
        return _FakeCompleted(_json_stream(sid, f"reply:{cmd[-1]}", tokens=tokens))

    import io
    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd,
        ctx_budget=10_000,
        precompact_at_pct=80,
        root=tmp_path,
        mark_stop=orch.mark_ctx_post_turn_if_over,
        spawn_result=orch.SpawnResult,
        runner=fake_run,
    )
    sup = orch.Supervisor(driver, root=tmp_path, task="demo-task")
    rc = sup.run_loop(str(tmp_path), io.StringIO("first\nsecond\n"), io.StringIO())
    assert rc == 0
    assert counts == {"spawn": 2, "relay": 2}


# ── live smoke (실 opencode · 기본 skip · 로컬 ollama) ────────────────────────

PM_RELAY_LIVE = os.environ.get("PM_RELAY_LIVE") == "1"
# 로컬 ollama 모델 — 가벼운 것. 환경변수로 override 가능.
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/gemma4:26b")


@pytest.mark.skipif(
    not PM_RELAY_LIVE or not shutil.which("opencode"),
    reason="live smoke — PM_RELAY_LIVE=1 + opencode CLI 필요(기본 skip·CI green 불변).",
)
def test_live_opencode_sid_marker_identity_smoke(orch, driver_mod, tmp_path):
    """실 opencode 1회 e2e: spawn → relay(≥2턴) → marker(driver-파싱 sid 로) → swap → relay.

    **핵심 검증 — driver-파싱 sid == plugin 이 쓸 marker sid 동일성**(ADR-0009 핵심 가정·
    silent-fail 방어). plugin 의 `info.sessionID`(event hook)와 driver 의 출력 파싱 sid 가
    같아야 supervisor 의 marker 예측이 성립한다. 여기선 driver 가 파싱한 sid 로 marker 를
    쓰고(plugin 대역) supervisor 가 그 sid 의 marker 를 stat 해 회전 — sid 일관성 실측.

    로컬 `.opencode/` plugin 이 실 ctx-STOP 을 트리거하진 않으므로 marker 는 강제 생성으로
    swap 만 검증(deferred 부분 — claude smoke 와 동일 한계). 모델 부재 시 정직 fail.
    """
    driver = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=_live_runner_with_model(LIVE_MODEL)
    )

    # ── spawn: opencode 가 sid 발급 → driver 가 출력에서 파싱·반환 ──
    spawned = driver.spawn(
        str(tmp_path),
        orch.new_session_id(),  # 엔진 uuid4 — driver 가 무시할 값.
        "Remember this code word: MANGO77. Reply with exactly: STORED",
    )
    observed, _ = spawned
    assert observed, "spawn 이 opencode 출력에서 session id 를 파싱하지 못함"
    assert observed.startswith("ses_"), f"opencode sid 형식 아님: {observed!r}"

    # ── relay turn2: resume 같은 세션(-s) → turn1 사실 회상(연속성) ──
    reply = driver.relay_turn(
        observed, "What was the code word? Reply with only the code word."
    )
    assert "MANGO77" in reply.upper(), f"resume 연속성 실패 — reply={reply!r}"

    # ── *** driver-파싱 sid == marker sid 동일성 *** ──
    # plugin 은 sanitize(currentSessionID).done 을 쓴다 — driver 파싱 sid 를 같은 규칙으로
    # sanitize 한 marker 경로가 supervisor 의 예측과 일치해야 한다.
    marker = orch._marker_path(tmp_path, observed)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ctx-stop handoff triggered\n", encoding="utf-8")
    assert orch.stop_marker_present(tmp_path, observed) is True, (
        "driver 파싱 sid 로 쓴 marker 를 supervisor 가 stat 하지 못함 — sid 예측 깨짐."
    )

    # ── swap: 새 세션 spawn(다른 sid) → relay 완주 ──
    new_spawned = driver.spawn(
        str(tmp_path),
        orch.new_session_id(),
        "Prior code word was MANGO77. Reply with exactly: CONTINUED",
    )
    new_observed, _ = new_spawned
    assert new_observed and new_observed != observed, "swap 후 새 세션 sid 발급 실패"
    swap_reply = driver.relay_turn(new_observed, "Reply with only the prior code word.")
    assert "MANGO77" in swap_reply.upper(), f"swap 후 relay 실패 — reply={swap_reply!r}"


def _live_runner_with_model(model: str):
    """live smoke 전용 runner — driver 가 조립한 opencode 명령에 `-m <model>` 을 주입한다.

    프로덕션 driver 는 agent frontmatter 의 model 을 쓰지만(`-m` 생략·ADR-0006 D5), 로컬
    smoke 는 ollama 가벼운 모델로 강제해야 하므로 runner seam 으로 `-m` 만 끼운다.
    """
    def runner(cmd, **kwargs):
        injected = list(cmd)
        if "-m" not in injected:
            # message positional(맨 끝) 앞에 -m <model> 삽입.
            injected = injected[:-1] + ["-m", model] + injected[-1:]
        return subprocess.run(injected, **kwargs)

    return runner


# ── 모델 목록 조회(`contract.model_listing`) — 모델과 모델별 variants (T-0946) ──────────────────
# 조회 실행·판독·시간 상한의 주인은 contract 하나다(pm_import 에서 옮겨 왔다). 가짜 실행 파일은
# 실제 `opencode models --verbose` 출력 형식을 재현한다(`_fake_opencode`).
from _fake_opencode import (  # noqa: E402
    OLLAMA_LISTING, calls, install_fake_opencode, listing_for, path_with,
)
from _win_skip import posix_bash_supported  # noqa: E402

_needs_posix_fake = pytest.mark.skipif(
    not posix_bash_supported(), reason="가짜 opencode 는 POSIX sh 스크립트다")


def _fake_env(folder: Path) -> dict[str, str]:
    """가짜 폴더를 PATH 앞에 둔 env(나머지는 이 프로세스 env 그대로)."""
    env = dict(os.environ)
    env["PATH"] = path_with(folder)
    return env


@_needs_posix_fake
def test_model_listing_reads_models_and_variants(orch, tmp_path):
    """머리 줄 순서로 모델을, 모델 객체의 `variants` 키를 원문 순서로 읽고 같은 조건은 한 번만 조회한다."""
    fakebin = install_fake_opencode(tmp_path / "fakebin", OLLAMA_LISTING)
    env = _fake_env(fakebin)

    listing = contract.model_listing(contract.MODEL_SOURCE["opencode"], cwd=tmp_path, env=env)
    again = contract.model_listing(contract.MODEL_SOURCE["opencode"], cwd=tmp_path, env=env)

    assert listing.models == ("ollama/glm-5.2:cloud", "ollama/ornith1.5:35b-a3b")
    assert listing.variants == {
        "ollama/glm-5.2:cloud": (),
        "ollama/ornith1.5:35b-a3b": ("low", "medium", "high"),
    }
    assert again == listing
    recorded = calls(fakebin)
    assert len(recorded) == 1, f"같은 조건의 조회가 다시 실행됐다: {recorded}"
    assert recorded[0][1] == "models --verbose"
    assert contract.model_listing(contract.MODEL_SOURCE["claude"], cwd=tmp_path, env={}).variants is None


@_needs_posix_fake
def test_model_listing_stops_on_lookup_failure(orch, tmp_path, monkeypatch):
    """조회 실패(rc·PATH 부재·시간 초과·판독 실패·모델 0개)는 빈 값이 아니라 HarnessContractError 다."""
    source = contract.MODEL_SOURCE["opencode"]
    failing = install_fake_opencode(tmp_path / "failing", OLLAMA_LISTING, rc=7)
    with pytest.raises(contract.HarnessContractError) as excinfo:
        contract.model_listing(source, cwd=tmp_path, env=_fake_env(failing))
    message = str(excinfo.value)
    assert "opencode models --verbose" in message and "rc=7" in message and "boom" in message

    empty = tmp_path / "empty-bin"
    empty.mkdir()
    with pytest.raises(contract.HarnessContractError) as excinfo:
        contract.model_listing(source, cwd=tmp_path, env={"PATH": str(empty)})
    assert "PATH 부재" in str(excinfo.value)

    without_variants = OLLAMA_LISTING.replace('  "variants": {}\n', '  "other": {}\n')
    assert without_variants != OLLAMA_LISTING
    codex_catalog = json.dumps({"models": [{"slug": "gpt-5.6-sol"}]})
    for index, listing in enumerate((without_variants, "", codex_catalog)):
        broken = install_fake_opencode(tmp_path / f"broken-{index}", listing)
        with pytest.raises(contract.HarnessContractError):
            contract.model_listing(source, cwd=tmp_path, env=_fake_env(broken))

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="opencode models --verbose", timeout=60)

    monkeypatch.delenv("PM_OPENCODE_MODELS_TIMEOUT", raising=False)
    monkeypatch.setattr(contract, "resolve_executable", lambda name, env: name)
    monkeypatch.setattr(contract.subprocess, "run", _raise_timeout)
    with pytest.raises(contract.HarnessContractError) as excinfo:
        contract.model_listing(source, cwd=tmp_path / "timeout", env={"PATH": str(empty)})
    assert "60s timeout 초과" in str(excinfo.value)
    assert "PM_OPENCODE_MODELS_TIMEOUT" in str(excinfo.value)


def test_reasoning_allowed_sources_partition_the_harnesses(orch, tmp_path):
    """정적 허용값 하네스와 모델별 허용값 하네스가 지원 하네스를 겹침 없이 나눈다."""
    static = set(conf_schema.REASONING_ALLOWED)
    per_model = set(contract.PER_MODEL_REASONING_HARNESSES)
    assert not static & per_model
    assert static | per_model == set(conf_schema.HARNESS_CHOICES)
    assert "opencode" not in conf_schema.REASONING_ALLOWED, "opencode 정적 허용값 행이 남았다"
    for harness in contract.PER_MODEL_REASONING_HARNESSES:
        assert contract.MODEL_SOURCE[harness].parse == "opencode-verbose", harness
    assert contract.MODEL_SOURCE["opencode"].command == ("opencode", "models", "--verbose")
    assert contract.reasoning_allowed("claude", None, cwd=tmp_path, env={}) == (
        "low", "medium", "high", "xhigh", "max")
    with pytest.raises(contract.HarnessContractError):
        contract.reasoning_allowed("gemini", None, cwd=tmp_path, env={})
    assert contract.normalize_reasoning("  ") is None
    assert contract.normalize_reasoning(" high ") == "high"


# ── 조회 실패 사유 — 사유 한 줄이 원인을 지목한다(pm_import 에서 옮긴 시험) ──────────────────


@pytest.fixture
def bare_executable(orch, monkeypatch):
    """`subprocess.run` 을 대역으로 바꾸는 조회 시험 — 실행 파일 해소는 이름 그대로 둔다(이 기계 PATH 무관).

    해소 자체는 `test_harness_cli_spawns_resolve_executable_*` 가 본다."""
    monkeypatch.setattr(contract, "resolve_executable", lambda name, env: name)


class _FakeListingRun:
    """subprocess.run 반환 모사 — 조회가 보는 필드만."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _codex_source(orch):
    return contract.MODEL_SOURCE["codex"]


def test_model_listing_rc_failure_truncates_stderr_to_200(orch, tmp_path, monkeypatch, bare_executable):
    """rc≠0 의 stderr 는 앞 200자까지만 사유에 싣는다(로그 폭증 방지)."""
    monkeypatch.setattr(contract.subprocess, "run",
                        lambda *a, **k: _FakeListingRun(returncode=1, stderr="x" * 500))
    with pytest.raises(contract.HarnessContractError) as excinfo:
        contract.model_listing(_codex_source(orch), cwd=tmp_path / "rc", env={})
    message = str(excinfo.value)
    assert "rc=1" in message and "x" * 200 in message and "x" * 201 not in message


def test_model_listing_zero_models_is_a_failure(orch, tmp_path, monkeypatch, bare_executable):
    """rc 0 이어도 모델이 0개면 성공이 아니다(빈 목록 성공 위장 0)."""
    monkeypatch.setattr(contract.subprocess, "run",
                        lambda *a, **k: _FakeListingRun(stdout=json.dumps({"models": []})))
    with pytest.raises(contract.HarnessContractError) as excinfo:
        contract.model_listing(_codex_source(orch), cwd=tmp_path / "zero", env={})
    assert "모델 0개" in str(excinfo.value)


def test_model_listing_uses_the_resolved_timeout(orch, tmp_path, monkeypatch, bare_executable):
    """조회는 `models_timeout()` 값을 `timeout=` 으로 쓰고 cwd·env 를 그대로 넘긴다."""
    monkeypatch.setenv("PM_OPENCODE_MODELS_TIMEOUT", "200")
    seen = {}

    def _capture(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return _FakeListingRun(stdout=json.dumps({"models": [{"slug": "gpt-6-astra"}]}))

    monkeypatch.setattr(contract.subprocess, "run", _capture)
    listing = contract.model_listing(_codex_source(orch), cwd=tmp_path, env={"A": "1"})
    assert listing == contract.ModelListing(("gpt-6-astra",), None)
    assert seen["timeout"] == 200
    assert seen["argv"] == ["codex", "debug", "models"]
    assert seen["cwd"] == str(tmp_path) and seen["env"] == {"A": "1"}


@pytest.mark.parametrize(("raw", "expected"), [
    (None, 60), ("120", 120), ("  90  ", 90),
    ("soon", None), ("0", None), ("-5", None), ("", None), ("1_000", None), ("+5", None),
], ids=["unset", "override", "strip", "non-numeric", "zero", "negative", "empty", "underscore",
        "signed"])
def test_models_timeout_env_override(orch, monkeypatch, raw, expected):
    """`PM_OPENCODE_MODELS_TIMEOUT` 는 미설정이면 기본 60, 있으면 양의 정수만 받고 그 밖은 멈춘다.

    늘렸다고 믿는 값을 기본값으로 바꾸지 않는다(expected None = `HarnessContractError`)."""
    if raw is None:
        monkeypatch.delenv("PM_OPENCODE_MODELS_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("PM_OPENCODE_MODELS_TIMEOUT", raw)
    if expected is None:
        with pytest.raises(contract.HarnessContractError, match="PM_OPENCODE_MODELS_TIMEOUT"):
            contract.models_timeout()
    else:
        assert contract.models_timeout() == expected


def test_the_json_catalog_parser_takes_slugs(orch, tmp_path, monkeypatch, bare_executable):
    """`codex debug models` 산출은 `models[].slug` 만 모은다(설명·추론 등급은 끌고 오지 않는다)."""
    output = json.dumps({"models": [
        {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "supported_reasoning_levels": []},
        {"slug": "gpt-5.6-sol"},
    ]})
    monkeypatch.setattr(contract.subprocess, "run", lambda *a, **k: _FakeListingRun(stdout=output))
    listing = contract.model_listing(_codex_source(orch), cwd=tmp_path, env={})
    assert listing.models == ("gpt-6-astra", "gpt-5.6-sol")


@pytest.mark.parametrize("output", [
    "not json at all",
    '{"models": "wrong shape"}',
    json.dumps({"models": [{"slug": "  "}]}),
    json.dumps({"models": [{"display_name": "slug 없음"}]}),
    json.dumps({"models": [{"slug": 3}]}),
], ids=["not-json", "wrong-shape", "blank-slug", "missing-slug", "non-string-slug"])
def test_a_catalog_that_does_not_parse_is_a_failure(orch, tmp_path, monkeypatch, output, bare_executable):
    """판독 실패는 빈 목록이 아니라 HarnessContractError 다(항목을 조용히 빼지 않는다)."""
    monkeypatch.setattr(contract.subprocess, "run", lambda *a, **k: _FakeListingRun(stdout=output))
    with pytest.raises(contract.HarnessContractError) as excinfo:
        contract.model_listing(_codex_source(orch), cwd=tmp_path, env={})
    assert "판독 실패" in str(excinfo.value)


def test_an_undeclared_source_kind_or_parse_name_is_fail_loud(orch, tmp_path):
    """표가 모르는 kind·판독 이름은 선언과 판정이 갈린 것이다 — 아무 형식으로나 읽지 않는다."""
    for source in (
        contract.ModelSource("nope", (), None, None, "", ""),
        contract.ModelSource("live", (), ("x",), "nope", "", ""),
    ):
        with pytest.raises(contract.HarnessContractError):
            contract.model_listing(source, cwd=tmp_path, env={})


def test_listing_for_matches_the_verbose_output_shape():
    """가짜 출력은 실측 형식(머리 줄 + indent 2 JSON, variants 없는 모델은 `{}`)을 그대로 낸다."""
    assert OLLAMA_LISTING.startswith(
        'ollama/glm-5.2:cloud\n{\n  "id": "glm-5.2:cloud",\n  "providerID": "ollama",\n'
        '  "variants": {}\n}\nollama/ornith1.5:35b-a3b\n{\n')
    assert '  "variants": {\n    "low": {},\n    "medium": {},\n    "high": {}\n  }\n}\n' \
        in OLLAMA_LISTING
    assert listing_for({}) == ""


# ── 실행 파일 해소 한 곳(`resolve_executable`) — 세 실행 자리가 쓴다 (T-0946 리뷰 03 F-001) ─────────────
# Windows 는 npm 셈(`opencode.cmd`)을 이름 그대로 실행하지 못한다(CreateProcess 는 `.exe` 만 붙인다).
# 해소는 `shutil.which`(PATHEXT 포함)이고, 해소 결과가 `.cmd` 면 cmd.exe 가 인자를 해석하므로 엔진의
# 실행 인자에는 여러 줄 값을 싣지 않는다.


class _RecordingPopen:
    """`Popen` 기록 대역 — 받은 argv 를 남기고 바로 끝나는 자식처럼 군다."""

    def __init__(self):
        self.argv = None

    def __call__(self, argv, **kwargs):
        import io
        self.argv = list(argv)
        recorder = self

        class _Proc:
            pid = 0
            returncode = 0
            stdin = None
            stdout = io.StringIO("")
            stderr = io.StringIO("")

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        recorder.proc = _Proc()
        return recorder.proc


def test_harness_cli_spawns_resolve_executable_to_the_found_path(orch, tmp_path, monkeypatch):
    """세 자리가 이름이 아니라 해소한 경로로 실행한다 — Windows 의 `opencode.cmd` 해소를 모사한다.

    `shutil.which` 가 `.cmd` 경로를 돌려주면(Windows PATHEXT 해소) model_listing 의 `subprocess.run` 과
    `_WatchedPopen` 의 `Popen` 이 그 경로를 첫 인자로 받고 나머지 인자는 그대로다."""
    shim = str(tmp_path / "npm" / "opencode.cmd")
    looked_up = []

    def _which(name, path=None):
        looked_up.append((name, path))
        return shim if name == "opencode" else None

    monkeypatch.setattr(contract.shutil, "which", _which)
    seen = {}

    def _run(argv, **kwargs):
        seen["argv"] = list(argv)
        return _FakeListingRun(stdout=OLLAMA_LISTING)

    monkeypatch.setattr(contract.subprocess, "run", _run)
    env = {"PATH": str(tmp_path / "npm")}
    contract.model_listing(contract.MODEL_SOURCE["opencode"], cwd=tmp_path / "listing", env=env)
    assert seen["argv"] == [shim, "models", "--verbose"]
    assert looked_up[-1] == ("opencode", str(tmp_path / "npm")), "해소가 자식 env 의 PATH 를 보지 않았다"

    recorder = _RecordingPopen()
    monkeypatch.setattr(contract.subprocess, "Popen", recorder)
    watchdog._WatchedPopen(["opencode", "run", "--format", "json"], env=env)
    assert recorder.argv == [shim, "run", "--format", "json"]


def test_harness_cli_spawns_resolve_executable_failure_keeps_the_path_message(orch, tmp_path, monkeypatch):
    """해소 실패는 실행 없이 PATH 부재로 멈춘다 — model_listing 은 지금 문구의 HarnessContractError,
    `_WatchedPopen` 은 확정 기동 실패(`FileNotFoundError`·자식 없음 표식)다."""
    monkeypatch.setattr(contract.subprocess, "run",
                        lambda *a, **k: pytest.fail("해소 실패인데 실행했다"))
    monkeypatch.setattr(contract.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("해소 실패인데 실행했다"))
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {"PATH": str(empty)}
    with pytest.raises(contract.HarnessContractError) as listing_error:
        contract.model_listing(contract.MODEL_SOURCE["opencode"], cwd=tmp_path / "missing", env=env)
    assert str(listing_error.value).startswith(
        "opencode 실행 파일 PATH 부재 — 이 프로세스의 PATH 에서 찾지 못해 모델 목록과 variant 를 확인할 수 없다.")

    with pytest.raises(FileNotFoundError) as spawn_error:
        watchdog._WatchedPopen(["opencode", "run"], env=env)
    assert "opencode 실행 파일 PATH 부재" in str(spawn_error.value)
    assert spawn_error.value.filename == "opencode"
    assert getattr(spawn_error.value, watchdog.SPAWN_FAILED_ATTR) is True


@_needs_posix_fake
def test_harness_cli_spawns_resolve_executable_posix_unchanged(orch, tmp_path):
    """POSIX 는 이름 그대로 실행할 때 찾는 파일과 같은 파일을 실행한다 — 자식 env 의 PATH 가 정한다."""
    fakebin = install_fake_opencode(tmp_path / "fakebin", OLLAMA_LISTING)
    env = _fake_env(fakebin)
    resolved = contract.resolve_executable("opencode", env)
    assert resolved == shutil.which("opencode", path=env["PATH"]) == str(fakebin / "opencode")
    proc = watchdog._WatchedPopen(["opencode", "models", "--verbose"], cwd=str(tmp_path), env=env)
    stdout, _stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0 and stdout.startswith("ollama/glm-5.2:cloud")
    with pytest.raises(FileNotFoundError):
        contract.resolve_executable("opencode", {"PATH": str(tmp_path / "nowhere")})


@pytest.mark.skipif(os.name != "nt", reason="Windows PATHEXT 해소 — 실측은 Windows 에서만")
def test_harness_cli_spawns_resolve_executable_finds_npm_cmd_shim_on_windows(orch, tmp_path):
    """PATH 에 `opencode.cmd` 하나만 있으면 그 경로로 해소된다(이름 그대로는 CreateProcess 가 못 찾는다)."""
    shim_dir = tmp_path / "npm"
    shim_dir.mkdir()
    (shim_dir / "opencode.cmd").write_text("@echo ollama/glm-5.2:cloud\r\n", encoding="utf-8")
    env = {**os.environ, "PATH": str(shim_dir)}
    resolved = contract.resolve_executable("opencode", env)
    assert Path(resolved).parent == shim_dir and resolved.lower().endswith(".cmd")


def test_ctx_native_protocol_resolves_through_the_relay(tmp_path):
    """사용 한도 조회(`pm_import._ctx_native_protocol`)도 같은 해소를 쓴다 — 없으면 PATH 부재."""
    pm_import = _load("pm_import_ctx_resolve", TOOLS / "pm_import.py")
    with pytest.raises(FileNotFoundError) as excinfo:
        pm_import._ctx_native_protocol(["no-such-harness-cli-xyz", "-p"], tmp_path, [])
    assert "no-such-harness-cli-xyz 실행 파일 PATH 부재" in str(excinfo.value)


_MULTILINE_PROMPT = "첫 줄\n둘째 줄 & echo INJECTED\r\n셋째 줄"


def _relay_driver_argvs(orch, driver_mod, tmp_path) -> list[list[str]]:
    """relay 세션 회전 드라이버 셋(claude·codex·opencode)이 여러 줄 프롬프트로 spawn·relay 할 때의 실행 인자."""
    argvs: list[list[str]] = []
    prompts: list[str] = []

    def recording(stdout):
        def runner(cmd, **kwargs):
            argvs.append(list(cmd))
            if "--file" in cmd:
                with open(cmd[cmd.index("--file") + 1], encoding="utf-8", newline="") as handle:
                    prompts.append(handle.read())
            else:
                prompts.append(kwargs["input"])
            return _FakeCompleted(stdout)
        return runner

    opencode = driver_mod.OpencodeCliDriver(
        contract.parse_opencode_json, spawn_result=relay.SpawnResult, temp_root=raw_output.temp_root, attached_message=contract.OPENCODE_ATTACHED_MSG, assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd, runner=recording(_json_stream("ses_m", "ok")))
    sid, _ = opencode.spawn(str(tmp_path), "uuid", _MULTILINE_PROMPT)
    opencode.relay_turn(sid, _MULTILINE_PROMPT)

    codex_mod = _load("pm_orch_codex_multiline", REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py")
    codex_stream = "\n".join((json.dumps({"type": "thread.started", "thread_id": "tid_m"}), json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}})))
    codex = codex_mod.CodexCliDriver(contract.parse_codex_json, spawn_result=relay.SpawnResult, mark_ctx_if_over=relay.mark_ctx_post_turn_if_over, runner=recording(codex_stream))
    tid, _ = codex.spawn(str(tmp_path), "uuid", _MULTILINE_PROMPT)
    codex.relay_turn(tid, _MULTILINE_PROMPT)

    claude_mod = _load("pm_orch_claude_multiline",
                       REPO / "templates" / "claude_code" / ".claude" / "pm_orch_claude.py")
    claude = claude_mod.ClaudeCliDriver(
        contract.parse_stream_json, spawn_result=relay.SpawnResult, runner=recording(json.dumps({"type": "result", "result": "ok"})))
    claude.relay_turn("uuid-m", _MULTILINE_PROMPT)
    assert prompts == [_MULTILINE_PROMPT] * len(argvs), "드라이버가 프롬프트 전문을 전달하지 않았다"
    return argvs


def test_engine_spawn_argv_carries_no_multiline_value(orch, driver_mod, tmp_path):
    """엔진·어댑터가 하네스 CLI 에 넘기는 실행 인자에는 여러 줄 값이 없다 — cross·추가 리뷰어 빌더, 모델
    목록 조회 명령, opencode 첨부 안내, relay 드라이버 셋. 프롬프트는 표준 입력이나 `--file` 이다
    (fill 은 test_pm_import_fill). Windows `.cmd` 는 cmd.exe 가 인자를 해석해 첫 줄 뒤를 조용히 버린다."""
    argvs = [list(source.command) for source in contract.MODEL_SOURCE.values() if source.command]
    for role in (*contract.WRITE_ROLES, *contract.READ_ROLES):
        argvs.append(contract.build_claude_argv("sonnet", "high", role))
        argvs.append(contract.build_codex_argv("gpt-6", "high", role, cwd=str(tmp_path)))
        argvs.append(contract.build_opencode_argv(
            "ollama/m", "high", role, str(tmp_path), str(tmp_path / "prompt.md")))
    argvs.extend(_relay_driver_argvs(orch, driver_mod, tmp_path))
    multiline = [(argv, part) for argv in argvs for part in argv if "\n" in part or "\r" in part]
    assert multiline == []


@pytest.mark.parametrize(("module_name", "relative"), [
    ("pm_orch_codex_runner", "templates/codex/.codex/pm_orch_codex.py"),
    ("pm_orch_claude_runner", "templates/claude_code/.claude/pm_orch_claude.py"),
])
def test_relay_driver_runner_resolve_executable(orch, monkeypatch, module_name, relative):
    """codex·claude relay 의 프로덕션 runner 는 엔진 해소 함수로 찾은 경로로 실행한다(opencode relay 는
    엔진 워치독 `_WatchedPopen` 이 해소한다). 해소 실패는 실행 없이 `FileNotFoundError`(PATH 부재)다."""
    driver = _load(module_name, REPO / relative)
    seen = {}

    def _run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return _FakeCompleted("")

    monkeypatch.setattr(driver.subprocess, "run", _run)
    monkeypatch.setattr(contract.shutil, "which", lambda name, path=None: rf"C:\npm\{name}.cmd")
    runner = driver._make_resolving_runner(contract)
    runner(["codex", "exec", "--json"], input="첫 줄\n둘째 줄", capture_output=True)
    assert seen["argv"] == [r"C:\npm\codex.cmd", "exec", "--json"]
    assert seen["kwargs"]["input"] == "첫 줄\n둘째 줄"

    seen.clear()
    monkeypatch.setattr(contract.shutil, "which", lambda name, path=None: None)
    with pytest.raises(FileNotFoundError, match="codex 실행 파일 PATH 부재"):
        runner(["codex", "exec"], input="x")
    assert seen == {}
