"""T-0674 — canonical skill ↔ opencode command 기계 사본 정합 가드.

command는 사람 슬래시 팔레트, skill은 모델 tool 표면이다. 기본 저작 소스는 root
``.claude/skills/<name>/SKILL.md``이며, native tool schema가 다른 명시 target override만
OpenCode flavor source를 쓴다. 이 가드는 실 파일 전수 집합과 기계 생성 정합을 강제한다.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / ".claude" / "skills"
COMMANDS = REPO / "templates" / "opencode" / ".opencode" / "command"
OPENCODE_OVERRIDES = {
    "pm-dev-delegate": REPO / "templates" / "opencode" / ".claude" / "skills"
    / "pm-dev-delegate" / "SKILL.md",
}


def _skills(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {p.parent.name: p for p in sorted(root.glob("*/SKILL.md"))}


def _commands(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {p.stem: p for p in sorted(root.glob("*.md"))}


_DETAIL_LINK = "(references/operational-details.md)"


def _expected_command(skill: Path, name: str) -> bytes:
    text = skill.read_text(encoding="utf-8")
    assert text.count(_DETAIL_LINK) == 1, f"{skill}: operational detail 링크 수 drift"
    return text.replace(
        _DETAIL_LINK,
        f"(../../.claude/skills/{name}/references/operational-details.md)"
    ).encode("utf-8")


def _parity_errors(
        canonical: Path, commands: Path, overrides: dict[str, Path] | None = None,
) -> list[str]:
    skills = _skills(canonical)
    copies = _commands(commands)
    sources = dict(skills)
    sources.update(overrides or {})
    errors = [f"missing-command:{name}" for name in sorted(skills.keys() - copies.keys())]
    errors += [f"orphan-command:{name}" for name in sorted(copies.keys() - skills.keys())]
    errors += [
        f"drift:{name}" for name in sorted(skills.keys() & copies.keys())
        if _expected_command(sources[name], name) != copies[name].read_bytes()
    ]
    return errors


def test_all_canonical_skills_have_exact_rendered_command_copies():
    skills = _skills(CANONICAL)
    copies = _commands(COMMANDS)
    assert len(skills) == 15, f"출하 canonical 스킬 예상 15개, 실제 {len(skills)}개: {sorted(skills)}"
    assert len(copies) == 15, f"opencode command 사본 예상 15개, 실제 {len(copies)}개: {sorted(copies)}"
    assert set(OPENCODE_OVERRIDES) <= set(skills)
    assert _parity_errors(CANONICAL, COMMANDS, OPENCODE_OVERRIDES) == []


def test_parity_guard_reports_missing_copy(tmp_path):
    canonical = tmp_path / "skills"
    commands = tmp_path / "command"
    skill = canonical / "pm-new" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    commands.mkdir()
    skill.write_text(f"canonical {_DETAIL_LINK}\n", encoding="utf-8")
    assert _parity_errors(canonical, commands) == ["missing-command:pm-new"]


def test_parity_guard_reports_orphan_copy(tmp_path):
    canonical = tmp_path / "skills"
    commands = tmp_path / "command"
    canonical.mkdir()
    commands.mkdir()
    (commands / "pm-old.md").write_text("orphan\n", encoding="utf-8")
    assert _parity_errors(canonical, commands) == ["orphan-command:pm-old"]


def test_parity_guard_reports_content_drift(tmp_path):
    canonical = tmp_path / "skills"
    commands = tmp_path / "command"
    skill = canonical / "pm-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    commands.mkdir()
    skill.write_text(f"canonical {_DETAIL_LINK}\n", encoding="utf-8")
    (commands / "pm-x.md").write_text("drifted\n", encoding="utf-8")
    assert _parity_errors(canonical, commands) == ["drift:pm-x"]


def test_parity_guard_accepts_exact_generated_copy(tmp_path):
    canonical = tmp_path / "skills"
    commands = tmp_path / "command"
    skill = canonical / "pm-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    commands.mkdir()
    skill.write_text(f"same {_DETAIL_LINK}\n", encoding="utf-8")
    (commands / "pm-x.md").write_bytes(_expected_command(skill, "pm-x"))
    assert _parity_errors(canonical, commands) == []
