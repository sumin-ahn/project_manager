"""판단 원칙 조회와 행동 직전 recall 판정."""
from __future__ import annotations
import re
from pm_engine.infra import principle_store
from pm_engine.domain import principle_rules


def load(home) -> tuple[principle_rules.Rule, ...]:
    """Home의 출하층과 로컬층을 합성한 규칙을 반환한다. 파손 건수는 judge_recall 결과로 제공한다."""
    rules, _broken = principle_store._load_all(home)
    return rules


def judge_recall(home, *, on: str, text: str, seen=None) -> dict | None:
    """일치한 미주입 규칙의 count·keys·text와 파손 건수 broken을 반환한다.

    기존 레지스트리 판독·정규식 실패는 비차단 값으로 처리한다. 실행 패키지 import 오류는 호출 전 노출된다."""
    if on not in principle_rules.ON_VALUES:
        return None
    rules, broken = principle_store._load_all(home)
    seen_keys = set(seen) if seen else set()
    matched: list[tuple[str, str]] = []
    for rule in rules:
        if rule.on != on:
            continue
        key = f"{rule.on}:{rule.match}"
        if key in seen_keys:
            continue
        try:
            hit = re.search(rule.match, text or "")
        except re.error:
            broken += 1
            continue
        if hit:
            matched.append((key, rule.text))
    if not matched and (not broken):
        return None
    result: dict = {
        "count": len(matched),
        "keys": [key for key, _ in matched],
        "text": "",
    }
    segments: list[str] = []
    if matched:
        body = "\n".join((f"- {rule_text}" for _, rule_text in matched))
        rendered = f"{principle_rules._INJECT_PREFIX} {body}"
        if len(rendered) > principle_rules._MAX_INJECT_CHARS:
            rendered = f"{principle_rules._INJECT_PREFIX} 매칭 {len(matched)}건 — 문안 상한 초과로 값만 표시"
        segments.append(rendered)
    if broken:
        result["broken"] = broken
        segments.append(
            f"{principle_rules._INJECT_PREFIX} 레지스트리 파손 항목 {broken}건 — 판정에서 제외"
        )
    result["text"] = "\n".join(segments)
    return result
