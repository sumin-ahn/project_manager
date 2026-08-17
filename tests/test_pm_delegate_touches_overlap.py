"""병렬 위임 touches 교집합 경고 회귀 (T-0701).

같은 세션이 claim 중인 다른 ticket 과 대상 ticket 의 touches 가 겹치면, dev 위임은 띄우기
**전에** loud 경고하고 회수 시점에 "겹친 파일이 실제로 바뀌었나"를 1줄로 알린다. 차단은
하지 않는다(rc 불변) — 판단은 PM 몫이다.
"""
from __future__ import annotations

import importlib.util
import json as _json
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

OVERLAP_HEADER = "=== ⚠ 병렬 위임 touches 겹침 ==="
CHANGED_OVERLAP_PREFIX = "경고: 겹친 파일 변경됨"
TARGET_TICKET = "T-9701"
SESSION = "dev@example.invalid/main"
OTHER_SESSION = "other@example.invalid/main"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load("pm_delegate", TOOLS / "pm_delegate.py")


def _codex_stdout(reply: str = "DONE") -> str:
    return "\n".join([
        _json.dumps({"type": "thread.started", "thread_id": "th1"}),
        _json.dumps({"type": "item.completed",
                     "item": {"type": "agent_message", "text": reply}}),
        _json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ])


def _ok_result(reply: str = "완료"):
    return {"returncode": 0, "stdout": _codex_stdout(reply), "stderr": "",
            "timed_out": False}


class _WritingRun:
    """run_fn seam — 위임 산출물을 워크스페이스에 쓰고 canned 결과를 낸다."""

    def __init__(self, workspace: Path, writes=()):
        self.workspace = workspace
        self.writes = list(writes)
        self.calls: list[str] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append(harness)
        for relative in self.writes:
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("delegated output\n", encoding="utf-8")
        return _ok_result()


def _enabled_conf(**extra) -> dict:
    conf = {"delegate_enabled": "true",
            "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x"}
    conf.update(extra)
    return conf


def _run_main(pd, monkeypatch, argv, conf, run_fn=None):
    monkeypatch.setattr(pd, "local_config", lambda: conf)
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *a, **k: True)
    return pd.main(list(argv), run_fn=run_fn)


def _ticket_text(pd, ticket_id: str, *, status: str, claimed_by: str | None,
                 touches) -> str:
    touches_block = (
        "touches:\n" + "\n".join(f"- {item}" for item in touches) + "\n"
        if touches else "touches: []\n"
    )
    claim_line = f"claimed_by: {claimed_by}\n" if claimed_by else ""
    text = (
        "---\n"
        f"id: {ticket_id}\n"
        f"title: 교집합 e2e {ticket_id}\n"
        f"status: {status}\n"
        f"{claim_line}"
        f"{touches_block}"
        "---\n\n"
        f"# {ticket_id} — 교집합 e2e\n\n"
        "<!-- pm-ticket-section:start role=developer -->\n"
        "## 구현 보충 (developer · 2026-08-17)\n\n"
        "<!-- pm-ticket-section:end role=developer -->\n"
        "<!-- pm-ticket-section:start role=code-reviewer -->\n"
        "## 리뷰 (code-reviewer · 2026-08-17)\n\n"
        "<!-- pm-ticket-section:end role=code-reviewer -->\n"
        "<!-- pm-ticket-section:start role=architect -->\n"
        "## 설계 (architect · 2026-08-17)\n\n"
        "<!-- pm-ticket-section:end role=architect -->\n"
    )
    sealed, _changed = pd.backfill_ticket_seals(text)
    return sealed


