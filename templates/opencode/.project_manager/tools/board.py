#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""보드 CLI 진입점.

명령 파서·분류·dispatch 는 pm_engine.cli.board_cmd, 어댑터 훅 판정은 pm_engine.cli.hooks,
각 명령의 처리는 pm_engine.cli·app 이 소유한다. 이 파일은 Python 런타임 하한 선검사·콘솔 설정·
설치 앵커 Home·엔진 사본 불일치 진단과 CLI 호출만 맡는다.

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

import argparse
import ast
import contextlib
import json
import os
import shlex
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import yaml

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.app import freshness_lint, hooks
from pm_engine.cli import regression_cmd  # noqa: E402
from pm_engine.domain import design_rules, git_policy, identity_args, round_rules, shell_policy, ticket_rules
from pm_engine.infra import console, delegate_ledger, fs, pm_paths, repo_coordinates, repo_files
from pm_engine.infra import task_store
from pm_engine.infra import branch_store
from pm_engine.infra import worktree_git

from pm_engine.cli import board_cmd
from pm_engine.cli import board_list
from pm_engine.app import engine_guard
from pm_engine.app import tier_signals
from pm_engine.cli import board_cluster
from pm_engine.cli import board_dispatch
from pm_engine.cli import board_lint
from pm_engine.cli import board_repin
from pm_engine.cli import board_round
from pm_engine.domain import board_command_rules
from pm_engine.infra import worktree_paths

from pm_engine.cli import board_init
from pm_engine.cli import board_migrate_identity
from pm_engine.cli import board_prefix
from pm_engine.cli import board_ticket


REPO = Path(__file__).resolve().parents[2]


def _home():
    """이 설치 사본의 CLI 앵커 Home — 자기 앵커 `REPO`."""
    return pm_paths.home_at(REPO)


def main(argv=None):
    """콘솔 인코딩 설정 후 board CLI 를 실행한다."""
    console.configure_console_utf8()
    return board_cmd.main(_home(), argv)


if __name__ == "__main__":
    sys.exit(main())
