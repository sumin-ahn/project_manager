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
_SH_FACADE = re.compile(r"\./(?P<name>[\w.-]+)\.sh\b")
# 표준 Windows 노트는 하나의 연속 blockquote 안에서 PowerShell 5.x의 `&&` 비호환을 경고한다.
_WINDOWS_NOTE_BLOCK = re.compile(r"(?m)(?:^>[^\n]*(?:\n|$))+")
_POWERSHELL_AND_WARNING = re.compile(
    r"PowerShell\s+5\.x[^\n]*&&[^\n]*(?:미지원|ParseError)"
)

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
# 파사드 스킬 — `.sh` 예시 + 같은 실명의 `.cmd` 병기.
_FACADE_CLAUDE = [
    ".claude/skills/pm-env/SKILL.md",
    ".claude/skills/pm-update/SKILL.md",
    ".claude/skills/pm-release/SKILL.md",
]


def _note_blocks(text: str) -> list[str]:
    """연속 blockquote 노트만 반환해 흩어진 마커의 우연한 조합을 배제한다."""
    return _WINDOWS_NOTE_BLOCK.findall(text)


def _has_python_windows_note(text: str) -> bool:
    """한 노트 블록이 python3·py 런처·PowerShell 경고를 모두 갖는지."""
    return any(
        _PY3_CMD.search(block)
        and _PY_LAUNCHER.search(block)
        and _POWERSHELL_AND_WARNING.search(block)
        for block in _note_blocks(text)
    )


def _facade_windows_note_pairs(text: str) -> list[str]:
    """한 표준 노트 안에서 basename이 같은 `.sh`↔`.cmd` 쌍을 반환한다."""
    pairs: list[str] = []
    for block in _note_blocks(text):
        if not _POWERSHELL_AND_WARNING.search(block):
            continue
        for name in sorted(set(_SH_FACADE.findall(block))):
            sh_name = f"./{name}.sh"
            cmd_name = ".\\" + name + ".cmd"
            if cmd_name in block:
                pairs.append(f"{sh_name} ↔ {cmd_name}")
    return pairs


# ── (1) python3 참조 ↔ 표준 Windows 노트 블록 ────────────────────────────────

@pytest.mark.parametrize("path", _CANONICAL_FILES, ids=_IDS)
def test_python3_reference_has_windows_py_launcher(path: Path):
    """`python3` 참조 스킬은 py 런처와 PowerShell 경고를 한 표준 노트에 둔다.

    마커가 문서 곳곳에 흩어져 있어도 통과시키지 않아 실제 실행 지점에서 노트를 놓치는
    형해화를 막는다.
    """
    text = path.read_text(encoding="utf-8")
    if _PY3_CMD.search(text):
        assert _has_python_windows_note(text), (
            f"{path.relative_to(REPO).as_posix()} — `python3` 참조가 있는데 한 blockquote 안에 "
            "Windows `py -3.x` 런처와 PowerShell 5.x `&&` 비호환 경고를 갖춘 표준 노트가 없다."
        )


# ── (2) .sh 파사드 참조 ↔ 실명 .cmd + PowerShell 경고 ────────────────────────

@pytest.mark.parametrize("path", _CANONICAL_FILES, ids=_IDS)
def test_sh_facade_reference_has_windows_cmd(path: Path):
    r"""각 `./<name>.sh`는 같은 노트에서 `.\<name>.cmd`와 PowerShell 경고를 병기한다.

    무관한 `.cmd` 한 줄만 있어도 통과하던 느슨한 마커 검사를 닫고 실명 대응을 강제한다.
    """
    text = path.read_text(encoding="utf-8")
    if _SH_FACADE.search(text):
        pairs = _facade_windows_note_pairs(text)
        assert pairs, (
            f"{path.relative_to(REPO).as_posix()} — 한 blockquote 안에 basename이 같은 "
            "`.sh`↔`.cmd` 등가와 PowerShell 5.x `&&` 경고를 갖춘 표준 노트가 없다."
        )
        # 참조된 .sh 실명 전수가 노트에서 짝지어져야 한다 — 한 쌍만 있으면 green 이던
        # 부분 커버(예: pm-update.sh 병기·pm-config.sh 누락)를 닫는다.
        referenced = set(_SH_FACADE.findall(text))
        paired = {p.split("./", 1)[1].split(".sh", 1)[0] for p in pairs}
        missing = sorted(referenced - paired)
        assert not missing, (
            f"{path.relative_to(REPO).as_posix()} — 문서가 참조하는 `.sh` 파사드 중 표준 노트에서 "
            f"`.cmd` 짝을 못 얻은 실명: {missing} (노트에 `.\\<name>.cmd` 병기 필요)."
        )


# ── (3) non-vacuous 커버리지 — 감사 대상이 실제로 스캔되고 표기를 담는지 ─────────

def test_scan_set_non_empty():
    """canonical 스킬/커맨드 스캔이 비지 않는다 (glob 경로 오류 방지)."""
    assert _CANONICAL_FILES, (
        f"canonical 스킬/커맨드 스캔 0 — glob {_CANONICAL_GLOBS} 경로 확인"
    )


@pytest.mark.parametrize("rel", _DIRECT_PYTHON_CLAUDE)
def test_direct_python_claude_skill_carries_both_markers(rel: str):
    """직접-python 스킬 9개가 표준 Windows 노트 블록을 실제로 담는다.

    파리티 가드가 python3 부재로 건너뛰는 공허 통과를 막는 고정 앵커다.
    """
    path = REPO / rel
    assert path.is_file(), f"{rel} 부재"
    text = path.read_text(encoding="utf-8")
    assert _PY3_CMD.search(text), f"{rel} — 직접-python 스킬인데 `python3` 참조 없음(감사 전제 붕괴)"
    assert _has_python_windows_note(text), (
        f"{rel} — `py -3.x` 런처 + PowerShell 5.x `&&` 경고 표준 노트 누락"
    )


@pytest.mark.parametrize("rel", _FACADE_CLAUDE)
def test_facade_claude_skill_carries_cmd(rel: str):
    """파사드 스킬이 실명 `.cmd` 등가와 PowerShell 경고를 담는다."""
    path = REPO / rel
    assert path.is_file(), f"{rel} 부재"
    text = path.read_text(encoding="utf-8")
    assert _SH_FACADE.search(text), f"{rel} — `.sh` 파사드 참조 없음(감사 전제 붕괴)"
    pairs = _facade_windows_note_pairs(text)
    assert pairs, f"{rel} — 실명 `.sh`↔`.cmd` + PowerShell 경고 표준 노트 누락"
