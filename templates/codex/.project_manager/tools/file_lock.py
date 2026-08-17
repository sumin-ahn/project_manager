#!/usr/bin/env python3
"""동시-쓰기 안전 파일 프리미티브 공용 seam — 엔진 도구가 공유하는 단일 구현.

엔진의 공유 파일 동시성은 두 프리미티브로 이뤄진다 — read-modify-write 파일은 **배타
파일락**으로 직렬화하고, append-only 파일은 **O_APPEND 원자 추가**로 lost update 를 피한다
(board.py 의 "보드 동시성" 주석이 둘을 한 쌍으로 기술한다). 도구마다 복제돼 있던 두 구현을
여기로 승격했다 — 배타락은 board(`board.lock`·`board-git.lock`)·pm_log(`log.lock`)·
pm_relay(raw 장부 `<ledger>.lock`)·pm_handoff(`dashboard.lock`)·worktree_pool(리스 장부
`worktree-leases.lock`)가, O_APPEND 추가는 board(areas 등록)·pm_log(log append)가 각자
복제하고 있었다. 사본마다 폴백 규칙·fd 수명·권한이 갈라질 수 있다.

파일 *경로 규약*과 *권한*은 여전히 호출자가 소유한다 — 도구별 관례
(`.project_manager/.local/` 격리·자기 파일 옆 `.lock`·raw 장부 0o600)가 다르고 그 결정은 이
seam 의 관심사가 아니다. 이 모듈은 "주어진 경로에 배타락을 건다"·"주어진 경로에 한 번에
붙인다"만 책임진다.

그 규칙의 **예외가 local.conf 락 하나**다(`conf_lock_path`·`local_conf_write_lock`). 이 락은 한
도구의 내부 관례가 아니라 **서로 다른 프로세스**(board init·pm_update 온보딩·pm_import/pm_config
의 키 writer)가 같은 파일을 두고 지켜야 하는 *도구 간* 규약이라, 유도 규칙이 모듈마다 복제되면
한 사본만 어긋나도 배타가 조용히 사라진다(같은 conf, 다른 락 파일 = 직렬화 없음). 그래서 경로
유도까지 이 한 곳이 소유한다.

(모듈 *파일명* `file_lock.py` 는 배타락 seam 으로 출발한 유래다 — 개명은 manifest 4벌·
`STAMPED_MODULES`·소비자 로더·테스트·채택자 사본 orphan 비용이 커서 하지 않고, 소유 범위를
이 docstring 으로 명시한다.)

계약 (기존 사본들의 동작을 그대로 보존):
  - 배타(exclusive)·블로킹 획득. 프로세스가 죽으면 OS 가 락을 회수한다(stale lock 없음).
  - **재진입 금지** — 같은 프로세스가 같은 락을 중첩해 잡으면 flock 재진입 동작이 OS 별로
    달라 정의되지 않는다. 호출부가 중첩 없는 구간으로 설계한다.
  - 락 프리미티브가 둘 다 없는 희귀 환경은 단일-머신 전제의 무락 폴백 — 락 *파일* 자체는
    생성되므로 호출 인터페이스는 동일하다. 그 폴백 진입은 **경고로 표면화한다**(조용한 degrade
    금지·`LocklessFallbackWarning`) — 배타 없이 도는 것과 배타가 있는 것은 데이터 안전이 다르다.
  - **무락 진행은 프리미티브 *부재*(import 실패)에만 허용된다.** 프리미티브가 있는데 획득이
    실패하면(예: Windows `LockFileEx` 실패 `OSError`) 삼키지 않고 그대로 올린다 —
    배타성 없는 임계 구역을 성공으로 위장하면 직렬화가 조용히 사라진다. 호출부가 그 예외를
    조치 문구로 번역한다(external_review 라운드 장부 = 전송 전 중단·마감은 판정 rc 보존).
  - append 는 단일 `os.write` — 부모 디렉토리 생성·상위 직렬화(락)는 호출자 몫이다.
  - append 의 **내구성(`os.fsync`)도 이 seam 이 소유한다** — 자기가 연 쓰기 가능 fd 위에서 sync
    한 뒤 닫는다. 호출부가 append 뒤에 파일을 다시 열어 sync 하지 않는다(읽기 전용 fd 의 fsync
    는 Windows 가 `[Errno 9] Bad file descriptor` 로 거부한다).
  - append 는 **바이트 그대로** 쓴다 — Windows 의 `os.open` 은 텍스트 모드가 기본이라
    `os.O_BINARY` 를 얹지 않으면 CRT 가 LF 를 CRLF 로 번역한다(T-0711 실측 클래스).
  - **삭제도 이 seam 이 소유한다**(`force_rmtree`·`force_unlink`) — read-only 속성을 풀고
    재시도하되, 끝내 남으면 예외를 올린다. 조용한 잔재를 만들지 않는다.
  - **소유자 전용 접근 제한도 이 seam 이 소유한다**(`restrict_to_owner`·`owner_only_access`) —
    POSIX 는 `chmod 0600/0700`, ACL 플랫폼(Windows)은 `icacls` 로 *같은 보장*을 낸다. 실패는
    조용히 넘어가지 않는다(`AccessRestrictionError`).
  - stdlib 만 사용한다 (외부 `filelock` 의존 금지 — 엔진 런타임 의존은 PyYAML 뿐).

self-contained — 형제 모듈을 로드하지 않는다(leaf). 소비자가 중앙 loader 로 이 모듈을 읽고
baked `ENGINE_REV` 로 사본 skew 를 대조한다.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import sys
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

# baked 엔진 rev — engine_rev.py --bump 이 STAMPED_MODULES 전체와 함께 기계 재작성한다.
ENGINE_REV = "v1.7.5"

# 락 파일 기본 권한. 더 좁은 권한이 필요한 호출자는 `mode=` 로 명시한다(pm_relay raw 장부=0o600).
DEFAULT_LOCK_MODE = 0o644

# append 로 *새로 생기는* 파일의 기본 권한 (board areas·log/current.md 관례).
DEFAULT_APPEND_MODE = 0o644


# ── 배타 파일락 플랫폼 분기 ──────────────────────────────────────────────────
# POSIX 는 `fcntl.flock`, Windows 는 Win32 **`LockFileEx`**(ctypes)다. Windows 쪽에서 CRT 래퍼
# `msvcrt.locking(fd, LK_LOCK, 1)` 을 쓰지 않는 이유는 그 함수의 "블로킹"(LK_LOCK)이 커널 대기가
# 아니라 **유한 재시도**여서, 보유자가 그 안에 놓지 않으면 획득이 실패로 끝나기 때문이다.
# 이 seam 의 계약상 획득 실패는 삼키지 않고 올라가므로(배타 없는 진행 금지), 그 실패는 곧
# 호출부 기능 중단이다 — 임계 구역이 그보다 길 수 있는 형상(멀티-PM 리스 장부 write·리뷰 라운드
# 장부)에서 `flock(LOCK_EX)` 와 같은 무기한 블로킹 획득이 필요하다. `LockFileEx` 는
# `LOCKFILE_FAIL_IMMEDIATELY` 를 빼면 커널이 대기시켜 그 보장을 그대로 낸다.
#
# 두 수단은 **같은 Win32 바이트-영역 락**으로 내려간다(CRT `_locking` 은 `LockFile` 래퍼) —
# 그래서 옛 엔진 사본(msvcrt)과 새 사본(LockFileEx)이 섞인 채택자 형상에서도 같은 영역
# (`[0, LOCK_REGION_BYTES)`)을 두고 서로 배제한다. 영역을 바꾸면 그 상호 배제가 조용히 깨진다.

# Win32 파일 영역 락 플래그(winbase.h).
LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

# 락을 거는 영역 — 파일 선두 1바이트. 락 파일은 내용이 없으므로(0바이트) EOF 너머 영역을
# 잠그는 셈인데, Win32 바이트-영역 락은 그것을 허용한다(파일을 늘릴 필요가 없다).
LOCK_REGION_BYTES = 1

# backend 이름 — `lock_backend()` 의 반환값이자 분기 키.
POSIX_LOCK_BACKEND = "posix"        # fcntl.flock
WINDOWS_LOCK_BACKEND = "windows"    # Win32 LockFileEx
NO_LOCK_BACKEND = "none"            # 프리미티브 부재 — 단일-머신 전제 무락 폴백


class WindowsLockApi(NamedTuple):
    """Win32 파일 영역 락의 최소 표면 — 정책과 원시 호출을 가르는 **주입 지점**.

    분기 정책(어느 영역에·어떤 플래그로 걸고 실패를 어떻게 올리는가)은 순수 파이썬이라
    POSIX 개발기에서도 그대로 태울 수 있어야 한다([[guard-must-cover-its-own-surface]]) —
    ctypes/kernel32 에 직접 매달리면 그 분기는 Windows 밖에서 한 줄도 실행되지 않는다.
    그래서 원시 호출만 이 세 콜러블 뒤로 밀어내고, 테스트는 같은 모양의 대역을 끼운다.

    `lock_region`·`unlock_region` 은 **Win32 에러코드**를 돌려준다(0=성공) — 예외가 아니라
    코드로 받는 이유는 대역이 실패면을 값으로 재현할 수 있어야 하기 때문이다.
    """

    osf_handle: Callable[[int], int]            # fd → OS 파일 핸들
    lock_region: Callable[[int, int, int], int]  # (handle, flags, length) → Win32 error
    unlock_region: Callable[[int, int], int]     # (handle, length) → Win32 error


def lock_backend() -> str:
    """이 프로세스가 쓸 배타락 backend 이름 — 부수효과 없이 판정한다(**주입 지점**).

    락 파일 open/acquire 는 하지 않는다. Windows 판정은 `msvcrt.get_osfhandle`(fd→핸들 변환)
    존재로 한다 — 실제로 필요한 능력이 그것이고, 락 자체는 `LockFileEx` 가 건다.
    """
    try:
        import fcntl
        if callable(getattr(fcntl, "flock", None)):
            return POSIX_LOCK_BACKEND
    except ImportError:
        pass
    try:
        import msvcrt
        if callable(getattr(msvcrt, "get_osfhandle", None)):
            return WINDOWS_LOCK_BACKEND
    except ImportError:
        pass
    return NO_LOCK_BACKEND


def exclusive_lock_supported() -> bool:
    """현재 플랫폼에 실제 OS 배타락 primitive가 있는지 부수효과 없이 판정한다.

    락 파일 open/acquire는 하지 않는다. 기존 ``acquire_exclusive``의 희귀 플랫폼 무락 폴백은
    그대로 두고, 배타성 없이는 진행하면 안 되는 보안 경계가 사전에 fail-closed할 때 쓴다.
    """
    return lock_backend() != NO_LOCK_BACKEND


# 프로세스 상수라 한 번만 만든다(락을 잡을 때마다 ctypes 시그니처를 다시 세우지 않는다).
_CACHED_WINDOWS_LOCK_API: WindowsLockApi | None = None


def _build_windows_lock_api() -> WindowsLockApi:
    """`LockFileEx`/`UnlockFileEx` ctypes 바인딩을 세운다 (Windows 전용).

    `OVERLAPPED` 는 오프셋 전달용이다 — 전부 0 으로 두면 파일 선두부터 잠근다. 비동기 대기가
    아니라 **동기 핸들의 블로킹 획득**이므로 `hEvent` 는 쓰지 않는다(`os.open` 이 만드는 핸들은
    동기 핸들이라 `LockFileEx` 가 커널에서 대기한다).
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL

    def lock_region(handle: int, flags: int, length: int) -> int:
        overlapped = _Overlapped()
        if kernel32.LockFileEx(handle, flags, 0, length, 0, ctypes.byref(overlapped)):
            return 0
        return ctypes.get_last_error()

    def unlock_region(handle: int, length: int) -> int:
        overlapped = _Overlapped()
        if kernel32.UnlockFileEx(handle, 0, length, 0, ctypes.byref(overlapped)):
            return 0
        return ctypes.get_last_error()

    return WindowsLockApi(msvcrt.get_osfhandle, lock_region, unlock_region)


