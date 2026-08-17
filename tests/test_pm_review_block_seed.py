"""T-0700 — parser 상수 기반 리뷰 골격과 disposition-template 계약."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PM_DELEGATE = REPO / ".project_manager" / "tools" / "pm_delegate.py"
BOARD = REPO / ".project_manager" / "tools" / "board.py"

_SCHEMA_SURFACE_GLOBS = (
    ".claude/agents/*.md",
    "templates/claude_code/.claude/agents/*.md",
    "templates/codex/.codex/agents/*.toml",
    "templates/opencode/.opencode/agents/*.md",
    ".claude/skills/*/SKILL.md",
    "templates/*/.claude/skills/*/SKILL.md",
    "templates/*/.agents/skills/*/SKILL.md",
    "templates/*/.codex/skills/*/SKILL.md",
    "templates/opencode/.opencode/command/*.md",
    ".project_manager/wiki/pm_playbook.md",
    "templates/*/.project_manager/wiki/pm_playbook.md",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema_literal_findings(pd, text: str) -> list[tuple[str, int]]:
    """parser 상수에서 파생한 schema family가 한 문단에 복제됐는지 찾는다."""
    families = {
        "finding-keys": pd.PM_REVIEW_FINDING_KEYS,
        "confirmation-keys": pd.PM_REVIEW_CONFIRMATION_KEYS,
        "disposition-keys": pd.PM_REVIEW_DISPOSITION_KEYS,
        "classes": pd.PM_REVIEW_CLASSES,
        "confirmation-states": pd.PM_REVIEW_CONFIRMATION_STATES,
    }
    findings: list[tuple[str, int]] = []
    offset = 0
    for paragraph in re.split(r"(?:\r?\n){2,}", text):
        line = text.count("\n", 0, offset) + 1
        for label, tokens in families.items():
            if all(re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])",
                paragraph,
            ) for token in tokens):
                findings.append((label, line))
        offset += len(paragraph)
        while offset < len(text) and text[offset] in "\r\n":
            offset += 1
    return findings


def _schema_surface_paths() -> list[Path]:
    return sorted({
        path
        for pattern in _SCHEMA_SURFACE_GLOBS
        for path in REPO.glob(pattern)
        if path.is_file()
    })


@pytest.fixture
def pd():
    return _load(PM_DELEGATE, "pm_delegate_review_seed")


@pytest.fixture
def board():
    return _load(BOARD, "board_review_seed")


def _review_payload(*ids: str) -> dict:
    return {
        "version": 1,
        "findings": [
            {
                "id": finding_id,
                "class": "implementation-defect",
                "severity": "must-fix",
                "authority": "[[T-0700]] §완료 조건",
                "evidence": f"{finding_id} probe",
                "recommendation": f"{finding_id} fix",
                "design_change": False,
            }
            for finding_id in ids
        ],
        "confirmations": [],
    }


def _decision(finding_id: str, decision: str) -> dict:
    return {
        "id": finding_id,
        "decision": decision,
        "reason": f"{finding_id} {decision} 근거",
        "scope": f"{finding_id} 허용 범위" if decision == "accepted" else "",
        "prerequisite": "",
    }


def _disposition_block(rows: list[dict], *, ordinal: int = 0) -> str:
    payload = {"version": 1, "reviewer_ordinal": ordinal, "dispositions": rows}
    return (
        "```pm-review-disposition-v1\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _sealed_reviewer_section(pd, payload: dict, *, ordinal: int = 0) -> str:
    content = (
        "## 리뷰 (code-reviewer · 2026-08-17)\n\n"
        "## must-fix\n- 구조화 finding 참조\n\n"
        "## should-fix\n- 없음\n\n"
        "## suggestion\n- 없음\n\n"
        "## 판정\n판정: 반려\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )
    digest = pd.seal_for(content.encode("utf-8"))
    return (
        "<!-- pm-ticket-section:start role=code-reviewer -->\n"
        + content
        + "<!-- pm-ticket-section:end role=code-reviewer -->\n"
        + f"<!-- pm-ticket-seal role=code-reviewer ordinal={ordinal} "
        + f"sha256={digest} by=section-add -->\n"
    )


def _seeded_section(pd, role: str, ticket_prefix: str = "") -> str:
    content = (
        f"## 역할 ({role} · 2026-08-17)\n\n"
        + pd.render_ticket_growth_section_seed(role, ticket_prefix)
    )
    digest = pd.seal_for(content.encode("utf-8"))
    return (
        f"<!-- pm-ticket-section:start role={role} -->\n"
        + content
        + f"<!-- pm-ticket-section:end role={role} -->\n"
        + f"<!-- pm-ticket-seal role={role} ordinal=0 sha256={digest} "
        + "by=section-add -->\n"
    )


def _seed_growth_ledger(pd, path: Path, ticket: str = "T-0700") -> None:
    """봉인 도입 이후 형상 — 현재 봉인들을 그 티켓 장부에 기재한다(sweep 등가·T-0699)."""
    pd.append_ticket_growth_records(
        pd.ticket_growth_dir_for_ticket_path(path), ticket,
        path.read_text(encoding="utf-8"), by="backfill", stamp=False,
    )


def _legacy_reviewer_section(pd, ordinal: int) -> str:
    content = (
        "## 리뷰 (code-reviewer · 2026-08-17)\n\n"
        "판정: 통과\n\n## must-fix\n- 없음\n\n"
        f"legacy reviewer ordinal {ordinal}\n"
    )
    digest = pd.seal_for(content.encode("utf-8"))
    return (
        "<!-- pm-ticket-section:start role=code-reviewer -->\n"
        + content
        + "<!-- pm-ticket-section:end role=code-reviewer -->\n"
        + f"<!-- pm-ticket-seal role=code-reviewer ordinal={ordinal} "
        + f"sha256={digest} by=section-add -->\n"
    )


def _run_harvest(
    pd, tmp_path, monkeypatch, *, ticket_text: str, copy_text: str, role: str,
):
    pm_home = tmp_path / "pm-home"
    slot = tmp_path / "slot"
    source = pm_home / "board" / "tickets" / "claimed" / "T-0700-seed.md"
    source.parent.mkdir(parents=True)
    source.write_text(ticket_text, encoding="utf-8", newline="\n")
    _seed_growth_ledger(pd, source)
    copy = slot / "copies" / "T-0700" / role / ("a" * 32) / "ticket-T-0700.md"
    copy.parent.mkdir(parents=True)
    copy.write_text(copy_text, encoding="utf-8", newline="\n")
    plan = pd.TicketCopyPlan(
        copy, copy.with_name("baseline.md"), copy.with_name("meta.json"),
        slot, pm_home, "T-0700", role, b"x" * 32,
    )
    baseline = ticket_text.encode("utf-8")
    metadata = {
        "ordinal": max(
            section.ordinal for section in pd._ticket_growth_sections(ticket_text)
            if section.role == role
        ),
        "source_relpath": "board/tickets/claimed/T-0700-seed.md",
        "baseline_sha256": hashlib.sha256(baseline).hexdigest(),
    }

    class FakeBoard:
        @staticmethod
        @contextlib.contextmanager
        def board_lock():
            yield

        @staticmethod
        def tickets_dir():
            return pm_home / "board" / "tickets"

        @staticmethod
        def drafts_dir():
            return pm_home / "board" / "tickets" / ".drafts"

        @staticmethod
        def _ticket_id_from_filename(_name):
            return "T-0700"

        @staticmethod
        def _atomic_write_text(path, text):
            path.write_text(text, encoding="utf-8", newline="\n")

        @staticmethod
        def _growth_mutation_sync(_message, _path):
            return True

    monkeypatch.setattr(
        pd, "_load_ticket_copy_plan",
        lambda *_a, **_k: (plan, metadata, copy_text.encode("utf-8"), baseline),
    )
    monkeypatch.setattr(pd, "_ticket_copy_ledger_record", lambda *_a, **_k: None)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _home: FakeBoard())
    monkeypatch.setattr(pd, "_mark_ticket_copy_harvested_best_effort", lambda *_a: None)
    result = pd.harvest_ticket_copy(
        copy_path=copy, cwd=slot, pm_home=pm_home, capability=b"x" * 32,
    )
    return result, source.read_text(encoding="utf-8")


def test_section_add_seeds_every_growth_role_from_delegate_renderer(
    pd, board, tmp_path, monkeypatch,
):
    path = tmp_path / "tickets" / "claimed" / "T-0700-seed.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: T-0700\nstatus: claimed\n---\n# ticket\n", encoding="utf-8", newline="\n")

    @contextlib.contextmanager
    def unlocked():
        yield

    monkeypatch.setattr(board, "board_lock", unlocked)
    monkeypatch.setattr(board, "_growth_ticket_path", lambda *_a, **_k: (0, path))
    monkeypatch.setattr(board, "load_ticket", lambda _path: ({}, ""))
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_growth_mutation_sync_paths", lambda *_a, **_k: True)
    monkeypatch.setattr(board, "drafts_dir", lambda: tmp_path / "drafts")

    # 성장 역할 집합의 단일 권위는 pm_delegate 이고 board 는 사람용 절명만 소유한다.
    assert set(board.TICKET_GROWTH_ROLE_LABELS) == set(pd.TICKET_COPY_ROLES)
    for role in sorted(pd.TICKET_COPY_ROLES):
        args = argparse.Namespace(role=role, label=None, id="T-0700")
        assert board.cmd_section_add(args) == 0, role

    text = path.read_text(encoding="utf-8")
    assert "검토 판정: <설계 통과|수정 후 통과|반려>" in text
    assert all(term in text for term in (
        "## 변경 파일", "## 신규 테스트", "## 회귀", "## DoD evidence", "## 민감도",
    ))
    assert all(term in text for term in (
        "## 조사 질문", "## 실측", "## 판단", "## 미해소",
    ))
    assert all(term in text for term in (
        "## must-fix", "## 판정", "```pm-review-v1",
    ))
    # 두 리뷰 채널이 각자 접두의 골격을 받는다(판정 표면은 하나·ID 네임스페이스는 분리).
    assert '"id":"F-NNN"' in text and '"id":"X-NNN"' in text
    assert pd.verify_ticket_seals(text) == []


def test_review_seed_follows_parser_class_status_and_key_constants(pd, monkeypatch):
    monkeypatch.setattr(
        pd, "PM_REVIEW_CLASSES", (*pd.PM_REVIEW_CLASSES, "security-regression"),
    )
    monkeypatch.setattr(
        pd, "PM_REVIEW_CONFIRMATION_STATES",
        (*pd.PM_REVIEW_CONFIRMATION_STATES, "not-retested"),
    )
    monkeypatch.setattr(
        pd, "PM_REVIEW_FINDING_KEYS", (*pd.PM_REVIEW_FINDING_KEYS, "impact"),
    )
    monkeypatch.setattr(
        pd, "PM_REVIEW_CONFIRMATION_KEYS",
        (*pd.PM_REVIEW_CONFIRMATION_KEYS, "environment"),
    )
    rendered = pd.render_ticket_growth_section_seed("code-reviewer", "")
    payload = json.loads(rendered.split("```pm-review-v1\n", 1)[1].split("\n```", 1)[0])
    finding = payload["findings"][0]
    confirmation = payload["confirmations"][0]
    assert "security-regression" in finding["class"]
    assert "not-retested" in confirmation["status"]
    assert set(finding) == set(pd.PM_REVIEW_FINDING_KEYS) and finding["impact"] == ""
    assert set(confirmation) == set(pd.PM_REVIEW_CONFIRMATION_KEYS)
    assert confirmation["environment"] == ""


def test_review_seed_pins_current_parser_schema_contract(pd):
    rendered = pd.render_ticket_growth_section_seed("code-reviewer", "")
    payload = json.loads(rendered.split("```pm-review-v1\n", 1)[1].split("\n```", 1)[0])
    assert payload["findings"][0]["class"] == (
        "<implementation-defect|spec-violation|design-proposal>"
    )
    assert payload["findings"][0]["severity"] == (
        "<must-fix|should-fix|suggestion>"
    )
    assert payload["confirmations"][0]["status"] == (
        "<resolved|unresolved|regressed>"
    )
    assert list(payload["findings"][0]) == [
        "id", "class", "severity", "authority", "evidence", "recommendation",
        "design_change",
    ]


def test_confirmation_round_prefills_every_id_from_previous_reviewer(pd):
    previous = _sealed_reviewer_section(pd, _review_payload("F-003", "F-001"))
    rendered = pd.render_ticket_growth_section_seed("code-reviewer", previous)
    payload = json.loads(rendered.split("```pm-review-v1\n", 1)[1].split("\n```", 1)[0])
    assert [row["id"] for row in payload["confirmations"]] == ["F-003", "F-001"]
    assert all(row["status"] == "<resolved|unresolved|regressed>"
               for row in payload["confirmations"])


def test_confirmation_seed_excludes_previous_rejected_findings(pd):
    previous = _sealed_reviewer_section(pd, _review_payload("F-001", "F-002"))
    ticket = previous + _disposition_block([
        _decision("F-001", "accepted"),
        _decision("F-002", "rejected"),
    ])
    rendered = pd.render_ticket_growth_section_seed("code-reviewer", ticket)
    payload = json.loads(rendered.split("```pm-review-v1\n", 1)[1].split("\n```", 1)[0])
    assert [row["id"] for row in payload["confirmations"]] == ["F-001"]


def test_unedited_seed_is_malformed_review_output(pd):
    ticket = _seeded_section(pd, "code-reviewer")
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(ticket)
    assert caught.value.code == "malformed"


def test_harvest_warns_loudly_when_seed_was_not_edited(pd, tmp_path, monkeypatch, capsys):
    ticket_text = "---\nid: T-0700\nstatus: claimed\n---\n" + _seeded_section(pd, "developer")
    result, _updated = _run_harvest(
        pd, tmp_path, monkeypatch, ticket_text=ticket_text,
        copy_text=ticket_text, role="developer",
    )
    assert result.sync_ready is True
    assert "산출 없음" in capsys.readouterr().err


def test_legacy_reviewer_prefill_degrades_and_section_add_stays_available(
    pd, board, tmp_path, monkeypatch, capsys,
):
    path = tmp_path / "tickets" / "claimed" / "T-0700-legacy.md"
    path.parent.mkdir(parents=True)
    original = (
        "---\nid: T-0700\nstatus: claimed\n---\n"
        + _legacy_reviewer_section(pd, 0)
        + _legacy_reviewer_section(pd, 1)
    )
    path.write_text(original, encoding="utf-8", newline="\n")
    _seed_growth_ledger(pd, path)

    @contextlib.contextmanager
    def unlocked():
        yield

    monkeypatch.setattr(board, "board_lock", unlocked)
    monkeypatch.setattr(board, "_growth_ticket_path", lambda *_a, **_k: (0, path))
    monkeypatch.setattr(board, "load_ticket", lambda _path: ({}, ""))
    monkeypatch.setattr(board, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(board, "_growth_mutation_sync_paths", lambda *_a, **_k: True)
    monkeypatch.setattr(board, "drafts_dir", lambda: tmp_path / "drafts")

    args = argparse.Namespace(role="code-reviewer", label=None, id="T-0700")
    assert board.cmd_section_add(args) == 0
    updated = path.read_text(encoding="utf-8")
    assert len([s for s in pd._ticket_growth_sections(updated)
                if s.role == "code-reviewer"]) == 3
    assert '"id":"F-NNN"' in updated
    assert "F-NNN 골격으로 강등" in capsys.readouterr().err
    assert pd.verify_ticket_seals(updated) == []


def test_legacy_reviewer_seed_probe_warns_false_and_harvest_does_not_block(
    pd, tmp_path, monkeypatch, capsys,
):
    ticket_text = (
        "---\nid: T-0700\nstatus: claimed\n---\n"
        + _legacy_reviewer_section(pd, 0)
        + _legacy_reviewer_section(pd, 1)
    )
    latest = [
        section for section in pd._ticket_growth_sections(ticket_text)
        if section.role == "code-reviewer"
    ][-1]
    assert pd.ticket_growth_section_seed_is_unedited(ticket_text, latest) is False
    assert "F-NNN 골격으로 강등" in capsys.readouterr().err

    addition = "R2 legacy reviewer 산출 회수 성공\n"
    copy_text = (
        ticket_text[:latest.content_end] + addition + ticket_text[latest.content_end:]
    )
    result, updated = _run_harvest(
        pd, tmp_path, monkeypatch, ticket_text=ticket_text,
        copy_text=copy_text, role="code-reviewer",
    )
    assert result.changed is True and result.sync_ready is True
    assert addition in updated
    assert "F-NNN 골격으로 강등" in capsys.readouterr().err


def test_seed_unedited_probe_fails_open_when_renderer_cannot_judge(
    pd, monkeypatch, capsys,
):
    ticket = _seeded_section(pd, "developer")
    section = pd._ticket_growth_sections(ticket)[0]

    def fail_seed(_role, _prefix):
        raise pd.DelegateError("legacy parser unavailable")

    monkeypatch.setattr(pd, "render_ticket_growth_section_seed", fail_seed)
    assert pd.ticket_growth_section_seed_is_unedited(ticket, section) is False
    warning = capsys.readouterr().err
    assert "편집됨으로 간주" in warning and "legacy parser unavailable" in warning


def test_disposition_template_prefills_all_pending_ids_and_filled_block_passes(pd):
    ticket = _sealed_reviewer_section(pd, _review_payload("F-002", "F-001"))
    rendered = pd.render_pm_review_disposition_template(ticket)
    payload = json.loads(
        rendered.split("```pm-review-disposition-v1\n", 1)[1].split("\n```", 1)[0]
    )
    assert payload["reviewer_ordinal"] == 0
    assert [row["id"] for row in payload["dispositions"]] == ["F-002", "F-001"]
    assert all(set(row) == set(pd.PM_REVIEW_DISPOSITION_KEYS)
               for row in payload["dispositions"])

    for row in payload["dispositions"]:
        row.update(decision="accepted", reason="PM 수락", scope=f"{row['id']} 범위")
    filled = (
        "```pm-review-disposition-v1\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )
    delta = pd.parse_pm_review_delta(ticket + filled)
    assert [finding.id for finding, _decision in delta.accepted] == ["F-002", "F-001"]


def test_disposition_template_preserves_existing_rows_and_replaces_partial_block(pd):
    review = _sealed_reviewer_section(pd, _review_payload("F-001", "F-002"))
    existing = _decision("F-001", "rejected")
    ticket = review + _disposition_block([existing])
    rendered = pd.render_pm_review_disposition_template(ticket)
    payload = json.loads(
        rendered.split("```pm-review-disposition-v1\n", 1)[1].split("\n```", 1)[0]
    )
    assert payload["dispositions"][0] == existing
    assert payload["dispositions"][1] == {
        "id": "F-002",
        "decision": "<accepted|rejected>",
        "reason": "",
        "scope": "",
        "prerequisite": "",
    }

    payload["dispositions"][1].update(
        decision="accepted", reason="F-002 수락", scope="F-002 허용 범위",
    )
    replacement = (
        "```pm-review-disposition-v1\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )
    delta = pd.parse_pm_review_delta(review + replacement)
    assert [finding.id for finding, _row in delta.accepted] == ["F-002"]


def test_disposition_template_rejects_confirmation_only_round(
    pd, tmp_path, monkeypatch, capsys,
):
    first = _sealed_reviewer_section(pd, _review_payload("F-001"), ordinal=0)
    second_payload = {
        "version": 1,
        "findings": [],
        "confirmations": [{
            "id": "F-001", "status": "resolved", "evidence": "회귀 통과",
        }],
    }
    second = _sealed_reviewer_section(pd, second_payload, ordinal=1)
    with pytest.raises(pd.DelegateError, match="confirmation-only.*신규 finding이 없습니다"):
        pd.render_pm_review_disposition_template(first + second, 1)

    ticket_path = tmp_path / "tickets" / "claimed" / "T-0700-confirmation-only.md"
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text(first + second, encoding="utf-8", newline="\n")
    _seed_growth_ledger(pd, ticket_path)

    class FakeBoard:
        @staticmethod
        @contextlib.contextmanager
        def board_lock():
            yield

        @staticmethod
        def find_ticket_exact(_ticket):
            return "claimed", ticket_path

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: FakeBoard())
    assert pd.main([
        "review", "disposition-template", "--ticket", "T-0700", "--ordinal", "1",
    ]) == 1
    assert "confirmation-only" in capsys.readouterr().err


def test_disposition_template_defaults_latest_and_honors_explicit_ordinal(pd):
    ticket = (
        _sealed_reviewer_section(pd, _review_payload("F-001"), ordinal=0)
        + _sealed_reviewer_section(pd, _review_payload("F-002"), ordinal=1)
    )
    latest = pd.render_pm_review_disposition_template(ticket)
    first = pd.render_pm_review_disposition_template(ticket, 0)
    assert '"reviewer_ordinal":1' in latest and '"id":"F-002"' in latest
    assert '"reviewer_ordinal":0' in first and '"id":"F-001"' in first


def test_disposition_template_cli_and_pending_error_prescribe_same_command(
    pd, tmp_path, monkeypatch, capsys,
):
    ticket_path = tmp_path / "tickets" / "claimed" / "T-0700-review.md"
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text(
        _sealed_reviewer_section(pd, _review_payload("F-007")), encoding="utf-8",
        newline="\n",
    )
    _seed_growth_ledger(pd, ticket_path)

    class FakeBoard:
        @staticmethod
        @contextlib.contextmanager
        def board_lock():
            yield

        @staticmethod
        def find_ticket_exact(_ticket):
            return "claimed", ticket_path

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: FakeBoard())
    assert pd.main([
        "review", "disposition-template", "--ticket", "T-0700",
    ]) == 0
    assert "F-007" in capsys.readouterr().out

    assert pd.main(["review", "delta", "--ticket", "T-0700"]) == 1
    error = capsys.readouterr().err
    assert "disposition-template --ticket T-0700" in error


def test_schema_literal_guard_derives_tokens_from_parser_constants(pd, monkeypatch):
    monkeypatch.setattr(
        pd, "PM_REVIEW_FINDING_KEYS", (*pd.PM_REVIEW_FINDING_KEYS, "impact-probe"),
    )
    sample = "schema: " + ",".join(pd.PM_REVIEW_FINDING_KEYS)
    assert ("finding-keys", 1) in _schema_literal_findings(pd, sample)
    assert _schema_literal_findings(pd, sample.replace("impact-probe", "other")) == []


def test_adapter_skill_and_playbook_surfaces_have_no_review_schema_literals(pd):
    paths = _schema_surface_paths()
    assert paths, "schema literal guard 표면 glob이 파일을 하나도 수집하지 못함"
    violations = [
        (path.relative_to(REPO).as_posix(), family, line)
        for path in paths
        for family, line in _schema_literal_findings(
            pd, path.read_text(encoding="utf-8"),
        )
    ]
    assert not violations, (
        "parser 상수 schema가 카드/스킬/playbook에 복제됨 — 시드 골격 작성 지시 한 줄로 "
        "교체해야 함:\n"
        + "\n".join(
            f"  - {path}:{line} [{family}]"
            for path, family, line in violations
        )
    )
