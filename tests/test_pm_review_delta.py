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


# 리뷰 블록 세대는 엔진 상수에서 읽는다 — 승격이 있으면 픽스처가 자동으로 따라간다.
BLOCK_VERSION = _load_pd().PM_REVIEW_VERSION
LEGACY_BLOCK_VERSION = _load_pd().PM_REVIEW_LEGACY_VERSION
DISPOSITION_VERSION = _load_pd().PM_REVIEW_DISPOSITION_VERSION


@pytest.fixture(scope="module")
def pd():
    return _load_pd()


class _Ticket:
    """명세(PM 판정 블록의 자리) + 라운드 목록 — 판정 입력 한 쌍을 조립한다.

    라운드 순번은 **조립 순서**로 1..N 이 붙는다([[ADR-0090]] 티켓 전역 순번). `+` 는 옛
    단일 파일 픽스처의 표기를 그대로 유지하려고 남긴 것이고, 두 축(명세/라운드)은 섞이지
    않는다 — 문자열을 더하면 명세에, `_Ticket` 을 더하면 각 축에 붙는다.
    """

    def __init__(self, spec: str = "", round_texts: tuple[str, ...] = ()):
        self.spec = spec
        self.round_texts = tuple(round_texts)

    def __add__(self, other):
        if isinstance(other, _Ticket):
            return _Ticket(
                self.spec + other.spec, self.round_texts + other.round_texts,
            )
        return _Ticket(self.spec + other, self.round_texts)

    def __radd__(self, other):
        return _Ticket(other + self.spec, self.round_texts)

    def replace(self, old: str, new: str, count: int = -1) -> "_Ticket":
        return _Ticket(
            self.spec.replace(old, new, count),
            tuple(text.replace(old, new, count) for text in self.round_texts),
        )

    def rounds(self, pd):
        return [
            _round(pd, ordinal, text)
            for ordinal, text in enumerate(self.round_texts, 1)
        ]


def _round(pd, ordinal: int, text: str, role: str = "code-reviewer", *, pending: bool = False):
    rounds_module = pd._load_ticket_rounds()
    return rounds_module.Round(
        ordinal=ordinal, role=role,
        path=Path(rounds_module.round_filename(ordinal, role)),
        text=text, pending=pending,
    )


def _delta(pd, ticket: _Ticket):
    return pd.parse_pm_review_delta(ticket.spec, ticket.rounds(pd))


def _review_section(payload: dict, *, pass_zero: bool = False) -> _Ticket:
    verdict = (
        "판정: 통과\n\n## must-fix\n- 없음\n"
        if pass_zero else "판정: 반려\n\n## must-fix\n- 구조화 finding 참조\n"
    )
    return _Ticket(round_texts=(
        "## 리뷰 (code-reviewer · 2026-08-14)\n\n"
        f"{verdict}\n```pm-review-v1\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```\n",
    ))


def _disposition(ordinal: int, rows: list[dict] | None = None, *, zero=False) -> _Ticket:
    value = (
        {"version": DISPOSITION_VERSION, "reviewer_ordinal": ordinal,
         "finding_zero": "accepted"}
        if zero else
        {"version": DISPOSITION_VERSION, "reviewer_ordinal": ordinal,
         "dispositions": rows or []}
    )
    return _Ticket("\n```pm-review-disposition-v1\n" + json.dumps(
        value, ensure_ascii=False, indent=2,
    ) + "\n```\n")


