"""opencode 어댑터 위임 규약 회귀 가드 (T-0032).

opencode 어댑터의 **위임 1차 경로를 `opencode run` 외부 프로세스 → 네이티브 `task` tool 로
뒤집었다** (PM 9차 deciding test 실증 + 회사 라이브 PM 결론과 일치). `opencode run` 은 삭제가
아니라 headless·CI·task tool 미노출 빌드용 *폴백*으로 강등됐고, 위임마다 모델을 명시하던
`-m {{OPENCODE_PRO_MODEL}}` 은 전부 제거됐다 — 모델은 subagent 정의(`.opencode/agents/*.md`
frontmatter `model:`)가 정한다. (ADR-0006 §3/D3/D5 supersede.)

이 테스트는 그 문서 계약을 회귀 가드한다:
  (a) templates/opencode 트리에 `opencode run ... -m {{OPENCODE_PRO_MODEL}}` 패턴(= 위임마다
      모델 명시)이 0건.  더 넓게 `-m {{OPENCODE_PRO_MODEL}}` 자체가 0건.
  (b) AGENTS.md 에 task tool 위임·`subagent_type` 매핑 문구가 존재.
  (c) 역할 카드(developer/architect/code-reviewer/researcher)의 `model:` 이 역할별 위임 토큰
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
    OPENCODE / ".opencode" / "agents" / "code-reviewer.md",
    OPENCODE / ".opencode" / "agents" / "architect.md",
    OPENCODE / ".opencode" / "agents" / "researcher.md",
]

# 위임마다 모델을 명시하던 강등 대상 패턴 — task tool 1차는 인자 없음, opencode run 폴백도
# `-m {{OPENCODE_PRO_MODEL}}` 을 생략한다. 폴백도 `mode: all` custom 역할 카드를 직접 선택해
# 카드의 `model:`을 읽으며 build/plan으로 강등하지 않는다. 어댑터 어디에도 수기 모델 pin이 남으면 안 된다.
DASH_M_PIN = "-m {{OPENCODE_PRO_MODEL}}"
PRO_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"

# cross 폴백도 custom 역할명을 직접 선택한다. 네 카드는 mode: all이라 native task와
# `opencode run --agent <role>` 양쪽에서 동일한 prompt/model/permission을 쓴다.
FALLBACK_AGENT = {
    "developer.md": "developer",
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

    위임 1차가 task tool 로 뒤집혔고 모델은 subagent 정의가 정한다 — `opencode run` 폴백도
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
        "(T-0032 위임마다 모델 명시 강등 회귀):\n" + "\n".join(offenders)
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

def test_pm_instructions_documents_task_tool_delegation():
    """위임 규약(네이티브 task tool 1차)이 pm-instructions.md 에 명시된다 (T-0032·ADR-0069 이관).

    T-0401: 위임 규약(§3)이 AGENTS.md 공통 코어에서 pm-instructions.md 로 이관됐다(opencode.jsonc
    instructions 배열 로드). task tool / subagent_type 매핑 문구 단언은 이관처를 본다.
    """
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    assert "task" in text and "subagent_type" in text, (
        "pm-instructions.md 에 task tool / subagent_type 위임 문구가 없음 (T-0032·T-0401)"
    )
    # role → subagent_type 매핑표의 세 타입이 모두 문서화돼야 한다.
    for subagent_type in ("developer", "code-reviewer", "architect"):
        assert subagent_type in text, (
            f"pm-instructions.md 에 subagent_type {subagent_type!r} 매핑 누락 (T-0032·T-0401)"
        )


def test_pm_instructions_demotes_opencode_run_to_fallback():
    """위임 규약이 `opencode run` 을 폴백으로 명시(강등)한다 — 삭제가 아닌 강등 (pm-instructions.md 이관).

    T-0401: opencode run 폴백 서술(§3.7)도 위임 규약과 함께 pm-instructions.md 로 이관됐다.
    """
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    assert "opencode run" in text, "pm-instructions.md 에서 opencode run 폴백이 사라짐 (강등 ≠ 삭제)"
    assert "폴백" in text, "pm-instructions.md 에 opencode run 폴백(강등) 문구가 없음 (T-0032·T-0401)"


def test_agent_fallback_run_mapping_matches_permission():
    """각 역할은 cross에서도 build/plan이 아니라 자기 custom agent를 직접 선택한다."""
    for path in AGENT_FILES:
        text = path.read_text(encoding="utf-8")
        want = FALLBACK_AGENT[path.name]
        assert f"opencode run --agent {want}" in text, (
            f"{path.name} 폴백이 `opencode run --agent {want}` 가 아님 (T-0032 권한 매핑)"
        )
        assert _load_frontmatter(path).get("mode") == "all", (
            f"{path.name}가 native subagent와 cross primary를 함께 지원하지 않음"
        )


def test_agents_lite_md_documents_task_tool_delegation():
    """AGENTS.lite.md 도 task tool 위임(1차)을 명시한다."""
    text = AGENTS_LITE_MD.read_text(encoding="utf-8")
    assert "task" in text and "subagent_type" in text, (
        "AGENTS.lite.md 에 task tool / subagent_type 위임 문구가 없음 (T-0032)"
    )


# ── (c) agents/*.md `model:` 토큰 유지 ──────────────────────────────────────

def test_agent_model_token_is_role_scoped():
    """역할 카드 frontmatter 의 `model:` 이 **역할별 위임 토큰**이다.

    task tool 1차가 이 필드대로 자식을 구동하므로(실증), 카드가 리터럴이거나 역할 무관 단일
    토큰이면 local.conf `delegate.<role>.model` 선언이 실행면에 닿지 못한다(선언↔실행 불일치).
    primary `pm.md` 는 위임 역할이 아니라 PM 자신의 모델이라 설치 모델 토큰을 유지한다.
    """
    for path in AGENT_FILES:
        fm = _load_frontmatter(path)
        assert fm.get("model") == ROLE_MODEL_TOKENS[path.name], (
            f"{path.name} 의 model 이 {ROLE_MODEL_TOKENS[path.name]!r} 가 아님: "
            f"{fm.get('model')!r} — 역할별 해소가 깨진다"
        )
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
