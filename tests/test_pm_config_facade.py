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
import contextlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_board():
    spec = importlib.util.spec_from_file_location("board_for_pm_config_test", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _marked_skew(message="injected engine rev skew"):
    exc = RuntimeError(message)
    exc._engine_rev_skew = True
    return exc


@pytest.fixture(scope="module")
def pc():
    return _load_pm_config()


# ── 주입형 엔진 fake (DI seam — hermetic) ────────────────────────────────────


class FakeLease:
    """worktree_pool.Lease 의 최소 대역 — 배선 검증에 필요한 필드만."""

    def __init__(self, slot, repo, branch=None, session="s1", pid=1, state="leased",
                 test_cmd=None, role="work"):
        self.slot = slot
        self.repo = repo
        self.branch = branch
        self.session = session
        self.pid = pid
        self.state = state
        self.test_cmd = test_cmd  # T-0066 — 슬롯 바인딩 회귀명령(파사드 print surface)
        self.role = role          # ⑬·T-0358 — work | readonly (cmd_status role surface)


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

    class NotTaskOwner(Exception):
        # release --task 소유검사 거부 대역 (T-0354·F3) — slot/task/holder 실어 디스패처가
        # 진단 메시지를 짜는 경로를 검증한다.
        def __init__(self, slot, task="", holder=""):
            self.slot = slot
            self.task = task
            self.holder = holder
            super().__init__(slot)

    class BareRepoMissing(RuntimeError):
        def __init__(self, repo="r", *a, **k):
            self.repo = repo
            super().__init__(repo)

    class CheckoutFailed(Exception):
        def __init__(self, slot="work/r_1", *a, **k):
            self.slot = slot
            super().__init__(slot)

    class InvalidTaskName(Exception):
        # 부적합 task 명 대역 (T-0354 must-fix ②) — name/reason 실어 CLI 진단 경로 검증.
        def __init__(self, name, reason="부적합"):
            self.name = name
            self.reason = reason
            super().__init__(name)

    class ReadonlySlotNotLeasable(RuntimeError):
        # readonly 공유 슬롯 lease-op 거부 대역 (⑬·T-0358·should-fix) — release/force_release 가
        # readonly 를 가리키면 이 예외로 rc1 surface 하는 CLI 경로를 검증.
        def __init__(self, slot="work/r_1", op="release"):
            self.slot = slot
            self.op = op
            super().__init__(f"{slot} readonly {op}")

    def __init__(self, *, leases=None, release_raises=None, force_returns="present",
                 set_test_raises=None, live_branches=None, reconcile=None,
                 alloc_returns=None, alloc_raises=None, task_record="__unset__",
                 end_task_result=None, validate_raises=False,
                 set_prefix_result="__unset__", tasks=None, slot_git=None):
        self.leases = leases or []
        self.calls: list[tuple] = []
        self._release_raises = release_raises   # 예외 클래스 또는 None
        self._force_returns = force_returns     # "present" → Lease, "absent" → None
        self._set_test_raises = set_test_raises  # set_test_cmd 가 던질 예외 클래스 또는 None
        # alloc(T-0354) — 반환할 FakeLease(또는 slot 문자열) / 던질 예외 클래스.
        self._alloc_returns = alloc_returns
        self._alloc_raises = alloc_raises
        # _validate_task_name(T-0354 must-fix ②) — True 면 InvalidTaskName 을 던진다(불법명 배선).
        self._validate_raises = validate_raises
        # find_task(T-0354) — task 레코드(prefix 접근용 SimpleNamespace) 또는 None. "__unset__"
        # 기본은 prefix=None 레코드로 해소(대다수 테스트가 prefix 무관).
        self._task_record = task_record
        # end_task(T-0354) — 반환할 EndTaskResult 대역(SimpleNamespace).
        self._end_task_result = end_task_result
        # set_task_prefix(T-0357·F5) — 반환할 갱신 Task 대역 또는 None(task 부재). "__unset__"
        # 기본은 인자(name, prefix) 를 실은 SimpleNamespace 로 해소(대다수 테스트가 성공 경로).
        self._set_prefix_result = set_prefix_result
        # slot → live 브랜치 매핑(ADR-0013 amend T-0072 — cmd_status 가 lease.branch 대신
        # current_branch(slot) 로 슬롯 git HEAD 를 live 조회). 미지정 슬롯은 None(detached).
        self._live_branches = live_branches or {}
        # reconcile 결과(T-0295·cmd_status drift surface) — (orphans, stale, incomplete) 튜플
        # 또는 None(=drift 없음·기본). 기존 status 테스트는 미지정 → 빈 결과라 drift 절 미출력(무영향).
        self._reconcile = reconcile
        # task 축(T-0361 cockpit·T-0353/0354/0357) — list_tasks 가 돌려줄 Task 대역 리스트.
        # 미지정 → [] (task 없음 절). 각 Task 대역은 name·prefix 필드(SimpleNamespace).
        self._tasks = tasks or []
        # slot → slot_git_status dict(T-0361·§F8·T-0350). 미지정 슬롯은 _live_branches 기반
        # 최소 dict(base 미기록·behind None)로 합성해 git 요약 배선을 fail-soft 로 친다.
        self._slot_git = slot_git or {}

    def create_slot(self, repo, *, base=None, test_cmd=None, readonly=False, owner_task=None):
        # base (T-0075) — areas 의 그 repo base 를 cmd_worktree_add 가 전달한다(슬롯 브랜치
        # 파생 base). 호출 인자에 base 를 함께 기록해 배선 검증. (4-tuple 유지 — 기존 assertion 무영향.)
        self.calls.append(("create_slot", repo, test_cmd, base))
        # readonly (⑬·T-0358) — --readonly 플래그 전달 배선을 별도 속성으로 기록(4-tuple 불변).
        self.last_readonly = readonly
        # owner_task (ⓓB·ADR-0068) — --task 명의 생성 직결 배선을 별도 속성으로 기록(tuple 불변).
        self.last_create_owner_task = owner_task
        role = "readonly" if readonly else "work"
        # task-명의 생성이면 그 task session 으로 leased(생성분 직결·bound). 미지정=기본 세션.
        session = owner_task if owner_task is not None else "s1"
        return FakeLease(slot=f"work/{repo}_1", repo=repo, test_cmd=None if readonly else test_cmd,
                         role=role, session=session)

    def install_protected_hook(self, repo, protected, *, gate_mode, test_cmd):
        # 보호 브랜치 pre-push 훅 (재)설치 대역 (T-0076) — repo·protected 목록을 기록해
        # repo add/worktree add 가 보호 훅 설치를 호출하는 배선을 결정적으로 검증한다.
        # True 반환(bare 존재 시 설치 성공·실 install_protected_hook 계약과 동형).
        self.calls.append(("install_protected_hook", repo, list(protected)))
        self.last_protected_gate = (gate_mode, test_cmd)
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

    def list_tasks(self):
        # task 축 대역(T-0361 cockpit·T-0353) — 명명 task 레코드 리스트. 미지정 → [].
        self.calls.append(("list_tasks",))
        return self._tasks

    def slots_for_task(self, name):
        # task 명의 leased 슬롯 대역(T-0361·T-0354) — session==name 인 leased 슬롯(장부 진실 동형).
        self.calls.append(("slots_for_task", name))
        return [l for l in self.leases
                if getattr(l, "state", None) == "leased" and l.session == name]

    def slot_git_status(self, slot, *, git_runner=None):
        # 슬롯 git 요약 대역(T-0361·§F8·T-0350) — 명시 dict 우선, 없으면 live_branches 기반
        # 최소 dict(base 미기록·behind None)로 합성한다. 실 slot_git_status 반환 shape 동형:
        # slot·base({branch,commit}|None)·branch·head·behind(int|None)·behind_reason(str|None).
        self.calls.append(("slot_git_status", slot))
        if slot in self._slot_git:
            return self._slot_git[slot]
        return {"slot": slot, "base": None, "branch": self._live_branches.get(slot),
                "head": None, "behind": None,
                "behind_reason": "기준점 미기록 — `set-base` 로 지정"}

    def reconcile_worktrees(self, *, git_runner=None):
        # git worktree × 장부 정합 대역(T-0295) — cmd_status 가 drift 를 surface 하는 배선을
        # 결정적으로 친다. reconcile 미지정이면 빈 결과(drift 없음·기존 status 테스트 무영향).
        self.calls.append(("reconcile_worktrees",))
        orphans, stale, incomplete = self._reconcile or ([], [], [])
        return SimpleNamespace(orphans=orphans, stale=stale, incomplete=incomplete)

    def release(self, slot, *, owner_task=None, require_clean=True, git_runner=None):
        self.calls.append(("release", slot, owner_task))
        if self._release_raises is not None:
            raise self._release_raises(slot)
        return FakeLease(slot=slot, repo="r", state="idle")

    def force_release(self, slot):
        self.calls.append(("force_release", slot))
        if self._force_returns == "absent":
            return None
        return FakeLease(slot=slot, repo="r", state="idle")

    def alloc(self, repo, *, session=None, owner_task=None, branch=None, resume=None, git_runner=None):
        # task-명의 alloc(ADR-0068 I3)은 owner_task 로 온다 — 유효 세션(lease.session)은 task 이름.
        # 기존 배선 assertion(`("alloc", repo, "job")`)이 유지되게 유효 세션을 기록하고, owner_task
        # 원값은 별도 속성으로 남겨 I3 경로(멱등 폐기·bound)를 검증한다.
        eff_session = owner_task if owner_task is not None else session
        self.calls.append(("alloc", repo, eff_session))
        self.last_alloc_owner_task = owner_task
        if self._alloc_raises is not None:
            raise self._alloc_raises(repo)
        if self._alloc_returns is not None:
            return self._alloc_returns
        return FakeLease(slot=f"work/{repo}_1", repo=repo, state="leased")

    def _validate_task_name(self, name, registered_repos=None):
        # 공유 엔진 validator 대역 (T-0354 must-fix ②) — registered_repos 전달 배선을 기록.
        self.calls.append(("_validate_task_name", name, registered_repos))
        if self._validate_raises:
            raise FakeWorktreePool.InvalidTaskName(name, "테스트 거부")
        # 예약패턴(`<repo>_<N>`·⑥) — registered_repos 가 전달됐을 때만 판별(실 엔진 계약 동형).
        # board 미해소로 registered_repos=None 이면 이 검증이 완화되므로(T-0398 must-fix ① 회귀 대상),
        # 예약명 거부 테스트가 registered 전달 배선을 실제로 태운다.
        import re as _re
        for repo in (registered_repos or ()):
            if _re.match(rf"^{_re.escape(repo)}_\d+$", name):
                raise FakeWorktreePool.InvalidTaskName(name, f"슬롯 예약패턴 {repo}_<N>")

    def find_task(self, name):
        self.calls.append(("find_task", name))
        if self._task_record == "__unset__":
            return SimpleNamespace(name=name, prefix=None)
        return self._task_record

    def end_task(self, name, *, git_runner=None):
        self.calls.append(("end_task", name))
        if self._end_task_result is not None:
            return self._end_task_result
        return SimpleNamespace(name=name, released=[], dirty=[], refused=False,
                               moved_from=None, moved_to=None)

    def set_task_prefix(self, name, prefix):
        # task prefix 지정/변경/해제 write 백엔드 대역 (T-0357·F5) — (name, prefix) 를 기록.
        # "__unset__" 기본은 갱신 Task(prefix 반영) 로 해소, None 은 task 부재(rc1 경로) 모델.
        self.calls.append(("set_task_prefix", name, prefix))
        if self._set_prefix_result == "__unset__":
            return SimpleNamespace(name=name, prefix=prefix)
        return self._set_prefix_result

    def did(self, name) -> bool:
        return any(c[0] == name for c in self.calls)


class FakeBoard:
    """board 모듈 대역 — areas_append 호출 인자를 기록·registered_prefixes 제어.

    main(argv) 도 기록한다 — `init` 위임(board.main(["init", ...]) verbatim forward·
    T-0065) 배선 검증용. main 의 rc 는 board_main_rc 로 제어(rc 전파 테스트).
    """

    def __init__(self, *, registered=(), board_main_rc=0, repo_bases=None,
                 repo_protecteds=None, repo_gits=None, task_scan=None,
                 known_prefixes=("pay",)):
        self._registered = set(registered)
        # 기존 task-prefix 테스트가 쓰는 `pay`를 4소스 중 하나에 시드한 형상. 승인 게이트
        # 자체는 실 board를 쓰는 전용 테스트가 검증하고, 이 fake는 downstream 저장 배선을 친다.
        self._known_prefixes = set(known_prefixes) | self._registered
        self.append_calls: list[tuple] = []
        self.main_argv = None
        self._board_main_rc = board_main_rc
        # scan_task_tickets 대역 결과(T-0354·⑲) — {"claimed":[...], "prefix_open":[...]} 또는
        # None(미지정 → 빈 두 축·게이트 통과). 호출 인자 기록으로 배선(user/task/prefix) 검증.
        self._task_scan = task_scan
        self.scan_calls: list[tuple] = []
        # repo → base 매핑 (T-0075) — `_resolve_repo_base` 가 board._repo_base 를 부르므로
        # worktree add 가 areas base 를 create_slot 으로 전달하는 배선을 결정적으로 친다.
        self._repo_bases = repo_bases or {}
        # repo → git URL 매핑 (T-0291) — `_resolve_clone_git_url` 이 board._areas_git_url 을
        # 부른다. `--git` 미제공 hydrate 경로(2번째 사용자)가 areas URL 로 clone 하는 배선을
        # 결정적으로 친다. 미지정 매핑은 None(areas 에 URL 없음·미등록/부분등록 폴백).
        self._repo_gits = repo_gits or {}
        # repo → protected 목록 매핑 (T-0076) — `_resolve_repo_protected` 가
        # board._repo_protected 를 부른다. 미지정 매핑은 default(main/master/develop).
        self._repo_protecteds = repo_protecteds or {}

    def registered_repos(self):
        # 멱등 재등록 판별 축(ADR-0042 후 repo 칼럼 기준·cmd_repo_add 가 이걸 부른다).
        # `registered` 는 이미 등록된 repo명 집합으로 해석한다.
        return set(self._registered)

    def registered_prefixes(self):
        return set(self._registered)

    def require_prefix_user_ack(self, prefix, user_ack, *, surface):
        for known in self._known_prefixes:
            if known.lower() == prefix.lower():
                return known
        return prefix if user_ack == prefix else None

    def invalidate_known_prefixes_cache(self):
        return None

    @contextlib.contextmanager
    def board_lock(self):
        yield

    def revalidate_prefix_user_ack(self, prefix, user_ack, expected, *, surface):
        fresh = self.require_prefix_user_ack(prefix, user_ack, surface=surface)
        return fresh if fresh == expected else None

    def areas_append(self, prefix, area, owner, *, repo=None, git=None,
                     test_cmd=None, base=None, protected=None, area_owner=None):
        self.append_calls.append(
            {"prefix": prefix, "area": area, "owner": owner,
             "repo": repo, "git": git, "test_cmd": test_cmd, "base": base,
             "protected": protected, "area_owner": area_owner}
        )

    def _repo_base(self, repo):
        # board._repo_base 대역 (T-0075) — 매핑에 없으면 None(구 스키마/솔로/미지정 폴백).
        return self._repo_bases.get(repo)

    def _areas_git_url(self, repo):
        # board._areas_git_url 대역 (T-0291) — 매핑에 없으면 None(미등록/`git` 칼럼 빔 폴백).
        # `_resolve_clone_git_url` 이 `--git` 미제공 hydrate·불일치 판정에 이 값을 쓴다.
        return self._repo_gits.get(repo)

    def _repo_protected(self, repo):
        # board._repo_protected 대역 (T-0076) — 매핑에 없으면 default(main/master/develop)
        # 폴백(`_resolve_repo_protected` 가 이 값을 install_protected_hook 으로 전달).
        return self._repo_protecteds.get(repo, ["main", "master", "develop"])

    def _validate_prefix(self, prefix):
        # board._validate_prefix 대역 (T-0357·소비 grammar 단일 진실) — 예약어 `none`·형식
        # `[A-Za-z0-9][A-Za-z0-9_]*` 위반이면 사유 문자열, 정상이면 None(ADR-0042·ADR-0055 동형).
        import re as _re
        if prefix.lower() == "none":
            return f"prefix {prefix!r} 은 예약어"
        if not _re.match(r"^[A-Za-z0-9][A-Za-z0-9_]*$", prefix):
            return f"prefix {prefix!r} 형식 위반"
        return None

    def scan_task_tickets(self, user, task, prefix=None):
        # scan_task_tickets 대역 (T-0354·⑲) — 호출 인자 기록 + 미리 정한 결과 반환.
        self.scan_calls.append((user, task, prefix))
        return self._task_scan or {"claimed": [], "prefix_open": []}

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
        # bare 실검증(T-0294·2조건) — 재사용 경로 테스트가 기본 유효 bare 로 판정되게 is-bare "true"
        # + HEAD rc0 둘 다 모델(무효 bare fail-loud 는 test_pm_config_repo_add 가 별도 runner 로).
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return 0, "true"
        if "rev-parse" in argv and "--verify" in argv and argv[-1] == "HEAD":
            return 0, "0123abc"
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
    for sub in ("init", "repo", "worktree", "status", "whoami", "release", "update", "upstream"):
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


# ── init/update forward usage prog 정합 (T-0249·ADR-0043) ────────────────────
# board.py / pm_update.py 의 main() 은 argparse prog 를 파일명으로 하드코딩(prog="board.py"/
# "pm_update.py")하고 main(argv) 에 prog 인자가 없어, pm-config 가 위임하면 usage 줄에 그
# 파일명이 새어 나온다(에이전트가 칠 실 커맨드 pm-config init/update 와 불일치·ADR-0043 부수
# CLI 위생). `_forwarded_prog` 가 위임 동안만 top-level prog 를 facade 이름으로 치환함을 검증한다.
# 아래 소수 테스트는 mock 을 주입하지 않고 **실 board/pm_update forward 를 관통**해 *실제 usage
# 출력*을 관찰한다(DoD 증거) — `--help`/인자 에러는 argparse 가 핸들러 dispatch *전*에 usage 만
# 찍고 SystemExit 하므로 board init·엔진 sync 부작용 0(안전).


def _usage_line(text: str) -> str:
    """`--help`/에러 출력에서 argparse usage 줄(prog 표기 줄)을 뽑는다.

    DoD 계약은 *usage 줄*의 prog 표기(pm-config <sub>)다 — forward 된 도구의 docstring/epilog
    본문이 자기 파일 경로를 예시로 참조하는 건(pm_update.py 직접 호출 문서) 별개 관심사(touches
    밖·argparse 가 생성하는 usage 아님). prog 는 usage 블록 첫 줄에만 나오므로 그 줄만 검사한다.
    """
    return next(l for l in text.splitlines() if l.startswith("usage:"))


def test_init_help_usage_shows_facade_prog_not_board_py(pc, capsys):
    """`init --help` usage 줄은 `pm-config init` — `board.py` leak 0 (T-0249·ADR-0043)."""
    with pytest.raises(SystemExit) as exc:
        pc.main(["init", "--help"])
    assert exc.value.code == 0
    usage = _usage_line(capsys.readouterr().out)
    assert usage.startswith("usage: pm-config init")   # facade 서브커맨드 표기(카드↔실행 정합)
    assert "board.py" not in usage                     # usage 줄 파일명 leak 0


def test_init_error_usage_no_board_py_leak(pc, capsys):
    """`init <bad flag>` 인자 에러 usage/에러 줄에 `board.py` leak 0·prog=pm-config (T-0249)."""
    with pytest.raises(SystemExit) as exc:
        pc.main(["init", "--no-such-flag"])
    assert exc.value.code == 2   # argparse 인자 에러
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "board.py" not in combined   # 파일명 leak 0(에러 경로도)
    assert "pm-config" in combined      # facade prog 로 표기


def test_update_help_usage_shows_facade_prog_not_pm_update_py(pc, capsys):
    """`update --help` usage 줄은 `pm-config update` — `pm_update.py` leak 0 (T-0249·ADR-0043).

    (usage 줄만 검사 — pm_update 의 docstring/epilog 본문은 자기 파일 경로를 직접-호출 예시로
    참조하지만 그건 argparse 가 만든 usage 가 아니라 forward 대상 도구의 문서다·touches 밖.)
    """
    with pytest.raises(SystemExit) as exc:
        pc.main(["update", "--help"])
    assert exc.value.code == 0
    usage = _usage_line(capsys.readouterr().out)
    assert usage.startswith("usage: pm-config update")   # 플랫 파서 → facade+서브 표기
    assert "pm_update.py" not in usage                   # usage 줄 파일명 leak 0


def test_update_error_usage_shows_facade_prog_not_pm_update_py(pc, capsys):
    """`update <bad flag>` 인자 에러 usage/에러 줄은 `pm-config update` — `pm_update.py` leak 0 (T-0249)."""
    with pytest.raises(SystemExit) as exc:
        pc.main(["update", "--no-such-flag"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "pm_update.py" not in combined     # 파일명 leak 0(에러 경로도)
    assert "pm-config update" in combined     # facade prog(+서브)로 표기


def test_forwarded_prog_remaps_only_mapped_and_restores(pc):
    """`_forwarded_prog` — 매핑 prog 만 치환·매핑 밖은 통과·블록 종료 후 __init__ 원복 (T-0249)."""
    original_init = argparse.ArgumentParser.__init__
    with pc._forwarded_prog({"board.py": "pm-config"}):
        # 매핑된 top-level prog → 치환.
        assert argparse.ArgumentParser(prog="board.py").prog == "pm-config"
        # 매핑 밖 prog(파생 subparser·타 도구)는 그대로 통과 — 오염 0.
        assert argparse.ArgumentParser(prog="pm-config init").prog == "pm-config init"
    # 블록 종료 후 전역 argparse 원복(누수 0).
    assert argparse.ArgumentParser.__init__ is original_init


def test_forwarded_prog_restores_on_exception(pc):
    """`_forwarded_prog` — 블록 안 예외(SystemExit 포함)에도 finally 로 __init__ 원복 (T-0249)."""
    original_init = argparse.ArgumentParser.__init__
    with pytest.raises(SystemExit):
        with pc._forwarded_prog({"board.py": "pm-config"}):
            raise SystemExit(2)
    assert argparse.ArgumentParser.__init__ is original_init


# ── upstream show/set 배선 — 검증(도달성·fail-closed) + local.conf atomic·타 키 보존 (T-0145) ──


def _load_pm_import():
    """실 pm_import 모듈 로드 — cmd_upstream 이 재사용하는 URL 안전 검증·conf set-or-replace 제공."""
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _good_upstream_runner(argv):
    """ls-remote·rev-parse·--is-inside-work-tree 전부 OK(도달/유효 checkout) 가짜 git."""
    if "ls-remote" in argv:
        return 0, "abc123\trefs/heads/main\n"
    return 0, "true\n"


def _bad_upstream_runner(argv):
    """git 호출 전부 실패(원격 부재·non-git) 가짜 git."""
    return 128, "fatal: repository not found"


def _setup_repo_conf(pc, tmp_path, text):
    """pc.REPO 를 tmp 로 핀하고 local.conf 를 만든다 — 실 worktree 오염 0."""
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True)
    conf = pm / "local.conf"
    conf.write_text(text, encoding="utf-8")
    pc.REPO = pm.parent
    return conf


def test_upstream_show_surfaces_value(pc, tmp_path, monkeypatch, capsys):
    """`upstream show` → 현재 upstream 값 + self-describing 분류 surface."""
    conf = _setup_repo_conf(pc, tmp_path, "upstream=https://github.com/x/y.git\n")
    args = argparse.Namespace(upstream_action="show")
    rc = pc.cmd_upstream(args, pm_import=_load_pm_import(), git_runner=_good_upstream_runner)
    assert rc == 0
    out = capsys.readouterr().out
    assert "https://github.com/x/y.git" in out
    assert "(url)" in out


def test_upstream_show_unregistered(pc, tmp_path, capsys):
    """upstream 미등록이면 안내(미등록) surface · rc 0."""
    _setup_repo_conf(pc, tmp_path, "session=pm\n")
    args = argparse.Namespace(upstream_action="show")
    rc = pc.cmd_upstream(args, pm_import=_load_pm_import(), git_runner=_good_upstream_runner)
    assert rc == 0
    assert "미등록" in capsys.readouterr().out


def test_upstream_set_url_valid_records_and_preserves_keys(pc, tmp_path):
    """`upstream set <url>` 도달성 통과 시 atomic 재기록 + 타 키·주석 보존(T-0145)."""
    conf = _setup_repo_conf(
        pc, tmp_path, "# header\nsession=pm\nupstream=/old\ntest_cmd=pytest\n")
    args = argparse.Namespace(
        upstream_action="set", value="https://github.com/foo/bar.git")
    rc = pc.cmd_upstream(args, pm_import=_load_pm_import(), git_runner=_good_upstream_runner)
    assert rc == 0
    text = conf.read_text(encoding="utf-8")
    assert "upstream=https://github.com/foo/bar.git" in text
    assert "upstream=/old" not in text          # 제자리 갱신(중복 아님)
    assert "session=pm" in text                 # 타 키 보존
    assert "test_cmd=pytest" in text            # 타 키 보존
    assert text.startswith("# header")          # 주석 보존


def test_upstream_set_normalizes_duplicates_and_effective_value_is_requested(
        pc, tmp_path):
    """A2 — first/last가 갈린 conf도 upstream 한 줄 + 요청 실효값으로 수렴한다."""
    conf = _setup_repo_conf(
        pc, tmp_path,
        "# header\nsession=pm\nupstream=/first\ntest_cmd=pytest -q\n"
        "upstream=/stale\nupstream=\nfooter=keep\n",
    )
    requested = "https://github.com/acme/framework.git"

    rc = pc.cmd_upstream(
        argparse.Namespace(upstream_action="set", value=requested),
        pm_import=_load_pm_import(), git_runner=_good_upstream_runner,
    )

    assert rc == 0
    text = conf.read_text(encoding="utf-8")
    assert text.count("upstream=") == 1
    assert pc._local_conf_value("upstream") == requested
    assert _load_pm_import()._parse_conf_keys(text)["upstream"] == requested
    board = _load_board()
    board.LOCAL_CONF = conf
    assert board.local_config()["upstream"] == requested
    assert "session=pm" in text and "test_cmd=pytest -q" in text and "footer=keep" in text
    assert text.startswith("# header\n")


def test_upstream_set_postcondition_mismatch_fails_loud(
        pc, tmp_path, monkeypatch, capsys):
    """A2 — 쓰기 뒤 last-wins 실효값이 요청과 다르면 성공을 출력하지 않는다."""
    _setup_repo_conf(pc, tmp_path, "upstream=/old\n")
    pm_import = _load_pm_import()
    monkeypatch.setattr(pm_import, "_parse_conf_keys", lambda _text: {"upstream": "/raced"})

    rc = pc.cmd_upstream(
        argparse.Namespace(upstream_action="set", value="https://github.com/acme/new.git"),
        pm_import=pm_import, git_runner=_good_upstream_runner,
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "실효값 불일치" in captured.err
    assert "✓ upstream 설정" not in captured.out


def test_upstream_set_recomputes_installed_gate_contract(pc, tmp_path):
    """upstream 변경 즉시 등록 repo 훅의 원자 gate contract를 중앙 resolver로 재설치한다."""
    _setup_repo_conf(pc, tmp_path, "upstream=/old\ntest_cmd=go test ./...\n")
    board = FakeBoard(
        registered=("svc",), repo_gits={"svc": "git@github.com:acme/framework.git"},
        repo_protecteds={"svc": ["main"]},
    )
    pool = FakeWorktreePool()
    args = argparse.Namespace(
        upstream_action="set", value="https://github.com/acme/framework.git")

    assert pc.cmd_upstream(
        args, pm_import=_load_pm_import(), git_runner=_good_upstream_runner,
        board=board, worktree_pool=pool,
    ) == 0
    assert pool.last_protected_gate == ("release", "go test ./...")
    assert _install_hook_call(pool) == ("install_protected_hook", "svc", ["main"])


def test_upstream_set_url_unreachable_rejected(pc, tmp_path, capsys):
    """URL 도달 불가(ls-remote 실패)면 fail-closed 거부 — 기록 안 함(T-0145)."""
    conf = _setup_repo_conf(pc, tmp_path, "upstream=/keep\n")
    args = argparse.Namespace(
        upstream_action="set", value="https://github.com/no/such.git")
    rc = pc.cmd_upstream(args, pm_import=_load_pm_import(), git_runner=_bad_upstream_runner)
    assert rc == 1
    assert "도달 불가" in capsys.readouterr().err
    assert conf.read_text(encoding="utf-8") == "upstream=/keep\n"  # 무변경


def test_upstream_set_path_valid_records(pc, tmp_path):
    """`upstream set <path>` 가 존재+git checkout 이면 기록(경로 upstream·공동개발 특수)."""
    conf = _setup_repo_conf(pc, tmp_path, "upstream=/old\n")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    args = argparse.Namespace(upstream_action="set", value=str(checkout))
    rc = pc.cmd_upstream(args, pm_import=_load_pm_import(), git_runner=_good_upstream_runner)
    assert rc == 0
    assert f"upstream={checkout}" in conf.read_text(encoding="utf-8")


def test_upstream_set_path_nonexistent_rejected(pc, tmp_path, capsys):
    """경로 upstream 이 존재하지 않으면 fail-closed 거부 — 기록 안 함(T-0145)."""
    conf = _setup_repo_conf(pc, tmp_path, "upstream=/keep\n")
    args = argparse.Namespace(
        upstream_action="set", value=str(tmp_path / "does_not_exist"))
    rc = pc.cmd_upstream(args, pm_import=_load_pm_import(), git_runner=_good_upstream_runner)
    assert rc == 1
    assert "디렉토리가 아니거나 존재하지 않음" in capsys.readouterr().err
    assert conf.read_text(encoding="utf-8") == "upstream=/keep\n"


def test_upstream_set_unsafe_value_rejected_before_network(pc, tmp_path, capsys):
    """나쁜 값(credential·leading-dash·비허용 scheme)은 도달성 검사 *전* 순수 검증에서 거부.

    git_runner 가 호출되지 않아야(네트워크 0) — 순수 검증이 1차 게이트(fail-closed·기록 안 함).
    """
    conf = _setup_repo_conf(pc, tmp_path, "upstream=/keep\n")
    called = {"n": 0}

    def runner(argv):
        called["n"] += 1
        return 0, "ok"

    args = argparse.Namespace(
        upstream_action="set", value="https://user:pass@github.com/x.git")
    rc = pc.cmd_upstream(args, pm_import=_load_pm_import(), git_runner=runner)
    assert rc == 1
    assert called["n"] == 0, "순수 검증 실패인데도 git(네트워크)이 호출됨"
    assert conf.read_text(encoding="utf-8") == "upstream=/keep\n"


def test_upstream_engine_missing_errors_isolated(pc, monkeypatch, capsys):
    """pm_import 로드 실패면 명시 에러 rc 1(침묵 무력화 금지)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    args = argparse.Namespace(upstream_action="show")
    rc = pc.cmd_upstream(args)
    assert rc == 1
    assert "pm_import.py 엔진을 찾을 수 없다" in capsys.readouterr().err


def test_upstream_no_action_surfaces_help(pc, monkeypatch):
    """`upstream` 만 주면 그룹 도움말 surface(SystemExit) — repo/worktree 동형."""
    with pytest.raises(SystemExit):
        pc.main(["upstream"])


def test_upstream_set_missing_conf_skips_network(pc, tmp_path, monkeypatch, capsys):
    """suggestion(codex): local.conf 부재면 reachability(네트워크) *전* 에 거부 — git 미호출."""
    # local.conf 를 만들지 않는다 — pc.REPO 만 tmp 로 핀.
    pc.REPO = tmp_path
    called = {"n": 0}

    def runner(argv):
        called["n"] += 1
        return 0, "ok"

    args = argparse.Namespace(
        upstream_action="set", value="https://github.com/x/y.git")
    rc = pc.cmd_upstream(args, pm_import=_load_pm_import(), git_runner=runner)
    assert rc == 1
    assert "local.conf 없음" in capsys.readouterr().err
    assert called["n"] == 0, "conf 부재인데 git(네트워크)이 호출됨(네트워크 낭비)"


# ── repo add 배선 — areas_append(per-repo) + git clone --bare ────────────────


# owner 기본값 "me" — 이 헬퍼를 쓰는 테스트는 owner *해소* 를 검증하지 않는다(base/clone/hook/
# name/routing 대상). owner 미해소 fail-loud(ADR-0040 D1)는 owner=None 을 *명시* 하는 전용
# 테스트(test_repo_add_unbound_session_owner_fail_loud_no_side_effects)가 친다.
def _repo_add_args(pc, name="svc", git="git@h:me/svc.git", test="pytest -q", owner="me",
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
    assert call["prefix"] == ""     # repo명 자동시드 폐지 — prefix 는 빈 값(ADR-0042·T-0237)
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


def test_repo_add_passes_explicit_user_as_area_owner(pc, tmp_path):
    """repo add --user → areas_append(area_owner=) 로 전달 (ADR-0033 ③·T-0161).

    area_owner 는 그 area 의 *user* 소유(`--mine` 풀 입력)다 — registrant `owner`(별개 칼럼·
    overload 금지)와 독립. 명시 `--user` 가 _default_user 폴백보다 우선임을 확증한다.
    """
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    args = argparse.Namespace(name="svc", git="git@h:me/svc.git", test="pytest -q",
                              owner="me", base=None, user="alice")
    rc = pc.cmd_repo_add(args, board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    call = board.append_calls[0]
    assert call["owner"] == "me"          # registrant(ADR-0014) — 별개
    assert call["area_owner"] == "alice"  # user 소유(T-0161·명시 --user 우선)


def test_repo_add_area_owner_falls_back_to_default_user(pc, tmp_path, monkeypatch):
    """repo add (--user 미지정) → _default_user 로 area_owner 해소 (T-0161·local.conf user=/git email).

    `_default_user` 를 hermetic stub 으로 고정해 local.conf user=/git email 폴백 경로가
    area_owner 로 흐르는 배선을 결정적으로 친다(실 git config 누출 0).
    """
    monkeypatch.setattr(pc, "_default_user", lambda: "resolved-user")
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    # owner="me"(registrant) — 이 테스트는 area_owner 폴백만 검증(owner 해소는 별개·overload 금지).
    args = argparse.Namespace(name="svc", git="git@h:me/svc.git", test="pytest -q",
                              owner="me", base=None)  # user 속성 부재 → getattr None → _default_user
    rc = pc.cmd_repo_add(args, board=board, clone_runner=gitr, repos_dir=repos)
    assert rc == 0
    assert board.append_calls[0]["area_owner"] == "resolved-user"


def test_default_user_local_conf_wins_over_git(pc, tmp_path, monkeypatch):
    """_default_user — local.conf user= 가 git email 폴백보다 우선 (T-0161·board.user_name 동형)."""
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True)
    (pm / "local.conf").write_text("user=alice\nsession=slot\n", encoding="utf-8")
    monkeypatch.setattr(pc, "REPO", tmp_path)
    monkeypatch.setattr(pc, "_git_config_email", lambda: "git@x.com")
    assert pc._default_user() == "alice"


def test_default_user_falls_back_to_git_email(pc, tmp_path, monkeypatch):
    """_default_user — local.conf user= 부재 → git config user.email 폴백 (T-0161)."""
    monkeypatch.setattr(pc, "REPO", tmp_path)   # local.conf 없음
    monkeypatch.setattr(pc, "_git_config_email", lambda: "dev@example.com")
    assert pc._default_user() == "dev@example.com"


def test_default_user_none_when_neither(pc, tmp_path, monkeypatch):
    """_default_user — local.conf user= 도 git email 도 없으면 None (graceful·T-0161)."""
    monkeypatch.setattr(pc, "REPO", tmp_path)
    monkeypatch.setattr(pc, "_git_config_email", lambda: None)
    assert pc._default_user() is None


def test_git_config_email_fail_soft_when_git_absent(pc, monkeypatch):
    """_git_config_email — git 바이너리 부재 → None (fail-soft·크래시 0·T-0161)."""
    monkeypatch.setattr(pc.shutil, "which", lambda _name: None)
    assert pc._git_config_email() is None


def test_repo_add_already_registered_bare_exists_is_noop(pc, tmp_path):
    """이미 등록 + bare 존재 → 등록 no-op rc 0(append 0·clone 0). refspec 보정만 돈다(T-0152)."""
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
    assert _clone_argv(gitr) is None  # 이미 완비 → clone 안 함(no-op)
    # 기존 bare 재사용 경로에서도 fetch refspec 보정은 멱등 수행한다(refspec-없는 과거 bare 복구·T-0152).
    assert any("config" in c and "remote.origin.fetch" in c for c in gitr.calls)


def test_repo_add_registered_bare_exists_no_git_empty_areas_is_noop(pc, tmp_path):
    """이미 등록 + bare 존재 + `--git` 미제공 + areas `git` 칼럼 빔 → rc0 no-op(자가치유·T-0291 codex).

    URL 은 clone(bare 부재)/신규 등록에만 필요 — 이 순수 no-op(refspec/tracking/보호훅 자가치유)
    경로는 URL 불요라, 레거시/부분 등록(areas `git` 빔)이어도 fail-loud 하지 않는다. `repo add` 재실행
    =훅 자가치유 경로(엔진 update 후 기존 repo 훅 획득)를 `--git` optional 계약이 깨지 않게 못박는다.
    (my-fix 전엔 `_resolve_clone_git_url` 이 부작용 앞에서 무조건 돌아 이 케이스가 rc1 로 실패했다.)
    """
    board = FakeBoard(registered=("svc",))     # 등록됨·repo_gits 없음 → _areas_git_url None(빈 칼럼)
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)     # bare 이미 있음
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", git=None),   # ← 명시적 no `--git`
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0                              # URL 불요 no-op — fail-loud 아님
    assert board.append_calls == []            # 중복 등록 안 함
    assert _clone_argv(gitr) is None           # clone 안 함(bare 존재)
    # 자가치유(refspec 보정)는 여전히 수행 — URL 불요 no-op 이 자가치유를 건너뛰지 않는다.
    assert any("config" in c and "remote.origin.fetch" in c for c in gitr.calls)


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
    argv = _clone_argv(gitr)                   # clone 재시도됨
    assert argv is not None
    assert argv[0] == "clone" and "--bare" in argv
    assert argv[-2] == "git@h:me/svc.git"
    assert argv[-1].endswith("svc.git")
    # clone 성공 직후 fetch refspec 보정도 수행한다(T-0152·origin/* remote-tracking ref).
    assert any("config" in c and "remote.origin.fetch" in c for c in gitr.calls)


# ── multi-user hydrate: `--git` optional·areas URL 참조 (T-0291) ─────────────


def test_repo_add_second_user_hydrates_from_areas_url_without_git(pc, tmp_path):
    """2번째 사용자 clone (핵심·T-0291) — 등록됨 + bare 부재 + `--git` 없음 → areas URL 로 hydrate.

    하나의 채택 폴더를 여러 사람이 clone 하면 areas.md(git-tracked)는 공유되나 `.repos/`
    (gitignore·per-clone) bare mirror 는 안 넘어온다 → 2번째 사용자는 repo 가 등록됐어도
    mirror 가 없다. URL 재제공 없이 `repo add <name>`(no `--git`) 로 areas 등록 URL 을 clone
    원으로 bare mirror 를 hydrate 한다(clone 1·rc0·append 0). clone runner fake — 실 네트워크 0.
    """
    board = FakeBoard(registered=("svc",),
                      repo_gits={"svc": "git@h:me/svc.git"})   # areas 등록 + git 칼럼 URL
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"                                # bare mirror 없음(2번째 사용자)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", git=None),              # `--git` 미제공
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    assert board.append_calls == []            # 이미 등록 → append-only(중복 등록 안 함)
    argv = _clone_argv(gitr)                    # areas URL 로 clone(hydrate)
    assert argv is not None
    assert argv[0] == "clone" and "--bare" in argv
    assert argv[-2] == "git@h:me/svc.git"       # areas.md 등록 URL 이 clone 원(재제공 없이)
    assert argv[-1].endswith("svc.git")


def test_repo_add_unregistered_without_git_fails_loud_no_side_effects(pc, tmp_path, capsys):
    """미등록 + `--git` 없음 → 명확 fail-loud·부작용 0 (T-0291).

    신규 repo 는 clone 원 URL 을 areas 에서 해소할 수 없다 → clone/등록/훅·`.repos` mkdir
    전혀 하지 않고 rc 1(어떤 부작용보다 앞에서 걸린다).
    """
    board = FakeBoard(registered=())           # 미등록
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", git=None),   # `--git` 미제공
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 1
    assert board.append_calls == []            # 등록 안 함
    assert _clone_argv(gitr) is None           # clone 안 함(부작용 0)
    assert not repos.exists()                  # `.repos` mkdir 도 안 함
    assert "--git" in capsys.readouterr().err  # URL 필수 안내


def test_repo_add_git_mismatch_prefers_areas_url_with_warning(pc, tmp_path, capsys):
    """`--git` 이 areas 등록 URL 과 다르면 → 경고 + areas 값 우선 (T-0291·등록=단일 진실).

    already-registered + bare 부재 + `--git <다른 url>` → mirror 는 areas 등록 URL 로 clone
    하고(mirror origin 은 등록과 일치해야) 불일치를 경고한다. CLI URL 은 무시(areas 우선).
    """
    board = FakeBoard(registered=("svc",),
                      repo_gits={"svc": "git@h:me/svc.git"})   # areas 등록 URL
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", git="git@h:other/svc.git"),  # 다른 URL 제공
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    argv = _clone_argv(gitr)
    assert argv is not None and argv[-2] == "git@h:me/svc.git"   # areas URL 우선(CLI 무시)
    err = capsys.readouterr().err
    assert "areas" in err.lower() and "git@h:other/svc.git" in err  # 불일치 경고 surface


def test_repo_add_git_provided_new_repo_uses_cli_url(pc, tmp_path):
    """신규 repo(`--git` 제공·미등록) 경로 무변경 — CLI URL 로 clone + 등록 (T-0291 회귀 가드)."""
    board = FakeBoard(registered=())           # 미등록(신규)
    gitr = FakeGitRecorder(rc=0)
    repos = tmp_path / ".repos"
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", git="git@h:me/svc.git"),   # CLI URL 제공
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    argv = _clone_argv(gitr)
    assert argv is not None and argv[-2] == "git@h:me/svc.git"   # CLI URL 로 clone
    assert len(board.append_calls) == 1                          # 신규 등록
    assert board.append_calls[0]["git"] == "git@h:me/svc.git"   # areas 에 CLI URL 기록


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
        # `-C <bare> rev-parse --is-bare-repository` / `--verify HEAD` — bare 실검증(T-0294·2조건).
        # 유효 bare 로 모델(is-bare "true" + HEAD rc0 둘 다).
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return 0, "true\n"
        if "rev-parse" in argv and "--verify" in argv and argv[-1] == "HEAD":
            return 0, "0123abc\n"
        # `-C <bare> symbolic-ref HEAD` — bare HEAD 해소(full ref·T-0377·동명 태그 모호성 회피).
        if "symbolic-ref" in argv:
            return 0, f"refs/heads/{self._head}\n"
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


def test_repo_add_base_default_tag_collision_pure_branch(pc, tmp_path):
    """T-0381: --base 미지정 + 기본 브랜치 = 동명 태그(full ref) → base 순수명 `v1.3.0` 기록.

    `symbolic-ref --short HEAD` 라면 동명 태그(`v1.3.0` 브랜치 == `v1.3.0` 태그) 모호성 회피로
    `heads/v1.3.0` 을 줘 잘못된 base 를 areas 에 박았다 — full ref(`symbolic-ref HEAD`) 전환으로
    항상 `refs/heads/v1.3.0` → `refs/heads/` 접두만 벗겨 순수명 `v1.3.0` (T-0377 계보·클래스 마감).
    """
    board = FakeBoard(registered=())
    gitr = _BaseAwareGit(head="v1.3.0")   # symbolic-ref HEAD → refs/heads/v1.3.0
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base=None),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert board.append_calls[0]["base"] == "v1.3.0"   # `heads/v1.3.0` 오염 아님
    # full ref 로 전환 — `--short`(모호성 접두 오염원) 미사용.
    assert not any("--short" in c for c in gitr.calls)


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
    """이미 등록 + bare 존재 → base 재해소/재등록 안 함(append-only·중복 등록 금지·T-0075).

    관측 신호 = 재등록 없음(`append_calls == []`) — base 재해소(`_resolve_base`→areas 재등록)는
    already_registered early-return 이전에 short-circuit 된다. (T-0273 이후 그 early-return
    *이전*에 `_ensure_bare_branch_tracking` 이 symbolic-ref 로 HEAD 를 읽어 tracking 을 자가치유
    하지만, 그건 base 재해소가 아니라 tracking 보정이다 — 재등록으로 이어지지 않는다.)
    """
    board = FakeBoard(registered=("svc",))
    gitr = _BaseAwareGit(head="main")
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", base=None),
        board=board, clone_runner=gitr, repos_dir=repos,
    )
    assert rc == 0
    assert board.append_calls == []   # 중복 등록 안 함(base 재해소→재등록 경로 미도달)


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


def test_repo_add_unbound_session_owner_fail_loud_no_side_effects(
        pc, tmp_path, monkeypatch, capsys):
    """미바인딩 세션 + --owner 미지정 → owner 미해소 fail-loud(rc 1)·부작용 0 (ADR-0040 D1).

    _default_session None(leased ≥2·무바인딩)을 그대로 areas_append 에 넘기면 board 가 owner 를
    문자열 "None" 으로 areas.md 에 기록(귀속 쓰기 누출) — 그 전에 clone/등록/훅 없이 차단한다
    (board.cmd_init owner required=True 와 동형). status/whoami surface 테스트가 못 잡던 *쓰기*
    경로 회귀 핀(codex must-fix).
    """
    _bind_tmp_repo(pc, monkeypatch, tmp_path)   # local.conf·장부 없음 → _default_session None
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", owner=None),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 1
    assert board.append_calls == []    # areas 등록 안 함(owner "None" 누출 0)
    assert gitr.calls == []             # clone/base 해소 git 호출 안 함(부작용 0)
    assert "owner 미해소" in capsys.readouterr().err


def test_repo_add_unbound_session_explicit_owner_passes(pc, tmp_path, monkeypatch):
    """미바인딩이라도 --owner 명시면 통과(short-circuit) — 정상 등록 (fail-loud 우회)."""
    _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)
    rc = pc.cmd_repo_add(
        _repo_add_args(pc, name="svc", owner="alice"),
        board=board, clone_runner=gitr, repos_dir=tmp_path / ".repos",
    )
    assert rc == 0
    assert board.append_calls[0]["owner"] == "alice"


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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    assert wp.did("create_slot")
    # test_cmd 미지정 → None · areas base 없음(FakeBoard 빈 매핑) → base None(현행 bare HEAD).
    assert ("create_slot", "svc", None, None) in wp.calls
    assert "work/svc_1" in capsys.readouterr().out


def test_worktree_add_readonly_flag_forwards_readonly(pc, capsys):
    """worktree add <repo> --readonly → create_slot(repo, readonly=True) (⑬·T-0358).

    readonly 공유 슬롯 — 세션 바인딩 없음(무소유)·갱신은 refresh 로만. 출력이 role=readonly 와
    refresh 안내를 surface 하고, pm-bootstrap 바인딩 다음스텝은 뜨지 않는다(무소유)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, readonly=True, user_ack="svc"), worktree_pool=wp,
        board=FakeBoard(),
    )
    assert rc == 0
    assert wp.last_readonly is True                       # --readonly → create_slot(readonly=True)
    out = capsys.readouterr().out
    assert "readonly 공유 슬롯" in out and "role=readonly" in out
    assert "refresh" in out                               # 갱신은 refresh 로만
    assert "/pm-bootstrap svc --slot" not in out          # 무소유 — 바인딩 다음스텝 없음


def test_worktree_add_default_not_readonly(pc, capsys):
    """worktree add <repo>(플래그 없음) → create_slot(readonly=False)·기존 바인딩 다음스텝(sensitivity 대조)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    assert wp.last_readonly is False
    assert "/pm-bootstrap svc --slot 1" in capsys.readouterr().out


def test_worktree_add_test_flag_forwards_test_cmd(pc, capsys):
    """worktree add <repo> --test "<cmd>" → create_slot(repo, test_cmd=cmd) (T-0066·ADR-0014 amend)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test="ctest -R hil2", user_ack="svc"), worktree_pool=wp,
        board=FakeBoard(),
    )
    assert rc == 0
    assert ("create_slot", "svc", "ctest -R hil2", None) in wp.calls
    out = capsys.readouterr().out
    assert "test_cmd 바인딩" in out and "ctest -R hil2" in out  # 사용자 surface


