"""codex relay (ADR-0009 · ADR-0070 T-0404) 단위 테스트.

엔진 core 의 `parse_codex_json`(claude `parse_stream_json`·opencode `parse_opencode_json` 대칭·
순수·단 usage 를 3번째로 냄)과 codex driver(`pm_orch_codex.py`·SessionDriver 구현)를 importlib 로
직접 검증한다. 실 codex 불요 — FakeRunner DI(claude/opencode driver 테스트 패턴)로 subprocess
폭발 없이 CLI 조립/파싱/ctx 가드만 본다.

검증 축 (ticket DoD):
  ① parse_codex_json 순수 — thread.started→tid · item.* agent_message→reply · turn.completed.usage
     → usage · 비-JSON/비-dict skip · edge.
  ② driver spawn — 엔진 uuid4 **무시** 하고 출력 파싱 thread_id 반환 · `--json` · `--skip-git-repo-check`
     · `-C` 격리 · **stdin=DEVNULL**(⚠ 미닫힘 시 codex 무기한 대기·실측) · resume 없음.
  ③ driver relay_turn — `resume <tid>` · reply 파싱 · spawn cwd 재사용.
  ④ driver close — 세션 cwd 메타 정리.
  ⑤ fail-soft — timeout·OSError → 빈 reply(루프 생존) · subprocess 폭발 가드.
  ⑥ driver-side ctx 기계 가드(ADR-0070 D4 ①) — usage 예산 초과 시 stop marker 박제 · 예산 해소
     precedence · Supervisor 회전(driver 자신의 가드가 marker → 엔진 무수정 회전).

엔진 Supervisor 재사용(spawn→relay→respawn 회전)은 `test_pm_relay.py` 가 FakeDriver 로 이미 커버 —
여기선 codex 고유 표면 + codex driver 의 실제 ctx-가드 경로로 회전을 재확인. 엔진 운영코드는
무수정(ADR-0009 불변식).

+ live smoke — 실 codex(`@skipif`·기본 skip). 게이트만 둔다 — 본체(연속성·resume·tid==marker·
  과금 수집 N)는 T-0407 몫.
"""
from __future__ import annotations

import importlib.util
import json as _json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# codex 라이브 공용 헬퍼 (격리 CODEX_HOME + auth·conftest 소유·ADR-0070 T-0407·live smoke 절).
from conftest import codex_auth_available, codex_live_env, drop_codex_auth, make_codex_home

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
CODEX_DRIVER = REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def orch():
    return _load("pm_relay", TOOLS / "pm_relay.py")


@pytest.fixture(scope="module")
def driver_mod():
    return _load("pm_orch_codex", CODEX_DRIVER)


# ── codex JSONL 이벤트 헬퍼 (실측 형식·codex 0.144.x·spike §1.2) ──────────────

def _codex_ev(**kw) -> str:
    return _json.dumps(kw)


def _codex_stream(tid: str, reply: str, usage: dict | None = None) -> str:
    """대표 codex --json 이벤트 스트림 1턴: thread.started → item.completed(agent_message) → turn.completed.

    usage 는 **실 wire dict**(`*_tokens` 키·codex 0.144.6 실측) — 파서가 contract 로 정규화한다."""
    events = [
        _codex_ev(type="thread.started", thread_id=tid),
        _codex_ev(type="turn.started"),
        _codex_ev(type="item.completed", item={"type": "agent_message", "text": reply}),
    ]
    if usage is not None:
        events.append(_codex_ev(type="turn.completed", usage=usage))
    else:
        events.append(_codex_ev(type="turn.completed"))
    return "\n".join(events)


def _usage_from_wire(orch, **wire) -> dict:
    """wire(`*_tokens` 키·실측 형식)를 **실 파서** 로 정규화해 contract usage 를 얻는다.

    가드 테스트가 파서-정규화 경계를 실제로 타게 해 자기정합 false-green(테스트가 접미사 없는
    contract 키를 직접 지어내 wire→contract 정규화를 건너뛰던 원 결함)을 제거한다 — 정규화가
    깨지면 used==0 → marker 미박제 → 테스트가 정직하게 실패한다(codex/opencode 리뷰 수렴 결함 방어)."""
    _, _, usage = orch.parse_codex_json([
        _codex_ev(type="thread.started", thread_id="t"),
        _codex_ev(type="turn.completed", usage=wire),
    ])
    return usage


def _write_rollout(
    codex_home: Path,
    tid: str,
    *last_totals: int,
    cumulative_input: int | None = None,
) -> Path:
    """실 codex rollout의 token_count 이벤트 최소 fixture를 쓴다."""
    session_dir = codex_home / "sessions" / "2026" / "08" / "06"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-2026-08-06T12-00-00-{tid}.jsonl"
    events = [
        {"type": "event_msg", "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"total_tokens": total},
                "total_token_usage": {"input_tokens": cumulative_input},
            },
        }}
        for total in last_totals
    ]
    rollout.write_text("\n".join(_json.dumps(event) for event in events) + "\n",
                       encoding="utf-8")
    return rollout


# ── ① parse_codex_json (순수·tid/reply/usage 추출·비-JSON skip·edge) ──────────

def test_parse_codex_extracts_thread_reply_usage(orch):
    """실측 형식(codex 0.144.6·PM 프로브 5회): thread.started.thread_id → tid · item agent_message.text
    → reply · turn.completed.usage 의 **`*_tokens` wire 키** → 접미사 없는 contract 로 정규화."""
    tid = "019f7ff4-d535-1234"
    lines = [
        _codex_ev(type="thread.started", thread_id=tid),
        _codex_ev(type="turn.started"),
        _codex_ev(type="item.completed",
                  item={"type": "agent_message", "text": "PONG"}),
        # 실측 wire 키(접미사 _tokens)·PM 라이브 프로브 권위 값.
        _codex_ev(type="turn.completed",
                  usage={"input_tokens": 12481, "cached_input_tokens": 9600,
                         "cache_write_input_tokens": 0,
                         "output_tokens": 105, "reasoning_output_tokens": 92}),
    ]
    got_tid, reply, usage = orch.parse_codex_json(lines)
    assert got_tid == tid
    assert reply == "PONG"
    # wire → contract 정규화(접미사 제거) — driver 인터페이스는 접미사 없는 키.
    assert usage == {
        "input": 12481, "cached_input": 9600, "cache_write_input": 0,
        "output": 105, "reasoning_output": 92,
    }


