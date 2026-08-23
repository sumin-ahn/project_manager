"""묶음 고정 라운드 예산 — 역할 수열·순서 밖 거부·재설계 리셋.

여기서 지키는 성질은 다섯이다.
  (1) 묶음이 도는 라운드 수열은 장부 `budget` 값이 정한다(설계 → 구현 → 리뷰 → fix).
  (2) 예산을 넘긴 요청도, 순서 밖 역할 요청도 **예약 전에** 거부되고 처방은 재설계 하나다.
  (3) 판정은 사전판정과 board_lock 재확인 **두 지점**에 걸려 동시 준비가 예산을 함께 넘지 못한다.
  (4) `cluster replan` 이 예산을 리셋하고 기준선을 박제하면 다음 주기가 다시 설계부터 열린다.
  (5) 크기 1 묶음도, 티켓 단축 표기 준비도 같은 판정을 받고(표면이 판정을 정하지 않는다),
      장부 없는 옛 티켓 경로만 이 축의 영향을 받지 않는다.

hermetic 패턴은 `test_delegate_cluster_rounds.py`(라운드 예약)와 `test_board_cluster.py`
(실 board git)를 각각 그대로 따른다.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_DELEGATE = TOOLS / "pm_delegate.py"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "budget", "GIT_AUTHOR_EMAIL": "budget@test.invalid",
    "GIT_COMMITTER_NAME": "budget", "GIT_COMMITTER_EMAIL": "budget@test.invalid",
}

# 픽스처 시각은 실행 시점에서 만든다 — 소스에 날짜 리터럴을 박지 않는다(출하 위생).
_FIXTURE_DAY = datetime.date.today().isoformat()
_FIXTURE_STAMP = f"{_FIXTURE_DAY}T00:00:00+00:00"

# 예산 수열이 정하는 4단계 — 이 파일의 기대값은 전부 이 순서에서 나온다.
_CYCLE = ("architect", "developer", "code-reviewer", "developer")

_FILLED_DESIGN_SECTION = (
    "## 설계\n"
    "- **경계 실측**: 예산 픽스처\n"
    "- **불변식**: 라운드 수열 4\n"
    "- **표면 상한**: 티켓당 라운드 파일 1\n"
    "- **테스트 전략**: 정상 수열·초과·순서 밖\n"
)


def _load_tool(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load_tool("pm_delegate", "pm_delegate_budget")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def _spec_text(ticket: str, cluster: str) -> str:
    return (
        "---\n"
        f"id: {ticket}\n"
        "title: 예산 축\n"
        "status: claimed\n"
        f"created: '{_FIXTURE_DAY}'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        f"claimed_at: '{_FIXTURE_STAMP}'\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: medium\n"
        "design: done\n"
        f"cluster: {cluster}\n"
        "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\n예산 판정.\n\n" + _FILLED_DESIGN_SECTION
    )


def _fixture_board(pd, pm_home: Path):
    board = pd._load_module_from_path(
        pm_home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = pm_home
    board.LOCAL_DIR = pm_home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"
    board._rounds_mutation_sync_paths = lambda _message, _paths: True
    return board


@pytest.fixture
def budget_env(tmp_path, pd, monkeypatch):
    """PM 홈(board 데이터 + 엔진 사본) 과 ignore 된 슬롯 git 한 쌍."""
    pm_home = tmp_path / "pm-home"
    slot = tmp_path / "slot"
    pm_tools = pm_home / ".project_manager" / "tools"
    pm_tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, pm_tools / source.name)
    tickets = pm_home / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (pm_home / ".project_manager" / ".local").mkdir(parents=True)
    slot.mkdir()
    assert _git(slot, "init", "-q").returncode == 0
    ignore = slot / ".project_manager" / ".gitignore"
    ignore.parent.mkdir()
    ignore.write_text(".local/\n", encoding="utf-8", newline="\n")
    (slot / "tracked.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(slot, "add", "tracked.txt", ".project_manager/.gitignore").returncode == 0
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, pm_home),
    )
    return pm_home, slot, tickets


def _write_cluster(
    pm_home: Path, cluster: str, tickets: list[str], *,
    budget: dict | None = None, replans: list | None = None,
) -> Path:
    directory = pm_home / ".project_manager" / "wiki" / "tickets" / "clusters"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "architect": 1, "developer_per_ticket": 1, "code-reviewer": 1, "fix": 1,
    }
    if budget is not None:
        payload = budget
    path = directory / f"{cluster}.md"
    path.write_text(
        "---\n"
        f"id: {cluster}\n"
        "tickets:\n" + "".join(f"- {item}\n" for item in tickets)
        + "base_branch: task/main\n"
        f"branch: task/{cluster[2:]}\n"
        "spike: null\n"
        "budget:\n"
        + "".join(f"  {key}: {value}\n" for key, value in payload.items())
        + "replans:\n"
        + ("".join(
            f"- ts: '{item['ts']}'\n  reason: {item['reason']}\n"
            f"  from_ordinal: {item['from_ordinal']}\n"
            for item in replans) if replans else " []\n")
        + "status: open\n"
        "---\n",
        encoding="utf-8", newline="\n")
    return path


def _seed(pm_home: Path, tickets_dir: Path, cluster: str, members: list[str], **kwargs):
    for ticket in members:
        (tickets_dir / f"{ticket}-budget.md").write_text(
            _spec_text(ticket, cluster), encoding="utf-8", newline="\n")
    return _write_cluster(pm_home, cluster, members, **kwargs)


def _write_round_output(pd, path: Path, role: str) -> None:
    """그 역할의 산출을 슬롯 라운드 파일에 쓴다(회수 검증을 실제로 통과하는 bytes).

    리뷰 라운드는 시드 골격의 자리표시자 블록을 **갈아 끼운다** — 덧붙이면 블록이 둘이 되어
    회수가 거부한다(그 거부 자체가 엔진 계약이다).
    """
    header = path.read_text(encoding="utf-8").partition("\n")[0]
    if role != "code-reviewer":
        path.write_text(
            path.read_text(encoding="utf-8") + f"\n## 산출\n- {role} 실측\n",
            encoding="utf-8", newline="")
        return
    payload = json.dumps(
        {"version": pd.PM_REVIEW_VERSION, "findings": [], "confirmations": []},
        ensure_ascii=False, separators=(",", ":"),
    )
    path.write_text(
        f"{header}\n\n## must-fix\n- 없음\n\n## 판정\n판정: 통과 · finding 0건\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n{payload}\n```\n",
        encoding="utf-8", newline="")


def _advance(pd, cluster: str, role: str, *, pm_home: Path, slot: Path):
    """한 단계를 실제로 예약하고 산출을 채워 회수한다(다음 단계의 입력이 된다)."""
    plan = pd.prepare_cluster_copy(
        cluster=cluster, role=role, cwd=slot, pm_home=pm_home,    )
    for round_plan in plan.rounds:
        _write_round_output(pd, round_plan.path, role)
    outcomes = pd.harvest_cluster_copy(run_dir=plan.run_dir, cwd=slot, pm_home=pm_home)
    assert all(item.refusal is None for item in outcomes), outcomes
    if role == "code-reviewer":
        for round_plan in plan.rounds:
            _record_finding_zero(pd, pm_home, round_plan.ticket, round_plan.ordinal)
    return plan


def _record_finding_zero(pd, pm_home: Path, ticket: str, ordinal: int) -> None:
    """PM 판정 — finding 0 리뷰 라운드의 수용 선언(다음 fix 라운드 시드의 선행 조건)."""
    payload = json.dumps({
        "version": pd.PM_REVIEW_DISPOSITION_VERSION,
        "reviewer_role": "code-reviewer",
        "reviewer_ordinal": ordinal,
        "finding_zero": "accepted",
    }, ensure_ascii=False, separators=(",", ":"))
    spec = pm_home / ".project_manager" / "wiki" / "tickets" / "claimed" / f"{ticket}-budget.md"
    spec.write_text(
        spec.read_text(encoding="utf-8")
        + f"\n```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n{payload}\n```\n",
        encoding="utf-8", newline="")


def _rounds_dir(pm_home: Path, ticket: str) -> Path:
    return pm_home / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket


def _open_runs(pd, slot: Path, cluster: str) -> list[str]:
    """아직 열려 있는 run-dir 이름 — 회수로 닫힌 run 은 여기 없다."""
    root = slot / pd.TICKET_COPY_REL_ROOT / cluster
    return sorted(item.name for item in root.iterdir()) if root.is_dir() else []


def _round_names(pm_home: Path, ticket: str) -> list[str]:
    directory = _rounds_dir(pm_home, ticket)
    return sorted(item.name for item in directory.glob("*.md")) if directory.is_dir() else []


# ════════════════════════════════════════════════════════════════════════
# 수열 — 장부 값이 순서를 말한다
# ════════════════════════════════════════════════════════════════════════

def test_budget_values_expand_into_the_role_sequence(pd):
    board = _load_tool("board", "board_budget_sequence")
    sequence = pd.cluster_round_sequence(
        board.CLUSTER_BUDGET_DEFAULT, cluster="C-cycle",
    )
    assert sequence == _CYCLE
    # 값이 곧 길이다 — 구현 라운드를 2 로 선언하면 그 단계가 두 번이다.
    assert pd.cluster_round_sequence(
        {**board.CLUSTER_BUDGET_DEFAULT, "developer_per_ticket": 2},
        cluster="C-cycle",
    ) == ("architect", "developer", "developer", "code-reviewer", "developer")
    # 음수는 "그 단계를 건너뛴다"는 선언이라 0 으로 접는다.
    assert pd.cluster_round_sequence(
        {**board.CLUSTER_BUDGET_DEFAULT, "architect": -3}, cluster="C-cycle",
    ) == ("developer", "code-reviewer", "developer")


@pytest.mark.parametrize("budget, missing", [
    (None, "(없음)"),
    ({"architect": "많이", "developer_per_ticket": 1,
      "code-reviewer": 1, "fix": 1}, "architect"),
    ({"architect": 1, "developer_per_ticket": 1, "code-reviewer": 1}, "fix"),
])
def test_an_undeclared_budget_value_stops_the_judgment(pd, budget, missing):
    """선언되지 않은 값은 기본값으로 지어내지 않는다 — 그 장부는 판정 입력이 아니다."""
    with pytest.raises(pd.DelegateError) as caught:
        pd.cluster_round_sequence(budget, cluster="C-broken")

    message = str(caught.value)
    assert "예산이 선언되지 않았습니다" in message and missing in message
    assert "cluster show C-broken" in message


# ════════════════════════════════════════════════════════════════════════
# 예산 초과 · 순서 밖
# ════════════════════════════════════════════════════════════════════════

def test_the_declared_cycle_runs_and_the_next_request_is_refused(pd, budget_env):
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-cycle", ["T-7001", "T-7002"])

    for role in _CYCLE:
        _advance(pd, "C-cycle", role, pm_home=pm_home, slot=slot)

    assert _round_names(pm_home, "T-7001") == [
        "01-architect.md", "02-developer.md", "03-code-reviewer.md", "04-developer.md",
    ]

    with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
        pd.prepare_cluster_copy(
            cluster="C-cycle", role="developer", cwd=slot, pm_home=pm_home,
        )

    message = str(caught.value)
    assert "예산 소진" in message and "T-7001" in message
    assert "cluster replan C-cycle --reason" in message
    # 거부는 예약 앞이다 — 라운드 파일도 run-dir 도 늘지 않는다(회수로 닫힌 run 만 있다).
    assert len(_round_names(pm_home, "T-7001")) == 4
    assert _open_runs(pd, slot, "C-cycle") == []


def test_review_without_the_implementation_round_is_refused(pd, budget_env):
    """순번이 곧 단계다 — 구현(02) 없이 리뷰(03)를 요청할 수 없다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-order", ["T-7010"])
    _advance(pd, "C-order", "architect", pm_home=pm_home, slot=slot)

    with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
        pd.prepare_cluster_copy(
            cluster="C-order", role="code-reviewer", cwd=slot, pm_home=pm_home,
        )

    message = str(caught.value)
    assert "순서 밖 역할" in message and "다음 라운드는 developer" in message
    assert "cluster replan C-order --reason" in message
    assert _round_names(pm_home, "T-7010") == ["01-architect.md"]


