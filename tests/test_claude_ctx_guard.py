"""claude 어댑터 ctx compaction-native 넛지 단위 테스트 (T-0550).

어댑터 스크립트(templates/claude_code/.claude/ctx_guard·ctx_statusline·ctx_stop_hook)를
importlib 로 직접 로드해 검증한다. stdlib only — 라이브 claude·subprocess 호출 없이
가짜 transcript JSONL·가짜 statusline stdin·격리 tmp 만 본다.

검증 축:
  1. 분모·임계 — 압축 한도 기록(E·비율)과 세션 모델 확인(T-0943).
  2. statusLine — context_window → used % (current_usage 단일 소스·ADR-0041) + 색/문구 넛지.
  3. 훅 — 세 밴드 모두 PreToolUse/UserPromptSubmit additionalContext 비차단 주입 +
     채널 공유 사이클별 marker 멱등.
  4. re-arm — PostCompact 완료 경계 또는 유효한 ok 실측 시 `.nudge`/`.nudge2`/`.final` 삭제.
  5. settings 배선 — PreToolUse·UserPromptSubmit·PostCompact 를 ctx 훅에 연결.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _textio import normalize_newline_bytes, utf8_child_env, write_lf
from _win_skip import posix_mode_supported

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
    assert guard.context_tokens_from_transcript(path) == (180_000, None)


def test_transcript_tokens_stop_at_compact_boundary_without_new_usage(guard, tmp_path):
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    _append_transcript_entry(path, _compact_boundary())

    # compactMetadata.postTokens 는 compact 결과 크기지만 아직 새 assistant 요청의 usage 실측이
    # 아니다. 경계 뒤 usage 가 없으면 측정불가 0으로 두어 이전 사이클 usage를 재사용하지 않는다.
    assert guard.context_tokens_from_transcript(path) == (0, None)


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

    assert guard.context_tokens_from_transcript(path) == (150_002, None)


def test_transcript_used_pct(guard, tmp_path):
    path = _write_transcript(tmp_path, [
        ("assistant", {"input_tokens": 180_000}),
    ])
    assert guard.context_used_pct_from_transcript(path, 200_000) == 90


def test_transcript_missing_file_zero(guard, tmp_path):
    assert guard.context_tokens_from_transcript(tmp_path / "nope.jsonl") == (0, None)
    assert guard.context_used_pct_from_transcript(tmp_path / "nope.jsonl", 200_000) == 0


def test_transcript_malformed_lines_skipped(guard, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"message": {"usage": {"input_tokens": 20_000}}}) + "\n"
        + "{broken\n",
        encoding="utf-8",
    )
    assert guard.context_tokens_from_transcript(path) == (20_000, None)


def test_hook_evaluate_ok_passes(guard, stop_hook, tmp_path):
    # 쓴 % 낮음 (used 50) → 출력 없이 통과.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 100_000})])
    stdin = {"transcript_path": str(path), "session_id": "sess-ok"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop").exists()


@pytest.mark.parametrize(
    ("band_present", "observation_present"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_precompact_auto_band_mismatch_four_quadrants(
    stop_hook, tmp_path, monkeypatch, band_present, observation_present,
):
    """auto PreCompact의 marker 有/無 × 관측 有/無가 엔진 인자로 그대로 전달된다."""
    from test_harness_ctx_limits import install_ctx_record
    manager = tmp_path / ".project_manager"
    install_ctx_record(tmp_path, {"harness.claude.ctx_window_tokens": "600000"})
    session_id = "auto-quadrant"
    if band_present:
        marker = manager / ".local" / "ctx-stop" / f"{session_id}.final"
        marker.parent.mkdir(parents=True)
        marker.write_text("ctx-final nudge injected\n", encoding="utf-8")
    stdin = {"trigger": "auto", "session_id": session_id}
    if observation_present:
        stdin["transcript_path"] = str(_write_transcript(
            tmp_path, [("assistant", {"input_tokens": 30_000})], sidechain=False,
        ))
    calls = []
    monkeypatch.setattr(
        stop_hook, "_create_checkpoint",
        lambda root, payload, **kwargs: calls.append((root, payload, kwargs)),
    )

    assert stop_hook.capture_precompact(stdin, tmp_path) == 0
    assert len(calls) == 1
    root, payload, kwargs = calls[0]
    assert root == tmp_path and payload is stdin
    assert kwargs["phase"] == "pre" and kwargs["breadcrumb"] is True
    assert kwargs["ctx_band_checked"] is True
    assert kwargs["ctx_band_missed"] is (not band_present)
    assert kwargs["ctx_window_tokens"] == 600_000
    assert kwargs["ctx_observed_tokens"] == (30_000 if observation_present else 0)
    assert kwargs["harness"] == "claude"


@pytest.mark.parametrize("trigger", ["manual", None, "future-trigger"])
def test_precompact_non_auto_trigger_conservatively_suppresses_mismatch(
    stop_hook, tmp_path, monkeypatch, trigger,
):
    """manual /compact와 trigger 부재·미지값은 관측치가 있어도 auto 진단을 만들지 않는다."""
    transcript = _write_transcript(
        tmp_path, [("assistant", {"input_tokens": 30_000})], sidechain=False,
    )
    stdin = {
        "session_id": "non-auto-trigger",
        "transcript_path": str(transcript),
    }
    if trigger is not None:
        stdin["trigger"] = trigger
    calls = []
    monkeypatch.setattr(
        stop_hook, "_create_checkpoint",
        lambda root, payload, **kwargs: calls.append(kwargs),
    )

    assert stop_hook.capture_precompact(stdin, tmp_path) == 0
    assert len(calls) == 1
    assert calls[0]["ctx_band_checked"] is False
    assert calls[0]["ctx_band_missed"] is False
    assert calls[0]["ctx_window_tokens"] == 0
    assert calls[0]["ctx_observed_tokens"] == 0


def test_hook_new_checkpoint_options_retry_old_engine_once(
    stop_hook, tmp_path, monkeypatch,
):
    """구 pm_log가 진단 옵션을 거부해도 구 argv로 compaction checkpoint를 남긴다."""
    engine = tmp_path / ".project_manager" / "tools" / "pm_log.py"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_text("# old engine probe\n", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=2 if len(calls) == 1 else 0)

    monkeypatch.setattr(stop_hook.subprocess, "run", fake_run)

    stop_hook._create_checkpoint(
        tmp_path, {"session_id": "legacy-session"}, phase="pre", breadcrumb=True,
        ctx_band_checked=True, ctx_band_missed=True, ctx_window_tokens=600_000,
        ctx_observed_tokens=30_000, harness="claude",
    )
    assert len(calls) == 2
    new_options = {
        "--ctx-band-checked", "--ctx-band-missed", "--ctx-window-tokens",
        "--ctx-observed-tokens", "--harness",
    }
    assert new_options & set(calls[0][0]) == new_options
    assert new_options.isdisjoint(calls[1][0])
    assert "--breadcrumb" in calls[1][0]
    assert all(call[1]["stdout"] is stop_hook.subprocess.DEVNULL for call in calls)
    assert calls[0][1]["stderr"] is stop_hook.subprocess.PIPE
    assert calls[1][1]["stderr"] is stop_hook.subprocess.DEVNULL


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


def test_settings_wires_precompact_pretool_userprompt_and_postcompact():
    # PreCompact는 durable flush+골격, PostCompact는 snapshot marker 무장 채널이다.
    # T-0202: 이제 래퍼(ctx_stop_hook.sh) 경유 — 래퍼가 인터프리터 self-resolve 후 ctx_stop_hook.py 를
    #   exec(stdin/args/rc 투명 전달). 래퍼→.py 링크는 test_new_wrappers_self_contained 가 커버.
    data = json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8"))
    for event in ("PreToolUse", "UserPromptSubmit", "PostCompact"):
        registrations = data["hooks"][event]
        assert isinstance(registrations, list) and registrations
        cmds = [h.get("command", "") for m in registrations for h in m.get("hooks", [])]
        assert any("ctx_stop_hook.sh" in c for c in cmds), f"{event} 에 ctx_stop_hook 래퍼 누락"
    # UserPromptSubmit 에는 git-anchor 허용 기록 훅(`--git-anchor-hook`)이 함께 있다 — ctx 훅 그룹만 대조한다.
    ctx_userprompt = [group for group in data["hooks"]["UserPromptSubmit"]
                      if "--git-anchor-hook" not in json.dumps(group)]
    assert data["hooks"]["PostCompact"] == ctx_userprompt, (
        "PostCompact 등록이 UserPromptSubmit ctx 훅과 동형이 아님")
    assert data["hooks"]["PreToolUse"][0].get("matcher") == "*"
    precompact = json.dumps(data["hooks"]["PreCompact"])
    assert "precompact_capture_hook.sh" in precompact
    assert "ctx_stop_hook.sh" not in precompact, "PreCompact가 재무장 훅을 직접 호출함"


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


def test_hook_nudge_refires_after_postcompact_without_ok_observation(stop_hook, tmp_path):
    """nudge 뒤 ok 관측 없이 PostCompact가 오면 marker를 지워 재상승 넛지를 허용한다."""
    sid = "sess-postcompact-rearm"
    high = str(_write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})]))
    prompt = {
        "transcript_path": high,
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
    }

    rc1, first = stop_hook.evaluate(prompt, tmp_path, {})
    rc2, duplicate = stop_hook.evaluate(prompt, tmp_path, {})
    rc3, compacting = stop_hook.evaluate({
        "transcript_path": high,
        "session_id": sid,
        "hook_event_name": "PostCompact",
    }, tmp_path, {})
    rc4, next_cycle = stop_hook.evaluate(prompt, tmp_path, {})

    assert rc1 == rc2 == rc3 == rc4 == 0
    assert first is not None and duplicate is None
    assert compacting is None
    assert next_cycle is not None


def test_postcompact_arms_snapshot_then_first_supported_channel_injects_once(
        stop_hook, tmp_path, monkeypatch):
    """PostCompact 저장→첫 UserPromptSubmit verbatim 주입→payload 소거 (marker-armed 정본)."""
    payload = "## PM 정체성 (compaction 복구)\n- task: main\n"
    checkpoints = []
    monkeypatch.setattr(stop_hook, "_build_snapshot", lambda root, stdin: payload)
    monkeypatch.setattr(
        stop_hook, "_create_checkpoint",
        lambda root, stdin, **kwargs: checkpoints.append((stdin["session_id"], kwargs)),
    )
    boundary = {"session_id": "sess-snapshot", "hook_event_name": "PostCompact"}
    assert stop_hook.evaluate(boundary, tmp_path, {}) == (0, None)
    marker = (tmp_path / ".project_manager" / ".local" / "ctx-stop" /
              "compact-snapshot.sess-snapshot")
    assert marker.read_text(encoding="utf-8") == payload
    assert checkpoints == [("sess-snapshot", {"phase": "post"})]

    prompt = {"session_id": "sess-snapshot", "hook_event_name": "UserPromptSubmit"}
    rc, output = stop_hook.evaluate(prompt, tmp_path, {})
    assert rc == 0
    assert output["hookSpecificOutput"]["additionalContext"] == payload
    assert not marker.exists()
    assert stop_hook.evaluate(prompt, tmp_path, {}) == (0, None)


def _postcompact_durable_mismatch_subprocess_probe(
    tmp_path: Path,
    *,
    consume_durable_mismatch: bool = False,
    failure: str | None = None,
    read_failure_as_none: bool = False,
    drop_append_fallback: bool = False,
) -> dict:
    """출하 hook과 pm_log CLI를 격리 트리에서 실제 subprocess로 잇는다.

    pm_log의 sibling loader와 hook→engine 경계를 그대로 보존하려고 tools 전체를 복사한다. 단순
    monkeypatch 단위검사보다 느리지만, T-0661의 쟁점인 "snapshot marker 소비와 append-only 진단
    원천은 별개"라는 프로세스 간 수명주기를 검증하려면 이 경계를 줄이면 안 된다.

    ``consume_durable_mismatch``는 회귀 감도 확인용 임시 사본 변형이다. 첫 snapshot 조회가 진단
    원천을 소비하게 만들어, 같은 불변식 단언이 실제로 실패하는지 확인한다. 출하 파일은 안 바꾼다.
    """
    root = tmp_path / "repo"
    shutil.copytree(REPO / ".project_manager" / "tools", root / ".project_manager" / "tools")
    claude = root / ".claude"
    claude.mkdir()
    for name in ("ctx_guard.py", "ctx_stop_hook.py"):
        shutil.copy2(CLAUDE / name, claude / name)

    if consume_durable_mismatch:
        pm_log = root / ".project_manager" / "tools" / "pm_log.py"
        text = pm_log.read_text(encoding="utf-8")
        anchor = (
            '    current = Path(pm_home) / ".project_manager" / "wiki" / "log" / "current.md"\n'
        )
        mutation = (
            anchor
            + '    consumed = current.with_name(".t0661-mismatch-consumed")\n'
            + "    if consumed.exists():\n"
            + "        return None\n"
            + '    consumed.write_text("consumed\\n", encoding="utf-8")\n'
        )
        assert text.count(anchor) == 1
        pm_log.write_text(text.replace(anchor, mutation), encoding="utf-8")

    if read_failure_as_none:
        pm_log = root / ".project_manager" / "tools" / "pm_log.py"
        text = pm_log.read_text(encoding="utf-8")
        mutation = "        return _CTX_WINDOW_MISMATCH_READ_FAILED\n"
        assert text.count(mutation) == 1
        pm_log.write_text(text.replace(mutation, "        return None\n"), encoding="utf-8")

    if drop_append_fallback:
        hook = claude / "ctx_stop_hook.py"
        text = hook.read_text(encoding="utf-8")
        fallback = "    if auto_compact and window_tokens is not None and not band_fired and not checkpointed:\n"
        assert text.count(fallback) == 1
        hook.write_text(
            text.replace(fallback, "    if False and auto_compact and window_tokens is not None and not band_fired and not checkpointed:\n"),
            encoding="utf-8",
        )

    manager = root / ".project_manager"
    log_dir = manager / "wiki" / "log"
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    write_lf(current, "# Project Log\n\n> T-0661 subprocess probe\n\n")
    write_lf(manager / "local.conf", "harness.claude.ctx_window_tokens=600000\n")
    state = manager / ".local" / "tasks" / "main" / "pm_state.md"
    state.parent.mkdir(parents=True)
    write_lf(state, "# main state\n- T-0661 probe\n")
    cwd = root / "work" / "project_1"
    cwd.mkdir(parents=True)
    write_lf(manager / ".local" / "worktree-leases.json",
        json.dumps({
            "leases": [{
                "slot": "work/project_1", "state": "leased", "session": "main",
            }],
        }),
    )

    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(root, {"harness.claude.ctx_window_tokens": "600000"})
    def run_hook(payload: dict, *args: str) -> dict | None:
        result = subprocess.run(
            [sys.executable, str(claude / "ctx_stop_hook.py"), *args],
            cwd=root,
            input=json.dumps(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=utf8_child_env(),
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if result.stdout else None

    marker_dir = manager / ".local" / "ctx-stop"
    transcript = root / "missed.jsonl"
    write_lf(transcript, json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "usage": {"input_tokens": 30_000}},
        "isSidechain": False,
    }) + "\n")
    base = {
        "session_id": "probe-missed", "transcript_path": str(transcript), "cwd": str(cwd),
    }
    if failure == "precompact-readonly":
        current.chmod(0o444)
    assert run_hook(
        base | {"hook_event_name": "PreCompact", "trigger": "auto"},
        "--precompact-capture",
    ) is None
    _append_transcript_entry(transcript, _compact_boundary())
    post = base | {"hook_event_name": "PostCompact"}
    snapshot_marker = marker_dir / "compact-snapshot.probe-missed"

    assert run_hook(post) is None
    first = snapshot_marker.read_bytes()

    if failure == "ledger-read":
        saved_log = current.with_name("current.saved.md")
        current.rename(saved_log)
        current.mkdir()
        assert run_hook(post) is None
        duplicate_unconsumed = snapshot_marker.read_bytes()
        return {
            "first": first,
            "duplicate_unconsumed": duplicate_unconsumed,
            "log": saved_log.read_bytes(),
        }

    if failure == "precompact-readonly":
        current.chmod(0o644)
        return {
            "snapshot": first,
            "marker": snapshot_marker.exists(),
            "log": current.read_bytes(),
        }

    if failure == "marker-write":
        consumed_output = run_hook(base | {"hook_event_name": "UserPromptSubmit"})
        assert consumed_output is not None and not snapshot_marker.exists()
        consumed = consumed_output["hookSpecificOutput"]["additionalContext"].encode("utf-8")
        marker_dir.chmod(0o555)
        assert run_hook(post) is None
        marker_during_failure = snapshot_marker.exists()
        prompt_during_failure = run_hook(base | {"hook_event_name": "UserPromptSubmit"})
        durable_log = current.read_bytes()
        marker_dir.chmod(0o755)
        assert run_hook(post) is None
        marker_after_recovery = snapshot_marker.exists()
        recovered_output = run_hook(base | {"hook_event_name": "UserPromptSubmit"})
        recovered = (
            recovered_output["hookSpecificOutput"]["additionalContext"].encode("utf-8")
            if recovered_output is not None else None
        )
        return {
            "first": first,
            "consumed": consumed,
            "marker_during_failure": marker_during_failure,
            "prompt_during_failure": prompt_during_failure,
            "log": durable_log,
            "marker_after_recovery": marker_after_recovery,
            "recovered": recovered,
        }

    assert run_hook(post) is None
    duplicate_unconsumed = snapshot_marker.read_bytes()

    consumed_output = run_hook(base | {"hook_event_name": "UserPromptSubmit"})
    assert consumed_output is not None
    consumed = consumed_output["hookSpecificOutput"]["additionalContext"].encode("utf-8")
    marker_after_take = snapshot_marker.exists()
    assert run_hook(post) is None
    after_consume_refire = snapshot_marker.read_bytes()

    clean_transcript = root / "clean.jsonl"
    write_lf(clean_transcript, json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "usage": {"input_tokens": 30_000}},
        "isSidechain": False,
    }) + "\n")
    clean_sid = "probe-clean"
    (marker_dir / f"{clean_sid}.final").write_text("fired\n", encoding="utf-8")
    clean_base = {
        "session_id": clean_sid,
        "transcript_path": str(clean_transcript),
        "cwd": str(cwd),
    }
    assert run_hook(
        clean_base | {"hook_event_name": "PreCompact", "trigger": "auto"},
        "--precompact-capture",
    ) is None
    _append_transcript_entry(clean_transcript, _compact_boundary())
    assert run_hook(clean_base | {"hook_event_name": "PostCompact"}) is None
    clean = (marker_dir / f"compact-snapshot.{clean_sid}").read_bytes()

    return {
        "first": first,
        "duplicate_unconsumed": duplicate_unconsumed,
        "consumed": consumed,
        "marker_after_take": marker_after_take,
        "after_consume_refire": after_consume_refire,
        "clean": clean,
        "log": current.read_bytes(),
    }


def _assert_postcompact_durable_mismatch_matrix(result: dict) -> None:
    """marker끼리는 바이트 동일성, marker↔전달 payload는 내용 동일성으로 본다(T-0708).

    marker(`first`·`duplicate_unconsumed`·`after_consume_refire`)는 모두 같은 디스크 읽기라 바이트
    그대로 대조한다. `consumed`는 훅이 marker를 universal-newline으로 읽어 전달한 payload라 CRLF
    플랫폼에서 표기가 다르다 — 층을 맞춰 내용으로 판정하고, 내용 1자 drift는 여전히 red다.
    """
    diagnostic = b"[ctx-window-mismatch]"
    assert result["first"].count(diagnostic) == 1
    assert result["duplicate_unconsumed"].count(diagnostic) == 1, (
        "duplicate-unconsumed PostCompact lost durable ctx-window-mismatch"
    )
    assert result["duplicate_unconsumed"] == result["first"]
    assert normalize_newline_bytes(result["consumed"]) == normalize_newline_bytes(
        result["first"]
    ), "consumed payload content diverged from the armed snapshot"
    assert result["marker_after_take"] is False
    # 소비 후 같은 경계 재발화는 현행대로 다시 무장한다. 단일 진실이 log라 payload/log 모두 누적 0.
    assert result["after_consume_refire"] == result["first"]
    assert result["after_consume_refire"].count(diagnostic) == 1
    assert result["clean"].count(diagnostic) == 0
    assert result["log"].count(diagnostic) == 1


def test_postcompact_duplicate_preserves_durable_mismatch_snapshot_subprocess(tmp_path):
    """미소비 중복 payload는 byte-identical·진단 보존, 소비 후 재발화는 누적 없이 재무장."""
    _assert_postcompact_durable_mismatch_matrix(
        _postcompact_durable_mismatch_subprocess_probe(tmp_path),
    )


def test_postcompact_durable_mismatch_regression_detects_consuming_source(tmp_path):
    """진단 원천을 소비성으로 바꾼 임시 실-engine 사본이면 핵심 불변식 단언이 red다."""
    result = _postcompact_durable_mismatch_subprocess_probe(
        tmp_path, consume_durable_mismatch=True,
    )
    diagnostic = b"[ctx-window-mismatch]"
    assert result["first"].count(diagnostic) == 1
    assert result["duplicate_unconsumed"].count(diagnostic) == 0
    with pytest.raises(AssertionError, match="duplicate-unconsumed"):
        _assert_postcompact_durable_mismatch_matrix(result)


# ── snapshot 표기 축(T-0708) ────────────────────────────────────────────────────
# 디스크 marker는 훅의 텍스트 쓰기라 플랫폼 개행을 따르고(Windows=CRLF), 전달 payload는 훅의
# universal-newline 읽기라 항상 LF다. 두 값을 원본 bytes로 맞대면 CRLF 플랫폼에서 내용이 같아도
# 항상 불일치한다. 아래 합성 결과가 그 형상을 LF 환경에서도 재현한다.

_MISMATCH_SNAPSHOT_LF = (
    "## PM 정체성 (compaction 복구)\n"
    "- task: main\n"
    "- [ctx-window-mismatch] 관측 window가 설정과 다르다\n"
)


def _durable_mismatch_matrix_result(marker: bytes, delivered: bytes) -> dict:
    """실 probe 산출과 같은 형태의 합성 결과 — 디스크 marker bytes와 전달 payload를 따로 준다."""
    return {
        "first": marker,
        "duplicate_unconsumed": marker,
        "consumed": delivered,
        "marker_after_take": False,
        "after_consume_refire": marker,
        "clean": "## PM 정체성 (compaction 복구)\n- task: main\n".encode("utf-8"),
        "log": "## [2026-08-17] checkpoint — [ctx-window-mismatch] 관측 window\n".encode("utf-8"),
    }


def test_durable_mismatch_matrix_accepts_crlf_marker_with_lf_delivery():
    """T-0708 — 디스크 marker가 CRLF여도 전달 payload 내용이 같으면 불변식은 green이다."""
    marker = _MISMATCH_SNAPSHOT_LF.replace("\n", "\r\n").encode("utf-8")
    delivered = _MISMATCH_SNAPSHOT_LF.encode("utf-8")
    assert marker != delivered, "픽스처가 표기 차이를 못 만들었다"
    _assert_postcompact_durable_mismatch_matrix(
        _durable_mismatch_matrix_result(marker, delivered),
    )


def test_durable_mismatch_matrix_detects_single_character_delivery_drift():
    """개행 정규화가 전달 payload의 실 내용 1자 drift를 가리지 않는다 — 여전히 red다."""
    marker = _MISMATCH_SNAPSHOT_LF.replace("\n", "\r\n").encode("utf-8")
    delivered = _MISMATCH_SNAPSHOT_LF.replace("task: main", "task: main2").encode("utf-8")
    with pytest.raises(AssertionError, match="consumed payload"):
        _assert_postcompact_durable_mismatch_matrix(
            _durable_mismatch_matrix_result(marker, delivered),
        )


def _assert_ledger_read_failure_preserves_mismatch(result: dict) -> None:
    diagnostic = b"[ctx-window-mismatch]"
    assert result["first"].count(diagnostic) == 1
    assert result["duplicate_unconsumed"].count(diagnostic) == 1, (
        "ledger-read failure replaced armed diagnostic with an untrusted snapshot"
    )
    assert result["duplicate_unconsumed"] == result["first"]


def test_postcompact_ledger_read_failure_preserves_armed_mismatch_subprocess(tmp_path):
    """원장 판독 불능은 활성 진단 없음이 아니다: 기존 payload를 byte 보존한다."""
    _assert_ledger_read_failure_preserves_mismatch(
        _postcompact_durable_mismatch_subprocess_probe(tmp_path, failure="ledger-read"),
    )


def test_ledger_read_failure_regression_detects_none_collapse(tmp_path):
    """판독 실패를 None으로 합친 임시 engine은 진단 1→0으로 실제 red가 된다."""
    result = _postcompact_durable_mismatch_subprocess_probe(
        tmp_path, failure="ledger-read", read_failure_as_none=True,
    )
    diagnostic = b"[ctx-window-mismatch]"
    assert result["first"].count(diagnostic) == 1
    assert result["duplicate_unconsumed"].count(diagnostic) == 0
    with pytest.raises(AssertionError, match="ledger-read failure"):
        _assert_ledger_read_failure_preserves_mismatch(result)


def _assert_precompact_append_failure_preserves_delivery(result: dict) -> None:
    diagnostic = b"[ctx-window-mismatch]"
    assert result["log"].count(diagnostic) == 0
    assert result["marker"] is True
    assert result["snapshot"].count(diagnostic) == 1, (
        "PreCompact append failure lost the fallback diagnostic delivery"
    )
    assert result["snapshot"].count(b"ctx-checkpoint-pending: append-failed") == 1


def test_precompact_readonly_log_arms_pending_diagnostic_subprocess(tmp_path):
    """append 불능이어도 rc0 hook은 원장 밖 pending snapshot으로 진단 1회를 보존한다."""
    _assert_precompact_append_failure_preserves_delivery(
        _postcompact_durable_mismatch_subprocess_probe(
            tmp_path, failure="precompact-readonly",
        ),
    )


def test_precompact_append_failure_regression_detects_dropped_fallback(tmp_path):
    """append 실패 fallback을 제거한 임시 hook은 snapshot 진단 0으로 실제 red가 된다."""
    result = _postcompact_durable_mismatch_subprocess_probe(
        tmp_path, failure="precompact-readonly", drop_append_fallback=True,
    )
    assert result["log"].count(b"[ctx-window-mismatch]") == 0
    assert result["snapshot"].count(b"[ctx-window-mismatch]") == 0
    with pytest.raises(AssertionError, match="fallback diagnostic delivery"):
        _assert_precompact_append_failure_preserves_delivery(result)


def _assert_marker_write_failure_retries_from_durable_log(result: dict) -> None:
    diagnostic = b"[ctx-window-mismatch]"
    assert result["first"].count(diagnostic) == 1
    assert result["consumed"].count(diagnostic) == 1
    assert result["marker_during_failure"] is False
    assert result["prompt_during_failure"] is None
    assert result["log"].count(diagnostic) == 1
    assert result["marker_after_recovery"] is True
    assert result["recovered"] is not None
    assert result["recovered"].count(diagnostic) == 1, (
        "marker write recovery lost the durable ctx-window-mismatch retry source"
    )


@pytest.mark.skipif(
    not posix_mode_supported(),
    reason="directory chmod 기반 marker 쓰기 거부는 POSIX mode 의미론 필요",
)
def test_marker_write_failure_retries_from_durable_log_subprocess(tmp_path):
    """marker 쓰기 불능은 rc0·무출력, 권한 복구 뒤 같은 원장 진단으로 전달을 재시도한다."""
    _assert_marker_write_failure_retries_from_durable_log(
        _postcompact_durable_mismatch_subprocess_probe(tmp_path, failure="marker-write"),
    )


@pytest.mark.skipif(
    not posix_mode_supported(),
    reason="directory chmod 기반 marker 쓰기 거부는 POSIX mode 의미론 필요",
)
def test_marker_write_failure_regression_detects_consumed_retry_source(tmp_path):
    """원장 진단까지 소비하는 임시 engine은 marker 권한 복구 뒤 진단 전달이 red다."""
    result = _postcompact_durable_mismatch_subprocess_probe(
        tmp_path, failure="marker-write", consume_durable_mismatch=True,
    )
    assert result["log"].count(b"[ctx-window-mismatch]") == 1
    assert result["recovered"] is not None
    assert result["recovered"].count(b"[ctx-window-mismatch]") == 0
    with pytest.raises(AssertionError, match="marker write recovery"):
        _assert_marker_write_failure_retries_from_durable_log(result)


def _worktree_pm_home_checkpoint_probe(tmp_path: Path, canonical_mode: str) -> dict:
    """신형 worktree hook/engine에서 별도 PM-home canonical engine까지 실제 subprocess로 잇는다."""
    pm_home = tmp_path / "pm-home"
    worktree = pm_home / "work" / "product_1"
    worktree.mkdir(parents=True)
    # 슬롯이 소유 PM 홈을 선언하는 `.git` 포인터(worktree_pool 실 형상). 소유 판정의 유일한
    # 입력이라 이 선언이 없으면 이 트리는 아무의 worktree 도 아니다.
    slot_git_dir = pm_home / ".repos" / "product.git" / "worktrees" / "product_1"
    slot_git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {slot_git_dir}\n", encoding="utf-8")
    (slot_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    shutil.copytree(
        REPO / ".project_manager" / "tools",
        worktree / ".project_manager" / "tools",
    )
    claude = worktree / ".claude"
    claude.mkdir()
    for name in ("ctx_guard.py", "ctx_stop_hook.py"):
        shutil.copy2(CLAUDE / name, claude / name)

    canonical_tools = pm_home / ".project_manager" / "tools"
    if canonical_mode == "old-engine":
        canonical_tools.mkdir(parents=True)
        (canonical_tools / "pm_log.py").write_text(
            """#!/usr/bin/env python3
