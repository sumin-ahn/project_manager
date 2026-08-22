"""T-0789 — kill 된 위임 잔여 정리(`ticket abandon`) + 판정 표면 pending 배제.

두 축을 검증한다.
  (1) `abandon_ticket_copy` — 시드 그대로·최대 순번인 예약을 board 라운드 파일 + PM 홈 장부
      행 + slot run-dir 세 자산 전부에서 지운다. 산출 있는 라운드·중간 순번은 거부한다.
  (2) `_pm_review_surface_rounds` 의 pending 배제 — 시드 그대로인 리뷰 라운드가 섞여도
      `parse_pm_review_delta`(review delta)·`render_pm_review_disposition_template`
      (disposition-template)·`pm_review_verify_template`/`pm_verified_evidence_problem`
      (verify-template·rounds resolve --pm-verified)가 malformed 로 막히지 않는다.
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
        "design: 'waived: test'\n"
        "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\nabandon 왕복 픽스처.\n"
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
    """PM 홈(board 데이터+엔진 사본)과 ignore 된 슬롯 git 트리 한 쌍 (`test_pm_delegate_rounds.py` 동형)."""
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
    sync_log: list = []
    monkeypatch.setattr(
        pd, "_load_board_for_repo",
        lambda _repo: _fixture_board(pd, pm_home, sync_log),
    )
    return pm_home, slot, tickets, sync_log


def _write_spec(tickets: Path, ticket: str, **kwargs) -> Path:
    path = tickets / f"{ticket}-abandon.md"
    path.write_text(_spec_text(ticket, **kwargs), encoding="utf-8", newline="\n")
    return path


def _rounds_dir(pm_home: Path, ticket: str) -> Path:
    return pm_home / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket


def _ledger_rows(pm_home: Path) -> list[dict]:
    path = pm_home / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# ── abandon: 세 자산 실물 왕복 (PM 지정 검증 근거) ─────────────────────────

def test_abandon_removes_all_three_kill_residue_assets(pd, rounds_env):
    """실제 prepare 로 만든 세 자산(run-dir·board 라운드 파일·장부 행)이 abandon 뒤 모두 사라진다."""
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-8001")
    plan = pd.prepare_ticket_copy(
        ticket="T-8001", role="developer", cwd=slot, pm_home=pm_home,
    )
    # 자산 1: run-dir · 자산 2: board 라운드 파일 · 자산 3: 장부 미회수 행 — 셋 다 실재한다.
    assert plan.run_dir.exists()
    assert plan.board_path.exists()
    before = _ledger_rows(pm_home)
    assert before[-1]["copy"] == str(plan.path) and before[-1]["harvested_at"] is None
    assert "abandoned_at" not in before[-1]

    result = pd.abandon_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    assert result.changed is True and result.sync_ready is True
    assert not plan.run_dir.exists()             # 자산 1 소멸
    assert not plan.board_path.exists()           # 자산 2 소멸
    row = _ledger_rows(pm_home)[-1]
    assert row["abandoned_at"] is not None and row["harvested_at"] is None  # 자산 3 마감
    assert pd.ticket_copy_records(pm_home, ticket="T-8001", unharvested=True) == []
    assert sync_log[-1][1] == [plan.board_path]


def test_abandon_lets_reprepare_reuse_the_same_ordinal(pd, rounds_env):
    """최대 순번 삭제라 재 prepare 가 같은 순번을 다시 채번한다(삭제 정의의 직접 증거)."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8002")
    first = pd.prepare_ticket_copy(
        ticket="T-8002", role="developer", cwd=slot, pm_home=pm_home,
    )
    pd.abandon_ticket_copy(copy_path=first.path, cwd=slot, pm_home=pm_home)

    second = pd.prepare_ticket_copy(
        ticket="T-8002", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert second.ordinal == first.ordinal == 1


# ── 역방향: 살아있는 자산은 지우지 않는다 ──────────────────────────────────

def test_abandon_leaves_other_live_runs_untouched(pd, rounds_env):
    """한 티켓의 kill 잔여를 지워도 다른 티켓의 진행 중 준비는 세 자산 전부 그대로다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8003")
    _write_spec(tickets, "T-8004")
    dead = pd.prepare_ticket_copy(
        ticket="T-8003", role="developer", cwd=slot, pm_home=pm_home,
    )
    alive = pd.prepare_ticket_copy(
        ticket="T-8004", role="developer", cwd=slot, pm_home=pm_home,
    )

    pd.abandon_ticket_copy(copy_path=dead.path, cwd=slot, pm_home=pm_home)

    assert alive.run_dir.exists() and alive.board_path.exists()
    alive_row = [row for row in _ledger_rows(pm_home) if row["ticket"] == "T-8004"][-1]
    assert alive_row["harvested_at"] is None and "abandoned_at" not in alive_row
    assert pd.ticket_copy_records(pm_home, ticket="T-8004", unharvested=True)[0]["copy"] == (
        str(alive.path)
    )


def test_abandon_refuses_a_round_with_real_output(pd, rounds_env):
    """산출이 있으면(시드 그대로가 아니면) harvest 대상이지 abandon 대상이 아니다."""
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-8005")
    plan = pd.prepare_ticket_copy(
        ticket="T-8005", role="developer", cwd=slot, pm_home=pm_home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n실산출\n", encoding="utf-8", newline="",
    )
    before = plan.board_path.read_bytes()

    with pytest.raises(pd.DelegateError, match="산출이 있어 포기할 수 없습니다"):
        pd.abandon_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    assert plan.board_path.read_bytes() == before
    assert plan.run_dir.exists()
    assert sync_log == []


def test_abandon_refuses_a_middle_ordinal(pd, rounds_env):
    """중간 순번 삭제는 round-gap 을 만든다 — 최대 순번일 때만 허용."""
    pm_home, slot, tickets, sync_log = rounds_env
    _write_spec(tickets, "T-8006")
    first = pd.prepare_ticket_copy(
        ticket="T-8006", role="developer", cwd=slot, pm_home=pm_home,
    )
    second = pd.prepare_ticket_copy(
        ticket="T-8006", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    assert (first.ordinal, second.ordinal) == (1, 2)

    with pytest.raises(pd.DelegateError, match="중간 순번은 포기할 수 없습니다"):
        pd.abandon_ticket_copy(copy_path=first.path, cwd=slot, pm_home=pm_home)

    assert first.board_path.exists() and second.board_path.exists()
    assert first.run_dir.exists() and second.run_dir.exists()
    assert sync_log == []


def test_abandon_refuses_unprepared_path(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8007")
    plan = pd.prepare_ticket_copy(
        ticket="T-8007", role="developer", cwd=slot, pm_home=pm_home,
    )
    smuggled = plan.run_dir / "02-developer.md"
    smuggled.write_text("## 리뷰\n밀반입\n", encoding="utf-8", newline="\n")

    with pytest.raises(pd.DelegateError, match="준비 기록 없음"):
        pd.abandon_ticket_copy(copy_path=smuggled, cwd=slot, pm_home=pm_home)


def test_abandon_refuses_an_already_harvested_run(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8008")
    plan = pd.prepare_ticket_copy(
        ticket="T-8008", role="developer", cwd=slot, pm_home=pm_home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n산출\n", encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    with pytest.raises(pd.DelegateError, match="이미 회수된 준비는 포기할 수 없습니다"):
        pd.abandon_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


def test_abandon_refuses_an_already_abandoned_run(pd, rounds_env):
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8009")
    plan = pd.prepare_ticket_copy(
        ticket="T-8009", role="developer", cwd=slot, pm_home=pm_home,
    )
    pd.abandon_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    with pytest.raises(pd.DelegateError, match="이미 포기된 준비입니다"):
        pd.abandon_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)


def test_abandon_does_not_touch_review_round_ledgers(pd):
    """비목표(설계 불변식 6) — abandon 은 리뷰 라운드 장부를 참조하지 않는다."""
    source = inspect.getsource(pd.abandon_ticket_copy)
    assert "review_rounds" not in source


def test_reproduces_the_seed_unchanged_residue_shape(pd, rounds_env):
    """실물 재현(T-0778 형상) — 산출 있는 01 라운드는 보존, 시드 그대로인 02 만 지워진다."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8010")
    first = pd.prepare_ticket_copy(
        ticket="T-8010", role="architect", cwd=slot, pm_home=pm_home,
    )
    first.path.write_text(
        first.path.read_text(encoding="utf-8") + "\n## 설계\n실산출\n",
        encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=first.path, cwd=slot, pm_home=pm_home)

    second = pd.prepare_ticket_copy(
        ticket="T-8010", role="architect", cwd=slot, pm_home=pm_home,
    )
    # second 는 편집 없이(=시드 그대로) kill 됐다고 가정한다.
    pd.abandon_ticket_copy(copy_path=second.path, cwd=slot, pm_home=pm_home)

    assert sorted(item.name for item in _rounds_dir(pm_home, "T-8010").iterdir()) == (
        ["01-architect.md"]
    )
    latest = {
        row["ordinal"]: row
        for row in pd.ticket_copy_records(pm_home, ticket="T-8010")
    }
    assert latest[1]["harvested_at"] is not None and "abandoned_at" not in latest[1]
    assert latest[2]["abandoned_at"] is not None and latest[2]["harvested_at"] is None


