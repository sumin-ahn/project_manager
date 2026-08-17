"""배타 파일락 **Windows 분기** 회귀 — POSIX 개발기에서 그 분기를 실제로 태운다 (T-0725).

`file_lock.acquire_exclusive`/`release_exclusive` 의 Windows 경로는 지금까지 Windows 밖에서
한 줄도 실행되지 않았다. 그 결과 "다른 프로세스를 실제로 배제하는가"를 CI 가 아니라 사람이
Windows VM 에서만 알 수 있었고, 배제가 깨진 채로 여러 릴리즈를 살았다(멀티-PM 리스 장부
lost update = 실 데이터 손상 축). 그래서 분기를 두 층으로 갈랐다:

  - **정책층**(순수 파이썬) — 어느 영역에·어떤 플래그로 걸고 실패를 어떻게 올리는가.
    이 파일이 backend 판정(`lock_backend`)과 원시 API(`_windows_lock_api`)를 갈아끼워
    POSIX 에서 그대로 태운다.
  - **원시층**(ctypes `LockFileEx`) — 커널 호출 자체. 그건 Windows 실측(프로브)이 덮는다.

주입이 no-op 이면 가드가 시험조차 되지 않으므로, 모든 케이스가 **대역이 실제로 불렸는지**를
먼저 단언한다(선-단언). 판정 불능은 통과가 아니다([[guard-must-cover-its-own-surface]]).
"""
from __future__ import annotations

import ast
import importlib.util
import os
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
FILE_LOCK_PY = TOOLS / "file_lock.py"

# Win32 파일 영역 락 API 이름 — 재복제 가드가 세는 대상(주석·문자열 제외·AST 판정).
_WIN32_LOCK_API_NAMES = {"LockFileEx", "UnlockFileEx"}

# 대역이 osf_handle 로 돌려주는 가짜 핸들의 기준값 — fd 와 구분되는 값이어야 "fd 를 그대로
# 원시 호출에 넘기지 않는다"를 단언할 수 있다.
_FAKE_HANDLE_BASE = 7000

# 대역이 흉내내는 Win32 에러코드 (winerror.h `ERROR_LOCK_VIOLATION`).
_ERROR_LOCK_VIOLATION = 33


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def file_lock():
    return _load(FILE_LOCK_PY, "file_lock_windows_branch_under_test")


class FakeWindowsLockApi:
    """`WindowsLockApi` 대역 — 호출을 기록하고 지정한 Win32 에러코드를 돌려준다.

    실제 커널 동작을 흉내내지 않는다(그건 Windows 실측 몫). 이 대역이 고정하는 것은 정책층이
    **무엇을 어떤 인자로 부르는가**와 **에러코드를 어떻게 번역하는가** 둘이다.
    """

    def __init__(self, *, lock_error: int = 0, unlock_error: int = 0) -> None:
        self.lock_error = lock_error
        self.unlock_error = unlock_error
        self.handle_requests: list[int] = []
        self.lock_calls: list[tuple[int, int, int]] = []
        self.unlock_calls: list[tuple[int, int]] = []

    def osf_handle(self, fd: int) -> int:
        self.handle_requests.append(fd)
        return _FAKE_HANDLE_BASE + fd

    def lock_region(self, handle: int, flags: int, length: int) -> int:
        self.lock_calls.append((handle, flags, length))
        return self.lock_error

    def unlock_region(self, handle: int, length: int) -> int:
        self.unlock_calls.append((handle, length))
        return self.unlock_error

    def as_named_tuple(self, module):
        return module.WindowsLockApi(
            self.osf_handle, self.lock_region, self.unlock_region)


def _force_windows_branch(module, monkeypatch, api: FakeWindowsLockApi) -> None:
    """backend 판정과 원시 API 를 Windows 쪽으로 갈아끼운다 (주입 지점 두 곳 모두)."""
    monkeypatch.setattr(module, "lock_backend", lambda: module.WINDOWS_LOCK_BACKEND)
    monkeypatch.setattr(module, "_windows_lock_api", lambda: api.as_named_tuple(module))


