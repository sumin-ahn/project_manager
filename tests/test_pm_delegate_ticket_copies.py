"""T-0850 — 재실행으로 대체된 라운드가 harvest·abandon 양쪽에 거부돼 미회수 장부 레코드에
종결 경로가 없는 실형상(라운드 5/6 F-001 재선언 순환) 픽스처.

`ticket abandon` 의 "시드 그대로가 아님" 거부에 명시-확인 축(`--superseded-by ordinal`) 하나만
연다 — 그 외 거부 조건(단일 처분·신뢰 뿌리·종료 증거·중간 순번 board 보존)은 손대지 않는다.
`ticket harvest` 의 finding ID 재선언 거부는 판정 무결성 축이라 이 티켓의 대상이 아니다(회귀로
고정).

  R1 명시 축 필요    — `--superseded-by` 없이는 종전대로 거부(회귀).
  R2 자기 참조 거부  — 대체본이 자기 자신일 수 없다.
  R3 생존 확인 유지  — 대체-확인이 종료 증거·소유 pid 생존 확인을 대신하지 않는다(역방향 확인).
  R4 실형상 종결     — 라운드 N/N+1 이 같은 finding ID 로 harvest·abandon 순환에 빠진 뒤,
                       `--superseded-by`(+`--assume-dead`) 로 라운드 N 이 종결되고
                       `ticket copies --unharvested` 목록에서 값으로 사라진다.
  R5 재호출 멱등     — 재호출은 `--superseded-by` 를 다시 주지 않아도 남은 정리를 끝낸다.
  R6 harvest 비목표  — `ticket harvest` 는 이 축을 모른다.
  R7 산출 보존 불변식 — 검증·loud·장부 기록은 대체-확인이 실제로 발화한 호출에서만 나고,
                       파괴 효력(run-dir 삭제)은 장부에 기록된 값만 인정한다(재호출 인자 무시).
  R8 값·실재·순서    — `<1`·자기참조·부재 ordinal 은 시드 대조 분기와 무관하게 거부되고,
                       loud 는 생존 게이트를 통과한 뒤에만 남는다.

중간 순번이라 보존한 board 시드에는 엔진 표식이 발행돼 `round-pending`·판정 표면·직전 산출에서
함께 빠지고, 파괴 판정 기준선은 그 발행 **이전** bytes 다(최대 순번 삭제 분기는 불변).

검증 근거는 실 board 트리·실 라운드 파일·실 `delegate-rounds.jsonl` 이고, 리뷰 블록은 엔진 골격
렌더(`render_pm_review_block_skeleton`)에서 key·enum 을 받아 값만 채운다(스키마 재타이핑 0).
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_DELEGATE = TOOLS / "pm_delegate.py"


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_ticket_copies", PM_DELEGATE)
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


def _spec_text(ticket: str, *, status: str = "claimed") -> str:
    return (
        "---\n"
        f"id: {ticket}\n"
        "title: 대체-확인 왕복\n"
        f"status: {status}\n"
        "created: '2026-08-23'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        "claimed_at: '2026-08-23T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "design: done\n"
        "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\n대체-확인 축 왕복 픽스처.\n\n"
        "## 설계\n"
        "- **경계 실측**: 기계 테스트 픽스처\n"
        "- **불변식**: 이 파일의 축 밖\n"
        "- **표면 상한**: 픽스처 1건\n"
        "- **테스트 전략**: 정상·실패 경로\n"
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
def env(tmp_path, pd, monkeypatch):
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
    monkeypatch.setenv("GIT_AUTHOR_NAME", "supersede")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "supersede@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "supersede")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "supersede@test.invalid")
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    sync_log: list = []
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, pm_home, sync_log),
    )
    return pm_home, slot, tickets, sync_log


def _write_spec(tickets: Path, ticket: str, **kwargs) -> Path:
    path = tickets / f"{ticket}-supersede.md"
    path.write_text(_spec_text(ticket, **kwargs), encoding="utf-8", newline="\n")
    return path


def _ledger_rows(pm_home: Path) -> list[dict]:
    path = pm_home / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _finding_values(pd, finding_id: str) -> dict:
    return {
        "id": finding_id,
        "class": pd.PM_REVIEW_CLASSES[0],
        "severity": pd.PM_REVIEW_SEVERITIES[0],
        "authority": "[[ADR-0090]] §경계",
        "evidence": f"{finding_id} probe rc=1",
        "recommendation": f"{finding_id}만 수정",
        "design_change": False,
    }


def _review_body(pd, role: str, finding_ids: list[str]) -> str:
    """리뷰 라운드 본문 — 블록 key·fence 는 엔진 골격 렌더가 소유하고 값만 채운다."""
    skeleton = pd.render_pm_review_block_skeleton(role, [])
    payload = pd._pm_review_json_blocks(skeleton)[0].value
    finding_shape = payload["findings"][0]
    payload["findings"] = [
        dict(finding_shape, **_finding_values(pd, finding_id))
        for finding_id in finding_ids
    ]
    payload["confirmations"] = []
    listed = "\n".join(f"- {finding_id}" for finding_id in finding_ids) or "- 없음"
    return (
        f"## must-fix\n{listed}\n\n"
        f"## 판정\n판정: 반려 · finding {len(finding_ids)}건"
        f"(must-fix {len(finding_ids)}건)\n\n"
        + pd._pm_review_block_text(payload)
    )


def _write_round_output(path: Path, body: str) -> str:
    """슬롯 라운드 파일에 산출을 쓴다 — 첫 줄 헤더는 그대로 둔다(라운드 규약)."""
    produced = path.read_text(encoding="utf-8").partition("\n")[0] + "\n\n" + body
    path.write_text(produced, encoding="utf-8", newline="")
    return produced


def _tickets_root(pm_home: Path) -> Path:
    return pm_home / ".project_manager" / "wiki" / "tickets"


def _problem_codes(pd, pm_home: Path, ticket: str) -> list[str]:
    rounds_module = pd._load_ticket_rounds()
    return [
        item.code
        for item in rounds_module.verify_rounds(_tickets_root(pm_home), ticket)
    ]


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


# ── R1 명시 축 필요(회귀 — 기본 거부 유지) ──────────────────────────────────

def test_abandon_without_superseded_by_still_refuses_mismatched_output(pd, env):
    """`--superseded-by` 없는 abandon 은 산출 있는 라운드를 종전대로 거부한다."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9001")
    plan = pd.prepare_ticket_copy(
        ticket="T-9001", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(plan.path, _review_body(pd, "code-reviewer", ["F-901"]))
    before = plan.board_path.read_bytes()

    with pytest.raises(pd.DelegateError, match="산출이 있어 포기할 수 없습니다"):
        pd.abandon_ticket_copy(
            copy_path=plan.path, cwd=slot, pm_home=pm_home, assume_dead=True,
        )

    assert plan.board_path.read_bytes() == before
    assert plan.run_dir.exists()
    assert "abandoned_at" not in _ledger_rows(pm_home)[-1]
    assert sync_log == []


# ── R2 자기 참조 거부 ────────────────────────────────────────────────────────

def test_superseded_by_self_reference_is_rejected(pd, env):
    """대체본이 자기 자신일 수 없다 — 값 없는 우회를 막는다."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9002")
    plan = pd.prepare_ticket_copy(
        ticket="T-9002", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(plan.path, _review_body(pd, "code-reviewer", ["F-902"]))
    before = plan.board_path.read_bytes()

    with pytest.raises(pd.DelegateError, match="대체 라운드 ordinal 이 올바르지 않습니다"):
        pd.abandon_ticket_copy(
            copy_path=plan.path, cwd=slot, pm_home=pm_home, assume_dead=True,
            superseded_by_ordinal=plan.ordinal,
        )

    assert plan.board_path.read_bytes() == before
    assert plan.run_dir.exists()
    assert "abandoned_at" not in _ledger_rows(pm_home)[-1]
    assert sync_log == []


# ── R3 생존 확인 유지(역방향 확인 — 대체-확인이 liveness gate 를 대신하지 않는다) ─────────

def test_superseded_by_without_evidence_still_requires_assume_dead(pd, env):
    """대체-확인만으로는 부족하다 — 종료 증거 없는 예약은 여전히 명시 확인이 필요하다."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9003")
    dead = pd.prepare_ticket_copy(
        ticket="T-9003", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    live = pd.prepare_ticket_copy(
        ticket="T-9003", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(dead.path, _review_body(pd, "code-reviewer", ["F-903"]))
    before = dead.board_path.read_bytes()

    with pytest.raises(pd.DelegateError, match="종료 증거가 없습니다") as caught:
        pd.abandon_ticket_copy(
            copy_path=dead.path, cwd=slot, pm_home=pm_home,
            superseded_by_ordinal=live.ordinal,
        )

    assert pd.ABANDON_ASSUME_DEAD_FLAG in str(caught.value)
    assert dead.board_path.read_bytes() == before
    assert dead.run_dir.exists()
    assert "abandoned_at" not in _ledger_rows(pm_home)[-1]
    assert sync_log == []


def test_superseded_by_does_not_override_a_live_owner_pid(pd, env, monkeypatch):
    """대체-확인을 줘도 소유 pid 가 살아 있으면 명시 확인(`--assume-dead`) 없이는 거부다."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9004")
    dead = pd.prepare_ticket_copy(
        ticket="T-9004", role="code-reviewer", cwd=slot, pm_home=pm_home, owner_pid=4242,
    )
    live = pd.prepare_ticket_copy(
        ticket="T-9004", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(dead.path, _review_body(pd, "code-reviewer", ["F-904"]))
    before = dead.board_path.read_bytes()
    _use_relay_pid(pd, monkeypatch, alive=True)

    with pytest.raises(pd.DelegateError, match="실행 중이라 포기하지 않습니다"):
        pd.abandon_ticket_copy(
            copy_path=dead.path, cwd=slot, pm_home=pm_home,
            superseded_by_ordinal=live.ordinal,
        )

    assert dead.board_path.read_bytes() == before
    assert dead.run_dir.exists()
    assert "abandoned_at" not in _ledger_rows(pm_home)[-1]
    assert sync_log == []

    # 죽음이 확인되면(`pid_is_alive`→False) `--assume-dead` 로 그제야 진행된다.
    _use_relay_pid(pd, monkeypatch, alive=False)
    result = pd.abandon_ticket_copy(
        copy_path=dead.path, cwd=slot, pm_home=pm_home, assume_dead=True,
        superseded_by_ordinal=live.ordinal,
    )
    assert result.converged is True


# ── R4 실형상 종결 + R5 재호출 멱등 ──────────────────────────────────────────

def test_superseded_round_closes_the_real_deadlock_shape(pd, env, capsys):
    """실측 재현: 라운드 1 이 F-001 을 내고, 라운드 2 가 같은 ID 로 회수된 뒤,

    라운드 1 은 harvest(ID 충돌)·abandon(산출 존재) 양쪽에 막힌다 — `--superseded-by` 로
    종결하면 `ticket copies --unharvested` 목록에서 라운드 1 이 값으로 사라진다.
    """
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9005")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9005", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round1.path, _review_body(pd, "code-reviewer", ["F-001"]))
    round1_board_seed = round1.board_path.read_bytes()

    round2 = pd.prepare_ticket_copy(
        ticket="T-9005", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    assert round2.ordinal == round1.ordinal + 1
    _write_round_output(round2.path, _review_body(pd, "code-reviewer", ["F-001"]))
    harvested = pd.harvest_ticket_copy(copy_path=round2.path, cwd=slot, pm_home=pm_home)
    assert harvested.changed is True

    # 라운드 1 은 이제 harvest·abandon 양쪽에 막힌다(실측 순환).
    with pytest.raises(pd.DelegateError, match="finding ID 재선언: F-001"):
        pd.harvest_ticket_copy(copy_path=round1.path, cwd=slot, pm_home=pm_home)
    with pytest.raises(pd.DelegateError, match="산출이 있어 포기할 수 없습니다"):
        pd.abandon_ticket_copy(
            copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
        )
    stuck = pd.ticket_copy_records(pm_home, ticket="T-9005", unharvested=True)
    assert str(round1.path) in [row["copy"] for row in stuck]

    capsys.readouterr()
    result = pd.abandon_ticket_copy(
        copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
        superseded_by_ordinal=round2.ordinal,
    )

    assert (result.changed, result.converged) == (True, True)
    assert result.board_removed is False           # 중간 순번 보존 규칙은 그대로다.
    assert result.run_dir_removed is True
    assert round1.board_path.exists()
    # 보존한 시드에는 엔진 표식 한 줄만 붙는다(그 앞 bytes 는 시드 그대로다).
    marker = pd.pm_review_refused_line("code-reviewer")
    assert round1.board_path.read_bytes() == round1_board_seed + f"{marker}\n".encode("utf-8")
    assert sync_log[-1] == (
        "ticket-abandon T-9005 code-reviewer", [round1.board_path],
    )
    assert not round1.run_dir.exists()
    loud = capsys.readouterr().err
    assert "대체-확인" in loud and f"ordinal={round2.ordinal}" in loud

    # 표식이 붙은 라운드는 `pending` 을 배제하는 자리에서 함께 빠진다.
    rounds_module = pd._load_ticket_rounds()
    loaded = rounds_module.load_rounds(_tickets_root(pm_home), "T-9005")
    assert [(item.ordinal, item.pending) for item in loaded] == [
        (round1.ordinal, False), (round2.ordinal, False)]
    assert pd._pm_review_refused_rounds(loaded) == {("code-reviewer", round1.ordinal)}
    assert [item.ordinal for item in pd._pm_review_surface_rounds(loaded)] == [round2.ordinal]
    assert rounds_module.latest_round_of_role(
        loaded, "code-reviewer",
    ).ordinal == round2.ordinal
    spec_text = (tickets / "T-9005-supersede.md").read_text(encoding="utf-8")
    assert "F-001" in pd.render_pm_review_disposition_template(spec_text, loaded)

    # 값으로 사라짐 — 미회수 목록에 더는 라운드 1 사본이 없다.
    unharvested = pd.ticket_copy_records(pm_home, ticket="T-9005", unharvested=True)
    assert str(round1.path) not in [row["copy"] for row in unharvested]
    assert unharvested == []          # 라운드 2 는 harvested, 라운드 1 은 abandoned.

    assert _problem_codes(pd, pm_home, "T-9005") == []      # `round-pending` 도 사라진다.


def test_superseded_abandon_retry_omits_the_flag_and_still_converges(pd, env, monkeypatch):
    """재호출은 `--superseded-by` 를 다시 안 줘도 남은 정리를 끝낸다(장부에서 되읽는다)."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9006")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9006", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round1.path, _review_body(pd, "code-reviewer", ["F-011"]))
    round2 = pd.prepare_ticket_copy(
        ticket="T-9006", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round2.path, _review_body(pd, "code-reviewer", ["F-011"]))
    pd.harvest_ticket_copy(copy_path=round2.path, cwd=slot, pm_home=pm_home)

    # run-dir 삭제만 실패하도록 주입해 "ledger 는 닫혔지만 run-dir 은 남은" 중간 상태를 만든다.
    real_rmtree = pd._load_file_lock().force_rmtree
    state = {"calls": 0}

    def _flaky_rmtree(path):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("주입 실패")
        return real_rmtree(path)

    monkeypatch.setattr(pd._load_file_lock(), "force_rmtree", _flaky_rmtree)

    with pytest.raises(pd.DelegateError, match="run-dir 삭제 실패"):
        pd.abandon_ticket_copy(
            copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
            superseded_by_ordinal=round2.ordinal,
        )
    assert "abandoned_at" in _ledger_rows(pm_home)[-1]      # 1단계는 이미 내구성 있게 닫혔다.
    assert round1.run_dir.exists()                          # 삭제는 실패해 남아 있다.

    # 재호출 — `superseded_by_ordinal` 인자 없이도 장부에서 되읽어 같은 기준선으로 마무리한다.
    result = pd.abandon_ticket_copy(
        copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
    )

    assert result.converged is True
    assert not round1.run_dir.exists()
    unharvested = pd.ticket_copy_records(pm_home, ticket="T-9006", unharvested=True)
    assert unharvested == []


# ── 보존 시드 표식 ─────────────────────────────────────────────────────────

def test_middle_ordinal_abandon_marks_the_preserved_seed_out_of_the_surfaces(
    pd, env,
):
    """대체-확인 **없는** 중간 순번 포기도 보존한 시드에 표식을 발행한다.

    그 board 파일은 영원히 시드 그대로라 표식이 없으면 `round-pending` 으로 남는다. 표식은
    파괴 판정 기준선을 읽은 뒤 붙으므로 run-dir 정리는 종전대로 끝난다.
    """
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9020")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9020", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    round1_seed = round1.board_path.read_bytes()
    round2 = pd.prepare_ticket_copy(
        ticket="T-9020", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round2.path, _review_body(pd, "code-reviewer", ["F-921"]))
    pd.harvest_ticket_copy(copy_path=round2.path, cwd=slot, pm_home=pm_home)
    assert _problem_codes(pd, pm_home, "T-9020") == ["round-pending"]

    result = pd.abandon_ticket_copy(
        copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
    )

    assert (result.changed, result.converged) == (True, True)
    assert result.board_removed is False and result.run_dir_removed is True
    marker = pd.pm_review_refused_line("code-reviewer")
    assert round1.board_path.read_bytes() == round1_seed + f"{marker}\n".encode("utf-8")
    assert sync_log[-1] == (
        "ticket-abandon T-9020 code-reviewer", [round1.board_path],
    )
    assert _problem_codes(pd, pm_home, "T-9020") == []
    rounds_module = pd._load_ticket_rounds()
    loaded = rounds_module.load_rounds(_tickets_root(pm_home), "T-9020")
    assert pd._pm_review_refused_rounds(loaded) == {("code-reviewer", round1.ordinal)}
    assert rounds_module.latest_round_of_role(
        loaded, "code-reviewer",
    ).ordinal == round2.ordinal


def test_max_ordinal_abandon_still_deletes_the_board_round_without_a_marker(pd, env):
    """역방향 — 최대 순번은 종전대로 board 파일을 지운다(표식 발행 0)."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9021")
    only = pd.prepare_ticket_copy(
        ticket="T-9021", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )

    result = pd.abandon_ticket_copy(
        copy_path=only.path, cwd=slot, pm_home=pm_home, assume_dead=True,
    )

    assert (result.board_removed, result.run_dir_removed) == (True, True)
    assert not only.board_path.exists()
    assert _problem_codes(pd, pm_home, "T-9021") == []
    rounds_module = pd._load_ticket_rounds()
    assert rounds_module.load_rounds(_tickets_root(pm_home), "T-9021") == []


def test_marker_is_not_a_second_write_and_does_not_block_the_retry(
    pd, env, monkeypatch,
):
    """재호출은 표식을 다시 쓰지 않고, 자기가 쓴 줄 때문에 산출 보존으로 뒤집히지도 않는다.

    파괴 판정 기준선은 표식 **이전** bytes 다 — 되돌리지 않으면 첫 호출이 붙인 줄이 다음
    호출에 "산출이 생겼다" 로 읽혀 남은 정리가 영영 끝나지 않는다.
    """
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9022")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9022", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    round1_seed = round1.board_path.read_bytes()
    round2 = pd.prepare_ticket_copy(
        ticket="T-9022", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round2.path, _review_body(pd, "code-reviewer", ["F-922"]))
    pd.harvest_ticket_copy(copy_path=round2.path, cwd=slot, pm_home=pm_home)

    real_rmtree = pd._load_file_lock().force_rmtree
    state = {"calls": 0}

    def _flaky_rmtree(path):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("주입 실패")
        return real_rmtree(path)

    monkeypatch.setattr(pd._load_file_lock(), "force_rmtree", _flaky_rmtree)

    with pytest.raises(pd.DelegateError, match="run-dir 삭제 실패"):
        pd.abandon_ticket_copy(
            copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
        )
    marker = pd.pm_review_refused_line("code-reviewer")
    assert round1.board_path.read_bytes() == round1_seed + f"{marker}\n".encode("utf-8")

    result = pd.abandon_ticket_copy(
        copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
    )

    assert result.converged is True
    assert not round1.run_dir.exists()
    # 표식은 한 줄뿐이다(재호출이 자기 줄 위에 다시 쓰지 않는다).
    assert round1.board_path.read_bytes() == round1_seed + f"{marker}\n".encode("utf-8")


# ── R6 harvest 비목표 ────────────────────────────────────────────────────────

def test_harvest_is_untouched_by_the_superseded_axis(pd):
    """`ticket harvest` 는 이 축을 모른다 — finding ID 재선언 거부는 그대로다."""
    source = inspect.getsource(pd.harvest_ticket_copy)
    assert "superseded" not in source


# ── R7 산출 보존 불변식(검증 지점 ≠ 효력 지점 회귀) ──────────────────────────

def test_self_reference_is_rejected_even_when_seed_is_intact(pd, env):
    """자기참조 ordinal 은 시드가 그대로여도(mismatch 분기가 아예 안 돌아도) 거부된다.

    산출을 안 쓴 라운드는 slot bytes 가 board 시드와 같아 mismatch 분기가 돌지 않는다 — 값
    검증이 그 분기 **안**에만 있으면 이 호출은 검증을 하나도 거치지 않고 통과해, 무효한
    자기참조 값이 장부에 그대로 박힌다.
    """
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9008")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9008", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    # 산출을 쓰지 않는다 — 슬롯 라운드 파일이 board 시드 그대로다.
    before = round1.board_path.read_bytes()

    with pytest.raises(pd.DelegateError, match="대체 라운드 ordinal 이 올바르지 않습니다"):
        pd.abandon_ticket_copy(
            copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
            superseded_by_ordinal=round1.ordinal,
        )

    last_row = _ledger_rows(pm_home)[-1]
    assert "superseded_by_ordinal" not in last_row
    assert "abandoned_at" not in last_row
    assert round1.board_path.read_bytes() == before
    assert round1.run_dir.exists()
    assert sync_log == []


def test_a_retry_only_flag_cannot_delete_output_the_closing_call_never_confirmed(
    pd, env, monkeypatch, capsys,
):
    """장부에 기록되지 않은 대체-확인은 재호출 인자만으로 파괴 효력을 못 가진다.

    라운드를 닫은 호출(시드 그대로 → mismatch 분기 미발화)은 `superseded_by_ordinal` 을 장부에
    남기지 않는다. 그 뒤 살아 있는 프로세스가 산출을 슬롯에 마저 쓰고, 재호출이(이미 닫힌 행이라
    2단계를 건너뛰면서) `--superseded-by` 를 새로 주더라도 — 그 인자는 검증을 거친 적이 없으므로
    5단계 파괴 판정에 쓰이지 않고, run-dir 은 산출 보존으로 거부된다(장부 0 · stderr 0).
    """
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9009")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9009", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    round2 = pd.prepare_ticket_copy(
        ticket="T-9009", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round2.path, _review_body(pd, "code-reviewer", ["F-931"]))
    pd.harvest_ticket_copy(copy_path=round2.path, cwd=slot, pm_home=pm_home)

    # run-dir 삭제만 실패하도록 주입 — round1 은 이 시점에 산출이 없어(시드 그대로) 마감 행이
    # `superseded_by_ordinal` 없이 정상 닫힌다.
    real_rmtree = pd._load_file_lock().force_rmtree
    state = {"calls": 0}

    def _flaky_rmtree(path):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("주입 실패")
        return real_rmtree(path)

    monkeypatch.setattr(pd._load_file_lock(), "force_rmtree", _flaky_rmtree)

    with pytest.raises(pd.DelegateError, match="run-dir 삭제 실패"):
        pd.abandon_ticket_copy(
            copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
        )
    closed_row = _ledger_rows(pm_home)[-1]
    assert "abandoned_at" in closed_row
    assert "superseded_by_ordinal" not in closed_row     # 마감 행에 대체-확인이 없다.

    # 마감 뒤 산출이 슬롯에 착지한다(살아 있는 agent 의 마지막 쓰기 형상).
    _write_round_output(round1.path, _review_body(pd, "code-reviewer", ["F-932"]))

    capsys.readouterr()
    with pytest.raises(
        pd.DelegateError, match="슬롯 라운드 파일이 시드와 달라져 run-dir 을 지우지 않았습니다",
    ):
        pd.abandon_ticket_copy(
            copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
            superseded_by_ordinal=round2.ordinal,
        )

    loud = capsys.readouterr().err
    assert "대체-확인" not in loud                          # stderr 0건.
    assert "superseded_by_ordinal" not in _ledger_rows(pm_home)[-1]   # 장부 0건.
    assert round1.run_dir.exists()                          # 산출 보존 — 지워지지 않는다.


# ── R8 값·실재·순서(should-fix) ──────────────────────────────────────────────

def test_superseded_by_value_below_one_is_rejected_before_ledger_write(pd, env):
    """`0`(1 미만)도 시드 그대로인 호출에서 자기참조와 같은 이유로 거부된다."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9010")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9010", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )

    with pytest.raises(pd.DelegateError, match="대체 라운드 ordinal 이 올바르지 않습니다"):
        pd.abandon_ticket_copy(
            copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
            superseded_by_ordinal=0,
        )

    assert "abandoned_at" not in _ledger_rows(pm_home)[-1]
    assert round1.run_dir.exists()