def _finding(
    classification: str, *, finding_id="F-001", design_change=False,
    severity="must-fix",
) -> dict:
    return {
        "id": finding_id,
        "class": classification,
        "severity": severity,
        "authority": "[[ADR-0001]] §경계",
        "evidence": f"{finding_id} probe rc=1",
        "recommendation": f"{finding_id}만 수정",
        "fix_contract": {
            "location": "src/example.py:1", "failure": "probe rc=1",
            "design": f"{finding_id} 결함만 수정", "test": f"{finding_id} 회귀",
            "command": "python3 --version", "expected": "Python",
        },
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
        "version": BLOCK_VERSION,
        "findings": [_finding(classification, design_change=design)],
        "confirmations": [],
    }) + _disposition(1, [_decision(
        decision,
        prerequisite="[[ADR-0001]] 선행 개정" if design and decision == "accepted" else "",
    )])

    if decision == "decision-required":
        with pytest.raises(pd.PMReviewError, match="선행 권위 결정") as caught:
            _delta(pd, ticket)
        assert caught.value.code == "decision-required"
        return
    delta = _delta(pd, ticket)
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
        (lambda value: value["findings"][0].pop("fix_contract"), "fix_contract"),
        (lambda value: value["findings"].append(dict(value["findings"][0])), "finding ID 중복"),
        (lambda value: value.update(extra=True), "extra"),
        (lambda value: value["findings"][0].update(**{"class": "style"}), "class 미지원"),
        (lambda value: value.update(version=True), "version은 정수 1"),
    ],
)
def test_strict_review_schema_rejects_missing_duplicate_extra_and_unknown(pd, mutator, pattern):
    value = {
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }
    mutator(value)
    ticket = _review_section(value) + _disposition(1, [_decision("accepted")])
    with pytest.raises(pd.PMReviewError, match=pattern) as caught:
        _delta(pd, ticket)
    assert caught.value.code == "malformed"


@pytest.mark.parametrize("field", (
    "location", "failure", "design", "test", "command", "expected",
))
def test_v3_fix_contract_rejects_placeholder_in_every_string_field(pd, field):
    finding = _finding("implementation-defect")
    finding["fix_contract"][field] = "prefix <placeholder>"
    ticket = _review_section({
        "version": BLOCK_VERSION,
        "findings": [finding],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted")])

    with pytest.raises(pd.PMReviewError, match="placeholder") as caught:
        _delta(pd, ticket)
    assert caught.value.code == "malformed"


def test_declared_contract_placeholder_words_are_all_parser_inputs(pd):
    """prompt가 렌더할 공개 금지어 집합과 parser의 실제 거부 집합은 하나다."""
    for word in pd.PM_REVIEW_CONTRACT_PLACEHOLDER_WORDS:
        assert pd._CONTRACT_PLACEHOLDER_RE.search(word)


def test_contract_test_targets_strip_korean_particles_and_punctuation(pd):
    original_f001 = (
        "tests/test_pm_review_delta.py에 v3 자리표시자 거부 케이스를, "
        "tests/test_round_budget.py에 diff 결속 회귀를 추가한다."
    )
    assert pd._contract_test_targets(
        original_f001, "reviewer 추가 회귀 F-001.test",
    ) == (
        "tests/test_pm_review_delta.py",
        "tests/test_round_budget.py",
    )
    assert pd._contract_test_targets(
        "tests/test_a.py와 tests/test_b.py과 tests/test_c.py, tests/test_d.py.",
        "reviewer 추가 회귀 F-002.test",
    ) == (
        "tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py",
    )


@pytest.mark.parametrize("value", (
    "src/test_wrong.py에 회귀를 추가한다",
    "tests/../test_escape.py에 회귀를 추가한다",
    "tests/test_joined.py임의문자를 허용하지 않는다",
))
def test_contract_test_targets_reject_non_test_traversal_and_unknown_suffix(pd, value):
    with pytest.raises(pd.DelegateError, match="repo-relative"):
        pd._contract_test_targets(value, "reviewer 추가 회귀 F-003.test")


def _duplicate_member_tickets() -> list[pytest.param]:
    review = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    })
    disposition = _disposition(1, [_decision("accepted")])
    confirmation = _review_section({
        "version": BLOCK_VERSION,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "resolved", "evidence": "probe rc=0"}],
    })
    return [
        pytest.param(
            review.replace(
                f'  "version": {BLOCK_VERSION},',
                f'  "version": 0,\n  "version": {BLOCK_VERSION},', 1,
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
                '  "reviewer_ordinal": 1,',
                '  "reviewer_ordinal": 99,\n  "reviewer_ordinal": 1,',
                1,
            ),
            id="disposition",
        ),
    ]


