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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from _slot_seed import fake_slot_path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 홈 사본 엔진으로 task 를 바인딩하는 실 서브프로세스 — 명령이 끝나도 열림 기록은 남는다.
_BIND_TASK_SOURCE = '''\
import importlib.util, json, sys

spec = importlib.util.spec_from_file_location("worktree_pool_session_bind", sys.argv[1])
worktree_pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worktree_pool)
record, action = worktree_pool.bind_task(sys.argv[2], session="s-test")
print(json.dumps({"open": record.open, "action": action}))
'''


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
    """worktree_pool.TaskActiveElsewhere 대역 — 열림 기록 거부(정체 표시용 시작 시각)."""

    def __init__(self, name: str, started: str = ""):
        self.name = name
        self.started = started
        super().__init__(name)


class _FakeInvalidTaskName(Exception):
    """worktree_pool.InvalidTaskName 대역 (T-0353·must-fix task 명 검증)."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(reason)


class _FakeTaskRecord:
    """bind_task 반환 Task 대역 — surface 가 읽는 name·prefix·open·started."""

    def __init__(self, name: str, prefix: "str | None", open: bool = True,
                 started: str = "2026-07-18T00:00:00+00:00"):
        self.name = name
        self.prefix = prefix
        self.open = open
        self.session = "s-other" if open else ""
        self.started = started


class FakeWorktreePool:
    """worktree_pool 인터페이스를 흉내내는 mock — 호출을 기록만 한다(실 부작용 0).

    실 엔진 시그니처: alloc(repo, *, branch, resume) → Lease · slot_path(slot) → Path ·
    release(slot) → Lease · NeedsCreate(repo) 예외.
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
                 task_open_started: "str | None" = None,
                 task_invalid_reason: "str | None" = None):
        self.NeedsCreate = _FakeNeedsCreate
        # task 축(T-0353·F1) — bind_task/task_dir/TaskActiveElsewhere/InvalidTaskName 대역.
        self.TaskActiveElsewhere = _FakeTaskActiveElsewhere
        self.InvalidTaskName = _FakeInvalidTaskName
        self._task_dir_root = task_dir_root       # None=미설정(테스트가 세팅)
        self._task_prefix = task_prefix           # bind_task 반환 prefix(기본 None=없음)
        self._task_action = task_action           # created|resumed
        # 설정 시 bind_task 가 열림 기록으로 거부하고 그 시작 시각을 싣는다.
        self._task_open_started = task_open_started
        self._task_invalid_reason = task_invalid_reason  # 설정 시 InvalidTaskName(명 검증 거부)
        self.bind_task_calls: list[dict] = []
        self.closed_tasks: list[str] = []        # close_task_session(종료 기록) 호출 기록
        self._alloc_raises = alloc_raises_needs_create
        self._alloc_slot = alloc_slot
        self._alloc_branch = alloc_branch
        self._force_detached = force_detached  # True → current_branch 항상 None(detached/조회불가)
        self.alloc_calls: list[dict] = []
        self.release_calls: list[dict] = []
        self.current_branch_calls: list[str] = []
        self.release_raises_keyerror = False
        # 0단계(T-0351) 실재 검사가 통과하도록 사전 시드하는 **idle** 슬롯 — 그 idle 리스를
        # list_leases 에 실어 phantom-거부를 피한다(§F1b "장부·폴더에 실재"). idle 이라 점유(leased)·"다른 활성 PM" surface 엔 안 잡힌다(회귀 0).
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

    def lease_is_open(self, lease):
        """열림 판정은 엔진 규칙 그대로 — 열림 기록(leased + session)이면 열림."""
        return getattr(lease, "state", None) == "leased" and bool(getattr(lease, "session", ""))

    def list_leases(self):
        # 상태점검 surface — 미리 심은 다른 활성 리스 + 0단계 실재 검사용 idle 시드(§F1b).
        leased: list = []
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

    def slots_for_task_strict(self, name):
        """실 엔진과 같은 짝 — 결과는 같고 장부 손상만 올린다(이 대역은 손상 없음)."""
        return self.slots_for_task(name)

    def compare_slot_git(self, slot, *, git_runner=None):
        # 0단계 record-vs-live(T-0350 compare 프리미티브) 대역 — 설정된 결과(없으면 None=검사 no-op).
        return self._compare_result

    def slot_git_status(self, slot, *, git_runner=None):
        # `_slot_era_info` 가 슬롯 바인딩마다 부르는 브랜치 부모 기록 조회 대역 — 이 스위트는
        # 시대차 자체를 보지 않으므로 parent 없음(None)으로 고정한다(신 계약 모양만 맞춤).
        return {"slot": slot, "branch": self._live_branch.get(slot), "head": None,
                "parent": None, "children": [], "stashes": 0, "submodules": [], "dirty": False}

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
        return fake_slot_path(slot)

    def task_dir(self, name):
        root = self._task_dir_root or (Path("/tmp/multipm") / "tasks")
        return root / name

    def task_pm_state_file(self, name):
        """실 엔진과 같은 조합 — 인계 표시가 이 경로를 그대로 보인다."""
        return self.task_dir(name) / "pm_state.md"

    def find_task(self, name):
        # 강제 인계 경로가 읽는 자리 — 열림 기록이 설정돼 있으면 그 행을 돌려준다.
        if self._task_open_started is None:
            return None
        return _FakeTaskRecord(name, self._task_prefix, open=True,
                               started=self._task_open_started)

    def read_task_todos(self, name):
        """열린 할 일 조회 — 부트스트랩 dump 가 읽는 자리(이 대역은 항상 0건)."""
        return []

    def close_task_session(self, name):
        """종료 기록 프리미티브 대역 — 닫은 뒤에는 열림 기록이 없다."""
        self.closed_tasks.append(name)
        self._task_open_started = None
        self._task_action = "resumed"
        return _FakeTaskRecord(name, self._task_prefix)

    NoTaskSessionClaim = type("NoTaskSessionClaim", (Exception,), {})

    def take_task_session_claim(self, name):
        return "s-test"

    def bind_task(self, name, *, session=None, registered_repos=None):
        # F1 task 바인딩 대역 — 명 검증 거부(InvalidTaskName)/열림 기록 거부, 아니면
        # (record, action) 2-튜플 반환 + task state 즉시 생성.
        self.bind_task_calls.append({"name": name, "registered_repos": registered_repos})
        if self._task_invalid_reason is not None:
            raise self.InvalidTaskName(name, self._task_invalid_reason)
        if self._task_open_started is not None:
            raise self.TaskActiveElsewhere(name, self._task_open_started)
        self.task_dir(name).mkdir(parents=True, exist_ok=True)
        state = self.task_dir(name) / "pm_state.md"
        if not state.exists():
            state.write_text(
                "## 세션 식별 (현재까지 사용된 이름)\n"
                "  - (아직 완료된 task 세션 없음)\n",
                encoding="utf-8",
            )
        return (_FakeTaskRecord(name, self._task_prefix), self._task_action)

    def release(self, slot):
        # 엔진 시그니처 그대로 — 자동경로 인자(옛 require_clean)를 넘기면 TypeError 로 드러난다.
        self.release_calls.append({"slot": slot})
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


