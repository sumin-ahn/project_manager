"""T-0091 — opencode 어댑터 진입문서 v2 정합 회귀 가드.

v2 재설계(ADR-0017~0020)가 도입한 변경이 opencode 어댑터 진입문서(AGENTS.md·
AGENTS.lite.md)에 반영돼 있는지 단언한다. diff-scoped 리뷰의 *부재맹점*(기능은
머지됐는데 어댑터 문서가 따라오지 않음)을 회귀로 막는다.

검사 축:
  (a) domain 지식 레이어 사용법(ADR-0018) — CLI 4명령이 문서화돼 있다.
  (b) relay 네이밍(ADR-0020) — PM 세션을 spawn 하는 supervisor 가 relay 로 표기.
      ⚠️ orchestrator(PM-conductor)는 ADR-0020 이 *유지*하기로 했으므로 0 을 요구하지 않는다.
  (c) PM_SESSION_NAME(T-0073) — 세션 변수 우선순위에 신 변수가 등장.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
OPENCODE = REPO / "templates" / "opencode"
AGENTS_MD = OPENCODE / "AGENTS.md"
AGENTS_LITE_MD = OPENCODE / "AGENTS.lite.md"
ARCHITECT_MD = OPENCODE / ".opencode" / "agents" / "architect.md"
RESEARCHER_MD = OPENCODE / ".opencode" / "agents" / "researcher.md"
PM_DEV_DELEGATE_MD = OPENCODE / ".opencode" / "command" / "pm-dev-delegate.md"


def _load_agent_frontmatter(path: Path) -> dict:
    """agent md 의 yaml frontmatter 를 파싱한다 (--- ... --- 블록)."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"frontmatter 없음: {path}"
    end = text.find("\n---\n", 4)
    assert end != -1, f"frontmatter 종료 구분자 없음: {path}"
    return yaml.safe_load(text[4:end]) or {}

# 출하 doc 이 wikilink 하면 안 되는 framework-내부 ID (어댑터엔 그 파일 부재 → dangling).
_FRAMEWORK_WIKILINK = re.compile(r"\[\[(ADR-\d+|T-\d+|idea-\d+)\]\]")


# ── (a) domain 지식 레이어 사용법 ─────────────────────────────────────────────

def test_agents_md_documents_domain_cli():
    """AGENTS.md 가 domain.py CLI 4명령을 문서화한다 (ADR-0018 사용법)."""
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "domain 지식 레이어" in text, "AGENTS.md 에 domain 지식 레이어 섹션이 없음 (T-0091)"
    for sub in ("domain.py list", "domain.py affected", "domain.py capture", "domain.py lint"):
        assert sub in text, f"AGENTS.md 에 domain CLI {sub!r} 누락 (T-0091)"


def test_agents_lite_points_to_domain():
    """AGENTS.lite.md 는 (전체 섹션이 아니라) domain 포인터를 둔다."""
    text = AGENTS_LITE_MD.read_text(encoding="utf-8")
    assert "domain" in text, "AGENTS.lite.md 에 domain 포인터가 없음 (T-0091)"


# ── architecture.md 1순위 배선 (ADR-0022·T-0105) ─────────────────────────────

def test_agents_lite_links_architecture():
    """AGENTS.lite.md §1 부트스트랩이 architecture.md(현재-진실 #1)로 링크한다.

    T-0102 가 full AGENTS.md 에만 배선하고 lite 진입문서를 놓쳤던 것(잔여)을 못박는다 —
    lite 도 §1 부트스트랩에서 architecture.md 를 1순위로 안내해야 한다 (ADR-0022·T-0105).
    """
    text = AGENTS_LITE_MD.read_text(encoding="utf-8")
    assert "](.project_manager/wiki/architecture.md)" in text, (
        "AGENTS.lite.md 가 architecture.md(현재-진실 #1)로 링크해야 함 (ADR-0022·T-0105)"
    )