def test_parse_codex_maps_cache_write_input_tokens(orch):
    """실 wire cache-write 축을 contract 에 보존하되 ctx 점유 합산에는 쓰지 않는다."""
    usage = _usage_from_wire(
        orch, input_tokens=10, cached_input_tokens=4,
        cache_write_input_tokens=3, output_tokens=2,
    )
    assert usage["cache_write_input"] == 3


def test_parse_codex_reply_takes_last_agent_message(orch):
    """스트리밍 item.started→completed 시 최종(마지막) agent_message 전체 텍스트를 취한다."""
    lines = [
        _codex_ev(type="thread.started", thread_id="t1"),
        _codex_ev(type="item.started", item={"type": "agent_message", "text": "partial"}),
        _codex_ev(type="item.completed",
                  item={"type": "agent_message", "text": "partial full answer"}),
    ]
    _, reply, _ = orch.parse_codex_json(lines)
    assert reply == "partial full answer"


def test_parse_codex_ignores_non_agent_message_items(orch):
    """reasoning 등 agent_message 아닌 item 은 reply 로 취하지 않는다."""
    lines = [
        _codex_ev(type="thread.started", thread_id="t2"),
        _codex_ev(type="item.completed", item={"type": "reasoning", "text": "thinking..."}),
        _codex_ev(type="item.completed", item={"type": "agent_message", "text": "actual reply"}),
    ]
    _, reply, _ = orch.parse_codex_json(lines)
    assert reply == "actual reply"


def test_parse_codex_skips_malformed_lines(orch):
    """비-JSON / 부분 라인 skip (claude·opencode 파서와 동일 robust 정책)."""
    lines = [
        "not json at all",
        "",
        "{broken",
        _codex_ev(type="thread.started", thread_id="t3"),
        "noise {",
        _codex_ev(type="item.completed", item={"type": "agent_message", "text": "answer"}),
    ]
    got_tid, reply, _ = orch.parse_codex_json(lines)
    assert got_tid == "t3" and reply == "answer"


def test_parse_codex_ignores_non_dict_events(orch):
    """JSON 배열/스칼라 라인은 dict 가 아니라 skip(robust)."""
    lines = [
        "[1,2,3]", "42",
        _codex_ev(type="thread.started", thread_id="t4"),
        _codex_ev(type="item.completed", item={"type": "agent_message", "text": "x"}),
    ]
    got_tid, reply, _ = orch.parse_codex_json(lines)
    assert got_tid == "t4" and reply == "x"


def test_parse_codex_empty_and_no_content(orch):
    assert orch.parse_codex_json([]) == (None, None, None)
    # thread.started 만 있고 reply/usage 없음.
    assert orch.parse_codex_json(
        [_codex_ev(type="thread.started", thread_id="t5")]) == ("t5", None, None)


def test_parse_codex_thread_started_missing_id(orch):
    """thread.started 인데 thread_id 누락/비-문자열이면 tid None(driver 가 치명 처리)."""
    lines = [
        _codex_ev(type="thread.started"),
        _codex_ev(type="item.completed", item={"type": "agent_message", "text": "r"}),
    ]
    got_tid, reply, _ = orch.parse_codex_json(lines)
    assert got_tid is None and reply == "r"


def test_parse_codex_agent_message_without_text(orch):
    """agent_message item 인데 text 누락/빈문자열이면 누적 안 함(edge robust — 최종 비어있지-않은 것)."""
    lines = [
        _codex_ev(type="thread.started", thread_id="t6"),
        _codex_ev(type="item.completed", item={"type": "agent_message"}),
        _codex_ev(type="item.completed", item={"type": "agent_message", "text": ""}),
        _codex_ev(type="item.completed", item={"type": "agent_message", "text": "real"}),
    ]
    got_tid, reply, _ = orch.parse_codex_json(lines)
    assert got_tid == "t6" and reply == "real"


def test_parse_codex_first_thread_id_wins(orch):
    """thread_id 는 첫 thread.started 값(resume 권위 id — 이후 등장은 무시)."""
    lines = [
        _codex_ev(type="thread.started", thread_id="first"),
        _codex_ev(type="thread.started", thread_id="second"),
    ]
    got_tid, _, _ = orch.parse_codex_json(lines)
    assert got_tid == "first"


def test_parse_codex_usage_takes_last_turn_completed(orch):
    """usage 는 마지막 turn.completed 값(멀티-turn 스트림 방어) — wire 키 정규화."""
    lines = [
        _codex_ev(type="thread.started", thread_id="t7"),
        _codex_ev(type="turn.completed", usage={"input_tokens": 1}),
        _codex_ev(type="turn.completed", usage={"input_tokens": 2}),
    ]
    _, _, usage = orch.parse_codex_json(lines)
    assert usage == {
        "input": 2, "cached_input": 0, "cache_write_input": 0,
        "output": 0, "reasoning_output": 0,
    }


def test_parse_codex_usage_reads_only_tokens_suffix_keys(orch):
    """회귀 가드 — 파서는 **`*_tokens` wire 키만** 읽는다(접미사 없는 키는 0).

    원 결함(codex/opencode 리뷰 수렴): 접미사 없는 `input`/`output` 을 읽어 실 codex 의 `input_tokens`
    를 놓쳐 used==0 → ctx 가드 영구 사멸. 접미사 없는 키만 담긴 usage 는 전부 0 으로 정규화돼야 한다."""
    lines = [
        _codex_ev(type="thread.started", thread_id="t8"),
        # 잘못된(접미사 없는) 키 — 무시돼 전부 0.
        _codex_ev(type="turn.completed",
                  usage={"input": 999, "output": 999, "reasoning_output": 999}),
    ]
    _, _, usage = orch.parse_codex_json(lines)
    assert usage == {
        "input": 0, "cached_input": 0, "cache_write_input": 0,
        "output": 0, "reasoning_output": 0,
    }


# ── codex driver (FakeRunner DI·실 codex 무호출) ─────────────────────────────

class _FakeCompleted:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── ② driver spawn (엔진 uuid4 무시·출력 tid 반환·--json·--skip-git-repo-check·-C·stdin=DEVNULL) ─

