"""T-0607 — 동기 실행 중 사본 rev 혼합(과도 상태) 자가-차단 폐쇄.

`pm_update.apply` 는 per-file 순차 write 라 실행 중 목적지 트리에는 구/신 `ENGINE_REV` 가
공존한다(원자 교체는 파일 단위다). 그 사이의 **중첩 로드**(pm_update → 형제 pm_import·목적지
pm_config → 다시 그 형제를 verifier 로 로드)는 이 혼합을 marked skew 로 올리고, 그러면 skew 를
고치는 유일한 채널인 동기 자신이 죽는다(v1.7.0 흡수 실행에서 1회 실측·실해 0·재실행 수렴).

이 파일이 고정하는 불변식은 하나다:

    동기 실행 경로의 어떤 중첩 로드도 rev 혼합을 사유로 동기를 중단하지 않는다.
    각 경계는 등록된 사유로 흡수해 자기 fail-soft 로 내려가고, 실행 **종료 시** 한 번
    수렴을 검증한다(흡수가 침묵으로 전락하지 않게 하는 짝).

지점 열거가 아니라 **범위**로 정의하므로, 전수 감사도 열거가 아니라 기계 판정으로 박제한다
(§1): pm_update 안의 형제 호출은 ① 등록된 흡수 경계 안이거나 ② 흡수하는 호출부 아래이거나
③ *그 API 가 verifier 로드에 도달할 수 없음* 셋 중 하나여야 한다. 새 형제 호출이 생기면 어느
쪽인지 선언하지 않는 한 red 다.

동기 실행 **밖**(board.py 등 일반 CLI 의 형제 로드)에서 skew 는 여전히 실결함 신호이므로
fail-loud 를 유지한다 — 그 회귀는 `test_engine_rev_skew.py` 가 소유한다.
"""
from __future__ import annotations

import ast
import collections
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _textio import utf8_child_env

# 판정 사본 금지 — "verifier 를 쓰는 로드인가" 는 fail-soft 가드가 이미 소유한 규칙이다.
from test_engine_rev_failsoft_guard import _call_uses_verifier, _called_name

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# pm_update 안에서 *형제 엔진 모듈 객체*를 담는 이름들. 형제는 이 이름들을 통해서만 소비되므로
# (로더 반환값을 이 이름에 묶는 관례) 이 집합이 skew 유입 표면의 입구다.
_SIBLING_NAMES = {
    "pm_import": "pm_import.py",
    "pm_config": "pm_config.py",
    "board": "board.py",
    "worktree_pool": "worktree_pool.py",
    "repo_files": "repo_owned_files.py",
    "pm_render": "pm_render.py",
}

_RECOVERY_MARKER = "_absorb_engine_rev_skew_for_recovery"

# 흡수 경계 **밖**에 남긴 형제 호출 — 코드 소유 감사표. 값은 남긴 근거이며 아래 테스트가 그
# 근거를 매번 다시 판정한다(선언만으로는 통과하지 못한다).
#   "no-verified-load"  그 API 가 verifier 로드에 도달할 수 없다 = skew 표면이 아니다.
#                       도달하게 되면 red → 그 지점을 흡수 경계로 감싸야 한다.
#   "caller-absorbs"    이 함수는 skew 를 그대로 올리고 **호출부**가 등록된 경계로 흡수한다.
#                       (여기서 흡수해 drift 로 내리면 혼합 트리의 엔진으로 훅을 생성·설치한다.)
_UNABSORBED_SIBLING_CALLS = {
    ("resolve_hook_set_predicate", "pm_import.is_live_hook_set_path"): "no-verified-load",
    ("_shipping_inventory", "repo_files._real_git_runner"): "no-verified-load",
    ("_shipping_inventory", "repo_files.list_repo_owned_entries"): "no-verified-load",
    ("_shipping_inventory", "repo_files.list_repo_owned_files"): "no-verified-load",
    ("_retired_manifest_files", "repo_files._real_git_runner"): "no-verified-load",
    ("_retired_manifest_files", "repo_files.list_repo_owned_entries"): "no-verified-load",
    ("_guest_engine_backfill_entries", "pm_import._guest_manifest_lines"): "no-verified-load",
    ("_upstream_shape", "pm_import.classify_upstream"): "no-verified-load",
    ("sync_adapter_configs", "pm_import.adapter_config_drift_summary"): "no-verified-load",
    ("sync_adapter_configs", "pm_import.unconverged_managed_adapter_configs"):
        "no-verified-load",
    ("_unverified_hook_scope_paths", "pm_import.hook_set_namespaces"): "no-verified-load",
    ("_retired_manifest_files", "repo_files.RepoFilesFallbackWarning"): "no-verified-load",
    ("_protected_hook_in_sync", "pm_config._resolve_repo_protected"): "caller-absorbs",
    ("_protected_hook_in_sync", "pm_config.protected_hook_wired"): "caller-absorbs",
    ("_protected_hook_in_sync", "pm_config._protected_push_gate_config"): "caller-absorbs",
    ("_protected_hook_in_sync", "worktree_pool.protected_hook_artifacts"): "caller-absorbs",
}


def _load_tool(name: str, tools: Path = TOOLS):
    spec = importlib.util.spec_from_file_location(name, tools / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pm_update():
    return _load_tool("pm_update")


def _marked_skew(message: str = "nested engine rev skew") -> RuntimeError:
    """중첩 로드가 다는 marker 를 그대로 단 예외 대역(형제 사본을 실제로 어긋내지 않고 판정)."""
    error = RuntimeError(message)
    error._engine_rev_skew = True
    return error


class _SkewingSibling:
    """어떤 속성을 만져도 marked skew 를 내는 형제 모듈 대역."""

    def __init__(self, exc_factory=_marked_skew):
        object.__setattr__(self, "_exc_factory", exc_factory)

    def __getattr__(self, name):
        raise object.__getattribute__(self, "_exc_factory")(
            f"nested engine rev skew on {name}")


# ── §1 전수 감사 — 기계 판정 ────────────────────────────────────────────────


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(tree: ast.Module, lineno: int) -> str:
    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= lineno <= (node.end_lineno or node.lineno)
    ]
    candidates.sort(key=lambda node: (node.end_lineno or node.lineno) - node.lineno)
    return candidates[0].name if candidates else "<module>"


