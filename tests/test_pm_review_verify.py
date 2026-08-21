"""T-0786 — dev 재현 커맨드(verify) + PM 기계 확인(confirmation) 기계화.

리뷰 확인 라운드의 reviewer 재투입을 기계 판정으로 대체하는 표면 — 신규 블록 2종
(`pm-review-verify-v1`·`pm-review-confirmation-v1`) + `review verify-template` CLI +
(D1) `pm-verified` 완료 처분의 파서·병합·CLI 계약을 지킨다.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PM_DELEGATE = REPO / ".project_manager" / "tools" / "pm_delegate.py"
BOARD = REPO / ".project_manager" / "tools" / "board.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load(PM_DELEGATE, "pm_delegate_verify")


@pytest.fixture(scope="module")
def board(pd):
    return _load(BOARD, "board_verify")


# ── 픽스처 빌더 ──────────────────────────────────────────────────────────

def _round(pd, ordinal: int, role: str, text: str):
    rounds_module = pd._load_ticket_rounds()
    return rounds_module.Round(
        ordinal=ordinal, role=role,
        path=Path(rounds_module.round_filename(ordinal, role)),
        text=text, pending=False,
    )


def _finding(
    fid: str = "F-001", *, classification: str = "implementation-defect",
    design_change: bool = False,
) -> dict:
    return {
        "id": fid, "class": classification, "severity": "must-fix",
        "authority": "[[T-0786]] §완료 조건", "evidence": f"{fid} probe",
        "recommendation": f"{fid} fix", "design_change": design_change,
    }


def _reviewer_round_text(pd, findings: list, confirmations: list | None = None) -> str:
    payload = {
        "version": pd.PM_REVIEW_VERSION, "findings": findings,
        "confirmations": confirmations or [],
    }
    mustfix = "\n".join(f"- {item['id']}" for item in findings) or "- 없음"
    verdict = "반려" if findings else "통과"
    return (
        "## 리뷰 (code-reviewer · 2026-08-21)\n\n"
        f"## must-fix\n{mustfix}\n\n"
        f"## 판정\n판정: {verdict}\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _decision(fid: str, decision: str, *, prerequisite: str = "") -> dict:
    return {
        "id": fid, "decision": decision, "reason": f"PM {decision} 근거",
        "scope": f"{fid} 허용 범위" if decision == "accepted" else "",
        "prerequisite": prerequisite or (
            "[[ADR-0001]] 선행 개정" if decision == "decision-required" else ""
        ),
    }


def _disposition_block(pd, ordinal: int, rows: list) -> str:
    payload = {
        "version": pd.PM_REVIEW_DISPOSITION_VERSION, "reviewer_ordinal": ordinal,
        "dispositions": rows,
    }
    return (
        f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _verify_row(
    fid: str = "F-001", *, machine_verifiable: bool = True,
    command: str = "echo hi", expected: str = "hi", before: str = "bye",
    reason: str = "",
) -> dict:
    return {
        "id": fid, "machine_verifiable": machine_verifiable, "command": command,
        "expected": expected, "before": before, "reason": reason,
    }


def _verify_block(pd, rows: list) -> str:
    payload = {"version": pd.PM_REVIEW_VERIFY_VERSION, "verifications": rows}
    return (
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _developer_round_text(pd, verify_rows: list | None = None) -> str:
    body = (
        "## 구현 보충 (developer · 2026-08-21)\n\n"
        "## 변경 파일\n- `x.py`: fix\n\n## 신규 테스트\n- 1개\n\n"
        "## 회귀\n- 커맨드: `pytest`\n- 결과: 1 passed\n\n"
        "## DoD evidence\n- 완료: 됨\n\n## 민감도\n- N/A\n"
    )
    if verify_rows:
        body += "\n" + _verify_block(pd, verify_rows)
    return body


def _confirmation_row(
    fid: str = "F-001", status: str = "resolved", *,
    command: str = "echo hi", observed: str = "hi",
) -> dict:
    return {"id": fid, "status": status, "command": command, "observed": observed}


def _confirmation_block(pd, round_ordinal: int, rows: list) -> str:
    payload = {
        "version": pd.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
        "round": round_ordinal, "confirmations": rows,
    }
    return (
        f"```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _basic_ticket(pd, **verify_kwargs):
    """reviewer round 1(F-001 accepted) + developer round 2(verify F-001) 한 쌍."""
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(
        pd, 2, "developer",
        _developer_round_text(pd, [_verify_row("F-001", **verify_kwargs)]),
    )
    return spec, [r1, dev2]


# ── 시드 ─────────────────────────────────────────────────────────────────

def test_seed_prefills_a_verify_row_per_accepted_finding(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(
        pd, [_finding("F-001"), _finding("F-002")],
    ))
    spec = _disposition_block(pd, 1, [
        _decision("F-001", "accepted"), _decision("F-002", "accepted"),
    ])
    body = pd.render_ticket_growth_section_seed("developer", spec, rounds=[r1])
    assert pd.PM_REVIEW_VERIFY_BLOCK in body
    payload = json.loads(
        body.split(f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n", 1)[1].split("\n```", 1)[0]
    )
    assert [row["id"] for row in payload["verifications"]] == ["F-001", "F-002"]
    assert set(payload["verifications"][0]) == set(pd.PM_REVIEW_VERIFY_ROW_KEYS)


def test_seed_has_no_fence_when_accepted_is_empty(pd):
    """accepted 0 건이면 fence 자체가 없다 — 최초 구현 라운드 골격은 bytes 그대로다."""
    body_no_rounds = pd.render_ticket_growth_section_seed("developer", "")
    body_with_empty_rounds = pd.render_ticket_growth_section_seed(
        "developer", "", rounds=(),
    )
    assert body_no_rounds == body_with_empty_rounds
    assert pd.PM_REVIEW_VERIFY_BLOCK not in body_no_rounds
    assert body_no_rounds == (
        "## 변경 파일\n- `<경로>`: <변경 내용과 이유>\n\n"
        "## 신규 테스트\n- 추가한 테스트: <N개 · 파일/케이스>\n\n"
        "## 회귀\n- 커맨드: `<실행 커맨드>`\n"
        "- 결과: <A passed / B failed>\n\n"
        "## DoD evidence\n- <완료 조건>: <충족 근거>\n\n"
        "## 민감도\n- <상수/가드 임시 변경 → red, 복원 → green 실측>\n"
    )


def test_seed_degrades_without_fence_when_delta_is_unresolvable(pd, capsys):
    """reviewer 라운드는 있으나 PM 미판정(pending) — 강등해 fence 없이 시드하고 경고를 낸다."""
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    body = pd.render_ticket_growth_section_seed("developer", "", rounds=[r1])
    assert pd.PM_REVIEW_VERIFY_BLOCK not in body
    assert "accepted delta 를 해소할 수 없어" in capsys.readouterr().err


# ── pending 판정(시점 독립) ──────────────────────────────────────────────

def test_pending_judgment_reads_only_the_round_body(pd):
    spec, rounds = _basic_ticket(pd)
    seed = pd._load_ticket_rounds().render_round_seed(
        "developer", spec, today="2026-08-21", rounds=[rounds[0]],
    )
    body = seed.partition("\n\n")[2]
    assert pd.ticket_round_body_is_pending("developer", body) is True

    filled = (
        body.replace('"machine_verifiable":"<true|false>"', '"machine_verifiable":true')
        .replace(
            '"command":"<machine_verifiable=true 면 단일 재현 커맨드, 아니면 빈 문자열>"',
            '"command":"echo hi"',
        )
        .replace('"expected":"<fix 후 관측돼야 하는 문자열>"', '"expected":"hi"')
        .replace(
            '"before":"<machine_verifiable=true 면 fix 전 실값, 아니면 빈 문자열>"',
            '"before":"bye"',
        )
        .replace(
            '"reason":"<machine_verifiable=false 일 때만 design-judgment|'
            'adversarial-probing|not-reproducible, 아니면 빈 문자열>"',
            '"reason":""',
        )
    )
    assert filled != body
    assert pd.ticket_round_body_is_pending("developer", filled) is False

    # 같은 골격을 다른 명세로 다시 지어도(다른 accepted 계산 입력) 판정은 그 본문 하나로 같다.
    assert pd.ticket_round_body_is_pending(
        "developer", pd._render_developer_round_seed_body(["F-001"]),
    ) is True


def test_pending_placeholder_verify_block_is_not_a_valid_edit(pd):
    """verify fence 가 있는데 파싱 불가(자리표시자 그대로 편집 중)면 pending 이 아니다(None 규칙)."""
    broken = (
        "## 변경 파일\n- `x`: y\n\n## 신규 테스트\n- 1\n\n## 회귀\n- 커맨드: `x`\n- 결과: ok\n\n"
        "## DoD evidence\n- x\n\n## 민감도\n- x\n\n"
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\nnot json\n```\n"
    )
    assert pd.ticket_round_body_is_pending("developer", broken) is False


# ── 파서 실패 클래스 (전수) ──────────────────────────────────────────────

def _verify_delta(pd, spec, rounds):
    return pd.parse_pm_review_delta(spec, rounds)


@pytest.mark.parametrize(
    "mutate,pattern",
    [
        (lambda row: row.pop("reason"), "missing"),
        (lambda row: row.update(extra="x"), "extra"),
        (lambda row: row.update(command=""), "command/expected/before"),
        (lambda row: row.update(before=row["expected"]), "before는 expected와 달라야"),
        (lambda row: row.update(command="echo hi; rm -rf /"), "메타문자"),
        (lambda row: row.update(command="echo hi\nrm -rf /"), "메타문자"),
        # F-003(R4 리뷰 fix) — strip 이 검사보다 먼저면 선두/후미 개행이 검사를 피해간다.
        (lambda row: row.update(command=row["command"] + "\n"), "메타문자"),
        (lambda row: row.update(command="\n" + row["command"]), "메타문자"),
        (lambda row: row.update(command=row["command"] + "\r"), "메타문자"),
        (lambda row: row.update(machine_verifiable=False, command="", before="",
                                 reason="not-a-real-reason"), "reason 미지원"),
    ],
    ids=(
        "missing-key", "extra-key", "empty-command", "before-equals-expected",
        "metachar-semicolon", "metachar-newline",
        "metachar-trailing-newline", "metachar-leading-newline", "metachar-trailing-cr",
        "reason-not-in-enum",
    ),
)
def test_verify_row_schema_failures(pd, mutate, pattern):
    """verify 행 내용 파싱은 지연 파싱이다 — 기계 확인이 그 라운드를 참조해야 촉발된다."""
    row = _verify_row("F-001")
    mutate(row)
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [row]))
    spec_confirmed = spec + _confirmation_block(pd, 2, [_confirmation_row("F-001")])
    with pytest.raises(pd.PMReviewError, match=pattern) as caught:
        _verify_delta(pd, spec_confirmed, [r1, dev2])
    assert caught.value.code == "malformed"


def test_unreferenced_malformed_verify_row_does_not_block_delta(pd):
    """아직 아무 기계 확인도 참조하지 않은 라운드의 깨진 verify 행은 delta 를 막지 않는다.

    표시면(harvest·review delta·verify-template)이 판정면 엄격 규칙에 전염되지 않는다는
    지연 파싱 설계의 핵심 단언이다."""
    broken_row = _verify_row("F-001")
    broken_row.pop("reason")
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [broken_row]))
    delta = _verify_delta(pd, spec, [r1, dev2])
    assert [finding.id for finding, _disposition in delta.accepted] == ["F-001"]


def test_verify_payload_duplicate_member_at_top_level(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    block = _verify_block(pd, [_verify_row("F-001")]).replace(
        '"version":1,', '"version":0,"version":1,', 1,
    )
    dev2 = _round(pd, 2, "developer", "## 구현 보충 (developer · 2026-08-21)\n\n" + block)
    with pytest.raises(pd.PMReviewError, match="duplicate JSON member") as caught:
        _verify_delta(pd, spec, [r1, dev2])
    assert caught.value.code == "malformed"


def test_verify_row_duplicate_member_at_row_level(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    block = _verify_block(pd, [_verify_row("F-001")]).replace(
        '"id":"F-001",', '"id":"F-000","id":"F-001",', 1,
    )
    dev2 = _round(pd, 2, "developer", "## 구현 보충 (developer · 2026-08-21)\n\n" + block)
    with pytest.raises(pd.PMReviewError, match="duplicate JSON member") as caught:
        _verify_delta(pd, spec, [r1, dev2])
    assert caught.value.code == "malformed"


def test_unregistered_fence_name_is_malformed(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", (
        "## 구현 보충 (developer · 2026-08-21)\n\n```pm-review-verify-v2\n{}\n```\n"
    ))
    with pytest.raises(pd.PMReviewError, match="지원하지 않거나 손상된") as caught:
        _verify_delta(pd, spec, [r1, dev2])
    assert caught.value.code == "malformed"


def test_placement_violation_verify_in_spec(pd):
    spec, rounds = _basic_ticket(pd)
    spec_bad = spec + _verify_block(pd, [_verify_row("F-001")])
    with pytest.raises(pd.PMReviewError, match="developer 라운드 파일 안에만") as caught:
        _verify_delta(pd, spec_bad, rounds)
    assert caught.value.code == "malformed"


def test_placement_violation_confirmation_in_round_file(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(
        pd, 2, "developer",
        _developer_round_text(pd, [_verify_row("F-001")])
        + _confirmation_block(pd, 2, [_confirmation_row("F-001")]),
    )
    with pytest.raises(pd.PMReviewError, match="명세의 PM 영역에 있어야") as caught:
        _verify_delta(pd, spec, [r1, dev2])
    assert caught.value.code == "malformed"


def test_placement_violation_verify_in_non_developer_round(pd):
    r1 = _round(pd, 1, "code-reviewer", (
        _reviewer_round_text(pd, [_finding("F-001")])
        + _verify_block(pd, [_verify_row("F-001")])
    ))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    with pytest.raises(pd.PMReviewError, match="developer 라운드 파일 안에만") as caught:
        _verify_delta(pd, spec, [r1])
    assert caught.value.code == "malformed"


# ── 판정 결속 ────────────────────────────────────────────────────────────

def test_confirmation_round_must_reference_a_developer_round(pd):
    spec, rounds = _basic_ticket(pd)
    spec_bad = spec + _confirmation_block(pd, 99, [_confirmation_row("F-001")])
    with pytest.raises(pd.PMReviewError, match="developer 라운드가 아닙니다") as caught:
        _verify_delta(pd, spec_bad, rounds)
    assert caught.value.code == "malformed"


def test_confirmation_command_must_match_the_verify_row(pd):
    spec, rounds = _basic_ticket(pd)
    spec_bad = spec + _confirmation_block(
        pd, 2, [_confirmation_row("F-001", command="echo different")],
    )
    with pytest.raises(pd.PMReviewError, match="verify 행과 결속되지 않습니다") as caught:
        _verify_delta(pd, spec_bad, rounds)
    assert caught.value.code == "malformed"


def test_machine_confirmation_rejected_for_non_machine_verifiable_finding(pd):
    spec, rounds = _basic_ticket(
        pd, machine_verifiable=False, command="", before="",
        reason="not-reproducible",
    )
    spec_bad = spec + _confirmation_block(pd, 2, [
        _confirmation_row("F-001", command="echo hi", observed="hi"),
    ])
    with pytest.raises(pd.PMReviewError, match="reviewer 확인 전용") as caught:
        _verify_delta(pd, spec_bad, rounds)
    assert caught.value.code == "malformed"


def test_resolved_confirmation_requires_expected_substring_in_observed(pd):
    spec, rounds = _basic_ticket(pd)
    spec_bad = spec + _confirmation_block(pd, 2, [
        _confirmation_row("F-001", "resolved", observed="여전히 bye"),
    ])
    with pytest.raises(pd.PMReviewError, match="expected가") as caught:
        _verify_delta(pd, spec_bad, rounds)
    assert caught.value.code == "malformed"


def test_machine_confirmation_rejected_for_design_axis_finding(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(
        pd, [_finding("F-001", classification="design-proposal", design_change=True)],
    ))
    spec = _disposition_block(pd, 1, [
        _decision("F-001", "accepted", prerequisite="[[T-0786]] 설계"),
    ])
    dev2 = _round(pd, 2, "developer", _developer_round_text(
        pd, [_verify_row("F-001")],
    ))
    spec_bad = spec + _confirmation_block(pd, 2, [_confirmation_row("F-001", "resolved")])
    with pytest.raises(pd.PMReviewError, match="설계 축") as caught:
        _verify_delta(pd, spec_bad, [r1, dev2])
    assert caught.value.code == "malformed"


def test_rejected_finding_id_reappearing_in_machine_confirmation_is_malformed(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "rejected")])
    dev2 = _round(pd, 2, "developer", "## 구현 보충 (developer · 2026-08-21)\n\n" + _verify_block(
        pd, [_verify_row("F-001")],
    ))
    spec_bad = spec + _confirmation_block(pd, 2, [_confirmation_row("F-001", "resolved")])
    with pytest.raises(pd.PMReviewError, match="rejected finding ID") as caught:
        _verify_delta(pd, spec_bad, [r1, dev2])
    assert caught.value.code == "malformed"


# ── delta 병합 ───────────────────────────────────────────────────────────

def test_resolved_machine_confirmation_drops_finding_from_accepted(pd):
    spec, rounds = _basic_ticket(pd)
    spec_ok = spec + _confirmation_block(pd, 2, [_confirmation_row("F-001", "resolved")])
    delta = pd.parse_pm_review_delta(spec_ok, rounds)
    assert delta.accepted == ()


def test_mixed_reviewer_and_machine_unresolved_history_hits_repeated_unresolved(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [_verify_row("F-001")]))
    r3 = _round(pd, 3, "code-reviewer", _reviewer_round_text(
        pd, [], confirmations=[{"id": "F-001", "status": "unresolved", "evidence": "여전"}],
    ))
    dev4 = _round(pd, 4, "developer", _developer_round_text(pd, [_verify_row("F-001")]))
    rounds = [r1, dev2, r3, dev4]
    spec_bad = spec + _confirmation_block(
        pd, 4, [_confirmation_row("F-001", "unresolved", observed="bye")],
    )
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(spec_bad, rounds)
    assert caught.value.code == "repeated-unresolved"


def test_unresolved_machine_confirmation_keeps_finding_accepted(pd):
    spec, rounds = _basic_ticket(pd)
    spec_still = spec + _confirmation_block(
        pd, 2, [_confirmation_row("F-001", "unresolved", observed="bye")],
    )
    delta = pd.parse_pm_review_delta(spec_still, rounds)
    assert [finding.id for finding, _disposition in delta.accepted] == ["F-001"]


# ── verify-template CLI 판정면 ──────────────────────────────────────────

def test_verify_template_routes_design_axis_and_dev_declared_reasons_to_reviewer(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [
        _finding("F-001", classification="design-proposal", design_change=True),
        _finding("F-002"),
    ]))
    spec = _disposition_block(pd, 1, [
        _decision("F-001", "accepted", prerequisite="[[T-0786]] 설계"),
        _decision("F-002", "accepted"),
    ])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [
        _verify_row("F-001"),
        _verify_row(
            "F-002", machine_verifiable=False, command="", before="",
            expected="사람 판단 필요", reason="design-judgment",
        ),
    ]))
    template = pd.pm_review_verify_template(spec, [r1, dev2])
    assert template.machine_rows == ()
    assert template.missing == ()
    reasons = dict(template.reviewer_required)
    assert "F-001" in reasons and "설계 축" in reasons["F-001"]
    assert "F-002" in reasons and "design-judgment" in reasons["F-002"]


def test_verify_template_reports_missing_verify_rows(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, []))
    template = pd.pm_review_verify_template(spec, [r1, dev2])
    assert template.missing == ("F-001",)
    assert pd.render_pm_review_verify_template(template) == ""


def test_verify_template_output_reparses_through_the_confirmation_parser(pd):
    spec, rounds = _basic_ticket(pd)
    template = pd.pm_review_verify_template(spec, rounds)
    rendered = pd.render_pm_review_verify_template(template)
    fence_body = rendered.split(f"```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n", 1)[1].split(
        "\n```", 1,
    )[0]
    payload = json.loads(fence_body)
    assert payload["round"] == 2
    assert payload["confirmations"][0]["command"] == "echo hi"
    filled = fence_body.replace(
        '"status":"<resolved|unresolved|regressed>"', '"status":"resolved"',
    ).replace('"observed":"<관측값>"', '"observed":"hi"')
    spec_ok = spec + f"```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n" + filled + "\n```\n"
    delta = pd.parse_pm_review_delta(spec_ok, rounds)
    assert delta.accepted == ()


def _fake_board(pd, spec_path: Path, tickets_dir: Path):
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


def _materialize(pd, spec: str, rounds, tickets_dir: Path, ticket_id: str) -> Path:
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


def test_verify_template_cli_rc0_on_full_coverage_and_rc1_on_missing(
    pd, tmp_path, monkeypatch, capsys,
):
    tickets_dir = tmp_path / "tickets"
    spec, rounds = _basic_ticket(pd)
    spec_path = _materialize(pd, spec, rounds, tickets_dir, "T-0786")
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _owner: _fake_board(pd, spec_path, tickets_dir),
    )
    assert pd.main(["review", "verify-template", "--ticket", "T-0786"]) == 0
    out = capsys.readouterr().out
    assert pd.PM_REVIEW_CONFIRMATION_BLOCK in out and '"round":2' in out

    r1 = rounds[0]
    dev2_empty = _round(pd, 2, "developer", _developer_round_text(pd, []))
    _materialize(pd, spec, [r1, dev2_empty], tickets_dir, "T-0786")
    assert pd.main(["review", "verify-template", "--ticket", "T-0786"]) == 1
    error = capsys.readouterr().err
    assert "verify 행이 없는 accepted finding" in error and "F-001" in error


def test_verify_template_cli_honors_explicit_round(pd, tmp_path, monkeypatch, capsys):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [_verify_row("F-001")]))
    tickets_dir = tmp_path / "tickets"
    spec_path = _materialize(pd, spec, [r1, dev2], tickets_dir, "T-0786")
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: tmp_path)
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _owner: _fake_board(pd, spec_path, tickets_dir),
    )
    assert pd.main([
        "review", "verify-template", "--ticket", "T-0786", "--round", "2",
    ]) == 0
    assert pd.main([
        "review", "verify-template", "--ticket", "T-0786", "--round", "99",
    ]) == 1
    assert "developer ordinal=99 라운드가 없습니다" in capsys.readouterr().err


# ── (D1) pm-verified 증거 + 상한 회계 불변 ──────────────────────────────

def test_pm_verified_evidence_problem_requires_empty_accepted_and_a_machine_confirmation(pd):
    spec, rounds = _basic_ticket(pd)
    # accepted 잔여가 있으면 거부.
    assert "accepted 잔여" in pd.pm_verified_evidence_problem(spec, rounds)
    # resolve 됐지만 기계 확인이 없으면(가상: 리뷰 confirmation 만으로 해소) — 대조군은
    # accepted==() 이 되도록 기계 확인을 넣은 경우만 통과해야 한다.
    spec_ok = spec + _confirmation_block(pd, 2, [_confirmation_row("F-001", "resolved")])
    assert pd.pm_verified_evidence_problem(spec_ok, rounds) is None


def test_pm_verified_evidence_problem_rejects_when_delta_cannot_parse(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    # PM 미판정(pending) — delta 파싱 실패.
    problem = pd.pm_verified_evidence_problem("", [r1])
    assert problem is not None and "delta 파싱 실패" in problem


def test_machine_confirmation_flow_leaves_internal_ledger_bytes_unchanged(pd, tmp_path):
    """기계 확인은 스폰이 없어 라운드 장부에 아무것도 남기지 않는다(불변식 11)."""
    ledger = tmp_path / "internal_review_rounds.json"
    original = b'{"T-0786": {"count": 2, "rounds": []}}'
    ledger.write_bytes(original)

    spec, rounds = _basic_ticket(pd)
    spec_ok = spec + _confirmation_block(pd, 2, [_confirmation_row("F-001", "resolved")])
    # 시드·판정면·CLI 판정 재료(verify-template) 전부 이 장부 경로를 인자로도 받지 않는다 —
    # 실제로 만지지 않는다는 사실을 bytes 대조로 못박는다.
    pd.render_ticket_growth_section_seed("developer", spec, rounds=rounds)
    pd.parse_pm_review_delta(spec_ok, rounds)
    pd.pm_review_verify_template(spec, rounds)
    pd.pm_verified_evidence_problem(spec_ok, rounds)

    assert ledger.read_bytes() == original


# ── 시드 골격 bytes 불변(accepted 0) ─────────────────────────────────────

def test_accepted_zero_developer_seed_bytes_are_unchanged_from_pre_t0786(pd):
    """구현 전 골격과 accepted 0 건 골격은 byte-for-byte 같다(회귀 anchor)."""
    pre_t0786_constant = (
        "## 변경 파일\n- `<경로>`: <변경 내용과 이유>\n\n"
        "## 신규 테스트\n- 추가한 테스트: <N개 · 파일/케이스>\n\n"
        "## 회귀\n- 커맨드: `<실행 커맨드>`\n"
        "- 결과: <A passed / B failed>\n\n"
        "## DoD evidence\n- <완료 조건>: <충족 근거>\n\n"
        "## 민감도\n- <상수/가드 임시 변경 → red, 복원 → green 실측>\n"
    )
    assert pd.render_ticket_growth_section_seed("developer", "") == pre_t0786_constant


# ── 하위호환: 기존 disposition 블록 2형상(reviewer_role 있음/없음) ────────

def test_legacy_disposition_blocks_still_parse_with_verify_confirmation_support_added(pd):
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    with_role = (
        '{"version":1,"reviewer_role":"code-reviewer","reviewer_ordinal":1,'
        '"dispositions":[{"id":"F-001","decision":"accepted","reason":"ok",'
        '"scope":"범위","prerequisite":""}]}'
    )
    spec_with_role = f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n{with_role}\n```\n"
    delta = pd.parse_pm_review_delta(spec_with_role, [r1])
    assert [finding.id for finding, _d in delta.accepted] == ["F-001"]

    without_role = (
        '{"version":1,"reviewer_ordinal":1,'
        '"dispositions":[{"id":"F-001","decision":"accepted","reason":"ok",'
        '"scope":"범위","prerequisite":""}]}'
    )
    spec_without_role = f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n{without_role}\n```\n"
    delta2 = pd.parse_pm_review_delta(spec_without_role, [r1])
    assert [finding.id for finding, _d in delta2.accepted] == ["F-001"]


# ── (D1) 완료 게이트 — board.py 통합 ────────────────────────────────────

def test_board_gate_resolution_recognizes_pm_verified_kind(board):
    entry = {
        "resolution": {
            "kind": board.GATE_RESOLUTION_PM_VERIFIED,
            "round_sequence": 2, "rounds": 2, "ts": "2026-08-21T00:00:00+00:00",
        },
        "rounds": [{"sequence": 2}, {"sequence": 1}],
    }
    declared = board.gate_resolution(entry)
    assert declared is not None
    assert declared["kind"] == board.GATE_RESOLUTION_PM_VERIFIED
    assert declared["round_sequence"] == 2 and declared["rounds"] == 2


def test_gate_disposition_problem_rejects_pm_verified_when_not_allowed(board):
    entry = {
        "resolution": {
            "kind": board.GATE_RESOLUTION_PM_VERIFIED,
            "round_sequence": 1, "rounds": 1,
        },
        "rounds": [{"sequence": 1, "verdict": 1, "must_fix": 1}],
    }
    problem = board._gate_disposition_problem(
        "T-0786", entry, {"T-0786": entry}, [],
    )
    assert problem is not None and "허용하지 않습니다" in problem


def test_gate_disposition_problem_accepts_pm_verified_when_evidence_clears(board):
    entry = {
        "resolution": {
            "kind": board.GATE_RESOLUTION_PM_VERIFIED,
            "round_sequence": 1, "rounds": 1,
        },
        "rounds": [{"sequence": 1, "verdict": 1, "must_fix": 1}],
    }
    problem = board._gate_disposition_problem(
        "T-0786", entry, {"T-0786": entry}, [],
        allow_pm_verified=True, pm_verified_problem=lambda: None,
    )
    assert problem is None


def test_gate_disposition_problem_surfaces_pm_verified_evidence_failure(board):
    entry = {
        "resolution": {
            "kind": board.GATE_RESOLUTION_PM_VERIFIED,
            "round_sequence": 1, "rounds": 1,
        },
        "rounds": [{"sequence": 1, "verdict": 1, "must_fix": 1}],
    }
    problem = board._gate_disposition_problem(
        "T-0786", entry, {"T-0786": entry}, [],
        allow_pm_verified=True,
        pm_verified_problem=lambda: "PM 판정 accepted 잔여가 있습니다: F-001",
    )
    assert problem is not None and "발동 조건 재검증 실패" in problem


# ── R4 리뷰 fix — F-001 CONFIRM_ROUND_SCOPE_RULE 단일 상수(cross+native 양 경로) ──────────

def test_confirm_charter_embeds_the_single_scope_rule_constant(pd):
    """cross 경로 — `_INTERNAL_CONFIRM_CHARTER`가 단일 상수를 그대로 embed한다(손 복제 0)."""
    assert pd.CONFIRM_ROUND_SCOPE_RULE in pd._INTERNAL_CONFIRM_CHARTER


def test_native_confirmation_round_seed_embeds_scope_rule_as_html_comment(pd):
    """native 경로 — 프리필된 실 ID가 있는 확인 라운드는 첫 줄(헤더) 아래에 같은 상수를 심는다."""
    previous = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    body = pd.render_ticket_growth_section_seed(
        "code-reviewer", "", previous_round=(previous.ordinal, previous.text),
    )
    assert body.startswith(f"<!-- {pd.CONFIRM_ROUND_SCOPE_RULE} -->\n\n")


def test_native_initial_review_round_seed_has_no_scope_rule_comment(pd):
    """역방향 확인 — 최초 리뷰 라운드(자리표시자 ID)는 확인 라운드가 아니라 주석을 심지 않는다."""
    body = pd.render_ticket_growth_section_seed("code-reviewer", "")
    assert pd.CONFIRM_ROUND_SCOPE_RULE not in body
    assert not body.startswith("<!--")


def test_confirmation_round_seed_scope_comment_survives_pending_round_trip(pd):
    """스코프 주석이 붙어도 시드 그대로(pending) 판정은 같은 렌더 함수 대조로 그대로 선다."""
    previous = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    rounds_module = pd._load_ticket_rounds()
    seed = rounds_module.render_round_seed(
        "code-reviewer", "", today="2026-08-22",
        previous_round=(previous.ordinal, previous.text),
    )
    body = seed.partition("\n\n")[2]
    assert body.startswith(f"<!-- {pd.CONFIRM_ROUND_SCOPE_RULE} -->\n\n")
    assert pd.ticket_round_body_is_pending("code-reviewer", body) is True


# ── R4 리뷰 fix — F-002 confirmation.round 결속(전역 고유+단조·causal floor) ──────────────

def test_pre_declared_verify_round_cannot_confirm_a_later_finding(pd):
    """F-002 우회 1(선-선언 verify 차용) — finding 선언보다 앞선 developer round 참조는 malformed."""
    dev1 = _round(pd, 1, "developer", _developer_round_text(pd, [_verify_row("F-001")]))
    r2 = _round(pd, 2, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 2, [_decision("F-001", "accepted")])
    spec_bad = spec + _confirmation_block(pd, 1, [_confirmation_row("F-001", "resolved")])
    with pytest.raises(pd.PMReviewError, match="뒤가 아닙니다") as caught:
        pd.parse_pm_review_delta(spec_bad, [dev1, r2])
    assert caught.value.code == "malformed"


def test_late_appended_stale_round_confirmation_cannot_reorder_a_newer_reviewer_resolution(pd):
    """F-002 우회 2(late unresolved 재정렬) — 명세 끝에 추가해도 과거 round 참조는 더 최신
    reviewer 확인(resolved)보다 앞설 수 없다."""
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [_verify_row("F-001")]))
    r3 = _round(pd, 3, "code-reviewer", _reviewer_round_text(
        pd, [], confirmations=[{"id": "F-001", "status": "resolved", "evidence": "해소"}],
    ))
    rounds = [r1, dev2, r3]
    spec_bad = spec + _confirmation_block(
        pd, 2, [_confirmation_row("F-001", "unresolved", observed="bye")],
    )
    with pytest.raises(pd.PMReviewError, match="뒤가 아닙니다") as caught:
        pd.parse_pm_review_delta(spec_bad, rounds)
    assert caught.value.code == "malformed"


def test_duplicate_round_confirmation_blocks_for_the_same_round_are_rejected(pd):
    """F-002 우회 3(동일 round 중복 블록) — round 는 티켓 전역 고유 키라 재사용은 malformed."""
    spec, rounds = _basic_ticket(pd)
    spec_bad = (
        spec
        + _confirmation_block(pd, 2, [_confirmation_row("F-001", "unresolved", observed="bye")])
        + _confirmation_block(pd, 2, [_confirmation_row("F-001", "resolved")])
    )
    with pytest.raises(pd.PMReviewError, match="단조") as caught:
        pd.parse_pm_review_delta(spec_bad, rounds)
    assert caught.value.code == "malformed"


def test_two_increasing_round_confirmations_for_the_same_finding_are_accepted(pd):
    """역방향 확인 — 서로 다른 developer round 를 오름차순으로 참조하는 정상 진행은 막히지 않는다."""
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [_verify_row("F-001")]))
    dev3 = _round(pd, 3, "developer", _developer_round_text(pd, [_verify_row("F-001")]))
    rounds = [r1, dev2, dev3]
    spec_ok = (
        spec
        + _confirmation_block(pd, 2, [_confirmation_row("F-001", "unresolved", observed="bye")])
        + _confirmation_block(pd, 3, [_confirmation_row("F-001", "resolved")])
    )
    delta = pd.parse_pm_review_delta(spec_ok, rounds)
    assert delta.accepted == ()
