"""worktree 슬롯 제거 시스템화 — remove_slot 엔진 + `pm-config worktree remove` CLI (T-0333 · ADR-0051).

worktree 슬롯 lifecycle 의 *제거 본체*(수동 `git worktree remove` 위임)를 단일 원자 커맨드로
닫는다. PM 69 footgun 체인(수동 remove → dangling 장부 → `add` 가 번호 skip → 뒤늦은 prune)을
원천 종결하는 게 이 티켓의 핵심이다. 검증:

  - **remove_slot 엔진**(worktree_pool·DI seam·mock git): 정상 제거 / dirty 거부 / --force stash
    보존 / 활성 리스 거부 / 미머지 브랜치 보존 / 공유 브랜치 스킵 / detached / 장부 없음 무해 종료 /
    worktree remove 실패 원자성.
  - **remove→add 번호 재사용 회귀**(실 git·hermetic 임시 repo): remove_slot 경유 시 다음 add 가
    빈 번호를 재사용(skip 없음) + 대조(수동 git worktree remove 는 dangling 장부로 번호 skip).
  - **cmd_worktree_remove CLI 배선**(pm_config·mock worktree_pool 주입): remove_slot 호출 / --force
    전달 / 거부·실패 rc1 안내 / 무해 종료 rc0 / 브랜치 처리 surface / main 라우팅 / help 구분.

hermetic 필수 — worktree_pool 모듈 전역(REPO·LEASES_FILE·WORK_DIR·REPOS_DIR)은 import 시점에
실 repo 절대경로로 굳으므로 tmp 로 재지정한다(test_worktree_pool.py 패턴 동류). board.py 는
import 하지 않는다(touches 격리).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


# ════════════════════════════════════════════════════════════════════════
# worktree_pool.remove_slot — 엔진 (DI seam·mock git)
# ════════════════════════════════════════════════════════════════════════


def _load_wp_bound(proj: Path):
    """worktree_pool.py 를 새로 로드하고 경로 전역을 `proj` tmp 루트로 재바인딩한다.

    import 시점에 굳은 실 REPO 경로를 tmp 로 덮어써 실 `.project_manager` 를 절대 건드리지
    않는다(test_worktree_pool.py._load_wp_bound 와 동형).
    """
    spec = importlib.util.spec_from_file_location("wp_rm_test", TOOLS / "worktree_pool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    local = proj / ".project_manager" / ".local"
    overrides = {
        "REPO": proj,
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "LEASES_LOCK": local / "worktree-leases.lock",
        "WORK_DIR": proj / "work",
        "REPOS_DIR": proj / ".repos",
        "REPO_HOOKS_DIR": local / "repo-hooks",
    }
    for name, val in overrides.items():
        setattr(mod, name, val)
    return mod


@pytest.fixture
def proj(tmp_path):
    """tmp 프로젝트 루트 — .project_manager/.local + work/ + .repos/ 골격."""
    p = tmp_path / "proj"
    (p / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    (p / "work").mkdir(parents=True, exist_ok=True)
    (p / ".repos").mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def wp(proj):
    """tmp-바인딩 worktree_pool 모듈."""
    return _load_wp_bound(proj)


class RemoveFakeGit:
    """remove_slot DI seam 용 mock git — status/symbolic-ref/worktree remove·prune/branch -d/stash 모델.

    - `dirty` → `status --porcelain` 이 변경 1줄(=dirty)·아니면 빈(=clean).
    - `head` → 슬롯 worktree 의 현재 브랜치(`symbolic-ref --short HEAD`). None=detached(rc≠0).
    - `branch_merged` → `git branch -d` rc(True=머지완료 rc0 삭제·False=미머지 rc1 보존).
    - `remove_rc` → `git worktree remove` rc(0=성공·≠0=실패→RuntimeError).
    - `stash_rc` → `git stash push` rc(0=성공 보존·≠0=실패→제거 중단·작업 유실 방지·codex must-fix).
    - `stash_clears_dirty` → stash rc0 시 워킹트리 dirty 를 지우는지. True(정상 stash)면 이후
      `status --porcelain` 이 clean. False(submodule 내부 변경 등 top-level stash 가 못 담는 잔존)면
      stash rc0 라도 dirty 잔존 → remove_slot 이 stash-후 재검사로 중단(codex R2 must-fix 모델).
    """

    def __init__(self, *, dirty=False, head=None, branch_merged=True, remove_rc=0,
                 stash_rc=0, stash_clears_dirty=True):
        self.dirty = dirty
        self.head = head
        self.branch_merged = branch_merged
        self.remove_rc = remove_rc
        self.stash_rc = stash_rc
        self.stash_clears_dirty = stash_clears_dirty
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[:2] == ["status", "--porcelain"]:
            return (0, " M f.py\n") if self.dirty else (0, "")
        if argv == ["symbolic-ref", "--short", "HEAD"]:
            return (1, "fatal: not a symbolic ref\n") if self.head is None else (0, self.head + "\n")
        if argv[:2] == ["worktree", "remove"]:
            return (self.remove_rc, "" if self.remove_rc == 0 else "fatal: cannot remove worktree\n")
        if argv[:2] == ["worktree", "prune"]:
            return (0, "")
        if argv[:2] == ["branch", "-d"]:
            return (0, "Deleted branch\n") if self.branch_merged else (1, "error: not fully merged\n")
        if argv[:2] == ["stash", "push"]:
            if self.stash_rc == 0:
                if self.stash_clears_dirty:
                    self.dirty = False   # 정상 stash — top-level dirty 를 담아 워킹트리 clean.
                return (0, "Saved working directory\n")
            return (self.stash_rc, "error: cannot save the current worktree state\n")
        return (0, "")

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


def _seed(wp, *leases):
    with wp._lease_lock():
        wp._write_ledger(list(leases))


def _lease(wp, *, slot, repo, session="s1", pid=None, state="leased"):
    return wp.Lease(slot=slot, repo=repo, session=session,
                    pid=os.getpid() if pid is None else pid, started="t", state=state)


def test_remove_slot_normal_removes_and_deletes_merged_branch(wp):
    """정상 제거 — idle·clean·전용 브랜치(머지 완료) → worktree remove + branch -d + 장부 삭제."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(head="A_1", branch_merged=True)
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert result is not None
    assert git.did("worktree", "remove", str(wp.slot_path("work/A_1")))
    assert git.did("worktree", "prune")
    assert git.did("branch", "-d", "A_1")
    assert result.branch_action == "deleted"
    assert result.stashed is False
    # ⑤ 장부 엔트리 제거 — 빈 번호 재사용 가능(footgun 종결).
    assert wp.list_leases() == []