@pytest.mark.parametrize(
    "mutator,pattern",
    [
        (lambda value: value["findings"][0].pop("severity"), "missing=\\['severity'\\]"),
        (lambda value: value["findings"][0].update(severity="blocker"),
         "severity 미지원"),
        (lambda value: value["findings"][0].update(severity=""),
         "severity는 비어 있지 않은 문자열"),
    ],
    ids=("absent", "unsupported-value", "empty"),
)
def test_severity_is_required_with_a_closed_value_set(pd, mutator, pattern):
    """심각도는 블록의 단일 진실이라 부재·허용 밖 값·빈 값을 모두 malformed 로 닫는다."""
    value = {
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }
    mutator(value)
    ticket = _review_section(value) + _disposition(1, [_decision("accepted")])
    with pytest.raises(pd.PMReviewError, match=pattern) as caught:
        _delta(pd, ticket)
    assert caught.value.code == "malformed"


def _without_severity(finding: dict) -> dict:
    return {key: value for key, value in finding.items() if key != "severity"}


def _legacy_finding(finding: dict, *, keep_severity: bool) -> dict:
    """v1 행은 v3의 fix_contract를 알지 못한다."""
    omitted = {"fix_contract"} if keep_severity else {"fix_contract", "severity"}
    return {key: value for key, value in finding.items() if key not in omitted}


def test_severity_boundary_is_the_block_generation_not_the_ticket_state(pd):
    """severity 요구의 경계는 **블록 payload 세대**다 — 티켓 상태·ordinal 이 아니다.

    이미 봉인된 v1 블록(진행 중 티켓의 옛 라운드)은 부재를 그대로 읽어 '미기재'로 표기하고,
    현행 세대 블록은 부재를 거부한다. 두 세대가 한 티켓에 섞여 있어도 각자 규칙을 받는다.
    """
    legacy = _review_section({
        "version": LEGACY_BLOCK_VERSION,
        "findings": [_legacy_finding(
            _finding("implementation-defect", finding_id="F-001"), keep_severity=False,
        )],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted", finding_id="F-001")])
    delta = _delta(pd, legacy)
    rendered = pd.render_pm_review_delta("T-0696", delta)
    assert f"- 심각도: {pd.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}" in rendered

    current_without_severity = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_without_severity(
            _finding("implementation-defect", finding_id="F-002")
        )],
        "confirmations": [],
    })
    with pytest.raises(pd.PMReviewError, match="missing=\\['severity'\\]") as caught:
        _delta(pd, current_without_severity)
    assert caught.value.code == "malformed"


def test_legacy_block_may_also_include_severity_from_the_transition_window(pd):
    """전환기 v1 산출이 severity 를 실었어도 읽는다(두 key 집합 중 하나와 정확 일치)."""
    ticket = _review_section({
        "version": LEGACY_BLOCK_VERSION,
        "findings": [_legacy_finding(
            _finding("implementation-defect", severity="should-fix"), keep_severity=True,
        )],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted")])
    delta = _delta(pd, ticket)
    assert "- 심각도: should-fix" in pd.render_pm_review_delta("T-0696", delta)


def test_delta_render_exposes_severity_and_channel(pd):
    """dev 는 산문을 다시 읽지 않고 delta 렌더에서 우선순위와 채널을 안다."""
    ticket = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect", severity="should-fix")],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted")])
    rendered = pd.render_pm_review_delta("T-0696", _delta(pd, ticket))
    assert "- 심각도: should-fix" in rendered
    assert f"- 채널: {pd.INTERNAL_REVIEW_ROLE}" in rendered


@pytest.mark.parametrize("ticket", _duplicate_member_tickets())
def test_json_duplicate_member_is_malformed_at_every_schema_depth(pd, ticket):
    with pytest.raises(pd.PMReviewError, match="duplicate JSON member") as caught:
        _delta(pd, ticket)
    assert caught.value.code == "malformed"


def test_pending_and_decision_required_block_all_accepted_delta(pd):
    findings = [
        _finding("implementation-defect", finding_id="F-001"),
        _finding("spec-violation", finding_id="F-002"),
    ]
    pending = _review_section(
        {"version": BLOCK_VERSION, "findings": findings, "confirmations": []}
    )
    pending += _disposition(1, [_decision("accepted", finding_id="F-001")])
    with pytest.raises(pd.PMReviewError) as caught:
        _delta(pd, pending)
    assert caught.value.code == "pending"

    required = _review_section(
        {"version": BLOCK_VERSION, "findings": findings, "confirmations": []}
    )
    required += _disposition(1, [
        _decision("accepted", finding_id="F-001"),
        _decision("decision-required", finding_id="F-002"),
    ])
    with pytest.raises(pd.PMReviewError) as caught:
        _delta(pd, required)
    assert caught.value.code == "decision-required"


