"""공용 동시-쓰기 파일 프리미티브 seam 회귀 (T-0561·T-0565).

`file_lock.py` 가 두 프리미티브를 소유한다 — **배타 파일락**(board `board.lock`·
`board-git.lock` / pm_log `log.lock` / pm_relay raw 장부 `<ledger>.lock` / pm_handoff
`dashboard.lock` / worktree_pool `worktree-leases.lock` / additional_reviewer 라운드 장부
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
import errno
import importlib.util
import multiprocessing as mp
import os
import re
import shutil
import stat
import sys
import time
import types
from pathlib import Path

import pytest

from _win_skip import posix_mode_supported

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
FILE_LOCK_PY = TOOLS / "file_lock.py"

SYNC_TIMEOUT = 120

# 아직 공용 seam 으로 수렴하지 않은 사본의 등재부 — **현재 0건**(전 도구 수렴 완결: board·
# pm_log·pm_relay·pm_handoff·worktree_pool·additional_reviewer). 비어 있는 상태가 정상이고,
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


# 원자 교체 프리미티브 — 이 이름을 *호출*하는 모듈이 곧 교체 사본 보유자다(T-0729).
_REPLACE_PRIMITIVE = "replace"

# `os.replace` → `file_lock.atomic_replace` 로 전환한 도구들(19지점·9파일). 이 목록은 가드의
# 판정 대상이 아니라 **위임이 실제로 있는지**를 보는 소비 확인용이다 — 재복제 판정 자체는
# `tools/*.py` 전수 스캔이라 사람이 목록을 관리하지 않는다.
_ATOMIC_REPLACE_CONSUMERS = (
    "board.py", "delegate_channel_guard.py", "pm_handoff.py", "pm_import.py",
    "pm_log.py", "pm_relay.py", "pm_update.py", "review_rounds.py",
    "worktree_pool.py",
)


# **등재된 예외** — 원자 교체 seam 을 설치·복구하거나 파일을 보존 이주하는 경로.
# `(모듈, 함수)` → 사유.
# seam 을 설치하는 쓰기가 그 seam 에 의존할 수는 없다(어떤 설계로도 없앨 수 없는 성질). 두 곳은
# `atomic_replace` 가 있으면 그것을 쓰고, 없거나 로드가 실패할 때만 loud 강등한다. 등재 밖 직접
# 호출도, 사유가 빈 등재도 red 다 — 예외가 조용히 늘어나지 못하게 한다([[T-0729]] §결정).
ATOMIC_REPLACE_BOOTSTRAP_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("pm_update.py", "_atomic_replace_or_degrade"): (
        "`_predeploy_central_loader` 가 중앙 로더를 내려놓는 부트스트랩 쓰기가 이 함수를 지난다 — "
        "그 시점 목적지 트리에는 file_lock.py 가 아직 없거나 손상일 수 있고, 여기서 올리면 중단된 "
        "업데이트를 채택자가 스스로 못 고친다"
    ),
    ("pm_update.py", "retire_manifest_paths"): (
        "manifest 퇴역 파일의 원본 bytes/mode를 백업 경로로 그대로 옮겨야 하며, 전용 락 안의 "
        "동일 파일시스템 rename 자체가 계약이라 복사 후 교체 seam으로 바꿀 수 없다"
    ),
    ("pm_import.py", "_atomic_replace_conf"): (
        "복구 채널의 conf writer — 구세대·손상 사본에서도 키 기록이 성립한다는 기존 보장"
        "(lockless recovery 계약)을 형제 의존으로 깨지 않는다"
    ),
}

# 공용 seam 안에서 프리미티브를 직접 부르는 **유일한** 자리 (POSIX 분기).
_SEAM_REPLACE_SITE = ("file_lock.py", "atomic_replace")

# seam 함수 이름 — 위임 확인이 보는 유일한 토큰.
_SEAM_REPLACE_NAME = "atomic_replace"


def _os_replace_call_sites(source: str) -> list[tuple[str, int]]:
    """소스 한 벌의 `os.replace(...)` 호출 위치 `(감싼 함수명, 줄번호)` 목록.

    호출 수만 세면 "어느 함수가 예외인가" 를 등재부로 고정할 수 없다 — 가드가 곧 목록이므로
    귀속까지 기계가 판정한다. 감싼 함수는 **가장 안쪽** 정의를 쓴다(중첩 정의도 그 자리로 귀속).
    """
    tree = ast.parse(source)
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        name = node.name if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing.get(node, "")
        for child in ast.iter_child_nodes(node):
            enclosing[child] = name
    # 바인딩 해소는 모듈당 **한 번**이다. 호출마다 다시 훑으면 board.py 규모(1.5만 줄)에서
    # 스캔이 호출수×노드수로 불어나 가드가 사실상 멈춘다(실측: 이 파일 회귀 996초 → 3초).
    bindings = _os_replace_bindings(tree)
    return [
        (enclosing.get(node, ""), node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_os_replace_call(node, bindings)
    ]


def _os_replace_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """import 로 묶인 이름을 해소한다 → (`os` 별칭 집합, `replace` 직접-이름 집합)."""
    os_aliases = {"os"}
    direct_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_aliases.update(
                alias.asname or alias.name
                for alias in node.names if alias.name == "os"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            direct_names.update(
                alias.asname or alias.name
                for alias in node.names if alias.name == _REPLACE_PRIMITIVE
            )
    return os_aliases, direct_names


def _is_os_replace_call(node: ast.Call, bindings: tuple[set[str], set[str]]) -> bool:
    """이 호출이 `os.replace(...)` 인가 (별칭·from-import 해소·동명 메서드 제외).

    바인딩은 호출부가 **모듈당 한 번** 해소해 넘긴다(`_os_replace_bindings`).
    """
    os_aliases, direct_names = bindings
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == _REPLACE_PRIMITIVE
        and isinstance(func.value, ast.Name)
        and func.value.id in os_aliases
    ):
        return True
    return isinstance(func, ast.Name) and func.id in direct_names


def _os_replace_call_count(source: str) -> int:
    """소스 한 벌의 `os.replace(...)` 호출 수 (별칭·from-import 해소·주석/문자열 제외).

    `_lock_call_count` 와 같은 규칙이다 — 이름 그대로만 보면 `import os as _sys_os` /
    `from os import replace` 형태의 재복제가 가드를 그냥 통과한다. 동명 메서드
    (`text.replace(...)`)는 수신자가 `os` 별칭이 아니므로 세지 않는다.
    """
    return len(_os_replace_call_sites(source))


def _modules_with_os_replace_calls() -> dict[str, int]:
    """tools/ 에서 원자 교체 프리미티브를 *직접* 호출하는 모듈 → 호출 수."""
    found = {
        path.name: _os_replace_call_count(path.read_text(encoding="utf-8"))
        for path in sorted(TOOLS.glob("*.py"))
    }
    return {name: calls for name, calls in found.items() if calls}


def _os_replace_sites_by_module() -> dict[str, list[tuple[str, int]]]:
    """tools/ 전수 → 모듈별 `os.replace` 호출 위치 `(감싼 함수, 줄번호)`."""
    found = {
        path.name: _os_replace_call_sites(path.read_text(encoding="utf-8"))
        for path in sorted(TOOLS.glob("*.py"))
    }
    return {name: sites for name, sites in found.items() if sites}


def _delegates_to_atomic_replace(source: str) -> bool:
    """이 소스가 공용 seam 의 원자 교체에 위임하는가 (AST 판정).

    문자열 포함으로 보면 등재된 예외 두 곳을 놓친다 — 그 둘은 형제 사본이 구세대일 수 있어
    `getattr(seam, "atomic_replace", None)` 으로 **있는지 물어본 뒤** 부르므로 소스에
    `atomic_replace(` 형태가 나타나지 않는다. 두 표기를 모두 위임으로 센다.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == _SEAM_REPLACE_NAME:
            return True
        if (
            isinstance(func, ast.Name) and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == _SEAM_REPLACE_NAME
        ):
            return True
    return False


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