def test_remove_slot_dirty_refused_without_force(wp):
    """dirty 거부 — dirty + force 아님 → RemoveRefused("dirty")·worktree 미제거·장부 보존."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(dirty=True, head="A_1")
    with pytest.raises(wp.RemoveRefused) as exc:
        wp.remove_slot("work/A_1", git_runner=git)
    assert exc.value.reason == "dirty"
    assert not git.did("worktree", "remove")
    assert wp.list_leases()[0].slot == "work/A_1"


def test_remove_slot_force_stashes_dirty_and_removes(wp):
    """--force stash 보존 — dirty + force → stash push 후 강제 제거·stashed=True."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(dirty=True, head="A_1", branch_merged=True)
    result = wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert result.stashed is True
    assert git.did("stash", "push")
    assert git.did("worktree", "remove")
    # force 면 worktree remove 에 --force 를 붙인다(dirty/submodule/locked 강제).
    assert git.did("worktree", "remove", str(wp.slot_path("work/A_1")), "--force")
    assert wp.list_leases() == []


def test_remove_slot_force_stash_failure_aborts_without_removing(wp):
    """codex must-fix — force+dirty 인데 stash 실패(rc≠0) → 제거 중단·worktree 미제거·장부 보존.

    stash 반환값을 확인하지 않으면 dirty 변경을 보존 못 한 채 `worktree remove --force` 가 작업을
    날린다. rc≠0 이면 RuntimeError 로 중단(아직 아무것도 안 지웠다)함을 박제한다.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(dirty=True, head="A_1", stash_rc=1)   # stash 실패.
    with pytest.raises(RuntimeError) as exc:
        wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert "stash" in str(exc.value).lower()
    assert git.did("stash", "push")               # stash 는 시도했고,
    assert not git.did("worktree", "remove")      # 실패했으니 worktree 는 안 지운다(작업 유실 방지).
    assert not git.did("branch", "-d")            # 브랜치도 미변경(원자).
    assert wp.list_leases()[0].slot == "work/A_1"  # 장부 보존.


def test_remove_slot_force_stash_residual_dirty_aborts(wp):
    """codex R2 must-fix — stash 성공(rc0) 후에도 여전히 dirty(submodule 내부 등) → 제거 중단.

    top-level `git stash push --include-untracked` 는 submodule 내부 변경을 담지 못한다. stash rc0
    라도 잔존 dirty 를 **stash-후 재검사**(class-fix 일반 불변식·submodule 전용 감지 아님)로 잡아
    worktree remove 미호출·장부 보존 — dirty submodule 작업이 `--force` 로 유실되는 것을 막는다.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(dirty=True, head="A_1", stash_rc=0, stash_clears_dirty=False)
    with pytest.raises(RuntimeError) as exc:
        wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert "stash" in str(exc.value).lower()
    assert git.did("stash", "push")               # stash 는 성공(만들어짐),
    assert not git.did("worktree", "remove")      # 그래도 여전히 dirty → 제거 안 함(작업 유실 방지).
    assert not git.did("branch", "-d")            # 브랜치 미변경(원자).
    assert wp.list_leases()[0].slot == "work/A_1"  # 장부 보존.


def test_remove_slot_force_stash_clears_dirty_proceeds(wp):
    """대조 — 정상 stash(rc0·워킹트리 clean)면 재검사 통과 → 정상 제거(재검사가 정상 경로를 막지 않음)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(dirty=True, head="A_1", stash_rc=0, stash_clears_dirty=True, branch_merged=True)
    result = wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert result.stashed is True
    assert git.did("worktree", "remove")
    assert wp.list_leases() == []


def test_remove_slot_force_override_active_lease_sets_forced_state(wp):
    """reviewer should-fix — --force 로 leased override 시 forced_state 에 원래 state 를 싣는다(CLI 경고용)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other", state="leased"))
    git = RemoveFakeGit(head="A_1", branch_merged=True)
    result = wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert result.forced_state == "leased"
    assert wp.list_leases() == []


def test_remove_slot_force_override_creating_sets_forced_state(wp):
    """--force 로 creating(in-flight) override 시 forced_state="creating" (leased 와 동형)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="creating"))
    git = RemoveFakeGit(head="A_1", branch_merged=True)
    result = wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert result.forced_state == "creating"


def test_remove_slot_idle_removal_forced_state_none(wp):
    """정상(idle) 제거는 forced_state=None (강제 회수 아님·CLI 경고 불요)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(head="A_1", branch_merged=True)
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert result.forced_state is None


def test_remove_slot_force_does_not_escalate_unmerged_branch_delete(wp):
    """suggestion 2 — force=True 여도 미머지 전용 브랜치는 preserved-unmerged (branch -D 로 escalate 안 함).

    force 는 worktree/리스만 강제하지 브랜치 삭제는 항상 `git branch -d`(머지 완료 전용)로 고정 —
    force 가 미머지 브랜치를 -D 로 날리지 않음을 박제(작업 유실 방지).
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(head="A_1", branch_merged=False)
    result = wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert result.branch_action == "preserved-unmerged"
    # 삭제 시도는 `branch -d`(안전) 고정 — `branch -D`(강제)는 절대 안 부른다.
    assert git.did("branch", "-d", "A_1")
    assert not any(c[:2] == ["branch", "-D"] for c in git.calls)


def test_remove_slot_real_path_missing_skips_remove_and_cleans_ledger(wp):
    """suggestion 3 — 실경로 worktree dir 부재 + 장부 잔존 → remove 스킵·prune·장부 정리(dangling).

    git_runner 미주입(실경로) + 슬롯 dir 미생성 → real_path_missing True. worktree remove 를
    건너뛰고(지울 dir 없음) 장부 엔트리만 제거한다(prune-stale 와 겹치는 방어 경로·크래시 0).
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    # 슬롯 dir 미생성(부재) + git_runner=None → real_path_missing True (dirty/branch 판별 불가).
    result = wp.remove_slot("work/A_1")
    assert result is not None
    assert result.branch_action == "none"    # 슬롯 부재라 브랜치 판별 불가.
    assert result.stashed is False
    assert wp.list_leases() == []            # dangling 장부 엔트리 정리.


