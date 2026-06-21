"""pm-config 셋업 파사드 디스패처 단위/배선 테스트 (T-0061 · ADR-0011·0014).

가벼운 디스패처(`pm_config.py`)의 라우팅·`--help` surface·엔진 호출 배선을 검증한다.
엔진 부작용(실 clone/worktree)은 **mock 주입**으로 격리한다 — test_worktree_pool.py 의
DI seam·test_pm_update.py 의 monkeypatch 격리 패턴 동류. 실 git/board/worktree_pool 을
건드리지 않고 *어떤 엔진을 어떤 인자로 부르는지*(배선)만 결정적으로 친다.

커버:
  - 디스패치 라우팅 — 각 서브커맨드가 올바른 핸들러로 간다.
  - `--help` surface — 등록 안내(서브커맨드 목록)가 단일 소스(epilog/docstring)에서 나온다.
  - init forward — `init` 뒤 인자가 board.main(["init", ...]) 으로 *verbatim*(argparse 미가공) 전달.
  - update forward — `update` 뒤 인자가 pm_update.main 으로 *verbatim*(argparse 미가공) 전달.
  - repo add 배선 — areas_append(per-repo 스키마) + `git clone --bare .repos/<name>.git`.
  - worktree add 배선 — worktree_pool.create_slot 호출.
  - status|whoami 배선 — list_leases() + 이 세션 리스 surface.
  - release 배선 — release / --force=force_release.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pc():
    return _load_pm_config()


# ── 주입형 엔진 fake (DI seam — hermetic) ────────────────────────────────────


class FakeLease:
    """worktree_pool.Lease 의 최소 대역 — 배선 검증에 필요한 필드만."""

    def __init__(self, slot, repo, branch=None, session="s1", pid=1, state="leased",
                 test_cmd=None):
        self.slot = slot
        self.repo = repo
        self.branch = branch
        self.session = session
        self.pid = pid
        self.state = state
        self.test_cmd = test_cmd  # T-0066 — 슬롯 바인딩 회귀명령(파사드 print surface)


class FakeWorktreePool:
    """worktree_pool 모듈 대역 — 호출을 기록하고 미리 정한 결과를 돌려준다.

    실 `git worktree add`/리스장부 없이 create_slot/list_leases/release/force_release
    배선을 결정적으로 친다. 예외 클래스(NeedsCreate·ReleaseRefused)도 노출해 디스패처가
    그를 잡는 경로를 검증할 수 있게 한다.
    """

    class NeedsCreate(Exception):
        def __init__(self, repo):
            self.repo = repo
            super().__init__(repo)

    class ReleaseRefused(Exception):
        def __init__(self, slot):
            self.slot = slot
            super().__init__(slot)

    def __init__(self, *, leases=None, release_raises=None, force_returns="present",
                 set_test_raises=None, live_branches=None):
        self.leases = leases or []
        self.calls: list[tuple] = []
        self._release_raises = release_raises   # 예외 클래스 또는 None
        self._force_returns = force_returns     # "present" → Lease, "absent" → None
        self._set_test_raises = set_test_raises  # set_test_cmd 가 던질 예외 클래스 또는 None
        # slot → live 브랜치 매핑(ADR-0013 amend T-0072 — cmd_status 가 lease.branch 대신
        # current_branch(slot) 로 슬롯 git HEAD 를 live 조회). 미지정 슬롯은 None(detached).
        self._live_branches = live_branches or {}

    def create_slot(self, repo, *, base=None, test_cmd=None):
        # base (T-0075) — areas 의 그 repo base 를 cmd_worktree_add 가 전달한다(슬롯 브랜치
        # 파생 base). 호출 인자에 base 를 함께 기록해 배선 검증.
        self.calls.append(("create_slot", repo, test_cmd, base))
        return FakeLease(slot=f"work/{repo}_1", repo=repo, test_cmd=test_cmd)

    def install_protected_hook(self, repo, protected):
        # 보호 브랜치 pre-push 훅 (재)설치 대역 (T-0076) — repo·protected 목록을 기록해
        # repo add/worktree add 가 보호 훅 설치를 호출하는 배선을 결정적으로 검증한다.
        # True 반환(bare 존재 시 설치 성공·실 install_protected_hook 계약과 동형).
        self.calls.append(("install_protected_hook", repo, list(protected)))
        return True

    def set_test_cmd(self, slot, cmd):
        self.calls.append(("set_test_cmd", slot, cmd))
        if self._set_test_raises is not None:
            raise self._set_test_raises(slot)
        return FakeLease(slot=slot, repo="r", test_cmd=cmd)

    def slot_path(self, slot):
        self.calls.append(("slot_path", slot))
        return REPO / slot

    def list_leases(self):
        self.calls.append(("list_leases",))
        return self.leases

    def current_branch(self, slot, *, git_runner=None):
        # 슬롯 worktree 의 git HEAD live 조회 대역(ADR-0013 amend T-0072). 매핑에 없으면
        # None(detached/조회불가) — cmd_status 가 "(detached/조회불가)" 로 surface.
        self.calls.append(("current_branch", slot))
        return self._live_branches.get(slot)

    def release(self, slot):
        self.calls.append(("release", slot))
        if self._release_raises is not None:
            raise self._release_raises(slot)
        return FakeLease(slot=slot, repo="r", state="idle")

    def force_release(self, slot):
        self.calls.append(("force_release", slot))
        if self._force_returns == "absent":
            return None
        return FakeLease(slot=slot, repo="r", state="idle")

    def did(self, name) -> bool:
        return any(c[0] == name for c in self.calls)


class FakeBoard:
    """board 모듈 대역 — areas_append 호출 인자를 기록·registered_prefixes 제어.

    main(argv) 도 기록한다 — `init` 위임(board.main(["init", ...]) verbatim forward·
    T-0065) 배선 검증용. main 의 rc 는 board_main_rc 로 제어(rc 전파 테스트).
    """

    def __init__(self, *, registered=(), board_main_rc=0, repo_bases=None,
                 repo_protecteds=None):
        self._registered = set(registered)
        self.append_calls: list[tuple] = []
        self.main_argv = None
        self._board_main_rc = board_main_rc
        # repo → base 매핑 (T-0075) — `_resolve_repo_base` 가 board._repo_base 를 부르므로
        # worktree add 가 areas base 를 create_slot 으로 전달하는 배선을 결정적으로 친다.
        self._repo_bases = repo_bases or {}
        # repo → protected 목록 매핑 (T-0076) — `_resolve_repo_protected` 가
        # board._repo_protected 를 부른다. 미지정 매핑은 default(main/master/develop).
        self._repo_protecteds = repo_protecteds or {}

    def registered_prefixes(self):
        return set(self._registered)

    def areas_append(self, prefix, area, owner, *, repo=None, git=None,
                     test_cmd=None, base=None, protected=None):
        self.append_calls.append(
            {"prefix": prefix, "area": area, "owner": owner,
             "repo": repo, "git": git, "test_cmd": test_cmd, "base": base,
             "protected": protected}
        )

    def _repo_base(self, repo):
        # board._repo_base 대역 (T-0075) — 매핑에 없으면 None(구 스키마/솔로/미지정 폴백).
        return self._repo_bases.get(repo)

    def _repo_protected(self, repo):
        # board._repo_protected 대역 (T-0076) — 매핑에 없으면 default(main/master/develop)
        # 폴백(`_resolve_repo_protected` 가 이 값을 install_protected_hook 으로 전달).
        return self._repo_protecteds.get(repo, ["main", "master", "develop"])

    def main(self, argv):
        self.main_argv = argv
        return self._board_main_rc


class FakeGitRecorder:
    """git clone runner 대역 — argv 기록·미리 정한 (rc, out) 반환."""

    def __init__(self, *, rc=0, out=""):
        self.calls: list[list] = []
        self._rc = rc
        self._out = out

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self._rc, self._out


class FakePmUpdate:
    """pm_update 모듈 대역 — main(argv) 호출 인자를 기록한다 (forward verbatim 검증)."""

    def __init__(self, *, rc=0):
        self.main_argv = None
        self._rc = rc

    def main(self, argv):
        self.main_argv = argv
        return self._rc


# ── --help surface (등록 안내 단일 소스) ──────────────────────────────────────


def test_help_surfaces_all_subcommands(pc, capsys):
    """`pm-config` 무인자 → 도움말 surface + 모든 서브커맨드 목록(rc 1)."""
    rc = pc.main([])
    assert rc == 1
    out = capsys.readouterr().out
    for sub in ("init", "repo", "worktree", "status", "whoami", "release", "update"):
        assert sub in out, f"서브커맨드 {sub!r} 가 --help surface 에 없다"


def test_dash_help_flag_exits_zero(pc):
    """`--help` 플래그는 argparse 가 surface 후 SystemExit(0)."""
    with pytest.raises(SystemExit) as exc:
        pc.main(["--help"])
    assert exc.value.code == 0


# ── init forward — verbatim (board.main(["init", ...]) · argparse 미가공) ─────


def test_init_forwards_verbatim_to_board_main(pc):
    """`init --prefix X --area a` → board.main(["init","--prefix","X","--area","a"]) 그대로.

    "init" prefix + 뒤 토큰을 verbatim(argparse 미가공) 으로 board.main 에 넘긴다 —
    board.py init 이 CLI 계약의 단일 진실(중복 파싱 0·T-0065).
    """
    fake = FakeBoard()
    rc = pc.cmd_init(["--prefix", "X", "--area", "a"], board=fake)
    assert rc == 0
    assert fake.main_argv == ["init", "--prefix", "X", "--area", "a"]


def test_init_no_args_forwards_bare_init(pc):
    """`init`(무인자) → board.main(["init"]) — 보편 솔로/팀 셋업(플래그 없이도 forward)."""
    fake = FakeBoard()
    rc = pc.cmd_init([], board=fake)
    assert rc == 0
    assert fake.main_argv == ["init"]


def test_init_forwards_through_main_dispatch(pc, monkeypatch):
    """main(["init", ...]) 경로도 option-like 플래그를 가로채지 않고 forward 한다.

    디스패처가 `--prefix`·`--owner`·`--session` 등을 자기 플래그로 오인하지 않음을
    검증 (argparse 우회 special-case·update 동형). raw 토큰 순서·내용 verbatim 보존.
    """
    fake = FakeBoard()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: fake if name == "board" else None)
    rc = pc.main(["init", "--prefix", "svc", "--owner", "me", "--session", "s1"])
    assert rc == 0
    assert fake.main_argv == ["init", "--prefix", "svc", "--owner", "me", "--session", "s1"]


def test_init_propagates_board_main_returncode(pc):
    """board.main 의 rc 가 그대로 전파된다 (위임·중복 로직 0)."""
    fake = FakeBoard(board_main_rc=3)
    assert pc.cmd_init(["--prefix", "X"], board=fake) == 3


def test_init_engine_missing_errors_isolated(pc, monkeypatch, capsys):
    """_load_module 가 None(board 부재)이면 명시 에러 rc 1."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_init(["--prefix", "X"])
    assert rc == 1
    assert "board.py 엔진을 찾을 수 없다" in capsys.readouterr().err