def test_codex_driver_spawn_ignores_uuid_returns_thread_id(orch, driver_mod):
    """spawn 이 엔진 uuid4 인자를 **무시** 하고 codex 출력에서 파싱한 thread_id 를 반환한다
    (codex thread_id 사전지정 불가·실측 → 출력 파싱이 권위)."""
    captured = {}
    codex_tid = "019f-CODEX-ISSUED"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(_codex_stream(codex_tid, "READY"))

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    engine_uuid = "11111111-2222-3333-4444-555555555555"
    spawned = driver.spawn("/repo/root", engine_uuid, "bootstrap text")
    assert isinstance(spawned, orch.SpawnResult)
    observed = spawned.session_id
    assert spawned.reply == "READY"
    # 엔진 uuid4 가 아니라 codex 가 발급한 thread_id 를 반환.
    assert observed == codex_tid
    assert observed != engine_uuid
    cmd = captured["cmd"]
    # sandbox 명시 핀(codex R4) — 사용자 전역 config 무관 workspace-write(spawn/resume 공통).
    assert cmd[:6] == ["codex", "exec", "--json", "-s", "workspace-write", "--skip-git-repo-check"]
    assert "-C" in cmd and "/repo/root" in cmd  # child cwd 격리.
    assert "resume" not in cmd  # 첫 spawn 은 resume 안 함.
    assert cmd[-1] == "bootstrap text"  # PROMPT positional 은 맨 끝.
    # ⚠ stdin close 필수 — 미닫힘 시 codex 가 stdin 무기한 대기(라이브 실측·spike §D3).
    assert captured["kwargs"].get("stdin") == subprocess.DEVNULL


def test_codex_driver_spawn_without_engine_type_falls_back_to_tuple(driver_mod):
    """제3자 parser에는 SpawnResult가 없어도 자매 driver처럼 구조 호환 tuple을 반환한다."""
    def parse_without_engine_globals(_lines):
        return "tid_tuple", "READY", None

    driver = driver_mod.CodexCliDriver(
        parse_without_engine_globals,
        runner=lambda _cmd, **_kwargs: _FakeCompleted("ignored"),
    )

    assert driver.spawn("/repo/root", "ignored-uuid", "bootstrap") == (
        "tid_tuple", "READY",
    )


def test_codex_driver_spawn_thread_parse_failure_raises(orch, driver_mod):
    """thread_id 파싱 실패 = 치명·명시 중단 (opencode sid-fail 동형).

    codex 는 thread_id 사전지정 불가라 uuid 폴백 시 `resume <uuid>` 가 존재하지 않는 세션을
    가리켜 연속성 침묵 파손 → 폴백 대신 RuntimeError 로 명시 중단(relay 는 유효 세션 없이 못 돈다)."""
    def fake_run(cmd, **kwargs):
        return _FakeCompleted("no thread here\nplain text")  # 비-JSON → tid None.

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    with pytest.raises(RuntimeError, match="thread"):
        driver.spawn("/r", "engine-uuid-ignored", "boot")


# ── ③ driver relay_turn (resume <tid>·reply 파싱·spawn cwd 재사용) ────────────

def test_codex_driver_relay_uses_resume(orch, driver_mod):
    """relay_turn 이 `resume <tid>` 로 같은 세션을 이어가고 reply 를 반환 + stdin=DEVNULL."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(_codex_stream("tid_r", "hello back"))

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    reply = driver.relay_turn("tid_r", "ping")
    assert reply == "hello back"
    cmd = captured["cmd"]
    assert "resume" in cmd
    assert cmd[cmd.index("resume") + 1] == "tid_r"
    assert cmd[-1] == "ping"
    assert captured["kwargs"].get("stdin") == subprocess.DEVNULL


def test_codex_driver_dash_c_precedes_resume(orch, driver_mod):
    """`-C` 는 exec-레벨 플래그라 `resume` 서브커맨드 *앞*(티켓 명세 `[-C] [resume]` 순).

    resume 뒤에 -C 를 두면 resume 이 -C 를 거부할 때 cwd 격리가 파손된다. spawn 으로 cwd 를 기억시킨
    뒤 relay(resume+-C 동시 경로)에서 상대 위치를 단언. (resume+-C 실효 자체는 T-0407 라이브 확인.)"""
    cmds = []

    def fake_run(cmd, **kwargs):
        cmds.append(cmd)
        return _FakeCompleted(_codex_stream("tid_pos", "ok"))

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    driver.spawn("/repo/root", "uuid", "boot")  # cwd 기억(tid_pos).
    driver.relay_turn("tid_pos", "msg")         # resume + -C 동시.
    relay_cmd = cmds[-1]
    assert "-C" in relay_cmd and "resume" in relay_cmd
    assert relay_cmd.index("-C") < relay_cmd.index("resume"), (
        f"-C 가 resume 뒤에 있음(cwd 격리 파손 위험): {relay_cmd}")


def test_codex_driver_relay_reuses_spawn_cwd(orch, driver_mod):
    """relay 가 spawn 때의 cwd(-C)를 재사용한다 — child cwd 격리 일관."""
    dirs = []

    def fake_run(cmd, **kwargs):
        dirs.append(cmd[cmd.index("-C") + 1] if "-C" in cmd else None)
        return _FakeCompleted(_codex_stream("tid_cwd", "ok"))

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    tid = driver.spawn("/repo/root", "uuid", "boot").session_id
    driver.relay_turn(tid, "msg")
    assert dirs == ["/repo/root", "/repo/root"]


def test_codex_driver_relay_unknown_session_no_dir(orch, driver_mod):
    """모르는 세션(메타 없음) relay 는 -C 없이도 fail-soft(빈 cwd 메타)."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(_codex_stream("tid_u", "r"))

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    reply = driver.relay_turn("tid_never_spawned", "x")
    assert reply == "r"
    assert "-C" not in captured["cmd"]  # cwd 메타 없으면 -C 생략.


# ── ④ driver close (세션 cwd 메타 정리) ──────────────────────────────────────

def test_codex_driver_close_clears_meta(orch, driver_mod):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(_codex_stream("tid_c", "READY"))

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    tid = driver.spawn("/r", "uuid", "boot").session_id
    assert tid in driver._session_cwd
    driver._last_total[tid] = 123
    assert driver.close(tid) is None
    assert tid not in driver._session_cwd  # 메타 정리.
    assert tid not in driver._last_total
    # 모르는 세션 close 도 fail-soft(예외 없음).
    assert driver.close("tid_never") is None