def test_duplicate_blocks_extra_disposition_and_finding_zero_mixture_fail_closed(pd):
    review = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    })
    duplicate_review = _Ticket(round_texts=(
        review.round_texts[0]
        + "```pm-review-v1\n{\"version\":1,\"findings\":[],\"confirmations\":[]}\n```\n",
    ))
    with pytest.raises(pd.PMReviewError, match="review block 중복"):
        _delta(pd, duplicate_review + _disposition(1, [_decision("accepted")]))

    with pytest.raises(pd.PMReviewError, match="disposition block 중복"):
        _delta(pd, review + _disposition(1, [_decision("accepted")])
            + _disposition(1, [_decision("accepted")])
        )
    with pytest.raises(pd.PMReviewError, match="extra disposition ID"):
        _delta(pd, review + _disposition(1, [
            _decision("accepted"), _decision("accepted", finding_id="F-999"),
        ]))
    with pytest.raises(pd.PMReviewError, match="finding_zero disposition 불일치"):
        _delta(pd, review + _disposition(1, zero=True))


def test_accepted_only_filter_and_confirmation_state_transitions(pd):
    first = _review_section({
        "version": BLOCK_VERSION,
        "findings": [
            _finding("implementation-defect", finding_id="F-001"),
            _finding("implementation-defect", finding_id="F-002"),
        ],
        "confirmations": [],
    })
    disposition = _disposition(1, [
        _decision("accepted", finding_id="F-001"),
        _decision("rejected", finding_id="F-002"),
    ])
    one_unresolved = _review_section({
        "version": BLOCK_VERSION,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "unresolved", "evidence": "probe rc=1"}],
    })
    delta = _delta(pd, first + disposition + one_unresolved)
    rendered = pd.render_pm_review_delta("T-0684", delta)
    assert "F-001" in rendered
    assert "F-002" not in rendered and "PM rejected" not in rendered

    two_unresolved = _review_section({
        "version": BLOCK_VERSION,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "regressed", "evidence": "probe rc=2"}],
    })
    with pytest.raises(pd.PMReviewError) as caught:
        _delta(pd, first + disposition + one_unresolved + two_unresolved)
    assert caught.value.code == "repeated-unresolved"

    resolved = _review_section({
        "version": BLOCK_VERSION,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "resolved", "evidence": "probe rc=0"}],
    })
    delta = _delta(pd, first + disposition + one_unresolved + resolved)
    assert pd.render_pm_review_delta("T-0684", delta) == ""

    rejected_confirmation = _review_section({
        "version": BLOCK_VERSION,
        "findings": [],
        "confirmations": [{"id": "F-002", "status": "resolved", "evidence": "재등장"}],
    })
    with pytest.raises(pd.PMReviewError, match="rejected finding ID"):
        _delta(pd, first + disposition + rejected_confirmation)


def test_indented_or_overlong_review_fence_cannot_hide_a_duplicate(pd):
    ticket = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted")])
    hidden = "\n  ```pm-review-v1\n{}\n```\n"
    with pytest.raises(pd.PMReviewError, match="손상된 review fence"):
        _delta(pd, ticket + hidden)
    hidden = "\n````pm-review-v1\n{}\n````\n"
    with pytest.raises(pd.PMReviewError, match="손상된 review fence"):
        _delta(pd, ticket + hidden)


def test_finding_zero_requires_cross_checked_pass_and_compact_pm_acceptance(pd):
    zero = _review_section(
        {"version": BLOCK_VERSION, "findings": [], "confirmations": []}, pass_zero=True,
    )
    with pytest.raises(pd.PMReviewError) as caught:
        _delta(pd, zero)
    assert caught.value.code == "pending"
    delta = _delta(pd, zero + _disposition(1, zero=True))
    assert delta.finding_zero is True
    assert pd.render_pm_review_delta("T-0684", delta) == ""


