#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""보드 CLI 진입점.

명령 파서·분류·dispatch·소유 PM 홈 미확정 안내는 pm_engine.cli.board_cmd, 어댑터 훅 판정은
pm_engine.cli.hooks, 각 명령의 처리는 pm_engine.cli·app 이 소유한다. 이 파일은 Python 런타임 하한
선검사·콘솔 설정과 설치 앵커 Home 으로 CLI 를 부르는 일만 맡는다.

하네스 훅은 이 파일을 한 번 실행해 판정까지 받는다. 그래서 파일 전체를 하한 미만 Python 도 파싱할 수
있는 문법으로 쓰고, 패키지 import 전에 런타임 하한을 확인한다(별도 probe 프로세스 없음).
"""
from __future__ import print_function

import sys

# pm_engine.MIN_PYTHON 미러. tests/test_python_floor.py 가 skew 를 차단한다.
MIN_PYTHON = (3, 11)

if sys.version_info[:2] < MIN_PYTHON:
    print("[중단] Python %d.%d+ 필요 · 현재 %d.%d" % (MIN_PYTHON + tuple(sys.version_info[:2])),
          file=sys.stderr)
    sys.exit(1)

import os
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import board_cmd
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return board_cmd.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
