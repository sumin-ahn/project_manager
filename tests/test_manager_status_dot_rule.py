"""상태 점 판정 순서 — 실 슬롯이 만든 JSON 으로 잰다 (T-0910 AT-006).

판정표는 화면과 이 테스트가 **같은 파일**(`manager/ui/src/lib/status-dots.json`)을 읽는다 —
위에서부터 첫 일치가 이긴다. 실 `worktree add --readonly` 슬롯은 `leased`+세션 없음이라
`alive false`(닫힘)로 읽히는데, readonly 가 맨 앞이 아니면 공유 슬롯이 그냥 닫힘으로 뭉뚱그려진다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import (
    close_session, git_required, make_bare_with_tags, make_pm_home, pm_config,
)

REPO = Path(__file__).resolve().parents[1]
RULES_FILE = REPO / "manager" / "ui" / "src" / "lib" / "status-dots.json"

pytestmark = git_required


def _rules() -> list[dict]:
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))["rules"]


def _dot(row: dict, rules: list[dict] | None = None) -> str:
    """화면(`status.ts`)과 같은 평가 — 첫 일치가 이기고, 일치가 없으면 조용히 넘어가지 않는다."""
    for rule in rules if rules is not None else _rules():
        if rule["field"] not in row:
            continue
        value, expected = row[rule["field"]], rule["equals"]
        matched = (value is expected if isinstance(expected, bool) or isinstance(value, bool)
                   else value == expected)
        if matched:
            return rule["dot"]
    raise ValueError(f"상태 점 규칙에 없는 행: {row}")


@pytest.fixture(scope="module")
def slots(tmp_path_factory) -> dict[str, dict]:
    """실 슬롯 세 형태 + task 두 형태를 실 명령으로 만든 뒤 실 `status --json` 으로 읽는다."""
    root = tmp_path_factory.mktemp("dot-rule")
    bare = make_bare_with_tags(root / "remote")
    home = make_pm_home(root / "demo")
    pm_config(home, "repo", "add", "app", "--git", str(bare))
    pm_config(home, "worktree", "add", "app", "--readonly", "--user-ack", "app")  # 파랑
    for slot in ("work/app_2", "work/app_3", "work/app_4"):
        pm_config(home, "worktree", "add", "app", "--user-ack", "app")
        pm_config(home, "release", slot)                                          # 회색
    pm_config(home, "task", "new", "idle-task")

    # 살아 있는 대여·task 는 엔진 API 로 만든다 — 이 프로세스가 그 pid 의 주인이라 실제로 살아 있다.
    specification = importlib.util.spec_from_file_location(
        f"_worktree_pool_dot_rule:{home}", home / ".project_manager" / "tools" / "worktree_pool.py")
    pool = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = pool
    specification.loader.exec_module(pool)
    pool.alloc("app", session="demo_2")                                           # 주황
    pool.bind_task("live-task")
    # 닫힘 — 종료 기록(반납)이 남은 대여는 idle 로 돌아간다(회색).
    pool.alloc("app", session="demo_3")
    close_session(pool, "demo_3")

    completed = subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_config.py"),
         "status", "--json"],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    rows = {row["slot"]: row for row in document["slots"]}
    rows.update({row["name"]: row for row in document["tasks"]})
    # 이름이 아니라 **엔진 값의 형태**로 고른다 — 슬롯 번호는 대여가 어느 idle 을 집느냐에 따라 는다.
    slot_rows = document["slots"]
    rows["readonly"] = next(row for row in slot_rows if row["role"] == "readonly")
    rows["idle"] = next(row for row in slot_rows if row["state"] == "idle")
    rows["working"] = next(row for row in slot_rows if row["state"] == "leased" and row["alive"])
    return rows


def test_the_readonly_slot_really_looks_closed(slots):
    """함정이 실재하는지 먼저 확인한다 — 이 형태가 아니면 아래 판정은 우연히 통과한다."""
    readonly = slots["readonly"]

    assert readonly["role"] == "readonly"
    assert readonly["state"] == "leased"
    assert readonly["session"] is None
    assert readonly["alive"] is False
    assert "pid" not in readonly


def test_the_states_map_to_the_colours(slots):
    assert _dot(slots["readonly"]) == "blue", "공유(readonly) 슬롯"
    assert _dot(slots["idle"]) == "gray", "반납된(종료 기록) idle 슬롯"
    assert _dot(slots["working"]) == "orange", "열린 기록이 잡은 슬롯"


def test_task_rows_use_the_same_rules(slots):
    assert _dot(slots["idle-task"]) == "gray"
    assert _dot(slots["live-task"]) == "orange"


def test_the_order_is_load_bearing(slots):
    """readonly 규칙이 앞에 없으면 실 공유 슬롯이 오표시된다."""
    rules = _rules()
    readonly = slots["readonly"]

    # role 규칙을 맨 뒤로 미루면 더 이상 공유 슬롯으로 보이지 않는다(닫힘 규칙이 먼저 잡는다).
    role_last = [rule for rule in rules if rule["field"] != "role"] + [rules[0]]
    assert _dot(readonly, role_last) == "gray"

    assert _dot(readonly, rules) == "blue"


def test_a_row_without_a_matching_rule_stops_instead_of_defaulting(slots):
    with pytest.raises(ValueError):
        _dot({"slot": "work/app_9"})


def test_the_screen_reads_the_same_rule_file():
    """규칙표가 두 벌이 되면 화면과 이 테스트가 갈린다."""
    source = (REPO / "manager" / "ui" / "src" / "lib" / "status.ts").read_text(encoding="utf-8")

    assert 'from "./status-dots.json"' in source
    assert [rule["dot"] for rule in _rules()] == ["blue", "gray", "orange", "gray"]