# ── update forward — verbatim (argparse 미가공) ──────────────────────────────


def test_update_forwards_verbatim_to_pm_update(pc):
    """`update --from X --dry-run` → pm_update.main(["--from","X","--dry-run"]) 그대로."""
    fake = FakePmUpdate(rc=0)
    rc = pc.cmd_update(["--from", "/up/stream", "--dry-run"], pm_update=fake)
    assert rc == 0
    assert fake.main_argv == ["--from", "/up/stream", "--dry-run"]


def test_update_forwards_through_main_dispatch(pc, monkeypatch):
    """main(["update", ...]) 경로도 option-like 플래그를 가로채지 않고 forward 한다.

    디스패처가 `--dry-run` 을 자기 플래그로 오인하지 않음을 검증 (argparse 우회 special-case).
    """
    fake = FakePmUpdate(rc=0)
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: fake if name == "pm_update" else None)
    rc = pc.main(["update", "--dry-run", "--from", "/x"])
    assert rc == 0
    assert fake.main_argv == ["--dry-run", "--from", "/x"]


def test_update_propagates_pm_update_returncode(pc):
    """pm_update.main 의 rc 가 그대로 전파된다 (위임·중복 로직 0)."""
    fake = FakePmUpdate(rc=2)
    assert pc.cmd_update(["--from", "/x"], pm_update=fake) == 2


