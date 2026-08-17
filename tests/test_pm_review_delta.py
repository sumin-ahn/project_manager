"""T-0684 — versioned reviewer finding/disposition과 accepted-only delta."""
from __future__ import annotations

import importlib.util
import json
import contextlib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PM_DELEGATE = REPO / ".project_manager" / "tools" / "pm_delegate.py"


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_review_delta", PM_DELEGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEAL_FOR = _load_pd().seal_for


@pytest.fixture(scope="module")
def pd():
    return _load_pd()


def _review_section(payload: dict, *, pass_zero: bool = False) -> str:
    verdict = (
        "판정: 통과\n\n## must-fix\n- 없음\n"
        if pass_zero else "판정: 반려\n\n## must-fix\n- 구조화 finding 참조\n"
    )
    content = (
        "## 리뷰 (code-reviewer · 2026-08-14)\n\n"
        f"{verdict}\n```pm-review-v1\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
    digest = _SEAL_FOR(content.encode("utf-8"))
    return (
        "<!-- pm-ticket-section:start role=code-reviewer -->\n"
        + content
        + "<!-- pm-ticket-section:end role=code-reviewer -->\n"
        + f"<!-- pm-ticket-seal role=code-reviewer ordinal=0 sha256={digest} "
          "by=backfill -->\n"
    )


def _disposition(ordinal: int, rows: list[dict] | None = None, *, zero=False) -> str:
    value = (
        {"version": 1, "reviewer_ordinal": ordinal, "finding_zero": "accepted"}
        if zero else
        {"version": 1, "reviewer_ordinal": ordinal, "dispositions": rows or []}
    )
    return "\n```pm-review-disposition-v1\n" + json.dumps(
        value, ensure_ascii=False, indent=2,
    ) + "\n```\n"


def _finding(classification: str, *, finding_id="F-001", design_change=False) -> dict:
    return {
        "id": finding_id,
        "class": classification,
        "authority": "[[ADR-0001]] §경계",
        "evidence": f"{finding_id} probe rc=1",
        "recommendation": f"{finding_id}만 수정",
        "design_change": design_change,
    }


def _decision(decision: str, *, finding_id="F-001", prerequisite="") -> dict:
    return {
        "id": finding_id,
        "decision": decision,
        "reason": f"PM {decision} 근거",
        "scope": f"{finding_id} 허용 범위" if decision == "accepted" else "",
        "prerequisite": prerequisite or (
            "[[ADR-0001]] 선행 개정" if decision == "decision-required" else ""
        ),
    }


@pytest.mark.parametrize("classification", sorted((
    "implementation-defect", "spec-violation", "design-proposal",
)))
@pytest.mark.parametrize("decision", sorted((
    "accepted", "rejected", "decision-required",
)))
def test_classification_disposition_table_3x3(pd, classification, decision):
    design = classification == "design-proposal"
    ticket = _review_section({
        "version": 1,
        "findings": [_finding(classification, design_change=design)],
        "confirmations": [],
    }) + _disposition(0, [_decision(
        decision,
        prerequisite="[[ADR-0001]] 선행 개정" if design and decision == "accepted" else "",
    )])

    if decision == "decision-required":
        with pytest.raises(pd.PMReviewError, match="선행 권위 결정") as caught:
            pd.parse_pm_review_delta(ticket)
        assert caught.value.code == "decision-required"
        return
    delta = pd.parse_pm_review_delta(ticket)
    rendered = pd.render_pm_review_delta("T-0684", delta)
    if decision == "accepted":
        assert "F-001" in rendered and classification in rendered
        assert "PM accepted 근거" in rendered and "허용 범위" in rendered
    else:
        assert rendered == ""


@pytest.mark.parametrize(
    "mutator,pattern",
    [
        (lambda value: value["findings"][0].pop("authority"), "missing"),
        (lambda value: value["findings"].append(dict(value["findings"][0])), "finding ID 중복"),
        (lambda value: value.update(extra=True), "extra"),
        (lambda value: value["findings"][0].update(**{"class": "style"}), "class 미지원"),
        (lambda value: value.update(version=True), "version은 정수 1"),
    ],
)
def test_strict_review_schema_rejects_missing_duplicate_extra_and_unknown(pd, mutator, pattern):
    value = {"version": 1, "findings": [_finding("implementation-defect")], "confirmations": []}
    mutator(value)
    ticket = _review_section(value) + _disposition(0, [_decision("accepted")])
    with pytest.raises(pd.PMReviewError, match=pattern) as caught:
        pd.parse_pm_review_delta(ticket)
    assert caught.value.code == "malformed"


def _duplicate_member_tickets() -> list[pytest.param]:
    review = _review_section({
        "version": 1,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    })
    disposition = _disposition(0, [_decision("accepted")])
    confirmation = _review_section({
        "version": 1,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "resolved", "evidence": "probe rc=0"}],
    })
    return [
        pytest.param(
            review.replace(
                '  "version": 1,', '  "version": 0,\n  "version": 1,', 1,
            ) + disposition,
            id="top-level",
        ),
        pytest.param(
            review.replace(
                '      "class": "implementation-defect",',
                '      "class": "unsupported",\n      "class": "implementation-defect",',
                1,
            ) + disposition,
            id="finding",
        ),
        pytest.param(
            review + disposition + confirmation.replace(
                '      "status": "resolved",',
                '      "status": "unsupported",\n      "status": "resolved",',
                1,
            ),
            id="confirmation",
        ),
        pytest.param(
            review + disposition.replace(
                '  "reviewer_ordinal": 0,',
                '  "reviewer_ordinal": 99,\n  "reviewer_ordinal": 0,',
                1,
            ),
            id="disposition",
        ),
    ]


