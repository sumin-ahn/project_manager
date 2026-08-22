"""bootstrap/handoff 리스 라이프사이클 바인딩 (T-0060 · ADR-0013·0011) 단위 테스트.

엔진 canonical(루트 .project_manager/tools/pm_bootstrap.py·pm_handoff.py)을 importlib 로
직접 검증한다. worktree_pool 은 **DI mock** 으로 주입해 hermetic — 실 리스 장부·git·
work/ 풀을 절대 건드리지 않는다(test_handoff_trigger.py 의 DI 패턴 동류).

검증 축:
  - bootstrap --repo --branch → alloc 호출·identity surface 출력·cwd 슬롯 보고.
  - 무인자(솔로) → 현행 동작 (alloc 경로 미진입·worktree_pool 안 건드림).
  - NeedsCreate (풀 소진) → 사용자 게이트 안내·자동 git worktree add 안 함.
  - handoff payload 에 slot/branch 기록 · --done → release · --done 없으면 리스 유지(release X).
  - 회전 재부착(resume) 연속성 · worktree_pool 부재 시 명시 에러(침묵 무력화 금지).
  - sensitivity: 배선 무력화 시 fail.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path, PureWindowsPath

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bootstrap():
    return _load("pm_bootstrap")


@pytest.fixture(scope="module")
def handoff():
    return _load("pm_handoff")


@pytest.fixture(autouse=True)
def _hermetic_dashboard(bootstrap, handoff, tmp_path, monkeypatch):
    """slot 대시보드 렌더를 tmp 로 재앵커한다.

    `_dashboard_file()` 은 모듈 `REPO` 를 따라가므로 재앵커 없이 부트스트랩/핸드오프를 돌리면
    실 작업 트리의 `wiki/log/dashboard.md` 를 갱신한다(tests/conftest.py 의 live board 오염
    가드가 teardown 에서 이를 잡는다)."""
    target = tmp_path / "dashboard.md"
    for module in (bootstrap, handoff):
        if hasattr(module, "_dashboard_file"):
            monkeypatch.setattr(module, "_dashboard_file", lambda t=target: t)


@pytest.fixture(autouse=True)
def _hermetic_engine_anchor(bootstrap, monkeypatch):
    """0단계 엔진 앵커 검사(T-0351)를 hermetic 무력화한다.

    엔진 테스트는 worktree ①(엔진 canonical·`work/<repo>_<N>`)에서 로드되므로 실 `REPO` 가 PM 홈
    등록 worktree 사본으로 보인다 → 0단계 앵커 검사(`board._pm_home_worktree_misanchor(REPO)`)가
    거부한다(프로덕션 ②-홈에선 실 board 소유로 통과·`work/`-misuse 만 거부). 실 board 를 로드해
    `_pm_home_worktree_misanchor`→None 으로만 패치(나머지 board 동작=실물 보존)하고, board=None 경로가
    그 패치본을 받게 `_load_board` 를 대체한다 — board 를 명시 주입한 구성은 영향 없음(`self._board` 승)."""
    real_board = bootstrap._load_board()
    if real_board is not None:
        monkeypatch.setattr(real_board, "_pm_home_worktree_misanchor",
                            lambda anchor, **_kw: None, raising=False)
    monkeypatch.setattr(bootstrap, "_load_board", lambda: real_board)


# ── worktree_pool DI mock (hermetic — 실 장부/git 미접촉) ─────────────────────


class _FakeLease:
    def __init__(self, slot: str, repo: str, branch: str | None):
        self.slot = slot
        self.repo = repo
        self.branch = branch


class _FakeLeaseEntry:
    """list_leases() 가 돌려주는 장부 엔트리 대역 — state/session/slot/role surface (상태점검용).

    `role` = canonical 슬롯 role(T-0358 이 `Lease.role` 로 승격) — 0단계 readonly carve-out(⑬·
    `_phase0_is_readonly`)이 **`lease.role`** 을 직접 읽는다("readonly"=공유 자산·타-점유/보호브랜치
    검사 비적용). `extra` = 미지 최상위 키 보존(T-0350·`Lease.extra` 동형·role 은 canonical 로 이관)."""

    def __init__(self, slot: str, repo: str, session: str, *, state: str = "leased",
                 role: str = "work", extra: dict | None = None):
        self.slot = slot
        self.repo = repo
        self.session = session
        self.state = state
        self.role = role
        self.extra = extra or {}


class _FakeCompare:
    """`compare_slot_git` 결과 대역 (T-0350 GitCompareResult 소비 표면·0단계 record-vs-live·T-0351).

    0단계는 `fail_loud`/`unrecorded`/`submodule_drift`/`recorded`/`live` 만 읽으므로 그것들만 든다."""

    def __init__(self, *, fail_loud: bool = False, unrecorded: bool = False,
                 head_relation: str = "match", submodule_drift: list | None = None,
                 recorded: dict | None = None, live: dict | None = None):
        self.fail_loud = fail_loud
        self.unrecorded = unrecorded
        self.head_relation = head_relation
        self.submodule_drift = submodule_drift or []
        self.recorded = recorded or {}
        self.live = live or {}


class _FakeNeedsCreate(Exception):
    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(repo)


class _FakeTaskActiveElsewhere(Exception):
    """worktree_pool.TaskActiveElsewhere 대역 (T-0353·㉑ 동시 세션 거부)."""

    def __init__(self, name: str, pid: int):
        self.name = name
        self.pid = pid
        super().__init__(name)


class _FakeInvalidTaskName(Exception):
    """worktree_pool.InvalidTaskName 대역 (T-0353·must-fix task 명 검증)."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(reason)


class _FakeTaskRecord:
    """bind_task 반환 Task 대역 — surface 가 읽는 name·prefix·started."""

    def __init__(self, name: str, prefix: "str | None", started: str = "2026-07-18T00:00:00+00:00"):
        self.name = name
        self.prefix = prefix
        self.started = started


class FakeWorktreePool:
    """worktree_pool 인터페이스를 흉내내는 mock — 호출을 기록만 한다(실 부작용 0).

    실 엔진 시그니처: alloc(repo, *, branch, resume) → Lease · slot_path(slot) → Path ·
    release(slot, *, require_clean) → Lease · NeedsCreate(repo) 예외.
    """

    def __init__(self, *, alloc_raises_needs_create: bool = False,
                 alloc_slot: str = "work/A_1", alloc_branch: str | None = "a5",
                 force_detached: bool = False,
                 present_slots: "tuple[str, ...] | None" = ("work/X_2",),
                 readonly_slots: "tuple[str, ...] | None" = None,
                 compare_result=None,
                 task_dir_root: "Path | None" = None,
                 task_prefix: "str | None" = None,
                 task_action: str = "created",
                 task_raises_pid: "int | None" = None,
                 task_reclaimed_from: "int | None" = None,
                 task_invalid_reason: "str | None" = None):
        self.NeedsCreate = _FakeNeedsCreate
        # task 축(T-0353·F1) — bind_task/task_dir/TaskActiveElsewhere/InvalidTaskName 대역.
        self.TaskActiveElsewhere = _FakeTaskActiveElsewhere
        self.InvalidTaskName = _FakeInvalidTaskName
        self._task_dir_root = task_dir_root       # None=미설정(테스트가 세팅)
        self._task_prefix = task_prefix           # bind_task 반환 prefix(기본 None=없음)
        self._task_action = task_action           # created|resumed|reclaimed
        self._task_raises_pid = task_raises_pid   # 설정 시 bind_task 가 거부(㉑ alive)
        self._task_reclaimed_from = task_reclaimed_from  # reclaimed 시 회수한 이전 pid(loud notice)
        self._task_invalid_reason = task_invalid_reason  # 설정 시 InvalidTaskName(명 검증 거부)
        self.bind_task_calls: list[dict] = []
        self._alloc_raises = alloc_raises_needs_create
        self._alloc_slot = alloc_slot
        self._alloc_branch = alloc_branch
        self._force_detached = force_detached  # True → current_branch 항상 None(detached/조회불가)
        self.alloc_calls: list[dict] = []
        self.bind_calls: list[dict] = []
        self.release_calls: list[dict] = []
        self.current_branch_calls: list[str] = []
        self.release_raises_keyerror = False
        # 0단계(T-0351) 실재 검사가 통과하도록 사전 시드하는 **idle** 슬롯 — lean 테스트는 실재하는
        # 슬롯(idle 리스)에 bind 하므로(§F1b "장부·폴더에 실재"), 그 idle 리스를 list_leases 에 실어
        # phantom-거부를 피한다. idle 이라 점유(leased)·"다른 활성 PM" surface 엔 안 잡힌다(회귀 0).
        self._present_slots: tuple[str, ...] = present_slots or ()
        # readonly 공유 슬롯(⑬·T-0358) 시드 — role="readonly"·무소유(session 없음)·leased state
        # (create_slot 확정 형태). 0단계 carve-out(타-점유/보호브랜치 비적용)이 이걸 읽는다.
        self._readonly_slots: tuple[str, ...] = readonly_slots or ()
        # 0단계 record-vs-live(compare_slot_git·T-0350) 결과 대역 — None=미설정(정합 검사 no-op).
        self._compare_result = compare_result
        # 슬롯 → live 브랜치 매핑(ADR-0013 amend T-0072 — identity/release surface 가
        # lease.branch 대신 current_branch(slot) 를 읽는다). alloc 이 effective 를 기록.
        self._live_branch: dict[str, str | None] = {}
        # 슬롯 → live HEAD override — alloc 이 심은 값을 *덮어쓴다*(사용자가 슬롯서 직접
        # git checkout 한 drift 모델링). 있으면 current_branch 가 이걸 우선 반환한다.
        self.live_branch_override: dict[str, str | None] = {}
        # 상태점검(다른 활성 PM) surface 용 — list_leases 가 돌려줄 추가 리스(이 세션 외).
        self._extra_leases: list = []

    def bind_slot(self, slot, repo, session, *, git_runner=None):
        # 사람 발의 직접 바인딩(T-0074) — 호출을 기록만 한다(실 장부 미접촉). branch 는
        # 안 만지고, identity 가 표시할 live 브랜치는 override 가 있으면 그걸 따른다.
        self.bind_calls.append({"slot": slot, "repo": repo, "session": session})
        if slot not in self._live_branch and slot not in self.live_branch_override:
            self._live_branch[slot] = self._alloc_branch
        return _FakeLease(slot, repo, self._live_branch.get(slot))

    def list_leases(self):
        # 상태점검 surface — bind 된 슬롯(이 세션) + 미리 심은 다른 활성 리스 + 0단계 실재 검사용
        # idle 시드(§F1b·bind 전에도 슬롯이 실재하도록).
        leased: list = []
        for slot, repo, session in [
            (c["slot"], c["repo"], c["session"]) for c in self.bind_calls
        ]:
            leased.append(_FakeLeaseEntry(slot, repo, session, state="leased"))
        for slot in self._present_slots:
            _, _, tail = slot.rpartition("/")
            repo = tail.rsplit("_", 1)[0]
            leased.append(_FakeLeaseEntry(slot, repo, "", state="idle"))
        for slot in self._readonly_slots:
            _, _, tail = slot.rpartition("/")
            repo = tail.rsplit("_", 1)[0]
            # readonly = role="readonly"·무소유(session="")·leased(create_slot 확정형·⑬·T-0358).
            leased.append(_FakeLeaseEntry(slot, repo, "", state="leased", role="readonly"))
        leased.extend(self._extra_leases)
        return leased

    def slots_for_task(self, name):
        """기본 task fixture는 작업공간 0개 — 해소 성공과 엔진 부재를 구분한다."""
        return []

    def compare_slot_git(self, slot, *, git_runner=None):
        # 0단계 record-vs-live(T-0350 compare 프리미티브) 대역 — 설정된 결과(없으면 None=검사 no-op).
        return self._compare_result

    def alloc(self, repo, *, branch=None, resume=None, **_kw):
        self.alloc_calls.append({"repo": repo, "branch": branch, "resume": resume})
        if self._alloc_raises:
            raise self.NeedsCreate(repo)
        # resume 이 주어지면 그 브랜치로 재부착(연속성) — 없으면 요청 branch 또는 기본.
        effective = branch if branch is not None else (resume if resume is not None else self._alloc_branch)
        # 슬롯 worktree 가 effective 브랜치를 체크아웃한 상태로 모델링(git=진실).
        self._live_branch[self._alloc_slot] = effective
        return _FakeLease(self._alloc_slot, repo, effective)

    def current_branch(self, slot, *, git_runner=None):
        # 슬롯 worktree 의 git HEAD live 조회 대역(ADR-0013 amend T-0072). override 가 있으면
        # 그걸(사용자 직접 checkout drift), 없으면 alloc 이 심은 매핑을 돌려준다 — 미등록
        # 슬롯/force_detached 는 None(detached/조회불가).
        self.current_branch_calls.append(slot)
        if self._force_detached:
            return None
        if slot in self.live_branch_override:
            return self.live_branch_override[slot]
        return self._live_branch.get(slot)

    def slot_path(self, slot):
        return Path("/tmp/multipm") / slot

    def task_dir(self, name):
        root = self._task_dir_root or (Path("/tmp/multipm") / "tasks")
        return root / name

    def bind_task(self, name, *, pid=None, registered_repos=None):
        # F1 task 바인딩 대역 — 명 검증 거부(InvalidTaskName)/alive 점유 거부(㉑), 아니면
        # (record, action, reclaimed_from) 3-튜플 반환 + task state 즉시 생성.
        self.bind_task_calls.append({"name": name, "registered_repos": registered_repos})
        if self._task_invalid_reason is not None:
            raise self.InvalidTaskName(name, self._task_invalid_reason)
        if self._task_raises_pid is not None:
            raise self.TaskActiveElsewhere(name, self._task_raises_pid)
        self.task_dir(name).mkdir(parents=True, exist_ok=True)
        state = self.task_dir(name) / "pm_state.md"
        if not state.exists():
            state.write_text(
                "## 세션 식별 (현재까지 사용된 이름)\n"
                "  - (아직 완료된 task 세션 없음)\n",
                encoding="utf-8",
            )
        return (_FakeTaskRecord(name, self._task_prefix),
                self._task_action, self._task_reclaimed_from)

    def release(self, slot, *, require_clean=True, **_kw):
        self.release_calls.append({"slot": slot, "require_clean": require_clean})
        if self.release_raises_keyerror:
            raise KeyError(slot)
        return _FakeLease(slot, "A", "a5")