def test_worktree_add_success_output_shows_bootstrap_binding_next_step(pc, capsys):
    """성공 출력이 슬롯 N + `/pm-bootstrap <repo> --slot <N>` 바인딩 다음스텝을 안내 (T-0296·audit #6).

    슬롯 fs 생성만으로 끝내지 않고, 슬롯을 세션에 바인딩하는 필수 다음스텝으로 이어준다. N 은
    lease.slot(`work/<repo>_<N>`)에서 파싱한다(FakeWorktreePool → work/svc_1 → N=1). 솔로/단일
    슬롯 무인자 부트스트랩 힌트도 곁들인다.
    """
    wp = FakeWorktreePool()   # create_slot → FakeLease(slot="work/svc_1", repo="svc")
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    out = capsys.readouterr().out
    # 바인딩 다음스텝: 정확한 슬롯 번호(N=1)와 함께 pm-bootstrap 커맨드를 명시.
    assert "/pm-bootstrap svc --slot 1" in out
    assert "바인딩" in out
    # 자동바인딩 아님(정체성=대화 맥락) + 솔로 힌트.
    assert "자동 아님" in out
    assert "무인자 `/pm-bootstrap`" in out


def test_worktree_add_passes_areas_base_to_create_slot(pc, capsys):
    """worktree add <repo> → areas.md 그 repo base 를 create_slot(base=) 로 전달 (T-0075).

    areas 에 svc→develop 이 등록돼 있으면 `_resolve_repo_base` 가 board._repo_base 로 읽어
    create_slot(base="develop") 으로 넘긴다 — 슬롯 브랜치가 develop 에서 파생되게(bare HEAD 아님).
    """
    wp = FakeWorktreePool()
    board = FakeBoard(repo_bases={"svc": "develop"})
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp, board=board
    )
    assert rc == 0
    assert ("create_slot", "svc", None, "develop") in wp.calls


