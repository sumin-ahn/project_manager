"""T-0786 — dev 재현 커맨드(verify) + PM 기계 확인(confirmation) 기계화.

리뷰 확인 라운드의 reviewer 재투입을 기계 판정으로 대체하는 표면 — 신규 블록 2종
(`pm-review-verify-v1`·`pm-review-confirmation-v1`) + `review verify-template` CLI +
(D1) `pm-verified` 완료 처분의 파서·병합·CLI 계약을 지킨다.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import subprocess
import sys
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


def _fill_verify_field(text: str, key: str, raw_json: str, *, count: int = 1) -> str:
    """verify 행 `key` 필드의 렌더된 값(따옴표 문자열이든 T-0808 raw placeholder 든)을
    `raw_json`(JSON 리터럴 텍스트)으로 치환한다. 자리표시자 정확한 문구를 몰라도 key 뒤 값을
    구조로 찾아 규칙대로 갈아 끼우는 dev 편집을 흉내낸다(엔진이 실제로 낸 골격에 태우는
    왕복 단언 · T-0808). `count=0` 이면 행 전부(무제한)를 갈아 끼운다."""
    pattern = re.compile(rf'"{key}":(?:"(?:[^"\\]|\\.)*"|[^,}}]+)')
    new_text, replaced = pattern.subn(f'"{key}":{raw_json}', text, count=count)
    assert replaced > 0, f"{key!r} 자리를 찾을 수 없습니다: {text!r}"
    return new_text


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
    fence = body.split(f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n", 1)[1].split("\n```", 1)[0]
    # 골격의 `machine_verifiable` 자리는 T-0808 이후 따옴표 없는 raw placeholder라 손대지
    # 않은 골격 그대로는 유효 JSON이 아니다(자리표시자를 실값으로 갈아 끼운 뒤에만 파싱된다 —
    # 왕복 계약은 별도 테스트가 단언한다). 여기서는 id 목록/key 집합만 보면 되므로 그 한
    # 자리만 규칙대로(`<true|false>` → `true`) 채워 파싱한다.
    fence = _fill_verify_field(fence, "machine_verifiable", "true", count=0)
    payload = json.loads(fence)
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

    filled = body
    for key, raw_json in (
        ("machine_verifiable", "true"),
        ("command", json.dumps("echo hi")),
        ("expected", json.dumps("hi")),
        ("before", json.dumps("bye")),
        ("reason", json.dumps("")),
    ):
        filled = _fill_verify_field(filled, key, raw_json)
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
        (lambda row: row.update(command="echo hi; rm -rf /"), "금지 토큰"),
        (lambda row: row.update(command="echo hi\nrm -rf /"), "금지 토큰"),
        # F-003(R4 리뷰 fix) — strip 이 검사보다 먼저면 선두/후미 개행이 검사를 피해간다.
        (lambda row: row.update(command=row["command"] + "\n"), "금지 토큰"),
        (lambda row: row.update(command="\n" + row["command"]), "금지 토큰"),
        (lambda row: row.update(command=row["command"] + "\r"), "금지 토큰"),
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


# ── T-0808: 골격 자리표시자가 계약을 어기는 값을 유도하지 않는다(왕복 단언) ──────────────

def _naive_placeholder_edit(text: str, token: str, replacement: str) -> str:
    """자리표시자 텍스트(`<...>`)만 그대로 갈아 끼우는 dev 편집을 흉내낸다 — 주변 따옴표 유무는
    건드리지 않는다. T-0808 결함류(축 1)의 재현/회귀 경로가 정확히 이 방식이다(자리표시자만
    갈아 끼우는 "정상적인 채움 방식")."""
    assert token in text, f"{token!r} 이 없습니다: {text!r}"
    return text.replace(token, replacement, 1)


def _verify_fence_text(pd, finding_ids=("F-001",)) -> str:
    """`render_pm_review_verify_skeleton` 이 실제로 낸 골격의 fence 본문만 뽑는다(조립 문자열이
    아니라 엔진 산출 그대로 — T-0808 검증 근거 요구)."""
    rendered = pd.render_pm_review_verify_skeleton(list(finding_ids))
    return rendered.split(f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n", 1)[1].split("\n```", 1)[0]


# ── 축 1 — machine_verifiable 자리는 따옴표 없는 raw placeholder ────────────────────

def test_machine_verifiable_placeholder_is_unquoted_raw_in_the_rendered_skeleton(pd):
    """축 1 — 골격 렌더가 boolean 자리를 따옴표 없이 낸다."""
    fence = _verify_fence_text(pd)
    assert '"machine_verifiable":<true|false>' in fence
    assert '"machine_verifiable":"<true|false>"' not in fence


@pytest.mark.parametrize("literal,expect", [("true", True), ("false", False)])
def test_machine_verifiable_naive_edit_round_trips_to_a_real_boolean(pd, literal, expect):
    """축 1 본체(왕복 단언) — 렌더 → 자리표시자만 갈아 끼움(주변 따옴표 없음) → 파싱 성공 →
    boolean 타입(문자열이 아니다)."""
    fence = _verify_fence_text(pd)
    edited = _naive_placeholder_edit(fence, "<true|false>", literal)
    payload = json.loads(edited)  # 자리표시자 하나만 바꿨는데 이미 유효 JSON — 본체 단언.
    assert payload["verifications"][0]["machine_verifiable"] is expect


def test_sensitivity_requoted_placeholder_reproduces_the_original_defect(pd, monkeypatch):
    """민감도(축 1) — 렌더 직렬화가 raw placeholder 특례를 잃으면(따옴표 되돌림) 실제 wave 에서
    4명 중 3명이 겪은 결함(문자열 `"true"`)이 그대로 재현된다. 위 두 테스트는 이 상태가
    아니라서 통과한다 — 되돌리면 여기서 red. (`_pm_review_render_json` 을 순수 `json.dumps`
    로 되돌려 raw placeholder 특례만 잃게 한다 — 다른 문자열 필드까지 깨는 monkeypatch는
    퇴행을 정확히 재현하지 못한다.)"""
    monkeypatch.setattr(
        pd, "_pm_review_render_json",
        lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )
    fence = _verify_fence_text(pd)
    assert '"machine_verifiable":"<true|false>"' in fence   # 퇴행 재현.
    naive_edit = _naive_placeholder_edit(fence, "<true|false>", "true")
    payload = json.loads(naive_edit)   # 구조는 여전히 유효 JSON이지만 값은 문자열이다.
    assert payload["verifications"][0]["machine_verifiable"] == "true"
    with pytest.raises(pd.PMReviewError, match="boolean이어야 합니다") as caught:
        pd._pm_review_parse_verify_row(payload["verifications"][0])
    assert caught.value.code == "malformed"


def test_string_true_is_still_rejected_no_parser_leniency(pd):
    """파서 완화 금지 — `machine_verifiable` 이 문자열 `"true"` 면 골격과 무관하게 항상 거부."""
    row = {
        "id": "F-001", "machine_verifiable": "true", "command": "echo hi",
        "expected": "hi", "before": "bye", "reason": "",
    }
    with pytest.raises(pd.PMReviewError, match="boolean이어야 합니다") as caught:
        pd._pm_review_parse_verify_row(row)
    assert caught.value.code == "malformed"


# ── 축 2 — command placeholder 는 파서 상수를 그대로 소비한다 ──────────────────────

def test_command_placeholder_derives_from_the_parser_command_shape_hint(pd):
    """축 2 파리티 — command 골격 문구가 파서의 재현 커맨드 안전 경계 문구를 그대로 담는다
    (두 곳 서술 금지). `cd X &&` 대신 절대경로 처방도 실값으로 실린다."""
    fence = _verify_fence_text(pd)
    assert pd._pm_review_command_shape_hint() in fence
    assert "절대경로" in fence
    assert "cd X &&" in fence


def test_command_placeholder_labels_match_the_single_forbidden_token_constant(pd):
    """축 2 파리티(전수) — 골격에 나열된 금지 토큰 표기가 검사 상수 하나에서만 나온다."""
    fence = _verify_fence_text(pd)
    for _token, label in pd._PM_REVIEW_COMMAND_FORBIDDEN_TOKENS:
        assert label in fence


def _parse_row_with_command(pd, command: str):
    """command 만 바꾼 유효 verify 행을 실제 행 파서에 태운다 — 허용 경계 관측 지점."""
    return pd._pm_review_parse_verify_row(_verify_row("F-001", command=command))


def _assert_command_rejected(pd, command: str) -> None:
    with pytest.raises(pd.PMReviewError) as caught:
        _parse_row_with_command(pd, command)
    assert caught.value.code == "malformed"
    assert pd._pm_review_command_shape_hint() in str(caught.value)


def test_sensitivity_forbidden_token_constant_flips_parser_and_render_together(
    pd, monkeypatch,
):
    """민감도(축 2 본체) — 금지 토큰 상수 하나에 토큰을 넣고 빼면 **파서의 통과/거부**와
    **골격 문구**가 같은 방향으로 함께 뒤집힌다. 검사용 상수와 표시용 상수가 두 벌이면
    한쪽만 뒤집히고 여기서 red 다(허용 경계는 파서로, 문구는 렌더 산출로 각각 관측한다)."""
    baseline = pd._PM_REVIEW_COMMAND_FORBIDDEN_TOKENS
    added, dropped = "echo:hi", "echo hi;"

    # 기준선 — `:` 은 허용·문구에 없음 / `;` 은 거부·문구에 있음.
    assert _parse_row_with_command(pd, added).command == added
    assert "`:`" not in _verify_fence_text(pd)
    _assert_command_rejected(pd, dropped)
    assert "`;`" in _verify_fence_text(pd)

    # 토큰 추가 → 파서가 거부하고 골격 문구에도 그 토큰이 나타난다.
    monkeypatch.setattr(
        pd, "_PM_REVIEW_COMMAND_FORBIDDEN_TOKENS", baseline + ((":", "`:`"),),
    )
    _assert_command_rejected(pd, added)
    assert "`:`" in _verify_fence_text(pd)

    # 토큰 제거 → 파서가 통과시키고 골격 문구에서도 그 토큰이 사라진다.
    monkeypatch.setattr(
        pd, "_PM_REVIEW_COMMAND_FORBIDDEN_TOKENS",
        tuple(pair for pair in baseline if pair[0] != ";"),
    )
    assert _parse_row_with_command(pd, dropped).command == dropped
    assert "`;`" not in _verify_fence_text(pd)


def test_cd_and_shell_join_command_is_still_rejected_absolute_path_form_still_passes(pd):
    """역방향 확인 — `cd X && ...` 는 여전히 거부, 절대경로 단일 커맨드는 여전히 통과(실증
    라운드 T-0782/T-0796/T-0777 이 유도됐던 형태를 재확인)."""
    with pytest.raises(pd.PMReviewError, match="금지 토큰"):
        pd._pm_review_assert_verify_command_shape(
            "cd /abs/path && pytest -q", "verify F-001.command",
        )
    pd._pm_review_assert_verify_command_shape(
        "pytest /abs/path/tests/test_x.py -q", "verify F-001.command",
    )
    with pytest.raises(pd.PMReviewError, match="금지 토큰"):
        # T-0777 실측 — `python3 -c "...;..."` 는 `cd &&` 를 없애도 `-c` 본문의 `;` 로 거부된다.
        pd._pm_review_assert_verify_command_shape(
            'python3 -c "a=1;b=2"', "verify F-001.command",
        )


# ── 축 3 — expected placeholder 는 짧은 기계 대조 가능 문자열만 유도한다 ────────────

def test_expected_placeholder_forbids_prose_and_points_to_round_body(pd):
    """축 3 — placeholder 문구가 '짧은'·'산문 금지'·'라운드 본문' 을 명시해 산문 유도를 막는다."""
    fence = _verify_fence_text(pd)
    assert "짧은 부분 문자열" in fence
    assert "산문" in fence
    assert "라운드 본문" in fence


def test_expected_short_literal_round_trips_through_a_real_command_into_the_confirmation_contract(
    pd,
):
    """축 3 본체(왕복 단언) — 골격대로 짧은 문자열을 채운 verify 행을 실제 커맨드로 실행해
    얻은 observed 가 expected 를 포함하고, 그 값으로 확인 블록을 구성해도 malformed 가 없다
    (`expected ⊆ observed` 계약이 실제로 성립함을 로컬 서브프로세스로 확인 — 외부 발신 없음)."""
    command = f"{sys.executable} -c \"print('3 passed')\""
    fence = _verify_fence_text(pd)
    fence = _naive_placeholder_edit(fence, "<true|false>", "true")
    fence = _fill_verify_field(fence, "command", json.dumps(command))
    fence = _fill_verify_field(fence, "expected", json.dumps("3 passed"))
    fence = _fill_verify_field(fence, "before", json.dumps("2 passed"))
    fence = _fill_verify_field(fence, "reason", json.dumps(""))
    payload = json.loads(fence)
    row_dict = payload["verifications"][0]
    row = pd._pm_review_parse_verify_row(row_dict)

    proc = subprocess.run(
        row.command, shell=True, capture_output=True, text=True, timeout=10,
    )
    observed = proc.stdout.strip()
    assert row.expected in observed   # expected ⊆ observed 계약 — 왕복의 핵심.

    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    spec = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [row_dict]))
    confirmation_row = {
        "id": "F-001", "status": "resolved", "command": row.command, "observed": observed,
    }
    spec_confirmed = spec + _confirmation_block(pd, 2, [confirmation_row])
    delta = pd.parse_pm_review_delta(spec_confirmed, [r1, dev2])   # malformed 없이 성립.
    assert delta.accepted == ()   # resolved 확인이 반영돼 accepted 잔여가 빠진다.


# ── T-0808: 엔진이 시드하는 모든 versioned block 골격 — 비문자열 자리 따옴표 placeholder 0 ──

def test_non_string_field_placeholders_are_zero_across_every_seeded_skeleton(pd):
    """전수(클래스 폐쇄) — verify(`machine_verifiable`)·finding(`design_change`)가 엔진이
    시드하는 골격 전체에서 유일한 비문자열 필드다. 둘 다 실제 타입으로 렌더되고 문자열
    placeholder 로 위장하지 않는다(파싱된 값 단언 — 존재 검사가 아니다). disposition·machine
    confirmation 골격에는 비문자열 필드 placeholder 가 아예 없다(문자열 enum 이거나 렌더
    시점 실값)."""
    # verify: 손대지 않은 골격은 raw placeholder 라 통짜 파싱이 실패한다(문자열로 위장하지
    # 않았다는 증거) — 자리표시자만 갈아 끼우면 실제 bool 이 된다(위 왕복 테스트가 값도 확인).
    with pytest.raises(json.JSONDecodeError):
        json.loads(_verify_fence_text(pd))

    # finding: design_change 는 처음부터 실값(False)이라 골격 자체가 이미 유효 JSON 이고
    # 파싱된 타입이 bool 이다(문자열 "false" 로 위장하지 않았다).
    review_rendered = pd.render_pm_review_block_skeleton("code-reviewer", ["F-NNN"])
    review_fence = review_rendered.split(f"```{pd.PM_REVIEW_BLOCK}\n", 1)[1].split(
        "\n```", 1,
    )[0]
    review_payload = json.loads(review_fence)
    assert review_payload["findings"][0]["design_change"] is False
    assert not isinstance(review_payload["findings"][0]["design_change"], str)

    # disposition — 행 필드는 전부 문자열(`PM_REVIEW_DISPOSITION_KEYS`), payload 의
    # reviewer_ordinal/version 은 렌더 시점 실값(정수)이지 placeholder 가 아니다.
    r1 = _round(pd, 1, "code-reviewer", _reviewer_round_text(pd, [_finding("F-001")]))
    disposition_rendered = pd.render_pm_review_disposition_template("", [r1])
    disposition_fence = disposition_rendered.split(
        f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n", 1,
    )[1].split("\n```", 1)[0]
    disposition_payload = json.loads(disposition_fence)   # 예외 없이 통과.
    assert isinstance(disposition_payload["reviewer_ordinal"], int)
    assert isinstance(disposition_payload["version"], int)

    # machine confirmation — 행 필드는 전부 문자열(`PM_REVIEW_MACHINE_CONFIRMATION_ROW_KEYS`),
    # payload 의 round/version 은 렌더 시점 실값이지 placeholder 가 아니다.
    spec_accepted = _disposition_block(pd, 1, [_decision("F-001", "accepted")])
    dev2 = _round(pd, 2, "developer", _developer_round_text(pd, [_verify_row("F-001")]))
    template = pd.pm_review_verify_template(spec_accepted, [r1, dev2])
    confirmation_rendered = pd.render_pm_review_verify_template(template)
    confirmation_fence = confirmation_rendered.split(
        f"```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n", 1,
    )[1].split("\n```", 1)[0]
    confirmation_payload = json.loads(confirmation_fence)   # 예외 없이 통과.
    assert isinstance(confirmation_payload["round"], int)
    assert isinstance(confirmation_payload["version"], int)


# ── 구조 스캔 전용 재-인용은 boolean 값 자리 하나에만 걸린다 ─────────────────────────

# 정상 문자열 필드가 담을 수 있는, boolean 자리 placeholder 와 같은 토큰.
_TOKEN_IN_STRING_FIELD = "ok:<true|false>"


def test_placeholder_token_in_another_string_field_passes_delta_scan_and_row_parser(pd):
    """재-인용 한정 — `expected` 처럼 정상 문자열 필드가 boolean 자리와 같은 토큰을 담아도
    delta 구조 스캔과 실제 행 파서를 **둘 다** 통과한다. 전역 치환이면 재-인용이 문자열 안에
    따옴표를 밀어 넣어 `pm-review-verify-v1 JSON 파싱 실패` malformed 로 delta 가 죽는다."""
    spec, rounds = _basic_ticket(pd, expected=_TOKEN_IN_STRING_FIELD)
    delta = pd.parse_pm_review_delta(spec, rounds)          # 구조 스캔 — malformed 없이 성립.
    assert [finding.id for finding, _disposition in delta.accepted] == ["F-001"]

    row = pd._pm_review_parse_verify_row(                   # 실제 행 파서 — 값 그대로 통과.
        _verify_row("F-001", expected=_TOKEN_IN_STRING_FIELD),
    )
    assert row.expected == _TOKEN_IN_STRING_FIELD


def test_requote_touches_only_the_machine_verifiable_slot_inside_the_verify_fence(pd):
    """재-인용 범위 — 채워진 라운드는 한 글자도 바뀌지 않고(fence 밖 산문의 같은 토큰 포함),
    손대지 않은 골격에서는 boolean 값 자리만 임시 재-인용되고 다른 필드는 원문 그대로다."""
    filled = _developer_round_text(pd, [_verify_row("F-001", expected=_TOKEN_IN_STRING_FIELD)])
    filled += '\n산문에도 같은 토큰이 있다: "machine_verifiable":<true|false>\n'
    assert pd._pm_review_requote_verify_placeholder(filled) == filled

    fence = _fill_verify_field(
        _verify_fence_text(pd), "expected", json.dumps(_TOKEN_IN_STRING_FIELD),
    )
    seed = f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n{fence}\n```\n"
    requoted = pd._pm_review_requote_verify_placeholder(seed)
    payload = json.loads(requoted.split("\n", 1)[1].rsplit("```", 1)[0])
    row = payload["verifications"][0]
    assert row["machine_verifiable"] == "<true|false>"   # 구조 스캔용 임시 재-인용.
    assert row["expected"] == _TOKEN_IN_STRING_FIELD     # 다른 필드는 그대로.
