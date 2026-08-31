"""claim/best-effort board-git 트랜잭션의 **비파괴 스코프** 회귀 (T-0419·ADR-0073).

사용자 실사용 보고 — *"다른 슬롯의 클레임 티켓이 커밋되지 않은 문제 때문에 타 슬롯 claim 이
차단된다."* — 에서 출발한 재작성이다. 지켜야 할 성질을 한 줄로:

> 공유 board 워킹트리의 **어떤 미커밋 작업도 커밋되지도 파괴되지도 않는다.** 차단은 "원격이
> 앞섰고 통합이 불가능한" 경우로만 남고, 소유 확정 권위는 여전히 **원격 ref FF push(CAS)** 다.

여기서 검증하는 것(architect 가 격리 fixture 로 실측한 위험 지점 D1~D6 대응):

  - **D1 과차단** — 무관 dirty 3종(staged/unstaged/untracked)에서 claim 이 *성공* 한다.
  - **D2 커밋 누출** — 그 dirty 가 claim/best-effort 커밋에 **실리지 않는다**(경로 스코프).
  - **D3 롤백 파괴** — 롤백 후 dirty 3종이 상태·내용 그대로 남는다(`reset --hard` 폐기).
  - **D5 오진** — 잔여 차단의 사유가 4분(dirty/rebase/offline/upstream 없음)되고 진단에
    behind·막고 있는 파일이 나온다.
  - **D6 고아 claim** — push 가 예외로 끝나도 원격 tip 을 재확인해 성공이면 롤백하지 않는다.
  - **직렬화** — board-git mutation 트랜잭션이 별도 flock 으로 상호배제된다.

hermetic 패턴은 `test_board_git_sync.py` 와 동형(실 board git + bare remote + REPO
monkeypatch). git 부재 환경에선 실 git 케이스를 skip 한다.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import shutil
import stat
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

# 5절을 모두 채운(placeholder 0) 템플릿 — `new` 가 draft 격리로 빠지지 않고 곧바로 open/ 에
# 발행되게 한다(이 파일의 관심사는 draft 게이트가 아니라 *커밋 스코프*다).
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
    "# T-NNNN — 제목\n\n"
    "## 목표\n실제 목표 문장이다.\n\n"
    "## 인터페이스\n실제 규격이다.\n\n"
    "## 결정\n실제 방향이다.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 실제 산출물\n\n"
    "## 참고\n- 실제 참고\n\n"
    "## 메모\n"
)

_TICKET_TEXT = (
    "---\n"
    "id: {tid}\n"
    "title: t\n"
    "status: {status}\n"
    "claimed_by: null\n"
    "claimed_at: null\n"
    "completed_at: null\n"
    "depends_on: []\n"
    "blocks: []\n"
    "touches: []\n"
    "estimate: small\n"
    "tags: []\n"
    "---\n\n# {tid} — t\n\n## 목표\nx\n\n"
    "## 완료 조건 (Definition of Done)\n- [x] 구현\n"
)


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# "이미 배포된 board" 형상 = **엔진이 쓰는 그 블록 그대로**. 리터럴을 손으로 베끼면 규칙이
# 넓어질 때 픽스처만 구세대로 남아, 커밋 스코프 단언에 backfill 잡음이 섞인다(T-0709 실측).
_DEPLOYED_BOARD_GITATTRIBUTES = _load_board()._BOARD_GITATTRIBUTES_BLOCK


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=False)


def _make_board_git(root: Path, *, remote: Path, tid: str = "T-0001",
                    gitattributes: bool = True, gitignore: bool = True) -> Path:
    """`<root>/.project_manager/board/` 에 실 board git 을 만든다 (tickets/ + areas + remote).

    `notes.md` 는 **추적 파일**이다 — unstaged dirty 3종 모사에 필요하다(untracked 만으로는
    `pull --rebase` 가 성공해 잔여 차단 경로를 못 탄다). board 루트 배포 파일
    (`.gitattributes`·`.gitignore`)은 기본으로 seed 해 **이미 배포된 board** 형상을 만든다 —
    커밋 스코프 단언에 backfill 잡음이 섞이지 않게 하기 위해서다. backfill 자체는
    `test_gitattributes_backfill_rides_scoped_claim_commit`(areas union)과
    `test_board_draft_gitignore.py`(draft ignore)가 따로 본다.
    """
    board = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board / "tickets" / f"{tid}-t.md").parent.mkdir(parents=True, exist_ok=True)
    (board / "tickets" / "open" / f"{tid}-t.md").write_text(
        _TICKET_TEXT.format(tid=tid, status="open"), encoding="utf-8")
    (board / "tickets" / "_template.md").write_text(_TEMPLATE_TEXT, encoding="utf-8")
    (board / "areas.md").write_text("# Area Registry\n", encoding="utf-8")
    (board / "notes.md").write_text("original\n", encoding="utf-8")
    if gitattributes:
        (board / ".gitattributes").write_text(
            _DEPLOYED_BOARD_GITATTRIBUTES, encoding="utf-8", newline="\n")
    if gitignore:
        (board / ".gitignore").write_text("tickets/.drafts/\n", encoding="utf-8")
    _git(["init", "-q", "-b", "main"], board)
    _git(["remote", "add", "origin", str(remote)], board)
    _git(["add", "-A"], board)
    _git(["commit", "-qm", "board init"], board)
    _git(["push", "-q", "-u", "origin", "main"], board)
    return board


def _bare(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / name
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    return bare


@pytest.fixture
def board(tmp_path, monkeypatch):
    """REPO 를 tmp 로 재지정한 fresh board 모듈 (실 루트 미접촉).

    `board_git_lock` 의 락 파일은 REPO 파생 lazy 해소라(별도 seam 없음) 이 monkeypatch 하나로
    함께 tmp 로 따라온다.
    """
    mod = _load_board()
    anchor_board_module(mod, tmp_path, monkeypatch)
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    # 정체성 축을 tmp 에 묶는다 — 소유 대조(T-0781)가 user 축을 보므로, 실 clone 의 local.conf
    # /전역 git email 이 새면 픽스처 claim(`me/…`)과 어긋난다. 세션은 각 테스트가 명시
    # (`--repo/--slot` 또는 `PM_SESSION_NAME`)한다 — conf `session=` 폴백은 폐지됐다(T-0779).
    conf = tmp_path / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("identity.user=me\n", encoding="utf-8")
    monkeypatch.setattr(mod, "LOCAL_CONF", conf)
    monkeypatch.setattr(
        mod, "LEASES_FILE",
        tmp_path / ".project_manager" / ".local" / "worktree-leases.json")
    return mod


def _claim_args(tid: str = "T-0001") -> argparse.Namespace:
    return argparse.Namespace(id=tid, repo="me", slot=1, user="me")


def _porcelain(board_dir: Path) -> dict[str, str]:
    """`path -> 상태코드` 맵 (dirty 3종의 *정확한* 보존을 단언하기 위한 스냅샷)."""
    out = _git(["status", "--porcelain"], board_dir).stdout
    result: dict[str, str] = {}
    for line in out.splitlines():
        if len(line) < 4:
            continue
        result[line[3:].strip().strip('"')] = line[:2]
    return result


def _head_files(board_dir: Path) -> set[str]:
    """HEAD 커밋이 담은 경로 집합 — 커밋 스코프 단언의 단일 관측 지점.

    `--no-renames` 로 rename 탐지를 끈다 — 켜져 있으면(git 기본) open→claimed 이동이 `R` 한
    줄로 접혀 *옛* 경로가 안 보인다(스코프가 좁은 것처럼 착시). `-z` 는 한글 경로 quote 회피.
    """
    out = _git(["show", "--no-renames", "--name-only", "--format=", "-z", "HEAD"],
               board_dir).stdout
    return {p.strip() for p in out.split("\0") if p.strip()}


def _seed_dirty_three(board_dir: Path) -> dict[str, str]:
    """무관한 미커밋 작업 3종을 만든다 — staged / unstaged / untracked. 반환 = 기대 상태코드."""
    staged = board_dir / "tickets" / "open" / "T-0003-staged.md"
    staged.write_text(_TICKET_TEXT.format(tid="T-0003", status="open"), encoding="utf-8")
    _git(["add", "--", str(staged)], board_dir)
    notes = board_dir / "notes.md"
    notes.write_text("original\nunstaged edit\n", encoding="utf-8")
    untracked = board_dir / "tickets" / "open" / "T-0004-untracked.md"
    untracked.write_text(_TICKET_TEXT.format(tid="T-0004", status="open"), encoding="utf-8")
    return {
        "tickets/open/T-0003-staged.md": "A ",
        "notes.md": " M",
        "tickets/open/T-0004-untracked.md": "??",
    }


def _claim_move(board_mod, board_dir: Path, tid: str = "T-0001"):
    """claim 의 파일 mutation 만 모사 — open/ → claimed/ 이동 + `_ClaimFiles` 반환 (롤백 격리용)."""
    src = board_dir / "tickets" / "open" / f"{tid}-t.md"
    dst = board_dir / "tickets" / "claimed" / f"{tid}-t.md"
    original = src.read_bytes()
    src.rename(dst)
    return board_mod._ClaimFiles(old=src, new=dst, original=original)


def _advance_remote(tmp_path: Path, bare: Path, *, name: str = "other") -> None:
    """다른 clone 이 remote 를 전진시킨다 — behind>0 / push non-FF 상황을 만든다."""
    other = tmp_path / name
    _git(["clone", "-q", str(bare), str(other)], tmp_path)
    (other / "advance.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], other)
    _git(["commit", "-qm", "remote advance"], other)
    _git(["push", "-q", "origin", "main"], other)


# ════════════════════════════════════════════════════════════════════════
# D1 — 무관 dirty 는 더 이상 claim 을 막지 않는다
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_claim_succeeds_with_unrelated_dirty_three_kinds(board, tmp_path):
    """staged/unstaged/untracked 무관 dirty 가 있어도 claim 이 성공한다 (사용자 보고 결함 폐쇄).

    옛 동작은 board 전체 porcelain 이 non-empty 이기만 하면 전면 차단이었다 — 다른 슬롯의
    미커밋 티켓 하나가 무관한 claim 을 막았다. 소유 확정 권위는 원격 push CAS 지 로컬 tree 의
    clean 여부가 아니다."""
    bare = _bare(tmp_path, "bare-dirty3")
    board_dir = _make_board_git(tmp_path, remote=bare)
    expected = _seed_dirty_three(board_dir)

    rc = board.cmd_claim(_claim_args())

    assert rc == 0, "무관 dirty 3종 때문에 claim 이 차단됨 — D1 과차단 재발."
    assert list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "claim 성공인데 티켓이 claimed/ 로 안 옮겨짐."
    after = _porcelain(board_dir)
    for path, code in expected.items():
        assert after.get(path) == code, \
            f"claim 이 무관 dirty 의 상태를 바꿈: {path} {expected[path]!r} → {after.get(path)!r}"


@requires_git
def test_claim_commit_carries_only_the_ticket_paths(board, tmp_path):
    """claim 커밋에 **그 티켓 두 경로만** 실린다 — 무관 dirty 는 로컬에도 원격에도 안 실린다 (D2).

    실증된 누출: claim 커밋에 티켓 rename + 남의 staged `areas.md` + 타 슬롯 WIP 가 함께 push
    됐다(T-0198 draft 유출과 같은 클래스)."""
    bare = _bare(tmp_path, "bare-scope")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _seed_dirty_three(board_dir)

    assert board.cmd_claim(_claim_args()) == 0

    assert _head_files(board_dir) == {
        "tickets/open/T-0001-t.md", "tickets/claimed/T-0001-t.md"}, \
        f"claim 커밋 스코프가 티켓 두 경로를 넘음: {_head_files(board_dir)}"
    remote_ls = _git(["ls-tree", "-r", "--name-only", "main"], bare).stdout
    assert "tickets/claimed/T-0001-t.md" in remote_ls, "claim 이 원격에 안 실림."
    for leaked in ("T-0003-staged.md", "T-0004-untracked.md"):
        assert leaked not in remote_ls, f"무관 미커밋 작업이 원격으로 push 됨: {leaked}"
    assert "unstaged edit" not in _git(["show", "HEAD:notes.md"], board_dir).stdout, \
        "무관 파일의 미커밋 편집이 claim 커밋에 실림 — D2 누출."


@requires_git
def test_gitattributes_backfill_rides_scoped_claim_commit(board, tmp_path):
    """`.gitattributes` backfill 이 **스코프 커밋에도 실린다** — T-0418 무력화 금지.

    backfill 호출은 commit funnel 안에 있다. pathspec 에서 `.gitattributes` 가 빠지면 그 파일은
    영구 미커밋으로 남아 areas union 배포(T-0418)가 조용히 죽는다."""
    bare = _bare(tmp_path, "bare-attrs")
    board_dir = _make_board_git(tmp_path, remote=bare, gitattributes=False)
    assert not (board_dir / ".gitattributes").exists()

    assert board.cmd_claim(_claim_args()) == 0

    assert ".gitattributes" in _head_files(board_dir), \
        "claim 스코프 커밋이 backfill 한 .gitattributes 를 싣지 않음 — T-0418 무력화."
    assert "areas.md merge=union" in _git(
        ["show", "HEAD:.gitattributes"], board_dir).stdout


@requires_git
def test_user_edited_gitattributes_is_not_swept_into_mutation(board, tmp_path):
    """사용자가 편집 중인 `.gitattributes` 는 티켓 mutation 이 **대신 커밋하지 않는다**.

    backfill 분은 실어야 하지만(위 테스트), 그렇다고 무조건 pathspec 에 넣으면 남의 미완성
    편집을 쓸어담는다 — 이 티켓이 닫으려는 누출과 **동형**이다(reviewer). 조건은 "이번 호출의
    backfill 이 실제로 썼는가" 다."""
    bare = _bare(tmp_path, "bare-attrs2")
    board_dir = _make_board_git(tmp_path, remote=bare)   # union 선언이 이미 있다 → backfill no-op.
    attrs = board_dir / ".gitattributes"
    attrs.write_text(attrs.read_text(encoding="utf-8") + "*.md text eol=lf\n", encoding="utf-8")

    assert board.cmd_claim(_claim_args()) == 0

    assert ".gitattributes" not in _head_files(board_dir), \
        "사용자의 미완성 .gitattributes 편집이 claim 커밋에 실림 — 누출 동형."
    assert _porcelain(board_dir).get(".gitattributes") == " M", \
        "사용자 편집이 미커밋 상태로 보존되지 않음."