# ── 주입이 실제로 걸리는가 (선-단언의 근거) ─────────────────────────────────

def _host_lock_backend(module) -> str:
    """주입 없는 이 호스트의 backend — OS 계열이 고른다(`os.name == "nt"` 면 Windows)."""
    return module.WINDOWS_LOCK_BACKEND if os.name == "nt" else module.POSIX_LOCK_BACKEND


def test_the_real_platform_takes_its_host_branch_without_injection(file_lock):
    """주입 전 baseline — 판정이 호스트 OS 계열을 따른다(=다른 분기는 주입으로만 실행된다).

    이 단언이 없으면 "Windows 분기를 태웠다"는 나머지 케이스가 사실은 호스트 기본 분기를 태우고도
    통과할 수 있다. 한쪽 OS 표기를 baseline 으로 박으면 다른 OS 에서 이 근거가 red 로 뒤집힌다.
    """
    assert file_lock.lock_backend() == _host_lock_backend(file_lock)
    assert file_lock.exclusive_lock_supported() is True


def test_injection_actually_reaches_the_primitive(file_lock, tmp_path, monkeypatch):
    """주입 선-단언 — 갈아끼운 대역이 실제 락 경로에서 불린다(no-op 주입 차단)."""
    api = FakeWindowsLockApi()
    _force_windows_branch(file_lock, monkeypatch, api)

    with file_lock.exclusive_file_lock(tmp_path / "injected.lock"):
        assert api.lock_calls, "주입이 no-op — Windows 분기가 실행되지 않았다"

    assert len(api.lock_calls) == 1
    assert len(api.unlock_calls) == 1


# ── 정책: 무엇을 어떤 인자로 부르는가 ────────────────────────────────────────

def test_windows_acquire_waits_in_the_kernel_instead_of_failing_immediately(
    file_lock, tmp_path, monkeypatch,
):
    """획득 플래그는 배타 + **FAIL_IMMEDIATELY 없음** = 무기한 블로킹(flock 등가).

    비차단으로 걸면 경합 시 그 자리에서 실패하고, 이 seam 은 획득 실패를 삼키지 않으므로
    호출부가 그대로 중단된다 — 직렬화가 아니라 기능 정지가 된다.
    """
    api = FakeWindowsLockApi()
    _force_windows_branch(file_lock, monkeypatch, api)

    with file_lock.exclusive_file_lock(tmp_path / "blocking.lock"):
        pass

    assert api.lock_calls, "주입이 no-op — 플래그를 판정할 수 없다"
    _handle, flags, _length = api.lock_calls[0]
    assert flags & file_lock.LOCKFILE_EXCLUSIVE_LOCK, "배타 플래그 없음(공유 락)"
    assert not flags & file_lock.LOCKFILE_FAIL_IMMEDIATELY, (
        "비차단 획득 — 경합하면 대기하지 않고 실패한다(블로킹 계약 위반)"
    )


def test_windows_lock_covers_the_first_byte_of_an_empty_lock_file(
    file_lock, tmp_path, monkeypatch,
):
    """빈(0바이트) 락 파일의 선두 1바이트를 잠근다 — 파일을 늘리지 않는다.

    영역이 바뀌면 옛 엔진 사본(`msvcrt.locking(fd, …, 1)`)과 서로 배제하지 못한다(사본 skew
    형상에서 배타가 조용히 사라진다). 파일을 늘리면 "락 파일은 내용이 없다"는 관례가 깨진다.
    """
    api = FakeWindowsLockApi()
    _force_windows_branch(file_lock, monkeypatch, api)
    lock_path = tmp_path / "empty.lock"

    with file_lock.exclusive_file_lock(lock_path):
        assert api.lock_calls, "주입이 no-op — 영역을 판정할 수 없다"
        assert lock_path.stat().st_size == 0, "락이 락 파일에 바이트를 썼다"

    assert file_lock.LOCK_REGION_BYTES == 1
    assert api.lock_calls[0][2] == file_lock.LOCK_REGION_BYTES
    assert api.unlock_calls[0][1] == file_lock.LOCK_REGION_BYTES


