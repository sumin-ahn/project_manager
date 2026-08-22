"""티켓 라운드 예약(section-add)·조회(show)와 위임 tier의 실제 CLI/board-git 계약.

section-add 는 명세 파일을 건드리지 않는다 — 쓰는 것은 `tickets/rounds/<티켓>/NN-<role>.md`
하나뿐이고, 그래서 lifecycle writer 와의 경합 축은 tier(in-place frontmatter writer)만 남는다.
"""
from __future__ import annotations

import ast
import datetime
import importlib.util
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from _git import commit_env
from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git 바이너리 부재 — 실 board-git 케이스 skip.")

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "growth-test",
    "GIT_AUTHOR_EMAIL": "growth@test.invalid",
    "GIT_COMMITTER_NAME": "growth-test",
    "GIT_COMMITTER_EMAIL": "growth@test.invalid",
}

_VALID_BODY = (
    "# {tid} — 성장 테스트\n\n"
    "## 목표\n위임 라운드 기록을 누적한다.\n\n"
    "## 인터페이스\nboard.py CLI 두 개를 쓴다.\n\n"
    "## 결정\n라운드 파일 하나로 왕복한다.\n\n"
    "## 설계\n설계 면제 티켓이다.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 실제 동작 검증\n\n"
    "## 참고\n- T-0675\n\n"
    "## 메모\n원본 메모.\n"
)


def _load_board():
    spec = importlib.util.spec_from_file_location("board_ticket_growth", BOARD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False, env=commit_env(),
    )


def _ticket_text(tid: str, status: str, *, tier: str | None = None) -> str:
    tier_line = f"tier: {tier}\n" if tier is not None else ""
    return (
        "---\n"
        f"id: {tid}\n"
        "title: 성장 테스트\n"
        f"status: {status}\n"
        "created: '2026-08-13'\n"
        "created_by: test\n"
        "claimed_by: null\n"
        "claimed_at: null\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: medium\n"
        "design: 'waived: 집중 테스트'\n"
        "tags: []\n"
        "custom:\n"
        "  nested:\n"
        "  - keep\n"
        f"{tier_line}"
        "---\n"
        + _VALID_BODY.format(tid=tid)
    )


def _make_board_git(root: Path, remote: Path) -> Path:
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board_dir / "tickets" / ".drafts").mkdir(parents=True)
    (board_dir / "areas.md").write_text("# Area Registry\n", encoding="utf-8")
    (board_dir / ".gitattributes").write_text(
        "areas.md merge=union\n*.md text eol=lf\n", encoding="utf-8")
    (board_dir / ".gitignore").write_text(
        "tickets/.drafts/\n", encoding="utf-8")
    assert _git(["init", "-q", "-b", "main"], board_dir).returncode == 0
    assert _git(["remote", "add", "origin", str(remote)], board_dir).returncode == 0
    assert _git(["add", "-A"], board_dir).returncode == 0
    assert _git(["commit", "-qm", "board init"], board_dir).returncode == 0
    assert _git(["push", "-q", "-u", "origin", "main"], board_dir).returncode == 0
    return board_dir


@pytest.fixture
def board_env(tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    assert _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path).returncode == 0
    board_dir = _make_board_git(tmp_path, bare)
    board = _load_board()
    anchor_board_module(board, tmp_path, monkeypatch)
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    return board, board_dir, bare


def _write_ticket(board_dir: Path, tid: str, status: str, *, tier: str | None = None) -> Path:
    target = ".drafts" if status == "draft" else status
    path = board_dir / "tickets" / target / f"{tid}-growth.md"
    path.write_text(_ticket_text(tid, "open" if status == "draft" else status, tier=tier),
                    encoding="utf-8")
    return path


def _seed_ticket(board_dir: Path, tid: str, status: str, *, tier: str | None = None) -> Path:
    path = _write_ticket(board_dir, tid, status, tier=tier)
    assert _git(["add", "-A"], board_dir).returncode == 0
    assert _git(["commit", "-qm", f"seed {tid}"], board_dir).returncode == 0
    assert _git(["push", "-q"], board_dir).returncode == 0
    return path


def _rounds_dir(board_dir: Path, tid: str) -> Path:
    """라운드 디렉터리 — 상태 디렉터리의 형제라 티켓 이동을 따라가지 않는다."""
    return board_dir / "tickets" / "rounds" / tid


def _round_names(board_dir: Path, tid: str) -> list[str]:
    directory = _rounds_dir(board_dir, tid)
    return sorted(item.name for item in directory.iterdir()) if directory.is_dir() else []


def _head(board_dir: Path) -> str:
    return _git(["rev-parse", "HEAD"], board_dir).stdout.strip()


def _remote_text(bare: Path, relpath: str) -> str:
    result = _git(["show", f"main:{relpath}"], bare)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _has_os_file_lock() -> bool:
    """board_lock이 실제 배타성을 제공하는 플랫폼인지(희귀 무락 fallback은 경합 단언 제외)."""
    try:
        import fcntl  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import msvcrt  # noqa: F401
        return True
    except ImportError:
        return False


