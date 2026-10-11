"""board complete-gate 테스트 (T-0104 · ADR-0023 · T-0596).

두 축을 담는다.

**축 A — 옛 status-mention 경고 제거 박제 (T-0104 · ADR-0023)**: status.md 는
judgment-only(모듈 *판정*)로 재정의돼 ticket ID 를 담지 않으므로(ADR-0023), "does not
mention {tid} — affected module row" 경고는 거의 모든 complete 에서 무의미하게 발화하던
노이즈였다. 이 축은 두 계약을 동시에 단언한다:

  (a) status.md 가 ticket id 를 언급하지 *않아도* 그 경고 문구가 stderr 에 안 나옴
      (제거 회귀 박제 — 경고를 되살리면 fail).
  (b) §1 log/current.md mention gate 는 **여전히 동작**한다 — log 에 ticket id 가 없으면
      blocking problem 을 돌려준다(§1·§2 불변 회귀 보호).

**축 B — DoD 기록 게이트 (T-0596)**: 지금의 §3 은 `## 완료 조건` 체크박스 검사다(옛 §3
status-mention 경고와 무관 — 그건 제거됐고 번호만 비었다). 미체크 `- [ ]` 가 남은 채 done 이
되면 그 항목은 보드에서 증발하므로(실사고: 라이브 probe DoD 소멸), 통과 형식을 둘로 못박는다:
`- [x] <원문>`(했다) 또는 `- [>] <원문> (이월: <사유·귀속>)`(안 했고 사유·귀속을 남겼다).
소급 검사는 없다 — 이미 done 인 티켓은 보지 않는다.

**hermetic**: board.py 모듈 전역(STATUS_FILE·LOG_FILE·REPO 등)은 import 시점에 실 repo
절대경로로 굳는다 — tmp 프로젝트로 재지정해 실 루트를 절대 건드리지 않는다
(test_board_concurrency.py 의 `_load_board_bound` 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 제거된 옛 status-mention 경고의 시그니처 문구 — stderr 에 나오면 경고가 살아있다는 뜻.
WARNING_FRAGMENTS = ("does not mention", "affected module row")


def _load_board_bound(proj: Path):
    """board.py 를 새로 로드하고 status/log 경로 전역을 `proj` tmp 프로젝트로 재바인딩한다.

    import 시점에 굳은 실 REPO 경로를 tmp 로 덮어써 complete-gate 가 tmp status.md/
    log/current.md 만 보도록 한다(실 루트 불간섭).
    """
    spec = importlib.util.spec_from_file_location("board_cgate", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    wiki = proj / ".project_manager" / "wiki"
    # tickets_dir()는 import-time TICKETS_DIR가 아니라 REPO→board_root()를 따른다. REPO까지
    # tmp로 묶지 않으면 `board` fixture의 티켓 쓰기가 라이브 worktree scaffold에 착지한다.
    # BOARD_FILE은 import-time 상수라 같은 자리에서 별도로 묶어 refresh 파생 출력도 격리한다.
    mod.REPO = proj
    mod.BOARD_FILE = wiki / "board.md"
    mod.STATUS_FILE = wiki / "status.md"
    mod.LOG_FILE = wiki / "log" / "current.md"
    mod.LOCAL_DIR = proj / ".project_manager" / ".local"
    return mod


class _Args:
    """argparse.Namespace 대용 — _complete_gate 인자 컨테이너."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _gate_args(**overrides):
    """기본 통과 조합 — log gate·regression gate 모두 만족. 케이스별로 덮어쓴다."""
    defaults = dict(allow_missing_log=True, tests_pass=True, allow_untested=False)
    defaults.update(overrides)
    return _Args(**defaults)


@pytest.fixture
def proj(tmp_path):
    """tmp 프로젝트 골격 — wiki/ + wiki/log/."""
    p = tmp_path / "proj"
    (p / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    return p


@pytest.fixture
def board(proj):
    return _load_board_bound(proj)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_legacy_pm_fixed_resolution_is_unrecognized(board):
    """옛 처분 종류(`pm-fixed`)는 처분으로 읽히지 않는다(공용 처분 판독 seam)."""
    entry = {
        "rounds": [{"sequence": 1, "verdict": 1, "must_fix": 1}],
        "resolution": {"kind": "pm-fixed", "round_sequence": 1, "rounds": 1},
    }
    assert board.gate_resolution(entry) is None


# ── T-0786(D1) — `pm-verified` 완료 게이트(기계 확인 증거) ────────────────

def _write_ticket_with_rounds(board, tid: str, spec_body: str, rounds: list[tuple[int, str, str]]):
    """티켓 명세(status=claimed) + `tickets/rounds/<id>/NN-<role>.md` 를 tmp board 에 깐다."""
    tickets_dir = board.tickets_dir()
    spec_path = tickets_dir / "claimed" / f"{tid}-gate.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        (
            "---\n"
            f"id: {tid}\n"
            "title: pm-verified 게이트 픽스처\n"
            "status: claimed\n"
            "created: '2026-08-21'\n"
            "created_by: test\n"
            "---\n"
            f"# {tid}\n\n## 목표\n게이트 픽스처.\n\n"
            "## 완료 조건 (Definition of Done)\n- [x] 픽스처\n\n"
            f"{spec_body}"
        ),
        encoding="utf-8", newline="",
    )
    rounds_module = board._load_ticket_rounds()
    rounds_dir = rounds_module.rounds_dir_for_ticket(tid, tickets_dir)
    rounds_dir.mkdir(parents=True, exist_ok=True)
    for ordinal, role, text in rounds:
        (rounds_dir / rounds_module.round_filename(ordinal, role)).write_text(
            text, encoding="utf-8", newline="",
        )
    return spec_path


def _reviewer_round_text(pd, finding_id: str) -> str:
    payload = {
        "version": pd.PM_REVIEW_VERSION,
        "findings": [{
            "id": finding_id, "class": "implementation-defect", "severity": "must-fix",
            "authority": "[[T-0786]]", "evidence": "probe", "recommendation": "fix",
            "fix_contract": {
                "location": "src/example.py:1", "failure": "probe",
                "design": "fix", "test": "regression",
                "command": "python3 --version", "expected": "Python",
            },
            "design_change": False,
        }],
        "confirmations": [],
    }
    return (
        "## 리뷰 (code-reviewer · 2026-08-21)\n\n## must-fix\n- "
        f"{finding_id}\n\n## 판정\n판정: 반려\n\n```{pd.PM_REVIEW_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
    )


def _developer_round_text(pd, finding_id: str, *, resolved_command: str = "echo hi") -> str:
    payload = {
        "version": pd.PM_REVIEW_VERIFY_VERSION,
        "verifications": [{
            "id": finding_id, "machine_verifiable": True, "command": resolved_command,
            "expected": "hi", "before": "bye", "reason": "",
        }],
    }
    return (
        "## 구현 보충 (developer · 2026-08-21)\n\n## 변경 파일\n- x\n\n"
        f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
    )


