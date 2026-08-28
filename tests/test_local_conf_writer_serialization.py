"""local.conf writer 전수 직렬화 — 공유 락 하나로 lost update 를 닫는다 (T-0590 4차).

`local.conf` 를 쓰는 진입은 한 도구가 아니다: board init(최초 전체 생성·비파괴 병합), board·
pm_update 의 두 opt-in append(추가 리뷰어·cross-harness 위임), pm_import 의 키 writer
(`_write_conf_keys` — pm_import 자신·pm_update 의 upstream_rev 기록·pm_config 의 upstream set 이
공유하는 백엔드). 이들은 **서로 다른 프로세스**로 같은 파일에 동시에 닿을 수 있다.

opt-in append 끼리만 직렬화하면 부족하다는 것이 이 파일의 출발점이다 — append 가 O_APPEND 단일
write 로 원자적이어도, 커밋 **전에** 내용을 읽고 나중에 통째로 갈아끼우는 writer(init 병합·
`_write_conf_keys` 의 temp+`os.replace`)가 그사이의 append 를 읽지 못하면 그 결정은 교체본에 없어
사라진다. 그래서 락의 단위는 "append" 가 아니라 "이 conf 를 읽고 쓰는 구간" 이고, 경로 유도까지
공용 seam(`file_lock.conf_lock_path`) 한 곳이 소유한다(사본이 갈리면 같은 conf 에 다른 락 파일 =
직렬화 없음).

R6 에서 그 단위가 한 번 더 정확해졌다 — 쓰기만 잠그고 **현재 상태 읽기와 계획**을 락 밖에 두면,
원자적으로 써도 그사이 들어온 결정을 옛 값으로 덮거나(재-import 의 사용자 키 보존) 잘못된 분기로
기록·누락한다(경로↔URL 형상에 따른 `upstream_seen_rev`). 계약은 read → 판단/계획 → write →
postcondition 전체의 직렬화다.

검증 축:
  1. inventory ratchet — canonical tools 를 AST 로 훑어 conf writer 를 기계적으로 세고, 각 writer 의
     처분(직접 락·상위 락 전제·위임)을 못박는다. 새 writer 가 생기면 red. 중첩 0 은 락 **구간
     본문**만 따로 훑어 잰다(락 밖 폴백 위임을 중첩으로 오판하지 않게).
  2. 경로 규약 — 모든 진입이 같은 conf 에서 같은 락 파일에 도달한다.
  3. 실 경쟁 — 배리어로 겹치게 돌려 **어느 순서로 겹쳐도** 양쪽 결정이 남는지 본다(helper 대역이
     아니라 실 writer 두 개를 실제 스레드로 경쟁시킨다). 계획이 락 안인지는 배리어를 *현재 상태를
     읽는 지점*·*형상이 뒤집히는 지점* 에 걸어 잰다.
  4. 비파괴 — 직렬화가 기존 주석·미지 키·개행 없는 마지막 줄을 바꾸지 않는다.
  5. 부분 업그레이드 호환 — 새 conf seam 이 없는 구세대 `file_lock` 사본에서도 복구 채널이 죽지
     않고 **같은 락 파일**로 물러나며, marked engine-rev skew 는 그 폴백에 삼켜지지 않는다.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    return _load("file_lock")


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
    ("board.py", "_commit_additional_reviewer_optin"):
        (HOLDS_LOCK, "추가 리뷰어 opt-in 커밋(재읽기→재판정→단일 추가)"),
    ("board.py", "_append_local_conf_atomic"):
        (UNDER_CALLER_LOCK, "선행 개행 + 블록을 한 번의 O_APPEND write 로 붙이는 helper"),
    ("pm_update.py", "_commit_additional_reviewer_optin"):
        (HOLDS_LOCK, "추가 리뷰어 opt-in 커밋(동기 대상 conf)"),
    ("pm_update.py", "_append_local_conf_atomic"):
        (UNDER_CALLER_LOCK, "board 사본과 동형 append helper(무락 폴백 포함)"),
    ("pm_update.py", "record_upstream_revs"):
        (HOLDS_LOCK, "형상 판정(upstream=)→updates 계획→키 write→검증이 한 구간"),
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
    for path in sorted(TOOLS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, node in _function_scopes(tree):
            identifiers = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            # conf 좌표를 이름으로 쓰거나, 이름 자체가 conf writer 를 표방하는 함수만 후보.
            if not (identifiers & _CONF_COORDINATES
                    or re.search(r"conf_keys|local_conf", name)):
                continue
            calls = {
                _called_name(call) for call in ast.walk(node)
                if isinstance(call, ast.Call) and _called_name(call) in _WRITE_CALLS
            }
            if calls:
                measured[(path.name, name)] = calls
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
    for path in sorted(TOOLS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, node in _function_scopes(tree):
            if name == function:
                continue          # 자기 정의(재귀 아님) — 호출부가 아니다.
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and _called_name(call) == function:
                    callers.add((path.name, name))
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
    """네 진입이 같은 conf 에 대해 같은 락 파일에 도달한다 — 유도 규칙 사본 0."""
    conf = tmp_path / ".project_manager" / "local.conf"
    expected = conf.parent / ".local" / "local-conf.lock"
    assert file_lock.conf_lock_path(conf) == expected
    assert board._local_conf_lock_path(conf) == expected
    assert pm_update._local_conf_lock_path(conf) == expected
    assert pm_import._local_conf_lock_path(conf) == expected


def test_the_path_rule_lives_only_in_the_shared_seam():
    """락 파일명 리터럴은 공용 seam 한 곳 + 두 복구 채널의 구세대-사본 폴백뿐이다.

    pm_import·pm_update 는 부분 업그레이드된 트리에서도 떠야 하는 복구 채널이라, `conf_lock_path`
    가 없는 구세대 `file_lock` 사본에서 같은 규칙을 인라인으로 계산한다(`_local_conf_lock_path`).
    두 폴백이 공용 seam 과 같은 파일에 도달하는지는 아래 유도 규약 테스트가 못박는다.
    """
    owners = {
        path.name for path in TOOLS.glob("*.py")
        if "local-conf.lock" in path.read_text(encoding="utf-8")
    }
    assert owners == {"file_lock.py", "pm_import.py", "pm_update.py"}, (
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


def _optin_yes(board, monkeypatch, conf):
    """board 추가 리뷰어 opt-in 커밋('예') 을 대상 conf 에 태우는 클로저."""
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    return lambda: board._commit_additional_reviewer_optin(True)


def test_a_concurrent_key_writer_never_loses_the_optin_append(
    board, pm_import, monkeypatch, tmp_path,
):
    """RMW writer 가 **먼저 읽고** 나중에 교체해도 그사이 opt-in append 가 사라지지 않는다.

    이것이 4차 지적의 원문 형상이다 — append 만 서로 직렬화하면 이 순서에서 opt-in 결정이 통째
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

    appended = threading.Event()
    commit = _optin_yes(board, monkeypatch, conf)

    def _append():
        commit()
        appended.set()

    append_thread = threading.Thread(target=_append, daemon=True)
    append_thread.start()
    assert not appended.wait(BLOCKED_PROBE), (
        "RMW 가 읽고 쓰는 사이에 append 가 끼어들었다 — 락을 공유하지 않는다")

    barrier.resume.set()
    barrier.join(rmw)
    append_thread.join(SYNC_TIMEOUT)
    assert not append_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.rev"] == "rev-2", "RMW 의 결정이 사라졌다"
    assert parsed["additional_reviewer.enabled"] == "true", "opt-in 결정이 교체에 덮였다"
    assert parsed["session"] == "pm"


