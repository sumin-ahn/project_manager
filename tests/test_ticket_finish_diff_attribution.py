"""완료 기록의 코드 트리 해소와 [4/5] stage — 분리 PM 홈 + 슬롯 worktree 실측.

각 형상은 tmp 안에 실제 엔진(board.py 포함)과 실제 git 저장소를 만들며, board/diff 대역을
쓰지 않는다. 실 보드 상태는 읽거나 바꾸지 않는다.
"""
from __future__ import annotations

from pm_engine.infra import cluster_store


import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from _slot_seed import seed_slot
from conftest import bind_product_cluster, current_branch, write_cluster_ledger
from test_cluster_close import _review_round_text


REPO = Path(__file__).resolve().parents[1]
_TASK = "finish-task"  # 슬롯 작업 폴더를 보유한 task (T-0962 — 정상 종결의 정체성은 `--task`)
TOOLS = REPO / ".project_manager" / "tools"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )


def _write_claimed_ticket(root: Path, ticket_id: str, touches: list[str]) -> None:
    claimed = root / ".project_manager" / "wiki" / "tickets" / "claimed"
    claimed.mkdir(parents=True, exist_ok=True)
    touch_lines = "\n".join(f"- {touch}" for touch in touches)
    (claimed / f"{ticket_id}-shape.md").write_text(
        "---\n"
        f"id: {ticket_id}\n"
        f"title: {ticket_id} shape\n"
        "status: claimed\n"
        f"touches:\n{touch_lines}\n"
        "estimate: small\n"
        # 장부(`write_cluster_ledger`)와 명세의 귀속은 **양방향**이어야 한다 — 한쪽만 있으면
        # 실 `board complete` 가 첫 write 앞에서 거부하는 형상이다(board 대역이 가리던 자리).
        f"cluster: C-{ticket_id}\n"
        "---\n\n"
        "# hermetic shape\n\n"
        # 완료 기록 경로는 DoD 게이트를 지난다 — 마감된 DoD 를 심어 이 픽스처의 관측 축
        # (측정/stage 트리)만 남긴다.
        "## 완료 조건 (Definition of Done)\n- [x] 구현\n",
        encoding="utf-8",
    )
    # 종결은 멤버마다 돌려받은 리뷰를 요구한다 — 실 운영의 종결 전 형상(구현 · 통과 리뷰)을 심는다.
    rounds = root / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket_id
    rounds.mkdir(parents=True, exist_ok=True)
    (rounds / "01-architect.md").write_text("## 라운드 표본\n", encoding="utf-8")
    (rounds / "02-developer.md").write_text("## 구현 표본\n", encoding="utf-8")
    (rounds / "03-code-reviewer.md").write_text(_review_round_text([]), encoding="utf-8")


# ── 분리 PM 홈 + 슬롯 worktree — 회귀 skip 과 코드 트리 해소의 분리 ──────────
#
# `--no-pytest` 가 슬롯 해소까지 우회하던 동안, 측정 root 는 PM 홈으로 폴백했다. 분리 형상의
# PM 홈에는 엔진 import 사본이 있어 같은 좌표의 dirty 사본이 코드 몫으로 잡힐 수 있다. 아래
# 형상은 같은 호출에서 코드 트리가 슬롯 worktree 이고, [4/5] stage 가 코드 touches 를 그 트리에,
# PM 홈 산출물을 PM 홈에 싣는다는 것을 관측 가능한 결과(커밋·index)로 고정한다.


