"""실행 플랫폼 규칙 (층: infra).

OS 계열 조회(`_probe_os_name`)와 명령 문자열의 플랫폼별 argv 분해(`split_command_argv`).
"""
from __future__ import annotations

import os
import shlex

from pm_engine.domain import shell_policy


def _probe_os_name() -> str:
    """Read the interpreter OS family through one injectable seam.

    Tests that need to exercise the Windows branch of `split_command_argv` or
    `_detect_py` on any host cannot rebind the global `os.name` — `pathlib`
    consults that same global to pick its flavour, and rebinding it mid-test
    breaks every path operation in the process (`NotImplementedError` on a
    `PosixPath` built under a monkeypatched `os.name == "nt"`). Routing the
    read through this function keeps the injection point inside this module
    instead of on the `os` global.
    """
    return os.name


def split_command_argv(command: str, *, windows: bool | None = None) -> list[str]:
    """command 문자열을 **실행 플랫폼 규칙**으로 argv 로 분해한다 (엔진 공용 seam).

    POSIX 규칙에서 ``\\`` 는 escape 라 `shlex.split` 만 쓰면 Windows 실행 경로의 구분자가 통째로
    사라진다 — ``C:\\Users\\pm\\codex.exe`` 가 ``C:Userspmcodex.exe`` 가 되어 실행이 `FileNotFoundError`
    로 끝난다. Windows 에서는 `pm_engine.domain.shell_policy` 의 shell 판정 경로와 **같은 보호 규칙**
    (`_SHELL_WINDOWS_ABSOLUTE_PATH`)으로 절대경로 토큰의 구분자를 표식으로 바꾼 뒤 shlex 인용
    규칙을 적용하고, 토큰화가 끝난 다음 표식을 원래 ``\\`` 로 되돌린다. 인용 의미(따옴표·공백)는
    그대로 두고 경로 구분자만 살린다.

    POSIX 실행 플랫폼에서는 종전 `shlex.split` 그대로다 — 그쪽에서 ``\\`` 는 실제로 escape 이므로
    보호를 걸면 채택자가 의도한 escape 를 바꾼다.

    `windows=None` 이면 실행 플랫폼(`os.name == "nt"`)을 본다. 명시 인자는 POSIX 개발기에서
    Windows 분해를 그대로 태우는 주입 seam 이다(회귀가 실행 플랫폼에 의존하지 않게).
    """
    if windows is None:
        windows = _probe_os_name() == "nt"
    if not windows:
        return shlex.split(command)
    return [
        shell_policy.restore_shell_windows_path_separators(token)
        for token in shlex.split(shell_policy._protect_shell_windows_path_separators(command))
    ]
