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
    conf = {"delegate.enabled": "true",
            "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x"}
    conf.update(extra)
    return conf


def _run_main(pd, monkeypatch, argv, conf, run_fn=None):
    monkeypatch.setattr(pd, "local_config", lambda: conf)
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *a, **k: True)
    return pd.main(list(argv), run_fn=run_fn)


def _ticket_text(pd, ticket_id: str, *, status: str, claimed_by: str | None,
                 touches) -> str:
    if isinstance(touches, str):
        touches_block = f"touches: {touches}\n"       # YAML 스칼라(형식 불명 축)
    elif touches:
        touches_block = "touches:\n" + "\n".join(f"- {item}" for item in touches) + "\n"
    else:
        touches_block = "touches: []\n"
    claim_line = f"claimed_by: {claimed_by}\n" if claimed_by else ""
    # T-0815 설계 근거 게이트(developer 시드 seam) 관심사 밖 — `done` + 설계 절로 미리
    # 해소한다(이 픽스처의 축은 touches 교집합 판정이지 설계 근거가 아니다).
    design_line = "design: done\n"
    text = (
        "---\n"
        f"id: {ticket_id}\n"
        f"title: 교집합 e2e {ticket_id}\n"
        f"status: {status}\n"
        f"{claim_line}"
        f"{touches_block}"
        f"{design_line}"
        "---\n\n"
        f"# {ticket_id} — 교집합 e2e\n\n## 목표\n교집합 판정 e2e.\n\n"
        "## 설계\n"
        "- **경계 실측**: 기계 테스트 픽스처\n"
        "- **불변식**: 이 파일의 축 밖\n"
        "- **표면 상한**: 픽스처 1건\n"
        "- **테스트 전략**: 정상·실패 경로\n"
    )
    sealed = text
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
        ticket_path = directory / f"{ticket_id}-overlap.md"
        text = _ticket_text(pd, ticket_id, status=status, claimed_by=claimed_by,
                            touches=touches)
        # 라운드는 명세 밖 파일이고 준비가 예약한다([[ADR-0090]]) — 명세만 세우면 된다.
        ticket_path.write_text(text, encoding="utf-8")

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
    assert "src/shared.py" in err                     # 공유 경로는 접힌 트리 좌표
    assert "src/only_mine.py" in err
    assert "docs/other.md" not in err                 # 겹치지 않는 선언은 안 싣는다
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
        ("work/demo_1/tests/test_x.py", "work/demo_1/tests", "tests/test_x.py"),
        # 대상이 디렉토리를 선언한 반대 축
        ("work/demo_1/tests", "work/demo_1/tests/test_x.py", "tests/test_x.py"),
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
    assert f"T-9702({other_touch})" in err            # 상대 **원 선언**을 그대로 보여준다
    assert f": {expected_path}" in err                # 공유 경로는 접힌 좌표의 좁은 쪽


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
    assert "T-9702(work\\demo_1\\src)" in err        # 원 선언은 board 표기 그대로
    assert ": src/shared.py" in err                   # 비교/표시 좌표는 접힌 트리 경로


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
    # codex code-reviewer preflight(`_preflight_codex_read_exec_root` — T-0844)의
    # staged-nonzero 요건 — workspace 자신의 독립 index에 변경 하나를 얹는다.
    (workspace / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "staged.txt"], cwd=workspace, check=True, capture_output=True,
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


# ── R1 delta: 좌표 규칙 하나 · 축 격리 · 표시 상한 · 형식 경계 ─────────────