# ── bootstrap fixture: board/git/pytest DI 로 hermetic stub ──────────────────


class _FakeBoard:
    """board 모듈 대역 — 보호 브랜치 surface(`_repo_protected`)용 (T-0076).

    `protected` 매핑(repo→목록)을 들고 있다가 `_repo_protected(repo)` 로 돌려준다(미지정
    repo 는 default). PmBootstrap._protected_warning 이 이 헬퍼로 라이브 브랜치를 판정한다.
    """

    def __init__(self, *, protected=None):
        self._protected = protected or {}

    def _repo_protected(self, repo):
        return self._protected.get(repo, ["main", "master", "develop"])


def _make_bootstrap(bootstrap, tmp_path, *, worktree_pool=None, areas_text: str | None = None,
                    board=None, pm_state_text: str | None = None):
    """격리된 PmBootstrap — board/git/log 는 stub, worktree_pool/board 는 mock 주입.

    pm_state 도 hermetic seam 으로 주입한다(T-0179·차수/인계 dump) — 미지정이면 빈 pm_state
    파일을 둬 실 worktree pm_state 누수를 막는다(차수 placeholder·남은작업 미해소로 폴백).
    """
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    if areas_text is not None:
        areas_file.write_text(areas_text, encoding="utf-8")
    # pm_state hermetic seam — 명시 텍스트면 그걸, 아니면 빈 파일(handoff_ctx 는 placeholder).
    pm_state_file = tmp_path / "pm_state.md"
    pm_state_file.write_text(pm_state_text if pm_state_text is not None else "", encoding="utf-8")

    board_output = (
        "  [open   ] T-0001  something  pm  tag\n"
        "  [done   ] T-0000  done thing  pm  tag\n"
    )

    def fake_board(args):
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        return 0, board_output

    def fake_git(args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 commit subject\n"
        if args[:1] == ["status"]:
            return 0, ""
        return 0, ""

    inst = bootstrap.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨")),
        run_git_fn=fake_git,
        log_file=log_file,
        areas_file=areas_file,
        worktree_pool=worktree_pool,
        board=board,
        pm_state_file=pm_state_file,
    )
    return inst


# ── 1. bootstrap --repo --branch → alloc + identity surface + cwd ─────────────


def test_bootstrap_repo_calls_alloc(bootstrap, tmp_path, capsys):
    wp = FakeWorktreePool(alloc_slot="work/A_2", alloc_branch="a5")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(repo="A", branch="a5")
    assert rc == 0
    # alloc 이 정확히 한 번·repo/branch 와 함께 호출됐다.
    assert wp.alloc_calls == [{"repo": "A", "branch": "a5", "resume": None}]


def test_bootstrap_repo_emits_identity_surface(bootstrap, tmp_path, capsys):
    wp = FakeWorktreePool(alloc_slot="work/A_2", alloc_branch="a5")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="A", branch="a5")
    out = capsys.readouterr().out
    # identity surface: "당신은 A PM" + worktree 슬롯 + branch + multi-PM 공유 보드.
    assert "당신은 **A PM**" in out
    assert "work/A_2" in out
    assert "a5" in out
    assert "보드=multi-PM 공유" in out


def test_bootstrap_identity_branch_from_live_current_branch(bootstrap, tmp_path, capsys):
    """identity 의 branch 가 `current_branch(slot)` live 조회에서 온다(ADR-0013 amend T-0072).

    alloc 후 슬롯의 live HEAD 를 다른 값으로 바꾼다(사용자가 슬롯서 직접 checkout 한 상황) —
    identity surface 가 저장 복사본이 아니라 *바뀐 live 값*을 표시하면 live 조회임이 입증된다.
    """
    wp = FakeWorktreePool(alloc_slot="work/A_2", alloc_branch="a5")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    # 사용자가 슬롯서 git checkout 한 상황 모델링 — alloc 이 심을 a5 를 a9-live 로 override.
    wp.live_branch_override["work/A_2"] = "a9-live"
    inst.run(repo="A", branch="a5")
    out = capsys.readouterr().out
    # identity 의 branch= 가 live 값(a9-live)이다 — 요청 branch(a5) 가 아니라 슬롯 HEAD live.
    assert "branch=`a9-live`" in out, "identity branch 가 live current_branch 가 아님(저장 복사본 사용)"
    # current_branch 가 슬롯에 대해 호출됐다(live 조회 경로).
    assert "work/A_2" in wp.current_branch_calls


def test_bootstrap_identity_detached_branch_shows_placeholder(bootstrap, tmp_path, capsys):
    """current_branch 가 None(detached/조회불가)이면 identity branch 가 "(미지정)"(fail-soft 유지)."""
    wp = FakeWorktreePool(alloc_slot="work/A_2", alloc_branch="a5", force_detached=True)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="A", branch="a5")
    out = capsys.readouterr().out
    assert "branch=`(미지정)`" in out, "detached current_branch 가 '(미지정)' 로 surface 안 됨"


def test_bootstrap_repo_reports_slot_cwd(bootstrap, tmp_path, capsys):
    wp = FakeWorktreePool(alloc_slot="work/A_2")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="A", branch="a5")
    out = capsys.readouterr().out
    # cwd(작업 슬롯) 경로가 보고된다 (slot_path 산출). 엔진 표시는 OS-네이티브라
    # Windows 에선 역슬래시 — 경로 구분자만 정규화해 비교(POSIX 무변경·os.sep="/").
    assert "/tmp/multipm/work/A_2" in out.replace(os.sep, "/")


def test_bootstrap_repo_identity_lists_registered_areas(bootstrap, tmp_path, capsys):
    """areas.md 가 있으면 identity surface '등록영역' 에 repo 목록을 표면한다."""
    areas = (
        "| repo | prefix | git | test_cmd | owner |\n"
        "|------|--------|-----|----------|-------|\n"
        "| A | A | g | pytest | me |\n"
        "| B | B | g | go test | me |\n"
    )
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp, areas_text=areas)
    inst.run(repo="A", branch="a5")
    out = capsys.readouterr().out
    assert "등록영역: A, B" in out


def test_bootstrap_repo_json_includes_worktree(bootstrap, tmp_path, capsys):
    """--json 출력에도 worktree identity 가 surface 된다."""
    import json as _json
    wp = FakeWorktreePool(alloc_slot="work/A_3", alloc_branch="a7")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="A", branch="a7", output_json=True)
    data = _json.loads(capsys.readouterr().out)
    assert data["worktree"]["repo"] == "A"
    assert data["worktree"]["slot"] == "work/A_3"
    assert data["worktree"]["branch"] == "a7"


# ── 2. 솔로 무인자 — 현행 동작 보존 (alloc 경로 미진입) ───────────────────────


def test_bootstrap_solo_does_not_touch_worktree_pool(bootstrap, tmp_path, capsys):
    """무인자(솔로)면 worktree_pool 을 절대 건드리지 않는다 (alloc 0회·현행 출력)."""
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run()  # repo 미지정 — 솔로.
    assert rc == 0
    assert wp.alloc_calls == []  # alloc 경로 미진입.
    out = capsys.readouterr().out
    # 현행 부트스트랩 출력은 유지되고, identity surface 는 없다 (헤더=차수 announce·T-0179).
    assert "부트스트랩" in out
    assert "당신은" not in out


def test_bootstrap_solo_no_worktree_pool_needed(bootstrap, tmp_path, capsys):
    """솔로 경로는 worktree_pool 미주입(None)이어도 동작한다 (fail-soft·import 불요)."""
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=None)
    assert inst.run() == 0


# ── 2b. lean 정체성 선언 — --repo --slot → bind_slot + identity + 상태점검 (T-0074) ──


