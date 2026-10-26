#!/usr/bin/env python3
"""pm_import — PM 프레임워크 import 단일 진입 커맨드 (--new = PM 홈 생성 / --into = 기존 프로젝트 임베드).

이 파일은 실행 진입점이다 — 인터프리터 하한을 먼저 검사한 뒤 콘솔을 설정하고 이 사본 위치의 Home 으로
`pm_engine.cli.import_cmd.main` 을 부른다. 사용법·동작·결정은
`pm_engine.cli.import_args` 모듈 문서(`pm_import.py --help`)가 소유한다.

하한 검사는 패키지 import 전에 돈다. tomllib 을 쓰는 설치 공급자(패키지 CLI)보다 먼저 실패해야 사유가
분명하다. 이 파일은 하한 미만 Python(3.10) 문법으로 전체 parse 가능하다.
"""
import sys

# pm_engine.MIN_PYTHON 미러. tests/test_python_floor.py 가 skew 를 차단한다.
MIN_PYTHON = (3, 11)

if sys.version_info[:2] < MIN_PYTHON:
    raise SystemExit("Python %d.%d+ 필요 · 현재 %d.%d" % (MIN_PYTHON + tuple(sys.version_info[:2])))

import os
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import import_cmd
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return import_cmd.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