def test_a_concurrent_key_writer_waits_for_an_in_flight_optin_append(
    board, pm_import, monkeypatch, tmp_path,
):
    """반대 순서 — append 가 임계 구간에 있으면 RMW writer 가 기다렸다 그 결과 위에 쓴다."""
    conf = _conf_at(tmp_path, "session=pm\n")
    barrier = _Barrier()
    real_append = board.file_lock.append_atomic

    def _blocking_append(path, text, **kwargs):
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_append(path, text, **kwargs)

    monkeypatch.setattr(board.file_lock, "append_atomic", _blocking_append)
    commit = _optin_yes(board, monkeypatch, conf)
    optin = barrier.run(commit)
    assert barrier.entered.wait(SYNC_TIMEOUT), "opt-in 이 임계 구간에 못 들어갔다"

    written = threading.Event()

    def _rmw():
        pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"})
        written.set()

    rmw_thread = threading.Thread(target=_rmw, daemon=True)
    rmw_thread.start()
    assert not written.wait(BLOCKED_PROBE), (
        "append 가 진행 중인데 RMW 가 먼저 교체했다 — 락을 공유하지 않는다")

    barrier.resume.set()
    barrier.join(optin)
    rmw_thread.join(SYNC_TIMEOUT)
    assert not rmw_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.rev"] == "rev-2"
    assert parsed["additional_reviewer.enabled"] == "true"


