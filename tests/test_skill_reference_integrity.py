"""T-0678 스킬 카드 상시/상황별 분리 참조 무결성."""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / ".claude" / "skills"
OPENCODE = REPO / "templates" / "opencode"
OPENCODE_COMMANDS = OPENCODE / ".opencode" / "command"
OPENCODE_MODEL_SKILLS = OPENCODE / ".claude" / "skills"
CODEX_SKILLS = REPO / "templates" / "codex" / ".agents" / "skills"
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


def test_environment_guides_replace_windows_blocks_without_moving_operational_details():
    """T-0679 환경 공통화와 T-0678 상황별 상세 분리를 서로 섞지 않는다."""
    for card in _cards():
        text = card.read_text(encoding="utf-8")
        assert "**Windows 노트:**" not in text
        assert "**Windows 진입**:" not in text
        assert text.count("../references/environment-windows.md") == 1
        assert text.count("../references/environment-posix.md") == 1
        detail = (card.parent / REFERENCE).read_text(encoding="utf-8")
        assert "**Windows 노트:**" not in detail
        assert "**Windows 진입**:" not in detail


def _opencode_command_reference_errors(command_root: Path) -> list[str]:
    """실배송 command-relative 링크만 해소한다(canonical fallback 금지)."""
    errors: list[str] = []
    ship_root = command_root.parent.parent
    root_resolved = ship_root.resolve()
    for card in sorted(command_root.glob("*.md")):
        text = card.read_text(encoding="utf-8")
        matches = re.findall(
            r"\[references/operational-details\.md\]\(([^)]+)\)", text
        )
        if len(matches) != 1:
            errors.append(f"link-count:{card.stem}:{len(matches)}")
            continue
        expected = (
            f"../../.claude/skills/{card.stem}/references/operational-details.md"
        )
        if matches[0] != expected:
            errors.append(f"link-target:{card.stem}:{matches[0]}")
            continue
        target = card.parent / matches[0]
        try:
            resolved = target.resolve(strict=True)
        except FileNotFoundError:
            errors.append(f"missing:{card.stem}")
            continue
        if not resolved.is_relative_to(root_resolved):
            errors.append(f"escape:{card.stem}")
        elif target.is_symlink() or not target.is_file():
            errors.append(f"not-regular:{card.stem}")
    return errors


def test_opencode_flat_commands_resolve_all_fifteen_shipped_details():
    cards = sorted(OPENCODE_COMMANDS.glob("*.md"))
    assert len(cards) == 15
    assert _opencode_command_reference_errors(OPENCODE_COMMANDS) == []
    for card in cards:
        detail = card.parent / (
            f"../../.claude/skills/{card.stem}/references/operational-details.md"
        )
        model_detail = (
            OPENCODE_MODEL_SKILLS / card.stem / "references"
            / "operational-details.md"
        )
        assert detail.read_bytes() == model_detail.read_bytes(), card.stem


def test_opencode_command_reference_guard_detects_missing_detail(tmp_path):
    commands = tmp_path / "ship" / ".opencode" / "command"
    commands.mkdir(parents=True)
    (commands / "pm-x.md").write_text(
        "[references/operational-details.md]"
        "(../../.claude/skills/pm-x/references/operational-details.md)\n",
        encoding="utf-8",
    )
    assert _opencode_command_reference_errors(commands) == ["missing:pm-x"]
    detail = (
        tmp_path / "ship" / ".claude" / "skills" / "pm-x"
        / "references" / "operational-details.md"
    )
    detail.parent.mkdir(parents=True)
    detail.write_text("detail\n", encoding="utf-8")
    assert _opencode_command_reference_errors(commands) == []


def test_codex_override_cards_own_linked_environment_neutral_details():
    expected = {
        CODEX_SKILLS / "pm-dev-delegate" / "SKILL.md",
        CODEX_SKILLS / "pm-review" / "SKILL.md",
    }
    linked: set[Path] = set()
    for card in expected:
        text = card.read_text(encoding="utf-8")
        matches = LINK.findall(text)
        assert len(matches) == 1, card
        target = card.parent / REFERENCE
        assert target.is_file(), card
        assert not target.is_symlink(), card
        assert target.resolve().is_relative_to(card.parent.resolve()), card
        linked.add(target)
        combined = text + "\n" + target.read_text(encoding="utf-8")
        assert "**Windows 노트:**" not in combined, card
        assert "**Windows 진입**:" not in combined, card
        assert text.count("../references/environment-windows.md") == 1, card
        assert text.count("../references/environment-posix.md") == 1, card
        assert len(target.read_bytes()) >= 500, target
    assert linked == {
        path for path in CODEX_SKILLS.glob(
            "*/references/operational-details.md"
        ) if path.parent.parent.name in {"pm-dev-delegate", "pm-review"}
    }
    review = (
        CODEX_SKILLS / "pm-review" / "SKILL.md"
    ).read_text(encoding="utf-8") + (
        CODEX_SKILLS / "pm-review" / REFERENCE
    ).read_text(encoding="utf-8")
    assert "Claude PM" not in review
    assert "Bash 툴" not in review