def test_mixed_slot_prefix_and_bare_notation_overlaps(pd, monkeypatch, tmp_path,
                                                      capsys):
    """F-001: 한쪽은 slot 접두, 다른 쪽은 무접두로 같은 파일을 선언해도 겹침이다.

    실 보드에는 두 표기가 공존한다(같은 상대경로가 양쪽 표기로 선언된 사례 실측). 표기 정규화만
    하고 비교하면 이 조합에서 교집합이 조용히 0이 된다 — 사전 축도 사후 축과 같은 normalizer 로
    이 workspace 좌표까지 접은 뒤 비교해야 한다.
    """
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["src/shared.py"]),        # 무접두
            ("T-9702", "claimed", SESSION, ["work/demo_1/src"]),           # slot 접두
            ("T-9703", "claimed", SESSION, ["work/demo_1/src/shared.py"]), # 접두 + 같은 파일
        ],
    )
    fake = _WritingRun(workspace, writes=["src/shared.py"])
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER in err
    assert "T-9702" in err and "T-9703" in err
    assert ": src/shared.py" in err                   # 접힌 좌표 하나로 만난다
    assert CHANGED_OVERLAP_PREFIX in err              # 사후 축도 같은 좌표를 쓴다


def test_other_slot_declaration_is_not_a_false_overlap(pd, monkeypatch, tmp_path,
                                                       capsys):
    """다른 슬롯 선언(`work/other_2/...`)은 이 workspace 좌표로 접히지 않아 겹침이 아니다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["src/shared.py"]),
            ("T-9702", "claimed", SESSION, ["work/other_2/src/shared.py"]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER not in err
    assert "T-9702" not in err


class _AxisIsolationScope:
    """범위-밖 축은 성공하고 교집합 축만 터지는 판정 모듈 대역(F-002 재현)."""

    def __init__(self, scope_module):
        self._scope = scope_module

    def capture_worktree_state(self, *a, **k):
        return self._scope.WorktreeState(())

    def head_moved(self, *a, **k):
        return False

    def out_of_scope_changes(self, *a, **k):
        return ("stray/out.txt",)

    def format_warning(self, paths):
        return self._scope.format_warning(paths)

    def changed_status_paths(self, *a, **k):
        raise RuntimeError("좌표 해소 실패(교집합 축)")

    def committed_paths(self, *a, **k):
        raise RuntimeError("좌표 해소 실패(교집합 축)")


def test_out_of_scope_warning_survives_overlap_axis_failure(pd, tmp_path, capsys):
    """F-002: 새 축(교집합)의 예외가 **이미 확정된** 범위-밖 경고를 지우지 않는다."""
    scope_module = _load("delegate_scope_axis", TOOLS / "delegate_scope.py")
    audit = pd.ScopeAudit(
        _AxisIsolationScope(scope_module),
        ("src",),                       # touches 축 살아 있음
        scope_module.WorktreeState(()),
        tmp_path,
        tmp_path,
        (),                             # 어댑터 축은 등록 0(강등 경고만)
        ("src/shared.py",),             # 교집합 축 입력 — 계산은 터진다
    )

    pd.report_scope_audit(audit, "developer")
    err = capsys.readouterr().err

    assert "=== ⚠ 위임 범위 밖 변경 ===" in err       # 기존 신호 보존
    assert "stray/out.txt" in err
    assert "위임 범위 판정 실패" in err               # 새 축 실패도 loud
    assert CHANGED_OVERLAP_PREFIX not in err


def test_overlap_warning_caps_ticket_and_path_lists(pd):
    """F-004: 목록은 8건까지만 싣고 나머지는 건수로 접는다(gate_snapshot 표시 관례)."""
    overlaps = tuple(
        pd.TicketTouchOverlap(
            f"T-97{index:02d}", SESSION,
            tuple(f"src/decl_{index}_{n}.py" for n in range(10)),
            tuple(f"src/path_{index}_{n}.py" for n in range(10)),
        )
        for index in range(10)
    )
    warning = pd.format_touch_overlap_warning(TARGET_TICKET, overlaps)

    assert warning.count("∩") == 8                    # ticket 행 8개
    assert "… 외 2건의 ticket 이 더 겹칩니다" in warning
    assert "src/decl_0_7.py … 외 2건" in warning       # 선언 목록 상한
    assert "src/path_0_7.py … 외 2건" in warning       # 경로 목록 상한
    assert "src/path_0_8.py" not in warning


def test_changed_overlap_line_caps_paths(pd):
    """사후 1줄도 같은 상한을 쓴다 — 한 줄이 무한히 자라지 않는다."""
    line = pd.format_changed_overlap_warning(
        tuple(f"src/f{n}.py" for n in range(12))
    )
    assert line.endswith("… 외 4건")
    assert "src/f8.py" not in line


def test_scalar_touches_ticket_is_skipped_without_blocking(pd, monkeypatch, tmp_path,
                                                           capsys):
    """F-006: 형식 불명 touches(스칼라)는 판정 보류 — 경고 0·비차단."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, ["src/shared.py"]),
            ("T-9702", "claimed", SESSION, "src/shared.py"),   # YAML 스칼라 선언
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0 and fake.calls == ["codex"]
    assert OVERLAP_HEADER not in err
    assert "교집합 계산 실패" not in err               # 예외가 아니라 판정 보류다