def test_update_engine_missing_errors_isolated(pc, monkeypatch, capsys):
    """_load_module 가 None(엔진 부재)이면 명시 에러 rc 1."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_update(["--dry-run"])
    assert rc == 1
    assert "pm_update.py 엔진을 찾을 수 없다" in capsys.readouterr().err


# ── repo add 배선 — areas_append(per-repo) + git clone --bare ────────────────


def _repo_add_args(pc, name="svc", git="git@h:me/svc.git", test="pytest -q", owner=None,
                   base=None):
    return argparse.Namespace(name=name, git=git, test=test, owner=owner, base=base)


def _clone_argv(gitr):
    """gitr 호출 중 `clone` argv 를 찾는다 (clone 과 base 해소 git 호출이 섞이므로·T-0075)."""
    for argv in gitr.calls:
        if argv and argv[0] == "clone":
            return argv
    return None


def test_repo_add_registers_areas_and_clones(pc, tmp_path):
    """repo add → `git clone --bare .repos/<name>.git` 후 areas_append(per-repo 스키마 칼럼)."""
    board = FakeBoard(registered=())
    # 기본 GitRecorder(rc=0·out="") — base 미지정 시 symbolic-ref 가 (0,"") → base="" 로 해소.
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", git="git@h:me/svc.git", test="pytest -q",
                       owner="me"),
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    # areas_append — per-repo 스키마(repo/git/test_cmd/base) 로 호출.
    assert len(board.append_calls) == 1
    call = board.append_calls[0]
    assert call["prefix"] == "svc"
    assert call["repo"] == "svc"
    assert call["git"] == "git@h:me/svc.git"
    assert call["test_cmd"] == "pytest -q"
    assert call["owner"] == "me"
    assert call["base"] == ""   # base 미지정 + symbolic-ref (0,"") → 빈 base(미해소·현행 폴백)
    # git clone --bare <url> .repos/svc.git (base 해소 git 호출과 섞여 있을 수 있음).
    argv = _clone_argv(gitr)
    assert argv is not None
    assert "--bare" in argv
    assert argv[-2] == "git@h:me/svc.git"
    assert argv[-1].endswith("svc.git")
    assert str(repos) in argv[-1]


def test_repo_add_already_registered_bare_exists_is_noop(pc, tmp_path):
    """이미 등록 + bare 존재 → 완전 no-op rc 0(append 0·clone 0·멱등 재실행)."""
    board = FakeBoard(registered=("svc",))
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)   # bare 가 이미 있음
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc"),
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    assert board.append_calls == []   # 중복 등록 안 함
    assert gitr.calls == []           # 이미 완비 → clone 안 함(no-op)


def test_repo_add_already_registered_bare_missing_retries_clone(pc, tmp_path):
    """이미 등록 + bare 부재 → 등록 건너뛰고 clone *재시도*(append 0·clone 1·rc 0).

    첫 실행이 areas 등록만 남기고 clone 실패한 상태의 복구 경로 — 옛 동작(중복=무조건
    거부)은 clone 을 영영 막았다(멱등·재시도 가능 회귀 가드).
    """
    board = FakeBoard(registered=("svc",))   # 이미 등록
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"               # bare 는 없음(clone 미완)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", git="git@h:me/svc.git"),
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    assert board.append_calls == []           # 중복 등록 안 함(append-only 보호)
    assert len(gitr.calls) == 1               # clone 재시도됨
    argv = gitr.calls[0]
    assert argv[0] == "clone" and "--bare" in argv
    assert argv[-2] == "git@h:me/svc.git"
    assert argv[-1].endswith("svc.git")


def test_repo_add_clone_failure_returns_error(pc, tmp_path, capsys):
    """clone 실패(rc!=0)면 rc 1 — 등록은 clone 성공 후이므로 areas 미등록 (T-0075 reorder).

    base 해소가 bare 에 의존하므로 등록 순서를 clone 뒤로 옮겼다 — clone 실패 시 areas_append
    를 부르지 않는다(이전엔 등록 후 clone 이라 실패해도 등록이 남았다). 재실행이 clone→등록을
    다시 한다(여전히 멱등·재시도 가능).
    """
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=128, out="fatal: repository not found")
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 1
    assert board.append_calls == []   # clone 실패 → 등록 안 함(reorder·재실행으로 복구)
    assert "clone" in capsys.readouterr().err.lower()


def test_repo_add_skips_clone_if_bare_exists(pc, tmp_path):
    """`.repos/<name>.git` 이 이미 있으면 clone 건너뜀(재사용·중복 clone 방지)·base 는 해소."""
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc"),
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    assert len(board.append_calls) == 1
    # clone 은 안 함(bare 존재) — base 해소 symbolic-ref 만 돈다(clone argv 없음).
    assert _clone_argv(gitr) is None


# ── repo add base 브랜치 (T-0075) ────────────────────────────────────────────


class _BaseAwareGit:
    """git 대역 — clone·symbolic-ref·show-ref 를 argv 로 분기해 base 해소를 모델링 (T-0075·T-0078).

    `head` = bare HEAD 해소(symbolic-ref --short HEAD)가 돌려줄 기본 브랜치명.
    `valid_branches` = `show-ref --verify --quiet refs/heads/<b>` 가 rc 0 으로 통과시킬 **로컬
    브랜치** 집합(T-0078). 명시 base 검증은 `refs/heads/` 네임스페이스 한정이라 argv 마지막 토큰이
    `refs/heads/<b>` 형태다 — 그 prefix 를 벗겨 로컬 브랜치 집합과 **exact 대조**한다(show-ref
    --verify 는 revision 문법 미적용 exact-ref primitive). `refs/heads/` 가 아닌 ref(태그·SHA·
    `HEAD`·원격 ref)·revision 표현(`main~0`·`main^{}`)은 valid 집합에 없어 rc 1(거부) — 실 git 의
    `show-ref --verify` exact-ref 검증과 동형. 그 외(clone 등)는 rc 0 성공.
    """

    def __init__(self, *, head="main", valid_branches=()):
        self.calls: list[list] = []
        self._head = head
        self._valid = set(valid_branches)

    def __call__(self, argv):
        self.calls.append(list(argv))
        # `-C <bare> symbolic-ref --short HEAD` — bare HEAD 해소.
        if "symbolic-ref" in argv:
            return 0, self._head + "\n"
        # `-C <bare> show-ref --verify --quiet refs/heads/<b>` — 로컬 브랜치 exact-ref 검증(T-0078).
        if "show-ref" in argv and "--verify" in argv:
            ref = argv[-1]
            prefix = "refs/heads/"
            # show-ref --verify 는 exact-ref primitive(revision 문법 미적용) — 저장된 ref 와
            # 정확히 일치할 때만 통과. refs/heads/<b> prefix 를 벗겨 로컬 브랜치 집합과 exact 대조
            # (태그·SHA·HEAD·원격 ref·revision 표현 main~0·main^{}·부재는 valid 에 없어 rc 1).
            if ref.startswith(prefix) and ref[len(prefix):] in self._valid:
                return 0, ref + "\n"
            return 1, f"fatal: {ref} unknown\n"
        return 0, ""   # clone 등 성공.


def test_repo_add_base_default_resolves_bare_head(pc, tmp_path):
    """--base 미지정 → bare HEAD(symbolic-ref)를 base 로 명시값화·areas 기록 (T-0075)."""
    board = FakeBoard(registered=())
    gitr = _BaseAwareGit(head="main")
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base=None),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert board.append_calls[0]["base"] == "main"   # bare HEAD = main → base 명시값화
    # symbolic-ref 가 bare 컨텍스트(`-C <bare>`)로 불렸는지.
    sym = [c for c in gitr.calls if "symbolic-ref" in c]
    assert sym and "-C" in sym[0]


def test_repo_add_base_explicit_validated_and_recorded(pc, tmp_path):
    """--base develop 지정 + 로컬 브랜치 존재(refs/heads/develop rc0) → 그 base 기록 (T-0075·T-0078).

    반환·기록 base 는 **bare 브랜치명(`develop`)** — refs/heads/ 검증을 거쳐도 areas.md base
    칼럼 계약은 불변(refs/heads/ prefix 를 박지 않는다).
    """
    board = FakeBoard(registered=())
    gitr = _BaseAwareGit(head="main", valid_branches=("develop",))
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base="develop"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert board.append_calls[0]["base"] == "develop"   # 브랜치명만 기록(refs/heads/ prefix 없음)
    # show-ref --verify --quiet refs/heads/develop 검증이 bare 컨텍스트로 불렸는지 (T-0078 정밀화).
    rp = [c for c in gitr.calls if "show-ref" in c and "--verify" in c]
    assert rp and rp[0][-1] == "refs/heads/develop" and "--quiet" in rp[0]


def test_repo_add_base_missing_rejected(pc, tmp_path, capsys):
    """--base nope 지정 + 로컬 브랜치 부재(refs/heads/nope rc≠0) → rc 1 + 등록 차단 (T-0075)."""
    board = FakeBoard(registered=())
    gitr = _BaseAwareGit(head="main", valid_branches=("develop",))  # nope 은 로컬 브랜치 아님
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base="nope"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 1
    assert board.append_calls == []   # 검증 실패 → areas 등록 안 함(잘못된 base 차단)
    assert "nope" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad_base", ["v1.0", "deadbeef", "HEAD", "origin/main", "main~0", "main^{}"]
)
def test_repo_add_base_non_local_branch_rejected(pc, tmp_path, capsys, bad_base):
    """--base 가 태그·SHA·`HEAD`·원격 ref·revision 표현이면 거부 (T-0078 — exact-ref·로컬 브랜치만).

    show-ref --verify refs/heads/<b> exact-ref 검증이라 비-로컬-브랜치 ref 는 통과하지 못한다 —
    `main~0`·`main^{}` 같은 revision 문법도 마찬가지(rev-parse 였다면 우회됐을 벡터·codex must-fix
    회귀 고정). worktree 슬롯 파생[T-0075]은 로컬 브랜치 base 가 전제. clone 은 됐어도 areas 등록은
    막아 잘못된 base 기록 방지.
    """
    board = FakeBoard(registered=())
    # main/develop 만 로컬 브랜치. 태그(v1.0)·SHA(deadbeef)·HEAD·원격(origin/main)은 거부돼야.
    gitr = _BaseAwareGit(head="main", valid_branches=("main", "develop"))
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base=bad_base),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 1
    assert board.append_calls == []   # 비-로컬-브랜치 base → 등록 차단(잘못된 base 기록 방지)
    assert bad_base in capsys.readouterr().err
    # 검증이 refs/heads/<bad_base> 로 한정돼 불렸는지 (정밀화 — 태그/SHA/HEAD/원격/revision 거부의 근거).
    rp = [c for c in gitr.calls if "show-ref" in c and "--verify" in c]
    assert rp and rp[0][-1] == f"refs/heads/{bad_base}"


def test_repo_add_base_local_branch_passes(pc, tmp_path):
    """--base main(로컬 브랜치) → 통과·기록 (T-0078 — 로컬 브랜치 base 보존·기존 동작)."""
    board = FakeBoard(registered=())
    gitr = _BaseAwareGit(head="develop", valid_branches=("main", "develop"))
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base="main"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert board.append_calls[0]["base"] == "main"


def test_repo_add_base_head_resolution_failure_falls_back_empty(pc, tmp_path):
    """bare HEAD 해소 실패(symbolic-ref rc≠0) → base 빈 값(미해소·등록은 진행·현행 폴백·T-0075)."""
    board = FakeBoard(registered=())

    class _NoHeadGit(_BaseAwareGit):
        def __call__(self, argv):
            self.calls.append(list(argv))
            if "symbolic-ref" in argv:
                return 1, "fatal: ref HEAD is not a symbolic ref\n"
            return 0, ""

    gitr = _NoHeadGit()
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base=None),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert board.append_calls[0]["base"] == ""   # 해소 실패 → 빈 base(worktree 가 bare HEAD 폴백)


def test_repo_add_already_registered_skips_base_resolution(pc, tmp_path):
    """이미 등록 + bare 존재 → base 재해소 안 함(append-only·중복 등록 금지·T-0075)."""
    board = FakeBoard(registered=("svc",))
    gitr = _BaseAwareGit(head="main")
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base=None),
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    assert board.append_calls == []                       # 중복 등록 안 함
    assert not any("symbolic-ref" in c for c in gitr.calls)  # base 재해소 안 함(이미 박힘)


def test_repo_add_parser_base_optional(pc):
    """`repo add <name> --git <url> --base <b>` 파싱 — --base optional·기본 None (T-0075)."""
    parser = pc.build_parser()
    args = parser.parse_args(
        ["repo", "add", "svc", "--git", "git@h:me/svc.git", "--base", "develop"])
    assert args.base == "develop"
    args2 = parser.parse_args(["repo", "add", "svc", "--git", "git@h:me/svc.git"])
    assert args2.base is None   # 미지정 → None


# ── repo add name 입력 검증 (T-0078) ─────────────────────────────────────────


def test_validate_repo_name_helper(pc):
    """`_validate_repo_name` 헬퍼 — 허용 패턴 `^[A-Za-z0-9][A-Za-z0-9_-]*$` (T-0078)."""
    # 정상 — 영숫자 시작, 이후 영숫자/`_`/`-`.
    for ok in ("billing", "web_api", "svc-1", "A", "9", "a1_b-2"):
        assert pc._validate_repo_name(ok) is True, ok
    # 위반 — 폴더탈출·경로분리자·공백·`.`·leading `-`·빈 문자열·trailing 개행.
    # trailing-newline(`"billing\n"`)은 `re.match` 의 `$` 가 통과시켜 bare 폴더명 개행·
    # areas.md 줄 corruption 을 부른다 — `fullmatch` 가 거부함을 고정한다(T-0078 재작업).
    for bad in ("../x", "a/b", "a b", "", "-x", ".", "..", "a.b", "a\tb", "a\nb", "_x",
                "billing\n", "x\n"):
        assert pc._validate_repo_name(bad) is False, bad


@pytest.mark.parametrize("bad_name", ["../x", "a/b", "a b", "", "-x"])
def test_repo_add_invalid_name_rejected_no_side_effects(pc, tmp_path, capsys, bad_name):
    """위반 name → rc 1 + 부작용 0(clone·areas_append·훅 미발생) (T-0078·fail-closed).

    가드가 어떤 부작용보다 앞에 있어 잘못된 폴더 clone·areas.md 줄 corruption 을 원천 차단한다.
    board 는 정상 주입(가드는 board None 체크 *이후* 부작용 *이전* — 가드 경로 도달 확인).
    """
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name=bad_name),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 1
    assert board.append_calls == []   # areas 등록 안 함(부작용 0)
    assert gitr.calls == []            # clone/base 해소 git 호출 안 함(부작용 0)
    assert "형식 위반" in capsys.readouterr().err


@pytest.mark.parametrize("good_name", ["billing", "web_api", "svc-1"])
def test_repo_add_valid_name_passes_guard(pc, tmp_path, good_name):
    """정상 name → 가드 통과·등록 진행 (T-0078 — 기존 동작 보존)."""
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name=good_name, git="git@h:me/x.git"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert len(board.append_calls) == 1
    assert board.append_calls[0]["repo"] == good_name


def test_repo_add_board_missing_errors_isolated(pc, tmp_path, monkeypatch, capsys):
    """_load_module 가 None(board 부재)이면 명시 에러 rc 1."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc"),
        clone_runner=FakeGitRecorder(), repos_dir=tmp_path / ".repos",
    )
    assert rc == 1
    assert "board.py 엔진을 찾을 수 없다" in capsys.readouterr().err


