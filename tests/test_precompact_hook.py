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


def _declare_slot_git_pointer(pm_home: Path, worktree: Path, repo: str = "product") -> None:
    """슬롯이 소유 PM 홈을 선언하는 `.git` 포인터를 세운다(`worktree_pool` 실 형상).

    공용 bare 저장소는 `<pm_home>/.repos/<repo>.git` 이고 슬롯의 `.git` 은 그 안 worktree
    gitdir 을 가리킨다. 소유 판정의 유일한 입력이라 이 선언이 없는 트리는 자기 자신이다.
    """
    git_dir = pm_home / ".repos" / f"{repo}.git" / "worktrees" / worktree.name
    git_dir.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")


def _known_budget_payload(root, payload):
    """밴드 판정의 분모가 될 압축 한도 기록을 설치한다 — 훅 입력은 그대로(PreCompact 입력에 모델은 없다)."""
    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(root, capacity=2000000)
    return payload


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


def _t0952_unbanded_auto_precompact(tmp_path: Path, session_id: str) -> tuple[Path, dict, Path]:
    """기록이 읽히고 밴드 표식이 하나도 없는 auto PreCompact 준비 — (훅, 입력, log 파일).

    변경 전 훅이 "설정 창이 실 압축 지점보다 큼" 경고를 쓰던 형상이다(설정 1,000,000 · 관측 655,736)."""
    hook = _make_repo(tmp_path, with_log=True)
    (tmp_path / ".project_manager" / "local.conf").write_text(
        "harness.claude.ctx_window_tokens=1000000\n", encoding="utf-8",
    )
    transcript = _write_usage_transcript(tmp_path, 655_736)
    payload = _known_budget_payload(tmp_path, {
        "cwd": str(tmp_path),
        "session_id": session_id,
        "transcript_path": str(transcript),
        "trigger": "auto",
    })
    band_markers = list((tmp_path / ".project_manager" / ".local" / "ctx-stop").glob(f"{session_id}.*"))
    assert band_markers == []
    return hook, payload, tmp_path / ".project_manager" / "wiki" / "log" / "current.md"


def _t0952_appended_part(log_file: Path) -> str:
    return log_file.read_text(encoding="utf-8").split("기존 entry", 1)[1]


def test_t0952_auto_precompact_without_band_records_skeleton_only(tmp_path):
    """안내 없이 난 auto 압축도 breadcrumb·checkpoint 골격만 남기고 압축 지점 추정 경고는 없다(AT-001)."""
    hook, payload, log_file = _t0952_unbanded_auto_precompact(tmp_path, "t0952-unbanded")

    pre = _run_hook(hook, payload)

    assert pre.returncode == 0
    appended = _t0952_appended_part(log_file)
    assert appended.count("\n## ") == 1
    assert appended.count("checkpoint | (task:main) — compaction") == 1
    assert appended.count(BREADCRUMB_MARKER) == 1
    for removed in ("ctx-band-check", "ctx-window-mismatch", "harness.claude.ctx_window_tokens"):
        assert removed not in appended

    post = _run_python_hook(hook, payload | {"hook_event_name": "PostCompact"})
    restored = _run_python_hook(hook, payload | {"hook_event_name": "UserPromptSubmit"})

    assert post.returncode == restored.returncode == 0
    recovery = json.loads(restored.stdout)["hookSpecificOutput"]["additionalContext"]
    assert recovery.startswith("## PM 정체성 (compaction 복구)")
    assert "ctx 설정 진단" not in recovery
    assert _t0952_appended_part(log_file).count("checkpoint | (task:main) — compaction") == 1


def test_t0952_repeated_auto_precompact_same_boundary_appends_once(tmp_path):
    """같은 경계의 auto PreCompact 재호출은 중복 방지 표식으로 1건만 남긴다(AT-002)."""
    hook, payload, log_file = _t0952_unbanded_auto_precompact(tmp_path, "t0952-repeat")

    results = [_run_hook(hook, payload) for _ in range(2)]

    assert [result.returncode for result in results] == [0, 0]
    appended = _t0952_appended_part(log_file)
    assert appended.count("checkpoint | (task:main) — compaction") == 1
    assert appended.count(BREADCRUMB_MARKER) == 1


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
    _declare_slot_git_pointer(pm_home, worktree)
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


def test_hook_never_writes_to_an_enclosing_pm_home(tmp_path):
    """감싸는 PM 홈이 있어도 훅은 자기 트리에만 기록한다 — 위치는 판정 입력이 아니다.

    2026-08-31 실측에서 실 PM 홈 `wiki/log/current.md` 에 108줄이 들어간 경로와 같은 형상이다:
    등록 슬롯 안쪽의 마커 없는 트리에서 훅을 돌리면, 조상 훑기가 살아 있을 때 바깥 PM 홈이
    소유자로 뽑혀 그쪽 log 에 append 된다.
    """
    pm_home = tmp_path / "pm-home"
    slot = pm_home / "work" / "product_1"
    slot.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, slot)
    shutil.copytree(TOOLS, pm_home / ".project_manager" / "tools")
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        '{"leases":[{"slot":"work/product_1","state":"leased","session":"main"}]}',
        encoding="utf-8",
    )
    state = pm_home / ".project_manager" / ".local" / "tasks" / "main" / "pm_state.md"
    state.parent.mkdir(parents=True)
    state.write_text("# main\n", encoding="utf-8")
    home_log = pm_home / ".project_manager" / "wiki" / "log" / "current.md"
    home_log.parent.mkdir(parents=True)
    home_log.write_text("## [2026-01-01] handoff | PM 홈 기존 entry\n", encoding="utf-8")
    home_log_before = home_log.read_bytes()

    inner = slot / ".project_manager" / ".local" / "tmp" / "anchor"
    inner.mkdir(parents=True)
    hook = _make_repo(inner, with_log=True)
    inner_log = inner / ".project_manager" / "wiki" / "log" / "current.md"

    result = _run_hook(hook, {"cwd": str(inner), "session_id": "inner-session"})

    assert result.returncode == 0
    inner_text = inner_log.read_text(encoding="utf-8")
    assert BREADCRUMB_MARKER in inner_text
    assert "checkpoint | (task:main) — compaction" in inner_text
    assert home_log.read_bytes() == home_log_before


# ── ③ 항상 exit 0 (fail-soft) ─────────────────────────────────────────────────

def test_exit_zero_always(tmp_path):
    """log 존재/부재 무관 훅은 exit 0 — 압축/세션을 절대 막지 않는다."""
    for i, with_log in enumerate((True, False)):
        hook = _make_repo(tmp_path / f"r{i}", with_log=with_log)
        result = _run_hook(hook)
        assert result.returncode == 0, (
            f"with_log={with_log} 시 fail-soft 위반 (exit {result.returncode}): {result.stderr}"
        )
