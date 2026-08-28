"""T-0750 — 라운드 파일 1개 왕복(prepare/harvest)과 run 닫힘 경계 [[ADR-0090]].

여기서 지키는 성질은 넷이다.
  (1) 에이전트가 쓸 수 있는 회수 대상 파일은 run 당 정확히 하나다.
  (2) 회수는 PM 홈 장부에 준비 기록이 있는 경로만 받는다(신뢰 뿌리는 슬롯 밖이다).
  (3) 회수 성공 = run-dir 삭제 = run 닫힘 — 같은 run 은 다시 회수되지 않는다.
  (4) 시드 그대로인 산출은 board 를 바꾸지 않는다(경고만 · 게이트가 아니다).

단일 파일 컨테이너 때문에 있던 장치(봉인·성장 장부·사본 MAC·baseline·절 밖 대조·transfer·
차등 판정·반사실 프로브)는 [[ADR-0090]] 로 사라졌다 — 그 성질을 재는 테스트는 옮기지 않고
지웠고, 여기서는 부재를 심볼 grep 으로 못박는다.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import unittest.mock
from pathlib import Path

import pytest

from _test_exec import python_argv_command
from _win_skip import _can_symlink as can_symlink, posix_mode_supported
from conftest import write_cluster_ledger

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_DELEGATE = TOOLS / "pm_delegate.py"

# 단일 파일 컨테이너 보정 장치의 심볼 — 하나라도 살아 있으면 삭제가 절반만 된 것이다.
DELETED_SYMBOLS = (
    "_ticket_growth_sections", "_append_ticket_growth_section", "_ticket_role_section",
    "seal_for", "parse_ticket_seals", "verify_ticket_seals", "backfill_ticket_seals",
    "_ticket_seal_line", "_upsert_ticket_seal", "ticket_growth_misplaced_seal_keys",
    "require_sealed_growth_before_write", "ticket_growth_seal_recovery_guidance",
    "TICKET_GROWTH_LEDGER_DIRNAME", "TICKET_GROWTH_LEDGER_FIELDS",
    "TICKET_GROWTH_MIGRATION_STAMP_NAME", "ticket_growth_dir",
    "ticket_growth_dir_for_ticket_path", "ticket_growth_ledger_path",
    "ticket_growth_stamp_path", "ticket_growth_migration_stamped",
    "ticket_growth_record_line", "parse_ticket_growth_ledger",
    "read_ticket_growth_ledger", "verify_ticket_growth",
    "format_ticket_growth_problems", "append_ticket_growth_records",
    "upsert_ticket_seal_with_ledger", "ticket_growth_migration_pending",
    "maybe_stamp_ticket_growth_migration", "ticket_growth_section_seed_is_unedited",
    "TICKET_COPY_TRUST_REL_ROOT", "TICKET_COPY_LEDGER_REL_PATH",
    "TICKET_COPY_BASELINE_NAME", "TICKET_COPY_METADATA_NAME", "TICKET_COPY_TAG_NAME",
    "TICKET_COPY_METADATA_VERSION", "TICKET_COPY_HMAC_DOMAIN", "_ticket_copy_tag",
    "_ticket_copy_metadata_bytes", "_load_ticket_copy_plan", "_secure_ticket_trust_dir",
    "_resolve_ticket_copy_capability", "_assert_ticket_copy_ledger_identity",
    "_mark_ticket_copy_harvested", "_read_machine_files",
    "_pm_review_probe_section_text", "_pm_review_probe_self_check",
    "_pm_review_probe_section_content", "_pm_review_delta_regression_reason",
    "_additional_reviewer_delta_regression", "_pm_review_delta_malformed_reason",
    "_pm_review_outside_sections_text", "_pm_review_refused_section_keys",
    "_cmd_ticket_seal_backfill", "_seal_backfill_one", "_SEAL_BACKFILL_STATUSES",
)


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_rounds", PM_DELEGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_argv_command_preserves_spaced_windows_interpreter_argv(monkeypatch):
    """POSIX에서도 Windows interpreter의 공백·backslash argv 손상을 재현한다."""
    interpreter = r"C:\Users\qa user\Python\python.exe"
    monkeypatch.setattr(sys, "executable", interpreter)

    command = python_argv_command("-m", "pytest", r"tests\test sample.py")

    assert shlex.split(command) == [
        interpreter, "-m", "pytest", r"tests\test sample.py",
    ]


@pytest.fixture(scope="module")
def pd():
    module = _load_pd()

    # 이 파일은 라운드 사본·장부·회수의 낮은 층 단위 계약을 각 role 좌석으로
    # 격리해 검증한다. 출하 고정 4자리 수열 계약은 test_round_budget.py가 실
    # cluster_round_sequence로 전수 검증하므로, 이 transport 픽스처에서는 장부 count를
    # 엣 수열로 편다. 제품 코드의 고정 수열 판정을 우회하는 출하 경로는 아니다.
    def _transport_round_sequence(budget, *, cluster):
        del cluster
        return tuple(
            role
            for role, key in module.CLUSTER_BUDGET_ROLE_SEQUENCE
            for _ in range(int(budget.get(key, 0)))
        )

    module.cluster_round_sequence = _transport_round_sequence

    # T-0871부터 developer 준비/회수는 architect가 정한 실제 테스트 계약과
    # 그 테스트 파일의 단계별 diff 결속을 함께 요구한다. 이 파일은 T-0750의
    # 사본·장부·회수 저수준 계약을 검증하며, 새 계약 자체는
    # test_round_budget.py / test_pm_review_delta.py가 실 입력으로 검증한다.
    # 따라서 여기의 옛 단일-role 픽스처에는 한 개의 유효 계약을 주입해
    # 운송 계층의 관측이 상위 단계 계약 부재 때문에 가려지지 않게 한다.
    transport_test = module.ArchitectTest(
        id="AT-TRANSPORT",
        target="tests/test_pm_delegate_rounds.py",
        command=python_argv_command("--version"),
        expected="Python",
        negative="명령 실패 또는 Python 표식 누락은 거부",
    )
    real_parse_architect_tests = module.parse_architect_tests

    def _transport_parse_architect_tests(text):
        try:
            return real_parse_architect_tests(text)
        except module.DelegateError as exc:
            if (
                "block 정확히 1개" in str(exc)
                or "placeholder" in str(exc)
            ):
                return (transport_test,)
            raise

    module.parse_architect_tests = _transport_parse_architect_tests
    real_architect_tests_from_rounds = module.architect_tests_from_rounds

    def _transport_architect_tests(rounds, *, required=True):
        try:
            return real_architect_tests_from_rounds(rounds, required=required)
        except module.DelegateError as exc:
            if required and (
                "architect 테스트 계약이 없습니다" in str(exc)
                or "placeholder" in str(exc)
            ):
                return (transport_test,)
            raise

    real_changed_paths = module._developer_round_changed_paths

    def _transport_changed_paths(repo_root, *, base_rev):
        return real_changed_paths(repo_root, base_rev=base_rev) | {
            transport_test.target,
        }

    module.architect_tests_from_rounds = _transport_architect_tests
    module._developer_round_changed_paths = _transport_changed_paths
    module._full_regression_command = lambda _cwd: python_argv_command("--version")
    module.parse_developer_regression_record = lambda _text: module.DeveloperRegressionRecord(
        python_argv_command("--version"), "rc=0 · transport fixture green",
    )
    return module


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


# 설계 근거 게이트가 인정하는 `design: done` 의 짝 — 4항목이 값으로 채워진 설계 절.
_FILLED_DESIGN_SECTION = (
    "## 설계\n"
    "- **경계 실측**: 진입점 3개 실측\n"
    "- **불변식**: 기록 없는 건너뜀 0\n"
    "- **표면 상한**: 판정 함수 1개\n"
    "- **테스트 전략**: 정상 3경로·실패 4형상\n"
)


def _fix_contract(finding_id: str) -> dict[str, str]:
    """현행 reviewer v3 finding에 필요한 실행 가능한 최소 수정 계약."""
    return {
        "location": ".project_manager/tools/pm_delegate.py:harvest_ticket_copy",
        "failure": f"{finding_id} 재현에서 회수 계약이 기대와 다름",
        "design": f"{finding_id} 범위만 수정하고 기존 장부 불변식 보존",
        "test": "tests/test_pm_delegate_rounds.py",
        "command": python_argv_command("--version"),
        "expected": "Python",
    }


def _spec_text(ticket: str, *, status: str = "claimed", body: str = "") -> str:
    return (
        "---\n"
        f"id: {ticket}\n"
        "title: 라운드 왕복\n"
        f"status: {status}\n"
        "created: '2026-08-18'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        "claimed_at: '2026-08-18T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: medium\n"
        "design: done\n"
        "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\n라운드 사이드카 왕복.\n\n"
        + _FILLED_DESIGN_SECTION + body
    )


def _fixture_board(pd, pm_home: Path, sync_log: list | None = None):
    board = pd._load_module_from_path(
        pm_home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = pm_home
    board.LOCAL_DIR = pm_home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"

    def _sync(message, paths):
        if sync_log is not None:
            sync_log.append((message, [Path(item) for item in paths]))
        return True

    board._rounds_mutation_sync_paths = _sync
    return board


@pytest.fixture
def rounds_env(tmp_path, pd, monkeypatch):
    """PM 홈(board 데이터+엔진 사본)과 ignore 된 슬롯 git 트리 한 쌍."""
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
    slot_ignore = slot / ".project_manager" / ".gitignore"
    slot_ignore.parent.mkdir()
    slot_ignore.write_text(".local/\n", encoding="utf-8", newline="\n")
    (slot / "tracked.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(slot, "add", "tracked.txt", ".project_manager/.gitignore").returncode == 0
    monkeypatch.setenv("GIT_AUTHOR_NAME", "rounds")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "rounds@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "rounds")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "rounds@test.invalid")
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    sync_log: list = []
    monkeypatch.setattr(
        pd, "_load_board_for_repo",
        lambda _repo: _fixture_board(pd, pm_home, sync_log),
    )
    return pm_home, slot, tickets, sync_log


# 이 파일의 티켓은 `cluster` 필드가 없는 크기 1 묶음이다 — 준비는 예산·순서를 그 묶음
# 장부에서만 읽으므로, 명세를 쓰는 자리가 장부도 함께 쓴다. `rounds` 는 그 티켓이 예약할
# 라운드 역할 순서다(장부 예산 = 그 수열).
_LEDGER_BASE_BRANCH = "task/main"


def _declare_rounds(tickets: Path, members, rounds, *, cluster: str | None = None) -> Path:
    """묶음 장부를 board 자리에 쓴다 — 예산은 예약 계획 그대로.

    `cluster` 를 생략하면 그 티켓의 크기 1 묶음(`C-<티켓>`)이다.
    """
    return write_cluster_ledger(
        tickets.parent.parent, members, base_branch=_LEDGER_BASE_BRANCH,
        cluster=cluster, rounds=rounds,
    )


def _write_spec(
    tickets: Path, ticket: str, *, rounds=("developer",), **kwargs,
) -> Path:
    path = tickets / f"{ticket}-rounds.md"
    path.write_text(_spec_text(ticket, **kwargs), encoding="utf-8", newline="\n")
    _declare_rounds(tickets, ticket, rounds)
    return path


def _copy_root(pd, tree: Path, ticket: str) -> Path:
    """그 티켓의 **크기 1 묶음** run 들이 사는 자리 — `<사본 루트>/C-<티켓>/`.

    준비 단위가 묶음이라 run-dir 은 묶음 키로 갈리고 그 안에서 티켓별 자리를 갖는다. 티켓
    하나짜리 준비도 같은 규약을 쓴다(특례 없음).
    """
    return tree / pd.TICKET_COPY_REL_ROOT / f"C-{ticket}"


def _rounds_dir(pm_home: Path, ticket: str) -> Path:
    return (
        pm_home / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket
    )


def _ledger_rows(pm_home: Path) -> list[dict]:
    path = (
        pm_home / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    )
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# ── 준비 → 편집 → 회수 왕복 ────────────────────────────────────────────────

def test_prepare_lays_one_writable_round_and_read_only_inputs(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7001")
    plan = pd.prepare_ticket_copy(
        ticket="T-7001", role="developer", cwd=slot, pm_home=pm_home,
    )

    assert plan.ordinal == 1
    assert plan.path.name == "01-developer.md"
    assert plan.board_path == _rounds_dir(pm_home, "T-7001") / "01-developer.md"
    # board 예약본과 슬롯 사본은 같은 bytes 다(시드).
    assert plan.path.read_bytes() == plan.board_path.read_bytes()
    # run-dir 안에서 쓸 수 있는 파일은 라운드 하나뿐이고 나머지는 읽기 전용 입력이다.
    names = sorted(item.name for item in plan.run_dir.iterdir())
    assert names == ["01-developer.md", "rounds", "spec.md"]
    assert (plan.run_dir / "spec.md").read_text(encoding="utf-8") == (
        (tickets / "T-7001-rounds.md").read_text(encoding="utf-8")
    )
    assert list((plan.run_dir / "rounds").iterdir()) == []   # 첫 라운드 — 이전 산출 없음
    if posix_mode_supported():
        assert stat.S_IMODE(plan.path.stat().st_mode) == 0o600
        assert stat.S_IMODE((plan.run_dir / "spec.md").stat().st_mode) == 0o400
    row = _ledger_rows(pm_home)[-1]
    assert row["ticket"] == "T-7001" and row["role"] == "developer"
    assert row["ordinal"] == 1 and row["harvested_at"] is None
    assert Path(row["copy"]) == plan.path
    assert (pm_home / row["board_rel"]) == plan.board_path


def test_harvest_replaces_board_round_and_closes_the_run(pd, rounds_env):
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7002")
    plan = pd.prepare_ticket_copy(
        ticket="T-7002", role="developer", cwd=slot, pm_home=pm_home,
    )
    produced = plan.path.read_text(encoding="utf-8") + "\n## 변경 파일\n- 실산출\n"
    plan.path.write_text(produced, encoding="utf-8", newline="")

    result = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    assert result.changed is True and result.sync_ready is True
    assert plan.board_path.read_text(encoding="utf-8") == produced
    assert not plan.run_dir.exists()          # run 닫힘 = 재회수 없음
    assert sync_log[-1][1] == [plan.board_path]
    assert _ledger_rows(pm_home)[-1]["harvested_at"] is not None


def test_closed_run_cannot_be_harvested_again(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7003")
    plan = pd.prepare_ticket_copy(
        ticket="T-7003", role="developer", cwd=slot, pm_home=pm_home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n산출\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    # run-dir 이 없다 = run 이 닫혔다. 별도 "이미 회수됨" 판정 없이 경로에서 자연 실패한다.
    with pytest.raises(pd.DelegateError, match="경로 검사 실패|읽기 실패"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


def test_unprepared_path_is_refused_before_any_read(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7004")
    plan = pd.prepare_ticket_copy(
        ticket="T-7004", role="developer", cwd=slot, pm_home=pm_home,
    )
    smuggled = plan.run_dir / "02-developer.md"
    smuggled.write_text("## 리뷰\n밀반입\n", encoding="utf-8", newline="\n")

    with pytest.raises(pd.DelegateError, match="준비 기록 없음"):
        pd.harvest_ticket_copy(copy_path=smuggled, cwd=slot, pm_home=pm_home)


@pytest.mark.skipif(not can_symlink(), reason="symlink 생성 능력이 없는 환경")
def test_symlinked_copy_root_is_refused(pd, rounds_env, tmp_path):
    """사본 루트로 가는 길목을 symlink 로 갈아끼우면 회수하지 않는다(이식 경계 seam)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7016")
    plan = pd.prepare_ticket_copy(
        ticket="T-7016", role="developer", cwd=slot, pm_home=pm_home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n산출\n",
        encoding="utf-8", newline="",
    )
    root = slot / pd.TICKET_COPY_REL_ROOT
    decoy = tmp_path / "decoy"
    shutil.move(str(root), str(decoy))
    os.symlink(decoy, root, target_is_directory=True)

    with pytest.raises(pd.DelegateError, match="symlink/비-directory 거부"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


@pytest.mark.skipif(not can_symlink(), reason="symlink 생성 능력이 없는 환경")
def test_symlink_round_file_is_refused(pd, rounds_env, tmp_path):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7005")
    plan = pd.prepare_ticket_copy(
        ticket="T-7005", role="developer", cwd=slot, pm_home=pm_home,
    )
    outside = tmp_path / "outside.md"
    outside.write_text("## 리뷰\n남의 파일\n", encoding="utf-8", newline="\n")
    plan.path.unlink()
    os.symlink(outside, plan.path)

    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW 없는 플랫폼 — 이식 경계 seam 축이 다름")
    with pytest.raises(pd.DelegateError, match="읽기 실패"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


# ── 산출 없음(시드 그대로) ─────────────────────────────────────────────────

def test_unedited_seed_warns_keeps_run_dir_and_leaves_board_untouched(
        pd, rounds_env, capsys):
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7006", rounds=("code-reviewer",))
    plan = pd.prepare_ticket_copy(
        ticket="T-7006", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    before = plan.board_path.read_bytes()

    result = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    assert result.changed is False
    assert plan.board_path.read_bytes() == before
    assert plan.run_dir.exists() and plan.path.exists()
    assert sync_log == []
    assert "산출 없음" in capsys.readouterr().err
    assert _ledger_rows(pm_home)[-1]["harvested_at"] is None


def test_crlf_slot_round_is_still_judged_unedited(pd, rounds_env, capsys):
    """CRLF 로 돌아온 같은 골격을 '편집됨' 으로 읽으면 산출 없는 라운드가 위장된다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7007")
    plan = pd.prepare_ticket_copy(
        ticket="T-7007", role="developer", cwd=slot, pm_home=pm_home,
    )
    seed = plan.path.read_text(encoding="utf-8")
    plan.path.write_bytes(seed.replace("\n", "\r\n").encode("utf-8"))

    result = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    assert result.changed is False
    assert "산출 없음" in capsys.readouterr().err


# ── 순번·병렬·resume ──────────────────────────────────────────────────────

def test_same_ticket_same_role_parallel_runs_get_distinct_ordinals(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7008", rounds=("developer", "developer"))
    first = pd.prepare_ticket_copy(
        ticket="T-7008", role="developer", cwd=slot, pm_home=pm_home,
    )
    second = pd.prepare_ticket_copy(
        ticket="T-7008", role="developer", cwd=slot, pm_home=pm_home,
    )

    assert (first.ordinal, second.ordinal) == (1, 2)
    assert first.run_dir != second.run_dir
    assert sorted(
        item.name for item in _rounds_dir(pm_home, "T-7008").iterdir()
    ) == ["01-developer.md", "02-developer.md"]
    # 두 번째 준비는 첫 라운드를 읽기 전용 입력으로 깐다.
    assert (second.run_dir / "rounds" / "01-developer.md").exists()


def test_resume_after_harvest_opens_a_new_round(pd, rounds_env):
    """이어 시키는 것은 재회수가 아니라 새 라운드다(`transfer_from` 이 사라진 자리)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7009", rounds=("developer", "developer"))
    first = pd.prepare_ticket_copy(
        ticket="T-7009", role="developer", cwd=slot, pm_home=pm_home,
    )
    first.path.write_text(
        first.path.read_text(encoding="utf-8") + "\n1라운드 산출\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=first.path, cwd=slot, pm_home=pm_home)

    second = pd.prepare_ticket_copy(
        ticket="T-7009", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert second.ordinal == 2
    assert "1라운드 산출" in (
        second.run_dir / "rounds" / "01-developer.md"
    ).read_text(encoding="utf-8")


def test_ordinal_is_ticket_global_across_roles(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7010", rounds=("developer", "code-reviewer"))
    dev = pd.prepare_ticket_copy(
        ticket="T-7010", role="developer", cwd=slot, pm_home=pm_home,
    )
    reviewer = pd.prepare_ticket_copy(
        ticket="T-7010", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    assert (dev.ordinal, reviewer.ordinal) == (1, 2)
    assert reviewer.board_path.name == "02-code-reviewer.md"


# ── 상태 게이트 · ignore 검증 ─────────────────────────────────────────────

def test_done_ticket_is_refused_for_prepare(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    done = tickets.parent / "done"
    done.mkdir()
    path = done / "T-7011-rounds.md"
    path.write_text(_spec_text("T-7011", status="done"), encoding="utf-8", newline="\n")

    with pytest.raises(pd.DelegateError, match="open/claimed 또는 draft×architect"):
        pd.prepare_ticket_copy(
            ticket="T-7011", role="developer", cwd=slot, pm_home=pm_home,
        )


def test_draft_ticket_allows_architect_only(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    drafts = tickets.parent / ".drafts"
    drafts.mkdir()
    (drafts / "T-7012-rounds.md").write_text(
        _spec_text("T-7012", status="draft"), encoding="utf-8", newline="\n",
    )
    _declare_rounds(tickets, "T-7012", ("architect",))

    with pytest.raises(pd.DelegateError, match="draft×architect"):
        pd.prepare_ticket_copy(
            ticket="T-7012", role="developer", cwd=slot, pm_home=pm_home,
        )
    plan = pd.prepare_ticket_copy(
        ticket="T-7012", role="architect", cwd=slot, pm_home=pm_home,
    )
    assert plan.board_path.name == "01-architect.md"


def test_slot_without_tracked_ignore_rule_is_refused(pd, rounds_env):
    """[[T-0704]] — 사본 루트를 숨기는 규칙이 정본 위치·tracked 여야 한다(유지)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7013")
    assert _git(slot, "rm", "-q", "--cached", ".project_manager/.gitignore").returncode == 0

    with pytest.raises(pd.DelegateError, match="untracked"):
        pd.prepare_ticket_copy(
            ticket="T-7013", role="developer", cwd=slot, pm_home=pm_home,
        )


# ── 시드 프리필 ([[T-0749]] 리뷰 F-007) ──────────────────────────────────

def test_review_seed_prefills_confirmations_from_previous_round_file(pd, rounds_env):
    """확인 대상 finding ID 의 입력은 **같은 역할의 직전 라운드 파일**이다([[T-0749]] F-007)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7014", rounds=("code-reviewer", "code-reviewer"))
    first = pd.prepare_ticket_copy(
        ticket="T-7014", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    block = json.dumps({
        "version": pd.PM_REVIEW_VERSION,
        "findings": [{
            "id": "F-042", "class": "implementation-defect", "severity": "must-fix",
            "authority": "[[ADR-0090]] §경계", "evidence": "probe rc=1",
            "recommendation": "F-042만 수정", "design_change": False,
            "fix_contract": _fix_contract("F-042"),
        }],
        "confirmations": [],
    }, ensure_ascii=False)
    first.path.write_text(
        first.path.read_text(encoding="utf-8").partition("\n")[0]
        + "\n\n## must-fix\n- F-042\n\n## 판정\n판정: 반려 · finding 1건(must-fix 1건)\n\n"
        + f"```{pd.PM_REVIEW_BLOCK}\n{block}\n```\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=first.path, cwd=slot, pm_home=pm_home)

    second = pd.prepare_ticket_copy(
        ticket="T-7014", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    seed = second.path.read_text(encoding="utf-8")
    block_text = seed.partition(f"```{pd.PM_REVIEW_BLOCK}")[2].replace(" ", "")
    # 확인 대상은 직전 라운드 파일의 실 ID 이고 자리표시자로 강등되지 않는다.
    assert '{"id":"F-042","status":"<' in block_text
    assert '"confirmations":[{"id":"F-NNN"' not in block_text


def test_review_seed_prefill_ignores_other_role_rounds(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7015", rounds=("developer", "code-reviewer"))
    dev = pd.prepare_ticket_copy(
        ticket="T-7015", role="developer", cwd=slot, pm_home=pm_home,
    )
    dev.path.write_text(
        dev.path.read_text(encoding="utf-8") + "\nF-042 를 산문으로 언급\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=dev.path, cwd=slot, pm_home=pm_home)

    reviewer = pd.prepare_ticket_copy(
        ticket="T-7015", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    # 직전 **같은 역할** 라운드가 없으므로 자리표시자 골격이다.
    block_text = reviewer.path.read_text(encoding="utf-8").partition(
        f"```{pd.PM_REVIEW_BLOCK}"
    )[2].replace(" ", "")
    assert '"confirmations":[{"id":"F-NNN"' in block_text


# ── 다음 finding ID 실값 주입 (시드·사본 프리앰블) ────────────────────────

def _tickets_root(pm_home: Path) -> Path:
    return pm_home / ".project_manager" / "wiki" / "tickets"


def _land_architect_round_citing(pd, pm_home: Path, slot: Path, ticket: str, cited: str):
    """산문에만 그 ID 가 있는 architect 라운드를 실제로 착지시킨다(실 파일 픽스처).

    리뷰 블록이 아니라 **산문 인용**이다 — 관용 스캔이 잡는 그 형상이 다음 번호를 밀어 올린다.
    """
    plan = pd.prepare_ticket_copy(
        ticket=ticket, role="architect", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(plan.path, (
        f"## 경계 실측\n- 다른 티켓 리뷰의 {cited} 인용\n\n"
        "## 불변식\n- 판정 입력은 그 파일 하나\n\n"
        "## 표면 상한\n- 픽스처 1건\n\n"
        "## 테스트 전략\n- 정상·실패 경로\n\n"
        "검토 판정: 수정 후 통과\n"
    ))
    pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)
    return plan


def test_review_seed_and_preamble_hold_the_next_finding_id_after_a_prose_citation(
    pd, rounds_env,
):
    """이전 라운드 **산문**에만 있는 ID 도 다음 번호를 밀어 올리고, 그 실값이 시드·프리앰블에 든다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7030", rounds=("architect", "code-reviewer"))
    _land_architect_round_citing(pd, pm_home, slot, "T-7030", "F-003")

    reviewer = pd.prepare_ticket_copy(
        ticket="T-7030", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )

    seed = reviewer.path.read_text(encoding="utf-8")
    assert '"id":"F-004"' in seed.replace(" ", "")
    assert "`F-004` 부터" in seed.partition("## 판정")[0]
    assert reviewer.next_finding_id == "F-004"
    assert "`F-004` 부터" in pd._ticket_copy_preamble(reviewer)
    assert reviewer.board_path.read_text(encoding="utf-8") == seed

    # 실 ID 를 실은 새 시드도 산출이 없다(판정 입력은 그 파일 자신이다).
    rounds_module = pd._load_ticket_rounds()
    loaded = rounds_module.load_rounds(_tickets_root(pm_home), "T-7030")
    assert [(item.ordinal, item.pending) for item in loaded] == [(1, False), (2, True)]
    problems = rounds_module.verify_rounds(_tickets_root(pm_home), "T-7030")
    assert [item.code for item in problems] == [rounds_module.PROBLEM_PENDING]


def test_a_reply_that_uses_the_seeded_finding_id_is_harvested(pd, rounds_env):
    """엔진이 시드에 넣은 ID 는 선언이 아니다 — 그 번호로 쓴 회신이 자기 자신과 충돌하지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7031", rounds=("architect", "code-reviewer"))
    _land_architect_round_citing(pd, pm_home, slot, "T-7031", "F-003")
    reviewer = pd.prepare_ticket_copy(
        ticket="T-7031", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    assert reviewer.next_finding_id == "F-004"

    _write_round_output(reviewer.path, _review_body(pd, "code-reviewer", ["F-004"]))
    result = pd.harvest_ticket_copy(
        copy_path=reviewer.path, cwd=slot, pm_home=pm_home,
    )

    assert result.changed is True
    assert not reviewer.run_dir.exists()
    assert '"id":"F-004"' in reviewer.board_path.read_text(encoding="utf-8").replace(" ", "")


def test_a_ticket_without_any_finding_id_seeds_the_first_number(pd, rounds_env):
    """0건 라운드는 첫 번호다 — 그리고 리뷰 채널이 아닌 역할에는 실을 값이 없다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7032", rounds=("code-reviewer", "developer"))

    reviewer = pd.prepare_ticket_copy(
        ticket="T-7032", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    assert reviewer.next_finding_id == "F-001"
    assert '"id":"F-001"' in reviewer.path.read_text(encoding="utf-8").replace(" ", "")

    developer = pd.prepare_ticket_copy(
        ticket="T-7032", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert developer.next_finding_id == ""
    assert "finding ID" not in pd._ticket_copy_preamble(developer)


def test_an_abandoned_seed_gives_its_finding_id_back_to_the_next_round(pd, rounds_env):
    """표식이 붙은 라운드의 시드 ID 는 다시 비어 있다 — 종결된 예약은 아무것도 선언하지 않았다.

    중간 순번 포기는 board 시드를 보존한 채 표식만 붙인다. 그 골격의 ID 가 스캔 corpus 에 남으면
    다음 시드는 아무도 쓰지 않은 번호를 건너뛰고, 그 번호를 그대로 쓴 유효 회신은 재선언으로
    거부된다(두 표면이 같은 술어로 표식 라운드를 빼야 한다).
    """
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(
        tickets, "T-7041",
        rounds=("code-reviewer", "code-reviewer", "code-reviewer"))
    abandoned = pd.prepare_ticket_copy(
        ticket="T-7041", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    assert abandoned.next_finding_id == "F-001"
    # 뒤 순번이 있어야 포기가 board 시드를 지우지 않고 보존한다(표식 발행 분기). 그 라운드는
    # finding 0 으로 회수한다 — 번호를 쓰지 않아야 포기한 시드의 ID 반환만 관측된다.
    later = pd.prepare_ticket_copy(
        ticket="T-7041", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(later.path, _review_body(pd, "code-reviewer", []))
    pd.harvest_ticket_copy(copy_path=later.path, cwd=slot, pm_home=pm_home)

    pd.abandon_ticket_copy(
        copy_path=abandoned.path, cwd=slot, pm_home=pm_home, assume_dead=True,
    )
    assert pd.pm_review_refused_marker_present(
        abandoned.board_path.read_text(encoding="utf-8")
    ), "표식이 발행되지 않았다(전제 붕괴)"

    reviewer = pd.prepare_ticket_copy(
        ticket="T-7041", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )

    assert reviewer.next_finding_id == "F-001"
    assert '"id":"F-001"' in reviewer.path.read_text(encoding="utf-8").replace(" ", "")
    # 그 번호로 쓴 회신이 회수된다 — 표식 라운드의 시드 ID 는 재선언 corpus 에도 없다.
    _write_round_output(reviewer.path, _review_body(pd, "code-reviewer", ["F-001"]))
    assert pd.harvest_ticket_copy(
        copy_path=reviewer.path, cwd=slot, pm_home=pm_home,
    ).changed is True
    assert '"id":"F-001"' in reviewer.board_path.read_text(
        encoding="utf-8",
    ).replace(" ", "")


def test_seed_value_inputs_have_no_default_that_folds_a_missing_value(pd, tmp_path):
    """준비가 계산하는 세 값은 기본값이 없다 — 안 넘기면 조용히 접히지 않고 그 자리에서 터진다.

    기본값이 있으면 값을 못 넘긴 조립이 옛 프롬프트 bytes·자리표시자 골격·빈 프리필로 접혀,
    번호를 말하지 않는 시드가 정상 산출처럼 나간다.
    """
    with pytest.raises(TypeError):
        pd.TicketCopyPlan(
            tmp_path / "01-code-reviewer.md", tmp_path, "T-7042",
            pd.INTERNAL_REVIEW_ROLE, 1, tmp_path / "board.md", "e" * 32,
        )
    with pytest.raises(TypeError):
        pd._render_review_round_seed_body(pd.INTERNAL_REVIEW_ROLE, ["F-001"])
    with pytest.raises(TypeError):
        pd.PMReviewVerifyTemplate((), (), (), (), ())


# ── 회수 게이트: 판정 불능 verify 블록 · 자리표시자 행 ──────────────────

def test_harvest_refuses_a_developer_round_whose_verify_block_is_malformed(
    pd, rounds_env,
):
    """verify 블록을 읽지 못하면 회수는 거부다 — 판정 불능을 통과로 접지 않는다.

    같은 시드를 자리표시자 그대로 둔 산출은 회수된다(태만은 거부 사유가 아니다) — 그 ID 를
    짚는 것은 회수가 아니라 `review verify-template` 판정면 하나다.
    """
    pm_home, slot, tickets, _sync = rounds_env
    spec_path = _write_spec(tickets, "T-7020", rounds=("code-reviewer", "developer"))

    reviewer = pd.prepare_ticket_copy(
        ticket="T-7020", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    review_payload = {
        "version": pd.PM_REVIEW_VERSION,
        "findings": [{
            "id": "F-001", "class": "implementation-defect", "severity": "must-fix",
            "authority": "[[T-0786]]", "evidence": "probe", "recommendation": "fix",
            "fix_contract": _fix_contract("F-001"),
            "design_change": False,
        }],
        "confirmations": [],
    }
    reviewer.path.write_text(
        reviewer.path.read_text(encoding="utf-8").partition("\n")[0]
        + "\n\n## must-fix\n- F-001\n\n## 판정\n판정: 반려 · finding 1건(must-fix 1건)\n\n"
        + f"```{pd.PM_REVIEW_BLOCK}\n" + json.dumps(review_payload) + "\n```\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=reviewer.path, cwd=slot, pm_home=pm_home)

    disposition_payload = {
        "version": pd.PM_REVIEW_DISPOSITION_VERSION, "reviewer_ordinal": 1,
        "dispositions": [{
            "id": "F-001", "decision": "accepted", "reason": "PM 수락",
            "scope": "F-001 범위", "prerequisite": "",
        }],
    }
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8")
        + f"\n```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n"
        + json.dumps(disposition_payload) + "\n```\n",
        encoding="utf-8", newline="",
    )

    dev = pd.prepare_ticket_copy(
        ticket="T-7020", role="developer", cwd=slot, pm_home=pm_home,
    )
    seed = dev.path.read_text(encoding="utf-8")
    assert '"id":"F-001"' in seed.partition(f"```{pd.PM_REVIEW_VERIFY_BLOCK}")[2]

    reserved_before = dev.board_path.read_bytes()

    # 시드가 실은 ID 는 그대로 둔 채 블록만 깨뜨린다 — 지운 행이 아니라 판정 불능이다.
    dev.path.write_text(
        seed.replace('"verifications"', '"verifications') + "\n실산출\n",
        encoding="utf-8", newline="",
    )
    with pytest.raises(pd.DelegateError, match="블록을 읽지 못했습니다"):
        pd.harvest_ticket_copy(copy_path=dev.path, cwd=slot, pm_home=pm_home)

    assert dev.board_path.read_bytes() == reserved_before
    assert dev.run_dir.exists()

    # 자리표시자 그대로인 행은 게이트를 통과하고, 태만은 판정면이 이름으로 짚는다.
    dev.path.write_text(seed + "\n실산출\n", encoding="utf-8", newline="")
    result = pd.harvest_ticket_copy(copy_path=dev.path, cwd=slot, pm_home=pm_home)

    assert result.changed is True
    assert _verify_template_of(pd, pm_home, "T-7020").missing == ("F-001",)


# ── T-0805: 시드 시점 의존 — 판정 미기입 거부(잔여 0) · 누적 프리필 ──────

def _finding_payload(
    pd, fid: str, *, design_change: bool = False,
    fix_contract: dict[str, str] | None = None,
) -> dict:
    return {
        "id": fid, "class": "implementation-defect", "severity": "must-fix",
        "authority": "[[T-0805]]", "evidence": f"{fid} probe",
        "recommendation": f"{fid} fix",
        "fix_contract": fix_contract or _fix_contract(fid),
        "design_change": design_change,
    }


def _harvest_review_round(
    pd, pm_home: Path, slot: Path, ticket: str, finding_ids: list[str],
    *, design_change_ids: tuple[str, ...] = (),
    fix_contracts: dict[str, dict[str, str]] | None = None,
):
    """실 준비→편집→회수로 reviewer 라운드 1개를 board 에 남긴다(조립 dict 아님)."""
    reviewer = pd.prepare_ticket_copy(
        ticket=ticket, role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    payload = {
        "version": pd.PM_REVIEW_VERSION,
        "findings": [
            _finding_payload(
                pd, fid, design_change=fid in design_change_ids,
                fix_contract=(fix_contracts or {}).get(fid),
            )
            for fid in finding_ids
        ],
        "confirmations": [],
    }
    mustfix = "\n".join(f"- {fid}" for fid in finding_ids)
    reviewer.path.write_text(
        reviewer.path.read_text(encoding="utf-8").partition("\n")[0]
        + f"\n\n## must-fix\n{mustfix}\n\n## 판정\n판정: 반려 · finding "
        f"{len(finding_ids)}건(must-fix {len(finding_ids)}건)\n\n"
        + f"```{pd.PM_REVIEW_BLOCK}\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=reviewer.path, cwd=slot, pm_home=pm_home)
    return reviewer


def _append_disposition(pd, spec_path: Path, rows: list, *, ordinal: int = 1) -> None:
    payload = {
        "version": pd.PM_REVIEW_DISPOSITION_VERSION, "reviewer_ordinal": ordinal,
        "dispositions": rows,
    }
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8")
        + f"\n```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False) + "\n```\n",
        encoding="utf-8", newline="",
    )


def _verify_rows_in(pd, text: str) -> list[dict]:
    """시드 골격의 verify 행 전체 — 자리표시자든 프리필된 실값이든 그대로 돌려준다.

    골격의 `machine_verifiable` 자리는 따옴표 없는 raw 자리표시자라 그대로는 유효 JSON 이 아니다
    — 엔진의 구조-스캔 전처리와 같은 재-인용을 거친 뒤 파싱한다(자리표시자 문안은 렌더 소유).
    """
    body = pd._pm_review_requote_verify_placeholder(text).partition(
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n")[2]
    if not body:
        return []
    return json.loads(body.split("\n```", 1)[0])["verifications"]


def _verify_ids_in(pd, text: str) -> list[str]:
    """시드 골격의 verify 행 ID 목록."""
    return [row["id"] for row in _verify_rows_in(pd, text)]


def _verify_fence(pd, rows: list) -> str:
    """dev 가 채운 verify 행들을 fence 로 직렬화한다(자리표시자 흉내 금지 — 실 선언 전용)."""
    return (
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps({"version": pd.PM_REVIEW_VERIFY_VERSION, "verifications": rows},
                     ensure_ascii=False)
        + "\n```\n"
    )


def _report_command(text: str) -> str:
    """부작용 없는 실 재현 커맨드 — 회수 게이트가 이 커맨드를 실제로 돌린다.

    셸 builtin(`echo`)은 Windows 에 실행 파일이 없어 `shell=False` 실행이 스폰 실패로 떨어진다 —
    실제로 도는 커맨드는 트리 내 선례(`tests/test_cluster_review_round.py`)와 같은 인터프리터
    호출로 쓴다(금지 토큰 0 · 트리·네트워크 부작용 0).
    """
    return python_argv_command("-c", f"print({text!r})")


def _fill_machine_verifiable(pd, text: str, index: int, value: str = "true") -> str:
    """시드가 심은 boolean 자리표시자 중 `index` 번째만 실값으로 갈아 끼운다.

    자리표시자만 바꾸는 것이 dev 의 정상 채움 경로다 — 나머지 bytes(프리필된 command·expected)는
    시드가 쓴 그대로 남아 그 행이 값 재입력 없이 재선언된다.
    """
    token = (
        f'"{pd._PM_REVIEW_MACHINE_VERIFIABLE_KEY}":'
        + pd._PM_REVIEW_MACHINE_VERIFIABLE_PLACEHOLDER
    )
    position = -1
    for _ in range(index + 1):
        position = text.index(token, position + 1)
    return (
        text[:position]
        + f'"{pd._PM_REVIEW_MACHINE_VERIFIABLE_KEY}":{value}'
        + text[position + len(token):]
    )


@pytest.mark.parametrize("code", ["pending", "decision-required"])
def test_prepare_refuses_a_developer_round_until_the_pm_judgment_is_written(
    pd, rounds_env, code,
):
    """판정 미기입 상태의 준비는 **거부**다 — board 라운드 파일도 장부 행도 남지 않는다(I8).

    강등해서 시드하면 검증 골격이 없는 라운드가 나가고, 두 단계 뒤 판정면의 태만 목록·rc=1 이
    dev 태만처럼 표면화된다(T-0790 실증 형상).
    """
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-7030" if code == "pending" else "T-7031"
    spec_path = _write_spec(tickets, ticket, rounds=("code-reviewer", "developer"))
    _harvest_review_round(pd, pm_home, slot, ticket, ["F-001"])
    if code == "decision-required":
        _append_disposition(pd, spec_path, [{
            "id": "F-001", "decision": "decision-required",
            "reason": "선행 권위 결정 필요", "scope": "",
            "prerequisite": "[[ADR-0001]] 개정 선행",
        }])

    rounds_before = sorted(item.name for item in _rounds_dir(pm_home, ticket).iterdir())
    ledger_before = len(_ledger_rows(pm_home))

    with pytest.raises(pd.DelegateError, match="시드할 수 없습니다") as caught:
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )

    message = str(caught.value)
    assert f"[{code}]" in message
    assert ("disposition-template" in message) if code == "pending" else (
        "현재 티켓을 정지" in message and "사용자 결정" in message
    )
    assert "재설계" not in message
    assert sorted(
        item.name for item in _rounds_dir(pm_home, ticket).iterdir()
    ) == rounds_before, "거부가 board 라운드 파일을 남겼다"
    assert len(_ledger_rows(pm_home)) == ledger_before, "거부가 장부 행을 남겼다"


def test_pending_prepare_guidance_names_the_real_ordinal_and_channel(pd, rounds_env):
    """작은 결함(PM 실측 2026-08-23) — `ticket prepare` 의 "pending" 거부 안내가
    `disposition-template` 기본값(그 채널의 **최신** ordinal)이 아니라 실제로 미판정인
    ordinal·채널·ticket 실값을 커맨드에 싣는다. 옛 ordinal(1)이 미판정이고 그 뒤 최신
    ordinal(2)은 이미 판정된 형상 — 안내대로 기본값(최신)을 그냥 실행하면 "미판정 finding이
    없습니다" 로 막히는 실측 사고를 재현한다.
    """
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-7040"
    spec_path = _write_spec(
        tickets, ticket, rounds=("code-reviewer", "code-reviewer", "developer"))
    _harvest_review_round(pd, pm_home, slot, ticket, ["F-001"])  # ordinal 1 — 미판정으로 남긴다
    _harvest_review_round(pd, pm_home, slot, ticket, ["F-002"])  # ordinal 2 — 뒤에서 판정해 최신을 깨끗하게 만든다
    _append_disposition(pd, spec_path, [{
        "id": "F-002", "decision": "rejected", "reason": "확인됨",
        "scope": "", "prerequisite": "",
    }], ordinal=2)

    with pytest.raises(pd.DelegateError, match="시드할 수 없습니다") as caught:
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )

    message = str(caught.value)
    assert "--ordinal 1" in message
    assert "--reviewer-role code-reviewer" in message
    assert f"--ticket {ticket}" in message
    assert "<T-NNNN>" not in message
    assert "--ordinal 2" not in message  # 최신(잘못된 옛 기본값)을 안내하지 않는다

    # 실측 재현 — 옛 기본값(그 채널의 최신 ordinal=2)으로 실행하면 PM 이 실제로 막혔던
    # 오류가 그대로 재현된다.
    _path, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, ticket)
    with pytest.raises(pd.PMReviewError, match="미판정 finding이 없습니다"):
        pd.render_pm_review_disposition_template(
            spec_text, rounds, 2, reviewer_role="code-reviewer",
        )

    # 안내가 지목한 실값(ordinal=1)으로는 정상적으로 골격이 나온다.
    rendered = pd.render_pm_review_disposition_template(
        spec_text, rounds, 1, reviewer_role="code-reviewer",
    )
    assert "F-001" in rendered


def test_prepare_seeds_the_first_developer_round_without_fence_or_warning(
    pd, rounds_env, capsys,
):
    """역방향 확인 — 리뷰 라운드가 없는 최초 구현 라운드는 종전대로 통과한다(오탐 0)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7032")
    capsys.readouterr()

    plan = pd.prepare_ticket_copy(
        ticket="T-7032", role="developer", cwd=slot, pm_home=pm_home,
    )

    assert pd.PM_REVIEW_VERIFY_BLOCK not in plan.path.read_text(encoding="utf-8")
    assert "경고" not in capsys.readouterr().err


def test_prepare_after_the_judgment_prefills_every_open_accepted_row(pd, rounds_env):
    """다음 시드는 **열린 accepted 전건**을 최신 선언 값 그대로 싣고 여전히 pending 이다.

    라운드 2 에서 F-002 는 해소 선언, F-001 은 빈틈 보고로 끝낸다. 라운드 3 시드는 둘 다 다시
    싣는다 — PM 이 fix 범위를 좁혀 F-002 를 처방에서 빼도 그 행이 시드에서 빠지면 개발자가
    낡은 기대값을 다시 보지 못한다(그 누락이 이 규칙의 기원이다).
    """
    pm_home, slot, tickets, _sync = rounds_env
    spec_path = _write_spec(
        tickets, "T-7033", rounds=("code-reviewer", "developer", "developer"))
    _harvest_review_round(pd, pm_home, slot, "T-7033", ["F-001", "F-002"])
    _append_disposition(pd, spec_path, [
        {"id": fid, "decision": "accepted", "reason": "PM 수락",
         "scope": f"{fid} 범위", "prerequisite": ""}
        for fid in ("F-001", "F-002")
    ])

    first = pd.prepare_ticket_copy(
        ticket="T-7033", role="developer", cwd=slot, pm_home=pm_home,
    )
    seed = first.path.read_text(encoding="utf-8")
    assert _verify_ids_in(pd, seed) == ["F-001", "F-002"]
    # reviewer v3 수정 계약의 test command/expected가 developer 시드에 그대로
    # 결속된다. developer가 별도 확인 명령으로 갈아끼우는 여지는 없다.
    assert [row["command"] for row in _verify_rows_in(pd, seed)] == [
        python_argv_command("--version"), python_argv_command("--version"),
    ]
    assert [row["expected"] for row in _verify_rows_in(pd, seed)] == [
        "Python", "Python",
    ]

    rows = [
        {"id": fid, "machine_verifiable": True,
         "command": python_argv_command("--version"),
         "expected": "Python", "before": f"{fid} 재현 실패", "reason": ""}
        for fid in ("F-001", "F-002")
    ]
    first.path.write_text(
        seed.partition("\n")[0] + "\n\n## 변경 파일\n- 실산출\n\n"
        + f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps({"version": pd.PM_REVIEW_VERIFY_VERSION, "verifications": rows},
                     ensure_ascii=False)
        + "\n```\n",
        encoding="utf-8", newline="",
    )
    result = pd.harvest_ticket_copy(copy_path=first.path, cwd=slot, pm_home=pm_home)
    assert result.changed is True
    # 빈틈 보고는 태만이 아니다 — 그 판정은 회수가 아니라 분류기가 낸다.
    assert _verify_template_of(pd, pm_home, "T-7033").missing == ()

    second = pd.prepare_ticket_copy(
        ticket="T-7033", role="developer", cwd=slot, pm_home=pm_home,
    )
    seed_two = second.path.read_text(encoding="utf-8")
    placeholder = pd._PM_REVIEW_MACHINE_VERIFIABLE_PLACEHOLDER
    assert _verify_rows_in(pd, seed_two) == [
        {"id": "F-001", "machine_verifiable": placeholder,
         "command": python_argv_command("--version"), "expected": "Python",
         "before": "F-001 재현 실패", "reason": ""},
        {"id": "F-002", "machine_verifiable": placeholder,
         "command": python_argv_command("--version"), "expected": "Python",
         "before": "F-002 재현 실패", "reason": ""},
    ]
    # 값이 실려도 선언 자리(`machine_verifiable`)는 자리표시자라 산출 없음 그대로다 —
    # 판정 입력은 그 파일 하나이고 주입 값은 본문 자신에서 되읽는다.
    assert _board_round_is_pending(pd, pm_home, "T-7033", second.ordinal) is True
    # 그 한 자리를 채우는 것이 곧 선언이다 — 채우면 판정이 뒤집힌다.
    assert pd.ticket_round_body_is_pending(
        "developer", _fill_machine_verifiable(pd, seed_two, 0).partition("\n\n")[2],
    ) is False


def _verify_block_bytes(pd, text: str) -> str:
    """그 라운드 파일이 담고 있는 검증 골격 블록 원문(없으면 빈 문자열)."""
    _head, fence, rest = text.partition(f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n")
    return fence + rest.split("\n```", 1)[0] + "\n```" if fence else ""


def _without_verify_block(pd, text: str) -> str:
    """검증 골격 블록을 통째로 걷어낸 본문 — 그 ID 의 행 자체가 없는 라운드를 만든다."""
    head, fence, rest = text.partition(f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n")
    if not fence:
        return text
    return (head + rest.split("\n```\n", 1)[1]).rstrip("\n") + "\n"


def _verify_template_of(pd, pm_home: Path, ticket: str):
    """board 에 실제로 착지한 명세·라운드 파일로 분류기를 돌린다."""
    board = pd._load_board_for_repo(pm_home)
    _status, spec_path = board.find_ticket_exact(ticket)
    spec_text = spec_path.read_text(encoding="utf-8")
    rounds = pd._load_ticket_rounds().load_rounds(
        board.tickets_dir(), ticket, ticket_text=spec_text,
    )
    return pd.pm_review_verify_template(spec_text, rounds)


def _board_round_is_pending(pd, pm_home: Path, ticket: str, ordinal: int) -> bool:
    """board 에 착지한 그 라운드가 '산출 없음' 인가 — 실제 소비 경로(`load_rounds`)로 읽는다."""
    board = pd._load_board_for_repo(pm_home)
    _status, spec_path = board.find_ticket_exact(ticket)
    rounds = pd._load_ticket_rounds().load_rounds(
        board.tickets_dir(), ticket,
        ticket_text=spec_path.read_text(encoding="utf-8"),
    )
    return next(item.pending for item in rounds if item.ordinal == ordinal)


def _verify_template_rc(pd, pm_home: Path, monkeypatch, ticket: str) -> int:
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: pm_home)
    return pd.main(["review", "verify-template", "--ticket", ticket])


def _gap_round_then_prepare_next(pd, pm_home: Path, slot: Path, tickets: Path, ticket: str):
    """실 준비→편집→회수로 라운드 2(F-001 빈틈 보고 · F-002 기계 선언)를 남기고 라운드 3 을 준비한다.

    돌려주는 값은 라운드 3 의 준비 계획과 그 시드 원문이다 — 시드가 실은 자리표시자 bytes 를
    손으로 흉내내지 않고 그대로 편집해야 무편집 판정과 회수 판정이 같은 입력을 본다.
    """
    spec_path = _write_spec(
        tickets, ticket, rounds=("code-reviewer", "developer", "developer"))
    _harvest_review_round(pd, pm_home, slot, ticket, ["F-001", "F-002"])
    _append_disposition(pd, spec_path, [
        {"id": fid, "decision": "accepted", "reason": "PM 수락",
         "scope": f"{fid} 범위", "prerequisite": ""}
        for fid in ("F-001", "F-002")
    ])

    second = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
    )
    seed_two = second.path.read_text(encoding="utf-8")
    assert _verify_ids_in(pd, seed_two) == ["F-001", "F-002"]
    declared = [
        {"id": "F-001", "machine_verifiable": False, "command": "",
         "expected": "처방이 정하지 않은 지점", "before": "",
         "reason": pd.PM_REVIEW_VERIFY_GAP_REASON},
        {"id": "F-002", "machine_verifiable": True, "command": _report_command("2 passed"),
         "expected": "2 passed", "before": "1 failed", "reason": ""},
    ]
    second.path.write_text(
        _without_verify_block(pd, seed_two) + "\n"
        + f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps({"version": pd.PM_REVIEW_VERIFY_VERSION, "verifications": declared},
                     ensure_ascii=False)
        + "\n```\n",
        encoding="utf-8", newline="",
    )
    gap_round = pd.harvest_ticket_copy(copy_path=second.path, cwd=slot, pm_home=pm_home)
    assert gap_round.changed is True
    # 빈틈 보고는 태만이 아니다(라운드 2) — 판정면이 그 ID 를 `gap` 으로 든다.
    assert _verify_template_of(pd, pm_home, ticket).missing == ()

    third = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
    )
    seed_three = third.path.read_text(encoding="utf-8")
    # 라운드 3 시드는 열린 accepted 전건을 다시 싣는다(빈틈 ID 만이 아니다).
    assert _verify_ids_in(pd, seed_three) == ["F-001", "F-002"]
    return third, seed_three


def test_unfilled_seed_row_in_a_produced_round_buries_the_earlier_gap_declaration(
    pd, rounds_env, monkeypatch, capsys,
):
    """부분 태만 — 채운 행은 살고 자리표시자로 둔 행은 앞 라운드의 빈틈 보고를 덮는다.

    라운드 3 은 프리필된 F-002 의 boolean 만 갈아 끼워 재선언하고 F-001 은 손대지 않는다.
    두 ID 가 같은 라운드에서 갈리므로 태만 판정이 부분 확인을 인질로 잡지 않는다.
    """
    pm_home, slot, tickets, _sync = rounds_env
    third, seed_three = _gap_round_then_prepare_next(
        pd, pm_home, slot, tickets, "T-7034",
    )
    edited = _fill_machine_verifiable(pd, seed_three, 1) + "\n## 메모\n- 산문만 정리했다\n"
    assert _verify_ids_in(pd, edited) == ["F-001", "F-002"]      # 행은 그대로 둔다
    third.path.write_text(edited, encoding="utf-8", newline="")

    harvested = pd.harvest_ticket_copy(copy_path=third.path, cwd=slot, pm_home=pm_home)

    # 자리표시자로 남은 F-001 은 이번 라운드의 '선언 없음' 이라 앞 라운드 빈틈 보고를 덮는다.
    assert harvested.changed is True
    template = _verify_template_of(pd, pm_home, "T-7034")
    assert template.missing == ("F-001",) and template.gap == ()
    # 프리필 값 그대로 재선언된 F-002 는 이번 라운드 선언으로 올라간다(확인 커서 재개방).
    assert [source for source, _row in template.machine_rows] == [3]
    capsys.readouterr()
    assert _verify_template_rc(pd, pm_home, monkeypatch, "T-7034") == 1
    err = capsys.readouterr().err
    assert "verify 행이 없는 accepted finding" in err and "F-001" in err


def test_harvest_refuses_a_round_that_deleted_a_seeded_verify_row(pd, rounds_env, monkeypatch):
    """시드가 실은 행을 지운 산출은 회수되지 않는다(산출로 범위를 좁힐 수 없다).

    거부는 파괴적이지 않다 — board 라운드 bytes 도 slot run-dir 도 그대로라, 행을 되돌린 사본을
    같은 경로로 다시 회수하면 통과한다.
    """
    pm_home, slot, tickets, _sync = rounds_env
    third, seed_three = _gap_round_then_prepare_next(
        pd, pm_home, slot, tickets, "T-7035",
    )
    reserved_before = third.board_path.read_bytes()
    produced = _without_verify_block(pd, seed_three) + "\n## 메모\n- 산문만 정리했다\n"
    third.path.write_text(produced, encoding="utf-8", newline="")

    with pytest.raises(pd.DelegateError, match="verify 행을 지웠습니다"):
        pd.harvest_ticket_copy(copy_path=third.path, cwd=slot, pm_home=pm_home)

    assert third.board_path.read_bytes() == reserved_before
    assert third.path.read_text(encoding="utf-8") == produced
    assert third.run_dir.exists()
    assert _harvest_rc(pd, monkeypatch, pm_home, slot, third.path) == 1

    third.path.write_text(
        _fill_machine_verifiable(pd, seed_three, 1) + "\n## 메모\n- 행을 되돌렸다\n",
        encoding="utf-8", newline="",
    )
    assert pd.harvest_ticket_copy(
        copy_path=third.path, cwd=slot, pm_home=pm_home,
    ).changed is True
    assert not third.run_dir.exists()


def test_harvest_runs_the_declared_command_and_refuses_a_stale_expected_value(
    pd, rounds_env, monkeypatch,
):
    """이번 라운드가 선언한 기계 검증 행은 회수가 실제로 돌린다(실 subprocess).

    관측이 기대와 다르면 거부다 — 낡은 기대값이 board 에 착지하면 PM 의 기계 확인이 그 값으로
    막히고 라운드 파일은 회수 뒤 불변이라 되돌릴 정식 수단이 없다.
    """
    pm_home, slot, tickets, _sync = rounds_env
    third, seed_three = _gap_round_then_prepare_next(
        pd, pm_home, slot, tickets, "T-7037",
    )
    reserved_before = third.board_path.read_bytes()
    stale = [
        {"id": "F-001", "machine_verifiable": True,
         "command": _report_command("1 passed"), "expected": "2 passed",
         "before": "1 failed", "reason": ""},
        {"id": "F-002", "machine_verifiable": True,
         "command": _report_command("2 passed"), "expected": "2 passed",
         "before": "1 failed", "reason": ""},
    ]
    third.path.write_text(
        _without_verify_block(pd, seed_three) + "\n" + _verify_fence(pd, stale),
        encoding="utf-8", newline="",
    )

    with pytest.raises(pd.DelegateError, match="관측이 기대와 다릅니다") as caught:
        pd.harvest_ticket_copy(copy_path=third.path, cwd=slot, pm_home=pm_home)

    # 거부 사유가 관측값을 그대로 싣는다(라운드 파일에는 기입하지 않는다).
    assert "F-001" in str(caught.value) and "rc=0" in str(caught.value)
    assert "1 passed" in str(caught.value)
    assert third.board_path.read_bytes() == reserved_before
    assert third.run_dir.exists()
    assert _harvest_rc(pd, monkeypatch, pm_home, slot, third.path) == 1
    assert _verify_block_bytes(pd, third.board_path.read_text(encoding="utf-8")) == (
        _verify_block_bytes(pd, seed_three)
    )   # 관측값 기입 0 — 예약 골격 그대로다

    # 기대값을 실측으로 갱신한 사본은 같은 경로로 회수된다.
    fixed = [dict(stale[0], expected="1 passed"), stale[1]]
    third.path.write_text(
        _without_verify_block(pd, seed_three) + "\n" + _verify_fence(pd, fixed),
        encoding="utf-8", newline="",
    )
    assert pd.harvest_ticket_copy(
        copy_path=third.path, cwd=slot, pm_home=pm_home,
    ).changed is True
    template = _verify_template_of(pd, pm_home, "T-7037")
    assert [source for source, _row in template.machine_rows] == [3, 3]


def test_harvest_runs_every_accepted_reviewer_contract_including_design_axis(
    pd, rounds_env,
):
    """accepted finding은 design_change 축이어도 reviewer 지정 테스트를 실행한다."""
    pm_home, slot, tickets, _sync = rounds_env
    spec_path = _write_spec(tickets, "T-7038", rounds=("code-reviewer", "developer"))
    _harvest_review_round(
        pd, pm_home, slot, "T-7038", ["F-001", "F-002"], design_change_ids=("F-002",),
    )
    _append_disposition(pd, spec_path, [
        # 설계 축 finding 의 수락은 선행 권위(wikilink)를 요구한다 — 그 규칙은 종전 그대로다.
        {"id": fid, "decision": "accepted", "reason": "PM 수락",
         "scope": f"{fid} 범위",
         "prerequisite": "[[T-0805]]" if fid == "F-002" else ""}
        for fid in ("F-001", "F-002")
    ])
    plan = pd.prepare_ticket_copy(
        ticket="T-7038", role="developer", cwd=slot, pm_home=pm_home,
    )
    seed = plan.path.read_text(encoding="utf-8")
    rows = [
        {"id": "F-001", "machine_verifiable": False, "command": "",
         "expected": "사람 판단 필요", "before": "", "reason": "design-judgment"},
        {"id": "F-002", "machine_verifiable": True,
         "command": _report_command("설계 축은 돌지 않는다"), "expected": "2 passed",
         "before": "1 failed", "reason": ""},
    ]
    plan.path.write_text(
        _without_verify_block(pd, seed) + "\n" + _verify_fence(pd, rows),
        encoding="utf-8", newline="",
    )

    reserved = plan.board_path.read_bytes()
    with pytest.raises(
        pd.TerminalFixHarvestError,
        match="verify F-002.*관측이 기대와 다릅니다",
    ):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)
    assert plan.board_path.read_bytes() == reserved
    assert plan.run_dir.exists()


def _pm_owned_audit_contract() -> dict[str, str]:
    """F-003 형상 — adopter PM 홈 절대경로 감사라 repo test target이 아니다."""
    return {
        "location": (
            "/home/smahn/workspace/reference/project_manager/.project_manager/.local/"
            "review_rounds.json"
        ),
        "failure": "PM-owned legacy 장부와 current-truth 감사가 끝나지 않음",
        "design": "PM이 같은 fix 단계에서 ADR·문서·local ledger를 일회 정리",
        "test": (
            "adopter PM 홈의 두 current ledger와 ADR/index/architecture/domain을 "
            "종결 감사해 legacy resolution 합계 0을 기록한다"
        ),
        "command": python_argv_command("-c", "print(0)"),
        "expected": "0",
    }


def _prepare_single_finding_fix(
    pd, pm_home: Path, slot: Path, tickets: Path, *, ticket: str,
    finding_id: str, contract: dict[str, str], scope: str, verify_row: dict,
):
    spec_path = _write_spec(
        tickets, ticket, rounds=("code-reviewer", "developer"),
    )
    _harvest_review_round(
        pd, pm_home, slot, ticket, [finding_id],
        fix_contracts={finding_id: contract},
    )
    _append_disposition(pd, spec_path, [{
        "id": finding_id, "decision": "accepted", "reason": "PM 수락",
        "scope": scope, "prerequisite": "",
    }])
    plan = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
    )
    seed = plan.path.read_text(encoding="utf-8")
    plan.path.write_text(
        _without_verify_block(pd, seed) + "\n" + _verify_fence(pd, [verify_row]),
        encoding="utf-8", newline="",
    )
    return plan


def test_pm_owned_absolute_audit_contract_skips_developer_target_and_command(
    pd, rounds_env, monkeypatch,
):
    pm_home, slot, tickets, _sync = rounds_env
    contract = _pm_owned_audit_contract()
    row = {
        "id": "F-003", "machine_verifiable": False, "command": "",
        "expected": "legacy resolution 합계 0과 current-truth 정렬을 PM이 기록한다",
        "before": "", "reason": pd.PM_REVIEW_VERIFY_PM_OWNED_REASON,
    }
    plan = _prepare_single_finding_fix(
        pd, pm_home, slot, tickets, ticket="T-7041", finding_id="F-003",
        contract=contract, scope="pm-owned: ADR·문서·local ledger", verify_row=row,
    )
    real_run = pd._run_required_test
    calls: list[str] = []

    def record_targeted(command, expected, *, cwd):
        calls.append(command)
        return real_run(command, expected, cwd=cwd)

    monkeypatch.setattr(pd, "_run_required_test", record_targeted)
    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
    )
    assert result.changed is True
    assert contract["command"] not in calls


@pytest.mark.parametrize("scope,reason", (
    ("F-003 developer 범위", "pm-owned"),
    ("pm-owned: ADR·문서·local ledger", "design-judgment"),
))
def test_pm_owned_scope_and_false_verify_must_match_loudly(
    pd, rounds_env, scope, reason,
):
    pm_home, slot, tickets, _sync = rounds_env
    suffix = "7042" if reason == "pm-owned" else "7043"
    row = {
        "id": "F-003", "machine_verifiable": False, "command": "",
        "expected": "PM 감사 완료 기준 실값", "before": "", "reason": reason,
    }
    plan = _prepare_single_finding_fix(
        pd, pm_home, slot, tickets, ticket=f"T-{suffix}", finding_id="F-003",
        contract=_pm_owned_audit_contract(), scope=scope, verify_row=row,
    )
    with pytest.raises(pd.TerminalFixHarvestError, match="PM-owned"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


def test_developer_owned_contract_without_repo_test_target_is_still_rejected(
    pd, rounds_env,
):
    pm_home, slot, tickets, _sync = rounds_env
    contract = dict(
        _fix_contract("F-001"),
        test="산문 회귀만 추가하고 repo-relative 테스트 파일은 지정하지 않는다",
    )
    row = {
        "id": "F-001", "machine_verifiable": False, "command": "",
        "expected": "사람 판단 필요", "before": "", "reason": "design-judgment",
    }
    plan = _prepare_single_finding_fix(
        pd, pm_home, slot, tickets, ticket="T-7044", finding_id="F-001",
        contract=contract, scope="F-001 developer 범위", verify_row=row,
    )
    with pytest.raises(
        pd.TerminalFixHarvestError, match="repo-relative 테스트 대상",
    ):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


def test_harvest_refuses_a_verify_command_outside_the_safety_boundary(pd, rounds_env):
    """역방향 — 금지 토큰 커맨드는 종전대로 거부다(실행 전 · 파서 경계 그대로)."""
    pm_home, slot, tickets, _sync = rounds_env
    third, seed_three = _gap_round_then_prepare_next(
        pd, pm_home, slot, tickets, "T-7039",
    )
    rows = [
        {"id": "F-001", "machine_verifiable": True,
         "command": _report_command("2 passed") + " | tee out.txt",
         "expected": "2 passed", "before": "1 failed", "reason": ""},
        {"id": "F-002", "machine_verifiable": True,
         "command": _report_command("2 passed"), "expected": "2 passed",
         "before": "1 failed", "reason": ""},
    ]
    third.path.write_text(
        _without_verify_block(pd, seed_three) + "\n" + _verify_fence(pd, rows),
        encoding="utf-8", newline="",
    )

    with pytest.raises(pd.DelegateError, match="금지 토큰"):
        pd.harvest_ticket_copy(copy_path=third.path, cwd=slot, pm_home=pm_home)

    assert third.run_dir.exists()
    assert not (out := slot / "out.txt").exists(), f"셸 해석이 열렸다: {out}"


def test_harvest_of_a_round_without_verify_rows_is_unchanged(pd, rounds_env):
    """역방향 — verify 행이 없는 라운드(최초 구현·리뷰 역할)의 회수는 종전 그대로다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7040", rounds=("architect", "developer"))
    architect = pd.prepare_ticket_copy(
        ticket="T-7040", role="architect", cwd=slot, pm_home=pm_home,
    )
    architect.path.write_text(
        architect.path.read_text(encoding="utf-8") + "\n## 메모\n- 실산출\n",
        encoding="utf-8", newline="",
    )
    assert pd.harvest_ticket_copy(
        copy_path=architect.path, cwd=slot, pm_home=pm_home,
    ).changed is True

    first = pd.prepare_ticket_copy(
        ticket="T-7040", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert pd.PM_REVIEW_VERIFY_BLOCK not in first.path.read_text(encoding="utf-8")
    first.path.write_text(
        first.path.read_text(encoding="utf-8") + "\n## 메모\n- 실산출\n",
        encoding="utf-8", newline="",
    )

    assert pd.harvest_ticket_copy(
        copy_path=first.path, cwd=slot, pm_home=pm_home,
    ).changed is True


def test_filled_fix_round_after_a_gap_report_stays_green(pd, rounds_env, monkeypatch, capsys):
    """역방향 — 시드가 요구한 행을 전부 채운 fix 라운드는 영향받지 않는다(rc=0·잔여 0)."""
    pm_home, slot, tickets, _sync = rounds_env
    third, seed_three = _gap_round_then_prepare_next(
        pd, pm_home, slot, tickets, "T-7036",
    )
    rows = [
        {"id": "F-001", "machine_verifiable": True,
         "command": _report_command("1 passed"), "expected": "1 passed",
         "before": "1 failed", "reason": ""},
        {"id": "F-002", "machine_verifiable": True,
         "command": _report_command("2 passed"), "expected": "2 passed",
         "before": "1 failed", "reason": ""},
    ]
    third.path.write_text(
        _without_verify_block(pd, seed_three) + "\n" + _verify_fence(pd, rows),
        encoding="utf-8", newline="",
    )

    harvested = pd.harvest_ticket_copy(copy_path=third.path, cwd=slot, pm_home=pm_home)

    assert harvested.changed is True
    template = _verify_template_of(pd, pm_home, "T-7036")
    assert template.missing == () and template.gap == () and template.stale == ()
    assert [source for source, _row in template.machine_rows] == [3, 3]
    capsys.readouterr()
    assert _verify_template_rc(pd, pm_home, monkeypatch, "T-7036") == 0


# ── 경로 예산 · 권한 표면 ─────────────────────────────────────────────────

def test_run_dir_path_has_no_role_segment_and_fits_budget(pd):
    """`<role>` 세그먼트 제거로 Windows MAX_PATH 여유가 늘었다(경로 예산 회귀)."""
    relative = pd._ticket_copy_relative_path(
        "C-T-2001", "a" * 32, "T-2001", "01-code-reviewer.md")
    text = relative.as_posix()
    assert "code-reviewer/" not in text.rsplit("/", 1)[0]
    assert len(text) <= 120
    # 이전 레이아웃(`<ticket>/<role>/<run>/ticket-<ticket>.md`)보다 짧다.
    legacy = (
        pd.TICKET_COPY_REL_ROOT / "T-2001" / "code-reviewer" / ("a" * 32)
        / "ticket-T-2001.md"
    ).as_posix()
    assert len(text) < len(legacy)


def _round_write_scope_of(pd, round_plan):
    """그 라운드 실행이 여는 쓰기 자리 — 엔진 seam 을 그대로 쓴다(좌표 재조립 금지)."""
    return pd._round_write_scope(round_plan, pd.INTERNAL_REVIEW_ROLE)


def _plan_for(pd, tmp_path: Path, ticket: str, run_hex: str):
    run_dir = tmp_path / "worktree" / pd.TICKET_COPY_REL_ROOT / ticket / run_hex
    run_dir.mkdir(parents=True)
    copy = run_dir / "01-code-reviewer.md"
    copy.write_text("## 리뷰 (code-reviewer · 2026-08-18)\n", encoding="utf-8", newline="\n")
    return copy


def test_codex_reviewer_opens_the_legacy_row_run_dir(pd, monkeypatch, tmp_path):
    """묶음 키 없는 옛 행 형상(`<루트>/<티켓>/<run>/`)도 그 run-dir 전체를 그대로 연다."""
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    copy = _plan_for(pd, tmp_path, "T-2001", "a" * 32)
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "code-reviewer", tmp_path / "worktree", "review",
        cluster_run_dir=copy.parent,
    )
    try:
        assert argv[argv.index("-s") + 1] == "workspace-write"
        assert argv[argv.index("--add-dir") + 1] == str(copy.parent)
        assert argv[argv.index("-C") + 1] == str(read_tmp.path)
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


def test_codex_reviewer_opens_the_whole_cluster_run_dir(
        pd, rounds_env, monkeypatch, tmp_path):
    """실 준비 산출 대조 — `--add-dir` 값이 `ClusterCopyPlan.run_dir` 과 정확히 같다.

    새 layout 에서 쓰기 허용 범위는 **묶음 run-dir 전체**다. 티켓 자리 하나(`<run>/<티켓>/`)만
    열면 같은 run 의 다른 자리가 read-only 라 묶음 라운드가 산출을 못 쓴다.
    """
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7101")
    _write_spec(tickets, "T-7102")
    _declare_rounds(
        tickets, ["T-7101", "T-7102"], ("code-reviewer",), cluster="C-wave")
    plan = pd.prepare_cluster_copy(
        cluster="C-wave", tickets=("T-7101", "T-7102"), role="code-reviewer",
        cwd=slot, pm_home=pm_home,
    )
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))

    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "code-reviewer", slot, "review",
        cluster_run_dir=_round_write_scope_of(pd, plan.rounds[0]),
    )

    try:
        opened = argv[argv.index("--add-dir") + 1]
        assert opened == str(plan.run_dir)
        # 자리 하나가 아니라 run 전체다 — 두 티켓 자리가 그 아래 산다.
        assert sorted(item.name for item in Path(opened).iterdir()) == [
            "T-7101", "T-7102"]
        assert {str(round_plan.run_dir) for round_plan in plan.rounds} != {opened}
        # 라운드 좌표는 준비가 실은 값 그대로다(자리 경로 역산이 아니다).
        for round_plan in plan.rounds:
            assert round_plan.cluster_run_dir == plan.run_dir
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


def test_a_plan_without_the_run_dir_coordinate_is_loud(pd, tmp_path):
    """좌표 없는 계획으로 리뷰 실행에 들어가면 쓰기 자리를 조용히 좁히지 않고 멈춘다."""
    plan = pd.TicketCopyPlan(
        tmp_path / "slot" / "01-code-reviewer.md", tmp_path / "slot", "T-7103",
        pd.INTERNAL_REVIEW_ROLE, 1, tmp_path / "board" / "01-code-reviewer.md",
        "d" * 32, "F-001",
    )

    with pytest.raises(pd.DelegateError, match="묶음 run-dir 좌표"):
        pd._round_write_scope(plan, pd.INTERNAL_REVIEW_ROLE)
    # 라운드 준비가 없거나 리뷰 실행이 아니면 열 자리 자체가 없다(종전 형상).
    assert pd._round_write_scope(plan, "developer") is None
    assert pd._round_write_scope(None, pd.INTERNAL_REVIEW_ROLE) is None


@pytest.mark.parametrize("harness,model", [("claude", "sonnet"), ("opencode", "prov/m")])
def test_non_codex_reviewer_warns_and_keeps_selected_target(
        pd, monkeypatch, tmp_path, capsys, harness, model):
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    copy = _plan_for(pd, tmp_path, "T-2002", "b" * 32)
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        harness, model, None, "code-reviewer", tmp_path / "worktree", "review",
        cluster_run_dir=copy.parent,
    )
    try:
        assert argv[0] == harness
        if harness == "opencode":
            assert argv[argv.index("--agent") + 1] == "code-reviewer"
        warning = capsys.readouterr().err
        assert "단일-path write 격리" in warning
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


def test_all_prompt_wires_get_the_same_round_preamble(pd, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = pd.TicketCopyPlan(
        run_dir / "01-developer.md", run_dir, "T-3001", "developer", 1,
        tmp_path / "board" / "01-developer.md", "c" * 32, "",
    )
    note = pd._ticket_copy_preamble(plan)
    assert str(plan.path) in note
    assert pd.TICKET_COPY_SPEC_NAME in note and pd.TICKET_COPY_ROUNDS_DIRNAME in note
    for prompt in ("full payload", "resume delta", "fallback"):
        assert pd._with_ticket_copy_preamble(prompt, plan).startswith(note)
    # 이미 붙은 프롬프트에 두 번 붙지 않는다.
    once = pd._with_ticket_copy_preamble("full payload", plan)
    assert pd._with_ticket_copy_preamble(once, plan) == once


# ── 삭제 목록 부재 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("symbol", DELETED_SYMBOLS)
def test_single_file_container_devices_are_gone(pd, symbol):
    assert not hasattr(pd, symbol), f"{symbol} 이 아직 살아 있다 — 삭제가 절반만 됐다"


def test_removed_cli_surface_is_gone(pd):
    parser = pd.build_subcommand_parser("ticket")
    text = parser.format_help()
    assert "seal-backfill" not in text
    assert "--transfer-from" not in text
    assert "--capability-stdin" not in text
    for command in ("prepare", "harvest", "copies"):
        assert command in text


def test_ledger_relocated_to_delegate_rounds(pd, tmp_path):
    assert pd.DELEGATE_ROUNDS_LEDGER_REL_PATH.name == "delegate-rounds.jsonl"
    assert pd._delegate_rounds_ledger_path(tmp_path) == (
        tmp_path.resolve() / pd.DELEGATE_ROUNDS_LEDGER_REL_PATH
    )


def test_load_board_is_cached_across_calls_for_ledger_row_validation(pd):
    """T-0787 — `_load_board()` cache=True 회귀: 장부 행별 board 재-import 를 막는다.

    이전엔 `cache=True` 를 안 넘겨 `_delegate_rounds_ledger_row`(행별 ID 검증)가 호출될 때마다
    board.py 를 재실행했다(실측: 79행 장부에서 `_load_board` ncalls 79·cumtime 0.632s). 같은
    프로세스 안에서 동일 파일을 캐시 없이 반복 로드하면 매번 새 module 객체가 생기므로, 캐시가
    걸리면 두 호출이 **같은 객체**를 반환해야 한다(시간 단언은 플래키라 identity 로 대신 고정).
    """
    first = pd._load_board()
    second = pd._load_board()
    assert first is second


def test_role_sets_match_the_rounds_seam(pd):
    """역할 집합의 단일 진실은 seam 이다 — 라벨 사본은 두지 않는다([[ADR-0090]] R4 에서 삭제)."""
    rounds_module = pd._load_ticket_rounds()
    assert not hasattr(pd, "EXTERNAL_REVIEW_SECTION_LABEL")
    assert set(pd.TICKET_COPY_ROLES) == set(rounds_module.ROLES)


# ── 거부된 준비는 board 를 건드리지 않는다 ([[T-0750]] 리뷰 F-001) ─────────

def _board_side_effects(pm_home: Path, ticket: str) -> tuple[list[str], list[dict]]:
    directory = _rounds_dir(pm_home, ticket)
    rounds = sorted(item.name for item in directory.iterdir()) if directory.is_dir() else []
    return rounds, [row for row in _ledger_rows(pm_home) if row["ticket"] == ticket]


def test_ignore_rejection_leaves_no_board_round_or_ledger_row(pd, rounds_env):
    """ignore 규칙 미검증 슬롯의 거부는 예약 **앞**이라 board 부작용이 0이다.

    뒤에 두면 회수 불가능한 고아 라운드(장부 행이 없어 harvest 가 거부한다)가 남고 순번 하나가
    영구 소모된다.
    """
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7200")
    assert _git(slot, "rm", "-q", "--cached", ".project_manager/.gitignore").returncode == 0

    with pytest.raises(pd.DelegateError, match="untracked"):
        pd.prepare_ticket_copy(
            ticket="T-7200", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert _board_side_effects(pm_home, "T-7200") == ([], [])
    assert sync_log == []


@pytest.mark.parametrize(
    ("ticket", "role", "pattern"),
    [
        ("T-7201", "reviewer-not-a-role", "미지원 역할"),
        ("T-7299", "developer", "ticket not found"),
    ],
    ids=("role", "missing-ticket"),
)
def test_pre_reservation_rejections_have_no_board_side_effect(
        pd, rounds_env, ticket, role, pattern):
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7201")
    with pytest.raises(pd.DelegateError, match=pattern):
        pd.prepare_ticket_copy(
            ticket=ticket, role=role, cwd=slot, pm_home=pm_home,
        )
    assert _board_side_effects(pm_home, ticket) == ([], [])
    assert sync_log == []


def test_done_ticket_rejection_has_no_board_side_effect(pd, rounds_env):
    pm_home, slot, tickets, sync_log = rounds_env
    done = tickets.parent / "done"
    done.mkdir()
    (done / "T-7202-rounds.md").write_text(
        _spec_text("T-7202", status="done"), encoding="utf-8", newline="\n",
    )
    with pytest.raises(pd.DelegateError, match="open/claimed 또는 draft×architect"):
        pd.prepare_ticket_copy(
            ticket="T-7202", role="developer", cwd=slot, pm_home=pm_home,
        )
    assert _board_side_effects(pm_home, "T-7202") == ([], [])
    assert sync_log == []


# ── 무편집 판정은 시점에 의존하지 않는다 ([[T-0750]] 리뷰 F-002) ───────────

def _reviewer_output(pd, seed_text: str, finding_id: str) -> str:
    block = json.dumps({
        "version": pd.PM_REVIEW_VERSION,
        "findings": [{
            "id": finding_id, "class": "implementation-defect", "severity": "must-fix",
            "authority": "[[ADR-0090]] §경계", "evidence": "probe rc=1",
            "recommendation": f"{finding_id}만 수정",
            "fix_contract": _fix_contract(finding_id), "design_change": False,
        }],
        "confirmations": [],
    }, ensure_ascii=False)
    return (
        seed_text.partition("\n")[0]
        + f"\n\n## must-fix\n- {finding_id}\n\n## 판정\n판정: 반려 · finding 1건(must-fix 1건)\n\n"
        + f"```{pd.PM_REVIEW_BLOCK}\n{block}\n```\n"
    )


def test_parallel_reviewer_rounds_keep_the_untouched_seed_unharvested(
        pd, rounds_env, capsys):
    """같은 역할 2라운드 병렬(설계가 명시 허용) — 앞 라운드 회수가 뒤 라운드 판정을 뒤집지 않는다.

    시드를 회수 **시점**에 재렌더해 비교하면 프리필 입력이 그 사이 바뀌어, 손대지 않은 슬롯
    파일이 '산출 있음'으로 회수된다(board 커밋 + run 닫힘). 판정 입력은 예약이 쓴 board 라운드
    bytes 여야 한다.
    """
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7203", rounds=("code-reviewer", "code-reviewer"))
    first = pd.prepare_ticket_copy(
        ticket="T-7203", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    second = pd.prepare_ticket_copy(
        ticket="T-7203", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    assert (first.ordinal, second.ordinal) == (1, 2)
    untouched = second.path.read_bytes()

    first.path.write_text(
        _reviewer_output(pd, first.path.read_text(encoding="utf-8"), "F-001"),
        encoding="utf-8", newline="",
    )
    assert pd.harvest_ticket_copy(
        copy_path=first.path, cwd=slot, pm_home=pm_home,
    ).changed is True
    capsys.readouterr()

    assert second.path.read_bytes() == untouched
    result = pd.harvest_ticket_copy(
        copy_path=second.path, cwd=slot, pm_home=pm_home,
    )
    assert result.changed is False
    assert "산출 없음" in capsys.readouterr().err
    assert second.run_dir.exists()
    assert second.board_path.read_bytes() == untouched
    assert [paths for _message, paths in sync_log] == [[first.board_path]]


def test_slot_write_failure_after_the_reservation_names_the_leftover_round(
    pd, rounds_env, monkeypatch,
):
    """예약 뒤 실패는 board 에 라운드를 남긴다 — 진단이 그 좌표와 이후 상태를 말한다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7216", rounds=("developer", "developer"))

    def _boom(*_args, **_kwargs):
        raise OSError("디스크 가득")

    monkeypatch.setattr(pd, "_write_exclusive_file", _boom)
    with pytest.raises(pd.DelegateError) as caught:
        pd.prepare_ticket_copy(
            ticket="T-7216", role="developer", cwd=slot, pm_home=pm_home,
        )

    message = str(caught.value)
    assert "티켓 라운드 사본 생성 실패" in message
    assert "01-developer.md" in message and "순번 1" in message
    assert "산출 없음" in message and "다음 순번" in message
    # 예약은 되돌리지 않는다(보상 삭제는 순번 빈틈을 만든다) — 진단이 말한 그대로다.
    assert [item.name for item in _rounds_dir(pm_home, "T-7216").iterdir()] == [
        "01-developer.md"]
    monkeypatch.undo()
    plan = pd.prepare_ticket_copy(
        ticket="T-7216", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert plan.ordinal == 2


def test_harvest_calls_the_board_commit_seam_directly_and_is_loud_without_it(
    pd, rounds_env, monkeypatch,
):
    """부분 동기 사본에서 board 커밋만 조용히 빠진 rc0 을 만들지 않는다(이름 폴백 없음)."""
    source = PM_DELEGATE.read_text(encoding="utf-8")
    assert "board._rounds_mutation_sync_paths(" in source
    assert "_growth_mutation_sync_paths" not in source
    assert 'getattr(board, "_rounds_mutation_sync_paths"' not in source

    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7217")
    plan = pd.prepare_ticket_copy(
        ticket="T-7217", role="developer", cwd=slot, pm_home=pm_home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n산출\n",
        encoding="utf-8", newline="",
    )
    original = pd._load_board_for_repo

    def _board_without_the_seam(repo):
        board = original(repo)
        del board._rounds_mutation_sync_paths      # 이름이 갈린 사본 재현
        return board

    monkeypatch.setattr(pd, "_load_board_for_repo", _board_without_the_seam)
    with pytest.raises(AttributeError):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


def test_pending_previous_round_is_not_a_prefill_source(pd, rounds_env, capsys):
    """미회수(pending) 앞 라운드는 자리표시자뿐이라 프리필 공급원이 아니다 ([[T-0750]] 리뷰 F-006).

    빼지 않으면 정상 경로(앞 라운드 진행 중)에서 '강등' 경고가 나가 진짜 이상 신호를 덮는다.
    규칙은 사이드카 seam 하나가 소유한다 — pm_delegate 는 자기 사본을 두지 않는다.
    """
    assert not hasattr(pd, "_previous_round_of_role")
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7204", rounds=("code-reviewer", "code-reviewer"))
    pd.prepare_ticket_copy(
        ticket="T-7204", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    capsys.readouterr()
    second = pd.prepare_ticket_copy(
        ticket="T-7204", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    warning = capsys.readouterr().err
    assert "강등" not in warning
    block_text = second.path.read_text(encoding="utf-8").partition(
        f"```{pd.PM_REVIEW_BLOCK}"
    )[2].replace(" ", "")
    assert '"confirmations":[{"id":"F-NNN"' in block_text


# ── 회수 입력 검증 = 장부 인가 경로 전량 일치 ([[T-0750]] 리뷰 F-003) ──────

def _ledger_path(pm_home: Path) -> Path:
    return pm_home / ".project_manager" / ".local" / "delegate-rounds.jsonl"


def _append_raw_ledger_line(pm_home: Path, payload: str) -> None:
    path = _ledger_path(pm_home)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(payload + "\n")


def _rewrite_last_row(pm_home: Path, **changes) -> dict:
    rows = _ledger_rows(pm_home)
    row = dict(rows[-1])
    row.update(changes)
    _append_raw_ledger_line(
        pm_home, json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    return row


def test_harvest_refuses_a_row_whose_binding_does_not_match_the_path(pd, rounds_env):
    """장부 행의 (순번·역할)이 요청 경로와 어긋나면 회수하지 않는다 — run 도 지우지 않는다."""
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7205")
    plan = pd.prepare_ticket_copy(
        ticket="T-7205", role="developer", cwd=slot, pm_home=pm_home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n산출\n", encoding="utf-8", newline="",
    )
    _rewrite_last_row(pm_home, ordinal=2)

    with pytest.raises(pd.DelegateError, match="장부가 인가한 라운드 경로와 불일치"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)
    assert plan.run_dir.exists() and plan.path.exists()
    assert sync_log == []


def test_harvest_refuses_a_row_pointing_outside_or_above_the_run_dir(pd, rounds_env):
    """조작된 행이 run-dir 밖·상위를 가리켜도 거부한다 — `force_rmtree` 가 남의 자리를 지우지 않는다."""
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7206", rounds=("developer", "developer"))
    plan = pd.prepare_ticket_copy(
        ticket="T-7206", role="developer", cwd=slot, pm_home=pm_home,
    )
    sibling = pd.prepare_ticket_copy(
        ticket="T-7206", role="developer", cwd=slot, pm_home=pm_home,
    )
    above = plan.run_dir.parent / plan.path.name
    above.write_text("## 구현 보충 (developer · 2026-08-18)\n\n밀반입\n",
                     encoding="utf-8", newline="\n")
    _rewrite_last_row(pm_home, copy=str(above))

    with pytest.raises(pd.DelegateError, match="장부가 인가한 라운드 경로와 불일치"):
        pd.harvest_ticket_copy(copy_path=above, cwd=slot, pm_home=pm_home)
    # 형제 run 과 상위 디렉터리 어느 쪽도 사라지지 않는다.
    assert sibling.run_dir.exists() and plan.run_dir.exists() and above.exists()
    assert sync_log == []


def test_harvest_refuses_when_the_authorising_row_is_value_corrupt(pd, rounds_env):
    """행 값이 형식을 벗어나면 그 행은 장부에서 빠지고 회수는 '준비 기록 없음'으로 거부된다."""
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7207")
    plan = pd.prepare_ticket_copy(
        ticket="T-7207", role="developer", cwd=slot, pm_home=pm_home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n산출\n", encoding="utf-8", newline="",
    )
    # 준비 기록을 값 손상 행으로 덮는다(run_id 형식 위반) — 남는 인가 행이 없다.
    original = _ledger_rows(pm_home)[-1]
    broken = dict(original, run_id="not-a-run-id")
    _ledger_path(pm_home).write_text(
        json.dumps(broken, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8", newline="",
    )

    with pytest.raises(pd.DelegateError, match="준비 기록 없음"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)
    assert plan.run_dir.exists() and plan.board_path.exists()
    assert sync_log == []


def test_corrupt_ledger_lines_are_skipped_loudly_without_losing_other_runs(
        pd, rounds_env, capsys):
    """한 행의 손상이 다른 run 의 회수를 막지 않는다 — 건너뛰되 조용하지 않다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7208")
    plan = pd.prepare_ticket_copy(
        ticket="T-7208", role="developer", cwd=slot, pm_home=pm_home,
    )
    _append_raw_ledger_line(pm_home, "{ not json")
    _append_raw_ledger_line(pm_home, json.dumps({"ticket": "T-7208"}))
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n산출\n", encoding="utf-8", newline="",
    )

    assert pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
    ).changed is True
    warning = capsys.readouterr().err
    assert warning.count("delegate-rounds 장부 손상 행 건너뜀") >= 2


@pytest.mark.skipif(not posix_mode_supported(), reason="POSIX mode 왕복이 되는 환경 필요")
def test_ledger_is_owner_only(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7209")
    pd.prepare_ticket_copy(
        ticket="T-7209", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert stat.S_IMODE(_ledger_path(pm_home).stat().st_mode) == 0o600


# ── 리뷰 라운드 회수 내용 게이트 (fail-early) ─────────────────────────────────
#
# 판정 표면에 올릴 수 없는 리뷰 산출은 board 라운드가 되지 못한다. 픽스처는 실 board 트리·실
# 라운드 파일·실 `delegate-rounds.jsonl` 이고, 리뷰 블록은 **엔진 골격 렌더**에서 key·enum 을
# 받아 값만 채운다(스키마 재타이핑 0).


def _finding_values(pd, finding_id: str) -> dict:
    return {
        "id": finding_id,
        "class": pd.PM_REVIEW_CLASSES[0],
        "severity": pd.PM_REVIEW_SEVERITIES[0],
        "authority": "[[ADR-0090]] §경계",
        "evidence": f"{finding_id} probe rc=1",
        "recommendation": f"{finding_id}만 수정",
        "fix_contract": _fix_contract(finding_id),
        "design_change": False,
    }


def _confirmation_values(pd, finding_id: str) -> dict:
    return {
        "id": finding_id,
        "status": pd.PM_REVIEW_CONFIRMATION_STATES[0],
        "evidence": f"{finding_id} 회귀 rc=0",
    }


def _review_body(pd, role: str, finding_ids, confirmation_ids=()) -> str:
    """리뷰 라운드 본문 — 블록 key·fence 는 엔진 골격 렌더가 소유하고 값만 채운다."""
    confirmation_ids = list(confirmation_ids)
    skeleton = pd.render_pm_review_block_skeleton(role, confirmation_ids)
    payload = pd._pm_review_json_blocks(skeleton)[0].value
    finding_shape = payload["findings"][0]
    confirmation_shape = (
        payload["confirmations"][0] if payload["confirmations"]
        else {key: "" for key in pd.PM_REVIEW_CONFIRMATION_KEYS}
    )
    payload["findings"] = [
        dict(finding_shape, **_finding_values(pd, finding_id))
        for finding_id in finding_ids
    ]
    payload["confirmations"] = [
        dict(confirmation_shape, **_confirmation_values(pd, finding_id))
        for finding_id in confirmation_ids
    ]
    listed = "\n".join(f"- {finding_id}" for finding_id in finding_ids) or "- 없음"
    verdict = "반려" if finding_ids else "통과"
    return (
        f"## must-fix\n{listed}\n\n"
        f"## 판정\n판정: {verdict} · finding {len(finding_ids)}건"
        f"(must-fix {len(finding_ids)}건)\n\n"
        + pd._pm_review_block_text(payload)
    )


def _write_round_output(path: Path, body: str) -> str:
    """슬롯 라운드 파일에 산출을 쓴다 — 첫 줄 헤더는 그대로 둔다(라운드 규약)."""
    produced = path.read_text(encoding="utf-8").partition("\n")[0] + "\n\n" + body
    path.write_text(produced, encoding="utf-8", newline="")
    return produced


def _land_review_round(pd, pm_home: Path, slot: Path, ticket: str, body: str):
    """준비 → 산출 → 회수 한 사이클(정상 착지 경로)."""
    plan = pd.prepare_ticket_copy(
        ticket=ticket, role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(plan.path, body)
    result = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)
    assert result.changed is True
    return plan


def _land_round_bypassing_the_gate(
    pd, pm_home: Path, slot: Path, ticket: str, role: str, body: str,
):
    """회수 게이트 이전에 들어간 board 상태 재현 — 예약 라운드 파일을 직접 교체한다."""
    plan = pd.prepare_ticket_copy(
        ticket=ticket, role=role, cwd=slot, pm_home=pm_home,
    )
    pd._load_ticket_rounds().replace_round(
        plan.board_path,
        plan.board_path.read_text(encoding="utf-8").partition("\n")[0] + "\n\n" + body,
    )
    return plan


def _template_ids(pd, rendered: str) -> list[str]:
    """골격이 프리필한 판정 대상 ID — 블록은 엔진 파서로 읽는다."""
    return [
        row["id"]
        for row in pd._pm_review_json_blocks(rendered)[0].value["dispositions"]
    ]


def _fill_disposition_template(pd, rendered: str) -> str:
    """골격을 **그대로** 채운다 — key·순서·fence 는 엔진 렌더 그대로 두고 값만 채운다."""
    payload = pd._pm_review_json_blocks(rendered)[0].value
    assert "accepted" in pd.PM_REVIEW_DECISIONS
    for row in payload["dispositions"]:
        assert row["decision"] == "<accepted|rejected>"
        row["decision"] = "accepted"
        row["reason"] = f"PM {row['id']} 수락"
        row["scope"] = f"{row['id']} 허용 범위"
    return (
        f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _spec_and_rounds(pd, pm_home: Path, tickets: Path, ticket: str):
    rounds_module = pd._load_ticket_rounds()
    spec_path = tickets / f"{ticket}-rounds.md"
    spec_text = spec_path.read_text(encoding="utf-8")
    tickets_root = pm_home / ".project_manager" / "wiki" / "tickets"
    return spec_path, spec_text, rounds_module.load_rounds(
        tickets_root, ticket, ticket_text=spec_text,
    )


def _harvest_rc(pd, monkeypatch, pm_home: Path, slot: Path, copy_path: Path) -> int:
    """실 CLI rc — 소유 PM 홈 해소만 픽스처 좌표로 고정한다(판정은 엔진 그대로)."""
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    return pd._cmd_ticket(["harvest", "--copy", str(copy_path), "--cwd", str(slot)])


def test_redeclared_prior_finding_id_is_refused_at_harvest(
    pd, rounds_env, monkeypatch, capsys,
):
    """확인 라운드가 선행 finding ID 를 `findings` 에 다시 실으면 회수가 거부한다.

    재현 형상은 실측 사건 그대로다 — 같은 ID 를 `findings` 와 `confirmations` 양쪽에 기재.
    """
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7230", rounds=("code-reviewer", "code-reviewer"))
    first = _land_review_round(
        pd, pm_home, slot, "T-7230", _review_body(pd, "code-reviewer", ["F-007"]),
    )
    second = pd.prepare_ticket_copy(
        ticket="T-7230", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    reserved = second.board_path.read_bytes()
    _write_round_output(
        second.path, _review_body(pd, "code-reviewer", ["F-007"], ["F-007"]),
    )
    capsys.readouterr()

    rc = _harvest_rc(pd, monkeypatch, pm_home, slot, second.path)

    assert rc == 1
    assert "F-007" in capsys.readouterr().err
    # 거부가 산출을 파괴하지 않는다 — board bytes 불변 + run-dir 유지(재회수 가능).
    assert second.board_path.read_bytes() == reserved
    assert second.run_dir.exists() and second.path.exists()
    assert _ledger_rows(pm_home)[-1]["harvested_at"] is None
    assert [paths for _message, paths in sync_log] == [[first.board_path]]

    # `findings` 에만 다시 실은 형상도 같은 회수면에서 거부된다(전역 유일성 축).
    _write_round_output(second.path, _review_body(pd, "code-reviewer", ["F-007"]))
    assert _harvest_rc(pd, monkeypatch, pm_home, slot, second.path) == 1
    assert "finding ID 재선언: F-007" in capsys.readouterr().err
    assert second.board_path.read_bytes() == reserved

    # 규약대로 고친 사본은 **같은 경로로** 다시 회수된다 — 거부는 run 을 닫지 않는다.
    produced = _write_round_output(
        second.path, _review_body(pd, "code-reviewer", ["F-008"], ["F-007"]),
    )
    assert _harvest_rc(pd, monkeypatch, pm_home, slot, second.path) == 0
    assert second.board_path.read_text(encoding="utf-8") == produced
    assert not second.run_dir.exists()


def test_first_review_round_confirming_its_own_finding_is_refused_at_harvest(
    pd, rounds_env, monkeypatch, capsys,
):
    """선행 선언이 공집합인 첫 리뷰 라운드의 자기-확인도 같은 회수면에서 거부된다."""
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7231", rounds=("code-reviewer",))
    plan = pd.prepare_ticket_copy(
        ticket="T-7231", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    reserved = plan.board_path.read_bytes()
    _write_round_output(
        plan.path,
        _review_body(pd, "code-reviewer", ["F-001", "F-002"], ["F-001", "F-002"]),
    )
    capsys.readouterr()

    rc = _harvest_rc(pd, monkeypatch, pm_home, slot, plan.path)

    assert rc == 1
    assert "F-001, F-002" in capsys.readouterr().err
    assert plan.board_path.read_bytes() == reserved
    assert plan.run_dir.exists() and plan.path.exists()
    assert _ledger_rows(pm_home)[-1]["harvested_at"] is None
    assert sync_log == []

    # 선행 선언이 공집합이라 **어떤** 기존 ID 참조도 표면에 없다 — 자기-확인이 아닌 확인도 거부다.
    _write_round_output(
        plan.path, _review_body(pd, "code-reviewer", ["F-001"], ["F-050"]),
    )
    assert _harvest_rc(pd, monkeypatch, pm_home, slot, plan.path) == 1
    assert "confirmation 대상 finding 부재: F-050" in capsys.readouterr().err
    assert plan.board_path.read_bytes() == reserved


def test_the_two_refused_shapes_map_to_distinct_delta_branches(pd, rounds_env):
    """두 픽스처는 판정 표면의 **서로 다른** malformed 분기에 대응한다.

    게이트 이전에 들어간 라운드(회수면을 지나지 않은 board 상태)로 재현한다 — 회수면이 막는
    형상이 판정면에서 무엇이었는지가 이 대조의 값이다.
    """
    pm_home, slot, tickets, _sync = rounds_env

    _write_spec(tickets, "T-7232", rounds=("code-reviewer", "code-reviewer"))
    first = _land_review_round(
        pd, pm_home, slot, "T-7232", _review_body(pd, "code-reviewer", ["F-007"]),
    )
    _land_round_bypassing_the_gate(
        pd, pm_home, slot, "T-7232", "code-reviewer",
        _review_body(pd, "code-reviewer", ["F-007"], ["F-007"]),
    )
    _spec, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7232")
    with pytest.raises(pd.PMReviewError) as redeclared:
        pd.parse_pm_review_delta(spec_text, rounds)
    assert "티켓 안 finding ID 재선언: F-007" in str(redeclared.value)
    assert first.board_path.exists()

    _write_spec(tickets, "T-7233", rounds=("code-reviewer",))
    _land_round_bypassing_the_gate(
        pd, pm_home, slot, "T-7233", "code-reviewer",
        _review_body(pd, "code-reviewer", ["F-001"], ["F-001"]),
    )
    _spec2, spec_text2, rounds2 = _spec_and_rounds(pd, pm_home, tickets, "T-7233")
    with pytest.raises(pd.PMReviewError) as self_confirmed:
        pd.parse_pm_review_delta(spec_text2, rounds2)
    assert "confirmation이 선행 finding ID를 참조하지 않음: F-001" in str(
        self_confirmed.value
    )
    assert str(redeclared.value) != str(self_confirmed.value)


def test_confirmation_round_referencing_prior_ids_still_lands(
    pd, rounds_env, monkeypatch, capsys,
):
    """역방향 확인 — 규약대로 쓴 확인 라운드(기존 ID 는 확인만·신규는 새 ID)는 그대로 착지한다."""
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-7234", rounds=("code-reviewer", "code-reviewer"))
    _land_review_round(
        pd, pm_home, slot, "T-7234", _review_body(pd, "code-reviewer", ["F-007"]),
    )
    second = pd.prepare_ticket_copy(
        ticket="T-7234", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    produced = _write_round_output(
        second.path, _review_body(pd, "code-reviewer", ["F-008"], ["F-007"]),
    )
    capsys.readouterr()

    rc = _harvest_rc(pd, monkeypatch, pm_home, slot, second.path)

    assert rc == 0
    assert second.board_path.read_text(encoding="utf-8") == produced
    assert not second.run_dir.exists()          # 정상 회수 = run 닫힘
    assert _ledger_rows(pm_home)[-1]["harvested_at"] is not None
    assert [paths for _message, paths in sync_log][-1] == [second.board_path]


def test_confirmation_only_and_finding_zero_rounds_pass_the_gate(
    pd, rounds_env, monkeypatch, capsys,
):
    """역방향 — 확인 전용 라운드와 finding 0건 통과 라운드는 게이트를 그대로 지난다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(
        tickets, "T-7240",
        rounds=("code-reviewer", "code-reviewer", "code-reviewer"))
    _land_review_round(
        pd, pm_home, slot, "T-7240", _review_body(pd, "code-reviewer", ["F-001"]),
    )
    confirming = pd.prepare_ticket_copy(
        ticket="T-7240", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    produced = _write_round_output(
        confirming.path, _review_body(pd, "code-reviewer", [], ["F-001"]),
    )
    capsys.readouterr()

    assert _harvest_rc(pd, monkeypatch, pm_home, slot, confirming.path) == 0
    assert confirming.board_path.read_text(encoding="utf-8") == produced

    empty = pd.prepare_ticket_copy(
        ticket="T-7240", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    produced = _write_round_output(empty.path, _review_body(pd, "code-reviewer", []))

    assert _harvest_rc(pd, monkeypatch, pm_home, slot, empty.path) == 0
    assert empty.board_path.read_text(encoding="utf-8") == produced
    assert not empty.run_dir.exists()


def test_both_review_channels_refuse_redeclaration_with_the_same_verdict(
    pd, rounds_env, monkeypatch, capsys,
):
    """채널 파리티 — 같은 형상에 두 채널이 같은 사유를 내고 어느 쪽도 산출을 잃지 않는다."""
    pm_home, slot, tickets, sync_log = rounds_env
    external = pd._load_module_from_path(
        pm_home / ".project_manager" / "tools" / "additional_reviewer.py",
        "additional_reviewer.py", verifier=pd._verify_engine_rev,
    )
    board = _fixture_board(pd, pm_home, sync_log)
    rounds_module = pd._load_ticket_rounds()

    _write_spec(tickets, "T-7235", rounds=("code-reviewer", "code-reviewer"))
    _land_review_round(
        pd, pm_home, slot, "T-7235", _review_body(pd, "code-reviewer", ["F-007"]),
    )
    internal_round = pd.prepare_ticket_copy(
        ticket="T-7235", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    # 추가 리뷰어 채널의 선행 라운드도 같은 board 트리에 세운다(접두만 다른 같은 형상).
    # 그 채널은 슬롯 왕복이 아니라 additional_reviewer 엔진이 직접 예약한다.
    external_first = rounds_module.reserve_round(
        _rounds_dir(pm_home, "T-7235").parent.parent, "T-7235",
        pd.ADDITIONAL_REVIEWER_ROLE,
        content=rounds_module.render_round_header(
            pd.ADDITIONAL_REVIEWER_ROLE, today="2026-08-22",
        ) + "\n\n" + _review_body(pd, pd.ADDITIONAL_REVIEWER_ROLE, ["X-007"]),
        lock=board.board_lock(),
    )
    assert external_first.exists()

    reserved = internal_round.board_path.read_bytes()
    _write_round_output(
        internal_round.path, _review_body(pd, "code-reviewer", ["F-007"], ["F-007"]),
    )
    external_reply = _review_body(pd, pd.ADDITIONAL_REVIEWER_ROLE, ["X-007"], ["X-007"])
    capsys.readouterr()

    internal_rc = _harvest_rc(pd, monkeypatch, pm_home, slot, internal_round.path)
    internal_error = capsys.readouterr().err
    external_problem = external._reserve_additional_reviewer_round(
        "T-7235", external_reply, delegate=pd, rounds_module=rounds_module, board=board,
    )

    # 같은 사유 종류 — 채널 ID 접두만 빼면 문장이 bytes 로 같다.
    _spec, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7235")
    internal_problem = pd.review_harvest_problem(
        internal_round.path.read_text(encoding="utf-8"),
        ticket_text=spec_text, rounds=rounds, reviewer_role=pd.INTERNAL_REVIEW_ROLE,
    )
    assert external_problem is not None and internal_problem is not None
    assert internal_problem.replace("F-007", "<ID>") == external_problem.replace(
        "X-007", "<ID>",
    )
    # 같은 rc — 두 채널 다 거부이고, 거부가 산출을 파괴하지 않는다.
    assert internal_rc == 1
    assert internal_problem in internal_error
    assert internal_round.board_path.read_bytes() == reserved
    assert internal_round.run_dir.exists()
    assert _round_names(pm_home, "T-7235", pd.ADDITIONAL_REVIEWER_ROLE) == [
        external_first.name,
    ]


def _round_names(pm_home: Path, ticket: str, role: str) -> list[str]:
    return sorted(
        item.name for item in _rounds_dir(pm_home, ticket).iterdir()
        if item.stem.endswith(f"-{role}")
    )


# ── disposition-template ↔ review delta 정합 ──────────────────────────────────


def test_disposition_template_skeleton_is_accepted_by_review_delta(pd, rounds_env):
    """왕복 불변식 — 골격대로 채운 판정은 판정 표면이 그대로 수용한다."""
    pm_home, slot, tickets, _sync = rounds_env
    spec_path = _write_spec(tickets, "T-7236", rounds=("code-reviewer",))
    _land_review_round(
        pd, pm_home, slot, "T-7236",
        _review_body(pd, "code-reviewer", ["F-001", "F-002"]),
    )
    _path, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7236")

    rendered = pd.render_pm_review_disposition_template(spec_text, rounds, 1)

    prefilled = _template_ids(pd, rendered)
    assert prefilled == ["F-001", "F-002"]      # 정당한 신규 finding 은 빠지지 않는다.
    spec_path.write_text(
        spec_text + "\n" + _fill_disposition_template(pd, rendered),
        encoding="utf-8", newline="",
    )
    _path, filled_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7236")

    delta = pd.parse_pm_review_delta(filled_text, rounds)

    assert [finding.id for finding, _row in delta.accepted] == prefilled


def test_disposition_template_emits_no_skeleton_for_a_redeclaring_round(
    pd, rounds_env, capsys,
):
    """재선언 혼합 라운드에는 골격을 내지 않는다 — 부분 출력은 왕복을 복구하지 못한다.

    나머지 ID 만 실은 골격을 채워도 판정 표면은 같은 라운드를 통째로 거부한다(왕복 불변식은
    "골격을 채우면 수용" 아니면 "골격 미출력" 둘 중 하나로만 선다).
    """
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7237", rounds=("code-reviewer", "code-reviewer"))
    _land_review_round(
        pd, pm_home, slot, "T-7237", _review_body(pd, "code-reviewer", ["F-001"]),
    )
    _land_round_bypassing_the_gate(
        pd, pm_home, slot, "T-7237", "code-reviewer",
        _review_body(pd, "code-reviewer", ["F-001", "F-002"]),
    )
    _path, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7237")
    capsys.readouterr()

    with pytest.raises(pd.PMReviewError) as refused:
        pd.render_pm_review_disposition_template(spec_text, rounds, 2)

    assert refused.value.code == "malformed"
    assert "판정 골격을 내지 않습니다: F-001(티켓 전역 재선언)" in str(refused.value)
    # 부분 골격도, 그 부분 출력을 알리던 경고도 없다 — 채울 값 자체가 나오지 않는다.
    assert "F-002" not in str(refused.value)
    assert "제외한 finding" not in capsys.readouterr().err
    # 판정 표면도 같은 라운드를 막는다 — template 과 delta 가 같은 상태에 같은 판정을 낸다.
    with pytest.raises(pd.PMReviewError) as surface:
        pd.parse_pm_review_delta(spec_text, rounds)
    assert surface.value.code == "malformed"
    assert "티켓 안 finding ID 재선언: F-001" in str(surface.value)


def test_disposition_template_emits_no_skeleton_when_every_finding_is_refused(
    pd, rounds_env, capsys,
):
    """전량 재선언 라운드도 같은 판정이다 — 빈 골격도 부분 골격도 내지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7238", rounds=("code-reviewer", "code-reviewer"))
    _land_review_round(
        pd, pm_home, slot, "T-7238", _review_body(pd, "code-reviewer", ["F-001"]),
    )
    _land_round_bypassing_the_gate(
        pd, pm_home, slot, "T-7238", "code-reviewer",
        _review_body(pd, "code-reviewer", ["F-001"]),
    )
    _path, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7238")
    capsys.readouterr()

    with pytest.raises(pd.PMReviewError) as refused:
        pd.render_pm_review_disposition_template(spec_text, rounds, 2)

    assert refused.value.code == "malformed"
    assert "판정 골격을 내지 않습니다: F-001(티켓 전역 재선언)" in str(refused.value)
    assert "제외한 finding" not in capsys.readouterr().err


def test_disposition_template_emits_no_skeleton_for_a_self_confirming_round(
    pd, rounds_env, capsys,
):
    """자기-확인 혼합 라운드도 골격 미출력이다 — 표면이 그 라운드를 다른 축으로 막는다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-7239", rounds=("code-reviewer",))
    _land_round_bypassing_the_gate(
        pd, pm_home, slot, "T-7239", "code-reviewer",
        _review_body(pd, "code-reviewer", ["F-001", "F-002"], ["F-001"]),
    )
    _path, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7239")
    capsys.readouterr()

    with pytest.raises(pd.PMReviewError) as refused:
        pd.render_pm_review_disposition_template(spec_text, rounds, 1)

    assert refused.value.code == "malformed"
    assert "판정 골격을 내지 않습니다: F-001(같은 라운드 자기-확인)" in str(refused.value)
    assert "F-002" not in str(refused.value)
    assert "제외한 finding" not in capsys.readouterr().err
    with pytest.raises(pd.PMReviewError) as surface:
        pd.parse_pm_review_delta(spec_text, rounds)
    assert surface.value.code == "malformed"
    assert "confirmation이 선행 finding ID를 참조하지 않음: F-001" in str(surface.value)


def test_clean_confirmation_round_skeleton_is_still_accepted_by_review_delta(
    pd, rounds_env,
):
    """역방향 — 재선언·자기-확인이 없는 확인 라운드는 종전대로 골격을 내고 delta 가 수용한다."""
    pm_home, slot, tickets, _sync = rounds_env
    spec_path = _write_spec(tickets, "T-7240", rounds=("code-reviewer", "code-reviewer"))
    _land_review_round(
        pd, pm_home, slot, "T-7240", _review_body(pd, "code-reviewer", ["F-001"]),
    )
    _path, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7240")
    first = pd.render_pm_review_disposition_template(spec_text, rounds, 1)
    spec_path.write_text(
        spec_text + "\n" + _fill_disposition_template(pd, first),
        encoding="utf-8", newline="",
    )
    # 정상 확인 라운드 — 선행 F-001 은 `confirmations` 로만 참조하고 F-002 를 새로 낸다.
    _land_review_round(
        pd, pm_home, slot, "T-7240",
        _review_body(pd, "code-reviewer", ["F-002"], ["F-001"]),
    )
    _path, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7240")

    rendered = pd.render_pm_review_disposition_template(spec_text, rounds, 2)

    assert _template_ids(pd, rendered) == ["F-002"]
    spec_path.write_text(
        spec_text + "\n" + _fill_disposition_template(pd, rendered),
        encoding="utf-8", newline="",
    )
    _path, filled_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7240")

    delta = pd.parse_pm_review_delta(filled_text, rounds)

    assert [finding.id for finding, _row in delta.accepted] == ["F-002"]


def test_pre_change_confirmation_seed_stays_pending_through_an_unchanged_harvest(
    pd, rounds_env, monkeypatch, capsys,
):
    """업그레이드 창 — 문구를 바꾸기 전에 예약된 확인 라운드 시드도 산출 없음으로 남는다.

    board·슬롯 사본에 옛 문구 bytes 를 그대로 두고 실 CLI 회수를 태운다. 회수는 board 를 바꾸지
    않고(rc 0 · 산출 없음), 그 라운드는 pending 이라 판정 표면을 막지 않는다.
    """
    pm_home, slot, tickets, _sync = rounds_env
    spec_path = _write_spec(tickets, "T-7241", rounds=("code-reviewer", "code-reviewer"))
    _land_review_round(
        pd, pm_home, slot, "T-7241", _review_body(pd, "code-reviewer", ["F-001"]),
    )
    _path, spec_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7241")
    spec_path.write_text(
        spec_text + "\n" + _fill_disposition_template(
            pd, pd.render_pm_review_disposition_template(spec_text, rounds, 1),
        ),
        encoding="utf-8", newline="",
    )

    plan = pd.prepare_ticket_copy(
        ticket="T-7241", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    seeded = plan.board_path.read_text(encoding="utf-8")
    legacy = seeded.replace(
        pd.CONFIRM_ROUND_SCOPE_RULE, pd.LEGACY_CONFIRM_ROUND_SCOPE_RULES[0],
    )
    assert legacy != seeded and pd.CONFIRM_ROUND_SCOPE_RULE not in legacy
    pd._load_ticket_rounds().replace_round(plan.board_path, legacy)
    plan.path.write_text(legacy, encoding="utf-8", newline="")
    capsys.readouterr()

    assert _harvest_rc(pd, monkeypatch, pm_home, slot, plan.path) == 0

    assert "산출 없음" in capsys.readouterr().err
    assert plan.board_path.read_text(encoding="utf-8") == legacy
    assert plan.run_dir.exists()
    _path, filled_text, rounds = _spec_and_rounds(pd, pm_home, tickets, "T-7241")
    landed = next(item for item in rounds if item.ordinal == plan.ordinal)
    assert landed.pending is True

    delta = pd.parse_pm_review_delta(filled_text, rounds)

    assert [finding.id for finding, _row in delta.accepted] == ["F-001"]


# ── T-0841: 내부 위임 라운드 상한 (additional_reviewer.py:49-59 미러) ──────────────────

def _prepare_rc(
    pd, monkeypatch, pm_home: Path, slot: Path, ticket: str, role: str,
) -> int:
    """실 CLI rc — 소유 PM 홈 해소만 픽스처 좌표로 고정한다(판정은 엔진 그대로)."""
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    return pd._cmd_ticket(["prepare", "--ticket", ticket, "--role", role, "--cwd", str(slot)])


def _rounds_past_the_cap(pd, role: str) -> tuple[str, ...]:
    """그 역할 상한보다 한 건 넉넉한 예약 계획 — 장부 예산이 아니라 상한이 걸리는 픽스처다.

    예산 판정이 상한보다 먼저 발동하므로, 상한 축을 태우는 티켓의 장부는 상한 + 1 건을
    선언해야 한다(상한과 같으면 예산 소진이 먼저 나서 축이 갈린다).
    """
    return (role,) * (pd.DEFAULT_INTERNAL_ROUND_LIMITS[role] + 1)


def _prepare_n_rounds(pd, pm_home: Path, slot: Path, ticket: str, role: str, n: int):
    """직접 API 호출로 n 개 라운드를 예약한다(회수 없이 — pending 라운드도 카운트 대상)."""
    plan = None
    for _ in range(n):
        plan = pd.prepare_ticket_copy(
            ticket=ticket, role=role, cwd=slot, pm_home=pm_home,
        )
    return plan


def test_developer_round_limit_blocks_at_default_cap(pd, rounds_env, capsys):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8001", rounds=_rounds_past_the_cap(pd, "developer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["developer"]
    _prepare_n_rounds(pd, pm_home, slot, "T-8001", "developer", limit)
    capsys.readouterr()

    with pytest.raises(pd.InternalRoundLimitExceeded, match="라운드 상한 도달"):
        pd.prepare_ticket_copy(ticket="T-8001", role="developer", cwd=slot, pm_home=pm_home)


def test_developer_round_limit_cli_rc_matches_external_channel(
    pd, rounds_env, monkeypatch, capsys,
):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8002", rounds=_rounds_past_the_cap(pd, "developer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["developer"]
    _prepare_n_rounds(pd, pm_home, slot, "T-8002", "developer", limit)
    capsys.readouterr()

    rc = _prepare_rc(pd, monkeypatch, pm_home, slot, "T-8002", "developer")

    external = pd._load_additional_reviewer()
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    err = capsys.readouterr().err
    assert "현재 티켓을 정지하고 사용자에게 보고" in err
    assert "라운드를 더 예약하지 않습니다" in err
    assert "재설계" not in err and "분할" not in err
    assert "현재 라운드" in err and "01-developer.md" in err
    assert "다시 시도" not in err and "재시도" not in err  # 라운드 추가 유도 문구 없음


def test_round_under_cap_is_not_blocked(pd, rounds_env):
    """역방향: 상한 미만이면 정상 준비된다(오차단 0)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8003", rounds=_rounds_past_the_cap(pd, "developer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["developer"]
    plan = _prepare_n_rounds(pd, pm_home, slot, "T-8003", "developer", limit - 1)
    assert plan.ordinal == limit - 1


def test_round_limit_rejection_leaves_no_slot_residue(pd, rounds_env):
    """게이트는 예약(board)뿐 아니라 슬롯 run-dir 도 만들기 전에 거부한다(고아 0)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8017", rounds=_rounds_past_the_cap(pd, "developer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["developer"]
    _prepare_n_rounds(pd, pm_home, slot, "T-8017", "developer", limit)
    copy_root = _copy_root(pd, slot, "T-8017")
    before = set(copy_root.iterdir()) if copy_root.exists() else set()

    with pytest.raises(pd.InternalRoundLimitExceeded):
        pd.prepare_ticket_copy(ticket="T-8017", role="developer", cwd=slot, pm_home=pm_home)

    after = set(copy_root.iterdir()) if copy_root.exists() else set()
    assert after == before


def test_code_reviewer_and_architect_caps_are_higher_than_developer(pd):
    """결정: code-reviewer·architect 는 developer 보다 라운드가 더 필요하다(실측 분포 근거)."""
    limits = pd.DEFAULT_INTERNAL_ROUND_LIMITS
    assert limits["code-reviewer"] > limits["developer"]
    assert limits["architect"] > limits["developer"]


def test_local_conf_overrides_role_round_limit(pd, rounds_env, capsys):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8004", rounds=_rounds_past_the_cap(pd, "developer"))
    (pm_home / ".project_manager" / "local.conf").write_text(
        "internal_review_round_limit.developer=2\n", encoding="utf-8",
    )
    _prepare_n_rounds(pd, pm_home, slot, "T-8004", "developer", 2)
    capsys.readouterr()

    with pytest.raises(pd.InternalRoundLimitExceeded, match=r"상한 2\)"):
        pd.prepare_ticket_copy(ticket="T-8004", role="developer", cwd=slot, pm_home=pm_home)


def test_the_role_cap_is_final_for_every_caller(pd, rounds_env):
    """예약 상한은 어떤 인자로도 한 라운드를 더 열지 않고 현재 티켓을 보고한다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8021", rounds=_rounds_past_the_cap(pd, "code-reviewer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["code-reviewer"]
    _prepare_n_rounds(pd, pm_home, slot, "T-8021", "code-reviewer", limit)

    with pytest.raises(pd.InternalRoundLimitExceeded, match=r"상한 \d+\)") as caught:
        pd.prepare_ticket_copy(
            ticket="T-8021", role="code-reviewer", cwd=slot, pm_home=pm_home,
        )

    message = str(caught.value)
    assert "현재 티켓을 정지하고 사용자에게 보고" in message
    assert "라운드를 더 예약하지 않습니다" in message
    assert "재설계" not in message and "분할" not in message


def test_researcher_round_prepare_is_refused(pd, rounds_env):
    """researcher 는 묶음 수열의 단계가 아니라 티켓 라운드를 준비하지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8005")
    with pytest.raises(pd.DelegateError):
        pd.prepare_ticket_copy(
            ticket="T-8005", role="researcher", cwd=slot, pm_home=pm_home,
        )


def test_harvest_and_copies_are_outside_the_gate(pd, rounds_env, monkeypatch):
    """역방향: harvest·copies 는 게이트 밖이다 — 진행 중 라운드를 고아로 만들지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8006", rounds=_rounds_past_the_cap(pd, "developer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["developer"]
    plan = _prepare_n_rounds(pd, pm_home, slot, "T-8006", "developer", limit)
    # 상한 도달 상태에서도 이미 예약된 라운드의 harvest 는 막히지 않는다.
    produced = plan.path.read_text(encoding="utf-8") + "\n## 변경 파일\n- x\n"
    plan.path.write_text(produced, encoding="utf-8", newline="")
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    rc = pd._cmd_ticket(["harvest", "--copy", str(plan.path), "--cwd", str(slot)])
    assert rc == 0
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: pm_home)
    rc2 = pd._cmd_ticket(["copies", "--ticket", "T-8006"])
    assert rc2 == 0


def test_flat_cross_cli_preserves_round_limit_rc(pd, rounds_env, monkeypatch):
    """F-004 회귀 — flat cross CLI 도 InternalRoundLimitExceeded 를 rc=1 로 뭉개지 않고
    전용 rc(외부 채널 EXIT_ROUND_LIMIT_EXCEEDED=4)로 보존한다(per-ticket cap 은 어떤 승인으로도
    안 열림)."""
    pm_home, _slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8020")

    def _raise(*_args, **_kwargs):
        raise pd.InternalRoundLimitExceeded("오류: 내부 위임 라운드 상한 도달 — test")

    monkeypatch.setattr(pd, "prepare_ticket_copy", _raise)
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x",
    })
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *a, **k: True)

    prompt = pm_home / "task.md"
    prompt.write_text("작업 내용", encoding="utf-8")

    rc = pd.main(
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(pm_home),
         "--ticket", "T-8020", "--output-dir", str(pm_home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("라운드 상한 거부 뒤 스폰되면 안 됨"),
    )

    external = pd._load_additional_reviewer()
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED


def test_concurrent_prepares_at_the_cap_boundary_admit_exactly_one(
    pd, rounds_env, monkeypatch,
):
    """F-003 TOCTOU 회귀 — 상한-1 에서 두 동시 prepare 중 정확히 하나만 성공하고
    최종 count 가 상한을 넘지 않는다(리뷰어 결정적 재현 재구성: 사전판정 직후 barrier)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-9001", rounds=_rounds_past_the_cap(pd, "developer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["developer"]
    _prepare_n_rounds(pd, pm_home, slot, "T-9001", "developer", limit - 1)

    barrier = threading.Barrier(2)
    real_count = pd._internal_round_count
    call_lock = threading.Lock()
    state = {"n": 0}

    def _synced_count(existing, role):
        # 처음 두 호출(양쪽 thread 의 (1.5) 사전판정)만 서로 기다린다 — phase-2(락 안·직렬)
        # 호출까지 묶으면 락을 쥔 쪽이 상대를 영원히 기다려 교착한다.
        with call_lock:
            state["n"] += 1
            my_call = state["n"]
        result = real_count(existing, role)
        if my_call <= 2:
            barrier.wait(timeout=5)
        return result

    results: list = []
    errors: list = []

    def _worker():
        try:
            plan = pd.prepare_ticket_copy(
                ticket="T-9001", role="developer", cwd=slot, pm_home=pm_home,
            )
            results.append(plan)
        except pd.InternalRoundLimitExceeded as exc:
            errors.append(exc)

    with unittest.mock.patch.object(pd, "_internal_round_count", side_effect=_synced_count):
        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    assert len(results) == 1, f"동시 성공 {len(results)}건(정확히 1이어야 함) — errors={errors}"
    assert len(errors) == 1

    rounds_module = pd._load_ticket_rounds()
    final = rounds_module.load_rounds(pm_home / ".project_manager" / "wiki" / "tickets", "T-9001")
    assert pd._internal_round_count(final, "developer") == limit


# ── F-005(라운드 6) — 스폰 전 게이트 거부의 ticket 예약 환불 ────────────────
#
# `main()`(flat cross CLI)이 `prepare_ticket_copy` 로 board 라운드·run-dir 을 실제로 예약한
# 뒤, 스폰 전 게이트(재앵커·시크릿 스캔·egress) 가 거부하면 그 예약을 `abandon_ticket_copy`
# 로 즉시 환불한다(대안 b · 새 경로 없음). 불변식: 거부 뒤 board 라운드 증가 0 · 장부 미회수
# 행 0 · run-dir 0, 정상 실행 경로엔 환불이 발동하지 않는다(역방향).

_REFUND_EGRESS_MARKER = "CODEX_SANDBOX_NETWORK_DISABLED"


@pytest.fixture
def refund_env(tmp_path, pd, monkeypatch):
    """cwd·PM 홈이 같은 디렉터리(자기-정박) — `main()` 이 실 prepare 로 board 라운드·장부
    행·run-dir 을 진짜로 만들게 하면서 `--cwd` 하나로 self-anchored PM 홈 해소가 되게 한다."""
    home = tmp_path / "home"
    pm_tools = home / ".project_manager" / "tools"
    pm_tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, pm_tools / source.name)
    tickets = home / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (home / ".project_manager" / ".local").mkdir(parents=True)
    assert _git(home, "init", "-q").returncode == 0
    gitignore = home / ".project_manager" / ".gitignore"
    gitignore.write_text(".local/\n", encoding="utf-8", newline="\n")
    (home / "tracked.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(home, "add", "tracked.txt", ".project_manager/.gitignore").returncode == 0
    monkeypatch.setenv("GIT_AUTHOR_NAME", "refund")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "refund@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "refund")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "refund@test.invalid")
    assert _git(home, "commit", "-qm", "seed").returncode == 0
    sync_log: list = []
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, home, sync_log),
    )
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x",
    })
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *a, **k: True)
    monkeypatch.delenv(_REFUND_EGRESS_MARKER, raising=False)
    return home, tickets


def _refund_round_file_count(pd, home: Path, ticket: str) -> int:
    rounds_dir = _rounds_dir(home, ticket)
    return len(list(rounds_dir.glob("*.md"))) if rounds_dir.is_dir() else 0


def _refund_run_dir_count(pd, home: Path, ticket: str) -> int:
    root = _copy_root(pd, home, ticket)
    return len(list(root.iterdir())) if root.is_dir() else 0


def _refund_unterminated_ledger_rows(pd, home: Path, ticket: str) -> int:
    """장부는 append-only다 — 같은 `copy` 의 마지막 행(추가 순서)이 그 예약의 현재 상태다."""
    rows = [row for row in _ledger_rows(home) if row["ticket"] == ticket]
    latest_by_copy: dict[str, dict] = {}
    for row in rows:
        latest_by_copy[row["copy"]] = row
    return sum(
        1 for row in latest_by_copy.values()
        if row.get("harvested_at") is None and "abandoned_at" not in row
    )


def test_secret_scan_rejection_refunds_the_reserved_ticket_copy(pd, refund_env):
    """F-005 — 시크릿 스캔 거부 뒤 board 라운드·run-dir·장부 미회수 행이 전부 0으로 되돌아간다."""
    home, tickets = refund_env
    ticket = "T-9101"
    _write_spec(tickets, ticket)
    before = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert before == (0, 0, 0)

    prompt = home / "task.md"
    prompt.write_text("배포 전에 config.secret.key 를 확인하라", encoding="utf-8")
    rc = pd.main(
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("시크릿 스캔 거부 뒤 스폰되면 안 됨"),
    )
    assert rc == 1

    after = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert after == (0, 0, 0), f"환불 뒤 잔류 — before={before} after={after}"


def test_reanchor_rejection_refunds_the_reserved_ticket_copy(pd, refund_env):
    """F-005 — 엔진 코드 write 재앵커 거부 뒤 board 라운드·run-dir·장부 미회수 행이 0으로
    되돌아간다. adopter#0 재앵커 판정은 canonical worktree(`work/<n>/…/additional_reviewer.py`)
    존재를 추가로 요구한다."""
    home, tickets = refund_env
    ticket = "T-9102"
    _write_spec(tickets, ticket)
    wt_tools = home / "work" / "wt1" / ".project_manager" / "tools"
    wt_tools.mkdir(parents=True)
    (wt_tools / "additional_reviewer.py").write_text("# stub", encoding="utf-8")
    before = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert before == (0, 0, 0)

    prompt = home / "task.md"
    prompt.write_text(
        "다음 파일을 수정하라: .project_manager/tools/board.py 의 함수", encoding="utf-8",
    )
    rc = pd.main(
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("재앵커 거부 뒤 스폰되면 안 됨"),
    )
    assert rc == 1

    after = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert after == (0, 0, 0), f"환불 뒤 잔류 — before={before} after={after}"


def test_codex_egress_rejection_refunds_the_reserved_ticket_copy(pd, refund_env, monkeypatch):
    """F-005 — codex egress 미승격 거부 뒤 board 라운드·run-dir·장부 미회수 행이 0으로
    되돌아간다."""
    home, tickets = refund_env
    ticket = "T-9103"
    _write_spec(tickets, ticket)
    monkeypatch.setenv(_REFUND_EGRESS_MARKER, "1")
    before = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert before == (0, 0, 0)

    prompt = home / "task.md"
    prompt.write_text("문서를 정리하라", encoding="utf-8")
    rc = pd.main(
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("egress 미승격 거부 뒤 스폰되면 안 됨"),
    )
    assert rc == 1

    after = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert after == (0, 0, 0), f"환불 뒤 잔류 — before={before} after={after}"


def test_successful_delegation_does_not_refund_the_ticket_copy(pd, refund_env, monkeypatch):
    """역방향 — 정상 스폰·정상 종료 경로는 환불을 부르지 않는다(회수된 라운드가 그대로 남는다).
    스폰 이후엔 기존 harvest 가 세 자산을 정상 종결하므로, 여기서 지키는 성질은 "환불 helper 가
    이번 호출에서 한 번도 안 불렸다"는 것 하나다."""
    home, tickets = refund_env
    ticket = "T-9104"
    _write_spec(tickets, ticket)

    refund_calls: list = []
    real_refund = pd._refund_gate_rejected_ticket_copy

    def _spy(ticket_copy, **kwargs):
        refund_calls.append(ticket_copy)
        return real_refund(ticket_copy, **kwargs)

    monkeypatch.setattr(pd, "_refund_gate_rejected_ticket_copy", _spy)

    prompt = home / "task.md"
    prompt.write_text("문서를 정리하라", encoding="utf-8")
    codex_reply = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "th1"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "DONE"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ])

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        return {"returncode": 0, "stdout": codex_reply, "stderr": "", "timed_out": False}

    rc = pd.main(
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=_run_fn,
    )
    assert rc == 0
    assert refund_calls == []


def _codex_reply(text: str = "DONE") -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "th1"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": text}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ])


def test_cross_delegation_binds_run_id_and_copy_to_its_delegate_rounds_reservation(
        pd, refund_env):
    """[[T-0838]] — 실 장부 조인: raw 행의 run_id·copy 가 delegate-rounds 장부의 같은 예약
    행과 문자열 그대로 일치한다(두 장부를 각자 실제 파일로 만들어 값으로 확인한다)."""
    home, tickets = refund_env
    ticket = "T-9105"
    _write_spec(tickets, ticket, rounds=("developer",))

    prompt = home / "task.md"
    prompt.write_text("문서를 정리하라", encoding="utf-8")

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        return {"returncode": 0, "stdout": _codex_reply(), "stderr": "", "timed_out": False}

    rc = pd.main(
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=_run_fn,
    )
    assert rc == 0

    round_row = next(row for row in _ledger_rows(home) if row["ticket"] == ticket)
    raw_rows = pd._load_relay().raw_records(home / "raw" / "raw_outputs.json")
    raw_row = next(row for row in raw_rows if row.get("ticket") == ticket)

    assert raw_row["run_id"] == round_row["run_id"]
    assert raw_row["copy"] == round_row["copy"]


def test_cross_delegation_prompt_carries_the_next_finding_id(pd, refund_env, monkeypatch):
    """실행 경로 — 하네스로 나가는 프롬프트가 시드와 **같은** 다음 finding ID 를 싣는다.

    리뷰어 세션은 라운드마다 fresh 라 이전 번호를 모른다. 사본을 열기 전에 프롬프트만 읽는
    위임에서 번호가 추측이 되면 그 라운드는 재선언으로 회수되지 않는다.
    """
    home, tickets = refund_env
    ticket = "T-9107"
    _write_spec(tickets, ticket, rounds=("code-reviewer",))
    (home / ".project_manager" / "local.conf").write_text(
        "delegate.enabled=true\n"
        "delegate.code-reviewer.harness=claude\n"
        "delegate.code-reviewer.model=opus\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "claude",
        "delegate.code-reviewer.model": "opus",
    })
    captured_plan: dict[str, object] = {}
    real_prepare = pd.prepare_ticket_copy

    def _prepare_spy(**kwargs):
        plan = real_prepare(**kwargs)
        captured_plan["plan"] = plan
        return plan

    monkeypatch.setattr(pd, "prepare_ticket_copy", _prepare_spy)
    prompts: list[str] = []

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        prompts.append(stdin_text)
        plan = captured_plan["plan"]
        _write_round_output(
            plan.path, _review_body(pd, "code-reviewer", [plan.next_finding_id]),
        )
        return {
            "returncode": 0,
            "stdout": json.dumps({
                "type": "result", "result": "라운드 파일에 판정을 기록했다.",
                "session_id": "session-1",
            }),
            "stderr": "", "timed_out": False,
        }

    prompt = home / "task.md"
    prompt.write_text("구현을 검토하라.", encoding="utf-8")

    rc = pd.main(
        ["--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=_run_fn,
    )

    assert rc == 0
    plan = captured_plan["plan"]
    assert plan.next_finding_id == "F-001"
    assert f"`{plan.next_finding_id}` 부터" in prompts[0]
    # 프롬프트가 지시한 번호로 쓴 산출이 그대로 회수된다(자기 충돌 0).
    assert not plan.run_dir.exists()
    assert '"id":"F-001"' in plan.board_path.read_text(encoding="utf-8").replace(" ", "")


def test_two_cross_runs_of_the_same_ticket_and_role_bind_1to1_not_swapped(
        pd, refund_env, monkeypatch):
    """같은 (ticket, role) 의 서로 다른 run 두 건이 각자 자기 예약에만 결속된다.

    rounds 장부는 prepare·harvest 두 곳이 쓰고, harvest 행은 prepare 행의 `dict(row)` 스냅샷이라
    `run_id`·`copy` 를 그대로 복제한다(실 장부 고유 run_id 대부분이 이 모양으로 2행). `_run_fn`
    이 슬롯 라운드 파일에 실제로 산출을 써서 harvest 가 "산출 없음" 조기 반환에 빠지지 않고 이
    복제를 실제로 만들게 한다 — 픽스처가 우연히 시드 그대로 두어 복제가 안 생기는 형상을
    불변식으로 박제하지 않는다. 예약의 정체성은 행이 아니라 `copy` 로 접은 최신 스냅샷이므로
    `ticket_copy_records`(copy 별 최신 append)로 접어서 단언한다."""
    home, tickets = refund_env
    ticket = "T-9106"
    _write_spec(tickets, ticket, rounds=("developer", "developer"))

    prepared_plans: list = []
    real_prepare = pd.prepare_ticket_copy

    def _prepare_spy(*args, **kwargs):
        plan = real_prepare(*args, **kwargs)
        prepared_plans.append(plan)
        return plan

    monkeypatch.setattr(pd, "prepare_ticket_copy", _prepare_spy)

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        # 실 하네스가 슬롯 라운드 파일에 산출을 쓰는 형상을 재현한다 — 그래야 harvest 가
        # 실제로 rounds 장부에 harvested 행(=prepared 행의 dict(row) 스냅샷)을 남긴다.
        plan = prepared_plans[-1]
        plan.path.write_text(
            plan.path.read_text(encoding="utf-8") + "\n## 변경 파일\n- 실산출\n",
            encoding="utf-8", newline="",
        )
        return {"returncode": 0, "stdout": _codex_reply(), "stderr": "", "timed_out": False}

    for label in ("1라운드", "2라운드"):
        prompt = home / f"task-{label}.md"
        prompt.write_text(f"{label} 지시", encoding="utf-8")
        argv = [
            "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
            "--ticket", ticket, "--output-dir", str(home / "raw"),
        ]
        if label == "2라운드":
            # cold 재투입 거부(같은 ticket·role 완료 레코드 존재)를 명시 fresh 로 넘긴다 —
            # 이 테스트가 보려는 건 재사용 축이 아니라 두 번째 run 의 결속 값이다.
            argv += ["--fresh", "1:1 결속 값 검증을 위한 두 번째 독립 run"]
        rc = pd.main(argv, run_fn=_run_fn)
        assert rc == 0

    all_round_rows = [row for row in _ledger_rows(home) if row["ticket"] == ticket]
    # 실 장부 형상: run 당 prepared+harvested 2행(dict(row) 스냅샷 복제) — 총 4행, 고유 run_id 는 2.
    assert len(all_round_rows) == 4
    assert len({row["run_id"] for row in all_round_rows}) == 2

    # 예약의 정체성은 행이 아니라 copy 로 접은 최신 스냅샷이다.
    round_rows = pd.ticket_copy_records(home, ticket=ticket)
    assert len(round_rows) == 2
    assert {row["ordinal"] for row in round_rows} == {1, 2}
    assert round_rows[0]["run_id"] != round_rows[1]["run_id"]
    assert all(row["harvested_at"] is not None for row in round_rows)

    raw_rows = [
        row for row in pd._load_relay().raw_records(home / "raw" / "raw_outputs.json")
        if row.get("ticket") == ticket
    ]
    assert len(raw_rows) == 2

    for round_row in round_rows:
        # run_id 로 접은 raw 행의 copy 가 전부 이 예약의 copy 와만 일치한다 — 다른 run 과
        # 섞이면(교차 결속) 이 set 에 다른 copy 가 섞여 값 단언이 깨진다. rounds 장부 쪽에
        # 같은 run_id 행이 2개(prepared+harvested) 있어도 raw 쪽 매칭 결과는 영향받지 않는다.
        matches = [row for row in raw_rows if row["run_id"] == round_row["run_id"]]
        assert matches, f"1:1 결속 위반 — run_id={round_row['run_id']} 에 raw 행 없음"
        assert {row["copy"] for row in matches} == {round_row["copy"]}

    # 서로 다른 run 이므로 raw 행의 copy 도 서로 다르다(같은 예약으로 뒤섞이지 않는다).
    assert raw_rows[0]["copy"] != raw_rows[1]["copy"]
def test_configured_convergence_limit_runs_every_round_through_main(
    pd, refund_env, monkeypatch,
):
    """설정된 수렴 상한(`delegate.code-reviewer.rounds_max=5` · 예약 상한 기본값과 같은 값)에서
    `main()` 경유 5회가 전부 성공하고, 그 다음 요청은 rc 1 로 막힌다(상한 뒤 창 0)."""
    home, tickets = refund_env
    ticket = "T-9105"
    _write_spec(tickets, ticket, rounds=_rounds_past_the_cap(pd, "code-reviewer"))
    rounds_max = 5
    assert rounds_max == pd.DEFAULT_INTERNAL_ROUND_LIMITS["code-reviewer"]
    # 실 conf 파일 하나를 단일 진실로 쓴다 — `main()`(prepare/reserve)과 board 의 완료
    # 재검증(별도로 로드되는 pm_delegate 사본)이 같은 파일을 읽어야 F-001 이 재현·검증된다.
    (home / ".project_manager" / "local.conf").write_text(
        "delegate.enabled=true\n"
        "delegate.code-reviewer.harness=claude\n"
        "delegate.code-reviewer.model=opus\n"
        f"{pd.INTERNAL_REVIEW_ROUNDS_MAX_KEY}={rounds_max}\n",
        encoding="utf-8",
    )
    # `refund_env` 가 이미 `local_config` 를 developer 프로필 dict 로 monkeypatch 해 뒀다 —
    # 여기서 code-reviewer 프로필(+ 수렴 상한)을 포함하도록 덮어써 `main()` 호출이 그 값을 본다.
    # board 의 완료 재검증은 별도 로드되는 pm_delegate 사본이라 이 monkeypatch 가 안 닿는다 —
    # 그쪽은 위에서 쓴 실 conf 파일을 그대로 읽는다(같은 값 · 한 진실).
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "claude",
        "delegate.code-reviewer.model": "opus",
        pd.INTERNAL_REVIEW_ROUNDS_MAX_KEY: str(rounds_max),
    })

    # 내부 code-reviewer 판정은 회수될 라운드 파일 bytes 에서 나온다(터미널 회신은 `--ticket`
    # 없는 실행의 대체 입력일 뿐이다) — 그래서 `_run_fn` 은 준비가 넘긴 실제 슬롯 경로에
    # must-fix 1건짜리 반려 본문을 직접 써 넣는다. finding ID 는 라운드마다 새로 발급한다
    # (같은 ID 재선언은 harvest 가 별도 축으로 거부한다 — 여기서 재는 축이 아니다).
    captured_plan: dict[str, object] = {}
    real_prepare = pd.prepare_ticket_copy

    def _prepare_spy(**kwargs):
        plan = real_prepare(**kwargs)
        captured_plan["plan"] = plan
        return plan

    monkeypatch.setattr(pd, "prepare_ticket_copy", _prepare_spy)
    finding_counter = iter(range(1, rounds_max + 2))

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        finding_id = f"F-{next(finding_counter):03d}"
        body = _review_body(pd, "code-reviewer", [finding_id])
        _write_round_output(captured_plan["plan"].path, body)
        return {
            "returncode": 0,
            "stdout": json.dumps({
                "type": "result", "result": "라운드 파일에 판정을 기록했다.",
                "session_id": "session-1",
            }),
            "stderr": "", "timed_out": False,
        }

    prompt = home / "task.md"
    prompt.write_text("구현을 검토하라.", encoding="utf-8")

    for index in range(rounds_max):
        rc = pd.main(
            ["--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(home),
             "--ticket", ticket, "--output-dir", str(home / "raw")],
            run_fn=_run_fn,
        )
        assert rc == 0, f"일반 라운드 {index + 1}/{rounds_max} 는 설정된 상한 안이다"

    rc = pd.main(
        ["--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=_run_fn,
    )
    assert rc == pd._load_additional_reviewer().EXIT_ROUND_LIMIT_EXCEEDED, (
        "상한을 넘긴 요청은 예약되지 않는다")


# ── T-0846 — 단일 정리 경계(지점 삽입 대체) ─────────────────────────────────
#
# [[T-0846]] 는 [[T-0841]] 라운드 6이 꽂은 6개 지점 삽입을 `main()` 의 finally 하나로 대체한다.
# 위 F-005 테스트 3건은 그 경계를 지나는 명시 `return` 경로 회귀를 그대로 지킨다(변경 없음).
# 아래는 라운드 7 이 지점 삽입이 못 닫은 것으로 실측한 축(전파 예외 · 정리 실패 마스킹 · 동시
# prepare 패자 run-dir)의 회귀다.


def test_propagated_exception_before_handoff_refunds_the_reserved_ticket_copy(
    pd, refund_env, monkeypatch,
):
    """T-0846 — 예약~실행 인계 사이 전파 예외(지점 `return` 이 아니다)도 단일 경계가 환불한다.
    라운드 7 재현 probe: `_resolved_adapter_directories()` 가 던지면 지점 삽입 방식에선 board·
    run-dir·장부 미회수 = (1,1,1) 로 남았다 — 이제 (0,0,0) 이다."""
    home, tickets = refund_env
    ticket = "T-9105"
    _write_spec(tickets, ticket)

    def _raise():
        raise pd.DelegateError("probe: adapter directories 조회 실패")

    monkeypatch.setattr(pd, "_resolved_adapter_directories", _raise)

    before = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert before == (0, 0, 0)

    prompt = home / "task.md"
    prompt.write_text("문서를 정리하라", encoding="utf-8")
    with pytest.raises(pd.DelegateError, match="probe"):
        pd.main(
            ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
             "--ticket", ticket, "--output-dir", str(home / "raw")],
            run_fn=lambda *a, **k: pytest.fail("전파 예외 뒤 스폰되면 안 됨"),
        )

    after = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert after == (0, 0, 0), f"전파 예외 뒤 잔류 — before={before} after={after}"


def test_internal_verdict_cap_refusal_refunds_the_reserved_ticket_copy(
    pd, refund_env, monkeypatch,
):
    """T-0846 — 내부 verdict 상한 거부(`internal_budget.refused_rc`)는 harvest 를 도는 안쪽
    finally 안에 중첩된 반환 지점이라, 지점 삽입 방식에선 이미 환불된 예약을 harvest 가 다시
    건드려(이미 포기됨 오류) 원 rc 를 덮었다(라운드 7 구조적 결함). 단일 경계 아래에서는
    `_ticket_copy_handed_off` 가 False 라 harvest 를 건너뛰고 환불만 한 번 돈다."""
    home, tickets = refund_env
    ticket = "T-9107"
    _write_spec(tickets, ticket)
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate_enabled": "true",
        "delegate.code-reviewer.harness": "codex", "delegate.code-reviewer.model": "gpt-x",
    })
    monkeypatch.setattr(
        pd, "_reserve_internal_review_round",
        lambda *a, **k: pd.InternalRoundBudget(refused_rc=1),
    )

    before = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert before == (0, 0, 0)

    prompt = home / "task.md"
    prompt.write_text("리뷰 요청", encoding="utf-8")
    rc = pd.main(
        ["--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("내부 verdict 상한 거부 뒤 스폰되면 안 됨"),
    )
    assert rc == 1

    after = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert after == (0, 0, 0), f"내부 verdict 상한 거부 뒤 잔류 — before={before} after={after}"


def test_cleanup_failure_does_not_mask_the_original_rejection(
    pd, refund_env, monkeypatch, capsys,
):
    """T-0846 — 환불(`abandon_ticket_copy`) 자체가 `DelegateError` 가 아닌 예외로 실패해도 원
    rc·거부 사유는 그대로 나오고, 정리 실패는 추가 loud 줄로만 붙는다(원 결과를 대체하지 않음).
    실패한 환불은 예약도 그대로 남긴다 — 잃어버리지 않고 loud 로 남긴다."""
    home, tickets = refund_env
    ticket = "T-9106"
    _write_spec(tickets, ticket)

    def _boom(**_kwargs):
        raise RuntimeError("정리 폭발(probe)")

    monkeypatch.setattr(pd, "abandon_ticket_copy", _boom)

    prompt = home / "task.md"
    prompt.write_text("배포 전에 config.secret.key 를 확인하라", encoding="utf-8")
    rc = pd.main(
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", ticket, "--output-dir", str(home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("시크릿 스캔 거부 뒤 스폰되면 안 됨"),
    )
    assert rc == 1

    err = capsys.readouterr().err
    assert "시크릿 denylist 판정" in err, "원 거부 사유가 그대로 나와야 한다"
    assert "환불 실패" in err and "정리 폭발" in err, "정리 실패가 추가 loud 줄로 붙어야 한다"

    after = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert after == (1, 1, 1), f"환불 실패 뒤 진단 좌표(잔류 예약)가 사라졌다 — after={after}"


def test_concurrent_prepare_loser_leaves_no_orphaned_run_dir(pd, rounds_env):
    """T-0846(F-010) — 상한-1 에서 동시 prepare 두 건 중 패자가 board 라운드·장부 행 없이
    run-dir 만 남기지 않는다(결정적 barrier 재현 — 라운드 7 실측: developer 상한 4 ·
    before=(3,3,3) → 성공 1·`InternalRoundLimitExceeded` 1 → after=(4,5,4) 였던 결함)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-9201", rounds=_rounds_past_the_cap(pd, "developer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["developer"]
    _prepare_n_rounds(pd, pm_home, slot, "T-9201", "developer", limit - 1)

    def _round_files() -> int:
        rounds_dir = _rounds_dir(pm_home, "T-9201")
        return len(list(rounds_dir.glob("*.md"))) if rounds_dir.is_dir() else 0

    def _run_dirs() -> int:
        root = _copy_root(pd, slot, "T-9201")
        return len(list(root.iterdir())) if root.is_dir() else 0

    def _unterminated_ledger_rows() -> int:
        rows = [row for row in _ledger_rows(pm_home) if row["ticket"] == "T-9201"]
        latest_by_copy: dict[str, dict] = {}
        for row in rows:
            latest_by_copy[row["copy"]] = row
        return sum(
            1 for row in latest_by_copy.values()
            if row.get("harvested_at") is None and "abandoned_at" not in row
        )

    before = (_round_files(), _run_dirs(), _unterminated_ledger_rows())
    assert before == (limit - 1, limit - 1, limit - 1)

    barrier = threading.Barrier(2)
    real_count = pd._internal_round_count
    call_lock = threading.Lock()
    state = {"n": 0}

    def _synced_count(existing, role):
        # F-003 회귀와 같은 재구성 — 처음 두 호출(양쪽 thread 의 (1.5) 사전판정)만 서로 기다린다.
        with call_lock:
            state["n"] += 1
            my_call = state["n"]
        result = real_count(existing, role)
        if my_call <= 2:
            barrier.wait(timeout=5)
        return result

    results: list = []
    errors: list = []

    def _worker():
        try:
            plan = pd.prepare_ticket_copy(
                ticket="T-9201", role="developer", cwd=slot, pm_home=pm_home,
            )
            results.append(plan)
        except pd.InternalRoundLimitExceeded as exc:
            errors.append(exc)

    with unittest.mock.patch.object(pd, "_internal_round_count", side_effect=_synced_count):
        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    assert len(results) == 1, f"동시 성공 {len(results)}건(정확히 1이어야 함) — errors={errors}"
    assert len(errors) == 1

    after = (_round_files(), _run_dirs(), _unterminated_ledger_rows())
    assert after == (limit, limit, limit), f"패자 run-dir 잔류 — before={before} after={after}"


# ── T-0846 라운드 3 fix — F-001(BaseException-safe 정리) · F-002(락 이탈 후 잔여) ──────────


def test_gate_refund_control_exceptions_do_not_override_the_original_rejection_rc(
    pd, refund_env, monkeypatch, capsys,
):
    """F-001 — `main()` 환불(`abandon_ticket_copy`)에 `KeyboardInterrupt`·`SystemExit` 를
    주입해도 원 거부 rc=1·원 사유가 그대로 나오고, 정리 실패는 추가 loud 줄로만 붙는다(원래는
    `except Exception`만 잡아 두 제어 예외가 rc 대신 그대로 전파됐다)."""
    home, tickets = refund_env

    for index, (exc_factory, label) in enumerate([
        (lambda: KeyboardInterrupt(), "KeyboardInterrupt"),
        (lambda: SystemExit(73), "SystemExit"),
    ]):
        ticket = f"T-9302{index}"
        _write_spec(tickets, ticket)

        def _boom(**_kwargs):
            raise exc_factory()

        monkeypatch.setattr(pd, "abandon_ticket_copy", _boom)

        prompt = home / "task.md"
        prompt.write_text("배포 전에 config.secret.key 를 확인하라", encoding="utf-8")
        rc = pd.main(
            ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
             "--ticket", ticket, "--output-dir", str(home / "raw")],
            run_fn=lambda *a, **k: pytest.fail("시크릿 스캔 거부 뒤 스폰되면 안 됨"),
        )
        assert rc == 1, f"{label}: 원 rc=1 이 보존돼야 한다"

        err = capsys.readouterr().err
        assert "시크릿 denylist 판정" in err, f"{label}: 원 거부 사유가 그대로 나와야 한다"
        assert "환불 실패" in err and label in err, (
            f"{label}: 정리 실패가 유형과 함께 추가 loud 줄로 붙어야 한다 — err={err}"
        )

        after = (
            _refund_round_file_count(pd, home, ticket),
            _refund_run_dir_count(pd, home, ticket),
            _refund_unterminated_ledger_rows(pd, home, ticket),
        )
        assert after == (1, 1, 1), f"{label}: 정리 실패 뒤 예약 좌표가 사라졌다 — after={after}"


def test_gate_refund_original_control_exception_still_propagates_when_cleanup_succeeds(
    pd, refund_env, monkeypatch,
):
    """역방향(F-001) — 원 예외 **자체**가 `KeyboardInterrupt` 고 환불이 성공하면, 그 예외가
    그대로 전파되고 세 축은 여전히 `(0,0,0)` 이다(정리 성공 경로는 이번 fix 로 안 바뀜)."""
    home, tickets = refund_env
    ticket = "T-9303"
    _write_spec(tickets, ticket)

    def _raise():
        raise KeyboardInterrupt()

    monkeypatch.setattr(pd, "_resolved_adapter_directories", _raise)

    before = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert before == (0, 0, 0)

    prompt = home / "task.md"
    prompt.write_text("문서를 정리하라", encoding="utf-8")
    with pytest.raises(KeyboardInterrupt):
        pd.main(
            ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
             "--ticket", ticket, "--output-dir", str(home / "raw")],
            run_fn=lambda *a, **k: pytest.fail("전파 예외 뒤 스폰되면 안 됨"),
        )

    after = (
        _refund_round_file_count(pd, home, ticket),
        _refund_run_dir_count(pd, home, ticket),
        _refund_unterminated_ledger_rows(pd, home, ticket),
    )
    assert after == (0, 0, 0), f"원 제어 예외 뒤 잔류 — before={before} after={after}"


def test_prepare_rollback_control_exceptions_preserve_the_original_round_limit_error(
    pd, rounds_env, monkeypatch, capsys,
):
    """F-001 — `prepare_ticket_copy` 예약 전 rollback(`force_rmtree`) 에 `RuntimeError`·
    `KeyboardInterrupt`·`SystemExit` 를 주입해도 원 `InternalRoundLimitExceeded` 가 그대로
    전파된다(원래는 `except OSError`만 잡아 주입 예외가 대신 전파되고 경고도 없었다). 정리
    자체는 여전히 실패하므로(주입) run-dir 고아는 남는다 — 이번 fix 가 보장하는 건 원 예외
    형·경고 유무이지 정리 성공이 아니다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-9304", rounds=_rounds_past_the_cap(pd, "developer"))
    limit = pd.DEFAULT_INTERNAL_ROUND_LIMITS["developer"]
    # (1.5) 빠른 사전판정은 통과시키고(existing=limit-1), 락 안 **최종 admission** 재확인만
    # 거부시킨다(review 재현과 동일 축 — F-003 TOCTOU 재구성 패턴 재사용). run_dir 은 사전판정
    # 뒤에 만들어지므로 이래야 force_rmtree 롤백 경로가 실제로 발동한다.
    _prepare_n_rounds(pd, pm_home, slot, "T-9304", "developer", limit - 1)

    def _round_files() -> int:
        rounds_dir = _rounds_dir(pm_home, "T-9304")
        return len(list(rounds_dir.glob("*.md"))) if rounds_dir.is_dir() else 0

    def _run_dirs() -> int:
        root = _copy_root(pd, slot, "T-9304")
        return len(list(root.iterdir())) if root.is_dir() else 0

    def _unterminated_ledger_rows() -> int:
        rows = [row for row in _ledger_rows(pm_home) if row["ticket"] == "T-9304"]
        latest_by_copy: dict[str, dict] = {}
        for row in rows:
            latest_by_copy[row["copy"]] = row
        return sum(
            1 for row in latest_by_copy.values()
            if row.get("harvested_at") is None and "abandoned_at" not in row
        )

    baseline = (_round_files(), _run_dirs(), _unterminated_ledger_rows())
    assert baseline == (limit - 1, limit - 1, limit - 1)

    real_count = pd._internal_round_count

    def _force_final_check_over_limit(existing, role):
        # 첫 호출((1.5) 사전판정)은 실값을 그대로 돌려 통과시키고, 두 번째 호출(락 안 최종
        # 재확인)만 상한 도달로 강제한다.
        state["n"] += 1
        if state["n"] == 1:
            return real_count(existing, role)
        return limit

    def _attempt_rejected_prepare():
        state["n"] = 0
        with unittest.mock.patch.object(
            pd, "_internal_round_count", side_effect=_force_final_check_over_limit,
        ):
            pd.prepare_ticket_copy(
                ticket="T-9304", role="developer", cwd=slot, pm_home=pm_home,
            )

    state = {"n": 0}
    # 역방향(F-001 c) — 정리가 정상 동작하면(주입 없음) 여전히 (0,0,0) 델타다.
    with pytest.raises(pd.InternalRoundLimitExceeded):
        _attempt_rejected_prepare()
    assert (_round_files(), _run_dirs(), _unterminated_ledger_rows()) == baseline

    file_lock = pd._load_file_lock()
    expected_run_dirs = baseline[1]
    for exc_factory, label in [
        (lambda: RuntimeError("정리 폭발(probe)"), "RuntimeError"),
        (lambda: KeyboardInterrupt(), "KeyboardInterrupt"),
        (lambda: SystemExit(74), "SystemExit"),
    ]:
        def _boom(*_a, _factory=exc_factory, **_k):
            raise _factory()

        monkeypatch.setattr(file_lock, "force_rmtree", _boom)
        capsys.readouterr()
        with pytest.raises(pd.InternalRoundLimitExceeded):
            _attempt_rejected_prepare()
        err = capsys.readouterr().err
        assert "run-dir 정리 실패" in err and label in err, (
            f"{label}: 정리 실패가 유형과 함께 loud 줄로 붙어야 한다 — err={err}"
        )
        expected_run_dirs += 1  # 정리 자체는 주입으로 계속 실패하므로 고아 1개씩 누적한다.
        assert _round_files() == baseline[0] and _unterminated_ledger_rows() == baseline[2]
        assert _run_dirs() == expected_run_dirs, (
            f"{label}: run-dir 고아 누적이 어긋났다 — {_run_dirs()} != {expected_run_dirs}"
        )


def test_board_lock_exit_failure_after_reserve_preserves_run_dir_and_diagnostics(
    pd, rounds_env, monkeypatch, capsys,
):
    """F-002 — `reserve_round` 성공 뒤 두 번째 `board_lock` 의 `__exit__` 가 `OSError` 로
    실패해도, board 라운드만 남고 run-dir·좌표가 사라지는 복구 불능 형상을 만들지 않는다.
    (board,run-dir)=(1,1) 로 run-dir 이 보존되고, 진단 메시지에 board 좌표(`_reserved_round_
    residue`)가 실린다(원래는 `reserved=True` 가 락 이탈 뒤라 이 경로에서 run-dir 이 삭제돼
    (board,run-dir)=(1,0) 이었다)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-9305")

    board = pd._load_board_for_repo(pm_home)
    real_board_lock = board.board_lock
    state = {"n": 0}

    @contextlib.contextmanager
    def _flaky_board_lock():
        state["n"] += 1
        call_number = state["n"]
        with real_board_lock():
            yield
            if call_number == 2:
                raise OSError("probe: board_lock __exit__ 실패")

    monkeypatch.setattr(board, "board_lock", _flaky_board_lock)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)

    def _round_files() -> int:
        rounds_dir = _rounds_dir(pm_home, "T-9305")
        return len(list(rounds_dir.glob("*.md"))) if rounds_dir.is_dir() else 0

    def _run_dirs() -> int:
        root = _copy_root(pd, slot, "T-9305")
        return len(list(root.iterdir())) if root.is_dir() else 0

    def _unterminated_ledger_rows() -> int:
        rows = [row for row in _ledger_rows(pm_home) if row["ticket"] == "T-9305"]
        return sum(1 for row in rows if row.get("harvested_at") is None)

    before = (_round_files(), _run_dirs(), _unterminated_ledger_rows())
    assert before == (0, 0, 0)

    with pytest.raises(OSError, match="probe: board_lock"):
        pd.prepare_ticket_copy(
            ticket="T-9305", role="developer", cwd=slot, pm_home=pm_home,
        )

    after = (_round_files(), _run_dirs(), _unterminated_ledger_rows())
    assert after == (1, 1, 0), (
        f"락 이탈 실패 뒤 board 만 남는 복구 불능 형상이 됐다 — before={before} after={after}"
    )
    err = capsys.readouterr().err
    assert "예약한 board 라운드는 남습니다" in err and "T-9305" in err, (
        f"진단 메시지에 board 복구 좌표가 실려야 한다 — err={err}"
    )


# ── T-0846 라운드 5 fix — F-001 잔여(post-reservation 진단 좌표 초기화) ───────────────────


def test_board_relative_path_failure_after_reserve_preserves_original_exception(
    pd, rounds_env, monkeypatch, capsys,
):
    """F-001(라운드 5) — `reserve_round` 성공 뒤 `_board_relative_path` 가 계약형
    `DelegateError` 를 던져도(PM 홈 밖 해소) 그 원 예외가 그대로 전파된다(원래는 `board_rel`
    이 미초기화라 복구 분기가 `UnboundLocalError` 로 원 예외를 덮었다 — review 재현: 세 축
    `(1,1,0)`·복구 경고 없음). fallback 좌표(`str(board_path)`)로 진단 경고가 나가고 세 축은
    여전히 `(1,1,0)` 이다(run-dir 은 보존 — F-002 와 같은 이유)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-9306")

    def _raise(*_a, **_k):
        raise pd.DelegateError("probe: board 라운드 경로가 PM 홈 밖으로 해소됨")

    monkeypatch.setattr(pd, "_board_relative_path", _raise)

    def _round_files() -> int:
        rounds_dir = _rounds_dir(pm_home, "T-9306")
        return len(list(rounds_dir.glob("*.md"))) if rounds_dir.is_dir() else 0

    def _run_dirs() -> int:
        root = _copy_root(pd, slot, "T-9306")
        return len(list(root.iterdir())) if root.is_dir() else 0

    def _unterminated_ledger_rows() -> int:
        rows = [row for row in _ledger_rows(pm_home) if row["ticket"] == "T-9306"]
        return sum(1 for row in rows if row.get("harvested_at") is None)

    before = (_round_files(), _run_dirs(), _unterminated_ledger_rows())
    assert before == (0, 0, 0)

    with pytest.raises(pd.DelegateError, match="probe: board 라운드 경로"):
        pd.prepare_ticket_copy(
            ticket="T-9306", role="developer", cwd=slot, pm_home=pm_home,
        )

    after = (_round_files(), _run_dirs(), _unterminated_ledger_rows())
    assert after == (1, 1, 0), (
        f"_board_relative_path 실패 뒤 세 축이 review 재현값과 달라졌다 — "
        f"before={before} after={after}"
    )
    err = capsys.readouterr().err
    assert "예약한 board 라운드는 남습니다" in err and "T-9306" in err, (
        f"fallback 좌표(raw board_path)로 진단 경고가 나가야 한다(원래는 경고 자체가 없었다) "
        f"— err={err}"
    )


def test_parse_round_filename_returning_none_does_not_crash_prepare(
    pd, rounds_env, monkeypatch, capsys,
):
    """F-001(라운드 5) — `reserve_round` 가 만든 파일명을 `parse_round_filename` 이 파싱하지
    못해(`None`) 도(방어적 seam — 정상 운영에선 발생하지 않는다) `ordinal, _role = None` 언패킹
    으로 `TypeError`/`UnboundLocalError` 크래시하지 않는다. fallback(`ordinal=0`)이 쓰여
    이후 장부 스키마 검증(`ordinal>=1`)이 **제어된** `DelegateError` 로 loud 하게 거부하고,
    그 진단 메시지 자체도(`_reserved_round_residue`) fallback 값으로 크래시 없이 조립된다 —
    가짜 성공으로 조용히 넘어가지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-9307")

    rounds_module = pd._load_ticket_rounds()
    real_parse = rounds_module.parse_round_filename

    def _parse_none_for_target(name):
        if name == "01-developer.md":
            return None
        return real_parse(name)

    monkeypatch.setattr(rounds_module, "parse_round_filename", _parse_none_for_target)

    with pytest.raises(pd.DelegateError, match="순번 0") as excinfo:
        pd.prepare_ticket_copy(
            ticket="T-9307", role="developer", cwd=slot, pm_home=pm_home,
        )
    assert not isinstance(excinfo.value, (TypeError, UnboundLocalError))
    assert "예약한 board 라운드는 남습니다" in str(excinfo.value)
    assert "T-9307" in str(excinfo.value)  # 실제 board_rel(정상 계산)이 실렸다 — fallback 아님


def _design_spec_text(
    ticket: str, *, design: str | None, section: str = "",
    raw_design_line: str | None = None,
) -> str:
    """`_spec_text` 골격이되 `design:` 값과 `## 설계` 절을 값으로 바꾼다.

    `design=None` 은 필드 자체를 뺀다(구세대 티켓 재현). `raw_design_line` 은 YAML 을 깨는
    형상(콜론을 인용 없이 쓴 스칼라)을 frontmatter 에 그대로 박는다 — 그때 `design` 은 무시된다.
    `json.dumps` 로 값을 인용해 콜론·따옴표가 든 값도 안전하게 싣는다.
    """
    if raw_design_line is not None:
        design_line = raw_design_line
    elif design is not None:
        design_line = f"design: {json.dumps(design, ensure_ascii=False)}\n"
    else:
        design_line = ""
    return (
        "---\n"
        f"id: {ticket}\n"
        "title: 설계 근거 게이트\n"
        "status: claimed\n"
        "created: '2026-08-18'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        "claimed_at: '2026-08-18T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: medium\n"
        + design_line
        + "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\n설계 근거 게이트 검증.\n"
        + (f"\n{section}\n" if section else "")
    )


def _write_design_spec(
    tickets: Path, ticket: str, *, rounds=("developer",), **kwargs,
) -> Path:
    path = tickets / f"{ticket}-rounds.md"
    path.write_text(_design_spec_text(ticket, **kwargs), encoding="utf-8", newline="\n")
    _declare_rounds(tickets, ticket, rounds)
    return path


def _harvest_architect_round(pd, pm_home: Path, slot: Path, ticket: str) -> None:
    """실 준비→편집→회수로 architect 라운드 1개를 board 에 실산출로 남긴다(조립 dict 아님)."""
    plan = pd.prepare_ticket_copy(
        ticket=ticket, role="architect", cwd=slot, pm_home=pm_home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n실측 근거\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


# ── 정상 경로(오탐 0 · I2) ───────────────────────────────────────────────────

def test_design_gate_passes_after_a_harvested_architect_round(pd, rounds_env):
    """근거 ①(회수된 architect 라운드) — fix 라운드(2·3회차)에서도 근거는 소멸하지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-8100"
    _write_design_spec(
        tickets, ticket, rounds=("architect", "developer", "developer"),
        design="n/a")
    _harvest_architect_round(pd, pm_home, slot, ticket)

    first = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
    )
    assert first.board_path.name == "02-developer.md"
    first.path.write_text(
        first.path.read_text(encoding="utf-8") + "\n## 변경 파일\n- x\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=first.path, cwd=slot, pm_home=pm_home)

    second = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
    )
    assert second.board_path.name == "03-developer.md"


def test_design_gate_passes_with_done_and_a_filled_design_section(pd, rounds_env):
    """근거 ②(`design: done` + 설계 절 4항목 충전) — architect 라운드 없이도 통과한다."""
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-8101"
    _write_design_spec(tickets, ticket, design="done", section=_FILLED_DESIGN_SECTION)

    plan = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
    )
    assert plan.board_path.name == "01-developer.md"


def test_design_gate_rejects_the_abolished_exemption_value(pd, rounds_env):
    """폐지된 면제 값은 근거가 아니라 **인식 불가**다 — 설계 단계를 건너뛰는 길이 없다."""
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-8102"
    _write_design_spec(tickets, ticket, design="waived: 검증 스코프 밖")

    with pytest.raises(pd.DelegateError, match="design 값 인식 불가"):
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )
    assert [row for row in _ledger_rows(pm_home) if row["ticket"] == ticket] == []


def test_design_gate_rejects_a_seed_only_architect_round_then_passes_after_harvest(
    pd, rounds_env,
):
    """I3 — 시드 그대로인 architect 라운드는 근거가 아니다. 같은 라운드를 회수하면 같은
    호출이 통과로 뒤집힌다(값으로)."""
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-8103"
    _write_design_spec(tickets, ticket, rounds=("architect", "developer"), design="n/a")
    architect = pd.prepare_ticket_copy(
        ticket=ticket, role="architect", cwd=slot, pm_home=pm_home,
    )

    with pytest.raises(pd.DelegateError, match="developer 라운드 준비 거부") as caught:
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )
    assert "architect 라운드를 회수" in str(caught.value)
    # I8 — 거부는 board·장부에 잔여를 남기지 않는다(시드 architect 라운드 파일 1개만 존재).
    assert sorted(
        item.name for item in _rounds_dir(pm_home, ticket).iterdir()
    ) == ["01-architect.md"]
    assert [row["ticket"] for row in _ledger_rows(pm_home) if row["role"] == "developer"] == []

    architect.path.write_text(
        architect.path.read_text(encoding="utf-8") + "\n실측 근거\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=architect.path, cwd=slot, pm_home=pm_home)

    developer = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
    )
    assert developer.board_path.name == "02-developer.md"


# ── 실패 경로(I1·I9) — 기록 없는 건너뜀 0 ────────────────────────────────────

@pytest.mark.parametrize(
    ("ticket", "kwargs"),
    [
        ("T-8110", dict(design="n/a")),
        ("T-8111", dict(design=None)),
        ("T-8112", dict(design="required")),
        ("T-8113", dict(design="done")),   # section="" — 설계 절 미충전
    ],
    ids=["na-no-rounds", "field-absent", "required", "done-unfilled"],
)
def test_design_gate_rejects_when_no_evidence_is_on_record(
    pd, rounds_env, ticket, kwargs,
):
    pm_home, slot, tickets, _sync = rounds_env
    _write_design_spec(tickets, ticket, **kwargs)
    copy_root = _copy_root(pd, slot, ticket)
    slot_before = set(copy_root.iterdir()) if copy_root.exists() else set()

    with pytest.raises(pd.DelegateError, match="developer 라운드 준비 거부") as caught:
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )

    message = str(caught.value)
    # 사유에 해소 수단 2종이 실린다(architect 회수·done+절 충전). 면제 항목은 폐지됐다.
    assert "architect 라운드를 회수" in message
    assert "design: done" in message and "설계" in message
    assert "waived" not in message
    assert not _rounds_dir(pm_home, ticket).exists() or list(
        _rounds_dir(pm_home, ticket).iterdir()
    ) == []
    assert [row for row in _ledger_rows(pm_home) if row["ticket"] == ticket] == []
    slot_after = set(copy_root.iterdir()) if copy_root.exists() else set()
    assert slot_after == slot_before  # 슬롯 run-dir 잔여 0


@pytest.mark.parametrize(
    ("ticket", "design"),
    [("T-8114", "waived"), ("T-8115", "waived: 리뷰 상한 초과")],
    ids=["bare", "with-reason"],
)
def test_design_gate_rejects_the_abolished_exemption_value_in_both_shapes(
    pd, rounds_env, ticket, design,
):
    """폐지된 면제 값은 사유 유무와 무관하게 통과가 아니다(`_design_state` 가 invalid 로 거부)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_design_spec(tickets, ticket, design=design)
    board = pd._load_board()
    assert board._design_state(design) == board.DESIGN_INVALID

    with pytest.raises(pd.DelegateError, match="design 값 인식 불가"):
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )


def test_design_gate_rejects_unparseable_design_scalar_without_crashing(pd, rounds_env):
    """I9 — 콜론을 인용 없이 쓴 스칼라(엔진이 이미 문서화한 손상 모드)는 판정불능이라
    통과가 아니라 제어된 거부다(`yaml.YAMLError` 가 traceback 으로 새지 않는다)."""
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-8116"
    _write_design_spec(
        tickets, ticket, design=None,
        raw_design_line="design: waived: 인용 없는 콜론 스칼라\n",
    )

    with pytest.raises(pd.DelegateError, match="명세 파싱 실패") as caught:
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )
    assert "developer 라운드 준비 거부" in str(caught.value)


# ── F-001(fix 라운드 4) — architect shortcut 이 판정불능을 통과로 삼키면 안 된다 ─────

def test_design_gate_rejects_malformed_frontmatter_even_with_a_harvested_architect_round(
    pd, rounds_env,
):
    """회수된 architect 라운드(근거 ①)가 있어도 명세 파싱 실패는 판정불능이라 통과가
    아니다 — frontmatter 파싱은 architect shortcut 보다 먼저 수행된다."""
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-8118"
    _write_design_spec(tickets, ticket, rounds=("architect", "developer"), design="n/a")
    _harvest_architect_round(pd, pm_home, slot, ticket)
    _write_design_spec(
        tickets, ticket, rounds=("architect", "developer"), design=None,
        raw_design_line="design: waived: 인용 없는 콜론 스칼라\n",
    )

    with pytest.raises(pd.DelegateError, match="명세 파싱 실패") as caught:
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )
    assert "developer 라운드 준비 거부" in str(caught.value)


def test_design_gate_rejects_invalid_design_even_with_a_harvested_architect_round(
    pd, rounds_env,
):
    """회수된 architect 라운드(근거 ①)가 있어도 `design` 값 인식 불가는 판정불능이라
    통과가 아니다 — design 유효성 검사는 architect shortcut 보다 먼저 수행된다."""
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-8119"
    _write_design_spec(tickets, ticket, rounds=("architect", "developer"), design="n/a")
    _harvest_architect_round(pd, pm_home, slot, ticket)
    # 폐지된 면제 값 = invalid
    _write_design_spec(
        tickets, ticket, rounds=("architect", "developer"), design="waived")

    with pytest.raises(pd.DelegateError, match="design 값 인식 불가"):
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )


# ── I7 — 다른 역할·다른 면은 무영향 ───────────────────────────────────────────

def test_design_gate_does_not_apply_to_non_developer_roles(pd, rounds_env):
    """근거 0(design: n/a·rounds 0)이어도 architect 준비는 막히지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    ticket = "T-8117"
    _write_design_spec(tickets, ticket, rounds=("architect",), design="n/a")

    architect = pd.prepare_ticket_copy(
        ticket=ticket, role="architect", cwd=slot, pm_home=pm_home,
    )
    assert architect.board_path.name == "01-architect.md"


# ── I4·I5 — 진입점 파리티(판정 1개소·rc 정책 1개소) ───────────────────────────

def test_all_three_entry_points_report_the_identical_rejection_reason(
    pd, refund_env, monkeypatch, capsys,
):
    """세 진입점(ticket prepare·cross 자동 준비·board section-add)이 같은 사유 문자열을
    낸다 — 판정이 한 seam 에만 있다는 것의 값 형태(I4). 사유는 tid 를 싣지 않으므로(시드
    seam 은 실 ticket id 를 모른다) 서로 다른 티켓이어도 문자열은 동일하다."""
    home, tickets = refund_env
    _write_design_spec(tickets, "T-8140", design="n/a")

    with pytest.raises(pd.DelegateError) as caught1:
        pd.prepare_ticket_copy(
            ticket="T-8140", role="developer", cwd=home, pm_home=home,
        )
    message1 = str(caught1.value)

    board_fixture = _fixture_board(pd, home, [])
    rc3 = board_fixture.main(["section-add", "T-8140", "--role", "developer"])
    err3 = capsys.readouterr().err
    assert rc3 == 1
    assert err3.rstrip("\n") == f"cannot section-add: {message1}"

    capsys.readouterr()
    _write_design_spec(tickets, "T-8141", design="n/a")
    prompt = home / "task.md"
    prompt.write_text("작업 내용", encoding="utf-8")
    rc2 = pd.main(
        ["--role", "developer", "--prompt-file", str(prompt), "--cwd", str(home),
         "--ticket", "T-8141", "--output-dir", str(home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("설계 근거 거부 뒤 스폰되면 안 됨"),
    )
    assert rc2 == 1
    err2 = capsys.readouterr().err
    assert message1 in err2


# ── 민감도 — 판정 호출을 빼면 red, 강제하면 정상 통과도 red ──────────────────

def test_disabling_the_design_gate_turns_every_rejection_shape_green(
    pd, rounds_env, monkeypatch,
):
    """판정 함수를 항상-통과로 치환하면 거부 형상이 rc=0 으로 뒤집힌다 — 판정 호출을
    빼면 red 라는 것의 역방향 값 형태. entry 1·2(같은 `pd` 그래프)는 `pd._load_board()`,
    entry 3(별도 board 사본)는 그 사본의 delegate 체인에서 **같은 자리**를 무력화한다."""
    pm_home, slot, tickets, sync_log = rounds_env
    monkeypatch.setattr(
        pd._load_board(), "design_evidence_problem", lambda *a, **k: None,
    )
    board_fixture = _fixture_board(pd, pm_home, sync_log)
    board_via_delegate = (
        board_fixture._load_ticket_rounds()._load_pm_delegate()._load_board()
    )
    monkeypatch.setattr(
        board_via_delegate, "design_evidence_problem", lambda *a, **k: None,
    )

    _write_design_spec(tickets, "T-8130", design="n/a")
    plan = pd.prepare_ticket_copy(
        ticket="T-8130", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert plan.board_path.name == "01-developer.md"

    _write_design_spec(tickets, "T-8131", design="n/a")
    rc = board_fixture.main(["section-add", "T-8131", "--role", "developer"])
    assert rc == 0


def test_forcing_the_design_gate_turns_every_pass_shape_red(pd, rounds_env, monkeypatch):
    """판정 함수를 항상-거부로 치환하면 정상 통과 형상(기본 `design: done` + 설계 절)도
    red 로 뒤집힌다 — 세 진입점이 같은 함수를 지난다는 것의 값 형태."""
    pm_home, slot, tickets, sync_log = rounds_env

    def _always_block(*_args, **_kwargs):
        return "test: 강제 거부"

    monkeypatch.setattr(pd._load_board(), "design_evidence_problem", _always_block)
    board_fixture = _fixture_board(pd, pm_home, sync_log)
    board_via_delegate = (
        board_fixture._load_ticket_rounds()._load_pm_delegate()._load_board()
    )
    monkeypatch.setattr(board_via_delegate, "design_evidence_problem", _always_block)

    _write_spec(tickets, "T-8132")
    with pytest.raises(pd.DelegateError, match="강제 거부"):
        pd.prepare_ticket_copy(
            ticket="T-8132", role="developer", cwd=slot, pm_home=pm_home,
        )

    _write_spec(tickets, "T-8133")
    rc = board_fixture.main(["section-add", "T-8133", "--role", "developer"])
    assert rc == 1


# ── cross 역할 수동 prepare 거부 [[T-0855]] ──────────────────────────────────
#
# `ticket prepare --role R`은 R 이 cross(PM 하네스 ≠ conf 매핑 하네스)면 rc≠0 으로 거부한다 —
# 통과시키면 cross 실 실행(`--ticket`)이 내부에서 다시 prepare 해 고아 시드가 남는다. 판정은
# `delegate_channel_guard.decide`(native Agent 위임 훅과 같은 seam) 하나 — 거부는 verdict=="deny"
# 뿐이고, 판정불능(PM 하네스 미상·매핑 미설정·해소 실패)은 fail-open 이되 stderr 경고 1줄을 낸다.

_HARNESS_MARKER_KEYS = ("CLAUDECODE", "CODEX_THREAD_ID", "CODEX_CI", "OPENCODE", "OPENCODE_PID")


def _isolate_harness_env(monkeypatch) -> None:
    """실측 세션 마커를 전부 지운 뒤 각 테스트가 필요한 것만 켠다(ambient 오염 차단)."""
    for key in _HARNESS_MARKER_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_local_conf(pm_home: Path, conf: dict[str, str]) -> Path:
    path = pm_home / ".project_manager" / "local.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in conf.items()) + "\n",
        encoding="utf-8", newline="",
    )
    return path


def _prepare_cli(pd, slot: Path, ticket: str, role: str, *, tier: str | None = None) -> int:
    argv = ["prepare", "--ticket", ticket, "--role", role, "--cwd", str(slot)]
    if tier is not None:
        argv += ["--tier", tier]
    return pd._cmd_ticket(argv)


def test_cross_role_prepare_is_denied_with_no_board_or_ledger_side_effect(
        pd, rounds_env, monkeypatch, capsys):
    """실 ② PM 홈 형상(code-reviewer=codex) 재현 — cross 수동 prepare 는 rc≠0·부작용 0."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-5.6-sol",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7900")

    rc = _prepare_cli(pd, slot, "T-7900", "code-reviewer")

    captured = capsys.readouterr()
    assert rc != 0
    assert "codex" in captured.err and "--ticket" in captured.err
    assert not _rounds_dir(pm_home, "T-7900").exists()
    assert _ledger_rows(pm_home) == []
    assert not _copy_root(pd, slot, "T-7900").exists()


def test_native_role_prepare_is_unaffected_by_a_cross_mapping_elsewhere(
        pd, rounds_env, monkeypatch, capsys):
    """역방향 — developer=claude(native)는 code-reviewer=codex(cross) conf 옆에서도 현행 그대로
    (prepare→harvest 왕복 green)."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-5.6-sol",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7901")

    rc = _prepare_cli(pd, slot, "T-7901", "developer")
    assert rc == 0
    plan_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    copy_path = Path(plan_json["copy"])
    copy_path.write_text(
        copy_path.read_text(encoding="utf-8") + "\n## 산출\n- 값\n", encoding="utf-8", newline="",
    )

    harvest_rc = pd._cmd_ticket(["harvest", "--copy", str(copy_path), "--cwd", str(slot)])

    assert harvest_rc == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["changed"] is True


def test_prepare_denial_follows_conf_not_role_name(pd, rounds_env, monkeypatch, capsys):
    """같은 명령이 conf 를 바꾸면 판정도 바뀐다 — 역할 이름을 하드코딩하지 않았다는 고정."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7902", rounds=("code-reviewer",))

    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-5.6-sol",
    })
    assert _prepare_cli(pd, slot, "T-7902", "code-reviewer") != 0
    capsys.readouterr()

    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "claude",
        "delegate.code-reviewer.model": "opus",
    })
    assert _prepare_cli(pd, slot, "T-7902", "code-reviewer") == 0


def test_tier_hard_is_denied_when_only_hard_maps_cross(pd, rounds_env, monkeypatch, capsys):
    """tier 양방향(a) — normal 은 native, hard 만 cross 면 `--tier hard` 만 거부된다."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
        "delegate.developer.hard.harness": "codex",
        "delegate.developer.hard.model": "gpt-5.6-sol",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7903")

    assert _prepare_cli(pd, slot, "T-7903", "developer") == 0        # --tier 생략 = normal
    capsys.readouterr()
    rc = _prepare_cli(pd, slot, "T-7903", "developer", tier="hard")

    captured = capsys.readouterr()
    assert rc != 0
    assert "codex" in captured.err


def test_tier_hard_passes_when_only_normal_maps_cross(pd, rounds_env, monkeypatch, capsys):
    """tier 양방향(b) — 역형상(hard 만 native)에서 hard 는 통과하고 생략(normal)은 거부된다
    (false-deny 부재 고정)."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-5.6-sol",
        "delegate.developer.hard.harness": "claude",
        "delegate.developer.hard.model": "opus",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7904")

    assert _prepare_cli(pd, slot, "T-7904", "developer", tier="hard") == 0
    capsys.readouterr()
    assert _prepare_cli(pd, slot, "T-7904", "developer") != 0        # --tier 생략 = normal = cross


def test_fail_open_when_pm_harness_marker_is_absent(pd, rounds_env, monkeypatch, capsys):
    """fail-open(a) — 세션 마커가 하나도 없으면(PM 하네스 미상) 통과하되 침묵하지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-5.6-sol",
    })
    _isolate_harness_env(monkeypatch)
    _write_spec(tickets, "T-7905", rounds=("code-reviewer",))

    rc = _prepare_cli(pd, slot, "T-7905", "code-reviewer")

    captured = capsys.readouterr()
    assert rc == 0
    assert "fail-open" in captured.err


def test_fail_open_when_pm_harness_markers_collide(pd, rounds_env, monkeypatch, capsys):
    """fail-open(b) — 중첩 세션(마커 2개 동시 일치)도 통과하되 침묵하지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-5.6-sol",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("OPENCODE", "1")
    _write_spec(tickets, "T-7906", rounds=("code-reviewer",))

    rc = _prepare_cli(pd, slot, "T-7906", "code-reviewer")

    captured = capsys.readouterr()
    assert rc == 0
    assert "fail-open" in captured.err


def test_fail_open_and_not_silent_when_no_delegate_mapping_exists(
        pd, rounds_env, monkeypatch, capsys):
    """fail-open(c) — conf 에 `delegate.*` 매핑이 아예 없어도(새 설치) 침묵 통과가 아니다."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7907", rounds=("code-reviewer",))

    rc = _prepare_cli(pd, slot, "T-7907", "code-reviewer")

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err.strip() != ""


def test_master_switch_off_denies_native_role_prepare_before_any_side_effect(
        pd, rounds_env, monkeypatch, capsys):
    """F-002 — 마스터 스위치(`delegate.enabled=false`)는 owner conf 로 판정하고, 채널
    (native/cross) 무관하게 prepare 전 rc=3·부작용 0 으로 거부한다(모듈 계약과
    tests/test_local_conf_notation.py 의 off 계약을 따른다). 침묵하지도 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "false",
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7909")

    rc = _prepare_cli(pd, slot, "T-7909", "developer")

    captured = capsys.readouterr()
    assert rc == 3
    assert "위임이 꺼져 있습니다" in captured.err
    assert not _rounds_dir(pm_home, "T-7909").exists()
    assert _ledger_rows(pm_home) == []
    assert not _copy_root(pd, slot, "T-7909").exists()


def test_prepare_denies_using_owner_conf_even_when_engine_copy_conf_lacks_mapping(
        pd, rounds_env, monkeypatch, capsys, tmp_path):
    """conf provenance — ① 사본 실행 형상 재현. 실행 엔진 사본(REPO) conf 에 역할 매핑이 없어도
    owner(PM 홈) conf 로 deny 된다(실측: REPO 로 읽으면 매핑 없는 사본에서 조용히 no-op)."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    engine_copy = tmp_path / "engine-copy-without-mapping"
    engine_copy.mkdir()
    monkeypatch.setattr(pd, "REPO", engine_copy)
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", None)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-5.6-sol",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7908")

    # 선-단언 — REPO(엔진 사본) conf 로 읽으면 매핑이 비어 no-op 이 된다는 전제 자체를 확인한다.
    assert pd.local_config() == {}

    rc = _prepare_cli(pd, slot, "T-7908", "code-reviewer")

    assert rc != 0
    assert _ledger_rows(pm_home) == []


def test_master_switch_denial_follows_owner_conf_not_engine_copy_conf(
        pd, rounds_env, monkeypatch, capsys, tmp_path):
    """F-002 — 마스터 스위치도 conf provenance 는 owner 단일 진실이다(I3 확장). 엔진 사본
    (REPO) conf 가 스위치를 아예 모르는 상태에서, owner off 는 여전히 rc=3·부작용 0 으로
    거부하고(정형) owner on 은 여전히 통과한다(역방향 — 엔진 사본 conf 는 판정에 관여하지
    않는다는 것의 값 형태)."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    engine_copy = tmp_path / "engine-copy-without-switch"
    engine_copy.mkdir()
    monkeypatch.setattr(pd, "REPO", engine_copy)
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", None)
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")

    # 선-단언 — REPO(엔진 사본) conf 로 읽으면 스위치 설정이 아예 없다는 전제 자체를 확인한다.
    assert pd.local_config() == {}

    _write_local_conf(pm_home, {
        "delegate.enabled": "false",
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    })
    _write_spec(tickets, "T-7910")
    rc = _prepare_cli(pd, slot, "T-7910", "developer")
    captured = capsys.readouterr()
    assert rc == 3
    assert "위임이 꺼져 있습니다" in captured.err
    assert not _rounds_dir(pm_home, "T-7910").exists()
    assert _ledger_rows(pm_home) == []

    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    })
    _write_spec(tickets, "T-7911")
    rc2 = _prepare_cli(pd, slot, "T-7911", "developer")
    assert rc2 == 0


def test_cross_role_prepare_denial_survives_a_reworded_deny_reason(
        pd, rounds_env, monkeypatch, capsys):
    """F-003 — cross deny 판정은 verdict(+harness/model 이 비어 있지 않음) 구조 필드로 소비한다.
    가드의 사유 문자열 접두만 바뀌어도(reason.startswith 분기 부재 고정) 거부는 warning+prepare 로
    강등되지 않는다."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-5.6-sol",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7912")

    guard = pd._load_delegate_channel_guard()
    real_decide = guard.decide

    def _reworded_decide(role, tier, conf, self_harness):
        result = real_decide(role, tier, conf, self_harness)
        if result.get("verdict") == "deny" and result.get("harness"):
            result = dict(result, reason="문구가 완전히 바뀐 새 사유")
        return result

    monkeypatch.setattr(guard, "decide", _reworded_decide)

    rc = _prepare_cli(pd, slot, "T-7912", "code-reviewer")

    captured = capsys.readouterr()
    assert rc != 0
    assert "문구가 완전히 바뀐 새 사유" in captured.err
    assert not _rounds_dir(pm_home, "T-7912").exists()
    assert _ledger_rows(pm_home) == []


def test_reject_cross_role_prepare_does_not_misclassify_a_master_switch_deny_as_cross(
        pd, monkeypatch, capsys):
    """역방향 — `verdict=="deny"` 이되 harness/model 이 둘 다 비어 있는 스위치-off deny(Row 0.5)는
    cross-harness 오판(`DelegateError`)으로 잘못 분류되지 않는다. `_cmd_ticket` 의 선행 게이트가
    실무에서는 이 경로에 닿기 전에 이미 걸러내지만, 판정식 `_reject_cross_role_prepare` 자체의
    구조적 안전판(harness/model 비어 있음 = cross 아님)을 직접 고정한다."""
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    conf = {"delegate.enabled": "false"}

    pd._reject_cross_role_prepare("developer", "normal", conf)  # raise 없이 통과해야 한다

    captured = capsys.readouterr()
    assert "위임이 꺼져 있습니다" in captured.err


def test_fail_open_and_single_stderr_line_when_the_guard_module_fails_to_load(
        pd, rounds_env, monkeypatch, capsys):
    """F-004 — 가드 로드 예외(개행 포함)를 직접 주입해도 rc=0·prepare 성공·stderr 침묵 없이
    정확히 물리 1행으로 접힌다(예외 텍스트의 CR/LF 를 공백으로 정규화)."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7913")

    def _boom():
        raise RuntimeError("가드 로드 실패\r\n2행째")

    monkeypatch.setattr(pd, "_load_delegate_channel_guard", _boom)

    rc = _prepare_cli(pd, slot, "T-7913", "developer")

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err.count("\n") == 1
    assert "가드 로드 실패 2행째" in captured.err
    plan_json = json.loads(captured.out.strip().splitlines()[-1])
    assert Path(plan_json["copy"]).exists()


def test_fail_open_and_single_stderr_line_when_decide_raises(
        pd, rounds_env, monkeypatch, capsys):
    """F-004 — decide() 실행 예외(개행 포함)를 직접 주입해도 rc=0·prepare 성공·stderr 침묵 없이
    정확히 물리 1행으로 접힌다."""
    pm_home, slot, tickets, _sync = rounds_env
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    _write_local_conf(pm_home, {
        "delegate.enabled": "true",
        "delegate.developer.harness": "claude",
        "delegate.developer.model": "opus",
    })
    _isolate_harness_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _write_spec(tickets, "T-7914")

    guard = pd._load_delegate_channel_guard()

    def _boom(*_a, **_k):
        raise RuntimeError("decide 실행 실패\r\n2행째")

    monkeypatch.setattr(guard, "decide", _boom)

    rc = _prepare_cli(pd, slot, "T-7914", "developer")

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err.count("\n") == 1
    assert "decide 실행 실패 2행째" in captured.err
    plan_json = json.loads(captured.out.strip().splitlines()[-1])
    assert Path(plan_json["copy"]).exists()
