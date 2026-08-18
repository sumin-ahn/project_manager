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


def _disposition_block(rows: list[dict], *, ordinal: int = 1) -> str:
    payload = {"version": 1, "reviewer_ordinal": ordinal, "dispositions": rows}
    return (
        "```pm-review-disposition-v1\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _round(pd, ordinal: int, text: str, role: str = "code-reviewer"):
    """라운드 파일 하나 — 순번·역할은 파일 이름이 단일 진실이다([[T-0749]])."""
    rounds_module = pd._load_ticket_rounds()
    return rounds_module.Round(
        ordinal=ordinal, role=role,
        path=Path(rounds_module.round_filename(ordinal, role)),
        text=text, pending=False,
    )


def _reviewer_round_text(pd, payload: dict) -> str:
    return (
        "## 리뷰 (code-reviewer · 2026-08-17)\n\n"
        "## must-fix\n- 구조화 finding 참조\n\n"
        "## should-fix\n- 없음\n\n"
        "## suggestion\n- 없음\n\n"
        "## 판정\n판정: 반려\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _reviewer_round(pd, payload: dict, *, ordinal: int = 1):
    return _round(pd, ordinal, _reviewer_round_text(pd, payload))


def _seeded_round_text(pd, role: str, ticket_text: str = "", previous=None) -> str:
    rounds_module = pd._load_ticket_rounds()
    return rounds_module.render_round_seed(
        role, ticket_text, today="2026-08-17", previous_round=previous,
    )


def _legacy_reviewer_round(pd, ordinal: int):
    """versioned block 도입 전 산출 — 프리필이 해소할 수 없는 직전 라운드."""
    return _round(pd, ordinal, (
        "## 리뷰 (code-reviewer · 2026-08-17)\n\n"
        "판정: 통과\n\n## must-fix\n- 없음\n\n"
        f"legacy reviewer ordinal {ordinal}\n"
    ))


def test_every_round_role_seed_comes_from_the_delegate_renderer(pd, board):
    """모든 역할의 라운드 시드는 한 렌더러에서 나온다 — 역할 집합의 권위도 하나다."""
    rounds_module = pd._load_ticket_rounds()
    # 라운드 역할 집합의 단일 권위는 seam 이고 board 는 사람용 절명만 소유한다.
    assert set(board.ticket_round_role_labels()) == set(pd.TICKET_COPY_ROLES)
    assert set(rounds_module.ROLES) == set(pd.TICKET_COPY_ROLES)
    assert board.ticket_round_role_labels() == rounds_module.ROLE_LABELS

    text = "".join(
        _seeded_round_text(pd, role) for role in sorted(pd.TICKET_COPY_ROLES)
    )
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


def _seed_payload(rendered: str) -> dict:
    return json.loads(rendered.split("```pm-review-v1\n", 1)[1].split("\n```", 1)[0])


def test_confirmation_round_prefills_every_id_from_previous_round_file(pd):
    """확인 대상 프리필의 입력은 같은 역할의 **직전 라운드 파일**이다([[T-0749]] F-007)."""
    previous = _reviewer_round(pd, _review_payload("F-003", "F-001"))
    rendered = pd.render_ticket_growth_section_seed(
        "code-reviewer", "", previous_round=(previous.ordinal, previous.text),
    )
    payload = _seed_payload(rendered)
    assert [row["id"] for row in payload["confirmations"]] == ["F-003", "F-001"]
    assert all(row["status"] == "<resolved|unresolved|regressed>"
               for row in payload["confirmations"])


def test_confirmation_seed_excludes_previous_rejected_findings(pd):
    """PM `rejected` 배제 입력은 명세의 판정 블록이다 — 라운드 파일 밖이다."""
    previous = _reviewer_round(pd, _review_payload("F-001", "F-002"))
    spec = _disposition_block([
        _decision("F-001", "accepted"),
        _decision("F-002", "rejected"),
    ], ordinal=previous.ordinal)
    rendered = pd.render_ticket_growth_section_seed(
        "code-reviewer", spec, previous_round=(previous.ordinal, previous.text),
    )
    payload = _seed_payload(rendered)
    assert [row["id"] for row in payload["confirmations"]] == ["F-001"]


def test_first_round_seed_has_no_previous_input(pd):
    rendered = pd.render_ticket_growth_section_seed("code-reviewer", "")
    payload = _seed_payload(rendered)
    assert [row["id"] for row in payload["confirmations"]] == ["F-NNN"]


def test_unedited_seed_is_malformed_review_output(pd):
    """시드 그대로인 라운드는 판정 표면에 올라도 malformed 다(산출이 없다)."""
    seed = _seeded_round_text(pd, "code-reviewer")
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta("", [_round(pd, 1, seed)])
    assert caught.value.code == "malformed"


def test_seed_round_is_judged_pending_by_the_rounds_seam(pd):
    """'산출 없음' 판정은 라운드 seam 이 소유한다 — 회수는 그 판정만 본다."""
    rounds_module = pd._load_ticket_rounds()
    for role in ("developer", "code-reviewer", "external-reviewer"):
        seed = _seeded_round_text(pd, role)
        item = rounds_module.Round(
            ordinal=1, role=role,
            path=Path(rounds_module.round_filename(1, role)),
            text=seed, pending=False,
        )
        assert rounds_module.round_is_pending(item) is True
        edited = rounds_module.Round(*item[:3], item.text + "\n실산출\n", False)
        assert rounds_module.round_is_pending(edited) is False


def test_pending_judgment_reads_only_the_round_body(pd):
    """판정 입력은 라운드 본문 하나다 — 프리필 ID·날짜·명세는 판정을 바꾸지 않는다."""
    previous = _reviewer_round(pd, _review_payload("F-003", "F-001"))
    prefilled = _seeded_round_text(
        pd, "code-reviewer", previous=(previous.ordinal, previous.text),
    )
    assert '"id":"F-003"' in prefilled.replace(" ", "")

    # 같은 골격을 서로 다른 날짜·명세로 지어도 판정은 같다(자리표시자 = 산출 없음).
    assert pd.ticket_round_body_is_pending(
        "code-reviewer", prefilled.partition("\n")[2].lstrip("\n"),
    ) is True
    for text in (prefilled, _seeded_round_text(pd, "code-reviewer")):
        assert pd._load_ticket_rounds().round_is_pending(
            _round(pd, 2, text),
        ) is True

    # 자리표시자를 실값으로 바꾸면 산출이다(ID 만 자유롭다).
    filled = prefilled.replace("<resolved|unresolved|regressed>", "resolved")
    assert pd._load_ticket_rounds().round_is_pending(_round(pd, 2, filled)) is False


def test_legacy_previous_round_prefill_degrades_to_placeholder(pd, capsys):
    """직전 라운드가 versioned block 이전 산출이면 자리표시자로 강등하고 loud 하게 알린다."""
    previous = _legacy_reviewer_round(pd, 1)
    rendered = pd.render_ticket_growth_section_seed(
        "code-reviewer", "", previous_round=(previous.ordinal, previous.text),
    )
    payload = _seed_payload(rendered)
    assert [row["id"] for row in payload["confirmations"]] == ["F-NNN"]
    assert "F-NNN 골격으로 강등" in capsys.readouterr().err


def test_refused_previous_round_is_not_a_prefill_source(pd):
    """회수 거부 표식이 있는 라운드는 판정 표면 밖이라 확인 대상을 공급하지 않는다."""
    refused_text = (
        "## 추가 리뷰 (external-reviewer · 2026-08-17)\n\n"
        + pd.EXTERNAL_REVIEW_REFUSED_LINE + "\n\n"
        + _reviewer_round_text(pd, _review_payload("X-001")).partition("\n")[2]
    )
    rendered = pd.render_ticket_growth_section_seed(
        "external-reviewer", "", previous_round=(1, refused_text),
    )
    payload = _seed_payload(rendered)
    assert [row["id"] for row in payload["confirmations"]] == ["X-NNN"]


def _materialize(pd, spec: str, rounds, tickets_dir: Path, ticket_id: str) -> Path:
    """명세 파일 하나 + `rounds/<id>/NN-<role>.md` 를 실제로 깐다(CLI 경로 입력)."""
    rounds_module = pd._load_ticket_rounds()
    spec_path = tickets_dir / "claimed" / f"{ticket_id}-review.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec, encoding="utf-8", newline="")
    rounds_dir = rounds_module.rounds_dir_for_ticket(ticket_id, tickets_dir)
    rounds_dir.mkdir(parents=True, exist_ok=True)
    for item in rounds:
        (rounds_dir / rounds_module.round_filename(item.ordinal, item.role)).write_text(
            item.text, encoding="utf-8", newline="",
        )
    return spec_path


def _fake_board(spec_path: Path, tickets_dir: Path):
    class FakeBoard:
        @staticmethod
        @contextlib.contextmanager
        def board_lock():
            yield

        @staticmethod
        def find_ticket_exact(_ticket):
            return "claimed", spec_path

        @staticmethod
        def tickets_dir():
            return tickets_dir

    return FakeBoard


def test_disposition_template_prefills_all_pending_ids_and_filled_block_passes(pd):
    reviewer = _reviewer_round(pd, _review_payload("F-002", "F-001"))
    rendered = pd.render_pm_review_disposition_template("", [reviewer])
    payload = json.loads(
        rendered.split("```pm-review-disposition-v1\n", 1)[1].split("\n```", 1)[0]
    )
    assert payload["reviewer_ordinal"] == 1
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
    delta = pd.parse_pm_review_delta(filled, [reviewer])
    assert [finding.id for finding, _decision in delta.accepted] == ["F-002", "F-001"]


def test_disposition_template_preserves_existing_rows_and_replaces_partial_block(pd):
    reviewer = _reviewer_round(pd, _review_payload("F-001", "F-002"))
    existing = _decision("F-001", "rejected")
    spec = _disposition_block([existing])
    rendered = pd.render_pm_review_disposition_template(spec, [reviewer])
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
    delta = pd.parse_pm_review_delta(replacement, [reviewer])
    assert [finding.id for finding, _row in delta.accepted] == ["F-002"]


def test_disposition_template_rejects_confirmation_only_round(
    pd, tmp_path, monkeypatch, capsys,
):
    first = _reviewer_round(pd, _review_payload("F-001"), ordinal=1)
    second = _reviewer_round(pd, {
        "version": 1,
        "findings": [],
        "confirmations": [{
            "id": "F-001", "status": "resolved", "evidence": "회귀 통과",
        }],
    }, ordinal=2)
    with pytest.raises(pd.DelegateError, match="confirmation-only.*신규 finding이 없습니다"):
        pd.render_pm_review_disposition_template("", [first, second], 2)

    tickets_dir = tmp_path / "tickets"
    spec_path = _materialize(pd, "", [first, second], tickets_dir, "T-0700")
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(
        pd, "_load_board_for_repo",
        lambda _owner: _fake_board(spec_path, tickets_dir),
    )
    assert pd.main([
        "review", "disposition-template", "--ticket", "T-0700", "--ordinal", "2",
    ]) == 1
    assert "confirmation-only" in capsys.readouterr().err


def test_disposition_template_defaults_latest_and_honors_explicit_ordinal(pd):
    rounds = [
        _reviewer_round(pd, _review_payload("F-001"), ordinal=1),
        _reviewer_round(pd, _review_payload("F-002"), ordinal=2),
    ]
    latest = pd.render_pm_review_disposition_template("", rounds)
    first = pd.render_pm_review_disposition_template("", rounds, 1)
    assert '"reviewer_ordinal":2' in latest and '"id":"F-002"' in latest
    assert '"reviewer_ordinal":1' in first and '"id":"F-001"' in first


def test_disposition_template_cli_and_pending_error_prescribe_same_command(
    pd, tmp_path, monkeypatch, capsys,
):
    tickets_dir = tmp_path / "tickets"
    spec_path = _materialize(
        pd, "", [_reviewer_round(pd, _review_payload("F-007"))], tickets_dir, "T-0700",
    )
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(
        pd, "_load_board_for_repo",
        lambda _owner: _fake_board(spec_path, tickets_dir),
    )
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
