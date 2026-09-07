"""아이들 규칙 — 어느 행에서 변경 동작이 열리나 (T-0913 AT-003).

판정표는 화면과 이 테스트가 **같은 파일**(`manager/ui/src/lib/status-dots.json`)을 읽는다. 점과
변경 동작 가부가 같은 한 줄에서 나오고, 위에서부터 첫 일치가 이긴다.

행은 실 명령이 만든 것을 실 `status --json` 으로 읽는다: 아이들 work 슬롯 · 대여된 채 pid 가
죽은 work 슬롯 · readonly 공유 슬롯 · pid 0 인 task. readonly 슬롯은 `leased`+`pid 0`+
`alive false` 라 순서가 없으면 "회수 대기" 로 오표시되고, 그러면 공유 슬롯마다 회수 사유가
붙는다(변경 동작은 어차피 없지만 사유가 거짓이 된다).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _manager_home import git_required, make_bare_with_tags, make_pm_home
from _repo_owned_inventory import OWNED, repo_owned_paths

from manager.bridge import Api

REPO = Path(__file__).resolve().parents[1]
RULES_FILE = REPO / "manager" / "ui" / "src" / "lib" / "status-dots.json"
# 동작 가부를 스스로 계산하면 안 되는 자리 — 판정 네 필드를 직접 읽지 않는다.
ACTION_SOURCES = (
    REPO / "manager" / "ui" / "src" / "components" / "StatusRowActions.tsx",
    REPO / "manager" / "ui" / "src" / "tabs" / "ConfigTab.tsx",
)
JUDGEMENT_FIELDS = (".alive", ".pid", ".state", ".role")
REPO_NAME = "app"

pytestmark = git_required


def _rules() -> list[dict]:
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))["rules"]


def _verdict(row: dict, rules: list[dict] | None = None) -> dict:
    """화면(`status.ts`)과 같은 평가 — 첫 일치가 이기고, 일치가 없으면 조용히 넘어가지 않는다."""
    for rule in rules if rules is not None else _rules():
        if rule["field"] not in row:
            continue
        value, expected = row[rule["field"]], rule["equals"]
        matched = (value is expected if isinstance(expected, bool) or isinstance(value, bool)
                   else value == expected)
        if matched:
            return rule
    raise ValueError(f"상태 판정 규칙에 없는 행: {row}")


@pytest.fixture(scope="module")
def rows(tmp_path_factory) -> dict[str, dict]:
    """세 종류 슬롯 행과 아이들 task 행 — 전부 브리지 `run` 이 만든다."""
    root = tmp_path_factory.mktemp("idle-rule")
    bare = make_bare_with_tags(root / "remote")
    home = make_pm_home(root / "demo", upstream=REPO)
    api = Api(folder_picker=lambda: None, registry_file=root / "config" / "projects.json")

    def run(argv: list[str]) -> dict:
        result = api.run(str(home), argv)
        assert result.get("rc") == 0, result
        return result

    run(["repo", "add", REPO_NAME, "--git", str(bare)])
    run(["worktree", "add", REPO_NAME, "--user-ack", REPO_NAME])
    run(["worktree", "add", REPO_NAME, "--readonly", "--user-ack", REPO_NAME])
    run(["task", "new", "t1"])
    document = api.status(str(home))
    # 만든 work 슬롯 하나를 반납해 아이들로 만든다(생성 직후는 만든 프로세스 명의로 대여 상태).
    created = next(row["slot"] for row in document["slots"]
                   if row["role"] == "work" and row["slot"] != ".")
    run(["release", created])

    document = api.status(str(home))
    slots = document["slots"]
    return {
        "idle": next(row for row in slots if row["state"] == "idle"),
        "reclaim": next(row for row in slots
                        if row["state"] == "leased" and not row["alive"]
                        and row["role"] != "readonly"),
        "readonly": next(row for row in slots if row["role"] == "readonly"),
        "task": next(row for row in document["tasks"] if row["name"] == "t1"),
    }


# ── ① 실제 행의 형태 ────────────────────────────────────────────────────────


def test_the_readonly_slot_really_looks_like_a_reclaim_candidate(rows):
    """함정이 실재하는지 먼저 본다 — 이 형태가 아니면 아래 판정은 우연히 통과한다."""
    readonly = rows["readonly"]

    assert (readonly["role"], readonly["state"]) == ("readonly", "leased")
    assert (readonly["pid"], readonly["alive"]) == (0, False)


# ── ② 아이들 규칙 ───────────────────────────────────────────────────────────


def test_only_a_live_session_closes_the_change_actions(rows):
    assert _verdict(rows["idle"])["actions"] == "change"
    assert _verdict(rows["task"])["actions"] == "change"          # pid 0 인 task
    # 회수 대기 행은 반납이 열린다 — 화면에서 대여한 슬롯은 브리지 서브프로세스가 끝나는
    # 순간 이 상태가 되고(lease.pid 는 그 프로세스), 그 행의 반납이 대여의 역이다.
    assert _verdict(rows["reclaim"])["actions"] == "change"
    assert _verdict(rows["readonly"])["actions"] == "refresh"     # 변경 축이 아니다


def test_a_live_session_row_is_disabled_by_the_same_table():
    """작업 중(`alive=true`)은 실 pid 없이도 판정표 한 줄이다 — 관리자 쪽 생존 조회가 없다."""
    live = {"slot": "work/app_9", "state": "leased", "pid": 4321, "alive": True, "role": "work"}

    assert _verdict(live)["actions"] == "disabled"
    assert _verdict(live)["dot"] == "orange"


def test_the_readonly_row_must_win_before_the_reclaim_rule(rows):
    """순서가 없으면 공유 슬롯이 회수 대기로 오표시되고 사유가 거짓이 된다."""
    rules = _rules()
    readonly = rows["readonly"]

    role_last = [rule for rule in rules if rule["field"] != "role"] + [rules[0]]
    assert _verdict(readonly, role_last)["actions"] != "refresh"

    assert _verdict(readonly, rules)["actions"] == "refresh"


def test_every_rule_carries_an_action_column(rows):
    """판정표의 셋째 칼럼이 비면 화면이 다시 계산해야 한다 — 그 자리를 만들지 않는다."""
    assert [rule["actions"] for rule in _rules()] == [
        "refresh", "change", "change", "disabled", "change"]


def test_a_row_without_a_matching_rule_stops_instead_of_defaulting():
    with pytest.raises(ValueError):
        _verdict({"slot": "work/app_9"})


# ── ③ 관리자는 생존을 다시 재지 않는다 ──────────────────────────────────────


def test_the_manager_never_looks_up_pid_liveness_itself():
    """`alive` 는 엔진 JSON 값 하나다 — 관리자 쪽 조회 코드가 있으면 두 판정이 갈린다."""
    sources = [path for path in repo_owned_paths(REPO, "manager", mode=OWNED)
               if path.suffix == ".py"]

    assert sources
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for token in ("os.kill", "psutil", "_pid_alive", "pid_is_alive"):
            assert token not in text, f"{source.name} 이 생존을 직접 조회한다: {token}"


def test_the_action_components_do_not_read_the_judgement_fields():
    """가부는 `rowDot` 한 줄에서 온다 — 행 컴포넌트가 네 필드를 직접 보면 판정이 두 벌이 된다."""
    for source in ACTION_SOURCES:
        text = source.read_text(encoding="utf-8")
        assert "rowDot" in text
        for field in JUDGEMENT_FIELDS:
            assert field not in text, f"{source.name} 이 판정 필드를 직접 읽는다: {field}"


def test_the_screen_reads_the_same_rule_file():
    source = (REPO / "manager" / "ui" / "src" / "lib" / "status.ts").read_text(encoding="utf-8")

    assert 'from "./status-dots.json"' in source
    assert "rule.actions as RowActionMode" in source
