"""claim 소유 대조 · 처분 종결(discarded) · 오처리 복구(reopen) 단위 회귀 (T-0781).

세 축을 담는다.

**축 A — 소유 대조**: `claimed_by` 가 박힌 티켓의 상태 변경(`complete`·`block`·`unclaim`·
`unblock`·`discard`)은 그 소유 정체성에서만 성립한다. 옛 동작은 status 만 봤기 때문에 다른
슬롯이 claim 한 티켓을 아무 세션이나 complete 할 수 있었다(채택자 실사고 — 다른 슬롯이 발행한
티켓을 자기 것으로 착각해 `complete --tests-pass`, 실제 진행 0). 판정 축은 조회 뷰
(`_ticket_is_mine`)와 **같은 규칙**이고 판정 함수도 하나다(`_ticket_ownership` — `cmd_reid` 도
이 함수를 소비한다).

**축 B — 소유자 부재 이전**: `unclaim --takeover --reason <사유>` 하나뿐이다. 다른 커맨드에는
takeover 인수를 두지 않는다(문이 넷이면 소유 규칙이 네 벌이 된다). 사유는 티켓 본문과 board-git
커밋 메시지 **양쪽**에 남는다 — board-git 을 안 쓰는 채택자도 이력이 남아야 한다.

**축 C — 종결 두 종류**: `done`(구현 완료) · `discarded`(처분 — 병합·폐기). 처분을 complete 로
내보내던 옛 형상은 done/ 에 구현 0 티켓을 섞었다. `reopen` 은 그 종결을 되돌리는 유일한 문이다.

**hermetic 필수**: board.py 의 경로 전역(`REPO`·`TICKETS_DIR`·`LOCAL_CONF`·`LEASES_FILE` 등)은
import 시점에 실 repo 절대경로로 굳는다 — tmp 프로젝트로 재지정하고, 정체성은 **명시 바인딩**
(세션=`PM_SESSION_NAME` env 또는 `--repo/--slot` · user=tmp `local.conf user=`)만 쓴다. per-clone
conf 의 `session=` 폴백은 폐지됐으므로(T-0779) 세션 바인딩에 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 board git 케이스 skip.",
)

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

# 통과하는 DoD — 완료 게이트의 다른 축(전량 이월·절 부재)에 걸리지 않는 최소 본문.
_DOD_DONE = "## 완료 조건 (Definition of Done)\n- [x] 구현\n"
# 전량 이월 DoD — 구현 0. 실증 4건(T-0769·T-0773·T-0775 병합 · T-0743 취소)의 형상이다.
_DOD_ALL_DEFERRED = (
    "## 완료 조건 (Definition of Done)\n"
    "- [>] 구현 (이월: T-0002 로 병합 — PM 2단계 분할 판정)\n"
    "- [>] 테스트 (이월: T-0002 로 병합 — PM 2단계 분할 판정)\n"
)


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


@pytest.fixture
def board(tmp_path, monkeypatch):
    """tmp 프로젝트에 묶인 hermetic board — 세션 `pm_1` · user `alice` 바인딩."""
    mod = _load_board()
    proj = tmp_path / "proj"
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    for status in mod.STATUS_DIRS:
        (wiki / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (wiki / "log").mkdir(parents=True, exist_ok=True)
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": pm / ".local",
        "BOARD_LOCK": pm / ".local" / "board.lock",
        "LEASES_FILE": pm / ".local" / "worktree-leases.json",
        "PM_STATE_FILE": wiki / "pm_state.md",
        "PM_STATE_TEMPLATE": wiki / "pm_state.template.md",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    # user 축은 conf 로, 세션 축은 env 로 명시 바인딩(실 git config·실 clone conf 미접촉).
    (pm / "local.conf").write_text("user=alice\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    monkeypatch.setenv("PM_SESSION_NAME", "pm_1")
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    mod._proj = proj
    return mod


def _seed(board, tid: str = "T-0001", *, status: str = "claimed",
          claimed_by: str | None = "alice/pm_1", body: str = _DOD_DONE,
          created_by: str = "alice/pm_1", **extra) -> Path:
    """`<status>/<tid>-t.md` 티켓을 심는다 — frontmatter 는 엔진 dump 형식으로."""
    fm = {
        "id": tid, "title": "t", "status": status, "created": "2026-08-20",
        "created_by": created_by, "claimed_by": claimed_by,
        "claimed_at": "2026-08-20T00:00:00+00:00" if claimed_by else None,
        "completed_at": None, "depends_on": [], "blocks": [], "touches": [],
        "estimate": "small", "tags": [],
    }
    fm.update(extra)
    path = board.tickets_dir() / status / f"{tid}-t.md"
    board.dump_ticket(path, fm, f"# {tid} — t\n\n## 목표\nx\n\n{body}")
    return path


def _ticket_path(board, tid: str, status: str) -> Path:
    (path,) = list((board.tickets_dir() / status).glob(f"{tid}-*.md"))
    return path


def _write_leases(board, *sessions: str) -> None:
    rows = [{"repo": s.rsplit("_", 1)[0], "slot": f"work/{s}", "session": s,
             "state": "leased"} for s in sessions]
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({"leases": rows}), encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════
# 축 A — 소유 대조 (5 커맨드 × 7 형상)
# ════════════════════════════════════════════════════════════════════════
#
# 커맨드별 (출발 status, 인자, 통과 시 도착 status). 인자는 실 파서가 만드는 Namespace 와 같은
# 필드다 — 정체성 인자(`repo`/`slot`)는 케이스가 덧붙인다.

_COMMANDS = {
    "complete": ("claimed", dict(tests_pass=True, allow_missing_log=True,
                                 allow_untested=False), "done"),
    "block": ("claimed", dict(reason="차단 사유"), "blocked"),
    "unclaim": ("claimed", dict(takeover=False, reason=None), "open"),
    "unblock": ("blocked", dict(), "claimed"),
    "discard": ("claimed", dict(disposition="merged", reason="T-0002 로 병합"),
                "discarded"),
}


def _call(board, command: str, tid: str = "T-0001", **identity) -> int:
    _status, kwargs, _dest = _COMMANDS[command]
    args = argparse.Namespace(id=tid, **kwargs, **identity)
    return getattr(board, f"cmd_{command}")(args)


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_owner_session_passes(board, command):
    """형상 1 — user·slot 이 모두 일치하는 소유 세션은 종전대로 통과한다(회귀 기준선)."""
    source, _kwargs, dest = _COMMANDS[command]
    _seed(board, status=source)

    assert _call(board, command) == 0
    assert list((board.tickets_dir() / dest).glob("T-0001-*.md")), \
        f"{command}: 소유자 실행인데 {dest}/ 로 안 옮겨졌다"


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_other_slot_is_rejected_without_touching_the_ticket(board, command, capsys):
    """형상 2 — 같은 user·다른 슬롯은 거부. 파일은 한 바이트도 안 바뀌고 문구가 3요소를 낸다."""
    source, _kwargs, dest = _COMMANDS[command]
    path = _seed(board, status=source, claimed_by="alice/pm_2")
    before = path.read_bytes()

    rc = _call(board, command)

    err = capsys.readouterr().err
    assert rc == 1, f"{command}: 타 슬롯 claim 을 그대로 실행했다"
    assert path.read_bytes() == before, f"{command}: 거부인데 티켓이 바뀌었다"
    assert not list((board.tickets_dir() / dest).glob("T-0001-*.md"))
    assert "alice/pm_2" in err, err                      # (1) 누구 소유인지
    assert "unclaim" in err and "claim" in err, err      # (2) 정상 이동 경로
    assert "--takeover" in err, err                      # (3) 소유자 부재 시 우회


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_other_user_on_the_same_slot_is_rejected(board, command):
    """형상 3 — 슬롯 이름이 같아도 user 가 다르면 거부(동명 슬롯 교차사용자 누출 차단)."""
    source, _kwargs, _dest = _COMMANDS[command]
    path = _seed(board, status=source, claimed_by="bob/pm_1", created_by="bob/pm_1")
    before = path.read_bytes()

    assert _call(board, command) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_legacy_slot_only_claim_passes_on_a_solo_board(board, command):
    """형상 4 — user 토큰 없는 레거시 claim 은 solo 보드에서 슬롯 일치로 통과(degrade 보존)."""
    source, _kwargs, dest = _COMMANDS[command]
    _seed(board, status=source, claimed_by="pm_1", created_by="pm_1")

    assert _call(board, command) == 0
    assert list((board.tickets_dir() / dest).glob("T-0001-*.md"))


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_legacy_slot_only_claim_is_rejected_on_a_multi_user_board(board, command):
    """형상 5 — 다중사용자 보드의 슬롯-only claim 은 귀속이 모호하다 → 거부(strict-exclude)."""
    source, _kwargs, _dest = _COMMANDS[command]
    path = _seed(board, status=source, claimed_by="pm_1", created_by="pm_1")
    # 다중사용자 신호 — 다른 user 로 귀속된 티켓 1건이면 `_distinct_ticket_users()` 가 2가 된다.
    _seed(board, "T-0002", status="open", claimed_by=None, created_by="bob/pm_9")
    before = path.read_bytes()

    assert _call(board, command) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_unresolved_session_is_rejected_and_names_the_explicit_flags(
        board, command, monkeypatch, capsys):
    """형상 6 — 세션 미해소(활성 리스 2개·인자 없음)는 거부하고 `--repo/--slot` 을 안내한다.

    "검사 skip" 폴백은 두지 않는다 — 소유를 증명할 수 없는 상태에서 남의 티켓을 옮기는 쪽이
    더 나쁘다(claim 의 `required=True` fail-loud 와 대칭).
    """
    source, _kwargs, _dest = _COMMANDS[command]
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    _write_leases(board, "pm_1", "pm_2")
    path = _seed(board, status=source)
    before = path.read_bytes()

    rc = _call(board, command)

    err = capsys.readouterr().err
    assert rc == 1
    assert path.read_bytes() == before
    assert "세션 미해소" in err and "--repo" in err and "--slot" in err, err


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_ticket_without_an_owner_is_not_a_target(board, command):
    """형상 7 — `claimed_by` 가 없으면 소유자 자체가 없다 → 무대상(새 차단 아님)."""
    source, _kwargs, _dest = _COMMANDS[command]
    _seed(board, status=source, claimed_by=None)

    assert _call(board, command) == 0


def test_open_ticket_block_and_claim_are_unaffected(board):
    """open 티켓의 block 은 소유자 개념이 없어 현행 그대로 통과한다(회귀 보증)."""
    _seed(board, status="open", claimed_by=None)

    assert board.cmd_block(argparse.Namespace(id="T-0001", reason="r")) == 0
    assert list((board.tickets_dir() / "blocked").glob("T-0001-*.md"))


def test_explicit_repo_slot_args_resolve_the_acting_session(board, monkeypatch):
    """정체성은 env 뿐 아니라 `--repo/--slot` 명시로도 해소된다(claim 과 같은 seam)."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    _write_leases(board, "pm_1", "pm_2")
    _seed(board, claimed_by="alice/pm_2")

    assert _call(board, "complete", repo="pm", slot=1) == 1     # 타 슬롯 명시 → 거부
    assert _call(board, "complete", repo="pm", slot=2) == 0     # 소유 슬롯 명시 → 통과
    assert list((board.tickets_dir() / "done").glob("T-0001-*.md"))


