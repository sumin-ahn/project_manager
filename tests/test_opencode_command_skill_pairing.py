"""T-0344 — opencode command 수기 사본 silent-drift 가드 (claude 스킬 pair-pin).

`templates/opencode/.opencode/command/*.md` 는 claude 스킬(`.claude/skills/*/SKILL.md`)의
**수기 적응 사본**이다 — root 소스가 없고(target-owned) 자동 전파 채널이 없다. canonical 스킬을
고쳐도 opencode 사본이 조용히 낡는 클래스(PM 71 pm-env 캐비앗 stale 실측)를 기계 가드로 loud 하게
만든다. (full 자동 생성기는 변환에 수기 적응[frontmatter·command wrapper·wikilink→슬래시]이 섞여
있어 v1.3.0 스코프 — 이 가드는 그때까지 silent 를 없애는 forcing function.)

**pair-pin 방식** (livegate pin·byte-parity 가드 동형):
  - 스킬↔command 대응쌍(스킬 dir 이름 == command 파일 stem)마다 **양쪽 파일의 정규화 content
    hash** 를 아래 `PAIR_PINS` 표에 박제한다.
  - pin = *정규화 후* hash — 개행(CRLF/CR→LF)·줄 트레일링 공백·끝 공백줄만 정규화한다. 두 표면은
    의도적으로 다른 형식이라 내용-diff 는 오탐 원천 → **각 측을 독립 pin**(cross-diff 안 함).
  - 어느 한쪽 파일이 바뀌면 그 측 hash 가 pin 과 어긋나 fail + 아래 지시 메시지.
  - 짝 없는 스킬(신규 스킬 추가·command 사본 누락)·짝 없는 command 도 fail (누락 클래스 커버).

**pin 갱신 절차** (fail 메시지에서 지시):
  1. 어느 표면이 바뀌었는지 확인한다 — canonical 스킬(`.claude/skills/<name>/SKILL.md`)인지
     opencode 사본(`templates/opencode/.opencode/command/<name>.md`)인지 (fail 은 측을 식별한다).
  2. 반대쪽 사본을 **손으로 검토·정합**한다 — 자동 전파 채널이 없다(수기 적응 사본).
  3. 정합 후 이 파일 `PAIR_PINS` 의 양쪽 hash 를 **둘 다** 새 값으로 갱신한다. 현 트리 전체를
     그대로 붙여넣을 수 있는 `PAIR_PINS` 블록은 이 파일을 직접 실행해 얻는다(파일 미수정·출력만):

         python3 tests/test_opencode_command_skill_pairing.py

  pin 만 올리고 사본을 정합 안 하는 것은 막을 수 없다(정직 전제) — 그러나 어느 한쪽 표면 변경을
  **모르고 지나가는 것**은 구조적으로 불가능해진다(이 가드의 목적).

hermetic — 실 파일 존재/내용만 본다(LLM·subprocess 미진입). 위치는 tests/ 만(엔진 repo 전용·
templates 미전파 대상·T-0344 DoD).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO / ".claude" / "skills"
COMMANDS_DIR = REPO / "templates" / "opencode" / ".opencode" / "command"

# ── 정당 비대칭 allowlist ────────────────────────────────────────────────────
# pm-regression 스킬은 opencode 에 대응 command 가 **없다** — harness-specific 정당 비대칭이다.
# 회귀 게이트는 claude 하니스의 pre-push/훅 인프라(`.claude/` run_tests_hook)에 묶여 있고 opencode
# 는 동등 회귀 트리거를 자기 하니스 방식으로 갖는다(별도 표면). 기존 가드
# (`tests/test_manifest_template_parity.py` 의 `CLAUDE_ONLY_PATHS`)가 `.claude/skills` 를 opencode
# manifest 에서 제외되는 claude-scoped 경로로 명시한다 — 이 비대칭이 harness-specific 정당임을 뒷받침. 여기서
# allowlist 로 못박아, 이 스킬은 command 사본 부재를 fail 로 보지 않는다. 그 밖의 스킬이 command
# 없이 새로 생기면(allowlist 밖) fail — "짝 없음" 누락 클래스를 커버한다.
SKILL_ONLY_ALLOWLIST = frozenset({"pm-regression"})

# ── pair-pin 표 (현 트리 = green baseline·PM 71 pm-env 정합 완료 직후) ────────────
# name -> (SKILL.md 정규화 hash, command .md 정규화 hash). 어느 한쪽이 바뀌면 fail.
# 갱신 절차는 모듈 docstring 참고 (양쪽 사본 정합 후 둘 다 갱신 — `python3 이_파일.py` 로 현
# 트리 전체 블록을 붙여넣기용으로 출력).
PAIR_PINS: dict[str, tuple[str, str]] = {
    "pm-bootstrap": ("fd642f09629f04262de493d3ad39890fcd4941364fd9a41ddde9d2c6bc9bdee2",
                     "98d689127c14cdbef28f182f52f9e7fba2f0acf35f377be321098672537e16ea"),
    "pm-dev-delegate": ("b6c309912e152c3931c1972cf6dba554ee7e725e94cd997232324c587a5ac049",
                        "713b033b1dac9d571eb4c0d9e52460ebf736d14fd870af5d4f9c5484ec2c97ff"),
    "pm-env": ("48e457b6c8f921a2fc4bacc49222df9c11717d97dc4727452e6cea74e506f674",
               "4c574f655ec737b86f99000d1864ffa693581a0efa9c092311c32ea943ea0c24"),
    "pm-handoff": ("5817a3955091a7b5fb831fce58a28bef5bd729e8de1c4bcb665c3c15d7d1f7e5",
                   "fda4427bb25b644ef860ee06d5445eb37f5219a0179d7a2175e82b7af9f116d0"),
    "pm-qa": ("334baf905073a840484dcc7f176d44cd07b7c4ccbc8b9ac48d73d18db90687e4",
              "3918b0e4c18ef9db174af9a3226b7fd494949b72923e5c5d7de83fc313d7926d"),
    "pm-release": ("1a86e877fbe9734f8fcfe6ed4e22559282b9059bea42c7a268b2206059503eee",
                   "08a005d13df5f95f3ca1a1aa69293fcd7c2f57d93e6a058f1896fd24198cb497"),
    "pm-update": ("01aa84f3d14350a4e03e0fd650bf8b9bb7777eefda729ad449f1f77f40859a22",
                  "0624916d0912d8711b3505a042c18dce3d87d5fcb2356b6103ad5d324850af47"),
    "pm-wave-claim": ("0262928751a2f2a1d4efd590ea17e1a0e28ea2c4120926755d972528e2cd63e1",
                      "979cf0354391d683d3225defa6c365725363916e83acf51c354dcd56d06ce2d8"),
    "pm-wave-finish": ("139d771c5bf8c52d069f0d346a09e85493f1d005a1c2d75210864f509044bf34",
                       "6d6a548cd6fe88d6edcbde6c45d8c3ff42885d9ff813d937e179685d99a0f6ab"),
    "pm-worktree": ("c287b3cbbf4268d982cd9749db35fcfeb778acb372873164a5d604cdf2267729",
                    "2ae1bcc33502605327973a9c4d434bd277c8c5fa5499061fd0b8ee1cbf6c3ff4"),
    "spike-new": ("06bf270347ff2a67f02816dbce70a059fb86f01e76d0e97ad5ce588027276aa3",
                  "c6f5d5d8ce1a8100c992735358f8305104cc692ac50f0a1fe78faf10635b9297"),
}

_UPDATE_HINT = (
    "스킬/사본 한쪽 표면이 변경됨(어느 측인지는 위 per-side 단언이 식별) — 반대쪽 사본을 검토·"
    "정합한 뒤 양쪽 pin 을 갱신하라(자동 전파 채널 없음·수기 적응 사본·T-0344). "
    "붙여넣기용 PAIR_PINS 블록: `python3 tests/test_opencode_command_skill_pairing.py`."
)


# ── helper ───────────────────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    """개행·트레일링 공백만 정규화 (내용은 안 건드림 — 두 표면은 의도적으로 다른 형식).

    CRLF/CR→LF · 각 줄 끝 공백 제거 · 끝 공백줄 제거. 공백-only churn(줄바꿈 형식·트레일링
    스페이스)에는 pin 이 흔들리지 않고, 실제 내용 변경에는 hash 가 어긋난다."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [ln.rstrip() for ln in lines]
    return "\n".join(lines).rstrip("\n") + "\n"