# ── readonly 공유 슬롯 0단계 carve-out (⑬·T-0358·seam #1 파급) ─────────────────
# `Lease.role` 을 canonical 필드로 승격(T-0358)하면 extra 에서 빠지므로, T-0351 의 extra["role"]
# 훅은 조용히 무력화된다 → `_phase0_is_readonly` 를 canonical `lease.role` read 로 갱신했다.
# 아래가 그 파급(readonly lease → 0단계 carve-out 실동작)을 못박는다.


# ── 2c. 보호 브랜치 경고 (소프트·T-0076) — 라이브 브랜치가 보호목록이면 🚫 surface ──


# ── 3. NeedsCreate (풀 소진) → 사용자 게이트·자동 생성 안 함 ───────────────────


# ── 4. worktree_pool 부재 시 명시 에러 (침묵 무력화 금지) ─────────────────────


# ── 5. handoff payload slot/branch 기록 ───────────────────────────────────────




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














# ── 8. 회전 재부착(resume) 연속성 ─────────────────────────────────────────────


# ── 9. CLI parser 배선 ────────────────────────────────────────────────────────


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








# ── 10. sensitivity — 배선 무력화 시 fail 재현 ─────────────────────────────────






# ── 11. _auto_slot — 단일 self-host 슬롯 판정 (Part B) ────────────────────────
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


