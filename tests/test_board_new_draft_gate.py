"""`board.py new` 발행 규율 게이트 — 미충전 stub 은 board-git 미커밋(draft) (T-0196).

board(tickets+areas)가 별도 git 으로 분리된(공유) 형상에서, `board.py new` 가 방금 만든
티켓 본문이 아직 `_template.md` placeholder(무엇을 만들/바꿀/검증할지 · [[xxxxx]] 등)를
그대로 담고 있으면(=기본값 그대로, 제목만 바뀜) **board-git 에 커밋하지 않는다** — draft 는
로컬 파일시스템(open/)엔 존재하되 board-git 엔 없어, 다른 slot 의 pull/handoff 에 나타나지
않는다(공유 board 오염 방지 — T-0191/T-0192 의 stub-committed 실패를 원천 차단).

본문을 채운 뒤 `board.py promote <id>` 로 승격(board-git commit) — 여전히 미충전이면 거부.

board 가 별도 git 이 아니면(legacy·솔로) 게이트 자체가 무의미(공유 board 가 없음) — 항상
즉시 sync(기존 무변경).

hermetic 패턴은 `test_board_git_sync.py` 와 동형 — 실 board git + bare remote 를 tmp 에
세우고 board 모듈의 `REPO` 를 그 tmp 로 monkeypatch 한다.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=False)


# 실 `_template.md` 와 동형(placeholder 그대로) — `board.py new` 가 이 골격에 제목만 채워
# 발행한다는 전제를 hermetic 하게 모사한다.
_TEMPLATE_TEXT = (
    "---\n"
    "id: T-NNNN\n"
    "title: <제목>\n"
    "status: open\n"
    "created_by:\n"
    "claimed_by:\n"
    "claimed_at:\n"
    "completed_at:\n"
    "depends_on: []\n"
    "blocks: []\n"
    "touches: []\n"
    "estimate: small\n"
    "tags: []\n"
    "---\n\n"
    "# T-NNNN — <제목>\n\n"
    "## 목표\n무엇을 만들 / 바꿀 / 검증할지 1~3 문장.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 핵심 산출물 (파일, 동작)\n\n"
    "## 참고\n- 관련 ADR / spec: [[xxxxx]]\n\n"
    "## 메모\n"
)


def _make_board_git(root: Path, *, remote: Path) -> Path:
    """`<root>/.project_manager/board/` 에 실 board git(tickets/ + _template.md + remote) 을 만든다."""
    board = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board / "tickets" / "_template.md").write_text(_TEMPLATE_TEXT, encoding="utf-8")
    _git(["init", "-q", "-b", "main"], board)
    _git(["remote", "add", "origin", str(remote)], board)
    _git(["add", "-A"], board)
    _git(["commit", "-qm", "board init"], board)
    _git(["push", "-q", "-u", "origin", "main"], board)
    return board


@pytest.fixture
def board(tmp_path, monkeypatch):
    mod = _load_tool("board")
    anchor_board_module(mod, tmp_path, monkeypatch)
    mod.BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    mod.BOARD_FILE.touch()
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    mod._tmp = tmp_path
    return mod


def _new_args(title: str) -> argparse.Namespace:
    return argparse.Namespace(title=title, touches=None, depends=None, tag=None,
                              estimate="small", prefix=None, user=None, session=None)


# ════════════════════════════════════════════════════════════════════════
# board-git 활성 — 미충전 draft 는 board-git 미커밋
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_new_placeholder_body_not_committed_to_board_git(board, tmp_path, capsys):
    """제목만 채운 기본 발행(placeholder 그대로) → 파일은 drafts_dir() 에 있으나 board-git 미커밋(T-0198).

    draft 는 이제 `tickets/open/` 이 아니라 `tickets/.drafts/`(STATUS_DIRS 밖)에 쓰인다 —
    board-git 이 커밋하는 대상(STATUS_DIRS)에 draft 가 물리적으로 존재하지 않아야 leak 이
    구조적으로 불가능하다(격리 방향 A)."""
    bare = tmp_path / "bare"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    rc = board.cmd_new(_new_args("어떤 제목"))
    assert rc == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err

    open_files = list((board_dir / "tickets" / "open").glob("T-*-*.md"))
    assert not open_files, "draft 가 STATUS_DIRS 대상인 open/ 에 있으면 안 된다(격리 위반)."
    draft_files = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))
    assert draft_files, "draft 는 drafts_dir()(tickets/.drafts/)엔 존재해야 한다."

    # 첫 mutation 이 draft 여도 sync funnel 이 루트 ignore backfill 을 즉시 commit/push 한다.
    # draft 파일은 계속 commit 대상이 아니지만 이제 일반 status/handoff 관측에서도 사라져야 한다.
    status = _git(["status", "--porcelain"], board_dir).stdout
    assert status.strip() == "", f"첫 draft 생성 뒤 dirty 잔여가 남음: {status!r}"
    head_files = _git(["ls-tree", "-r", "--name-only", "HEAD"], board_dir).stdout
    assert ".gitignore" in head_files
    assert "tickets/.drafts/" not in head_files, "ignore backfill 커밋에 draft 자체가 유출됨."
    remote_files = _git(["ls-tree", "-r", "--name-only", "main"], bare).stdout
    assert ".gitignore" in remote_files, "첫 draft 경로에서 ignore backfill 이 push 되지 않음."

    # dirty *판정용* 헬퍼(`_board_git_status_porcelain` — claim prefetch 가 쓴다)는 draft 를
    # pathspec exclude 하므로 clean 으로 봐야 한다(무관 claim 이 draft 때문에 막히면 안 됨).
    assert not board._board_git_status_porcelain().strip(), (
        "_board_git_status_porcelain 이 draft 를 dirty 로 오판함 — 무관 claim 이 막힐 위험.")

    handoff = _load_tool("pm_handoff")
    assert handoff._dirty_paths_in_tree(str(board_dir), handoff._module_run_git) == [], \
        "cmd_new→draft 뒤 handoff 실제 dirty seam 이 draft 잔여를 검출함."

    log = _git(["log", "--oneline"], board_dir).stdout
    assert "board init" in log
    assert len(log.strip().splitlines()) == 2, "첫 draft 의 루트 backfill 커밋이 정확히 1개여야 한다."


@requires_git
def test_first_draft_root_backfill_commit_failure_is_loud_and_pending(
        board, tmp_path, capsys):
    """빈 draft pathspec이어도 실제 작성한 루트 파일의 commit 실패를 성공으로 숨기지 않는다."""
    bare = tmp_path / "bare-first-draft-commit-fail"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    hook = board_dir / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    assert board.cmd_new(_new_args("commit 실패 draft")) == 0

    captured = capsys.readouterr()
    err = captured.err
    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == head_before
    status = _git([
        "status", "--porcelain", "--", ".gitattributes", ".gitignore",
    ], board_dir).stdout
    assert "A  .gitattributes" in status and "A  .gitignore" in status, status
    assert "board local commit 실패" in err, err
    assert ".gitattributes" in err and ".gitignore" in err, err
    assert "draft 격리 기록 보류: local-only/uncommitted" in err, err
    remote_files = _git(["ls-tree", "-r", "--name-only", "main"], bare).stdout
    assert ".gitattributes" not in remote_files and ".gitignore" not in remote_files


@requires_git
def test_second_draft_with_deployed_root_rules_never_contacts_remote(
        board, tmp_path, monkeypatch, capsys):
    """보강할 루트 규칙이 없으면 두 번째 draft는 pull/push sync funnel을 열지 않는다."""
    bare = tmp_path / "bare-noop-draft"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    assert board.cmd_new(_new_args("첫 번째")) == 0
    capsys.readouterr()
    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    remote_calls: list[str] = []

    def _unexpected_remote(op: str):
        def _call():
            remote_calls.append(op)
            raise AssertionError(f"두 번째 draft가 원격 {op}를 호출함")
        return _call

    monkeypatch.setattr(board, "_board_git_pull_rebase", _unexpected_remote("pull"))
    monkeypatch.setattr(board, "_board_git_push", _unexpected_remote("push"))

    assert board.cmd_new(_new_args("두 번째")) == 0

    assert remote_calls == []
    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == head_before
    assert len(list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))) == 2
    assert "board sync 보류" not in capsys.readouterr().err


@requires_git
def test_union_only_board_first_draft_backfills_markdown_lf(
        board, tmp_path):
    """union-only attrs도 draft-only sync를 열어 새 Markdown LF 블록을 배포한다."""
    bare = tmp_path / "bare-union-only-draft"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    attrs = board_dir / ".gitattributes"
    ignore = board_dir / ".gitignore"
    attrs.write_bytes(b"areas.md merge=union\n")
    ignore.write_bytes(b"tickets/.drafts/\n")
    _git(["add", "--", ".gitattributes", ".gitignore"], board_dir)
    _git(["commit", "-qm", "seed legacy root rules"], board_dir)
    _git(["push", "-q", "origin", "main"], board_dir)

    assert board._board_git_root_files_need_backfill() is True
    assert board.cmd_new(_new_args("LF backfill draft")) == 0

    local = _git(["show", "HEAD:.gitattributes"], board_dir).stdout
    remote = _git(["show", "main:.gitattributes"], bare).stdout
    assert "*.md text eol=lf" in local
    assert remote == local
    assert board._board_git_root_files_need_backfill() is False


@requires_git
def test_promote_rejects_still_placeholder(board, tmp_path):
    """본문이 여전히 미충전이면 `promote` 가 거부(rc=1)한다(파일은 drafts_dir() 에 잔류)."""
    bare = tmp_path / "bare2"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    board.cmd_new(_new_args("제목"))
    tid = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0].name.split("-", 2)
    ticket_id = f"{tid[0]}-{tid[1]}"
    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()

    rc = board.cmd_promote(argparse.Namespace(id=ticket_id))
    assert rc == 1
    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == head_before, \
        "거부된 promote 가 추가 커밋을 남기면 안 된다."
    assert list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "거부된 promote 후 draft 파일이 drafts_dir() 에 남아있어야 한다(이동 없음)."
    assert not list((board_dir / "tickets" / "open").glob("T-*-*.md")), \
        "거부된 promote 인데 draft 가 open/ 으로 이동됨."


@requires_git
def test_promote_commits_when_body_filled(board, tmp_path):
    """본문을 채운 뒤 `promote` 하면 drafts_dir() → open/ 로 이동 + board-git 에 커밋(승격 성공)."""
    bare = tmp_path / "bare3"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    board.cmd_new(_new_args("제목"))
    path = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]
    ticket_id = path.name.split("-seed")[0] if "seed" in path.name else "-".join(path.stem.split("-")[:2])

    # 본문을 self-contained 하게 채운다(placeholder 제거 + 필수 섹션 유지).
    fm, _body = board.load_ticket(path)
    filled_body = (
        f"# {ticket_id} — 제목\n\n"
        "## 목표\n실제 목표를 채웠다.\n\n"
        "## 인터페이스\n실제 인터페이스 규격.\n\n"
        "## 결정\n실제 구현 방향.\n\n"
        "## 완료 조건 (Definition of Done)\n- [ ] 실제 산출물\n\n"
        "## 참고\n- 실제 참고 사항\n\n"
        "## 메모\n"
    )
    board.dump_ticket(path, fm, filled_body)

    rc = board.cmd_promote(argparse.Namespace(id=ticket_id))
    assert rc == 0
    assert not list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "승격된 draft 가 drafts_dir() 에 남아있으면 안 된다(open/ 으로 이동해야)."
    assert list((board_dir / "tickets" / "open").glob("T-*-*.md")), \
        "승격된 티켓이 open/ 으로 이동 안 됨."
    log = _git(["log", "--oneline"], board_dir).stdout
    lines = log.strip().splitlines()
    assert len(lines) == 3, \
        "board init + 첫 draft 격리 backfill + promote 커밋이 각각 하나여야 한다."
    assert any("promote" in ln for ln in lines)
    # `-z` 로 NUL 구분 raw 경로를 받는다 — 기본 `ls-tree` 는 non-ASCII(한글) 파일명을
    # core.quotepath 8진 이스케이프로 quote 해 문자열 포함 비교가 깨진다.
    remote_ls = _git(["ls-tree", "-zr", "--name-only", "main"], bare).stdout
    assert f"tickets/open/{path.name}" in remote_ls.split("\0"), \
        "promote 가 승격된 티켓을 remote 로 push 안 함."


@requires_git
def test_promote_nonexistent_ticket_errors(board, tmp_path):
    bare = tmp_path / "bare4"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    _make_board_git(tmp_path, remote=bare)
    rc = board.cmd_promote(argparse.Namespace(id="T-9999"))
    assert rc == 2


# ════════════════════════════════════════════════════════════════════════
# leak 재현 — draft 생성 *후* 무관 후속 mutation 이 draft 를 board-git 에 안 쓸어담아야
# 한다 (T-0198 MUST-FIX). fix 이전엔 draft 가 `tickets/open/` 에 물리적으로 존재해
# `_board_git_stage_and_commit` 의 `git add -A` 가 다음 아무 mutation 에서나 draft 를
# 커밋해버렸다(T-0196 은 draft *자신의* sync 만 skip·후속 mutation 은 못 막음).
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_draft_not_leaked_by_unrelated_promote(board, tmp_path):
    """draft(T-0001) 생성 후 무관 티켓(T-0002) 을 promote 해도 T-0001 draft 는 board-git 미커밋.

    fix 전: draft 가 `tickets/open/T-0001-*.md` 에 있어 T-0002 의 promote 가 부르는
    `_board_git_stage_and_commit` 의 `git add -A` 에 T-0001 draft 까지 함께 stage 돼 같은
    commit 에 실려 remote 로 push 됐다 — 이게 leak. fix 후: draft 는 drafts_dir()(STATUS_DIRS
    밖)에 있어 어떤 mutation 의 `git add -A` 도 볼 수 없다(추가 pathspec exclude 로 이중 방어)."""
    bare = tmp_path / "bare-leak1"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    # 1) T-0001 draft 생성(placeholder 그대로 — board-git 미커밋 상태로 남음).
    board.cmd_new(_new_args("첫 번째"))
    draft_path = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]
    draft_id = "-".join(draft_path.stem.split("-")[:2])

    # 2) 무관한 T-0002 를 만들어 본문을 채우고 promote — 이게 leak 을 유발하던 후속 mutation.
    board.cmd_new(_new_args("두 번째"))
    filled_id = None
    for p in (board_dir / "tickets" / ".drafts").glob("T-*-*.md"):
        if p != draft_path:
            filled_id = "-".join(p.stem.split("-")[:2])
            fm, _ = board.load_ticket(p)
            filled_body = (
                f"# {filled_id} — 두 번째\n\n"
                "## 목표\n실제 목표.\n\n"
                "## 인터페이스\n규격.\n\n"
                "## 결정\n방향.\n\n"
                "## 완료 조건 (Definition of Done)\n- [ ] 산출물\n\n"
                "## 참고\n- 참고\n\n## 메모\n"
            )
            board.dump_ticket(p, fm, filled_body)
    assert filled_id, "두 번째 draft 파일을 못 찾음(테스트 셋업 오류)."

    rc = board.cmd_promote(argparse.Namespace(id=filled_id))
    assert rc == 0, "무관 티켓의 promote 자체가 실패함(테스트 전제 붕괴)."

    # T-0001 draft 는 여전히 drafts_dir() 에 있고 board-git 에 커밋되지 않아야 한다.
    assert draft_path.exists(), "무관 promote 후 draft 파일이 사라짐(예상 밖 부작용)."
    # `-z` 로 NUL 구분 raw 경로를 받는다 — 기본 출력은 non-ASCII(한글) 파일명을
    # core.quotepath 8진 이스케이프로 quote 해 단순 `in` 포함 검사가 leak 을 놓칠 수 있다.
    remote_ls = _git(["ls-tree", "-zr", "--name-only", "main"], bare).stdout.split("\0")
    assert not any(draft_path.name in entry for entry in remote_ls), (
        f"leak 재발 — 무관 promote(T-0002)의 커밋에 draft({draft_path.name})가 "
        f"remote 로 push 됨: {remote_ls!r}")
    log_files = _git(["show", "--stat", "--oneline", "HEAD"], board_dir).stdout
    assert draft_path.name not in log_files, (
        f"leak 재발 — 무관 promote 의 HEAD commit 에 draft 파일이 포함됨: {log_files!r}")


@requires_git
def test_draft_not_leaked_by_unrelated_claim_and_complete(board, tmp_path):
    """draft 생성 후 무관 티켓(T-0001, 기존 seed)을 claim+complete 해도 draft 는 board-git 미커밋.

    claim(strict)·complete(best-effort) 모두 `_board_git_stage_and_commit` 을 거친다 — 둘 다
    무관 draft 를 안 쓸어담아야 한다."""
    bare = tmp_path / "bare-leak2"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    # draft(placeholder 그대로) 를 만든다 — board-git 미커밋 상태.
    board.cmd_new(_new_args("드래프트"))
    draft_path = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]

    # seed 티켓(T-0001, _make_board_git 이 만든 기존 open 티켓과 별개로 이번엔 new 로 만든
    # 필드가 채워진 티켓)을 claim + complete — 무관 mutation 연쇄.
    seed_path = list((board_dir / "tickets" / "open").glob("T-0001-*.md"))
    if not seed_path:
        # `_make_board_git` 은 이 fixture 에서 template 만 커밋하므로(테스트용 별도 seed 필요)
        # T-0001 자체를 직접 만든다.
        board.cmd_new(_new_args("실 티켓"))
        for p in (board_dir / "tickets" / ".drafts").glob("T-*-*.md"):
            if p != draft_path:
                fm, _ = board.load_ticket(p)
                seed_id = "-".join(p.stem.split("-")[:2])
                # DoD 는 체크 상태로 심는다 — 이 테스트의 주제는 draft leak 이지만 아래에서
                # 실제로 complete 까지 태우므로 DoD 기록 게이트(T-0596)를 만족해야 한다.
                filled_body = (
                    f"# {seed_id} — 실 티켓\n\n"
                    "## 목표\n실제 목표.\n\n"
                    "## 인터페이스\n규격.\n\n"
                    "## 결정\n방향.\n\n"
                    "## 완료 조건 (Definition of Done)\n- [x] 산출물\n\n"
                    "## 참고\n- 참고\n\n## 메모\n"
                )
                board.dump_ticket(p, fm, filled_body)
                assert board.cmd_promote(argparse.Namespace(id=seed_id)) == 0
    else:
        seed_id = "-".join(seed_path[0].stem.split("-")[:2])

    assert board.cmd_claim(
        argparse.Namespace(id=seed_id, repo="me", slot=1, user="me")) == 0, \
        "seed 티켓 claim 실패(테스트 전제 붕괴)."
    # complete 는 claim 과 같은 정체성으로 부른다 — 소유 대조(T-0781).
    rc = board.cmd_complete(argparse.Namespace(
        id=seed_id, tests_pass=True, allow_missing_log=True, allow_untested=False,
        repo="me", slot=1, user="me"))
    assert rc == 0, "seed 티켓 complete 실패(테스트 전제 붕괴)."

    # draft 는 여전히 drafts_dir() 에 있고 board-git 에 커밋/push 되지 않아야 한다.
    assert draft_path.exists(), "무관 claim/complete 후 draft 파일이 사라짐(예상 밖 부작용)."
    # `-z` 로 NUL 구분 raw 경로 — quote 이스케이프로 인한 false-negative 방지(위 promote 케이스 동형).
    remote_ls = _git(["ls-tree", "-zr", "--name-only", "main"], bare).stdout.split("\0")
    assert not any(draft_path.name in entry for entry in remote_ls), (
        f"leak 재발 — 무관 claim/complete 커밋에 draft({draft_path.name})가 "
        f"remote 로 push 됨: {remote_ls!r}")


# ════════════════════════════════════════════════════════════════════════
# board-git 비활성(legacy·솔로) — 게이트 무의미, 기존처럼 즉시 동작
# ════════════════════════════════════════════════════════════════════════

def test_new_legacy_no_git_gate_no_op(board, tmp_path, monkeypatch):
    """board 가 별도 git 아니면(legacy) 게이트 없이 기존 sync 경로 그대로(git 미호출)."""
    wiki = tmp_path / ".project_manager" / "wiki"
    for status in ("open", "claimed", "blocked", "done"):
        (wiki / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (wiki / "tickets" / "_template.md").write_text(_TEMPLATE_TEXT, encoding="utf-8")

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("legacy 인데 board-git 호출 발생 — 게이트 누출")

    monkeypatch.setattr(board, "_board_git", _boom)
    rc = board.cmd_new(_new_args("제목"))
    assert rc == 0
    assert called["n"] == 0
    assert list((wiki / "tickets" / "open").glob("T-*-*.md")), \
        "legacy 에서도 파일은 정상 생성돼야 한다."


# ════════════════════════════════════════════════════════════════════════
# draft 종결 경로 — discard 가 draft 도 받아 discarded/ 로 박제하고 reopen 이 되돌린다.
# 번호 배정(`_ID_SCAN_STATUSES`)은 무변경 — 여기선 종결 기록만 다룬다.
# ════════════════════════════════════════════════════════════════════════

def _discard_args(tid: str, disposition: str, reason: str) -> argparse.Namespace:
    return argparse.Namespace(id=tid, disposition=disposition, reason=reason)


def _reopen_args(tid: str, reason: str) -> argparse.Namespace:
    return argparse.Namespace(id=tid, reason=reason)


def _draft_id_and_path(board_dir: Path) -> tuple[str, Path]:
    path = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]
    tid = "-".join(path.stem.split("-")[:2])
    return tid, path


def _commit_file_status(board_dir: Path, rev: str = "HEAD") -> list[str]:
    """`<STATUS>\t<경로>` 목록 — 커밋 메타 없이 그 한 커밋이 담은 파일만.

    `-z`(NUL 구분)로 받는다 — 기본 출력은 non-ASCII(한글) 파일명을 core.quotepath 8진
    이스케이프로 quote 해 문자열 비교가 깨진다(이 파일의 다른 `ls-tree -z` 케이스와 동형)."""
    out = _git(["diff-tree", "--no-commit-id", "--name-status", "-r", "-z", rev], board_dir).stdout
    parts = [p for p in out.split("\0") if p]
    pairs = iter(parts)
    return [f"{status}\t{path}" for status, path in zip(pairs, pairs)]


@requires_git
def test_discard_accepts_a_draft_and_commits_only_the_new_file(board, tmp_path):
    """draft 를 discard 하면 discarded/ 로 이동 + discarded_from 기록 + board-git 커밋은 추가 1건만."""
    bare = tmp_path / "bare-discard-draft"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    board.cmd_new(_new_args("치울 draft"))
    tid, draft_path = _draft_id_and_path(board_dir)
    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()

    rc = board.cmd_discard(_discard_args(tid, "dropped", "폐기 — 대상 아님"))

    assert rc == 0
    assert not draft_path.exists(), "draft 원본이 .drafts/ 에 남아있으면 안 된다."
    discarded = list((board_dir / "tickets" / "discarded").glob(f"{tid}-*.md"))
    assert discarded, "discard 뒤 discarded/ 에 파일이 있어야 한다."
    fm, body = board.load_ticket(discarded[0])
    assert fm["status"] == "discarded"
    assert fm["disposition"] == "dropped"
    assert fm["discarded_from"] == "draft"
    assert "## Discarded" in body

    head_after = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    assert head_after != head_before, "discard 가 board-git 커밋을 내지 않았다."
    assert _commit_file_status(board_dir) == [f"A\ttickets/discarded/{discarded[0].name}"]
    assert _git(["status", "--porcelain"], board_dir).stdout.strip() == "", \
        "discard 뒤 워킹트리가 clean 이어야 한다(옛 draft 경로가 untracked 로 남으면 안 됨)."

    # I4 — 폐기된 번호는 재사용되지 않는다: 다음 발행이 정확히 다음 순번을 받는다
    # (`_ID_SCAN_STATUSES` 가 discarded 를 놓치면 여기서 실패한다).
    assert board.cmd_new(_new_args("다음 draft")) == 0
    next_tid, _next_path = _draft_id_and_path(board_dir)
    discarded_number = int(tid.split("-", 1)[1])
    next_number = int(next_tid.split("-", 1)[1])
    assert next_number == discarded_number + 1, \
        f"폐기 번호({tid}) 다음이 아니라 {next_tid} 가 배정됐다."


@requires_git
def test_discard_of_a_draft_with_a_pending_round_passes_with_a_notice(board, tmp_path, capsys):
    """미회수(시드 그대로) 라운드가 있어도 draft discard 는 차단되지 않고, 미회수 장부에서 해소한
    실행 가능한 abandon 처방 1줄만 ⓘ 로 낸다 — board-git 커밋은 discarded/ 추가만 담고, tracked
    pending 라운드는 discard 전후로 바이트 하나 바뀌지 않는다."""
    bare = tmp_path / "bare-discard-pending"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    board.cmd_new(_new_args("라운드 딸린 draft"))
    tid, draft_path = _draft_id_and_path(board_dir)
    rounds = board._load_ticket_rounds()
    round_dir = rounds.rounds_dir_for_ticket(tid, board.tickets_dir())
    round_dir.mkdir(parents=True, exist_ok=True)
    seed_text = rounds.render_round_seed(
        "architect", draft_path.read_text(encoding="utf-8"),
        today=datetime.date.today().isoformat())
    round_path = round_dir / "01-architect.md"
    round_path.write_text(seed_text, encoding="utf-8")
    # 실 prepare/sync 와 동형: 이 라운드는 이미 board-git 에 tracked 다(예 — 다른 라운드가
    # 나중에 승격 스코프를 통해 같은 디렉터리를 함께 실었을 때의 형상). discard 자신은 이
    # tracked 상태를 건드리지 않는다는 것이 아래 값 단언(I6)의 대상이다.
    _git(["add", "-A", "--", "tickets/rounds"], board_dir)
    _git(["commit", "-qm", "seed pending round"], board_dir)
    round_bytes_before = round_path.read_bytes()
    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()

    # 실 prepare 가 남기는 미회수 장부 행 — discard 의 ⓘ 안내가 여기서 --copy/--cwd 실값을
    # 해소해야 한다(F-001). run-dir 자체는 짓지 않는다(안내는 장부 값만 읽는다).
    run_id = "f" * 32
    copy_root = tmp_path / "delegate-cwd"
    copy_path = (
        copy_root / ".project_manager" / ".local" / "delegate-ticket-copies"
        / tid / run_id / "01-architect.md"
    )
    copy_path.parent.mkdir(parents=True, exist_ok=True)
    copy_path.write_text(seed_text, encoding="utf-8")
    ledger_row = {
        "ticket": tid, "role": "architect", "ordinal": 1, "run_id": run_id,
        "copy": str(copy_path.resolve()),
        "board_rel": f"tickets/rounds/{tid}/01-architect.md",
        "prepared_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "harvested_at": None,
    }
    ledger_dir = tmp_path / ".project_manager" / ".local"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "delegate-rounds.jsonl").write_text(
        json.dumps(ledger_row, sort_keys=True) + "\n", encoding="utf-8")

    rc = board.cmd_discard(_discard_args(tid, "dropped", "폐기 — 라운드 미회수"))

    err = capsys.readouterr().err
    assert rc == 0, f"미회수 라운드가 discard 를 막았다: {err}"
    notice_lines = [line for line in err.splitlines() if "round-pending" in line]
    assert len(notice_lines) == 1, f"round-pending 안내가 정확히 1줄이 아니다: {err!r}"
    notice = notice_lines[0]
    assert "01-architect.md" in notice
    expected_command = (
        "python3 .project_manager/tools/pm_delegate.py ticket abandon "
        f"--copy {copy_path.resolve()} --cwd {copy_root.resolve()} --assume-dead"
    )
    assert expected_command in notice, notice
    discarded = list((board_dir / "tickets" / "discarded").glob(f"{tid}-*.md"))
    assert discarded, "안내만 내고 discard 자체는 통과해야 한다."

    head_after = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    assert head_after != head_before, "discard 가 board-git 커밋을 내지 않았다."
    assert _commit_file_status(board_dir) == [f"A\ttickets/discarded/{discarded[0].name}"], \
        "discard 커밋은 discarded/ 추가만 담아야 한다(tracked pending 라운드는 안 건드린다)."
    assert _git(["status", "--porcelain"], board_dir).stdout.strip() == "", \
        "discard 뒤 워킹트리가 clean 이어야 한다(tracked pending 라운드가 dirty 로 남으면 안 됨)."
    assert round_path.read_bytes() == round_bytes_before, \
        "discard 가 tracked pending 라운드 파일을 건드리면 안 된다(바이트 무변경)."


@requires_git
def test_reopen_of_a_draft_discard_returns_to_drafts(board, tmp_path):
    """`discarded_from: draft` 의 reopen 은 `.drafts/` 로 복귀 — status=draft·필드 제거·삭제 커밋 1건."""
    bare = tmp_path / "bare-reopen-draft"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    board.cmd_new(_new_args("되돌릴 draft"))
    tid, _draft_path = _draft_id_and_path(board_dir)
    assert board.cmd_discard(_discard_args(tid, "dropped", "일단 폐기")) == 0
    discarded_name = list((board_dir / "tickets" / "discarded").glob(f"{tid}-*.md"))[0].name
    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()

    rc = board.cmd_reopen(_reopen_args(tid, "다시 필요함"))

    assert rc == 0
    assert not list((board_dir / "tickets" / "discarded").glob(f"{tid}-*.md"))
    assert not list((board_dir / "tickets" / "open").glob(f"{tid}-*.md")), \
        "draft 출처 reopen 이 open/ 으로 갔다(placeholder 게이트를 우회함)."
    restored = list((board_dir / "tickets" / ".drafts").glob(f"{tid}-*.md"))
    assert restored, "draft 출처 reopen 이 .drafts/ 로 복귀하지 않았다."
    fm, body = board.load_ticket(restored[0])
    assert fm["status"] == "draft"
    assert "discarded_from" not in fm
    assert "## Reopened" in body

    head_after = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    assert head_after != head_before, "reopen 이 board-git 커밋을 내지 않았다."
    assert _commit_file_status(board_dir) == [f"D\ttickets/discarded/{discarded_name}"], \
        "reopen 커밋은 discarded/ 삭제만 담아야 한다(복귀 경로는 ignore 대상)."
    assert _git(["status", "--porcelain"], board_dir).stdout.strip() == ""
