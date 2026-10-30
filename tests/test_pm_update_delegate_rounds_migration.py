"""갱신 끝 위임 라운드 기록 파일 키 이동(`update_finish.rename_delegate_round_collected_key`).

옛 키 `harvested_at` 을 `collected_at` 으로 옮기는 쓰기 형태(같은 폴더 임시 파일 → 원자 교체),
값·읽을 수 없는 행·권한 보존, dry-run 과 재실행이 파일을 바꾸지 않음을 잰다.
"""
from __future__ import annotations
from pm_engine.app import update_finish
from pm_engine.infra import delegate_ledger
from pm_engine.infra import fs

import json
import os
import stat
from pathlib import Path

OLD_KEY = "harvested_at"
NEW_KEY = "collected_at"
UNREADABLE_LINE = "{not json\n"


def _row(dest: Path, ordinal: int, value: str | None, **extra) -> dict:
    row = {
        "ticket": "T-0001", "role": "developer", "ordinal": ordinal,
        "run_id": "a" * 32, "copy": str(dest / f"copy-{ordinal}.md"),
        "board_rel": f"tickets/rounds/T-0001/{ordinal:02d}-developer.md",
        "prepared_at": "2026-10-01T00:00:00.000000+00:00", OLD_KEY: value,
    }
    row.update(extra)
    return row


def _line(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _write_ledger(dest: Path, lines: list[str]) -> Path:
    ledger = dest / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(lines), encoding="utf-8", newline="")
    os.chmod(ledger, 0o600)
    return ledger


def test_rename_moves_only_the_key_through_tmp_and_atomic_replace(tmp_path, capsys):
    dest = tmp_path / "home"
    discard_reason = 'value mentions "harvested_at": on purpose'
    rows = [
        _row(dest, 1, None),
        _row(dest, 2, "2026-10-02T00:00:00.000000+00:00"),
        _row(dest, 3, "2026-10-03T00:00:00.000000+00:00",
             abandoned_at="2026-10-03T00:00:00.000000+00:00", discard_reason=discard_reason),
    ]
    ledger = _write_ledger(dest, [_line(rows[0]), UNREADABLE_LINE, _line(rows[1]), _line(rows[2])])
    seen: list[tuple[str, str]] = []
    original = fs.atomic_replace

    def spy(src, dst):
        seen.append((Path(src).name, Path(dst).name))
        original(src, dst)

    fs.atomic_replace = spy
    try:
        assert update_finish.rename_delegate_round_collected_key(dest, write=True) == 3
    finally:
        fs.atomic_replace = original

    assert seen == [("delegate-rounds.jsonl.tmp", "delegate-rounds.jsonl")]
    assert not (ledger.parent / "delegate-rounds.jsonl.tmp").exists()
    assert NEW_KEY in capsys.readouterr().out
    lines = ledger.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines[1] == UNREADABLE_LINE
    moved = [json.loads(line) for index, line in enumerate(lines) if index != 1]
    for before, after in zip(rows, moved):
        assert OLD_KEY not in after
        assert after[NEW_KEY] == before[OLD_KEY]
    assert moved[2]["discard_reason"] == discard_reason
    if os.name == "posix":
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    records = delegate_ledger._delegate_rounds_ledger_records(dest)
    assert [record["ordinal"] for record in records] == [1, 2, 3]


def test_dry_run_and_idempotent_rerun_do_not_write(tmp_path):
    dest = tmp_path / "home"
    ledger = _write_ledger(dest, [_line(_row(dest, 1, None)), _line(_row(dest, 2, None))])
    before = ledger.read_bytes()
    assert update_finish.rename_delegate_round_collected_key(dest, write=False) == 2
    assert ledger.read_bytes() == before
    assert update_finish.rename_delegate_round_collected_key(dest, write=True) == 2
    after = ledger.read_bytes()
    assert update_finish.rename_delegate_round_collected_key(dest, write=True) == 0
    assert ledger.read_bytes() == after
    assert update_finish.rename_delegate_round_collected_key(tmp_path / "empty", write=True) == 0