def _seed_git_tree(root: Path, files: dict[str, int], *,
                   extra_paths: tuple[str, ...] = (),
                   ledger_ticket: str | None = None) -> None:
    """root 를 실 git 저장소로 만들고 커밋한 뒤 파일마다 지정 줄 수의 미커밋 변경을 남긴다.

    `ledger_ticket` 을 주면 그 티켓의 묶음 장부를 첫 커밋 안에 함께 심는다 — 판정 기준(통합
    브랜치)의 소유자가 장부라, 선언이 없으면 완료 기록이 멈춘다.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "ticket-finish-test@example.invalid")
    _git(root, "config", "user.name", "ticket finish test")
    if ledger_ticket is not None:
        write_cluster_ledger(
            root / ".project_manager" / "wiki", ledger_ticket,
            base_branch=current_branch(root), branch=current_branch(root))
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
    code_files: list[str] | None = None,
):
    """분리 PM 홈(엔진 사본 보유)과 슬롯 worktree 를 각각 실 git 저장소로 만든다.

    두 트리가 같은 touches 파일을 갖고 서로 다른 크기의 미커밋 변경을 두므로, 측정 root 가
    어느 트리인지에 따라 diff 총량(=차단 여부)이 갈린다.

    `slot_target` 을 주면 worktree 를 PM 홈 **밖**에 만들고 슬롯 경로를 그 심링크로 둔다(슬롯이
    다른 마운트/심링크인 형상). `home_only` 는 PM 홈에만 사는 touches(wiki 산출물 등)와 그
    미커밋 줄 수다 — 코드 트리에는 만들지 않는다. `code_files` 는 두 트리에 만들 파일 이름이다 —
    touches 가 `work/<repo>_<N>/` 접두 형식일 때 선언 좌표와 파일 좌표를 따로 준다(미지정이면
    touches 그대로).

    슬롯은 대여 기록 행을 갖는다 — 종결은 그 행의 작업 폴더가 속한 저장소에서 묶음 기록의
    브랜치를 쥔 트리를 작업 트리로 찾는다. 슬롯 저장소가 그 브랜치를 쥐고(두 저장소 모두 같은
    기본 브랜치로 시작한다), PM 홈 자신은 찾는 곳이 아니라 같은 이름의 브랜치를 쥐어도 후보가
    아니다.
    """
    home_only = dict(home_only or {})
    shared = (list(code_files) if code_files is not None
              else [touch for touch in touches if touch not in home_only])
    home = tmp_path / "pm-home"
    tools = home / ".project_manager" / "tools"
    tools.parent.mkdir(parents=True)
    shutil.copytree(TOOLS, tools)
    slot_name = slot.rsplit("/", 1)[-1]
    seed_slot(home, slot=slot, repo=slot_name.rsplit("_", 1)[0], session=_TASK)
    _write_claimed_ticket(home, ticket_id, touches)
    log_dir = home / ".project_manager" / "wiki" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "current.md").write_text("# log\n", encoding="utf-8")
    home_files = {touch: home_lines for touch in shared}
    home_files.update(home_only)
    _seed_git_tree(home, home_files, extra_paths=(".project_manager",),
                   ledger_ticket=ticket_id)
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
    # 완료 파이프라인은 명시 제품 branch를 요구한다. 기존 명령으로 실제 결속하고,
    # 제품 통합 브랜치를 가진 별도 worktree를 준비한다 (측정·stage oracle은 그대로).
    board = module._board_module_at(tools / "board.py")
    base = current_branch(worktree)
    cluster = bind_product_cluster(board, ticket_id, repo="myrepo", number=1, task=_TASK)
    branch = cluster_store.load_cluster(board._home(), cluster)["branch"]
    _git(worktree, "checkout", "-q", branch)
    _git(worktree, "worktree", "add", "-q", str(tmp_path / "product-integration"), base)
    # 결속 준비의 명세·장부만 baseline에 넣는다. 홈 코드 사본과 산출물의 dirty는 보존한다.
    _git(home, "add", "--", ".project_manager/wiki/tickets")
    _git(home, "commit", "-qm", "product binding fixture", "--", ".project_manager/wiki/tickets")
    return home, worktree, module


def _probe_code_tree(tf, monkeypatch) -> list[Path]:
    """main() 이 만드는 finisher 의 코드 트리 해소값을 관찰한다.

    board.py subprocess 만 대역으로 막는다 — 이 형상이 검증하는 것은 measurement/stage 트리이고,
    기록 자체(claimed→done 이동)는 별도 테스트가 소유한다. git·stage 경로는 실물 그대로다.
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


