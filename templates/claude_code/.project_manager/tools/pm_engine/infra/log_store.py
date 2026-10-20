"""PM 운영 log 쓰기 (층: infra).

`log/current.md` writer 를 직렬화하는 잠금 파일 위치 규약과 잠금 안 추가 쓰기를 소유한다.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from collections.abc import Iterator

from pm_engine.infra import fs


def _log_lock_path(log_path: Path) -> Path:
    """current.md가 속한 `.project_manager/.local/log.lock`을 해소한다.

    운영 경로가 아닌 주입형 테스트 경로는 그 파일의 부모 아래 `.local/`로 격리한다.
    """
    log_path = Path(log_path)
    log_dir = log_path.parent
    if (
        log_dir.name == "log"
        and log_dir.parent.name == "wiki"
        and log_dir.parent.parent.name == ".project_manager"
    ):
        local_dir = log_dir.parent.parent / ".local"
    else:
        local_dir = log_dir / ".local"
    return local_dir / "log.lock"


@contextlib.contextmanager
def log_write_lock(log_path: Path) -> Iterator[None]:
    """모든 `log/current.md` writer를 단일 OS 파일락으로 직렬화한다.

    운영 경로의 잠금 파일은 `.project_manager/.local/log.lock` 하나다. append와
    archive 재작성 모두 이 seam을 거쳐 서로의 갱신을 덮어쓰지 않는다. 프로세스가
    종료되면 OS가 락을 회수하므로 stale lock은 남지 않는다. 재진입은 지원하지 않는다.

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 `fs` seam이 소유하고
    경로 규약만 이 도구가 정한다.
    """
    with fs.exclusive_file_lock(_log_lock_path(Path(log_path))):
        yield


def append_log(path: Path, text: str) -> None:
    """공유 log append 공개 seam — flock 안에서 O_APPEND 단일 write.

    O_APPEND 원자 추가 자체는 공용 `fs` seam이 소유한다 — 이 모듈은 락 경로
    규약과 부모 디렉토리 생성만 정한다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with log_write_lock(path):
        append_log_locked(path, text)


def append_log_locked(path: Path, text: str) -> None:
    """이미 ``log_write_lock(path)``를 보유한 호출자의 O_APPEND primitive.

    read→판정→append 전체를 한 임계구역에 묶어야 하는 소비자용이다. 이 함수 자체는 락을
    잡지 않는다. 공개 기본 경로는 계속 :func:`append_log`이며, 소비자는 반드시 같은
    ``log_write_lock`` 문맥 안에서만 이 함수를 호출한다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fs.append_atomic(path, text)