@requires_git
def test_section_add_reserves_one_round_file_per_call_and_keeps_the_spec_bytes(board_env):
    """역할 3종·label override·같은 역할 재호출이 4개 라운드 파일 + 4개 commit 으로 누적된다.

    명세 파일은 **바이트가 그대로**여야 한다 — 라운드는 명세 밖 파일이고, 예약이 명세를 다시
    쓰면 lifecycle writer 와 같은 자리를 두 writer 가 만지는 옛 형상으로 되돌아간다.
    """
    board, board_dir, bare = board_env
    path = _seed_ticket(board_dir, "T-1001", "open")
    spec_before = path.read_bytes()
    calls = [
        ("architect", None, "설계"),
        ("developer", None, "구현 보충"),
        ("code-reviewer", None, "리뷰"),
        ("developer", "재구현", "재구현"),
    ]
    before_count = int(_git(["rev-list", "--count", "HEAD"], board_dir).stdout)
    for role, override, _label in calls:
        argv = ["section-add", "T-1001", "--role", role]
        if override:
            argv += ["--label", override]
        assert board.main(argv) == 0

    assert _round_names(board_dir, "T-1001") == [
        "01-architect.md", "02-developer.md", "03-code-reviewer.md", "04-developer.md",
    ]
    today = datetime.date.today().isoformat()
    for ordinal, (role, _override, label) in enumerate(calls, start=1):
        text = (_rounds_dir(board_dir, "T-1001")
                / f"{ordinal:02d}-{role}.md").read_text(encoding="utf-8")
        assert text.splitlines()[0] == f"## {label} ({role} · {today})"
        assert len(text.splitlines()) > 1, "역할 골격 본문이 비어 있음"
    assert path.read_bytes() == spec_before, "라운드 예약이 명세 파일을 건드렸다"
    assert int(_git(["rev-list", "--count", "HEAD"], board_dir).stdout) == before_count + 4
    assert _git(["log", "-4", "--format=%s"], board_dir).stdout.count("section-add T-1001") == 4
    remote = _remote_text(bare, "tickets/rounds/T-1001/04-developer.md")
    assert remote == (_rounds_dir(board_dir, "T-1001")
                      / "04-developer.md").read_text(encoding="utf-8")


@requires_git
def test_section_add_seed_is_the_shared_seam_render(board_env):
    """시드 bytes 의 단일 진실은 사이드카 seam 이다 (board 가 자기 골격을 따로 렌더하지 않는다)."""
    board, board_dir, _bare = board_env
    path = _seed_ticket(board_dir, "T-1014", "open")
    rounds = board._load_ticket_rounds()

    assert board.main(["section-add", "T-1014", "--role", "developer"]) == 0

    spec_text = path.read_text(encoding="utf-8")
    expected = rounds.render_round_seed(
        "developer", spec_text, today=datetime.date.today().isoformat())
    written = (_rounds_dir(board_dir, "T-1014") / "01-developer.md").read_text(
        encoding="utf-8")
    assert written == expected
    loaded = rounds.load_rounds(board.tickets_dir(), "T-1014", ticket_text=spec_text)
    assert [item.pending for item in loaded] == [True], "예약 직후 라운드는 산출 없음이어야 한다"


@requires_git
def test_section_add_refuses_a_developer_round_until_the_pm_judgment_is_written(
    board_env, capsys,
):
    """T-0805 채널 파리티 — cross 진입점(`section-add`)도 판정 미기입이면 같은 규칙으로 거부한다.

    거부는 예약 **전**이라 라운드 파일도 commit 도 남지 않고, traceback 이 아니라 기존 문언
    경로(`cannot section-add: …` rc=1)로 나온다.
    """
    board, board_dir, _bare = board_env
    _seed_ticket(board_dir, "T-1020", "claimed")
    rounds = board._load_ticket_rounds()
    pd = rounds._load_pm_delegate()
    review_payload = {
        "version": pd.PM_REVIEW_VERSION,
        "findings": [{
            "id": "F-001", "class": "implementation-defect", "severity": "must-fix",
            "authority": "[[T-0805]]", "evidence": "probe", "recommendation": "fix",
            "design_change": False,
        }],
        "confirmations": [],
    }
    review_round = _rounds_dir(board_dir, "T-1020") / "01-code-reviewer.md"
    review_round.parent.mkdir(parents=True, exist_ok=True)
    review_round.write_text(
        "## 리뷰 (code-reviewer · 2026-08-22)\n\n## must-fix\n- F-001\n\n"
        "## 판정\n판정: 반려 · finding 1건(must-fix 1건)\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n" + json.dumps(review_payload, ensure_ascii=False)
        + "\n```\n",
        encoding="utf-8",
    )
    head_before = _head(board_dir)

    assert board.main(["section-add", "T-1020", "--role", "developer"]) == 1

    error = capsys.readouterr().err
    assert error.startswith("cannot section-add: ")
    assert "[pending]" in error and "disposition-template" in error
    assert _round_names(board_dir, "T-1020") == ["01-code-reviewer.md"]
    assert _head(board_dir) == head_before


