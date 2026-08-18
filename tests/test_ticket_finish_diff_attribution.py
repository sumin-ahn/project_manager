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


# ── 분리 PM 홈 + 슬롯 worktree — 회귀 skip 과 코드 트리 해소의 분리 ──────────
#
# `--no-pytest` 가 슬롯 해소까지 우회하던 동안, 측정 root 는 PM 홈으로 폴백했다. 분리 형상의
# PM 홈에는 엔진 import 사본이 있어 그 사본이 dirty 하면 상한을 훌쩍 넘고, 코드 트리(worktree)가
# clean 이어도 완료 부기가 false-block 됐다. 아래 두 형상은 같은 호출에서 측정 root 가 슬롯
# worktree 라는 것을 차단/통과라는 관측 가능한 결과로 고정한다.


def _seed_git_tree(root: Path, files: dict[str, int], *,
                   extra_paths: tuple[str, ...] = ()) -> None:
    """root 를 실 git 저장소로 만들고 커밋한 뒤 파일마다 지정 줄 수의 미커밋 변경을 남긴다."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "ticket-finish-test@example.invalid")
    _git(root, "config", "user.name", "ticket finish test")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for relative in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    _git(root, "add", "seed.txt", *extra_paths, *files)
    _git(root, "commit", "-q", "-m", "seed")
    for relative, lines in files.items():
        (root / relative).write_text("x\n" * lines, encoding="utf-8")


def _split_home_shape(
    tmp_path: Path, *, ticket_id: str, touches: list[str],
    home_lines: int, worktree_lines: int, slot: str = "work/myrepo_1",
    slot_target: Path | None = None, home_only: dict[str, int] | None = None,
):
    """분리 PM 홈(엔진 사본 보유)과 슬롯 worktree 를 각각 실 git 저장소로 만든다.

    두 트리가 같은 touches 파일을 갖고 서로 다른 크기의 미커밋 변경을 두므로, 측정 root 가
    어느 트리인지에 따라 diff 총량(=차단 여부)이 갈린다.

    `slot_target` 을 주면 worktree 를 PM 홈 **밖**에 만들고 슬롯 경로를 그 심링크로 둔다(슬롯이
    다른 마운트/심링크인 형상). `home_only` 는 PM 홈에만 사는 touches(wiki 산출물 등)와 그
    미커밋 줄 수다 — 코드 트리에는 만들지 않는다.
    """
    home_only = dict(home_only or {})
    shared = [touch for touch in touches if touch not in home_only]
    home = tmp_path / "pm-home"
    tools = home / ".project_manager" / "tools"
    tools.parent.mkdir(parents=True)
    shutil.copytree(TOOLS, tools)
    _write_claimed_ticket(home, ticket_id, touches)
    log_dir = home / ".project_manager" / "wiki" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "current.md").write_text("# log\n", encoding="utf-8")
    home_files = {touch: home_lines for touch in shared}
    home_files.update(home_only)
    _seed_git_tree(home, home_files, extra_paths=(".project_manager",))
    worktree = home / slot
    if slot_target is None:
        _seed_git_tree(worktree, {touch: worktree_lines for touch in shared})
    else:
        _seed_git_tree(slot_target, {touch: worktree_lines for touch in shared})
        worktree.parent.mkdir(parents=True, exist_ok=True)
        try:
            worktree.symlink_to(slot_target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:   # 권한/플랫폼 미지원
            pytest.skip(f"심링크 슬롯을 만들 수 없다 ({exc})")

    spec = importlib.util.spec_from_file_location(
        f"ticket_finish_split_{tmp_path.name}", tools / "ticket_finish.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return home, worktree, module


def _probe_code_tree(tf, monkeypatch) -> list[Path]:
    """main() 이 만드는 finisher 의 코드 트리 해소값을 관찰한다.

    board.py subprocess 만 대역으로 막는다 — 이 형상이 검증하는 것은 measurement/stage 트리이고,
    부기 자체(claimed→done 이동)는 별도 테스트가 소유한다. git·stage 경로는 실물 그대로다.
    """
    trees: list[Path] = []
    original = tf.TicketFinisher

    class _CodeTreeProbe(original):
        def _code_tree(self):
            tree = super()._code_tree()
            trees.append(tree)
            return tree

        def _default_run_board(self, args):
            return 0, "board ok"

    monkeypatch.setattr(tf, "TicketFinisher", _CodeTreeProbe)
    return trees


def _staged_paths(root: Path) -> list[str]:
    """root 의 index 에 올라간 경로들 (실 git 조회)."""
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--name-only"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return sorted(line for line in result.stdout.splitlines() if line)


def test_no_pytest_measures_slot_worktree_not_pm_home(tmp_path, monkeypatch, capsys):
    """--no-pytest + 명시 슬롯 → PM 홈 사본이 상한을 넘게 dirty 해도 clean worktree 를 재 통과.

    옛 규칙(회귀 skip = 슬롯 해소 skip)에서는 측정 root 가 PM 홈으로 폴백해 이 형상이
    false-block 이었다.
    """
    _home, worktree, tf = _split_home_shape(
        tmp_path, ticket_id="T-6701", touches=["src/engine.py"],
        home_lines=2000, worktree_lines=0,
    )
    trees = _probe_code_tree(tf, monkeypatch)

    rc = tf.main(
        ["T-6701", "--repo", "myrepo", "--slot", "1", "--no-pytest", "--dry-run"]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert trees and set(trees) == {worktree}   # PM 홈이 아니라 슬롯 worktree 를 잰다
    assert "diff 서킷브레이커 차단" not in captured.err
    assert "회귀 측정 skip" in captured.out      # [1/5] 만 skip


def test_no_pytest_blocks_when_slot_worktree_exceeds_cap(tmp_path, monkeypatch, capsys):
    """반대 형상 — worktree 가 상한 초과면 PM 홈이 clean 이어도 차단된다(가드 유지)."""
    _home, worktree, tf = _split_home_shape(
        tmp_path, ticket_id="T-6702", touches=["src/engine.py"],
        home_lines=0, worktree_lines=2000,
    )
    trees = _probe_code_tree(tf, monkeypatch)

    rc = tf.main(
        ["T-6702", "--repo", "myrepo", "--slot", "1", "--no-pytest", "--dry-run"]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert trees and set(trees) == {worktree}
    assert "diff 서킷브레이커 차단" in captured.err
    assert "diff 2000줄 > 상한 300줄" in captured.err


def test_no_pytest_stages_touches_in_slot_worktree_not_pm_home(tmp_path, monkeypatch, capsys):
    """--no-pytest + 명시 슬롯 → [4/5] 가 코드 touches 를 슬롯 worktree 에, log 산출물을 PM 홈에.

    PM 홈에도 같은 좌표의 사본이 dirty 하지만 stage 되지 않아야 한다 — 계획의 분기 축은 코드
    트리 위치이지 `--task` 유무가 아니다. [5/5] 커밋 안내도 같은 계획을 따라 repo별로 나온다.
    """
    home, worktree, tf = _split_home_shape(
        tmp_path, ticket_id="T-6703", touches=["src/engine.py"],
        home_lines=5, worktree_lines=10,
    )
    trees = _probe_code_tree(tf, monkeypatch)

    rc = tf.main(["T-6703", "--repo", "myrepo", "--slot", "1", "--no-pytest"])

    out = capsys.readouterr().out
    assert rc == 0
    assert trees and set(trees) == {worktree}
    assert _staged_paths(worktree) == ["src/engine.py"]          # 코드는 worktree 에만
    assert _staged_paths(home) == [".project_manager/wiki/log/current.md"]  # 홈은 산출물만
    assert f"[PM 홈 산출물] cwd={home}" in out
    assert f"[slot worktree touches] cwd={worktree}" in out
    assert (f"[slot worktree touches] cwd={worktree}: "
            '`git commit -m "<메시지>" -- src/engine.py`') in out


def test_symlinked_slot_keeps_measurement_and_stage_on_one_tree(
        tmp_path, monkeypatch, capsys):
    """슬롯 worktree 가 PM 홈 밖(심링크)이어도 측정과 stage 가 같은 트리를 본다.

    위치로 분리 여부를 판정하면(PM 홈 하위인가) 이 형상에서 diff 는 슬롯을, stage 는 PM 홈을
    보게 돼 한 값의 두 소비자가 갈린다 — PM 홈의 dirty 한 동명 사본이 다시 stage 된다.
    """
    home, worktree, tf = _split_home_shape(
        tmp_path, ticket_id="T-6704", touches=["src/engine.py"],
        home_lines=2000, worktree_lines=10,
        slot_target=tmp_path / "outside-slot",
    )
    trees = _probe_code_tree(tf, monkeypatch)

    rc = tf.main(["T-6704", "--repo", "myrepo", "--slot", "1", "--no-pytest"])

    out = capsys.readouterr().out
    assert rc == 0                                   # 측정은 코드 트리(10줄·상한 이내)
    assert trees and set(trees) == {worktree}
    assert _staged_paths(worktree) == ["src/engine.py"]
    assert _staged_paths(home) == [".project_manager/wiki/log/current.md"]
    assert f"[slot worktree touches] cwd={worktree}" in out


def test_home_resident_touch_is_staged_in_pm_home(tmp_path, monkeypatch, capsys):
    """PM 홈에만 사는 touch 는 홈 계획이 stage 하고, 코드 touch 는 코드 트리가 stage 한다.

    두 계획이 각자 몫만 맡으므로 홈-상주 산출물이 어느 repo 에도 안 실리는 구멍이 없다. 홈에
    있는 코드 파일 사본은 코드 몫이라 홈에서 stage 되지 않는다.
    """
    home_touch = ".project_manager/wiki/roadmap.md"
    home, worktree, tf = _split_home_shape(
        tmp_path, ticket_id="T-6705", touches=["src/engine.py", home_touch],
        home_lines=5, worktree_lines=10, home_only={home_touch: 20},
    )
    _probe_code_tree(tf, monkeypatch)

    rc = tf.main(["T-6705", "--repo", "myrepo", "--slot", "1", "--no-pytest"])

    out = capsys.readouterr().out
    assert rc == 0
    assert _staged_paths(worktree) == ["src/engine.py"]
    assert _staged_paths(home) == [
        ".project_manager/wiki/log/current.md", home_touch,
    ]
    assert f'[PM 홈 산출물] cwd={home}: `git commit -m "<메시지>" --' in out
    assert home_touch in out