# ── worktree add 배선 — create_slot ──────────────────────────────────────────


def test_worktree_add_calls_create_slot(pc, capsys):
    """worktree add <repo> → worktree_pool.create_slot(repo, test_cmd=None, base=None)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    assert wp.did("create_slot")
    # test_cmd 미지정 → None · areas base 없음(FakeBoard 빈 매핑) → base None(현행 bare HEAD).
    assert ("create_slot", "svc", None, None) in wp.calls
    assert "work/svc_1" in capsys.readouterr().out


def test_worktree_add_test_flag_forwards_test_cmd(pc, capsys):
    """worktree add <repo> --test "<cmd>" → create_slot(repo, test_cmd=cmd) (T-0066·ADR-0014 amend)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test="ctest -R hil2"), worktree_pool=wp,
        board=FakeBoard(),
    )
    assert rc == 0
    assert ("create_slot", "svc", "ctest -R hil2", None) in wp.calls
    out = capsys.readouterr().out
    assert "test_cmd 바인딩" in out and "ctest -R hil2" in out  # 사용자 surface


def test_worktree_add_passes_areas_base_to_create_slot(pc, capsys):
    """worktree add <repo> → areas.md 그 repo base 를 create_slot(base=) 로 전달 (T-0075).

    areas 에 svc→develop 이 등록돼 있으면 `_resolve_repo_base` 가 board._repo_base 로 읽어
    create_slot(base="develop") 으로 넘긴다 — 슬롯 브랜치가 develop 에서 파생되게(bare HEAD 아님).
    """
    wp = FakeWorktreePool()
    board = FakeBoard(repo_bases={"svc": "develop"})
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None), worktree_pool=wp, board=board
    )
    assert rc == 0
    assert ("create_slot", "svc", None, "develop") in wp.calls


def test_worktree_add_no_areas_base_passes_none(pc, capsys):
    """areas 에 base 없으면(구 스키마/솔로/미지정) create_slot(base=None) — 현행 회귀 0 (T-0075)."""
    wp = FakeWorktreePool()
    board = FakeBoard(repo_bases={})   # 그 repo base 없음 → None 폴백
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None), worktree_pool=wp, board=board
    )
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls


def test_worktree_add_missing_test_attr_defaults_none(pc, capsys):
    """Namespace 에 test 속성이 아예 없어도 getattr 폴백으로 None(파사드 직접 호출 견고성)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(argparse.Namespace(repo="svc"), worktree_pool=wp,
                             board=FakeBoard())
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls


# ── 보호 브랜치 훅 (재)설치 배선 (T-0076) ─────────────────────────────────────


def _install_hook_call(wp):
    """FakeWorktreePool.calls 중 install_protected_hook 호출을 찾는다 (없으면 None)."""
    for c in wp.calls:
        if c and c[0] == "install_protected_hook":
            return c
    return None


def test_worktree_add_installs_protected_hook(pc, capsys):
    """worktree add → install_protected_hook(repo, protected) (재)설치 호출 (T-0076·자가치유)."""
    wp = FakeWorktreePool()
    board = FakeBoard(repo_protecteds={"svc": ["main", "develop"]})
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None), worktree_pool=wp, board=board
    )
    assert rc == 0
    call = _install_hook_call(wp)
    assert call == ("install_protected_hook", "svc", ["main", "develop"])


def test_worktree_add_install_hook_uses_default_protected(pc, capsys):
    """areas 에 protected 미지정 → board default(main/master/develop)로 훅 설치 (T-0076)."""
    wp = FakeWorktreePool()
    board = FakeBoard(repo_protecteds={})   # 미지정 → FakeBoard._repo_protected default
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None), worktree_pool=wp, board=board
    )
    assert rc == 0
    assert _install_hook_call(wp) == (
        "install_protected_hook", "svc", ["main", "master", "develop"])


def test_repo_add_installs_protected_hook(pc, tmp_path):
    """repo add(신규 등록) → bare clone·areas 등록 후 install_protected_hook 호출 (T-0076)."""
    board = FakeBoard(registered=(), repo_protecteds={"svc": ["main"]})
    gitr = FakeGitRecorder(rc=0)
    wp = FakeWorktreePool()
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
        worktree_pool=wp,
    )
    assert rc == 0
    assert _install_hook_call(wp) == ("install_protected_hook", "svc", ["main"])


def test_repo_add_already_registered_reinstalls_hook(pc, tmp_path):
    """이미 등록 + bare 존재(no-op 등록) 경로도 보호 훅 (재)설치 — 자가치유 (T-0076).

    엔진 update 후 기존 repo 가 다음 repo add 에 훅을 얻는 경로(별도 명령 불요).
    """
    board = FakeBoard(registered=("svc",), repo_protecteds={"svc": ["main", "develop"]})
    gitr = FakeGitRecorder(rc=0)
    wp = FakeWorktreePool()
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)   # bare 이미 존재(no-op 등록 경로)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc"),
        board=board, clone_runner=gitr, repos_dir=repos, worktree_pool=wp,
    )
    assert rc == 0
    assert board.append_calls == []   # 중복 등록 안 함(no-op)
    assert _install_hook_call(wp) == (
        "install_protected_hook", "svc", ["main", "develop"])   # 훅은 재설치(자가치유)


def test_repo_add_registers_protected_column_empty(pc, tmp_path):
    """repo add 신규 등록 → areas_append(protected="") (빈 칼럼·default 폴백·T-0076)."""
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    wp = FakeWorktreePool()
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos", worktree_pool=wp,
    )
    assert rc == 0
    assert board.append_calls[0]["protected"] == ""   # 빈 protected → _repo_protected default


def test_install_protected_hook_helper_fail_soft_no_wp(pc):
    """_install_protected_hook — worktree_pool 부재/헬퍼 부재면 fail-soft False (보호훅=추가 가드·T-0076)."""
    class _NoHookWp:  # install_protected_hook 없는 구 엔진 대역
        pass
    assert pc._install_protected_hook("svc", board=FakeBoard(), worktree_pool=_NoHookWp()) is False


def test_install_protected_hook_helper_fail_soft_on_exception(pc):
    """_install_protected_hook — install 이 던져도 fail-soft False (등록/슬롯 생성을 안 깬다·T-0076)."""
    class _BoomWp:
        def install_protected_hook(self, repo, protected):
            raise RuntimeError("boom")
    assert pc._install_protected_hook("svc", board=FakeBoard(), worktree_pool=_BoomWp()) is False


def test_resolve_repo_protected_board_absent_defaults(pc):
    """_resolve_repo_protected — board 부재면 _DEFAULT_PROTECTED 폴백 (보호 기본값 보장·T-0076)."""
    assert pc._resolve_repo_protected("svc", board=object()) == ["main", "master", "develop"]


def test_worktree_add_create_failure_errors(pc, capsys):
    """create_slot 이 RuntimeError(예: worktree add 실패)면 rc 1 + 명시 에러."""
    wp = FakeWorktreePool()

    def boom(repo, *, base=None, test_cmd=None):
        raise RuntimeError("git worktree add failed")
    wp.create_slot = boom
    rc = pc.cmd_worktree_add(argparse.Namespace(repo="svc", test=None), worktree_pool=wp,
                             board=FakeBoard())
    assert rc == 1
    assert "슬롯 생성 실패" in capsys.readouterr().err


def test_worktree_add_bare_missing_caught_as_runtime_error(pc, capsys):
    """create_slot 이 *실* `BareRepoMissing` 을 던지면 파사드가 잡아 rc 1 + 안내 (cross-module 계약·T-0063).

    `BareRepoMissing` 이 `RuntimeError` 서브클래스여야 `cmd_worktree_add` 의 `except RuntimeError`
    가드가 잡는다 — `Exception` 베이스면 traceback 이 사용자에게 노출된다(codex T-0063 must-fix).
    실 예외 클래스를 import 해 베이스가 회귀하면(다시 Exception) 이 테스트가 깨지도록 박는다.
    """
    spec = importlib.util.spec_from_file_location("worktree_pool", TOOLS / "worktree_pool.py")
    wp_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wp_mod)
    assert issubclass(wp_mod.BareRepoMissing, RuntimeError)  # 계약: 파사드 가드가 잡는 베이스

    wp = FakeWorktreePool()

    def bare_missing(repo, *, base=None, test_cmd=None):
        raise wp_mod.BareRepoMissing(repo, TOOLS.parent / ".repos" / f"{repo}.git")
    wp.create_slot = bare_missing
    rc = pc.cmd_worktree_add(argparse.Namespace(repo="svc", test=None), worktree_pool=wp,
                             board=FakeBoard())
    assert rc == 1
    assert "슬롯 생성 실패" in capsys.readouterr().err


def test_worktree_add_engine_missing_errors(pc, monkeypatch, capsys):
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_worktree_add(argparse.Namespace(repo="svc"))
    assert rc == 1
    assert "worktree_pool.py 엔진을 찾을 수 없다" in capsys.readouterr().err


# ── status | whoami 배선 — list_leases + 이 세션 surface ──────────────────────


def test_status_lists_leases(pc, monkeypatch, capsys):
    """status → list_leases() 호출 + 전체 리스 surface."""
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", branch="feat", session="me", state="leased"),
        FakeLease(slot="work/svc_2", repo="svc", branch=None, session="", state="idle"),
    ]
    wp = FakeWorktreePool(leases=leases)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    assert wp.did("list_leases")
    out = capsys.readouterr().out
    assert "work/svc_1" in out
    assert "work/svc_2" in out


def test_whoami_highlights_my_lease(pc, monkeypatch, capsys):
    """whoami → 이 세션($CLAUDE_SESSION_NAME)의 leased 슬롯을 "이 세션의 리스" 로 강조."""
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", branch="feat", session="me", state="leased"),
        FakeLease(slot="work/svc_3", repo="svc", branch="x", session="other", state="leased"),
    ]
    wp = FakeWorktreePool(leases=leases)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="whoami"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "이 세션의 리스:" in out
    # 내 슬롯(svc_1)은 "이 세션의 리스" 절에, other 의 svc_3 은 거기 없음(전체엔 있음).
    my_section = out.split("풀 전체 리스 장부")[0]
    assert "work/svc_1" in my_section
    assert "work/svc_3" not in my_section


def test_status_empty_pool(pc, capsys):
    """리스 없으면 빈 풀 안내(크래시 없음)."""
    wp = FakeWorktreePool(leases=[])
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    assert "리스 없음" in capsys.readouterr().out


def test_status_shows_live_branch_from_current_branch(pc, monkeypatch, capsys):
    """status 가 branch 를 `current_branch(slot)` live 조회로 표시한다(ADR-0013 amend T-0072).

    장부에 저장된 lease.branch 가 아니라 슬롯 worktree 의 git HEAD live 값을 surface한다 —
    이 세션 리스 줄·풀 전체 줄 둘 다. current_branch 가 호출됐는지도 검증.
    """
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", session="me", state="leased"),
    ]
    # 슬롯 live HEAD = "live-feat" (저장 필드와 무관·git=진실).
    wp = FakeWorktreePool(leases=leases, live_branches={"work/svc_1": "live-feat"})
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "live-feat" in out, "live branch(current_branch) 가 surface 안 됨"
    # 이 세션 리스 줄에도 live branch.
    my_section = out.split("풀 전체 리스 장부")[0]
    assert "live-feat" in my_section
    # current_branch 가 슬롯에 대해 호출됐다(저장 필드 대신 live 조회).
    assert ("current_branch", "work/svc_1") in wp.calls


def test_status_detached_branch_shows_placeholder(pc, monkeypatch, capsys):
    """current_branch 가 None(detached/조회불가)면 "(detached/조회불가)" 로 surface(fail-soft 표시)."""
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", session="me", state="leased"),
    ]
    # live_branches 미지정 → current_branch 가 None(detached/조회불가).
    wp = FakeWorktreePool(leases=leases)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    assert "(detached/조회불가)" in capsys.readouterr().out


def test_status_engine_missing_errors(pc, monkeypatch, capsys):
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_status(argparse.Namespace(command="status"))
    assert rc == 1
    assert "worktree_pool.py 엔진을 찾을 수 없다" in capsys.readouterr().err


# ── release 배선 — release / --force=force_release ───────────────────────────


def test_release_calls_release(pc, capsys):
    """release <slot> → worktree_pool.release(slot) (require_clean 기본)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", force=False), worktree_pool=wp
    )
    assert rc == 0
    assert ("release", "work/svc_1") in wp.calls
    assert not wp.did("force_release")