def test_implementation_after_the_fix_round_is_refused(pd, budget_env):
    """fix(04) 뒤 developer 재요청은 다음 주기가 아니라 예산 소진이다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-tail", ["T-7020"])
    for role in _CYCLE:
        _advance(pd, "C-tail", role, pm_home=pm_home, slot=slot)

    with pytest.raises(pd.ClusterRoundBudgetExceeded):
        pd.prepare_cluster_copy(
            cluster="C-tail", role="developer", cwd=slot, pm_home=pm_home,
        )


def test_the_ticket_surface_gets_the_same_budget_verdict(pd, budget_env):
    """표면이 판정을 정하지 않는다 — 티켓 단축 표기 준비도 같은 예산·같은 처방을 받는다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-surface", ["T-7030"])
    for role in _CYCLE:
        _advance(pd, "C-surface", role, pm_home=pm_home, slot=slot)

    with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
        pd.prepare_ticket_copy(
            ticket="T-7030", role="developer", cwd=slot, pm_home=pm_home,
        )

    message = str(caught.value)
    assert "예산 소진" in message and "T-7030" in message
    assert "cluster replan C-surface --reason" in message
    assert len(_round_names(pm_home, "T-7030")) == 4


def test_the_ticket_surface_refuses_an_out_of_order_role_too(pd, budget_env):
    """순서 판정도 표면과 무관하다 — 설계 없이 구현을 티켓 표면으로 열 수 없다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-surfaceorder", ["T-7031"])

    with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
        pd.prepare_ticket_copy(
            ticket="T-7031", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert "다음 라운드는 architect" in str(caught.value)
    assert _round_names(pm_home, "T-7031") == []


def test_cli_prepare_refuses_over_budget_with_the_replan_prescription(
        pd, budget_env, monkeypatch, capsys):
    """CLI rc 는 1 이고 처방은 재설계 커맨드 실값이다(우회 플래그 안내 0)."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-cli", ["T-7040"])
    for role in _CYCLE:
        _advance(pd, "C-cli", role, pm_home=pm_home, slot=slot)
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _repo: pm_home)
    monkeypatch.setattr(pd, "_repo_root_for_cwd", lambda cwd: Path(cwd))
    monkeypatch.setattr(pd, "local_config", lambda *_a, **_k: {})
    monkeypatch.setattr(pd, "_reject_cross_role_prepare", lambda *_a, **_k: None)
    capsys.readouterr()

    rc = pd.main(["ticket", "prepare", "--cluster", "C-cli", "--role", "developer",
                  "--cwd", str(slot)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "예산 소진" in err
    assert "cluster replan C-cli --reason" in err
    assert "--force" not in err and "우회" not in err.replace("우회 없음", "")


# ════════════════════════════════════════════════════════════════════════
# 두 판정 지점 — 동시 준비가 예산을 함께 넘지 못한다
# ════════════════════════════════════════════════════════════════════════

def test_the_lock_recheck_refuses_a_racing_prepare(pd, budget_env, monkeypatch):
    """사전판정 통과 뒤 다른 준비가 마지막 자리를 가져가면 락 안 재확인이 거부한다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-race", ["T-7050"])
    for role in _CYCLE[:3]:
        _advance(pd, "C-race", role, pm_home=pm_home, slot=slot)

    board = _fixture_board(pd, pm_home)
    rounds_module = pd._load_ticket_rounds()
    real_lock = board.board_lock
    entries: list[int] = []

    def _racing_lock(*args, **kwargs):
        entries.append(len(entries) + 1)
        if len(entries) == 2:
            # 사전판정과 이 임계구역 사이에 다른 준비가 마지막 fix 자리를 가져갔다.
            rounds_module.reserve_round(
                board.tickets_dir(), "T-7050", "developer", content="## 경쟁 예약\n",
                lock=contextlib.nullcontext(),
            )
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(board, "board_lock", _racing_lock)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)

    with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
        pd.prepare_cluster_copy(
            cluster="C-race", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert "예산 소진" in str(caught.value)
    # 경쟁 예약분(04) 하나만 남고 이 호출의 예약은 없다.
    assert _round_names(pm_home, "T-7050") == [
        "01-architect.md", "02-developer.md", "03-code-reviewer.md", "04-developer.md",
    ]


_ALL_ZERO_BUDGET = {
    "architect": 0, "developer_per_ticket": 0, "code-reviewer": 0, "fix": 0,
}


def test_an_all_zero_budget_declares_an_empty_sequence(pd):
    """모든 값이 0 이면 수열은 빈 tuple 이다 — '선언 없음' 이 아니라 '허용 라운드 0' 이다."""
    board = _load_tool("board", "board_budget_zero")
    assert pd.cluster_round_sequence(_ALL_ZERO_BUDGET, cluster="C-zero") == ()


def test_a_zero_budget_ledger_refuses_every_role(pd, budget_env):
    """0 을 선언한 장부는 무제한이 아니라 전량 거부다(우회 0) — 그리고 그 판정은 되돌릴 수 있다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-zero", ["T-7100"], budget=_ALL_ZERO_BUDGET)

    for role in _CYCLE:
        with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
            pd.prepare_cluster_copy(
                cluster="C-zero", role=role, cwd=slot, pm_home=pm_home,
            )
        message = str(caught.value)
        assert "예산 소진" in message and "예산 0건" in message
        assert "cluster replan C-zero --reason" in message
    # 거부는 예약 앞이다 — 라운드도 run-dir 도 만들어지지 않았다.
    assert _round_names(pm_home, "T-7100") == []
    assert _open_runs(pd, slot, "C-zero") == []

    # 역방향: 양수 예산을 선언한 장부에서는 같은 요청이 그대로 통과한다.
    _write_cluster(pm_home, "C-zero", ["T-7100"])
    plan = pd.prepare_cluster_copy(
        cluster="C-zero", role="architect", cwd=slot, pm_home=pm_home,
    )
    assert plan.rounds[0].board_path.name == "01-architect.md"


def _refuse_at_the_lock(pd, pm_home: Path, monkeypatch, mutate) -> None:
    """두 판정 지점 사이(사전판정 통과 뒤·락 안 재확인 전)에 장부를 바꾼다."""
    board = _fixture_board(pd, pm_home)
    real_lock = board.board_lock
    entries: list[int] = []

    def _racing_lock(*args, **kwargs):
        entries.append(len(entries) + 1)
        if len(entries) == 2:
            mutate()
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(board, "board_lock", _racing_lock)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)


def test_the_lock_recheck_sees_a_budget_that_shrank_to_zero(
        pd, budget_env, monkeypatch):
    """사전판정 뒤 수열이 빈 수열로 바뀌면 락 안 재확인이 거부한다(재확인은 항상 재판독)."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-shrink", ["T-7110"])
    _refuse_at_the_lock(
        pd, pm_home, monkeypatch,
        lambda: _write_cluster(
            pm_home, "C-shrink", ["T-7110"], budget=_ALL_ZERO_BUDGET),
    )

    with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
        pd.prepare_cluster_copy(
            cluster="C-shrink", role="architect", cwd=slot, pm_home=pm_home,
        )

    assert "예산 0건" in str(caught.value)
    assert _round_names(pm_home, "T-7110") == []


def test_a_ledger_that_disappears_mid_prepare_refuses(pd, budget_env, monkeypatch):
    """장부가 사라진 요청은 열리지 않는다(삭제가 우회 수단이 아니다)."""
    pm_home, slot, tickets = budget_env
    ledger = _seed(pm_home, tickets, "C-vanish", ["T-7120"])
    _refuse_at_the_lock(pd, pm_home, monkeypatch, ledger.unlink)

    with pytest.raises(pd.DelegateError, match="묶음 장부가 없습니다"):
        pd.prepare_cluster_copy(
            cluster="C-vanish", role="architect", cwd=slot, pm_home=pm_home,
        )

    assert _round_names(pm_home, "T-7120") == []


def test_a_ticket_without_a_ledger_stops_the_preparation(pd, budget_env):
    """장부 없는 크기 1 해석(구세대 티켓)도 무제한이 아니라 정지다 — 판정 입력이 없다."""
    pm_home, slot, tickets = budget_env
    (tickets / "T-7130-budget.md").write_text(
        _spec_text("T-7130", ""), encoding="utf-8", newline="\n")

    with pytest.raises(pd.DelegateError) as caught:
        pd.prepare_cluster_copy(
            cluster="C-T-7130", role="architect", cwd=slot, pm_home=pm_home,
        )

    assert "묶음 장부가 없습니다" in str(caught.value)
    assert _round_names(pm_home, "T-7130") == []


# ════════════════════════════════════════════════════════════════════════
# 재설계 — 리셋과 기준선
# ════════════════════════════════════════════════════════════════════════

def test_replan_baseline_reopens_the_cycle_from_design(pd, budget_env):
    """기준선 뒤 라운드만 이번 주기다 — 리셋 직후 다음 준비는 architect 다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-reset", ["T-7060"])
    for role in _CYCLE:
        _advance(pd, "C-reset", role, pm_home=pm_home, slot=slot)
    # 재설계 기록: 그 시점 최대 순번 4 를 기준선으로 박제한다.
    _write_cluster(pm_home, "C-reset", ["T-7060"], replans=[
        {"ts": _FIXTURE_STAMP, "reason": "설계 축 지적", "from_ordinal": 4},
    ])

    plan = pd.prepare_cluster_copy(
        cluster="C-reset", role="architect", cwd=slot, pm_home=pm_home,
    )

    assert plan.rounds[0].board_path.name == "05-architect.md"
    # 리셋은 주기만 다시 연다 — 옛 라운드 파일은 그대로 남는다(산출 보존).
    assert len(_round_names(pm_home, "T-7060")) == 5


def test_replan_does_not_reopen_a_role_out_of_order(pd, budget_env):
    """리셋 뒤에도 순서는 그대로다 — 설계부터가 아니면 여전히 거부다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-resetorder", ["T-7070"], replans=[
        {"ts": _FIXTURE_STAMP, "reason": "재설계", "from_ordinal": 0},
    ])

    with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
        pd.prepare_cluster_copy(
            cluster="C-resetorder", role="code-reviewer", cwd=slot, pm_home=pm_home,
        )
    assert "다음 라운드는 architect" in str(caught.value)


# ════════════════════════════════════════════════════════════════════════
# 역방향 — 크기 1 동형 · 장부 없는 옛 경로 무영향
# ════════════════════════════════════════════════════════════════════════

def test_size_one_cluster_takes_the_same_path(pd, budget_env):
    """크기 1 도 특례가 아니다 — 같은 수열·같은 거부·같은 처방이다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-one", ["T-7080"])
    for role in _CYCLE:
        plan = _advance(pd, "C-one", role, pm_home=pm_home, slot=slot)
        assert len(plan.rounds) == 1

    with pytest.raises(pd.ClusterRoundBudgetExceeded) as caught:
        pd.prepare_cluster_copy(
            cluster="C-one", role="developer", cwd=slot, pm_home=pm_home,
        )
    assert "cluster replan C-one --reason" in str(caught.value)


def test_the_ticket_surface_without_a_ledger_stops_too(pd, budget_env):
    """티켓 표면도 장부 없이는 열리지 않는다 — 필드 없는 티켓이 우회로가 아니다."""
    pm_home, slot, tickets = budget_env
    (tickets / "T-7090-legacy.md").write_text(
        _spec_text("T-7090", "C-T-7090").replace("cluster: C-T-7090\n", ""),
        encoding="utf-8", newline="\n")

    with pytest.raises(pd.DelegateError) as caught:
        pd.prepare_ticket_copy(
            ticket="T-7090", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert "묶음 장부가 없습니다" in str(caught.value)
    assert _round_names(pm_home, "T-7090") == []


# ════════════════════════════════════════════════════════════════════════
# board `cluster replan` — 장부 쓰기
# ════════════════════════════════════════════════════════════════════════

_TEMPLATE_TEXT = (
    "---\nid: T-NNNN\ntitle: <제목>\nstatus: open\ncreated_by:\nclaimed_by:\n"
    "claimed_at:\ncompleted_at:\ndepends_on: []\nblocks: []\ntouches: []\n"
    "estimate: small\ndesign: n/a\ncluster:\ntags: []\n---\n\n"
    "# T-NNNN — <제목>\n\n## 목표\n실제 목표를 채웠다.\n\n"
    "## 인터페이스\n실제 인터페이스 규격.\n\n## 결정\n실제 구현 방향.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 실제 산출물\n\n"
    "## 참고\n- 실제 참고 사항\n\n## 메모\n"
)


@pytest.fixture
def board_env(tmp_path, monkeypatch):
    """실 board git + 코드 git — `cluster replan` 이 쓰는 두 저장소."""
    board = _load_tool("board", "board_replan")
    anchor_board_module(board, tmp_path, monkeypatch)
    board.BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.BOARD_FILE.touch()
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    bare = tmp_path / "bare"
    assert _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(bare)).returncode == 0
    board_dir = tmp_path / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board_dir / "tickets" / "_template.md").write_text(
        _TEMPLATE_TEXT, encoding="utf-8")
    assert _git(board_dir, "init", "-q", "-b", "main").returncode == 0
    assert _git(board_dir, "remote", "add", "origin", str(bare)).returncode == 0
    assert _git(board_dir, "add", "-A").returncode == 0
    assert _git(board_dir, "commit", "-qm", "board init").returncode == 0
    assert _git(board_dir, "push", "-q", "-u", "origin", "main").returncode == 0
    assert _git(tmp_path, "init", "-q", "-b", "task/main").returncode == 0
    (tmp_path / "code.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(tmp_path, "add", "--", "code.txt").returncode == 0
    assert _git(tmp_path, "commit", "-qm", "code seed").returncode == 0
    return board, board_dir


def _cluster_args(action: str, name: str, **kwargs) -> argparse.Namespace:
    values = dict(cluster_cmd=action, name=name, tickets=None, spike=None,
                  reason=None, repo=None, slot=None, task=None)
    values.update(kwargs)
    return argparse.Namespace(**values)


def _issue(board, title: str) -> str:
    directory = board.tickets_dir()
    before = {path.name for path in directory.glob("*/T-*.md")}
    assert board.cmd_new(argparse.Namespace(
        title=title, touches=None, depends=None, tag=None, estimate="small",
        prefix=None, user=None, session=None, repo=None, slot=None, task=None,
    )) == 0
    created = [
        path for path in directory.glob("*/T-*.md") if path.name not in before
    ]
    assert len(created) == 1, created
    return board._canonical_ticket_id(created[0])


@requires_git
def test_cluster_replan_records_the_baseline_and_resets_the_budget(board_env, capsys):
    board, _board_dir = board_env
    ticket = _issue(board, "재설계 대상")
    assert board.cmd_cluster(_cluster_args("new", "wave", tickets=ticket)) == 0
    rounds_module = board._load_ticket_rounds()
    for role in _CYCLE:
        rounds_module.reserve_round(
            board.tickets_dir(), ticket, role, content=f"## {role}\n- 산출\n",
            lock=contextlib.nullcontext())
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args("replan", "wave", reason="설계 축 지적 수용"))

    assert rc == 0
    ledger = board.load_cluster("C-wave")
    assert len(ledger["replans"]) == 1
    record = ledger["replans"][0]
    assert set(record) == set(board.CLUSTER_REPLAN_KEYS)
    assert record["reason"] == "설계 축 지적 수용"
    assert record[board.CLUSTER_REPLAN_BASELINE_KEY] == 4
    assert ledger["budget"] == board.CLUSTER_BUDGET_DEFAULT
    assert ledger["status"] == board.CLUSTER_STATUS_OPEN
    assert board.cluster_replan_baseline(ledger) == 4
    out = capsys.readouterr().out
    assert "기준선 순번 4" in out and "architect 1 라운드" in out
    # 재설계는 라운드 파일을 만들지도 지우지도 않는다(예약 표면의 몫).
    assert len(list((board.tickets_dir() / "rounds" / ticket).glob("*.md"))) == 4


