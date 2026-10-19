"""실행 중인 Python 환경의 기본 테스트 명령 (층: infra).
"""
from __future__ import annotations

import functools
import importlib.util
import sys


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
