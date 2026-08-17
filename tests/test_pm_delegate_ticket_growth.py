"""T-0676 — slot 티켓 성장 사본 prepare/harvest 경계."""
from __future__ import annotations

import argparse
import ast
import errno
import importlib.util
import contextlib
import hashlib
import io
import itertools
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from _win_skip import posix_mode_supported

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_DELEGATE = TOOLS / "pm_delegate.py"

GROWTH_ROLES = ("architect", "developer", "code-reviewer")
GROWTH_MATRIX = tuple(itertools.product(("claude", "codex", "opencode"), repeat=2))
GROWTH_MATRIX = tuple(
    (main, target, role)
    for main, target in GROWTH_MATRIX
    for role in GROWTH_ROLES
)
SELECTED_RELEASE_ROUTES = (
    ("claude", "claude"),
    ("codex", "codex"),
    ("opencode", "opencode"),
    ("claude", "codex"),
    ("claude", "opencode"),
    ("codex", "claude"),
)
SELECTED_RELEASE_CELLS = tuple(
    (main, target, role)
    for main, target in SELECTED_RELEASE_ROUTES
    for role in GROWTH_ROLES
)
NATIVE_CARDS = {
    "claude": REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md",
    "codex": REPO / "templates" / "codex" / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md",
    "opencode": REPO / "templates" / "opencode" / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md",
}
NATIVE_TOOL = {"claude": "Agent 툴 호출", "codex": "spawn_agent(", "opencode": "task tool 호출"}


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_growth", PM_DELEGATE)
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


def _ticket_text(ticket: str, sections: list[tuple[str, str]]) -> str:
    body = [
        "---\n", f"id: {ticket}\n", "title: 성장 사본\n", "status: claimed\n",
        "created: '2026-08-13'\n", "created_by: test\n", "claimed_by: test/slot\n",
        "claimed_at: '2026-08-13T00:00:00+00:00'\n", "completed_at: null\n",
        "depends_on: []\n", "blocks: []\n", "touches: []\n", "estimate: medium\n",
        "design: 'waived: test'\n", "tags: []\n", "---\n",
        f"# {ticket}\n\n## 목표\n성장 marker 평문 `pm-ticket-section:start/end role=<role>` 설명.\n",
    ]
    labels = {"developer": "구현 보충", "code-reviewer": "리뷰", "architect": "설계"}
    ordinals: dict[str, int] = {}
    for role, content in sections:
        ordinal = ordinals.get(role, 0)
        ordinals[role] = ordinal + 1
        section_content = f"## {labels[role]} ({role} · 2026-08-13)\n\n" + content
        digest = _load_pd().seal_for(section_content.encode("utf-8"))
        body += [
            f"\n<!-- pm-ticket-section:start role={role} -->\n",
            section_content,
            f"<!-- pm-ticket-section:end role={role} -->\n",
            f"<!-- pm-ticket-seal role={role} ordinal={ordinal} sha256={digest} "
            "by=backfill -->\n",
        ]
    return "".join(body)


@pytest.fixture
def growth_env(tmp_path, pd, monkeypatch):
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
    monkeypatch.setenv("GIT_AUTHOR_NAME", "growth")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "growth@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "growth")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "growth@test.invalid")
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    # focused helper tests do not exercise board-git remote sync/render.
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, pm_home))
    return pm_home, slot, tickets


