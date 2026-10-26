"""상류 훅 세트 선언의 package snapshot·하위 프로세스 조회 경계.

상류 선언은 상류 엔진 package 의 배송 Python 파일 전체를 한 번 읽은 bytes 로 결속하고, 그 bytes 만
실행 Home 의 임시 트리에 내려 별도 `python -I -B` 프로세스에서 직렬화해 받는다. 이 파일이 고정하는
성질:

  - 두 실행 Home·두 상류는 서로의 세대·임시 트리를 섞지 않는다(부모 프로세스 선언 혼입 0).
  - digest 는 Python 파일의 추가·삭제·이름 변경·내용 변경에 반응하고, 실행된 선언은 해시한 bytes 다.
  - timestamp 유효 `.pyc` 가 해시한 원문을 대신하지 못한다.
  - wire 형식 위반·자식 실패는 사유 있는 미해소이고, 예상하지 못한 오류는 그대로 올라간다.
  - package 를 해소하지 못하면 수용·부분 전파 게이트가 쓰기 전에 멈춘다.
  - 임시 트리 정리 실패는 경고만 남기고 이미 얻은 세대를 유지한다.

픽스처는 실제 package 사본·실행 Home·snapshot 을 쓴다(저장소 밖 임시 경로).
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pm_engine.app import adapter_accept
from pm_engine.app import hook_sets
from pm_engine.domain import adapter_specs
from pm_engine.infra import adapter_baseline
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import pm_paths
from pm_engine.infra import raw_output
from pm_engine.infra import repo_files
from pm_engine.infra import upstream_declarations
from pm_engine.app import update_adapters

from test_adapter_hook_set_generation import (
    _DRIVER_NEW, _DRIVER_OLD, _DRIVER_REL, _SETTINGS_REL, _UPSTREAM_DECLARATION_REL,
    _UPSTREAM_PACKAGE_REL, _WRAPPER_NEW, _WRAPPER_REL, _flag_declaration_tail, _make_case,
    _package_digest, _plant_upstream_package, _settings,
)

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
_LOCAL_DECLARATION = TOOLS / "pm_engine" / "domain" / "adapter_specs.py"

# 두 상류가 각자만 아는 플래그 — 같은 길이라 두 세대의 선언 파일 bytes 길이가 같다.
_FOREIGN_FLAG_A = "--s9-foreign-gen-a"
_FOREIGN_FLAG_B = "--s9-foreign-gen-b"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def _pm_update_cli():
    return _load("pm_update")


@pytest.fixture
def pm_update(_pm_update_cli, monkeypatch):
    from _manager_home import complete_in_process
    with complete_in_process(monkeypatch):
        yield _pm_update_cli


def _home(tmp_path: Path, name: str):
    return pm_paths.home_at(tmp_path / name)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """`.git` 밖 모든 파일의 상대 경로 → bytes (읽기 전용 확인용)."""
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.relative_to(root).parts}


def _track(source: Path) -> None:
    subprocess.run(["git", "-C", str(source), "add", "-f", "-A", "--", _UPSTREAM_PACKAGE_REL],
                   check=True)


def _record_staging(monkeypatch) -> list[Path]:
    """선언 조회가 만든 임시 트리 경로를 기록한다(실제 staging 은 그대로 만든다)."""
    created: list[Path] = []
    real_stage = upstream_declarations._stage_snapshot

    def _recorded(home, payloads):
        staging = real_stage(home, payloads)
        created.append(staging)
        return staging

    monkeypatch.setattr(upstream_declarations, "_stage_snapshot", _recorded)
    return created


def _flags(generation) -> set[str]:
    return set(generation.declarations["claude"].flag_support)


def test_two_homes_keep_foreign_generation_and_temp_separate(tmp_path, monkeypatch):
    """두 상류의 서로 다른 세대를 두 실행 Home 이 각자 받는다 — 값·임시 트리·상류 트리가 섞이지 않는다."""
    source_a = tmp_path / "source_a"
    source_b = tmp_path / "source_b"
    _plant_upstream_package(source_a, extra_tail=_flag_declaration_tail(_FOREIGN_FLAG_A))
    _plant_upstream_package(source_b, extra_tail=_flag_declaration_tail(_FOREIGN_FLAG_B))
    before_a, before_b = _tree_bytes(source_a), _tree_bytes(source_b)
    parent_table = dict(adapter_specs.ADAPTER_HOOK_SET)
    home_1, home_2 = _home(tmp_path, "home_1"), _home(tmp_path, "home_2")
    created = _record_staging(monkeypatch)

    generation_a = hook_sets.hook_set_declarations(home_1, source_a, required=True)
    generation_b = hook_sets.hook_set_declarations(home_2, source_b, required=True)

    for generation in (generation_a, generation_b):
        assert generation.origin == hook_sets.HOOK_SET_ORIGIN_UPSTREAM, generation.reasons
        assert set(generation.declarations) == set(parent_table)
        for spec in generation.declarations.values():
            assert type(spec) is adapter_specs.AdapterHookSetSpec
            assert all(type(entry) is adapter_specs.AdapterHookEntrypoint
                       for entry in spec.entrypoints)
            assert all(type(paths) is tuple for paths in spec.flag_support.values())
        assert generation.declarations["codex"] == parent_table["codex"]
    assert _FOREIGN_FLAG_A in _flags(generation_a) and _FOREIGN_FLAG_B not in _flags(generation_a)
    assert _FOREIGN_FLAG_B in _flags(generation_b) and _FOREIGN_FLAG_A not in _flags(generation_b)
    assert generation_a.source_sha256 == _package_digest(source_a)
    assert generation_b.source_sha256 == _package_digest(source_b)
    # 부모 프로세스의 선언은 그대로다(자식 값 혼입 0).
    assert adapter_specs.ADAPTER_HOOK_SET == parent_table
    assert not ({_FOREIGN_FLAG_A, _FOREIGN_FLAG_B}
                & set(adapter_specs.ADAPTER_HOOK_SET["claude"].flag_support))
    # 임시 트리는 각 Home 의 임시 루트 아래에만 생기고 끝나면 지워진다.
    assert [staging.parent for staging in created] == [
        raw_output.temp_root(home_1.root), raw_output.temp_root(home_2.root)]
    assert not any(staging.exists() for staging in created)
    # 상류 트리는 읽기만 했다.
    assert _tree_bytes(source_a) == before_a and _tree_bytes(source_b) == before_b
    assert not (source_a / ".project_manager" / ".local").exists()
    assert not (source_b / ".project_manager" / ".local").exists()


def test_snapshot_bytes_and_accept_recheck_bind_every_python_member(tmp_path, monkeypatch):
    """digest 는 Python 파일 집합 전체를 묶고, 실행된 선언은 해시한 bytes 이며, 수용은 쓰기 직전에 다시 대조한다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        upstream_tail=_flag_declaration_tail(_FOREIGN_FLAG_A))
    package = source / _UPSTREAM_PACKAGE_REL
    declaration = source / _UPSTREAM_DECLARATION_REL
    digest = upstream_declarations._read_declaration_snapshot(source)[1]
    assert digest == _package_digest(source)

    # 파일 집합·내용 변경은 digest 를 바꾸고, 되돌리면 원래 digest 로 돌아온다.
    extra = package / "domain" / "s9_extra.py"
    extra.write_text("VALUE = 1\n", encoding="utf-8")
    _track(source)
    added = upstream_declarations._read_declaration_snapshot(source)[1]
    renamed_path = package / "domain" / "s9_renamed.py"
    subprocess.run(["git", "-C", str(source), "mv", "-f", str(extra), str(renamed_path)], check=True)
    renamed = upstream_declarations._read_declaration_snapshot(source)[1]
    renamed_path.write_text("VALUE = 2\n", encoding="utf-8")
    edited = upstream_declarations._read_declaration_snapshot(source)[1]
    subprocess.run(["git", "-C", str(source), "rm", "-q", "-f", str(renamed_path)], check=True)
    removed = upstream_declarations._read_declaration_snapshot(source)[1]
    assert len({digest, added, renamed, edited}) == 4
    assert removed == digest
    # 실행 집합 밖은 digest 에 들어가지 않는다 — 추적된 바이트코드 캐시·비 Python 파일, 미추적 Python.
    (package / "domain" / "__pycache__").mkdir(exist_ok=True)
    (package / "domain" / "__pycache__" / "adapter_specs.cpython-311.pyc").write_bytes(b"\0stale")
    (package / "domain" / "__pycache__" / "stray.py").write_text("STRAY = 1\n", encoding="utf-8")
    (package / "domain" / "notes.txt").write_text("메모\n", encoding="utf-8")
    _track(source)
    (package / "domain" / "untracked_extra.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert upstream_declarations._read_declaration_snapshot(source)[1] == digest
    (package / "domain" / "untracked_extra.py").unlink()

    # 해시 뒤 원천이 바뀌어도 실행되는 선언은 해시한 bytes 다. 다음 조회는 새 세대·새 digest 다.
    real_stage = upstream_declarations._stage_snapshot

    def _stage_then_replace_source(home, payloads):
        staging = real_stage(home, payloads)
        declaration.write_text(
            _LOCAL_DECLARATION.read_text(encoding="utf-8")
            + _flag_declaration_tail(_FOREIGN_FLAG_B), encoding="utf-8")
        return staging

    with monkeypatch.context() as patch:
        patch.setattr(upstream_declarations, "_stage_snapshot", _stage_then_replace_source)
        hashed = hook_sets.hook_set_declarations(_home(tmp_path, "run"), source, required=True)
    assert _FOREIGN_FLAG_A in _flags(hashed) and _FOREIGN_FLAG_B not in _flags(hashed)
    assert hashed.source_sha256 == digest
    following = hook_sets.hook_set_declarations(_home(tmp_path, "run"), source, required=True)
    assert _FOREIGN_FLAG_B in _flags(following) and _FOREIGN_FLAG_A not in _flags(following)
    assert following.source_sha256 == _package_digest(source) != digest

    # 수용 직전 재대조 — 판정한 세대 digest 와 지금 package 가 다르면 쓰지 않는다.
    decision = hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL, declarations=following)
    assert decision.blockers == [] and decision.generation_sha256 == following.source_sha256
    config_before = (dest / _SETTINGS_REL).read_bytes()
    ledger = dest / ".project_manager" / "adapter_baseline.json"
    ledger_before = ledger.read_bytes() if ledger.exists() else None
    (package / "domain" / "s9_late.py").write_text("LATE = 1\n", encoding="utf-8")
    _track(source)
    raced = adapter_accept.accept_adapter_config(
        dest, source, _SETTINGS_REL,
        expected_template_sha256=decision.template_sha256,
        expected_generation_sha256=decision.generation_sha256)
    assert raced.status == "raced" and "세대 선언 package" in raced.detail, raced
    assert (dest / _SETTINGS_REL).read_bytes() == config_before
    assert (ledger.read_bytes() if ledger.exists() else None) == ledger_before

    # template raw hash 결속은 별도 축으로 남아 있다.
    current = hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=hook_sets.hook_set_declarations(_home(tmp_path, "run"), source, required=True))
    assert current.blockers == [], current
    (source / "templates" / "claude_code" / _SETTINGS_REL).write_text(
        _settings(git_anchor=False), encoding="utf-8")
    template_raced = adapter_accept.accept_adapter_config(
        dest, source, _SETTINGS_REL,
        expected_template_sha256=current.template_sha256,
        expected_generation_sha256=current.generation_sha256)
    assert template_raced.status == "raced" and "상류 template" in template_raced.detail
    assert (dest / _SETTINGS_REL).read_bytes() == config_before


