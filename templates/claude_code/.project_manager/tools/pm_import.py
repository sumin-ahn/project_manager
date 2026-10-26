#!/usr/bin/env python3
"""pm_import — PM 프레임워크 import 단일 진입 커맨드 (--new = PM 홈 생성 / --into = 기존 프로젝트 임베드).

이 파일은 실행 진입점이다 — 인터프리터 하한을 먼저 검사한 뒤 이 사본 위치의 Home 으로
`pm_engine.cli.import_cmd.main` 을 부른다. 사용법·동작·결정은
`pm_engine.cli.import_args` 모듈 문서(`pm_import.py --help`)가 소유한다.
"""

from __future__ import annotations


import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import pm_engine  # noqa: E402


def _require_python(version_info=None) -> None:
    """tomllib import 전에 현재 인터프리터 하한을 명시적으로 검증한다."""
    current = sys.version_info if version_info is None else version_info
    minimum = tuple(pm_engine.MIN_PYTHON)
    if tuple(current[:2]) < minimum:
        major, minor = minimum
        raise SystemExit(
            f"Python {major}.{minor}+ 필요 · 현재 {current[0]}.{current[1]}"
        )


# 반드시 tomllib 보다 먼저 실행한다. 이 파일은 Python 3.10 문법으로 전체 parse 가능하며
# future annotations 덕분에 아래 list[...] / X | None 표기도 체크 전 평가되지 않는다.
_require_python()

# tomllib 을 쓰는 설치 공급자(package CLI)는 하한 검사 뒤에 적재한다(하한 검사가 먼저 실패해야 사유가 분명하다).
from pm_engine.cli import import_cmd  # noqa: E402
from pm_engine.infra import pm_paths  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    sys.exit(import_cmd.main(pm_paths.home_at(REPO)))