def _fixture_board(pd, pm_home: Path):
    board = pd._load_module_from_path(
        pm_home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = pm_home
    board.LOCAL_DIR = pm_home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"
    board._growth_mutation_sync = lambda _message, _path: True
    board._growth_mutation_sync_paths = lambda _message, _paths: True
    return board


def _seed_growth_ledger(ticket: str, path: Path) -> Path:
    """봉인 도입 이후 형상 — 현재 봉인들을 그 티켓 장부에 기재한다(sweep 등가·T-0699).

    장부 축은 봉인 축과 함께 도입됐다. 봉인만 있고 장부가 없는 상태는 마이그레이션 대상이라
    성장 write 가 loud 거부하므로, 성장 왕복 fixture 는 마이그레이션이 끝난 보드를 만든다.
    """
    pd = _load_pd()
    growth_dir = pd.ticket_growth_dir_for_ticket_path(path)
    pd.append_ticket_growth_records(
        growth_dir, ticket, path.read_text(encoding="utf-8"),
        by="backfill", stamp=False,
    )
    return pd.ticket_growth_ledger_path(growth_dir, ticket)


def _write_ticket(tickets: Path, ticket: str, sections: list[tuple[str, str]]) -> Path:
    path = tickets / f"{ticket}-growth.md"
    path.write_text(_ticket_text(ticket, sections), encoding="utf-8", newline="\n")
    _seed_growth_ledger(ticket, path)
    return path


def _without_ticket_seals(pd, text: str) -> str:
    seals = sorted(
        pd.parse_ticket_seals(text).values(),
        key=lambda seal: seal.line_start,
        reverse=True,
    )
    for seal in seals:
        text = text[:seal.line_start] + text[seal.line_end:]
    return text


def _replace_content(pd, path: Path, role: str, ordinal: int, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    section = pd._ticket_role_section(text, role, ordinal=ordinal)
    path.write_text(
        text[:section.content_start] + content + text[section.content_end:],
        encoding="utf-8",
        newline="\n",
    )


def _function_calls(source: str, function: str) -> set[str]:
    """정적 seam 가드용 함수별 직접 호출 이름 집합."""
    tree = ast.parse(source)
    owner = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
    )
    calls: set[str] = set()
    for node in ast.walk(owner):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def _missing_ticket_seal_seam_edges(
        delegate_source: str, board_source: str) -> set[str]:
    """봉인 writer 전주체와 verifier가 canonical hash seam을 우회했는지 판정.

    성장 절을 쓰는 두 주체(사본 harvest·external-reviewer 절 기록)는 봉인 재발급을 공용
    seam(`seal_and_replace_ticket_text`) 하나로만 지난다 — 지점이 늘면 봉인 규칙과 그 위에
    붙는 부기가 호출부마다 갈린다.
    """
    required = {
        "_ticket_seal_hash_input": {"replace", "encode"},
        "seal_for": {"_ticket_seal_hash_input", "sha256"},
        "_ticket_seal_line": {"seal_for"},
        # 봉인 축 판정은 구조적 함수가 hash seam 을 직접 재계산하고, 공개 문자열 축은 그
        # 구조 판정을 그대로 옮긴다(T-0699 F-021 이관).
        "_verify_ticket_seal_problems": {"seal_for"},
        "verify_ticket_seals": {"_verify_ticket_seal_problems"},
        "verify_ticket_growth": {"_verify_ticket_seal_problems"},
        "_upsert_ticket_seal": {"_ticket_seal_line"},
        "backfill_ticket_seals": {"_ticket_seal_line"},
        # 장부 레코드의 sha 도 같은 seam 으로 계산해야 봉인과 장부가 같은 값을 든다.
        "append_ticket_growth_records": {"seal_for"},
        "ticket_growth_misplaced_seal_keys": {"seal_for"},
        # 봉인 write 와 장부 append 는 한 seam 안에서 함께 일어난다(쓰기 주체가 늘어도
        # 그 seam 을 지나면 레코드가 남는다) — harvest 와 external-reviewer 회수가 모두 지난다.
        "upsert_ticket_seal_with_ledger": {"_upsert_ticket_seal",
                                           "append_ticket_growth_records"},
        "harvest_ticket_copy": {"seal_for", "upsert_ticket_seal_with_ledger"},
        "write_external_reviewer_section": {"upsert_ticket_seal_with_ledger"},
    }
    missing = {
        f"pm_delegate.{function}->{callee}"
        for function, callees in required.items()
        for callee in callees
        if callee not in _function_calls(delegate_source, function)
    }
    if "_ticket_seal_line" not in _function_calls(board_source, "cmd_section_add"):
        missing.add("board.cmd_section_add->_ticket_seal_line")
    return missing


@pytest.mark.parametrize(
    ("main_harness", "target_harness", "role"),
    GROWTH_MATRIX,
    ids=lambda value: value,
)
def test_ticket_growth_main_target_role_matrix_27_cells(
        pd, main_harness, target_harness, role):
    """3 main × 3 target × 3 성장 역할을 실패 클래스와 곱하지 않고 한 표로 고정한다."""
    assert len(GROWTH_MATRIX) == 27 and len(set(GROWTH_MATRIX)) == 27
    if main_harness == target_harness:
        card = NATIVE_CARDS[main_harness].read_text(encoding="utf-8")
        assert "prepare →" in card and "→ harvest" in card
        assert NATIVE_TOOL[main_harness] in card
        assert f"role={role}" in card
        assert "<prepare JSON의 copy>" in card
        return

    # cross는 main에 따라 target을 바꾸지 않는다. 실제 argv의 첫 프로그램과 카드의 warning 계약을
    # 함께 고정하고, 실패 클래스(HMAC/stale/marker 등)는 기존 공용 역방향 테스트로 분리한다.
    argv = pd._build_target_argv(
        target_harness, "model-x", None, role, Path("/worktree"), Path("/prompt.md")
    )
    assert argv[0] == target_harness
    card = NATIVE_CARDS[main_harness].read_text(encoding="utf-8")
    assert "사용자가 고른 target으로 계속 실행" in card
    assert "target 자동 대체" in card or "자동 대체" in card


def test_selected_release_ticket_growth_manifest_is_exactly_18_cells():
    """T-0685 사용자 선택: native 3경로 + 실제 사용 cross 3경로만 release live evidence를 가진다."""
    assert len(SELECTED_RELEASE_CELLS) == 18
    assert len(set(SELECTED_RELEASE_CELLS)) == 18
    assert set(SELECTED_RELEASE_CELLS).issubset(set(GROWTH_MATRIX))
    assert set(SELECTED_RELEASE_ROUTES) == {
        ("claude", "claude"), ("codex", "codex"), ("opencode", "opencode"),
        ("claude", "codex"), ("claude", "opencode"), ("codex", "claude"),
    }

    wave = (REPO / "tests" / "test_release_wave.py").read_text(encoding="utf-8")
    cross = (REPO / "tests" / "test_pm_delegate_live.py").read_text(encoding="utf-8")
    assert "test_release_wave_claude_full_wave" in wave
    assert "test_release_wave_opencode_full_wave" in wave
    assert "test_release_wave_codex_native_ticket_growth" in wave
    for name in (
        "test_ticket_growth_cross_claude_to_codex_release",
        "test_ticket_growth_cross_claude_to_opencode_release",
        "test_ticket_growth_cross_codex_to_claude_release",
    ):
        assert name in cross


@pytest.mark.parametrize("role", ["developer", "code-reviewer", "architect"])
def test_prepare_edit_harvest_round_trip_and_git_hidden(growth_env, pd, role):
    pm_home, slot, tickets = growth_env
    ticket = "T-1001"
    source = _write_ticket(tickets, ticket, [(role, "")])
    plan = pd.prepare_ticket_copy(ticket=ticket, role=role, cwd=slot, pm_home=pm_home)

    assert plan.path.is_relative_to(slot)
    if posix_mode_supported():
        assert stat.S_IMODE(plan.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(plan.baseline_path.stat().st_mode) == 0o400
        assert stat.S_IMODE(
            plan.path.with_name(pd.TICKET_COPY_TAG_NAME).stat().st_mode
        ) == 0o400
    assert _git(slot, "status", "--short").stdout == ""
    _replace_content(pd, plan.path, role, 0, f"{role} 사실 기록\n")
    first = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert first == pd.TicketHarvestResult(True, True)
    assert f"{role} 사실 기록" in source.read_text(encoding="utf-8")
    assert plan.path.exists() and plan.baseline_path.exists()
    second = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert second == pd.TicketHarvestResult(False, True)


def test_ticket_copy_ledger_registers_and_harvest_resolves_without_stdin(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, tickets = growth_env
    ticket = "T-1100"
    source = _write_ticket(tickets, ticket, [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
    )
    ledger = pm_home / pd.TICKET_COPY_LEDGER_REL_PATH
    first = pd.ticket_copy_records(pm_home)
    assert len(first) == 1
    assert first[0] == {
        "ticket": ticket,
        "role": "developer",
        "ordinal": 0,
        "run_id": plan.path.parent.name,
        "copy": str(plan.path.resolve()),
        "capability": plan.capability.hex(),
        "prepared_at": first[0]["prepared_at"],
        "harvested_at": None,
    }
    assert set(first[0]) == {
        "ticket", "role", "ordinal", "run_id", "copy", "capability",
        "prepared_at", "harvested_at",
    }
    _replace_content(pd, plan.path, "developer", 0, "ledger-resolved\n")
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    assert pd.main([
        "ticket", "harvest", "--copy", str(plan.path), "--cwd", str(slot),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["changed"] is True
    assert "ledger-resolved" in source.read_text(encoding="utf-8")
    latest = pd.ticket_copy_records(pm_home)[0]
    assert latest["harvested_at"] is not None
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2


def test_ticket_copy_stdin_precedes_capability_but_ledger_mismatch_is_loud(
        growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1101", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1101", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 0, "stdin wins\n")
    wrong_capability = dict(pd.ticket_copy_records(pm_home)[0])
    wrong_capability["capability"] = (b"x" * 32).hex()
    pd._append_ticket_copy_ledger(pm_home, wrong_capability)

    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert result.changed and "stdin wins" in source.read_text(encoding="utf-8")
    assert pd.ticket_copy_records(pm_home)[0]["capability"] == plan.capability.hex()

    corrupt = dict(pd.ticket_copy_records(pm_home)[0])
    corrupt["role"] = "architect"
    pd._append_ticket_copy_ledger(pm_home, corrupt)
    before = source.read_bytes()
    with pytest.raises(pd.DelegateError, match="장부 동일 copy 불변 필드 불일치"):
        pd.harvest_ticket_copy(
            copy_path=plan.path, cwd=slot, pm_home=pm_home,
            capability=plan.capability,
        )
    assert source.read_bytes() == before


def test_explicit_capability_harvests_pre_ledger_copy_and_backfills(
        growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1110", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1110", role="developer", cwd=slot, pm_home=pm_home,
    )
    ledger = pm_home / pd.TICKET_COPY_LEDGER_REL_PATH
    ledger.rename(ledger.with_suffix(".pre-ledger"))
    _replace_content(pd, plan.path, "developer", 0, "legacy capability harvest\n")

    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )

    assert result == pd.TicketHarvestResult(True, True)
    assert "legacy capability harvest" in source.read_text(encoding="utf-8")
    rows = pd.ticket_copy_records(pm_home)
    assert len(rows) == 1
    assert rows[0]["copy"] == str(plan.path.resolve())
    assert rows[0]["capability"] == plan.capability.hex()
    assert rows[0]["harvested_at"] is not None


def test_no_stdin_corrupt_target_ledger_row_reports_damage(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1117", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1117", role="developer", cwd=slot, pm_home=pm_home,
    )
    ledger = pm_home / pd.TICKET_COPY_LEDGER_REL_PATH
    payload = ledger.read_text(encoding="utf-8")
    assert payload.endswith("}\n")
    ledger.write_text(payload[:-2] + "\n", encoding="utf-8", newline="\n")
    _replace_content(pd, plan.path, "developer", 0, "must not harvest\n")
    before = source.read_bytes()
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)

    assert pd.main([
        "ticket", "harvest", "--copy", str(plan.path), "--cwd", str(slot),
    ]) == 1

    error = capsys.readouterr().err
    assert "장부 대상 사본 등록 여부를 확인할 수 없습니다 — 손상 행 존재" in error
    assert "ticket-copy 장부 JSON 손상 건너뜀" in error
    assert "line=1" in error
    assert "장부에 사본 등록이 없습니다" not in error
    assert source.read_bytes() == before


def test_explicit_capability_warns_about_retained_corrupt_target_row(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1118", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1118", role="developer", cwd=slot, pm_home=pm_home,
    )
    ledger = pm_home / pd.TICKET_COPY_LEDGER_REL_PATH
    payload = ledger.read_text(encoding="utf-8")
    assert payload.endswith("}\n")
    ledger.write_text(payload[:-2] + "\n", encoding="utf-8", newline="\n")
    _replace_content(pd, plan.path, "developer", 0, "explicit recovery\n")
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    monkeypatch.setattr(pd.sys, "stdin", io.StringIO(plan.capability.hex() + "\n"))

    assert pd.main([
        "ticket", "harvest", "--copy", str(plan.path), "--cwd", str(slot),
        "--capability-stdin",
    ]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["changed"] is True
    assert "장부 대상 사본 조회 중 손상 행이 잔존합니다" in captured.err
    assert "ticket-copy 장부 JSON 손상 건너뜀" in captured.err
    assert "line=1" in captured.err
    assert "explicit recovery" in source.read_text(encoding="utf-8")
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    with pytest.raises(ValueError):
        json.loads(lines[0])
    assert json.loads(lines[1])["copy"] == str(plan.path.resolve())


def test_no_stdin_unregistered_copy_fails_loud(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1119", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1119", role="developer", cwd=slot, pm_home=pm_home,
    )
    ledger = pm_home / pd.TICKET_COPY_LEDGER_REL_PATH
    ledger.rename(ledger.with_suffix(".pre-ledger"))
    _replace_content(pd, plan.path, "developer", 0, "must remain unharvested\n")
    before = source.read_bytes()
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)

    assert pd.main([
        "ticket", "harvest", "--copy", str(plan.path), "--cwd", str(slot),
    ]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "오류: ticket harvest 실패: ticket-copy 장부에 사본 등록이 없습니다: "
        f"{plan.path.resolve()}\n"
    )
    assert source.read_bytes() == before


def test_ticket_copies_cli_lists_and_filters_unharvested(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1102", [("developer", "")])
    _write_ticket(tickets, "T-1103", [("developer", "")])
    harvested = pd.prepare_ticket_copy(
        ticket="T-1102", role="developer", cwd=slot, pm_home=pm_home,
    )
    pending = pd.prepare_ticket_copy(
        ticket="T-1103", role="developer", cwd=slot, pm_home=pm_home,
    )
    pd.harvest_ticket_copy(
        copy_path=harvested.path, cwd=slot, pm_home=pm_home,
    )
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: pm_home)

    assert pd.main(["ticket", "copies", "--unharvested"]) == 0
    output = capsys.readouterr().out
    assert f"조회 장부: {pm_home / pd.TICKET_COPY_LEDGER_REL_PATH}" in output
    assert "미회수 ticket copies 1건" in output
    assert str(pending.path) in output and str(harvested.path) not in output
    assert "role=developer · ordinal=0 · run_id=" in output

    assert pd.main(["ticket", "copies", "--ticket", "T-1102"]) == 0
    filtered = capsys.readouterr().out
    assert str(harvested.path) in filtered and str(pending.path) not in filtered
    assert "회수(" in filtered


def test_prepare_transfer_from_preserves_new_baseline_outside_and_harvests(
        growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1104", [("developer", "")])
    old = pd.prepare_ticket_copy(
        ticket="T-1104", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, old.path, "developer", 0, "first harvest\n")
    pd.harvest_ticket_copy(copy_path=old.path, cwd=slot, pm_home=pm_home)
    _replace_content(pd, old.path, "developer", 0, "amended old copy\r\n")
    current = source.read_text(encoding="utf-8")
    source.write_text(current.replace("## 목표\n", "## 최신 baseline 밖 변경\n\n## 목표\n"), encoding="utf-8", newline="\n")

    transferred = pd.prepare_ticket_copy(
        ticket="T-1104", role="developer", cwd=slot, pm_home=pm_home,
        transfer_from=old.path,
    )
    metadata = json.loads(transferred.metadata_path.read_text(encoding="utf-8"))
    assert metadata["transferred_from"] == str(old.path.resolve())
    assert old.capability.hex() not in json.dumps(metadata)
    with transferred.baseline_path.open("r", encoding="utf-8", newline="") as handle:
        baseline = handle.read()
    with transferred.path.open("r", encoding="utf-8", newline="") as handle:
        copy = handle.read()
    baseline_section = pd._ticket_role_section(baseline, "developer", ordinal=0)
    copy_section = pd._ticket_role_section(copy, "developer", ordinal=0)
    assert copy[copy_section.content_start:copy_section.content_end] == "amended old copy\r\n"
    assert (
        baseline[:baseline_section.content_start] + baseline[baseline_section.content_end:]
        == copy[:copy_section.content_start] + copy[copy_section.content_end:]
    )
    result = pd.harvest_ticket_copy(
        copy_path=transferred.path, cwd=slot, pm_home=pm_home,
    )
    with source.open("r", encoding="utf-8", newline="") as handle:
        final = handle.read()
    assert result.changed and "amended old copy\r\n" in final
    assert "최신 baseline 밖 변경" in final


def test_prepare_transfer_from_rejects_missing_section_and_role_mismatch(
        growth_env, pd):
    pm_home, slot, tickets = growth_env
    _write_ticket(
        tickets, "T-1105", [("developer", ""), ("architect", "")],
    )
    old = pd.prepare_ticket_copy(
        ticket="T-1105", role="developer", cwd=slot, pm_home=pm_home,
    )
    with pytest.raises(pd.DelegateError, match="--transfer-from 역할 불일치"):
        pd.prepare_ticket_copy(
            ticket="T-1105", role="architect", cwd=slot, pm_home=pm_home,
            transfer_from=old.path,
        )
    old_text = old.path.read_text(encoding="utf-8")
    old_section = pd._ticket_role_section(old_text, "developer", ordinal=0)
    old.path.write_text(
        old_text[:old_section.marker_start] + old_text[old_section.marker_end:],
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(pd.DelegateError, match="role=developer 성장 절이 없습니다"):
        pd.prepare_ticket_copy(
            ticket="T-1105", role="developer", cwd=slot, pm_home=pm_home,
            transfer_from=old.path,
        )


def test_prepare_transfer_from_rejects_cross_ticket_before_creating_copy(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1111", [("developer", "foreign output\n")])
    _write_ticket(tickets, "T-1112", [("developer", "")])
    old = pd.prepare_ticket_copy(
        ticket="T-1111", role="developer", cwd=slot, pm_home=pm_home,
    )
    trust_root = pm_home / pd.TICKET_COPY_TRUST_REL_ROOT
    trust_before = sorted(path.name for path in trust_root.iterdir())
    rows_before = pd.ticket_copy_records(pm_home)
    monkeypatch.setattr(pd, "_repo_root_for_cwd", lambda _cwd: slot)
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)

    assert pd.main([
        "ticket", "prepare", "--ticket", "T-1112", "--role", "developer",
        "--cwd", str(slot), "--transfer-from", str(old.path),
    ]) == 1

    assert capsys.readouterr().err == (
        "오류: ticket prepare 실패: --transfer-from ticket 불일치: "
        "old=T-1111 · new=T-1112\n"
    )
    assert sorted(path.name for path in trust_root.iterdir()) == trust_before
    assert pd.ticket_copy_records(pm_home) == rows_before


def test_ticket_copy_ledger_mode_is_0600(growth_env, pd):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1106", [("developer", "")])
    pd.prepare_ticket_copy(
        ticket="T-1106", role="developer", cwd=slot, pm_home=pm_home,
    )
    ledger = pm_home / pd.TICKET_COPY_LEDGER_REL_PATH
    if posix_mode_supported():
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o600


def _ledger_row(slot: Path, ticket: str) -> dict:
    return {
        "ticket": ticket,
        "role": "developer",
        "ordinal": 0,
        "run_id": "0" * 32,
        "copy": str((slot / f"{ticket}-developer-0.md").resolve()),
        "capability": "a" * 64,
        "prepared_at": "2026-08-17T00:00:00+00:00",
        "harvested_at": None,
    }


def test_ticket_copy_ledger_append_syncs_on_the_writable_fd(
        growth_env, pd, monkeypatch):
    """장부 내구성은 공용 append seam이 연 쓰기 fd 위에서 수행된다 (재-open sync 0·T-0716)."""
    pm_home, slot, _tickets = growth_env
    ledger = pm_home / pd.TICKET_COPY_LEDGER_REL_PATH
    row = _ledger_row(slot, "T-1120")
    # fd 번호는 close 뒤 재사용되므로 열림 시점 좌표를 sync 시점에 확정한다.
    live: dict[int, tuple[str, int]] = {}
    opens: list[tuple[str, int]] = []
    synced: list[tuple[str, int]] = []
    real_open, real_fsync = os.open, os.fsync

    def _record_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        live[fd] = (str(path), flags)
        opens.append((str(path), flags))
        return fd

    def _record_fsync(fd):
        synced.append(live[fd])
        real_fsync(fd)

    monkeypatch.setattr(os, "open", _record_open)
    monkeypatch.setattr(os, "fsync", _record_fsync)
    pd._append_ticket_copy_ledger(pm_home, row)

    assert json.loads(ledger.read_text(encoding="utf-8").splitlines()[0]) == row
    assert len(synced) == 1
    synced_path, synced_flags = synced[0]
    assert synced_path == str(ledger)
    assert synced_flags & os.O_WRONLY == os.O_WRONLY
    ledger_flags = [flags for path, flags in opens if path == str(ledger)]
    assert ledger_flags and all(
        flags & os.O_WRONLY == os.O_WRONLY for flags in ledger_flags
    ), "장부를 읽기 전용으로 다시 열어 sync하면 Windows에서 EBADF다"


def test_ticket_copy_prepare_survives_windows_readonly_fsync_rejection(
        growth_env, pd, monkeypatch):
    """Windows `_commit()` 형상 — 읽기 전용 fd fsync를 EBADF로 거부해도 prepare가 성공한다."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1121", [("developer", "")])
    # fd 번호는 재사용되므로 열림 시점 파일 정체성(dev·ino)까지 함께 박아 대조한다.
    opened_by_fd: dict[int, tuple[int, tuple[int, int]]] = {}
    rejected: list[int] = []
    real_open, real_fsync = os.open, os.fsync

    def _identity(fd: int) -> tuple[int, int]:
        observed = os.fstat(fd)
        return observed.st_dev, observed.st_ino

    def _record_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        opened_by_fd[fd] = (flags, _identity(fd))
        return fd

    def _windows_fsync(fd):
        # 추적하지 못한 fd는 쓰기 가능으로 본다 (거부는 관측된 읽기 전용 open에만).
        flags, identity = opened_by_fd.get(fd, (os.O_WRONLY, None))
        if identity is not None and identity != _identity(fd):
            flags = os.O_WRONLY
        if not flags & (os.O_WRONLY | os.O_RDWR):
            rejected.append(fd)
            raise OSError(errno.EBADF, "Bad file descriptor")
        real_fsync(fd)

    monkeypatch.setattr(os, "open", _record_open)
    monkeypatch.setattr(os, "fsync", _windows_fsync)
    plan = pd.prepare_ticket_copy(
        ticket="T-1121", role="developer", cwd=slot, pm_home=pm_home,
    )

    assert rejected == [], "읽기 전용 fd에 fsync를 걸었다 (Windows에서 EBADF)"
    assert pd.ticket_copy_records(pm_home)[0]["copy"] == str(plan.path.resolve())


def test_ticket_copy_lifecycle_without_posix_mode_capability(
        growth_env, pd, monkeypatch):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1113", [("developer", "")])
    monkeypatch.setattr(pd, "_posix_mode_supported", lambda _directory: False)
    monkeypatch.delattr(pd.os, "fchmod", raising=False)

    plan = pd.prepare_ticket_copy(
        ticket="T-1113", role="developer", cwd=slot, pm_home=pm_home,
    )
    ledger = pm_home / pd.TICKET_COPY_LEDGER_REL_PATH
    ledger.chmod(0o666)
    assert pd.ticket_copy_records(pm_home)[0]["copy"] == str(plan.path.resolve())
    _replace_content(pd, plan.path, "developer", 0, "non-posix lifecycle\n")
    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
    )

    assert result == pd.TicketHarvestResult(True, True)
    assert "non-posix lifecycle" in source.read_text(encoding="utf-8")


def test_harvest_ledger_completion_failure_warns_but_returns_success(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1114", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1114", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 0, "ticket write survives ledger failure\n")
    real_append = pd._append_ticket_copy_ledger

    def fail_completion(owner, row):
        if row["harvested_at"] is not None:
            raise pd.DelegateError("injected ledger append failure")
        return real_append(owner, row)

    monkeypatch.setattr(pd, "_append_ticket_copy_ledger", fail_completion)
    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
    )

    assert result == pd.TicketHarvestResult(True, True)
    assert "ticket write survives ledger failure" in source.read_text(encoding="utf-8")
    assert pd.ticket_copy_records(pm_home)[0]["harvested_at"] is None
    warning = capsys.readouterr().err
    assert "반영은 완료됐지만 장부 완료 기록에 실패" in warning
    assert "injected ledger append failure" in warning


def test_corrupt_copy_ledger_row_is_scoped_and_copies_warns(
        growth_env, pd, capsys):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1115", [("developer", "")])
    second_source = _write_ticket(tickets, "T-1116", [("developer", "")])
    damaged = pd.prepare_ticket_copy(
        ticket="T-1115", role="developer", cwd=slot, pm_home=pm_home,
    )
    healthy = pd.prepare_ticket_copy(
        ticket="T-1116", role="developer", cwd=slot, pm_home=pm_home,
    )
    corrupt = dict(pd.ticket_copy_records(pm_home)[-1])
    assert corrupt["copy"] == str(damaged.path.resolve())
    corrupt["role"] = "architect"
    pd._append_ticket_copy_ledger(pm_home, corrupt)

    rows = pd.ticket_copy_records(pm_home)
    warning = capsys.readouterr().err
    assert len(rows) == 2
    assert "동일 copy 불변 필드 불일치" in warning
    assert "손상 행 건너뜀" in warning
    _replace_content(pd, healthy.path, "developer", 0, "healthy harvest\n")
    result = pd.harvest_ticket_copy(
        copy_path=healthy.path, cwd=slot, pm_home=pm_home,
    )

    assert result == pd.TicketHarvestResult(True, True)
    assert "healthy harvest" in second_source.read_text(encoding="utf-8")


def test_prepare_requires_legacy_backfill_then_succeeds(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1030", [("developer", "legacy\n")])
    legacy = _without_ticket_seals(pd, source.read_text(encoding="utf-8"))
    source.write_text(legacy, encoding="utf-8", newline="\n")

    with pytest.raises(
        pd.DelegateError,
        match=r"ticket prepare.*seal-backfill --ticket T-1030",
    ) as caught:
        pd.prepare_ticket_copy(
            ticket="T-1030", role="developer", cwd=slot, pm_home=pm_home,
        )
    assert "role=developer ordinal=0 비었는지=아니오" in str(caught.value)

    backfilled, changed = pd.backfill_ticket_seals(legacy)
    assert changed == [("developer", 0)]
    source.write_text(backfilled, encoding="utf-8", newline="\n")
    plan = pd.prepare_ticket_copy(
        ticket="T-1030", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert plan.path.exists()


@pytest.mark.parametrize(
    ("content", "empty_label", "specific_guidance"),
    [
        ("", "예", "빈 미봉인 절"),
        ("architect 산출\n", "아니오", "기계 복구 경로가 없습니다"),
    ],
)
def test_prepare_mixed_seals_prescribes_recreate_or_role_rerecord(
        growth_env, pd, content, empty_label, specific_guidance):
    pm_home, slot, tickets = growth_env
    ticket = "T-1034"
    source = _write_ticket(
        tickets, ticket, [("developer", "sealed\n"), ("architect", content)],
    )
    mixed = source.read_text(encoding="utf-8")
    missing_seal = pd.parse_ticket_seals(mixed)[("architect", 0)]
    mixed = mixed[:missing_seal.line_start] + mixed[missing_seal.line_end:]
    source.write_text(mixed, encoding="utf-8", newline="\n")

    guidance = pd.ticket_growth_seal_recovery_guidance(mixed, ticket)
    assert guidance is not None
    assert "seal-backfill" not in guidance
    assert f"role=architect ordinal=0 비었는지={empty_label}" in guidance
    assert specific_guidance in guidance
    assert (
        "python3 .project_manager/tools/board.py section-add "
        "T-1034 --role architect"
    ) in guidance
    if content:
        assert "사본을 재prepare해 역할이 재기록·harvest" in guidance
    else:
        assert "재prepare" not in guidance

    with pytest.raises(pd.DelegateError) as caught:
        pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )
    assert guidance in str(caught.value)
    assert "seal-backfill" not in str(caught.value)
    assert not (slot / pd.TICKET_COPY_REL_ROOT / ticket).exists()


def test_draft_architect_requires_backfill_then_prepare_succeeds(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, _tickets = growth_env
    drafts = pm_home / ".project_manager" / "wiki" / "tickets" / ".drafts"
    drafts.mkdir()
    source = _write_ticket(drafts, "T-1032", [("architect", "legacy draft\n")])
    legacy = _without_ticket_seals(pd, source.read_text(encoding="utf-8"))
    source.write_text(legacy, encoding="utf-8", newline="\n")

    with pytest.raises(
        pd.DelegateError,
        match=r"ticket prepare.*seal-backfill --ticket T-1032",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1032", role="architect", cwd=slot, pm_home=pm_home,
        )

    monkeypatch.setattr(
        pd, "_load_board",
        lambda: type("IdBoard", (), {"_is_valid_ticket_id": staticmethod(lambda _tid: True)})(),
    )
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: pm_home)
    log = pm_home / ".project_manager" / "wiki" / "log" / "current.md"
    log.parent.mkdir(parents=True)
    log.write_text("", encoding="utf-8", newline="\n")
    assert pd._cmd_ticket(["seal-backfill", "--ticket", "T-1032"]) == 0
    assert "seal-backfill T-1032" in capsys.readouterr().out
    assert pd.verify_ticket_seals(source.read_text(encoding="utf-8")) == []

    plan = pd.prepare_ticket_copy(
        ticket="T-1032", role="architect", cwd=slot, pm_home=pm_home,
    )
    assert plan.path.exists()


def test_prepare_rejects_target_role_sha_mismatch_before_copy(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1033", [("developer", "sealed body\n")])
    tampered = source.read_text(encoding="utf-8").replace(
        "sealed body\n", "hand-edited body\n", 1,
    )
    source.write_text(tampered, encoding="utf-8", newline="\n")
    assert any("sha256 불일치" in problem for problem in pd.verify_ticket_seals(tampered))

    with pytest.raises(
        pd.DelegateError,
        match=r"ticket prepare.*sha256.*불일치",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1033", role="developer", cwd=slot, pm_home=pm_home,
        )
    assert not (slot / pd.TICKET_COPY_REL_ROOT / "T-1033").exists()


def test_harvest_requires_legacy_backfill_then_succeeds(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1031", [("developer", "legacy\n")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1031", role="developer", cwd=slot, pm_home=pm_home,
    )
    legacy = _without_ticket_seals(pd, source.read_text(encoding="utf-8"))
    source.write_text(legacy, encoding="utf-8", newline="\n")

    with pytest.raises(
        pd.DelegateError,
        match=r"ticket harvest.*seal-backfill --ticket T-1031",
    ):
        pd.harvest_ticket_copy(
            copy_path=plan.path, cwd=slot, pm_home=pm_home,
            capability=plan.capability,
        )
    assert source.read_text(encoding="utf-8") == legacy

    backfilled, changed = pd.backfill_ticket_seals(legacy)
    assert changed == [("developer", 0)]
    source.write_text(backfilled, encoding="utf-8", newline="\n")
    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert result == pd.TicketHarvestResult(True, True)
    assert pd.verify_ticket_seals(source.read_text(encoding="utf-8")) == []


def test_draft_architect_prepare_harvest_round_trip_never_syncs(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, _tickets = growth_env
    drafts = pm_home / ".project_manager" / "wiki" / "tickets" / ".drafts"
    drafts.mkdir()
    source = _write_ticket(drafts, "T-1020", [("architect", "")])
    board = pd._load_board_for_repo(pm_home)
    sync_calls = []
    board._growth_mutation_sync = lambda *_args: sync_calls.append(_args) or True
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)

    plan = pd.prepare_ticket_copy(
        ticket="T-1020", role="architect", cwd=slot, pm_home=pm_home,
    )
    metadata = json.loads(plan.metadata_path.read_text(encoding="utf-8"))
    assert "/tickets/.drafts/" in ("/" + metadata["source_relpath"])
    _replace_content(pd, plan.path, "architect", 0, "draft 설계 사실\n")
    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert result == pd.TicketHarvestResult(True, True)
    assert sync_calls == [] and "draft 설계 사실" in source.read_text(encoding="utf-8")
    assert "board-git 동기화 0회" in capsys.readouterr().err


@pytest.mark.parametrize("role", ["developer", "code-reviewer"])
def test_draft_prepare_rejects_non_architect_before_trust_state(
        growth_env, pd, role):
    pm_home, slot, _tickets = growth_env
    drafts = pm_home / ".project_manager" / "wiki" / "tickets" / ".drafts"
    drafts.mkdir()
    _write_ticket(drafts, "T-1021", [(role, "")])
    trust = pm_home / pd.TICKET_COPY_TRUST_REL_ROOT

    with pytest.raises(pd.DelegateError, match="draft×architect"):
        pd.prepare_ticket_copy(
            ticket="T-1021", role=role, cwd=slot, pm_home=pm_home,
        )
    assert not trust.exists() or not any(trust.iterdir())


def test_draft_harvest_refuses_intermediate_promote_path_drift(growth_env, pd):
    pm_home, slot, tickets = growth_env
    drafts = pm_home / ".project_manager" / "wiki" / "tickets" / ".drafts"
    drafts.mkdir()
    source = _write_ticket(drafts, "T-1022", [("architect", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1022", role="architect", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "architect", 0, "late draft 설계\n")
    promoted = tickets / source.name
    source.rename(promoted)

    with pytest.raises(pd.DelegateError, match="경로 drift"):
        pd.harvest_ticket_copy(
            copy_path=plan.path, cwd=slot, pm_home=pm_home,
            capability=plan.capability,
        )
    assert "late draft 설계" not in promoted.read_text(encoding="utf-8")


def test_same_role_recall_targets_latest_and_preserves_pm_home_drift(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(
        tickets, "T-1002", [("developer", "old round\n"), ("developer", "")],
    )
    plan = pd.prepare_ticket_copy(
        ticket="T-1002", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 1, "new round facts\n")
    # 준비 뒤 다른 역할 절 append/drift는 그대로 보존되어야 한다.
    current = source.read_text(encoding="utf-8")
    source.write_text(current + "\nPM parallel note outside prepared section\n", encoding="utf-8", newline="\n")
    assert pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability).changed
    final = source.read_text(encoding="utf-8")
    assert "old round" in final and "new round facts" in final
    assert "PM parallel note outside prepared section" in final


def test_ticket_seal_hash_normalizes_lf_crlf_and_lone_cr(pd):
    """(a) 같은 논리 내용의 세 개행 표기는 같은 seal hash다."""
    logical = "## 구현 (developer · 2026-08-17)\n\n첫 줄\n둘째 줄\n"
    digests = {
        pd.seal_for(logical.replace("\n", newline).encode("utf-8"))
        for newline in ("\n", "\r\n", "\r")
    }
    assert digests == {hashlib.sha256(logical.encode("utf-8")).hexdigest()}


def test_lf_seal_verifies_unchanged_crlf_checkout_and_detects_edit(
        tmp_path, pd):
    """LF로 발급한 실형상 봉인은 CRLF checkout에서 무마이그레이션 통과하되 편집은 red다."""
    lf = _ticket_text("T-1034", [("developer", "sealed body\n")])
    ticket = tmp_path / "T-1034-growth.md"
    ticket.write_bytes(lf.replace("\n", "\r\n").encode("utf-8"))
    crlf = ticket.read_bytes().decode("utf-8")
    assert pd.verify_ticket_seals(crlf) == []

    ticket.write_bytes(crlf.replace("sealed body", "sealed Body", 1).encode("utf-8"))
    assert any(
        "sha256 불일치" in problem
        for problem in pd.verify_ticket_seals(ticket.read_bytes().decode("utf-8"))
    )


def test_mixed_newlines_per_growth_section_verify_stably(tmp_path, pd):
    """(d) 역할 절별 LF·CRLF·lone CR 혼합 표기도 LF 봉인 그대로 안정 판정한다."""
    mixed = _ticket_text("T-1035", [
        ("architect", "LF design\n"),
        ("developer", "CRLF implementation\n"),
        ("code-reviewer", "CR review\n"),
    ])
    for role, newline in (("developer", "\r\n"), ("code-reviewer", "\r")):
        section = pd._ticket_role_section(mixed, role)
        seal = pd.parse_ticket_seals(mixed)[(role, 0)]
        block = mixed[section.marker_start:seal.line_end].replace("\n", newline)
        mixed = mixed[:section.marker_start] + block + mixed[seal.line_end:]
    ticket = tmp_path / "T-1035-growth.md"
    ticket.write_bytes(mixed.encode("utf-8"))
    assert pd.verify_ticket_seals(ticket.read_bytes().decode("utf-8")) == []


def test_crlf_seal_backfill_writes_canonical_hash_without_rewriting_bytes(
        tmp_path, pd):
    """(b) seal-backfill은 CRLF 본문을 보존하면서 LF와 같은 봉인을 쓴다."""
    lf = _ticket_text("T-1036", [("developer", "backfill facts\n")])
    unsealed = _without_ticket_seals(pd, lf)
    ticket = tmp_path / "T-1036-growth.md"
    ticket.write_bytes(unsealed.replace("\n", "\r\n").encode("utf-8"))
    original = ticket.read_bytes().decode("utf-8")
    updated, changed = pd.backfill_ticket_seals(original, ticket="T-1036")
    ticket.write_bytes(updated.encode("utf-8"))

    assert changed == [("developer", 0)]
    assert b"\n" not in ticket.read_bytes().replace(b"\r\n", b"")
    assert pd.verify_ticket_seals(ticket.read_bytes().decode("utf-8")) == []
    assert pd.parse_ticket_seals(updated)[("developer", 0)].sha256 == (
        pd.parse_ticket_seals(lf)[("developer", 0)].sha256
    )


def test_ticket_seal_hash_writers_and_verifier_have_static_seam_guard():
    """writer 전주체·verify가 canonical seam을 우회하면 AST 가드가 red가 된다."""
    delegate_source = PM_DELEGATE.read_text(encoding="utf-8")
    board_source = (TOOLS / "board.py").read_text(encoding="utf-8")
    assert _missing_ticket_seal_seam_edges(delegate_source, board_source) == set()

    bypassed = delegate_source.replace(
        "expected = seal_for(content)",
        "expected = hashlib.sha256(content).hexdigest()",
        1,
    )
    assert "pm_delegate._verify_ticket_seal_problems->seal_for" in (
        _missing_ticket_seal_seam_edges(bypassed, board_source)
    )
    # 장부 레코드 sha 가 seam 을 우회하면 봉인과 장부가 서로 다른 값을 들 수 있다.
    ledger_bypassed = delegate_source.replace(
        "digest = seal_for(text[section.content_start:section.content_end])",
        "digest = hashlib.sha256(b\"\").hexdigest()",
    )
    assert {
        "pm_delegate.append_ticket_growth_records->seal_for",
        "pm_delegate.ticket_growth_misplaced_seal_keys->seal_for",
    } <= _missing_ticket_seal_seam_edges(ledger_bypassed, board_source)


def test_crlf_prepare_edit_harvest_and_idempotent_preserve_newlines(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = tickets / "T-1019-growth.md"
    lf = _ticket_text("T-1019", [("developer", "")])
    # LF에서 발급된 기존 seal을 그대로 둔 채 Git-for-Windows checkout 형상만 CRLF로 바꾼다.
    original = lf.replace("\n", "\r\n")
    source.write_bytes(original.encode("utf-8"))
    _seed_growth_ledger("T-1019", source)
    plan = pd.prepare_ticket_copy(
        ticket="T-1019", role="developer", cwd=slot, pm_home=pm_home,
    )
    copy_text = plan.path.read_bytes().decode("utf-8")
    section = pd._ticket_role_section(copy_text, "developer")
    edited = (
        copy_text[:section.content_start]
        + "CRLF facts\r\n"
        + copy_text[section.content_end:]
    )
    plan.path.write_bytes(edited.encode("utf-8"))
    first = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert first == pd.TicketHarvestResult(True, True)
    after = source.read_bytes()
    assert b"CRLF facts\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b"")
    second = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert second == pd.TicketHarvestResult(False, True)
    assert source.read_bytes() == after


def test_crlf_ticket_survives_engine_rewrite_between_prepare_and_harvest(growth_env, pd):
    """CRLF 티켓의 prepare → **엔진 재작성** → harvest 가 stale 오판 없이 통과한다 (T-0709).

    harvest 는 준비 시점 baseline 과 현재 절을 **bytes 로** 대조해 "준비 뒤 외부 편집"을 판정한다.
    그 사이 board lifecycle writer(`dump_ticket*`)나 성장 절 append 가 CRLF 티켓을 LF 로 되쓰면
    내용이 한 글자도 안 바뀌었는데 stale 로 읽혀 회수가 거부된다(Windows 실측 클래스). 재작성
    쪽이 표기를 보존해야 하고, 덧붙이는 절도 같은 표기로 렌더돼 혼재가 없어야 한다."""
    pm_home, slot, tickets = growth_env
    source = tickets / "T-1021-growth.md"
    source.write_bytes(
        _ticket_text("T-1021", [("developer", "")]).replace("\n", "\r\n").encode("utf-8"))
    _seed_growth_ledger("T-1021", source)
    plan = pd.prepare_ticket_copy(
        ticket="T-1021", role="developer", cwd=slot, pm_home=pm_home)

    # PM 홈에서 엔진이 같은 티켓을 재작성한다 — frontmatter 갱신(dump_ticket_atomic)과
    #   다른 역할의 성장 절 append(_atomic_write_text) 두 경로 모두.
    board = pd._load_board_for_repo(pm_home)
    assert board.cmd_tier(argparse.Namespace(id="T-1021", tier="normal")) == 0
    assert board.cmd_section_add(
        argparse.Namespace(id="T-1021", role="code-reviewer", label=None)) == 0
    rewritten = source.read_bytes()
    assert b"tier: normal" in rewritten, "엔진 재작성이 실제로 없었다(공허 게이트)"
    assert b"role=code-reviewer" in rewritten, "성장 절 append 가 없었다(공허 게이트)"
    assert b"\n" not in rewritten.replace(b"\r\n", b""), (
        "엔진 재작성이 CRLF 티켓을 LF 로 뒤집거나 표기를 혼재시켰다")

    copy_text = plan.path.read_bytes().decode("utf-8")
    section = pd._ticket_role_section(copy_text, "developer")
    plan.path.write_bytes((
        copy_text[:section.content_start]
        + "CRLF facts\r\n"
        + copy_text[section.content_end:]
    ).encode("utf-8"))
    assert pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability,
    ) == pd.TicketHarvestResult(True, True)
    after = source.read_bytes()
    assert b"CRLF facts\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b"")
    assert pd.verify_ticket_seals(after.decode("utf-8")) == []


def test_stale_same_section_refuses_without_overwrite(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1003", [("developer", "")])
    plan = pd.prepare_ticket_copy(ticket="T-1003", role="developer", cwd=slot, pm_home=pm_home)
    _replace_content(pd, plan.path, "developer", 0, "agent content\n")
    _replace_content(pd, source, "developer", 0, "parallel PM content\n")
    current = source.read_text(encoding="utf-8")
    source.write_text(
        pd._upsert_ticket_seal(current, "developer", 0, by="harvest"),
        encoding="utf-8",
        newline="\n",
    )
    # 실제 별도 변경은 엔진 write 라 장부 레코드도 함께 남는다 — 장부 축은 정합이고 거부는
    # stale overwrite 축이 낸다.
    _seed_growth_ledger("T-1003", source)
    with pytest.raises(pd.DelegateError, match="stale overwrite") as caught:
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert f"ticket prepare --transfer-from {plan.path}" in str(caught.value)
    assert "parallel PM content" in source.read_text(encoding="utf-8")
    assert plan.path.exists() and plan.baseline_path.exists()


def test_newer_same_role_section_refuses_old_round_harvest(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1006", [("developer", "")])
    plan = pd.prepare_ticket_copy(ticket="T-1006", role="developer", cwd=slot, pm_home=pm_home)
    _replace_content(pd, plan.path, "developer", 0, "late old agent\n")
    new_content = "## 재구현 (developer · 2026-08-13)\n\n"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n<!-- pm-ticket-section:start role=developer -->\n"
        + new_content
        + "<!-- pm-ticket-section:end role=developer -->\n"
        + pd._ticket_seal_line(
            "developer", 1, new_content.encode("utf-8"), by="section-add",
        ),
        encoding="utf-8",
        newline="\n",
    )
    assert pd.verify_ticket_seals(source.read_text(encoding="utf-8")) == []
    with pytest.raises(pd.DelegateError, match="새 성장 절"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert "late old agent" not in source.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mutator,pattern",
    [
        (lambda text: text.replace("## 목표", "## changed outside"), "절 밖 bytes"),
        (lambda text: text.replace("<!-- pm-ticket-section:end role=developer -->", ""), "end 누락"),
        (lambda text: text.replace("<!-- pm-ticket-section:end role=developer -->", "<!-- pm-ticket-section:end role=architect -->"), "역할 불일치"),
        (lambda text: text.replace("<!-- pm-ticket-section:start role=developer -->", "<!-- pm-ticket-section:start role=developer -->\n<!-- pm-ticket-section:start role=developer -->"), "중첩"),
    ],
)
def test_tamper_and_marker_damage_fail_loud_preserving_files(growth_env, pd, mutator, pattern):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1004", [("developer", "")])
    plan = pd.prepare_ticket_copy(ticket="T-1004", role="developer", cwd=slot, pm_home=pm_home)
    plan.path.write_text(mutator(plan.path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
    before = source.read_bytes()
    with pytest.raises(pd.DelegateError, match=pattern):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert source.read_bytes() == before
    assert plan.path.exists() and plan.baseline_path.exists()


def test_forged_bundle_cannot_redirect_harvest_to_another_ticket(growth_env, pd):
    """동일 uid가 bundle mode를 풀어 baseline/metadata를 함께 위조해도 PM trust가 막는다."""
    pm_home, slot, tickets = growth_env
    first = _write_ticket(tickets, "T-1012", [("code-reviewer", "")])
    second = _write_ticket(tickets, "T-1013", [("code-reviewer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1012", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    second_bytes = second.read_bytes()
    forged_copy = second.read_text(encoding="utf-8")
    section = pd._ticket_role_section(forged_copy, "code-reviewer")
    forged_copy = (
        forged_copy[:section.content_start]
        + "위조된 cross-ticket review\n"
        + forged_copy[section.content_end:]
    )
    metadata = json.loads(plan.metadata_path.read_text(encoding="utf-8"))
    metadata["source_relpath"] = second.relative_to(pm_home).as_posix()
    metadata["baseline_sha256"] = hashlib.sha256(second_bytes).hexdigest()
    plan.baseline_path.chmod(0o600)
    plan.metadata_path.chmod(0o600)
    plan.baseline_path.write_bytes(second_bytes)
    plan.metadata_path.write_text(
        pd._ticket_copy_metadata_bytes(metadata).decode("utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    trust_dir = pm_home / pd.TICKET_COPY_TRUST_REL_ROOT / metadata["run_id"]
    for target, payload in (
        (trust_dir / pd.TICKET_COPY_BASELINE_NAME, second_bytes),
        (trust_dir / pd.TICKET_COPY_METADATA_NAME, pd._ticket_copy_metadata_bytes(metadata)),
    ):
        target.chmod(0o600)
        target.write_bytes(payload)
    plan.path.write_text(forged_copy, encoding="utf-8", newline="\n")
    first_before, second_before = first.read_bytes(), second.read_bytes()
    with pytest.raises(pd.DelegateError, match="capability MAC 검증 실패"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert first.read_bytes() == first_before
    assert second.read_bytes() == second_before


def test_harvest_requeries_plan_ticket_canonical_exactly_once(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1014", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1014", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 0, "agent facts\n")
    duplicate = source.with_name("T-1014-duplicate.md")
    duplicate.write_bytes(source.read_bytes())
    before = source.read_bytes()
    with pytest.raises(pd.DelegateError, match="canonical ticket 재조회.*found=2"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert source.read_bytes() == before


def test_capability_replay_and_path_substitution_fail(growth_env, pd):
    pm_home, slot, tickets = growth_env
    first = _write_ticket(tickets, "T-1015", [("developer", "")])
    second = _write_ticket(tickets, "T-1016", [("developer", "")])
    first_plan = pd.prepare_ticket_copy(
        ticket="T-1015", role="developer", cwd=slot, pm_home=pm_home,
    )
    second_plan = pd.prepare_ticket_copy(
        ticket="T-1016", role="developer", cwd=slot, pm_home=pm_home,
    )
    before = first.read_bytes(), second.read_bytes()
    with pytest.raises(pd.DelegateError, match="capability MAC 검증 실패"):
        pd.harvest_ticket_copy(
            copy_path=second_plan.path, cwd=slot, pm_home=pm_home,
            capability=first_plan.capability,
        )
    substituted = first_plan.path.with_name("ticket-T-1016.md")
    substituted.write_bytes(first_plan.path.read_bytes())
    with pytest.raises(pd.DelegateError, match="사본 경로 ticket/role/run 불일치"):
        pd.harvest_ticket_copy(
            copy_path=substituted, cwd=slot, pm_home=pm_home,
            capability=first_plan.capability,
        )
    assert (first.read_bytes(), second.read_bytes()) == before


def test_solo_same_uid_full_replica_forgery_fails_before_pm_drift_overwrite(
        pd, monkeypatch, tmp_path):
    solo = tmp_path / "solo"
    tools = solo / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for source_tool in TOOLS.glob("*.py"):
        shutil.copy2(source_tool, tools / source_tool.name)
    tickets = solo / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (solo / ".project_manager" / ".local").mkdir(parents=True)
    source = _write_ticket(tickets, "T-1018", [("code-reviewer", "")])
    assert _git(solo, "init", "-q").returncode == 0
    (solo / ".project_manager" / ".gitignore").write_text(".local/\n", encoding="utf-8", newline="\n")
    # T-0704 (F-001) — ignore 규칙 출처 검증이 tracked 여부까지 보므로 이 fixture 도 add 해야 한다.
    assert _git(solo, "add", ".project_manager/.gitignore").returncode == 0
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, solo))
    plan = pd.prepare_ticket_copy(
        ticket="T-1018", role="code-reviewer", cwd=solo, pm_home=solo,
    )
    _replace_content(pd, plan.path, "code-reviewer", 0, "forged agent review\n")
    _replace_content(pd, source, "code-reviewer", 0, "parallel PM review\n")
    metadata = json.loads(plan.metadata_path.read_text(encoding="utf-8"))
    trust_dir = solo / pd.TICKET_COPY_TRUST_REL_ROOT / metadata["run_id"]
    forged_meta = dict(metadata)
    forged_meta["ordinal"] = metadata["ordinal"] + 1
    forged_meta_bytes = pd._ticket_copy_metadata_bytes(forged_meta)
    for path, payload in (
        (plan.baseline_path, plan.path.read_bytes()),
        (plan.metadata_path, forged_meta_bytes),
        (plan.path.with_name(pd.TICKET_COPY_TAG_NAME), b"z" * 32),
        (trust_dir / pd.TICKET_COPY_BASELINE_NAME, plan.path.read_bytes()),
        (trust_dir / pd.TICKET_COPY_METADATA_NAME, forged_meta_bytes),
        (trust_dir / pd.TICKET_COPY_TAG_NAME, b"z" * 32),
    ):
        path.chmod(0o600)
        path.write_bytes(payload)
    before = source.read_bytes()
    with pytest.raises(pd.DelegateError, match="capability MAC 검증 실패"):
        pd.harvest_ticket_copy(
            copy_path=plan.path, cwd=solo, pm_home=solo,
            capability=plan.capability,
        )
    assert source.read_bytes() == before
    assert b"parallel PM review" in before


def test_idempotent_reharvest_retries_sync_after_atomic_write_crash(
        growth_env, pd, monkeypatch):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1017", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1017", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 0, "crash-recovery facts\n")
    board = pd._load_board_for_repo(pm_home)
    calls = []
    board._growth_mutation_sync_paths = lambda *_args: calls.append(True) or False
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    first = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    board._growth_mutation_sync_paths = lambda *_args: calls.append(True) or True
    second = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert first == pd.TicketHarvestResult(True, False)
    assert second == pd.TicketHarvestResult(False, True)
    assert len(calls) == 2 and "crash-recovery facts" in source.read_text(encoding="utf-8")


def test_harvest_pre_growth_helper_board_uses_legacy_sync_primitives(
        growth_env, pd, monkeypatch):
    """T-0675 PM-home 흡수 전에도 절 반영 뒤 AttributeError 부분 성공을 만들지 않는다."""
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1019", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1019", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 0, "transition-compatible facts\n")
    board = pd._load_board_for_repo(pm_home)
    delattr(board, "_growth_mutation_sync")
    delattr(board, "_growth_mutation_sync_paths")
    calls = []
    board.refresh_board = lambda: calls.append("refresh")
    board._board_git_sync_best_effort = (
        lambda message, paths: calls.append((message, tuple(paths))) or True
    )
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)

    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )

    assert result == pd.TicketHarvestResult(True, True)
    assert calls == [
        "refresh",
        ("ticket-harvest T-1019 developer", (source.resolve(),)),
    ]
    assert "transition-compatible facts" in source.read_text(encoding="utf-8")


def test_plain_marker_discussion_is_not_data(pd):
    text = "문서의 `pm-ticket-section:start/end role=<role>` 문법 설명\n"
    assert pd._ticket_growth_sections(text) == []


def test_prepare_uses_filename_identity_when_frontmatter_is_corrupt(growth_env, pd):
    """기존 scope fail-soft처럼 손상 frontmatter도 filename+실 marker 사본은 준비한다."""
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1011", [("developer", "")])
    source.write_text(
        source.read_text(encoding="utf-8").replace("id: T-1011", "id: T-CORRUPTED"),
        encoding="utf-8",
        newline="\n",
    )
    plan = pd.prepare_ticket_copy(
        ticket="T-1011", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert plan.path.read_bytes() == source.read_bytes()


def test_numeric_leading_legacy_slug_prepare_and_harvest(growth_env, pd):
    """`T-0683-3...`의 `3`은 prefix ID 일부가 아니라 legacy slug 첫 문자다."""
    pm_home, slot, tickets = growth_env
    source = tickets / "T-0683-3하네스-ticket-growth.md"
    source.write_text(_ticket_text("T-0683", [("developer", "")]), encoding="utf-8", newline="\n")
    _seed_growth_ledger("T-0683", source)

    plan = pd.prepare_ticket_copy(
        ticket="T-0683", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 0, "numeric slug facts\n")
    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )

    assert result == pd.TicketHarvestResult(True, True)
    assert "numeric slug facts" in source.read_text(encoding="utf-8")


def test_prepare_lookup_and_read_share_board_lock(pd, monkeypatch, tmp_path):
    pm_home = tmp_path / "pm"
    slot = tmp_path / "slot"
    source = pm_home / ".project_manager" / "wiki" / "tickets" / "claimed" / "T-1010-x.md"
    source.parent.mkdir(parents=True)
    source.write_text(_ticket_text("T-1010", [("developer", "")]), encoding="utf-8", newline="\n")
    _seed_growth_ledger("T-1010", source)
    slot.mkdir()
    assert _git(slot, "init", "-q").returncode == 0
    slot_ignore = slot / ".project_manager" / ".gitignore"
    slot_ignore.parent.mkdir()
    slot_ignore.write_text(".local/\n", encoding="utf-8", newline="\n")
    # T-0704 (F-001) — ignore 규칙 출처 검증이 tracked 여부까지 보므로 add 해야 통과한다.
    assert _git(slot, "add", ".project_manager/.gitignore").returncode == 0
    state = {"locked": False, "lookup_locked": False, "read_locked": False}

    class FakeBoard:
        @contextlib.contextmanager
        def board_lock(self):
            assert not state["locked"]
            state["locked"] = True
            try:
                yield
            finally:
                state["locked"] = False

        def tickets_dir(self):
            state["lookup_locked"] = state["locked"]
            return source.parents[1]

        @staticmethod
        def _ticket_id_from_filename(_name):
            return "T-1010"

    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: FakeBoard())
    # 티켓 본문 판독은 공유 읽기 seam 을 지난다([[T-0729]]) — 추적도 그 자리에 건다.
    seam = pd._load_file_lock()
    original_read_bytes = seam.read_bytes_shared

    def tracked_read_bytes(path):
        if Path(path) == source:
            state["read_locked"] = state["locked"]
        return original_read_bytes(path)

    monkeypatch.setattr(seam, "read_bytes_shared", tracked_read_bytes)
    plan = pd.prepare_ticket_copy(
        ticket="T-1010", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert state == {"locked": False, "lookup_locked": True, "read_locked": True}
    assert plan.path.exists()


def test_reviewer_codex_keeps_worktree_read_only_and_grants_only_copy_dir(
        pd, monkeypatch, tmp_path):
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    copy = cwd / pd.TICKET_COPY_REL_ROOT / "T-2001" / "code-reviewer" / ("a" * 32) / "ticket-T-2001.md"
    temp_root.mkdir()
    copy.parent.mkdir(parents=True)
    copy.write_text("copy", encoding="utf-8", newline="\n")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "code-reviewer", cwd, "review",
        ticket_copy_path=copy,
    )
    try:
        assert argv[argv.index("-s") + 1] == "workspace-write"
        assert argv[argv.index("--add-dir") + 1] == str(copy.parent)
        assert not any(
            token == "-c" and argv[index + 1].startswith("permissions.")
            for index, token in enumerate(argv[:-1])
        )
        assert argv[argv.index("-C") + 1] == str(read_tmp.path)
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


def test_acl_platform_reviewer_codex_grants_copy_dir_without_fd_binding(
        pd, monkeypatch, tmp_path):
    """fd 결속 primitive 가 없는 ACL 플랫폼(Windows)도 같은 권한 형상을 낸다.

    5차 Windows 측정의 `assert 'read-only' == 'workspace-write'` 는 이 수단 부재가 원인이었다 —
    수단이 없다고 권한을 낮추지 않는다. 플랫폼 축을 여기서 주입해 Linux 에서 태운다."""
    monkeypatch.setattr(pd, "_READ_TMP_FD_SUPPORTED", False)
    monkeypatch.setattr(pd, "_read_tmp_owner_acl_platform", lambda: True)
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    copy = cwd / pd.TICKET_COPY_REL_ROOT / "T-2003" / "code-reviewer" / ("e" * 32) / "ticket-T-2003.md"
    temp_root.mkdir()
    copy.parent.mkdir(parents=True)
    copy.write_text("copy", encoding="utf-8", newline="\n")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "code-reviewer", cwd, "review",
        ticket_copy_path=copy,
    )
    try:
        assert read_tmp is not None and read_tmp.parent_fd is None
        assert argv[argv.index("-s") + 1] == "workspace-write"
        assert argv[argv.index("--add-dir") + 1] == str(copy.parent)
        assert argv[argv.index("-C") + 1] == str(read_tmp.path)
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)
    assert not read_tmp.path.exists()
    assert list(temp_root.iterdir()) == []


@pytest.mark.parametrize("harness,model", [("claude", "sonnet"), ("opencode", "prov/m")])
def test_reviewer_non_codex_ticket_copy_warns_and_keeps_selected_target(
        pd, monkeypatch, tmp_path, capsys, harness, model):
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    copy = cwd / pd.TICKET_COPY_REL_ROOT / "T-2002" / "code-reviewer" / ("b" * 32) / "ticket-T-2002.md"
    temp_root.mkdir()
    copy.parent.mkdir(parents=True)
    copy.write_text("copy", encoding="utf-8", newline="\n")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
            harness, model, None, "code-reviewer", cwd, "review",
            ticket_copy_path=copy,
    )
    try:
        assert argv[0] == harness
        if harness == "opencode":
            assert argv[argv.index("--agent") + 1] == "code-reviewer"
        warning = capsys.readouterr().err
        assert "단일-path write 격리" in warning
        assert f"선택한 {harness} target으로 계속 실행" in warning
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


@pytest.mark.parametrize(
    ("harness", "model", "stdout"),
    [
        ("claude", "sonnet", json.dumps({"type": "result", "result": "reviewed", "session_id": "s1"})),
        ("opencode", "prov/m", json.dumps({
            "type": "text", "sessionID": "ses_1",
            "part": {"type": "text", "text": "reviewed"},
        })),
    ],
)
def test_non_codex_reviewer_ticket_copy_warns_then_spawns_runner_once(
        pd, monkeypatch, tmp_path, capsys, harness, model, stdout):
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    copy = cwd / pd.TICKET_COPY_REL_ROOT / "T-2020" / "code-reviewer" / ("c" * 32) / "ticket-T-2020.md"
    temp_root.mkdir()
    copy.parent.mkdir(parents=True)
    copy.write_text("copy", encoding="utf-8", newline="\n")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    spawned = []

    def runner(*_args, **_kwargs):
        spawned.append(_args[0])
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "", "timed_out": False,
        }

    pd._execute_attempt(
        harness=harness, model=model, reasoning=None,
        role="code-reviewer", cwd=cwd, prompt="review", timeout=30,
        output_dir=tmp_path / "raw", run_fn=runner, attempt="primary",
        ticket_copy_path=copy,
    )
    assert [argv[0] for argv in spawned] == [harness]
    if harness == "opencode":
        assert spawned[0][spawned[0].index("--agent") + 1] == "code-reviewer"
    assert f"선택한 {harness} target으로 계속 실행" in capsys.readouterr().err


def test_reviewer_missing_read_temp_warns_and_continues(pd, monkeypatch, tmp_path, capsys):
    cwd = tmp_path / "worktree"
    copy = cwd / pd.TICKET_COPY_REL_ROOT / "T-2021" / "code-reviewer" / ("d" * 32) / "ticket-T-2021.md"
    copy.parent.mkdir(parents=True)
    copy.write_text("copy", encoding="utf-8", newline="\n")
    monkeypatch.setattr(pd, "_create_read_role_temp", lambda *_args: None)

    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "claude", "sonnet", None, "code-reviewer", cwd, "review",
        ticket_copy_path=copy,
    )

    assert argv[0] == "claude" and read_tmp is None
    assert "격리 temp를 준비하지 못해" in capsys.readouterr().err


def test_resume_fallback_and_fresh_prompts_share_same_copy_preamble(pd, tmp_path):
    copy = tmp_path / "ticket-T-3001.md"
    plan = pd.TicketCopyPlan(
        copy, tmp_path / "baseline.md", tmp_path / "metadata.json", tmp_path,
        tmp_path, "T-3001", "developer",
    )
    for payload in ("resume delta", "fallback full", "fresh retry full"):
        composed = pd._with_ticket_copy_preamble(payload, plan)
        assert str(copy) in composed
        assert composed.endswith(payload)
        assert composed.count(str(copy)) == 1
    existing = pd._ticket_copy_preamble(plan) + "\n\nfull"
    assert pd._with_ticket_copy_preamble(existing, plan) == existing


def test_symlink_copy_root_and_outside_copy_are_rejected(growth_env, pd, tmp_path):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1005", [("architect", "")])
    machine_root = slot / pd.TICKET_COPY_REL_ROOT
    machine_root.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    machine_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(pd.DelegateError, match=r"git check-ignore 실패\(rc="):
        pd.prepare_ticket_copy(ticket="T-1005", role="architect", cwd=slot, pm_home=pm_home)
    rogue = outside / "ticket-T-1005.md"
    rogue.write_text("x", encoding="utf-8", newline="\n")
    with pytest.raises(pd.DelegateError, match="containment"):
        pd.harvest_ticket_copy(copy_path=rogue, cwd=slot, pm_home=pm_home, capability=b"x" * 32)


def test_prepare_keeps_ticket_copy_root_out_of_git_status(growth_env, pd):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1023", [("developer", "")])

    plan = pd.prepare_ticket_copy(
        ticket="T-1023", role="developer", cwd=slot, pm_home=pm_home,
    )

    assert plan.path.exists()
    assert _git(slot, "status", "--porcelain").stdout == ""


def test_prepare_fails_loud_without_local_ignore_before_copy(growth_env, pd):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1024", [("developer", "")])
    # `.project_manager/.gitignore` 기준 상대 앵커. 옛 가짜 `probe` 좌표만 무시하고 실사본 경로는
    # 무시하지 않는 형상 — check-ignore 가 실사본 좌표를 보지 않으면 이 규칙에 속아 통과한다.
    (slot / ".project_manager" / ".gitignore").write_text(
        ".local/delegate-ticket-copies/probe\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _git(
        slot, "check-ignore", "-q", ".project_manager/.local/delegate-ticket-copies/probe",
    ).returncode == 0
    assert _git(
        slot, "check-ignore", "-q",
        ".project_manager/.local/delegate-ticket-copies/T-1024/developer/x/ticket-T-1024.md",
    ).returncode == 1

    with pytest.raises(
        pd.DelegateError,
        match=r"사본 루트가 git 무시 대상이 아님.*\.project_manager/\.gitignore.*\.local/.*복원",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1024", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert not (slot / pd.TICKET_COPY_REL_ROOT).exists()


def test_check_ignore_source_tracked_gitignore_passes(growth_env, pd):
    """T-0704 (a) — 무시 규칙 출처가 tracked `.project_manager/.gitignore` 면 통과한다."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1040", [("developer", "")])

    plan = pd.prepare_ticket_copy(
        ticket="T-1040", role="developer", cwd=slot, pm_home=pm_home,
    )

    assert plan.path.exists()


def test_check_ignore_source_local_only_exclude_fails_loud(growth_env, pd):
    """T-0704 (b) — `.git/info/exclude` 로만 무시되면 fail-loud, 진단에 실제 출처가 보인다."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1041", [("developer", "")])
    # tracked `.gitignore` 는 무관한 패턴만 남겨, 사본 경로를 숨기는 규칙이 로컬 전용
    # `.git/info/exclude` 유래가 되게 한다.
    (slot / ".project_manager" / ".gitignore").write_text(
        "unrelated-pattern-only\n", encoding="utf-8", newline="\n",
    )
    exclude = Path(
        _git(
            slot, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude",
        ).stdout.strip()
    )
    exclude.write_text(".local/\n", encoding="utf-8", newline="\n")
    assert _git(
        slot, "check-ignore", "-q",
        ".project_manager/.local/delegate-ticket-copies/T-1041/developer/x/ticket-T-1041.md",
    ).returncode == 0

    with pytest.raises(
        pd.DelegateError,
        match=r"로컬 전용 소스.*출처=\.git/info/exclude.*\.project_manager/\.gitignore.*복원",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1041", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert not (slot / pd.TICKET_COPY_REL_ROOT).exists()


def test_check_ignore_source_not_ignored_keeps_current_message(growth_env, pd):
    """T-0704 (c) — 사본 경로가 애초에 무시되지 않으면 기존 안내 메시지를 유지한다.

    F-004: `test_prepare_fails_loud_without_local_ignore_before_copy` 와 같은 rc==1 분기·같은
    정규식을 검증하는 중복 커버리지다 — probe 패턴 없는 최소형만 남긴다.
    """
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1042", [("developer", "")])
    (slot / ".project_manager" / ".gitignore").write_text(
        "", encoding="utf-8", newline="\n",
    )

    with pytest.raises(
        pd.DelegateError,
        match=r"사본 루트가 git 무시 대상이 아님.*\.project_manager/\.gitignore.*\.local/.*복원",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1042", role="developer", cwd=slot, pm_home=pm_home,
        )


def test_check_ignore_tool_failure_keeps_current_message(growth_env, pd, monkeypatch):
    """T-0704 (d) — `check-ignore` 자체가 비정상 종료하면 기존 rc>=2 fail-loud 를 유지한다."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1043", [("developer", "")])
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if "check-ignore" in argv:
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="fatal: 강제 실패")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(pd.subprocess, "run", fake_run)

    with pytest.raises(
        pd.DelegateError, match=r"git check-ignore 실패\(rc=2\): fatal: 강제 실패",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1043", role="developer", cwd=slot, pm_home=pm_home,
        )


def test_check_ignore_source_untracked_canonical_fails_loud(growth_env, pd):
    """T-0704 (e) — 정본 위치와 경로는 같지만 untracked 면 fail-loud(F-001)."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1044", [("developer", "")])
    # fresh import 직후 아직 git add 하지 않은 형상을 재현 — 작업 트리 파일은 그대로 두고
    # 인덱스에서만 뺀다(패턴 자체는 여전히 check-ignore 에 읽힌다).
    assert _git(slot, "rm", "--cached", "-q", ".project_manager/.gitignore").returncode == 0

    with pytest.raises(
        pd.DelegateError,
        match=r"untracked 상태.*\.project_manager/\.gitignore.*git add",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1044", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert not (slot / pd.TICKET_COPY_REL_ROOT).exists()


def test_check_ignore_multiple_lines_output_fails_loud(growth_env, pd, monkeypatch):
    """T-0704 (F-004) — check-ignore -v 가 예상 밖으로 여러 줄을 돌려주면 해석 실패로 fail-loud."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1045", [("developer", "")])
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if "check-ignore" in argv:
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=(
                    ".project_manager/.gitignore:1:.local/\tfirst\n"
                    ".project_manager/.gitignore:1:.local/\tsecond\n"
                ),
                stderr="",
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(pd.subprocess, "run", fake_run)

    with pytest.raises(pd.DelegateError, match=r"출력을 해석하지 못함"):
        pd.prepare_ticket_copy(
            ticket="T-1045", role="developer", cwd=slot, pm_home=pm_home,
        )


def test_check_ignore_empty_stdout_fails_loud(growth_env, pd, monkeypatch):
    """T-0704 (F-004) — check-ignore -v 가 rc=0 인데 stdout 이 비면 해석 실패로 fail-loud."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1046", [("developer", "")])
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if "check-ignore" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(pd.subprocess, "run", fake_run)

    with pytest.raises(pd.DelegateError, match=r"출력을 해석하지 못함"):
        pd.prepare_ticket_copy(
            ticket="T-1046", role="developer", cwd=slot, pm_home=pm_home,
        )


def test_parse_check_ignore_verbose_line_windows_drive_absolute_source(pd):
    """T-0704 (F-004) — Windows 드라이브 절대경로 출처의 콜론이 linenum 구분자로 오인되지 않는다."""
    parsed = pd._parse_check_ignore_verbose_line(
        "C:/Users/foo/.gitignore:12:.local/\t.project_manager/.local/x"
    )
    assert parsed == ("C:/Users/foo/.gitignore", "12", ".local/")


def test_parse_check_ignore_verbose_line_windows_extended_length_prefix(pd):
    """T-0704 (F-004) — 확장 길이 경로 접두(두 백슬래시·물음표·백슬래시)가 붙은 절대경로도 분해된다."""
    prefix = chr(92) + chr(92) + "?" + chr(92)
    line = prefix + "C:" + chr(92) + "Users" + chr(92) + "foo" + chr(92) + ".gitignore:7:.local/\t.project_manager/.local/x"
    parsed = pd._parse_check_ignore_verbose_line(line)
    assert parsed == (
        prefix + "C:" + chr(92) + "Users" + chr(92) + "foo" + chr(92) + ".gitignore", "7", ".local/",
    )


def test_parse_check_ignore_verbose_line_colon_in_source_path(pd):
    """T-0704 (F-004) — source 경로 자체에 콜론이 더 있어도 최우측 linenum 구분자를 고른다."""
    parsed = pd._parse_check_ignore_verbose_line(
        "/tmp/weird:dir/.gitignore:3:.local/\t.project_manager/.local/x"
    )
    assert parsed == ("/tmp/weird:dir/.gitignore", "3", ".local/")


def test_parse_check_ignore_verbose_line_without_tab_returns_none(pd):
    """T-0704 (F-004) — 탭이 없는 줄(예상 밖 형식)은 판정 불능으로 None 을 돌려준다."""
    assert pd._parse_check_ignore_verbose_line(".project_manager/.gitignore:1:.local/") is None


def test_parse_check_ignore_verbose_line_tab_inside_pattern(pd):
    """T-0704 (F-008) — 패턴 안에 탭이 들어 있어도 rpartition 이 pathname 만 정확히 떼어낸다."""
    parsed = pd._parse_check_ignore_verbose_line(
        ".project_manager/.gitignore:1:pat\tter\t.project_manager/.local/x"
    )
    assert parsed == (".project_manager/.gitignore", "1", "pat\tter")


def test_check_ignore_source_relative_outside_repo_excludesfile_treated_as_local(growth_env, pd):
    """T-0704 (F-006) — 저장소 밖 상대경로 core.excludesFile 출처를 ls-files 호출 없이 로컬 전용으로 판정한다."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1047", [("developer", "")])
    (slot / ".project_manager" / ".gitignore").write_text(
        "unrelated-pattern-only\n", encoding="utf-8", newline="\n",
    )
    outside_name = "outside_ignore"
    outside = slot.parent / outside_name
    outside.write_text(".local/\n", encoding="utf-8", newline="\n")
    assert _git(slot, "config", "core.excludesFile", f"../{outside_name}").returncode == 0
    assert _git(
        slot, "check-ignore", "-q",
        ".project_manager/.local/delegate-ticket-copies/T-1047/developer/x/ticket-T-1047.md",
    ).returncode == 0

    with pytest.raises(
        pd.DelegateError,
        match=rf"로컬 전용 소스.*출처=\.\./{outside_name}.*\.project_manager/\.gitignore.*복원",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1047", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert not (slot / pd.TICKET_COPY_REL_ROOT).exists()


def test_check_ignore_source_tracked_non_canonical_location_fails_loud(growth_env, pd):
    """T-0704 (F-007) — tracked 비정본 위치(루트 .gitignore) 유래는 그 문구로 고정된다."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1048", [("developer", "")])
    (slot / ".project_manager" / ".gitignore").write_text(
        "unrelated-pattern-only\n", encoding="utf-8", newline="\n",
    )
    (slot / ".gitignore").write_text(
        ".project_manager/.local/\n", encoding="utf-8", newline="\n",
    )
    assert _git(slot, "add", ".gitignore").returncode == 0

    with pytest.raises(
        pd.DelegateError,
        match=r"tracked 비정본 위치 유래.*출처=\.gitignore.*\.project_manager/\.gitignore.*복원",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1048", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert not (slot / pd.TICKET_COPY_REL_ROOT).exists()


def test_check_ignore_source_untracked_non_canonical_location_fails_loud(growth_env, pd):
    """T-0704 (F-007) — untracked 비정본 위치(루트 .gitignore) 유래는 로컬 전용 문구로 고정된다."""
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1049", [("developer", "")])
    (slot / ".project_manager" / ".gitignore").write_text(
        "unrelated-pattern-only\n", encoding="utf-8", newline="\n",
    )
    # git add 하지 않는다 — ls-files --error-unmatch rc=1(untracked) 경로를 비정본 축에서 고정한다.
    (slot / ".gitignore").write_text(
        ".project_manager/.local/\n", encoding="utf-8", newline="\n",
    )

    with pytest.raises(
        pd.DelegateError,
        match=r"로컬 전용 소스 유래.*출처=\.gitignore.*\.project_manager/\.gitignore.*복원",
    ):
        pd.prepare_ticket_copy(
            ticket="T-1049", role="developer", cwd=slot, pm_home=pm_home,
        )

    assert not (slot / pd.TICKET_COPY_REL_ROOT).exists()


def test_prepare_succeeds_with_empty_dir_fd_support(growth_env, pd, monkeypatch):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1025", [("developer", "")])
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())

    plan = pd.prepare_ticket_copy(
        ticket="T-1025", role="developer", cwd=slot, pm_home=pm_home,
    )

    assert plan.path.exists()


def test_prepare_leaves_git_info_exclude_byte_identical(growth_env, pd):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1026", [("developer", "")])
    exclude = Path(
        _git(
            slot, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude",
        ).stdout.strip()
    )
    exclude.write_bytes(b"existing-without-newline")
    before = exclude.read_bytes()

    pd.prepare_ticket_copy(
        ticket="T-1026", role="developer", cwd=slot, pm_home=pm_home,
    )

    assert exclude.read_bytes() == before


def test_linked_worktree_prepare_is_hidden_with_empty_dir_fd_support(
        growth_env, pd, monkeypatch, tmp_path):
    pm_home, primary, tickets = growth_env
    linked = tmp_path / "linked"
    _write_ticket(tickets, "T-1027", [("developer", "")])
    assert _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked)).returncode == 0
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())

    plan = pd.prepare_ticket_copy(
        ticket="T-1027", role="developer", cwd=linked, pm_home=pm_home,
    )

    assert plan.path.exists()
    assert _git(linked, "status", "--porcelain").stdout == ""


def test_ticket_cli_parser_help_and_dispatch(pd, monkeypatch, capsys, tmp_path):
    slot = tmp_path / "slot"
    slot.mkdir()
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: tmp_path / "pm")
    fake = pd.TicketCopyPlan(
        slot / "ticket-T-2000.md", slot / "baseline.md", slot / "metadata.json",
        slot, tmp_path / "pm", "T-2000", "developer", b"c" * 32,
    )
    monkeypatch.setattr(pd, "prepare_ticket_copy", lambda **_kwargs: fake)
    assert pd.main(["ticket", "prepare", "--ticket", "T-2000", "--role", "developer", "--cwd", str(slot)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared == {"copy": str(fake.path), "capability": fake.capability.hex()}
    received = []
    monkeypatch.setattr(
        pd, "harvest_ticket_copy",
        lambda **kwargs: received.append(kwargs) or pd.TicketHarvestResult(False, True),
    )
    monkeypatch.setattr(pd.sys, "stdin", io.StringIO(fake.capability.hex() + "\n"))
    assert pd.main([
        "ticket", "harvest", "--copy", str(fake.path), "--cwd", str(slot),
        "--capability-stdin",
    ]) == 0
    harvested = json.loads(capsys.readouterr().out)
    assert harvested["changed"] is False and harvested["sync_ready"] is True
    assert received[0]["capability"] == fake.capability
    assert fake.capability.hex() not in json.dumps(harvested)
    monkeypatch.setattr(
        pd, "harvest_ticket_copy",
        lambda **_kwargs: (_ for _ in ()).throw(pd.DelegateError("sealed damage")),
    )
    monkeypatch.setattr(pd.sys, "stdin", io.StringIO(fake.capability.hex() + "\n"))
    assert pd.main([
        "ticket", "harvest", "--copy", str(fake.path), "--cwd", str(slot),
        "--capability-stdin",
    ]) == 1
    assert fake.capability.hex() not in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        pd.main(["ticket", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "prepare" in help_text and "harvest" in help_text
    assert "capability" not in help_text.lower() or "stdin" in help_text.lower()


def test_ticket_cli_subdirectory_cwd_prepare_and_harvest_use_repo_root(
        growth_env, pd, monkeypatch, capsys):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-2001", [("developer", "before\n")])
    nested = slot / "src" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)

    assert pd.main([
        "ticket", "prepare", "--ticket", "T-2001", "--role", "developer",
        "--cwd", str(nested),
    ]) == 0
    prepared = json.loads(capsys.readouterr().out)
    copy = Path(prepared["copy"])
    capability = prepared["capability"]
    assert copy.is_relative_to(slot / pd.TICKET_COPY_REL_ROOT)
    assert not copy.is_relative_to(nested)
    copy.write_text(
        copy.read_text(encoding="utf-8").replace("before\n", "after\n"),
        encoding="utf-8",
        newline="\n",
    )

    monkeypatch.setattr(pd.sys, "stdin", io.StringIO(capability + "\n"))
    assert pd.main([
        "ticket", "harvest", "--copy", str(copy), "--cwd", str(nested),
        "--capability-stdin",
    ]) == 0
    harvested = json.loads(capsys.readouterr().out)
    assert harvested["changed"] is True
    assert "after\n" in source.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "runner_mode",
    [
        "ok", "nonzero", "exception", "ok-harvest-fail", "exception-harvest-fail",
        "ok-harvest-runtime", "exception-harvest-runtime",
        "ok-harvest-skew", "exception-harvest-skew",
    ],
)
def test_cross_main_always_harvests_and_preserves_copy(
        pd, monkeypatch, tmp_path, capsys, runner_mode):
    """subprocess rc/예외와 무관하게 cross 후처리가 같은 보존 사본을 harvest한다."""
    cwd = tmp_path / "slot"
    cwd.mkdir()
    prompt = cwd / "prompt.md"
    prompt.write_text("단일 티켓 작업", encoding="utf-8", newline="\n")
    copy = cwd / ".project_manager" / ".local" / "delegate-ticket-copies" / "T-3000" / "developer" / "run" / "ticket-T-3000.md"
    copy.parent.mkdir(parents=True)
    copy.write_text("preserved", encoding="utf-8", newline="\n")
    plan = pd.TicketCopyPlan(
        copy, copy.with_name("baseline.md"), copy.with_name("metadata.json"),
        cwd, cwd, "T-3000", "developer", b"k" * 32,
    )
    prepared = []
    harvested = []
    runner_error = RuntimeError("fake runner exploded")

    real_er = pd._load_external_review()
    monkeypatch.setattr(real_er, "repo_root_from_cwd", lambda _cwd: cwd)
    monkeypatch.setattr(real_er, "resolve_pm_home_for_repo", lambda *_a, **_k: cwd)
    monkeypatch.setattr(real_er, "_owns_real_board", lambda _pm: False)
    monkeypatch.setattr(pd, "_load_external_review", lambda: real_er)
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *_a, **_k: True)
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-x",
    })
    monkeypatch.setattr(pd, "cold_reinjection_record", lambda *_a, **_k: None)
    monkeypatch.setattr(pd, "check_local_conf_divergence", lambda *_a, **_k: (cwd, None, real_er))
    monkeypatch.setattr(pd, "begin_scope_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(pd, "report_scope_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(pd, "prepare_ticket_copy", lambda **kwargs: prepared.append(kwargs) or plan)
    def harvest(**kwargs):
        harvested.append(kwargs)
        if runner_mode.endswith("harvest-fail"):
            raise pd.DelegateError("simultaneous harvest damage")
        if runner_mode.endswith("harvest-runtime"):
            raise RuntimeError("runtime harvest damage")
        if runner_mode.endswith("harvest-skew"):
            skew = RuntimeError("marked harvest engine skew")
            skew._engine_rev_skew = True
            raise skew
        return pd.TicketHarvestResult(True, True)

    monkeypatch.setattr(pd, "harvest_ticket_copy", harvest)

    seen_prompt = []

    def runner(_argv, *, stdin_text, **_kwargs):
        seen_prompt.append(stdin_text)
        if runner_mode.startswith("exception"):
            raise runner_error
        return {
            "returncode": 0 if runner_mode == "ok" else 9,
            "stdout": (
                '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
                if runner_mode.startswith("ok") else ""
            ),
            "stderr": "", "timed_out": False,
        }

    argv = [
        "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd),
        "--ticket", "T-3000", "--output-dir", str(tmp_path / "raw"),
    ]
    if runner_mode.startswith("exception"):
        with pytest.raises(RuntimeError, match="fake runner exploded") as caught:
            pd.main(argv, run_fn=runner)
        assert caught.value is runner_error
    elif runner_mode == "ok-harvest-skew":
        with pytest.raises(RuntimeError, match="marked harvest engine skew") as caught:
            pd.main(argv, run_fn=runner)
        assert pd._is_engine_rev_skew(caught.value)
    else:
        expected = 0 if runner_mode == "ok" else 1
        assert pd.main(argv, run_fn=runner) == expected
    assert len(prepared) == 1 and len(harvested) == 1
    assert harvested[0]["copy_path"] == copy
    assert copy.read_text(encoding="utf-8") == "preserved"
    assert str(copy) in seen_prompt[0]
    assert "자기 역할 절만" in seen_prompt[0]
    captured = capsys.readouterr()
    token = plan.capability.hex()
    assert token not in captured.out and token not in captured.err
    assert all(token not in prompt_text for prompt_text in seen_prompt)
    assert all(token not in path.read_text(encoding="utf-8")
               for path in (tmp_path / "raw").glob("*.txt"))
    if "harvest-" in runner_mode and runner_mode != "ok-harvest-skew":
        err = captured.err
        expected_damage = (
            "marked harvest engine skew" if runner_mode.endswith("harvest-skew")
            else (
                "runtime harvest damage"
                if runner_mode.endswith("harvest-runtime")
                else "simultaneous harvest damage"
            )
        )
        assert expected_damage in err and "새 prepare/위임" in err
        if runner_mode.startswith("exception"):
            assert "RuntimeError: fake runner exploded" in err


def test_cross_prepare_failure_is_loud_before_primary_or_fallback_spawn(
        pd, monkeypatch, tmp_path, capsys):
    """사본 경계가 없으면 무사본 실행으로 degrade하지 않고 전송 0회로 닫는다."""
    cwd = tmp_path / "slot"
    cwd.mkdir()
    prompt = cwd / "prompt.md"
    prompt.write_text("단일 티켓 작업", encoding="utf-8", newline="\n")
    real_er = pd._load_external_review()
    monkeypatch.setattr(real_er, "repo_root_from_cwd", lambda _cwd: cwd)
    monkeypatch.setattr(real_er, "resolve_pm_home_for_repo", lambda *_a, **_k: cwd)
    monkeypatch.setattr(real_er, "_owns_real_board", lambda _pm: False)
    monkeypatch.setattr(pd, "_load_external_review", lambda: real_er)
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *_a, **_k: True)
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-x",
        "delegate.developer.fallback.harness": "claude",
        "delegate.developer.fallback.model": "sonnet",
    })
    monkeypatch.setattr(
        pd, "check_local_conf_divergence", lambda *_a, **_k: (cwd, None, real_er),
    )
    monkeypatch.setattr(
        pd, "prepare_ticket_copy",
        lambda **_kwargs: (_ for _ in ()).throw(
            pd.DelegateError("developer 성장 marker 없음")
        ),
    )
    spawned = []

    def runner(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("prepare 실패 뒤 외부 실행 금지")

    rc = pd.main([
        "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd),
        "--ticket", "T-3010", "--output-dir", str(tmp_path / "raw"),
    ], run_fn=runner)
    assert rc == 1 and spawned == []
    err = capsys.readouterr().err
    assert "위임 티켓 사본 준비 실패" in err and "성장 marker 없음" in err


def test_role_docs_and_delegate_card_pin_growth_contract():
    developer = (REPO / ".claude" / "agents" / "developer.md").read_text(encoding="utf-8")
    reviewer = (REPO / ".claude" / "agents" / "code-reviewer.md").read_text(encoding="utf-8")
    architect = (REPO / ".claude" / "agents" / "architect.md").read_text(encoding="utf-8")
    card = (REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md").read_text(encoding="utf-8")
    assert "구현 방식·변경 지점" in developer and "빈틈을 메운 판단" in developer
    assert "설계·구현 보충 포함" in reviewer and "must-fix/should-fix/suggestion" in reviewer
    assert "경계 실측·불변식·표면 상한·테스트 전략" in architect and "재설계 절" in architect
    assert "ticket prepare" in card and "ticket harvest" in card
    assert "finally" in card and "--dry-run`은 무부수효과" in card
    assert "단일 경로 쓰기 격리를 보장하지 못해도 경고 후" in card
    assert "pm-review-v1" in reviewer and "review delta --ticket" in developer
    assert "pm-review-disposition-v1" in card and "accepted ID" in card
    engine = _load_pd()
    rendered_help = engine.build_arg_parser().format_help()
    assert all(term in rendered_help for term in (
        "단일-path write 격리", "경고 후", "선택한 target", "계속 실행",
    ))
    for text in (developer, reviewer, architect):
        assert "자기평가" in text and "marker" in text and "PM 홈 티켓" in text


def test_three_harness_cards_and_roles_share_review_delta_contract():
    skills = [path.read_text(encoding="utf-8") for path in NATIVE_CARDS.values()]
    for text in skills:
        assert "pm-review-v1" in text
        assert "pm-review-disposition-v1" in text
        assert "review delta --ticket T-NNNN" in text
        assert "accepted ID" in text and "rejected/decision-required" in text

    reviewers = [
        REPO / ".claude" / "agents" / "code-reviewer.md",
        REPO / "templates" / "codex" / ".codex" / "agents" / "code-reviewer.toml",
        REPO / "templates" / "opencode" / ".opencode" / "agents" / "code-reviewer.md",
    ]
    developers = [
        REPO / ".claude" / "agents" / "developer.md",
        REPO / "templates" / "codex" / ".codex" / "agents" / "developer.toml",
        REPO / "templates" / "opencode" / ".opencode" / "agents" / "developer.md",
    ]
    for path in reviewers:
        text = path.read_text(encoding="utf-8")
        assert "pm-review-v1" in text and "section-add" in text and "시드한" in text
    for path in developers:
        text = path.read_text(encoding="utf-8")
        assert "accepted-only delta" in text and "review delta --ticket T-NNNN" in text

    ticket_cards = [
        REPO / ".claude" / "skills" / "pm-ticket" / "SKILL.md",
        REPO / "templates" / "codex" / ".agents" / "skills" / "pm-ticket" / "SKILL.md",
        REPO / "templates" / "opencode" / ".claude" / "skills" / "pm-ticket" / "SKILL.md",
    ]
    for path in ticket_cards:
        text = path.read_text(encoding="utf-8")
        assert "draft의 developer/code-reviewer" in text
        assert "board-git sync 0회" in text and "--role architect" in text
