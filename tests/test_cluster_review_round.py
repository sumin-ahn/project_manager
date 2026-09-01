"""묶음 리뷰 라운드 — 격리 스냅샷·프롬프트 조립·라운드 파일 N·백그라운드·기계 확인.

여기서 지키는 성질은 여섯이다.
  (1) 리뷰 입력은 `merge-base(통합 브랜치, 묶음 브랜치)` 이후 묶음 브랜치 변경 전부다.
  (2) 격리 스냅샷은 **기존 생성기**가 만들고(새 격리 경로 0) 그 사실 마커를 남긴다.
  (3) 프롬프트는 **기존 조립기**가 만든다 — 티켓 본문 N·변경 파일·검토 중점이 실값으로 실린다.
  (4) 위임 1회가 run-dir 하나에 티켓별 리뷰 자리를 깔고, 실행 root 는 그 스냅샷이며, 종료 시
      자리 전부를 회수하고 스냅샷을 정리한다.
  (5) `--background` 는 부작용 없이 분리 세션을 띄우고 pid 를 장부에 남긴다. 회수 판정은
      rc 가 아니라 라운드 회수 상태다.
  (6) final-fix 확인 입력은 read-only preflight하고, PM resolve가 기계 확인과 엄격히 이중 결속된
      PM-owned terminal 확인을 만든 뒤 게이트를 처분한다(reviewer 재호출 경로 없음).

hermetic 패턴은 `test_pm_delegate_rounds.py`(자기-정박 PM 홈 + 실 git)를 따른다.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from _test_exec import python_argv_command
REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "cluster-review",
    "GIT_AUTHOR_EMAIL": "cluster-review@test.invalid",
    "GIT_COMMITTER_NAME": "cluster-review",
    "GIT_COMMITTER_EMAIL": "cluster-review@test.invalid",
}

# 픽스처 시각은 실행 시점에서 만든다 — 소스에 날짜 리터럴을 박지 않는다(출하 위생).
_FIXTURE_DAY = datetime.date.today().isoformat()
_FIXTURE_STAMP = f"{_FIXTURE_DAY}T00:00:00+00:00"

_BASE_BRANCH = "task/main"
_CLUSTER = "C-wave"
_CLUSTER_BRANCH = "task/wave"
_MEMBERS = ("T-8001", "T-8002")

_FILLED_DESIGN_SECTION = (
    "## 설계\n"
    "- **경계 실측**: 묶음 리뷰 픽스처\n"
    "- **불변식**: run-dir 1 · 리뷰 자리 N\n"
    "- **표면 상한**: 스냅샷 1\n"
    "- **테스트 전략**: 입력·스냅샷·프롬프트·왕복\n"
)


def _load_tool(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load_tool("pm_delegate", "pm_delegate_cluster_review")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def _spec_text(ticket: str) -> str:
    return (
        "---\n"
        f"id: {ticket}\n"
        f"title: 묶음 리뷰 {ticket}\n"
        "status: claimed\n"
        f"created: '{_FIXTURE_DAY}'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        f"claimed_at: '{_FIXTURE_STAMP}'\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        f"touches:\n- {ticket.lower()}.py\n"
        "estimate: medium\n"
        "design: done\n"
        f"cluster: {_CLUSTER}\n"
        "tags: []\n"
        "---\n"
        f"# {ticket} — 묶음 리뷰 대상\n\n"
        f"## 목표\n{ticket} 목표 문장.\n\n" + _FILLED_DESIGN_SECTION
    )


def _fixture_board(pd, home: Path):
    board = pd._load_module_from_path(
        home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = home
    board.LOCAL_DIR = home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"
    board._rounds_mutation_sync_paths = lambda _message, _paths: True
    return board


def _write_cluster_ledger(home: Path, *, replans: list | None = None) -> Path:
    directory = home / ".project_manager" / "wiki" / "tickets" / "clusters"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_CLUSTER}.md"
    path.write_text(
        "---\n"
        f"id: {_CLUSTER}\n"
        "tickets:\n" + "".join(f"- {item}\n" for item in _MEMBERS)
        + f"base_branch: {_BASE_BRANCH}\n"
        f"branch: {_CLUSTER_BRANCH}\n"
        "spike: null\n"
        "budget:\n  architect: 1\n  developer_per_ticket: 1\n"
        "  code-reviewer: 1\n  fix: 1\n"
        + ("replans:\n" + "".join(
            f"- ts: '{item['ts']}'\n  reason: {item['reason']}\n"
            f"  from_ordinal: {item['from_ordinal']}\n" for item in replans)
           if replans else "replans: []\n")
        + "status: open\n"
        "---\n",
        encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def review_env(tmp_path, pd, monkeypatch):
    """cwd·PM 홈이 같은 자기-정박 트리 — 통합 브랜치와 묶음 브랜치를 실 git 으로 세운다."""
    home = tmp_path / "home"
    pm_tools = home / ".project_manager" / "tools"
    pm_tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, pm_tools / source.name)
    tickets = home / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (home / ".project_manager" / ".local").mkdir(parents=True)
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    assert _git(home, "init", "-q", "-b", _BASE_BRANCH).returncode == 0
    (home / ".project_manager" / ".gitignore").write_text(
        ".local/\n", encoding="utf-8", newline="\n")
    (home / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(home, "add", "seed.txt", ".project_manager/.gitignore").returncode == 0
    assert _git(home, "commit", "-qm", "base seed").returncode == 0
    # 묶음 브랜치: 멤버마다 파일 하나(리뷰 입력이 될 변경).
    assert _git(home, "checkout", "-q", "-b", _CLUSTER_BRANCH).returncode == 0
    for ticket in _MEMBERS:
        target = home / f"{ticket.lower()}.py"
        target.write_text(f"# {ticket} 구현\nvalue = 1\n", encoding="utf-8", newline="\n")
        assert _git(home, "add", "--", target.name).returncode == 0
        assert _git(home, "commit", "-qm", f"{ticket} 구현").returncode == 0
    for ticket in _MEMBERS:
        (tickets / f"{ticket}-review.md").write_text(
            _spec_text(ticket), encoding="utf-8", newline="\n")
    _write_cluster_ledger(home)
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, home),
    )
    monkeypatch.setattr(pd, "local_config", lambda *_a, **_k: {
        "delegate_enabled": "true",
        "delegate.code-reviewer.harness": "codex",
        "delegate.code-reviewer.model": "gpt-x",
    })
    return home, tickets


def _integration_advances(home: Path) -> str:
    """통합 브랜치가 앞서 간 상태를 만든다 — 그 커밋은 리뷰 입력이 아니어야 한다."""
    assert _git(home, "checkout", "-q", _BASE_BRANCH).returncode == 0
    (home / "integration.txt").write_text(
        "다른 묶음이 머지한 변경\n", encoding="utf-8", newline="\n")
    assert _git(home, "add", "--", "integration.txt").returncode == 0
    assert _git(home, "commit", "-qm", "다른 묶음 흡수").returncode == 0
    assert _git(home, "checkout", "-q", _CLUSTER_BRANCH).returncode == 0
    return _git(home, "rev-parse", _BASE_BRANCH).stdout.strip()


# ════════════════════════════════════════════════════════════════════════
# 리뷰 입력 — merge-base 기준
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_review_input_spans_the_merge_base_with_the_integration_tip(pd, review_env):
    home, _tickets = review_env
    _integration_advances(home)
    board = _fixture_board(pd, home)

    review = pd.cluster_review_input(board, _CLUSTER, repo=home)

    assert review.members == _MEMBERS
    assert review.branch == _CLUSTER_BRANCH and review.base_branch == _BASE_BRANCH
    assert review.merge_base == _git(
        home, "merge-base", _BASE_BRANCH, _CLUSTER_BRANCH).stdout.strip()
    # 이 묶음이 만든 변경만 입력이다 — 통합 브랜치가 흡수한 변경은 조상이라 빠진다.
    assert sorted(review.paths) == ["t-8001.py", "t-8002.py"]
    assert "integration.txt" not in review.diff
    assert "T-8001 구현" in review.diff and "T-8002 구현" in review.diff


@requires_git
def test_review_input_refuses_a_ledger_without_branch_coordinates(pd, review_env):
    home, _tickets = review_env
    ledger = home / ".project_manager" / "wiki" / "tickets" / "clusters" / f"{_CLUSTER}.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            f"branch: {_CLUSTER_BRANCH}\n", "branch:\n"),
        encoding="utf-8", newline="\n")
    board = _fixture_board(pd, home)

    with pytest.raises(pd.DelegateError, match="통합 브랜치 좌표가 없습니다"):
        pd.cluster_review_input(board, _CLUSTER, repo=home)


@requires_git
def test_review_input_refuses_an_empty_span(pd, review_env):
    """빈 diff 는 가짜 통과의 입력이다 — 스냅샷·호출 전에 멈춘다."""
    home, _tickets = review_env
    assert _git(home, "reset", "-q", "--hard", _BASE_BRANCH).returncode == 0
    board = _fixture_board(pd, home)

    with pytest.raises(pd.DelegateError, match="리뷰할 diff 가 없습니다"):
        pd.cluster_review_input(board, _CLUSTER, repo=home)


# ════════════════════════════════════════════════════════════════════════
# 격리 스냅샷 — 기존 생성기 재사용
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_snapshot_is_the_shared_generator_output_with_its_marker(pd, review_env):
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)
    gate_snapshot = pd._load_gate_snapshot()

    snapshot = pd.create_cluster_review_snapshot(home, review, board=board)
    try:
        # 저장소 밖이고, 생성기의 사실 마커가 있으며, 내용이 묶음 브랜치와 같다.
        assert not str(snapshot).startswith(str(home))
        assert gate_snapshot.is_snapshot(snapshot)
        assert gate_snapshot.snapshot_marker_path(snapshot).is_file()
        for path in review.paths:
            assert (snapshot / path).read_text(encoding="utf-8") == (
                home / path).read_text(encoding="utf-8")
    finally:
        pd.remove_cluster_review_snapshot(home, snapshot)

    # 정리는 등록까지 지운다 — 같은 자리를 다시 만들 수 있어야 한다.
    assert not snapshot.exists()
    worktrees = _git(home, "worktree", "list").stdout
    assert str(snapshot) not in worktrees


@requires_git
def test_snapshot_failure_blocks_before_any_review_run(pd, review_env):
    """생성 실패는 강등이 아니라 차단이다(리뷰 실행 전)."""
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)

    with pytest.raises(pd.DelegateError, match="격리 스냅샷 생성 실패"):
        pd.create_cluster_review_snapshot(
            home, review._replace(paths=("없는파일.py",)), board=board,
        )


@requires_git
def test_snapshot_input_is_bound_to_the_cluster_branch_tip(pd, review_env):
    """스냅샷 입력은 묶음 브랜치 tip 이다 — 다른 브랜치를 체크아웃한 트리는 거부한다.

    프롬프트 diff 는 장부 브랜치의 merge-base..tip 이고 스냅샷은 그 자리 트리의 파일이라,
    둘이 갈리면 모델이 읽는 코드와 판정 대상이 다른 코드가 된다.
    """
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    tip_text = (home / "t-8001.py").read_text(encoding="utf-8")

    # 역방향: 묶음 브랜치 tip 에 선 트리는 그대로 통과한다(정상 리뷰를 막지 않는다).
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)
    pd.assert_cluster_review_tree(board, review, repo=home)

    # 같은 경로에 **다른 내용**을 담은 브랜치로 옮긴다 — 스냅샷이 그 내용을 실으면 갈린다.
    assert _git(home, "checkout", "-q", "-b", "task/other", _BASE_BRANCH).returncode == 0
    (home / "t-8001.py").write_text(
        "# 다른 브랜치의 같은 경로\nvalue = 99\n", encoding="utf-8", newline="\n")
    assert _git(home, "add", "--", "t-8001.py").returncode == 0
    assert _git(home, "commit", "-qm", "다른 브랜치 구현").returncode == 0
    assert (home / "t-8001.py").read_text(encoding="utf-8") != tip_text

    with pytest.raises(pd.DelegateError, match="묶음 브랜치가 아닙니다"):
        pd.cluster_review_input(board, _CLUSTER, repo=home)
    with pytest.raises(pd.DelegateError, match="묶음 브랜치가 아닙니다"):
        pd.assert_cluster_review_tree(board, review, repo=home)


@requires_git
def test_uncommitted_change_in_a_reviewed_path_is_refused(pd, review_env):
    """브랜치 tip 과 다른 작업트리 내용은 리뷰 입력이 아니다(같은 브랜치여도 거부)."""
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)

    (home / "t-8002.py").write_text(
        "# 미커밋 수정\nvalue = 2\n", encoding="utf-8", newline="\n")

    # 입력 해소 표면과 결속 helper 가 같은 값으로 거부한다.
    with pytest.raises(pd.DelegateError, match="브랜치 tip 과 다릅니다") as caught:
        pd.cluster_review_input(board, _CLUSTER, repo=home)
    assert "t-8002.py" in str(caught.value)
    with pytest.raises(pd.DelegateError, match="브랜치 tip 과 다릅니다"):
        pd.assert_cluster_review_tree(board, review, repo=home)


def _head_count(home: Path) -> int:
    """그 트리의 HEAD 까지 커밋 수 — 회수가 커밋을 만들었는지 값으로 본다."""
    return int(_git(home, "rev-list", "--count", "HEAD").stdout.strip())


def _developer_round(pd, home: Path, ticket: str):
    """dev 라운드 하나를 준비하고 산출을 채운 좌표를 돌려준다."""
    plan = pd.prepare_ticket_copy(
        ticket=ticket, role="developer", cwd=home, pm_home=home,
    )
    command = pd._full_regression_command(home)
    text = plan.path.read_text(encoding="utf-8").replace(
        "- 커맨드: `<실행 커맨드>`", f"- 커맨드: `{command}`",
    ).replace(
        "- 결과: <rc=0 · A passed / 0 failed>", "- 결과: rc=0 · fixture green",
    )
    plan.path.write_text(
        text + "\n## 산출\n- 실측 값\n", encoding="utf-8", newline="")
    return plan


@requires_git
def test_developer_round_harvest_commits_the_slot_output(pd, review_env):
    """dev 라운드 회수가 그 슬롯에서 산출을 커밋한다 — 문안은 티켓 제목(손 커밋 0).

    커밋이 없으면 스냅샷 결속(브랜치 tip 대조)이 그 산출을 미커밋으로 거부해 묶음 리뷰가
    아예 서지 않는다 — 회수가 커밋 자리다.
    """
    home, _tickets = review_env
    ticket = _MEMBERS[0]
    before = _head_count(home)
    _reserve_prior_rounds(pd, home, ("architect",))
    plan = _developer_round(pd, home, ticket)
    (home / f"{ticket.lower()}.py").write_text(
        "# 구현 갱신\nvalue = 2\n", encoding="utf-8", newline="\n")
    contract_test = home / "tests" / "test_cluster_review_round.py"
    contract_test.parent.mkdir(parents=True, exist_ok=True)
    contract_test.write_text(
        "def test_cluster_output():\n    assert True\n",
        encoding="utf-8", newline="\n",
    )

    result = pd.harvest_ticket_copy(copy_path=plan.path, cwd=home, pm_home=home)

    assert result.changed is True
    assert _head_count(home) == before + 1
    assert _git(home, "log", "-1", "--format=%s").stdout.strip() == f"묶음 리뷰 {ticket}"
    assert _git(home, "status", "--porcelain").stdout.strip() == ""
    # 커밋된 산출은 이제 브랜치 tip 이라 리뷰 입력 결속을 그대로 통과한다.
    board = _fixture_board(pd, home)
    pd.assert_cluster_review_tree(
        board, pd.cluster_review_input(board, _CLUSTER, repo=home), repo=home)


@requires_git
def test_developer_round_harvest_without_the_architect_test_leaves_head_alone(
        pd, review_env, tmp_path):
    """architect 대상 테스트 변경이 없으면 회수·커밋 모두 거부한다.

    슬롯은 PM 홈과 다른 트리다 — board 쓰기가 슬롯을 더럽히지 않는 실 형상이라, 이 판정이
    보는 변경은 dev 산출뿐이다.
    """
    home, _tickets = review_env
    slot = tmp_path / "slot"
    slot.mkdir()
    assert _git(slot, "init", "-q", "-b", _CLUSTER_BRANCH).returncode == 0
    (slot / ".project_manager").mkdir()
    (slot / ".project_manager" / ".gitignore").write_text(
        ".local/\n", encoding="utf-8", newline="\n")
    (slot / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(slot, "add", "seed.txt", ".project_manager/.gitignore").returncode == 0
    assert _git(slot, "commit", "-qm", "slot seed").returncode == 0
    before = _head_count(slot)
    _reserve_prior_rounds(pd, home, ("architect",))
    plan = pd.prepare_ticket_copy(
        ticket=_MEMBERS[1], role="developer", cwd=slot, pm_home=home,
    )
    plan.path.write_text(
        plan.path.read_text(encoding="utf-8") + "\n## 산출\n- 실측 값\n",
        encoding="utf-8", newline="")

    with pytest.raises(pd.DelegateError, match="architect 필수 테스트 .* 추가·수정되지"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=home)

    assert _head_count(slot) == before


@requires_git
def test_developer_harvest_uses_markerless_linked_app_worktree_diff(
        pd, review_env, tmp_path):
    """ADR-0069 app slot의 `.git` 파일만으로 developer diff 루트를 고정한다."""
    home, _tickets = review_env
    ticket = _MEMBERS[0]
    app_source = tmp_path / "app-source"
    app_source.mkdir()
    assert _git(app_source, "init", "-q", "-b", "task/app-base").returncode == 0
    source_file = app_source / f"{ticket.lower()}.py"
    contract_test = app_source / "tests" / "test_cluster_review_round.py"
    contract_test.parent.mkdir()
    source_file.write_text("value = 1\n", encoding="utf-8", newline="\n")
    contract_test.write_text(
        "def test_app_output():\n    assert True\n",
        encoding="utf-8", newline="\n",
    )
    assert _git(app_source, "add", "--", source_file.name, "tests").returncode == 0
    assert _git(app_source, "commit", "-qm", "app seed").returncode == 0

    slot = home / "work" / "finance_1"
    slot.parent.mkdir()
    assert _git(
        app_source, "worktree", "add", "-q", "-b", _CLUSTER_BRANCH, str(slot),
    ).returncode == 0
    assert (slot / ".git").is_file()
    assert not (slot / ".project_manager").exists()

    base_rev = _git(slot, "rev-parse", "HEAD").stdout.strip()
    (slot / source_file.name).write_text(
        "value = 2\n", encoding="utf-8", newline="\n",
    )
    (slot / "tests" / contract_test.name).write_text(
        "def test_app_output():\n    assert 1 + 1 == 2\n",
        encoding="utf-8", newline="\n",
    )

    # harvest의 AT 결속 판정이 실제로 연결하는 두 seam을 함께 탄다.
    repo_root = pd._repo_root_for_cwd(slot)
    changed = pd._developer_round_changed_paths(repo_root, base_rev=base_rev)

    assert repo_root == slot.resolve()
    assert changed == frozenset({source_file.name, f"tests/{contract_test.name}"})


@requires_git
def test_developer_changed_paths_reports_unknown_base_revision(pd, tmp_path):
    """repo/base 해소 실패는 빈 diff로 축약되지 않는다."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0

    with pytest.raises(pd.DelegateError, match=r"git diff .* 실패"):
        pd._developer_round_changed_paths(repo, base_rev="not-a-real-revision")