def _handler_absorbs(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Call) and _called_name(node) == _RECOVERY_MARKER
        for node in ast.walk(handler)
    )


def _absorbed(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """이 노드가 등록된 흡수 경계(try 본문)의 보호 아래 있는가."""
    child: ast.AST = node
    parent = parents.get(child)
    while parent is not None:
        if isinstance(parent, ast.Try) and child in parent.body:
            if any(_handler_absorbs(handler) for handler in parent.handlers):
                return True
        child, parent = parent, parents.get(parent)
    return False


def _sibling_member_access(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    """이 노드가 형제 모듈 멤버를 **꺼내 부르는** 지점이면 `<sibling>.<attr>`.

    직접 표기(`pm_import.foo(...)`)와 `getattr(pm_import, "foo", ...)` 간접 표기를 같은 판정에
    태운다 — 후자를 빼면 감사 자체를 한 줄로 우회할 수 있다."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id not in _SIBLING_NAMES:
            return None
        parent = parents.get(node)
        if not (isinstance(parent, ast.Call) and parent.func is node):
            return None     # 상수/데이터 참조는 이미 로드된 객체를 읽을 뿐 로드를 유발하지 않는다.
        return f"{node.value.id}.{node.attr}"
    if (
        isinstance(node, ast.Call)
        and _called_name(node) == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in _SIBLING_NAMES
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return f"{node.args[0].id}.{node.args[1].value}"
    return None


def _sibling_calls(source: str) -> list[tuple[str, str, int]]:
    """(enclosing function, `<sibling>.<attr>`, lineno) — 흡수 경계 **밖**의 형제 멤버 소비."""
    tree = ast.parse(source)
    parents = _parents(tree)
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        api = _sibling_member_access(node, parents)
        if api is None or _absorbed(node, parents):
            continue
        found.append((_enclosing_function(tree, node.lineno), api, node.lineno))
    return found


def _call_graph(source: str) -> dict[str, set[str]]:
    tree = ast.parse(source)
    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = graph.setdefault(node.name, set())
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = _called_name(child)
                if name:
                    names.add(name)
    return graph


def _verifier_load_functions(source: str) -> set[str]:
    """`verifier=` 로드를 실행하는 함수 이름 집합 = 그 모듈의 skew 발화 지점."""
    tree = ast.parse(source)
    owners: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(child, ast.Call) and _call_uses_verifier(child)
            for child in ast.walk(node)
        ):
            owners.add(node.name)
    return owners


def _absorbing_functions(source: str) -> set[str]:
    """등록된 흡수 마커(`_absorb_engine_rev_skew_for_recovery`)를 소비하는 함수 집합.

    이 함수를 지나는 호출 경로에서는 marked skew 가 **밖으로 나오지 않는다** — 흡수가 옳은지는
    사유 등록(호출 시 강제)과 fail-soft 경계 가드가 따로 판정하므로, 여기서는 "skew 표면인가" 만
    본다. 이 가지를 계속 따라가면 호출부가 볼 수 없는 skew 를 도달로 세게 된다.
    """
    absorbing = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "_absorb_engine_rev_skew_for_recovery"):
                absorbing.add(node.name)
                break
    return absorbing


def _reaches_verified_load(source: str, entry: str) -> list[str] | None:
    """`entry` 에서 verifier 로드까지의 호출 경로(없으면 None). 정적 하한(간접 호출 제외).

    등록된 흡수 경계를 지나는 가지는 잘라낸다 — 그 너머의 로드는 호출부에 skew 를 내보내지
    않으므로 "흡수 없이 남긴 API 가 skew 표면인가" 라는 이 판정의 대상이 아니다.
    """
    graph = _call_graph(source)
    seeds = _verifier_load_functions(source)
    absorbing = _absorbing_functions(source)
    if entry in seeds:
        return [entry]
    if entry in absorbing:
        return None
    seen = {entry}
    queue = collections.deque([(entry, [entry])])
    while queue:
        current, path = queue.popleft()
        for callee in graph.get(current, ()):
            if callee in absorbing:
                continue
            if callee in seeds:
                return [*path, callee]
            if callee in graph and callee not in seen:
                seen.add(callee)
                queue.append((callee, [*path, callee]))
    return None


def test_sync_path_sibling_calls_are_absorbed_or_declared():
    """동기 경로의 형제 호출 전수 — 흡수 경계 밖은 감사표에 선언된 것뿐이다.

    새 형제 호출이 흡수 없이 들어오면 red 다. 지점을 하나씩 고치는(point-patch) 대신 "선언하거나
    감싸라" 를 기계가 요구하는 게 이 티켓의 폐쇄 방식이다."""
    calls = _sibling_calls((TOOLS / "pm_update.py").read_text(encoding="utf-8"))
    undeclared = sorted(
        f"{function}:{api} (line {lineno})"
        for function, api, lineno in calls
        if (function, api) not in _UNABSORBED_SIBLING_CALLS
    )
    assert not undeclared, (
        "흡수 경계 밖의 미선언 형제 호출 — 등록된 경계로 감싸거나 감사표에 근거와 함께 "
        f"선언하라: {undeclared}"
    )


def test_audit_table_has_no_stale_entries():
    """감사표는 실제 호출 지점만 담는다 — 사라진 지점이 근거로 남아 있으면 red."""
    live = {
        (function, api)
        for function, api, _lineno in _sibling_calls(
            (TOOLS / "pm_update.py").read_text(encoding="utf-8"))
    }
    stale = sorted(f"{function}:{api}" for function, api in _UNABSORBED_SIBLING_CALLS
                   if (function, api) not in live)
    assert not stale, f"감사표에 남은 죽은 항목: {stale}"


@pytest.mark.parametrize(
    ("function", "api"),
    sorted(key for key, reason in _UNABSORBED_SIBLING_CALLS.items()
           if reason == "no-verified-load"),
)
def test_unabsorbed_sibling_apis_cannot_reach_a_verified_load(function, api):
    """흡수 없이 남긴 API 는 verifier 로드에 도달할 수 없어야 한다(=skew 표면이 아니다).

    도달하게 되는 순간 red 다 — 그 지점은 그때 흡수 경계로 감싸야 한다. 감사표의 근거를 선언이
    아니라 **판정**으로 유지하는 장치다."""
    owner_name, _, attribute = api.partition(".")
    owner = TOOLS / _SIBLING_NAMES[owner_name]
    path = _reaches_verified_load(owner.read_text(encoding="utf-8"), attribute)
    assert path is None, (
        f"{function} 이 흡수 없이 부르는 {api} 가 verifier 로드에 도달한다: {' → '.join(path or [])}"
    )


def test_protected_hook_in_sync_is_covered_by_an_absorbing_caller():
    """`_protected_hook_in_sync` 의 skew 재전파는 호출부 흡수가 받는다(감사표 근거 재판정).

    여기서 흡수해 False(=drift)로 내리면 혼합 트리의 엔진으로 훅을 **생성·설치**하게 되므로
    재전파가 옳다. 대신 유일한 호출부가 등록된 경계로 흡수하는지를 못박는다."""
    tree = ast.parse((TOOLS / "pm_update.py").read_text(encoding="utf-8"))
    callers = sorted(
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(child, ast.Call)
            and _called_name(child) == "_protected_hook_in_sync"
            for child in ast.walk(node)
        )
    )
    assert callers == ["reinstall_protected_hooks"]
    caller = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "reinstall_protected_hooks"
    )
    assert any(
        _handler_absorbs(handler)
        for node in ast.walk(caller) if isinstance(node, ast.Try)
        for handler in node.handlers
    )


