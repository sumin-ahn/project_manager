"""claude 어댑터 ctx 정지-핸드오프 단위 테스트 (T-0015).

어댑터 스크립트(templates/claude_code/.claude/ctx_guard·ctx_statusline·ctx_stop_hook)를
importlib 로 직접 로드해 검증한다. stdlib only — 라이브 claude·외부 호출 없이
가짜 transcript JSONL·가짜 statusline stdin·격리 tmp 만 본다.

검증 축:
  1. 임계 config — local.conf nudge/stop 읽기 + sanity 폴백 (codex 인계).
  2. statusLine — context_window → used % (native/manual/total_input 폴백) + 색/문구 넛지.
  3. 훅 — transcript JSONL 토큰합 → used %, 임계 분기(ok/nudge/stop), deny 출력 스키마.
  4. 멱등 — 정지·트리거 세션당 1회 (marker 가드로 중복 handoff 차단).
  5. settings 배선 — settings.json/settings.local.json 에 PreToolUse 훅·statusLine.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLAUDE = REPO / "templates" / "claude_code" / ".claude"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"claude_adapter_{name}", CLAUDE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard():
    return _load("ctx_guard")


@pytest.fixture(scope="module")
def statusline():
    return _load("ctx_statusline")


@pytest.fixture(scope="module")
def stop_hook():
    return _load("ctx_stop_hook")


# ── transcript JSONL fixture 헬퍼 ──────────────────────────────────────────

def _write_transcript(tmp_path: Path, messages) -> Path:
    """messages = [(role, usage_dict|None), ...] → JSONL 파일 경로."""
    path = tmp_path / "transcript.jsonl"
    lines = []
    for role, usage in messages:
        entry = {"type": role, "message": {"role": role}}
        if usage is not None:
            entry["message"]["usage"] = usage
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_content_transcript(tmp_path: Path, messages, name="content.jsonl") -> Path:
    """messages = [(role, content), ...] → JSONL.

    content 는 str (간단 텍스트) 또는 block list ([{type:"text",text:...}, ...])
    둘 다 받는다 (claude transcript 의 message.content 두 형식). extract_thread_tail
    검증용 — usage 기반 _write_transcript 와 별도.
    """
    path = tmp_path / name
    lines = []
    for role, content in messages:
        entry = {"type": role, "message": {"role": role, "content": content}}
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── 1. 임계 config (local.conf 읽기 + sanity 폴백) ──────────────────────────

def test_thresholds_defaults(guard):
    assert guard.ctx_thresholds({}) == {"nudge_pct": 20, "stop_pct": 10}
    assert guard.CTX_NUDGE_PCT_DEFAULT == 20
    assert guard.CTX_STOP_PCT_DEFAULT == 10


def test_thresholds_reads_conf(guard):
    th = guard.ctx_thresholds({"ctx_nudge_pct": "30", "ctx_stop_pct": "5"})
    assert th == {"nudge_pct": 30, "stop_pct": 5}


def test_thresholds_sanity_fallback(guard):
    # stop > nudge (역전) → 기본 폴백.
    assert guard.ctx_thresholds({"ctx_nudge_pct": "5", "ctx_stop_pct": "30"}) == {
        "nudge_pct": 20, "stop_pct": 10
    }
    # 음수 → 기본 폴백.
    assert guard.ctx_thresholds({"ctx_stop_pct": "-3"}) == {"nudge_pct": 20, "stop_pct": 10}
    # 비정수 → 기본 폴백.
    assert guard.ctx_thresholds({"ctx_nudge_pct": "abc"}) == {"nudge_pct": 20, "stop_pct": 10}
    # 100 이상 → 기본 폴백.
    assert guard.ctx_thresholds({"ctx_nudge_pct": "100"}) == {"nudge_pct": 20, "stop_pct": 10}


def test_load_local_config_parses(guard, tmp_path):
    pm = tmp_path / ".project_manager"
    pm.mkdir()
    (pm / "local.conf").write_text(
        "# comment\nctx_nudge_pct=25\n\nctx_stop_pct = 8\nprefix=PAY\n", encoding="utf-8"
    )
    conf = guard.load_local_config(tmp_path)
    assert conf["ctx_nudge_pct"] == "25"
    assert conf["ctx_stop_pct"] == "8"
    assert conf["prefix"] == "PAY"


def test_load_local_config_missing(guard, tmp_path):
    assert guard.load_local_config(tmp_path) == {}


def test_window_tokens_default_and_override(guard):
    assert guard.ctx_window_tokens({}) == 200_000
    assert guard.ctx_window_tokens({"ctx_window_tokens": "100000"}) == 100_000
    # 비정상(0·음수·비정수) → 기본.
    assert guard.ctx_window_tokens({"ctx_window_tokens": "0"}) == 200_000
    assert guard.ctx_window_tokens({"ctx_window_tokens": "-5"}) == 200_000


# ── 2. statusLine: context_window → used % + 넛지 ──────────────────────────

def test_statusline_used_pct_native(guard):
    assert guard.context_used_pct_from_statusline({"context_window": {"used_percentage": 73}}) == 73


def test_statusline_used_pct_manual_fallback(guard):
    # native 없음 → current_usage 토큰합 / size.
    sl = {
        "context_window": {
            "context_window_size": 200_000,
            "current_usage": {
                "input_tokens": 100_000,
                "cache_creation_input_tokens": 40_000,
                "cache_read_input_tokens": 40_000,
            },
        }
    }
    assert guard.context_used_pct_from_statusline(sl) == 90


def test_statusline_used_pct_total_input_fallback(guard):
    # native·manual 없음 → total_input_tokens / size.
    sl = {"context_window": {"context_window_size": 200_000, "total_input_tokens": 50_000}}
    assert guard.context_used_pct_from_statusline(sl) == 25


def test_statusline_no_signal_zero(guard):
    assert guard.context_used_pct_from_statusline({}) == 0
    assert guard.context_used_pct_from_statusline({"context_window": {}}) == 0
    assert guard.context_used_pct_from_statusline({"context_window": "bad"}) == 0


def test_statusline_render_colors(guard, statusline):
    th = {"nudge_pct": 20, "stop_pct": 10}
    # ok (used 50, 잔여 50): 회색·정지문구 없음.
    ok = statusline.build_statusline({"context_window": {"used_percentage": 50}}, {})
    assert "\033[90m" in ok and "ctx 50%" in ok and "정지" not in ok
    # nudge (used 85, 잔여 15 <= 20): 노랑·"곧 정지".
    nudge = statusline.build_statusline({"context_window": {"used_percentage": 85}}, {})
    assert "\033[33m" in nudge and "곧 정지" in nudge
    # stop (used 92, 잔여 8 <= 10): 빨강·"정지 임계".
    stop = statusline.build_statusline({"context_window": {"used_percentage": 92}}, {})
    assert "\033[31m" in stop and "정지 임계" in stop


def test_classify_boundaries(guard):
    th = {"nudge_pct": 20, "stop_pct": 10}
    assert guard.classify(50, th) == "ok"
    assert guard.classify(80, th) == "nudge"   # 잔여 20 == nudge_pct → nudge.
    assert guard.classify(79, th) == "ok"      # 잔여 21 > nudge_pct → ok.
    assert guard.classify(90, th) == "stop"    # 잔여 10 == stop_pct → stop.
    assert guard.classify(89, th) == "nudge"   # 잔여 11 > stop_pct → nudge.


# ── 3. 훅: transcript 토큰합 → used % + deny ───────────────────────────────

def test_transcript_tokens_last_request(guard, tmp_path):
    # 가장 최근(끝) assistant usage 의 입력+캐시 토큰 = 현재 점유.
    path = _write_transcript(tmp_path, [
        ("user", None),
        ("assistant", {"input_tokens": 1000, "cache_read_input_tokens": 500}),
        ("user", None),
        ("assistant", {"input_tokens": 100_000, "cache_creation_input_tokens": 50_000,
                       "cache_read_input_tokens": 30_000}),
    ])
    assert guard.context_tokens_from_transcript(path) == 180_000


def test_transcript_used_pct(guard, tmp_path):
    path = _write_transcript(tmp_path, [
        ("assistant", {"input_tokens": 180_000}),
    ])
    assert guard.context_used_pct_from_transcript(path, 200_000) == 90


def test_transcript_missing_file_zero(guard, tmp_path):
    assert guard.context_tokens_from_transcript(tmp_path / "nope.jsonl") == 0
    assert guard.context_used_pct_from_transcript(tmp_path / "nope.jsonl", 200_000) == 0


def test_transcript_malformed_lines_skipped(guard, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"message": {"usage": {"input_tokens": 20_000}}}) + "\n"
        + "{broken\n",
        encoding="utf-8",
    )
    assert guard.context_tokens_from_transcript(path) == 20_000


# ── 3b. thread-tail 추출 (handoff 자동 채움 — T-0047) ───────────────────────

def test_thread_tail_user_only_chronological(guard, tmp_path):
    """user 발화만 수집·역순→시간순 복원 (assistant 제외)."""
    path = _write_content_transcript(tmp_path, [
        ("user", "첫 요청"),
        ("assistant", "어시스턴트 응답 — 제외돼야 함"),
        ("user", "두 번째 요청"),
        ("assistant", "또 다른 응답"),
        ("user", "마지막 요청"),
    ])
    tail = guard.extract_thread_tail(path)
    # assistant 텍스트는 들어가지 않는다.
    assert "어시스턴트 응답" not in tail
    assert "또 다른 응답" not in tail
    # 시간순(오래된→최신)으로 복원된다.
    assert tail == "첫 요청 / 두 번째 요청 / 마지막 요청"


def test_thread_tail_max_turns_cap(guard, tmp_path):
    """최근 max_turns 개 user 발화만 (역순 수집 → 시간순 복원)."""
    path = _write_content_transcript(tmp_path, [
        ("user", "t1"), ("user", "t2"), ("user", "t3"), ("user", "t4"), ("user", "t5"),
    ])
    # 최근 3턴(t3·t4·t5)만 시간순으로.
    assert guard.extract_thread_tail(path, max_turns=3) == "t3 / t4 / t5"


def test_thread_tail_excludes_tool_result_only(guard, tmp_path):
    """tool_result-only user turn 은 발화가 아니므로 제외된다."""
    path = _write_content_transcript(tmp_path, [
        ("user", "진짜 발화"),
        ("user", [{"type": "tool_result", "tool_use_id": "x", "content": "도구 결과"}]),
    ])
    tail = guard.extract_thread_tail(path)
    assert tail == "진짜 발화"
    assert "도구 결과" not in tail


def test_thread_tail_block_list_text_only(guard, tmp_path):
    """block-list content 에서 text 블록만 추출 (tool_result 블록 섞여도)."""
    path = _write_content_transcript(tmp_path, [
        ("user", [
            {"type": "tool_result", "tool_use_id": "x", "content": "결과 무시"},
            {"type": "text", "text": "블록 안 텍스트"},
        ]),
    ])
    tail = guard.extract_thread_tail(path)
    assert tail == "블록 안 텍스트"
    assert "결과 무시" not in tail


def test_thread_tail_str_and_block_forms(guard, tmp_path):
    """str content 와 block-list content 가 섞여도 둘 다 추출된다 (양형식)."""
    path = _write_content_transcript(tmp_path, [
        ("user", "str 형식 발화"),
        ("user", [{"type": "text", "text": "block 형식 발화"}]),
    ])
    assert guard.extract_thread_tail(path) == "str 형식 발화 / block 형식 발화"


def test_thread_tail_newline_flattened(guard, tmp_path):
    """발화 내 개행은 ' / ' 로 1줄 평탄화된다 (handoff entry 줄 단위 슬롯)."""
    path = _write_content_transcript(tmp_path, [
        ("user", "첫 줄\n둘째 줄\n셋째 줄"),
    ])
    assert guard.extract_thread_tail(path) == "첫 줄 / 둘째 줄 / 셋째 줄"


def test_thread_tail_max_chars_cap(guard, tmp_path):
    """결합 결과는 총 max_chars 로 캡된다."""
    path = _write_content_transcript(tmp_path, [
        ("user", "가" * 50),
        ("user", "나" * 50),
    ])
    tail = guard.extract_thread_tail(path, max_chars=30)
    assert len(tail) <= 30


def test_thread_tail_missing_file_failsoft(guard, tmp_path):
    """누락 transcript → '' (fail-soft)."""
    assert guard.extract_thread_tail(tmp_path / "nope.jsonl") == ""


def test_thread_tail_empty_and_no_user_failsoft(guard, tmp_path):
    """빈 transcript·user 발화 없음 → '' (엔진이 placeholder 유지)."""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert guard.extract_thread_tail(empty) == ""
    # assistant 만 있는 transcript.
    only_asst = _write_content_transcript(tmp_path, [("assistant", "응답만")], name="asst.jsonl")
    assert guard.extract_thread_tail(only_asst) == ""


def test_thread_tail_malformed_lines_skipped(guard, tmp_path):
    """파싱 불가 줄은 건너뛰고 유효 user 발화만 (context_tokens 파서 재사용 견고성)."""
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"message": {"role": "user", "content": "유효 발화"}}) + "\n"
        + "{broken\n",
        encoding="utf-8",
    )
    assert guard.extract_thread_tail(path) == "유효 발화"


def test_hook_evaluate_ok_passes(guard, stop_hook, tmp_path):
    # 잔여 넉넉 (used 50) → 출력 없이 통과.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 100_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-ok"}
    calls = []
    rc, output = stop_hook.evaluate(
        stdin, tmp_path, {}, handoff_fn=lambda root, pct: calls.append(pct) or 0
    )
    assert rc == 0 and output is None
    assert calls == []  # ok → handoff 안 함.


def test_hook_evaluate_stop_denies_and_triggers(guard, stop_hook, tmp_path):
    # used 92% (잔여 8 <= 10) → deny + handoff 1회.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 184_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-stop"}
    calls = []
    rc, output = stop_hook.evaluate(
        stdin, tmp_path, {}, handoff_fn=lambda root, pct: calls.append(pct) or 0
    )
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "새 세션" in hso["permissionDecisionReason"]
    assert "ctx-stop" in hso["permissionDecisionReason"]
    # handoff 가 잔여 ctx-pct 로 1회 호출됐다.
    assert calls == [8]


def test_hook_idempotent_single_trigger(guard, stop_hook, tmp_path):
    """같은 세션에서 두 번 정지 임계여도 handoff 는 1회만 (marker 가드)."""
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-idem"}
    calls = []
    fn = lambda root, pct: calls.append(pct) or 0
    rc1, out1 = stop_hook.evaluate(stdin, tmp_path, {}, handoff_fn=fn)
    rc2, out2 = stop_hook.evaluate(stdin, tmp_path, {}, handoff_fn=fn)
    # 두 번 다 deny.
    assert out1["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out2["hookSpecificOutput"]["permissionDecision"] == "deny"
    # handoff 는 1회만.
    assert calls == [5]
    # 두 번째 deny 는 "이미 박제됨" 안내.
    assert "이미 핸드오프 박제됨" in out2["hookSpecificOutput"]["permissionDecisionReason"]
    # marker 파일이 생성됐다.
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-idem.done").exists()


def test_hook_no_transcript_passes(stop_hook, tmp_path):
    # transcript_path 없음 → used 0 → 통과.
    rc, output = stop_hook.evaluate({"session_id": "x"}, tmp_path, {}, handoff_fn=lambda r, p: 0)
    assert rc == 0 and output is None


def test_hook_handoff_failure_still_denies(guard, stop_hook, tmp_path):
    # handoff rc!=0 이어도 정지(deny)는 유효하고 실패 안내 포함.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 184_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-fail"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {}, handoff_fn=lambda r, p: 1)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "실패" in output["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_handoff_failure_retries_next_call(stop_hook, tmp_path):
    # handoff 실패(rc!=0)면 marker 안 남김 → 다음 호출이 재시도 (미박제인데 "이미 박제됨" 처리되는 버그 방지).
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-retry"}
    calls = []
    fn = lambda root, pct: calls.append(pct) or 1  # 항상 실패(rc=1)
    stop_hook.evaluate(stdin, tmp_path, {}, handoff_fn=fn)
    stop_hook.evaluate(stdin, tmp_path, {}, handoff_fn=fn)
    # 실패라 marker 미생성 → 두 번 다 재시도(handoff 2회 호출).
    assert len(calls) == 2
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-retry.done").exists()


def test_hook_user_prompt_submit_blocks(guard, stop_hook, tmp_path):
    # UserPromptSubmit 이벤트 → prompt 자체 block (새 작업 진입 차단·T-0012 정지 경계).
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-ups",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {}, handoff_fn=lambda r, p: 0)
    # UserPromptSubmit 은 top-level block 스키마 (PreToolUse 의 hookSpecificOutput 아님).
    assert output["decision"] == "block"
    assert "새 세션" in output["reason"]


def test_hook_pretooluse_default_when_no_event(guard, stop_hook, tmp_path):
    # hook_event_name 없으면 기본 PreToolUse(deny) — 후방호환.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-def"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {}, handoff_fn=lambda r, p: 0)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_settings_wires_user_prompt_submit():
    # settings.json 에 UserPromptSubmit 훅(ctx_stop_hook) 배선 — 새 작업 진입 차단.
    data = json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8"))
    ups = data["hooks"]["UserPromptSubmit"]
    assert isinstance(ups, list) and ups
    cmds = [h.get("command", "") for m in ups for h in m.get("hooks", [])]
    assert any("ctx_stop_hook.py" in c for c in cmds), "UserPromptSubmit 에 ctx_stop_hook 누락"


def test_hook_session_id_sanitized(stop_hook):
    # 경로 traversal 문자는 제거된다 (marker 파일명 안전).
    assert "/" not in stop_hook._session_id({"session_id": "../../etc/passwd"})
    assert stop_hook._session_id({}) == "unknown"


def test_run_handoff_builds_trigger_cmd(stop_hook, tmp_path):
    captured = {}
    def fake_run(cmd, cwd, capture_output, text):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        class R: returncode = 0
        return R()
    rc = stop_hook.run_handoff(tmp_path, 7, runner=fake_run)
    assert rc == 0
    cmd = captured["cmd"]
    assert "--trigger" in cmd and "--reason" in cmd and "ctx-stop" in cmd
    assert "--ctx-pct" in cmd and "7" in cmd
    assert cmd[1].endswith("pm_handoff.py")
    # thread_tail 미전달 시 --thread-tail 은 배선되지 않는다 (하위호환).
    assert "--thread-tail" not in cmd


def test_run_handoff_wires_thread_tail(stop_hook, tmp_path):
    """thread_tail 비어있지 않으면 cmd 에 --thread-tail <text> 배선 (T-0047)."""
    captured = {}
    def fake_run(cmd, cwd, capture_output, text):
        captured["cmd"] = cmd
        class R: returncode = 0
        return R()
    rc = stop_hook.run_handoff(tmp_path, 7, runner=fake_run, thread_tail="정지 직전 발화")
    assert rc == 0
    cmd = captured["cmd"]
    assert "--thread-tail" in cmd
    idx = cmd.index("--thread-tail")
    assert cmd[idx + 1] == "정지 직전 발화"


def test_run_handoff_empty_thread_tail_omits_flag(stop_hook, tmp_path):
    """빈 thread_tail 은 --thread-tail 을 붙이지 않는다 (엔진 placeholder 유지)."""
    captured = {}
    def fake_run(cmd, cwd, capture_output, text):
        captured["cmd"] = cmd
        class R: returncode = 0
        return R()
    stop_hook.run_handoff(tmp_path, 7, runner=fake_run, thread_tail="")
    assert "--thread-tail" not in captured["cmd"]


def test_hook_evaluate_extracts_and_wires_thread_tail(guard, stop_hook, tmp_path):
    """evaluate() 가 정지 시 transcript_path 로 extract_thread_tail 호출 → run_handoff 에 전달.

    handoff_fn 미주입(기본 경로) 시 run_handoff 가 호출되므로 그 runner 를 가로채
    --thread-tail 배선까지 end-to-end 확인한다.
    """
    # used 92% (잔여 8 <= 10) → 정지. usage + content 를 한 transcript 에 같이 둔다.
    path = tmp_path / "stop.jsonl"
    path.write_text(
        json.dumps({"message": {"role": "user", "content": "정지 직전 사용자 발화"}}) + "\n"
        + json.dumps({"message": {"role": "assistant", "usage": {"input_tokens": 184_000}}}) + "\n",
        encoding="utf-8",
    )
    stdin = {"transcript_path": str(path), "session_id": "sess-tail"}
    captured = {}
    real_run_handoff = stop_hook.run_handoff
    def spy_run_handoff(root, ctx_pct, runner=None, thread_tail=""):
        captured["thread_tail"] = thread_tail
        return 0
    # handoff_fn=None → 기본 run_handoff 경로 사용. run_handoff 를 spy 로 대체해
    # evaluate 가 추출한 thread_tail 을 전달하는지 확인한다.
    import unittest.mock as _mock
    with _mock.patch.object(stop_hook, "run_handoff", spy_run_handoff):
        rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    # evaluate 가 transcript 에서 추출한 정지 직전 발화를 run_handoff 에 전달했다.
    assert captured["thread_tail"] == "정지 직전 사용자 발화"
    assert real_run_handoff is stop_hook.run_handoff  # 패치 복원 확인.


# ── 4. main() stdin 경로 (가짜 stdin → stdout JSON) ────────────────────────

def test_statusline_main_emits_line(statusline, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"context_window": {"used_percentage": 95}})))
    rc = statusline.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "ctx 95%" in out and "정지 임계" in out


def test_statusline_main_empty_stdin(statusline, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = statusline.main()
    out = capsys.readouterr().out
    assert rc == 0 and "ctx 0%" in out


# ── 5. settings 배선 (statusLine·PreToolUse 훅) ────────────────────────────

@pytest.mark.parametrize("name", ["settings.json"])
def test_settings_wires_statusline(name):
    data = json.loads((CLAUDE / name).read_text(encoding="utf-8"))
    sl = data.get("statusLine")
    assert isinstance(sl, dict), f"{name} 에 statusLine 누락"
    assert "ctx_statusline.py" in sl["command"]


@pytest.mark.parametrize("name", ["settings.json"])
def test_settings_wires_pretooluse_hook(name):
    data = json.loads((CLAUDE / name).read_text(encoding="utf-8"))
    pre = data["hooks"]["PreToolUse"]
    assert isinstance(pre, list) and pre
    cmds = [
        h.get("command", "")
        for matcher in pre
        for h in matcher.get("hooks", [])
    ]
    assert any("ctx_stop_hook.py" in c for c in cmds), f"{name} PreToolUse 에 ctx_stop_hook 누락"


@pytest.mark.parametrize("name", ["settings.json"])
def test_settings_preserves_posttooluse(name):
    # 기존 PostToolUse(run_tests_hook) 가 보존됐다 (회귀 — 무관한 hook 안 깨짐).
    data = json.loads((CLAUDE / name).read_text(encoding="utf-8"))
    post = data["hooks"]["PostToolUse"]
    cmds = [h.get("command", "") for m in post for h in m.get("hooks", [])]
    assert any("run_tests_hook.sh" in c for c in cmds)