@requires_git
def test_show_assembles_the_spec_and_the_rounds_in_ordinal_order(board_env, capsys):
    """조회는 명세 전문 + 라운드 파일을 순번 순으로 조립하고 미회수 개수를 낸다."""
    board, board_dir, _bare = board_env
    path = _seed_ticket(board_dir, "T-1015", "claimed")
    assert board.main(["section-add", "T-1015", "--role", "developer"]) == 0
    assert board.main(["section-add", "T-1015", "--role", "code-reviewer"]) == 0
    harvested = _rounds_dir(board_dir, "T-1015") / "01-developer.md"
    harvested.write_text(
        harvested.read_text(encoding="utf-8") + "\n실제 구현 산출.\n", encoding="utf-8")

    assert board.main(["show", "T-1015"]) == 0

    out = capsys.readouterr().out
    assert path.read_text(encoding="utf-8") in out, "명세 전문이 조회에 없다"
    assert out.index("--- 01-developer ---") < out.index("--- 02-code-reviewer")
    assert "실제 구현 산출." in out
    assert "--- 02-code-reviewer (산출 없음) ---" in out
    assert "미회수 1건: 02-code-reviewer.md" in out


def _harvested_review_text(finding_id: str = "F-001") -> str:
    """회수된 리뷰 라운드 본문 — 자리표시자가 아닌 값이 든 `pm-review-v1` 블록 하나."""
    block = json.dumps({
        "version": 2,
        "findings": [{
            "id": finding_id, "class": "implementation-defect",
            "severity": "must-fix", "authority": "설계 §경계",
            "evidence": "probe rc=1", "recommendation": f"{finding_id} 수정",
            "design_change": False,
        }],
        "confirmations": [],
    }, ensure_ascii=False)
    return (
        "## 리뷰 (code-reviewer · 2026-01-03)\n\n"
        f"## must-fix\n- {finding_id}\n\n"
        "## 판정\n판정: 반려 · finding 1건(must-fix 1건)\n\n"
        f"```pm-review-v1\n{block}\n```\n"
    )


@requires_git
def test_show_keeps_marking_a_parallel_round_unharvested(board_env, capsys):
    """같은 역할 병렬 2라운드 — 01 회수 뒤에도 손대지 않은 02 는 미회수로 보인다.

    조회의 산출 없음 판정이 다른 라운드의 현재 내용에 기대면 02 가 산출 있는 것으로 읽혀
    `(산출 없음)`·`미회수` 가 사라지고 요약이 골격을 파싱한다.
    """
    board, board_dir, _bare = board_env
    _seed_ticket(board_dir, "T-1018", "claimed")
    for _ in range(2):
        assert board.main(["section-add", "T-1018", "--role", "code-reviewer"]) == 0
    second_before = (_rounds_dir(board_dir, "T-1018") / "02-code-reviewer.md").read_bytes()
    first = _rounds_dir(board_dir, "T-1018") / "01-code-reviewer.md"
    first.write_text(_harvested_review_text("F-001"), encoding="utf-8", newline="")

    assert board.main(["show", "T-1018"]) == 0

    out = capsys.readouterr().out
    assert (_rounds_dir(board_dir, "T-1018")
            / "02-code-reviewer.md").read_bytes() == second_before
    assert "--- 01-code-reviewer ---" in out and "(산출 없음)" not in out.split(
        "--- 02-code-reviewer")[0].split("--- 01-code-reviewer")[1]
    assert "--- 02-code-reviewer (산출 없음) ---" in out
    assert "미회수 1건: 02-code-reviewer.md" in out


@requires_git
def test_show_without_rounds_prints_only_the_spec(board_env, capsys):
    """라운드가 없으면 조회 출력은 종전 그대로다(빈 구분선·빈 요약 없음)."""
    board, board_dir, _bare = board_env
    _seed_ticket(board_dir, "T-1016", "open")

    assert board.main(["show", "T-1016"]) == 0

    out = capsys.readouterr().out
    assert "-- T-1016 (open/) --" in out
    tail = out.split("-- T-1016 (open/) --", 1)[1]
    assert "-- 라운드" not in tail and "미회수" not in tail
    assert "--- 0" not in tail, "라운드 구분선이 없는데 렌더됨"


@requires_git
def test_show_survives_a_malformed_round_file(board_env, capsys):
    """표시면은 조회를 막지 않는다 — 이름 문법 위반은 loud 경고이고 명세는 그대로 나온다."""
    board, board_dir, _bare = board_env
    _seed_ticket(board_dir, "T-1017", "open")
    directory = _rounds_dir(board_dir, "T-1017")
    directory.mkdir(parents=True)
    (directory / "notes.md").write_text("라운드가 아닌 파일\n", encoding="utf-8")

    assert board.main(["show", "T-1017"]) == 0

    captured = capsys.readouterr()
    assert "-- T-1017 (open/) --" in captured.out
    assert "라운드 조회 실패" in captured.err


@requires_git
def test_tier_cli_records_updates_and_preserves_unrelated_frontmatter(board_env):
    """claimed 티켓 tier 최초 기록→갱신을 실제 commit하고 기존 미지/중첩 field는 보존한다."""
    board, board_dir, bare = board_env
    path = _seed_ticket(board_dir, "T-1002", "claimed", tier="legacy-value")
    before_count = int(_git(["rev-list", "--count", "HEAD"], board_dir).stdout)

    assert board.main(["tier", "T-1002", "normal"]) == 0
    fm, _body = board.load_ticket(path)
    assert fm["tier"] == "normal"
    assert fm["custom"] == {"nested": ["keep"]}
    assert board.main(["tier", "T-1002", "hard"]) == 0
    fm, _body = board.load_ticket(path)
    assert fm["tier"] == "hard"
    assert fm["custom"] == {"nested": ["keep"]}
    assert int(_git(["rev-list", "--count", "HEAD"], board_dir).stdout) == before_count + 2
    remote = _remote_text(bare, "tickets/claimed/T-1002-growth.md")
    remote_fm, _ = board._parse_ticket_text(remote, "remote:claimed/T-1002")
    assert remote_fm["tier"] == "hard"


