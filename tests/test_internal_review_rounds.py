"""code-reviewer 리뷰 라운드 판정 파서 — 산문 축·블록 축 결합.

판정 입력은 리뷰 라운드 파일 bytes 하나다(`_internal_round_verdict`). 판정 및 중첩 1R/2R
fixture는 PM 홈 `.project_manager/.local/delegate/`의 실제 Codex raw JSONL에서 마지막
`agent_message.text`를 발췌한 것이다. 사용자/절대경로만 일반화했고 판정, 최상위 항목, 2~3칸
probe·근거 불릿 형상은 보존했다. 조립 문자열은 실 보고서 계수 검증에 쓰지 않고, 코드펜스 같은
독립 문법 경계의 단위 테스트에만 쓴다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pm_engine.infra import review_rounds
from pm_engine.domain import review_form, review_state, review_verdict, round_rules


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
FIXTURES = REPO / "tests" / "fixtures"
PASS_REPLY = FIXTURES / "internal_review_pass_reply.txt"
REJECT_REPLY = FIXTURES / "internal_review_reject_reply.txt"
NESTED_ROUND1_REPLY = FIXTURES / "internal_review_nested_round1_reply.txt"
NESTED_ROUND2_REPLY = FIXTURES / "internal_review_nested_round2_reply.txt"
T0656_PROSE_NONE_REPLY = FIXTURES / "internal_review_t0656_prose_none_reply.txt"
T0657_ROUND2_STRUCTURED_PASS_REPLY = (
    FIXTURES / "internal_review_t0657_round2_structured_pass_reply.txt"
)
# 회수된 board 라운드 파일 — 산문 통과 선언 + finding 0건 블록.
T0817_ROUND_FILE = FIXTURES / "internal_round_t0813_pass_both_axes.md"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pd(monkeypatch, tmp_path):
    module = _load(f"pm_delegate_internal_{tmp_path.name}", TOOLS / "pm_delegate.py")
    owner = tmp_path / "pm-home"
    (owner / ".project_manager" / ".local").mkdir(parents=True)
    monkeypatch.setattr(module, "_CONFIG_REPO_OVERRIDE", owner)
    return module


def _reply(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _round_text(path: Path) -> str:
    """회수된 board 라운드 파일 bytes(fixture)."""
    return path.read_text(encoding="utf-8")


def _review_round(pd, text: str):
    """판정 입력 라운드 하나 — 리뷰 라운드 파일 bytes 를 그대로 싣는다."""
    return round_rules.Round(
        ordinal=1,
        role=review_form.INTERNAL_REVIEW_ROLE,
        path=Path(round_rules.round_filename(1, review_form.INTERNAL_REVIEW_ROLE)),
        text=text,
        pending=False,
    )


def test_real_reviewer_reply_fixtures_drive_verdict_and_must_fix(pd):
    rejected = review_verdict._internal_reply_outcome(_reply(REJECT_REPLY))
    passed = review_verdict._internal_reply_outcome(_reply(PASS_REPLY))

    assert rejected.verdict == 1
    assert len(rejected.must_fix_items) == 1
    assert rejected.must_fix_items[0].startswith("실제 손편집 manifest")
    assert passed == review_verdict.InternalReplyOutcome(0, [])


def test_t0658_real_reply_fixtures_keep_prose_none_unknown_and_reject_list_counted(pd):
    """T-0656 실물은 계속 unknown이고, T-0651 실 반려 목록은 정상 계수한다."""
    prose_none = _reply(T0656_PROSE_NONE_REPLY)
    rejected = review_verdict._internal_reply_assessment(_reply(NESTED_ROUND1_REPLY))
    passed = review_verdict._internal_reply_assessment(prose_none)

    assert review_verdict._extract_internal_must_fix_items(prose_none) is None
    assert passed.outcome == review_verdict.InternalReplyOutcome(None, None)
    assert passed.diagnostic.code == review_verdict.INTERNAL_DIAGNOSTIC_PASS_WITHOUT_ZERO
    assert "must-fix 0건 절 부재" in passed.diagnostic.message
    assert rejected.outcome.verdict == 1
    assert len(rejected.outcome.must_fix_items) == 6
    assert rejected.diagnostic is None


def test_t0658_fix1_real_structured_pass_reply_is_zero_verdict_and_must_fix(pd):
    """T-0657 2R 실 reply의 bare 절이 `확인 결과`에서 닫혀 통과 0건으로 판정된다."""
    reply = _reply(T0657_ROUND2_STRUCTURED_PASS_REPLY)
    external = pd._load_additional_reviewer()

    assert review_verdict.verdict_words(reply) == ("통과",)
    assert review_verdict._extract_internal_must_fix_items(reply) == []
    assert review_verdict._internal_reply_outcome(reply) == review_verdict.InternalReplyOutcome(0, [])


@pytest.mark.parametrize(
    ("reply", "code", "reason"),
    [
        pytest.param(
            "검토는 완료됐습니다.",
            "missing-verdict-word",
            "판정 낱말 없음",
            id="missing-verdict-word",
        ),
        pytest.param(
            "판정: 통과\n판정: 반려\n\n## must-fix\n- 없음\n",
            "conflicting-verdict-words",
            "판정 낱말 상충",
            id="conflicting-pass-reject-words",
        ),
        pytest.param(
            "판정: 통과\n판정: MAYBE\n\n## must-fix\n- 없음\n",
            "conflicting-verdict-words",
            "판정 낱말 상충",
            id="conflicting-unknown-word",
        ),
        pytest.param(
            None,
            "pass-without-zero-must-fix",
            "통과 선언인데 must-fix 0건 절 부재",
            id="pass-without-zero-must-fix",
        ),
        pytest.param(
            "판정: 반려\n\n## must-fix\n검토 결과만 있고 목록은 비어 있습니다.\n",
            "reject-without-must-fix-items",
            "반려 선언인데 must-fix 항목 없음",
            id="reject-without-must-fix-items",
        ),
    ],
)
def test_t0658_four_diagnostics_reach_the_completion_judgement(pd, reply, code, reason):
    """4종 실패 각각 리뷰 라운드 완료 판정 사유에 원인과 재리뷰 처방을 보존한다."""
    actual_reply = _reply(T0656_PROSE_NONE_REPLY) if reply is None else reply

    assert review_verdict._internal_round_verdict(actual_reply).diagnostic.code == code
    problem = review_state.review_completion_problem(
        "---\nid: T-DIAG\n---\n# T-DIAG\n", [_review_round(pd, actual_reply)])

    assert problem is not None
    assert "판정 추출 진단" in problem
    assert reason in problem and "재리뷰 시" in problem


@pytest.mark.parametrize("absence", ["- 없음", "- 해당 없음"])
def test_list_shaped_explicit_absence_is_zero_must_fix(pd, absence):
    reply = f"판정: 통과\n\n## must-fix\n{absence}\n"
    assert review_verdict._internal_reply_outcome(reply) == review_verdict.InternalReplyOutcome(0, [])


def test_real_nested_reviewer_rounds_count_top_level_six_to_two(pd):
    """실 T-0651 1R/2R reply의 하위 probe를 세지 않아 정상 수렴 6→2를 보존한다."""
    first = review_verdict._internal_reply_outcome(_reply(NESTED_ROUND1_REPLY))
    second = review_verdict._internal_reply_outcome(_reply(NESTED_ROUND2_REPLY))

    assert first.verdict == second.verdict == 1
    assert len(first.must_fix_items) == 6
    assert len(second.must_fix_items) == 2


def test_pm_verified_still_refuses_the_finding_zero_review_round(pd):
    """복구된 통과 라운드도 `--pm-verified` 는 그대로 거부한다(그 거부는 무변경이다)."""
    recovered = round_rules.Round(
        ordinal=2,
        role=review_form.INTERNAL_REVIEW_ROLE,
        path=Path(round_rules.round_filename(2, review_form.INTERNAL_REVIEW_ROLE)),
        text=_round_text(T0817_ROUND_FILE),
        pending=False,
    )

    problem = review_state.pm_verified_evidence_problem(
        "---\nid: T-0817\n---\n# T-0817\n",
        [recovered],
        reviewer_role=review_form.INTERNAL_REVIEW_ROLE,
        surface_floor=0,
    )

    assert problem is not None
    assert "finding-zero 리뷰 라운드" in problem


def test_legacy_unfingerprinted_fixed_resolution_is_unrecognized(pd):
    gate = {
        "rounds": [{
            "sequence": 1, "verdict": 1, "must_fix": 1,
            "started_at": "2026-08-12T00:00:00+00:00",
            "ts": "2026-08-12T00:01:00+00:00",
        }],
        "resolution": {
            "kind": "fixed",
            "evidence_gate": "T-LEGACY-PASS",
            "round_sequence": 1,
            "rounds": 1,
        },
    }
    assert review_rounds.gate_resolution(gate) is None
    problem = review_rounds._gate_disposition_problem("T-LEGACY-BLOCK", gate)
    assert problem is not None and "처분 선언 없음" in problem


@pytest.mark.parametrize(
    "argv",
    [
        ["delete", "--gate", "T-0651"],
        ["recalculate", "--gate", "T-0651", "--must-fix", "0"],
        ["recalculate", "--gate", "T-0651", "--delete"],
        ["resolve", "--gate", "T-0651", "--into", "T-0654", "--delete"],
        ["resolve", "--gate", "T-0651", "--fixed", "T-0654", "--must-fix", "0"],
    ],
)
def test_rounds_cli_has_no_destructive_or_direct_value_surface(pd, argv):
    with pytest.raises(SystemExit) as exc:
        pd._cmd_rounds(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param("## must-fix\n\n## next", None, id="empty-section"),
        pytest.param("## must-fix\n- 없음\n", [], id="explicit-none"),
        pytest.param(
            "## must-fix\n1. 첫째\n2. 둘째\n", ["첫째", "둘째"],
            id="numbered-top-level",
        ),
        pytest.param(
            "## must-fix\n```text\n- 코드 속 가짜 항목\n## 코드 속 가짜 절\n```\n"
            "1. 실제 항목\n   - 하위 근거\n\n| probe | result |\n|---|---|\n"
            "| nested | ignored |\n## next\n",
            [
                "실제 항목 - 하위 근거 | probe | result | |---|---| "
                "| nested | ignored |"
            ],
            id="fence-nested-bullet-and-table",
        ),
    ],
)
def test_must_fix_counting_boundaries(pd, reply, expected):
    assert review_verdict._extract_internal_must_fix_items(reply) == expected


@pytest.mark.parametrize(
    ("boundary", "reply", "expected"),
    [
        pytest.param(
            "markdown-heading",
            "## must-fix\n- 없음\n\n## 확인 결과\n- 후속 확인\n",
            [],
            id="markdown-heading",
        ),
        pytest.param(
            "bold-heading",
            "## must-fix\n1. 실제 결함\n\n**확인 결과**\n- 후속 확인\n",
            ["실제 결함"],
            id="bold-heading",
        ),
        pytest.param(
            "plain-after-blank",
            "must-fix\n\n- 없음\n\n확인 결과\n\n- 후속 확인\n",
            [],
            id="plain-after-blank",
        ),
        pytest.param(
            "table",
            "## must-fix\n1. 첫째\n\n| probe | result |\n|---|---|\n| A | red |\n\n2. 둘째\n",
            ["첫째 | probe | result | |---|---| | A | red |", "둘째"],
            id="table-is-body",
        ),
        pytest.param(
            "code-fence",
            "## must-fix\n\n```text\n- 가짜 항목\n## 가짜 제목\n```\n1. 실제 항목\n",
            ["실제 항목"],
            id="code-fence-is-body",
        ),
        pytest.param(
            "document-end",
            "## must-fix\n1. 첫째\n   - 하위 근거\n2. 둘째\n",
            ["첫째 - 하위 근거", "둘째"],
            id="continues-to-document-end",
        ),
    ],
)
def test_t0658_fix1_must_fix_section_end_matrix(pd, boundary, reply, expected):
    """절 종료만 교정하고 표/fence/문서끝 및 최소 들여쓰기 계수 단위는 보존한다."""
    assert boundary
    assert review_verdict._extract_internal_must_fix_items(reply) == expected


def test_t0658_fix1_canonical_markdown_format_judges_pass_and_reject(pd):
    """preamble 표준 `## must-fix` 형식의 통과·반려가 각각 0/1로 판정된다."""
    passed = review_verdict._internal_reply_outcome(
        "판정: 통과\n\n## must-fix\n\n- 없음\n\n## 확인 결과\n- 통과 근거\n")
    rejected = review_verdict._internal_reply_outcome(
        "판정: 반려\n\n## must-fix\n\n1. 실제 수정 항목\n\n## 확인 결과\n- 반려 근거\n")

    assert (passed.verdict, len(passed.must_fix_items)) == (0, 0)
    assert (rejected.verdict, len(rejected.must_fix_items)) == (1, 1)


@pytest.mark.parametrize(
    "reply",
    [
        "회신 추출은 성공했지만 형식 없는 검토 결과입니다.",
        "판정: 통과\n\n검토 완료",  # must_fix_items 부재는 통과가 아니다.
        "판정: 통과\n\n### must-fix\n\n1. 아직 결함",  # 모순 판정
        None,
    ],
)
def test_terminal_judgement_failure_is_unknown_not_zero(pd, reply):
    assert review_verdict._internal_reply_outcome(reply) == review_verdict.InternalReplyOutcome(None, None)


def test_resolve_offers_only_pm_verified(pd, capsys):
    """내부 처분 표면은 기계 확인 하나뿐이다."""
    with pytest.raises(SystemExit) as exc:
        pd._cmd_rounds(["resolve", "--help"])

    assert exc.value.code == 0
    out = "".join(capsys.readouterr().out.split())
    assert "--pm-verified" in out
    assert "--into" not in out and "--fixed" not in out
    assert "--pm-fixed" not in out