def _lease(repo: str, n: int, state: str = "leased") -> dict:
    """worktree-leases.json 엔트리 1개 (`work/<repo>_<N>`·기본 leased·idle 회귀용 state 인자)."""
    return {"slot": f"work/{repo}_{n}", "repo": repo, "session": f"{repo}_{n}", "state": state}


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


# ── 14. run() 진입부 _bound_slot 스레딩 (순서 함정 — 수집 전 세팅) ─────────────
# 명시 --repo --slot multi-PM 경로에서 run() 이 _collect_git/_collect_pytest *전*에
# self._bound_slot 을 세팅하는지 검증한다. 주입 git_fn 으로 호출 시점의 _bound_slot 을
# 캡처해, git 수집이 worktree cwd 를 쓸 수 있는 상태였는지 확인한다.


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


def test_bootstrap_task_open_record_asks_before_dump(bootstrap, tmp_path, capsys):
    """열린 기록이면 rc1 로 묻고 **부분 dump 도 금지**(⑧ 동형) — 정체·복원·택1 을 싣는다."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks",
                          task_open_started="2026-09-09T01:02:03+00:00")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(task="job1")
    assert rc == 1
    captured = capsys.readouterr()
    # stderr 에 그 기록의 정체(언제 열렸나)와 응답 자리 — stdout(dump)은 비었다(부분 dump 금지).
    assert "열린 세션 기록" in captured.err and "2026-09-09T01:02:03+00:00" in captured.err
    assert "이전 세션이 하던 것" in captured.err
    assert "task close job1" in captured.err and "--force" in captured.err
    assert "task identity surface" not in captured.out
    assert "board" not in captured.out.lower() or captured.out.strip() == ""


def test_bootstrap_task_force_closes_the_record_and_hands_over(bootstrap, tmp_path, capsys):
    """`--force` 가 사용자 응답이다 — 기록을 닫고(기존 프리미티브) 인계 표시 뒤 진입한다."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks",
                          task_open_started="2026-09-09T01:02:03+00:00")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)

    rc = inst.run(task="job1", force=True)

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert wp.closed_tasks == ["job1"], "강제 인계가 기존 종료 기록 프리미티브를 부르지 않았다"
    assert "강제 인계" in captured.err and "이전 세션이 하던 것" in captured.err
    assert "task identity surface" in captured.out


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


def test_bootstrap_task_entry_has_no_reclaim_notice(bootstrap, tmp_path, capsys):
    """자동 회수 개념이 사라졌다 — 진입 표면에 회수 문구가 없다(대체 장치 0)."""
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks", task_action="created")
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


# ── F1b 동시 세션 거부의 실효 (기록 pid = 하네스 세션·T-0915) ────────────────────
# 위 F1 은 `FakeWorktreePool` DI 로 부트스트랩 배선을 본다. 거부가 **실제로 발화하는지**는
# 살아 있는 세션이 있어야 알 수 있어서, 여기서는 더미 하네스 세션을 띄워 그 아래에서 실
# 엔진으로 task 를 바인딩하고 같은 장부에 다른 세션으로 들어가 본다.


