"""설정 행 접기 규칙 — 화면과 이 테스트가 **같은 규칙표**를 읽는지 (T-0917 AT-001).

접기 판정표는 `manager/ui/src/lib/settings-rows.json` 하나다. 네 축(값 없음 · 필수 아님 ·
기본값 없음 · 배지 없음)이 전부 참인 행만 접히고, 그 판정이 화면 소스에 다시 박히면 두 벌이
되어 한쪽만 고쳐지는 날 갈린다. 한 줄로 묶는 세트(역할 묶음·교차 검토 묶음)는 엔진이 행마다 준
`unit` 에서만 나오므로 화면에 역할·티어 목록이 필요 없다.

판정 대상은 실물이다: 그 자리에서 만든 실 PM 홈의 실 `local_conf.py --keys --json` 산출로 접기
판정을 돌리고, 화면 소스에는 그 산출이 소유한 이름(하네스·역할·티어·모델)이 없는지 본다.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import git_required, make_pm_home

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "manager" / "ui" / "src"
RULES_FILE = UI / "lib" / "settings-rows.json"
RULE_READER = UI / "lib" / "settings-rows.ts"
SCREENS = (UI / "tabs" / "SettingsTab.tsx", UI / "components" / "QuestionMode.tsx")

pytestmark = git_required


def _rule() -> dict:
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def registry(tmp_path_factory) -> dict:
    """그 자리에서 만든 실 PM 홈의 실 키 레지스트리 — 화면 픽스처와 같은 명령이다."""
    home = make_pm_home(tmp_path_factory.mktemp("fold-rule-home") / "demo")
    completed = subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "local_conf.py"),
         "--keys", "--json"],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _rows(registry: dict) -> list[dict]:
    """화면이 그리는 행 전수 — 실명 키와 패턴 키군에서 전개된 실명이 한 목록이다."""
    rows: list[dict] = []
    for entry in registry["keys"]:
        if entry["kind"] == "key":
            rows.append({"key": entry["key"], "default": entry["default"],
                         "required": entry["required"], "family": None, "unit": entry["unit"]})
            continue
        for member in entry["members"] or []:
            rows.append({"key": member["key"], "default": member["default"],
                         "required": entry["required"], "family": entry["key"],
                         "unit": member["unit"]})
    return rows


def _units(registry: dict) -> dict[str, list[dict]]:
    """엔진이 준 세트 이름 → 그 세트의 행(전개 순서)."""
    units: dict[str, list[dict]] = {}
    for row in _rows(registry):
        if row["unit"] is not None:
            units.setdefault(row["unit"], []).append(row)
    return units


def _foldable(row: dict, value: str, marked: set[str]) -> bool:
    """규칙표 그대로의 판정 — 화면(`lib/settings-rows.ts`)과 같은 식이다."""
    fold = _rule()["fold"]
    return (value == fold["value"] and row["required"] == fold["required"]
            and row["default"] == fold["default"] and row["key"] not in marked)


# ── ① 규칙표와 화면이 한 자리를 본다 ─────────────────────────────────────────


def test_the_screen_reads_the_same_rule_file():
    """판정표가 두 벌이 되면 화면과 이 테스트가 갈린다."""
    assert 'from "./settings-rows.json"' in _source(RULE_READER)
    screen = _source(SCREENS[0])
    assert 'from "../lib/settings-rows"' in screen
    assert "foldable(" in screen and "markedKeys(" in screen


def test_the_fold_verdict_is_not_written_again_in_the_screen():
    """네 축을 화면이 다시 적으면 규칙표를 고쳐도 화면이 따라오지 않는다."""
    screen = _source(SCREENS[0])

    assert "function foldable" not in screen
    assert "required === false" not in screen and "required === true" not in screen
    assert "default === null" not in screen
    for name in _rule()["fold"]["marks"]:
        assert f'"{name}"' not in screen, f"배지 목록 이름을 화면이 들고 있다: {name}"


def test_the_rule_counts_the_same_badges_the_screen_draws():
    """접기의 '배지 없음' 은 행에 그려지는 배지와 같은 집합이어야 한다."""
    screen = _source(SCREENS[0])
    assert "function RowMarks" in screen, "행 배지를 그리는 자리를 못 찾았다"
    marks = screen.split("function RowMarks", 1)[1].split("\n}\n", 1)[0]
    drawn = sorted(set(re.findall(r"conf\.([a-z_]+)", marks)))

    assert set(_rule()["fold"]) == {"value", "required", "default", "marks"}
    assert drawn == sorted(_rule()["fold"]["marks"])


# ── ② 네 축이 실 레지스트리에서 실제로 갈린다 ────────────────────────────────


def test_a_row_folds_only_when_all_four_axes_say_so(registry):
    rows = _rows(registry)
    plain = next(row for row in rows
                 if not row["required"] and row["default"] is None)
    with_default = next(row for row in rows if row["default"] is not None)
    required = next(row for row in rows if row["required"])

    assert _foldable(plain, "", set())
    assert not _foldable(plain, "값", set())                    # 값 있음
    assert not _foldable(plain, "", {plain["key"]})             # 배지 있음
    assert not _foldable(with_default, "", set())               # 기본값 있음
    assert not _foldable(required, "", set())                   # 필수


def test_the_untouched_registry_folds_most_rows_and_keeps_the_rest(registry):
    """값이 하나도 없는 사본에서 접히는 행이 실제로 있고, 기본값·필수 행은 남는다."""
    rows = _rows(registry)
    folded = [row for row in rows if _foldable(row, "", set())]

    assert folded, "접히는 행이 하나도 없으면 규칙이 무의미하다"
    assert len(folded) < len(rows), "전부 접히면 화면에 남는 것이 없다"
    assert all(not _foldable(row, "", set())
               for row in rows if row["required"] or row["default"] is not None)


# ── ③ 한 줄로 묶는 세트 — 엔진이 준 `unit` 에서만 나온다 ─────────────────────


def test_every_unit_is_named_and_has_the_same_cells(registry):
    """세트마다 줄 이름이 있고 칸 구성이 같다 — 화면은 칸 이름을 표 머리에 한 번만 적는다."""
    units = _units(registry)

    assert len(units) > 1
    assert set(units) == set(registry["units"]), "이름 없는 세트나 행 없는 이름이 있다"
    fields = {tuple(row["key"].rpartition(".")[2] for row in cells) for cells in units.values()}
    assert len(fields) == 1, f"세트마다 칸 구성이 다르다: {fields}"
    assert set(next(iter(fields))) == set(registry["unit_fields"])


def test_the_screen_groups_rows_by_the_engine_unit_only(registry):
    """화면이 키 모양으로 세트를 짐작하면 엔진이 세트를 바꿔도 따라오지 않는다."""
    screen = _source(SCREENS[0])

    assert "row.unit" in screen
    assert "row_unit_family" not in _rule(), "옛 규칙표 축이 남았다"
    for unit in registry["units"]:
        assert unit not in screen, f"세트 이름이 화면에 박혔다: {unit}"


def test_a_unit_row_folds_as_a_whole(registry):
    """세 칸이 모두 비어야 그 줄이 접힌다 — 한 칸이라도 차 있으면 세트로 보인다."""
    name, cells = next(iter(_units(registry).items()))

    assert all(_foldable(cell, "", set()) for cell in cells), name
    filled = {cells[0]["key"]: "값"}
    assert not all(_foldable(cell, filled.get(cell["key"], ""), set()) for cell in cells)


# ── ④ 화면에는 엔진이 소유한 이름이 없다 ─────────────────────────────────────


def _engine_owned_names(registry: dict) -> set[str]:
    """레지스트리가 소유한 값 이름 전수 — 하네스·추론 강도·권장 모델·역할·티어."""
    names: set[str] = set()
    for entry in registry["keys"]:
        candidates = [entry] + list(entry["members"] or [])
        for candidate in candidates:
            choices = candidate.get("choices")
            if choices:
                for bucket, values in choices["values"].items():
                    names.add(bucket)
                    names.update(values)
            if candidate.get("recommended"):
                names.add(candidate["recommended"])
    for unit in registry["units"]:              # 역할·티어 — 세트 이름의 뒤 토큰들
        names.update(unit.split(".")[1:])
    names.discard("")
    return names


def test_the_screen_carries_no_harness_role_tier_or_model_name(registry):
    """이름을 화면이 들면 그 목록은 반드시 엔진과 갈린다 — 목록의 주인은 레지스트리 하나다."""
    names = _engine_owned_names(registry)
    assert len(names) > 5, f"레지스트리에서 이름을 못 모았다: {names}"

    offenders: list[str] = []
    for path in SCREENS + (RULE_READER,):
        source = _source(path)
        for name in names:
            if re.search(r"[\"'`]" + re.escape(name) + r"[\"'`]", source):
                offenders.append(f"{path.name}: {name}")
    assert offenders == [], offenders
