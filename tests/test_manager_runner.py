"""프로젝트 실행기 — 실 PM 홈 사본을 실제로 불러 판정한다 (T-0910 AT-002).

인터프리터 해소는 엔진 `pm_bootstrap._resolve_python_argv` 와 같은 규칙이고, 옛 세대 사본은
**실물**(`--json` 을 모르는 실제 스크립트 · 파일 부재)로 재현한다. 실패는 예외로 올라오며
stderr 원문과 rc 가 보존된다(빈 dict·None 으로 삼키지 않는다).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import (
    git_required, install_old_generation_engine, install_slow_engine, make_pm_home, pm_config,
    remove_engine_script, set_conf_key,
)

from manager import CallError, runner

REPO = Path(__file__).resolve().parents[1]

pytestmark = git_required


@pytest.fixture(scope="module")
def pm_home(tmp_path_factory) -> Path:
    return make_pm_home(tmp_path_factory.mktemp("runner-home") / "demo")


@pytest.fixture
def copied_home(pm_home, tmp_path) -> Path:
    """손대는 테스트는 실 사본을 복사해서 쓴다(픽스처 홈은 pristine 으로 남는다)."""
    destination = tmp_path / "copy"
    shutil.copytree(pm_home, destination, symlinks=True)
    return destination


# ── 인터프리터 해소 (엔진 규칙과 같음) ────────────────────────────────────────


def test_configured_bare_name_is_one_token(pm_home):
    assert runner.conf_values(pm_home)["runtime.py"] == "python3"
    assert runner.interpreter(pm_home) == ("python3",)


def test_configured_absolute_path_is_kept_verbatim(copied_home):
    set_conf_key(copied_home, "runtime.py", "/opt/venv/bin/python")

    assert runner.interpreter(copied_home) == ("/opt/venv/bin/python",)


@pytest.mark.parametrize(("os_name", "expected"),
                         [("posix", ("python3",)), ("nt", ("py", "-3"))])
def test_empty_value_falls_to_the_os_default(copied_home, os_name, expected):
    """실측상 이 머신 PM 홈 7개 중 4개가 빈 값이다 — 예외를 두면 절반이 조회 불가가 된다."""
    set_conf_key(copied_home, "runtime.py", "")

    assert runner.interpreter(copied_home, os_name=os_name) == expected


def test_missing_conf_file_is_the_os_default_not_an_error(copied_home):
    (copied_home / ".project_manager" / "local.conf").unlink()

    assert runner.conf_values(copied_home) == {}
    assert runner.interpreter(copied_home, os_name="posix") == ("python3",)


def test_a_copy_with_a_retired_key_is_still_callable(copied_home):
    """`load_checked` 였다면 여기서 예외가 난다 — "먼저 갱신" 을 보여 줄 사본을 못 부르게 된다."""
    set_conf_key(copied_home, "delegate_enabled", "true")

    assert runner.interpreter(copied_home) == ("python3",)
    assert runner.conf_values(copied_home)["delegate_enabled"] == "true"


# ── upstream 값 (관리자가 직접 읽는 두 번째이자 마지막 키) ─────────────────────


def test_relative_upstream_resolves_against_the_pm_home(copied_home):
    set_conf_key(copied_home, "upstream.path", "../framework")

    assert runner.upstream(copied_home) == str((copied_home / ".." / "framework").resolve())


def test_url_upstream_is_left_alone(copied_home):
    set_conf_key(copied_home, "upstream.path", "git@github.com:example/project_manager.git")

    assert runner.upstream(copied_home) == "git@github.com:example/project_manager.git"


def test_unset_upstream_is_none(copied_home):
    set_conf_key(copied_home, "upstream.path", "")

    assert runner.upstream(copied_home) is None


# ── 현황 조회 ────────────────────────────────────────────────────────────────


def test_status_returns_the_engine_document(pm_home):
    document = runner.status(pm_home)

    assert document["schema"] == 1
    assert document["pm_home"] == str(pm_home.resolve())
    assert {"slots", "tasks", "git", "repos", "harnesses", "conf"} <= set(document)


def test_the_light_call_does_not_load_the_board(pm_home):
    """불변식 — 가벼운 절은 티켓 파일을 읽지 않는다(프로젝트당 0.5초 이내의 근거)."""
    assert runner.status(pm_home)["board"] is None


def test_the_heavy_call_carries_the_board(pm_home):
    document = runner.status(pm_home, with_board=True)

    assert document["board"] is not None
    assert set(document["board"]["counts"]) >= {"open", "claimed", "done"}


def test_an_engine_that_does_not_know_json_keeps_its_stderr(copied_home):
    install_old_generation_engine(copied_home)

    with pytest.raises(CallError) as caught:
        runner.status(copied_home)

    assert caught.value.kind == "old-generation"
    assert caught.value.rc == 2
    assert "unrecognized arguments: --json" in caught.value.stderr
    assert "먼저 갱신" in caught.value.message


def test_a_copy_without_the_engine_script_reports_the_same_way(copied_home):
    remove_engine_script(copied_home)

    with pytest.raises(CallError) as caught:
        runner.status(copied_home)

    assert caught.value.kind == "old-generation"
    assert caught.value.rc == 2
    assert "pm_config.py" in caught.value.stderr


def test_an_interpreter_that_cannot_be_launched_is_a_spawn_failure(copied_home):
    set_conf_key(copied_home, "runtime.py", str(copied_home / "no-such-python"))

    with pytest.raises(CallError) as caught:
        runner.status(copied_home)

    assert caught.value.kind == "spawn"
    assert "no-such-python" in caught.value.message


def test_a_copy_that_never_answers_hits_the_timeout(copied_home, monkeypatch):
    install_slow_engine(copied_home)
    monkeypatch.setattr(runner, "LIGHT_TIMEOUT_SECONDS", 1)

    with pytest.raises(CallError) as caught:
        runner.status(copied_home)

    assert caught.value.kind == "timeout"


def test_output_that_is_not_json_is_not_read_as_a_status(copied_home):
    """rc 0 인데 JSON 이 아니면 그 사실이 오류다 — 빈 dict 로 바꾸지 않는다."""
    (copied_home / ".project_manager" / "tools" / "pm_config.py").write_text(
        'print("현황: 사람용 텍스트")\n', encoding="utf-8")

    with pytest.raises(CallError) as caught:
        runner.status(copied_home)

    assert caught.value.kind == "not-json"
    assert "사람용 텍스트" in caught.value.stderr


def test_a_relative_pm_home_is_resolved_before_the_call(pm_home, monkeypatch):
    """조회는 그 홈으로 cwd 를 옮겨 부른다 — 상대 경로를 넘기면 자식이 다시 풀어 두 번 붙는다."""
    monkeypatch.chdir(pm_home.parent)

    document = runner.status(Path(pm_home.name))

    assert document["pm_home"] == str(pm_home.resolve())


def test_a_folder_that_is_not_a_pm_home_is_refused_before_spawning(tmp_path):
    with pytest.raises(CallError) as caught:
        runner.status(tmp_path)

    assert caught.value.kind == "invalid-path"


def test_the_manager_writes_nothing_into_the_project_folder(pm_home):
    """불변식 (1) — 조회는 사본 폴더의 직계 구성을 바꾸지 않는다."""
    before = sorted(entry.name for entry in (pm_home / ".project_manager").iterdir())

    runner.status(pm_home)
    runner.status(pm_home, with_board=True)

    assert sorted(entry.name for entry in (pm_home / ".project_manager").iterdir()) == before


def test_task_rows_come_from_the_engine(copied_home):
    pm_config(copied_home, "task", "new", "demo")

    tasks = runner.status(copied_home)["tasks"]

    assert [task["name"] for task in tasks] == ["demo"]
    assert tasks[0]["pid"] == 0 and tasks[0]["alive"] is False


CONCURRENT_PROBE = """
import sys, threading
sys.path.insert(0, sys.argv[1])
from manager import runner
home = sys.argv[2]
failures = []
def resolve():
    try:
        assert runner.interpreter(home) == ("python3",), "interpreter"
        assert runner.conf_values(home)["runtime.py"] == "python3", "conf"
    except BaseException as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
threads = [threading.Thread(target=resolve) for _ in range(8)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=30)
print("failures:", failures)
"""


def test_engine_modules_survive_concurrent_first_loads(pm_home, tmp_path):
    """창은 브리지 호출을 스레드마다 따로 처리한다 — 첫 로드가 겹쳐도 반쯤 채워진 모듈이 새지 않는다.

    새 프로세스로 잰다: 로드도 강등 경고도 프로세스당 한 번이라, 이미 로드된 pytest 안에서는
    이 결함이 재현되지 않는다. 실측(잠금 없이 프로젝트 7개 동시 조회)에서는 `local_conf` 에
    `load` 가 없다는 AttributeError 와 공유 읽기 강등 경고가 함께 났다.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(CONCURRENT_PROBE, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(probe), str(REPO), str(pm_home)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)

    assert completed.returncode == 0, completed.stderr
    assert "failures: []" in completed.stdout, completed.stdout
    assert completed.stderr == "", f"엔진이 강등 경고를 냈다: {completed.stderr}"
