"""T-0674 — opencode skill tool·slash command 두 진입 표면 가드.

root `.claude/skills/*/SKILL.md`는 유일 저작 canonical이다. opencode는 그 스킬
미러를 모델 `skill` tool로 소비하고, 동일 본문에서 기계 생성한
`.opencode/command/<name>.md`를 사람 slash 팔레트로 소비한다. 이 legacy
파일은 스킬 미러 정합, command 채널 존재, 두 표면을 혼동하는 옛 문서
서술 부재를 보조 검증한다. 전수·byte·manifest 정합은 T-0674 신설 가드가 담당한다.
"""
from __future__ import annotations

import re
from pathlib import Path

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
ROOT_SKILLS = REPO / ".claude" / "skills"
OPENCODE_SKILLS_MIRROR = REPO / "templates" / "opencode" / ".claude" / "skills"
OPENCODE_COMMAND_DIR = REPO / "templates" / "opencode" / ".opencode" / "command"

# T-0364 단일-채널 서술의 재유입 가드.
_STALE_SINGLE_CHANNEL_RES = (
    re.compile(r"\.opencode/command[^\n]*(?:은퇴|미출하|사본\s*0)"),
    re.compile(r"(?:스킬|skill)[^\n]{0,24}단일\s*소비", re.IGNORECASE),
    re.compile(r"자체\s+slash\s+command를\s+뜻하지\s+않"),
)
# 출하 진입문서 — 옛 단일-채널 서술 스캔 대상.
_SHIP_ENTRY_DOCS = (
    "templates/opencode/AGENTS.md",
    "templates/opencode/AGENTS.lite.md",
    "templates/opencode/README.md",
)


# ── helper ───────────────────────────────────────────────────────────────────


def _skill_files(base: Path) -> "dict[str, Path]":
    """{base 상대 SKILL.md 경로: 절대경로} — base 부재 시 빈 dict."""
    if not base.is_dir():
        return {}
    return {
        p.relative_to(base).as_posix(): p
        for p in repo_owned_paths(REPO, base.relative_to(REPO), mode=OWNED)
        if p.name == "SKILL.md"
    }


# ── (a) 스킬 출하 경로 byte-정합 (전파 미러 무결성) ────────────────────────────


def _neutralize_entry_notation(raw: bytes) -> bytes:
    return re.sub(rb"(?<![A-Za-z0-9_.>/])[/\$](pm-[a-z][a-z0-9-]*)", rb"\1", raw)


def test_opencode_skill_mirror_matches_canonical_except_entry_notation():
    """opencode 출하 스킬 미러는 native 진입 표기 외에 canonical 과 동일하다.

    opencode 는 claude_code 와 동일한 bare @render(root `.claude/skills` 소스)로 canonical 스킬을
    소비하고, `pm_update --target opencode` 가 root 를 `templates/opencode/.claude/skills` 로 미러
    한다. 그 미러가 canonical 과 1바이트라도 다르면(전파 누락·구버전 잔존) 여기서 fail — 단일
    소비 출하의 무결성을 못박는다(옛 pair-pin 이 두 *다른* 표면을 hash 대조하던 자리를, 이제 *같은*
    canonical 의 전파 정합으로 대체)."""
    canon = _skill_files(ROOT_SKILLS)
    mirror = _skill_files(OPENCODE_SKILLS_MIRROR)
    # sensitivity: canonical 이 비면 경로 stale (공허 통과 방지).
    assert canon, "sensitivity: root .claude/skills 에 SKILL.md 0개 — 경로 상수 stale."
    assert mirror, (
        "opencode 출하 스킬 미러(templates/opencode/.claude/skills)가 비었다 — "
        "`pm_update --from <root> --target opencode` 로 canonical 을 전파해야 한다(ADR-0065 단일 소비 출하).")
    assert set(canon) == set(mirror), (
        "opencode 스킬 미러가 canonical 과 스킬 집합 불일치 — "
        f"canonical에만: {sorted(set(canon) - set(mirror))} / 미러에만: {sorted(set(mirror) - set(canon))}. "
        "`pm_update --target opencode` 로 재전파.")
    drifted = sorted(
        rel
        for rel in canon
        if _neutralize_entry_notation(canon[rel].read_bytes())
        != _neutralize_entry_notation(mirror[rel].read_bytes())
    )
    assert not drifted, (
        f"opencode 스킬 미러가 진입 표기 외 canonical 내용과 drift: {drifted} — "
        "전파 누락/구버전 잔존 또는 허용 범위 밖 수기 변경.")