@requires_git
@pytest.mark.parametrize("status", ["open", "claimed"])
@pytest.mark.parametrize("command", ["section-add", "tier"])
def test_growth_commands_allow_each_active_state(board_env, status, command):
    """명시 allowlist의 open/claimed × 두 명령 전 조합이 실제 CLI dispatch로 성공한다."""
    board, board_dir, _bare = board_env
    path = _seed_ticket(board_dir, "T-1003", status)
    argv = (["section-add", "T-1003", "--role", "developer"]
            if command == "section-add" else ["tier", "T-1003", "pm-direct"])
    assert board.main(argv) == 0
    fm, _body = board.load_ticket(path)
    if command == "section-add":
        assert _round_names(board_dir, "T-1003") == ["01-developer.md"]
    else:
        assert fm["tier"] == "pm-direct"


@requires_git
@pytest.mark.parametrize("status", ["blocked", "done"])
@pytest.mark.parametrize("command", ["section-add", "tier"])
def test_growth_commands_reject_non_active_states_without_writes(board_env, capsys, status, command):
    """active allowlist 밖 blocked/done은 rc=1이며 파일과 Git HEAD를 바꾸지 않는다."""
    board, board_dir, _bare = board_env
    if status == "draft":
        path = _write_ticket(board_dir, "T-1004", status)
    else:
        path = _seed_ticket(board_dir, "T-1004", status)
    before_text = path.read_text(encoding="utf-8")
    before_head = _head(board_dir)
    argv = (["section-add", "T-1004", "--role", "architect"]
            if command == "section-add" else ["tier", "T-1004", "normal"])
    assert board.main(argv) == 1
    assert path.read_text(encoding="utf-8") == before_text
    assert _round_names(board_dir, "T-1004") == []
    assert _head(board_dir) == before_head
    error = capsys.readouterr().err
    assert "open/claimed 티켓" in error
    if command == "section-add":
        assert "draft×architect" in error
    else:
        assert "draft×architect" not in error


@requires_git
def test_draft_section_add_allows_only_architect_and_never_syncs(board_env, monkeypatch, capsys):
    """draft 의 라운드도 같은 규칙으로 예약하되 board-git 기록은 promote 가 소유한다."""
    board, board_dir, _bare = board_env
    path = _write_ticket(board_dir, "T-1011", "draft")
    before_head = _head(board_dir)
    before_spec = path.read_bytes()
    sync_calls = []
    monkeypatch.setattr(
        board, "_rounds_mutation_sync_paths",
        lambda *_args: sync_calls.append(_args) or True,
    )

    assert board.main(["section-add", "T-1011", "--role", "architect"]) == 0
    assert _round_names(board_dir, "T-1011") == ["01-architect.md"]
    assert path.read_bytes() == before_spec
    assert sync_calls == [] and _head(board_dir) == before_head
    assert "local draft; promote가 출하 소유" in capsys.readouterr().out


@requires_git
@pytest.mark.parametrize("role", ["developer", "code-reviewer"])
def test_draft_section_add_rejects_non_architect_before_write(board_env, capsys, role):
    board, board_dir, _bare = board_env
    path = _write_ticket(board_dir, "T-1012", "draft")
    before = path.read_bytes()
    assert board.main(["section-add", "T-1012", "--role", role]) == 1
    assert path.read_bytes() == before
    assert _round_names(board_dir, "T-1012") == []
    assert "draft×architect" in capsys.readouterr().err


@requires_git
def test_draft_tier_remains_rejected(board_env, capsys):
    board, board_dir, _bare = board_env
    path = _write_ticket(board_dir, "T-1013", "draft")
    before = path.read_bytes()
    assert board.main(["tier", "T-1013", "hard"]) == 1
    assert path.read_bytes() == before
    assert "open/claimed 티켓만 허용" in capsys.readouterr().err


@requires_git
def test_tier_unknown_value_is_rc1_before_lookup_or_write(board_env, capsys):
    """인식 불가 tier는 argparse rc=2가 아니라 명령 rc=1이며 대상/HEAD 불변이다."""
    board, board_dir, _bare = board_env
    path = _seed_ticket(board_dir, "T-1005", "open")
    before = path.read_bytes()
    before_head = _head(board_dir)
    assert board.main(["tier", "T-1005", "extreme"]) == 1
    assert path.read_bytes() == before
    assert _head(board_dir) == before_head
    assert "허용값" in capsys.readouterr().err


@requires_git
@pytest.mark.parametrize("command", ["section-add", "tier"])
def test_growth_commands_missing_ticket_are_rc2(board_env, command, capsys):
    """기존 정확 mutation lookup의 미존재 rc=2 계약을 두 명령이 공유한다."""
    board, _board_dir, _bare = board_env
    argv = (["section-add", "T-9999", "--role", "developer"]
            if command == "section-add" else ["tier", "T-9999", "normal"])
    assert board.main(argv) == 2
    assert _round_names(_board_dir, "T-9999") == []
    assert "ticket not found" in capsys.readouterr().err