def _overlap_workspace(
    tmp_path: Path, monkeypatch, pd, *, tickets, board_layout: str = "wiki",
) -> tuple[Path, Path]:
    """PM 홈 + `work/demo_1` git 워크스페이스 + claimed ticket 들을 실물로 세운다.

    `tickets` 는 (id, status, claimed_by, touches) 튜플 목록이다. board 형상은 legacy
    (`wiki/tickets`)와 board-git 분리(`board/tickets`) 둘 다 세울 수 있다.
    """
    pm_home = tmp_path / "pm_home"
    workspace = pm_home / "work" / "demo_1"
    pm_home.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=pm_home, check=True, capture_output=True)
    ignore = pm_home / ".project_manager" / ".gitignore"
    ignore.parent.mkdir()
    ignore.write_text(".local/\n", encoding="utf-8")
    (pm_home / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "seed.txt", ".project_manager/.gitignore"],
        cwd=pm_home, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=pm_home, check=True, capture_output=True,
    )
    workspace.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "overlap-slot", str(workspace)],
        cwd=pm_home, check=True, capture_output=True,
    )

    board_root = pm_home / ".project_manager" / (
        "board" if board_layout == "board" else "wiki"
    )
    for ticket_id, status, claimed_by, touches in tickets:
        directory = board_root / "tickets" / status
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{ticket_id}-overlap.md").write_text(
            _ticket_text(pd, ticket_id, status=status, claimed_by=claimed_by,
                         touches=touches),
            encoding="utf-8",
        )

    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        _json.dumps({"leases": [{"slot": "work/demo_1", "state": "leased"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(pd, "REPO", pm_home)
    monkeypatch.setattr(pd, "BOARD_PY", TOOLS / "board.py")
    prompt = workspace / "prompt.md"
    prompt.write_text("티켓 본문: 구현하라.", encoding="utf-8")
    return workspace, prompt


def _dev_argv(prompt: Path, workspace: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(workspace), "--ticket", TARGET_TICKET,
        "--output-dir", str(tmp_path / "raw"), *extra,
    ]


# ── (a) 같은 세션 claimed 겹침 → 경고·rc 0 ─────────────────────────────────

def test_same_session_claimed_overlap_warns_and_does_not_block(
    pd, monkeypatch, tmp_path, capsys,
):
    """같은 세션 claimed 2건과 겹치면 한 블록에 둘 다 나오고 rc 는 그대로 0이다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION,
             ["work/demo_1/src/shared.py", "work/demo_1/src/only_mine.py"]),
            ("T-9702", "claimed", SESSION, ["work/demo_1/src/shared.py"]),
            ("T-9703", "claimed", SESSION,
             ["work/demo_1/src/only_mine.py", "work/demo_1/docs/other.md"]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0 and fake.calls == ["codex"]        # never-block · 위임은 그대로 수행
    assert err.count(OVERLAP_HEADER) == 1             # 위임 1건 = 사전 경고 1블록
    assert "T-9702" in err and "T-9703" in err
    assert "work/demo_1/src/shared.py" in err
    assert "work/demo_1/src/only_mine.py" in err
    assert "work/demo_1/docs/other.md" not in err     # 겹치지 않는 선언은 안 싣는다
    assert "pm-config worktree add" in err            # 처방


# ── (b) 겹침 0 → 무경고 ────────────────────────────────────────────────────

def test_disjoint_claimed_tickets_have_no_warning(pd, monkeypatch, tmp_path, capsys):
    """같은 세션이라도 touches 가 disjoint 면 경고하지 않는다(오탐 0)."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["work/demo_1/src/mine.py"]),
            ("T-9702", "claimed", SESSION, ["work/demo_1/src/theirs.py"]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER not in err
    assert CHANGED_OVERLAP_PREFIX not in err


# ── (c) 다른 세션 claimed 는 계산 제외 ─────────────────────────────────────

def test_other_session_claim_is_excluded(pd, monkeypatch, tmp_path, capsys):
    """`claimed_by` 가 다르면 같은 파일을 선언해도 이 트리를 공유하지 않는다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["work/demo_1/src/shared.py"]),
            ("T-9702", "claimed", OTHER_SESSION, ["work/demo_1/src/shared.py"]),
            ("T-9703", "claimed", None, ["work/demo_1/src/shared.py"]),
            ("T-9704", "open", SESSION, ["work/demo_1/src/shared.py"]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER not in err
    for excluded in ("T-9702", "T-9703", "T-9704"):
        assert excluded not in err


# ── (d) 디렉토리 접두 겹침 (양방향) ────────────────────────────────────────

@pytest.mark.parametrize(
    ("target_touch", "other_touch", "expected_path"),
    [
        # 상대가 디렉토리를 선언한 축 (T-0701 발단 형상: `tests/` ⊃ `tests/x.py`)
        ("work/demo_1/tests/test_x.py", "work/demo_1/tests",
         "work/demo_1/tests/test_x.py"),
        # 대상이 디렉토리를 선언한 반대 축
        ("work/demo_1/tests", "work/demo_1/tests/test_x.py",
         "work/demo_1/tests/test_x.py"),
    ],
)
def test_directory_prefix_overlap_is_detected_both_ways(
    pd, monkeypatch, tmp_path, capsys, target_touch, other_touch, expected_path,
):
    """디렉토리 선언은 그 아래 파일을 덮는다 — 어느 쪽이 디렉토리든 겹침이다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, [target_touch]),
            ("T-9702", "claimed", SESSION, [other_touch]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER in err
    assert f"T-9702({other_touch})" in err            # 상대 선언을 그대로 보여준다
    assert expected_path in err                       # 공유 경로는 좁은 쪽


def test_windows_separator_touch_notation_is_normalized(pd, monkeypatch, tmp_path,
                                                        capsys):
    """`\\` 표기/`./` 접두 선언도 같은 경로로 접혀 겹침을 놓치지 않는다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["./work/demo_1/src/shared.py"]),
            ("T-9702", "claimed", SESSION, ["work\\demo_1\\src"]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER in err
    assert "work/demo_1/src/shared.py" in err


# ── (e) 회수 시점 — 겹친 파일이 실제로 변경됨 ──────────────────────────────

def test_changed_overlap_path_is_reported_after_delegation(
    pd, monkeypatch, tmp_path, capsys,
):
    """겹친 경로가 이 위임 시간창에 실제로 바뀌면 회수 시점에 1줄이 더 나온다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["work/demo_1/src"]),
            ("T-9702", "claimed", SESSION, ["work/demo_1/src/shared.py"]),
        ],
    )
    fake = _WritingRun(workspace, writes=["src/shared.py"])
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER in err                      # 사전 경고
    assert CHANGED_OVERLAP_PREFIX in err              # 사후 1줄
    assert "src/shared.py" in err
    assert "다른 dev 산출 공존 여부" in err


def test_untouched_overlap_path_has_no_after_line(pd, monkeypatch, tmp_path, capsys):
    """겹침 선언만 있고 그 경로를 안 바꿨으면 사후 줄은 안 나온다(사전 경고만)."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION,
             ["work/demo_1/src", "work/demo_1/docs"]),
            ("T-9702", "claimed", SESSION, ["work/demo_1/src/shared.py"]),
        ],
    )
    fake = _WritingRun(workspace, writes=["docs/note.md"])
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER in err
    assert CHANGED_OVERLAP_PREFIX not in err


# ── (f) dry-run 에도 사전 경고 ─────────────────────────────────────────────

def test_dry_run_still_shows_pre_warning(pd, monkeypatch, tmp_path, capsys):
    """미리보기에서 보는 것이 이 경고의 값이다 — 띄우기 전에 순차/슬롯 분리를 고른다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["work/demo_1/src/shared.py"]),
            ("T-9702", "claimed", SESSION, ["work/demo_1/src"]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(
        pd, monkeypatch,
        _dev_argv(prompt, workspace, tmp_path, "--dry-run"),
        _enabled_conf(), fake,
    )
    captured = capsys.readouterr()

    assert rc == 0 and fake.calls == []               # 미실행
    assert OVERLAP_HEADER in captured.err
    assert "T-9702" in captured.err
    assert CHANGED_OVERLAP_PREFIX not in captured.err  # 회수 축은 실행이 있어야 돈다


# ── board 형상 둘 · 역할 축 · 실패 비차단 ──────────────────────────────────

def test_board_git_split_layout_is_read(pd, monkeypatch, tmp_path, capsys):
    """board-git 분리 형상(`.project_manager/board/tickets/`)에서도 claimed 를 읽는다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["work/demo_1/src/shared.py"]),
            ("T-9702", "claimed", SESSION, ["work/demo_1/src"]),
        ],
        board_layout="board",
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER in err and "T-9702" in err


def test_read_only_role_is_not_subject_to_overlap_warning(
    pd, monkeypatch, tmp_path, capsys,
):
    """리뷰어는 격리 스냅샷을 읽기 전용으로 본다 — 겹침 경고 대상이 아니다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["work/demo_1/src/shared.py"]),
            ("T-9702", "claimed", SESSION, ["work/demo_1/src"]),
        ],
    )
    conf = _enabled_conf(**{"delegate.code-reviewer.harness": "codex",
                            "delegate.code-reviewer.model": "gpt-r"})
    fake = _WritingRun(workspace)
    rc = _run_main(
        pd, monkeypatch,
        ["--role", "code-reviewer", "--prompt-file", str(prompt),
         "--cwd", str(workspace), "--ticket", TARGET_TICKET,
         "--output-dir", str(tmp_path / "raw")],
        conf, fake,
    )
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER not in err


