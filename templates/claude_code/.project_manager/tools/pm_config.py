#!/usr/bin/env python3
"""pm-config CLI 진입점.

서브커맨드 파서·dispatch·도움말은 pm_engine.cli.config_cmd, 각 명령의 처리는 pm_engine.cli.config_* 가
소유한다. 이 파일은 설치 앵커 Home 해소와 CLI 호출만 맡는다.
"""

from __future__ import annotations


import os
import sys
from pathlib import Path


_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from pm_engine.infra import console, pm_paths
from pm_engine.cli import config_cmd

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py 와 동일 앵커 관례
# (어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).
REPO = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    """콘솔 인코딩 설정 후 pm-config CLI 를 실행한다."""
    console.configure_console_utf8()
    return config_cmd.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main())