@requires_git
@pytest.mark.parametrize("command", ["section-add", "tier"])
def test_growth_commands_commit_failure_preserves_local_mutation(board_env, capsys, command):
    """기존 best-effort 정책: pre-commit 실패면 rc=0·로컬 산출 보존·HEAD 불변·loud 보류."""
    board, board_dir, _bare = board_env
    path = _seed_ticket(board_dir, "T-1006", "open")
    hook = board_dir / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    before_head = _head(board_dir)
    argv = (["section-add", "T-1006", "--role", "code-reviewer"]
            if command == "section-add" else ["tier", "T-1006", "hard"])
    assert board.main(argv) == 0
    assert _head(board_dir) == before_head
    fm, _body = board.load_ticket(path)
    if command == "section-add":
        assert _round_names(board_dir, "T-1006") == ["01-code-reviewer.md"]
    else:
        assert fm["tier"] == "hard"
    captured = capsys.readouterr()
    assert "board local commit 실패" in captured.err
    assert "local-only/uncommitted" in captured.out


@requires_git
@pytest.mark.parametrize("lifecycle", ["block", "claim"])
def test_inplace_write_and_lifecycle_transition_share_one_ticket_lock(
        board_env, monkeypatch, lifecycle):
    """결정적 probe: in-place 티켓 write(tier) 중 lifecycle move/dump가 끼어들지 않는다.

    tier writer가 temp write를 끝내고 최종 replace 직전에 멈춘다. 이 시점에 block 또는 claim을
    시작한다. 공통 board_lock이면 lifecycle은 끝나지 못하고, write 완료 뒤 최신 body를 읽어
    이동한다. lock이 빠지면 lifecycle이 먼저 이동/dump한 뒤 replace가 옛 open path를 다시
    만들어 open+blocked/claimed 중복 또는 기록 유실을 즉시 재현한다.

    section-add 는 이 축의 대상이 아니다 — 명세 파일을 쓰지 않고 자기 라운드 파일만 만든다.
    """
    growth = "tier"
    if not _has_os_file_lock():
        pytest.skip("OS 배타락 부재 — board_lock 무락 fallback에서는 경합 단언 비적용")

    board, board_dir, _bare = board_env
    open_path = _write_ticket(board_dir, "T-1008", "open")

    # 경합 테스트 관심은 파일 트랜잭션이다. board-git/파생 render는 이미 별도 실제 Git 테스트가
    # 검증하며 여기서 섞으면 lock 획득 순서 대신 원격 왕복 타이밍을 재게 된다.
    monkeypatch.setattr(board, "_board_git_enabled", lambda: False)
    monkeypatch.setattr(board, "_rounds_mutation_sync_paths", lambda _message, _paths: True)
    monkeypatch.setattr(board, "_board_git_sync_best_effort", lambda _message, _paths: True)
    monkeypatch.setattr(board, "refresh_board", lambda: None)

    writer_entered = threading.Event()
    writer_release = threading.Event()
    lifecycle_done = threading.Event()
    results: dict[str, int] = {}
    errors: list[BaseException] = []

    if growth == "section-add":
        original_writer = board._atomic_write_text

        def paused_writer(path, text):
            writer_entered.set()
            assert writer_release.wait(timeout=10), "성장 write 재개 timeout"
            return original_writer(path, text)

        monkeypatch.setattr(board, "_atomic_write_text", paused_writer)
        growth_argv = ["section-add", "T-1008", "--role", "developer"]
    else:
        original_writer = board.dump_ticket_atomic

        def paused_writer(path, fm, body):
            writer_entered.set()
            assert writer_release.wait(timeout=10), "tier write 재개 timeout"
            return original_writer(path, fm, body)

        monkeypatch.setattr(board, "dump_ticket_atomic", paused_writer)
        growth_argv = ["tier", "T-1008", "hard"]

    lifecycle_argv = (
        ["block", "T-1008", "--reason", "경합 probe"] if lifecycle == "block" else
        ["claim", "T-1008", "--repo", "repo", "--slot", "1", "--user", "test"]
    )

    def run_growth():
        try:
            results["growth"] = board.main(growth_argv)
        except BaseException as exc:  # noqa: BLE001 — thread failure를 부모에서 단언.
            errors.append(exc)

    def run_lifecycle():
        try:
            results["lifecycle"] = board.main(lifecycle_argv)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            lifecycle_done.set()

    growth_thread = threading.Thread(target=run_growth, daemon=True)
    lifecycle_thread = threading.Thread(target=run_lifecycle, daemon=True)
    growth_thread.start()
    try:
        assert writer_entered.wait(timeout=10), "성장 writer가 pause 지점에 진입하지 못함"
        lifecycle_thread.start()
        assert not lifecycle_done.wait(timeout=0.2), (
            "lifecycle이 성장 write의 board_lock을 무시하고 먼저 완료 — TOCTOU 재현")
    finally:
        writer_release.set()
    growth_thread.join(timeout=10)
    lifecycle_thread.join(timeout=10)

    assert not growth_thread.is_alive() and not lifecycle_thread.is_alive(), "경합 thread timeout"
    assert not errors, errors
    assert results == {"growth": 0, "lifecycle": 0}
    assert not open_path.exists(), "옛 open path가 성장 replace로 부활함"
    destination = board_dir / "tickets" / ("blocked" if lifecycle == "block" else "claimed") / open_path.name
    assert destination.exists()
    assert len(list((board_dir / "tickets").glob(f"*/{open_path.name}"))) == 1
    fm, _body = board.load_ticket(destination)
    assert fm["status"] == ("blocked" if lifecycle == "block" else "claimed")
    assert fm["tier"] == "hard"