def test_recovery_reason_ledger_matches_its_call_sites(pm_update):
    """장부 ↔ 호출 지점 1:1 — 미등록 경계도, 사용처 없는 사유도 없다."""
    tree = ast.parse((TOOLS / "pm_update.py").read_text(encoding="utf-8"))
    used = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == _RECOVERY_MARKER
        and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
    }
    registered = set(pm_update._ENGINE_REV_SKEW_RECOVERY_REASONS)
    assert used == registered
    assert all(reason.strip()
               for reason in pm_update._ENGINE_REV_SKEW_RECOVERY_REASONS.values())


def test_unregistered_boundary_is_fail_loud(pm_update):
    """사유 없는 흡수는 금지 — 등록되지 않은 경계 이름은 ValueError."""
    with pytest.raises(ValueError):
        pm_update._absorb_engine_rev_skew_for_recovery(_marked_skew(), "made_up_boundary")


# ── §2 경계별 흡수 동작 ─────────────────────────────────────────────────────


def test_installed_entry_notation_manifests_absorbs_midsync_skew(pm_update, monkeypatch,
                                                                 tmp_path):
    """설치 하네스 판별(계획 전)이 혼합 트리에서 죽지 않고 core manifest 로 계속한다."""
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: _SkewingSibling())
    upstream = [tmp_path / "engine.manifest"]
    paths = pm_update._installed_entry_notation_manifests(tmp_path, tmp_path, upstream)
    assert paths == upstream
    assert pm_update._ABSORBED_ENGINE_REV_SKEW[-1] == "installed_entry_notation_manifests"


def test_sync_adapter_configs_absorbs_midsync_skew(pm_update, monkeypatch, tmp_path):
    """어댑터 config 채널은 unavailable 로 내려간다(완료 게이트는 여전히 red)."""
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: _SkewingSibling())
    result = pm_update.sync_adapter_configs(tmp_path, tmp_path, write=True)
    assert result["status"] == "unavailable"
    assert pm_update._adapter_config_gate_failed(result) is True


def test_check_adapter_hook_sets_absorbs_midsync_skew(pm_update, monkeypatch, tmp_path):
    """훅 세트 세대 검사(가드)는 unavailable 로 접고 동기를 완주시킨다."""
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: _SkewingSibling())
    result = pm_update.check_adapter_hook_sets(tmp_path, tmp_path)
    assert result["status"] == "unavailable"
    assert pm_update._adapter_hook_set_gate_failed(result) is False


def test_refuse_partial_hook_set_scope_absorbs_midsync_skew(pm_update, monkeypatch,
                                                            capsys):
    """훅과 무관한 경로 스코프는 가드 판정을 잃어도 통과한다(복구 전파 자기잠금 금지)."""
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: _SkewingSibling())
    rc = pm_update.refuse_partial_hook_set_scope(
        [(".project_manager/wiki/pm_role.md", None, None, "update")],
        [(".project_manager/wiki/pm_role.md", None, None, "update")],
        None,
    )
    assert rc == 0
    assert "훅 세트 부분 전파 가드를 건너뛰었다" in capsys.readouterr().err


