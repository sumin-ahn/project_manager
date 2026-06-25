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


def _make_lifecycle_prompt(entry_doc: str) -> str:
    """진입문서만 보고 ticket 의 **full 라이프사이클**(new→claim→complete)을 운영하라는 프롬프트.

    board.py 경로를 *주지 않는다* — adopter 가 문서만으로 도구를 찾아 new·claim·complete 와
    complete sync-gate(log entry·--tests-pass 류)까지 자력으로 운영해야 통과(= 진짜 문서 운영성).
    side-effect(ticket 파일이 open→claimed→done 으로 이동)를 단언하므로 출력 phrasing 비결정에 강건.
    """
    return (
        f"You are the PM for this project. Read {entry_doc} to learn how the project board "
        "works. Then run a full ticket lifecycle using the project's board tool: (1) create "
        "exactly one ticket titled 'lifecycle smoke' (touches README.md), (2) claim it, "
        "(3) mark it complete/done. The complete step has a sync gate — satisfy it however the "
        "docs say (e.g. a log entry and the tests-pass / untested flag). After the ticket is "
        "marked done, reply with the ticket id."
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


def _tickets_in(dest: Path, status: str) -> set[str]:
    """`tickets/<status>/` 의 T-*.md 파일명 집합 (라이프사이클 side-effect 단언용)."""
    status_dir = dest / ".project_manager" / "wiki" / "tickets" / status
    return {p.name for p in status_dir.glob("T-*.md")} if status_dir.exists() else set()


@pytest.mark.live_gate
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


@pytest.mark.live_gate
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


# ── 채택자 *update* 경로 라이브 검증 (T-0133·activation 이 update 를 깨지 않는가) ──────────────
# 위 두 테스트는 import+operate 만 친다. 아래 두 테스트는 그 사이 *채택자 self-update*(pm_update)를
# 끼워, 활성화(@render·@target-owned·모델 폴백 중화)가 바꾼 *update 경로*를 라이브로 검증한다:
# import → pm_update(self-update) → 실 LLM 이 *post-update* 진입문서로 ticket 발행. import smoke 가
# 못 친 update 경로(opencode: .opencode/* @target-owned graceful skip / claude: .claude/* @render 재렌더)를
# 커버한다(회귀·rc 실측·codex 의 *기계* 검증 위에 *런타임* 층 1개 더).


def _self_update(dest: Path) -> subprocess.CompletedProcess:
    """채택자 디렉토리에서 self-update(pm_update) 실행 — 진짜 채택자 update 흐름."""
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_update.py")],
        cwd=str(dest), capture_output=True, text=True,
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


@pytest.mark.live_gate
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("opencode"),
    reason="runtime smoke — PM_ORCH_LIVE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·on-demand.",
)
def test_live_opencode_adopter_survives_pm_update_then_operates(tmp_path):
    """opencode 채택자가 self-update 후에도 안 깨지고 board 운영 가능 (.opencode/* @target-owned skip 경로)."""
    pm_import = _load_pm_import()
    dest = _import_adopter(tmp_path, "opencode")
    # 1) self-update: .opencode/* 는 @target-owned graceful skip(upstream=framework-root 가 안 들고 있음)·
    #    엔진경로 동기·rc0(crash/clobber 0). 활성화 전엔 source-부재로 rc2 였던 경로(T-0137+self-update 확장).
    upd = _self_update(dest)
    assert upd.returncode == 0, (
        f"opencode 채택자 pm_update 실패(rc={upd.returncode}) — activation 이 update 경로를 깸.\n"
        f"--- stdout ---\n{upd.stdout[-1500:]}\n--- stderr ---\n{upd.stderr[-800:]}"
    )
    # .opencode/agents 는 skip(보존)돼야 — 리터럴 모델 토큰 0(neutralized 유지·@render leak 0).
    dev_text = (dest / ".opencode" / "agents" / "developer.md").read_text(encoding="utf-8")
    assert pm_import.OPENCODE_MODEL_TOKEN not in dev_text, \
        "pm_update 후 .opencode/agents 에 리터럴 모델 토큰 잔존(@render leak·skip 안 됨)"
    # 2) post-update 운영성: 실 opencode 가 update 후 AGENTS.md 로 ticket 발행.
    open_dir = dest / ".project_manager" / "wiki" / "tickets" / "open"
    before = {p.name for p in open_dir.glob("T-*.md")}
    proc = subprocess.run(
        ["opencode", "run", "--agent", "build", "--dir", str(dest), "-m", LIVE_MODEL,
         _make_prompt("AGENTS.md")],
        cwd=str(dest), capture_output=True, text=True, timeout=RUNTIME_TIMEOUT,
    )
    created = {p.name for p in open_dir.glob("T-*.md")} - before
    assert created, (
        "pm_update 후 실 opencode 가 ticket 을 발행하지 못함 — update 후 운영성 실패.\n"
        f"--- opencode stdout(tail) ---\n{proc.stdout[-2000:]}\n--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert _board_list_recognizes_ticket(dest), "pm_update 후 board.py list 가 실 opencode 발행 ticket 미인식"


@pytest.mark.live_gate
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("claude"),
    reason="runtime smoke — PM_ORCH_LIVE=1 + claude CLI 필요(API 과금). 기본 skip·on-demand.",
)
def test_live_claude_adopter_survives_pm_update_then_operates(tmp_path):
    """claude 채택자가 self-update(.claude/* @render 재렌더) 후에도 안 깨지고 board 운영 가능."""
    dest = _import_adopter(tmp_path, "claude")
    # 1) self-update: .claude/* 는 @render(framework-root=claude 가 source 보유) 재렌더·rc0.
    upd = _self_update(dest)
    assert upd.returncode == 0, (
        f"claude 채택자 pm_update 실패(rc={upd.returncode}) — activation 이 update 경로를 깸.\n"
        f"--- stdout ---\n{upd.stdout[-1500:]}\n--- stderr ---\n{upd.stderr[-800:]}"
    )
    # 재렌더 산출물에 리터럴 토큰 0(self-containment·operational 해소).
    dev_text = (dest / ".claude" / "agents" / "developer.md").read_text(encoding="utf-8")
    import re as _re
    assert not _re.search(r"\{\{[A-Z_]+\}\}", dev_text), \
        "pm_update 재렌더 후 .claude/agents 에 미해소 토큰 잔존(leak)"
    # 2) post-update 운영성: 실 claude 가 update 후 CLAUDE.md 로 ticket 발행.
    open_dir = dest / ".project_manager" / "wiki" / "tickets" / "open"
    before = {p.name for p in open_dir.glob("T-*.md")}
    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--allowedTools", "Bash",
         "--dangerously-skip-permissions", _make_prompt("CLAUDE.md")],
        cwd=str(dest), capture_output=True, text=True, timeout=RUNTIME_TIMEOUT,
    )
    created = {p.name for p in open_dir.glob("T-*.md")} - before
    assert created, (
        "pm_update 후 실 claude 가 ticket 을 발행하지 못함 — update 후 운영성 실패.\n"
        f"--- claude stdout(tail) ---\n{proc.stdout[-2000:]}\n--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert _board_list_recognizes_ticket(dest), "pm_update 후 board.py list 가 실 claude 발행 ticket 미인식"


# ── A tier 라이브 커버 확장: full 티켓 라이프사이클 (new→claim→finish · T-0150) ──────────────
# 위 테스트들은 *new*(발행)까지만 라이브로 친다. 아래 두 테스트는 실 LLM 이 진입문서만 보고
# board.py 로 **new→claim→complete** 전 라이프사이클을 운영하는지 검증한다(spike §3.2). 단언은
# side-effect(ticket 파일이 open→claimed→done 으로 이동)라 출력 phrasing 비결정에 강건하다 —
# complete 의 sync-gate(log entry·--tests-pass 류)까지 문서만 보고 자력 운영해야 통과한다.


def _assert_full_lifecycle(dest: Path, proc: subprocess.CompletedProcess, harness: str) -> None:
    """라이프사이클 side-effect 단언 — ticket 1개가 done/ 에 도달(open·claimed 잔류 없음).

    done/ 도달이 핵심 단언(open→claimed→done 전이 완주). complete sync-gate 까지 통과해야만
    done/ 에 ticket 이 생기므로, 이 단언이 곧 full 라이프사이클 운영성을 증명한다.
    """
    done_tickets = _tickets_in(dest, "done")
    tail = (
        f"--- {harness} stdout(tail) ---\n{proc.stdout[-2000:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert done_tickets, (
        f"실 {harness} 가 ticket 을 done/ 까지 운영하지 못함 — full 라이프사이클(new→claim→"
        f"complete) 실패.\nopen={_tickets_in(dest, 'open')} claimed={_tickets_in(dest, 'claimed')}\n"
        + tail
    )
    assert _board_list_recognizes_ticket(dest), f"board.py list 가 실 {harness} 운영 ticket 미인식"


@pytest.mark.live_gate
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("opencode"),
    reason="runtime smoke — PM_ORCH_LIVE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·on-demand.",
)
def test_live_opencode_adopter_runs_full_ticket_lifecycle(tmp_path):
    """실 opencode(agentic·ollama)가 `AGENTS.md` 만 보고 ticket new→claim→complete 를 운영한다."""
    dest = _import_adopter(tmp_path, "opencode")
    proc = subprocess.run(
        ["opencode", "run", "--agent", "build", "--dir", str(dest), "-m", LIVE_MODEL,
         _make_lifecycle_prompt("AGENTS.md")],
        cwd=str(dest), capture_output=True, text=True, timeout=RUNTIME_TIMEOUT,
    )
    _assert_full_lifecycle(dest, proc, "opencode")


@pytest.mark.live_gate
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("claude"),
    reason="runtime smoke — PM_ORCH_LIVE=1 + claude CLI 필요(API 과금). 기본 skip·on-demand.",
)
def test_live_claude_adopter_runs_full_ticket_lifecycle(tmp_path):
    """실 claude(`claude-sonnet-4-6`·agentic)가 `CLAUDE.md` 만 보고 ticket new→claim→complete 운영.

    claude 는 subprocess cwd 를 존중한다(`--dir` 불요). `--dangerously-skip-permissions` =
    throwaway tmp adopter 격리라 안전(실 repo 무영향). API 과금.
    """
    dest = _import_adopter(tmp_path, "claude")
    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--allowedTools", "Bash",
         "--dangerously-skip-permissions", _make_lifecycle_prompt("CLAUDE.md")],
        cwd=str(dest), capture_output=True, text=True, timeout=RUNTIME_TIMEOUT,
    )
    _assert_full_lifecycle(dest, proc, "claude")