@requires_git
@pytest.mark.parametrize("growth", ["section-add", "tier"])
@pytest.mark.parametrize("push_outcome", ["success", "reject"])
def test_strict_claim_confirm_or_rollback_excludes_growth_until_finished(
        board_env, monkeypatch, growth, push_outcome):
    """재설계 핵심: 실제 push confirm/rollback 중 growth는 대기하고 종료 뒤 최신 경로에 쓴다."""
    if not _has_os_file_lock():
        pytest.skip("OS 배타락 부재 — board_lock 무락 fallback에서는 경합 단언 비적용")

    board, board_dir, bare = board_env
    open_path = _seed_ticket(board_dir, "T-1009", "open")
    head_before = _head(board_dir)
    claim_paused = threading.Event()
    allow_claim_finish = threading.Event()
    growth_done = threading.Event()
    results: dict[str, int] = {}
    errors: list[BaseException] = []

    real_push = board._board_git_push
    push_calls = {"count": 0}
    if push_outcome == "success":
        def paused_push():
            push_calls["count"] += 1
            result = real_push()
            if push_calls["count"] == 1:
                assert result.returncode == 0, result.stderr
                claim_paused.set()  # 원격 수락 뒤 confirm 반환 전.
                assert allow_claim_finish.wait(timeout=10), "claim confirm 재개 timeout"
            return result

        monkeypatch.setattr(board, "_board_git_push", paused_push)
    else:
        root = board_dir.parent.parent

        def rejecting_push():
            push_calls["count"] += 1
            if push_calls["count"] == 1:
                racer = root / "reject-racer"
                assert _git(["clone", "-q", str(bare), str(racer)], root).returncode == 0
                (racer / "remote-racer.txt").write_text("wins before claim push\n", encoding="utf-8")
                assert _git(["add", "-A"], racer).returncode == 0
                assert _git(["commit", "-qm", "remote racer"], racer).returncode == 0
                assert _git(["push", "-q", "origin", "main"], racer).returncode == 0
            return real_push()  # 첫 호출=non-FF 거부, growth 후속 호출=정상 push.

        real_rollback = board._board_git_claim_rollback
        rollback_calls = {"count": 0}

        def paused_rollback(*args, **kwargs):
            rollback_calls["count"] += 1
            if rollback_calls["count"] == 1:
                claim_paused.set()  # push 거부 판정 뒤 rollback 시작 직전.
                assert allow_claim_finish.wait(timeout=10), "claim rollback 재개 timeout"
            return real_rollback(*args, **kwargs)

        monkeypatch.setattr(board, "_board_git_push", rejecting_push)
        monkeypatch.setattr(board, "_board_git_claim_rollback", paused_rollback)

    growth_argv = (["section-add", "T-1009", "--role", "developer"]
                   if growth == "section-add" else ["tier", "T-1009", "hard"])
    claim_argv = ["claim", "T-1009", "--repo", "repo", "--slot", "1", "--user", "test"]

    def run_claim():
        try:
            results["claim"] = board.main(claim_argv)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def run_growth():
        try:
            results["growth"] = board.main(growth_argv)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            growth_done.set()

    claim_thread = threading.Thread(target=run_claim, daemon=True)
    growth_thread = threading.Thread(target=run_growth, daemon=True)
    claim_thread.start()
    try:
        assert claim_paused.wait(timeout=10), f"claim {push_outcome} pause에 진입하지 못함"
        growth_thread.start()
        assert not growth_done.wait(timeout=0.2), (
            f"growth가 claim {push_outcome} confirm/rollback 완료 전에 board_lock을 통과함")
    finally:
        allow_claim_finish.set()
    claim_thread.join(timeout=15)
    growth_thread.join(timeout=15)

    assert not claim_thread.is_alive() and not growth_thread.is_alive(), "strict claim 경합 timeout"
    assert not errors, errors
    expected_claim_rc = 0 if push_outcome == "success" else 1
    assert results == {"claim": expected_claim_rc, "growth": 0}
    final_status = "claimed" if push_outcome == "success" else "open"
    final_path = board_dir / "tickets" / final_status / open_path.name
    assert final_path.exists()
    assert len(list((board_dir / "tickets").glob(f"*/{open_path.name}"))) == 1
    fm, _body = board.load_ticket(final_path)
    assert fm["status"] == final_status
    if growth == "section-add":
        assert _round_names(board_dir, "T-1009") == ["01-developer.md"]
    else:
        assert fm["tier"] == "hard"

    # 최종 index/working tree는 clean. reject branch 이력에는 rollback된 claim commit이 없고,
    # remote racer + growth만 남는다(HEAD exact anchor는 racer pull/growth commit 때문에 달라진다).
    assert _git(["status", "--porcelain"], board_dir).stdout == ""
    subjects = _git(["log", "--format=%s", f"{head_before}..HEAD"], board_dir).stdout.splitlines()
    if push_outcome == "success":
        assert "claim" in subjects
    else:
        assert "claim" not in subjects
        assert "remote racer" in subjects
    growth_subject = "section-add T-1009 developer" if growth == "section-add" else "tier T-1009 hard"
    assert growth_subject in subjects
    if growth == "section-add":
        remote_round = _remote_text(bare, "tickets/rounds/T-1009/01-developer.md")
        assert remote_round.startswith("## 구현 보충 (developer · ")
    else:
        assert "tier: hard" in _remote_text(
            bare, f"tickets/{final_status}/{open_path.name}")


