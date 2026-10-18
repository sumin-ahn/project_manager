"""엔진 tools 트리의 Python 파일 전수 열거 (평면 진입 파일 + `pm_engine/` 패키지).

엔진 전수 규율 스캔이 같은 열거를 쓰도록 한 곳에 둔다. 키는
`path.relative_to(tools).as_posix()` 로 잡으면 평면 파일은 `board.py` 그대로, 패키지 모듈은
`pm_engine/infra/fs.py` 처럼 나온다.
"""
from __future__ import annotations

from pathlib import Path


def engine_python_files(tools: Path) -> list[Path]:
    """`tools` 아래 Python 파일 전수(하위 패키지 포함)를 경로 순으로 돌려준다."""
    return sorted(tools.rglob("*.py"))