def _architect_deliverables_section() -> str:
    """architect.md 의 "## 위임받는 설계 spike 유형" 산출물 목록 섹션 본문만 슬라이스.

    frontmatter·"안 하는 것" 경계절 등 *다른 곳*에 같은 문자열(content-truth·ADR-0022)이
    있어 bullet 부재를 못 잡던 가드 약점(T-0105 리뷰)을 닫는다 — 산출물 섹션 안에서만 단언한다.
    """
    text = ARCHITECT_MD.read_text(encoding="utf-8")
    start = text.index("## 위임받는 설계 spike 유형")
    rest = text[start + len("## 위임받는 설계 spike 유형"):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_opencode_architect_mentions_architecture_content_truth():
    """opencode architect 산출물 섹션에 architecture.md content-truth 유지 bullet(ADR-0022)이 있다.

    `.claude` architect 파리티 — opencode architect 의 "위임받는 설계 spike 유형" 목록에
    architecture.md content-truth 유지 bullet 이 있어야 한다 (T-0105 잔여 배선).
    문자열 *존재*가 아니라 **산출물 섹션 안의 bullet** 을 단언한다 — frontmatter·경계절의
    동일 문자열로 통과하던 약점(T-0105 리뷰)을 닫음.
    """
    section = _architect_deliverables_section()
    assert "- **architecture.md · status.md content-truth 유지**" in section, (
        "opencode architect 산출물 목록에 architecture.md content-truth 유지 bullet 이 없음 "
        "(ADR-0022·T-0105 — frontmatter/경계절 언급은 산출물 보유가 아님)"
    )
    assert "ADR-0022" in section, (
        "content-truth bullet 이 ADR-0022(architecture content-truth)를 인용하지 않음 (T-0105)"
    )


def test_opencode_architect_lists_domain_author_deliverable():
    """opencode architect 산출물 섹션에 domain concept·guide author bullet(ADR-0018)이 있다.

    `.claude` architect 파리티의 다른 누락 bullet — 산출물 목록 안에서 단언한다 (T-0105).
    """
    section = _architect_deliverables_section()
    assert "- **domain concept·guide page author**" in section, (
        "opencode architect 산출물 목록에 domain concept·guide author bullet 이 없음 "
        "(ADR-0018·T-0105 파리티)"
    )


# ── researcher subagent 파리티 (gather 축 · ADR-0019 · T-0106) ────────────────

def test_opencode_researcher_exists():
    """opencode researcher subagent(gather 축)가 존재한다.

    claude_code 에는 `.claude/agents/researcher.md` 가 있는데 opencode 어댑터엔 통째로
    빠져 있던 갭(gather 축)을 못박는다 — 4축 subagent 파리티 (ADR-0019·T-0106).
    """
    assert RESEARCHER_MD.exists(), (
        f"opencode researcher subagent 없음: {RESEARCHER_MD} (gather 축 부재 · ADR-0019·T-0106)"
    )


def test_opencode_researcher_is_read_only():
    """opencode researcher frontmatter 가 read-only 다 (edit/write false).

    researcher 는 gather(조사·사실수집) 전용 — 파일을 만들거나 고치지 않는다. tools 의
    edit/write 가 false 여야 하고 permission.edit 도 deny 여야 한다 (T-0106).
    """
    fm = _load_agent_frontmatter(RESEARCHER_MD)
    assert fm.get("mode") == "subagent", "researcher mode 가 subagent 가 아님 (T-0106)"
    tools = fm.get("tools", {})
    assert tools.get("edit") is False, (
        f"researcher tools.edit 가 false 가 아님: {tools.get('edit')!r} (read-only 위반 · T-0106)"
    )
    assert tools.get("write") is False, (
        f"researcher tools.write 가 false 가 아님: {tools.get('write')!r} (read-only 위반 · T-0106)"
    )
    # 읽기/조사 도구는 켜져 있어야 gather 가 가능.
    for read_tool in ("read", "glob", "grep", "bash"):
        assert tools.get(read_tool) is True, (
            f"researcher tools.{read_tool} 가 true 가 아님: {tools.get(read_tool)!r} "
            f"(gather 에 읽기/조사 도구 필요 · T-0106)"
        )
    assert fm.get("permission", {}).get("edit") == "deny", (
        "researcher permission.edit 가 deny 가 아님 — read-only 가드 (T-0106)"
    )


def test_agents_md_lists_researcher_subagent_type():
    """AGENTS.md §3 가 researcher 를 subagent_type 으로 언급한다 (gather 축 배선 · T-0106).

    §3.1 후보 나열 + §3.2 매핑 표에 researcher 행이 있어야 PM 이 gather 위임을 쓸 수 있다.
    """
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "`researcher`" in text, (
        "AGENTS.md §3 에 researcher 가 subagent_type 으로 나열되지 않음 (gather 축 누락 · T-0106)"
    )
    # §3.2 매핑 표 행이 gather 성격을 명시하는지.
    assert "researcher | `researcher`" in text, (
        "AGENTS.md §3.2 매핑 표에 researcher 행이 없음 (T-0106)"
    )


# ── pm-dev-delegate command 파리티 (출하 trigger 면 · T-0110) ─────────────────

def test_opencode_pm_dev_delegate_command_exists():
    """opencode pm-dev-delegate command 가 존재한다 (출하 파리티 · T-0110).

    claude_code 에는 `.claude/skills/pm-dev-delegate/SKILL.md` 가 있는데 opencode 어댑터엔
    그 등가 command 가 빠져 있던 command 파리티 갭을 못박는다 (T-0110 · researcher 자매 갭).
    """
    assert PM_DEV_DELEGATE_MD.exists(), (
        f"opencode pm-dev-delegate command 없음: {PM_DEV_DELEGATE_MD} "
        f"(command 파리티 갭 · T-0110)"
    )


def test_opencode_pm_dev_delegate_is_command_format():
    """opencode pm-dev-delegate 가 opencode command 형식이다 (skill 형식 blind copy 아님 · T-0110).

    opencode command 는 `<command-instruction>` 래퍼 + `$ARGUMENTS` 주입 + frontmatter
    `description:`/`argument-hint:` 패턴이다 (pm-wave-finish.md 미러). claude skill 의
    `name:`/`Triggers:` 어휘를 적응 변환했는지 형식으로 단언한다 (T-0110).
    """
    text = PM_DEV_DELEGATE_MD.read_text(encoding="utf-8")
    fm = _load_agent_frontmatter(PM_DEV_DELEGATE_MD)
    assert "description" in fm, "pm-dev-delegate frontmatter 에 description 없음 (command 형식 · T-0110)"
    assert "argument-hint" in fm, (
        "pm-dev-delegate frontmatter 에 argument-hint 없음 (command 형식 · T-0110)"
    )
    # claude skill frontmatter 어휘(name:)를 적응 변환했는지 — opencode command 엔 없어야.
    assert "name" not in fm, (
        "pm-dev-delegate frontmatter 에 claude skill 어휘 'name:' 잔존 — command 형식으로 적응 필요 (T-0110)"
    )
    assert "<command-instruction>" in text and "</command-instruction>" in text, (
        "pm-dev-delegate 에 <command-instruction> 래퍼 없음 (opencode command 형식 위반 · T-0110)"
    )
    assert "<user-request>\n$ARGUMENTS\n</user-request>" in text, (
        "pm-dev-delegate 에 $ARGUMENTS 주입(<user-request> 블록) 없음 (opencode command 형식 · T-0110)"
    )


def test_opencode_pm_dev_delegate_references_agents_md_prompts():
    """pm-dev-delegate 가 dev/reviewer 표준 프롬프트를 복제하지 않고 AGENTS.md §3.4/§3.5 를 참조한다.

    single-source(ADR-0008 lean) — 표준 프롬프트 복제는 stale 원천이므로 command 본문은 trigger +
    절차만 thin 하게 두고 AGENTS.md 를 참조해야 한다 (T-0110 적응 규칙).
    """
    text = PM_DEV_DELEGATE_MD.read_text(encoding="utf-8")
    assert "§3.4" in text and "§3.5" in text, (
        "pm-dev-delegate 가 AGENTS.md §3.4(dev)/§3.5(reviewer) 표준 프롬프트를 참조하지 않음 "
        "(single-source 위반 · T-0110)"
    )


def test_opencode_pm_dev_delegate_uses_task_tool_vocab():
    """pm-dev-delegate 가 claude harness 어휘 대신 opencode task tool 어휘를 쓴다 (T-0110 적응 규칙).

    claude 의 `Agent 툴`/`subagent_type`/`run_in_background` harness background 표현을 task tool
    어휘로 치환했는지 단언한다 — `run_in_background` 잔존 0 (harness background 표현 제거).
    """
    text = PM_DEV_DELEGATE_MD.read_text(encoding="utf-8")
    assert "task tool" in text, (
        "pm-dev-delegate 가 opencode task tool 위임 어휘를 안 씀 (claude Agent 툴 어휘 잔존? · T-0110)"
    )
    assert "run_in_background" not in text, (
        "pm-dev-delegate 에 claude harness 'run_in_background' 표현 잔존 — "
        "task 병렬(opencode 자식 세션 관리)로 치환 필요 (T-0110)"
    )


def test_opencode_pm_dev_delegate_no_framework_wikilink():
    """pm-dev-delegate(출하 doc)가 framework ADR/ticket 을 wikilink 하지 않는다 (T-0090·T-0110).

    어댑터엔 그 ADR/ticket 파일이 없어 [[…]] 는 dangling 이다 — plain text(예 'ADR-0008')로.
    """
    hits = _FRAMEWORK_WIKILINK.findall(PM_DEV_DELEGATE_MD.read_text(encoding="utf-8"))
    assert not hits, (
        f"pm-dev-delegate 에 framework wikilink {hits} 잔존 — plain text 로 (T-0090·T-0110)"
    )


# ── (b) relay 네이밍 (ADR-0020 — spawn supervisor 만 개명) ─────────────────────

def test_agents_md_spawn_supervisor_is_relay():
    """PM 세션을 spawn 하는 supervisor 가 relay 로 표기된다 (ADR-0020 개명).

    ADR-0020: "orchestrator 는 PM-conductor 에 양보·세션 회전 supervisor 만 relay".
    따라서 orchestrator==0 을 요구하지 않는다 — relay 가 spawn 맥락에 등장하는지만 본다.
    """
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "relay" in text, "AGENTS.md 에 relay 표기가 없음 — spawn supervisor 개명 누락 (T-0091)"
    assert "ADR-0020" in text, "AGENTS.md 가 ADR-0020(relay 개명)을 인용하지 않음 (T-0091)"
    # 구 표현(orchestrator 가 PM 세션을 spawn)이 남아 있지 않은지 — spawn 맥락 한정.
    assert "orchestrator(ADR-0009)가" not in text, (
        "AGENTS.md 에 'orchestrator(ADR-0009)가 ... spawn' 구 표현 잔존 — relay 로 정정 필요 (T-0091)"
    )


# ── (c) PM_SESSION_NAME (T-0073) ─────────────────────────────────────────────

def test_agents_md_uses_pm_session_name():
    """세션 변수 안내가 PM_SESSION_NAME 을 (구 CLAUDE_SESSION_NAME alias 와 함께) 쓴다."""
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "PM_SESSION_NAME" in text, "AGENTS.md 가 PM_SESSION_NAME 을 안내하지 않음 (T-0073·T-0091)"


# ── 출하 doc = framework wikilink 0 (T-0090 규칙·scan 갭 백스톱) ───────────────

def test_opencode_entry_docs_no_framework_wikilink():
    """AGENTS.md·AGENTS.lite.md 가 framework ADR/ticket 을 wikilink 하지 않는다.

    어댑터엔 그 ADR/ticket 파일이 없어 [[…]] 는 dangling 이다. template parity 테스트의
    `board.py lint` 는 (claude 의) CLAUDE.md 만 스캔하고 opencode AGENTS.md 는 놓치는
    scan 갭이 있으므로(실측) 여기서 직접 단언한다 (T-0090 incident 재발 방지).
    """
    for p in (AGENTS_MD, AGENTS_LITE_MD):
        hits = _FRAMEWORK_WIKILINK.findall(p.read_text(encoding="utf-8"))
        assert not hits, (
            f"{p.name} 에 framework wikilink {hits} 잔존 — plain text(예 'ADR-0018')로 (T-0090·T-0091)"
        )
