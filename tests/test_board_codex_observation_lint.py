"""T-0653 live Codex observation scan wired to the PM-facing board lint."""

from __future__ import annotations

from pm_engine.app import codex_hook

import pytest

from pm_engine.domain import codex_envelope
from pm_engine.infra import codex_observation
from pm_engine.domain import delegate_channel_rules
from pm_engine.infra import pm_paths

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from pm_engine.app import areas_lint, body_lint, conf_lint, doc_lint, freshness_lint, lint_hooks, ticket_lint, wiki_lint, wikilink_lint
from pm_engine.cli import board_lint
from pm_engine.app import render_lint


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


def _observation_home(board, tmp_path):
    """board 실행 Home 에서 PM 로컬 입력(관측 로그 폴더)만 임시 경로로 바꾼 Home."""
    return pm_paths.home_at(board.REPO)._replace(local_dir=tmp_path / "local")


def _observation_dir(home):
    """lint 가 읽는 관측 로그 폴더 — 명시 Home 의 PM 로컬 입력 아래 `delegate-channel`."""
    return home.local_dir / "delegate-channel"


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
        (render_lint, "lint_render_leak", lambda home: []),
        (areas_lint, "lint_areas_repo_unregistered", lambda home: []),
        (areas_lint, "lint_areas_duplicate_repo", lambda home: []),
        (areas_lint, "lint_areas_merge_union", lambda home: []),
        (conf_lint, "lint_delegate", lambda home: []),
    ):
        monkeypatch.setattr(module, name, stub)
    monkeypatch.setattr(lint_hooks, "_run_lint_hooks", lambda home: [])


def test_unmatched_subagentstart_is_one_never_blocking_lint_advisory(
    guard_home, monkeypatch, tmp_path, capsys
):
    guard = _load("delegate_guard_board_lint_miss", TOOLS / "delegate_channel_guard.py")
    board = _load("board_codex_lint_miss", TOOLS / "board.py")
    home = _observation_home(board, tmp_path)
    codex_hook.observe_codex_subagent_start(guard_home, _subagent_start(), state_dir=_observation_dir(home))

    findings = ticket_lint.lint_codex_delegate_observations(home)

    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "Codex SubagentStart"
    assert kind == "codex-delegate-matcher-miss"
    assert kind in board_lint._ADVISORY_LINT_KINDS
    assert "PreToolUse 처리 기록이 없는 SubagentStart 1건" in detail
    assert "matcher drift" in detail
    _isolate_codex_lint(board, monkeypatch)
    assert ticket_lint.lint_tickets(home) == findings
    assert board_lint.cmd_lint(home, SimpleNamespace(gate=True)) == 0
    output = capsys.readouterr().out
    assert output.count("[codex-delegate-matcher-miss]") == 1


def test_matching_pretooluse_and_subagentstart_emit_zero_lint_advisories(
    guard_home, monkeypatch, tmp_path
):
    guard = _load("delegate_guard_board_lint_match", TOOLS / "delegate_channel_guard.py")
    board = _load("board_codex_lint_match", TOOLS / "board.py")
    decision = delegate_channel_rules._result("allow", "[delegate-channel/record] fixture")
    result = codex_envelope._codex_decision_envelope(decision)
    # allow envelope 는 `{}` 라 판정을 담지 못한다 — 라이브 훅과 같이 decision 을 함께 넘긴다.
    home = _observation_home(board, tmp_path)
    codex_hook.observe_codex_pretooluse(
        guard_home, _pretooluse(), result, decision=decision, state_dir=_observation_dir(home)
    )
    codex_hook.observe_codex_subagent_start(guard_home, _subagent_start(), state_dir=_observation_dir(home))

    assert ticket_lint.lint_codex_delegate_observations(home) == []
    _isolate_codex_lint(board, monkeypatch)
    assert ticket_lint.lint_tickets(home) == []


@pytest.fixture
def guard_home(tmp_path):
    return pm_paths.home_at(tmp_path / "rendered")
