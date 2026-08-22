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

import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from _win_skip import _can_symlink as can_symlink, posix_mode_supported

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
    "_external_review_delta_regression", "_pm_review_delta_malformed_reason",
    "_pm_review_outside_sections_text", "_pm_review_refused_section_keys",
    "_cmd_ticket_seal_backfill", "_seal_backfill_one", "_SEAL_BACKFILL_STATUSES",
)


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_rounds", PM_DELEGATE)
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
        "design: 'waived: test'\n"
        "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\n라운드 사이드카 왕복.\n" + body
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


def _write_spec(tickets: Path, ticket: str, **kwargs) -> Path:
    path = tickets / f"{ticket}-rounds.md"
    path.write_text(_spec_text(ticket, **kwargs), encoding="utf-8", newline="\n")
    return path


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
    assert result.verify_missing == ()        # 리뷰 라운드가 없어 accepted 대상 자체가 없다.
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
    _write_spec(tickets, "T-7006")
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
    _write_spec(tickets, "T-7008")
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
    _write_spec(tickets, "T-7009")
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
    _write_spec(tickets, "T-7010")
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
    _write_spec(tickets, "T-7014")
    first = pd.prepare_ticket_copy(
        ticket="T-7014", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    block = json.dumps({
        "version": pd.PM_REVIEW_VERSION,
        "findings": [{
            "id": "F-042", "class": "implementation-defect", "severity": "must-fix",
            "authority": "[[ADR-0090]] §경계", "evidence": "probe rc=1",
            "recommendation": "F-042만 수정", "design_change": False,
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
    _write_spec(tickets, "T-7015")
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


# ── T-0786: harvest `verify_missing` (표시면 관용) ──────────────────────

def test_harvest_reports_verify_missing_for_unfilled_accepted_findings(pd, rounds_env):
    """dev 라운드 골격에 verify 프리필이 실려도 자리표시자 그대로면 harvest 가 이름을 짚는다."""
    pm_home, slot, tickets, _sync = rounds_env
    spec_path = _write_spec(tickets, "T-7020")

    reviewer = pd.prepare_ticket_copy(
        ticket="T-7020", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    review_payload = {
        "version": pd.PM_REVIEW_VERSION,
        "findings": [{
            "id": "F-001", "class": "implementation-defect", "severity": "must-fix",
            "authority": "[[T-0786]]", "evidence": "probe", "recommendation": "fix",
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

    # verify 골격을 자리표시자 그대로 두고 다른 절만 채워 harvest 대상으로 만든다.
    dev.path.write_text(seed + "\n실산출\n", encoding="utf-8", newline="")
    result = pd.harvest_ticket_copy(copy_path=dev.path, cwd=slot, pm_home=pm_home)

    assert result.changed is True
    assert result.verify_missing == ("F-001",)


# ── 경로 예산 · 권한 표면 ─────────────────────────────────────────────────

def test_run_dir_path_has_no_role_segment_and_fits_budget(pd):
    """`<role>` 세그먼트 제거로 Windows MAX_PATH 여유가 늘었다(경로 예산 회귀)."""
    relative = pd._ticket_copy_relative_path("T-2001", "a" * 32, "01-code-reviewer.md")
    text = relative.as_posix()
    assert "code-reviewer/" not in text.rsplit("/", 1)[0]
    assert len(text) <= 110
    # 이전 레이아웃(`<ticket>/<role>/<run>/ticket-<ticket>.md`)보다 짧다.
    legacy = (
        pd.TICKET_COPY_REL_ROOT / "T-2001" / "code-reviewer" / ("a" * 32)
        / "ticket-T-2001.md"
    ).as_posix()
    assert len(text) < len(legacy)


def _plan_for(pd, tmp_path: Path, ticket: str, run_hex: str):
    run_dir = tmp_path / "worktree" / pd.TICKET_COPY_REL_ROOT / ticket / run_hex
    run_dir.mkdir(parents=True)
    copy = run_dir / "01-code-reviewer.md"
    copy.write_text("## 리뷰 (code-reviewer · 2026-08-18)\n", encoding="utf-8", newline="\n")
    return copy


def test_codex_reviewer_opens_only_the_run_dir(pd, monkeypatch, tmp_path):
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    copy = _plan_for(pd, tmp_path, "T-2001", "a" * 32)
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "code-reviewer", tmp_path / "worktree", "review",
        ticket_copy_path=copy,
    )
    try:
        assert argv[argv.index("-s") + 1] == "workspace-write"
        assert argv[argv.index("--add-dir") + 1] == str(copy.parent)
        assert argv[argv.index("-C") + 1] == str(read_tmp.path)
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


@pytest.mark.parametrize("harness,model", [("claude", "sonnet"), ("opencode", "prov/m")])
def test_non_codex_reviewer_warns_and_keeps_selected_target(
        pd, monkeypatch, tmp_path, capsys, harness, model):
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    copy = _plan_for(pd, tmp_path, "T-2002", "b" * 32)
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        harness, model, None, "code-reviewer", tmp_path / "worktree", "review",
        ticket_copy_path=copy,
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
        tmp_path / "board" / "01-developer.md",
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
            "recommendation": f"{finding_id}만 수정", "design_change": False,
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
    _write_spec(tickets, "T-7203")
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
    _write_spec(tickets, "T-7216")

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
    _write_spec(tickets, "T-7204")
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
    _write_spec(tickets, "T-7206")
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
    _write_spec(tickets, "T-7230")
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
    _write_spec(tickets, "T-7231")
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

    _write_spec(tickets, "T-7232")
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

    _write_spec(tickets, "T-7233")
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
    _write_spec(tickets, "T-7234")
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
    _write_spec(tickets, "T-7240")
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
        pm_home / ".project_manager" / "tools" / "external_review.py",
        "external_review.py", verifier=pd._verify_engine_rev,
    )
    board = _fixture_board(pd, pm_home, sync_log)
    rounds_module = pd._load_ticket_rounds()

    _write_spec(tickets, "T-7235")
    _land_review_round(
        pd, pm_home, slot, "T-7235", _review_body(pd, "code-reviewer", ["F-007"]),
    )
    # 추가 리뷰어 채널의 선행 라운드도 같은 board 트리에 세운다(접두만 다른 같은 형상).
    external_first = rounds_module.reserve_round(
        _rounds_dir(pm_home, "T-7235").parent.parent, "T-7235",
        pd.EXTERNAL_REVIEW_ROLE,
        content=rounds_module.render_round_header(
            pd.EXTERNAL_REVIEW_ROLE, today="2026-08-22",
        ) + "\n\n" + _review_body(pd, pd.EXTERNAL_REVIEW_ROLE, ["X-007"]),
        lock=board.board_lock(),
    )
    assert external_first.exists()

    internal_round = pd.prepare_ticket_copy(
        ticket="T-7235", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    reserved = internal_round.board_path.read_bytes()
    _write_round_output(
        internal_round.path, _review_body(pd, "code-reviewer", ["F-007"], ["F-007"]),
    )
    external_reply = _review_body(pd, pd.EXTERNAL_REVIEW_ROLE, ["X-007"], ["X-007"])
    capsys.readouterr()

    internal_rc = _harvest_rc(pd, monkeypatch, pm_home, slot, internal_round.path)
    internal_error = capsys.readouterr().err
    external_problem = external._reserve_external_review_round(
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
    assert _round_names(pm_home, "T-7235", pd.EXTERNAL_REVIEW_ROLE) == [
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
    spec_path = _write_spec(tickets, "T-7236")
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
    _write_spec(tickets, "T-7237")
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
    _write_spec(tickets, "T-7238")
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
    _write_spec(tickets, "T-7239")
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
    spec_path = _write_spec(tickets, "T-7240")
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
    spec_path = _write_spec(tickets, "T-7241")
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
