"""local.conf writer 전수 직렬화 — 공유 락 하나로 lost update 를 닫는다 (T-0590 4차).

`local.conf` 를 쓰는 진입은 한 도구가 아니다: board init(최초 전체 생성·비파괴 병합), pm_update 의
upstream rev 기록, pm_import 의 키 writer(`_write_conf_keys` — pm_import 자신·pm_update 의
upstream_rev 기록·pm_config 의 upstream set 이 공유하는 백엔드). 이들은 **서로 다른 프로세스**로
같은 파일에 동시에 닿을 수 있다.

쓰기끼리만 직렬화하면 부족하다는 것이 이 파일의 출발점이다 — 각 write 가 원자적이어도, 커밋
**전에** 내용을 읽고 나중에 통째로 갈아끼우는 writer(init 병합·`_write_conf_keys` 의
temp+`os.replace`)가 그사이의 다른 결정을 읽지 못하면 그 결정은 교체본에 없어 사라진다. 그래서
락의 단위는 "쓰기" 가 아니라 "이 conf 를 읽고 쓰는 구간" 이고, 경로 유도까지 공용
seam(`pm_engine.infra.fs.conf_lock_path`) 한 곳이 소유한다(사본이 갈리면 같은 conf 에 다른 락 파일 =
직렬화 없음).

R6 에서 그 단위가 한 번 더 정확해졌다 — 쓰기만 잠그고 **현재 상태 읽기와 계획**을 락 밖에 두면,
원자적으로 써도 그사이 들어온 결정을 옛 값으로 덮는다(재-import 의 사용자 키 보존). 계약은
read → 판단/계획 → write → postcondition 전체의 직렬화다.

검증 축:
  1. inventory ratchet — canonical tools 를 AST 로 훑어 conf writer 를 기계적으로 세고, 각 writer 의
     처분(직접 락·상위 락 전제·위임)을 못박는다. 새 writer 가 생기면 red. 중첩 0 은 락 **구간
     본문**만 따로 훑어 잰다(락 밖 폴백 위임을 중첩으로 오판하지 않게).
  2. 경로 규약 — 모든 진입이 같은 conf 에서 같은 락 파일에 도달한다.
  3. 실 경쟁 — 배리어로 겹치게 돌려 **어느 순서로 겹쳐도** 양쪽 결정이 남는지 본다(helper 대역이
     아니라 실 writer 두 개를 실제 스레드로 경쟁시킨다). 계획이 락 안인지는 배리어를 *현재 상태를
     읽는 지점* 에 걸어 잰다.
  4. 비파괴 — 직렬화가 기존 주석·미지 키·개행 없는 마지막 줄을 바꾸지 않는다.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from pm_engine.infra import fs, python_env
from _engine_tree import engine_python_files

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

SYNC_TIMEOUT = 60          # 배리어 대기 상한(초) — 데드락이면 실패로 끝난다.
BLOCKED_PROBE = 0.5        # "막혀 있음" 을 관측하는 창(초).


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_conf_lock", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def file_lock():
    """공용 잠금 모듈(`pm_engine.infra.fs`)."""
    return fs


@pytest.fixture(scope="module")
def board():
    return _load("board")


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update")


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import")


def _parse_conf(text: str) -> dict[str, str]:
    """local.conf 활성 키만 파싱(주석 제외·last-wins) — 엔진 reader 와 동치."""
    conf: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        conf[key.strip()] = value.strip()
    return conf


def test_t0927_migration_lock_replans_new_user_value(tmp_path, pm_update, pm_import,
                                                   monkeypatch):
    conf = tmp_path / ".project_manager" / "local.conf"
    conf.parent.mkdir()
    conf.write_text("ctx.stop_pct=10\n", encoding="utf-8")

    @contextmanager
    def competing_writer(path):
        # 계획을 읽고 락을 얻는 사이 사용자가 새 키를 저장한 실제 경합 순서다.
        pm_import._write_conf_keys(path, {"ctx.precompact_at_pct": "87"})
        with pm_import._local_conf_write_lock(path):
            yield

    monkeypatch.setattr(pm_update, "_local_conf_write_lock", competing_writer)
    pm_update.migrate_renamed_conf_keys(tmp_path, write=True)
    values = _parse_conf(conf.read_text(encoding="utf-8"))
    assert values["ctx.precompact_at_pct"] == "87"
    assert "ctx.stop_pct" not in values


# ── 축 1: writer inventory ratchet ──────────────────────────────────────────
#
# 처분(disposition):
#   HOLDS_LOCK        — 자기가 공용 락 구간을 연다(읽기·판정·쓰기·검증이 그 안).
#   UNDER_CALLER_LOCK — 실제 쓰기만 하는 helper. 호출부가 락을 쥔 채 부른다(자기가 다시 잡으면
#                       재진입 = 정의되지 않은 동작).
#   DELEGATES         — 직접 안 쓰고 HOLDS_LOCK writer 에 넘긴다(자기는 락을 잡지 않는다 —
#                       잡으면 그 안에서 다시 잡혀 데드락).
HOLDS_LOCK = "holds-lock"
UNDER_CALLER_LOCK = "under-caller-lock"
DELEGATES = "delegates"

LOCK_SEAM_CALL = "_local_conf_write_lock("

# 등재된 conf writer 전수. 값 = (처분, 사유). 스캐너가 새 writer 를 잡으면 이 표와 어긋나 red 다.
LOCAL_CONF_WRITERS: dict[tuple[str, str], tuple[str, str]] = {
    ("board.py", "_write_init_local_conf"):
        (HOLDS_LOCK, "init 최초 전체 생성 + 비파괴 병합(read→merge→write_text)"),
    ("pm_update.py", "record_upstream_revs"):
        (HOLDS_LOCK, "형상 판정(upstream=)→updates 계획→키 write→검증이 한 구간"),
    ("pm_update.py", "migrate_renamed_conf_keys"):
        (HOLDS_LOCK, "구표기 판정→이름 이동 계획→키 write+옛 줄 삭제가 한 구간"),
    ("pm_import.py", "_write_conf_keys"):
        (HOLDS_LOCK, "키 단위 RMW 백엔드(pm_import·pm_update·pm_config 공용)"),
    ("pm_import.py", "_write_conf_keys_locked"):
        (UNDER_CALLER_LOCK, "임계 구간 본문(temp write + os.replace + 실효값 검증)"),
    ("pm_import.py", "sync_local_conf"):
        (DELEGATES, "import 직후 operational 키 정렬"),
    ("pm_import.py", "reapply_preserved_conf_keys"):
        (HOLDS_LOCK, "현재 conf 읽기→보존 대상 계산→키 write→검증이 한 구간"),
    ("pm_import.py", "record_upstream"):
        (DELEGATES, "upstream 값 기록"),
    ("pm_import.py", "record_upstream_rev"):
        (DELEGATES, "import 시 baseline rev 기록"),
    ("pm_import.py", "record_opencode_model"):
        (DELEGATES, "해소된 opencode 모델 기록"),
    ("pm_config.py", "cmd_upstream"):
        (DELEGATES, "`pm-config upstream set` → pm_import._write_conf_keys"),
    ("pm_update.py", "apply_conf"):
        (HOLDS_LOCK, "제안 값→반영 단계 계획 전부→conf 키 쓰기→단계 기록이 공용 락 안 한 흐름"),
    ("pm_update.py", "prune_unavailable_reasoning"):
        (HOLDS_LOCK, "판정(모델 목록 조회)은 락 밖, 락 안에서 다시 읽어 판정 때 값과 대조→키 삭제"),
}

# conf 좌표로 인정하는 식별자 — 이 이름이 코드(주석·docstring 아님)에 실제로 등장해야 후보다.
_CONF_COORDINATES = {"LOCAL_CONF", "local_conf", "conf_path"}
# 파일을 실제로 바꾸는 호출 + conf writer 로 위임하는 호출.
_WRITE_CALLS = {
    "write_text", "append_atomic", "replace", "write", "open",
    "_append_local_conf_atomic", "_write_conf_keys", "_write_conf_keys_locked",
}


def _called_name(call: ast.Call) -> str | None:
    """호출 노드의 피호출 이름 — 이름 호출(`f()`)·속성 호출(`mod.f()`) 둘 다 같은 폭으로 본다."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _function_scopes(tree: ast.Module):
    """top-level·중첩 함수를 (이름, 노드)로 훑는다 (클래스 메서드 포함)."""
    found: list[tuple[str, ast.AST]] = []

    def walk(nodes, prefix: str = "") -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{node.name}" if prefix else node.name
                found.append((name, node))
                walk(node.body, name)
            elif isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}.{node.name}" if prefix else node.name)

    walk(tree.body)
    return found


