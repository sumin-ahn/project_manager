"""worktree 슬롯 제거 — remove_slot 엔진 + 브랜치 삭제 판정 + `pm-config worktree remove` CLI.

worktree 슬롯 lifecycle 의 *제거 본체*(수동 `git worktree remove` 위임)를 단일 커맨드로
닫는다(PM 69 footgun 체인: 수동 remove → dangling 장부 → `add` 가 번호 skip → 뒤늦은 prune).
설계 단일 진실: PM 홈 spike `branch-parent-record-and-delete-rules-2026-09-14` 0절 규칙 2·3·4.
검증:

  - **remove_slot 엔진**(DI seam·mock git): 정상 제거(`worktree remove --force`) / 커밋 안 한 변경
    거부(force 무관·stash 없음) / 활성 리스 거부·force 는 점유만 무시 / git 미등록 잔재 트리 거부
    (force 무관·파일 무접촉) / detached / 장부 없음 / worktree remove 실패 뒤 등록 판정 / 승인값 전달 /
    판정 조회 실패는 raise.
  - **브랜치 삭제 판정**(`delete_branch_by_rule`·실 git): 판정 표 행마다 — 보호 브랜치 · 공용 저장소
    기본 브랜치 · 자식 브랜치 · 부모 기록 없음 · 부모 브랜치 없음 · 합쳐짐 삭제 · 합쳐지지 않음(무승인
    보존 + 커밋 목록 + origin 반영 여부 / 승인 삭제) · 끝 커밋 이동 · `branch -D` 거부.
  - **실 git 슬롯 제거**: 번호 재사용 / 하위 모듈 있는 깨끗한 슬롯 제거 / 하위 모듈 안 변경 거부 /
    합쳐지지 않은 브랜치 보존·승인 삭제 / 읽기 전용 잔재 / SlotBranchExists / 보존 브랜치 재개.
  - **cmd_worktree_remove CLI**: 승인값 게이트(없음/다름/같음) / 거부 문구 / 판정 출력 / 파서·help.

hermetic 필수 — worktree_pool 모듈 전역(REPO·LEASES_FILE·WORK_DIR·REPOS_DIR)은 import 시점에
실 repo 절대경로로 굳으므로 tmp 로 재지정한다(test_worktree_pool.py 패턴 동류). board.py 는
import 하지 않는다(보호목록은 `_resolve_protected` 를 monkeypatch 한다).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from _git_fixture import init_git_repo

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
def wp(proj, monkeypatch):
    """tmp-바인딩 worktree_pool 모듈 — 보호목록은 `main` 하나로 고정(실 areas.md 미접촉)."""
    mod = _load_wp_bound(proj)
    monkeypatch.setattr(mod, "_resolve_protected", lambda repo: ["main"])
    return mod


_HEADS = "refs/heads/"
_COMMIT_SUFFIX = "^{commit}"
_PARENT_SUFFIX = ".pm-parent"
_FORK_SUFFIX = ".pm-fork"
_FAKE_TIP = "c0ffee0000000000000000000000000000000000"


class RemoveFakeGit:
    """remove_slot DI seam 용 mock git — 등록·변경·HEAD·worktree remove/prune 과 브랜치 판정 조회를 모델링한다.

    - `slot` → 등록 상태를 모델링할 슬롯(경로는 `wp.slot_path(slot)` 으로 해소).
    - `registered` / `list_rc` → `worktree list --porcelain` 이 그 경로를 등록으로 내는지 / rc(≠0=조회 실패).
    - `dirty` → `status --porcelain` 이 변경 1줄(=커밋 안 한 변경)·아니면 빈 출력.
    - `head` → 슬롯 worktree 의 현재 브랜치(`symbolic-ref HEAD` full ref). None=detached(rc≠0).
    - `default_branch` → 공용 저장소 HEAD(`--git-dir . rev-parse --symbolic-full-name HEAD`). None=조회 실패.
    - `parents` → 부모 기록 `{브랜치: 부모}` — 브랜치마다 `pm-parent`·`pm-fork` 두 키로 펼쳐
      `config --get <키>`·`config --get-regexp <정규식>`(정규식에 맞는 키) 가 낸다(없으면 rc1).
      기록된 브랜치와 그 부모, 슬롯 HEAD, 기본 브랜치는 로컬 브랜치로 존재한다(`show-ref`·`rev-parse --verify`).
    - `merged` → `merge-base --is-ancestor` rc(True=0 조상·False=1 조상 아님).
    - `delete_rc` → `branch -D` rc.
    - `remove_rc` / `deregisters_on_failure` → `git worktree remove` rc / rc≠0 인데 등록은 이미 지워진
      실측 형상(읽기 전용 잔재).
    """

    def __init__(self, wp, *, slot="work/A_1", registered=True, list_rc=0, dirty=False,
                 head=None, default_branch="main", parents=None, merged=True, delete_rc=0,
                 remove_rc=0, deregisters_on_failure=False):
        self.slot_dir = wp.slot_path(slot)
        self.registered = registered
        self.list_rc = list_rc
        self.dirty = dirty
        self.head = head
        self.default_branch = default_branch
        self.parents = dict(parents or {})
        self.merged = merged
        self.delete_rc = delete_rc
        self.remove_rc = remove_rc
        self.deregisters_on_failure = deregisters_on_failure
        self.calls: list[list] = []

    def _config(self) -> dict:
        """부모 기록을 git 설정 키로 펼친다 — `branch.<b>.pm-parent`=부모·`branch.<b>.pm-fork`=고정 커밋."""
        config: dict = {}
        for child, parent in self.parents.items():
            config[f"branch.{child}{_PARENT_SUFFIX}"] = parent
            config[f"branch.{child}{_FORK_SUFFIX}"] = "f0f0f0"
        return config

    def _branches(self) -> set:
        names = {self.head, self.default_branch, *self.parents, *self.parents.values()}
        return {name for name in names if name}

    def _porcelain(self) -> str:
        if not self.registered:
            return "worktree /nowhere/bare.git\nbare\n\n"
        return (f"worktree /nowhere/bare.git\nbare\n\n"
                f"worktree {self.slot_dir}\nHEAD 0123abc\n"
                + (f"branch refs/heads/{self.head}\n" if self.head else "detached\n"))

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv == ["worktree", "list", "--porcelain"]:
            if self.list_rc != 0:
                return (self.list_rc, "fatal: not a git repository\n")
            return (0, self._porcelain())
        if argv[:2] == ["status", "--porcelain"]:
            return (0, " M f.py\n") if self.dirty else (0, "")
        if argv == ["symbolic-ref", "HEAD"]:
            return (1, "fatal: not a symbolic ref\n") if self.head is None else (0, _HEADS + self.head + "\n")
        if argv == ["rev-parse", "--git-common-dir"]:
            return (0, ".\n")
        if argv == ["--git-dir", ".", "rev-parse", "--symbolic-full-name", "HEAD"]:
            if self.default_branch is None:
                return (128, "fatal: ambiguous argument 'HEAD'\n")
            return (0, _HEADS + self.default_branch + "\n")
        if argv[:2] == ["worktree", "remove"]:
            if self.remove_rc == 0:
                self.registered = False
                return (0, "")
            if self.deregisters_on_failure:
                self.registered = False   # git 은 등록을 지운 뒤 트리 삭제에서 실패한다(실측).
            return (self.remove_rc, "fatal: cannot remove worktree\n")
        if argv[:2] == ["worktree", "prune"]:
            return (0, "")
        if argv[:2] == ["config", "--get-regexp"]:
            lines = [f"{key} {value}\n" for key, value in self._config().items()
                     if re.search(argv[2], key)]
            return (0, "".join(lines)) if lines else (1, "")   # git: 맞는 키가 없으면 rc1
        if argv[:2] == ["config", "--get"]:
            value = self._config().get(argv[2])
            return (1, "") if value is None else (0, value + "\n")
        if argv[:3] == ["show-ref", "--verify", "--quiet"]:
            ref = argv[3]
            exists = ref.startswith(_HEADS) and ref[len(_HEADS):] in self._branches()
            return (0, "") if exists else (1, "")
        if argv[:3] == ["rev-parse", "--verify", "--quiet"]:
            name = argv[3][len(_HEADS):-len(_COMMIT_SUFFIX)]
            return (0, _FAKE_TIP + "\n") if name in self._branches() else (1, "")
        if argv[:2] == ["merge-base", "--is-ancestor"]:
            return (0, "") if self.merged else (1, "")
        if argv[:1] == ["log"]:
            return (0, "c0ffee0 wip\n")
        if argv[:2] == ["branch", "-D"]:
            if self.delete_rc == 0:
                return (0, "Deleted branch\n")
            return (self.delete_rc, "error: cannot delete branch used by worktree\n")
        return (0, "")

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


def _seed(wp, *leases, tree=True):
    """리스 장부를 쓰고, 기본으로 그 슬롯 디렉터리도 세운다.

    `remove_slot` 은 지울 트리가 실재하는지를 파일시스템에 묻는다(주입 runner 여부와 무관) —
    hermetic 케이스도 슬롯 자리를 실물로 둬야 등록 조회 이후 경로를 탄다. `tree=False` 는
    트리가 이미 사라진 형상(dangling 장부)을 재현하는 자리다.
    """
    with wp._lease_lock():
        wp._write_ledger(list(leases))
    if tree:
        for lease in leases:
            wp.slot_path(lease.slot).mkdir(parents=True, exist_ok=True)


def _lease(wp, *, slot, repo, session="s1", state="leased"):
    return wp.Lease(slot=slot, repo=repo, session=session, started="t", state=state)


def test_remove_slot_clean_idle_slot_removes_worktree_and_deletes_merged_branch(wp):
    """정상 제거 — idle·변경 없음·부모에 합쳐진 브랜치 → `worktree remove --force` + `branch -D` + 장부 삭제.

    `--force` 는 변경 없음을 엔진이 확인한 뒤에만 붙는다(하위 모듈이 있는 깨끗한 슬롯 때문).
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"}, merged=True)
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert git.did("worktree", "remove", "--force", str(wp.slot_path("work/A_1")))
    assert git.did("worktree", "prune")
    assert git.did("branch", "-D", "A_1")
    deletion = result.branch_deletion
    assert (deletion.action, deletion.reason) == (wp.BRANCH_DELETED, wp.BRANCH_REASON_MERGED)
    assert deletion.parent == "develop" and deletion.parent_ref == "develop"
    assert result.branch == "A_1" and result.tree_cleaned is False
    assert wp.list_leases() == []            # 빈 번호 재사용 가능(footgun 종결).


