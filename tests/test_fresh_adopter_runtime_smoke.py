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

# LLM subprocess env 화이트리스트 — 부모 셸의 모델 선택 변수(PM_ORCH_LIVE_MODEL·
# PM_ORCH_LIVE_CLAUDE_MODEL·PM_ORCH_LIVE)가 하위 LLM 으로 누수하면 모델 선택이 부모 env
# 의존(비-hermetic·재현성 저하)이 된다. 부모 환경을 통째 상속하지 않고 LLM 바이너리 동작에
# 필수인 것만 통과시킨다. 모델 값은 _live_env(model=...) 가 테스트 의도값으로 명시 set.
_LIVE_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE")


def _live_env(model: str) -> dict[str, str]:
    """LLM subprocess 용 명시 env(화이트리스트) — 부모 env 통째 상속·모델 누수 차단.

    필수 환경(PATH/HOME/로케일·LLM 바이너리 동작에 필요)만 부모에서 통과시키고, 테스트가
    의도한 모델을 `PM_ORCH_LIVE_MODEL` 로 직접 박는다(부모 env 폴백에 의존하지 않음). 부모
    셸에 set 된 PM_ORCH_LIVE_MODEL·PM_ORCH_LIVE_CLAUDE_MODEL·PM_ORCH_LIVE 는 화이트리스트
    밖이라 안 흘러든다 — 누가/어디서 돌려도 같은 모델로 동작(hermetic).
    """
    env = {k: os.environ[k] for k in _LIVE_ENV_PASSTHROUGH if k in os.environ}
    env["PM_NONINTERACTIVE"] = "1"
    env["PM_ORCH_LIVE_MODEL"] = model
    return env


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
        # 엔진 도구(board.py)는 LLM 이 아니라 모델 선택 무관 — 부모 env 상속 OK.
        # 모델 누수가 문제되는 LLM subprocess(opencode/claude)만 _live_env 로 격리한다.
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
        env=_live_env(LIVE_MODEL),
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
        env=_live_env(CLAUDE_MODEL),
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
    """채택자 디렉토리에서 self-update(pm_update) 실행 — 채택자 update 흐름 (hermetic).

    `--from str(REPO)` = 로컬 worktree 를 명시한다. pm_import 가 기록하는 기본 upstream 은
    릴리즈 추적용 **URL**(git@github.com:…)이고, 엔진 pm_update 는 URL 을 git clone/fetch
    하지 않는다(ADR-0032 D5 — URL→cache clone 은 `pm-update` 스킬 책임). 실 채택자는 그 스킬을
    쓰지만 테스트는 스킬(LLM/facade)을 못 돌리므로, 스킬이 cache clone 후 하는 "로컬 checkout
    에서 sync" 단계를 `--from <로컬 worktree>` 로 hermetic 근사한다. (이 명시가 없으면 URL
    upstream 에서 엔진이 rc 1 로 거부 → 게이트 영구 red — 라이브 게이트 도그푸드가 포착.)
    """
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_update.py"),
         "--from", str(REPO)],
        cwd=str(dest), capture_output=True, text=True,
        # 엔진 도구(pm_update.py)는 LLM 이 아니라 모델 선택 무관 — 부모 env 상속 OK.
        # 모델 누수가 문제되는 LLM subprocess(opencode/claude)만 _live_env 로 격리한다.
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
        env=_live_env(LIVE_MODEL),
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
        env=_live_env(CLAUDE_MODEL),
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
        env=_live_env(LIVE_MODEL),
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
        env=_live_env(CLAUDE_MODEL),
    )
    _assert_full_lifecycle(dest, proc, "claude")


# ── env 격리 가드 (라이브 실행 없이·hermetic — T-0155) ──────────────────────────
# 위 라이브 테스트들은 LLM subprocess 를 `env=_live_env(model)` 로 띄운다 — 부모 env 통째 상속
# 대신 화이트리스트. 아래 가드는 부모 셸에 모델 선택 변수를 오염시킨 채로도 그 부모 값이 LLM env
# 로 안 흘러듦을 단언한다(라이브 미호출·env 빌더/subprocess 인자 단위). 격리를 떼면 fail(sensitivity).

# 부모 셸에서 새면 모델 선택을 비-hermetic 하게 만드는 누수 변수 — 단일 진실.
# 가드들이 이 목록을 순회해 오염·누수0 을 단언하므로, 미래에 누수 변수가 늘면 여기 한 곳만
# 갱신하면 가드가 자동 커버한다. 각 값에 "leaked" 토큰을 박아 누수 시 env 에서 검출.
_LEAK_VARS = ("PM_ORCH_LIVE_MODEL", "PM_ORCH_LIVE_CLAUDE_MODEL", "PM_ORCH_LIVE")
_LEAK_SENTINEL = "leaked-by-parent"


def _pollute_parent_leak_vars(monkeypatch) -> None:
    """부모 셸 오염 모사 — 모든 누수 변수에 sentinel 토큰을 박는다(_LEAK_VARS 단일 진실)."""
    for var in _LEAK_VARS:
        monkeypatch.setenv(var, _LEAK_SENTINEL)