def test_windows_branch_converts_the_descriptor_to_an_os_handle(
    file_lock, tmp_path, monkeypatch,
):
    """fd 를 그대로 원시 호출에 넘기지 않는다 — `osf_handle` 로 OS 핸들을 얻어 쓴다."""
    api = FakeWindowsLockApi()
    _force_windows_branch(file_lock, monkeypatch, api)

    with file_lock.exclusive_file_lock(tmp_path / "handle.lock"):
        pass

    assert api.handle_requests, "주입이 no-op — 핸들 변환을 판정할 수 없다"
    expected = [_FAKE_HANDLE_BASE + fd for fd in api.handle_requests]
    assert [call[0] for call in api.lock_calls] == expected[:1]
    assert [call[0] for call in api.unlock_calls] == expected[1:]


# ── 정책: 실패를 어떻게 올리는가 ─────────────────────────────────────────────

def test_windows_acquire_failure_raises_instead_of_progressing_lockless(
    file_lock, tmp_path, monkeypatch,
):
    """획득이 Win32 에러로 실패하면 임계 구역에 들어가지 않는다 (fd 는 닫힌다).

    프리미티브가 *있는데* 실패한 것이므로 무락 폴백 대상이 아니다 — 배타 없는 구간을 성공으로
    위장하면 직렬화가 조용히 사라진다.
    """
    api = FakeWindowsLockApi(lock_error=_ERROR_LOCK_VIOLATION)
    _force_windows_branch(file_lock, monkeypatch, api)
    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(
        file_lock.os, "close", lambda fd: closed.append(fd) or real_close(fd))

    with pytest.raises(OSError) as raised:
        with file_lock.exclusive_file_lock(tmp_path / "denied.lock"):
            pytest.fail("배타성 없이 임계 구역에 진입했다")

    assert api.lock_calls, "주입이 no-op — 실패 번역을 판정할 수 없다"
    assert str(_ERROR_LOCK_VIOLATION) in str(raised.value), "진단에 WinError 가 없다"
    assert "LockFileEx" in str(raised.value)
    assert len(closed) == 1, "실패 경로에서 fd 가 새어나갔다"
    assert not api.unlock_calls, "획득하지도 않은 영역을 해제하려 했다"


def test_windows_release_failure_is_reported_instead_of_swallowed(
    file_lock, tmp_path, monkeypatch,
):
    """해제 실패도 삼키지 않는다 — 남은 영역 락은 다음 획득을 이유 없이 막는다."""
    api = FakeWindowsLockApi(unlock_error=_ERROR_LOCK_VIOLATION)
    _force_windows_branch(file_lock, monkeypatch, api)

    with pytest.raises(OSError) as raised:
        with file_lock.exclusive_file_lock(tmp_path / "stuck.lock"):
            pass

    assert api.unlock_calls, "주입이 no-op — 해제 실패 번역을 판정할 수 없다"
    assert "UnlockFileEx" in str(raised.value)
    assert str(_ERROR_LOCK_VIOLATION) in str(raised.value)


# ── 무락 폴백은 loud ─────────────────────────────────────────────────────────

def test_lockless_fallback_announces_itself(file_lock, tmp_path, monkeypatch):
    """프리미티브가 둘 다 없으면 무락으로 진행하되 **그 사실을 알린다** (조용한 degrade 0).

    엔진의 다른 강등 신호(`repo_owned_files.RepoFilesFallbackWarning`)와 같은 수단을 쓴다.
    """
    monkeypatch.setattr(file_lock, "lock_backend", lambda: file_lock.NO_LOCK_BACKEND)
    lock_path = tmp_path / "lockless.lock"

    with pytest.warns(file_lock.LocklessFallbackWarning) as recorded:
        with file_lock.exclusive_file_lock(lock_path):
            pass

    assert file_lock.LOCKLESS_FALLBACK_MESSAGE in str(recorded[0].message)
    assert lock_path.is_file(), "폴백에서도 락 파일 자체는 생긴다(인터페이스 동일)"


