"""claude 어댑터 ctx 정지-핸드오프 단위 테스트 (T-0015).

어댑터 스크립트(templates/claude_code/.claude/ctx_guard·ctx_statusline·ctx_stop_hook)를
importlib 로 직접 로드해 검증한다. stdlib only — 라이브 claude·외부 호출 없이
가짜 transcript JSONL·가짜 statusline stdin·격리 tmp 만 본다.

검증 축:
  1. 임계 config — local.conf nudge/stop 읽기 + sanity 폴백 (codex 인계).
  2. statusLine — context_window → used % (current_usage 단일 소스·ADR-0041) + 색/문구 넛지.
  3. 훅 — transcript JSONL 토큰합 → used %, 임계 분기(ok/nudge/stop), deny/block 출력 스키마.
  3c. 핸드오프 도구 allow-list (ADR-0038 D2) — stop 밴드에서 진행 중 /pm-handoff 도구는
      통과(None)·새 작업 도구는 deny. Bash 셸-연산자 밀반입 fail-closed·env-prefix 정규화.
  4. STOP marker — stop 도달 시 `.done` 무조건 박제(relay 신호·ADR-0038 D4·handoff_rc 게이트 없음),
      멱등=파일 존재(write 실패 시 부재로 남아 다음 호출 self-heal 재시도).
  5. settings 배선 — settings.json 에 PreToolUse·UserPromptSubmit 훅·statusLine.
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


# ── 1. 임계 config (local.conf 읽기 + sanity 폴백) ──────────────────────────

def test_thresholds_defaults(guard):
    assert guard.ctx_thresholds({}) == {"nudge_pct": 30, "stop_pct": 20}
    assert guard.CTX_NUDGE_PCT_DEFAULT == 30
    assert guard.CTX_STOP_PCT_DEFAULT == 20


def test_thresholds_reads_conf(guard):
    th = guard.ctx_thresholds({"ctx_nudge_pct": "30", "ctx_stop_pct": "5"})
    assert th == {"nudge_pct": 30, "stop_pct": 5}


def test_thresholds_sanity_fallback(guard):
    # stop > nudge (역전) → 기본 폴백.
    assert guard.ctx_thresholds({"ctx_nudge_pct": "5", "ctx_stop_pct": "30"}) == {
        "nudge_pct": 30, "stop_pct": 20
    }
    # 음수 → 기본 폴백.
    assert guard.ctx_thresholds({"ctx_stop_pct": "-3"}) == {"nudge_pct": 30, "stop_pct": 20}
    # 비정수 → 기본 폴백.
    assert guard.ctx_thresholds({"ctx_nudge_pct": "abc"}) == {"nudge_pct": 30, "stop_pct": 20}
    # 100 이상 → 기본 폴백.
    assert guard.ctx_thresholds({"ctx_nudge_pct": "100"}) == {"nudge_pct": 30, "stop_pct": 20}


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
    # generic-only 헬퍼(back-compat) — 유지(하네스 오버라이드는 무시·resolve_budget 이 담당).
    assert guard.ctx_window_tokens({}) == 200_000
    assert guard.ctx_window_tokens({"ctx_window_tokens": "100000"}) == 100_000
    # 비정상(0·음수·비정수) → 기본.
    assert guard.ctx_window_tokens({"ctx_window_tokens": "0"}) == 200_000
    assert guard.ctx_window_tokens({"ctx_window_tokens": "-5"}) == 200_000


# ── 1b. resolve_budget: per-harness precedence (ADR-0041 Decision 1) ────────
# 분모 = ctx_window_tokens_<harness> > generic ctx_window_tokens > 200000 (각 층 >0 sanity).

def test_resolve_budget_defaults_to_200k(guard):
    assert guard.resolve_budget({}, "claude") == 200_000
    assert guard.resolve_budget({}, "opencode") == 200_000
    assert guard.resolve_budget({}) == 200_000  # harness 기본값 = claude.


def test_resolve_budget_generic_fallback(guard):
    # 하네스 오버라이드 없음 → generic ctx_window_tokens (back-compat·② 1M 무변경).
    assert guard.resolve_budget({"ctx_window_tokens": "1000000"}, "claude") == 1_000_000
    assert guard.resolve_budget({"ctx_window_tokens": "1000000"}, "opencode") == 1_000_000


def test_resolve_budget_harness_override_wins(guard):
    # 하네스 키가 generic 을 이긴다 (precedence 최상층).
    conf = {
        "ctx_window_tokens": "300000",
        "ctx_window_tokens_claude": "1000000",
        "ctx_window_tokens_opencode": "200000",
    }
    assert guard.resolve_budget(conf, "claude") == 1_000_000
    assert guard.resolve_budget(conf, "opencode") == 200_000


def test_resolve_budget_independent_harnesses(guard):
    # claude·opencode 오버라이드 키 완전 독립 (동시 운용·generic 없이도 각자 해소).
    conf = {"ctx_window_tokens_claude": "500000", "ctx_window_tokens_opencode": "200000"}
    assert guard.resolve_budget(conf, "claude") == 500_000
    assert guard.resolve_budget(conf, "opencode") == 200_000
    # 다른 하네스 키만 있고 대상 하네스 키 없으면 → generic(없으면 200K).
    assert guard.resolve_budget({"ctx_window_tokens_opencode": "999"}, "claude") == 200_000


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "", "  "])
def test_resolve_budget_sanity_falls_through(guard, bad):
    # 오버라이드가 ≤0/비정수 → generic 으로, generic 도 비정상이면 200K (각 층 >0 sanity).
    assert guard.resolve_budget(
        {"ctx_window_tokens_claude": bad, "ctx_window_tokens": "300000"}, "claude") == 300_000
    assert guard.resolve_budget(
        {"ctx_window_tokens_claude": bad, "ctx_window_tokens": bad}, "claude") == 200_000


# ── 2. statusLine: context_window → used % (분모=예산·ADR-0041) + 넛지 ────────

def test_statusline_used_pct_current_usage(guard):
    # used_tokens = current_usage(input+cache 합) / 예산.
    sl = {"context_window": {"current_usage": {
        "input_tokens": 100_000,
        "cache_creation_input_tokens": 40_000,
        "cache_read_input_tokens": 40_000,
    }}}
    assert guard.context_used_pct_from_statusline(sl, 200_000) == 90


def test_statusline_current_usage_null_with_stale_total_input_is_zero(guard):
    # codex T-0234 must-fix: current_usage null(post-/compact) + 누적성 total_input 잔존 →
    # 폴백하면 과대 표시→넛지 오판정. 반드시 0% graceful.
    sl = {"context_window": {"current_usage": None, "total_input_tokens": 150_000}}
    assert guard.context_used_pct_from_statusline(sl, 200_000) == 0


def test_statusline_current_usage_empty_dict_is_zero(guard):
    # current_usage 가 빈 dict 여도 0% (토큰 신호 없음 = graceful).
    sl = {"context_window": {"current_usage": {}, "total_input_tokens": 99_000}}
    assert guard.context_used_pct_from_statusline(sl, 200_000) == 0

def test_statusline_budget_is_denominator(guard):
    # 물리 window% 폐기 — 같은 토큰이라도 예산 분모가 다르면 다른 % (표시=정지 일관 근거).
    sl = {"context_window": {"current_usage": {"input_tokens": 100_000}}}
    assert guard.context_used_pct_from_statusline(sl, 200_000) == 50
    assert guard.context_used_pct_from_statusline(sl, 500_000) == 20
    assert guard.context_used_pct_from_statusline(sl, 1_000_000) == 10


def test_statusline_ignores_native_physical_pct(guard):
    # ADR-0041: native used_percentage(물리%)는 안 읽는다 — current_usage 예산 분모만.
    sl = {"context_window": {"used_percentage": 73,
                             "current_usage": {"input_tokens": 100_000}}}
    assert guard.context_used_pct_from_statusline(sl, 200_000) == 50  # native 73 무시.
    # current_usage/total_input 없이 native 만 → 0 (native 안 읽음).
    assert guard.context_used_pct_from_statusline(
        {"context_window": {"used_percentage": 73}}, 200_000) == 0


def test_statusline_current_usage_null_zero(guard):
    # current_usage null/부재(세션초·/compact 직후) + total_input 없음 → 0% graceful.
    assert guard.context_used_pct_from_statusline(
        {"context_window": {"current_usage": None}}, 200_000) == 0
    assert guard.context_used_pct_from_statusline({"context_window": {}}, 200_000) == 0


def test_statusline_no_signal_zero(guard):
    assert guard.context_used_pct_from_statusline({}, 200_000) == 0
    assert guard.context_used_pct_from_statusline({"context_window": "bad"}, 200_000) == 0
    # budget<=0(비정상)도 0.
    assert guard.context_used_pct_from_statusline(
        {"context_window": {"current_usage": {"input_tokens": 100_000}}}, 0) == 0


def test_statusline_render_colors(guard, statusline):
    # conf={} → 예산 200K 기본·기본 임계(30/20·T-0207). current_usage 토큰으로 밴드 구성.
    def _sl(tokens):
        return {"context_window": {"current_usage": {"input_tokens": tokens}}}
    # ok (used 50, 잔여 50 > 30): 회색·정지문구 없음.
    ok = statusline.build_statusline(_sl(100_000), {})
    assert "\033[90m" in ok and "ctx 50%" in ok and "정지" not in ok
    # nudge (used 75, 잔여 25 <= 30·> 20): 노랑·"곧 정지".
    nudge = statusline.build_statusline(_sl(150_000), {})
    assert "\033[33m" in nudge and "곧 정지" in nudge
    # nudge2 (used 78, 잔여 22 <= 23[=min(20+3,30)]·> 20): 빨강·"정지 임박" (2단 strong·T-0328).
    nudge2 = statusline.build_statusline(_sl(156_000), {})
    assert "\033[31m" in nudge2 and "정지 임박" in nudge2
    # stop (used 92, 잔여 8 <= 20): 빨강·"정지 임계".
    stop = statusline.build_statusline(_sl(184_000), {})
    assert "\033[31m" in stop and "정지 임계" in stop


def test_statusline_render_colors_respects_budget_override(guard, statusline):
    # 하네스 오버라이드로 예산이 커지면 같은 토큰이 낮은 %로 표시(표시=정지 일관·per-harness).
    sl = {"context_window": {"current_usage": {"input_tokens": 184_000}}}
    # 기본 200K: 92% used → stop(빨강).
    stop = statusline.build_statusline(sl, {})
    assert "\033[31m" in stop and "정지 임계" in stop
    # claude 오버라이드 1M: 18% used → ok(회색).
    ok = statusline.build_statusline(sl, {"ctx_window_tokens_claude": "1000000"})
    assert "\033[90m" in ok and "ctx 18%" in ok and "정지" not in ok


def test_classify_boundaries(guard):
    # nudge2_threshold = min(stop_pct + CTX_NUDGE2_MARGIN_PCT, nudge_pct) = min(10+3, 20) = 13.
    th = {"nudge_pct": 20, "stop_pct": 10}
    assert guard.classify(50, th) == "ok"
    assert guard.classify(80, th) == "nudge"    # 잔여 20 == nudge_pct → nudge (1단).
    assert guard.classify(79, th) == "ok"       # 잔여 21 > nudge_pct → ok.
    assert guard.classify(86, th) == "nudge"    # 잔여 14 (13 < 14 <= 20) → nudge (1단).
    assert guard.classify(87, th) == "nudge2"   # 잔여 13 == nudge2_threshold → nudge2 (2단).
    assert guard.classify(89, th) == "nudge2"   # 잔여 11 (10 < 11 <= 13) → nudge2 (2단).
    assert guard.classify(90, th) == "stop"     # 잔여 10 == stop_pct → stop.
    assert guard.classify(91, th) == "stop"     # 잔여 9 < stop_pct → stop.


def test_nudge2_threshold_derivation(guard):
    # 2단 임계 = stop_pct + margin(3), nudge_pct 로 캡 (config 노브 신설 없이 파생).
    assert guard.CTX_NUDGE2_MARGIN_PCT == 3
    assert guard.nudge2_threshold({"nudge_pct": 30, "stop_pct": 20}) == 23   # min(23, 30).
    assert guard.nudge2_threshold({"nudge_pct": 20, "stop_pct": 10}) == 13   # min(13, 20).
    # nudge 밴드가 좁으면(stop+3 > nudge) nudge_pct 로 캡 — nudge2 가 ok 영역으로 안 샌다.
    assert guard.nudge2_threshold({"nudge_pct": 21, "stop_pct": 20}) == 21   # min(23, 21).
    assert guard.nudge2_threshold({"nudge_pct": 22, "stop_pct": 20}) == 22   # min(23, 22).


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


def test_hook_evaluate_ok_passes(guard, stop_hook, tmp_path):
    # 잔여 넉넉 (used 50) → 출력 없이 통과.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 100_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-ok"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    # ok → STOP marker 안 박힌다.
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-ok.done").exists()


def test_hook_evaluate_stop_denies_and_triggers(guard, stop_hook, tmp_path):
    # used 92% (잔여 8 <= 20) + 새 작업 도구 → deny + STOP marker 무조건 박제 (ADR-0038 D4).
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 184_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-stop",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},  # 새 작업 도구.
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "새 세션" in hso["permissionDecisionReason"]
    assert "ctx-stop" in hso["permissionDecisionReason"]
    # STOP marker 가 무조건(handoff_rc 게이트 없음) 박제됐다.
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-stop.done").exists()


def test_hook_idempotent_single_trigger(guard, stop_hook, tmp_path):
    """같은 세션에서 두 번 정지 임계여도 marker 는 1개·2회차도 에러 없이 deny (멱등=파일 존재)."""
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-idem",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cat foo.py"},  # 새 작업 도구.
    }
    rc1, out1 = stop_hook.evaluate(stdin, tmp_path, {})
    rc2, out2 = stop_hook.evaluate(stdin, tmp_path, {})
    # 두 번 다 deny.
    assert out1["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out2["hookSpecificOutput"]["permissionDecision"] == "deny"
    # marker 파일이 (한 번) 생성돼 두 번째 호출에도 그대로 남아있다.
    marker = tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-idem.done"
    assert marker.exists()


def test_hook_no_transcript_passes(stop_hook, tmp_path):
    # transcript_path 없음 → used 0 → 통과.
    rc, output = stop_hook.evaluate({"session_id": "x"}, tmp_path, {})
    assert rc == 0 and output is None


def test_hook_marker_write_failure_self_heals_next_call(stop_hook, tmp_path, monkeypatch):
    # marker 파일 write 자체가 실패하면 marker 는 부재·_already_triggered 는 False 로 남아
    # 다음 stop-도구 호출이 재시도(self-heal) — in-memory 플래그를 두지 않는 설계(ADR-0038 D4).
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-heal",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},  # 새 작업 도구.
    }
    # marker write 를 no-op 로 만들어 파일이 안 생기게 한다 (디렉토리 unwritable 등가).
    monkeypatch.setattr(stop_hook, "_mark_triggered", lambda root, sid: None)
    rc1, out1 = stop_hook.evaluate(stdin, tmp_path, {})
    rc2, out2 = stop_hook.evaluate(stdin, tmp_path, {})
    marker = tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-heal.done"
    # marker 부재 → 매 호출이 미박제로 판정(_already_triggered False)해 재시도한다.
    assert not marker.exists()
    assert not stop_hook._already_triggered(tmp_path, "sess-heal")
    # 정지(deny) 자체는 두 번 다 유효 (marker 실패해도 hard-stop 은 살아있다).
    assert out1["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out2["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_user_prompt_submit_blocks(guard, stop_hook, tmp_path):
    # UserPromptSubmit 이벤트 → prompt 자체 block (새 작업 진입 차단·ADR-0038 D2 정지 경계).
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-ups",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    # UserPromptSubmit 은 top-level block 스키마 (PreToolUse 의 hookSpecificOutput 아님).
    assert output["decision"] == "block"
    assert "새 세션" in output["reason"]
    # marker 는 event 무관하게 stop 도달 즉시 박제된다.
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-ups.done").exists()


def test_hook_pretooluse_default_when_no_event(guard, stop_hook, tmp_path):
    # hook_event_name 없으면 기본 PreToolUse 처리 — 새 작업 도구(핸드오프 아님)면 deny.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-def",
        "tool_name": "Bash",
        "tool_input": {"command": "python3 other.py"},  # 새 작업 도구.
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── 3c. 핸드오프 도구 allow-list (ADR-0038 D2 — 진행 중 /pm-handoff 통과) ─────
# stop 밴드에서 PreToolUse 도구를 통과(None)/deny 로 가르는 안전-핵심 로직.
# transcript 는 used 95%(잔여 5 <= stop 20·기본 T-0207)로 stop 밴드에 넣는다.

def _stop_transcript(tmp_path: Path) -> Path:
    # input_tokens 190_000 / window 200_000 = 95% used → 잔여 5 <= stop_pct 20 → stop.
    return _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])


def _pretooluse_stdin(tmp_path: Path, session_id: str, tool_name: str, tool_input: dict) -> dict:
    return {
        "transcript_path": str(_stop_transcript(tmp_path)),
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


@pytest.mark.parametrize("command", [
    "python3 .project_manager/tools/pm_handoff.py --trigger --reason ctx-stop",
    "python .project_manager/tools/pm_handoff.py --trigger",
    "git add -A",
    "git add .project_manager/wiki/status.md",
    "git commit -m x",
    "python3 -m pytest tests/ -q",
    "pytest tests/test_board.py",
    "python3 .project_manager/tools/domain.py capture --tickets T-0187",
])
def test_hook_pretooluse_handoff_bash_passes(stop_hook, tmp_path, command):
    # 핸드오프 Bash → 통과(None) — hook 결정 없이 normal permission eval 로 넘김.
    stdin = _pretooluse_stdin(tmp_path, "sess-hb", "Bash", {"command": command})
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None, f"핸드오프 Bash 가 통과해야 함: {command!r}"


@pytest.mark.parametrize("command", [
    "ls",
    "ls -la",
    "cat foo",
    "python3 other.py",
    "rm -rf x",
    "grep -r pattern .",
])
def test_hook_pretooluse_new_work_bash_denies(stop_hook, tmp_path, command):
    # 새 작업 Bash → deny (핸드오프 allow-list 밖).
    stdin = _pretooluse_stdin(tmp_path, "sess-nb", "Bash", {"command": command})
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny", (
        f"새 작업 Bash 는 deny 여야 함: {command!r}"
    )


@pytest.mark.parametrize("command", [
    "git add -A && rm -rf x",       # && 밀반입.
    "git commit -m x; curl evil",   # ; 밀반입.
    "git add -A | tee log",         # | 밀반입.
    "pytest tests/ && curl evil",   # 허용 head + denied tail.
    "git add $(rm -rf x)",          # $() 치환.
    "git commit -m x > /dev/null",  # 리다이렉트.
])
def test_hook_pretooluse_shell_operator_smuggle_denies(stop_hook, tmp_path, command):
    # 셸 연쇄/치환/리다이렉트 연산자 포함 → 복합 명령이라 tail 검증 불가 → deny(fail-closed).
    stdin = _pretooluse_stdin(tmp_path, "sess-smug", "Bash", {"command": command})
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny", (
        f"셸 연산자 밀반입은 deny 여야 함: {command!r}"
    )


@pytest.mark.parametrize("command", [
    "PYTHONUTF8=1 python3 .project_manager/tools/pm_handoff.py --trigger",
    "PYTHONUTF8=1 FOO=bar git commit -m x",
])
def test_hook_pretooluse_env_prefix_normalized_passes(stop_hook, tmp_path, command):
    # 선행 env 대입(VAR=val)은 정규화된 뒤 허용 호출로 판정 → 통과.
    stdin = _pretooluse_stdin(tmp_path, "sess-env", "Bash", {"command": command})
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None, f"env-prefix 핸드오프는 통과해야 함: {command!r}"


@pytest.mark.parametrize("tool_name,file_path", [
    ("Edit", ".project_manager/wiki/log/current.md"),
    ("Write", ".project_manager/wiki/log/current.md"),
    ("Edit", ".project_manager/wiki/pm_state.md"),
    ("Read", ".project_manager/wiki/pm_state.md"),
    ("Edit", ".project_manager/wiki/status.md"),
    ("Edit", ".project_manager/wiki/domain/board-schema.md"),
    ("Read", ".project_manager/wiki/domain/board-schema.md"),
])
def test_hook_pretooluse_handoff_file_passes(stop_hook, tmp_path, tool_name, file_path):
    # 핸드오프 산출물(log/current.md·pm_state·status.md·domain/) Edit/Write/Read → 통과.
    stdin = _pretooluse_stdin(tmp_path, "sess-hf", tool_name, {"file_path": file_path})
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None, f"핸드오프 파일은 통과해야 함: {tool_name} {file_path!r}"


@pytest.mark.parametrize("tool_name,file_path", [
    ("Edit", ".project_manager/tools/board.py"),
    ("Read", ".project_manager/tools/board.py"),
    ("Write", "src/new_feature.py"),
    ("Edit", "README.md"),
])
def test_hook_pretooluse_new_work_file_denies(stop_hook, tmp_path, tool_name, file_path):
    # 소스/무관 파일 Edit/Write/Read → deny (핸드오프 산출물 밖·새 작업 방어).
    stdin = _pretooluse_stdin(tmp_path, "sess-nf", tool_name, {"file_path": file_path})
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny", (
        f"소스 파일 편집은 deny 여야 함: {tool_name} {file_path!r}"
    )


def test_hook_pretooluse_handoff_tool_marks_stop_unconditionally(stop_hook, tmp_path):
    # 핸드오프 도구가 통과(None)돼도 STOP marker 는 무조건 박제된다 (회전 신호 누락 방지).
    stdin = _pretooluse_stdin(
        tmp_path, "sess-hmark", "Bash",
        {"command": "python3 .project_manager/tools/pm_handoff.py --trigger"},
    )
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert output is None  # 통과.
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-hmark.done").exists()


def test_settings_wires_user_prompt_submit():
    # settings.json 에 UserPromptSubmit 훅(ctx_stop_hook) 배선 — 새 작업 진입 차단.
    # T-0202: 이제 래퍼(ctx_stop_hook.sh) 경유 — 래퍼가 인터프리터 self-resolve 후 ctx_stop_hook.py 를
    #   exec(stdin/args/rc 투명 전달). 래퍼→.py 링크는 test_new_wrappers_self_contained 가 커버.
    data = json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8"))
    ups = data["hooks"]["UserPromptSubmit"]
    assert isinstance(ups, list) and ups
    cmds = [h.get("command", "") for m in ups for h in m.get("hooks", [])]
    assert any("ctx_stop_hook.sh" in c for c in cmds), "UserPromptSubmit 에 ctx_stop_hook 래퍼 누락"


def test_hook_session_id_sanitized(stop_hook):
    # 경로 traversal 문자는 제거된다 (marker 파일명 안전).
    assert "/" not in stop_hook._session_id({"session_id": "../../etc/passwd"})
    assert stop_hook._session_id({}) == "unknown"


# ── 4. main() stdin 경로 (가짜 stdin → stdout JSON) ────────────────────────

def test_statusline_main_emits_line(statusline, monkeypatch, capsys):
    import io
    # load_local_config 를 빈 conf 로 고정 → resolve_budget 200K 결정론(실 repo local.conf 무관).
    monkeypatch.setattr(statusline.ctx_guard, "load_local_config", lambda root: {})
    stdin = {"context_window": {"current_usage": {"input_tokens": 190_000}}}  # 95% of 200K.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stdin)))
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

# T-0202: statusLine·PreToolUse 배선은 이제 래퍼(.sh) 경유 — 래퍼가 인터프리터를 self-resolve
#   (python3→python) 후 대응 .py 를 exec(stdin/args/rc 투명). settings.json 이 .py 를 직접 부르지
#   않아 {{PY}} 치환토큰·절대경로가 사라진다(portable-by-construction). 래퍼→.py 링크는
#   test_claude_adapter_parity.test_new_wrappers_self_contained 가 커버.

@pytest.mark.parametrize("name", ["settings.json"])
def test_settings_wires_statusline(name):
    data = json.loads((CLAUDE / name).read_text(encoding="utf-8"))
    sl = data.get("statusLine")
    assert isinstance(sl, dict), f"{name} 에 statusLine 누락"
    assert "ctx_statusline.sh" in sl["command"]


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
    assert any("ctx_stop_hook.sh" in c for c in cmds), f"{name} PreToolUse 에 ctx_stop_hook 래퍼 누락"


@pytest.mark.parametrize("name", ["settings.json"])
def test_settings_preserves_posttooluse(name):
    # 기존 PostToolUse(run_tests_hook) 가 보존됐다 (회귀 — 무관한 hook 안 깨짐).
    data = json.loads((CLAUDE / name).read_text(encoding="utf-8"))
    post = data["hooks"]["PostToolUse"]
    cmds = [h.get("command", "") for m in post for h in m.get("hooks", [])]
    assert any("run_tests_hook.sh" in c for c in cmds)


# ── graceful nudge (ADR-0037) — nudge 임계서 모델-facing 비차단 안내 주입 ──────────
# 1단(nudge)이 비어있던 자리를 채운다: UserPromptSubmit additionalContext 로 모델이 스스로
# /pm-handoff 하게 유도. hard-stop(2단)은 독립 fail-safe 로 무변경.


def test_build_nudge_guidance(guard):
    # 안내문 = 조건부 권고(현 단계 마무리 후·/pm-handoff·자동정지 임계). 정지/지시 아님.
    g = guard.build_nudge_guidance(82, {"nudge_pct": 20, "stop_pct": 10})
    assert "ctx-nudge" in g
    assert "잔여 18%" in g          # remaining_pct(82) = 18.
    assert "/pm-handoff" in g
    assert "10%" in g               # stop_pct 안내.
    assert "ADR-0037" in g


def test_hook_nudge_userpromptsubmit_injects(guard, stop_hook, tmp_path):
    # nudge 레벨(used 75·잔여 25 — stop 20 < 25 <= nudge 30) + UserPromptSubmit → additionalContext 비차단 주입.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-nudge",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in hso
    assert "/pm-handoff" in hso["additionalContext"]
    assert "ctx-nudge" in hso["additionalContext"]
    # 비차단: deny/block 아님 (정지 스키마 부재).
    assert "permissionDecision" not in hso
    assert output.get("decision") != "block"
    # nudge marker(.nudge) 생성·stop marker(.done) 미생성.
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-nudge.nudge").exists()
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-nudge.done").exists()


def test_hook_nudge_pretooluse_passes_no_injection(stop_hook, tmp_path):
    # nudge 레벨(잔여 25) + PreToolUse(주입 채널 없음) → 통과(도구 진행)·주입/marker 없음.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-nudge-ptu"}  # event 없음=PreToolUse 기본.
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-nudge-ptu.nudge").exists()


def test_hook_nudge_idempotent_single_injection(stop_hook, tmp_path):
    # 같은 세션 두 번 nudge(UserPromptSubmit)여도 주입은 1회 (.nudge marker 가드).
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-nudge-idem",
        "hook_event_name": "UserPromptSubmit",
    }
    rc1, out1 = stop_hook.evaluate(stdin, tmp_path, {})
    rc2, out2 = stop_hook.evaluate(stdin, tmp_path, {})
    assert "additionalContext" in out1["hookSpecificOutput"]   # 1회차 주입.
    assert out2 is None                                        # 2회차 통과(이미 주입).


def test_hook_nudge_independent_from_stop(stop_hook, tmp_path):
    # 2단 fail-safe 독립: nudge(.nudge) 발동해도 stop 은 별개로 deny+박제 (서로 marker 분리).
    sid = "sess-2tier"
    nudge_tx = _write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])
    nudge_stdin = {"transcript_path": str(nudge_tx), "session_id": sid,
                   "hook_event_name": "UserPromptSubmit"}
    stop_hook.evaluate(nudge_stdin, tmp_path, {})  # nudge 주입.
    # 같은 세션이 stop 레벨 transcript 로 진입(transcript.jsonl 은 같은 경로라 덮어씀) → nudge
    # marker(.nudge)는 stop marker(.done)와 *별개 파일*이라 stop 을 막지 않는다(2단 fail-safe 독립).
    stop_tx = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stop_stdin = {
        "transcript_path": str(stop_tx),
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},  # 새 작업 도구.
    }
    rc, output = stop_hook.evaluate(stop_stdin, tmp_path, {})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"  # stop 정상 작동.
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / f"{sid}.nudge").exists()
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / f"{sid}.done").exists()


# ── 6. hard-stop 핸드오프-intent 예외 (T-0205·ADR-0038 D2 amend) ────────────────
# 락아웃 해소: stop 밴드 UserPromptSubmit 에서 `/pm-handoff` 로 *시작*하는 prompt 는 통과(None),
# 그 외 prompt 는 block 유지(좁은 매칭·과통과=hard-stop 무력화 방지). 통과 케이스도 STOP marker 는
# 이벤트 무관하게 박힌다. 회귀: nudge/ok/PreToolUse 경로는 이 예외에 무영향.

_OMIT = object()  # "prompt 키 자체 부재" 를 명시(None 값과 구분).


def _ups_stop_stdin(tmp_path: Path, session_id: str, prompt=_OMIT) -> dict:
    """stop 밴드 UserPromptSubmit stdin (prompt=_OMIT → 키 생략·그 외는 그대로 세팅)."""
    stdin = {
        "transcript_path": str(_stop_transcript(tmp_path)),
        "session_id": session_id,
        "hook_event_name": "UserPromptSubmit",
    }
    if prompt is not _OMIT:
        stdin["prompt"] = prompt
    return stdin


def test_hook_ups_handoff_prompt_passes(stop_hook, tmp_path):
    # stop 밴드 + `/pm-handoff` prompt → 통과(None·block JSON 없음)로 핸드오프 진입 허용.
    stdin = _ups_stop_stdin(tmp_path, "sess-hp", prompt="/pm-handoff")
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    # 통과해도 STOP marker(.done)는 stop 도달 즉시 박힌다 (이벤트 무관·회전 신호 누락 방지).
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-hp.done").exists()


@pytest.mark.parametrize("prompt", [
    "/pm-handoff --dry-run",       # 인자 허용.
    "/pm-handoff --reason ctx-stop",
    "  /pm-handoff  ",             # 선행/후행 공백.
    "\t/pm-handoff\n",             # 탭·개행도 strip.
])
def test_hook_ups_handoff_prompt_variants_pass(stop_hook, tmp_path, prompt):
    # 인자/공백 변형도 통과 — strip 후 정확 커맨드(+공백 인자). 변형 통과에도 STOP marker
    # 계약(이벤트 무관 박제)은 유지된다(codex suggestion — 대표 케이스 아닌 전 변형 단언).
    stdin = _ups_stop_stdin(tmp_path, "sess-hpv", prompt=prompt)
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None, f"핸드오프 트리거 변형은 통과해야 함: {prompt!r}"
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-hpv.done").exists(), \
        f"통과 변형에서 STOP marker 누락: {prompt!r} (relay 회전 신호 계약)"


@pytest.mark.parametrize("prompt", [
    "/pm-handofffoo",              # 접미 변형 — 비정확 커맨드 (토큰 경계·codex must-fix).
    "/pm-handoffs",
    "/pm-handoff;echo x",          # 공백 없는 접미(세미콜론) — 커맨드 토큰이 아님.
])
def test_hook_ups_inexact_command_suffix_blocks(stop_hook, tmp_path, prompt):
    # 토큰 경계: 정확 커맨드 단독 또는 공백 뒤 인자만 통과 — bare prefix 매칭이 허용하던
    # `/pm-handoffX` 류 비정확 커맨드는 block 유지 (codex·내부 reviewer 수렴 지점).
    stdin = _ups_stop_stdin(tmp_path, "sess-hpx", prompt=prompt)
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is not None and output.get("decision") == "block", \
        f"비정확 커맨드 접미 변형이 통과됨(토큰 경계 붕괴): {prompt!r}"


@pytest.mark.parametrize("prompt", [
    "핸드오프 해줘",              # 자연어(키워드) — 오인식 위험이라 계속 block.
    "인계 부탁해",                # 자연어 키워드.
    "pm-handoff",                 # 슬래시 없음.
    "버그 고치고 /pm-handoff",    # `/pm-handoff` 가 시작 아님(중간).
    "please run /pm-handoff",     # 영문 산문 — 시작 아님.
])
def test_hook_ups_non_handoff_prompt_still_blocks(stop_hook, tmp_path, prompt):
    # 좁은 매칭 경계(핵심): `/pm-handoff` 로 *시작*하지 않으면 새 작업 진입 block 유지.
    stdin = _ups_stop_stdin(tmp_path, "sess-nblk", prompt=prompt)
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert output["decision"] == "block", f"비-핸드오프 prompt 는 block 유지: {prompt!r}"
    # block 이어도 STOP marker 는 박힌다 (이벤트 무관).
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-nblk.done").exists()


def test_hook_ups_block_reason_guides_handoff_command(stop_hook, tmp_path):
    # block reason 이 통과 가능한 정확 커맨드(`/pm-handoff`)를 안내 → 락아웃 없음.
    stdin = _ups_stop_stdin(tmp_path, "sess-guide", prompt="다른 일 해줘")
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert output["decision"] == "block"
    assert "/pm-handoff" in output["reason"], "락아웃 해소 안내(정확 커맨드) 누락"


@pytest.mark.parametrize("prompt", [_OMIT, None, 123, {"cmd": "/pm-handoff"}, ["/pm-handoff"]])
def test_hook_ups_missing_or_nonstr_prompt_blocks(stop_hook, tmp_path, prompt):
    # prompt 필드 부재/비-str → fail-closed block (기존 동작 보존·과통과 방지).
    stdin = _ups_stop_stdin(tmp_path, "sess-badp", prompt=prompt)
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert output["decision"] == "block", f"prompt 부재/비-str 은 fail-closed block: {prompt!r}"


def test_hook_ups_handoff_exception_scoped_to_stop_band(stop_hook, tmp_path):
    # 회귀: 핸드오프-intent 예외는 stop 밴드 한정 — nudge 밴드 UserPromptSubmit 은 prompt 내용과
    # 무관하게 여전히 비차단 nudge 주입(예외가 nudge 경로를 오염시키지 않음).
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])  # 잔여 25 = nudge.
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-nudge-hp",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "/pm-handoff",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert "additionalContext" in hso              # nudge 주입 — 통과(None)도 block 도 아님.
    assert output.get("decision") != "block"


def test_hook_ups_ok_band_passes_regardless_of_prompt(stop_hook, tmp_path):
    # 회귀: ok 밴드면 prompt 내용 무관 통과·marker 미박제 (stop 예외 로직 미진입).
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 100_000})])  # used 50 = ok.
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-ok-ups",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "아무 일이나 해줘",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-ok-ups.done").exists()


@pytest.mark.parametrize("stdin,expected", [
    ({"prompt": "/pm-handoff"}, True),
    ({"prompt": "/pm-handoff --dry-run"}, True),
    ({"prompt": "  /pm-handoff  "}, True),
    ({"prompt": "핸드오프 해줘"}, False),
    ({"prompt": "pm-handoff"}, False),
    ({"prompt": "버그 고치고 /pm-handoff"}, False),
    # 토큰 경계 (codex·reviewer 수렴) — 정확 커맨드 단독/공백+인자만·접미 변형은 비정확 커맨드.
    ({"prompt": "/pm-handofffoo"}, False),
    ({"prompt": "/pm-handoffs"}, False),
    ({"prompt": "/pm-handoff;echo x"}, False),
    ({"prompt": 123}, False),
    ({}, False),
])
def test_is_handoff_prompt_unit(stop_hook, stdin, expected):
    # 좁은 매칭 계약 단위 검증 — 정확 커맨드 `/pm-handoff` 단독 또는 공백 뒤 인자만 True.
    assert stop_hook._is_handoff_prompt(stdin) is expected


# ── 7. graceful nudge 2단(strong·stop 직전·ADR-0037·T-0328) — 능동 유도 재안내 ──────────────
# 1단(soft)이 비었던 자리에 2단(strong)을 추가: 모델이 1단을 무시했거나 1단 창을 건너뛴 세션에
# hard-stop 직전 "지금 즉시 /pm-handoff·새 작업 금지" 강하게 재안내. 별도 `.nudge2` marker(세션당
# 1회·1단과 독립). 1단 문구·hard-stop 경로는 무변경(이 스코프는 2단 추가만). 밴드(기본 30/20):
# nudge2_threshold=23 → 잔여 (20,23] 이 nudge2, (23,30] 이 nudge(1단).

def _nudge2_transcript(tmp_path: Path) -> Path:
    # 156_000 / 200_000 = 78% used → 잔여 22 (20 < 22 <= 23) → nudge2 밴드.
    return _write_transcript(tmp_path, [("assistant", {"input_tokens": 156_000})])


def test_build_nudge2_guidance(guard):
    # 2단 안내문 = 능동 유도(즉시 /pm-handoff·새 tool 작업 금지·hard-stop 직전). 여전히 안내(비차단).
    g = guard.build_nudge2_guidance(82, {"nudge_pct": 20, "stop_pct": 10})
    assert "ctx-nudge/최종" in g
    assert "잔여 18%" in g          # remaining_pct(82) = 18.
    assert "hard-stop" in g
    assert "/pm-handoff" in g
    assert "10%" in g               # stop_pct 안내.
    assert "강제 정지" in g
    assert "ADR-0037" in g
    # 1단 문구와 구별된다 (별개 강도 표지).
    assert g != guard.build_nudge_guidance(82, {"nudge_pct": 20, "stop_pct": 10})


def test_hook_nudge2_userpromptsubmit_injects(guard, stop_hook, tmp_path):
    # nudge2 레벨(잔여 22 — stop 20 < 22 <= nudge2_threshold 23) + UserPromptSubmit → strong 주입.
    stdin = {
        "transcript_path": str(_nudge2_transcript(tmp_path)),
        "session_id": "sess-nudge2",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in hso
    assert "ctx-nudge/최종" in hso["additionalContext"]   # 2단 강도 표지.
    assert "/pm-handoff" in hso["additionalContext"]
    # 비차단: deny/block 아님.
    assert "permissionDecision" not in hso
    assert output.get("decision") != "block"
    # nudge2 marker(.nudge2) 생성·stop(.done) 미생성.
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (ctx / "sess-nudge2.nudge2").exists()
    assert not (ctx / "sess-nudge2.done").exists()


def test_hook_nudge2_pretooluse_passes_no_injection(stop_hook, tmp_path):
    # nudge2 레벨 + PreToolUse(주입 채널 없음) → 통과(도구 진행)·주입/marker 없음 (1단 동형).
    stdin = {"transcript_path": str(_nudge2_transcript(tmp_path)), "session_id": "sess-n2-ptu"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-n2-ptu.nudge2").exists()


def test_hook_nudge2_idempotent_single_injection(stop_hook, tmp_path):
    # 같은 세션 두 번 nudge2(UserPromptSubmit)여도 주입은 1회 (.nudge2 marker 가드).
    stdin = {
        "transcript_path": str(_nudge2_transcript(tmp_path)),
        "session_id": "sess-n2-idem",
        "hook_event_name": "UserPromptSubmit",
    }
    rc1, out1 = stop_hook.evaluate(stdin, tmp_path, {})
    rc2, out2 = stop_hook.evaluate(stdin, tmp_path, {})
    assert "additionalContext" in out1["hookSpecificOutput"]   # 1회차 주입.
    assert out2 is None                                        # 2회차 통과(이미 주입).


def test_hook_nudge2_fires_independent_of_nudge1(stop_hook, tmp_path):
    # 2단은 1단 발화 여부와 독립: 세션이 nudge(1단) 창을 건너뛰고 곧장 nudge2 밴드로 진입해도
    # 2단은 발화한다 (.nudge2 생성·.nudge 은 미생성 — 1단은 안 거쳤으므로).
    stdin = {
        "transcript_path": str(_nudge2_transcript(tmp_path)),
        "session_id": "sess-n2-solo",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert "ctx-nudge/최종" in output["hookSpecificOutput"]["additionalContext"]
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (ctx / "sess-n2-solo.nudge2").exists()
    assert not (ctx / "sess-n2-solo.nudge").exists()


def test_hook_nudge1_then_nudge2_both_fire(stop_hook, tmp_path):
    # 세션이 nudge(1단·잔여 25)를 거쳐 nudge2(2단·잔여 22)로 진행 → 두 marker 다 생성·각 문구 구별.
    sid = "sess-both"
    # 1단: 잔여 25 (nudge_threshold 23 < 25 <= nudge 30).
    nudge_stdin = {
        "transcript_path": str(_write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])),
        "session_id": sid, "hook_event_name": "UserPromptSubmit",
    }
    rc1, out1 = stop_hook.evaluate(nudge_stdin, tmp_path, {})
    assert "ctx-nudge]" in out1["hookSpecificOutput"]["additionalContext"]  # 1단 표지.
    # 2단: 같은 세션이 nudge2 밴드 진입 (transcript 경로 덮어씀).
    nudge2_stdin = {
        "transcript_path": str(_nudge2_transcript(tmp_path)),
        "session_id": sid, "hook_event_name": "UserPromptSubmit",
    }
    rc2, out2 = stop_hook.evaluate(nudge2_stdin, tmp_path, {})
    assert "ctx-nudge/최종" in out2["hookSpecificOutput"]["additionalContext"]  # 2단 표지.
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (ctx / f"{sid}.nudge").exists()
    assert (ctx / f"{sid}.nudge2").exists()


def test_hook_nudge2_independent_from_stop(stop_hook, tmp_path):
    # nudge2(.nudge2) 발동해도 stop 은 별개로 deny+박제 (marker 분리·2단 fail-safe 독립).
    sid = "sess-n2-stop"
    nudge2_stdin = {
        "transcript_path": str(_nudge2_transcript(tmp_path)),
        "session_id": sid, "hook_event_name": "UserPromptSubmit",
    }
    stop_hook.evaluate(nudge2_stdin, tmp_path, {})  # nudge2 주입.
    stop_stdin = {
        "transcript_path": str(_write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])),
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},  # 새 작업 도구.
    }
    rc, output = stop_hook.evaluate(stop_stdin, tmp_path, {})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"  # stop 정상 작동.
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (ctx / f"{sid}.nudge2").exists()
    assert (ctx / f"{sid}.done").exists()


def test_nudge2_margin_mirrors_opencode(guard):
    # 양 하네스 파리티: 2단 임계 마진(+3)이 claude·opencode 어댑터에서 동일 (한 곳만 바꾸면 어긋남).
    import re
    opencode_core = (
        REPO / "templates" / "opencode" / ".opencode" / "lib" / "ctx-guard-core.cjs"
    ).read_text(encoding="utf-8")
    m = re.search(r"const\s+NUDGE2_MARGIN_PCT\s*=\s*(\d+)", opencode_core)
    assert m, "opencode ctx-guard-core.cjs 에 NUDGE2_MARGIN_PCT 상수 없음"
    assert int(m.group(1)) == guard.CTX_NUDGE2_MARGIN_PCT, (
        f"2단 마진 미러 불일치: claude={guard.CTX_NUDGE2_MARGIN_PCT} opencode={m.group(1)}"
    )
