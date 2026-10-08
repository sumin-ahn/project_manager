"""위임 transport 규칙 한 문장 — 위임을 안내하는 표면 전부가 같은 규칙을 말한다 (T-0947).

규칙(R1): 리뷰(`code-reviewer`)는 하네스와 무관하게 엔진이 실행하고, 설계·구현·조사는 target
하네스 == PM 하네스면 native, 다르면 `pm_delegate.py` cross 다. 리뷰가 엔진 실행인 이유는 실행
위치 격리(스냅샷을 하네스 프로세스의 실행 root 로)와 라운드 상한·발산 판정이다 — native 위임 도구에는
작업 디렉터리 인자가 없다.

여기서 고정하는 성질:
  (1) R1 문장이 공용 카드·codex 카드·opencode 운영 지침·playbook·opencode 명령 파생본과
      lite 진입 문서 2벌·템플릿 README 3벌·세션 자동 로드 공통 코어 `AGENTS.md` 2벌·opencode PM 카드·
      codex 부트스트랩 카드 절에 있다.
  (2) 리뷰를 native 로 안내하는 문구(손 spawn·손 프롬프트·"더 저렴"·"하네스 네이티브 채널")가 위 표면과
      opencode `code-reviewer` 카드·codex `code-reviewer` 카드에 없다.
  (3) 카드의 `ticket prepare` 역할 목록은 엔진 선택지(developer·architect)와 같다.
  (4) 역방향 — 설계·구현 native 안내와 리뷰가 쓰는 native `code-reviewer` 카드 파일은 남아 있다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

SHARED_CARD = REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
CODEX_CARD = REPO / "templates" / "codex" / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"
OPENCODE_INSTRUCTIONS = REPO / "templates" / "opencode" / ".opencode" / "pm-instructions.md"
PLAYBOOK = REPO / ".project_manager" / "wiki" / "pm_playbook.md"
OPENCODE_COMMAND = REPO / "templates" / "opencode" / ".opencode" / "command" / "pm-dev-delegate.md"
CLAUDE_LITE = REPO / "templates" / "claude_code" / "CLAUDE.lite.md"
OPENCODE_LITE = REPO / "templates" / "opencode" / "AGENTS.lite.md"
CLAUDE_README = REPO / "templates" / "claude_code" / "README.md"
OPENCODE_README = REPO / "templates" / "opencode" / "README.md"
OPENCODE_REVIEWER_CARD = REPO / "templates" / "opencode" / ".opencode" / "agents" / "code-reviewer.md"
# PM 세션에 자동으로 실리는 진입 표면 — codex·opencode 공통 코어, opencode PM primary 카드, codex README.
CODEX_AGENTS = REPO / "templates" / "codex" / "AGENTS.md"
OPENCODE_AGENTS = REPO / "templates" / "opencode" / "AGENTS.md"
CODEX_README = REPO / "templates" / "codex" / "README.md"
OPENCODE_PM_CARD = REPO / "templates" / "opencode" / ".opencode" / "agents" / "pm.md"
CODEX_REVIEWER_CARD = REPO / "templates" / "codex" / ".codex" / "agents" / "code-reviewer.toml"
BOOTSTRAP = REPO / ".project_manager" / "tools" / "pm_bootstrap.py"

SURFACES = (
    SHARED_CARD, CODEX_CARD, OPENCODE_INSTRUCTIONS, PLAYBOOK, OPENCODE_COMMAND,
    CLAUDE_LITE, OPENCODE_LITE, CLAUDE_README, OPENCODE_README,
    CODEX_AGENTS, OPENCODE_AGENTS, CODEX_README, OPENCODE_PM_CARD,
)
# `ticket prepare` 명령 예시를 싣는 위임 카드(공용·codex·공용 파생본).
PREPARE_CARDS = (SHARED_CARD, CODEX_CARD, OPENCODE_COMMAND)

TRANSPORT_RULE = (
    "리뷰(`code-reviewer`)는 하네스와 무관하게 엔진이 실행한다"
    "(`pm_delegate.py --role code-reviewer --cluster`). 설계·구현·조사는 target 하네스 == PM "
    "하네스면 native, 다르면 `pm_delegate.py` cross 다."
)
# 리뷰를 native 로 안내하던 문구 — 손 spawn·손 프롬프트·비용 근거·native 역할 목록·native 폴백 실행.
NATIVE_REVIEW_GUIDANCE = (
    "같은 프로세스 계열이라 더 저렴",
    "subagent_type: code-reviewer",
    'agent_type="code-reviewer"',
    "`Agent`(code-reviewer)",
    "subagent_type=developer|code-reviewer",
    "`developer`|`code-reviewer`",
    "<developer|architect|code-reviewer>",
    "변경을 검토하라",
    "opencode run --agent code-reviewer",
    "code-reviewer native profile",
    "하네스 네이티브 채널",
    "subagent(developer / code-reviewer",
    "자식 프로세스 위임 없음",
)
# opencode `code-reviewer` 카드는 엔진 실행(`opencode run --agent code-reviewer`)이 쓰므로 그 실행
# 문구는 남고, PM 의 `task` 직접 호출 안내만 없어야 한다.
REVIEWER_CARD_NATIVE_GUIDANCE = ("subagent_type: code-reviewer", "1차 위임 경로")
ENGINE_PREPARE_ROLES = {"developer", "architect"}
NATIVE_REVIEWER_CARDS = (
    REPO / ".claude" / "agents" / "code-reviewer.md",
    REPO / "templates" / "codex" / ".codex" / "agents" / "code-reviewer.toml",
    REPO / "templates" / "opencode" / ".opencode" / "agents" / "code-reviewer.md",
)


def _flat(path: Path) -> str:
    """줄바꿈·들여쓰기를 공백 하나로 접는다 — 문장 판정이 줄 접기에 흔들리지 않게."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("surface", SURFACES, ids=lambda path: path.relative_to(REPO).as_posix())
