"""위임 모델 설정 점검 (층: domain).

developer 와 code-reviewer 가 같은 기반 모델로 해소되는지(별칭 교차 포함) 판정해 경고 목록을 낸다. 막지 않는다.
"""
from __future__ import annotations

import re


# alias 정규화 테이블 키 접두 — `delegate.model_alias.<name> = m1, m2, …`.
_MODEL_ALIAS_PREFIX = "delegate.model_alias."


def _lint_role_model(conf: dict[str, str], role: str, tier: str = "normal") -> str | None:
    """lint 용 역할 모델 해소 — 설정 키만 읽고 **fail-loud 하지 않는다**(미설정=None·skip).

    resolve_delegate 는 미설정 시 fail-loud 라 lint(설정 점검)엔 부적합하다 — lint 는 강제가
    아니라 정합 권고이므로 미매핑 역할은 조용히 건너뛴다(경고 대상 아님)."""
    key = f"delegate.{role}" + (".hard" if tier == "hard" else "")
    model = (conf.get(f"{key}.model") or "").strip()
    return model or None


def _lint_alias_sets(conf: dict[str, str]) -> dict[str, set[str]]:
    """`delegate.model_alias.<name> = m1, m2, …` 를 (모델문자열 → 그 모델이 속한 alias명 **집합**)으로
    뒤집는다.

    서로 다른 표기(하네스별 이름·프로바이더 경로)의 같은 기반 모델을 alias 로 묶어 비교한다. 한 모델이
    **여러 alias 에 속할 수 있으므로 집합으로 모은다**(마지막 alias 가 덮어써 경고를 놓치던 문제 폐쇄·
    codex suggestion). 문자열 비교 + 명시 매핑 이상은 과설계 금지(family 자동추론·버전 파싱 없음)."""
    out: dict[str, set[str]] = {}
    for key, value in conf.items():
        if not key.startswith(_MODEL_ALIAS_PREFIX):
            continue
        alias = key[len(_MODEL_ALIAS_PREFIX):].strip()
        if not alias:
            continue
        for member in re.split(r"[,\s]+", value or ""):
            member = member.strip()
            if member:
                out.setdefault(member, set()).add(alias)
    return out


def _lint_models_match(a: str, b: str, alias_sets: dict[str, set[str]]) -> bool:
    """두 모델 문자열이 같은 기반 모델인가 — 동일 문자열이거나 **alias 집합이 교차**하면 True(
    하네스 무관).

    집합 교차라 한 모델이 여러 alias 에 속해도 공유 alias 를 놓치지 않는다(단일 대표 alias 덮어쓰기
    회귀 폐쇄). 비교는 alias **멤버십**(모델→속한 alias명)이지 이름-값이 아니므로, 실제 모델명이 우연히
    어떤 alias 명과 같은 문자열이어도 오인 매칭하지 않는다(collision-safe)."""
    if a == b:
        return True
    sa = alias_sets.get(a)
    sb = alias_sets.get(b)
    return bool(sa and sb and (sa & sb))


def lint_same_model(conf: dict[str, str]) -> list[tuple[str, str]]:
    """dev(normal+hard)와 code-reviewer 해소 모델이 같으면 (label, detail) 경고 리스트 반환.

    **하네스 무관 모델 문자열 비교**(+선택적 alias 동치류 교차) — 같은 기반 모델을 서로 다른 하네스로
    돌려도 맹점 공유는 동일하므로 `harness:model` 완전일치가 아니라 `.model` 문자열
    (또는 공유 alias)로 비교한다. 같으면 경고 1줄. **never-block** — lint 는 설정 점검이지 강제가
    아니다.

    **경고가 말하는 것과 말하지 않는 것**: 위임은 stateless 라 dev 와 reviewer 가 매번 별개 세션이고
    전사 공유가 0이다 — 즉 generate≠evaluate(구현하지 않은 주체가 검토)는 **모델이 같아도 성립**한다.
    이 경고는 독립성 위반을 알리는 게 아니라, 같은 모델이면 같은 맹점을 공유해 검출력이 낮아진다는
    **정도의 문제**를 표면화한다. 모델 선택이 제약된 형상(회사 기준 모델 고정 등)은 정상 운영이며
    이 경고를 해소 못 해도 결함이 아니다. 미설정 역할(reviewer 또는 특정 dev tier)은 조용히 skip(경고 대상 아님). **순수 함수**
    (I/O·board import 없음) — board lint 가 이 함수를 deep-import 로 호출해 advisory 로 표면화한다
    (순환 import 방지 — pm_delegate 는 board 를 import 하지 않는다)."""
    reviewer = _lint_role_model(conf, "code-reviewer")
    if reviewer is None:
        return []
    alias_sets = _lint_alias_sets(conf)
    findings: list[tuple[str, str]] = []
    for tier, label in (("normal", "developer"), ("hard", "developer.hard")):
        dev = _lint_role_model(conf, "developer", tier)
        if dev is None:
            continue
        if _lint_models_match(dev, reviewer, alias_sets):
            via = " (alias 경유)" if dev != reviewer else ""
            findings.append((
                f"delegate.{label}/code-reviewer",
                f"delegate.{label}(model={dev}) 와 delegate.code-reviewer(model={reviewer}) 가 "
                f"같은 모델로 해소됩니다{via} — 위임은 매번 새 세션이라 전사 공유가 없어 "
                "generate≠evaluate 자체는 성립하지만, 모델이 다르면 같은 맹점을 공유하지 않아 "
                "검출력이 는다. 선택 가능하면 서로 다른 모델을 권장한다(모델 선택이 제약된 형상도 "
                "정상·하네스 무관·never-block)."
            ))
    return findings
