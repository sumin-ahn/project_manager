"""출하 PreCompact 훅 breadcrumb + fail-soft smoke (T-0621).

훅은 네이티브 압축이 수동 /pm-handoff 보다 먼저 터질 때 PM 홈 log/current.md 에 breadcrumb와
checkpoint를 남긴다. 셸은 현재 worktree log를 직접 만지지 않고 Python의 sidechain·PM-home 판정 뒤
엔진 append를 호출한다. 계약:
  ① log/current.md 부재 → exit 0 (graceful skip·log 미생성).
  ② log/current.md 존재 → exit 0 + breadcrumb/checkpoint append.
  ③ 항상 exit 0 (압축/세션 절대 무차단).
  ④ sidechain → 둘 다 skip, 등록 worktree → PM 홈에만 기록.

전부 hermetic — `subprocess.run(["sh", hook_path])` 를 tmp 디렉토리에서만 실행(실 repo 무오염).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "templates" / "claude_code" / ".claude" / "precompact_capture_hook.sh"
CLAUDE = HOOK.parent
ROOT_CLAUDE = REPO / ".claude"
TOOLS = REPO / ".project_manager" / "tools"

# breadcrumb 이 남기는 마커 문구(네이티브 압축 발생 신호).
BREADCRUMB_MARKER = "auto-compact 발생 — 압축은 자동이고 세션은 그대로 이어진다"

# 훅이 sh 없으면 돌릴 수 없다 — 그런 환경(드묾)은 skip(hermetic·crash 금지).
_SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(_SH is None, reason="sh 미설치 — POSIX 훅 smoke skip")


def _make_repo(tmp_path: Path, *, with_log: bool, source_claude: Path = CLAUDE) -> Path:
    """tmp 에 실제 Python 판정까지 실행할 최소 adapter/engine 구조를 만든다.

    훅은 자기 위치(.claude/)에서 repo_root 를 자기해소하므로 이 tmp 루트가 곧 repo_root(실 repo 무오염).
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    hook_copy = claude_dir / "precompact_capture_hook.sh"
    hook_copy.write_text(
        (source_claude / "precompact_capture_hook.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    hook_copy.chmod(0o755)
    for name in ("ctx_stop_hook.py", "ctx_guard.py"):
        shutil.copy2(source_claude / name, claude_dir / name)
    shutil.copytree(TOOLS, tmp_path / ".project_manager" / "tools")
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "# test root anchor\n", encoding="utf-8",
    )
    state = tmp_path / ".project_manager" / ".local" / "tasks" / "main" / "pm_state.md"
    state.parent.mkdir(parents=True)
    state.write_text("# main\n", encoding="utf-8")

    if with_log:
        log_dir = tmp_path / ".project_manager" / "wiki" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "current.md").write_text(
            "## [2026-01-01] handoff | 기존 entry\n", encoding="utf-8"
        )

    return hook_copy


def _run_hook(hook_path: Path, payload: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_SH, str(hook_path)], input=json.dumps(payload or {}),
        capture_output=True, text=True, timeout=30,
    )


def _run_python_hook(hook_path: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(hook_path.parent / "ctx_stop_hook.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )


def _write_usage_transcript(root: Path, tokens: int) -> Path:
    transcript = root / "transcript.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "usage": {"input_tokens": tokens}},
            "isSidechain": False,
        }) + "\n",
        encoding="utf-8",
    )
    return transcript


# ── (전제) 실 훅 파일 존재 ────────────────────────────────────────────────────

def test_hook_file_present():
    """실 훅 파일이 존재한다 — smoke 가 무의미해지지 않게."""
    assert HOOK.exists(), f"precompact 훅 없음: {HOOK}"


def test_root_hook_chain_is_byte_identical_and_executes_real_root_copies(tmp_path):
    """도그푸딩 root의 실제 3파일을 복사 실행해 breadcrumb/checkpoint 체인을 끝까지 검증한다."""
    for name in ("precompact_capture_hook.sh", "ctx_stop_hook.py", "ctx_guard.py"):
        root_copy = ROOT_CLAUDE / name
        template_copy = CLAUDE / name
        assert root_copy.is_file(), f"root 훅 체인 파일 없음: {root_copy}"
        assert root_copy.read_bytes() == template_copy.read_bytes(), (
            f"root 훅 체인 canonical drift: {root_copy} != {template_copy}"
        )

    hook = _make_repo(tmp_path, with_log=True, source_claude=ROOT_CLAUDE)
    result = _run_hook(hook, {"session_id": "root-dogfood"})

    assert result.returncode == 0
    text = (tmp_path / ".project_manager" / "wiki" / "log" / "current.md").read_text(
        encoding="utf-8"
    )
    assert BREADCRUMB_MARKER in text
    assert "checkpoint | (task:main) — compaction" in text


