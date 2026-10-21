"""묶음 리뷰 라운드 — 리뷰 입력·트리 검사(I1·I2)·프롬프트 조립·준비·돌려받기·기계 확인.

여기서 지키는 성질은 여섯이다.
  (1) 리뷰 입력은 `merge-base(통합 브랜치, 묶음 브랜치)` 이후 묶음 브랜치 변경 전부다.
  (2) 준비(I1)는 슬롯이 묶음 브랜치이고 작업 트리 변경이 0 일 때만 예약하고, 돌려받기(I2)는
      HEAD·작업 트리가 준비 시점과 같을 때만 board 라운드를 바꾼다.
  (3) 프롬프트는 **기존 조립기**가 만든다 — 티켓 본문 N·변경 파일·검토 중점이 실값으로 실린다.
  (4) 같은 하네스(`ticket prepare --role code-reviewer`)와 다른 하네스(`--role code-reviewer
      --cluster`) 두 전송이 같은 준비·조립·돌려받기를 쓴다.
  (5) 리뷰 잔여 판정 입력은 리뷰 라운드 파일과 명세 PM 영역뿐이다(내부 기록 파일 없음).
  (6) final-fix 확인 입력은 read-only preflight하고, PM resolve가 기계 확인과 엄격히 이중 결속된
      PM-owned terminal 확인을 기입한 뒤 완료 판정의 리뷰 잔여가 열린다(reviewer 재호출 경로 없음).

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
from pm_engine.infra import pm_paths
from pm_engine.domain import delegate_errors, review_form, review_state, round_rules
from pm_engine.infra import delegate_ledger, round_store
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
    # 실 운영의 리뷰 트리는 PM 홈과 다른 풀 슬롯이다 — 이 픽스처는 PM 홈 = 코드 트리라 PM 홈
    # 쓰기(엔진 사본·board)가 슬롯 작업 트리 변경으로 보이지 않게 ignore 한다.
    (home / ".project_manager" / ".gitignore").write_text(
        ".local/\ntools/\nwiki/\n", encoding="utf-8", newline="\n")
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
    assert not hasattr(review, "diff")
    prompt = pd.build_cluster_review_prompt(
        review, dict.fromkeys(review.members, "본문"), tree=home, focus=None,
    )
    command = shlex.split(prompt.split("```sh\n", 1)[1].split("\n```", 1)[0])
    patch = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8").stdout
    assert "integration.txt" not in patch
    assert "T-8001 구현" in patch and "T-8002 구현" in patch


@requires_git
def test_review_input_refuses_a_ledger_without_branch_coordinates(pd, review_env):
    home, _tickets = review_env
    ledger = home / ".project_manager" / "wiki" / "tickets" / "clusters" / f"{_CLUSTER}.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            f"branch: {_CLUSTER_BRANCH}\n", "branch:\n"),
        encoding="utf-8", newline="\n")
    board = _fixture_board(pd, home)

    with pytest.raises(delegate_errors.DelegateError, match="통합 브랜치 좌표가 없습니다"):
        pd.cluster_review_input(board, _CLUSTER, repo=home)


@requires_git
def test_review_input_refuses_an_empty_span(pd, review_env):
    """빈 diff 는 가짜 통과의 입력이다 — 스냅샷·호출 전에 멈춘다."""
    home, _tickets = review_env
    assert _git(home, "reset", "-q", "--hard", _BASE_BRANCH).returncode == 0
    board = _fixture_board(pd, home)

    with pytest.raises(delegate_errors.DelegateError, match="리뷰할 diff 가 없습니다"):
        pd.cluster_review_input(board, _CLUSTER, repo=home)


# ════════════════════════════════════════════════════════════════════════
# 준비 트리 검사(I1) — 묶음 브랜치 + 작업 트리 변경 0
# ════════════════════════════════════════════════════════════════════════

_SESSION_MARKER_KEYS = (
    "CODEX_THREAD_ID", "CODEX_CI", "CLAUDECODE", "CLAUDE_CONFIG_DIR",
    "OPENCODE", "OPENCODE_PID", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR",
)


def _native_review_harness(pd, monkeypatch) -> None:
    """같은 하네스 형상 — PM 하네스 표식과 리뷰 역할 하네스를 같은 값(claude)으로 고정한다."""
    for key in _SESSION_MARKER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(pd, "local_config", lambda *_a, **_k: {
        "delegate_enabled": "true",
        "delegate.code-reviewer.harness": "claude",
        "delegate.code-reviewer.model": "sonnet",
    })


def _machine_lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def _prepare_native_review(pd, home: Path, capsys, *extra: str) -> list[dict]:
    """`ticket prepare --cluster --role code-reviewer` 를 실 CLI 로 부르고 JSON 줄을 낸다."""
    capsys.readouterr()
    rc = pd._cmd_ticket([
        "prepare", "--cluster", _CLUSTER, "--role", "code-reviewer",
        "--cwd", str(home), *extra,
    ])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    return _machine_lines(captured.out)


def _board_review_round(home: Path, ticket: str, ordinal: int = 3) -> Path:
    return (
        home / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket
        / f"{ordinal:02d}-code-reviewer.md"
    )


def _write_seat(pd, seat: Path, findings: list) -> None:
    header = seat.read_text(encoding="utf-8").partition("\n")[0]
    seat.write_text(header + _reviewer_block(pd, findings), encoding="utf-8", newline="")


@requires_git
def test_review_prepare_refuses_a_dirty_slot(pd, review_env, monkeypatch, capsys):
    """AT-007 — 리뷰 경로 밖 추적 파일 수정·추적 안 된 파일이 있으면 준비가 예약 앞에서 멈춘다.

    변경 전에는 I1 이 리뷰 경로 drift 만 봐서 두 형상을 모두 통과시켰다.
    """
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _native_review_harness(pd, monkeypatch)
    for shape in ("tracked", "untracked"):
        if shape == "tracked":
            (home / "seed.txt").write_text("dirty\n", encoding="utf-8", newline="\n")
            expected = "seed.txt"
        else:
            assert _git(home, "checkout", "--", "seed.txt").returncode == 0
            (home / "scratch.txt").write_text("x\n", encoding="utf-8", newline="\n")
            expected = "scratch.txt"
        capsys.readouterr()

        rc = pd._cmd_ticket([
            "prepare", "--cluster", _CLUSTER, "--role", "code-reviewer", "--cwd", str(home),
        ])

        err = capsys.readouterr().err
        assert rc == 1, err
        assert "커밋되지 않은 변경" in err and expected in err
        assert delegate_ledger.ticket_copy_records(home) == []
        for ticket in _MEMBERS:
            assert not _board_review_round(home, ticket).exists()


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
        pd.cluster_review_input(board, _CLUSTER, repo=home), repo=home)


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

    with pytest.raises(delegate_errors.DelegateError, match="architect 필수 테스트 .* 추가·수정되지"):
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

    with pytest.raises(delegate_errors.DelegateError, match=r"git diff .* 실패"):
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
         "--output-dir", str(home.parent / "raw")],
        run_fn=lambda *a, **k: pytest.fail("결속 거부 뒤 스폰되면 안 됨"),
    )

    assert rc == 1
    assert "묶음 브랜치가 아닙니다" in capsys.readouterr().err
    assert delegate_ledger.ticket_copy_records(home) == []
    assert _git(home, "worktree", "list").stdout == worktrees_before


# ════════════════════════════════════════════════════════════════════════
# 프롬프트 조립 — 기존 조립기 하나
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_prompt_carries_every_ticket_body_changed_files_focus_and_diff(pd, review_env):
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)
    specs = pd._cluster_member_specs(pm_paths.home_at(home), review.members)
    focus = "이번 라운드는 경계 판정의 값 대조를 중점으로 본다."

    prompt = pd.build_cluster_review_prompt(
        review, specs, tree=home, focus=focus,
    )

    assert f"리뷰 단위: {_CLUSTER}" in prompt
    assert f"리뷰 대상 트리: {home} (브랜치 {_CLUSTER_BRANCH})" in prompt
    assert f"git -C {home}" in prompt and "돌려받기가 거부된다" in prompt
    for ticket in _MEMBERS:
        assert f"### 게이트 티켓 본문 ({ticket})" in prompt
        assert f"{ticket} 목표 문장." in prompt
    for path in review.paths:
        assert PurePosixPath(path).as_posix() in prompt
    assert "### PM 검토 중점" in prompt and focus in prompt
    assert "### 리뷰 입력 좌표" in prompt and "### 리뷰 입력 확인" in prompt
    assert "```diff" not in prompt and "T-8001 구현" not in prompt
    assert review.merge_base[:12] in prompt


@requires_git
def test_prompt_leaves_the_block_requirement_to_the_round_seed(pd, review_env):
    """구조화 블록 요구는 라운드 파일 시드가 소유한다 — 두 벌이면 갈린다."""
    home, _tickets = review_env
    board = _fixture_board(pd, home)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home)
    specs = pd._cluster_member_specs(pm_paths.home_at(home), review.members)

    prompt = pd.build_cluster_review_prompt(review, specs, tree=home, focus=None)

    assert "### 구조화 판정 블록 (필수)" not in prompt
    assert "### PM 검토 중점" not in prompt
    # 추가 리뷰어 채널의 기존 조립은 그대로다(요구 블록 포함).
    external = pd._load_additional_reviewer()
    assert "### 구조화 판정 블록 (필수)" in external.build_prompt(
        "본문", tree=home, base=review.merge_base, target=review.branch,
        changed_paths=review.paths, read_commands=[("git", "-C", str(home), "diff", "--")],
    )


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
        {"version": review_form.PM_REVIEW_VERSION, "findings": findings, "confirmations": []},
        ensure_ascii=False, separators=(",", ":"),
    )
    mustfix = "\n".join(f"- {item['id']}" for item in findings) or "- 없음"
    verdict = "반려" if findings else "통과"
    return (
        f"\n## must-fix\n{mustfix}\n\n## 판정\n판정: {verdict} · finding {len(findings)}건\n\n"
        f"```{review_form.PM_REVIEW_BLOCK}\n{payload}\n```\n"
    )


def _add_dir(argv: list[str]) -> Path:
    return Path(argv[argv.index("--add-dir") + 1])


def _reserve_prior_rounds(pd, home: Path, roles: tuple[str, ...]) -> None:
    """예산 수열의 선행 단계를 board 라운드로 깔아 둔다(리뷰가 그 다음 자리가 되게)."""
    import contextlib as _contextlib
    board = _fixture_board(pd, home)
    for ticket in _MEMBERS:
        for role in roles:
            if role == "architect":
                test_payload = json.dumps({
                    "version": review_form.ARCHITECT_TEST_VERSION,
                    "tests": [{
                        "id": "AT-001", "target": "tests/test_cluster_review_round.py",
                        "command": python_argv_command("--version"), "expected": "Python",
                        "negative": "계약 누락은 developer 준비를 차단한다",
                    }],
                }, ensure_ascii=False, separators=(",", ":"))
                content = (
                    "## 경계 실측\n- 묶음 리뷰\n\n## 불변식\n- 고정 수열\n\n"
                    "## 표면 상한\n- 추가 라운드 없음\n\n## 테스트 전략\n- 정상·실패\n\n"
                    f"```{review_form.ARCHITECT_TEST_BLOCK}\n{test_payload}\n```\n"
                )
            else:
                content = f"## {role} 산출\n- {ticket} 실측\n"
            round_store.reserve_round(
                pm_paths.tickets_dir(board._home()), ticket, role,
                content=content,
                lock=_contextlib.nullcontext(),
            )


def _cross_claude_review(pd, monkeypatch) -> None:
    """다른 하네스 형상 — PM 하네스 표식 없음 · 리뷰 역할 하네스 claude."""
    for key in _SESSION_MARKER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(pd, "local_config", lambda *_a, **_k: {
        "delegate_enabled": "true",
        "delegate.code-reviewer.harness": "claude",
        "delegate.code-reviewer.model": "sonnet",
    })


def _slot_seats(home: Path) -> list[Path]:
    root = home / ".project_manager" / ".local" / "delegate-ticket-copies" / _CLUSTER
    return sorted(root.glob("*/*/*-code-reviewer.md"))


@requires_git
def test_cross_review_runs_in_the_slot_and_harvests(pd, review_env, monkeypatch, capsys):
    """AT-012 — 교차 리뷰는 `--cwd` 슬롯에서 실행되고 끝나면 멤버 라운드를 돌려받는다.

    분리 실행 기록·내부 기록 파일은 생기지 않는다(그 경로는 지웠다).
    """
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _cross_claude_review(pd, monkeypatch)
    seen: dict = {}

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        seen["cwd"] = Path(cwd)
        seen["prompt"] = stdin_text or ""
        seats = _slot_seats(home)
        seen["seats"] = seats
        for seat in seats:
            _write_seat(pd, seat, [])
        reply = json.dumps({"type": "result", "result": "DONE", "session_id": "s-1"})
        return {"returncode": 0, "stdout": reply, "stderr": "", "timed_out": False}

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--output-dir", str(home.parent / "raw")],
        run_fn=_run_fn,
    )

    assert rc == 0, capsys.readouterr()
    assert seen["cwd"].resolve() == home.resolve()
    assert len(seen["seats"]) == len(_MEMBERS)
    assert f"리뷰 대상 트리: {home.resolve()}" in seen["prompt"]
    assert "격리 스냅샷" not in seen["prompt"]
    assert seen["prompt"].count("### 리뷰 입력 좌표") == 1
    assert "```sh" in seen["prompt"] and "T-8001 구현" not in seen["prompt"]
    # 리뷰 세션은 fresh 라 이전 번호를 모른다 — 자리마다 준비가 확정한 다음 finding ID 를 싣는다.
    assert seen["prompt"].count("finding ID: 이번 라운드의 새 finding 은 `F-001` 부터") == len(
        _MEMBERS)
    for ticket in _MEMBERS:
        assert review_form.PM_REVIEW_BLOCK in _board_review_round(home, ticket).read_text(
            encoding="utf-8")
    assert _slot_seats(home) == []
    local = home / ".project_manager" / ".local"
    assert not (local / "internal_review_rounds.json").exists()
    assert not (local / "cluster-review-runs.jsonl").exists()
    rows = delegate_ledger.ticket_copy_records(home)
    head = _git(home, "rev-parse", "HEAD").stdout.strip()
    assert {row["ticket"] for row in rows if row["harvested_at"]} == set(_MEMBERS)
    assert all(row.get("base_rev") == head for row in rows)


@requires_git
def test_cross_review_with_the_pm_harness_states_the_native_rule(
        pd, review_env, monkeypatch, capsys):
    """리뷰도 다른 역할과 같은 규칙이다 — PM 과 같은 하네스로 CLI 를 부르면 native 안내가 나온다."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _native_review_harness(pd, monkeypatch)
    executed: list = []

    def _run_fn(argv, *, stdin_text, cwd, env, timeout, harness):
        executed.append(harness)
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}

    pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--output-dir", str(home.parent / "raw")],
        run_fn=_run_fn,
    )

    err = capsys.readouterr().err
    assert executed == ["claude"], err
    assert "하네스 claude(model=sonnet) 실행 중" in err
    assert "설계·구현·리뷰·조사는 native 위임이 규칙이다" in err
    assert "리뷰만 엔진" not in err


