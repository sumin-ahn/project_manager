"""티켓 설계 단계 판정 (층: domain).

frontmatter `design:` 값 정규화, `## 설계` 절 4항목 충전 판정, developer 라운드 준비 전 설계 근거 판정을 소유한다.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pm_engine.domain import markdown_text, ticket_rules


# ── 티켓 설계 단계 (design 필드 · 설계 절 · claim 게이트) ──────────────
#
# 설계 결함이 코드 리뷰 라운드로 전가되는 클래스를 라이프사이클 *앞단*에서 차단한다(근거:
# 티켓 인터페이스가 명세한 입력 가정이 실환경과 어긋난 채 구현으로 전진해 리뷰 12라운드로도 미수렴).
# 판정은 **frontmatter 명시값 기준**(기계)이고 설계 *내용* 의 옳고 그름은 사람/리뷰어가 본다 —
# 엔진은 필드값과 설계 절 충전 여부만 본다.
# **전 티켓 강제가 아니다**: 자동 `required` 는 estimate=large 뿐이고 나머지는 `n/a` 기본이라
# 설계 오버헤드가 새 비용이 되지 않는다(touches 패턴 자동 required 는 운용 후 판단).
DESIGN_REQUIRED = "required"   # 설계 절 완성 + done 승격 전에는 claim 차단
DESIGN_DONE = "done"           # 설계 절 완성 + 설계 검토 끝 (PM 수동 기입)
DESIGN_NA = "n/a"              # 설계 단계 비대상. **필드 부재 = 이 값**(구세대 티켓 하위호환)
DESIGN_INVALID = "invalid"     # 위 3형식 어디에도 안 맞는 값(오타로 게이트가 조용히 꺼지는 것 방지)

# 사람 표면(--design help·거부 메시지)의 값 형식 — 한 곳에서 렌더한다. **면제 경로는 없다**:
# 설계 단계를 건너뛰는 값을 두면 그 티켓만 라운드 순번이 어긋나고(설계 라운드가 빠진다) 면제가
# 곧 우회 플래그가 된다. 설계가 몇 줄이면 몇 줄로 쓰고 `done` 으로 올린다.
DESIGN_VALUE_FORMS = "required | done | n/a"
_DESIGN_SECTION = "## 설계"

# `_template.md` 설계 절 하위 항목의 뼈대 문장 — 채우면 사라지는 리터럴이라 오탐 0(`_PLACEHOLDERS`
# 동형). **별도 집합인 이유**: 설계 절은 `design: required|done` 티켓만 채운다 — `n/a` 티켓이
# 뼈대를 그대로 둔 건 정상이라(`## 메모` 와 같은 부류) 전 티켓 공통 집합에 넣으면 안 된다.
_DESIGN_PLACEHOLDERS: tuple[str, ...] = (
    "무엇으로 실측했는지",                              # 경계 실측 미충전
    "지켜야 하는 성질을 문장으로",                       # 불변식 미충전
    "무한 표면이면 설계를 기각하고 범위를 좁힌다",         # 표면 상한 미충전
    "불변식을 고정할 케이스 (경계값·실패 경로)",          # 테스트 전략 미충전
)

# 미충전 항목 이름 — 경고 1줄에 "무엇이 비었나" 를 싣는다(`_DESIGN_PLACEHOLDERS` 와 같은 순서).
_DESIGN_PLACEHOLDER_LABELS: tuple[str, ...] = (
    "경계 실측", "불변식", "표면 상한", "테스트 전략")


def _design_state(design: Any) -> str:
    """frontmatter `design:` 값 → 정규화 상태 (required/done/n/a/invalid).

    필드 부재·빈값은 `n/a` — 구세대 티켓(설계 절도 필드도 없음)은 마이그레이션 없이 무영향이다.
    형식에 안 맞는 값은 `n/a` 로 삼키지 않고 `invalid` 로 세운다 — 오타 하나로 게이트가 조용히
    꺼지지 않게(fail-loud). 폐지된 면제 값도 이 축으로 떨어져 `board.py design` 이 거부하고
    lint 가 잔존을 보인다.
    """
    if design is None:
        return DESIGN_NA
    text = str(design).strip()
    if not text:
        return DESIGN_NA
    lowered = text.lower()
    if lowered in (DESIGN_REQUIRED, DESIGN_DONE, DESIGN_NA):
        return lowered
    return DESIGN_INVALID


def _design_item_values(section: str) -> dict[str, str]:
    """설계 절 → {항목 이름: 그 항목의 값 텍스트} (선언 안 된 항목은 키 자체가 없다).

    항목 줄은 `- **경계 실측**: <값>` 형태이고, 값은 그 줄의 나머지 + **다음 항목 줄 직전까지**의
    이어지는 줄이다 — 값을 하위 불릿으로 적는 실 표기(`- **테스트 전략**:` 다음 줄부터 케이스
    나열)를 '빈 값'으로 오판하지 않게 한다. 항목 이름은 `_DESIGN_PLACEHOLDER_LABELS` 단일
    진실이라 라벨이 바뀌면 판정도 같이 바뀐다(사본 0)."""
    label_res = [
        (label, re.compile(rf"^\s*[-*+]\s*\**\s*{re.escape(label)}\s*\**\s*[:：](?P<value>.*)$"))
        for label in _DESIGN_PLACEHOLDER_LABELS
    ]
    values: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in section.splitlines():
        matched = next(
            ((label, m) for label, pattern in label_res
             if (m := pattern.match(line)) is not None), None)
        if matched is not None:
            label, match = matched
            current = values.setdefault(label, [])
            current.append(match.group("value"))
            continue
        if current is not None:
            current.append(line)
    return {label: "\n".join(lines).strip() for label, lines in values.items()}


def _design_section_gaps(body: str) -> list[str]:
    """`## 설계` 절의 미충전 지점 이름 — 절 부재는 절 이름, 충전 완료는 빈 리스트.

    항목마다 **한 판정**을 낸다(우선순위: 항목 부재 > 값 없음 > 뼈대 잔존):

      - **항목 부재**·**값 없음** — placeholder 부재만 보면 뼈대를 *지우는 것*만으로 게이트가
        열린다(빈 `## 설계` 절이나 산문 한 줄짜리 절이 `design: done` 을 통과했다). 4항목
        (`_DESIGN_PLACEHOLDER_LABELS`)의 존재와 비어 있지 않은 값을 함께 본다.
      - **뼈대 잔존** — 토큰 스캔 범위는 **설계 절 슬라이스**다(본문 전체가 아니다). 전체 스캔은
        설계 단계를 다루는 후속 티켓이 다른 절에서 뼈대 문장을 인용하기만 해도 "미충전"으로
        읽는다. 토큰은 항목 값이 아니라 절 전체에서 찾는다 — 라벨을 지우고 뼈대 문장만 남긴
        형상도 그 항목의 미충전으로 잡힌다.

    절 슬라이싱은 DoD 판정과 같은 공용 슬라이서를 쓰므로 펜스 안에 인용된 헤딩도 절 시작으로
    오인하지 않는다. **존재 축도 같은 규율이다**: 절 시작은 `## 설계` 헤딩 줄(괄호 부제 허용)
    이어야 한다(`exact`). 자유 접두 일치면 `## 설계 검토 이력` 같은 다른 절이 설계 절로 잡혀,
    산문 한 줄만으로 `design: done` 이 통과한다.
    """
    section = markdown_text._section_text(body, _DESIGN_SECTION, exact=True)
    if section is None:
        return [f"{_DESIGN_SECTION} 절 부재"]
    prose = markdown_text._strip_code(section)
    values = _design_item_values(prose)
    gaps: list[str] = []
    for token, label in zip(_DESIGN_PLACEHOLDERS, _DESIGN_PLACEHOLDER_LABELS):
        value = values.get(label)
        if value is None:
            gaps.append(f"{label} 항목 부재")
        elif not value:
            gaps.append(f"{label} 값 없음")
        elif token in prose:
            gaps.append(label)
    return gaps


def _design_issues(tid: str, body: str, design: Any) -> list[tuple[str, str, str]]:
    """설계 단계 판정 — `design:` 값 + `## 설계` 절 충전 상태를 **1줄**로 요약한다.

    claim 게이트 · authoring 게이트(new/promote) · 전역 lint advisory 가 공유하는 단일 깔때기다.
    반환은 0 또는 1건(kind=`design-pending`) — 전역 lint 는 "경고 1줄", 게이트는 그 1줄을 차단
    사유로 쓴다. `n/a`(필드 부재 포함)는 항상 0건이라 설계 단계가 무영향이다.

    `required` 는 설계 절이 다 채워져 있어도 1건을 낸다 — `done` 승격(설계 검토를 사람이 끝냈다는
    선언)이 남아 있기 때문이다. 반대로 `done` 인데 설계 절이 비었으면 필드값과 절 내용이 어긋난
    상태라 역시 1건이다(엔진이 보는 건 필드값과 절 존재뿐).
    """
    state = _design_state(design)
    if state == DESIGN_INVALID:
        return [(tid, "design-pending",
                 f"design 값 인식 불가: {str(design)!r} — {DESIGN_VALUE_FORMS} 중 하나")]
    if state not in (DESIGN_REQUIRED, DESIGN_DONE):
        return []
    gaps = _design_section_gaps(body)
    if state == DESIGN_DONE:
        if not gaps:
            return []
        return [(tid, "design-pending",
                 f"design: {DESIGN_DONE} 인데 설계 절 미충전({' · '.join(gaps)}) — "
                 f"필드값과 절 내용이 어긋남")]
    unfilled = f"설계 절 미충전({' · '.join(gaps)}) · " if gaps else ""
    return [(tid, "design-pending",
             f"design: {DESIGN_REQUIRED} — {unfilled}설계 절 완성 후 "
             f"`design: {DESIGN_DONE}` 으로 승격")]


# developer 라운드 준비 게이트의 해소 안내 2종. 문구는 여기 한 곳뿐이라(재작성 0)
# `_DESIGN_SECTION`·`_DESIGN_PLACEHOLDER_LABELS`·`DESIGN_DONE` 을 그대로 인용한다.
# `T-NNNN`(괄호 없음)은 그대로 둔다 — pm_delegate 쪽 `<T-NNNN>` 치환 문자열과 다른 표기라야,
# 실 ticket id 를 아는 진입점(치환됨)과 모르는 진입점(치환 안 됨)의 사유 문자열이 우연히
# 갈리지 않는다(세 진입점 파리티 단언의 전제). **면제 항목은 없다** — 설계 단계를 건너뛰는
# 길이 있으면 그 티켓만 라운드 순번이 어긋난다.
_DESIGN_EVIDENCE_REMEDY = (
    "해소: (1) architect 라운드를 회수(시드 그대로면 근거 아님) · "
    f"(2) `design: {DESIGN_DONE}` + `{_DESIGN_SECTION}` 절 4항목"
    f"({'·'.join(_DESIGN_PLACEHOLDER_LABELS)}) 충전"
)


def design_evidence_problem(tid: str, ticket_text: str, rounds: Sequence) -> str | None:
    """developer 라운드 준비 직전 설계 근거 판정 — 통과면 None, 아니면 거부 사유 1줄.

    "PM 초안 → architect 점검 → PM 비준" 3단 규율이 산문에만 있어 위임 준비 시점에 아무것도
    막지 않던 자리를 여기서 잠근다. 통과 근거는 둘 중 하나다: (1) `rounds` 에 `pending=False`
    (시드 그대로가 아닌 실 산출)인 architect 라운드가 1건 이상(시드 그대로는 근거가 아니다) ·
    (2) `design: done` 且 `## 설계` 절 4항목 충전(`_design_issues` 0건). 둘 다 없으면 "기록 없는
    건너뜀"이라 거부한다 — 면제 경로는 없다.

    `ticket_text` 파싱 실패·`design` 값 인식 불가는 판정불능이라 통과로 삼키지 않고 거부한다
    (fail-loud) — architect 실산출이 있어도 마찬가지다. 그래서 파싱·`design` 값 유효성 검사를
    architect shortcut 보다 **먼저** 수행한다: 손상된 명세 위에서는 architect 실산출 근거가 있어도
    판정 자체가 불능이라 통과로 삼킬 수 없다. `rounds` 는 호출자가 이미 로드해 손에 든 것을
    그대로 받는다(재로드 0) — 시드 seam(`render_ticket_growth_section_seed`)의 developer 분기가
    예약 직전 라운드 전체를 이미 들고 있다. 파싱 소스 식별자에 실 tid 를 싣지 않는 이유는
    `_DESIGN_EVIDENCE_REMEDY` 와 같다 — 이 함수를 부르는 시드 seam 은 실 ticket id 를 모른다.
    """
    # PyYAML 은 판정할 때 읽는다(`ticket_rules._parse_ticket_text` 와 같은 규칙). 이 모듈을 import
    # 하는 위임 채널 가드의 pm_delegate 가 PyYAML 없는 훅 인터프리터에서 모듈 적재로 멈추지 않게 한다.
    import yaml

    try:
        fm, body = ticket_rules._parse_ticket_text(ticket_text, "<티켓 명세>")
    except (ValueError, yaml.YAMLError) as exc:
        # ValueError = frontmatter 구획 자체가 없거나 안 닫힘(`_parse_ticket_text`),
        # yaml.YAMLError = 구획은 있는데 스칼라 문법 위반(예: 콜론 포함 값을 인용 없이 쓴
        # `title: a: b`) — 둘 다 판정불능이라 같은 취급이다.
        return (
            f"developer 라운드 준비 거부 — 명세 파싱 실패로 설계 근거를 판정할 수 없음: "
            f"{exc} — {_DESIGN_EVIDENCE_REMEDY}"
        )
    design = fm.get("design")
    state = _design_state(design)
    if state == DESIGN_INVALID:
        return (
            f"developer 라운드 준비 거부 — design 값 인식 불가: {str(design)!r} — "
            f"{_DESIGN_EVIDENCE_REMEDY}"
        )
    if any(item.role == "architect" and not item.pending for item in rounds):
        return None
    if state == DESIGN_DONE:
        issues = _design_issues(tid, body, design)
        if not issues:
            return None
        return f"developer 라운드 준비 거부 — {issues[0][2]} — {_DESIGN_EVIDENCE_REMEDY}"
    missing = f"architect 라운드 선행도 design 완료 선언도 없음(design: {state})"
    return f"developer 라운드 준비 거부 — {missing} — {_DESIGN_EVIDENCE_REMEDY}"
