#!/usr/bin/env python3
"""worktree CLI 진입점.

명령 파서·dispatch 는 pm_engine.cli.worktree_cmd, 슬롯·task·장부·git 작업은 pm_engine 패키지가
소유한다. 이 파일은 콘솔 설정과 설치 앵커 Home 으로 CLI 를 부르는 일만 맡는다.
"""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import worktree_cmd
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return worktree_cmd.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
