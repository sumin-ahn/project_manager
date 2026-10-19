"""실행 플랫폼 규칙 (층: infra).

OS 계열 조회(`_probe_os_name`)와 명령 문자열의 플랫폼별 argv 분해(`split_command_argv`), 회귀 명령 실행과
출력 중계(`_run_regression_cmd`·`_tee_stream`).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading

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


def _tee_stream(stream, echo: Callable[[str], None]) -> str:
    """자식 스트림을 줄 단위로 **실시간 echo 하면서 동시에** 전체를 모아 돌려준다.

    수집 증감 표시는 pytest 요약행을 읽어야 해서 출력을 버퍼에 담아야 하는데, 캡처만 하면
    `git push` 회귀(pre-push 훅)가 수 분 동안 **완전 무출력**으로 멈춰 있는 것처럼 보인다
    (실측 367s). tee 로 진행을 그대로 흘리면서 파싱용 버퍼를 유지해 둘 다 만족한다.
    스트림 미개방(None)은 빈 문자열.
    """
    if stream is None:
        return ""
    chunks: list[str] = []
    for line in stream:
        chunks.append(line)
        echo(line)
    return "".join(chunks)


def _run_regression_cmd(cmd: str, cwd: str,
                        env: dict[str, str]) -> tuple[int, str, str]:
    """회귀 test_cmd 를 실행하고 `(rc, stdout, stderr)` 를 돌려준다 — run 두 경로 공용 단일 지점.

    출력을 버퍼에 담는 이유는 수집 증감 표시가 pytest 요약행을 읽어야 하기 때문이다(별도
    `--co -q` 재실행 대신 *이미 돈* 실행의 요약을 쓴다·자식 1회). 동시에 `_tee_stream` 으로
    실시간 echo 해 캡처 이전의 가시성을 그대로 유지한다(무출력 대기 근절).

    **두 스트림을 분리해 돌려준다** — 요약행 파싱은 **stdout 단독**이어야 한다. pytest 요약은
    stdout 에만 나오므로, stderr 에 섞인 로그(`3 passed …`)를 합치면 그게 "마지막 요약행"으로
    뽑혀 수집수가 오염된다(false-green/false-RED 양방향). 재출력·진단은 두 스트림 모두 쓴다.
    스트림 읽기는 스레드 2개로 동시에 — 한쪽만 읽으면 파이프가 차서 자식이 멈춘다(교착).
    """
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    captured: dict[str, str] = {}

    def _pump(name: str, stream, echo: Callable[[str], None]) -> None:
        captured[name] = _tee_stream(stream, echo)

    pumps = [
        threading.Thread(target=_pump, daemon=True, args=(
            "stdout", proc.stdout, lambda line: print(line, end="", flush=True))),
        threading.Thread(target=_pump, daemon=True, args=(
            "stderr", proc.stderr, lambda line: print(line, end="", flush=True,
                                                      file=sys.stderr))),
    ]
    for pump in pumps:
        pump.start()
    rc = proc.wait()
    for pump in pumps:
        pump.join()
    return rc, captured.get("stdout", ""), captured.get("stderr", "")