def _measured_conf_writers() -> dict[tuple[str, str], set[str]]:
    """canonical tools 전수에서 conf writer 를 기계적으로 센다 (AST — 주석·문자열 아님)."""
    measured: dict[tuple[str, str], set[str]] = {}
    for path in engine_python_files(TOOLS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, node in _function_scopes(tree):
            identifiers = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            # conf 좌표를 이름으로 쓰거나, 이름 자체가 conf writer 를 표방하는 함수만 후보.
            direct_writer = any(isinstance(n, ast.Call) and _called_name(n) in {"_write_conf_keys", "_write_conf_keys_locked", "_append_local_conf_atomic"} for n in ast.walk(node))
            if not (direct_writer or identifiers & _CONF_COORDINATES
                    or re.search(r"conf_keys|local_conf", name)):
                continue
            calls = {
                _called_name(call) for call in ast.walk(node)
                if isinstance(call, ast.Call) and _called_name(call) in _WRITE_CALLS
            }
            if calls:
                measured[(path.relative_to(TOOLS).as_posix(), name)] = calls
    return measured


def _source_of(module_file: str, function: str) -> str:
    tree = ast.parse((TOOLS / module_file).read_text(encoding="utf-8"))
    for name, node in _function_scopes(tree):
        if name == function:
            return ast.unparse(node)
    raise AssertionError(f"함수 없음: {module_file}::{function}")


def test_the_writer_inventory_is_complete():
    """conf writer 전수가 등재와 일치한다 — 새 writer 가 조용히 생기면 red.

    등재를 늘리려면 처분(직접 락/상위 락 전제/위임)을 함께 선언해야 하므로, 락 없는 writer 가
    "그냥 추가" 되지 않는다.
    """
    measured = set(_measured_conf_writers())
    expected = set(LOCAL_CONF_WRITERS)
    assert measured == expected, (
        f"conf writer 등재 불일치 — 신규: {sorted(measured - expected)} / "
        f"사라짐(등재 정리 필요): {sorted(expected - measured)}")
    assert all(reason.strip() for _disposition, reason in LOCAL_CONF_WRITERS.values())


@pytest.mark.parametrize(
    ("coordinate", "disposition"),
    [(key, value[0]) for key, value in LOCAL_CONF_WRITERS.items()],
    ids=[f"{module}::{name}" for module, name in LOCAL_CONF_WRITERS],
)
def test_each_writer_matches_its_declared_lock_disposition(coordinate, disposition):
    """직접 락을 여는 writer 만 seam 을 부르고, 나머지는 **부르지 않는다**(재진입/데드락 차단)."""
    module_file, function = coordinate
    source = _source_of(module_file, function)
    if disposition == HOLDS_LOCK:
        assert LOCK_SEAM_CALL in source, (
            f"{module_file}::{function} 이 공용 락 없이 conf 를 쓴다")
    else:
        assert LOCK_SEAM_CALL not in source, (
            f"{module_file}::{function} 은 상위/피위임자가 이미 쥔 락을 다시 잡는다(재진입)")


def test_lock_holders_do_not_nest_another_conf_writer():
    """락 구간 안에서 다른 락-보유 writer 를 부르지 않는다 — 중첩은 곧 데드락이다."""
    holders = {name for (module, name), (disposition, _reason)
               in LOCAL_CONF_WRITERS.items() if disposition == HOLDS_LOCK}
    for (module_file, function), (disposition, _reason) in LOCAL_CONF_WRITERS.items():
        if disposition != HOLDS_LOCK:
            continue
        tree = ast.parse(_source_of(module_file, function))
        called = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        nested = called & (holders - {function})
        assert not nested, f"{module_file}::{function} 이 락 안에서 {sorted(nested)} 를 부른다"


def _callers_of(function: str) -> set[tuple[str, str]]:
    """canonical tools 전수에서 `function` 을 부르는 함수 좌표 — 이름 호출·속성 호출 둘 다.

    호출부가 다른 도구일 수도 있다(pm_update 가 로드한 pm_import 사본의 임계 구간 본문을
    `pm_import._write_conf_keys_locked(...)` 로 부른다) — 같은 파일만 보면 그 교차-도구 호출이
    ratchet 밖으로 빠진다.
    """
    callers: set[tuple[str, str]] = set()
    for path in engine_python_files(TOOLS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, node in _function_scopes(tree):
            if name == function:
                continue          # 자기 정의(재귀 아님) — 호출부가 아니다.
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and _called_name(call) == function:
                    callers.add((path.relative_to(TOOLS).as_posix(), name))
    return callers


def test_helpers_under_the_caller_lock_are_only_called_by_lock_holders():
    """상위 락 전제 helper 는 락-보유 writer 밖에서 불리지 않는다(락 밖 write 0)."""
    for (module_file, function), (disposition, _reason) in LOCAL_CONF_WRITERS.items():
        if disposition != UNDER_CALLER_LOCK:
            continue
        callers = _callers_of(function)
        assert callers, f"{module_file}::{function} 호출부 0 — 등재가 낡았다"
        for caller in sorted(callers):
            assert LOCAL_CONF_WRITERS.get(caller, (None, ""))[0] == HOLDS_LOCK, (
                f"{caller[0]}::{caller[1]} 가 락 없이 {function} 로 conf 를 쓴다")


def test_no_lock_holder_calls_another_conf_writer_inside_its_lock_body():
    """락 **구간 본문** 안에서 다른 락-보유 writer 를 부르지 않는다 — 중첩 0의 정밀 측정.

    앞의 등재 단위 검사는 함수 전체를 보므로, 폴백 분기처럼 락 **밖**에 있는 위임 호출까지
    중첩으로 오판한다(pm_update 는 구세대 pm_import 사본에서 자기-락 writer 로 물러난다 — 그
    호출은 우리 락 밖이어야 한다). 여기서는 `with _local_conf_write_lock(...)` 의 본문만 훑고
    이름 호출·속성 호출을 함께 본다.
    """
    holders = {name for (_module, name), (disposition, _reason)
               in LOCAL_CONF_WRITERS.items() if disposition == HOLDS_LOCK}
    for (module_file, function), (disposition, _reason) in LOCAL_CONF_WRITERS.items():
        if disposition != HOLDS_LOCK:
            continue
        bodies = 0
        for node in ast.walk(ast.parse(_source_of(module_file, function))):
            if not isinstance(node, ast.With) or not any(
                isinstance(item.context_expr, ast.Call)
                and _called_name(item.context_expr) == "_local_conf_write_lock"
                for item in node.items
            ):
                continue
            bodies += 1
            called = {
                _called_name(call)
                for statement in node.body for call in ast.walk(statement)
                if isinstance(call, ast.Call)
            }
            nested = called & (holders - {function})
            assert not nested, (
                f"{module_file}::{function} 이 자기 락 구간 안에서 {sorted(nested)} 를 부른다")
        assert bodies, f"{module_file}::{function} 에 측정 가능한 락 구간이 없다"


# ── 축 2: 경로 규약 (같은 conf → 같은 락 파일) ──────────────────────────────

def test_every_entry_derives_the_same_lock_path(
    board, pm_update, pm_import, file_lock, tmp_path,
):
    """세 진입이 같은 conf 에 대해 같은 락 파일에 도달한다 — 유도 규칙 사본 0.

    pm_update 는 경로를 직접 유도하지 않는다 — 공용 seam 의 `local_conf_write_lock` 만 쓴다(T-0951)."""
    conf = tmp_path / ".project_manager" / "local.conf"
    expected = conf.parent / ".local" / "local-conf.lock"
    assert file_lock.conf_lock_path(conf) == expected
    assert board._local_conf_lock_path(conf) == expected
    assert pm_import._local_conf_lock_path(conf) == expected


def test_the_path_rule_lives_only_in_the_shared_seam():
    """락 파일명 리터럴은 공용 seam(`pm_engine.infra.fs`) 한 곳뿐이다.

    pm_import 의 구세대-사본 인라인 폴백은 T-0963 이 지웠다 — 공용 잠금이 패키지 import 라
    구세대 사본이 섞일 자리가 없다.
    """
    owners = {
        path.relative_to(TOOLS).as_posix() for path in engine_python_files(TOOLS)
        if "local-conf.lock" in path.read_text(encoding="utf-8")
    }
    assert owners == {"pm_engine/infra/fs.py"}, (
        f"락 경로 유도 사본이 늘었다: {sorted(owners)}")


# ── 축 3: 실 경쟁 (배리어로 겹치는 두 실 writer) ────────────────────────────

def _conf_at(tmp_path: Path, text: str) -> Path:
    conf = tmp_path / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(text, encoding="utf-8")
    return conf


class _Barrier:
    """한쪽이 임계 구간에 **들어간 뒤** 다른 쪽을 풀어 주는 배리어."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.done = threading.Event()
        self.error: BaseException | None = None

    def run(self, work) -> threading.Thread:
        def _target():
            try:
                work()
            except BaseException as exc:  # noqa: BLE001 — 스레드 실패를 본체로 올린다.
                self.error = exc
            finally:
                self.done.set()

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        return thread

    def join(self, thread: threading.Thread) -> None:
        thread.join(SYNC_TIMEOUT)
        assert not thread.is_alive(), "스레드가 끝나지 않았다(데드락 의심)"
        if self.error is not None:
            raise self.error


def _init_merge(board, monkeypatch, conf):
    """board init 의 비파괴 병합(read→merge→write)을 대상 conf 에 태우는 클로저."""
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    monkeypatch.setattr(python_env, "_detect_py", lambda: "python3")
    return board._write_init_local_conf


def test_a_concurrent_key_writer_never_loses_the_init_merge(
    board, pm_import, monkeypatch, tmp_path,
):
    """RMW writer 가 **먼저 읽고** 나중에 교체해도 그사이 init 병합 결정이 사라지지 않는다.

    이것이 4차 지적의 원문 형상이다 — 각자 자기 쓰기만 직렬화하면 이 순서에서 한쪽 결정이 통째
    교체에 덮인다. 같은 락을 공유해야 두 결정이 모두 남는다.
    """
    conf = _conf_at(tmp_path, "session=pm\n")
    barrier = _Barrier()
    real_set_conf_keys = pm_import._set_conf_keys

    def _blocking_set(text, updates):
        # 락을 쥔 채 "읽은 뒤" 멈춘다 — 경쟁 상대가 이 창에 끼어들 기회를 실제로 준다.
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_set_conf_keys(text, updates)

    monkeypatch.setattr(pm_import, "_set_conf_keys", _blocking_set)
    rmw = barrier.run(
        lambda: pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"}))
    assert barrier.entered.wait(SYNC_TIMEOUT), "RMW writer 가 임계 구간에 못 들어갔다"

    merged = threading.Event()
    commit = _init_merge(board, monkeypatch, conf)

    def _merge():
        commit()
        merged.set()

    merge_thread = threading.Thread(target=_merge, daemon=True)
    merge_thread.start()
    assert not merged.wait(BLOCKED_PROBE), (
        "RMW 가 읽고 쓰는 사이에 init 병합이 끼어들었다 — 락을 공유하지 않는다")

    barrier.resume.set()
    barrier.join(rmw)
    merge_thread.join(SYNC_TIMEOUT)
    assert not merge_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.rev"] == "rev-2", "RMW 의 결정이 사라졌다"
    assert parsed["runtime.py"] == "python3", "init 병합 결정이 교체에 덮였다"
    assert parsed["session"] == "pm"


def test_a_concurrent_key_writer_waits_for_an_in_flight_init_merge(
    board, pm_import, monkeypatch, tmp_path,
):
    """반대 순서 — init 병합이 임계 구간에 있으면 RMW writer 가 기다렸다 그 결과 위에 쓴다."""
    conf = _conf_at(tmp_path, "session=pm\n")
    barrier = _Barrier()
    commit = _init_merge(board, monkeypatch, conf)
    real_set_conf_keys = board._set_conf_keys

    def _blocking_set(text, updates):
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_set_conf_keys(text, updates)

    monkeypatch.setattr(board, "_set_conf_keys", _blocking_set)
    merge = barrier.run(commit)
    assert barrier.entered.wait(SYNC_TIMEOUT), "init 병합이 임계 구간에 못 들어갔다"

    written = threading.Event()

    def _rmw():
        pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"})
        written.set()

    rmw_thread = threading.Thread(target=_rmw, daemon=True)
    rmw_thread.start()
    assert not written.wait(BLOCKED_PROBE), (
        "init 병합이 진행 중인데 RMW 가 먼저 교체했다 — 락을 공유하지 않는다")

    barrier.resume.set()
    barrier.join(merge)
    rmw_thread.join(SYNC_TIMEOUT)
    assert not rmw_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.rev"] == "rev-2"
    assert parsed["runtime.py"] == "python3"
    assert parsed["session"] == "pm"


def test_init_merge_reads_the_conf_it_writes(board, monkeypatch, tmp_path):
    """병합 판단(어느 기본키가 없나)은 잠그고 쓰는 그 conf 를 읽는다 — 앵커 트리의 conf 가 아니다.

    앵커 트리(`board.REPO`)에 기본키가 다 든 conf 가 있어도, 대상 conf 에 없는 기본키는 추가된다.
    """
    anchor_tree = tmp_path / "anchor"
    _conf_at(anchor_tree, (
        "runtime.py=oldpy\n"
        "test.cmd=oldpy -m pytest\n"
        "project.name=anchor\n"
        "ctx.checkpoint_at_pct=1\n"
        "ctx.precompact_at_pct=2\n"
        "ctx.compact_at_pct=3\n"
    ))
    conf = _conf_at(tmp_path / "target", "session=pm\n")
    monkeypatch.setattr(board, "REPO", anchor_tree)
    commit = _init_merge(board, monkeypatch, conf)

    commit()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["runtime.py"] == "python3"
    assert parsed["session"] == "pm"


# ── 축 3'': 계획도 락 안이다 (stale plan 경쟁·R5 재현) ──────────────────────
#
# 앞의 경쟁들은 "쓰기끼리" 겹쳤다. 이 절은 한 단계 앞을 본다 — 현재 conf 를 **락 밖에서** 읽어
# 세운 계획은 커밋 시점엔 이미 낡아, 원자적으로 쓰더라도 그사이 들어온 결정을 잘못된 값/분기로
# 덮거나 빠뜨린다. 그래서 락의 단위는 read → 판단/계획 → write → postcondition 전체다.

def test_preserve_does_not_revive_a_backup_value_over_a_decision_made_meanwhile(
    board, pm_import, monkeypatch, tmp_path,
):
    """재-import 의 사용자 키 보존이 **그사이 생긴 결정**을 백업의 옛 값으로 덮지 않는다.

    형상: 백업에 `runtime.py=oldpy` 가 있고 대상 conf 에는 아직 그 키가 없다. 보존 재병합이
    "현재 conf 에 없는 키" 판정을 락 밖에서 해 두면, 그 사이 init 병합이 `python3` 를 기록해도
    낡은 계획이 `oldpy` 로 되돌린다(방금 정한 값을 무음 롤백). 판정을 락 안에서 하면 새 결정은
    그대로 남고 충돌하지 않는 보존 대상만 복원된다.
    """
    conf = _conf_at(tmp_path, "session=pm\n")
    backup = "runtime.py=oldpy\nmy_custom_key=값\n"
    barrier = _Barrier()
    commit = _init_merge(board, monkeypatch, conf)
    real_set_conf_keys = board._set_conf_keys

    def _blocking_set(text, updates):
        # init 병합이 락을 쥔 채 **아직 쓰지 않은** 창 — 보존 재병합이 이 창에서 계획을 세우면
        # 그 계획은 곧 낡는다.
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_set_conf_keys(text, updates)

    monkeypatch.setattr(board, "_set_conf_keys", _blocking_set)
    merge = barrier.run(commit)
    assert barrier.entered.wait(SYNC_TIMEOUT), "init 병합이 임계 구간에 못 들어갔다"

    preserved = threading.Event()

    def _preserve():
        pm_import.reapply_preserved_conf_keys(conf.parent.parent, backup)
        preserved.set()

    preserve_thread = threading.Thread(target=_preserve, daemon=True)
    preserve_thread.start()
    assert not preserved.wait(BLOCKED_PROBE), (
        "init 병합 임계 구간에 보존 재병합이 끼어들었다 — 락을 공유하지 않는다")

    barrier.resume.set()
    barrier.join(merge)
    preserve_thread.join(SYNC_TIMEOUT)
    assert not preserve_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["runtime.py"] == "python3", (
        "백업의 옛 값이 방금 기록된 결정을 덮었다(락 밖에서 세운 계획)")
    assert parsed["my_custom_key"] == "값", "충돌하지 않는 보존 대상이 사라졌다"
    assert parsed["session"] == "pm"


def test_an_in_flight_preserve_blocks_a_later_merge_and_keeps_both_decisions(
    board, pm_import, monkeypatch, tmp_path,
):
    """반대 순서 — 보존 재병합이 **현재 상태를 읽는 지점부터** 락 안이라 뒤 init 병합이 기다린다.

    배리어를 쓰기 지점이 아니라 *대상 conf 의 현재 상태 파싱* 지점에 둔다 — 그 읽기가 락 밖이면
    init 병합이 이 창에서 즉시 끝나 버린다(red). 락 안이면 순서가 강제되고 두 결정
    (보존 대상 + init 병합)이 모두 남는다.
    """
    conf = _conf_at(tmp_path, "session=pm\n")
    backup = ("my_custom_key=값\nadditional_reviewer.harness=codex\n"
              "additional_reviewer.model=gpt-5.6-sol\n")
    commit = _init_merge(board, monkeypatch, conf)
    barrier = _Barrier()
    real_parse = pm_import._parse_conf_keys

    def _blocking_parse(text):
        if text != backup and not barrier.entered.is_set():
            # 백업 텍스트가 아닌 첫 파싱 = 대상 conf 의 현재 상태 읽기(=계획의 입력).
            barrier.entered.set()
            assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_parse(text)

    monkeypatch.setattr(pm_import, "_parse_conf_keys", _blocking_parse)
    preserve = barrier.run(
        lambda: pm_import.reapply_preserved_conf_keys(conf.parent.parent, backup))
    assert barrier.entered.wait(SYNC_TIMEOUT), "보존 재병합이 현재 상태를 읽지 않았다"

    done = threading.Event()

    def _merge():
        commit()
        done.set()

    merge_thread = threading.Thread(target=_merge, daemon=True)
    merge_thread.start()
    assert not done.wait(BLOCKED_PROBE), (
        "보존 재병합이 현재 상태를 읽는 사이 init 병합이 끼어들었다 — 읽기가 락 밖이다")

    barrier.resume.set()
    barrier.join(preserve)
    merge_thread.join(SYNC_TIMEOUT)
    assert not merge_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["my_custom_key"] == "값"
    assert parsed["additional_reviewer.harness"] == "codex"
    assert parsed["runtime.py"] == "python3", "init 병합 결정이 보존 교체에 덮였다"


def _pm_update_rev_writer(pm_update, monkeypatch, rev: str):
    """`record_upstream_revs` 가 쓸 pm_import 사본을 고정하고 source rev 만 못박는다.

    실 코드는 호출마다 pm_import 사본을 새로 로드한다(캐시 없음) — 경쟁 상대(테스트가
    monkeypatch 한 fixture 사본)와 **다른 인스턴스**여야 배리어가 한쪽만 멈춘다. git repo 를
    만들지 않기 위해 rev 읽기만 대체하고, 형상 분류·키 writer·postcondition 은 실물 그대로 쓴다.
    """
    writer = _load("pm_import")
    monkeypatch.setattr(writer, "read_upstream_rev", lambda source_root: rev)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: writer)
    return writer


def _run_rev_record_against_an_upstream_flip(
    pm_update, pm_import, monkeypatch, tmp_path, *, stored: str, flipped: str,
):
    """`upstream` 플립이 먼저 커밋되는 순서를 강제하고 rev 기록 결과를 돌려준다.

    플립(=`pm-config upstream set` 백엔드)이 락을 쥔 채 아직 교체하지 않은 창에서 rev 기록이
    출발한다 — 두 writer 가 같은 락을 공유하지 않으면 나중 교체본이 앞선 결정을 지운다. 반환은
    (record_upstream_revs 반환값, 최종 conf 파싱).
    """
    conf = _conf_at(tmp_path, f"session=pm\nupstream.path={stored}\n")
    _pm_update_rev_writer(pm_update, monkeypatch, "rev-new")
    barrier = _Barrier()
    real_set_conf_keys = pm_import._set_conf_keys

    def _blocking_set(text, updates):
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_set_conf_keys(text, updates)

    monkeypatch.setattr(pm_import, "_set_conf_keys", _blocking_set)
    flip = barrier.run(lambda: pm_import._write_conf_keys(conf, {"upstream.path": flipped}))
    assert barrier.entered.wait(SYNC_TIMEOUT), "upstream 플립이 임계 구간에 못 들어갔다"

    recorded: list[tuple[bool, dict[str, str]]] = []
    done = threading.Event()

    def _record():
        recorded.append(
            pm_update.record_upstream_revs(conf.parent.parent, tmp_path / "src"))
        done.set()

    rev_thread = threading.Thread(target=_record, daemon=True)
    rev_thread.start()
    assert not done.wait(BLOCKED_PROBE), (
        "플립의 임계 구간에 rev 기록이 끼어들었다 — 락을 공유하지 않는다")

    barrier.resume.set()
    barrier.join(flip)
    rev_thread.join(SYNC_TIMEOUT)
    assert not rev_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.path"] == flipped, "플립이 rev 기록에 덮였다"
    return recorded[0], parsed


@pytest.mark.parametrize(
    ("stored", "flipped"),
    [
        ("/local/upstream", "https://example.invalid/u.git"),
        ("https://example.invalid/u.git", "/local/upstream"),
    ],
)
def test_rev_record_and_an_upstream_flip_both_survive(
    pm_update, pm_import, monkeypatch, tmp_path, stored, flipped,
):
    """플립과 rev 기록이 겹쳐도 양쪽 결정이 남는다 — 두 rev 키는 형상과 무관하게 함께 쓰인다.

    한쪽만 남으면(플립이 덮이거나 관찰값이 누락되면) 두 키가 영구히 어긋나 정상 흡수 직후에도
    drift 거짓 경보가 상시 뜬다. 기록 자체는 형상을 읽지 않으므로 어느 방향 플립이든 결과가
    같다(T-0908 — 두 키의 writer 는 record_upstream_revs 하나).
    """
    (changed, updates), parsed = _run_rev_record_against_an_upstream_flip(
        pm_update, pm_import, monkeypatch, tmp_path, stored=stored, flipped=flipped)

    assert changed is True
    assert updates == {"upstream.rev": "rev-new", "upstream.seen_rev": "rev-new"}
    assert parsed["upstream.rev"] == "rev-new"
    assert parsed["upstream.seen_rev"] == "rev-new", (
        "겹친 실행에서 관찰값이 누락됐다(두 키 어긋남)")
    assert parsed["upstream.path"] == flipped, "플립 결정이 rev 기록에 덮였다"


def _vanishing_conf_lock(module, monkeypatch):
    """락 구간에 들어간 **직후** 대상 conf 가 사라지는 상황 — 존재 판정이 락 안인지의 실측."""
    @contextmanager
    def _lock(conf_path):
        Path(conf_path).unlink()
        yield None

    monkeypatch.setattr(module, "_local_conf_write_lock", _lock)


def test_rev_record_skips_an_absent_conf_without_leaving_a_lock_file(
    pm_update, monkeypatch, tmp_path, capsys,
):
    """conf 자체가 없는 형상(init 전·출하 템플릿)에는 락 파일도 만들지 않는다(발자국 0)."""
    dest = tmp_path / "dest"
    (dest / ".project_manager").mkdir(parents=True)
    _pm_update_rev_writer(pm_update, monkeypatch, "rev-new")

    assert pm_update.record_upstream_revs(dest, tmp_path / "src") == (False, {})

    assert not (dest / ".project_manager" / ".local").exists(), (
        "conf 없는 트리에 락 파일을 남겼다")
    assert "local.conf 없음" in capsys.readouterr().err


@pytest.mark.parametrize("entry", ("rev-record", "preserve"))
def test_the_existence_check_inside_the_lock_is_the_authoritative_one(
    pm_update, pm_import, monkeypatch, tmp_path, entry,
):
    """락 밖 단축은 "쓰지 않는다" 만 결정한다 — 락 안에서 사라진 conf 는 되살리지 않는다.

    락 밖 판정만 믿고 쓰면 그사이 사라진/교체된 conf 를 되살려 남의 결정을 지운 자리에 옛 내용을
    박는다. 두 진입 모두 락 안 재판정이 권위이며, 그 경로는 graceful 생략이다.
    """
    conf = _conf_at(tmp_path, "session=pm\nupstream.path=/local/upstream\n")
    if entry == "rev-record":
        _pm_update_rev_writer(pm_update, monkeypatch, "rev-new")
        _vanishing_conf_lock(pm_update, monkeypatch)
        assert pm_update.record_upstream_revs(conf.parent.parent, tmp_path / "src") == (False, {})
    else:
        _vanishing_conf_lock(pm_import, monkeypatch)
        assert pm_import.reapply_preserved_conf_keys(
            conf.parent.parent, "my_custom_key=값\n") is False
    assert not conf.exists(), "락 안에서 사라진 conf 를 되살려 썼다"


# ── 축 3': 락 획득의 실측 (경로까지) ────────────────────────────────────────

def _spy_lock(module, monkeypatch, taken: list):
    """그 도구가 쓰는 file_lock 사본의 공용 conf 락을 감시한다(실 락은 그대로 건다)."""
    real = module.local_conf_write_lock

    def _spy(conf_path, **kwargs):
        taken.append(str(module.conf_lock_path(conf_path)))
        return real(conf_path, **kwargs)

    monkeypatch.setattr(module, "local_conf_write_lock", _spy)


def test_board_init_write_takes_the_shared_lock(board, monkeypatch, tmp_path):
    """init 의 전체 write(최초 생성)도 공용 락 안이다 — 존재 판정과 생성 사이 창을 닫는다."""
    conf = tmp_path / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    monkeypatch.setattr(python_env, "_detect_py", lambda: "python3")
    taken: list[str] = []
    _spy_lock(board.fs, monkeypatch, taken)

    board._write_init_local_conf()

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]
    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["test.cmd"] == python_env.default_pytest_cmd("python3")
    # 세션·prefix 는 per-clone conf 의 키가 아니다 (T-0779).
    assert "session" not in parsed and "prefix" not in parsed


def test_key_writer_takes_the_shared_lock(pm_import, monkeypatch, tmp_path):
    """`_write_conf_keys` 도 같은 락 파일을 잡는다(pm_import·pm_update·pm_config 공용 백엔드)."""
    conf = _conf_at(tmp_path, "session=pm\n")
    taken: list[str] = []
    _spy_lock(pm_import.fs, monkeypatch, taken)

    assert pm_import._write_conf_keys(conf, {"upstream.path": "/u"}) is True

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]


def test_pm_update_rev_record_takes_the_shared_lock(pm_update, monkeypatch, tmp_path):
    """pm_update 의 upstream rev 기록도 공용 락 안이다(형상 판정→계획→write 가 한 구간)."""
    conf = _conf_at(tmp_path, "session=pm\nupstream.path=/somewhere\n")
    _pm_update_rev_writer(pm_update, monkeypatch, "rev-new")
    taken: list[str] = []
    _spy_lock(pm_update.fs, monkeypatch, taken)

    assert pm_update.record_upstream_revs(conf.parent.parent, tmp_path / "src")[0]

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]
    assert _parse_conf(conf.read_text(encoding="utf-8"))["upstream.rev"] == "rev-new"


# ── 축 4: 비파괴 (직렬화가 바이트 계약을 바꾸지 않는다) ─────────────────────

@pytest.mark.parametrize(
    "existing",
    (
        pytest.param("# 사용자 주석\nsession=pm\nmy_custom_key=값\n", id="comments-and-unknown"),
        pytest.param("session=pm\nupstream.rev=abc", id="no-trailing-newline"),
    ),
)
def test_serialized_writers_preserve_comments_unknown_keys_and_newlines(
    board, pm_import, monkeypatch, tmp_path, existing,
):
    """락 도입 뒤에도 주석·미지 키·개행 없는 마지막 줄 계약은 그대로다."""
    conf = _conf_at(tmp_path, existing)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    monkeypatch.setattr(python_env, "_detect_py", lambda: "python3")
    board._write_init_local_conf()
    pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"})

    text = conf.read_text(encoding="utf-8")
    parsed = _parse_conf(text)
    assert parsed["session"] == "pm"
    assert parsed["runtime.py"] == "python3"
    assert parsed["upstream.rev"] == "rev-2"
    if "my_custom_key" in existing:
        assert "# 사용자 주석" in text and parsed["my_custom_key"] == "값"
    else:
        # 개행 없이 끝나던 마지막 키가 이어붙기로 변질되지 않았다.
        assert "abc#" not in text and "abcrev-2" not in text


def test_t0932_commit_writer_lock_sensitivity():
    source = _source_of('pm_update.py', 'apply_conf')
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _called_name(n) == '_write_conf_keys_locked']
    assert calls
    locks = [n for n in ast.walk(tree) if isinstance(n, ast.With) and any(isinstance(i.context_expr, ast.Call) and _called_name(i.context_expr) == '_local_conf_write_lock' for i in n.items)]
    assert locks and all(any(call in list(ast.walk(lock)) for lock in locks) for call in calls)
    broken = source.replace('_local_conf_write_lock(', '_removed_lock(')
    with pytest.raises(AssertionError):
        assert LOCK_SEAM_CALL in broken, '동결 commit 락 누락'