def test_design_acceptance_without_authority_reference_is_decision_required(pd):
    ticket = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("design-proposal", design_change=True)],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted", prerequisite="plain prose")])
    with pytest.raises(pd.PMReviewError) as caught:
        _delta(pd, ticket)
    assert caught.value.code == "decision-required"


def test_internal_round_projection_prefers_structured_ids_and_hashes_legacy(pd):
    structured = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect", finding_id="F-101")],
        "confirmations": [],
    })
    assert pd._internal_projected_finding_ids(
        structured.round_texts[0], ["legacy prose"],
    ) == ["F-101"]
    first = pd._internal_projected_finding_ids("old reply", ["same legacy item"])
    second = pd._internal_projected_finding_ids("old reply", ["same legacy item"])
    assert first == second and first[0].startswith("LEGACY-")


def _materialize(pd, ticket: _Ticket, tickets_dir: Path, ticket_id: str) -> Path:
    """명세 파일 하나 + `rounds/<id>/NN-<role>.md` 로 라운드를 실제로 깐다."""
    rounds_module = pd._load_ticket_rounds()
    spec_path = tickets_dir / "claimed" / f"{ticket_id}-review.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(ticket.spec, encoding="utf-8", newline="")
    rounds_dir = rounds_module.rounds_dir_for_ticket(ticket_id, tickets_dir)
    if rounds_dir.exists():
        for stale in rounds_dir.iterdir():
            stale.unlink()
    rounds_dir.mkdir(parents=True, exist_ok=True)
    for ordinal, text in enumerate(ticket.round_texts, 1):
        (rounds_dir / rounds_module.round_filename(ordinal, "code-reviewer")).write_text(
            text, encoding="utf-8", newline="",
        )
    return spec_path


def test_review_delta_cli_dispatch_renders_success_and_prescribes_pending(
        pd, monkeypatch, tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    accepted = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted")])
    ticket_path = _materialize(pd, accepted, tickets_dir, "T-0684")

    class FakeBoard:
        @staticmethod
        @contextlib.contextmanager
        def board_lock():
            yield

        @staticmethod
        def find_ticket_exact(_ticket):
            return "claimed", ticket_path

        @staticmethod
        def tickets_dir():
            return tickets_dir

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: FakeBoard())
    assert pd.main(["review", "delta", "--ticket", "T-0684"]) == 0
    output = capsys.readouterr().out
    assert "F-001" in output and "허용 수정 범위" in output

    pending = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    })
    _materialize(pd, pending, tickets_dir, "T-0684")
    assert pd.main(["review", "delta", "--ticket", "T-0684"]) == 1
    error = capsys.readouterr().err
    assert "[pending]" in error and "전수 disposition" in error


def test_machine_confirmation_merges_into_the_same_confirmation_history(pd):
    """T-0786 — PM 기계 확인은 reviewer 확인과 같은 확인 이력에 합류한다(불변식 6).

    dev verify 골격(`pm-review-verify-v1`)과 PM 기계 확인(`pm-review-confirmation-v1`)은
    이 파일이 다루는 `parse_pm_review_delta` 의 병합 축에 새로 얹힌 입력이다 — 별도
    reviewer 재투입 없이 accepted 가 비는지가 이 병합의 전부다.
    """
    review = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted")])
    dev_round = _round(pd, 2, (
        "## 구현 보충 (developer · 2026-08-21)\n\n## 변경 파일\n- x\n\n"
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps({
            "version": pd.PM_REVIEW_VERIFY_VERSION,
            "verifications": [{
                "id": "F-001", "machine_verifiable": True, "command": "echo hi",
                "expected": "hi", "before": "bye", "reason": "",
            }],
        })
        + "\n```\n"
    ), role="developer")
    confirmation = _Ticket(f"```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n" + json.dumps({
        "version": pd.PM_REVIEW_MACHINE_CONFIRMATION_VERSION, "round": 2,
        "confirmations": [{
            "id": "F-001", "status": "resolved", "command": "echo hi", "observed": "hi",
        }],
    }) + "\n```\n")

    rounds = review.rounds(pd) + [dev_round]
    delta = pd.parse_pm_review_delta((review + confirmation).spec, rounds)
    assert delta.accepted == ()


