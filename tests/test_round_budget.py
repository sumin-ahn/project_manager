"""묶음 고정 라운드 예산 — 역할 수열·순서 밖 거부·종료 수렴.

여기서 지키는 성질은 다섯이다.
  (1) 묶음의 라운드 수열은 PM → 설계 → 구현 → 리뷰 → fix에서 사람 라운드 4개로 고정된다.
  (2) 예산을 넘긴 요청도, 순서 밖 역할 요청도 **예약 전에** 거부되고 추가 라운드는 없다.
  (3) 판정은 사전판정과 board_lock 재확인 **두 지점**에 걸려 동시 준비가 예산을 함께 넘지 못한다.
  (4) 예산 값은 네 키 모두 정확히 1이며 0·증액·replan으로 수열을 바꿀 수 없다.
  (5) 크기 1 묶음도, 티켓 단축 표기 준비도 같은 판정을 받는다.

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

from _test_exec import python_argv_command
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
    (slot / ".project_manager" / "local.conf").write_text(
        f"test.cmd={python_argv_command('--version')}\n", encoding="utf-8", newline="\n",
    )
    (slot / "tracked.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(slot, "add", "tracked.txt", ".project_manager/.gitignore").returncode == 0
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    real_load_board = pd._load_board_for_repo

    def _load_board(repo):
        resolved = Path(repo).resolve()
        return (
            _fixture_board(pd, pm_home)
            if resolved == pm_home.resolve()
            else real_load_board(resolved)
        )

    monkeypatch.setattr(pd, "_load_board_for_repo", _load_board)
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


def _write_round_output(
    pd, path: Path, role: str, *, regression_result: str = "rc=0 · fixture green",
) -> None:
    """그 역할의 산출을 슬롯 라운드 파일에 쓴다(회수 검증을 실제로 통과하는 bytes).

    리뷰 라운드는 시드 골격의 자리표시자 블록을 **갈아 끼운다** — 덧붙이면 블록이 둘이 되어
    회수가 거부한다(그 거부 자체가 엔진 계약이다).
    """
    header = path.read_text(encoding="utf-8").partition("\n")[0]
    if role == "architect":
        path.write_text(
            f"{header}\n\n" + _architect_output(pd),
            encoding="utf-8", newline="",
        )
        return
    if role != "code-reviewer":
        text = path.read_text(encoding="utf-8") + f"\n## 산출\n- {role} 실측\n"
        if role == "developer":
            slot = next(parent for parent in path.parents if (parent / ".git").exists())
            command = pd._full_regression_command(slot)
            text = text.replace(
                "- 커맨드: `<실행 커맨드>`",
                f"- 커맨드: `{command}`",
            ).replace(
                "- 결과: <rc=0 · A passed / 0 failed>",
                f"- 결과: {regression_result}",
            )
        path.write_text(text, encoding="utf-8", newline="")
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
    if role == "developer":
        target = slot / "tests" / "test_round_budget.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            target.read_text(encoding="utf-8") + "# developer contract target\n"
            if target.exists() else "# developer contract target\n",
            encoding="utf-8", newline="\n",
        )
    outcomes = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )
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


def _record_accepted_finding(
    pd, pm_home: Path, ticket: str, ordinal: int, finding_id: str,
) -> None:
    payload = json.dumps({
        "version": pd.PM_REVIEW_DISPOSITION_VERSION,
        "reviewer_role": "code-reviewer", "reviewer_ordinal": ordinal,
        "dispositions": [{
            "id": finding_id, "decision": "accepted", "reason": "fix에서 해소",
            "scope": f"{finding_id} 계약 범위", "prerequisite": "",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    spec = pm_home / ".project_manager" / "wiki" / "tickets" / "claimed" / f"{ticket}-budget.md"
    spec.write_text(
        spec.read_text(encoding="utf-8")
        + f"\n```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n{payload}\n```\n",
        encoding="utf-8", newline="",
    )


def _rounds_dir(pm_home: Path, ticket: str) -> Path:
    return pm_home / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket


def _open_runs(pd, slot: Path, cluster: str) -> list[str]:
    """아직 열려 있는 run-dir 이름 — 회수로 닫힌 run 은 여기 없다."""
    root = slot / pd.TICKET_COPY_REL_ROOT / cluster
    return sorted(item.name for item in root.iterdir()) if root.is_dir() else []


def _round_names(pm_home: Path, ticket: str) -> list[str]:
    directory = _rounds_dir(pm_home, ticket)
    return sorted(item.name for item in directory.glob("*.md")) if directory.is_dir() else []


def _architect_output(
    pd, *, command: str | None = None, expected: str = "Python",
) -> str:
    command = command or python_argv_command("--version")
    payload = json.dumps({
        "version": pd.ARCHITECT_TEST_VERSION,
        "tests": [{
            "id": "AT-001", "target": "tests/test_round_budget.py",
            "command": command, "expected": expected,
            "negative": "계약 누락 또는 red면 developer를 종료하지 않는다",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    return (
        "## 경계 실측\n- 고정 수열\n\n## 불변식\n- 단계당 1회\n\n"
        "## 표면 상한\n- 추가 라운드 없음\n\n## 테스트 전략\n- 정상·실패\n\n"
        f"```{pd.ARCHITECT_TEST_BLOCK}\n{payload}\n```\n\n검토 판정: 설계 통과\n"
    )


# ════════════════════════════════════════════════════════════════════════
# 수열 — 장부 값이 순서를 말한다
# ════════════════════════════════════════════════════════════════════════

def _prepare_accepted_review(
    pd, *, pm_home: Path, slot: Path, cluster: str, ticket: str,
    target: str = "tests/test_terminal_regression.py",
    command: str | None = None,
):
    command = command or python_argv_command(
        "-m", "pytest", "tests/test_terminal_regression.py", "-q",
    )
    review = pd.prepare_cluster_copy(
        cluster=cluster, role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    finding = {
        "id": "F-001", "class": "implementation-defect", "severity": "must-fix",
        "authority": f"[[{ticket}]] §완료 조건", "evidence": "terminal probe red",
        "recommendation": "현재 fix에서 계약대로 수정",
        "fix_contract": {
            "location": "src/example.py:1", "failure": "terminal probe red",
            "design": "현재 fix 경계 안에서 결함을 제거",
            "test": f"{target} 회귀를 추가",
            "command": command, "expected": "passed",
        },
        "design_change": False,
    }
    payload = json.dumps({
        "version": pd.PM_REVIEW_VERSION, "findings": [finding], "confirmations": [],
    }, ensure_ascii=False, separators=(",", ":"))
    header = review.rounds[0].path.read_text(encoding="utf-8").partition("\n")[0]
    review.rounds[0].path.write_text(
        f"{header}\n\n## must-fix\n- F-001\n\n## 판정\n판정: 반려\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n{payload}\n```\n",
        encoding="utf-8", newline="",
    )
    outcomes = pd.harvest_cluster_copy(
        run_dir=review.run_dir, cwd=slot, pm_home=pm_home,
    )
    assert outcomes[0].refusal is None
    _record_accepted_finding(pd, pm_home, ticket, review.rounds[0].ordinal, "F-001")
    return review


def test_budget_values_expand_into_the_role_sequence(pd):
    board = _load_tool("board", "board_budget_sequence")
    sequence = pd.cluster_round_sequence(
        board.CLUSTER_BUDGET_DEFAULT, cluster="C-cycle",
    )
    assert sequence == _CYCLE
    # 값을 늘이거나 줄여 가변 루프/단계 생략으로 바꾸는 장부는 손상이다.
    for key, value in (("developer_per_ticket", 2), ("architect", 0), ("fix", -1)):
        with pytest.raises(pd.DelegateError, match=rf"{key}=1"):
            pd.cluster_round_sequence(
                {**board.CLUSTER_BUDGET_DEFAULT, key: value}, cluster="C-cycle",
            )


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


def test_developer_prepare_requires_the_architect_test_contract(pd, budget_env):
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-contract", ["T-7000"])
    board = _fixture_board(pd, pm_home)
    pd._load_ticket_rounds().reserve_round(
        board.tickets_dir(), "T-7000", "architect",
        content="## 설계\n- 테스트 계약 누락\n", lock=contextlib.nullcontext(),
    )

    with pytest.raises(pd.DelegateError, match="pm-architect-tests-v1"):
        pd.prepare_cluster_copy(
            cluster="C-contract", role="developer", cwd=slot, pm_home=pm_home,
        )
    assert _round_names(pm_home, "T-7000") == ["01-architect.md"]
    assert _open_runs(pd, slot, "C-contract") == []


@pytest.mark.parametrize("field", (
    "id", "target", "command", "expected", "negative",
))
def test_architect_contract_rejects_placeholder_in_every_string_field(pd, field):
    row = {
        "id": "AT-001", "target": "tests/test_round_budget.py",
        "command": "python3 --version", "expected": "Python",
        "negative": "계약 누락을 거부",
    }
    row[field] = "prefix <placeholder>"
    payload = json.dumps({
        "version": pd.ARCHITECT_TEST_VERSION, "tests": [row],
    }, ensure_ascii=False, separators=(",", ":"))

    with pytest.raises(pd.DelegateError, match="placeholder"):
        pd.parse_architect_tests(
            f"```{pd.ARCHITECT_TEST_BLOCK}\n{payload}\n```\n"
        )


def test_initial_developer_harvest_requires_the_architect_target_in_its_diff(
    pd, budget_env,
):
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-arch-target", ["T-7006"])
    board = _fixture_board(pd, pm_home)
    pd._load_ticket_rounds().reserve_round(
        board.tickets_dir(), "T-7006", "architect",
        content=_architect_output(pd), lock=contextlib.nullcontext(),
    )
    plan = pd.prepare_cluster_copy(
        cluster="C-arch-target", role="developer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(pd, plan.rounds[0].path, "developer")

    outcome = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )[0]
    assert outcome.refusal is not None
    assert "architect 필수 테스트 AT-001 대상" in outcome.refusal
    assert "tests/test_round_budget.py" in outcome.refusal


def test_developer_harvest_requires_an_exact_green_full_regression_record(
    pd, budget_env,
):
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-stage-record", ["T-7009"])
    board = _fixture_board(pd, pm_home)
    pd._load_ticket_rounds().reserve_round(
        board.tickets_dir(), "T-7009", "architect",
        content=_architect_output(pd), lock=contextlib.nullcontext(),
    )
    plan = pd.prepare_cluster_copy(
        cluster="C-stage-record", role="developer", cwd=slot, pm_home=pm_home,
    )
    target = slot / "tests" / "test_round_budget.py"
    target.parent.mkdir(parents=True)
    target.write_text("# architect contract target\n", encoding="utf-8", newline="\n")

    # 시드 placeholder는 developer가 전체 회귀를 직접 실행·기록한 증거가 아니다.
    plan.rounds[0].path.write_text(
        plan.rounds[0].path.read_text(encoding="utf-8") + "\n## 산출\n- 구현 완료\n",
        encoding="utf-8", newline="",
    )
    outcome = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )[0]
    assert outcome.refusal is not None and "placeholder가 아닌 실값" in outcome.refusal
    assert plan.rounds[0].path.exists()


def test_developer_regression_record_rejects_wrong_command_and_red_result(pd):
    expected = "python3 -m pytest tests/ -q -n auto"
    wrong = (
        "## 회귀\n- 커맨드: `python3 -m pytest tests/ -q`\n"
        "- 결과: rc=0 · 10 passed\n"
    )
    assert pd.parse_developer_regression_record(wrong).command != expected
    red = (
        f"## 회귀\n- 커맨드: `{expected}`\n"
        "- 결과: rc=1 · 1 failed\n"
    )
    with pytest.raises(pd.DelegateError, match="green이 아닙니다"):
        pd.parse_developer_regression_record(red)


def test_stage_exit_uses_pm_owned_project_test_cmd_not_candidate_local_serial(
    pd, tmp_path, monkeypatch,
):
    """PM 홈의 -n8 project 명령이 후보 worktree의 낡은 serial local.conf보다 우선한다."""
    pm_home = tmp_path / "pm-home"
    candidate = tmp_path / "candidate"
    (candidate / ".project_manager").mkdir(parents=True)
    (candidate / ".project_manager" / "local.conf").write_text(
        "test.cmd=python3 -m pytest tests/ -q\n", encoding="utf-8", newline="\n",
    )
    pm_home.mkdir()

    class _ER:
        class AnchorResolutionError(RuntimeError):
            pass

        @staticmethod
        def resolve_pm_home_for_repo(_repo):
            return pm_home

    class _Board:
        @staticmethod
        def _test_cmd(_override, *, session):
            assert session is None
            return "python3 -m pytest tests/ -q -n 8"

    monkeypatch.setattr(pd, "_load_additional_reviewer", lambda: _ER)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda root: _Board if root == pm_home else None)
    assert pd._full_regression_command(candidate) == "python3 -m pytest tests/ -q -n 8"


def test_developer_harvest_refuses_when_an_architect_required_test_is_red(
    pd, budget_env,
):
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-arch-red", ["T-7003"])
    board = _fixture_board(pd, pm_home)
    pd._load_ticket_rounds().reserve_round(
        board.tickets_dir(), "T-7003", "architect",
        content=_architect_output(
            pd, command=python_argv_command("-m", "module_that_does_not_exist_t0871"), expected="green",
        ),
        lock=contextlib.nullcontext(),
    )
    plan = pd.prepare_cluster_copy(
        cluster="C-arch-red", role="developer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(pd, plan.rounds[0].path, "developer")
    target = slot / "tests" / "test_round_budget.py"
    target.parent.mkdir(parents=True)
    target.write_text("# architect contract target\n", encoding="utf-8", newline="\n")

    outcomes = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )
    assert outcomes[0].refusal is not None
    assert "architect 필수 테스트 AT-001" in outcomes[0].refusal
    assert _round_names(pm_home, "T-7003") == ["01-architect.md", "02-developer.md"]


def test_fix_harvest_requires_each_reviewer_test_target_in_the_fix_diff(
    pd, budget_env,
):
    pm_home, slot, tickets = budget_env
    ticket = "T-7007"
    cluster = "C-review-target"
    _seed(pm_home, tickets, cluster, [ticket])
    for role in _CYCLE[:2]:
        _advance(pd, cluster, role, pm_home=pm_home, slot=slot)
    _prepare_accepted_review(
        pd, pm_home=pm_home, slot=slot, cluster=cluster, ticket=ticket,
    )
    fix = pd.prepare_cluster_copy(
        cluster=cluster, role="developer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(pd, fix.rounds[0].path, "developer")

    outcome = pd.harvest_cluster_copy(
        run_dir=fix.run_dir, cwd=slot, pm_home=pm_home,
    )[0]
    assert outcome.refusal is not None
    assert "reviewer 추가 회귀 F-001 대상" in outcome.refusal
    assert "tests/test_terminal_regression.py" in outcome.refusal
    assert outcome.terminal is True


def test_fix_harvest_binds_each_korean_particle_test_target_to_the_fix_diff(
    pd, budget_env,
):
    pm_home, slot, tickets = budget_env
    ticket = "T-7090"
    cluster = "C-review-korean-targets"
    _seed(pm_home, tickets, cluster, [ticket])
    for role in _CYCLE[:2]:
        _advance(pd, cluster, role, pm_home=pm_home, slot=slot)
    _prepare_accepted_review(
        pd, pm_home=pm_home, slot=slot, cluster=cluster, ticket=ticket,
        target=(
            "tests/test_pm_review_delta.py에 v3 자리표시자 거부 케이스를, "
            "tests/test_round_budget.py에 diff 결속 회귀를 추가한다"
        ),
    )
    fix = pd.prepare_cluster_copy(
        cluster=cluster, role="developer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(pd, fix.rounds[0].path, "developer")
    first_target = slot / "tests" / "test_pm_review_delta.py"
    first_target.parent.mkdir(parents=True, exist_ok=True)
    first_target.write_text("# first reviewer target\n", encoding="utf-8", newline="\n")

    outcome = pd.harvest_cluster_copy(
        run_dir=fix.run_dir, cwd=slot, pm_home=pm_home,
    )[0]
    assert outcome.refusal is not None
    assert (
        "reviewer 추가 회귀 F-001 대상이 이 fix diff에 추가·수정되지 않았습니다: "
        "tests/test_round_budget.py"
    ) in outcome.refusal
    assert outcome.terminal is True


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
    assert "고정 라운드 종료" in message and "T-7001" in message
    assert "추가 라운드 없이 정지·보고" in message
    # 거부는 예약 앞이다 — 라운드 파일도 run-dir 도 늘지 않는다(회수로 닫힌 run 만 있다).
    assert len(_round_names(pm_home, "T-7001")) == 4
    assert _open_runs(pd, slot, "C-cycle") == []


def _prepare_recorded_developer(pd, budget_env, *, phase: str, suffix: str):
    pm_home, slot, tickets = budget_env
    ticket = f"T-{suffix}"
    cluster = f"C-record-{suffix}"
    _seed(pm_home, tickets, cluster, [ticket])
    advance = 1 if phase == "initial" else 3
    for role in _CYCLE[:advance]:
        _advance(pd, cluster, role, pm_home=pm_home, slot=slot)
    plan = pd.prepare_cluster_copy(
        cluster=cluster, role="developer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(pd, plan.rounds[0].path, "developer")
    if phase == "initial":
        target = slot / "tests" / "test_round_budget.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# architect contract target\n", encoding="utf-8", newline="\n")
    return pm_home, slot, ticket, plan


@pytest.mark.parametrize("phase", ("initial", "fix"))
def test_developer_harvest_accepts_exact_record_without_reexecuting_full(
    pd, budget_env, monkeypatch, phase,
):
    pm_home, slot, _ticket, plan = _prepare_recorded_developer(
        pd, budget_env, phase=phase, suffix="7040" if phase == "initial" else "7050",
    )
    real_run = pd._run_required_test
    calls = []

    def targeted_only(command, expected, *, cwd):
        calls.append((command, expected))
        assert expected is not None, "harvest가 stage-exit full을 중복 실행했다"
        return real_run(command, expected, cwd=cwd)

    monkeypatch.setattr(pd, "_run_required_test", targeted_only)
    outcome = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )[0]

    assert outcome.refusal is None
    assert calls, "architect targeted 계약은 harvest에서 계속 검증해야 한다"
    assert all(expected is not None for _command, expected in calls)


@pytest.mark.parametrize("phase", ("initial", "fix"))
@pytest.mark.parametrize(
    "record_axis,expected_message",
    (("missing", "절 정확히 1개"),
     ("mismatch", "stage-exit 명령과 다릅니다"),
     ("nonzero", "green이 아닙니다")),
)
def test_developer_harvest_rejects_invalid_record_without_running_full(
    pd, budget_env, monkeypatch, phase, record_axis, expected_message,
):
    suffix = f"{7100 + (0 if phase == 'initial' else 10) + ('missing', 'mismatch', 'nonzero').index(record_axis)}"
    pm_home, slot, _ticket, plan = _prepare_recorded_developer(
        pd, budget_env, phase=phase, suffix=suffix,
    )
    path = plan.rounds[0].path
    text = path.read_text(encoding="utf-8")
    if record_axis == "missing":
        text = text.replace("## 회귀\n", "## 회귀 기록 누락\n", 1)
    elif record_axis == "mismatch":
        text = text.replace(
            f"- 커맨드: `{pd._full_regression_command(slot)}`",
            "- 커맨드: `python3 -m pytest tests/ -q`",
            1,
        )
    else:
        text = text.replace("- 결과: rc=0 · fixture green", "- 결과: rc=1 · 1 failed", 1)
    path.write_text(text, encoding="utf-8", newline="")
    calls = []

    def targeted_only(command, expected, *, cwd):
        calls.append((command, expected))
        assert expected is not None, "거부 경로도 full을 중복 실행하면 안 된다"
        return None

    monkeypatch.setattr(pd, "_run_required_test", targeted_only)

    outcome = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )[0]
    assert outcome.refusal is not None and expected_message in outcome.refusal
    assert outcome.terminal is (phase == "fix")
    assert all(expected is not None for _command, expected in calls)


def test_fix_harvest_runs_the_reviewer_required_regression(pd, budget_env):
    pm_home, slot, tickets = budget_env
    ticket = "T-7005"
    _seed(pm_home, tickets, "C-review-red", [ticket])
    for role in _CYCLE[:2]:
        _advance(pd, "C-review-red", role, pm_home=pm_home, slot=slot)

    review = pd.prepare_cluster_copy(
        cluster="C-review-red", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    finding = {
        "id": "F-001", "class": "implementation-defect", "severity": "must-fix",
        "authority": f"[[{ticket}]] §완료 조건", "evidence": "red 재현",
        "recommendation": "contract대로 수정",
        "fix_contract": {
            "location": "src/example.py:1", "failure": "현재 red",
            "design": "불변식을 보존하며 수정",
            "test": "tests/test_review_regression.py 회귀 1건 추가",
            "command": python_argv_command("-m", "module_that_does_not_exist_t0871"),
            "expected": "green",
        },
        "design_change": False,
    }
    payload = json.dumps({
        "version": pd.PM_REVIEW_VERSION, "findings": [finding], "confirmations": [],
    }, ensure_ascii=False, separators=(",", ":"))
    header = review.rounds[0].path.read_text(encoding="utf-8").partition("\n")[0]
    review.rounds[0].path.write_text(
        f"{header}\n\n## must-fix\n- F-001\n\n## 판정\n판정: 반려\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n{payload}\n```\n",
        encoding="utf-8", newline="",
    )
    outcomes = pd.harvest_cluster_copy(
        run_dir=review.run_dir, cwd=slot, pm_home=pm_home,
    )
    assert outcomes[0].refusal is None
    _record_accepted_finding(pd, pm_home, ticket, review.rounds[0].ordinal, "F-001")

    fix = pd.prepare_cluster_copy(
        cluster="C-review-red", role="developer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(pd, fix.rounds[0].path, "developer")
    target = slot / "tests" / "test_review_regression.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# reviewer contract target\n", encoding="utf-8", newline="\n")
    outcomes = pd.harvest_cluster_copy(
        run_dir=fix.run_dir, cwd=slot, pm_home=pm_home,
    )
    assert outcomes[0].refusal is not None
    assert "reviewer 추가 회귀 F-001" in outcomes[0].refusal


@pytest.mark.parametrize("red_axis", ("architect", "reviewer", "record"))
def test_final_fix_red_is_a_terminal_stop_with_preserved_evidence(
    pd, budget_env, monkeypatch, red_axis,
):
    pm_home, slot, tickets = budget_env
    ticket = "T-7008"
    cluster = f"C-terminal-{red_axis}"
    _seed(pm_home, tickets, cluster, [ticket])
    for role in _CYCLE[:2]:
        _advance(pd, cluster, role, pm_home=pm_home, slot=slot)
    reviewer_command = python_argv_command(
        "-m", "pytest", "tests/test_terminal_regression.py", "-q",
    )
    _prepare_accepted_review(
        pd, pm_home=pm_home, slot=slot, cluster=cluster, ticket=ticket,
        command=reviewer_command,
    )
    (slot / ".project_manager" / "local.conf").write_text(
        f"test.cmd={python_argv_command('-m', 'pytest', 'tests/', '-q', '-n', 'auto')}\n",
        encoding="utf-8", newline="\n",
    )
    fix = pd.prepare_cluster_copy(
        cluster=cluster, role="developer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(pd, fix.rounds[0].path, "developer")
    target = slot / "tests" / "test_terminal_regression.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# final fix regression target\n", encoding="utf-8", newline="\n")
    if red_axis == "record":
        text = fix.rounds[0].path.read_text(encoding="utf-8").replace(
            "- 결과: rc=0 · fixture green", "- 결과: rc=1 · 1 failed", 1,
        )
        fix.rounds[0].path.write_text(text, encoding="utf-8", newline="")

    def required_test(command, expected, *, cwd):
        assert expected is not None, "final-fix harvest가 stage-exit full을 중복 실행했다"
        if red_axis == "architect" and command == python_argv_command("--version"):
            return "green이 아닙니다: architect axis"
        if red_axis == "reviewer" and command == reviewer_command:
            return "green이 아닙니다: reviewer axis"
        return None

    monkeypatch.setattr(pd, "_run_required_test", required_test)
    outcome = pd.harvest_cluster_copy(
        run_dir=fix.run_dir, cwd=slot, pm_home=pm_home,
    )[0]

    assert outcome.refusal is not None and outcome.terminal is True
    assert "terminal stop" in outcome.refusal
    assert "사용자에게 보고" in outcome.refusal
    for forbidden in ("재회수", "새 prepare", "새 위임", "사본을 고쳐"):
        assert forbidden not in outcome.refusal
    assert fix.rounds[0].path.exists()
    assert _round_names(pm_home, ticket)[-1] == "04-developer.md"

    delegated = pd._delegation_harvest_failure_message(
        pd.TerminalFixHarvestError(outcome.refusal), fix.run_dir,
    )
    assert "terminal stop" in delegated and "사용자에게 보고" in delegated
    for forbidden in ("재회수", "새 prepare", "새 위임", "사본을 고쳐"):
        assert forbidden not in delegated


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
    assert "되돌리는 경로는 없습니다" in message
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
    assert "고정 라운드 종료" in message and "T-7030" in message
    assert "추가 라운드 없이 정지·보고" in message
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


def test_cli_prepare_refuses_over_budget_with_the_stop_prescription(
        pd, budget_env, monkeypatch, capsys):
    """CLI rc 는 1 이고 처방은 추가 라운드 없는 정지·보고다."""
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
    assert "고정 라운드 종료" in err
    assert "추가 라운드 없이 정지·보고" in err
    assert "replan" not in err and "--force" not in err


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

    assert "고정 라운드 종료" in str(caught.value)
    # 경쟁 예약분(04) 하나만 남고 이 호출의 예약은 없다.
    assert _round_names(pm_home, "T-7050") == [
        "01-architect.md", "02-developer.md", "03-code-reviewer.md", "04-developer.md",
    ]


_ALL_ZERO_BUDGET = {
    "architect": 0, "developer_per_ticket": 0, "code-reviewer": 0, "fix": 0,
}


def test_an_all_zero_budget_is_invalid(pd):
    """0은 단계 생략 경로가 아니라 손상된 고정 수열 선언이다."""
    with pytest.raises(pd.DelegateError, match="architect=1"):
        pd.cluster_round_sequence(_ALL_ZERO_BUDGET, cluster="C-zero")


def test_a_zero_budget_ledger_refuses_before_any_reservation(pd, budget_env):
    """0을 선언한 손상 장부는 전 역할을 fail-loud하고 잔여를 만들지 않는다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-zero", ["T-7100"], budget=_ALL_ZERO_BUDGET)

    for role in _CYCLE:
        with pytest.raises(pd.DelegateError) as caught:
            pd.prepare_cluster_copy(
                cluster="C-zero", role=role, cwd=slot, pm_home=pm_home,
            )
        message = str(caught.value)
        assert "architect=1" in message
    # 거부는 예약 앞이다 — 라운드도 run-dir 도 만들어지지 않았다.
    assert _round_names(pm_home, "T-7100") == []
    assert _open_runs(pd, slot, "C-zero") == []

    # 네 값이 모두 1인 canonical 장부에서는 첫 architect 요청이 통과한다.
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


