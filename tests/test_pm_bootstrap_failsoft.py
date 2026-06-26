"""pm_bootstrap fresh-clone fail-soft 단위 테스트 (T-0023).

빈 repo(커밋 0 — `pm_import --new` 직후·초기 커밋 전 clone)에서 `_collect_git()` 가
**크래시 없이 degrade** 하는지 본다. 실 git 비의존 — run_git_fn 을 DI 로 갈아끼워
결정론적으로 검증한다 (test_pm_bootstrap_tz 의 _load_module + PmBootstrap DI 패턴 재사용).

검증 축:
  - 빈 repo: rev-parse rc≠0 + symbolic-ref OK + log rc≠0 → no_commits=True·
    branch="main"·commits==[], sys.exit 하지 않음.
  - 렌더가 "초기 커밋 없음 — fresh clone" 표시.
  - 정상 repo: rev-parse OK → 기존 동작(no_commits=False) 보존.
  - 진짜 git repo 아님: symbolic-ref·status 전부 실패 → 명확히 sys.exit.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOOTSTRAP_PY = TOOLS / "pm_bootstrap.py"


def _load_module(name: str = "pm_bootstrap"):
    """pm_bootstrap 를 경로 로드한다 (도구는 패키지가 아니므로 importlib)."""
    spec = importlib.util.spec_from_file_location(name, BOOTSTRAP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_git_fn(responses: dict[str, tuple[int, str]]):
    """git argv 의 첫 토큰(서브커맨드)으로 (rc, out) 을 반환하는 fake run_git_fn."""
    def _fn(args: list[str]) -> tuple[int, str]:
        subcommand = args[0]
        if subcommand not in responses:
            raise AssertionError(f"예상치 못한 git 호출: {args}")
        return responses[subcommand]
    return _fn


_BOARD_LIST_OUTPUT = (
    "보드 목록 (T-NNNN)\n\n"
    "  [open   ] T-0010  어댑터  -  adapter\n"
)


def _make_board_fn(list_resp: tuple[int, str], lint_resp: tuple[int, str]):
    """board argv 의 첫 토큰(list/lint)으로 (rc, out) 을 반환하는 fake run_board_fn.

    `_collect_board` 가 lint 를 게이트(`["lint", "--gate"]`)로 부르는지도 함께 검증한다.
    """
    def _fn(args: list[str]) -> tuple[int, str]:
        if args[0] == "list":
            return list_resp
        if args[0] == "lint":
            assert args == ["lint", "--gate"], f"lint 는 --gate 로 호출해야 함: {args}"
            return lint_resp
        raise AssertionError(f"예상치 못한 board 호출: {args}")
    return _fn


# ── _collect_board lint 게이트 정합 (T-0038) ─────────────────────────────────

def test_collect_board_advisory_only_does_not_abort():
    """advisory-only 게이트 출력(rc=0) → _collect_board 가 abort 안 함 · summary 정확.

    `lint --gate` 는 advisory(unstable-ref-advice·status drift)만 있으면 rc=0 →
    부트스트랩이 sys.exit 하지 않고 통과하고, lint 요약은 헤더 제외 issue 수(2)다.
    """
    mod = _load_module()
    gate_out = (
        "⚠️  2 lint issue(s) (0 blocking 차단):\n"
        "    [unstable-ref-advice] T-0001: 슬러그 참조 권고\n"
        "    [status-done-accum] T-0002: status drift\n"
    )
    board_fn = _make_board_fn((0, _BOARD_LIST_OUTPUT), (0, gate_out))
    bootstrap = mod.PmBootstrap(run_board_fn=board_fn)

    # sys.exit 가 나면 SystemExit 으로 테스트 실패 — 발생하지 않아야 한다.
    board = bootstrap._collect_board()

    assert board["lint"] == "2 warnings"
    assert board["open_tickets"] == ["T-0010"]


def test_collect_board_blocking_lint_aborts(capsys):
    """차단 카테고리 게이트 출력(rc=1) → 기존대로 sys.exit(1) (회귀 보존)."""
    mod = _load_module()
    gate_out = (
        "⚠️  1 lint issue(s) (1 blocking 차단):\n"
        "  ✗ [dangling-wikilink] T-0003: 깨진 링크\n"
    )
    board_fn = _make_board_fn((0, _BOARD_LIST_OUTPUT), (1, gate_out))
    bootstrap = mod.PmBootstrap(run_board_fn=board_fn)

    with pytest.raises(SystemExit) as exc:
        bootstrap._collect_board()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "board.py lint 실패" in err


def test_collect_board_clean_passes():
    """clean 게이트 출력(rc=0) → lint='clean' 으로 통과."""
    mod = _load_module()
    board_fn = _make_board_fn((0, _BOARD_LIST_OUTPUT), (0, "✓ no lint issues\n"))
    bootstrap = mod.PmBootstrap(run_board_fn=board_fn)

    board = bootstrap._collect_board()
    assert board["lint"] == "clean"


# ── 기본 보드 뷰 = --mine (T-0164·ADR-0033 ④) ────────────────────────────────

def test_collect_board_default_view_is_mine():
    """_collect_board 가 list 를 `--mine` 렌즈로 부른다 (부트스트랩 기본뷰·T-0164).

    부트스트랩이 전체 contention 을 떠안지 않고 *내 것*만 surface 한다 — 솔로(user 미상)는
    board 의 graceful 폴백으로 현행과 사실상 동등(`--mine` 솔로 폴백·spike §2.D).
    """
    mod = _load_module()
    captured: list[list[str]] = []

    def _fn(args: list[str]) -> tuple[int, str]:
        captured.append(args)
        if args[0] == "list":
            return (0, _BOARD_LIST_OUTPUT)
        return (0, "✓ no lint issues\n")

    bootstrap = mod.PmBootstrap(run_board_fn=_fn)
    bootstrap._collect_board()

    list_calls = [a for a in captured if a[0] == "list"]
    assert list_calls == [["list", "--mine"]]


# ── 빈 repo: rev-parse rc≠0 + symbolic-ref OK + log rc≠0 → fail-soft ──────────

def test_empty_repo_degrades_without_exit():
    """빈 repo서 _collect_git() 가 no_commits=True·branch="main"·commits==[] 로 degrade.

    rev-parse 는 빈 repo서 rc≠0(stdout 에 "HEAD" 를 찍기도 하므로 rc 로만 판정),
    symbolic-ref 는 rc 0 으로 브랜치명을 준다, log 는 rc≠0("아직 커밋 없음").
    어느 단계도 sys.exit 하지 않아야 한다.
    """
    mod = _load_module()
    git_fn = _make_git_fn({
        # rev-parse: 빈 repo서 fatal + stdout "HEAD" + rc 128.
        "rev-parse": (128, "fatal: ambiguous argument 'HEAD'\nHEAD\n"),
        # symbolic-ref: 빈 repo서도 rc 0 으로 브랜치명 반환.
        "symbolic-ref": (0, "main\n"),
        # log: 빈 repo서 "아직 커밋 없음" rc 128.
        "log": (128, "fatal: your current branch 'main' does not have any commits yet\n"),
        # status: 빈 repo서도 rc 0 (clean).
        "status": (0, ""),
    })
    bootstrap = mod.PmBootstrap(run_git_fn=git_fn)

    # sys.exit 가 발생하면 SystemExit 으로 테스트 실패 — 발생하지 않아야 한다.
    git = bootstrap._collect_git()

    assert git["no_commits"] is True
    assert git["branch"] == "main"
    assert git["commits"] == []
    assert git["working_tree"] == "clean"


def test_empty_repo_render_shows_fresh_clone():
    """no_commits 면 markdown 렌더가 "초기 커밋 없음 — fresh clone" 을 표시한다."""
    mod = _load_module()
    git_fn = _make_git_fn({
        "rev-parse": (128, "HEAD\n"),
        "symbolic-ref": (0, "main\n"),
        "log": (128, "no commits yet\n"),
        "status": (0, ""),
    })
    bootstrap = mod.PmBootstrap(run_git_fn=git_fn)
    git = bootstrap._collect_git()

    board = {"counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0},
             "open_tickets": [], "lint": "clean"}
    markdown = bootstrap._build_markdown(board, None, git, None, "2026-06-14 00:00 KST")

    assert "초기 커밋 없음 — fresh clone" in markdown
    # 빈 repo면 "마지막 3 commit:" 헤더는 나오지 않는다.
    assert "마지막 3 commit" not in markdown


def test_empty_repo_json_carries_no_commits_flag():
    """JSON 렌더도 no_commits=True 를 그대로 실어 보낸다 (소비자 일관성)."""
    mod = _load_module()
    git_fn = _make_git_fn({
        "rev-parse": (128, "HEAD\n"),
        "symbolic-ref": (0, "main\n"),
        "log": (128, "no commits yet\n"),
        "status": (0, ""),
    })
    bootstrap = mod.PmBootstrap(run_git_fn=git_fn)
    git = bootstrap._collect_git()

    board = {"counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0},
             "open_tickets": [], "lint": "clean"}
    data = bootstrap._build_json(board, None, git, None, "2026-06-14 00:00 KST")

    assert data["git"]["no_commits"] is True
    assert data["git"]["commits"] == []


# ── 정상 repo: rev-parse OK → 기존 동작 보존 ──────────────────────────────────

def test_normal_repo_preserves_existing_behavior():
    """커밋이 있는 정상 repo는 symbolic-ref 폴백 없이 rev-parse 만으로 동작한다."""
    mod = _load_module()
    git_fn = _make_git_fn({
        "rev-parse": (0, "main\n"),
        "log": (0, "abc1234 first commit\ndef5678 second commit\n"),
        "status": (0, " M file.py\n"),
    })
    bootstrap = mod.PmBootstrap(run_git_fn=git_fn)
    git = bootstrap._collect_git()

    assert git["no_commits"] is False
    assert git["branch"] == "main"
    assert git["commits"] == [("abc1234", "first commit"), ("def5678", "second commit")]
    assert git["working_tree"] == "1 files modified"


# ── 진짜 git repo 아님: symbolic-ref·status 전부 실패 → 명확히 중단 ────────────

def test_non_git_repo_aborts_clearly():
    """git repo 자체가 아니면(rev-parse·symbolic-ref 둘 다 실패) sys.exit 로 중단.

    빈 repo 의 fail-soft 와 달리, 폴백 brnach 조회까지 실패하면 진짜 비-git 상태다.
    중단 메시지는 빈 repo 와 구분되도록 "git repo 아님" 을 담아야 한다.
    """
    mod = _load_module()
    git_fn = _make_git_fn({
        "rev-parse": (128, "fatal: not a git repository\n"),
        "symbolic-ref": (128, "fatal: not a git repository\n"),
    })
    bootstrap = mod.PmBootstrap(run_git_fn=git_fn)

    with pytest.raises(SystemExit) as exc:
        bootstrap._collect_git()
    assert exc.value.code == 1


def test_non_git_repo_status_failure_aborts(capsys):
    """rev-parse 정상이라도 status 가 실패하면(드문 비-git 신호) "git repo 아님" 중단."""
    mod = _load_module()
    git_fn = _make_git_fn({
        "rev-parse": (0, "main\n"),
        "log": (0, "abc1234 c\n"),
        "status": (128, "fatal: not a git repository\n"),
    })
    bootstrap = mod.PmBootstrap(run_git_fn=git_fn)

    with pytest.raises(SystemExit) as exc:
        bootstrap._collect_git()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "git repo 아님" in err
