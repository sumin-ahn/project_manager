"""T-0288 — 출하 PM 스킬/커맨드의 Windows 표기(py 런처·.cmd) 파리티 가드.

Windows PM 세션의 LLM 이 스킬/커맨드 안 literal `python3 …`/`./…​.sh` 를 그대로 실행하면
WindowsApps 가짜 shim(Permission denied)·bash 부재에 걸려 진단·재시도·fallback 루프(토큰·
컨텍스트 낭비)를 유발한다. 각 스킬/커맨드가 `python3` 를 보이면 Windows 런처 `py`(`py -3.x`)를,
`./…​.sh` 파사드를 보이면 `.cmd` 등가를 **함께** 명시하는지 못박는다 — per-skill 명시성이 글로벌
CLAUDE.md/AGENTS.md 노트 의존보다 강하다([[agent-guides-like-live-delegation]]).

⚠️ **CANONICAL 소스만 스캔** — `.claude/skills/*/SKILL.md`(① root canonical·@render). opencode 는
`.opencode/command` 수기 사본 채널을 은퇴하고(T-0364·ADR-0065) 이 canonical 스킬을 네이티브
소비하므로, 별도 opencode 표면을 스캔할 필요가 없다(단일 소스). `templates/{claude_code,opencode}/
.claude/skills` 는 **전파 미러**라 `pm_update --target` 전파 *전엔* canonical 뒤처져(stale) false-red
를 낸다 → 스캔 제외(test_terminology 의 templates 제외 정신과 동일).

재발 교훈(메모리): 재발하는 표기/규칙은 지식이 아니라 테스트로 못박는다.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# ── canonical 스캔 표면 (전파 미러 templates/claude_code 는 의도적 제외) ──────────
_CANONICAL_GLOBS = (
    ".claude/skills/*/SKILL.md",
)


def _canonical_files() -> list[Path]:
    files: list[Path] = []
    for g in _CANONICAL_GLOBS:
        files += [Path(p) for p in glob.glob(str(REPO / g))]
    return sorted(f for f in files if f.is_file())


_CANONICAL_FILES = _canonical_files()
_IDS = [f.relative_to(REPO).as_posix() for f in _CANONICAL_FILES]

# ── 표기 마커 ─────────────────────────────────────────────────────────────────
# 실제 command 호출 `python3 <arg>` (프로즈의 python3 도 포함되나, 그 경우도 py 런처 노트를
#   요구하는 게 맞다 — 문서가 python3 를 보이면 Windows 대안을 병기해야 한다).
_PY3_CMD = re.compile(r"python3\s")
# Windows 런처 표기 — `py -3`(예 `py -3.12`). "cd X && cmd" 의 'cmd'(dot 없음)와 무관.
_PY_LAUNCHER = re.compile(r"\bpy -3")
# `./<name>.sh` 루트 파사드 호출.
_SH_FACADE = re.compile(r"\./[\w.-]+\.sh\b")
# `.cmd` Windows 등가(예 `.\pm-update.cmd`). literal dot 요구 → 산문의 "cmd" 오탐 없음.
_CMD_FACADE = re.compile(r"\.cmd\b")

# 이 ticket 이 감사·통일한 직접-python 스킬(claude 9) — 가드 non-vacuous 앵커.
_DIRECT_PYTHON_CLAUDE = [
    ".claude/skills/pm-bootstrap/SKILL.md",
    ".claude/skills/pm-dev-delegate/SKILL.md",
    ".claude/skills/pm-handoff/SKILL.md",
    ".claude/skills/pm-qa/SKILL.md",
    ".claude/skills/pm-regression/SKILL.md",
    ".claude/skills/pm-wave-claim/SKILL.md",
    ".claude/skills/pm-wave-finish/SKILL.md",
    ".claude/skills/pm-worktree/SKILL.md",
    ".claude/skills/spike-new/SKILL.md",
]
# 파사드 스킬(claude 2) — `.sh` 예시 + `.cmd` 병기.
_FACADE_CLAUDE = [
    ".claude/skills/pm-env/SKILL.md",
    ".claude/skills/pm-update/SKILL.md",
]


# ── (1) python3 참조 ↔ py 런처 병기 (파리티) ──────────────────────────────────

@pytest.mark.parametrize("path", _CANONICAL_FILES, ids=_IDS)
def test_python3_reference_has_windows_py_launcher(path: Path):
    """스킬/커맨드가 `python3 …` 를 보이면 Windows 런처 `py`(`py -3.x`)도 병기한다.

    literal `python3` 만 있으면 Windows 세션 LLM 이 그대로 실행 → shim 실패 → fallback
    토큰 낭비. per-skill 표준 Windows 노트 블록이 이를 막는다 (T-0288).
    """
    text = path.read_text(encoding="utf-8")
    if _PY3_CMD.search(text):
        assert _PY_LAUNCHER.search(text), (
            f"{path.relative_to(REPO).as_posix()} — `python3` 참조가 있는데 Windows 런처 "
            f"`py`(예 `py -3.12`) 표기가 없다. 상단 표준 Windows 노트 블록을 삽입하라 (T-0288)."
        )


# ── (2) .sh 파사드 참조 ↔ .cmd 병기 (파리티) ──────────────────────────────────

@pytest.mark.parametrize("path", _CANONICAL_FILES, ids=_IDS)
def test_sh_facade_reference_has_windows_cmd(path: Path):
    r"""스킬/커맨드가 `./…​.sh` 파사드를 보이면 Windows `.cmd` 등가도 병기한다.

    `./…​.sh` 는 bash 전용 — PowerShell/cmd 세션엔 `.\<name>.cmd` 가 짝이다 (T-0288).
    """
    text = path.read_text(encoding="utf-8")
    if _SH_FACADE.search(text):
        assert _CMD_FACADE.search(text), (
            f"{path.relative_to(REPO).as_posix()} — `./…​.sh` 파사드 참조가 있는데 Windows "
            f"`.cmd` 등가 표기가 없다 (T-0288)."
        )


# ── (3) non-vacuous 커버리지 — 감사 대상이 실제로 스캔되고 표기를 담는지 ─────────

def test_scan_set_non_empty():
    """canonical 스킬/커맨드 스캔이 비지 않는다 (glob 경로 오류 방지)."""
    assert _CANONICAL_FILES, (
        f"canonical 스킬/커맨드 스캔 0 — glob {_CANONICAL_GLOBS} 경로 확인"
    )


@pytest.mark.parametrize("rel", _DIRECT_PYTHON_CLAUDE)
def test_direct_python_claude_skill_carries_both_markers(rel: str):
    """감사 대상 직접-python claude 스킬 9개가 `python3` + `py -3` 을 **둘 다** 담는다.

    파리티 가드(1)가 vacuous(python3 부재로 skip)로 통과하는 걸 막는 앵커 —
    이 9개는 실제로 python3 를 쓰고, 따라서 py 런처 노트를 반드시 가져야 한다 (T-0288 DoD).
    """
    path = REPO / rel
    assert path.is_file(), f"{rel} 부재"
    text = path.read_text(encoding="utf-8")
    assert _PY3_CMD.search(text), f"{rel} — 직접-python 스킬인데 `python3` 참조 없음(감사 전제 붕괴)"
    assert _PY_LAUNCHER.search(text), f"{rel} — Windows 런처 `py`(py -3.x) 노트 누락 (T-0288)"


@pytest.mark.parametrize("rel", _FACADE_CLAUDE)
def test_facade_claude_skill_carries_cmd(rel: str):
    """파사드 스킬(pm-env·pm-update)이 `.cmd` Windows 등가를 담는다 (T-0288 DoD)."""
    path = REPO / rel
    assert path.is_file(), f"{rel} 부재"
    assert _CMD_FACADE.search(path.read_text(encoding="utf-8")), (
        f"{rel} — `.cmd` Windows 등가 표기 누락 (T-0288)"
    )
