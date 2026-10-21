"""T-0653 live Codex observation scan wired to the PM-facing board lint."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from pm_engine.app import areas_lint, body_lint, conf_lint, doc_lint, freshness_lint, lint_hooks, ticket_lint, wiki_lint, wikilink_lint


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
LIVE_PAYLOADS_FIXTURE = (
    REPO / "tests" / "fixtures" / "codex_0_147_0_live_hook_payloads.json"
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live_events() -> list[dict[str, object]]:
    evidence = json.loads(LIVE_PAYLOADS_FIXTURE.read_text(encoding="utf-8"))
    events = evidence["events"]
    assert isinstance(events, list) and len(events) == 4
    return events


def _pretooluse() -> dict[str, object]:
    return deepcopy(_live_events()[0])


def _subagent_start() -> dict[str, object]:
    return deepcopy(_live_events()[3])


def _isolate_codex_lint(board, monkeypatch) -> None:
    for module, name, stub in (
        (ticket_lint, "lint_dependencies", lambda home: []),
        (body_lint, "lint_bodies", lambda home: []),
        (body_lint, "lint_ideas", lambda home: []),
        (wiki_lint, "lint_status", lambda home: []),
        (wikilink_lint, "lint_wikilinks", lambda home: []),
        (wikilink_lint, "lint_unstable_refs", lambda home: []),
        (wiki_lint, "lint_scopes", lambda home: []),
        (doc_lint, "lint_domain", lambda home: []),
        (wiki_lint, "lint_adr_lifecycle", lambda home: []),
        (wiki_lint, "lint_adr_author", lambda home: []),
        (freshness_lint, "lint_architecture_freshness", lambda home: []),
        (freshness_lint, "lint_status_freshness", lambda home: []),
        (doc_lint, "lint_domain_freshness", lambda home: []),
        (conf_lint, "lint_adapter_drift", lambda home: []),
        (board, "lint_render_leak", lambda: []),
        (areas_lint, "lint_areas_repo_unregistered", lambda home: []),
        (areas_lint, "lint_areas_duplicate_repo", lambda home: []),
        (areas_lint, "lint_areas_merge_union", lambda home: []),
        (conf_lint, "lint_delegate", lambda home: []),
    ):
        monkeypatch.setattr(module, name, stub)
    monkeypatch.setattr(lint_hooks, "_run_lint_hooks", lambda home: [])


def test_unmatched_subagentstart_is_one_never_blocking_lint_advisory(
    monkeypatch, tmp_path, capsys
):
    guard = _load("delegate_guard_board_lint_miss", TOOLS / "delegate_channel_guard.py")
    board = _load("board_codex_lint_miss", TOOLS / "board.py")
    guard.observe_codex_subagent_start(_subagent_start(), state_dir=tmp_path)
    monkeypatch.setattr(board, "CODEX_DELEGATE_OBSERVATIONS", guard._codex_audit_path(tmp_path))

    findings = board.lint_codex_delegate_observations()

    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "Codex SubagentStart"
    assert kind == "codex-delegate-matcher-miss"
    assert kind in board._ADVISORY_LINT_KINDS
    assert "PreToolUse 처리 기록이 없는 SubagentStart 1건" in detail
    assert "matcher drift" in detail
    _isolate_codex_lint(board, monkeypatch)
    assert board.lint_tickets() == findings
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    output = capsys.readouterr().out
    assert output.count("[codex-delegate-matcher-miss]") == 1


def test_matching_pretooluse_and_subagentstart_emit_zero_lint_advisories(
    monkeypatch, tmp_path
):
    guard = _load("delegate_guard_board_lint_match", TOOLS / "delegate_channel_guard.py")
    board = _load("board_codex_lint_match", TOOLS / "board.py")
    decision = guard._result("allow", "[delegate-channel/record] fixture")
    result = guard._codex_decision_envelope(decision)
    # allow envelope 는 `{}` 라 판정을 담지 못한다 — 라이브 훅과 같이 decision 을 함께 넘긴다.
    guard.observe_codex_pretooluse(
        _pretooluse(), result, decision=decision, state_dir=tmp_path
    )
    guard.observe_codex_subagent_start(_subagent_start(), state_dir=tmp_path)
    monkeypatch.setattr(board, "CODEX_DELEGATE_OBSERVATIONS", guard._codex_audit_path(tmp_path))

    assert board.lint_codex_delegate_observations() == []
    _isolate_codex_lint(board, monkeypatch)
    assert board.lint_tickets() == []
