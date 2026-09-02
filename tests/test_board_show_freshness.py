"""`board show` 원격 대조 표면화 (T-0782 — show 는 원격 대조 0 이던 비대칭을 닫는다).

`list`(T-0379·v1.3.1)는 이미 board-git freshness 1줄(TTL fetch + 전역 behind)을 stderr 로
낸다. `show` 는 완전히 로컬(fetch 0)이었다 — 다중 clone 채택자에서 한 clone 이 complete 하면
다른 clone 의 `show` 는 그 사실을 전혀 모른 채 stale 한 로컬 status 만 보여준다. 이 파일은:

  1. **실 git 저장소 2개**(bare remote + 두 클론)로 한쪽이 티켓을 옮겨 push 한 뒤, 다른 클론의
     `show` 가 그 티켓의 로컬↔원격-추적 status 불일치를 stderr 1줄로 말하는지 실측한다
     (`_print_ticket_remote_mismatch` — 새 판정 아님·`_board_git_remote_ticket_status` 재사용).
  2. 같은 픽스처에서 `list` 의 기존 freshness(`behind N`) 줄을 **처음으로** 회귀 고정한다.
  3. claim 거부 문구(`_claim_block_message`·RACE_LOST)가 로컬 stale 진술 + behind 수치를
     함께 내는지 순수 함수 단위로 단언한다.
  4. offline(원격 소멸) 강등, board 비-git 무출력, 읽기 무변경(해시·HEAD·porcelain), TTL
     (FETCH_HEAD 실측)에서 fetch 생략을 검증한다.

hermetic 패턴은 `test_board_claim_strict.py`/`test_board_git_sync.py` 와 동형(실 board git +
bare remote + REPO monkeypatch). git 부재 환경에선 실 git 케이스를 skip 한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from _git_fixture import remove_git_tree
from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip(단위 케이스는 항상 실행).",
)

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

_TICKET_TEXT = (
    "---\n"
    "id: {tid}\n"
    "title: t\n"
    "status: open\n"
    "claimed_by: null\n"
    "claimed_at: null\n"
    "completed_at: null\n"
    "depends_on: []\n"
    "blocks: []\n"
    "touches: []\n"
    "estimate: small\n"
    "tags: []\n"
    "---\n\n# {tid} — t\n\n## 목표\nx\n"
)


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=False)


def _bare(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / name
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    return bare


def _make_board_git(root: Path, *, remote: Path, tid: str = "T-0001") -> Path:
    """`<root>/.project_manager/board/` 에 실 board git 을 만든다 (open 티켓 1건 + remote push)."""
    board = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board / "tickets" / "open" / f"{tid}-t.md").write_text(
        _TICKET_TEXT.format(tid=tid), encoding="utf-8")
    _git(["init", "-q", "-b", "main"], board)
    _git(["remote", "add", "origin", str(remote)], board)
    _git(["add", "-A"], board)
    _git(["commit", "-qm", "board init"], board)
    _git(["push", "-q", "-u", "origin", "main"], board)
    return board


@pytest.fixture
def board(tmp_path, monkeypatch):
    """REPO 를 tmp 로 재지정한 fresh board 모듈 (실 루트 미접촉)."""
    mod = _load_board()
    anchor_board_module(mod, tmp_path, monkeypatch)
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    return mod


def _advance_other_clone(bare: Path, tmp_path: Path, *, tid: str = "T-0001",
                         dest_status: str = "claimed") -> None:
    """두 번째(원격) 클론이 티켓을 `dest_status/` 로 옮겨 push — 우리 클론은 아직 안 당김."""
    other = tmp_path / "other-clone"
    _git(["clone", "-q", str(bare), str(other)], tmp_path)
    (other / "tickets" / dest_status).mkdir(parents=True, exist_ok=True)
    (other / "tickets" / "open" / f"{tid}-t.md").rename(
        other / "tickets" / dest_status / f"{tid}-t.md")
    _git(["add", "-A"], other)
    _git(["commit", "-qm", f"other moves {tid} to {dest_status}"], other)
    _git(["push", "-q", "origin", "main"], other)


def _show_args(tid: str = "T-0001") -> argparse.Namespace:
    return argparse.Namespace(id=tid)


def _expected_show_stdout(ticket_path: Path, tid: str = "T-0001",
                          status: str = "open") -> str:
    """`show` stdout 의 baseline — 헤더 1줄 + 명세 전문(라운드 0건)뿐이다.

    T-0782 의 핵심 계약이 "신선도 표기는 stderr 전용·stdout 무오염" 이라, 포함 여부가 아니라
    **정확일치**로 고정한다(freshness 문구가 한 글자라도 stdout 에 섞이면 red)."""
    body = ticket_path.read_text(encoding="utf-8")
    return f"-- {tid} ({status}/) --\n\n{body}\n"


def _list_args(**over) -> argparse.Namespace:
    base = dict(mine=False, repo=None, slot=None, task=None, tag=None, status=None)
    base.update(over)
    return argparse.Namespace(**base)


# ── 실 2-클론 픽스처: show 가 티켓 단위 불일치를 말한다 ────────────────────────

@requires_git
def test_show_surfaces_ticket_local_remote_status_mismatch(board, tmp_path, capsys):
    """원격 done·로컬 open 픽스처 → `show` stderr 가 로컬/원격 status·behind 를 표기한다."""
    bare = _bare(tmp_path, "bare-show-mismatch")
    _make_board_git(tmp_path, remote=bare)
    _advance_other_clone(bare, tmp_path, dest_status="done")

    board_dir = tmp_path / ".project_manager" / "board"
    rc = board.cmd_show(_show_args())
    assert rc == 0
    cap = capsys.readouterr()
    # 역방향(F-001): upstream 이 있는 정상 형상에서는 전역 freshness 줄도 그대로 나온다.
    assert "board-git:" in cap.err
    assert "T-0001" in cap.err
    assert "로컬 status=open" in cap.err
    assert "원격-추적 status=done" in cap.err
    assert "behind" in cap.err
    assert "로컬 사본 stale" in cap.err
    # F-004: stdout 은 baseline 과 정확히 같다(신선도 문구 무혼입).
    assert cap.out == _expected_show_stdout(
        board_dir / "tickets" / "open" / "T-0001-t.md")


@requires_git
def test_show_silent_when_local_matches_remote(board, tmp_path, capsys):
    """로컬·원격 status 가 같으면(불일치 없음) 티켓 단위 줄이 나오지 않는다(오탐 0)."""
    bare = _bare(tmp_path, "bare-show-match")
    _make_board_git(tmp_path, remote=bare)
    # 원격을 전혀 전진시키지 않음 — 로컬/원격 둘 다 open.
    rc = board.cmd_show(_show_args())
    assert rc == 0
    err = capsys.readouterr().err
    assert "로컬 사본 stale" not in err


@requires_git
def test_cmd_list_behind_line_first_pinned_on_real_multi_clone(board, tmp_path, capsys):
    """같은 픽스처에서 `list` 의 기존 freshness(behind N) 줄을 실 git 으로 처음 고정한다."""
    bare = _bare(tmp_path, "bare-list-behind")
    _make_board_git(tmp_path, remote=bare)
    _advance_other_clone(bare, tmp_path, dest_status="claimed")

    rc = board.cmd_list(_list_args())
    assert rc == 0
    err = capsys.readouterr().err
    assert "board-git:" in err
    assert "behind" in err


# ── 읽기 무변경 (해시·HEAD·porcelain 전후 동일 — fetch 의 원격-추적 ref 갱신만 예외) ──

@requires_git
def test_show_does_not_mutate_board_working_tree(board, tmp_path, capsys):
    bare = _bare(tmp_path, "bare-show-noop")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_other_clone(bare, tmp_path, dest_status="done")

    ticket_path = board_dir / "tickets" / "open" / "T-0001-t.md"
    before_bytes = ticket_path.read_bytes()
    before_head = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    before_porcelain = _git(["status", "--porcelain"], board_dir).stdout

    board.cmd_show(_show_args())
    capsys.readouterr()

    assert ticket_path.read_bytes() == before_bytes
    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == before_head
    assert _git(["status", "--porcelain"], board_dir).stdout == before_porcelain


# ── offline 강등 (원격 소멸) — "최신" 오탐 없이 rc=0 유지 ──────────────────────

@requires_git
def test_show_offline_degrades_without_crash_or_false_currency_claim(board, tmp_path, capsys):
    bare = _bare(tmp_path, "bare-show-offline")
    board_dir = _make_board_git(tmp_path, remote=bare)
    remove_git_tree(bare)   # 원격 도달 불가.

    rc = board.cmd_show(_show_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.out == _expected_show_stdout(   # stdout 은 baseline 그대로.
        board_dir / "tickets" / "open" / "T-0001-t.md")
    assert "판정불가" in cap.err or "fetch 실패" in cap.err
    assert "board-git: 최신" not in cap.err   # offline 을 "최신"으로 오단정하지 않는다.
    assert "로컬 사본 stale" not in cap.err   # 원격 판정 불가면 불일치도 단정하지 않는다.


# ── 검증되지 않은 스냅샷(캐시 불일치 + TTL 밖 + fetch 실패) — 단정 금지 ────────

@requires_git
def test_show_withholds_mismatch_verdict_when_remote_snapshot_unverified(
        board, tmp_path, capsys):
    """캐시된 원격-추적 ref 에 불일치가 남아 있어도, 이번 조회가 원격을 검증 못 했으면 단정 0.

    형상: 원격이 done 으로 앞선 뒤 **실제로 fetch** 해서 추적 ref 에 불일치를 적재 →
    FETCH_HEAD mtime 을 TTL(60s) 밖으로 되돌림 → bare remote 삭제로 도달 불가. 이때
    남아 있는 추적 ref 는 *검증되지 않은 캐시*라 "로컬 사본 stale" 은 판정이 아니라 추측이다
    — 전역 줄이 판정불가를 말하는 같은 흐름에서 티켓 단정이 나오면 두 줄이 서로를 부정한다.
    """
    bare = _bare(tmp_path, "bare-show-cached-mismatch")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_other_clone(bare, tmp_path, dest_status="done")

    _git(["fetch", "origin"], board_dir)      # 추적 ref 에 불일치(원격 done)를 실제로 적재.
    tracked = _git(["ls-tree", "-r", "--name-only", "refs/remotes/origin/main",
                    "--", "tickets/"], board_dir).stdout
    assert "tickets/done/T-0001-t.md" in tracked, tracked   # 캐시 불일치 적재 확인.

    fetch_head = board_dir / ".git" / "FETCH_HEAD"
    assert fetch_head.exists()
    stale_ts = time.time() - 120              # TTL(60s) 밖 → 이번 조회는 재검증이 필요하다.
    os.utime(fetch_head, (stale_ts, stale_ts))
    remove_git_tree(bare)                       # 그런데 원격 도달 불가 → fetch 실패(검증 불가).

    rc = board.cmd_show(_show_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.out == _expected_show_stdout(
        board_dir / "tickets" / "open" / "T-0001-t.md")
    assert "판정불가" in cap.err or "fetch 실패" in cap.err   # 강등은 전역 줄이 말한다.
    assert "로컬 사본 stale" not in cap.err    # 검증 못 한 캐시로 stale 을 단정하지 않는다.
    assert "원격-추적 status" not in cap.err   # 문구 완화가 아니라 **판정 자체를 안 낸다**.


# ── 원격에는 tickets/ 가 있으나 이 티켓만 없는 형상 — 조회 정상·단정 0 ────────

@requires_git
def test_show_silent_when_ticket_absent_from_remote_tracking(board, tmp_path, capsys):
    """원격 추적에는 다른 티켓만 있고 이 티켓이 없으면(로컬 신규) rc=0·stdout 정상·단정 0."""
    bare = _bare(tmp_path, "bare-show-absent")
    board_dir = _make_board_git(tmp_path, remote=bare, tid="T-0002")
    (board_dir / "tickets" / "open" / "T-0001-t.md").write_text(
        _TICKET_TEXT.format(tid="T-0001"), encoding="utf-8")
    _git(["add", "-A"], board_dir)
    _git(["commit", "-qm", "local-only T-0001"], board_dir)   # 원격엔 push 하지 않는다.

    rc = board.cmd_show(_show_args("T-0001"))
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.out == _expected_show_stdout(
        board_dir / "tickets" / "open" / "T-0001-t.md")
    assert "board-git:" in cap.err            # 원격은 도달 가능 — 전역 줄은 그대로 나온다.
    assert "로컬 사본 stale" not in cap.err    # 원격에 없는 티켓을 stale 이라 하지 않는다.


# ── board 비-git — 무출력·무오탐·rc=0 ──────────────────────────────────────

def _make_legacy_project(tmp_path: Path, tid: str = "T-0001") -> None:
    tickets = tmp_path / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    (tickets / "open" / f"{tid}-t.md").write_text(
        _TICKET_TEXT.format(tid=tid), encoding="utf-8")


def test_show_non_git_board_no_freshness_output(board, tmp_path, capsys):
    """board 비-git — stderr 는 존재가 아니라 **값**으로 무출력이고 stdout 은 baseline 이다."""
    _make_legacy_project(tmp_path)
    rc = board.cmd_show(_show_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.err == ""
    assert cap.out == _expected_show_stdout(
        tmp_path / ".project_manager" / "wiki" / "tickets" / "open" / "T-0001-t.md")


# ── board-git 이지만 원격(upstream) 없음 — 비-git 과 별개의 정상 형상 ──────────

@requires_git
def test_show_board_git_without_upstream_is_silent(board, tmp_path, capsys):
    """board 가 별도 git 이지만 remote/upstream 미설정(단일 로컬) → **완전 무출력**·rc=0.

    대조할 원격이 없으면 신선도라는 개념 자체가 없다(명세 §완료조건 "board 비-git·upstream
    부재에서 읽기가 무출력/무오탐이고 rc=0"). show 는 네트워크 0 인 `_board_git_upstream`
    으로 이 형상을 먼저 갈라 전역 freshness 줄과 티켓 불일치 줄을 **둘 다** 생략한다 —
    무출력을 존재가 아니라 `cap.err == ""` 값으로 고정한다."""
    board_dir = tmp_path / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board_dir / "tickets" / "open" / "T-0001-t.md").write_text(
        _TICKET_TEXT.format(tid="T-0001"), encoding="utf-8")
    _git(["init", "-q", "-b", "main"], board_dir)
    _git(["add", "-A"], board_dir)
    _git(["commit", "-qm", "board init (no remote)"], board_dir)

    rc = board.cmd_show(_show_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.err == ""
    assert cap.out == _expected_show_stdout(
        board_dir / "tickets" / "open" / "T-0001-t.md")


# ── TTL: FETCH_HEAD 를 실제로 조작해 fetch 생략/재실행을 확인 ──────────────────

@requires_git
def test_show_skips_fetch_when_fetch_head_within_ttl(board, tmp_path, capsys, monkeypatch):
    """FETCH_HEAD 를 방금(실 fetch로) 세운 상태에서 재호출한 `show` 는 fetch 를 부르지 않는다."""
    bare = _bare(tmp_path, "bare-ttl-fresh")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_other_clone(bare, tmp_path, dest_status="claimed")

    # 실 fetch 한 번 미리 실행 — FETCH_HEAD mtime 이 지금 시각(TTL 60s 이내)이 된다.
    _git(["fetch", "origin"], board_dir)

    real_board_git = board._board_git
    calls: list[list[str]] = []

    def _counting_board_git(args, *, check=False, timeout=None):
        calls.append(list(args))
        return real_board_git(args, check=check, timeout=timeout)

    monkeypatch.setattr(board, "_board_git", _counting_board_git)
    rc = board.cmd_show(_show_args())
    assert rc == 0
    err = capsys.readouterr().err
    assert not any(c and c[0] == "fetch" for c in calls), \
        f"TTL 이내인데 show 가 fetch 를 호출함: {calls}"
    # 역방향(F-002): TTL 이내 스냅샷은 **검증된** 스냅샷이다 — fetch 를 안 불렀어도 직전
    # fetch 로 갱신된 추적 ref 로 불일치 단정이 그대로 나온다(판정을 통째로 없애지 않았다).
    assert "로컬 사본 stale" in err


@requires_git
def test_show_runs_fetch_when_fetch_head_stale(board, tmp_path, capsys, monkeypatch):
    """FETCH_HEAD mtime 을 TTL 밖으로 실제로 되돌리면 `show` 가 fetch 를 다시 수행한다."""
    bare = _bare(tmp_path, "bare-ttl-stale")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_other_clone(bare, tmp_path, dest_status="claimed")

    _git(["fetch", "origin"], board_dir)
    fetch_head = board_dir / ".git" / "FETCH_HEAD"
    assert fetch_head.exists()
    stale_ts = time.time() - 120   # TTL(60s) 밖.
    os.utime(fetch_head, (stale_ts, stale_ts))

    real_board_git = board._board_git
    calls: list[list[str]] = []

    def _counting_board_git(args, *, check=False, timeout=None):
        calls.append(list(args))
        return real_board_git(args, check=check, timeout=timeout)

    monkeypatch.setattr(board, "_board_git", _counting_board_git)
    board.cmd_show(_show_args())
    capsys.readouterr()
    assert any(c and c[0] == "fetch" for c in calls), \
        f"TTL 밖인데 show 가 fetch 를 호출하지 않음: {calls}"


# ── behind 수치의 정직성 — known/unknown 보존 (실 git 계산 경로) ──────────────

@requires_git
def test_board_git_behind_returns_real_zero_when_up_to_date(board, tmp_path):
    """원격과 같은 커밋이면 실측 0 을 낸다 — 폴백 0 이 아니라 **계산된** 0(known)."""
    bare = _bare(tmp_path, "bare-behind-zero")
    _make_board_git(tmp_path, remote=bare)
    upstream = board._board_git_upstream()
    assert upstream is not None
    assert board._board_git_behind(upstream.tracking) == 0


@requires_git
def test_board_git_behind_returns_real_count_when_remote_advances(board, tmp_path):
    """원격이 1커밋 앞서면 1 — 진단이 싣는 수치가 실측임을 실 git 으로 고정한다."""
    bare = _bare(tmp_path, "bare-behind-one")
    board_dir = _make_board_git(tmp_path, remote=bare)
    _advance_other_clone(bare, tmp_path, dest_status="done")
    _git(["fetch", "origin"], board_dir)
    upstream = board._board_git_upstream()
    assert upstream is not None
    assert board._board_git_behind(upstream.tracking) == 1


@requires_git
def test_board_git_behind_returns_none_when_rev_list_fails(board, tmp_path):
    """rev-list 가 실패하면(없는 추적 ref) 0 이 아니라 None — 계산 불능은 값이 아니다."""
    bare = _bare(tmp_path, "bare-behind-unknown")
    _make_board_git(tmp_path, remote=bare)
    assert board._board_git_behind("refs/remotes/origin/no-such-branch") is None


# ── claim 거부 문구 — 로컬 stale 진술 + 원격 status + behind 3요소 (순수 함수) ──

def test_claim_block_message_race_lost_states_stale_and_behind():
    mod = _load_board()
    prefetch = mod._ClaimPrefetch(
        block=mod._CLAIM_BLOCK_RACE_LOST, detail="done", behind=3)
    msg = mod._claim_block_message("T-0042", prefetch)
    assert "claim race lost" in msg
    assert "done/" in msg
    assert "로컬 사본이 stale" in msg
    assert "3 커밋" in msg


def test_claim_block_message_race_lost_states_actual_zero():
    """실측 0 은 그대로 0 이라고 말한다 — unknown 처리가 정상 수치를 삼키면 안 된다."""
    mod = _load_board()
    prefetch = mod._ClaimPrefetch(
        block=mod._CLAIM_BLOCK_RACE_LOST, detail="done", behind=0)
    msg = mod._claim_block_message("T-0042", prefetch)
    assert "원격보다 0 커밋 뒤처진 상태" in msg
    assert "판정불가" not in msg


def test_claim_block_message_race_lost_omits_number_when_behind_unknown():
    """behind 계산 불능(None)이면 수치를 단정하지 않고 판정불가로 표시한다."""
    mod = _load_board()
    prefetch = mod._ClaimPrefetch(
        block=mod._CLAIM_BLOCK_RACE_LOST, detail="done", behind=None)
    msg = mod._claim_block_message("T-0042", prefetch)
    assert "커밋 뒤처진 상태" not in msg      # 없는 수치를 지어내지 않는다.
    assert "판정불가" in msg
    assert "claim race lost" in msg          # 차단 사유 진술은 그대로.
    assert "로컬 사본이 stale" in msg        # 원격 선점은 검증된 사실이라 유지.


# ── claim 차단 **판단** 은 불변 — 진단 수치만 정직해진다 ──────────────────────

@requires_git
def test_claim_prefetch_blocks_race_lost_with_real_behind(board, tmp_path):
    """원격이 done 이면 여전히 RACE_LOST 로 차단하고 behind 는 실측 1 을 싣는다."""
    bare = _bare(tmp_path, "bare-prefetch-race")
    _make_board_git(tmp_path, remote=bare)
    _advance_other_clone(bare, tmp_path, dest_status="done")

    prefetch = board._board_git_claim_prefetch("T-0001")
    assert prefetch.block == board._CLAIM_BLOCK_RACE_LOST
    assert prefetch.detail == "done"
    assert prefetch.behind == 1


@requires_git
def test_claim_prefetch_block_decision_unchanged_when_behind_unknown(
        board, tmp_path, monkeypatch):
    """behind 가 계산 불능이어도 차단 판단은 옛 0 폴백과 같다 — 선점만 막고 나머진 통과."""
    bare = _bare(tmp_path, "bare-prefetch-unknown")
    _make_board_git(tmp_path, remote=bare)
    monkeypatch.setattr(board, "_board_git_behind", lambda tracking: None)

    # (1) 선점 없음 → 차단하지 않는다(옛 0 폴백과 동일 방향: 원격이 앞섰다고 보지 않는다).
    prefetch = board._board_git_claim_prefetch("T-0001")
    assert prefetch.block is None
    assert prefetch.anchor

    # (2) 원격 선점 → 여전히 차단한다(수치만 unknown 으로 실린다).
    _advance_other_clone(bare, tmp_path, dest_status="claimed")
    blocked = board._board_git_claim_prefetch("T-0001")
    assert blocked.block == board._CLAIM_BLOCK_RACE_LOST
    assert blocked.behind is None