def test_release_force_calls_force_release(pc, capsys):
    """release <slot> --force → worktree_pool.force_release(slot)."""
    wp = FakeWorktreePool(force_returns="present")
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", force=True), worktree_pool=wp
    )
    assert rc == 0
    assert ("force_release", "work/svc_1") in wp.calls
    assert not wp.did("release")


def test_release_force_absent_slot_is_harmless(pc, capsys):
    """--force 인데 슬롯이 장부에 없으면(None) 무해 종료 rc 0."""
    wp = FakeWorktreePool(force_returns="absent")
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/gone_9", force=True), worktree_pool=wp
    )
    assert rc == 0
    assert "이미 정리됨" in capsys.readouterr().out


def test_release_dirty_refused(pc, capsys):
    """release(비-force) 가 dirty 면 ReleaseRefused → rc 1 + 안내(작업 유실 방지)."""
    wp = FakeWorktreePool(release_raises=FakeWorktreePool.ReleaseRefused)
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", force=False), worktree_pool=wp
    )
    assert rc == 1
    assert "dirty" in capsys.readouterr().err


def test_release_unknown_slot_errors(pc, capsys):
    """release(비-force) 가 KeyError(미존재 리스)면 rc 1 + 명시 에러."""
    wp = FakeWorktreePool(release_raises=KeyError)
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/none_0", force=False), worktree_pool=wp
    )
    assert rc == 1
    assert "리스가 없다" in capsys.readouterr().err


def test_release_engine_missing_errors(pc, monkeypatch, capsys):
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_release(argparse.Namespace(slot="work/x_1", force=False))
    assert rc == 1
    assert "worktree_pool.py 엔진을 찾을 수 없다" in capsys.readouterr().err


# ── main 디스패치 라우팅 (서브커맨드 → 핸들러) ───────────────────────────────


def test_main_routes_status_to_engine(pc, monkeypatch):
    """main(["status"]) 가 cmd_status → worktree_pool.list_leases 로 라우팅됨을 확인.

    build_parser 가 set_defaults(func=cmd_status) 를 빌드 시점에 굳히므로 핸들러를
    직접 monkeypatch 하는 대신, 엔진 로드 seam(_load_module)에 fake worktree_pool 을
    주입해 list_leases 호출(=cmd_status 도달)을 관찰한다.
    """
    wp = FakeWorktreePool(leases=[])
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["status"])
    assert rc == 0
    assert wp.did("list_leases"), "main(['status']) 가 cmd_status→list_leases 로 라우팅 안 됨"


def test_main_routes_release_to_engine(pc, monkeypatch):
    """main(["release", "work/x_1"]) 가 cmd_release → worktree_pool.release 로 라우팅."""
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["release", "work/x_1"])
    assert rc == 0
    assert ("release", "work/x_1") in wp.calls


def test_main_routes_repo_add_to_engine(pc, monkeypatch, tmp_path):
    """main(["repo","add",...]) 가 cmd_repo_add → board.areas_append 로 라우팅."""
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)

    def fake_load(name, filename):
        return board if name == "board" else None
    monkeypatch.setattr(pc, "_load_module", fake_load)
    monkeypatch.setattr(pc, "_real_clone_runner", lambda: gitr)
    monkeypatch.setattr(pc, "REPOS_DIR", tmp_path / ".repos")
    rc = pc.main(["repo", "add", "svc", "--git", "git@h:me/svc.git", "--test", "pytest -q"])
    assert rc == 0
    assert len(board.append_calls) == 1
    assert board.append_calls[0]["repo"] == "svc"


def test_main_routes_worktree_add_to_engine(pc, monkeypatch):
    """main(["worktree","add","svc"]) 가 cmd_worktree_add → create_slot(test_cmd=None) 로 라우팅."""
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["worktree", "add", "svc"])
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls   # --test 없음 → None(현행 하위호환)


def test_main_worktree_add_test_flag_parses_and_forwards(pc, monkeypatch):
    """main(["worktree","add","svc","--test","<cmd>"]) → create_slot(svc, test_cmd=cmd) (T-0066 end-to-end 파싱).

    build_parser 의 --test 서브파서 인자 + cmd_worktree_add 배선을 한 경로로 검증 — DI mock
    wp 로 create_slot 호출의 test_cmd 인자를 관찰한다(실 worktree add 없이).
    """
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["worktree", "add", "svc", "--test", "make hil3"])
    assert rc == 0
    assert ("create_slot", "svc", "make hil3", None) in wp.calls


def test_main_repo_without_add_shows_group_help(pc):
    """`repo`(하위 동작 없이) → repo 그룹 도움말 surface(SystemExit·argparse)."""
    with pytest.raises(SystemExit):
        pc.main(["repo"])


def test_main_worktree_without_add_shows_group_help(pc):
    """`worktree`(하위 동작 없이) → worktree 그룹 도움말 surface(SystemExit)."""
    with pytest.raises(SystemExit):
        pc.main(["worktree"])


# ════════════════════════════════════════════════════════════════════════
# _default_session — board.session_name 과 동형 우선순위 (T-0066 must-fix)
# env > local.conf session= > <host>-<pid>. cmd_status/whoami 의 "이 세션의 리스"
# surface 가 이걸 쓰므로, local.conf-session 운영에서 board 매칭과 정합해야 한다.
# ════════════════════════════════════════════════════════════════════════

def _bind_tmp_repo(pc, monkeypatch, tmp_path):
    """pc 의 REPO 를 tmp 로 재지정 — 실 루트 local.conf 무오염(hermetic).

    _local_conf_session 이 `REPO / .project_manager / local.conf` 를 읽으므로 REPO 를
    tmp 로 묶어야 실 루트를 안 건드린다. module-scope pc 라도 monkeypatch 가 테스트 후 복원.
    """
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pc, "REPO", tmp_path)
    return pm / "local.conf"