def _windows_lock_api() -> WindowsLockApi:
    """Windows 락 API 묶음 (프로세스당 1회 생성·테스트가 갈아끼우는 **주입 지점**)."""
    global _CACHED_WINDOWS_LOCK_API
    if _CACHED_WINDOWS_LOCK_API is None:
        _CACHED_WINDOWS_LOCK_API = _build_windows_lock_api()
    return _CACHED_WINDOWS_LOCK_API


class LocklessFallbackWarning(RuntimeWarning):
    """OS 배타락 없이 임계 구역을 진행했다는 loud 신호 (`repo_owned_files` 강등 경고 동형)."""


LOCKLESS_FALLBACK_MESSAGE = (
    "OS 배타 파일락 프리미티브(POSIX fcntl.flock · Windows LockFileEx)가 없어 무락으로 "
    "진행함; 여러 프로세스가 같은 파일을 쓰면 서로의 갱신을 덮어쓴다(lost update). "
    "단일-머신·단일-프로세스 전제에서만 안전하다"
)


def _warn_lockless_fallback() -> None:
    """무락 폴백 진입을 표면화한다 — 조용한 degrade 를 남기지 않는다.

    엔진의 다른 강등 신호와 같은 수단(`warnings.warn` + 전용 `RuntimeWarning`)을 쓴다.
    귀속 프레임은 **이 seam 자신**(`stacklevel=2`)이다 — 알리는 사실이 "이 플랫폼에 락
    프리미티브가 없다"는 프로세스 단위 조건이지 특정 호출자의 문제가 아니고, 귀속이 한 곳에
    고정되면 기본 필터가 프로세스당 한 번으로 접어 장부 op 마다 같은 줄이 쌓이지 않는다.
    """
    warnings.warn(LOCKLESS_FALLBACK_MESSAGE, LocklessFallbackWarning, stacklevel=2)


