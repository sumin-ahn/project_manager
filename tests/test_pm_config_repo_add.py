"""pm-config repo add — bare clone fetch refspec 보정 단위/배선 테스트 (T-0152).

`git clone --bare` 는 일반 clone 과 달리 `remote.origin.fetch` 를 설정하지 않아 bare 에
origin/* remote-tracking ref(origin/main 등)가 안 생긴다 → 그 bare 를 공유하는 worktree
슬롯이 핸드오프 라이브-게이트(T-0151)의 baseline(origin/main)을 해소 못 한다. `cmd_repo_add`
가 bare clone 성공 직후(+기존 bare 재사용 경로) refspec 설정 + `fetch origin` 으로 근절하는지
검증한다 — clone_runner **mock 주입**으로 hermetic(실 clone/네트워크 0·test_pm_config_facade
의 DI seam 동류). 커버:

  - refspec config 호출 단언 — `config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'`.
  - fetch 호출 단언 — `fetch origin` (refspec 성공 시).
  - clone 실패 시 refspec/fetch skip — clone rc≠0 면 보정 git 호출 0(early return).
  - 멱등 — 재실행(기존 bare)에서도 refspec 보정 1회 수행(refspec-없는 과거 bare 복구).
  - 기존 bare 재사용 경로 — clone skip 이어도 refspec 보정은 수행.
  - fail-soft — refspec set 실패·fetch 실패는 경고 surface 하되 repo add rc 0(등록 진행).
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

REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pc():
    return _load_pm_config()


# ── 주입형 fake (DI seam — hermetic·test_pm_config_facade 동형 최소 대역) ───────


class FakeBoard:
    """board 대역 — areas_append 기록·registered_repos 제어 (refspec 테스트에 필요한 면만).

    `registered` = 이미 등록된 **repo명** 집합(멱등 재등록 판별 축·ADR-0042 후 repo 칼럼 기준).
    """

    def __init__(self, *, registered=()):
        self._registered = set(registered)
        self.append_calls: list = []

    def registered_repos(self):
        return set(self._registered)

    def registered_prefixes(self):     # 자동시드 폐지 후 cmd_repo_add 는 미사용 — 하위 배선용 유지.
        return set(self._registered)

    def areas_append(self, prefix, area, owner, *, repo=None, git=None,
                     test_cmd=None, base=None, protected=None, area_owner=None):
        self.append_calls.append({"prefix": prefix, "repo": repo, "git": git,
                                  "base": base, "area_owner": area_owner})

    def _repo_protected(self, repo):
        return ["main", "master", "develop"]


class GitFake:
    """git runner 대역 — argv 를 기록하고 argv 모양에 따라 (rc, out) 을 돌려준다.

    clone·config(refspec)·fetch·symbolic-ref 를 구분해 base 해소(T-0075)와 refspec 보정
    (T-0152)이 같은 주입 runner 를 `-C <bare>` 로 재사용하는 경로를 결정적으로 친다.
    rc 오버라이드(`config_rc`/`fetch_rc`/`clone_rc`)로 실패 분기(fail-soft)를 모델링한다.
    """

    def __init__(self, *, clone_rc=0, config_rc=0, fetch_rc=0, head="main",
                 symref_rc=0, revparse_rc=0, branch_config_rc=0):
        self.calls: list[list] = []
        self._clone_rc = clone_rc
        self._config_rc = config_rc
        self._fetch_rc = fetch_rc
        self._head = head
        # 기본 브랜치 origin tracking(T-0273) 모델링 — symbolic-ref/rev-parse(HEAD 해소)와
        # config branch.<d>.remote/merge 의 rc 를 각각 오버라이드해 fail-soft 분기를 친다.
        self._symref_rc = symref_rc            # symbolic-ref --short HEAD rc
        self._revparse_rc = revparse_rc        # rev-parse --abbrev-ref HEAD rc (symref 실패 폴백)
        self._branch_config_rc = branch_config_rc   # config branch.<d>.remote/merge rc

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv and argv[0] == "clone":
            return self._clone_rc, ("" if self._clone_rc == 0 else "fatal: clone failed")
        if "config" in argv and "remote.origin.fetch" in argv:
            return self._config_rc, ("" if self._config_rc == 0 else "fatal: config failed")
        if "config" in argv and any(a.startswith("branch.") for a in argv):
            return self._branch_config_rc, ("" if self._branch_config_rc == 0 else "fatal: config failed")
        if "fetch" in argv:
            return self._fetch_rc, ("" if self._fetch_rc == 0 else "fatal: could not fetch")
        if "symbolic-ref" in argv:
            return self._symref_rc, (self._head + "\n" if self._symref_rc == 0
                                     else "fatal: ref HEAD is not a symbolic ref\n")
        if "rev-parse" in argv and "--abbrev-ref" in argv:
            return self._revparse_rc, (self._head + "\n" if self._revparse_rc == 0
                                       else "fatal: bad revision 'HEAD'\n")
        return 0, ""


def _args(name="svc", git="git@h:me/svc.git", test=None, owner="me", base=None):
    return argparse.Namespace(name=name, git=git, test=test, owner=owner, base=base)


def _config_call(gitr):
    for argv in gitr.calls:
        if "config" in argv and "remote.origin.fetch" in argv:
            return argv
    return None


def _fetch_call(gitr):
    for argv in gitr.calls:
        if "fetch" in argv and "origin" in argv:
            return argv
    return None


def _clone_call(gitr):
    for argv in gitr.calls:
        if argv and argv[0] == "clone":
            return argv
    return None


def _branch_config_calls(gitr):
    """bare 기본 브랜치 tracking config 호출(`config branch.<d>.remote/merge`)만 추린다 (T-0273)."""
    return [argv for argv in gitr.calls
            if "config" in argv and any(a.startswith("branch.") for a in argv)]


# ── refspec config + fetch 호출 단언 (신규 clone 경로) ────────────────────────


def test_fresh_clone_sets_refspec_and_fetches(pc, tmp_path):
    """신규 bare clone 성공 직후 → refspec config + fetch origin (T-0152·DoD 1).

    config 가 `-C <bare> config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'`
    형태로(덮어쓰기·멱등), fetch 가 `-C <bare> fetch origin` 형태로 같은 주입 runner 에 불린다.
    """
    board = FakeBoard(registered=())
    gitr = GitFake()
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    assert _clone_call(gitr) is not None       # 신규 clone 수행
    # refspec config — bare 컨텍스트(-C <bare>) + 정확한 refspec 값.
    cfg = _config_call(gitr)
    assert cfg is not None
    assert "-C" in cfg and str(repos / "svc.git") in cfg
    assert cfg[-1] == REFSPEC                   # 덮어쓰기 set (config 마지막 토큰=refspec 값)
    # fetch origin — refspec config 통과 후 호출(같은 bare 컨텍스트).
    fetch = _fetch_call(gitr)
    assert fetch is not None
    assert "-C" in fetch and str(repos / "svc.git") in fetch


def test_refspec_config_precedes_fetch(pc, tmp_path):
    """refspec config 가 fetch *보다 먼저* 불린다 (refspec 없으면 fetch 가 origin/* 못 채움)."""
    board = FakeBoard(registered=())
    gitr = GitFake()
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr,
                         repos_dir=tmp_path / ".repos")
    assert rc == 0
    config_idx = next(i for i, c in enumerate(gitr.calls)
                      if "config" in c and "remote.origin.fetch" in c)
    fetch_idx = next(i for i, c in enumerate(gitr.calls) if "fetch" in c)
    assert config_idx < fetch_idx


# ── 기존 bare 재사용 경로에서도 refspec 멱등 보정 (DoD 2) ─────────────────────


def test_existing_bare_reuse_still_corrects_refspec(pc, tmp_path):
    """기존 `.repos/<name>.git` 재사용(clone skip) 경로에서도 refspec 보정 + fetch (T-0152·DoD 2).

    refspec-없는 과거 bare(다음 채택자/슬롯)를 복구한다 — clone 은 안 하지만 보정은 멱등 수행.
    """
    board = FakeBoard(registered=())
    gitr = GitFake()
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)    # bare 가 이미 존재 → clone skip
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    assert _clone_call(gitr) is None           # clone 안 함(재사용)
    assert _config_call(gitr) is not None       # refspec 보정은 수행(멱등·과거 bare 복구)
    assert _fetch_call(gitr) is not None


def test_already_registered_bare_exists_still_corrects_refspec(pc, tmp_path):
    """이미 등록 + bare 존재(등록 no-op) 경로도 refspec 보정 — early return 전 수행 (T-0152).

    refspec 보정은 등록/clone 멱등 분기보다 *앞*(clone 블록 직후)이라, already_registered
    early-return 경로에서도 보정이 돈다(과거 bare 복구가 등록 상태와 무관해야 함).
    """
    board = FakeBoard(registered=("svc",))
    gitr = GitFake()
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    assert board.append_calls == []            # 등록 no-op
    assert _config_call(gitr) is not None        # refspec 보정은 그래도 수행
    assert _fetch_call(gitr) is not None


# ── 멱등 — 재실행 안전 (DoD 3) ───────────────────────────────────────────────


def test_refspec_correction_idempotent_on_rerun(pc, tmp_path):
    """재실행(기존 bare)에서도 refspec 보정이 깨지지 않고 1회 수행 — set=덮어쓰기 멱등 (T-0152).

    config set 은 덮어쓰기라 두 번째 실행도 무해(같은 값으로 재set). 호출 count 가 정확히 1.
    """
    board = FakeBoard(registered=())
    repos = tmp_path / ".repos"
    # 1차: 신규 clone → refspec 보정.
    pc.cmd_repo_add(_args(), board=board, clone_runner=GitFake(), repos_dir=repos)
    (repos / "svc.git").mkdir(parents=True, exist_ok=True)   # bare 존재 모사(1차 clone 결과)
    # 2차: 재실행(기존 bare) → 다시 refspec 보정(멱등).
    board2 = FakeBoard(registered=())
    gitr2 = GitFake()
    rc = pc.cmd_repo_add(_args(), board=board2, clone_runner=gitr2, repos_dir=repos)
    assert rc == 0
    config_calls = [c for c in gitr2.calls if "config" in c and "remote.origin.fetch" in c]
    assert len(config_calls) == 1              # 정확히 1회(중복 set 폭주 0)


# ── clone 실패 시 refspec/fetch skip (DoD) ───────────────────────────────────


def test_clone_failure_skips_refspec_and_fetch(pc, tmp_path, capsys):
    """clone 실패(rc≠0)면 refspec/fetch 호출 0 — early return·부작용 0 (T-0152·fail-soft)."""
    board = FakeBoard(registered=())
    gitr = GitFake(clone_rc=128)
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr,
                         repos_dir=tmp_path / ".repos")
    assert rc == 1
    assert _config_call(gitr) is None          # clone 실패 → refspec skip
    assert _fetch_call(gitr) is None           # clone 실패 → fetch skip
    assert "clone" in capsys.readouterr().err.lower()


# ── fail-soft — refspec/fetch 실패가 repo add 를 깨지 않는다 ──────────────────


def test_refspec_config_failure_skips_fetch_but_repo_add_succeeds(pc, tmp_path, capsys):
    """refspec config 실패 시 fetch 는 skip(미설정이면 origin/* 못 채움)하되 repo add rc 0 (fail-soft)."""
    board = FakeBoard(registered=())
    gitr = GitFake(config_rc=1)
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr,
                         repos_dir=tmp_path / ".repos")
    assert rc == 0                             # fail-soft — refspec 실패가 repo add 를 안 깬다
    assert _config_call(gitr) is not None       # config 는 시도됨
    assert _fetch_call(gitr) is None           # config 실패 → fetch skip
    assert len(board.append_calls) == 1         # 등록은 진행됨(refspec 은 추가 가드)
    assert "refspec 설정 실패" in capsys.readouterr().err


def test_fetch_failure_warns_but_repo_add_succeeds(pc, tmp_path, capsys):
    """fetch 실패(네트워크)는 경고 surface 하되 repo add rc 0 — refspec 은 박혔으니 복구 가능 (fail-soft)."""
    board = FakeBoard(registered=())
    gitr = GitFake(fetch_rc=1)
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr,
                         repos_dir=tmp_path / ".repos")
    assert rc == 0                             # fail-soft — fetch 실패가 repo add 를 안 깬다
    assert _config_call(gitr) is not None        # refspec 은 설정됨(이후 fetch 로 복구 가능)
    assert _fetch_call(gitr) is not None         # fetch 는 시도됨(실패)
    assert len(board.append_calls) == 1         # 등록은 진행됨
    assert "fetch origin" in capsys.readouterr().err


# ── repo명 자동시드 폐지 (ADR-0042·T-0237) ───────────────────────────────────


def test_repo_add_seeds_empty_prefix_not_repo_name(pc, tmp_path):
    """repo add 는 prefix 를 **빈 값**으로 등록한다 — repo명 자동시드 폐지 (ADR-0042·DoD 1).

    이전엔 `areas_append(name, …)` 로 repo명이 prefix 칼럼에 박혀 다음 티켓이 `T-<repo>-NNN`
    으로 튀었다. 이제 positional prefix 는 `""` — repo명은 작업 카테고리가 아니다.
    """
    board = FakeBoard(registered=())
    rc = pc.cmd_repo_add(_args(name="svc"), board=board, clone_runner=GitFake(),
                         repos_dir=tmp_path / ".repos")
    assert rc == 0
    assert len(board.append_calls) == 1
    call = board.append_calls[0]
    assert call["prefix"] == ""          # 자동시드 폐지 — repo명 아님
    assert call["repo"] == "svc"          # repo 칼럼엔 repo명 유지(등록 식별)


def test_repo_add_idempotent_by_repo_name_not_prefix(pc, tmp_path):
    """이미 등록된 repo(빈 prefix) 재실행 → 중복 append 0 — 멱등 판별이 repo명 기준 (ADR-0042·DoD 1).

    자동시드 폐지로 prefix 칼럼이 비어 `registered_prefixes()` 로는 이 repo 를 못 세므로,
    멱등 판별을 `registered_repos()`(repo 칼럼)로 옮겼다 — 재실행이 같은 repo 를 두 줄로
    append 하지 않는다(append-only 레지스트리 오염 방지).
    """
    board = FakeBoard(registered=("svc",))    # repo명으로 이미 등록됨
    (tmp_path / ".repos" / "svc.git").mkdir(parents=True)
    rc = pc.cmd_repo_add(_args(name="svc"), board=board, clone_runner=GitFake(),
                         repos_dir=tmp_path / ".repos")
    assert rc == 0
    assert board.append_calls == []           # 중복 등록 안 됨(멱등)


# ── 헬퍼 직접 단위 (배선 격리) ───────────────────────────────────────────────


def test_set_bare_fetch_refspec_helper_success(pc, tmp_path, capsys):
    """`_set_bare_fetch_refspec` 헬퍼 — config 후 fetch, 둘 다 bare 컨텍스트(-C <bare>) 호출."""
    bare = tmp_path / "svc.git"
    gitr = GitFake()
    pc._set_bare_fetch_refspec(bare, runner=gitr)
    cfg = _config_call(gitr)
    fetch = _fetch_call(gitr)
    assert cfg is not None and cfg[1] == str(bare) and cfg[-1] == REFSPEC
    assert fetch is not None and fetch[1] == str(bare)
    assert "fetch origin" in capsys.readouterr().out


def test_bare_fetch_refspec_constant(pc):
    """refspec 상수가 origin/* remote-tracking 매핑(force `+`) 형태 (회귀 고정·T-0152)."""
    assert pc._BARE_FETCH_REFSPEC == REFSPEC


# ── 기본 브랜치 origin tracking 보정 (T-0273 — T-0152 나머지 절반) ───────────
#
# `git clone --bare` 는 로컬 branch.<n>.remote/merge 를 안 만든다 → 그 bare 슬롯의 기본
# 브랜치가 @{upstream} 미해소. `_ensure_bare_branch_tracking` 이 bare HEAD 기본 브랜치에
# branch.<d>.remote=origin + merge=refs/heads/<d> 를 박아 닫는다(멱등·fail-soft).


def test_fresh_clone_sets_default_branch_tracking(pc, tmp_path):
    """신규 bare clone 직후 → branch.main.remote=origin + branch.main.merge=refs/heads/main (T-0273·DoD).

    두 config 가 bare 컨텍스트(-C <bare>)로 같은 주입 runner 에 불린다 — refspec 보정
    (origin/* 절반) 옆에서 tracking 나머지 절반을 채운다.
    """
    board = FakeBoard(registered=())
    gitr = GitFake()
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    bare = str(repos / "svc.git")
    calls = _branch_config_calls(gitr)
    assert ["-C", bare, "config", "branch.main.remote", "origin"] in calls
    assert ["-C", bare, "config", "branch.main.merge", "refs/heads/main"] in calls


def test_existing_bare_reuse_still_sets_branch_tracking(pc, tmp_path):
    """기존 bare 재사용(clone skip) 경로도 tracking 보정 — 과거 tracking-없는 bare 자가치유 (T-0273)."""
    board = FakeBoard(registered=())
    gitr = GitFake()
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)    # bare 이미 존재 → clone skip
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    assert _clone_call(gitr) is None           # clone 안 함(재사용)
    assert _branch_config_calls(gitr)          # tracking 보정은 멱등 수행


def test_already_registered_bare_exists_still_sets_branch_tracking(pc, tmp_path):
    """이미 등록 + bare 존재(등록 no-op) 경로도 tracking 보정 — early return 이전 위치라 수행 (T-0273)."""
    board = FakeBoard(registered=("svc",))
    gitr = GitFake()
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    assert board.append_calls == []            # 등록 no-op
    assert _branch_config_calls(gitr)          # tracking 보정은 그래도 수행(early-return 이전)


def test_branch_tracking_idempotent_on_rerun(pc, tmp_path):
    """재실행(기존 bare)에서도 tracking 보정이 정확히 remote+merge 2개 — set=덮어쓰기 멱등 (T-0273)."""
    board = FakeBoard(registered=())
    repos = tmp_path / ".repos"
    pc.cmd_repo_add(_args(), board=board, clone_runner=GitFake(), repos_dir=repos)
    (repos / "svc.git").mkdir(parents=True, exist_ok=True)   # 1차 clone 결과 모사
    gitr2 = GitFake()
    rc = pc.cmd_repo_add(_args(), board=FakeBoard(registered=()),
                         clone_runner=gitr2, repos_dir=repos)
    assert rc == 0
    assert len(_branch_config_calls(gitr2)) == 2   # 정확히 remote+merge (중복 폭주 0)


def test_branch_tracking_non_main_default(pc, tmp_path):
    """기본 브랜치가 main 아닌 경우(develop)도 정확 해소 → branch.develop.* (T-0273·DoD)."""
    board = FakeBoard(registered=())
    gitr = GitFake(head="develop")
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    bare = str(repos / "svc.git")
    calls = _branch_config_calls(gitr)
    assert ["-C", bare, "config", "branch.develop.remote", "origin"] in calls
    assert ["-C", bare, "config", "branch.develop.merge", "refs/heads/develop"] in calls


def test_branch_tracking_config_failure_warns_but_repo_add_succeeds(pc, tmp_path, capsys):
    """tracking config 실패는 경고 surface 하되 repo add rc 0 — tracking 은 추가 가드 (T-0273·fail-soft)."""
    board = FakeBoard(registered=())
    gitr = GitFake(branch_config_rc=1)
    rc = pc.cmd_repo_add(_args(), board=board, clone_runner=gitr,
                         repos_dir=tmp_path / ".repos")
    assert rc == 0                              # fail-soft — tracking 실패가 repo add 를 안 깬다
    assert len(board.append_calls) == 1         # 등록은 진행됨(추가 가드)
    assert "branch.main.remote" in capsys.readouterr().err


def test_ensure_bare_branch_tracking_helper_success(pc, tmp_path, capsys):
    """`_ensure_bare_branch_tracking` 헬퍼 — 기본 브랜치에 remote+merge 를 -C <bare> 로 박는다 (T-0273)."""
    bare = tmp_path / "svc.git"
    gitr = GitFake()
    pc._ensure_bare_branch_tracking(bare, runner=gitr)
    calls = _branch_config_calls(gitr)
    assert ["-C", str(bare), "config", "branch.main.remote", "origin"] in calls
    assert ["-C", str(bare), "config", "branch.main.merge", "refs/heads/main"] in calls
    assert "origin tracking" in capsys.readouterr().out


def test_ensure_bare_branch_tracking_failsoft_head_unresolved(pc, tmp_path, capsys):
    """bare HEAD 미해소(symbolic-ref·rev-parse 둘 다 실패) → 조용히 계속·tracking 대상 없음 (T-0273·fail-soft)."""
    bare = tmp_path / "svc.git"
    gitr = GitFake(symref_rc=1, revparse_rc=1)
    pc._ensure_bare_branch_tracking(bare, runner=gitr)   # 예외 없이 리턴(rc 안 깨짐)
    assert _branch_config_calls(gitr) == []              # 해소 실패 → config 시도 안 함
    assert "기본 브랜치 해소 실패" in capsys.readouterr().err


def test_ensure_bare_branch_tracking_revparse_fallback(pc, tmp_path):
    """symbolic-ref 실패 시 rev-parse --abbrev-ref HEAD 폴백으로 기본 브랜치 해소 (T-0273)."""
    bare = tmp_path / "svc.git"
    gitr = GitFake(symref_rc=1, revparse_rc=0, head="main")
    pc._ensure_bare_branch_tracking(bare, runner=gitr)
    assert any("rev-parse" in c for c in gitr.calls)      # 폴백 경로가 탔다
    calls = _branch_config_calls(gitr)
    assert ["-C", str(bare), "config", "branch.main.remote", "origin"] in calls
    assert ["-C", str(bare), "config", "branch.main.merge", "refs/heads/main"] in calls


# ── 통합 — 실 git 으로 origin/main remote-tracking ref 생성 고정 (codex/reviewer 권고) ──


@pytest.mark.skipif(shutil.which("git") is None, reason="git 바이너리 부재 (실 clone 불가)")
def test_real_clone_creates_origin_main_tracking_ref(pc, tmp_path):
    """실 `git clone --bare` 경로가 origin/main remote-tracking ref 를 실제로 만든다 (T-0152·통합).

    mock 배선 테스트(GitFake)는 argv 단언만이라 refspec 문자열이 미묘히 틀려도 통과한다 —
    이 테스트는 clone_runner 미주입(실 `_real_clone_runner`·실 git clone --bare)으로
    refspec 설정 + `fetch origin` 이 *실제로* `.repos/<name>.git` 에
    `refs/remotes/origin/main` 을 생성하는지 고정한다(네트워크 0·로컬 tmp source repo only).
    """
    git_binary = shutil.which("git")

    # 1) tmp 에 source repo — 환경 git 버전 무관하게 기본 브랜치를 main 으로 고정한다
    #    (init -b main 미지원 구 git 도 symbolic-ref 로 main 강제).
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run([git_binary, "init"], cwd=src, check=True, capture_output=True)
    subprocess.run(
        [git_binary, "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=src, check=True, capture_output=True,
    )
    subprocess.run([git_binary, "config", "user.email", "t@t"], cwd=src, check=True, capture_output=True)
    subprocess.run([git_binary, "config", "user.name", "t"], cwd=src, check=True, capture_output=True)
    (src / "README").write_text("hello\n", encoding="utf-8")
    subprocess.run([git_binary, "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(
        [git_binary, "commit", "-m", "init"],
        cwd=src, check=True, capture_output=True,
    )

    # 2) cmd_repo_add — clone_runner 미주입(실 _real_clone_runner·실 git clone --bare).
    #    board 는 FakeBoard 주입(areas.md 실파일 미접촉). source 는 로컬 file 경로(네트워크 0).
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(
        _args(git=str(src), name="svc"),
        board=FakeBoard(registered=()),
        repos_dir=repos,
    )
    assert rc == 0

    # 3) 핵심 단언 — refspec + fetch 가 실제로 origin/main remote-tracking ref 를 만들었다.
    bare = repos / "svc.git"
    rev = subprocess.run(
        [git_binary, "-C", str(bare), "rev-parse", "--verify", "--quiet",
         "refs/remotes/origin/main"],
        capture_output=True,
    )
    assert rev.returncode == 0, "refspec+fetch 가 refs/remotes/origin/main 을 생성하지 못함"

    # 보강 — remote.origin.fetch refspec 이 실제로 박혔다(clone --bare 가 안 박는 결함 근절).
    cfg = subprocess.run(
        [git_binary, "-C", str(bare), "config", "--get", "remote.origin.fetch"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert cfg.returncode == 0
    assert cfg.stdout.strip() == REFSPEC


@pytest.mark.skipif(shutil.which("git") is None, reason="git 바이너리 부재 (실 clone 불가)")
def test_real_clone_sets_default_branch_upstream_tracking(pc, tmp_path):
    """실 `git clone --bare` 경로가 기본 브랜치 origin tracking 을 박고 슬롯 @{upstream} 을 해소한다 (T-0273·통합).

    mock 배선(GitFake)은 argv 단언만이라 config 값이 미묘히 틀려도 통과한다 — 이 테스트는
    clone_runner 미주입(실 clone)으로 (1) bare 에 branch.main.remote=origin +
    branch.main.merge=refs/heads/main 이 실제로 박히고, (2) 그 bare 를 공유하는 worktree 슬롯이
    config 상속으로 main@{upstream}=origin/main 을 해소함을 고정한다(검증된 사실·PM 55·네트워크 0).
    """
    git_binary = shutil.which("git")

    # 1) tmp source repo — 기본 브랜치 main 강제(구 git 도 symbolic-ref 로).
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run([git_binary, "init"], cwd=src, check=True, capture_output=True)
    subprocess.run(
        [git_binary, "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=src, check=True, capture_output=True,
    )
    subprocess.run([git_binary, "config", "user.email", "t@t"], cwd=src, check=True, capture_output=True)
    subprocess.run([git_binary, "config", "user.name", "t"], cwd=src, check=True, capture_output=True)
    (src / "README").write_text("hello\n", encoding="utf-8")
    subprocess.run([git_binary, "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run([git_binary, "commit", "-m", "init"], cwd=src, check=True, capture_output=True)

    # 2) cmd_repo_add — 실 clone(refspec[T-0152] + branch tracking[T-0273] 둘 다 실행).
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(
        _args(git=str(src), name="svc"),
        board=FakeBoard(registered=()),
        repos_dir=repos,
    )
    assert rc == 0
    bare = repos / "svc.git"

    # 3) 핵심 — bare 에 branch.main.remote/merge 가 실제로 박혔다(clone --bare 가 안 박는 나머지 절반).
    remote = subprocess.run(
        [git_binary, "-C", str(bare), "config", "--get", "branch.main.remote"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert remote.returncode == 0 and remote.stdout.strip() == "origin"
    merge = subprocess.run(
        [git_binary, "-C", str(bare), "config", "--get", "branch.main.merge"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert merge.returncode == 0 and merge.stdout.strip() == "refs/heads/main"

    # 4) 검증된 사실 — 그 bare 를 공유하는 worktree 슬롯이 config 상속으로 @{upstream} 을 해소한다.
    wt = tmp_path / "wt-main"
    add = subprocess.run(
        [git_binary, "-C", str(bare), "worktree", "add", str(wt), "main"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert add.returncode == 0, f"worktree add 실패: {add.stdout}{add.stderr}"
    up = subprocess.run(
        [git_binary, "-C", str(wt), "rev-parse", "--abbrev-ref", "main@{upstream}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert up.returncode == 0, f"main@{{upstream}} 미해소: {up.stdout}{up.stderr}"
    assert up.stdout.strip() == "origin/main"