import pathlib
import sys

NEW_OPTIONS = {
    "--ctx-band-checked", "--ctx-band-missed", "--ctx-window-tokens",
    "--ctx-observed-tokens", "--harness",
}
args = sys.argv[1:]
if any(token in NEW_OPTIONS for token in args):
    raise SystemExit(2)
root = pathlib.Path(__file__).resolve().parents[2]
current = root / ".project_manager" / "wiki" / "log" / "current.md"
if args and args[0] == "checkpoint":
    with current.open("a", encoding="utf-8") as stream:
        stream.write(
            "## [2026-08-12] checkpoint | (task:main) — compaction\\n\\n"
            "- 구간: <legacy checkpoint>\\n- 서사: <PM 손>\\n"
        )
elif args and args[0] == "snapshot":
    sys.stdout.write("## PM 정체성 (compaction 복구)\\n- task: main\\n")
""",
            encoding="utf-8",
        )
    else:
        assert canonical_mode in {"normal", "write-failure"}
        shutil.copytree(REPO / ".project_manager" / "tools", canonical_tools)

    manager = pm_home / ".project_manager"
    current = manager / "wiki" / "log" / "current.md"
    current.parent.mkdir(parents=True)
    current.write_text("# Project Log\n\n> worktree→PM-home probe\n\n", encoding="utf-8")
    state = manager / ".local" / "tasks" / "main" / "pm_state.md"
    state.parent.mkdir(parents=True)
    state.write_text("# main state\n- forwarding probe\n", encoding="utf-8")
    (manager / ".local" / "worktree-leases.json").write_text(
        json.dumps({
            "leases": [{
                "slot": "work/product_1", "state": "leased", "session": "main",
            }],
        }),
        encoding="utf-8",
    )
    (worktree / ".project_manager" / "local.conf").write_text(
        "harness.claude.ctx_window_tokens=600000\n", encoding="utf-8",
    )
    transcript = worktree / "transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "usage": {"input_tokens": 30_000}},
        "isSidechain": False,
    }) + "\n", encoding="utf-8")

    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(worktree, {"harness.claude.ctx_window_tokens": "600000"})
    def run_hook(payload: dict, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(claude / "ctx_stop_hook.py"), *args],
            cwd=worktree,
            input=json.dumps(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=utf8_child_env(),
            timeout=15,
            check=False,
        )

    if canonical_mode == "write-failure":
        current.chmod(0o444)
    base = {
        "session_id": "forwarded-session",
        "transcript_path": str(transcript),
        "cwd": str(worktree),
    }
    pre = run_hook(
        base | {"hook_event_name": "PreCompact", "trigger": "auto"},
        "--precompact-capture",
    )
    snapshot_marker = (
        worktree / ".project_manager" / ".local" / "ctx-stop"
        / "compact-snapshot.forwarded-session"
    )
    fallback_armed = snapshot_marker.is_file()
    fallback = snapshot_marker.read_bytes() if fallback_armed else b""

    _append_transcript_entry(transcript, _compact_boundary())
    post = run_hook(base | {"hook_event_name": "PostCompact"})
    preserved = snapshot_marker.read_bytes() if snapshot_marker.is_file() else b""
    ledger = current.read_bytes()
    if canonical_mode == "write-failure":
        current.chmod(0o644)
    return {
        "pre_rc": pre.returncode,
        "post_rc": post.returncode,
        "pre_stderr": pre.stderr,
        "fallback_armed": fallback_armed,
        "fallback": fallback,
        "preserved": preserved,
        "ledger": ledger,
    }


@pytest.mark.parametrize(
    ("canonical_mode", "expected_fallback", "expected_ledger_diagnostic"),
    [
        pytest.param(
            "write-failure", True, 0,
            marks=pytest.mark.skipif(
                not posix_mode_supported(),
                reason="chmod 기반 쓰기 거부 시뮬레이션은 POSIX mode 의미론 필요",
            ),
        ),
        ("old-engine", True, 0),
        ("normal", False, 1),
    ],
)
def test_worktree_pm_home_diagnostic_append_signal_preserves_never_block(
    tmp_path, canonical_mode, expected_fallback, expected_ledger_diagnostic,
):
    """신형 worktree→쓰기 실패/구형/정상 PM-home의 rc0·fallback·진단 보존 3셀."""
    result = _worktree_pm_home_checkpoint_probe(tmp_path, canonical_mode)
    diagnostic = b"[ctx-window-mismatch]"
    assert result["pre_rc"] == result["post_rc"] == 0
    assert result["fallback_armed"] is expected_fallback
    assert result["fallback"].count(diagnostic) == (1 if expected_fallback else 0)
    assert result["ledger"].count(diagnostic) == expected_ledger_diagnostic
    assert result["preserved"].count(diagnostic) == 1
    if expected_fallback:
        assert "pending 진단 payload로 보존" in result["pre_stderr"]
    if canonical_mode == "old-engine":
        assert result["ledger"].count(b"legacy checkpoint") == 2


def test_hook_postcompact_waits_for_postboundary_usage_before_refiring(stop_hook, tmp_path):
    """compact 직후 첫 prompt는 old usage로 marker를 재생성하지 않고 새 usage를 기다린다."""
    sid = "sess-postcompact-boundary"
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
    rc2, compacted = stop_hook.evaluate({
        "transcript_path": str(transcript),
        "session_id": sid,
        "hook_event_name": "PostCompact",
    }, tmp_path, {})
    assert rc2 == 0 and compacted is None
    assert not any(marker.exists() for marker in markers)

    rc3, first_prompt = stop_hook.evaluate(prompt, tmp_path, {})
    assert rc3 == 0 and first_prompt is None
    assert not any(marker.exists() for marker in markers)

    _append_transcript_entry(transcript, {
        "type": "assistant",
        "message": {"role": "assistant", "usage": {"input_tokens": 190_000}},
    })
    rc4, next_high = stop_hook.evaluate(prompt, tmp_path, {})
    assert rc4 == 0 and next_high is not None
    assert markers[-1].exists()


def test_hook_precompact_does_not_rearm_before_compaction_completes(stop_hook, tmp_path):
    """압축 전 발화하는 PreCompact는 실패/차단 가능성이 있으므로 현재 marker를 보존한다."""
    sid = "sess-precompact-no-rearm"
    high = str(_write_transcript(tmp_path, [("assistant", {"input_tokens": 150_000})]))
    prompt = {
        "transcript_path": high,
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
    }

    rc1, first = stop_hook.evaluate(prompt, tmp_path, {})
    rc2, before_compaction = stop_hook.evaluate({
        "transcript_path": high,
        "session_id": sid,
        "hook_event_name": "PreCompact",
    }, tmp_path, {})
    rc3, still_same_cycle = stop_hook.evaluate(prompt, tmp_path, {})

    marker = tmp_path / ".project_manager" / ".local" / "ctx-stop" / f"{sid}.nudge"
    assert rc1 == rc2 == rc3 == 0
    assert first is not None
    assert before_compaction is None and still_same_cycle is None
    assert marker.exists()


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


# ── 8. 서브에이전트(sidechain) 감지 + 면제 (메인 세션만 checkpoint 넛지) ─────
# claude 는 서브에이전트(Task) 대화를 <parent>/subagents/agent-*.jsonl 에 전 엔트리 isSidechain:true
# 로 기록하고, 메인 세션 <session>.jsonl 은 전 엔트리 false (실측 확인). 훅은 transcript_path 로 이
# 필드를 읽어 서브에이전트면 면제(통과·auto-compact 로 자체 정리)·메인이면 넛지한다. 감지
# 불능/모호(신호 부재·읽기 실패·비-boolean)는 **메인 취급**(보수적 — 면제는 확실할 때만).

def test_transcript_is_sidechain_true(guard, tmp_path):
    path = _write_transcript(
        tmp_path, [("user", None), ("assistant", {"input_tokens": 190_000})], sidechain=True)
    assert guard.transcript_is_sidechain(path) is True


def test_transcript_is_sidechain_false(guard, tmp_path):
    path = _write_transcript(
        tmp_path, [("user", None), ("assistant", {"input_tokens": 190_000})], sidechain=False)
    assert guard.transcript_is_sidechain(path) is False


def test_transcript_is_sidechain_no_field_is_false(guard, tmp_path):
    # isSidechain 필드 부재(신호 없음) → False(메인 취급·보수적). 기존 transcript 픽스처가 이 형태.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    assert guard.transcript_is_sidechain(path) is False


def test_transcript_is_sidechain_missing_file_is_false(guard, tmp_path):
    assert guard.transcript_is_sidechain(tmp_path / "nope.jsonl") is False


def test_transcript_is_sidechain_malformed_lines_skipped(guard, tmp_path):
    # 깨진 줄은 건너뛰고 유효 엔트리의 isSidechain 을 읽는다 (transcript 꼬리 부분 파손 견고성).
    path = tmp_path / "t.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"type": "user", "isSidechain": True, "message": {}}) + "\n"
        + "{broken\n",
        encoding="utf-8",
    )
    assert guard.transcript_is_sidechain(path) is True


def test_transcript_is_sidechain_skips_trailing_fieldless_entries(guard, tmp_path):
    # 파일 끝에서부터 첫 isSidechain boolean 을 찾는다 — isSidechain 없는 후행 엔트리(system·
    # file-history-snapshot 등·실 transcript 에 섞임)는 건너뛰고 최근 boolean 을 쓴다.
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"type": "user", "isSidechain": True, "message": {}}) + "\n"
        + json.dumps({"type": "system", "content": "no isSidechain"}) + "\n"
        + json.dumps({"type": "file-history-snapshot"}) + "\n",
        encoding="utf-8",
    )
    assert guard.transcript_is_sidechain(path) is True


def test_transcript_is_sidechain_non_bool_ignored(guard, tmp_path):
    # isSidechain 이 boolean 이 아니면(문자열 "true" 등) 신호로 안 쓴다 → 유효 boolean 없으면 False
    # (보수적 — 모호하면 메인 취급).
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"type": "user", "isSidechain": "true", "message": {}}) + "\n",
        encoding="utf-8",
    )
    assert guard.transcript_is_sidechain(path) is False


def test_hook_sidechain_exempt_at_final_band(stop_hook, tmp_path):
    # 서브에이전트 + stop 밴드 UserPromptSubmit → checkpoint 최종 넛지 없이 통과한다.
    path = _write_transcript(
        tmp_path, [("assistant", {"input_tokens": 190_000})], sidechain=True)
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-sub",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    assert not (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-sub.final").exists()


def test_hook_sidechain_exempt_at_nudge_band(stop_hook, tmp_path):
    # nudge 밴드(used 75)에서도 서브에이전트는 주입 없이 통과 (면제는 밴드 무관·nudge 도 안 함).
    path = _write_transcript(
        tmp_path, [("assistant", {"input_tokens": 150_000})], sidechain=True)
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-sub-nudge",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0 and output is None
    ctx = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert not (ctx / "sess-sub-nudge.nudge").exists()


def test_hook_main_session_gets_final_nudge(stop_hook, tmp_path):
    # 회귀 대칭: isSidechain:false(메인) + stop 밴드 → 최종 넛지 + `.final` marker.
    path = _write_transcript(
        tmp_path, [("assistant", {"input_tokens": 190_000})], sidechain=False)
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-main",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert "ctx-nudge/최종" in output["hookSpecificOutput"]["additionalContext"]
    assert (tmp_path / ".project_manager" / ".local" / "ctx-stop" / "sess-main.final").exists()


def test_hook_no_sidechain_field_treated_as_main(stop_hook, tmp_path):
    # isSidechain 필드 부재(신호 없음) → 메인 취급(보수적) → 최종 넛지.
    path = _write_transcript(tmp_path, [("assistant", {"input_tokens": 190_000})])
    stdin = {
        "transcript_path": str(path),
        "session_id": "sess-nofield",
        "hook_event_name": "UserPromptSubmit",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert "ctx-nudge/최종" in output["hookSpecificOutput"]["additionalContext"]


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