def test_bootstrap_slot_calls_bind_not_alloc(bootstrap, tmp_path, capsys):
    """--slot lean 모드는 bind_slot 을 호출하고 alloc 은 절대 안 부른다(직접 바인딩·pool 우회)."""
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    # bind_slot 이 정확히 한 번·세션명/슬롯과 함께 호출됐다.
    assert wp.bind_calls == [
        {"slot": "work/X_2", "repo": "X", "session": "X_2"}
    ]
    # alloc 은 절대 안 탄다(bind 경로는 풀 alloc 을 거치지 않음).
    assert wp.alloc_calls == []


def test_bootstrap_slot_emits_lean_identity_surface(bootstrap, tmp_path, capsys):
    """lean identity surface — 세션명 `X_2`·worktree·라이브 브랜치·`--session X_2` 안내."""
    wp = FakeWorktreePool(alloc_branch="x-feat")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "당신은 **X PM**" in out
    assert "세션=`X_2`" in out
    assert "worktree=`work/X_2`" in out
    assert "브랜치=`x-feat`" in out          # 라이브 브랜치(current_branch)
    assert "--repo X --slot 2" in out        # 보드 조작 명시 안내 (ADR-0057·decomposed)
    assert "--session X_2" not in out        # 옛 actor 플래그 잔존 금지 (BREAKING·--session-seq 차수는 무관)
    assert "보드=multi-PM 공유" in out


def test_bootstrap_slot_identity_label_unified_worktree(bootstrap, tmp_path, capsys):
    """lean variant 라벨이 공개-제품 variant 와 통일(`worktree=`)·옛 `슬롯=` 제거 (T-0298).

    같은 슬롯 정체성을 두 표기(`슬롯=`/`worktree=`)로 부르던 혼선을 닫는다 — worktree 경로임을
    명시하는 단일 용어로 통일.
    """
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "worktree=`work/X_2`" in out
    assert "슬롯=`work/X_2`" not in out       # 옛 라벨 제거(용어 통일)


def test_bootstrap_slot_identity_appends_pm_state_path(bootstrap, tmp_path, capsys):
    """lean identity surface 가 이 슬롯의 per-slot pm_state 경로를 병기한다 (T-0298).

    독자가 pm_state 위치를 못 잡던 표기 혼선(identity=`work/<repo>_<N>` vs 실경로=`.local/slots/
    <repo>_<N>/`)을 닫는다 — `_pm_state_display_path` 재사용 산출을 identity 뒤에 한 줄.
    """
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "pm_state (이 슬롯):" in out
    assert ".project_manager/.local/slots/X_2/pm_state.md" in out


def test_bootstrap_slot_identity_branch_is_live(bootstrap, tmp_path, capsys):
    """identity 의 브랜치가 `current_branch(slot)` live 조회에서 온다(저장 복사본 아님)."""
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    # 슬롯 live HEAD 를 명시 override — bind 가 심을 기본값 대신 이 값이 표시되면 live 조회 입증.
    wp.live_branch_override["work/X_2"] = "x-live"
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "브랜치=`x-live`" in out
    assert "work/X_2" in wp.current_branch_calls


def test_bootstrap_slot_identity_detached_shows_placeholder(bootstrap, tmp_path, capsys):
    """current_branch None(detached/조회불가) → 브랜치 surface 가 "(미지정)"(fail-soft)."""
    wp = FakeWorktreePool(force_detached=True)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "브랜치=`(미지정)`" in out


# ── readonly 공유 슬롯 0단계 carve-out (⑬·T-0358·seam #1 파급) ─────────────────
# `Lease.role` 을 canonical 필드로 승격(T-0358)하면 extra 에서 빠지므로, T-0351 의 extra["role"]
# 훅은 조용히 무력화된다 → `_phase0_is_readonly` 를 canonical `lease.role` read 로 갱신했다.
# 아래가 그 파급(readonly lease → 0단계 carve-out 실동작)을 못박는다.


def test_phase0_is_readonly_reads_canonical_role_not_extra(bootstrap, tmp_path):
    """seam #1 — `_phase0_is_readonly` 는 canonical `lease.role` 을 읽는다(extra 아님·T-0358).

    role 이 `_LEASE_CANONICAL_KEYS` 로 승격되며 extra 에서 빠졌으므로 옛 extra["role"] 훅은
    무력화된다. sensitivity: role="work"이되 extra={"role":"readonly"}(stale 스키마)는 **False** —
    canonical role 만 신뢰하고 extra 경유는 무시해야 훅 무력화 회귀가 없다."""
    inst = _make_bootstrap(bootstrap, tmp_path)
    ro = _FakeLeaseEntry("work/A_1", "A", "", state="leased", role="readonly")
    work = _FakeLeaseEntry("work/A_1", "A", "s", state="leased", role="work")
    stale_extra = _FakeLeaseEntry("work/A_1", "A", "s", state="leased", role="work",
                                  extra={"role": "readonly"})
    assert inst._phase0_is_readonly(ro) is True
    assert inst._phase0_is_readonly(work) is False
    assert inst._phase0_is_readonly(stale_extra) is False   # extra 경유 무시(canonical role 만)
    assert inst._phase0_is_readonly(None) is False


def test_bootstrap_readonly_slot_bind_refused(bootstrap, tmp_path, capsys):
    """⑬ should-fix — readonly 공유 슬롯 바인딩은 0단계에서 거부(rc1·무소유 공유 자산·T-0358).

    0단계 carve-out(F6)은 *조회 지칭*만 허용하고 bind 는 *점유*라 의미가 다르다 — `/pm-bootstrap
    --slot N` 오지정을 fail-loud 로 막는다(bind_slot 엔진 `ReadonlySlotNotLeasable` 의 user-facing 짝)."""
    wp = FakeWorktreePool(present_slots=())
    wp._extra_leases = [_FakeLeaseEntry("work/X_2", "X", "",
                                        state="leased", role="readonly")]
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1
    assert "readonly" in capsys.readouterr().err
    assert wp.bind_calls == [], "readonly bind 거부인데 bind_slot 이 불렸다"


def test_bootstrap_work_slot_other_holder_rejected_sensitivity(bootstrap, tmp_path, capsys):
    """sensitivity 대조 — 같은 슬롯이 role="work"(비-readonly)면 타 세션 점유로 0단계 rc1 거부(다른 사유).

    readonly 는 bind 거부(readonly 사유)·work 는 other-holder 거부(다른 세션 사유) — 둘의 사유가 갈려야
    role 판별이 실제로 작동함을 입증(양쪽 rc1 이되 메시지 다름)."""
    wp = FakeWorktreePool(present_slots=())
    wp._extra_leases = [_FakeLeaseEntry("work/X_2", "X", "other_sess",
                                        state="leased", role="work")]
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(repo="X", slot=2)
    assert rc == 1
    assert "다른 세션" in capsys.readouterr().err


def test_bootstrap_slot_surfaces_other_active_pms(bootstrap, tmp_path, capsys):
    """상태점검 — 다른 활성 리스가 있으면 그 현황(세션·슬롯·라이브 브랜치)을 surface."""
    wp = FakeWorktreePool()
    # 다른 활성 PM 을 장부에 심는다(이 세션 X_2 와 별개).
    wp._extra_leases = [_FakeLeaseEntry("work/billing_3", "billing", "billing_3")]
    wp.live_branch_override["work/billing_3"] = "bill-b"
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "다른 활성 PM" in out
    assert "`billing_3`" in out
    assert "`work/billing_3`" in out
    assert "bill-b" in out


def test_bootstrap_slot_no_other_pms_shows_placeholder(bootstrap, tmp_path, capsys):
    """다른 활성 리스가 없으면 "(다른 활성 PM 없음)" 을 surface(이 세션은 제외)."""
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "(다른 활성 PM 없음)" in out


def test_bootstrap_slot_excludes_own_session_from_status(bootstrap, tmp_path, capsys):
    """상태점검은 *이 세션 제외* — 자기 자신은 '다른 활성 PM' 에 안 나온다."""
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    # 이 세션(X_2)만 leased 면 '다른 활성 PM 없음' — 자기 세션을 타자로 surface 하지 않음.
    assert "(다른 활성 PM 없음)" in out


def test_bootstrap_slot_json_includes_worktree(bootstrap, tmp_path, capsys):
    """slot JSON은 identity를 내고 task-only 키/null을 섞지 않는다."""
    import json as _json
    wp = FakeWorktreePool(alloc_branch="x-feat")
    wp._extra_leases = [_FakeLeaseEntry("work/billing_3", "billing", "billing_3")]
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst._run_pytest_fn = lambda: (0, "1 passed in 0.01s\n")
    inst.run(repo="X", slot=2, output_json=True, with_pytest=True)
    data = _json.loads(capsys.readouterr().out)
    wt = data["worktree"]
    assert wt["repo"] == "X"
    assert wt["session"] == "X_2"
    assert wt["slot"] == "work/X_2"
    assert wt["branch"] == "x-feat"
    assert [o["session"] for o in wt["others"]] == ["billing_3"]
    assert data["board"]["counts_scope"] == "slot 2"
    assert "counts_mine" not in data["board"]
    assert "counts_task" not in data["board"]
    assert data["pytest"] == {"passed": 1, "total": 1}
    assert "scopes" not in data["pytest"]
    assert "task_cwd_slot" not in data["git"]
    assert "task_workspace_count" not in data["git"]


def test_bootstrap_slot_does_not_alloc_or_release(bootstrap, tmp_path, capsys):
    """lean bind 경로는 alloc/release 를 절대 부르지 않는다(직접 바인딩·명시 release 만)."""
    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    assert wp.alloc_calls == []
    assert wp.release_calls == []


# ── 2c. 보호 브랜치 경고 (소프트·T-0076) — 라이브 브랜치가 보호목록이면 🚫 surface ──


def test_bootstrap_slot_warns_when_live_branch_protected(bootstrap, tmp_path, capsys):
    """라이브 브랜치가 보호목록(main)이면 identity surface 에 🚫 보호 경고 (T-0076·소프트)."""
    wp = FakeWorktreePool(alloc_branch="main")   # 슬롯 live HEAD = main(보호)
    board = _FakeBoard(protected={"X": ["main", "develop"]})
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp, board=board)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "🚫" in out
    assert "보호 브랜치 `main`" in out
    assert "커밋/푸시 금지" in out


def test_bootstrap_slot_no_warning_when_feature_branch(bootstrap, tmp_path, capsys):
    """라이브 브랜치가 feature(보호목록 아님)면 보호 경고 없음 (T-0076·sensitivity 대조)."""
    wp = FakeWorktreePool(alloc_branch="x-feat")   # 보호목록 아님
    board = _FakeBoard(protected={"X": ["main", "develop"]})
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp, board=board)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "🚫" not in out
    assert "보호 브랜치" not in out


def test_bootstrap_slot_warning_uses_repo_protected_override(bootstrap, tmp_path, capsys):
    """보호 판정은 board._repo_protected(per-repo override) — release 가 보호목록이면 경고 (T-0076)."""
    wp = FakeWorktreePool(alloc_branch="release")
    board = _FakeBoard(protected={"X": ["release"]})   # per-repo override(default 아님)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp, board=board)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "보호 브랜치 `release`" in out