# ── ⑤ driver fail-soft (timeout·실행실패·subprocess 폭발 가드) ────────────────

def test_codex_driver_relay_timeout_returns_empty(orch, driver_mod):
    """subprocess timeout 은 fail-soft — 빈 reply(루프 안 죽음)."""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    assert driver.relay_turn("tid", "x") == ""


def test_codex_driver_relay_oserror_returns_empty(orch, driver_mod):
    """codex 바이너리 부재(OSError)도 fail-soft."""
    def fake_run(cmd, **kwargs):
        raise OSError("codex not found")

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    assert driver.relay_turn("tid", "x") == ""


def test_codex_driver_rc0_empty_stdout_warns(orch, driver_mod, capsys):
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, runner=lambda *args, **kwargs: _FakeCompleted("")
    )
    assert driver.relay_turn("tid", "prompt") == ""
    assert capsys.readouterr().err == (
        "[pm-orch] codex turn 무출력(rc 0) — stdin/파싱 점검\n"
    )


def test_codex_driver_does_not_spawn_real_subprocess(orch, driver_mod, monkeypatch):
    """driver 경로가 (FakeRunner 주입 시) 실 subprocess.run/Popen 을 호출하지 않는다."""
    import subprocess as _sp

    def _boom(*a, **k):
        raise AssertionError("driver 가 실 subprocess 를 호출했다")

    monkeypatch.setattr(_sp, "run", _boom)
    monkeypatch.setattr(_sp, "Popen", _boom)

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(_codex_stream("tid_safe", "READY"))

    driver = driver_mod.CodexCliDriver(orch.parse_codex_json, runner=fake_run)
    tid = driver.spawn("/r", "uuid", "boot").session_id
    assert driver.relay_turn(tid, "x") == "READY"


# ── ⑥ driver-side ctx 기계 가드 (ADR-0070 D4 ①·usage 예산 초과 → marker 박제) ──

def test_codex_maybe_mark_ctx_writes_marker_over_budget(orch, driver_mod, tmp_path):
    """실 wire usage 가 예산 정지점(잔여 20%↔사용 80%)에 도달하면 driver 가 post-turn marker 를 박제 →
    supervisor 가 stat 가능(엔진 write_post_turn_marker DI). used = input+output."""
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )
    # used = 720+80 = 800 = 1000*(100-20)/100 → 정지점 도달(경계 포함). cached/reasoning은 가산 안 함.
    usage = _usage_from_wire(orch, input_tokens=720, cached_input_tokens=600,
                             output_tokens=80, reasoning_output_tokens=20)
    driver._maybe_mark_ctx("tid_over", usage)
    assert orch.stop_marker_present(tmp_path, "tid_over") is True


def test_relay_usage_stop_marker_is_proactive_guard_when_nonblocking_hook_is_observation_only():
    """compaction 훅 systemMessage는 미도달이므로 relay의 proactive driver 회전이 실보호 경로다.

    T-0770 라이브 실측으로 도달 여부가 **채널별로 갈린다**는 사실이 확정됐다 — 진입점 훅의
    additionalContext 는 모델에 닿고, compaction 훅 systemMessage 는 닿지 않는다. relay 축의
    근거는 후자 하나이므로 그 채널로 한정해 고정한다(옛 무조건 문장은 재등장 금지).
    """
    readme = (REPO / "templates" / "codex" / "README.md").read_text(encoding="utf-8")
    assert "exec 경로에서 `systemMessage` 안내는 모델에 닿지 않는다(관측만 가능)" in readme
    assert "exec 경로 안내는 모델에 닿지 않는다" not in readme
    assert "driver 회전 선점이 relay 경로를 실보호" in readme
    assert "**proactive** 기계 가드" in readme
    assert "turn.completed.usage" in readme


def test_codex_maybe_mark_ctx_noop_under_budget(orch, driver_mod, tmp_path):
    """예산 정지점 미만이면 marker 안 씀(실 wire usage 경로)."""
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )
    # used = 300+100 = 400 < 800.
    usage = _usage_from_wire(orch, input_tokens=300, cached_input_tokens=200,
                             output_tokens=100, reasoning_output_tokens=100)
    driver._maybe_mark_ctx("tid_under", usage)
    assert orch.stop_marker_present(tmp_path, "tid_under") is False


def test_codex_maybe_mark_ctx_diffs_measured_thread_totals(orch, driver_mod, tmp_path):
    """프로브 실측 누계 14358→30449→47201은 각 turn 차분만 판정해 조기 발화하지 않는다.

    budget=40000·stop=20의 임계는 32000이다. 구 구현은 세 번째 누계 47201을 turn 점유로
    오독해 marker를 박았으므로 이 테스트가 old-red 회귀 가드다. 실제 차분은 14358, 16091,
    16752로 모두 임계 미만이다.
    """
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=40_000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )
    for total in (14_358, 30_449, 47_201):
        driver._maybe_mark_ctx("tid_probe", _usage_from_wire(orch, input_tokens=total))
        assert orch.stop_marker_present(tmp_path, "tid_probe") is False
    assert driver._last_total["tid_probe"] == 47_201


def test_codex_maybe_mark_ctx_marks_when_turn_delta_is_over_budget(orch, driver_mod, tmp_path):
    """누계 자체가 아니라 현 turn 차분이 임계에 닿을 때 엔진 헬퍼가 marker를 박는다."""
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )
    driver._maybe_mark_ctx("tid_delta", _usage_from_wire(orch, input_tokens=100))
    assert orch.stop_marker_present(tmp_path, "tid_delta") is False
    driver._maybe_mark_ctx("tid_delta", _usage_from_wire(orch, input_tokens=900))
    assert orch.stop_marker_present(tmp_path, "tid_delta") is True


def test_codex_maybe_mark_ctx_warns_when_marker_write_fails(
        orch, driver_mod, tmp_path, capsys):
    """임계 초과지만 marker writer가 실패하면 기존 fail-soft stderr 경고를 남긴다."""
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000, root=tmp_path,
        mark_stop=lambda *_args: False,
        mark_ctx_if_over=orch.mark_ctx_post_turn_if_over,
    )

    driver._maybe_mark_ctx("tid_fail", _usage_from_wire(orch, input_tokens=900))

    assert capsys.readouterr().err == "[pm-orch] codex ctx marker 박제 실패\n"