def test_stale_timestamp_pyc_cannot_replace_the_hashed_source(tmp_path, monkeypatch):
    """같은 길이·mtime 의 A→B 교체에서 자식은 B 원문을 실행한다 — 유효한 A `.pyc` 가 끼어들지 못한다."""
    source = tmp_path / "framework"
    declaration = _plant_upstream_package(
        source, extra_tail=_flag_declaration_tail(_FOREIGN_FLAG_A))
    stat_before = declaration.stat()
    cached = Path(py_compile.compile(
        str(declaration), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP))
    declaration.write_text(
        _LOCAL_DECLARATION.read_text(encoding="utf-8")
        + _flag_declaration_tail(_FOREIGN_FLAG_B), encoding="utf-8")
    os.utime(declaration, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    assert declaration.stat().st_size == stat_before.st_size
    # 민감도 — 같은 경로를 제자리 import 하면 A `.pyc` 를 재사용한다.
    in_place = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "from pm_engine.domain import adapter_specs; "
         "print(sorted(adapter_specs.ADAPTER_HOOK_SET['claude'].flag_support))",
         str(source / ".project_manager" / "tools")],
        capture_output=True, text=True, encoding="utf-8", check=True)
    assert cached.is_file() and _FOREIGN_FLAG_A in in_place.stdout, in_place.stdout
    staged_members: list[list[str]] = []
    real_child = upstream_declarations._run_declaration_child

    def _listing_child(home, staged_tools):
        staged_members.append(sorted(
            path.relative_to(staged_tools).as_posix()
            for path in staged_tools.rglob("*") if path.is_file()))
        return real_child(home, staged_tools)

    monkeypatch.setattr(upstream_declarations, "_run_declaration_child", _listing_child)
    parent_flags = set(adapter_specs.ADAPTER_HOOK_SET["claude"].flag_support)

    generation = hook_sets.hook_set_declarations(_home(tmp_path, "run"), source, required=True)

    assert _FOREIGN_FLAG_B in _flags(generation) and _FOREIGN_FLAG_A not in _flags(generation)
    assert generation.source_sha256 == _package_digest(source)
    assert staged_members and all(member.endswith(".py") for member in staged_members[0])
    assert not any("__pycache__" in member for member in staged_members[0]), "pyc 를 복사했다"
    assert set(adapter_specs.ADAPTER_HOOK_SET["claude"].flag_support) == parent_flags