# ════════════════════════════════════════════════════════════════════════
# D3 — 롤백은 그 claim 이 만진 것만 되돌린다
# ════════════════════════════════════════════════════════════════════════

def _racing_push(board_mod, tmp_path: Path, bare: Path, *, name: str):
    """push **직전** 에 다른 clone 이 원격을 전진시키는 경합을 만든다 (prefetch 시점엔 behind=0).

    롤백 경로를 non-vacuous 하게 타려면 이 순서가 필요하다 — 원격을 *미리* 전진시키면 prefetch
    가 `behind>0 ∧ 추적 dirty` 로 **차단**해 버려(rc=1) 롤백 함수에 도달조차 하지 않는다
    (초판 테스트가 그래서 공허했다·reviewer 실측). 실제 결함 시나리오도 "내 fetch 이후 남이
    push" 라 이쪽이 현실 충실도도 높다.
    """
    real_push = board_mod._board_git_push

    def _push():
        _advance_remote(tmp_path, bare, name=name)   # 그 사이 다른 clone 이 push.
        return real_push()                            # → non-FF 로 거부된다.

    return _push


@requires_git
def test_rollback_preserves_unrelated_dirty_three_kinds(board, tmp_path, monkeypatch):
    """push non-FF 롤백 후 dirty 3종이 **상태·내용 그대로** 남는다 (`reset --hard` 파괴 폐쇄).

    옛 롤백은 anchor 로 hard-reset 해 무관한 미커밋 작업을 통째로 되돌렸다(실측). 신 롤백은
    `reset --soft` + 티켓 역이동·원본 바이트 복원 + 두 경로 재-stage 뿐이다. 이 테스트가 ADR-0073
    간판 결정의 회귀 가드다 — `reset --soft` 를 `--hard` 로 되돌리면 **red 여야 한다**(staged 신규
    파일이 삭제되고 unstaged 편집이 되감기므로)."""
    bare = _bare(tmp_path, "bare-rb")
    board_dir = _make_board_git(tmp_path, remote=bare)
    expected = _seed_dirty_three(board_dir)
    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    original = ticket.read_bytes()
    # prefetch 시점엔 behind=0(차단 없음) → claim commit → **push 직전** 경합 → non-FF → 롤백.
    monkeypatch.setattr(board, "_board_git_push",
                        _racing_push(board, tmp_path, bare, name="rb-other"))

    rc = board.cmd_claim(_claim_args())

    assert rc == 1, "원격이 앞서 push 가 거부됐는데 claim 이 확정됨 — strict 위반."
    after = _porcelain(board_dir)
    for path, code in expected.items():
        assert after.get(path) == code, \
            f"롤백이 무관 dirty 를 훼손함: {path} {code!r} → {after.get(path)!r}"
    assert (board_dir / "notes.md").read_text(encoding="utf-8") == "original\nunstaged edit\n", \
        "롤백이 무관 파일의 미커밋 편집 내용을 되돌림 — D3 파괴 재발."
    assert ticket.exists() and ticket.read_bytes() == original, \
        "롤백 후 티켓이 open/ 에 원본 바이트로 복원되지 않음(거짓 소유·손상)."
    assert not list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "롤백인데 티켓이 claimed/ 에 남음 — 거짓 소유."
    staged = _git(["diff", "--cached", "--name-only"], board_dir).stdout.split()
    assert "tickets/claimed/T-0001-t.md" not in staged, \
        f"롤백 후 claim 이동이 index 에 staged 로 남음 — 두 경로 재-stage 누락: {staged}"
    assert "tickets/open/T-0003-staged.md" in staged, \
        f"재-stage 가 무관한 staged 작업을 index 에서 걷어냄: {staged}"


@requires_git
@pytest.mark.parametrize("target_state", ["unstaged", "untracked", "staged"])
def test_rollback_preserves_claim_target_index_state(board, tmp_path, monkeypatch, target_state):
    """롤백 후 **claim 대상 파일 자신**의 index 상태도 claim 직전 그대로다 (codex must-fix).

    무관 파일 3종은 보존하면서 대상 파일만 `add -A` 로 staged 로 바꾸면, 이 티켓의 핵심
    불변식("미커밋 작업을 상태·내용 그대로 보존")이 한쪽 경로에만 성립하는 것이다. 판정은
    **claim 전후 `git status --porcelain` 전체 동일성** — 상태 조합이 늘어도 이 단언은 그대로다.
    """
    bare = _bare(tmp_path, f"bare-idx-{target_state}")
    board_dir = _make_board_git(tmp_path, remote=bare)
    if target_state == "untracked":
        # 아직 한 번도 커밋되지 않은 티켓(로컬 발행 직후·best-effort 보류 등).
        tid = "T-0010"
        ticket = board_dir / "tickets" / "open" / f"{tid}-t.md"
        ticket.write_text(_TICKET_TEXT.format(tid=tid, status="open"), encoding="utf-8")
    else:
        tid = "T-0001"
        ticket = board_dir / "tickets" / "open" / f"{tid}-t.md"
        ticket.write_text(ticket.read_text(encoding="utf-8") + "\n로컬 편집\n", encoding="utf-8")
        if target_state == "staged":
            _git(["add", "--", str(ticket)], board_dir)

    before = _porcelain(board_dir)
    body_before = ticket.read_bytes()
    monkeypatch.setattr(board, "_board_git_push",
                        _racing_push(board, tmp_path, bare, name=f"idx-{target_state}-other"))

    assert board.cmd_claim(_claim_args(tid)) == 1, "push 경합인데 claim 이 확정됨."

    assert _porcelain(board_dir) == before, (
        f"롤백이 claim 대상 파일의 index 상태를 바꿈({target_state}): "
        f"{before} → {_porcelain(board_dir)}")
    assert ticket.read_bytes() == body_before, "롤백이 대상 파일 내용을 바꿈."