# ── CLI 표면 ────────────────────────────────────────────────────────────

def test_abandon_cli_surface_matches_harvest_argument_shape(pd):
    parser = pd.build_subcommand_parser("ticket")
    text = parser.format_help()
    assert "abandon" in text
    args = parser.parse_args(["abandon", "--copy", "/abs/copy.md", "--cwd", "/abs/cwd"])
    assert args.ticket_command == "abandon"
    assert args.copy == "/abs/copy.md" and args.cwd == "/abs/cwd"


# ── 장부 스키마: 상한-집합 완화(구 8키 ⊆ row ⊆ 8키 ∪ {abandoned_at}) ────────

def _base_row(**overrides) -> dict:
    row = {
        "ticket": "T-8100", "role": "developer", "ordinal": 1,
        "run_id": "a" * 32, "copy": "/abs/copy.md",
        "board_rel": "wiki/tickets/rounds/T-8100/01-developer.md",
        "prepared_at": "2026-08-22T00:00:00+00:00", "harvested_at": None,
    }
    row.update(overrides)
    return row


def test_ledger_row_accepts_legacy_eight_key_rows(pd):
    row = pd._delegate_rounds_ledger_row(_base_row(), line_number=1)
    assert "abandoned_at" not in row


def test_ledger_row_accepts_abandoned_at_as_an_optional_ninth_key(pd):
    row = pd._delegate_rounds_ledger_row(
        _base_row(abandoned_at="2026-08-22T01:00:00+00:00"), line_number=1,
    )
    assert row["abandoned_at"] == "2026-08-22T01:00:00+00:00"


