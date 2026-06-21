"""T-0092 — claude_code 어댑터 진입문서 v2 정합 회귀 가드.

v2 재설계(ADR-0017~0020)가 도입한 변경이 claude_code 어댑터 진입문서
(CLAUDE.md·CLAUDE.lite.md)에 반영돼 있는지 단언한다. README 는 이미 v2 갱신
완료였으나 CLAUDE.md 스캐폴드가 drift 했던 것(architecture.md 잔존·researcher 누락·
domain 미언급·CLAUDE_SESSION_NAME)을 회귀로 막는다.

검사 축:
  (a) architecture.md(retired·ADR-0017) 부트스트랩 링크가 없다 — domain/ 으로 대체.
  (b) researcher agent(ADR-0019)가 문서에 등장.
  (c) domain 지식 레이어(ADR-0018)가 문서에 등장.
  (d) PM_SESSION_NAME(T-0073)가 세션 변수 안내에 등장.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLAUDE_CODE = REPO / "templates" / "claude_code"
CLAUDE_MD = CLAUDE_CODE / "CLAUDE.md"
CLAUDE_LITE_MD = CLAUDE_CODE / "CLAUDE.lite.md"

# 출하 doc 이 wikilink 하면 안 되는 framework-내부 ID (어댑터엔 그 파일 부재 → dangling).
_FRAMEWORK_WIKILINK = re.compile(r"\[\[(ADR-\d+|T-\d+|idea-\d+)\]\]")


# ── (a) architecture.md retired — 부트스트랩 링크 제거 ────────────────────────

def test_claude_md_links_architecture():
    """CLAUDE.md 부트스트랩이 architecture.md(현재-진실 단일 doc)로 링크한다.

    ADR-0017 의 architecture.md retire 를 ADR-0022 가 amend(부활) — 이제 architecture.md 가
    현재-아키텍처 단일 진실·부트스트랩 1순위. CLAUDE.md 가 그것을 가리켜야 한다.
    """
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "](.project_manager/wiki/architecture.md)" in text, (
        "CLAUDE.md 가 architecture.md(현재-진실 #1)로 링크해야 함 (ADR-0022)"
    )


def test_claude_lite_links_architecture():
    """CLAUDE.lite.md 부트스트랩도 architecture.md(현재-진실 #1)로 링크한다.

    T-0102 가 full CLAUDE.md 에만 배선하고 lite 진입문서를 놓쳤던 것(잔여)을 못박는다 —
    lite 도 §1 부트스트랩에서 architecture.md 를 1순위로 안내해야 한다 (ADR-0022·T-0105).
    """
    text = CLAUDE_LITE_MD.read_text(encoding="utf-8")
    assert "](.project_manager/wiki/architecture.md)" in text, (
        "CLAUDE.lite.md 가 architecture.md(현재-진실 #1)로 링크해야 함 (ADR-0022·T-0105)"
    )


# ── (b) researcher agent (ADR-0019) ──────────────────────────────────────────

def test_claude_md_mentions_researcher():
    """CLAUDE.md 가 researcher 서브에이전트를 언급한다 (claude_code 는 researcher.md 보유)."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "researcher" in text, "CLAUDE.md 가 researcher agent 를 언급하지 않음 (ADR-0019·T-0092)"


# ── (c) domain 지식 레이어 (ADR-0018) ─────────────────────────────────────────

def test_claude_md_mentions_domain_layer():
    """CLAUDE.md 가 domain 지식 레이어를 언급한다 (부트스트랩 또는 명령 절)."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "domain" in text, "CLAUDE.md 가 domain 지식 레이어를 언급하지 않음 (ADR-0018·T-0092)"


# ── (d) PM_SESSION_NAME (T-0073) ─────────────────────────────────────────────

def test_claude_md_uses_pm_session_name():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "PM_SESSION_NAME" in text, "CLAUDE.md 가 PM_SESSION_NAME 을 안내하지 않음 (T-0073·T-0092)"


def test_claude_lite_uses_pm_session_name():
    text = CLAUDE_LITE_MD.read_text(encoding="utf-8")
    assert "PM_SESSION_NAME" in text, "CLAUDE.lite.md 가 PM_SESSION_NAME 을 안내하지 않음 (T-0073·T-0092)"


# ── 출하 doc = framework wikilink 0 (T-0090 규칙) ─────────────────────────────

def test_claude_entry_docs_no_framework_wikilink():
    """CLAUDE.md·CLAUDE.lite.md 가 framework ADR/ticket 을 wikilink 하지 않는다.

    어댑터엔 그 ADR/ticket 파일이 없어 [[…]] 는 dangling — T-0090 incident(출하 template
    lint 깨짐)의 재발 방지. plain text 'ADR-NNNN' 으로 인용한다.
    """
    for p in (CLAUDE_MD, CLAUDE_LITE_MD):
        hits = _FRAMEWORK_WIKILINK.findall(p.read_text(encoding="utf-8"))
        assert not hits, (
            f"{p.name} 에 framework wikilink {hits} 잔존 — plain text(예 'ADR-0018')로 (T-0090·T-0092)"
        )