def _worker_hold_lock(
    module_path: str, lock_path: str, acquired, report_path: str,
) -> None:
    """락을 잡은 채 멈춘다(부모가 terminate) — 크래시-시-자동해제 검증용.

    로드·획득이 실패하면 사유를 `report_path` 에 남긴다 — 그냥 죽으면 부모는 `wait()` 타임아웃
    (False) 만 보고 *왜* 못 잡았는지 한 줄도 읽지 못한다. 판정 불능은 통과가 아니라 진단
    실패다([[guard-must-cover-its-own-surface]]).
    """
    try:
        spec = importlib.util.spec_from_file_location("file_lock_worker", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cm = module.exclusive_file_lock(Path(lock_path))
        cm.__enter__()
    except BaseException as exc:  # noqa: BLE001 — 사유 보고 후 그대로 죽는다.
        import traceback
        Path(report_path).write_text(
            f"{exc!r}\n{traceback.format_exc()}", encoding="utf-8", newline="\n")
        raise
    acquired.set()
    time.sleep(3600)


def _child_failure_report(report_path: Path, child) -> str:
    """자식이 남긴 실패 사유 + 프로세스 상태 (진단 문구용)."""
    reason = (
        report_path.read_text(encoding="utf-8").strip()
        if report_path.exists() else "(자식이 사유를 남기지 않음)"
    )
    return f"exitcode={child.exitcode} alive={child.is_alive()} · 자식 보고: {reason}"


def test_second_process_is_excluded_and_crash_releases_the_lock(tmp_path):
    """다른 프로세스가 보유 중이면 비차단 획득이 실패하고, 그 프로세스가 죽으면 풀린다."""
    lock_path = tmp_path / "cross-process.lock"
    report_path = tmp_path / "child-failure.txt"
    ctx = mp.get_context("spawn")
    acquired = ctx.Event()
    child = ctx.Process(
        target=_worker_hold_lock,
        args=(str(FILE_LOCK_PY), str(lock_path), acquired, str(report_path)),
    )
    child.start()
    try:
        assert acquired.wait(timeout=SYNC_TIMEOUT), (
            "자식이 락을 획득하지 못함 — " + _child_failure_report(report_path, child)
        )
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
    """RMW 없이 O_APPEND 단일 write — open/write/fsync/close 4콜·권한은 호출자 소유."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        file_lock.os,
        "open",
        lambda path, flags, mode=0: calls.append(("open", path, flags, mode)) or 41,
    )
    monkeypatch.setattr(
        file_lock.os, "write", lambda fd, payload: calls.append(("write", fd, payload))
    )
    monkeypatch.setattr(file_lock.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    monkeypatch.setattr(file_lock.os, "close", lambda fd: calls.append(("close", fd)))

    target = tmp_path / "current.md"
    file_lock.append_atomic(target, "\nentry")
    file_lock.append_atomic(target, "x", mode=0o600)

    assert calls[0][0:2] == ("open", str(target))
    assert calls[0][2] & os.O_APPEND and calls[0][2] & os.O_CREAT
    assert calls[0][2] & os.O_WRONLY == os.O_WRONLY
    assert calls[0][3] == file_lock.DEFAULT_APPEND_MODE == 0o644
    assert calls[1:4] == [
        ("write", 41, b"\nentry"), ("fsync", 41), ("close", 41),
    ]
    assert calls[4][3] == 0o600


def test_append_atomic_syncs_the_writable_descriptor_it_opened(
    file_lock, tmp_path, monkeypatch,
):
    """내구성은 append 가 연 **쓰기** fd 위에서 수행된다 (읽기 전용 재-open sync 아님·T-0716).

    호출부가 append 뒤에 파일을 다시 열어 sync 하면 그 fd 는 읽기 전용이고, Windows 는 그
    fsync 를 `[Errno 9] Bad file descriptor` 로 거부한다.
    """
    opened_flags: dict[int, int] = {}
    synced: list[int] = []
    real_open, real_fsync = os.open, os.fsync

    def _record_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        opened_flags[fd] = flags
        return fd

    def _record_fsync(fd):
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(file_lock.os, "open", _record_open)
    monkeypatch.setattr(file_lock.os, "fsync", _record_fsync)

    target = tmp_path / "areas.md"
    file_lock.append_atomic(target, "line\n")

    assert len(synced) == 1
    assert opened_flags[synced[0]] & os.O_WRONLY == os.O_WRONLY
    assert target.read_text(encoding="utf-8") == "line\n"


def test_append_atomic_propagates_a_failing_sync_and_closes_the_descriptor(
    file_lock, tmp_path, monkeypatch,
):
    """sync 실패는 삼키지 않는다 (내구성의 조용한 소실 금지)·fd 는 닫는다."""
    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(
        file_lock.os, "close", lambda fd: closed.append(fd) or real_close(fd)
    )

    def boom(fd):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(file_lock.os, "fsync", boom)
    with pytest.raises(OSError):
        file_lock.append_atomic(tmp_path / "a.md", "x")
    assert len(closed) == 1


def test_append_atomic_opt_out_skips_the_sync(file_lock, tmp_path, monkeypatch):
    """`fsync=False` 는 sync 만 끄고 write 는 그대로다 (기본값은 sync 수행)."""
    synced: list[int] = []
    monkeypatch.setattr(file_lock.os, "fsync", lambda fd: synced.append(fd))

    target = tmp_path / "a.md"
    file_lock.append_atomic(target, "x", fsync=False)

    assert synced == []
    assert target.read_text(encoding="utf-8") == "x"


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
    ("pm_log.py", "pm_relay.py", "additional_reviewer.py", "pm_update.py", "pm_import.py"),
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
        "additional_reviewer.py",
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
        ("additional_reviewer", ("repo_owned_files", "console_encoding")),
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
        ("additional_reviewer", ("repo_owned_files", "console_encoding")),
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


# ── append 바이트 보존 (플랫폼 줄끝 번역 차단·T-0711) ────────────────────────
# Windows 의 `os.open` 은 텍스트 모드가 기본이라 `os.O_BINARY` 없이 열면 CRT 가 `\n` 을
# `\r\n` 으로 번역한다. board 루트 파일 backfill 은 append 한 바이트를 롤백 때 그대로 되계산해
# 대조하므로, 번역이 끼면 "engine 이 쓴 것"과 "engine 이 썼다고 계산한 것"이 갈려 롤백이
# 제3자 변경으로 보고 잔재를 남긴다(Windows 실측). POSIX 에는 `os.O_BINARY` 자체가 없어 그
# 분기를 **주입해** 태운다.

_INJECTED_O_BINARY = 0x8000


def _record_open_flags(file_lock, monkeypatch) -> list[int]:
    """`file_lock` 이 `os.open` 에 넘긴 flags 를 기록한다 (주입 비트는 실 호출에서 뗀다)."""
    seen: list[int] = []
    real_open = os.open

    def _spy(path, flags, mode=0o777, **kwargs):
        seen.append(flags)
        return real_open(path, flags & ~_INJECTED_O_BINARY, mode, **kwargs)

    monkeypatch.setattr(file_lock.os, "open", _spy)
    return seen


def test_append_atomic_opens_binary_where_the_platform_translates_newlines(
    file_lock, tmp_path, monkeypatch,
):
    """append 는 `os.O_BINARY` 를 얹어 연다 — 그 플랫폼의 텍스트 모드 줄끝 번역을 막는다.

    주입은 **플랫폼 인지형**이다. POSIX 에는 이 상수가 없으므로 상수를 주입해 Windows 분기를
    여기서 태우고(주입 없이는 이 축이 개발기에서 영원히 안 보인다·
    [[guard-must-cover-its-own-surface]]), 상수가 **실재하는** 플랫폼에서는 주입하지 않고 엔진의
    실제 동작을 본다. 거기서 부재/치환을 주입하면 기록 spy 가 실제 `O_BINARY` 비트를 떼어내
    **테스트가 만든** 텍스트 모드 쓰기를 엔진 결함으로 보고한다(T-0724 Windows 실측: 엔진 산출은
    LF·`b"a\\nb\\n"`, 비트를 뗀 `os.write` 만 `b"a\\r\\nb\\r\\n"`).
    """
    native_binary = getattr(os, "O_BINARY", None)
    if native_binary is None:
        monkeypatch.setattr(file_lock.os, "O_BINARY", _INJECTED_O_BINARY, raising=False)
        # 주입이 실제로 걸렸는지 선-단언 — no-op 주입은 아무것도 태우지 않는 초록이다.
        assert getattr(file_lock.os, "O_BINARY", None) == _INJECTED_O_BINARY, \
            "O_BINARY 주입이 걸리지 않았다 — 이 가드는 Windows 분기를 태우지 못한다"
        required = _INJECTED_O_BINARY
        seen = _record_open_flags(file_lock, monkeypatch)  # 주입 비트는 실 호출에서 뗀다
    else:
        # 상수 실재 플랫폼 — 요구 비트를 기록만 하고 flags 는 손대지 않는다(엔진 실동작 판정).
        required = native_binary
        assert required, "이 플랫폼의 `os.O_BINARY` 가 0 이라 비트 단언이 무의미하다"
        seen = []
        real_open = os.open

        def _passthrough_spy(path, flags, mode=0o777, **kwargs):
            seen.append(flags)
            return real_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(file_lock.os, "open", _passthrough_spy)

    target = tmp_path / "current.md"
    file_lock.append_atomic(target, "a\nb\n")

    assert seen and seen[0] & required, \
        f"append 가 바이너리 모드를 요구하지 않음 — 줄끝이 번역될 수 있다: {seen}"
    assert target.read_bytes() == b"a\nb\n"
    # 판정은 byte 그대로다 — 정규화를 끼워 넣으면 번역 자체를 못 본다. CRLF 를 준 append 도
    # 준 bytes 로 남아야 한다(텍스트 모드면 `\r\n` 이 `\r\r\n` 으로 부푼다).
    file_lock.append_atomic(target, "c\r\nd\n")
    assert target.read_bytes() == b"a\nb\nc\r\nd\n"


def test_append_atomic_writes_the_exact_bytes_it_was_given(file_lock, tmp_path):
    """append 는 준 문자열의 bytes 를 그대로 남긴다 (호출부의 바이트 계산과 갈리지 않는다)."""
    target = tmp_path / ".gitattributes"
    target.write_bytes(b"*.md text eol=lf\n")

    file_lock.append_atomic(target, "\n# block\nareas.md merge=union\n")

    assert target.read_bytes() == (
        b"*.md text eol=lf\n\n# block\nareas.md merge=union\n")


# ── 원자 교체 (열린 리더와 공존·T-0729) ─────────────────────────────────────
# 엔진의 원자 쓰기 관용구(임시 파일 → 이름 바꾸기)는 **락 없는 리더도 일관 스냅샷을 본다**는
# 보장을 낸다(board 보드 파일·worktree_pool 리스 장부가 그 성질에 명시적으로 기대 있다).
# `os.replace` 는 POSIX 에서 그 보장을 그대로 내지만 Windows 에서는 대상이 열려 있으면
# `ERROR_ACCESS_DENIED` 로 실패한다(리더가 share-delete 여도 마찬가지·Windows 11 실측). 그래서
# 그 플랫폼만 POSIX 의미 rename(`FileRenameInfoEx`)으로 **같은 의미**를 낸다.
#
# 검증 축은 두 층이다 — 정책층(어떤 권한/플래그로 무엇을 부르고 실패를 어떻게 올리는가)은
# 주입으로 POSIX 개발기에서 그대로 태우고, 원시층(ctypes 커널 호출)은 Windows 실측이 덮는다
# (`WindowsLockApi` 선례와 같은 구조·[[guard-must-cover-its-own-surface]]).

# 대역이 돌려주는 가짜 핸들 — 경로/fd 와 구분되는 값이어야 "핸들을 닫았는가"를 단언할 수 있다.
_FAKE_RENAME_HANDLE = 4242

# 대역이 흉내내는 Win32 에러코드 (winerror.h `ERROR_SHARING_VIOLATION`).
_ERROR_SHARING_VIOLATION = 32


class FakeWindowsRenameApi:
    """`WindowsRenameApi` 대역 — 호출을 기록하고 지정한 Win32 에러코드를 돌려준다.

    실제 커널 동작(리더와의 공존)은 흉내내지 않는다 — 그건 Windows 실측 몫이다. 이 대역이
    고정하는 것은 정책층이 **무엇을 어떤 인자로 부르는가**와 **실패를 어떻게 올리는가** 둘이다.
    """

    def __init__(self, *, rename_error: int = 0, open_error: OSError | None = None) -> None:
        self.rename_error = rename_error
        self.open_error = open_error
        self.open_calls: list[tuple[str, int, int]] = []
        self.rename_calls: list[tuple[int, str, int]] = []
        self.closed: list[int] = []

    def open_handle(self, path: str, access: int, share: int) -> int:
        self.open_calls.append((path, access, share))
        if self.open_error is not None:
            raise self.open_error
        return _FAKE_RENAME_HANDLE

    def rename(self, handle: int, target: str, flags: int) -> int:
        self.rename_calls.append((handle, target, flags))
        return self.rename_error

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)

    def as_named_tuple(self, module):
        return module.WindowsRenameApi(
            self.open_handle, self.rename, self.close_handle)


def _force_windows_replace_branch(module, monkeypatch, api: FakeWindowsRenameApi) -> None:
    """플랫폼 판정과 원시 API 를 Windows 쪽으로 갈아끼운다 (주입 지점 두 곳 모두)."""
    monkeypatch.setattr(module, "windows_replace_platform", lambda: True)
    monkeypatch.setattr(
        module, "_windows_rename_api", lambda: api.as_named_tuple(module))


def _forbid_os_replace(module, monkeypatch) -> list[tuple]:
    """`os.replace` 강등 여부를 감시한다 — 불리면 그 호출을 기록한다(조용한 폴백 탐지)."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        module.os, "replace", lambda src, dst: calls.append((src, dst)))
    return calls


def _pair(root: Path, tag: str, *, old: bytes | None = b"OLD", new: bytes = b"NEW"):
    """(임시 파일, 대상) 한 쌍 — `old=None` 이면 대상 부재(신규 생성) 형상."""
    target = root / f"{tag}.json"
    tmp = root / f"{tag}.json.tmp"
    if old is not None:
        target.write_bytes(old)
    tmp.write_bytes(new)
    return tmp, target


# ── 불변식 1·2·3 (POSIX 에서 실제로 성립하는 것을 실동작으로 고정) ───────────

def test_atomic_replace_leaves_the_new_content_and_removes_the_temporary(
    file_lock, tmp_path,
):
    """교체 후 디스크는 새 내용이고 임시 파일은 사라진다 (중간 상태 없음·불변식 1)."""
    tmp, target = _pair(tmp_path, "ledger")

    file_lock.atomic_replace(tmp, target)

    assert target.read_bytes() == b"NEW"
    assert not tmp.exists(), "임시 파일이 남았다 — 이름 바꾸기가 아니라 복사가 됐다"


def test_atomic_replace_creates_the_target_when_it_is_absent(file_lock, tmp_path):
    """대상이 없으면 새로 만든다 (`os.replace` 와 같은 경계 동작)."""
    tmp, target = _pair(tmp_path, "fresh", old=None)

    file_lock.atomic_replace(tmp, target)

    assert target.read_bytes() == b"NEW"


def test_atomic_replace_accepts_paths_and_strings_like_the_primitive(
    file_lock, tmp_path,
):
    """인자는 `Path`·`str` 둘 다 받는다 (호출부 19지점이 섞여 쓰던 표기 보존)."""
    tmp, target = _pair(tmp_path, "strings")

    file_lock.atomic_replace(str(tmp), str(target))

    assert target.read_bytes() == b"NEW"


def test_atomic_replace_succeeds_while_a_reader_holds_the_target_open(
    file_lock, tmp_path,
):
    """리더가 열고 있어도 교체가 성공하고, 그 핸들은 옛 내용을 끝까지 읽는다 (불변식 2·3).

    이게 이 seam 이 지키는 의미 자체다 — Windows 의 `os.replace` 는 바로 이 형상에서
    WinError 5 로 실패한다(그래서 그 플랫폼만 POSIX 의미 rename 을 쓴다).

    리더는 **엔진 읽기 seam** 으로 연다 — 쓰기 seam 과 읽기 seam 은 세트다. 내장 `open` 으로 열면
    Windows 에서 공유 삭제가 빠져 교체가 WinError 32 로 막히고(실측표), Linux 에서만 통과하는 잘못된
    전제가 된다.
    """
    tmp, target = _pair(tmp_path, "held", old=b"OLD-CONTENT", new=b"NEW-CONTENT")

    with file_lock.open_shared(target, binary=True) as reader:
        head = reader.read(3)
        file_lock.atomic_replace(tmp, target)
        seen = head + reader.read()

    assert seen == b"OLD-CONTENT", "교체가 열린 핸들의 내용을 바꿨다"
    assert target.read_bytes() == b"NEW-CONTENT"


def _worker_hold_reader(target_path: str, opened, release, seen_path: str) -> None:
    """대상 파일을 **엔진 읽기 seam** 으로 연 채 부모의 교체를 기다렸다가 읽은 내용을 남긴다.

    타 프로세스 리더는 seam(`open_shared`)으로 열어야 한다 — 그것이 T-0729 가 읽기 축을 전환한 이유다.
    내장 `open` 으로 열면 Windows 에서 공유 삭제가 빠져 교체가 WinError 32 로 막히고(실측표), Linux 에선
    차이가 안 보여 이 회귀가 잘못된 전제로 통과한다. spawn 자식이라 모듈을 다시 로드한다.
    """
    lock = _load(FILE_LOCK_PY, "file_lock_child_reader")
    with lock.open_shared(Path(target_path), binary=True) as reader:
        head = reader.read(3)
        opened.set()
        release.wait(timeout=SYNC_TIMEOUT)
        Path(seen_path).write_bytes(head + reader.read())


def test_atomic_replace_succeeds_while_another_process_holds_the_target_open(
    file_lock, tmp_path,
):
    """타 프로세스 리더가 열고 있어도 교체가 성립한다 (불변식 3 의 프로세스 간 형상)."""
    tmp, target = _pair(tmp_path, "cross", old=b"OLD-CONTENT", new=b"NEW-CONTENT")
    seen_path = tmp_path / "child-seen.bin"
    ctx = mp.get_context("spawn")
    opened, release = ctx.Event(), ctx.Event()
    child = ctx.Process(
        target=_worker_hold_reader,
        args=(str(target), opened, release, str(seen_path)),
    )
    child.start()
    try:
        assert opened.wait(timeout=SYNC_TIMEOUT), "자식이 대상을 열지 못했다"

        file_lock.atomic_replace(tmp, target)

        release.set()
        child.join(timeout=SYNC_TIMEOUT)
        assert child.exitcode == 0, f"자식 비정상 종료: exitcode={child.exitcode}"
        assert seen_path.read_bytes() == b"OLD-CONTENT", \
            "타 프로세스 핸들이 교체 뒤 내용을 봤다"
        assert target.read_bytes() == b"NEW-CONTENT"
    finally:
        release.set()
        if child.is_alive():
            child.terminate()
        child.join(timeout=10)


# ── 플랫폼 분기: POSIX 는 프리미티브 그대로 ──────────────────────────────────

def test_this_development_platform_takes_the_posix_branch(file_lock):
    """주입 전 baseline — 플랫폼 판정이 실행 OS 와 일치한다.

    POSIX 개발기에서는 False(=Windows 분기는 주입으로만 실행된다) 라야 아래 "Windows 분기를 태웠다"
    케이스들이 사실은 POSIX 분기를 태우고도 통과하는 일이 없다. Windows VM 에서는 True 라야 실 API
    분기가 실제로 도는 것이다 — 어느 쪽이든 OS 를 잘못 읽으면 red.
    """
    assert file_lock.windows_replace_platform() is (os.name == "nt")


def test_posix_branch_delegates_to_the_rename_primitive_unchanged(
    file_lock, tmp_path, monkeypatch,
):
    """POSIX 는 `os.replace` 를 인자 그대로 부른다 (의미를 덧입히지 않는다).

    분기를 명시 주입해 POSIX 경로를 태운다 — Windows VM 에서도 같은 것을 재기 위해서다(Windows 분기를
    Linux 에서 주입으로 태우는 것과 대칭). 실행 OS 를 가정하지 않는다.
    """
    monkeypatch.setattr(file_lock, "windows_replace_platform", lambda: False)
    calls = _forbid_os_replace(file_lock, monkeypatch)
    tmp, target = _pair(tmp_path, "posix")

    file_lock.atomic_replace(tmp, target)

    assert calls == [(str(tmp), str(target))]


# ── 플랫폼 분기: Windows 정책층을 주입으로 태운다 ────────────────────────────

def test_windows_replace_injection_actually_reaches_the_primitive(
    file_lock, tmp_path, monkeypatch,
):
    """주입 선-단언 — 갈아끼운 대역이 실제 교체 경로에서 불린다(no-op 주입 차단)."""
    api = FakeWindowsRenameApi()
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    forbidden = _forbid_os_replace(file_lock, monkeypatch)
    tmp, target = _pair(tmp_path, "injected")

    file_lock.atomic_replace(tmp, target)

    assert api.rename_calls, "주입이 no-op — Windows 분기가 실행되지 않았다"
    assert not forbidden, "Windows 분기인데 `os.replace` 로 내려앉았다"


def test_windows_branch_opens_the_source_with_delete_access_and_full_sharing(
    file_lock, tmp_path, monkeypatch,
):
    """원본은 `DELETE|SYNCHRONIZE` 권한 + 읽기/쓰기/삭제 공유로 연다.

    rename 은 `DELETE` 권한을 요구하고, 공유 셋을 다 열지 않으면 그 순간 남이 연 핸들과
    공존하지 못해 이 seam 이 고치려는 실패를 스스로 재현한다.
    """
    api = FakeWindowsRenameApi()
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    tmp, target = _pair(tmp_path, "access")

    file_lock.atomic_replace(tmp, target)

    assert api.open_calls, "주입이 no-op — 접근/공유 모드를 판정할 수 없다"
    path, access, share = api.open_calls[0]
    assert path == str(tmp), "원본이 아닌 대상을 열었다"
    assert access == file_lock.ATOMIC_REPLACE_ACCESS
    assert access & file_lock.DELETE_ACCESS, "rename 에 필요한 DELETE 권한 없음"
    assert share == file_lock.SHARE_ALL_MODES
    for mode in (
        file_lock.FILE_SHARE_READ, file_lock.FILE_SHARE_WRITE,
        file_lock.FILE_SHARE_DELETE,
    ):
        assert share & mode, f"공유 모드 누락: {mode:#x}"


def test_windows_branch_renames_with_posix_semantics_and_replace_if_exists(
    file_lock, tmp_path, monkeypatch,
):
    """rename 플래그 = 기존 대상 교체 + **POSIX 의미**.

    POSIX 의미 플래그가 빠지면 열린 리더와 공존하지 못해(WinError) 이 티켓이 닫는 결함이
    그대로 남고, REPLACE_IF_EXISTS 가 빠지면 기존 대상이 있을 때 교체 자체가 실패한다.
    """
    api = FakeWindowsRenameApi()
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    tmp, target = _pair(tmp_path, "flags")

    file_lock.atomic_replace(tmp, target)

    assert api.rename_calls, "주입이 no-op — 플래그를 판정할 수 없다"
    handle, _target, flags = api.rename_calls[0]
    assert handle == _FAKE_RENAME_HANDLE, "열어 둔 핸들이 아닌 값을 rename 에 넘겼다"
    assert flags == file_lock.ATOMIC_REPLACE_RENAME_FLAGS
    assert flags & file_lock.FILE_RENAME_FLAG_POSIX_SEMANTICS, \
        "POSIX 의미 없음 — 열린 리더와 공존하지 못한다"
    assert flags & file_lock.FILE_RENAME_FLAG_REPLACE_IF_EXISTS, \
        "기존 대상 교체 플래그 없음"
    assert file_lock.FILE_RENAME_INFO_EX_CLASS == 22, \
        "구 FileRenameInfo(3) 는 POSIX 의미 플래그를 받지 않는다"


def test_windows_branch_passes_a_fully_qualified_target(
    file_lock, tmp_path, monkeypatch,
):
    """대상은 완전 수식 경로로 넘긴다 — `RootDirectory=None` 이면 상대 경로는 해소되지 않는다."""
    api = FakeWindowsRenameApi()
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    tmp, target = _pair(tmp_path, "relative")
    monkeypatch.chdir(tmp_path)

    file_lock.atomic_replace(tmp.name, target.name)

    assert api.rename_calls, "주입이 no-op — 대상 표기를 판정할 수 없다"
    _handle, passed, _flags = api.rename_calls[0]
    assert os.path.isabs(passed), f"상대 경로를 그대로 넘겼다: {passed!r}"
    assert Path(passed) == target


def test_windows_branch_closes_the_handle_on_success_and_on_failure(
    file_lock, tmp_path, monkeypatch,
):
    """열어 둔 핸들은 성공 경로와 실패 경로 모두에서 닫는다 (핸들 누수 0)."""
    api = FakeWindowsRenameApi()
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    tmp, target = _pair(tmp_path, "ok")
    file_lock.atomic_replace(tmp, target)
    assert api.closed == [_FAKE_RENAME_HANDLE], "성공 경로에서 핸들이 샜다"

    failing = FakeWindowsRenameApi(rename_error=_ERROR_SHARING_VIOLATION)
    _force_windows_replace_branch(file_lock, monkeypatch, failing)
    tmp, target = _pair(tmp_path, "boom")
    with pytest.raises(file_lock.AtomicReplaceError):
        file_lock.atomic_replace(tmp, target)
    assert failing.closed == [_FAKE_RENAME_HANDLE], "실패 경로에서 핸들이 샜다"


# ── 불변식 4: 못 쓰는 수단은 조용히 강등하지 않는다 ──────────────────────────

def test_windows_rename_failure_is_loud_instead_of_falling_back(
    file_lock, tmp_path, monkeypatch,
):
    """rename 이 Win32 에러로 실패하면 `os.replace` 로 내려앉지 않고 사유와 함께 올린다.

    강등하면 이 seam 이 닫으려던 결함(열린 리더에서 WinError 5)이 그대로 되살아나면서
    호출부에는 성공으로 보인다([[no-green-by-disabling]]).
    """
    api = FakeWindowsRenameApi(rename_error=_ERROR_SHARING_VIOLATION)
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    forbidden = _forbid_os_replace(file_lock, monkeypatch)
    tmp, target = _pair(tmp_path, "denied")

    with pytest.raises(file_lock.AtomicReplaceError) as raised:
        file_lock.atomic_replace(tmp, target)

    assert api.rename_calls, "주입이 no-op — 실패 번역을 판정할 수 없다"
    assert not forbidden, "실패를 `os.replace` 로 조용히 대체했다"
    assert str(_ERROR_SHARING_VIOLATION) in str(raised.value), "진단에 WinError 가 없다"
    assert "FileRenameInfoEx" in str(raised.value)
    assert target.read_bytes() == b"OLD", "실패했는데 대상이 바뀌었다"


def test_unsupported_generation_fails_with_its_reason(
    file_lock, tmp_path, monkeypatch,
):
    """수단 미지원 세대(`ERROR_INVALID_PARAMETER`)는 **사유**를 담아 실패한다 (불변식 4).

    Windows 10 1709 미만은 POSIX 의미 rename 을 모른다. 그 환경에서 `os.replace` 로 내려앉는
    것은 "동작하는 것처럼 보이지만 리더가 있으면 실패한다"는 원래 상태로 되돌리는 것이다.
    """
    api = FakeWindowsRenameApi(rename_error=file_lock.ERROR_INVALID_PARAMETER)
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    forbidden = _forbid_os_replace(file_lock, monkeypatch)
    tmp, target = _pair(tmp_path, "legacy")

    with pytest.raises(file_lock.AtomicReplaceError) as raised:
        file_lock.atomic_replace(tmp, target)

    assert api.rename_calls, "주입이 no-op — 미지원 진단을 판정할 수 없다"
    assert not forbidden, "미지원 환경을 `os.replace` 로 조용히 대체했다"
    message = str(raised.value)
    assert "1709" in message, "미지원 세대라는 사유가 진단에 없다"
    assert str(file_lock.ERROR_INVALID_PARAMETER) in message


def test_source_open_failure_propagates_without_attempting_a_rename(
    file_lock, tmp_path, monkeypatch,
):
    """원본을 열지 못하면 그대로 올린다 — 교체를 시작하지도 않는다(대상 불변)."""
    api = FakeWindowsRenameApi(open_error=PermissionError(13, "denied"))
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    forbidden = _forbid_os_replace(file_lock, monkeypatch)
    tmp, target = _pair(tmp_path, "unopenable")

    with pytest.raises(PermissionError):
        file_lock.atomic_replace(tmp, target)

    assert api.open_calls, "주입이 no-op — 열기 실패 경로를 판정할 수 없다"
    assert not api.rename_calls, "열지도 못했는데 rename 을 시도했다"
    assert not forbidden, "열기 실패를 `os.replace` 로 조용히 대체했다"
    assert target.read_bytes() == b"OLD"


def test_atomic_replace_error_is_an_oserror_subclass(file_lock):
    """실패 타입은 `OSError` 계열 — 기존 호출부의 IO 실패 처리 경로를 그대로 탄다."""
    assert issubclass(file_lock.AtomicReplaceError, OSError)


@pytest.mark.parametrize(
    ("winerror", "expected_errno"),
    (
        (_ERROR_SHARING_VIOLATION, errno.EACCES),   # 32 — 공유 위반(일반 리더 보유)
        (5, errno.EACCES),                          # 5  — 접근 거부
        (87, errno.EINVAL),                         # 87 — POSIX 의미 rename 미지원 세대
    ),
)
def test_atomic_replace_error_carries_the_win32_code_as_attributes(
    file_lock, tmp_path, monkeypatch, winerror, expected_errno,
):
    """실패는 Win32 코드를 **속성으로도** 싣는다 (`winerror`·`errno`).

    코드가 메시지에만 있으면 호출부와 진단 도구가 문자열을 파싱해야 하고, 원시
    `PermissionError` 를 잡던 자리(`except OSError as exc: exc.winerror`)가 조용히 `None` 을
    본다 — Windows 실측에서 대조군 두 케이스가 그 이유로 갈렸다. 종전 실패 표면과 같은 모양을
    낸다.
    """
    api = FakeWindowsRenameApi(rename_error=winerror)
    _force_windows_replace_branch(file_lock, monkeypatch, api)
    tmp, target = _pair(tmp_path, f"code{winerror}")

    with pytest.raises(file_lock.AtomicReplaceError) as raised:
        file_lock.atomic_replace(tmp, target)

    assert api.rename_calls, "주입이 no-op — 실패 속성을 판정할 수 없다"
    assert raised.value.winerror == winerror
    assert raised.value.errno == expected_errno
    assert str(winerror) in str(raised.value), "메시지의 Win32 코드가 사라졌다"


# ── 감도 (변이가 실제로 red 가 되는가) ───────────────────────────────────────

def _mutated_seam(tmp_path: Path, old: str, new: str, name: str):
    """`file_lock.py` 한 곳을 변이시킨 사본을 로드한다 (변이 앵커 소실은 즉시 실패)."""
    source = FILE_LOCK_PY.read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source, "변이 앵커 소실"
    path = tmp_path / f"{name}.py"
    path.write_text(mutated, encoding="utf-8", newline="\n")
    return _load(path, f"file_lock_{name}_under_test")


def test_dropping_posix_semantics_is_caught_by_the_flag_guard(tmp_path, monkeypatch):
    """POSIX 의미 플래그를 떼면 플래그 가드가 red 로 잡는다 (가드 감도 실증)."""
    module = _mutated_seam(
        tmp_path,
        "ATOMIC_REPLACE_RENAME_FLAGS = (\n"
        "    FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS\n"
        ")",
        "ATOMIC_REPLACE_RENAME_FLAGS = FILE_RENAME_FLAG_REPLACE_IF_EXISTS",
        "no_posix_semantics",
    )
    api = FakeWindowsRenameApi()
    _force_windows_replace_branch(module, monkeypatch, api)
    tmp, target = _pair(tmp_path, "mutated_flags")

    module.atomic_replace(tmp, target)

    assert api.rename_calls, "주입이 no-op — 변이를 판정할 수 없다"
    assert not api.rename_calls[0][2] & module.FILE_RENAME_FLAG_POSIX_SEMANTICS, \
        "변이가 실제로 POSIX 의미를 떼지 않았다"


def test_a_silent_fallback_mutation_is_caught_by_the_loud_failure_guard(
    tmp_path, monkeypatch,
):
    """실패를 `os.replace` 강등으로 바꾼 사본은 fail-loud 가드에 걸린다 (가드 감도 실증)."""
    module = _mutated_seam(
        tmp_path,
        "    if error:\n        raise _atomic_replace_failure(source, absolute_target, error)",
        "    if error:\n        os.replace(source, target)",
        "silent_fallback",
    )
    api = FakeWindowsRenameApi(rename_error=_ERROR_SHARING_VIOLATION)
    _force_windows_replace_branch(module, monkeypatch, api)
    tmp, target = _pair(tmp_path, "mutated_fallback")

    module.atomic_replace(tmp, target)      # 변이본은 조용히 성공한다(그게 결함이다)

    assert api.rename_calls, "주입이 no-op — 변이를 판정할 수 없다"
    assert target.read_bytes() == b"NEW", "변이가 실제로 강등 폴백이 아니다"


# ── 재복제 차단 (원자 교체 프리미티브는 seam 한 곳뿐) ────────────────────────

def test_os_replace_lives_only_in_the_seam_and_the_registered_exceptions():
    """`os.replace` 직접 호출 = 공용 seam 한 곳 + **등재된 부트스트랩 예외 두 곳**뿐이다.

    목록을 사람이 관리하지 않는다 — `tools/*.py` 전수를 AST 로 훑어 호출을 **감싼 함수까지**
    귀속시키고, 등재부 밖에서 되살아나면 그 자리가 곧 red 다. 등재부가 사라지거나 늘어나도
    red 이므로 예외는 조용히 증식하지 못한다([[T-0729]] §결정).
    """
    measured = {
        (module, function)
        for module, sites in _os_replace_sites_by_module().items()
        for function, _line in sites
    }
    expected = {_SEAM_REPLACE_SITE} | set(ATOMIC_REPLACE_BOOTSTRAP_EXCEPTIONS)
    assert measured == expected, (
        f"등재 밖 직접 호출: {sorted(measured - expected)} / "
        f"사라진 등재(목록 정리 필요): {sorted(expected - measured)}"
    )
    per_site = [
        (module, function)
        for module, sites in _os_replace_sites_by_module().items()
        for function, _line in sites
    ]
    assert len(per_site) == len(measured), f"한 함수 안 중복 호출: {per_site}"


def test_every_registered_exception_carries_a_reason():
    """등재된 예외는 사유가 있어야 한다 — 빈 사유는 등재가 아니라 구멍이다."""
    assert ATOMIC_REPLACE_BOOTSTRAP_EXCEPTIONS, "등재부가 비었다(예외 0이면 가드가 더 좁아야 한다)"
    for site, reason in ATOMIC_REPLACE_BOOTSTRAP_EXCEPTIONS.items():
        assert reason.strip(), f"사유 없는 등재: {site}"


def test_atomic_replace_consumers_delegate_to_the_seam():
    """전환 대상 9개 도구는 seam 위임을 쓴다 (등재 예외 밖 직접 호출 0)."""
    exempt_modules = {module for module, _function in ATOMIC_REPLACE_BOOTSTRAP_EXCEPTIONS}
    for tool in _ATOMIC_REPLACE_CONSUMERS:
        source = (TOOLS / tool).read_text(encoding="utf-8")
        assert _delegates_to_atomic_replace(source), f"{tool}: seam 위임이 없다"
        allowed = sum(1 for module, _function in ATOMIC_REPLACE_BOOTSTRAP_EXCEPTIONS
                      if module == tool)
        assert _os_replace_call_count(source) == allowed, (
            f"{tool}: `os.replace` 직접 호출이 등재({allowed}개)와 다르다")


@pytest.mark.parametrize(
    ("label", "snippet"),
    (
        ("plain", "import os\nos.replace(tmp, path)\n"),
        ("module-alias", "import os as _sys_os\n_sys_os.replace(tmp, path)\n"),
        ("from-import", "from os import replace\nreplace(tmp, path)\n"),
        ("from-import-alias", "from os import replace as _mv\n_mv(tmp, path)\n"),
    ),
)
def test_os_replace_guard_counts_aliased_and_from_imported_calls(label, snippet):
    """별칭·from-import 로 우회한 재복제도 가드가 센다 (이름 그대로 보는 판정의 사각)."""
    assert _os_replace_call_count(snippet) == 1, label


def test_os_replace_guard_ignores_the_name_in_comments_and_strings():
    """주석·문자열의 `os.replace` 언급은 세지 않는다 (문서화 자유·AST 판정).

    seam 전환으로 도구들의 *산문*에는 옛 이름이 남을 수 있고(왜 그 관용구인지 설명), 그건
    재복제가 아니다. `str.replace` 같은 동명 메서드도 대상이 아니다.
    """
    prose = (
        '"""temp + `os.replace` 로 원자 교체한다."""\n'
        "# os.replace(str(tmp), str(path))\n"
        'DOC = "os.replace"\n'
        'text = body.replace("a", "b")\n'
    )
    assert _os_replace_call_count(prose) == 0


def test_a_new_unregistered_exception_is_red():
    """등재 없는 새 예외는 가드가 red 로 잡는다 (등재부가 곧 상한·감도 실증)."""
    source = (TOOLS / "pm_log.py").read_text(encoding="utf-8")
    mutated = source.replace(
        "        _load_file_lock().atomic_replace(tmp, path)",
        "        os.replace(str(tmp), str(path))",
        1,
    )
    assert mutated != source, "변이 앵커 소실"
    sites = _os_replace_call_sites(mutated)
    assert sites, "변이가 실제로 직접 호출을 만들지 않았다"
    assert all(
        ("pm_log.py", function) not in ATOMIC_REPLACE_BOOTSTRAP_EXCEPTIONS
        for function, _line in sites
    ), "이 변이 지점이 이미 등재돼 있다 — 감도 실증이 성립하지 않는다"


# ── 등재된 예외 두 곳: 양쪽 분기를 다 태운다 ─────────────────────────────────
# 이 두 지점은 "seam 이 있으면 seam, 없으면 loud 강등" 이다. **정상 분기를 안 태우면** 강등이
# 상시 경로가 돼도 초록이고, **강등 분기를 안 태우면** 복구 계약이 깨져도 초록이다. 둘 다 태우고
# 각 케이스에 주입 선-단언을 붙인다.

_LEGACY_SEAM_WITHOUT_ATOMIC_REPLACE = "구세대 file_lock 사본에 atomic_replace 가 없음"


def _recording_seam(recorded: list[tuple]) -> types.SimpleNamespace:
    """`atomic_replace` 만 기록하는 seam 대역 (실제 교체까지 수행)."""
    def _replace(src, dst):
        recorded.append((Path(src), Path(dst)))
        os.replace(src, dst)

    return types.SimpleNamespace(atomic_replace=_replace)


def _legacy_seam() -> types.SimpleNamespace:
    """`atomic_replace` 가 없는 구세대 사본 대역 (부분 업그레이드 형상)."""
    legacy = types.SimpleNamespace(ENGINE_REV="v0.0.0-legacy")
    assert not hasattr(legacy, "atomic_replace"), "대역이 구세대 형상이 아니다"
    return legacy


@pytest.mark.parametrize(
    ("tool", "entry"),
    (
        ("pm_update.py", "_atomic_replace_or_degrade"),
        ("pm_import.py", "_atomic_replace_conf"),
    ),
)
def test_registered_exception_uses_the_seam_when_it_is_available(
    tool, entry, tmp_path, monkeypatch,
):
    """정상 트리에서는 **항상 seam 을 탄다** — 강등은 예비 경로일 뿐이다."""
    module = _load(TOOLS / tool, f"{tool[:-3]}_atomic_replace_normal")
    recorded: list[tuple] = []
    monkeypatch.setattr(module, "_load_file_lock", lambda: _recording_seam(recorded))
    tmp, target = _pair(tmp_path, "normal")

    getattr(module, entry)(tmp, target)

    assert recorded == [(tmp, target)], "seam 을 타지 않았다(주입이 no-op 이거나 강등했다)"
    assert target.read_bytes() == b"NEW"


@pytest.mark.parametrize(
    ("tool", "entry"),
    (
        ("pm_update.py", "_atomic_replace_or_degrade"),
        ("pm_import.py", "_atomic_replace_conf"),
    ),
)
@pytest.mark.parametrize("failure", ("missing", "broken"))
def test_registered_exception_degrades_loudly_without_the_seam(
    tool, entry, failure, tmp_path, monkeypatch, capsys,
):
    """seam 부재/손상에서는 교체를 **완주하되 사유를 stderr 로 남긴다** (조용한 강등 0).

    복구 채널이 자기 잠금하면 채택자가 깨진 트리를 못 고친다. 그렇다고 침묵하면 옛 방식이
    상시 경로가 돼도 아무도 모른다 — 그래서 완주 + loud 다.
    """
    module = _load(TOOLS / tool, f"{tool[:-3]}_atomic_replace_{failure}")
    seen: list[str] = []

    def _unusable():
        seen.append(failure)
        raise ModuleNotFoundError("No module named 'file_lock'")

    if failure == "missing":
        monkeypatch.setattr(module, "_load_file_lock", _unusable)
    else:
        monkeypatch.setattr(
            module, "_load_file_lock", lambda: seen.append(failure) or _legacy_seam())
    tmp, target = _pair(tmp_path, failure)

    getattr(module, entry)(tmp, target)

    assert seen == [failure], "주입이 no-op — 강등 분기를 태우지 못했다"
    assert target.read_bytes() == b"NEW", "강등 경로가 교체를 완주하지 못했다"
    message = capsys.readouterr().err
    assert "os.replace" in message, f"강등이 조용하다: {message!r}"
    expected_reason = (
        "ModuleNotFoundError" if failure == "missing"
        else _LEGACY_SEAM_WITHOUT_ATOMIC_REPLACE
    )
    assert expected_reason in message, f"강등 사유가 없다: {message!r}"


def test_conf_writer_exception_still_refuses_to_absorb_a_marked_skew(
    tmp_path, monkeypatch,
):
    """구세대 호환 강등이 **marked skew** 까지 삼키지는 않는다 (기존 경계 보존).

    같은 rev 안의 API 형상 차이는 물러날 근거지만, rev 자체가 다른 사본은 조용한 오작동이 아니라
    재동기 안내로 표출해야 한다 — pm_import 락 경로 유도와 같은 규칙이다.
    """
    pm_import = _load(TOOLS / "pm_import.py", "pm_import_atomic_replace_skew")
    raised: list[str] = []

    def _skew():
        raised.append("skew")
        error = RuntimeError("엔진 사본 버전 불일치 — file_lock.py")
        error._engine_rev_skew = True
        raise error

    monkeypatch.setattr(pm_import, "_load_file_lock", _skew)
    tmp, target = _pair(tmp_path, "skew")

    with pytest.raises(RuntimeError) as error:
        pm_import._atomic_replace_conf(tmp, target)

    assert raised == ["skew"], "주입이 no-op — skew 경계를 태우지 못했다"
    assert getattr(error.value, "_engine_rev_skew", False) is True
    assert target.read_bytes() == b"OLD", "skew 인데 교체가 진행됐다"


def test_bootstrap_exception_absorbs_a_marked_skew_through_the_registered_boundary(
    tmp_path, monkeypatch,
):
    """pm_update 부트스트랩은 반대다 — 혼합 트리의 marked skew 도 **등록된 경계**로 흡수한다.

    동기 실행 중 rev 혼합은 정상 과도 상태라, 여기서 올리면 이미 착지한 엔진 파일 위에서 복구가
    죽는다(pm_update 의 기존 흡수 규칙과 같다). 흡수는 장부에 남고 강등은 loud 다.
    """
    pm_update = _load(TOOLS / "pm_update.py", "pm_update_atomic_replace_skew")
    raised: list[str] = []

    def _skew():
        raised.append("skew")
        error = RuntimeError("엔진 사본 버전 불일치 — file_lock.py")
        error._engine_rev_skew = True
        raise error

    monkeypatch.setattr(pm_update, "_load_file_lock", _skew)
    tmp, target = _pair(tmp_path, "bootstrap_skew")

    pm_update._atomic_replace_or_degrade(tmp, target)

    assert raised == ["skew"], "주입이 no-op — 흡수 경계를 태우지 못했다"
    assert target.read_bytes() == b"NEW"
    assert "atomic_copy_replace_seam" in pm_update._ABSORBED_ENGINE_REV_SKEW, \
        "흡수가 장부에 남지 않았다(감사 불가)"
    assert pm_update._ENGINE_REV_SKEW_RECOVERY_REASONS[
        "atomic_copy_replace_seam"].strip(), "등록 사유가 비어 있다"


def test_mutation_reintroduced_os_replace_in_a_converged_tool_is_red():
    """수렴 도구에 직접 호출을 되살리면 가드가 red 로 잡는다 (감도 실증)."""
    source = (TOOLS / "worktree_pool.py").read_text(encoding="utf-8")
    mutated = source.replace(
        "    file_lock.atomic_replace(tmp, LEASES_FILE)",
        "    os.replace(str(tmp), str(LEASES_FILE))",
        1,
    )
    assert mutated != source, "변이 앵커 소실"
    assert _os_replace_call_count(source) == 0, "worktree_pool 은 수렴 상태여야 한다"
    assert _os_replace_call_count(mutated) == 1


# ── 공유 읽기 (원자 교체의 짝·T-0729) ────────────────────────────────────────
# 쓰기만 POSIX 의미 rename 으로 바꾸면 Windows 에서 실질 개선이 0 이다 — 일반 `open()` 리더가
# 하나라도 잡고 있으면 그 rename 이 WinError 32 로 막힌다(실측 표). 리더가 공유 삭제를 허용해야
# 교체가 성공하고 그 리더는 옛 내용을 끝까지 읽는다. 두 축은 세트로만 성립한다.
#
# 이 seam 의 제1 계약은 **POSIX 에서 내장 호출과 글자 그대로 같은 동작**이다. 전환 대상이
# 100지점 단위라 인코딩·개행 의미가 1mm 어긋나면 그만큼 곱해진다([[T-0709]]·[[T-0710]] 축).
# 그래서 아래 동치 회귀는 "그럴듯한 값" 이 아니라 **내장 호출의 결과와 직접 대조**한다.

# 개행 축의 경계값 — LF·CRLF·CR·혼합·마지막 줄 개행 없음. universal newlines 는 셋을 모두
# `\n` 으로 접고, `newline=""` 은 원문을 보존한다.
_NEWLINE_PAYLOADS = (
    ("lf", b"a\nb\n"),
    ("crlf", b"a\r\nb\r\n"),
    ("cr", b"a\rb\r"),
    ("mixed", b"a\r\nb\nc\rd"),
    ("no-trailing", b"a\nb"),
)

# 텍스트 축의 경계값 — 비-ASCII 가 인코딩별로 다른 바이트가 되는 쌍.
_ENCODING_PAYLOADS = (
    ("utf-8", "티켓 본문 — 인용부호 “값”\n"),
    ("cp949", "티켓 본문 한글\n"),
    ("latin-1", "café résumé\n"),
)


@pytest.mark.parametrize(("label", "payload"), _NEWLINE_PAYLOADS)
@pytest.mark.parametrize("newline", (None, "", "\n"))
def test_shared_text_read_matches_the_builtin_open_exactly(
    file_lock, tmp_path, label, payload, newline
):
    """`read_text_shared` 는 같은 인자의 내장 `open().read()` 와 **같은 문자열**이다.

    개행 인자의 의미가 보존되는지를 값으로 고정한다 — `None`(universal)·`""`(원문 보존)·
    `"\\n"`(번역 없음)이 서로 다른 결과를 내는 payload 로 태운다.
    """
    target = tmp_path / f"{label}.md"
    target.write_bytes(payload)
    with open(target, "r", encoding="utf-8", newline=newline) as handle:
        expected = handle.read()

    assert file_lock.read_text_shared(
        target, encoding="utf-8", newline=newline) == expected


def test_newline_argument_actually_changes_the_result(file_lock, tmp_path):
    """선-단언 — 개행 인자가 결과를 실제로 가르는가.

    가르지 않으면 위 동치 회귀는 인자를 흘려도 초록이라 아무것도 지키지 못한다.
    """
    target = tmp_path / "crlf.md"
    target.write_bytes(b"a\r\nb\r\n")

    universal = file_lock.read_text_shared(target, encoding="utf-8", newline=None)
    verbatim = file_lock.read_text_shared(target, encoding="utf-8", newline="")

    assert universal == "a\nb\n"
    assert verbatim == "a\r\nb\r\n"
    assert universal != verbatim, "개행 축이 무의미하다 — 회귀가 공허해진다"


@pytest.mark.parametrize(("encoding", "text"), _ENCODING_PAYLOADS)
def test_shared_text_read_honours_the_requested_encoding(
    file_lock, tmp_path, encoding, text
):
    """인코딩 인자는 내장 호출과 같은 의미다 (locale 기본으로 새지 않는다)."""
    target = tmp_path / "encoded.md"
    target.write_bytes(text.encode(encoding))

    assert file_lock.read_text_shared(target, encoding=encoding) == text
    assert file_lock.read_text_shared(
        target, encoding=encoding) == target.read_text(encoding=encoding)


def test_shared_text_read_honours_the_error_policy(file_lock, tmp_path):
    """`errors` 도 내장 그대로다 — 깨진 바이트를 진단용으로 읽는 지점이 죽지 않는다.

    엔진은 사본 대조·진단 출력에서 `errors="replace"` 로 읽는다(그 자리에서 죽으면 진단 자체가
    사라진다). 기본값(`None`=strict)과 결과가 갈리는 payload 로 두 축을 함께 태운다.
    """
    target = tmp_path / "broken.md"
    target.write_bytes(b"ok \xff\xfe tail\n")

    with pytest.raises(UnicodeDecodeError):
        file_lock.read_text_shared(target, encoding="utf-8")
    assert file_lock.read_text_shared(
        target, encoding="utf-8", errors="replace",
    ) == target.read_text(encoding="utf-8", errors="replace")


def test_binary_mode_refuses_the_error_policy(file_lock, tmp_path):
    """바이너리 모드에 `errors` 를 주는 것도 호출 오류다 (내장 `open` 과 같은 규칙)."""
    target = tmp_path / "bytes.bin"
    target.write_bytes(b"x")

    with pytest.raises(ValueError):
        file_lock.open_shared(target, binary=True, errors="replace")


def test_shared_text_read_defaults_match_the_builtin_default(file_lock, tmp_path):
    """인자를 안 주면 내장 `open` 의 기본(locale 인코딩·universal newlines)과 같다.

    엔진은 인코딩을 명시하는 관례지만, seam 이 **기본값을 몰래 바꾸면** 명시하지 않은 전환
    지점의 의미가 조용히 달라진다. 기본값도 내장과 대조해 고정한다.
    """
    target = tmp_path / "plain.txt"
    target.write_bytes(b"ascii-only\r\nline\r\n")
    with open(target, "r") as handle:
        expected = handle.read()

    assert file_lock.read_text_shared(target) == expected


@pytest.mark.parametrize(("label", "payload"), _NEWLINE_PAYLOADS)
def test_shared_binary_read_returns_the_exact_bytes(file_lock, tmp_path, label, payload):
    """`read_bytes_shared` 는 `Path.read_bytes()` 와 **바이트 그대로** 같다 (번역 0)."""
    target = tmp_path / f"{label}.bin"
    target.write_bytes(payload)

    assert file_lock.read_bytes_shared(target) == payload == target.read_bytes()


def test_open_shared_returns_a_readable_handle_in_both_modes(file_lock, tmp_path):
    """`open_shared` 는 내장 `open` 처럼 컨텍스트 매니저로 쓰는 파일 객체를 돌려준다."""
    target = tmp_path / "ledger.json"
    target.write_bytes(b'{"a": 1}\n')

    with file_lock.open_shared(target, binary=True) as handle:
        assert handle.read() == b'{"a": 1}\n'
    with file_lock.open_shared(target, binary=False, encoding="utf-8") as handle:
        assert handle.read() == '{"a": 1}\n'


def test_open_shared_accepts_paths_and_strings_like_the_builtin(file_lock, tmp_path):
    """경로 표기(`Path`/`str`)를 내장 호출과 같게 받는다 (전환이 표기를 강요하지 않는다)."""
    target = tmp_path / "either.md"
    target.write_bytes(b"same\n")

    assert file_lock.read_bytes_shared(str(target)) == b"same\n"
    assert file_lock.read_text_shared(Path(target), encoding="utf-8") == "same\n"


def test_binary_mode_refuses_text_arguments(file_lock, tmp_path):
    """바이너리 모드에 텍스트 인자를 주면 호출 오류다 (내장 `open` 과 같은 규칙)."""
    target = tmp_path / "bytes.bin"
    target.write_bytes(b"x")

    with pytest.raises(ValueError):
        file_lock.open_shared(target, binary=True, encoding="utf-8")
    with pytest.raises(ValueError):
        file_lock.open_shared(target, binary=True, newline="")


def test_missing_target_raises_the_same_error_as_the_builtin(file_lock, tmp_path):
    """부재 파일은 내장 호출과 **같은 예외 타입**이다 (호출부 except 절이 그대로 산다)."""
    missing = tmp_path / "absent.md"

    with pytest.raises(FileNotFoundError):
        file_lock.read_text_shared(missing, encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        file_lock.read_bytes_shared(missing)


def test_shared_reader_keeps_the_old_content_across_an_atomic_replace(
    file_lock, tmp_path
):
    """불변식 2 — 교체 시점에 열려 있던 공유 리더는 **옛 내용**을 끝까지 읽는다.

    락-free 읽기가 일관 스냅샷을 본다는 보장이 바로 이 성질이다(board·worktree_pool 이 명시로
    기대 있다). 읽기 seam 을 얹은 뒤에도 그 성질이 남는지 실동작으로 고정한다.
    """
    target = tmp_path / "leases.json"
    target.write_bytes(b"OLD-CONTENT")
    tmp = tmp_path / "leases.json.tmp"
    tmp.write_bytes(b"NEW-CONTENT")

    with file_lock.open_shared(target, binary=True) as reader:
        file_lock.atomic_replace(tmp, target)
        assert reader.read() == b"OLD-CONTENT"
    assert target.read_bytes() == b"NEW-CONTENT"


# ── 읽기 플랫폼 분기: POSIX 는 내장 호출 그대로 ──────────────────────────────

def test_this_development_platform_takes_the_posix_read_branch(file_lock):
    """선-단언 — 플랫폼 판정이 실행 OS 와 일치한다.

    POSIX 개발기에서 False 여야 위 동치 회귀들이 실제로 POSIX 분기를 태운 것이고(Windows 분기가 켜진
    채였다면 "내장과 같다" 는 단언이 다른 것을 재고 있었던 셈), Windows VM 에서 True 여야 실 API 분기가
    도는 것이다. 어느 쪽이든 OS 를 잘못 읽으면 red.
    """
    assert file_lock.windows_shared_read_platform() is (os.name == "nt")


def test_posix_read_branch_never_touches_the_windows_primitive(
    file_lock, tmp_path, monkeypatch
):
    """POSIX 분기는 Windows 원시 API 를 **부르지 않는다** (분기 판정이 실제로 갈린다).

    분기를 명시 주입해 POSIX 경로를 태운다 — Windows VM 에서도 같은 것을 재기 위해서다.
    """
    monkeypatch.setattr(file_lock, "windows_shared_read_platform", lambda: False)

    def _forbidden():
        raise AssertionError("POSIX 분기가 Windows 원시 API 를 불렀다")

    monkeypatch.setattr(file_lock, "_windows_shared_open_api", _forbidden)
    target = tmp_path / "posix.md"
    target.write_bytes(b"plain\n")

    assert file_lock.read_text_shared(target, encoding="utf-8") == "plain\n"


# ── 읽기 플랫폼 분기: Windows 정책층을 주입으로 태운다 ───────────────────────
# 대역은 실제 fd 를 돌려준다 — 정책층이 "핸들을 fd 로 넘기고 그 위에 파일 객체를 얹는다" 까지
# 끝까지 가야 개행·인코딩 의미가 그 분기에서도 같은지 값으로 대조할 수 있다.

# 대역이 흉내내는 CRT 바이너리 플래그 (Windows `os.O_BINARY`·POSIX 에는 없다).
_INJECTED_READ_O_BINARY = 0x8000


class FakeWindowsSharedOpenApi:
    """`WindowsSharedOpenApi` 대역 — 인자를 기록하되 **진짜 fd** 로 이어 준다.

    `open_handle` 은 POSIX `os.open` 으로 연 fd 를 '핸들' 로 쓰고, `open_osfhandle` 은 그 값을
    그대로 fd 로 넘긴다(Windows 의 소유권 이전과 같은 모양). 그래서 이 대역으로도 읽기 결과가
    실제 파일 내용이며, 정책층의 인자와 소유권 처리를 함께 단언할 수 있다.
    """

    def __init__(self, *, open_error: OSError | None = None,
                 osfhandle_error: OSError | None = None) -> None:
        self.open_error = open_error
        self.osfhandle_error = osfhandle_error
        self.open_calls: list[tuple[str, int, int]] = []
        self.osfhandle_calls: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def open_handle(self, path: str, access: int, share: int) -> int:
        self.open_calls.append((path, access, share))
        if self.open_error is not None:
            raise self.open_error
        return os.open(path, os.O_RDONLY)

    def open_osfhandle(self, handle: int, flags: int) -> int:
        self.osfhandle_calls.append((handle, flags))
        if self.osfhandle_error is not None:
            raise self.osfhandle_error
        return handle

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)
        os.close(handle)

    def as_named_tuple(self, module):
        return module.WindowsSharedOpenApi(
            self.open_handle, self.open_osfhandle, self.close_handle)


def _force_windows_read_branch(
    module, monkeypatch, api: FakeWindowsSharedOpenApi
) -> None:
    """플랫폼 판정과 원시 API 를 Windows 쪽으로 갈아끼운다 (주입 지점 두 곳 모두).

    `os.O_BINARY` 도 함께 심는다 — POSIX 에는 그 이름이 없어 플래그 축을 태울 수 없다.
    """
    monkeypatch.setattr(module, "windows_shared_read_platform", lambda: True)
    monkeypatch.setattr(
        module, "_windows_shared_open_api", lambda: api.as_named_tuple(module))
    monkeypatch.setattr(module.os, "O_BINARY", _INJECTED_READ_O_BINARY, raising=False)


def test_windows_read_injection_actually_reaches_the_primitive(
    file_lock, tmp_path, monkeypatch
):
    """선-단언 — 주입이 no-op 이 아니다 (원시 대역이 실제로 불린다).

    이 단언 없이는 아래 Windows 회귀들이 POSIX 분기를 재면서 초록일 수 있다
    ([[guard-must-cover-its-own-surface]]).
    """
    api = FakeWindowsSharedOpenApi()
    _force_windows_read_branch(file_lock, monkeypatch, api)
    target = tmp_path / "probe.md"
    target.write_bytes(b"probe\n")

    assert file_lock.read_bytes_shared(target) == b"probe\n"
    assert api.open_calls, "주입이 no-op — Windows 읽기 분기를 태우지 못했다"
    assert api.osfhandle_calls, "핸들이 fd 로 넘어가지 않았다"


def test_windows_read_opens_with_read_access_and_full_sharing(
    file_lock, tmp_path, monkeypatch
):
    """Windows 분기는 **읽기 권한 + 읽기/쓰기/삭제 공유**로 연다.

    공유 삭제가 빠지면 그 리더가 POSIX 의미 rename 을 WinError 32 로 막는다 — 이 seam 이
    존재하는 이유 자체가 사라진다.
    """
    api = FakeWindowsSharedOpenApi()
    _force_windows_read_branch(file_lock, monkeypatch, api)
    target = tmp_path / "ledger.json"
    target.write_bytes(b"{}")

    file_lock.read_bytes_shared(target)

    path, access, share = api.open_calls[0]
    assert path == str(target)
    assert access == file_lock.GENERIC_READ
    assert share == (
        file_lock.FILE_SHARE_READ | file_lock.FILE_SHARE_WRITE
        | file_lock.FILE_SHARE_DELETE
    )
    assert share == file_lock.SHARE_ALL_MODES


def test_windows_read_hands_the_descriptor_a_binary_flag(
    file_lock, tmp_path, monkeypatch
):
    """fd 는 **바이너리**로 넘긴다 — CRT 텍스트 모드가 붙으면 읽기가 번역된다.

    내장 `open` 은 Windows 에서도 바이너리 fd 위에 TextIOWrapper 를 얹어 개행을 파이썬 층에서만
    처리한다. 우리가 만든 fd 가 텍스트 모드면 CRLF 가 fd 층에서 한 번 더 접혀 `newline=""`
    (원문 보존)이 의미를 잃는다.
    """
    api = FakeWindowsSharedOpenApi()
    _force_windows_read_branch(file_lock, monkeypatch, api)
    target = tmp_path / "crlf.md"
    target.write_bytes(b"a\r\nb\r\n")

    file_lock.read_bytes_shared(target)

    _handle, flags = api.osfhandle_calls[0]
    assert flags & _INJECTED_READ_O_BINARY, f"바이너리 플래그가 없다: {flags}"
    assert flags & os.O_RDONLY == os.O_RDONLY or os.O_RDONLY == 0


@pytest.mark.parametrize(("label", "payload"), _NEWLINE_PAYLOADS)
@pytest.mark.parametrize("newline", (None, ""))
def test_windows_read_yields_the_same_text_as_the_posix_branch(
    file_lock, tmp_path, monkeypatch, label, payload, newline
):
    """두 분기가 **같은 문자열**을 낸다 — 이식이 의미를 바꾸지 않았다.

    같은 파일을 POSIX 분기로 한 번, 주입한 Windows 분기로 한 번 읽어 직접 대조한다.
    """
    target = tmp_path / f"{label}.md"
    target.write_bytes(payload)
    posix_text = file_lock.read_text_shared(
        target, encoding="utf-8", newline=newline)

    api = FakeWindowsSharedOpenApi()
    _force_windows_read_branch(file_lock, monkeypatch, api)
    windows_text = file_lock.read_text_shared(
        target, encoding="utf-8", newline=newline)

    assert api.open_calls, "주입이 no-op — 대조가 성립하지 않는다"
    assert windows_text == posix_text


def test_windows_read_returns_the_handle_when_the_descriptor_handoff_fails(
    file_lock, tmp_path, monkeypatch
):
    """fd 로 넘기기 전에 실패하면 **핸들을 반환한다** (소유권이 아직 여기 있다).

    이 자리를 빼먹으면 실패마다 커널 핸들이 새고, 그 파일은 이후 삭제·교체가 막힌다.
    """
    api = FakeWindowsSharedOpenApi(osfhandle_error=OSError("핸들 이전 실패"))
    _force_windows_read_branch(file_lock, monkeypatch, api)
    target = tmp_path / "handoff.md"
    target.write_bytes(b"x")

    with pytest.raises(OSError, match="핸들 이전 실패"):
        file_lock.read_bytes_shared(target)

    assert len(api.closed) == 1, f"핸들이 반환되지 않았다: {api.closed}"


def test_windows_read_does_not_double_close_after_a_successful_handoff(
    file_lock, tmp_path, monkeypatch
):
    """소유권이 fd 로 넘어간 뒤에는 핸들을 따로 닫지 않는다 (이중 반환 금지).

    Windows 에서 이중 close 는 그 사이 재사용된 남의 핸들을 닫아 무관한 IO 를 깨뜨린다.
    """
    api = FakeWindowsSharedOpenApi()
    _force_windows_read_branch(file_lock, monkeypatch, api)
    target = tmp_path / "once.md"
    target.write_bytes(b"y")

    assert file_lock.read_bytes_shared(target) == b"y"
    assert api.closed == [], f"소유권 이전 뒤에도 핸들을 닫았다: {api.closed}"


def test_windows_read_propagates_the_open_failure_without_a_descriptor(
    file_lock, tmp_path, monkeypatch
):
    """열기 실패는 그대로 올라간다 — 일반 `open` 으로 조용히 내려앉지 않는다.

    폴백을 두면 그 경로의 리더가 다시 교체를 막는데도 초록이 된다([[no-green-by-disabling]]).
    """
    api = FakeWindowsSharedOpenApi(open_error=PermissionError("열기 거부"))
    _force_windows_read_branch(file_lock, monkeypatch, api)
    target = tmp_path / "denied.md"
    target.write_bytes(b"z")

    with pytest.raises(PermissionError, match="열기 거부"):
        file_lock.read_bytes_shared(target)

    assert api.osfhandle_calls == [], "열기에 실패했는데 fd 이전을 시도했다"
    assert api.closed == [], "열지 못한 핸들을 닫으려 했다"


def test_shared_read_flags_include_the_platform_binary_flag(file_lock, monkeypatch):
    """플래그 조립은 플랫폼이 그 이름을 줄 때만 얹는다 (POSIX 에서 AttributeError 금지)."""
    assert file_lock._shared_read_flags() == os.O_RDONLY | getattr(os, "O_BINARY", 0)

    monkeypatch.setattr(file_lock.os, "O_BINARY", _INJECTED_READ_O_BINARY, raising=False)
    assert file_lock._shared_read_flags() == os.O_RDONLY | _INJECTED_READ_O_BINARY


# ── 읽기 재복제 차단 (원자 교체 대상 판독은 seam 한 곳으로·T-0729) ───────────
# 쓰기 축과 같은 규율이다 — **가드가 곧 목록**이라 사람이 전환 대상을 관리하지 않는다.
# `tools/*.py` 전수를 AST 로 훑어 파일 판독(`read_text`/`read_bytes`/읽기 모드 `open`)을 모두
# 열거하고, 등재부 밖에서 종전 읽기가 되살아나면 그 자리가 red 다.
#
# 판정 범위가 "원자 교체 대상 파일" 이 아니라 "엔진 도구의 모든 판독" 인 이유는 실측이다 —
# 대상만 좁히려면 dst 값-흐름을 풀어야 하는데 17개 호출의 dst 가 전부 지역 변수라 파일 정체가
# 안 나오고(티켓 파일은 런타임 조립이라 어떤 표현식으로도 안 잡힌다), 리터럴 기반으로 좁히면
# 24모듈 중 24모듈이 걸려 전량과 같아진다. 판정 가능한 규칙은 전량 + 등재 예외 하나뿐이다.

# 판독 프리미티브 — 이 이름을 부르는 자리가 곧 전환 대상이다.
_READ_PRIMITIVES = frozenset({"read_text", "read_bytes"})

# `os.open` 은 **fd 프리미티브**라 파일 객체 판독이 아니다 — 심볼릭 링크 안전 walk(dir_fd·
# O_NOFOLLOW)와 배타 생성(O_EXCL)이 그 위에 서 있고, 공유 읽기로 바꾸면 그 보장이 사라진다.
_FD_PRIMITIVE_RECEIVERS = frozenset({"os"})

# 쓰기 모드 문자 — 하나라도 있으면 판독이 아니다.
_WRITE_MODE_FLAGS = "wax+"

# 공용 seam 안에서 종전 열기를 부르는 **유일한** 자리 (POSIX 분기).
_SEAM_READ_SITE = ("file_lock.py", "open_shared")


def _degrade_helper_reason(module: str, why: str, *helpers: str) -> dict:
    """한 모듈의 강등 헬퍼들에 같은 사유를 단다 (헬퍼는 등재 예외의 *구현*이다).

    헬퍼 이름을 인자로 받는다 — 모듈마다 쓰는 판독 종류가 달라(예: `review_rounds` 는 bytes
    판독이 없다) 세 개를 가정하면 없는 함수가 등재부에 남아 red 가 된다.
    """
    return {(module, name): why for name in helpers}


# **등재된 예외** — 공유 읽기 seam 을 쓸 수 없거나 대상이 아닌 판독. `(모듈, 함수)` → 사유.
# 등재 밖 종전 읽기도, 사유가 빈 등재도 red 다([[T-0729]] §결정 · 선택지 A 의 판독 쪽 확장).
SHARED_READ_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("engine_rev.py", "read_literal"): (
        "rev 검증자 자신의 부트스트랩 판독 — 형제 file_lock 을 로드하려면 그 사본의 rev 를 "
        "대조해야 하고 그 대조를 하는 곳이 여기라 순환이다"
    ),
    ("engine_rev.py", "bump"): (
        "같은 순환 위에서 자기 rev 를 포함해 전 모듈 리터럴을 다시 쓰는 경로 — 스탬프가 바뀌는 "
        "도중이라 형제 rev 대조가 성립하지 않는다"
    ),
    ("pm_render.py", "render_file"): (
        "인자가 상류 템플릿 원본(원자 교체 대상 아님)인지 목적지 사본(pm_import 가 원자 교체)인지 "
        "**정적으로 구분되지 않는다**. 렌더는 순수 변환이라 이 판독이 상태를 만들지 않고, 호출부의 "
        "목적지 판독은 이미 seam 을 지난다"
    ),
    ("pm_update.py", "_copy_manifest_preserving_guest"): (
        "`sp` 가 파일 경로가 아니라 **인메모리 소스**(`_ManifestTextSource`·flavor manifest "
        "합집합)일 수 있는 자리다 — 같은 문에서 `isinstance(sp, Path)` 로 갈라 경로일 때만 seam 을 "
        "지난다"
    ),
    **_degrade_helper_reason(
        "pm_update.py",
        "복구 채널의 판독 강등 분기 — pm_update 는 엔진 사본이 깨진 채택자에게도 떠야 한다"
        "(`_atomic_replace_or_degrade` 의 판독 쪽 짝·등록 사유 `shared_read_seam`)",
        "_read_text_shared", "_read_bytes_shared", "_open_shared",
    ),
    **_degrade_helper_reason(
        "pm_import.py",
        "복구/도입 채널의 판독 강등 분기 — 구세대·손상 사본 트리에서도 conf 판독과 키 기록이 "
        "성립해야 한다(`_atomic_replace_conf` 와 같은 등재 항목의 판독 쪽)",
        "_read_text_shared", "_read_bytes_shared", "_open_shared",
    ),
    **_degrade_helper_reason(
        "pm_log.py",
        "판독은 형제 없이도 떠야 한다 — pm_bootstrap 이 이 모듈의 로그 판독을 fail-soft 로 "
        "재사용한다(로더 주석의 명시 계약)",
        "_read_text_shared", "_read_bytes_shared", "_open_shared",
    ),
    **_degrade_helper_reason(
        "review_rounds.py",
        "판독은 형제 없이도 떠야 한다 — 부분 동기 트리에서도 라운드 판정이 살아 있어야 그 판정을 "
        "쓰는 도구들이 복구를 안내한다(로더 주석의 명시 계약)",
        "_read_text_shared", "_open_shared",
    ),
    ("additional_reviewer.py", "_read_text_shared"): (
        "판독은 형제 없이도 떠야 한다 — 진단·denylist·재앵커는 seam 이 필요한 `--gate` 구간 밖이고 "
        "pm_delegate 가 그 경로를 deep-import 로 재사용한다(로더 주석의 기능 축)"
    ),
    ("local_conf.py", "_read_text_shared"): (
        "conf 판독은 형제 없이도 떠야 한다 — 모든 도구가 conf 를 읽는 지점에서 이 leaf 파서를 "
        "로드하므로, seam 부재를 예외로 올리면 부분 동기 트리에서 전 명령이 동시에 죽는다"
    ),
    ("local_conf.py", "_sibling_sequence"): (
        "레지스트리 패턴 전개가 형제 **소스 선언**을 AST 로 읽는 자리다 — 상태 파일이 아니라 "
        "코드 텍스트라 원자 교체 경합의 대상이 아니고, 판정의 소비자는 never-block advisory 다"
    ),
    ("identity_args.py", "_read_text_shared"): (
        "board 가 import 시점에 로드하는 leaf point-reader 의 강등 분기 — 판독 계약이 "
        "'부재/손상 → None' 이라 seam 부재로 올리면 실재하는 장부를 '없음' 으로 위장한다"
    ),
    ("repo_coordinates.py", "_read_text_shared"): (
        "같은 leaf 좌표 모듈의 강등 분기 — seam 부재로 올리면 실재하는 리스 장부를 '읽을 수 없다' "
        "로 위장해 좌표 해소 전체가 막힌다"
    ),
}