def test_scalar_touches_on_target_holds_judgment(pd, monkeypatch, tmp_path, capsys):
    """대상 ticket 쪽이 스칼라여도 같은 축으로 접는다(경고 0·비차단)."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", SESSION, "src/shared.py"),
            ("T-9702", "claimed", SESSION, ["src/shared.py"]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0 and fake.calls == ["codex"]
    assert OVERLAP_HEADER not in err


def test_slot_only_claimed_by_is_the_same_session(pd, monkeypatch, tmp_path, capsys):
    """F-006: legacy 슬롯-only `claimed_by`(user 토큰 없음)도 값이 같으면 같은 세션이다."""
    workspace, prompt = _overlap_workspace(
        tmp_path, monkeypatch, pd,
        tickets=[
            (TARGET_TICKET, "claimed", "main", ["src/shared.py"]),
            ("T-9702", "claimed", "main", ["work/demo_1/src"]),
            ("T-9703", "claimed", "other-slot", ["src/shared.py"]),
        ],
    )
    fake = _WritingRun(workspace)
    rc = _run_main(pd, monkeypatch, _dev_argv(prompt, workspace, tmp_path),
                   _enabled_conf(), fake)
    err = capsys.readouterr().err

    assert rc == 0
    assert OVERLAP_HEADER in err
    assert "같은 세션(main)" in err
    assert "T-9702" in err
    assert "T-9703" not in err                        # 다른 슬롯은 여전히 제외


# ── 순수 계산 단위 ─────────────────────────────────────────────────────────

def test_overlap_paths_union_is_sorted_and_deduplicated(pd):
    """회수 축 입력은 정렬·중복 제거된 합집합이다(같은 경로 두 티켓에서 1회)."""
    overlaps = (
        pd.TicketTouchOverlap("T-9702", SESSION, ("tests",), ("tests/b.py",)),
        pd.TicketTouchOverlap("T-9703", SESSION, ("tests/b.py", "src"),
                              ("tests/b.py", "src/a.py")),
    )
    assert pd.overlap_touch_paths(overlaps) == ("src/a.py", "tests/b.py")


def test_scalar_touches_frontmatter_is_format_unknown(pd, tmp_path):
    """스칼라 `touches:` 는 board 형식 판정에서 None(판정 보류)이다 — 픽스처 의미 고정."""
    board = pd._load_board_for_repo(tmp_path)
    coordinates = pd._load_repo_coordinates()
    assert board._normalized_touches("src/pay.py") is None
    assert pd._touch_notations(
        "src/pay.py", board, coordinates, pm_root=tmp_path, workspace=tmp_path,
    ) is None
    assert pd._touch_notations(
        ["src/pay.py"], board, coordinates, pm_root=tmp_path, workspace=tmp_path,
    ) == (("src/pay.py", "src/pay.py"),)


def test_touch_path_overlap_is_prefix_aware_on_segment_boundaries(pd):
    """접두 판정은 세그먼트 경계다 — `tests` 는 `tests_extra` 를 덮지 않는다."""
    assert pd._touch_paths_overlap("tests", "tests/x.py")
    assert pd._touch_paths_overlap("tests/x.py", "tests")
    assert pd._touch_paths_overlap("tests/x.py", "tests/x.py")
    assert not pd._touch_paths_overlap("tests", "tests_extra/x.py")
    assert not pd._touch_paths_overlap("tests/x.py", "tests/y.py")
