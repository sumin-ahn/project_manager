#!/usr/bin/env python3
"""engine.manifest 기반 배포 sync 의 실행 진입점 — upstream 엔진 경로만 덮어쓴다.

이 파일은 콘솔을 설정하고 이 사본 위치의 Home 으로 `pm_engine.cli.update_cmd.main` 을 부른다.
사용법·동작·결정은 `pm_engine.cli.update_cmd` 모듈 문서(`pm_update.py --help`)가 소유한다. 자기 갱신의
완료 프로세스도 옮겨 받은 이 파일을 같은 인자로 다시 실행한다.
"""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import update_cmd
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return update_cmd.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