def test_bootstrap_slot_detached_branch_no_protected_warning(bootstrap, tmp_path, capsys):
    """detached/조회불가(current_branch None)면 보호 경고 없음 (fail-soft·T-0076)."""
    wp = FakeWorktreePool(force_detached=True)
    board = _FakeBoard(protected={"X": ["main"]})
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp, board=board)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "🚫" not in out


def test_bootstrap_slot_protected_warning_in_json(bootstrap, tmp_path, capsys):
    """--json 출력에도 보호 브랜치(protected_branch) 필드가 surface 된다 (T-0076)."""
    import json as _json
    wp = FakeWorktreePool(alloc_branch="main")
    board = _FakeBoard(protected={"X": ["main"]})
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp, board=board)
    inst.run(repo="X", slot=2, output_json=True)
    data = _json.loads(capsys.readouterr().out)
    assert data["worktree"]["protected_branch"] == "main"


def test_bootstrap_slot_board_absent_no_protected_warning(bootstrap, tmp_path, capsys):
    """board 부재(헬퍼 없음)면 보호 경고 생략 — 소프트(정체성 선언은 안 깨짐·T-0076)."""
    wp = FakeWorktreePool(alloc_branch="main")
    board = object()   # _repo_protected 없는 board 대역 → getattr None → 경고 생략
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp, board=board)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    out = capsys.readouterr().out
    assert "🚫" not in out
    assert "당신은 **X PM**" in out   # 정체성 선언 자체는 정상 surface


# ── 3. NeedsCreate (풀 소진) → 사용자 게이트·자동 생성 안 함 ───────────────────


def test_bootstrap_repo_needs_create_user_gate(bootstrap, tmp_path, capsys):
    wp = FakeWorktreePool(alloc_raises_needs_create=True)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    with pytest.raises(SystemExit) as exc:
        inst.run(repo="A", branch="a5")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    # 사용자 게이트 안내 — 풀 소진·수동 추가 안내·자동 안 함을 표면.
    assert "사용자 게이트" in err
    assert "풀 소진" in err
    assert "git worktree add" in err  # 안내 문구(자동 실행 아님).
    # release/추가 alloc 등 부작용 없음.
    assert wp.release_calls == []


# ── 4. worktree_pool 부재 시 명시 에러 (침묵 무력화 금지) ─────────────────────


def test_bootstrap_repo_without_pool_errors(bootstrap, tmp_path, capsys, monkeypatch):
    """--repo 줬는데 worktree_pool 이 없으면 명시 에러 (침묵 무력화 금지·ADR-0013)."""
    # 주입 None + 동적 로드도 None 으로 막는다.
    monkeypatch.setattr(bootstrap, "_load_worktree_pool", lambda: None)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=None)
    with pytest.raises(SystemExit) as exc:
        inst.run(repo="A", branch="a5")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "worktree_pool" in err


# ── 5. handoff payload slot/branch 기록 ───────────────────────────────────────


def test_handoff_skeleton_records_slot_branch(handoff):
    """multi-PM 모드 — handoff entry skeleton 에 slot/branch 기록 줄이 들어간다."""
    sk = handoff.build_handoff_log_skeleton(
        session_num=9, date="2026-06-16", worktree_slot="work/A_2", branch="a5",
    )
    assert "- worktree: slot=`work/A_2`" in sk
    assert "branch=`a5`" in sk


def test_handoff_skeleton_solo_omits_slot_line(handoff):
    """솔로면 worktree를 생략하고 자동 박제 목록 + 손-채움 신 스키마를 보존한다."""
    sk = handoff.build_handoff_log_skeleton(session_num=9, date="2026-06-16")
    assert "worktree: slot" not in sk
    assert "- 이 세션 박제 entries: (이 세션 박제 entry 없음)" in sk
    assert "- 메타 학습:" in sk
    assert f"- pending user intent: {handoff.PENDING_INTENT_PLACEHOLDER}" in sk
    assert "- 회귀/incident:" in sk
    assert "회귀 \"N passed / 상태\" 1줄" in sk
    assert "- 읽기 범위:" not in sk
    assert "대화 thread-tail" not in sk


# ── handoff fixture (DI — pytest/git stub·worktree_pool mock) ─────────────────


_PM_STATE_FIXTURE = """\
# PM State

## 세션 식별 (현재까지 사용된 이름)

최근 N 차 (sliding window, 기본 3 차):
  - **4차** (2026-06-14 · 직전 wave): 직전 PM 세션.
  - 이전 차 (PM 1차~3차) = `log/current.md` handoff entry 단일 진실.

## 진행 중인 의사결정
"""


def _make_handoff(handoff, tmp_path, *, worktree_pool=None, green_pytest=True):
    log_file = tmp_path / "current.md"
    state_file = tmp_path / "pm_state.md"
    playbook_file = tmp_path / "pm_playbook.md"
    log_file.write_text("# log\n", encoding="utf-8")
    state_file.write_text(_PM_STATE_FIXTURE, encoding="utf-8")
    playbook_file.write_text(
        "## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)\n\n```\n읽기 범위 / 메타 학습 / 다음 intent / 회귀/incident\n```\n",
        encoding="utf-8",
    )
    pytest_out = "5 passed in 0.1s" if green_pytest else "1 failed, 4 passed"
    inst = handoff.PmHandoff(
        run_pytest_fn=lambda: (0 if green_pytest else 1, pytest_out),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_state_file=state_file,
        pm_playbook_file=playbook_file,
        worktree_pool=worktree_pool,
    )
    return inst, log_file, state_file


# ── 6. handoff run --done → release ───────────────────────────────────────────


def test_handoff_done_releases_slot(handoff, tmp_path, capsys):
    """--done(작업완료) → worktree_pool.release 호출 (idle 반납·ADR-0013)."""
    wp = FakeWorktreePool()
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    rc = inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        worktree_slot="work/A_2", user_ack="A_2", branch="a5", done=True,
    )
    assert rc == 0
    # release 가 정확히 슬롯과 함께 호출됐다 (require_clean=False 자동경로).
    assert wp.release_calls == [{"slot": "work/A_2", "require_clean": False}]


def test_handoff_done_requires_slot(handoff, tmp_path, capsys):
    """--done 인데 슬롯 미지정 → 명시 에러(rc 1·release 안 함)."""
    wp = FakeWorktreePool()
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    rc = inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        worktree_slot=None, branch=None, done=True,
    )
    assert rc == 1
    assert wp.release_calls == []


def test_handoff_no_done_does_not_release(handoff, tmp_path, capsys):
    """--done 없이(세션종료/회전) handoff → release 안 함 (리스 유지·ADR-0013)."""
    wp = FakeWorktreePool()
    inst, log_file, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    rc = inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        worktree_slot="work/A_2", user_ack="A_2", branch="a5", done=False,
    )
    assert rc == 0
    # release 미호출 — 슬롯/브랜치는 handoff entry 에 기록만.
    assert wp.release_calls == []
    log_text = log_file.read_text(encoding="utf-8")
    assert "- worktree: slot=`work/A_2`" in log_text


def test_handoff_done_dry_run_does_not_release(handoff, tmp_path, capsys):
    """--done --dry-run → release 실행 안 함 (예고만)."""
    wp = FakeWorktreePool()
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    inst.run(
        session_num=5, wave_summary="x", dry_run=True, skip_pytest=True,
        worktree_slot="work/A_2", user_ack="A_2", branch="a5", done=True,
    )
    assert wp.release_calls == []


def test_handoff_done_release_keyerror_soft(handoff, tmp_path, capsys):
    """이미 release 된 슬롯(KeyError)은 무해하게 스킵(rc 0)."""
    wp = FakeWorktreePool()
    wp.release_raises_keyerror = True
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    rc = inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        worktree_slot="work/A_2", user_ack="A_2", branch="a5", done=True,
    )
    assert rc == 0


# ── 8. 회전 재부착(resume) 연속성 ─────────────────────────────────────────────


def test_bootstrap_resume_reattaches_same_stream(bootstrap, tmp_path, capsys):
    """--resume 으로 회전 재부착 — alloc 에 resume 이 전달돼 같은 작업스트림 복원."""
    wp = FakeWorktreePool(alloc_slot="work/A_2")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="A", resume="a5")
    assert wp.alloc_calls == [{"repo": "A", "branch": None, "resume": "a5"}]
    out = capsys.readouterr().out
    # 재부착된 브랜치(a5)가 identity surface 에 복원돼 표면된다.
    assert "a5" in out


# ── 9. CLI parser 배선 ────────────────────────────────────────────────────────


def test_bootstrap_parser_repo_flags(bootstrap):
    parser = bootstrap.build_parser()
    ns = parser.parse_args(["--repo", "A", "--branch", "a5"])
    assert ns.repo == "A" and ns.branch == "a5"
    ns2 = parser.parse_args(["--repo", "A", "--resume", "a5"])
    assert ns2.resume == "a5"
    # 솔로 무인자 — repo None·slot None.
    ns3 = parser.parse_args([])
    assert ns3.repo is None and ns3.slot is None


def test_bootstrap_parser_slot_flag(bootstrap):
    """--slot 은 정수 파싱(lean multi-PM 모드·T-0074)."""
    parser = bootstrap.build_parser()
    ns = parser.parse_args(["--repo", "X", "--slot", "2"])
    assert ns.repo == "X" and ns.slot == 2


def test_bootstrap_branch_without_repo_errors(bootstrap):
    """--branch 를 --repo 없이 주면 거부(오용 신호)."""
    with pytest.raises(SystemExit):
        bootstrap.main(["--branch", "a5"])


def test_bootstrap_slot_without_repo_errors(bootstrap):
    """--slot 을 --repo 없이 주면 거부(multi-PM 모드 전용·오용 신호)."""
    with pytest.raises(SystemExit):
        bootstrap.main(["--slot", "2"])


def test_bootstrap_slot_with_branch_rejected(bootstrap):
    """--slot(bind) + --branch(alloc) 동시 사용은 거부 — 둘은 배타 경로다."""
    with pytest.raises(SystemExit):
        bootstrap.main(["--repo", "X", "--slot", "2", "--branch", "b"])


def test_bootstrap_slot_with_resume_rejected(bootstrap):
    """--slot(bind) + --resume(alloc) 동시 사용은 거부 — 둘은 배타 경로다."""
    with pytest.raises(SystemExit):
        bootstrap.main(["--repo", "X", "--slot", "2", "--resume", "b"])


def test_bootstrap_slot_below_one_rejected(bootstrap):
    """--slot 0/음수는 거부 — 슬롯 번호는 1부터(work/<repo>_<N> 정합·codex 게이트)."""
    with pytest.raises(SystemExit):
        bootstrap.main(["--repo", "X", "--slot", "0"])
    with pytest.raises(SystemExit):
        bootstrap.main(["--repo", "X", "--slot", "-1"])


