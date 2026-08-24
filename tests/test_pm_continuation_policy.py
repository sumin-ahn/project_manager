"""T-0655 PM 중단 사유의 집합 소속·결론 극성 문면 가드."""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PM_ROLE = ROOT / ".project_manager" / "wiki" / "pm_role.md"
PM_PLAYBOOK = ROOT / ".project_manager" / "wiki" / "pm_playbook.md"

POLICY_SECTIONS = (
    (PM_ROLE, "## 결정 권한", "\n## "),
)

GROUP_ITEMS = {
    "유효 집합": (
        "사용자 명시 지시",
        "사용자 결정 게이트",
        "기술적 불가",
    ),
    "무효 집합": (
        "컨텍스트 잔량",
        "라운드·wave 상한",
        "티켓 미완",
        "남은 작업량",
        "세션 자기 판단",
    ),
    "미완 보고 판정": (
        "다음 행동 명시",
        "자기 수행 우선",
        "상한 이후 보고",
        "종료·축소 권한",
    ),
}
ALL_ITEMS = tuple(item for items in GROUP_ITEMS.values() for item in items)

GROUP_HEADER_RE = re.compile(
    r"\s*\*\*(?P<group>유효 집합|무효 집합|미완 보고 판정)\.\*\*\s*"
)
ITEM_RE = re.compile(r"\s*-\s+\*\*(?P<key>[^*]+)\*\*:\s*(?P<value>.+)")
CLAUSE_RE = re.compile(r"조건:\s*(?P<condition>.+?)\s+결론:\s*(?P<conclusion>.+)")

# 조건은 해당 항목 범위 안에서만 개념을 식별하고, 규범 극성은 짧은 결론 필드가 소유한다.
# 의도적 경계: 조건에 상충 문장을 삽입하면서 집합과 결론을 그대로 보존한 경우까지 자연어로
# 해석하지 않는다. 이 가드는 임의 의미 판정기가 아니라 집합 소속과 결론 극성 판정기다.
CONDITION_CONCEPTS = {
    "사용자 명시 지시": (("사용자",), ("명시", "분명"), ("지시", "요청"), ("종료", "중단", "끝내", "멈추")),
    "사용자 결정 게이트": (
        ("사용자",), ("결정", "승인"), ("보호 영역",), ("mission scope",), ("외부 비가역",), ("게이트",),
    ),
    "기술적 불가": (("자원",), ("부재", "없"), ("권한",), ("거부", "없"), ("수행할 수 없",)),
    "컨텍스트 잔량": (("컨텍스트",), ("잔량", "남아 있는"), ("작업 범위", "수행 범위"), ("중단",)),
    "라운드·wave 상한": (("라운드",), ("wave",), ("상한", "제한"), ("도달", "닿")),
    "티켓 미완": (("티켓",), ("미완", "완료되지 않")),
    "남은 작업량": (("작업량",), ("많", "크")),
    "세션 자기 판단": (("세션",), ("자기 판단",), ("정확한 상태",)),
    "다음 행동 명시": (("여기까지",), ("다음 행동", "다음 한 걸음"), ("명시", "밝히", "제시"), ("않", "없")),
    "자기 수행 우선": (("세션",), ("직접 수행",), ("수행할 수 있",)),
    "상한 이후 보고": (("라운드",), ("wave",), ("상한", "제한"), ("루프",), ("정지", "끝")),
    "종료·축소 권한": (("세션 종료",), ("작업 축소",), ("결정",)),
}

EXPECTED_CONCLUSIONS = {
    "사용자 명시 지시": "작업 중단 가능.",
    "사용자 결정 게이트": "작업 중단 가능.",
    "기술적 불가": "작업 중단 가능.",
    "컨텍스트 잔량": "이를 이유로 작업을 중단하면 규약 위반이다.",
    "라운드·wave 상한": "이를 이유로 작업을 중단하면 규약 위반이다.",
    "티켓 미완": "이를 이유로 작업을 중단하면 규약 위반이다.",
    "남은 작업량": "이를 이유로 작업을 중단하면 규약 위반이다.",
    "세션 자기 판단": "이를 이유로 작업을 중단하면 규약 위반이다.",
    "다음 행동 명시": "다음 행동 없는 미완 보고는 규약 위반이다.",
    "자기 수행 우선": "수행 가능한 행동을 남긴 미완 보고는 규약 위반이다.",
    "상한 이후 보고": "라운드를 더 열거나 board를 쓰지 않고 현재 티켓 상태와 실패 근거를 사용자에게 보고한다.",
    "종료·축소 권한": "세션 종료·작업 축소는 사용자 지시로만 한다.",
}