def test_absorbed_skew_does_not_bypass_partial_hook_scope_refusal(pm_update, monkeypatch,
                                                                  capsys):
    """흡수 경로로 들어왔다고 훅 영역 반쪽 갱신이 통과하면 가드가 skew 한 줄로 우회된다.

    "결합 묶음을 검증할 수 없다" 는 인식 상태는 선언 미해소 폴백과 같으므로 처분도 같다 —
    훅 네임스페이스 하위는 fail-closed(rc1), 탈출구는 스코프 없는 pm-update."""
    real_pm_import = _load_tool("pm_import")

    class _NamespacesOnly(_SkewingSibling):
        """네임스페이스 조회만 살아 있는 사본 — 판정 API 는 skew(실측 형상)."""

        def __getattr__(self, name):
            if name == "hook_set_namespaces":
                return real_pm_import.hook_set_namespaces
            return super().__getattr__(name)

    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: _NamespacesOnly())
    namespace = real_pm_import.hook_set_namespaces(None)[0]
    scoped = [(f"{namespace}/wrapper.py", None, None, "update")]
    rc = pm_update.refuse_partial_hook_set_scope(scoped, scoped, None)
    assert rc == 1
    err = capsys.readouterr().err
    assert "어댑터 훅 영역의 부분 전파를 거부한다" in err
    assert "스코프 없이 pm-update" in err


def test_record_upstream_revs_absorbs_midsync_skew(pm_update, monkeypatch, tmp_path,
                                                   capsys):
    """apply 이후의 baseline 기록도 혼합 트리에서 동기를 죽이지 않는다(기록만 생략)."""
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("upstream.path=/somewhere\n", encoding="utf-8")
    sibling = SimpleNamespace(
        read_upstream_rev=lambda _source: "deadbeef",
        _write_conf_keys_locked=lambda *_a, **_k: (_ for _ in ()).throw(_marked_skew()),
        classify_upstream=lambda _value: "path",
    )
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: sibling)
    changed, updates = pm_update.record_upstream_revs(tmp_path, tmp_path)
    assert (changed, updates) == (False, {})
    assert "upstream.rev 기록을 건너뛴다" in capsys.readouterr().err


def test_record_upstream_revs_still_propagates_non_skew_failures(pm_update, monkeypatch,
                                                                 tmp_path):
    """흡수는 marked skew 한정 — 일반 write 실패는 종전대로 올라간다(무차별 삼킴 금지)."""
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("upstream.path=/somewhere\n", encoding="utf-8")
    sibling = SimpleNamespace(
        read_upstream_rev=lambda _source: "deadbeef",
        _write_conf_keys_locked=lambda *_a, **_k: (_ for _ in ()).throw(
            OSError("disk gone")),
        classify_upstream=lambda _value: "path",
    )
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: sibling)
    with pytest.raises(OSError):
        pm_update.record_upstream_revs(tmp_path, tmp_path)


def test_reinstall_protected_hooks_absorbs_nested_skew_from_in_sync_probe(
        pm_update, monkeypatch, tmp_path):
    """`_protected_hook_in_sync` 의 재전파를 호출부가 받아 unavailable 로 내린다(회귀 단언)."""
    monkeypatch.setattr(pm_update, "_load_dest_pm_config", lambda _dest: _SkewingSibling())
    result = pm_update.reinstall_protected_hooks(tmp_path, write=False)
    assert result["status"] == "unavailable"
    assert "엔진 사본 불일치" in result["reason"]


# ── §3 흡수의 짝 — 종료 시 수렴 검증 ─────────────────────────────────────────


_INVENTORY = ("board.py", "domain.py", "pm_import.py")


def _write_tools(root: Path, files: dict[str, str | None]) -> Path:
    """`{파일명: rev}` 로 목적지 엔진 사본을 만든다(rev None = 스탬프 리터럴 없는 구세대 사본)."""
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for name, rev in files.items():
        body = "VALUE = 1\n" if rev is None else f'ENGINE_REV = "{rev}"\nVALUE = 1\n'
        (tools / name).write_text(body, encoding="utf-8")
    return tools


def _write_upstream_engine_rev(root: Path, rev: str, inventory=_INVENTORY) -> Path:
    """상류 `engine_rev.py` — 기대 rev + 활성 stamped inventory 의 단일 진실."""
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    listing = ", ".join(repr(name) for name in inventory)
    (tools / "engine_rev.py").write_text(
        f'ENGINE_REV = "{rev}"\nSTAMPED_MODULES = ({listing},)\n', encoding="utf-8")
    return tools


def _fresh_run(pm_update):
    """실행 스코프 초기화 — 수렴 판정은 실행당 1회 캐시라 테스트마다 리셋한다."""
    pm_update._ENGINE_REV_CONVERGENCE = None
    pm_update._ABSORBED_ENGINE_REV_SKEW.clear()


def test_convergence_is_silent_when_tree_matches_upstream_rev(pm_update, tmp_path, capsys):
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v9.9.9", "domain.py": "v9.9.9",
                        "pm_import.py": "v9.9.9"})
    assert pm_update._verify_engine_rev_convergence(dest, source) is True
    assert capsys.readouterr().err == ""


def test_convergence_scans_stamped_inventory_not_directory_glob(pm_update, tmp_path,
                                                                capsys):
    """폐기 모듈은 판정 대상이 아니다 — 동기가 지우지 않으므로 세면 영구 오경고가 된다."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    tools = _write_tools(dest, {"board.py": "v9.9.9", "domain.py": "v9.9.9",
                                "pm_import.py": "v9.9.9"})
    # 활성 inventory 밖(옛 세대가 남긴 잔존물) — 구 rev 를 들고 있어도 조용해야 한다.
    (tools / "retired_tool.py").write_text('ENGINE_REV = "v0.0.0-stale"\n', encoding="utf-8")
    assert pm_update._verify_engine_rev_convergence(dest, source) is True
    assert capsys.readouterr().err == ""


def test_unstamped_active_module_is_unconverged(pm_update, tmp_path, capsys):
    """활성 모듈의 **리터럴 부재**는 verifier 가 skew 로 판정하는 상태 — 미수렴으로 센다."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v9.9.9", "domain.py": "v9.9.9",
                        "pm_import.py": None})
    assert pm_update._verify_engine_rev_convergence(dest, source) is False
    err = capsys.readouterr().err
    assert "pm_import.py(스탬프 없음)" in err
    assert "pm-update 를 한 번 더" in err


