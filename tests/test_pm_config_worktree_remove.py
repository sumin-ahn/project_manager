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
    """reviewer emergent gap — 미머지 보존 시 '같은 번호 base-경로 재생성 막힘' 캐비앗 1줄."""
    wp = FakeWP(branch_action="preserved-unmerged", branch="A_1")
    rc = pc.cmd_worktree_remove(argparse.Namespace(slot="work/A_1", force=False), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "보존(미머지)" in out
    assert "already exists" in out and "재시도" in out


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
