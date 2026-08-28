"""kill 된 위임 잔여를 명시적으로 정리하는 `ticket abandon` 의 불변식 픽스처.

세 자산(PM 홈 delegate-rounds 장부 행 · board 라운드 파일 · 슬롯 run-dir)을 전진 수렴
(roll-forward)으로 종결하는 처분이라, 검증은 성공 경로가 아니라 **경계**에 있다.

  I1 산출 보존   — 지우기 직전 관측에서 시드와 다르면 그 자산을 보존하고 rc≠0.
  I2 fail-closed — 소유 pid 가 살아 있으면 명시 확인 없이는 아무것도 바뀌지 않는다.
  I3 단일 처분   — 한 copy 는 회수 또는 포기 중 하나로만 종결된다.
  I4 전진 수렴   — 어느 단계에서 실패하든 같은 명령 재호출이 남은 작업을 끝낸다.
  I5 순번 무결   — 포기 전후로 round-gap·round-dup 이 늘지 않는다.
  I6 락 경계     — board_lock 안에는 순번 판정과 unlink 만 있다(교착 회귀는 실 그래프로).
  I7 신뢰 뿌리   — 인가 경로는 회수와 같다(장부 행·경로 전량 일치·chain plain).
  I8 비목표      — 리뷰 라운드 예산 장부는 건드리지 않는다.

검증 근거는 실 board 트리·실 라운드 파일·실 장부 파일이고, 교착 회귀만 stub 없는 실 호출
그래프를 subprocess 로 태운다.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest

from conftest import write_cluster_ledger

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_DELEGATE = TOOLS / "pm_delegate.py"
LIVE_OUTPUT_MARKER = "LIVE OUTPUT"
# 실 그래프 CLI 는 교착하면 끝나지 않는다 — 시간 상한이 곧 판정이다(교착이면 timeout 예외).
CLI_WALL_TIMEOUT_SEC = 60.0
# 락 경합 픽스처에서 다른 프로세스가 board.lock 을 쥐고 있는 시간.
BOARD_LOCK_HOLD_SEC = 2.0


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_abandon", PM_DELEGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load_pd()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def _git_identity_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "abandon", "GIT_AUTHOR_EMAIL": "abandon@test.invalid",
        "GIT_COMMITTER_NAME": "abandon", "GIT_COMMITTER_EMAIL": "abandon@test.invalid",
    })
    return env


def _spec_text(ticket: str, *, status: str = "claimed") -> str:
    return (
        "---\n"
        f"id: {ticket}\n"
        "title: abandon 왕복\n"
        f"status: {status}\n"
        "created: '2026-08-22'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        "claimed_at: '2026-08-22T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "design: done\n"
        "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\nabandon 왕복 픽스처.\n\n"
        "## 설계\n"
        "- **경계 실측**: 기계 테스트 픽스처\n"
        "- **불변식**: 이 파일의 축 밖\n"
        "- **표면 상한**: 픽스처 1건\n"
        "- **테스트 전략**: 정상·실패 경로\n"
    )


class Env(NamedTuple):
    """PM 홈(board 데이터+엔진 사본)과 ignore 된 슬롯 git 트리 한 쌍."""

    pm_home: Path
    slot: Path
    tickets: Path
    sync_calls: list
    failures: dict


def _fixture_board(pd, env: Env):
    board = pd._load_module_from_path(
        env.pm_home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = env.pm_home
    board.LOCAL_DIR = env.pm_home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"

    def _sync(message, paths):
        # 단위 속도용 stub 이다 — 실 호출 그래프(교착 회귀)는 CLI subprocess 픽스처가 태운다.
        if env.failures.get("sync"):
            raise RuntimeError("sync 실패 주입")
        env.sync_calls.append((message, [Path(item) for item in paths]))
        return True

    board._rounds_mutation_sync_paths = _sync
    return board


@pytest.fixture
def env(tmp_path, pd, monkeypatch) -> Env:
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
    monkeypatch.setenv("GIT_AUTHOR_NAME", "abandon")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "abandon@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "abandon")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "abandon@test.invalid")
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    prepared = Env(pm_home, slot, tickets, [], {})
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, prepared),
    )
    return prepared


_FIXED_ROUNDS = ("architect", "developer", "code-reviewer", "developer")


def _write_spec(env: Env, ticket: str, *, rounds=_FIXED_ROUNDS, **kwargs) -> Path:
    """명세와 그 티켓의 크기 1 묶음 장부를 함께 쓴다.

    abandon 축과 무관하게 장부 예산은 제품의 고정 수열만 쓴다. ``rounds`` 인자는 옛 fixture
    호출부 호환용이며 값을 바꿔도 가변 예산을 되살리지 않는다.
    """
    path = env.tickets / f"{ticket}-abandon.md"
    path.write_text(_spec_text(ticket, **kwargs), encoding="utf-8", newline="\n")
    write_cluster_ledger(
        env.pm_home / ".project_manager" / "wiki", ticket,
        base_branch="task/main", rounds=_FIXED_ROUNDS,
    )
    return path


def _prepare(pd, env: Env, ticket: str, role: str = "architect", **kwargs):
    return pd.prepare_ticket_copy(
        ticket=ticket, role=role, cwd=env.slot, pm_home=env.pm_home, **kwargs,
    )


def _fill_architect(pd, plan) -> None:
    """architect 사본의 계약 placeholder 를 harvest 가능한 실값으로 채운다."""
    text = plan.path.read_text(encoding="utf-8")
    opening = f"```{pd.ARCHITECT_TEST_BLOCK}\n"
    start = text.index(opening) + len(opening)
    end = text.index("\n```", start)
    contract = json.dumps({
        "version": 1,
        "tests": [{
            "id": "AT-001",
            "target": "tests/test_ticket_abandon.py",
            "command": "python3 -m pytest tests/test_ticket_abandon.py -q",
            "expected": "passed",
            "negative": "abandon 자산 누락을 거부한다",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    plan.path.write_text(
        text[:start] + contract + text[end:] + "\n## 실 산출\n- architect 계약\n",
        encoding="utf-8", newline="",
    )


def _land_architect(pd, env: Env, plan) -> None:
    """고정 수열의 다음 단계 준비가 가능하도록 architect 계약을 실값으로 회수한다."""
    _fill_architect(pd, plan)
    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=env.slot, pm_home=env.pm_home,
    )
    assert result.changed is True


def _abandon(pd, env: Env, plan, *, assume_dead: bool = True):
    return pd.abandon_ticket_copy(
        copy_path=plan.path, cwd=env.slot, pm_home=env.pm_home, assume_dead=assume_dead,
    )


def _rounds_dir(env: Env, ticket: str) -> Path:
    return env.pm_home / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket


def _ledger_path(env: Env) -> Path:
    return env.pm_home / ".project_manager" / ".local" / "delegate-rounds.jsonl"


def _ledger_rows(env: Env) -> list[dict]:
    path = _ledger_path(env)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _write_output(path: Path, marker: str = LIVE_OUTPUT_MARKER) -> None:
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n{marker}\n", encoding="utf-8", newline="",
    )


def _problem_codes(pd, env: Env, ticket: str) -> list[str]:
    rounds_module = pd._load_ticket_rounds()
    tickets_root = env.pm_home / ".project_manager" / "wiki" / "tickets"
    return [item.code for item in rounds_module.verify_rounds(tickets_root, ticket)]


def _assets(plan, env: Env) -> tuple[bool, bytes | None, bool, dict]:
    """세 자산의 현재 값 — run-dir 존재 · board bytes · 장부 마지막 행."""
    board_bytes = plan.board_path.read_bytes() if plan.board_path.exists() else None
    rows = [row for row in _ledger_rows(env) if Path(row["copy"]) == plan.path]
    return plan.run_dir.exists(), board_bytes, plan.path.exists(), rows[-1]


def _use_relay_pid(pd, monkeypatch, *, alive: bool):
    """생존 조회 seam 만 주입한다 — 나머지 relay 표면은 실 모듈 그대로 쓴다."""
    real = pd._load_relay()

    class _Relay:
        raw_storage_paths = staticmethod(real.raw_storage_paths)
        raw_records = staticmethod(real.raw_records)

        @staticmethod
        def pid_is_alive(_pid):
            return alive

    monkeypatch.setattr(pd, "_load_relay", lambda: _Relay)


def _inject_slot_write(pd, monkeypatch, *, call_index: int, path: Path, before: bool):
    """슬롯 판독을 **감싸서** 지정한 호출 시점에 살아 있는 산출을 착지시킨다.

    대체가 아니라 wrapper 라 실제 호출 그래프는 그대로 돈다. `before=True` 는 그 판독 **직전**
    (앞 단계가 끝난 뒤)에, False 는 판독 **직후**에 쓴다.
    """
    real = pd._read_slot_round_text
    state = {"calls": 0}

    def _land():
        path.write_text(
            path.read_text(encoding="utf-8") + f"\n{LIVE_OUTPUT_MARKER}\n",
            encoding="utf-8", newline="",
        )

    def _wrapped(copy_path):
        state["calls"] += 1
        if state["calls"] == call_index and before:
            _land()
        text = real(copy_path)
        if state["calls"] == call_index and not before:
            _land()
        return text

    monkeypatch.setattr(pd, "_read_slot_round_text", _wrapped)
    return state


# ── 세 자산 종결 (증거 없는 legacy 예약 · 명시 확인) ────────────────────────

def test_abandon_closes_all_three_kill_residue_assets(pd, env):
    """실 prepare 로 만든 세 자산이 명시 확인 뒤 모두 종결된다(legacy 잔여가 실제로 지워진다)."""
    _write_spec(env, "T-8001")
    plan = _prepare(pd, env, "T-8001")
    assert plan.run_dir.exists() and plan.board_path.exists()
    before = _ledger_rows(env)[-1]
    assert Path(before["copy"]) == plan.path and before["harvested_at"] is None
    assert "abandoned_at" not in before and "owner_pid" not in before

    result = _abandon(pd, env, plan)

    assert (result.changed, result.board_removed, result.run_dir_removed) == (
        True, True, True
    )
    assert result.converged is True and result.sync_ready is True
    assert not plan.run_dir.exists()
    assert not plan.board_path.exists()
    row = _ledger_rows(env)[-1]
    assert row["abandoned_at"] is not None and row["harvested_at"] is None
    assert pd.ticket_copy_records(env.pm_home, ticket="T-8001", unharvested=True) == []
    assert env.sync_calls[-1][1] == [plan.board_path]


def test_abandon_lets_reprepare_reuse_the_same_ordinal(pd, env):
    """최대 순번 삭제라 재 prepare 가 같은 순번을 다시 채번한다(삭제 정의의 직접 증거)."""
    _write_spec(env, "T-8002")
    first = _prepare(pd, env, "T-8002")
    _abandon(pd, env, first)

    second = _prepare(pd, env, "T-8002")

    assert second.ordinal == first.ordinal == 1


def test_abandon_leaves_other_live_runs_untouched(pd, env):
    """한 티켓의 kill 잔여를 지워도 다른 티켓의 진행 중 준비는 세 자산 전부 그대로다."""
    _write_spec(env, "T-8003")
    _write_spec(env, "T-8004")
    dead = _prepare(pd, env, "T-8003")
    alive = _prepare(pd, env, "T-8004")

    _abandon(pd, env, dead)

    assert alive.run_dir.exists() and alive.board_path.exists()
    alive_row = [row for row in _ledger_rows(env) if row["ticket"] == "T-8004"][-1]
    assert alive_row["harvested_at"] is None and "abandoned_at" not in alive_row
    assert pd.ticket_copy_records(env.pm_home, ticket="T-8004", unharvested=True)[0][
        "copy"
    ] == str(alive.path)


def test_abandon_refuses_a_round_with_real_output(pd, env):
    """산출이 있으면(시드 그대로가 아니면) harvest 대상이지 abandon 대상이 아니다."""
    _write_spec(env, "T-8005")
    plan = _prepare(pd, env, "T-8005")
    _write_output(plan.path, "실산출")
    before = plan.board_path.read_bytes()

    with pytest.raises(pd.DelegateError, match="산출이 있어 포기할 수 없습니다"):
        _abandon(pd, env, plan)

    assert plan.board_path.read_bytes() == before
    assert plan.run_dir.exists()
    assert "abandoned_at" not in _ledger_rows(env)[-1]
    assert env.sync_calls == []


def test_abandon_refuses_an_unprepared_path(pd, env):
    """신뢰 뿌리는 PM 홈 장부다 — 슬롯이 스스로 자격을 주장할 수 없다(I7)."""
    _write_spec(env, "T-8007")
    plan = _prepare(pd, env, "T-8007")
    smuggled = plan.run_dir / "02-developer.md"
    smuggled.write_text("## 리뷰\n밀반입\n", encoding="utf-8", newline="\n")

    with pytest.raises(pd.DelegateError, match="준비 기록 없음"):
        pd.abandon_ticket_copy(
            copy_path=smuggled, cwd=env.slot, pm_home=env.pm_home, assume_dead=True,
        )

    assert smuggled.exists() and plan.run_dir.exists()


def test_abandon_does_not_touch_review_round_ledgers(pd):
    """비목표(I8) — 리뷰 라운드 예산 환불은 이 처분의 축이 아니다."""
    source = inspect.getsource(pd.abandon_ticket_copy)
    assert "review_rounds" not in source


# ── D1 증거 없음 게이트 (파괴 연산의 기본값은 거부) ─────────────────────────

def test_abandon_without_evidence_is_refused_and_leaves_all_three_assets(pd, env):
    """표식 없는 예약(기존 8키 행 형상)은 명시 확인 없이는 거부다 — 세 자산 불변."""
    _write_spec(env, "T-8020")
    plan = _prepare(pd, env, "T-8020")
    before = _assets(plan, env)

    with pytest.raises(pd.DelegateError, match="종료 증거가 없습니다") as caught:
        _abandon(pd, env, plan, assume_dead=False)

    assert pd.ABANDON_ASSUME_DEAD_FLAG in str(caught.value)
    assert _assets(plan, env) == before
    assert env.sync_calls == []


def test_evidence_refusal_message_carries_the_reservation_coordinates(pd, env):
    """무엇을 지우는지 매번 값으로 보게 한다 — 명시 확인이 습관이 되지 않게."""
    _write_spec(env, "T-8021")
    plan = _prepare(pd, env, "T-8021")
    prepared_at = _ledger_rows(env)[-1]["prepared_at"]

    with pytest.raises(pd.DelegateError) as caught:
        _abandon(pd, env, plan, assume_dead=False)

    message = str(caught.value)
    for coordinate in ("T-8021", f"ordinal={plan.ordinal}", prepared_at, str(plan.path)):
        assert coordinate in message


def test_unfinished_raw_record_is_a_hint_and_not_a_decision_input(pd, env):
    """raw 장부 조인은 휴리스틱이라 참고로만 붙는다 — 그 행이 있어도 판정은 그대로 거부다."""
    _write_spec(env, "T-8022")
    plan = _prepare(pd, env, "T-8022")
    ledger = env.pm_home / ".project_manager" / ".local" / "raw_outputs.json"
    ledger.write_text(json.dumps({"version": 1, "records": [{
        "id": "rawid01", "surface": "delegate", "harness": "codex", "model": "m",
        "role": "architect", "attempt": "1", "pid": 999999999,
        "started_at": "2026-08-22T00:00:00+00:00", "raw_path": str(plan.run_dir),
        "finished_at": None, "ticket": "T-8022",
    }]}), encoding="utf-8", newline="\n")

    with pytest.raises(pd.DelegateError) as caught:
        _abandon(pd, env, plan, assume_dead=False)

    message = str(caught.value)
    assert "참고(판정 입력 아님)" in message and "rawid01" in message
    assert "종료 증거가 없습니다" in message      # 참고가 증거로 승격되지 않는다.
    assert plan.run_dir.exists() and plan.board_path.exists()


# ── D1 생존 fail-closed (owner_pid) ────────────────────────────────────────

def test_live_owner_pid_refusal_leaves_the_three_assets_unchanged(pd, env, monkeypatch):
    """소유 pid 가 살아 있으면 명시 확인 없이는 아무것도 바뀌지 않는다(I2)."""
    _write_spec(env, "T-8030")
    plan = _prepare(pd, env, "T-8030", owner_pid=4242)
    before = _assets(plan, env)
    _use_relay_pid(pd, monkeypatch, alive=True)

    with pytest.raises(pd.DelegateError, match="실행 중이라 포기하지 않습니다") as caught:
        _abandon(pd, env, plan, assume_dead=False)

    assert "pid=4242" in str(caught.value) and "ps -p 4242" in str(caught.value)
    assert _assets(plan, env) == before
    assert env.sync_calls == []


def test_live_owner_pid_is_overridable_only_by_explicit_confirmation(pd, env, monkeypatch):
    """기계가 막은 것을 사람이 이름을 걸고 통과시키는 통로는 명시 확인 하나뿐이다."""
    _write_spec(env, "T-8031")
    plan = _prepare(pd, env, "T-8031", owner_pid=4242)
    _use_relay_pid(pd, monkeypatch, alive=True)

    result = _abandon(pd, env, plan, assume_dead=True)

    assert result.converged is True
    assert not plan.run_dir.exists() and not plan.board_path.exists()


def test_dead_owner_pid_needs_no_explicit_confirmation(pd, env, monkeypatch):
    """대조군 — 기계 증거가 사망을 말하면 플래그 없이 통과한다."""
    _write_spec(env, "T-8032")
    plan = _prepare(pd, env, "T-8032", owner_pid=4242)
    _use_relay_pid(pd, monkeypatch, alive=False)

    result = _abandon(pd, env, plan, assume_dead=False)

    assert result.converged is True
    assert not plan.run_dir.exists() and not plan.board_path.exists()


def test_owner_pid_is_recorded_only_when_the_caller_owns_the_run(pd, env, monkeypatch):
    """표식은 run 소유자만 남긴다 — native 준비 CLI 는 키를 싣지 않는다(부재=증거 없음)."""
    _write_spec(env, "T-8033")
    _write_spec(env, "T-8034")
    owned = _prepare(pd, env, "T-8033", owner_pid=4242)
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: env.pm_home)

    rc = pd._cmd_ticket([
        "prepare", "--ticket", "T-8034", "--role", "architect", "--cwd", str(env.slot),
    ])

    assert rc == 0
    rows = {row["ticket"]: row for row in _ledger_rows(env)}
    assert rows[owned.ticket]["owner_pid"] == 4242
    assert "owner_pid" not in rows["T-8034"]


# ── I1 경합: 사전 read 이후 착지한 산출은 지우지 않는다 ─────────────────────

@pytest.mark.parametrize(
    "ticket,call_index,before",
    [("T-8040", 1, False), ("T-8041", 2, True)],
    ids=["착지=사전-read-직후", "착지=unlink-직후·삭제-직전"],
)
def test_output_landing_before_the_removal_preserves_bytes_and_run_dir(
    pd, env, monkeypatch, ticket, call_index, before,
):
    """살아 있는 agent 가 쓴 산출 bytes 와 run-dir 은 어느 시점에 착지해도 보존된다."""
    _write_spec(env, ticket)
    plan = _prepare(pd, env, ticket)
    _inject_slot_write(
        pd, monkeypatch, call_index=call_index, path=plan.path, before=before,
    )

    with pytest.raises(pd.DelegateError, match="시드와 달라져") as caught:
        _abandon(pd, env, plan)

    assert plan.run_dir.is_dir()
    assert LIVE_OUTPUT_MARKER in plan.path.read_text(encoding="utf-8")
    assert str(plan.path) in str(caught.value)


# ── I4 실패 주입 × 3 → 같은 명령 재호출로 수렴 ─────────────────────────────

def _assert_converged(pd, env: Env, plan, *, board_removed: bool = True) -> None:
    row = [row for row in _ledger_rows(env) if Path(row["copy"]) == plan.path][-1]
    assert row["abandoned_at"] is not None
    assert not plan.run_dir.exists()
    assert plan.board_path.exists() is not board_removed


def test_sync_failure_converges_on_the_next_call(pd, env):
    """4단계(sync) 실패 뒤 재호출이 남은 정리를 끝낸다 — 부분 상태가 굳지 않는다."""
    _write_spec(env, "T-8050")
    plan = _prepare(pd, env, "T-8050")
    env.failures["sync"] = True
    with pytest.raises(RuntimeError, match="sync 실패 주입"):
        _abandon(pd, env, plan)
    assert not plan.board_path.exists() and plan.run_dir.exists()
    assert _ledger_rows(env)[-1]["abandoned_at"] is not None

    env.failures["sync"] = False
    result = _abandon(pd, env, plan)

    assert result.converged is True and result.sync_ready is True
    _assert_converged(pd, env, plan)


def test_ledger_append_failure_converges_on_the_next_call(pd, env, monkeypatch):
    """2단계(마감 행 append) 실패는 아무것도 지우기 전이다 — 재호출이 처음부터 다시 한다."""
    _write_spec(env, "T-8051")
    plan = _prepare(pd, env, "T-8051")
    real_append = pd._append_delegate_rounds_ledger

    def _boom(pm_home, row):
        if "abandoned_at" in row:
            raise pd.DelegateError("장부 append 실패 주입")
        return real_append(pm_home, row)

    monkeypatch.setattr(pd, "_append_delegate_rounds_ledger", _boom)
    with pytest.raises(pd.DelegateError, match="장부 append 실패 주입"):
        _abandon(pd, env, plan)
    assert plan.board_path.exists() and plan.run_dir.exists()
    assert "abandoned_at" not in _ledger_rows(env)[-1]

    monkeypatch.setattr(pd, "_append_delegate_rounds_ledger", real_append)
    result = _abandon(pd, env, plan)

    assert result.converged is True
    _assert_converged(pd, env, plan)


def test_run_dir_removal_failure_converges_on_the_next_call(pd, env, monkeypatch):
    """5단계(run-dir 삭제) 실패 뒤 재호출이 '이미 포기된 준비'로 막히지 않는다."""
    _write_spec(env, "T-8052")
    plan = _prepare(pd, env, "T-8052")
    real_file_lock = pd._load_file_lock()

    class _FileLock:
        def __getattr__(self, name):
            return getattr(real_file_lock, name)

        def force_rmtree(self, _path):
            raise OSError("run-dir 삭제 실패 주입")

    monkeypatch.setattr(pd, "_load_file_lock", lambda: _FileLock())
    with pytest.raises(pd.DelegateError, match="run-dir 삭제 실패"):
        _abandon(pd, env, plan)
    assert not plan.board_path.exists() and plan.run_dir.exists()
    assert _ledger_rows(env)[-1]["abandoned_at"] is not None

    monkeypatch.setattr(pd, "_load_file_lock", lambda: real_file_lock)
    result = _abandon(pd, env, plan)

    assert result.converged is True
    _assert_converged(pd, env, plan)


def test_converged_abandon_is_idempotent(pd, env):
    """수렴 뒤 재호출은 아무것도 바꾸지 않고 성공한다(멱등 · 거부가 아니다)."""
    _write_spec(env, "T-8053")
    plan = _prepare(pd, env, "T-8053")
    _abandon(pd, env, plan)
    rows_before = len(_ledger_rows(env))

    result = _abandon(pd, env, plan)

    assert result.changed is False and result.converged is True
    assert result.board_removed is True and result.run_dir_removed is True
    assert len(_ledger_rows(env)) == rows_before


# ── I5·D3 순번: 중간 순번은 board 파일을 보존한다 ──────────────────────────

def test_middle_ordinal_keeps_the_board_round_and_closes_the_other_two(pd, env):
    """고정 수열의 중간 developer 시드 — board 파일 보존 · 나머지 두 자산 종결."""
    _write_spec(env, "T-8060")
    architect = _prepare(pd, env, "T-8060", "architect")
    _land_architect(pd, env, architect)
    target = _prepare(pd, env, "T-8060", "developer")
    _prepare(pd, env, "T-8060", "code-reviewer")
    assert target.ordinal == 2
    before_codes = _problem_codes(pd, env, "T-8060")

    result = _abandon(pd, env, target)

    assert result.board_removed is False and result.converged is True
    assert target.board_path.exists()                     # 자산 2 보존
    assert not target.run_dir.exists()                    # 자산 1 종결
    row = [row for row in _ledger_rows(env) if Path(row["copy"]) == target.path][-1]
    assert row["abandoned_at"] is not None                # 자산 3 종결
    after_codes = _problem_codes(pd, env, "T-8060")
    for code in ("round-gap", "round-dup"):
        assert after_codes.count(code) == before_codes.count(code) == 0
    # 보존 분기도 board 파일을 바꾼다(표식 발행) — 그 write 를 커밋하지 않으면 PM 홈에 미커밋
    # 변경이 남아 다음 board mutation 에 섞인다.
    assert env.sync_calls[-1] == (
        "ticket-abandon T-8060 developer", [target.board_path],
    )


def test_middle_ordinal_abandon_is_idempotent_too(pd, env):
    """보존 분기의 재호출도 성공한다 — 보존된 board 파일이 '미수렴'으로 읽히지 않는다."""
    _write_spec(env, "T-8063")
    architect = _prepare(pd, env, "T-8063")
    _land_architect(pd, env, architect)
    target = _prepare(pd, env, "T-8063", "developer")
    _prepare(pd, env, "T-8063", "code-reviewer")
    first = _abandon(pd, env, target)
    board_bytes = target.board_path.read_bytes()

    second = _abandon(pd, env, target)

    assert first.converged is True and second.converged is True
    assert second.changed is False and second.board_removed is False
    assert target.board_path.read_bytes() == board_bytes


def test_middle_ordinal_abandon_is_not_listed_as_unharvested(pd, env):
    """PM 표시면(진행 중 작업)의 입력은 board 파일이 아니라 장부 행이다."""
    _write_spec(env, "T-8061")
    architect = _prepare(pd, env, "T-8061")
    _land_architect(pd, env, architect)
    first = _prepare(pd, env, "T-8061", "developer")
    _prepare(pd, env, "T-8061", "code-reviewer")

    _abandon(pd, env, first)

    unharvested = pd.ticket_copy_records(
        env.pm_home, ticket="T-8061", unharvested=True,
    )
    assert [row["ordinal"] for row in unharvested] == [3]


def test_max_ordinal_abandon_leaves_no_gap_for_the_remaining_rounds(pd, env):
    """최대 순번을 지운 뒤에도 남은 라운드의 순번은 연속이다(I5)."""
    _write_spec(env, "T-8062")
    architect = _prepare(pd, env, "T-8062")
    _land_architect(pd, env, architect)
    _prepare(pd, env, "T-8062", "developer")
    last = _prepare(pd, env, "T-8062", "code-reviewer")

    _abandon(pd, env, last)

    assert not last.board_path.exists()
    codes = _problem_codes(pd, env, "T-8062")
    assert "round-gap" not in codes and "round-dup" not in codes


# ── I3 단일 처분 (D4) ──────────────────────────────────────────────────────

def test_abandon_refuses_an_already_harvested_run(pd, env):
    _write_spec(env, "T-8070")
    plan = _prepare(pd, env, "T-8070")
    _land_architect(pd, env, plan)

    with pytest.raises(pd.DelegateError, match="이미 회수된 준비는 포기할 수 없습니다"):
        _abandon(pd, env, plan)


def test_harvest_refuses_an_abandoned_reservation(pd, env):
    """포기된 행의 회수를 열어 두면 재사용된 순번의 새 라운드를 옛 run 이 덮는다."""
    _write_spec(env, "T-8071")
    plan = _prepare(pd, env, "T-8071")
    _abandon(pd, env, plan)

    with pytest.raises(pd.DelegateError, match="이미 포기된 준비는 회수할 수 없습니다"):
        pd.harvest_ticket_copy(
            copy_path=plan.path, cwd=env.slot, pm_home=env.pm_home,
        )


def test_reused_ordinal_survives_the_old_runs_harvest(pd, env):
    """포기 → 같은 순번 재채번 → 옛 copy 회수 시도 = 거부 ∧ 새 라운드 bytes 불변."""
    _write_spec(env, "T-8072")
    dead = _prepare(pd, env, "T-8072")
    dead_copy = dead.path
    dead_text = dead_copy.read_text(encoding="utf-8")
    _abandon(pd, env, dead)
    fresh = _prepare(pd, env, "T-8072")
    assert fresh.ordinal == dead.ordinal
    fresh_bytes = fresh.board_path.read_bytes()
    dead_copy.parent.mkdir(parents=True, exist_ok=True)
    dead_copy.write_text(dead_text + "\n옛 run 산출\n", encoding="utf-8", newline="")

    with pytest.raises(pd.DelegateError, match="이미 포기된 준비는 회수할 수 없습니다"):
        pd.harvest_ticket_copy(
            copy_path=dead_copy, cwd=env.slot, pm_home=env.pm_home,
        )

    assert fresh.board_path.read_bytes() == fresh_bytes


# ── 장부 하위 호환 (구 8키 ⊆ row ⊆ 8키 ∪ {abandoned_at, owner_pid}) ────────

def _base_row(**overrides) -> dict:
    row = {
        "ticket": "T-8100", "role": "developer", "ordinal": 1,
        "run_id": "a" * 32, "copy": str((Path.cwd() / "copy.md").resolve()),
        "board_rel": "wiki/tickets/rounds/T-8100/01-developer.md",
        "prepared_at": "2026-08-22T00:00:00+00:00", "harvested_at": None,
    }
    row.update(overrides)
    return row


def test_ledger_row_accepts_legacy_eight_key_rows(pd):
    row = pd._delegate_rounds_ledger_row(_base_row(), line_number=1)
    assert "abandoned_at" not in row and "owner_pid" not in row


def test_ledger_row_accepts_the_two_optional_keys(pd):
    row = pd._delegate_rounds_ledger_row(
        _base_row(abandoned_at="2026-08-22T01:00:00+00:00", owner_pid=4242),
        line_number=1,
    )
    assert row["abandoned_at"] == "2026-08-22T01:00:00+00:00"
    assert row["owner_pid"] == 4242


@pytest.mark.parametrize("bad", [None, ""])
def test_ledger_row_rejects_null_or_empty_abandoned_at(pd, bad):
    with pytest.raises(pd.DelegateError, match="값 형식 불일치"):
        pd._delegate_rounds_ledger_row(_base_row(abandoned_at=bad), line_number=1)


@pytest.mark.parametrize("bad", [0, -1, True, "4242", None])
def test_ledger_row_rejects_owner_pid_values_that_query_as_absent(pd, bad):
    """생존 조회 seam 이 부재로 정규화하는 값은 장부 경계에서 막는다(조용한 퇴화 금지)."""
    with pytest.raises(pd.DelegateError, match="값 형식 불일치"):
        pd._delegate_rounds_ledger_row(_base_row(owner_pid=bad), line_number=1)


def test_ledger_row_still_rejects_unknown_keys(pd):
    with pytest.raises(pd.DelegateError, match="schema 불일치"):
        pd._delegate_rounds_ledger_row(_base_row(mystery="x"), line_number=1)


def test_ledger_row_still_rejects_missing_required_keys(pd):
    row = _base_row()
    del row["harvested_at"]
    with pytest.raises(pd.DelegateError, match="schema 불일치"):
        pd._delegate_rounds_ledger_row(row, line_number=1)


def test_mixed_legacy_and_new_rows_stay_readable_and_harvestable(pd, env, capsys):
    """구 8키 행·`abandoned_at` 행·`owner_pid` 행이 섞여도 손상 경고 0 · 조회/회수 정상."""
    for ticket in ("T-8101", "T-8102", "T-8103"):
        _write_spec(env, ticket)
    legacy = _prepare(pd, env, "T-8101")
    _land_architect(pd, env, legacy)
    abandoned = _prepare(pd, env, "T-8102")
    _abandon(pd, env, abandoned)
    owned = _prepare(pd, env, "T-8103", owner_pid=4242)
    _fill_architect(pd, owned)

    capsys.readouterr()
    rows = pd.ticket_copy_records(env.pm_home)
    warning = capsys.readouterr().err
    harvested = pd.harvest_ticket_copy(
        copy_path=owned.path, cwd=env.slot, pm_home=env.pm_home,
    )

    assert "손상" not in warning
    assert {row["ticket"] for row in rows} == {"T-8101", "T-8102", "T-8103"}
    assert harvested.changed is True
    assert owned.board_path.read_text(encoding="utf-8").endswith("architect 계약\n")


# ── CLI 표면 ────────────────────────────────────────────────────────────────

def test_abandon_cli_surface_matches_harvest_argument_shape(pd):
    parser = pd.build_subcommand_parser("ticket")
    args = parser.parse_args(["abandon", "--copy", "/abs/copy.md", "--cwd", "/abs/cwd"])

    assert args.ticket_command == "abandon"
    assert args.copy == "/abs/copy.md" and args.cwd == "/abs/cwd"
    assert args.assume_dead is False
    assert parser.parse_args([
        "abandon", "--copy", "/abs/copy.md", "--cwd", "/abs/cwd",
        pd.ABANDON_ASSUME_DEAD_FLAG,
    ]).assume_dead is True


def test_cli_reports_the_convergence_assertion_as_machine_fields(pd, env, monkeypatch, capsys):
    """기계 보고는 상태 선언이 아니라 세 자산 재판독 결과다."""
    _write_spec(env, "T-8110")
    plan = _prepare(pd, env, "T-8110")
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: env.pm_home)
    capsys.readouterr()

    rc = pd._cmd_ticket([
        "abandon", "--copy", str(plan.path), "--cwd", str(env.slot),
        pd.ABANDON_ASSUME_DEAD_FLAG,
    ])

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert payload["converged"] is True
    assert payload["board_removed"] is True and payload["run_dir_removed"] is True
    assert payload["changed"] is True and payload["copy"] == str(plan.path)


def test_cli_copies_query_labels_abandoned_rows(pd, env, monkeypatch, capsys):
    """역방향 — 조회면은 포기된 행을 그대로 보여주고 미회수 목록에서는 뺀다."""
    _write_spec(env, "T-8111")
    plan = _prepare(pd, env, "T-8111")
    _abandon(pd, env, plan)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: env.pm_home)
    capsys.readouterr()

    assert pd._cmd_ticket(["copies", "--ticket", "T-8111"]) == 0
    listed = capsys.readouterr().out
    assert pd._cmd_ticket(["copies", "--ticket", "T-8111", "--unharvested"]) == 0
    unharvested = capsys.readouterr().out

    assert "포기(" in listed and str(plan.path) in listed
    assert "미회수 라운드 준비 없음" in unharvested


# ── I6 락 경계: stub 없이 실 board 호출 그래프를 태우는 교착 회귀 ───────────

def _cli_home(tmp_path: Path, ticket: str) -> Path:
    """PM 홈이자 슬롯인 단일 git 트리 — 실 CLI 가 소유 PM 홈을 스스로 해소한다."""
    home = tmp_path / "cli-home"
    tools = home / ".project_manager" / "tools"
    tools.parent.mkdir(parents=True)
    shutil.copytree(TOOLS, tools, ignore=shutil.ignore_patterns("__pycache__"))
    tickets = home / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (tickets / f"{ticket}-abandon.md").write_text(
        _spec_text(ticket), encoding="utf-8", newline="\n",
    )
    write_cluster_ledger(
        home / ".project_manager" / "wiki", ticket,
        base_branch="task/main", rounds=_FIXED_ROUNDS,
    )
    (home / ".project_manager" / ".gitignore").write_text(
        ".local/\n", encoding="utf-8", newline="\n",
    )
    (home / "tracked.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(home, "init", "-q").returncode == 0
    assert _git(home, "add", "-A").returncode == 0
    assert subprocess.run(
        ["git", "-C", str(home), "commit", "-qm", "seed"], env=_git_identity_env(),
        capture_output=True, text=True, check=False,
    ).returncode == 0
    return home


def _cli(home: Path, *argv: str, timeout: float = CLI_WALL_TIMEOUT_SEC):
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_delegate.py"),
         "ticket", *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_git_identity_env(), timeout=timeout, check=False,
    )


def _machine_payload(completed: subprocess.CompletedProcess) -> dict:
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    assert lines, completed.stdout + completed.stderr
    return json.loads(lines[-1])


def _cli_prepare(home: Path, ticket: str) -> dict:
    prepared = _cli(
        home, "prepare", "--ticket", ticket, "--role", "architect", "--cwd", str(home),
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    return _machine_payload(prepared)


def test_cli_abandon_runs_the_real_board_graph_without_deadlocking(tmp_path):
    """stub 0 — 실 board 모듈의 sync(refresh_board→board_lock)까지 태운다.

    `board_lock` 을 쥔 채 sync 를 부르면 같은 파일 락 재진입으로 교착해 rc 가 영원히 오지
    않는다. 이 케이스는 그때 wall timeout 예외로 red 가 된다.
    """
    home = _cli_home(tmp_path, "T-8200")
    prepared = _cli_prepare(home, "T-8200")

    abandoned = _cli(
        home, "abandon", "--copy", prepared["copy"], "--cwd", str(home), "--assume-dead",
    )

    assert abandoned.returncode == 0, abandoned.stdout + abandoned.stderr
    payload = _machine_payload(abandoned)
    assert payload["converged"] is True and payload["board_removed"] is True
    assert payload["run_dir_removed"] is True
    assert not Path(prepared["copy"]).exists()
    assert not Path(prepared["run_dir"]).exists()


def test_cli_abandon_actually_waits_on_the_board_lock(tmp_path):
    """락을 실제로 잡는지 값으로 본다 — 다른 프로세스가 쥔 동안 차단됐다가 해제 후 완료된다."""
    home = _cli_home(tmp_path, "T-8201")
    prepared = _cli_prepare(home, "T-8201")
    lock_path = home / ".project_manager" / ".local" / "board.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import importlib.util, pathlib, sys, time\n"
         "spec = importlib.util.spec_from_file_location('file_lock', sys.argv[1])\n"
         "module = importlib.util.module_from_spec(spec)\n"
         "spec.loader.exec_module(module)\n"
         "with module.exclusive_file_lock(pathlib.Path(sys.argv[2])):\n"
         "    print('locked', flush=True)\n"
         "    time.sleep(float(sys.argv[3]))\n",
         str(home / ".project_manager" / "tools" / "file_lock.py"), str(lock_path),
         str(BOARD_LOCK_HOLD_SEC)],
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        abandoned = _cli(
            home, "abandon", "--copy", prepared["copy"], "--cwd", str(home),
            "--assume-dead",
        )
        elapsed = time.monotonic() - started
    finally:
        holder.wait(timeout=CLI_WALL_TIMEOUT_SEC)

    assert abandoned.returncode == 0, abandoned.stdout + abandoned.stderr
    # 경합 없는 같은 호출은 1초 밑에서 끝난다(실측 · 위 케이스) — 이 하한은 대기했을 때만 넘는다.
    assert elapsed >= BOARD_LOCK_HOLD_SEC * 0.8
    assert _machine_payload(abandoned)["converged"] is True


def test_cli_abandon_without_explicit_confirmation_is_refused(tmp_path):
    """실 CLI 에서도 파괴 연산의 기본값은 거부다 — 세 자산 불변 · rc≠0."""
    home = _cli_home(tmp_path, "T-8202")
    prepared = _cli_prepare(home, "T-8202")

    refused = _cli(home, "abandon", "--copy", prepared["copy"], "--cwd", str(home))

    assert refused.returncode == 1
    assert "--assume-dead" in refused.stderr
    assert Path(prepared["copy"]).exists() and Path(prepared["run_dir"]).is_dir()