def _head_paths(root: Path) -> list[str]:
    """root 의 마지막 커밋에 실린 경로들 (실 git 조회).

    완료 기록 단위는 묶음 하나라 [4/5] 가 stage 한 자리를 종결의 커밋 단계가 그 트리에서
    그대로 커밋한다 — 귀속 축(어느 트리가 그 경로를 맡는가)은 이제 이 자리에서 읽는다.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "show", "--name-only", "--format=", "HEAD"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return sorted(line for line in result.stdout.splitlines() if line)


def _ledger(ticket_id: str) -> str:
    """종결이 `closed` 를 쓰는 소속 장부 — board 가 홈 git 인 형상이라 홈 커밋에 실린다([[T-0959]]).

    장부 이름은 conftest 의 결속 픽스처가 짓는다(`bound-<티켓>`)."""
    return f".project_manager/wiki/tickets/clusters/C-bound-{ticket_id.lower()}.md"


def _commit_subjects(root: Path) -> list[str]:
    """root 의 커밋 제목들 (최신 순) — 새 커밋이 생겼는지를 값으로 본다."""
    result = subprocess.run(
        ["git", "-C", str(root), "log", "--format=%s"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return [line for line in result.stdout.splitlines() if line]


def test_no_pytest_stages_touches_in_slot_worktree_not_pm_home(tmp_path, monkeypatch, capsys):
    """--no-pytest + 명시 task → [4/5] 가 코드 touches 를 슬롯 worktree 에, log 산출물을 PM 홈에.

    PM 홈에도 같은 좌표의 사본이 dirty 하지만 stage 되지 않아야 한다 — 계획의 분기 축은 코드
    트리 위치이지 `--task` 유무가 아니다. [5/5] 커밋 안내도 같은 계획을 따라 repo별로 나온다.
    """
    home, worktree, tf = _split_home_shape(
        tmp_path, ticket_id="T-6703", touches=["src/engine.py"],
        home_lines=5, worktree_lines=10,
    )
    trees = _probe_code_tree(tf, monkeypatch)

    rc = tf.main(["T-6703", "--task", _TASK, "--no-pytest"])

    out = capsys.readouterr().out
    assert rc == 0
    assert trees and set(trees) == {worktree}
    assert _head_paths(worktree) == ["src/engine.py"]            # 코드는 worktree 에만
    assert _staged_paths(worktree) == []                         # 종결이 그 자리에서 커밋
    assert _head_paths(home) == [".project_manager/wiki/log/current.md", _ledger("T-6703")]  # 홈은 산출물만
    assert f"[PM 홈 산출물] cwd={home}" in out
    assert f"[slot worktree touches] cwd={worktree}" in out
    # 커밋은 손 안내가 아니라 종결 파이프라인이 실행한다(계획은 repo 별 그대로).
    assert "git commit — 종결 파이프라인이 실행한다" in out


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

    rc = tf.main(["T-6704", "--task", _TASK, "--no-pytest"])

    out = capsys.readouterr().out
    assert rc == 0                                   # 코드 트리를 본다(PM 홈 2000줄 사본 아님)
    assert trees and set(trees) == {worktree}
    assert _head_paths(worktree) == ["src/engine.py"]
    assert _head_paths(home) == [".project_manager/wiki/log/current.md", _ledger("T-6704")]
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

    rc = tf.main(["T-6705", "--task", _TASK, "--no-pytest"])

    out = capsys.readouterr().out
    assert rc == 0
    assert _head_paths(worktree) == ["src/engine.py"]
    assert _head_paths(home) == [
        ".project_manager/wiki/log/current.md", home_touch, _ledger("T-6705"),
    ]
    assert f"[PM 홈 산출물] cwd={home}" in out
    assert home_touch in out


def test_home_resident_touch_deletion_is_committed_in_pm_home(tmp_path, monkeypatch, capsys):
    """[[T-0959]] AT-006 — 홈-상주 touch 를 지운 상태로 완료하면 그 삭제가 PM 홈 커밋에 실린다.

    삭제된 경로는 어느 트리에도 실재하지 않는다 — 코드 몫으로만 접으면 코드 트리에서 추적되지
    않아 stage 필터가 떨구고 삭제가 어느 계획에도 안 실린다. 그 삭제는 이 종결이 커밋하는 홈
    변경이라 코드 경로(`src/engine.py`)의 변경이 없어도 선언 변경 판정을 통과한다.

    예전에는 [4/5] add 가 삭제를 index 에 반영해 커밋 단계가 다시 계산한 pathspec 에서 그 경로가
    빠졌고, 삭제는 홈 커밋 밖에 staged 로 남았다.
    """
    home_touch = ".project_manager/wiki/roadmap.md"
    home, worktree, tf = _split_home_shape(
        tmp_path, ticket_id="T-6706", touches=["src/engine.py", home_touch],
        home_lines=0, worktree_lines=0, home_only={home_touch: 0},
    )
    (home / home_touch).unlink()
    _probe_code_tree(tf, monkeypatch)

    rc = tf.main(["T-6706", "--task", _TASK, "--no-pytest"])

    assert rc == 0
    assert capsys.readouterr() is not None
    assert _staged_paths(home) == []                             # staged 삭제로 남지 않는다
    assert home_touch in _head_paths(home)                       # 삭제는 홈 커밋에 실린다
    tracked = subprocess.run(["git", "-C", str(home), "ls-files"], check=True, capture_output=True,
                             text=True, encoding="utf-8").stdout.splitlines()
    assert home_touch not in tracked
    assert _staged_paths(worktree) == []
    assert _commit_subjects(worktree) == ["seed"]                # 코드 트리엔 변경 자체가 없다


def test_symlinked_slot_with_prefixed_touch_uses_code_tree(tmp_path, monkeypatch, capsys):
    """심링크 슬롯 + `work/<repo>_<N>/` 접두 touch — 측정과 stage 가 외부 코드 트리를 쓴다.

    접두 형식은 좌표 normalizer 를 타는데, slot 도출이 실체 경로만 보면 심링크 슬롯이 PM 홈
    밖이라 정규화가 실패한다 — 측정은 skip, stage 는 빈 scope 가 돼 이 형상만 조용히 가드 밖으로
    빠진다. 상대 표기 touch 는 normalizer 를 타지 않아 이 갈래를 덮지 못한다.
    """
    home, worktree, tf = _split_home_shape(
        tmp_path, ticket_id="T-6707",
        touches=["work/myrepo_1/src/engine.py"], code_files=["src/engine.py"],
        home_lines=2000, worktree_lines=10,
        slot_target=tmp_path / "outside-slot",
    )
    trees = _probe_code_tree(tf, monkeypatch)

    rc = tf.main(["T-6707", "--task", _TASK, "--no-pytest"])

    err = capsys.readouterr().err
    assert rc == 0                                   # 코드 트리를 본다(PM 홈 2000줄 사본 아님)
    assert trees and set(trees) == {worktree}
    assert "좌표 정규화 실패" not in err
    assert _head_paths(worktree) == ["src/engine.py"]
    assert _head_paths(home) == [".project_manager/wiki/log/current.md", _ledger("T-6707")]