def test_init_merge_and_optin_append_do_not_lose_either_decision(
    board, monkeypatch, tmp_path,
):
    """board init 병합(전체 교체)과 opt-in append 가 겹쳐도 양쪽 결정이 남는다."""
    conf = _conf_at(tmp_path, "session=pm\nupstream.path=/somewhere\n")
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    barrier = _Barrier()
    real_set_conf_keys = board._set_conf_keys

    def _blocking_set(text, updates):
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_set_conf_keys(text, updates)

    monkeypatch.setattr(board, "_set_conf_keys", _blocking_set)
    merge = barrier.run(board._write_init_local_conf)
    assert barrier.entered.wait(SYNC_TIMEOUT), "init 병합이 임계 구간에 못 들어갔다"

    appended = threading.Event()

    def _append():
        board._commit_additional_reviewer_optin(True)
        appended.set()

    append_thread = threading.Thread(target=_append, daemon=True)
    append_thread.start()
    assert not appended.wait(BLOCKED_PROBE), "init 병합 중에 append 가 끼어들었다"

    barrier.resume.set()
    barrier.join(merge)
    append_thread.join(SYNC_TIMEOUT)
    assert not append_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["additional_reviewer.enabled"] == "true", "opt-in 결정이 병합 교체에 덮였다"
    assert parsed["upstream.path"] == "/somewhere", "init 이 안 쓰는 사용자 키가 사라졌다"
    assert parsed["runtime.py"] == "python3"