def acquire_exclusive(fd: int) -> None:
    """열린 fd 에 OS 배타락을 건다 (블로킹).

    POSIX=`fcntl.flock(LOCK_EX)`·Windows=`LockFileEx(LOCKFILE_EXCLUSIVE_LOCK)`. 둘 다 없는
    희귀 환경만 단일-머신 전제의 무락 폴백이고, 그 진입은 **경고로 표면화한다**
    (`LocklessFallbackWarning`·락 파일 자체는 존재하므로 인터페이스는 동일). 프리미티브가
    *있는데* 획득이 실패하면 무락 진행하지 않고 예외를 그대로 올린다.
    """
    backend = lock_backend()
    if backend == POSIX_LOCK_BACKEND:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    if backend == WINDOWS_LOCK_BACKEND:
        api = _windows_lock_api()
        # FAIL_IMMEDIATELY 없음 = 커널 대기(무기한 블로킹) — flock(LOCK_EX) 등가.
        error = api.lock_region(
            api.osf_handle(fd), LOCKFILE_EXCLUSIVE_LOCK, LOCK_REGION_BYTES)
        if error:
            raise OSError(
                f"Windows 배타 파일락 획득 실패 — LockFileEx WinError {error}")
        return
    _warn_lockless_fallback()


def release_exclusive(fd: int) -> None:
    """OS 배타락을 해제한다 (`acquire_exclusive` 와 같은 플랫폼 분기).

    close 만으로도 OS 가 해제하지만 명시적으로 풀어 둔다. 해제 실패도 삼키지 않는다 — 잡은
    영역이 남아 있는데 성공으로 넘어가면 같은 프로세스의 다음 획득이 이유 없이 막힌다.
    """
    backend = lock_backend()
    if backend == POSIX_LOCK_BACKEND:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if backend == WINDOWS_LOCK_BACKEND:
        api = _windows_lock_api()
        error = api.unlock_region(api.osf_handle(fd), LOCK_REGION_BYTES)
        if error:
            raise OSError(
                f"Windows 배타 파일락 해제 실패 — UnlockFileEx WinError {error}")
        return
    # 무락 폴백 — 풀 락이 없다(진입은 acquire 가 이미 알렸다).


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


