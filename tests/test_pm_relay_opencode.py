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
import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
    return _load("pm_relay", TOOLS / "pm_relay.py")


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
    got_sid, reply = orch.parse_opencode_json(lines)
    assert got_sid == sid
    assert reply == "PONG"


def test_parse_opencode_accumulates_multiple_text_parts(orch):
    """멀티 text part 는 등장 순서대로 누적 — 한 답변이 여러 part 로 쪼개져 와도 합친다."""
    sid = "ses_abc"
    lines = [_text_ev(sid, "Hello "), _text_ev(sid, "world"), _text_ev(sid, "!")]
    got_sid, reply = orch.parse_opencode_json(lines)
    assert got_sid == sid
    assert reply == "Hello world!"


def test_parse_opencode_sid_from_first_event(orch):
    """sid 는 첫 등장 이벤트에서 잡는다(text 이벤트 전의 step_start 도 sessionID 보유)."""
    sid = "ses_first"
    lines = [
        _ev(type="step_start", sessionID=sid, part={}),
        _text_ev(sid, "reply"),
    ]
    got_sid, _ = orch.parse_opencode_json(lines)
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
    got_sid, reply = orch.parse_opencode_json(lines)
    assert got_sid == sid
    assert reply == "answer"


def test_parse_opencode_ignores_non_dict_events(orch):
    """JSON 배열/스칼라 라인은 dict 가 아니라 skip(robust)."""
    sid = "ses_y"
    lines = ["[1,2,3]", "42", _text_ev(sid, "x")]
    got_sid, reply = orch.parse_opencode_json(lines)
    assert got_sid == sid and reply == "x"


def test_parse_opencode_empty_and_no_text(orch):
    assert orch.parse_opencode_json([]) == (None, None)
    # sid 만 있고 text part 0 → reply None.
    lines = [_ev(type="step_start", sessionID="ses_z", part={})]
    assert orch.parse_opencode_json(lines) == ("ses_z", None)


def test_parse_opencode_text_event_without_part_text(orch):
    """type:text 인데 part.text 누락/비-문자열이면 누적 안 함(edge robust)."""
    sid = "ses_w"
    lines = [
        _json.dumps({"type": "text", "sessionID": sid, "part": {"type": "text"}}),
        _json.dumps({"type": "text", "sessionID": sid, "part": None}),
        _json.dumps({"type": "text", "sessionID": sid}),  # part 키 자체 없음.
        _text_ev(sid, "real"),
    ]
    got_sid, reply = orch.parse_opencode_json(lines)
    assert got_sid == sid and reply == "real"


def test_parse_opencode_no_sessionid(orch):
    """sessionID 없는 이벤트만 있으면 sid None(폴백은 driver 가 처리)."""
    lines = [_json.dumps({"type": "text", "part": {"type": "text", "text": "x"}})]
    sid, reply = orch.parse_opencode_json(lines)
    assert sid is None and reply == "x"


# ── opencode driver (FakeRunner DI·실 opencode 무호출) ───────────────────────

class _FakeCompleted:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _json_stream(sid: str, reply: str) -> str:
    return "\n".join(
        [
            _ev(type="step_start", sessionID=sid, part={"type": "step-start"}),
            _text_ev(sid, reply),
            _ev(type="step_finish", sessionID=sid, part={"type": "step-finish"}),
        ]
    )


# ── ② driver spawn (엔진 uuid4 무시·출력 sid 반환·--agent·--dir·--format json) ─

def test_opencode_driver_spawn_ignores_uuid_returns_parsed_sid(orch, driver_mod):
    """spawn 이 엔진 uuid4 인자를 **무시** 하고 opencode 출력에서 파싱한 sid 를 반환한다
    (opencode sid 사전지정 불가·실측 → 출력 파싱이 권위)."""
    captured = {}
    opencode_sid = "ses_OPENCODE_ISSUED"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(_json_stream(opencode_sid, "READY"))

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    engine_uuid = "11111111-2222-3333-4444-555555555555"
    observed = driver.spawn("/repo/root", engine_uuid, "bootstrap text")
    # 엔진 uuid4 가 아니라 opencode 가 발급한 sid 를 반환.
    assert observed == opencode_sid
    assert observed != engine_uuid
    cmd = captured["cmd"]
    assert cmd[:3] == ["opencode", "run", "--format"]  # run + json 포맷.
    assert "json" in cmd
    assert "--agent" in cmd and "pm" in cmd  # PM primary 기본 spawn 타깃(T-0045).
    assert "--dir" in cmd and "/repo/root" in cmd  # child cwd 격리.
    assert "-s" not in cmd  # 첫 spawn 은 resume 안 함.
    assert cmd[-1] == "bootstrap text"  # message positional 은 맨 끝.


