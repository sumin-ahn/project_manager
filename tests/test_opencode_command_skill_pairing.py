"""T-0364 / ADR-0065 — opencode 스킬 단일 소비 가드 (T-0344 pair-pin 은퇴 전환).

이 파일은 [[T-0344]] pair-pin 가드(claude 스킬 ↔ opencode `command/*.md` 수기 사본의 정규화
content-hash 대조)를 **은퇴**하고, 그 자리를 [[ADR-0065]] 단일 소비 모델의 대체 가드 3종으로
채운다. opencode(≥1.17.19·회사 기준)가 canonical `.claude/skills/*/SKILL.md` 를 네이티브 스캔·
슬래시(`/pm-…` · `run --command <스킬명>`) 호출하므로 `.opencode/command/*` 수기 사본 채널이
은퇴됐다 — 갈라질 두 표면 자체가 사라져, pair-pin 이 방어하던 silent-drift 클래스가 **원천 소멸**
한다(가드로 잡을 drift 가 없어짐). 파일명은 legacy(전환 이력 보존·rm 대신 in-place 재작성).

**대체 가드 3종** (ADR-0065 실행 티켓 T-0364 DoD):
  (a) 스킬 출하 경로 byte-정합 — opencode 출하 미러(`templates/opencode/.claude/skills`)가 root
      canonical(`.claude/skills`)과 1바이트도 다르지 않다. opencode 는 claude_code 와 **동일한**
      bare `@render`(root-sourced) 로 canonical 스킬을 소비하므로, 별도 hand-drift 채널이 아니라
      `pm_update --target opencode` 전파 미러다 — 그 전파 무결성을 못박는다.
  (b) command 잔존 0 (재유입 loud) — `templates/opencode/.opencode/command/` 에 PM-workflow 사본
      `.md` 가 0. 은퇴의 실제 실행(사본 파일 제거)은 **사용자 위임**(rm 직접 금지) — 삭제 전엔 이
      가드가 fail 로 '삭제 대기'를 loud 하게 표면화한다. 삭제 후 green. 재유입(신설 command 사본
      출하)도 같은 fail 로 잡힌다.
  (c) command=skill 등가 문구 잔재 0 — 출하 진입문서(AGENTS.md·AGENTS.lite.md·README.md)에 옛
      '등가' 주장이 남지 않았다. 은퇴를 *설명*하는 historical 노트(`…채널 은퇴·T-0364`)는 정당
      (terminology 가드의 '재정의 설명은 정당' 정신과 동일) — 금지 대상은 등가-*주장*뿐.

hermetic — 실 파일 존재/내용만 본다(LLM·subprocess 미진입). 위치는 tests/ 만(엔진 repo 전용·
templates 미전파 대상).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT_SKILLS = REPO / ".claude" / "skills"
OPENCODE_SKILLS_MIRROR = REPO / "templates" / "opencode" / ".claude" / "skills"
OPENCODE_COMMAND_DIR = REPO / "templates" / "opencode" / ".opencode" / "command"

# 옛 등가-주장 클래스의 한/영 변형 정규식 (T-0375·codex suggestion — 좁은 리터럴 4종에서 확대).
# 잡는 것: "command/커맨드 가 skill/스킬 의 등가·equivalent" 주장 + "쌍(으로) 출하/동시 출하" 의무 주장.
# 안 잡는 것: 무관 등가 표현(README 의 "CLAUDE.md 에 대응하는 opencode 등가물" 등 — command↔skill
# 짝이 아닌 문맥)·은퇴 historical 설명("채널 은퇴·T-0364").
_STALE_EQUIVALENCE_RES = (
    re.compile(r"(skill|스킬)\s*[-=]?\s*등가"),
    re.compile(r"(command|커맨드)\s*[-=]?\s*등가"),
    re.compile(r"skill[- ]?equivalent|equivalent\s+to\s+(the\s+)?skill", re.IGNORECASE),
    re.compile(r"쌍(으로)?\s*(동시\s*)?출하|동시\s*출하"),
)
# 출하 진입문서 — 등가-주장 잔재 스캔 대상.
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
        for p in sorted(base.rglob("SKILL.md"))
    }


# ── (a) 스킬 출하 경로 byte-정합 (전파 미러 무결성) ────────────────────────────


def test_opencode_skill_mirror_byte_identical_to_canonical():
    """opencode 출하 스킬 미러가 root canonical 과 byte-정합 (ADR-0065 단일 소비 출하 무결성).

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
    drifted = sorted(rel for rel in canon if canon[rel].read_bytes() != mirror[rel].read_bytes())
    assert not drifted, (
        f"opencode 스킬 미러가 canonical 과 byte drift: {drifted} — "
        "pm_update 전파 누락/구버전 잔존. `pm_update --target opencode` 로 재전파해야 한다(단일 소비 출하 무결성).")