def test_lockless_fallback_warning_is_a_runtime_warning_subclass(file_lock):
    """경고 분류가 `RuntimeWarning` 계열이라 기본 필터에서 stderr 로 나온다."""
    assert issubclass(file_lock.LocklessFallbackWarning, RuntimeWarning)


def test_lockless_fallback_warning_is_attributed_to_the_seam(
    file_lock, tmp_path, monkeypatch,
):
    """경고 귀속은 **seam 자신** — 기본 필터가 프로세스당 1회로 접는 근거다.

    호출자 프레임에 귀속하면 락을 잡는 자리 수만큼 같은 줄이 새 지점으로 다시 뜬다(장부 op 마다
    반복). 귀속이 한 곳이면 기본 필터의 지점별 1회가 곧 프로세스당 1회가 된다.
    """
    monkeypatch.setattr(file_lock, "lock_backend", lambda: file_lock.NO_LOCK_BACKEND)

    with pytest.warns(file_lock.LocklessFallbackWarning) as recorded:
        for name in ("first.lock", "second.lock"):
            with file_lock.exclusive_file_lock(tmp_path / name):
                pass

    assert len(recorded) == 2, "두 구간 모두에서 신호가 나야 한다(필터는 바깥이 결정)"
    assert {Path(item.filename).name for item in recorded} == {FILE_LOCK_PY.name}
    assert len({item.lineno for item in recorded}) == 1, "귀속 지점이 호출자마다 갈린다"


def test_windows_platform_never_reaches_the_lockless_fallback(
    file_lock, tmp_path, monkeypatch,
):
    """Windows 는 프리미티브가 있으므로 무락 폴백에 떨어지지 않는다 ([[no-green-by-disabling]])."""
    api = FakeWindowsLockApi()
    _force_windows_branch(file_lock, monkeypatch, api)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with file_lock.exclusive_file_lock(tmp_path / "windows.lock"):
            pass

    assert api.lock_calls, "주입이 no-op — 폴백 여부를 판정할 수 없다"
    assert not [
        item for item in recorded
        if issubclass(item.category, file_lock.LocklessFallbackWarning)
    ], "Windows 인데 무락 폴백으로 떨어졌다"


# ── backend 판정 ─────────────────────────────────────────────────────────────

def test_backend_names_are_distinct_and_support_derives_from_the_backend(file_lock):
    """지원 판정은 backend 이름 하나에서 나온다(두 판정이 갈리면 폴백 규칙이 갈라진다)."""
    names = {
        file_lock.POSIX_LOCK_BACKEND,
        file_lock.WINDOWS_LOCK_BACKEND,
        file_lock.NO_LOCK_BACKEND,
    }
    assert len(names) == 3


@pytest.mark.parametrize(
    ("backend_name", "supported"),
    (
        ("POSIX_LOCK_BACKEND", True),
        ("WINDOWS_LOCK_BACKEND", True),
        ("NO_LOCK_BACKEND", False),
    ),
)
def test_exclusive_lock_supported_follows_each_backend(
    file_lock, monkeypatch, backend_name, supported,
):
    backend = getattr(file_lock, backend_name)
    monkeypatch.setattr(file_lock, "lock_backend", lambda: backend)
    assert file_lock.exclusive_lock_supported() is supported


def test_windows_backend_is_selected_by_the_handle_capability(file_lock, monkeypatch):
    """Windows backend 판정은 실제로 필요한 능력(`msvcrt.get_osfhandle`)을 본다.

    `fcntl` 부재 + `msvcrt` 존재 형상을 만들어 판정 자체를 태운다 — 플랫폼 문자열이 아니라
    능력으로 갈리는지 확인한다.
    """
    import sys
    import types

    real_import = __import__
    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.get_osfhandle = lambda fd: fd

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr("builtins.__import__", no_fcntl)
    assert file_lock.lock_backend() == file_lock.WINDOWS_LOCK_BACKEND

    del fake_msvcrt.get_osfhandle
    assert file_lock.lock_backend() == file_lock.NO_LOCK_BACKEND


# ── 재복제 차단 (Win32 API 이름) ─────────────────────────────────────────────