# local.conf writer 직렬화 락의 파일명. 경로는 `conf_lock_path` 가 대상 conf 에서 유도한다.
LOCAL_CONF_LOCK_NAME = "local-conf.lock"


def conf_lock_path(conf_path: Path | str) -> Path:
    """`local.conf` writer 를 직렬화하는 락 경로 — **대상 conf 에서** 유도한다.

    상수(도구 자기 repo 의 `LOCAL_CONF`)가 아니라 인자로 받은 conf 에서 유도하는 이유는
    pm_update/pm_import 가 **남의 트리**(dest_root)의 conf 를 쓰기 때문이다. 같은 conf 를 건드리는
    모든 writer 가 같은 파일에 도달해야 배타가 성립한다. board 의 락 관례
    (`.project_manager/.local/*.lock`)를 그대로 따른다.
    """
    return Path(conf_path).parent / ".local" / LOCAL_CONF_LOCK_NAME


@contextlib.contextmanager
def local_conf_write_lock(
    conf_path: Path | str, *, mode: int = DEFAULT_LOCK_MODE,
) -> Iterator[None]:
    """`conf_path` 의 **모든** writer(전체 write·RMW 교체·opt-in append)가 공유하는 배타 구간.

    append 만 서로 직렬화해서는 부족하다 — 커밋 전 내용을 읽고 나중에 통째 교체하는 writer
    (`board init` 병합·`pm_import._write_conf_keys` 의 temp+`os.replace`)가 그 사이의 append 를
    읽지 못하면, 원자적으로 쓴 append 도 교체에 덮여 사라진다(lost update). 그래서 락의 단위는
    "append" 가 아니라 "이 conf 를 쓰는 구간" 이고, 읽기→판정→쓰기→(검증)까지 한 구간 안에 든다.

    **재진입 금지**(`exclusive_file_lock` 계약 그대로) — 이 구간 안에서 다른 conf writer 를 부르지
    않는다. 온보딩 프롬프트는 락 밖에서 묻고 커밋만 이 구간에서 한다.
    """
    with exclusive_file_lock(conf_lock_path(conf_path), mode=mode):
        yield


def append_atomic(
    path: Path | str, text: str, *, mode: int = DEFAULT_APPEND_MODE,
    fsync: bool = True,
) -> None:
    """`path` 끝에 UTF-8 텍스트를 O_APPEND 단일 write 로 원자 추가하고 sync 한다 (없으면 생성).

    `O_APPEND` 는 각 write 의 offset 이동+기록을 OS 가 원자로 처리해, 동시 writer 가 서로의
    추가를 덮어쓰지 않는다(read-modify-write 의 lost update 회피). 부모 디렉토리 생성과 상위
    직렬화(락·헤더 초기화)는 호출자 몫이다 — 이 함수는 "주어진 파일에 한 번에 붙인다"만
    책임진다. 인코딩은 엔진 관례대로 UTF-8.

    내구성 sync 는 **이 함수가 연 쓰기 가능 fd** 위에서 수행한다 — 호출부가 append 뒤에 파일을
    다시 열어 sync 하면 그 fd 는 읽기 전용이 되기 쉽고, Windows 의 `_commit()` 은 쓰기 가능
    핸들을 요구해 그 호출을 `[Errno 9] Bad file descriptor` 로 거부한다(POSIX 는 허용).
    쓰기와 내구성을 같은 자리에 둬 호출부마다 흩어진 sync 를 없앤다. `fsync=False` 는 sync 를
    감당할 수 없는 호출부만 쓰고 그 자리에 사유를 남긴다 — 실패는 삼키지 않고 그대로 올린다.

    **`os.O_BINARY`(Windows) 를 함께 연다** — 그 플랫폼의 `os.open` 은 텍스트 모드가 기본이라
    이 플래그가 없으면 CRT 가 `\\n` 을 `\\r\\n` 으로 번역해, 호출부가 계산한 append 바이트와
    디스크 바이트가 갈린다(board 루트 파일 backfill 의 롤백 대조가 어긋나 잔재로 남던 실측
    클래스). 다른 엔진 IO 채널이 쓰는 것과 같은 관용구다(`pm_import` 의 dest 쓰기).
    """
    binary = getattr(os, "O_BINARY", 0)  # Windows 텍스트 모드 줄끝 번역 차단(바이트 보존).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | binary, mode)
    try:
        os.write(fd, text.encode("utf-8"))
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