_VALID_SPEC = {
    "config_relpath": ".claude/settings.json",
    "live_files": [".claude/hook.sh"],
    "flag_support": {"--flag": [".claude/hook.sh"]},
    "coupled_groups": [[".claude/hook.sh", ".claude/core.py"]],
    "entrypoints": [{"event": "Stop", "matcher": None, "dispatcher": ".claude/hook.sh",
                     "flag": None}],
}


def _wire(**overrides) -> bytes:
    envelope = {"version": 1, "declarations": {"future-harness": dict(_VALID_SPEC)}}
    envelope.update(overrides)
    return json.dumps(envelope).encode("utf-8")


def _with_spec(**fields) -> bytes:
    return _wire(declarations={"future-harness": {**_VALID_SPEC, **fields}})


def test_wire_and_child_failures_remain_explicit(tmp_path, monkeypatch):
    """wire 위반·자식 실패는 사유가 있는 미해소이고, 예상하지 못한 오류는 그대로 올라간다."""
    decoded = upstream_declarations._decode_declarations(_wire() + b"\n  \n")
    spec = decoded["future-harness"]
    assert type(spec) is adapter_specs.AdapterHookSetSpec
    assert spec.entrypoints[0].matcher is None and spec.flag_support == {"--flag": (".claude/hook.sh",)}
    entry_with_matcher = dict(_VALID_SPEC["entrypoints"][0], matcher="Bash", flag="--flag")
    assert upstream_declarations._decode_declarations(
        _with_spec(entrypoints=[entry_with_matcher]))["future-harness"].entrypoints[0].matcher == "Bash"
    rejected = {
        "누락 키": json.dumps({"version": 1}).encode(),
        "추가 키": _wire(extra=1),
        "중복 키": b'{"version": 1, "version": 1, "declarations": {}}',
        "bool 버전": _wire(version=True),
        "다른 버전": _wire(version=2),
        "빈 선언": _wire(declarations={}),
        "spec 추가 키": _with_spec(unknown=1),
        "live_files 비목록": _with_spec(live_files=".claude/hook.sh"),
        "flag 값 비목록": _with_spec(flag_support={"--flag": ".claude/hook.sh"}),
        "묶음 원소 비목록": _with_spec(coupled_groups=[".claude/hook.sh"]),
        "nullable 비문자열": _with_spec(entrypoints=[dict(_VALID_SPEC["entrypoints"][0], matcher=3)]),
        "NaN": _wire().replace(b'"version": 1', b'"version": NaN'),
        "잔여 출력": _wire() + b"\n{}",
        "UTF-8 아님": b'{"version": 1, "declarations": {"\xff": {}}}',
    }
    for label, payload in rejected.items():
        with pytest.raises((upstream_declarations.DeclarationWireError, ValueError)):
            upstream_declarations._decode_declarations(payload)
        assert label  # 실패 메시지에서 어느 입력인지 보이도록 이름을 둔다.

    source = tmp_path / "framework"
    _plant_upstream_package(source)
    home = _home(tmp_path, "run")
    temp_root = raw_output.temp_root(home.root)

    def _reason_with_program(program: str) -> str:
        with monkeypatch.context() as patch:
            patch.setattr(upstream_declarations, "_DECLARATION_PROGRAM", program)
            table, reason, digest = upstream_declarations._upstream_hook_set_declarations(home, source)
        assert table is None and digest is None, reason
        return reason

    assert "rc=7" in _reason_with_program("import sys; sys.stderr.write('child-stderr'); sys.exit(7)")
    assert "child-stderr" in _reason_with_program(
        "import sys; sys.stderr.write('child-stderr'); sys.exit(7)")
    assert "JSONDecodeError" in _reason_with_program("print('not json')")
    assert "DeclarationWireError" in _reason_with_program(
        "import json; print(json.dumps({'version': True, 'declarations': {}}))")
    with monkeypatch.context() as patch:
        patch.setattr(upstream_declarations, "DECLARATION_TIMEOUT_SECONDS", 0.5)
        timeout_reason = _reason_with_program("import time; time.sleep(30)")
    assert "timeout" in timeout_reason
    with monkeypatch.context() as patch:
        patch.setattr(sys, "executable", str(tmp_path / "no-such-python"))
        table, start_reason, _digest = upstream_declarations._upstream_hook_set_declarations(home, source)
    assert table is None and "FileNotFoundError" in start_reason
    with monkeypatch.context() as patch:
        def _no_space(*_args, **_kwargs):
            raise OSError(28, "No space left on device")
        patch.setattr(upstream_declarations.tempfile, "mkdtemp", _no_space)
        table, stage_reason, _digest = upstream_declarations._upstream_hook_set_declarations(home, source)
    assert table is None and "임시 트리 생성 실패" in stage_reason
    assert list(temp_root.iterdir()) == [], "실패 경로가 임시 트리를 남겼다"

    def _bug(_stdout):
        raise RuntimeError("decoder 버그(주입)")

    with monkeypatch.context() as patch:
        patch.setattr(upstream_declarations, "_decode_declarations", _bug)
        with pytest.raises(RuntimeError, match="decoder 버그"):
            hook_sets.hook_set_declarations(home, source)
    assert list(temp_root.iterdir()) == [], "예외 전파 중에도 임시 트리는 정리된다"