@requires_git
def test_identity_migration_and_inplace_write_share_snapshot_to_dump_lock(
        board_env, monkeypatch):
    """probe: migration stale snapshot dump 전에 tier write 가 끼어 그 값을 잃지 않는다."""
    growth = "tier"
    if not _has_os_file_lock():
        pytest.skip("OS 배타락 부재 — board_lock 무락 fallback에서는 경합 단언 비적용")

    board, board_dir, _bare = board_env
    path = _write_ticket(board_dir, "T-1010", "open")
    path.write_text(
        path.read_text(encoding="utf-8").replace("created_by: test\n", "created_by: null\n"),
        encoding="utf-8",
    )
    monkeypatch.setattr(board, "_rounds_mutation_sync_paths", lambda _message, _paths: True)

    migration_paused = threading.Event()
    allow_migration_dump = threading.Event()
    growth_done = threading.Event()
    results: dict[str, object] = {}
    errors: list[BaseException] = []
    real_atomic = board.dump_ticket_atomic
    calls = {"count": 0}

    def paused_first_atomic(target, fm, body):
        calls["count"] += 1
        if calls["count"] == 1:  # migration이 옛 body snapshot을 이미 읽은 뒤 최종 dump 직전.
            migration_paused.set()
            assert allow_migration_dump.wait(timeout=10), "migration dump 재개 timeout"
        return real_atomic(target, fm, body)

    monkeypatch.setattr(board, "dump_ticket_atomic", paused_first_atomic)
    growth_argv = (["section-add", "T-1010", "--role", "developer"]
                   if growth == "section-add" else ["tier", "T-1010", "hard"])

    def run_migration():
        try:
            results["migration"] = board._migrate_tickets_apply(
                "alice", "repo_1", ("open",))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def run_growth():
        try:
            results["growth"] = board.main(growth_argv)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            growth_done.set()

    migration_thread = threading.Thread(target=run_migration, daemon=True)
    growth_thread = threading.Thread(target=run_growth, daemon=True)
    migration_thread.start()
    try:
        assert migration_paused.wait(timeout=10), "migration이 stale snapshot pause에 진입하지 못함"
        growth_thread.start()
        assert not growth_done.wait(timeout=0.2), (
            "growth가 migration read→dump board_lock 사이에 끼어 stale overwrite 창이 열림")
    finally:
        allow_migration_dump.set()
    migration_thread.join(timeout=10)
    growth_thread.join(timeout=10)

    assert not migration_thread.is_alive() and not growth_thread.is_alive(), "migration 경합 timeout"
    assert not errors, errors
    assert results == {"migration": (1, True), "growth": 0}
    fm, _body = board.load_ticket(path)
    assert fm["created_by"] == "alice", "migration 결과 유실"
    assert fm["tier"] == "hard", "migration stale frontmatter가 tier를 덮음"