def _content_hash(path: Path) -> str:
    """정규화 후 sha256 hex (pin 값 계산·갱신 시 이 함수 사용)."""
    return hashlib.sha256(
        _normalize(path.read_text(encoding="utf-8")).encode("utf-8")
    ).hexdigest()


def _discover() -> tuple[set[str], set[str]]:
    """(스킬명 집합, command stem 집합) 자동 발견.

    스킬명 = `.claude/skills/<name>/SKILL.md` 가 실재하는 dir. command = command/*.md 의 stem."""
    skills = {
        d.name for d in SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    }
    commands = {f.stem for f in COMMANDS_DIR.glob("*.md")}
    return skills, commands


# ── ① 대응쌍 발견 정합 (pin 표 == 실 대응쌍) ─────────────────────────────────


def test_discovered_pairs_match_pin_table():
    """자동 발견한 스킬∩command 대응쌍이 PAIR_PINS 표와 정확히 일치.

    새 스킬+command 쌍이 추가되면(또는 삭제) 발견 집합이 표와 갈라져 fail — pin 을 추가/제거
    하라고 강제한다(신규 표면이 무pin 으로 새는 것 차단)."""
    skills, commands = _discover()
    paired = skills & commands
    pinned = set(PAIR_PINS)
    assert paired == pinned, (
        f"대응쌍 발견 집합이 PAIR_PINS 와 불일치 — "
        f"표에 없는 신규 쌍: {sorted(paired - pinned)} / "
        f"표에만 있고 실재 안 함: {sorted(pinned - paired)}. "
        "새 쌍은 pin 추가, 사라진 쌍은 pin 제거."
    )


