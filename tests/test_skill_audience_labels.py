"""스킬 audience 라벨 존재/유효 가드 (ADR-0049 청중 라벨·T-0370 backfill·1a 정합 가드 계열).

전 canonical 스킬(`.claude/skills/*/SKILL.md`) frontmatter 에 `audience` binary 라벨
(`user-entrypoint` | `pm-internal`)이 있어야 한다 — 신규 스킬이 라벨 없이 출하되는 클래스를
loud 로 잡는다(T-0348 존재-정합 가드 계열·조용한 누락 금지). user-entrypoint 는 ADR-0049 가
명시한 사용자 진입점 3종(pm-env·pm-bootstrap·pm-handoff)을 반드시 포함한다(최소 3 유지 —
exact-set pin 은 아님: 진입점 추가는 ADR 개정으로 정당·여기선 후퇴만 잡는다).

templates 사본은 별도 스캔 불요 — `.claude/skills` 는 양 타깃 bare @render 미러(ADR-0065)라
byte-정합 가드(`test_opencode_command_skill_pairing`·manifest parity)가 라벨 동기까지 상속.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / ".claude" / "skills"

_VALID = {"user-entrypoint", "pm-internal"}
_ENTRYPOINT_MIN = {"pm-env", "pm-bootstrap", "pm-handoff"}  # ADR-0049 명시 진입점(후퇴 금지)


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "frontmatter 블록 부재"
    out: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def test_every_skill_has_valid_audience_label():
    """전 스킬 frontmatter 에 audience ∈ {user-entrypoint, pm-internal} — 누락/오값 loud."""
    dirs = sorted(d for d in SKILLS.iterdir() if (d / "SKILL.md").is_file())
    assert dirs, "sensitivity: 스캔한 스킬 0 — .claude/skills 경로 stale."
    offenders = []
    for d in dirs:
        fm = _frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
        aud = fm.get("audience")
        if aud not in _VALID:
            offenders.append(f"{d.name}: audience={aud!r}")
    assert not offenders, (
        "audience 라벨 누락/오값 스킬(ADR-0049 4요소 — 신규 스킬도 라벨 필수):\n  "
        + "\n  ".join(offenders))


def test_adr0049_entrypoints_are_user_entrypoint():
    """ADR-0049 명시 진입점 3종이 user-entrypoint 유지 — 라벨 후퇴(pm-internal 강등) 금지."""
    for name in sorted(_ENTRYPOINT_MIN):
        p = SKILLS / name / "SKILL.md"
        assert p.is_file(), f"진입점 스킬 {name} 부재 — ADR-0049 최소 3 위반."
        fm = _frontmatter(p.read_text(encoding="utf-8"))
        assert fm.get("audience") == "user-entrypoint", (
            f"{name}: audience={fm.get('audience')!r} — ADR-0049 명시 진입점은 user-entrypoint 여야 한다.")


def test_audience_guard_is_sensitive():
    """sensitivity — 파서가 라벨 부재/오값을 실제로 구분함을 in-memory 로 입증(non-vacuous)."""
    ok = _frontmatter("---\nname: x\naudience: pm-internal\n---\nbody")
    assert ok.get("audience") == "pm-internal"
    missing = _frontmatter("---\nname: x\n---\nbody")
    assert missing.get("audience") not in _VALID
    bad = _frontmatter("---\nname: x\naudience: everyone\n---\nbody")
    assert bad.get("audience") not in _VALID