@pytest.fixture
def pool_home(tmp_path):
    """실 엔진 사본을 담은 tmp PM 홈 — 그 사본의 `worktree_pool` 이 자기 장부를 소유한다."""
    home = tmp_path / "pool-home"
    (home / ".project_manager" / ".local").mkdir(parents=True)
    (home / ".project_manager" / "wiki").mkdir(parents=True)
    shutil.copytree(TOOLS, home / ".project_manager" / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    shutil.copyfile(REPO / ".project_manager" / "wiki" / "pm_state.template.md",
                    home / ".project_manager" / "wiki" / "pm_state.template.md")
    return home


def _load_pool(home: Path):
    """홈 사본의 `worktree_pool` 을 로드한다 — 경로 전역이 그 홈으로 굳는다(재배선 0)."""
    target = home / ".project_manager" / "tools" / "worktree_pool.py"
    spec = importlib.util.spec_from_file_location("worktree_pool_bootstrap_lease", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bind_task_refuses_a_task_with_an_open_record(pool_home, tmp_path):
    """실 서브프로세스가 연 task 는 그 명령이 끝난 뒤에도 `TaskActiveElsewhere` 로 거부된다.

    프로세스 번호를 보던 시절엔 bind 직후 그 번호가 죽어 거부가 사실상 발화하지 못했다.
    이제 판정 재료가 기록이라, 종료 기록이 남기 전에는 두 번째 진입이 막힌다 — 닫는 것은
    사용자 행위(`task close`)다.
    """
    binder = tmp_path / "bind_task.py"
    binder.write_text(_BIND_TASK_SOURCE, encoding="utf-8")
    tools = pool_home / ".project_manager" / "tools" / "worktree_pool.py"
    pool = _load_pool(pool_home)

    done = subprocess.run([sys.executable, str(binder), str(tools), "job1"],
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"open": True, "action": "created"}
    assert pool.find_task("job1").open is True

    with pytest.raises(pool.TaskActiveElsewhere) as refused:
        pool.bind_task("job1", session="s-other")
    assert refused.value.started == pool.find_task("job1").started

    pool.close_task_session("job1")            # 사용자가 닫는다(`pm-config task close`)
    record, action = pool.bind_task("job1", session="s-test")

    assert action == "resumed", "닫힌 기록은 그대로 재개다"
    assert record.open is True


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
    """`_bound_session_name` = `task:<이름>` — log/pm_state 귀속 태그."""
    inst = _make_bootstrap(bootstrap, tmp_path,
                           worktree_pool=FakeWorktreePool(task_dir_root=tmp_path / "tasks"))
    inst._task_name = "payments-refactor"
    assert inst._bound_session_name() == "task:payments-refactor"


def test_task_resume_dumps_pm_state_body(bootstrap, tmp_path, capsys):
    """resume — pm_state 에서 차수를 추론하고 열린 todo 를 dump(포인터가 아닌 소비)."""
    _write_task_pm_state(
        tmp_path, "job1",
        "# pm_state\n\n"
        "## 세션 식별 (현재까지 사용된 이름)\n"
        "  - **3차** job1 세션\n"
        "  - **2차** job1 세션\n",
    )
    inst, _ = _task_read_bootstrap(bootstrap, tmp_path, action="resumed")
    rc = inst.run(task="job1")
    assert rc == 0
    out = capsys.readouterr().out
    # 차수 추론(3차 → 4차)·열린 todo 절이 실제로 dump(포인터만이 아니라 소비).
    assert "## PM 4차 부트스트랩" in out
    assert "### 열린 todo" in out


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


# ── F1c 인계 표시 (T-0920) — 이전 세션이 하던 것을 **읽기만** 해서 보여 준다 ─────────
# 재료 여섯은 전부 이미 조회 가능한 것들이다(새 기록 파일 0): claim 티켓 · 미회수 위임 준비와
# 그 라운드 단계 · 서술 pm_state · 마지막 기록 항목 · 보유 작업 폴더와 갈래.


def _handover_home(bootstrap, tmp_path, monkeypatch):
    """인계 표시의 여섯 재료를 실값으로 갖춘 PM 홈 + 그 홈을 앵커로 하는 부트스트랩.

    위임 준비 사본은 **그 세션이 잡고 있던 슬롯 안**에 둔다(실제 위임의 cwd 가 슬롯이다).
    PM 홈(`REPO`) 아래에는 준비 사본을 하나도 두지 않는다 — 뿌리를 홈으로 잡으면 이 자리가
    빈다.
    """
    monkeypatch.setattr(bootstrap, "REPO", tmp_path)
    wp = FakeWorktreePool(task_dir_root=tmp_path / "tasks")
    wp._extra_leases = [_FakeLeaseEntry("work/X_7", "X", "job1", state="leased")]
    run_dir = (wp.slot_path("work/X_7") / ".project_manager" / ".local"
               / "delegate-ticket-copies" / "C-wave" / "abc123" / "T-0777")
    if (run_dir / "rounds").is_dir():
        for stale in (run_dir / "rounds").iterdir():
            stale.unlink()
    (run_dir / "rounds").mkdir(parents=True, exist_ok=True)
    (run_dir / "rounds" / "01-architect.md").write_text("설계\n", encoding="utf-8")
    (run_dir / "rounds" / "02-developer.md").write_text("구현\n", encoding="utf-8")
    assert not (tmp_path / ".project_manager" / ".local"
                / "delegate-ticket-copies").exists(), "PM 홈 아래에는 준비 사본이 없다"

    wp.live_branch_override["work/X_7"] = "task/x-7"
    wp.task_pm_state_file = lambda name: tmp_path / "tasks" / name / "pm_state.md"

    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst._log_file.write_text(
        "# log\n\n## [2026-09-09] handoff | PM 12차 (task:job1) → 다음 PM 세션\n\n본문\n",
        encoding="utf-8",
    )
    inst._run_board_fn = lambda argv: (
        (0, "  [claimed] T-0777  진행 중이던 일  me  tag\n")
        if argv[:3] == ["list", "--status", "claimed"] else (0, "")
    )
    return inst, run_dir


def test_handover_shows_the_six_materials_with_real_values(bootstrap, tmp_path, monkeypatch):
    """강제 인계 표시 여섯 항목이 실값으로 나온다 — 지어낸 값이 아니라 조회 결과다."""
    inst, run_dir = _handover_home(bootstrap, tmp_path, monkeypatch)

    text = inst._render_handover(task="job1")

    assert "T-0777" in text                                   # claim 중인 티켓
    assert str(run_dir) in text or run_dir.name in text        # 미회수 위임 준비(run-dir)
    assert "02-developer.md" in text                           # 라운드 단계(마지막 순번)
    assert "tasks/job1/pm_state.md" in text.replace("\\", "/")  # 서술 pm_state
    assert "PM 12차 (task:job1) → 다음 PM 세션" in text          # 마지막 기록 항목
    assert "work/X_7 (갈래: task/x-7)" in text                  # 보유 작업 폴더와 갈래


def test_handover_delegate_runs_read_the_held_slot_not_the_pm_home(
    bootstrap, tmp_path, monkeypatch,
):
    """미회수 위임 준비는 **보유 슬롯** 아래를 읽는다 — PM 홈 아래 사본은 그 세션 것이 아니다."""
    inst, run_dir = _handover_home(bootstrap, tmp_path, monkeypatch)
    decoy = (tmp_path / ".project_manager" / ".local" / "delegate-ticket-copies"
             / "C-other" / "zzz999" / "T-9999" / "rounds")
    decoy.mkdir(parents=True)
    (decoy / "01-architect.md").write_text("남의 것\n", encoding="utf-8")

    text = inst._render_handover(task="job1")

    assert "T-0777" in text and "02-developer.md" in text
    assert str(run_dir.parent.parent) in text or "abc123" in text
    assert "T-9999" not in text, "PM 홈 아래 사본이 그 세션 것으로 실렸다"


def test_handover_claimed_lens_is_the_session_that_held_the_place(
    bootstrap, tmp_path, monkeypatch,
):
    """claim 조회 렌즈는 그 자리를 잡고 있던 task 다 — 전역 `--mine` 이 아니다."""
    inst, _run_dir = _handover_home(bootstrap, tmp_path, monkeypatch)
    seen: list[list[str]] = []

    def record_board(argv):
        seen.append(list(argv))
        return 0, "  [claimed] T-0777  진행 중이던 일  me  tag\n"

    inst._run_board_fn = record_board

    assert inst._handover_claimed_tickets("job1") == ["T-0777"]
    assert seen[-1] == ["list", "--status", "claimed", "--task", "job1"]


def test_handover_only_reads(bootstrap, tmp_path, monkeypatch):
    """복원은 읽기만 한다 — log·장부·준비 사본 어느 것도 1바이트 바뀌지 않는다."""
    inst, run_dir = _handover_home(bootstrap, tmp_path, monkeypatch)
    watched = [inst._log_file, run_dir / "rounds" / "02-developer.md"]
    before = {path: path.read_bytes() for path in watched}
    rounds_before = sorted(path.name for path in (run_dir / "rounds").iterdir())

    inst._render_handover(task="job1")

    for path, blob in before.items():
        assert path.read_bytes() == blob, f"복원이 {path.name} 을 썼다"
    assert sorted(path.name for path in (run_dir / "rounds").iterdir()) == rounds_before, (
        "복원이 라운드 자리에 파일을 만들었다")