def test_worktree_add_no_areas_base_passes_none(pc, capsys):
    """areas 에 base 없으면(구 스키마/솔로/미지정) create_slot(base=None) — 현행 회귀 0 (T-0075)."""
    wp = FakeWorktreePool()
    board = FakeBoard(repo_bases={})   # 그 repo base 없음 → None 폴백
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp, board=board
    )
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls


def test_worktree_add_missing_test_attr_defaults_none(pc, capsys):
    """Namespace 에 test 속성이 아예 없어도 getattr 폴백으로 None(파사드 직접 호출 견고성)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(argparse.Namespace(repo="svc", user_ack="svc"), worktree_pool=wp,
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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp, board=board
    )
    assert rc == 0
    call = _install_hook_call(wp)
    assert call == ("install_protected_hook", "svc", ["main", "develop"])


def test_worktree_add_install_hook_uses_default_protected(pc, capsys):
    """areas 에 protected 미지정 → board default(main/master/develop)로 훅 설치 (T-0076)."""
    wp = FakeWorktreePool()
    board = FakeBoard(repo_protecteds={})   # 미지정 → FakeBoard._repo_protected default
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp, board=board
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
        def install_protected_hook(self, repo, protected, *, gate_mode, test_cmd):
            raise RuntimeError("boom")
    assert pc._install_protected_hook("svc", board=FakeBoard(), worktree_pool=_BoomWp()) is False


@pytest.mark.parametrize("action", ["repo-add", "worktree-add"])
def test_ordinary_hook_install_error_does_not_abort_repo_or_worktree_add(
        pc, tmp_path, capsys, action):
    """A1/B4 경계 — 일반 설치 예외는 두 생성 명령 모두 rc0·경고이며 traceback이 아니다."""
    class _BoomWp(FakeWorktreePool):
        def install_protected_hook(self, repo, protected, *, gate_mode, test_cmd):
            raise RuntimeError("ordinary install failure")

    pool = _BoomWp()
    if action == "repo-add":
        repos = tmp_path / ".repos"
        (repos / "svc.git").mkdir(parents=True)
        rc = pc.cmd_repo_add(
            _repo_add_args(pc, name="svc"),
            board=FakeBoard(registered=("svc",)), clone_runner=FakeGitRecorder(rc=0),
            repos_dir=repos, worktree_pool=pool,
        )
    else:
        rc = pc.cmd_worktree_add(
            argparse.Namespace(repo="svc", test=None, readonly=False, task=None, user_ack="svc"),
            board=FakeBoard(), worktree_pool=pool,
        )
    captured = capsys.readouterr()
    assert rc == 0
    assert "ordinary install failure" in captured.err
    assert "Traceback" not in captured.err


def test_install_protected_hook_rethrows_marked_engine_rev_skew(pc):
    """A1 — 일반 installer 오류와 달리 marked skew는 repo/worktree add 경계 밖으로 전파한다."""
    class _SkewWp:
        def install_protected_hook(self, repo, protected, *, gate_mode, test_cmd):
            raise _marked_skew()

    with pytest.raises(RuntimeError, match="engine rev skew"):
        pc._install_protected_hook("svc", board=FakeBoard(), worktree_pool=_SkewWp())


def test_protected_hook_resolution_boundaries_rethrow_marked_engine_rev_skew(
        pc, tmp_path, monkeypatch):
    """A1 — gate/install 관련 보조 fail-soft 경계도 marker를 None/default로 강등하지 않는다."""
    skew = _marked_skew()

    class _Board:
        REPO = pc.REPO

        @staticmethod
        def _parse_areas():
            raise skew

        @staticmethod
        def _areas_git_url(_repo):
            raise skew

        @staticmethod
        def _repo_protected(_repo):
            raise skew

        @staticmethod
        def _repo_base(_repo):
            raise skew

        @staticmethod
        def registered_repos():
            raise skew

    with pytest.raises(RuntimeError):
        pc._resolve_repo_test_cmd("svc", board=_Board())
    with pytest.raises(RuntimeError):
        pc._repo_registry_git("svc", board=_Board())
    with pytest.raises(RuntimeError):
        pc._resolve_repo_protected("svc", board=_Board())
    with pytest.raises(RuntimeError):
        pc._resolve_repo_base("svc", board=_Board())

    monkeypatch.setattr(
        pc, "_load_module",
        lambda *_args: SimpleNamespace(classify_upstream=lambda _value: (_ for _ in ()).throw(skew)),
    )
    with pytest.raises(RuntimeError):
        pc._classify_upstream("https://example.test/repo.git")

    class _WiringPool:
        REPO_HOOKS_DIR = tmp_path / "hooks"

        @staticmethod
        def bare_repo_path(_repo):
            raise skew

    with pytest.raises(RuntimeError):
        pc.protected_hook_wired("svc", worktree_pool=_WiringPool())
    with pytest.raises(RuntimeError):
        pc._refresh_protected_gate_contracts(board=_Board(), worktree_pool=object())


@pytest.mark.parametrize("upstream", ["~pm_user_that_cannot_exist_0524/repo", "bad\0path"])
def test_install_protected_hook_gate_resolution_error_is_fail_soft(
        pc, tmp_path, monkeypatch, upstream):
    """B4 — gate config의 expanduser/Path 오류도 등록·슬롯 생성을 깨지 않고 False로 강등한다."""
    home = tmp_path / "bad-gate-config"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"upstream={upstream}\ntest_cmd=pytest -q\n", encoding="utf-8")
    monkeypatch.setattr(pc, "REPO", home)

    class _MustNotInstall:
        def install_protected_hook(self, *args, **kwargs):
            raise AssertionError("resolver 실패 뒤 raw installer를 호출함")

    assert pc._install_protected_hook(
        "svc", board=FakeBoard(), worktree_pool=_MustNotInstall()) is False
    reason = pc.protected_hook_install_failure_reason("svc")
    assert reason is not None
    assert "RuntimeError" in reason or "ValueError" in reason
    assert (upstream in reason or "home directory" in reason
            or "embedded null byte" in reason)


# ── `~user` 확장 판정 = 플랫폼 무관 (T-0712 축 C) ────────────────────────────
# POSIX 의 `expanduser()` 는 없는 사용자를 RuntimeError 로 거절하지만 Windows 는 존재하지 않는
# 사용자도 `C:\Users\<name>` 으로 조립해 준다 — 그 형상에서만 gate resolver 가 성공해 raw
# installer 까지 갔다(5차 Windows 측정: `'RuntimeError' in 'AssertionError: resolver 실패 뒤 raw
# installer를 호출함'`). 확장 동작을 seam 에 주입해 **Linux 에서 그 분기를 태운다**.


def _windows_style_expanduser(users_root: Path):
    """Windows `Path.expanduser()` 대역 — 없는 사용자도 `<users_root>/<name>` 으로 조립한다.

    ntpath 는 `~name` 을 현재 사용자 홈의 부모에 이어 붙일 뿐 그 사용자의 실재를 보지 않는다.
    POSIX 확장기로는 이 분기가 안 서므로 확장 seam 에 주입한다(플랫폼 skip 대신 재현).
    """

    def expand(path: Path) -> Path:
        parts = path.parts
        if parts and parts[0].startswith("~"):
            name = parts[0][1:] or "current"
            return Path(users_root, name, *parts[1:])
        return path

    return expand


def _adopter_home_with_upstream(tmp_path: Path, upstream: str) -> Path:
    """`upstream=` 만 다른 PM 홈 형상 — gate resolver 입력을 한 줄로 세팅한다."""
    home = tmp_path / "adopter-home"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"upstream={upstream}\ntest_cmd=pytest -q\n", encoding="utf-8")
    return home


class _RecordingInstaller:
    """raw installer 대역 — 실제로 호출됐는지와 전달된 증거 계약을 기록한다."""

    def __init__(self):
        self.calls: list[tuple] = []

    def install_protected_hook(self, repo, protected, *, gate_mode, test_cmd):
        self.calls.append((repo, list(protected), gate_mode, test_cmd))
        return True


def test_install_protected_hook_rejects_phantom_user_home_from_windows_expanduser(
        pc, tmp_path, monkeypatch):
    """없는 사용자를 조립해 주는 확장 동작에서도 해소 실패 판정이 서고 설치로 못 넘어간다."""
    upstream = "~pm_user_that_cannot_exist_0524/repo"
    monkeypatch.setattr(pc, "REPO", _adopter_home_with_upstream(tmp_path, upstream))
    monkeypatch.setattr(pc, "_expanduser_path",
                        _windows_style_expanduser(tmp_path / "Users"))

    class _MustNotInstall:
        def install_protected_hook(self, *args, **kwargs):
            raise AssertionError("resolver 실패 뒤 raw installer를 호출함")

    assert pc._install_protected_hook(
        "svc", board=FakeBoard(), worktree_pool=_MustNotInstall()) is False
    reason = pc.protected_hook_install_failure_reason("svc")
    assert reason is not None
    assert "RuntimeError" in reason
    assert upstream in reason          # 어떤 입력이 안 풀렸는지 진단에 남는다


def test_install_protected_hook_keeps_existing_user_home_resolving(
        pc, tmp_path, monkeypatch):
    """같은 확장 동작이라도 **실재하는** 사용자 홈은 그대로 해소된다 — 과차단 아님."""
    users = tmp_path / "Users"
    (users / "pm_user").mkdir(parents=True)
    monkeypatch.setattr(pc, "REPO", _adopter_home_with_upstream(tmp_path, "~pm_user/repo"))
    monkeypatch.setattr(pc, "_expanduser_path", _windows_style_expanduser(users))
    installer = _RecordingInstaller()

    assert pc._install_protected_hook(
        "svc", board=FakeBoard(), worktree_pool=installer) is True
    assert pc.protected_hook_install_failure_reason("svc") is None
    assert [(repo, gate_mode) for repo, _protected, gate_mode, _cmd in installer.calls] == [
        ("svc", "self-test")]


def test_gate_resolution_failure_names_the_input_whatever_the_platform_text(
        pc, tmp_path, monkeypatch):
    """예외 문구가 플랫폼마다 달라도 진단에는 **어떤 입력이** 안 풀렸는지가 남는다.

    같은 NUL 입력이 POSIX 에선 "embedded null byte", Windows 에선 "stat: embedded null
    character in path" 다 — 문구에 기대면 한쪽에서 식별 정보가 통째로 사라진다. Windows 문구를
    주입해 그 자리를 Linux 에서 태운다.
    """
    upstream = "bad\0path"
    monkeypatch.setattr(pc, "REPO", _adopter_home_with_upstream(tmp_path, upstream))

    def _windows_null_character_failure(_path):
        raise ValueError("stat: embedded null character in path")

    monkeypatch.setattr(pc, "_expanduser_path", _windows_null_character_failure)

    class _MustNotInstall:
        def install_protected_hook(self, *args, **kwargs):
            raise AssertionError("resolver 실패 뒤 raw installer를 호출함")

    assert pc._install_protected_hook(
        "svc", board=FakeBoard(), worktree_pool=_MustNotInstall()) is False
    reason = pc.protected_hook_install_failure_reason("svc")
    assert reason is not None
    assert upstream in reason
    assert "ValueError" in reason      # 원인 예외 문구도 잃지 않는다


def test_expanded_user_path_does_not_gate_the_current_user_home(pc, tmp_path, monkeypatch):
    """`~`(현재 사용자)는 실재 검사 축이 아니다 — 분기는 `~user` 자리에만 둔다."""
    monkeypatch.setattr(pc, "_expanduser_path",
                        _windows_style_expanduser(tmp_path / "Users"))

    assert pc._expanded_user_path("~/work/repo") == (
        tmp_path / "Users" / "current" / "work" / "repo")


def test_protected_push_gate_config_keeps_framework_self_repo_release_gate(
        pc, tmp_path, monkeypatch):
    """upstream이 이 PM 홈의 canonical repo 슬롯이면 기존 release gate를 그대로 선택한다."""
    home = tmp_path / "pm-home"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        f"upstream={home / 'work' / 'svc_2'}\ntest_cmd=python -m pytest tests/ -q\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "REPO", home)

    assert pc._protected_push_gate_config("svc", board=object()) == (
        "release", "python -m pytest tests/ -q")


def test_protected_push_gate_config_routes_adopter_to_repo_test_cmd(
        pc, tmp_path, monkeypatch):
    """외부 framework upstream을 쓰는 adopter는 areas의 자기 test_cmd를 선택한다."""
    home = tmp_path / "adopter-home"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        f"upstream={tmp_path / 'framework-checkout'}\ntest_cmd=wrong-global-command\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "REPO", home)

    class _Board:
        @staticmethod
        def _parse_areas():
            return ["repo", "test_cmd"], [
                {"repo": "svc", "test_cmd": "go test ./..."},
            ]

    assert pc._protected_push_gate_config("svc", board=_Board()) == (
        "self-test", "go test ./...")


def test_protected_push_gate_config_url_identity_keeps_framework_release(
        pc, tmp_path, monkeypatch):
    """URL upstream도 areas repo remote와 같은 identity면 framework release gate다."""
    home = tmp_path / "url-framework-home"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "upstream=https://github.com/acme/framework.git\ntest_cmd=pytest -q\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "REPO", home)
    board = FakeBoard(repo_gits={"svc": "git@github.com:acme/framework.git"})

    assert pc._protected_push_gate_config("svc", board=board) == (
        "release", "pytest -q")


@pytest.mark.parametrize(
    ("upstream", "registered"),
    [
        ("ssh://git@github.com:22/acme/framework.git",
         "https://github.com/acme/framework"),
        ("https://GITHUB.COM/acme/framework.git/",
         "git@github.com:acme/framework.git"),
        ("github.com:acme/framework.git",
         "ssh://github.com/acme/framework"),
    ],
    ids=["ssh-default-port-vs-https", "host-case-https-vs-scp", "scp-vs-ssh"],
)
def test_git_remote_identity_normalizes_legitimate_network_notations(
        pc, upstream, registered):
    """A3 identity 표 — network transport 표기/user/host case/default port/.git 차이는 합친다."""
    assert pc._git_remote_identity(upstream) == pc._git_remote_identity(registered)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("file://github.com/acme/framework.git",
         "https://github.com/acme/framework.git"),
        ("ssh://github.com:2222/acme/framework.git",
         "ssh://github.com:22/acme/framework.git"),
        ("https://github.com/acme/framework.git",
         "https://github.com/acme/other.git"),
    ],
    ids=["file-vs-network", "non-default-port", "path"],
)
def test_git_remote_identity_preserves_security_relevant_axes(pc, left, right):
    """A3 identity 표 — file transport, non-default port, path는 보존해 오합치지 않는다."""
    assert pc._git_remote_identity(left) != pc._git_remote_identity(right)


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("ssh://git@[::1]/acme/framework.git", "https://[::1]/acme/framework", True),
        ("git@[::1]:acme/framework.git", "ssh://[::1]:22/acme/framework", True),
        ("https://[::1]:443/acme/framework.git", "ssh://[::1]/acme/framework", True),
        ("file://[::1]/acme/framework.git", "https://[::1]/acme/framework.git", False),
        ("ssh://[::1]:2222/acme/framework.git", "ssh://[::1]/acme/framework.git", False),
        ("ssh://[::1]:2222/acme/framework.git", "ssh://[::1:2222]/acme/framework.git", False),
    ],
    ids=[
        "ipv6-ssh-https", "ipv6-scp-default-22", "ipv6-https-default-443",
        "ipv6-file-vs-network", "ipv6-non-default-port", "ipv6-host-port-boundary",
    ],
)
def test_git_remote_identity_ipv6_decision_table(pc, left, right, same):
    """A3 — IPv6에서도 transport/default-port/non-default-port 판정표와 host 경계가 유지된다."""
    left_identity = pc._git_remote_identity(left)
    right_identity = pc._git_remote_identity(right)
    assert (left_identity == right_identity) is same
    assert len(left_identity) == 4 and len(right_identity) == 4


def test_protected_push_gate_config_file_transport_collision_is_adopter_self_test(
        pc, tmp_path, monkeypatch):
    """A3 — file:// authority/path가 https remote 문자열과 같아도 release로 오판하지 않는다."""
    home = tmp_path / "file-transport-adopter"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "upstream=file://github.com/acme/framework.git\ntest_cmd=pytest -q\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "REPO", home)
    board = FakeBoard(repo_gits={"svc": "https://github.com/acme/framework.git"})

    assert pc._protected_push_gate_config("svc", board=board) == (
        "self-test", "pytest -q")


