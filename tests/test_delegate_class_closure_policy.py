"""T-0665 developer 위임의 클래스 전수 열거·역방향 확인 문면 가드."""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLAUDE_SKILL = ROOT / ".claude/skills/pm-dev-delegate/SKILL.md"
CODEX_SKILL = ROOT / "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md"

DELEGATE_SURFACES = (
    CLAUDE_SKILL,
    ROOT / "templates/claude_code/.claude/skills/pm-dev-delegate/SKILL.md",
    ROOT / "templates/opencode/.claude/skills/pm-dev-delegate/SKILL.md",
    CODEX_SKILL,
)
PLAYBOOK_SURFACES = (
    ROOT / ".project_manager/wiki/pm_playbook.md",
    ROOT / "templates/claude_code/.project_manager/wiki/pm_playbook.md",
    ROOT / "templates/codex/.project_manager/wiki/pm_playbook.md",
    ROOT / "templates/opencode/.project_manager/wiki/pm_playbook.md",
)

ENUMERATION_HEADING = "클래스 전수 열거 의무"
REVERSE_HEADING = "역방향 확인 의무"

# 문장 전체가 아니라 정책을 구성하는 항목·개념을 계약으로 둔다. 같은 의미의 문면 재서술은 허용한다.
POLICY_CONCEPTS = {
    "enumeration-heading": ((ENUMERATION_HEADING,),),
    "before-implementation": (("구현 전", "구현 전에"),),
    "defect-class": (("결함 클래스", "결함의 클래스"),),
    "entrypoint": (("진입점",),),
    "platform": (("플랫폼",),),
    "failure-mode": (("실패 모드",),),
    "call-path": (("호출 경로",),),
    "enumerate-all": (("전수 나열",),),
    "report": (("보고",),),
    "close-all": (("전부 처리", "전부 닫"),),
    "reported-shape-incomplete": (("보고된 형상",), ("미완",)),
    "impossible-boundary": (("불가능",), ("경계",)),
    "scope-boundary": (("스코프",), ("티켓 밖",), ("금지", "확대하지 않")),
    "reverse-heading": ((REVERSE_HEADING,),),
    "opposite-failure-assertion": (("반대 방향 실패",), ("단언",)),
    "tighten-overbinding": (("느슨함",), ("조인",), ("과결속",), ("확인",)),
    "loosen-omission": (("조임",), ("푼",), ("누락",), ("확인",)),
    "block-normal-use": (("차단",), ("정상 사용",), ("확인",)),
    "instance-report": (("열거한 인스턴스 목록",), ("각각의 처리",)),
}


def _read(path: Path) -> str:
    assert path.is_file(), f"정책 표면 없음: {path}"
    return path.read_text(encoding="utf-8")


def _concept_inventory(text: str) -> set[str]:
    found = set()
    for name, groups in POLICY_CONCEPTS.items():
        if all(any(term in text for term in alternatives) for alternatives in groups):
            found.add(name)
    return found


def _policy_errors(text: str) -> list[str]:
    expected = set(POLICY_CONCEPTS)
    errors = [
        f"개념 누락: {name}"
        for name in sorted(expected - _concept_inventory(text))
    ]

    # 의미 반전 백스톱. 개별 의무 항목에서 금지·부정 극성이 나타나면 개념 토큰이 남아도 실패한다.
    polarity_checks = (
        ("보고된 형상", ("충분", "완료로 본", "미완이 아니")),
        ("반대 방향 실패", ("단언하지 않", "확인하지 않", "확인 불필요")),
        ("과결속", ("확인하지 않", "확인 불필요", "생략")),
        ("누락", ("확인하지 않", "확인 불필요", "생략")),
        ("정상 사용", ("확인하지 않", "확인 불필요", "생략")),
    )
    for anchor, forbidden in polarity_checks:
        for line in text.splitlines():
            if anchor in line and any(term in line for term in forbidden):
                errors.append(f"의미 반전: {anchor}")
                break
    return errors


@pytest.mark.parametrize("path", DELEGATE_SURFACES, ids=lambda path: str(path.relative_to(ROOT)))
def test_all_developer_delegate_surfaces_hold_class_closure_policy(path: Path):
    """Claude·Codex·OpenCode 출하 표면 모두 두 의무와 보고 항목을 보유한다."""
    assert not (errors := _policy_errors(_read(path))), f"{path}: {errors}"


@pytest.mark.parametrize("path", PLAYBOOK_SURFACES, ids=lambda path: str(path.relative_to(ROOT)))
def test_all_playbook_surfaces_hold_class_closure_policy(path: Path):
    """방법론 기준과 3개 출하 사본이 같은 정책 개념을 보유한다."""
    assert not (errors := _policy_errors(_read(path))), f"{path}: {errors}"


def test_claude_and_codex_delegate_wording_is_concept_equivalent():
    """하네스별 실행 문면은 달라도 developer 정책 항목의 의미 집합은 동일하다."""
    claude = _concept_inventory(_read(CLAUDE_SKILL))
    codex = _concept_inventory(_read(CODEX_SKILL))
    assert claude == codex == set(POLICY_CONCEPTS)


def test_playbook_rejects_report_without_enumerated_instance_treatment():
    """라운드 프로토콜이 목록 없는 완료 보고의 PM 반려까지 명시한다."""
    text = _read(PLAYBOOK_SURFACES[0])
    assert "목록이 없으면" in text
    assert "PM 이 반려" in text


@pytest.mark.parametrize("heading", (ENUMERATION_HEADING, REVERSE_HEADING))
def test_guard_rejects_removed_policy_section(heading: str):
    """두 절 중 하나를 지운 문면은 가드 판정에서 red다."""
    text = _read(CLAUDE_SKILL)
    mutated = text.replace(heading, "삭제된 정책 절", 1)
    errors = _policy_errors(mutated)
    assert errors
    assert any("heading" in error for error in errors)


def test_guard_accepts_concept_preserving_rephrases():
    """항목 의미를 보존한 개조식 재서술은 문장 원문과 달라도 green이다."""
    text = _read(CLAUDE_SKILL)
    mutated = (
        text.replace("구현 전에", "구현 전", 1)
        .replace("인스턴스 전부 처리", "인스턴스 전부 닫음", 1)
        .replace("티켓 밖 기능은 포함 금지", "티켓 밖 기능으로 확대하지 않음", 1)
    )
    assert not _policy_errors(mutated)


@pytest.mark.parametrize(
    ("before", "after", "expected_error"),
    (
        ("보고된 형상만 처리한 결과는 미완.", "보고된 형상만 처리해도 충분.", "reported-shape-incomplete"),
        ("과결속 확인.", "과결속 확인 불필요.", "의미 반전: 과결속"),
        ("정상 사용 차단 확인.", "정상 사용 차단 확인 불필요.", "의미 반전: 정상 사용"),
    ),
)
def test_guard_rejects_opposite_policy_polarity(before: str, after: str, expected_error: str):
    """핵심 단어가 남아도 완료·확인 극성을 뒤집은 문면은 red다."""
    text = _read(CLAUDE_SKILL)
    assert before in text
    errors = _policy_errors(text.replace(before, after, 1))
    assert any(expected_error in error for error in errors), errors
