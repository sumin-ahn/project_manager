"""opencode 어댑터 인코딩 prefix 회귀 가드 (T-0031).

opencode 어댑터(AGENTS·command·agents)가 엔진 호출에 bash 전용 env prefix
`PYTHONUTF8=1 PYTHONIOENCODING=utf-8 <명령>` 를 강제하던 잔재를 제거했다 — PowerShell 은
`VAR=val cmd` bash 문법을 모르므로 `CommandNotFoundException` 으로 깨진다. 게다가 엔진이
PM 7차(C1 파일 IO `encoding="utf-8"`·C2 콘솔 reconfigure)로 인코딩을 코드로 처리하므로 이
prefix 는 redundant 다. 따라서 어댑터 md 에 bash prefix 패턴이 **0건**임을 단언한다(회귀 가드).

board.py 의 자식 인코딩(T-0024)은 파이썬 dict `{"PYTHONUTF8": "1", ...}` → `subprocess(env=)`
형태(크로스플랫폼·bash 아님)라 이 패턴에 매칭되지 않고, 다른 디렉토리(tools/)라 검사 대상도 아니다.

stdlib 만 사용 — opencode CLI 미실행. 파일 iterate·존재 시만 검사(hermetic).
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OPENCODE = REPO / "templates" / "opencode"

# bash 전용 env prefix 패턴 — 명령 앞에 붙던 형태. board.py 의 dict 문법
# (`"PYTHONUTF8": "1"`)·셸별 안내 문구(`$env:PYTHONUTF8`)는 이 패턴에 매칭되지 않는다.
BASH_PREFIX = "PYTHONUTF8=1 PYTHONIOENCODING=utf-8"


def _adapter_md_files() -> list[Path]:
    """검사 대상 어댑터 md 파일 — 존재하는 것만 (hermetic)."""
    candidates = [
        OPENCODE / "AGENTS.md",
        OPENCODE / "AGENTS.lite.md",
    ]
    candidates += sorted((OPENCODE / ".opencode" / "command").glob("*.md"))
    candidates += sorted((OPENCODE / ".opencode" / "agents").glob("*.md"))
    return [p for p in candidates if p.exists()]


def test_adapter_md_files_present():
    """어댑터 md 파일이 실제로 존재한다 — 빈 iterate 로 가드가 무의미해지지 않게."""
    files = _adapter_md_files()
    assert files, f"opencode 어댑터 md 파일을 못 찾음: {OPENCODE}"


def test_no_bash_env_prefix_in_adapter_md():
    """어댑터 md 에 bash 전용 env prefix `PYTHONUTF8=1 PYTHONIOENCODING=utf-8` 0건.

    PowerShell 비호환 + 엔진 코드-레벨 인코딩(PM 7차)으로 redundant — 제거 회귀 가드.
    """
    offenders = []
    for path in _adapter_md_files():
        text = path.read_text(encoding="utf-8")
        if BASH_PREFIX in text:
            for i, line in enumerate(text.splitlines(), 1):
                if BASH_PREFIX in line:
                    offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, (
        "opencode 어댑터에 bash 전용 env prefix 잔존 (PowerShell 비호환·T-0031 회귀):\n"
        + "\n".join(offenders)
    )