def test_protected_push_gate_config_scp_without_user_keeps_framework_release(
        pc, tmp_path, monkeypatch):
    """지원 SCP `host:path`도 pm_import 공용 분류를 거쳐 같은 remote면 release다."""
    home = tmp_path / "scp-framework-home"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "upstream=github.com:acme/framework.git\ntest_cmd=pytest -q\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "REPO", home)
    board = FakeBoard(repo_gits={"svc": "git@github.com:acme/framework.git"})

    assert pc._protected_push_gate_config("svc", board=board) == (
        "release", "pytest -q")


def test_protected_push_gate_config_missing_upstream_downgrades_to_self_test(
        pc, tmp_path, monkeypatch, capsys):
    """사용자 통제 밖 upstream 부재는 영구 차단 대신 증거 요구를 유지한 self-test로 강등한다."""
    home = tmp_path / "unresolved-home"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("test_cmd=pytest -q\n", encoding="utf-8")
    monkeypatch.setattr(pc, "REPO", home)

    assert pc._protected_push_gate_config("svc", board=object()) == (
        "self-test", "pytest -q")
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and "upstream 축 미해소" in err[0]
    assert "upstream을 설정" in err[0]


def test_protected_push_gate_config_url_without_registry_git_downgrades_to_self_test(
        pc, tmp_path, monkeypatch, capsys):
    """URL upstream + 구 registry의 빈 git 칼럼도 unresolved 하드 차단 없이 self-test다."""
    home = tmp_path / "url-no-registry-git"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "upstream=https://github.com/acme/framework.git\ntest_cmd=go test ./...\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "REPO", home)

    assert pc._protected_push_gate_config("svc", board=FakeBoard(repo_gits={})) == (
        "self-test", "go test ./...")
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and "areas.md git 축 미해소" in err[0]
    assert "git URL을 등록" in err[0]


