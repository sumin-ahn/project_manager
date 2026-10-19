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
  5. **3각 파리티** — `lease_store.session_name` · `worktree_pool._default_session` ·
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
from pm_engine.domain import identity_args
from pm_engine.infra import lease_store, pm_paths
from pm_engine.app import slot_resolution

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
        self.ia = identity_args
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
           "started": "t", "state": "leased", "test_cmd": None}
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
                                    "started": "t"}])
    home.write_areas([_ADOPTER_REPO])
    home.mkslot(*[f"work/{_ADOPTER_REPO}_{n}" for n in range(1, 8)])
    home.mktask_dir(_TASK_SESSION)


# ════════════════════════════════════════════════════════════════════════
# 1. 동작 무변경 스냅샷 — adopter#0 형상(행 7·task session·readonly·idle)
# ════════════════════════════════════════════════════════════════════════


def test_single_slot_shape_resolves_to_ledger_row(home):
    """단일 self-host — 자동해소가 행의 정체성 키와 **행의 경로 값**을 함께 낸다."""
    home.write_ledger([_pool_row("A", 1, "A_1", test_cmd="pytest -q")])
    home.write_areas(["A"])
    home.mkslot("work/A_1")

    resolved = slot_resolution._auto_slot(pm_paths.home_at(home.bp.REPO), home.areas, home.ledger)
    assert (resolved.key, resolved.slot) == ("A_1", "work/A_1")
    assert slot_resolution._regression_cwd(pm_paths.home_at(home.hf.REPO), None, home.areas, home.ledger) == \
        str(home.root / "work/A_1")


# ════════════════════════════════════════════════════════════════════════
# 2. 결정적 케이스 — 장부 등록 이름 ≠ 디렉토리 basename
# ════════════════════════════════════════════════════════════════════════


def _mismatch_shape(home: Home) -> None:
    """장부가 `work/A_1` 슬롯을 세션 `B_7` 로 등록한 형상 — 경로 이름과 정체성이 갈린다."""
    home.write_ledger([{"slot": "work/A_1", "repo": "A", "session": "B_7", "started": "t", "state": "leased", "test_cmd": "pytest -q"}])
    home.write_areas(["A"])
    home.mkslot("work/A_1")


def test_actor_resolution_returns_ledger_session_verbatim(home):
    """`--repo` 단독 actor 해소는 행의 session 값 그대로 — 번호로 세션을 재조립하지 않는다."""
    _mismatch_shape(home)
    assert lease_store.resolve_actor_workspace("A", home.ledger).session == "B_7"
    # 경로 축은 행의 slot 값 — 세션에서 `work/B_7` 을 만들지 않는다.
    assert slot_resolution._resolve_explicit_identity_slot(pm_paths.home_at(home.hf.REPO), "A", 1, home.ledger) == \
        ("work/A_1", None)
    assert home.wp._resolve_actor_slot_for_repo("A") == "work/A_1"


def test_actor_resolution_returns_task_name_session(home):
    """슬롯을 task 명의로 대여한 홈에서 actor 해소는 그 task 이름을 낸다(장부 값 그대로).

    종전 구현은 번호에서 `<repo>_<N>` 을 재조립해 장부에 없는 세션을 냈다 — bare 해소
    (`session_name` 단일-lease 층)와 갈리는 값이었다.
    """
    home.write_ledger([_pool_row(_ADOPTER_REPO, 1, _TASK_SESSION)],
                      tasks=[{"name": _TASK_SESSION, "prefix": None, "started": "t"}])
    home.write_areas([_ADOPTER_REPO])
    home.mkslot(f"work/{_ADOPTER_REPO}_1")
    assert lease_store.resolve_actor_workspace(_ADOPTER_REPO, home.ledger).session == _TASK_SESSION


# ════════════════════════════════════════════════════════════════════════
# 3. 경로 마디가 없는 슬롯 — 장부 행이 정체성을 준다
# ════════════════════════════════════════════════════════════════════════


def _home_row_shape(home: Home, repo: str = "adopter") -> str:
    """PM 홈 자신을 가리키는 행(`slot="."`)을 실 장부 파일로 세운다 — 읽기 해소만 검증한다."""
    home.write_ledger([{"slot": ".", "repo": repo, "session": f"{repo}_1", "started": "t", "state": "leased", "test_cmd": None,
                        "bound": True}])
    home.write_areas([repo])
    return f"{repo}_1"


