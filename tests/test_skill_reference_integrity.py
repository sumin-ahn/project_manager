"""T-0678 스킬 카드 상시/상황별 분리 참조 무결성."""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / ".claude" / "skills"
REFERENCE = "references/operational-details.md"
LINK = re.compile(
    r"\[references/operational-details\.md\]"
    r"\(references/operational-details\.md\)"
)


def _cards() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


def test_all_fifteen_skill_cards_link_their_relative_detail_document():
    cards = _cards()
    assert len(cards) == 15, "T-0678 대상 카드 수가 바뀌면 분리 sweep 범위를 재검토하라"
    for card in cards:
        text = card.read_text(encoding="utf-8")
        matches = LINK.findall(text)
        assert len(matches) == 1, f"{card}: 상황별 상대 참조가 정확히 1개여야 한다"
        target = card.parent / REFERENCE
        assert target.is_file(), f"{card}: 끊긴 참조 {REFERENCE}"
        assert target.resolve().is_relative_to(card.parent.resolve())


def test_detail_documents_are_nonempty_preserved_sections_not_stubs():
    for card in _cards():
        detail = card.parent / REFERENCE
        text = detail.read_text(encoding="utf-8")
        assert "상시 카드에서 분리한 원문" in text
        # 제목+안내만 놓은 공허 분리를 금지한다.
        assert len(re.findall(r"(?m)^#{2,4} ", text)) >= 1, detail
        assert len(text.encode("utf-8")) >= 500, detail


def test_windows_entry_notes_remain_on_all_always_loaded_cards():
    """T-0679 직렬 전에 Windows 블록을 참조 문서로 옮기지 않았음을 고정."""
    for card in _cards():
        text = card.read_text(encoding="utf-8")
        assert "**Windows 노트:**" in text or "**Windows 진입**:" in text, card
        detail = (card.parent / REFERENCE).read_text(encoding="utf-8")
        assert "**Windows 노트:**" not in detail
        assert "**Windows 진입**:" not in detail