@requires_git
@pytest.mark.parametrize("failure", ["commit", "push-rejected", "push-exception"])
def test_rollback_restores_absent_board_root_files(
        board, tmp_path, monkeypatch, failure):
    """strict 실패 3경로 모두 backfill 루트 파일을 index·워킹트리에서 정확히 걷어낸다."""
    bare = _bare(tmp_path, f"bare-root-rb-{failure}")
    board_dir = _make_board_git(
        tmp_path, remote=bare, gitattributes=False, gitignore=False)
    root_names = (".gitattributes", ".gitignore")
    assert all(not (board_dir / name).exists() for name in root_names)

    if failure == "commit":
        hook = board_dir / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
    elif failure == "push-rejected":
        monkeypatch.setattr(
            board, "_board_git_push",
            _racing_push(board, tmp_path, bare, name="root-rb-other"))
    else:
        def _timeout():
            raise subprocess.TimeoutExpired(cmd="git push", timeout=30)
        monkeypatch.setattr(board, "_board_git_push", _timeout)

    assert board.cmd_claim(_claim_args()) == 1, f"{failure} 실패인데 claim 이 확정됨."

    assert all(not (board_dir / name).exists() for name in root_names), \
        f"{failure} rollback 뒤 backfill 파일이 워킹트리에 남음."
    after = _porcelain(board_dir)
    assert not any(name in after for name in root_names), \
        f"{failure} rollback 뒤 루트 파일이 staged/untracked 잔여로 남음: {after}"
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        f"{failure} rollback 이 티켓을 open 으로 복원하지 못함."


@requires_git
def test_rollback_restores_preexisting_clean_gitattributes(
        board, tmp_path, monkeypatch):
    """선재 clean `.gitattributes` backfill도 push rollback 뒤 원래 bytes/index로 돌아간다."""
    bare = _bare(tmp_path, "bare-root-rb-existing")
    board_dir = _make_board_git(tmp_path, remote=bare, gitattributes=False)
    attrs = board_dir / ".gitattributes"
    original = b"*.md text eol=lf\n"
    attrs.write_bytes(original)
    _git(["add", "--", ".gitattributes"], board_dir)
    _git(["commit", "-qm", "seed custom attributes"], board_dir)
    _git(["push", "-q", "origin", "main"], board_dir)
    expected = board._board_git_expected_backfill_state(
        ".gitattributes", board._BoardGitRootFileState(True, original),
    )
    assert expected is not None
    assert expected.contents == (
        original + b"\n" + board._BOARD_GITATTRIBUTES_BLOCK.encode("utf-8")
    )
    assert b"*.md text eol=lf\n" in expected.contents
    monkeypatch.setattr(
        board, "_board_git_push",
        _racing_push(board, tmp_path, bare, name="root-rb-existing-other"))

    assert board.cmd_claim(_claim_args()) == 1

    assert attrs.read_bytes() == original, "rollback 뒤 engine append 가 워킹트리에 남음."
    assert ".gitattributes" not in _porcelain(board_dir), \
        f"rollback 뒤 선재 루트 파일 index 상태가 dirty: {_porcelain(board_dir)}"


@requires_git
@pytest.mark.parametrize("preexisting", [None, b"*.md text eol=lf\n"])
def test_backfill_writes_exactly_the_bytes_rollback_expects(
        board, tmp_path, preexisting):
    """backfill 이 **쓴 바이트**와 롤백이 되계산하는 바이트가 같다 (플랫폼 번역 금지).

    롤백은 워킹트리 bytes 가 `_board_git_expected_backfill_state` 와 정확히 같을 때만 원복한다.
    append 가 플랫폼 텍스트 모드로 열려 `\\n` 이 `\\r\\n` 으로 번역되면 이 대조가 어긋나
    "제3자 변경" 으로 판정돼 backfill 잔재가 워킹트리에 눌러앉는다(Windows 실측). 선재 파일
    유무 두 갈래(separator 분기)를 함께 못박는다.
    """
    bare = _bare(tmp_path, f"bare-backfill-bytes-{'pre' if preexisting else 'new'}")
    board_dir = _make_board_git(
        tmp_path, remote=bare, gitattributes=False, gitignore=False)
    if preexisting is not None:
        attrs = board_dir / ".gitattributes"
        attrs.write_bytes(preexisting)
        _git(["add", "--", ".gitattributes"], board_dir)
        _git(["commit", "-qm", "seed custom attributes"], board_dir)

    snapshot = board._board_git_root_files_snapshot()
    written = board._ensure_board_root_files()

    assert set(written) == {".gitattributes", ".gitignore"}, written
    for name in written:
        expected = board._board_git_expected_backfill_state(name, snapshot.states[name])
        assert expected is not None
        assert (board_dir / name).read_bytes() == expected.contents, \
            f"{name} backfill 바이트가 롤백 기대와 다름 — 개행 번역/추가 write 의심."


@requires_git
def test_rollback_removes_a_read_only_backfill_file(board, tmp_path, capsys):
    """부재였던 루트 파일은 read-only 여도 워킹트리에서 **실제로** 걷힌다.

    맨 `unlink` 는 read-only 속성 파일을 Windows 가 거부하고, 쓰기 권한 없는 디렉터리 안에서는
    POSIX 도 거부한다 — 그 실패를 경고로만 흘리면 backfill 파일이 잔재로 남아 다음 mutation 의
    ensure 가 no-write 한다.
    """
    bare = _bare(tmp_path, "bare-root-rb-readonly")
    board_dir = _make_board_git(
        tmp_path, remote=bare, gitattributes=False, gitignore=False)
    snapshot = board._board_git_root_files_snapshot()
    attrs = board_dir / ".gitattributes"
    expected = board._board_git_expected_backfill_state(
        ".gitattributes", snapshot.states[".gitattributes"])
    attrs.write_bytes(expected.contents)
    snapshot.backfilled.append(".gitattributes")
    os.chmod(attrs, stat.S_IREAD)
    os.chmod(board_dir, stat.S_IREAD | stat.S_IEXEC)
    try:
        with pytest.raises(OSError):
            attrs.unlink()          # 픽스처가 실제로 삭제를 막는지 먼저 확인.

        restored = board._board_git_restore_root_files(snapshot)
    finally:
        os.chmod(board_dir, 0o700)

    assert restored == (".gitattributes",)
    assert not attrs.exists(), "rollback 뒤 read-only backfill 파일이 워킹트리에 남음."
    assert "복원 실패" not in capsys.readouterr().err


@requires_git
def test_rollback_does_not_restore_root_file_without_backfill(
        board, tmp_path, capsys):
    """이미 정합해 실제 backfill하지 않은 루트 파일은 실패 hook의 후속 편집을 보존한다."""
    bare = _bare(tmp_path, "bare-root-rb-no-backfill")
    board_dir = _make_board_git(tmp_path, remote=bare)
    attrs = board_dir / ".gitattributes"
    hook_line = "# hook-owned-after-snapshot\n"
    hook = board_dir / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf '%s' '{hook_line.rstrip()}' >> .gitattributes\n"
        "git add -- .gitattributes\n"
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    assert board.cmd_claim(_claim_args()) == 1

    assert hook_line.strip() in attrs.read_text(encoding="utf-8"), \
        "실제 backfill하지 않은 .gitattributes를 rollback이 스냅샷으로 덮어씀."
    assert ".gitattributes" in _porcelain(board_dir), \
        "실패 hook의 루트 파일 index/워킹트리 변경이 rollback에서 사라짐."
    assert "claim race lost" in capsys.readouterr().err


@requires_git
def test_rollback_preserves_and_warns_on_post_backfill_root_edit(
        board, tmp_path, capsys):
    """실제 backfill 파일도 snapshot 이후 제3자 bytes/index면 보존하고 loud 경고한다."""
    bare = _bare(tmp_path, "bare-root-rb-third-party")
    board_dir = _make_board_git(tmp_path, remote=bare, gitattributes=False)
    attrs = board_dir / ".gitattributes"
    hook_line = "# third-party-after-backfill\n"
    hook = board_dir / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf '%s' '{hook_line.rstrip()}' >> .gitattributes\n"
        "git add -- .gitattributes\n"
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    assert board.cmd_claim(_claim_args()) == 1

    text = attrs.read_text(encoding="utf-8")
    assert "areas.md merge=union" in text, "engine backfill 자체가 예상 밖으로 사라짐."
    assert hook_line.strip() in text, "snapshot 이후 제3자 편집을 rollback이 덮어씀."
    assert ".gitattributes" in _porcelain(board_dir), \
        "보존해야 할 제3자 루트 파일 변경이 index/워킹트리에서 사라짐."
    err = capsys.readouterr().err
    assert "스냅샷 이후 제3자 변경을 보존했다" in err, err
    assert "덮어쓰지 않음" in err, err


