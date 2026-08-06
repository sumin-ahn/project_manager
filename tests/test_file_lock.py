"""공용 배타 파일락 seam 회귀 (T-0561).

board(`board.lock`·`board-git.lock`)·pm_log(`log.lock`)·pm_relay(raw 장부 `<ledger>.lock`)가
각자 복제하던 플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)를 `file_lock.py` 하나로
수렴했다. 검증 축:

  1. seam 자체 — 경로/권한/획득·해제·close 순서, 실 프로세스 간 상호배제, 보유자 크래시 시
     OS 자동 해제(stale lock 없음).
  2. 소비 — 3 도구가 *같은 파일*의 seam 을 쓰고 각자의 락 경로 규약(0o600·`.local/log.lock`
     등)은 그대로 보존한다.
  3. 재-복제 차단 — 수렴한 3 도구에 플랫폼 락 분기가 되살아나면 red. 아직 수렴 안 한 사본은
     사유와 함께 등재해 이 가드가 후속 티켓에서 조여지게 둔다.
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
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
FILE_LOCK_PY = TOOLS / "file_lock.py"

SYNC_TIMEOUT = 120

# 아직 공용 seam 으로 수렴하지 않은 사본 — **구조적 면제가 아니라 이번 스코프 밖일 뿐**이다.
# (T-0561 은 board/pm_log/pm_relay 3 도구만 수렴했다.) 공용 seam 은 형제를 로드하지 않는 leaf 라
# 이 3 도구도 같은 방식으로 수렴할 수 있다 — 각 사본의 "독립 구현·import 금지" 주석은 seam 신설
# *이전*의 사유이므로 후속 수렴 때 코드와 함께 지운다. 하나씩 지우면 이 가드가 자동으로 조여지고,
# 목록 밖 새 사본이 생기면 red.
PENDING_DUPLICATE_LOCK_MODULES = {
    "pm_handoff.py": "대시보드 자체 파일락 — T-0561 스코프 밖·후속 수렴 대상",
    "worktree_pool.py": "리스 장부 자체 파일락 — T-0561 스코프 밖·후속 수렴 대상",
    "external_review.py": "라운드 장부 자체 파일락 — T-0561 스코프 밖·후속 수렴 대상",
}

# 플랫폼 락 프리미티브 (모듈 → 그 모듈의 락 호출 이름). 주석·문자열 아님·AST 로만 판정.
_LOCK_PRIMITIVES = {"fcntl": {"flock"}, "msvcrt": {"locking"}}


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


def test_lock_file_survives_the_critical_section(file_lock, tmp_path):
    """락 파일을 지우지 않는다 — 지우면 다른 프로세스가 쥔 inode 와 갈라져 배타성이 깨진다."""
    lock_path = tmp_path / "keep.lock"
    with file_lock.exclusive_file_lock(lock_path):
        assert lock_path.is_file()
    assert lock_path.is_file()


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


# ── 2. 소비 (3 도구가 같은 seam 파일을 쓴다) ────────────────────────────────

def test_board_binds_the_canonical_seam_at_import():
    """board 는 모듈 import 시점에 canonical `file_lock.py` 를 바인딩한다 (지연 로드 아님).

    락을 잡을 때마다 형제를 로드하면 board 를 fail-soft 로 소비하는 호출층이 사본 skew 를
    조용히 삼키는 경로가 생긴다 — import 경계 단일 fail-loud 로 그 확산을 막는다.
    """
    board = _load(TOOLS / "board.py", "board_file_lock_seam")
    assert Path(board.file_lock.__file__).resolve() == FILE_LOCK_PY.resolve()
    assert "with file_lock.exclusive_file_lock(" in (
        TOOLS / "board.py"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("tool", ("pm_log.py", "pm_relay.py"))
def test_lazy_consumers_load_the_canonical_seam(tool):
    """pm_log·pm_relay 는 write 경로에서만 seam 을 로드한다(읽기 경로 fail-soft 보존)."""
    module = _load(TOOLS / tool, f"{tool[:-3]}_file_lock_seam")
    assert Path(module._load_file_lock().__file__).resolve() == FILE_LOCK_PY.resolve()


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


# ── 3. 재-복제 차단 ───────────────────────────────────────────────────────

def test_platform_lock_branch_lives_only_in_the_shared_seam():
    """플랫폼 락 호출은 공용 seam + 아직 미수렴 사본(등재분)에만 있다.

    수렴한 board/pm_log/pm_relay 에 분기가 되살아나거나, 등재 밖 모듈에 새 사본이 생기면 red.
    미수렴 사본이 후속 티켓에서 수렴하면 이 목록을 함께 지운다.
    """
    measured = set(_modules_with_platform_lock_calls())
    expected = {"file_lock.py"} | set(PENDING_DUPLICATE_LOCK_MODULES)
    assert measured == expected, (
        f"플랫폼 락 분기 보유 모듈이 예상과 불일치 — 추가: {sorted(measured - expected)} / "
        f"사라짐(목록 정리 필요): {sorted(expected - measured)}"
    )
    assert all(reason.strip() for reason in PENDING_DUPLICATE_LOCK_MODULES.values())


def test_converged_tools_delegate_instead_of_reimplementing():
    """3 도구는 락 컨텍스트를 seam 위임으로만 연다 (자체 open+acquire 재구현 0)."""
    for tool in ("board.py", "pm_log.py", "pm_relay.py"):
        source = (TOOLS / tool).read_text(encoding="utf-8")
        assert "exclusive_file_lock(" in source, tool
        assert not re.search(r"def _[a-z_]*flock_(acquire|release)\b", source), tool


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