def test_the_lock_recheck_sees_a_budget_that_became_invalid(
        pd, budget_env, monkeypatch):
    """사전판정 뒤 장부가 0으로 손상되면 락 안 재확인이 fail-loud한다."""
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-shrink", ["T-7110"])
    _refuse_at_the_lock(
        pd, pm_home, monkeypatch,
        lambda: _write_cluster(
            pm_home, "C-shrink", ["T-7110"], budget=_ALL_ZERO_BUDGET),
    )

    with pytest.raises(pd.DelegateError) as caught:
        pd.prepare_cluster_copy(
            cluster="C-shrink", role="architect", cwd=slot, pm_home=pm_home,
        )

    assert "architect=1" in str(caught.value)
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
# 폐지된 재시작 메타데이터는 수열을 다시 열지 않는다
# ════════════════════════════════════════════════════════════════════════

def test_legacy_replan_metadata_does_not_reopen_the_fixed_sequence(pd, budget_env):
    pm_home, slot, tickets = budget_env
    _seed(pm_home, tickets, "C-closed", ["T-7060"])
    for role in _CYCLE:
        _advance(pd, "C-closed", role, pm_home=pm_home, slot=slot)
    _write_cluster(pm_home, "C-closed", ["T-7060"], replans=[
        {"ts": _FIXTURE_STAMP, "reason": "폐지 데이터", "from_ordinal": 4},
    ])

    with pytest.raises(pd.ClusterRoundBudgetExceeded, match="고정 라운드 종료"):
        pd.prepare_cluster_copy(
            cluster="C-closed", role="architect", cwd=slot, pm_home=pm_home,
        )
    assert len(_round_names(pm_home, "T-7060")) == 4


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
    assert "추가 라운드 없이 정지·보고" in str(caught.value)


