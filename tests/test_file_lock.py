"""공용 동시-쓰기 파일 프리미티브 seam 회귀 (T-0561·T-0565).

`file_lock.py` 가 두 프리미티브를 소유한다 — **배타 파일락**(board `board.lock`·
`board-git.lock` / pm_log `log.lock` / pm_relay raw 장부 `<ledger>.lock` / pm_handoff
`dashboard.lock` / worktree_pool `worktree-leases.lock` / external_review 라운드 장부
`review_rounds.lock`)과 **O_APPEND 원자 추가**(board areas 등록 · pm_log log append). 각 도구가
복제하던 플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)와 append 구현을 한 곳으로 수렴했다.
검증 축:

  1. seam 자체 — 경로/권한/획득·해제·close 순서, 실 프로세스 간 상호배제, 보유자 크래시 시
     OS 자동 해제(stale lock 없음), append 의 O_APPEND 단일 write.
  2. 소비 — 소비 도구가 *같은 파일*의 seam 을 쓰고 각자의 경로 규약(0o600·`.local/log.lock`·
     `.local/dashboard.lock` 등)은 그대로 보존한다.
  3. 재-복제 차단 — 어느 도구에든 플랫폼 락 분기나 O_APPEND write 가 되살아나면 red. 미수렴
     사본 등재부는 현재 0건(수렴 완결)이라 등재를 되살리려면 완결 단언을 함께 손대야 한다.
  4. 사본 skew — 소비자가 stale `file_lock.py` 를 만나면 marked skew 로 fail-loud.

도구는 패키지가 아니므로 importlib 경로 로드 관용구를 쓴다(test_pm_log 동형).
"""
from __future__ import annotations

import ast
import importlib.util
import multiprocessing as mp
import os
import re
import shutil
import sys
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
FILE_LOCK_PY = TOOLS / "file_lock.py"

SYNC_TIMEOUT = 120

# 아직 공용 seam 으로 수렴하지 않은 사본의 등재부 — **현재 0건**(전 도구 수렴 완결: board·
# pm_log·pm_relay·pm_handoff·worktree_pool·external_review). 비어 있는 상태가 정상이고,
# `test_lock_convergence_has_no_pending_duplicates` 가 그 완결을 박제한다. 새 사본이 불가피하면
# 사유와 함께 등재하되(그만큼 가드가 느슨해진다) 그 완결 테스트도 함께 손대야 하므로, 조용히
# 미수렴 사본이 되살아나지 않는다.
PENDING_DUPLICATE_LOCK_MODULES: dict[str, str] = {}

# 플랫폼 락 프리미티브 (모듈 → 그 모듈의 락 호출 이름). 주석·문자열 아님·AST 로만 판정.
_LOCK_PRIMITIVES = {"fcntl": {"flock"}, "msvcrt": {"locking"}}

# O_APPEND 원자 추가 플래그 — 이 이름을 *참조*하는 모듈이 곧 append write 사본 보유자다.
_APPEND_FLAG = "O_APPEND"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def file_lock():
    return _load(FILE_LOCK_PY, "file_lock_under_test")