def test_handoff_parser_worktree_flags(handoff):
    """파서가 분해형 `--repo`/`--slot`(ADR-0057) + `--branch`/`--done` 조합을 받아들인다.

    `worktree_slot`(=`work/<repo>_<N>`)은 이제 parser.parse_args() 직후가 아니라 `main()`
    ingress 가 `_resolve_explicit_identity_slot` 로 파생하므로(T-0316), 이 parser-레벨 테스트는
    분해 필드(`repo`/`slot`)까지만 검증한다."""
    parser = handoff.build_parser()
    ns = parser.parse_args(
        ["--session-seq", "5", "--wave-summary", "x",
         "--repo", "A", "--slot", "2", "--branch", "a5", "--done"]
    )
    assert ns.repo == "A" and ns.slot == 2 and ns.branch == "a5" and ns.done is True


def test_handoff_branch_without_slot_rejected(handoff):
    """--branch 만(--repo/--slot 없이) → parser.error 거부 (조용히 무시 X·오용 축소).

    슬롯 없는 브랜치는 회전 재부착 단서로 불완전 — 어느 슬롯에 재부착할지 모른다.
    `--no-pytest`·`--dry-run` 을 함께 줘 가드가 없을 때(sensitivity)도 실 회귀·파일편집
    없이 정상 종료하게 한다 — 그러면 parser.error 미발생이 단언 실패로 즉시 드러난다.
    """
    with pytest.raises(SystemExit):
        handoff.main(
            ["--session-seq", "5", "--wave-summary", "x",
             "--branch", "a5", "--no-pytest", "--dry-run"],
            identity_resolver=lambda: (_ for _ in ()).throw(
                AssertionError("--branch 오류보다 identity 해소가 먼저 실행됨")
            ),
        )


def test_handoff_branch_with_slot_accepted_by_parser(handoff):
    """--branch + --repo/--slot(ADR-0057) 동반은 파서 통과 (main() 의 가드는 슬롯 없는 경우만 거부)."""
    parser = handoff.build_parser()
    ns = parser.parse_args(
        ["--session-seq", "5", "--wave-summary", "x",
         "--repo", "A", "--slot", "2", "--branch", "a5"]
    )
    assert ns.branch == "a5" and ns.repo == "A" and ns.slot == 2


# ── 10. sensitivity — 배선 무력화 시 fail 재현 ─────────────────────────────────


def test_sensitivity_done_must_release(handoff, tmp_path, capsys):
    """sensitivity: --done 이 release 를 호출하지 않으면(배선 무력화) 이 단언이 깨진다."""
    wp = FakeWorktreePool()
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        worktree_slot="work/A_2", user_ack="A_2", branch="a5", done=True,
    )
    # 배선이 살아있으면 release 가 정확히 1회. (무력화 시 0회 → fail.)
    assert len(wp.release_calls) == 1


def test_sensitivity_no_done_must_not_release(handoff, tmp_path, capsys):
    """sensitivity: --done 없는 handoff 가 실수로 release 를 호출하면(리스 파괴) 이 단언이 깨진다."""
    wp = FakeWorktreePool()
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        worktree_slot="work/A_2", branch="a5", done=False,
    )
    assert len(wp.release_calls) == 0


# ── 11. _auto_slot — 단일 self-host 자동바인딩 판정 (Part B) ──────────────────
# `_auto_slot(areas_file=, leases_file=)` 는 인자로 파일 seam 을 노출하므로 실 장부/areas
# 를 안 건드린다(hermetic·_registered_repos 가 areas.md 를 stdlib 로 읽는 것과 동형 패턴).

import json as _auto_json  # noqa: E402 — Part B 테스트 전용 로컬 import


def _write_areas(path: Path, repos: list[str]) -> None:
    """areas.md (신 스키마) — repo 행을 repos 개수만큼. 빈 리스트면 헤더만."""
    lines = [
        "| repo | prefix | git | test_cmd | owner | base | protected |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in repos:
        lines.append(f"| {r} | {r} |  |  | alice |  |  |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_leases(path: Path, entries: list[dict]) -> None:
    """worktree-leases.json — {"leases": [...]} 스키마 (worktree_pool.Lease.to_dict 와 동형)."""
    path.write_text(_auto_json.dumps({"leases": entries}), encoding="utf-8")


def _identity(key: str, slot: str):
    """`_auto_slot` 해소 결과 대역 — 엔진의 실제 `identity_args.SlotIdentity` 타입 그대로.

    정체성 키(`key`)와 장부 경로 값(`slot`)을 분리해 나르는 계약을 테스트에서도 값으로 쓴다
    (튜플 재조립 금지). 대역이 필요한 곳은 장부 없이 해소 결과만 주입하는 monkeypatch 지점뿐이고,
    해소 자체를 보는 테스트는 실 장부 파일로 판정한다.
    """
    ia = _load("identity_args")
    return ia.SlotIdentity(key=key, slot=slot,
                           source=ia.IDENTITY_FROM_LEDGER_SESSION, session=key)


def test_auto_slot_single_repo_single_slot_returns_identity(bootstrap, tmp_path):
    """등록 repo 정확히 1개 + 그 repo 슬롯 정확히 1개 → 그 행의 정체성(키 + 장부 경로 값)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    resolved = bootstrap._auto_slot(areas_file=areas, leases_file=leases)
    assert (resolved.key, resolved.slot) == ("project_manager_1", "work/project_manager_1")
    assert (resolved.repo, resolved.number) == ("project_manager", 1)


def test_auto_slot_zero_repos_returns_none(bootstrap, tmp_path):
    """등록 repo 0개(레지스트리 부재) → None (현행 솔로)."""
    areas = tmp_path / "areas.md"   # 미생성 → 부재
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) is None


def test_auto_slot_two_repos_returns_none(bootstrap, tmp_path):
    """등록 repo 2개(모호·진짜 multi-PM) → None (사용자가 --repo --slot 명시)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["A", "B"])
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
    ])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) is None


def test_auto_slot_two_slots_returns_none(bootstrap, tmp_path):
    """등록 repo 1개지만 그 repo 슬롯 2개(모호) → None."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
        {"slot": "work/project_manager_2", "repo": "project_manager",
         "session": "project_manager_2", "state": "leased"},
    ])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) is None


def test_auto_slot_missing_leases_returns_none(bootstrap, tmp_path):
    """등록 repo 1개지만 lease 장부 부재 → None (슬롯 0개·fail-soft)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"  # 미생성 → 부재
    _write_areas(areas, ["project_manager"])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) is None


def test_auto_slot_corrupt_leases_returns_none(bootstrap, tmp_path):
    """등록 repo 1개 + 깨진 JSON 장부 → None (fail-soft·크래시 안 함)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    leases.write_text("{not valid json", encoding="utf-8")
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) is None


def test_auto_slot_schema_mismatch_returns_none(bootstrap, tmp_path):
    """유효 JSON 이지만 dict/leases 리스트 아님 → None (fail-soft)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    leases.write_text(_auto_json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) is None


def test_auto_slot_slot_for_other_repo_returns_none(bootstrap, tmp_path):
    """등록 repo 1개지만 장부 슬롯이 *다른* repo 것뿐 → None (그 repo 슬롯 0개)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/other_1", "repo": "other", "session": "other_1", "state": "leased"},
    ])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) is None


def test_auto_slot_parses_nonone_slot_number(bootstrap, tmp_path):
    """슬롯 N 이 1이 아닌 값(예 3)도 정확히 파싱한다 → (repo, 3)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_3", "repo": "project_manager",
         "session": "project_manager_3", "state": "leased"},
    ])
    resolved = bootstrap._auto_slot(areas_file=areas, leases_file=leases)
    assert (resolved.key, resolved.slot) == ("project_manager_3", "work/project_manager_3")


# ── 11b. _resolve_session_slot — guarded 슬롯해소 (default-1 + fail-loud·T-0178) ──
# `_auto_slot`(순수 resolver·"정확히 1 슬롯") 과 달리 session-entry 용 — repo-안 default-1
# (slot1>단독>fail-loud). solo(멀티-PM 미셋업)는 None(fail-soft), ambiguous(under-specified)
# 는 SlotResolutionError 로 fail-loud. _write_areas/_write_leases hermetic seam 재사용.


def _lease(repo: str, n: int, state: str = "leased") -> dict:
    """worktree-leases.json 엔트리 1개 (`work/<repo>_<N>`·기본 leased·idle 회귀용 state 인자)."""
    return {"slot": f"work/{repo}_{n}", "repo": repo, "session": f"{repo}_{n}", "state": state}


def _resolve(bootstrap, tmp_path, repos: list[str], lease_entries: list[dict] | None):
    """areas/leases 파일 seam 을 깔고 _resolve_session_slot 을 hermetic 하게 호출한다.

    `lease_entries=None` 이면 장부 파일을 *만들지 않는다*(부재). 빈 리스트면 빈 장부.
    """
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, repos)
    if lease_entries is not None:
        _write_leases(leases, lease_entries)
    return bootstrap._resolve_session_slot(areas_file=areas, leases_file=leases)


def test_resolve_session_slot_single_self_host(bootstrap, tmp_path):
    """repo 1개 + 슬롯 `{1}` → (repo, 1) (현행 단일 self-host 보존)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 1)])
    assert got == ("project_manager", 1)


def test_resolve_session_slot_default_1_when_slot1_present(bootstrap, tmp_path):
    """repo 1개 + 슬롯 `{1,2}` → (repo, 1) (slot1 존재 → default-1·모호 아님)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 1), _lease("project_manager", 2)])
    assert got == ("project_manager", 1)


def test_resolve_session_slot_sole_non1_slot(bootstrap, tmp_path):
    """repo 1개 + 슬롯 `{3}`(단독·1 아님) → (repo, 3) (단독 규칙·현행 `_3`-only 보존)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 3)])
    assert got == ("project_manager", 3)


def test_resolve_session_slot_zero_repos_returns_none(bootstrap, tmp_path):
    """등록 repo 0개(멀티-PM 미셋업) → None (solo·fail-soft·bare bootstrap 무변경)."""
    got = _resolve(bootstrap, tmp_path, [], [_lease("project_manager", 1)])
    assert got is None


def test_resolve_session_slot_repo1_no_slots_returns_none(bootstrap, tmp_path):
    """repo 1개지만 그 repo 슬롯 0개(셋업 미완) → None (solo·fail-soft)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"], [])
    assert got is None


def test_resolve_session_slot_missing_leases_returns_none(bootstrap, tmp_path):
    """repo 1개 + 장부 부재 → None (solo·fail-soft·_auto_slot None 동형)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"], None)
    assert got is None


def test_resolve_session_slot_corrupt_leases_returns_none(bootstrap, tmp_path):
    """repo 1개 + 깨진 JSON 장부 → None (fail-soft·크래시 안 함)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    leases.write_text("{not valid json", encoding="utf-8")
    assert bootstrap._resolve_session_slot(areas_file=areas, leases_file=leases) is None


def test_resolve_session_slot_two_repos_fails_loud(bootstrap, tmp_path):
    """등록 repo ≥2 (no --repo) → SlotResolutionError (fail-loud·--repo 안내)."""
    with pytest.raises(bootstrap.SlotResolutionError) as exc:
        _resolve(bootstrap, tmp_path, ["A", "B"], [_lease("A", 1)])
    msg = str(exc.value)
    assert "repo 2개" in msg
    assert "--repo" in msg


