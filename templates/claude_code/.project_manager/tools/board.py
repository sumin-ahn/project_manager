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


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.8.1"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다 (fail-loud·skew→명시 에러).

    불일치/부재(구형 형제는 리터럴 부재=None)면 사본 skew → 명시 에러(어느 파일이 어떤 rev 인지
    지목 + pm-update 안내). self-contained(engine_rev.py 런타임 의존 0)라 부분복사도 정확 검출한다.
    """
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            "엔진 사본 버전 불일치 — 로더 %s(rev=%r)가 형제 %s(rev=%r)를 로드했다 (사본 skew: 부분/수동 "
            "복사 또는 구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 "
            "재동기하라." % (Path(__file__).name, ENGINE_REV, sibling_filename, got)
        )
        err._engine_rev_skew = True  # fail-soft 로더가 재-raise 식별
        raise err


def _is_engine_rev_skew(exc):
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지.

    fail-soft sibling 로더의 `except Exception` 은 로드 실패/부재만 None 으로 흡수하고, 이
    판정이 True 인 예외(중첩 로드에서 검출된 형제 skew)는 재-raise 해 fail-loud 를 보존한다."""
    return getattr(exc, "_engine_rev_skew", False)


def main(argv=None):
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 안내로 번역한다."""
    try:
        console.configure_console_utf8()
        return board_cmd.main(_home(), argv)
    except Exception as exc:  # noqa: BLE001 — marked skew만 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return board_dispatch._report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