def _pm_body(pd, finding_id: str, *, machine_confirmed: bool) -> str:
    disposition = {
        "version": pd.PM_REVIEW_DISPOSITION_VERSION, "reviewer_ordinal": 1,
        "dispositions": [{
            "id": finding_id, "decision": "accepted", "reason": "PM 수락",
            "scope": f"{finding_id} 범위", "prerequisite": "",
        }],
    }
    body = (
        f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n"
        + json.dumps(disposition, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
    )
    if machine_confirmed:
        confirmation = {
            "version": pd.PM_REVIEW_MACHINE_CONFIRMATION_VERSION, "round": 2,
            "confirmations": [{
                "id": finding_id, "status": "resolved", "command": "echo hi", "observed": "hi",
            }],
        }
        body += (
            f"```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n"
            + json.dumps(confirmation, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
        )
    return body


def _pm_delegate_module():
    spec = importlib.util.spec_from_file_location(
        "pm_delegate_gate", TOOLS / "pm_delegate.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _passed_review_round_text(pd) -> str:
    payload = {"version": pd.PM_REVIEW_VERSION, "findings": [], "confirmations": []}
    return (
        "## 리뷰 (code-reviewer · 2026-08-21)\n\n## must-fix\n- 없음\n\n"
        "## 판정\n판정: 통과\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
    )


def _gate_spec_and_body(spec_path: Path) -> tuple[str, str]:
    """`cmd_complete` 가 넘기는 두 입력 — 명세 전문과 본문."""
    spec_text = spec_path.read_text(encoding="utf-8")
    return spec_text, spec_text.split("---\n", 2)[2]


def _review_gate_problems(board, tid: str, spec_path: Path) -> list[str]:
    spec_text, body = _gate_spec_and_body(spec_path)
    return board._complete_gate(tid, _gate_args(), body, spec_text=spec_text)


def test_pm_verified_completion_gate_opens_only_with_machine_confirmation_evidence(board):
    """(D1) 리뷰 라운드 잔여는 accepted==() + 기계 확인 ≥1 일 때만 완료를 연다(장부 없음)."""
    pd = _pm_delegate_module()
    tid = "T-PVF-201"
    spec_body = _pm_body(pd, "F-001", machine_confirmed=True)
    spec_path = _write_ticket_with_rounds(board, tid, spec_body, [
        (1, "code-reviewer", _reviewer_round_text(pd, "F-001")),
        (2, "developer", _developer_round_text(pd, "F-001")),
    ])

    assert _review_gate_problems(board, tid, spec_path) == []
    assert not (board.LOCAL_DIR / "internal_review_rounds.json").exists()


def test_pm_verified_completion_gate_rejects_when_accepted_finding_still_pending(board):
    """기계 확인이 없어 delta 의 accepted 가 비지 않으면 리뷰 잔여가 완료를 막는다."""
    pd = _pm_delegate_module()
    tid = "T-PVF-202"
    spec_body = _pm_body(pd, "F-001", machine_confirmed=False)   # 기계 확인 없음
    spec_path = _write_ticket_with_rounds(board, tid, spec_body, [
        (1, "code-reviewer", _reviewer_round_text(pd, "F-001")),
        (2, "developer", _developer_round_text(pd, "F-001")),
    ])

    problems = _review_gate_problems(board, tid, spec_path)

    assert len(problems) == 1
    assert "code-reviewer 리뷰 라운드 01" in problems[0]
    assert "accepted 잔여" in problems[0] and "F-001" in problems[0]


def test_review_round_must_fix_blocks_completion_without_internal_ledger(board):
    """AT-003 — 내부 기록 파일 없이 리뷰 라운드 파일의 미처분 must-fix 가 완료를 막는다.

    판정 입력 이전 전에는 이 형상에서 DoD·라운드 사유만 나오고 리뷰 잔여는 보이지 않았다.
    역방향: 통과 리뷰 라운드(must-fix 0)는 리뷰 사유를 만들지 않는다.
    """
    pd = _pm_delegate_module()
    blocked = "T-RVW-301"
    blocked_spec = _write_ticket_with_rounds(board, blocked, "", [
        (1, "code-reviewer", _reviewer_round_text(pd, "F-001")),
    ])
    assert not (board.LOCAL_DIR / "internal_review_rounds.json").exists()

    problems = _review_gate_problems(board, blocked, blocked_spec)

    review = [item for item in problems if "code-reviewer 리뷰 라운드" in item]
    assert len(review) == 1, problems
    assert "must-fix 1건" in review[0] and "01" in review[0]

    passed = "T-RVW-302"
    passed_spec = _write_ticket_with_rounds(board, passed, "", [
        (1, "code-reviewer", _passed_review_round_text(pd)),
    ])
    assert not [
        item for item in _review_gate_problems(board, passed, passed_spec)
        if "code-reviewer" in item
    ]
    assert not (board.LOCAL_DIR / "internal_review_rounds.json").exists()


def test_review_round_unknown_verdict_blocks_completion(board):
    """판정 불능 리뷰 라운드(두 축 모두 판정 없음)는 통과가 아니다 — 미상으로 막는다."""
    tid = "T-RVW-303"
    spec_path = _write_ticket_with_rounds(board, tid, "", [
        (1, "code-reviewer", "## 리뷰 (code-reviewer · 2026-08-21)\n\n판정 없이 끝난 산출.\n"),
    ])

    problems = _review_gate_problems(board, tid, spec_path)

    assert len(problems) == 1
    assert "미상(판정 무효)" in problems[0]
    assert "판정 추출 진단" in problems[0]


def test_review_residual_reads_the_latest_review_round(board):
    """최대 순번 리뷰 라운드가 잔여 입력이다 — 앞 라운드의 반려는 뒤 통과에 덮인다."""
    pd = _pm_delegate_module()
    tid = "T-RVW-304"
    spec_path = _write_ticket_with_rounds(board, tid, "", [
        (1, "code-reviewer", _reviewer_round_text(pd, "F-001")),
        (2, "developer", _developer_round_text(pd, "F-001")),
        (3, "code-reviewer", _passed_review_round_text(pd)),
    ])

    assert not [
        item for item in _review_gate_problems(board, tid, spec_path)
        if "code-reviewer 리뷰 라운드" in item
    ]


def test_review_absence_is_non_target_for_existing_tickets(board):
    """리뷰 라운드가 없는 티켓은 이 축의 대상이 아니다(소급 차단 없음)."""
    tid = "T-RVW-305"
    spec_path = _write_ticket_with_rounds(board, tid, "", [])

    assert _review_gate_problems(board, tid, spec_path) == []


def test_internal_completion_helpers_are_removed(board):
    """내부 리뷰 기록 완료 판정과 그 판정만 쓰던 통과 조회는 board 에 남지 않는다."""
    for removed in ("gate_passed", "_internal_review_completion_problem",
                    "_internal_review_rounds_ledger"):
        assert not hasattr(board, removed), removed


# ════════════════════════════════════════════════════════════════════════
# (a) §3 status-mention 경고 제거 — status.md 에 ticket 없어도 경고 무발화
# ════════════════════════════════════════════════════════════════════════

def test_status_mention_warning_gone_when_status_lacks_ticket(board, capsys):
    """status.md 가 ticket id 를 언급하지 않아도 §3 경고가 stderr 에 안 나온다.

    judgment-only status.md(ADR-0023)는 ticket id 를 담지 않는다 — 옛 §3 경고를 되살리면
    이 단언이 fail 한다(제거 회귀 박제).
    """
    # judgment-only status.md — ticket id 를 전혀 담지 않는 모듈 판정 표.
    _write(board.STATUS_FILE, "# Status\n\n| 모듈 | 판정 |\n|---|---|\n| core | OK |\n")

    tid = "T-0104"
    problems = board._complete_gate(tid, _gate_args())

    err = capsys.readouterr().err
    for fragment in WARNING_FRAGMENTS:
        assert fragment not in err, (
            f"제거됐어야 할 §3 status-mention 경고 문구가 stderr 에 발화함: {fragment!r}\n{err!r}")
    # §3 만 제거 — 기본 통과 인자에선 blocking problem 도 없어야 한다(§1·§2 만족).
    assert problems == [], f"기대 외 blocking problem: {problems}"


def test_status_mention_warning_gone_even_without_status_file(board, capsys):
    """status.md 가 *아예 없어도* §3 경고가 안 나온다(파일 부재 경로도 제거 확인)."""
    assert not board.STATUS_FILE.exists()

    problems = board._complete_gate("T-0104", _gate_args())

    err = capsys.readouterr().err
    for fragment in WARNING_FRAGMENTS:
        assert fragment not in err, f"§3 경고가 status.md 부재 시에도 발화함: {fragment!r}\n{err!r}"
    assert problems == []


# ════════════════════════════════════════════════════════════════════════
# (b) §1 log/current.md mention gate — 여전히 동작(회귀 보호·§1 불변)
# ════════════════════════════════════════════════════════════════════════

def test_log_mention_gate_blocks_when_log_lacks_ticket(board):
    """§1: log/current.md 가 ticket id 를 안 담으면 blocking problem 을 돌려준다(여전히 동작).

    §3 제거가 §1 을 건드리지 않았음을 박제한다.
    """
    _write(board.LOG_FILE, "# Log\n\n다른 작업만 기록됨 — 이 ticket 미언급.\n")

    problems = board._complete_gate(
        "T-0104", _gate_args(allow_missing_log=False))

    assert any("T-0104" in p and "log/current.md" in p for p in problems), (
        f"§1 log-mention gate 가 동작 안 함 — blocking problem 없음: {problems}")


def test_log_mention_gate_passes_when_log_mentions_ticket(board, capsys):
    """§1: log/current.md 가 ticket id 를 담으면 통과(blocking 없음). 옛 경고도 여전히 무발화."""
    _write(board.LOG_FILE, "# Log\n\n- T-0104 완료: 게이트 정합.\n")
    # status.md 는 ticket id 미언급(judgment-only) — 옛 경고가 살아있으면 여기서 발화했을 것.
    _write(board.STATUS_FILE, "# Status\n\n| 모듈 | 판정 |\n|---|---|\n| core | OK |\n")

    problems = board._complete_gate(
        "T-0104", _gate_args(allow_missing_log=False))

    assert problems == [], f"log 가 ticket 을 담는데도 blocking: {problems}"
    err = capsys.readouterr().err
    for fragment in WARNING_FRAGMENTS:
        assert fragment not in err, f"옛 status-mention 경고 재발: {fragment!r}\n{err!r}"


# ════════════════════════════════════════════════════════════════════════
# 축 B — §3 DoD 기록 게이트 (T-0596)
# ════════════════════════════════════════════════════════════════════════

def _dod_body(*items: str, section: str = "## 완료 조건 (Definition of Done)") -> str:
    """DoD 절에 주어진 줄들을 담은 최소 티켓 본문."""
    lines = "\n".join(items)
    return (
        "# T-0596 — 게이트 픽스처\n\n"
        "## 목표\n게이트 판정 입력.\n\n"
        f"{section}\n{lines}\n\n"
        "## 참고\n- 없음\n\n## 메모\n"
    )


def test_dod_unchecked_item_blocks(board):
    """미체크 `- [ ]` 잔존 → blocking problem (false-complete 폐쇄)."""
    body = _dod_body("- [x] 코드", "- [ ] 라이브 probe 실측")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("DoD 미체크" in p and "라이브 probe 실측" in p for p in problems), (
        f"미체크 DoD 가 차단되지 않음: {problems}")


def test_dod_all_checked_passes(board):
    """전 항목 `- [x]` → 통과(blocking 0)."""
    body = _dod_body("- [x] 코드", "- [X] 테스트", "  - [x] 들여쓴 하위 항목")

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_dod_deferred_with_reason_passes(board):
    """`- [>] <원문> (이월: <사유·귀속>)` → 통과 (명시 이월만 미체크를 대체한다)."""
    body = _dod_body(
        "- [x] 코드",
        "- [>] 라이브 probe 실측 (이월: 하네스 한도 소진·T-0600 귀속)",
    )

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_dod_deferred_without_reason_blocks(board):
    """사유 없는 `- [>]` → 차단 (이월의 근거가 남지 않으면 이월이 아니다)."""
    body = _dod_body("- [x] 코드", "- [>] 라이브 probe 실측")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("이월 사유 누락" in p for p in problems), (
        f"사유 없는 이월이 통과함: {problems}")


def test_dod_deferred_with_empty_reason_blocks(board):
    """`(이월: )` 처럼 사유가 빈 표기도 차단 — 문법만 흉내낸 통과를 막는다."""
    body = _dod_body("- [>] 라이브 probe 실측 (이월: )")

    assert board._complete_gate("T-0596", _gate_args(), body), "빈 사유 이월이 통과함"


def test_dod_unknown_mark_blocks(board):
    """`- [~]` 같은 알 수 없는 마커도 차단 — 통과 형식은 `[x]`·`[>]` 둘뿐이다(오타로 게이트가 꺼지지 않게)."""
    body = _dod_body("- [~] 애매한 상태")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("DoD 미체크" in p for p in problems), f"미지 마커가 통과함: {problems}"


@pytest.mark.parametrize("line", ["- [] 빈 브라켓 항목", "* []빈 브라켓·공백 없음"])
def test_dod_empty_bracket_blocks(board, line):
    """`- []`(빈 브라켓)도 차단 — 마커를 1글자로만 보면 체크박스로 인식조차 못 해 통과했다.

    사람 눈에는 미체크인 줄이라, 조용한 통과는 done 이행에서 그 항목을 증발시킨다(fail-open).
    """
    problems = board._complete_gate("T-0596", _gate_args(), _dod_body("- [x] 코드", line))

    assert any("DoD 미체크" in p and "빈 브라켓" in p for p in problems), (
        f"빈 브라켓 항목이 통과함: {problems}")


# ── 다문자 마커·접두 헤딩 (T-0602 ④) ─────────────────────────────────────────
# codex R2 지적: ① `- [xx] 항목` 은 마커를 0~1글자로만 보던 정규식에 **체크박스로 인식조차 안 돼**
# 판정 대상 밖이었고 ② `## 완료 조건 검토 이력` 같은 다른 절이 접두 매칭으로 DoD 를 가렸다.
# 두 경우 모두 미체크 항목이 남아도 완료가 통과한다 — 아래는 그 형상 그대로 재현하고 차단을 본다.


@pytest.mark.parametrize("line, mark", [
    ("- [xx] 다문자 마커 항목", "xx"),
    ("- [ x] 앞 공백만 다른 게 아니라 두 글자", "x"),      # strip 후 `x` = 체크 (통과)
    ("* [WIP] 진행 중 표기", "WIP"),
    ("+ [x?] 물음표 붙은 마커", "x?"),
])
def test_dod_multichar_mark_is_an_unknown_marker(board, line, mark):
    """다문자 마커는 미지 마커로 **차단**된다 — 단 공백만 걷어 `x` 가 되는 표기는 체크로 본다."""
    problems = board._complete_gate("T-0602", _gate_args(), _dod_body("- [x] 코드", line))

    if mark == "x":
        assert problems == [], f"사람이 체크한 `[ x]` 표기가 차단됨: {problems}"
        return
    assert any("DoD 미체크" in p and mark in p for p in problems), (
        f"다문자 마커 {mark!r} 가 판정에서 빠짐(비인식=통과): {problems}")


def test_dod_markdown_link_bullet_is_not_a_checkbox(board):
    """`- [라벨](경로)`·`- [라벨][ref]`·`- [[wikilink]]` 는 링크지 체크박스가 아니다 (오탐 0)."""
    body = _dod_body(
        "- [x] 코드",
        "- [산출물 보고서](.project_manager/wiki/raw/report.md) 첨부",
        "- [참고][ref-1] 표기",
        "- [[architecture]] 절 갱신",
    )

    assert board._complete_gate("T-0602", _gate_args(), body) == []


def test_dod_prefix_heading_cannot_hide_the_real_section(board):
    """`## 완료 조건 검토 이력` 이 앞서 있어도 **실제 DoD** 의 미체크를 잡는다 (접두 매칭 폐쇄)."""
    body = (
        "# T-0602 — 픽스처\n\n## 목표\n게이트 판정 입력.\n\n"
        "## 완료 조건 검토 이력\n- 2026-08-09 PM 이 구두로 확인했다.\n\n"
        "## 완료 조건 (Definition of Done)\n- [ ] 진짜 미체크 항목\n\n"
        "## 참고\n- 없음\n"
    )

    problems = board._complete_gate("T-0602", _gate_args(), body)

    assert any("진짜 미체크 항목" in p for p in problems), (
        f"접두 헤딩이 실제 DoD 를 가림: {problems}")


def test_dod_heading_with_a_parenthesized_subtitle_is_the_section(board):
    """괄호 부제(`(Definition of Done)`)는 그 절이다 — 정상 티켓 무영향(정확 일치 규칙)."""
    for heading in ("## 완료 조건", "## 완료 조건 (Definition of Done)", "## 완료 조건 (DoD)"):
        body = _dod_body("- [ ] 미체크 항목", section=heading)
        problems = board._complete_gate("T-0602", _gate_args(), body)
        assert any("미체크 항목" in p for p in problems), f"{heading!r} 절을 못 봤다: {problems}"


def test_dod_unidentifiable_heading_blocks_instead_of_passing(board):
    """괄호 밖 부제가 붙은 헤딩은 조용히 통과시키지 않고 형식 차단으로 표면화한다.

    정확 일치만 요구하면 `## 완료 조건 (DoD) — 부제` 가 어느 절로도 안 잡혀 미체크 항목이 통째로
    안 보인다(닫으려던 그 형상이 헤딩만 바꿔 되살아난다)."""
    body = _dod_body("- [ ] 미체크 항목",
                     section="## 완료 조건 (Definition of Done) — falsifiable verdict")

    problems = board._complete_gate("T-0602", _gate_args(), body)

    assert any("헤딩 형식" in p for p in problems), f"식별 불가 헤딩이 통과함: {problems}"


def test_dod_gate_ignores_checkboxes_outside_dod_section(board):
    """DoD 절 **밖**(메모·참고)의 미체크 체크박스는 판정 대상이 아니다(오탐 0)."""
    body = _dod_body("- [x] 코드") + "\n## 메모\n- [ ] 다음 세션 후보 아이디어\n"

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_dod_gate_ignores_fenced_code_examples(board):
    """DoD 절 안의 코드펜스 *예시* 체크박스는 판정 대상이 아니다(문법 설명이 차단 사유가 되지 않게)."""
    body = _dod_body(
        "- [x] 코드",
        "",
        "```",
        "- [ ] 예시일 뿐(이 줄은 판정 대상 아님)",
        "```",
    )

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_dod_section_anchor_ignores_fenced_heading_quotes(board):
    """코드펜스 안에 인용된 `## 완료 조건`·`## …` 헤딩은 절 경계를 흔들지 못한다(양방향 오판).

    펜스를 먼저 걷지 않으면 ① 앞선 코드블록의 인용 헤딩이 절 시작으로 잡혀 실제 DoD 를 못 보고
    ② DoD 절 안의 인용 헤딩이 절 끝으로 잡혀 그 뒤 미체크 항목이 통째로 빠진다.
    """
    # ① 실제 DoD 절 **앞**에 인용된 헤딩 — 가짜 앵커가 이기면 진짜 미체크를 못 본다.
    quoted_before = (
        "# T-0596 — 픽스처\n\n## 목표\n문서에 문법을 인용한다.\n\n"
        "```markdown\n## 완료 조건 (Definition of Done)\n- [x] 인용된 예시\n```\n\n"
        "## 완료 조건 (Definition of Done)\n- [ ] 진짜 미체크 항목\n\n## 참고\n- 없음\n"
    )
    problems = board._complete_gate("T-0596", _gate_args(), quoted_before)
    assert any("진짜 미체크 항목" in p for p in problems), (
        f"펜스 안 인용 헤딩이 절 앵커를 가로챔 — 실제 미체크를 못 봄: {problems}")

    # ② DoD 절 **안**에 인용된 헤딩 — 가짜 절 끝이 이기면 그 뒤 미체크가 빠진다.
    quoted_inside = _dod_body(
        "- [x] 앞 항목",
        "",
        "```markdown",
        "## 참고",
        "```",
        "",
        "- [ ] 펜스 뒤 미체크 항목",
    )
    problems = board._complete_gate("T-0596", _gate_args(), quoted_inside)
    assert any("펜스 뒤 미체크 항목" in p for p in problems), (
        f"펜스 안 인용 헤딩이 절 끝으로 오판돼 뒤쪽 항목이 검사에서 빠짐: {problems}")


def test_dod_gate_detects_all_markdown_bullet_markers(board):
    """`-`·`*`·`+` 세 불릿 전부에서 미체크를 잡는다 — 하나라도 빠지면 그 표기가 조용히 통과(fail-open)."""
    for bullet in ("-", "*", "+"):
        body = _dod_body(f"{bullet} [ ] {bullet} 표기 미체크 항목")

        problems = board._complete_gate("T-0596", _gate_args(), body)

        assert any("DoD 미체크" in p for p in problems), (
            f"{bullet!r} 불릿 체크박스가 판정에서 빠짐: {problems}")


def test_dod_prose_or_missing_section_blocks(board):
    """체크박스 없는 산문 DoD·DoD 절 부재는 **차단**된다 (T-0781 — 레거시 면제 없음).

    옛 규칙은 "있는 체크박스의 미결만 막는다" 였다 — 그래서 절을 지우거나 산문으로만 적으면
    게이트가 통째로 꺼졌다(완료 증거 0으로 done). 같은 판정이 `lint`(thin)로 이미 open/claimed
    를 차단하고 있어 이 강화로 새로 잠기는 활성 티켓은 없다.
    """
    prose = _dod_body("완료 조건을 산문으로만 적은 옛 티켓.")
    no_section = "# T-0001 — 옛 티켓\n\n## 목표\n옛 형식.\n\n## 참고\n- 없음\n"

    prose_problems = board._complete_gate("T-0596", _gate_args(), prose)
    missing_problems = board._complete_gate("T-0001", _gate_args(), no_section)

    assert any("체크박스 0개" in p for p in prose_problems), prose_problems
    assert any("절 부재" in p for p in missing_problems), missing_problems


def test_dod_all_deferred_blocks_and_points_at_discard(board):
    """전항 `[>]`(사유 정상) → 차단 · 문구가 처분 커맨드(`discard`)를 지목한다.

    사유 붙은 이월은 개별로는 통과 형식이라, 전항이 이월이면 옛 게이트가 **구현 0** 인 티켓을
    done 으로 내보냈다(실증: done/ 의 병합·취소 4건). 그 형상은 완료가 아니라 처분이다.
    """
    body = _dod_body(
        "- [>] 코드 (이월: T-0766 으로 병합·PM 판정)",
        "- [>] 테스트 (이월: T-0766 으로 병합·PM 판정)",
    )

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("전량 이월" in p and "discard" in p for p in problems), problems


def test_dod_unchecked_only_is_not_diagnosed_as_all_deferred(board):
    """미체크만 있는 DoD 는 차단되되 **처분 안내를 받지 않는다** — 그건 미완료지 처분이 아니다.

    옛 판정은 `[x]` 0 하나만 봤다. 그래서 `- [ ]` 한 줄짜리 미완료 티켓도 "전량 이월 → discard"
    를 함께 안내했고, 그 안내대로 하면 아직 할 일이 남은 티켓이 처분으로 닫힌다(틀린 방향).
    """
    body = _dod_body("- [ ] 아직 안 한 항목")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("DoD 미체크" in p for p in problems), problems
    assert not any("전량 이월" in p for p in problems), problems


def test_dod_unknown_marker_only_is_not_diagnosed_as_all_deferred(board):
    """미지 마커만 있는 DoD 도 처분 안내를 받지 않는다 — 통과 형식이 아닐 뿐 이월도 아니다."""
    body = _dod_body("- [?] 통과 형식이 아닌 마커 항목")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("DoD 미체크" in p for p in problems), problems
    assert not any("전량 이월" in p for p in problems), problems


def test_dod_deferral_without_reason_only_is_not_diagnosed_as_all_deferred(board):
    """사유 없는 이월만 있는 DoD 도 처분 안내를 받지 않는다 — 유효 이월로 세지 않는다.

    사유 없는 `- [>]` 를 이월로 세면 사유를 지우는 것만으로 "전량 이월" 진단을 얻는다.
    """
    body = _dod_body("- [>] 사유 없는 이월 항목")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("이월 사유 누락" in p for p in problems), problems
    assert not any("전량 이월" in p for p in problems), problems


def test_dod_unchecked_mixed_with_valid_deferral_is_not_all_deferred(board):
    """유효 이월 + 미체크 혼합은 미완료다 — 전량 이월 진단이 없고 미체크만 지목된다."""
    body = _dod_body(
        "- [>] 문서 (이월: 후속 티켓 귀속)",
        "- [ ] 아직 안 한 항목",
    )

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("DoD 미체크" in p for p in problems), problems
    assert not any("전량 이월" in p for p in problems), problems


def test_dod_all_deferred_message_counts_valid_deferrals(board):
    """전량 이월 문구는 **유효 이월 개수**를 체크박스 수와 함께 낸다(판정 근거가 문구에 있다)."""
    body = _dod_body(
        "- [>] 코드 (이월: 다른 티켓으로 병합)",
        "- [>] 테스트 (이월: 다른 티켓으로 병합)",
        "- [>] 문서 (이월: 다른 티켓으로 병합)",
    )

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("사유 있는 이월 3 / 체크박스 3개" in p for p in problems), problems


def test_dod_partial_deferral_still_passes(board):
    """부분 이월(`[x]` ≥1 + 사유 있는 `[>]`)은 현행대로 통과 — 정당한 이월 경로를 막지 않는다."""
    body = _dod_body(
        "- [x] 코드",
        "- [>] 라이브 probe 실측 (이월: 하네스 한도 소진·T-0600 귀속)",
        "- [>] 문서 (이월: 후속 T-0601 귀속)",
    )

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_complete_gate_without_body_skips_dod(board):
    """본문 미제공 호출은 DoD 검사를 건너뛴다(기록 축만 묻는 레거시 호출 무영향)."""
    assert board._complete_gate("T-0596", _gate_args()) == []


# ── cmd_complete 실 경로 — 차단 시 티켓은 claimed/ 에 그대로 남는다 ─────────────
#
# `ticket_finish.py` 는 이 CLI(`board.py complete <id> --tests-pass`)를 그대로 호출하므로
# 마감 도구 경유 경로도 같은 게이트를 탄다(게이트 구현이 cmd_complete 안에 있다).

_TICKET_FRONTMATTER = (
    "---\n"
    "id: {tid}\n"
    "title: 픽스처\n"
    "status: {status}\n"
    "created: '2026-08-08'\n"
    "created_by: t/t_1\n"
    "claimed_by: t/t_1\n"
    "claimed_at: '2026-08-08T00:00:00+00:00'\n"
    "completed_at: null\n"
    "depends_on: []\n"
    "blocks: []\n"
    "touches: []\n"
    "estimate: small\n"
    # T-0815 설계 근거 게이트(developer 시드 seam) 관심사 밖 — `done` + 설계 절로 미리
    # 해소한다(면제 값은 폐지됐다).
    "design: done\n"
    "tags: []\n"
    "---\n\n"
    "## 설계\n"
    "- **경계 실측**: 완료 게이트 픽스처\n"
    "- **불변식**: 이 파일의 축 밖\n"
    "- **표면 상한**: 픽스처 1건\n"
    "- **테스트 전략**: 정상·실패 경로\n\n"
)


@pytest.fixture
def live_board(tmp_path, monkeypatch):
    """실 파일 이동이 도는 hermetic board — legacy 형상(board-git 비활성·sync no-op).

    정체성은 픽스처 티켓의 `claimed_by: t/t_1` 에 맞춰 **명시 바인딩**한다(T-0781 소유 게이트):
    세션은 `PM_SESSION_NAME` env, user 는 tmp `local.conf user=`. per-clone conf 의
    `session=` 폴백은 폐지됐으므로(T-0779) 세션 바인딩에 쓰지 않는다. git email 폴백은 None 으로
    막아 실 git config 가 user 축에 새지 않게 한다.
    """
    root = tmp_path / "proj"
    mod = _load_board_bound(root)
    monkeypatch.setattr(mod, "REPO", root)
    pm = root / ".project_manager"
    monkeypatch.setattr(mod, "BOARD_LOCK", pm / ".local" / "board.lock")
    monkeypatch.setattr(mod, "LOCAL_CONF", pm / "local.conf")
    monkeypatch.setattr(mod, "AREAS_FILE", pm / "areas.md")
    monkeypatch.setattr(mod, "LEASES_FILE", pm / ".local" / "worktree-leases.json")
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    for status in mod.STATUS_DIRS:
        (pm / "wiki" / "tickets" / status).mkdir(parents=True, exist_ok=True)
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "local.conf").write_text("identity.user=t\n", encoding="utf-8")
    monkeypatch.setenv("PM_SESSION_NAME", "t_1")
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    return mod


def _seed_ticket(board, tid: str, body: str, *, status: str = "claimed") -> Path:
    path = (board.tickets_dir() / status / f"{tid}-픽스처.md")
    path.write_text(
        _TICKET_FRONTMATTER.format(tid=tid, status=status) + body, encoding="utf-8")
    return path


def _complete_args(tid: str) -> argparse.Namespace:
    return argparse.Namespace(
        id=tid, tests_pass=True, allow_missing_log=True, allow_untested=False)


def _seed_cluster(board, cluster: str, tickets: tuple[str, ...], *, status: str = "open") -> Path:
    path = board.tickets_dir() / "clusters" / f"{cluster}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {cluster}\n"
        "tickets:\n" + "".join(f"- {tid}\n" for tid in tickets) +
        "base_branch: task/main\nbranch: task/fixture\nspike: null\n"
        "budget: {}\n"
        f"status: {status}\n"
        "---\n",
        encoding="utf-8",
    )
    return path


def test_cluster_member_complete_requires_exact_closer_binding(
    live_board, capsys, monkeypatch,
):
    """AT-001: cluster 멤버 direct complete는 첫 write 전 막고 exact closer만 연다."""
    tid = "T-9882"
    claimed = _seed_ticket(live_board, tid, _dod_body("- [x] 코드"))
    claimed.write_text(
        claimed.read_text(encoding="utf-8").replace("tags: []", "cluster: C-gate\ntags: []"),
        encoding="utf-8",
    )
    ledger = _seed_cluster(live_board, "C-gate", (tid,))
    sync_calls: list[tuple] = []
    monkeypatch.setattr(
        live_board, "_board_git_sync_best_effort",
        lambda *args: sync_calls.append(args) or True,
    )
    ticket_before = claimed.read_bytes()
    ledger_before = ledger.read_bytes()

    assert live_board.cmd_complete(_complete_args(tid)) == 1
    assert claimed.read_bytes() == ticket_before
    assert ledger.read_bytes() == ledger_before
    assert sync_calls == []
    assert "direct complete" in capsys.readouterr().err

    wrong = _complete_args(tid)
    wrong.cluster_close = "C-other"
    assert live_board.cmd_complete(wrong) == 1
    assert claimed.read_bytes() == ticket_before and ledger.read_bytes() == ledger_before
    assert sync_calls == []

    exact = _complete_args(tid)
    exact.cluster_close = "C-gate"
    assert live_board.cmd_complete(exact) == 0
    assert not claimed.exists()
    assert list((live_board.tickets_dir() / "done").glob(f"{tid}*"))
    assert len(sync_calls) == 1


def test_legacy_ticket_completes_with_its_size_one_binding(live_board, capsys, monkeypatch):
    """X-003: 장부도 역참조도 없는 구세대 티켓은 크기 1 해석 결속만 받고 나머지는 거부한다.

    `ticket_cluster` 의 이름 해석이 그 티켓을 `C-<티켓 ID>` 로 부르고 `ClusterCloser` 가 그 값을
    결속으로 싣고 오므로, 완료 게이트가 다른 규칙을 쓰면 그 티켓은 종결도 직접 완료도 못 하는
    막다른 길에 놓인다. 멤버 합성은 이 규칙의 입력이 아니다 — 장부 없는 이름의 멤버는 없다.
    """
    tid = "T-9884"
    claimed = _seed_ticket(live_board, tid, _dod_body("- [x] 코드"))
    assert "cluster:" not in claimed.read_text(encoding="utf-8")
    assert live_board.cluster_members(f"C-{tid}") == ()
    monkeypatch.setattr(live_board, "_board_git_sync_best_effort", lambda *args: True)
    ticket_before = claimed.read_bytes()

    wrong = _complete_args(tid)
    wrong.cluster_close = "C-other"
    assert live_board.cmd_complete(wrong) == 1
    assert claimed.read_bytes() == ticket_before
    assert f"C-{tid}" in capsys.readouterr().err

    exact = _complete_args(tid)
    exact.cluster_close = f"C-{tid}"
    assert live_board.cmd_complete(exact) == 0
    assert not claimed.exists()
    assert list((live_board.tickets_dir() / "done").glob(f"{tid}*"))


def test_cluster_binding_rejects_unreadable_ledger_catalog(live_board, capsys, monkeypatch):
    """F-003: 장부 catalog 를 한 파일이라도 못 읽으면 귀속 유일성 판정은 정지다.

    조회용 `all_clusters()` 는 손상 파일을 경고 뒤 건너뛴다. 그 관용을 mutation 판정이 쓰면
    빠진 파일이 그 티켓을 중복으로 담고 있을 가능성을 배제하지 못한 채 "owner 가 하나"라고
    말하게 된다 — 판정 불능은 통과가 아니라 첫 write 앞 정지다.
    """
    tid = "T-9885"
    claimed = _seed_ticket(live_board, tid, _dod_body("- [x] 코드"))
    claimed.write_text(
        claimed.read_text(encoding="utf-8").replace("tags: []", "cluster: C-gate\ntags: []"),
        encoding="utf-8",
    )
    ledger = _seed_cluster(live_board, "C-gate", (tid,))
    sync_calls: list[tuple] = []
    monkeypatch.setattr(
        live_board, "_board_git_sync_best_effort",
        lambda *args: sync_calls.append(args) or True,
    )
    ticket_before = claimed.read_bytes()
    ledger_before = ledger.read_bytes()
    exact = _complete_args(tid)
    exact.cluster_close = "C-gate"

    broken = live_board.tickets_dir() / "clusters" / "C-broken.md"
    broken.write_text("---\nid: C-broken\ntickets: [T-9885\n---\n", encoding="utf-8")
    assert live_board.cmd_complete(exact) == 1
    assert claimed.read_bytes() == ticket_before and ledger.read_bytes() == ledger_before
    assert sync_calls == []
    assert "전수 확인할 수 없어" in capsys.readouterr().err

    # id 를 선언하지 않은 장부도 같은 축이다 — 그 파일이 담은 멤버를 알 수 없다.
    broken.write_text("---\ntickets:\n- T-9885\nstatus: open\n---\n", encoding="utf-8")
    assert live_board.cmd_complete(exact) == 1
    assert claimed.read_bytes() == ticket_before and ledger.read_bytes() == ledger_before
    assert sync_calls == []
    assert "id 선언이 없다" in capsys.readouterr().err

    # 손상 장부를 치우면 같은 호출이 그대로 통과한다(가드가 정상 경로를 막지 않는다).
    broken.unlink()
    assert live_board.cmd_complete(exact) == 0
    assert list((live_board.tickets_dir() / "done").glob(f"{tid}*"))
    assert len(sync_calls) == 1


def _internal_round(
    sequence: int,
    verdict: int,
    must_fix: int,
    *,
    started_at: str,
    ts: str,
    target_rev: str,
) -> dict:
    return {
        "sequence": sequence,
        "verdict": verdict,
        "must_fix": must_fix,
        "started_at": started_at,
        "ts": ts,
        "target_rev": target_rev,
    }


def test_cmd_complete_blocks_unchecked_dod_and_keeps_ticket_claimed(
    live_board, capsys
):
    """미체크 DoD → rc=1 이고 티켓은 claimed/ 에 그대로(부분 이동·false-done 없음)."""
    path = _seed_ticket(live_board, "T-9001",
                        _dod_body("- [x] 코드", "- [ ] 라이브 probe 실측"))

    rc = live_board.cmd_complete(_complete_args("T-9001"))

    err = capsys.readouterr().err
    assert rc == 1, "미체크 DoD 인데 complete 가 통과함"
    assert path.exists(), "차단됐는데 티켓이 claimed/ 에서 사라짐"
    assert not list((live_board.tickets_dir() / "done").glob("T-9001*")), \
        "차단됐는데 done/ 으로 이동함"
    assert "sync gate failed" in err and "DoD 미체크" in err


def test_cmd_complete_passes_with_checked_and_deferred_dod(live_board):
    """`[x]` + 사유 붙은 `[>]` 조합 → rc=0 · done/ 이동(정상 마감 경로 불변)."""
    _seed_ticket(live_board, "T-9002", _dod_body(
        "- [x] 코드",
        "- [>] 라이브 probe 실측 (이월: 하네스 한도 소진·T-0600 귀속)",
    ))

    rc = live_board.cmd_complete(_complete_args("T-9002"))

    assert rc == 0
    assert list((live_board.tickets_dir() / "done").glob("T-9002*")), "done/ 이동 안 함"


def test_cmd_complete_blocks_an_undisposed_review_must_fix(live_board, capsys):
    """실 complete 경로도 리뷰 라운드 파일의 미처분 must-fix 를 막고, 처분 뒤에만 done 으로 옮긴다."""
    pd = _pm_delegate_module()
    tid = "T-9902"
    claimed = _seed_ticket(live_board, tid, _dod_body("- [x] 코드"))
    _write_round(live_board, tid, "01-code-reviewer.md", _reviewer_round_text(pd, "F-001"))

    assert live_board.cmd_complete(_complete_args(tid)) == 1
    assert claimed.exists()
    assert "code-reviewer 리뷰 라운드 01" in capsys.readouterr().err

    rejected = {
        "version": pd.PM_REVIEW_DISPOSITION_VERSION, "reviewer_ordinal": 1,
        "dispositions": [{
            "id": "F-001", "decision": "rejected", "reason": "PM 기각 근거",
            "scope": "", "prerequisite": "",
        }],
    }
    claimed.write_text(
        claimed.read_text(encoding="utf-8")
        + f"\n```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n"
        + json.dumps(rejected, ensure_ascii=False, separators=(",", ":")) + "\n```\n",
        encoding="utf-8",
    )
    assert live_board.cmd_complete(_complete_args(tid)) == 0
    assert list((live_board.tickets_dir() / "done").glob(f"{tid}*"))


def test_internal_legacy_fixed_resolution_is_unrecognized(board):
    entry = {"rounds": [_internal_round(
        1, 1, 1, started_at="2026-08-12T00:00:00+00:00",
        ts="2026-08-12T00:01:00+00:00", target_rev="a" * 40,
    )]}
    entry["resolution"] = {"kind": "fixed", "evidence_gate": "T-9302",
                           **board.gate_round_binding(entry)}
    assert board.gate_resolution(entry) is None


def test_done_tickets_are_not_retroactively_checked(live_board):
    """기존 done 티켓(미체크 DoD)은 소급 검사 대상이 아니다 — 새 complete 도 그것 때문에 막히지 않는다."""
    legacy = _seed_ticket(live_board, "T-9003",
                          _dod_body("- [ ] 옛 티켓의 미체크 항목"), status="done")
    before = legacy.read_text(encoding="utf-8")
    _seed_ticket(live_board, "T-9004", _dod_body("- [x] 코드"))

    rc = live_board.cmd_complete(_complete_args("T-9004"))

    assert rc == 0, "done/ 의 옛 미체크 DoD 가 무관한 complete 를 막음(소급 검사 발생)"
    assert legacy.exists() and legacy.read_text(encoding="utf-8") == before, \
        "옛 done 티켓이 건드려짐"


# ════════════════════════════════════════════════════════════════════════
# 중복 `## 완료 조건` 절 = 전 절 합산 (T-0605 ⑧)
# ════════════════════════════════════════════════════════════════════════
# codex R5 지적: 슬라이서가 **첫 절만** 검사한다 — 앞에 빈(또는 전부 체크된) `## 완료 조건` 절을
# 하나 두고 뒤 절에 `- [ ]` 를 남기면 완료 게이트가 통과한다. 판정 대상을 그 이름의 **모든** 절로
# 넓혀 닫는다(중복 자체를 새 형식 차단으로 만들지 않는다 — 기존 판정 구조가 "있는 체크박스의
# 미결만 막는다"이므로 레거시 본문에 새 차단 사유를 만들지 않는 쪽이 같은 축이다).

_DUPLICATE_DOD_BODY = (
    "# T-0605 — 픽스처\n\n## 목표\n게이트 판정 입력.\n\n"
    "## 완료 조건 (Definition of Done)\n- [x] 앞 절은 전부 체크돼 있다\n\n"
    "## 참고\n- 없음\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 뒤 절에 남은 미체크 항목\n\n"
    "## 메모\n"
)


def test_a_second_dod_section_cannot_hide_unchecked_items(board):
    """앞 절이 전부 체크돼 있어도 **뒤 절**의 미체크가 잡힌다 (재현 → 차단·DoD)."""
    problems = board._complete_gate("T-0605", _gate_args(), _DUPLICATE_DOD_BODY)

    assert any("뒤 절에 남은 미체크 항목" in p for p in problems), (
        f"앞 빈 절이 뒤 절을 가림: {problems}")


def test_every_dod_section_is_summed_not_just_the_first(board):
    """합산이다 — 두 절에 각각 남은 미체크가 **모두** 사유로 올라온다."""
    body = (
        "# T-0605 — 픽스처\n\n## 목표\n게이트 판정 입력.\n\n"
        "## 완료 조건 (Definition of Done)\n- [ ] 앞 절 항목\n\n"
        "## 참고\n- 없음\n\n"
        "## 완료 조건 (Definition of Done)\n- [ ] 뒤 절 항목\n\n## 메모\n"
    )

    problems = board._dod_open_items(body)

    # 미체크 사유는 절마다 하나씩 = 2 (전항 미체크라 T-0781 의 전량-이월 사유도 함께 올라오므로
    # 전체 길이가 아니라 미체크 축만 센다).
    unchecked = [p for p in problems if "DoD 미체크" in p]
    assert len(unchecked) == 2, problems
    assert any("앞 절 항목" in p for p in unchecked) and any("뒤 절 항목" in p for p in unchecked)


def test_duplicate_sections_fully_checked_still_pass(board):
    """정상 경로 무변경 — 중복 절이라도 전부 마감돼 있으면 통과다(중복 자체는 차단 사유 아님)."""
    body = (
        "# T-0605 — 픽스처\n\n## 목표\n게이트 판정 입력.\n\n"
        "## 완료 조건 (Definition of Done)\n- [x] 앞 절 항목\n\n"
        "## 참고\n- 없음\n\n"
        "## 완료 조건 (Definition of Done)\n"
        "- [>] 뒤 절 항목 (이월: 하네스 한도 소진·다음 wave 귀속)\n\n## 메모\n"
    )

    assert board._complete_gate("T-0605", _gate_args(), body) == []


def test_a_single_dod_section_is_unchanged(board):
    """단일 절 본문의 판정은 종전 그대로다 (합산 도입이 정상 티켓을 바꾸지 않는다)."""
    assert board._dod_open_items(_dod_body("- [x] 코드")) == []
    assert board._dod_section_texts(_dod_body("- [x] 코드")) == ["- [x] 코드\n"]


def test_cmd_complete_blocks_on_a_hidden_second_dod_section(live_board, capsys):
    """e2e — 우회 본문으로 부른 complete 는 rc 1 이고 티켓은 claimed/ 에 남는다."""
    path = _seed_ticket(live_board, "T-9005", _DUPLICATE_DOD_BODY)

    rc = live_board.cmd_complete(_complete_args("T-9005"))

    assert rc == 1
    assert path.exists()
    assert not list((live_board.tickets_dir() / "done").glob("T-9005*"))
    assert "DoD 미체크" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# 축 C — 라운드 판정 게이트 (순번 유일성·연속성만 차단)
# ════════════════════════════════════════════════════════════════════════
#
# 완료 증거가 성립하지 않는 것은 라운드가 지워졌거나(빈틈) 순서가 모호한(중복) 상태뿐이다.
# 산출 없는 라운드(시드 그대로)와 이름 문법 위반은 정보로만 낸다 — 미회수는 게이트가 아니다.


def _write_round(board, tid: str, name: str, text: str) -> Path:
    path = board._load_ticket_rounds().rounds_dir_for_ticket(tid, board.tickets_dir()) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _harvested_round_text(role: str) -> str:
    return f"## 라벨 ({role} · 2026-01-02)\n\n실제 산출.\n"


def _seeded_round_text(board, tid: str, role: str, path: Path) -> str:
    """예약 직후와 **같은 bytes** — 무편집(산출 없음) 판정의 유일한 통과 형상."""
    rounds = board._load_ticket_rounds()
    return rounds.render_round_seed(
        role, path.read_text(encoding="utf-8"), today="2026-01-02")


def test_round_gap_blocks_completion(live_board, capsys):
    """순번 빈틈(삭제 의심) → rc=1 · 티켓은 claimed/ 에 남는다."""
    path = _seed_ticket(live_board, "T-9101", _dod_body("- [x] 코드"))
    _write_round(live_board, "T-9101", "01-developer.md", _harvested_round_text("developer"))
    _write_round(live_board, "T-9101", "03-code-reviewer.md",
                 _harvested_round_text("code-reviewer"))

    rc = live_board.cmd_complete(_complete_args("T-9101"))

    err = capsys.readouterr().err
    assert rc == 1 and path.exists()
    assert not list((live_board.tickets_dir() / "done").glob("T-9101*"))
    assert "round-gap" in err and "02" in err


def test_round_duplicate_blocks_completion(live_board, capsys):
    """같은 순번을 둘이 쥔 상태(순서 모호) → rc=1."""
    path = _seed_ticket(live_board, "T-9102", _dod_body("- [x] 코드"))
    _write_round(live_board, "T-9102", "01-developer.md", _harvested_round_text("developer"))
    _write_round(live_board, "T-9102", "01-architect.md", _harvested_round_text("architect"))

    rc = live_board.cmd_complete(_complete_args("T-9102"))

    assert rc == 1 and path.exists()
    assert "round-dup" in capsys.readouterr().err


def test_pending_round_does_not_block_completion(live_board, capsys):
    """산출 없는 라운드(시드 그대로)는 정보 출력일 뿐 완료를 막지 않는다."""
    path = _seed_ticket(live_board, "T-9103", _dod_body("- [x] 코드"))
    _write_round(live_board, "T-9103", "01-developer.md",
                 _seeded_round_text(live_board, "T-9103", "developer", path))

    rc = live_board.cmd_complete(_complete_args("T-9103"))

    err = capsys.readouterr().err
    assert rc == 0, f"미회수 라운드가 완료를 막음: {err}"
    assert list((live_board.tickets_dir() / "done").glob("T-9103*"))
    assert "round-pending" in err and "01-developer.md" in err


def test_pending_round_survives_a_sibling_harvest_in_the_gate(live_board, capsys):
    """같은 역할 병렬 2라운드 — 01 회수 뒤에도 02 는 미회수 정보로 남는다(차단은 아니다).

    01 은 통과 리뷰다 — 반려 리뷰는 리뷰 잔여 축이 막으므로(별도 테스트) 이 축의 관심사 밖이다.
    """
    path = _seed_ticket(live_board, "T-9107", _dod_body("- [x] 코드"))
    _write_round(
        live_board, "T-9107", "01-code-reviewer.md",
        _passed_review_round_text(_pm_delegate_module()),
    )
    _write_round(
        live_board, "T-9107", "02-code-reviewer.md",
        _seeded_round_text(live_board, "T-9107", "code-reviewer", path),
    )

    rc = live_board.cmd_complete(_complete_args("T-9107"))

    err = capsys.readouterr().err
    assert rc == 0, f"미회수 라운드가 완료를 막음: {err}"
    assert "round-pending" in err and "02-code-reviewer.md" in err


def test_round_name_violation_is_information_only(live_board, capsys):
    """이름 문법 위반은 라운드 판정에서 표시용이다 — 다만 라운드를 읽을 수 없게 만든다.

    순번 판정(`_round_completion_problems`)의 차단 사유는 아니다. 완료 게이트의 리뷰 잔여
    판정은 라운드를 읽어야 하므로, 읽지 못하면 통과가 아니라 판정 불능 사유로 막는다.
    """
    path = _seed_ticket(live_board, "T-9104", _dod_body("- [x] 코드"))
    _write_round(live_board, "T-9104", "notes.md", "라운드가 아닌 파일\n")

    assert live_board._round_completion_problems(
        "T-9104", path.read_text(encoding="utf-8")) == []
    assert "round-name" in capsys.readouterr().err

    rc = live_board.cmd_complete(_complete_args("T-9104"))

    assert rc == 1
    assert "리뷰 잔여를 판정할 수 없습니다" in capsys.readouterr().err


def test_harvested_rounds_in_sequence_pass_the_gate(live_board):
    """1..N 연속·중복 없음 → 게이트 무영향(정상 경로 회귀 · 리뷰 라운드는 통과 판정)."""
    _seed_ticket(live_board, "T-9105", _dod_body("- [x] 코드"))
    _write_round(live_board, "T-9105", "01-developer.md", _harvested_round_text("developer"))
    _write_round(live_board, "T-9105", "02-code-reviewer.md",
                 _passed_review_round_text(_pm_delegate_module()))

    assert live_board.cmd_complete(_complete_args("T-9105")) == 0
    assert list((live_board.tickets_dir() / "done").glob("T-9105*"))


def test_gate_without_rounds_directory_is_silent(live_board, capsys):
    """라운드가 없는 티켓(대다수 레거시)은 이 축에서 아무 것도 내지 않는다."""
    assert live_board._complete_gate("T-9106", _gate_args(), _dod_body("- [x] 코드")) == []
    assert "round-" not in capsys.readouterr().err
