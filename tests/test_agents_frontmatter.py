"""`.claude/agents/*.md` frontmatter 무결성 회귀 가드 (T-0089·[[T-0086]]).

서브에이전트 정의는 Claude Code 가 frontmatter(`name`/`description`/`tools`/`model`)로
로드한다 — 깨진 frontmatter·필수키 누락은 에이전트 미로드(silent)로 이어진다. v2 머지 전
4 에이전트(architect/code-reviewer/developer/researcher) 정의를 자동 검증한다:
  - 전부 `---\n` 로 시작·종료 구분자 존재·YAML 파싱 OK.
  - `name`/`description`/`tools` 키 존재.
  - researcher = read-only — tools 에 `Edit`/`Write` 없음·`Read`/`Glob`/`Grep` 포함.
  - root(.claude/) ↔ templates(claude_code/.claude/) 4파일 byte-identical(전파 무드리프트).

본보기: `tests/test_opencode_adapter_delegation.py::_load_frontmatter`. stdlib + pyyaml
(엔진이 이미 의존 — board.py). 파일 iterate·존재 시만 검사(hermetic·CLI 미실행).
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ROOT_AGENTS = REPO / ".claude" / "agents"
TEMPLATE_AGENTS = REPO / "templates" / "claude_code" / ".claude" / "agents"

AGENT_NAMES = ["architect.md", "code-reviewer.md", "developer.md", "researcher.md"]
REQUIRED_KEYS = ("name", "description", "tools")

# researcher = read-only gather 에이전트 — 쓰기 도구 금지·읽기 도구 필수(ADR-0018·T-0086).
RESEARCHER_FORBIDDEN_TOOLS = ("Edit", "Write")
RESEARCHER_REQUIRED_TOOLS = ("Read", "Glob", "Grep")


def _load_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"frontmatter 없음(--- 로 시작 안 함): {path}"
    end = text.find("\n---\n", 4)
    assert end != -1, f"frontmatter 종료 구분자 없음: {path}"
    return yaml.safe_load(text[4:end]) or {}


def _tools_set(fm: dict) -> set[str]:
    """frontmatter `tools` 를 토큰 집합으로 정규화 (콤마분리 문자열 또는 리스트)."""
    tools = fm.get("tools")
    if isinstance(tools, str):
        return {t.strip() for t in tools.split(",") if t.strip()}
    if isinstance(tools, list):
        return {str(t).strip() for t in tools if str(t).strip()}
    return set()


# ── (전제) 4 에이전트 파일이 양 트리에 존재 ──────────────────────────────────

def test_agent_files_present_in_both_trees():
    """root·templates 두 트리에 4 에이전트 정의가 모두 존재 — 빈 iterate 가드 무력화 방지."""
    for name in AGENT_NAMES:
        assert (ROOT_AGENTS / name).exists(), f"root 에이전트 없음: {ROOT_AGENTS / name}"
        assert (TEMPLATE_AGENTS / name).exists(), (
            f"templates 에이전트 없음: {TEMPLATE_AGENTS / name}"
        )


# ── frontmatter 파싱 + 필수키 (root·templates 양쪽) ──────────────────────────

def test_agent_frontmatter_parses_and_has_required_keys():
    """4 에이전트(양 트리)가 `---` 로 시작·YAML 파싱 OK·name/description/tools 키 존재."""
    for tree in (ROOT_AGENTS, TEMPLATE_AGENTS):
        for name in AGENT_NAMES:
            path = tree / name
            fm = _load_frontmatter(path)
            for key in REQUIRED_KEYS:
                assert key in fm and fm[key], (
                    f"{path.relative_to(REPO)} frontmatter 에 필수키 {key!r} 누락/빈값"
                )


# ── researcher read-only (쓰기 도구 없음·읽기 도구 있음) ─────────────────────

def test_researcher_is_read_only():
    """researcher = read-only — tools 에 Edit/Write 없음·Read/Glob/Grep 포함(양 트리)."""
    for tree in (ROOT_AGENTS, TEMPLATE_AGENTS):
        fm = _load_frontmatter(tree / "researcher.md")
        tools = _tools_set(fm)
        for forbidden in RESEARCHER_FORBIDDEN_TOOLS:
            assert forbidden not in tools, (
                f"researcher({tree.name}) 가 쓰기 도구 {forbidden!r} 보유 — read-only 위반 "
                f"(tools={sorted(tools)})"
            )
        for required in RESEARCHER_REQUIRED_TOOLS:
            assert required in tools, (
                f"researcher({tree.name}) tools 에 {required!r} 누락 (tools={sorted(tools)})"
            )


# ── root ↔ templates byte-identical (전파 무드리프트) ────────────────────────

def test_agents_root_templates_byte_identical():
    """4 에이전트 정의가 root 와 templates 에서 byte-identical (pm_update 전파 무드리프트).

    어댑터 에이전트는 루트 단일 진실에서 templates 로 동기화된다 — 드리프트 시 채택
    프로젝트가 옛/다른 정의를 받는다 ([[verify-engine-template-propagation]]).
    """
    for name in AGENT_NAMES:
        root_bytes = (ROOT_AGENTS / name).read_bytes()
        tmpl_bytes = (TEMPLATE_AGENTS / name).read_bytes()
        assert root_bytes == tmpl_bytes, (
            f"{name} 가 root↔templates byte-identical 아님 (전파 드리프트) — "
            "엔진/어댑터 변경 후 pm_update 로 전파 필요"
        )
