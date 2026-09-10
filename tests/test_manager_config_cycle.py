"""구성 탭·행 동작의 명령 사이클 — 실 PM 홈 + 실 bare 로 14개를 브리지로 돌린다 (T-0913 AT-001).

가짜 runner 없이 **실물**로 잰다: `pm_import --new` 로 만든 PM 홈, `git clone --bare` 로 만든
로컬 bare, 그리고 화면이 부르는 것과 같은 `Api.run(path, argv)` 서브프로세스.

사이클 순서는 엔진이 강제하는 순서다 — `alloc` 은 task 가 먼저 있어야 하고(`task new`),
물리 슬롯 생성과 새 prefix 신설은 승인 값이 있어야 한다(`--user-ack`):

    repo add → task new → worktree add → alloc → task prefix → release(거부) → release
    → task end → task reopen → worktree remove → prune-stale

축 다섯:
  1. 14개 명령이 **실제 인자 형식 그대로** 통과한다(이름은 위치 인자·URL 은 `--git` 플래그).
  2. 단계마다 장부가 바뀐다 — 판정은 관리자가 아니라 `status --json` 의 repos·slots·tasks 다.
  3. 엔진의 거부(승인 없음·task 없음·남의 task 반납)는 rc 와 문구가 원문 그대로 올라온다.
  4. 승인 토큰은 인자로 준 값뿐이다 — 브리지가 붙여 주는 자리가 없어 없으면 그대로 거부된다.
  5. 조회 명령은 프로젝트 트리를 한 파일도 바꾸지 않는다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from _manager_home import git_required, make_bare_with_tags, make_pm_home, tree_snapshot

from manager.bridge import Api
from manager.bridge_run import ALLOWED_COMMANDS, matched_command

REPO = Path(__file__).resolve().parents[1]

pytestmark = git_required

# 사이클이 쓰는 이름 — repo 이름은 `--user-ack` 값과 정확히 같아야 한다(엔진 결속 규칙).
# PM 홈 폴더 이름(`demo`)은 그 자신이 repo 로 등록돼 있어 다른 이름을 쓴다(같으면 `repo add`
# 가 "이미 등록됨" 으로 등록을 건너뛴다 — 실측).
REPO_NAME = "app"
TASK = "t1"
OTHER_TASK = "other"
PREFIX = "dm"


class Cycle:
    """단계마다 `run` 산출과 **직후 재조회**한 현황을 남긴다(화면이 하는 것과 같은 순서)."""

    def __init__(self, api: Api, home: Path) -> None:
        self.api = api
        self.home = home
        self.steps: dict[str, dict] = {}

    def run(self, name: str, argv: list[str]) -> dict:
        result = self.api.run(str(self.home), argv)
        assert "error" not in result, (name, result)
        document = self.api.status(str(self.home))
        assert "error" not in document, (name, document)
        self.steps[name] = {"argv": argv, **result, "status": document}
        return self.steps[name]

    def status(self) -> dict:
        return self.api.status(str(self.home))


def _slots(document: dict) -> dict[str, dict]:
    return {row["slot"]: row for row in document["slots"]}


def _work_slot(document: dict) -> str:
    """이 사이클이 만든 work 슬롯 — 갓 만든 홈의 `.` 슬롯과 readonly 는 뺀다."""
    return next(row["slot"] for row in document["slots"]
                if row["slot"] != "." and row["role"] != "readonly")


@pytest.fixture(scope="module")
def cycle(tmp_path_factory) -> Cycle:
    root = tmp_path_factory.mktemp("config-cycle")
    bare = make_bare_with_tags(root / "remote")
    # upstream 은 이 트리다 — `add-harness` 가 소스를 로컬에서 해소한다(네트워크 0).
    home = make_pm_home(root / "demo", upstream=REPO)
    api = Api(folder_picker=lambda: None, registry_file=root / "config" / "projects.json")
    run = Cycle(api, home)

    run.run("repo add", ["repo", "add", REPO_NAME, "--git", str(bare),
                         "--test", "python3 -m pytest -q", "--protected", "main,release"])
    run.run("repo list", ["repo", "list"])
    run.run("repo protected show", ["repo", "protected", REPO_NAME])
    run.run("repo protected set", ["repo", "protected", REPO_NAME, "main"])
    run.run("alloc without task", ["alloc", REPO_NAME, "--task", TASK])
    run.run("task new", ["task", "new", TASK])
    run.run("worktree add without ack", ["worktree", "add", REPO_NAME])
    run.run("worktree add", ["worktree", "add", REPO_NAME, "--user-ack", REPO_NAME])
    # 갓 만든 슬롯은 무소유 idle 이다(T-0915 — 생성은 풀 확장이고 점유는 대여다). 같은 세션의
    # 다음 대여가 그 슬롯을 그대로 집는다 — 사이에 반납 단계가 없다.
    run.run("alloc", ["alloc", REPO_NAME, "--task", TASK])
    run.run("task prefix without ack", ["task", "prefix", TASK, PREFIX])
    run.run("task prefix", ["task", "prefix", TASK, PREFIX, "--user-ack", PREFIX])
    slot = _work_slot(run.steps["alloc"]["status"])
    run.run("release other task", ["release", slot, "--task", OTHER_TASK])
    run.run("release", ["release", slot, "--task", TASK])
    run.run("task end", ["task", "end", TASK])
    run.run("task reopen", ["task", "reopen", TASK])
    run.run("worktree remove", ["worktree", "remove", slot])
    run.run("prune-stale", ["worktree", "prune-stale"])
    run.run("worktree add readonly", ["worktree", "add", REPO_NAME, "--readonly",
                                      "--user-ack", REPO_NAME])
    run.run("add-harness", ["add-harness", "codex"])
    run.run("status", ["status", "--json"])
    return run


# ── ① 14개 명령이 실제 인자 형식으로 통과한다 ────────────────────────────────


def test_every_command_of_the_screen_runs_through_the_bridge(cycle):
    """화면이 부르는 14개가 전부 실제 인자 형식으로 통과했다 — 허용 목록이 정상 명령을 막지 않는다."""
    called = {matched_command(step["argv"]) for step in cycle.steps.values()}

    assert called == set(ALLOWED_COMMANDS)
    # 거부도 rc 로만 온다 — 브리지가 `error` 로 바꾼 자리가 하나도 없다.
    assert all("rc" in step for step in cycle.steps.values())


def test_repo_add_takes_the_url_as_a_flag_and_registers_the_row(cycle):
    step = cycle.steps["repo add"]

    assert step["rc"] == 0, step["stderr"]
    row = next(row for row in step["status"]["repos"] if row["repo"] == REPO_NAME)
    assert row["protected"] == "main,release"
    assert row["test_cmd"] == "python3 -m pytest -q"
    # 조회는 같은 칼럼을 사람 표로 낸다.
    assert REPO_NAME in cycle.steps["repo list"]["stdout"]
    assert "protected" in cycle.steps["repo protected show"]["stdout"]


def test_repo_protected_set_changes_the_ledger_column(cycle):
    assert cycle.steps["repo protected set"]["rc"] == 0
    row = next(row for row in cycle.steps["repo protected set"]["status"]["repos"]
               if row["repo"] == REPO_NAME)
    assert row["protected"] == "main"


# ── ② 장부 변화 — 판정은 status --json 이 낸다 ───────────────────────────────


def test_worktree_add_creates_a_slot_and_alloc_binds_it_to_the_task(cycle):
    before = _slots(cycle.steps["task new"]["status"])
    after = _slots(cycle.steps["worktree add"]["status"])
    created = set(after) - set(before)

    assert cycle.steps["worktree add"]["rc"] == 0
    assert len(created) == 1
    slot = created.pop()
    # 갓 만든 슬롯은 무소유 idle 로 풀에 들어간다 — 세션 명의로 남으면 열린 기록이라 같은
    # 세션의 alloc 이 풀 소진으로 막히고 화면에도 복구 경로가 없다.
    assert after[slot]["role"] == "work"
    assert (after[slot]["state"], after[slot]["session"], after[slot]["alive"]) == ("idle", None, False)
    # 그래서 그 다음 대여(같은 세션)가 반납 없이 바로 그 슬롯을 집는다.
    leased = _slots(cycle.steps["alloc"]["status"])[slot]
    assert cycle.steps["alloc"]["rc"] == 0
    assert leased["state"] == "leased" and leased["session"] == TASK
    assert leased["task"] == TASK
    assert cycle.steps["alloc"]["status"]["tasks"][0]["slots"] == [slot]


def test_task_prefix_stores_the_label_and_the_audit_line_stays(cycle):
    step = cycle.steps["task prefix"]

    assert step["rc"] == 0
    assert step["status"]["tasks"][0]["prefix"] == PREFIX
    assert "[승인 감사]" in step["stdout"]


def test_release_returns_the_slot_to_the_pool(cycle):
    step = cycle.steps["release"]
    slot = _work_slot(step["status"])

    assert step["rc"] == 0
    assert _slots(step["status"])[slot]["state"] == "idle"
    assert step["status"]["tasks"][0]["slots"] == []


def test_task_end_drops_the_row_and_reopen_restores_it_reset(cycle):
    """종료한 task 는 현황 행이 없다 — 되살리기가 행 동작이 아니라 이름 입력인 이유다."""
    assert cycle.steps["task end"]["rc"] == 0
    assert cycle.steps["task end"]["status"]["tasks"] == []

    restored = cycle.steps["task reopen"]["status"]["tasks"]
    assert cycle.steps["task reopen"]["rc"] == 0
    assert [row["name"] for row in restored] == [TASK]
    assert restored[0]["prefix"] is None and restored[0]["alive"] is False


def test_worktree_remove_and_prune_stale_clear_the_ledger(cycle):
    removed = cycle.steps["worktree remove"]
    slot = _work_slot(cycle.steps["release"]["status"])

    assert removed["rc"] == 0
    assert slot not in _slots(removed["status"])
    assert cycle.steps["prune-stale"]["rc"] == 0
    assert "dangling" in cycle.steps["prune-stale"]["stdout"]


def test_readonly_slot_and_add_harness_land_in_the_status_document(cycle):
    readonly = cycle.steps["worktree add readonly"]
    slot = next(row for row in readonly["status"]["slots"] if row["role"] == "readonly")

    assert readonly["rc"] == 0
    assert (slot["state"], slot["session"], slot["alive"]) == ("leased", None, False)
    assert cycle.steps["add-harness"]["rc"] == 0
    assert cycle.steps["add-harness"]["status"]["harnesses"] == ["claude", "codex"]


# ── ③ 거부 문구는 엔진 원문 그대로 ───────────────────────────────────────────


def test_a_slot_is_not_created_without_the_user_approval_value(cycle):
    step = cycle.steps["worktree add without ack"]

    assert step["rc"] == 1
    assert "물리 슬롯 생성에는 사용자 명시 승인이 필요하다" in step["stderr"]
    assert f"--user-ack {REPO_NAME}" in step["stderr"]
    # 슬롯은 늘지 않았다 — 거부가 곧 부작용 0이다.
    assert _slots(step["status"]).keys() == _slots(cycle.steps["task new"]["status"]).keys()


def test_alloc_before_the_task_exists_is_refused_in_the_engine_words(cycle):
    step = cycle.steps["alloc without task"]

    assert step["rc"] == 1
    assert "아직 없다" in step["stderr"]
    assert step["status"]["tasks"] == []


def test_release_refuses_another_task_slot(cycle):
    step = cycle.steps["release other task"]

    assert step["rc"] == 1
    assert "소유가 아니다" in step["stderr"]
    slot = _work_slot(step["status"])
    assert _slots(step["status"])[slot]["state"] == "leased"


def test_a_new_prefix_is_refused_without_the_approval_value(cycle):
    step = cycle.steps["task prefix without ack"]

    assert step["rc"] == 1
    assert "사용자 명시 승인이 필요하다" in step["stderr"]
    assert step["status"]["tasks"][0]["prefix"] is None


# ── ④ 조회는 프로젝트 트리를 바꾸지 않는다 ───────────────────────────────────


def test_read_only_commands_change_no_file_in_the_project(cycle):
    before = tree_snapshot(cycle.home)

    assert cycle.api.run(str(cycle.home), ["repo", "list"])["rc"] == 0
    assert cycle.api.run(str(cycle.home), ["repo", "protected", REPO_NAME])["rc"] == 0
    assert cycle.api.run(str(cycle.home), ["status", "--json"])["rc"] == 0

    assert tree_snapshot(cycle.home) == before