# 1R에서 통과시켜야 했던 2종과 추가 의미 보존 재서술 4종. 조건만 재서술하고
# 집합 소속·결론 극성은 보존한다.
REPHRASES = (
    (
        "1r-reviewer-example",
        "다음 행동 명시",
        '조건: 세션이 "여기까지"라고 보고하면서 다음 행동을 밝히지 않은 미완 보고. '
        "결론: 다음 행동 없는 미완 보고는 규약 위반이다.",
    ),
    (
        "1r-context-synonym",
        "컨텍스트 잔량",
        "조건: 남아 있는 컨텍스트의 양을 수행 범위나 중단 여부와 함께 관측한 상태. "
        "결론: 이를 이유로 작업을 중단하면 규약 위반이다.",
    ),
    (
        "user-directive-synonym",
        "사용자 명시 지시",
        "조건: 사용자가 작업을 끝내거나 멈추라고 분명히 요청한 경우. 결론: 작업 중단 가능.",
    ),
    (
        "gate-synonym",
        "사용자 결정 게이트",
        "조건: 보호 영역·mission scope·외부 비가역 행위가 사용자 승인을 요구하는 게이트에 이른 경우. "
        "결론: 작업 중단 가능.",
    ),
    (
        "technical-synonym",
        "기술적 불가",
        "조건: 필요한 자원이 없거나 권한이 없어 수행할 수 없는 경우. 결론: 작업 중단 가능.",
    ),
    (
        "report-synonym",
        "상한 이후 보고",
        "조건: 라운드 및 wave 제한에 닿아 해당 루프만 끝난 상태. "
        "결론: 라운드를 더 열거나 board를 쓰지 않고 현재 티켓 상태와 실패 근거를 사용자에게 보고한다.",
    ),
)

# 리뷰어가 제시한 의미 훼손 6종. 앞의 2종은 무효 항목을 유효 집합으로 교차 이동하고,
# 나머지 4종은 결론의 부정·금지·권한·계속 극성을 뒤집는다.
HARMFUL_MUTATIONS = (
    (
        "context-becomes-valid",
        "move",
        "컨텍스트 잔량",
        (
            "유효 집합",
            "조건: 컨텍스트 잔량을 작업 범위와 중단 결정의 유효한 입력으로 사용하는 경우. "
            "결론: 작업 중단 가능.",
        ),
    ),
    (
        "cap-becomes-stop-reason",
        "move",
        "라운드·wave 상한",
        (
            "유효 집합",
            "조건: 라운드·wave 상한 도달을 작업 종료 사유로 인정한 경우. 결론: 작업 중단 가능.",
        ),
    ),
    (
        "next-action-not-violation",
        "replace",
        "다음 행동 명시",
        '조건: 세션이 "여기까지"라고 알리면서 다음 행동을 명시하지 않은 미완 보고. '
        "결론: 다음 행동이 없어도 규약 위반이 아니다.",
    ),
    (
        "self-action-do-not-continue",
        "replace",
        "자기 수행 우선",
        "조건: 세션이 직접 수행할 수 있는 다음 행동이 남은 상태. "
        "결론: 수행 가능한 작업은 계속하지 않는다.",
    ),
    (
        "authority-without-user",
        "replace",
        "종료·축소 권한",
        "조건: 세션 종료 또는 작업 축소를 결정하는 경우. "
        "결론: 세션 종료·작업 축소는 사용자 지시가 없는 경우에만 한다.",
    ),
    (
        "report-becomes-discretion",
        "replace",
        "상한 이후 보고",
        "조건: 라운드·wave 상한 도달로 해당 루프가 정지한 상태. "
        "결론: 재설계·분할·다음 티켓 진행 여부는 세션 판단에 맡긴다.",
    ),
)