def test_surface_states_the_transport_rule(surface):
    assert TRANSPORT_RULE in _flat(surface)


@pytest.mark.parametrize("surface", SURFACES, ids=lambda path: path.relative_to(REPO).as_posix())
def test_surface_has_no_native_review_guidance(surface):
    text = surface.read_text(encoding="utf-8")
    remaining = [phrase for phrase in NATIVE_REVIEW_GUIDANCE if phrase in text]
    assert remaining == []


def test_reviewer_card_has_no_native_task_guidance():
    text = OPENCODE_REVIEWER_CARD.read_text(encoding="utf-8")
    remaining = [phrase for phrase in REVIEWER_CARD_NATIVE_GUIDANCE if phrase in text]
    assert remaining == []
    assert "opencode run --agent code-reviewer" in text  # 역방향 — 엔진 실행 경로는 남는다


@pytest.mark.parametrize("card", PREPARE_CARDS, ids=lambda path: path.relative_to(REPO).as_posix())
def test_prepare_role_list_matches_the_engine_choices(card):
    lists = re.findall(r"ticket prepare (?:\\ )?--cluster <C-이름> --role <([^>]+)>", _flat(card))
    assert lists, "카드에 `ticket prepare` 역할 목록이 없다"
    for roles in lists:
        assert set(roles.split("|")) == ENGINE_PREPARE_ROLES


def test_design_and_implementation_native_guidance_remains():
    """역방향 — 리뷰만 빠졌고 설계·구현 native 위임 안내는 그대로다."""
    shared = SHARED_CARD.read_text(encoding="utf-8")
    assert "subagent_type: developer" in shared and "subagent_type: architect" in shared
    codex = CODEX_CARD.read_text(encoding="utf-8")
    assert 'agent_type="developer"' in codex and 'agent_type="architect"' in codex
    instructions = OPENCODE_INSTRUCTIONS.read_text(encoding="utf-8")
    assert "| developer | `developer` |" in instructions
    assert "| architect | `architect` |" in instructions


@pytest.mark.parametrize(
    "card", NATIVE_REVIEWER_CARDS, ids=lambda path: path.relative_to(REPO).as_posix(),
)
def test_native_reviewer_card_used_by_engine_execution_remains(card):
    """리뷰 실행(`opencode run --agent code-reviewer`·codex named profile)이 쓰는 카드는 남는다."""
    assert card.is_file()


def _codex_card_section() -> str:
    """codex 세션 `$pm-bootstrap` 카드 끝에 붙는 절(`pm_bootstrap._CODEX_CARD_SECTION`) — 이 절이 codex 위임
    규약의 유일 전달 채널이다. 모듈 전체를 싣지 않고 그 상수 할당만 평가한다."""
    import ast
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(target, "id", None) == "_CODEX_CARD_SECTION" for target in node.targets):
            return eval(compile(ast.Expression(node.value), str(BOOTSTRAP), "eval"))
    raise AssertionError("pm_bootstrap 에 _CODEX_CARD_SECTION 이 없다")


def test_codex_bootstrap_section_states_the_transport_rule():
    """codex 부트스트랩 카드 절은 R1 을 말하고, 리뷰를 세션 내 spawn 대상으로 열거하지 않는다."""
    section = re.sub(r"\s+", " ", _codex_card_section())
    assert TRANSPORT_RULE in section
    remaining = [phrase for phrase in (*NATIVE_REVIEW_GUIDANCE, "code-reviewer,researcher}",
                                       "위임 = 세션 내 spawn") if phrase in section]
    assert remaining == []
    assert "{architect,developer,researcher}" in section  # 역방향 — 설계·구현·조사 native 안내는 남는다


def test_codex_reviewer_card_describes_engine_execution():
    """codex `code-reviewer` 카드는 세션 내 spawn 대상이라 말하지 않고 엔진 실행을 말한다."""
    text = _flat(CODEX_REVIEWER_CARD)
    assert "in-session spawn" not in text
    assert "pm_delegate.py --role code-reviewer --cluster" in text

