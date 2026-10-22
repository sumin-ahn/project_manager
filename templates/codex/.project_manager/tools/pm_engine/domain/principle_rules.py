"""판단 원칙 스키마·정규식·중복 키 규칙."""
from __future__ import annotations
from typing import NamedTuple
import re

ON_VALUES = ("shell", "edit", "delegate", "prompt")
_BUNDLE_RE = re.compile("^#{2,4}\\s+(.+?)\\s*$")
_ITEM_RE = re.compile("^-\\s+(.*)$")
_TAG_RE = re.compile(
    "^`\\[(?P<on>shell|edit|delegate|prompt):\\s+(?P<match>.+?)\\]`\\s+(?P<body>.+)$"
)
_MAX_INJECT_CHARS = 10000
_INJECT_PREFIX = "[principle-recall]"


class Rule(NamedTuple):
    """레지스트리 항목 하나. `on is None` 이면 JUDGMENT(주입 대상 아님)."""

    bundle: str
    on: str | None
    match: str | None
    text: str
    layer: str


def _parse_text(text: str, *, layer: str) -> tuple[tuple[Rule, ...], int]:
    """마크다운 본문 → (규칙 튜플, 파손 항목 수). 파손은 조용히 skip 하지 않고 건수를 센다."""
    bundle = ""
    rules: list[Rule] = []
    broken = 0
    for line in text.splitlines():
        heading = _BUNDLE_RE.match(line)
        if heading:
            bundle = heading.group(1)
            continue
        item = _ITEM_RE.match(line)
        if not item:
            continue
        body = item.group(1).strip()
        if not body:
            continue
        if body.startswith("`["):
            tag = _TAG_RE.match(body)
            if not tag:
                broken += 1
                continue
            match_pattern = tag.group("match")
            try:
                re.compile(match_pattern)
            except re.error:
                broken += 1
                continue
            rules.append(
                Rule(
                    bundle=bundle,
                    on=tag.group("on"),
                    match=match_pattern,
                    text=tag.group("body"),
                    layer=layer,
                )
            )
        else:
            rules.append(
                Rule(bundle=bundle, on=None, match=None, text=body, layer=layer)
            )
    return (tuple(rules), broken)


def _dedup_key(rule: Rule):
    if rule.on is not None:
        return ("recall", rule.on, rule.match)
    return ("judgment", rule.text)


def _safe_session_id(session_id) -> str:
    text = str(session_id or "unknown").strip()
    safe = "".join((c for c in text if c.isalnum() or c in "-_"))[:64]
    return safe or "unknown"
