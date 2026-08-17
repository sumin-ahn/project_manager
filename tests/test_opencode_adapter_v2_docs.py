"""T-0091 — opencode 어댑터 진입문서 v2 정합 회귀 가드.

v2 재설계(ADR-0017~0020)가 도입한 변경이 opencode 어댑터 진입문서(AGENTS.md·
AGENTS.lite.md)에 반영돼 있는지 단언한다. diff-scoped 리뷰의 *부재맹점*(기능은
머지됐는데 어댑터 문서가 따라오지 않음)을 회귀로 막는다.

**T-0401(ADR-0069) 갱신 — 진입 doc 공통 코어 + 하네스별 전달 채널**: AGENTS.md 를 얇은
harness-neutral 공통 코어로 축소하고, opencode-고유 실행 모델(§0)·위임 규약(§3)·config 캐싱
노트는 `.opencode/pm-instructions.md`(opencode.jsonc `instructions` 배열 로드·@source 전파)로
이관했다. 따라서 relay(b)·researcher subagent_type(c 하위) 단언은 *이관처* pm-instructions.md
를 본다(코어/이관 분리). domain 사용법(a)·PM_SESSION_NAME(c)·안전가드 등 harness-neutral 항은
AGENTS.md 공통 코어에 잔류한다.

검사 축:
  (a) domain 지식 레이어 사용법(ADR-0018) — CLI 4명령이 AGENTS.md 공통 코어에 문서화돼 있다.
  (b) relay 네이밍(ADR-0020) — PM 세션을 spawn 하는 supervisor 가 relay 로 표기(pm-instructions.md).
      ⚠️ orchestrator(PM-conductor)는 ADR-0020 이 *유지*하기로 했으므로 0 을 요구하지 않는다.
  (c) PM_SESSION_NAME(T-0073) — 세션 변수 우선순위에 신 변수가 AGENTS.md 에 등장.
  (d) 공통 코어/이관 분리(ADR-0069·T-0401) — AGENTS.md=코어만(실행모델·위임규약 부재)·
      pm-instructions.md=이관 전문(실행모델·위임규약·free-form-free @render)·jsonc 등록.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from _repo_owned_inventory import OWNED, repo_owned_paths
from _skill_command import command_matches_skill

REPO = Path(__file__).resolve().parents[1]
OPENCODE = REPO / "templates" / "opencode"
AGENTS_MD = OPENCODE / "AGENTS.md"
AGENTS_LITE_MD = OPENCODE / "AGENTS.lite.md"
# ADR-0069·T-0401: opencode-고유 실행 모델·위임 규약의 이관처(@render·opencode.jsonc instructions 로드).
PM_INSTRUCTIONS_MD = OPENCODE / ".opencode" / "pm-instructions.md"
OPENCODE_JSONC = OPENCODE / ".opencode" / "opencode.jsonc"
ARCHITECT_MD = OPENCODE / ".opencode" / "agents" / "architect.md"
RESEARCHER_MD = OPENCODE / ".opencode" / "agents" / "researcher.md"
# T-0674: opencode는 canonical skill 미러와 기계 생성 command 사본을 모두 출하한다.
PM_DEV_DELEGATE_MIRROR = OPENCODE / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
PM_DEV_DELEGATE_COMMAND = OPENCODE / ".opencode" / "command" / "pm-dev-delegate.md"
PM_DEV_DELEGATE_CANONICAL = OPENCODE / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
OPENCODE_MANIFEST = OPENCODE / ".project_manager" / "engine.manifest"


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
    """AGENTS.lite.md도 architecture.md의 현재-진실 지위를 링크로 보존한다.

    T-0102 가 full AGENTS.md 에만 배선하고 lite 진입문서를 놓쳤던 것(잔여)을 못박는다 —
    시작 시 통독 대상은 아니지만 필요 시 읽을 현재 아키텍처 단일 진실 포인터는 유지한다.
    """
    text = AGENTS_LITE_MD.read_text(encoding="utf-8")
    assert "](.project_manager/wiki/architecture.md)" in text, (
        "AGENTS.lite.md 가 architecture.md 현재-진실 포인터를 링크해야 함"
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
    """researcher는 native/cross 공용 custom agent이며 Bash·edit를 기계적으로 거부한다."""
    fm = _load_agent_frontmatter(RESEARCHER_MD)
    assert fm.get("mode") == "all", "researcher가 native task와 cross run을 함께 지원하지 않음"
    assert "tools" not in fm, "deprecated tools 설정을 권위 permission과 중복하면 안 됨"
    permission = fm.get("permission", {})
    for read_tool in ("read", "glob", "grep", "list"):
        assert permission.get(read_tool) == "allow"
    assert permission.get("edit") == "deny"
    assert permission.get("bash") == "deny"
    assert permission.get("task") == "deny"


def test_pm_instructions_lists_researcher_subagent_type():
    """pm-instructions.md §2 위임 규약이 researcher 를 subagent_type 으로 언급한다 (gather 축 · T-0106).

    ADR-0069·T-0401: 위임 규약(§3)이 AGENTS.md 공통 코어에서 pm-instructions.md 로 이관됐다 —
    §2.1 후보 나열 + §2.2 매핑 표에 researcher 행이 있어야 PM 이 gather 위임을 쓸 수 있다.
    """
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    assert "`researcher`" in text, (
        "pm-instructions.md §2 에 researcher 가 subagent_type 으로 나열되지 않음 (gather 축 누락 · T-0106)"
    )
    # §2.2 매핑 표 행이 gather 성격을 명시하는지.
    assert "researcher | `researcher`" in text, (
        "pm-instructions.md §2.2 매핑 표에 researcher 행이 없음 (T-0106·T-0401)"
    )


# ── pm-dev-delegate 출하 표면(skill tool + slash command·T-0674) ─────────────
# 두 표면은 모두 root canonical 에서 기계 생성되며 command는 평탄 좌표 링크만 기계 rewrite한다.


def test_opencode_pm_dev_delegate_ships_as_canonical_skill_mirror():
    """opencode pm-dev-delegate target override와 command가 같은 canonical source를 쓴다.

    command 사본 판정은 개행 표기를 정규화한 **내용 동일성**이다(바이트 표기 동일성이 아니다·T-0708).
    """
    assert PM_DEV_DELEGATE_MIRROR.is_file(), (
        f"opencode pm-dev-delegate 출하 스킬 미러 없음: {PM_DEV_DELEGATE_MIRROR} "
        "(`pm_update --target opencode` 로 전파).")
    assert PM_DEV_DELEGATE_CANONICAL.is_file(), (
        f"canonical pm-dev-delegate 스킬 없음: {PM_DEV_DELEGATE_CANONICAL}")
    assert PM_DEV_DELEGATE_MIRROR.resolve() == PM_DEV_DELEGATE_CANONICAL.resolve()
    assert PM_DEV_DELEGATE_COMMAND.is_file(), "opencode pm-dev-delegate 슬래시 command 누락"
    # 판정 층은 공용 seam(LF 정규화 bytes 내용 동일성·T-0708) — 기대·실측을 한 곳에서 읽어
    # 체크아웃 개행 표기와 무관하게, 내용 1자 차이는 red로 판정한다.
    assert command_matches_skill(
        PM_DEV_DELEGATE_CANONICAL, "pm-dev-delegate", PM_DEV_DELEGATE_COMMAND,
    ), "opencode pm-dev-delegate command가 canonical과 내용 drift(T-0674)."
    manifest = OPENCODE_MANIFEST.read_text(encoding="utf-8")
    source = "templates/opencode/.claude/skills/pm-dev-delegate/SKILL.md"
    assert f".claude/skills/pm-dev-delegate/SKILL.md    @render @source={source}" in manifest
    assert f".opencode/command/pm-dev-delegate.md     @render @source={source}" in manifest


def test_opencode_native_ticket_growth_uses_task_contract_only():
    """3개 성장 역할은 OpenCode task의 실제 3필드로 prepare→spawn→harvest한다."""
    text = PM_DEV_DELEGATE_CANONICAL.read_text(encoding="utf-8")
    blocks = re.findall(r"task tool 호출:\n(.*?)(?=\n```)", text, flags=re.DOTALL)
    assert len(blocks) == 3
    for role, block in zip(("developer", "code-reviewer", "architect"), blocks):
        assert f"subagent_type: {role}" in block
        assert "description:" in block and "prompt:" in block
        assert f"role={role}" in block and "<prepare JSON의 copy>" in block
    assert "ticket prepare" in text and "ticket harvest" in text
    assert "Agent 툴 호출" not in text
    assert "run_in_background" not in text
    assert "최신 architect" in text and "절을 직접 성장" in text


def test_opencode_pm_dev_delegate_no_framework_wikilink():
    """pm-dev-delegate 출하 두 표면이 framework ADR/ticket 을 wikilink 하지 않는다.

    어댑터/채택자 트리엔 그 ADR/ticket 파일이 없어 `[[…]]`는 dangling 이다."""
    for path in (PM_DEV_DELEGATE_MIRROR, PM_DEV_DELEGATE_COMMAND):
        hits = _FRAMEWORK_WIKILINK.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name}에 framework wikilink {hits} 잔존 — plain text 로"


# ── (b) relay 네이밍 (ADR-0020 — spawn supervisor 만 개명 · pm-instructions.md 이관·T-0401) ──

def test_pm_instructions_spawn_supervisor_is_relay():
    """PM 세션을 spawn 하는 supervisor 가 relay 로 표기된다 (ADR-0020 개명 · pm-instructions.md).

    ADR-0020: "orchestrator 는 PM-conductor 에 양보·세션 회전 supervisor 만 relay".
    따라서 orchestrator==0 을 요구하지 않는다 — relay 가 spawn 맥락에 등장하는지만 본다.
    ADR-0069·T-0401: 실행 모델(§0·relay spawn 서술 포함)이 pm-instructions.md 로 이관됐다.
    """
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    assert "relay" in text, "pm-instructions.md 에 relay 표기가 없음 — spawn supervisor 개명 누락 (T-0091)"
    assert "ADR-0020" in text, "pm-instructions.md 가 ADR-0020(relay 개명)을 인용하지 않음 (T-0091)"
    # 구 표현(orchestrator 가 PM 세션을 spawn)이 남아 있지 않은지 — spawn 맥락 한정.
    assert "orchestrator(ADR-0009)가" not in text, (
        "pm-instructions.md 에 'orchestrator(ADR-0009)가 ... spawn' 구 표현 잔존 — relay 로 정정 (T-0091)"
    )


# ── (c) PM_SESSION_NAME (T-0073) ─────────────────────────────────────────────

def test_agents_md_uses_pm_session_name():
    """세션 변수 안내가 PM_SESSION_NAME 을 (구 CLAUDE_SESSION_NAME alias 와 함께) 쓴다."""
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "PM_SESSION_NAME" in text, "AGENTS.md 가 PM_SESSION_NAME 을 안내하지 않음 (T-0073·T-0091)"


# ── 출하 doc = framework wikilink 0 (T-0090 규칙·scan 갭 백스톱) ───────────────

def test_opencode_entry_docs_no_framework_wikilink():
    """AGENTS.md·AGENTS.lite.md·pm-instructions.md 가 framework ADR/ticket 을 wikilink 하지 않는다.

    어댑터엔 그 ADR/ticket 파일이 없어 [[…]] 는 dangling 이다. template parity 테스트의
    `board.py lint` 는 (claude 의) CLAUDE.md 만 스캔하고 opencode AGENTS.md 는 놓치는
    scan 갭이 있으므로(실측) 여기서 직접 단언한다 (T-0090 incident 재발 방지).
    T-0401: 이관처 pm-instructions.md 도 같은 출하 doc 위생 대상으로 편입.
    """
    for p in (AGENTS_MD, AGENTS_LITE_MD, PM_INSTRUCTIONS_MD):
        hits = _FRAMEWORK_WIKILINK.findall(p.read_text(encoding="utf-8"))
        assert not hits, (
            f"{p.name} 에 framework wikilink {hits} 잔존 — plain text(예 'ADR-0018')로 (T-0090·T-0091)"
        )


# ── (d) 공통 코어 / 이관 분리 (ADR-0069·T-0401) ───────────────────────────────
# @render 파일의 free-form 토큰(활성화 시 omit→채택자 안전 라인 소실·ADR-0030) — pm-instructions.md
# 는 @render 라 이 토큰들을 담으면 render 시 _assert_no_leak 가 hard-fail 한다(test_adapter_free_form_free
# 동형 불변식을 이 단일 파일에도 lock-in — RENDER_SCOPED_DIRS 는 디렉토리만 스캔해 이 단일 파일을 놓침).
_FREEFORM_TOKENS = ("{{PROJECT_CONSTRAINTS}}", "{{PROTECTED_PATHS}}", "{{USER_GATE_ITEMS}}")


def test_agents_md_is_thin_common_core():
    """AGENTS.md = 얇은 harness-neutral 공통 코어 — 실행 모델·위임 규약 절이 *부재* (ADR-0069·T-0401 DoD).

    opencode-고유 실행 모델(구 §0)·위임 규약(구 §3)은 pm-instructions.md 로 이관됐다. AGENTS.md 는
    공통 코어(프로젝트 정체성·엔진 호출·완료 부기·결정 권한·안전 가드)만 남는다. codex 어댑터(T-0402)가
    이 코어를 byte-parity 로 공유하는 전제 — 실행모델/위임규약이 남으면 harness-neutral 이 깨진다.
    """
    text = AGENTS_MD.read_text(encoding="utf-8")
    # 이관된 절(header)이 AGENTS.md 에 없어야 한다.
    assert "## 0. opencode 실행 모델" not in text, (
        "AGENTS.md 에 실행 모델 절(§0)이 잔존 — pm-instructions.md 로 이관돼야 한다 (ADR-0069)")
    assert "## 3. 위임 규약" not in text, (
        "AGENTS.md 에 위임 규약 절(§3)이 잔존 — pm-instructions.md 로 이관돼야 한다 (ADR-0069)")
    # 위임 규약 세부(매핑 표 어휘)도 코어에 남지 않아야 한다.
    for moved in ("subagent_type", "role → subagent_type", "위임 = `task` tool 호출"):
        assert moved not in text, (
            f"AGENTS.md 에 위임 규약 세부 '{moved}' 잔존 — pm-instructions.md 로 이관 (ADR-0069)")
    # 공통 코어 잔류 절은 남아 있어야 한다 (thin 이지 empty 가 아님).
    for core in (
        "{{PROJECT_TAGLINE}}",              # 프로젝트 한 줄
        "## 1. 엔진 호출 규약",             # 엔진 호출[인코딩]
        "## 4. 작업 완료 부기",             # 완료 부기
        "## 5. PM 결정 권한",               # 결정 권한
        "## 6. 라이브 외부 행위 안전 가드",  # 안전 가드
        "### 프로젝트 고유 제약",           # @render agent 가 참조하는 named anchor
    ):
        assert core in text, f"AGENTS.md 공통 코어 절 '{core}' 누락 (ADR-0069·T-0401)"
    # harness-neutral — 특정 하네스 어휘는 별도 전달 채널에만 둔다.
    for hs in ("opencode", ".opencode", ".claude", "task tool"):
        assert hs not in text, (
            f"AGENTS.md 공통 코어에 harness-specific 어휘 '{hs}' 잔존 — harness-neutral 이어야 한다 (ADR-0069)")


def test_pm_instructions_has_execution_model_and_delegation():
    """pm-instructions.md 가 이관받은 실행 모델 + 위임 규약(2.1~2.7) + config 캐싱을 담는다 (ADR-0069·T-0401)."""
    assert PM_INSTRUCTIONS_MD.is_file(), (
        f"pm-instructions.md 부재: {PM_INSTRUCTIONS_MD} (ADR-0069 이관처 누락 · @source 전파 등록 대상)")
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    for marker in (
        "실행 모델",         # §1 opencode 실행 모델
        "task tool",         # 위임 1차 채널
        "pm primary",        # PM 실행 모델
        "재시작",            # config 1회 로드 캐싱 노트
        "위임 규약",         # §2 위임 규약
        "subagent_type",     # role 매핑
        "opencode run",      # §2.7 폴백
    ):
        assert marker in text, f"pm-instructions.md 에 이관 전문 마커 '{marker}' 누락 (ADR-0069·T-0401)"
    # 위임 규약 subsection 이 온전히(2.1~2.7) 이관됐는지 — 대표 앵커.
    for sub in ("### 2.1", "### 2.2", "### 2.7"):
        assert sub in text, f"pm-instructions.md 에 위임 규약 subsection '{sub}' 누락 (§3 전체 이관 · T-0401)"


def test_pm_instructions_is_free_form_free():
    """pm-instructions.md(@render)에 free-form 토큰 0 — render 활성 시 _assert_no_leak hard-fail 방지 (ADR-0030·T-0401).

    이관된 reviewer 프롬프트의 옛 `{{PROJECT_CONSTRAINTS}}` 는 pointer(AGENTS.md §프로젝트 고유 제약)로
    치환돼야 한다 — @render 파일은 operational 토큰({{PY}}·{{TEST_CMD}} 등)만 보유한다(code-reviewer.md 선례).
    """
    text = PM_INSTRUCTIONS_MD.read_text(encoding="utf-8")
    offenders = [tok for tok in _FREEFORM_TOKENS if tok in text]
    assert not offenders, (
        f"pm-instructions.md(@render)에 free-form 토큰 잔존 {offenders} — pointer 로 치환하라 "
        "(ADR-0030·code-reviewer.md 선례: 'AGENTS.md §프로젝트 고유 제약')")


def test_opencode_jsonc_registers_pm_instructions():
    """opencode.jsonc 가 pm-instructions.md 를 instructions 배열로 로드 등록한다 (ADR-0069·T-0401).

    공통 코어 AGENTS.md 는 opencode 가 자동 로드하지만, 이관된 opencode-고유 지침은 이 배열
    등록이 있어야 함께 로드된다(라이브 실측 PASS·spike §D3 ②). 주석 파싱 없이 substring 확인.
    """
    text = OPENCODE_JSONC.read_text(encoding="utf-8")
    assert '"instructions"' in text, "opencode.jsonc 에 instructions 키 부재 (ADR-0069·T-0401)"
    assert ".opencode/pm-instructions.md" in text, (
        "opencode.jsonc instructions 배열에 .opencode/pm-instructions.md 미등록 — 이관 지침이 로드 안 됨 (T-0401)")


# ── (e) blast-radius 재발 가드 — 사라진 AGENTS.md 앵커 참조 0 (T-0401 리뷰 must-fix) ──────────
# §0(실행 모델)·§3(위임 규약)이 pm-instructions.md 로 이관됐다. 출하 opencode 문서가 그 사라진
# 앵커를 "위임/실행 단일 진실"로 계속 가리키면 채택자가 없는 섹션으로 안내된다. 이 클래스는 "red
# 없는 prose drift"라 리뷰 전까지 어떤 테스트도 못 잡았다 → grep 기반 durable 가드로 닫는다.
# 정규식은 과단속 방지로 정확히: "AGENTS.md" 와 "§0"/"§3" 이 같은 참조 맥락(15자 내 근접)으로 붙은
# 경우만. 매칭 제외(정당): ADR-0006 §3·spike §3.2(AGENTS.md 아닌 앵커)·AGENTS.md §1/§5/§10(유효 앵커).
_STALE_AGENTS_ANCHOR_RE = re.compile(r"AGENTS\.md[^\n]{0,15}§\s*[03](?![0-9])")


def test_no_shipped_opencode_doc_points_to_removed_agents_anchors():
    """출하 opencode md 전수에 사라진 `AGENTS.md §0`(실행모델)/`§3`(위임규약) 앵커 참조 0 (ADR-0069·T-0401 리뷰 must-fix).

    §0·§3 이 pm-instructions.md(§1 실행·§2 위임)로 이관됐으므로, 어떤 출하 문서도 그 앵커를
    "단일 진실"로 가리키면 안 된다(채택자가 없는 섹션으로 안내됨). AGENTS.lite.md 는 이번 범위 밖
    (자족 압축 설계·인라인 위임 §3 유지)이라 제외한다.
    """
    opencode_root = REPO / "templates" / "opencode"
    offenders = []
    for p in sorted(
        path
        for path in repo_owned_paths(REPO, opencode_root.relative_to(REPO), mode=OWNED)
        if path.suffix == ".md"
    ):
        if p.name == "AGENTS.lite.md":
            continue  # 코디네이터 명시 범위 밖 (자족 압축·인라인 §3)
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _STALE_AGENTS_ANCHOR_RE.search(line):
                offenders.append(f"{p.relative_to(REPO).as_posix()}:{i}: {line.strip()}")
    assert not offenders, (
        "출하 opencode 문서가 이관된 AGENTS.md 앵커(§0 실행모델·§3 위임규약)를 계속 가리킴 — "
        "`.opencode/pm-instructions.md` §1(실행)·§2(위임)로 리다이렉트하라 (ADR-0069·T-0401 리뷰 must-fix):\n  "
        + "\n  ".join(offenders))


def test_stale_agents_anchor_guard_is_sensitive():
    """sensitivity — 가드 정규식이 stale 앵커를 실제로 잡고 정당 참조는 안 잡는다(non-vacuous·과단속 방지).

    catch: `AGENTS.md §3`/`§0`/`§3.7`(이관된 앵커). pass: ADR/spike §3·AGENTS.md §1/§5/§10(유효)·
    pm-instructions.md §2·AGENTS.md 공통 코어(§ 미인접).
    """
    catch = (
        "자세한 규약은 `AGENTS.md §3` 가 단일 진실",
        "build primary 폴백 — AGENTS.md §0.",
        "위임 규약 단일 진실 = `AGENTS.md §3`",
        "AGENTS.md §3.7 폴백",
    )
    passes = (
        "결정 근거는 ADR-0006(§3·D2)",
        "spike §3.2 참조",
        "사용법 full [`AGENTS.md`](AGENTS.md) §10 / ADR-0018",
        "엔진 호출은 AGENTS.md §1 인코딩",
        "`.opencode/pm-instructions.md` §2 위임 규약",
        "`AGENTS.md` 공통 코어와 함께 자동 로드",
    )
    for s in catch:
        assert _STALE_AGENTS_ANCHOR_RE.search(s), f"stale 앵커 미검출(vacuous 위험): {s!r}"
    for s in passes:
        assert not _STALE_AGENTS_ANCHOR_RE.search(s), f"정당 참조 오검출(과단속): {s!r}"