@requires_git
def test_rollback_warns_when_root_file_snapshot_capture_raises_oserror(
        board, tmp_path, monkeypatch, capsys):
    """snapshot OSError(None)도 restore seam을 지나 loud 경고 후 나머지 rollback을 마친다."""
    bare = _bare(tmp_path, "bare-root-snapshot-oserror")
    board_dir = _make_board_git(tmp_path, remote=bare)
    attrs = board_dir / ".gitattributes"
    roots_before = {
        name: (board_dir / name).read_bytes()
        for name in board._BOARD_GIT_ROOT_FILES
    }
    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    # 루트 파일 스냅샷 판독은 공유 읽기 seam 을 지난다([[T-0729]]) — 주입도 그 자리에 건다.
    real_read_bytes = board.file_lock.read_bytes_shared
    failed = {"once": False}

    def _fail_snapshot_once(path) -> bytes:
        if Path(path) == attrs and not failed["once"]:
            failed["once"] = True
            raise OSError("forced root snapshot failure")
        return real_read_bytes(path)

    monkeypatch.setattr(board.file_lock, "read_bytes_shared", _fail_snapshot_once)
    hook = board_dir / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    assert board.cmd_claim(_claim_args()) == 1

    assert failed["once"] is True
    assert "캡처하지 못해 정확히 복원하지 못했다" in capsys.readouterr().err
    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == head_before
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md"))
    assert not list((board_dir / "tickets" / "claimed").glob("T-0001-*.md"))
    assert _porcelain(board_dir) == {}
    assert {
        name: real_read_bytes(board_dir / name)
        for name in board._BOARD_GIT_ROOT_FILES
    } == roots_before


@requires_git
def test_rollback_leaves_head_at_anchor_only_for_own_commit(board, tmp_path):
    """`reset --soft` 는 **HEAD == 내 claim 커밋** 일 때만 — 남의 커밋은 되돌리지 않는다.

    ADR-0073 이 명시한 가드다. 직렬화 락이 이 상황을 막지만(같은 clone), 락이 없는 경로(외부
    도구·수동 git)에서 HEAD 가 내 커밋이 아닐 수 있다 — 그때 무조건 `reset --soft <anchor>` 하면
    **남의 커밋을 이력에서 떨어뜨린다**. 가드를 지우면 이 테스트가 red 다."""
    bare = _bare(tmp_path, "bare-guard")
    board_dir = _make_board_git(tmp_path, remote=bare)
    anchor = board._board_git_head()
    files = _claim_move(board, board_dir)
    # 다른 주체가 그 사이 board git 에 커밋했다(HEAD ≠ 내 claim 커밋).
    (board_dir / "someone-else.md").write_text("theirs\n", encoding="utf-8")
    _git(["add", "-A", "--", "someone-else.md"], board_dir)
    _git(["commit", "-qm", "someone else's commit"], board_dir)
    foreign_head = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()

    board._board_git_claim_rollback(anchor, files, claim_commit="0" * 40)

    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == foreign_head, \
        "HEAD 가 내 claim 커밋이 아닌데 reset 했다 — 남의 커밋을 이력에서 떨어뜨림."
    assert (board_dir / "someone-else.md").exists(), "남의 커밋 산출물이 사라짐."
    # 이력은 안 건드려도 *파일 복원* 은 한다(거짓 소유 0).
    assert files.old.exists() and not files.new.exists(), \
        "이력 무조작 경로에서 티켓 파일 복원까지 건너뜀."


@requires_git
def test_rollback_warns_when_refresh_pull_fails(board, tmp_path, monkeypatch, capsys):
    """롤백 후 winner 반영 pull 이 **실패**해도 loud 하다 — 약속의 나머지 절반 (codex must-fix).

    사전 감지는 *추적* 변경만 본다. untracked 경로 충돌은 그 관문을 통과하고도 pull 이 rc≠0 라,
    rc 를 안 보면 stale 뷰가 **무경고**로 남는다. 사유 판정은 새로 만들지 않고 claim prefetch 와
    같은 `_classify_pull_failure` 를 재사용한다."""
    bare = _bare(tmp_path, "bare-refresh3")
    board_dir = _make_board_git(tmp_path, remote=bare)
    # 우리 쪽엔 아직 커밋 안 된(untracked) 파일이 있고, winner 가 **같은 경로**를 원격에 올린다.
    collide = board_dir / "tickets" / "open" / "T-0011-t.md"
    # 유효한 티켓 본문이어야 한다 — 실패 경로의 `refresh_board()` 가 open/ 을 전부 파싱한다.
    local_draft = _TICKET_TEXT.format(tid="T-0011", status="open") + "\n로컬 초안\n"
    collide.write_text(local_draft, encoding="utf-8")
    winner = tmp_path / "refresh3-winner"
    real_push = board._board_git_push

    def _winner_pushes_colliding_path():
        _git(["clone", "-q", str(bare), str(winner)], tmp_path)
        (winner / "tickets" / "open" / "T-0011-t.md").write_text(
            _TICKET_TEXT.format(tid="T-0011", status="open"), encoding="utf-8")
        _git(["add", "-A"], winner)
        _git(["commit", "-qm", "winner adds T-0011"], winner)
        _git(["push", "-q", "origin", "main"], winner)
        return real_push()          # → non-FF 거부 → 롤백 → refresh pull 이 충돌로 실패.

    monkeypatch.setattr(board, "_board_git_push", _winner_pushes_colliding_path)

    assert board.cmd_claim(_claim_args()) == 1
    err = capsys.readouterr().err
    assert "로컬 board 뷰 stale" in err, f"refresh pull 실패가 무경고로 묻힘: {err!r}"
    assert "미커밋 파일이 통합을 막음" in err, f"사유가 부정확하다: {err!r}"
    assert "T-0011-t.md" in err, f"막고 있는 파일을 지목하지 않음: {err!r}"
    assert collide.read_text(encoding="utf-8") == local_draft, \
        "경고 대신 사용자의 미추적 파일을 치웠다(또는 덮어썼다)."


@requires_git
def test_rollback_refreshes_local_view_to_winner(board, tmp_path, monkeypatch, capsys):
    """롤백 마무리가 winner 를 로컬에 반영한다 — 패자의 board 뷰가 stale 로 남지 않는다 (D4).

    push 거부 직후엔 원격-추적 ref 가 *정의상* stale 이라, fetch 없이 `behind` 를 읽으면 항상
    0 이 되어 winner 반영도 stale 경고도 **한 번도 안 나간다**(reviewer must-fix·옛 코드는
    롤백 후 pull 로 claimed/ 를 반영했으므로 회귀였다)."""
    bare = _bare(tmp_path, "bare-refresh")
    board_dir = _make_board_git(tmp_path, remote=bare)
    winner = tmp_path / "winner"

    def _winner_claims_then_push():
        _git(["clone", "-q", str(bare), str(winner)], tmp_path)
        (winner / "tickets" / "claimed").mkdir(parents=True, exist_ok=True)
        (winner / "tickets" / "open" / "T-0001-t.md").rename(
            winner / "tickets" / "claimed" / "T-0001-t.md")
        _git(["add", "-A"], winner)
        _git(["commit", "-qm", "winner claims T-0001"], winner)
        _git(["push", "-q", "origin", "main"], winner)
        return real_push()

    real_push = board._board_git_push
    monkeypatch.setattr(board, "_board_git_push", _winner_claims_then_push)

    assert board.cmd_claim(_claim_args()) == 1, "push 가 거부됐는데 확정함."

    # 로컬 뷰가 winner 상태로 갱신돼야 한다(clean tree 라 pull 가능).
    assert list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "롤백 후 winner 의 claim 이 로컬에 반영되지 않음 — 뷰가 stale 로 방치됨(D4)."
    assert not list((board_dir / "tickets" / "open").glob("T-0001-*.md"))
    # 성공 경로는 조용하다 — 실패 경로 경고를 넣으면서 정상 경로가 시끄러워지면 안 된다(회귀).
    assert "로컬 board 뷰 stale" not in capsys.readouterr().err, \
        "refresh 가 성공했는데 stale 경고가 나옴(거짓 경보)."


@requires_git
def test_rollback_warns_loudly_when_view_cannot_refresh(board, tmp_path, monkeypatch, capsys):
    """추적 dirty 라 winner 를 못 당기면 **loud 하게** 알린다 — 조용한 stale 금지 (D4)."""
    bare = _bare(tmp_path, "bare-refresh2")
    board_dir = _make_board_git(tmp_path, remote=bare)
    (board_dir / "notes.md").write_text("original\nmy edit\n", encoding="utf-8")
    monkeypatch.setattr(board, "_board_git_push",
                        _racing_push(board, tmp_path, bare, name="refresh2-other"))

    assert board.cmd_claim(_claim_args()) == 1
    err = capsys.readouterr().err
    assert "stale" in err and "pull --rebase" in err, \
        f"당길 수 없는데 stale 경고가 안 나옴(조용한 stale): {err!r}"
    assert (board_dir / "notes.md").read_text(encoding="utf-8") == "original\nmy edit\n", \
        "경고 대신 사용자 편집을 치웠다(auto-stash 기각 위반)."


# ════════════════════════════════════════════════════════════════════════
# D5 — 잔여 차단은 사유가 정확하다 (dirty / rebase / offline / upstream 없음)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_behind_plus_tracked_dirty_blocks_with_accurate_reason(board, tmp_path, capsys):
    """`behind>0 ∧ 추적 dirty` = 유일한 잔여 차단 — 사유·behind·막는 파일이 안내에 나온다.

    이 조합만 남기는 게 ADR-0073 의 결론이다(정직한 차단). 안내는 **일괄 `add -A` 가 아니라**
    막고 있는 경로를 지목해야 한다 — 공유 board 에서 일괄 커밋은 남의 미완성 편집을 대신
    커밋시킨다."""
    bare = _bare(tmp_path, "bare-behind")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_remote(tmp_path, bare)
    notes = board_dir / "notes.md"
    notes.write_text("original\nmy edit\n", encoding="utf-8")   # 추적 변경 → rebase 불가.

    rc = board.cmd_claim(_claim_args())

    assert rc == 1, "behind>0 + 추적 dirty 인데 claim 이 진행됨(통합 실패를 무시)."
    err = capsys.readouterr().err
    assert "1 커밋" in err, f"behind 수치가 안내에 없음: {err!r}"
    assert "notes.md" in err, f"막고 있는 파일을 지목하지 않음: {err!r}"
    assert "offline 아님" in err, f"네트워크 문제로 오판될 안내: {err!r}"
    assert "offline — board 도달 불가" not in err, f"offline 메시지 이중출력: {err!r}"
    # 로컬 변경 0 — 차단은 prefetch 단계라 mutation 이 없다.
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        "차단인데 티켓이 open/ 에 없음 — prefetch 가 로컬을 변경함."
    assert notes.read_text(encoding="utf-8") == "original\nmy edit\n", \
        "차단 경로가 사용자 편집을 건드림."


