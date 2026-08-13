"""T-0651 내부 code-reviewer 라운드 장부.

판정 및 중첩 1R/2R fixture는 PM 홈 `.project_manager/.local/delegate/`의 실제 Codex raw
JSONL에서 마지막 `agent_message.text`를 발췌한 것이다. 사용자/절대경로만 일반화했고 판정,
최상위 항목, 2~3칸 probe·근거 불릿 형상은 보존했다. 조립 문자열은 실 보고서 계수 검증에 쓰지
않고, 코드펜스 같은 독립 문법 경계의 단위 테스트에만 쓴다.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


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
_DIFF_A = "sha256:" + "a" * 64
_DIFF_B = "sha256:" + "b" * 64
_DIFF_C = "sha256:" + "c" * 64


def _pm_fixed_evidence() -> str:
    return (
        "change=tests/test_internal_review_rounds.py:1; "
        "regression=pytest tests/test_internal_review_rounds.py -q; "
        "result=rc=0 (targeted regression passed)"
    )


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


def _consume(
    pd,
    gate: str,
    reply: str | None,
    *,
    raw_ids=("raw-1",),
    spawned=(True,),
    confirm_fix=False,
    diff_fingerprint=_DIFF_A,
):
    evidence = pd._preview_internal_confirm_fix_evidence(gate) if confirm_fix else None
    budget = pd._reserve_internal_review_round(
        gate,
        confirm_fix=confirm_fix,
        wall_timeout_sec=60,
        target_rev="deadbeef",
        diff_fingerprint=diff_fingerprint,
        expected_confirm_evidence=evidence,
    )
    if budget.refused_rc is not None:
        return budget, None
    trace = pd.InternalRoundTrace(budget)
    for index, record_id in enumerate(raw_ids):
        trace.start_attempt(record_id)
        trace.finish_attempt(
            {} if spawned[index] else {pd.RUN_RESULT_LAUNCH_FAILED: True},
            reply if index == len(raw_ids) - 1 else None,
        )
    pd._finish_internal_review_round(budget, trace)
    return budget, trace


def _entry(pd, gate: str) -> dict:
    data = json.loads(pd._internal_round_ledger_path().read_text(encoding="utf-8"))
    return data[gate]


def _codex_stdout(reply: str) -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": reply},
        }),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
    ])


def _raw_record(
    pd,
    *,
    record_id: str,
    gate: str,
    round_id: str,
    reply: str | None,
) -> dict:
    raw_dir, _ledger_path = pd._raw_storage(None)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{record_id}.txt"
    if reply is not None:
        raw_path.write_text(
            pd._format_meta(
                ["codex"], 0, "codex", "review-model", 0.1,
                _codex_stdout(reply), "",
            ),
            encoding="utf-8",
        )
    return {
        "id": record_id,
        "surface": "delegate",
        "harness": "codex",
        "model": "review-model",
        "role": "code-reviewer",
        "attempt": "primary",
        "pid": 1,
        "started_at": "2026-08-11T00:00:00+00:00",
        "finished_at": "2026-08-11T00:01:00+00:00",
        "raw_path": str(raw_path.resolve()),
        pd.INTERNAL_GATE_FIELD: gate,
        pd.INTERNAL_ROUND_ID_FIELD: round_id,
    }


def _write_raw_ledger(pd, records: list[dict]) -> Path:
    _raw_dir, ledger_path = pd._raw_storage(None)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({"version": 1, "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return ledger_path


def _seed_pm_ticket(pd, tid: str, *, status: str = "claimed") -> Path:
    owner = pd._CONFIG_REPO_OVERRIDE
    path = (
        owner / ".project_manager" / "wiki" / "tickets" / status
        / f"{tid}-fixture.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {tid}\n"
        f"status: {status}\n"
        "title: fixture\n"
        "---\n",
        encoding="utf-8",
    )
    return path


def test_real_reviewer_reply_fixtures_drive_verdict_and_must_fix(pd):
    rejected = pd._internal_reply_outcome(_reply(REJECT_REPLY))
    passed = pd._internal_reply_outcome(_reply(PASS_REPLY))

    assert rejected.verdict == 1
    assert len(rejected.must_fix_items) == 1
    assert rejected.must_fix_items[0].startswith("실제 손편집 manifest")
    assert passed == pd.InternalReplyOutcome(0, [])


def test_t0658_real_reply_fixtures_keep_prose_none_unknown_and_reject_list_counted(pd):
    """T-0656 실물은 계속 unknown이고, T-0651 실 반려 목록은 정상 계수한다."""
    prose_none = _reply(T0656_PROSE_NONE_REPLY)
    rejected = pd._internal_reply_assessment(_reply(NESTED_ROUND1_REPLY))
    passed = pd._internal_reply_assessment(prose_none)

    assert pd._extract_internal_must_fix_items(prose_none) is None
    assert passed.outcome == pd.InternalReplyOutcome(None, None)
    assert passed.diagnostic.code == pd.INTERNAL_DIAGNOSTIC_PASS_WITHOUT_ZERO
    assert "must-fix 0건 절 부재" in passed.diagnostic.message
    assert rejected.outcome.verdict == 1
    assert len(rejected.outcome.must_fix_items) == 6
    assert rejected.diagnostic is None


def test_t0658_fix1_real_structured_pass_reply_records_zero_verdict_and_must_fix(pd):
    """T-0657 2R 실 reply의 bare 절이 `확인 결과`에서 닫혀 통과 0건으로 기록된다."""
    reply = _reply(T0657_ROUND2_STRUCTURED_PASS_REPLY)
    external = pd._load_external_review()

    assert external.verdict_words(reply) == ("통과",)
    assert pd._extract_internal_must_fix_items(reply) == []
    assert pd._internal_reply_outcome(reply) == pd.InternalReplyOutcome(0, [])

    gate = "T-0657"
    _consume(pd, gate, reply, raw_ids=("raw-t0657-round2",))
    entry = _entry(pd, gate)
    assert entry["rounds"][0]["verdict"] == 0
    assert entry["rounds"][0]["must_fix"] == 0
    assert entry["records"][0]["must_fix_items"] == []


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
def test_t0658_four_diagnostics_reach_stderr_ledger_and_completion_gate(
    pd, monkeypatch, capsys, reply, code, reason,
):
    """4종 실패 각각 stderr→장부→board complete 안내에 원인과 재리뷰 처방을 보존한다."""
    actual_reply = _reply(T0656_PROSE_NONE_REPLY) if reply is None else reply
    gate = f"T-DIAG-{code.upper()}"

    _consume(pd, gate, actual_reply, raw_ids=(f"raw-{code}",))
    err = capsys.readouterr().err
    entry = _entry(pd, gate)
    stored = entry["rounds"][0][pd.INTERNAL_VERDICT_DIAGNOSTIC_FIELD]

    assert "내부 리뷰 판정 추출 실패" in err
    assert reason in err and "재리뷰 시" in err
    assert stored["code"] == code
    assert reason in stored["message"] and "재리뷰 시" in stored["message"]

    board = pd._load_board()
    monkeypatch.setattr(board, "_internal_review_rounds_ledger",
                        pd._internal_round_ledger_path)
    monkeypatch.setattr(board, "_ticket_search_dirs", lambda: [])
    monkeypatch.setattr(board, "_rel_to_repo", lambda path: str(path))
    problem = board._internal_review_completion_problem(gate)

    assert problem is not None
    assert "판정 추출 진단" in problem
    assert reason in problem and "재리뷰 시" in problem


@pytest.mark.parametrize("absence", ["- 없음", "- 해당 없음"])
def test_list_shaped_explicit_absence_is_zero_must_fix(pd, absence):
    reply = f"판정: 통과\n\n## must-fix\n{absence}\n"
    assert pd._internal_reply_outcome(reply) == pd.InternalReplyOutcome(0, [])


def test_real_nested_reviewer_rounds_count_top_level_six_to_two(pd):
    """실 T-0651 1R/2R reply의 하위 probe를 세지 않아 정상 수렴 6→2를 보존한다."""
    first = pd._internal_reply_outcome(_reply(NESTED_ROUND1_REPLY))
    second = pd._internal_reply_outcome(_reply(NESTED_ROUND2_REPLY))

    assert first.verdict == second.verdict == 1
    assert len(first.must_fix_items) == 6
    assert len(second.must_fix_items) == 2
    gate = "T-NESTED-001"
    _consume(pd, gate, _reply(NESTED_ROUND1_REPLY))
    _consume(pd, gate, _reply(NESTED_ROUND2_REPLY))
    entry = _entry(pd, gate)
    assert [row["must_fix"] for row in entry["rounds"]] == [6, 2]
    common = pd._load_review_rounds()
    assert common.recorded_must_fix_series(entry) == (6, 2)
    assert common.convergence_refusal(entry, 3, wall_timeout_sec=60) is None


def test_recalculate_actual_ledger_file_repairs_false_divergence_and_opens_next_round(
    pd, capsys,
):
    """실 장부/실 raw 파일 형상에서 오계측 15→16을 현행 parser의 6→2로 복구한다."""
    gate = "T-0651"
    _consume(
        pd, gate, _reply(NESTED_ROUND1_REPLY), raw_ids=("raw-round-1",),
    )
    _consume(
        pd, gate, _reply(NESTED_ROUND2_REPLY), raw_ids=("raw-round-2",),
    )
    entry = _entry(pd, gate)
    entry["rounds"][0]["must_fix"] = 15
    entry["rounds"][1]["must_fix"] = 16
    entry["records"][0]["must_fix_items"] = ["오계측"] * 15
    entry["records"][1]["must_fix_items"] = ["오계측"] * 16
    pd._save_internal_round_ledger({gate: entry})
    raw_rows = [
        _raw_record(
            pd,
            record_id=outcome["outcome_record_id"],
            gate=gate,
            round_id=outcome["id"],
            reply=_reply(reply_path),
        )
        for outcome, reply_path in zip(
            entry["rounds"],
            (NESTED_ROUND1_REPLY, NESTED_ROUND2_REPLY),
            strict=True,
        )
    ]
    _write_raw_ledger(pd, raw_rows)

    assert pd._cmd_rounds(["recalculate", "--gate", gate]) == 0
    output = capsys.readouterr().out

    assert "before=15 → 16" in output
    assert "after=6 → 2" in output
    repaired = _entry(pd, gate)
    assert [row["verdict"] for row in repaired["rounds"]] == [1, 1]
    assert [row["must_fix"] for row in repaired["rounds"]] == [6, 2]
    assert [
        row[pd.INTERNAL_RECALCULATION_FIELD]["status"]
        for row in repaired["rounds"]
    ] == [pd.INTERNAL_RECALCULATION_OK, pd.INTERNAL_RECALCULATION_OK]
    assert pd._load_review_rounds().convergence_refusal(
        repaired, pd.INTERNAL_REVIEW_ROUNDS_MAX, wall_timeout_sec=60,
    ) is None
    next_round = pd._reserve_internal_review_round(
        gate,
        confirm_fix=False,
        wall_timeout_sec=60,
        target_rev="after-recalculation",
        expected_confirm_evidence=None,
    )
    assert next_round.refused_rc is None
    assert next_round.reserved


def test_recalculate_missing_record_and_unreadable_reply_stay_unknown(pd):
    gate = "T-0654"
    _consume(
        pd, gate, _reply(REJECT_REPLY), raw_ids=("missing-record",),
    )
    _consume(
        pd, gate, _reply(REJECT_REPLY), raw_ids=("missing-file",),
    )
    entry = _entry(pd, gate)
    entry["rounds"][0]["must_fix"] = 91
    entry["rounds"][1]["must_fix"] = 92
    pd._save_internal_round_ledger({gate: entry})
    missing_file_row = _raw_record(
        pd,
        record_id="missing-file",
        gate=gate,
        round_id=entry["rounds"][1]["id"],
        reply=None,
    )
    _write_raw_ledger(pd, [missing_file_row])

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == (91, 92)
    assert report.after == (None, None)
    repaired = _entry(pd, gate)
    assert [row["verdict"] for row in repaired["rounds"]] == [None, None]
    assert [row["must_fix"] for row in repaired["rounds"]] == [None, None]
    assert [row["must_fix_items"] for row in repaired["records"]] == [None, None]
    statuses = [
        row[pd.INTERNAL_RECALCULATION_FIELD]
        for row in repaired["rounds"]
    ]
    assert [row["status"] for row in statuses] == [
        pd.INTERNAL_RECALCULATION_UNKNOWN,
        pd.INTERNAL_RECALCULATION_UNKNOWN,
    ]
    assert "raw 레코드 부재" in statuses[0]["detail"]
    assert "FileNotFoundError" in statuses[1]["detail"]


def test_recalculate_recomputes_verdict_from_raw_reply(pd):
    gate = "T-VERDICT-001"
    _consume(pd, gate, _reply(REJECT_REPLY), raw_ids=("pass-source",))
    entry = _entry(pd, gate)
    _write_raw_ledger(pd, [
        _raw_record(
            pd,
            record_id="pass-source",
            gate=gate,
            round_id=entry["rounds"][0]["id"],
            reply=_reply(PASS_REPLY),
        )
    ])

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == (1,)
    assert report.after == (0,)
    repaired = _entry(pd, gate)
    assert repaired["rounds"][0]["verdict"] == 0
    assert repaired["rounds"][0]["must_fix"] == 0
    assert repaired["records"][0]["verdict"] is True
    assert repaired["records"][0]["must_fix_items"] == []


def test_rounds_resolve_into_records_current_binding_and_rejects_self(pd, capsys):
    gate = "T-RESOLVE-001"
    target = "T-RESOLVE-002"
    _consume(pd, gate, _reply(REJECT_REPLY), raw_ids=("reject-source",))
    _seed_pm_ticket(pd, target)

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--into", gate,
    ]) == 1
    assert _entry(pd, gate).get("resolution") is None
    assert "자기 자신" in capsys.readouterr().err

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--into", target,
    ]) == 0
    declared = _entry(pd, gate)["resolution"]
    assert declared["kind"] == pd._load_board().GATE_RESOLUTION_INTO
    assert declared["ticket"] == target
    assert declared["round_sequence"] == 1
    assert declared["rounds"] == 1
    assert declared["must_fix"] == 1


def test_rounds_resolve_fixed_requires_later_changed_pass(pd, capsys):
    gate = "T-FIXED-001"
    rejected_evidence = "T-FIXED-002"
    unchanged_evidence = "T-FIXED-003"
    passed_evidence = "T-FIXED-004"
    _consume(
        pd, gate, _reply(REJECT_REPLY), raw_ids=("blocked",),
        diff_fingerprint=_DIFF_A,
    )
    _consume(
        pd, rejected_evidence, _reply(REJECT_REPLY), raw_ids=("still-rejected",),
        diff_fingerprint=_DIFF_B,
    )
    _consume(
        pd, unchanged_evidence, _reply(PASS_REPLY), raw_ids=("unchanged-pass",),
        diff_fingerprint=_DIFF_A,
    )
    _consume(
        pd, passed_evidence, _reply(PASS_REPLY), raw_ids=("changed-pass",),
        diff_fingerprint=_DIFF_C,
    )
    ledger = json.loads(
        pd._internal_round_ledger_path().read_text(encoding="utf-8")
    )
    coordinates = {
        gate: ("2026-08-12T00:00:00+00:00", "2026-08-12T00:01:00+00:00", "a" * 40),
        rejected_evidence: (
            "2026-08-12T00:02:00+00:00", "2026-08-12T00:03:00+00:00", "b" * 40,
        ),
        unchanged_evidence: (
            "2026-08-12T00:04:00+00:00", "2026-08-12T00:05:00+00:00", "c" * 40,
        ),
        passed_evidence: (
            "2026-08-12T00:06:00+00:00", "2026-08-12T00:07:00+00:00", "d" * 40,
        ),
    }
    for gate_id, (started_at, ts, target_rev) in coordinates.items():
        ledger[gate_id]["rounds"][0].update({
            "started_at": started_at,
            "ts": ts,
            "target_rev": target_rev,
        })
    pd._save_internal_round_ledger(ledger)

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--fixed", rejected_evidence,
    ]) == 1
    assert _entry(pd, gate).get("resolution") is None
    assert "통과가 아닙니다" in capsys.readouterr().err

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--fixed", unchanged_evidence,
    ]) == 1
    assert _entry(pd, gate).get("resolution") is None
    assert "같은 대상 diff fingerprint" in capsys.readouterr().err

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--fixed", passed_evidence,
    ]) == 0
    declared = _entry(pd, gate)["resolution"]
    assert declared["kind"] == pd._load_board().GATE_RESOLUTION_FIXED
    assert declared["evidence_gate"] == passed_evidence
    assert declared["round_sequence"] == 1
    assert declared["blocked_diff_fingerprint"] == _DIFF_A
    assert declared["evidence_diff_fingerprint"] == _DIFF_C


def test_internal_round_records_dirty_diff_fingerprint_next_to_target_rev(pd):
    budget, _trace = _consume(
        pd, "T-FINGERPRINT-001", _reply(PASS_REPLY),
        diff_fingerprint=_DIFF_B,
    )
    entry = _entry(pd, "T-FINGERPRINT-001")

    assert budget.target_rev == "deadbeef"
    assert budget.diff_fingerprint == _DIFF_B
    assert entry["records"][0]["target_rev"] == "deadbeef"
    assert entry["records"][0]["diff_fingerprint"] == _DIFF_B
    assert entry["rounds"][0]["target_rev"] == "deadbeef"
    assert entry["rounds"][0]["diff_fingerprint"] == _DIFF_B


def test_internal_fixed_rejects_unfingerprinted_old_rounds_for_new_declaration(
    pd, capsys,
):
    gate, evidence = "T-OLD-001", "T-OLD-002"
    _consume(pd, gate, _reply(REJECT_REPLY), diff_fingerprint=None)
    _consume(pd, evidence, _reply(PASS_REPLY), diff_fingerprint=None)
    ledger = json.loads(pd._internal_round_ledger_path().read_text(encoding="utf-8"))
    ledger[gate]["rounds"][0].update({
        "started_at": "2026-08-12T00:00:00+00:00",
        "ts": "2026-08-12T00:01:00+00:00",
    })
    ledger[evidence]["rounds"][0].update({
        "started_at": "2026-08-12T00:02:00+00:00",
        "ts": "2026-08-12T00:03:00+00:00",
    })
    pd._save_internal_round_ledger(ledger)

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--fixed", evidence,
    ]) == 1
    assert _entry(pd, gate).get("resolution") is None
    error = capsys.readouterr().err
    assert "diff fingerprint" in error and "확인할 수 없습니다" in error


def test_legacy_unfingerprinted_fixed_resolution_is_distinguished_and_preserved(pd):
    board = pd._load_board()
    gate = {
        "rounds": [{
            "sequence": 1, "verdict": 1, "must_fix": 1,
            "started_at": "2026-08-12T00:00:00+00:00",
            "ts": "2026-08-12T00:01:00+00:00",
        }],
        "resolution": {
            "kind": board.GATE_RESOLUTION_FIXED,
            "evidence_gate": "T-LEGACY-PASS",
            "round_sequence": 1,
            "rounds": 1,
        },
    }
    evidence = {"rounds": [{
        "sequence": 1, "verdict": 0, "must_fix": 0,
        "started_at": "2026-08-12T00:02:00+00:00",
        "ts": "2026-08-12T00:03:00+00:00",
    }]}
    declared = board.gate_resolution(gate)

    assert board.internal_fixed_proof_status(declared) == "legacy-unverifiable"
    assert board.internal_gate_evidence_problem(gate, evidence) is not None
    assert board._gate_disposition_problem(
        "T-LEGACY-BLOCK", gate, {"T-LEGACY-PASS": evidence}, [],
        evidence_problem=board.internal_gate_evidence_problem,
        allow_legacy_internal_fixed=True,
    ) is None


def test_internal_diff_fingerprint_changes_with_dirty_content_and_fails_closed(pd, tmp_path):
    target = tmp_path / "tracked.py"
    target.write_text("one\n", encoding="utf-8")

    def audit(digest):
        state = argparse.Namespace(
            head="1" * 40,
            entries=(argparse.Namespace(code=" M", path="tracked.py"),),
            digests=() if digest is None else (("tracked.py", digest),),
            modes=(("tracked.py", "N...:100644"),),
        )
        return argparse.Namespace(before=state, workspace=tmp_path)

    first = pd._internal_diff_fingerprint(audit(
        pd.hashlib.sha256(target.read_bytes()).hexdigest()
    ))
    target.write_text("two\n", encoding="utf-8")
    second = pd._internal_diff_fingerprint(audit(
        pd.hashlib.sha256(target.read_bytes()).hexdigest()
    ))
    assert first is not None and first.startswith("sha256:")
    assert second is not None and second != first
    assert pd._internal_diff_fingerprint(audit(None)) is None


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
    assert pd._extract_internal_must_fix_items(reply) == expected


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
    assert pd._extract_internal_must_fix_items(reply) == expected


def test_t0658_fix1_canonical_markdown_format_records_pass_and_reject(pd):
    """preamble 표준 `## must-fix` 형식의 통과·반려가 장부에 각각 0/1로 기록된다."""
    replies = {
        "T-FORMAT-PASS": (
            "판정: 통과\n\n## must-fix\n\n- 없음\n\n## 확인 결과\n- 통과 근거\n"
        ),
        "T-FORMAT-REJECT": (
            "판정: 반려\n\n## must-fix\n\n1. 실제 수정 항목\n\n## 확인 결과\n- 반려 근거\n"
        ),
    }

    for gate, reply in replies.items():
        _consume(pd, gate, reply, raw_ids=(f"raw-{gate}",))

    passed = _entry(pd, "T-FORMAT-PASS")
    rejected = _entry(pd, "T-FORMAT-REJECT")
    assert (passed["rounds"][0]["verdict"], passed["rounds"][0]["must_fix"]) == (0, 0)
    assert (rejected["rounds"][0]["verdict"], rejected["rounds"][0]["must_fix"]) == (1, 1)


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
    assert pd._internal_reply_outcome(reply) == pd.InternalReplyOutcome(None, None)


def test_reservation_finish_links_all_attempts_to_one_terminal_outcome(pd):
    budget, _trace = _consume(
        pd,
        "T-0651",
        _reply(REJECT_REPLY),
        raw_ids=("resume-id", "fresh-id", "fallback-id"),
        spawned=(True, True, True),
    )
    entry = _entry(pd, "T-0651")

    assert budget.sequence == 1
    assert entry["count"] == 1
    assert len(entry["records"]) == len(entry["rounds"]) == 1
    assert entry["records"][0]["raw_record_ids"] == [
        "resume-id", "fresh-id", "fallback-id",
    ]
    assert entry["rounds"][0]["raw_record_ids"] == [
        "resume-id", "fresh-id", "fallback-id",
    ]
    assert entry["rounds"][0]["outcome_record_id"] == "fallback-id"
    assert entry["rounds"][0]["verdict"] == 1
    assert entry["rounds"][0]["must_fix"] == 1


def test_all_attempts_failing_before_spawn_refunds_the_round(pd):
    budget, _trace = _consume(
        pd,
        "T-0652",
        None,
        raw_ids=("primary-launch", "fallback-launch"),
        spawned=(False, False),
    )
    entry = _entry(pd, "T-0652")

    assert budget.reserved
    assert entry["count"] == 0
    assert entry["records"] == []
    assert entry["rounds"] == []


def test_one_spawned_attempt_consumes_even_when_terminal_attempt_did_not_spawn(pd):
    _budget, _trace = _consume(
        pd,
        "T-0653",
        None,
        raw_ids=("spawned-primary", "launch-failed-fallback"),
        spawned=(True, False),
    )
    entry = _entry(pd, "T-0653")

    assert entry["count"] == 1
    assert entry["rounds"][0]["raw_record_ids"] == [
        "spawned-primary", "launch-failed-fallback",
    ]
    assert entry["rounds"][0]["outcome_record_id"] == "launch-failed-fallback"
    assert entry["rounds"][0]["verdict"] is None
    assert entry["rounds"][0]["must_fix"] is None


def test_cap_three_and_one_confirm_fix_exception_matrix(pd, capsys):
    gate = "T-0654"
    for _ in range(3):
        budget, _ = _consume(pd, gate, _reply(REJECT_REPLY))
        assert budget.refused_rc is None

    blocked, _ = _consume(pd, gate, _reply(REJECT_REPLY))
    assert blocked.refused_rc == 1
    refusal = capsys.readouterr().err
    assert "재설계" in refusal
    assert "rounds recalculate --gate T-0654" in refusal

    confirmation, _ = _consume(
        pd, gate, _reply(PASS_REPLY), confirm_fix=True,
    )
    assert confirmation.refused_rc is None
    entry = _entry(pd, gate)
    assert entry["count"] == 4
    assert entry["confirm_fix"] == 1
    assert entry["rounds"][-1]["verdict"] == 0

    second, _ = _consume(pd, gate, _reply(PASS_REPLY), confirm_fix=True)
    assert second.refused_rc == 1
    assert _entry(pd, gate)["confirm_fix"] == 1


def test_t0663_shape_pm_fixed_opens_completion_and_is_distinctly_reported(
    pd, monkeypatch, capsys,
):
    """상한 3+confirm-fix 소진·마지막 반려·raw 결속 없는 손기입 형상을 정식 처분으로 닫는다."""
    gate = "T-0663"
    for _index in range(pd.INTERNAL_REVIEW_ROUNDS_MAX):
        budget, _trace = _consume(pd, gate, _reply(REJECT_REPLY))
        assert budget.refused_rc is None
    confirmation, _trace = _consume(
        pd, gate, _reply(REJECT_REPLY), confirm_fix=True,
    )
    assert confirmation.refused_rc is None

    # 실 손기입 라운드와 같은 내구성: outcome_record_id가 양쪽 레코드에서 없어도 처분/판정은
    # 라운드 산출 사실만 소비하며 raw 재계산 축과 섞이지 않는다.
    ledger = json.loads(pd._internal_round_ledger_path().read_text(encoding="utf-8"))
    for row in ledger[gate]["rounds"] + ledger[gate]["records"]:
        row.pop("outcome_record_id", None)
    pd._save_internal_round_ledger(ledger)

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--pm-fixed", _pm_fixed_evidence(),
    ]) == 0
    declaration = capsys.readouterr().out
    entry = _entry(pd, gate)
    common = pd._load_review_rounds()
    assert entry[common.PM_FIXED_USAGE_KEY] == 1
    assert entry["resolution"]["kind"] == common.RESOLUTION_PM_FIXED
    assert "pm-fixed(PM 직접 해소·리뷰 통과 아님" in declaration

    assert pd._cmd_rounds(["--rounds-report", "--gate", gate]) == 0
    report = capsys.readouterr().out
    assert "처분=pm-fixed(PM 직접 해소·리뷰 통과 아님" in report
    assert "판정=1(비통과)" in report

    board = pd._load_board()
    monkeypatch.setattr(
        board, "_internal_review_rounds_ledger", pd._internal_round_ledger_path,
    )
    monkeypatch.setattr(board, "_ticket_search_dirs", lambda: [])
    assert board._internal_review_completion_problem(gate) is None
    completion_output = capsys.readouterr().err
    assert "완료 증거는 리뷰 통과가 아니라 pm-fixed" in completion_output
    assert "변경 tests/test_internal_review_rounds.py:1" in completion_output

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--pm-fixed", _pm_fixed_evidence(),
    ]) == 1
    assert "게이트당 1회 제한을 이미 소진" in capsys.readouterr().err


def test_pm_fixed_refuses_before_round_cap_even_after_confirm_fix(pd, capsys):
    gate = "T-PMF-001"
    _consume(pd, gate, _reply(REJECT_REPLY))
    _consume(pd, gate, _reply(REJECT_REPLY), confirm_fix=True)

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--pm-fixed", _pm_fixed_evidence(),
    ]) == 1
    assert _entry(pd, gate).get("resolution") is None
    assert "라운드 상한이 미소진" in capsys.readouterr().err


def test_pm_fixed_does_not_turn_divergence_into_a_completion_bypass(pd, capsys):
    gate = "T-PMF-003"
    _consume(pd, gate, _reply(REJECT_REPLY))
    _consume(pd, gate, _reply(REJECT_REPLY))
    entry = _entry(pd, gate)
    entry["rounds"][1]["must_fix"] = 2
    pd._save_internal_round_ledger({gate: entry})
    _consume(pd, gate, _reply(REJECT_REPLY), confirm_fix=True)
    entry = _entry(pd, gate)
    entry["rounds"][2]["must_fix"] = 3
    pd._save_internal_round_ledger({gate: entry})

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--pm-fixed", _pm_fixed_evidence(),
    ]) == 1
    assert _entry(pd, gate).get("resolution") is None
    assert "증가(발산)" in capsys.readouterr().err


@pytest.mark.parametrize(
    "evidence",
    [
        "",
        "직접 고쳤고 회귀도 통과",
        "change=tests/test_internal_review_rounds.py:1; regression=pytest -q",
        (
            "change=tests/test_internal_review_rounds.py:999999; "
            "regression=pytest tests/test_internal_review_rounds.py -q; result=rc=0"
        ),
    ],
)
def test_pm_fixed_rejects_empty_freeform_incomplete_and_unverifiable_evidence(
    pd, capsys, evidence,
):
    gate = "T-PMF-002"
    for _index in range(pd.INTERNAL_REVIEW_ROUNDS_MAX):
        _consume(pd, gate, _reply(REJECT_REPLY))
    _consume(pd, gate, _reply(REJECT_REPLY), confirm_fix=True)

    assert pd._cmd_rounds([
        "resolve", "--gate", gate, "--pm-fixed", evidence,
    ]) == 1
    assert _entry(pd, gate).get("resolution") is None
    error = capsys.readouterr().err
    assert "--pm-fixed" in error and ("근거" in error or "변경 지점" in error)


def test_confirm_fix_quota_is_refunded_with_a_no_spawn_call(pd):
    gate = "T-0655"
    _consume(pd, gate, _reply(REJECT_REPLY))
    budget, _ = _consume(
        pd, gate, None,
        raw_ids=("no-child",), spawned=(False,), confirm_fix=True,
    )
    assert budget.reserved
    entry = _entry(pd, gate)
    assert entry["count"] == 1
    assert entry["confirm_fix"] == 0

    retry, _ = _consume(pd, gate, _reply(PASS_REPLY), confirm_fix=True)
    assert retry.refused_rc is None


def test_divergence_blocks_before_the_three_round_cap(pd, capsys):
    gate = "T-0656"
    one = _reply(REJECT_REPLY)
    # 같은 실제 reply를 쓰되 두 번째는 실제 항목 하나를 복제해 count 2 형상을 만든다. 판정 parser
    # 검증은 원문 fixture 단독 테스트가 소유하고, 여기서는 실제 장부의 수렴 수열만 조작한다.
    _consume(pd, gate, one)
    entry = _entry(pd, gate)
    entry["records"][0]["must_fix_items"] = ["MF-1"]
    entry["rounds"][0]["must_fix"] = 1
    pd._save_internal_round_ledger({gate: entry})

    _consume(pd, gate, one)
    entry = _entry(pd, gate)
    entry["records"][1]["must_fix_items"] = ["MF-1", "MF-2"]
    entry["rounds"][1]["must_fix"] = 2
    pd._save_internal_round_ledger({gate: entry})

    blocked, _ = _consume(pd, gate, one)
    assert blocked.refused_rc == 1
    assert _entry(pd, gate)["count"] == 2
    refusal = capsys.readouterr().err
    assert "발산" in refusal
    assert "rounds recalculate --gate T-0656" in refusal


def test_actual_file_lock_serializes_check_and_reserve_at_cap(pd):
    gate = "T-0657"
    # 동적 loader/cache를 단일 스레드에서 먼저 해소해 이 테스트가 파일락 자체만 겨냥하게 한다.
    pd._load_review_rounds()
    pd._load_file_lock()

    def reserve(_index):
        return pd._reserve_internal_review_round(
            gate, confirm_fix=False, wall_timeout_sec=60,
            target_rev="same", expected_confirm_evidence=None,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        budgets = list(pool.map(reserve, range(8)))

    admitted = [budget for budget in budgets if budget.refused_rc is None]
    refused = [budget for budget in budgets if budget.refused_rc == 1]
    entry = _entry(pd, gate)
    assert len(admitted) == 3
    assert len(refused) == 5
    assert entry["count"] == len(entry["records"]) == 3
    assert len({record["sequence"] for record in entry["records"]}) == 3


def test_lock_failure_warns_and_allows_unmetered_advisory(pd, monkeypatch, capsys):
    """가드 락 고장은 reviewer를 막지 않되 라운드/완료 증거를 만들지 않는다."""
    @contextlib.contextmanager
    def broken_lock():
        raise OSError("lock unavailable")
        yield

    monkeypatch.setattr(pd, "_internal_round_lock", broken_lock)
    budget = pd._reserve_internal_review_round(
        "T-LOCK-001", confirm_fix=False, wall_timeout_sec=60,
        target_rev="same", expected_confirm_evidence=None,
    )

    assert budget.refused_rc is None
    assert not budget.reserved
    assert not pd._internal_round_ledger_path().exists()
    warning = capsys.readouterr().err
    assert "비계측 자문으로 진행" in warning
    assert "완료 증거가 아닙니다" in warning


@pytest.mark.parametrize(
    ("damage", "expected_error"),
    [
        pytest.param("invalid-utf8", "UnicodeDecodeError", id="invalid-utf8-bytes"),
        pytest.param("malformed-json", "JSONDecodeError", id="malformed-json"),
        pytest.param("empty", "JSONDecodeError", id="empty-file"),
        pytest.param("permission", "PermissionError", id="permission-error"),
    ],
)
def test_damaged_ledger_warns_recovers_and_keeps_reservation_metered(
    pd, monkeypatch, capsys, damage, expected_error,
):
    """바이트/JSON/빈 파일/권한 손상은 빈 장부로 복구돼 정상 예약·계측된다."""
    path = pd._internal_round_ledger_path()
    if damage == "invalid-utf8":
        # 실제 비-UTF-8 바이트를 디스크에 써 read_text 디코더 경계를 통과시킨다.
        path.write_bytes(b"{\xff\xfe\x80}")
    elif damage == "malformed-json":
        path.write_text("{ not-json", encoding="utf-8")
    elif damage == "empty":
        path.write_bytes(b"")
    else:
        path.write_text("{}", encoding="utf-8")
        original_read_text = Path.read_text
        denied_once = False

        def permission_denied_once(self, *args, **kwargs):
            nonlocal denied_once
            if self == path and not denied_once:
                denied_once = True
                raise PermissionError("ledger read denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", permission_denied_once)

    budget = pd._reserve_internal_review_round(
        "T-DAMAGE-001", confirm_fix=False, wall_timeout_sec=60,
        target_rev="same", expected_confirm_evidence=None,
    )

    assert budget.refused_rc is None
    assert budget.reserved
    entry = _entry(pd, "T-DAMAGE-001")
    assert entry["count"] == len(entry["records"]) == 1
    assert entry["rounds"] == []
    warning = capsys.readouterr().err
    assert expected_error in warning
    assert "빈 장부로 복구해 예약·계측을 계속" in warning


def test_reservation_boundary_absorbs_unicode_error_from_reader(
    pd, monkeypatch, capsys,
):
    """공용 reader 바깥에서 UnicodeError가 새도 최외곽 예약 경계는 비계측 fail-open이다."""
    def fail_decode():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(pd, "_load_internal_round_ledger", fail_decode)
    budget = pd._reserve_internal_review_round(
        "T-DAMAGE-002", confirm_fix=False, wall_timeout_sec=60,
        target_rev="same", expected_confirm_evidence=None,
    )

    assert budget.refused_rc is None
    assert not budget.reserved
    warning = capsys.readouterr().err
    assert "UnicodeDecodeError" in warning
    assert "비계측 자문으로 진행" in warning


def test_driver_return_before_raw_write_failure_still_consumes_round(
    pd, monkeypatch, tmp_path,
):
    """driver 송신 뒤 raw 박제가 실패해도 any_spawned가 이미 고정돼 환불되지 않는다."""
    budget = pd._reserve_internal_review_round(
        "T-SPAWN-001", confirm_fix=False, wall_timeout_sec=60,
        target_rev="same", expected_confirm_evidence=None,
    )
    trace = pd.InternalRoundTrace(budget)
    driver_returned = False

    def run_fn(*_args, **_kwargs):
        nonlocal driver_returned
        driver_returned = True
        return {
            "returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
        }

    def fail_raw(*_args, **_kwargs):
        raise OSError("raw write failed")

    monkeypatch.setattr(pd, "_write_reserved_raw", fail_raw)
    with pytest.raises(OSError, match="raw write failed"):
        pd._execute_attempt(
            harness="claude", model="opus", reasoning=None,
            role="code-reviewer", cwd=tmp_path, prompt="review",
            timeout=30, output_dir=tmp_path / "raw", run_fn=run_fn,
            attempt="primary", internal_trace=trace,
        )
    pd._finish_internal_review_round(budget, trace)

    entry = _entry(pd, "T-SPAWN-001")
    assert driver_returned is True
    assert trace.any_spawned is True
    assert entry["count"] == len(entry["records"]) == len(entry["rounds"]) == 1
    assert entry["rounds"][0]["verdict"] is None


def test_namespaced_gate_reuses_board_ticket_id_grammar(pd):
    parser = pd.build_arg_parser()
    valid = parser.parse_args([
        "--role", "code-reviewer", "--prompt-file", "/tmp/p", "--cwd", "/tmp/w",
        "--gate", "T-PAY-001",
    ])
    assert pd._validate_args(parser, valid) == Path("/tmp/w")

    invalid = parser.parse_args([
        "--role", "code-reviewer", "--prompt-file", "/tmp/p", "--cwd", "/tmp/w",
        "--gate", "T-PAY",
    ])
    with pytest.raises(SystemExit) as exc:
        pd._validate_args(parser, invalid)
    assert exc.value.code == 2


def test_main_derives_gate_from_ticket_and_links_the_real_raw_record(
    pd, monkeypatch, tmp_path,
):
    owner = pd._CONFIG_REPO_OVERRIDE
    # 이 테스트의 축은 internal raw record linkage다. 성장 transport는 별도 회귀가
    # 소유하므로 prepare seam을 no-copy로 격리한다.
    monkeypatch.setattr(pd, "prepare_ticket_copy", lambda **_kw: None)
    prompt = owner / ".project_manager" / "review-prompt.md"
    prompt.write_text("구현을 검토하라.", encoding="utf-8")
    conf = {
        pd.DELEGATE_ENABLED_KEY: "true",
        "delegate.code-reviewer.harness": "claude",
        "delegate.code-reviewer.model": "opus",
    }
    external = pd._load_external_review()
    monkeypatch.setattr(pd, "local_config", lambda: conf)
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *_args, **_kw: True)
    monkeypatch.setattr(external, "repo_root_from_cwd", lambda cwd: owner)
    monkeypatch.setattr(external, "resolve_pm_home_for_repo", lambda *_a, **_kw: owner)
    monkeypatch.setattr(external, "_owns_real_board", lambda _path: False)
    monkeypatch.setattr(pd, "_load_external_review", lambda: external)
    monkeypatch.setattr(
        pd, "check_local_conf_divergence",
        lambda *args, **kwargs: (owner, None, external),
    )
    monkeypatch.setattr(pd, "begin_scope_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(pd, "codex_egress_escalation_required", lambda *_a, **_kw: False)

    class FakeRun:
        def __call__(self, argv, **kwargs):
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "type": "result", "result": _reply(PASS_REPLY),
                    "session_id": "session-1",
                }),
                "stderr": "",
                "timed_out": False,
            }

    rc = pd.main([
        "--role", "code-reviewer",
        "--prompt-file", str(prompt),
        "--cwd", str(owner),
        "--ticket", "T-0658",
        "--output-dir", str(owner / ".project_manager" / ".local" / "delegate"),
    ], run_fn=FakeRun())

    assert rc == 0
    entry = _entry(pd, "T-0658")
    raw_id = entry["rounds"][0]["outcome_record_id"]
    assert entry["rounds"][0]["raw_record_ids"] == [raw_id]
    raw_ledger = json.loads(
        (owner / ".project_manager" / ".local" / "delegate" / "raw_outputs.json")
        .read_text(encoding="utf-8")
    )
    raw = next(row for row in raw_ledger["records"] if row["id"] == raw_id)
    assert raw[pd.INTERNAL_ROUND_ID_FIELD] == entry["rounds"][0]["id"]
    assert raw[pd.INTERNAL_GATE_FIELD] == "T-0658"


def test_gate_flags_are_code_reviewer_only(pd):
    parser = pd.build_arg_parser()
    args = parser.parse_args([
        "--role", "developer", "--prompt-file", "/tmp/p", "--cwd", "/tmp/w",
        "--gate", "T-0651",
    ])
    with pytest.raises(SystemExit) as exc:
        pd._validate_args(parser, args)
    assert exc.value.code == 2