def _win32_lock_api_reference_count(source: str) -> int:
    """소스 한 벌의 `LockFileEx`/`UnlockFileEx` 참조 수 (주석·문자열 제외·AST 판정).

    기존 가드(`test_file_lock.py`)는 `fcntl.flock`·`msvcrt.locking` 호출만 센다. Windows 분기가
    Win32 API 직접 호출로 바뀌면 그 이름으로 재복제한 사본은 옛 가드의 사각이다 — 가드 시야를
    자기 표면에 맞춰 넓힌다([[guard-must-cover-its-own-surface]]).
    """
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _WIN32_LOCK_API_NAMES:
            count += 1
        elif isinstance(node, ast.Name) and node.id in _WIN32_LOCK_API_NAMES:
            count += 1
    return count


def test_win32_lock_api_lives_only_in_the_shared_seam():
    """Win32 파일 영역 락 호출도 공용 seam 한 곳뿐이다 (도구별 재복제 0)."""
    measured = {
        path.name
        for path in sorted(TOOLS.glob("*.py"))
        if _win32_lock_api_reference_count(path.read_text(encoding="utf-8"))
    }
    assert measured == {"file_lock.py"}, f"Win32 락 API 재복제: {sorted(measured)}"


def test_win32_guard_ignores_the_names_in_comments_and_strings():
    """주석·문자열의 API 언급은 세지 않는다 (문서화 자유·AST 판정)."""
    prose = (
        '"""Windows 는 LockFileEx 로 잠근다."""\n'
        "# kernel32.LockFileEx(handle, flags, 0, 1, 0, overlapped)\n"
        'DOC = "UnlockFileEx"\n'
    )
    assert _win32_lock_api_reference_count(prose) == 0


def test_win32_guard_counts_a_reintroduced_call_in_another_tool():
    """다른 도구에 Win32 락 호출을 되살리면 가드가 red 로 잡는다 (감도 실증)."""
    source = (TOOLS / "worktree_pool.py").read_text(encoding="utf-8")
    mutated = source.replace(
        "    with file_lock.exclusive_file_lock(LEASES_LOCK):\n        yield",
        "    import ctypes\n"
        "    ctypes.windll.kernel32.LockFileEx(0, 2, 0, 1, 0, None)\n"
        "    yield",
        1,
    )
    assert mutated != source, "변이 앵커 소실"
    assert _win32_lock_api_reference_count(source) == 0, "worktree_pool 은 수렴 상태여야 한다"
    assert _win32_lock_api_reference_count(mutated) > 0


# ── 감도: 비차단으로 되돌리면 red ────────────────────────────────────────────

def test_a_non_blocking_windows_acquire_is_caught_by_the_guard(tmp_path, monkeypatch):
    """소스를 비차단 획득으로 되돌린 사본은 블로킹 가드에 걸린다 (가드 감도 실증).

    "블로킹인가"를 대역 인자로만 판정하므로, 그 판정이 실제로 민감한지 원본을 변이시켜 보인다.
    """
    source = FILE_LOCK_PY.read_text(encoding="utf-8")
    mutated = source.replace(
        "            api.osf_handle(fd), LOCKFILE_EXCLUSIVE_LOCK, LOCK_REGION_BYTES)",
        "            api.osf_handle(fd),\n"
        "            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,\n"
        "            LOCK_REGION_BYTES)",
        1,
    )
    assert mutated != source, "변이 앵커 소실"
    mutated_path = tmp_path / "file_lock_nonblocking.py"
    mutated_path.write_text(mutated, encoding="utf-8", newline="\n")
    module = _load(mutated_path, "file_lock_nonblocking_under_test")

    api = FakeWindowsLockApi()
    _force_windows_branch(module, monkeypatch, api)
    with module.exclusive_file_lock(tmp_path / "mutated.lock"):
        pass

    assert api.lock_calls, "주입이 no-op — 변이를 판정할 수 없다"
    _handle, flags, _length = api.lock_calls[0]
    assert flags & module.LOCKFILE_FAIL_IMMEDIATELY, "변이가 실제로 비차단이 아니다"