@requires_git
def test_dirty_diagnostics_lists_sample_and_total(board, tmp_path, capsys):
    """더러운 파일이 많으면 **표본 5건 + 총계**로 낸다 (안내가 묻히지도, 비지도 않게)."""
    bare = _bare(tmp_path, "bare-many")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_remote(tmp_path, bare)
    (board_dir / "notes.md").write_text("original\nedit\n", encoding="utf-8")
    for n in range(7):
        (board_dir / f"scratch-{n}.md").write_text("x\n", encoding="utf-8")

    assert board.cmd_claim(_claim_args()) == 1
    err = capsys.readouterr().err
    assert "미커밋 8건" in err, f"총계가 안 나옴: {err!r}"
    assert "외 3건" in err, f"표본 상한(5) + 나머지 표기가 안 나옴: {err!r}"


@requires_git
def test_untracked_path_conflict_is_dirty_not_offline(board, tmp_path, capsys):
    """원격이 들고 오는 경로에 **로컬 untracked 파일**이 있어 pull 이 거부되면 — offline 아님.

    git 은 이때 "untracked working tree files would be overwritten" 로 통합을 거부한다. 네트워크
    문제가 아니라 로컬 파일 충돌인데, 옛 분류는 '추적 변경이 없다'는 이유로 **offline** 이라
    진단하고 네트워크를 확인하라는 틀린 안내를 냈다(D5 잔여·codex must-fix). 분류는 문자열이
    아니라 구조(fetch 재시도 성공 = 네트워크 정상)로 한다."""
    bare = _bare(tmp_path, "bare-untracked")
    board_dir = _make_board_git(tmp_path, remote=bare)
    # 다른 clone 이 새 티켓 파일을 원격에 올린다(= 우리 쪽으로 들어올 추적 파일).
    other = tmp_path / "untracked-other"
    _git(["clone", "-q", str(bare), str(other)], tmp_path)
    (other / "tickets" / "open" / "T-0008-t.md").write_text(
        _TICKET_TEXT.format(tid="T-0008", status="open"), encoding="utf-8")
    _git(["add", "-A"], other)
    _git(["commit", "-qm", "other adds T-0008"], other)
    _git(["push", "-q", "origin", "main"], other)
    # 우리 쪽엔 **같은 경로**가 untracked 로 존재한다(추적 변경은 0).
    collide = board_dir / "tickets" / "open" / "T-0008-t.md"
    collide.write_text("내 로컬 초안\n", encoding="utf-8")
    assert not board._board_git_has_tracked_changes(), "전제: 추적 변경은 없어야 한다."

    result = board._board_git_claim_prefetch("T-0001")

    assert result.block == board._CLAIM_BLOCK_DIRTY, \
        f"untracked 경로 충돌이 dirty 로 분류되지 않음(offline 오진): {result.block}"
    rc = board.cmd_claim(_claim_args())
    assert rc == 1
    err = capsys.readouterr().err
    assert "offline — board 도달 불가" not in err, f"네트워크 문제로 오진: {err!r}"
    assert "T-0008-t.md" in err, f"막고 있는 파일을 지목하지 않음: {err!r}"
    assert "옮겨라" in err or "옮기" in err, \
        f"미추적 파일의 처방(커밋 또는 치우기)이 안내되지 않음: {err!r}"
    assert collide.read_text(encoding="utf-8") == "내 로컬 초안\n", \
        "차단 경로가 사용자의 미추적 파일을 건드림."


@requires_git
def test_pull_failure_with_unreachable_remote_is_offline(board, tmp_path, monkeypatch):
    """진짜 도달 불가는 여전히 offline — untracked 오진을 고치면서 반대편이 새지 않는다.

    fetch 재시도 프로브가 분류의 축이다: 원격이 사라졌으면 프로브도 실패하므로 정확히 offline
    이 된다(위 테스트와 **쌍**으로 고정해야 오진이 재유입되지 않는다·codex)."""
    bare = _bare(tmp_path, "bare-realoff")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_remote(tmp_path, bare, name="realoff-other")   # behind>0 로 만든다.
    gone = tmp_path / "bare-realoff-gone"

    def _pull_fails_and_remote_vanishes():
        bare.rename(gone)   # pull 도중 원격 소실(도달 불가).
        return subprocess.CompletedProcess([], 1, "", "could not read from remote repository")

    monkeypatch.setattr(board, "_board_git_pull_rebase", _pull_fails_and_remote_vanishes)

    result = board._board_git_claim_prefetch("T-0001")

    assert result.block == board._CLAIM_BLOCK_OFFLINE, \
        f"원격 도달 불가인데 offline 이 아님: {result.block}"
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), "로컬 변경 0 위반."


@requires_git
def test_unclassifiable_pull_failure_is_not_called_offline(board, tmp_path, capsys, monkeypatch):
    """네트워크 정상 + 미커밋 0 인데 pull 이 실패하면 — offline 이라 **거짓말하지 않는다**.

    사유 4분 어디에도 정직하게 안 들어가는 잔여(액션이 "직접 돌려 원인을 보라" 로 다르다)라
    별도 사유로 두고 git 출력을 함께 낸다."""
    bare = _bare(tmp_path, "bare-unknown")
    _make_board_git(tmp_path, remote=bare)
    _advance_remote(tmp_path, bare, name="unknown-other")
    monkeypatch.setattr(board, "_board_git_pull_rebase",
                        lambda: subprocess.CompletedProcess(
                            [], 1, "", "fatal: refusing to merge unrelated histories"))

    result = board._board_git_claim_prefetch("T-0001")
    assert result.block == board._CLAIM_BLOCK_INTEGRATION, \
        f"원인 미상 통합 실패가 잘못 분류됨: {result.block}"

    assert board.cmd_claim(_claim_args()) == 1
    err = capsys.readouterr().err
    assert "offline — board 도달 불가" not in err, f"거짓 offline 진단: {err!r}"
    assert "네트워크는 정상" in err and "unrelated histories" in err, \
        f"원인 확인 경로가 안내되지 않음: {err!r}"


@requires_git
def test_mid_rebase_is_first_check_and_advises_rebase(board, tmp_path, capsys):
    """mid-rebase = **1순위 선체크** — checkout 오안내 없이 abort/continue 를 안내하고 커밋 0.

    mid-rebase 에서 `git commit -- <paths>` 는 rc=0 으로 detached rebase HEAD 위에 커밋을
    만든다(architect 실측). 그래서 이 선체크는 어떤 board-git mutation 보다 앞이어야 한다."""
    bare = _bare(tmp_path, "bare-rebase")
    board_dir = _make_board_git(tmp_path, remote=bare)
    # 충돌하는 두 갈래를 만들어 rebase 를 중단 상태로 남긴다.
    _git(["checkout", "-q", "-b", "side"], board_dir)
    (board_dir / "notes.md").write_text("side\n", encoding="utf-8")
    _git(["commit", "-qam", "side edit"], board_dir)
    _git(["checkout", "-q", "main"], board_dir)
    (board_dir / "notes.md").write_text("main\n", encoding="utf-8")
    _git(["commit", "-qam", "main edit"], board_dir)
    conflict = _git(["rebase", "side"], board_dir)
    assert conflict.returncode != 0, "fixture 전제: rebase 가 충돌로 멈춰야 한다."
    assert board._board_git_rebase_in_progress() is True

    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    rc = board.cmd_claim(_claim_args())

    assert rc == 1, "mid-rebase 인데 claim 이 진행됨 — rebase HEAD 위 커밋 위험."
    err = capsys.readouterr().err
    assert "rebase" in err and "--abort" in err, f"rebase 처방이 안내되지 않음: {err!r}"
    assert "checkout <branch>" not in err, f"mid-rebase 에 checkout 오안내(2단 오진): {err!r}"
    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == head_before, \
        "mid-rebase 에서 claim 이 커밋을 만듦 — 1순위 선체크 누락."


@requires_git
def test_missing_upstream_is_its_own_reason_not_offline(board, tmp_path, capsys):
    """upstream 미설정 = offline 과 **다른 사유** (D5 오진 폐쇄)."""
    bare = _bare(tmp_path, "bare-noups")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _git(["branch", "--unset-upstream"], board_dir)

    result = board._board_git_claim_prefetch("T-0001")
    assert result.block == board._CLAIM_BLOCK_NO_UPSTREAM, \
        f"upstream 미설정인데 사유가 {result.block}"

    assert board.cmd_claim(_claim_args()) == 1
    err = capsys.readouterr().err
    assert "upstream" in err and "push -u" in err, f"upstream 처방이 안내되지 않음: {err!r}"
    assert "offline — board 도달 불가" not in err, f"offline 으로 오진: {err!r}"


# ════════════════════════════════════════════════════════════════════════
# 선점 감지 — 읽기 전용(로컬 변경 0)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_remote_preemption_is_race_lost_without_local_change(board, tmp_path, capsys):
    """원격에서 이미 claimed 면 race-lost — **로컬은 한 바이트도 안 바뀐다** (읽기 전용 감지).

    옛 판정은 winner 를 pull 로 끌어와야 보였다(통합 성공에 의존). 신 판정은 `ls-tree` 로 원격
    트리를 직접 읽으므로 dirty·behind 와 무관하게 성립한다 — 등가 이상."""
    bare = _bare(tmp_path, "bare-race")
    board_dir = _make_board_git(tmp_path, remote=bare)
    other = tmp_path / "other-clone"
    _git(["clone", "-q", str(bare), str(other)], tmp_path)
    (other / "tickets" / "claimed").mkdir(parents=True, exist_ok=True)
    (other / "tickets" / "open" / "T-0001-t.md").rename(
        other / "tickets" / "claimed" / "T-0001-t.md")
    _git(["add", "-A"], other)
    _git(["commit", "-qm", "other claims T-0001"], other)
    _git(["push", "-q", "origin", "main"], other)

    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    rc = board.cmd_claim(_claim_args())

    assert rc == 1, "원격 선점인데 claim 이 성공함 — 중복작업 방지 붕괴."
    assert "claim race lost" in capsys.readouterr().err
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        "race-lost 인데 로컬 티켓이 open/ 에 없음(로컬 변경 0 위반)."
    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == head_before, \
        "race-lost 판정이 로컬 HEAD 를 전진시킴(읽기 전용 위반)."


