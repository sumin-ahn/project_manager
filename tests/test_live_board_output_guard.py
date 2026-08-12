"""테스트 세션의 live board 산출 오염 가드 자체를 임시 루트에서 검증한다."""
from __future__ import annotations

from pathlib import Path

import pytest

import conftest as test_config


def _make_board_shape(root: Path) -> dict[str, Path]:
    """wiki/board 분리 양쪽과 파생 board.md가 있는 최소 live board 형상."""
    paths = {
        "wiki-ticket": (
            root / ".project_manager" / "wiki" / "tickets" / "open"
            / "T-9001-fixture.md"
        ),
        "split-ticket": (
            root / ".project_manager" / "board" / "tickets" / "open"
            / "T-9002-fixture.md"
        ),
        "derived-board": root / ".project_manager" / "wiki" / "board.md",
    }
    paths["derived-board"].parent.mkdir(parents=True, exist_ok=True)
    paths["derived-board"].write_text("before\n", encoding="utf-8")
    return paths


@pytest.mark.parametrize(
    "mutation",
    ["wiki-ticket", "split-ticket", "derived-board"],
)
def test_live_board_output_guard_rejects_each_owned_surface(tmp_path, mutation):
    """wiki/split 티켓 신규와 board.md 변경은 모두 같은 가드에서 실패한다."""
    paths = _make_board_shape(tmp_path)
    before = test_config._snapshot_repo_board_outputs(tmp_path)
    target = paths[mutation]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "after\n" if mutation == "derived-board" else "---\nid: fixture\n---\n",
        encoding="utf-8",
    )
    after = test_config._snapshot_repo_board_outputs(tmp_path)

    with pytest.raises(AssertionError, match="live board 산출") as raised:
        test_config._assert_repo_board_outputs_unchanged(before, after)
    assert str(target) in str(raised.value)


def test_live_board_output_guard_accepts_unchanged_shape(tmp_path):
    """실제 감시 루트를 읽되 아무 산출도 바뀌지 않으면 가드는 통과한다."""
    _make_board_shape(tmp_path)
    before = test_config._snapshot_repo_board_outputs(tmp_path)
    after = test_config._snapshot_repo_board_outputs(tmp_path)

    test_config._assert_repo_board_outputs_unchanged(before, after)
    assert before == after