def test_convergence_compares_against_upstream_not_local_majority(pm_update, tmp_path,
                                                                  capsys):
    """중단 초기(구 rev 다수)에도 방금 착지한 새 파일을 straggler 로 오지목하지 않는다."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v0.0.0-stale", "domain.py": "v0.0.0-stale",
                        "pm_import.py": "v9.9.9"})     # 다수는 구 rev·새 사본은 하나뿐
    assert pm_update._verify_engine_rev_convergence(dest, source) is False
    err = capsys.readouterr().err
    assert "상류 기대 rev v9.9.9 와 어긋난 사본 2건" in err
    assert "board.py(v0.0.0-stale)" in err
    assert "pm_import.py" not in err        # 새 사본은 지목 대상이 아니다


def test_convergence_report_degrades_to_rev_groups_without_upstream(pm_update, tmp_path,
                                                                    capsys):
    """상류 기대 rev 를 못 읽으면 단정하지 않고 rev별 그룹으로 보고한다."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    (source / ".project_manager" / "tools").mkdir(parents=True)   # engine_rev 부재
    _write_tools(dest, {"board.py": "v9.9.9", "domain.py": "v0.0.0-stale"})
    assert pm_update._verify_engine_rev_convergence(dest, source) is False
    err = capsys.readouterr().err
    assert "상류 기대 rev 미해소" in err
    assert "v9.9.9: board.py" in err
    assert "v0.0.0-stale: domain.py" in err


def test_convergence_is_no_judgment_without_engine_copies(pm_update, tmp_path, capsys):
    """활성 모듈 사본이 **하나도 없는** 형상은 미수렴이 아니라 무판정이다(rc·baseline 영향 0)."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    (dest / ".project_manager" / "tools").mkdir(parents=True)
    assert pm_update._verify_engine_rev_convergence(dest, source) is True
    assert capsys.readouterr().err == ""


def test_all_unstamped_tree_is_unconverged_not_no_judgment(pm_update, tmp_path, capsys):
    """사본이 **있는데 전부** 리터럴 없는 구형이면 미수렴이다 — 무판정으로 접으면 안 된다.

    사본 전무(스캐폴드)와 전부-구형은 다른 상태다. 후자를 무판정으로 접으면 스탬프 이전 세대
    트리가 baseline 갱신 + rc0 으로 "흡수 완료" 처리되어 그 상태가 영구 침묵한다."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": None, "domain.py": None, "pm_import.py": None})
    assert pm_update._verify_engine_rev_convergence(dest, source) is False
    err = capsys.readouterr().err
    assert "board.py(스탬프 없음)" in err
    assert "pm_import.py(스탬프 없음)" in err


def test_all_unstamped_tree_suppresses_baseline_and_fails_rc(pm_update, tmp_path,
                                                             monkeypatch, capsys):
    """전부-구형 트리: baseline 억제 + 비영 rc (부분 픽스처의 무판정 rc0 과 갈린다)."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": None})
    calls = []
    monkeypatch.setattr(pm_update, "record_upstream_revs",
                        lambda *args, **kwargs: calls.append(args) or (False, {}))

    def fake_main(_argv):
        pm_update._SYNC_RUN_SCOPE = (dest, source, True)
        pm_update.converge_upstream_revs(dest, source, "in_sync", [])
        return 0

    monkeypatch.setattr(pm_update, "_main", fake_main)
    assert pm_update.main([]) == pm_update._UNCONVERGED_RC
    assert calls == []                                   # baseline 미기록
    assert "수렴하지 않았다" in capsys.readouterr().err


def test_tree_without_any_active_module_copy_stays_rc0(pm_update, tmp_path, monkeypatch):
    """사본 전무 트리는 무판정 — baseline 기록·rc0 이 종전대로다(전부-구형과 대조)."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    (dest / ".project_manager" / "tools").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(pm_update, "record_upstream_revs",
                        lambda *args, **kwargs: calls.append(args) or (False, {}))

    def fake_main(_argv):
        pm_update._SYNC_RUN_SCOPE = (dest, source, True)
        pm_update.converge_upstream_revs(dest, source, "in_sync", [])
        return 0

    monkeypatch.setattr(pm_update, "_main", fake_main)
    assert pm_update.main([]) == 0
    assert len(calls) == 1                               # baseline 기록 유지


def test_convergence_report_counts_absorbed_boundaries(pm_update, tmp_path, capsys):
    """흡수 건수를 함께 보고한다 — 흡수와 미해소가 같은 자리에서 읽혀야 한다."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v9.9.9", "pm_import.py": "v0.0.0-stale"})
    pm_update._absorb_engine_rev_skew_for_recovery(
        _marked_skew(), "installed_entry_notation_manifests")
    try:
        assert pm_update._verify_engine_rev_convergence(dest, source) is False
    finally:
        pm_update._ABSORBED_ENGINE_REV_SKEW.clear()
    assert "중첩 로드 skew 1건을 흡수" in capsys.readouterr().err


def test_convergence_verdict_is_computed_once_per_run(pm_update, tmp_path, capsys):
    """baseline 억제와 종료 rc 가 같은 판정을 쓰고 보고는 한 번만 나간다."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v0.0.0-stale"})
    assert pm_update._verify_engine_rev_convergence(dest, source) is False
    assert pm_update._verify_engine_rev_convergence(dest, source) is False
    assert capsys.readouterr().err.count("수렴하지 않았다") == 1