@pytest.mark.parametrize("ticket", _duplicate_member_tickets())
def test_json_duplicate_member_is_malformed_at_every_schema_depth(pd, ticket):
    with pytest.raises(pd.PMReviewError, match="duplicate JSON member") as caught:
        pd.parse_pm_review_delta(ticket)
    assert caught.value.code == "malformed"


def test_pending_and_decision_required_block_all_accepted_delta(pd):
    findings = [
        _finding("implementation-defect", finding_id="F-001"),
        _finding("spec-violation", finding_id="F-002"),
    ]
    pending = _review_section({"version": 1, "findings": findings, "confirmations": []})
    pending += _disposition(0, [_decision("accepted", finding_id="F-001")])
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(pending)
    assert caught.value.code == "pending"

    required = _review_section({"version": 1, "findings": findings, "confirmations": []})
    required += _disposition(0, [
        _decision("accepted", finding_id="F-001"),
        _decision("decision-required", finding_id="F-002"),
    ])
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(required)
    assert caught.value.code == "decision-required"


def test_duplicate_blocks_extra_disposition_and_finding_zero_mixture_fail_closed(pd):
    review = _review_section({
        "version": 1,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    })
    duplicate_review = review.replace(
        "<!-- pm-ticket-section:end role=code-reviewer -->",
        "```pm-review-v1\n{\"version\":1,\"findings\":[],\"confirmations\":[]}\n```\n"
        "<!-- pm-ticket-section:end role=code-reviewer -->",
    )
    with pytest.raises(pd.PMReviewError, match="review block 중복"):
        pd.parse_pm_review_delta(duplicate_review + _disposition(0, [_decision("accepted")]))

    with pytest.raises(pd.PMReviewError, match="disposition block 중복"):
        pd.parse_pm_review_delta(
            review + _disposition(0, [_decision("accepted")])
            + _disposition(0, [_decision("accepted")])
        )
    with pytest.raises(pd.PMReviewError, match="extra disposition ID"):
        pd.parse_pm_review_delta(review + _disposition(0, [
            _decision("accepted"), _decision("accepted", finding_id="F-999"),
        ]))
    with pytest.raises(pd.PMReviewError, match="finding_zero disposition 불일치"):
        pd.parse_pm_review_delta(review + _disposition(0, zero=True))