def test_resolve_session_slot_slot1_absent_nonsole_fails_loud(bootstrap, tmp_path):
    """repo 1개 + 슬롯 `{2,3}`(1 부재·비단독) → SlotResolutionError (fail-loud·--slot 안내)."""
    with pytest.raises(bootstrap.SlotResolutionError) as exc:
        _resolve(bootstrap, tmp_path, ["project_manager"],
                 [_lease("project_manager", 2), _lease("project_manager", 3)])
    msg = str(exc.value)
    assert "슬롯 2개" in msg
    assert "--slot" in msg


def test_resolve_session_slot_no_silent_fallback_sensitivity(bootstrap, tmp_path):
    """sensitivity — 모호 케이스가 *조용히* (repo,N)/None 으로 폴백하면 fail.

    `{2,3}`(1 부재·비단독)은 명시 에러여야 한다. 침묵 폴백(returns 대신)이면 이 테스트가
    잡는다 — fail-loud 가 실제로 발화함을 입증(에러 안 나면 실패)."""
    raised = False
    try:
        _resolve(bootstrap, tmp_path, ["project_manager"],
                 [_lease("project_manager", 2), _lease("project_manager", 3)])
    except bootstrap.SlotResolutionError:
        raised = True
    assert raised, "모호 케이스가 명시 에러 없이 조용히 폴백했다 (침묵 무력화 회귀)"


# ── 11c. idle(반납) 슬롯 필터 — leased 만 라우팅 (codex must-fix·ADR-0035 활성 연속성) ──
# default-1 이 permissive 해지며 idle 슬롯으로 라우팅하던 결함을 닫는다 — `_repo_slot_numbers`
# 가 state=="leased" 만 센다. idle 은 죽은 세션이라 자동바인딩/연속성 대상이 아니다.


def test_resolve_session_slot_idle_slot1_routes_to_leased_slot2(bootstrap, tmp_path):
    """`{1:idle, 2:leased}` → (repo, 2) — idle 슬롯1 아니라 *활성* 슬롯2 (핵심 must-fix)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 1, "idle"),
                    _lease("project_manager", 2, "leased")])
    assert got == ("project_manager", 2)


def test_resolve_session_slot_idle_slot2_keeps_leased_slot1(bootstrap, tmp_path):
    """`{1:leased, 2:idle}` → (repo, 1) — 활성 슬롯1 (idle 2 제외·default-1)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 1, "leased"),
                    _lease("project_manager", 2, "idle")])
    assert got == ("project_manager", 1)


def test_resolve_session_slot_both_leased_default_1(bootstrap, tmp_path):
    """`{1:leased, 2:leased}`(둘 다 활성) → (repo, 1) — default-1 의도 유지."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 1, "leased"),
                    _lease("project_manager", 2, "leased")])
    assert got == ("project_manager", 1)


def test_resolve_session_slot_all_idle_returns_none(bootstrap, tmp_path):
    """`{1:idle, 2:idle}`(활성 없음) → None (fail-soft·활성 세션 부재·솔로 폴백)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 1, "idle"),
                    _lease("project_manager", 2, "idle")])
    assert got is None


def test_resolve_session_slot_solo_single_leased_unchanged(bootstrap, tmp_path):
    """solo `{1:leased}` → (repo, 1) — 단일 활성 슬롯 불변(idle 필터가 solo 안 깸·재확인)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 1, "leased")])
    assert got == ("project_manager", 1)


def test_resolve_session_slot_duplicate_entries_dedup(bootstrap, tmp_path):
    """같은 슬롯 N 중복 장부 엔트리(2행) → "1 슬롯" 으로 정상 (dedup·codex suggestion)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [_lease("project_manager", 3, "leased"),
                    _lease("project_manager", 3, "leased")])
    assert got == ("project_manager", 3)  # dedup 없으면 "슬롯 2개"→fail-loud 오진.


def test_resolve_session_slot_state_absent_treated_leased(bootstrap, tmp_path):
    """state 키 부재 엔트리 → leased 로 취급 (back-compat·worktree_pool from_dict default)."""
    got = _resolve(bootstrap, tmp_path, ["project_manager"],
                   [{"slot": "work/project_manager_1", "repo": "project_manager",
                     "session": "project_manager_1"}])  # state 키 없음.
    assert got == ("project_manager", 1)


# ── _auto_slot idle 필터 영향 (공유 헬퍼·의도된 변화·codex 영향 분석) ──────────

def test_auto_slot_idle_slot1_resolves_leased_slot2(bootstrap, tmp_path):
    """`{1:idle, 2:leased}` → _auto_slot 도 leased={2}→exactly-1→(repo, 2) (의도된 변화).

    이전엔 2개 엔트리→None→폴백이었으나, idle 필터로 *활성* 슬롯만 세 exactly-1 해소된다 —
    incidental(`_regression_cwd`·display)이 활성 슬롯을 찾는 것이라 정합·개선."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [_lease("project_manager", 1, "idle"),
                           _lease("project_manager", 2, "leased")])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases).key == "project_manager_2"


def test_auto_slot_solo_single_leased_unchanged(bootstrap, tmp_path):
    """solo `{1:leased}` → (repo, 1) — idle 필터가 solo 핵심 케이스 안 깸(재확인)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [_lease("project_manager", 1, "leased")])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases).key == "project_manager_1"


def test_auto_slot_all_idle_returns_none(bootstrap, tmp_path):
    """`{1:idle}`(활성 0개) → None — _auto_slot 도 활성만 센다(fail-soft·솔로 폴백)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [_lease("project_manager", 1, "idle")])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) is None


# ── 12. main() 자동 세팅 분기 — 둘 다 None 일 때만 guarded 해소 적용 (T-0178) ──
# PmBootstrap.run 을 stub 으로 갈아끼워 받은 repo/slot 인자만 캡처한다(실 worktree_pool
# 동적로드·git/장부 미접촉). main() 은 `_resolve_session_slot`(guarded·default-1+fail-loud)
# 을 부르므로 그걸 monkeypatch 로 결정값/예외 주입한다(분기만 검증).


class _CaptureBootstrap:
    """run() 이 받은 repo/slot 을 클래스 변수에 캡처하는 stub (실행 부작용 없음)."""
    last: dict | None = None

    def run(self, **kwargs):
        type(self).last = kwargs
        return 0


def _patch_main_stub(bootstrap, monkeypatch, auto_result):
    """main() 의 _resolve_session_slot 을 결정값으로, PmBootstrap 을 캡처 stub 으로 교체."""
    _CaptureBootstrap.last = None
    monkeypatch.setattr(bootstrap, "_resolve_session_slot", lambda: auto_result)
    monkeypatch.setattr(bootstrap, "PmBootstrap", _CaptureBootstrap)


def test_main_auto_binds_when_both_none(bootstrap, monkeypatch, capsys):
    """무인자(repo/slot 둘 다 None) + 해소가 (repo,N) → run 에 그 값이 전달."""
    _patch_main_stub(bootstrap, monkeypatch, ("project_manager", 2))
    rc = bootstrap.main([])
    assert rc == 0
    assert _CaptureBootstrap.last["repo"] == "project_manager"
    assert _CaptureBootstrap.last["slot"] == 2
    assert "슬롯 자동 해소" in capsys.readouterr().err


def test_main_no_auto_when_resolve_none(bootstrap, monkeypatch, capsys):
    """무인자 + 해소가 None(solo) → 현행 솔로 (repo/slot 둘 다 None 유지·안내 없음)."""
    _patch_main_stub(bootstrap, monkeypatch, None)
    rc = bootstrap.main([])
    assert rc == 0
    assert _CaptureBootstrap.last["repo"] is None
    assert _CaptureBootstrap.last["slot"] is None
    assert "슬롯 자동 해소" not in capsys.readouterr().err


def test_main_ambiguous_fails_loud(bootstrap, monkeypatch, capsys):
    """무인자 + 해소가 SlotResolutionError(멀티-PM 모호) → 명시 에러로 exit (침묵 폴백 부재)."""
    _CaptureBootstrap.last = None
    monkeypatch.setattr(bootstrap, "PmBootstrap", _CaptureBootstrap)

    def _raise():
        raise bootstrap.SlotResolutionError("등록 repo 2개(A, B) — --repo <name> --slot <N> 으로 명시하라.")

    monkeypatch.setattr(bootstrap, "_resolve_session_slot", _raise)
    with pytest.raises(SystemExit):
        bootstrap.main([])
    # argparse error 는 SystemExit(2)·stderr 로 안내. run() 은 호출되지 않는다(침묵 폴백 부재).
    assert _CaptureBootstrap.last is None
    assert "등록 repo 2개" in capsys.readouterr().err


def test_main_explicit_slot_skips_auto(bootstrap, monkeypatch, capsys):
    """명시 --repo --slot 경로는 해소 분기를 타지 않는다 (해소가 던져도 무시)."""
    # _resolve_session_slot 이 호출되면 예외로 오염시켜, 호출 안 됨을 확인.
    monkeypatch.setattr(bootstrap, "_resolve_session_slot",
                        lambda: (_ for _ in ()).throw(AssertionError("해소 호출되면 안 됨")))
    monkeypatch.setattr(bootstrap, "PmBootstrap", _CaptureBootstrap)
    _CaptureBootstrap.last = None
    rc = bootstrap.main(["--repo", "A", "--slot", "1"])
    assert rc == 0
    assert _CaptureBootstrap.last["repo"] == "A"
    assert _CaptureBootstrap.last["slot"] == 1


# ── 13. _worktree_cwd — git/pytest 러너 worktree cwd 자동해소 (T-0125·T-0124 동형) ─
# `_worktree_cwd(slot=)` 는 명시 slot > `_auto_slot()` > REPO 순으로 해소한다. _auto_slot
# 은 areas/leases 파일 seam 으로 hermetic(위 Part B 헬퍼 _write_areas/_write_leases 재사용).
# 자동해소 경로는 _auto_slot 을 monkeypatch 로 결정값 주입(REPO 상수 의존 회피).


def test_worktree_cwd_explicit_slot_wins(bootstrap):
    """명시 slot(`work/<repo>_<N>`) 이 최우선 — REPO/slot 으로 끝난다 (_auto_slot 무시)."""
    inst = bootstrap.PmBootstrap()
    cwd = inst._worktree_cwd("work/foo_2")
    assert cwd == str(bootstrap.REPO / "work/foo_2")
    assert cwd.replace(os.sep, "/").endswith("work/foo_2")


def test_worktree_cwd_single_selfhost_resolves_slot(bootstrap, tmp_path, monkeypatch):
    """단일 self-host (1 repo + 1 슬롯) → _auto_slot 해소 → REPO/work/<repo>_<N> 로 끝난다."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    # _auto_slot 을 이 hermetic 파일 seam 으로 해소하도록 고정 (실 장부 미접촉).
    real_auto = bootstrap._auto_slot
    monkeypatch.setattr(bootstrap, "_auto_slot",
                        lambda: real_auto(areas_file=areas, leases_file=leases))
    inst = bootstrap.PmBootstrap()
    cwd = inst._worktree_cwd()
    assert cwd == str(bootstrap.REPO / "work/project_manager_1")
    assert cwd.replace(os.sep, "/").endswith("work/project_manager_1")


