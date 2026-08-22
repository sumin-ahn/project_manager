"""board git 즉시 sync — claim STRICT / new·move·complete best-effort 단위 회귀 (T-0163·ADR-0033 ②).

board(tickets+areas)가 별도 git(submodule·standalone)으로 분리된 형상에서, board.py 의 ticket
mutation 이 board git 에 자동 commit + pull --rebase + push 하는지 검증한다(spike §3.6). 핵심
안전속성:

  - **claim = STRICT(조율 primitive)**: push 가 성공해야 비로소 소유 확정. push 충돌(non-FF)이면
    로컬 claim 을 rollback(티켓 open/ 복귀·commit 되돌림)하고 race-lost 로 명시 실패한다 — 거짓
    소유 0. remote 도달 불가(offline)면 claim 자체가 명시 실패(best-effort 로 claim 을 남기면
    중복작업).
  - **new/complete/block/unclaim/unblock = best-effort**: 로컬 commit 은 항상 성공 → pull+push 가
    실패해도 **작업을 차단하지 않고** stale 경고만 낸다(무차단).
  - **legacy(board 가 별도 git 아님) 100% 무변경**: `_board_git_enabled()` False → sync no-op
    (git 호출 0). 이건 test_board_root.py + 기존 1729 테스트가 별도 board git 없이 green 으로
    남는 핵심 가드라, 여기선 그 게이트의 *True* 분기(별도 git 형상)를 직접 단언한다.

**hermetic 필수**: 실 board git + bare remote 를 tmp 에 세우고, board 모듈의 `REPO` 를 그 tmp 로
monkeypatch 한다(test_board_root 의 함수-scope 새-모듈 로드 + REPO 재지정 패턴 동류). git 부재
환경에선 실 git 케이스를 skip 한다(단위 게이트 테스트는 항상 실행).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip(게이트 단위 테스트는 항상 실행).",
)


def _force_rmtree(path: Path) -> None:
    """`shutil.rmtree` — Windows read-only git object(WinError 5)는 chmod +w 후 재시도.

    이 테스트들은 remote(bare)를 *실제로 지워* offline(도달 불가)을 모의한다. Windows 는
    git object 가 read-only 라 rmtree 가 PermissionError 로 막히므로, 쓰기권한을 부여하고
    재삭제해 반드시 remote 를 없앤다 — 실패를 삼키지 않고(chmod 재시도로도 안 되면 예외
    전파) offline 모의 시맨틱을 보존한다. POSIX 는 부모 디렉토리 쓰기권한만 있으면 지워져
    이 핸들러에 도달하지 않으므로 현행 동작 그대로다.
    """
    def _chmod_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_chmod_and_retry)
    else:  # pragma: no cover — 3.11 이하 호환(onexc 미지원 → onerror)
        shutil.rmtree(path, onerror=_chmod_and_retry)

# hermetic git commit 을 위한 결정적 author/committer (실 사용자 config 불요).
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_board():
    """board.py 를 (패키지 아님) importlib 로 경로 로드 — test_board_root 와 동일 규약."""
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=False)


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
    "---\n\n# {tid} — t\n\n## 목표\nx\n\n"
    "## 완료 조건 (Definition of Done)\n- [x] 구현\n"
)


def _detach_head(board_dir: Path) -> str:
    """board git 을 detached HEAD 로 만든다 (현재 SHA 로 checkout) — 반환 = detached SHA.

    `git checkout <sha>` 는 HEAD 를 브랜치가 아닌 커밋에 직접 붙인다(working tree 는 그대로·
    clean 유지). detached HEAD 재현의 표준 수단."""
    head = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    _git(["checkout", "-q", head], board_dir)
    return head


def _make_board_git(root: Path, *, remote: Path, tid: str = "T-0001") -> Path:
    """`<root>/.project_manager/board/` 에 실 board git 을 만든다 (tickets/ + open ticket + remote).

    별도 board git 형상(ADR-0033 ①)을 모사한다 — `board/tickets/` 가 dir 이라 board_root() 가
    board/ 로 갈리고, `board/.git` 이 있어 `_board_git_enabled()` 가 True 가 된다. bare remote 를
    origin 으로 두고 main 을 push+upstream 설정해 pull/push 가 동작하게 한다.
    """
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
    """REPO 를 tmp 로 재지정한 fresh board 모듈 + BOARD_LOCK 을 tmp 로 (실 루트 미접촉)."""
    mod = _load_board()
    # board_lock() / best-effort sync 가 .local/ 을 건드리고 렌더가 wiki/board.md 를 쓴다 —
    # REPO 파생 상수를 공용 헬퍼로 한꺼번에 tmp 로 격리한다.
    anchor_board_module(mod, tmp_path, monkeypatch)
    # 엔진의 board git subprocess(os.environ 상속)가 결정적 author 를 쓰도록 git identity 만 주입.
    # `os.environ` 전체를 갈아끼우지 않고(fragile·HOME/PATH 등 보존) 필요한 키만 setenv 한다.
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    # 정체성 축 tmp 격리 — 소유 대조(T-0781)가 user 축을 보므로 실 clone conf/전역 git email
    # 이 새면 픽스처 claim(`me/…`)과 어긋난다. 세션은 테스트가 `--repo/--slot` 으로 명시한다.
    conf = tmp_path / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("user=me\n", encoding="utf-8")
    monkeypatch.setattr(mod, "LOCAL_CONF", conf)
    monkeypatch.setattr(
        mod, "LEASES_FILE",
        tmp_path / ".project_manager" / ".local" / "worktree-leases.json")
    mod._tmp = tmp_path
    return mod


# ════════════════════════════════════════════════════════════════════════
# 게이트 — 별도 git 일 때만 sync (legacy no-op)
# ════════════════════════════════════════════════════════════════════════

def test_board_git_disabled_when_legacy(board):
    """board 가 별도 git 이 아니면(legacy·wiki 안) `_board_git_enabled()` False → sync no-op."""
    # board/ 자체가 없음 → board_root()==wiki, wiki/.git 부재 → False.
    assert board._board_git_enabled() is False


@requires_git
def test_board_git_enabled_when_separated(board, tmp_path):
    """board/tickets + board/.git 존재(분리 형상) → `_board_git_enabled()` True (sync 활성)."""
    _make_board_git(tmp_path, remote=tmp_path / "bare-a")
    bare = tmp_path / "bare-a"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    assert board._board_git_enabled() is True


@requires_git
def test_best_effort_noop_on_legacy_does_not_call_git(board, monkeypatch):
    """legacy 면 best-effort sync 가 git 을 *전혀* 부르지 않는다 (현 동작 byte-identical 보증)."""
    called = {"n": 0}

    def _boom(*a, **k):  # 호출되면 게이트가 새는 것 — 즉시 실패.
        called["n"] += 1
        raise AssertionError("legacy 에서 git 호출 발생 — sync 게이트 누출")

    monkeypatch.setattr(board, "_board_git", _boom)
    board._board_git_sync_best_effort("noop", ())  # 예외 없이 통과해야 함(git 미호출).
    assert called["n"] == 0


# ════════════════════════════════════════════════════════════════════════
# claim STRICT — 충돌 rollback / offline 실패
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_claim_strict_push_conflict_rolls_back_and_race_lost(board, tmp_path):
    """claim push 충돌(non-FF) → 로컬 claim rollback(open/ 복귀) + race-lost(rc=1).

    remote 를 두 번째 클론이 먼저 전진시켜(다른 claim push) 우리 push 가 non-FF 로 거부되게
    한다. 그러면 claim 은 로컬 commit 을 reset --hard 로 되돌리고 ticket 을 open/ 으로 복원해야
    한다(거짓 소유 0)."""
    bare = tmp_path / "bare"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    # 두 번째 클론(다른 PM)이 remote 를 먼저 전진시킨다(우리 push 가 non-FF 가 되도록).
    other = tmp_path / "other-clone"
    _git(["clone", "-q", str(bare), str(other)], tmp_path)
    (other / "tickets" / "claimed").mkdir(parents=True, exist_ok=True)
    other_tk = other / "tickets" / "open" / "T-0001-t.md"
    other_tk.rename(other / "tickets" / "claimed" / "T-0001-t.md")
    _git(["add", "-A"], other)
    _git(["commit", "-qm", "other claims T-0001"], other)
    _git(["push", "-q", "origin", "main"], other)

    # 우리 claim — pull(우리 클론은 아직 안 당김) 후 로컬은 open 으로 보이지만 push 가 충돌해야 함.
    # 단, pull --rebase 가 remote 의 winner claim 을 끌어오면 ticket 이 claimed/ 로 이동 →
    # 그 경우 find_ticket 단계에서 race-lost(cannot claim). 둘 다 race-lost(작업 차단)면 정답.
    rc = board.cmd_claim(argparse.Namespace(id="T-0001", repo="me", slot=1, user="me"))
    assert rc == 1, "remote 선점/충돌인데 claim 이 성공함 — 중복작업 방지 깨짐."
    # 거짓 소유 0: 우리 board 에서 ticket 이 claimed/ 에 *우리 이름으로* 남아 있으면 안 된다.
    ours_claimed = list((board_dir / "tickets" / "claimed").glob("T-0001-*.md"))
    if ours_claimed:
        text = ours_claimed[0].read_text(encoding="utf-8")
        assert "me/me" not in text, "claim 실패인데 우리 소유로 claimed/ 에 남음 — rollback 누락."


@requires_git
def test_claim_strict_offline_fails_explicitly(board, tmp_path):
    """claim 시 remote 도달 불가(offline) → 명시 실패(rc=1)·로컬 claim 안 남김.

    remote 를 삭제해 pull --rebase 가 실패(도달 불가)하게 만든다. claim 은 best-effort 로
    "내가 claim" 을 남기면 중복작업이라, prefetch 실패 시 명시 실패해야 한다(spike §3.6)."""
    bare = tmp_path / "bare-off"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    # remote 제거 → pull --rebase 가 도달 불가로 실패.
    _force_rmtree(bare)

    rc = board.cmd_claim(argparse.Namespace(id="T-0001", repo="me", slot=1, user="me"))
    assert rc == 1, "offline 인데 claim 이 성공함 — claim 은 remote 도달 없이 확정 금지."
    # ticket 은 여전히 open/ 에 있어야 한다(로컬 claim 안 남김).
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        "offline claim 실패인데 ticket 이 open/ 에 없음 — prefetch 단계가 로컬을 변경함."
    assert not list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "offline claim 실패인데 ticket 이 claimed/ 에 남음."


def _claim_move(board_mod, board_dir: Path, tid: str = "T-0001"):
    """claim 의 파일 mutation 만 모사 — open/ → claimed/ 이동 + `_ClaimFiles` 반환.

    `_board_git_claim_confirm` 을 격리 단언할 때 쓴다(cmd_claim 통합 경로 없이 commit/push/
    rollback 메커니즘만 본다). 원본 바이트는 이동 *전* 에 읽어 둔다 — rollback 의 복원 기준이다
    (ADR-0073: `reset --hard` 대신 역이동 + 원본 바이트 복원).
    """
    src = board_dir / "tickets" / "open" / f"{tid}-t.md"
    dst = board_dir / "tickets" / "claimed" / f"{tid}-t.md"
    original = src.read_bytes()
    src.rename(dst)
    return board_mod._ClaimFiles(old=src, new=dst, original=original)


@requires_git
def test_claim_confirm_push_nonff_rolls_back_scoped(board, tmp_path):
    """`_board_git_claim_confirm` push non-FF → **스코프 롤백**(open/ 복원) + False (ADR-0073).

    cmd_claim 통합 경로에선 winner 의 claim 이 먼저 보여 find_ticket 단계에서 걸리는 경우가
    많다 — 여기선 *push-conflict rollback 메커니즘 자체*를 격리 단언한다. anchor 를 기록한 뒤
    (=open 상태) 로컬에서 ticket 을 claimed/ 로 옮기고, remote 를 미리 전진시켜 우리 push 가
    non-FF 가 되게 한다. confirm 은 False 를 내고 **그 티켓 두 경로만** 되돌려야 한다
    (`reset --hard` 폐기 — 무관한 미커밋 작업 파괴 금지)."""
    bare = tmp_path / "bare-nonff"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    # remote 를 다른 클론이 전진(우리 fetch ref 와 무관하게 origin/main 이 앞섬 → push non-FF).
    other = tmp_path / "other"
    _git(["clone", "-q", str(bare), str(other)], tmp_path)
    (other / "advance.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], other)
    _git(["commit", "-qm", "remote advance"], other)
    _git(["push", "-q", "origin", "main"], other)

    # anchor = pull 없이 *현재* HEAD(open 상태) — confirm 의 rollback 복귀 지점.
    anchor = board._board_git_head()
    files = _claim_move(board, board_dir)

    ok = board._board_git_claim_confirm(anchor, files)
    assert ok is False, "remote 가 앞선(push non-FF) 상황인데 confirm 이 소유 확정함 — strict 위반."
    # rollback: ticket 이 open/ 으로 복원돼야 하고 claimed/ 엔 없어야 한다(거짓 소유 0).
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        "push 충돌 rollback 후 ticket 이 open/ 으로 복원 안 됨."
    assert not list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "push 충돌인데 ticket 이 claimed/ 에 남음 — rollback 누락."
    # reset --soft 로 claim commit 을 제거한 뒤 rollback refresh 가 원격 winner 를 당긴다. 루트
    # backfill 잔여가 staged 로 남으면 이 pull 이 막혀 우연히 anchor 에 머무르므로, 최종 HEAD 는
    # stale anchor 가 아니라 원격 최신 tip 이어야 복구가 완결된 것이다.
    remote_head = _git(["rev-parse", "main"], bare).stdout.strip()
    assert board._board_git_head() == remote_head, \
        "롤백 뒤 claim commit 제거·원격 최신화가 완결되지 않음."
    assert remote_head != anchor, "fixture 전제: 원격 winner 가 anchor 를 전진시켜야 한다."


@requires_git
def test_claim_strict_success_confirms_and_pushes(board, tmp_path):
    """경쟁 없는 claim → 로컬 claim commit + remote push 까지 성공(소유 확정·rc=0)."""
    bare = tmp_path / "bare-ok"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    rc = board.cmd_claim(argparse.Namespace(id="T-0001", repo="me", slot=1, user="me"))
    assert rc == 0, "경쟁 없는 claim 이 실패함."
    claimed = list((board_dir / "tickets" / "claimed").glob("T-0001-*.md"))
    assert claimed, "claim 성공인데 ticket 이 claimed/ 로 안 옮겨짐."
    assert "me/me" in claimed[0].read_text(encoding="utf-8"), "claimed_by 가 우리 식별자 아님."
    # remote 에 우리 claim 이 push 됐는지 — bare remote 의 main 트리에 claimed/ ticket 이 있어야.
    ls = _git(["ls-tree", "-r", "--name-only", "main"], bare)
    assert "tickets/claimed/T-0001-t.md" in ls.stdout, \
        "claim 이 remote 로 push 안 됨 — 소유 확정(strict push)이 동작 안 함."


@requires_git
def test_claim_confirm_commit_fail_rolls_back_despite_push_ok(board, tmp_path, monkeypatch):
    """commit 이 새 commit 을 못 내면(push rc=0=up-to-date) → 거짓 확정 금지·rollback·False (codex must-fix).

    claim 경로엔 항상 rename 변경이 있으므로 commit 은 반드시 새 commit 을 내야 정상이다. commit
    이 실패(identity 부재·hook·nothing-to-commit)하면 `git push` 가 "Everything up-to-date" rc=0
    을 내 remote 미전파인데 로컬만 claimed = 중복작업 방지 붕괴. confirm 은 commit 실패(False)를
    감지해 즉시 rollback(open/ 복원) + False 를 내야 한다. push 가 rc=0 이어도 *확정 금지*."""
    bare = tmp_path / "bare-cfail"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    anchor = board._board_git_head()
    files = _claim_move(board, board_dir)

    # 실 실패 모드 충실 재현: `add -A` 는 정상 실행(move 가 index 에 stage)되지만 `commit` 만
    # rc≠0 으로 실패시킨다 → 새 commit 이 안 생겨 `_board_git_stage_and_commit` 가 False 를 낸다
    # (판정은 rc 가 아니라 HEAD 전진 여부·ADR-0073).
    real_git = board._board_git

    def _commit_fails(args, *, check=False, timeout=None):
        if args[:1] == ["commit"]:
            return subprocess.CompletedProcess(args, 1, "", "simulated commit failure")
        return real_git(args, check=check, timeout=timeout)

    monkeypatch.setattr(board, "_board_git", _commit_fails)
    # push 는 정상이면 up-to-date rc=0 을 낸다 — commit 실패를 무시했다면 거짓 확정될 조건.

    ok = board._board_git_claim_confirm(anchor, files)
    assert ok is False, "commit 실패인데 push rc=0 으로 거짓 확정함 — must-fix 회귀."
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        "commit 실패 rollback 후 ticket 이 open/ 으로 복원 안 됨."
    assert not list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "commit 실패인데 ticket 이 claimed/ 에 남음 — rollback 누락(거짓 소유)."


@requires_git
def test_claim_confirm_rollback_reset_throw_never_traceback(board, tmp_path, monkeypatch):
    """rollback 의 reset 이 throw 해도 confirm 은 bool 만 반환(예외 미전파·never traceback·reviewer sug).

    push non-FF 후 rollback 의 `reset --soft` 가 예외(timeout·git 소실)를 던지는 상황에서도,
    `_board_git_claim_rollback` 가 `contextlib.suppress` 로 예외를 삼켜 confirm 이 깨끗한 False 를
    내야 한다(ADR-0012 loser=race-lost rc=1·never traceback). reset/push 호출만 throw 시키고
    commit 은 정상 통과시켜 push-실패 경로를 탄다."""
    bare = tmp_path / "bare-throw"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    anchor = board._board_git_head()
    files = _claim_move(board, board_dir)

    real_git = board._board_git

    def _flaky_git(args, *, check=False, timeout=None):
        # reset(rollback) 호출은 throw — rollback 이 이를 삼켜야 confirm 이 bool 만 낸다.
        if args[:1] == ["reset"]:
            raise RuntimeError("simulated reset failure (timeout/git 소실)")
        return real_git(args, check=check, timeout=timeout)

    # commit 은 정상(새 commit 생성), push 는 non-FF 가 아니라도 rollback 경로를 강제하려고
    # push 도 실패시킨다(어떤 push 결과든 rollback 으로 가게) — 그 rollback 의 reset 이 throw.
    monkeypatch.setattr(board, "_board_git", _flaky_git)
    monkeypatch.setattr(board, "_board_git_push",
                        lambda: subprocess.CompletedProcess([], 1, "", "rejected (non-FF)"))

    # 예외가 confirm 을 빠져나오면 이 호출이 raise → 테스트 실패. bool 만 나와야 한다.
    ok = board._board_git_claim_confirm(anchor, files)
    assert ok is False, "push 실패인데 confirm 이 확정함(또는 예외 전파)."
    # reset 이 throw 해도 **파일 복원은 별도 suppress 구간**이라 티켓은 open/ 으로 돌아온다
    # (거짓 소유 0 — 이력만 못 되감은 상태로 남고 다음 mutation 이 catch-up).
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        "reset throw 인데 티켓 파일 복원까지 건너뜀 — 롤백 단계가 서로를 막고 있다."

    # 통합: cmd_claim 도 같은 조건에서 traceback 없이 rc=1 을 내야 한다(reset throw 잔존 monkeypatch).
    rc = board.cmd_claim(argparse.Namespace(id="T-0001", repo="me", slot=1, user="me"))
    assert rc == 1, "reset throw 경로에서 cmd_claim 이 race-lost rc=1 을 안 냄(traceback 위험)."


@requires_git
def test_claim_preserves_best_effort_backlog(board, tmp_path):
    """best-effort backlog(밀린 unpushed commit)가 쌓인 뒤 claim → backlog 보존 + claim 만 처리 (reviewer sug).

    미묘 불변식: best-effort 가 offline 으로 push 못 한 로컬 commit(backlog)이 있는 상태에서
    이후 claim 이 일어나면, prefetch pull --rebase 가 backlog 를 remote tip *위로* rebase 하고,
    claim 이 성공하면 backlog+claim 이 함께 push 된다 — backlog 가 유실/덮어쓰여선 안 된다."""
    bare = tmp_path / "bare-backlog"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    # 1) best-effort backlog 1건 — 새 ticket T-0002 를 로컬 commit 했지만 (offline 가정) push 안 됨.
    #    remote 를 잠깐 제거해 best-effort push 가 실패하게 만든 뒤 복구한다.
    (board_dir / "tickets" / "open" / "T-0002-t.md").write_text(
        _TICKET_TEXT.format(tid="T-0002"), encoding="utf-8")
    moved = tmp_path / "bare-backlog-moved"
    bare.rename(moved)  # remote 일시 도달 불가.
    board._board_git_sync_best_effort(
        "new T-0002", (board_dir / "tickets" / "open" / "T-0002-t.md",))  # 로컬 commit O·push 실패(무차단).
    moved.rename(bare)  # remote 복구.
    backlog_head = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    assert _git(["log", "--oneline"], board_dir).stdout.count("\n") >= 2, \
        "best-effort backlog commit 이 로컬에 안 쌓임."

    # 2) 이제 claim T-0001 — prefetch pull --rebase 가 backlog 를 보존한 채 진행, 성공 push.
    rc = board.cmd_claim(argparse.Namespace(id="T-0001", repo="me", slot=1, user="me"))
    assert rc == 0, "backlog 있는 상태의 claim 이 실패함."
    # backlog(T-0002) commit 이 로컬+remote 에 여전히 존재해야 한다(유실 0).
    log = _git(["log", "--oneline"], board_dir).stdout
    assert "new T-0002" in log, "claim 후 best-effort backlog commit 이 유실됨 — rebase/rollback 이 backlog 를 삼킴."
    assert (board_dir / "tickets" / "open" / "T-0002-t.md").exists(), \
        "backlog ticket(T-0002) 파일이 claim 후 사라짐."
    remote_ls = _git(["ls-tree", "-r", "--name-only", "main"], bare).stdout
    assert "tickets/open/T-0002-t.md" in remote_ls, \
        "claim push 가 backlog(T-0002)를 remote 로 전파 안 함 — catch-up 누락."
    assert "tickets/claimed/T-0001-t.md" in remote_ls, "claim(T-0001)이 remote 로 push 안 됨."


# ════════════════════════════════════════════════════════════════════════
# claim prefetch — 읽기 전용 선점 감지 · 사유 4분 (T-0175 계승 → ADR-0073 재작성)
#
# ADR-0073 이전엔 board 전체가 dirty 이기만 하면 prefetch 가 즉시 차단했다(dirty sentinel).
# 그게 결함이었다: 무관한 파일 하나(다른 슬롯의 미커밋 티켓·남의 areas 편집)로 **모든** claim
# 이 전면 차단됐고, 안내는 `add -A`(= 남의 미완성 편집을 대신 커밋)였다. 아래 3건은 그
# dirty-sentinel 계열 테스트의 후신이다 — 지켜야 할 성질(원인 정확·오판 0·로컬 변경 0)은
# 그대로 두고, "차단" 을 "진행" 으로 뒤집는다. 잔여 차단은 `behind>0 ∧ 통합 불가` 뿐이다.
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_claim_prefetch_dirty_board_no_longer_blocks(board, tmp_path):
    """board 에 uncommitted 변경이 있어도(원격 미선행) prefetch 는 **anchor 를 낸다** (ADR-0073).

    옛 동작: dirty sentinel → claim 차단(D1 과차단). 신 동작: 원격이 안 앞섰으면 통합할 게 없고
    소유 확정 권위는 push CAS 라 로컬 dirty 는 claim 과 무관하다 — 진행한다."""
    bare = tmp_path / "bare-dirty"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    # board 트리에 unstaged 변경 1건(ticket 본문 Edit 모사) — remote 는 그대로 살아있다.
    tk = board_dir / "tickets" / "open" / "T-0001-t.md"
    tk.write_text(tk.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    result = board._board_git_claim_prefetch("T-0001")
    assert result.block is None, \
        f"무관 dirty 인데 claim 이 차단됨(D1 과차단 재발): {result.block}"
    assert result.anchor == _git(["rev-parse", "HEAD"], board_dir).stdout.strip(), \
        "dirty 상태에서 anchor 가 현재 HEAD 가 아님."


@requires_git
def test_claim_dirty_board_now_claims_and_leaves_dirty_untouched(board, tmp_path):
    """dirty board 에서도 claim 이 성공하고 그 dirty 는 **커밋되지도 파괴되지도 않는다** (D1·D2·통합).

    옛 통합 테스트는 여기서 rc=1(차단) + 'commit 후 재시도' 안내를 단언했다. 그 동작이 사용자가
    보고한 결함이다 — 다른 슬롯의 미커밋 티켓 하나가 무관한 claim 을 전면 차단했다."""
    bare = tmp_path / "bare-dirty2"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    other = board_dir / "tickets" / "open" / "T-0002-other.md"
    other.write_text(_TICKET_TEXT.format(tid="T-0002"), encoding="utf-8")  # 무관 untracked.

    rc = board.cmd_claim(argparse.Namespace(id="T-0001", repo="me", slot=1, user="me"))
    assert rc == 0, "무관 dirty 때문에 claim 이 막힘 — 사용자 보고 결함 재발."
    assert other.exists(), "claim 이 무관한 미커밋 파일을 지움(파괴)."
    remote_ls = _git(["ls-tree", "-r", "--name-only", "main"], bare).stdout
    assert "tickets/claimed/T-0001-t.md" in remote_ls, "claim 이 원격에 안 실림."
    assert "T-0002-other.md" not in remote_ls, \
        "claim 커밋이 무관한 미커밋 파일을 함께 push 함 — D2 누출 재발."


@requires_git
def test_claim_prefetch_fetch_failure_is_offline(board, tmp_path, monkeypatch):
    """fetch 실패 → offline 사유(진행 불가·다른 사유와 구분·T-0175 의미 계승).

    선점 감지가 fetch(읽기 전용)로 바뀌었으므로 도달 불가는 여기서 갈린다. dirty·rebase·
    upstream-없음 과 **다른 코드**여야 안내가 섞이지 않는다."""
    bare = tmp_path / "bare-clean-off"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    _make_board_git(tmp_path, remote=bare)
    monkeypatch.setattr(board, "_board_git_fetch", lambda remote: False)

    result = board._board_git_claim_prefetch("T-0001")
    assert result.block == board._CLAIM_BLOCK_OFFLINE, \
        f"fetch 실패인데 offline 사유가 아님: {result.block}"
    assert result.anchor is None, "차단인데 anchor 가 나옴(진행 위험)."


@requires_git
def test_claim_prefetch_clean_success_returns_anchor(board, tmp_path):
    """clean tree + 정상 원격 → board HEAD SHA anchor 반환(차단 사유 없음·T-0175 보존)."""
    bare = tmp_path / "bare-clean-ok"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)

    result = board._board_git_claim_prefetch("T-0001")
    expected = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    assert result.anchor == expected, "clean+성공인데 board HEAD anchor 가 아님."
    assert result.block is None, f"정상인데 차단 사유가 붙음: {result.block}"


@requires_git
def test_pull_is_skipped_when_not_behind(board, tmp_path, monkeypatch):
    """sensitivity: behind==0 이면 `pull --rebase` 를 **아예 부르지 않는다** (ADR-0073 가드 실증).

    이 조건 게이트가 D1 과차단 해소의 load-bearing 부분이다 — dirty tree 에서 pull 을 부르면
    그 pull 이 실패하고(추적 변경) 옛 차단이 그대로 돌아온다. pull 을 throw 로 바꿔 두고 dirty
    board 의 prefetch 가 **정상 anchor** 를 내면, pull 경로를 타지 않았다는 뜻이다."""
    bare = tmp_path / "bare-sens"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    tk = board_dir / "tickets" / "open" / "T-0001-t.md"
    tk.write_text(tk.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    def _pull_must_not_run():
        raise AssertionError("behind==0 인데 pull --rebase 를 호출함 — 조건 게이트 누출.")

    monkeypatch.setattr(board, "_board_git_pull_rebase", _pull_must_not_run)
    result = board._board_git_claim_prefetch("T-0001")
    assert result.block is None and result.anchor, \
        f"behind==0 + dirty 인데 진행하지 못함: {result}"


# ════════════════════════════════════════════════════════════════════════
# best-effort — push 실패해도 무차단(경고만)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_best_effort_local_commit_failure_is_loud_and_scope_isolated(
        board, tmp_path, monkeypatch, capsys):
    """commit rc 실패는 uncommitted를 loud 출력하고, 다른 ticket WIP는 진단/commit에 안 넣는다.

    ``_board_git_stage_and_commit``의 False를 다시 무시하면 pull/push의 "로컬 commit 보존"
    경고만 남으므로 이 테스트가 red가 된다. T-0003은 이번 mutation 밖의 untracked WIP라
    recovery 명령과 staged index 어느 쪽에도 들어가면 안 된다.
    """
    bare = tmp_path / "bare-local-fail"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    old = board_dir / "tickets" / "open" / "T-0001-t.md"
    changed = board_dir / "tickets" / "claimed" / "T-0001-t.md"
    unrelated = board_dir / "tickets" / "open" / "T-0003-t.md"
    old.rename(changed)
    unrelated.write_text(_TICKET_TEXT.format(tid="T-0003"), encoding="utf-8")
    before = board._board_git_head()

    real_git = board._board_git

    def _commit_fails(args, *, check=False, timeout=None):
        if args[:1] == ["commit"]:
            return subprocess.CompletedProcess(args, 1, "", "simulated permission denied")
        return real_git(args, check=check, timeout=timeout)

    monkeypatch.setattr(board, "_board_git", _commit_fails)
    sync_calls = []
    monkeypatch.setattr(board, "_board_git_pull_rebase", lambda: sync_calls.append("pull"))
    monkeypatch.setattr(board, "_board_git_push", lambda: sync_calls.append("push"))
    board._board_git_sync_best_effort("block T-0001", (old, changed))

    assert board._board_git_head() == before, "commit 실패인데 HEAD가 전진함."
    staged = _git(["diff", "--cached", "--name-status"], board_dir).stdout
    assert "tickets/open/T-0001-t.md" in staged
    assert "tickets/claimed/T-0001-t.md" in staged
    assert "tickets/open/T-0003-t.md" not in staged, \
        "이번 mutation 밖 T-0003 WIP가 local commit 시도 scope에 섞임."
    assert sync_calls == [], "local commit 실패 뒤 pull/push를 호출함."
    err = capsys.readouterr().err
    assert "local commit 실패" in err and "uncommitted" in err, err
    assert "tickets/open/T-0001-t.md" in err, "move의 old 삭제 복구 경로가 빠짐."
    assert "tickets/claimed/T-0001-t.md" in err, "move의 new 경로가 복구 경로에 없음."
    assert ".gitattributes" in err, "실제 stage된 .gitattributes backfill이 복구 경로에서 빠짐."
    assert "T-0003-t.md" not in err, "다른 ticket WIP를 복구 대상으로 노출함."
    assert "로컬 commit 은 보존" not in err, "commit 실패를 push 실패처럼 false-success 보고함."
    assert "git -C .project_manager/board add" in err and " commit -m " in err, err


@requires_git
def test_best_effort_commit_false_without_scoped_change_is_normal_noop(
        board, tmp_path, capsys):
    """scope에 변경이 없으면 False여도 local-commit-failed로 오진하지 않는다."""
    bare = tmp_path / "bare-noop"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    # 같은 board 안의 다른 WIP가 있어도, scope 밖이면 이번 mutation의 commit 실패 증거가 아니다.
    (board_dir / "tickets" / "open" / "T-0003-t.md").write_text(
        _TICKET_TEXT.format(tid="T-0003"), encoding="utf-8")
    outside = tmp_path / "outside-board.md"

    board._board_git_sync_best_effort("noop", (outside,))

    err = capsys.readouterr().err
    assert "local commit 실패" not in err, err
    assert "T-0003-t.md" not in err, err


@requires_git
def test_promote_reports_board_git_pending_when_local_commit_is_uncommitted(
        board, tmp_path, monkeypatch, capsys):
    """promote는 local commit 실패 뒤 `board-git 승격 완료`를 출력하면 안 된다."""
    bare = tmp_path / "bare-promote-pending"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    draft = board_dir / "tickets" / ".drafts" / "T-0002-t.md"
    draft.parent.mkdir()
    draft.write_text(_TICKET_TEXT.format(tid="T-0002").replace("status: open", "status: draft"),
                     encoding="utf-8")
    monkeypatch.setattr(board, "_body_lint_issues", lambda *args, **kwargs: [])
    monkeypatch.setattr(board, "_board_git_sync_best_effort", lambda *args, **kwargs: False)

    assert board.cmd_promote(argparse.Namespace(id="T-0002")) == 0

    out = capsys.readouterr().out
    assert "board-git 승격 완료" not in out, out
    assert "board-git 기록 보류: local-only/uncommitted" in out, out


@requires_git
def test_best_effort_pull_exception_after_local_commit_preserves_ready_state(
        board, tmp_path, monkeypatch, capsys):
    """local commit 뒤 pull 예외는 local-only 실패가 아니라 catch-up 보류다."""
    bare = tmp_path / "bare-pull-exception"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    changed = board_dir / "tickets" / "open" / "T-0002-t.md"
    changed.write_text(_TICKET_TEXT.format(tid="T-0002"), encoding="utf-8")
    before = board._board_git_head()

    def _pull_timeout():
        raise subprocess.TimeoutExpired(["git", "pull", "--rebase"], 5)

    monkeypatch.setattr(board, "_board_git_pull_rebase", _pull_timeout)
    ready = board._board_git_sync_best_effort("new T-0002", (changed,))

    assert ready is True, "local commit 성공 뒤 pull 예외를 local-only 실패로 분류함."
    assert board._board_git_head() != before, "pull 예외 전에 local commit이 생성되지 않음."
    assert "로컬 commit 은 보존" in capsys.readouterr().err


@requires_git
def test_best_effort_push_failure_does_not_block(board, tmp_path, capsys):
    """best-effort sync 에서 push 가 실패해도 작업을 차단하지 않는다 — 로컬 commit 보존 + 경고만.

    remote 를 제거해 pull/push 가 실패하게 한 뒤 best-effort sync 를 부른다. 예외/비0 종료 없이
    경고만 stderr 로 나오고, 로컬 commit 은 보존돼야 한다(무차단)."""
    bare = tmp_path / "bare-be"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    # 로컬 변경 1건(다음 commit 거리) + remote 제거.
    (board_dir / "tickets" / "open" / "T-0002-t.md").write_text(
        _TICKET_TEXT.format(tid="T-0002"), encoding="utf-8")
    _force_rmtree(bare)

    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    # 예외 없이 리턴해야 한다(무차단).
    board._board_git_sync_best_effort(
        "new T-0002", (board_dir / "tickets" / "open" / "T-0002-t.md",))
    head_after = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    assert head_after != head_before, "best-effort 가 로컬 commit 을 안 함 — local-first 위반."
    err = capsys.readouterr().err
    assert "보류" in err or "sync" in err, f"push 실패인데 stale 경고가 안 나옴: {err!r}"


@requires_git
def test_complete_best_effort_offline_still_completes(board, tmp_path, capsys):
    """complete(best-effort)는 remote 도달 불가여도 로컬 완료 + 경고만(작업 무차단·rc=0).

    claim 으로 ticket 을 claimed/ 에 둔 뒤 remote 를 제거하고 complete 한다 — 로컬에선 done/ 으로
    옮겨지고(완료), sync 실패는 경고로만 표면화돼야 한다(차단 아님)."""
    bare = tmp_path / "bare-cmp"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    assert board.cmd_claim(argparse.Namespace(id="T-0001", repo="me", slot=1, user="me")) == 0

    _force_rmtree(bare)  # 이제 remote 도달 불가.
    rc = board.cmd_complete(argparse.Namespace(
        id="T-0001", tests_pass=True, allow_missing_log=True, allow_untested=False,
        repo="me", slot=1))
    assert rc == 0, "complete(best-effort)가 offline 에서 차단됨 — best-effort 위반."
    assert list((board_dir / "tickets" / "done").glob("T-0001-*.md")), \
        "complete 가 로컬에서 done/ 으로 안 옮겨짐."
    err = capsys.readouterr().err
    assert "보류" in err or "sync" in err, f"offline complete 인데 stale 경고가 안 나옴: {err!r}"


# ════════════════════════════════════════════════════════════════════════
# detached HEAD — 공유 primitive + prefetch 오진 수정(T-0203) + best-effort orphan 방지(T-0204)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_head_detached_false_when_on_branch(board, tmp_path):
    """`_board_git_head_detached` — 브랜치 위(attached)면 False (공유 primitive·T-0203/0204)."""
    bare = tmp_path / "bare-att"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    _make_board_git(tmp_path, remote=bare)  # HEAD → refs/heads/main (attached).
    assert board._board_git_head_detached() is False, \
        "브랜치 위인데 detached 로 오판 — attached 판정 오발."


@requires_git
def test_head_detached_true_when_detached(board, tmp_path):
    """`_board_git_head_detached` — detached HEAD 면 True (symbolic-ref rc≠0)."""
    bare = tmp_path / "bare-det"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    _detach_head(board_dir)
    assert board._board_git_head_detached() is True, \
        "detached HEAD 인데 감지 못 함 — symbolic-ref rc 판정 실패."


def test_head_detached_fail_soft_on_exception(board, monkeypatch):
    """`_board_git_head_detached` — 판정 예외(git 소실·timeout)는 fail-soft False(현행 경로)."""
    def _boom(*a, **k):
        raise RuntimeError("simulated git failure (timeout/소실)")

    monkeypatch.setattr(board, "_board_git", _boom)
    assert board._board_git_head_detached() is False, \
        "판정 예외인데 True(detached) 로 떨어짐 — fail-soft 위반(정상 경로를 막을 위험)."


def test_head_detached_rc1_only_fatal_rc128_is_false(board, monkeypatch):
    """rc=1 만 detached — fatal(rc=128·gitdir 손상/조회 불능)은 False (codex must-fix).

    rc≠0 전부를 detached 로 취급하면 fatal 오류가 "offline 아님·detached" 오진으로 claim 을
    잘못 차단하고 best-effort commit 도 잘못 skip 한다 — fatal 은 기존 실패 경로로 흘린다.
    """
    import subprocess as _sp

    def _fake(rc):
        def _run(args, **k):
            return _sp.CompletedProcess(args, rc, stdout="", stderr="")
        return _run

    monkeypatch.setattr(board, "_board_git", _fake(1))
    assert board._board_git_head_detached() is True, "rc=1(detached)인데 False."
    monkeypatch.setattr(board, "_board_git", _fake(128))
    assert board._board_git_head_detached() is False, \
        "rc=128(fatal)인데 True(detached) — fatal 을 detached 로 오진(claim 오차단·sync 오skip)."


@requires_git
def test_claim_prefetch_detached_branches_not_offline(board, tmp_path):
    """detached HEAD → prefetch 가 detached sentinel 반환(≠offline None·≠dirty·≠anchor·T-0203).

    detached 에선 `pull --rebase` 가 rc≠0 로 거부되는데, 이를 offline(None) 으로 오판하지 않고
    별도 sentinel 로 가른다 — 네트워크는 정상(실 board git·remote 살아있음)."""
    bare = tmp_path / "bare-pd"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    _detach_head(board_dir)

    result = board._board_git_claim_prefetch("T-0001")
    assert result.block == board._CLAIM_BLOCK_DETACHED, \
        "detached board 인데 prefetch 가 detached 사유를 안 냄(offline/dirty/anchor 오판)."
    assert result.block != board._CLAIM_BLOCK_OFFLINE, \
        "detached 가 offline 으로 오판됨 — T-0203 회귀."
    assert result.block != board._CLAIM_BLOCK_DIRTY, "detached 가 dirty 로 오판됨."


@requires_git
def test_claim_prefetch_detached_precedes_everything(board, tmp_path):
    """dirty + detached 동시 → detached 선체크가 **1순위**(어떤 git mutation 보다 앞·T-0204/ADR-0073).

    best-effort 가 detached 에서 commit 을 skip 하면 board 가 dirty 로 남는다 — 그 상태에서
    dirty 안내는 오도이고(detached 라 단순 commit 불가), 더 중요하게는 detached/mid-rebase 에서
    `git commit -- <paths>` 가 rebase HEAD 위에 커밋을 만들어 버린다(architect 실측). 그래서 이
    선체크는 순서상 양보 불가다."""
    bare = tmp_path / "bare-pdd"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    _detach_head(board_dir)
    # detached 상태 위에 unstaged 변경 1건(best-effort commit skip 으로 dirty 잔존 모사).
    tk = board_dir / "tickets" / "open" / "T-0001-t.md"
    tk.write_text(tk.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    assert board._board_git_status_porcelain().strip(), "fixture 전제: board 가 dirty 여야 함."

    result = board._board_git_claim_prefetch("T-0001")
    assert result.block == board._CLAIM_BLOCK_DETACHED, \
        "dirty+detached 동시인데 detached 가 우선 안 됨 — 선체크 순서 위반·오도 안내 위험."


@requires_git
def test_claim_detached_prints_checkout_guidance_not_offline(board, tmp_path, capsys):
    """detached board claim → detached 원인-정확 안내(checkout)·offline 문구 미출력·차단(rc=1)·로컬 변경 0 (T-0203 통합).

    cmd_claim 이 detached sentinel 을 offline/dirty 와 별도 분기해, 사용자가 'offline' 이나
    'commit 후 재시도' 가 아니라 '브랜치 복귀(checkout)' 안내를 본다(오판·이중출력 0). ticket 은
    open/ 그대로(prefetch 가 로컬 미변경)."""
    bare = tmp_path / "bare-cd"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    _detach_head(board_dir)

    rc = board.cmd_claim(argparse.Namespace(id="T-0001", repo="me", slot=1, user="me"))
    assert rc == 1, "detached board claim 이 차단 안 됨(anchor 없는데 진행)."
    err = capsys.readouterr().err
    assert "detached HEAD" in err and "checkout" in err, \
        f"detached 안내 메시지가 detached/checkout 을 안 담음: {err!r}"
    assert "offline — board 도달 불가" not in err, \
        f"detached 케이스에 offline 메시지가 나옴(오판·이중출력): {err!r}"
    assert "uncommitted" not in err, \
        f"detached 케이스에 dirty(uncommitted) 메시지가 나옴(오판·이중출력): {err!r}"
    # 로컬 변경 0: ticket 은 open/ 그대로, claimed/ 엔 없어야 한다(prefetch 가 안 건드림).
    assert list((board_dir / "tickets" / "open").glob("T-0001-*.md")), \
        "detached claim 실패인데 ticket 이 open/ 에 없음 — prefetch 가 로컬을 변경함."
    assert not list((board_dir / "tickets" / "claimed").glob("T-0001-*.md")), \
        "detached claim 실패인데 ticket 이 claimed/ 에 남음."


@requires_git
def test_best_effort_detached_skips_commit_no_orphan(board, tmp_path, capsys):
    """detached HEAD → best-effort sync 가 commit 을 skip(orphan 0)하고 loud 경고만 낸다 (T-0204).

    detached 위의 commit 은 orphan 으로 쌓이고 catch-up 이 구조적으로 불가하므로, best-effort 는
    commit/pull/push 를 전부 skip 하고 기록을 보류한다 — HEAD 불변(새 orphan commit 0)·경고 출력.
    파일 mutation 은 이미 끝난 뒤라 작업은 무차단(파일은 남는다)."""
    bare = tmp_path / "bare-bed"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    _detach_head(board_dir)
    # mutation 모사: 새 ticket 파일(정상이면 best-effort 가 commit 할 대상).
    (board_dir / "tickets" / "open" / "T-0002-t.md").write_text(
        _TICKET_TEXT.format(tid="T-0002"), encoding="utf-8")

    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    count_before = _git(["rev-list", "--count", "HEAD"], board_dir).stdout.strip()
    board._board_git_sync_best_effort(
        "new T-0002", (board_dir / "tickets" / "open" / "T-0002-t.md",))  # 예외 없이 리턴(무차단).
    head_after = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    count_after = _git(["rev-list", "--count", "HEAD"], board_dir).stdout.strip()

    assert head_after == head_before, \
        "detached 에서 best-effort 가 commit 을 냄 — orphan 누적(HEAD 전진)·T-0204 위반."
    assert count_after == count_before, "detached 에서 commit 개수가 늘어남 — orphan 누적."
    assert board._board_git_head_detached() is True, \
        "best-effort 가 detached 를 자동 checkout 함 — PM 브랜치 의도 침해(안내만 해야)."
    err = capsys.readouterr().err
    assert "detached HEAD" in err and "보류" in err, \
        f"detached best-effort 인데 detached 보류 경고가 안 나옴: {err!r}"
    # 작업 무차단: mutation 파일 자체는 남아야 한다(git 기록만 보류·working tree 미접촉).
    assert (board_dir / "tickets" / "open" / "T-0002-t.md").exists(), \
        "detached best-effort 가 mutation 파일을 되돌림 — 작업 무차단 원칙 위반."


@requires_git
def test_best_effort_attached_still_commits(board, tmp_path):
    """회귀: attached 브랜치에선 best-effort 가 종전대로 로컬 commit 을 낸다(detached 가드가 정상 경로 미차단).

    T-0204 detached 가드가 attached 정상 경로를 실수로 막지 않는지 — detached 감지가 False 인
    브랜치 상태에서 새 mutation 이 로컬 commit 으로 박제되는지 단언한다(HEAD 전진)."""
    bare = tmp_path / "bare-att-c"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)  # attached.
    (board_dir / "tickets" / "open" / "T-0002-t.md").write_text(
        _TICKET_TEXT.format(tid="T-0002"), encoding="utf-8")

    head_before = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    board._board_git_sync_best_effort(
        "new T-0002", (board_dir / "tickets" / "open" / "T-0002-t.md",))
    head_after = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    assert head_after != head_before, \
        "attached 인데 best-effort 가 commit 을 안 함 — detached 가드가 정상 경로를 오차단."