def test_reid_consumes_the_same_ownership_judgement(board, capsys):
    """`reid` 의 타 세션 claim 가드도 같은 판정 함수를 쓴다 — user 축까지 본다(판정 2벌 금지)."""
    _seed(board, claimed_by="bob/pm_1", created_by="bob/pm_1")

    rc = board.cmd_reid(argparse.Namespace(
        old_id="T-0001", new_id="T-0250", dry_run=False, user_ack=None,
        repo=None, slot=None, task=None))

    assert rc == 1
    assert "claim 중" in capsys.readouterr().err
    assert list((board.tickets_dir() / "claimed").glob("T-0001-*.md"))


# ════════════════════════════════════════════════════════════════════════
# 축 B — 소유자 부재 이전 (unclaim --takeover)
# ════════════════════════════════════════════════════════════════════════

def test_takeover_releases_a_foreign_claim_and_records_the_reason(board):
    """`--takeover --reason` 은 타 세션 claim 을 해제하고 사유를 **본문에** 남긴다."""
    _seed(board, claimed_by="bob/other_1", created_by="bob/other_1")

    rc = board.cmd_unclaim(argparse.Namespace(
        id="T-0001", takeover=True, reason="소유 세션 종료 — PM 43 인계"))

    assert rc == 0
    fm, body = board.load_ticket(_ticket_path(board, "T-0001", "open"))
    assert fm["claimed_by"] is None and fm["claimed_at"] is None
    assert "claimed_rev" not in fm
    assert "## Takeover" in body and "소유 세션 종료 — PM 43 인계" in body
    assert "bob/other_1" in body, "이전된 소유자가 이력에 안 남았다"


