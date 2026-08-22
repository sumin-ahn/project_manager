"""슬롯 정체성 축 — **장부 값**으로 해소되는가 (경로 basename·정규식 파싱 폐기).

슬롯 식별의 단일 진실은 lease 장부 행의 값이다: **정체성 = 행의 `session`, 경로 = 행의 `slot`**.
어느 쪽도 다른 쪽에서 재구성하지 않는다(번호로 `work/<repo>_<N>` 을 짓지 않고, 경로 basename 으로
정체성을 파싱하지 않는다). 이 파일이 고정하는 계약:

  1. **동작 무변경 스냅샷** — 현행 형상(모든 행이 `work/<repo>_<N>`)에서 소비자 해소값이 종전과
     같다. adopter#0 형상(행 7 · task 명 session · readonly · idle)과 multi-PM N=2 를 **실 장부
     파일 + 실 디렉토리**로 세우고 소비자 산출을 값으로 단언한다.
  2. **결정적 케이스** — 장부가 등록한 슬롯 이름과 디렉토리 basename 이 **다른** 행. 경로 파싱이
     남아 있으면 이 케이스가 red 다(파싱과 장부 값이 우연히 같은 픽스처로는 회귀를 못 잡는다).
  3. **경로 마디가 없는 슬롯**(`slot="."` — PM 홈 자신을 가리키는 행)도 전 소비자가 해소한다.
     이 형상은 후속(홈 N=1 행 등록)의 선행 조건이며, 여기선 **읽기 해소만** 검증한다.
  4. **판정 불능은 fail-loud** — 장부가 없거나 손상됐고 경로도 슬롯 키가 아니면 정체성을 지어내지
     않는다(None/에러). 반대로 정상 형상을 과하게 조이지도 않는다(대조군).
  5. **3각 파리티** — `board.session_name` · `worktree_pool._default_session` ·
     `pm_config._default_session` 이 같은 장부에서 같은 층 순서로 해소한다(tail 만 상이).

**hermetic 필수**: 엔진 모듈의 경로 전역은 import 시점에 실 repo 절대경로로 굳는다 — tmp PM 홈으로
전부 재지정해 실 `.project_manager` 를 건드리지 않는다(test_worktree_pool·test_board_identity 동형).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 픽스처 장부 표기 — 이름을 손으로 반복하지 않도록 한 곳에서 만든다.
_ADOPTER_REPO = "project_manager"
_TASK_SESSION = "main"          # 슬롯을 대여한 task 이름(슬롯 키 형식이 아니다).


def _load(name: str, alias: str | None = None):
    spec = importlib.util.spec_from_file_location(alias or name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rebind(mod, root: Path) -> None:
    """엔진 모듈의 경로 전역을 tmp PM 홈으로 재지정한다(있는 이름만)."""
    pm = root / ".project_manager"
    mapping = {
        "REPO": root,
        "LOCAL_DIR": pm / ".local",
        "LEASES_FILE": pm / ".local" / "worktree-leases.json",
        "LEASES_LOCK": pm / ".local" / "worktree-leases.lock",
        "TASKS_DIR": pm / ".local" / "tasks",
        "WORK_DIR": root / "work",
        "REPOS_DIR": root / ".repos",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_CONF": pm / "local.conf",
        "PM_STATE_FILE": pm / "wiki" / "pm_state.md",
        "LOG_FILE": pm / "wiki" / "log" / "current.md",
        "CURRENT_FILE": pm / "wiki" / "log" / "current.md",
        "LOG_DIR": pm / "wiki" / "log",
        "WIKI_DIR": pm / "wiki",
        "TICKETS_DIR": pm / "wiki" / "tickets",
        "BOARD_LOCK": pm / ".local" / "board.lock",
        "REPO_HOOKS_DIR": pm / ".local" / "repo-hooks",
    }
    for name, value in mapping.items():
        if hasattr(mod, name):
            setattr(mod, name, value)


class Home:
    """tmp PM 홈 — 실 장부 파일·실 슬롯 디렉토리 + 재지정된 엔진 모듈 묶음."""

    def __init__(self, root: Path):
        self.root = root
        self.ledger = root / ".project_manager" / ".local" / "worktree-leases.json"
        self.areas = root / ".project_manager" / "areas.md"
        self.ia = _load("identity_args")
        self.bp = _load("pm_bootstrap")
        self.hf = _load("pm_handoff")
        self.wp = _load("worktree_pool")
        self.lg = _load("pm_log")
        for mod in (self.bp, self.hf, self.wp, self.lg):
            _rebind(mod, root)

    def write_ledger(self, leases: list[dict], tasks: list[dict] | None = None) -> None:
        payload: dict = {"leases": leases}
        if tasks is not None:
            payload["tasks"] = tasks
        self.ledger.write_text(json.dumps(payload), encoding="utf-8")

    def write_areas(self, repos: list[str]) -> None:
        rows = ["| repo | prefix | git | test_cmd | owner | base | protected |",
                "|---|---|---|---|---|---|---|"]
        rows += [f"| {r} | {r} |  |  | alice |  |  |" for r in repos]
        text = "\n".join(rows) + "\n"
        self.areas.write_text(text, encoding="utf-8")
        (self.root / ".project_manager" / "board" / "areas.md").write_text(
            text, encoding="utf-8")

    def mkslot(self, *slots: str) -> None:
        for slot in slots:
            (self.root / slot).mkdir(parents=True, exist_ok=True)

    def mktask_dir(self, *tasks: str) -> None:
        for task in tasks:
            (self.root / ".project_manager" / ".local" / "tasks" / task).mkdir(
                parents=True, exist_ok=True)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Home:
    root = tmp_path / "pm-home"
    pm = root / ".project_manager"
    (pm / ".local").mkdir(parents=True)
    (pm / "wiki" / "log").mkdir(parents=True)
    (pm / "board").mkdir(parents=True)
    (root / "work").mkdir()
    (root / ".repos").mkdir()
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    return Home(root)


def _pool_row(repo: str, number: int, session: str, **extra) -> dict:
    row = {"slot": f"work/{repo}_{number}", "repo": repo, "session": session,
           "pid": 100 + number, "started": "t", "state": "leased", "test_cmd": None}
    row.update(extra)
    return row


def _adopter0_shape(home: Home) -> None:
    """adopter#0 실 형상 복제 — 행 7 · 행1~6 leased(session=task 명 `main`) · 행3 readonly ·
    행7 idle. 슬롯 디렉토리와 task 서술 dir 도 실제로 만든다."""
    rows = [
        _pool_row(_ADOPTER_REPO, 1, _TASK_SESSION, test_cmd="pytest tests/ -q"),
        _pool_row(_ADOPTER_REPO, 2, _TASK_SESSION),
        _pool_row(_ADOPTER_REPO, 3, "", pid=0, role="readonly"),
        _pool_row(_ADOPTER_REPO, 4, _TASK_SESSION),
        _pool_row(_ADOPTER_REPO, 5, _TASK_SESSION),
        _pool_row(_ADOPTER_REPO, 6, _TASK_SESSION),
        _pool_row(_ADOPTER_REPO, 7, "", pid=0, state="idle"),
    ]
    home.write_ledger(rows, tasks=[{"name": _TASK_SESSION, "prefix": None,
                                    "pid": 0, "started": "t"}])
    home.write_areas([_ADOPTER_REPO])
    home.mkslot(*[f"work/{_ADOPTER_REPO}_{n}" for n in range(1, 8)])
    home.mktask_dir(_TASK_SESSION)


# ════════════════════════════════════════════════════════════════════════
# 1. 동작 무변경 스냅샷 — adopter#0 형상(행 7·task session·readonly·idle)
# ════════════════════════════════════════════════════════════════════════


def test_adopter0_shape_consumer_values_are_unchanged(home):
    """행 7 형상에서 슬롯 정체성 소비자 전원이 종전과 같은 값을 낸다(동작 무변경).

    이 형상의 행 session 은 **task 이름**(`main`)이거나 비어 있다 — 슬롯 축 정체성이 아니므로
    pool 명명 규약(경로 마디)이 키를 준다. 즉 값-경계 교체 뒤에도 슬롯 키는 종전 그대로다.
    """
    _adopter0_shape(home)
    ia, hf, wp = home.ia, home.hf, home.wp

    # 번호 축 — 조회(leased 만·idle 7 제외)와 이름 점유(상태 무관·idle 포함)가 각자 종전 동형.
    assert ia.repo_slot_numbers(_ADOPTER_REPO, home.ledger) == [1, 2, 3, 4, 5, 6]
    assert sorted(wp._existing_slot_numbers(_ADOPTER_REPO, wp.list_leases())) == \
        [1, 2, 3, 4, 5, 6, 7]

    # actor 축 — 활성 슬롯 ≥2 는 여전히 모호(fail-loud).
    with pytest.raises(ia.SlotResolutionError):
        ia.resolve_actor_slot(_ADOPTER_REPO, home.ledger)

    # 슬롯 키 축 — task 명 session 은 슬롯 정체성이 아니라 경로 규약이 키를 준다.
    for number in (1, 3, 7):
        slot = f"work/{_ADOPTER_REPO}_{number}"
        key = f"{_ADOPTER_REPO}_{number}"
        assert hf._parse_worktree_slot(slot) == (_ADOPTER_REPO, number)
        assert hf._resolve_state_slot(slot, home.areas, home.ledger) == key
        assert hf._handoff_user_ack_target(None, slot) == (key, f"slot {key!r}")
        assert wp._normalize_slot(slot) == slot
        assert wp.slot_pm_state_file(slot) == (
            home.root / ".project_manager" / ".local" / "slots" / key / "pm_state.md")

    # 자동해소 축 — 슬롯 ≥2 라 미해소(종전 동형)이고 cwd 는 PM 홈.
    assert home.bp._auto_slot(home.areas, home.ledger) is None
    assert hf._regression_cwd(None, home.areas, home.ledger) == str(home.root)


def test_adopter0_shape_task_session_row_stays_task_axis(home):
    """task 명으로 대여된 행은 slot 축으로 승격되지 않는다 — 축 판정은 장부 tasks 등록이다."""
    _adopter0_shape(home)
    cwd = home.root / f"work/{_ADOPTER_REPO}_2"
    identity, source = home.lg.resolve_snapshot_identity(home.root, cwd)
    assert (identity, source) == (_TASK_SESSION, home.lg.IDENTITY_SOURCE_CWD_LEASE)
    assert home.lg._checkpoint_identity_axes(home.root, cwd, identity, source) == \
        (_TASK_SESSION, None)


def test_multipm_two_slot_shape_consumer_values_are_unchanged(home):
    """multi-PM N=2(슬롯 세션 canonical) — 소비자 값이 종전과 같다."""
    home.write_ledger([_pool_row("A", 1, "A_1", test_cmd="pytest -q"),
                       _pool_row("A", 2, "A_2")])
    home.write_areas(["A"])
    home.mkslot("work/A_1", "work/A_2")
    ia, hf, wp = home.ia, home.hf, home.wp

    assert ia.repo_slot_numbers("A", home.ledger) == [1, 2]
    with pytest.raises(ia.SlotResolutionError):
        ia.resolve_actor_slot("A", home.ledger)
    assert hf._resolve_explicit_identity_slot("A", 2, home.ledger) == ("work/A_2", None)
    assert hf._resolve_state_slot("work/A_2", home.areas, home.ledger) == "A_2"
    assert wp.slot_pm_state_file("A_2") == (
        home.root / ".project_manager" / ".local" / "slots" / "A_2" / "pm_state.md")
    # slot 축 — 등록 task 가 아니고 슬롯 키 형식이라 slot 축(종전 동형).
    cwd = home.root / "work/A_1"
    identity, source = home.lg.resolve_snapshot_identity(home.root, cwd)
    assert (identity, source) == ("A_1", home.lg.IDENTITY_SOURCE_CWD_LEASE)
    assert home.lg._checkpoint_identity_axes(home.root, cwd, identity, source) == (None, "A_1")


def test_single_slot_shape_resolves_to_ledger_row(home):
    """단일 self-host — 자동해소가 행의 정체성 키와 **행의 경로 값**을 함께 낸다."""
    home.write_ledger([_pool_row("A", 1, "A_1", test_cmd="pytest -q")])
    home.write_areas(["A"])
    home.mkslot("work/A_1")

    resolved = home.bp._auto_slot(home.areas, home.ledger)
    assert (resolved.key, resolved.slot) == ("A_1", "work/A_1")
    assert home.hf._regression_cwd(None, home.areas, home.ledger) == \
        str(home.root / "work/A_1")
    assert home.bp._pm_state_display_path(None, home.areas, home.ledger) == \
        ".project_manager/.local/slots/A_1/pm_state.md"


# ════════════════════════════════════════════════════════════════════════
# 2. 결정적 케이스 — 장부 등록 이름 ≠ 디렉토리 basename
# ════════════════════════════════════════════════════════════════════════


def _mismatch_shape(home: Home) -> None:
    """장부가 `work/A_1` 슬롯을 세션 `B_7` 로 등록한 형상 — 경로 이름과 정체성이 갈린다."""
    home.write_ledger([{"slot": "work/A_1", "repo": "A", "session": "B_7", "pid": 9,
                        "started": "t", "state": "leased", "test_cmd": "pytest -q"}])
    home.write_areas(["A"])
    home.mkslot("work/A_1")


def test_identity_comes_from_ledger_not_directory_name(home):
    """정체성은 장부 행의 session 이다 — 디렉토리 basename(`A_1`)이 아니다.

    경로 파싱이 남아 있으면 이 단언들이 전부 `A_1` 로 red 가 난다(파싱과 장부가 우연히 같은
    픽스처로는 이 회귀를 못 잡는다).
    """
    _mismatch_shape(home)
    ia, hf, wp = home.ia, home.hf, home.wp

    identity = ia.resolve_slot_identity("work/A_1", home.ledger)
    assert (identity.key, identity.slot) == ("B_7", "work/A_1")
    assert identity.source == ia.IDENTITY_FROM_LEDGER_SESSION
    assert hf._resolve_state_slot("work/A_1", home.areas, home.ledger) == "B_7"
    assert hf._handoff_user_ack_target(None, "work/A_1") == ("B_7", "slot 'B_7'")
    # 재접속 **좌표**는 키 분해가 아니라 행 값이다 — 행 repo `A` 의 1번 슬롯(§6 왕복 참조).
    assert hf._parse_worktree_slot("work/A_1") == ("A", 1)
    assert (identity.repo, identity.number) == ("A", 1)
    assert wp.slot_pm_state_file("work/A_1") == (
        home.root / ".project_manager" / ".local" / "slots" / "B_7" / "pm_state.md")


def test_actor_resolution_returns_ledger_session_verbatim(home):
    """`--repo` 단독 actor 해소는 행의 session 값 그대로 — 번호로 세션을 재조립하지 않는다."""
    _mismatch_shape(home)
    assert home.ia.resolve_actor_slot("A", home.ledger) == "B_7"
    # 경로 축은 행의 slot 값 — 세션에서 `work/B_7` 을 만들지 않는다.
    assert home.hf._resolve_explicit_identity_slot("A", None, home.ledger) == \
        ("work/A_1", None)
    assert home.wp._resolve_actor_slot_for_repo("A") == "work/A_1"


def test_actor_resolution_returns_task_name_session(home):
    """슬롯을 task 명의로 대여한 홈에서 actor 해소는 그 task 이름을 낸다(장부 값 그대로).

    종전 구현은 번호에서 `<repo>_<N>` 을 재조립해 장부에 없는 세션을 냈다 — bare 해소
    (`session_name` 단일-lease 층)와 갈리는 값이었다.
    """
    home.write_ledger([_pool_row(_ADOPTER_REPO, 1, _TASK_SESSION)],
                      tasks=[{"name": _TASK_SESSION, "prefix": None, "pid": 0, "started": "t"}])
    home.write_areas([_ADOPTER_REPO])
    home.mkslot(f"work/{_ADOPTER_REPO}_1")
    assert home.ia.resolve_actor_slot(_ADOPTER_REPO, home.ledger) == _TASK_SESSION


# ════════════════════════════════════════════════════════════════════════
# 3. 경로 마디가 없는 슬롯 — 장부 행이 정체성을 준다
# ════════════════════════════════════════════════════════════════════════


def _home_row_shape(home: Home, repo: str = "adopter") -> str:
    """PM 홈 자신을 가리키는 행(`slot="."`)을 실 장부 파일로 세운다 — 읽기 해소만 검증한다."""
    home.write_ledger([{"slot": ".", "repo": repo, "session": f"{repo}_1", "pid": 0,
                        "started": "t", "state": "leased", "test_cmd": None,
                        "bound": True}])
    home.write_areas([repo])
    return f"{repo}_1"


def test_home_row_slot_resolves_identity_from_ledger(home):
    """경로에 이름이 없는 슬롯(`.`)도 장부 session 으로 정체성이 해소된다."""
    key = _home_row_shape(home)
    ia, hf, wp = home.ia, home.hf, home.wp

    identity = ia.resolve_slot_identity(".", home.ledger)
    assert (identity.key, identity.slot, identity.source) == (
        key, ".", ia.IDENTITY_FROM_LEDGER_SESSION)
    assert hf._resolve_state_slot(".", home.areas, home.ledger) == key
    assert hf._handoff_user_ack_target(None, ".") == (key, f"slot {key!r}")
    assert wp._normalize_slot(".") == "."
    assert wp.slot_pm_state_file(".") == (
        home.root / ".project_manager" / ".local" / "slots" / key / "pm_state.md")


def test_home_row_number_axis_and_paths_are_ledger_derived(home):
    """홈 행도 번호를 점유하고, 경로 소비자는 행의 `slot` 값(=PM 홈)을 얻는다."""
    key = _home_row_shape(home)
    ia, hf, wp = home.ia, home.hf, home.wp

    assert ia.repo_slot_numbers("adopter", home.ledger) == [1]
    assert sorted(wp._existing_slot_numbers("adopter", wp.list_leases())) == [1]
    assert ia.slot_path_for_number("adopter", 1, home.ledger) == "."
    assert ia.resolve_actor_slot("adopter", home.ledger) == key
    assert hf._resolve_explicit_identity_slot("adopter", 1, home.ledger) == (".", None)
    assert hf._resolve_explicit_identity_slot("adopter", None, home.ledger) == (".", None)
    resolved = home.bp._auto_slot(home.areas, home.ledger)
    assert (resolved.key, resolved.slot) == (key, ".")
    assert hf._regression_cwd(None, home.areas, home.ledger) == str(home.root)


def test_home_row_is_not_reported_stale_and_is_slot_axis(home):
    """홈 행은 git worktree 목록의 값 域 밖이라 stale 판정 대상이 아니고, 축은 slot 이다."""
    key = _home_row_shape(home)
    result = home.wp.reconcile_worktrees(git_runner=lambda argv: (0, ""))
    assert [l.slot for l in result.stale] == []

    identity, source = home.lg.resolve_snapshot_identity(home.root, home.root)
    assert (identity, source) == (key, home.lg.IDENTITY_SOURCE_CWD_LEASE)
    assert home.lg._checkpoint_identity_axes(home.root, home.root, identity, source) == \
        (None, key)


def test_pool_slot_without_worktree_is_still_stale(home):
    """대조군 — pool 자리(`work/…`) 행은 worktree 가 없으면 여전히 stale 이다(과잉 완화 방지)."""
    home.write_ledger([_pool_row("A", 1, "A_1"), {"slot": ".", "repo": "A",
                                                  "session": "A_9", "state": "leased"}])
    home.write_areas(["A"])
    result = home.wp.reconcile_worktrees(git_runner=lambda argv: (0, ""))
    assert [l.slot for l in result.stale] == ["work/A_1"]


# ════════════════════════════════════════════════════════════════════════
# 4. 판정 불능은 fail-loud — 장부 부재/손상에서 정체성을 지어내지 않는다
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("ledger_text", [None, "{not valid json", '{"leases": "nope"}'])
def test_unresolvable_identity_never_invents_a_key(home, ledger_text):
    """장부 부재/손상 + 경로도 슬롯 키가 아님 → 정체성 미해소(None) · 대체값 0."""
    if ledger_text is not None:
        home.ledger.write_text(ledger_text, encoding="utf-8")
    ia, hf, wp = home.ia, home.hf, home.wp

    assert ia.resolve_slot_identity(".", home.ledger) is None
    assert hf._resolve_state_slot(".", home.areas, home.ledger) is None
    assert hf._parse_worktree_slot(".") is None
    with pytest.raises(wp.SlotResolutionError):
        wp.slot_pm_state_file(".")
    # 대조군 — 같은 장부 상태라도 경로가 슬롯 키를 지니면 pool 명명 규약으로 해소된다.
    assert hf._resolve_state_slot("work/A_1", home.areas, home.ledger) == "A_1"


def test_corrupt_ledger_does_not_downgrade_actor_resolution_to_a_guess(home):
    """손상 장부에서 actor 해소는 미해소(None)다 — 번호/이름으로 세션을 짓지 않는다."""
    home.ledger.write_text("{not valid json", encoding="utf-8")
    assert home.ia.resolve_actor_slot("A", home.ledger) is None
    assert home.ia.repo_slot_numbers("A", home.ledger) is None
    assert home.ia.slot_path_for_number("A", 1, home.ledger) is None


def _divergent_shape(home: Home) -> None:
    """같은 슬롯(`work/A_1`)을 서로 다른 session(`A_1`/`B_2`)이 주장하는 실 장부 — 모순 반례."""
    home.write_ledger([
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/A_1", "repo": "A", "session": "B_2", "state": "leased"},
    ])
    home.write_areas(["A"])
    home.mkslot("work/A_1")


def test_divergent_duplicate_rows_fail_loud_instead_of_picking_one(home):
    """같은 슬롯을 서로 다른 session 으로 주장하는 중복 행 → 모순 판정·임의 선택 0.

    이전 라운드는 이 자리에서 `slot-path` 폴백(`A_1`)을 **기대값으로 박아** 결함을 잠갔다
    (리뷰 F-004). 판정형은 모순을 `identity=None` + `SLOT_IDENTITY_CONFLICT` 로 낸다 —
    "장부가 두 답을 준다"가 "장부가 답을 안 준다"와 같은 값으로 접히지 않는다.
    """
    _divergent_shape(home)
    with pytest.raises(home.ia.SlotResolutionError):
        home.ia.resolve_actor_slot("A", home.ledger)

    resolution = home.ia.slot_identity_resolution("work/A_1", home.ledger)
    assert resolution.status == home.ia.SLOT_IDENTITY_CONFLICT
    assert resolution.identity is None
    assert "A_1" in resolution.detail and "B_2" in resolution.detail


# ════════════════════════════════════════════════════════════════════════
# 5. 3각 파리티 — board.session_name ↔ worktree_pool ↔ pm_config
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def trio(home, monkeypatch):
    """세 모듈을 같은 tmp PM 홈에 동시 배선한다 — 같은 장부를 세 해소가 함께 읽는다."""
    board = _load("board")
    pm_config = _load("pm_config")
    _rebind(board, home.root)
    _rebind(pm_config, home.root)
    monkeypatch.setattr(board, "_git_config_email", lambda: None)
    # 두 모듈의 동적 board 로더가 **이 홈에 배선된** board 를 보게 한다(REPO 대조 가드 통과).
    monkeypatch.setattr(home.wp, "_load_board", lambda: board)
    real_load = pm_config._load_module
    monkeypatch.setattr(
        pm_config, "_load_module",
        lambda name, filename: board if name == "board" else real_load(name, filename))
    return board, home.wp, pm_config


def _trio_sessions(trio):
    board, wp, pm_config = trio
    return board.session_name(), wp._default_session(), pm_config._default_session()


@pytest.mark.parametrize("env_var", ["PM_SESSION_NAME", "CLAUDE_SESSION_NAME"])
def test_trio_env_layer_is_identical(home, trio, monkeypatch, env_var):
    """env 층 — 세 모듈이 같은 변수를 같은 순서로 읽는다(구 alias 포함)."""
    home.write_ledger([_pool_row("A", 1, "A_1"), _pool_row("A", 2, "A_2")])
    home.write_areas(["A"])
    monkeypatch.setenv(env_var, "env-session")
    assert _trio_sessions(trio) == ("env-session",) * 3


def test_trio_env_precedence_pm_over_deprecated_alias(home, trio, monkeypatch):
    """둘 다 설정되면 세 모듈 모두 `PM_SESSION_NAME` 승."""
    home.write_ledger([])
    home.write_areas(["A"])
    monkeypatch.setenv("PM_SESSION_NAME", "pm-wins")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "legacy")
    assert _trio_sessions(trio) == ("pm-wins",) * 3


def test_trio_single_lease_layer_is_identical(home, trio):
    """단일-lease 층 — leased 행이 하나면 세 모듈 모두 **그 행의 session 값**으로 해소한다."""
    home.write_ledger([_pool_row("A", 1, "custom-session")])
    home.write_areas(["A"])
    assert _trio_sessions(trio) == ("custom-session",) * 3


def test_trio_single_registration_layer_is_identical(home, trio):
    """단일-등록 유도 층 — 등록 repo 1개 && 장부 0행이면 세 모듈이 같은 값을 유도한다."""
    home.write_ledger([])
    home.write_areas(["A"])
    assert _trio_sessions(trio) == ("A_1",) * 3


def test_trio_ambiguous_and_corrupt_share_unresolved_judgment(home, trio):
    """모호(leased ≥2)·손상 장부 — 세 모듈이 함께 미해소로 판정한다(tail 표현만 상이).

    board/pm_config 는 None(귀속 해소는 fail-loud 로 이어짐), worktree_pool 은 lease *취득* 용
    국소 임시 명명(`<host>-<pid>`)으로 폴백한다 — 층 판정은 같고 tail 만 다르다.
    """
    board, wp, pm_config = trio
    home.write_areas(["A"])
    home.write_ledger([_pool_row("A", 1, "A_1"), _pool_row("A", 2, "A_2")])
    assert board.session_name() is None and pm_config._default_session() is None
    ambiguous_tail = wp._default_session()
    assert ambiguous_tail not in {"A_1", "A_2"}

    home.ledger.write_text("{not valid json", encoding="utf-8")
    assert board.session_name() is None and pm_config._default_session() is None
    assert wp._default_session() not in {"A_1", "A_2"}


def test_trio_reads_the_same_ledger_file(home, trio):
    """세 해소가 **같은 장부 파일**을 읽는다 — 파일을 바꾸면 셋이 함께 바뀐다(공유 소스 확인)."""
    home.write_areas(["A"])
    home.write_ledger([_pool_row("A", 1, "first")])
    assert _trio_sessions(trio) == ("first",) * 3
    home.write_ledger([_pool_row("A", 1, "second")])
    assert _trio_sessions(trio) == ("second",) * 3


def test_trio_ignores_per_clone_conf_session_key(home, trio):
    """per-clone `local.conf session=` 은 층이 아니다 — 세 모듈 어디서도 읽지 않는다."""
    (home.root / ".project_manager" / "local.conf").write_text(
        "session=conf-note\n", encoding="utf-8")
    home.write_areas(["A"])
    home.write_ledger([_pool_row("A", 1, "ledger-session")])
    assert _trio_sessions(trio) == ("ledger-session",) * 3
    assert os.environ.get("PM_SESSION_NAME") is None


# ════════════════════════════════════════════════════════════════════════
# 6. 실제 진입 E2E — bootstrap · task · worktree · finish (리뷰 F-001)
#
# 해소 helper 만 값으로 재던 이전 라운드에선, 실 진입점이 그 helper 를 우회해 경로를 다시
# 조립하는 것을 못 잡았다. 아래 넷은 **CLI/진입 함수**를 그대로 태우고 실 장부 반례
# (`{slot: ".", repo: "A", session: "A_1"}` — 경로에 이름이 없는 홈 행)로 판정한다.
# ════════════════════════════════════════════════════════════════════════


def _home_row_A(home: Home) -> None:
    """리뷰가 재현에 쓴 실 장부 반례 그대로 — PM 홈 자신을 가리키는 행."""
    home.write_ledger([{"slot": ".", "repo": "A", "session": "A_1", "pid": 0,
                        "started": "t", "state": "leased", "test_cmd": None,
                        "bound": True}])
    home.write_areas(["A"])


def test_entry_bootstrap_bind_reaches_the_ledger_row(home, monkeypatch):
    """`/pm-bootstrap A --slot 1` 의 bind 진입이 **그 행**에 도달한다(새 행을 파지 않는다).

    경로를 `work/A_1` 로 재조립하면 bind 는 장부에 없는 슬롯을 새로 만들고(행 2개) 세션이
    phantom 작업공간을 기준으로 굴러간다.
    """
    _home_row_A(home)
    # slot pm_state 생성 경로(bind → ensure)가 실제로 돌도록 tracked template 을 홈에 깐다.
    template = REPO / ".project_manager" / "wiki" / "pm_state.template.md"
    (home.root / ".project_manager" / "wiki" / "pm_state.template.md").write_text(
        template.read_text(encoding="utf-8"), encoding="utf-8")

    bootstrap = home.bp.PmBootstrap(worktree_pool=home.wp, areas_file=home.areas)
    identity = bootstrap._bind_and_identity("A", 1)

    assert identity["slot"] == "."                      # 행의 경로 값(재조립 아님)
    assert identity["slot_path"] == str(home.root)      # cwd = PM 홈 자신
    assert identity["slot_number"] == 1                 # 카드 재접속 좌표
    rows = json.loads(home.ledger.read_text(encoding="utf-8"))["leases"]
    assert [r["slot"] for r in rows] == ["."]           # 새 행을 파지 않았다
    assert rows[0]["session"] == "A_1"


def test_entry_bootstrap_phase0_admits_the_ledger_row(home, capsys):
    """0단계 진입 검사(`/pm-bootstrap A --slot 1`)가 그 행을 실재로 인정한다.

    경로를 재조립하면 홈 행이 "장부·폴더 어디에도 없습니다"로 rc 1 차단된다 — 세션이 아예
    시작되지 못한다.
    """
    _home_row_A(home)
    bootstrap = home.bp.PmBootstrap(worktree_pool=home.wp, areas_file=home.areas)
    assert bootstrap._phase0_preflight("A", 1) == 0
    assert "장부·폴더 어디에도 없습니다" not in capsys.readouterr().out


def test_entry_task_workspace_resolves_the_ledger_row(home):
    """task 명시 해소(`--repo A --slot 1`)가 행의 경로에 도달한다 — `work/A_1` 미보유 거부 없음."""
    _home_row_A(home)
    ia = home.ia
    identity = ia.Identity(kind="slot", repo="A", slot=1, session="A_1", task="A_1")
    workspace = ia.resolve_task_workspace(identity, home.ledger)
    assert (workspace.slot, workspace.repo, workspace.session) == (".", "A", "A_1")


def test_entry_worktree_cli_targets_the_ledger_row(home, monkeypatch):
    """`worktree_pool dev --repo A --slot 1` 진입이 행의 슬롯을 대상으로 잡는다."""
    _home_row_A(home)
    seen: list[str] = []
    monkeypatch.setattr(home.wp, "dev",
                        lambda slot, sub, branch: (seen.append(slot), (0, ""))[1])
    rc = home.wp.main(["dev", "vendor/sub", "dev-branch", "--repo", "A", "--slot", "1"])
    assert rc == 0
    assert seen == ["."]


def test_entry_finish_attributes_the_ledger_identity(home, monkeypatch):
    """`ticket_finish --repo A --slot 1` 의 귀속 세션이 행의 정체성이다(경로 basename 아님).

    경로 basename 을 정체성으로 읽으면 홈 행(`slot="."`)의 귀속 세션이 **빈 문자열**이 된다.
    """
    _home_row_A(home)
    tf = _load("ticket_finish")
    _rebind(tf, home.root)
    # 슬롯 해소를 위임받는 형제 pm_handoff 도 **같은 홈**에 배선한다 — 동적 로드본은 실 repo
    # 경로를 들고 태어나므로, 배선하지 않으면 이 진입이 tmp 장부를 못 보고 canonical 폴백으로
    # 새어 나가 테스트가 엉뚱한 이유로 green 이 된다(대역이 아니라 배선).
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: home.hf)
    captured: dict = {}

    class _Finisher:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, **kwargs):
            captured.update(kwargs)
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _Finisher)
    rc = tf._main(["T-0001", "--no-pytest", "--repo", "A", "--slot", "1"])
    assert rc == 0
    assert captured["session"] == "A_1"
    assert captured["regression_cwd"] == str(home.root)


# ════════════════════════════════════════════════════════════════════════
# 7. 렌더→재해소 왕복 — 엔진이 낸 지시가 다시 해소되는가 (리뷰 F-002)
# ════════════════════════════════════════════════════════════════════════

_BOOTSTRAP_TRIGGER_RE = re.compile(r"pm-bootstrap (?P<repo>\S+) --slot (?P<slot>\d+)")


def _rendered_trigger(home: Home, slot_value: str) -> "tuple[str, int] | None":
    """handoff 가 복사 블록에 렌더한 명시 트리거를 파싱해 `(repo, N)` 으로 돌려준다."""
    rendered = home.hf._inject_slot_into_template("/pm-bootstrap\n", slot_value)
    match = _BOOTSTRAP_TRIGGER_RE.search(rendered)
    if match is None:
        return None
    return match.group("repo"), int(match.group("slot"))


def test_rendered_trigger_round_trips_on_divergent_session_row(home):
    """`repo=A · slot=work/A_1 · session=B_7` — 렌더한 트리거가 **같은 슬롯으로 재해소**된다.

    session 을 분해해 `--repo B --slot 7` 을 렌더하면 즉시 재해소가 M3(repo B 활성 행 부재)로
    거부된다 — 엔진이 스스로 낸 지시가 작동하지 않는 상태였다(리뷰 F-002 실측).
    """
    _mismatch_shape(home)
    trigger = _rendered_trigger(home, "work/A_1")
    assert trigger == ("A", 1)

    repo, number = trigger
    slot, error = home.hf._resolve_explicit_identity_slot(repo, number, home.ledger)
    assert (slot, error) == ("work/A_1", None)          # 같은 슬롯으로 되돌아온다
    assert home.ia.resolve_slot_identity(slot, home.ledger).key == "B_7"


def test_rendered_trigger_round_trips_on_home_row(home):
    """경로에 이름이 없는 홈 행도 트리거→재해소가 같은 슬롯으로 닫힌다."""
    _home_row_A(home)
    trigger = _rendered_trigger(home, ".")
    assert trigger == ("A", 1)
    assert home.hf._resolve_explicit_identity_slot(*trigger, home.ledger) == (".", None)


def test_unresolvable_coordinates_render_no_explicit_trigger(home):
    """장부가 좌표를 주지 못하면 명시 트리거를 **만들지 않는다**(틀린 지시를 내느니 bare 유지)."""
    # 홈 행을 task 명의로 대여 — 경로에도 session 에도 repo 기준 번호가 없다.
    home.write_ledger([{"slot": ".", "repo": "A", "session": "job1", "state": "leased"}],
                      tasks=[{"name": "job1", "prefix": None, "pid": 0, "started": "t"}])
    home.write_areas(["A"])
    assert home.hf._parse_worktree_slot(".") is None
    assert _rendered_trigger(home, ".") is None
    assert home.hf._inject_slot_into_template("/pm-bootstrap\n", ".") == "/pm-bootstrap\n"


def test_session_axis_helpers_read_coordinates_from_the_ledger(home, monkeypatch):
    """`board._repo_from_session`·`pm_bootstrap._session_owns_untagged` 도 같은 축이다.

    세션 이름 말단을 뜯으면 `B_7` 은 repo `B`·slot 7 이지만, 장부는 그 세션이 **A 의 1번 슬롯**
    을 들고 있다고 말한다. 이름 분해가 남아 있으면 prefix 유도·무태그 소유 판정이 갈린다.
    """
    _mismatch_shape(home)
    board = _load("board")
    _rebind(board, home.root)
    assert board._repo_from_session("B_7") == "A"
    assert board._session_slot_coordinates("B_7") == ("A", 1)
    monkeypatch.setattr(home.bp, "REPO", home.root)
    assert home.bp._session_owns_untagged("B_7") is True       # 장부: A 의 1번 슬롯 명의
    # 같은 이름이 2번 슬롯을 들고 있으면 판정이 뒤집힌다 — 값이 이름이 아니라 장부에서 온다는 증거
    # (이름만 보면 두 형상 모두 `_7` 이라 판정이 같아야 한다).
    home.write_ledger([{"slot": "work/A_2", "repo": "A", "session": "B_7", "state": "leased"}])
    assert home.bp._session_owns_untagged("B_7") is False


def test_session_axis_helpers_keep_name_rule_without_ledger_rows(home):
    """역방향 — 장부에 그 세션 행이 없으면 이름 규약이 유일한 진실이라 종전 값 그대로다."""
    home.write_ledger([])
    board = _load("board")
    _rebind(board, home.root)
    assert board._repo_from_session("project_manager_1") == "project_manager"
    assert board._repo_from_session("my-session") is None
    assert board._session_slot_coordinates("a_2_3") == ("a_2", 3)
    assert home.bp._session_owns_untagged("x_1", home.ledger) is True
    assert home.bp._session_owns_untagged("x_2", home.ledger) is False
    assert home.bp._session_owns_untagged("task:foo_1", home.ledger) is False
    assert home.bp._session_owns_untagged(None, home.ledger) is True


# ════════════════════════════════════════════════════════════════════════
# 8. 값 경계 — 장부 session 키도 POSIX·Windows 공통 안전 마디 (리뷰 F-003)
# ════════════════════════════════════════════════════════════════════════

# 두 축이 **같은 술어**를 소비하는지 보는 반례 — 첫 값이 리뷰 실측(`C:_1` 이 키로 통과해
# `PureWindowsPath("D:/pm/.../slots") / "C:_1"` 가 부모를 버렸다).
_UNSAFE_SLOT_VALUES = ["C:_1", "D:", "a<b_1", 'a"b_1', "a|b_1", "a?b_1", "a*b_1",
                       "con", "NUL", "com1", "a_1.", "..", "a\x00_1"]


@pytest.mark.parametrize("value", _UNSAFE_SLOT_VALUES)
def test_unsafe_component_rejected_on_both_axes(home, value):
    """같은 값이 슬롯 키 축과 슬롯 경로 축 **양쪽**에서 거부된다(술어 공유)."""
    assert home.ia.is_slot_key(value) is False
    assert home.ia.path_component_rejection(value) is not None
    with pytest.raises(home.wp.SlotResolutionError):
        home.wp._normalize_slot(value)


def test_whitespace_padded_value_is_stripped_not_smuggled(home):
    """앞뒤 공백은 키 축에선 거부, 경로 축에선 **정규화**된다 — 어느 쪽도 공백 마디를 만들지 않는다."""
    assert home.ia.is_slot_key("a_1 ") is False
    assert home.ia.path_component_rejection("a_1 ") is not None
    assert home.wp._normalize_slot(" a_1 ") == "work/a_1"     # 값 자체의 패딩은 정규화(종전)
    with pytest.raises(home.wp.SlotResolutionError):
        home.wp._normalize_slot("work/ a_1")                  # 마디 안의 공백은 거부


def test_drive_marker_session_key_cannot_escape_the_state_root(home):
    """드라이브 표기 session 키는 정체성이 되지 못한다 — 상태 경로가 홈 밖을 가리키지 않는다."""
    home.write_ledger([{"slot": "work/A_1", "repo": "A", "session": "C:_1",
                        "state": "leased"}])
    home.write_areas(["A"])
    home.mkslot("work/A_1")
    identity = home.ia.resolve_slot_identity("work/A_1", home.ledger)
    # 장부 층이 그 값을 정체성으로 채택하지 않으므로 pool 명명 규약(경로 마디)이 키다.
    assert (identity.key, identity.source) == ("A_1", home.ia.IDENTITY_FROM_SLOT_PATH)
    state = home.wp.slot_pm_state_file("work/A_1")
    slots_root = home.root / ".project_manager" / ".local" / "slots"
    assert state == slots_root / "A_1" / "pm_state.md"
    assert slots_root in state.parents


def test_real_session_key_shapes_stay_allowed(home):
    """역방향 — 실재하는 정상 값이 새로 차단되지 않는다(과잉 조임 방지)."""
    assert home.ia.is_slot_key("project_manager_5") is True
    assert home.ia.is_slot_key("a_2_3") is True
    # 슬롯 키가 아닌 정상 session 값(task 명·임시 명명)은 종전대로 슬롯 축이 아닐 뿐,
    # 경로 마디로서는 안전하다 — 두 판정을 섞지 않는다.
    for value in ("main", "host-4242", "my-session"):
        assert home.ia.is_slot_key(value) is False
        assert home.ia.path_component_rejection(value) is None
    for slot in ("work/project_manager_5", "work/legacy-dir", ".", "work/a_2_3"):
        assert home.wp._normalize_slot(slot) == slot


def test_existing_traversal_and_symlink_defenses_stay_green(home, tmp_path):
    """역방향 — 직전 라운드가 넣은 traversal·symlink 탈출 거부가 그대로 red 를 낸다."""
    for value in ("", "../x", "work/../x", "work/x/../y", "/etc", "work\\A_1",
                  "work/", "work/A_1/sub"):
        with pytest.raises(home.wp.SlotResolutionError):
            home.wp._normalize_slot(value)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home.root / "work" / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(home.wp.SlotResolutionError):
        home.wp._normalize_slot("work/escape")


# ════════════════════════════════════════════════════════════════════════
# 9. 판정 구분 — 부재 · 읽기 불가 · 행 부재 · 모순 (리뷰 F-004)
# ════════════════════════════════════════════════════════════════════════


def test_identity_resolution_distinguishes_four_judgments(home):
    """네 판정이 각각 다른 값으로 나온다 — "못 읽었다"와 "두 답이 있다"가 안 접힌다."""
    ia = home.ia
    # 1. 장부 부재(파일 없음).
    assert ia.slot_identity_resolution(".", home.ledger).status == ia.SLOT_IDENTITY_LEDGER_MISSING
    # 2. 읽기 불가(파일은 있으나 손상).
    home.ledger.write_text("{not valid json", encoding="utf-8")
    assert ia.slot_identity_resolution(".", home.ledger).status == \
        ia.SLOT_IDENTITY_LEDGER_UNREADABLE
    # 3. 행 부재(정상 장부인데 그 슬롯 행이 없다).
    home.write_ledger([_pool_row("A", 1, "A_1")])
    assert ia.slot_identity_resolution(".", home.ledger).status == ia.SLOT_IDENTITY_ROW_ABSENT
    # 4. 중복 모순.
    _divergent_shape(home)
    assert ia.slot_identity_resolution("work/A_1", home.ledger).status == \
        ia.SLOT_IDENTITY_CONFLICT
    # 대조군 — 정상 해소.
    home.write_ledger([_pool_row("A", 1, "A_1")])
    assert ia.slot_identity_resolution("work/A_1", home.ledger).status == ia.SLOT_IDENTITY_OK


def test_slot_path_resolution_distinguishes_four_judgments(home):
    """번호→경로 축도 같은 네 판정을 구분한다(모호 None 이 canonical 폴백과 안 섞이게)."""
    ia = home.ia
    assert ia.slot_path_resolution("A", 1, home.ledger).status == ia.SLOT_PATH_LEDGER_MISSING
    home.ledger.write_text("{not valid json", encoding="utf-8")
    assert ia.slot_path_resolution("A", 1, home.ledger).status == ia.SLOT_PATH_LEDGER_UNREADABLE
    home.write_ledger([_pool_row("A", 2, "A_2")])
    assert ia.slot_path_resolution("A", 1, home.ledger).status == ia.SLOT_PATH_ROW_ABSENT
    home.write_ledger([{"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
                       {"slot": "nested/A_1", "repo": "A", "session": "A_1", "state": "leased"}])
    ambiguous = ia.slot_path_resolution("A", 1, home.ledger)
    assert ambiguous.status == ia.SLOT_PATH_CONFLICT and ambiguous.path is None


def test_conflict_fails_loud_at_write_entries(home):
    """모순은 **쓰기 진입 전부**에서 fail-loud — 경로 추측으로 통과시키지 않는다."""
    _divergent_shape(home)
    # pm_state 앵커(쓰기 위치).
    with pytest.raises(home.wp.SlotResolutionError):
        home.wp.slot_pm_state_file("work/A_1")
    # handoff 진입 검문(log/current.md·pm_state.md 쓰기).
    refusal = home.hf._slot_identity_admission("work/A_1", "worktree_slot")
    assert refusal is not None and "모순" in refusal
    # actor 해소(귀속 쓰기).
    with pytest.raises(home.ia.SlotResolutionError):
        home.ia.resolve_actor_slot("A", home.ledger)


def test_conflict_is_tolerated_on_read_display(home):
    """읽기 표시는 종전대로 관용 — 모순에서도 표시 연속성은 pool 규약 폴백으로 유지된다."""
    _divergent_shape(home)
    resolution = home.ia.slot_identity_resolution("work/A_1", home.ledger)
    assert resolution.fallback.key == "A_1"
    assert home.ia.resolve_slot_identity("work/A_1", home.ledger).key == "A_1"
    assert home.hf._resolve_state_slot("work/A_1", home.areas, home.ledger) == "A_1"


def test_number_ambiguity_is_not_downgraded_to_canonical_guess(home):
    """번호 모호는 canonical 조립으로 폴백되지 않는다 — 명시 진입이 fail-loud 한다."""
    home.write_ledger([{"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
                       {"slot": "nested/A_1", "repo": "A", "session": "A_1", "state": "leased"}])
    home.write_areas(["A"])
    slot, error = home.hf._resolve_explicit_identity_slot("A", 1, home.ledger)
    assert slot is None and "모순" in error
    with pytest.raises(home.wp.SlotResolutionError):
        home.wp._explicit_slot_path("A", 1)
    with pytest.raises(home.bp.SlotResolutionError):
        home.bp._explicit_slot_path("A", 1, home.ledger)
    with pytest.raises(home.ia.WorkspaceResolutionError):
        home.ia.resolve_task_workspace(
            home.ia.Identity(kind="slot", repo="A", slot=1, session="A_1", task="A_1"),
            home.ledger)


def test_normal_shapes_are_not_judged_conflicting(home):
    """역방향 — 정상 단일 행·정상 다중 task 형상이 모순으로 오판되지 않는다."""
    # 단일 행.
    home.write_ledger([_pool_row("A", 1, "A_1")])
    assert home.ia.slot_identity_resolution("work/A_1", home.ledger).status == \
        home.ia.SLOT_IDENTITY_OK
    # 다중 task — 같은 task 명의가 슬롯 여러 개, 슬롯마다 행 1개(정상 형상).
    home.write_ledger([_pool_row("A", 1, "job1"), _pool_row("A", 2, "job1"),
                       _pool_row("B", 1, "job2")],
                      tasks=[{"name": "job1", "prefix": None, "pid": 0, "started": "t"},
                             {"name": "job2", "prefix": None, "pid": 0, "started": "t"}])
    home.write_areas(["A", "B"])
    for slot, key in (("work/A_1", "A_1"), ("work/A_2", "A_2"), ("work/B_1", "B_1")):
        resolution = home.ia.slot_identity_resolution(slot, home.ledger)
        assert (resolution.status, resolution.identity.key) == (home.ia.SLOT_IDENTITY_OK, key)
        assert home.wp.slot_pm_state_file(slot).parent.name == key
    # idle 로 반납된 옛 행 + 새 leased 행이 같은 슬롯에 있어도 모순이 아니다(session 은 하나).
    home.write_ledger([{"slot": "work/A_1", "repo": "A", "session": "", "state": "idle"},
                       {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"}])
    assert home.ia.slot_identity_resolution("work/A_1", home.ledger).identity.key == "A_1"