@pytest.mark.parametrize("force", [False, True])
def test_remove_slot_uncommitted_changes_refused_with_or_without_force(wp, force):
    """커밋 안 한 변경 → force 와 무관하게 RemoveRefused("dirty") · stash 없음 · 아무것도 안 지운다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, dirty=True, head="A_1", parents={"A_1": "develop"})
    with pytest.raises(wp.RemoveRefused) as exc:
        wp.remove_slot("work/A_1", force=force, git_runner=git)
    assert exc.value.reason == "dirty"
    assert not git.did("stash")
    assert not git.did("worktree", "remove")
    assert not git.did("branch", "-D")
    assert wp.list_leases()[0].slot == "work/A_1"
    assert wp.slot_path("work/A_1").is_dir()


@pytest.mark.parametrize("state", ["leased", "creating"])
def test_remove_slot_active_lease_refused_without_force(wp, state):
    """활성 리스(사용 중·in-flight 생성) + force 아님 → RemoveRefused("active-lease")·아무 git op 없음."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other", state=state))
    git = RemoveFakeGit(wp, head="A_1")
    with pytest.raises(wp.RemoveRefused) as exc:
        wp.remove_slot("work/A_1", git_runner=git)
    assert (exc.value.reason, exc.value.state) == ("active-lease", state)
    assert git.calls == []
    assert wp.list_leases()[0].state == state


@pytest.mark.parametrize("state", ["leased", "creating"])
def test_remove_slot_force_overrides_active_lease_and_reports_state(wp, state):
    """--force 는 점유를 무시하고 제거한다 — 원래 state 를 forced_state 에 실어 CLI 가 강제 회수를 경고한다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", session="other", state=state))
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"})
    result = wp.remove_slot("work/A_1", force=True, git_runner=git)
    assert result.forced_state == state
    assert git.did("worktree", "remove")
    assert wp.list_leases() == []


def test_remove_slot_idle_removal_forced_state_none(wp):
    """정상(idle) 제거는 forced_state=None (강제 회수 아님·CLI 경고 불요)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"})
    assert wp.remove_slot("work/A_1", git_runner=git).forced_state is None


def test_remove_slot_tree_missing_prunes_and_cleans_ledger(wp):
    """worktree dir 부재 + 장부 잔존 → remove 스킵·prune·장부 정리(dangling)·브랜치 판정 없음."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"), tree=False)
    git = RemoveFakeGit(wp, head="A_1")
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert result.branch is None and result.branch_deletion is None
    assert git.did("worktree", "prune") and not git.did("worktree", "remove")
    assert wp.list_leases() == []


def test_remove_slot_detached_head_no_branch_judgement(wp):
    """detached HEAD — 브랜치 판별 불가 → branch_deletion=None·삭제 시도 안 함·슬롯은 제거."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head=None)
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert result.branch is None and result.branch_deletion is None
    assert not git.did("branch", "-D")
    assert wp.list_leases() == []


def test_remove_slot_absent_ledger_returns_none(wp):
    """장부 없음 무해 종료 — 엔트리 없으면 None·git op 0(orphan 은 git worktree remove)."""
    git = RemoveFakeGit(wp, head="Z_9")
    assert wp.remove_slot("work/Z_9", git_runner=git) is None
    assert git.calls == []


def test_remove_slot_registration_intact_failure_changes_nothing(wp):
    """remove 실패 + 등록 잔존 → RuntimeError·장부/브랜치/디렉터리 미변경(재시도 방법을 지어내지 않는다)."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"}, remove_rc=1)
    with pytest.raises(RuntimeError) as exc:
        wp.remove_slot("work/A_1", git_runner=git)
    assert "--force" not in str(exc.value) and "원자" not in str(exc.value)
    assert wp.list_leases()[0].slot == "work/A_1"
    assert not git.did("branch", "-D")
    assert wp.slot_path("work/A_1").is_dir()


def test_remove_slot_finishes_when_git_deregistered_but_tree_remains(wp):
    """remove 실패인데 등록은 사라진 실측 형상 → git 이 판정을 통과시킨 뒤이므로 잔재를 마저 지우고 마무리한다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"}, remove_rc=1,
                        deregisters_on_failure=True)
    result = wp.remove_slot("work/A_1", git_runner=git)
    assert result.tree_cleaned is True
    assert not wp.slot_path("work/A_1").exists(), "디렉터리 잔재가 남았다"
    assert result.branch_deletion.action == wp.BRANCH_DELETED
    assert wp.list_leases() == []


@pytest.mark.parametrize("force", [False, True])
def test_remove_slot_unregistered_tree_refused_with_or_without_force(wp, force):
    """git 미등록 잔재 트리 → force 와 무관하게 RemoveRefused("unregistered-tree", path=) · 파일시스템 무접촉.

    등록이 없으면 git 에게 커밋 안 한 변경을 물을 자리가 없어 엔진이 지우지 않는다 — 파일 삭제는
    사용자 몫이고, 거부 값이 사용자에게 넘길 경로를 싣는다.
    """
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    slot_dir = wp.slot_path("work/A_1")
    leftover = slot_dir / "wip.txt"
    leftover.write_text("미커밋 작업\n", encoding="utf-8")
    git = RemoveFakeGit(wp, registered=False, head="A_1")
    with pytest.raises(wp.RemoveRefused) as exc:
        wp.remove_slot("work/A_1", force=force, git_runner=git)
    assert exc.value.reason == "unregistered-tree"
    assert exc.value.path == slot_dir
    assert leftover.read_text(encoding="utf-8") == "미커밋 작업\n"
    assert wp.list_leases()[0].slot == "work/A_1"
    assert not git.did("worktree", "prune") and not git.did("worktree", "remove")


def test_remove_slot_passes_acked_branches_to_judgement(wp):
    """합쳐지지 않은 브랜치 — 승인값이 없으면 보존, 그 브랜치 이름이 승인값에 있으면 삭제한다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"}, merged=False)
    kept = wp.remove_slot("work/A_1", acked_branches=frozenset({"work/A_1"}), git_runner=git)
    assert (kept.branch_deletion.action, kept.branch_deletion.reason) == (
        wp.BRANCH_PRESERVED, wp.BRANCH_REASON_UNMERGED)
    assert not git.did("branch", "-D")

    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"}, merged=False)
    gone = wp.remove_slot("work/A_1", acked_branches=frozenset({"work/A_1", "A_1"}), git_runner=git)
    assert (gone.branch_deletion.action, gone.branch_deletion.reason) == (
        wp.BRANCH_DELETED, wp.BRANCH_REASON_ACKED)
    assert gone.branch_deletion.unmerged_commits == ["c0ffee0 wip"]
    assert git.did("branch", "-D", "A_1")


def test_remove_slot_branch_lookup_failure_raises_without_deleting(wp):
    """판정 조회 실패(공용 저장소 HEAD 미해소)는 보존으로 접지 않고 raise — 삭제 시도 없음·장부는 정지 지점에 남는다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head="task/wip", default_branch=None, parents={"task/wip": "develop"})
    with pytest.raises(wp.BranchParentError) as exc:
        wp.remove_slot("work/A_1", git_runner=git)
    assert "기본 브랜치" in str(exc.value)
    assert not git.did("branch", "-D")
    assert wp.list_leases()[0].slot == "work/A_1"


