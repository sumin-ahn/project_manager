"""한 실행이 기준으로 삼는 경로 묶음 (층: domain).

엔진 함수가 첫 인자로 받는 값 객체다. 만드는 곳은 `pm_engine.infra.pm_paths.home_at` 하나다.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class Home(NamedTuple):
    """실행 기준 경로.

    root: 도구가 설치된 트리(자기 앵커). 상대경로 표시와 git 작업 위치의 기준이다.
    board_root: board(tickets·areas) 루트.
    local_dir: 클론별 작업 디렉터리(`.project_manager/.local`).
    local_conf: 클론별 설정 파일(`.project_manager/local.conf`).
    """

    root: Path
    board_root: Path
    local_dir: Path
    local_conf: Path