def test_delegate_optin_and_key_writer_do_not_lose_either_decision(
    board, pm_import, monkeypatch, tmp_path,
):
    """opt-in append 도 같은 락 아래다 — 키 writer 와 겹쳐도 둘 다 남는다."""
    conf = _conf_at(tmp_path, "session=pm\n")
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    barrier = _Barrier()
    real_set_conf_keys = pm_import._set_conf_keys

    def _blocking_set(text, updates):
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_set_conf_keys(text, updates)

    monkeypatch.setattr(pm_import, "_set_conf_keys", _blocking_set)
    rmw = barrier.run(lambda: pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"}))
    assert barrier.entered.wait(SYNC_TIMEOUT)

    done = threading.Event()

    def _optin():
        board.prompt_additional_reviewer_optin()
        done.set()

    optin_thread = threading.Thread(target=_optin, daemon=True)
    optin_thread.start()
    assert not done.wait(BLOCKED_PROBE), "RMW 임계 구간에 opt-in 이 끼어들었다"

    barrier.resume.set()
    barrier.join(rmw)
    optin_thread.join(SYNC_TIMEOUT)
    assert not optin_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.rev"] == "rev-2"
    assert parsed["additional_reviewer.enabled"] == "true"


# ── 축 3'': 계획도 락 안이다 (stale plan 경쟁·R5 재현) ──────────────────────
#
# 앞의 경쟁들은 "쓰기끼리" 겹쳤다. 이 절은 한 단계 앞을 본다 — 현재 conf 를 **락 밖에서** 읽어
# 세운 계획은 커밋 시점엔 이미 낡아, 원자적으로 쓰더라도 그사이 들어온 결정을 잘못된 값/분기로
# 덮거나 빠뜨린다. 그래서 락의 단위는 read → 판단/계획 → write → postcondition 전체다.

def test_preserve_does_not_revive_a_backup_value_over_a_decision_made_meanwhile(
    board, pm_import, monkeypatch, tmp_path,
):
    """재-import 의 사용자 키 보존이 **그사이 생긴 결정**을 백업의 옛 값으로 덮지 않는다.

    형상: 백업에 `additional_reviewer.enabled=false` 가 있고, board init 산출 conf 에는 아직 그 키가
    없다. 보존 재병합이 "현재 conf 에 없는 키" 판정을 락 밖에서 해 두면, 그 사이 추가 리뷰어
    opt-in 이 `true` 를 기록해도 낡은 계획이 `false` 로 되돌린다(사람이 방금 켠 결정을 무음
    롤백). 판정을 락 안에서 하면 새 결정은 그대로 남고 충돌하지 않는 보존 대상만 복원된다.
    """
    conf = _conf_at(tmp_path, "session=pm\n")
    backup = "additional_reviewer.enabled=false\nmy_custom_key=값\n"
    barrier = _Barrier()
    real_append = board.file_lock.append_atomic

    def _blocking_append(path, text, **kwargs):
        # opt-in 이 락을 쥔 채 **아직 붙이지 않은** 창 — 보존 재병합이 이 창에서 계획을 세우면
        # 그 계획은 곧 낡는다.
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_append(path, text, **kwargs)

    monkeypatch.setattr(board.file_lock, "append_atomic", _blocking_append)
    optin = barrier.run(_optin_yes(board, monkeypatch, conf))
    assert barrier.entered.wait(SYNC_TIMEOUT), "opt-in 이 임계 구간에 못 들어갔다"

    preserved = threading.Event()

    def _preserve():
        pm_import.reapply_preserved_conf_keys(conf.parent.parent, backup)
        preserved.set()

    preserve_thread = threading.Thread(target=_preserve, daemon=True)
    preserve_thread.start()
    assert not preserved.wait(BLOCKED_PROBE), (
        "opt-in 임계 구간에 보존 재병합이 끼어들었다 — 락을 공유하지 않는다")

    barrier.resume.set()
    barrier.join(optin)
    preserve_thread.join(SYNC_TIMEOUT)
    assert not preserve_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["additional_reviewer.enabled"] == "true", (
        "백업의 옛 값이 방금 기록된 결정을 덮었다(락 밖에서 세운 계획)")
    assert parsed["my_custom_key"] == "값", "충돌하지 않는 보존 대상이 사라졌다"
    assert parsed["session"] == "pm"


def test_an_in_flight_preserve_blocks_a_later_optin_and_keeps_both_decisions(
    board, pm_import, monkeypatch, tmp_path,
):
    """반대 순서 — 보존 재병합이 **현재 상태를 읽는 지점부터** 락 안이라 뒤 opt-in 이 기다린다.

    배리어를 쓰기 지점이 아니라 *대상 conf 의 현재 상태 파싱* 지점에 둔다 — 그 읽기가 락 밖이면
    opt-in 이 이 창에서 즉시 끝나 버린다(red). 락 안이면 순서가 강제되고 두 결정
    (보존 대상 + opt-in)이 모두 남는다.
    """
    conf = _conf_at(tmp_path, "session=pm\n")
    backup = ("my_custom_key=값\nadditional_reviewer.harness=codex\n"
              "additional_reviewer.model=gpt-5.6-sol\n")
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
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

    def _optin():
        board.prompt_additional_reviewer_optin()
        done.set()

    optin_thread = threading.Thread(target=_optin, daemon=True)
    optin_thread.start()
    assert not done.wait(BLOCKED_PROBE), (
        "보존 재병합이 현재 상태를 읽는 사이 opt-in 이 끼어들었다 — 읽기가 락 밖이다")

    barrier.resume.set()
    barrier.join(preserve)
    optin_thread.join(SYNC_TIMEOUT)
    assert not optin_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["my_custom_key"] == "값"
    assert parsed["additional_reviewer.harness"] == "codex"
    assert parsed["additional_reviewer.enabled"] == "true", "opt-in 결정이 보존 교체에 덮였다"


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
    출발한다 — 형상 판정이 락 밖이면 그 창의 **옛 형상**으로 계획이 굳는다. 반환은
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


def test_seen_rev_is_not_written_when_the_shape_becomes_a_url_meanwhile(
    pm_update, pm_import, monkeypatch, tmp_path,
):
    """path→URL 플립이 먼저 커밋되면 경로-전용 관찰값은 **쓰지 않는다**(stale 형상 금지).

    `upstream_seen_rev` 은 URL 형상에서 스킬층(fetch 후 관찰)이 소유한다 — 엔진이 옛 'path'
    계획으로 그 키를 쓰면 한 키를 두 주체가 쓰게 되고, 자기비교로 drift 판정이 무의미해진다.
    """
    (changed, updates), parsed = _run_rev_record_against_an_upstream_flip(
        pm_update, pm_import, monkeypatch, tmp_path,
        stored="/local/upstream", flipped="https://example.invalid/u.git")

    assert changed is True
    assert updates == {"upstream.rev": "rev-new"}, "URL 형상인데 경로-전용 키를 계획했다"
    assert parsed["upstream.rev"] == "rev-new"
    assert "upstream.seen_rev" not in parsed, (
        "URL 이 된 conf 에 경로-전용 관찰값이 stale 계획으로 기록됐다")


def test_seen_rev_is_written_when_the_shape_becomes_a_path_meanwhile(
    pm_update, pm_import, monkeypatch, tmp_path,
):
    """URL→path 플립이 먼저 커밋되면 관찰값을 **함께** 쓴다 — 누락도 같은 창의 결함이다.

    baseline 만 갱신되면 두 키가 영구히 어긋나 정상 흡수 직후에도 drift 거짓 경보가 상시 뜬다.
    """
    (changed, updates), parsed = _run_rev_record_against_an_upstream_flip(
        pm_update, pm_import, monkeypatch, tmp_path,
        stored="https://example.invalid/u.git", flipped="/local/upstream")

    assert changed is True
    assert updates == {"upstream.rev": "rev-new", "upstream.seen_rev": "rev-new"}
    assert parsed["upstream.rev"] == "rev-new"
    assert parsed["upstream.seen_rev"] == "rev-new", (
        "경로 형상이 된 conf 에서 관찰값이 stale 계획으로 누락됐다")


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
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    taken: list[str] = []
    _spy_lock(board.file_lock, monkeypatch, taken)

    board._write_init_local_conf()

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]
    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["test.cmd"] == board.default_pytest_cmd("python3")
    # 세션·prefix 는 per-clone conf 의 키가 아니다 (T-0779).
    assert "session" not in parsed and "prefix" not in parsed


