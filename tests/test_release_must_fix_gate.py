"""릴리즈 must-fix 게이트의 수렴 계약.

처분은 ``pm-verified`` 하나뿐이다. 폐지된 ``into``/``fixed``는 파서에서 거부되고
기존 장부의 동일 kind도 미처분으로 fail-loud한다. 라이브 실행은 모두 대역으로 격리한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import types
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".project_manager" / ".local").mkdir(parents=True)
    for status in ("open", "claimed", "blocked", "done"):
        (root / ".project_manager" / "wiki" / "tickets" / status).mkdir(parents=True)
    return root


def _ledger_path(root: Path) -> Path:
    return root / ".project_manager" / ".local" / "review_rounds.json"


def _write_ledger(root: Path, value: object) -> None:
    _ledger_path(root).write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8", newline="\n",
    )


def _rejected_entry(*, resolution: dict | None = None) -> dict:
    entry = {"count": 1, "rounds": [
        {"sequence": 1, "verdict": 1, "must_fix": 2},
    ]}
    if resolution is not None:
        entry["resolution"] = resolution
    return entry


@pytest.fixture
def review(tmp_path, monkeypatch):
    root = _project(tmp_path)
    module = _load("external_review", "release_gate_review")
    monkeypatch.setattr(module, "REPO", root)
    _write_ledger(root, {"T-0610": _rejected_entry()})
    return types.SimpleNamespace(module=module, root=root, ledger=_ledger_path(root))


@pytest.mark.parametrize("removed", ["--into", "--fixed"])
def test_removed_resolution_options_are_rejected_without_ledger_mutation(
    review, removed, capsys,
):
    before = review.ledger.read_bytes()
    with pytest.raises(SystemExit):
        review.module.main(["--resolve-gate", "T-0610", removed, "T-0611"])
    assert review.ledger.read_bytes() == before
    assert f"unrecognized arguments: {removed}" in capsys.readouterr().err


def test_resolve_gate_requires_pm_verified_and_never_suggests_follow_up(review, capsys):
    assert review.module.main(["--resolve-gate", "T-0610"]) == 1
    err = capsys.readouterr().err
    assert "--pm-verified" in err
    assert "--into" not in err and "--fixed" not in err
    assert "후속 티켓" in err and "지원하지 않습니다" in err


def test_external_help_exposes_only_pm_verified_resolution(review, capsys):
    with pytest.raises(SystemExit) as caught:
        review.module.main(["--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "--pm-verified" in help_text
    assert "--into" not in help_text and "--fixed" not in help_text


def test_legacy_resolution_kinds_are_not_current_board_resolutions():
    board = _load("board", "release_gate_board_resolution")
    for kind in ("into", "fixed", "pm-fixed"):
        entry = _rejected_entry(resolution={
            "kind": kind, "round_sequence": 1, "rounds": 1,
        })
        assert board.gate_resolution(entry) is None
        problem = board._gate_disposition_problem("T-0610", entry)
        assert problem is not None and "처분 선언 없음" in problem


def test_pm_verified_is_the_only_current_resolution_and_revalidates_callback():
    board = _load("board", "release_gate_board_pm_verified")
    entry = _rejected_entry(resolution={
        "kind": board.GATE_RESOLUTION_PM_VERIFIED,
        "round_sequence": 1, "rounds": 1,
    })
    assert board.gate_resolution(entry)["kind"] == "pm-verified"
    assert board._gate_disposition_problem(
        "T-0610", entry, allow_pm_verified=True, pm_verified_problem=lambda: None,
    ) is None
    problem = board._gate_disposition_problem(
        "T-0610", entry, allow_pm_verified=True,
        pm_verified_problem=lambda: "accepted 잔여 F-001",
    )
    assert problem is not None and "발동 조건 재검증 실패" in problem


class _Run:
    def __init__(self, result_pin: str):
        self.result_pin = result_pin
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return types.SimpleNamespace(
            returncode=0,
            stdout=f"{self.result_pin} passed, 800 deselected in 1.0s",
            stderr="",
        )


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = _project(tmp_path)
    board = _load("board", "release_gate_live")
    local = root / ".project_manager" / ".local"
    monkeypatch.setattr(board, "REPO", root)
    monkeypatch.setattr(board, "LOCAL_DIR", local)
    monkeypatch.setattr(board, "LIVEGATE_FLAG", local / "livegate.json")
    monkeypatch.setattr(board, "LEASES_FILE", local / "worktree-leases.json")
    monkeypatch.setattr(board, "_git_config_get", lambda cwd, key: None)
    monkeypatch.setattr(board, "_git_head_at", lambda cwd: "f" * 40)
    runner = _Run(board.LIVEGATE_RELEASE_PIN)
    monkeypatch.setattr(board.subprocess, "run", runner)
    args = argparse.Namespace(
        action="record", rev=None, cwd=str(root), repo=None, slot=None, task=None,
    )
    return types.SimpleNamespace(board=board, root=root, runner=runner, args=args)


def test_unresolved_must_fix_blocks_before_release_wave(release, capsys):
    _write_ledger(release.root, {"T-0610": _rejected_entry()})
    assert release.board.cmd_livegate(release.args) == 1
    assert release.runner.calls == []
    err = capsys.readouterr().err
    assert "처분 선언 없음" in err and "--pm-verified" in err


@pytest.mark.parametrize("kind", ["into", "fixed", "pm-fixed"])
def test_legacy_ledger_resolution_does_not_open_release(kind, release, capsys):
    _write_ledger(release.root, {"T-0610": _rejected_entry(resolution={
        "kind": kind, "round_sequence": 1, "rounds": 1,
    })})
    assert release.board.cmd_livegate(release.args) == 1
    assert release.runner.calls == []
    assert "처분 선언 없음" in capsys.readouterr().err


def test_suggestion_only_and_reserved_wave_section_do_not_block(release):
    _write_ledger(release.root, {
        "wave": {"id": "wave", "spent": 1},
        "T-0610": {"rounds": [
            {"sequence": 1, "verdict": 0, "must_fix": 0, "suggestions": 2},
        ]},
    })
    assert release.board.cmd_livegate(release.args) == 0
    assert len(release.runner.calls) == 1


def test_corrupt_gate_entry_fails_closed_before_release_wave(release, capsys):
    _write_ledger(release.root, {"T-0610": {"rounds": "broken"}})
    assert release.board.cmd_livegate(release.args) == 1
    assert release.runner.calls == []
    assert "형식 오류" in capsys.readouterr().err


def test_pm_verified_resolution_is_bound_to_the_latest_round():
    board = _load("board", "release_gate_stale")
    entry = _rejected_entry(resolution={
        "kind": "pm-verified", "round_sequence": 1, "rounds": 1,
    })
    entry["rounds"].append({"sequence": 2, "verdict": 1, "must_fix": 1})
    problem = board._gate_disposition_problem(
        "T-0610", entry, allow_pm_verified=True, pm_verified_problem=lambda: None,
    )
    assert problem is not None and "새 라운드" in problem
