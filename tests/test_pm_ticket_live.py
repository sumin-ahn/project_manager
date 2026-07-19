"""pm-ticket 스킬 라이브 하네스 테스트 — 실 LLM authoring flow → 실 board 상태 단언 (ADR-0050·T-0366).

`pm-ticket` 스킬(draft→fill→promote flow·얇은 래퍼)은 **LLM-facing 프롬프트**라 backbone pytest
(`test_board_promote_fill_gate.py`)로는 "게이트가 옳게 도나"만 검증되고 "**실 LLM 이 스킬 읽고
자족적 티켓을 authoring 하나**"는 미검증이다([[ADR-0050]]·[[harness-test-vs-machine-test]]). 이
파일이 그 갭을 on-demand 라이브 tier 에서 메운다 — fresh import 홈에서 **스킬을 유일 컨텍스트**로
준 실 LLM(claude + opencode/glm-5.2:cloud)에게 구체 주제의 티켓을 authoring 시키고, **실 board
상태**(발행된 티켓이 존재 + 5절 placeholder 0 + 주제 marker 포함)로 단언한다.

판정 철학(release_wave·pm_worktree_live 상속):
- **side-effect(실 board 파일 상태) 기반** — LLM 출력 phrasing 비결정에 강건. LLM 이 아무것도
  안 하면 티켓 파일이 없어 fail·티켓만 만들고 fill 을 안 하면 placeholder 잔존으로 fail →
  silent pass 불가(false-green 가드).
- **자족성 marker** — 프롬프트가 요구한 주제 고유 토큰이 본문에 있어야 통과(LLM 이 template 를
  그대로 두거나 무관 내용으로 채우면 fail).

on-demand — 사용자가 `PM_ORCH_LIVE=1` 로 트리거(비용·flaky 감수). `release` 마커는 **달지 않는다**
— 릴리즈 게이트 커플드-pin(board.LIVEGATE_RELEASE_PIN·_RELEASE_TEST_FILES 합산·templates 전파)은
orchestrator 소유라, 이 스킬을 릴리즈 tier 로 승격할지는 PM 결정에 맡긴다(승격 시 `@pytest.mark.
release` 부착 + pin 14→N 갱신). 기본 skip(env 미설정·바이너리 부재·CI green 불변).

always-run 가드(라이브 없이·매 회귀): (1) backbone fill 게이트의 강화 토큰 집합이 온전한지
(setup-rot pin·스킬 무관) (2) 스킬이 적용됐다면 backbone 참조·audience·프롬프트 구조. 스킬은
scratchpad→`.claude/skills` 를 **PM 이 적용**(서브에이전트 `.claude/` 쓰기 불가·harness 구조)하므로,
스킬-의존 가드는 스킬 부재 시 skip 하고 적용 후 활성화된다(T-0366 handoff — PM 이 적용 후 재회귀).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# 라이브 인프라 재사용(중복 금지·같은 tests/ import) — adopter import(hermetic·models 조회 차단)·
# LLM env 격리(화이트리스트).
from test_fresh_adopter_runtime_smoke import _import_adopter, _live_env

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# on-demand 트리거 — 미설정이면 라이브 전부 skip(CI green 불변·runtime_smoke 와 동일 단일 진실).
PM_ORCH_LIVE = os.environ.get("PM_ORCH_LIVE") == "1"
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/glm-5.2:cloud")
_CLAUDE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_CLAUDE_TIMEOUT", "600"))
_OPENCODE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_TIMEOUT", "1800"))

# 검증 대상 스킬(① canonical·ADR-0065 단일 소비·command 사본 은퇴). PM 이 scratchpad 에서
# `.claude/skills/pm-ticket/SKILL.md` 로 적용한다(T-0366). 미적용 시 스킬-의존 가드는 skip.
_SKILL = REPO / ".claude" / "skills" / "pm-ticket" / "SKILL.md"
_skill_present = pytest.mark.skipif(
    not _SKILL.exists(),
    reason="pm-ticket 스킬 미적용 — PM 이 scratchpad→.claude/skills 적용 후 활성(T-0366 handoff).",
)

# 라이브 authoring 주제 — 본문에 반드시 등장해야 하는 고유 marker(자족성/false-green 가드).
_TOPIC_TITLE = "graceful shutdown drain hook"
_TOPIC_MARKER = "SIGTERM"


def _load_board(path: Path = TOOLS / "board.py"):
    spec = importlib.util.spec_from_file_location(f"board_live_{abs(hash(str(path)))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _authoring_prompt(skill_text: str) -> str:
    """실 LLM 에 스킬만 주고 구체 주제의 티켓을 authoring 시키는 프롬프트(스킬 = 유일 컨텍스트).

    진입문서(CLAUDE.md/AGENTS.md) 경로를 *주지 않는다* — 스킬만으로 board.py new/fill/promote 를
    골라야 통과(= 스킬 사용성·ADR-0050). 주제 고유 토큰(SIGTERM 등)을 본문에 요구해 자족성/
    false-green 을 판정한다. --help 금지(command_card_usability 미러)."""
    return (
        "You are the PM for this project. Below (between <<<SKILL and SKILL>>>) is the pm-ticket "
        "skill — use ONLY it to decide the exact board.py commands and flags. Do NOT run any "
        "command with --help or -h, and do NOT open other documentation.\n\n"
        "Create ONE new ticket and fill in ALL of its body sections (목표/인터페이스/결정/완료 "
        "조건/참고) with real, self-contained content — leave NO template placeholder text. The "
        f"ticket is titled {_TOPIC_TITLE!r}: add a graceful shutdown hook that drains in-flight "
        f"work on a {_TOPIC_MARKER} signal before exit. The word {_TOPIC_MARKER!r} MUST appear in "
        "the ticket body. Touches src/server.py. After filling the body, promote/validate it per "
        "the skill.\n\n"
        "<<<SKILL\n" + skill_text + "\nSKILL>>>\n\n"
        "Run the skill's commands now from the directory that contains .project_manager."
    )


def _ticket_roots(dest: Path) -> list[Path]:
    """adopter 홈의 tickets 루트 후보 (legacy=wiki/tickets · board-git=board/tickets)."""
    return [
        dest / ".project_manager" / "wiki" / "tickets",
        dest / ".project_manager" / "board" / "tickets",
    ]


def _find_authored_ticket(dest: Path) -> tuple[Path, str] | None:
    """adopter 홈에서 새로 발행된 티켓 파일 + 본문을 찾는다 (open/ ∪ claimed/ ∪ .drafts/).

    seed 템플릿(`_template.md`)은 제외하고 실 T-*.md 만 본다. 여러 개면 가장 최근 것을 고른다.
    `.drafts/` 도 포함해 찾아 — 승격 안 된 draft 잔류(false-green)를 단언 지점에서 검출한다."""
    board = _load_board()
    candidates: list[Path] = []
    for root in _ticket_roots(dest):
        for status in ("open", "claimed", ".drafts"):
            candidates.extend((root / status).glob("T-*-*.md"))
    if not candidates:
        return None
    path = max(candidates, key=lambda p: p.stat().st_mtime)
    _fm, body = board.load_ticket(path)
    return path, body


def _assert_self_contained_ticket(dest: Path, proc: subprocess.CompletedProcess, harness: str):
    """라이브 실행 후 실 board 상태 단언 — 티켓 존재 + open/ 안착 + 5절 자족 + 주제 marker(false-green)."""
    board = _load_board()
    tail = (f"--- {harness} stdout(tail) ---\n{proc.stdout[-2500:]}\n"
            f"--- stderr(tail) ---\n{proc.stderr[-1200:]}")
    found = _find_authored_ticket(dest)
    assert found is not None, (
        f"실 {harness} 가 pm-ticket 스킬로 티켓을 발행하지 않음(board 에 T-*.md 없음·미실행 "
        f"false-green 가드).\n" + tail)
    path, body = found
    # promote 안착 — board-git 형상(.drafts 디렉토리 실존)이면 최종 파일이 open/ 에 있어야 한다.
    # .drafts/ 잔류 = LLM 이 authoring 만 하고 promote 를 안 함(authored-but-not-promoted false-green).
    # legacy(.drafts 없음)는 `new` 가 open/ 직행이라 조건부 — 그래도 open/claimed 안착은 요구한다.
    drafts_present = any((root / ".drafts").is_dir() for root in _ticket_roots(dest))
    if drafts_present:
        assert path.parent.name == "open", (
            f"board-git 형상인데 발행 티켓이 {path.parent.name}/ 에 잔류(open/ 아님) — LLM 이 "
            f"promote 미실행(false-green 가드).\n" + tail)
    else:
        assert path.parent.name in ("open", "claimed"), (
            f"발행 티켓이 {path.parent.name}/ 에 있음 — 미완 authoring(false-green 가드).\n" + tail)
    # 자족성 — authoring 게이트와 동일 strict(5절 존재 + placeholder 0). 하나라도 남으면 fail.
    issues = board._body_lint_issues(path.stem, body, strict_sections=True)
    assert not issues, (
        f"발행된 티켓 {path.name} 에 placeholder/thin 잔존 — 스킬 fill 미완(자족성 위반): "
        f"{[(k, d) for _t, k, d in issues]}\n" + tail)
    # 주제 marker — LLM 이 template 를 그대로 두거나 무관 내용으로 채웠으면 fail(false-green 가드).
    assert _TOPIC_MARKER in body, (
        f"발행된 티켓 본문에 주제 marker {_TOPIC_MARKER!r} 없음 — LLM 이 주제를 실제로 채우지 "
        f"않음(false-green 가드).\n" + tail)


# ── 라이브 테스트 (on-demand · 기본 skip · PM_ORCH_LIVE=1 opt-in · release 마커 없음) ──────────
@_skill_present
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("claude"),
    reason="pm-ticket 라이브 — PM_ORCH_LIVE=1 + claude CLI(API 과금) 필요. 기본 skip·on-demand.",
)
def test_pm_ticket_live_claude(tmp_path):
    """실 claude 가 pm-ticket 스킬만 보고 자족적 티켓을 authoring·실 board 상태 단언(hard).

    스킬(SKILL.md)만 컨텍스트로 주고 [발행 → 5절 fill → promote]를 시킨다 — 진입문서 미제공. claude 는
    subprocess cwd 존중(`--dir` 불요). 발행된 티켓이 존재 + placeholder 0 + 주제 marker(SIGTERM) 포함을
    hard 단언(미실행/미충전 시 red). API 과금."""
    dest = _import_adopter(tmp_path, "claude")
    prompt = _authoring_prompt(_SKILL.read_text(encoding="utf-8"))
    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--allowedTools", "Bash",
         "--output-format", "stream-json", "--verbose",
         "--dangerously-skip-permissions", prompt],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )
    _assert_self_contained_ticket(dest, proc, "claude")


@_skill_present
@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("opencode"),
    reason="pm-ticket 라이브 — PM_ORCH_LIVE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·on-demand.",
)
def test_pm_ticket_live_opencode(tmp_path):
    """실 opencode(glm-5.2:cloud)가 스킬만 보고 자족적 티켓을 authoring·실 board 상태 단언.

    claude 와 같은 스킬-only 프롬프트. side-effect(실 board 파일 상태)로만 판정해 opencode 의 tool-call
    비노출에 강건(release_wave 위임-관측 비대칭 상속). `--dir` 로 루트 핀(opencode 는 PWD 로 루트 오판)·
    `--dangerously-skip-permissions`(비대화 헤드리스 auto-reject 회피·throwaway tmp 홈 격리). API 과금 0."""
    dest = _import_adopter(tmp_path, "opencode")
    prompt = _authoring_prompt(_SKILL.read_text(encoding="utf-8"))
    proc = subprocess.run(
        ["opencode", "run", "--agent", "build", "--dir", str(dest),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL, prompt],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )
    _assert_self_contained_ticket(dest, proc, "opencode")


# ── always-run 가드 (라이브 없이·매 회귀 — setup-rot·마커 소실 백스톱) ──────────────────────────

def test_backbone_fill_gate_tokens_intact():
    """backbone fill 게이트의 강화 placeholder 토큰(T-0366)이 온전한지 — 라이브 무관 setup-rot pin.

    라이브가 skip 되는 CI 에서도 always-run 으로 backbone 을 exercise 한다: 라이브 시나리오가 의존하는
    "절별 placeholder 잔존 → 차단" 규칙이 후퇴하면(토큰 삭제·검사 우회) 여기서 red 로 잡힌다."""
    board = _load_board()
    for token in ("이 ticket 이 만들거나 바꾸는", "구현 방향에 대한 확정 사항",
                  "[[architecture]] 관련 절", "T-XXXX"):
        assert token in board._PLACEHOLDERS, (
            f"강화 토큰 {token!r} 이 _PLACEHOLDERS 에서 빠짐 — 절별 자족성 게이트 후퇴.")
    # 목표·DoD 만 채우고 인터페이스·결정을 뼈대로 남긴 본문은 issue 를 내야 한다(게이트 실효 확인).
    half = (
        "# T-0001 — 실 제목\n\n## 목표\n실제 목표.\n\n"
        "## 인터페이스\n이 ticket 이 만들거나 바꾸는 함수·클래스·CLI·데이터 형식의 시그니처/규격.\n\n"
        "## 결정\n구현 방향에 대한 확정 사항 (어떤 방식으로 / 왜).\n\n"
        "## 완료 조건 (Definition of Done)\n- [ ] 실제 산출물\n\n"
        "## 참고\n- [[ADR-0049]]\n\n## 메모\n"
    )
    assert board._body_lint_issues("T-0001", half), \
        "절반-채운 본문이 게이트를 통과함 — 강화 토큰이 실효하지 않음."


@_skill_present
def test_skill_references_backbone_and_audience():
    """스킬이 backbone(board.py new/lint/promote)을 참조하고 audience=pm-internal 을 선언한다.

    라이브 프롬프트가 임베드하는 스킬이 소실/개명되면 라이브가 read 에서 죽거나 가짜가 되므로,
    적용됐다면 backbone 커맨드 형태 + 청중 라벨을 고정한다(ADR-0049 4요소 중 backbone·audience)."""
    text = _SKILL.read_text(encoding="utf-8")
    assert "board.py" in text, "스킬이 backbone board.py 를 참조 안 함"
    for sub in ("new", "promote", "lint"):
        assert sub in text, f"스킬에 board 서브커맨드 {sub!r} 부재"
    assert "audience: pm-internal" in text, "스킬 frontmatter 에 audience: pm-internal 부재(ADR-0049)"


@_skill_present
def test_prompt_embeds_skill_and_topic():
    """프롬프트가 스킬 전문 + 주제 marker 를 담고 진입문서/--help 를 배제한다(스킬 유일 컨텍스트)."""
    skill_text = _SKILL.read_text(encoding="utf-8")
    prompt = _authoring_prompt(skill_text)
    assert skill_text in prompt
    assert "CLAUDE.md" not in prompt and "AGENTS.md" not in prompt
    assert _TOPIC_MARKER in prompt and _TOPIC_TITLE in prompt
    assert "Do NOT run any command with --help or -h" in prompt
