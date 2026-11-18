"""opencode 어댑터 위임 규약 회귀 가드 (T-0032).

opencode 어댑터의 **위임 1차 경로를 `opencode run` 자식 프로세스 → 네이티브 `task` tool 로
뒤집었다** (PM 9차 deciding test 실증 + 회사 라이브 PM 결론과 일치). `opencode run` 은 삭제가
아니라 headless·CI·task tool 미노출 빌드용 *대체 경로*로 낮춰졌고, 위임마다 모델을 명시하던
`-m {{OPENCODE_PRO_MODEL}}` 은 전부 제거됐다 — 모델은 subagent 정의(`.opencode/agents/*.md`
frontmatter `model:`)가 정한다. (ADR-0006 §3/D3/D5 supersede.)

이 테스트는 그 문서 계약을 회귀 가드한다:
  (a) templates/opencode 트리에 `opencode run ... -m {{OPENCODE_PRO_MODEL}}` 패턴(= 위임마다
      모델 명시)이 0건.  더 넓게 `-m {{OPENCODE_PRO_MODEL}}` 자체가 0건.
  (b) AGENTS.md 에 task tool 위임·`subagent_type` 매핑 문구가 존재.
  (c) 역할 카드(developer/developer-hard/architect/code-reviewer/researcher)의 `model:` 이 위임 토큰
      (`{{DELEGATE_MODEL_<ROLE>}}` — local.conf `delegate.<role>.model` 의 렌더 파생물)이고,
      primary `pm.md` 만 설치 모델 pin(`{{OPENCODE_PRO_MODEL}}`)을 유지.
  (d) `{{OPENCODE_PRO_MODEL}}` 전체 잔존이 정확히 `pm.md` 의 `model:` 줄 하나뿐.

stdlib + pyyaml(엔진이 이미 의존 — board.py) 만 사용. opencode CLI 미실행. 파일 iterate·존재
시만 검사(hermetic).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
OPENCODE = REPO / "templates" / "opencode"

AGENTS_MD = OPENCODE / "AGENTS.md"
AGENTS_LITE_MD = OPENCODE / "AGENTS.lite.md"
# ADR-0069·T-0401: 위임 규약(task tool·subagent_type 매핑)이 AGENTS.md 공통 코어에서
#   pm-instructions.md 로 이관됐다(opencode.jsonc instructions 로드). 위임 규약 문구 단언은 이관처를 본다.
PM_INSTRUCTIONS_MD = OPENCODE / ".opencode" / "pm-instructions.md"
AGENT_FILES = [
    OPENCODE / ".opencode" / "agents" / "developer.md",
    OPENCODE / ".opencode" / "agents" / "developer-hard.md",
    OPENCODE / ".opencode" / "agents" / "code-reviewer.md",
    OPENCODE / ".opencode" / "agents" / "architect.md",
    OPENCODE / ".opencode" / "agents" / "researcher.md",
]

# 위임마다 모델을 명시하던 지운 패턴 — task tool 1차는 인자 없음, opencode run 대체 경로도
# `-m {{OPENCODE_PRO_MODEL}}` 을 생략한다. 대체 경로도 `mode: all` custom 역할 카드를 직접 선택해
# 카드의 `model:`을 읽으며 build/plan으로 낮추지 않는다. 어댑터 어디에도 수기 모델 pin이 남으면 안 된다.
DASH_M_PIN = "-m {{OPENCODE_PRO_MODEL}}"
PRO_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"

# cross 대체 경로도 custom 역할명을 직접 선택한다. 다섯 카드는 mode: all이라 native task와
# `opencode run --agent <role>` 양쪽에서 동일한 prompt/model/permission을 쓴다.
# developer-hard 는 developer 역할의 hard 티어 변주다 — 역할이 늘어난 게 아니라
# 티어가 자기 카드를 갖는다(T-0766·세 하네스 같은 규칙).
FALLBACK_AGENT = {
    "developer.md": "developer",
    "developer-hard.md": "developer-hard",
    "architect.md": "architect",
    "code-reviewer.md": "code-reviewer",
    "researcher.md": "researcher",
}

# agents/*.md frontmatter 의 model 줄. **역할 카드는 역할별 위임 토큰**(local.conf
# `delegate.<role>.model` 의 렌더 파생물)이고, primary `pm.md` 는 위임 역할이 아니라 PM 자신의
# 모델이라 opencode 설치 모델 토큰을 유지한다.
PRO_MODEL_PIN_LINE = 'model: "{{OPENCODE_PRO_MODEL}}"'
ROLE_MODEL_TOKENS = {
    "developer.md": "{{DELEGATE_MODEL_DEVELOPER}}",
    "developer-hard.md": "{{DELEGATE_MODEL_DEVELOPER_HARD}}",
    "code-reviewer.md": "{{DELEGATE_MODEL_CODE_REVIEWER}}",
    "architect.md": "{{DELEGATE_MODEL_ARCHITECT}}",
    "researcher.md": "{{DELEGATE_MODEL_RESEARCHER}}",
}
PM_CARD = OPENCODE / ".opencode" / "agents" / "pm.md"


def _opencode_md_files() -> list[Path]:
    """검사 대상 어댑터 md 파일 전부 — 존재하는 것만 (hermetic)."""
    candidates = [AGENTS_MD, AGENTS_LITE_MD]
    # T-0674: canonical skill 미러와 사람 slash command 기계 사본을 모두 검사한다.
    candidates += sorted((OPENCODE / ".claude" / "skills").glob("*/SKILL.md"))
    candidates += sorted((OPENCODE / ".opencode" / "command").glob("*.md"))
    candidates += sorted((OPENCODE / ".opencode" / "agents").glob("*.md"))
    return [p for p in candidates if p.exists()]


def _load_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"frontmatter 없음: {path}"
    end = text.find("\n---\n", 4)
    assert end != -1, f"frontmatter 종료 구분자 없음: {path}"
    return yaml.safe_load(text[4:end]) or {}


# ── (전제) 대상 파일 존재 ───────────────────────────────────────────────────

def test_adapter_files_present():
    """어댑터 md 파일이 실제로 존재한다 — 빈 iterate 로 가드가 무의미해지지 않게."""
    assert AGENTS_MD.exists(), f"AGENTS.md 없음: {AGENTS_MD}"
    assert AGENTS_LITE_MD.exists(), f"AGENTS.lite.md 없음: {AGENTS_LITE_MD}"
    for path in AGENT_FILES:
        assert path.exists(), f"agent 정의 없음: {path}"


# ── (a) `opencode run ... -m {{OPENCODE_PRO_MODEL}}` / `-m` pin 0건 ─────────

def test_no_opencode_run_with_model_flag():
    """`opencode run` 명령에 `-m {{OPENCODE_PRO_MODEL}}` (위임마다 모델 명시)이 0건.

    위임 1차가 task tool 로 뒤집혔고 모델은 subagent 정의가 정한다 — `opencode run` 대체 경로도
    `-m` 을 생략한다 (정의/기본 상속). (ADR-0006 D3/D5 supersede.)
    """
    offenders = []
    pattern = re.compile(r"opencode run.*-m\s+\{\{OPENCODE_PRO_MODEL\}\}")
    for path in _opencode_md_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, (
        "opencode 어댑터에 `opencode run ... -m {{OPENCODE_PRO_MODEL}}` 잔존 "
        "(T-0032 위임마다 모델 명시 회귀):\n" + "\n".join(offenders)
    )


def test_no_dash_m_pro_model_anywhere():
    """더 넓게 — 어댑터 어디에도 `-m {{OPENCODE_PRO_MODEL}}` (모델 플래그 명시) 0건."""
    offenders = []
    for path in _opencode_md_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if DASH_M_PIN in line:
                offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, (
        "opencode 어댑터에 `-m {{OPENCODE_PRO_MODEL}}` 잔존 (T-0032 회귀):\n"
        + "\n".join(offenders)
    )


# ── (b) task tool 위임 · subagent_type 매핑 문구 존재 (ADR-0069·T-0401: pm-instructions.md 이관) ──

def test_pm_instructions_documents_subagent_tool_delegation():
    """위임 규약(네이티브 `subagent` 도구)이 pm-instructions.md 에 명시된다.

    opencode 2.x 의 위임 도구는 `subagent`(인자 `agent`·`description`·`prompt`)이고 결과는 백그라운드
    자식이 끝난 뒤 `<subagent>` 메시지로 온다. 1.x `task` 도구의 `subagent_type` 인자는 남지 않는다.
    """
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    assert "`subagent` 도구" in text and "- `agent` —" in text and "<subagent" in text, (
        "pm-instructions.md 에 subagent 도구 위임 문구가 없음")
    assert "subagent_type" not in text, "pm-instructions.md 에 1.x task 도구 인자(subagent_type) 잔존"
    for agent in ("developer", "code-reviewer", "architect"):
        assert f"`{agent}`" in text, f"pm-instructions.md 에 agent {agent!r} 매핑 누락"


def test_pm_instructions_routes_cross_opencode_run_through_engine():
    """`opencode run` 은 다른 하네스 PM 의 위임(cross)을 엔진이 실행하는 경로로만 남는다.

    opencode 2.x 는 `subagent` 도구를 늘 싣고 `opencode run` 도 백그라운드 자식을 기다리므로 1.x 의
    '도구 미노출 빌드용 대체 동작'은 없다.
    """
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    assert "opencode run --agent" in text and "pm_delegate.py" in text, "cross opencode run 서술이 사라짐"
    assert "대체 동작" not in text, "pm-instructions.md 에 1.x 대체 동작 서술 잔존"


def test_agent_fallback_run_mapping_matches_permission():
    """각 역할은 cross에서도 build/plan이 아니라 자기 custom agent를 직접 선택한다."""
    for path in AGENT_FILES:
        text = path.read_text(encoding="utf-8")
        want = FALLBACK_AGENT[path.name]
        assert f"opencode run --agent {want}" in text, (
            f"{path.name} 대체 경로가 `opencode run --agent {want}` 가 아님 (T-0032 권한 매핑)"
        )
        assert _load_frontmatter(path).get("mode") == "all", (
            f"{path.name}가 native subagent와 cross primary를 함께 지원하지 않음"
        )


def test_agents_lite_md_documents_subagent_tool_delegation():
    """AGENTS.lite.md 도 `subagent` 도구 위임을 명시한다."""
    text = AGENTS_LITE_MD.read_text(encoding="utf-8")
    assert "`subagent` 도구" in text and "`agent`" in text, "AGENTS.lite.md 에 subagent 도구 위임 문구가 없음"
    assert "subagent_type" not in text, "AGENTS.lite.md 에 1.x task 도구 인자(subagent_type) 잔존"



def test_subagent_message_states_guide_collect_and_failure():
    """`<subagent>` 메시지는 state 로 판정한다.

    opencode 2.x 는 자식에 백그라운드 작업이 남아 있으면 `state="running"` 메시지를 먼저 보내고 그
    작업이 끝나면 한 번 더 보낸다. 지침은 running 이면 다음 메시지를 기다리고 `state="completed"` 를
    받은 뒤 돌려받으며, error·cancelled 만 실패로 본다.
    """
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    assert 'state="running"' in text and 'state="completed"' in text, (
        "pm-instructions.md 가 subagent 메시지 state(running·completed)를 가르지 않음")
    running_lines = [line for line in text.splitlines() if 'state="running"' in line]
    assert any("기다" in line for line in running_lines), (
        f"running 메시지를 받으면 기다린다는 안내가 없음: {running_lines}")
    lite = AGENTS_LITE_MD.read_text(encoding="utf-8")
    assert "completed 아님" not in lite, "AGENTS.lite.md 가 running 메시지를 위임 실패로 판정함"
    assert "error" in lite, "AGENTS.lite.md 위임 실패 판정에 error state 가 없음"

# ── (c) agents/*.md `model:` 토큰 유지 ──────────────────────────────────────

def test_agent_model_token_is_role_scoped():
    """역할 카드 frontmatter 의 `model:` 이 **역할별 위임 토큰**이다.

    task tool 1차가 이 필드대로 자식을 구동하므로(실증), 카드가 리터럴이거나 역할 무관 단일
    토큰이면 local.conf `delegate.<role>.model` 선언이 실행면에 닿지 못한다(선언↔실행 불일치).
    primary `pm.md` 는 위임 역할이 아니라 PM 자신의 모델이라 설치 모델 토큰을 유지한다.
    """
    for path in AGENT_FILES:
        fm = _load_frontmatter(path)
        assert "temperature" not in fm, f"{path.name}: opencode 2.x 가 보내지 않는 temperature 잔존"
        assert fm.get("model") == ROLE_MODEL_TOKENS[path.name], (
            f"{path.name} 의 model 이 {ROLE_MODEL_TOKENS[path.name]!r} 가 아님: "
            f"{fm.get('model')!r} — 역할별 해소가 깨진다"
        )
    assert "temperature" not in _load_frontmatter(PM_CARD), "pm.md: opencode 2.x 가 보내지 않는 temperature 잔존"
    assert _load_frontmatter(PM_CARD).get("model") == PRO_MODEL_TOKEN, (
        f"pm.md 의 model pin 이 {PRO_MODEL_TOKEN!r} 가 아님 (PM 자신의 모델은 위임 역할이 아님)"
    )


def test_agent_model_token_line_present_verbatim():
    """`model: "{{DELEGATE_MODEL_<ROLE>}}"` 줄이 각 역할 카드에 문자 그대로 존재한다."""
    for path in AGENT_FILES:
        text = path.read_text(encoding="utf-8")
        expected = f'model: "{ROLE_MODEL_TOKENS[path.name]}"'
        assert expected in text, f"{path.name} 에 {expected!r} 줄이 없음"
    assert PRO_MODEL_PIN_LINE in PM_CARD.read_text(encoding="utf-8")


# ── (d) {{OPENCODE_PRO_MODEL}} 잔존이 정확히 pm.md model: 줄 하나 ────────────

def test_pro_model_token_only_in_pm_primary_pin():
    """`{{OPENCODE_PRO_MODEL}}` 전체 잔존 = `pm.md` 의 `model:` 줄 1곳뿐.

    `-m` 위임 명시는 전부 제거됐고, 역할 카드 4장은 역할별 위임 토큰으로 옮겨갔다. 남는 건
    primary(PM 자신·relay spawn 타깃)의 설치 모델 pin 하나다 — 그것까지 위임 토큰으로 바꾸면
    위임 역할이 아닌 자리를 `delegate.*` 로 표현하게 된다.
    """
    occurrences = []
    for path in _opencode_md_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PRO_MODEL_TOKEN in line:
                occurrences.append((path, i, line.strip()))

    assert len(occurrences) == 1, (
        f"`{PRO_MODEL_TOKEN}` 잔존이 1건(pm.md model pin)이 아님: "
        + "\n".join(f"{p.relative_to(REPO)}:{i}: {ln}" for p, i, ln in occurrences)
    )
    path, i, line = occurrences[0]
    assert path.name == "pm.md", f"설치 모델 pin 이 pm.md 밖에 있음: {path.relative_to(REPO)}"
    assert line == PRO_MODEL_PIN_LINE, (
        f"{path.relative_to(REPO)}:{i} 의 토큰이 model pin 줄이 아님: {line!r}"
    )


def test_role_model_tokens_only_in_their_own_card():
    """역할 토큰은 자기 카드에만 있다 — 카드끼리 토큰이 섞이면 역할별 해소가 무너진다."""
    for path in AGENT_FILES:
        text = path.read_text(encoding="utf-8")
        for name, token in ROLE_MODEL_TOKENS.items():
            if name == path.name:
                continue
            assert token not in text, f"{path.name} 에 타 역할 토큰 {token} 잔존"