def test_remove_slot_active_lease_refused_without_force(wp):
    """활성 리스 거부 — leased(사용 중) + force 아님 → RemoveRefused("active-lease")·아무 git op 없음."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other", state="leased"))
    git = RemoveFakeGit(head="A_1")
    with pytest.raises(wp.RemoveRefused) as exc:
        wp.remove_slot("work/A_1", git_runner=git)
    assert exc.value.reason == "active-lease"
    assert exc.value.state == "leased"
    assert not git.did("worktree", "remove")
    assert wp.list_leases()[0].state == "leased"


def test_remove_slot_creating_state_also_refused_without_force(wp):
    """provisional(creating·in-flight 생성)도 활성으로 거부 — force 로만 (state=creating surface)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="creating"))
    git = RemoveFakeGit(head="A_1")
    with pytest.raises(wp.RemoveRefused) as exc:
        wp.remove_slot("work/A_1", git_runner=git)
    assert exc.value.reason == "active-lease"
    assert exc.value.state == "creating"


def test_remove_slot_force_overrides_active_lease(wp):
    """--force 는 활성 리스를 무시하고 제거한다(사용 중 슬롯 강제 회수·경고는 CLI 담당)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other", state="leased"))
    git = RemoveFakeGit(head="A_1", branch_merged=True)
    result = wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert result is not None
    assert git.did("worktree", "remove")
    assert wp.list_leases() == []


def test_remove_slot_unmerged_branch_preserved(wp):
    """미머지 브랜치 보존 — 전용 브랜치가 미머지(branch -d rc≠0) → preserved-unmerged·슬롯은 제거."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(head="A_1", branch_merged=False)
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert result.branch_action == "preserved-unmerged"
    assert git.did("branch", "-d", "A_1")   # 삭제 시도는 한다(rc≠0 = 보존).
    assert wp.list_leases() == []            # 슬롯은 제거(브랜치만 보존).


def test_remove_slot_shared_branch_skips_deletion(wp):
    """공유 브랜치 스킵 — 슬롯이 전용(A_1) 아닌 공유 main 체크아웃 → 브랜치 삭제 자체 스킵."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(head="main")
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert result.branch_action == "skipped-shared"
    assert not git.did("branch", "-d")       # 공유 브랜치는 안 지운다.
    assert wp.list_leases() == []


def test_remove_slot_detached_head_no_branch_action(wp):
    """detached HEAD — 브랜치 판별 불가 → branch_action="none"·삭제 시도 안 함."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(head=None)
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert result.branch_action == "none"
    assert not git.did("branch", "-d")
    assert wp.list_leases() == []


def test_remove_slot_absent_ledger_returns_none(wp):
    """장부 없음 무해 종료 — 엔트리 없으면 None·git op 0(orphan 은 git worktree remove)."""
    git = RemoveFakeGit(head="Z_9")
    result = wp.remove_slot("work/Z_9", git_runner=git)
    assert result is None
    assert git.calls == []   # 아무 git op 도 안 한다(무해).


def test_remove_slot_worktree_remove_failure_raises_and_preserves_ledger(wp):
    """worktree remove 실패 원자성 — rc≠0 → RuntimeError·장부/브랜치 미변경(부분 상태 없음)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(head="A_1", remove_rc=1)
    with pytest.raises(RuntimeError):
        wp.remove_slot("work/A_1", git_runner=git)
    assert wp.list_leases()[0].slot == "work/A_1"   # 장부 보존.
    assert not git.did("branch", "-d")               # 브랜치 정리 도달 안 함(원자).


def test_remove_slot_removes_branch_before_ledger_sensitivity(wp):
    """sensitivity — 브랜치 판별을 무력화(head=None)하면 branch -d 가 안 불림.

    `current_branch` 로 읽은 슬롯 HEAD 가 전용 브랜치 판별의 load-bearing 신호임을 박제한다:
    detached 로 오판하면(head=None) 전용 브랜치가 있어도 삭제가 안 된다.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    # 정상: head=A_1 → branch -d A_1 호출.
    git_ok = RemoveFakeGit(head="A_1", branch_merged=True)
    assert wp.remove_slot("work/A_1", git_runner=git_ok).branch_action == "deleted"
    # 무력화: head=None → branch 판별 불가 → branch -d 호출 0.
    _seed(wp, _lease(wp, slot="work/A_2", repo="A", state="idle"))
    git_blind = RemoveFakeGit(head=None)
    assert wp.remove_slot("work/A_2", git_runner=git_blind).branch_action == "none"
    assert not git_blind.did("branch", "-d")


# ════════════════════════════════════════════════════════════════════════
# remove→add 번호 재사용 회귀 (실 git·PM 69 footgun 종결·DoD 핵심)
# ════════════════════════════════════════════════════════════════════════

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")


def _git(cwd, *argv):
    e = dict(os.environ)
    e.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    return subprocess.run([_GIT, "-C", str(cwd), *argv], check=True,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _mk_real_bare(wp, repo, tmp_path):
    """실 bare repo `.repos/<repo>.git` — `pm-config repo add` 가 만든 것과 동형(ADR-0011 §31)."""
    origin = _init_repo(tmp_path / f"{repo}-origin")
    bare = wp.bare_repo_path(repo)
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))
    return bare