def test_unconverged_run_suppresses_upstream_rev_baseline(pm_update, tmp_path,
                                                          monkeypatch, capsys):
    """미수렴이면 baseline 을 박지 않는다 — manifest skew 억제와 같은 패턴."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v0.0.0-stale"})
    calls = []
    monkeypatch.setattr(pm_update, "record_upstream_revs",
                        lambda *args, **kwargs: calls.append(args) or (False, {}))
    pm_update.converge_upstream_revs(dest, source, "in_sync", [])
    assert calls == []
    assert "baseline" in capsys.readouterr().out


def test_converged_run_still_records_upstream_rev_baseline(pm_update, tmp_path,
                                                           monkeypatch):
    """수렴이면 종전대로 기록한다(억제가 상시 걸리지 않는다)."""
    _fresh_run(pm_update)
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v9.9.9"})
    calls = []
    monkeypatch.setattr(pm_update, "record_upstream_revs",
                        lambda *args, **kwargs: calls.append(args) or (False, {}))
    pm_update.converge_upstream_revs(dest, source, "in_sync", [])
    assert len(calls) == 1


def test_main_reports_and_fails_when_write_run_ends_unconverged(pm_update, tmp_path,
                                                                monkeypatch, capsys):
    """미수렴 종료는 성공으로 보고하지 않는다(rc0 → 비영·게이트 rc 는 보존)."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v0.0.0-stale"})

    def fake_main(_argv):
        pm_update._SYNC_RUN_SCOPE = (dest, source, True)
        return 0

    monkeypatch.setattr(pm_update, "_main", fake_main)
    assert pm_update.main([]) == pm_update._UNCONVERGED_RC
    assert "수렴하지 않았다" in capsys.readouterr().err
    assert pm_update._SYNC_RUN_SCOPE is None      # 실행 간 누수 없음


def test_main_preserves_existing_nonzero_rc_when_unconverged(pm_update, tmp_path,
                                                             monkeypatch):
    """기존 게이트 rc 를 덮어쓰지 않는다(진단 우선순위 보존)."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v0.0.0-stale"})

    def fake_main(_argv):
        pm_update._SYNC_RUN_SCOPE = (dest, source, True)
        return 2

    monkeypatch.setattr(pm_update, "_main", fake_main)
    assert pm_update.main([]) == 2


def test_main_dry_run_reports_without_setting_rc(pm_update, tmp_path, monkeypatch,
                                                 capsys):
    """무write 실행은 보고만 한다 — dry-run 의 rc 는 "계획이 온전한가" 다."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v0.0.0-stale"})

    def fake_main(_argv):
        pm_update._SYNC_RUN_SCOPE = (dest, source, False)
        return 0

    monkeypatch.setattr(pm_update, "_main", fake_main)
    assert pm_update.main([]) == 0
    assert "수렴하지 않았다" in capsys.readouterr().err


