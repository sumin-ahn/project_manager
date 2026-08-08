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

검증 축:
  1. inventory ratchet — canonical tools 를 AST 로 훑어 conf writer 를 기계적으로 세고, 각 writer 의
     처분(직접 락·상위 락 전제·위임)을 못박는다. 새 writer 가 생기면 red.
  2. 경로 규약 — 모든 진입이 같은 conf 에서 같은 락 파일에 도달한다.
  3. 실 경쟁 — 배리어로 겹치게 돌려 **어느 순서로 겹쳐도** 양쪽 결정이 남는지 본다(helper 대역이
     아니라 실 writer 두 개를 실제 스레드로 경쟁시킨다).
  4. 비파괴 — 직렬화가 기존 주석·미지 키·개행 없는 마지막 줄을 바꾸지 않는다.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import threading
from pathlib import Path

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
    ("board.py", "prompt_delegate_optin"):
        (HOLDS_LOCK, "cross-harness 위임 opt-in 커밋"),
    ("board.py", "_append_local_conf_atomic"):
        (UNDER_CALLER_LOCK, "선행 개행 + 블록을 한 번의 O_APPEND write 로 붙이는 helper"),
    ("pm_update.py", "_commit_additional_reviewer_optin"):
        (HOLDS_LOCK, "추가 리뷰어 opt-in 커밋(동기 대상 conf)"),
    ("pm_update.py", "maybe_prompt_delegate_optin"):
        (HOLDS_LOCK, "cross-harness 위임 opt-in 커밋(동기 대상 conf)"),
    ("pm_update.py", "_append_local_conf_atomic"):
        (UNDER_CALLER_LOCK, "board 사본과 동형 append helper(무락 폴백 포함)"),
    ("pm_update.py", "record_upstream_revs"):
        (DELEGATES, "upstream_rev/seen_rev 기록 → pm_import._write_conf_keys"),
    ("pm_import.py", "_write_conf_keys"):
        (HOLDS_LOCK, "키 단위 RMW 백엔드(pm_import·pm_update·pm_config 공용)"),
    ("pm_import.py", "_write_conf_keys_locked"):
        (UNDER_CALLER_LOCK, "임계 구간 본문(temp write + os.replace + 실효값 검증)"),
    ("pm_import.py", "sync_local_conf"):
        (DELEGATES, "import 직후 operational 키 정렬"),
    ("pm_import.py", "reapply_preserved_conf_keys"):
        (DELEGATES, "재-import 시 사용자 키 재병합"),
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
            calls = set()
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                called = (func.id if isinstance(func, ast.Name)
                          else func.attr if isinstance(func, ast.Attribute) else None)
                if called in _WRITE_CALLS:
                    calls.add(called)
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


def test_helpers_under_the_caller_lock_are_only_called_by_lock_holders():
    """상위 락 전제 helper 는 락-보유 writer 밖에서 불리지 않는다(락 밖 write 0)."""
    for (module_file, function), (disposition, _reason) in LOCAL_CONF_WRITERS.items():
        if disposition != UNDER_CALLER_LOCK:
            continue
        tree = ast.parse((TOOLS / module_file).read_text(encoding="utf-8"))
        callers = set()
        for name, node in _function_scopes(tree):
            if name == function:
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                        and call.func.id == function:
                    callers.add(name)
        assert callers, f"{module_file}::{function} 호출부 0 — 등재가 낡았다"
        for caller in callers:
            assert LOCAL_CONF_WRITERS.get((module_file, caller), (None, ""))[0] == HOLDS_LOCK, (
                f"{module_file}::{caller} 가 락 없이 {function} 로 conf 를 쓴다")


# ── 축 2: 경로 규약 (같은 conf → 같은 락 파일) ──────────────────────────────

def test_every_entry_derives_the_same_lock_path(board, pm_update, file_lock, tmp_path):
    """세 진입이 같은 conf 에 대해 같은 락 파일에 도달한다 — 유도 규칙 사본 0."""
    conf = tmp_path / ".project_manager" / "local.conf"
    expected = conf.parent / ".local" / "local-conf.lock"
    assert file_lock.conf_lock_path(conf) == expected
    assert board._local_conf_lock_path(conf) == expected
    assert pm_update._local_conf_lock_path(conf) == expected


def test_the_path_rule_lives_only_in_the_shared_seam():
    """락 파일명 리터럴은 공용 seam 한 곳 + pm_update 의 손상-사본 폴백뿐이다."""
    owners = {
        path.name for path in TOOLS.glob("*.py")
        if "local-conf.lock" in path.read_text(encoding="utf-8")
    }
    assert owners == {"file_lock.py", "pm_update.py"}, (
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
        lambda: pm_import._write_conf_keys(conf, {"upstream_rev": "rev-2"}))
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
    assert parsed["upstream_rev"] == "rev-2", "RMW 의 결정이 사라졌다"
    assert parsed["external_review_enabled"] == "true", "opt-in 결정이 교체에 덮였다"
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
        pm_import._write_conf_keys(conf, {"upstream_rev": "rev-2"})
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
    assert parsed["upstream_rev"] == "rev-2"
    assert parsed["external_review_enabled"] == "true"


