"""어댑터 파일 절과 전체 보기 매트릭스 (T-0911 AT-003).

관리자는 어댑터 파일을 **보고 수용만** 한다 — 판정은 그 사본의 `status --json --with-adapters`
가 내고, 수용은 그 사본의 `sync-adapter-config --accept` 가 한다. 관리자가 파일을 복사하거나
백업하지 않는다는 사실을 백업·원장 같은 **엔진만 남기는 흔적**으로 잰다.

소스는 경로 upstream(이 트리를 커밋 하나로 담은 임시 저장소)으로 해소된다 — 네트워크 0. 이 트리를
그대로 쓰지 않는 이유: 설치는 작업 트리를 깔고 HEAD 를 설치 기준으로 적으므로, 커밋 전 템플릿
변경이 있으면 손대지 않은 파일에도 기준 차이가 보인다.

축 넷:
  1. `files[]` 의 모양이 둘이다(채널 있는 파일 10필드 / 인스턴스 소유 3필드·키 부재).
  2. 편집이 상태·기능 표·거리·차이 둘에 그대로 나타난다.
  3. 수용은 사본 명령이고, 그 결과는 재조회로 확인된다(관리자의 복사 아님).
  4. 매트릭스는 등록 프로젝트 전부를 돌되 프로젝트별로 독립 실패한다.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from _manager_home import (
    git_required, install_old_generation_engine, make_committed_source, make_pm_home,
    tree_snapshot,
)

from manager.bridge import Api

SETTINGS = ".claude/settings.json"
# 채널 있는 어댑터 config 행이 드는 필드 전부(T-0907 계약) — 화면이 읽는 표면이다.
CHANNEL_FIELDS = {"path", "harness", "mode", "status", "template_rev", "diff_lines",
                  "diff_vs_baseline", "diff_vs_latest", "features", "distance"}
# 인스턴스 소유 문서 행 — `mode`·`features` 는 null 이 아니라 **키가 없다**.
INSTANCE_FIELDS = {"path", "ownership", "status"}

pytestmark = git_required


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    """설치 소스이자 경로 upstream — 설치 기준 커밋이 설치한 내용과 같다."""
    return make_committed_source(tmp_path_factory.mktemp("adapters-source"))


@pytest.fixture(scope="module")
def base_home(tmp_path_factory, source) -> Path:
    return make_pm_home(tmp_path_factory.mktemp("adapters-base") / "demo", source=source,
                        upstream=source)


@pytest.fixture
def pm_home(base_home, tmp_path) -> Path:
    home = tmp_path / "demo"
    shutil.copytree(base_home, home, symlinks=True)
    from _manager_home import reanchor_ctx_record
    reanchor_ctx_record(home,base_home)
    return home


@pytest.fixture
def api(tmp_path) -> Api:
    return Api(folder_picker=lambda: None, registry_file=tmp_path / "config" / "projects.json")


def _row(section: dict, path: str) -> dict:
    return next(row for row in section["files"] if row["path"] == path)


def _drop_conf_key(pm_home: Path, key: str) -> None:
    """그 사본의 `local.conf` 에서 키 한 줄을 지운다 — 소스 기준 누락을 실제로 만든다."""
    path = pm_home / ".project_manager" / "local.conf"
    kept = [line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith(f"{key}=")]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _shrink_allow_list(pm_home: Path, keep: int = 5) -> None:
    """채택자 편집을 실제로 만든다 — 허용 목록을 줄여 `permissions.allow` 를 다르게 한다."""
    path = pm_home / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["permissions"]["allow"] = settings["permissions"]["allow"][:keep]
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


# ── ① 두 모양 ───────────────────────────────────────────────────────────────


def test_the_section_carries_the_source_and_both_file_shapes(api, pm_home, source):
    section = api.adapters(str(pm_home))

    assert section["source"] == str(source)        # 소스는 절 머리에 있다(파일 안이 아니다)
    channel = _row(section, SETTINGS)
    assert set(channel) == CHANNEL_FIELDS
    assert channel["harness"] == "claude"
    assert channel["mode"] in {"managed", "report"}
    document = _row(section, "CLAUDE.md")
    assert set(document) == INSTANCE_FIELDS
    assert document == {"path": "CLAUDE.md", "ownership": "instance", "status": "unchecked"}
    # 화면은 `"mode" in row` 로 가른다 — 없는 키를 무조건 읽으면 그 자리에서 터진다.
    assert "mode" not in document and "features" not in document and "distance" not in document


def test_the_section_carries_the_conf_verdict_of_the_same_call(api, pm_home):
    """다섯 번째 목록(`missing_for_latest`)이 실리는 조회는 이것 하나다.

    엔진은 소스가 해소된 호출(`--with-adapters`·`--from`)에서만 그 목록을 채운다. 화면이 늘
    보관하는 가벼운/무거운 절은 소스를 해소하지 않아 null(모름)이라, 이 응답이 그 값을 화면에
    나르는 유일한 자리다(조회는 늘리지 않는다).
    """
    section = api.adapters(str(pm_home))

    conf = section["conf"]
    assert set(conf) == {"missing_required", "legacy", "unknown", "nonstandard",
                         "missing_for_latest", "ctx_budget", "ctx_applied"}
    assert conf["missing_for_latest"] == []                   # 없음 — 모름(null)과 다른 값이다
    light = api.status(str(pm_home))["conf"]
    assert light["missing_for_latest"] is None                # 같은 사본의 가벼운 절은 모름
    # 나머지 네 목록은 같은 사본의 같은 판정이다 — 어댑터 절이 판정을 다시 계산하지 않는다.
    assert {key: value for key, value in conf.items() if key != "missing_for_latest"} \
        == {key: value for key, value in light.items() if key != "missing_for_latest"}

    _drop_conf_key(pm_home, "test.cmd")

    assert api.adapters(str(pm_home))["conf"]["missing_for_latest"] == ["test.cmd"]
    assert api.status(str(pm_home))["conf"]["missing_for_latest"] is None


def test_looking_at_the_adapter_section_writes_nothing(api, pm_home):
    api.adapters(str(pm_home))                     # 공유 읽기 seam 을 먼저 세운다
    before = tree_snapshot(pm_home)

    api.adapters(str(pm_home))

    assert tree_snapshot(pm_home) == before


# ── ② 편집 → 상태·기능 표·거리·차이 둘 ──────────────────────────────────────


def test_an_edit_shows_up_as_status_distance_and_two_diffs(api, pm_home):
    _shrink_allow_list(pm_home)

    row = _row(api.adapters(str(pm_home)), SETTINGS)

    assert row["status"] == "edited"
    assert row["diff_lines"]["removed"] > 0
    verdicts = {feature["name"]: feature["verdict"] for feature in row["features"]}
    assert verdicts["permissions.allow"] == "differs"
    assert row["distance"] == sum(1 for feature in row["features"]
                                  if feature["verdict"] != "same")
    assert row["distance"] >= 1
    # 차이 둘 — 설치 기준↔현재, 현재↔최신. 방향이 반대라 머리글이 서로 다르다.
    assert row["diff_vs_baseline"].startswith(f"--- {SETTINGS}@")
    assert row["diff_vs_latest"].startswith(f"--- {SETTINGS}\n+++ {SETTINGS}@latest")


def test_an_untouched_file_is_in_sync_with_no_diff(api, pm_home):
    row = _row(api.adapters(str(pm_home)), SETTINGS)

    assert row["status"] == "in-sync"
    assert row["distance"] == 0
    assert row["diff_vs_baseline"] == "" and row["diff_vs_latest"] == ""


# ── ③ 수용 = 사본 명령 ──────────────────────────────────────────────────────


def test_accept_runs_the_copys_command_and_the_requery_confirms_it(api, pm_home, monkeypatch):
    import manager.bridge_settings as bridge
    from test_manager_settings import _KNOWN_CTX_CHILD
    original = bridge._run
    def run(home, argv, **kwargs):
        if "--accept" in argv:
            argv = [argv[0], "-c", _KNOWN_CTX_CHILD, argv[1], *argv[2:]]
        return original(home, argv, **kwargs)
    monkeypatch.setattr(bridge, "_run", run)
    _shrink_allow_list(pm_home)
    assert _row(api.adapters(str(pm_home)), SETTINGS)["status"] == "edited"

    response = api.adapter_accept(str(pm_home), SETTINGS)

    assert response["rc"] == 0, response["stderr"]
    assert "어댑터 config 수용" in response["stdout"]
    # 백업과 원장은 **엔진만** 남긴다 — 관리자가 파일을 복사했다면 이 흔적이 없다.
    backup = re.search(r"\(백업 (.+?)\)", response["stdout"])
    assert backup is not None, response["stdout"]
    assert (pm_home / backup.group(1)).is_file(), "엔진이 말한 백업이 그 자리에 없다"
    assert (pm_home / ".project_manager" / "adapter_baseline.json").is_file()

    row = _row(api.adapters(str(pm_home)), SETTINGS)
    assert row["status"] == "in-sync" and row["distance"] == 0


def test_accepting_a_path_the_copy_does_not_know_is_refused_word_for_word(api, pm_home):
    before = (pm_home / ".claude" / "settings.json").read_bytes()

    response = api.adapter_accept(str(pm_home), ".claude/nope.json")

    assert response["rc"] == 1
    assert "오류:" in response["stderr"] or "[중단]" in response["stderr"]
    assert (pm_home / ".claude" / "settings.json").read_bytes() == before


def test_an_unreachable_project_answers_with_an_error_dict(api, tmp_path):
    assert api.adapters(str(tmp_path / "nowhere"))["error"]["kind"] == "invalid-path"
    assert api.adapter_accept(str(tmp_path / "nowhere"), SETTINGS)["error"]["kind"] \
        == "invalid-path"


# ── ④ 전체 보기 매트릭스 ────────────────────────────────────────────────────


def test_the_matrix_covers_every_registered_project(api, pm_home, tmp_path):
    second = tmp_path / "second"
    shutil.copytree(pm_home, second, symlinks=True)
    _shrink_allow_list(second)
    api.add_project(str(pm_home))
    api.add_project(str(second))

    matrix = api.adapter_matrix()

    assert set(matrix) == {str(pm_home.resolve()), str(second.resolve())}
    assert _row(matrix[str(pm_home.resolve())], SETTINGS)["distance"] == 0
    assert _row(matrix[str(second.resolve())], SETTINGS)["distance"] >= 1


def test_one_broken_project_does_not_take_the_matrix_down(api, pm_home, tmp_path):
    broken = tmp_path / "broken"
    shutil.copytree(pm_home, broken, symlinks=True)
    install_old_generation_engine(broken)
    api.add_project(str(pm_home))
    api.add_project(str(broken))

    matrix = api.adapter_matrix()

    assert "files" in matrix[str(pm_home.resolve())]
    assert matrix[str(broken.resolve())]["error"]["kind"] == "old-generation"


def test_an_empty_registry_gives_an_empty_matrix(api):
    assert api.adapter_matrix() == {}