def test_review_delta_cli_blocks_on_round_gap(pd, monkeypatch, tmp_path, capsys):
    """순번 빈틈(삭제 의심)은 판정 전에 막는다 — [[ADR-0090]] 3.8 의 유일한 red 축."""
    rounds_module = pd._load_ticket_rounds()
    tickets_dir = tmp_path / "tickets"
    ticket = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }) + _disposition(1, [_decision("accepted")])
    ticket_path = _materialize(pd, ticket, tickets_dir, "T-0685")
    rounds_dir = rounds_module.rounds_dir_for_ticket("T-0685", tickets_dir)
    (rounds_dir / "01-code-reviewer.md").rename(rounds_dir / "02-code-reviewer.md")

    class FakeBoard:
        @staticmethod
        @contextlib.contextmanager
        def board_lock():
            yield

        @staticmethod
        def find_ticket_exact(_ticket):
            return "claimed", ticket_path

        @staticmethod
        def tickets_dir():
            return tickets_dir

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: FakeBoard())
    assert pd.main(["review", "delta", "--ticket", "T-0685"]) == 1
    error = capsys.readouterr().err
    assert "round-gap" in error and "빠진 순번" in error


# ── 시드 그대로인(pending) 리뷰 라운드는 판정 표면 밖 ────────────────────

def _pending_seed_text(pd, role: str, spec_text: str, previous_round: tuple[int, str]) -> str:
    """엔진이 렌더한 시드 본문 그대로 — 헤더는 직전 라운드 첫 줄을 재사용해 손으로 다시 짓지 않는다."""
    previous_header = previous_round[1].splitlines()[0]
    body = pd.render_ticket_growth_section_seed(role, spec_text, previous_round=previous_round)
    return f"{previous_header}\n\n{body}"


def test_pending_seed_review_round_is_excluded_from_the_judgment_surface(pd):
    """kill·미회수로 시드 그대로 남은 다음 라운드는 판정 표면에 오르지 않는다.

    시드 골격의 자리표시 finding(`class` 값이 `<...>` 그대로)이 실 선언으로 읽히면
    `parse_pm_review_delta` 가 malformed 로 막힌다 — 표면 함수뿐 아니라, 그 함수를 거치지
    않고 라운드를 직접 순회해 reviewer block 을 모으는 내부 구간도 같은 배제가 있어야
    막히지 않는다.
    """
    round1 = _round(pd, 1, _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }).round_texts[0])
    spec = _disposition(1, [_decision("accepted")]).spec
    round2 = _round(
        pd, 2, _pending_seed_text(pd, "code-reviewer", spec, (1, round1.text)), pending=True,
    )

    assert pd._pm_review_surface_rounds([round1, round2]) == [round1]
    delta = pd.parse_pm_review_delta(spec, [round1, round2])
    assert [finding.id for finding, _disposition in delta.accepted] == ["F-001"]


def test_pending_seed_round_does_not_shadow_a_legitimate_trailing_round(pd):
    """정당한 산출이 있는 뒤 라운드는 그 뒤에 낀 시드 라운드가 있어도 그대로 판정된다.

    round2 가 F-001 을 실제로 해소 확인하는 라운드다. round3(시드 그대로)을 더 붙여도
    round2 의 산출은 과잉 배제 없이 그대로 반영되어야 한다."""
    first = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    })
    disposition = _disposition(1, [_decision("accepted")])
    resolved = _review_section({
        "version": BLOCK_VERSION,
        "findings": [],
        "confirmations": [{"id": "F-001", "status": "resolved", "evidence": "probe rc=0"}],
    })
    ticket = first + disposition + resolved
    rounds = ticket.rounds(pd)
    round3 = _round(
        pd, 3,
        _pending_seed_text(pd, "code-reviewer", ticket.spec, (2, rounds[1].text)),
        pending=True,
    )

    assert pd._pm_review_surface_rounds(rounds + [round3]) == rounds
    delta = pd.parse_pm_review_delta(ticket.spec, rounds + [round3])
    # 값 핀은 3필드 전부다 — `confirmation_cursor` 는 [[T-0805]] 가 뒤에 실은 필드이고, 여기서
    # F-001 이 2 인 것은 라운드 2 의 reviewer 확인이 그 ID 의 인과 floor 이기 때문이다(시드
    # 라운드와 무관). 기본값에 기대 두 필드만 적으면 필드가 늘어난 순간 핀이 조용히 헐거워진다.
    assert delta == pd.parse_pm_review_delta(ticket.spec, rounds) == pd.PMReviewDelta(
        (), False, (("F-001", 2),),
    )


