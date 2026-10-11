"""claude 어댑터 ctx compaction-native 넛지 단위 테스트 (T-0550).

어댑터 스크립트(templates/claude_code/.claude/ctx_guard·ctx_statusline·ctx_stop_hook)를
importlib 로 직접 로드해 검증한다. stdlib only — 라이브 claude·subprocess 호출 없이
가짜 transcript JSONL·가짜 statusline stdin·격리 tmp 만 본다.

검증 축:
  1. 분모·임계 — 압축 한도 기록(E·비율)과 세션 모델 확인(T-0943).
  2. statusLine — context_window → used % (current_usage 단일 소스·ADR-0041) + 색/문구 넛지.
  3. 훅 — 세 밴드 모두 PreToolUse/UserPromptSubmit additionalContext 비차단 주입 +
     채널 공유 사이클별 marker 멱등.
  4. re-arm — 주 대화 기록의 압축 경계 증가(압축 뒤 첫 주 스레드 훅) 또는 유효한 ok 실측 시
     `.nudge`/`.nudge2`/`.final` 삭제.
  5. settings 배선 — PreToolUse·UserPromptSubmit 만 ctx 훅에 연결(압축 이벤트 등록 없음).
  6. 압축 기록(T-0953) — 경계마다 한 번 checkpoint·snapshot, 실패 신호, `agent_id` 면제.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize(
    "command",
    [
        "python3 .project_manager/tools/board.py list",
        "python3 -m pytest tests/ -q",
        "cd /tmp && python3 build.py",
    ],
    ids=["engine-tool", "pytest", "persistent-cd"],
)
def test_git_anchor_prefilter_routes_new_command_classes_to_board(
    monkeypatch, tmp_path, command,
):
    """T-0697 선필터 세 분기는 모두 중앙 board 판정 seam까지 도달한다."""
    driver = _load("pm_orch_claude")
    calls = []

    class FakeBoard:
        @staticmethod
        def judge_git_anchor_command(_root, _session, cwd, shell_command, grants=False):
            calls.append((cwd, shell_command))
            return {"verdict": "ok", "cwd_identity": "non-repo", "reason": "fixture"}

    monkeypatch.setattr(driver, "_load_board", lambda _root: FakeBoard)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "cwd": str(tmp_path),
        "tool_input": {"command": command},
    }
    assert driver.git_anchor_hook_evaluate(payload, tmp_path) is None
    assert calls == [(str(tmp_path), command)]


# ── transcript JSONL fixture 헬퍼 ──────────────────────────────────────────

def _write_transcript(tmp_path: Path, messages, *, sidechain=None) -> Path:
    """messages = [(role, usage_dict|None), ...] → JSONL 파일 경로.

    sidechain=None(기본·back-compat) → isSidechain 필드 없음. True/False 면 각 엔트리 top-level 에
    isSidechain 을 심는다 (실 transcript 구조 미러 — 서브에이전트 파일은 전 엔트리 true·메인은 false).
    """
    path = tmp_path / "transcript.jsonl"
    lines = []
    for role, usage in messages:
        entry = {"type": role, "message": {"role": role}}
        if usage is not None:
            entry["message"]["usage"] = usage
        if sidechain is not None:
            entry["isSidechain"] = sidechain
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _append_transcript_entry(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")


def _compact_boundary(*, post_tokens=11_387) -> dict:
    """Claude Code 2.1.222 환경에서 관측한 저장 transcript compact 경계 최소 fixture."""
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {
            "trigger": "manual",
            "preTokens": 655_736,
            "postTokens": post_tokens,
        },
        "isSidechain": False,
    }


# ── 1. local.conf 읽기 ───────────────────────────────────────────────────────

def test_load_local_config_parses(guard, tmp_path):
    pm = tmp_path / ".project_manager"
    pm.mkdir(exist_ok=True)
    (pm / "local.conf").write_text(
        "# comment\nctx.checkpoint_at_pct=75\n\nctx.precompact_at_pct = 92\nprefix=PAY\n",
        encoding="utf-8"
    )
    conf = guard.load_local_config(tmp_path)
    assert conf["ctx.checkpoint_at_pct"] == "75"
    assert conf["ctx.precompact_at_pct"] == "92"
    assert conf["prefix"] == "PAY"


def test_load_local_config_missing(guard, tmp_path):
    assert guard.load_local_config(tmp_path / "elsewhere") == {}


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


def test_statusline_render_colors(guard, statusline,tmp_path):
    # conf={} → 예산 200K 기본·기본 임계(쓴 % 70/80). current_usage 토큰으로 밴드 구성.
    def _sl(tokens):
        return {"context_window": {"current_usage": {"input_tokens": tokens}}}
    # ok (used 50 < 70): 회색·checkpoint 문구 없음.
    ok = statusline.build_statusline(_sl(100_000), {},root=tmp_path)
    assert "\033[90m" in ok and "ctx 50%" in ok and "checkpoint" not in ok
    # nudge (70 <= used 75 < 77): 노랑·checkpoint 준비.
    nudge = statusline.build_statusline(_sl(150_000), {},root=tmp_path)
    assert "\033[33m" in nudge and "checkpoint 준비" in nudge
    # nudge2 (77[=max(80-3,70)] <= used 78 < 80): 빨강·checkpoint 권고.
    nudge2 = statusline.build_statusline(_sl(156_000), {},root=tmp_path)
    assert "\033[31m" in nudge2 and "checkpoint 권고" in nudge2
    # stop (used 92 >= 80): 빨강·checkpoint 최종 알림.
    stop = statusline.build_statusline(_sl(184_000), {},root=tmp_path)
    assert "\033[31m" in stop and "checkpoint 최종 알림" in stop


def test_statusline_render_colors_respects_budget_override(guard, statusline,tmp_path):
    # 하네스 오버라이드로 예산이 커지면 같은 토큰이 낮은 %로 표시(표시=밴드 일관·per-harness).
    sl = {"context_window": {"current_usage": {"input_tokens": 184_000}}}
    # 기본 200K: 92% used → stop(빨강).
    stop = statusline.build_statusline(sl, {},root=tmp_path)
    assert "\033[31m" in stop and "checkpoint 최종 알림" in stop
    # claude 오버라이드 1M(다시 적용한 기록): 18% used → ok(회색).
    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(tmp_path, {"harness.claude.ctx_window_tokens": "1000000"})
    ok = statusline.build_statusline(sl, {"harness.claude.ctx_window_tokens": "1000000"},root=tmp_path)
    assert "\033[90m" in ok and "ctx 18%" in ok and "checkpoint" not in ok


def test_classify_boundaries(guard):
    # nudge2_threshold = max(precompact_at − CTX_NUDGE2_MARGIN_PCT, checkpoint_at) = max(87, 80) = 87.
    th = {"checkpoint_at_pct": 80, "precompact_at_pct": 90}
    assert guard.classify(50, th) == "ok"
    assert guard.classify(80, th) == "nudge"    # 쓴 80 == checkpoint_at → nudge (1단).
    assert guard.classify(79, th) == "ok"       # 쓴 79 < checkpoint_at → ok.
    assert guard.classify(86, th) == "nudge"    # 80 <= 86 < 87 → nudge (1단).
    assert guard.classify(87, th) == "nudge2"   # 쓴 87 == nudge2_threshold → nudge2 (2단).
    assert guard.classify(89, th) == "nudge2"   # 87 <= 89 < 90 → nudge2 (2단).
    assert guard.classify(90, th) == "stop"     # 쓴 90 == precompact_at → stop.
    assert guard.classify(91, th) == "stop"     # 쓴 91 > precompact_at → stop.


def test_nudge2_threshold_derivation(guard):
    # 2단 임계 = precompact_at − margin(3), checkpoint_at 으로 하한 (config 노브 신설 없이 파생).
    assert guard.CTX_NUDGE2_MARGIN_PCT == 3
    assert guard.nudge2_threshold({"checkpoint_at_pct": 70, "precompact_at_pct": 80}) == 77
    assert guard.nudge2_threshold({"checkpoint_at_pct": 80, "precompact_at_pct": 90}) == 87
    # nudge 밴드가 좁으면(precompact_at − 3 < checkpoint_at) checkpoint_at 이 하한 — nudge2 가 ok 영역으로 안 샌다.
    assert guard.nudge2_threshold({"checkpoint_at_pct": 79, "precompact_at_pct": 80}) == 79
    assert guard.nudge2_threshold({"checkpoint_at_pct": 78, "precompact_at_pct": 80}) == 78


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
    assert guard.context_tokens_from_transcript(path) == (180_000, None, 0)


def test_transcript_tokens_stop_at_compact_boundary_without_new_usage(guard, tmp_path):
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    _append_transcript_entry(path, _compact_boundary())

    # compactMetadata.postTokens 는 compact 결과 크기지만 아직 새 assistant 요청의 usage 실측이
    # 아니다. 경계 뒤 usage 가 없으면 측정불가 0으로 두어 이전 사이클 usage를 재사용하지 않는다.
    assert guard.context_tokens_from_transcript(path) == (0, None, 1)


def test_transcript_tokens_only_use_usage_after_compact_boundary(guard, tmp_path):
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    _append_transcript_entry(path, _compact_boundary(post_tokens=12_345))
    _append_transcript_entry(path, {
        "type": "assistant",
        "message": {"role": "assistant", "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 48_000,
            "cache_read_input_tokens": 102_000,
        }},
    })

    assert guard.context_tokens_from_transcript(path) == (150_002, None, 1)


def test_transcript_used_pct(guard, tmp_path):
    path = _write_transcript(tmp_path, [
        ("assistant", {"input_tokens": 180_000}),
    ])
    assert guard.context_used_pct_from_transcript(path, 200_000) == 90


def test_transcript_missing_file_zero(guard, tmp_path):
    assert guard.context_tokens_from_transcript(tmp_path / "nope.jsonl") == (0, None, 0)
    assert guard.context_used_pct_from_transcript(tmp_path / "nope.jsonl", 200_000) == 0


def test_transcript_malformed_lines_skipped(guard, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"message": {"usage": {"input_tokens": 20_000}}}) + "\n"
        + "{broken\n",
        encoding="utf-8",
    )
    assert guard.context_tokens_from_transcript(path) == (20_000, None, 0)


def test_hook_evaluate_ok_passes(guard, stop_hook, tmp_path):
    # 쓴 % 낮음 (used 50) → 출력 없이 통과.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 100_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-ok"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop").exists()


def _stop_transcript(tmp_path: Path) -> Path:
    # input_tokens 190_000 / window 200_000 = 95% used >= precompact_at 80 → stop.
    return _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])


def test_hook_stop_userpromptsubmit_injects_final_nudge(stop_hook, tmp_path):
    # stop 밴드는 block/deny 대신 additionalContext 최종 넛지를 주입한다.
    stdin = {
        "transcript_path": str(_stop_transcript(tmp_path)),
        "session_id": "sess-final",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "다음 작업도 진행해줘",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    guidance = hso["additionalContext"]
    assert "ctx-nudge/최종" in guidance
    assert "python3 .project_manager/tools/pm_log.py checkpoint --task <이름> --trigger compaction" in guidance
    assert "<이름>`에는 현재 task 이름" in guidance
    assert "auto-compact" in guidance
    assert "permissionDecision" not in hso
    assert "decision" not in output
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (marker_dir / "sess-final.final").exists()


def test_hook_final_nudge_idempotent_within_cycle(stop_hook, tmp_path):
    """같은 상승 사이클의 stop 밴드에서는 `.final` 넛지를 한 번만 주입한다."""
    stdin = {
        "transcript_path": str(_stop_transcript(tmp_path)),
        "session_id": "sess-final-idem",
        "hook_event_name": "UserPromptSubmit",
    }
    rc1, out1 = stop_hook.evaluate(stdin, tmp_path, {})
    rc2, out2 = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc1 == rc2 == 0
    assert "additionalContext" in out1["hookSpecificOutput"]
    assert out2 is None
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-final-idem.final").exists()


def test_hook_no_transcript_passes(stop_hook, tmp_path):
    # transcript_path 없음 → used 0 → 통과.
    rc, output = stop_hook.evaluate({"session_id": "x"}, tmp_path, {})
    assert rc == 0 and output is None


def test_hook_final_marker_claim_failure_does_not_inject_and_retries(
        stop_hook, tmp_path, monkeypatch):
    # marker 선점 실패 호출은 무주입이고, 파일이 없으면 다음 호출에서 다시 선점할 수 있다.
    stdin = {
        "transcript_path": str(_stop_transcript(tmp_path)),
        "session_id": "sess-heal",
        "hook_event_name": "UserPromptSubmit",
    }

    def fail_open(*args, **kwargs):
        raise PermissionError("marker unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(stop_hook.os, "open", fail_open)
        rc1, out1 = stop_hook.evaluate(stdin, tmp_path, {})
    marker = tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-heal.final"
    assert not marker.exists()
    assert rc1 == 0 and out1 is None

    rc2, out2 = stop_hook.evaluate(stdin, tmp_path, {})
    assert marker.exists()
    assert rc2 == 0
    assert "additionalContext" in out2["hookSpecificOutput"]


def test_hook_atomic_marker_claim_race_loser_does_not_inject(
        stop_hook, tmp_path, monkeypatch):
    """경쟁 호출이 직전에 marker를 선점하면 `.nudge`/`.nudge2`/`.final` loser는 무주입."""
    cases = (("nudge", 150_000), ("nudge2", 156_000), ("final", 190_000))
    real_open = stop_hook.os.open
    claimed = []

    def competing_open(path, flags, mode=0o777):
        # evaluate의 O_EXCL 시점에 경쟁 호출이 같은 marker를 먼저 생성한 상황을 결정적으로 재현한다.
        fd = real_open(path, flags, mode)
        stop_hook.os.close(fd)
        claimed.append(Path(path).suffix.removeprefix("."))
        raise FileExistsError(path)

    monkeypatch.setattr(stop_hook.os, "open", competing_open)
    for suffix, tokens in cases:
        transcript = _write_transcript(
            tmp_path, [("assistant", {"input_tokens": tokens})])
        rc, output = stop_hook.evaluate({
            "transcript_path": str(transcript),
            "session_id": f"sess-race-{suffix}",
            "hook_event_name": "PreToolUse",
        }, tmp_path, {})
        marker = (
            tmp_path / ".project_manager" / ".local" / "ctx-stop" /
            f"sess-race-{suffix}.{suffix}"
        )
        assert rc == 0 and output is None
        assert marker.exists()

    assert claimed == ["nudge", "nudge2", "final"]


def test_hook_final_pretooluse_injects_without_permission_decision(stop_hook, tmp_path):
    # Claude Code v2.1.9+ PreToolUse additionalContext 비차단 주입 계약.
    stdin = {
        "transcript_path": str(_stop_transcript(tmp_path)),
        "session_id": "sess-pretool",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "python3 build.py"},
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "ctx-nudge/최종" in hso["additionalContext"]
    assert "permissionDecision" not in hso and "decision" not in output
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (marker_dir / "sess-pretool.final").exists()


def test_settings_wires_pretool_and_userprompt_only():
    # 압축 처리는 주 스레드 훅(PreToolUse·UserPromptSubmit)이 한다 — 압축 이벤트 등록은 없다(T-0953).
    # T-0202: 래퍼(ctx_stop_hook.sh) 경유 — 래퍼가 인터프리터 self-resolve 후 ctx_stop_hook.py 를
    #   exec(stdin/args/rc 투명 전달). 래퍼→.py 링크는 test_new_wrappers_self_contained 가 커버.
    data = json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8"))
    assert set(data["hooks"]) == {"PreToolUse", "UserPromptSubmit"}
    for event in ("PreToolUse", "UserPromptSubmit"):
        registrations = data["hooks"][event]
        assert isinstance(registrations, list) and registrations
        cmds = [h.get("command", "") for m in registrations for h in m.get("hooks", [])]
        assert any(c.endswith("ctx_stop_hook.sh") for c in cmds), f"{event} 에 ctx_stop_hook 래퍼 누락"
    assert data["hooks"]["PreToolUse"][0].get("matcher") == "*"


def test_hook_session_id_sanitized(stop_hook):
    # 경로 traversal 문자는 제거된다 (marker 파일명 안전).
    assert "/" not in stop_hook._session_id({"session_id": "../../etc/passwd"})
    assert stop_hook._session_id({}) == "unknown"


# ── 4. main() stdin 경로 (가짜 stdin → stdout JSON) ────────────────────────

def test_statusline_main_emits_line(statusline, monkeypatch, capsys, tmp_path):
    import io
    # 저장소 루트를 기록이 있는 tmp 로 고정 → 분모 200K 결정론(실 repo local.conf·기록 무관).
    monkeypatch.setattr(statusline.ctx_guard, "repo_root", lambda start: tmp_path)
    stdin = {"context_window": {"current_usage": {"input_tokens": 190_000}}}  # 95% of 200K.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stdin)))
    rc = statusline.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "ctx 95%" in out and "checkpoint 최종 알림" in out


def test_statusline_main_empty_stdin(statusline, monkeypatch, capsys, tmp_path):
    import io
    monkeypatch.setattr(statusline.ctx_guard, "repo_root", lambda start: tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = statusline.main()
    out = capsys.readouterr().out
    assert rc == 0 and "ctx 0%" in out


# ── 5. settings 배선 (statusLine·UserPromptSubmit 훅) ─────────────────────

# T-0202: statusLine·UserPromptSubmit 배선은 래퍼(.sh) 경유 — 래퍼가 인터프리터를 self-resolve
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
    assert pre and pre[0].get("matcher") == "*"
    assert "ctx_stop_hook.sh" in json.dumps(pre)


@pytest.mark.parametrize("name", ["settings.json"])
def test_settings_preserves_unrelated_pretooluse_hook(name):
    # ctx 가드와 무관한 훅(위임 채널 가드·Agent matcher)이 같은 이벤트에 공존한다
    # (회귀 — ctx 배선이 남의 훅을 덮지 않음. 옛 축이던 PostToolUse 회귀 훅은 T-0771 로 폐지).
    data = json.loads((CLAUDE / name).read_text(encoding="utf-8"))
    pre = data["hooks"]["PreToolUse"]
    agent = [m for m in pre if m.get("matcher") == "Agent"]
    assert agent, "template settings.json 에 Agent matcher 훅 누락"
    cmds = [h.get("command", "") for m in agent for h in m.get("hooks", [])]
    assert any("delegate_channel_guard_hook.sh" in c for c in cmds)


# ── 6. compaction-native 넛지 + 사이클 재무장 (ADR-0081) ──────────────────


def test_hook_nudge_userpromptsubmit_injects(guard, stop_hook, tmp_path):
    # nudge 레벨(used 75 — checkpoint_at 70 <= 75 < nudge2 77) + UserPromptSubmit → additionalContext 비차단 주입.
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
    assert "python3 .project_manager/tools/pm_log.py checkpoint --task <이름>" in hso["additionalContext"]
    assert "ctx-nudge" in hso["additionalContext"]
    # 비차단: deny/block 아님 (정지 스키마 부재).
    assert "permissionDecision" not in hso
    assert output.get("decision") != "block"
    # nudge marker 만 생성된다.
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-nudge.nudge").exists()


def test_hook_nudge_pretooluse_injects_and_consumes_cycle(stop_hook, tmp_path):
    # PreToolUse 가 먼저 발화하면 비차단 주입하고 채널 공유 marker 를 소비한다.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-nudge-ptu",
             "hook_event_name": "PreToolUse"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "ctx-nudge]" in hso["additionalContext"]
    assert "permissionDecision" not in hso and "decision" not in output
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-nudge-ptu.nudge").exists()
    stdin["hook_event_name"] = "UserPromptSubmit"
    assert stop_hook.evaluate(stdin, tmp_path, {}) == (0, None)


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


def test_hook_nudge_independent_from_final(stop_hook, tmp_path):
    # 1단 marker 가 있어도 stop 밴드의 최종 비차단 넛지는 별도로 발화한다.
    sid = "sess-nudge-final"
    nudge_tx = _write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])
    nudge_stdin = {"transcript_path": str(nudge_tx), "session_id": sid,
                   "hook_event_name": "UserPromptSubmit"}
    stop_hook.evaluate(nudge_stdin, tmp_path, {})  # nudge 주입.
    stop_tx = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stop_stdin = {
        "transcript_path": str(stop_tx),
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stop_stdin, tmp_path, {})
    guidance = output["hookSpecificOutput"]["additionalContext"]
    assert "ctx-nudge/최종" in guidance
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (marker_dir / f"{sid}.nudge").exists()
    assert (marker_dir / f"{sid}.final").exists()


def test_hook_ok_rearms_all_cycle_markers(stop_hook, tmp_path):
    sid = "sess-rearm-all"
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    marker_dir.mkdir(parents=True)
    for suffix in ("nudge", "nudge2", "final"):
        (marker_dir / f"{sid}.{suffix}").write_text("fired\n", encoding="utf-8")
    ok = {
        "transcript_path": str(_write_transcript(tmp_path, [("assistant", {"input_tokens": 100_000})])),
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(ok, tmp_path, {})
    assert rc == 0 and output is None
    assert not any((marker_dir / f"{sid}.{suffix}").exists() for suffix in ("nudge", "nudge2", "final"))


def test_hook_zero_pct_with_raw_tokens_rearms_cycle_markers(stop_hook, tmp_path):
    """1M 예산의 raw 4K는 정수 0%여도 유효 측정이므로 ok 사이클을 재무장한다."""
    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(tmp_path, {"harness.claude.ctx_window_tokens": "1000000"})
    sid = "sess-measured-zero-pct"
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    marker_dir.mkdir(parents=True)
    markers = [marker_dir / f"{sid}.{suffix}" for suffix in ("nudge", "nudge2", "final")]
    for marker in markers:
        marker.write_text("fired\n", encoding="utf-8")

    transcript = _write_transcript(tmp_path, [("assistant", {"input_tokens": 4_000})])
    assert stop_hook.ctx_guard.context_used_pct_from_transcript(transcript, 1_000_000) == 0
    stdin = {
        "transcript_path": str(transcript),
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {"harness.claude.ctx_window_tokens": "1000000"})
    assert rc == 0 and output is None
    assert not any(marker.exists() for marker in markers)


@pytest.mark.parametrize("measurement", ["absent", "unreadable", "usage-missing"])
def test_hook_unmeasurable_raw_zero_preserves_cycle_markers(
        stop_hook, tmp_path, measurement):
    """raw 0 폴백은 측정불가이므로 현재 사이클 marker를 보존한다."""
    sid = f"sess-unmeasurable-{measurement}"
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    marker_dir.mkdir(parents=True)
    markers = [marker_dir / f"{sid}.{suffix}" for suffix in ("nudge", "nudge2", "final")]
    for marker in markers:
        marker.write_text("fired\n", encoding="utf-8")

    stdin = {"session_id": sid, "hook_event_name": "UserPromptSubmit"}
    if measurement == "unreadable":
        unreadable = tmp_path / "unreadable-transcript"
        unreadable.mkdir()
        stdin["transcript_path"] = str(unreadable)
    elif measurement == "usage-missing":
        stdin["transcript_path"] = str(_write_transcript(tmp_path, [("assistant", None)]))

    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    assert all(marker.exists() for marker in markers)


def test_hook_nudge_refires_after_ok_rearm(stop_hook, tmp_path):
    sid = "sess-rearm-cycle"

    def evaluate_at(tokens):
        return stop_hook.evaluate({
            "transcript_path": str(_write_transcript(
                tmp_path, [("assistant", {"input_tokens": tokens})])),
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
        }, tmp_path, {})

    _, first = evaluate_at(150_000)   # nudge → marker 생성.
    _, duplicate = evaluate_at(150_000)
    _, compacted = evaluate_at(100_000)  # ok → marker 삭제(re-arm).
    _, second_cycle = evaluate_at(150_000)
    assert first is not None and duplicate is None and compacted is None
    assert second_cycle is not None


def test_hook_nudge_refires_after_boundary_without_ok_observation(
        stop_hook, tmp_path, monkeypatch):
    """nudge 뒤 ok 관측 없이 경계가 늘면 첫 주 스레드 훅이 marker를 지워 재상승 넛지를 허용한다."""
    monkeypatch.setattr(stop_hook, "_compaction_output", lambda *_a: None)
    sid = "sess-boundary-rearm"
    transcript = _write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})])
    prompt = {
        "transcript_path": str(transcript),
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
    }

    rc1, first = stop_hook.evaluate(prompt, tmp_path, {})
    rc2, duplicate = stop_hook.evaluate(prompt, tmp_path, {})
    _append_transcript_entry(transcript, _compact_boundary())
    _append_transcript_entry(transcript, {
        "type": "assistant",
        "message": {"role": "assistant", "usage": {"input_tokens": 150_000}},
    })
    rc3, next_cycle = stop_hook.evaluate(prompt, tmp_path, {})
    rc4, duplicate_again = stop_hook.evaluate(prompt, tmp_path, {})

    assert rc1 == rc2 == rc3 == rc4 == 0
    assert first is not None and duplicate is None
    assert "ctx-nudge" in next_cycle["hookSpecificOutput"]["additionalContext"]
    assert duplicate_again is None


def test_hook_boundary_waits_for_postboundary_usage_before_refiring(
        stop_hook, tmp_path, monkeypatch):
    """경계 뒤 첫 prompt는 marker를 재무장하고 old usage로 재생성하지 않으며 새 usage를 기다린다."""
    monkeypatch.setattr(stop_hook, "_compaction_output", lambda *_a: None)
    sid = "sess-boundary-wait"
    transcript = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    prompt = {
        "transcript_path": str(transcript),
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
    }
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    markers = [marker_dir / f"{sid}.{suffix}" for suffix in ("nudge", "nudge2", "final")]

    rc1, first = stop_hook.evaluate(prompt, tmp_path, {})
    assert rc1 == 0 and first is not None
    assert markers[-1].exists()

    _append_transcript_entry(transcript, _compact_boundary())
    rc3, first_prompt = stop_hook.evaluate(prompt, tmp_path, {})
    assert rc3 == 0 and first_prompt is None
    assert not any(marker.exists() for marker in markers)
    assert (marker_dir / f"{sid}.compact-1").exists()

    _append_transcript_entry(transcript, {
        "type": "assistant",
        "message": {"role": "assistant", "usage": {"input_tokens": 190_000}},
    })
    rc4, next_high = stop_hook.evaluate(prompt, tmp_path, {})
    assert rc4 == 0 and next_high is not None
    assert markers[-1].exists()


# ── 7. 강화 넛지 + 최종 넛지 (ADR-0081) ──────────────────────────────────
# nudge2 는 checkpoint 즉시 실행을 재안내하고, stop 밴드는 auto-compact 임박을 알리는 최종 넛지다.
# 별도 `.nudge2`/`.final` marker 로 같은 사이클에서 각 1회 발화한다. 밴드(기본 쓴 % 70/80):
# nudge2_threshold=77 → 쓴 [77,80) 이 nudge2, [70,77) 이 nudge(1단).

def _nudge2_transcript(tmp_path: Path) -> Path:
    # 156_000 / 200_000 = 78% used (77 <= 78 < 80) → nudge2 밴드.
    return _write_transcript(tmp_path, [("assistant", {"input_tokens": 156_000})])


@pytest.mark.parametrize("band", ["nudge", "nudge2", "final"])
def test_engine_failure_fallback_keeps_continuity_policy_in_every_band(
    stop_hook, tmp_path, band,
):
    """pm_log 부재 fallback도 세션 종료/핸드오프 신호로 퇴행하지 않는다."""
    guidance = stop_hook._build_ctx_guidance(
        tmp_path,
        band=band,
        used_pct=92,
        thresholds={"checkpoint_at_pct": 70, "precompact_at_pct": 80},
    )

    assert "압축은 자동이고 세션은 그대로 이어진다" in guidance
    assert "핸드오프는 사용자 명시 지시로만 한다" in guidance
    assert "새 큰 작업보다 현재 서사 기록을 우선" not in guidance
    # 사용률은 쓴 % 하나다 — 남은 % 를 다시 계산해 싣지 않는다.
    assert "컨텍스트 사용 92%" in guidance and "잔여" not in guidance


def test_hook_nudge2_userpromptsubmit_injects(guard, stop_hook, tmp_path):
    # nudge2 레벨(쓴 78 — nudge2_threshold 77 <= 78 < precompact_at 80) + UserPromptSubmit → strong 주입.
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
    assert "ctx-nudge/강화" in hso["additionalContext"]
    assert "python3 .project_manager/tools/pm_log.py checkpoint --task <이름>" in hso["additionalContext"]
    # 비차단: deny/block 아님.
    assert "permissionDecision" not in hso
    assert output.get("decision") != "block"
    # nudge2 marker 를 생성한다.
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (ctx / "sess-nudge2.nudge2").exists()


def test_hook_nudge2_pretooluse_injects_without_permission_decision(stop_hook, tmp_path):
    # nudge2 레벨도 PreToolUse additionalContext 비차단 주입(제결정 없음).
    stdin = {"transcript_path": str(_nudge2_transcript(tmp_path)), "session_id": "sess-n2-ptu",
             "hook_event_name": "PreToolUse"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "ctx-nudge/강화" in hso["additionalContext"]
    assert "permissionDecision" not in hso and "decision" not in output
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-n2-ptu.nudge2").exists()


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
    assert "ctx-nudge/강화" in output["hookSpecificOutput"]["additionalContext"]
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (ctx / "sess-n2-solo.nudge2").exists()
    assert not (ctx / "sess-n2-solo.nudge").exists()


def test_hook_nudge1_then_nudge2_both_fire(stop_hook, tmp_path):
    # 세션이 nudge(1단·쓴 75)를 거쳐 nudge2(2단·쓴 78)로 진행 → 두 marker 다 생성·각 문구 구별.
    sid = "sess-both"
    # 1단: 쓴 75 (checkpoint_at 70 <= 75 < nudge2_threshold 77).
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
    assert "ctx-nudge/강화" in out2["hookSpecificOutput"]["additionalContext"]
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (ctx / f"{sid}.nudge").exists()
    assert (ctx / f"{sid}.nudge2").exists()


def test_hook_nudge2_independent_from_final(stop_hook, tmp_path):
    # nudge2 marker 가 있어도 stop 밴드의 최종 넛지는 별도로 주입된다.
    sid = "sess-n2-final"
    nudge2_stdin = {
        "transcript_path": str(_nudge2_transcript(tmp_path)),
        "session_id": sid, "hook_event_name": "UserPromptSubmit",
    }
    stop_hook.evaluate(nudge2_stdin, tmp_path, {})  # nudge2 주입.
    stop_stdin = {
        "transcript_path": str(_write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])),
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stop_stdin, tmp_path, {})
    assert "ctx-nudge/최종" in output["hookSpecificOutput"]["additionalContext"]
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert (ctx / f"{sid}.nudge2").exists()
    assert (ctx / f"{sid}.final").exists()


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


def test_settings_auto_compact_enabled_true():
    # hard-stop 없이 메인/서브에이전트 모두 native compaction 을 허용한다.
    data = json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8"))
    assert data.get("autoCompactEnabled") is True, (
        "settings.json autoCompactEnabled 가 true 가 아님 — 서브에이전트 compaction 봉쇄 재발 위험(T-0458)"
    )

@pytest.fixture(autouse=True)
def _ctx_record(tmp_path):
    """밴드 판정·표시의 분모 — tmp_path 에 기본 입력(사용 한도 200,000)으로 적용한 압축 한도 기록."""
    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(tmp_path)


# ── T-0943 세션 모델 확인과 기록 오류 (AT-006) ─────────────────────────────────
# 실제 훅 입력에는 모델이 없다(Claude Code 2.1.292 · PreToolUse·UserPromptSubmit·Stop 공통 필드).
_T0943_CONF = {"harness.claude.ctx_window_tokens": "600000", "ctx.checkpoint_at_pct": "80",
               "ctx.precompact_at_pct": "90", "ctx.compact_at_pct": "94"}


def _t0943_model_transcript(root: Path, input_tokens: int, model: str) -> Path:
    path = root / "transcript-model.jsonl"
    path.write_text(json.dumps({"type": "assistant", "isSidechain": False, "message": {
        "role": "assistant", "model": model, "usage": {"input_tokens": input_tokens}}}) + "\n",
        encoding="utf-8")
    return path


def _t0943_hook_input(root: Path, event: str, transcript: Path) -> dict:
    payload = {"session_id": "t0943-session", "transcript_path": str(transcript), "cwd": str(root),
               "permission_mode": "default", "hook_event_name": event}
    if event == "PreToolUse":
        payload.update(tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="toolu_t0943")
    else:
        payload.update(prompt="계속")
    return payload


def test_t0943_statusline_model_check_and_record_error(statusline, tmp_path):
    """상태줄 — 기록 모델과 같은 `model.id` 면 E 대비 사용 %, 다르면 같은 % 와 경고, 기록이 없으면 오류 문구."""
    from test_harness_ctx_limits import install_ctx_record
    model = install_ctx_record(tmp_path, _T0943_CONF, capacity=200000)
    payload = {"model": {"id": model, "display_name": "Fixture"},
               "transcript_path": str(tmp_path / "t.jsonl"),
               "context_window": {"current_usage": {"input_tokens": 180000}}}
    line = statusline.build_statusline(payload, _T0943_CONF, root=tmp_path)
    assert "ctx 90%" in line and "모델이 바뀌었습니다" not in line
    other = statusline.build_statusline({**payload, "model": {"id": "claude-other"}}, _T0943_CONF, root=tmp_path)
    assert "ctx 90%" in other and "모델이 바뀌었습니다" in other and "claude-other" in other
    (tmp_path / ".project_manager/.local/ctx-budget.json").unlink()
    missing = statusline.build_statusline(payload, _T0943_CONF, root=tmp_path)
    assert "ctx 한도 기록 오류" in missing and "`pm_config.py apply`" in missing


def test_t0943_hook_real_input_fields_nudge_and_model_warning(stop_hook, tmp_path):
    """훅 — 모델 없는 실제 입력에 transcript 사용량 항목의 모델로 확인한다. 같으면 80% 넛지, 다르면
    UserPromptSubmit `systemMessage` 경고(넛지는 그대로)."""
    from test_harness_ctx_limits import install_ctx_record
    model = install_ctx_record(tmp_path, _T0943_CONF, capacity=200000)
    transcript = _t0943_model_transcript(tmp_path, 162000, model)               # 81%
    rc, output = stop_hook.evaluate(_t0943_hook_input(tmp_path, "PreToolUse", transcript), tmp_path, _T0943_CONF)
    assert rc == 0 and "additionalContext" in output["hookSpecificOutput"] and "systemMessage" not in output

    transcript = _t0943_model_transcript(tmp_path, 182000, "claude-other")      # 91%
    rc, output = stop_hook.evaluate(_t0943_hook_input(tmp_path, "UserPromptSubmit", transcript), tmp_path,
                                    _T0943_CONF)
    assert rc == 0 and "additionalContext" in output["hookSpecificOutput"]
    assert "모델이 바뀌었습니다" in output["systemMessage"] and "claude-other" in output["systemMessage"]


def test_t0943_hook_missing_record_is_reported(stop_hook, tmp_path, capsys):
    """기록이 없으면 UserPromptSubmit 은 `systemMessage`, PreToolUse 는 stderr 로 알린다(조용히 넘기지 않는다)."""
    (tmp_path / ".project_manager/.local/ctx-budget.json").unlink()
    transcript = _t0943_model_transcript(tmp_path, 190000, "fixture-model")
    rc, output = stop_hook.evaluate(_t0943_hook_input(tmp_path, "UserPromptSubmit", transcript), tmp_path, {})
    assert rc == 0 and output == {"systemMessage": output["systemMessage"]}
    assert output["systemMessage"].startswith("[ctx-budget]") and "`pm_config.py apply`" in output["systemMessage"]
    capsys.readouterr()
    assert stop_hook.evaluate(_t0943_hook_input(tmp_path, "PreToolUse", transcript), tmp_path, {}) == (0, None)
    assert "[ctx-budget]" in capsys.readouterr().err


# ── T-0953 압축 기록: 경계 감지·서브에이전트 면제·실패 신호 ──────────────────────
# 훅 입력 필드는 Claude Code 2.1.290·2.1.293 실측 형상이다(설계 3.1절): 주 스레드 PreToolUse 는
#   `agent_id` 가 없고, 서브에이전트 도구 호출은 `agent_id`·`agent_type` 이 있으며 `session_id`·
#   `transcript_path` 는 주 세션 값과 같다. 압축 경계는 주 대화 기록의
#   `{"type":"system","subtype":"compact_boundary"}` 항목이다(설계 3.3절).

_T0953_SNAPSHOT = "## PM 정체성 (compaction 복구)\n- task: main\n"


def _t0953_engine(root: Path) -> None:
    engine = root / ".project_manager" / "tools" / "pm_log.py"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_text("", encoding="utf-8")


def _t0953_transcript(root: Path, tokens: list[int], boundaries_before: list[bool]) -> Path:
    """usage 항목마다 앞에 경계를 둘지 정해 주 대화 기록을 쓴다."""
    path = root / "main-session.jsonl"
    path.write_text("", encoding="utf-8")
    for value, boundary in zip(tokens, boundaries_before):
        if boundary:
            _append_transcript_entry(path, _compact_boundary())
        _append_transcript_entry(path, {
            "type": "assistant", "isSidechain": False,
            "message": {"role": "assistant", "usage": {"input_tokens": value}},
        })
    return path


def _t0953_fake_run(calls: list, *, checkpoint=(0, "", ""), snapshot=(0, _T0953_SNAPSHOT, "")):
    def fake_run(command, **kwargs):
        calls.append(command)
        verb = command[2]
        rc, stdout, stderr = {
            "checkpoint": checkpoint, "snapshot": snapshot,
        }.get(verb, (1, "", ""))
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)
    return fake_run


def _t0953_pretool(transcript: Path, root: Path, **extra) -> dict:
    return {
        "session_id": "s-1", "transcript_path": str(transcript), "cwd": str(root),
        "permission_mode": "default", "hook_event_name": "PreToolUse",
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_t0953",
        **extra,
    }


def _t0953_engine_calls(calls: list) -> list:
    return [command[2:] for command in calls if command[2] in ("checkpoint", "snapshot")]


def test_t0953_main_hook_after_boundary_runs_checkpoint_and_snapshot_once(
        stop_hook, tmp_path, monkeypatch):
    """경계 N 이 늘면 첫 주 스레드 훅이 한 번 재무장·checkpoint·snapshot 을 한다(AT-007)."""
    _t0953_engine(tmp_path)
    transcript = _t0953_transcript(tmp_path, [150_000, 1_000, 2_000], [False, True, True])
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    marker_dir.mkdir(parents=True)
    for suffix in ("nudge", "final"):
        (marker_dir / f"s-1.{suffix}").write_text("fired\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(stop_hook.subprocess, "run", _t0953_fake_run(calls))

    rc, output = stop_hook.evaluate(_t0953_pretool(transcript, tmp_path), tmp_path, {})

    assert rc == 0
    assert _t0953_engine_calls(calls) == [
        ["checkpoint", "--trigger", "compaction", "--session-id", "s-1",
         "--boundary-id", "claude-s-1-2", "--breadcrumb"],
        ["snapshot", "--session-id", "s-1"],
    ]
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("## PM 정체성 (compaction 복구)")
    assert "permissionDecision" not in output["hookSpecificOutput"]
    assert (marker_dir / "s-1.compact-2").is_file()
    assert not (marker_dir / "s-1.nudge").exists() and not (marker_dir / "s-1.final").exists()

    calls.clear()
    rc, output = stop_hook.evaluate(_t0953_pretool(transcript, tmp_path), tmp_path, {})
    assert rc == 0 and calls == []
    assert "PM 정체성" not in json.dumps(output, ensure_ascii=False)


def test_t0953_subagent_exemption_uses_agent_id_not_agent_type(stop_hook, tmp_path, monkeypatch):
    """`agent_id` 있는 입력은 아무 상태도 바꾸지 않는다 — `agent_type` 만 있는 주 스레드는 처리한다(AT-008)."""
    _t0953_engine(tmp_path)
    transcript = _t0953_transcript(tmp_path, [190_000], [True])   # 경계 1개 뒤 95%
    marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    marker_dir.mkdir(parents=True)
    (marker_dir / "s-1.nudge").write_text("fired\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(stop_hook.subprocess, "run", _t0953_fake_run(calls))

    subagent = _t0953_pretool(transcript, tmp_path, agent_id="a1", agent_type="Explore")
    assert stop_hook.evaluate(subagent, tmp_path, {}) == (0, None)
    assert calls == []
    assert not (marker_dir / "s-1.compact-1").exists()
    assert (marker_dir / "s-1.nudge").read_text(encoding="utf-8") == "fired\n"
    assert not (marker_dir / "s-1.final").exists()

    agent_session = _t0953_pretool(transcript, tmp_path, agent_type="pm")
    rc, output = stop_hook.evaluate(agent_session, tmp_path, {})
    assert rc == 0 and output is not None
    assert (marker_dir / "s-1.compact-1").is_file()
    assert [call[0] for call in _t0953_engine_calls(calls)].count("checkpoint") == 1


def test_t0953_failure_signal_carries_engine_reason_once(stop_hook, tmp_path, monkeypatch):
    """기록 실패는 엔진 stderr 마지막 줄을 `systemMessage`·`additionalContext` 로 한 번 보인다(AT-009)."""
    _t0953_engine(tmp_path)
    transcript = _t0953_transcript(tmp_path, [150_000, 1_000, 2_000], [False, True, True])
    reason = ("[중단] checkpoint 세션 task 미해소: 이 세션이 연 task 없음 (세션 s-1). "
              "--task 이름 을 주거나 이 세션에서 pm_bootstrap.py --task 이름 으로 task 를 연다.")
    calls: list = []
    monkeypatch.setattr(stop_hook.subprocess, "run", _t0953_fake_run(
        calls, checkpoint=(1, "", f"경고\n{reason}\n")))

    rc, output = stop_hook.evaluate(_t0953_pretool(transcript, tmp_path), tmp_path, {})

    assert rc == 0
    assert [call[0] for call in _t0953_engine_calls(calls)] == ["checkpoint"]
    assert output["systemMessage"].startswith(
        "[ctx-checkpoint] 압축 기록 실패: [중단] checkpoint 세션 task 미해소: 이 세션이 연 task 없음")
    assert output["systemMessage"] in output["hookSpecificOutput"]["additionalContext"]

    calls.clear()
    rc, output = stop_hook.evaluate(_t0953_pretool(transcript, tmp_path), tmp_path, {})
    assert rc == 0 and calls == []
    assert output is None or "systemMessage" not in output

    _append_transcript_entry(transcript, _compact_boundary())
    _append_transcript_entry(transcript, {
        "type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 3_000}},
    })
    snapshot_reason = "[pm-snapshot] 세션 task 미해소: 세션 ID 없음. 재주입 생략."
    monkeypatch.setattr(stop_hook.subprocess, "run", _t0953_fake_run(
        calls, snapshot=(1, "", snapshot_reason + "\n")))
    rc, output = stop_hook.evaluate(_t0953_pretool(transcript, tmp_path), tmp_path, {})
    assert rc == 0
    assert output["systemMessage"] == "[ctx-checkpoint] 압축 기록 실패: " + snapshot_reason


def test_t0953_transcript_reading_counts_compact_boundaries(guard, tmp_path):
    """한 번 읽기로 점유 토큰·모델·경계 수를 낸다 — 비슷한 subtype 과 본문 문자열은 세지 않는다(AT-010)."""
    path = tmp_path / "boundaries.jsonl"
    entries = [
        _compact_boundary(),
        {"type": "assistant", "message": {"role": "assistant", "model": "m1",
                                          "usage": {"input_tokens": 5_000}}},
        {"type": "system", "subtype": "compact_boundary_note"},
        {"type": "user", "message": {"role": "user", "content": "compact_boundary"}},
        _compact_boundary(),
        {"type": "assistant", "message": {"role": "assistant", "model": "m2",
                                          "usage": {"input_tokens": 7_000}}},
    ]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    assert guard.context_tokens_from_transcript(path) == (7_000, "m2", 2)

    three = tmp_path / "three.jsonl"
    three.write_text("".join(json.dumps(entry) + "\n" for entry in (
        _compact_boundary(),
        {"type": "assistant", "message": {"usage": {"input_tokens": 9}}},
        _compact_boundary(), _compact_boundary(),
    )), encoding="utf-8")
    assert guard.context_tokens_from_transcript(three) == (0, None, 3)
    assert guard.context_tokens_from_transcript(tmp_path / "nope.jsonl") == (0, None, 0)