def test_judgement_ancestor_query_failure_raises(wp):
    """`merge-base --is-ancestor` rc 가 0·1 이 아니면 판정 실패로 raise — 조상 아님(보존)으로 접지 않는다."""
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"})

    def runner(argv):
        if argv[:2] == ["merge-base", "--is-ancestor"]:
            return (128, "fatal: Not a valid commit name\n")
        return git(argv)

    with pytest.raises(wp.BranchParentError):
        wp.delete_branch_by_rule("A_1", repo="A", git_runner=runner)
    assert not git.did("branch", "-D")


# ════════════════════════════════════════════════════════════════════════
# 실 git — 브랜치 삭제 판정 표 행마다 · 슬롯 제거
# ════════════════════════════════════════════════════════════════════════

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")


def _git(cwd, *argv, check=True):
    e = dict(os.environ)
    e.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    return subprocess.run([_GIT, "-C", str(cwd), *argv], check=check,
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


def _sha(repo, ref):
    return _git(repo, "rev-parse", ref).stdout.strip()


def _new_commit(repo, parent, message):
    """`parent` 위 새 커밋 sha — 어떤 ref 도 옮기지 않는다(슬롯 체크아웃 무접촉)."""
    tree = _sha(repo, f"{parent}^{{tree}}")
    return _git(repo, "commit-tree", tree, "-p", parent, "-m", message).stdout.strip()


def _branch_exists(repo, branch):
    return _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


@pytest.fixture
def family(proj, tmp_path, monkeypatch):
    """실 bare + 슬롯 A_1(base main·부모 main 기록) — (wp, bare, slot_dir, bare_runner)."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    monkeypatch.setattr(wp, "_resolve_protected", lambda repo: ["release"])
    bare = _mk_real_bare(wp, "A", tmp_path)
    wp.create_slot("A", base="main", init_submodules=False)
    return wp, bare, wp.slot_path("work/A_1"), wp._real_git_runner(bare)


def _side_branch(wp, bare, runner, name, *, parent="main", commit_message=None):
    """bare 에 브랜치 `name` 을 만들고 부모를 기록한다 — `commit_message` 면 부모 위 커밋 1개를 얹는다."""
    tip = _new_commit(bare, parent, commit_message) if commit_message else _sha(bare, parent)
    _git(bare, "update-ref", f"refs/heads/{name}", tip)
    wp.record_branch_parent(name, parent, _sha(bare, parent), git_runner=runner)
    return tip


@_git_required
def test_real_judge_protected_branch_preserved(family):
    """차례 1 — repo 보호 브랜치는 부모 기록·합쳐짐과 무관하게 보존."""
    wp, bare, _slot_dir, runner = family
    _side_branch(wp, bare, runner, "release")
    deletion = wp.delete_branch_by_rule("release", repo="A", git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_PRESERVED, wp.BRANCH_REASON_PROTECTED)
    assert _branch_exists(bare, "release")


@_git_required
def test_real_judge_shared_default_branch_preserved_even_from_worktree_runner(family, monkeypatch):
    """차례 1 — 공용 저장소 기본 브랜치는 보존. 슬롯 worktree 에 묶인 runner 여도 슬롯 HEAD 가 아니라 공용 HEAD 를 읽는다."""
    wp, bare, slot_dir, runner = family
    monkeypatch.setattr(wp, "_resolve_protected", lambda repo: [])
    slot_runner = wp._real_git_runner(slot_dir)
    assert wp._shared_default_branch(git_runner=slot_runner) == "main"
    deletion = wp.delete_branch_by_rule("main", repo="A", git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_PRESERVED, wp.BRANCH_REASON_DEFAULT)
    assert _branch_exists(bare, "main")


@_git_required
def test_real_judge_branch_with_children_preserved(family):
    """차례 2 — 부모 기록이 이 브랜치를 가리키는 자식이 있으면 보존 + 자식 목록."""
    wp, bare, _slot_dir, runner = family
    _side_branch(wp, bare, runner, "child", parent="A_1")
    deletion = wp.delete_branch_by_rule("A_1", repo="A", git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_PRESERVED, wp.BRANCH_REASON_CHILDREN)
    assert deletion.children == ["child"]
    assert _branch_exists(bare, "A_1")


@_git_required
def test_real_judge_branch_without_parent_record_preserved(family):
    """차례 3 — 부모 기록이 없는 브랜치는 합쳐졌어도 보존(부모를 추측하지 않는다)."""
    wp, bare, _slot_dir, runner = family
    _git(bare, "branch", "loose", "main")
    deletion = wp.delete_branch_by_rule("loose", repo="A", git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_PRESERVED, wp.BRANCH_REASON_NO_PARENT)
    assert _branch_exists(bare, "loose")


@_git_required
def test_real_judge_parent_branch_gone_preserved(family):
    """차례 3 — 부모 브랜치가 지워져 비교 ref 가 없으면 합쳐짐을 잴 수 없어 보존 + 부모 이름."""
    wp, bare, _slot_dir, runner = family
    _git(bare, "branch", "orphaned", "main")
    wp.record_branch_parent("orphaned", "gone-parent", _sha(bare, "main"), git_runner=runner)
    deletion = wp.delete_branch_by_rule("orphaned", repo="A", git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_PRESERVED, wp.BRANCH_REASON_PARENT_GONE)
    assert deletion.parent == "gone-parent"
    assert _branch_exists(bare, "orphaned")


@_git_required
def test_real_judge_merged_branch_deleted_with_its_parent_record(family):
    """차례 5 — 부모에 다 합쳐진 브랜치는 `branch -D` 로 지우고 부모 기록 설정도 함께 사라진다."""
    wp, bare, _slot_dir, runner = family
    _side_branch(wp, bare, runner, "done")
    deletion = wp.delete_branch_by_rule("done", repo="A", git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_DELETED, wp.BRANCH_REASON_MERGED)
    assert deletion.parent_ref == "main"
    assert not _branch_exists(bare, "done")
    assert wp.read_branch_parent("done", git_runner=runner) is None


@_git_required
def test_real_judge_unmerged_without_ack_preserved_with_commits_and_origin_state(family):
    """차례 4 — 합쳐지지 않은 커밋 + 승인값 없음 → 보존 + 커밋 목록 + origin 반영 여부(세 값)."""
    wp, bare, _slot_dir, runner = family
    tip = _side_branch(wp, bare, runner, "feat", commit_message="feat work")
    expected_line = _git(bare, "log", "--format=%h %s", "-1", tip).stdout.strip()

    deletion = wp.delete_branch_by_rule("feat", repo="A", git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_PRESERVED, wp.BRANCH_REASON_UNMERGED)
    assert deletion.unmerged_commits == [expected_line]
    assert deletion.origin == wp.ORIGIN_NO_BRANCH
    assert _branch_exists(bare, "feat")

    _git(bare, "update-ref", "refs/remotes/origin/feat", tip)
    assert wp.delete_branch_by_rule("feat", repo="A", git_runner=runner).origin == wp.ORIGIN_CONTAINS_TIP
    _git(bare, "update-ref", "refs/remotes/origin/feat", _sha(bare, "main"))
    assert wp.delete_branch_by_rule("feat", repo="A", git_runner=runner).origin == wp.ORIGIN_MISSING_TIP
    assert _branch_exists(bare, "feat")


@_git_required
def test_real_judge_unmerged_with_branch_ack_deleted(family):
    """차례 5 — 합쳐지지 않았어도 그 브랜치 이름이 승인값에 있으면 삭제(근거 acked·커밋 목록 유지)."""
    wp, bare, _slot_dir, runner = family
    _side_branch(wp, bare, runner, "feat", commit_message="feat work")
    other_ack = wp.delete_branch_by_rule("feat", repo="A", acked_branches=frozenset({"work/A_1"}),
                                         git_runner=runner)
    assert other_ack.action == wp.BRANCH_PRESERVED          # 다른 이름의 승인값은 이 브랜치를 넘지 않는다.
    deletion = wp.delete_branch_by_rule("feat", repo="A", acked_branches=frozenset({"feat"}),
                                        git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_DELETED, wp.BRANCH_REASON_ACKED)
    assert len(deletion.unmerged_commits) == 1
    assert not _branch_exists(bare, "feat")


@_git_required
def test_real_judge_tip_moved_after_judgement_preserved(family):
    """차례 5 — 판정 때 읽은 끝 커밋과 삭제 직전 끝 커밋이 다르면 보존(판정 뒤 들어온 커밋을 지우지 않는다)."""
    wp, bare, _slot_dir, runner = family
    judged_tip = _side_branch(wp, bare, runner, "feat")
    later = _new_commit(bare, "main", "arrived after judgement")
    tip_reads: list[list] = []

    def racing_runner(argv):
        if argv[:3] == ["rev-parse", "--verify", "--quiet"]:
            tip_reads.append(list(argv))
            if len(tip_reads) == 2:          # 재확인 직전에 다른 세션이 브랜치를 옮겼다.
                _git(bare, "update-ref", "refs/heads/feat", later)
        return runner(argv)

    deletion = wp.delete_branch_by_rule("feat", repo="A", git_runner=racing_runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_PRESERVED, wp.BRANCH_REASON_TIP_MOVED)
    assert judged_tip in deletion.detail and later in deletion.detail
    assert _sha(bare, "refs/heads/feat") == later


@_git_required
def test_real_judge_delete_refused_by_git_preserved_with_output(family):
    """차례 5 — `branch -D` rc≠0(슬롯 worktree 가 그 브랜치를 쥠) → 보존 + git 출력."""
    wp, bare, _slot_dir, runner = family
    deletion = wp.delete_branch_by_rule("A_1", repo="A", git_runner=runner)
    assert (deletion.action, deletion.reason) == (wp.BRANCH_PRESERVED, wp.BRANCH_REASON_DELETE_REFUSED)
    assert deletion.detail.startswith("rc=") and "A_1" in deletion.detail
    assert _branch_exists(bare, "A_1")


@_git_required
def test_real_git_remove_slot_then_add_reuses_number(proj, tmp_path):
    """remove_slot 경유 시 다음 add 가 빈 번호를 재사용 — PM 69 footgun 체인 종결.

    create A_1·A_2 → remove_slot A_1(worktree remove + 합쳐진 전용 브랜치 삭제 + 장부 엔트리 삭제) →
    다음 create 가 A_3 로 skip 하지 않고 **A_1 재사용**. 장부·전용 브랜치 둘 중 하나라도 남으면 재사용 실패.
    """
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)

    a1 = wp.create_slot("A", base="main", init_submodules=False)
    a2 = wp.create_slot("A", base="main", init_submodules=False)
    assert a1.slot == "work/A_1" and a2.slot == "work/A_2"

    assert a1.state == "idle"
    result = wp.remove_slot("work/A_1")
    assert not wp.slot_path("work/A_1").is_dir(), "worktree 가 실제로 안 지워짐"
    assert all(l.slot != "work/A_1" for l in wp.list_leases()), "장부 엔트리 미삭제(dangling)"
    assert result.branch_deletion.action == wp.BRANCH_DELETED, "합쳐진 전용 브랜치 A_1 이 삭제 안 됨"

    a1b = wp.create_slot("A", base="main", init_submodules=False)
    assert a1b.slot == "work/A_1", f"번호 재사용 실패: {a1b.slot!r} (A_3 skip footgun 재발)"


@_git_required
def test_real_git_manual_worktree_remove_leaves_dangling_ledger_skips_number(proj, tmp_path):
    """대조(footgun 재현) — 수동 `git worktree remove` 는 장부를 남겨 다음 add 가 번호 skip."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "A", tmp_path)
    wp.create_slot("A", base="main", init_submodules=False)   # A_1
    wp.create_slot("A", base="main", init_submodules=False)   # A_2

    _git(wp.bare_repo_path("A"), "worktree", "remove", str(wp.slot_path("work/A_1")), "--force")

    a3 = wp.create_slot("A", base="main", init_submodules=False)
    assert a3.slot == "work/A_3", \
        "수동 remove 후에도 번호가 재사용되면 remove_slot 대조가 무의미(footgun 전제 붕괴)"


def _mk_real_bare_with_submodule(wp, repo, tmp_path):
    """하위 모듈 `sub` 를 가진 origin 의 bare 사본."""
    sub = _init_repo(tmp_path / f"{repo}-sub")
    origin = _init_repo(tmp_path / f"{repo}-origin")
    _git(origin, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    _git(origin, "commit", "-q", "-m", "add sub")
    bare = wp.bare_repo_path(repo)
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))
    return bare