def test_pending_seed_round_leaves_delta_output_bytes_unchanged(pd):
    """pending 라운드가 없는 형상과 비교해 delta 산출이 bytes 로 같다.

    시드 라운드를 뒤에 덧붙여도 이미 accepted 판정된 F-001 의 delta 렌더는 한 글자도
    안 바뀐다."""
    round1 = _round(pd, 1, _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }).round_texts[0])
    spec = _disposition(1, [_decision("accepted")]).spec
    round2 = _round(
        pd, 2, _pending_seed_text(pd, "code-reviewer", spec, (1, round1.text)), pending=True,
    )
    baseline = [round1]
    with_pending = [round1, round2]

    baseline_delta = pd.parse_pm_review_delta(spec, baseline)
    with_pending_delta = pd.parse_pm_review_delta(spec, with_pending)
    assert baseline_delta == with_pending_delta
    assert (
        pd.render_pm_review_delta("T-TEST", baseline_delta)
        == pd.render_pm_review_delta("T-TEST", with_pending_delta)
    )


def test_pending_seed_round_leaves_prep_surface_output_bytes_unchanged(pd):
    """pending 라운드가 없는 형상과 비교해 disposition-template·confirmable 산출이 bytes 로
    같다 — 아직 PM 미판정인 F-001 의 준비 골격·확인 대상 ID 는 시드 라운드를 덧붙여도
    그대로다."""
    round1 = _round(pd, 1, _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }).round_texts[0])
    spec = ""
    round2 = _round(
        pd, 2, _pending_seed_text(pd, "code-reviewer", spec, (1, round1.text)), pending=True,
    )
    baseline = [round1]
    with_pending = [round1, round2]

    assert (
        pd.render_pm_review_disposition_template(spec, baseline)
        == pd.render_pm_review_disposition_template(spec, with_pending)
    )
    assert (
        pd.collect_confirmable_finding_ids(spec, "code-reviewer", baseline)
        == pd.collect_confirmable_finding_ids(spec, "code-reviewer", with_pending)
    )


# ── 세 CLI 표면(review delta·review verify-template·rounds resolve --pm-verified) ──

def _materialize_rounds(pd, spec_text: str, rounds, tickets_dir: Path, ticket_id: str) -> Path:
    """명세 파일 + (역할이 섞일 수 있는) 라운드 목록을 실 파일로 깐다."""
    rounds_module = pd._load_ticket_rounds()
    spec_path = tickets_dir / "claimed" / f"{ticket_id}-review.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec_text, encoding="utf-8", newline="")
    rounds_dir = rounds_module.rounds_dir_for_ticket(ticket_id, tickets_dir)
    rounds_dir.mkdir(parents=True, exist_ok=True)
    for item in rounds:
        (rounds_dir / rounds_module.round_filename(item.ordinal, item.role)).write_text(
            item.text, encoding="utf-8", newline="",
        )
    return spec_path


def test_review_delta_cli_ignores_a_pending_seed_round(pd, monkeypatch, tmp_path, capsys):
    """실 라운드 파일로 깐 티켓에서 시드 라운드가 섞여도 `review delta` CLI 는 rc=0."""
    tickets_dir = tmp_path / "tickets"
    round1_text = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }).round_texts[0]
    spec = _disposition(1, [_decision("accepted")]).spec
    round2_text = _pending_seed_text(pd, "code-reviewer", spec, (1, round1_text))
    ticket_path = _materialize(pd, _Ticket(spec, (round1_text, round2_text)), tickets_dir, "T-0684")

    loaded = pd._load_ticket_rounds().load_rounds(tickets_dir, "T-0684", ticket_text=spec)
    assert [item.pending for item in loaded] == [False, True]

    class FakeBoard:
        @staticmethod
        @contextlib.contextmanager
        def board_lock():
            yield

        @staticmethod
        def find_ticket_exact(_ticket):
            return "claimed", ticket_path

        @staticmethod
        def tickets_dir():
            return tickets_dir

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: FakeBoard())
    assert pd.main(["review", "delta", "--ticket", "T-0684"]) == 0
    assert "F-001" in capsys.readouterr().out


