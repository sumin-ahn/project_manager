#!/usr/bin/env python3
"""배타 파일락 공용 seam — 엔진 도구가 공유하는 단일 구현.

board(`board.lock`·`board-git.lock`)·pm_log(`log.lock`)·pm_relay(raw 장부 `<ledger>.lock`)가
플랫폼 분기(POSIX `fcntl.flock` / Windows `msvcrt.locking` / 둘 다 부재 시 무락 폴백)를 각자
복제하고 있었다. 사본마다 폴백 규칙과 fd 수명이 갈라질 수 있어 한 곳으로 승격한다.

락 파일 *경로 규약*은 여전히 호출자가 소유한다 — 도구별 관례(`.project_manager/.local/` 격리·
자기 파일 옆 `.lock`)가 다르고 그 결정은 이 seam 의 관심사가 아니다. 이 모듈은 "주어진 경로에
배타락을 건다"만 책임진다.

계약 (기존 3 사본의 동작을 그대로 보존):
  - 배타(exclusive)·블로킹 획득. 프로세스가 죽으면 OS 가 락을 회수한다(stale lock 없음).
  - **재진입 금지** — 같은 프로세스가 같은 락을 중첩해 잡으면 flock 재진입 동작이 OS 별로
    달라 정의되지 않는다. 호출부가 중첩 없는 구간으로 설계한다.
  - 락 프리미티브가 둘 다 없는 희귀 환경은 단일-머신 전제의 무락 폴백 — 락 *파일* 자체는
    생성되므로 호출 인터페이스는 동일하다.
  - stdlib 만 사용한다 (외부 `filelock` 의존 금지 — 엔진 런타임 의존은 PyYAML 뿐).

self-contained — 형제 모듈을 로드하지 않는다(leaf). 소비자가 중앙 loader 로 이 모듈을 읽고
baked `ENGINE_REV` 로 사본 skew 를 대조한다.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

# baked 엔진 rev — engine_rev.py --bump 이 STAMPED_MODULES 전체와 함께 기계 재작성한다.
ENGINE_REV = "v1.6.1"

# 락 파일 기본 권한. 더 좁은 권한이 필요한 호출자는 `mode=` 로 명시한다(pm_relay raw 장부=0o600).
DEFAULT_LOCK_MODE = 0o644


def acquire_exclusive(fd: int) -> None:
    """열린 fd 에 OS 배타락을 건다 (블로킹).

    POSIX=`fcntl.flock`·Windows=`msvcrt.locking`·둘 다 임포트 안 되는 희귀 환경은 단일-머신
    전제의 무락 폴백(락 파일 자체는 존재하므로 인터페이스는 동일).
    """
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        # 첫 1바이트에 배타락 — 블로킹(LK_LOCK). 빈 파일이면 한 바이트 확보가 필요.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    except ImportError:
        pass
    # 폴백: 락 프리미티브 없음 — 단일-머신 전제로 무락 진행(락 파일만 존재).


def release_exclusive(fd: int) -> None:
    """OS 배타락을 해제한다 (`acquire_exclusive` 와 같은 플랫폼 분기).

    close 만으로도 OS 가 해제하지만 명시적으로 풀어 둔다.
    """
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    except ImportError:
        pass


@contextlib.contextmanager
def exclusive_file_lock(
    lock_path: Path | str, *, mode: int = DEFAULT_LOCK_MODE,
) -> Iterator[None]:
    """`lock_path` 에 배타 파일락을 건 구간을 연다 (부모 디렉토리는 없으면 생성).

    락 파일은 지우지 않는다 — 존재 자체가 아니라 OS 락이 배타성의 근거이고, 지우면 다른
    프로세스가 잡고 있는 inode 와 새 파일이 갈라져 배타성이 깨진다.
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, mode)
    try:
        acquire_exclusive(fd)
        try:
            yield
        finally:
            release_exclusive(fd)
    finally:
        os.close(fd)  # close 만으로도 OS 가 락을 해제 (크래시 시 안전망)