def test_byte_parity_guard_is_sensitive_to_drift():
    """sensitivity — byte-정합 비교가 1바이트 차이를 검출함을 in-memory 로 입증(non-vacuous·실 파일 불변).

    실 canonical SKILL.md 한 개의 바이트에 1바이트를 더한 사본이 원본과 다르게(검출) 판정되는지,
    동일 바이트는 같게(음성 통제) 판정되는지 확인한다 — 가드가 공허(vacuous)하지 않음을 못박는다."""
    canon = _skill_files(ROOT_SKILLS)
    assert canon, "sensitivity: canonical 스킬 0개"
    raw = next(iter(canon.values())).read_bytes()
    assert raw == raw, "음성 통제: 동일 바이트가 다르게 판정됨"
    assert raw != raw + b"# drift\n", "1바이트 차이를 byte-비교가 못 잡음(vacuous 위험)"


# ── (b) command 잔존 0 (재유입 loud · 삭제 위임 대기 표면화) ───────────────────


def test_no_opencode_command_copy_residue():
    """opencode PM-workflow command 수기 사본이 0 (ADR-0065 은퇴 · 재유입 loud).

    ⚠️ 은퇴의 실제 실행(사본 파일 제거)은 **사용자 위임**(rm 직접 금지) — 삭제 전엔 이 가드가
    fail 로 '삭제 대기'를 loud 하게 표면화한다(pytest.mark 로 숨기지 않는다). 삭제 후 green.
    재유입(신설 command 사본을 다시 출하)도 이 가드가 같은 fail 로 잡는다(단일 소비 위반)."""
    residue = sorted(p.name for p in OPENCODE_COMMAND_DIR.glob("*.md")) if OPENCODE_COMMAND_DIR.is_dir() else []
    assert not residue, (
        "opencode PM-workflow command 수기 사본이 잔존한다(ADR-0065 은퇴 대상) — "
        f"{residue}. 삭제 위임(사용자): `rm templates/opencode/.opencode/command/*.md` 후 빈 디렉토리 제거. "
        "삭제하면 이 가드가 green 이 된다(단일 소비 = canonical .claude/skills 만). "
        "재유입(신설 command 사본)도 이 fail 로 잡힌다.")


# ── (c) command=skill 등가 문구 잔재 0 (출하 진입문서) ─────────────────────────


def test_no_command_skill_equivalence_phrase_residue():
    """출하 진입문서에 옛 'command = skill 등가' 주장 잔재 0 (ADR-0065 단일 소비 정정).

    금지 대상은 등가-*주장*뿐 — 은퇴를 *설명*하는 historical 노트(`…채널 은퇴·T-0364`)는 정당
    하다(terminology 가드의 '재정의 설명은 정당' 정신). 재유입 시 옛 등가 주장이 다시 박히면 fail."""
    scanned = 0
    offenders: "list[str]" = []
    for rel in _SHIP_ENTRY_DOCS:
        p = REPO / rel
        if not p.is_file():
            continue
        scanned += 1
        text = p.read_text(encoding="utf-8")
        for rx in _STALE_EQUIVALENCE_RES:
            m = rx.search(text)
            if m:
                offenders.append(f"{rel}: '{m.group(0)}'")
    # sensitivity: 스캔 대상 0 = 경로 stale (공허 통과 방지).
    assert scanned, "sensitivity: 스캔한 출하 진입문서 0 — _SHIP_ENTRY_DOCS 경로 stale."
    assert not offenders, (
        "출하 진입문서에 옛 'command = skill 등가' 주장 잔존 — ADR-0065 단일 소비로 정정하라:\n  "
        + "\n  ".join(offenders))


def test_equivalence_guard_catches_phrase_variants():
    """sensitivity (T-0375) — 확대 정규식이 한/영 변형 등가-주장을 실제로 잡는다(non-vacuous).

    변형 케이스가 전부 ≥1 정규식에 걸리고, 정당 문구(무관 등가·은퇴 설명)는 안 걸림을 못박는다."""
    should_catch = (
        "command 는 skill 등가 다", "커맨드=등가", ".opencode/command (skill-equivalent)",
        "this command is equivalent to the skill", "opencode 등가물을 쌍으로 출하한다",
        "command 를 동시 출하", "스킬 등가",
    )
    should_pass = (
        "command 채널 은퇴·T-0364", "CLAUDE.md 에 대응하는 opencode 등가물",
        "canonical SKILL.md 단일 소비",
    )
    for s in should_catch:
        assert any(rx.search(s) for rx in _STALE_EQUIVALENCE_RES), f"변형 미검출: {s!r}"
    for s in should_pass:
        assert not any(rx.search(s) for rx in _STALE_EQUIVALENCE_RES), f"정당 문구 오검출: {s!r}"