def test_review_verify_template_cli_ignores_a_pending_seed_round(pd, monkeypatch, tmp_path, capsys):
    """developer 라운드 뒤에 시드 그대로인 리뷰 라운드가 이어져도 `verify-template` CLI 는 rc=0."""
    tickets_dir = tmp_path / "tickets"
    round1_text = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }).round_texts[0]
    spec = _disposition(1, [_decision("accepted")]).spec
    round1 = _round(pd, 1, round1_text)
    dev_round = _round(pd, 2, (
        "## 구현 보충 (developer · 2026-08-14)\n\n## 변경 파일\n- x\n\n"
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps({
            "version": pd.PM_REVIEW_VERIFY_VERSION,
            "verifications": [{
                "id": "F-001", "machine_verifiable": True, "command": "echo hi",
                "expected": "hi", "before": "bye", "reason": "",
            }],
        })
        + "\n```\n"
    ), role="developer")
    round3_text = _pending_seed_text(pd, "code-reviewer", spec, (1, round1_text))
    round3 = _round(pd, 3, round3_text, pending=True)
    ticket_path = _materialize_rounds(
        pd, spec, [round1, dev_round, round3], tickets_dir, "T-0786",
    )

    loaded = pd._load_ticket_rounds().load_rounds(tickets_dir, "T-0786", ticket_text=spec)
    assert [item.pending for item in loaded] == [False, False, True]

    class FakeBoard:
        @staticmethod
        @contextlib.contextmanager
        def board_lock():
            yield

        @staticmethod
        def find_ticket_exact(_ticket):
            return "claimed", ticket_path

        @staticmethod
        def tickets_dir():
            return tickets_dir

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: FakeBoard())
    assert pd.main(["review", "verify-template", "--ticket", "T-0786"]) == 0
    assert pd.PM_REVIEW_CONFIRMATION_BLOCK in capsys.readouterr().out


def test_pm_verified_evidence_problem_ignores_a_pending_seed_round(pd, tmp_path):
    """`rounds resolve --pm-verified` 가 소비하는 증거 판정도 시드 라운드에 막히지 않는다.

    선언 CLI 와 같은 형태로(내부 채널 스코프 · 표면 하한 = 장부 잔여 1) 호출한다 — T-0791 에서
    무스코프 전역 분기를 삭제했다."""
    tickets_dir = tmp_path / "tickets"
    round1_text = _review_section({
        "version": BLOCK_VERSION,
        "findings": [_finding("implementation-defect")],
        "confirmations": [],
    }).round_texts[0]
    round1 = _round(pd, 1, round1_text)
    dev_round = _round(pd, 2, (
        "## 구현 보충 (developer · 2026-08-14)\n\n## 변경 파일\n- x\n\n"
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps({
            "version": pd.PM_REVIEW_VERIFY_VERSION,
            "verifications": [{
                "id": "F-001", "machine_verifiable": True, "command": "echo hi",
                "expected": "hi", "before": "bye", "reason": "",
            }],
        })
        + "\n```\n"
    ), role="developer")
    round3_text = _pending_seed_text(pd, "code-reviewer", "", (1, round1_text))
    round3 = _round(pd, 3, round3_text, pending=True)

    confirmation = _Ticket(f"```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n" + json.dumps({
        "version": pd.PM_REVIEW_MACHINE_CONFIRMATION_VERSION, "round": 2,
        "confirmations": [{
            "id": "F-001", "status": "resolved", "command": "echo hi", "observed": "hi",
        }],
    }) + "\n```\n")
    spec = (_disposition(1, [_decision("accepted")]) + confirmation).spec
    _materialize_rounds(pd, spec, [round1, dev_round, round3], tickets_dir, "T-0786")

    rounds_module = pd._load_ticket_rounds()
    loaded = rounds_module.load_rounds(tickets_dir, "T-0786", ticket_text=spec)
    assert [item.pending for item in loaded] == [False, False, True]
    assert pd.pm_verified_evidence_problem(
        spec, loaded, reviewer_role=pd.INTERNAL_REVIEW_ROLE, surface_floor=1,
    ) is None
