"""T-0659 — ticket_finish diff 서킷브레이커의 claimed-ticket 귀속 실측.

각 형상은 tmp 안에 실제 엔진(board.py 포함)과 실제 git 저장소를 만들며, board/diff 대역을
쓰지 않는다. 실 보드 상태는 읽거나 바꾸지 않는다.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )


def _write_claimed_ticket(
    root: Path, ticket_id: str, touches: list[str], *, estimate: str = "small",
) -> None:
    claimed = root / ".project_manager" / "wiki" / "tickets" / "claimed"
    claimed.mkdir(parents=True, exist_ok=True)
    touch_lines = "\n".join(f"- {touch}" for touch in touches)
    (claimed / f"{ticket_id}-shape.md").write_text(
        "---\n"
        f"id: {ticket_id}\n"
        f"title: {ticket_id} shape\n"
        "status: claimed\n"
        f"touches:\n{touch_lines}\n"
        f"estimate: {estimate}\n"
        "---\n\n"
        "# hermetic shape\n",
        encoding="utf-8",
    )


def _shape(
    tmp_path: Path, tickets: dict[str, list[str]], files: dict[str, int],
):
    """실 board 엔진 + 실 claimed 티켓 + 실 tracked working-tree diff 를 가진 tmp repo."""
    root = tmp_path / "repo"
    tools = root / ".project_manager" / "tools"
    tools.parent.mkdir(parents=True)
    shutil.copytree(TOOLS, tools)
    for ticket_id, touches in tickets.items():
        _write_claimed_ticket(root, ticket_id, touches)

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "ticket-finish-test@example.invalid")
    _git(root, "config", "user.name", "ticket finish test")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for relative in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    # board/engine도 baseline 에 넣어 넓은 `.project_manager/` 공격 셀이 테스트 하네스 복사본을
    # 미커밋 작업물로 오인하지 않게 한다. 이후 diff 는 `files`의 실 tracked 변경뿐이다.
    _git(root, "add", "seed.txt", ".project_manager", *files)
    _git(root, "commit", "-q", "-m", "seed")
    for relative, lines in files.items():
        target = root / relative
        target.write_text("x\n" * lines, encoding="utf-8")

    module_path = tools / "ticket_finish.py"
    spec = importlib.util.spec_from_file_location(
        f"ticket_finish_t0659_{tmp_path.name}", module_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return root, module


def _measurement(root: Path, tf, ticket_id: str):
    finisher = tf.TicketFinisher(
        board_py=root / ".project_manager" / "tools" / "board.py",
        regression_cwd=root,
        log_file=root / "log.md",
    )
    external = tf._load_external_review()
    assert external is not None
    touches = finisher._measured_touches(ticket_id)
    assert touches is not None
    return finisher, external, finisher._ticket_diff_attribution(
        ticket_id, external, touches,
    )


def _rename_shape(
    tmp_path: Path, *, edited: bool, current_touches: list[str],
    other_touches: list[str],
):
    """50줄 tracked 파일을 staged rename하고 선택적으로 10줄 추가한다."""
    root, tf = _shape(
        tmp_path,
        {"T-6601": current_touches, "T-6602": other_touches},
        {"src/old.py": 50},
    )
    _git(root, "add", "src/old.py")
    _git(root, "commit", "-q", "-m", "50-line source")
    (root / "dst").mkdir()
    _git(root, "mv", "src/old.py", "dst/new.py")
    if edited:
        with (root / "dst/new.py").open("a", encoding="utf-8") as stream:
            stream.write("edited\n" * 10)
    _git(root, "add", "dst/new.py")
    return root, tf


def test_shape_a_two_claimed_tickets_measure_only_their_files(tmp_path):
    """형상 A: 서로 겹치지 않는 두 파일은 claimed 합집합 한 번에서 각 티켓 몫으로 갈린다."""
    root, tf = _shape(
        tmp_path,
        {
            "T-1001": ["src/a.py"],
            "T-1002": ["src/b.py"],
        },
        {"src/a.py": 11, "src/b.py": 17},
    )

    _finisher_a, _external_a, measured_a = _measurement(root, tf, "T-1001")
    _finisher_b, _external_b, measured_b = _measurement(root, tf, "T-1002")

    assert measured_a == tf.DiffAttribution(11, 17, ("T-1002",))
    assert measured_b == tf.DiffAttribution(17, 11, ("T-1001",))


def test_shape_b_same_file_overlap_is_not_excluded(tmp_path):
    """형상 B: 같은 파일의 동률 touches 는 양쪽 모두에서 남아 가드가 무력화되지 않는다."""
    root, tf = _shape(
        tmp_path,
        {
            "T-2001": ["src/shared.py"],
            "T-2002": ["src/shared.py"],
        },
        {"src/shared.py": 13},
    )

    _finisher_a, _external_a, measured_a = _measurement(root, tf, "T-2001")
    _finisher_b, _external_b, measured_b = _measurement(root, tf, "T-2002")

    assert measured_a == tf.DiffAttribution(13, 0, ())
    assert measured_b == tf.DiffAttribution(13, 0, ())


@pytest.mark.parametrize("edited", [False, True], ids=["exact-rename", "edited-rename"])
@pytest.mark.parametrize(
    ("ownership", "current_touches", "other_touches"),
    [
        pytest.param("source-only", ["src/old.py"], ["dst/new.py"], id="source-only"),
        pytest.param("destination-only", ["dst/new.py"], ["src/old.py"],
                     id="destination-only"),
        pytest.param("both", ["src/old.py", "dst/new.py"], [], id="both"),
        pytest.param("neither", ["keep/current.py"], ["src/old.py", "dst/new.py"],
                     id="neither"),
    ],
)
def test_staged_rename_uses_source_and_destination_for_attribution(
    tmp_path, edited, ownership, current_touches, other_touches,
):
    """rename 8셀: 어느 endpoint든 현재 ticket이 claim하면 타 티켓 몫으로 빼지 않는다."""
    root, tf = _rename_shape(
        tmp_path, edited=edited, current_touches=current_touches,
        other_touches=other_touches,
    )
    finisher = tf.TicketFinisher(
        board_py=root / ".project_manager/tools/board.py",
        regression_cwd=root,
        log_file=root / "log.md",
    )
    external = tf._load_external_review()
    assert external is not None
    touches = finisher._measured_touches("T-6601")
    assert touches is not None

    original = external.diff_line_total(root, "HEAD", touches)
    measured = finisher._ticket_diff_attribution("T-6601", external, touches)

    if ownership == "source-only":
        expected = (50, 50, 0, ())
    elif ownership == "destination-only":
        destination_lines = 60 if edited else 50
        expected = (destination_lines, destination_lines, 0, ())
    elif ownership == "both":
        delta = 10 if edited else 0
        expected = (delta, delta, 0, ())
    else:
        excluded = 10 if edited else 0
        expected = (0, 0, excluded, ("T-6602",) if edited else ())
    assert (original, measured.total, measured.excluded_total,
            measured.excluded_ticket_ids) == expected


def test_shape_c_single_claimed_ticket_keeps_the_original_total(tmp_path):
    """형상 C: claimed 가 자기 하나면 귀속 보정값은 기존 diff_line_total 과 같다."""
    root, tf = _shape(
        tmp_path,
        {"T-3001": ["src/"]},
        {"src/a.py": 11, "src/b.py": 17},
    )

    _finisher, external, measured = _measurement(root, tf, "T-3001")
    original = external.diff_line_total(root, "HEAD", ["src/"])

    assert original == 28
    assert measured == tf.DiffAttribution(original, 0, ())


def test_shape_d_block_reports_excluded_amount_and_ticket_id(tmp_path):
    """형상 D: 타 티켓 25줄을 뺀 뒤 320>300이면 차단하고 제외 근거도 싣는다."""
    root, tf = _shape(
        tmp_path,
        {
            "T-4001": ["src/a.py"],
            "T-4002": ["src/b.py"],
        },
        {"src/a.py": 320, "src/b.py": 25},
    )

    finisher, _external, measured = _measurement(root, tf, "T-4001")
    block = finisher._default_diff_cap_block("T-4001")

    assert measured == tf.DiffAttribution(320, 25, ("T-4002",))
    assert block is not None
    assert "diff 320줄 > 상한 300줄" in block
    assert "타 claimed 티켓 귀속 제외: 25줄" in block
    assert "티켓 T-4002" in block


@pytest.mark.parametrize(
    ("current_touches", "other_touches", "changed_path"),
    [
        pytest.param(
            ["tests/"], ["tests/test_shared.py"], "tests/test_shared.py",
            id="current-tests-dir-other-specific-file",
        ),
        pytest.param(
            [".project_manager/tools/current.py"], [".project_manager/"],
            ".project_manager/tools/current.py", id="other-project-manager-dir",
        ),
        pytest.param(
            ["tests/test_current.py"], ["tests/"], "tests/test_current.py",
            id="other-tests-dir",
        ),
    ],
)
def test_attack_matrix_overlap_is_never_excluded(
    tmp_path, current_touches, other_touches, changed_path,
):
    """공격 매트릭스: 디렉터리/하위파일 어느 방향의 겹침도 깊이 우선으로 빼지 않는다."""
    root, tf = _shape(
        tmp_path,
        {"T-5001": current_touches, "T-5002": other_touches},
        {changed_path: 19},
    )

    _finisher, _external, measured = _measurement(root, tf, "T-5001")

    assert measured == tf.DiffAttribution(19, 0, ())


def test_claimed_board_parse_failure_is_loud_and_keeps_the_breaker(
    tmp_path, capsys,
):
    """claimed 한 파일이 깨지면 보정만 skip 하고 현재 320줄 상한 판정은 계속한다."""
    root, tf = _shape(
        tmp_path,
        {"T-6001": ["src/current.py"]},
        {"src/current.py": 320},
    )
    claimed = root / ".project_manager" / "wiki" / "tickets" / "claimed"
    (claimed / "T-6002-broken.md").write_text(
        "---\nid: [unterminated\n---\n", encoding="utf-8",
    )
    finisher = tf.TicketFinisher(
        board_py=root / ".project_manager" / "tools" / "board.py",
        regression_cwd=root,
        log_file=root / "log.md",
    )

    block = finisher._default_diff_cap_block("T-6001")
    err = capsys.readouterr().err

    assert block is not None and "diff 320줄 > 상한 300줄" in block
    assert "claimed 보드 읽기 실패" in err
    assert "타 티켓 제외 없이 현재 touches 전체를 측정합니다" in err


@pytest.mark.parametrize("failure", ["syntax", "read"], ids=["syntax-error", "read-error"])
def test_board_module_failure_is_loud_and_keeps_the_breaker(
    tmp_path, capsys, failure,
):
    """board.py 자체 syntax/read 실패도 최소 입력 복구 후 같은 경고와 320줄 차단을 유지한다."""
    root, tf = _shape(
        tmp_path,
        {"T-6051": ["src/current.py"]},
        {"src/current.py": 320},
    )
    board_py = root / ".project_manager/tools/board.py"
    if failure == "syntax":
        board_py.write_text("def broken(:\n", encoding="utf-8")
    else:
        board_py = root / ".project_manager/tools/missing_board.py"
    finisher = tf.TicketFinisher(
        board_py=board_py,
        regression_cwd=root,
        log_file=root / "log.md",
    )

    block = finisher._default_diff_cap_block("T-6051")
    err = capsys.readouterr().err

    assert block is not None and "diff 320줄 > 상한 300줄" in block
    assert "⚠ diff 서킷브레이커 귀속 보정 skip — claimed 보드 읽기 실패" in err
    assert "타 티켓 제외 없이 현재 touches 전체를 측정합니다" in err


def test_claimed_empty_touches_is_not_reported_as_board_failure(tmp_path, capsys):
    """정상적인 빈 touches 는 제외 대상 없음이며 board 실패 경고를 내지 않는다."""
    root, tf = _shape(
        tmp_path,
        {"T-6101": ["src/current.py"], "T-6102": []},
        {"src/current.py": 23},
    )

    _finisher, _external, measured = _measurement(root, tf, "T-6101")

    assert measured == tf.DiffAttribution(23, 0, ())
    assert "claimed 보드 읽기 실패" not in capsys.readouterr().err


@pytest.mark.parametrize("claimed_count", [1, 5, 20])
def test_git_call_count_does_not_scale_with_claimed_ticket_count(
    tmp_path, claimed_count,
):
    """claimed 수와 무관하게 경로별 numstat 한 벌(실 git 3호출)만 소비한다."""
    tickets = {
        f"T-{7000 + index}": [f"src/ticket_{index}.py"]
        for index in range(claimed_count)
    }
    files = {f"src/ticket_{index}.py": 1 for index in range(claimed_count)}
    root, tf = _shape(tmp_path, tickets, files)
    ticket_id = "T-7000"
    finisher = tf.TicketFinisher(
        board_py=root / ".project_manager" / "tools" / "board.py",
        regression_cwd=root,
        log_file=root / "log.md",
    )
    external = tf._load_external_review()
    touches = finisher._measured_touches(ticket_id)
    assert external is not None and touches is not None
    git_calls: list[list[str]] = []

    def counting_real_git(args, **kwargs):
        git_calls.append(list(args))
        return subprocess.run(args, **kwargs)

    measured = finisher._ticket_diff_attribution(
        ticket_id, external, touches, run_fn=counting_real_git,
    )

    assert measured.total == 1
    assert measured.excluded_total == claimed_count - 1
    assert len(git_calls) == 3