# ════════════════════════════════════════════════════════════════════════
# D6 — push 예외 시 고아 claim 금지
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_push_timeout_after_remote_accepted_keeps_ownership(board, tmp_path, monkeypatch):
    """push 가 timeout 예외로 끝나도 **원격에 반영됐으면 확정**한다 (고아 claim 폐쇄).

    옛 코드는 예외를 곧장 실패로 보고 롤백해, 원격은 claimed·로컬은 open 인 고아 claim 을
    만들었다. 이제 `ls-remote` 로 원격 tip 을 재확인한다."""
    bare = _bare(tmp_path, "bare-d6a")
    board_dir = _make_board_git(tmp_path, remote=bare)
    real_git = board._board_git

    def _push_then_timeout():
        real_git(["push"])   # 원격엔 실제로 반영된다.
        raise subprocess.TimeoutExpired(cmd="git push", timeout=30)

    monkeypatch.setattr(board, "_board_git_push", _push_then_timeout)

    rc = board.cmd_claim(_claim_args())

    assert rc == 0, "원격이 이미 받은 push 인데 롤백함 — 고아 claim 재발(D6)."
    assert list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "확정인데 로컬 티켓이 claimed/ 에 없음."
    assert "tickets/claimed/T-0001-t.md" in _git(
        ["ls-tree", "-r", "--name-only", "main"], bare).stdout, \
        "fixture 전제: 원격에 claim 이 반영돼 있어야 한다."


@requires_git
def test_push_timeout_then_unrelated_remote_commit_still_keeps_ownership(
        board, tmp_path, monkeypatch):
    """push 수락 → 그 사이 **무관 커밋**이 원격에 얹힘 → timeout. 그래도 확정한다 (D6 절반 폐쇄).

    재확인 술어가 tip **동일성**이면 이 경우 `tip != claim_commit` 라 롤백해 고아 claim 을 다시
    만든다(원격은 내 소유·로컬은 open). 정답은 **조상 관계**(`merge-base --is-ancestor`) 다."""
    bare = _bare(tmp_path, "bare-d6c")
    board_dir = _make_board_git(tmp_path, remote=bare)
    real_push = board._board_git_push

    def _push_then_others_then_timeout():
        real_push()                                        # 내 claim 이 원격에 수락됨.
        _advance_remote(tmp_path, bare, name="d6c-other")  # 그 위에 무관 커밋 → tip 이 바뀜.
        raise subprocess.TimeoutExpired(cmd="git push", timeout=30)

    monkeypatch.setattr(board, "_board_git_push", _push_then_others_then_timeout)

    rc = board.cmd_claim(_claim_args())

    assert rc == 0, "원격이 내 claim 커밋을 포함하는데 롤백함 — tip 동일성 술어의 고아 claim."
    assert list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "확정인데 로컬 티켓이 claimed/ 에 없음."


@requires_git
def test_push_timeout_without_remote_effect_rolls_back(board, tmp_path, monkeypatch):
    """push 가 예외인데 원격 tip 이 내 커밋이 아니면 **롤백**한다 (거짓 소유 0·D6 의 반대편)."""
    bare = _bare(tmp_path, "bare-d6b")
    board_dir = _make_board_git(tmp_path, remote=bare)

    def _push_timeout():
        raise subprocess.TimeoutExpired(cmd="git push", timeout=30)

    monkeypatch.setattr(board, "_board_git_push", _push_timeout)

    rc = board.cmd_claim(_claim_args())

    assert rc == 1, "원격 미반영 push 예외인데 소유를 확정함 — 거짓 소유."
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        "롤백인데 티켓이 open/ 으로 복원 안 됨."
    assert "tickets/claimed" not in _git(
        ["ls-tree", "-r", "--name-only", "main"], bare).stdout


# ════════════════════════════════════════════════════════════════════════
# best-effort 7 callsite / 8 mutation surface — 같은 스코프 불변식
# ════════════════════════════════════════════════════════════════════════

def _new_args(title: str) -> argparse.Namespace:
    return argparse.Namespace(title=title, touches=None, depends=None, tag=None,
                              estimate="small", prefix=None, user=None, session=None)


@requires_git
@pytest.mark.parametrize("mutation", ["complete", "block", "unclaim", "unblock"])
def test_best_effort_transitions_commit_only_their_paths(board, tmp_path, mutation):
    """complete/block/unclaim/unblock 커밋도 **그 전이 두 경로만** 담는다.

    claim 차단을 풀면 board 는 상시 dirty 가 된다 — best-effort 가 board 전체를 쓸어담는 채로
    남으면 누출 노출이 오늘보다 커지므로 같은 티켓에서 함께 스코프화했다."""
    bare = _bare(tmp_path, f"bare-be-{mutation}")
    board_dir = _make_board_git(tmp_path, remote=bare)
    tickets = board_dir / "tickets"

    if mutation in ("complete", "unclaim"):
        assert board.cmd_claim(_claim_args()) == 0
    elif mutation == "unblock":
        assert board.cmd_block(argparse.Namespace(id="T-0001", reason="r")) == 0
    dirty = _seed_dirty_three(board_dir)

    if mutation == "complete":
        rc = board.cmd_complete(argparse.Namespace(
            id="T-0001", tests_pass=True, allow_missing_log=True, allow_untested=False,
            repo="me", slot=1))
        expected = {"tickets/claimed/T-0001-t.md", "tickets/done/T-0001-t.md"}
    elif mutation == "block":
        rc = board.cmd_block(argparse.Namespace(id="T-0001", reason="r"))
        expected = {"tickets/open/T-0001-t.md", "tickets/blocked/T-0001-t.md"}
    elif mutation == "unclaim":
        rc = board.cmd_unclaim(argparse.Namespace(id="T-0001", repo="me", slot=1))
        expected = {"tickets/claimed/T-0001-t.md", "tickets/open/T-0001-t.md"}
    else:
        rc = board.cmd_unblock(argparse.Namespace(id="T-0001"))
        expected = {"tickets/blocked/T-0001-t.md", "tickets/open/T-0001-t.md"}

    assert rc == 0, f"{mutation} 이 실패함(best-effort 는 무차단이어야 한다)."
    assert _head_files(board_dir) == expected, \
        f"{mutation} 커밋 스코프가 전이 경로를 넘음: {_head_files(board_dir)}"
    after = _porcelain(board_dir)
    for path, code in dirty.items():
        assert after.get(path) == code, \
            f"{mutation} 이 무관 dirty 를 건드림: {path} {code!r} → {after.get(path)!r}"
    assert (tickets / "_template.md").exists()


@requires_git
def test_new_commits_only_the_created_ticket(board, tmp_path):
    """`new` 커밋엔 방금 만든 티켓과 그 묶음 장부만 담긴다 (무관 dirty 미동반).

    발행은 운영 단위 귀속을 함께 쓴다 — 크기 1 장부도 board-git 공유 파일이라 같은 스코프
    채널로 실린다(스코프가 넓어진 게 아니라 이 발행이 만든 파일이 둘이다).
    """
    bare = _bare(tmp_path, "bare-be-new")
    board_dir = _make_board_git(tmp_path, remote=bare)
    dirty = _seed_dirty_three(board_dir)

    assert board.cmd_new(_new_args("새 티켓")) == 0

    created = _head_files(board_dir)
    assert len(created) == 2, f"new 커밋 스코프가 발행 산출을 넘음: {created}"
    assert sum(1 for path in created if path.startswith("tickets/open/")) == 1, created
    assert sum(1 for path in created if path.startswith("tickets/clusters/")) == 1, created
    after = _porcelain(board_dir)
    for path, code in dirty.items():
        assert after.get(path) == code, f"new 가 무관 dirty 를 건드림: {path}"


@requires_git
def test_promote_commits_only_the_promoted_ticket(board, tmp_path):
    """`promote` 커밋엔 승격된 open/ 경로만 담긴다 — draft 경로는 스코프에서 제외(T-0198)."""
    bare = _bare(tmp_path, "bare-be-promote")
    board_dir = _make_board_git(tmp_path, remote=bare)
    drafts = board_dir / "tickets" / ".drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    draft = drafts / "T-0009-draft.md"
    draft.write_text(_TEMPLATE_TEXT.replace("T-NNNN", "T-0009").replace(
        "status: open", "status: draft"), encoding="utf-8")
    dirty = _seed_dirty_three(board_dir)

    assert board.cmd_promote(argparse.Namespace(id="T-0009")) == 0

    assert _head_files(board_dir) == {
        "tickets/open/T-0009-draft.md", "tickets/clusters/C-T-0009.md",
    }, f"promote 커밋 스코프가 승격 산출을 넘음: {_head_files(board_dir)}"
    after = _porcelain(board_dir)
    for path, code in dirty.items():
        assert after.get(path) == code, f"promote 가 무관 dirty 를 건드림: {path}"