# ── 강제 삭제 (플랫폼 공용 seam) ─────────────────────────────────────────────
# 정리 경로가 `shutil.rmtree(..., ignore_errors=True)` 로 실패를 삼키면, 지우지 못한 트리가
# 채택자 디스크에 그대로 남는데도 rc=0 이 된다. 리뷰어 격리 컨테이너(검토 대상 저장소 사본 +
# 홈 인증 사본)와 board 롤백의 backfill 파일이 그 자리라 침묵은 그대로 보안·위생 결함이다.
# 실패 원인은 두 축이다:
#   - **read-only 속성** — git 은 object·packfile 을 read-only 로 만든다. Windows 의 `unlink` 는
#     그 속성 파일을 거부하고(POSIX 는 부모 디렉토리 쓰기 권한만 본다), 쓰기 권한 없는 디렉토리
#     안의 항목은 어느 플랫폼에서도 못 지운다.
#   - **열린 핸들** — Windows 는 열려 있는 파일의 삭제를 거부한다. 이건 속성 해제로 풀리지 않아
#     짧은 대기 후 재시도만이 수단이고, 무한 대기는 하지 않는다(상한 후 예외).
# 우리 코드가 파일을 닫지 않아 생긴 잠금이면 재시도로 덮지 않고 그 지점을 고친다 — 이 seam 은
# 남의 스캐너·인덱서가 잡은 순간적 핸들만 흡수한다.

# 트리/파일 하나를 지우기 위해 시도하는 총 횟수(첫 시도 포함).
FORCE_DELETE_RETRIES = 3
# 재시도 사이 대기 — 순간적 핸들이 닫힐 시간만 준다(무한 대기 금지).
FORCE_DELETE_RETRY_SLEEP_SECONDS = 0.05


def _clear_delete_block(path: Path) -> None:
    """삭제를 막는 read-only 속성을 **대상과 그 부모**에서 푼다 (best-effort).

    두 플랫폼의 규칙이 달라 둘 다 푼다 — Windows 는 *그 파일 자신*의 read-only 속성이 unlink 를
    거부하고, POSIX 는 파일 권한이 아니라 *부모 디렉토리*의 쓰기 권한을 본다. 한쪽만 풀면 다른
    플랫폼에서 같은 실패가 그대로 남는다.

    symlink 는 건너뛴다 — `os.chmod` 가 링크를 따라가 **대상**의 권한을 바꾸는데, 링크 삭제에는
    대상 권한이 필요 없다(정리하다 남의 파일 권한을 바꾸지 않는다).
    """
    for target in (path.parent, path):
        try:
            info = os.lstat(target)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        mode = stat.S_IMODE(info.st_mode) | stat.S_IRUSR | stat.S_IWUSR
        if stat.S_ISDIR(info.st_mode):
            mode |= stat.S_IXUSR  # 디렉토리는 탐색 권한이 있어야 안의 항목에 닿는다.
        with contextlib.suppress(OSError):
            os.chmod(target, mode)


def _rmtree_clearing_readonly(target: Path) -> None:
    """`shutil.rmtree` 한 번 — 항목별 실패마다 read-only 를 풀고 그 항목만 즉시 재시도한다.

    fd 기반 순회(`shutil.rmtree.avoids_symlink_attacks`)를 잃지 않으려고 직접 순회를 다시 짜지
    않고 stdlib 의 에러 훅을 쓴다. 훅 이름이 3.12 에서 `onerror`(func, path, excinfo) → `onexc`
    (func, path, exc) 로 갈렸고 옛 이름은 DeprecationWarning 이라 버전으로 갈라 부른다
    (엔진 지원 하한은 3.11).
    """
    def _retry_after_unlock(func, failed_path, _exc) -> None:
        _clear_delete_block(Path(failed_path))
        func(failed_path)   # 이 재시도의 실패는 그대로 올라간다 — 바깥 루프가 받는다.

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_retry_after_unlock)
    else:
        shutil.rmtree(
            target,
            onerror=lambda func, p, excinfo: _retry_after_unlock(func, p, excinfo[1]),
        )


