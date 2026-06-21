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
  (c) agents/{developer,architect,code-reviewer}.md 의 `model: "{{OPENCODE_PRO_MODEL}}"` pin 유지
      (T-0033 pm_import 치환 타깃 안정).
  (d) `{{OPENCODE_PRO_MODEL}}` 전체 잔존이 정확히 그 3개 agents `model:` 줄뿐.

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
AGENT_FILES = [
    OPENCODE / ".opencode" / "agents" / "developer.md",
    OPENCODE / ".opencode" / "agents" / "code-reviewer.md",
    OPENCODE / ".opencode" / "agents" / "architect.md",
]

# 위임마다 모델을 명시하던 강등 대상 패턴 — task tool 1차는 인자 없음, opencode run 폴백도
# `-m {{OPENCODE_PRO_MODEL}}` 을 생략한다(폴백 모델 = opencode 기본; `--agent build/plan` 은
# 내장 primary 라 subagent `model:` 을 안 읽음 — Pro 강제는 `-m <model>`). 어댑터 어디에도 남으면 안 된다.
DASH_M_PIN = "-m {{OPENCODE_PRO_MODEL}}"
PRO_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"

# 각 agent 정의의 `opencode run --agent {build|plan}` 폴백은 쓰기/읽기 필요와 일치해야 한다.
# architect 는 설계 초안 문서 **쓰기** 권한이 필요 → build(쓰기). plan(읽기 전용)으로 적으면
# 폴백 시 쓰기가 막혀 모순 (codex must-fix·T-0032).
FALLBACK_AGENT = {
    "developer.md": "build",     # 쓰기 (코드 + 테스트)
    "architect.md": "build",     # 쓰기 (설계 초안 문서) — plan 아님
    "code-reviewer.md": "plan",  # 읽기 전용 (generate ≠ evaluate)
}

# agents/*.md frontmatter 에 pin 으로 유지돼야 할 model 줄.
MODEL_PIN_LINE = 'model: "{{OPENCODE_PRO_MODEL}}"'


def _opencode_md_files() -> list[Path]:
    """검사 대상 어댑터 md 파일 전부 — 존재하는 것만 (hermetic)."""
    candidates = [AGENTS_MD, AGENTS_LITE_MD]
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


# ── (b) AGENTS.md task tool 위임 · subagent_type 매핑 문구 존재 ──────────────

def test_agents_md_documents_task_tool_delegation():
    """AGENTS.md 가 네이티브 task tool 위임(1차)을 규약으로 명시한다."""
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "task" in text and "subagent_type" in text, (
        "AGENTS.md 에 task tool / subagent_type 위임 문구가 없음 (T-0032)"
    )
    # role → subagent_type 매핑표의 세 타입이 모두 문서화돼야 한다.
    for subagent_type in ("developer", "code-reviewer", "architect"):
        assert subagent_type in text, (
            f"AGENTS.md 에 subagent_type {subagent_type!r} 매핑 누락 (T-0032)"
        )


def test_agents_md_demotes_opencode_run_to_fallback():
    """AGENTS.md 가 `opencode run` 을 폴백으로 명시(강등)한다 — 삭제가 아닌 강등."""
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "opencode run" in text, "AGENTS.md 에서 opencode run 폴백이 사라짐 (강등 ≠ 삭제)"
    assert "폴백" in text, "AGENTS.md 에 opencode run 폴백(강등) 문구가 없음 (T-0032)"


def test_agent_fallback_run_mapping_matches_permission():
    """각 agent 정의의 `opencode run --agent {build|plan}` 폴백이 쓰기/읽기 필요와 일치한다.

    architect 는 설계 초안 문서 **쓰기** 권한이 필요 → build(쓰기). plan(읽기 전용)으로 적으면
    폴백 시 쓰기가 막혀 모순 (codex must-fix·T-0032). AGENTS.md §3.7 의 build 그룹과도 일치.
    """
    for path in AGENT_FILES:
        text = path.read_text(encoding="utf-8")
        want = FALLBACK_AGENT[path.name]
        wrong = "plan" if want == "build" else "build"
        assert f"opencode run --agent {want}" in text, (
            f"{path.name} 폴백이 `opencode run --agent {want}` 가 아님 (T-0032 권한 매핑)"
        )
        assert f"opencode run --agent {wrong}" not in text, (
            f"{path.name} 가 폴백을 `opencode run --agent {wrong}` 로 잘못 매핑 "
            f"(codex must-fix·T-0032 — 쓰기/읽기 권한 불일치)"
        )