@_git_required
def test_real_git_remove_slot_then_add_reuses_number(proj, tmp_path):
    """remove_slot 경유 시 다음 add 가 빈 번호를 재사용 — PM 69 footgun 체인 종결(DoD 핵심).

    create A_1·A_2 → release+remove_slot A_1(worktree remove + branch -d + 장부 엔트리 삭제) →
    다음 create 가 A_3 로 skip 하지 않고 **A_1 재사용**. remove_slot 이 장부까지 정리(⑤)하고 전용
    브랜치 A_1 을 삭제(④)해야 재사용이 성립한다(둘 중 하나라도 남으면 재사용 실패).
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)

    a1 = wp.create_slot("A", session="me", init_submodules=False)
    a2 = wp.create_slot("A", session="me", init_submodules=False)
    assert a1.slot == "work/A_1" and a2.slot == "work/A_2"

    # 정석 흐름 — release(→idle) 후 remove.
    wp.release("work/A_1")
    result = wp.remove_slot("work/A_1")
    assert result is not None
    assert not wp.slot_path("work/A_1").is_dir(), "worktree 가 실제로 안 지워짐"
    assert all(l.slot != "work/A_1" for l in wp.list_leases()), "장부 엔트리 미삭제(dangling)"
    assert result.branch_action == "deleted", "전용 브랜치 A_1(머지 완료)이 삭제 안 됨"

    # 다음 create 는 A_1 재사용(번호 skip 없음).
    a1b = wp.create_slot("A", session="me", init_submodules=False)
    assert a1b.slot == "work/A_1", f"번호 재사용 실패: {a1b.slot!r} (A_3 skip footgun 재발)"


@_git_required
def test_real_git_manual_worktree_remove_leaves_dangling_ledger_skips_number(proj, tmp_path):
    """대조(footgun 재현) — 수동 `git worktree remove` 는 장부를 남겨 다음 add 가 번호 skip.

    remove_slot 이 장부까지 정리하는 게 왜 번호 재사용을 가능케 하는지 박제한다(T-0333 근거):
    수동 remove(장부 미정리)면 dangling 엔트리 A_1 이 남아 다음 create 가 A_3 로 건너뛴다.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)
    wp.create_slot("A", session="me", init_submodules=False)   # A_1
    wp.create_slot("A", session="me", init_submodules=False)   # A_2

    # 수동 git worktree remove — 장부 미정리(PM 69 실측 체인).
    _git(wp.bare_repo_path("A"), "worktree", "remove",
         str(wp.slot_path("work/A_1")), "--force")

    # 장부엔 A_1 이 dangling 으로 남아 다음 create 가 A_3 로 skip(footgun).
    a3 = wp.create_slot("A", session="me", init_submodules=False)
    assert a3.slot == "work/A_3", \
        "수동 remove 후에도 번호가 재사용되면 remove_slot 대조가 무의미(footgun 전제 붕괴)"


