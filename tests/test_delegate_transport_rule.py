"""위임 transport 규칙 한 문장 — 위임을 안내하는 표면 전부가 같은 규칙을 말한다 (T-0947 · T-0950).

규칙(R1): 설계·구현·리뷰·조사는 target 하네스 == PM 하네스면 native, 다르면 `pm_delegate.py` cross 다.
리뷰도 역할 예외가 없다 — 같은 하네스면 `ticket prepare --cluster --role code-reviewer` 가 만든 엔진
프롬프트 파일을 native 서브에이전트에 넘기고 돌려받는다.

여기서 고정하는 성질:
  (1) R1 문장이 공용 카드·codex 카드·opencode 운영 지침·playbook·opencode 명령 파생본과
      lite 진입 문서 2벌·템플릿 README 3벌·세션 자동 로드 공통 코어 `AGENTS.md` 2벌·opencode PM 카드·
      codex 부트스트랩 카드 절에 있다.
  (2) 리뷰를 엔진 전용으로 안내하던 문구(하네스와 무관한 엔진 실행·격리 스냅샷·분리 실행·대기)가 위
      표면과 `pm_principles.md` 에 없다.
  (3) 카드의 `ticket prepare` 역할 목록 합집합은 엔진 선택지(developer·architect·code-reviewer)와 같다.
  (4) 설계·구현·리뷰 native 안내와 리뷰가 쓰는 native `code-reviewer` 카드 파일이 있다.
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
    "설계·구현·리뷰·조사는 target 하네스 == PM 하네스면 native, 다르면 `pm_delegate.py` cross 다."
)
PRINCIPLES = REPO / ".project_manager" / "wiki" / "pm_principles.md"
# 리뷰를 엔진 전용으로 안내하던 문구 — 하네스 무관 엔진 실행·이유 설명·준비 제외·격리 트리·분리 실행·대기.
ENGINE_ONLY_REVIEW_RULE = (
    "하네스와 무관하게 엔진이 실행",
    "리뷰만 엔진",
    "리뷰는 준비 대상이 아니다",
    "격리 스냅샷",
    "cluster wait",
    "--background",
)
ENGINE_PREPARE_ROLES = {"developer", "architect", "code-reviewer"}
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


@pytest.mark.parametrize(
    "surface", (*SURFACES, PRINCIPLES), ids=lambda path: path.relative_to(REPO).as_posix(),
)
def test_surface_has_no_engine_only_review_rule(surface):
    """AT-017 — 리뷰를 엔진 전용으로 안내하는 문구가 안내 표면 13개와 `pm_principles.md` 에 없다."""
    text = surface.read_text(encoding="utf-8")
    remaining = [phrase for phrase in ENGINE_ONLY_REVIEW_RULE if phrase in text]
    assert remaining == []


def test_reviewer_card_states_both_execution_paths():
    """opencode `code-reviewer` 카드는 같은 하네스면 PM 의 `task`, 다르면 엔진의 `opencode run` 을 말한다."""
    text = _flat(OPENCODE_REVIEWER_CARD)
    assert "PM 은 리뷰를 `task` 로 호출하지 않는다" not in text
    assert "PM 이 `task` 로" in text
    assert "opencode run --agent code-reviewer" in text


@pytest.mark.parametrize("card", PREPARE_CARDS, ids=lambda path: path.relative_to(REPO).as_posix())
def test_prepare_role_list_matches_the_engine_choices(card):
    """카드의 준비 명령 역할(자리표시 목록과 리터럴 역할)의 합집합이 엔진 선택지와 같다."""
    lists = re.findall(
        r"ticket prepare (?:\\ )?--cluster C-<ticket-group> --role (<[^>]+>|[a-z-]+)", _flat(card),
    )
    assert lists, "카드에 `ticket prepare` 역할 목록이 없다"
    roles = {role for item in lists for role in item.strip("<>").split("|")}
    assert roles == ENGINE_PREPARE_ROLES


def test_native_guidance_covers_design_implementation_and_review():
    """설계·구현·리뷰 native 위임 안내가 세 하네스 표면에 있다."""
    shared = SHARED_CARD.read_text(encoding="utf-8")
    for role in ("developer", "architect", "code-reviewer"):
        assert f"subagent_type: {role}" in shared
    codex = CODEX_CARD.read_text(encoding="utf-8")
    for role in ("developer", "architect", "code-reviewer"):
        assert f'agent_type="{role}"' in codex
    instructions = OPENCODE_INSTRUCTIONS.read_text(encoding="utf-8")
    for role in ("developer", "architect", "code-reviewer"):
        assert f"| {role} | `{role}` |" in instructions


@pytest.mark.parametrize(
    "card", NATIVE_REVIEWER_CARDS, ids=lambda path: path.relative_to(REPO).as_posix(),
)
def test_native_reviewer_card_remains(card):
    """리뷰 실행(native 서브에이전트·`opencode run --agent code-reviewer`·codex named profile)이 쓰는 카드."""
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
    """codex 부트스트랩 카드 절은 R1 을 말하고 리뷰를 세션 내 spawn 대상에 포함한다."""
    section = re.sub(r"\s+", " ", _codex_card_section())
    assert TRANSPORT_RULE in section
    remaining = [phrase for phrase in ENGINE_ONLY_REVIEW_RULE if phrase in section]
    assert remaining == []
    assert "{architect,code-reviewer,developer,researcher}" in section


def test_codex_reviewer_card_describes_native_and_cross_execution():
    """codex `code-reviewer` 카드는 엔진 전용 실행을 말하지 않고 두 실행 경로를 말한다."""
    text = _flat(CODEX_REVIEWER_CARD)
    assert "하네스와 무관하게" not in text
    assert "PM 이 세션 안에서 띄우지 않는다" not in text
    assert "spawn_agent" in text
