"""board.py mutation 커맨드 — wrong-state 거부 매트릭스 (T-0784 공백 1).

board 5축 감사(2026-08-20)로 확인된 공백: `claim`·`complete`·`block`·`unclaim`·`unblock`·
`discard`·`reopen`·`promote` 8종 mutation 의 "존재하는 티켓·틀린 상태" 거부 문구를 단언하는
회귀가 없었다(`grep "cannot complete|must be claimed|cannot unclaim|cannot unblock|must be
blocked|cannot block from" tests/` → 0건). 소유 대조(T-0781)·claimed↔blocked 왕복(T-0783)은
각 티켓이 소유한다 — 이 파일은 **상태 하나**만 본다(정확 일치 조회가 이미 성공한 뒤의 상태
검사). 모든 mutation 이 상태 검사를 소유 대조보다 **먼저** 하므로(코드 실측) 이 축은 정체성
바인딩 없이도 도달한다.

**hermetic 필수**: board.py 경로 전역은 tmp 프로젝트로 monkeypatch 재지정한다
(test_board_prefix_cli.py 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    local = pm / ".local"
    mod = _load_board()
    for status in mod.STATUS_DIRS:
        (wiki / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (wiki / "log").mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "BOARD_LOCK": local / "board.lock",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    (pm / "local.conf").write_text("identity.user=me\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    return mod


def _seed(board, tid: str, status: str) -> Path:
    """`<status>/<tid>-t.md` 를 심는다 — 손상 0·claimed_by 없음(상태 검사만 겨냥)."""
    path = board.TICKETS_DIR / status / f"{tid}-t.md"
    board.dump_ticket(
        path,
        {"id": tid, "title": "t", "status": status, "created": "2026-08-20",
         "claimed_by": None, "claimed_at": None, "completed_at": None,
         "depends_on": [], "blocks": [], "touches": [], "estimate": "small", "tags": []},
        f"# {tid} — t\n\n## 목표\nx\n\n## 완료 조건 (Definition of Done)\n- [x] 구현\n")
    return path


def _tickets_tree_snapshot(board) -> dict[str, bytes]:
    """TICKETS_DIR 전체(모든 상태 디렉토리) relpath→bytes.

    거부 전후로 비교해 원본 티켓 bytes 변경·형제 파일 생성·다른 상태 디렉토리로의
    무관 이동을 한 번에 잡는다(F-001 — path 존재·파일명 set 만으로는 이들을 놓친다).
    """
    out: dict[str, bytes] = {}
    for p in board.TICKETS_DIR.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(board.TICKETS_DIR))] = p.read_bytes()
    return out


def _derived_files_snapshot(board) -> dict[str, bytes | None]:
    """board/status/log 파생 파일의 존재·bytes — 거부가 이들을 재생성/변경하지 않는지 대조용."""
    return {
        "board": board.BOARD_FILE.read_bytes() if board.BOARD_FILE.exists() else None,
        "status": board.STATUS_FILE.read_bytes() if board.STATUS_FILE.exists() else None,
        "log": board.LOG_FILE.read_bytes() if board.LOG_FILE.exists() else None,
    }


def _expected_rejection_text(board, command: str, tid: str, status: str) -> str:
    """board.py 의 거부 메시지 리터럴을 그대로 인용 — 커맨드별 허용 출발 상태 문구를 고정한다."""
    if command == "claim":
        return f"cannot claim {tid}: currently in {status}/"
    if command == "complete":
        return f"cannot complete {tid}: in {status}/, must be claimed"
    if command == "block":
        return f"cannot block from {status}/"
    if command == "unclaim":
        allowed = "/".join(board._UNCLAIM_SOURCE_STATUSES)
        return f"cannot unclaim {tid}: in {status}/, must be {allowed}"
    if command == "unblock":
        return f"cannot unblock {tid}: in {status}/, must be blocked"
    if command == "discard":
        allowed = "/".join(board._DISCARD_SOURCE_STATUSES)
        return f"cannot discard {tid}: in {status}/, must be {allowed}"
    if command == "reopen":
        allowed = "/".join(board.TERMINAL_STATUS_DIRS)
        return f"cannot reopen {tid}: in {status}/, must be {allowed}"
    if command == "promote":
        return f"cannot promote {tid}: currently in {status}/ (promote 는 open/draft 만)"
    raise AssertionError(f"unmapped command: {command}")


# ── 커맨드별 유효 출발 상태 (코드 실측 — board.py 단일 진실을 그대로 인용) ─────────
#   claim:    status == "open"                                     (board.py cmd_claim)
#   complete: status == "claimed"                                  (board.py cmd_complete)
#   block:    status in ("open", "claimed")                        (board.py cmd_block)
#   unclaim:  status in _UNCLAIM_SOURCE_STATUSES = ("claimed", "blocked")
#   unblock:  status == "blocked"                                  (board.py cmd_unblock)
#   discard:  status in _DISCARD_SOURCE_STATUSES = ACTIVE_STATUS_DIRS (open/claimed/blocked)
#   reopen:   status in TERMINAL_STATUS_DIRS = ("done", "discarded")
#   promote:  status in ("open", "draft")  — "draft" 는 STATUS_DIRS 밖(별도 축)이라 여기 무관.
# 무효 상태 = STATUS_DIRS - 유효 상태. 손으로 나열하지 않고 board 모듈 상수에서 뺀다
# (새 상태가 STATUS_DIRS 에 늘면 이 매트릭스가 자동으로 그 상태를 포함한다).

def _args(command: str, tid: str) -> argparse.Namespace:
    extra = {
        "claim": dict(repo="me", slot=1, user="me"),
        "complete": dict(tests_pass=True, allow_missing_log=True, allow_untested=False),
        "block": dict(reason="사유"),
        "unclaim": dict(takeover=False, reason=None),
        "unblock": dict(),
        "discard": dict(disposition="merged", reason="사유"),
        "reopen": dict(reason="사유"),
        "promote": dict(),
    }[command]
    return argparse.Namespace(id=tid, **extra)


def _valid_statuses(board, command: str) -> frozenset[str]:
    return {
        "claim": frozenset({"open"}),
        "complete": frozenset({"claimed"}),
        "block": frozenset({"open", "claimed"}),
        "unclaim": frozenset(board._UNCLAIM_SOURCE_STATUSES),
        "unblock": frozenset({"blocked"}),
        "discard": frozenset(board._DISCARD_SOURCE_STATUSES),
        "reopen": frozenset(board.TERMINAL_STATUS_DIRS),
        "promote": frozenset({"open"}),
    }[command]


_COMMANDS = ("claim", "complete", "block", "unclaim", "unblock",
             "discard", "reopen", "promote")


def _wrong_state_cases(board) -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for command in _COMMANDS:
        valid = _valid_statuses(board, command)
        for status in board.STATUS_DIRS:
            if status not in valid:
                cases.append((command, status))
    return cases


# 수집 시점(모듈 로드 시)에 실측 (command, status) 조합을 물질화한다 — 정적 상한+skip 대신
# 직접 parametrize 이라 조합이 늘거나 줄어도 전량이 수집/실행된다(STATUS_DIRS 가 늘면 이 목록도
# 그만큼 늘어 자동으로 커버한다). 현재 실측 27건(F-002 — range(40)+padding skip 13 제거).
_WRONG_STATE_CASES: list[tuple[str, str]] = _wrong_state_cases(_load_board())


@pytest.fixture
def cases(board):
    return _wrong_state_cases(board)


def test_wrong_state_matrix_is_non_empty(cases):
    """전제 고정 — 이 매트릭스가 실제로 (커맨드×무효상태) 조합을 만들어내는지."""
    assert len(cases) >= 8 * 2, cases


@pytest.mark.parametrize("command, status", _WRONG_STATE_CASES)
def test_wrong_state_rejects_without_moving_the_ticket(board, capsys, command, status):
    """존재하는 티켓을 틀린 상태에서 부른 mutation 은 rc≠0 이고 부작용을 남기지 않는다 (I1).

    `command, status` 는 `_WRONG_STATE_CASES`(모듈 로드 시 물질화 — 실측 27 조합) 에서 직접
    parametrize 된다. tickets 트리 전체(relpath→bytes)와 board/status/log 파생 파일을 거부
    전후로 비교해 원본 bytes 변경·형제 파일 생성·무관 이동·파생 파일 재생성을 모두 잡고,
    stderr 는 커맨드별 허용 출발 상태 문구까지 리터럴로 고정한다.
    """
    tid = "T-0001"
    _seed(board, tid, status)
    before_tickets = _tickets_tree_snapshot(board)
    before_derived = _derived_files_snapshot(board)

    rc = getattr(board, f"cmd_{command}")(_args(command, tid))

    assert rc != 0, f"{command} on {status}/ 가 통과함 — wrong-state 거부 회귀."
    assert _tickets_tree_snapshot(board) == before_tickets, (
        f"{command} 거부인데 tickets 트리에 부작용(파일 이동/변경/생성)이 생김.")
    assert _derived_files_snapshot(board) == before_derived, (
        f"{command} 거부인데 board/status/log 파생 파일이 바뀜.")
    err = capsys.readouterr().err
    expected = _expected_rejection_text(board, command, tid, status)
    assert expected in err, f"{command} 거부 문구가 기대와 다르다: {err!r} (기대: {expected!r})"


def test_wrong_state_matrix_covers_t0781_disposal_terminal_states():
    """DoD — wrong-state 매트릭스가 T-0781 의 처분 종결(discard→discarded/)·reopen 을 포함한다."""
    board = _load_board()
    cases = _wrong_state_cases(board)
    commands = {c for c, _ in cases}
    assert "discard" in commands and "reopen" in commands
    discard_invalid = {s for c, s in cases if c == "discard"}
    reopen_invalid = {s for c, s in cases if c == "reopen"}
    assert "discarded" in discard_invalid, "discard 매트릭스에 이미 종결된 discarded/ 가 없다"
    assert "done" in discard_invalid, "discard 매트릭스에 done/ 이 없다"
    assert {"open", "claimed", "blocked"} <= reopen_invalid, (
        "reopen 매트릭스에 활성 상태 3종이 없다")


# ════════════════════════════════════════════════════════════════════════
# 정상 경로 역방향 확인 — 새 wrong-state 가드가 정당한 전이를 막지 않는다
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("command, status", [
    ("claim", "open"),
    ("complete", "claimed"),
    ("block", "open"),
    ("unclaim", "claimed"),
    ("unblock", "blocked"),
    ("discard", "open"),
    ("reopen", "done"),
    ("promote", "open"),
])
def test_valid_state_still_passes(board, command, status):
    """역방향 — 유효 상태에서는 종전대로 rc0 이다(과도한 단언으로 정상 경로를 막지 않았는지)."""
    tid = "T-0001"
    _seed(board, tid, status)

    rc = getattr(board, f"cmd_{command}")(_args(command, tid))

    assert rc == 0, f"{command} on {status}/(유효 상태)가 거부됨 — 회귀."