def test_local_conf_value_duplicate_empty_is_last_wins_like_board(
        pc, tmp_path, monkeypatch):
    """A2 — 마지막 빈 값도 앞 값을 해제한다(board.local_config과 같은 last-wins)."""
    home = tmp_path / "local-conf-values"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("test_cmd=first\ntest_cmd=second\ntest_cmd=   \n", encoding="utf-8")
    monkeypatch.setattr(pc, "REPO", home)
    assert pc._local_conf_value("test_cmd") == ""


def test_protected_push_gate_trailing_empty_upstream_downgrades_like_upstream_show(
        pc, tmp_path, monkeypatch, capsys):
    """A2 — canonical upstream 뒤 `upstream=`는 해제이며 gate도 미해소 self-test로 본다."""
    home = tmp_path / "cleared-upstream"
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "upstream=https://github.com/acme/framework.git\n"
        "upstream=\n"
        "test_cmd=go test ./...\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "REPO", home)
    board = FakeBoard(repo_gits={"svc": "https://github.com/acme/framework.git"})

    assert pc._local_conf_value("upstream") == ""
    assert pc._protected_push_gate_config("svc", board=board) == (
        "self-test", "go test ./...")
    assert "upstream 축 미해소" in capsys.readouterr().err


def test_resolve_repo_protected_board_absent_defaults(pc):
    """_resolve_repo_protected — board 부재면 _DEFAULT_PROTECTED 폴백 (보호 기본값 보장·T-0076)."""
    assert pc._resolve_repo_protected("svc", board=object()) == ["main", "master", "develop"]


def test_worktree_add_create_failure_errors(pc, capsys):
    """create_slot 이 RuntimeError(예: worktree add 실패)면 rc 1 + 명시 에러."""
    wp = FakeWorktreePool()

    def boom(repo, *, base=None, test_cmd=None, readonly=False, owner_task=None):
        raise RuntimeError("git worktree add failed")
    wp.create_slot = boom
    rc = pc.cmd_worktree_add(argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp,
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

    def bare_missing(repo, *, base=None, test_cmd=None, readonly=False, owner_task=None):
        raise wp_mod.BareRepoMissing(repo, TOOLS.parent / ".repos" / f"{repo}.git")
    wp.create_slot = bare_missing
    rc = pc.cmd_worktree_add(argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp,
                             board=FakeBoard())
    assert rc == 1
    assert "슬롯 생성 실패" in capsys.readouterr().err


def test_worktree_add_engine_missing_errors(pc, monkeypatch, capsys):
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_worktree_add(argparse.Namespace(repo="svc", user_ack="svc"))
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


def test_status_surfaces_readonly_role(pc, monkeypatch, capsys):
    """status → readonly 슬롯은 `role=readonly` 를 surface·work 슬롯은 role 표기 없음(⑬·T-0358)."""
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", branch="feat", session="me", state="leased"),
        FakeLease(slot="work/svc_2", repo="svc", branch=None, session="",
                  state="leased", role="readonly"),
    ]
    wp = FakeWorktreePool(leases=leases)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    full = out.split("풀 전체 리스 장부")[1]
    assert "role=readonly" in full                    # readonly 슬롯 role surface
    # work 슬롯(svc_1) 줄엔 role 표기 없음(기본은 생략).
    svc1_line = next(l for l in full.splitlines() if "work/svc_1" in l)
    assert "role=" not in svc1_line


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