def test_live_env_does_not_leak_parent_model_vars(monkeypatch):
    """부모 env 에 모델 선택 변수가 오염돼 있어도 _live_env 결과로 그 부모 값이 안 새어든다."""
    _pollute_parent_leak_vars(monkeypatch)

    env = _live_env("test/intended-model")

    # 모델 값은 테스트가 의도한 것 — 부모의 sentinel 값이 아니다.
    assert env["PM_ORCH_LIVE_MODEL"] == "test/intended-model"
    # 어떤 누수 변수의 부모(sentinel) 값도 env 로 새지 않는다(_LEAK_VARS 순회).
    for var in _LEAK_VARS:
        assert env.get(var) != _LEAK_SENTINEL, f"부모 {var} 값이 _live_env 로 누수됨"
    # 부모의 sentinel 토큰이 env 어느 값에도 없다.
    assert _LEAK_SENTINEL not in repr(env)


def test_live_env_includes_required_runtime_env(monkeypatch):
    """화이트리스트가 LLM 바이너리 동작 필수 환경(PATH·HOME·PM_NONINTERACTIVE)을 포함한다.

    PATH/HOME/로케일 누락 시 LLM 실행 자체가 깨지므로 격리가 동작을 망가뜨리지 않는지 가드.
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/tester")

    env = _live_env("test/model")

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/tester"
    assert env["PM_NONINTERACTIVE"] == "1"


def test_live_env_passthrough_is_explicit_whitelist(monkeypatch):
    """부모의 임의 변수는 화이트리스트 밖이면 통과 안 함(부모 통째 상속 아님)."""
    monkeypatch.setenv("SOME_UNRELATED_PARENT_VAR", "should-not-pass")

    env = _live_env("test/model")

    assert "SOME_UNRELATED_PARENT_VAR" not in env
    # env 키는 화이트리스트 ∪ {PM_NONINTERACTIVE, PM_ORCH_LIVE_MODEL} 부분집합.
    allowed = set(_LIVE_ENV_PASSTHROUGH) | {"PM_NONINTERACTIVE", "PM_ORCH_LIVE_MODEL"}
    assert set(env).issubset(allowed)


def test_live_calls_use_isolated_env(monkeypatch):
    """라이브 호출 6개가 부모 env 통째 상속이 아니라 _live_env(화이트리스트)로 LLM 을 띄운다.

    실 LLM 을 띄우지 않고(subprocess.run 을 가로채) 각 라이브 테스트가 LLM 호출에 넘기는 env 가
    부모 누수 변수를 안 담음을 단언한다. 격리(env=_live_env)를 떼면 부모 통째 상속이 되어 fail
    (sensitivity). adopter import·board list·pm_update 등 LLM 이 아닌 subprocess 는 통과시킨다.
    """
    # 부모 셸 오염 — 격리가 없으면 이 값들이 LLM env 로 샌다(_LEAK_VARS 단일 진실).
    _pollute_parent_leak_vars(monkeypatch)

    real_run = subprocess.run
    llm_envs: list[dict | None] = []

    def _spy_run(cmd, *args, **kwargs):
        # LLM 바이너리 호출만 env 를 포착(import/board/pm_update 는 실행 그대로).
        if cmd and cmd[0] in ("opencode", "claude"):
            llm_envs.append(kwargs.get("env"))
            # 라이브 LLM 은 실제로 띄우지 않는다 — 빈 성공 응답으로 대역.
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy_run)
    # 게이트 우회 — 라이브 분기 진입(실 LLM 은 spy 가 막음). which 도 통과시킨다.
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/stub")

    import importlib
    mod = importlib.import_module(__name__)
    monkeypatch.setattr(mod, "PM_ORCH_LIVE", True)

    # tmp_path 대신 직접 만든 임시 디렉토리로 6개 라이브 테스트 함수를 spy 하에 구동.
    import tempfile
    live_tests = [
        test_live_opencode_adopter_bootstraps_and_creates_ticket,
        test_live_claude_adopter_bootstraps_and_creates_ticket,
        test_live_opencode_adopter_survives_pm_update_then_operates,
        test_live_claude_adopter_survives_pm_update_then_operates,
        test_live_opencode_adopter_runs_full_ticket_lifecycle,
        test_live_claude_adopter_runs_full_ticket_lifecycle,
    ]
    for fn in live_tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
            except AssertionError:
                # 라이브 LLM 을 stub 으로 막아 발행 단언은 실패할 수 있다 — 여기선 env 만 관심.
                pass

    assert llm_envs, "LLM subprocess 호출이 한 건도 포착되지 않음 — spy 배선 오류"
    for env in llm_envs:
        assert env is not None, "LLM 호출에 env 미전달(부모 통째 상속) — 격리 깨짐"
        # 어떤 누수 변수의 부모(sentinel) 값도 LLM env 로 새지 않는다(_LEAK_VARS 순회).
        for var in _LEAK_VARS:
            assert env.get(var) != _LEAK_SENTINEL, \
                f"부모의 {var} 이 LLM env 로 누수됨 — 격리 깨짐"
