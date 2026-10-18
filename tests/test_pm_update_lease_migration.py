"""흡수 말미 리스 장부 마이그레이션의 직렬화 (T-0919 수정 라운드 F-001).

`pm_update.drop_self_lease_rows` 의 쓰기 형태(tmp → 원자 교체)와 멱등성을 잰다. 배타 구간
(`worktree-leases.lock`) 축은 `tests/test_home_slot_concept_removed.py::
test_home_row_migration_writes_under_the_lease_lock` 이 소유한다.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ledger(dest: Path, leases: list[dict]) -> Path:
    ledger = dest / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"leases": leases, "tasks": []}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return ledger


def _rows(ledger: Path) -> list[str]:
    data = json.loads(ledger.read_text(encoding="utf-8"))
    return [row["slot"] for row in data["leases"]]


def test_migration_writes_through_tmp_and_atomic_replace(tmp_path):
    pm_update = _load("pm_update")
    dest = tmp_path / "home"
    ledger = _make_ledger(dest, [{"slot": "."}, {"slot": "proj_1"}])
    seen: list[tuple[str, str]] = []
    real_replace = pm_update.fs.atomic_replace

    def spy(src, dst):
        seen.append((Path(src).name, Path(dst).name))
        real_replace(src, dst)

    seam = pm_update.fs
    original = seam.atomic_replace
    seam.atomic_replace = spy
    try:
        assert pm_update.drop_self_lease_rows(dest, write=True) == 1
    finally:
        seam.atomic_replace = original
    assert seen == [("worktree-leases.json.tmp", "worktree-leases.json")]
    assert _rows(ledger) == ["proj_1"]
    assert not (ledger.parent / "worktree-leases.json.tmp").exists()


def test_dry_run_and_idempotent_rerun_do_not_write(tmp_path):
    pm_update = _load("pm_update")
    dest = tmp_path / "home"
    ledger = _make_ledger(dest, [{"slot": "."}, {"slot": "proj_1"}])
    before = ledger.read_bytes()
    assert pm_update.drop_self_lease_rows(dest, write=False) == 1
    assert ledger.read_bytes() == before
    assert pm_update.drop_self_lease_rows(dest, write=True) == 1
    after = ledger.read_bytes()
    assert pm_update.drop_self_lease_rows(dest, write=True) == 0
    assert ledger.read_bytes() == after