def test_opencode_driver_spawn_agent_arg(orch, driver_mod):
    """`--agent` 인자화 — build 폴백 등 커스텀 agent 지정 가능."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(_json_stream("ses_b", "READY"))

    driver = driver_mod.OpencodeCliDriver(
        orch.parse_opencode_json, agent="build", runner=fake_run
    )
    driver.spawn("/r", "uuid", "boot")
    cmd = captured["cmd"]
    assert "--agent" in cmd
    assert cmd[cmd.index("--agent") + 1] == "build"


def test_opencode_driver_spawn_sid_parse_failure_raises(orch, driver_mod):
    """sid 파싱 실패 = 치명·명시 중단 (codex T-0048).

    opencode 는 sid 사전지정 불가라 엔진 uuid 폴백 시 그 세션이 존재하지 않아 다음 relay_turn 의
    `-s <uuid>` 가 "Session not found" → 연속성 침묵 파손. 따라서 폴백 대신 RuntimeError 로
    명시 중단한다(relay 는 유효 세션 없이 못 돈다)."""
    import pytest

    def fake_run(cmd, **kwargs):
        return _FakeCompleted("no sessionID here\nplain text")  # 비-JSON → sid None.

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    with pytest.raises(RuntimeError, match="sessionID"):
        driver.spawn("/r", "engine-uuid-ignored", "boot")


# ── ③ driver relay_turn (-s resume·reply 파싱·spawn cwd 재사용) ───────────────

def test_opencode_driver_relay_uses_session_flag(orch, driver_mod):
    """relay_turn 이 `-s <sid>` 로 같은 세션을 resume 하고 reply 를 반환."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(_json_stream("ses_r", "hello back"))

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    reply = driver.relay_turn("ses_r", "ping")
    assert reply == "hello back"
    cmd = captured["cmd"]
    assert "-s" in cmd
    assert cmd[cmd.index("-s") + 1] == "ses_r"
    assert "--agent" not in cmd  # resume 은 새 세션 발급 안 함.
    assert cmd[-1] == "ping"


def test_opencode_driver_relay_reuses_spawn_cwd(orch, driver_mod):
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

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    sid = driver.spawn("/repo/root", "uuid", "boot")
    driver.relay_turn(sid, "msg")
    assert dirs == ["/repo/root", "/repo/root"]


