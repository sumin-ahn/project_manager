"""실행 플랫폼 규칙 (층: infra).

OS 계열 조회(`_probe_os_name`)와 bash 문법 명령 문자열의 argv 분해(`split_command_argv`).
"""
from __future__ import annotations

import os
import shlex

from pm_engine.domain import shell_policy


def _probe_os_name() -> str:
    """Read the interpreter OS family through one injectable override.

    Tests that need to exercise the Windows branch of `_detect_py` on any
    host cannot rebind the global `os.name` — `pathlib` consults that same
    global to pick its path variant, and rebinding it mid-test
    breaks every path operation in the process (`NotImplementedError` on a
    `PosixPath` built under a monkeypatched `os.name == "nt"`). Routing the
    read through this function keeps the injection point inside this module
    instead of on the `os` global.
    """
    return os.name


def split_command_argv(command: str) -> list[str]:
    """bash 문법 command 문자열을 argv 로 분해한다(엔진 공용 함수, 모든 플랫폼에서 같은 규칙).

    두 호출처의 글은 bash 문법이다. `reviewer_cmd` 는 엔진이 `shlex.join` 으로 만든 글이고, 사용자 1회 허용
    명령은 기록(셸을 모르는 사용자 프롬프트 훅)과 소비(도구 호출 판정)가 같은 글을 같은 규칙으로 나눠 맞춰 보는
    값이다. 가드 훅의 bash 읽기와 같은 준비(`shell_policy._protect_shell_windows_path_separators`)로 drive/UNC
    절대경로 토큰의 구분자를 표시로 바꾼 뒤 shlex 인용 규칙을 적용하고, 토큰화가 끝난 다음 표시를 원래 ``\\`` 로
    되돌린다. 그래서 ``C:\\Users\\pm\\codex.exe`` 가 ``C:Userspmcodex.exe`` 로 뭉개지지 않는다.
    """
    return [
        shell_policy.restore_shell_windows_path_separators(token)
        for token in shlex.split(shell_policy._protect_shell_windows_path_separators(command))
    ]