def test_codex_maybe_mark_ctx_prefers_rollout_last_turn_usage(
        orch, driver_mod, tmp_path, monkeypatch):
    """rollout 누계-input anchor가 wire와 일치하면 마지막 요청 total을 1순위로 쓴다."""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write_rollout(codex_home, "tid_precise", 650, cumulative_input=850)
    seen = []
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000, root=tmp_path,
        mark_stop=lambda *_args: True,
        mark_ctx_if_over=lambda root, sid, used, budget, stop, mark: seen.append(used),
    )

    # turn.completed 누계는 900이지만 rollout의 현 요청 점유는 650이다.
    driver._maybe_mark_ctx(
        "tid_precise", _usage_from_wire(orch, input_tokens=850, output_tokens=50))

    assert seen == [650]


def test_codex_maybe_mark_ctx_stale_rollout_falls_back_and_rotates(
        orch, driver_mod, tmp_path, monkeypatch):
    """작은 옛 rollout은 누계-input anchor 불일치로 버리고 최신 wire 차분으로 회전한다.

    old-red: anchor 검증 전에는 stale total=100을 채택해 임계 800 미달로 회전을 억제했다.
    """
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write_rollout(codex_home, "tid_stale", 100, cumulative_input=100)
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )

    driver._maybe_mark_ctx(
        "tid_stale", _usage_from_wire(orch, input_tokens=900, output_tokens=0))

    assert orch.stop_marker_present(tmp_path, "tid_stale") is True


def test_codex_maybe_mark_ctx_without_rollout_falls_back_to_cumulative_delta(
        orch, driver_mod, tmp_path, monkeypatch):
    """rollout 파일이 없으면 input+output 누계 차분을 fail-soft 상한 근사로 사용한다."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
    seen = []
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000, root=tmp_path,
        mark_stop=lambda *_args: True,
        mark_ctx_if_over=lambda root, sid, used, budget, stop, mark: seen.append(used),
    )

    driver._maybe_mark_ctx("tid_fallback", _usage_from_wire(orch, input_tokens=100, output_tokens=10))
    driver._maybe_mark_ctx("tid_fallback", _usage_from_wire(orch, input_tokens=850, output_tokens=50))

    assert seen == [110, 790]


def test_codex_maybe_mark_ctx_multicall_turn_uses_precise_rollout_value(
        orch, driver_mod, tmp_path, monkeypatch):
    """anchor가 맞으면 다중호출 차분보다 rollout의 마지막 요청 점유가 회전을 결정한다."""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write_rollout(codex_home, "tid_multicall", 600, cumulative_input=800)
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )

    # tool-heavy turn: 누계 차분 900(여러 호출 합)은 임계 800을 넘지만 마지막 요청은 600이다.
    driver._maybe_mark_ctx(
        "tid_multicall", _usage_from_wire(orch, input_tokens=800, output_tokens=100))

    assert orch.stop_marker_present(tmp_path, "tid_multicall") is False


def test_codex_maybe_mark_ctx_does_not_add_reasoning_twice(
        orch, driver_mod, tmp_path, monkeypatch):
    """output_tokens가 이미 포함한 reasoning_output_tokens를 다시 더하지 않는다."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )
    # 올바른 점유=650+100=750<800. reasoning 100을 재가산하면 850으로 잘못 회전한다.
    driver._maybe_mark_ctx(
        "tid_reasoning", _usage_from_wire(
            orch, input_tokens=650, output_tokens=100, reasoning_output_tokens=100))

    assert orch.stop_marker_present(tmp_path, "tid_reasoning") is False


def test_codex_maybe_mark_ctx_excludes_cached_input(orch, driver_mod, tmp_path):
    """cached_input 이중 계상 방지 — cached 를 (잘못) 더하면 정지점 초과지만, 제외하면 미만 → marker 안 씀.

    실측: input_tokens 가 cached_input_tokens 를 포함(input ⊃ cached). used = input+output
    = 500+100 = 600 < 800(정지점). cached 400 을 더하면 1000 ≥ 800 이라 이 가드가 깨지면(누가 cached
    를 합산하면) marker 가 잘못 박제돼 이 테스트가 실패한다."""
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )
    usage = _usage_from_wire(orch, input_tokens=500, cached_input_tokens=400,
                             cache_write_input_tokens=10_000,
                             output_tokens=100, reasoning_output_tokens=50)
    driver._maybe_mark_ctx("tid_cached", usage)
    assert orch.stop_marker_present(tmp_path, "tid_cached") is False


def test_codex_maybe_mark_ctx_honors_stop_pct_override(orch, driver_mod, tmp_path):
    """ctx.stop_pct override — stop_pct=40 이면 잔여 40%↔사용 60% 에서 정지(기본 20 보다 이르게)."""
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000, stop_pct=40,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )
    # used = 540+60 = 600 = 1000*(100-40)/100 → 정지(기본 stop_pct=20 이면 800 이라 미달).
    usage = _usage_from_wire(orch, input_tokens=540, output_tokens=60, reasoning_output_tokens=40)
    driver._maybe_mark_ctx("tid_sp", usage)
    assert orch.stop_marker_present(tmp_path, "tid_sp") is True


def test_codex_maybe_mark_ctx_disabled_without_budget(orch, driver_mod, tmp_path):
    """예산 미설정(None)이면 가드 no-op — 아무리 usage 커도 marker 안 씀."""
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=None,
        mark_stop=orch.write_post_turn_marker, root=tmp_path,
    )
    driver._maybe_mark_ctx("tid_x", _usage_from_wire(orch, input_tokens=10 ** 9))
    assert orch.stop_marker_present(tmp_path, "tid_x") is False


def test_codex_maybe_mark_ctx_disabled_without_mark_stop(orch, driver_mod, tmp_path):
    """mark_stop 미주입이면 가드 no-op(예외 없이 조용히 skip)."""
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000, mark_stop=None, root=tmp_path,
    )
    # 예외 없이 통과(박제 트리거 자체가 없음).
    driver._maybe_mark_ctx("tid_y", _usage_from_wire(orch, input_tokens=10 ** 9))
    assert orch.stop_marker_present(tmp_path, "tid_y") is False