@pytest.mark.parametrize("bad", [None, ""])
def test_ledger_row_rejects_null_or_empty_abandoned_at(pd, bad):
    with pytest.raises(pd.DelegateError, match="값 형식 불일치"):
        pd._delegate_rounds_ledger_row(_base_row(abandoned_at=bad), line_number=1)


def test_ledger_row_still_rejects_unknown_keys(pd):
    with pytest.raises(pd.DelegateError, match="schema 불일치"):
        pd._delegate_rounds_ledger_row(_base_row(mystery="x"), line_number=1)


def test_ledger_row_still_rejects_missing_required_keys(pd):
    row = _base_row()
    del row["harvested_at"]
    with pytest.raises(pd.DelegateError, match="schema 불일치"):
        pd._delegate_rounds_ledger_row(row, line_number=1)


def test_legacy_and_abandoned_rows_coexist_without_corruption_warnings(pd, rounds_env, capsys):
    """실 장부에서 구 8키(harvested) 행 + 신 9키(abandoned_at) 행이 섞여도 손상 행 경고 0."""
    pm_home, slot, tickets, _sync = rounds_env
    _write_spec(tickets, "T-8101")
    _write_spec(tickets, "T-8102")
    legacy_plan = pd.prepare_ticket_copy(
        ticket="T-8101", role="developer", cwd=slot, pm_home=pm_home,
    )
    legacy_plan.path.write_text(
        legacy_plan.path.read_text(encoding="utf-8") + "\n산출\n", encoding="utf-8", newline="",
    )
    pd.harvest_ticket_copy(copy_path=legacy_plan.path, cwd=slot, pm_home=pm_home)  # 구 8키 행

    abandoned_plan = pd.prepare_ticket_copy(
        ticket="T-8102", role="developer", cwd=slot, pm_home=pm_home,
    )
    pd.abandon_ticket_copy(copy_path=abandoned_plan.path, cwd=slot, pm_home=pm_home)  # 신 9키 행

    capsys.readouterr()
    rows = pd.ticket_copy_records(pm_home)
    warning = capsys.readouterr().err
    assert "손상" not in warning
    assert {row["ticket"] for row in rows} == {"T-8101", "T-8102"}