# ── ① log 부재 → graceful skip (exit 0·파일 미생성) ───────────────────────────

def test_graceful_skip_when_log_absent(tmp_path):
    """log/current.md 가 없는 트리(어댑터 미배선 등)에서 훅은 exit 0·파일 미생성."""
    hook = _make_repo(tmp_path, with_log=False)
    result = _run_hook(hook)
    assert result.returncode == 0, (
        f"log 부재 시 graceful skip 위반 (exit {result.returncode}): {result.stderr}"
    )
    assert not (tmp_path / ".project_manager" / "wiki" / "log" / "current.md").exists()


# ── ② log 존재 → exit 0 + breadcrumb 1줄 append ──────────────────────────────

def test_breadcrumb_appended_when_log_present(tmp_path):
    """log/current.md 존재 시 Python 판정 뒤 breadcrumb와 checkpoint를 한 경계로 append한다.

    기존 entry는 보존되고 breadcrumb 자체는 blockquote이며 새 header는 checkpoint 하나뿐이다.
    """
    hook = _make_repo(tmp_path, with_log=True)
    result = _run_hook(hook)
    assert result.returncode == 0, f"exit 0 위반 (exit {result.returncode}): {result.stderr}"

    log_file = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    text = log_file.read_text(encoding="utf-8")
    assert BREADCRUMB_MARKER in text, f"breadcrumb 미append: {text!r}"
    assert "기존 entry" in text, "기존 entry 유실 (append-only 위반)"
    after = text.split("기존 entry", 1)[1]
    assert after.count("\n## ") == 1
    assert "checkpoint | (task:main) — compaction" in after


def test_band_missed_precompact_durably_records_remedy_and_reinjects_after_compaction(
    tmp_path,
):
    """설정 창 때문에 밴드가 0회면 log 박제 + PostCompact 복구 context 모두 loud하다."""
    hook = _make_repo(tmp_path, with_log=True)
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "ctx_window_tokens_claude=1000000\n", encoding="utf-8",
    )
    transcript = _write_usage_transcript(tmp_path, 655_736)
    payload = {
        "cwd": str(tmp_path),
        "session_id": "mismatch-session",
        "transcript_path": str(transcript),
        "trigger": "auto",
    }

    pre = _run_hook(hook, payload)

    assert pre.returncode == 0
    log_file = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    text = log_file.read_text(encoding="utf-8")
    assert "[ctx-window-mismatch] 설정 창이 실 압축 지점보다 큼" in text
    assert "설정 1,000,000 tokens" in text
    assert "PreCompact 관측 655,736 tokens" in text
    assert "`ctx_window_tokens_claude`" in text
    assert "관측 사용량 655,736 tokens 이하" in text

    duplicate_pre = _run_hook(hook, payload)
    assert duplicate_pre.returncode == 0
    duplicate_text = log_file.read_text(encoding="utf-8")
    assert duplicate_text.count("[ctx-window-mismatch]") == 2
    assert duplicate_text.count("관측 사용량 655,736 tokens 이하") == 2

    post = _run_python_hook(hook, payload | {"hook_event_name": "PostCompact"})
    restored = _run_python_hook(hook, payload | {"hook_event_name": "UserPromptSubmit"})

    assert post.returncode == restored.returncode == 0
    restored_payload = json.loads(restored.stdout)
    recovery = restored_payload["hookSpecificOutput"]["additionalContext"]
    assert "## ctx 설정 진단 (compaction 경계)" in recovery
    assert "[ctx-window-mismatch] 설정 창이 실 압축 지점보다 큼" in recovery
    assert "관측 사용량 655,736 tokens 이하" in recovery