def test_codex_resolve_stop_pct(driver_mod):
    """ctx.stop_pct 해소 — conf 값 우선·비정상/범위 밖/미설정은 기본 20 (ctx_guard.ctx_thresholds 대칭)."""
    resolve = driver_mod.resolve_stop_pct
    assert resolve({"ctx.stop_pct": "15"}) == 15
    assert resolve({}) == driver_mod.CTX_STOP_PCT_DEFAULT
    assert resolve({"ctx.stop_pct": "abc"}) == driver_mod.CTX_STOP_PCT_DEFAULT
    assert resolve({"ctx.stop_pct": "0"}) == driver_mod.CTX_STOP_PCT_DEFAULT     # 범위 밖(≤0) → 기본.
    assert resolve({"ctx.stop_pct": "100"}) == driver_mod.CTX_STOP_PCT_DEFAULT   # 범위 밖(≥100) → 기본.


def test_codex_resolve_ctx_budget_precedence(driver_mod):
    """ctx 예산 해소 순서: harness.codex.ctx_window_tokens > ctx_window_tokens > 200000 (ADR-0041)."""
    resolve = driver_mod.resolve_ctx_budget
    # codex 키가 generic 보다 우선.
    assert resolve({"harness.codex.ctx_window_tokens": "300000", "ctx.window_tokens": "500000"}) == 300000
    assert resolve({"harness.codex.ctx_window_tokens": "300000"}) == 300000
    # codex 키 없으면 generic.
    assert resolve({"ctx.window_tokens": "500000"}) == 500000
    # 둘 다 없으면 기본.
    assert resolve({}) == driver_mod.CTX_WINDOW_TOKENS_DEFAULT
    # 비정수/≤0 은 다음 층 폴백.
    assert resolve({"harness.codex.ctx_window_tokens": "abc", "ctx.window_tokens": "200000"}) == 200000
    assert resolve({"harness.codex.ctx_window_tokens": "0"}) == driver_mod.CTX_WINDOW_TOKENS_DEFAULT


# ── 엔진 Supervisor + codex driver 결합 (post-turn 회전·재전송 금지·codex R2 핵심 가드) ──

def test_supervisor_with_codex_driver_post_turn_no_resend(orch, driver_mod, tmp_path):
    """codex post-turn marker 회전 — turn 실행 *후* 박제라 그 입력을 **재전송하지 않는다**(이중 실행 방지).

    opencode/claude pre-turn(입력 *처리 전* 차단)은 회전 후 그 입력을 재전송하지만, codex 는 turn 이
    이미 실행·응답됐으므로 Supervisor 가 재전송 없이 회전한다(codex R2 must-fix). marker 는 codex
    driver 자신의 `_maybe_mark_ctx`(usage 예산 초과 → 엔진 write_post_turn_marker)가 박제 — 통합 경로.
    'first' 입력이 정확히 **1회만** relay(부작용 1회)되고 회전이 일어남을 단언. (구 pre-turn 재전송이면
    'first' 가 2회 relay 돼 이 단언이 실패 → 이중 실행 결함을 못박는다.)"""
    relayed = []          # relay 로 전달된 프롬프트 순서(spawn bootstrap 은 제외).
    spawn_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        if "resume" not in cmd:  # spawn(fresh exec) → 새 thread_id 발급.
            spawn_count["n"] += 1
            return _FakeCompleted(_codex_stream(f"tid{spawn_count['n']}", "READY", usage=None))
        prompt = cmd[-1]
        relayed.append(prompt)
        tid = cmd[cmd.index("resume") + 1]
        # 'first' turn 만 예산 초과(실 wire 키) → driver 가 post-turn marker 박제(그 turn 은 이미 실행됨).
        usage = ({"input_tokens": 900, "output_tokens": 100, "reasoning_output_tokens": 0}
                 if prompt == "first" else None)
        return _FakeCompleted(_codex_stream(tid, f"reply:{prompt}", usage=usage))

    import io
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path, runner=fake_run,
    )
    sup = orch.Supervisor(driver, root=tmp_path)
    rc = sup.run_loop("/repo/root", io.StringIO("first\nsecond\n"), io.StringIO())
    assert rc == 0
    assert spawn_count["n"] == 2               # 회전 발생(초기 spawn + post-turn respawn).
    assert relayed.count("first") == 1         # 'first' 재전송 안 됨 — 정확히 1회(부작용 1회).
    assert relayed == ["first", "second"]      # 재전송이면 ["first","first","second"] 가 됐을 것.


def test_supervisor_codex_spawn_marker_rotates_before_first_input(orch, driver_mod, tmp_path):
    """spawn 의 bootstrap turn 이 예산 초과 post-turn marker 를 남기면, 첫 사용자 입력은 그 (과예산)
    세션이 아니라 회전된 새 세션으로 relay 된다 (codex R3 엣지 — 'marker 존재 → 추가 입력 0' 불변식).

    구 결함: run_loop 이 spawn 직후 marker 를 안 보고 첫 입력을 과예산 sid 로 relay(지연 회전·입력
    1회 추가 실행). 수정 후: spawn marker → 첫 입력 처리 전 회전 → 새 sid 가 첫 입력 처리."""
    relayed = []  # (sid, prompt) — 어느 세션에 무슨 입력이 relay 됐나.
    spawn_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        if "resume" not in cmd:  # spawn(bootstrap turn).
            spawn_count["n"] += 1
            # 첫 spawn 의 bootstrap turn 만 예산 초과 → codex driver 가 post-turn marker 박제.
            usage = ({"input_tokens": 900, "output_tokens": 100, "reasoning_output_tokens": 0}
                     if spawn_count["n"] == 1 else None)
            return _FakeCompleted(_codex_stream(f"tid{spawn_count['n']}", "READY", usage=usage))
        tid = cmd[cmd.index("resume") + 1]
        relayed.append((tid, cmd[-1]))
        return _FakeCompleted(_codex_stream(tid, f"reply:{cmd[-1]}", usage=None))

    import io
    driver = driver_mod.CodexCliDriver(
        orch.parse_codex_json, ctx_budget=1000,
        mark_stop=orch.write_post_turn_marker, root=tmp_path, runner=fake_run,
    )
    sup = orch.Supervisor(driver, root=tmp_path)
    rc = sup.run_loop("/repo/root", io.StringIO("input1\n"), io.StringIO())
    assert rc == 0
    assert spawn_count["n"] == 2                  # 초기 spawn(marker) + 회전 spawn.
    # 첫 입력은 과예산 tid1 이 아니라 회전된 tid2 로 relay(추가 입력 0 불변식). 구 결함이면 tid1 로 감.
    assert relayed == [("tid2", "input1")]