def _is_read_call(node: ast.Call, bindings: tuple[set[str], set[str]]) -> bool:
    """이 호출이 **파일 판독**인가 (`read_text`/`read_bytes`/읽기 모드 `open`).

    `os.open` 은 제외한다(fd 프리미티브). 모드가 상수로 안 보이면 내장 `open` 기본값(`"r"`)을
    따라 판독으로 본다 — 놓치는 쪽보다 더 세는 쪽이 안전하다(등재로 풀 수 있다).
    """
    open_names, os_aliases = bindings
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _READ_PRIMITIVES:
        return True
    is_builtin = isinstance(func, ast.Name) and func.id in open_names
    is_method = isinstance(func, ast.Attribute) and func.attr == "open"
    if not (is_builtin or is_method):
        return False
    if (is_method and isinstance(func.value, ast.Name)
            and func.value.id in os_aliases):
        return False
    index = 1 if is_builtin else 0
    mode = None
    if len(node.args) > index and isinstance(node.args[index], ast.Constant):
        mode = node.args[index].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = keyword.value.value
    effective = mode if isinstance(mode, str) else "r"
    return not any(flag in effective for flag in _WRITE_MODE_FLAGS)


def _open_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """`(open 을 가리키는 이름들, os 별칭들)` — 별칭 우회를 판정에 반영한다.

    `import os as _o` 로 받은 `_o.open(...)` 도 fd 프리미티브이고, `from builtins import open
    as o` 로 받은 `o(path)` 도 판독이다. 이름 그대로만 보면 둘 다 가드의 사각이 된다.
    """
    names = {"open"}
    os_aliases = set(_FD_PRIMITIVE_RECEIVERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_aliases.update(alias.asname or alias.name
                              for alias in node.names if alias.name == "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins":
            names.update(alias.asname or alias.name
                         for alias in node.names if alias.name == "open")
    return names, os_aliases


def _read_call_sites(source: str) -> list[tuple[str, int]]:
    """소스 한 벌의 판독 호출 위치 `(감싼 함수명, 줄번호)` 목록 (귀속까지 기계 판정)."""
    tree = ast.parse(source)
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        name = node.name if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing.get(node, "")
        for child in ast.iter_child_nodes(node):
            enclosing[child] = name
    bindings = _open_bindings(tree)
    return [
        (enclosing.get(node, ""), node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_read_call(node, bindings)
    ]


def _read_sites_by_module() -> dict[str, list[tuple[str, int]]]:
    """tools/ 전수 → 모듈별 판독 호출 위치 `(감싼 함수, 줄번호)`."""
    found = {
        path.name: _read_call_sites(path.read_text(encoding="utf-8"))
        for path in sorted(TOOLS.glob("*.py"))
    }
    return {name: sites for name, sites in found.items() if sites}


def test_plain_reads_live_only_in_the_seam_and_the_registered_exceptions():
    """종전 읽기 = 공용 seam 한 곳 + **등재된 예외**뿐이다 (가드가 곧 목록).

    쓰기 축(`test_os_replace_lives_only_in_the_seam_and_the_registered_exceptions`)과 같은
    판정이다 — `tools/*.py` 전수를 훑어 판독을 감싼 함수까지 귀속시키고, 등재부 밖에서 되살아나면
    그 자리가 red 다. 등재부가 사라져도 red 이므로 예외는 조용히 증식하지 못한다.
    """
    measured = {
        (module, function)
        for module, sites in _read_sites_by_module().items()
        for function, _line in sites
    }
    expected = {_SEAM_READ_SITE} | set(SHARED_READ_EXCEPTIONS)
    assert measured == expected, (
        f"등재 밖 종전 읽기: {sorted(measured - expected)} / "
        f"사라진 등재(목록 정리 필요): {sorted(expected - measured)}"
    )


def test_every_registered_read_exception_carries_a_reason():
    """등재된 판독 예외는 사유가 있어야 한다 — 빈 사유는 등재가 아니라 구멍이다."""
    assert SHARED_READ_EXCEPTIONS, "등재부가 비었다(예외 0이면 가드가 더 좁아야 한다)"
    for site, reason in SHARED_READ_EXCEPTIONS.items():
        assert reason.strip(), f"사유 없는 등재: {site}"


def test_read_seam_consumers_delegate_instead_of_reading_directly():
    """전환한 도구는 seam 판독 위임을 실제로 갖는다 (등재 예외 밖 종전 읽기 0).

    "종전 읽기가 없다" 만 보면 판독 자체가 사라진 모듈도 초록이다 — 위임이 **있는지**를 함께 본다.
    """
    exempt = {module for module, _function in SHARED_READ_EXCEPTIONS}
    for module, sites in _read_sites_by_module().items():
        if module == _SEAM_READ_SITE[0]:
            continue
        assert module in exempt, f"{module}: 등재 없는 종전 읽기 {sites}"
    converted = {
        path.name for path in sorted(TOOLS.glob("*.py"))
        if _delegates_to_shared_read(path.read_text(encoding="utf-8"))
    }
    assert len(converted) >= 20, f"전환 모듈이 너무 적다(측정 오류 의심): {sorted(converted)}"


def _delegates_to_shared_read(source: str) -> bool:
    """이 소스가 공유 읽기에 위임하는가 (seam 호출 또는 모듈 지역 강등 헬퍼 호출)."""
    names = {"read_text_shared", "read_bytes_shared", "open_shared",
             "_read_text_shared", "_read_bytes_shared", "_open_shared"}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in names:
            return True
        if isinstance(func, ast.Name) and func.id in names:
            return True
        if (isinstance(func, ast.Name) and func.id == "getattr"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in names):
            return True
    return False


@pytest.mark.parametrize(
    ("label", "snippet", "expected"),
    (
        ("read-text", "p.read_text(encoding='utf-8')\n", 1),
        ("read-bytes", "p.read_bytes()\n", 1),
        ("open-default", "open(p)\n", 1),
        ("open-text", "open(p, 'r', encoding='utf-8')\n", 1),
        ("open-binary", "open(p, 'rb')\n", 1),
        ("open-method", "p.open('r', encoding='utf-8')\n", 1),
        ("open-keyword-mode", "open(p, mode='r')\n", 1),
        ("write", "open(p, 'w', encoding='utf-8')\n", 0),
        ("append", "open(p, 'a')\n", 0),
        ("update", "open(p, 'r+')\n", 0),
        ("fd-primitive", "import os\nos.open(p, os.O_RDONLY)\n", 0),
        ("write-text", "p.write_text('x', encoding='utf-8')\n", 0),
        ("comment", "# p.read_text(encoding='utf-8')\nDOC = 'p.read_bytes()'\n", 0),
    ),
)
def test_read_guard_classifies_each_form(label, snippet, expected):
    """판정기가 각 표기를 옳게 가른다 — 판독만 세고 쓰기·fd 프리미티브·산문은 안 센다."""
    assert len(_read_call_sites(snippet)) == expected, label


def test_a_new_unregistered_plain_read_is_red():
    """등재 없는 새 종전 읽기는 가드가 red 로 잡는다 (등재부가 곧 상한·감도 실증)."""
    source = (TOOLS / "worktree_pool.py").read_text(encoding="utf-8")
    mutated = source.replace(
        'file_lock.read_text_shared(LEASES_FILE, encoding="utf-8")',
        'LEASES_FILE.read_text(encoding="utf-8")',
        1,
    )
    assert mutated != source, "변이 앵커 소실"
    sites = _read_call_sites(mutated)
    assert sites, "변이가 실제로 종전 읽기를 만들지 않았다"
    assert all(
        ("worktree_pool.py", function) not in SHARED_READ_EXCEPTIONS
        for function, _line in sites
    ), "이 변이 지점이 이미 등재돼 있다 — 감도 실증이 성립하지 않는다"


def test_the_fd_primitive_exclusion_is_not_a_blanket_hole():
    """`os.open` 제외는 **수신자 기준**이다 — 같은 이름의 파일 객체 열기는 그대로 센다.

    제외를 이름(`open`)으로 걸면 `path.open("rb")` 전량이 가드 밖으로 새어 나간다.
    """
    assert len(_read_call_sites("import os\nos.open(p, os.O_RDONLY)\n")) == 0
    assert len(_read_call_sites("import os\np.open('rb')\n")) == 1
    assert len(_read_call_sites("import os as _o\n_o.open(p, 0)\np.open('rb')\n")) == 1


# ── 강제 삭제 (read-only·잔재 금지·T-0711) ──────────────────────────────────


def _read_only_tree(root: Path) -> Path:
    """git object 트리와 같은 형상 — read-only 파일 + **쓰기 권한 없는 디렉토리**.

    파일만 read-only 로 만들면 POSIX 에서는 여전히 지워진다(부모 디렉토리 권한만 보므로).
    디렉토리 조합까지 넣어야 이 축이 Linux 에서도 red 로 재현된다.
    """
    objects = root / "objects" / "10"
    objects.mkdir(parents=True)
    blob = objects / "a9500e"
    blob.write_bytes(b"packed object\n")
    os.chmod(blob, stat.S_IREAD)
    os.chmod(objects, stat.S_IREAD | stat.S_IEXEC)
    return root


def test_force_rmtree_removes_a_read_only_tree_that_ignore_errors_leaves_behind(
    file_lock, tmp_path,
):
    """read-only 트리를 실제로 지운다 — 옛 관용구(`ignore_errors=True`)는 잔재를 남긴다."""
    swallowed = _read_only_tree(tmp_path / "swallowed")
    shutil.rmtree(swallowed, ignore_errors=True)
    assert swallowed.exists(), \
        "픽스처가 read-only 를 재현하지 못했다 — 이 트리는 맨 rmtree 로도 지워진다"

    target = _read_only_tree(tmp_path / "target")
    file_lock.force_rmtree(target)

    assert not target.exists(), "force_rmtree 뒤에도 트리가 남음"


def test_force_rmtree_treats_absence_as_success(file_lock, tmp_path):
    """부재는 성공이다 — 정리의 목적은 '없다' 이고 경쟁 삭제도 그 목적을 이룬다."""
    file_lock.force_rmtree(tmp_path / "never-existed")


def test_force_rmtree_raises_instead_of_leaving_a_silent_leftover(file_lock, tmp_path):
    """지우지 못하면 예외 — 삼키면 잔재가 있어도 rc=0 이 된다."""
    not_a_tree = tmp_path / "regular.txt"
    not_a_tree.write_text("x", encoding="utf-8")

    with pytest.raises(OSError) as exc:
        file_lock.force_rmtree(not_a_tree)

    assert str(not_a_tree) in str(exc.value)
    assert not_a_tree.exists(), "판정 대상이 사라져 단언이 공허해졌다"


def test_force_rmtree_retries_then_gives_up_with_the_last_error(
    file_lock, tmp_path, monkeypatch,
):
    """열린 핸들처럼 속성 해제로 안 풀리는 실패는 상한까지 재시도하고 그 뒤 예외다."""
    target = tmp_path / "locked"
    target.mkdir()
    attempts: list[int] = []
    naps: list[float] = []
    boom = PermissionError(errno.EACCES, "다른 프로세스가 파일을 사용 중")

    def _always_fail(path, **kwargs):
        attempts.append(1)
        raise boom

    monkeypatch.setattr(file_lock.shutil, "rmtree", _always_fail)
    monkeypatch.setattr(file_lock.time, "sleep", naps.append)

    with pytest.raises(OSError) as exc:
        file_lock.force_rmtree(target, retries=3)

    assert len(attempts) == 3, f"재시도 상한이 지켜지지 않음: {len(attempts)}"
    assert len(naps) == 2, f"재시도 사이 대기가 없거나 마지막 뒤에도 잔다: {naps}"
    assert exc.value.__cause__ is boom
    assert "다른 프로세스가 파일을 사용 중" in str(exc.value)


def test_force_unlink_removes_a_file_a_plain_unlink_cannot(file_lock, tmp_path):
    """쓰기 권한 없는 디렉토리 안의 read-only 파일도 지운다 (맨 unlink 는 거부된다)."""
    tree = _read_only_tree(tmp_path / "tree")
    blob = tree / "objects" / "10" / "a9500e"
    with pytest.raises(OSError):
        blob.unlink()

    file_lock.force_unlink(blob)

    assert not blob.exists()


def test_force_unlink_absence_follows_the_caller_declaration(file_lock, tmp_path):
    """부재를 성공으로 볼지는 호출부의 뜻이다 — 기본은 `Path.unlink` 와 같은 raise."""
    missing = tmp_path / "gone.md"
    file_lock.force_unlink(missing, missing_ok=True)
    with pytest.raises(FileNotFoundError):
        file_lock.force_unlink(missing)


def test_force_unlink_raises_when_the_target_cannot_be_removed(file_lock, tmp_path):
    """지우지 못하면 예외 — 파일 축도 조용한 잔재를 만들지 않는다."""
    directory = tmp_path / "dir"
    directory.mkdir()

    with pytest.raises(OSError) as exc:
        file_lock.force_unlink(directory)

    assert str(directory) in str(exc.value)
    assert directory.exists()


def _delete_call_keywords(source: str, name: str) -> list[ast.Call]:
    """소스에서 `<name>=True` 키워드를 넘기는 호출만 모은다 (주석·문자열 아님·AST 판정)."""
    found: list[ast.Call] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (keyword.arg == name and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True):
                found.append(node)
    return found


def test_engine_cleanup_never_swallows_delete_failures():
    """엔진 어디에도 `ignore_errors=True` 호출이 없다 — 잔재는 loud 하거나 없거나다.

    산문(주석·docstring)에 그 관용구를 *설명* 하는 것은 판정 대상이 아니다(AST 로만 센다).
    """
    offenders = {
        path.name: len(_delete_call_keywords(path.read_text(encoding="utf-8"), "ignore_errors"))
        for path in sorted(TOOLS.glob("*.py"))
        if _delete_call_keywords(path.read_text(encoding="utf-8"), "ignore_errors")
    }
    assert offenders == {}, f"삭제 실패를 삼키는 호출이 남음: {offenders}"


# ── 소유자 전용 접근 제한 (POSIX 퍼미션 / Windows ACL·T-0711) ────────────────


@pytest.mark.skipif(
    not posix_mode_supported(),
    reason="POSIX 퍼미션이 무효인 환경 — 그 플랫폼의 수단(ACL)은 아래 분기 테스트가 본다.")
def test_restrict_to_owner_applies_posix_owner_only_permissions(file_lock, tmp_path):
    """POSIX 수단은 퍼미션 — 파일 0600·디렉토리 0700 이고 판정도 그것을 되읽는다."""
    target = tmp_path / "auth.json"
    target.write_text("{}", encoding="utf-8")
    directory = tmp_path / "home"
    directory.mkdir()

    file_lock.restrict_to_owner(target)
    file_lock.restrict_to_owner(directory)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert file_lock.owner_only_access(target) is True
    assert file_lock.owner_only_access(directory) is True
    os.chmod(target, 0o644)
    assert file_lock.owner_only_access(target) is False, \
        "그룹/타인 읽기가 열렸는데 소유자 전용으로 판정됨"


# 한국어 Windows 11 실측 형상 — `whoami /user /fo csv /nh` 한 줄과 계정 SID.
_PROBE_SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
_PROBE_WHOAMI = f'"DESKTOP-A1B2C3\\smahn","{_PROBE_SID}"\n'


class _FakeAcl:
    """`whoami`/`icacls` 를 갈아끼우는 주입 runner — 호출 argv 를 그대로 기록한다.

    `whoami_rc` 를 비0 으로 두면 **SID 조회 실패** 형상이 되고, 그때 주체가 계정 이름으로
    폴백하는지까지 같은 도구로 본다.
    """

    def __init__(self, *, whoami_rc: int = 0, icacls_rc: int = 0,
                 icacls_output: str = "", query_output: str | None = None):
        self.whoami_rc, self.icacls_rc = whoami_rc, icacls_rc
        self.icacls_output, self.query_output = icacls_output, query_output
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "whoami":
            return self.whoami_rc, (_PROBE_WHOAMI if self.whoami_rc == 0 else "")
        if len(argv) == 2 and self.query_output is not None:   # 조회(`icacls <path>`)
            return self.icacls_rc, self.query_output
        return self.icacls_rc, self.icacls_output

    @property
    def icacls_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[0] == "icacls"]


def test_restrict_to_owner_grants_by_sid_on_platforms_where_chmod_is_inert(
    file_lock, tmp_path,
):
    """ACL 플랫폼(Windows)에서는 `icacls` 로 같은 보장을 낸다 — 주체는 **SID** 다.

    `chmod 0600` 은 그 플랫폼에서 아무 제한도 걸지 않으므로(실측 `S_IMODE`=0o666) 단언을
    지우거나 skip 하는 대신 수단을 갈아끼운다. 계정 *이름*으로 주면 로케일·계정 표기에 따라
    icacls 가 해소에 실패한다(한국어 Windows 11 실측 `rc=1332` = `ERROR_NONE_MAPPED`) — SID 는
    그 표기에 무관하다. 디렉토리는 상속 ACE 로 줘 이후 생기는 항목도 소유자 전용이 되게 한다.
    """
    target = tmp_path / "prompt.md"
    target.write_text("검토 diff", encoding="utf-8")
    directory = tmp_path / "transport"
    directory.mkdir()
    acl = _FakeAcl()

    file_lock.restrict_to_owner(target, runner=acl, acl_platform=True)
    file_lock.restrict_to_owner(directory, runner=acl, acl_platform=True)

    assert acl.icacls_calls == [
        ["icacls", str(target), "/inheritance:r", "/grant:r", f"*{_PROBE_SID}:(F)"],
        ["icacls", str(directory), "/inheritance:r", "/grant:r",
         f"*{_PROBE_SID}:(OI)(CI)(F)"],
    ], f"ACL 적용 명령이 SID 기반 소유자 전용 부여가 아님: {acl.icacls_calls}"
    assert acl.calls[0][0] == "whoami", "SID 조회 없이 이름으로 부여했다"


def test_restrict_to_owner_falls_back_to_the_account_name_without_a_sid(
    file_lock, tmp_path,
):
    """SID 조회가 실패하면 계정 이름으로라도 건다 — 제한 없이 지나가지 않는다."""
    target = tmp_path / "prompt.md"
    target.write_text("검토 diff", encoding="utf-8")
    acl = _FakeAcl(whoami_rc=1)

    file_lock.restrict_to_owner(target, runner=acl, acl_platform=True)

    owner = file_lock.current_owner_principal()
    assert acl.icacls_calls == [[
        "icacls", str(target), "/inheritance:r", "/grant:r", f"{owner}:(F)",
    ]]


def test_restrict_to_owner_raises_when_the_acl_tool_fails(file_lock, tmp_path):
    """제한을 못 걸었으면 loud — 조용히 넘어가면 호출부가 격리를 믿고 비밀을 쓴다.

    실패 진단에는 **쓴 주체**가 함께 실린다 — `rc=1332`(계정 해소 실패)처럼 주체 표기가 원인인
    실패를 rc 숫자만 보고는 못 가른다(실측).
    """
    target = tmp_path / "prompt.md"
    target.write_text("검토 diff", encoding="utf-8")

    with pytest.raises(file_lock.AccessRestrictionError) as exc:
        file_lock.restrict_to_owner(
            target,
            runner=_FakeAcl(icacls_rc=1332, icacls_output="계정 이름과 보안 ID 간에 매핑이..."),
            acl_platform=True)

    assert str(target) in str(exc.value)
    assert "계정 이름과 보안 ID" in str(exc.value)
    assert f"*{_PROBE_SID}" in str(exc.value), "실패 진단에 쓴 주체가 없다"

    with pytest.raises(file_lock.AccessRestrictionError):
        file_lock.restrict_to_owner(
            target, runner=_raise_missing_tool, acl_platform=True)


def _raise_missing_tool(argv):
    raise FileNotFoundError(errno.ENOENT, "icacls 없음")


def test_owner_only_access_reads_the_acl_back_and_fails_closed(file_lock, tmp_path):
    """판정은 OS 에 되묻는다 — 현재 계정 ACE 하나뿐일 때만 참, 판정 불가는 거짓.

    되읽은 ACE 는 SID 가 이름으로 해소되면 이름으로, 아니면 SID 그대로 찍힌다 — 둘 다 현재
    계정으로 인정하되 다른 주체는 그대로 거부한다.
    """
    target = tmp_path / "prompt.md"
    target.write_text("검토 diff", encoding="utf-8")
    owner = file_lock.current_owner_principal()

    def _query(output: str, rc: int = 0):
        return _FakeAcl(icacls_rc=rc, query_output=output)

    assert file_lock.owner_only_access(
        target, runner=_query(f"{target} {owner}:(F)\n\n1개 파일을 처리했습니다\n"),
        acl_platform=True) is True
    assert file_lock.owner_only_access(
        target, runner=_query(f"{target} {_PROBE_SID}:(F)\n"),
        acl_platform=True) is True, "이름으로 해소되지 않은 SID ACE 를 남으로 판정했다"
    assert file_lock.owner_only_access(
        target,
        runner=_query(f"{target} {owner}:(F)\n              BUILTIN\\Users:(RX)\n"),
        acl_platform=True) is False, "다른 주체의 ACE 가 있는데 소유자 전용으로 판정됨"
    assert file_lock.owner_only_access(
        target, runner=_query(f"{target} S-1-5-21-9-9-9-500:(F)\n"),
        acl_platform=True) is False, "다른 계정 SID 를 현재 계정으로 판정했다"
    assert file_lock.owner_only_access(
        target, runner=_query("", rc=5), acl_platform=True) is False
    assert file_lock.owner_only_access(
        target, runner=_query("형식 밖 출력\n"), acl_platform=True) is False


def test_owner_sid_lookup_is_cached_for_the_real_runner(file_lock, monkeypatch):
    """실 조회는 프로세스당 한 번 — 파일마다 `whoami` 를 띄우지 않는다.

    주입 runner 는 캐시를 오염시키지도, 캐시에 오염되지도 않는다(테스트 소유 값).
    """
    monkeypatch.setattr(file_lock, "_CACHED_OWNER_SID", None, raising=False)
    spawns: list[list[str]] = []

    def _real(argv):
        spawns.append(list(argv))
        return 0, _PROBE_WHOAMI

    monkeypatch.setattr(file_lock, "_run_acl_command", _real)

    assert file_lock.current_owner_sid() == _PROBE_SID
    assert file_lock.current_owner_sid() == _PROBE_SID
    assert len(spawns) == 1, f"SID 조회가 매번 프로세스를 띄운다: {spawns}"
    assert file_lock.current_owner_sid(runner=_FakeAcl(whoami_rc=1)) is None