@_git_required
def test_real_git_remove_slot_preserves_unmerged_branch(proj, tmp_path):
    """실 git — 전용 브랜치에 미머지 커밋이 있으면 보존(preserved-unmerged)·슬롯만 제거.

    슬롯 브랜치 A_1 에 main 에 없는 커밋을 추가한 뒤 release+remove — `git branch -d` 가
    미머지로 거부(보존)하고 worktree/장부는 정리된다. bare 에 A_1 브랜치가 살아있는지 확인.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    bare = _mk_real_bare(wp, "A", tmp_path)
    wp.create_slot("A", session="me", init_submodules=False)   # A_1 (branch A_1 at main)
    slot_dir = wp.slot_path("work/A_1")
    # 슬롯 브랜치 A_1 에 main 에 없는 커밋(미머지).
    (slot_dir / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(slot_dir, "add", "wip.txt")
    _git(slot_dir, "commit", "-q", "-m", "unmerged work")

    wp.release("work/A_1")
    result = wp.remove_slot("work/A_1")
    assert result.branch_action == "preserved-unmerged", \
        f"미머지 전용 브랜치가 보존 안 됨: {result.branch_action!r}"
    assert not slot_dir.is_dir(), "worktree 는 제거돼야 한다(브랜치만 보존)"
    # bare 에 A_1 브랜치가 살아있다(미머지라 삭제 거부됨).
    branches = _git(bare, "branch", "--list", "A_1").stdout
    assert "A_1" in branches, "미머지 브랜치가 삭제됨(작업 유실)"


@_git_required
def test_real_git_base_recreate_after_preserved_branch_fails_loud(proj, tmp_path):
    """T-0335 — remove(미머지 보존) 후 같은 번호 base-경로 재생성이 SlotBranchExists 로 fail-loud (오귀인 정정·DoD 핵심).

    슬롯 A_1 을 base-경로(base=main)로 만들고 미머지 커밋을 얹은 뒤 release+remove → 전용 브랜치
    A_1 은 보존(preserved-unmerged)된다. 같은 번호(A_1)를 base-경로로 재생성하면 **옛 동작**은
    cryptic `fatal: a branch named 'A_1' already exists`(rc≠0) + already-exists 오귀인(orphan-
    worktree 정리 안내)으로 죽었다(T-0333 reviewer 실측). **이제** create_slot 이 선-검출해
    `SlotBranchExists`(결정 (b)·정확한 원인=브랜치 잔존 + 두 갈래 안내)로 fail-loud 하고, provisional
    lease 는 롤백된다(중단-안전·T-0295). 실 git·hermetic 임시 repo.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    bare = _mk_real_bare(wp, "A", tmp_path)

    # 슬롯 A_1 을 base-경로(base=main)로 생성 — 슬롯 전용 브랜치 A_1 을 판다.
    wp.create_slot("A", base="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path("work/A_1")
    # 슬롯 브랜치 A_1 에 main 에 없는 커밋(미머지) — remove 시 `branch -d` 가 거부(보존).
    (slot_dir / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(slot_dir, "add", "wip.txt")
    _git(slot_dir, "commit", "-q", "-m", "unmerged work")

    wp.release("work/A_1")
    result = wp.remove_slot("work/A_1")
    assert result.branch_action == "preserved-unmerged", \
        f"미머지 전용 브랜치 A_1 이 보존 안 됨: {result.branch_action!r}"
    assert "A_1" in _git(bare, "branch", "--list", "A_1").stdout, "보존 브랜치 A_1 이 bare 에 없음"

    # 같은 번호(A_1) base-경로 재생성 — 선-검출로 fail-loud(cryptic already exists·orphan 오귀인 제거).
    with pytest.raises(wp.SlotBranchExists) as exc:
        wp.create_slot("A", base="main", session="me", init_submodules=False)
    msg = str(exc.value)
    assert exc.value.branch == "A_1"
    assert exc.value.slot == "work/A_1"
    assert exc.value.base == "main"
    # 정확한 원인(브랜치 잔존) — orphan/worktree 경로 오귀인이 아니다(T-0333 결함 정정).
    assert "orphan" not in msg.lower(), f"orphan-worktree 오귀인 잔존: {msg!r}"
    assert "브랜치" in msg and "A_1" in msg
    # provisional lease 롤백 — 장부에 A_1 잔존 안 함(중단-안전·T-0295).
    assert all(l.slot != "work/A_1" for l in wp.list_leases()), "provisional lease 롤백 실패(장부 dangling)"
    # add 전에 죽었으므로 worktree dir 도 안 남는다.
    assert not wp.slot_path("work/A_1").is_dir(), "실패한 재생성이 worktree 를 남김"


@_git_required
def test_real_git_branch_resume_preserved_unmerged_no_reset(proj, tmp_path):
    """T-0343 — 미머지 보존 브랜치를 create_slot(branch=)로 재개 → 보존 커밋 잔존(리셋 없음·데이터-유실 클래스 종결·DoD).

    슬롯 A_1(base=main)에 미머지 커밋을 얹고 release+remove → 전용 브랜치 A_1 보존. 그 브랜치를
    `create_slot(branch="A_1")` 로 재개하면 **옛 `-B`** 는 A_1 을 bare HEAD(main)로 **리셋**해 미머지
    커밋을 잃었다(T-0335 codex 데이터-유실 클래스). **T-0343** 은 기존 브랜치를 그 tip 에서 **checkout**
    (리셋 없음)해 보존 커밋이 슬롯 HEAD 로 살아있음을 확증한다. 실 git·hermetic 임시 repo.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    bare = _mk_real_bare(wp, "A", tmp_path)

    wp.create_slot("A", base="main", session="me", init_submodules=False)   # A_1 @ main
    slot_dir = wp.slot_path("work/A_1")
    (slot_dir / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(slot_dir, "add", "wip.txt")
    _git(slot_dir, "commit", "-q", "-m", "unmerged work")
    preserved_sha = _git(slot_dir, "rev-parse", "HEAD").stdout.strip()

    wp.release("work/A_1")
    result = wp.remove_slot("work/A_1")
    assert result.branch_action == "preserved-unmerged"
    assert not slot_dir.is_dir()
    assert "A_1" in _git(bare, "branch", "--list", "A_1").stdout, "보존 브랜치 A_1 이 bare 에 없음"

    # 재개 — create_slot(branch="A_1") → 기존 브랜치 checkout(리셋 없음·`-B` 아님).
    lease = wp.create_slot("A", branch="A_1", session="me", init_submodules=False)
    assert lease.slot == "work/A_1"
    resumed_dir = wp.slot_path("work/A_1")
    # 슬롯 HEAD 가 보존 커밋 그대로 — bare HEAD(main)로 리셋되지 않았다(핵심 확증).
    assert _git(resumed_dir, "rev-parse", "HEAD").stdout.strip() == preserved_sha, \
        "재개 슬롯이 보존 커밋을 잃음(-B 리셋 재발·데이터-유실)"
    # 미머지 파일이 워킹트리에 존재(작업 이어가기 가능).
    assert (resumed_dir / "wip.txt").read_text(encoding="utf-8") == "wip\n", "미머지 작업 파일이 유실됨"
    # 현재 브랜치가 A_1 checkout(detached 아님).
    assert wp.current_branch("work/A_1") == "A_1", "재개 슬롯이 A_1 브랜치 checkout 상태가 아님"


@_git_required
def test_real_git_branch_exists_detection_color_safe(proj, tmp_path):
    """T-0343 codex must-fix — ambient `color.branch=always` 서도 branch-존재 판정 정확 (ANSI 오염 백스톱·실 git).

    평문 `git branch --list <b>` 는 `color.branch=always` 서 출력에 ANSI escape(`\\x1b[m`)가 섞여
    `.split()` 토큰이 브랜치명과 불일치 → 기존 브랜치를 "없음"으로 오판 → checkout 대신 `-B` 로 가
    리셋-유실 재개방(codex 실측). `_slot_branch_exists` 는 `--format=%(refname:short)`(color-safe plain)
    로 뽑아 막는다. **fake 출력은 평문이라 이 오염을 못 재현** — 실 git 에 color 를 박아 두 경로를
    실측: (1) 슬롯-전용 브랜치 선-검출(SlotBranchExists) 여전히 발화 + (2) branch= checkout 이 보존
    커밋을 리셋 없이 유지.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    bare = _mk_real_bare(wp, "A", tmp_path)
    # ANSI 오염 재현 — 평문 `branch --list` 를 깨는 조건(색을 non-TTY 에도 강제).
    _git(bare, "config", "color.branch", "always")
    _git(bare, "config", "color.ui", "always")

    # 슬롯 A_1(base=main)에 미머지 커밋 → release+remove → 전용 브랜치 A_1 보존.
    wp.create_slot("A", base="main", session="me", init_submodules=False)
    slot_dir = wp.slot_path("work/A_1")
    (slot_dir / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(slot_dir, "add", "wip.txt")
    _git(slot_dir, "commit", "-q", "-m", "unmerged work")
    preserved_sha = _git(slot_dir, "rev-parse", "HEAD").stdout.strip()
    wp.release("work/A_1")
    assert wp.remove_slot("work/A_1").branch_action == "preserved-unmerged"

    # (1) base-경로 재생성 선-검출: color 오염에도 A_1 존재를 정확 감지 → SlotBranchExists.
    with pytest.raises(wp.SlotBranchExists):
        wp.create_slot("A", base="main", session="me", init_submodules=False)
    assert all(l.slot != "work/A_1" for l in wp.list_leases()), "선-검출 실패 시 provisional 잔존"

    # (2) branch= 재개: color 오염에도 A_1 존재를 정확 감지 → checkout(리셋 없음)·보존 커밋 유지.
    lease = wp.create_slot("A", branch="A_1", session="me", init_submodules=False)
    assert lease.slot == "work/A_1"
    resumed_dir = wp.slot_path("work/A_1")
    assert _git(resumed_dir, "rev-parse", "HEAD").stdout.strip() == preserved_sha, \
        "color.branch=always 오염으로 브랜치를 '없음' 오판 → -B 리셋(데이터-유실 재발·must-fix 미해소)"
    assert wp.current_branch("work/A_1") == "A_1"


# ════════════════════════════════════════════════════════════════════════
# create_slot 미머지-보존 브랜치 충돌 — 선-검출 fail-loud (T-0335·DI seam·hermetic)
# ════════════════════════════════════════════════════════════════════════


class _CreateBaseFakeGit:
    """create_slot base/else/branch-경로 DI seam mock — bare 검증·branch --list(전용 브랜치 잔존)·fetch/show-ref·worktree add.

    `slot_branch_exists` → `_slot_branch_exists` 의 `git branch --list --format=%(refname:short)
    <branch>` 가 그 브랜치를 리스트(잔존·T-0335 선-검출/T-0343 checkout 분기 트리거)한다. False 면
    빈 출력(미존재·정상 생성/`-B`). helper 는 `--format=%(refname:short)`(color-safe plain·T-0343)로
    뽑고 **splitlines 정확-일치**로 판정하므로, 존재 시 브랜치명만 실은 평문 라인(`A_1\\n`·프리픽스/
    ANSI 없음)을 돌려준다·미존재 시 빈 출력. rc 는 무시(branch --list 는 매치 없어도 rc0).
    """

    def __init__(self, *, slot_branch_exists=False):
        self.slot_branch_exists = slot_branch_exists
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")                       # 유효 bare 형식(T-0294)
        if "rev-parse" in argv and "--verify" in argv and argv[-1] == "HEAD":
            return (0, "0123abc\n")                    # HEAD 해소(T-0294)
        if argv[:2] == ["branch", "--list"]:
            name = argv[-1]                            # 브랜치 패턴 = 마지막 인자(--format 은 중간)
            # `%(refname:short)` 평문 — 프리픽스/ANSI 없이 브랜치명만(splitlines 정확-일치·color-safe).
            return (0, f"{name}\n") if self.slot_branch_exists else (0, "")
        if argv[:2] == ["fetch", "origin"]:
            return (0, "")
        if argv[:3] == ["show-ref", "--verify", "--quiet"]:
            return (0, "")                             # origin/<base> 해소
        if argv == ["symbolic-ref", "--short", "HEAD"]:
            return (0, "A_1\n")
        return (0, "")                                 # worktree add/list·submodule 등 성공

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


def _mk_bare_dir(wp, repo):
    """bare 부재 가드(`bare.exists()`) 통과용 placeholder — is-bare 유효성은 injected mock 이 모델."""
    wp.bare_repo_path(repo).mkdir(parents=True, exist_ok=True)


def test_create_slot_base_preexisting_branch_raises_slot_branch_exists(wp):
    """T-0335 — create_slot(base=) 이 파려는 슬롯 전용 브랜치가 이미 존재하면 SlotBranchExists (선-검출·fail-loud)."""
    _mk_bare_dir(wp, "A")
    git = _CreateBaseFakeGit(slot_branch_exists=True)
    with pytest.raises(wp.SlotBranchExists) as exc:
        wp.create_slot("A", base="develop", session="me", init_submodules=False, git_runner=git)
    assert exc.value.branch == "A_1"
    assert exc.value.base == "develop"
    assert exc.value.slot == "work/A_1"
    # 선-검출 = worktree add 를 아예 안 부른다(add 전에 raise·cryptic 에러 회피). color-safe argv 확인.
    assert git.did("branch", "--list", "--format=%(refname:short)", "A_1")
    assert not git.did("worktree", "add")
    # 오귀인 제거 — 진단이 브랜치 잔존이지 orphan-worktree 가 아니다.
    assert "orphan" not in str(exc.value).lower()
    # provisional lease 롤백(중단-안전·T-0295) — 장부에 A_1 잔존 안 함.
    assert all(l.slot != "work/A_1" for l in wp.list_leases())


def test_create_slot_base_no_preexisting_branch_proceeds(wp):
    """대조 — 전용 브랜치 미존재면 선-검출 통과 → 정상 worktree add (선-검출이 정상 경로를 막지 않음)."""
    _mk_bare_dir(wp, "A")
    git = _CreateBaseFakeGit(slot_branch_exists=False)
    lease = wp.create_slot("A", base="develop", session="me", init_submodules=False, git_runner=git)
    assert lease.slot == "work/A_1"
    assert git.did("branch", "--list", "--format=%(refname:short)", "A_1")   # 선-검출(color-safe)은 수행하되,
    assert git.did("worktree", "add", "--no-track", "-b", "A_1")     # 미존재라 정상 진행.


def test_create_slot_else_path_preexisting_branch_also_guarded(wp):
    """T-0335 클래스-fix — base 미지정(else) 경로도 슬롯 전용 브랜치 잔존을 선-검출 (같은 브랜치명 자동 생성).

    `git worktree add <path>`(base·branch 미지정)는 git 이 path basename(=A_1)으로 브랜치를 자동
    생성하므로 base-경로와 **동일 충돌 클래스**다 — 솔로/무base 사용자도 fail-loud 로 보호한다.
    """
    _mk_bare_dir(wp, "A")
    git = _CreateBaseFakeGit(slot_branch_exists=True)
    with pytest.raises(wp.SlotBranchExists) as exc:
        wp.create_slot("A", session="me", init_submodules=False, git_runner=git)  # base 미지정 = else
    assert exc.value.branch == "A_1"
    assert exc.value.base is None                 # else-경로는 base None.
    assert not git.did("worktree", "add")


def test_create_slot_branch_path_existing_branch_checks_out_no_reset(wp):
    """T-0343 — 명시 `branch=` 가 기존 브랜치면 checkout(`add <path> <branch>`·리셋 없음·`-B` 아님).

    branch-존재 검사(`_slot_branch_exists`·color-safe `--format=%(refname:short)`) → 존재하면 그 tip
    에서 checkout 한다(리셋 없음·보존 커밋 유지). 옛 `-B`(create-or-reset)는 기존 브랜치를 bare HEAD 로
    리셋해 미머지-보존 커밋을 잃던 데이터-유실 클래스(T-0335 codex) — T-0343 이 이 checkout 분기로 API
    에서 닫는다. 명시 `branch=` 는 SlotBranchExists(슬롯-전용 브랜치 선-검출·base/else 전용)를 안 탄다
    (사용자 직접 지정=명시 의도).
    """
    _mk_bare_dir(wp, "A")
    git = _CreateBaseFakeGit(slot_branch_exists=True)   # 요청 브랜치 존재를 모델
    lease = wp.create_slot("A", branch="a1", session="me", init_submodules=False, git_runner=git)
    assert lease.slot == "work/A_1"                      # SlotBranchExists 안 남(명시 branch= 는 가드 밖).
    assert git.did("branch", "--list", "--format=%(refname:short)", "a1")   # color-safe 존재 검사 수행,
    # 기존 브랜치 → checkout(리셋 없음)·`-B` 절대 안 씀(보존 커밋 유실 방지·T-0343).
    assert git.did("worktree", "add", str(wp.slot_path("work/A_1")), "a1")
    assert not any(c[:3] == ["worktree", "add", "-B"] for c in git.calls)


def test_create_slot_branch_path_new_branch_uses_dash_b(wp):
    """T-0343 무회귀 — 명시 `branch=` 가 신규 브랜치면 `-B`(생성·리셋 대상 없어 안전·기존 동작 보존).

    `add <path> <newbranch>` 는 "invalid reference" 로 죽으므로 신규 브랜치는 `-B`(create)로 판다 —
    존재 검사 rc≠0/빈 출력 경로가 옛 `-B` 동작을 그대로 유지함을 박제(checkout 분기가 신규를 안 깬다).
    """
    _mk_bare_dir(wp, "A")
    git = _CreateBaseFakeGit(slot_branch_exists=False)  # 요청 브랜치 부재
    lease = wp.create_slot("A", branch="a1", session="me", init_submodules=False, git_runner=git)
    assert lease.slot == "work/A_1"
    assert git.did("branch", "--list", "--format=%(refname:short)", "a1")   # color-safe 존재 검사 수행,
    assert git.did("worktree", "add", "-B", "a1")       # 부재 → -B 생성(무회귀).


def test_create_slot_post_hoc_branch_exists_not_misattributed_orphan(wp):
    """T-0335 오귀인 정정 — worktree add 가 'a branch named ... already exists' 로 죽으면 브랜치 진단(orphan 아님).

    선-검출을 우회(branch --list 빈)했으나 worktree add 가 브랜치-존재 에러를 내는 잔여/레이스 경로
    에서도, 옛 코드의 already-exists 부분매치 오귀인(orphan-worktree 정리 안내)을 내지 않고 브랜치
    잔존 진단을 낸다(post-hoc 분리 판정·클래스-fix 방어).
    """
    _mk_bare_dir(wp, "A")

    class _G(_CreateBaseFakeGit):
        def __call__(self, argv):
            if argv[:2] == ["worktree", "add"]:
                self.calls.append(list(argv))
                return (128, "fatal: a branch named 'A_1' already exists\n")
            return super().__call__(argv)

    git = _G(slot_branch_exists=False)   # 선-검출은 통과(빈), add 가 브랜치-존재로 죽음.
    with pytest.raises(RuntimeError) as exc:
        wp.create_slot("A", base="develop", session="me", init_submodules=False, git_runner=git)
    msg = str(exc.value)
    assert "A_1" in msg
    # 오귀인 제거 — orphan/worktree 경로 정리 안내가 아니라 브랜치 잔존 안내.
    assert "orphan" not in msg.lower()
    assert "브랜치" in msg


# ════════════════════════════════════════════════════════════════════════
# cmd_worktree_remove — CLI 배선 (pm_config·mock worktree_pool 주입)
# ════════════════════════════════════════════════════════════════════════


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config_rm", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pc():
    return _load_pm_config()


class FakeRemoveResult:
    def __init__(self, slot, branch, branch_action, stashed, forced_state=None):
        self.slot = slot
        self.branch = branch
        self.branch_action = branch_action
        self.stashed = stashed
        self.forced_state = forced_state


class FakeWP:
    """worktree_pool 대역 — remove_slot 호출을 기록하고 결과/예외를 결정적으로 낸다(DI seam)."""

    class RemoveRefused(Exception):
        def __init__(self, slot, reason, *, state=None):
            self.slot = slot
            self.reason = reason
            self.state = state
            super().__init__(f"{slot} {reason}")

    def __init__(self, *, result="ok", raises=None, branch_action="deleted",
                 branch="A_1", stashed=False, forced_state=None):
        self.calls: list[tuple] = []
        self._result = result           # "ok" → FakeRemoveResult · None → None(무해)
        self._raises = raises            # 예외 인스턴스 또는 None
        self._branch_action = branch_action
        self._branch = branch
        self._stashed = stashed
        self._forced_state = forced_state

    def remove_slot(self, slot, *, force=False):
        self.calls.append(("remove_slot", slot, force))
        if self._raises is not None:
            raise self._raises
        if self._result is None:
            return None
        return FakeRemoveResult(slot, self._branch, self._branch_action,
                                self._stashed, self._forced_state)

    def did(self, name) -> bool:
        return any(c[0] == name for c in self.calls)


def test_cmd_remove_wires_to_remove_slot(pc, capsys):
    """`worktree remove <slot>` → worktree_pool.remove_slot(slot, force=False)."""
    wp = FakeWP()
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 0
    assert ("remove_slot", "work/A_1", False) in wp.calls
    assert "제거" in capsys.readouterr().out


def test_cmd_remove_force_passes_force_true(pc, capsys):
    """`worktree remove <slot> --force` → remove_slot(slot, force=True)."""
    wp = FakeWP()
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=True), worktree_pool=wp)
    assert rc == 0
    assert ("remove_slot", "work/A_1", True) in wp.calls


def test_cmd_remove_dirty_refused_rc1(pc, capsys):
    """RemoveRefused("dirty") → rc1 + dirty·--force 안내(작업 유실 방지)."""
    wp = FakeWP(raises=FakeWP.RemoveRefused("work/A_1", "dirty"))
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 1
    err = capsys.readouterr().err
    assert "dirty" in err and "--force" in err


def test_cmd_remove_active_lease_refused_rc1(pc, capsys):
    """RemoveRefused("active-lease") → rc1 + 사용 중·release 먼저 안내."""
    wp = FakeWP(raises=FakeWP.RemoveRefused("work/A_1", "active-lease", state="leased"))
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 1
    err = capsys.readouterr().err
    assert "사용 중" in err and "release" in err


def test_cmd_remove_absent_ledger_harmless_rc0(pc, capsys):
    """remove_slot 이 None(장부 없음) → 무해 종료 rc0 + orphan 안내."""
    wp = FakeWP(result=None)
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/gone_9", force=False), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "이미 정리됨" in out and "git worktree remove" in out


def test_cmd_remove_git_failure_rc1(pc, capsys):
    """remove_slot 이 RuntimeError(worktree remove 실패) → rc1 + 실패 안내."""
    wp = FakeWP(raises=RuntimeError("git worktree remove failed for 'work/A_1'"))
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 1
    assert "제거 실패" in capsys.readouterr().err


def test_cmd_remove_deleted_branch_surfaces(pc, capsys):
    """전용 브랜치 삭제(머지 완료)를 출력에 surface."""
    wp = FakeWP(branch_action="deleted", branch="A_1")
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 0
    assert "삭제(머지 완료)" in capsys.readouterr().out


def test_cmd_remove_preserved_branch_surfaces(pc, capsys):
    """미머지 브랜치 보존을 '브랜치 X 보존(미머지)' 1줄로 surface(결정 문구)."""
    wp = FakeWP(branch_action="preserved-unmerged", branch="A_1")
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 0
    assert "보존(미머지)" in capsys.readouterr().out


def test_cmd_remove_skipped_shared_surfaces(pc, capsys):
    """공유 브랜치 스킵을 surface(공유 브랜치 보호)."""
    wp = FakeWP(branch_action="skipped-shared", branch="main")
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 0
    assert "스킵" in capsys.readouterr().out


def test_cmd_remove_stash_note_surfaces(pc, capsys):
    """--force stash 보존 시 stash 안내를 출력에 surface."""
    wp = FakeWP(branch_action="deleted", branch="A_1", stashed=True)
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=True), worktree_pool=wp)
    assert rc == 0
    assert "stash 보존" in capsys.readouterr().out


