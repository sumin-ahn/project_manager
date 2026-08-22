"""위임 세션 재사용(resume 라운드) + 비용 원장 회귀 — T-0595.

전부 mock(run_fn DI·실 하네스 스폰 0). 라이브 1회는 `test_pm_delegate_live.py` 의
`PM_DELEGATE_LIVE` 게이트가 소유한다.

검증 축(ticket DoD):
  ① 저장 축 단일 — 세션 id·usage 4필드·must_fix 항목·기준 rev 가 raw 장부의 **그 실행 레코드
     행**에 실리고, 리뷰 라운드 장부(review_rounds.json)는 만들지도 건드리지도 않는다.
  ② `--resume-from` 후보 선택 결정성(같은 티켓·role·rc=0·started_at 최신 1건) + 보존 창 밖
     자연 fresh.
  ③ 세션 id 형식 가드 — `--` 로 시작하는 손상 값이 argv 에 실리지 않는다(플래그 오소비 0).
  ④ 성공 판정 = 회신 세션 id 일치. 불일치/미회신이면 fresh + **full payload** 재실행 + loud 1줄.
  ⑤ delta 조립 입력 = 장부 구조화 필드 + 호출자 원문뿐(raw .txt 재파싱 0).
  ⑥ codex exec resume JSONL/stdin 배선·opencode 무배선.
  ⑦ usage 4필드 분해(수집 불가 필드는 부재 — 0 채우기 금지).
  ⑧ 위임 회계는 기록만 — 새 상한/거부 rc 없음.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

TICKET_ID = "T-" + "0777"
SESSION_ID = "11111111-2222-4333-8444-555555555555"
OTHER_SESSION_ID = "99999999-8888-4777-8666-555555555555"
# 역할 preamble 이 붙었는지(=full payload 인지) 가르는 마커.
_PREAMBLE_MARKER = "서브에이전트다"


def _load(name: str):
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"resume_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pd_module():
    """fixture 밖(파라미터 목록·전수 단언)에서 쓰는 엔진 모듈 로더."""
    return _load("pm_delegate")


@pytest.fixture(scope="module")
def pd():
    return _load("pm_delegate")


@pytest.fixture(scope="module")
def relay():
    return _load("pm_relay")


# ── canned wire (claude stream-json 실측 형식) ────────────────────────────────

def _claude_wire(reply: str = "DONE", session_id: str = SESSION_ID,
                 usage: dict | None = None) -> str:
    events = [
        {"type": "system", "subtype": "init", "session_id": session_id},
    ]
    if usage is not None:
        events.append({"type": "assistant", "message": {"usage": usage}})
    events.append({"type": "result", "result": reply, "session_id": session_id})
    return "\n".join(json.dumps(event) for event in events)


_MEASURED_USAGE = {
    "input_tokens": 4,
    "cache_creation_input_tokens": 1_204,
    "cache_read_input_tokens": 26_079,
    "output_tokens": 311,
}


class _FakeRun:
    """run_fn seam stub — 호출마다 준비된 RunResult 를 내고 argv/stdin 을 기록한다."""

    def __init__(self, *results: dict):
        self._results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append({"argv": argv, "stdin_text": stdin_text, "harness": harness})
        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        return dict(result)


def _ok(stdout: str) -> dict:
    return {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}


def _write_prompt(directory: Path, text: str = "직전 지적을 해소했다. 다시 검토하라.") -> Path:
    prompt = directory / "prompt.md"
    prompt.write_text(text, encoding="utf-8")
    return prompt


def _run_main(pd, monkeypatch, argv, run_fn=None, conf=None):
    monkeypatch.setattr(pd, "local_config", lambda: dict(conf or {"delegate.enabled": "true"}))
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *a, **k: True)
    # 이 파일은 resume wire/ledger 축을 검증한다. code-reviewer의 실 ticket-copy 왕복은 전용
    # growth 회귀가 소유하므로 여기서는 필수 --ticket을 유지하되 copy transport만 격리한다.
    monkeypatch.setattr(pd, "prepare_ticket_copy", lambda **_kw: None)
    return pd.main(list(argv), run_fn=run_fn)


def _argv(prompt: Path, cwd: Path, out_dir: Path, *extra: str,
          role: str = "code-reviewer", harness: str = "claude") -> list[str]:
    argv = [
        "--role", role, "--prompt-file", str(prompt), "--cwd", str(cwd),
        "--harness", harness, "--model", "opus",
        "--output-dir", str(out_dir),
    ]
    if role == "code-reviewer" and "--ticket" not in extra and "--gate" not in extra:
        argv += ["--ticket", TICKET_ID]
    return [*argv, *extra]


# ── 장부 시드 (실 엔진 경로로 기록 — 대역 장부를 만들지 않는다) ────────────────────

def _recent_seed_time() -> datetime.datetime:
    """prune 창(완료 7일·미마감 30일) 안에 항상 머무는 시드 기준 시각(고정 달력 날짜 금지)."""
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)


def _seed_record(relay, ledger_path: Path, *, role: str = "code-reviewer",
                 rc: int = 0, started: datetime.datetime | None = None,
                 surface: str = "delegate", harness: str = "claude",
                 finish: bool = True, extra: dict | None = None,
                 start_extra: dict | None = None) -> str:
    # 장부 prune(pm_relay.RAW_LEDGER_COMPLETED_DAYS=7·UNFINISHED_DAYS=30) 창 안에 항상 머무는
    # 상대 시각. 고정 달력 날짜를 쓰면 그 날짜 + 7일이 지나는 순간 시드가 prune 되는 시간폭탄이 된다.
    started = started or _recent_seed_time()
    record_id = relay.start_raw_record(
        ledger_path,
        surface=surface, harness=harness, model="opus", role=role,
        raw_path=ledger_path.parent / f"seed_{started.isoformat()}.txt",
        attempt="primary", now=started, extra=start_extra or {},
    )
    if finish:
        relay.finish_raw_record(
            ledger_path, record_id, rc=rc, elapsed_sec=1.0, silence_sec=None,
            now=started + datetime.timedelta(seconds=5), extra=extra or {},
        )
    return record_id


def _rows(ledger_path: Path) -> list[dict]:
    return json.loads(ledger_path.read_text(encoding="utf-8"))["records"]


def _row(ledger_path: Path, record_id: str) -> dict:
    return next(row for row in _rows(ledger_path) if row["id"] == record_id)


# ══ ⑦ usage 4필드 분해 (수집 불가 = 필드 부재) ═══════════════════════════════

def test_claude_usage_is_decomposed_into_four_fields(relay):
    """스칼라 합 뒤에 가려져 있던 캐시 **재적재**(cache_read)와 **새 적재**(cache_creation)를 분리한다."""
    _sid, _reply, used, usage = relay._parse_stream_json_events(
        _claude_wire(usage=_MEASURED_USAGE).splitlines())
    assert usage == {
        "input": 4, "cache_creation": 1_204, "cache_read": 26_079, "output": 311,
    }
    assert used == sum(usage.values())            # 회전 판정 스칼라는 불변


def test_absent_usage_fields_are_omitted_not_zero_filled(relay):
    """wire 에 없는 필드는 **적지 않는다** — 0 은 '0 토큰'이라는 다른 사실이다."""
    partial = relay._claude_usage_fields({"input_tokens": 7})
    assert partial == {"input": 7}
    assert "cache_read" not in partial and "output" not in partial
    assert relay._claude_usage_fields({}) is None
    assert relay._claude_usage_fields(None) is None
    # 형식이 깨진 값이 하나라도 있으면 그 관측 전체를 버린다(부분 신뢰 값 금지).
    assert relay._claude_usage_fields({"input_tokens": -1}) is None
    assert relay._claude_usage_fields({"output_tokens": True}) is None


def test_scalar_usage_contract_of_public_parser_is_unchanged(relay):
    """공개 3-tuple 계약(회전 판정 소비면)은 그대로 — 파서는 하나고 소비면만 둘이다."""
    lines = _claude_wire(usage=_MEASURED_USAGE).splitlines()
    assert relay.parse_stream_json(lines) == relay._parse_stream_json_events(lines)[:3]


def test_harness_without_breakdown_records_no_usage_field(relay):
    """분해 관측이 없는 축은 추정 매핑 대신 **필드 부재**(false-정밀 금지)."""
    codex_wire = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "th-1"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ])
    observed = relay.extract_harness_result("codex", codex_wire)
    assert (observed.reply, observed.session_id, observed.usage) == ("OK", "th-1", None)

    opencode_wire = json.dumps({
        "type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "OK"}})
    observed = relay.extract_harness_result("opencode", opencode_wire)
    assert (observed.reply, observed.session_id, observed.usage) == ("OK", "ses_1", None)


def test_reply_only_facade_keeps_both_call_sites_intact(relay, pd):
    """회신 전용 seam 의 반환 계약(str|None)은 불변 — 두 호출부가 그대로 산다."""
    assert relay.extract_harness_reply("claude", _claude_wire("답")) == "답"
    assert relay.extract_harness_reply("claude", _claude_wire("   ")) is None
    assert pd.extract_reply("claude", _claude_wire("답")) == "답"      # 위임 호출부
    external = _load("external_review")
    source = Path(external.__file__).read_text(encoding="utf-8")
    assert source.count("relay.extract_harness_reply(") == 1           # 리뷰어 호출부
    with pytest.raises(relay.HarnessContractError, match="미지원 harness"):
        relay.extract_harness_reply("gemini", "{}")


# ══ ③ 세션 id 형식 가드 ═══════════════════════════════════════════════════════

def test_session_id_guard_rejects_flag_shaped_values(relay):
    """장부가 손상돼도 `--` 로 시작하는 문자열은 재개 argv 에 실리지 않는다."""
    assert relay.is_resumable_session_id(SESSION_ID) is True
    for corrupted in ("--dangerously-skip-permissions", "", "not-a-uuid", None,
                      f"{SESSION_ID} --print", 12345):
        assert relay.is_resumable_session_id(corrupted) is False
    with pytest.raises(relay.HarnessContractError, match="세션 id 형식"):
        relay.build_claude_argv("opus", None, "code-reviewer",
                                "--dangerously-skip-permissions")


def test_claude_argv_carries_resume_only_when_requested(relay):
    """재개 요청이 없으면 argv 는 종전과 byte-동일하다."""
    fresh = relay.build_claude_argv("opus", None, "code-reviewer")
    assert "--resume" not in fresh
    resumed = relay.build_claude_argv("opus", None, "code-reviewer", SESSION_ID)
    assert resumed[:len(fresh)] == fresh
    assert resumed[resumed.index("--resume") + 1] == SESSION_ID


def test_resume_support_is_a_declaration_table(relay):
    """지원 축은 분기가 아니라 선언표 — codex는 실측 승격, opencode는 무배선이다."""
    assert relay.HARNESS_RESUME_SUPPORT == {
        "claude": True, "codex": True, "opencode": False}
    assert relay.harness_supports_resume("claude") is True
    assert relay.harness_supports_resume("codex") is True
    assert relay.harness_supports_resume("opencode") is False
    assert relay.harness_supports_resume("gemini") is False


# ══ ② 후보 선택 결정성 ═══════════════════════════════════════════════════════

def _candidate(**overrides) -> dict:
    row = {
        "id": "rec-1", "surface": "delegate", "role": "code-reviewer", "rc": 0,
        "harness": "claude", "ticket": TICKET_ID,
        "started_at": "2026-08-08T10:00:00+00:00", "session_id": SESSION_ID,
    }
    row.update(overrides)
    return row


def test_candidate_selection_is_deterministic_latest_success(pd):
    """같은 티켓·role·rc=0 중 started_at 최신 1건 — 여러 레코드에서도 고르는 값이 하나다."""
    rows = [
        _candidate(id="old", started_at="2026-08-08T09:00:00+00:00"),
        _candidate(id="newest", started_at="2026-08-08T11:00:00+00:00"),
        _candidate(id="middle", started_at="2026-08-08T10:30:00+00:00"),
    ]
    picked = pd.select_resume_record(
        rows, selector=TICKET_ID, role="code-reviewer", harness="claude")
    assert picked["id"] == "newest"
    # 입력 순서가 달라져도 같은 값을 고른다(정렬 의존 아님).
    assert pd.select_resume_record(
        list(reversed(rows)), selector=TICKET_ID, role="code-reviewer",
        harness="claude")["id"] == "newest"


def test_candidate_selection_excludes_other_axes(pd):
    """다른 role·하네스·실패/미마감·리뷰 축 레코드는 후보가 아니다.

    하네스 축이 특히 중요하다 — 다른 축의 세션 id 도 형식 가드를 통과할 수 있어(codex
    thread id 가 uuid 표기다) 형식만으로는 남의 세션을 거르지 못한다."""
    newest = "2026-08-08T23:00:00+00:00"
    for excluded in (
        _candidate(id="x", role="developer", started_at=newest),
        _candidate(id="x", harness="codex", started_at=newest,
                   session_id="01994a1e-2b3c-7def-8123-456789abcdef"),
        _candidate(id="x", rc=1, started_at=newest),
        _candidate(id="x", rc=None, started_at=newest),
        _candidate(id="x", surface="external-review", started_at=newest),
        _candidate(id="x", ticket="T-" + "0001", started_at=newest),
    ):
        rows = [_candidate(id="keeper"), excluded]
        assert pd.select_resume_record(
            rows, selector=TICKET_ID, role="code-reviewer",
            harness="claude")["id"] == "keeper"
    assert pd.select_resume_record(
        [_candidate(id="x", role="developer")], selector=TICKET_ID,
        role="code-reviewer", harness="claude") is None
    # 같은 티켓·role 에 codex 위임만 있으면 claude 재개 후보는 없다(남의 세션 금지).
    assert pd.select_resume_record(
        [_candidate(id="x", harness="codex")], selector=TICKET_ID,
        role="code-reviewer", harness="claude") is None


def test_record_id_selector_is_exact_match(pd):
    """티켓 표기가 아닌 지시자는 장부 레코드 id 정확일치."""
    rows = [_candidate(id="rec-a", ticket=None), _candidate(id="rec-b", ticket=None)]
    assert pd.select_resume_record(
        rows, selector="rec-b", role="code-reviewer",
        harness="claude")["id"] == "rec-b"
    assert pd.select_resume_record(
        rows, selector="rec-zzz", role="code-reviewer", harness="claude") is None


# ── 후보 정밀화 (T-0601 ⑩) ──────────────────────────────────────────────────
# rc 0 이라고 다 재개의 입력이 아니다. 회신을 못 읽은 라운드는 이어받을 세션 id 자체가 없고,
# 세션 불일치로 끝난 라운드의 세션 id 는 **남의 세션** 것이다. 둘 다 선택 *전에* 거른다 — 뒤에서
# 걸러 최신이 탈락하면 유효한 이전 후보가 있어도 재사용이 통째로 불가로 접힌다.


@pytest.mark.parametrize("broken", [
    {"session_id": None},                    # 회신 wire 관측 실패
    {"session_id": ""},
    {"session_id": "   "},
    {"resume_matched": False},               # 다른 세션이 답한 라운드 — 남의 세션 id 다
])
def test_candidate_selection_excludes_unusable_records(pd, broken):
    """이어받을 세션이 없는 레코드는 최신이어도 후보가 아니고, 유효한 이전 건이 선택된다."""
    newest = _candidate(id="unusable", started_at="2026-08-08T23:00:00+00:00", **broken)
    keeper = _candidate(id="keeper", started_at="2026-08-08T10:00:00+00:00")

    picked = pd.select_resume_record(
        [keeper, newest], selector=TICKET_ID, role="code-reviewer", harness="claude")
    assert picked["id"] == "keeper"
    # 입력 순서가 달라도 같은 값 — 결정성은 정밀화 뒤에도 유지된다.
    assert pd.select_resume_record(
        [newest, keeper], selector=TICKET_ID, role="code-reviewer",
        harness="claude")["id"] == "keeper"
    # 유효 후보가 하나도 없으면 재사용 불가(None) — fresh 로 간다.
    assert pd.select_resume_record(
        [newest], selector=TICKET_ID, role="code-reviewer", harness="claude") is None


# ── 유효 성공만 후보다 (T-0603 ③) ─────────────────────────────────────────
# codex R3 지적: 장부 `rc` 는 subprocess 종료 코드로 **회신 검증보다 먼저** 확정되고, 유효한
# reply 가 없어도 세션 id 는 저장된다 — 실제로는 실패한 실행이 rc 0 성공 후보로 재개된다.
# 유효 성공 여부를 레코드 필드(`reply_extracted`)로 남기고 후보 조건에 편입해 그 경로를 닫는다.


def test_reply_extraction_failure_is_not_a_success_candidate(pd):
    """재현: rc 0 · 세션 id 있음 · **회신 미추출** — 최신이어도 후보가 아니다 (DoD)."""
    newest = _candidate(id="no-reply", started_at="2026-08-08T23:00:00+00:00",
                        reply_extracted=False)
    keeper = _candidate(id="keeper", started_at="2026-08-08T10:00:00+00:00",
                        reply_extracted=True)

    assert pd.select_resume_record(
        [keeper, newest], selector=TICKET_ID, role="code-reviewer",
        harness="claude")["id"] == "keeper"
    assert pd.select_resume_record(
        [newest], selector=TICKET_ID, role="code-reviewer", harness="claude") is None


def test_legacy_records_without_the_field_keep_the_rc_rule(pd):
    """필드 자체가 없는 **구레코드**는 종전대로 rc 기준이다 (하위호환 — 정상 재개 무회귀)."""
    legacy = _candidate(id="legacy")
    assert "reply_extracted" not in legacy
    assert pd.select_resume_record(
        [legacy], selector=TICKET_ID, role="code-reviewer",
        harness="claude")["id"] == "legacy"


@pytest.mark.parametrize("broken, needle", [
    ({"session_id": None}, "세션 id 가 관측되지 않았다"),
    ({"resume_matched": False}, "세션 불일치로 끝난 라운드"),
    ({"reply_extracted": False}, "유효 회신이 없던 라운드"),
])
def test_unusable_record_gets_its_own_reason_not_retention(pd, broken, needle):
    """있는데 못 이어받는 것과 정말 없는 것은 처방이 다르다 — 사유를 갈라 낸다.

    특히 레코드 id 를 **직접 지목**한 실행에 "보존 창 밖"이라 답하면 방금 눈으로 본 레코드를
    없다고 하는 셈이다."""
    row = _candidate(id="rec-x", **broken)
    reason = pd.resume_unusable_reason(
        [row], selector="rec-x", role="code-reviewer", harness="claude")
    assert reason is not None and needle in reason and "rec-x" in reason
    assert "보존 창" not in reason


def test_truly_absent_record_keeps_the_retention_reason(pd):
    """맞는 레코드가 아예 없으면 종전 안내(보존 창 밖·아직 없는 라운드) 그대로다."""
    assert pd.resume_unusable_reason(
        [_candidate(id="rec-x")], selector="rec-zzz", role="code-reviewer",
        harness="claude") is None


def test_resume_resolution_surfaces_the_unusable_reason(pd, relay, tmp_path, capsys):
    """실 해소 경로가 그 사유를 stderr 로 낸다 (판정 헬퍼가 배선돼 있다)."""
    out_dir = tmp_path / "raw"
    ledger_path = out_dir / "raw_outputs.json"
    record_id = _seed_record(relay, ledger_path, start_extra={"ticket": TICKET_ID})

    plan = pd.resolve_resume_plan(
        record_id, harness="claude", role="code-reviewer",
        task_text="본문", output_dir=out_dir)

    assert plan is None
    err = capsys.readouterr().err
    assert "세션 id 가 관측되지 않았다" in err and "보존 창" not in err


def test_matched_resume_records_stay_candidates(pd):
    """`resume_matched=True` 인 재개 라운드는 정상 후보다 — 사슬이 이어진다."""
    row = _candidate(id="chained", resume_matched=True,
                     resume_from_session_id=SESSION_ID)
    assert pd.select_resume_record(
        [row], selector=TICKET_ID, role="code-reviewer", harness="claude")["id"] == "chained"


def test_retention_window_bounds_resume_availability(pd, relay, tmp_path):
    """장부 완료 보존 창 밖 레코드는 정리돼 후보에서 사라진다 — 창 밖 재위임은 자연 fresh."""
    ledger_path = tmp_path / "raw_outputs.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    stale = now - datetime.timedelta(days=relay.RAW_LEDGER_COMPLETED_DAYS + 1)
    _seed_record(relay, ledger_path, started=stale,
                 start_extra={"ticket": TICKET_ID}, extra={"session_id": SESSION_ID})
    # 다음 기록이 정리 규칙을 태운다(장부는 쓰기 시점에 정리된다).
    _seed_record(relay, ledger_path, started=now, start_extra={"ticket": "T-" + "0002"})
    rows = relay.raw_records(ledger_path)
    assert pd.select_resume_record(
        rows, selector=TICKET_ID, role="code-reviewer", harness="claude") is None


# ══ ① 저장 축 단일 (raw 장부 레코드 행) ══════════════════════════════════════

def test_structured_fields_live_on_the_single_delegate_ledger_row(pd, relay, tmp_path):
    """세션 id·usage·must_fix·기준 rev·티켓이 **한 행**에 실리고 정리 왕복 후에도 살아남는다."""
    out_dir = tmp_path / "raw"
    ledger_path = out_dir / "raw_outputs.json"
    attempt = pd._execute_attempt(
        harness="claude", model="opus", reasoning=None, role="code-reviewer",
        cwd=tmp_path, prompt="p", timeout=60, output_dir=out_dir,
        run_fn=_FakeRun(_ok(_claude_wire(
            "판정: 반려\n**must-fix**:\n- 세션 id 판정을 rc 로 하지 마라\n- 원장 0 채우기 금지",
            usage=_MEASURED_USAGE))),
        attempt="primary", ticket=TICKET_ID, base_rev="deadbeef",
    )
    rows = _rows(ledger_path)
    assert len(rows) == 1                          # 위임 실행 1건 = 장부 행 1개
    row = rows[0]
    assert row["session_id"] == SESSION_ID == attempt.session_id
    assert row["usage"] == {
        "input": 4, "cache_creation": 1_204, "cache_read": 26_079, "output": 311}
    assert row["must_fix_items"] == [
        "세션 id 판정을 rc 로 하지 마라", "원장 0 채우기 금지"]
    assert row["base_rev"] == "deadbeef" and row["ticket"] == TICKET_ID
    assert row["rc"] == 0 and row["finished_at"]   # 공통 스키마도 그대로
    assert row["reply_extracted"] is True          # 회신 검증까지 통과한 유효 성공

    # 같은 장부에 한 건 더 쓰면 정리 규칙이 다시 돈다 — 행 id 와 구조화 필드가 생존해야 한다.
    _seed_record(relay, ledger_path, started=datetime.datetime.now(datetime.timezone.utc))
    survived = _row(ledger_path, row["id"])
    assert survived["session_id"] == SESSION_ID
    assert survived["must_fix_items"] == row["must_fix_items"]


def test_a_pass_replys_none_marker_is_not_a_must_fix_item(pd, tmp_path):
    """통과 응답의 `- 없음` 은 항목이 아니다 — `["없음"]` 으로 박제되면 다음 라운드 delta 가
    '없음'을 고칠 지적으로 되읽는다. 정규화는 추가 리뷰 경로와 **같은 술어**(`_is_none_items`)를
    쓴다 — 두 축이 각자 판별하면 같은 회신이 축마다 다르게 남는다 (T-0604 ⑤)."""
    assert pd._observed_must_fix_items(
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n") == []
    assert pd._observed_must_fix_items(
        "판정: 반려\n\n**must-fix** (반드시 수정):\n- 실 지적\n") == ["실 지적"]

    out_dir = tmp_path / "raw"
    ledger_path = out_dir / "raw_outputs.json"
    pd._execute_attempt(
        harness="claude", model="opus", reasoning=None, role="code-reviewer",
        cwd=tmp_path, prompt="p", timeout=60, output_dir=out_dir,
        run_fn=_FakeRun(_ok(_claude_wire(
            "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"))),
        attempt="primary", ticket=TICKET_ID, base_rev="deadbeef",
    )
    # 항목 0 건은 종전대로 필드 자체를 만들지 않는다(빈 목록과 동치·구레코드 구분 규칙 불변).
    assert _rows(ledger_path)[0].get("must_fix_items", []) == []


def test_rc_zero_without_a_reply_is_recorded_as_an_invalid_success(pd, tmp_path):
    """rc 0 + 세션 id 관측 + **회신 없음** 실행은 장부에 `reply_extracted: false` 로 남고
    그 레코드는 재개 후보가 아니다 (기록 → 후보 조건 배선 e2e·DoD)."""
    out_dir = tmp_path / "raw"
    ledger_path = out_dir / "raw_outputs.json"
    # init 이벤트만 있는 wire — 세션 id 는 관측되지만 최종 회신(result)이 없다.
    truncated = json.dumps(
        {"type": "system", "subtype": "init", "session_id": SESSION_ID})
    pd._execute_attempt(
        harness="claude", model="opus", reasoning=None, role="code-reviewer",
        cwd=tmp_path, prompt="p", timeout=60, output_dir=out_dir,
        run_fn=_FakeRun(_ok(truncated)), attempt="primary", ticket=TICKET_ID,
    )
    row = _rows(ledger_path)[0]
    assert (row["rc"], row["session_id"]) == (0, SESSION_ID)   # rc 는 성공처럼 보인다
    assert row["reply_extracted"] is False                     # 유효 성공은 아니다
    assert pd.select_resume_record(
        [row], selector=TICKET_ID, role="code-reviewer", harness="claude") is None


def test_ledger_extra_cannot_overwrite_common_schema(relay, tmp_path):
    """표면별 extra 가 공통 스키마 키를 덮어쓰지 못한다(조회면의 뜻이 표면마다 갈리지 않게)."""
    ledger_path = tmp_path / "raw_outputs.json"
    record_id = _seed_record(
        relay, ledger_path, rc=0,
        extra={"rc": 99, "surface": "spoofed", "session_id": SESSION_ID},
    )
    row = _row(ledger_path, record_id)
    assert row["rc"] == 0 and row["surface"] == "delegate"
    assert row["session_id"] == SESSION_ID


def test_review_round_ledger_is_never_touched(pd, monkeypatch, tmp_path, capsys):
    """위임 회계는 별도 네임스페이스 — 리뷰 라운드 장부는 생성도 갱신도 되지 않는다."""
    external = _load("external_review")
    pm_home = tmp_path / "pm_home"
    (pm_home / ".project_manager" / ".local").mkdir(parents=True)
    monkeypatch.setattr(external, "REPO", pm_home)
    rounds_ledger = external._round_ledger_path()
    assert not rounds_ledger.exists()

    out_dir = tmp_path / "raw"
    prompt = _write_prompt(tmp_path)
    ledger_path = out_dir / "raw_outputs.json"
    out_dir.mkdir()
    _seed_record(pd._load_relay(), ledger_path, start_extra={"ticket": TICKET_ID},
                 extra={"session_id": SESSION_ID})
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))
    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)

    assert rc == 0                                  # 새 상한/거부 rc 없음 — 기록만 한다
    assert not rounds_ledger.exists()
    assert len(_rows(ledger_path)) == 2             # 시드 1 + 이번 실행 1


# ══ ④⑤ resume 라운드 실행 (delta 송신·일치 판정·폴백) ════════════════════════

def _resume_fixture(pd, tmp_path, *, must_fix=("직전 지적 A", "직전 지적 B"),
                    base_rev="cafebabe", session_id=SESSION_ID,
                    role="code-reviewer"):
    out_dir = tmp_path / "raw"
    out_dir.mkdir()
    ledger_path = out_dir / "raw_outputs.json"
    raw_path = out_dir / "seed_raw.txt"
    # raw 박제 파일에는 장부에 없는 **독약 문자열**을 심는다 — delta 가 이 파일을 다시 읽으면 잡힌다.
    raw_path.write_text(
        "# pm_delegate raw 출력 (감사)\nPOISON-ONLY-IN-RAW-TXT\n", encoding="utf-8")
    relay = pd._load_relay()
    record_id = relay.start_raw_record(
        ledger_path, surface="delegate", harness="claude", model="opus",
        role=role, raw_path=raw_path, attempt="primary",
        now=_recent_seed_time(),
        extra={"ticket": TICKET_ID, "base_rev": base_rev},
    )
    relay.finish_raw_record(
        ledger_path, record_id, rc=0, elapsed_sec=1.0, silence_sec=None,
        now=_recent_seed_time() + datetime.timedelta(minutes=1),
        extra={"session_id": session_id, "must_fix_items": list(must_fix)},
    )
    return out_dir, ledger_path, record_id


def test_resume_round_sends_delta_on_the_reused_session(pd, monkeypatch, tmp_path, capsys):
    """재사용 라운드 = `--resume <sid>` argv + delta payload 1회 송신(full 재적재 없음)."""
    out_dir, ledger_path, record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path, "지적 두 건 모두 해소했다.")
    fake = _FakeRun(_ok(_claude_wire("판정: 통과", usage=_MEASURED_USAGE)))

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)

    assert rc == 0 and len(fake.calls) == 1
    argv = fake.calls[0]["argv"]
    assert argv[argv.index("--resume") + 1] == SESSION_ID
    sent = fake.calls[0]["stdin_text"]
    assert "직전 지적 A" in sent and "직전 지적 B" in sent   # 장부 구조화 필드
    assert "cafebabe" in sent                                # 장부 기준 rev
    assert "지적 두 건 모두 해소했다." in sent                # 호출자 원문(불투명 전달)
    assert "POISON-ONLY-IN-RAW-TXT" not in sent              # raw .txt 재파싱 0
    assert _PREAMBLE_MARKER not in sent                       # 역할 preamble 재전송 없음
    # 이번 실행 레코드가 이어받은 세션과 일치 결과를 남긴다.
    row = max(_rows(ledger_path), key=lambda item: item["started_at"])
    assert row["attempt"] == pd.RESUME_ATTEMPT
    assert row["resume_from_session_id"] == SESSION_ID
    assert row["resume_matched"] is True


def test_resume_success_is_session_identity_not_rc(pd, monkeypatch, tmp_path, capsys):
    """재사용 성공 판정은 rc 가 아니라 **세션 정체성**이다 — rc=0 이어도 다른 세션이면 실패다.

    관측은 재실행 금지 클래스 **밖**에서 한다(그 클래스는 rc=1 fail-loud 로 닫히는 별도 축이고
    전용 테스트가 소유한다). researcher 는 T-0696 에서 그 클래스로 옮겨졌으므로 이 축에서는
    분류만 임시로 되돌려 폴백 경로를 본다.
    """
    monkeypatch.setattr(
        pd, "RESUME_MUTATING_ROLES", pd.RESUME_MUTATING_ROLES - {"researcher"},
    )
    out_dir, _ledger_path, _record_id = _resume_fixture(pd, tmp_path, role="researcher")
    prompt = _write_prompt(tmp_path, "해소 주장 본문")
    fake = _FakeRun(
        _ok(_claude_wire("맥락 없는 답", session_id=OTHER_SESSION_ID)),
        _ok(_claude_wire("판정: 통과")),
    )

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID,
                         role="researcher"), fake)
    err = capsys.readouterr().err

    assert rc == 0 and len(fake.calls) == 2
    assert "--resume" not in fake.calls[1]["argv"]           # 폴백은 fresh 스폰
    resent = fake.calls[1]["stdin_text"]
    assert _PREAMBLE_MARKER in resent                        # full payload(역할 preamble 포함)
    assert "해소 주장 본문" in resent
    assert "직전 지적 A" not in resent                        # delta-only 폴백 금지
    assert "세션 재사용 실패" in err and OTHER_SESSION_ID in err


def test_resume_failure_without_reply_falls_back_to_full_payload(
        pd, monkeypatch, tmp_path, capsys):
    """미존재 id 의 실패(rc≠0·회신 0)도 같은 축으로 처리된다 — 세션 id 부재 = 불일치."""
    out_dir, _ledger_path, _record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(
        {"returncode": 1, "stdout": "", "stderr": "No conversation found",
         "timed_out": False},
        _ok(_claude_wire("판정: 통과")),
    )

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)
    err = capsys.readouterr().err

    assert rc == 0 and len(fake.calls) == 2
    assert "세션 재사용 실패" in err and "회신 없음" in err
    assert _PREAMBLE_MARKER in fake.calls[1]["stdin_text"]


# ── 티켓 식별자 계승 (T-0601 ⑩) ─────────────────────────────────────────────


def test_resume_round_record_inherits_the_ticket(pd, monkeypatch, tmp_path):
    """ticket-copy 비대상 researcher 재개도 레코드의 티켓을 계승해 선택 사슬을 유지한다.

    안 남기면 이 라운드 레코드가 티켓 지시자 선택(최신 1건)에서 빠지고, 다음 `--resume-from
    T-NNNN` 이 **직전 라운드가 아닌 그 앞 라운드**를 이어받아 사슬이 끊긴다."""
    out_dir, ledger_path, _record_id = _resume_fixture(pd, tmp_path, role="researcher")
    prompt = _write_prompt(tmp_path, "해소했다.")
    fake = _FakeRun(_ok(_claude_wire("판정: 통과", session_id=SESSION_ID)))

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID,
                         role="researcher"), fake)

    assert rc == 0
    latest = max(_rows(ledger_path), key=lambda row: row["started_at"])
    assert latest["ticket"] == TICKET_ID
    # 그리고 그 레코드가 다음 재개의 후보로 실제 선택된다(사슬 유지의 진짜 관측치).
    assert pd.select_resume_record(
        _rows(ledger_path), selector=TICKET_ID, role="researcher",
        harness="claude")["id"] == latest["id"]


def test_explicit_ticket_wins_over_the_inherited_one(pd, monkeypatch, tmp_path):
    """명시 `--ticket` 이 우선이다 — 계승은 미지정일 때의 폴백이다."""
    other = "T-" + "0888"
    # 이 테스트의 축은 resume record ticket 우선순위다. 성장 transport는 별도 회귀가
    # 소유하므로 prepare seam을 no-copy로 격리해 Claude resume wire를 그대로 관측한다.
    monkeypatch.setattr(pd, "prepare_ticket_copy", lambda **_kw: None)
    out_dir, ledger_path, _record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))

    rc = _run_main(
        pd, monkeypatch,
        _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID, "--ticket", other),
        fake)

    assert rc == 0
    latest = max(_rows(ledger_path), key=lambda row: row["started_at"])
    assert latest["ticket"] == other


def test_record_id_selector_resume_also_inherits_the_ticket(pd, monkeypatch, tmp_path):
    """레코드 id 로 재개해도 그 레코드의 티켓을 계승한다 (지시자 형태와 무관한 규칙)."""
    out_dir, ledger_path, record_id = _resume_fixture(pd, tmp_path, role="researcher")
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", record_id,
                         role="researcher"), fake)

    assert rc == 0
    latest = max(_rows(ledger_path), key=lambda row: row["started_at"])
    assert latest["ticket"] == TICKET_ID


# ── write 역할 재개 불일치 fail-loud (T-0601 ⑪) ──────────────────────────────
# 불일치 실행도 **쓰기 권한**으로 돌았으므로 이미 트리를 고쳤을 수 있다. 그 위에 같은 지시를
# fresh 로 한 번 더 태우면 같은 편집이 두 번 적용되거나(중복) 반쯤 고친 트리에 다른 판단이
# 얹힌다(충돌). read 계열은 트리를 안 만지므로 현행 재실행을 유지한다.


def _write_role_fixture(pd, tmp_path, role: str):
    """write 역할 레코드로 재개 픽스처를 세운다(role 은 후보 선택 키다)."""
    out_dir = tmp_path / "raw"
    out_dir.mkdir()
    ledger_path = out_dir / "raw_outputs.json"
    relay = pd._load_relay()
    record_id = relay.start_raw_record(
        ledger_path, surface="delegate", harness="claude", model="opus",
        role=role, raw_path=out_dir / "seed_raw.txt", attempt="primary",
        now=_recent_seed_time(),
        extra={"ticket": TICKET_ID, "base_rev": "cafebabe"},
    )
    relay.finish_raw_record(
        ledger_path, record_id, rc=0, elapsed_sec=1.0, silence_sec=None,
        now=_recent_seed_time() + datetime.timedelta(minutes=1),
        extra={"session_id": SESSION_ID, "must_fix_items": ["직전 지적 A"]},
    )
    return out_dir, ledger_path


_NO_RERUN_ROLES = sorted({"developer", "architect", "code-reviewer", "researcher"})


def test_no_rerun_class_is_pinned_to_the_engine_classification():
    """재실행 금지 클래스 전수 고정 — 역할이 들어오고 나갈 때 이 목록이 red 로 알려준다.

    researcher 는 T-0696 에서 편입됐다: 제품 트리에는 read 지만 자기 티켓 사본 절을 기록하므로
    세션 불일치 뒤 fresh 재실행이 같은 절을 두 번 쓸 수 있다(code-reviewer 와 같은 근거).
    """
    assert set(_NO_RERUN_ROLES) == set(pd_module().RESUME_MUTATING_ROLES)
    assert set(pd_module().READ_ROLES) <= set(pd_module().RESUME_MUTATING_ROLES)


@pytest.mark.parametrize("role", _NO_RERUN_ROLES)
def test_mutating_role_resume_mismatch_fails_loud(pd, monkeypatch, tmp_path, capsys, role):
    """재실행 금지 클래스 불일치 → rc 1 · fresh 재실행 0 · 확인 안내 (중복 기록 차단)."""
    assert role in pd.RESUME_MUTATING_ROLES
    out_dir, _ledger_path = _write_role_fixture(pd, tmp_path, role)
    prompt = _write_prompt(tmp_path, "구현했다.")
    fake = _FakeRun(_ok(_claude_wire("다른 세션의 답", session_id=OTHER_SESSION_ID)))

    rc = _run_main(
        pd, monkeypatch,
        _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID, role=role), fake)

    err = capsys.readouterr().err
    assert rc == 1
    assert len(fake.calls) == 1, f"write 역할인데 fresh 재실행이 일어남: {fake.calls}"
    assert "세션 재사용 실패" in err and role in err
    assert "트리를 이미 고쳤을 수 있어" in err and "git status" in err
    assert "재위임은 명시 재호출만" in err              # 무음 대체 금지 안내(단일 깔때기)


def test_id_mismatch_rerun_survives_for_the_unclassified_role_axis(
        pd, monkeypatch, tmp_path, capsys):
    """id 불일치 fresh 재실행 경로는 **분류 밖 역할**에만 남는다(T-0696 이후 위임 4역할은 전부 금지).

    옛 전제("read 역할이라 재실행")는 researcher 가 자기 티켓 절을 기록하면서 사라졌다. 경로
    자체는 살아 있어야 하므로(분류가 다시 갈릴 때의 기본값) 분류에서 뺀 역할로 관측한다.
    """
    role = "researcher"
    monkeypatch.setattr(
        pd, "RESUME_MUTATING_ROLES", pd.RESUME_MUTATING_ROLES - {role},
    )
    out_dir, _ledger_path = _write_role_fixture(pd, tmp_path, role)
    prompt = _write_prompt(tmp_path, "해소 주장 본문")
    fake = _FakeRun(
        _ok(_claude_wire("맥락 없는 답", session_id=OTHER_SESSION_ID)),
        _ok(_claude_wire("판정: 통과")),
    )

    rc = _run_main(
        pd, monkeypatch,
        _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID, role=role), fake)

    assert rc == 0 and len(fake.calls) == 2
    assert _PREAMBLE_MARKER in fake.calls[1]["stdin_text"]
    assert "1회 다시 실행합니다" in capsys.readouterr().err


def test_write_role_infrastructure_failure_is_not_the_resume_axis(
        pd, monkeypatch, tmp_path, capsys):
    """스폰 실패는 재사용 축의 실패가 아니다 — write 역할도 기존 인프라 경로 그대로다."""
    out_dir, _ledger_path = _write_role_fixture(pd, tmp_path, "developer")
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun({"returncode": 127, "stdout": "",
                     "stderr": "하네스 claude 실행 불가: not found",
                     "timed_out": False, pd.RUN_RESULT_LAUNCH_FAILED: True})

    rc = _run_main(
        pd, monkeypatch,
        _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID, role="developer"),
        fake)

    err = capsys.readouterr().err
    assert rc == 1 and len(fake.calls) == 1
    assert "트리를 이미 고쳤을 수 있어" not in err        # 재사용 축 안내가 아니다
    assert "재위임은 명시 재호출만" in err


def _fallback_conf() -> dict:
    """config 매핑 + 명시 폴백(CLI 완전지정이 아니어야 폴백이 살아 있다)."""
    return {
        "delegate.enabled": "true",
        "delegate.code-reviewer.harness": "claude",
        "delegate.code-reviewer.model": "opus",
        "delegate.code-reviewer.fallback.harness": "codex",
        "delegate.code-reviewer.fallback.model": "gpt-x",
    }


def _mapped_argv(prompt: Path, cwd: Path, out_dir: Path, *extra: str) -> list[str]:
    """config 매핑으로 해소되는 argv(--harness/--model 미지정) + 고정 timeout."""
    return [
        "--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(cwd),
        "--output-dir", str(out_dir), "--timeout", "600", "--ticket", TICKET_ID,
        *extra,
    ]


def test_infrastructure_failure_goes_to_fallback_without_a_fresh_rerun(
        pd, monkeypatch, tmp_path, capsys):
    """스폰 실패는 재사용 축의 실패가 아니다 — full 재실행 없이 기존 폴백으로 직행한다.

    인프라 실패도 세션 id 를 못 남기므로 형식만 보면 '불일치'다. 그때 재실행까지 태우면 한
    위임이 세 번 스폰되고(재사용 → fresh → 폴백) 호출층 상한 선언과 어긋난다."""
    out_dir, _ledger_path, _record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path)
    # 폴백은 codex 로 떨어진다 — codex code-reviewer preflight(`_preflight_codex_read_exec_root`
    # — T-0844)가 스폰 전에 --cwd 저장소/staged 형상을 요구한다.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "staged.txt"], cwd=tmp_path, check=True, capture_output=True,
    )
    fake = _FakeRun(
        {"returncode": 127, "stdout": "", "stderr": "하네스 claude 실행 불가: not found",
         "timed_out": False, pd.RUN_RESULT_LAUNCH_FAILED: True},
        _ok("\n".join([
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "판정: 통과"}}),
        ])),
    )

    rc = _run_main(pd, monkeypatch,
                   _mapped_argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID),
                   fake, conf=_fallback_conf())
    err = capsys.readouterr().err

    assert rc == 0 and len(fake.calls) == 2           # 재사용 1 + 폴백 1 (재실행 0)
    assert [call["harness"] for call in fake.calls] == ["claude", "codex"]
    assert "세션 재사용 실패" not in err               # 재사용 축 오진단 없음
    assert "폴백: claude→codex" in err
    assert _PREAMBLE_MARKER in fake.calls[1]["stdin_text"]   # 폴백은 full payload


def test_declared_execution_budget_matches_worst_case_spawn_count(
        pd, monkeypatch, tmp_path, capsys):
    """호출층 상한 advisory 요구치가 실제 최악 스폰 수(재사용+재실행+폴백)와 일치한다."""
    out_dir, _ledger_path, _record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path)
    seen: list[int] = []
    monkeypatch.setattr(
        pd, "harness_cap_advisory",
        lambda *, execution_budget=None: seen.append(execution_budget))
    primary_budget = pd._harness_timeout_budget("claude", 600)
    fallback_budget = pd._harness_timeout_budget("codex", 600)

    _run_main(pd, monkeypatch, _mapped_argv(prompt, tmp_path, out_dir, "--dry-run"),
              _FakeRun(_ok(_claude_wire())), conf=_fallback_conf())
    _run_main(pd, monkeypatch,
              _mapped_argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID,
                           "--dry-run"),
              _FakeRun(_ok(_claude_wire())), conf=_fallback_conf())

    # 재사용 미요청 = primary + 폴백 2회분 · 재사용 라운드 = primary 2회 + 폴백 1회분.
    assert seen == [
        primary_budget + fallback_budget,
        primary_budget * 2 + fallback_budget,
    ]
    assert f"fresh 재실행 1회 = primary 예산 {primary_budget}s 추가" in capsys.readouterr().out


def test_corrupt_session_id_never_reaches_argv(pd, monkeypatch, tmp_path, capsys):
    """손상 장부 값(`--` 시작)은 재개 시도 자체를 막는다 — argv 오소비 0."""
    corrupted = "--dangerously-skip-permissions"
    out_dir, _ledger_path, _record_id = _resume_fixture(
        pd, tmp_path, session_id=corrupted)
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)
    err = capsys.readouterr().err

    assert rc == 0 and len(fake.calls) == 1
    assert corrupted not in fake.calls[0]["argv"]
    assert "--resume" not in fake.calls[0]["argv"]
    assert _PREAMBLE_MARKER in fake.calls[0]["stdin_text"]     # full payload
    assert "세션 재사용 미적용" in err and "세션 id 형식 불일치" in err


def test_dry_run_previews_the_resume_round_without_sending(pd, monkeypatch, tmp_path, capsys):
    """미리보기는 재개 argv 와 실제 보낼 delta 를 보여주고 아무것도 보내지 않는다."""
    out_dir, _ledger_path, record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path, "해소 주장 본문")
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))

    rc = _run_main(
        pd, monkeypatch,
        _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID, "--dry-run"), fake)
    out = capsys.readouterr().out

    assert rc == 0 and fake.calls == []
    assert f"세션 재사용: session_id={SESSION_ID}" in out
    assert f"장부 레코드={record_id}" in out
    assert f"--resume {SESSION_ID}" in out               # argv 미리보기
    assert "직전 지적 A" in out and "해소 주장 본문" in out  # 보낼 본문 = delta
    assert _PREAMBLE_MARKER not in out


def test_missing_candidate_falls_back_to_fresh_with_notice(pd, monkeypatch, tmp_path, capsys):
    """후보가 없으면 안내 1줄 + fresh — 차단하지 않는다(새 거부 rc 없음).

    장부는 **있고** 그 안에 대응 레코드만 없는 형상이다(장부 부재는 별도 사유·아래 테스트).
    """
    out_dir = tmp_path / "raw"
    out_dir.mkdir()
    _seed_record(pd._load_relay(), out_dir / "raw_outputs.json",
                 start_extra={"ticket": "T-" + "0001"})   # 다른 티켓 = 후보 아님
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)
    err = capsys.readouterr().err

    assert rc == 0 and len(fake.calls) == 1
    assert "--resume" not in fake.calls[0]["argv"]
    assert "세션 재사용 미적용" in err and "성공 마감 위임 레코드" in err


# ══ ⑨ 장부 부재 형상 — 조회가 아무것도 만들지 않는다 (T-0600) ═══════════════════

def test_absent_ledger_is_reported_before_any_lock_is_taken(pd, tmp_path, capsys):
    """장부가 없으면 락 획득 전에 끝난다 — 상위 디렉터리·`.lock` 파일을 만들지 않는다.

    락 획득(`file_lock.exclusive_file_lock`)은 부모 디렉터리와 락 파일을 **만든다**. 조회
    한 번이 트리에 `.project_manager/.local/` 을 새로 심으면 읽기 전용 경로가 아니다.
    """
    out_dir = tmp_path / "never-created"
    plan = pd.resolve_resume_plan(
        TICKET_ID, harness="claude", role="code-reviewer",
        task_text="본문", output_dir=out_dir,
    )
    err = capsys.readouterr().err

    assert plan is None
    assert "세션 재사용 미적용" in err and "raw 장부 없음" in err
    assert not out_dir.exists()                      # 디렉터리·lock 신설 0


def test_resume_dry_run_leaves_no_trace_on_a_fresh_tree(pd, monkeypatch, tmp_path, capsys):
    """`--resume-from` 미리보기는 부작용 0 — 장부 위치에 아무 파일도 생기지 않는다."""
    out_dir = tmp_path / "raw"                        # 미생성 상태로 넘긴다
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))

    rc = _run_main(
        pd, monkeypatch,
        _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID, "--dry-run"), fake)
    out = capsys.readouterr().out

    assert rc == 0 and fake.calls == []
    assert "세션 재사용: 미적용" in out
    assert not out_dir.exists()


# ══ ⑥ 하네스 축 (codex exec resume 실측·opencode 무배선) ═════════════════════

def test_unsupported_harness_stays_fresh_and_loud(pd, monkeypatch, tmp_path, capsys):
    """재개 미지원 축은 요청이 와도 fresh — 미검증 argv 를 만들지 않는다."""
    harness = "opencode"
    out_dir, _ledger_path, _record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path)
    wire = {
        "codex": "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "판정: 통과"}}),
        ]),
        "opencode": json.dumps({
            "type": "text", "sessionID": "ses_1",
            "part": {"type": "text", "text": "판정: 통과"}}),
    }[harness]
    fake = _FakeRun(_ok(wire))

    rc = _run_main(
        pd, monkeypatch,
        _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID, harness=harness),
        fake,
    )
    err = capsys.readouterr().err

    assert rc == 0 and len(fake.calls) == 1
    assert not [token for token in fake.calls[0]["argv"] if "resume" in token]
    assert SESSION_ID not in " ".join(fake.calls[0]["argv"])
    assert "세션 재사용 미적용" in err and "미검증" in err


def test_execute_attempt_refuses_resume_id_on_unsupported_harness(pd, tmp_path):
    """호출층이 잘못 넘겨도 미지원 축엔 재개 id 가 실리지 않는다(방어선 2중)."""
    with pytest.raises(pd.DelegateError, match="세션 재사용 미지원"):
        pd._execute_attempt(
            harness="opencode", model="prov/m", reasoning=None, role="code-reviewer",
            cwd=tmp_path, prompt="p", timeout=60, output_dir=tmp_path / "raw",
            run_fn=_FakeRun(_ok("")), attempt="primary",
            resume_session_id=SESSION_ID,
        )


# ══ delta 조립 순수성 ════════════════════════════════════════════════════════

def test_delta_payload_consumes_only_structured_fields(pd):
    """조립 입력은 장부 필드 + 호출자 원문뿐 — 해소 주장은 파싱하지 않고 그대로 싣는다."""
    claim = "**must-fix:** 이 문장은 엔진이 해석하면 안 된다"
    payload = pd.build_resume_delta_payload(
        must_fix_items=["항목 1", "  ", "항목 2"], base_rev="rev-1", task_text=claim)
    assert "1. 항목 1" in payload and "2. 항목 2" in payload   # 빈 항목은 버린다
    assert payload.endswith(claim)                            # 원문 그대로(불투명)
    assert "rev-1" in payload
    missing = pd.build_resume_delta_payload(
        must_fix_items=(), base_rev=None, task_text="본문")
    assert "장부에 기록 없음" in missing


def test_delta_round_is_gated_like_a_fresh_send(pd, monkeypatch, tmp_path, capsys):
    """delta 도 실제 송신 본문이므로 전송-전 시크릿 게이트를 통과해야 한다(무검사 우회 0)."""
    out_dir, _ledger_path, _record_id = _resume_fixture(
        pd, tmp_path, must_fix=("~/.ssh/id_rsa 를 프롬프트에서 빼라",))
    prompt = _write_prompt(tmp_path, "해소했다")
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)
    err = capsys.readouterr().err

    assert rc == 1 and fake.calls == []             # 전송 전 차단
    assert "시크릿 denylist 판정" in err


# ══ 실 위임 배선 (ticket·base_rev 가 장부까지 흐르는가) ═══════════════════════

def _git_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True, capture_output=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=workspace, check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=workspace, check=True, capture_output=True,
    )
    return workspace


def test_base_rev_comes_from_the_prerun_worktree_capture(pd, monkeypatch, tmp_path):
    """기준 rev 는 이미 캡처한 실행-전 worktree 상태에서 온다(추가 git 호출 0)."""
    workspace = _git_workspace(tmp_path)
    out_dir = tmp_path / "raw"
    out_dir.mkdir()
    prompt = _write_prompt(workspace)
    fake = _FakeRun(_ok(_claude_wire("판정: 통과")))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace,
                          check=True, capture_output=True, text=True).stdout.strip()

    rc = _run_main(pd, monkeypatch, _argv(prompt, workspace, out_dir), fake)

    assert rc == 0
    row = _rows(out_dir / "raw_outputs.json")[0]
    assert row["base_rev"] == head


# ── 재실행 자격 = 확정된 재사용 실패뿐 (T-0605 ⑤) ─────────────────────────────
# codex R5 지적: 세션 id 가 없고 인프라 실패로 분류되지 않은 **모든** 실패를 재사용 불일치로 보고
# read 역할을 자동 재실행했다 — 전송 *후* 죽은 미분류 `rc≠0` 도 full payload 로 다시 나가
# 중복 과금·중복 외부 전송이 된다. 자격은 둘뿐이다: 깨끗한 완료의 명시적 id 불일치, 확정된
# "세션 없음" 오류.


def test_unclassified_failure_after_send_is_not_rerun(pd, monkeypatch, tmp_path, capsys):
    """미분류 `rc≠0`(전송 후 실패 가능)은 재실행하지 않는다 — 기존 fail-loud (DoD)."""
    out_dir, _ledger_path, _record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(
        {"returncode": 1, "stdout": "", "stderr": "unexpected tool crash",
         "timed_out": False},
        _ok(_claude_wire("판정: 통과")),          # 재실행이 일어나면 이걸 소비한다
    )

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)
    err = capsys.readouterr().err

    assert rc == 1
    assert len(fake.calls) == 1, f"미분류 실패인데 재전송이 일어남: {fake.calls}"
    assert "세션 재사용 실패" not in err          # 재사용 축 오진단 없음
    assert "위임 하네스 실패(rc=1)" in err        # 기존 fail-loud 그대로


def test_a_clean_completion_without_an_observed_id_is_not_rerun(
        pd, monkeypatch, tmp_path, capsys):
    """rc=0 인데 세션 id 를 **관측하지 못한** 라운드도 재실행 대상이 아니다(불일치 미확정).

    관측 실패는 재사용이 깨졌다는 증거가 아니다 — 그 상태로 full payload 를 다시 태우면 이미
    소비된 라운드가 한 번 더 과금된다."""
    out_dir, _ledger_path, _record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(
        _ok(json.dumps({"type": "result", "result": "판정: 통과"})),   # session_id 없음
        _ok(_claude_wire("판정: 통과")),
    )

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)

    assert rc == 0
    assert len(fake.calls) == 1, f"관측 실패인데 재전송이 일어남: {fake.calls}"
    assert "세션 재사용 실패" not in capsys.readouterr().err


@pytest.mark.parametrize("wire", [
    "No conversation found",           # claude 실측 문구
    "Session not found: 11111111",     # opencode 계열
    "conversation not found",          # codex 계열
])
def test_a_confirmed_missing_session_still_reruns(pd, monkeypatch, tmp_path, capsys, wire):
    """정상 경로 무변경 — "세션 없음"이 확정된 실패는 delta 가 소비되지 않았으므로 재실행한다."""
    out_dir, _ledger_path, _record_id = _resume_fixture(pd, tmp_path)
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun(
        {"returncode": 1, "stdout": "", "stderr": wire, "timed_out": False},
        _ok(_claude_wire("판정: 통과")),
    )

    rc = _run_main(pd, monkeypatch,
                   _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID), fake)

    assert rc == 0 and len(fake.calls) == 2
    assert "재개 대상 세션 없음" in capsys.readouterr().err
    assert _PREAMBLE_MARKER in fake.calls[1]["stdin_text"]


def test_write_role_unclassified_failure_keeps_the_generic_fail_loud(
        pd, monkeypatch, tmp_path, capsys):
    """write 역할의 미분류 실패도 재사용 축 안내가 아니라 기존 fail-loud 다 (오진단 0)."""
    out_dir, _ledger_path = _write_role_fixture(pd, tmp_path, "developer")
    prompt = _write_prompt(tmp_path)
    fake = _FakeRun({"returncode": 1, "stdout": "", "stderr": "unexpected tool crash",
                     "timed_out": False})

    rc = _run_main(
        pd, monkeypatch,
        _argv(prompt, tmp_path, out_dir, "--resume-from", TICKET_ID, role="developer"),
        fake)
    err = capsys.readouterr().err

    assert rc == 1 and len(fake.calls) == 1
    assert "트리를 이미 고쳤을 수 있어" not in err
    assert "위임 하네스 실패(rc=1)" in err


@pytest.mark.parametrize("result, observed, expected", [
    ({"returncode": 0}, OTHER_SESSION_ID, "회신 세션 id 불일치"),
    ({"returncode": 0}, SESSION_ID, None),          # 일치 = 성공
    ({"returncode": 0}, None, None),                # 관측 실패 = 미확정
    ({"returncode": 1, "stderr": "boom"}, None, None),            # 미분류 실패
    ({"returncode": 1, "stderr": "No conversation found"}, None, "재개 대상 세션 없음"),
])
def test_rerun_reason_is_the_single_eligibility_funnel(pd, result, observed, expected):
    """자격 판정은 한 함수가 소유한다 — 호출부가 각자 조건을 조립하면 다시 갈린다."""
    assert pd.resume_rerun_reason(dict(result), observed, SESSION_ID) == expected