@requires_git
def test_delegation_refuses_a_tree_that_is_not_the_cluster_branch(
        pd, review_env, capsys):
    """위임 표면도 같은 결속이다 — 스냅샷도 예약도 만들지 않고 rc 1 이다."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    assert _git(home, "checkout", "-q", _BASE_BRANCH).returncode == 0
    worktrees_before = _git(home, "worktree", "list").stdout
    capsys.readouterr()

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--output-dir", str(home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("결속 거부 뒤 스폰되면 안 됨"),
    )

    assert rc == 1
    assert "묶음 브랜치가 아닙니다" in capsys.readouterr().err
    assert pd.ticket_copy_records(home) == []
    assert _git(home, "worktree", "list").stdout == worktrees_before


# ════════════════════════════════════════════════════════════════════════
# 프롬프트 조립 — 기존 조립기 하나
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_prompt_carries_every_ticket_body_changed_files_focus_and_diff(pd, review_env):
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)
    specs = pd._cluster_member_specs(board, review.members)
    focus = "이번 라운드는 경계 판정의 값 대조를 중점으로 본다."

    prompt = pd.build_cluster_review_prompt(
        review, specs, snapshot=Path("/tmp/snap"), focus=focus,
    )

    assert f"리뷰 단위: {_CLUSTER}" in prompt
    assert str(Path("/tmp/snap")) in prompt
    for ticket in _MEMBERS:
        assert f"### 게이트 티켓 본문 ({ticket})" in prompt
        assert f"{ticket} 목표 문장." in prompt
    for path in review.paths:
        assert PurePosixPath(path).as_posix() in prompt
    assert "### PM 검토 중점" in prompt and focus in prompt
    assert "### 리뷰 대상 diff" in prompt and "```diff" in prompt
    assert review.merge_base[:12] in prompt


@requires_git
def test_prompt_leaves_the_block_requirement_to_the_round_seed(pd, review_env):
    """구조화 블록 요구는 라운드 파일 시드가 소유한다 — 두 벌이면 갈린다."""
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)
    specs = pd._cluster_member_specs(board, review.members)

    prompt = pd.build_cluster_review_prompt(review, specs, snapshot=None, focus=None)

    assert "### 구조화 판정 블록 (필수)" not in prompt
    assert "### PM 검토 중점" not in prompt
    # 추가 리뷰어 채널의 기존 조립은 그대로다(요구 블록 포함).
    external = pd._load_additional_reviewer()
    assert "### 구조화 판정 블록 (필수)" in external.build_prompt("diff", "본문")


# ════════════════════════════════════════════════════════════════════════
# 위임 1회 — run-dir 1 · 리뷰 자리 N · 실행 root = 스냅샷 · 회수
# ════════════════════════════════════════════════════════════════════════

def _codex_reply(text: str = "DONE") -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "th1"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": text}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ])


def _reviewer_block(pd, findings: list) -> str:
    payload = json.dumps(
        {"version": pd.PM_REVIEW_VERSION, "findings": findings, "confirmations": []},
        ensure_ascii=False, separators=(",", ":"),
    )
    mustfix = "\n".join(f"- {item['id']}" for item in findings) or "- 없음"
    verdict = "반려" if findings else "통과"
    return (
        f"\n## must-fix\n{mustfix}\n\n## 판정\n판정: {verdict} · finding {len(findings)}건\n\n"
        f"```{pd.PM_REVIEW_BLOCK}\n{payload}\n```\n"
    )


def _add_dir(argv: list[str]) -> Path:
    return Path(argv[argv.index("--add-dir") + 1])


def _reserve_prior_rounds(pd, home: Path, roles: tuple[str, ...]) -> None:
    """예산 수열의 선행 단계를 board 라운드로 깔아 둔다(리뷰가 그 다음 자리가 되게)."""
    import contextlib as _contextlib
    board = _fixture_board(pd, home)
    rounds_module = pd._load_ticket_rounds()
    for ticket in _MEMBERS:
        for role in roles:
            if role == "architect":
                test_payload = json.dumps({
                    "version": pd.ARCHITECT_TEST_VERSION,
                    "tests": [{
                        "id": "AT-001", "target": "tests/test_cluster_review_round.py",
                        "command": python_argv_command("--version"), "expected": "Python",
                        "negative": "계약 누락은 developer 준비를 차단한다",
                    }],
                }, ensure_ascii=False, separators=(",", ":"))
                content = (
                    "## 경계 실측\n- 묶음 리뷰\n\n## 불변식\n- 고정 수열\n\n"
                    "## 표면 상한\n- 추가 라운드 없음\n\n## 테스트 전략\n- 정상·실패\n\n"
                    f"```{pd.ARCHITECT_TEST_BLOCK}\n{test_payload}\n```\n"
                )
            else:
                content = f"## {role} 산출\n- {ticket} 실측\n"
            rounds_module.reserve_round(
                board.tickets_dir(), ticket, role,
                content=content,
                lock=_contextlib.nullcontext(),
            )


@requires_git
def test_delegation_lays_a_reviewer_seat_per_ticket_and_runs_in_the_snapshot(
        pd, review_env, capsys):
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    seen: dict = {}

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        seen["argv"] = list(argv)
        seen["prompt"] = stdin_text or ""
        run_dir = _add_dir(list(argv))
        seen["run_dir"] = run_dir
        seats = sorted(run_dir.glob("*/*-code-reviewer.md"))
        seen["seats"] = [str(item) for item in seats]
        for seat in seats:
            header = seat.read_text(encoding="utf-8").partition("\n")[0]
            seat.write_text(
                header + _reviewer_block(pd, []), encoding="utf-8", newline="")
        return {"returncode": 0, "stdout": _codex_reply(), "stderr": "",
                "timed_out": False}

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--output-dir", str(home / "raw")],
        run_fn=_run_fn,
    )

    assert rc == 0, capsys.readouterr()
    # run-dir 하나 안에 티켓마다 리뷰 자리 하나.
    assert len(seen["seats"]) == len(_MEMBERS)
    assert {Path(item).parent.name for item in seen["seats"]} == set(_MEMBERS)
    # 실행 root 는 확정된 격리 스냅샷이다(프롬프트가 그 절대경로를 싣는다).
    snapshot_line = [
        line for line in seen["prompt"].splitlines()
        if "리뷰 대상 트리(격리 스냅샷)" in line
    ]
    assert snapshot_line, seen["prompt"][:400]
    snapshot = Path(snapshot_line[0].split(": ", 1)[1].strip())
    assert not str(snapshot).startswith(str(home))
    # 실행이 끝나면 자리 전부가 회수되고(board 라운드에 산출) 스냅샷은 정리된다.
    for ticket in _MEMBERS:
        board_round = (
            home / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket
            / "03-code-reviewer.md"
        )
        assert pd.PM_REVIEW_BLOCK in board_round.read_text(encoding="utf-8")
    assert not snapshot.exists()
    assert not seen["run_dir"].exists()
    # 내부 라운드 장부는 게이트마다 한 항목이다(묶음 리뷰 = 게이트 N).
    ledger = json.loads((
        home / ".project_manager" / ".local" / pd.INTERNAL_REVIEW_LEDGER_NAME
    ).read_text(encoding="utf-8"))
    assert set(_MEMBERS) <= set(ledger)
    for ticket in _MEMBERS:
        rounds = ledger[ticket]["rounds"]
        assert rounds and rounds[-1]["verdict"] == 0


@requires_git
def test_delegation_refuses_when_the_cluster_budget_is_spent(pd, review_env, capsys):
    """묶음 예산은 리뷰 위임 표면에서도 같은 판정이다(우회 인자 없음)."""
    home, _tickets = review_env
    _reserve_prior_rounds(
        pd, home, ("architect", "developer", "code-reviewer", "developer"))
    capsys.readouterr()

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--output-dir", str(home / "raw")],
        run_fn=lambda *a, **k: pytest.fail("예산 거부 뒤 스폰되면 안 됨"),
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "고정 라운드 종료" in err and "추가 라운드 없이 정지·보고" in err


@requires_git
def test_cluster_and_ticket_targets_are_mutually_exclusive(pd, review_env, capsys):
    home, _tickets = review_env
    with pytest.raises(SystemExit):
        pd.main(["--role", "code-reviewer", "--cluster", _CLUSTER,
                 "--ticket", "T-8001", "--cwd", str(home)])
    assert "--ticket/--gate 와 병용" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        pd.main(["--role", "developer", "--cluster", _CLUSTER, "--cwd", str(home)])
    assert "code-reviewer 역할 전용" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        pd.main(["--role", "code-reviewer", "--cwd", str(home), "--ticket", "T-8001"])
    assert "--prompt-file 은 필수다" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# 백그라운드 — pid 장부 · 회수 분리
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_background_opens_a_run_row_and_makes_no_reservation(
        pd, review_env, monkeypatch, capsys):
    home, _tickets = review_env
    spawned: dict = {}

    class _FakeProcess:
        pid = 424242

    def _fake_spawn(argv, **kwargs):
        spawned["argv"] = list(argv)
        spawned["kwargs"] = kwargs
        return _FakeProcess()

    real_spawn = pd._spawn_background_cluster_review
    monkeypatch.setattr(
        pd, "_spawn_background_cluster_review",
        lambda argv, **kwargs: real_spawn(argv, spawn_fn=_fake_spawn, **kwargs),
    )

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--background"],
        run_fn=lambda *a, **k: pytest.fail("부모는 하네스를 부르지 않는다"),
    )

    assert rc == 0
    # 자식은 같은 CLI 를 `--background` 만 뺀 채 받는다.
    assert "--background" not in spawned["argv"]
    assert "--cluster" in spawned["argv"] and _CLUSTER in spawned["argv"]
    runs = pd.cluster_review_runs(home, _CLUSTER)
    assert len(runs) == 1
    assert runs[0]["pid"] == _FakeProcess.pid
    assert Path(runs[0]["log"]).parent.is_dir()
    # 실행 1건에 실행 키가 붙고, 아직 마감(rc)은 없다.
    assert runs[0]["run_id"] and runs[0]["rc"] is None
    # 자식은 **어느 장부의 어느 실행인가**를 env 로 받는다 — 그래야 자기 rc 로 마감할 수 있다.
    handoff = json.loads(spawned["kwargs"]["env"][pd.CLUSTER_REVIEW_RUN_ENV])
    assert handoff["run_id"] == runs[0]["run_id"]
    assert handoff["cluster"] == _CLUSTER
    assert Path(handoff["ledger"]) == pd._cluster_review_runs_ledger(home)
    # 부모는 부작용을 만들지 않는다 — 예약도 run-dir 도 없다.
    assert pd.ticket_copy_records(home) == []
    assert not (home / pd.TICKET_COPY_REL_ROOT / _CLUSTER).exists()
    assert "cluster wait" in capsys.readouterr().out


@requires_git
def test_background_does_not_spawn_when_the_run_ledger_cannot_be_written(
        pd, review_env, monkeypatch, capsys):
    """장부에 못 쓰면 자식을 아예 띄우지 않는다 — 기록 없는 자식은 회수 불가다."""
    home, _tickets = review_env
    ledger = pd._cluster_review_runs_ledger(home)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.mkdir()  # 같은 자리에 디렉터리 — append 가 실패하는 실 IO 형상.
    real_spawn = pd._spawn_background_cluster_review
    monkeypatch.setattr(
        pd, "_spawn_background_cluster_review",
        lambda argv, **kwargs: real_spawn(
            argv, spawn_fn=lambda *a, **k: pytest.fail("장부 실패 뒤 스폰 금지"), **kwargs,
        ),
    )
    capsys.readouterr()

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--background"],
        run_fn=lambda *a, **k: pytest.fail("부모는 하네스를 부르지 않는다"),
    )

    assert rc == 1
    assert "장부를 쓸 수 없어 띄우지 않습니다" in capsys.readouterr().err


@requires_git
def test_a_child_that_cannot_be_recorded_is_cleaned_up(
        pd, review_env, monkeypatch, capsys):
    """시작 행을 못 남기면 그 자식은 추적 불가다 — 정리하고 비성공을 낸다."""
    home, _tickets = review_env
    killed: list = []

    class _FakeProcess:
        pid = 424243

        def kill(self):
            killed.append(self.pid)

        def wait(self, timeout=None):
            return -9

    real_spawn = pd._spawn_background_cluster_review
    monkeypatch.setattr(
        pd, "_spawn_background_cluster_review",
        lambda argv, **kwargs: real_spawn(
            argv, spawn_fn=lambda *a, **k: _FakeProcess(), **kwargs,
        ),
    )
    monkeypatch.setattr(
        pd, "_append_cluster_review_run",
        lambda *_a, **_k: (_ for _ in ()).throw(pd.DelegateError("장부 기록 실패")),
    )
    capsys.readouterr()

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--background"],
        run_fn=lambda *a, **k: pytest.fail("부모는 하네스를 부르지 않는다"),
    )

    assert rc == 1 and killed == [_FakeProcess.pid]
    err = capsys.readouterr().err
    assert "시작 행 기록 실패" in err and "정리함(kill)" in err


def _record_run(pd, home: Path, *, run_id: str, pid: int, rc: int | None = None,
                started_at: str | None = None) -> str:
    """백그라운드 실행 1건을 장부에 쓴다(마감 rc 는 자식이 남기는 행이다)."""
    pd._append_cluster_review_run(home, {
        "cluster": _CLUSTER, "run_id": run_id, "pid": pid,
        "started_at": started_at or datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "log": str(home / f"{run_id}.log"), "cwd": str(home),
    })
    if rc is not None:
        pd._append_cluster_review_run(home, {
            "cluster": _CLUSTER, "run_id": run_id, "rc": rc,
            "ended_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
    return run_id


@requires_git
def test_wait_fails_when_the_child_ended_before_reserving_a_round(
        pd, review_env, capsys):
    """예약 전에 끝난 자식은 성공이 아니다 — 실 자식 프로세스의 rc 가 판정 입력이다.

    입력은 예산 소진이다(이 주기 4단계가 이미 예약돼 있어 자식은 준비 앞에서 거부된다).
    """
    home, _tickets = review_env
    _reserve_prior_rounds(
        pd, home, ("architect", "developer", "code-reviewer", "developer"))
    (home / ".project_manager" / "local.conf").write_text(
        "delegate.enabled=true\n"
        "delegate.code-reviewer.harness=codex\n"
        "delegate.code-reviewer.model=gpt-x\n",
        encoding="utf-8", newline="")
    capsys.readouterr()

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--output-dir", str(home / "raw"), "--background"],
        run_fn=lambda *a, **k: pytest.fail("부모는 하네스를 부르지 않는다"),
    )
    assert rc == 0, capsys.readouterr()

    rc = pd.cluster_wait(
        home, _CLUSTER, sleep_fn=lambda _seconds: time.sleep(0.05), budget_sec=120,
    )

    captured = capsys.readouterr()
    runs = pd.cluster_review_runs(home, _CLUSTER)
    log_text = Path(runs[-1]["log"]).read_text(encoding="utf-8", errors="replace")
    assert rc == 1, (captured, log_text)
    # 자식이 자기 rc 로 그 실행을 마감했고, 회수는 그 값을 그대로 판정에 쓴다.
    assert runs[-1]["rc"] == 1 and runs[-1]["ended_at"]
    assert "고정 라운드 종료" in log_text
    assert "rc=1" in captured.out and "백그라운드 묶음 리뷰 실패" in captured.err
    # 예약 자체가 없었다 — 미회수 0 을 성공으로 읽던 자리다.
    assert pd.ticket_copy_records(home) == []


@requires_git
def test_wait_ignores_unharvested_rows_from_another_run(pd, review_env, capsys):
    """다른(옛) 실행이 남긴 미회수 행은 이번 실행 판정에 들어오지 않는다."""
    home, _tickets = review_env
    stale = pd.prepare_cluster_copy(
        cluster=_CLUSTER, role="architect", cwd=home, pm_home=home,
    )
    assert [row["run_id"] for row in pd.ticket_copy_records(home, unharvested=True)]
    _record_run(pd, home, run_id="1" * 32, pid=os.getpid(), rc=0)
    capsys.readouterr()

    rc = pd.cluster_wait(
        home, _CLUSTER, sleep_fn=lambda _s: pytest.fail("마감된 실행은 대기하지 않는다"),
        clock_fn=lambda: 0.0,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "이번 실행 준비 0건 · 미회수 0" in out
    # 옛 잔여는 그대로 남아 있다(무시했을 뿐 지우지 않는다).
    assert stale.rounds[0].ticket in {
        row["ticket"] for row in pd.ticket_copy_records(home, unharvested=True)
    }


@requires_git
def test_wait_refuses_when_this_run_left_a_round_unharvested(pd, review_env, capsys):
    """이번 실행이 깐 자리가 남아 있으면 끝났다고 말하지 않는다."""
    home, _tickets = review_env
    _record_run(pd, home, run_id="2" * 32, pid=os.getpid(), rc=0)
    plan = pd.prepare_cluster_copy(
        cluster=_CLUSTER, role="architect", cwd=home, pm_home=home,
        owner_pid=os.getpid(),
    )
    capsys.readouterr()

    rc = pd.cluster_wait(home, _CLUSTER, clock_fn=lambda: 0.0)

    assert rc == 1
    err = capsys.readouterr().err
    assert "미회수 라운드 준비" in err and plan.rounds[0].ticket in err


@requires_git
def test_wait_refuses_a_child_that_died_without_closing_its_run(
        pd, review_env, capsys):
    """마감 없이 pid 가 사라진 실행은 실패 관측이다(성공으로 강등하지 않는다)."""
    home, _tickets = review_env
    _record_run(pd, home, run_id="3" * 32, pid=2 ** 30)
    capsys.readouterr()

    rc = pd.cluster_wait(home, _CLUSTER, clock_fn=lambda: 0.0)

    assert rc == 1
    assert "마감 없이 종료" in capsys.readouterr().err


@requires_git
def test_wait_without_a_record_is_loud(pd, review_env, capsys):
    home, _tickets = review_env
    assert pd.cluster_wait(home, _CLUSTER) == 1
    assert "백그라운드 실행 기록이 없습니다" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# 조회 — `review ... --cluster` 는 티켓 반복이다
# ════════════════════════════════════════════════════════════════════════

def _round(pd, ordinal: int, role: str, text: str):
    rounds_module = pd._load_ticket_rounds()
    return rounds_module.Round(
        ordinal=ordinal, role=role,
        path=Path(rounds_module.round_filename(ordinal, role)),
        text=text, pending=False,
    )


def _finding(fid: str, *, design_change: bool = False,
             classification: str = "implementation-defect") -> dict:
    return {
        "id": fid, "class": classification, "severity": "must-fix",
        "authority": "티켓 §목표", "evidence": f"{fid} 관측", "recommendation": f"{fid} 권고",
        "fix_contract": {
            "location": "src/example.py:1", "failure": f"{fid} 관측",
            "design": f"{fid} 수정", "test": f"{fid} 회귀",
            "command": python_argv_command("--version"), "expected": "Python",
        },
        "design_change": design_change,
    }


def _decision(fid: str, decision: str = "accepted", *, prerequisite: str = "") -> dict:
    return {
        "id": fid, "decision": decision, "reason": f"PM {decision} 근거",
        "scope": f"{fid} 허용 범위" if decision == "accepted" else "",
        "prerequisite": prerequisite,
    }


def _reviewer_round_text(pd, findings: list) -> str:
    return f"## 리뷰 (code-reviewer · {_FIXTURE_DAY})\n" + _reviewer_block(pd, findings)


def _disposition_block(pd, ordinal: int, rows: list) -> str:
    payload = json.dumps({
        "version": pd.PM_REVIEW_DISPOSITION_VERSION,
        "reviewer_role": "code-reviewer",
        "reviewer_ordinal": ordinal,
        "dispositions": rows,
    }, ensure_ascii=False, separators=(",", ":"))
    return f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n{payload}\n```\n"


def _verify_block(pd, rows: list) -> str:
    payload = json.dumps(
        {"version": pd.PM_REVIEW_VERIFY_VERSION, "verifications": rows},
        ensure_ascii=False, separators=(",", ":"),
    )
    return f"```{pd.PM_REVIEW_VERIFY_BLOCK}\n{payload}\n```\n"


def _verify_row(fid: str, *, command: str, expected: str, before: str = "옛 값",
                machine_verifiable: bool = True, reason: str = "") -> dict:
    return {
        "id": fid, "machine_verifiable": machine_verifiable, "command": command,
        "expected": expected, "before": before, "reason": reason,
    }


def _developer_round_text(pd, rows: list) -> str:
    return (
        f"## 구현 보충 (developer · {_FIXTURE_DAY})\n\n## 변경 파일\n- `x.py`: fix\n\n"
        "## 신규 테스트\n- 1개\n\n## 회귀\n- 커맨드: `pytest`\n- 결과: 1 passed\n\n"
        "## DoD evidence\n- 완료: 됨\n\n## 민감도\n- N/A\n\n" + _verify_block(pd, rows)
    )


def _accepted_ticket(pd, *, command: str = "echo hi", expected: str = "hi",
                     design_change: bool = False):
    """리뷰 라운드 1(F-001 accepted) + developer 라운드 2(verify 선언) 한 쌍."""
    reviewer = _round(pd, 1, "code-reviewer", _reviewer_round_text(
        pd, [_finding("F-001", design_change=design_change)]))
    spec = _disposition_block(pd, 1, [_decision(
        "F-001", prerequisite="[[T-0001]] 선행" if design_change else "")])
    developer = _round(pd, 2, "developer", _developer_round_text(
        pd, [_verify_row(
            "F-001", command=command, expected=expected,
            before="옛 값", machine_verifiable=True, reason="")]))
    return spec, [reviewer, developer]


def _pm_owned_ticket(
    pd, *, scope: str = "pm-owned: ADR·current-truth·local ledger",
    reason: str = "pm-owned",
):
    """F-003 형상 — PM-owned dual binding 외에는 terminal 확인 경로가 없다."""
    finding = _finding("F-003")
    finding["fix_contract"] = {
        "location": "/pm-home/.project_manager/.local/review_rounds.json",
        "failure": "legacy resolution 32건이 current ledger에 남음",
        "design": "PM이 같은 fix 단계에서 권위 문서와 local ledger를 일회 정리",
        "test": "PM 홈 절대경로 audit 결과와 current-truth 정렬을 종결 기록한다",
        "command": "python3 -c \"print(0)\"",
        "expected": "0",
    }
    reviewer = _round(
        pd, 1, "code-reviewer", _reviewer_round_text(pd, [finding]),
    )
    decision = _decision("F-003")
    decision["scope"] = scope
    spec = _disposition_block(pd, 1, [decision])
    developer = _round(pd, 2, "developer", _developer_round_text(pd, [
        _verify_row(
            "F-003", command="", expected="PM audit 완료 실값", before="",
            machine_verifiable=False, reason=reason,
        ),
    ]))
    return spec, [reviewer, developer]


def _partially_confirmed_pm_owned_ticket(pd):
    """같은 fix round의 machine 행만 먼저 기록되고 PM-owned 행이 빠진 재시도 입력."""
    pm_owned = _finding("F-003")
    pm_owned["fix_contract"] = {
        "location": "/pm-home/.project_manager/.local/review_rounds.json",
        "failure": "PM-owned current-truth 감사 미완",
        "design": "PM이 같은 fix 단계에서 권위 문서와 local ledger를 일회 정리",
        "test": "PM 홈 절대경로 audit 결과와 current-truth 정렬을 종결 기록한다",
        "command": "python3 -c \"print(0)\"",
        "expected": "0",
    }
    reviewer = _round(
        pd, 1, "code-reviewer",
        _reviewer_round_text(pd, [_finding("F-001"), pm_owned]),
    )
    pm_owned_decision = _decision("F-003")
    pm_owned_decision["scope"] = "pm-owned: ADR·current-truth·local ledger"
    spec = _disposition_block(
        pd, 1, [_decision("F-001"), pm_owned_decision],
    )
    developer = _round(pd, 2, "developer", _developer_round_text(pd, [
        _verify_row("F-001", command="echo hi", expected="hi"),
        _verify_row(
            "F-003", command="", expected="PM audit 완료 실값", before="",
            machine_verifiable=False, reason="pm-owned",
        ),
    ]))
    partial = pd.render_pm_review_confirmation_section((
        (2, pd.PMReviewMachineConfirmation(
            "F-001", "resolved", "echo hi", "rc=0\nhi", 2,
        )),
    ))
    return spec + "\n" + partial, [reviewer, developer]


@requires_git
def test_review_delta_over_a_cluster_concatenates_per_ticket_output(
        pd, review_env, monkeypatch, capsys):
    home, tickets = review_env
    board = _fixture_board(pd, home)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    rounds_module = pd._load_ticket_rounds()
    import contextlib
    for ticket in _MEMBERS:
        spec, rounds = _accepted_ticket(pd)  # noqa: PLW2901 — 티켓마다 같은 형상
        path = tickets / f"{ticket}-review.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n" + spec,
                        encoding="utf-8", newline="")
        for item in rounds:
            rounds_module.reserve_round(
                board.tickets_dir(), ticket, item.role, content=item.text,
                lock=contextlib.nullcontext(),
            )
    capsys.readouterr()

    rc = pd.main(["review", "delta", "--cluster", _CLUSTER])

    assert rc == 0
    out = capsys.readouterr().out
    for ticket in _MEMBERS:
        assert f"# {ticket}" in out
        assert f"## PM 승인 리뷰 delta — {ticket}" in out
    assert out.count("F-001") >= len(_MEMBERS)


# ════════════════════════════════════════════════════════════════════════
# 기계 확인 — 엔진이 실행하고 엔진이 기입한다
# ════════════════════════════════════════════════════════════════════════

def _stub_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return _run


def test_confirmation_runs_the_command_and_records_the_observed_value(pd, tmp_path):
    spec, rounds = _accepted_ticket(pd, command="echo hi", expected="hi")
    template = pd.pm_review_verify_template(spec, rounds)
    assert [source for source, _row in template.machine_rows] == [2]

    rows = pd.run_pm_review_confirmations(
        template, cwd=tmp_path, run_fn=_stub_run(0, "hi there\n"),
    )

    assert len(rows) == 1
    source_round, row = rows[0]
    assert source_round == 2 and row.id == "F-001"
    assert row.status == "resolved"
    assert "hi there" in row.observed and "rc=0" in row.observed


def test_confirmation_is_unresolved_when_expected_is_absent_or_rc_is_nonzero(
        pd, tmp_path):
    spec, rounds = _accepted_ticket(pd, command="echo hi", expected="hi")
    template = pd.pm_review_verify_template(spec, rounds)

    mismatch = pd.run_pm_review_confirmations(
        template, cwd=tmp_path, run_fn=_stub_run(0, "bye\n"))[0][1]
    failed = pd.run_pm_review_confirmations(
        template, cwd=tmp_path, run_fn=_stub_run(2, "hi\n", "boom"))[0][1]

    assert mismatch.status == "unresolved" and "bye" in mismatch.observed
    # 실행 실패는 통과로 접지 않는다 — 기대값이 보여도 rc 가 0 이 아니면 미해소다.
    assert failed.status == "unresolved" and "rc=2" in failed.observed


def test_confirmation_refuses_a_command_outside_the_safety_boundary(pd, tmp_path):
    """안전 경계는 그대로다 — 금지 토큰 커맨드는 실행 전에 거부한다(셸 해석 없음)."""
    def _never(argv, **kwargs):
        pytest.fail("금지 토큰 커맨드가 실행되면 안 됨")

    with pytest.raises(pd.PMReviewError, match="금지 토큰"):
        pd.run_pm_review_confirmation_command(
            "pytest -q | tee out.txt", cwd=tmp_path, expected="passed",
            run_fn=_never,
        )


def test_confirmation_command_runs_a_real_process(pd, tmp_path):
    """DI 없이도 실제로 도는 경로 하나 — 관측값이 실 출력이다."""
    command = f"{shlex.quote(sys.executable)} -c print(1234)"
    status, observed = pd.run_pm_review_confirmation_command(
        command, cwd=tmp_path, expected="1234",
    )
    assert status == "resolved" and "1234" in observed


def test_observed_excerpt_keeps_the_expected_value_visible(pd):
    """발췌가 기대값을 잘라내면 엔진이 적은 resolved 를 파서가 거부한다(왕복 정합)."""
    limit = pd.PM_REVIEW_CONFIRMATION_OBSERVED_LIMIT
    noisy = ("x" * limit) + "찾는값" + ("y" * limit)
    excerpt = pd._pm_review_observed_excerpt(noisy, "찾는값")
    assert "찾는값" in excerpt and len(excerpt) <= limit + 2


def test_written_confirmation_section_round_trips_through_the_parser(pd, tmp_path):
    """엔진이 기입한 확인 절이 파서를 통과하고 accepted 잔여를 닫는다."""
    spec, rounds = _accepted_ticket(pd, command="echo hi", expected="hi")
    template = pd.pm_review_verify_template(spec, rounds)
    rows = pd.run_pm_review_confirmations(
        template, cwd=tmp_path, run_fn=_stub_run(0, "hi\n"))

    section = pd.render_pm_review_confirmation_section(rows)

    assert section.startswith(pd.PM_REVIEW_CONFIRMATION_SECTION)
    assert "<관측값>" not in section and "<resolved|" not in section
    delta = pd.parse_pm_review_delta(spec + "\n" + section, rounds)
    assert delta.accepted == ()
    assert pd.pm_verified_evidence_problem(
        spec + "\n" + section, rounds,
        reviewer_role="code-reviewer", surface_floor=1,
    ) is None


def test_pm_owned_terminal_confirmation_is_generated_without_running_a_command(
    pd, tmp_path,
):
    spec, rounds = _pm_owned_ticket(pd)
    template = pd.pm_review_verify_template(spec, rounds)
    assert template.machine_rows == ()
    assert [(source, row.id) for source, row in template.pm_owned_rows] == [(2, "F-003")]
    assert pd.pm_verified_resolution_input_problem(
        spec, rounds, reviewer_role="code-reviewer", surface_floor=1,
    ) is None

    rows = pd.run_pm_review_confirmations(
        template, cwd=tmp_path,
        run_fn=lambda *_a, **_k: pytest.fail("PM-owned confirmation은 command를 실행하지 않는다"),
    )
    source, row = rows[0]
    assert (source, row.id, row.status) == (2, "F-003", "resolved")
    assert row.command == pd.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND
    assert row.observed == "PM audit 완료 실값"

    confirmed = spec + "\n" + pd.render_pm_review_confirmation_section(rows)
    assert pd.parse_pm_review_delta(confirmed, rounds).accepted == ()
    assert pd.pm_verified_evidence_problem(
        confirmed, rounds, reviewer_role="code-reviewer", surface_floor=1,
    ) is None


@pytest.mark.parametrize(("scope", "reason"), (
    ("F-003 developer 범위", "pm-owned"),
    ("pm-owned: ADR·current-truth·local ledger", "design-judgment"),
))
def test_pm_owned_terminal_confirmation_rejects_scope_verify_spoof(
    pd, scope, reason,
):
    spec, rounds = _pm_owned_ticket(pd, scope=scope, reason=reason)
    problem = pd.pm_verified_resolution_input_problem(
        spec, rounds, reviewer_role="code-reviewer", surface_floor=1,
    )
    assert problem is not None and (
        "PM-owned" in problem or "machine_verifiable=false" in problem
    )
    forged = json.dumps({
        "version": pd.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
        "round": 2,
        "confirmations": [{
            "id": "F-003", "status": "resolved",
            "command": pd.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND,
            "observed": "PM audit 완료 실값",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(pd.PMReviewError, match="strict pm-owned|PM-owned"):
        pd.parse_pm_review_delta(
            spec + f"\n```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n{forged}\n```\n",
            rounds,
        )


def test_arbitrary_false_verify_remains_reviewer_required_at_terminal_preflight(pd):
    spec, rounds = _pm_owned_ticket(
        pd, scope="F-003 developer 범위", reason="design-judgment",
    )
    template = pd.pm_review_verify_template(spec, rounds)
    assert template.pm_owned_rows == ()
    assert [row[0] for row in template.reviewer_required] == ["F-003"]
    problem = pd.pm_verified_resolution_input_problem(
        spec, rounds, reviewer_role="code-reviewer", surface_floor=1,
    )
    assert problem is not None and "machine_verifiable=false" in problem


@requires_git
def test_confirmation_is_appended_to_the_pm_area_of_the_spec(pd, review_env):
    home, tickets = review_env
    ticket = _MEMBERS[0]
    spec, rounds = _accepted_ticket(pd, command="echo hi", expected="hi")
    path = tickets / f"{ticket}-review.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + spec,
                    encoding="utf-8", newline="")
    template = pd.pm_review_verify_template(path.read_text(encoding="utf-8"), rounds)
    rows = pd.run_pm_review_confirmations(
        template, cwd=home, run_fn=_stub_run(0, "hi\n"))

    written = pd.append_pm_review_confirmation(
        home, ticket, pd.render_pm_review_confirmation_section(rows), rounds=rounds)

    assert written == path
    text = path.read_text(encoding="utf-8")
    assert pd.PM_REVIEW_CONFIRMATION_SECTION in text
    # 기입은 명세 PM 영역이다 — frontmatter 는 그대로다.
    assert text.startswith("---\n")
    assert pd.parse_pm_review_delta(text, rounds).accepted == ()


@requires_git
def test_confirmation_append_merges_missing_pm_owned_row_into_existing_round_idempotently(
    pd, review_env,
):
    """부분 실패 뒤 같은 fix round 재시도는 두 번째 block 대신 PM-owned 행만 병합한다."""
    home, tickets = review_env
    ticket = _MEMBERS[0]
    spec, rounds = _partially_confirmed_pm_owned_ticket(pd)
    path = tickets / f"{ticket}-review.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n" + spec,
        encoding="utf-8", newline="",
    )
    template = pd.pm_review_verify_template(path.read_text(encoding="utf-8"), rounds)
    rows = pd.run_pm_review_confirmations(
        template, cwd=home,
        run_fn=lambda *_a, **_k: pytest.fail("남은 행은 PM-owned라 command 실행이 없어야 한다"),
    )
    section = pd.render_pm_review_confirmation_section(rows)

    pd.append_pm_review_confirmation(home, ticket, section, rounds=rounds)
    first = path.read_bytes()
    pd.append_pm_review_confirmation(home, ticket, section, rounds=rounds)

    assert path.read_bytes() == first
    text = path.read_text(encoding="utf-8")
    blocks = [
        block for block in pd._pm_review_json_blocks(text)
        if block.kind == pd.PM_REVIEW_CONFIRMATION_BLOCK
    ]
    assert len(blocks) == 1 and blocks[0].value["round"] == 2
    assert [row["id"] for row in blocks[0].value["confirmations"]] == [
        "F-001", "F-003",
    ]
    assert pd.parse_pm_review_delta(text, rounds).accepted == ()


@pytest.mark.parametrize(("row", "message"), (
    (
        ("F-001", "resolved", "echo hi", "충돌한 관측"),
        "기존 행과 충돌",
    ),
    (
        ("F-003", "resolved", "echo spoof", "PM audit 완료 실값"),
        "strict PM-owned",
    ),
))
@requires_git
def test_confirmation_same_round_merge_rejects_conflict_and_spoof_atomically(
    pd, review_env, row, message,
):
    home, tickets = review_env
    ticket = _MEMBERS[0]
    spec, rounds = _partially_confirmed_pm_owned_ticket(pd)
    path = tickets / f"{ticket}-review.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n" + spec,
        encoding="utf-8", newline="",
    )
    before = path.read_bytes()
    incoming = pd.render_pm_review_confirmation_section((
        (2, pd.PMReviewMachineConfirmation(*row, 2)),
    ))

    with pytest.raises(pd.PMReviewError, match=message):
        pd.append_pm_review_confirmation(home, ticket, incoming, rounds=rounds)

    assert path.read_bytes() == before


# ════════════════════════════════════════════════════════════════════════
# 설계 축도 reviewer의 수정·테스트 계약으로 fix에서 종결한다
# ════════════════════════════════════════════════════════════════════════

def test_design_axis_uses_the_reviewer_machine_test_contract(pd):
    spec, rounds = _accepted_ticket(pd, design_change=True)
    template = pd.pm_review_verify_template(spec, rounds)

    assert [(source, row.id) for source, row in template.machine_rows] == [(2, "F-001")]
    assert template.reviewer_required == ()

    confirmation = json.dumps({
        "version": pd.PM_REVIEW_MACHINE_CONFIRMATION_VERSION, "round": 2,
        "confirmations": [{"id": "F-001", "status": "resolved",
                           "command": "echo hi", "observed": "hi"}],
    }, ensure_ascii=False, separators=(",", ":"))
    delta = pd.parse_pm_review_delta(
        spec + f"\n```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n{confirmation}\n```\n",
        rounds,
    )
    assert delta.accepted == ()


def test_design_axis_residual_closes_after_the_fix_test_confirmation(pd):
    spec, rounds = _accepted_ticket(pd, design_change=True)

    before = pd.pm_verified_evidence_problem(
        spec, rounds, reviewer_role="code-reviewer", surface_floor=1)
    confirmation = json.dumps({
        "version": pd.PM_REVIEW_MACHINE_CONFIRMATION_VERSION, "round": 2,
        "confirmations": [{"id": "F-001", "status": "resolved",
                           "command": "echo hi", "observed": "hi"}],
    }, ensure_ascii=False, separators=(",", ":"))
    after = pd.pm_verified_evidence_problem(
        spec + f"\n```{pd.PM_REVIEW_CONFIRMATION_BLOCK}\n{confirmation}\n```\n",
        rounds, reviewer_role="code-reviewer", surface_floor=1)

    assert before is not None and "F-001" in before
    assert after is None


def test_design_axis_replan_escape_hatch_is_removed(pd):
    assert not hasattr(pd, "_design_axis_replan_block")


@requires_git
def test_confirmation_tree_must_be_the_cluster_branch(pd, review_env):
    """다른 브랜치에서 잰 관측을 확인으로 적지 않는다."""
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    board._cluster_code_tree = lambda *_a, **_k: str(home)

    assert pd._cluster_confirmation_tree(board, _CLUSTER) == home

    assert _git(home, "checkout", "-q", _BASE_BRANCH).returncode == 0
    with pytest.raises(pd.DelegateError, match="통합 브랜치가 아닙니다"):
        pd._cluster_confirmation_tree(board, _CLUSTER)


@requires_git
def test_focus_file_is_read_as_prompt_source_and_missing_paths_fail_loud(
        pd, review_env):
    """검토 중점 파일은 그대로 프롬프트에 실리고, 없는 경로는 조용히 비지 않는다."""
    home, _tickets = review_env
    inside = home / "focus.md"
    inside.write_text("경계 판정을 중점으로 본다.\n", encoding="utf-8", newline="\n")

    assert "경계 판정" in pd._cluster_review_focus(inside, cwd=home, pm_home=home)
    with pytest.raises(pd.DelegateError, match="--focus 파일이 없습니다"):
        pd._cluster_review_focus(home / "없다.md", cwd=home, pm_home=home)


@requires_git
def test_codex_preflight_accepts_a_verified_snapshot_without_staged_changes(
        pd, review_env):
    """묶음 리뷰 입력은 미커밋 작업물이 아니라 브랜치 diff 다 — staged 요구가 성립하지 않는다.

    민감도: 스냅샷 마커를 지우면 같은 트리가 staged 0 으로 거부된다(가드가 실제로 그 사실을 본다).
    """
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)
    snapshot = pd.create_cluster_review_snapshot(home, review, board=board)
    marker = pd._load_gate_snapshot().snapshot_marker_path(snapshot)
    try:
        assert _git(snapshot, "diff", "--cached", "--name-only").stdout.strip() == ""
        pd._preflight_codex_read_exec_root(snapshot, role="code-reviewer")

        marker.rename(marker.with_suffix(".moved"))
        with pytest.raises(pd.DelegateError, match="staged 변경 0"):
            pd._preflight_codex_read_exec_root(snapshot, role="code-reviewer")
    finally:
        if marker.with_suffix(".moved").exists():
            marker.with_suffix(".moved").rename(marker)
        pd.remove_cluster_review_snapshot(home, snapshot)


def _rejected_reply(finding_id: str = "F-001") -> str:
    return (
        "판정: 반려\n\n"
        "**must-fix** (반드시 수정):\n"
        f"- {finding_id}\n\n"
        "**suggestion** (권장):\n- 없음\n"
    )


def _record_rejected_round(pd, gate: str) -> None:
    """그 게이트의 내부 라운드 장부에 잔여 must-fix 1 건을 남긴다(처분 대상 상태)."""
    budget = pd._reserve_internal_review_round(
        gate, wall_timeout_sec=60, target_rev="deadbeef",
        diff_fingerprint="fp-a",
    )
    trace = pd.InternalRoundTrace(budget)
    trace.start_attempt("raw-1")
    trace.finish_attempt({}, _rejected_reply())
    pd._finish_internal_review_round(budget, trace)


@requires_git
def test_resolve_cluster_executes_writes_and_declares_per_ticket(
        pd, review_env, monkeypatch, capsys):
    """엔진이 확인 커맨드를 실행해 기입하고 그 증거로 처분까지 낸다(티켓마다)."""
    home, tickets = review_env
    board = _fixture_board(pd, home)
    tree_identities = []

    def task_tree(identity=None):
        tree_identities.append(identity)
        return str(home)

    board._cluster_code_tree = task_tree
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", home)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    rounds_module = pd._load_ticket_rounds()
    import contextlib
    for ticket in _MEMBERS:
        spec, rounds = _accepted_ticket(pd, command="echo hi", expected="hi")
        path = tickets / f"{ticket}-review.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n" + spec,
                        encoding="utf-8", newline="")
        for item in rounds:
            rounds_module.reserve_round(
                board.tickets_dir(), ticket, item.role, content=item.text,
                lock=contextlib.nullcontext(),
            )
        _record_rejected_round(pd, ticket)
    capsys.readouterr()

    rc = pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified", "--task", "main"],
        run_fn=_stub_run(0, "hi\n"),
    )

    assert rc == 0, capsys.readouterr()
    assert len(tree_identities) == 1
    assert tree_identities[0].task == "main"
    assert tree_identities[0].repo is None and tree_identities[0].slot is None
    out = capsys.readouterr().out
    ledger = json.loads((
        home / ".project_manager" / ".local" / pd.INTERNAL_REVIEW_LEDGER_NAME
    ).read_text(encoding="utf-8"))
    for ticket in _MEMBERS:
        text = (tickets / f"{ticket}-review.md").read_text(encoding="utf-8")
        assert pd.PM_REVIEW_CONFIRMATION_SECTION in text
        assert '"observed":"rc=0' in text and "hi" in text
        assert ledger[ticket]["resolution"]["kind"] == "pm-verified"
        assert "기계 확인 1건 기입" in out and ticket in out


@requires_git
def test_resolve_cluster_closes_strict_pm_owned_findings_without_reviewer_or_command(
    pd, review_env, monkeypatch, capsys,
):
    home, tickets = review_env
    board = _fixture_board(pd, home)
    board._cluster_code_tree = lambda *_a, **_k: str(home)
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", home)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    rounds_module = pd._load_ticket_rounds()
    import contextlib
    for ticket in _MEMBERS:
        spec, rounds = _pm_owned_ticket(pd)
        path = tickets / f"{ticket}-review.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + spec,
            encoding="utf-8", newline="",
        )
        for item in rounds:
            rounds_module.reserve_round(
                board.tickets_dir(), ticket, item.role, content=item.text,
                lock=contextlib.nullcontext(),
            )
        _record_rejected_round(pd, ticket)
    capsys.readouterr()

    rc = pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified"],
        run_fn=lambda *_a, **_k: pytest.fail(
            "PM-owned terminal confirmation은 reviewer contract command를 실행하지 않는다"
        ),
    )

    captured = capsys.readouterr()
    assert rc == 0, captured
    ledger = json.loads((
        home / ".project_manager" / ".local" / pd.INTERNAL_REVIEW_LEDGER_NAME
    ).read_text(encoding="utf-8"))
    for ticket in _MEMBERS:
        text = (tickets / f"{ticket}-review.md").read_text(encoding="utf-8")
        assert f'"command":"{pd.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND}"' in text
        spec_text, rounds = pd._ticket_spec_and_rounds(board, ticket)
        assert pd.parse_pm_review_delta(spec_text, rounds).accepted == ()
        assert ledger[ticket]["resolution"]["kind"] == "pm-verified"
    assert "PM-owned terminal 확인 1건 기입" in captured.out


@requires_git
def test_resolve_retry_merges_pm_owned_row_into_partial_same_round_and_completes(
    pd, review_env, monkeypatch, capsys,
):
    """machine 확인 append 뒤 처분 실패를 재시도해도 같은 round block을 중복하지 않는다."""
    home, tickets = review_env
    board = _fixture_board(pd, home)
    board._cluster_code_tree = lambda *_a, **_k: str(home)
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", home)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    rounds_module = pd._load_ticket_rounds()
    import contextlib
    for ticket in _MEMBERS:
        spec, rounds = _partially_confirmed_pm_owned_ticket(pd)
        path = tickets / f"{ticket}-review.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + spec,
            encoding="utf-8", newline="",
        )
        for item in rounds:
            rounds_module.reserve_round(
                board.tickets_dir(), ticket, item.role, content=item.text,
                lock=contextlib.nullcontext(),
            )
        _record_rejected_round(pd, ticket)
    capsys.readouterr()

    no_command = lambda *_a, **_k: pytest.fail(
        "partial state의 남은 행은 PM-owned라 command를 실행하지 않는다"
    )
    assert pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified"], run_fn=no_command,
    ) == 0
    # 이미 처분된 상태의 재실행도 append/중복 없이 성공해야 한다.
    assert pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified"], run_fn=no_command,
    ) == 0

    for ticket in _MEMBERS:
        spec_text, rounds = pd._ticket_spec_and_rounds(board, ticket)
        blocks = [
            block for block in pd._pm_review_json_blocks(spec_text)
            if block.kind == pd.PM_REVIEW_CONFIRMATION_BLOCK
        ]
        assert len(blocks) == 1 and blocks[0].value["round"] == 2
        assert [row["id"] for row in blocks[0].value["confirmations"]] == [
            "F-001", "F-003",
        ]
        assert pd.parse_pm_review_delta(spec_text, rounds).accepted == ()


@requires_git
def test_resolve_cluster_is_scoped_to_the_machine_evidence_disposition(pd, capsys):
    """묶음 처분은 `--pm-verified` 전용이다 — 게이트별 판단은 묶음으로 접지 않는다."""
    with pytest.raises(SystemExit):
        pd._cmd_rounds(["resolve", "--cluster", _CLUSTER, "--into", "T-0001"])
    err = capsys.readouterr().err
    assert "--pm-verified" in err and "--into" not in err

    with pytest.raises(SystemExit):
        pd._cmd_rounds(["resolve", "--cluster", _CLUSTER, "--gate", "T-0001",
                        "--pm-verified"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_resolve_identity_rejects_task_and_repo_slot_mix(pd, capsys):
    with pytest.raises(SystemExit) as exc:
        pd._cmd_rounds([
            "resolve", "--cluster", _CLUSTER, "--pm-verified",
            "--task", "main", "--repo", "project_manager", "--slot", "1",
        ])
    assert "--task 는 독립 정체성" in str(exc.value)


@requires_git
def test_dry_run_previews_the_cluster_without_side_effects(pd, review_env, capsys):
    """미리보기는 부작용 0 이다 — 스냅샷도 예약도 만들지 않고 입력만 값으로 보인다."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    focus = home / "focus.md"
    focus.write_text("경계 판정의 값 대조를 중점으로 본다.\n",
                     encoding="utf-8", newline="\n")
    before = _git(home, "worktree", "list").stdout

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--focus", str(focus), "--dry-run"],
        run_fn=lambda *a, **k: pytest.fail("dry-run 은 스폰하지 않는다"),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert f"{_CLUSTER} 멤버 {len(_MEMBERS)}건" in out
    assert "변경 파일 2건" in out
    assert "dry-run 은 만들지 않는다" in out
    # PM 검토 중점은 파일 실값 그대로 프롬프트에 실린다(엔진은 자리만 만든다).
    assert "### PM 검토 중점" in out and "경계 판정의 값 대조" in out
    for ticket in _MEMBERS:
        assert f"### 게이트 티켓 본문 ({ticket})" in out
    assert _git(home, "worktree", "list").stdout == before
    assert pd.ticket_copy_records(home) == []