def test_status_unbound_session_surfaces_placeholder(pc, monkeypatch, tmp_path, capsys):
    """_default_session None(미바인딩) → cmd_status 헤더 "(비바인딩)"·남의 리스로 self-identify 안 함.

    Windows 4슬롯 홈에서 비바인딩(pm-env) 세션이 남의 세션으로 self-identify 하던 직접 증상의
    수정(ADR-0040 surface) — 세션 None 이면 헤더가 "(비바인딩)"·"이 세션의 리스: (없음)".
    """
    _bind_tmp_repo(pc, monkeypatch, tmp_path)   # local.conf·장부 없음 → _default_session None
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    wp = FakeWorktreePool(leases=[
        FakeLease(slot="work/svc_1", repo="svc", session="other", state="leased"),
    ])
    rc = pc.cmd_status(argparse.Namespace(command="whoami"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(비바인딩)" in out
    assert "이 세션의 리스: (없음)" in out   # 남의 세션(other)을 내 것으로 오식별하지 않음


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


# ── status 2축 cockpit — task 상황 + slot 풀·슬롯당 git 요약 (T-0361·§F8·결정 ⑥⑪) ──


def _git_status(*, base=None, branch=None, head=None, behind=None, behind_reason=None):
    """slot_git_status 반환 shape 대역(T-0361·§F8) — 명시 필드로 조립."""
    return {"slot": "?", "base": base, "branch": branch, "head": head,
            "behind": behind, "behind_reason": behind_reason}


def test_status_task_axis_lists_named_tasks_and_workspaces(pc, monkeypatch, capsys):
    """DoD — task 축이 명명 task 별 {보유 작업공간·prefix} 를 surface (T-0361·결정 ⑥·T-0353/0354/0357).

    task=`payjob`(prefix=PAY·슬롯 2개 보유)·`readmod`(prefix 없음·슬롯 0개). slot-모드 세션(`me`)은
    task 축에 안 올라온다(auto-task 폐기·2축 분리)."""
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", session="payjob", state="leased"),
        FakeLease(slot="work/svc_2", repo="svc", session="payjob", state="leased"),
        FakeLease(slot="work/svc_9", repo="svc", session="me", state="leased"),
    ]
    tasks = [SimpleNamespace(name="payjob", prefix="PAY"),
             SimpleNamespace(name="readmod", prefix=None)]
    wp = FakeWorktreePool(leases=leases, tasks=tasks)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    task_axis = out.split("task 상황")[1].split("slot 풀")[0]
    # payjob 은 prefix=PAY + 슬롯 2개, readmod 는 prefix 없음(기본) + 슬롯 0개.
    assert "payjob" in task_axis and "PAY" in task_axis
    assert "work/svc_1" in task_axis and "work/svc_2" in task_axis
    assert "readmod" in task_axis and "없음(기본)" in task_axis
    assert "보유 작업공간 없음" in task_axis
    # slot-모드 세션 `me` 는 task 축에 없다(2축 분리·결정 ⑥).
    assert "me" not in task_axis


def test_status_task_axis_empty_when_no_named_tasks(pc, capsys):
    """DoD — 명명 task 0 이면 task 축은 "(task 없음 …)" 안내(slot-모드 세션은 slot 풀 축에만)."""
    leases = [FakeLease(slot="work/svc_1", repo="svc", session="me", state="leased")]
    wp = FakeWorktreePool(leases=leases, tasks=[])
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "task 상황" in out
    assert "task 없음" in out


def test_status_slot_git_summary_behind_when_base_recorded(pc, monkeypatch, capsys):
    """DoD — base 기록 있으면 슬롯 git 요약 = `branch@head (base: b@sha · N behind)` (T-0361·§F8·T-0350)."""
    leases = [FakeLease(slot="work/svc_1", repo="svc", session="me", state="leased")]
    slot_git = {
        "work/svc_1": _git_status(
            base={"branch": "origin/main", "commit": "def6789012345"},
            branch="feat-x", head="abc1234567890", behind=3, behind_reason=None),
    }
    wp = FakeWorktreePool(leases=leases, slot_git=slot_git)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    # branch@head(단축 8) + base branch@commit(단축 8) + N behind.
    assert "feat-x@abc12345" in out
    assert "base: origin/main@def67890" in out
    assert "3 behind" in out
    assert ("slot_git_status", "work/svc_1") in wp.calls


def test_status_slot_git_summary_unrecorded_base_shows_reason(pc, monkeypatch, capsys):
    """DoD — base 미기록이면 behind=`-` + 이유(자동 추론 금지·결정 ⑪·침묵 추론 금지)."""
    leases = [FakeLease(slot="work/svc_1", repo="svc", session="me", state="leased")]
    slot_git = {
        "work/svc_1": _git_status(
            base=None, branch="feat-x", head="abc1234567890", behind=None,
            behind_reason="기준점 미기록 — `set-base` 로 지정"),
    }
    wp = FakeWorktreePool(leases=leases, slot_git=slot_git)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "base: -" in out
    assert "기준점 미기록" in out
    assert "set-base" in out


def test_status_slot_git_summary_base_recorded_but_unresolved(pc, monkeypatch, capsys):
    """base 는 기록됐으나 behind 계산 불가(ref 미해소/fetch 필요) → base 표시 + `- (이유)`."""
    leases = [FakeLease(slot="work/svc_1", repo="svc", session="me", state="leased")]
    slot_git = {
        "work/svc_1": _git_status(
            base={"branch": "origin/dev", "commit": "0011223344"},
            branch="feat", head="99887766", behind=None,
            behind_reason="base.branch 해소 실패(ref 부재/fetch 필요)"),
    }
    wp = FakeWorktreePool(leases=leases, slot_git=slot_git)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "base: origin/dev@00112233" in out
    assert "해소 실패" in out
    assert "behind" not in out.split("slot 풀")[1] or "- (" in out.split("slot 풀")[1]


def test_status_readonly_slot_git_shows_detached_and_role(pc, monkeypatch, capsys):
    """DoD — readonly 슬롯은 role 표시·git 요약 branch `(detached)`·base 만 의미 (T-0361·⑬·T-0358)."""
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", session="", state="leased", role="readonly"),
    ]
    slot_git = {
        "work/svc_1": _git_status(
            base={"branch": "origin/main", "commit": "aabbccdd1122"},
            branch="feat-live", head="ffee00112233", behind=0, behind_reason=None),
    }
    wp = FakeWorktreePool(leases=leases, slot_git=slot_git)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    pool = out.split("slot 풀")[1]
    assert "role=readonly" in pool
    # readonly = branch 축은 (detached)(무소유 기준면)이라 live branch 를 요약에 안 씌운다.
    assert "(detached)@" in pool
    assert "feat-live@" not in pool          # readonly 는 branch 이름을 요약에 안 실음
    assert "base: origin/main@aabbccdd" in pool


def test_status_slot_git_summary_task_attribution(pc, monkeypatch, capsys):
    """slot 풀 축의 "보유 task" — session 이 명명 task 면 그 task, 아니면 `-` (T-0361·⑥ session 축)."""
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", session="payjob", state="leased"),
        FakeLease(slot="work/svc_2", repo="svc", session="me", state="leased"),
    ]
    tasks = [SimpleNamespace(name="payjob", prefix="PAY")]
    wp = FakeWorktreePool(leases=leases, tasks=tasks)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    pool = capsys.readouterr().out.split("slot 풀")[1]
    svc1 = pool.split("work/svc_1")[1].split("work/svc_2")[0]
    svc2 = pool.split("work/svc_2")[1]
    assert "보유 task=payjob" in svc1        # session=task 명 → task 귀속
    assert "보유 task=-" in svc2             # slot-모드 세션 → task 아님


def test_status_slot_git_failure_keeps_task_axis(pc, monkeypatch, capsys):
    """must-fix — slot_git_status 가 던져도 장부 축(보유 task·role)은 유지·git 요약만 fail-soft 대체.

    slot 풀 축 명세({state·보유 task·role})는 git 요약과 분리다 — git 실패로 장부 정보(task 귀속)까지
    사라지면 silent degrade. git 실패 시 `보유 task=…` 는 그대로 출력하고 git 부분만 `(조회 불가)`."""
    class _Boom(FakeWorktreePool):
        def slot_git_status(self, slot, *, git_runner=None):
            raise RuntimeError("git blew up")

    leases = [FakeLease(slot="work/svc_1", repo="svc", session="payjob", state="leased")]
    tasks = [SimpleNamespace(name="payjob", prefix="PAY")]
    wp = _Boom(leases=leases, tasks=tasks)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "work/svc_1" in out                  # 리스 장부 축은 정상 surface
    assert "보유 task=payjob" in out             # 장부 축(task 귀속)은 git 실패에도 유지(must-fix)
    assert "(조회 불가)" in out                  # git 요약만 fail-soft 대체(크래시 0)


def test_status_idle_lease_not_attributed_to_none_task(pc, monkeypatch, capsys):
    """방어(reviewer) — name 미보유 task 레코드가 있어도 idle lease(session 빈값)가 `보유 task=None`
    으로 오귀인되지 않는다(task_names 는 truthy name 만·None 혼입 차단)."""
    leases = [FakeLease(slot="work/svc_2", repo="svc", session="", state="idle")]
    tasks = [SimpleNamespace(name=None, prefix=None)]   # name 미보유 레코드(방어 대상)
    wp = FakeWorktreePool(leases=leases, tasks=tasks)
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    pool = capsys.readouterr().out.split("slot 풀")[1]
    assert "보유 task=None" not in pool          # None 오귀인 없음
    assert "보유 task=-" in pool                  # idle/무귀속 슬롯은 `-`


# ── status reconcile — orphan/stale/incomplete drift surface (T-0295) ────────


def test_status_surfaces_orphan_drift(pc, capsys):
    """DoD(1) — reconcile 이 orphan(git worktree 존재·장부 미등록)을 status 가 flag 한다 (T-0295)."""
    orphan = SimpleNamespace(slot="work/svc_2", branch="svc_2")
    wp = FakeWorktreePool(leases=[], reconcile=([orphan], [], []))
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    assert wp.did("reconcile_worktrees")
    out = capsys.readouterr().out
    assert "[orphan] work/svc_2" in out
    assert "drift" in out
    # 조회 전용 — 복구 안내는 있으나 자동삭제 표현은 없다(사용자 위임).
    assert "자동삭제 안 함" in out


def test_status_surfaces_stale_and_incomplete_drift(pc, capsys):
    """DoD(2) — reconcile 이 stale(장부 등록·worktree 없음)·incomplete(creating)를 flag 한다 (T-0295)."""
    stale = FakeLease(slot="work/svc_9", repo="svc", session="", state="idle")
    incomplete = FakeLease(slot="work/svc_5", repo="svc", session="me", state="creating")
    wp = FakeWorktreePool(leases=[], reconcile=([], [stale], [incomplete]))
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[stale] work/svc_9" in out
    assert "[incomplete] work/svc_5" in out
    assert "creating" in out


def test_status_no_drift_omits_section(pc, monkeypatch, capsys):
    """reconcile 이 빈 결과면 drift 절을 아예 출력하지 않는다(clean 상태 무소음·기존 status 무영향)."""
    leases = [FakeLease(slot="work/svc_1", repo="svc", session="me", state="leased")]
    wp = FakeWorktreePool(leases=leases)   # reconcile 미지정 → 빈 결과
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "me")
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "drift" not in out
    assert "[orphan]" not in out and "[stale]" not in out and "[incomplete]" not in out


def test_status_reconcile_failure_does_not_break_status(pc, capsys):
    """reconcile_worktrees 가 예외를 던져도 status 는 rc0 로 끝난다(fail-soft·조회 전용·T-0295)."""
    class _Boom(FakeWorktreePool):
        def reconcile_worktrees(self, *, git_runner=None):
            raise RuntimeError("git blew up")

    wp = _Boom(leases=[FakeLease(slot="work/svc_1", repo="svc", state="idle")])
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "work/svc_1" in out          # 리스 장부는 정상 surface
    assert "drift" not in out           # reconcile 실패 → drift 절 생략(크래시 0)


