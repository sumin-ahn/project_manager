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
import uuid
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
# 회수된 board 라운드 파일 3건 — 장부만 미상으로 박혀 완료가 막혔던 실 게이트의 산출 bytes.
# 세 파일 모두 산문 통과 선언 + finding 0건 블록이고 절 배치만 다르다. T-0817 회수 bytes 는
# `internal_round_t0813_pass_both_axes.md` 와 byte 동일이라 그 사본을 그대로 쓴다.
T0771_ROUND_FILE = FIXTURES / "internal_round_t0771_pass_evidence_after_block.md"
T0817_ROUND_FILE = FIXTURES / "internal_round_t0813_pass_both_axes.md"
T0822_ROUND_FILE = FIXTURES / "internal_round_t0822_pass_bullets_before_block.md"
T0783_REJECT_ROUND_FILE = FIXTURES / "internal_round_t0783_reject_both_axes.md"
# 같은 실행의 터미널 회신 — 허용 선언형이 없어 장부에 미상을 박은 입력이다.
T0817_TERMINAL_PROSE_REPLY = (
    FIXTURES / "internal_review_t0817_terminal_prose_only_reply.txt"
)
_DIFF_A = "sha256:" + "a" * 64
_DIFF_B = "sha256:" + "b" * 64
_DIFF_C = "sha256:" + "c" * 64


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
    diff_fingerprint=_DIFF_A,
):
    budget = pd._reserve_internal_review_round(
        gate,
        wall_timeout_sec=60,
        target_rev="deadbeef",
        diff_fingerprint=diff_fingerprint,
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
    ticket: str | None = None,
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
    record = {
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
    if ticket is not None:
        # 위임이 라운드 파일을 준비했다는 유일한 기계 흔적 — 회수된 파일 좌표의 ticket 축이다.
        record[pd.RESUME_FIELD_TICKET] = ticket
    return record


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
        repaired, pd.internal_review_rounds_max(), wall_timeout_sec=60,
    ) is None
    next_round = pd._reserve_internal_review_round(
        gate,
        wall_timeout_sec=60,
        target_rev="after-recalculation",
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


# ── T-0842 — 회수된 라운드 파일에서 판정을 되살리는 재계산 ──────────────────

def _round_text(path: Path) -> str:
    """회수된 board 라운드 파일 bytes(fixture)."""
    return path.read_text(encoding="utf-8")


def _harvest_board_round(
    pd,
    *,
    ticket: str,
    ordinal: int,
    body: str,
    outcome: dict,
    prepared_at: str | None = None,
    harvested_at: str | None = None,
) -> Path:
    """이 라운드가 회수해 board 에 남긴 라운드 파일 + PM 홈 준비/회수 기록을 만든다.

    준비~회수 구간이 라운드 예약 구간과 겹치는 것이 좌표의 유일한 기계 링크라, 실 형상 그대로
    라운드 자신의 예약 구간을 쓴다. 두 시각은 구간 역전 형상을 만들 때만 덮어쓴다.
    """
    owner = Path(pd._CONFIG_REPO_OVERRIDE).resolve()
    rounds_module = pd._load_ticket_rounds()
    name = rounds_module.round_filename(ordinal, pd.INTERNAL_REVIEW_ROLE)
    path = (
        owner / ".project_manager" / "board" / "tickets" / "rounds" / ticket / name
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    run_id = uuid.uuid4().hex
    pd._append_delegate_rounds_ledger(owner, {
        "ticket": ticket,
        "role": pd.INTERNAL_REVIEW_ROLE,
        "ordinal": ordinal,
        "run_id": run_id,
        "copy": str(owner / "slot" / ticket / run_id / name),
        "board_rel": pd._board_relative_path(path, owner),
        "prepared_at": prepared_at or outcome["started_at"],
        "harvested_at": harvested_at or outcome["ts"],
    })
    return path


def _stuck_unknown_round(pd, gate: str) -> dict:
    """터미널 회신만으로 미상이 박힌 라운드 1건 — 막힌 실 게이트의 장부 형상."""
    reply = _reply(T0817_TERMINAL_PROSE_REPLY)
    assessment = pd._internal_reply_assessment(reply)
    assert assessment.outcome == pd.InternalReplyOutcome(None, None)
    assert assessment.diagnostic.code == pd.INTERNAL_DIAGNOSTIC_MISSING_VERDICT

    _consume(pd, gate, reply, raw_ids=("terminal-only",))
    entry = _entry(pd, gate)
    outcome = entry["rounds"][0]
    assert outcome["verdict"] is None and outcome["must_fix"] is None
    _write_raw_ledger(pd, [
        _raw_record(
            pd,
            record_id="terminal-only",
            gate=gate,
            round_id=outcome["id"],
            reply=reply,
            ticket=gate,
        )
    ])
    return outcome


def _completion_problem(pd, monkeypatch, gate: str):
    board = pd._load_board()
    monkeypatch.setattr(
        board, "_internal_review_rounds_ledger", pd._internal_round_ledger_path,
    )
    monkeypatch.setattr(board, "_ticket_search_dirs", lambda: [])
    monkeypatch.setattr(board, "_rel_to_repo", lambda path: str(path))
    return board._internal_review_completion_problem(gate)


@pytest.mark.parametrize(
    ("gate", "ordinal", "round_file"),
    [
        pytest.param("T-0771", 5, T0771_ROUND_FILE, id="T-0771"),
        pytest.param("T-0817", 2, T0817_ROUND_FILE, id="T-0817"),
        pytest.param("T-0822", 3, T0822_ROUND_FILE, id="T-0822"),
    ],
)
def test_recalculate_recovers_recorded_unknown_from_the_harvested_round_file(
    pd, monkeypatch, gate, ordinal, round_file,
):
    """회수된 파일이 통과인데 장부만 미상이던 실 게이트 3건이 0으로 복구되고 완료가 열린다."""
    outcome = _stuck_unknown_round(pd, gate)
    board_path = _harvest_board_round(
        pd, ticket=gate, ordinal=ordinal,
        body=_round_text(round_file), outcome=outcome,
    )
    assert _completion_problem(pd, monkeypatch, gate) is not None

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == (None,)
    assert report.after == (0,)
    entry = _entry(pd, gate)
    row = entry["rounds"][0]
    assert row["verdict"] == 0
    assert row["must_fix"] == 0
    assert row[pd.INTERNAL_FINDING_IDS_FIELD] == []
    assert row[pd.INTERNAL_VERDICT_SOURCE_FIELD] == pd.INTERNAL_VERDICT_SOURCE_BLOCK
    assert pd.INTERNAL_VERDICT_DIAGNOSTIC_FIELD not in row
    recalculation = row[pd.INTERNAL_RECALCULATION_FIELD]
    assert recalculation["status"] == pd.INTERNAL_RECALCULATION_OK
    assert str(board_path) in recalculation["detail"]
    # 예약 레코드도 같은 값으로 따라간다(판정 추출 성공 · 잔여 0건).
    record = entry["records"][0]
    assert record["verdict"] is True
    assert record["must_fix_items"] == []
    assert _completion_problem(pd, monkeypatch, gate) is None


@pytest.mark.parametrize(
    ("gate", "shape", "reason", "code"),
    [
        pytest.param(
            "T-UNKNOWN-001", "no-record", "회수 기록이 이 라운드 예약 구간에 0건",
            "missing-verdict-word", id="prepare-record-absent",
        ),
        pytest.param(
            "T-UNKNOWN-002", "file-deleted", "board 라운드 파일 부재",
            "missing-verdict-word", id="round-file-absent",
        ),
        pytest.param(
            "T-UNKNOWN-003", "broken-block", "기계 블록 축 판정 불능",
            "block-axis-unusable", id="block-broken",
        ),
        pytest.param(
            "T-UNKNOWN-004", "no-block", "기계 블록 축 판정 불능",
            "block-axis-unusable", id="block-absent",
        ),
    ],
)
def test_recalculate_keeps_unknown_when_the_round_file_is_absent_or_unusable(
    pd, monkeypatch, gate, shape, reason, code,
):
    """파일 부재·블록 손상/부재는 종전대로 미상이다 — 판정 불능을 통과로 만들지 않는다."""
    outcome = _stuck_unknown_round(pd, gate)
    passing = _round_text(T0817_ROUND_FILE)
    block_payload = '{"version":2,"findings":[],"confirmations":[]}'
    assert block_payload in passing
    if shape != "no-record":
        body = {
            "file-deleted": passing,
            # 산문 축은 통과 그대로 두고 기계 블록만 무너뜨린다 — 한 축만 통과인 형상이
            # 통과로 접히지 않는지가 판정 대상이다.
            "broken-block": passing.replace(block_payload, block_payload[:-1]),
            "no-block": passing.split(f"```{pd.PM_REVIEW_BLOCK}")[0],
        }[shape]
        board_path = _harvest_board_round(
            pd, ticket=gate, ordinal=2, body=body, outcome=outcome,
        )
        if shape == "file-deleted":
            board_path.unlink()

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == (None,)
    assert report.after == (None,)
    row = _entry(pd, gate)["rounds"][0]
    assert row["verdict"] is None
    assert row["must_fix"] is None
    recalculation = row[pd.INTERNAL_RECALCULATION_FIELD]
    assert recalculation["status"] == pd.INTERNAL_RECALCULATION_UNKNOWN
    assert reason in recalculation["detail"]
    assert row[pd.INTERNAL_VERDICT_DIAGNOSTIC_FIELD]["code"] == code
    assert row[pd.INTERNAL_VERDICT_SOURCE_FIELD] is None
    assert _completion_problem(pd, monkeypatch, gate) is not None


@pytest.mark.parametrize(
    ("gate", "recorded_reply", "before"),
    [
        pytest.param("T-REJECT-001", REJECT_REPLY, (1,), id="ledger-reject"),
        pytest.param("T-REJECT-002", None, (None,), id="ledger-unknown"),
    ],
)
def test_recalculate_keeps_a_reject_round_rejected(pd, gate, recorded_reply, before):
    """반려 라운드 파일은 재계산 후에도 반려다 — 되살리기가 완화가 아니다."""
    if recorded_reply is None:
        outcome = _stuck_unknown_round(pd, gate)
    else:
        reply = _reply(recorded_reply)
        _consume(pd, gate, reply, raw_ids=("reject-source",))
        outcome = _entry(pd, gate)["rounds"][0]
        _write_raw_ledger(pd, [
            _raw_record(
                pd,
                record_id="reject-source",
                gate=gate,
                round_id=outcome["id"],
                reply=reply,
                ticket=gate,
            )
        ])
    _harvest_board_round(
        pd, ticket=gate, ordinal=2,
        body=_round_text(T0783_REJECT_ROUND_FILE), outcome=outcome,
    )

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == before
    assert report.after == (1,)
    row = _entry(pd, gate)["rounds"][0]
    assert row["verdict"] == 1
    assert row["must_fix"] == 1
    assert row[pd.INTERNAL_FINDING_IDS_FIELD] == ["F-001", "F-002"]
    assert row[pd.INTERNAL_VERDICT_SOURCE_FIELD] == pd.INTERNAL_VERDICT_SOURCE_BLOCK


@pytest.mark.parametrize(
    ("gate", "raw_fixture"),
    [
        pytest.param("T-RELAX-001", REJECT_REPLY, id="raw-reject"),
        pytest.param("T-RELAX-002", PASS_REPLY, id="raw-pass"),
        pytest.param("T-RELAX-003", T0817_TERMINAL_PROSE_REPLY, id="raw-unknown"),
    ],
)
def test_recalculate_never_relaxes_a_recorded_reject_whatever_the_raw_reply_says(
    pd, gate, raw_fixture,
):
    """파일 통과 + 기록된 반려면 **어느 입력도 채택하지 않는다** — raw 축과 무관하게 반려 유지."""
    reply = _reply(raw_fixture)
    _consume(pd, gate, _reply(REJECT_REPLY), raw_ids=("reject-source",))
    outcome = _entry(pd, gate)["rounds"][0]
    recorded = {
        key: outcome.get(key) for key in (
            "verdict", "must_fix", pd.INTERNAL_FINDING_IDS_FIELD,
            pd.INTERNAL_VERDICT_SOURCE_FIELD,
        )
    }
    assert recorded["verdict"] == 1 and recorded["must_fix"] == 1
    _write_raw_ledger(pd, [
        _raw_record(
            pd,
            record_id="reject-source",
            gate=gate,
            round_id=outcome["id"],
            reply=reply,
            ticket=gate,
        )
    ])
    board_path = _harvest_board_round(
        pd, ticket=gate, ordinal=2,
        body=_round_text(T0817_ROUND_FILE), outcome=outcome,
    )

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == (1,)
    assert report.after == (1,)
    row = _entry(pd, gate)["rounds"][0]
    # 값은 하나도 바뀌지 않는다(미채택) — 메타데이터만 사유를 남긴다.
    assert {key: row.get(key) for key in recorded} == recorded
    recalculation = row[pd.INTERNAL_RECALCULATION_FIELD]
    assert recalculation["status"] == pd.INTERNAL_RECALCULATION_UNKNOWN
    assert "미채택" in recalculation["detail"]
    assert "완화" in recalculation["detail"]
    assert str(board_path) in recalculation["detail"]


def test_recalculate_without_a_board_file_still_recomputes_reject_to_pass(pd):
    """board 파일이 없으면 종전 raw 재계산 그대로다 — 완화 차단은 파일 축에만 붙는다."""
    gate = "T-RELAX-004"
    _consume(pd, gate, _reply(REJECT_REPLY), raw_ids=("reject-source",))
    outcome = _entry(pd, gate)["rounds"][0]
    _write_raw_ledger(pd, [
        _raw_record(
            pd,
            record_id="reject-source",
            gate=gate,
            round_id=outcome["id"],
            reply=_reply(PASS_REPLY),
            ticket=gate,
        )
    ])

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == (1,)
    assert report.after == (0,)
    row = _entry(pd, gate)["rounds"][0]
    assert row["verdict"] == 0
    assert row[pd.INTERNAL_RECALCULATION_FIELD]["status"] == pd.INTERNAL_RECALCULATION_OK


@pytest.mark.parametrize(
    ("gate", "axis", "reason"),
    [
        pytest.param(
            "T-SKEW-001", "outcome", "라운드 예약 구간 역전", id="reservation-window",
        ),
        pytest.param(
            "T-SKEW-002", "harvest", "회수 기록이 이 라운드 예약 구간에 0건",
            id="prepare-harvest-window",
        ),
    ],
)
def test_recalculate_refuses_a_coordinate_whose_window_is_inverted(
    pd, gate, axis, reason,
):
    """시각이 파싱돼도 구간이 역전이면 좌표를 세우지 않는다 — 추측 없이 미상 유지."""
    outcome = _stuck_unknown_round(pd, gate)
    started, finished = outcome["started_at"], outcome["ts"]
    if axis == "outcome":
        # 예약 구간만 뒤집는다 — 준비~회수 기록은 실 형상 그대로 둔다.
        ledger = json.loads(
            pd._internal_round_ledger_path().read_text(encoding="utf-8")
        )
        stored = ledger[gate]["rounds"][0]
        stored["started_at"], stored["ts"] = finished, started
        pd._save_internal_round_ledger(ledger)
        prepared_at = harvested_at = None
    else:
        # 준비~회수만 뒤집는다 — 두 시각 모두 예약 구간 **안**이라 겹침 판정은 통과한다.
        prepared_at, harvested_at = finished, started
    _harvest_board_round(
        pd, ticket=gate, ordinal=2, body=_round_text(T0817_ROUND_FILE),
        outcome=outcome, prepared_at=prepared_at, harvested_at=harvested_at,
    )

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == (None,)
    assert report.after == (None,)
    row = _entry(pd, gate)["rounds"][0]
    assert row["verdict"] is None
    assert reason in row[pd.INTERNAL_RECALCULATION_FIELD]["detail"]


@pytest.mark.parametrize(
    ("gate", "board", "after", "status", "reason"),
    [
        pytest.param(
            "T-RAWPATH-001", True, (0,), "recalculated", "board 라운드 파일",
            id="board-authoritative-without-raw-path",
        ),
        pytest.param(
            "T-RAWPATH-002", False, (None,), "unknown", "raw_path 부재/형식 오류",
            id="fallback-still-needs-raw-path",
        ),
    ],
)
def test_recalculate_reads_the_board_file_independently_of_raw_path(
    pd, gate, board, after, status, reason,
):
    """raw_path 유효성은 대체 입력에만 걸린다 — 권위 입력 판독을 가리지 않는다."""
    outcome = _stuck_unknown_round(pd, gate)
    raw_row = _raw_record(
        pd,
        record_id="terminal-only",
        gate=gate,
        round_id=outcome["id"],
        reply=None,
        ticket=gate,
    )
    raw_row.pop("raw_path")
    _write_raw_ledger(pd, [raw_row])
    if board:
        _harvest_board_round(
            pd, ticket=gate, ordinal=2,
            body=_round_text(T0817_ROUND_FILE), outcome=outcome,
        )

    report = pd._recalculate_internal_review_rounds(gate)

    assert report.before == (None,)
    assert report.after == after
    row = _entry(pd, gate)["rounds"][0]
    recalculation = row[pd.INTERNAL_RECALCULATION_FIELD]
    assert recalculation["status"] == status
    assert reason in recalculation["detail"]


def test_pm_verified_still_refuses_the_finding_zero_review_round(pd):
    """복구된 통과 라운드도 `--pm-verified` 는 그대로 거부한다(그 거부는 무변경이다)."""
    rounds_module = pd._load_ticket_rounds()
    recovered = rounds_module.Round(
        ordinal=2,
        role=pd.INTERNAL_REVIEW_ROLE,
        path=Path(rounds_module.round_filename(2, pd.INTERNAL_REVIEW_ROLE)),
        text=_round_text(T0817_ROUND_FILE),
        pending=False,
    )

    problem = pd.pm_verified_evidence_problem(
        "---\nid: T-0817\n---\n# T-0817\n",
        [recovered],
        reviewer_role=pd.INTERNAL_REVIEW_ROLE,
        surface_floor=0,
    )

    assert problem is not None
    assert "finding-zero 리뷰 라운드" in problem


def test_rounds_resolve_rejects_removed_into_without_mutating_ledger(pd, capsys):
    gate = "T-RESOLVE-001"
    _consume(pd, gate, _reply(REJECT_REPLY), raw_ids=("reject-source",))
    before = pd._internal_round_ledger_path().read_bytes()

    with pytest.raises(SystemExit):
        pd._cmd_rounds(["resolve", "--gate", gate, "--into", "T-RESOLVE-002"])
    assert pd._internal_round_ledger_path().read_bytes() == before
    assert "--pm-verified" in capsys.readouterr().err


def test_rounds_resolve_rejects_removed_fixed_without_mutating_ledger(pd, capsys):
    gate = "T-FIXED-001"
    _consume(
        pd, gate, _reply(REJECT_REPLY), raw_ids=("blocked",),
        diff_fingerprint=_DIFF_A,
    )
    before = pd._internal_round_ledger_path().read_bytes()
    with pytest.raises(SystemExit):
        pd._cmd_rounds(["resolve", "--gate", gate, "--fixed", "T-FIXED-002"])
    assert pd._internal_round_ledger_path().read_bytes() == before
    assert "--pm-verified" in capsys.readouterr().err


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


def test_removed_fixed_does_not_gain_a_legacy_fingerprint_exception(pd, capsys):
    gate, evidence = "T-OLD-001", "T-OLD-002"
    _consume(pd, gate, _reply(REJECT_REPLY), diff_fingerprint=None)
    _consume(pd, evidence, _reply(PASS_REPLY), diff_fingerprint=None)
    before = pd._internal_round_ledger_path().read_bytes()
    with pytest.raises(SystemExit):
        pd._cmd_rounds(["resolve", "--gate", gate, "--fixed", evidence])
    assert pd._internal_round_ledger_path().read_bytes() == before
    assert "--pm-verified" in capsys.readouterr().err


def test_legacy_unfingerprinted_fixed_resolution_is_unrecognized(pd):
    board = pd._load_board()
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
    assert board.gate_resolution(gate) is None
    problem = board._gate_disposition_problem("T-LEGACY-BLOCK", gate)
    assert problem is not None and "처분 선언 없음" in problem


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


def test_cap_reached_gate_gets_no_further_round_of_any_kind(pd, capsys):
    """상한에 걸린 게이트는 추가 라운드 없이 정지·보고한다."""
    gate = "T-0654"
    for _ in range(3):
        budget, _ = _consume(pd, gate, _reply(REJECT_REPLY))
        assert budget.refused_rc is None

    blocked, _ = _consume(pd, gate, _reply(REJECT_REPLY))
    assert blocked.refused_rc == 1
    refusal = capsys.readouterr().err
    assert "현재 티켓을 정지해 사용자에게 보고" in refusal
    assert "라운드를 더" not in refusal
    assert "--confirm-fix" not in refusal

    again, _ = _consume(pd, gate, _reply(PASS_REPLY))
    entry = _entry(pd, gate)
    assert again.refused_rc == 1
    assert entry["count"] == 3                 # 거부는 예약하지 않는다
    assert "confirm_fix" not in entry          # 폐지 필드는 정규화에서 되살아나지 않는다


def test_resolve_offers_only_pm_verified(pd, capsys):
    """내부 처분 표면은 기계 확인 하나뿐이다."""
    with pytest.raises(SystemExit) as exc:
        pd._cmd_rounds(["resolve", "--help"])

    assert exc.value.code == 0
    out = "".join(capsys.readouterr().out.split())
    assert "--pm-verified" in out
    assert "--into" not in out and "--fixed" not in out
    assert "--pm-fixed" not in out


def test_resolve_rejects_the_retired_pm_direct_disposition(pd, capsys):
    """폐지된 처분 표기는 조용히 무시되지 않고 usage error 다(장부 무변경)."""
    gate = "T-PMF-001"
    for _index in range(pd.internal_review_rounds_max()):
        _consume(pd, gate, _reply(REJECT_REPLY))
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        pd._cmd_rounds([
            "resolve", "--gate", gate, "--pm-fixed",
            "change=tests/test_internal_review_rounds.py:1; "
            "regression=pytest -q; result=rc=0 (ok)",
        ])

    assert exc.value.code == 2
    assert _entry(pd, gate).get("resolution") is None


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
            gate, wall_timeout_sec=60,
            target_rev="same",
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
        "T-LOCK-001", wall_timeout_sec=60,
        target_rev="same",
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
        # 판독은 공유 읽기 seam 을 지난다(T-0729) — 주입도 그 자리에 건다. `Path.read_text` 에
        # 걸면 엔진이 그 호출을 더는 하지 않아 회귀가 공허해진다.
        seam = pd._load_file_lock()
        original_read_text = seam.read_text_shared
        denied_once = False

        def permission_denied_once(target, *args, **kwargs):
            nonlocal denied_once
            if Path(target) == path and not denied_once:
                denied_once = True
                raise PermissionError("ledger read denied")
            return original_read_text(target, *args, **kwargs)

        monkeypatch.setattr(seam, "read_text_shared", permission_denied_once)

    budget = pd._reserve_internal_review_round(
        "T-DAMAGE-001", wall_timeout_sec=60,
        target_rev="same",
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
        "T-DAMAGE-002", wall_timeout_sec=60,
        target_rev="same",
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
        "T-SPAWN-001", wall_timeout_sec=60,
        target_rev="same",
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


def test_namespaced_gate_reuses_board_ticket_id_grammar(pd, tmp_path):
    parser = pd.build_arg_parser()
    prompt = tmp_path / "p"
    cwd = tmp_path / "w"
    prompt.write_text("review\n", encoding="utf-8")
    cwd.mkdir()
    valid = parser.parse_args([
        "--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(cwd),
        "--gate", "T-PAY-001",
    ])
    assert pd._validate_args(parser, valid) == cwd

    invalid = parser.parse_args([
        "--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(cwd),
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


# ── 내부 라운드 수렴 상한 conf 화 (T-0772 · 병합 T-0773) ────────────────────
#
# 추가 리뷰어 축은 전부 local.conf 로 조정되는데 내부 축만 코드에 박혀 있었다. 키는 역할 상수에서
# 파생하고(표기가 갈리는 자리를 만들지 않는다), 소비자는 다음 라운드 예약 판정 하나다 — 상한을
# 소진해서 여는 처분이 없으므로 값이 갈릴 두 번째 자리도 없다.


def _write_internal_conf(pd, values: dict[str, str]) -> Path:
    """이 클론의 local.conf 를 쓴다 — 노브 해소는 파일에서 온다(상수 주입 아님)."""
    path = pd._CONFIG_REPO_OVERRIDE / ".project_manager" / "local.conf"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()),
                    encoding="utf-8")
    return path


def test_internal_rounds_max_key_is_derived_from_the_role_constant(pd):
    """키 문자열을 하드코딩하지 않는다 — 역할 표기가 갈리는 자리를 원천 차단한다."""
    assert (pd.INTERNAL_REVIEW_ROUNDS_MAX_KEY
            == f"delegate.{pd.INTERNAL_REVIEW_ROLE}.rounds_max")
    assert pd.DEFAULT_INTERNAL_REVIEW_ROUNDS_MAX == 3
    assert pd.internal_review_rounds_max() == 3          # conf 없음 = 엔진 기본값


@pytest.mark.parametrize("raw", ["", "   ", "abc", "-1", "3.5"])
def test_broken_internal_rounds_max_falls_back_to_the_default(pd, raw):
    """깨진 값은 기본 3 으로 fail-soft — 설정이 위임을 벽돌로 만들지 않는다."""
    _write_internal_conf(pd, {pd.INTERNAL_REVIEW_ROUNDS_MAX_KEY: raw})
    assert pd.internal_review_rounds_max() == 3


@pytest.mark.parametrize("limit", [1, 3, 5], ids=["하향", "기본", "상향"])
def test_internal_rounds_max_conf_moves_the_refusal_point(pd, capsys, limit):
    """설정값이 실제 차단 시점을 옮기고, 거부 문구가 그 값·조정 키를 그대로 싣는다."""
    _write_internal_conf(pd, {pd.INTERNAL_REVIEW_ROUNDS_MAX_KEY: str(limit)})
    gate = f"T-RMX-00{limit}"
    for index in range(limit):
        budget, _trace = _consume(pd, gate, _reply(REJECT_REPLY))
        assert budget.refused_rc is None, f"라운드 {index + 1} 는 상한 안이다"
    assert _entry(pd, gate)["count"] == limit
    capsys.readouterr()

    refused, _trace = _consume(pd, gate, _reply(REJECT_REPLY))

    assert refused.refused_rc == 1
    err = capsys.readouterr().err
    assert f"사용 라운드: {limit}/{limit}" in err
    assert f"상한 {limit} 도달" in err                     # 값 재타이핑이 아니라 주입
    assert f"local.conf `{pd.INTERNAL_REVIEW_ROUNDS_MAX_KEY}`" in err
    assert "(기본 3)" in err
    assert _entry(pd, gate)["count"] == limit             # 거부는 예약하지 않는다


def test_internal_ledger_drops_the_retired_ack_field_too(pd, capsys):
    """공용 스키마의 두 번째 소비자(내부 장부)도 같은 규칙이다 — 폐지 필드는 승계 없이 떨어진다.

    감지 여부는 `_internal_gate_entry_with_retirement` 가 호출부에 반환한다(F-002) — 정규화
    자체(`_internal_gate_entry`)는 저장 여부를 모르는 조회 seam이라 침묵한다."""
    common = pd._load_review_rounds()
    gate = "T-ACK-INT"
    ledger = {gate: {"count": 3, common.RETIRED_ACK_FIELD: 2, "records": [], "rounds": []}}

    entry, retired_ack = pd._internal_gate_entry_with_retirement(ledger, gate)

    assert entry["count"] == 3
    assert common.RETIRED_ACK_FIELD not in entry
    assert retired_ack == 2
    assert capsys.readouterr().err == ""
    # 키가 이미 떨어졌으므로 같은 장부를 다시 정규화해도 감지되지 않는다(자연히 1회).
    pd._internal_gate_entry(ledger, gate)
    assert capsys.readouterr().err == ""