def test_unresolved_package_blocks_accept_and_partial_scope(
        pm_update, tmp_path, monkeypatch, capsys):
    """package 를 해소하지 못하면 조회는 사유와 함께 내려가고, 수용·부분 전파는 쓰기 전에 멈춘다."""
    home = _home(tmp_path, "run")
    unresolved_sources: dict[str, Path] = {}

    flat_only = tmp_path / "flat_only"
    (flat_only / ".project_manager" / "tools").mkdir(parents=True)
    shutil.copy2(TOOLS / "pm_import.py", flat_only / ".project_manager" / "tools" / "pm_import.py")
    unresolved_sources["평면 pm_import 만"] = flat_only

    missing_member = tmp_path / "missing_member"
    _plant_upstream_package(missing_member)
    (missing_member / _UPSTREAM_PACKAGE_REL / "domain" / "__init__.py").unlink()
    subprocess.run(["git", "-C", str(missing_member), "add", "-A", "--", _UPSTREAM_PACKAGE_REL],
                   check=True)
    unresolved_sources["필수 파일 부재"] = missing_member

    untracked = tmp_path / "untracked"
    shutil.copytree(TOOLS / "pm_engine", untracked / _UPSTREAM_PACKAGE_REL,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (untracked / "README.md").write_text("상류\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(untracked), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(untracked), "add", "README.md"], check=True)
    unresolved_sources["추적되지 않은 package"] = untracked

    for label, source in unresolved_sources.items():
        # 추적되지 않은 package 는 기존 배송 열거의 빈 결과 경고를 그대로 낸다(조용한 빈 snapshot 아님).
        loud = (pytest.warns(repo_files.RepoFilesEmptyWarning) if source is untracked
                else contextlib.nullcontext())
        with loud:
            required = hook_sets.hook_set_declarations(home, source, required=True)
            query = hook_sets.hook_set_declarations(home, source)
        assert required.declarations is None, label
        assert required.origin == hook_sets.HOOK_SET_ORIGIN_UNRESOLVED and required.reasons, label
        assert query.origin == hook_sets.HOOK_SET_ORIGIN_LOCAL, label
        assert query.declarations == adapter_specs.ADAPTER_HOOK_SET and query.reasons, label
        assert "상류 선언 package 판독 실패" in query.reasons[0], (label, query.reasons)
    capsys.readouterr()

    readable = tmp_path / "readable"
    _plant_upstream_package(readable)
    real_read = fs.read_bytes_shared

    def _unreadable(path, *args, **kwargs):
        if Path(path).name == "adapter_specs.py":
            raise PermissionError(13, "Permission denied", str(path))
        return real_read(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fs, "read_bytes_shared", _unreadable)
        read_failure = hook_sets.hook_set_declarations(home, readable, required=True)
    assert read_failure.declarations is None
    assert "PermissionError" in read_failure.reasons[0], read_failure.reasons

    # mutation 게이트 — 해소 불가 세대로는 수용 판정이 blocker 를 낸다(config 미변경).
    dest, source = _make_case(
        tmp_path / "accept", dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True), upstream_tail=None)
    blocked = hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=hook_sets.hook_set_declarations(home, source, required=True))
    assert blocked.blockers and "상류 세대 선언" in blocked.blockers[0].detail, blocked
    config_before = (dest / _SETTINGS_REL).read_bytes()

    # 부분 훅 scope — 상류 세대를 확인할 수 없으면 훅 영역 지목은 쓰기 전에 거부한다.
    monkeypatch.setattr(pm_update, "REPO", dest)
    change = (_WRAPPER_REL, source / _WRAPPER_REL, dest / _WRAPPER_REL, "M")
    peer = (_DRIVER_REL, source / _DRIVER_REL, dest / _DRIVER_REL, "M")
    rc = update_adapters.refuse_partial_hook_set_scope(pm_paths.home_at(pm_update.REPO), [change], [change, peer], source)
    assert rc == 1
    assert "상류 훅 세트 세대 선언을 확인할 수 없어" in capsys.readouterr().err
    assert (dest / _SETTINGS_REL).read_bytes() == config_before

    # 해소된 세대 — 구형 드라이버가 남아 있으면 수용은 엔진 파일 선행을 요구한다.
    old_dest, resolved_source = _make_case(
        tmp_path / "order", dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD,
        template_settings=_settings(git_anchor=True))
    resolved = hook_sets.hook_set_declarations(home, resolved_source, required=True)
    assert resolved.origin == hook_sets.HOOK_SET_ORIGIN_UPSTREAM, resolved.reasons
    order = hook_sets.hook_set_accept_decision(
        old_dest, resolved_source, _SETTINGS_REL, declarations=resolved)
    assert order.blockers and any(_DRIVER_REL in item.detail for item in order.blockers), order

    # 세트 수용 결속 — 판정한 digest 가 그대로면 설치되고 원장이 그 내용을 기록한다.
    (old_dest / _DRIVER_REL).write_text(_DRIVER_NEW, encoding="utf-8")
    accepted_decision = hook_sets.hook_set_accept_decision(
        old_dest, resolved_source, _SETTINGS_REL, declarations=resolved)
    assert accepted_decision.blockers == [], accepted_decision
    accepted = adapter_accept.accept_adapter_config(
        old_dest, resolved_source, _SETTINGS_REL,
        expected_template_sha256=accepted_decision.template_sha256,
        expected_generation_sha256=accepted_decision.generation_sha256)
    assert accepted.status not in {"raced", "write-failed"}, accepted
    assert (old_dest / _SETTINGS_REL).read_text(encoding="utf-8") == _settings(git_anchor=True)
    assert adapter_baseline.read_adapter_baseline(old_dest)["files"][_SETTINGS_REL]["sha256"] \
        == adapter_baseline.content_sha256(old_dest / _SETTINGS_REL)