def test_init_merge_and_optin_append_do_not_lose_either_decision(
    board, monkeypatch, tmp_path,
):
    """board init 병합(전체 교체)과 opt-in append 가 겹쳐도 양쪽 결정이 남는다."""
    conf = _conf_at(tmp_path, "session=pm\nupstream=/somewhere\n")
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    barrier = _Barrier()
    real_set_conf_keys = board._set_conf_keys

    def _blocking_set(text, updates):
        barrier.entered.set()
        assert barrier.resume.wait(SYNC_TIMEOUT), "배리어 해제 실패"
        return real_set_conf_keys(text, updates)

    monkeypatch.setattr(board, "_set_conf_keys", _blocking_set)
    merge = barrier.run(lambda: board._write_init_local_conf(
        prefix=None, namespaced=False, sess="pm", override=None))
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
    assert parsed["external_review_enabled"] == "true", "opt-in 결정이 병합 교체에 덮였다"
    assert parsed["upstream"] == "/somewhere", "init 이 안 쓰는 사용자 키가 사라졌다"
    assert parsed["py"] == "python3"


def test_delegate_optin_and_key_writer_do_not_lose_either_decision(
    board, pm_import, monkeypatch, tmp_path,
):
    """위임 opt-in append 도 같은 락 아래다 — 키 writer 와 겹쳐도 둘 다 남는다."""
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
    rmw = barrier.run(lambda: pm_import._write_conf_keys(conf, {"upstream_rev": "rev-2"}))
    assert barrier.entered.wait(SYNC_TIMEOUT)

    done = threading.Event()

    def _optin():
        board.prompt_delegate_optin()
        done.set()

    optin_thread = threading.Thread(target=_optin, daemon=True)
    optin_thread.start()
    assert not done.wait(BLOCKED_PROBE), "RMW 임계 구간에 위임 opt-in 이 끼어들었다"

    barrier.resume.set()
    barrier.join(rmw)
    optin_thread.join(SYNC_TIMEOUT)
    assert not optin_thread.is_alive()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream_rev"] == "rev-2"
    assert parsed["delegate_enabled"] == "true"


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

    board._write_init_local_conf(prefix=None, namespaced=False, sess="pm", override=None)

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]
    assert _parse_conf(conf.read_text(encoding="utf-8"))["session"] == "pm"


def test_key_writer_takes_the_shared_lock(pm_import, monkeypatch, tmp_path):
    """`_write_conf_keys` 도 같은 락 파일을 잡는다(pm_import·pm_update·pm_config 공용 백엔드)."""
    conf = _conf_at(tmp_path, "session=pm\n")
    taken: list[str] = []
    _spy_lock(pm_import._load_file_lock(), monkeypatch, taken)

    assert pm_import._write_conf_keys(conf, {"upstream": "/u"}) is True

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]


def test_pm_update_delegate_optin_takes_the_shared_lock(pm_update, monkeypatch, tmp_path):
    """pm_update 의 위임 opt-in 기록도 공용 락 + 단일 원자 추가다."""
    conf = _conf_at(tmp_path, "session=pm\n")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(pm_update.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    taken: list[str] = []
    _spy_lock(pm_update._load_file_lock(), monkeypatch, taken)

    pm_update.maybe_prompt_delegate_optin(conf.parent.parent)

    assert taken == [str(conf.parent / ".local" / "local-conf.lock")]
    assert _parse_conf(conf.read_text(encoding="utf-8"))["delegate_enabled"] == "true"


# ── 축 4: 비파괴 (직렬화가 바이트 계약을 바꾸지 않는다) ─────────────────────

@pytest.mark.parametrize(
    "existing",
    (
        pytest.param("# 사용자 주석\nsession=pm\nmy_custom_key=값\n", id="comments-and-unknown"),
        pytest.param("session=pm\nupstream_rev=abc", id="no-trailing-newline"),
    ),
)
def test_serialized_writers_preserve_comments_unknown_keys_and_newlines(
    board, pm_import, monkeypatch, tmp_path, existing,
):
    """락 도입 뒤에도 주석·미지 키·개행 없는 마지막 줄 계약은 그대로다."""
    conf = _conf_at(tmp_path, existing)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    board._commit_additional_reviewer_optin(True)
    pm_import._write_conf_keys(conf, {"upstream_rev": "rev-2"})

    text = conf.read_text(encoding="utf-8")
    parsed = _parse_conf(text)
    assert parsed["session"] == "pm"
    assert parsed["external_review_enabled"] == "true"
    assert parsed["upstream_rev"] == "rev-2"
    if "my_custom_key" in existing:
        assert "# 사용자 주석" in text and parsed["my_custom_key"] == "값"
    else:
        # 개행 없이 끝나던 마지막 키가 append 로 변질되지 않았다.
        assert "abc#" not in text and "abctrue" not in text