def test_accepted_only_filter_and_confirmation_state_transitions(pd):
    first = _review_section({
        "version": 1,
        "findings": [
            _finding("implementation-defect", finding_id="F-001"),
            _finding("implementation-defect", finding_id="F-002"),
        ],
        "confirmations": [],
    })
    disposition = _disposition(0, [
        _decision("accepted", finding_id="F-001"),
        _decision("rejected", finding_id="F-002"),
    ])
    one_unresolved = _review_section({
        "version": 1,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "unresolved", "evidence": "probe rc=1"}],
    })
    delta = pd.parse_pm_review_delta(first + disposition + one_unresolved)
    rendered = pd.render_pm_review_delta("T-0684", delta)
    assert "F-001" in rendered
    assert "F-002" not in rendered and "PM rejected" not in rendered

    two_unresolved = _review_section({
        "version": 1,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "regressed", "evidence": "probe rc=2"}],
    })
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(first + disposition + one_unresolved + two_unresolved)
    assert caught.value.code == "repeated-unresolved"

    resolved = _review_section({
        "version": 1,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "resolved", "evidence": "probe rc=0"}],
    })
    delta = pd.parse_pm_review_delta(first + disposition + one_unresolved + resolved)
    assert pd.render_pm_review_delta("T-0684", delta) == ""

    rejected_confirmation = _review_section({
        "version": 1,
        "findings": [],
        "confirmations": [{"id": "F-002", "status": "resolved", "evidence": "재등장"}],
    })
    with pytest.raises(pd.PMReviewError, match="rejected finding ID"):
        pd.parse_pm_review_delta(first + disposition + rejected_confirmation)


def test_indented_or_overlong_review_fence_cannot_hide_a_duplicate(pd):
    ticket = _review_section({
        "version": 1,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }) + _disposition(0, [_decision("accepted")])
    hidden = "\n  ```pm-review-v1\n{}\n```\n"
    with pytest.raises(pd.PMReviewError, match="손상된 review fence"):
        pd.parse_pm_review_delta(ticket + hidden)
    hidden = "\n````pm-review-v1\n{}\n````\n"
    with pytest.raises(pd.PMReviewError, match="손상된 review fence"):
        pd.parse_pm_review_delta(ticket + hidden)


def test_finding_zero_requires_cross_checked_pass_and_compact_pm_acceptance(pd):
    zero = _review_section(
        {"version": 1, "findings": [], "confirmations": []}, pass_zero=True,
    )
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(zero)
    assert caught.value.code == "pending"
    delta = pd.parse_pm_review_delta(zero + _disposition(0, zero=True))
    assert delta.finding_zero is True
    assert pd.render_pm_review_delta("T-0684", delta) == ""


def test_design_acceptance_without_authority_reference_is_decision_required(pd):
    ticket = _review_section({
        "version": 1,
        "findings": [_finding("design-proposal", design_change=True)],
        "confirmations": [],
    }) + _disposition(0, [_decision("accepted", prerequisite="plain prose")])
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(ticket)
    assert caught.value.code == "decision-required"


def test_internal_round_projection_prefers_structured_ids_and_hashes_legacy(pd):
    structured = _review_section({
        "version": 1,
        "findings": [_finding("implementation-defect", finding_id="F-101")],
        "confirmations": [],
    })
    assert pd._internal_projected_finding_ids(structured, ["legacy prose"]) == ["F-101"]
    first = pd._internal_projected_finding_ids("old reply", ["same legacy item"])
    second = pd._internal_projected_finding_ids("old reply", ["same legacy item"])
    assert first == second and first[0].startswith("LEGACY-")


def test_review_delta_cli_dispatch_renders_success_and_prescribes_pending(
        pd, monkeypatch, tmp_path, capsys):
    ticket_path = tmp_path / "tickets" / "claimed" / "T-0684-review.md"
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text(_review_section({
        "version": 1,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }) + _disposition(0, [_decision("accepted")]), encoding="utf-8")
    # 봉인 도입 이후 형상 — 성장 절의 봉인이 장부에 기재된 보드(T-0699).
    pd.append_ticket_growth_records(
        pd.ticket_growth_dir_for_ticket_path(ticket_path), "T-0684",
        ticket_path.read_text(encoding="utf-8"), by="backfill", stamp=False,
    )

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
    assert pd.main(["review", "delta", "--ticket", "T-0684"]) == 0
    output = capsys.readouterr().out
    assert "F-001" in output and "허용 수정 범위" in output

    ticket_path.write_text(_review_section({
        "version": 1,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }), encoding="utf-8")
    assert pd.main(["review", "delta", "--ticket", "T-0684"]) == 1
    error = capsys.readouterr().err
    assert "[pending]" in error and "전수 disposition" in error