def test_cmd_remove_stash_recovery_line_surfaces(pc, capsys):
    """suggestion 1 — stash 보존 시 '복구: git stash list/pop (공유 refs/stash)' UX 1줄."""
    wp = FakeWP(branch_action="deleted", branch="A_1", stashed=True)
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=True), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "복구" in out and "git stash" in out


def test_cmd_remove_forced_state_warns_on_stderr(pc, capsys):
    """reviewer should-fix — forced_state 있으면 '⚠ 활성 강제 회수' 를 stderr 로 찍는다."""
    wp = FakeWP(branch_action="deleted", branch="A_1", forced_state="leased")
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=True), worktree_pool=wp)
    assert rc == 0
    cap = capsys.readouterr()
    assert "강제 회수" in cap.err and "leased" in cap.err
    assert "⚠" in cap.err


def test_cmd_remove_no_forced_state_no_warning(pc, capsys):
    """정상(idle·forced_state=None) 제거는 강제-회수 경고를 안 찍는다(오경보 방지)."""
    wp = FakeWP(branch_action="deleted", branch="A_1", forced_state=None)
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 0
    assert "강제 회수" not in capsys.readouterr().err


def test_cmd_remove_preserved_unmerged_caveat_surfaces(pc, capsys):
    """reviewer emergent gap — 미머지 보존 시 재생성 캐비앗 1줄 (T-0335 선-검출 진단·재개=수동 checkout 문구).

    재개 안내는 **수동 checkout**(`git worktree add`)으로 준다 — `create_slot(branch=)` 의 `-B`
    create-or-reset 데이터-유실(codex must-fix)을 피한다. 문구에 SlotBranchExists + 수동 checkout 확인.
    """
    wp = FakeWP(branch_action="preserved-unmerged", branch="A_1")
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "보존(미머지)" in out
    assert "SlotBranchExists" in out
    # 재개는 수동 checkout(`git worktree add`)으로 — branch= (-B reset) 유도 아님.
    assert "수동 checkout" in out and "git worktree add" in out