@requires_git
def test_a_refused_seat_harvest_does_not_pass_as_success(pd, review_env, capsys):
    """자리 하나가 회수 거부면 위임 rc 는 성공이 아니다(다른 자리 교체는 보존)."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        run_dir = _add_dir(list(argv))
        seats = sorted(run_dir.glob("*/*-code-reviewer.md"))
        # 첫 자리만 정상 산출 — 둘째는 시드 골격을 남긴 채 블록을 하나 더 붙여 거부시킨다.
        header = seats[0].read_text(encoding="utf-8").partition("\n")[0]
        seats[0].write_text(header + _reviewer_block(pd, []),
                            encoding="utf-8", newline="")
        seats[1].write_text(
            seats[1].read_text(encoding="utf-8") + _reviewer_block(pd, []),
            encoding="utf-8", newline="")
        return {"returncode": 0, "stdout": _codex_reply(), "stderr": "",
                "timed_out": False}

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--output-dir", str(home / "raw")],
        run_fn=_run_fn,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "ticket harvest 거부" in err and _MEMBERS[1] in err
    first = (
        home / ".project_manager" / "wiki" / "tickets" / "rounds" / _MEMBERS[0]
        / "03-code-reviewer.md"
    )
    assert pd.PM_REVIEW_BLOCK in first.read_text(encoding="utf-8")