def test_takeover_without_a_reason_is_rejected(board):
    """`--reason` 없는 `--takeover` 는 거부 — 이력 없는 강제 이전을 만들지 않는다."""
    path = _seed(board, claimed_by="bob/other_1", created_by="bob/other_1")
    before = path.read_bytes()

    assert board.cmd_unclaim(argparse.Namespace(
        id="T-0001", takeover=True, reason=None)) == 1
    assert path.read_bytes() == before


def test_takeover_accepts_a_blocked_ticket_and_keeps_it_blocked(board):
    """blocked + 소유자 부재 교착 해소 — 상태는 blocked 유지, 소유만 해제한다.

    `unblock` 이 `claimed_by` 를 보고 claimed/ 로 복귀시키므로(T-0783), unclaim 이 blocked 를
    못 받으면 퇴장한 세션의 blocked 티켓은 어떤 문으로도 안 풀린다.
    """
    _seed(board, status="blocked", claimed_by="bob/other_1", created_by="bob/other_1")

    rc = board.cmd_unclaim(argparse.Namespace(
        id="T-0001", takeover=True, reason="소유 슬롯 반납됨"))

    assert rc == 0
    fm, _body = board.load_ticket(_ticket_path(board, "T-0001", "blocked"))
    assert fm["status"] == "blocked", "unclaim 이 차단까지 풀었다(소유 해제만이어야 한다)"
    assert fm["claimed_by"] is None
    assert not list((board.tickets_dir() / "open").glob("T-0001-*.md"))