@requires_git
def test_delegation_refuses_when_the_cluster_budget_is_spent(pd, review_env, capsys):
    """묶음 예산은 리뷰 위임 표면에서도 같은 판정이다(우회 인자 없음)."""
    home, _tickets = review_env
    _reserve_prior_rounds(
        pd, home, ("architect", "developer", "code-reviewer", "developer"))
    capsys.readouterr()

    rc = pd.main(
        ["--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", str(home),
         "--output-dir", str(home.parent / "raw")],
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
    assert "--ticket 과 병용" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        pd.main(["--role", "developer", "--cluster", _CLUSTER, "--cwd", str(home)])
    assert "code-reviewer 역할 전용" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        pd.main(["--role", "code-reviewer", "--cwd", str(home), "--ticket", "T-8001"])
    assert "--cluster 가 필수다" in capsys.readouterr().err


_REMOVED_REVIEW_CLI = (
    ("--role", "code-reviewer", "--cluster", _CLUSTER, "--cwd", "{home}", "--background"),
    ("--role", "code-reviewer", "--gate", "T-8001", "--cwd", "{home}"),
    ("cluster", "wait", "--cluster", _CLUSTER, "--cwd", "{home}"),
    ("rounds", "recalculate", "--gate", "T-8001"),
    ("rounds", "report"),
    ("--rounds-report",),
    ("rounds", "--rounds-report"),
    ("rounds", "resolve", "--gate", "T-8001", "--pm-verified"),
)


def test_review_cli_has_no_background_or_wait(pd, review_env, capsys):
    """AT-013 — 지운 CLI 표면(분리 실행·대기·재계산·조회·게이트 처분)은 전부 사용 오류(rc 2)다."""
    home, _tickets = review_env
    codes = {}
    for argv in _REMOVED_REVIEW_CLI:
        resolved = [item.format(home=home) for item in argv]
        with pytest.raises(SystemExit) as caught:
            pd.main(resolved)
        codes[" ".join(argv)] = caught.value.code
        capsys.readouterr()

    assert codes == {" ".join(argv): 2 for argv in _REMOVED_REVIEW_CLI}


def test_removed_cluster_subcommand_has_no_parser(pd):
    """지운 `cluster wait` 는 조회 표면(실행 단일 진실 parser)에도 남지 않는다."""
    assert pd.build_subcommand_parser("cluster") is None


# ════════════════════════════════════════════════════════════════════════
# 같은 하네스 리뷰 — ticket prepare → 라운드 파일 → ticket harvest (I2)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_ticket_prepare_code_reviewer_writes_engine_prompt(
        pd, review_env, monkeypatch, capsys):
    """AT-008 — 준비가 엔진 프롬프트 파일을 내고 멤버 행마다 `base_rev` 를 남긴다."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _native_review_harness(pd, monkeypatch)
    focus = home / "focus.md"
    focus.write_text("경계 판정의 값 대조를 중점으로 본다.\n", encoding="utf-8", newline="\n")
    # 검토 중점 파일도 슬롯 안이면 작업 트리 변경이다 — 커밋해 둔다(준비 I1).
    assert _git(home, "add", "focus.md").returncode == 0
    assert _git(home, "commit", "-qm", "focus").returncode == 0
    head = _git(home, "rev-parse", "HEAD").stdout.strip()

    lines = _prepare_native_review(pd, home, capsys, "--focus", str(focus))

    assert {line["ticket"] for line in lines} == set(_MEMBERS)
    assert {line["ordinal"] for line in lines} == {3}
    prompts = {line["prompt"] for line in lines}
    assert len(prompts) == 1
    prompt_path = Path(prompts.pop())
    assert prompt_path.parent == home / ".project_manager" / ".local" / "delegate"
    prompt = prompt_path.read_text(encoding="utf-8")
    assert f"리뷰 대상 트리: {home.resolve()} (브랜치 {_CLUSTER_BRANCH})" in prompt
    for ticket in _MEMBERS:
        assert f"### 게이트 티켓 본문 ({ticket})" in prompt
        assert f"{ticket} 목표 문장." in prompt
    for line in lines:
        assert f"- {line['ticket']}: {line['copy']}" in prompt
    assert "### PM 검토 중점" in prompt and "경계 판정의 값 대조" in prompt
    assert "t-8001.py" in prompt and "t-8002.py" in prompt
    assert "### 리뷰 입력 좌표" in prompt and "```sh" in prompt
    assert "```diff" not in prompt and "T-8001 구현" not in prompt
    assert "격리 스냅샷" not in prompt
    rows = delegate_ledger.ticket_copy_records(home)
    assert len(rows) == len(_MEMBERS)
    assert all(row["role"] == "code-reviewer" and row.get("base_rev") == head for row in rows)
    assert all("owner_pid" not in row for row in rows)


@pytest.mark.parametrize("extra", [
    ["--ticket", "T-8001", "--role", "code-reviewer"],
    ["--ticket", "T-8001", "--role", "developer", "--focus", "/tmp/focus.md"],
])
@requires_git
def test_ticket_prepare_review_takes_the_cluster_only(
        pd, review_env, monkeypatch, capsys, extra):
    """AT-010 — code-reviewer 는 `--cluster` 로만, `--focus` 는 code-reviewer 만 받는다(rc 2)."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _native_review_harness(pd, monkeypatch)

    with pytest.raises(SystemExit) as caught:
        pd._cmd_ticket(["prepare", *extra, "--cwd", str(home)])

    assert caught.value.code == 2
    assert delegate_ledger.ticket_copy_records(home) == []


