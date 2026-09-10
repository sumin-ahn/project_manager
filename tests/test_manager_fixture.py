"""화면 픽스처 drift 가드 — `status.sample.json` 이 현행 엔진 산출과 같은 모양인지 (T-0910 AT-005).

픽스처는 엔진이 실제로 만든 산출물(값만 일반화)이고, 그 자리에서 `pm_import --new` 로 만든 실
PM 홈의 실 `status --json --with-board --with-adapters --from <이 repo>` 와 키 집합을 대조한다.
손으로 적은 필드 목록과 비교하면 엔진이 필드를 바꿔도 red 가 되지 않는다.

픽스처 재생성: tmp PM 홈에서 위 명령을 돌린 산출을 그대로 저장하고, PM 홈·소스 checkout 절대경로와
사람 식별자만 일반화한다(키는 손대지 않는다).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import git_required, make_bare_with_tags, make_pm_home, pm_config

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "manager" / "ui" / "src" / "fixtures"
FIXTURE = FIXTURES / "status.sample.json"
KEYS_FIXTURE = FIXTURES / "conf-keys.sample.json"

pytestmark = git_required


@pytest.fixture(scope="module")
def sample() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> dict:
    """그 자리에서 만든 실 PM 홈의 실 산출 — 무거운 절 둘 다 붙인다."""
    root = tmp_path_factory.mktemp("fixture-home")
    bare = make_bare_with_tags(root / "remote")
    home = make_pm_home(root / "demo")
    # 슬롯 절은 작업 폴더가 있어야 행이 생긴다 — 실 명령으로 하나 만든다.
    pm_config(home, "repo", "add", "app", "--git", str(bare))
    pm_config(home, "worktree", "add", "app", "--user-ack", "app")
    pm_config(home, "task", "new", "demo")
    subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "board.py"), "new",
         "픽스처 대조용 티켓", "--estimate", "small"],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True)
    completed = subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_config.py"), "status",
         "--json", "--with-board", "--with-adapters", "--from", str(REPO)],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _row_keys(rows: list[dict]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        keys |= set(row)
    return keys


@pytest.mark.parametrize("section", ["slots", "tasks", "repos"])
def test_row_fields_match_the_engine(sample, live, section):
    assert _row_keys(sample[section]), f"픽스처에 {section} 행이 없으면 대조가 무의미하다"
    assert _row_keys(live[section]), f"실 산출에 {section} 행이 없으면 대조가 무의미하다"
    assert _row_keys(sample[section]) == _row_keys(live[section])


def test_top_level_keys_match_the_engine(sample, live):
    assert set(sample) == set(live)


@pytest.mark.parametrize("section", ["conf", "git", "board", "adapters"])
def test_section_keys_match_the_engine(sample, live, section):
    assert set(sample[section]) == set(live[section])


@pytest.mark.parametrize("section", ["open", "lint"])
def test_board_row_fields_match_the_engine(sample, live, section):
    assert _row_keys(sample["board"][section]) == _row_keys(live["board"][section])


def test_adapter_file_rows_match_the_engine(sample, live):
    assert _row_keys(sample["adapters"]["files"]) == _row_keys(live["adapters"]["files"])


def test_the_fixture_is_a_real_engine_document(sample):
    assert sample["schema"] == 1
    assert sample["engine_rev"].startswith("v")
    assert sample["board"] is not None and sample["adapters"] is not None


def test_the_fixture_carries_every_status_dot_case(sample):
    """화면 테스트가 세 색과 닫힌 task 를 전부 태울 수 있어야 한다(회색·주황·파랑)."""
    slots = sample["slots"]

    assert any(slot["role"] == "readonly" for slot in slots)
    assert any(slot["state"] == "idle" for slot in slots)
    assert any(slot["state"] == "leased" and slot["alive"] for slot in slots)
    assert any(not task["alive"] for task in sample["tasks"])       # 닫힌 기록


@pytest.mark.parametrize("fixture", [FIXTURE, KEYS_FIXTURE])
def test_the_fixture_carries_no_personal_path(fixture):
    text = fixture.read_text(encoding="utf-8")

    assert "/home/" not in text and "/Users/" not in text


# ── 키 목록 픽스처 (T-0911) ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def live_keys(tmp_path_factory) -> dict:
    """그 자리에서 만든 실 PM 홈의 실 `local_conf.py --keys --json`."""
    home = make_pm_home(tmp_path_factory.mktemp("keys-home") / "demo")
    completed = subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "local_conf.py"),
         "--keys", "--json"],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _member_rows(document: dict) -> list[dict]:
    """패턴 키군에서 전개된 실명 행 전수 — 실명 키(`members is None`)는 셀 것이 없다."""
    return [member for row in document["keys"] for member in row["members"] or []]


def _mismatched_fields(sample: dict, live: dict) -> list[str]:
    """픽스처와 실 산출이 갈린 자리를 `<키>[.<멤버>].<필드>` 로 짚는다(실패 메시지용).

    문서 전체를 통째로 비교하면 red 는 뜨지만 어느 행이 어긋났는지 눈으로 찾아야 한다 —
    어긋난 좌표를 값으로 적어 재생성 대상이 바로 읽히게 한다.
    """
    out: list[str] = []
    for axis in ("schema", "groups"):
        if sample.get(axis) != live.get(axis):
            out.append(axis)
    sample_keys = {row["key"]: row for row in sample["keys"]}
    live_keys_by_name = {row["key"]: row for row in live["keys"]}
    for key in sorted(set(sample_keys) | set(live_keys_by_name)):
        sample_row, live_row = sample_keys.get(key, {}), live_keys_by_name.get(key, {})
        for field in sorted(set(sample_row) | set(live_row)):
            if field == "members":
                sample_members = {m["key"]: m for m in sample_row.get("members") or []}
                live_members = {m["key"]: m for m in live_row.get("members") or []}
                for member in sorted(set(sample_members) | set(live_members)):
                    sample_member = sample_members.get(member, {})
                    live_member = live_members.get(member, {})
                    out += [f"{key}.{member}.{name}"
                            for name in sorted(set(sample_member) | set(live_member))
                            if sample_member.get(name) != live_member.get(name)]
            elif sample_row.get(field) != live_row.get(field):
                out.append(f"{key}.{field}")
    return out


def test_the_key_fixture_is_the_engines_own_registry(live_keys):
    """설정 화면 픽스처가 현행 레지스트리와 **문서 전체로** 같은지 (손으로 적은 목록 0).

    키·필드 이름 집합만 대조하면 필드 **값**(설명·기본값·선택지·권장값)이 갈려도 green 이라,
    화면 회귀 전부가 엔진이 더는 내지 않는 값 위에서 돈다 — 실제로 `additional_reviewer.reasoning`
    의 설명이 어긋난 채 통과했다. 재생성은 이 픽스처를 만든 것과 같은 명령이다
    (`local_conf.py --keys --json` 산출을 그대로 저장).
    """
    sample = json.loads(KEYS_FIXTURE.read_text(encoding="utf-8"))

    assert _member_rows(live_keys), "실 산출에 멤버 행이 없으면 대조가 무의미하다"
    assert sample == live_keys, (
        "픽스처가 실 레지스트리와 갈렸다 — 어긋난 자리: "
        f"{_mismatched_fields(sample, live_keys)}")