@requires_git
def test_best_effort_catches_up_when_remote_advanced(board, tmp_path):
    """best-effort 가 **매번 따라잡는다** — 원격이 앞서 있어도 다음 mutation 이 통합+push 한다.

    "다음 mutation 이 catch-up 한다"(ADR-0033·코드 주석·domain)는 약속이 성립하려면 통합 시도가
    **fetch 를 포함**해야 한다. pull 여부를 원격-추적 ref 기반 `behind` 로 가르면 이 경로엔
    fetch 가 없어 ref 가 영구 stale → behind 항상 0 → pull 을 영영 안 타고 push 가 계속
    non-FF 로 밀린다(reviewer 1:1 실측: 그 구현에선 아래 두 단언이 모두 False)."""
    bare = _bare(tmp_path, "bare-catchup")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_remote(tmp_path, bare, name="catchup-other")   # 원격이 앞선다(로컬은 모른다).

    for tid in ("T-0005", "T-0006"):
        path = board_dir / "tickets" / "open" / f"{tid}-t.md"
        path.write_text(_TICKET_TEXT.format(tid=tid, status="open"), encoding="utf-8")
        board._board_git_sync_best_effort(f"new {tid}", (path,))

    remote_ls = _git(["ls-tree", "-r", "--name-only", "main"], bare).stdout
    for tid in ("T-0005", "T-0006"):
        assert f"tickets/open/{tid}-t.md" in remote_ls, \
            f"best-effort 가 원격에 반영되지 않음({tid}) — catch-up 약속이 구조적으로 깨짐."


@requires_git
def test_best_effort_still_pushes_when_tracked_dirty(board, tmp_path):
    """추적 dirty 면 pull 을 건너뛰되 **push 는 시도**한다 — dirty 가 기록을 막지 않는다."""
    bare = _bare(tmp_path, "bare-be-dirty")
    board_dir = _make_board_git(tmp_path, remote=bare)
    (board_dir / "notes.md").write_text("original\nmy edit\n", encoding="utf-8")
    path = board_dir / "tickets" / "open" / "T-0007-t.md"
    path.write_text(_TICKET_TEXT.format(tid="T-0007", status="open"), encoding="utf-8")

    board._board_git_sync_best_effort("new T-0007", (path,))

    assert "tickets/open/T-0007-t.md" in _git(
        ["ls-tree", "-r", "--name-only", "main"], bare).stdout, \
        "추적 dirty 때문에 push 까지 건너뜀 — best-effort 기록이 막힘."
    assert (board_dir / "notes.md").read_text(encoding="utf-8") == "original\nmy edit\n"