def force_rmtree(path: Path | str, *, retries: int = FORCE_DELETE_RETRIES) -> None:
    """디렉토리 트리를 **실제로** 지운다 — 못 지우면 조용히 넘어가지 않고 예외를 올린다.

    read-only 항목은 속성을 풀어 그 자리에서 재시도하고, 그래도 실패하면 짧게 기다렸다 트리
    전체를 다시 시도한다(열린 핸들이 닫힐 시간). 시도를 다 쓰고도 남아 있으면 `OSError` —
    호출부는 그 실패를 사용자가 볼 수 있는 조치 문구로 번역하거나 그대로 올린다.

    **부재는 성공**이다(정리의 목적은 "없다"이고 경쟁 삭제도 그 목적을 이룬다).
    """
    target = Path(path)
    attempts = max(1, retries)
    last_error: OSError | None = None
    for attempt in range(attempts):
        if not os.path.lexists(target):
            return
        try:
            _rmtree_clearing_readonly(target)
            return
        except FileNotFoundError:
            return                      # 경쟁 삭제 — 목적(부재)은 달성됐다.
        except OSError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(FORCE_DELETE_RETRY_SLEEP_SECONDS)
    raise OSError(
        f"디렉토리 정리 실패 — {attempts}회 시도 후에도 남아 있습니다: {target} "
        f"({type(last_error).__name__}: {last_error})"
    ) from last_error


def force_unlink(
    path: Path | str, *, missing_ok: bool = False,
    retries: int = FORCE_DELETE_RETRIES,
) -> None:
    """파일 하나를 **실제로** 지운다 (`force_rmtree` 와 같은 규칙의 파일판).

    read-only 속성(대상·부모)을 풀고 재시도하며, 시도를 다 쓰고도 남으면 `OSError` 를 올린다.
    `missing_ok=True` 면 부재를 성공으로 본다(부재를 성공으로 볼지는 호출부의 뜻이라 기본값은
    `Path.unlink` 와 같은 False 다 — 없는 파일을 지우려 한 것 자체가 신호일 수 있다).
    """
    target = Path(path)
    attempts = max(1, retries)
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.unlink(target)
            return
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        except OSError as exc:
            last_error = exc
            _clear_delete_block(target)
            if attempt + 1 < attempts:
                time.sleep(FORCE_DELETE_RETRY_SLEEP_SECONDS)
    raise OSError(
        f"파일 정리 실패 — {attempts}회 시도 후에도 남아 있습니다: {target} "
        f"({type(last_error).__name__}: {last_error})"
    ) from last_error


# ── 소유자 전용 접근 제한 (플랫폼 공용 seam) ─────────────────────────────────
# 격리 산출물(리뷰어 임시 홈의 인증 사본·리뷰 샌드박스의 프롬프트 전달 파일·raw 출력)은
# **다른 사용자에게 읽히면 안 된다**. POSIX 는 `chmod 0600` 이 그 보장을 내지만 Windows 의
# `chmod` 는 read-only 속성 하나만 만지므로(실측 `S_IMODE`=0o666) 같은 호출이 아무 제한도
# 걸지 않는다. 플랫폼마다 *수단*이 다를 뿐 걸어야 하는 *보장*은 같으므로, 수단 분기를 이
# seam 이 소유하고 호출부는 "소유자 전용으로 제한한다" 하나만 말한다.
#
# Windows 수단은 `icacls` 다 — 상속 ACE 를 끊고(`/inheritance:r`) 현재 계정에만 Full 을
# 준다(`/grant:r`). 판정(`owner_only_access`)은 저장된 플래그가 아니라 **OS 에 되물어**
# 확인한다(POSIX=`stat`·Windows=`icacls` 조회) — 적용과 판정이 같은 기억을 공유하면 둘 다
# no-op 이어도 서로 맞다고 답한다.
#
# 주체는 **SID 로 지정한다**(`*S-1-5-21-…`). 계정 *이름*(`DOMAIN\user`)은 로케일·도메인 조인
# 형태·Microsoft 계정 표기에 따라 icacls 가 계정으로 해소하지 못한다 — 한국어 Windows 11
# 실측에서 `rc=1332`(`ERROR_NONE_MAPPED`, "계정 이름과 보안 ID 간에 매핑이 수행되지 않았습니다")
# 로 전량 실패했다. SID 는 그 표기에 무관한 유일한 식별자이고, `icacls` 는 `*` 접두로 받는다.

OWNER_ONLY_FILE_MODE = 0o600
OWNER_ONLY_DIR_MODE = 0o700

# icacls 호출 상한 — 응답 없는 도구가 정리/리뷰 경로를 무기한 잡지 않게 한다.
_ACL_COMMAND_TIMEOUT_SECONDS = 20

# 현재 계정 SID 조회. `/fo csv /nh` 는 `"DOMAIN\user","S-1-5-21-…"` 한 줄만 낸다(머리글 없음).
_OWNER_SID_COMMAND: tuple[str, ...] = ("whoami", "/user", "/fo", "csv", "/nh")
_SID_TOKEN_RE = re.compile(r"S-1-\d+(?:-\d+)+")

# 프로세스 상수라 한 번만 조회한다(파일마다 whoami 를 띄우지 않는다). 빈 문자열 = 조회 실패 확정.
# **기본 runner 로 해소한 값만** 캐시한다 — 주입 runner 는 테스트 소유라 캐시를 오염시키지 않는다.
_CACHED_OWNER_SID: str | None = None

# 디렉토리는 상속 ACE 로 준다 — 그 뒤 만들어지는 항목도 기본이 소유자 전용이 되게 한다.
_ACL_FILE_RIGHTS = "(F)"
_ACL_DIR_RIGHTS = "(OI)(CI)(F)"