@pytest.fixture
def submodule_slot(proj, tmp_path, monkeypatch):
    """하위 모듈을 초기화한 슬롯 A_1 — (wp, bare, slot_dir)."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    monkeypatch.setattr(wp, "_resolve_protected", lambda repo: ["main"])
    bare = _mk_real_bare_with_submodule(wp, "A", tmp_path)
    wp.create_slot("A", base="main", init_submodules=False)
    slot_dir = wp.slot_path("work/A_1")
    _git(slot_dir, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "-q")
    assert (slot_dir / "sub" / "README.md").is_file()
    return wp, bare, slot_dir


@_git_required
def test_real_git_remove_clean_slot_with_submodule_succeeds(submodule_slot):
    """하위 모듈이 있는 깨끗한 슬롯 — `--force` 없는 `git worktree remove` 는 거부되지만 엔진 제거는 성공한다."""
    wp, bare, slot_dir = submodule_slot
    plain = _git(bare, "worktree", "remove", str(slot_dir), check=False)
    assert plain.returncode != 0, f"전제 붕괴 — --force 없이도 제거됐다: {plain.stdout}{plain.stderr}"
    assert slot_dir.is_dir()

    result = wp.remove_slot("work/A_1")
    assert not slot_dir.exists()
    assert wp.list_leases() == []
    assert result.branch_deletion.action == wp.BRANCH_DELETED


@_git_required
def test_real_git_remove_refuses_untracked_file_inside_submodule(submodule_slot):
    """하위 모듈 안 미추적 파일도 커밋 안 한 변경이다 — force 여도 거부하고 파일을 지우지 않는다."""
    wp, _bare, slot_dir = submodule_slot
    untracked = slot_dir / "sub" / "new.txt"
    untracked.write_text("wip\n", encoding="utf-8")
    for force in (False, True):
        with pytest.raises(wp.RemoveRefused) as exc:
            wp.remove_slot("work/A_1", force=force)
        assert exc.value.reason == "dirty"
    assert untracked.read_text(encoding="utf-8") == "wip\n"
    assert wp.list_leases()[0].slot == "work/A_1"


@_git_required
def test_real_git_remove_slot_preserves_unmerged_branch_and_deletes_it_with_branch_ack(proj, tmp_path):
    """슬롯 전용 브랜치에 부모 밖 커밋 — 승인값 없으면 보존(슬롯만 제거), 브랜치 승인값이 있으면 삭제."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    bare = _mk_real_bare(wp, "A", tmp_path)
    wp.create_slot("A", base="main", init_submodules=False)   # A_1
    wp.create_slot("A", base="main", init_submodules=False)   # A_2
    for slot_name in ("work/A_1", "work/A_2"):
        slot_dir = wp.slot_path(slot_name)
        (slot_dir / "wip.txt").write_text("wip\n", encoding="utf-8")
        _git(slot_dir, "add", "wip.txt")
        _git(slot_dir, "commit", "-q", "-m", "unmerged work")

    kept = wp.remove_slot("work/A_1")
    assert kept.branch_deletion.reason == wp.BRANCH_REASON_UNMERGED
    assert not wp.slot_path("work/A_1").is_dir(), "worktree 는 제거돼야 한다(브랜치만 보존)"
    assert _branch_exists(bare, "A_1"), "합쳐지지 않은 브랜치가 승인 없이 삭제됨(작업 유실)"

    gone = wp.remove_slot("work/A_2", acked_branches=frozenset({"work/A_2", "A_2"}))
    assert (gone.branch_deletion.action, gone.branch_deletion.reason) == (
        wp.BRANCH_DELETED, wp.BRANCH_REASON_ACKED)
    assert not _branch_exists(bare, "A_2")


@_git_required
def test_real_git_remove_slot_with_readonly_leftovers_succeeds(proj, tmp_path):
    """읽기 전용 잔재가 있는 깨끗한 슬롯 — git 이 등록을 지운 뒤 트리 삭제에서 실패해도 잔재를 마저 지운다."""
    init_git_repo(proj, commit="home")
    wp = _load_wp_bound(proj)
    origin = init_git_repo(tmp_path / "A-origin", commit="seed")
    bare = wp.bare_repo_path("A")
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))

    wp.create_slot("A", base="main", init_submodules=False)   # A_1
    slot_dir = wp.slot_path("work/A_1")
    locked = slot_dir / "sub"
    locked.mkdir()
    (locked / "file.txt").write_text("x\n", encoding="utf-8")
    _git(slot_dir, "add", "-A")
    _git(slot_dir, "commit", "-q", "-m", "readonly leftover")
    locked.chmod(0o555)

    try:
        result = wp.remove_slot("work/A_1")
    finally:
        if locked.exists():
            locked.chmod(0o755)     # 실패해도 임시 트리가 정리되게 권한을 되돌린다.

    assert result.tree_cleaned is True, "잔재 정리를 보고하지 않았다"
    assert not slot_dir.exists(), "디렉터리 잔재가 남았다"
    assert wp.list_leases() == [], "장부 엔트리가 남았다"
    assert str(slot_dir) not in _git(bare, "worktree", "list", "--porcelain").stdout