def test_owner_unclaim_is_unchanged_by_the_takeover_flag(board):
    """정상 소유자의 unclaim 은 종전 그대로 — takeover 노트도 안 남는다(현행 무변경)."""
    _seed(board, claimed_rev="a" * 40)

    assert board.cmd_unclaim(argparse.Namespace(
        id="T-0001", takeover=False, reason=None)) == 0

    fm, body = board.load_ticket(_ticket_path(board, "T-0001", "open"))
    assert fm["status"] == "open" and fm["claimed_by"] is None
    assert "claimed_rev" not in fm
    assert "## Takeover" not in body


def test_blocked_owner_can_unclaim_without_takeover(board):
    """소유자 본인은 takeover 없이도 blocked 티켓의 소유를 놓을 수 있다(상태 유지)."""
    _seed(board, status="blocked")

    assert board.cmd_unclaim(argparse.Namespace(
        id="T-0001", takeover=False, reason=None)) == 0
    fm, _body = board.load_ticket(_ticket_path(board, "T-0001", "blocked"))
    assert fm["claimed_by"] is None and fm["status"] == "blocked"


# ════════════════════════════════════════════════════════════════════════
# 축 C — 처분 종결(discard) · 오처리 복구(reopen)
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("source", ["open", "claimed", "blocked"])
def test_discard_closes_from_every_active_status(board, source):
    """활성 3상태 전부에서 처분 종결로 닫힌다 — frontmatter 에 종류·사유가 박힌다."""
    _seed(board, status=source, claimed_by=None if source == "open" else "alice/pm_1")

    rc = board.cmd_discard(argparse.Namespace(
        id="T-0001", disposition="merged", reason="T-0002 로 병합 — 2단계 분할"))

    assert rc == 0
    fm, body = board.load_ticket(_ticket_path(board, "T-0001", "discarded"))
    assert fm["status"] == "discarded"
    assert fm["disposition"] == "merged"
    assert fm["disposition_reason"] == "T-0002 로 병합 — 2단계 분할"
    assert "## Discarded" in body


def test_discard_rejects_an_already_terminal_ticket(board):
    """이미 종결된 티켓은 처분 대상이 아니다 — 되돌리려면 `reopen` 이 먼저다."""
    _seed(board, status="done", claimed_by="alice/pm_1")

    rc = board.cmd_discard(argparse.Namespace(
        id="T-0001", disposition="dropped", reason="폐기"))

    assert rc == 1
    assert list((board.tickets_dir() / "done").glob("T-0001-*.md"))


def test_discard_requires_a_reason(board):
    """사유 없는 처분은 거부 — 왜 닫혔는지 모르는 종결을 만들지 않는다."""
    path = _seed(board)
    before = path.read_bytes()

    assert board.cmd_discard(argparse.Namespace(
        id="T-0001", disposition="dropped", reason="   ")) == 1
    assert path.read_bytes() == before