def test_cleanup_warning_preserves_the_resolved_generation(tmp_path, monkeypatch, capsys):
    """자식 성공 뒤 임시 트리 정리가 실패해도 세대·digest 는 유지되고 경고에 남은 경로가 나온다."""
    source = tmp_path / "framework"
    _plant_upstream_package(source, extra_tail=_flag_declaration_tail(_FOREIGN_FLAG_A))
    home, other_home = _home(tmp_path, "run"), _home(tmp_path, "other")
    created = _record_staging(monkeypatch)
    real_rmtree = install_conf._force_rmtree

    def _locked(path, *args, **kwargs):
        raise PermissionError(13, "파일이 잠겨 있음(주입)", str(path))

    with monkeypatch.context() as patch:
        patch.setattr(install_conf, "_force_rmtree", _locked)
        locked = hook_sets.hook_set_declarations(home, source, required=True)
    err = capsys.readouterr().err

    assert locked.origin == hook_sets.HOOK_SET_ORIGIN_UPSTREAM, locked.reasons
    assert _FOREIGN_FLAG_A in _flags(locked)
    assert locked.source_sha256 == _package_digest(source)
    leftover = created[0]
    assert leftover.is_dir() and str(leftover) in err and "정리 실패" in err, err

    following = hook_sets.hook_set_declarations(home, source, required=True)
    elsewhere = hook_sets.hook_set_declarations(other_home, source, required=True)

    assert following.declarations == locked.declarations
    assert following.source_sha256 == locked.source_sha256 == elsewhere.source_sha256
    assert leftover.is_dir(), "다음 조회가 남의 임시 트리를 건드렸다"
    assert not created[1].exists() and not created[2].exists()
    assert list(raw_output.temp_root(other_home.root).iterdir()) == []
    real_rmtree(leftover)