def test_worktree_cwd_no_slot_falls_back_to_repo(bootstrap, monkeypatch):
    """_auto_slot 이 None(0/2 repo·2 슬롯·부재·모호)면 REPO 폴백 (솔로 무변경)."""
    monkeypatch.setattr(bootstrap, "_auto_slot", lambda: None)
    inst = bootstrap.PmBootstrap()
    assert inst._worktree_cwd() == str(bootstrap.REPO)
    assert inst._worktree_cwd(None) == str(bootstrap.REPO)


def test_worktree_cwd_auto_slot_exception_falls_back_to_repo(bootstrap, monkeypatch):
    """_auto_slot 이 예외를 던져도 흡수해 REPO 폴백 (fail-soft — 자동해소는 추가 편의)."""
    monkeypatch.setattr(bootstrap, "_auto_slot",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    inst = bootstrap.PmBootstrap()
    assert inst._worktree_cwd() == str(bootstrap.REPO)


def test_default_git_pytest_cwd_is_worktree_but_board_is_repo(bootstrap, monkeypatch):
    """분리 회귀 가드 — git/pytest 기본 러너 cwd=worktree, board 기본 러너 cwd=REPO.

    자기분리(ADR-0027): 코드/tests=① worktree·board/wiki=② 홈. 세 기본 러너의 subprocess
    cwd 를 캡처해 git·pytest 는 worktree 슬롯, board 는 REPO 임을 단언한다(러너별 cwd 분리).
    subprocess.run 을 fake 로 갈아 실 git/pytest/board 를 절대 부르지 않는다(hermetic).
    """
    captured: dict[str, list[str]] = {"git": [], "pytest": [], "board": []}

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        cwd = kwargs.get("cwd")
        if argv[0] == "git":
            captured["git"].append(cwd)
        elif "pytest" in argv:
            captured["pytest"].append(cwd)
        else:
            captured["board"].append(cwd)
        return _FakeCompleted()

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    # _auto_slot 을 단일 self-host 로 고정 → worktree cwd 가 결정된다.
    monkeypatch.setattr(
        bootstrap, "_auto_slot",
        lambda: _identity("project_manager_1", "work/project_manager_1"))
    inst = bootstrap.PmBootstrap()

    inst._default_run_git(["status"])
    inst._default_run_pytest()
    inst._default_run_board(["list"])

    worktree = str(bootstrap.REPO / "work/project_manager_1")
    assert captured["git"] == [worktree], "git 러너 cwd 가 worktree 가 아님"
    assert captured["pytest"] == [worktree], "pytest 러너 cwd 가 worktree 가 아님"
    # board 는 ②(PM 홈) 소유라 REPO 고정 — worktree 가 아님(분리 가드).
    assert captured["board"] == [str(bootstrap.REPO)], "board 러너 cwd 가 REPO 가 아님"
    assert captured["board"][0] != worktree


def test_default_git_uses_bound_slot_when_set(bootstrap, monkeypatch):
    """명시 multi-PM 바인딩(self._bound_slot) → git 기본 러너가 그 슬롯 worktree cwd 를 쓴다."""
    captured: list[str] = []

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        if argv[0] == "git":
            captured.append(kwargs.get("cwd"))
        return _FakeCompleted()

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    # _auto_slot 이 호출되면 안 된다 — 명시 _bound_slot 이 우선.
    monkeypatch.setattr(bootstrap, "_auto_slot",
                        lambda: (_ for _ in ()).throw(AssertionError("명시 slot 인데 auto 호출됨")))
    inst = bootstrap.PmBootstrap()
    inst._bound_slot = "work/billing_3"
    inst._default_run_git(["status"])
    assert captured == [str(bootstrap.REPO / "work/billing_3")]


# ── 14. run() 진입부 _bound_slot 스레딩 (순서 함정 — 수집 전 세팅) ─────────────
# 명시 --repo --slot multi-PM 경로에서 run() 이 _collect_git/_collect_pytest *전*에
# self._bound_slot 을 세팅하는지 검증한다. 주입 git_fn 으로 호출 시점의 _bound_slot 을
# 캡처해, git 수집이 worktree cwd 를 쓸 수 있는 상태였는지 확인한다.


def test_run_sets_bound_slot_before_git_collection(bootstrap, tmp_path, capsys):
    """명시 --repo --slot 시 _collect_git 호출 시점에 _bound_slot 이 이미 세팅돼 있다(순서 함정 해소)."""
    seen: dict[str, str | None] = {}

    def capturing_git(args):
        # git 수집 시점의 _bound_slot 을 캡처 — 바인딩이 수집보다 먼저면 값이 잡힌다.
        seen["bound_slot"] = inst._bound_slot
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 commit subject\n"
        return 0, ""

    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    # 기본 git stub 대신 캡처 git_fn 으로 교체.
    inst._run_git_fn = capturing_git
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    # git 수집 시점에 _bound_slot = work/X_2 (명시 multi-PM 슬롯 식별자).
    assert seen["bound_slot"] == "work/X_2"


def test_run_solo_leaves_bound_slot_none(bootstrap, tmp_path, capsys):
    """솔로(무인자) run 은 _bound_slot 을 None 으로 유지(→ _worktree_cwd 가 _auto_slot 해소)."""
    seen: dict[str, str | None] = {}

    def capturing_git(args):
        seen["bound_slot"] = inst._bound_slot
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 commit subject\n"
        return 0, ""

    wp = FakeWorktreePool()
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst._run_git_fn = capturing_git
    rc = inst.run()  # 솔로
    assert rc == 0
    assert seen["bound_slot"] is None


# ── F1 --task 부트스트랩 (신규/resume·prefix surface·㉑ 동시세션 거부·T-0353) ──


def test_bootstrap_task_new_surfaces_identity(bootstrap, tmp_path, capsys):
    """`--task` 신규 — task identity surface(정체성·prefix 없음·상태=신규) dump·rc0."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks", task_action="created")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(task="payments-refactor")
    assert rc == 0
    assert [c["name"] for c in wp.bind_task_calls] == ["payments-refactor"]
    # 예약명 이중화(should-fix) — 엔진 진입점에 등록 repo 집합이 전달된다.
    assert wp.bind_task_calls[0]["registered_repos"] is not None
    out = capsys.readouterr().out
    assert "task identity surface" in out
    assert "task `payments-refactor`" in out
    assert "신규 task" in out


def test_bootstrap_task_prefix_surface_default_none(bootstrap, tmp_path, capsys):
    """prefix 상태 surface — 기본 없음(①ⓑ) 이면 '(없음)' 표시."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks", task_prefix=None)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(task="job1")
    out = capsys.readouterr().out
    assert "prefix 상태 = (없음)" in out
    assert "task prefix" in out   # 변경 명령 pointer(T-0357)


def test_bootstrap_task_prefix_surface_shows_value(bootstrap, tmp_path, capsys):
    """지정된 prefix 는 값으로 surface(기본 없음 대비)."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks",
                          task_prefix="PAY", task_action="resumed")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(task="job1")
    out = capsys.readouterr().out
    assert "prefix=`PAY`" in out
    assert "재개(resume)" in out


def test_bootstrap_task_resume_reads_pm_state_pointer(bootstrap, tmp_path, capsys):
    """resume — 서술 pm_state 포인터 surface(읽기 경로·쓰기는 T-0356).

    포인터 줄은 엔진 렌더(`_render_task_pm_state_pointer`)로 대조한다 — 문구/경로 표기를 손으로
    재타이핑하면 렌더가 바뀔 때 테스트만 옛 형식에 남는다."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks", task_action="resumed")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(task="job1")
    out = capsys.readouterr().out
    assert bootstrap._render_task_pm_state_pointer(
        tmp_path / "tasks" / "job1" / "pm_state.md") in out


def test_bootstrap_task_active_elsewhere_rejects_before_dump(bootstrap, tmp_path, capsys):
    """㉑ — 살아있는 다른 세션 점유면 rc1 로 거부하고 **부분 dump 도 금지**(⑧ 동형)."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks", task_raises_pid=4242)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(task="job1")
    assert rc == 1
    captured = capsys.readouterr()
    # stderr 에 거부 안내(pid) — stdout(dump)엔 board/task surface 가 없다(부분 dump 금지).
    assert "다른 살아있는 세션" in captured.err and "4242" in captured.err
    assert "task identity surface" not in captured.out
    assert "board" not in captured.out.lower() or captured.out.strip() == ""


@pytest.mark.parametrize(
    "identity",
    [
        {"repo": "A"},
        {"repo": "A", "slot": 2},
        {"branch": "feature"},
        {"resume": "feature"},
    ],
)
def test_bootstrap_task_rejects_slot_identity_mixing(
    bootstrap, tmp_path, capsys, identity
):
    """task Python 진입은 `--task`만 허용하고 repo/slot/branch/resume 혼합을 부작용 전에 막는다."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)

    rc = inst.run(task="job1", **identity)

    assert rc == 1
    captured = capsys.readouterr()
    assert "`--task <이름>`만 받는다" in captured.err
    assert wp.bind_task_calls == []
    assert wp.bind_calls == []


def test_bootstrap_task_json_includes_task(bootstrap, tmp_path, capsys):
    """--json 출력에 task 키(name·prefix·action) 포함."""
    import json as _json
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks", task_prefix="PAY",
                          task_action="resumed")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(task="job1", output_json=True)
    data = _json.loads(capsys.readouterr().out)
    assert data["task"]["name"] == "job1"
    assert data["task"]["prefix"] == "PAY"
    assert data["task"]["action"] == "resumed"


def test_bootstrap_no_task_omits_task_surface(bootstrap, tmp_path, capsys):
    """`--task` 미지정(기존 --repo alloc)엔 task surface 가 안 뜬다(100% 불변·⑥)."""
    wp = FakeWorktreePool(alloc_slot="work/A_2", alloc_branch="a5")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="A", branch="a5")
    out = capsys.readouterr().out
    assert "task identity surface" not in out
    assert wp.bind_task_calls == []


def test_bootstrap_task_reclaimed_emits_loud_notice(bootstrap, tmp_path, capsys):
    """㉑ 정직화 — dead-pid 회수(reclaimed·이전 pid>0) 진입 시 loud notice(다른 창 작업중 가능)."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks",
                          task_action="reclaimed", task_reclaimed_from=54321)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(task="job1")
    out = capsys.readouterr().out
    assert "회수 진입" in out
    assert "54321" in out
    assert "다른 창에서 아직" in out


def test_bootstrap_task_created_has_no_reclaim_notice(bootstrap, tmp_path, capsys):
    """created/resumed(회수 아님)엔 notice 가 안 뜬다(오탐 0·reclaimed_from None)."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks",
                          task_action="created", task_reclaimed_from=None)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(task="job1")
    out = capsys.readouterr().out
    assert "회수 진입" not in out