# ── driver CLI 배선 (parser·main·repo_root) ───────────────────────────────────

def test_codex_driver_parser_flags(driver_mod):
    parser = driver_mod.build_parser()
    ns = parser.parse_args(["--cwd", "/some/repo"])
    assert ns.cwd == "/some/repo"
    ns2 = parser.parse_args([])
    assert ns2.cwd is None  # 기본 = 실행 dir(main 에서 os.getcwd()).
    # --task 수용(claude/opencode 대칭·기본 None·F7·T-0356).
    assert parser.parse_args(["--task", "mytask"]).task == "mytask"
    assert parser.parse_args([]).task is None


def test_codex_main_forwards_task_budget_and_engine_seams(driver_mod, monkeypatch, tmp_path):
    """main이 task/local.conf와 엔진 판정·SpawnResult seam을 driver에 명시 주입한다."""
    captured: dict = {}

    class _FakeSup:
        def __init__(self, driver, *, root, task=None):
            captured["task"] = task
            captured["driver"] = driver

        def run_loop(self, cwd, in_stream, out_stream):
            return 0

    def _fake_write_marker(root, sid):
        return True

    class _FakeEngine:
        Supervisor = _FakeSup
        SpawnResult = staticmethod(lambda sid, reply: (sid, reply))
        write_post_turn_marker = staticmethod(_fake_write_marker)
        mark_ctx_post_turn_if_over = staticmethod(lambda *args: False)

        @staticmethod
        def validate_relay_budget(budget, stop_pct):
            captured["validated"] = (budget, stop_pct)

        @staticmethod
        def parse_codex_json(lines):
            return None, None, None

    # local.conf codex 예산·정지임계 주입 확인(해소 precedence 가 main 배선에 실제로 닿음).
    (tmp_path / ".project_manager").mkdir()
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "harness.codex.ctx_window_tokens=333000\nctx.stop_pct=15\n", encoding="utf-8")
    monkeypatch.setattr(driver_mod, "_load_engine", lambda: (_FakeEngine(), tmp_path))
    rc = driver_mod.main(["--task", "mytask", "--cwd", str(tmp_path)])
    assert rc == 0 and captured["task"] == "mytask"
    assert captured["driver"]._ctx_budget == 333000
    assert captured["driver"]._stop_pct == 15
    assert captured["driver"]._mark_stop is _fake_write_marker
    assert captured["driver"]._mark_ctx_if_over is _FakeEngine.mark_ctx_post_turn_if_over
    assert captured["driver"]._spawn_result is _FakeEngine.SpawnResult
    assert captured["validated"] == (333000, 15)


def test_codex_driver_repo_root_finds_engine(driver_mod, tmp_path):
    """repo_root 가 pm_handoff.py 가 있는 조상을 엔진 루트로 찾는다(opencode findEngineRoot 동형)."""
    (tmp_path / ".project_manager" / "tools").mkdir(parents=True)
    (tmp_path / ".project_manager" / "tools" / "pm_handoff.py").write_text(
        "x", encoding="utf-8"
    )
    nested = tmp_path / ".codex"
    nested.mkdir()
    assert driver_mod.repo_root(nested) == tmp_path.resolve()


# ── codex 고유 라이브 실측 (T-0407·codex-cli 0.144.6·gpt-5.5·격리 CODEX_HOME·spike §6 잔여 해소) ──
# 아래 4건은 라이브(실 codex)로 실측한 결과를 박제한다(코드 주석=실측 로그·재현 커맨드는 보고 참조).
# 라이브 게이트(relay smoke)의 자동 단언은 spawn→resume·usage 누적이고, 아래는 관측 결과 기록이다.
#
#   ① skills discovery — codex 는 adopter `.agents/skills/*/SKILL.md`(cwd→root 스캔·trusted project)를
#      네이티브 발견한다. 실측: canonical 15 스킬 전부 열거(pm-adr·pm-ticket·pm-handoff·pm-review·…·
#      spike-new). auto-trigger 는 description 매칭 implicit 발화가 기본이나(§6 위험), "나열만 하고
#      호출 말라" 중립 프롬프트엔 발화 없이 열거만 함(발견≠강제발화). description-매칭 프롬프트의 과대
#      발화 억제(per-skill allow_implicit_invocation)는 관찰 시 후속 티켓(§6·현 어댑터 기본 미설정).
#   ② TUI env 마커 — codex shell tool env 에 `CODEX_THREAD_ID=<tid>`·`CODEX_CI=1`·
#      `CODEX_SANDBOX_NETWORK_DISABLED` 실재(exec 경로·부트스트랩 카드 `_is_codex_harness` 판정 원천).
#      실측: exec 에서 셸이 셋 모두 반환(tid=발급 thread_id·ci=1). **대화형 TUI 세션은 비대화 자동화
#      불가**(입력 대기) — exec 경로로 재확인하되 TUI 마커 존치는 자동 커버 불가(한계 명시). 카드 감지는
#      exec/relay 경로에서 확정 동작.
#   ③ compaction off — `config.toml model_auto_compact_token_limit=900000`(D4 ②)는 trusted project 에서
#      **오류 없이 로드**(unknown field/invalid config 없음·codex 0.144.6 수용). 완전-off 스위치는 여전히
#      미확인(900k fill 실측은 비현실적 과금) — 900k 상향이 실효 상한이고 auto-compact 는 최후 backstop,
#      현행 PreCompact는 compaction을 통과시키고 checkpoint 기록을 안내한다.
#   ④ hooks PreCompact 1>&2 셸-실행(T-0406 전제) — codex 는 hooks.json 의 문자열 command 를 **셸로
#      해석**한다. 실측: `echo … 1> <file>` 셸 리다이렉션이 파일을 생성(content 정확) → 같은 리다이렉션
#      클래스인 shipped `echo '…' 1>&2` 는 유효(stdout→stderr). **단 hook 은 trust 승인 후에만 발화** —
#      무승인 exec 에선 hook 미발화, `--dangerously-bypass-hook-trust`(또는 `/hooks` 승인)에서 발화
#      (D5 trust 2단계 안내 정합·T-0406 hooks.json 유효성 확인).

# ── live smoke (실 codex · 기본 skip · PM_RELAY_LIVE on-demand + release tier · T-0407 본체) ────────

