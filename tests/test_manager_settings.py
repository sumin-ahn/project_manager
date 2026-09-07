"""설정 탭 브리지 — 키 목록·현재값·저장·[기본값으로]·upstream (T-0911 AT-002).

전부 **실물**로 잰다: 실 `pm_import --new` PM 홈, 실 `local.conf`, 실 엔진 writer, 실 로컬 bare.
가짜 runner 나 수기 dict 로 판정을 지어내지 않는다 — 이 파일이 지키는 것이 "관리자가 프로젝트
폴더에 무엇을 하는가" 라서, 그 답은 디스크에서만 나온다.

축 다섯:
  1. 항목 목록의 정본이 **그 사본의** 레지스트리다(사본을 옛 세대로 갈면 그 사실이 드러난다).
  2. 저장은 엔진의 유일한 conf writer 를 통과한다 — 공용 락 파일이 서고, 다른 프로세스의
     writer 와 같은 락에 직렬화되며, 그 밖의 파일은 한 개도 바뀌지 않는다.
  3. 저장 뒤 화면 값은 **재조회 결과**다(낙관적 갱신 없음) — 판정은 그 사본이 낸다.
  4. [기본값으로] 는 레지스트리 기본값을 쓰고, 기본값이 없는 키에는 쓸 값이 없다.
  5. upstream 전환은 도달성 검증을 포함한 사본 명령이고, 거부 문구는 원문 그대로 온다.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from _manager_home import (
    conf_path, git_required, install_old_generation_key_registry, make_bare_with_tags,
    make_pm_home, set_conf_key, tree_snapshot,
)

from manager import runner
from manager.bridge import Api

REPO = Path(__file__).resolve().parents[1]

pytestmark = git_required

# 다른 프로세스가 conf 락을 쥐고 있는 창(초)과, 그 안에서 저장이 막혔다고 볼 하한.
LOCK_HOLD_SECONDS = 2.0
BLOCKED_FLOOR_SECONDS = 1.0
# 저장 스레드가 남의 락 대기에 들어가기까지 주는 창 — 이 뒤의 조회가 "대기 중" 의 조회다.
SAVE_ENTERS_THE_WAIT_SECONDS = 0.3

# 락을 실제로 쥐는 **다른 프로세스** — 엔진 자신의 writer 로 결정 하나를 쓰고 창을 연 채 기다린다.
LOCK_HOLDER = '''
import importlib.util, sys, time
from pathlib import Path

tools, conf, ready, hold = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4])
spec = importlib.util.spec_from_file_location("pm_import_holder", Path(tools) / "pm_import.py")
pm_import = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm_import)
with pm_import._local_conf_write_lock(conf):
    pm_import._write_conf_keys_locked(conf, {"identity.user": "holder@example.invalid"})
    ready.write_text("held", encoding="utf-8")
    time.sleep(hold)
'''


@pytest.fixture(scope="module")
def base_home(tmp_path_factory) -> Path:
    """실 엔진이 만든 PM 홈 한 벌 — 테스트마다 복사해 쓴다(원본은 읽기만)."""
    return make_pm_home(tmp_path_factory.mktemp("settings-base") / "demo", upstream=REPO)


@pytest.fixture
def pm_home(base_home, tmp_path) -> Path:
    home = tmp_path / "demo"
    shutil.copytree(base_home, home, symlinks=True)
    return home


@pytest.fixture
def api(tmp_path) -> Api:
    return Api(folder_picker=lambda: None, registry_file=tmp_path / "config" / "projects.json")


def _values(pm_home: Path) -> dict[str, str]:
    """디스크의 conf 를 직접 읽는다 — 브리지의 답과 대조할 두 번째 눈."""
    values: dict[str, str] = {}
    for line in conf_path(pm_home).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


# ── ① 항목 목록의 정본 = 그 사본의 레지스트리 ────────────────────────────────


def test_conf_keys_comes_from_the_project_copy_registry(api, pm_home):
    document = api.conf_keys(str(pm_home))

    assert document["schema"] == 1
    assert document["groups"][0] == "project" and "harness" in document["groups"]
    kinds = {row["kind"] for row in document["keys"]}
    assert kinds == {"key", "pattern"}
    named = [row for row in document["keys"] if row["kind"] == "key"]
    patterns = [row for row in document["keys"] if row["kind"] == "pattern"]
    assert {"runtime.py", "test.cmd"} <= {row["key"] for row in named}
    assert all(row["members"] is None for row in named)
    assert all(isinstance(row["members"], list) for row in patterns)
    # 패턴 멤버는 전개된 실명이고 각자 자기 기본값을 든다(표의 합성 표기가 아니다).
    diff_cap = next(row for row in patterns if row["key"].startswith("diff_cap."))
    assert {member["key"]: member["default"] for member in diff_cap["members"]} == {
        "diff_cap.small": "300", "diff_cap.medium": "1000", "diff_cap.large": "2500"}


def test_an_old_generation_copy_cannot_answer_the_key_list(api, pm_home):
    """번들 레지스트리로 대신 그리지 않는다 — 사본이 모르면 "먼저 갱신" 이 화면에 간다."""
    install_old_generation_key_registry(pm_home)

    response = api.conf_keys(str(pm_home))

    assert response["error"]["kind"] == "old-generation"
    assert response["error"]["rc"] == 2
    assert "--keys" in response["error"]["stderr"]


# ── ② 현재값과 설정 완전성 다섯 목록 ─────────────────────────────────────────


def test_conf_values_carries_the_file_values_and_the_five_lists(api, pm_home):
    response = api.conf_values(str(pm_home))

    assert response["values"] == _values(pm_home)
    assert set(response["conf"]) == {
        "missing_required", "legacy", "unknown", "nonstandard", "missing_for_latest"}
    # 가벼운 절은 소스를 해소하지 않는다 — "갱신하면 필요" 는 없음([])이 아니라 모름(null)이다.
    assert response["conf"]["missing_for_latest"] is None


def test_the_five_lists_are_the_copys_verdict_not_a_manager_calculation(api, pm_home):
    """필수 누락·폐지·미지·표준과 다름을 한 conf 에 한꺼번에 심고 그대로 받는지 본다."""
    text = conf_path(pm_home).read_text(encoding="utf-8")
    conf_path(pm_home).write_text(
        text.replace("test.cmd=", "#test.cmd=").replace("ctx.nudge_pct=30", "ctx.nudge_pct=25")
        + "delegate_enabled=true\nnope.nope=1\n", encoding="utf-8")

    verdict = api.conf_values(str(pm_home))["conf"]

    assert verdict["missing_required"] == ["test.cmd"]
    assert verdict["legacy"] == [{"key": "delegate_enabled", "replacement": "delegate.enabled"}]
    assert verdict["unknown"] == ["nope.nope"]      # 폐지 키는 미지가 아니라 폐지로 갈린다
    assert {"key": "ctx.nudge_pct", "value": "25", "default": "30"} in verdict["nonstandard"]


def test_missing_for_latest_is_a_list_once_the_source_is_resolved(api, pm_home):
    """소스가 해소된 호출(어댑터 절)에서는 같은 축이 배열로 온다 — 모름과 없음이 갈린다."""
    assert "files" in api.adapters(str(pm_home))          # 소스 해소가 되는 형상인지 먼저 확인

    heavy = api.status(str(pm_home), False, True)

    assert heavy["conf"]["missing_for_latest"] == []


# ── ③ 저장 — 엔진 writer 하나 · 그 밖의 파일 0 · 직후 재조회 ─────────────────


def test_conf_save_writes_through_the_engine_writer_and_its_shared_lock(api, pm_home):
    lock = pm_home / ".project_manager" / ".local" / "local-conf.lock"
    lock.unlink()      # import 가 세워 둔 것을 치우고, 저장이 같은 경로를 다시 여는지 본다

    response = api.conf_save(str(pm_home), {"ctx.nudge_pct": "25"})

    assert response["changed"] is True
    assert _values(pm_home)["ctx.nudge_pct"] == "25"
    assert lock.is_file(), "공용 락 파일이 없다 — 엔진 writer 를 우회해 직접 썼다"


def test_a_save_touches_no_other_file_in_the_project(api, pm_home):
    """관리자가 직접 쓰는 프로젝트 파일은 `local.conf` 하나다(그 밖의 쓰기는 사본 명령)."""
    api.conf_save(str(pm_home), {"ctx.stop_pct": "19"})       # 락 파일까지 세운 뒤가 기준선
    before = tree_snapshot(pm_home)

    api.conf_save(str(pm_home), {"ctx.nudge_pct": "25"})

    after = tree_snapshot(pm_home)
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    assert changed == {".project_manager/local.conf"}


def test_read_only_calls_write_nothing(api, pm_home):
    api.conf_values(str(pm_home))                             # 공유 읽기 seam 을 먼저 세운다
    before = tree_snapshot(pm_home)

    api.conf_keys(str(pm_home))
    api.conf_values(str(pm_home))
    api.upstream_show(str(pm_home))

    assert tree_snapshot(pm_home) == before


def test_a_save_keeps_comments_and_keys_it_was_not_asked_to_change(api, pm_home):
    before = conf_path(pm_home).read_text(encoding="utf-8")
    set_conf_key(pm_home, "nope.nope", "1")

    api.conf_save(str(pm_home), {"ctx.nudge_pct": "25"})

    after = conf_path(pm_home).read_text(encoding="utf-8")
    assert after.splitlines()[0] == before.splitlines()[0]     # 머리 주석 그대로
    assert _values(pm_home)["nope.nope"] == "1"                # 모르는 키도 지우지 않는다
    assert _values(pm_home)["test.cmd"] == "python3 -m pytest tests/ -q -n auto"


def test_the_screen_value_after_a_save_is_the_requery_not_the_request(api, pm_home):
    """저장이 돌려주는 판정은 그 사본이 다시 낸 것이다 — 요청값을 그대로 비추지 않는다.

    관리자가 낙관적으로 갱신하면 "레지스트리 밖 키" 같은 판정이 화면에 뜨지 않는다. 사본만이
    아는 판정(`unknown`)을 저장으로 만들고, 그 판정이 저장 응답에 실려 오는지로 잰다.
    """
    response = api.conf_save(str(pm_home), {"test.windows.cmd": "py -3 -m pytest -q"})

    assert response["conf"]["unknown"] == ["test.windows.cmd"]  # qa.platforms 미선언 → 미지 키
    assert response["conf"] == api.conf_values(str(pm_home))["conf"]


def test_saving_an_unchanged_value_reports_no_change(api, pm_home):
    assert api.conf_save(str(pm_home), {"ctx.nudge_pct": "30"})["changed"] is False


def test_a_concurrent_writer_in_another_process_serializes_on_the_same_lock(api, pm_home,
                                                                           tmp_path):
    """다른 프로세스의 엔진 writer 가 락을 쥐면 저장이 기다리고, 두 결정이 모두 남는다.

    기다리는 동안에도 다른 조회는 돌아야 한다 — 그 대기는 관리자 안의 공용 잠금(`ENGINE_LOCK`)
    **밖**이다. 안에서 기다리면 같은 잠금을 쓰는 `runner.conf_values`·`interpreter` 가 함께
    막혀 등록 프로젝트 전부의 주기 조회가 선다(창은 호출을 스레드마다 따로 처리한다).
    """
    script = tmp_path / "holder.py"
    script.write_text(LOCK_HOLDER, encoding="utf-8")
    ready = tmp_path / "held.flag"
    holder = subprocess.Popen(
        [sys.executable, str(script), str(pm_home / ".project_manager" / "tools"),
         str(conf_path(pm_home)), str(ready), str(LOCK_HOLD_SECONDS)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace")
    saved: list[dict] = []
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            if holder.poll() is not None:
                pytest.fail(f"락 보유 프로세스가 먼저 죽었다: {holder.communicate()[1]}")
            time.sleep(0.02)
        assert ready.exists(), "락 보유 프로세스가 30초 안에 락을 잡지 못했다"

        started = time.monotonic()
        saver = threading.Thread(
            target=lambda: saved.append(api.conf_save(str(pm_home), {"ctx.nudge_pct": "25"})))
        saver.start()
        time.sleep(SAVE_ENTERS_THE_WAIT_SECONDS)
        assert saver.is_alive(), "저장이 이미 끝났다 — 대기 중을 재지 못했다"

        polled = time.monotonic()
        during = runner.conf_values(pm_home)
        queried = time.monotonic() - polled

        saver.join(timeout=30)
        waited = time.monotonic() - started
    finally:
        holder.wait(timeout=30)

    assert queried < BLOCKED_FLOOR_SECONDS, f"대기 중 조회가 {queried:.2f}초 막혔다"
    assert during["identity.user"] == "holder@example.invalid"  # 그 조회는 실제로 값을 읽었다
    assert waited >= BLOCKED_FLOOR_SECONDS, f"저장이 남의 락을 기다리지 않았다 ({waited:.2f}초)"
    assert saved[0]["changed"] is True
    values = _values(pm_home)
    assert values["ctx.nudge_pct"] == "25"                     # 이쪽 결정
    assert values["identity.user"] == "holder@example.invalid"  # 저쪽 결정도 살아 있다


def test_a_save_that_cannot_reach_the_project_is_an_error_dict(api, tmp_path):
    response = api.conf_save(str(tmp_path / "nowhere"), {"ctx.nudge_pct": "25"})

    assert response["error"]["kind"] == "invalid-path"


# ── ④ [기본값으로] ───────────────────────────────────────────────────────────


def test_conf_reset_writes_the_registry_default(api, pm_home):
    api.conf_save(str(pm_home), {"ctx.nudge_pct": "25"})

    response = api.conf_reset(str(pm_home), "ctx.nudge_pct")

    assert _values(pm_home)["ctx.nudge_pct"] == "30"
    assert all(row["key"] != "ctx.nudge_pct" for row in response["conf"]["nonstandard"])


def test_a_key_without_a_declared_default_has_nothing_to_reset_to(api, pm_home):
    """기본값이 없는 키는 화면에 버튼이 없고, 브리지도 빈값을 쓰지 않는다(실패는 실패다)."""
    before = conf_path(pm_home).read_bytes()

    with pytest.raises(ValueError):
        api.conf_reset(str(pm_home), "project.name")

    assert conf_path(pm_home).read_bytes() == before


def test_upstream_path_is_not_written_through_the_plain_save_path(api, pm_home):
    """이 키는 값 자체가 도달성 계약이라 검증을 건너뛰는 경로가 없다."""
    before = conf_path(pm_home).read_bytes()

    with pytest.raises(ValueError):
        api.conf_save(str(pm_home), {"upstream.path": str(pm_home)})

    assert conf_path(pm_home).read_bytes() == before


# ── ⑤ upstream show / set ────────────────────────────────────────────────────


def test_upstream_show_is_the_copys_own_line(api, pm_home):
    response = api.upstream_show(str(pm_home))

    assert response["rc"] == 0
    assert response["stdout"].startswith("upstream: ")
    assert str(REPO) in response["stdout"] and "(path)" in response["stdout"]


def test_upstream_show_says_unregistered_in_the_engines_own_words(api, pm_home):
    set_conf_key(pm_home, "upstream.path", "")

    response = api.upstream_show(str(pm_home))

    assert response["rc"] == 0
    assert response["stdout"].strip() == (
        "upstream: (미등록) — `pm-config upstream set <url|path>` 로 설정하라.")


def test_upstream_set_records_a_reachable_local_repository(api, pm_home, tmp_path):
    bare = make_bare_with_tags(tmp_path / "remote")

    response = api.upstream_set(str(pm_home), str(bare))

    assert response["rc"] == 0, response["stderr"]
    assert _values(pm_home)["upstream.path"] == str(bare)


def test_an_unreachable_value_comes_back_refused_word_for_word(api, pm_home, tmp_path):
    missing = tmp_path / "no-such-checkout"
    before = _values(pm_home)["upstream.path"]

    response = api.upstream_set(str(pm_home), str(missing))

    assert response["rc"] == 1
    assert response["stderr"].strip() == (
        "[중단] upstream 값 거부 (기록 안 함) — 경로 upstream 이 디렉토리가 아니거나 "
        f"존재하지 않음: {missing}")
    assert _values(pm_home)["upstream.path"] == before