def test_superseded_by_nonexistent_ordinal_is_rejected(pd, env):
    """존재하지 않는 ordinal 을 대체본으로 대면 거부된다 — 허위 loud 를 남기지 않는다."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9011")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9011", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round1.path, _review_body(pd, "code-reviewer", ["F-941"]))

    with pytest.raises(pd.DelegateError, match="대체 라운드가 존재하지 않습니다"):
        pd.abandon_ticket_copy(
            copy_path=round1.path, cwd=slot, pm_home=pm_home, assume_dead=True,
            superseded_by_ordinal=99,
        )

    assert "abandoned_at" not in _ledger_rows(pm_home)[-1]
    assert round1.run_dir.exists()
    assert round1.board_path.exists()


def test_loud_does_not_print_when_the_liveness_gate_still_rejects(pd, env, monkeypatch, capsys):
    """loud 는 생존 게이트 뒤에만 — 거부된 호출에 "포기합니다" 를 남기지 않는다."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9012")
    dead = pd.prepare_ticket_copy(
        ticket="T-9012", role="code-reviewer", cwd=slot, pm_home=pm_home, owner_pid=4343,
    )
    live = pd.prepare_ticket_copy(
        ticket="T-9012", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(dead.path, _review_body(pd, "code-reviewer", ["F-951"]))
    _use_relay_pid(pd, monkeypatch, alive=True)

    capsys.readouterr()
    with pytest.raises(pd.DelegateError, match="실행 중이라 포기하지 않습니다"):
        pd.abandon_ticket_copy(
            copy_path=dead.path, cwd=slot, pm_home=pm_home,
            superseded_by_ordinal=live.ordinal,
        )

    loud = capsys.readouterr().err
    assert "대체-확인" not in loud
    assert "abandoned_at" not in _ledger_rows(pm_home)[-1]


# ── CLI 표면 (argparse 배선) ─────────────────────────────────────────────────

def _cli_ticket_owner(pd, monkeypatch, pm_home: Path):
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)


def test_cli_abandon_wires_the_superseded_by_flag(pd, env, monkeypatch):
    """CLI 표면 — `--superseded-by` 가 argparse 를 거쳐 실제 처분까지 전달된다."""
    pm_home, slot, tickets, sync_log = env
    _write_spec(tickets, "T-9007")
    round1 = pd.prepare_ticket_copy(
        ticket="T-9007", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round1.path, _review_body(pd, "code-reviewer", ["F-021"]))
    round2 = pd.prepare_ticket_copy(
        ticket="T-9007", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    _write_round_output(round2.path, _review_body(pd, "code-reviewer", ["F-021"]))
    pd.harvest_ticket_copy(copy_path=round2.path, cwd=slot, pm_home=pm_home)
    _cli_ticket_owner(pd, monkeypatch, pm_home)

    rc = pd._cmd_ticket([
        "abandon", "--copy", str(round1.path), "--cwd", str(slot),
        "--assume-dead", "--superseded-by", str(round2.ordinal),
    ])

    assert rc == 0
    assert not round1.run_dir.exists()
    unharvested = pd.ticket_copy_records(pm_home, ticket="T-9007", unharvested=True)
    assert unharvested == []