def test_key_writer_takes_the_shared_lock(pm_import, monkeypatch, tmp_path):
    """`_write_conf_keys` 도 같은 락 파일을 잡는다(pm_import·pm_update·pm_config 공용 백엔드)."""
    conf = _conf_at(tmp_path, "session=pm\n")
    taken: list[str] = []
    _spy_lock(pm_import._load_file_lock(), monkeypatch, taken)

    assert pm_import._write_conf_keys(conf, {"upstream.path": "/u"}) is True

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]


def test_pm_update_optin_takes_the_shared_lock(pm_update, monkeypatch, tmp_path):
    """pm_update 의 opt-in 기록도 공용 락 + 단일 원자 추가다."""
    conf = _conf_at(tmp_path, "session=pm\n")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(pm_update.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    taken: list[str] = []
    _spy_lock(pm_update._load_file_lock(), monkeypatch, taken)

    pm_update.maybe_prompt_additional_reviewer(conf.parent.parent)

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]
    assert (_parse_conf(conf.read_text(encoding="utf-8"))["additional_reviewer.enabled"]
            == "true")


# ── 축 5: 부분 업그레이드 호환 (구세대 file_lock 사본) ──────────────────────
#
# `conf_lock_path`·`local_conf_write_lock` 은 rev 중간에 들어왔고 `ENGINE_REV` 는 릴리스 단위로
# 찍힌다 — 같은 rev 로 찍힌 트리에도 새 seam 이전 `file_lock.py` 사본이 남을 수 있다. pm_import·
# pm_update 는 그런 트리를 고치러 들어가는 **복구 채널**이라, 그 자리에서 AttributeError 로 죽으면
# 채택자가 자기 트리를 못 고친다. 락은 포기하지 않고(같은 락 파일을 구 API 로) 물러나야 한다.

_LEGACY_MODULES = ("pm_import", "pm_update")


def _legacy_lock_copy(file_lock, *, with_exclusive: bool):
    """구세대 `file_lock` 사본 대역 — 새 conf seam 두 개가 없는 형상(속성 자체가 부재).

    `with_exclusive=False` 는 락 프리미티브가 아예 없던 더 옛 사본/희귀 환경(무락 폴백 경계).
    """
    attrs = {"ENGINE_REV": file_lock.ENGINE_REV, "append_atomic": file_lock.append_atomic}
    if with_exclusive:
        attrs["exclusive_file_lock"] = file_lock.exclusive_file_lock
    legacy = SimpleNamespace(**attrs)
    assert not hasattr(legacy, "local_conf_write_lock")
    assert not hasattr(legacy, "conf_lock_path")
    return legacy


