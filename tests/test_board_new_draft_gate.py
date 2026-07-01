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
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

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


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
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
    mod = _load_board()
    monkeypatch.setattr(mod, "REPO", tmp_path)
    lock = tmp_path / ".project_manager" / ".local" / "board.lock"
    monkeypatch.setattr(mod, "BOARD_LOCK", lock)
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
    out = capsys.readouterr().out + capsys.readouterr().err

    open_files = list((board_dir / "tickets" / "open").glob("T-*-*.md"))
    assert not open_files, "draft 가 STATUS_DIRS 대상인 open/ 에 있으면 안 된다(격리 위반)."
    draft_files = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))
    assert draft_files, "draft 는 drafts_dir()(tickets/.drafts/)엔 존재해야 한다."

    # 순수 `git status --porcelain`(exclude 없음)은 draft 를 untracked(`??`)로 보여준다 —
    # 이건 정상(파일이 board_root 안에 물리적으로 있으므로). "커밋 안 됐다"만 확인한다.
    status = _git(["status", "--porcelain"], board_dir).stdout
    assert status.strip(), "미충전 draft 가 board-git 에 이미 커밋됨 — 게이트 누출."
    assert "?? tickets/.drafts/" in status, \
        f"draft 가 예상과 다른 형태로 나타남: {status!r}"

    # dirty *판정용* 헬퍼(`_board_git_status_porcelain` — claim prefetch 가 쓴다)는 draft 를
    # pathspec exclude 하므로 clean 으로 봐야 한다(무관 claim 이 draft 때문에 막히면 안 됨).
    assert not board._board_git_status_porcelain().strip(), (
        "_board_git_status_porcelain 이 draft 를 dirty 로 오판함 — 무관 claim 이 막힐 위험.")

    log = _git(["log", "--oneline"], board_dir).stdout
    assert "board init" in log
    assert len(log.strip().splitlines()) == 1, "draft 가 board-git 에 커밋되면 안 된다."


@requires_git
def test_promote_rejects_still_placeholder(board, tmp_path):
    """본문이 여전히 미충전이면 `promote` 가 거부(rc=1)한다(파일은 drafts_dir() 에 잔류)."""
    bare = tmp_path / "bare2"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    board.cmd_new(_new_args("제목"))
    tid = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0].name.split("-", 2)
    ticket_id = f"{tid[0]}-{tid[1]}"

    rc = board.cmd_promote(argparse.Namespace(id=ticket_id))
    assert rc == 1
    log = _git(["log", "--oneline"], board_dir).stdout
    assert len(log.strip().splitlines()) == 1, "거부된 promote 가 커밋을 남기면 안 된다."
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
    assert len(lines) == 2, "채운 본문의 promote 는 board-git 에 새 commit 을 남겨야 한다."
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
                filled_body = (
                    f"# {seed_id} — 실 티켓\n\n"
                    "## 목표\n실제 목표.\n\n"
                    "## 완료 조건 (Definition of Done)\n- [ ] 산출물\n\n"
                    "## 참고\n- 참고\n\n## 메모\n"
                )
                board.dump_ticket(p, fm, filled_body)
                assert board.cmd_promote(argparse.Namespace(id=seed_id)) == 0
    else:
        seed_id = "-".join(seed_path[0].stem.split("-")[:2])

    assert board.cmd_claim(
        argparse.Namespace(id=seed_id, session="me", user="me")) == 0, \
        "seed 티켓 claim 실패(테스트 전제 붕괴)."
    rc = board.cmd_complete(argparse.Namespace(
        id=seed_id, tests_pass=True, allow_missing_log=True, allow_untested=False))
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
