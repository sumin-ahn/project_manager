"""Fresh-adopter RUNTIME smoke — 실 opencode 가 출하 문서로 PM 을 운영하나 (기본 skip · on-demand).

테스트 3-tier 의 Tier 2(런타임). 기계층 e2e(`test_fresh_adopter_e2e`)는 harness-중립 engine 만
구동한다. 이 테스트는 그 위 *런타임* 층 — **실 opencode(agentic·로컬 ollama)가 import 된 adopter 의
`AGENTS.md` 를 읽고 `board.py` 로 ticket 을 실제 발행** 하는지(= LLM 이 출하 문서로 PM 을 운영) 검증한다.

비결정·느림·라이브 → relay smoke 와 같은 `PM_ORCH_LIVE` 게이트(기본 skip·`PM_ORCH_LIVE=1` 일 때만).
로컬 ollama 라 API 과금 0. 프롬프트에 board.py 경로를 *주지 않는다* — adopter 가 문서만으로 board
도구를 찾아 운영해야 통과(= 진짜 문서 운영성). side-effect(ticket 파일 생성)를 단언하므로 LLM 출력
phrasing 비결정에 강건하고, 모델이 운영 못하면 정직하게 fail 한다(기존 live smoke 철학).

이 테스트의 존재 자체가 "런타임 검증은 자동화 불가·사용자 직접" 이 틀렸음을 박제한다 — Linux headless
로 가능(2026-06-20 실증). **양 harness** 커버: opencode(로컬 ollama=과금 0·`--dir` 핀)·claude
(`claude-sonnet-4-6`·API 과금·cwd 존중). 남는 사용자 게이트는 Windows 플랫폼 특이점(CP949·py 런처·
회사 Pro 모델)뿐.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

PM_ORCH_LIVE = os.environ.get("PM_ORCH_LIVE") == "1"
# opencode: 로컬 ollama 모델(과금 0) — 기존 opencode live smoke 와 동일 default.
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/gemma4:26b")
# claude: sonnet-4-6(사용자 지정·API 과금) — env override.
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
RUNTIME_TIMEOUT = int(os.environ.get("PM_ADOPTER_RUNTIME_TIMEOUT", "300"))


def _make_prompt(entry_doc: str) -> str:
    """adopter 의 진입문서(`entry_doc`)만 보고 board 도구로 ticket 을 발행하라는 프롬프트.

    board.py 경로를 *주지 않는다* — adopter 가 문서만으로 도구를 찾아 운영해야 통과(= 문서 운영성).
    """
    return (
        f"You are the PM for this project. Read {entry_doc} to learn how the project board "
        "works. Then create exactly one ticket titled 'runtime smoke' using the project's board "
        "tool (touches README.md). After it is created, reply with the new ticket id."
    )


def _load_pm_import():
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_adopter(tmp_path: Path, harness: str) -> Path:
    """harness adopter 를 tmp 에 import (hermetic·라이브 models 조회 차단)."""
    pm_import = _load_pm_import()
    pm_import._real_models_runner = lambda: (False, [])
    dest = tmp_path / f"adopter-{harness}"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} adopter import 실패 (rc={rc})"
    return dest


def _board_list_recognizes_ticket(dest: Path) -> bool:
    """adopter 의 board.py list 가 ticket(T-)을 인식하는지 (= 형식적으로 유효한 발행)."""
    listing = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "board.py"), "list"],
        cwd=str(dest),
        capture_output=True,
        text=True,
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )
    return "T-" in listing.stdout


@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("opencode"),
    reason="runtime smoke — PM_ORCH_LIVE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·on-demand.",
)
def test_live_opencode_adopter_bootstraps_and_creates_ticket(tmp_path):
    """실 opencode(agentic·ollama)가 adopter `AGENTS.md` 만 보고 `board.py` 로 ticket 을 발행한다."""
    dest = _import_adopter(tmp_path, "opencode")
    open_dir = dest / ".project_manager" / "wiki" / "tickets" / "open"
    before = {p.name for p in open_dir.glob("T-*.md")}

    # 실 opencode agentic 구동 — board.py 경로 미제공(docs 서 찾게). 로컬 ollama·과금 0.
    # **`--dir` 로 프로젝트 루트를 adopter 에 명시 핀** — subprocess cwd 만으론 opencode 가
    # PWD(부모=repo)로 루트를 오판해 *엉뚱한 트리*(상위 repo)에서 작동한다(relay driver 도 --dir 격리).
    proc = subprocess.run(
        ["opencode", "run", "--agent", "build", "--dir", str(dest), "-m", LIVE_MODEL,
         _make_prompt("AGENTS.md")],
        cwd=str(dest), capture_output=True, text=True, timeout=RUNTIME_TIMEOUT,
    )

    created = {p.name for p in open_dir.glob("T-*.md")} - before
    assert created, (
        "실 opencode 가 adopter 문서로 ticket 을 발행하지 못함 — 런타임 운영 실패.\n"
        f"--- opencode stdout(tail) ---\n{proc.stdout[-2000:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert _board_list_recognizes_ticket(dest), "board.py list 가 실 opencode 발행 ticket 미인식"


@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("claude"),
    reason="runtime smoke — PM_ORCH_LIVE=1 + claude CLI 필요(API 과금). 기본 skip·on-demand.",
)
def test_live_claude_adopter_bootstraps_and_creates_ticket(tmp_path):
    """실 claude(`claude-sonnet-4-6`·agentic)가 adopter `CLAUDE.md` 만 보고 `board.py` 로 ticket 발행.

    claude 는 opencode 와 달리 subprocess cwd 를 존중한다(`--dir` 불요·프로브 실증·repo 오염 0).
    `--dangerously-skip-permissions` = throwaway tmp adopter 격리라 안전(실 repo 무영향). API 과금.
    """
    dest = _import_adopter(tmp_path, "claude")
    open_dir = dest / ".project_manager" / "wiki" / "tickets" / "open"
    before = {p.name for p in open_dir.glob("T-*.md")}

    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--allowedTools", "Bash",
         "--dangerously-skip-permissions", _make_prompt("CLAUDE.md")],
        cwd=str(dest), capture_output=True, text=True, timeout=RUNTIME_TIMEOUT,
    )

    created = {p.name for p in open_dir.glob("T-*.md")} - before
    assert created, (
        "실 claude 가 adopter 문서로 ticket 을 발행하지 못함 — 런타임 운영 실패.\n"
        f"--- claude stdout(tail) ---\n{proc.stdout[-2000:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert _board_list_recognizes_ticket(dest), "board.py list 가 실 claude 발행 ticket 미인식"
