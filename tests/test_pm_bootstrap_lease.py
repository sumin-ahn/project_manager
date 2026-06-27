"""bootstrap/handoff 리스 라이프사이클 바인딩 (T-0060 · ADR-0013·0011) 단위 테스트.

엔진 canonical(루트 .project_manager/tools/pm_bootstrap.py·pm_handoff.py)을 importlib 로
직접 검증한다. worktree_pool 은 **DI mock** 으로 주입해 hermetic — 실 리스 장부·git·
work/ 풀을 절대 건드리지 않는다(test_handoff_trigger.py 의 DI 패턴 동류).

검증 축:
  - bootstrap --repo --branch → alloc 호출·identity surface 출력·cwd 슬롯 보고.
  - 무인자(솔로) → 현행 동작 (alloc 경로 미진입·worktree_pool 안 건드림).
  - NeedsCreate (풀 소진) → 사용자 게이트 안내·자동 git worktree add 안 함.
  - handoff payload 에 slot/branch 기록 · --done → release · --trigger → 리스 유지(release X).
  - 회전 재부착(resume) 연속성 · worktree_pool 부재 시 명시 에러(침묵 무력화 금지).
  - sensitivity: 배선 무력화 시 fail.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

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


# ── worktree_pool DI mock (hermetic — 실 장부/git 미접촉) ─────────────────────


class _FakeLease:
    def __init__(self, slot: str, repo: str, branch: str | None):
        self.slot = slot
        self.repo = repo
        self.branch = branch


class _FakeLeaseEntry:
    """list_leases() 가 돌려주는 장부 엔트리 대역 — state/session/slot surface (상태점검용)."""

    def __init__(self, slot: str, repo: str, session: str, *, state: str = "leased"):
        self.slot = slot
        self.repo = repo
        self.session = session
        self.state = state


class _FakeNeedsCreate(Exception):
    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(repo)


class FakeWorktreePool:
    """worktree_pool 인터페이스를 흉내내는 mock — 호출을 기록만 한다(실 부작용 0).

    실 엔진 시그니처: alloc(repo, *, branch, resume) → Lease · slot_path(slot) → Path ·
    release(slot, *, require_clean) → Lease · NeedsCreate(repo) 예외.
    """

    def __init__(self, *, alloc_raises_needs_create: bool = False,
                 alloc_slot: str = "work/A_1", alloc_branch: str | None = "a5",
                 force_detached: bool = False):
        self.NeedsCreate = _FakeNeedsCreate
        self._alloc_raises = alloc_raises_needs_create
        self._alloc_slot = alloc_slot
        self._alloc_branch = alloc_branch
        self._force_detached = force_detached  # True → current_branch 항상 None(detached/조회불가)
        self.alloc_calls: list[dict] = []
        self.bind_calls: list[dict] = []
        self.release_calls: list[dict] = []
        self.current_branch_calls: list[str] = []
        self.release_raises_keyerror = False
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
        # 상태점검 surface — bind 된 슬롯(이 세션) + 미리 심은 다른 활성 리스.
        leased: list = []
        for slot, repo, session in [
            (c["slot"], c["repo"], c["session"]) for c in self.bind_calls
        ]:
            leased.append(_FakeLeaseEntry(slot, repo, session, state="leased"))
        leased.extend(self._extra_leases)
        return leased

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
                    board=None):
    """격리된 PmBootstrap — board/git/log 는 stub, worktree_pool/board 는 mock 주입."""
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    if areas_text is not None:
        areas_file.write_text(areas_text, encoding="utf-8")

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
    # cwd(작업 슬롯) 경로가 보고된다 (slot_path 산출).
    assert "/tmp/multipm/work/A_2" in out


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
    # 현행 부트스트랩 출력은 유지되고, identity surface 는 없다.
    assert "PM 세션 부트스트랩" in out
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
    """lean identity surface — 세션명 `X_2`·슬롯·라이브 브랜치·`--session X_2` 안내."""
    wp = FakeWorktreePool(alloc_branch="x-feat")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2)
    out = capsys.readouterr().out
    assert "당신은 **X PM**" in out
    assert "세션=`X_2`" in out
    assert "슬롯=`work/X_2`" in out
    assert "브랜치=`x-feat`" in out          # 라이브 브랜치(current_branch)
    assert "--session X_2" in out            # 보드 조작 명시 안내
    assert "보드=multi-PM 공유" in out


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
    """--json 출력에도 lean worktree identity(세션명·슬롯·브랜치·others)가 surface 된다."""
    import json as _json
    wp = FakeWorktreePool(alloc_branch="x-feat")
    wp._extra_leases = [_FakeLeaseEntry("work/billing_3", "billing", "billing_3")]
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2, output_json=True)
    data = _json.loads(capsys.readouterr().out)
    wt = data["worktree"]
    assert wt["repo"] == "X"
    assert wt["session"] == "X_2"
    assert wt["slot"] == "work/X_2"
    assert wt["branch"] == "x-feat"
    assert [o["session"] for o in wt["others"]] == ["billing_3"]


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
    """솔로(미지정)면 worktree 줄을 생략한다 (현행 lean 스키마 100% 보존)."""
    sk = handoff.build_handoff_log_skeleton(session_num=9, date="2026-06-16")
    assert "worktree: slot" not in sk
    # lean 3섹션 + 회귀/incident 는 그대로.
    assert "- 읽기 범위:" in sk
    assert "- 회귀/incident:" in sk


def test_trigger_skeleton_records_slot_branch(handoff):
    """트리거 skeleton 도 multi-PM 모드 slot/branch 기록 (회전 재부착 단서)."""
    sk = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="ctx-stop", ctx_pct=6, date="2026-06-16",
        worktree_slot="work/B_1", branch="b2",
    )
    assert "- worktree: slot=`work/B_1`" in sk and "branch=`b2`" in sk


def test_trigger_skeleton_solo_omits_slot_line(handoff):
    sk = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="ctx-stop", ctx_pct=6, date="2026-06-16",
    )
    assert "worktree: slot" not in sk


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
        worktree_slot="work/A_2", branch="a5", done=True,
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
        worktree_slot="work/A_2", branch="a5", done=False,
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
        worktree_slot="work/A_2", branch="a5", done=True,
    )
    assert wp.release_calls == []


def test_handoff_done_release_keyerror_soft(handoff, tmp_path, capsys):
    """이미 release 된 슬롯(KeyError)은 무해하게 스킵(rc 0)."""
    wp = FakeWorktreePool()
    wp.release_raises_keyerror = True
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    rc = inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        worktree_slot="work/A_2", branch="a5", done=True,
    )
    assert rc == 0


# ── 7. trigger → 리스 유지 (release 절대 호출 안 함) ───────────────────────────


def test_trigger_records_slot_but_never_releases(handoff, tmp_path, capsys):
    """ctx-STOP --trigger 는 slot/branch 기록만 — release 절대 호출 안 함(리스 유지)."""
    wp = FakeWorktreePool()
    inst, log_file, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    rc = inst.run_trigger(
        reason="ctx-stop", ctx_pct=8, worktree_slot="work/A_2", branch="a5",
    )
    assert rc == 0
    # 리스 유지 — release 0회 (다음 bootstrap 이 같은 슬롯 resume).
    assert wp.release_calls == []
    log_text = log_file.read_text(encoding="utf-8")
    assert "- worktree: slot=`work/A_2`" in log_text


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
    parser = handoff.build_parser()
    ns = parser.parse_args(
        ["--session-num", "5", "--wave-summary", "x",
         "--worktree-slot", "work/A_2", "--branch", "a5", "--done"]
    )
    assert ns.worktree_slot == "work/A_2" and ns.branch == "a5" and ns.done is True


def test_handoff_done_with_trigger_rejected(handoff):
    """--done + --trigger 동시 사용은 거부 (ctx-STOP 회전은 release 아님·ADR-0013)."""
    with pytest.raises(SystemExit):
        handoff.main(["--trigger", "--done", "--worktree-slot", "work/A_2"])


def test_handoff_branch_without_slot_rejected_interactive(handoff):
    """--branch 만(--worktree-slot 없이) → parser.error 거부 (조용히 무시 X·오용 축소).

    슬롯 없는 브랜치는 회전 재부착 단서로 불완전 — 어느 슬롯에 재부착할지 모른다.
    `--no-pytest`·`--dry-run` 을 함께 줘 가드가 없을 때(sensitivity)도 실 회귀·파일편집
    없이 정상 종료하게 한다 — 그러면 parser.error 미발생이 단언 실패로 즉시 드러난다.
    """
    with pytest.raises(SystemExit):
        handoff.main(["--session-num", "5", "--wave-summary", "x",
                      "--branch", "a5", "--no-pytest", "--dry-run"])


def test_handoff_branch_without_slot_rejected_trigger(handoff):
    """트리거 경로에서도 --branch 만(슬롯 없이) → 거부 (양 경로 공통 가드)."""
    with pytest.raises(SystemExit):
        handoff.main(["--trigger", "--branch", "a5", "--dry-run"])


def test_handoff_branch_with_slot_accepted_by_parser(handoff):
    """--branch + --worktree-slot 동반은 파서 통과 (가드는 슬롯 없는 경우만 거부)."""
    parser = handoff.build_parser()
    ns = parser.parse_args(
        ["--session-num", "5", "--wave-summary", "x",
         "--worktree-slot", "work/A_2", "--branch", "a5"]
    )
    assert ns.branch == "a5" and ns.worktree_slot == "work/A_2"


# ── 10. sensitivity — 배선 무력화 시 fail 재현 ─────────────────────────────────


def test_sensitivity_done_must_release(handoff, tmp_path, capsys):
    """sensitivity: --done 이 release 를 호출하지 않으면(배선 무력화) 이 단언이 깨진다."""
    wp = FakeWorktreePool()
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=True,
        worktree_slot="work/A_2", branch="a5", done=True,
    )
    # 배선이 살아있으면 release 가 정확히 1회. (무력화 시 0회 → fail.)
    assert len(wp.release_calls) == 1


def test_sensitivity_trigger_must_not_release(handoff, tmp_path, capsys):
    """sensitivity: 트리거가 실수로 release 를 호출하면(리스 파괴) 이 단언이 깨진다."""
    wp = FakeWorktreePool()
    inst, _, _ = _make_handoff(handoff, tmp_path, worktree_pool=wp)
    inst.run_trigger(reason="ctx-stop", ctx_pct=8, worktree_slot="work/A_2", branch="a5")
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


def test_auto_slot_single_repo_single_slot_returns_pair(bootstrap, tmp_path):
    """등록 repo 정확히 1개 + 그 repo 슬롯 정확히 1개 → (repo, N)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) == ("project_manager", 1)


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
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) == ("project_manager", 3)


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
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) == ("project_manager", 2)


def test_auto_slot_solo_single_leased_unchanged(bootstrap, tmp_path):
    """solo `{1:leased}` → (repo, 1) — idle 필터가 solo 핵심 케이스 안 깸(재확인)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [_lease("project_manager", 1, "leased")])
    assert bootstrap._auto_slot(areas_file=areas, leases_file=leases) == ("project_manager", 1)


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
    assert cwd.endswith("work/foo_2")


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
    assert cwd.endswith("work/project_manager_1")


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
    monkeypatch.setattr(bootstrap, "_auto_slot", lambda: ("project_manager", 1))
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
