"""더미 하네스 세션 사슬 헬퍼 — 하네스 이름으로 뜬 프로세스 아래에서 명령을 한 번 돌린다.

장부(`worktree_pool`)가 적는 pid 는 명령을 실행한 엔진 프로세스가 아니라 그 명령을 부른
**세션**(`pm_relay.session_pid`)이다. 그 계약을 주변 프로세스 사슬에 기대지 않고 검증하려면
테스트가 세션을 직접 띄워야 한다 — 실 하네스(claude·codex·opencode)를 부르는 게 아니라
하네스 **이름**으로 만든 심링크로 파이썬 인터프리터를 띄운다. `ps -eo comm=` 은 execve 에 넘긴
경로의 basename 을 그대로 실으므로(심링크 이름 보존) 그 프로세스가 하네스로 관측된다.
주변에 실제 하네스 세션이 있어도 판정은 흔들리지 않는다 — 더미가 순회에서 더 가깝다.

사슬은 실 구동(`claude → bash → python`)과 같은 3단이다:

    더미 하네스(`claude` 심링크) → 중간 홉(평범한 python) → 요청한 명령

중간 홉이 load-bearing 이다 — 홉이 없으면 하네스가 곧 직계 부모라서 "하네스 조상을 찾았다"와
"못 찾아 부모로 떨어졌다"가 같은 값이 되어 판정을 가르지 못한다.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator, NamedTuple

# 더미 세션이 첫 결과 줄을 내기까지의 상한. 넘기면 kill 하고 실패로 올린다(무기한 대기 금지).
SESSION_START_TIMEOUT_SECONDS = 120
# 블록을 나간 뒤 더미 세션이 스스로 끝나기를 기다리는 상한.
SESSION_STOP_TIMEOUT_SECONDS = 30

# 더미 하네스 프로세스가 도는 코드. 자기 `sys.executable` 은 하네스 이름 심링크라, 중간 홉은
# 테스트가 넘긴 **실 인터프리터 경로**로 띄운다(홉까지 하네스 이름이면 사슬이 무의미해진다).
_DRIVER_SOURCE = '''\
import json, os, subprocess, sys

real_python, hop = sys.argv[1], sys.argv[2]
done = subprocess.run([real_python, hop, *sys.argv[3:]], capture_output=True,
                      text=True, encoding="utf-8", errors="replace")
print(json.dumps({"pid": os.getpid(), "rc": done.returncode,
                  "stdout": done.stdout, "stderr": done.stderr}), flush=True)
sys.stdin.readline()   # 테스트가 stdin 을 닫을 때까지 이 세션은 살아 있는다.
'''

# 중간 홉 — 실 구동의 도구 셸 자리. 받은 명령을 그대로 돌려 결과만 되돌린다.
_HOP_SOURCE = '''\
import subprocess, sys

done = subprocess.run(sys.argv[1:], capture_output=True, text=True,
                      encoding="utf-8", errors="replace")
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
raise SystemExit(done.returncode)
'''


def real_process_table_supported() -> bool:
    """실 프로세스 표(`ps`)로 조상 사슬을 관측할 수 있는 환경인지 판정한다."""
    return os.name != "nt" and shutil.which("ps") is not None


class SessionRun(NamedTuple):
    """더미 세션 아래에서 한 번 실행한 결과."""

    pid: int        # 더미 하네스 세션 프로세스의 pid (= 엔진이 장부에 적어야 할 값).
    rc: int
    stdout: str
    stderr: str


@contextlib.contextmanager
def live_harness_session(
    workdir: Path, argv: "list[str]", *, harness: str = "claude",
) -> Iterator[SessionRun]:
    """`harness` 이름으로 뜬 더미 세션 아래(홉 하나 건너)에서 `argv` 를 한 번 실행한다.

    `with` 블록 동안 더미 세션은 살아 있다(그 pid 는 `pid_is_alive` 가 True). 블록을 나가면
    stdin 을 닫아 정상 종료시키고 `wait()` 로 수확한다 — zombie 는 signal 0 에 여전히 살아
    있다고 답하므로, 수확까지 해야 죽음이 관측된다.
    """
    link = workdir / harness
    link.symlink_to(sys.executable)
    driver = workdir / "_session_driver.py"
    driver.write_text(_DRIVER_SOURCE, encoding="utf-8")
    hop = workdir / "_session_hop.py"
    hop.write_text(_HOP_SOURCE, encoding="utf-8")

    process = subprocess.Popen(
        [str(link), str(driver), sys.executable, str(hop), *argv],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        watchdog = threading.Timer(SESSION_START_TIMEOUT_SECONDS, process.kill)
        watchdog.start()
        try:
            line = process.stdout.readline()
        finally:
            watchdog.cancel()
        if not line.strip():
            raise RuntimeError(
                f"더미 하네스 세션이 결과를 내지 못했다 (rc={process.poll()!r}, argv={argv!r})"
            )
        payload = json.loads(line)
        yield SessionRun(
            pid=int(payload["pid"]), rc=int(payload["rc"]),
            stdout=payload["stdout"], stderr=payload["stderr"],
        )
    finally:
        with contextlib.suppress(OSError, ValueError):
            process.stdin.close()
        try:
            process.wait(timeout=SESSION_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
