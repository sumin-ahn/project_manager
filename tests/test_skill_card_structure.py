"""Shipped PM skill cards keep their fenced delegation examples intact.

This is deliberately narrower than a general Markdown linter.  Existing tests
own adapter parity, harness-specific fields, and private-context markers.  This
guard owns only the failure mode where a fenced call example is left open or a
Markdown list item is pasted into the call body.

The Codex delegation card is a hand-authored variant with a Python-shaped
``spawn_agent(...)`` call. The shared card uses an ``Agent 툴 호출:``
pseudo-configuration block, which OpenCode reads through the harness note
(T-0895). Only the Python-shaped variant is parsed as Python; every variant
rejects a top-level Markdown list item inside the delegation block.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from _harness_matrix import HARNESS_ADAPTER_DIRS


REPO = Path(__file__).resolve().parents[1]
_ADAPTER_DIRS = tuple(dict.fromkeys(
    adapter
    for adapter_dirs in HARNESS_ADAPTER_DIRS.values()
    for adapter in adapter_dirs
))
_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<tail>.*)$")
_MARKDOWN_LIST_ITEM = re.compile(r"^[-+*]\s")
_DELEGATION_MARKERS = ("spawn_agent(", "Agent 툴 호출:")


@dataclass(frozen=True)
class _FencedBlock:
    start_line: int
    end_line: int
    body: str


def _card_roots(repo: Path) -> list[Path]:
    roots = [repo / adapter for adapter in _ADAPTER_DIRS]
    templates = repo / "templates"
    if templates.is_dir():
        for template in sorted(path for path in templates.iterdir() if path.is_dir()):
            roots.extend(template / adapter for adapter in _ADAPTER_DIRS)
    return roots


def _skill_cards(repo: Path = REPO) -> list[Path]:
    cards: set[Path] = set()
    for root in _card_roots(repo):
        if not root.is_dir():
            continue
        cards.update(root.glob("skills/pm-*/SKILL.md"))
    return sorted(cards)


def _covered_card_roots(repo: Path, cards: list[Path]) -> set[str]:
    return {
        root.relative_to(repo).as_posix()
        for root in _card_roots(repo)
        if any(root in card.parents for card in cards)
    }


# 현재 출하 표면에서 실제 카드를 가진 root를 한 번 측정해 inventory 축으로 고정한다. adapter 후보는
# HARNESS_ADAPTER_DIRS에서 파생하므로 `.opencode` 및 향후 하네스 namespace가 자동으로 검색에 들어간다.
_REQUIRED_CARD_ROOTS = frozenset(_covered_card_roots(REPO, _skill_cards(REPO)))


def _closing_fence(line: str, marker: str) -> bool:
    match = _FENCE.fullmatch(line)
    if match is None:
        return False
    candidate = match.group("marker")
    return (
        candidate[0] == marker[0]
        and len(candidate) >= len(marker)
        and not match.group("tail").strip()
    )


def _fenced_blocks(text: str) -> tuple[list[_FencedBlock], list[str]]:
    blocks: list[_FencedBlock] = []
    open_marker: str | None = None
    open_line = 0
    body: list[str] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        if open_marker is None:
            match = _FENCE.fullmatch(line)
            if match is not None:
                open_marker = match.group("marker")
                open_line = line_number
                body = []
            continue

        if _closing_fence(line, open_marker):
            blocks.append(
                _FencedBlock(
                    start_line=open_line,
                    end_line=line_number,
                    body="\n".join(body) + "\n",
                )
            )
            open_marker = None
            body = []
        else:
            body.append(line)

    issues = []
    if open_marker is not None:
        issues.append(
            f"line {open_line}: unclosed {open_marker[0]} fence "
            f"(opened with {len(open_marker)} markers)"
        )
    return blocks, issues


def _card_structure_issues(
    text: str, *, require_delegation: bool = True,
) -> list[str]:
    blocks, issues = _fenced_blocks(text)
    delegation_blocks = [
        block
        for block in blocks
        if any(marker in block.body for marker in _DELEGATION_MARKERS)
    ]
    if require_delegation and not delegation_blocks:
        issues.append(
            "delegation block count is 0 "
            f"(expected one of markers: {_DELEGATION_MARKERS!r})"
        )

    for block in delegation_blocks:
        for offset, line in enumerate(block.body.splitlines(), start=1):
            if _MARKDOWN_LIST_ITEM.match(line):
                issues.append(
                    f"line {block.start_line + offset}: top-level Markdown list item "
                    "inside delegation call example"
                )

        if "spawn_agent(" in block.body:
            try:
                ast.parse(block.body)
            except SyntaxError as exc:
                line_number = block.start_line + (exc.lineno or 1)
                issues.append(
                    f"line {line_number}: invalid spawn_agent call example: {exc.msg}"
                )
    return issues


def _delegate_fixture(tmp_path: Path, *, guidance_inside: bool) -> Path:
    path = tmp_path / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"
    path.parent.mkdir(parents=True)
    inside = "- **raw 안내:** 결과 파일을 확인한다.\n" if guidance_inside else ""
    outside = "" if guidance_inside else "- **raw 안내:** 결과 파일을 확인한다.\n"
    path.write_text(
        "# delegate\n\n"
        "```\n"
        "spawn_agent(\n"
        '  agent_type="developer",\n'
        '  message="""작업을 구현하라.""",\n'
        f"{inside}"
        ")\n"
        "```\n\n"
        f"{outside}",
        encoding="utf-8",
    )
    return path


def test_skill_card_inventory_covers_every_shipping_surface():
    cards = _skill_cards()
    assert cards, "shipped pm-* skill card inventory is empty"
    assert len(cards) >= 56, f"shipped card coverage shrank below 4×14: {len(cards)}"
    assert len(cards) == len(set(cards)), "duplicate paths in shipped skill card inventory"

    covered_roots = _covered_card_roots(REPO, cards)
    missing = _REQUIRED_CARD_ROOTS - covered_roots
    assert not missing, f"shipping skill card surfaces have no discovered cards: {sorted(missing)}"


def test_card_root_inventory_guard_is_sensitive_to_each_root_omission():
    """D sensitivity: 파생된 현재 출하 root 어느 하나를 빼도 missing 집합이 정확히 red다."""
    cards = _skill_cards()
    covered_roots = _covered_card_roots(REPO, cards)
    assert _REQUIRED_CARD_ROOTS
    for omitted in _REQUIRED_CARD_ROOTS:
        reduced = covered_roots - {omitted}
        assert _REQUIRED_CARD_ROOTS - reduced == {omitted}


def test_all_shipped_skill_card_examples_are_structurally_intact():
    cards = _skill_cards()
    assert cards, "structure guard examined zero shipped pm-* skill cards"
    delegation_cards = [
        path for path in cards if path.parent.name == "pm-dev-delegate"
    ]
    assert len(delegation_cards) == len(_REQUIRED_CARD_ROOTS), (
        "각 출하 root의 pm-dev-delegate 카드 하나씩을 검사해야 함: "
        f"cards={len(delegation_cards)} roots={len(_REQUIRED_CARD_ROOTS)}"
    )
    failures = {
        path.relative_to(REPO).as_posix(): issues
        for path in cards
        if (
            issues := _card_structure_issues(
                path.read_text(encoding="utf-8"),
                require_delegation=path.parent.name == "pm-dev-delegate",
            )
        )
    }
    assert not failures, f"broken fenced examples in shipped skill cards: {failures}"


def test_structure_guard_is_sensitive_when_spawn_agent_marker_is_removed():
    """C sensitivity: marker 삭제로 Python call 문법까지 깨진 실제 codex 변이는 0-block red다."""
    card = REPO / "templates" / "codex" / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"
    text = card.read_text(encoding="utf-8")
    assert "spawn_agent(" in text
    issues = _card_structure_issues(text.replace("spawn_agent(", ""))
    assert any("delegation block count is 0" in issue for issue in issues)


def test_structure_guard_is_sensitive_when_agent_tool_marker_is_removed():
    """claude·opencode 가 공유하는 카드의 marker 손상도 0-block으로 검출한다."""
    card = REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
    text = card.read_text(encoding="utf-8")
    assert "Agent 툴 호출:" in text
    assert _card_structure_issues(text) == []
    issues = _card_structure_issues(text.replace("Agent 툴 호출:", ""))
    assert any("delegation block count is 0" in issue for issue in issues)


def test_guidance_after_delegation_fence_is_valid(tmp_path):
    card = _delegate_fixture(tmp_path, guidance_inside=False)
    assert _card_structure_issues(card.read_text(encoding="utf-8")) == []


def test_guidance_between_triple_quote_and_closing_call_is_rejected(tmp_path):
    card = _delegate_fixture(tmp_path, guidance_inside=True)
    issues = _card_structure_issues(card.read_text(encoding="utf-8"))
    assert len(issues) == 2
    assert any("top-level Markdown list item" in issue for issue in issues)
    assert any("invalid spawn_agent call example" in issue for issue in issues)


def test_unclosed_fence_is_rejected():
    issues = _card_structure_issues("```bash\npython3 tool.py\n")
    assert any("unclosed" in issue for issue in issues)
