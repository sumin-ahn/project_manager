"""PreCompact 훅(`.claude/precompact_capture_hook.sh`) 행위 smoke (T-0089).

훅은 네이티브 압축이 수동 handoff 보다 먼저 터질 때의 durable flush 폴백이다
([[ADR-0020]] pre-compact·[[T-0084]]). 계약:
  ① pm_handoff.py 부재 디렉토리 → exit 0 (graceful skip·파일 미생성).
  ② 실 repo 구조 모방(tmp 에 `.claude/`+`.project_manager/tools/pm_handoff.py` stub)
     → exit 0 + stub 이 `--trigger --reason precompact` 로 호출돼 log/current.md 에
     PreCompact marker(`precompact-flush`/`reason=precompact`) append.
  ③ stub pm_handoff 가 rc!=0(실패)여도 훅 자체는 exit 0 (`|| true` fail-soft).

전부 hermetic — `subprocess.run(["sh", hook_path])` 를 **tmp 디렉토리**에서만 실행한다.
실 repo 의 log/current.md 는 절대 건드리지 않는다(훅의 repo_root 자기해소가 tmp 를 가리킴).
sh 부재 환경(드묾)은 skip.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "precompact_capture_hook.sh"

# 훅이 호출하는 pm_handoff 의 비대화 트리거 모드가 남기는 PreCompact marker
# (pm_handoff._TRIGGER_MARKERS["precompact"] = "precompact-flush"·ADR-0020).
PRECOMPACT_MARKER = "precompact-flush"

# 훅이 sh 없으면 돌릴 수 없다 — 그런 환경(드묾)은 skip(hermetic·crash 금지).
_SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(_SH is None, reason="sh 미설치 — POSIX 훅 smoke skip")


def _make_repo(tmp_path: Path, *, with_handoff: bool, stub_body: str = "") -> Path:
    """tmp 에 훅이 기대하는 최소 repo 구조를 만든다.

    `.claude/precompact_capture_hook.sh` (실 훅 복사) + 선택적으로
    `.project_manager/tools/pm_handoff.py` (stub). 훅은 자기 위치(.claude/)에서
    repo_root 를 자기해소하므로 이 tmp 루트가 곧 repo_root 가 된다(실 repo 무오염).
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    hook_copy = claude_dir / "precompact_capture_hook.sh"
    hook_copy.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    hook_copy.chmod(0o755)

    if with_handoff:
        tools_dir = tmp_path / ".project_manager" / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "pm_handoff.py").write_text(stub_body, encoding="utf-8")

    return hook_copy


def _run_hook(hook_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_SH, str(hook_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── (전제) 실 훅 파일 존재 ────────────────────────────────────────────────────

def test_hook_file_present():
    """실 훅 파일이 존재한다 — 복사 smoke 가 무의미해지지 않게."""
    assert HOOK.exists(), f"precompact 훅 없음: {HOOK}"


# ── ① pm_handoff 부재 → graceful skip (exit 0·파일 미생성) ────────────────────

def test_graceful_skip_when_handoff_absent(tmp_path):
    """pm_handoff.py 가 없는 디렉토리(어댑터 미배선 등)에서 훅은 exit 0·파일 미생성."""
    hook = _make_repo(tmp_path, with_handoff=False)
    result = _run_hook(hook)
    assert result.returncode == 0, (
        f"handoff 부재 시 graceful skip 위반 (exit {result.returncode}): {result.stderr}"
    )
    # 훅이 아무 파일도 만들지 않았다 — .claude/ 의 훅 자신만 존재.
    assert not (tmp_path / ".project_manager").exists()


# ── ② 실 repo 모방 → exit 0 + PreCompact marker append ───────────────────────

def test_marker_appended_on_precompact_flush(tmp_path):
    """실 repo 구조 모방 — stub pm_handoff 가 `--trigger --reason precompact` 로 호출돼
    log/current.md 에 PreCompact marker 를 append하고 훅은 exit 0.

    stub 은 훅이 넘긴 인자(--trigger --reason precompact)를 검증하고 marker 를 쓴다 —
    인자가 안 맞으면(계약 회귀) stub 이 marker 를 안 써 테스트가 잡는다.
    """
    log_dir = tmp_path / ".project_manager" / "wiki" / "log"
    # stub: 훅이 cd repo_root 후 호출하므로 상대경로로 log 에 쓴다. --trigger·--reason
    # precompact 가 인자에 둘 다 있을 때만 marker append (계약 회귀 가드).
    stub = (
        "import os, sys\n"
        "argv = sys.argv[1:]\n"
        "if '--trigger' in argv and 'precompact' in argv:\n"
        "    os.makedirs('.project_manager/wiki/log', exist_ok=True)\n"
        "    with open('.project_manager/wiki/log/current.md', 'a', encoding='utf-8') as fh:\n"
        f"        fh.write('reason=precompact {PRECOMPACT_MARKER}\\n')\n"
        "sys.exit(0)\n"
    )
    hook = _make_repo(tmp_path, with_handoff=True, stub_body=stub)
    result = _run_hook(hook)
    assert result.returncode == 0, f"exit 0 위반 (exit {result.returncode}): {result.stderr}"

    log_file = log_dir / "current.md"
    assert log_file.exists(), "stub pm_handoff 가 호출되지 않았다 (log/current.md 미생성)"
    text = log_file.read_text(encoding="utf-8")
    assert PRECOMPACT_MARKER in text, f"PreCompact marker 미append: {text!r}"
    assert "reason=precompact" in text, f"reason=precompact 미기록: {text!r}"


# ── ③ stub rc!=0(handoff 실패)여도 훅은 exit 0 (fail-soft) ────────────────────

def test_fail_soft_when_handoff_errors(tmp_path):
    """pm_handoff 가 rc!=0(실패)여도 훅은 exit 0 — 압축/세션을 절대 막지 않는다(`|| true`)."""
    stub = "import sys\nsys.exit(7)\n"  # 비0 rc 로 실패 모사.
    hook = _make_repo(tmp_path, with_handoff=True, stub_body=stub)
    result = _run_hook(hook)
    assert result.returncode == 0, (
        f"handoff 실패 시 fail-soft 위반 — 훅이 비0 exit ({result.returncode}): {result.stderr}"
    )