def test_cmd_remove_stash_failure_surfaces_rc1(pc, capsys):
    """codex must-fix 배선 — remove_slot 이 stash 실패 RuntimeError → rc1 + 제거 실패 안내."""
    wp = FakeWP(raises=RuntimeError("git stash failed for 'work/A_1'"))
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=True), worktree_pool=wp)
    assert rc == 1
    assert "제거 실패" in capsys.readouterr().err


def test_cmd_remove_engine_missing_rc1(pc, monkeypatch, capsys):
    """엔진 로드 실패 → rc1 + 안내(다른 서브커맨드 동형·크래시 0)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False))
    assert rc == 1
    assert "worktree_pool.py 엔진을 찾을 수 없다" in capsys.readouterr().err


def test_main_routes_worktree_remove(pc, monkeypatch):
    """main(["worktree","remove","work/A_1"]) → cmd_worktree_remove → remove_slot(force=False)."""
    wp = FakeWP()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["worktree", "remove", "work/A_1"])
    assert rc == 0
    assert ("remove_slot", "work/A_1", False) in wp.calls


def test_main_routes_worktree_remove_force(pc, monkeypatch):
    """main(["worktree","remove","work/A_1","--force"]) → remove_slot(force=True) (플래그 파싱 e2e)."""
    wp = FakeWP()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["worktree", "remove", "work/A_1", "--force"])
    assert rc == 0
    assert ("remove_slot", "work/A_1", True) in wp.calls


def test_build_parser_worktree_remove_parses_slot_and_force(pc):
    """build_parser 가 `worktree remove <slot> [--force]` 를 파싱하고 cmd_worktree_remove 로 라우팅."""
    parser = pc.build_parser()
    args = parser.parse_args(["worktree", "remove", "work/A_1", "--force"])
    assert args.slot == "work/A_1"
    assert args.force is True
    assert args.func is pc.cmd_worktree_remove
    # --force 미지정 기본 False.
    args2 = parser.parse_args(["worktree", "remove", "work/A_1"])
    assert args2.force is False


def test_worktree_help_surfaces_remove_and_distinguishes_prune(pc, capsys):
    """`worktree --help` 이 remove 서브커맨드 + prune-stale 역할 구분을 surface(DoD·help 명시)."""
    parser = pc.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["worktree", "--help"])
    out = capsys.readouterr().out
    assert "remove" in out
    assert "prune-stale" in out   # help 에 역할 구분 1줄(실 worktree 삭제 vs 부재 장부만 정리).
    assert "미머지" in out          # 미머지 보존 브랜치 캐비앗(base-경로 재생성 막힘·reviewer).