def test_status_drift_guidance_recommends_prune_not_force_release(pc, capsys):
    """must-fix(1) 안내정정 — drift 복구가 위험한 `release --force`(idle 화·삭제 안 함) 대신
    `prune-stale`(안전 기록 정리) + orphan 은 `git worktree remove` 를 권한다 (T-0295)."""
    stale = FakeLease(slot="work/svc_9", repo="svc", state="idle")
    orphan = SimpleNamespace(slot="work/svc_2", branch="svc_2")
    wp = FakeWorktreePool(leases=[], reconcile=([orphan], [stale], []))
    rc = pc.cmd_status(argparse.Namespace(command="status"), worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "prune-stale" in out                 # 안전 프리미티브 안내
    assert "release --force" not in out         # 위험한 옛 안내 제거(force_release=idle 화·잔여)
    assert "worktree remove" in out             # orphan 은 사용자가 git 로


# ── worktree prune-stale 배선 — 안전 dangling cleanup (T-0295) ────────────────


def test_worktree_prune_stale_wires_to_engine(pc, capsys):
    """worktree prune-stale → worktree_pool.prune_stale_leases() 호출 + 정리 결과 surface (T-0295)."""
    class _PruneWP(FakeWorktreePool):
        def prune_stale_leases(self):
            self.calls.append(("prune_stale_leases",))
            return ["work/svc_1", "work/svc_2"]

    wp = _PruneWP()
    rc = pc.cmd_worktree_prune_stale(argparse.Namespace(), worktree_pool=wp)
    assert rc == 0
    assert wp.did("prune_stale_leases")
    out = capsys.readouterr().out
    assert "work/svc_1" in out and "work/svc_2" in out
    assert "2개" in out


def test_worktree_prune_stale_empty_is_harmless(pc, capsys):
    """정리할 dangling 엔트리 없으면 무해 안내(rc0·크래시 0)."""
    wp = FakeWorktreePool()
    wp.prune_stale_leases = lambda: []
    rc = pc.cmd_worktree_prune_stale(argparse.Namespace(), worktree_pool=wp)
    assert rc == 0
    assert "없음" in capsys.readouterr().out


def test_worktree_prune_stale_engine_missing_errors(pc, monkeypatch, capsys):
    """엔진 로드 실패 → rc1 + 안내(다른 서브커맨드 동형·크래시 0)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_worktree_prune_stale(argparse.Namespace())
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
    assert ("release", "work/svc_1", None) in wp.calls   # owner_task=None (--task 미지정·T-0354)
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


def test_release_readonly_slot_refused(pc, capsys):
    """release(비-force) 가 readonly 공유 슬롯이면 ReadonlySlotNotLeasable → rc 1 + 안내(⑬·T-0358)."""
    wp = FakeWorktreePool(release_raises=FakeWorktreePool.ReadonlySlotNotLeasable)
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", force=False), worktree_pool=wp
    )
    assert rc == 1
    assert "readonly" in capsys.readouterr().err


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
    assert ("release", "work/x_1", None) in wp.calls   # owner_task=None (--task 미지정·T-0354)


def test_main_routes_repo_add_to_engine(pc, monkeypatch, tmp_path):
    """main(["repo","add",...]) 가 cmd_repo_add → board.areas_append 로 라우팅."""
    board = FakeBoard(registered=())
    gitr = FakeGitRecorder(rc=0)

    def fake_load(name, filename):
        return board if name == "board" else None
    monkeypatch.setattr(pc, "_load_module", fake_load)
    monkeypatch.setattr(pc, "_real_clone_runner", lambda: gitr)
    monkeypatch.setattr(pc, "REPOS_DIR", tmp_path / ".repos")
    # --owner me — 라우팅 검증 테스트(owner 해소 대상 아님)라 미바인딩 fail-loud(ADR-0040)를
    # 피하려 명시한다. argparse 경로엔 _repo_add_args 헬퍼 기본값이 안 걸린다.
    rc = pc.main(["repo", "add", "svc", "--git", "git@h:me/svc.git", "--test", "pytest -q",
                  "--owner", "me"])
    assert rc == 0
    assert len(board.append_calls) == 1
    assert board.append_calls[0]["repo"] == "svc"


def test_main_routes_worktree_add_to_engine(pc, monkeypatch):
    """main(["worktree","add","svc"]) 가 cmd_worktree_add → create_slot(test_cmd=None) 로 라우팅."""
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["worktree", "add", "svc", "--user-ack", "svc"])
    assert rc == 0
    assert ("create_slot", "svc", None, None) in wp.calls   # --test 없음 → None(현행 하위호환)


def test_main_worktree_add_marked_skew_translates_at_cli_terminal(pc, monkeypatch, capsys):
    """create_slot 의 marked skew 는 핸들러가 rc 로 번역하지 않고 재전파 → main 이 번역 (T-0545 ②).

    핸들러 rc 번역은 rc 를 읽지 않는 surface(콘솔 `[w]`)에서 종료 경계가 아니었다. 재전파로
    돌려도 CLI 표면은 동일하다 — 같은 안내 문구·rc 1·traceback 0(main 이 같은 헬퍼로 번역).
    """
    class _SkewPool(FakeWorktreePool):
        def create_slot(self, *args, **kwargs):
            raise _marked_skew("worktree_pool.py 사본 불일치")

    pool = _SkewPool()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: pool if name == "worktree_pool" else None)
    rc = pc.main(["worktree", "add", "svc", "--test", "pytest -q", "--user-ack", "svc"])
    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith("[중단] 엔진 사본 불일치")
    assert "pm-update" in err
    assert "Traceback" not in err


def test_main_worktree_add_test_flag_parses_and_forwards(pc, monkeypatch):
    """main(["worktree","add","svc","--test","<cmd>"]) → create_slot(svc, test_cmd=cmd) (T-0066 end-to-end 파싱).

    build_parser 의 --test 서브파서 인자 + cmd_worktree_add 배선을 한 경로로 검증 — DI mock
    wp 로 create_slot 호출의 test_cmd 인자를 관찰한다(실 worktree add 없이).
    """
    wp = FakeWorktreePool()
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: wp if name == "worktree_pool" else None)
    rc = pc.main(["worktree", "add", "svc", "--test", "make hil3", "--user-ack", "svc"])
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
# _default_session — board.session_name 과 동형 count-based 유도 (ADR-0040 D1·T-0073)
# env > lease 장부 leased 1개면 그 session(단일-lease 유도) > None. per-clone
# local.conf `session=` 층은 T-0779 가 폐지했다. cmd_status/whoami 의
# "이 세션의 리스" surface 가 이걸 쓰므로 board 매칭과 정합해야 한다(T-0066 must-fix).
# board 와 tail 만 다르다(host-pid 폴백 없음 — surface 는 미해소=None → "(비바인딩)").
# ════════════════════════════════════════════════════════════════════════

def _bind_tmp_repo(pc, monkeypatch, tmp_path):
    """pc 의 REPO 를 tmp 로 재지정 — 실 루트 local.conf/리스장부 무오염(hermetic).

    `_leased_sessions`·conf 헬퍼가 `REPO / .project_manager / …` 를 읽으므로 REPO 를
    tmp 로 묶어야 실 루트를 안 건드린다. module-scope pc 라도 monkeypatch 가 테스트 후 복원.
    """
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pc, "REPO", tmp_path)
    return pm / "local.conf"


def _write_ledger_pc(tmp_path, *rows):
    """`REPO/.project_manager/.local/worktree-leases.json` 에 리스 행을 쓴다 (유도 전제)."""
    import json
    local = tmp_path / ".project_manager" / ".local"
    local.mkdir(parents=True, exist_ok=True)
    leases = [{"slot": f"work/{r['session']}", "repo": "r",
               "session": r["session"], "state": r.get("state", "leased")}
              for r in rows]
    (local / "worktree-leases.json").write_text(json.dumps({"leases": leases}),
                                                encoding="utf-8")


def test_default_session_prefers_pm_env(pc, monkeypatch, tmp_path):
    """`$PM_SESSION_NAME` 최우선 — alias·lease·local.conf session= 무시 (ADR-0040)."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.setenv("PM_SESSION_NAME", "from-pm-env")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    conf.write_text("session=from-conf\n", encoding="utf-8")
    _write_ledger_pc(tmp_path, {"session": "leased-sess"})
    assert pc._default_session() == "from-pm-env"


def test_default_session_claude_env_is_alias(pc, monkeypatch, tmp_path):
    """`$CLAUDE_SESSION_NAME` 단독 → deprecated alias 로 조용히 동작 (back-compat)."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    conf.write_text("session=from-conf\n", encoding="utf-8")
    assert pc._default_session() == "from-alias"


def test_default_session_pm_wins_over_claude(pc, monkeypatch, tmp_path):
    """둘 다 설정 시 `PM_SESSION_NAME` 승 (마이그레이션 중 명시 우선)."""
    _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.setenv("PM_SESSION_NAME", "new")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "old")
    assert pc._default_session() == "new"


def test_default_session_single_lease_derives_session(pc, monkeypatch, tmp_path):
    """env 없음·leased 슬롯 1개 → 그 session 유도 (ADR-0040 count-based·board 동형)."""
    _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _write_ledger_pc(tmp_path, {"session": "project_manager_1"})
    assert pc._default_session() == "project_manager_1"


def test_default_session_single_lease_wins_over_local_conf(pc, monkeypatch, tmp_path):
    """단일-lease 값과 local.conf 값이 다르면 유도값(lease) 승."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    conf.write_text("session=stale-conf\n", encoding="utf-8")
    _write_ledger_pc(tmp_path, {"session": "derived-1"})
    assert pc._default_session() == "derived-1"


def test_default_session_two_leases_returns_none(pc, monkeypatch, tmp_path):
    """leased ≥2 (모호) → None (silent 오귀속 차단·ADR-0040)."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    conf.write_text("session=some-conf\n", encoding="utf-8")
    _write_ledger_pc(tmp_path, {"session": "a_1"}, {"session": "b_1"})
    assert pc._default_session() is None


def test_default_session_ignores_local_conf_session(pc, monkeypatch, tmp_path):
    """env·lease 없음 + local.conf `session=foo` → None (폴백 폐지·board 동형·T-0779)."""
    conf = _bind_tmp_repo(pc, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    conf.write_text("session=foo\n", encoding="utf-8")
    assert pc._default_session() is None


def test_default_session_solo_unbound_returns_none(pc, monkeypatch, tmp_path):
    """env·lease 모두 없음 → None (구 host-pid 폴백 제거·ADR-0040).

    surface(cmd_status/whoami·required=False)가 이 None 을 "(비바인딩)" 으로 표시한다 —
    `<host>-<pid>` 는 세션-귀속 아닌 국소 용처(worktree_pool lease 취득)에만 잔존.
    """
    _bind_tmp_repo(pc, monkeypatch, tmp_path)  # local.conf 없음·장부 없음
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    assert pc._default_session() is None


def test_no_local_conf_session_parser_remains(pc):
    """conf `session=` 자체 파서(`_local_conf_session`)가 모듈에서 사라졌다 (T-0779).

    board 를 import 하지 않는 3개 모듈이 각자 같은 키를 파싱하던 사본이 폐지 대상이었다 —
    이름이 살아 있으면 다음 호출자가 다시 그 층을 살릴 수 있으므로 부재를 못박는다.
    """
    assert not hasattr(pc, "_local_conf_session")
    assert hasattr(pc, "_local_conf_user")      # 같은 관용구의 살아 있는 키는 유지


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
    # --owner me — 라우팅 검증(owner 해소 대상 아님)·미바인딩 fail-loud(ADR-0040) 회피.
    rc = pc.main(["repo", "add", "svc", "--git", "git@h:me/svc.git", "--owner", "me"])
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
        argparse.Namespace(repo="svc", test="ctest -R hil2", user_ack="svc"),
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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"),
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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"),
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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"),
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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"),
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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"),
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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"),
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
        argparse.Namespace(repo="svc", test=None, user_ack="svc"),
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


# ════════════════════════════════════════════════════════════════════════
# alloc <repo> --task <이름> (F2·§F2b·⑤·T-0354)
# ════════════════════════════════════════════════════════════════════════


def test_alloc_success_leases_under_task_session(pc, capsys):
    """alloc <repo> --task <이름>(기바인딩) → worktree_pool.alloc(repo, session=task) 로 대여·rc0."""
    wp = FakeWorktreePool(alloc_returns=FakeLease(slot="work/svc_1", repo="svc", state="leased"))
    rc = pc.cmd_alloc(
        argparse.Namespace(repo="svc", task="job"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    assert ("alloc", "svc", "job") in wp.calls    # session=task 로 대여(⑥)
    # 명 검증 + 기바인딩 확인 배선(must-fix ②).
    assert wp.did("_validate_task_name") and wp.did("find_task")
    out = capsys.readouterr().out
    assert "work/svc_1" in out and "job" in out


def test_alloc_validates_task_name(pc, capsys):
    """불법/예약 task 명 → 엔진 validator InvalidTaskName → rc1(대여 시도 안 함·must-fix ②)."""
    wp = FakeWorktreePool(validate_raises=True)
    rc = pc.cmd_alloc(
        argparse.Namespace(repo="svc", task="../evil"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    assert not wp.did("alloc") and not wp.did("find_task")   # 검증이 alloc/find_task 이전


def test_alloc_forwards_registered_repos_for_reserved_check(pc):
    """검증에 등록 repo 목록 전달 — 예약패턴(`<repo>_<N>`·⑥) 판별 근거(must-fix ②)."""
    wp = FakeWorktreePool(alloc_returns=FakeLease(slot="work/svc_1", repo="svc", state="leased"))
    board = FakeBoard(registered=("svc", "acc"))
    pc.cmd_alloc(argparse.Namespace(repo="svc", task="job"), worktree_pool=wp, board=board)
    val = [c for c in wp.calls if c[0] == "_validate_task_name"]
    assert val and set(val[0][2]) == {"svc", "acc"}    # registered_repos 전달


def test_alloc_requires_prebound_task(pc, capsys):
    """미바인딩 task → rc1 + F1(bootstrap) 안내(정체성 생성 단일화·reviewer suggestion)."""
    wp = FakeWorktreePool(task_record=None)   # find_task → None(미바인딩)
    rc = pc.cmd_alloc(
        argparse.Namespace(repo="svc", task="job"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "pm-bootstrap" in err and "job" in err
    assert not wp.did("alloc")    # 미바인딩이면 대여 안 함


def test_alloc_pool_exhausted_requests_user_creation(pc, capsys):
    """idle 슬롯 없음(NeedsCreate) → 자동 생성 안 함·rc1 + `worktree add` 승인 요청(⑤)."""
    wp = FakeWorktreePool(alloc_raises=FakeWorktreePool.NeedsCreate)
    rc = pc.cmd_alloc(
        argparse.Namespace(repo="svc", task="job"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "worktree add" in err and "svc" in err
    # 자동 생성 안 함 — create_slot 을 부르지 않는다(⑤ 물리층=사용자 승인).
    assert not wp.did("create_slot")


def test_alloc_bare_missing_errors(pc, capsys):
    """bare 부재(BareRepoMissing) → rc1(안내 surface)."""
    wp = FakeWorktreePool(alloc_raises=FakeWorktreePool.BareRepoMissing)
    rc = pc.cmd_alloc(
        argparse.Namespace(repo="svc", task="job"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1


def test_main_routes_alloc_to_engine(pc, monkeypatch):
    """main(["alloc","svc","--task","job"]) → cmd_alloc → worktree_pool.alloc 라우팅."""
    wp = FakeWorktreePool(alloc_returns=FakeLease(slot="work/svc_1", repo="svc", state="leased"))
    board = FakeBoard()
    monkeypatch.setattr(
        pc, "_load_module",
        lambda name, filename: wp if name == "worktree_pool" else (board if name == "board" else None))
    rc = pc.main(["alloc", "svc", "--task", "job"])
    assert rc == 0
    assert ("alloc", "svc", "job") in wp.calls


# ════════════════════════════════════════════════════════════════════════
# ADR-0068 W1 — task-명의 alloc 항상-신규(I3) · 집합 재열거(I1) · add --task(ⓓB) · T-0398
# ════════════════════════════════════════════════════════════════════════


def test_alloc_forwards_owner_task_for_always_new_lease(pc, capsys):
    """cmd_alloc 이 `owner_task=task` 로 alloc 호출 — 항상 신규 대여(I3·멱등 폐기 경로 시그널)."""
    wp = FakeWorktreePool(
        alloc_returns=FakeLease(slot="work/svc_2", repo="svc", session="job", state="leased"))
    rc = pc.cmd_alloc(
        argparse.Namespace(repo="svc", task="job"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    assert wp.last_alloc_owner_task == "job"   # session= 대신 owner_task= 로 I3 경로 진입


def test_alloc_reenumerates_held_slots(pc, capsys):
    """alloc 성공 직후 task 보유 슬롯 집합을 재열거 surface(I1·ADR-0068)."""
    leases = [
        FakeLease(slot="work/svc_1", repo="svc", session="job", state="leased"),
        FakeLease(slot="work/svc_2", repo="svc", session="job", state="leased"),
    ]
    wp = FakeWorktreePool(leases=leases, alloc_returns=leases[1],
                          live_branches={"work/svc_1": "svc_1", "work/svc_2": "svc_2"})
    rc = pc.cmd_alloc(
        argparse.Namespace(repo="svc", task="job"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "보유 2" in out                                   # 집합 크기 재열거
    assert "work/svc_1" in out and "work/svc_2" in out       # 두 슬롯 모두 행렬에
    assert "branch=svc_2" in out                             # live branch(current_branch) 반영
    assert ("slots_for_task", "job") in wp.calls


def test_alloc_pool_exhausted_suggests_add_task(pc, capsys):
    """풀 소진 거부 메시지가 `worktree add <repo> --task <이름>`(생성+대여 한 흐름)를 제시(ⓓB)."""
    wp = FakeWorktreePool(alloc_raises=FakeWorktreePool.NeedsCreate)
    rc = pc.cmd_alloc(
        argparse.Namespace(repo="svc", task="job"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "worktree add svc --task job" in err
    assert not wp.did("create_slot")   # 자동 생성 안 함(⑤ 물리층=사용자 승인)


def test_worktree_add_task_creates_and_leases_and_reenumerates(pc, capsys):
    """worktree add <repo> --task <이름> → create_slot(owner_task=task) 생성 직결 + 재열거(ⓓB·I1)."""
    leases = [FakeLease(slot="work/svc_1", repo="svc", session="job", state="leased")]
    wp = FakeWorktreePool(leases=leases, live_branches={"work/svc_1": "svc_1"})
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, readonly=False, task="job", user_ack="svc"),
        worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    assert wp.last_create_owner_task == "job"   # 생성 직후 그 슬롯 task 명의 대여(ⓓB)
    out = capsys.readouterr().out
    assert "task 대여" in out and "job" in out
    assert ("slots_for_task", "job") in wp.calls   # 집합 재열거(I1)


def test_worktree_add_task_and_readonly_mutually_exclusive(pc, capsys):
    """--task 와 --readonly 는 상호배타 — 함께 주면 rc1·생성 안 함(무소유 vs task 명의 모순)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, readonly=True, task="job", user_ack="svc"),
        worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    assert "함께 쓸 수 없다" in capsys.readouterr().err
    assert not wp.did("create_slot")


def test_worktree_add_task_validates_name(pc, capsys):
    """add --task 도 불법/예약 task 명 → InvalidTaskName rc1·생성 안 함(alloc 과 동일 헬퍼·must-fix ②)."""
    wp = FakeWorktreePool(validate_raises=True)
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, readonly=False, task="../evil", user_ack="svc"),
        worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    assert not wp.did("create_slot")


def test_worktree_add_task_requires_prebound(pc, capsys):
    """add --task 도 기바인딩 task 요구(F1→F2) — 미바인딩이면 rc1·생성 안 함."""
    wp = FakeWorktreePool(task_record=None)   # find_task → None(미바인딩)
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, readonly=False, task="job", user_ack="svc"),
        worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    assert "pm-bootstrap" in capsys.readouterr().err
    assert not wp.did("create_slot")


def test_worktree_add_task_reserved_name_rejected_via_resolved_board(pc, capsys, monkeypatch):
    """add --task 예약명(`<repo>_<N>`·⑥) 거부 — board 미주입(CLI 경로)이어도 해소해 registered 전달(must-fix ①).

    구 버그: cmd_worktree_add 가 board=None 을 그대로 `_validate_prebound_task` 로 넘겨 registered_repos
    가 None → 예약패턴 검증이 add 경로만 무력화됐다(alloc 은 board 해소). board 해소로 계약 통일.
    """
    wp = FakeWorktreePool()
    board = FakeBoard(registered=("svc",))
    # board 미주입(main 디스패치처럼) — _load_module 이 board 를 돌려주도록 monkeypatch.
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: board if name == "board" else None)
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, readonly=False, task="svc_1", user_ack="svc"),
        worktree_pool=wp   # board 미주입 — 내부 해소가 예약 검증을 활성화해야 거부된다
    )
    assert rc == 1
    assert not wp.did("create_slot")   # 예약명 거부 → 슬롯 생성 안 함
    # registered_repos 가 실제로 전달됐는지(None 완화 아님) 배선 확인.
    val = [c for c in wp.calls if c[0] == "_validate_task_name"]
    assert val and set(val[0][2]) == {"svc"}


