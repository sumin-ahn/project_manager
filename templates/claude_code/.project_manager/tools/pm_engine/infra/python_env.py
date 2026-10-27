"""실행 중인 Python 환경 (층: infra).

기본 테스트 명령, 엔진 Python 하한, init 이 쓸 인터프리터 탐지를 소유한다.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import NamedTuple

import functools
import importlib.util
import os
import re
import shutil
import subprocess
import sys

import pm_engine

from pm_engine.domain import conf_schema
from pm_engine.infra import fs, process


@functools.lru_cache(maxsize=None)
def _pytest_xdist_available(py: str) -> bool:
    """엔진 Python 환경에 pytest-xdist가 설치됐는지 import 없이 확인한다.

    ``default_pytest_cmd``의 runtime은 이 엔진이 실행 검증해 고른 interpreter다. 여기서 다시
    subprocess를 열면 regression 테스트가 주입한 Popen/run seam을 침범하고, 스캐폴드 기본값을
    묻는 순수 해소가 프로세스 생성 부작용을 갖는다. module spec 조회만으로 같은 환경을 판정한다.
    ``py``는 cache key이자 호출 계약을 보존한다.
    """
    return importlib.util.find_spec("xdist") is not None


def default_pytest_cmd(py: str | None = None) -> str:
    """Python 프로젝트의 스캐폴드 test_cmd; xdist가 있으면 병렬이 기본이다."""
    # 이미 이 엔진을 실행 중인 interpreter는 재-probe할 이유가 없다. 기본값 해소가
    # subprocess를 열면 regression runner의 Popen DI seam을 선점해 실제 테스트 호출 관측을
    # 오염시킨다. scaffold init은 감지한 runtime을 명시 인자로 넘기므로 그 계약도 보존된다.
    runtime = py or sys.executable or "python3"
    parallel = " -n auto" if _pytest_xdist_available(runtime) else ""
    return f"{runtime} -m pytest tests/ -q{parallel}"


def _minimum_python() -> tuple[int, int]:
    """엔진 Python 하한 (`pm_engine.MIN_PYTHON`)."""
    return tuple(pm_engine.MIN_PYTHON)


def _python_floor_probe_path() -> str:
    """엔진 진입점과 같은 shebang을 가진 2.7-파싱 안전 하한 probe 경로.

    probe 는 이 패키지를 담은 tools 폴더(`pm_engine/infra/` 의 두 단계 위)에 있다.
    """
    # _detect_py 의 Windows 분기는 _probe_os_name() seam 주입으로 테스트한다(전역 os.name 은
    # 더 이상 안 바뀐다). 그래도 pathlib 은 실행 시점 os.name 을 참조해 WindowsPath/PosixPath 를
    # 고르므로, 이 함수는 그 선택 자체에 기대지 않도록 import 시 고정된 os.path 를 계속 쓴다.
    tools_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return os.path.join(tools_dir, "python_floor.py")


def _interp_runs(cmd: str) -> bool:
    """후보 인터프리터가 실행되고 엔진 Python 하한을 충족하는지 검증한다.

    존재하지만 죽은 shim (Windows 의 비기능 `python3` WindowsApps 별칭 등) 을
    `--version` rc 로 먼저 거른 뒤, 엔진과 같은 shebang의 2.7-파싱 안전 probe를
    ``cmd <probe.py>``로 실행한다. 특히 Windows ``py``의 ``-c`` 기본 버전과 실제
    스크립트 shebang 디스패치가 갈리는 창을 닫는다. 단, 부분/수동 복사본에 probe가
    없으면 ``-c`` 인라인 검사로 degrade해 하한 자체는 계속 강제한다
    시나리오). 짧은 timeout·예외 전부 흡수해 탐지가 절대 실패하지 않게 한다(fail-soft).
    """
    try:
        r = subprocess.run(
            [cmd, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if r.returncode != 0:
            return False
        probe = _python_floor_probe_path()
        if os.path.isfile(probe):
            argv = [cmd, probe]
        else:
            major, minor = _minimum_python()
            argv = [
                cmd,
                "-c",
                "import sys; sys.exit(0 if sys.version_info >= "
                f"({major}, {minor}) else 1)",
            ]
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _interp_version_label(cmd: str) -> str | None:
    """실 스크립트 디스패치 버전을 loud 진단용 ``cmd=X.Y`` 형태로 읽는다."""
    try:
        probe = _python_floor_probe_path()
        # probe가 없는 부분 사본은 _interp_runs와 같은 -c 디스패치에서 버전을 읽는다.
        argv = (
            [cmd, probe]
            if os.path.isfile(probe)
            else [
                cmd,
                "-c",
                "import sys; print('Python %d.%d' % sys.version_info[:2])",
            ]
        )
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        output = f"{r.stdout}\n{r.stderr}"
        match = re.search(r"\bPython\s+(\d+)\.(\d+)", output)
        if match:
            return f"{cmd}={match.group(1)}.{match.group(2)}"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@functools.lru_cache(maxsize=1)
def _detect_py() -> str:
    """init 의 local.conf runtime.py= 기본값으로 쓸 bare 인터프리터 명령을 탐지한다.

    Windows(`os.name == "nt"`) 는 `python` 을 1순위로 둔다 — **직접 인터프리터라 스크립트
    shebang 을 무시**한다. 반면 `py` 런처는 `py board.py` 에서 `#!/usr/bin/env python3`
    shebang 을 읽어 *엉뚱한* 버전으로 디스패치하므로(다중 버전 머신에서 deps 없는 Store
    Python 으로 빠질 수 있음) `py -m pytest`(기본 버전)와 `py board.py`(shebang)가 갈린다.
    `python <script>`·`python -m pytest` 는 같은 인터프리터로 일관된다. `py` 는 차선(런처),
    `python3` 은 최후(흔히 비기능 WindowsApps 별칭·sh 훅서 Permission denied). POSIX 는
    현행대로 `python3` 우선.

    후보 순서: Windows = (python, py, python3), POSIX = (python3, python). 각 후보는
    `shutil.which` 존재 **및** `_interp_runs` 실행검증을 모두 통과해야 채택된다 —
    존재하지만 죽은 shim·Python 하한 미달을 건너뛴다. 아무 것도 통과 못 하면 loud 진단을
    stderr 에 한 줄 남기고 `"python3"` 리터럴로 폴백한다(기존 fail-soft 계약 유지).
    **bare 명령**을 반환한다(which 의 절대경로가 아니라) —
    subprocess 가 PATH 해석하고, CLAUDE.md `{{PY}}` 표시에도 가독하다.
    탐지는 후보마다 subprocess를 최대 세 번 실행하므로 프로세스 수명 동안 결과를 캐시한다.
    """
    candidates = (
        ("python", "py", "python3")
        if process._probe_os_name() == "nt"
        else ("python3", "python")
    )
    probe_missing = not os.path.isfile(_python_floor_probe_path())
    found: list[str] = []
    for cand in candidates:
        if not shutil.which(cand):
            continue
        if _interp_runs(cand):
            return cand
        label = _interp_version_label(cand)
        if label:
            found.append(label)
    discovered = ", ".join(found) if found else "없음"
    major, minor = _minimum_python()
    if probe_missing:
        message = (
            f"Python 하한 probe 부재 — 인라인 검사상 {major}.{minor}+ 필요, "
            f"발견: {discovered}"
        )
    else:
        message = f"Python {major}.{minor}+ 필요, 발견: {discovered}"
    print(message, file=sys.stderr)
    return "python3"


class _CommandEnvironment(NamedTuple):
    """OS-safe command rendering policy for the bootstrap dump and card.

    This deliberately models only the Python launcher as argv.  `platform` and
    `os.name` cannot identify the active shell, so `chain_policy` is a safe
    rendering policy, not a claim that bash/PowerShell was detected.
    """

    os_label: str
    python_argv: tuple[str, ...]
    python_source: str
    chain_policy: str


def _command_os_label(*, system: str | None = None, os_name: str | None = None) -> str:
    """Resolve Windows/Linux/macOS/other from the platform and os-name probes (probe errors propagate)."""
    if system is None:
        system = platform.system()
    if os_name is None:
        os_name = process._probe_os_name()
    normalized = str(system).strip().lower()
    if str(os_name).strip().lower() == "nt" or normalized == "windows":
        return "windows"
    if normalized == "linux":
        return "linux"
    if normalized == "darwin":
        return "macos"
    return "other"


def _resolve_python_argv(home, os_label: str = "linux") -> tuple[tuple[str, ...], str]:
    """Resolve the launcher argv, preserving the existing final-`runtime.py=` contract.

    A non-empty final `runtime.py=` wins verbatim as one configured executable token.
    Missing/unreadable config, a missing key, or an empty final value uses the
    OS default.  In particular, an earlier non-empty value followed by `runtime.py=` is
    intentionally treated as empty and falls back, matching the previous card
    resolver.
    """
    conf_path = Path(home.local_conf)
    try:
        conf_text = fs.read_text_shared(conf_path, encoding="utf-8")
    except (OSError, UnicodeError):
        values = {}
    else:
        values = conf_schema.parse(conf_text)
    conf_schema.assert_no_legacy(
        conf_schema.ConfResult(values, conf_schema.legacy_keys(values), conf_path)
    )
    interpreter = (values.get("runtime.py") or "").strip()
    if interpreter:
        return ((interpreter,), "local-conf")
    if os_label == "windows":
        return (("py", "-3"), "os-default")
    return (("python3",), "os-default")


def _detect_command_environment(
    home, *, system: str | None = None, os_name: str | None = None
) -> _CommandEnvironment:
    """Return one finite OS-safe policy; never inspect or infer the live shell."""
    os_label = _command_os_label(system=system, os_name=os_name)
    python_argv, python_source = _resolve_python_argv(home, os_label)
    chain_policy = "and-if" if os_label in {"linux", "macos"} else "separate-lines"
    return _CommandEnvironment(os_label, python_argv, python_source, chain_policy)


def _default_python(home) -> str:
    """플랫폼-인지 venv 인터프리터 경로 (없으면 sys.executable 폴백).

    Windows 는 venv/Scripts/python.exe, POSIX 는 venv/bin/python. venv 가 없으면
    현재 인터프리터로 폴백한다. 이 머신은 시스템 python3 에 pytest 가 없고 venv 에만
    있으므로, venv 가 있으면 무조건 venv 를 우선해 회귀 측정 인터프리터를 보존한다.
    """
    cand = (
        home.root
        / "venv"
        / ("Scripts/python.exe" if process._probe_os_name() == "nt" else "bin/python")
    )
    return str(cand) if cand.exists() else sys.executable