def test_default_session_prefers_pm_env(pc, monkeypatch, tmp_path):
    """`$PM_SESSION_NAME` 최우선 — alias·local.conf session= 무시 (T-0073)."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.setenv("PM_SESSION_NAME", "from-pm-env")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    conf.write_text("session=from-conf\n", encoding="utf-8")
    assert pc._default_session() == "from-pm-env"


def test_default_session_claude_env_is_alias(pc, monkeypatch, tmp_path):
    """`$CLAUDE_SESSION_NAME` 단독 → deprecated alias 로 조용히 동작 (T-0073 back-compat)."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    conf.write_text("session=from-conf\n", encoding="utf-8")
    assert pc._default_session() == "from-alias"


def test_default_session_pm_wins_over_claude(pc, monkeypatch, tmp_path):
    """둘 다 설정 시 `PM_SESSION_NAME` 승 (T-0073 마이그레이션 중 명시 우선)."""
    _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.setenv("PM_SESSION_NAME", "new")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "old")
    assert pc._default_session() == "new"


def test_default_session_reads_local_conf_session(pc, monkeypatch, tmp_path):
    """env 없음 → local.conf `session=` (board.session_name 3층과 동형·must-fix)."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    conf.write_text("session=foo\n", encoding="utf-8")
    assert pc._default_session() == "foo"


def test_default_session_falls_back_to_host_pid(pc, monkeypatch, tmp_path):
    """env(둘 다)·local.conf session= 모두 없음 → `<host>-<pid>` (4층 폴백)."""
    import os
    import socket
    _bind_tmp_repo(pc, monkeypatch, tmp_path)  # local.conf 없음
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    assert pc._default_session() == f"{socket.gethostname()}-{os.getpid()}"


def test_local_conf_session_ignores_comments_and_blanks(pc, monkeypatch, tmp_path):
    """헬퍼가 `#` 주석/빈 줄/무관 키 무시하고 session= 만 집는다(board.local_config 동형)."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    conf.write_text("# c\n\nprefix=PAY\nsession=bar\n", encoding="utf-8")
    assert pc._local_conf_session() == "bar"


def test_local_conf_session_absent_returns_none(pc, monkeypatch, tmp_path):
    """local.conf 부재 → None (OSError 폴백)."""
    _bind_tmp_repo(pc, monkeypatch, tmp_path)
    assert pc._local_conf_session() is None


# ════════════════════════════════════════════════════════════════════════
# repo add --test optional (T-0069) — required 아님·미지정 → areas test_cmd 빈 값
# ════════════════════════════════════════════════════════════════════════


def test_repo_add_parser_test_is_optional(pc):
    """`repo add <name> --git <url>` (--test 없이) 파싱 성공 — required 아님(T-0069)."""
    parser = pc.build_parser()
    args = parser.parse_args(["repo", "add", "svc", "--git", "git@h:me/svc.git"])
    assert args.name == "svc"
    assert args.git == "git@h:me/svc.git"
    assert args.test is None  # 미지정 → None 기본값


def test_repo_add_without_test_registers_empty_test_cmd(pc, tmp_path):
    """--test 미지정 → areas_append(test_cmd=None) (해소 체인이 슬롯/local.conf 로 폴백·T-0066)."""
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    rc = pc.cmd_repo_add(
        argparse.Namespace(name="svc", git="git@h:me/svc.git", test=None, owner="me"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert len(board.append_calls) == 1
    assert board.append_calls[0]["test_cmd"] is None  # 빈 값 — board 가 "" 로 떨군다(폴백)


def test_repo_add_with_test_still_forwards_test_cmd(pc, tmp_path):
    """--test 지정 경로 보존 — areas_append(test_cmd=<cmd>) 그대로(현행 회귀 0)."""
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    rc = pc.cmd_repo_add(
        argparse.Namespace(name="svc", git="git@h:me/svc.git", test="pytest -q", owner="me"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert board.append_calls[0]["test_cmd"] == "pytest -q"


def test_main_repo_add_without_test_routes_and_registers(pc, monkeypatch, tmp_path):
    """main(["repo","add","svc","--git",...]) (--test 없이) → board.areas_append(test_cmd=None) 라우팅."""
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: board if name == "board" else None)
    monkeypatch.setattr(pc, "_real_clone_runner", lambda: gitr)
    monkeypatch.setattr(pc, "REPOS_DIR", tmp_path / ".repos")
    rc = pc.main(["repo", "add", "svc", "--git", "git@h:me/svc.git"])
    assert rc == 0
    assert board.append_calls[0]["test_cmd"] is None


# ════════════════════════════════════════════════════════════════════════
# worktree add 빌드명령 프롬프트 (T-0069)
#   --test 지정 → 그 값 · --test 미지정 + tty → 프롬프트 · 비-tty → None.
#   input_fn/is_tty 주입으로 hermetic(라이브 input·실 tty 없이 분기 검증).
# ════════════════════════════════════════════════════════════════════════


def test_worktree_add_test_flag_skips_prompt(pc):
    """--test 명시 → 프롬프트 생략·그 값으로 create_slot (tty 여부 무관)."""
    wp = FakeWorktreePool()
    prompted = []

    def fake_input(prompt):
        prompted.append(prompt)
        return "should-not-be-used"

    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test="ctest -R hil2"),
        worktree_pool=wp, input_fn=fake_input, is_tty=lambda: True,
    )
    assert rc == 0
    assert prompted == []  # --test 있으면 프롬프트 안 함
    assert ("create_slot", "svc", "ctest -R hil2", None) in wp.calls


def test_worktree_add_no_test_tty_prompts_for_build_cmd(pc, monkeypatch):
    """--test 미지정 + tty → 빌드명령 프롬프트 → 그 값으로 create_slot(test_cmd=)."""
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_resolve_repo_test_cmd", lambda repo, **kw: "pytest -q")
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None),
        worktree_pool=wp, input_fn=lambda prompt: "make hil3", is_tty=lambda: True,
    )
    assert rc == 0
    assert ("create_slot", "svc", "make hil3", None) in wp.calls   # 비어있지 않은 입력만 바인딩


def test_worktree_add_prompt_empty_input_binds_none(pc, monkeypatch):
    """프롬프트 빈입력(Enter) → create_slot(test_cmd=None)(슬롯 미바인딩·must-fix 1·codex).

    슬롯 test_cmd 는 board 해소 체인서 areas per-repo test_cmd 보다 우선(T-0066)이라, 빈입력에
    기본값을 박으면 areas 의 그 repo 명령(예 go test)을 잘못 덮는다 → 빈입력은 None 이어야
    해소 체인이 areas/local.conf 로 폴백(기존 동작 보존). 비어있지 않은 입력만 바인딩.
    """
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_resolve_repo_test_cmd", lambda repo, **kw: "pytest -q")
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None),
        worktree_pool=wp, input_fn=lambda prompt: "", is_tty=lambda: True,
    )
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls          # 빈입력 → None(슬롯 미바인딩)
    # 기본값(pytest -q)이 슬롯에 잘못 박히지 않았는지 — areas 폴백 보존의 핵심.
    assert ("create_slot", "svc", "pytest -q", None) not in wp.calls


def test_worktree_add_no_test_non_tty_skips_prompt(pc):
    """--test 미지정 + 비-tty(CI/파이프) → 프롬프트 생략·create_slot(test_cmd=None)."""
    wp = FakeWorktreePool()
    prompted = []

    def fake_input(prompt):
        prompted.append(prompt)
        return "x"

    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None),
        worktree_pool=wp, input_fn=fake_input, is_tty=lambda: False,
    )
    assert rc == 0
    assert prompted == []                                   # 비-tty → 프롬프트 안 함
    assert ("create_slot", "svc", None, None) in wp.calls         # 현행 None(폴백)


class _FakeBoardAreasRow:
    """board 대역 — _areas_row_for_prefix(prefix) 만 제공(test_cmd resolve 검증용)."""

    def __init__(self, *, rows=None):
        self._rows = rows or {}   # prefix → row dict

    def _areas_row_for_prefix(self, prefix):
        return self._rows.get(prefix)


