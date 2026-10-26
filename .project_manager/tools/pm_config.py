#!/usr/bin/env python3
"""pm-config CLI 진입점.

서브커맨드 파서·dispatch·도움말은 pm_engine.cli.config_cmd, 각 명령의 처리는 pm_engine.cli.config_* 가
소유한다. 이 파일은 설치 앵커 Home·엔진 각인 값 전달·엔진 사본 불일치 진단과 CLI 호출만 맡는다.
"""

from __future__ import annotations


import argparse
import functools
import os
import sys
from pathlib import Path


_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from pm_engine.infra import console, lease_store, pm_paths, repo_files
from pm_engine.app import repo_protection
from pm_engine.cli import config_adapter
from pm_engine.cli import config_cmd
from pm_engine.cli import config_console
from pm_engine.cli import config_install
from pm_engine.cli import config_models
from pm_engine.cli import config_repo
from pm_engine.cli import config_slots
from pm_engine.cli import config_status
from pm_engine.cli import config_tasks
from pm_engine.cli import config_upstream


# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py 와 동일 앵커 관례
# (어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).
REPO = Path(__file__).resolve().parents[2]


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.8.1"


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지.

    fail-soft sibling 로더의 `except Exception` 은 로드 실패/부재만 None 으로 흡수하고, 이
    판정이 True 인 예외(중첩 로드에서 검출된 형제 skew)는 재-raise 해 fail-loud 를 보존한다
    (예: 신 pm_config→신 board→구 형제 검출이 None 강등되지 않게)."""
    return getattr(exc, "_engine_rev_skew", False)


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 복구 안내로 번역한다."""
    try:
        console.configure_console_utf8()
        return config_cmd.main(pm_paths.home_at(REPO), argv, engine_rev=ENGINE_REV)
    except Exception as exc:  # noqa: BLE001 — marked skew만 사용자 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return _report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