def test_overlap_calculation_failure_is_loud_but_nonblocking(
    pd, monkeypatch, tmp_path, capsys,
):
    """계산 실패를 조용히 삼키면 "겹침 0" 과 구분되지 않는다 — 1줄 남기고 계속한다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["work/demo_1/src/shared.py"]),
            ("T-9702", "claimed", SESSION, ["work/demo_1/src"]),
        ],
    )

    def _boom(*a, **k):
        raise pd.DelegateError("board 조회 불능")

    monkeypatch.setattr(pd, "claimed_touch_overlaps", _boom)
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0 and fake.calls == ["codex"]
    assert "교집합 계산 실패" in err
    assert OVERLAP_HEADER not in err


# ── 순수 계산 단위 ─────────────────────────────────────────────────────────

def test_overlap_paths_union_is_sorted_and_deduplicated(pd):
    """회수 축 입력은 정렬·중복 제거된 합집합이다(같은 경로 두 티켓에서 1회)."""
    overlaps = (
        pd.TicketTouchOverlap("T-9702", SESSION, ("tests",), ("tests/b.py",)),
        pd.TicketTouchOverlap("T-9703", SESSION, ("tests/b.py", "src"),
                              ("tests/b.py", "src/a.py")),
    )
    assert pd.overlap_touch_paths(overlaps) == ("src/a.py", "tests/b.py")


def test_touch_path_overlap_is_prefix_aware_on_segment_boundaries(pd):
    """접두 판정은 세그먼트 경계다 — `tests` 는 `tests_extra` 를 덮지 않는다."""
    assert pd._touch_paths_overlap("tests", "tests/x.py")
    assert pd._touch_paths_overlap("tests/x.py", "tests")
    assert pd._touch_paths_overlap("tests/x.py", "tests/x.py")
    assert not pd._touch_paths_overlap("tests", "tests_extra/x.py")
    assert not pd._touch_paths_overlap("tests/x.py", "tests/y.py")