def test_home_row_number_axis_and_paths_are_ledger_derived(home):
    """홈 행도 번호를 점유하고, 경로 소비자는 행의 `slot` 값(=PM 홈)을 얻는다."""
    key = _home_row_shape(home)
    hf, wp = home.hf, home.wp

    assert lease_store.repo_slot_numbers("adopter", home.ledger) == [1]
    assert sorted(wp._existing_slot_numbers("adopter", wp.list_leases())) == [1]
    assert lease_store.slot_path_resolution("adopter", 1, home.ledger).path == "."
    assert lease_store.resolve_actor_workspace("adopter", home.ledger).session == key
    assert slot_resolution._resolve_explicit_identity_slot(pm_paths.home_at(hf.REPO), "adopter", 1, home.ledger) == (".", None)
    resolved = slot_resolution._auto_slot(pm_paths.home_at(home.bp.REPO), home.areas, home.ledger)
    assert (resolved.key, resolved.slot) == (key, ".")
    assert slot_resolution._regression_cwd(pm_paths.home_at(hf.REPO), None, home.areas, home.ledger) == str(home.root)


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


def test_corrupt_ledger_does_not_downgrade_actor_resolution_to_a_guess(home):
    """손상 장부에서 actor 해소는 미해소(None)다 — 번호/이름으로 세션을 짓지 않는다."""
    home.ledger.write_text("{not valid json", encoding="utf-8")
    assert lease_store.resolve_actor_workspace("A", home.ledger) is None
    assert lease_store.repo_slot_numbers("A", home.ledger) is None
    assert lease_store.slot_path_resolution("A", 1, home.ledger).path is None


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
        lease_store.resolve_actor_workspace("A", home.ledger)

    resolution = lease_store.slot_identity_resolution("work/A_1", home.ledger)
    assert resolution.status == home.ia.SLOT_IDENTITY_CONFLICT
    assert resolution.identity is None
    assert "A_1" in resolution.detail and "B_2" in resolution.detail


# ════════════════════════════════════════════════════════════════════════
# 5. 3각 파리티 — lease_store.session_name ↔ worktree_pool ↔ pm_config
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
    return lease_store.session_name(board._home()), wp._default_session(), pm_config._default_session()


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


def test_trio_home_row_layer_is_identical(home, trio):
    """홈 슬롯 행 — 홈 자신을 가리키는 N=1 행 하나로 세 모듈이 같은 세션을 해소한다."""
    home.write_areas(["A"])
    home.write_ledger([{"slot": ".", "repo": "A", "session": "A_1", "started": "2026-08-22T00:00:00+00:00", "state": "leased",
                        "test_cmd": None, "bound": True}])
    assert _trio_sessions(trio) == ("A_1",) * 3


def test_trio_zero_rows_does_not_derive_from_registration(home, trio):
    """등록 repo 1개라도 장부 행이 0이면 유도하지 않는다 — 등록은 장부 행이 한다.

    board/pm_config 는 미해소(None), worktree_pool 은 lease 취득용 국소 임시 명명으로 떨어진다.
    이 층을 되살리면 세 값이 다시 `A_1` 이 되어 이 단언이 red 다.
    """
    board, wp, pm_config = trio
    home.write_areas(["A"])
    home.write_ledger([])
    assert lease_store.session_name(board._home()) is None and pm_config._default_session() is None
    assert wp._default_session() != "A_1"


def test_trio_ambiguous_and_corrupt_share_unresolved_judgment(home, trio):
    """모호(leased ≥2)·손상 장부 — 세 모듈이 함께 미해소로 판정한다(tail 표현만 상이).

    board/pm_config 는 None(귀속 해소는 fail-loud 로 이어짐), worktree_pool 은 lease *취득* 용
    국소 임시 명명(`<host>-<pid>`)으로 폴백한다 — 층 판정은 같고 tail 만 다르다.
    """
    board, wp, pm_config = trio
    home.write_areas(["A"])
    home.write_ledger([_pool_row("A", 1, "A_1"), _pool_row("A", 2, "A_2")])
    assert lease_store.session_name(board._home()) is None and pm_config._default_session() is None
    ambiguous_tail = wp._default_session()
    assert ambiguous_tail not in {"A_1", "A_2"}

    home.ledger.write_text("{not valid json", encoding="utf-8")
    assert lease_store.session_name(board._home()) is None and pm_config._default_session() is None
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
    home.write_ledger([{"slot": ".", "repo": "A", "session": "A_1", "started": "t", "state": "leased", "test_cmd": None,
                        "bound": True}])
    home.write_areas(["A"])


