"""JS 브리지 — 화면이 보는 표면과 오류 dict (T-0910 AT-004).

정상은 엔진 JSON 그대로이고(감싸지 않는다), 실패는 `{"error": {kind, message, stderr, rc}}` 다.
합성 클래스 `Api` 의 MRO 에 탭별 mixin 셋이 들어 있는지도 여기서 고정한다(묶음 분할의 계약).
"""
from __future__ import annotations

import inspect
import shutil
from pathlib import Path

import pytest

from _manager_home import (
    git_required, install_old_generation_engine, make_bare_with_tags, make_pm_home, set_conf_key,
)

from manager import CallError, registry
from manager.bridge import Api, BaseApi
from manager.bridge_run import RunApi
from manager.bridge_settings import SettingsApi
from manager.bridge_update import UpdateApi

pytestmark = git_required

BASE_API_METHODS = {
    "list_projects", "add_project", "remove_project", "status", "latest_tags", "pick_folder",
}


@pytest.fixture(scope="module")
def pm_home(tmp_path_factory) -> Path:
    return make_pm_home(tmp_path_factory.mktemp("bridge-home") / "demo")


@pytest.fixture
def picked(tmp_path):
    """폴더 선택 대화 자리 — 창이 주입한다(브리지는 pywebview 를 import 하지 않는다)."""
    return {"value": None}


@pytest.fixture
def api(tmp_path, picked) -> Api:
    return Api(folder_picker=lambda: picked["value"],
               registry_file=tmp_path / "config" / "projects.json")


# ── 묶음 분할의 계약 ─────────────────────────────────────────────────────────


def test_the_composed_api_carries_every_tab_mixin():
    assert Api.__mro__[:5] == (Api, BaseApi, SettingsApi, UpdateApi, RunApi)


def test_the_skeleton_keeps_exactly_six_methods():
    public = {name for name, _ in inspect.getmembers(BaseApi, inspect.isfunction)
              if not name.startswith("_")}

    assert public == BASE_API_METHODS


def test_the_bridge_does_not_import_the_window_toolkit():
    """뒷단은 창 없이 돈다 — pywebview import 는 `window.py` 하나뿐이다."""
    source = (Path(__file__).resolve().parents[1] / "manager" / "bridge.py").read_text(
        encoding="utf-8")

    assert "import webview" not in source


# ── 등록부 표면 ──────────────────────────────────────────────────────────────


def test_list_add_remove_round_trip(api, pm_home):
    assert api.list_projects() == {"projects": []}

    added = api.add_project(str(pm_home))
    assert added == {"projects": [{"path": str(pm_home.resolve()), "name": pm_home.name}]}
    assert api.list_projects() == added

    assert api.remove_project(str(pm_home)) == {"projects": []}


def test_a_refused_registration_comes_back_as_an_error_dict(api, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    response = api.add_project(str(plain))

    assert set(response) == {"error"}
    assert set(response["error"]) == {"kind", "message", "stderr", "rc"}
    assert response["error"]["kind"] == "invalid-path"


def test_folder_choice_and_cancel(api, picked, pm_home):
    picked["value"] = str(pm_home)
    assert api.pick_folder() == {"path": str(pm_home)}

    picked["value"] = None
    assert api.pick_folder() == {"path": None}


# ── 현황 표면 ────────────────────────────────────────────────────────────────


def test_status_is_the_engine_document_unwrapped(api, pm_home):
    response = api.status(str(pm_home))

    assert response["schema"] == 1
    assert "error" not in response
    assert "ok" not in response and "data" not in response


def test_status_with_board_carries_the_heavy_section(api, pm_home):
    response = api.status(str(pm_home), True, False)

    assert response["board"] is not None


def test_an_old_generation_copy_keeps_its_stderr_in_the_error_dict(api, pm_home, tmp_path):
    copy = tmp_path / "old"
    shutil.copytree(pm_home, copy, symlinks=True)
    install_old_generation_engine(copy)

    response = api.status(str(copy))

    assert response["error"]["kind"] == "old-generation"
    assert response["error"]["rc"] == 2
    assert "unrecognized arguments: --json" in response["error"]["stderr"]


def test_failures_are_never_answered_with_none_or_an_empty_value(api, tmp_path):
    response = api.status(str(tmp_path / "nowhere"))

    assert response is not None and response != {}
    assert response["error"]["message"]


# ── 최신 태그 표면 ───────────────────────────────────────────────────────────


def test_latest_tags_maps_projects_to_upstreams(api, pm_home, tmp_path):
    bare = make_bare_with_tags(tmp_path / "remote")
    second = tmp_path / "second"
    shutil.copytree(pm_home, second, symlinks=True)
    for home in (pm_home, second):
        set_conf_key(home, "upstream.path", str(bare))
        api.add_project(str(home))

    response = api.latest_tags()

    assert response["projects"][str(pm_home.resolve())] == {"upstream": str(bare)}
    assert response["projects"][str(second.resolve())] == {"upstream": str(bare)}
    assert response["upstreams"] == {str(bare): {"tag": "v2.0.0"}}


def test_a_project_without_upstream_has_no_tag_axis(api, pm_home, tmp_path):
    without = tmp_path / "no-upstream"
    shutil.copytree(pm_home, without, symlinks=True)
    set_conf_key(without, "upstream.path", "")
    api.add_project(str(without))

    response = api.latest_tags()

    assert response["projects"][str(without.resolve())] == {"upstream": None}
    assert response["upstreams"] == {}


def test_an_unreadable_registry_surfaces_as_an_error_dict(api, tmp_path):
    path = tmp_path / "config" / "projects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    assert api.list_projects()["error"]["kind"] == "invalid-path"
    assert api.latest_tags()["error"]["kind"] == "invalid-path"


# ── 오류 종류의 계약 ─────────────────────────────────────────────────────────


def test_the_error_kinds_are_a_closed_set():
    assert CallError.KINDS == (
        "old-generation", "spawn", "timeout", "not-json", "invalid-path")

    with pytest.raises(ValueError):
        CallError("something-else", "새 종류를 브리지 밖에서 만들 수 없다")