def test_discarded_is_hidden_from_the_default_view_and_shown_by_selector(board, capsys):
    """처분 티켓은 기본 `list` 에서 접히고 `--status discarded` 로만 보인다(done 과 비혼합)."""
    _seed(board, "T-0001", status="discarded", claimed_by="alice/pm_1",
          disposition="dropped", disposition_reason="취소")
    _seed(board, "T-0002", status="open", claimed_by=None)

    assert board.cmd_list(argparse.Namespace(
        status=None, tag=None, mine=False, all=True, task=None, repo=None,
        slot=None)) == 0
    default_out = capsys.readouterr().out
    assert "T-0002" in default_out and "T-0001" not in default_out

    assert board.cmd_list(argparse.Namespace(
        status="discarded", tag=None, mine=False, all=True, task=None, repo=None,
        slot=None)) == 0
    assert "T-0001" in capsys.readouterr().out


def test_board_md_renders_a_discarded_section_without_a_key_error(board):
    """board.md 렌더가 처분 섹션을 낸다 — 상태 추가 시 emoji 표 누락(KeyError) 회귀 가드.

    done 카운트는 처분에 영향받지 않는다(두 종결이 집계에서 섞이지 않는다).
    """
    _seed(board, "T-0001", status="discarded", claimed_by="alice/pm_1",
          disposition="merged", disposition_reason="T-0002 로 병합")
    _seed(board, "T-0003", status="done", claimed_by="alice/pm_1")

    board.refresh_board()

    text = board.BOARD_FILE.read_text(encoding="utf-8")
    assert "DISCARDED (1)" in text and "DONE (1)" in text
    assert "T-0002 로 병합" in text
    done_section = text.split("DONE (1)")[1].split("## ")[0]
    assert "T-0001" not in done_section, "처분 티켓이 done 섹션에 섞였다"


def test_task_end_scan_ignores_discarded_tickets(board):
    """task 소진 게이트는 종결(done·discarded)을 안 본다 — 처분 티켓이 task end 를 영구 차단하지 않는다."""
    _seed(board, "T-0001", status="discarded", claimed_by="alice/job1",
          disposition="dropped", disposition_reason="취소")

    assert board.scan_task_tickets("alice", "job1")["claimed"] == []


def test_reopen_resets_every_terminal_field(board):
    """`reopen` 은 완료·처분·소유 표식을 **전부** 비우고 사유를 본문에 남긴다(값 단언)."""
    _seed(board, status="discarded", claimed_by="alice/pm_1",
          claimed_rev="b" * 40, completed_at="2026-08-20T01:00:00+00:00",
          disposition="merged", disposition_reason="T-0002 로 병합")

    rc = board.cmd_reopen(argparse.Namespace(id="T-0001", reason="병합 판정 철회"))

    assert rc == 0
    fm, body = board.load_ticket(_ticket_path(board, "T-0001", "open"))
    assert fm["status"] == "open"
    assert fm["completed_at"] is None
    assert fm["claimed_by"] is None and fm["claimed_at"] is None
    assert "claimed_rev" not in fm
    assert "disposition" not in fm and "disposition_reason" not in fm
    assert "## Reopened" in body and "병합 판정 철회" in body


def test_reopen_rejects_a_non_terminal_ticket(board):
    """진행 중(open·claimed·blocked) 티켓은 되돌릴 종결이 없다 → 거부."""
    path = _seed(board, status="claimed")
    before = path.read_bytes()

    assert board.cmd_reopen(argparse.Namespace(id="T-0001", reason="아무거나")) == 1
    assert path.read_bytes() == before


def test_reopened_ticket_runs_the_normal_cycle_again(board):
    """왕복 — done → reopen → claim → complete 가 정상 사이클로 돈다."""
    _seed(board, status="done", claimed_by="alice/pm_1",
          completed_at="2026-08-20T01:00:00+00:00")

    assert board.cmd_reopen(argparse.Namespace(id="T-0001", reason="오처리 복구")) == 0
    assert board.cmd_claim(argparse.Namespace(
        id="T-0001", repo="pm", slot=1, user="alice")) == 0
    fm, _body = board.load_ticket(_ticket_path(board, "T-0001", "claimed"))
    assert fm["claimed_by"] == "alice/pm_1"

    assert _call(board, "complete") == 0
    assert list((board.tickets_dir() / "done").glob("T-0001-*.md"))