def test_entry_task_workspace_resolves_the_ledger_row(home):
    """task 명시 해소(`--repo A --slot 1`)가 행의 경로에 도달한다 — `work/A_1` 미보유 거부 없음."""
    _home_row_A(home)
    ia = home.ia
    identity = ia.Identity(kind="slot", repo="A", slot=1, task="A_1")
    workspace = lease_store.resolve_task_workspace(identity, home.ledger)
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
    identity = lease_store.resolve_slot_identity("work/A_1", home.ledger)
    # 장부 층이 그 값을 정체성으로 채택하지 않으므로 pool 명명 규약(경로 마디)이 키다.
    assert (identity.key, identity.source) == ("A_1", home.ia.IDENTITY_FROM_SLOT_PATH)


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
    assert lease_store.slot_identity_resolution(".", home.ledger).status == ia.SLOT_IDENTITY_LEDGER_MISSING
    # 2. 읽기 불가(파일은 있으나 손상).
    home.ledger.write_text("{not valid json", encoding="utf-8")
    assert lease_store.slot_identity_resolution(".", home.ledger).status == \
        ia.SLOT_IDENTITY_LEDGER_UNREADABLE
    # 3. 행 부재(정상 장부인데 그 슬롯 행이 없다).
    home.write_ledger([_pool_row("A", 1, "A_1")])
    assert lease_store.slot_identity_resolution(".", home.ledger).status == ia.SLOT_IDENTITY_ROW_ABSENT
    # 4. 중복 모순.
    _divergent_shape(home)
    assert lease_store.slot_identity_resolution("work/A_1", home.ledger).status == \
        ia.SLOT_IDENTITY_CONFLICT
    # 대조군 — 정상 해소.
    home.write_ledger([_pool_row("A", 1, "A_1")])
    assert lease_store.slot_identity_resolution("work/A_1", home.ledger).status == ia.SLOT_IDENTITY_OK


def test_slot_path_resolution_distinguishes_four_judgments(home):
    """번호→경로 축도 같은 네 판정을 구분한다(모호 None 이 canonical 폴백과 안 섞이게)."""
    ia = home.ia
    assert lease_store.slot_path_resolution("A", 1, home.ledger).status == ia.SLOT_PATH_LEDGER_MISSING
    home.ledger.write_text("{not valid json", encoding="utf-8")
    assert lease_store.slot_path_resolution("A", 1, home.ledger).status == ia.SLOT_PATH_LEDGER_UNREADABLE
    home.write_ledger([_pool_row("A", 2, "A_2")])
    assert lease_store.slot_path_resolution("A", 1, home.ledger).status == ia.SLOT_PATH_ROW_ABSENT
    home.write_ledger([{"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
                       {"slot": "nested/A_1", "repo": "A", "session": "A_1", "state": "leased"}])
    ambiguous = lease_store.slot_path_resolution("A", 1, home.ledger)
    assert ambiguous.status == ia.SLOT_PATH_CONFLICT and ambiguous.path is None


def test_normal_shapes_are_not_judged_conflicting(home):
    """역방향 — 정상 단일 행·정상 다중 task 형상이 모순으로 오판되지 않는다."""
    # 단일 행.
    home.write_ledger([_pool_row("A", 1, "A_1")])
    assert lease_store.slot_identity_resolution("work/A_1", home.ledger).status == \
        home.ia.SLOT_IDENTITY_OK
    # 다중 task — 같은 task 명의가 슬롯 여러 개, 슬롯마다 행 1개(정상 형상).
    home.write_ledger([_pool_row("A", 1, "job1"), _pool_row("A", 2, "job1"),
                       _pool_row("B", 1, "job2")],
                      tasks=[{"name": "job1", "prefix": None, "started": "t"},
                             {"name": "job2", "prefix": None, "started": "t"}])
    home.write_areas(["A", "B"])
    for slot, key in (("work/A_1", "A_1"), ("work/A_2", "A_2"), ("work/B_1", "B_1")):
        resolution = lease_store.slot_identity_resolution(slot, home.ledger)
        assert (resolution.status, resolution.identity.key) == (home.ia.SLOT_IDENTITY_OK, key)
    # idle 로 반납된 옛 행 + 새 leased 행이 같은 슬롯에 있어도 모순이 아니다(session 은 하나).
    home.write_ledger([{"slot": "work/A_1", "repo": "A", "session": "", "state": "idle"},
                       {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"}])
    assert lease_store.slot_identity_resolution("work/A_1", home.ledger).identity.key == "A_1"
