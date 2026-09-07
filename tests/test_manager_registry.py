"""프로젝트 등록부 — 실 파일·실 `HOME`/`APPDATA` 로 판정한다 (T-0910 AT-001).

담는 값은 PM 홈 절대경로 목록 하나이고, 쓰기는 같은 디렉터리 tmp → `os.replace` 원자 교체다.
비-PM홈은 등록 자체가 거부된다(`.project_manager/` 표식이 없으면 사본을 부를 수 없다).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _manager_home import git_required, make_pm_home

from manager import CallError, registry

pytestmark = git_required


@pytest.fixture(scope="module")
def pm_home(tmp_path_factory) -> Path:
    return make_pm_home(tmp_path_factory.mktemp("registry-home") / "demo")


@pytest.fixture
def registry_file(tmp_path) -> Path:
    return tmp_path / "config" / "projects.json"


def test_default_location_follows_the_user_config_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    posix = registry.registry_path(os_name="posix")
    windows = registry.registry_path(os_name="nt")

    assert posix == tmp_path / "home" / ".config" / "project_manager" / "projects.json"
    assert windows == tmp_path / "appdata" / "project_manager" / "projects.json"


def test_windows_without_appdata_stops_instead_of_guessing(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)

    with pytest.raises(CallError) as caught:
        registry.registry_path(os_name="nt")

    assert caught.value.kind == "invalid-path"


def test_missing_file_is_an_empty_registry(registry_file):
    assert registry.load(registry_file) == []
    assert not registry_file.exists()


def test_add_writes_the_absolute_path_and_duplicates_are_a_no_op(pm_home, registry_file):
    first = registry.add(pm_home, registry_file)
    written = registry_file.read_bytes()

    second = registry.add(pm_home, registry_file)

    assert first == [pm_home.resolve()] == second
    assert json.loads(registry_file.read_text(encoding="utf-8")) == [str(pm_home.resolve())]
    assert registry_file.read_bytes() == written, "중복 등록이 파일을 다시 썼다"
    assert registry.load(registry_file) == [pm_home.resolve()]


def test_non_pm_home_is_refused_and_nothing_is_written(tmp_path, registry_file):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(CallError) as caught:
        registry.add(plain, registry_file)

    assert caught.value.kind == "invalid-path"
    assert ".project_manager" in caught.value.message
    assert not registry_file.exists()


def test_remove_drops_the_entry_and_leaves_the_project_folder_alone(pm_home, registry_file):
    registry.add(pm_home, registry_file)
    before = sorted(entry.name for entry in pm_home.iterdir())

    remaining = registry.remove(pm_home, registry_file)

    assert remaining == []
    assert registry.load(registry_file) == []
    assert sorted(entry.name for entry in pm_home.iterdir()) == before
    assert (pm_home / ".project_manager").is_dir()


def test_removing_an_unregistered_path_does_not_rewrite_the_file(pm_home, registry_file):
    registry.add(pm_home, registry_file)
    written = registry_file.read_bytes()

    registry.remove(pm_home.parent / "never-registered", registry_file)

    assert registry_file.read_bytes() == written


def test_write_is_an_atomic_replace(pm_home, registry_file, monkeypatch):
    """교체가 실패하면 옛 등록부가 그대로 남는다 — 자리에서 덮어쓰는 구현이면 red."""
    registry.add(pm_home, registry_file)
    survivor = registry_file.read_bytes()

    def refuse(*_args, **_kwargs):
        raise OSError("replace refused")

    monkeypatch.setattr(registry.os, "replace", refuse)
    with pytest.raises(OSError):
        registry.remove(pm_home, registry_file)

    assert registry_file.read_bytes() == survivor
    assert [entry.name for entry in registry_file.parent.iterdir()] == [registry_file.name], (
        "교체 실패 뒤 tmp 파일이 남았다")


@pytest.mark.parametrize("payload", ['{"projects": []}', "[1, 2]", "not json at all"])
def test_a_registry_that_is_not_a_path_list_stops_loudly(registry_file, payload):
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    registry_file.write_text(payload, encoding="utf-8")

    with pytest.raises(CallError) as caught:
        registry.load(registry_file)

    assert caught.value.kind == "invalid-path"


def test_registry_holds_paths_only(pm_home, registry_file):
    """캐시·버전·현황을 넣지 않는다 — 현황은 매번 사본에서 읽는다([[ADR-0099]])."""
    registry.add(pm_home, registry_file)

    entries = json.loads(registry_file.read_text(encoding="utf-8"))

    assert entries == [str(pm_home.resolve())]
    assert all(isinstance(entry, str) for entry in entries)


def test_override_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    override = tmp_path / "elsewhere" / "projects.json"

    assert registry.registry_path(override) == override.resolve()
    assert os.environ["HOME"] == str(tmp_path / "home")