def test_normalized_parity_guard_is_sensitive_to_non_notation_drift():
    """sensitivity — 진입 표기 외 1바이트 차이는 정규화 뒤에도 검출한다.

    실 canonical SKILL.md 한 개의 바이트에 1바이트를 더한 사본이 원본과 다르게(검출) 판정되는지,
    동일 바이트는 같게(음성 통제) 판정되는지 확인한다 — 가드가 공허(vacuous)하지 않음을 못박는다."""
    canon = _skill_files(ROOT_SKILLS)
    assert canon, "sensitivity: canonical 스킬 0개"
    raw = next(iter(canon.values())).read_bytes()
    normalized = _neutralize_entry_notation(raw)
    assert normalized == _neutralize_entry_notation(raw), "음성 통제 실패"
    assert normalized != _neutralize_entry_notation(raw + b"# drift\n")


# ── (b) command 채널 복원 ────────────────────────────────────────────────────


def test_opencode_command_copy_channel_restored():
    """T-0674: command 팔레트 사본 채널이 실제 출하된다.

    전수 집합·내용·manifest 기계 생성 계약은 신설
    ``test_skill_command_parity.py``가 누락·drift sensitivity와 함께 단일 가드로 강제한다."""
    copies = sorted(p.name for p in OPENCODE_COMMAND_DIR.glob("*.md"))
    assert "pm-bootstrap.md" in copies
    assert len(copies) == 15


# ── (c) 옛 단일-채널 서술 잔재 0 (출하 진입문서) ──────────────────────────────


def test_no_retired_single_channel_phrase_residue():
    """출하 진입문서에 command 은퇴·스킬 단일 소비 서술이 남지 않는다."""
    scanned = 0
    offenders: "list[str]" = []
    for rel in _SHIP_ENTRY_DOCS:
        p = REPO / rel
        if not p.is_file():
            continue
        scanned += 1
        text = p.read_text(encoding="utf-8")
        for rx in _STALE_SINGLE_CHANNEL_RES:
            m = rx.search(text)
            if m:
                offenders.append(f"{rel}: '{m.group(0)}'")
    # sensitivity: 스캔 대상 0 = 경로 stale (공허 통과 방지).
    assert scanned, "sensitivity: 스캔한 출하 진입문서 0 — _SHIP_ENTRY_DOCS 경로 stale."
    assert not offenders, (
        "출하 진입문서에 옛 command 은퇴/단일 소비 서술 잔존:\n  "
        + "\n  ".join(offenders))


def test_single_channel_guard_catches_phrase_variants():
    """sensitivity — 옛 단일-채널 주장을 실제로 잡는다."""
    should_catch = (
        ".opencode/command 채널 은퇴", ".opencode/command 미출하",
        "스킬 단일 소비", "자체 slash command를 뜻하지 않는다",
    )
    should_pass = (
        "skill tool과 command 채널을 둘 다 출하한다",
        "command 파일은 canonical SKILL.md에서 기계 생성한다",
    )
    for s in should_catch:
        assert any(rx.search(s) for rx in _STALE_SINGLE_CHANNEL_RES), f"변형 미검출: {s!r}"
    for s in should_pass:
        assert not any(rx.search(s) for rx in _STALE_SINGLE_CHANNEL_RES), f"정당 문구 오검출: {s!r}"
