"""PreCompact 훅(`.claude/precompact_capture_hook.sh`) breadcrumb 행위 smoke (T-0089·ADR-0038 D3).

훅은 도그푸딩 root(auto-compact ON·ctx hard-stop 훅 부재) 전용 최소 breadcrumb 다 — 네이티브
압축이 수동 /pm-handoff 보다 먼저 터질 때 log/current.md 에 1줄 신호를 남긴다. 폐기된
pm_handoff.py `--trigger`(T-0186)에 비의존 — inline append·항상 exit 0(fail-soft). 계약:
  ① log/current.md 부재 → exit 0 (graceful skip·파일 미생성).
  ② log/current.md 존재 → exit 0 + breadcrumb 1줄 append (blockquote·새 `##` entry 아님).
  ③ 항상 exit 0 (압축/세션 절대 무차단).

전부 hermetic — `subprocess.run(["sh", hook_path])` 를 tmp 디렉토리에서만 실행(실 repo 무오염).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "precompact_capture_hook.sh"

# breadcrumb 이 남기는 마커 문구(네이티브 압축 발생 신호).
BREADCRUMB_MARKER = "네이티브 auto-compact 발생"

# 훅이 sh 없으면 돌릴 수 없다 — 그런 환경(드묾)은 skip(hermetic·crash 금지).
_SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(_SH is None, reason="sh 미설치 — POSIX 훅 smoke skip")


def _make_repo(tmp_path: Path, *, with_log: bool) -> Path:
    """tmp 에 훅이 기대하는 최소 repo 구조 — `.claude/` 훅 사본 + 선택적 log/current.md.

    훅은 자기 위치(.claude/)에서 repo_root 를 자기해소하므로 이 tmp 루트가 곧 repo_root(실 repo 무오염).
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    hook_copy = claude_dir / "precompact_capture_hook.sh"
    hook_copy.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    hook_copy.chmod(0o755)

    if with_log:
        log_dir = tmp_path / ".project_manager" / "wiki" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "current.md").write_text(
            "## [2026-01-01] handoff | 기존 entry\n", encoding="utf-8"
        )

    return hook_copy


def _run_hook(hook_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([_SH, str(hook_path)], capture_output=True, text=True, timeout=30)


# ── (전제) 실 훅 파일 존재 ────────────────────────────────────────────────────

def test_hook_file_present():
    """실 훅 파일이 존재한다 — smoke 가 무의미해지지 않게."""
    assert HOOK.exists(), f"precompact 훅 없음: {HOOK}"


# ── ① log 부재 → graceful skip (exit 0·파일 미생성) ───────────────────────────

def test_graceful_skip_when_log_absent(tmp_path):
    """log/current.md 가 없는 트리(어댑터 미배선 등)에서 훅은 exit 0·파일 미생성."""
    hook = _make_repo(tmp_path, with_log=False)
    result = _run_hook(hook)
    assert result.returncode == 0, (
        f"log 부재 시 graceful skip 위반 (exit {result.returncode}): {result.stderr}"
    )
    # 훅이 아무 파일도 만들지 않았다 — .claude/ 의 훅 자신만 존재.
    assert not (tmp_path / ".project_manager").exists()


# ── ② log 존재 → exit 0 + breadcrumb 1줄 append ──────────────────────────────

def test_breadcrumb_appended_when_log_present(tmp_path):
    """log/current.md 존재 시 훅이 breadcrumb 1줄을 append 하고 exit 0.

    폐기된 `--trigger` machinery 에 비의존 — 훅 자체가 inline 으로 append(pm_handoff 호출 없음).
    기존 entry 는 보존(append-only)하고, blockquote 라 새 `##` handoff entry 를 만들지 않는다
    (pm_log/pm_bootstrap 의 "마지막 entry" 파싱 무오염).
    """
    hook = _make_repo(tmp_path, with_log=True)
    result = _run_hook(hook)
    assert result.returncode == 0, f"exit 0 위반 (exit {result.returncode}): {result.stderr}"

    log_file = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    text = log_file.read_text(encoding="utf-8")
    assert BREADCRUMB_MARKER in text, f"breadcrumb 미append: {text!r}"
    assert "기존 entry" in text, "기존 entry 유실 (append-only 위반)"
    # breadcrumb 가 새 `## ` handoff entry 를 만들지 않는다 (blockquote 신호).
    after = text.split("기존 entry", 1)[1]
    assert "\n## " not in after, f"breadcrumb 가 새 entry 를 만듦(파싱 오염): {after!r}"


# ── ③ 항상 exit 0 (fail-soft) ─────────────────────────────────────────────────

def test_exit_zero_always(tmp_path):
    """log 존재/부재 무관 훅은 exit 0 — 압축/세션을 절대 막지 않는다."""
    for i, with_log in enumerate((True, False)):
        hook = _make_repo(tmp_path / f"r{i}", with_log=with_log)
        result = _run_hook(hook)
        assert result.returncode == 0, (
            f"with_log={with_log} 시 fail-soft 위반 (exit {result.returncode}): {result.stderr}"
        )