# ── ② 짝-없음 (누락 클래스 커버·red) ─────────────────────────────────────────


def test_skill_only_pairs_are_allowlisted():
    """command 사본이 없는 스킬은 정확히 allowlist(pm-regression·harness-specific)뿐.

    신규 스킬을 추가하고 opencode command 사본을 안 만들면 skill-only 집합이 allowlist 를 넘어
    fail — "짝 없음(사본 누락)" 클래스를 red 로 만든다(T-0344 DoD)."""
    skills, commands = _discover()
    skill_only = skills - commands
    assert skill_only == set(SKILL_ONLY_ALLOWLIST), (
        f"command 사본 없는 스킬이 allowlist 와 불일치 — "
        f"allowlist 밖(사본 누락 의심): {sorted(skill_only - SKILL_ONLY_ALLOWLIST)} / "
        f"allowlist 인데 실재 안 함: {sorted(SKILL_ONLY_ALLOWLIST - skill_only)}. "
        "신규 스킬이면 opencode command 사본을 만들거나(대개 정답), harness-specific 정당 비대칭이면 "
        "SKILL_ONLY_ALLOWLIST 에 근거 주석과 함께 추가."
    )


def test_no_orphan_command_without_skill():
    """대응 스킬이 없는 command(고아 사본)는 없어야 한다.

    command 는 스킬의 수기 사본이므로 원본 스킬 없이 존재할 수 없다 — 원본 삭제/개명 시 stale
    사본을 잡는다."""
    skills, commands = _discover()
    orphans = commands - skills
    assert not orphans, (
        f"대응 스킬 없는 고아 command: {sorted(orphans)} — "
        "원본 스킬이 삭제/개명됐거나 사본 이름이 어긋남."
    )


# ── ③ pin 일치 (양쪽 content hash 박제·green baseline) ────────────────────────