def _lock_primitive_bindings(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """import 로 묶인 이름을 해소한다 → (모듈-별칭 → 실 모듈, 락 함수 직접-이름 집합).

    엔진은 락 프리미티브를 함수 *안*에서 import 하므로 tree.body 가 아니라 전수 walk 한다.
    deep-import 가드의 `_spec_aliases` 선례와 같은 이유로 별칭을 해소한다 — 이름 그대로만 보면
    `import fcntl as _f` / `from fcntl import flock` 형태의 재복제가 가드를 그냥 통과한다.
    """
    module_aliases: dict[str, str] = {}
    direct_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _LOCK_PRIMITIVES:
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in _LOCK_PRIMITIVES:
            for alias in node.names:
                if alias.name in _LOCK_PRIMITIVES[node.module]:
                    direct_names.add(alias.asname or alias.name)
    return module_aliases, direct_names


def _lock_call_count(source: str) -> int:
    """소스 한 벌의 플랫폼 락 호출 수 (별칭·from-import 해소·주석/문자열 제외)."""
    tree = ast.parse(source)
    module_aliases, direct_names = _lock_primitive_bindings(tree)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            # 별칭이면 실 모듈로, 아니면 이름 그대로 본다(import 없이 주입된 대역도 포착).
            module = module_aliases.get(func.value.id, func.value.id)
            if func.attr in _LOCK_PRIMITIVES.get(module, ()):
                count += 1
        elif isinstance(func, ast.Name) and func.id in direct_names:
            count += 1
    return count


def _modules_with_platform_lock_calls() -> dict[str, int]:
    """tools/ 에서 플랫폼 락 프리미티브를 *호출*하는 모듈 → 호출 수."""
    found = {
        path.name: _lock_call_count(path.read_text(encoding="utf-8"))
        for path in sorted(TOOLS.glob("*.py"))
    }
    return {name: calls for name, calls in found.items() if calls}


def _append_flag_reference_count(source: str) -> int:
    """소스 한 벌의 `O_APPEND` 참조 수 (from-import 별칭 해소·주석/문자열 제외).

    append write 는 락처럼 전용 호출 이름이 없고 `os.open` 의 *플래그*로 드러난다 — 그래서
    플래그 이름 참조를 센다. `_lock_call_count` 와 같은 이유로 별칭을 해소한다.
    """
    tree = ast.parse(source)
    direct_names = {_APPEND_FLAG}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == _APPEND_FLAG:
                    direct_names.add(alias.asname or alias.name)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == _APPEND_FLAG:
            count += 1
        elif isinstance(node, ast.Name) and node.id in direct_names:
            count += 1
    return count


def _modules_with_append_writes() -> dict[str, int]:
    """tools/ 에서 O_APPEND write 를 직접 구현하는 모듈 → 플래그 참조 수."""
    found = {
        path.name: _append_flag_reference_count(path.read_text(encoding="utf-8"))
        for path in sorted(TOOLS.glob("*.py"))
    }
    return {name: refs for name, refs in found.items() if refs}


# ── 1. seam 자체 ──────────────────────────────────────────────────────────

def test_lock_file_and_parent_directory_are_created_with_requested_mode(
    file_lock, tmp_path, monkeypatch,
):
    """부모 디렉토리를 만들고 요청한 권한으로 락 파일을 연다 (기본 0o644·명시 mode 존중)."""
    calls: list[tuple] = []
    real_open = os.open
    monkeypatch.setattr(
        file_lock.os,
        "open",
        lambda path, flags, mode=0: calls.append((Path(path), flags, mode))
        or real_open(path, flags, mode),
    )

    lock_path = tmp_path / "nested" / "dir" / "ledger.json.lock"
    with file_lock.exclusive_file_lock(lock_path, mode=0o600):
        pass
    with file_lock.exclusive_file_lock(tmp_path / "default.lock"):
        pass

    assert calls[0][0] == lock_path
    assert calls[0][2] == 0o600
    assert calls[1][2] == file_lock.DEFAULT_LOCK_MODE == 0o644
    assert lock_path.is_file()
    assert (calls[0][1] & os.O_CREAT) and (calls[0][1] & os.O_RDWR)


def test_acquire_release_close_wrap_the_critical_section(
    file_lock, tmp_path, monkeypatch,
):
    """획득 → 임계구역 → 해제 → close 순서 (예외 경로에서도 해제·close)."""
    calls: list[str] = []
    monkeypatch.setattr(file_lock, "acquire_exclusive", lambda fd: calls.append("acquire"))
    monkeypatch.setattr(file_lock, "release_exclusive", lambda fd: calls.append("release"))
    real_close = os.close
    monkeypatch.setattr(
        file_lock.os, "close", lambda fd: calls.append("close") or real_close(fd)
    )

    with file_lock.exclusive_file_lock(tmp_path / "a.lock"):
        calls.append("critical")
    assert calls == ["acquire", "critical", "release", "close"]

    calls.clear()
    with pytest.raises(RuntimeError):
        with file_lock.exclusive_file_lock(tmp_path / "a.lock"):
            raise RuntimeError("boom")
    assert calls == ["acquire", "release", "close"]


def test_exclusive_lock_supported_is_pure_and_matches_available_backend(
        file_lock, monkeypatch):
    """지원 판정은 fd/open/acquire 없이 backend 존재만 보고한다."""
    calls = []
    monkeypatch.setattr(
        file_lock.os, "open",
        lambda *_a, **_kw: calls.append("open") or pytest.fail("support probe opened fd"),
    )
    assert file_lock.exclusive_lock_supported() is True
    assert calls == []


def test_exclusive_lock_supported_reports_no_backend_without_changing_fallback(
        file_lock, monkeypatch):
    """두 stdlib backend 부재는 False지만 기존 acquire 무락 폴백 계약은 유지한다."""
    real_import = __import__

    def no_lock_import(name, *args, **kwargs):
        if name in {"fcntl", "msvcrt"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_lock_import)
    assert file_lock.exclusive_lock_supported() is False
    assert file_lock.acquire_exclusive(12345) is None
    assert file_lock.release_exclusive(12345) is None


def test_primitive_failure_is_raised_instead_of_progressing_lockless(
    file_lock, tmp_path, monkeypatch,
):
    """프리미티브가 *있는데* 획득이 실패하면 무락 진행하지 않고 그대로 올린다 (계약 핀).

    무락 폴백은 프리미티브 **부재**(import 실패) 전용이다 — 획득 실패(Windows msvcrt 재시도
    소진 등)를 삼키면 배타성 없는 임계 구역이 성공으로 위장돼 직렬화가 조용히 사라진다.
    실패해도 fd 는 닫힌다(누수 0).
    """
    stub = types.ModuleType("fcntl")
    stub.LOCK_EX = 2
    stub.LOCK_UN = 8

    def _boom(fd, operation):
        raise OSError(11, "resource temporarily unavailable")

    stub.flock = _boom
    monkeypatch.setitem(sys.modules, "fcntl", stub)
    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(
        file_lock.os, "close", lambda fd: closed.append(fd) or real_close(fd)
    )

    with pytest.raises(OSError):
        with file_lock.exclusive_file_lock(tmp_path / "contended.lock"):
            pytest.fail("배타성 없이 임계 구역에 진입했다")

    assert len(closed) == 1


def test_lock_file_survives_the_critical_section(file_lock, tmp_path):
    """락 파일을 지우지 않는다 — 지우면 다른 프로세스가 쥔 inode 와 갈라져 배타성이 깨진다."""
    lock_path = tmp_path / "keep.lock"
    with file_lock.exclusive_file_lock(lock_path):
        assert lock_path.is_file()
    assert lock_path.is_file()


def test_local_conf_lock_derives_its_path_from_the_target_conf(file_lock, tmp_path):
    """local.conf 락은 **대상 conf** 에서 유도한다 — 남의 트리 conf 를 쓰는 진입도 같은 파일에.

    이 경로 규약만 seam 이 소유하는 이유는 그것이 한 도구의 내부 관례가 아니라 도구 *간* 규약이기
    때문이다(사본이 갈리면 같은 conf 에 다른 락 = 직렬화 없음).
    """
    conf = tmp_path / "dest" / ".project_manager" / "local.conf"
    expected = conf.parent / ".local" / file_lock.LOCAL_CONF_LOCK_NAME
    assert file_lock.conf_lock_path(conf) == expected
    assert file_lock.conf_lock_path(str(conf)) == expected

    with file_lock.local_conf_write_lock(conf):
        assert expected.is_file()          # 부모 디렉토리 생성 포함
    assert expected.is_file()              # 구간 뒤에도 남는다(inode 유지)
    assert not conf.exists(), "락이 conf 자체를 만들지 않는다"


def _has_lock_primitive() -> bool:
    """OS 배타락 프리미티브(fcntl/msvcrt) 유무 — 없으면 seam 은 무락 폴백(배타성 단언 비적용)."""
    try:
        import fcntl  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import msvcrt  # noqa: F401
        return True
    except ImportError:
        return False


def _probe_lock_free(lock_path: Path) -> bool:
    """`lock_path` 에 *비차단* 배타락이 잡히는지 (잡으면 즉시 해제). True=free."""
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return True
            except (BlockingIOError, OSError):
                return False
        except ImportError:
            pass
        try:
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return True
            except OSError:
                return False
        except ImportError:
            return True  # 폴백 무락 — 항상 free
    finally:
        os.close(fd)


def _worker_hold_lock(module_path: str, lock_path: str, acquired) -> None:
    """락을 잡은 채 멈춘다(부모가 terminate) — 크래시-시-자동해제 검증용."""
    spec = importlib.util.spec_from_file_location("file_lock_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cm = module.exclusive_file_lock(Path(lock_path))
    cm.__enter__()
    acquired.set()
    time.sleep(3600)


def test_second_process_is_excluded_and_crash_releases_the_lock(tmp_path):
    """다른 프로세스가 보유 중이면 비차단 획득이 실패하고, 그 프로세스가 죽으면 풀린다."""
    lock_path = tmp_path / "cross-process.lock"
    ctx = mp.get_context("spawn")
    acquired = ctx.Event()
    child = ctx.Process(
        target=_worker_hold_lock, args=(str(FILE_LOCK_PY), str(lock_path), acquired)
    )
    child.start()
    try:
        assert acquired.wait(timeout=SYNC_TIMEOUT), "자식이 락을 획득하지 못함"
        # skip 판정은 *능력* 탐지 하나로만 좁힌다 — "지금 락이 free 인가"로 skip 하면
        # 배타성이 실제로 깨진 회귀(프리미티브는 있는데 획득 실패)가 green 으로 통과한다.
        if not _has_lock_primitive():
            pytest.skip("락 프리미티브 없음(폴백 무락) — 배타성 단언 비적용")

        assert not _probe_lock_free(lock_path), "보유 중인데 락이 free (배타성 위반)"

        child.terminate()
        child.join(timeout=SYNC_TIMEOUT)

        deadline = time.time() + SYNC_TIMEOUT
        while time.time() < deadline and not _probe_lock_free(lock_path):
            time.sleep(0.05)
        assert _probe_lock_free(lock_path), "보유자 크래시 후에도 안 풀림 (stale-lock)"
    finally:
        if child.is_alive():
            child.terminate()
        child.join(timeout=10)


def test_append_atomic_creates_the_file_and_appends_without_truncating(
    file_lock, tmp_path,
):
    """append 는 없으면 만들고, 있으면 끝에 붙인다 (덮어쓰기 아님·수렴 전 동작 보존)."""
    target = tmp_path / "areas.md"
    file_lock.append_atomic(target, "line1\n")
    file_lock.append_atomic(target, "line2\n")
    assert target.read_text(encoding="utf-8") == "line1\nline2\n"


def test_append_atomic_uses_one_o_append_write_with_requested_mode(
    file_lock, tmp_path, monkeypatch,
):
    """RMW 없이 O_APPEND 단일 write — open/write/close 3콜·권한은 호출자 소유."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        file_lock.os,
        "open",
        lambda path, flags, mode=0: calls.append(("open", path, flags, mode)) or 41,
    )
    monkeypatch.setattr(
        file_lock.os, "write", lambda fd, payload: calls.append(("write", fd, payload))
    )
    monkeypatch.setattr(file_lock.os, "close", lambda fd: calls.append(("close", fd)))

    target = tmp_path / "current.md"
    file_lock.append_atomic(target, "\nentry")
    file_lock.append_atomic(target, "x", mode=0o600)

    assert calls[0][0:2] == ("open", str(target))
    assert calls[0][2] & os.O_APPEND and calls[0][2] & os.O_CREAT
    assert calls[0][2] & os.O_WRONLY == os.O_WRONLY
    assert calls[0][3] == file_lock.DEFAULT_APPEND_MODE == 0o644
    assert calls[1:3] == [("write", 41, b"\nentry"), ("close", 41)]
    assert calls[3][3] == 0o600


def test_append_atomic_closes_the_descriptor_when_the_write_fails(
    file_lock, tmp_path, monkeypatch,
):
    """write 실패에도 fd 를 닫는다 (예외 경로 누수 0)."""
    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(
        file_lock.os, "close", lambda fd: closed.append(fd) or real_close(fd)
    )

    def boom(fd, payload):
        raise OSError("disk full")

    monkeypatch.setattr(file_lock.os, "write", boom)
    with pytest.raises(OSError):
        file_lock.append_atomic(tmp_path / "a.md", "x")
    assert len(closed) == 1


# ── 2. 소비 (수렴 도구가 같은 seam 파일을 쓴다) ─────────────────────────────

@pytest.mark.parametrize(
    "tool", ("board.py", "pm_handoff.py", "worktree_pool.py")
)
def test_import_time_consumers_bind_the_canonical_seam(tool):
    """락이 *모든* 변경 경로에 깔린 도구는 import 시점에 seam 을 바인딩한다 (지연 로드 아님).

    락을 잡을 때마다 형제를 로드하면 그 도구를 fail-soft 로 소비하는 호출층이 사본 skew 를
    조용히 삼키는 경로가 락 호출 그래프만큼 늘어난다(test_engine_rev_failsoft_guard 의 경계
    ratchet 이 그 확산을 계량한다) — import 경계 단일 fail-loud 로 그 확산을 막는다.
    """
    module = _load(TOOLS / tool, f"{tool[:-3]}_file_lock_seam")
    assert Path(module.file_lock.__file__).resolve() == FILE_LOCK_PY.resolve()
    assert "with file_lock.exclusive_file_lock(" in (
        TOOLS / tool
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "tool",
    ("pm_log.py", "pm_relay.py", "external_review.py", "pm_update.py", "pm_import.py"),
)
def test_lazy_consumers_load_the_canonical_seam(tool):
    """지연 소비자는 쓰는 경로에서만 seam 을 로드한다(읽기·재사용 경로 fail-soft 보존).

    pm_update·pm_import 는 **복구 채널**이라 지연 로드다 — 엔진 사본이 부분적으로 깨진 트리에서도
    이 두 도구는 떠야 하고(자기 자신을 고치는 경로), conf 락은 그 안의 쓰기 구간에서만 필요하다.
    """
    module = _load(TOOLS / tool, f"{tool[:-3]}_file_lock_seam")
    assert Path(module._load_file_lock().__file__).resolve() == FILE_LOCK_PY.resolve()


def test_board_append_uses_the_seam_instead_of_its_own_o_append_write():
    """board 의 areas 등록은 seam append 로 위임한다 (자체 O_APPEND write 0).

    (pm_log 쪽 append 위임은 그 도구 suite 가 대역으로 라우팅까지 본다 —
    `test_pm_log.py::test_append_log_delegates_the_write_to_the_shared_seam`.)
    """
    board = _load(TOOLS / "board.py", "board_append_seam")
    source = (TOOLS / "board.py").read_text(encoding="utf-8")
    assert "file_lock.append_atomic(" in source
    assert not hasattr(board, "_append_atomic")


def test_relay_raw_ledger_lock_keeps_its_path_and_permission_convention(
    tmp_path, monkeypatch,
):
    """raw 장부 락 = 장부 옆 `<name>.lock` · 0o600 (수렴 전 동작 보존)."""
    relay = _load(TOOLS / "pm_relay.py", "pm_relay_file_lock_seam")
    ledger = tmp_path / "raw_outputs.json"
    seen: list[tuple] = []
    lock_module = relay._load_file_lock()
    real_open = os.open
    monkeypatch.setattr(
        lock_module.os,
        "open",
        lambda path, flags, mode=0: seen.append((Path(path), mode))
        or real_open(path, flags, mode),
    )

    with relay._raw_ledger_lock(ledger):
        pass

    assert seen == [(tmp_path / "raw_outputs.json.lock", 0o600)]


def test_pm_log_lock_path_stays_in_project_local(tmp_path):
    """log 락 = `.project_manager/.local/log.lock` (수렴 전 경로 규약 보존)."""
    pm_log = _load(TOOLS / "pm_log.py", "pm_log_file_lock_seam")
    current = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    with pm_log.log_write_lock(current):
        pass
    assert (tmp_path / ".project_manager" / ".local" / "log.lock").is_file()


def _record_seam_opens(lock_module, monkeypatch) -> list[tuple]:
    """seam 이 여는 (경로, 권한)을 기록한다 (실제 open 은 그대로 수행)."""
    seen: list[tuple] = []
    real_open = os.open
    monkeypatch.setattr(
        lock_module.os,
        "open",
        lambda path, flags, mode=0: seen.append((Path(path), mode))
        or real_open(path, flags, mode),
    )
    return seen


def test_handoff_dashboard_lock_keeps_its_path_and_permission_convention(
    tmp_path, monkeypatch,
):
    """대시보드 락 = `.project_manager/.local/dashboard.lock` · 0o644 (수렴 전 규약 보존)."""
    handoff = _load(TOOLS / "pm_handoff.py", "pm_handoff_file_lock_seam")
    monkeypatch.setattr(handoff, "REPO", tmp_path)
    seen = _record_seam_opens(handoff.file_lock, monkeypatch)

    with handoff._dashboard_lock():
        pass

    assert seen == [
        (tmp_path / ".project_manager" / ".local" / "dashboard.lock", 0o644)
    ]


def test_worktree_pool_lease_lock_keeps_its_path_and_permission_convention(
    tmp_path, monkeypatch,
):
    """리스 장부 락 = `.local/worktree-leases.lock` · 0o644 (수렴 전 규약 보존)."""
    pool = _load(TOOLS / "worktree_pool.py", "worktree_pool_file_lock_seam")
    lock_path = tmp_path / ".project_manager" / ".local" / "worktree-leases.lock"
    monkeypatch.setattr(pool, "LEASES_LOCK", lock_path)
    seen = _record_seam_opens(pool.file_lock, monkeypatch)

    with pool._lease_lock():
        pass

    assert seen == [(lock_path, 0o644)]


# ── 3. 재-복제 차단 ───────────────────────────────────────────────────────

def test_platform_lock_branch_lives_only_in_the_shared_seam():
    """플랫폼 락 호출은 공용 seam + 아직 미수렴 사본(등재분)에만 있다.

    수렴한 도구에 분기가 되살아나거나, 등재 밖 모듈에 새 사본이 생기면 red.
    미수렴 사본이 후속 티켓에서 수렴하면 이 목록을 함께 지운다.
    """
    measured = set(_modules_with_platform_lock_calls())
    expected = {"file_lock.py"} | set(PENDING_DUPLICATE_LOCK_MODULES)
    assert measured == expected, (
        f"플랫폼 락 분기 보유 모듈이 예상과 불일치 — 추가: {sorted(measured - expected)} / "
        f"사라짐(목록 정리 필요): {sorted(expected - measured)}"
    )
    assert all(reason.strip() for reason in PENDING_DUPLICATE_LOCK_MODULES.values())


def test_lock_convergence_has_no_pending_duplicates():
    """수렴 완결 박제 — 미수렴 등재 0건이고 플랫폼 락 분기는 seam 단 하나다.

    등재부를 다시 채우려면 이 단언을 의도적으로 손봐야 한다(조용한 되돌림 차단).
    """
    assert PENDING_DUPLICATE_LOCK_MODULES == {}
    assert set(_modules_with_platform_lock_calls()) == {"file_lock.py"}


def test_converged_tools_delegate_instead_of_reimplementing():
    """수렴 도구는 락 컨텍스트를 seam 위임으로만 연다 (자체 open+acquire 재구현 0)."""
    for tool in (
        "board.py", "pm_log.py", "pm_relay.py", "pm_handoff.py", "worktree_pool.py",
        "external_review.py",
    ):
        source = (TOOLS / tool).read_text(encoding="utf-8")
        assert "exclusive_file_lock(" in source, tool
        assert not re.search(r"def _[a-z_]*flock_(acquire|release)\b", source), tool


def test_o_append_write_lives_only_in_the_shared_seam():
    """O_APPEND write 도 공용 seam 한 곳뿐이다 (board/pm_log 사본 수렴분).

    판정 폭은 락 가드와 같다 — `tools/*.py` 전수를 AST 로 훑어 **`O_APPEND` 플래그 참조**를
    센다(주석·문자열 제외·`from os import O_APPEND as X` 별칭 해소). 락 가드가 호출 이름으로
    보는 자리를 append 는 플래그 이름으로 볼 뿐 대상 범위는 동일하다. 문자열 조립
    (`getattr(os, "O_APPEND")`)은 두 가드 모두의 사각이다 — 재복제는 그렇게 우회하지 않고
    쓰이므로 이 폭을 의도적으로 유지한다.
    """
    assert set(_modules_with_append_writes()) == {"file_lock.py"}


def test_converged_tools_delegate_the_append_write():
    """append 소비 도구는 `_append_atomic` 사본을 정의하지 않고 seam 을 부른다."""
    for tool in ("board.py", "pm_log.py"):
        source = (TOOLS / tool).read_text(encoding="utf-8")
        assert "append_atomic(" in source, tool
        assert not re.search(r"^def _append_atomic\b", source, re.MULTILINE), tool


@pytest.mark.parametrize(
    ("label", "snippet"),
    (
        ("plain", "import os\nos.open(p, os.O_WRONLY | os.O_APPEND, 0o644)\n"),
        (
            "from-import",
            "from os import O_APPEND, open as _open\n_open(p, O_APPEND, 0o644)\n",
        ),
        (
            "from-import-alias",
            "from os import O_APPEND as _APP\nos.open(p, _APP, 0o644)\n",
        ),
    ),
)
def test_append_guard_counts_aliased_and_from_imported_flags(label, snippet):
    """별칭·from-import 로 우회한 append 재복제도 가드가 센다."""
    assert _append_flag_reference_count(snippet) >= 1, label


def test_mutation_reintroduced_append_write_in_converged_tool_is_red():
    """수렴 도구에 자체 O_APPEND write 를 되살리면 가드가 red 로 잡는다 (감도 실증)."""
    source = (TOOLS / "board.py").read_text(encoding="utf-8")
    mutated = source.replace(
        "    file_lock.append_atomic(\n        af,",
        "    _fd = os.open(str(af), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)\n"
        "    os.close(_fd)\n"
        "    file_lock.append_atomic(\n        af,",
        1,
    )
    assert mutated != source, "변이 앵커 소실"
    assert _append_flag_reference_count(source) == 0, "board.py 는 수렴 상태여야 한다"
    assert _append_flag_reference_count(mutated) > 0


def test_append_guard_ignores_flag_names_in_comments_and_strings():
    """주석·문자열의 O_APPEND 언급은 세지 않는다(AST 판정·문서화 자유)."""
    prose = (
        '"""append 는 os.O_APPEND 단일 write 다."""\n'
        "# os.open(path, os.O_WRONLY | os.O_APPEND, 0o644)\n"
        'DOC = "O_APPEND"\n'
    )
    assert _append_flag_reference_count(prose) == 0


@pytest.mark.parametrize(
    ("label", "snippet"),
    (
        ("module-alias", "import fcntl as _lock_mod\n_lock_mod.flock(fd, 2)\n"),
        ("from-import", "from fcntl import flock\nflock(fd, 2)\n"),
        ("from-import-alias", "from msvcrt import locking as _take\n_take(fd, 1, 1)\n"),
        ("plain", "import msvcrt\nmsvcrt.locking(fd, 1, 1)\n"),
    ),
)
def test_guard_counts_aliased_and_from_imported_lock_calls(label, snippet):
    """별칭·from-import 로 우회한 재복제도 가드가 센다(이름 그대로 보는 판정의 사각)."""
    assert _lock_call_count(snippet) == 1, label


@pytest.mark.parametrize(
    ("label", "mutate"),
    (
        (
            "module-alias",
            lambda source: source.replace(
                "    with file_lock.exclusive_file_lock(BOARD_LOCK):\n        yield",
                "    import fcntl as _lock_mod\n"
                "    _lock_mod.flock(0, _lock_mod.LOCK_EX)\n"
                "    yield",
                1,
            ),
        ),
        (
            "from-import",
            lambda source: source.replace(
                "    with file_lock.exclusive_file_lock(BOARD_LOCK):\n        yield",
                "    from fcntl import flock\n"
                "    flock(0, 2)\n"
                "    yield",
                1,
            ),
        ),
    ),
)
def test_mutation_reintroduced_lock_branch_in_converged_tool_is_red(label, mutate):
    """수렴 도구에 락 분기를 되살리면(우회 형태 포함) 가드가 red 로 잡는다 (감도 실증)."""
    source = (TOOLS / "board.py").read_text(encoding="utf-8")
    mutated = mutate(source)
    assert mutated != source, f"변이 앵커 소실: {label}"
    assert _lock_call_count(source) == 0, "board.py 는 수렴 상태여야 한다"
    assert _lock_call_count(mutated) > 0, label


def test_guard_ignores_lock_names_in_comments_and_strings():
    """주석·문자열의 프리미티브 언급은 세지 않는다(AST 판정·문서화 자유)."""
    prose = (
        '"""POSIX 는 fcntl.flock, Windows 는 msvcrt.locking 을 쓴다."""\n'
        '# fcntl.flock(fd, fcntl.LOCK_EX)\n'
        'DOC = "msvcrt.locking(fd, msvcrt.LK_LOCK, 1)"\n'
    )
    assert _lock_call_count(prose) == 0


# ── 4. 사본 skew ──────────────────────────────────────────────────────────

def _copy_tools(tmp_path: Path, *names: str) -> Path:
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for name in names:
        shutil.copy2(TOOLS / f"{name}.py", tools / f"{name}.py")
    return tools


def _make_stale(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    stale = source.replace('ENGINE_REV = "', 'ENGINE_REV = "v0.0.0-stale-', 1)
    assert stale != source, path
    path.write_text(stale, encoding="utf-8")


@pytest.mark.parametrize(
    ("consumer", "extra"),
    (
        ("board", ("identity_args", "repo_owned_files", "engine_rev", "console_encoding")),
        ("pm_log", ("identity_args", "repo_owned_files")),
        ("pm_relay", ("repo_owned_files",)),
        ("pm_handoff", ("identity_args", "repo_owned_files", "console_encoding")),
        ("worktree_pool", ("identity_args", "repo_owned_files", "console_encoding")),
        ("external_review", ("repo_owned_files", "console_encoding")),
    ),
)
def test_stale_seam_copy_is_reported_as_marked_skew(tmp_path, consumer, extra):
    """stale `file_lock.py` 사본은 조용한 오작동이 아니라 marked skew 로 표출된다."""
    tools = _copy_tools(tmp_path, consumer, "file_lock", *extra)
    _make_stale(tools / "file_lock.py")

    with pytest.raises(RuntimeError) as exc:
        module = _load(tools / f"{consumer}.py", f"{consumer}_stale_seam")
        module._load_file_lock()

    assert getattr(exc.value, "_engine_rev_skew", False) is True
    assert "file_lock.py" in str(exc.value)


@pytest.mark.parametrize(
    ("consumer", "extra"),
    (
        ("board", ("identity_args", "repo_owned_files", "engine_rev", "console_encoding")),
        ("pm_log", ("identity_args", "repo_owned_files")),
        ("pm_relay", ("repo_owned_files",)),
        ("pm_handoff", ("identity_args", "repo_owned_files", "console_encoding")),
        ("worktree_pool", ("identity_args", "repo_owned_files", "console_encoding")),
        ("external_review", ("repo_owned_files", "console_encoding")),
    ),
)
def test_missing_seam_copy_is_translated_like_a_stale_copy(tmp_path, consumer, extra):
    """seam **부재**도 raw FileNotFoundError 가 아니라 복구 안내가 붙은 marked skew 다.

    부재의 원인(부분/수동 복사)과 해소(pm-update 재동기)가 stale 사본과 같으므로 진단도 같은
    등급으로 준다 — raw traceback 은 "무엇을 해야 하나"를 안 알려주고, unmarked 예외는
    fail-soft 로더가 조용히 None 으로 삼킨다.
    """
    tools = _copy_tools(tmp_path, consumer, *extra)      # file_lock.py 를 일부러 빼고 복사
    assert not (tools / "file_lock.py").exists()

    with pytest.raises(RuntimeError) as exc:
        module = _load(tools / f"{consumer}.py", f"{consumer}_missing_seam")
        module._load_file_lock()

    message = str(exc.value)
    assert getattr(exc.value, "_engine_rev_skew", False) is True
    assert "file_lock.py" in message and "pm-update" in message
    assert not isinstance(exc.value, FileNotFoundError)


def test_missing_seam_diagnosis_is_absent_when_the_check_is_removed(tmp_path):
    """가드 감도 — 부재 선-검사를 지우면 raw FileNotFoundError 로 되돌아간다.

    번역이 실제로 그 검사에서 나오는지(우연한 다른 경로가 아니라) 변이로 실증한다.
    """
    tools = _copy_tools(tmp_path, "pm_log", "identity_args", "repo_owned_files")
    source = (tools / "pm_log.py").read_text(encoding="utf-8")
    mutated = source.replace(
        '    _require_engine_sibling(lock_path, "file_lock.py")\n', "", 1
    )
    assert mutated != source, "변이 앵커 소실"
    (tools / "pm_log.py").write_text(mutated, encoding="utf-8")

    module = _load(tools / "pm_log.py", "pm_log_missing_seam_unguarded")
    with pytest.raises(FileNotFoundError):
        module._load_file_lock()
