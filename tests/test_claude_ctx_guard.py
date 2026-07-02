"""claude 어댑터 ctx 정지-핸드오프 단위 테스트 (T-0015).

어댑터 스크립트(templates/claude_code/.claude/ctx_guard·ctx_statusline·ctx_stop_hook)를
importlib 로 직접 로드해 검증한다. stdlib only — 라이브 claude·외부 호출 없이
가짜 transcript JSONL·가짜 statusline stdin·격리 tmp 만 본다.

검증 축:
  1. 임계 config — local.conf nudge/stop 읽기 + sanity 폴백 (codex 인계).
  2. statusLine — context_window → used % (native/manual/total_input 폴백) + 색/문구 넛지.
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
    # conf={} → 엔진 기본 임계(30/20 · T-0207) 로 밴드 판정.
    th = {"nudge_pct": 30, "stop_pct": 20}
    # ok (used 50, 잔여 50 > 30): 회색·정지문구 없음.
    ok = statusline.build_statusline({"context_window": {"used_percentage": 50}}, {})
    assert "\033[90m" in ok and "ctx 50%" in ok and "정지" not in ok
    # nudge (used 75, 잔여 25 <= 30·> 20): 노랑·"곧 정지".
    nudge = statusline.build_statusline({"context_window": {"used_percentage": 75}}, {})
    assert "\033[33m" in nudge and "곧 정지" in nudge
    # stop (used 92, 잔여 8 <= 20): 빨강·"정지 임계".
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