# ══════════════════════════════════════════════════════════════════════════
# 판정 표면 pending 배제 — review delta·disposition-template·verify-template·
# rounds resolve --pm-verified 가 시드 그대로인 리뷰 라운드로 malformed 되지 않는다.
# ══════════════════════════════════════════════════════════════════════════

def _reviewer_round_text(pd, findings: list, *, today: str = "2026-08-21") -> str:
    payload = {"version": pd.PM_REVIEW_VERSION, "findings": findings, "confirmations": []}
    mustfix = "\n".join(f"- {item['id']}" for item in findings) or "- 없음"
    verdict = "반려" if findings else "통과"
    return (
        f"## 리뷰 (code-reviewer · {today})\n\n"
        f"## must-fix\n{mustfix}\n\n"
        f"## 판정\n판정: {verdict}\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _finding(fid: str = "F-001", *, classification: str = "implementation-defect") -> dict:
    return {
        "id": fid, "class": classification, "severity": "must-fix",
        "authority": "[[ADR-0001]] §경계", "evidence": f"{fid} probe",
        "recommendation": f"{fid} fix", "design_change": False,
    }


def _decision(fid: str, decision: str) -> dict:
    return {
        "id": fid, "decision": decision, "reason": f"PM {decision} 근거",
        "scope": f"{fid} 허용 범위" if decision == "accepted" else "", "prerequisite": "",
    }


def _disposition_block(pd, ordinal: int, rows: list) -> str:
    payload = {
        "version": pd.PM_REVIEW_DISPOSITION_VERSION, "reviewer_ordinal": ordinal,
        "dispositions": rows,
    }
    return (
        f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _confirmation_block(pd, round_ordinal: int, rows: list) -> str:
    payload = {
        "version": pd.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
        "round": round_ordinal, "confirmations": rows,
    }
    return (
        f"```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _real_round(pd, ordinal: int, role: str, text: str):
    rounds_module = pd._load_ticket_rounds()
    return rounds_module.Round(
        ordinal=ordinal, role=role, path=Path(rounds_module.round_filename(ordinal, role)),
        text=text, pending=False,
    )


def _pending_round(pd, ordinal: int, role: str = "code-reviewer", *, today: str = "2026-08-22"):
    """엔진이 실제로 렌더한 시드 그대로 라운드 — 손 재타이핑 없이 kill 잔여를 재현한다."""
    rounds_module = pd._load_ticket_rounds()
    seed = rounds_module.render_round_seed(role, "", today=today)
    assert rounds_module._text_is_pending(role, seed) is True  # 전제: 정말 시드 그대로다.
    return rounds_module.Round(
        ordinal=ordinal, role=role, path=Path(rounds_module.round_filename(ordinal, role)),
        text=seed, pending=True,
    )


def test_seed_unchanged_review_round_alone_no_longer_hits_placeholder_finding_parsing(pd):
    """회귀 원본 재현 — 이전엔 placeholder `finding.class` 로 malformed(판정 파이프라인 차단급)."""
    lonely = _pending_round(pd, 1)

    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta("", [lonely])

    assert "finding.class" not in str(caught.value)
    assert "없습니다" in str(caught.value)


def test_pending_review_round_is_excluded_from_the_judgement_surface(pd):
    real = _real_round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    pending = _pending_round(pd, 2)

    surface = pd._pm_review_surface_rounds([real, pending])

    assert [item.ordinal for item in surface] == [1]


def test_pending_review_round_mixed_with_a_real_round_does_not_block_delta(pd):
    """kill 잔여(시드 그대로 02) 가 섞여도 `review delta`(parse_pm_review_delta) 는 정상 판정한다."""
    real = _real_round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    pending = _pending_round(pd, 2)
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])

    delta = pd.parse_pm_review_delta(spec, [real, pending])

    assert [finding.id for finding, _disposition in delta.accepted] == ["F-001"]


def test_disposition_template_targets_the_real_round_not_the_pending_one(pd):
    """`review disposition-template` 이 시드 그대로인 뒷 라운드가 아니라 실 라운드를 대상 삼는다."""
    real = _real_round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    pending = _pending_round(pd, 2)

    rendered = pd.render_pm_review_disposition_template("", [real, pending]).replace(" ", "")

    assert '"reviewer_ordinal":1' in rendered


def test_verify_template_and_pm_verified_survive_a_trailing_pending_review_round(pd):
    """`review verify-template`·`rounds resolve --pm-verified` 도 kill 잔여로 막히지 않는다."""
    real = _real_round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev_text = (
        "## 구현 보충 (developer · 2026-08-21)\n\n"
        "## 변경 파일\n- `x.py`: fix\n\n## 신규 테스트\n- 1개\n\n"
        "## 회귀\n- 커맨드: `pytest`\n- 결과: 1 passed\n\n"
        "## DoD evidence\n- 완료: 됨\n\n## 민감도\n- N/A\n\n"
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps({
            "version": pd.PM_REVIEW_VERIFY_VERSION,
            "verifications": [{
                "id": "F-001", "machine_verifiable": True, "command": "echo hi",
                "expected": "hi", "before": "bye", "reason": "",
            }],
        }, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )
    dev = _real_round(pd, 2, "developer", dev_text)
    # kill 잔여 — dev 이후 다음 리뷰 라운드가 예약만 되고 시드 그대로 남았다(T-0786 실물 형상).
    pending = _pending_round(pd, 3)
    rounds = [real, dev, pending]

    template = pd.pm_review_verify_template(spec, rounds)
    assert [row.id for row in template.machine_rows] == ["F-001"]

    spec_ok = spec + _confirmation_block(
        pd, 2, [{"id": "F-001", "status": "resolved", "command": "echo hi", "observed": "hi"}],
    )
    assert pd.pm_verified_evidence_problem(spec_ok, rounds) is None


def test_pending_round_exclusion_has_no_effect_when_there_is_no_pending_round(pd):
    """부작용 없음 — pending 라운드가 없는 형상은 배제 전과 결과가 같다."""
    real = _real_round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])

    with_only_real = pd.parse_pm_review_delta(spec, [real])
    with_surface_helper = pd._pm_review_surface_rounds([real])

    assert [finding.id for finding, _disposition in with_only_real.accepted] == ["F-001"]
    assert [item.ordinal for item in with_surface_helper] == [1]