def test_main_verifies_convergence_even_when_an_exception_escapes(
        pm_update, tmp_path, monkeypatch, capsys):
    """최외곽까지 올라간 예외도 진단을 잃지 않는다(예외 경로가 검증을 건너뛰지 않는다)."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")
    _write_tools(dest, {"board.py": "v0.0.0-stale"})

    def fake_main(_argv):
        pm_update._SYNC_RUN_SCOPE = (dest, source, True)
        raise _marked_skew()

    monkeypatch.setattr(pm_update, "_main", fake_main)
    with pytest.raises(RuntimeError):
        pm_update.main([])
    assert "수렴하지 않았다" in capsys.readouterr().err


def test_baked_engine_revs_splits_stamped_and_unstamped(pm_update, tmp_path):
    """판정 입력은 소스 텍스트만 읽는다 — 진단기가 그 혼합 때문에 로드로 죽으면 안 된다."""
    tools = _write_tools(tmp_path, {"board.py": "v9.9.9", "domain.py": None})
    (tools / "broken.py").write_bytes(b"\xff\xfe not utf-8")
    revs, unstamped = pm_update.baked_engine_revs(
        tmp_path, ("board.py", "domain.py", "broken.py", "absent.py"))
    assert revs == {"board.py": "v9.9.9"}
    assert unstamped == ["domain.py"]      # 부재·읽기 실패는 다른 축 소관


# ── §4 mid-sync 혼합 rev 재현 (실 트리·subprocess) ───────────────────────────


def _stale_rev_copy(tools: Path, name: str, current_rev: str) -> None:
    path = tools / name
    text = path.read_text(encoding="utf-8")
    current = f'ENGINE_REV = "{current_rev}"'
    assert text.count(current) == 1, name
    path.write_text(text.replace(current, 'ENGINE_REV = "v0.0.0-stale"', 1),
                    encoding="utf-8")


def _build_engine_tree(root: Path, *, shipped_adapter: bool = False) -> Path:
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for path in TOOLS.glob("*.py"):
        shutil.copy(path, tools / path.name)
    (root / ".project_manager" / "engine.manifest").write_text(
        ".project_manager/tools\n", encoding="utf-8")
    if shipped_adapter:
        # 설치 하네스 판별이 **상류 출하물을 열거**해야 형제 로더(verifier)까지 들어간다 —
        #   templates/ 가 없으면 그 판별이 열거 전에 접혀 skew 창 자체가 안 열린다(픽스처 감도).
        skill = root / "templates" / "claude_code" / ".claude" / "skills"
        (skill / "pm-bootstrap").mkdir(parents=True)
        (skill / "pm-bootstrap" / "SKILL.md").write_text("# pm-bootstrap\n",
                                                         encoding="utf-8")
    return tools


def _commit_tree(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "engine"],
        check=True, capture_output=True, text=True)


def test_mixed_rev_tree_does_raise_skew_on_the_nested_load(tmp_path):
    """재현 — 혼합 트리에서 형제 pm_import 의 중첩 로드는 실제로 marked skew 를 낸다.

    수정 후 동기가 완주하는 게 "skew 가 안 났기 때문" 이 아님을 못박는 짝 단언이다."""
    current_rev = _load_tool("engine_rev").ENGINE_REV
    tools = _build_engine_tree(tmp_path / "dest")
    _stale_rev_copy(tools, "pm_import.py", current_rev)
    pm_import = _load_tool("pm_import", tools)
    with pytest.raises(RuntimeError) as exc:
        pm_import._load_repo_owned_files()
    assert getattr(exc.value, "_engine_rev_skew", False) is True


def test_midsync_mixed_rev_sync_completes_and_converges(tmp_path):
    """혼합 rev 목적지에서 돌린 pm-update 가 자가-차단 없이 완주하고 트리를 수렴시킨다.

    목적지 형상은 업그레이드 실측 그대로다 — 구 pm_import 사본 + 나머지 현행. 그 조합에서
    설치 하네스 판별(계획 전)이 중첩 로드로 skew 를 내는데, 그게 동기 자신을 죽이면 채택자는
    사본 불일치를 고칠 채널을 잃는다."""
    current_rev = _load_tool("engine_rev").ENGINE_REV
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _build_engine_tree(source, shipped_adapter=True)
    _commit_tree(source)
    dest_tools = _build_engine_tree(dest)
    _stale_rev_copy(dest_tools, "pm_import.py", current_rev)
    (dest / ".project_manager" / "local.conf").write_text(
        f"upstream.path={source}\n", encoding="utf-8")

    # 감도 앵커 — 동기 **직전** 트리에서 설치 하네스 판별(`_installed_entry_notation_manifests`
    #   가 부르는 바로 그 API)은 실제로 marked skew 를 낸다. 아래 완주가 "skew 창이 안 열려서"
    #   green 인 게 아님을 못박는다.
    with pytest.raises(RuntimeError) as exc:
        _load_tool("pm_import", dest_tools).installed_harnesses(dest, source)
    assert getattr(exc.value, "_engine_rev_skew", False) is True

    proc = subprocess.run(
        [sys.executable, str(dest_tools / "pm_update.py"), "--from", str(source)],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8",
        env=utf8_child_env(), timeout=600,
    )
    combined = proc.stdout + proc.stderr
    assert "Traceback" not in combined, combined
    # 사본 불일치는 **흡수된 강등 알림**으로만 나타난다 — 미흡수 실패로 새어 나오면 red 다.
    # 판독 seam(T-0729)은 구세대 형제에서 강등할 때 사유로 원문 진단을 싣는다: 그 줄은 흡수의
    # 증거지 실패가 아니므로, 판정은 "그 문구를 가진 줄이 강등 알림인가" 로 한다.
    unabsorbed = [
        line for line in combined.splitlines()
        if "엔진 사본 버전 불일치" in line and "일반 읽기로 진행합니다" not in line
    ]
    assert not unabsorbed, combined
    assert proc.returncode == 0, combined
    # 혼합이 실제로 해소됐다 — 수렴 검증도 조용하다(경고 없음).
    engine = _load_tool("pm_update")
    inventory, expected = engine.engine_rev_expectation(source)
    revs, unstamped = engine.baked_engine_revs(dest, inventory)
    assert expected == current_rev
    assert set(revs.values()) == {current_rev} and not unstamped
    assert "수렴하지 않았다" not in combined
    assert "upstream.rev=" in (dest / ".project_manager" / "local.conf").read_text(
        encoding="utf-8")                  # 수렴했으므로 baseline 은 종전대로 기록된다


def test_mixed_source_tree_fails_loud_instead_of_silently_copying(tmp_path):
    """혼합 `--from` 을 그대로 복사한 실행은 성공으로 보고하지 않는다 (침묵 루프 폐쇄).

    흡수만 하고 rc0 + baseline 기록이면 소스가 혼합인 한 재실행도 영영 못 고치는데 drift-lint 는
    "최신" 으로 침묵한다 — 종료 시 미수렴은 baseline 억제 + 비영 rc 로 끝나야 한다."""
    current_rev = _load_tool("engine_rev").ENGINE_REV
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source_tools = _build_engine_tree(source, shipped_adapter=True)
    _stale_rev_copy(source_tools, "pm_import.py", current_rev)   # 상류 자신이 혼합
    _commit_tree(source)
    dest_tools = _build_engine_tree(dest)
    (dest / ".project_manager" / "local.conf").write_text(
        f"upstream.path={source}\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(dest_tools / "pm_update.py"), "--from", str(source)],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8",
        env=utf8_child_env(), timeout=600,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "수렴하지 않았다" in combined
    assert "pm_import.py(v0.0.0-stale)" in combined      # 어긋난 사본 지목
    assert "`--from` 트리 자신이 혼합" in combined        # 재실행이 답이 아님을 말한다
    assert "baseline" in combined                        # 억제 사실
    assert "upstream.rev=" not in (dest / ".project_manager" / "local.conf").read_text(
        encoding="utf-8")


# ── §5 부분 전파의 흡수 보고 · 수렴 결과 소비 (T-0611) ────────────────────────
# 흡수의 짝(종료 시 수렴 검증)은 전량 실행에만 걸린다 — `--paths` 는 혼합이 *정상 결과*라 검증
# 대상이 아니기 때문이다. 그런데 계획-전 형제 로드(`_installed_entry_notation_manifests` 등)는
# 스코프와 무관하게 돌아 skew 를 흡수하므로, 비엔진 경로만 지목한 실행은 흡수 장부를 아무도 읽지
# 않은 채 rc0 으로 끝난다. 이 절이 고정하는 성질:
#
#   부분 전파 실행도 흡수 사실은 **반드시 보고**한다(rc 는 불변 — report-only).
#   그리고 미수렴으로 끝나는 실행은 baseline 뿐 아니라 **opt-in 프롬프트도 건너뛴다.**


def _sync_tree(tmp_path: Path, *, dest_rev: str) -> tuple[Path, Path, str]:
    """(dest, source, 전파 대상 relpath) — 실 sync 1회를 태우는 최소 트리.

    dest 의 stamped 사본 rev 하나로 수렴/미수렴을 만든다(상류 기대 rev 는 v9.9.9 고정)."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    rel = ".project_manager/wiki/pm_role.md"
    (source / rel).parent.mkdir(parents=True, exist_ok=True)
    (source / rel).write_text("# 상류 문서\n", encoding="utf-8")
    _write_upstream_engine_rev(source, "v9.9.9", ("board.py",))
    (source / ".project_manager" / "engine.manifest").write_text(
        rel + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-f", "-A"], check=True)
    _write_tools(dest, {"board.py": dest_rev})
    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
    (dest / rel).write_text("# 구 문서\n", encoding="utf-8")
    return dest, source, rel


def test_partial_run_reports_absorbed_skew_at_exit(pm_update, tmp_path, monkeypatch,
                                                   capsys):
    """`--paths` 실행이 흡수한 skew 는 종료 시 보고된다 — 장부가 조용히 폐기되면 안 된다.

    비엔진 경로만 지목하면 그 뒤 어댑터·훅 채널이 전부 꺼지므로, 흡수 사실을 알릴 자리가 이
    종료 지점 말고는 없다. rc 는 건드리지 않는다(부분 전파는 혼합이 정상 결과다)."""
    dest, source, rel = _sync_tree(tmp_path, dest_rev="v9.9.9")
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: _SkewingSibling())

    rc = pm_update.main(["--from", str(source), "--paths", rel])

    err = capsys.readouterr().err
    assert rc == 0, err                       # report-only — rc 불변
    assert (dest / rel).read_text(encoding="utf-8") == "# 상류 문서\n", \
        "보고 때문에 요청 경로 전파가 막혔다"
    assert "경로 스코프 실행이 엔진 사본 rev 혼합 skew" in err, f"장부가 폐기됐다: {err!r}"
    assert "installed_entry_notation_manifests" in err, "어느 경계가 흡수했는지 안 나온다"
    assert "스코프 없이 pm-update" in err, "처방 부재"