def test_worktree_add_task_forwards_registered_repos_when_injected(pc):
    """board 주입 경로도 registered_repos 전달 — add 예약 검증 계약(alloc 동형·must-fix ①)."""
    wp = FakeWorktreePool()
    board = FakeBoard(registered=("svc", "acc"))
    pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, readonly=False, task="job", user_ack="svc"),
        worktree_pool=wp, board=board
    )
    val = [c for c in wp.calls if c[0] == "_validate_task_name"]
    assert val and set(val[0][2]) == {"svc", "acc"}


def test_worktree_add_without_task_no_reenumerate(pc, capsys):
    """--task 미지정 add(현행 경로)는 owner_task=None·재열거 없음(회귀 0)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_worktree_add(
        argparse.Namespace(repo="svc", test=None, user_ack="svc"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    assert wp.last_create_owner_task is None
    assert not wp.did("slots_for_task")


def test_release_task_reenumerates_remaining(pc, capsys):
    """release --task 성공 직후 남은 보유 슬롯 집합 재열거(I1·ADR-0068)."""
    leases = [FakeLease(slot="work/svc_2", repo="svc", session="job", state="leased")]
    wp = FakeWorktreePool(leases=leases, live_branches={"work/svc_2": "svc_2"})
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", task="job", force=False), worktree_pool=wp
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "work/svc_2" in out
    assert ("slots_for_task", "job") in wp.calls


def test_release_no_task_skips_reenumerate(pc, capsys):
    """--task 없는 slot-only 반납(백스톱)은 대상 task 가 없어 재열거하지 않는다."""
    wp = FakeWorktreePool()
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", force=False), worktree_pool=wp
    )
    assert rc == 0
    assert not wp.did("slots_for_task")


def test_task_end_reenumerates_empty_after_release(pc, capsys):
    """task end 성공 직후 재열거 — 전부 반납이라 보유 0(없음) surface(I1·ADR-0068)."""
    wp = FakeWorktreePool(
        end_task_result=SimpleNamespace(name="job", released=["work/svc_1"], dirty=[],
                                        refused=False, moved_to=None, moved_from=None))
    board = FakeBoard(task_scan={"claimed": [], "prefix_open": []})
    rc = pc.cmd_task_end(
        argparse.Namespace(name="job"), worktree_pool=wp, board=board
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "없음" in out                            # slots_for_task("job")=[] → "(없음)"
    assert ("slots_for_task", "job") in wp.calls


# ════════════════════════════════════════════════════════════════════════
# release --task 소유검사 (F3·T-0354)
# ════════════════════════════════════════════════════════════════════════


def test_release_task_forwards_owner_task(pc, capsys):
    """release <slot> --task <이름> → worktree_pool.release(slot, owner_task=이름)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", task="job", force=False), worktree_pool=wp
    )
    assert rc == 0
    assert ("release", "work/svc_1", "job") in wp.calls


def test_release_task_not_owner_refused(pc, capsys):
    """소유 아님(NotTaskOwner) → rc1 + 거부 안내(다른 task 슬롯 보호)."""
    wp = FakeWorktreePool(
        release_raises=lambda slot: FakeWorktreePool.NotTaskOwner(slot, "job", "other"))
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", task="job", force=False), worktree_pool=wp
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "job" in err and "소유가 아니다" in err


def test_release_force_ignores_task(pc, capsys):
    """release --force 는 force_release(백스톱) — --task 소유검사 우회."""
    wp = FakeWorktreePool(force_returns="present")
    rc = pc.cmd_release(
        argparse.Namespace(slot="work/svc_1", task="job", force=True), worktree_pool=wp
    )
    assert rc == 0
    assert wp.did("force_release") and not wp.did("release")


# ════════════════════════════════════════════════════════════════════════
# task end <이름> (F4·②⑲·T-0354)
# ════════════════════════════════════════════════════════════════════════


def _end_result(*, released=(), dirty=(), moved_to=None, moved_from=None):
    refused = bool(dirty)
    return SimpleNamespace(name="job", released=list(released), dirty=list(dirty),
                           refused=refused, moved_to=moved_to, moved_from=moved_from)


def test_task_end_claimed_gate_refuses(pc, capsys):
    """claimed 티켓 잔존(⑲) → rc1 + 목록·거부·end_task 미호출(반납/이동 안 함)."""
    wp = FakeWorktreePool()
    board = FakeBoard(task_scan={"claimed": [{"id": "T-0009", "title": "wip", "status": "claimed"}],
                                 "prefix_open": []})
    rc = pc.cmd_task_end(
        argparse.Namespace(name="job"), worktree_pool=wp, board=board
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "T-0009" in err and "종료 거부" in err
    # 소진 게이트가 막았으니 반납/이동을 시도하지 않는다.
    assert not wp.did("end_task")


def test_task_end_dirty_gate_refuses(pc, capsys):
    """claimed 없음·보유 슬롯 dirty → end_task refused → rc1 + dirty 목록."""
    wp = FakeWorktreePool(end_task_result=_end_result(dirty=["work/svc_1"]))
    board = FakeBoard(task_scan={"claimed": [], "prefix_open": []})
    rc = pc.cmd_task_end(
        argparse.Namespace(name="job"), worktree_pool=wp, board=board
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "work/svc_1" in err and "dirty" in err


def test_task_end_clean_success_releases_and_archives(pc, capsys, tmp_path):
    """전부 clean → rc0 + 반납 슬롯·아카이브 이동 목적지 surface."""
    dest = tmp_path / "_ended" / "job-20260718"
    wp = FakeWorktreePool(end_task_result=_end_result(
        released=["work/svc_1", "work/svc_2"], moved_to=dest, moved_from=tmp_path / "job"))
    board = FakeBoard(task_scan={"claimed": [], "prefix_open": []})
    rc = pc.cmd_task_end(
        argparse.Namespace(name="job"), worktree_pool=wp, board=board
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "work/svc_1" in out and "work/svc_2" in out
    assert str(dest) in out and "아카이브 이동" in out


def test_task_end_prefix_open_info_only(pc, capsys):
    """task 지정 prefix 의 open 티켓 = 정보 표시만(rc0·차단 안 함·①)."""
    wp = FakeWorktreePool(end_task_result=_end_result(released=["work/svc_1"]),
                          task_record=SimpleNamespace(name="job", prefix="PAY"))
    board = FakeBoard(task_scan={"claimed": [],
                                 "prefix_open": [{"id": "T-PAY-009", "title": "backlog", "status": "open"}]})
    rc = pc.cmd_task_end(
        argparse.Namespace(name="job"), worktree_pool=wp, board=board
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "T-PAY-009" in out and "정보" in out
    # 스캔이 task prefix(PAY)로 호출됐는지(find_task.prefix 전달 배선).
    assert board.scan_calls and board.scan_calls[0][1:] == ("job", "PAY")


def test_task_end_board_absent_still_ends(pc, capsys, monkeypatch):
    """board 부재(scanner 없음) → claimed 게이트 graceful skip·end_task 진행(순수 슬롯 정리·rc0)."""
    wp = FakeWorktreePool(end_task_result=_end_result(released=["work/svc_1"]))
    # board 모듈 로드 실패(None) 모사 — worktree_pool 만 주입, board 미주입.
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: None)
    rc = pc.cmd_task_end(
        argparse.Namespace(name="job"), worktree_pool=wp, board=None
    )
    assert rc == 0
    assert wp.did("end_task")


def test_task_end_validates_task_name(pc, capsys):
    """불법 task 명 → rc1 (board 스캔·end_task 이전 선-fail·must-fix ②)."""
    wp = FakeWorktreePool(validate_raises=True)
    board = FakeBoard(task_scan={"claimed": [], "prefix_open": []})
    rc = pc.cmd_task_end(
        argparse.Namespace(name="../evil"), worktree_pool=wp, board=board
    )
    assert rc == 1
    # 검증이 스캔·end_task 이전 — 잘못된 이름으로 스캔/이동을 시도하지 않는다.
    assert not wp.did("end_task") and not board.scan_calls


def test_main_routes_task_end_to_engine(pc, monkeypatch):
    """main(["task","end","job"]) → cmd_task_end 라우팅(task 그룹 서브커맨드)."""
    wp = FakeWorktreePool(end_task_result=_end_result(released=[]))
    board = FakeBoard(task_scan={"claimed": [], "prefix_open": []})
    monkeypatch.setattr(
        pc, "_load_module",
        lambda name, filename: wp if name == "worktree_pool" else (board if name == "board" else None))
    rc = pc.main(["task", "end", "job"])
    assert rc == 0
    assert wp.did("end_task")


# ════════════════════════════════════════════════════════════════════════
# task prefix <이름> <p|none> (F5·T-0357·중간 변경 자유·분류 라벨≠경계·①)
# ════════════════════════════════════════════════════════════════════════


def test_task_prefix_sets_prefix(pc, capsys):
    """task prefix <이름> <p> → set_task_prefix(이름, p) 로 저장·rc0(지정)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_task_prefix(
        argparse.Namespace(name="job", value="pay"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 0
    assert ("set_task_prefix", "job", "pay") in wp.calls
    out = capsys.readouterr().out
    assert "pay" in out and "job" in out


def test_task_prefix_none_clears_and_skips_format_validation(pc, capsys):
    """value=`none` → 해제(prefix=None)·board `_validate_prefix` 미호출(해제 리터럴 선-가로채기)."""
    wp = FakeWorktreePool()
    board = FakeBoard()
    rc = pc.cmd_task_prefix(
        argparse.Namespace(name="job", value="none"), worktree_pool=wp, board=board
    )
    assert rc == 0
    assert ("set_task_prefix", "job", None) in wp.calls   # None = 해제로 저장
    out = capsys.readouterr().out
    assert "해제" in out


def test_task_prefix_none_case_insensitive_clears(pc):
    """`NONE`/`None` 도 fold 로 해제 리터럴(ADR-0055 case-insensitive)."""
    for literal in ("NONE", "None", "nOnE"):
        wp = FakeWorktreePool()
        rc = pc.cmd_task_prefix(
            argparse.Namespace(name="job", value=literal), worktree_pool=wp, board=FakeBoard()
        )
        assert rc == 0
        assert ("set_task_prefix", "job", None) in wp.calls


def test_task_prefix_validates_task_name_before_write(pc, capsys):
    """불법/예약 task 명 → InvalidTaskName → rc1(set_task_prefix 미호출·must-fix)."""
    wp = FakeWorktreePool(validate_raises=True)
    rc = pc.cmd_task_prefix(
        argparse.Namespace(name="../evil", value="pay"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    assert not wp.did("set_task_prefix")   # 검증이 write 이전


def test_task_prefix_forwards_registered_repos_for_reserved_check(pc):
    """task 명 검증에 등록 repo 전달 — 예약패턴(`<repo>_<N>`·⑥) 판별 근거(alloc/task end 동형)."""
    wp = FakeWorktreePool()
    board = FakeBoard(registered=("svc", "acc"))
    pc.cmd_task_prefix(
        argparse.Namespace(name="job", value="pay"), worktree_pool=wp, board=board
    )
    val = [c for c in wp.calls if c[0] == "_validate_task_name"]
    assert val and set(val[0][2]) == {"svc", "acc"}


def test_task_prefix_bad_format_refused(pc, capsys):
    """형식 위반(하이픈 등·ADR-0042) → board `_validate_prefix` 사유 → rc1(set_task_prefix 미호출)."""
    wp = FakeWorktreePool()
    rc = pc.cmd_task_prefix(
        argparse.Namespace(name="job", value="bad-prefix"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    assert not wp.did("set_task_prefix")   # 입력 sanity 실패 → 저장 안 함
    err = capsys.readouterr().err
    assert "형식 위반" in err


def test_task_prefix_task_missing_refuses_with_bootstrap_hint(pc, capsys):
    """set_task_prefix→None(task 부재) → rc1 + `/pm-bootstrap --task` 안내(생성은 F1 단일 지점)."""
    wp = FakeWorktreePool(set_prefix_result=None)   # task 부재 모델
    rc = pc.cmd_task_prefix(
        argparse.Namespace(name="ghost", value="pay"), worktree_pool=wp, board=FakeBoard()
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "pm-bootstrap" in err and "ghost" in err
    # T-0810 — 스트립이 남긴 빈 괄호 잔재(`()`) 정정 확인(실 CLI 출력 값 단언·조립 문자열 금지).
    assert "()" not in err
    assert "정체성을 생성하지 않는다. 먼저" in err


def test_task_prefix_engine_missing_errors(pc, capsys, monkeypatch):
    """worktree_pool 부재(None) → 명시 에러 rc1(침묵 무력화 금지·ADR-0013)."""
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_task_prefix(
        argparse.Namespace(name="job", value="pay"), worktree_pool=None, board=None
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "worktree_pool" in err


def test_task_prefix_board_absent_fails_closed_before_store(pc, monkeypatch, capsys):
    """board 부재면 4소스 승인 게이트를 판정할 수 없어 저장 전 fail-closed한다."""
    wp = FakeWorktreePool()
    # board 로드 실패(None) 모사 — 형식 validator·registered_repos 부재(hermetic).
    monkeypatch.setattr(pc, "_load_module", lambda name, filename: None)
    rc = pc.cmd_task_prefix(
        argparse.Namespace(name="job", value="pay"), worktree_pool=wp, board=None
    )
    assert rc == 1
    assert not wp.did("set_task_prefix")
    assert "승인 게이트" in capsys.readouterr().err


def test_main_routes_task_prefix_to_engine(pc, monkeypatch):
    """main(["task","prefix","job","pay"]) → cmd_task_prefix → set_task_prefix 라우팅."""
    wp = FakeWorktreePool()
    board = FakeBoard()
    monkeypatch.setattr(
        pc, "_load_module",
        lambda name, filename: wp if name == "worktree_pool" else (board if name == "board" else None))
    rc = pc.main(["task", "prefix", "job", "pay"])
    assert rc == 0
    assert ("set_task_prefix", "job", "pay") in wp.calls


def test_task_prefix_surfaced_in_task_group_help(pc, capsys):
    """`task prefix` 가 task 그룹 도움말에 노출(end 와 나란히·발견성)."""
    with pytest.raises(SystemExit):
        pc.main(["task", "--help"])
    out = capsys.readouterr().out
    assert "prefix" in out and "end" in out


def test_main_task_group_without_sub_shows_help(pc, capsys):
    """`task` 만 주고 하위 동작 없음 → 그룹 도움말 surface·rc1(repo/worktree 동형)."""
    with pytest.raises(SystemExit):
        # argparse 그룹 --help 는 SystemExit(0) 로 끝난다(repo/worktree 동형 경로).
        pc.main(["task"])
