"""pm-adr 스킬 라이브 하네스 테스트 — 실 LLM ADR 발행 flow → 실 decisions 상태 단언 (ADR-0050·T-0368).

`pm-adr` 스킬(채번→scaffold→README 색인→log decide 원자화·얇은 래퍼)은 **LLM-facing 프롬프트**라
backbone pytest(`test_pm_adr.py` 20케이스)로는 "pm_adr.py 가 옳게 도나"만 검증되고 "**실 LLM 이
스킬 읽고 ADR 발행 flow 를 원자 수행하나**"는 미검증이다([[ADR-0050]]·[[harness-test-vs-machine-test]]).
이 파일이 그 갭을 on-demand 라이브 tier 에서 메운다 — fresh import 홈에 seed decisions(기존 ADR 1 +
README 표)를 깔고, **스킬을 유일 컨텍스트**로 준 실 LLM(claude + opencode/glm-5.2:cloud)에게 구체
주제의 ADR 발행을 시켜 **실 decisions 상태**(새 ADR 파일 = 다음 번호 + frontmatter + 주제 marker ·
README Accepted 행 · log decide entry)로 단언한다.

판정 철학(pm_ticket_live/release_wave 상속):
- **side-effect(실 파일 상태) 기반** — LLM 이 아무것도 안 하면 새 ADR 파일이 없어 fail·채번만 하고
  본문/색인을 안 채우면 각 단언이 fail → silent pass 불가(false-green 가드).
- **주제 marker** — 프롬프트가 요구한 고유 토큰이 ADR 본문에 있어야 통과.

on-demand — `PM_ORCH_LIVE=1` 트리거. `release` 마커는 달지 않는다(릴리즈 pin 커플드-갱신은
orchestrator 소유·pm_ticket_live 와 동일 근거). 기본 skip(CI green 불변).

always-run 가드(라이브 없이·매 회귀): 스킬 존재·backbone(pm_adr.py) 참조·audience 라벨 —
스킬은 PM 이 적용(`.claude/` 서브에이전트 쓰기 불가)하므로 부재 시 skip·적용 후 활성(T-0368 handoff).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_fresh_adopter_runtime_smoke import _import_adopter, _live_env
# codex 라이브 공용 헬퍼 (격리 CODEX_HOME + auth + codex exec·conftest 소유·ADR-0070 T-0407).
from conftest import codex_auth_available, drop_codex_auth, make_codex_home, run_codex_exec

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

PM_ORCH_LIVE = os.environ.get("PM_ORCH_LIVE") == "1"
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/glm-5.2:cloud")
# codex 축(D7): 기본 모델 = 로컬 config 상속(gpt-5.5·-m 생략)·명시 override 만 env.
CODEX_MODEL = os.environ.get("PM_ORCH_LIVE_CODEX_MODEL")  # None → codex 로컬 config 기본 상속.
_CLAUDE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_CLAUDE_TIMEOUT", "600"))
_OPENCODE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_TIMEOUT", "1800"))
_CODEX_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_CODEX_TIMEOUT", "600"))

_SKILL = REPO / ".claude" / "skills" / "pm-adr" / "SKILL.md"
_skill_present = pytest.mark.skipif(
    not _SKILL.exists(),
    reason="pm-adr 스킬 미적용 — PM 이 scratchpad→.claude/skills 적용 후 활성(T-0368 handoff).",
)

# 라이브 발행 주제 — 본문에 반드시 등장해야 하는 고유 marker(자족성/false-green 가드).
_TOPIC_TITLE = "sandbox egress allowlist"
_TOPIC_MARKER = "egress-allowlist"

# seed 기존 ADR — 라이브 홈 decisions 초기 상태(채번이 0002 를 내야 함·README 표 실구조).
_SEED_ADR = (
    "---\ntitle: seed decision\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
    "author: seed\ntype: decision\nstatus: accepted\nscope: internal-process\n---\n\n"
    "# ADR-0001 — seed decision\n\n## Context\nseed.\n\n## Decision\nseed.\n\n"
    "## Consequences\nseed.\n\n## References\n(없음)\n"
)
_SEED_README = (
    "# ADR 색인\n\n## Accepted (live)\n\n| ADR | 제목 | date | tags |\n|---|---|---|---|\n"
    "| [0001](0001-seed-decision.md) | seed decision | 2026-01-01 | seed |\n\n"
    "## Superseded (비권위)\n\n| ADR | 제목 | superseded_by | 요지 |\n|---|---|---|---|\n\n"
    "## Amended (유효)\n\n| ADR | 제목 | amended_by | 요지 |\n|---|---|---|---|\n"
)


def _seed_decisions(dest: Path) -> Path:
    """adopter 홈에 seed decisions 상태(ADR-0001 + README 표)를 깐다 — 라이브 시나리오 초기 상태."""
    ddir = dest / ".project_manager" / "wiki" / "decisions"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "0001-seed-decision.md").write_text(_SEED_ADR, encoding="utf-8")
    (ddir / "README.md").write_text(_SEED_README, encoding="utf-8")
    return ddir


def _issue_prompt(skill_text: str) -> str:
    """실 LLM 에 스킬만 주고 새 ADR 발행을 시키는 프롬프트(스킬 = 유일 컨텍스트·--help 금지)."""
    return (
        "You are the PM for this project. Below (between <<<SKILL and SKILL>>>) is the pm-adr "
        "skill — use ONLY it to decide the exact commands. Do NOT run any command with --help "
        "or -h, and do NOT open other documentation.\n\n"
        f"Issue ONE new accepted ADR titled {_TOPIC_TITLE!r}: outbound network egress from the "
        f"sandbox must go through an explicit allowlist. The token {_TOPIC_MARKER!r} MUST appear "
        "in the ADR body (Context or Decision). Fill Context/Decision/Consequences with real "
        "content (no placeholder text left), and complete the index/log steps per the skill.\n\n"
        "<<<SKILL\n" + skill_text + "\nSKILL>>>\n\n"
        "Run the skill's commands now from the directory that contains .project_manager."
    )


def _assert_adr_issued(dest: Path, proc: subprocess.CompletedProcess, harness: str):
    """라이브 실행 후 실 decisions 상태 단언 — 파일·번호·marker·README 행·log decide entry."""
    tail = (f"--- {harness} stdout(tail) ---\n{proc.stdout[-2500:]}\n"
            f"--- stderr(tail) ---\n{proc.stderr[-1200:]}")
    ddir = dest / ".project_manager" / "wiki" / "decisions"
    new = [p for p in ddir.glob("0002-*.md")]
    assert new, (
        f"실 {harness} 가 pm-adr 스킬로 ADR-0002 를 발행하지 않음(파일 없음·미실행 false-green 가드). "
        f"decisions: {sorted(p.name for p in ddir.glob('*.md'))}\n" + tail)
    body = new[0].read_text(encoding="utf-8")
    assert _TOPIC_MARKER in body, (
        f"발행된 ADR 본문에 주제 marker {_TOPIC_MARKER!r} 없음 — 주제 미충전(false-green 가드).\n" + tail)
    assert "status:" in body and "title:" in body, (
        f"발행된 ADR 에 frontmatter 필드 누락.\n" + tail)
    readme = (ddir / "README.md").read_text(encoding="utf-8")
    assert "0002-" in readme, (
        f"README 색인에 ADR-0002 행 부재 — 색인 단계 미수행(원자화 목표 위반).\n" + tail)
    log = dest / ".project_manager" / "wiki" / "log" / "current.md"
    assert log.exists() and ("decide" in log.read_text(encoding="utf-8")), (
        f"log decide entry 부재 — log 단계 미수행.\n" + tail)


# ── 라이브 (on-demand · 기본 skip · release 마커 없음) ─────────────────────────────────────

@_skill_present
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("claude"),
    reason="pm-adr 라이브 — PM_ORCH_LIVE=1 + claude CLI(API 과금) 필요. 기본 skip·on-demand.",
)
def test_pm_adr_live_claude(tmp_path):
    """실 claude 가 pm-adr 스킬만 보고 ADR 발행 flow 를 원자 수행·실 decisions 상태 단언."""
    dest = _import_adopter(tmp_path, "claude")
    _seed_decisions(dest)
    prompt = _issue_prompt(_SKILL.read_text(encoding="utf-8"))
    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--allowedTools", "Bash",
         "--output-format", "stream-json", "--verbose",
         "--dangerously-skip-permissions", prompt],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )
    _assert_adr_issued(dest, proc, "claude")


@_skill_present
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("opencode"),
    reason="pm-adr 라이브 — PM_ORCH_LIVE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·on-demand.",
)
def test_pm_adr_live_opencode(tmp_path):
    """실 opencode(glm-5.2:cloud)가 스킬만 보고 ADR 발행 flow 수행·실 decisions 상태 단언."""
    dest = _import_adopter(tmp_path, "opencode")
    _seed_decisions(dest)
    prompt = _issue_prompt(_SKILL.read_text(encoding="utf-8"))
    proc = subprocess.run(
        ["opencode", "run", "--agent", "build", "--dir", str(dest),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL, prompt],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )
    _assert_adr_issued(dest, proc, "opencode")


@_skill_present
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("codex") or not codex_auth_available(),
    reason="pm-adr 라이브 — PM_ORCH_LIVE=1 + codex CLI(gpt-5.5 과금·~/.codex/auth.json) 필요. 기본 skip·on-demand.",
)
def test_pm_adr_live_codex(tmp_path):
    """실 codex(gpt-5.5)가 pm-adr 스킬만 보고 ADR 발행 flow 를 원자 수행·실 decisions 상태 단언.

    claude/opencode 와 같은 스킬-only 프롬프트. side-effect(실 decisions 파일 상태)로만 판정 — codex
    tool-call 비노출에 강건. 격리 CODEX_HOME(auth 복사·종료 시 삭제·실 ~/.codex 미오염)·`codex exec …
    stdin=DEVNULL`(미닫힘 시 무기한 대기·spike §D3 실측)·`-s workspace-write`(board.py 실행/파일 쓰기)·
    `-C dest`(cwd 핀). 모델 기본 = 로컬 config 상속(gpt-5.5)·`PM_ORCH_LIVE_CODEX_MODEL` override. gpt-5.5
    과금(승인 완료·D7). T-0407 실측: gpt-5.5·codex-cli 0.144.6·ADR-0002 발행 flow green."""
    dest = _import_adopter(tmp_path, "codex")
    _seed_decisions(dest)
    prompt = _issue_prompt(_SKILL.read_text(encoding="utf-8"))
    home = make_codex_home(tmp_path)
    try:
        proc = run_codex_exec(prompt, dest, home, model=CODEX_MODEL, timeout=_CODEX_TIMEOUT)
    finally:
        drop_codex_auth(home)  # scratch 에 auth 잔류 방지(라이브 규율).
    _assert_adr_issued(dest, proc, "codex")


# ── always-run 가드 (라이브 없이·매 회귀) ─────────────────────────────────────────────────

@_skill_present
def test_pm_adr_skill_references_backbone_and_labeled():
    """pm-adr 스킬이 backbone(pm_adr.py)을 참조하고 audience 라벨을 보유(ADR-0049 4요소 정합)."""
    text = _SKILL.read_text(encoding="utf-8")
    assert "pm_adr.py" in text, "스킬이 backbone pm_adr.py 를 참조하지 않음(thin wrapper 위반)."
    assert "audience: pm-internal" in text, "audience 라벨(pm-internal) 부재(ADR-0049)."


def test_pm_adr_backbone_shipped_and_loadable():
    """backbone pm_adr.py 가 canonical 에 존재·로드 가능 — 라이브 시나리오 전제 setup-rot pin."""
    path = TOOLS / "pm_adr.py"
    assert path.is_file(), "pm_adr.py 부재 — T-0368 backbone 소실."
    spec = importlib.util.spec_from_file_location("pm_adr_live_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "next_adr_number"), "pm_adr.py 에 next_adr_number 부재 — API rot."