@pytest.mark.parametrize("change", ["tracked", "untracked", "commit"])
@requires_git
def test_review_harvest_refuses_when_slot_changed_after_prepare(
        pd, review_env, monkeypatch, capsys, change):
    """AT-004 — 준비 뒤 슬롯이 바뀌면 돌려받기가 board·장부·run-dir 을 바꾸지 않고 거부한다.

    변경 전에는 code-reviewer 행에 `base_rev` 가 없고 돌려받기가 트리를 보지 않아 수락했다.
    """
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _native_review_harness(pd, monkeypatch)
    lines = _prepare_native_review(pd, home, capsys)
    run_dir = Path(lines[0]["run_dir"])
    for line in lines:
        _write_seat(pd, Path(line["copy"]), [])
    seeds = {ticket: _board_review_round(home, ticket).read_bytes() for ticket in _MEMBERS}
    prepared_head = _git(home, "rev-parse", "HEAD").stdout.strip()
    if change == "tracked":
        (home / "t-8001.py").write_text("# 리뷰어가 고침\n", encoding="utf-8", newline="\n")
        evidence = ["t-8001.py"]
    elif change == "untracked":
        (home / "probe.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
        evidence = ["probe.py"]
    else:
        (home / "seed.txt").write_text("again\n", encoding="utf-8", newline="\n")
        assert _git(home, "commit", "-qam", "after prepare").returncode == 0
        evidence = [prepared_head, _git(home, "rev-parse", "HEAD").stdout.strip()]
    capsys.readouterr()

    rc = pd._cmd_ticket(["harvest", "--copy", str(run_dir), "--cwd", str(home)])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert "준비 뒤 슬롯이 바뀌었습니다" in err
    for item in evidence:
        assert item in err
    for ticket in _MEMBERS:
        assert _board_review_round(home, ticket).read_bytes() == seeds[ticket]
    assert run_dir.is_dir()
    assert all(row["harvested_at"] is None for row in delegate_ledger.ticket_copy_records(home))


@requires_git
def test_review_harvest_accepts_round_file_only_change(pd, review_env, monkeypatch, capsys):
    """AT-005 — 라운드 파일과 ignore 대상만 바뀐 정상 리뷰는 돌려받기가 통과한다(정상 사용)."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _native_review_harness(pd, monkeypatch)
    lines = _prepare_native_review(pd, home, capsys)
    for line in lines:
        _write_seat(pd, Path(line["copy"]), [])
    scratch = home / ".project_manager" / ".local" / "tmp" / "review-probe.txt"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("ignore 대상 산출\n", encoding="utf-8")
    capsys.readouterr()

    rc = pd._cmd_ticket([
        "harvest", "--copy", lines[0]["run_dir"], "--cwd", str(home),
    ])

    assert rc == 0, capsys.readouterr().err
    for ticket in _MEMBERS:
        assert review_form.PM_REVIEW_BLOCK in _board_review_round(home, ticket).read_text(
            encoding="utf-8")
    assert all(row["harvested_at"] for row in delegate_ledger.ticket_copy_records(home))


@requires_git
def test_review_harvest_refuses_a_row_without_base_rev(pd, review_env, capsys):
    """AT-006 — `base_rev` 없는 code-reviewer 행(옛 CLI 준비)은 HEAD 로 대신하지 않고 거부한다."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    plan = pd.prepare_cluster_copy(
        cluster=_CLUSTER, role="code-reviewer", cwd=home, pm_home=home,
    )
    ledger = home / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row.pop("base_rev", None)
    ledger.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8")
    seat = plan.rounds[0]
    _write_seat(pd, seat.path, [])
    seed = seat.board_path.read_bytes()
    capsys.readouterr()

    rc = pd._cmd_ticket(["harvest", "--copy", str(seat.path), "--cwd", str(home)])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert "base_rev 가 없습니다" in err
    assert "ticket abandon" in err and "--reopen-ordinal 3" in err
    assert seat.board_path.read_bytes() == seed
    assert seat.path.is_file()