@pytest.mark.parametrize("name", _LEGACY_MODULES)
def test_the_new_lock_api_is_used_when_the_copy_has_it(name, file_lock, tmp_path):
    """새 API 가 있으면 그것으로 구간을 연다 — 구 API 폴백이 정상 경로를 가로채지 않는다."""
    module = _load(name)
    conf = tmp_path / ".project_manager" / "local.conf"
    taken: list[str] = []
    probe = SimpleNamespace(
        conf_lock_path=file_lock.conf_lock_path,
        local_conf_write_lock=lambda path: taken.append(("new", str(path))) or nullcontext(),
        exclusive_file_lock=lambda path, **kw: taken.append(("legacy", str(path))),
    )

    with module._conf_lock_section(probe, conf):
        pass

    assert taken == [("new", str(conf))], "새 API 가 있는데 구 API 로 물러났다"


@pytest.mark.parametrize("name", _LEGACY_MODULES)
def test_a_legacy_copy_locks_the_same_file_through_the_old_api(
    name, file_lock, tmp_path, monkeypatch,
):
    """새 seam 이 없는 사본에서도 **같은 락 파일**을 구 `exclusive_file_lock` 으로 잡는다.

    다른 파일을 잡으면 배타가 조용히 사라진다(같은 conf, 다른 락 파일 = 직렬화 없음) — 경로가
    공용 seam 의 유도 결과와 글자 단위로 같은지 못박는다.
    """
    module = _load(name)
    conf = tmp_path / ".project_manager" / "local.conf"
    legacy = _legacy_lock_copy(file_lock, with_exclusive=True)
    taken: list[str] = []
    real_lock = legacy.exclusive_file_lock

    def _spy(path, **kwargs):
        taken.append(str(path))
        return real_lock(path, **kwargs)

    legacy.exclusive_file_lock = _spy
    monkeypatch.setattr(module, "_load_file_lock", lambda: legacy)

    with module._local_conf_write_lock(conf) as lock:
        assert lock is legacy

    assert taken == [str(file_lock.conf_lock_path(conf))]
    assert module._local_conf_lock_path(conf) == file_lock.conf_lock_path(conf)