class AccessRestrictionError(OSError):
    """소유자 전용 접근 제한을 걸지 못했다 — 조용히 넘어가지 않기 위한 loud 신호."""


def windows_acl_platform() -> bool:
    """이 플랫폼의 접근 제한 수단이 ACL 인가 (= POSIX 퍼미션이 무효인가).

    별도 함수로 둔 이유는 **주입 지점**이다 — POSIX 개발기에서 Windows 분기를 실제로 태워
    보려면 판정을 한 곳에서 바꿔 끼울 수 있어야 한다([[guard-must-cover-its-own-surface]]).
    """
    return os.name == "nt"


def _run_acl_command(argv: list[str]) -> tuple[int, str]:
    """ACL 도구를 실행해 `(rc, 출력)` 을 돌려준다 (테스트가 갈아끼우는 주입 지점).

    출력은 **bytes 로 받아** 로케일 인코딩으로 푼다 — 콘솔 도구의 메시지는 UTF-8 이 아니라
    시스템 코드페이지다(한국어 Windows=cp949). 우리가 파싱하는 토큰(SID·ACE 주체)은 ASCII 라
    해독 실패가 판정을 바꾸지는 않지만, 진단 문구가 깨지면 실패 원인을 읽을 수 없다.
    """
    import locale
    import subprocess

    completed = subprocess.run(
        argv, capture_output=True, timeout=_ACL_COMMAND_TIMEOUT_SECONDS, check=False,
    )
    payload = (completed.stdout or b"") + (completed.stderr or b"")
    encoding = locale.getpreferredencoding(False) or "utf-8"
    try:
        text = payload.decode(encoding, errors="replace")
    except LookupError:
        text = payload.decode("utf-8", errors="replace")
    return completed.returncode, text


def current_owner_principal() -> str:
    """현재 계정의 ACL 주체 **이름** (`DOMAIN\\user` · 도메인 없으면 사용자명).

    이름은 SID 조회가 실패했을 때의 폴백이자, ACL 되읽기에서 이름으로 해소돼 출력된 ACE 를
    현재 계정으로 인정하기 위한 대조값이다(적용 주체는 SID 를 우선한다).
    """
    import getpass

    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        with contextlib.suppress(Exception):
            user = getpass.getuser()
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{user}" if domain and user else user


def _parse_whoami_sid(output: str) -> str | None:
    """`whoami /user` 출력에서 SID 토큰을 뽑는다 (형식 밖이면 None).

    `/fo csv /nh` 는 `"DOMAIN\\user","S-1-5-21-…"` 한 줄이지만, 표 형식으로 떨어져도 SID 토큰
    자체는 같은 모양이라 **토큰을 찾는다**(포맷 옵션 미지원 세대까지 같은 규칙으로 덮는다).
    이름이 먼저·SID 가 나중이므로 마지막 토큰을 쓴다.
    """
    matches = _SID_TOKEN_RE.findall(output)
    return matches[-1] if matches else None


def current_owner_sid(*, runner=None) -> str | None:
    """현재 계정의 SID (`S-1-5-21-…`) — 조회 실패면 None.

    ACL 주체를 이름으로 주면 icacls 가 계정 해소에 실패하는 형상이 실재한다(한국어 Windows 11
    실측 `rc=1332` = `ERROR_NONE_MAPPED`). SID 는 로케일·도메인 조인 형태·계정명 표기에 무관해
    그 실패면을 아예 없앤다.
    """
    global _CACHED_OWNER_SID
    if runner is None and _CACHED_OWNER_SID is not None:
        return _CACHED_OWNER_SID or None
    run = _run_acl_command if runner is None else runner
    sid: str | None = None
    try:
        rc, output = run(list(_OWNER_SID_COMMAND))
    except OSError:
        rc, output = 1, ""      # whoami 부재 — 이름 폴백이 받는다.
    if rc == 0:
        sid = _parse_whoami_sid(output)
    if runner is None:
        _CACHED_OWNER_SID = sid or ""
    return sid


def owner_grant_principal(*, runner=None) -> str:
    """`icacls /grant` 에 넘길 주체 표기 — SID 우선(`*S-1-…`), 실패 시 계정 이름 폴백."""
    sid = current_owner_sid(runner=runner)
    return f"*{sid}" if sid else current_owner_principal()


def _acceptable_owner_identities(*, runner=None) -> tuple[str, ...]:
    """ACE 주체가 "현재 계정" 으로 인정되는 표기들 — SID 와 계정 이름 둘 다.

    icacls 조회 출력은 SID 가 이름으로 해소되면 이름을, 안 되면 SID 를 그대로 찍는다. 어느
    쪽이 나올지는 그 기기의 계정 매핑 상태에 달렸으므로 둘 다 인정한다(다른 주체는 여전히 거부).
    """
    identities = []
    sid = current_owner_sid(runner=runner)
    if sid:
        identities.append(sid)
    name = current_owner_principal()
    if name:
        identities.append(name)
    return tuple(identities)