def test_agents_lite_md_documents_task_tool_delegation():
    """AGENTS.lite.md 도 task tool 위임(1차)을 명시한다."""
    text = AGENTS_LITE_MD.read_text(encoding="utf-8")
    assert "task" in text and "subagent_type" in text, (
        "AGENTS.lite.md 에 task tool / subagent_type 위임 문구가 없음 (T-0032)"
    )


# ── (c) agents/*.md `model:` pin 유지 ───────────────────────────────────────

def test_agent_model_pin_retained():
    """각 agent frontmatter 의 `model: "{{OPENCODE_PRO_MODEL}}"` pin 이 유지된다.

    task tool 1차가 이 필드대로 자식을 구동하고(실증), pm_import(T-0033)가 이 토큰을
    결정적 치환 타깃으로 삼으므로 제거하면 안 된다. (ADR-0006 D5 supersede.)
    """
    for path in AGENT_FILES:
        fm = _load_frontmatter(path)
        assert fm.get("model") == PRO_MODEL_TOKEN, (
            f"{path.name} 의 model pin 이 {PRO_MODEL_TOKEN!r} 가 아님: {fm.get('model')!r} "
            "(T-0032 — pin 유지 필수)"
        )


def test_agent_model_pin_line_present_verbatim():
    """`model: "{{OPENCODE_PRO_MODEL}}"` 줄이 각 agent md 에 문자 그대로 존재한다."""
    for path in AGENT_FILES:
        text = path.read_text(encoding="utf-8")
        assert MODEL_PIN_LINE in text, (
            f"{path.name} 에 {MODEL_PIN_LINE!r} 줄이 없음 (T-0032 pin 회귀)"
        )


# ── (d) {{OPENCODE_PRO_MODEL}} 잔존이 정확히 agents 4곳 model: 줄뿐 ──────────

def test_pro_model_token_only_in_agent_model_pins():
    """`{{OPENCODE_PRO_MODEL}}` 전체 잔존 = agents/*.md 의 `model:` 줄 5곳뿐.

    `-m` 위임 명시는 전부 제거됐고, 토큰은 agent model pin 으로만 남아야 한다 (T-0032 DoD).
    subagent 4곳(researcher/architect/developer/code-reviewer — 4축 gather/design/build/
    evaluate · researcher=gather 추가 T-0106) + pm primary 1곳(T-0045·ADR-0009 relay spawn
    타깃 — primary 도 Pro 모델 pin 을 가지며 pm_import 치환 대상이다).
    """
    occurrences = []
    for path in _opencode_md_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PRO_MODEL_TOKEN in line:
                occurrences.append((path, i, line.strip()))

    # 정확히 5건 — 그리고 다섯 다 model pin 줄.
    assert len(occurrences) == 5, (
        f"`{PRO_MODEL_TOKEN}` 잔존이 5건(agents model pin)이 아님: "
        + "\n".join(f"{p.relative_to(REPO)}:{i}: {ln}" for p, i, ln in occurrences)
    )
    for path, i, line in occurrences:
        assert line == MODEL_PIN_LINE, (
            f"{path.relative_to(REPO)}:{i} 의 토큰이 model pin 줄이 아님: {line!r} (T-0032)"
        )
    pin_files = {p.name for p, _, _ in occurrences}
    assert pin_files == {
        "researcher.md", "developer.md", "code-reviewer.md", "architect.md", "pm.md"
    }, (
        f"model pin 이 예상 5 agent 파일이 아님: {pin_files}"
    )