def test_partial_run_is_quiet_when_nothing_was_absorbed(pm_update, tmp_path,
                                                        monkeypatch, capsys):
    """흡수가 없으면 조용하다 — 정상 부분 전파에 상시 알림이 붙으면 신호가 죽는다."""
    dest, source, rel = _sync_tree(tmp_path, dest_rev="v9.9.9")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--from", str(source), "--paths", rel])

    err = capsys.readouterr().err
    assert rc == 0, err
    assert "엔진 사본 rev 혼합 skew" not in err, err


def test_partial_run_scope_does_not_leak_between_runs(pm_update, tmp_path, monkeypatch,
                                                      capsys):
    """부분 전파 스코프는 실행마다 초기화된다 — 다음 실행이 남의 장부를 보고하면 안 된다."""
    dest, source, rel = _sync_tree(tmp_path, dest_rev="v9.9.9")
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: _SkewingSibling())
    assert pm_update.main(["--from", str(source), "--paths", rel]) == 0
    assert pm_update._PARTIAL_RUN_SCOPE is None
    capsys.readouterr()

    monkeypatch.undo()
    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main(["--from", str(source), "--paths", rel]) == 0
    assert "엔진 사본 rev 혼합 skew" not in capsys.readouterr().err


def test_unconverged_run_skips_the_baseline_update(pm_update, tmp_path, monkeypatch,
                                                   capsys):
    """미수렴 실행은 upstream baseline 을 갱신하지 않는다 — 성공하지 않은 실행이라서다."""
    dest, source, rel = _sync_tree(tmp_path, dest_rev="v0.0.0-stale")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--from", str(source)])

    err = capsys.readouterr().err
    assert rc == pm_update._UNCONVERGED_RC, err
    assert (dest / rel).read_text(encoding="utf-8") == "# 상류 문서\n", \
        "미수렴 게이트가 파일 적용까지 되돌렸다"


def test_converged_run_finishes_with_rc_zero(pm_update, tmp_path, monkeypatch,
                                             capsys):
    """수렴 실행은 종전대로 rc 0 — 게이트가 정상 경로까지 좁히면 안 된다."""
    dest, source, rel = _sync_tree(tmp_path, dest_rev="v9.9.9")
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--from", str(source)])

    assert rc == 0, capsys.readouterr().err


def test_converge_returns_the_convergence_verdict(pm_update, tmp_path, capsys):
    """`converge_upstream_revs` 는 수렴 여부를 돌려준다 — 호출부가 프롬프트를 그 값으로 가른다."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")

    _fresh_run(pm_update)
    _write_tools(dest, {"board.py": "v9.9.9"})
    assert pm_update.converge_upstream_revs(dest, source, "in_sync", []) is True

    _fresh_run(pm_update)
    _write_tools(dest, {"board.py": "v0.0.0-stale"})
    assert pm_update.converge_upstream_revs(dest, source, "in_sync", []) is False
    assert "미수렴" in capsys.readouterr().out


def test_manifest_skew_branch_reports_the_same_verdict(pm_update, tmp_path, capsys):
    """manifest skew 억제는 baseline 축이라 rev 수렴 판정과 독립이다(반환은 수렴 여부 하나).

    그 분기에서 무조건 True 를 돌려주면 혼합 트리 + manifest skew 조합이 프롬프트까지 간다."""
    source, dest = tmp_path / "source", tmp_path / "dest"
    _write_upstream_engine_rev(source, "v9.9.9")

    _fresh_run(pm_update)
    _write_tools(dest, {"board.py": "v9.9.9"})
    assert pm_update.converge_upstream_revs(dest, source, "skew", ["new.py"]) is True

    _fresh_run(pm_update)
    _write_tools(dest, {"board.py": "v0.0.0-stale"})
    assert pm_update.converge_upstream_revs(dest, source, "skew", ["new.py"]) is False
    assert "manifest skew" in capsys.readouterr().out