@requires_git
def test_ticket_prepare_code_reviewer_reopens_the_same_ordinal(
        pd, review_env, monkeypatch, capsys):
    """AT-009 — 폐기한 리뷰를 같은 순번으로 다시 열면 그 시점 HEAD 를 새 `base_rev` 로 남긴다."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _native_review_harness(pd, monkeypatch)
    first = _prepare_native_review(pd, home, capsys)
    for line in first:
        _write_seat(pd, Path(line["copy"]), [])
        assert pd._cmd_ticket([
            "abandon", "--copy", line["copy"], "--cwd", str(home), "--assume-dead",
            "--discard-reason", "리뷰 뒤 슬롯이 앞서 감",
        ]) == 0
    (home / "seed.txt").write_text("fix 뒤\n", encoding="utf-8", newline="\n")
    assert _git(home, "commit", "-qam", "slot moves on").returncode == 0
    new_head = _git(home, "rev-parse", "HEAD").stdout.strip()

    reopened = _prepare_native_review(pd, home, capsys, "--reopen-ordinal", "3")

    assert {line["ordinal"] for line in reopened} == {3}
    latest = [
        row for row in delegate_ledger.ticket_copy_records(home)
        if row["run_id"] == Path(reopened[0]["run_dir"]).name
    ]
    assert latest and all(row.get("base_rev") == new_head for row in latest)
    for line in reopened:
        seat = Path(line["copy"])
        kept = [
            item for item in seat.read_text(encoding="utf-8").splitlines(keepends=True)
            if review_form.PM_REVIEW_REFUSED_MARKER not in item
        ]
        seat.write_text("".join(kept), encoding="utf-8", newline="")
    capsys.readouterr()
    rc = pd._cmd_ticket([
        "harvest", "--copy", reopened[0]["run_dir"], "--cwd", str(home),
    ])
    assert rc == 0, capsys.readouterr().err


@requires_git
def test_native_review_must_fix_blocks_completion_until_disposed(
        pd, review_env, monkeypatch, capsys):
    """AT-001 — CLI 실행 없이 같은 하네스 경로로 만든 리뷰의 미처분 must-fix 가 판정을 막는다.

    준비(`ticket prepare`) → 라운드 파일 작성 → 돌려받기(`ticket harvest`) 뒤 내부 기록 파일이
    없어도 완료·종결 판정이 잔여를 거부하고, 기각 처분 기입 뒤에는 둘 다 통과한다.
    """
    home, tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    _native_review_harness(pd, monkeypatch)
    lines = _prepare_native_review(pd, home, capsys)
    finding = _finding("F-001")
    for line in lines:
        _write_seat(pd, Path(line["copy"]), [finding] if line["ticket"] == _MEMBERS[0] else [])
    assert pd._cmd_ticket([
        "harvest", "--copy", lines[0]["run_dir"], "--cwd", str(home),
    ]) == 0
    board = _fixture_board(pd, home)
    ticket = _MEMBERS[0]
    assert not (home / ".project_manager" / ".local" / "internal_review_rounds.json").exists()

    completion = board._review_completion_problem(ticket)
    resolution = board._review_resolution_input_problem(ticket)
    assert completion is not None and "code-reviewer 리뷰 라운드 03" in completion
    assert resolution is not None
    assert board._review_completion_problem(_MEMBERS[1]) is None
    assert board._review_resolution_input_problem(_MEMBERS[1]) is None

    spec = tickets / f"{ticket}-review.md"
    spec.write_text(
        spec.read_text(encoding="utf-8") + "\n"
        + _disposition_block(pd, 3, [_decision("F-001", "rejected")]),
        encoding="utf-8", newline="")

    assert board._review_completion_problem(ticket) is None
    assert board._review_resolution_input_problem(ticket) is None


def _round(pd, ordinal: int, role: str, text: str):
    return round_rules.Round(
        ordinal=ordinal, role=role,
        path=Path(round_rules.round_filename(ordinal, role)),
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
        "version": review_form.PM_REVIEW_DISPOSITION_VERSION,
        "reviewer_role": "code-reviewer",
        "reviewer_ordinal": ordinal,
        "dispositions": rows,
    }, ensure_ascii=False, separators=(",", ":"))
    return f"```{review_form.PM_REVIEW_DISPOSITION_BLOCK}\n{payload}\n```\n"


def _verify_block(pd, rows: list) -> str:
    payload = json.dumps(
        {"version": review_form.PM_REVIEW_VERIFY_VERSION, "verifications": rows},
        ensure_ascii=False, separators=(",", ":"),
    )
    return f"```{review_form.PM_REVIEW_VERIFY_BLOCK}\n{payload}\n```\n"


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
        (2, review_form.PMReviewMachineConfirmation(
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
    import contextlib
    for ticket in _MEMBERS:
        spec, rounds = _accepted_ticket(pd)  # noqa: PLW2901 — 티켓마다 같은 형상
        path = tickets / f"{ticket}-review.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n" + spec,
                        encoding="utf-8", newline="")
        for item in rounds:
            round_store.reserve_round(
                pm_paths.tickets_dir(board._home()), ticket, item.role, content=item.text,
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
    template = review_state.pm_review_verify_template(spec, rounds)
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
    template = review_state.pm_review_verify_template(spec, rounds)

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

    with pytest.raises(review_form.PMReviewError, match="금지 토큰"):
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
    template = review_state.pm_review_verify_template(spec, rounds)
    rows = pd.run_pm_review_confirmations(
        template, cwd=tmp_path, run_fn=_stub_run(0, "hi\n"))

    section = pd.render_pm_review_confirmation_section(rows)

    assert section.startswith(pd.PM_REVIEW_CONFIRMATION_SECTION)
    assert "<관측값>" not in section and "<resolved|" not in section
    delta = review_state.parse_pm_review_delta(spec + "\n" + section, rounds)
    assert delta.accepted == ()
    assert review_state.pm_verified_evidence_problem(
        spec + "\n" + section, rounds,
        reviewer_role="code-reviewer", surface_floor=1,
    ) is None


def test_pm_owned_terminal_confirmation_is_generated_without_running_a_command(
    pd, tmp_path,
):
    spec, rounds = _pm_owned_ticket(pd)
    template = review_state.pm_review_verify_template(spec, rounds)
    assert template.machine_rows == ()
    assert [(source, row.id) for source, row in template.pm_owned_rows] == [(2, "F-003")]
    assert review_state.pm_verified_resolution_input_problem(
        spec, rounds, reviewer_role="code-reviewer", surface_floor=1,
    ) is None

    rows = pd.run_pm_review_confirmations(
        template, cwd=tmp_path,
        run_fn=lambda *_a, **_k: pytest.fail("PM-owned confirmation은 command를 실행하지 않는다"),
    )
    source, row = rows[0]
    assert (source, row.id, row.status) == (2, "F-003", "resolved")
    assert row.command == review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND
    assert row.observed == "PM audit 완료 실값"

    confirmed = spec + "\n" + pd.render_pm_review_confirmation_section(rows)
    assert review_state.parse_pm_review_delta(confirmed, rounds).accepted == ()
    assert review_state.pm_verified_evidence_problem(
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
    problem = review_state.pm_verified_resolution_input_problem(
        spec, rounds, reviewer_role="code-reviewer", surface_floor=1,
    )
    assert problem is not None and (
        "PM-owned" in problem or "machine_verifiable=false" in problem
    )
    forged = json.dumps({
        "version": review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
        "round": 2,
        "confirmations": [{
            "id": "F-003", "status": "resolved",
            "command": review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND,
            "observed": "PM audit 완료 실값",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(review_form.PMReviewError, match="strict pm-owned|PM-owned"):
        review_state.parse_pm_review_delta(
            spec + f"\n```{review_form.PM_REVIEW_CONFIRMATION_BLOCK}\n{forged}\n```\n",
            rounds,
        )


def test_arbitrary_false_verify_remains_reviewer_required_at_terminal_preflight(pd):
    spec, rounds = _pm_owned_ticket(
        pd, scope="F-003 developer 범위", reason="design-judgment",
    )
    template = review_state.pm_review_verify_template(spec, rounds)
    assert template.pm_owned_rows == ()
    assert [row[0] for row in template.reviewer_required] == ["F-003"]
    problem = review_state.pm_verified_resolution_input_problem(
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
    template = review_state.pm_review_verify_template(path.read_text(encoding="utf-8"), rounds)
    rows = pd.run_pm_review_confirmations(
        template, cwd=home, run_fn=_stub_run(0, "hi\n"))

    written = pd.append_pm_review_confirmation(
        home, ticket, pd.render_pm_review_confirmation_section(rows), rounds=rounds)

    assert written == path
    text = path.read_text(encoding="utf-8")
    assert pd.PM_REVIEW_CONFIRMATION_SECTION in text
    # 기입은 명세 PM 영역이다 — frontmatter 는 그대로다.
    assert text.startswith("---\n")
    assert review_state.parse_pm_review_delta(text, rounds).accepted == ()


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
    template = review_state.pm_review_verify_template(path.read_text(encoding="utf-8"), rounds)
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
        block for block in review_form._pm_review_json_blocks(text)
        if block.kind == review_form.PM_REVIEW_CONFIRMATION_BLOCK
    ]
    assert len(blocks) == 1 and blocks[0].value["round"] == 2
    assert [row["id"] for row in blocks[0].value["confirmations"]] == [
        "F-001", "F-003",
    ]
    assert review_state.parse_pm_review_delta(text, rounds).accepted == ()


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
        (2, review_form.PMReviewMachineConfirmation(*row, 2)),
    ))

    with pytest.raises(review_form.PMReviewError, match=message):
        pd.append_pm_review_confirmation(home, ticket, incoming, rounds=rounds)

    assert path.read_bytes() == before


# ════════════════════════════════════════════════════════════════════════
# 설계 축도 reviewer의 수정·테스트 계약으로 fix에서 종결한다
# ════════════════════════════════════════════════════════════════════════

def test_design_axis_uses_the_reviewer_machine_test_contract(pd):
    spec, rounds = _accepted_ticket(pd, design_change=True)
    template = review_state.pm_review_verify_template(spec, rounds)

    assert [(source, row.id) for source, row in template.machine_rows] == [(2, "F-001")]
    assert template.reviewer_required == ()

    confirmation = json.dumps({
        "version": review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION, "round": 2,
        "confirmations": [{"id": "F-001", "status": "resolved",
                           "command": "echo hi", "observed": "hi"}],
    }, ensure_ascii=False, separators=(",", ":"))
    delta = review_state.parse_pm_review_delta(
        spec + f"\n```{review_form.PM_REVIEW_CONFIRMATION_BLOCK}\n{confirmation}\n```\n",
        rounds,
    )
    assert delta.accepted == ()


def test_design_axis_residual_closes_after_the_fix_test_confirmation(pd):
    spec, rounds = _accepted_ticket(pd, design_change=True)

    before = review_state.pm_verified_evidence_problem(
        spec, rounds, reviewer_role="code-reviewer", surface_floor=1)
    confirmation = json.dumps({
        "version": review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION, "round": 2,
        "confirmations": [{"id": "F-001", "status": "resolved",
                           "command": "echo hi", "observed": "hi"}],
    }, ensure_ascii=False, separators=(",", ":"))
    after = review_state.pm_verified_evidence_problem(
        spec + f"\n```{review_form.PM_REVIEW_CONFIRMATION_BLOCK}\n{confirmation}\n```\n",
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
    board._claim_code_tree = lambda *_a, **_k: str(home)

    assert pd._cluster_confirmation_tree(board, _CLUSTER, home=pm_paths.home_at(home)) == home

    assert _git(home, "checkout", "-q", _BASE_BRANCH).returncode == 0
    with pytest.raises(delegate_errors.DelegateError, match="통합 브랜치가 아닙니다"):
        pd._cluster_confirmation_tree(board, _CLUSTER, home=pm_paths.home_at(home))


@requires_git
def test_focus_file_is_read_as_prompt_source_and_missing_paths_fail_loud(
        pd, review_env):
    """검토 중점 파일은 그대로 프롬프트에 실리고, 없는 경로는 조용히 비지 않는다."""
    home, _tickets = review_env
    inside = home / "focus.md"
    inside.write_text("경계 판정을 중점으로 본다.\n", encoding="utf-8", newline="\n")

    assert "경계 판정" in pd._cluster_review_focus(inside, cwd=home, pm_home=home)
    with pytest.raises(delegate_errors.DelegateError, match="--focus 파일이 없습니다"):
        pd._cluster_review_focus(home / "없다.md", cwd=home, pm_home=home)


@requires_git
def test_resolve_cluster_completes_without_internal_ledger(
        pd, review_env, monkeypatch, capsys):
    """AT-014 — 엔진이 확인 커맨드를 실행해 기입하고 완료 판정의 리뷰 잔여를 연다(내부 기록 없음).

    변경 전에는 기계 확인 기입 뒤 "내부 리뷰 게이트 장부 항목이 없음" 으로 rc 1 이었다.
    """
    home, tickets = review_env
    board = _fixture_board(pd, home)
    board.LEASES_FILE = home / ".project_manager/.local/worktree-leases.json"
    board.LEASES_FILE.write_text(json.dumps({"leases": [{
        "slot": ".", "repo": "proj", "session": "main", "state": "leased",
    }]}), encoding="utf-8")
    tree_identities = []

    def task_tree(identity=None):
        tree_identities.append(identity)
        return str(home)

    board._claim_code_tree = task_tree
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", home)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    import contextlib
    for ticket in _MEMBERS:
        spec, rounds = _accepted_ticket(pd, command="echo hi", expected="hi")
        path = tickets / f"{ticket}-review.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n" + spec,
                        encoding="utf-8", newline="")
        for item in rounds:
            round_store.reserve_round(
                pm_paths.tickets_dir(board._home()), ticket, item.role, content=item.text,
                lock=contextlib.nullcontext(),
            )
    capsys.readouterr()

    rc = pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified", "--task", "main"],
        run_fn=_stub_run(0, "hi\n"),
    )

    out = capsys.readouterr().out
    assert rc == 0, out
    assert tree_identities == []  # task 단독은 finish의 묶음 브랜치 탐색을 소비한다.
    for ticket in _MEMBERS:
        text = (tickets / f"{ticket}-review.md").read_text(encoding="utf-8")
        assert pd.PM_REVIEW_CONFIRMATION_SECTION in text
        assert '"observed":"rc=0' in text and "hi" in text
        assert "기계 확인 1건 기입" in out and ticket in out
        assert f"{ticket}: 리뷰 잔여 확인 완료" in out
        assert board._review_completion_problem(ticket) is None
    assert not (home / ".project_manager" / ".local" / "internal_review_rounds.json").exists()


@requires_git
def test_resolve_cluster_closes_strict_pm_owned_findings_without_reviewer_or_command(
    pd, review_env, monkeypatch, capsys,
):
    home, tickets = review_env
    board = _fixture_board(pd, home)
    # 확인 트리는 `--task` 가 보유한 폴더에서만 해소한다(T-0962 — PM 홈 접기 없음).
    board.LEASES_FILE = home / ".project_manager/.local/worktree-leases.json"
    board.LEASES_FILE.write_text(json.dumps({"leases": [{
        "slot": ".", "repo": "proj", "session": "main", "state": "leased",
    }]}), encoding="utf-8")
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", home)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    import contextlib
    for ticket in _MEMBERS:
        spec, rounds = _pm_owned_ticket(pd)
        path = tickets / f"{ticket}-review.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + spec,
            encoding="utf-8", newline="",
        )
        for item in rounds:
            round_store.reserve_round(
                pm_paths.tickets_dir(board._home()), ticket, item.role, content=item.text,
                lock=contextlib.nullcontext(),
            )
    capsys.readouterr()

    rc = pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified", "--task", "main"],
        run_fn=lambda *_a, **_k: pytest.fail(
            "PM-owned terminal confirmation은 reviewer contract command를 실행하지 않는다"
        ),
    )

    captured = capsys.readouterr()
    assert rc == 0, captured
    for ticket in _MEMBERS:
        text = (tickets / f"{ticket}-review.md").read_text(encoding="utf-8")
        assert f'"command":"{review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND}"' in text
        spec_text, rounds = pd._ticket_spec_and_rounds(pm_paths.home_at(home), ticket)
        assert review_state.parse_pm_review_delta(spec_text, rounds).accepted == ()
        assert review_state.review_completion_problem(spec_text, rounds) is None
    assert "PM-owned terminal 확인 1건 기입" in captured.out


@requires_git
def test_resolve_retry_merges_pm_owned_row_into_partial_same_round_and_completes(
    pd, review_env, monkeypatch, capsys,
):
    """machine 확인 append 뒤 처분 실패를 재시도해도 같은 round block을 중복하지 않는다."""
    home, tickets = review_env
    board = _fixture_board(pd, home)
    # 확인 트리는 `--task` 가 보유한 폴더에서만 해소한다(T-0962 — PM 홈 접기 없음).
    board.LEASES_FILE = home / ".project_manager/.local/worktree-leases.json"
    board.LEASES_FILE.write_text(json.dumps({"leases": [{
        "slot": ".", "repo": "proj", "session": "main", "state": "leased",
    }]}), encoding="utf-8")
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", home)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    import contextlib
    for ticket in _MEMBERS:
        spec, rounds = _partially_confirmed_pm_owned_ticket(pd)
        path = tickets / f"{ticket}-review.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + spec,
            encoding="utf-8", newline="",
        )
        for item in rounds:
            round_store.reserve_round(
                pm_paths.tickets_dir(board._home()), ticket, item.role, content=item.text,
                lock=contextlib.nullcontext(),
            )
    capsys.readouterr()

    no_command = lambda *_a, **_k: pytest.fail(
        "partial state의 남은 행은 PM-owned라 command를 실행하지 않는다"
    )
    assert pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified", "--task", "main"], run_fn=no_command,
    ) == 0
    # 이미 처분된 상태의 재실행도 append/중복 없이 성공해야 한다.
    assert pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified", "--task", "main"], run_fn=no_command,
    ) == 0

    for ticket in _MEMBERS:
        spec_text, rounds = pd._ticket_spec_and_rounds(pm_paths.home_at(home), ticket)
        blocks = [
            block for block in review_form._pm_review_json_blocks(spec_text)
            if block.kind == review_form.PM_REVIEW_CONFIRMATION_BLOCK
        ]
        assert len(blocks) == 1 and blocks[0].value["round"] == 2
        assert [row["id"] for row in blocks[0].value["confirmations"]] == [
            "F-001", "F-003",
        ]
        assert review_state.parse_pm_review_delta(spec_text, rounds).accepted == ()


@requires_git
def test_resolve_cluster_is_scoped_to_the_machine_evidence_disposition(pd, capsys):
    """묶음 처분은 `--pm-verified` 전용이다 — 게이트별 판단은 묶음으로 접지 않는다."""
    with pytest.raises(SystemExit):
        pd._cmd_rounds(["resolve", "--cluster", _CLUSTER, "--into", "T-0001"])
    err = capsys.readouterr().err
    assert "--pm-verified" in err and "--into" not in err

    with pytest.raises(SystemExit) as caught:
        pd._cmd_rounds(["resolve", "--cluster", _CLUSTER, "--gate", "T-0001",
                        "--pm-verified"])
    assert caught.value.code == 2
    assert "unrecognized arguments: --gate" in capsys.readouterr().err


def test_resolve_identity_accepts_task_and_repo_slot_execution(pd, review_env, monkeypatch):
    home, _ = review_env
    board = _fixture_board(pd, home)
    seen = []
    board._claim_code_tree = lambda identity: seen.append(identity) or str(home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda owner: board)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_resolve_ticket_pm_verified", lambda *a, **k: 0)
    assert pd._cmd_rounds([
        "resolve", "--cluster", _CLUSTER, "--pm-verified",
        "--task", "main", "--repo", "proj", "--slot", "5",
    ]) == 0
    assert len(seen) == 1
    assert (seen[0].task, seen[0].repo, seen[0].slot) == ("main", "proj", 5)


@requires_git
def test_dry_run_previews_the_cluster_without_side_effects(pd, review_env, capsys):
    """미리보기는 부작용 0 이다 — 스냅샷도 예약도 만들지 않고 입력만 값으로 보인다."""
    home, _tickets = review_env
    _reserve_prior_rounds(pd, home, ("architect", "developer"))
    # 검토 중점 파일은 슬롯 밖에 둔다 — 슬롯 안 추적 안 된 파일은 준비 I1 의 작업 트리 변경이다.
    focus = home.parent / "focus.md"
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
    assert delegate_ledger.ticket_copy_records(home) == []


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
         "--output-dir", str(home.parent / "raw")],
        run_fn=_run_fn,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "ticket harvest 거부" in err and _MEMBERS[1] in err
    first = (
        home / ".project_manager" / "wiki" / "tickets" / "rounds" / _MEMBERS[0]
        / "03-code-reviewer.md"
    )
    assert review_form.PM_REVIEW_BLOCK in first.read_text(encoding="utf-8")


@requires_git
def test_resolve_cluster_skips_a_member_whose_review_passed_clean(
        pd, review_env, monkeypatch, capsys):
    """통과(must-fix 0) 멤버는 무대상으로 건너뛰고 반려 멤버만 처분한다 — 묶음은 닫힌다."""
    home, tickets = review_env
    board = _fixture_board(pd, home)
    board._claim_code_tree = lambda *_a, **_k: str(home)
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", home)
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: home)
    monkeypatch.setattr(pd, "_load_board", lambda: board)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    import contextlib
    rejected, passed = _MEMBERS
    spec, rounds = _accepted_ticket(pd, command="echo hi", expected="hi")
    path = tickets / f"{rejected}-review.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + spec,
                    encoding="utf-8", newline="")
    for item in rounds:
        round_store.reserve_round(
            pm_paths.tickets_dir(board._home()), rejected, item.role, content=item.text,
            lock=contextlib.nullcontext(),
        )
    round_store.reserve_round(
        pm_paths.tickets_dir(board._home()), passed, "code-reviewer",
        content=_reviewer_round_text(pd, []), lock=contextlib.nullcontext(),
    )
    capsys.readouterr()

    rc = pd._cmd_rounds(
        ["resolve", "--cluster", _CLUSTER, "--pm-verified"],
        run_fn=_stub_run(0, "hi\n"),
    )

    out = capsys.readouterr().out
    assert rc == 0, out
    assert board._review_completion_problem(rejected) is None
    assert f"{passed}: 처분할 리뷰 잔여 없음(must-fix 0건) — 무대상" in out
    assert pd.PM_REVIEW_CONFIRMATION_SECTION not in (
        tickets / f"{passed}-review.md").read_text(encoding="utf-8")


@requires_git
@pytest.mark.parametrize('explicit', [False, True])
def test_t0929_finish_confirmation_uses_declared_branch_tree(pd, review_env, monkeypatch, explicit):
    import argparse
    home, tickets = review_env
    board = _fixture_board(pd, home)
    board.LEASES_FILE = home / '.project_manager/.local/worktree-leases.json'
    # 동일 저장소의 두 작업 트리, 묶음 브랜치를 가진 트리는 두 번째 행이다.
    one, five = home / 'work/proj_1', home / 'work/proj_5'
    assert _git(home, 'checkout', '-q', _BASE_BRANCH).returncode == 0
    assert _git(home, 'worktree', 'add', '-q', '-b', 'task/other', str(one)).returncode == 0
    assert _git(home, 'worktree', 'add', '-q', str(five), _CLUSTER_BRANCH).returncode == 0
    rows = [{'slot': f'work/proj_{n}', 'repo': 'proj', 'session': 'main', 'state': 'leased', 'role': 'work'} for n in (1, 5)]
    board.LEASES_FILE.write_text(json.dumps({'leases': rows}), encoding='utf-8')
    tf = _load_tool('ticket_finish', f'ticket_finish_t0929_{explicit}')
    monkeypatch.setattr(tf, 'REPO', home)
    monkeypatch.setattr(tf, 'LEASES_FILE', board.LEASES_FILE)
    finisher = tf.TicketFinisher(board_py=Path(board.__file__))
    closer = tf.ClusterCloser(_CLUSTER, finisher=finisher, board_py=Path(board.__file__), task='main', board_identity_args=['--task', 'main'])
    closer._board = board
    found, problem = closer._resolve_code_tree()
    assert problem is None and found == five
    forwarded = []
    closer._run_delegate_fn = lambda argv: (forwarded.append(argv) or (0, ''))
    closer._pending_gates = lambda: list(_MEMBERS)
    assert closer._step_resolve() is None
    # finish의 기존 argv 계약은 task 단독이며 슬롯 좌표를 넣지 않는다.
    assert forwarded == [[
        'rounds', 'resolve', '--cluster', _CLUSTER, '--pm-verified', '--task', 'main']]
    identity = argparse.Namespace(task='main', repo='proj' if explicit else None, slot=5 if explicit else None)
    before = board.LEASES_FILE.read_bytes()
    assert pd._cluster_confirmation_tree(
        board, _CLUSTER, home=pm_paths.home_at(home), identity=identity) == found
    assert board.LEASES_FILE.read_bytes() == before

    # finish argv에서 실제 내부 확인·기입·처분까지 연결하며 외부 명령만 대역으로 둔다.
    import contextlib
    monkeypatch.setattr(pd, '_CONFIG_REPO_OVERRIDE', home)
    monkeypatch.setattr(pd, '_activate_internal_rounds_cli_owner', lambda: home)
    monkeypatch.setattr(pd, '_load_board', lambda: board)
    monkeypatch.setattr(pd, '_load_board_for_repo', lambda owner: board)
    for ticket in _MEMBERS:
        spec, rounds = _accepted_ticket(pd, command='echo hi', expected='hi')
        path = tickets / f'{ticket}-review.md'
        path.write_text(path.read_text(encoding='utf-8') + '\n' + spec, encoding='utf-8')
        for item in rounds:
            round_store.reserve_round(pm_paths.tickets_dir(board._home()), ticket, item.role,
                                        content=item.text, lock=contextlib.nullcontext())
    command_calls = []
    runner = _stub_run(0, 'hi\n')
    def observe(argv, **kwargs):
        command_calls.append(kwargs)
        return runner(argv, **kwargs)
    argv = forwarded[0][1:]
    if explicit:
        argv += ['--repo', 'proj', '--slot', '5']
    assert pd._cmd_rounds(argv, run_fn=observe) == 0
    assert len(command_calls) == len(_MEMBERS)
    assert all(Path(call['cwd']) == found for call in command_calls)
    assert board.LEASES_FILE.read_bytes() == before
    for ticket in _MEMBERS:
        assert pd.PM_REVIEW_CONFIRMATION_SECTION in (tickets / f'{ticket}-review.md').read_text(encoding='utf-8')


@requires_git
def test_t0929_finish_confirmation_explicit_wrong_branch_refused(pd, review_env):
    import argparse
    home, _ = review_env
    board = _fixture_board(pd, home)
    board.LEASES_FILE = home / '.project_manager/.local/worktree-leases.json'
    slot = home / 'work/proj_1'
    assert _git(home, 'worktree', 'add', '-q', str(slot), _BASE_BRANCH).returncode == 0
    board.LEASES_FILE.write_text(json.dumps({'leases': [{'slot': 'work/proj_1', 'repo': 'proj', 'session': 'main'}]}), encoding='utf-8')
    with pytest.raises(delegate_errors.DelegateError, match='브랜치'):
        pd._cluster_confirmation_tree(board, _CLUSTER, home=pm_paths.home_at(home), identity=argparse.Namespace(task='main', repo='proj', slot=1))


@requires_git
@pytest.mark.parametrize("task_only", [False, True])
def test_t0930_confirmation_unbound(pd, review_env, monkeypatch, task_only):
    import argparse
    home, tickets = review_env
    board = _fixture_board(pd, home)
    board.LEASES_FILE = home / ".project_manager/.local/worktree-leases.json"
    slot = home / "work/proj_1"
    assert _git(home, "worktree", "add", "-q", str(slot), _BASE_BRANCH).returncode == 0
    board.LEASES_FILE.write_text(json.dumps({"leases": [{"slot": "work/proj_1", "repo": "proj", "session": "main", "state": "leased", "role": "work"}]}), encoding="utf-8")
    ledger_path = board.cluster_ledger_path(_CLUSTER)
    ledger = board.load_cluster(_CLUSTER)
    ledger["branch"] = None
    board.dump_cluster(ledger)
    before = (ledger_path.read_bytes(), board.LEASES_FILE.read_bytes(),
              tuple(path.read_bytes() for path in sorted(tickets.glob("T-*.md"))))
    identity = argparse.Namespace(task="main", repo=None if task_only else "proj", slot=None if task_only else 1)
    with pytest.raises(delegate_errors.DelegateError, match="cluster new"):
        pd._cluster_confirmation_tree(board, _CLUSTER, home=pm_paths.home_at(home), identity=identity)
    assert (ledger_path.read_bytes(), board.LEASES_FILE.read_bytes(),
            tuple(path.read_bytes() for path in sorted(tickets.glob("T-*.md")))) == before


@pytest.mark.parametrize("file_count", [1, 208])
@requires_git
def test_cluster_prompt_size_is_independent_of_patch_and_queries_names_only(
        pd, review_env, file_count):
    home, _ = review_env
    board = _fixture_board(pd, home)
    paths = tuple(f"src/file-{i}.py" for i in range(file_count))
    prompts = []
    for patch in ("AUTO_PATCH_SENTINEL", "AUTO_PATCH_SENTINEL" + "x" * 2_682_927):
        def run(argv, **kwargs):
            if "diff" in argv:
                assert "--name-only" in argv and "-z" in argv, patch[:19]
                return subprocess.CompletedProcess(argv, 0, "\0".join(paths) + "\0", "")
            return subprocess.run(argv, **kwargs)
        review = pd.cluster_review_input(board, _CLUSTER, repo=home, git_run_fn=run)
        prompts.append(pd.build_cluster_review_prompt(
            review, dict.fromkeys(review.members, "본문"), tree=home, focus="중점",
        ))
    assert prompts[0] == prompts[1]
    assert "AUTO_PATCH_SENTINEL" not in prompts[0]
    assert prompts[0].count("### 리뷰 입력 좌표") == 1
    assert prompts[0].count("### 리뷰 입력 확인") == 1
    line = next(line for line in prompts[0].splitlines() if line.startswith("- 변경 파일"))
    assert json.loads(line.split(": ", 1)[1]) == list(paths)


@requires_git
def test_cluster_input_preserves_nul_paths_and_rejects_name_query_failure(pd, review_env):
    home, _ = review_env
    board = _fixture_board(pd, home)
    names = (" 한글 '파일'\n.py", "deleted.py")
    def run(argv, **kwargs):
        if "diff" in argv:
            return subprocess.CompletedProcess(argv, 0, "\0".join(names) + "\0", "")
        return subprocess.run(argv, **kwargs)
    review = pd.cluster_review_input(board, _CLUSTER, repo=home, git_run_fn=run)
    assert review.paths == names
    prompt = pd.build_cluster_review_prompt(review, dict.fromkeys(_MEMBERS, "본문"), tree=home, focus=None)
    line = next(line for line in prompt.splitlines() if line.startswith("- 변경 파일"))
    assert json.loads(line.split(": ", 1)[1]) == list(names)
    def fail(argv, **kwargs):
        if "diff" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "name query failed")
        return subprocess.run(argv, **kwargs)
    with pytest.raises(delegate_errors.DelegateError, match="name query failed"):
        pd.cluster_review_input(board, _CLUSTER, repo=home, git_run_fn=fail)