def test_bootstrap_task_invalid_name_rejected_rc1(bootstrap, tmp_path, capsys):
    """엔진 명 검증 거부(InvalidTaskName) → rc1·부분 dump 금지(must-fix)."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks",
                          task_invalid_reason="path separator(`/`·`\\`) 불가 — 단일 이름이어야")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(task="../../evil")
    assert rc == 1
    captured = capsys.readouterr()
    assert "부적합" in captured.err
    assert "task identity surface" not in captured.out


# ── F7 resume-read 소비 배선 (task 태그 귀속·task pm_state 본문 dump·T-0374) ──────
# T-0356 이 낸 log 태그 파서(`(task:<이름>)`)+handoff task pm_state 쓰기를 부트스트랩 dump 가
# 직접 소비하는지 검증한다. slot/solo 불변은 기존 441 테스트(this 파일 + per_slot)가 담보.


def _task_read_bootstrap(bootstrap, tmp_path, *, action="resumed", log_text=None):
    """task resume-read 부트스트랩 — 주입 pm_state 를 우회(_pm_state_file=None)해 task 서술
    pm_state(`.local/tasks/<이름>/pm_state.md`) 실해소 경로를 태운다(F7). log 는 원하면 주입."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks", task_action=action)
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst._pm_state_file = None  # 주입 우회 → task pm_state 해소 경로 진입(F7·T-0374)
    if log_text is not None:
        inst._log_file.write_text(log_text, encoding="utf-8")
    return inst, wp


def _write_task_pm_state(tmp_path, name, text):
    """task 서술 pm_state 파일을 fake task_dir(tmp_path/tasks/<name>)에 쓴다."""
    d = tmp_path / "tasks" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "pm_state.md").write_text(text, encoding="utf-8")


def test_bound_session_name_task_returns_tag(bootstrap, tmp_path):
    """`_bound_session_name` = `task:<이름>` (log/pm_state 귀속)·`_slot_session_name` 은 task-무관."""
    inst = _make_bootstrap(bootstrap, tmp_path,
                           worktree_pool=FakeWorktreePool(task_dir_root=tmp_path / "tasks"))
    inst._task_name = "payments-refactor"
    assert inst._bound_session_name() == "task:payments-refactor"
    # 슬롯 스코프(board/lease/대시보드)는 task 태그를 안 본다 — task-무관 슬롯 정체성.
    assert not (inst._slot_session_name() or "").startswith("task:")


def test_task_resume_dumps_pm_state_body(bootstrap, tmp_path, capsys):
    """resume — task 서술 pm_state 본문(차수 추론 + '남은 작업' 절)을 dump(포인터가 아닌 소비)."""
    _write_task_pm_state(
        tmp_path, "job1",
        "# pm_state\n\n"
        "## 세션 식별 (현재까지 사용된 이름)\n"
        "  - **3차** job1 세션\n"
        "  - **2차** job1 세션\n\n"
        "## 남은 작업 전체 그림\n"
        "- [ ] TASK-남은작업-표식-A\n",
    )
    inst, _ = _task_read_bootstrap(bootstrap, tmp_path, action="resumed")
    rc = inst.run(task="job1")
    assert rc == 0
    out = capsys.readouterr().out
    # 차수 추론(3차 → 4차)·남은작업 절 본문이 실제로 dump(포인터만이 아니라 소비).
    assert "## PM 4차 부트스트랩" in out
    assert "TASK-남은작업-표식-A" in out


def test_task_first_session_has_state_and_announces_session_one(bootstrap, tmp_path, capsys):
    """신규 task는 bind 즉시 state가 생기고 완료 이력 0 marker에서 현재 1차를 추론한다."""
    inst, _ = _task_read_bootstrap(bootstrap, tmp_path, action="created")
    rc = inst.run(task="job1")
    assert rc == 0
    out = capsys.readouterr().out
    assert bootstrap._render_task_pm_state_pointer(
        tmp_path / "tasks" / "job1" / "pm_state.md") in out
    assert "🆕 첫 바인딩 슬롯" not in out
    assert "## PM 1차 부트스트랩" in out


def test_task_tag_log_entry_attributed_and_session_inferred(bootstrap, tmp_path, capsys):
    """task 태그 handoff entry(`(task:job1)`) 를 자기 것으로 귀속·차수 추론(3차→4차)·본문 dump."""
    log = (
        "# log\n\n"
        "## [2026-07-18] handoff | PM 3차 (task:job1) → 다음 PM 세션\n"
        "- 남은작업: TASK태그-본문-표식\n"
    )
    inst, _ = _task_read_bootstrap(bootstrap, tmp_path, action="resumed", log_text=log)
    rc = inst.run(task="job1")  # task pm_state 부재 — 차수는 log 태그 entry 에서만 추론.
    assert rc == 0
    out = capsys.readouterr().out
    assert "## PM 4차 부트스트랩" in out              # log 태그 entry 차수 추론(3→4)
    assert "TASK태그-본문-표식" in out                # 자기 태그 entry 본문 dump(귀속)


def test_task_bootstrap_uses_log_when_state_lags_after_interrupted_handoff(
    bootstrap, tmp_path, capsys
):
    """task state가 1차에 머물고 log만 3차면 공통 복구 규칙으로 4차를 announce한다."""
    _write_task_pm_state(
        tmp_path,
        "job1",
        "# pm_state\n\n"
        "## 세션 식별 (현재까지 사용된 이름)\n"
        "  - **1차** stale state\n",
    )
    log = (
        "# log\n\n"
        "## [2026-07-18] handoff | PM 3차 (task:job1) → 다음 PM 세션\n"
        "- 남은작업: log-only 중단 복구\n"
    )
    inst, _ = _task_read_bootstrap(bootstrap, tmp_path, action="resumed", log_text=log)

    rc = inst.run(task="job1")

    assert rc == 0
    out = capsys.readouterr().out
    assert "## PM 4차 부트스트랩" in out
    assert "pm_state 는 PM 2차" in out


def test_task_ignores_untagged_and_slot_tagged_log(bootstrap, tmp_path, capsys):
    """task 세션은 무태그(솔로/slot-1)·타 슬롯 태그 handoff 를 자기 것으로 오귀속하지 않는다."""
    log = (
        "# log\n\n"
        "## [2026-07-12] handoff | PM 7차 → 다음 PM 세션\n"
        "- 남은작업: 무태그-타슬롯-표식\n\n"
        "## [2026-07-13] handoff | PM 5차 (project_manager_1) → 다음 PM 세션\n"
        "- 남은작업: 슬롯태그-표식\n"
    )
    inst, _ = _task_read_bootstrap(bootstrap, tmp_path, action="created", log_text=log)
    rc = inst.run(task="job1")  # task 태그 entry 0개 → 자기 컨텍스트 없음.
    assert rc == 0
    out = capsys.readouterr().out
    # 무태그 PM 7차·슬롯태그 PM 5차 를 자기 차수/본문으로 흡수하지 않는다(귀속 격리).
    assert "## PM 7차 부트스트랩" not in out
    assert "## PM 5차 부트스트랩" not in out
    assert "무태그-타슬롯-표식" not in out
    assert "슬롯태그-표식" not in out


# ── task pm_state 포인터의 경로 표기 (Windows 분기를 Linux 에서 태운다·T-0718) ──────
# 구분자는 OS 가 정하므로(`str(WindowsPath)` → `\`) Linux 회귀는 그대로면 Windows 분기를 못 태운다.
# `PureWindowsPath` 대역을 pool 에 주입해 그 표기만 재현하고, 포인터 surface 가 POSIX 단일
# (`_display_path_text`)로 나오는지 본다 — 표기가 갈리면 세션 정체성 복구 앵커가 OS 를 건너
# 다른 문자열로 읽힌다(축 A·B).

_WINDOWS_TASK_ROOT = "C:/pmhome/.project_manager/.local/tasks"


class _WindowsFlavourPath(PureWindowsPath):
    """Linux 에서 `WindowsPath` 표기(역슬래시 `str()`)를 태우는 경로 대역 — 실 IO 는 없다.

    부트스트랩이 task 경로에 거는 실 IO 는 bind 뒤 존재 확인뿐이라(`pm_state.exists()`) 그것만
    True 로 고정하고, pool 대역이 부르는 `mkdir` 은 no-op 로 흡수한다."""

    def exists(self) -> bool:
        return True

    def mkdir(self, *args, **kwargs) -> None:
        return None


class _WindowsFlavourTaskPool(FakeWorktreePool):
    """`task_dir` 만 Windows 표기로 돌려주는 pool 대역 — 나머지 bind/resume 동작은 공용 mock."""

    def task_dir(self, name):
        return _WindowsFlavourPath(_WINDOWS_TASK_ROOT) / name


def _windows_task_pm_state(name: str) -> _WindowsFlavourPath:
    return _WindowsFlavourPath(_WINDOWS_TASK_ROOT) / name / "pm_state.md"


def test_display_path_text_serializes_windows_path_as_posix(bootstrap):
    """`_display_path_text` = POSIX 단일 직렬화 — 이미 문자열인 경로는 추측 치환하지 않는다."""
    assert bootstrap._display_path_text(
        _windows_task_pm_state("job1")
    ) == f"{_WINDOWS_TASK_ROOT}/job1/pm_state.md"
    assert bootstrap._display_path_text(Path("/pmhome/tasks/job1")) == "/pmhome/tasks/job1"
    # POSIX 파일명엔 `\` 가 정당하게 들어갈 수 있다 — 문자열 입력을 구분자로 해석하지 않는다.
    assert bootstrap._display_path_text("weird\\name") == "weird\\name"


def test_task_pm_state_pointer_is_posix_on_windows_path(bootstrap, tmp_path, capsys):
    """축 A — Windows 표기 경로에서도 pm_state 포인터가 POSIX 단일 표기로 나온다."""
    wp = _WindowsFlavourTaskPool(task_dir_root=tmp_path / "tasks", task_action="resumed")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)

    rc = inst.run(task="job1")

    assert rc == 0
    out = capsys.readouterr().out
    assert bootstrap._render_task_pm_state_pointer(_windows_task_pm_state("job1")) in out
    assert "tasks/job1/pm_state.md" in out
    assert "tasks\\job1" not in out   # 첫-turn 안내 포함 전 surface 가 native 표기로 새지 않는다.


def test_task_first_session_pointer_announced_on_windows_path(bootstrap, tmp_path, capsys):
    """축 B — 신규 task 첫 진입 안내(포인터 줄)가 Windows 표기 경로에서도 뜬다.

    `_pm_state_file` 주입을 우회해 task 서술 pm_state 해소 경로(F7)를 태운다."""
    wp = _WindowsFlavourTaskPool(task_dir_root=tmp_path / "tasks", task_action="created")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst._pm_state_file = None

    rc = inst.run(task="job1")

    assert rc == 0
    out = capsys.readouterr().out
    assert bootstrap._TASK_PM_STATE_POINTER_LABEL in out
    assert bootstrap._render_task_pm_state_pointer(_windows_task_pm_state("job1")) in out
    # 첫-turn 안내(`_pm_state_display_path` 소비 2줄)도 같은 POSIX 표기를 쓴다.
    assert inst._pm_state_display_path() == f"{_WINDOWS_TASK_ROOT}/job1/pm_state.md"
    assert "tasks\\job1" not in out