PM_RELAY_LIVE = os.environ.get("PM_RELAY_LIVE") == "1"
# release tier 게이트 — `livegate record`(release wave·PM_ORCH_LIVE_RELEASE=1)에서 이 smoke 가 돌아야
# codex 라이브 green 이 릴리즈 pin(수집 N)에 편입된다(ADR-0070 D7). on-demand(PM_RELAY_LIVE)와 릴리즈
# (PM_ORCH_LIVE_RELEASE) 둘 중 하나면 실행 — 릴리즈 wave 는 후자만 set 하므로 게이트에 합류시킨다.
PM_ORCH_LIVE_RELEASE = os.environ.get("PM_ORCH_LIVE_RELEASE") == "1"


class _CodexHomeRunner:
    """실 `subprocess.run` 을 격리 CODEX_HOME env 로 감싸고 각 turn 의 stdout 을 기록한다.

    driver 는 turn subprocess 에 env 를 안 넘긴다(어댑터=부모 env 상속) — 라이브 테스트가 실 ~/.codex
    를 오염시키지 않도록 여기서 격리 홈(auth 복사본)을 주입한다. 기록한 stdout 은 usage 2턴 대조
    (input_tokens 누적 vs per-turn 실측)에 쓴다. driver 가 주는 stdin=DEVNULL 등 kwargs 는 그대로 전달."""

    def __init__(self, home: Path):
        self._home = home
        self.stdouts: list[str] = []

    def __call__(self, cmd, **kwargs):
        kwargs["env"] = codex_live_env(self._home)
        proc = subprocess.run(cmd, **kwargs)
        self.stdouts.append(proc.stdout or "")
        return proc


@pytest.mark.release
@pytest.mark.skipif(
    not (PM_RELAY_LIVE or PM_ORCH_LIVE_RELEASE) or not shutil.which("codex") or not codex_auth_available(),
    reason="live smoke — (PM_RELAY_LIVE 또는 PM_ORCH_LIVE_RELEASE)=1 + codex CLI(gpt-5.5 과금·~/.codex/auth.json) 필요(기본 skip·CI green 불변).",
)
def test_live_codex_thread_marker_identity_smoke(orch, driver_mod, tmp_path):
    """실 codex 1회 e2e (T-0407 본체) — spawn→resume 연속성·driver-파싱 tid==marker tid·usage 누적 실측.

    검증 축:
      - **연속성**: spawn 에서 code word(MANGO77)를 심고 `exec resume <tid>` 로 물으면 같은 세션이
        기억을 반환(resume 이 진짜 세션을 이어감·`-C`+resume 실효·티켓 명세 전제 라이브 확인).
      - **tid==marker**: driver 가 codex 출력에서 파싱한 thread_id 로 marker 를 쓰면 supervisor 가 같은
        sid 로 stat 성공(sid 예측 성립·엔진 회전 배선 전제).
      - **usage 누적 실측**: spawn·resume 2턴의 `turn.completed.usage.input_tokens`를 대조해
        input이 누계임(turn2 ⊃ turn1)을 단언한다. driver는 이 누계를 turn별 차분 폴백에 쓰고,
        rollout 정밀 신호가 있으면 `total_token_usage.input_tokens` 신선도 anchor와 대조한다.

    ⚠ stdin close(codex 무기한 대기 방어)는 driver 가 stdin=DEVNULL 로 박제. 격리 CODEX_HOME(auth
    복사·종료 시 삭제·runner 가 주입). 기본 skip(라이브 env 미설정·CI green 불변). **@release** — codex
    라이브 green 을 릴리즈 pin(수집 N)에 편입한다(ADR-0070 D7·pin 16→17 커플드 sweep: board.py·
    `test_release_wave._EXPECTED_RELEASE_TESTS`+`_RELEASE_TEST_FILES`·`test_worktree_pool._LIVEGATE_RELEASE_PIN`·
    `test_board_livegate` 동시 갱신·templates 는 PM 통합 시 pm_update 전파). 이 smoke 가 skip/누락되면
    수집<pin 으로 `livegate record` red — codex green 없이 v1.4.0 push 차단(티켓 목표)."""
    home = make_codex_home(tmp_path)
    runner = _CodexHomeRunner(home)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    try:
        driver = driver_mod.CodexCliDriver(
            orch.parse_codex_json, mark_stop=orch.write_post_turn_marker, root=tmp_path,
            runner=runner,  # 격리 홈 주입 + stdout 기록.
        )
        # spawn — 엔진 uuid4 는 driver 가 무시(codex 발급 thread_id 가 권위).
        observed = driver.spawn(
            str(workdir), orch.new_session_id(),
            "Remember this code word: MANGO77. Reply with exactly: STORED",
        ).session_id
        assert observed, "spawn 이 codex 출력에서 thread_id 를 파싱하지 못함(thread.started 부재?)"
        # resume 연속성 — 같은 세션이 code word 를 기억(exec resume <tid> 실효).
        reply = driver.relay_turn(
            observed, "What was the code word? Reply with only the code word."
        )
        assert "MANGO77" in reply.upper(), f"resume 연속성 실패 — reply={reply!r}"
        # driver-파싱 tid == marker tid: 그 tid 로 쓴 marker 를 supervisor 가 stat(sid 예측 성립).
        assert orch.write_post_turn_marker(tmp_path, observed) is True
        assert orch.stop_marker_present(tmp_path, observed) is True
        # input_tokens 누적 실측(usage 2턴 대조·runner 기록 stdout): 차분·rollout anchor의 원천.
        _, _, usage1 = orch.parse_codex_json(runner.stdouts[0].splitlines())
        _, _, usage2 = orch.parse_codex_json(runner.stdouts[1].splitlines())
        assert usage1 and usage2, f"usage 파싱 실패 — usage1={usage1} usage2={usage2}"
        # 실측(T-0407·codex 0.144.6): input은 누적 컨텍스트(turn2가 turn1 포함) → turn2 > turn1.
        # 판정은 이 누계의 turn 차분을 폴백으로 쓰고, rollout이 있으면 같은 누계 input을 anchor로 삼는다.
        assert usage2["input"] > usage1["input"], (
            f"input_tokens 가 누적이 아님(per-turn?) — turn1={usage1['input']} turn2={usage2['input']}. "
            "driver의 누계 차분 폴백과 rollout 신선도 anchor 전제를 재검토해야 한다.")
    finally:
        drop_codex_auth(home)  # scratch 에 auth 잔류 방지(라이브 규율).
