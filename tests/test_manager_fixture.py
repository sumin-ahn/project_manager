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

from _manager_home import git_required, make_pm_home, pm_config

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "manager" / "ui" / "src" / "fixtures" / "status.sample.json"

pytestmark = git_required


@pytest.fixture(scope="module")
def sample() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> dict:
    """그 자리에서 만든 실 PM 홈의 실 산출 — 무거운 절 둘 다 붙인다."""
    home = make_pm_home(tmp_path_factory.mktemp("fixture-home") / "demo")
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
    """화면 테스트가 네 색을 전부 태울 수 있어야 한다(회색·주황·빨강·파랑)."""
    slots = sample["slots"]

    assert any(slot["role"] == "readonly" for slot in slots)
    assert any(slot["state"] == "idle" for slot in slots)
    assert any(slot["state"] == "leased" and slot["alive"] for slot in slots)
    assert any(slot["state"] == "leased" and not slot["alive"]
               and slot["role"] != "readonly" for slot in slots)


def test_the_fixture_carries_no_personal_path():
    text = FIXTURE.read_text(encoding="utf-8")

    assert "/home/" not in text and "/Users/" not in text
