"""브리지 `run` 의 허용 목록 — 무엇을 부를 수 있고 무엇이 서브프로세스 전에 거절되나 (T-0913 AT-002).

축 넷:
  1. 허용 목록은 **선행 토큰 튜플**의 집합이다. `argv[0]` 만 보면 `repo`·`worktree`·`task` 가
     통째로 열려 `repo remove` 같은 형제와 앞으로 생길 하위명령이 자동으로 들어온다.
  2. 목록 밖과 `--force` 는 **서브프로세스를 띄우기 전에** 거절된다 — 스폰 자리에 세운 관찰자가
     한 번도 불리지 않고, 프로젝트 트리도 한 파일 바뀌지 않는다.
  3. 거부 형태는 화면이 이미 아는 모양 하나다(`{"error": {kind, message, stderr, rc}}`).
  4. `--user-ack` 을 브리지가 만들지 않는다 — 붙이지 않고 부르면 엔진이 그대로 거부한다.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from _manager_home import git_required, make_bare_with_tags, make_pm_home, tree_snapshot
from _repo_owned_inventory import OWNED, repo_owned_paths

from manager import CallError, runner
from manager.bridge import Api
from manager.bridge_run import (
    ALLOWED_COMMANDS, FORBIDDEN_FLAG, REJECT_RC, forbidden_flag, matched_command,
)

REPO = Path(__file__).resolve().parents[1]
REPO_NAME = "app"

# 화면이 실제로 만드는 argv 14개(구성 탭·행 동작이 내는 것과 같은 형식).
SCREEN_ARGV: dict[tuple[str, ...], list[str]] = {
    ("status",): ["status", "--json"],
    ("repo", "list"): ["repo", "list"],
    ("repo", "add"): ["repo", "add", REPO_NAME, "--git", "/tmp/app.git", "--protected", "main"],
    ("repo", "protected"): ["repo", "protected", REPO_NAME, "main,release"],
    ("worktree", "add"): ["worktree", "add", REPO_NAME, "--readonly", "--user-ack", REPO_NAME],
    ("worktree", "remove"): ["worktree", "remove", "work/app_1"],
    ("worktree", "prune-stale"): ["worktree", "prune-stale"],
    ("add-harness",): ["add-harness", "codex"],
    ("alloc",): ["alloc", REPO_NAME, "--task", "t1"],
    ("release",): ["release", "work/app_1", "--task", "t1"],
    ("task", "new"): ["task", "new", "t1"],
    ("task", "prefix"): ["task", "prefix", "t1", "dm", "--user-ack", "dm"],
    ("task", "end"): ["task", "end", "t1"],
    ("task", "reopen"): ["task", "reopen", "t1"],
}

# 화면에 자리가 없는 것들 — 다른 탭이 부르는 명령(`update`·`upstream`·`sync-adapter-config`)과
# 형제 하위명령(`repo remove`), 그리고 상위 토큰만 준 형태.
OUT_OF_LIST = [
    ["repo", "remove", REPO_NAME],
    ["init", "--prefix", "dm"],
    ["update"],
    ["upstream", "set", "/tmp/x"],
    ["sync-adapter-config", "--accept-all"],
    ["whoami"],
    ["repo"],
    ["task"],
    ["worktree"],
    [],
]


@pytest.fixture(scope="module")
def pm_home(tmp_path_factory) -> Path:
    """실 PM 홈 하나 — 거절 경로가 트리를 건드리지 않는지 재려면 실물이어야 한다."""
    root = tmp_path_factory.mktemp("run-allowlist")
    bare = make_bare_with_tags(root / "remote")
    home = make_pm_home(root / "demo", upstream=REPO)
    api = Api(folder_picker=lambda: None, registry_file=root / "config" / "projects.json")
    assert api.run(str(home), ["repo", "add", REPO_NAME, "--git", str(bare)])["rc"] == 0
    return home


@pytest.fixture
def api(tmp_path) -> Api:
    return Api(folder_picker=lambda: None, registry_file=tmp_path / "config" / "projects.json")


@pytest.fixture
def spawned(monkeypatch) -> list[list[str]]:
    """스폰된 명령줄의 기록 — 실행은 그대로 하고 인자만 관찰한다(결과를 지어내지 않는다)."""
    seen: list[list[str]] = []
    original = subprocess.run

    def observer(argv, *args, **kwargs):
        seen.append(list(argv))
        return original(argv, *args, **kwargs)

    monkeypatch.setattr("manager.bridge_settings.subprocess.run", observer)
    return seen


@pytest.fixture
def no_spawn(monkeypatch):
    """스폰 자리의 관찰자 — 불리면 그 자리에서 실패한다(결과를 지어내지 않는다)."""
    def observer(*args, **kwargs):
        raise AssertionError(f"서브프로세스가 떴다: {args!r}")
    monkeypatch.setattr("manager.bridge_settings.subprocess.run", observer)


# ── ① 허용 목록의 결속 축 ────────────────────────────────────────────────────


def test_the_allowlist_is_a_set_of_leading_token_tuples(api):
    assert len(ALLOWED_COMMANDS) == 14
    assert ("whoami",) not in ALLOWED_COMMANDS      # 부르는 곳 없는 표면을 두지 않는다
    assert all(isinstance(command, tuple) for command in ALLOWED_COMMANDS)
    # 한 축에 두 낱말이 필요한 것과 한 낱말짜리가 같은 집합에 있다.
    assert ("repo", "add") in ALLOWED_COMMANDS and ("repo",) not in ALLOWED_COMMANDS
    assert ("alloc",) in ALLOWED_COMMANDS


def test_the_screen_argv_of_all_fourteen_commands_passes_the_gate():
    """정상 명령이 막히지 않는다 — 14개 각각의 실제 인자 형식으로 맞춘다."""
    assert set(SCREEN_ARGV) == set(ALLOWED_COMMANDS)
    for command, argv in SCREEN_ARGV.items():
        assert matched_command(argv) == command, argv


@pytest.mark.parametrize("argv", OUT_OF_LIST, ids=lambda argv: " ".join(argv) or "(빈 인자)")
def test_a_command_outside_the_list_never_spawns(api, pm_home, no_spawn, argv):
    response = api.run(str(pm_home), argv)

    assert response["error"]["kind"] == "not-allowed"
    assert response["error"]["rc"] == REJECT_RC
    assert response["error"]["stderr"] == ""


def test_release_stays_in_the_list_because_the_screen_calls_it(api, pm_home, spawned):
    """현황 탭의 회수 대기 행이 부르는 자리다 — 목록에서 빼면 그 [반납] 이 거절로 죽는다."""
    assert ("release",) in ALLOWED_COMMANDS
    argv = SCREEN_ARGV[("release",)]
    assert matched_command(argv) == ("release",)

    response = api.run(str(pm_home), argv)

    assert "error" not in response          # 거절이 아니라 사본이 답한 자리다
    assert spawned, "허용 목록을 지났는데 서브프로세스가 뜨지 않았다"
    assert spawned[0][-4:] == argv


def test_the_first_token_alone_does_not_open_the_family(api, pm_home, no_spawn):
    """`repo` 를 여는 것과 `repo add` 를 여는 것은 다르다 — 형제 하위명령은 닫혀 있다."""
    assert matched_command(["repo", "remove", REPO_NAME]) is None
    assert matched_command(["task", "archive", "t1"]) is None
    assert api.run(str(pm_home), ["repo", "remove", REPO_NAME])["error"]["kind"] == "not-allowed"


# ── ② --force 는 argv 어디에 있어도 ──────────────────────────────────────────


@pytest.mark.parametrize("argv", [
    ["release", "work/app_1", "--force"],
    ["release", "--force", "work/app_1"],
    ["worktree", "remove", "work/app_1", "--force=1"],
    ["repo", "remove", "--force"],
])
def test_the_force_flag_is_refused_before_any_spawn(api, pm_home, no_spawn, argv):
    response = api.run(str(pm_home), argv)

    assert response["error"]["kind"] in {"forbidden-flag", "not-allowed"}
    assert response["error"]["rc"] == REJECT_RC
    if matched_command(argv) is not None:
        assert response["error"]["kind"] == "forbidden-flag"
        assert FORBIDDEN_FLAG in response["error"]["message"]


def test_the_screen_never_writes_the_force_flag():
    """노출 0 — 화면 소스 어디에도 그 토큰을 넘기는 문자열이 없다(설명 문장은 인용부호 밖이다)."""
    sources = [path for path in repo_owned_paths(REPO, "manager/ui/src", mode=OWNED)
               if path.suffix in {".ts", ".tsx"} and "__tests__" not in path.parts]

    assert sources
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for literal in (f'"{FORBIDDEN_FLAG}"', f"'{FORBIDDEN_FLAG}'"):
            assert literal not in text, f"{source.name} 이 {FORBIDDEN_FLAG} 를 넘긴다"


def test_the_force_scan_reads_value_forms_too():
    assert forbidden_flag(["release", "x"]) is None
    assert forbidden_flag(["release", "--force"]) == "--force"
    assert forbidden_flag(["release", "--force=1"]) == "--force=1"
    # 이름이 비슷한 다른 플래그까지 막지는 않는다.
    assert forbidden_flag(["release", "--forced-note"]) is None


def test_rejections_leave_the_project_tree_untouched(api, pm_home, tmp_path):
    """거절은 부작용 0이다 — 목록 밖 명령이 트리를 건드릴 기회 자체가 없다."""
    home = tmp_path / "demo"
    shutil.copytree(pm_home, home, symlinks=True)
    before = tree_snapshot(home)

    for argv in [*OUT_OF_LIST, ["release", "work/app_1", "--force"]]:
        assert "error" in api.run(str(home), argv)

    assert tree_snapshot(home) == before


# ── ③ 거부 형태 ─────────────────────────────────────────────────────────────


def test_the_refusal_has_the_shape_the_screen_already_knows(api, pm_home, no_spawn):
    error = api.run(str(pm_home), ["update"])["error"]
    known = CallError("spawn", "다른 축의 실패").as_error_dict()["error"]

    assert error.keys() == known.keys()
    assert "허용:" in error["message"]                # 무엇을 부를 수 있는지 말해 준다
    assert error["kind"] not in CallError.KINDS       # 부르다 실패한 자리가 아니다


def test_a_refusal_comes_before_the_path_is_even_resolved(api, no_spawn):
    response = api.run("/없는/경로", ["repo", "remove", REPO_NAME])

    assert response["error"]["kind"] == "not-allowed"


# ── ④ 승인 토큰은 브리지가 만들지 않는다 ─────────────────────────────────────


def test_the_bridge_never_writes_an_approval_token(api, pm_home, spawned):
    """붙이지 않고 부르면 엔진이 그대로 거부한다 — 브리지가 채워 주는 자리가 없다는 실측."""
    response = api.run(str(pm_home), ["worktree", "add", REPO_NAME])

    assert response["rc"] == 1
    assert "물리 슬롯 생성에는 사용자 명시 승인이 필요하다" in response["stderr"]
    assert "--user-ack" not in spawned[0]


def test_the_argv_reaches_the_copy_unchanged(api, pm_home, spawned):
    """브리지가 argv 를 늘리거나 줄이지 않는다 — 인터프리터 + 사본 스크립트 + 넘긴 토큰뿐이다."""
    assert api.run(str(pm_home), ["repo", "list"])["rc"] == 0

    expected = [*runner.interpreter(pm_home),
                str(pm_home / ".project_manager" / "tools" / "pm_config.py"), "repo", "list"]
    assert spawned == [expected]


pytestmark = git_required