def test_all_deferred_completion_is_refused_but_the_discard_path_works(board, capsys):
    """backfill 2스텝 회귀 — 전량-이월 티켓은 complete 가 막히고 `reopen`→`discard` 로 닫힌다.

    done/ 에 남은 실증 4건(병합 3·취소 1)을 정정하는 절차 그대로다 — 전용 backfill 코드를
    신설하지 않고 lifecycle 커맨드 2개만 쓴다.
    """
    _seed(board, status="done", claimed_by="alice/pm_1", body=_DOD_ALL_DEFERRED,
          completed_at="2026-08-20T01:00:00+00:00")

    assert board.cmd_reopen(argparse.Namespace(
        id="T-0001", reason="처분 종결로 재분류(T-0781 backfill)")) == 0
    # 되돌린 뒤 다시 claim→complete 를 시도하면 전량 이월이라 막힌다(그 형상이 done 이 아님).
    assert board.cmd_claim(argparse.Namespace(
        id="T-0001", repo="pm", slot=1, user="alice")) == 0
    assert _call(board, "complete") == 1
    assert "discard" in capsys.readouterr().err

    assert board.cmd_unclaim(argparse.Namespace(
        id="T-0001", takeover=False, reason=None)) == 0
    assert board.cmd_discard(argparse.Namespace(
        id="T-0001", disposition="merged",
        reason="T-0002 로 병합 — 구현분 없음(T-0781 backfill)")) == 0

    fm, _body = board.load_ticket(_ticket_path(board, "T-0001", "discarded"))
    assert fm["disposition"] == "merged"
    assert not list((board.tickets_dir() / "done").glob("T-0001-*.md"))


# ════════════════════════════════════════════════════════════════════════
# board-git 결속 — takeover 사유 커밋 · 원격 처분 티켓 claim race-lost
# ════════════════════════════════════════════════════════════════════════

def _make_board_git(board, root: Path, *, remote: Path, tid: str = "T-0001",
                    status: str = "claimed", claimed_by: str = "bob/other_1") -> Path:
    """`<root>/.project_manager/board/` 에 실 board git(+remote)을 세우고 티켓 1건을 심는다."""
    board_dir = root / ".project_manager" / "board"
    for name in board.STATUS_DIRS:
        (board_dir / "tickets" / name).mkdir(parents=True, exist_ok=True)
    fm = {
        "id": tid, "title": "t", "status": status, "created": "2026-08-20",
        "created_by": claimed_by, "claimed_by": claimed_by,
        "claimed_at": "2026-08-20T00:00:00+00:00", "completed_at": None,
        "depends_on": [], "blocks": [], "touches": [], "estimate": "small", "tags": [],
    }
    board.dump_ticket(board_dir / "tickets" / status / f"{tid}-t.md", fm,
                      f"# {tid} — t\n\n## 목표\nx\n\n{_DOD_DONE}")
    (board_dir / "areas.md").write_text("# Area Registry\n", encoding="utf-8")
    _git(["init", "-q", "-b", "main"], board_dir)
    _git(["remote", "add", "origin", str(remote)], board_dir)
    _git(["add", "-A"], board_dir)
    _git(["commit", "-qm", "board init"], board_dir)
    _git(["push", "-q", "-u", "origin", "main"], board_dir)
    return board_dir


@requires_git
def test_takeover_reason_lands_in_the_board_git_commit(board, tmp_path):
    """takeover 사유는 board-git 커밋 메시지에도 남는다 — 본문 노트와 **양쪽**이다."""
    bare = tmp_path / "bare-takeover"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(board, board._proj, remote=bare)

    rc = board.cmd_unclaim(argparse.Namespace(
        id="T-0001", takeover=True, reason="소유 슬롯 반납 — PM 인계"))

    assert rc == 0
    subject = _git(["log", "-1", "--format=%s"], board_dir).stdout.strip()
    assert "takeover" in subject and "소유 슬롯 반납 — PM 인계" in subject, subject


@requires_git
def test_remote_discarded_ticket_makes_a_claim_race_lost(board, tmp_path, capsys):
    """원격에서 이미 처분된 티켓은 claim 이 race-lost 로 막힌다(로컬만 open 인 stale 사본).

    `_board_git_remote_ticket_status` 판정은 STATUS_DIRS 를 추종하므로 새 종결 상태가 자동
    편입된다 — 손-열거였다면 처분 티켓이 조용히 claim 가능해진다.
    """
    bare = tmp_path / "bare-race"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(board, board._proj, remote=bare, status="discarded",
                                claimed_by="bob/other_1")
    # 로컬 사본만 open 으로 되돌린 stale 형상 — 원격은 여전히 discarded 다.
    local = board_dir / "tickets" / "discarded" / "T-0001-t.md"
    local.rename(board_dir / "tickets" / "open" / "T-0001-t.md")

    rc = board.cmd_claim(argparse.Namespace(
        id="T-0001", repo="pm", slot=1, user="alice"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "discarded" in err, err