def test_opencode_driver_relay_unknown_session_no_dir(orch, driver_mod):
    """모르는 세션(메타 없음) relay 는 --dir 없이도 fail-soft(빈 cwd 메타)."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(_json_stream("ses_u", "r"))

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    reply = driver.relay_turn("ses_never_spawned", "x")
    assert reply == "r"
    assert "--dir" not in captured["cmd"]  # cwd 메타 없으면 --dir 생략.


# ── ④ driver close (no-op·세션 cwd 메타 정리) ────────────────────────────────

def test_opencode_driver_close_is_noop_and_clears_meta(orch, driver_mod):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(_json_stream("ses_c", "READY"))

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    sid = driver.spawn("/r", "uuid", "boot")
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

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    assert driver.relay_turn("ses", "x") == ""


def test_opencode_driver_relay_oserror_returns_empty(orch, driver_mod):
    """opencode 바이너리 부재(OSError)도 fail-soft."""
    def fake_run(cmd, **kwargs):
        raise OSError("opencode not found")

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    assert driver.relay_turn("ses", "x") == ""


def test_opencode_driver_parser_flags(driver_mod):
    parser = driver_mod.build_parser()
    ns = parser.parse_args(["--cwd", "/some/repo", "--agent", "build"])
    assert ns.cwd == "/some/repo" and ns.agent == "build"
    ns2 = parser.parse_args([])
    assert ns2.cwd is None  # 기본 = 실행 dir(main 에서 os.getcwd()).
    assert ns2.agent == "pm"  # PM primary 기본.


def test_opencode_driver_repo_root_finds_engine(driver_mod, tmp_path):
    """repo_root 가 pm_handoff.py 가 있는 조상을 엔진 루트로 찾는다(JS findEngineRoot 동형)."""
    (tmp_path / ".project_manager" / "tools").mkdir(parents=True)
    (tmp_path / ".project_manager" / "tools" / "pm_handoff.py").write_text("x")
    nested = tmp_path / ".opencode"
    nested.mkdir()
    assert driver_mod.repo_root(nested) == tmp_path.resolve()


# ── ⑥ subprocess 폭발 가드 (driver 가 실 opencode 안 부름) ────────────────────

def test_opencode_driver_does_not_spawn_real_subprocess(orch, driver_mod, monkeypatch):
    """driver 경로가 (FakeRunner 주입 시) 실 subprocess.run 을 호출하지 않는다."""
    import subprocess as _sp

    def _boom(*a, **k):
        raise AssertionError("driver 가 실 subprocess 를 호출했다")

    monkeypatch.setattr(_sp, "run", _boom)
    monkeypatch.setattr(_sp, "Popen", _boom)

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(_json_stream("ses_safe", "READY"))

    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    sid = driver.spawn("/r", "uuid", "boot")
    assert driver.relay_turn(sid, "x") == "READY"


# ── 엔진 Supervisor + opencode driver 결합 (FakeRunner·회전까지) ──────────────

def test_supervisor_with_opencode_driver_rotation(orch, driver_mod, tmp_path):
    """엔진 Supervisor 가 opencode driver 로 spawn→relay→marker→respawn 회전한다
    (엔진 재사용 확증 — 양 하니스 동일 코드). marker 는 첫 relay 직후 강제 박제."""
    issued = iter(["ses_first", "ses_first", "ses_second", "ses_second"])
    relay_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        # spawn(--agent) 과 relay(-s) 모두 다음 sid stream 을 돌려준다.
        if "--agent" in cmd:
            sid = "ses_first" if relay_count["n"] == 0 else "ses_second"
            return _FakeCompleted(_json_stream(sid, "READY"))
        # relay — 현재 활성 세션 sid 로 응답.
        relay_count["n"] += 1
        sid = cmd[cmd.index("-s") + 1]
        stream = _json_stream(sid, f"reply:{cmd[-1]}")
        # 첫 relay 직후 marker 강제(ctx-guard.js plugin 모사) → supervisor STOP 관측.
        if relay_count["n"] == 1:
            mp = orch._marker_path(tmp_path, sid)
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_text("ctx-stop handoff triggered\n", encoding="utf-8")
        return _FakeCompleted(stream)

    import io
    driver = driver_mod.OpencodeCliDriver(orch.parse_opencode_json, runner=fake_run)
    sup = orch.Supervisor(driver, root=tmp_path)
    rc = sup.run_loop("/repo/root", io.StringIO("first\nsecond\n"), io.StringIO())
    assert rc == 0
    # 회전 발생 — 두 세션이 쓰였다(첫 relay marker → respawn).
    assert relay_count["n"] >= 2


# ── live smoke (실 opencode · 기본 skip · 로컬 ollama) ────────────────────────

PM_ORCH_LIVE = os.environ.get("PM_ORCH_LIVE") == "1"
# 로컬 ollama 모델 — 가벼운 것. 환경변수로 override 가능.
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/gemma4:26b")


@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("opencode"),
    reason="live smoke — PM_ORCH_LIVE=1 + opencode CLI 필요(기본 skip·CI green 불변).",
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
        orch.parse_opencode_json, runner=_live_runner_with_model(LIVE_MODEL)
    )

    # ── spawn: opencode 가 sid 발급 → driver 가 출력에서 파싱·반환 ──
    observed = driver.spawn(
        str(tmp_path),
        orch.new_session_id(),  # 엔진 uuid4 — driver 가 무시할 값.
        "Remember this code word: MANGO77. Reply with exactly: STORED",
    )
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
    new_observed = driver.spawn(
        str(tmp_path),
        orch.new_session_id(),
        "Prior code word was MANGO77. Reply with exactly: CONTINUED",
    )
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
