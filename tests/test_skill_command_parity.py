"""T-0674 — canonical skill ↔ opencode command 기계 사본 정합 가드.

command는 사람 슬래시 팔레트, skill은 모델 tool 표면이다. 기본 저작 소스는 root
``.claude/skills/<name>/SKILL.md``이며, native tool schema가 다른 명시 target override만
OpenCode flavor source를 쓴다. 이 가드는 실 파일 전수 집합과 기계 생성 정합을 강제한다.

**판정 층(T-0708)**: 기대값·실측값을 모두 `read_bytes()` 로 읽어 개행 표기만 LF로 정규화한
**내용 동일성**이다(바이트 표기 동일성이 아니다). 체크아웃 표기는 채택자 설정 소관이고, 내용이
한 글자라도 다르면 여전히 red다.
"""
from __future__ import annotations

from pathlib import Path

from _skill_command import DETAIL_LINK, expected_command_bytes, normalized_bytes
from _textio import write_crlf, write_lf

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


# 링크 문자열과 렌더·읽기 층의 단일 진실은 공용 seam `_skill_command` 다.
_DETAIL_LINK = DETAIL_LINK


def _expected_command(skill: Path, name: str) -> bytes:
    """기대 command 내용 (LF 정규화 bytes) — 실측값과 같은 층에서 만든다."""
    assert normalized_bytes(skill).count(_DETAIL_LINK.encode("utf-8")) == 1, (
        f"{skill}: operational detail 링크 수 drift")
    return expected_command_bytes(skill, name)


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
        if _expected_command(sources[name], name) != normalized_bytes(copies[name])
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


# ── 개행 표기 축(T-0708) ────────────────────────────────────────────────────────
# CRLF 체크아웃(core.autocrlf=true·Windows)에서 실 파일은 CRLF다. 아래 픽스처가 그 표기를 LF
# 환경에서도 재현해, 기대값만 텍스트-읽기로 만드는 층 혼합이 되살아나면 여기서 red가 된다.

_SKILL_BODY = f"---\nname: pm-x\n---\n본문 한 줄 {_DETAIL_LINK}\n"
_COMMAND_BODY = _SKILL_BODY.replace(
    _DETAIL_LINK, "(../../.claude/skills/pm-x/references/operational-details.md)"
)


def _skill_and_command_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """(canonical root, commands root, skill 파일, command 사본 파일) 픽스처 좌표."""
    canonical = tmp_path / "skills"
    commands = tmp_path / "command"
    skill = canonical / "pm-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    commands.mkdir()
    return canonical, commands, skill, commands / "pm-x.md"


def test_expected_command_is_newline_representation_independent(tmp_path):
    """기대 렌더는 source 표기(LF/CRLF)와 무관하게 같은 층(LF 정규화 bytes)에서 나온다."""
    lf_skill = tmp_path / "lf" / "SKILL.md"
    crlf_skill = tmp_path / "crlf" / "SKILL.md"
    lf_skill.parent.mkdir(parents=True)
    crlf_skill.parent.mkdir(parents=True)
    write_lf(lf_skill, _SKILL_BODY)
    write_crlf(crlf_skill, _SKILL_BODY)
    assert crlf_skill.read_bytes() != lf_skill.read_bytes(), "픽스처가 표기 차이를 못 만들었다"
    assert _expected_command(crlf_skill, "pm-x") == _expected_command(lf_skill, "pm-x")
    assert b"\r\n" not in _expected_command(crlf_skill, "pm-x")


def test_parity_guard_accepts_crlf_checkout_copies(tmp_path):
    """canonical·사본이 모두 CRLF인 체크아웃에서 내용이 같으면 drift 0이다."""
    canonical, commands, skill, copy = _skill_and_command_fixture(tmp_path)
    write_crlf(skill, _SKILL_BODY)
    write_crlf(copy, _COMMAND_BODY)
    assert b"\r\n" in copy.read_bytes(), "픽스처가 CRLF 사본이 아니다"
    assert _parity_errors(canonical, commands) == []


def test_parity_guard_accepts_mixed_newline_representations(tmp_path):
    """canonical과 사본의 개행 표기가 서로 달라도 내용이 같으면 drift가 아니다."""
    canonical, commands, skill, copy = _skill_and_command_fixture(tmp_path)
    write_crlf(skill, _SKILL_BODY)
    write_lf(copy, _COMMAND_BODY)
    assert _parity_errors(canonical, commands) == []


def test_parity_guard_reports_one_character_drift_under_crlf(tmp_path):
    """개행 정규화가 실 내용 drift를 가리지 않는다 — CRLF 사본의 1자 변경도 red다."""
    canonical, commands, skill, copy = _skill_and_command_fixture(tmp_path)
    write_crlf(skill, _SKILL_BODY)
    write_crlf(copy, _COMMAND_BODY.replace("한 줄", "두 줄"))
    assert _parity_errors(canonical, commands) == ["drift:pm-x"]


def test_parity_guard_reports_one_character_drift_under_lf(tmp_path):
    """같은 1자 변경은 LF 체크아웃에서도 red다 (판정이 표기에 의존하지 않는다)."""
    canonical, commands, skill, copy = _skill_and_command_fixture(tmp_path)
    write_lf(skill, _SKILL_BODY)
    write_lf(copy, _COMMAND_BODY.replace("한 줄", "두 줄"))
    assert _parity_errors(canonical, commands) == ["drift:pm-x"]