def test_a_legacy_copy_still_serializes_against_a_new_api_writer(
    board, pm_import, file_lock, monkeypatch, tmp_path,
):
    """구 API 로 잡은 락이 새 API 로 잡은 락과 실제로 배타적이다(경로가 같으므로).

    경로 단언만으로는 "같은 파일" 을 말할 뿐이라, 실제로 한쪽이 다른 쪽을 막는지 배리어로 본다 —
    구세대 사본으로 물러난 pm_import 의 키 writer 가 임계 구간에 있으면 board 의 opt-in
    (새 API)이 기다린다.
    """
    conf = _conf_at(tmp_path, "session=pm\n")
    legacy = _legacy_lock_copy(file_lock, with_exclusive=True)
    monkeypatch.setattr(pm_import, "_load_file_lock", lambda: legacy)
    barrier = _Barrier()
    real_set_conf_keys = pm_import._set_conf_keys

    def _blocking_set(text, updates):
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_set_conf_keys(text, updates)

    monkeypatch.setattr(pm_import, "_set_conf_keys", _blocking_set)
    rmw = barrier.run(lambda: pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"}))
    assert barrier.entered.wait(SYNC_TIMEOUT), "구세대 사본 writer 가 임계 구간에 못 들어갔다"

    appended = threading.Event()

    def _append():
        _optin_yes(board, monkeypatch, conf)()
        appended.set()

    append_thread = threading.Thread(target=_append, daemon=True)
    append_thread.start()
    assert not appended.wait(BLOCKED_PROBE), (
        "구 API 락이 새 API 락과 배타적이지 않다 — 다른 파일을 잡았다")

    barrier.resume.set()
    barrier.join(rmw)
    append_thread.join(SYNC_TIMEOUT)
    assert not append_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.rev"] == "rev-2"
    assert parsed["additional_reviewer.enabled"] == "true"


@pytest.mark.parametrize("name", _LEGACY_MODULES)
def test_a_copy_without_any_lock_primitive_keeps_the_lockless_recovery_contract(
    name, file_lock, tmp_path, monkeypatch,
):
    """락 프리미티브가 아예 없는 사본은 종전 계약대로 **무락 진행**이다(loud 아님·write 는 남는다)."""
    module = _load(name)
    conf = _conf_at(tmp_path, "session=pm\n")
    legacy = _legacy_lock_copy(file_lock, with_exclusive=False)
    monkeypatch.setattr(module, "_load_file_lock", lambda: legacy)

    assert module._conf_lock_section(legacy, conf) is None
    with module._local_conf_write_lock(conf) as lock:
        assert lock is legacy

    if name == "pm_import":
        assert module._write_conf_keys(conf, {"upstream.rev": "rev-2"}) is True
        assert _parse_conf(conf.read_text(encoding="utf-8"))["upstream.rev"] == "rev-2"
    else:
        monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
        monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        module.maybe_prompt_additional_reviewer(conf.parent.parent)
        assert (_parse_conf(conf.read_text(encoding="utf-8"))["additional_reviewer.enabled"]
                == "true")


def test_marked_rev_skew_is_not_absorbed_by_the_conf_lock_seam(pm_import, monkeypatch, tmp_path):
    """marked engine-rev skew 는 부분 업그레이드 호환에 삼켜지지 않는다(기존 경계 보존).

    같은 rev 안의 API 형상 차이는 물러날 근거이지만, **rev 자체가 다른** 사본은 조용한 오작동이
    아니라 재동기 안내로 표출해야 한다 — 락 경로 유도에서도 같다.
    """
    conf = _conf_at(tmp_path, "session=pm\n")

    def _skew():
        err = RuntimeError("엔진 사본 버전 불일치 — file_lock.py")
        err._engine_rev_skew = True
        raise err

    monkeypatch.setattr(pm_import, "_load_file_lock", _skew)
    with pytest.raises(RuntimeError) as exc:
        pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"})
    assert getattr(exc.value, "_engine_rev_skew", False) is True
    with pytest.raises(RuntimeError):
        pm_import._local_conf_lock_path(conf)
    assert _parse_conf(conf.read_text(encoding="utf-8")) == {"session": "pm"}

    # 일반 형제 손상(unmarked)은 종전대로 무락 복구 — 이 경계가 함께 유지된다.
    monkeypatch.setattr(
        pm_import, "_load_file_lock", lambda: (_ for _ in ()).throw(OSError("손상 사본")))
    assert pm_import._local_conf_lock_path(conf) == conf.parent / ".local" / "local-conf.lock"
    assert pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"}) is True


def test_record_upstream_revs_falls_back_when_the_key_writer_seam_is_old(
    pm_update, monkeypatch, tmp_path,
):
    """구세대 pm_import 사본(임계 구간 본문 seam 부재)에서도 rev 기록이 뜨고 결과가 같다.

    그 사본에서는 자기-락 writer 로 물러난다 — 우리 락을 쥔 채 부르면 같은 락 파일을 두 fd 로
    잡아 데드락이므로, 이 테스트가 끝나는 것 자체가 "중첩 락 0" 의 실측이다.
    """
    conf = _conf_at(tmp_path, "session=pm\nupstream.path=/local/upstream\n")
    writer = _pm_update_rev_writer(pm_update, monkeypatch, "rev-new")
    # 구세대 사본의 표면 — 임계 구간 본문(`_write_conf_keys_locked`)이 아직 없다.
    legacy_writer = SimpleNamespace(
        read_upstream_rev=writer.read_upstream_rev,
        classify_upstream=writer.classify_upstream,
        _write_conf_keys=writer._write_conf_keys,
    )
    assert not hasattr(legacy_writer, "_write_conf_keys_locked")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: legacy_writer)
    taken: list[str] = []
    _spy_lock(writer._load_file_lock(), monkeypatch, taken)

    changed, updates = pm_update.record_upstream_revs(conf.parent.parent, tmp_path / "src")

    assert changed is True
    assert updates == {"upstream.rev": "rev-new", "upstream.seen_rev": "rev-new"}
    # 락은 자기-락 writer 가 **한 번만** 잡는다(우리 구간과 중첩되지 않았다).
    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]
    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.rev"] == "rev-new" and parsed["upstream.seen_rev"] == "rev-new"


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

    board._commit_additional_reviewer_optin(True)
    pm_import._write_conf_keys(conf, {"upstream.rev": "rev-2"})

    text = conf.read_text(encoding="utf-8")
    parsed = _parse_conf(text)
    assert parsed["session"] == "pm"
    assert parsed["additional_reviewer.enabled"] == "true"
    assert parsed["upstream.rev"] == "rev-2"
    if "my_custom_key" in existing:
        assert "# 사용자 주석" in text and parsed["my_custom_key"] == "값"
    else:
        # 개행 없이 끝나던 마지막 키가 append 로 변질되지 않았다.
        assert "abc#" not in text and "abctrue" not in text