def test_the_ticket_surface_without_a_ledger_stops_too(pd, budget_env):
    """티켓 표면도 장부 없이는 열리지 않는다 — 필드 없는 티켓이 우회로가 아니다."""
    pm_home, slot, tickets = budget_env
    (tickets / "T-7090-legacy.md").write_text(
        _spec_text("T-7090", "C-T-7090").replace("cluster: C-T-7090\n", ""),
        encoding="utf-8", newline="\n")

    with pytest.raises(pd.DelegateError) as caught:
        pd.prepare_ticket_copy(
            ticket="T-7090", role="architect", cwd=slot, pm_home=pm_home,
        )

    assert "묶음 장부가 없습니다" in str(caught.value)
    assert _round_names(pm_home, "T-7090") == []


# ════════════════════════════════════════════════════════════════════════
# board `cluster replan` 폐지
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


def test_cluster_replan_is_not_a_parser_surface(board_env):
    board, _board_dir = board_env
    with pytest.raises(SystemExit):
        board.build_parser().parse_args([
            "cluster", "replan", "wave", "--reason", "설계 축 지적",
        ])


def test_cluster_help_exposes_only_new_and_show(board_env):
    board, _board_dir = board_env
    actions = board.build_parser()._subparsers._group_actions[0].choices["cluster"]
    cluster_actions = actions._subparsers._group_actions[0].choices
    assert set(cluster_actions) == {"new", "show"}


def test_replan_helpers_are_removed(board_env):
    board, _board_dir = board_env
    assert not hasattr(board, "ticket_design_replan_ordinal")
    assert not hasattr(board, "cluster_replan_baseline")


def test_new_cluster_ledger_has_only_the_fixed_budget(board_env):
    board, _board_dir = board_env
    fm = board._new_cluster_fm("C-json", ["T-0001"])
    assert fm["budget"] == board.CLUSTER_BUDGET_DEFAULT
    assert "replans" not in fm
    assert set(fm["budget"].values()) == {1}