@_git_required
def test_real_git_base_recreate_after_preserved_branch_fails_loud(proj, tmp_path):
    """보존된 전용 브랜치가 남으면 같은 번호 base-경로 재생성이 SlotBranchExists 로 fail-loud(provisional 롤백)."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    bare = _mk_real_bare(wp, "A", tmp_path)

    wp.create_slot("A", base="main", init_submodules=False)
    slot_dir = wp.slot_path("work/A_1")
    (slot_dir / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(slot_dir, "add", "wip.txt")
    _git(slot_dir, "commit", "-q", "-m", "unmerged work")

    wp.release("work/A_1")
    result = wp.remove_slot("work/A_1")
    assert result.branch_deletion.reason == wp.BRANCH_REASON_UNMERGED
    assert _branch_exists(bare, "A_1"), "보존 브랜치 A_1 이 bare 에 없음"

    with pytest.raises(wp.SlotBranchExists) as exc:
        wp.create_slot("A", base="main", init_submodules=False)
    msg = str(exc.value)
    assert (exc.value.branch, exc.value.slot, exc.value.base) == ("A_1", "work/A_1", "main")
    assert "orphan" not in msg.lower(), f"orphan-worktree 오귀인 잔존: {msg!r}"
    assert "브랜치" in msg and "A_1" in msg
    assert all(l.slot != "work/A_1" for l in wp.list_leases()), "provisional lease 롤백 실패(장부 dangling)"
    assert not wp.slot_path("work/A_1").is_dir(), "실패한 재생성이 worktree 를 남김"


@_git_required
def test_real_git_branch_resume_preserved_unmerged_no_reset(proj, tmp_path):
    """보존 브랜치를 다른 슬롯에서 `switch` 로 재개 → 보존 커밋 잔존(리셋 없음·데이터-유실 클래스)."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    bare = _mk_real_bare(wp, "A", tmp_path)

    wp.create_slot("A", base="main", init_submodules=False)   # A_1 @ main
    wp.create_slot("A", base="main", init_submodules=False)   # A_2 @ main (재개할 슬롯)
    slot_dir = wp.slot_path("work/A_1")
    (slot_dir / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(slot_dir, "add", "wip.txt")
    _git(slot_dir, "commit", "-q", "-m", "unmerged work")
    preserved_sha = _sha(slot_dir, "HEAD")

    result = wp.remove_slot("work/A_1")
    assert result.branch_deletion.reason == wp.BRANCH_REASON_UNMERGED
    assert not slot_dir.is_dir()

    switched = wp.switch("work/A_2", "A_1", protected=["main"])
    assert switched.branch == "A_1"
    resumed_dir = wp.slot_path("work/A_2")
    assert _sha(resumed_dir, "HEAD") == preserved_sha, "재개 슬롯이 보존 커밋을 잃음(리셋 재발·데이터-유실)"
    assert _sha(bare, "refs/heads/A_1") == preserved_sha, "전환이 보존 브랜치 ref 를 옮겼다"
    assert (resumed_dir / "wip.txt").read_text(encoding="utf-8") == "wip\n", "미머지 작업 파일이 유실됨"
    assert wp.current_branch("work/A_2") == "A_1"


@_git_required
def test_real_git_branch_exists_detection_color_safe(proj, tmp_path):
    """ambient `color.branch=always` 서도 보존 브랜치 선-검출(SlotBranchExists)과 재개(`switch`)가 정확하다."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    bare = _mk_real_bare(wp, "A", tmp_path)
    _git(bare, "config", "color.branch", "always")
    _git(bare, "config", "color.ui", "always")

    wp.create_slot("A", base="main", init_submodules=False)
    wp.create_slot("A", base="main", init_submodules=False)
    slot_dir = wp.slot_path("work/A_1")
    (slot_dir / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(slot_dir, "add", "wip.txt")
    _git(slot_dir, "commit", "-q", "-m", "unmerged work")
    preserved_sha = _sha(slot_dir, "HEAD")
    assert wp.remove_slot("work/A_1").branch_deletion.reason == wp.BRANCH_REASON_UNMERGED

    with pytest.raises(wp.SlotBranchExists):
        wp.create_slot("A", base="main", init_submodules=False)
    assert all(l.slot != "work/A_1" for l in wp.list_leases()), "선-검출 실패 시 provisional 잔존"

    assert wp.switch("work/A_2", "A_1", protected=["main"]).branch == "A_1"
    assert _sha(wp.slot_path("work/A_2"), "HEAD") == preserved_sha, \
        "color.branch=always 오염에서 재개가 보존 커밋을 잃음(데이터-유실 재발)"
    assert wp.current_branch("work/A_2") == "A_1"


# ════════════════════════════════════════════════════════════════════════
# 슬롯 없는 보존 브랜치 기록 — 슬롯 제거가 쓰고 · 슬롯 체크아웃이 지우고 · 조회가 읽는다
# ════════════════════════════════════════════════════════════════════════

_PRESERVED_KEYS = ("pm-slot", "pm-preserved-at", "pm-preserved-reason")
# 보존 판정 8개 = 기록하는 6개 + 슬롯 수명과 무관해 기록하지 않는 2개(보호·기본 브랜치).
_RECORDED_REASONS = ("children", "no-parent", "parent-gone", "unmerged", "tip-moved", "delete-refused")
_UNRECORDED_REASONS = ("protected", "default-branch")
_NO_PRESERVED_RECORD = dict.fromkeys(_PRESERVED_KEYS)


def _preserved_record(bare, branch):
    """bare 공용 설정의 보존 기록 세 키 — 없는 키는 None."""
    record = {}
    for key in _PRESERVED_KEYS:
        done = _git(bare, "config", "--get", f"branch.{branch}.{key}", check=False)
        record[key] = done.stdout.strip() if done.returncode == 0 else None
    return record


def _assert_recent_utc(stamp, before):
    """보존 시각은 UTC ISO 이고 기록 직전 시각과 지금 사이다."""
    from datetime import datetime, timezone
    at = datetime.fromisoformat(stamp)
    assert at.utcoffset() is not None and at.utcoffset().total_seconds() == 0, stamp
    assert before <= at <= datetime.now(timezone.utc), stamp


def _commit_in(slot_dir, name):
    (slot_dir / name).write_text(f"{name}\n", encoding="utf-8")
    _git(slot_dir, "add", name)
    _git(slot_dir, "commit", "-q", "-m", name)


@pytest.fixture
def pool(proj, tmp_path, monkeypatch):
    """실 bare + 슬롯 A_1·A_2(base main) — A_1 에 합쳐지지 않은 커밋 1개. (wp, bare)."""
    _init_repo(proj)
    wp = _load_wp_bound(proj)
    monkeypatch.setattr(wp, "_resolve_protected", lambda repo: ["main"])
    bare = _mk_real_bare(wp, "A", tmp_path)
    wp.create_slot("A", base="main", init_submodules=False)   # A_1
    wp.create_slot("A", base="main", init_submodules=False)   # A_2
    _commit_in(wp.slot_path("work/A_1"), "wip.txt")
    return wp, bare


def test_preserved_reason_list_covers_every_non_delete_judgement(proj):
    """사유 표 둘의 합 = 판정 사유 전부에서 삭제 근거(merged·acked)를 뺀 것 — 보존 사유가 늘면 이 표부터 red."""
    wp = _load_wp_bound(proj)
    reasons = {value for name, value in vars(wp).items() if name.startswith("BRANCH_REASON_")}
    assert set(_RECORDED_REASONS) | set(_UNRECORDED_REASONS) == (
        reasons - {wp.BRANCH_REASON_MERGED, wp.BRANCH_REASON_ACKED})
    assert set(_UNRECORDED_REASONS) == wp._UNRECORDED_PRESERVED_REASONS


@_git_required
def test_real_git_remove_slot_records_slot_time_and_reason_of_preserved_branches(pool):
    """실 판정 둘(합쳐지지 않음·부모 기록 없음) — 보존된 브랜치에 쓰던 슬롯·UTC 보존 시각·사유가 남는다."""
    from datetime import datetime, timezone
    wp, bare = pool
    for key in ("pm-parent", "pm-fork"):
        _git(bare, "config", "--unset", f"branch.A_2.{key}")
    before = datetime.now(timezone.utc).replace(microsecond=0)

    kept = wp.remove_slot("work/A_1")
    loose = wp.remove_slot("work/A_2")

    for result, slot_name, reason in ((kept, "work/A_1", wp.BRANCH_REASON_UNMERGED),
                                      (loose, "work/A_2", wp.BRANCH_REASON_NO_PARENT)):
        assert result.branch_deletion.reason == reason
        record = _preserved_record(bare, result.branch)
        assert (record["pm-slot"], record["pm-preserved-reason"]) == (slot_name, reason)
        _assert_recent_utc(record["pm-preserved-at"], before)


@_git_required
@pytest.mark.parametrize("reason", _RECORDED_REASONS)
def test_real_git_remove_slot_records_every_preserved_reason(pool, monkeypatch, reason):
    """기록하는 보존 사유마다 세 키가 남는다 — 판정 결과를 사유별로 고정하고 기록은 실 git 설정에 쓴다."""
    wp, bare = pool
    monkeypatch.setattr(wp, "delete_branch_by_rule", lambda branch, **_kwargs: wp.BranchDeletion(
        branch, action=wp.BRANCH_PRESERVED, reason=reason))

    result = wp.remove_slot("work/A_2")

    record = _preserved_record(bare, "A_2")
    assert result.branch_deletion.reason == reason
    assert (record["pm-slot"], record["pm-preserved-reason"]) == ("work/A_2", reason)
    assert record["pm-preserved-at"] is not None
    assert _branch_exists(bare, "A_2")


@_git_required
@pytest.mark.parametrize("reason", _UNRECORDED_REASONS)
def test_real_git_remove_slot_does_not_record_protected_or_default_branch(pool, monkeypatch, reason):
    """보호·기본 브랜치 보존은 기록하지 않는다 — 슬롯 수명과 무관한 브랜치라 조회 목록에도 오르지 않는다."""
    wp, bare = pool
    monkeypatch.setattr(wp, "delete_branch_by_rule", lambda branch, **_kwargs: wp.BranchDeletion(
        branch, action=wp.BRANCH_PRESERVED, reason=reason))

    result = wp.remove_slot("work/A_2")

    assert (result.branch_deletion.action, result.branch_deletion.reason) == (wp.BRANCH_PRESERVED, reason)
    assert _preserved_record(bare, "A_2") == _NO_PRESERVED_RECORD
    assert _branch_exists(bare, "A_2")
    assert wp.preserved_branches() == []


@_git_required
def test_real_git_deleted_branch_has_no_preserved_record(pool):
    """삭제 판정이면 기록을 쓰지 않고, 보존됐던 브랜치를 나중에 지우면 git 이 기록도 함께 지운다."""
    wp, bare = pool
    assert wp.remove_slot("work/A_2").branch_deletion.action == wp.BRANCH_DELETED
    assert _preserved_record(bare, "A_2") == _NO_PRESERVED_RECORD

    assert wp.remove_slot("work/A_1").branch_deletion.action == wp.BRANCH_PRESERVED
    assert _preserved_record(bare, "A_1")["pm-slot"] == "work/A_1"
    later = wp.delete_branch_by_rule("A_1", repo="A", acked_branches=frozenset({"A_1"}),
                                     git_runner=wp._real_git_runner(bare))
    assert later.action == wp.BRANCH_DELETED
    assert _preserved_record(bare, "A_1") == _NO_PRESERVED_RECORD
    assert wp.preserved_branches() == []


@_git_required
def test_real_git_switch_back_into_a_slot_clears_the_preserved_record(pool):
    """보존 브랜치를 다른 슬롯에서 `switch` 로 다시 쓰면 보존 기록 세 키가 지워지고 부모 기록은 남는다."""
    wp, bare = pool
    wp.remove_slot("work/A_1")
    assert [row["name"] for row in wp.preserved_branches()] == ["A_1"]

    wp.switch("work/A_2", "A_1", protected=["main"])

    assert _preserved_record(bare, "A_1") == _NO_PRESERVED_RECORD
    assert wp.read_branch_parent("A_1", git_runner=wp._real_git_runner(bare))["name"] == "main"
    assert wp.preserved_branches() == []


@_git_required
def test_real_git_alloc_checkout_of_a_preserved_branch_clears_the_record(pool):
    """`alloc(branch=)` 가 보존 브랜치를 idle 슬롯에 체크아웃해도 같은 체크아웃 프리미티브라 기록이 지워진다."""
    wp, bare = pool
    wp.remove_slot("work/A_1")

    lease = wp.alloc("A", branch="A_1", owner_task="job")

    assert lease.slot == "work/A_2" and wp.current_branch("work/A_2") == "A_1"
    assert _preserved_record(bare, "A_1") == _NO_PRESERVED_RECORD


def test_remove_slot_preserved_record_write_failure_raises_and_keeps_ledger_row(wp):
    """보존 기록 쓰기 rc≠0 → BranchParentError(기록 없이 넘어가지 않는다) · 장부 행은 정지 지점에 남는다."""
    _seed(wp, _lease(wp, slot="work/A_1", repo="A", state="idle"))
    git = RemoveFakeGit(wp, head="A_1", parents={"A_1": "develop"}, merged=False)

    def runner(argv):
        if argv[:2] == ["config", "branch.A_1.pm-slot"]:
            return (255, "error: could not lock config file\n")
        return git(argv)

    with pytest.raises(wp.BranchParentError, match="보존 기록 쓰기 실패"):
        wp.remove_slot("work/A_1", git_runner=runner)
    assert wp.list_leases()[0].slot == "work/A_1"


def test_checkout_clears_preserved_keys_and_raises_on_other_failures(wp):
    """체크아웃 뒤 도착 브랜치의 세 키를 지운다 — 없는 키(rc 5)는 통과, 그 밖의 rc≠0 은 BranchParentError."""
    git = RemoveFakeGit(wp, head="A_1")
    unset: list[str] = []

    def missing_keys(argv):
        if argv[:2] == ["config", "--unset"]:
            unset.append(argv[2])
            return (5, "")
        return git(argv)

    wp._checkout_required("work/A_1", "A_1", git_runner=missing_keys)
    assert unset == ["branch.A_1.pm-slot", "branch.A_1.pm-preserved-at", "branch.A_1.pm-preserved-reason"]

    def locked(argv):
        if argv[:2] == ["config", "--unset"]:
            return (255, "error: could not lock config file\n")
        return git(argv)

    with pytest.raises(wp.BranchParentError, match="보존 기록 지우기 실패"):
        wp._checkout_required("work/A_1", "A_1", git_runner=locked)


@_git_required
def test_real_git_preserved_branches_rows_parent_and_unmerged_count(family):
    """조회 행 — 부모에 합쳐지지 않은 커밋 수 · 부모 기록 없음·부모 브랜치 없음은 null · 이름 순."""
    wp, bare, _slot_dir, runner = family
    tip = _side_branch(wp, bare, runner, "feat", commit_message="one")
    _git(bare, "update-ref", "refs/heads/feat", _new_commit(bare, tip, "two"))
    _git(bare, "branch", "loose", "main")
    _git(bare, "branch", "orphaned", "main")
    wp.record_branch_parent("orphaned", "gone-parent", _sha(bare, "main"), git_runner=runner)
    for name, slot_name, reason in (("orphaned", "work/A_9", "parent-gone"),
                                    ("feat", "work/A_7", "unmerged"),
                                    ("loose", "work/A_8", "no-parent")):
        wp.record_preserved_branch(name, slot=slot_name, reason=reason, git_runner=runner)

    rows = wp.preserved_branches()

    assert [(row["name"], row["slot"], row["reason"], row["parent"], row["unmerged"]) for row in rows] == [
        ("feat", "work/A_7", "unmerged", "main", 2),
        ("loose", "work/A_8", "no-parent", None, None),
        ("orphaned", "work/A_9", "parent-gone", "gone-parent", None),
    ]
    assert all(set(row) == {"repo", "name", "slot", "preserved_at", "reason", "parent", "unmerged"}
               and row["repo"] == "A" for row in rows)


@_git_required
def test_real_git_preserved_branches_damaged_record_raises(family):
    """세 키 중 일부만 있거나 기록된 브랜치가 없으면 기록 손상으로 raise — 빈 행으로 접지 않는다."""
    wp, bare, _slot_dir, runner = family
    _git(bare, "branch", "half", "main")
    _git(bare, "config", "branch.half.pm-slot", "work/A_3")
    with pytest.raises(wp.BranchParentError, match="보존 기록 손상"):
        wp.preserved_branches()

    _git(bare, "config", "--unset", "branch.half.pm-slot")
    wp.record_preserved_branch("ghost", slot="work/A_4", reason="unmerged", git_runner=runner)
    with pytest.raises(wp.BranchParentError, match="'ghost' 가 없다"):
        wp.preserved_branches()


# ════════════════════════════════════════════════════════════════════════
# create_slot 보존 브랜치 충돌 — 선-검출 fail-loud (T-0335·DI seam·hermetic)
# ════════════════════════════════════════════════════════════════════════


class _CreateBaseFakeGit:
    """create_slot 작업 슬롯 경로 DI seam mock — bare 검증·branch --list(전용 브랜치 잔존)·fetch/show-ref·
    갈라진 커밋 해소(`rev-parse --verify <ref>^{commit}`)·worktree add.

    `slot_branch_exists` → `_slot_branch_exists` 의 `git branch --list --format=%(refname:short)
    <branch>` 가 그 브랜치를 리스트(잔존·T-0335 선-검출 트리거)한다. False 면 빈 출력(미존재·정상
    생성). 존재 시 브랜치명만 실은 평문 라인(`A_1\\n`·프리픽스/ANSI 없음)을 돌려준다.
    """

    def __init__(self, *, slot_branch_exists=False):
        self.slot_branch_exists = slot_branch_exists
        self.calls: list[list] = []

    def __call__(self, argv: list) -> tuple[int, str]:
        self.calls.append(list(argv))
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return (0, "true\n")
        if "rev-parse" in argv and "--verify" in argv and argv[-1] == "HEAD":
            return (0, "0123abc\n")
        if argv[:2] == ["rev-parse", "--verify"] and argv[-1].endswith("^{commit}"):
            return (0, "0123abc\n")
        if argv[:2] == ["branch", "--list"]:
            name = argv[-1]
            return (0, f"{name}\n") if self.slot_branch_exists else (0, "")
        if argv[:2] == ["fetch", "origin"]:
            return (0, "")
        if argv[:3] == ["show-ref", "--verify", "--quiet"]:
            return (0, "")
        if argv == ["symbolic-ref", "HEAD"]:
            return (0, "refs/heads/A_1\n")
        return (0, "")

    def did(self, *prefix) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


def _mk_bare_dir(wp, repo):
    """bare 부재 가드(`bare.exists()`) 통과용 placeholder — is-bare 유효성은 injected mock 이 모델."""
    wp.bare_repo_path(repo).mkdir(parents=True, exist_ok=True)


def test_create_slot_base_preexisting_branch_raises_slot_branch_exists(wp):
    """create_slot(base=) 이 파려는 슬롯 전용 브랜치가 이미 존재하면 SlotBranchExists (선-검출·fail-loud)."""
    _mk_bare_dir(wp, "A")
    git = _CreateBaseFakeGit(slot_branch_exists=True)
    with pytest.raises(wp.SlotBranchExists) as exc:
        wp.create_slot("A", base="develop", init_submodules=False, git_runner=git)
    assert (exc.value.branch, exc.value.base, exc.value.slot) == ("A_1", "develop", "work/A_1")
    assert git.did("branch", "--list", "--format=%(refname:short)", "A_1")
    assert not git.did("worktree", "add")
    assert "orphan" not in str(exc.value).lower()
    assert all(l.slot != "work/A_1" for l in wp.list_leases())


def test_create_slot_base_no_preexisting_branch_proceeds(wp):
    """대조 — 전용 브랜치 미존재면 선-검출 통과 → 정상 worktree add + 부모(base) 기록."""
    _mk_bare_dir(wp, "A")
    git = _CreateBaseFakeGit(slot_branch_exists=False)
    lease = wp.create_slot("A", base="develop", init_submodules=False, git_runner=git)
    assert lease.slot == "work/A_1"
    assert git.did("branch", "--list", "--format=%(refname:short)", "A_1")
    assert git.did("worktree", "add", "--no-track", "-b", "A_1")
    assert git.did("config", "branch.A_1.pm-parent", "develop")


def test_create_slot_post_hoc_branch_exists_not_misattributed_orphan(wp):
    """worktree add 가 'a branch named ... already exists' 로 죽으면 브랜치 진단(orphan 오귀인 아님)."""
    _mk_bare_dir(wp, "A")

    class _G(_CreateBaseFakeGit):
        def __call__(self, argv):
            if argv[:2] == ["worktree", "add"]:
                self.calls.append(list(argv))
                return (128, "fatal: a branch named 'A_1' already exists\n")
            return super().__call__(argv)

    git = _G(slot_branch_exists=False)
    with pytest.raises(RuntimeError) as exc:
        wp.create_slot("A", base="develop", init_submodules=False, git_runner=git)
    msg = str(exc.value)
    assert "A_1" in msg and "브랜치" in msg
    assert "orphan" not in msg.lower()


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


@pytest.fixture(scope="module")
def engine():
    """출력 테스트가 실 `BranchDeletion`(문구 단일 소스)을 쓰도록 로드한 엔진 — 경로 전역은 쓰지 않는다."""
    spec = importlib.util.spec_from_file_location("wp_rm_engine", TOOLS / "worktree_pool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRemoveResult:
    def __init__(self, slot, *, branch_deletion=None, forced_state=None, tree_cleaned=False):
        self.slot = slot
        self.branch = branch_deletion.branch if branch_deletion is not None else None
        self.branch_deletion = branch_deletion
        self.tree_cleaned = tree_cleaned
        self.forced_state = forced_state


class FakeWP:
    """worktree_pool 대역 — remove_slot 호출을 기록하고 결과/예외를 결정적으로 낸다(DI seam)."""

    class RemoveRefused(Exception):
        def __init__(self, slot, reason, *, state=None, path=None):
            self.slot = slot
            self.reason = reason
            self.state = state
            self.path = path
            super().__init__(f"{slot} {reason}")

    def __init__(self, engine, *, result="ok", raises=None, branch_deletion=None,
                 forced_state=None, tree_cleaned=False):
        self.BRANCH_PRESERVED = engine.BRANCH_PRESERVED
        self.calls: list[tuple] = []
        self._result = result
        self._raises = raises
        self._branch_deletion = branch_deletion
        self._forced_state = forced_state
        self._tree_cleaned = tree_cleaned

    def remove_slot(self, slot, *, force=False, acked_branches=frozenset()):
        self.calls.append(("remove_slot", slot, force, acked_branches))
        if self._raises is not None:
            raise self._raises
        if self._result is None:
            return None
        return FakeRemoveResult(slot, branch_deletion=self._branch_deletion,
                                forced_state=self._forced_state, tree_cleaned=self._tree_cleaned)


def _ns(slot="work/A_1", *, force=False, user_ack=None):
    return argparse.Namespace(slot=slot, force=force,
                              user_ack=[slot] if user_ack is None else user_ack)


def _deletion(engine, branch="A_1", **fields):
    return engine.BranchDeletion(branch, **fields)


def test_cmd_remove_without_user_ack_refuses_before_engine(pc, engine, capsys):
    """승인값 없음 → 엔진을 부르지 않고 rc1 + 사용자 승인 요청 처방(세션 자동 부착 금지)."""
    wp = FakeWP(engine)
    rc = pc.cmd_worktree_remove(_ns(user_ack=[]), worktree_pool=wp)
    assert rc == 1
    assert wp.calls == []
    err = capsys.readouterr().err
    assert "사용자 명시 승인" in err and "--user-ack work/A_1" in err


def test_cmd_remove_with_other_user_ack_refuses_before_engine(pc, engine, capsys):
    """승인값이 슬롯과 다름(브랜치 이름만 등) → 엔진을 부르지 않고 rc1 + 제공된 값 표시."""
    wp = FakeWP(engine)
    rc = pc.cmd_worktree_remove(_ns(user_ack=["A_1", "work/A_2"]), worktree_pool=wp)
    assert rc == 1
    assert wp.calls == []
    err = capsys.readouterr().err
    assert "'work/A_2'" in err and "슬롯 'work/A_1'이 없다" in err


def test_cmd_remove_with_slot_user_ack_calls_engine_with_all_ack_values(pc, engine, capsys):
    """승인값에 슬롯이 있으면 remove_slot(slot, force=, acked_branches=값 전부) 를 부른다."""
    wp = FakeWP(engine)
    rc = pc.cmd_worktree_remove(_ns(user_ack=["work/A_1", "A_1"]), worktree_pool=wp)
    assert rc == 0
    assert wp.calls == [("remove_slot", "work/A_1", False, frozenset({"work/A_1", "A_1"}))]
    assert "[승인 감사] worktree remove" in capsys.readouterr().out


def test_cmd_remove_force_passes_force_true(pc, engine):
    """`--force` → remove_slot(slot, force=True)."""
    wp = FakeWP(engine)
    assert pc.cmd_worktree_remove(_ns(force=True), worktree_pool=wp) == 0
    assert wp.calls[0][2] is True


def test_cmd_remove_dirty_refusal_says_no_stash_and_force_does_not_pass(pc, engine, capsys):
    """RemoveRefused("dirty") → rc1 · 엔진은 stash 하지 않음 · `--force` 도 넘지 않음을 알린다."""
    wp = FakeWP(engine, raises=FakeWP.RemoveRefused("work/A_1", "dirty"))
    assert pc.cmd_worktree_remove(_ns(force=True), worktree_pool=wp) == 1
    err = capsys.readouterr().err
    assert "커밋 안 한 변경" in err and "stash 하지" in err
    assert "`--force` 는 이 거부를 넘지 않는다" in err


def test_cmd_remove_active_lease_refusal_points_to_release_or_force(pc, engine, capsys):
    """RemoveRefused("active-lease") → rc1 + 사용 중·release 먼저·--force(점유 무시) 안내."""
    wp = FakeWP(engine, raises=FakeWP.RemoveRefused("work/A_1", "active-lease", state="leased"))
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 1
    err = capsys.readouterr().err
    assert "사용 중" in err and "release" in err and "점유 무시" in err


def test_cmd_remove_unregistered_tree_refusal_hands_path_to_user(pc, engine, capsys):
    """RemoveRefused("unregistered-tree") → rc1 + 그 경로 + 직접 확인·삭제 뒤 prune-stale 안내."""
    path = Path("/w/work/A_1")
    wp = FakeWP(engine, raises=FakeWP.RemoveRefused("work/A_1", "unregistered-tree", path=path))
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 1
    err = capsys.readouterr().err
    assert str(path) in err and "직접 확인·삭제" in err and "prune-stale" in err


def test_cmd_remove_absent_ledger_harmless_rc0(pc, engine, capsys):
    """remove_slot 이 None(장부 없음) → 무해 종료 rc0 + orphan 안내."""
    wp = FakeWP(engine, result=None)
    assert pc.cmd_worktree_remove(_ns("work/gone_9"), worktree_pool=wp) == 0
    out = capsys.readouterr().out
    assert "이미 정리됨" in out and "git worktree remove" in out


def test_cmd_remove_engine_runtime_error_rc1_without_force_advice(pc, engine, capsys):
    """엔진 RuntimeError(worktree remove 실패·판정 조회 실패) → rc1 + 관측만(도달 불가한 --force 재시도 권하지 않음)."""
    wp = FakeWP(engine, raises=RuntimeError(
        "git worktree remove failed for 'work/A_1' (rc=255) — git 등록·장부·브랜치가 그대로다."))
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 1
    err = capsys.readouterr().err
    assert "제거 실패" in err and "--force" not in err


def test_cmd_remove_merged_branch_deleted_line(pc, engine, capsys):
    """삭제(합쳐짐) — 부모 비교 ref 를 밝힌 한 줄 · 보존 캐비앗 없음."""
    deletion = _deletion(engine, action=engine.BRANCH_DELETED, reason=engine.BRANCH_REASON_MERGED,
                         parent="main", parent_ref="origin/main")
    wp = FakeWP(engine, branch_deletion=deletion)
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 0
    out = capsys.readouterr().out
    assert "브랜치 A_1 삭제 (부모 origin/main 에 다 합쳐짐)" in out
    assert "SlotBranchExists" not in out


def test_cmd_remove_unmerged_branch_preserved_lists_commits_origin_and_caveat(pc, engine, capsys):
    """보존(합쳐지지 않음) — 커밋 수·origin 반영 여부 한 줄 + 커밋 줄 + 재생성 캐비앗(switch 안내)."""
    deletion = _deletion(engine, action=engine.BRANCH_PRESERVED, reason=engine.BRANCH_REASON_UNMERGED,
                         parent="main", parent_ref="main", origin=engine.ORIGIN_MISSING_TIP,
                         unmerged_commits=["abc1234 second", "def5678 first"])
    wp = FakeWP(engine, branch_deletion=deletion)
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 0
    out = capsys.readouterr().out
    assert "브랜치 A_1 보존 (부모 main 에 합쳐지지 않은 커밋 2개 · origin 브랜치에 끝 커밋 없음)" in out
    assert "    abc1234 second\n    def5678 first\n" in out
    assert "SlotBranchExists" in out and "worktree_pool.py switch <슬롯> A_1" in out


def test_cmd_remove_unmerged_commit_list_folds_over_limit(pc, engine, capsys):
    """커밋 줄은 상한까지만 싣고 나머지는 '외 N개' 로 접는다."""
    shown = pc._UNMERGED_COMMITS_SHOWN
    commits = [f"{index:07x} c{index}" for index in range(shown + 2)]
    deletion = _deletion(engine, action=engine.BRANCH_PRESERVED, reason=engine.BRANCH_REASON_UNMERGED,
                         parent="main", parent_ref="main", origin=engine.ORIGIN_NO_BRANCH,
                         unmerged_commits=commits)
    wp = FakeWP(engine, branch_deletion=deletion)
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 0
    out = capsys.readouterr().out
    assert commits[shown - 1] in out and commits[shown] not in out
    assert "… 외 2개" in out


@pytest.mark.parametrize(("fields", "expected"), [
    ({"action": "deleted", "reason": "acked", "unmerged_commits": ["a1 x"]},
     "브랜치 A_1 삭제 (합쳐지지 않은 커밋 1개 · 사용자 승인값)"),
    ({"action": "preserved", "reason": "protected"}, "브랜치 A_1 보존 (보호 브랜치)"),
    ({"action": "preserved", "reason": "default-branch"}, "브랜치 A_1 보존 (공용 저장소 기본 브랜치)"),
    ({"action": "preserved", "reason": "children", "children": ["c1", "c2"]},
     "브랜치 A_1 보존 (자식 브랜치 c1, c2)"),
    ({"action": "preserved", "reason": "no-parent"}, "브랜치 A_1 보존 (부모 기록 없음 · 합쳐짐 판정 불가)"),
    ({"action": "preserved", "reason": "parent-gone", "parent": "task/x"},
     "브랜치 A_1 보존 (부모 브랜치 task/x 없음 · 합쳐짐 판정 불가)"),
    ({"action": "preserved", "reason": "tip-moved", "detail": "판정 때 aaa → 지금 bbb"},
     "브랜치 A_1 보존 (판정 뒤 끝 커밋이 바뀜 · 판정 때 aaa → 지금 bbb)"),
    ({"action": "preserved", "reason": "delete-refused", "detail": "rc=1: error: used by worktree"},
     "브랜치 A_1 보존 (git branch -D 거부 · rc=1: error: used by worktree)"),
])
def test_cmd_remove_branch_line_per_reason(pc, engine, capsys, fields, expected):
    """판정 사유마다 출력 한 줄 — 값 문자열이 엔진 상수와 같다는 것도 함께 고정한다."""
    assert fields["action"] in (engine.BRANCH_DELETED, engine.BRANCH_PRESERVED)
    assert fields["reason"] in {value for name, value in vars(engine).items()
                                if name.startswith("BRANCH_REASON_")}
    wp = FakeWP(engine, branch_deletion=_deletion(engine, **fields))
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 0
    assert expected in capsys.readouterr().out


def test_cmd_remove_without_branch_prints_no_branch_line(pc, engine, capsys):
    """브랜치를 판별하지 못한 제거(detached·트리 부재) — 브랜치 줄 없음·stash 줄 없음."""
    wp = FakeWP(engine, branch_deletion=None)
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 0
    out = capsys.readouterr().out
    assert "제거" in out and "브랜치" not in out and "stash" not in out


def test_cmd_remove_tree_cleaned_is_announced(pc, engine, capsys):
    """git 이 등록만 지우고 남긴 디렉터리를 엔진이 마저 지웠으면 그 사실을 1줄로 고지한다."""
    wp = FakeWP(engine, tree_cleaned=True)
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 0
    assert "디렉터리 잔재" in capsys.readouterr().out


def test_cmd_remove_forced_state_warns_on_stderr(pc, engine, capsys):
    """forced_state 있으면 '⚠ 활성 강제 회수' 를 stderr 로 · 없으면 경고 없음."""
    wp = FakeWP(engine, forced_state="leased")
    assert pc.cmd_worktree_remove(_ns(force=True), worktree_pool=wp) == 0
    err = capsys.readouterr().err
    assert "⚠" in err and "강제 회수" in err and "leased" in err

    wp = FakeWP(engine)
    assert pc.cmd_worktree_remove(_ns(), worktree_pool=wp) == 0
    assert "강제 회수" not in capsys.readouterr().err


def test_cmd_remove_engine_missing_rc1(pc, monkeypatch, capsys):
    """승인값은 있는데 엔진 로드 실패 → rc1 + 안내(다른 서브커맨드 동형·크래시 0)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    assert pc.cmd_worktree_remove(_ns()) == 1
    assert "worktree_pool.py 엔진을 찾을 수 없다" in capsys.readouterr().err


def test_main_routes_worktree_remove_with_user_ack_and_force(pc, engine, monkeypatch):
    """main 파싱 e2e — `--user-ack` 반복 값이 모여 엔진에 가고, `--force` 가 전달된다."""
    wp = FakeWP(engine)
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    assert pc.main(["worktree", "remove", "work/A_1", "--user-ack", "work/A_1",
                    "--user-ack", "A_1"]) == 0
    assert pc.main(["worktree", "remove", "work/A_1", "--user-ack", "work/A_1", "--force"]) == 0
    assert wp.calls == [
        ("remove_slot", "work/A_1", False, frozenset({"work/A_1", "A_1"})),
        ("remove_slot", "work/A_1", True, frozenset({"work/A_1"})),
    ]


def test_main_worktree_remove_without_user_ack_rc1(pc, engine, monkeypatch):
    """main 파싱 e2e — `--user-ack` 없이 부르면 엔진 호출 0·rc1."""
    wp = FakeWP(engine)
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    assert pc.main(["worktree", "remove", "work/A_1"]) == 1
    assert wp.calls == []


def test_build_parser_worktree_remove_parses_slot_user_ack_and_force(pc):
    """`worktree remove <slot> --user-ack ... [--force]` 파싱 — user_ack 는 목록(반복)·기본 빈 목록."""
    parser = pc.build_parser()
    args = parser.parse_args(["worktree", "remove", "work/A_1", "--user-ack", "work/A_1",
                              "--user-ack", "A_1", "--force"])
    assert (args.slot, args.user_ack, args.force) == ("work/A_1", ["work/A_1", "A_1"], True)
    assert args.func is pc.cmd_worktree_remove
    bare_args = parser.parse_args(["worktree", "remove", "work/A_1"])
    assert (bare_args.user_ack, bare_args.force) == ([], False)


def test_worktree_help_surfaces_remove_ack_and_distinguishes_prune(pc, capsys):
    """`worktree --help` 이 remove(승인값·합쳐지지 않은 브랜치 보존 캐비앗) + prune-stale 역할 구분을 surface."""
    parser = pc.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["worktree", "--help"])
    out = capsys.readouterr().out
    assert "remove" in out and "prune-stale" in out
    assert "승인값" in out and "합쳐지지 않은" in out