def test_each_pair_content_hash_matches_pin():
    """각 대응쌍의 SKILL.md·command .md 정규화 hash 가 pin 표와 일치(현 트리 = green baseline).

    canonical 스킬이나 opencode 사본 어느 한쪽이 바뀌면 그 측 hash 가 pin 과 어긋나 fail +
    지시 메시지 — silent drift 를 loud 하게 만든다."""
    for name, (skill_pin, cmd_pin) in sorted(PAIR_PINS.items()):
        skill_path = SKILLS_DIR / name / "SKILL.md"
        cmd_path = COMMANDS_DIR / f"{name}.md"
        assert skill_path.is_file(), f"pin 된 스킬 파일 부재: {skill_path}"
        assert cmd_path.is_file(), f"pin 된 command 파일 부재: {cmd_path}"

        skill_hash = _content_hash(skill_path)
        cmd_hash = _content_hash(cmd_path)
        assert skill_hash == skill_pin, (
            f"[{name}] canonical 스킬(.claude/skills/{name}/SKILL.md) 변경 감지 — "
            f"pin {skill_pin[:12]}… != 현재 {skill_hash[:12]}…\n{_UPDATE_HINT}"
        )
        assert cmd_hash == cmd_pin, (
            f"[{name}] opencode 사본(templates/opencode/.opencode/command/{name}.md) 변경 감지 — "
            f"pin {cmd_pin[:12]}… != 현재 {cmd_hash[:12]}…\n{_UPDATE_HINT}"
        )


# ── ④ sensitivity (in-memory·non-vacuous·실 파일 미변경) ──────────────────────


def test_pin_guard_is_sensitive_to_change():
    """sensitivity — 1바이트 변경을 pin 이 검출함을 in-memory 로 입증(non-vacuous·실 파일 불변).

    실 스킬 내용에 1바이트를 더한 사본의 hash 가 pin 과 달라지는지(검출), 그리고 정규화가 공백-only
    churn(트레일링 스페이스·개행 형식)에는 흔들리지 않는지(안정)를 확인한다. 실 파일은 안 건드린다 —
    보고용 실 파일 편집 후 red→복원은 별도(수기 재현·PR 참고)."""
    name = "pm-env"          # PM 71 실증 표면 — sensitivity 대표.
    skill_pin, _ = PAIR_PINS[name]
    raw = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")

    # 음성 통제: 실 내용 → pin 그대로(false-positive 아님).
    assert _content_hash(SKILLS_DIR / name / "SKILL.md") == skill_pin

    # 양성: 1바이트(내용) 추가 → hash 어긋남(검출).
    def _hash_text(t: str) -> str:
        return hashlib.sha256(_normalize(t).encode("utf-8")).hexdigest()

    assert _hash_text(raw + "x") != skill_pin, "1바이트 내용 변경을 pin 이 못 잡음(vacuous 위험)"

    # 정규화 안정성: 트레일링 공백·CRLF churn 은 hash 를 흔들지 않는다(오탐 원천 제거).
    churn = raw.replace("\n", "  \n").replace("\n", "\r\n")
    assert _hash_text(churn) == skill_pin, "공백-only churn 이 pin 을 흔듦(정규화 미흡·오탐 위험)"


# ── pin 갱신 helper (직접 실행 시 붙여넣기용 PAIR_PINS 블록 출력·파일 미수정) ──
#   $ python3 tests/test_opencode_command_skill_pairing.py
# 현 트리의 대응쌍 전체를 현재 표와 동일한 정렬 형식으로 stdout 출력한다 — 사본 정합 후 이 파일의
# PAIR_PINS 블록을 그 출력으로 통째 교체하면 된다(hash 는 truncate 되지 않은 full 값).


def _render_pair_pins_block() -> str:
    """현 트리 대응쌍의 PAIR_PINS 선언 블록 문자열 (붙여넣기용·full hash·PAIR_PINS 형식과 동형)."""
    skills, commands = _discover()
    lines = ["PAIR_PINS: dict[str, tuple[str, str]] = {"]
    for name in sorted(skills & commands):
        skill_hash = _content_hash(SKILLS_DIR / name / "SKILL.md")
        cmd_hash = _content_hash(COMMANDS_DIR / f"{name}.md")
        head = f'    "{name}": ('
        lines.append(f'{head}"{skill_hash}",')
        lines.append(f'{" " * len(head)}"{cmd_hash}"),')
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(_render_pair_pins_block())
