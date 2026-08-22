"""T-0679 common environment guides and 15-card migration guards."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / ".claude" / "skills"
GUIDES = SKILLS / "references"
WINDOWS_GUIDE = GUIDES / "environment-windows.md"
POSIX_GUIDE = GUIDES / "environment-posix.md"
ENVIRONMENT_LINE = re.compile(
    r'^환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 '
    r'\[Windows 안내\]\(\.\./references/environment-windows\.md\) 또는 '
    r'\[Linux/macOS 안내\]\(\.\./references/environment-posix\.md\)를 참조한다\.$',
    re.MULTILINE,
)
OLD_MARKERS = ("**Windows 노트:**", "**Windows 진입**:")


def _cards() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


# The 15 canonical preimages normalize to exactly these four semantic families.
# Each tuple lists the load-bearing tokens that must survive relocation; this is
# intentionally separate from T-0681's derived-template surface axis.
_PREIMAGE_VARIANTS = {
    "python-shim-and-ps5": (
        "python3 …", "py -3.12", "WindowsApps", "Permission denied",
        "PowerShell 5.x", "&&", "ParseError", "workdir", "명령 분리",
    ),
    "pm-env-pm-config-cmd": (
        "./pm-config.sh", ".\\pm-config.cmd", "pm_import", "동일 인자",
        "PowerShell 5.x", "&&", "ParseError", "workdir", "명령 분리",
    ),
    "pm-update-two-cmd-facades": (
        "./pm-update.sh", ".\\pm-update.cmd", "./pm-config.sh",
        ".\\pm-config.cmd", "python3 …", "py -3.12", "WindowsApps",
        "Permission denied", "PowerShell 5.x", "&&", "ParseError",
        "workdir", "명령 분리",
    ),
    "pm-release-composed": (
        "python3 …", "py -3.12", "WindowsApps", "Permission denied",
        "./pm-update.sh", ".\\pm-update.cmd", "./pm-config.sh",
        ".\\pm-config.cmd", "동일 인자", "PowerShell 5.x", "&&",
        "ParseError", "workdir", "명령 분리",
    ),
}


def test_migration_surface_is_exactly_fifteen_cards_and_four_preimage_variants():
    assert len(_cards()) == 15
    assert len(_PREIMAGE_VARIANTS) == 4


@pytest.mark.parametrize("card", _cards(), ids=lambda p: p.parent.name)
def test_each_card_has_one_common_environment_reference_and_no_old_marker(card: Path):
    text = card.read_text(encoding="utf-8")
    assert len(ENVIRONMENT_LINE.findall(text)) == 1, card
    assert all(marker not in text for marker in OLD_MARKERS), card


def test_all_four_preimage_semantics_are_preserved_in_windows_guide():
    text = WINDOWS_GUIDE.read_text(encoding="utf-8")
    missing = {
        variant: [token for token in tokens if token not in text]
        for variant, tokens in _PREIMAGE_VARIANTS.items()
    }
    missing = {variant: tokens for variant, tokens in missing.items() if tokens}
    assert not missing


def test_encoding_contract_is_preserved_without_making_env_prefix_default():
    text = WINDOWS_GUIDE.read_text(encoding="utf-8")
    for token in (
        'encoding="utf-8"', "stdout", "$env:PYTHONUTF8='1';",
        "PYTHONUTF8=1", "opt-in", "bash 문법을 Windows 전 환경에 강제하지 않는다",
    ):
        assert token in text


@pytest.mark.parametrize("card", _cards(), ids=lambda p: p.parent.name)
def test_environment_links_are_contained_nofollow_regular_files(card: Path):
    text = card.read_text(encoding="utf-8")
    targets = re.findall(r"\((\.\./references/environment-(?:windows|posix)\.md)\)", text)
    assert targets == [
        "../references/environment-windows.md",
        "../references/environment-posix.md",
    ]
    for rel in targets:
        lexical = Path(os.path.normpath(card.parent / rel))
        assert lexical.is_relative_to(SKILLS)
        assert not lexical.is_symlink()
        assert stat.S_ISREG(lexical.stat(follow_symlinks=False).st_mode)
        resolved = lexical.resolve(strict=True)
        assert resolved.is_relative_to(SKILLS.resolve())


def test_posix_guide_covers_linux_macos_launcher_chain_and_facades():
    text = POSIX_GUIDE.read_text(encoding="utf-8")
    for token in (
        "현재 환경: linux", "현재 환경: macos", "python3", "local.conf",
        "and-if(`&&`)", "./pm-config.sh", "./pm-update.sh", "encoding=\"utf-8\"",
    ):
        assert token in text


@pytest.mark.parametrize("guide", [WINDOWS_GUIDE, POSIX_GUIDE], ids=lambda p: p.stem)
def test_guide_matches_final_py_assignment_semantics(guide: Path):
    """A final empty assignment overrides, rather than skips, an earlier value."""
    text = guide.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "마지막 `runtime.py=` assignment의 값이 non-empty일 때만" in normalized
    assert "마지막 assignment가 비었으면 앞선 non-empty 값도 무효화" in normalized
    assert "마지막 non-empty `runtime.py=`" not in text


def test_t0681_environment_guides_ship_to_all_model_and_opencode_command_channels():
    """T-0681 ships both guides to three model roots and the OpenCode command root."""
    text = WINDOWS_GUIDE.read_text(encoding="utf-8")
    for token in (
        ".claude/skills/references/",
        ".agents/skills/references/",
        ".opencode/references/",
        ".opencode/command/<skill>.md",
        "../references/environment-*.md",
        ".opencode/references/environment-*.md",
    ):
        assert token in text
    derived = sorted([
        *REPO.glob("templates/*/.claude/skills/references/environment-*.md"),
        *REPO.glob("templates/*/.agents/skills/references/environment-*.md"),
        *REPO.glob("templates/*/.opencode/references/environment-*.md"),
    ])
    assert len(derived) == 8, f"환경 guide 2벌 × (모델 3 + OpenCode command 1) 필요: {derived}"
    for copy in derived:
        canonical = GUIDES / copy.name
        expected = canonical.read_text(encoding="utf-8")
        if "/.agents/skills/" in copy.as_posix():
            expected = expected.replace("`/pm-update`", "`$pm-update`")
        assert copy.read_text(encoding="utf-8") == expected, f"환경 guide 파생 사본 drift: {copy}"