def test_duplicate_postcompact_preserves_unconsumed_window_mismatch_recovery(tmp_path):
    """같은 완료 경계의 중복 훅이 아직 미소비인 loud 진단 snapshot을 덮지 않는다."""
    hook = _make_repo(tmp_path, with_log=True)
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "ctx_window_tokens_claude=1000000\n", encoding="utf-8",
    )
    transcript = _write_usage_transcript(tmp_path, 655_736)
    payload = {
        "cwd": str(tmp_path),
        "session_id": "duplicate-post-session",
        "transcript_path": str(transcript),
        "trigger": "auto",
    }

    assert _run_hook(hook, payload).returncode == 0
    posts = [
        _run_python_hook(hook, payload | {"hook_event_name": "PostCompact"})
        for _ in range(2)
    ]
    restored = _run_python_hook(hook, payload | {"hook_event_name": "UserPromptSubmit"})

    assert all(post.returncode == 0 for post in posts)
    assert all(post.stdout == post.stderr == "" for post in posts)
    assert restored.returncode == 0
    recovery = json.loads(restored.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "## ctx 설정 진단 (compaction 경계)" in recovery
    assert "[ctx-window-mismatch] 설정 창이 실 압축 지점보다 큼" in recovery
    assert "관측 사용량 655,736 tokens 이하" in recovery


def test_duplicate_postcompact_without_transcript_preserves_unconsumed_mismatch(
    tmp_path,
):
    """관측 transcript가 없어도 같은 완료 경계의 중복 훅은 loud payload를 보존한다."""
    hook = _make_repo(tmp_path, with_log=True)
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "ctx_window_tokens_claude=1000000\n", encoding="utf-8",
    )
    payload = {
        "cwd": str(tmp_path),
        "session_id": "no-transcript-duplicate-post",
        "trigger": "auto",
    }

    assert _run_hook(hook, payload).returncode == 0
    posts = [
        _run_python_hook(hook, payload | {"hook_event_name": "PostCompact"})
        for _ in range(2)
    ]
    restored = _run_python_hook(
        hook, payload | {"hook_event_name": "UserPromptSubmit"},
    )

    assert all(post.returncode == 0 for post in posts)
    assert all(post.stdout == post.stderr == "" for post in posts)
    assert restored.returncode == 0
    recovery = json.loads(restored.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "## ctx 설정 진단 (compaction 경계)" in recovery
    assert "[ctx-window-mismatch] 설정 창이 실 압축 지점보다 큼" in recovery
    assert "관측 사용량 측정 불가" not in recovery
    assert "PreCompact 관측" not in recovery
    assert "실 auto-compact 지점 이하" in recovery
    state_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert list(state_dir.glob("ctx-window-mismatch.*")) == []
    assert list(state_dir.glob("compact-snapshot-receipt.*")) == []


def test_precompact_retries_diagnostic_after_prior_process_died_post_claim(tmp_path):
    """선점 직후 죽은 흔적이 있어도 재실행은 조건을 다시 평가해 진단을 append한다."""
    hook = _make_repo(tmp_path, with_log=True)
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "ctx_window_tokens_claude=1000000\n", encoding="utf-8",
    )
    transcript = _write_usage_transcript(tmp_path, 655_736)
    session_id = "crash-retry-session"
    boundary_id = f"claude-{session_id}-1"
    stale_claim = (
        tmp_path / ".project_manager" / ".local" / "ctx-stop"
        / f"compact-checkpoint.{session_id}.{boundary_id}"
    )
    stale_claim.parent.mkdir(parents=True, exist_ok=True)
    stale_claim.write_text("compaction checkpoint claimed\nphase=pre\n", encoding="utf-8")
    payload = {
        "cwd": str(tmp_path),
        "session_id": session_id,
        "transcript_path": str(transcript),
        "trigger": "auto",
    }

    rerun = _run_hook(hook, payload)

    assert rerun.returncode == 0
    current = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    assert "[ctx-window-mismatch]" in current.read_text(encoding="utf-8")
    assert _run_python_hook(
        hook, payload | {"hook_event_name": "PostCompact"},
    ).returncode == 0
    restored = _run_python_hook(
        hook, payload | {"hook_event_name": "UserPromptSubmit"},
    )
    recovery = json.loads(restored.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "[ctx-window-mismatch]" in recovery


@pytest.mark.parametrize("band_suffix", ("nudge", "nudge2", "final"))
def test_fired_band_precompact_has_no_window_mismatch_false_positive(
    tmp_path, band_suffix,
):
    """이번 사이클 band marker가 하나라도 실재하면 같은 압축의 불일치 진단은 0건이다."""
    hook = _make_repo(tmp_path, with_log=True)
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "ctx_window_tokens_claude=1000000\n", encoding="utf-8",
    )
    transcript = _write_usage_transcript(tmp_path, 655_736)
    marker = (
        tmp_path / ".project_manager" / ".local" / "ctx-stop"
        / f"normal-session.{band_suffix}"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ctx-nudge injected\n", encoding="utf-8")

    result = _run_hook(hook, {
        "cwd": str(tmp_path),
        "session_id": "normal-session",
        "transcript_path": str(transcript),
        "trigger": "auto",
    })

    assert result.returncode == 0
    text = (tmp_path / ".project_manager" / "wiki" / "log" / "current.md").read_text(
        encoding="utf-8",
    )
    assert "checkpoint | (task:main) — compaction" in text
    assert "ctx-window-mismatch" not in text
    assert "ctx_window_tokens_claude" not in text
    post = _run_python_hook(hook, {
        "cwd": str(tmp_path),
        "session_id": "normal-session",
        "transcript_path": str(transcript),
        "trigger": "auto",
        "hook_event_name": "PostCompact",
    })
    restored = _run_python_hook(hook, {
        "cwd": str(tmp_path),
        "session_id": "normal-session",
        "hook_event_name": "UserPromptSubmit",
    })
    assert post.returncode == restored.returncode == 0
    recovery = json.loads(restored.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "ctx-window-mismatch" not in recovery
    assert "ctx_window_tokens_claude" not in recovery


@pytest.mark.parametrize("trigger", ["manual", None, "future-trigger"])
def test_non_auto_precompact_never_persists_or_reinjects_window_mismatch(
    tmp_path, trigger,
):
    """manual /compact와 trigger 부재·미지값은 낮은 사용량이어도 durable 오탐이 0건이다."""
    hook = _make_repo(tmp_path, with_log=True)
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "ctx_window_tokens_claude=600000\n", encoding="utf-8",
    )
    transcript = _write_usage_transcript(tmp_path, 30_000)
    payload = {
        "cwd": str(tmp_path),
        "session_id": "non-auto-session",
        "transcript_path": str(transcript),
    }
    if trigger is not None:
        payload["trigger"] = trigger

    pre = _run_hook(hook, payload)
    post = _run_python_hook(hook, payload | {"hook_event_name": "PostCompact"})
    restored = _run_python_hook(hook, payload | {"hook_event_name": "UserPromptSubmit"})

    assert pre.returncode == post.returncode == restored.returncode == 0
    log_text = (
        tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    ).read_text(encoding="utf-8")
    assert "checkpoint | (task:main) — compaction" in log_text
    assert "ctx-window-mismatch" not in log_text
    recovery = json.loads(restored.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "ctx-window-mismatch" not in recovery
    assert "ctx_window_tokens_claude" not in recovery


def test_sidechain_skips_breadcrumb_and_checkpoint(tmp_path):
    """sidechain 판정이 breadcrumb보다 먼저여서 서브에이전트 압축은 메인 log를 오염시키지 않는다."""
    hook = _make_repo(tmp_path, with_log=True)
    transcript = tmp_path / "sidechain.jsonl"
    transcript.write_text('{"isSidechain":true}\n', encoding="utf-8")
    log_file = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    before = log_file.read_text(encoding="utf-8")

    result = _run_hook(hook, {"transcript_path": str(transcript), "session_id": "child-1"})

    assert result.returncode == 0
    assert log_file.read_text(encoding="utf-8") == before


def test_registered_worktree_records_only_in_pm_home(tmp_path):
    """lease 역참조가 shell의 현재 worktree보다 먼저 적용돼 PM 홈 log만 갱신된다."""
    pm_home = tmp_path / "pm-home"
    worktree = pm_home / "work" / "product_1"
    hook = _make_repo(worktree, with_log=False)
    shutil.copytree(TOOLS, pm_home / ".project_manager" / "tools")
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        '{"leases":[{"slot":"work/product_1","state":"leased","session":"main"}]}',
        encoding="utf-8",
    )
    state = pm_home / ".project_manager" / ".local" / "tasks" / "main" / "pm_state.md"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("# main\n", encoding="utf-8")
    home_log = pm_home / ".project_manager" / "wiki" / "log" / "current.md"
    home_log.parent.mkdir(parents=True)
    home_log.write_text("## [2026-01-01] handoff | PM 홈 기존 entry\n", encoding="utf-8")

    result = _run_hook(hook, {"cwd": str(worktree), "session_id": "main-session"})

    assert result.returncode == 0
    assert BREADCRUMB_MARKER in home_log.read_text(encoding="utf-8")
    assert not (worktree / ".project_manager" / "wiki" / "log" / "current.md").exists()


# ── ③ 항상 exit 0 (fail-soft) ─────────────────────────────────────────────────

def test_exit_zero_always(tmp_path):
    """log 존재/부재 무관 훅은 exit 0 — 압축/세션을 절대 막지 않는다."""
    for i, with_log in enumerate((True, False)):
        hook = _make_repo(tmp_path / f"r{i}", with_log=with_log)
        result = _run_hook(hook)
        assert result.returncode == 0, (
            f"with_log={with_log} 시 fail-soft 위반 (exit {result.returncode}): {result.stderr}"
        )