def test_ticket_mutations_pass_scoped_paths():
    """메타가드 — board.py 의 best-effort sync **호출 전부**가 경로 인자를 준다 (AST 감사).

    `paths` 는 레거시 호출부(pm_config 의 areas.md 갱신) 호환 때문에 선택 인자다. 그래서
    ticket mutation 이 인자를 빠뜨리면 조용히 board 전체를 커밋하는 옛 동작으로 되돌아간다.
    문자열 매칭(`f"` 포함 줄 세기)은 **비-f-string 메시지로 추가된 새 호출부를 못 본다** —
    호출자 이름 기준 AST 감사로 그 사각을 없앤다."""
    tree = ast.parse((TOOLS / "board.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_board_git_sync_best_effort"]
    # 11 = ticket mutation 9(new·promote·complete·discard·reopen·block·unclaim·unblock +
    # section-add/tier 공용 helper) + `init` 의 areas repo 행 등록(T-0779 — 등록 행도 board git
    # 의 공유 파일이라 같은 스코프 채널로 기록한다) + `cluster new`(묶음 장부·멤버 명세)
    # `cluster replan`은 고정 5단계 계약에서 폐지됐다.
    assert len(calls) == 11, \
        f"best-effort sync 호출이 11곳이 아님(신규/삭제 시 이 가드를 함께 갱신): {len(calls)}"
    for call in calls:
        has_paths = len(call.args) >= 2 or any(kw.arg == "paths" for kw in call.keywords)
        assert has_paths, \
            f"스코프 경로 인자 없이 호출됨 (board.py:{call.lineno})"


# ════════════════════════════════════════════════════════════════════════
# 직렬화 락 — board-git mutation 트랜잭션 상호배제
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_board_git_lock_excludes_concurrent_holder(board, tmp_path):
    """`board_git_lock` 은 실제 OS 락이다 — 보유 중 다른 fd 의 획득 시도가 거부된다.

    flock 은 *open file description* 단위라 같은 프로세스의 다른 fd 도 배제된다(flock(2)) —
    이 성질로 상호배제를 결정적으로 단언한다(두 프로세스 띄우기 없이)."""
    fcntl = pytest.importorskip("fcntl", reason="POSIX flock 없는 플랫폼(Windows) — skip.")
    _make_board_git(tmp_path, remote=_bare(tmp_path, "bare-lock"))
    lock_file = board._board_git_lock_file()

    with board.board_git_lock():
        assert lock_file.exists(), "락 파일이 생성되지 않음."
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    # 해제 후엔 다시 잡힌다(락이 걸린 채 남지 않는다).
    fd = os.open(str(lock_file), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_board_git_lock_is_noop_when_disabled(board, tmp_path):
    """board-git 비활성(legacy·솔로)이면 락 파일조차 만들지 않는다 (현 동작 무변경)."""
    assert board._board_git_enabled() is False
    with board.board_git_lock():
        pass
    assert not board._board_git_lock_file().exists(), \
        "legacy 형상에서 board-git 락 파일이 생김 — no-op 위반."


@requires_git
def test_claim_transaction_runs_inside_board_git_lock(board, tmp_path, monkeypatch):
    """claim 의 git 트랜잭션 **전체**(prefetch 포함)가 락 안에서 돈다 — 인터리브 창 0.

    prefetch 시점에 락이 이미 잡혀 있어야 다른 슬롯의 commit→push→rollback 이 그 사이에 끼어들
    수 없다. 락 순서(board_git_lock → board_lock)의 바깥쪽이 이 락이라는 뜻이기도 하다."""
    fcntl = pytest.importorskip("fcntl", reason="POSIX flock 없는 플랫폼(Windows) — skip.")
    board_dir = _make_board_git(tmp_path, remote=_bare(tmp_path, "bare-lockclaim"))

    def _lock_is_free() -> bool:
        """비블로킹 획득이 되면 아무도 안 잡고 있다는 뜻 (flock 은 fd 단위라 자기 프로세스도 배제)."""
        lock_file = board._board_git_lock_file()
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    # sensitivity: claim 밖에서는 자유롭다 — 아래 단언이 "항상 False" 로 vacuous 하지 않다는 근거.
    assert _lock_is_free() is True, "fixture 전제: 시작 시 락은 비어 있어야 한다."

    seen: dict[str, bool] = {}
    real_prefetch = board._board_git_claim_prefetch

    def _observing_prefetch(ticket_id):
        seen["free_during_prefetch"] = _lock_is_free()
        return real_prefetch(ticket_id)

    monkeypatch.setattr(board, "_board_git_claim_prefetch", _observing_prefetch)
    assert board.cmd_claim(_claim_args()) == 0
    assert seen.get("free_during_prefetch") is False, \
        "prefetch 시점에 board-git 락이 안 잡혀 있음 — 트랜잭션 밖에서 도는 구간 존재."
    assert list((board_dir / "tickets" / "claimed").glob("T-0001-*.md"))
    assert _lock_is_free() is True, "claim 종료 후에도 락이 걸린 채 남음."


# ════════════════════════════════════════════════════════════════════════
# claim 코드 트리 해소 앵커 — 박제 위치·3형상 값 단언·비-git 경고·unclaim 클리어
# (T-0738 R1 리뷰 F-001·F-003·F-004·F-006)
#
# 측정 폭(claim 앵커를 *소비*해 diff 를 재는 쪽)은 `tests/test_ticket_finish.py` 소관이다
# ("── 측정 폭 = claim 시점 rev 앵커" 절). 여기서는 claim 계약 자체 — **어느 트리의 HEAD 를
# 박제하는가**(코드 트리 해소 3형상: task 작업공간·`--repo`/`--slot` 슬롯 worktree·솔로) 와
# 해소 실패/무효화 시 필드 상태만 값으로 단언한다. `board` fixture(`anchor_board_module`)는
# `REPO`/`BOARD_FILE`/`BOARD_LOCK` 만 재앵커하므로, 리스 장부(`LEASES_FILE`)가 필요한 케이스는
# 여기서 직접 tmp 로 재지정한다(다른 44 케이스는 리스 장부를 안 건드려 그 갭이 안 드러났다).
# ════════════════════════════════════════════════════════════════════════

def _claim_anchor_leases_file(tmp_path: Path) -> Path:
    """이 절 전용 리스 장부 경로 — `LEASES_FILE` 를 여기서만 tmp 로 재지정한다."""
    path = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _init_code_git(root: Path, *, seed_text: str) -> str:
    """`root` 를 (코드) git 저장소로 만들고 첫 커밋의 HEAD sha 를 반환한다.

    `seed_text` 로 서로 다른 트리는 서로 다른 HEAD 를 갖는다 — "어느 트리를 쟀나" 를 sha 값으로
    구분하기 위한 장치(`board` fixture 가 이미 `_GIT_IDENTITY` 를 env 로 세팅해 로컬
    `git config user.*` 없이도 커밋된다)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "seed.txt").write_text(seed_text, encoding="utf-8")
    _git(["init", "-q", "-b", "main"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "seed"], root)
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


def _seed_open_ticket(root: Path, tid: str) -> Path:
    """`<root>/.project_manager/board/tickets/{open,claimed,blocked,done}/` 에 open 티켓 하나."""
    tdir = root / ".project_manager" / "board" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tdir / status).mkdir(parents=True, exist_ok=True)
    path = tdir / "open" / f"{tid}-t.md"
    path.write_text(_TICKET_TEXT.format(tid=tid, status="open"), encoding="utf-8")
    return path


def _claimed_ticket(root: Path, tid: str) -> Path:
    return root / ".project_manager" / "board" / "tickets" / "claimed" / f"{tid}-t.md"


@requires_git
def test_claim_anchor_task_shape_stamps_the_slot_worktree_head(board, tmp_path, monkeypatch):
    """`--task` 형상 — 앵커는 그 task 가 보유한 슬롯 worktree HEAD 다(PM 홈 HEAD 가 아니다)."""
    monkeypatch.setattr(board, "LEASES_FILE", _claim_anchor_leases_file(tmp_path))
    repo_head = _init_code_git(tmp_path, seed_text="home\n")
    slot_head = _init_code_git(tmp_path / "work" / "proj_1", seed_text="slot\n")
    assert repo_head != slot_head, "fixture 전제: 두 트리의 HEAD 가 달라야 '어느 트리를 쟀나' 를 값으로 가른다."
    board.LEASES_FILE.write_text(json.dumps({"leases": [
        {"slot": "work/proj_1", "repo": "proj", "session": "mytask", "state": "leased"},
    ]}), encoding="utf-8")
    _seed_open_ticket(tmp_path, "T-9010")

    assert board.cmd_claim(argparse.Namespace(id="T-9010", task="mytask", user="me")) == 0

    fm, _body = board.load_ticket(_claimed_ticket(tmp_path, "T-9010"))
    assert fm["claimed_rev"] == slot_head
    assert fm["claimed_rev"] != repo_head


@requires_git
def test_claim_anchor_slot_shape_stamps_the_worktree_head_even_when_lease_session_differs(
        board, tmp_path, monkeypatch):
    """`--repo`/`--slot` 형상 — 리스 `session` 이 `<repo>_<N>` 이 아니어도(F-001 실측 형상)
    슬롯 worktree HEAD 를 잰다 — 세션 문자열 불일치로 PM 홈에 조용히 접히지 않는다."""
    monkeypatch.setattr(board, "LEASES_FILE", _claim_anchor_leases_file(tmp_path))
    repo_head = _init_code_git(tmp_path, seed_text="home\n")
    slot_head = _init_code_git(tmp_path / "work" / "proj_1", seed_text="slot\n")
    assert repo_head != slot_head
    # 리스 session 은 "proj_1" 이 아니라 임의 문자열("main") — 실측 형상 재현.
    board.LEASES_FILE.write_text(json.dumps({"leases": [
        {"slot": "work/proj_1", "repo": "proj", "session": "main", "state": "leased"},
    ]}), encoding="utf-8")
    _seed_open_ticket(tmp_path, "T-9011")

    assert board.cmd_claim(argparse.Namespace(id="T-9011", repo="proj", slot=1, user="me")) == 0

    fm, _body = board.load_ticket(_claimed_ticket(tmp_path, "T-9011"))
    assert fm["claimed_rev"] == slot_head
    assert fm["claimed_rev"] != repo_head


@requires_git
def test_claim_anchor_solo_shape_stamps_the_repo_head(board, tmp_path, monkeypatch):
    """인자 전무(솔로) — REPO 자체가 코드 트리다(리스 장부 없음·기존 세션/REPO 층 무변경)."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    repo_head = _init_code_git(tmp_path, seed_text="home\n")
    _seed_open_ticket(tmp_path, "T-9012")

    assert board.cmd_claim(argparse.Namespace(id="T-9012", user="me")) == 0

    fm, _body = board.load_ticket(_claimed_ticket(tmp_path, "T-9012"))
    assert fm["claimed_rev"] == repo_head


@requires_git
def test_claim_stamps_the_claim_time_code_tree_head(board, tmp_path, monkeypatch):
    """`claim` 이 그 시점 코드 트리 HEAD 를 `claimed_rev` 로 박제한다(솔로 형상)."""
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    expected = _init_code_git(tmp_path, seed_text="home\n")
    _seed_open_ticket(tmp_path, "T-9013")

    assert board.cmd_claim(argparse.Namespace(id="T-9013", user="me")) == 0

    fm, _body = board.load_ticket(_claimed_ticket(tmp_path, "T-9013"))
    assert fm["claimed_rev"] == expected
    keys = list(fm)
    assert keys[keys.index("claimed_at") + 1] == "claimed_rev", \
        "claim 3종(주체·시각·rev)이 떨어져 기록되면 사람이 티켓에서 앵커를 못 읽는다."


def test_claim_without_a_git_tree_warns_and_still_claims(board, tmp_path, monkeypatch, capsys):
    """비-git 트리에선 필드를 생략하고 경고만 낸다 — 박제 실패가 claim 을 막지 않는다."""
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    _seed_open_ticket(tmp_path, "T-9014")

    assert board.cmd_claim(argparse.Namespace(id="T-9014", user="me")) == 0

    fm, _body = board.load_ticket(_claimed_ticket(tmp_path, "T-9014"))
    assert "claimed_rev" not in fm
    assert "rev 박제 skip" in capsys.readouterr().err


@requires_git
def test_claim_warns_when_bare_claim_folds_to_repo_home_despite_an_active_slot(
        board, tmp_path, monkeypatch, capsys):
    """인자 전무 claim 이 REPO 를 재는데 **이 세션의 활성 슬롯**이 다른 트리면 경고한다(F-001).

    [[T-0793]] 이후 판정은 리스 장부의 존재가 아니라 **이 세션 행이 해소하는 슬롯 경로 값**이다
    (`_warn_claim_code_tree_folded_to_repo_home` 독스트링) — 리스 장부는 있어도 이 세션과
    매칭되는 행이 없는 형상(구 케이스)은 이제 무경고다(홈 자신이 그 세션의 슬롯인 형상과
    값으로 구분되지 않아서다). 여기서는 이 세션("me") 행이 실재하고 다른 슬롯을 가리키게
    해 그 값 대조를 직접 겨눈다.

    값은 여전히 REPO HEAD(기존 폴백 무변경) — 이 테스트는 그 폴백이 *조용하지 않다* 는 것만
    추가로 단언한다."""
    monkeypatch.setattr(board, "LEASES_FILE", _claim_anchor_leases_file(tmp_path))
    repo_head = _init_code_git(tmp_path, seed_text="home\n")
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    # 이 세션("me")의 리스가 REPO 가 아닌 다른 슬롯을 가리키는데, bare claim(kind=none)은
    # 여전히 REPO 트리를 잰다 — 그 접힘이 경고 대상이다.
    board.LEASES_FILE.write_text(json.dumps({"leases": [
        {"slot": "work/other_1", "repo": "other", "session": "me", "state": "leased"},
    ]}), encoding="utf-8")
    _seed_open_ticket(tmp_path, "T-9015")

    assert board.cmd_claim(argparse.Namespace(id="T-9015", user="me")) == 0

    fm, _body = board.load_ticket(_claimed_ticket(tmp_path, "T-9015"))
    assert fm["claimed_rev"] == repo_head
    err = capsys.readouterr().err
    assert "PM 홈(REPO)으로 접혔다" in err
    assert str(tmp_path / "work" / "other_1") in err


def test_claim_stays_silent_when_no_lease_row_matches_this_session(
        board, tmp_path, monkeypatch, capsys):
    """리스 장부는 있어도 이 세션과 매칭되는 행이 없으면 REPO 로 접혀도 무경고다.

    "장부 파일이 존재한다" 는 판정 축이 아니다 — 홈 자신이 이 세션의 슬롯인 형상과 값으로
    구분되지 않으면 상시 오발화하므로 [[T-0793]] 이후 이 형상은 의도적으로 침묵한다."""
    monkeypatch.setattr(board, "LEASES_FILE", _claim_anchor_leases_file(tmp_path))
    _init_code_git(tmp_path, seed_text="home\n")
    board.LEASES_FILE.write_text(json.dumps({"leases": [
        {"slot": "work/other_1", "repo": "other", "session": "other-session", "state": "leased"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    _seed_open_ticket(tmp_path, "T-9019")

    assert board.cmd_claim(argparse.Namespace(id="T-9019", user="me")) == 0

    assert "PM 홈(REPO)으로 접혔다" not in capsys.readouterr().err


@requires_git
def test_unclaim_clears_the_claimed_rev_anchor(board, tmp_path, monkeypatch):
    """unclaim 은 `claimed_by`/`claimed_at` 과 함께 `claimed_rev` 도 비운다(F-003).

    소유가 풀린 티켓에 옛 claim 의 앵커가 남으면, 다음 claim 이 해소에 실패해도(비-git 등)
    stale rev 가 앵커로 살아남아 인터페이스의 '필드 생략' 이 깨진다."""
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    repo_head = _init_code_git(tmp_path, seed_text="home\n")
    _seed_open_ticket(tmp_path, "T-9016")
    assert board.cmd_claim(argparse.Namespace(id="T-9016", user="me")) == 0
    fm, _body = board.load_ticket(_claimed_ticket(tmp_path, "T-9016"))
    assert fm["claimed_rev"] == repo_head

    assert board.cmd_unclaim(argparse.Namespace(id="T-9016")) == 0

    fm, _body = board.load_ticket(
        tmp_path / ".project_manager" / "board" / "tickets" / "open" / "T-9016-t.md")
    assert "claimed_rev" not in fm


def test_reclaim_without_git_pops_a_stale_claimed_rev_from_a_reused_ticket(
        board, tmp_path, monkeypatch):
    """claim 해소 실패(비-git) 시 티켓에 이미 값이 있어도 `claimed_rev` 를 비운다(F-003).

    unclaim 을 거치지 않고 stale 값이 남은 open 티켓(구 claim 주기 재사용) 형상을 직접
    재현한다 — `_cmd_claim_locked` 의 해소-실패 분기가 옛 값을 그대로 두지 않는지 단언한다."""
    monkeypatch.setenv("PM_SESSION_NAME", "me")
    path = _seed_open_ticket(tmp_path, "T-9017")
    fm, body = board.load_ticket(path)
    fm["claimed_rev"] = "a" * 40
    board.dump_ticket(path, fm, body)

    assert board.cmd_claim(argparse.Namespace(id="T-9017", user="me")) == 0

    fm, _body = board.load_ticket(_claimed_ticket(tmp_path, "T-9017"))
    assert "claimed_rev" not in fm
