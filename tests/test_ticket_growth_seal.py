"""T-0694 — 성장 역할 절 sha256 seal의 쓰기·검증·게이트 계약."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
PM_DELEGATE = REPO / ".project_manager" / "tools" / "pm_delegate.py"
BOARD = REPO / ".project_manager" / "tools" / "board.py"
PM_LOG = REPO / ".project_manager" / "tools" / "pm_log.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pd():
    return _load(PM_DELEGATE, "pm_delegate_seal_test")


@pytest.fixture
def board():
    return _load(BOARD, "board_seal_test")


def _frontmatter(ticket: str, status: str = "claimed") -> str:
    return (
        "---\n"
        f"id: {ticket}\n"
        "title: seal test\n"
        f"status: {status}\n"
        "created: '2026-08-16'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        "claimed_at: '2026-08-16T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\nblocks: []\ntouches: []\nestimate: medium\n"
        "design: 'waived: seal test'\ntags: []\n---\n"
    )


def _block(pd, role: str, ordinal: int, content: str, *, by="harvest") -> str:
    return (
        f"<!-- pm-ticket-section:start role={role} -->\n"
        + content
        + f"<!-- pm-ticket-section:end role={role} -->\n"
        + pd._ticket_seal_line(
            role, ordinal, content.encode("utf-8"), by=by,
        )
    )


def _unsealed_block(role: str, content: str) -> str:
    return (
        f"<!-- pm-ticket-section:start role={role} -->\n"
        + content
        + f"<!-- pm-ticket-section:end role={role} -->\n"
    )


def _review_ticket(pd, ticket: str = "T-2003") -> str:
    payload = {"version": 1, "findings": [], "confirmations": []}
    content = (
        "## 리뷰 (code-reviewer · 2026-08-16)\n\n"
        "probe=0\n\n판정: 통과\n\n## must-fix\n- 없음\n\n"
        "```pm-review-v1\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n```\n"
    )
    disposition = (
        "\n```pm-review-disposition-v1\n"
        '{"version":1,"reviewer_ordinal":0,"finding_zero":"accepted"}\n'
        "```\n"
    )
    return (
        _frontmatter(ticket)
        + f"# {ticket}\n\n## 완료 조건 (Definition of Done)\n- [x] seal test\n\n"
        + _block(pd, "code-reviewer", 0, content)
        + disposition
    )


def test_section_add_writes_seal_and_verifies(tmp_path, monkeypatch, board, pd):
    ticket = tmp_path / "claimed" / "T-2001-seal.md"
    ticket.parent.mkdir()
    ticket.write_bytes(
        (_frontmatter("T-2001", "open") + "# T-2001\n\n## 목표\nseal\n")
        .replace("\n", "\r\n").encode("utf-8")
    )
    monkeypatch.setattr(board, "_growth_ticket_path", lambda *_a, **_k: (0, ticket))
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_growth_mutation_sync", lambda *_a: True)
    monkeypatch.setattr(board, "drafts_dir", lambda: tmp_path / "drafts")
    calls = []
    real_line = pd._ticket_seal_line

    def recording_line(*args, **kwargs):
        calls.append((args, kwargs))
        return real_line(*args, **kwargs)

    monkeypatch.setattr(pd, "_ticket_seal_line", recording_line)

    assert board.cmd_section_add(argparse.Namespace(
        id="T-2001", role="developer", label=None,
    )) == 0
    text = ticket.read_bytes().decode("utf-8")
    assert pd.verify_ticket_seals(text) == []
    assert "\r\n" in text, "section-add가 기존 CRLF bytes를 먼저 재작성하면 안 된다"
    seal = pd.parse_ticket_seals(text)[("developer", 0)]
    assert seal.by == "section-add"
    assert len(calls) == 1 and calls[0][1] == {"by": "section-add"}


def test_section_add_requires_legacy_backfill_then_succeeds(
        tmp_path, monkeypatch, board, pd, capsys):
    ticket = tmp_path / "claimed" / "T-2014-seal.md"
    ticket.parent.mkdir()
    legacy = (
        _frontmatter("T-2014", "open")
        + _unsealed_block("developer", "## legacy\n\n")
    )
    ticket.write_text(legacy, encoding="utf-8", newline="\n")
    monkeypatch.setattr(board, "_growth_ticket_path", lambda *_a, **_k: (0, ticket))
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_growth_mutation_sync", lambda *_a: True)
    monkeypatch.setattr(board, "drafts_dir", lambda: tmp_path / "drafts")
    args = argparse.Namespace(id="T-2014", role="code-reviewer", label=None)

    assert board.cmd_section_add(args) == 1
    assert "seal-backfill --ticket T-2014" in capsys.readouterr().err
    assert ticket.read_text(encoding="utf-8") == legacy

    backfilled, changed = pd.backfill_ticket_seals(legacy)
    assert changed == [("developer", 0)]
    ticket.write_text(backfilled, encoding="utf-8", newline="\n")
    assert board.cmd_section_add(args) == 0
    assert pd.verify_ticket_seals(ticket.read_text(encoding="utf-8")) == []


def test_seal_syntax_documentation_is_not_a_candidate_but_column_zero_is(pd):
    content = "## 구현\n\nbody\n"
    documented = (
        "봉인 문법: `<!-- pm-ticket-seal role=<role> ordinal=<n> "
        "sha256=<hex64> by=<writer> -->`\n"
        + _block(pd, "developer", 0, content)
    )
    assert pd.verify_ticket_seals(documented) == []
    old_candidates = [
        line for line in documented.splitlines(keepends=True)
        if "<!-- pm-ticket-seal" in line
    ]
    assert any(pd._TICKET_SEAL_LINE_RE.fullmatch(line) is None for line in old_candidates)
    malformed = "<!-- pm-ticket-seal documented-only -->\n"
    assert any("손상 문법" in problem for problem in pd.verify_ticket_seals(malformed))


def test_prepare_edit_harvest_refreshes_seal_hash_atomically(
        tmp_path, monkeypatch, pd):
    pm_home = tmp_path / "pm"
    tickets = pm_home / ".project_manager" / "wiki" / "tickets"
    claimed = tickets / "claimed"
    claimed.mkdir(parents=True)
    for status in ("open", "blocked", "done"):
        (tickets / status).mkdir()
    (tickets / ".drafts").mkdir()
    ticket = claimed / "T-2002-seal.md"
    baseline = _frontmatter("T-2002") + _block(
        pd, "developer", 0, "## 구현\n\nold\n", by="backfill",
    )
    edited = baseline.replace("old\n", "new\n")
    ticket.write_text(baseline, encoding="utf-8", newline="\n")
    copy = tmp_path / "copy.md"
    plan = pd.TicketCopyPlan(
        copy, tmp_path / "baseline.md", tmp_path / "metadata.json",
        tmp_path, pm_home, "T-2002", "developer", b"x" * 32,
    )
    metadata = {
        "ordinal": 0,
        "baseline_sha256": pd.hashlib.sha256(baseline.encode()).hexdigest(),
        "source_relpath": str(ticket.relative_to(pm_home)),
    }

    class FakeBoard:
        @staticmethod
        def board_lock():
            return contextlib.nullcontext()

        @staticmethod
        def tickets_dir():
            return tickets

        @staticmethod
        def drafts_dir():
            return tickets / ".drafts"

        @staticmethod
        def _ticket_id_from_filename(name):
            return name.split("-seal.md", 1)[0]

        @staticmethod
        def _atomic_write_text(path, text):
            path.write_text(text, encoding="utf-8", newline="")

        @staticmethod
        def _growth_mutation_sync(_message, _path):
            return True

    monkeypatch.setattr(
        pd, "_load_ticket_copy_plan",
        lambda *_a, **_k: (plan, metadata, edited.encode(), baseline.encode()),
    )
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: FakeBoard)

    result = pd.harvest_ticket_copy(
        copy_path=copy, cwd=tmp_path, pm_home=pm_home, capability=b"x" * 32,
    )
    actual = ticket.read_text(encoding="utf-8")
    assert result == pd.TicketHarvestResult(True, True)
    assert "new\n" in actual and pd.verify_ticket_seals(actual) == []
    seal = pd.parse_ticket_seals(actual)[("developer", 0)]
    section = pd._ticket_role_section(actual, "developer")
    assert seal.by == "harvest"
    assert seal.sha256 == pd.seal_for(
        actual[section.content_start:section.content_end].encode("utf-8")
    )


def test_harvest_rejects_reissuing_a_mismatched_existing_seal(
        tmp_path, monkeypatch, pd):
    pm_home = tmp_path / "pm"
    tickets = pm_home / ".project_manager" / "wiki" / "tickets"
    claimed = tickets / "claimed"
    claimed.mkdir(parents=True)
    for status in ("open", "blocked", "done"):
        (tickets / status).mkdir()
    (tickets / ".drafts").mkdir()
    ticket = claimed / "T-2010-seal.md"
    sealed = _frontmatter("T-2010") + _block(
        pd, "developer", 0, "## 구현\n\ntrusted=0\n",
    )
    tampered = sealed.replace("trusted=0", "trusted=1", 1)
    ticket.write_text(tampered, encoding="utf-8", newline="\n")
    copy = tmp_path / "copy.md"
    plan = pd.TicketCopyPlan(
        copy, tmp_path / "baseline.md", tmp_path / "metadata.json",
        tmp_path, pm_home, "T-2010", "developer", b"x" * 32,
    )
    metadata = {
        "ordinal": 0,
        "baseline_sha256": pd.hashlib.sha256(tampered.encode()).hexdigest(),
        "source_relpath": str(ticket.relative_to(pm_home)),
    }

    class FakeBoard:
        @staticmethod
        def board_lock():
            return contextlib.nullcontext()

        @staticmethod
        def tickets_dir():
            return tickets

        @staticmethod
        def drafts_dir():
            return tickets / ".drafts"

        @staticmethod
        def _ticket_id_from_filename(name):
            return name.split("-seal.md", 1)[0]

        @staticmethod
        def _atomic_write_text(_path, _text):
            raise AssertionError("불일치 seal은 write 전에 거부해야 한다")

    monkeypatch.setattr(
        pd, "_load_ticket_copy_plan",
        lambda *_a, **_k: (plan, metadata, tampered.encode(), tampered.encode()),
    )
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: FakeBoard)

    with pytest.raises(pd.DelegateError, match="seal.*불일치.*재발급"):
        pd.harvest_ticket_copy(
            copy_path=copy, cwd=tmp_path, pm_home=pm_home, capability=b"x" * 32,
        )
    assert ticket.read_text(encoding="utf-8") == tampered
    assert any("sha256 불일치" in p for p in pd.verify_ticket_seals(tampered))
    laundered = pd._upsert_ticket_seal(tampered, "developer", 0, by="harvest")
    assert pd.verify_ticket_seals(laundered) == []
    assert pd.parse_ticket_seals(laundered)[("developer", 0)].by == "harvest"


def test_harvest_relocates_a_valid_but_misplaced_seal(
        tmp_path, monkeypatch, pd):
    pm_home = tmp_path / "pm"
    tickets = pm_home / ".project_manager" / "wiki" / "tickets"
    claimed = tickets / "claimed"
    claimed.mkdir(parents=True)
    for status in ("open", "blocked", "done"):
        (tickets / status).mkdir()
    (tickets / ".drafts").mkdir()
    ticket = claimed / "T-2016-seal.md"
    sealed = _frontmatter("T-2016") + _block(
        pd, "developer", 0, "## 구현\n\ncontent\n",
    )
    seal = pd.parse_ticket_seals(sealed)[("developer", 0)]
    seal_line = sealed[seal.line_start:seal.line_end]
    misplaced = sealed[:seal.line_start] + sealed[seal.line_end:] + "\n" + seal_line
    assert any("위치 불일치" in p for p in pd.verify_ticket_seals(misplaced))
    ticket.write_text(misplaced, encoding="utf-8", newline="\n")
    copy = tmp_path / "copy.md"
    plan = pd.TicketCopyPlan(
        copy, tmp_path / "baseline.md", tmp_path / "metadata.json",
        tmp_path, pm_home, "T-2016", "developer", b"x" * 32,
    )
    metadata = {
        "ordinal": 0,
        "baseline_sha256": pd.hashlib.sha256(misplaced.encode()).hexdigest(),
        "source_relpath": str(ticket.relative_to(pm_home)),
    }

    class FakeBoard:
        @staticmethod
        def board_lock():
            return contextlib.nullcontext()

        @staticmethod
        def tickets_dir():
            return tickets

        @staticmethod
        def drafts_dir():
            return tickets / ".drafts"

        @staticmethod
        def _ticket_id_from_filename(name):
            return name.split("-seal.md", 1)[0]

        @staticmethod
        def _atomic_write_text(path, text):
            path.write_text(text, encoding="utf-8", newline="")

        @staticmethod
        def _growth_mutation_sync(_message, _path):
            return True

    monkeypatch.setattr(
        pd, "_load_ticket_copy_plan",
        lambda *_a, **_k: (plan, metadata, misplaced.encode(), misplaced.encode()),
    )
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: FakeBoard)

    result = pd.harvest_ticket_copy(
        copy_path=copy, cwd=tmp_path, pm_home=pm_home, capability=b"x" * 32,
    )
    actual = ticket.read_text(encoding="utf-8")
    assert result == pd.TicketHarvestResult(True, True)
    assert pd.verify_ticket_seals(actual) == []
    section = pd._ticket_role_section(actual, "developer", ordinal=0)
    relocated = pd.parse_ticket_seals(actual)[("developer", 0)]
    assert relocated.line_start == section.marker_end


@pytest.mark.parametrize("mutation", ["one-byte-edit", "delete-seal"])
def test_tamper_is_red_at_review_and_complete_but_green_with_guard_disabled(
        mutation, tmp_path, monkeypatch, pd, board, capsys):
    ticket = tmp_path / "T-2003-seal.md"
    text = _review_ticket(pd)
    if mutation == "one-byte-edit":
        text = text.replace("probe=0", "probe=1", 1)
    else:
        seal = pd.parse_ticket_seals(text)[("code-reviewer", 0)]
        text = text[:seal.line_start] + text[seal.line_end:]
    ticket.write_text(text, encoding="utf-8", newline="\n")

    owner_board = SimpleNamespace(
        board_lock=lambda: contextlib.nullcontext(),
        find_ticket_exact=lambda _tid: ("claimed", ticket),
    )
    monkeypatch.setattr(
        pd, "_load_board", lambda: SimpleNamespace(_is_valid_ticket_id=lambda _tid: True),
    )
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: owner_board)

    real_verify = pd.verify_ticket_seals
    assert pd._cmd_review(["delta", "--ticket", "T-2003"]) == 1
    assert "unsealed" in capsys.readouterr().err
    monkeypatch.setattr(pd, "verify_ticket_seals", lambda _text: [])
    assert pd._cmd_review(["delta", "--ticket", "T-2003"]) == 0
    monkeypatch.setattr(pd, "verify_ticket_seals", real_verify)

    monkeypatch.setattr(board, "board_lock", lambda: contextlib.nullcontext())
    monkeypatch.setattr(
        board, "find_ticket_for_mutation", lambda _tid: ("claimed", ticket),
    )
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_internal_review_completion_problem", lambda _tid: None)
    args = argparse.Namespace(
        id="T-2003", tests_pass=True, allow_untested=False, allow_missing_log=True,
    )
    assert board.cmd_complete(args) == 1
    assert "unsealed" in capsys.readouterr().err
    monkeypatch.setattr(
        board, "_load_pm_delegate_module",
        lambda: SimpleNamespace(
            verify_ticket_seals=lambda _text: [],
            parse_ticket_seals=lambda _text: {},
        ),
    )
    assert board._complete_gate("T-2003", args, board.load_ticket(ticket)[1]) == []


def test_crlf_complete_verifies_the_exact_file_bytes(
        tmp_path, monkeypatch, pd, board):
    ticket = tmp_path / "T-2011-crlf.md"
    lf_content = "## 구현 (developer · 2026-08-16)\n\nCRLF facts\n"
    lf_growth = (
        "<!-- pm-ticket-section:start role=developer -->\n"
        + lf_content
        + "<!-- pm-ticket-section:end role=developer -->\n"
        + pd._ticket_seal_line(
            "developer", 0, lf_content.encode("utf-8"), by="harvest",
        )
    )
    growth = lf_growth.replace("\n", "\r\n")
    raw = (
        _frontmatter("T-2011").replace("\n", "\r\n")
        + "# T-2011\r\n\r\n## 완료 조건 (Definition of Done)\r\n"
          "- [x] CRLF 검증\r\n\r\n"
        + growth
    )
    ticket.write_bytes(raw.encode("utf-8"))
    observed = []
    real_verify = pd.verify_ticket_seals

    def recording_verify(text):
        observed.append(text)
        return real_verify(text)

    monkeypatch.setattr(pd, "verify_ticket_seals", recording_verify)
    monkeypatch.setattr(board, "board_lock", lambda: contextlib.nullcontext())
    monkeypatch.setattr(
        board, "find_ticket_for_mutation", lambda _tid: ("claimed", ticket),
    )
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_internal_review_completion_problem", lambda _tid: None)
    done = tmp_path / "done" / ticket.name
    monkeypatch.setattr(board, "move_ticket", lambda _path, _status: done)
    monkeypatch.setattr(board, "dump_ticket", lambda *_a, **_k: None)
    monkeypatch.setattr(board, "refresh_board", lambda: None)
    monkeypatch.setattr(board, "_board_git_sync_best_effort", lambda *_a: True)
    args = argparse.Namespace(
        id="T-2011", tests_pass=True, allow_untested=False, allow_missing_log=True,
    )

    assert board.cmd_complete(args) == 0
    assert observed and "\r\n" in observed[0]
    assert real_verify(observed[0]) == []
    assert real_verify(board.load_ticket(ticket)[1]) == []


def test_complete_without_growth_does_not_require_delegate_module(
        monkeypatch, board):
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: None)
    monkeypatch.setattr(board, "_internal_review_completion_problem", lambda _tid: None)
    args = argparse.Namespace(
        tests_pass=True, allow_untested=False, allow_missing_log=True,
    )
    body = "# T-2012\n\n## 완료 조건 (Definition of Done)\n- [x] no growth\n"
    assert board._complete_gate("T-2012", args, body) == []


def test_complete_blocks_orphan_seal_without_growth_marker(
        monkeypatch, board, pd):
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_internal_review_completion_problem", lambda _tid: None)
    args = argparse.Namespace(
        tests_pass=True, allow_untested=False, allow_missing_log=True,
    )
    body = (
        "# T-2015\n\n## 완료 조건 (Definition of Done)\n- [x] orphan\n\n"
        + pd._ticket_seal_line("code-reviewer", 0, b"", by="harvest")
    )
    problems = board._complete_gate("T-2015", args, body)
    assert any("unsealed" in problem and "고아 seal" in problem for problem in problems)


def test_orphan_and_duplicate_seals_are_malformed(pd):
    orphan = pd._ticket_seal_line("developer", 9, b"", by="backfill")
    assert any("고아 seal" in problem for problem in pd.verify_ticket_seals(orphan))

    content = "## 구현\n\n"
    sealed = _block(pd, "developer", 0, content)
    duplicate = sealed + pd._ticket_seal_line(
        "developer", 0, content.encode(), by="harvest",
    )
    assert any("seal 중복" in problem for problem in pd.verify_ticket_seals(duplicate))


def test_seal_backfill_only_fills_missing_and_done_cli_is_rejected(
        tmp_path, monkeypatch, pd, capsys):
    assert pd._SEAL_BACKFILL_STATUSES == frozenset(
        {"open", "claimed", "blocked", "draft"}
    )
    text = (
        _unsealed_block("developer", "round-0\n")
        + _unsealed_block("architect", "design\n")
    )
    updated, changed = pd.backfill_ticket_seals(text)
    assert changed == [("developer", 0), ("architect", 0)]
    assert pd.parse_ticket_seals(updated)[("developer", 0)].by == "backfill"
    assert pd.parse_ticket_seals(updated)[("architect", 0)].by == "backfill"
    assert pd.verify_ticket_seals(updated) == []

    sealed = _block(pd, "developer", 0, "round-0\n", by="harvest")
    same, no_changes = pd.backfill_ticket_seals(sealed)
    assert same == sealed and no_changes == []
    mixed = sealed + _unsealed_block("architect", "new manual section\n")
    with pytest.raises(pd.DelegateError, match="기계 복구 경로가 없습니다") as caught:
        pd.backfill_ticket_seals(mixed, ticket="T-2004")
    assert "seal-backfill" not in str(caught.value)
    assert "role=architect ordinal=0 비었는지=아니오" in str(caught.value)

    done = tmp_path / "T-2004-seal.md"
    done.write_text(_frontmatter("T-2004", "done") + text, encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        pd, "_load_board", lambda: SimpleNamespace(_is_valid_ticket_id=lambda _tid: True),
    )
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(
        pd, "_load_board_for_repo",
        lambda _owner: SimpleNamespace(
            board_lock=lambda: contextlib.nullcontext(),
            find_ticket_exact=lambda _tid: ("done", done),
        ),
    )
    assert pd._cmd_ticket(["seal-backfill", "--ticket", "T-2004"]) == 1
    assert "open/claimed" in capsys.readouterr().err


def test_mixed_recovery_guidance_is_shared_by_write_backfill_and_complete(
        monkeypatch, pd, board):
    growth = (
        _block(pd, "developer", 0, "recorded\n")
        + _unsealed_block("architect", "")
    )
    guidance = pd.ticket_growth_seal_recovery_guidance(growth, "T-2016")
    assert guidance is not None
    assert "seal-backfill" not in guidance
    assert "role=architect ordinal=0 비었는지=예" in guidance
    assert "빈 미봉인 절" in guidance
    assert "section-add T-2016 --role architect" in guidance

    with pytest.raises(pd.DelegateError) as write_error:
        pd.require_sealed_growth_before_write(
            growth, "T-2016", action="ticket prepare",
        )
    with pytest.raises(pd.DelegateError) as backfill_error:
        pd.backfill_ticket_seals(growth, ticket="T-2016")
    assert guidance in str(write_error.value)
    assert guidance in str(backfill_error.value)

    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_internal_review_completion_problem", lambda _tid: None)
    args = argparse.Namespace(
        tests_pass=True, allow_untested=False, allow_missing_log=True,
    )
    body = (
        "# T-2016\n\n## 완료 조건 (Definition of Done)\n- [x] mixed 안내\n\n"
        + growth
    )
    problems = board._complete_gate("T-2016", args, body)
    assert any(guidance in problem for problem in problems)


def test_review_delta_uses_shared_mixed_recovery_guidance(
        tmp_path, monkeypatch, pd, capsys):
    ticket_id = "T-2017"
    growth = (
        _block(pd, "developer", 0, "recorded\n")
        + _unsealed_block("architect", "")
    )
    guidance = pd.ticket_growth_seal_recovery_guidance(growth, ticket_id)
    assert guidance is not None
    ticket = tmp_path / f"{ticket_id}-seal.md"
    ticket.write_text(_frontmatter(ticket_id) + growth, encoding="utf-8", newline="\n")
    owner_board = SimpleNamespace(
        board_lock=lambda: contextlib.nullcontext(),
        find_ticket_exact=lambda _tid: ("claimed", ticket),
    )
    monkeypatch.setattr(
        pd, "_load_board", lambda: SimpleNamespace(_is_valid_ticket_id=lambda _tid: True),
    )
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: owner_board)

    assert pd._cmd_review(["delta", "--ticket", ticket_id]) == 1
    error = capsys.readouterr().err
    assert guidance in error
    assert "사본 재기록·재회수를 요청하라" not in error


@pytest.mark.parametrize("unsealed_is_terminal", [True, False])
def test_mixed_recovery_guidance_distinguishes_terminal_ordinal(
        pd, unsealed_is_terminal):
    if unsealed_is_terminal:
        growth = (
            _block(pd, "developer", 0, "recorded\n")
            + _unsealed_block("developer", "new output\n")
        )
    else:
        growth = (
            _unsealed_block("developer", "old output\n")
            + _block(pd, "developer", 1, "recorded\n")
        )

    guidance = pd.ticket_growth_seal_recovery_guidance(growth, "T-2018")
    assert guidance is not None
    if unsealed_is_terminal:
        assert "내용 있는 미봉인 절(role=developer ordinal=1)" in guidance
        assert "절을 제거한 뒤" in guidance
        assert "section-add T-2018 --role developer" in guidance
        assert "재prepare해 역할이 재기록·harvest" in guidance
    else:
        assert (
            "비말단 미봉인 절(role=developer ordinal=0, 해당 역할의 최대 ordinal=1)"
            in guidance
        )
        assert "기계 복구 경로가 없어 장부 기반 판정이 필요" in guidance
        assert "복구 처방을 제공하지 않습니다" in guidance
        assert "절을 제거" not in guidance
        assert "section-add" not in guidance
        assert "재prepare" not in guidance


def test_seal_backfill_cli_writes_ticket_and_reason_log(
        tmp_path, monkeypatch, pd, capsys):
    ticket = tmp_path / "T-2006-seal.md"
    ticket.write_text(
        _frontmatter("T-2006", "open")
        + _unsealed_block("developer", "legacy body\n"),
        encoding="utf-8",
        newline="\n",
    )
    sync_calls = []

    class OwnerBoard:
        @staticmethod
        def board_lock():
            return contextlib.nullcontext()

        @staticmethod
        def find_ticket_exact(_tid):
            return "open", ticket

        @staticmethod
        def _atomic_write_text(path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")

        @staticmethod
        def _board_git_sync_best_effort(message, paths):
            sync_calls.append((message, tuple(paths)))
            return True

    monkeypatch.setattr(
        pd, "_load_board", lambda: SimpleNamespace(_is_valid_ticket_id=lambda _tid: True),
    )
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: OwnerBoard)

    assert pd._cmd_ticket(["seal-backfill", "--ticket", "T-2006"]) == 0
    assert pd.verify_ticket_seals(ticket.read_text(encoding="utf-8")) == []
    log = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    log_text = log.read_text(encoding="utf-8")
    assert "T-2006 seal-backfill" in log_text
    assert "성장 절 봉인 도입 이전" in log_text and "T-0694 발행 이전" not in log_text
    pm_log = _load(PM_LOG, "pm_log_seal_test")
    entries = pm_log.split_entries(log_text)[1]
    assert len(entries) == 1
    assert entries[0][1].startswith("## [") and " ticket | T-2006 seal-backfill" in entries[0][1]
    assert sync_calls and "role=developer ordinal=0" in capsys.readouterr().out


def test_seal_line_inside_role_section_is_malformed(pd):
    nested = pd._ticket_seal_line("developer", 0, b"", by="harvest")
    text = _unsealed_block("developer", "## 구현\n\n" + nested)
    assert any("역할 절 본문 안" in problem for problem in pd.verify_ticket_seals(text))


def test_three_rounds_have_independent_ordinal_seals(pd):
    text = "".join(
        _unsealed_block("developer", f"round-{ordinal}\n")
        for ordinal in range(3)
    )
    updated, changed = pd.backfill_ticket_seals(text)
    assert changed == [("developer", 0), ("developer", 1), ("developer", 2)]
    assert pd.verify_ticket_seals(updated) == []
    seals = pd.parse_ticket_seals(updated)
    assert set(seals) == {("developer", 0), ("developer", 1), ("developer", 2)}
    assert len({seal.sha256 for seal in seals.values()}) == 3


def test_complete_surfaces_backfill_seal_as_one_advisory(
        monkeypatch, board, pd, capsys):
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_internal_review_completion_problem", lambda _tid: None)
    args = argparse.Namespace(
        tests_pass=True, allow_untested=False, allow_missing_log=True,
    )
    body = (
        "# T-2013\n\n## 완료 조건 (Definition of Done)\n- [x] migrated\n\n"
        + _block(pd, "developer", 0, "legacy\n", by="backfill")
    )
    assert board._complete_gate("T-2013", args, body) == []
    lines = [line for line in capsys.readouterr().err.splitlines() if "growth-seal" in line]
    assert len(lines) == 1 and "by=backfill" in lines[0]


def test_growth_seal_lint_is_one_never_block_advisory(
        tmp_path, monkeypatch, board, pd):
    tickets = tmp_path / "tickets"
    (tickets / "open").mkdir(parents=True)
    (tickets / "claimed").mkdir()
    (tickets / "done").mkdir()
    (tickets / "open" / "T-2005-seal.md").write_text(
        _frontmatter("T-2005", "open") + _unsealed_block("developer", "body\n"),
        encoding="utf-8",
        newline="\n",
    )
    (tickets / "claimed" / "T-2007-backfill.md").write_text(
        _frontmatter("T-2007") + _block(
            pd, "developer", 0, "legacy\n", by="backfill",
        ),
        encoding="utf-8",
        newline="\n",
    )
    (tickets / "done" / "T-1999-old.md").write_text(
        _frontmatter("T-1999", "done") + _unsealed_block("developer", "legacy\n"),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(board, "tickets_dir", lambda: tickets)
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    findings = board.lint_growth_seals()
    assert len(findings) == 1
    assert findings[0][1] == "growth-seal" and "T-2005" in findings[0][2]
    assert "T-2007" in findings[0][2] and "by=backfill" in findings[0][2]
    assert "T-1999" not in findings[0][2]
    assert "growth-seal" in board._ADVISORY_LINT_KINDS