@requires_git
def test_cluster_replan_requires_a_reason_and_an_existing_ledger(board_env, capsys):
    board, _board_dir = board_env
    ticket = _issue(board, "사유 필수")
    assert board.cmd_cluster(_cluster_args("new", "needreason", tickets=ticket)) == 0
    capsys.readouterr()

    assert board.cmd_cluster(_cluster_args("replan", "needreason", reason="  ")) == 1
    assert "--reason" in capsys.readouterr().err
    assert board.cmd_cluster(_cluster_args("replan", "absent", reason="사유")) == 2
    assert "cluster not found" in capsys.readouterr().err
    assert board.load_cluster("C-needreason")["replans"] == []


@requires_git
def test_replan_ordinal_is_read_from_the_ledger_for_the_ticket(board_env):
    """설계 축 잔여 종결 판정이 읽는 값과 예산 판정이 읽는 값이 같은 하나다."""
    board, _board_dir = board_env
    ticket = _issue(board, "기준선 판독")
    assert board.cmd_cluster(_cluster_args("new", "shared", tickets=ticket)) == 0
    rounds_module = board._load_ticket_rounds()
    for role in _CYCLE[:3]:
        rounds_module.reserve_round(
            board.tickets_dir(), ticket, role, content=f"## {role}\n- 산출\n",
            lock=contextlib.nullcontext())
    assert board.cmd_cluster(_cluster_args("replan", "shared", reason="축 교체")) == 0

    _status, path = board.find_ticket_exact(ticket)
    spec_text = path.read_text(encoding="utf-8")
    assert board.ticket_design_replan_ordinal(ticket, spec_text) == 3
    # 장부가 없는 티켓은 0 이다(재설계 없음 = 종전 판정).
    assert board.ticket_design_replan_ordinal("T-0001", "---\nid: T-0001\n---\n") == 0


def test_replan_ledger_is_json_serialisable_for_the_board_writer(board_env):
    """장부 행은 frontmatter 로 그대로 왕복한다(YAML 손편집 없이 기계 판독)."""
    board, _board_dir = board_env
    fm = board._new_cluster_fm("C-json", ["T-0001"])
    fm["replans"] = [{
        "ts": _FIXTURE_STAMP, "reason": "왕복",
        board.CLUSTER_REPLAN_BASELINE_KEY: 2,
    }]
    path = board.dump_cluster(fm)
    reloaded = board.load_cluster("C-json")
    assert reloaded["replans"] == fm["replans"]
    assert json.loads(json.dumps(reloaded["replans"])) == fm["replans"]
    assert path.is_file()