def _policy_section(path: Path, heading: str, next_heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.find(heading)
    assert start >= 0, f"{path}: 정책 절 누락: {heading}"
    rest = text[start + len(heading):]
    end = rest.find(next_heading)
    return rest if end < 0 else rest[:end]


def _parse_policy(section: str) -> dict[str, dict[str, tuple[str, str]]]:
    groups: dict[str, dict[str, tuple[str, str]]] = {}
    current_group: str | None = None
    for line in section.splitlines():
        group_match = GROUP_HEADER_RE.fullmatch(line)
        if group_match:
            current_group = group_match.group("group")
            assert current_group not in groups, f"중복 집합: {current_group}"
            groups[current_group] = {}
            continue
        item_match = ITEM_RE.fullmatch(line)
        if not item_match or current_group is None:
            continue
        key = item_match.group("key")
        assert key not in groups[current_group], f"중복 항목: {current_group}/{key}"
        clause_match = CLAUSE_RE.fullmatch(item_match.group("value"))
        assert clause_match, f"조건/결론 구조 누락: {current_group}/{key}"
        groups[current_group][key] = (
            clause_match.group("condition"),
            clause_match.group("conclusion"),
        )
    return groups


def _validate_policy(section: str) -> None:
    groups = _parse_policy(section)
    assert set(groups) == set(GROUP_ITEMS), "유효/무효/미완 집합 구조 불일치"
    for group, expected_items in GROUP_ITEMS.items():
        actual_items = groups[group]
        assert set(actual_items) == set(expected_items), (
            f"{group} 소속 불일치: missing={sorted(set(expected_items) - set(actual_items))}, "
            f"extra={sorted(set(actual_items) - set(expected_items))}"
        )
        for key in expected_items:
            condition, conclusion = actual_items[key]
            for alternatives in CONDITION_CONCEPTS[key]:
                assert any(term in condition for term in alternatives), (
                    f"{group}/{key}: 조건 개념 누락: {alternatives!r}"
                )
            assert conclusion == EXPECTED_CONCLUSIONS[key], (
                f"{group}/{key}: 결론 극성 불일치: {conclusion!r}"
            )


def _replace_item(section: str, key: str, value: str) -> str:
    pattern = re.compile(
        rf"(?P<prefix>^\s*-\s+\*\*{re.escape(key)}\*\*:\s*)[^\n]*$",
        re.MULTILINE,
    )
    replaced, count = pattern.subn(lambda match: match.group("prefix") + value, section)
    assert count == 1, f"교체 대상 항목 수 오류: {key}: {count}"
    return replaced


def _delete_item(section: str, key: str) -> str:
    pattern = re.compile(
        rf"^\s*-\s+\*\*{re.escape(key)}\*\*:[^\n]*(?:\n|$)",
        re.MULTILINE,
    )
    replaced, count = pattern.subn("", section)
    assert count == 1, f"삭제 대상 항목 수 오류: {key}: {count}"
    return replaced


def _move_item(section: str, key: str, target_group: str, value: str) -> str:
    without_item = _delete_item(section, key)
    header = re.compile(
        rf"^(?P<indent>\s*)\*\*{re.escape(target_group)}\.\*\*\s*$",
        re.MULTILINE,
    )
    moved, count = header.subn(
        lambda match: match.group(0) + f"\n{match.group('indent')}- **{key}**: {value}",
        without_item,
        count=1,
    )
    assert count == 1, f"이동 대상 집합 수 오류: {target_group}: {count}"
    return moved


def _apply_harmful_mutation(section: str, kind: str, key: str, payload) -> str:
    if kind == "move":
        target_group, value = payload
        return _move_item(section, key, target_group, value)
    assert kind == "replace"
    return _replace_item(section, key, payload)


@pytest.mark.parametrize("path,heading,next_heading", POLICY_SECTIONS, ids=("pm_role",))
def test_structured_stop_policy_is_complete(path, heading, next_heading):
    _validate_policy(_policy_section(path, heading, next_heading))


@pytest.mark.parametrize("path,heading,next_heading", POLICY_SECTIONS, ids=("pm_role",))
@pytest.mark.parametrize("key", ALL_ITEMS, ids=ALL_ITEMS)
@pytest.mark.parametrize("mutation", ("delete", "neutralize"))
def test_every_policy_concept_deletion_or_neutralization_is_red(
    path, heading, next_heading, key, mutation,
):
    section = _policy_section(path, heading, next_heading)
    mutated = _delete_item(section, key) if mutation == "delete" else _replace_item(
        section, key, "해당 없음.",
    )
    with pytest.raises(AssertionError):
        _validate_policy(mutated)


@pytest.mark.parametrize("path,heading,next_heading", POLICY_SECTIONS, ids=("pm_role",))
@pytest.mark.parametrize("case_id,kind,key,payload", HARMFUL_MUTATIONS, ids=[case[0] for case in HARMFUL_MUTATIONS])
def test_reviewer_meaning_reversals_are_red(path, heading, next_heading, case_id, kind, key, payload):
    section = _policy_section(path, heading, next_heading)
    mutated = _apply_harmful_mutation(section, kind, key, payload)
    with pytest.raises(AssertionError, match="소속 불일치|결론 극성 불일치"):
        _validate_policy(mutated)


@pytest.mark.parametrize("path,heading,next_heading", POLICY_SECTIONS, ids=("pm_role",))
@pytest.mark.parametrize("case_id,key,rephrased", REPHRASES, ids=[case[0] for case in REPHRASES])
def test_meaning_preserving_rephrases_are_green(path, heading, next_heading, case_id, key, rephrased):
    section = _replace_item(_policy_section(path, heading, next_heading), key, rephrased)
    _validate_policy(section)