def test_resolve_repo_test_cmd_prefers_areas(pc, monkeypatch):
    """_resolve_repo_test_cmd: areas per-repo test_cmd 가 있으면 그것(go test 등·1순위)."""
    board = _FakeBoardAreasRow(rows={"svc": {"test_cmd": "go test ./..."}})
    monkeypatch.setattr(pc, "_default_test_cmd", lambda: "pytest -q")
    assert pc._resolve_repo_test_cmd("svc", board=board) == "go test ./..."


def test_resolve_repo_test_cmd_falls_back_to_local_conf(pc, monkeypatch):
    """areas 미등록/빈 test_cmd → _default_test_cmd(local.conf 또는 pytest -q)로 폴백."""
    board = _FakeBoardAreasRow(rows={"svc": {"test_cmd": ""}})   # 빈 → 폴백
    monkeypatch.setattr(pc, "_default_test_cmd", lambda: "ctest")
    assert pc._resolve_repo_test_cmd("svc", board=board) == "ctest"
    # 미등록 repo 도 폴백.
    assert pc._resolve_repo_test_cmd("absent", board=board) == "ctest"


def test_resolve_repo_test_cmd_no_board_falls_back(pc, monkeypatch):
    """board 부재(파서 없음) → 솔로 폴백만(크래시 0)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    monkeypatch.setattr(pc, "_default_test_cmd", lambda: "pytest -q")
    assert pc._resolve_repo_test_cmd("svc", board=None) == "pytest -q"


def test_worktree_add_prompt_displays_resolved_default(pc, monkeypatch):
    """프롬프트 표시값 = 그 repo 의 areas→local.conf→pytest-q resolve (Enter 적용값 투명화)."""
    wp = FakeWorktreePool()
    board = _FakeBoardAreasRow(rows={"svc": {"test_cmd": "go test ./..."}})
    prompts = []
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None),
        worktree_pool=wp, board=board,
        input_fn=lambda prompt: prompts.append(prompt) or "",   # 빈입력
        is_tty=lambda: True,
    )
    assert rc == 0
    # 프롬프트에 그 repo 의 areas 폴백값(go test)이 표시 — Enter 시 적용될 값.
    assert any("go test ./..." in p for p in prompts)
    # 빈입력이라 슬롯엔 안 박힘(must-fix 1·areas 폴백 보존).
    assert ("create_slot", "svc", None, None) in wp.calls


def test_worktree_add_empty_input_does_not_override_areas(pc, monkeypatch):
    """areas 에 repo test_cmd 있어도 빈입력은 그걸 슬롯에 박지 않음(해소 체인 보존·must-fix 1)."""
    wp = FakeWorktreePool()
    board = _FakeBoardAreasRow(rows={"svc": {"test_cmd": "go test ./..."}})
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None),
        worktree_pool=wp, board=board, input_fn=lambda prompt: "", is_tty=lambda: True,
    )
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls               # 슬롯 미바인딩
    assert ("create_slot", "svc", "go test ./...", None) not in wp.calls  # areas 안 덮음


def test_worktree_add_prompt_eof_falls_back_none(pc, monkeypatch):
    """프롬프트 중 EOFError(EOF) → None 폴백(크래시 0·create_slot 진행)."""
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_resolve_repo_test_cmd", lambda repo, **kw: "pytest -q")

    def boom(prompt):
        raise EOFError()

    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None),
        worktree_pool=wp, input_fn=boom, is_tty=lambda: True,
    )
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls         # EOF → None 폴백


def test_worktree_add_prompt_keyboardinterrupt_falls_back_none(pc, monkeypatch):
    """프롬프트 중 KeyboardInterrupt(Ctrl-C) → None 폴백(크래시 0)."""
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_resolve_repo_test_cmd", lambda repo, **kw: "pytest -q")

    def boom(prompt):
        raise KeyboardInterrupt()

    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None),
        worktree_pool=wp, input_fn=boom, is_tty=lambda: True,
    )
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls


# ════════════════════════════════════════════════════════════════════════
# cmd_set_test_cmd 배선 (T-0069) — worktree_pool.set_test_cmd 위임·slot 부재 에러
# ════════════════════════════════════════════════════════════════════════


def test_set_test_cmd_calls_engine(pc, capsys):
    """cmd_set_test_cmd(slot, cmd) → worktree_pool.set_test_cmd(slot, cmd) 위임 + surface."""
    wp = FakeWorktreePool()
    rc = pc.cmd_set_test_cmd("work/svc_1", "ctest -R hil", worktree_pool=wp)
    assert rc == 0
    assert ("set_test_cmd", "work/svc_1", "ctest -R hil") in wp.calls
    out = capsys.readouterr().out
    assert "work/svc_1" in out and "ctest -R hil" in out


def test_set_test_cmd_empty_normalizes_to_none(pc, capsys):
    """빈/공백 cmd → None 으로 정규화(바인딩 해제) 후 set_test_cmd(None) 위임."""
    wp = FakeWorktreePool()
    rc = pc.cmd_set_test_cmd("work/svc_1", "   ", worktree_pool=wp)
    assert rc == 0
    assert ("set_test_cmd", "work/svc_1", None) in wp.calls
    assert "해제" in capsys.readouterr().out


def test_set_test_cmd_missing_slot_errors(pc, capsys):
    """set_test_cmd 가 KeyError(슬롯 부재)면 rc 1 + 명시 안내(침묵 무력화 금지)."""
    wp = FakeWorktreePool(set_test_raises=KeyError)
    rc = pc.cmd_set_test_cmd("work/gone_9", "cmd", worktree_pool=wp)
    assert rc == 1
    assert "리스가 없다" in capsys.readouterr().err


def test_set_test_cmd_engine_missing_errors(pc, monkeypatch, capsys):
    """worktree_pool 부재 → 명시 에러 rc 1(엔진 로드 실패 격리)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_set_test_cmd("work/svc_1", "cmd")
    assert rc == 1
    assert "worktree_pool.py 엔진을 찾을 수 없다" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# 무인자 분기 (T-0069) — tty → 콘솔(run_console) · 비-tty → help(현행 계약 보존)
# ════════════════════════════════════════════════════════════════════════


def test_main_no_args_tty_enters_console(pc, monkeypatch):
    """무인자 + tty → run_console 진입(help 아님). run_console 을 stub 해 진입만 관찰."""
    called = {}

    def fake_console():
        called["console"] = True
        return 0

    monkeypatch.setattr(pc, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(pc, "run_console", fake_console)
    rc = pc.main([])
    assert rc == 0
    assert called.get("console") is True


def test_main_no_args_non_tty_shows_help(pc, monkeypatch, capsys):
    """무인자 + 비-tty(파이프/CI) → 현행 help(rc 1) — 콘솔로 안 멈춘다(기존 계약 보존)."""
    console_called = {}
    monkeypatch.setattr(pc, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(pc, "run_console",
                        lambda: console_called.setdefault("c", True) or 0)
    rc = pc.main([])
    assert rc == 1
    assert "c" not in console_called   # 콘솔 진입 안 함
    out = capsys.readouterr().out
    for sub in ("init", "repo", "worktree", "status", "release", "update"):
        assert sub in out  # help surface 보존


def test_main_subcommand_with_tty_does_not_enter_console(pc, monkeypatch):
    """서브커맨드를 주면 tty 라도 콘솔 미진입 — 커맨드형 경로 그대로(동작 0 변경)."""
    wp = FakeWorktreePool(leases=[])
    monkeypatch.setattr(pc, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(pc, "run_console",
                        lambda: (_ for _ in ()).throw(AssertionError("콘솔 진입하면 안 됨")))
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["status"])   # 서브커맨드 → CLI 경로
    assert rc == 0
    assert wp.did("list_leases")
