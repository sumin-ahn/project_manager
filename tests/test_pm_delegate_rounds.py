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

    board._growth_mutation_sync_paths = _sync
    board._growth_mutation_sync = lambda message, path: _sync(message, (path,))
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

    with pytest.raises(pd.DelegateError, match="읽기 실패"):
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


def test_role_sets_match_the_rounds_seam(pd):
    """역할 집합의 단일 진실은 seam 이다 — 라벨 사본은 두지 않는다([[ADR-0090]] R4 에서 삭제)."""
    rounds_module = pd._load_ticket_rounds()
    assert not hasattr(pd, "EXTERNAL_REVIEW_SECTION_LABEL")
    assert set(pd.TICKET_COPY_ROLES) == set(rounds_module.ROLES)