def _parse_icacls_principals(output: str, path: Path) -> tuple[str, ...]:
    """`icacls <path>` 출력에서 ACE 주체 이름만 뽑는다 (형식 밖은 빈 결과 = 판정 불가).

    출력 첫 줄은 `<경로> <주체>:(권한)` 이고 이후 줄은 들여쓴 `<주체>:(권한)` 이다. 꼬리의
    요약 줄("Successfully processed …")은 **로케일을 탄다**(한국어 Windows 실측) — 문구로
    거르지 않고 `:(` 를 가진 줄만 ACE 로 센다. 권한 표기는 `(F)`·`(OI)(CI)(F)` 처럼 여러 개일
    수 있어 **첫 `:(` 앞**까지를 주체로 자른다.
    """
    principals: list[str] = []
    prefix = str(path)
    for raw in output.splitlines():
        if ":(" not in raw:
            continue
        line = raw.strip()
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
        name = line.split(":(", 1)[0].strip()
        if name:
            principals.append(name)
    return tuple(principals)


def _same_principal(candidate: str, owner: str) -> bool:
    """ACL 주체가 현재 계정인가 — 도메인 표기 차이(`DOMAIN\\me` vs `me`)를 흡수한다.

    SID 표기는 백슬래시가 없어 완전 일치로만 맞는다(뒤 컴포넌트 비교가 헐거워지지 않는다).
    """
    left = candidate.strip().lower().lstrip("*")
    right = owner.strip().lower().lstrip("*")
    if not left or not right:
        return False
    return left == right or left.rsplit("\\", 1)[-1] == right.rsplit("\\", 1)[-1]


def restrict_to_owner(
    path: Path | str, *, runner=None, acl_platform=None,
) -> None:
    """`path` 를 **소유자 전용 접근**으로 제한한다 (파일 0600 · 디렉토리 0700 등가).

    실패는 삼키지 않는다(`AccessRestrictionError`) — 제한이 걸리지 않았는데 성공으로 넘어가면
    호출부는 격리를 믿고 인증 사본·검토 대상 diff 를 그 자리에 쓴다.

    `runner`/`acl_platform` 은 테스트 주입 지점이다(기본은 실제 플랫폼·실제 `icacls`).
    """
    target = Path(path)
    is_acl = windows_acl_platform() if acl_platform is None else acl_platform
    if not is_acl:
        try:
            info = os.stat(target)
        except OSError as exc:
            raise AccessRestrictionError(
                f"접근 제한 대상 확인 실패: {target} ({type(exc).__name__}: {exc})") from exc
        mode = OWNER_ONLY_DIR_MODE if stat.S_ISDIR(info.st_mode) else OWNER_ONLY_FILE_MODE
        try:
            os.chmod(target, mode)
        except OSError as exc:
            raise AccessRestrictionError(
                f"접근 제한 실패: {target} ({type(exc).__name__}: {exc})") from exc
        return
    run = _run_acl_command if runner is None else runner
    grant = owner_grant_principal(runner=runner)
    if not grant:
        raise AccessRestrictionError(
            f"접근 제한 실패 — 현재 계정(SID·이름)을 확인하지 못했습니다: {target}")
    rights = _ACL_DIR_RIGHTS if target.is_dir() else _ACL_FILE_RIGHTS
    try:
        rc, output = run([
            "icacls", str(target), "/inheritance:r", "/grant:r", f"{grant}:{rights}",
        ])
    except OSError as exc:
        raise AccessRestrictionError(
            f"접근 제한 실패 — icacls 실행 불가: {target} "
            f"({type(exc).__name__}: {exc})") from exc
    if rc != 0:
        raise AccessRestrictionError(
            f"접근 제한 실패 — icacls rc={rc} (주체={grant}): {target}\n{output.strip()}")


def owner_only_access(
    path: Path | str, *, runner=None, acl_platform=None,
) -> bool:
    """`path` 가 실제로 소유자 전용 접근인가 — **OS 에 되물어** 확인한다.

    POSIX 는 퍼미션 비트가 정확히 0600(파일)·0700(디렉토리)인지 보고, ACL 플랫폼은
    `icacls` 조회 결과의 ACE 주체가 **현재 계정 하나뿐**인지 본다. 판정 불가(조회 실패·형식
    밖 출력)는 False 다 — 보안 경계에서 모르는 것은 통과가 아니다.
    """
    target = Path(path)
    is_acl = windows_acl_platform() if acl_platform is None else acl_platform
    if not is_acl:
        try:
            info = os.stat(target)
        except OSError:
            return False
        expected = OWNER_ONLY_DIR_MODE if stat.S_ISDIR(info.st_mode) else OWNER_ONLY_FILE_MODE
        return stat.S_IMODE(info.st_mode) == expected
    run = _run_acl_command if runner is None else runner
    try:
        rc, output = run(["icacls", str(target)])
    except OSError:
        return False
    if rc != 0:
        return False
    principals = _parse_icacls_principals(output, target)
    identities = _acceptable_owner_identities(runner=runner)
    return bool(principals) and bool(identities) and all(
        any(_same_principal(name, identity) for identity in identities)
        for name in principals)