def test_ticket_writer_lock_inventory_and_claim_lock_order_are_ast_guarded():
    """sink 호출자를 독립 발견해 writer 누락/lock 순서/재진입을 미래 변경에서 red로 만든다."""
    tree = ast.parse(BOARD_PY.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def call_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id
        return None

    def lock_withs(fn: ast.AST, lock_name: str) -> list[ast.With]:
        return [node for node in ast.walk(fn) if isinstance(node, ast.With)
                and any(call_name(item.context_expr) == lock_name for item in node.items)]

    # 직접 파일 sink 호출자를 코드에서 독립 발견한다. 새 writer가 sink를 부르면 아래 reviewed
    # exception 또는 ticket writer 집합에 명시 분류하기 전까지 equality가 red다. helper 간접 호출은
    # 한 단계(`cmd_claim`→`_cmd_claim_locked`)만 아래에서 별도 결속한다.
    ticket_sinks = {"dump_ticket", "dump_ticket_atomic", "_atomic_write_text", "move_ticket"}
    direct_sink_callers = {
        name for name, fn in functions.items()
        if ticket_sinks.intersection(
            call_name(node) for node in ast.walk(fn) if isinstance(node, ast.Call)
        )
    }
    reviewed_non_ticket_exceptions = {
        "cmd_idea_new",       # ideas/ 전용, ticket lifecycle 대상 아님.
        "_transition_idea",   # ideas/ open→promoted|killed.
        "cmd_promote_scope",  # decisions/specs frontmatter writer.
        "repin_verified_at",  # current-truth 문서 freshness writer.
    }
    discovered_ticket_writers = direct_sink_callers - reviewed_non_ticket_exceptions
    # section-add 는 없다 — 라운드 예약은 명세 파일을 쓰지 않고 자기 라운드 파일만 만든다.
    # `_rounds_migration_rewrite_spec` 은 구 컨테이너 변환이 명세에서 절 블록을 걷어내는
    # in-place writer 다(`rounds migrate` 의 티켓 단계).
    expected_ticket_sink_callers = {
        "cmd_new", "cmd_promote", "_cmd_claim_locked", "cmd_complete", "cmd_block",
        "cmd_unclaim", "cmd_unblock", "cmd_tier", "_migrate_tickets_apply",
        "_rounds_migration_rewrite_spec",
    }
    assert direct_sink_callers & reviewed_non_ticket_exceptions == reviewed_non_ticket_exceptions, (
        "reviewed exception이 더 이상 sink caller가 아님 — inventory 분류 갱신 필요")
    assert discovered_ticket_writers == expected_ticket_sink_callers, (
        f"ticket sink caller 미분류: discovered={sorted(discovered_ticket_writers)}")

    ordinary_writers = {
        "cmd_new", "cmd_promote", "cmd_complete", "cmd_block", "cmd_unclaim", "cmd_unblock",
        "cmd_tier", "_migrate_tickets_apply", "_rounds_migration_rewrite_spec",
    }
    assert ordinary_writers | {"_cmd_claim_locked"} == discovered_ticket_writers
    for name in ordinary_writers:
        locks = lock_withs(functions[name], "board_lock")
        assert len(locks) == 1, f"{name} board_lock 경계 누락/중복: {len(locks)}"
        forbidden_inside = {
            "refresh_board", "_rounds_mutation_sync_paths", "_board_git_sync_best_effort"}
        assert not forbidden_inside.intersection(
            call_name(node) for node in ast.walk(locks[0]) if isinstance(node, ast.Call)
        ), f"{name} board_lock 안에서 refresh/board-git 재진입"

    # section-add 는 조회 구간만 board_lock 을 잡고, 채번+생성은 seam 에 **진입하지 않은** 락
    # 컨텍스트를 넘겨 그쪽이 잡는다(board_lock 은 재진입이 없다).
    section_add = functions["cmd_section_add"]
    assert len(lock_withs(section_add, "board_lock")) == 1, "조회 구간 락 경계 누락/중복"
    reserve = [
        node for node in ast.walk(section_add)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reserve_round"
    ]
    assert len(reserve) == 1, "라운드 예약 호출이 하나가 아님"
    assert any(
        kw.arg == "lock" and call_name(kw.value) == "board_lock"
        for kw in reserve[0].keywords
    ), "예약에 board_lock 컨텍스트를 넘기지 않음"
    assert not lock_withs(section_add, "board_lock")[0].items[0].optional_vars, \
        "조회 락은 이름을 묶지 않는다(구간 밖 재사용 금지)"

    claim = functions["cmd_claim"]
    git_locks = lock_withs(claim, "board_git_lock")
    assert len(git_locks) == 1
    nested_board_locks = lock_withs(git_locks[0], "board_lock")
    assert len(nested_board_locks) == 1, "claim lock 순서는 board_git_lock → board_lock 이어야 함"
    assert "_cmd_claim_locked" in {
        call_name(node) for node in ast.walk(nested_board_locks[0]) if isinstance(node, ast.Call)
    }

    locked_helper = functions["_cmd_claim_locked"]
    assert lock_withs(locked_helper, "board_lock") == []
    assert lock_withs(locked_helper, "board_git_lock") == []
    helper_calls = {
        call_name(node) for node in ast.walk(locked_helper) if isinstance(node, ast.Call)
    }
    assert {"_board_git_claim_prefetch", "_board_git_claim_confirm", "_refresh_board_locked"} \
        <= helper_calls
    assert "refresh_board" not in helper_calls


@requires_git
def test_promote_ships_the_draft_rounds_with_the_spec(board_env):
    """draft 의 라운드는 promote 커밋에 함께 실린다 — 예약 자체는 promote 게이트를 막지 않는다."""
    board, board_dir, bare = board_env
    path = _write_ticket(board_dir, "T-1007", "draft")

    assert board.main(["section-add", "T-1007", "--role", "architect"]) == 0
    assert _round_names(board_dir, "T-1007") == ["01-architect.md"]
    assert board.main(["promote", "T-1007"]) == 0

    promoted = board_dir / "tickets" / "open" / path.name
    assert promoted.exists() and not path.exists()
    tracked = _git(["ls-tree", "-r", "--name-only", "main"], bare).stdout
    assert "tickets/open/T-1007-growth.md" in tracked
    assert "tickets/rounds/T-1007/01-architect.md" in tracked, \
        "draft 라운드가 승격 커밋에서 빠짐(미커밋으로 눌러앉는다)"
    assert _remote_text(bare, "tickets/rounds/T-1007/01-architect.md") == (
        _rounds_dir(board_dir, "T-1007") / "01-architect.md").read_text(encoding="utf-8")


def test_parser_dispatch_keeps_existing_commands_and_classifies_growth_mutations():
    """신규 parser 배선이 기존 claim/promote/complete/lint fn dispatch를 바꾸지 않는다."""
    board = _load_board()
    existing = {
        ("claim", "T-1"): board.cmd_claim,
        ("promote", "T-1"): board.cmd_promote,
        ("complete", "T-1"): board.cmd_complete,
        ("lint",): board.cmd_lint,
    }
    parser = board.build_parser()
    for argv, expected_fn in existing.items():
        args = parser.parse_args(list(argv))
        assert args.fn is expected_fn
    assert parser.parse_args(
        ["section-add", "T-1", "--role", "developer"]).fn is board.cmd_section_add
    assert parser.parse_args(["tier", "T-1", "hard"]).fn is board.cmd_tier
    assert {"section-add", "tier"} <= board._MUTATION_SUBCOMMANDS
