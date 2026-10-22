"""T-0587 — raw git cwd-misanchor 판정 seam + 3하네스 배선."""
from __future__ import annotations

from pm_engine.app import engine_guard
from pm_engine.infra import pm_paths


from pm_engine.app import engine_guard
from pm_engine.infra import pm_paths


import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pm_engine.app import hooks
from pm_engine.domain import git_policy, shell_policy

REPO = Path(__file__).resolve().parents[1]
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
CLAUDE_DRIVER = REPO / "templates" / "claude_code" / ".claude" / "pm_orch_claude.py"
CLAUDE_WRAPPER = REPO / "templates" / "claude_code" / ".claude" / "ctx_stop_hook.sh"
OPEN_CORE = REPO / "templates" / "opencode" / ".opencode" / "lib" / "git-anchor-core.cjs"
OPEN_PLUGIN = REPO / "templates" / "opencode" / ".opencode" / "plugins" / "git-anchor.js"
CODEX_RULES = REPO / "templates" / "codex" / ".codex" / "rules" / "default.rules"
CODEX_DISPATCHER = REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py"
CODEX_LIVE_HOOK_FIXTURE = REPO / "tests" / "fixtures" / "codex_0_147_0_live_hook_payloads.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("entrypoint", ["write", "git-status"])
def test_flat_guard_string_and_path_roots_are_equivalent(tmp_path, entrypoint):
    """평면 어댑터는 문자열과 Path root에서 같은 실제 판정을 반환한다."""
    board = _load("board_root_input_contract", BOARD_PY)

    def judge(root):
        if entrypoint == "write":
            return board.judge_tool_input(
                root, "session", str(tmp_path), "Write",
                {"file_path": str(tmp_path / "note.txt"), "content": "note"},
            )
        return board.judge_git_anchor_command(
            root, "session", str(tmp_path), "git status",
        )

    path_judgment = judge(tmp_path)
    assert path_judgment["verdict"] == "ok"
    assert judge(str(tmp_path)) == path_judgment


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")


def _seed_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for rel in ("tests/shared.txt", "templates/shared.txt"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _seed_engine_copy(root: Path, rev: str) -> None:
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "engine_rev.py").write_text(
        f'ENGINE_REV = "{rev}"\n', encoding="utf-8",
    )
    for name in ("board.py", "pm_delegate.py"):
        (tools / name).write_text(
            f'ENGINE_REV = "{rev}"\n', encoding="utf-8",
        )


def _commit_engine_copy(root: Path) -> None:
    """`_seed_engine_copy` 가 쓴 사본을 그 저장소의 커밋으로 확정한다(clean worktree 형상)."""
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "engine copy")


def _shell_slot_path(home: Path, slot: Path, notation: str) -> str:
    """같은 slot을 POSIX/Windows 셸 경로 표기로 반환한다.

    POSIX CI에서도 실제 ``C:\\Users\\x\\slot`` 표기를 판정부까지 통과시키기 위해
    drive-like 상대 alias를 실제 slot symlink로 연결한다. Windows에서는 실제 절대경로의
    native 표기를 그대로 사용한다.
    """
    if notation == "posix":
        return slot.as_posix()
    if os.name == "nt":
        return str(slot)
    windows_alias = home / "C:" / "Users" / "x" / "slot"
    windows_alias.parent.mkdir(parents=True, exist_ok=True)
    windows_alias.symlink_to(slot, target_is_directory=True)
    return r"C:\Users\x\slot"


def _seed_pm_home_state(home: Path) -> None:
    """PM 홈 정체(실 board)를 만드는 최소 상태 — done ticket + status.md."""
    ticket = home / ".project_manager" / "wiki" / "tickets" / "done" / "T-0001-x.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("---\nid: T-0001\n---\n", encoding="utf-8")
    status = home / ".project_manager" / "wiki" / "status.md"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text("status\n", encoding="utf-8")


@pytest.fixture
def topology(tmp_path):
    """서로 다른 git인 PM 홈 + 그 아래 등록 linked slot(양쪽 tests/templates 실재)."""
    home = tmp_path / "pm-home"
    _seed_repo(home)
    _seed_pm_home_state(home)

    source = tmp_path / "product-source"
    _seed_repo(source)
    # ADR-0027 실 형상 — 공유 저장소는 PM 홈 안 `.repos/<repo>.git`(`worktree_pool.bare_repo_path`).
    # 슬롯의 `.git` 포인터가 이 자리를 통해 소유 PM 홈을 선언한다(product git 은 여전히 별개).
    bare = home / ".repos" / "product.git"
    bare.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    slot = home / "work" / "product_1"
    slot.parent.mkdir(parents=True)
    _git(bare, "worktree", "add", "-q", str(slot))
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    # 슬롯 주인 = task t1, 그 task 를 연 하네스 세션 = "S"(판정 호출의 세션 인자).
    ledger.write_text(json.dumps({"leases": [{"slot": "work/product_1", "repo": "product",
                                               "state": "leased", "session": "t1"}],
                                  "tasks": [{"name": "t1", "session": "S"}]}) + "\n", encoding="utf-8")
    return home, slot


@pytest.fixture
def foreign_pm_home(topology, tmp_path):
    """`topology` 와 소유 관계가 전혀 없는 **다른** PM 홈 git 저장소.

    저장소 3개(PM 홈 + 그 등록 slot + 남남인 PM 홈) 형상을 만들어, 정체 클래스만으로는
    짝처럼 보이는 두 사본이 실제로는 서로의 import 사본이 아닌 경우를 재현한다.
    """
    home = tmp_path / "other-pm-home"
    _seed_repo(home)
    _seed_pm_home_state(home)
    return home


@pytest.fixture
def topology_without_home_tests(topology):
    """PM 홈 정체는 유지하되 pytest 대상 ``tests/``만 실재하지 않는 형상."""
    home, slot = topology
    shutil.rmtree(home / "tests")
    return home, slot


def test_cross_existing_pathspec_is_the_only_deny(topology):
    board = _load("git_anchor_board_deny", BOARD_PY)
    home, _slot = topology
    got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "add", "tests/shared.txt"])
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"
    assert "tests/shared.txt" in got["reason"]


def test_false_deny_zero_for_normal_home_slot_and_missing_path(topology, tmp_path):
    board = _load("git_anchor_board_normal", BOARD_PY)
    home, slot = topology
    normal_home = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "commit", "-m", "done", "--", ".project_manager/wiki/status.md"]
    )
    normal_slot = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(slot), ["git", "commit", "-m", "code", "--", "tests/shared.txt"]
    )
    missing = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "add", "tests/not-there.txt"])
    reset_cwd = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(tmp_path / "not-a-repo"), ["git", "commit", "-m", "x"])
    assert normal_home["verdict"] == "warn" and normal_home["cwd_identity"] == "pm-home"
    assert normal_slot["verdict"] == "ok" and normal_slot["cwd_identity"] == "slot"
    assert missing["verdict"] == "warn"
    assert reset_cwd["verdict"] == "warn" and reset_cwd["cwd_identity"] == "non-repo"


def test_read_git_is_ok_and_git_dash_c_changes_anchor(topology):
    board = _load("git_anchor_board_read", BOARD_PY)
    home, slot = topology
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "status"])["verdict"] == "ok"
    got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "-C", str(slot), "add", "tests/shared.txt"])
    assert got["verdict"] == "ok" and got["cwd_identity"] == "slot"


@pytest.mark.parametrize("path_notation", ["posix", "windows"])
def test_shell_parser_uses_worst_git_judgment(topology, path_notation):
    board = _load("git_anchor_board_shell", BOARD_PY)
    home, slot = topology
    shell_slot = _shell_slot_path(home, slot, path_notation)
    got = board.judge_git_anchor_command(home, "S", 
        str(home), "echo pre && git status && git add -- tests/shared.txt"
    )
    assert got["verdict"] == "deny"
    reversed_calls = board.judge_git_anchor_command(home, "S", 
        str(home), "git add -- tests/shared.txt && git status"
    )
    assert reversed_calls["verdict"] == "deny"
    assert board.judge_git_anchor_command(home, "S", str(home), "python3 build.py")["verdict"] == "ok"
    # git 자체는 정상 slot 판정이지만 cwd 잔존 패턴은 별도 warn으로 승격한다.
    changed = board.judge_git_anchor_command(home, "S", 
        str(home), f"cd {shell_slot} && git add -- tests/shared.txt"
    )
    assert changed["verdict"] == "warn"
    assert "대상을 절대경로로 지정하라" in changed["reason"]


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "tests/"],
        ["python3", "-m", "pytest", "tests/"],
    ],
)
def test_engine_guard_allows_pm_home_pytest_when_tests_exist(topology, argv):
    """(a) 임베드 채택자의 PM 홈에 tests/가 실재하면 출하 회귀 명령은 ok."""
    board = _load(f"engine_anchor_home_pytest_{len(argv)}", BOARD_PY)
    home, _slot = topology
    got = engine_guard.judge_engine_invocation(str(home), argv)
    assert got["verdict"] == "ok"
    assert got["cwd_identity"] == "pm-home"
    assert "오앵커 패턴이 아님" in got["reason"]


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "tests/"],
        ["python3", "-m", "pytest", "tests/"],
    ],
)
def test_engine_guard_denies_pm_home_pytest_when_tests_missing(
    topology_without_home_tests, argv,
):
    """(a) PM 홈 cwd에 명시 대상 tests/가 없을 때만 기계 확정 deny."""
    board = _load(f"engine_anchor_missing_home_pytest_{len(argv)}", BOARD_PY)
    home, _slot = topology_without_home_tests
    got = engine_guard.judge_engine_invocation(str(home), argv)
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"
    assert "cwd의 tests/ 디렉터리가 실재하지 않음" in got["reason"]


def test_engine_guard_denies_worktree_board_mutation(topology):
    """(b) board mutation 소유자는 PM 홈이며 canonical 사본 선택은 deny."""
    board = _load("engine_anchor_slot_board_mutation", BOARD_PY)
    home, slot = topology
    _seed_engine_copy(home, "v-old")
    _seed_engine_copy(slot, "v-new")
    got = engine_guard.judge_engine_invocation(
        str(slot), ["python3", ".project_manager/tools/board.py", "complete", "T-0001"],
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "slot"
    assert "board mutation 'complete'" in got["reason"]
    assert str(slot / ".project_manager" / "tools" / "board.py") in got["reason"]
    assert "board mutation은 PM 홈 사본" in got["reason"]
    via_shell = board.judge_git_anchor_command(home, "S", 
        str(slot), "python3 .project_manager/tools/board.py complete T-0001",
    )
    assert via_shell["verdict"] == "deny"
    repeated_slash = board.judge_git_anchor_command(home, "S", 
        str(slot), "python3 .project_manager//tools/board.py claim T-1",
    )
    assert repeated_slash["verdict"] == "deny"

    # cwd가 아니라 선택한 script 사본이 엔진 repo 앵커를 정한다.
    slot_script = slot / ".project_manager" / "tools" / "board.py"
    from_home = engine_guard.judge_engine_invocation(
        str(home), ["python3", str(slot_script), "complete", "T-0001"],
    )
    assert from_home["verdict"] == "deny"
    home_script = home / ".project_manager" / "tools" / "board.py"
    pm_owned = engine_guard.judge_engine_invocation(
        str(slot), ["python3", str(home_script), "complete", "T-0001"],
    )
    assert pm_owned["verdict"] == "ok"


def test_relative_engine_call_warns_on_same_rev_different_bytes(topology):
    """(c) 같은 release rev여도 도구 bytes가 다르면 stale import를 검출한다.

    T-0800 이후 이 진단은 worktree 사본이 **커밋으로 확정된** 형상에서만 나온다 —
    fixture 도 그 형상(slot 사본 커밋)으로 고정한다.
    """
    board = _load("engine_anchor_relative_copy", BOARD_PY)
    home, slot = topology
    _seed_engine_copy(home, "v1.7.5")
    _seed_engine_copy(slot, "v1.7.5")
    _commit_engine_copy(slot)
    home_script = _engine_script(home)
    slot_script = _engine_script(slot)
    home_script.write_text('ENGINE_REV = "v1.7.5"\nIMPORT_COPY = True\n', encoding="utf-8")
    assert (
        (home / ".project_manager" / "tools" / "engine_rev.py").read_bytes()
        == (slot / ".project_manager" / "tools" / "engine_rev.py").read_bytes()
    )
    assert home_script.read_bytes() != slot_script.read_bytes()
    got = engine_guard.judge_engine_invocation(
        str(slot), ["python3", ".project_manager/tools/pm_delegate.py", "status"],
    )
    assert got == _expected_engine_verdict(
        cwd_identity="slot", script_path=slot_script, script_root=slot,
        script_identity="slot", other_path=home_script,
        copy_state=(
            f"도구 파일 sha256 다름({_sha256(slot_script)} != {_sha256(home_script)}) — "
            f"worktree 사본 {slot_script} 은 커밋 상태(clean) — "
            "stale import 사본 — 게이트 판정은 canonical 로"
        ),
    )
    via_shell = board.judge_git_anchor_command(home, "S", 
        str(slot), "python3 .project_manager/tools/pm_delegate.py status",
    )
    assert via_shell == got


class _GitStatusCountingRunner:
    """실 git 을 그대로 실행하되 방향 판정용 ``status`` 조회만 세는 seam.

    합성은 ``fail_mode="oserror"`` 하나뿐이다 — git 실행 파일 부재는 프로세스 전역 PATH 를
    바꿔야 재현되고, 그러면 정체 해소용 ``rev-parse`` 까지 같이 죽어 이 형상(정체는 해소되는데
    방향 조회만 실패) 자체가 사라진다. 반환코드 실패는 합성하지 않고 실제 git 실패
    경계(`_corrupt_git_index`)를 쓴다.
    """

    def __init__(self, fail_mode: str = "") -> None:
        self.status_calls: list[list[str]] = []
        self._fail_mode = fail_mode

    def __call__(self, argv, **kwargs):
        values = [str(value) for value in argv]
        if "status" in values and "--porcelain" in values:
            self.status_calls.append(values)
            if self._fail_mode == "oserror":
                raise OSError("fixture: git 실행 불가")
        return subprocess.run(values, **kwargs)


def _corrupt_git_index(root: Path) -> None:
    """실제 git 실패 경계 — `root` worktree 의 index 파일을 깨뜨린다.

    이 상태에서 `git status` 는 rc≠0(fatal)로 실패하지만 `rev-parse`·`worktree list` 는 계속
    성공한다. 그래서 정체 해소는 그대로 통과하고 방향 판정 조회만 불가가 된다.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    (Path(result.stdout.strip()) / "index").write_bytes(b"not-a-git-index")


def _sha256(path: Path) -> str:
    """기대 sha 는 프로덕션 `_file_sha256` 을 쓰지 않고 테스트가 독립 계산한다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_engine_verdict(
    *, cwd_identity: str, script_path: Path, script_root: Path, script_identity: str,
    other_path: Path, copy_state: str,
) -> dict[str, str]:
    """상대경로 엔진 호출 warn 의 **반환 dict 전문**을 테스트가 독립 전사한다.

    프로덕션 문자열을 import 하지 않으므로, 가드가 문구를 바꾸거나 방향 단정을 뒤에 덧붙이면
    전체 동등 비교가 red 가 된다(부분 문자열 ``in`` 단언이 놓치던 drift).
    """
    return {
        "verdict": "warn",
        "cwd_identity": cwd_identity,
        "reason": (
            f"상대경로 엔진 호출의 실행 사본={script_path}; 이 사본의 repo 앵커={script_root} "
            f"({script_identity}); board mutation은 PM 홈 사본으로 실행하라; "
            f"함께 존재하는 다른 사본={other_path}; {copy_state}"
        ),
    }


def _undecided_copy_state(current_sha: str, other_sha: str, detail: str) -> str:
    """sha 불일치 + **방향 판정 불가** 문구 전문(테스트 독립 전사)."""
    return (
        f"도구 파일 sha256 다름({current_sha} != {other_sha}) — "
        f"사본 내용이 다름 — 어느 쪽이 최신인지 판정 불가({detail}) — "
        "두 사본 경로를 직접 대조하라"
    )


def _engine_script(root: Path) -> Path:
    """비교 대상 도구 파일(엔진 사본의 `pm_delegate.py`) 경로."""
    return root / ".project_manager" / "tools" / "pm_delegate.py"


def _mismatched_engine_copies(home: Path, slot: Path, *, commit_slot: bool) -> None:
    """두 사본의 ``pm_delegate.py`` bytes 를 서로 다르게 만든다.

    ``commit_slot`` 이 참이면 worktree 사본을 커밋으로 확정(clean)하고 PM 홈 사본만 바꾼다 —
    진짜 stale import 형상이다. 거짓이면 worktree 사본이 미커밋(편집 중) 형상이 된다.
    """
    _seed_engine_copy(home, "v1.7.9")
    _seed_engine_copy(slot, "v1.7.9")
    if commit_slot:
        _commit_engine_copy(slot)
        _engine_script(home).write_text(
            'ENGINE_REV = "v1.7.9"\nIMPORT_COPY = True\n', encoding="utf-8",
        )
    else:
        _engine_script(slot).write_text(
            'ENGINE_REV = "v1.7.9"\nWIP = True\n', encoding="utf-8",
        )


def _ignore_engine_copy(root: Path) -> None:
    """`root` 저장소가 엔진 사본 경로를 `.gitignore` 로 무시하게 만든다(무시 규칙은 커밋)."""
    (root / ".gitignore").write_text(".project_manager/\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "ignore engine copy")


@pytest.mark.parametrize("dirt", ["untracked", "tracked-modified", "gitignored"])
def test_sha_mismatch_with_dirty_worktree_copy_does_not_claim_stale_import(
    topology, dirt,
):
    """worktree 사본이 미커밋이면 방향을 단정하지 않는다(역방향 오안내 차단).

    ``gitignored`` 는 사본이 `.gitignore` 에 걸려 추적되지 않는 실제 형상이다 — ``--ignored``
    없이 조회하면 출력이 비어 clean(=stale import)으로 오독된다.
    """
    board = _load(f"engine_anchor_dirty_worktree_{dirt}", BOARD_PY)
    home, slot = topology
    if dirt == "untracked":
        _mismatched_engine_copies(home, slot, commit_slot=False)
    elif dirt == "tracked-modified":
        _seed_engine_copy(home, "v1.7.9")
        _seed_engine_copy(slot, "v1.7.9")
        _commit_engine_copy(slot)
        _engine_script(slot).write_text(
            'ENGINE_REV = "v1.7.9"\nWIP = True\n', encoding="utf-8",
        )
    else:
        _ignore_engine_copy(slot)
        _mismatched_engine_copies(home, slot, commit_slot=False)
    home_script = _engine_script(home)
    slot_script = _engine_script(slot)
    assert home_script.read_bytes() != slot_script.read_bytes()

    runner = _GitStatusCountingRunner()
    got = engine_guard.judge_engine_invocation(
        str(slot), ["python3", ".project_manager/tools/pm_delegate.py", "status"],
        runner=runner,
    )
    assert got == _expected_engine_verdict(
        cwd_identity="slot", script_path=slot_script, script_root=slot,
        script_identity="slot", other_path=home_script,
        copy_state=(
            f"도구 파일 sha256 다름({_sha256(slot_script)} != {_sha256(home_script)}) — "
            f"worktree 사본 {slot_script} 이 미커밋 변경(편집 중·미확정) — "
            "어느 쪽이 최신인지 단정 불가; worktree 를 커밋으로 확정한 뒤 다시 대조하라"
        ),
    )
    assert len(runner.status_calls) == 1


def test_sha_mismatch_with_clean_worktree_copy_reports_stale_import(topology):
    """worktree 사본이 커밋 상태면 기존 stale import 안내가 그대로 나온다(PM 홈 cwd)."""
    board = _load("engine_anchor_clean_worktree_from_home", BOARD_PY)
    home, slot = topology
    _mismatched_engine_copies(home, slot, commit_slot=True)
    home_script = _engine_script(home)
    slot_script = _engine_script(slot)
    assert home_script.read_bytes() != slot_script.read_bytes()

    runner = _GitStatusCountingRunner()
    got = engine_guard.judge_engine_invocation(
        str(home), ["python3", ".project_manager/tools/pm_delegate.py", "status"],
        runner=runner,
    )
    assert got == _expected_engine_verdict(
        cwd_identity="pm-home", script_path=home_script, script_root=home,
        script_identity="pm-home", other_path=slot_script,
        copy_state=(
            f"도구 파일 sha256 다름({_sha256(home_script)} != {_sha256(slot_script)}) — "
            f"worktree 사본 {slot_script} 은 커밋 상태(clean) — "
            "stale import 사본 — 게이트 판정은 canonical 로"
        ),
    )
    assert len(runner.status_calls) == 1


def test_freshness_git_query_happens_only_when_sha_differs(topology):
    """성능 제약: 사본이 같으면 git 조회 0회, 다를 때만 1회."""
    board = _load("engine_anchor_freshness_query_count", BOARD_PY)
    home, slot = topology
    _seed_engine_copy(home, "v1.7.9")
    _seed_engine_copy(slot, "v1.7.9")
    _commit_engine_copy(slot)
    home_script = _engine_script(home)
    slot_script = _engine_script(slot)
    assert home_script.read_bytes() == slot_script.read_bytes()

    same = _GitStatusCountingRunner()
    got_same = engine_guard.judge_engine_invocation(
        str(slot), ["python3", ".project_manager/tools/pm_delegate.py", "status"],
        runner=same,
    )
    assert got_same == _expected_engine_verdict(
        cwd_identity="slot", script_path=slot_script, script_root=slot,
        script_identity="slot", other_path=home_script,
        copy_state=f"도구 파일 sha256 동일({_sha256(slot_script)})",
    )
    assert same.status_calls == []

    home_script.write_text(
        'ENGINE_REV = "v1.7.9"\nIMPORT_COPY = True\n', encoding="utf-8",
    )
    differs = _GitStatusCountingRunner()
    got_differs = engine_guard.judge_engine_invocation(
        str(slot), ["python3", ".project_manager/tools/pm_delegate.py", "status"],
        runner=differs,
    )
    assert got_differs == _expected_engine_verdict(
        cwd_identity="slot", script_path=slot_script, script_root=slot,
        script_identity="slot", other_path=home_script,
        copy_state=(
            f"도구 파일 sha256 다름({_sha256(slot_script)} != {_sha256(home_script)}) — "
            f"worktree 사본 {slot_script} 은 커밋 상태(clean) — "
            "stale import 사본 — 게이트 판정은 canonical 로"
        ),
    )
    assert differs.status_calls == [
        ["git", "-C", str(slot), "status", "--porcelain", "--ignored", "--",
         ".project_manager/tools/pm_delegate.py"],
    ]


@pytest.mark.parametrize("fail_mode", ["corrupt-index", "oserror"])
def test_sha_mismatch_with_unreadable_git_state_does_not_assert_direction(
    topology, fail_mode,
):
    """git 상태를 못 읽으면 어느 쪽이 최신인지 단정하지 않고 두 경로를 남긴다."""
    board = _load(f"engine_anchor_freshness_git_fail_{fail_mode}", BOARD_PY)
    home, slot = topology
    _mismatched_engine_copies(home, slot, commit_slot=False)
    home_script = _engine_script(home)
    slot_script = _engine_script(slot)
    if fail_mode == "corrupt-index":
        _corrupt_git_index(slot)
        runner = _GitStatusCountingRunner()
    else:
        runner = _GitStatusCountingRunner(fail_mode="oserror")

    got = engine_guard.judge_engine_invocation(
        str(slot), ["python3", ".project_manager/tools/pm_delegate.py", "status"],
        runner=runner,
    )
    assert got == _expected_engine_verdict(
        cwd_identity="slot", script_path=slot_script, script_root=slot,
        script_identity="slot", other_path=home_script,
        copy_state=_undecided_copy_state(
            _sha256(slot_script), _sha256(home_script),
            f"worktree {slot} git 상태 조회 실패",
        ),
    )
    assert len(runner.status_calls) == 1


def test_cross_owner_copies_are_undecided_without_git_query(topology, foreign_pm_home):
    """소유 PM 홈이 다른 두 사본은 짝이 아니다 — git 조회 0회로 방향을 단정하지 않는다.

    정체 클래스만 보면 pm-home + slot 이라 짝처럼 보이지만, 그 slot 을 소유하는 PM 홈은
    사본이 있는 PM 홈이 아니다. 서로 남남인 저장소라 어느 쪽도 상대의 import 사본이 아니다.
    """
    board = _load("engine_anchor_cross_owner", BOARD_PY)
    home, slot = topology
    _seed_engine_copy(slot, "v1.7.9")
    _commit_engine_copy(slot)
    _seed_engine_copy(foreign_pm_home, "v1.7.9")
    foreign_script = _engine_script(foreign_pm_home)
    foreign_script.write_text(
        'ENGINE_REV = "v1.7.9"\nIMPORT_COPY = True\n', encoding="utf-8",
    )
    slot_script = _engine_script(slot)
    relative_call = os.path.relpath(foreign_script, slot)

    runner = _GitStatusCountingRunner()
    got = engine_guard.judge_engine_invocation(
        str(slot), ["python3", relative_call, "status"], runner=runner,
    )
    assert got == _expected_engine_verdict(
        cwd_identity="slot", script_path=foreign_script, script_root=foreign_pm_home,
        script_identity="pm-home", other_path=slot_script,
        copy_state=_undecided_copy_state(
            _sha256(foreign_script), _sha256(slot_script),
            f"PM 홈–worktree 짝 아님: worktree {slot} 의 소유 PM 홈={home}, "
            f"상대 사본의 PM 홈={foreign_pm_home}",
        ),
    )
    assert runner.status_calls == []


def test_freshness_without_home_worktree_pair_is_undecided_without_git_query(topology):
    """정체 클래스가 PM 홈–worktree 짝을 이루지 않으면 git 조회 없이 판정 불가로 남긴다."""
    board = _load("engine_anchor_freshness_unpaired", BOARD_PY)
    home, slot = topology
    _mismatched_engine_copies(home, slot, commit_slot=False)
    relative = Path(".project_manager") / "tools" / "pm_delegate.py"

    runner = _GitStatusCountingRunner()
    note = hooks._engine_copy_freshness(
        ("foreign", slot, slot / relative, None),
        ("pm-home", home, home / relative, home),
        relative, runner=runner,
    )
    assert note == (
        "사본 내용이 다름 — 어느 쪽이 최신인지 판정 불가"
        "(PM 홈–worktree 짝 아님: foreign vs pm-home) — 두 사본 경로를 직접 대조하라"
    )
    assert runner.status_calls == []


def test_unexpanded_engine_path_reports_unknown_without_changing_deny_axis(topology):
    """F-008: 미확장 parameter로 사본·앵커를 조립하지 않고 deny 강도는 보존한다."""
    board = _load("engine_anchor_unexpanded_path", BOARD_PY)
    home, slot = topology

    for token in ("$PMH", "${PMH}", "$1"):
        got = board.judge_git_anchor_command(home, "S", 
            str(home),
            f"python3 {token}/.project_manager/tools/pm_delegate.py status",
        )
        assert got["verdict"] == "warn"
        assert f"미확장 토큰={token}" in got["reason"]
        assert "확장 불가 — 실제 사본 미상" in got["reason"]
        assert "실행 사본=" not in got["reason"]
        assert "repo 앵커=" not in got["reason"]

    denied = board.judge_git_anchor_command(home, "S", 
        str(slot), "python3 $PMH/.project_manager/tools/board.py claim T-0001",
    )
    assert denied["verdict"] == "deny"
    assert denied["cwd_identity"] == "slot"
    assert "확장 불가 — 실제 사본 미상" in denied["reason"]
    assert "실행 사본=" not in denied["reason"]
    assert "repo 앵커=" not in denied["reason"]


def test_relative_engine_copy_hash_read_failure_is_fail_soft(
    monkeypatch, topology, capsys,
):
    """도구 사본을 읽지 못해도 판정 seam은 warn dict를 반환해 훅 rc0을 보존한다."""
    board = _load("engine_anchor_copy_hash_failure", BOARD_PY)
    home, slot = topology
    _seed_engine_copy(home, "v1.7.5")
    _seed_engine_copy(slot, "v1.7.5")

    def unreadable(_path):
        raise OSError("fixture unreadable")

    # 사본 해시 판독은 공유 읽기 seam 을 지난다([[T-0729]]) — 주입도 그 자리에 건다.
    monkeypatch.setattr(board.fs, "read_bytes_shared", unreadable)
    got = engine_guard.judge_engine_invocation(
        str(slot), ["python3", ".project_manager/tools/pm_delegate.py", "status"],
    )
    assert got["verdict"] == "warn"
    assert "sha256 비교 불가(파일 읽기 실패)" in got["reason"]

    driver = _load("engine_anchor_copy_hash_failure_hook", CLAUDE_DRIVER)
    monkeypatch.setattr(driver, "_load_board", lambda _root: board)
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(slot),
        "tool_input": {
            "command": "python3 .project_manager/tools/pm_delegate.py status",
        },
    }
    monkeypatch.setattr(driver.sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = driver.run_git_anchor_hook(home)
    hook_output = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "sha256 비교 불가(파일 읽기 실패)" in (
        hook_output["hookSpecificOutput"]["additionalContext"]
    )


def test_absolute_engine_call_is_ok(topology):
    """(d) 명시 사본의 비-mutation 엔진 호출은 정체가 확정되므로 ok."""
    board = _load("engine_anchor_absolute_copy", BOARD_PY)
    home, slot = topology
    _seed_engine_copy(home, "v1")
    _seed_engine_copy(slot, "v1")
    script = slot / ".project_manager" / "tools" / "pm_delegate.py"
    got = engine_guard.judge_engine_invocation(str(home), ["python3", str(script), "status"])
    assert got["verdict"] == "ok"
    assert f"절대경로 엔진 사본={script}" in got["reason"]
    assert f"repo 앵커={slot} (slot)" in got["reason"]


def test_mixed_git_and_engine_calls_use_strongest_judgment(topology):
    """(e) PM 홈에 tests/가 실재하면 혼합 호출에서도 false-deny가 없다."""
    board = _load("engine_anchor_mixed_rank", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(home, "S", 
        str(home), "git status && python3 -m pytest tests/",
    )
    assert got["verdict"] == "ok"
    assert "호출 1 [pm-home/ok]" in got["reason"]
    assert "호출 2 [pm-home/ok]" in got["reason"]


def test_mixed_git_and_engine_calls_promote_missing_tests_deny(
    topology_without_home_tests,
):
    """(e) tests/ 부재가 실측된 형상에서는 혼합 호출의 deny가 최강 판정이다."""
    board = _load("engine_anchor_mixed_missing_rank", BOARD_PY)
    home, _slot = topology_without_home_tests
    got = board.judge_git_anchor_command(home, "S", 
        str(home), "git status && python3 -m pytest tests/",
    )
    assert got["verdict"] == "deny"
    assert "호출 2 [pm-home/deny]" in got["reason"]


def test_adjacent_identical_warns_render_as_one_range(topology):
    board = _load("git_anchor_adjacent_warn_range", BOARD_PY)
    home, _slot = topology
    warning = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "commit", "-m", "repeat"],
    )["reason"]

    got = board.judge_git_anchor_command(home, "S", 
        str(home),
        "git status && git commit -m repeat && git commit -m repeat "
        "&& git commit -m repeat && git commit -m repeat",
    )

    assert got["verdict"] == "warn"
    assert (
        f"호출 2–5 [pm-home/warn] ×4: {warning}"
        in got["reason"]
    )
    assert got["reason"].count(warning) == 1


def test_reason_rendering_does_not_rerun_judgments(monkeypatch, topology):
    board = _load("git_anchor_render_single_pass", BOARD_PY)
    home, _slot = topology
    judged: list[tuple[str, tuple[str, ...]]] = []

    def fake_judge(_root, _session, cwd, argv, *, runner, cwd_certain):
        judged.append((cwd, tuple(argv)))
        return {"verdict": "warn", "cwd_identity": "pm-home", "reason": "repeat"}

    monkeypatch.setattr(engine_guard, "judge_git_anchor", fake_judge)
    got = board.judge_git_anchor_command(home, "S", 
        str(home), "git status && git status && git status && git status",
    )

    assert len(judged) == 4
    assert got["reason"] == "호출 1–4 [pm-home/warn] ×4: repeat"


def test_strongest_renderer_filters_lower_verdicts_without_closing_call_gaps(
    monkeypatch, topology,
):
    board = _load("git_anchor_strongest_render_runs", BOARD_PY)
    home, _slot = topology
    verdicts = iter([
        ("ok", "ok"), ("ok", "ok"), ("ok", "ok"), ("ok", "ok"),
        ("warn", "repeat"), ("warn", "repeat"),
    ])

    def filtered_judge(_root, _session, cwd, argv, *, runner, cwd_certain):
        verdict, reason = next(verdicts)
        return {"verdict": verdict, "cwd_identity": "pm-home", "reason": reason}

    monkeypatch.setattr(engine_guard, "judge_git_anchor", filtered_judge)
    filtered = board.judge_git_anchor_command(home, "S", 
        str(home), "git status && git status && git status && git status && git status && git status",
    )
    assert filtered["reason"] == "호출 5–6 [pm-home/warn] ×2: repeat"
    assert "ok" not in filtered["reason"]

    gap_verdicts = iter([("warn", "repeat"), ("ok", "ok"), ("warn", "repeat")])

    def gapped_judge(_root, _session, cwd, argv, *, runner, cwd_certain):
        verdict, reason = next(gap_verdicts)
        return {"verdict": verdict, "cwd_identity": "pm-home", "reason": reason}

    monkeypatch.setattr(engine_guard, "judge_git_anchor", gapped_judge)
    gapped = board.judge_git_anchor_command(home, "S", 
        str(home), "git status && git status && git status",
    )
    assert gapped["reason"] == (
        "호출 1 [pm-home/warn]: repeat | 호출 3 [pm-home/warn]: repeat"
    )


def test_nonadjacent_or_different_warns_remain_separate(topology):
    board = _load("git_anchor_warn_run_boundaries", BOARD_PY)
    home, _slot = topology
    commit_warning = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "commit", "-m", "repeat"],
    )["reason"]
    missing_path_warning = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "add", "tests/not-there.txt"],
    )["reason"]

    nonadjacent = board.judge_git_anchor_command(home, "S", 
        str(home), "git commit -m repeat && git status && git commit -m repeat",
    )
    assert nonadjacent["reason"] == (
        f"호출 1 [pm-home/warn]: {commit_warning} | "
        f"호출 3 [pm-home/warn]: {commit_warning}"
    )

    different_reasons = board.judge_git_anchor_command(home, "S", 
        str(home), "git commit -m repeat && git add tests/not-there.txt",
    )
    assert different_reasons["reason"] == (
        f"호출 1 [pm-home/warn]: {commit_warning} | "
        f"호출 2 [pm-home/warn]: {missing_path_warning}"
    )


def test_repeated_special_deny_keeps_code_but_mixed_denies_drop_it(
    topology_without_home_tests,
):
    board = _load("git_anchor_deny_range_code", BOARD_PY)
    home, slot = topology_without_home_tests
    special = engine_guard.judge_engine_invocation(
        str(home), ["python3", "-m", "pytest", "tests/"],
    )

    repeated = board.judge_git_anchor_command(home, "S", 
        str(home), "python3 -m pytest tests/ && python3 -m pytest tests/",
    )
    assert repeated["verdict"] == "deny"
    assert repeated["code"] == "pm-home-relative-pytest-tests-missing"
    assert repeated["reason"] == (
        f"호출 1–2 [pm-home/deny] ×2: {special['reason']}"
    )

    _seed_engine_copy(home, "v1")
    _seed_engine_copy(slot, "v1")
    mixed = board.judge_git_anchor_command(home, "S", 
        str(home),
        f"python3 {slot / '.project_manager/tools/board.py'} complete T-0001 "
        "&& python3 -m pytest tests/",
    )
    assert mixed["verdict"] == "deny"
    assert "code" not in mixed
    assert "호출 1 [pm-home/deny]" in mixed["reason"]
    assert "호출 2 [pm-home/deny]" in mixed["reason"]


@pytest.mark.parametrize(
    ("separator", "separator_id"),
    [(" && ", "and"), ("; ", "semicolon"), ("\n", "newline")],
    ids=["and", "semicolon", "newline"],
)
@pytest.mark.parametrize("path_notation", ["posix", "windows"])
def test_persistent_cd_pattern_warns_with_executable_prescription(
    topology, path_notation, separator, separator_id,
):
    """(f) 셸 cwd 잔존 패턴은 실행 종류와 무관하게 warn한다."""
    board = _load(
        f"engine_anchor_persistent_cd_{path_notation}_{separator_id}", BOARD_PY,
    )
    home, slot = topology
    shell_slot = _shell_slot_path(home, slot, path_notation)
    command = f"echo pre{separator}cd {shell_slot}{separator}python3 build.py"
    got = board.judge_git_anchor_command(home, "S", str(home), command)
    assert got["verdict"] == "warn"
    assert "대상을 절대경로로 지정하라" in got["reason"]
    assert "cd가 꼭 필요하면 이 호출 안에서만 사용" in got["reason"]
    assert "다음 호출은 cwd를 가정하지 마라" in got["reason"]
    assert "workdir 파라미터" not in got["reason"]

    contextual = board.judge_git_anchor_command(home, "S", 
        str(home), f"echo pre{separator}cd {shell_slot}{separator}git status",
    )
    assert contextual["verdict"] == "warn"
    assert "호출 2 [non-repo/warn]" in contextual["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "docker run img pytest tests/",
        "npm test -- --grep 'cd foo && bar'",
        "(cd /tmp && ls)",
        "cat <<'EOF'\ncd /tmp && ls\nEOF",
    ],
    ids=["docker", "npm", "subshell", "heredoc"],
)
def test_prefilter_false_positive_candidates_remain_ok(topology, command):
    """넓어진 선필터가 argv data·서브셸·heredoc을 cwd 잔존으로 오판하지 않는다."""
    board = _load(f"engine_anchor_prefilter_false_positive_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor_command(home, "S", str(home), command)["verdict"] == "ok"


def test_existing_git_mutation_deny_is_unchanged(topology):
    """(g) 엔진 합류 뒤에도 기존 PM 홈 cross-pathspec git deny는 불변."""
    board = _load("engine_anchor_git_regression", BOARD_PY)
    home, _slot = topology
    direct = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "add", "tests/shared.txt"])
    shell = board.judge_git_anchor_command(home, "S", str(home), "git add tests/shared.txt")
    assert shell == direct
    assert shell["verdict"] == "deny"


def test_posix_escaped_path_operand_keeps_shell_meaning(topology):
    """Windows 표기 정규화가 POSIX ``\\`` escape를 경로 구분자로 바꾸지 않는다."""
    board = _load("engine_anchor_posix_escaped_path", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(home, "S", 
        str(home), r"git add t\ests/shared.txt",
    )
    assert got["verdict"] == "deny"
    assert "tests/shared.txt" in got["reason"]


@pytest.mark.parametrize(
    ("command", "identity"),
    [
        ("cd /nonexistent || git add tests/shared.txt", "pm-home"),
        ("cd {slot} | git add tests/shared.txt", "pm-home"),
        ("false && cd {slot}; git add tests/shared.txt", "pm-home"),
        ("/usr/bin/git add tests/shared.txt", "pm-home"),
    ],
)
def test_shell_control_flow_preserves_actual_git_cwd(topology, command, identity):
    board = _load(f"git_anchor_shell_{identity}_{abs(hash(command))}", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(home, "S", 
        str(home), command.format(home=home, slot=slot)
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == identity


@pytest.mark.parametrize("path_notation", ["posix", "windows"])
def test_shell_if_and_multiple_git_calls_keep_context(topology, path_notation):
    board = _load("git_anchor_shell_if_multi", BOARD_PY)
    home, slot = topology
    shell_slot = _shell_slot_path(home, slot, path_notation)
    conditional = board.judge_git_anchor_command(home, "S", 
        str(home), f"if cd {shell_slot}; then git add tests/shared.txt; fi"
    )
    assert conditional["verdict"] == "ok"
    assert conditional["cwd_identity"] == "slot"

    multiple = board.judge_git_anchor_command(home, "S", 
        str(home), f"git -C {shell_slot} commit -m slot && git commit -m home"
    )
    assert multiple["verdict"] == "warn"
    assert "호출 2 [pm-home/warn]" in multiple["reason"]

    reversed_calls = board.judge_git_anchor_command(home, "S", 
        str(home), f"git commit -m home && git -C {shell_slot} commit -m slot"
    )
    assert reversed_calls["verdict"] == "warn"
    assert "호출 1 [pm-home/warn]" in reversed_calls["reason"]


def test_dynamic_cd_is_ambiguous_and_blocks_only_destructive_forms(topology):
    """대상 미확정 호출은 비파괴면 warn 이고, 파괴 형태면 슬롯·공용 저장소일 수 있어 거부한다."""
    board = _load("git_anchor_shell_dynamic_cd", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(home, "S", 
        str(home), "cd $TARGET; git add tests/shared.txt"
    )
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]
    destructive = board.judge_git_anchor_command(home, "S", str(home), "cd $TARGET; git branch -D main")
    assert destructive["verdict"] == "deny"
    assert "대상을 정적으로 정할 수 없어 슬롯·공용 저장소일 수 있음" in destructive["reason"]
    non_destructive = board.judge_git_anchor_command(home, "S", str(home), "cd $TARGET; git push origin main")
    assert non_destructive["verdict"] == "warn"


@pytest.mark.parametrize(
    "command",
    [
        "cd {slot} >/dev/null && git add tests/shared.txt",
        "cd {slot} 2>/dev/null && git add tests/shared.txt",
        "cd -P {slot} && git add tests/shared.txt",
        "pushd {slot} && git add tests/shared.txt",
        "pushd {slot} >/dev/null; git add tests/shared.txt",
        "pushd {slot}; popd; git add tests/shared.txt",
    ],
)
def test_unmodeled_cwd_changers_are_ambiguity_warn(topology, command):
    board = _load(f"git_anchor_shell_unknown_cwd_{abs(hash(command))}", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(home, "S", str(home), command.format(slot=slot))
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]


def test_unexecuted_cwd_changer_preserves_proven_home_anchor(topology):
    board = _load("git_anchor_shell_unexecuted_pushd", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(home, "S", 
        str(home), f"false && pushd {slot}; git add tests/shared.txt"
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF' > NOTES.md\n# how to stage\ngit add tests/shared.txt\nEOF",
        "cat <<EOF > NOTES.md\ngit add tests/shared.txt\nEOF",
        "cat <<-EOF >> NOTES.md\n\tgit add tests/shared.txt\n\tEOF",
        "git commit -F - <<EOF\nmessage: git add tests/shared.txt\nEOF",
    ],
)
def test_heredoc_body_git_is_data_and_never_denies(topology, command):
    board = _load(f"git_anchor_heredoc_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(home, "S", str(home), command)
    assert got["verdict"] in {"ok", "warn"}


def test_real_commands_around_heredoc_are_still_judged(topology):
    board = _load("git_anchor_heredoc_surrounding", BOARD_PY)
    home, _slot = topology
    before = board.judge_git_anchor_command(home, "S", 
        str(home), "git add tests/shared.txt; cat <<EOF\ngit status\nEOF"
    )
    after = board.judge_git_anchor_command(home, "S", 
        str(home), "cat <<EOF\ngit status\nEOF\ngit add tests/shared.txt"
    )
    assert before["verdict"] == "deny"
    assert after["verdict"] == "deny"


@pytest.mark.parametrize("command", [
    "echo '<<EOF'\ngit add tests/shared.txt\nEOF",
    "echo ok # <<EOF\ngit add tests/shared.txt\nEOF",
])
def test_fake_heredoc_does_not_hide_real_git(topology, command):
    board = _load(f"git_anchor_fake_heredoc_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor_command(home, "S", str(home), command)["verdict"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git add tests/shared.txt &",
        "{ git add tests/shared.txt; }",
    ],
)
def test_unmodeled_shell_shapes_surface_ambiguity_warn(topology, command):
    board = _load(f"git_anchor_unknown_shape_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(home, "S", str(home), command)
    assert got["verdict"] == "warn"
    assert "정적으로" in got["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "echo git add tests/shared.txt",
        "printf %s git add tests/shared.txt",
        "# git add tests/shared.txt",
    ],
)
def test_git_data_arguments_and_comments_are_not_commands(topology, command):
    board = _load(f"git_anchor_shell_data_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(home, "S", str(home), command)
    assert got["verdict"] == "ok"


@pytest.mark.parametrize(
    "command",
    [
        "while false; do git add tests/shared.txt; done",
        "for x in one; do git add tests/shared.txt; done",
    ],
)
def test_unsupported_loop_control_is_ambiguity_warn(topology, command):
    board = _load(f"git_anchor_shell_loop_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(home, "S", str(home), command)
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]


@pytest.mark.parametrize("path_notation", ["posix", "windows"])
def test_newline_is_a_sequential_shell_boundary(topology, path_notation):
    board = _load("git_anchor_shell_newline", BOARD_PY)
    home, slot = topology
    shell_slot = _shell_slot_path(home, slot, path_notation)
    got = board.judge_git_anchor_command(home, "S", 
        str(home), f"git -C {shell_slot} status\ngit add tests/shared.txt"
    )
    assert got["verdict"] == "deny"
    assert "호출 2 [pm-home/deny]" in got["reason"]


def test_if_else_tracks_failed_cd_branch_cwd(topology):
    board = _load("git_anchor_shell_else", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(home, "S", 
        str(home),
        "if cd /nonexistent; then true; else git add tests/shared.txt; fi",
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "add", "tests/../templates/shared.txt"],
        ["git", "add", ":(top)tests/shared.txt"],
        ["git", "restore", "tests/shared.txt"],
        ["git", "commit", "-m", "path", "tests/shared.txt"],
    ],
)
def test_git_pathspec_normalization_and_no_separator_forms_deny(topology, argv):
    board = _load(f"git_anchor_pathspec_{abs(hash(tuple(argv)))}", BOARD_PY)
    home, _slot = topology
    got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), argv)
    assert got["verdict"] == "deny"


def test_shell_redirection_targets_are_not_git_pathspecs(topology):
    board = _load("git_anchor_shell_redirection", BOARD_PY)
    home, _slot = topology
    commit = board.judge_git_anchor_command(home, "S", 
        str(home), "git commit -m msg > tests/shared.txt"
    )
    add = board.judge_git_anchor_command(home, "S", 
        str(home), "git add src/app.py 2> tests/shared.txt"
    )
    still_dangerous = board.judge_git_anchor_command(home, "S", 
        str(home), "git add tests/shared.txt > /dev/null"
    )
    assert commit["verdict"] == "warn"
    assert add["verdict"] == "warn"
    assert still_dangerous["verdict"] == "deny"


@pytest.mark.parametrize("command", [
    "git>/tmp/out add tests/shared.txt",
    "git</dev/null add tests/shared.txt",
    "/usr/bin/git>out add tests/shared.txt",
])
def test_attached_redirection_still_recognizes_git(topology, command):
    board = _load(f"git_anchor_attached_redirect_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor_command(home, "S", str(home), command)["verdict"] == "deny"


def test_pathspecs_resolve_from_invocation_cwd(topology):
    board = _load("git_anchor_pathspec_cwd", BOARD_PY)
    home, _slot = topology
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home / "tests"), ["git", "add", "shared.txt"])["verdict"] == "deny"
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "-C", "tests", "add", "shared.txt"])["verdict"] == "deny"
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home / "templates"), ["git", "add", "../tests/shared.txt"])["verdict"] == "deny"


def test_work_tree_and_git_dir_overrides_are_never_approved_and_deny_only_destructive_forms(
    topology,
):
    """대상 지정 override 는 승인하지 않는다. 비파괴면 warn, 파괴 형태면 대상 미확정 거부다."""
    board = _load("git_anchor_work_tree_override", BOARD_PY)
    home, slot = topology
    cases = [
        (str(slot), ["git", "--work-tree", str(home), "add", "tests/shared.txt"]),
        (str(home), ["git", "--work-tree", str(slot), "add", "tests/shared.txt"]),
        (str(slot), [f"GIT_WORK_TREE={home}", "git", "add", "tests/shared.txt"]),
        (str(slot), [f"GIT_DIR={home / '.git'}", "git", "add", "tests/shared.txt"]),
    ]
    assert all(engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", cwd, argv)["verdict"] == "warn" for cwd, argv in cases)
    destructive = [
        (str(home), [f"GIT_DIR={slot / '.git'}", "git", "branch", "-D", "main"]),
        (str(home), ["git", f"--git-dir={home / '.repos' / 'product.git'}", "branch", "-D", "main"]),
        (str(home), ["git", "--work-tree", str(slot), "reset", "--hard"]),
    ]
    for cwd, argv in destructive:
        got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", cwd, argv)
        assert got["verdict"] == "deny", (argv, got)
        assert got["code"] == git_policy.GIT_DESTRUCTIVE_DENY_CODE


def test_git_clean_exclude_values_are_not_pathspecs(topology):
    board = _load("git_anchor_clean_exclude", BOARD_PY)
    home, _slot = topology
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "clean", "-e", "tests/shared.txt"])["verdict"] == "warn"
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "clean", "--exclude", "tests/shared.txt"])["verdict"] == "warn"
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "clean", "-ne", "tests/shared.txt"])["verdict"] == "warn"


def test_r8_parser_boundaries(topology):
    board = _load("git_anchor_r8", BOARD_PY)
    home, slot = topology
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home / "tests"), ["git", "add", ":(top)tests/shared.txt"])["verdict"] == "deny"
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home / "tests"), ["git", "add", ":/tests/shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor_command(home, "S", str(home), "echo x# <<EOF\ngit add tests/shared.txt\nEOF")["verdict"] != "deny"
    assert board.judge_git_anchor_command(home, "S", str(home), "cat <<''\ngit add tests/shared.txt\n\n")["verdict"] != "deny"
    assert board.judge_git_anchor_command(home, "S", str(home), "'git' add tests/shared.txt")["verdict"] == "deny"
    assert board.judge_git_anchor_command(home, "S", str(home), "git\\\n add tests/shared.txt")["verdict"] == "deny"
    assert board.judge_git_anchor_command(home, "S", str(slot), f"env GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt")["verdict"] == "warn"
    assert board.judge_git_anchor_command(home, "S", str(home), "command git add tests/shared.txt")["verdict"] == "deny"


def test_r9_heredoc_data_preserves_backslash_newline(topology):
    board = _load("git_anchor_r9_heredoc", BOARD_PY)
    home, _slot = topology
    command = "cat <<'EOF'\nEO\\\nF\ngit add tests/shared.txt\nEOF"
    assert board.judge_git_anchor_command(home, "S", str(home), command)["verdict"] == "ok"
    assert shell_policy._normalize_shell_line_continuations("git\\\n add") == "git add"
    quoted = "'git\\\n add'"
    assert shell_policy._normalize_shell_line_continuations(quoted) == quoted


def test_r9_wrapper_and_fragmented_command_words(topology):
    board = _load("git_anchor_r9_wrappers", BOARD_PY)
    home, slot = topology
    overrides = [
        f"env -i PATH=/usr/bin:/bin GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
        f"env -- GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
        f"/usr/bin/env GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(home, "S", str(slot), command)["verdict"] == "warn"
        for command in overrides
    )
    home_mutations = [
        "command -- git add tests/shared.txt",
        "command -p git add tests/shared.txt",
        "g''it add tests/shared.txt",
        "/usr/bin/g''it add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(home, "S", str(home), command)["verdict"] == "deny"
        for command in home_mutations
    )
    assert board.judge_git_anchor_command(home, "S", 
        str(slot), "command -p git add tests/shared.txt"
    )["verdict"] == "ok"
    assert board.judge_git_anchor_command(home, "S", 
        str(home), "env --unknown git add tests/shared.txt"
    )["verdict"] == "warn"


def test_r10_execution_wrappers_and_escaped_quote_continuation(topology):
    board = _load("git_anchor_r10_wrappers", BOARD_PY)
    home, slot = topology
    wrappers = [
        "exec git add tests/shared.txt",
        "nice git add tests/shared.txt",
        "timeout 5 git add tests/shared.txt",
        "nohup git add tests/shared.txt",
        "sudo git add tests/shared.txt",
        "exec env -i command -p nice -n 5 timeout --preserve-status 5 nohup -- git add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(home, "S", str(home), command)["verdict"] == "deny"
        for command in wrappers
    )
    assert all(
        board.judge_git_anchor_command(home, "S", str(slot), command)["verdict"] == "ok"
        for command in wrappers
    )
    assert board.judge_git_anchor_command(home, "S", 
        str(home), "sudo -u root git add tests/shared.txt"
    )["verdict"] == "warn"
    escaped_quote = 'echo \\"; git\\\n add tests/shared.txt'
    assert board.judge_git_anchor_command(home, "S", str(home), escaped_quote)["verdict"] == "deny"


def test_r11_dynamic_command_word_is_ambiguity_not_ok(topology):
    board = _load("git_anchor_r11_dynamic_wrapper", BOARD_PY)
    home, slot = topology
    dynamic = [
        "$WRAPPER git add tests/shared.txt",
        '"$WRAPPER" git add tests/shared.txt',
    ]
    for cwd in (home, slot):
        assert all(
            board.judge_git_anchor_command(home, "S", str(cwd), command)["verdict"] == "warn"
            for command in dynamic
        )
    assert board.judge_git_anchor_command(home, "S", 
        str(slot), "git add tests/shared.txt"
    )["verdict"] == "ok"
    data_only = [
        "echo $WRAPPER git add tests/shared.txt",
        "printf %s '$WRAPPER git add tests/shared.txt'",
        "# $WRAPPER git add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(home, "S", str(home), command)["verdict"] == "ok"
        for command in data_only
    )


def test_pathspec_file_and_ambiguous_ref_forms_fail_open(topology):
    board = _load("git_anchor_pathspec_fail_open", BOARD_PY)
    home, _slot = topology
    from_file = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "add", "--pathspec-from-file", "templates/shared.txt"]
    )
    checkout = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "checkout", "tests/shared.txt"])
    reset = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "reset", "tests/shared.txt"])
    combined_message = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "commit", "-am", "tests/shared.txt"]
    )
    top_magic_shell = board.judge_git_anchor_command(home, "S", 
        str(home), "git add ':(top)tests/shared.txt'"
    )
    assert from_file["verdict"] == "warn"
    assert checkout["verdict"] == "warn"
    assert reset["verdict"] == "warn"
    assert combined_message["verdict"] == "warn"
    assert top_magic_shell["verdict"] == "deny"


def test_malicious_ledger_parent_and_symlink_slots_are_not_registered(topology, tmp_path):
    board = _load("git_anchor_lease_containment", BOARD_PY)
    home, _slot = topology
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"

    ledger.write_text(json.dumps({"leases": [
        {"slot": "../product-source", "repo": "product", "state": "leased"},
    ]}) + "\n", encoding="utf-8")
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "add", "tests/shared.txt"]
    )["verdict"] == "warn"

    outside = tmp_path / "product-source"
    symlink_slot = home / "work" / "evil_1"
    symlink_slot.symlink_to(outside, target_is_directory=True)
    ledger.write_text(json.dumps({"leases": [
        {"slot": "work/evil_1", "repo": "evil", "state": "leased"},
    ]}) + "\n", encoding="utf-8")
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "add", "tests/shared.txt"]
    )["verdict"] == "warn"


@pytest.mark.parametrize(
    "lease",
    [
        {"slot": "work/product_1", "repo": "product", "state": "idle"},
        {"slot": "work/x/../product_1", "repo": "product", "state": "leased"},
    ],
)
def test_only_canonical_active_lease_is_slot_identity(topology, lease):
    board = _load(f"git_anchor_lease_identity_{lease['state']}_{len(lease['slot'])}", BOARD_PY)
    home, slot = topology
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.write_text(json.dumps({"leases": [lease]}) + "\n", encoding="utf-8")
    got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", "commit", "-m", "normal"])
    assert got["verdict"] == ("deny" if lease["state"] == "idle" else "warn")
    assert got["cwd_identity"] == "worktree"


def test_internal_symlink_slot_alias_and_target_are_not_registered(topology):
    board = _load("git_anchor_lease_internal_symlink", BOARD_PY)
    home, slot = topology
    alias = home / "work" / "evil_1"
    alias.symlink_to(slot, target_is_directory=True)
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.write_text(json.dumps({"leases": [
        {"slot": "work/evil_1", "repo": "evil", "state": "leased"},
    ]}) + "\n", encoding="utf-8")

    alias_got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(alias), ["git", "commit", "-m", "alias"])
    target_got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", "commit", "-m", "target"])
    assert alias_got["verdict"] == "warn" and alias_got["cwd_identity"] == "worktree"
    assert target_got["verdict"] == "warn" and target_got["cwd_identity"] == "worktree"


def test_registered_slot_mutation_is_explicitly_allowed(topology):
    board = _load("git_anchor_registered_slot_allow", BOARD_PY)
    home, slot = topology
    got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", "add", "tests/shared.txt"])
    assert got["verdict"] == "ok"
    assert got["cwd_identity"] == "slot"


@pytest.mark.parametrize("subcommand", ["rm", "mv", "clean", "apply", "stash"])
def test_additional_mutations_warn_at_home_and_judge_form_in_registered_slot(topology, subcommand):
    """PM 홈은 종전 판정 그대로다. 슬롯에서는 파괴 형태(`clean` 미리보기 없음)만 거부한다."""
    board = _load(f"git_anchor_mutation_{subcommand}", BOARD_PY)
    home, slot = topology
    argv = ["git", subcommand]
    if subcommand == "mv":
        argv += ["tests/shared.txt", "templates/shared.txt"]
    elif subcommand == "apply":
        argv += ["change.patch"]
    elif subcommand == "stash":
        argv += ["push"]
    else:
        argv += ["tests/shared.txt"]
    home_got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), argv)
    slot_got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), argv)
    expected_home = {
        "rm": "deny", "mv": "deny", "clean": "deny", "apply": "warn", "stash": "warn",
    }
    assert home_got["verdict"] == expected_home[subcommand]
    assert slot_got["cwd_identity"] == "slot"
    assert slot_got["verdict"] == ("deny" if subcommand == "clean" else "ok")


def test_board_cli_emits_hook_json(topology, capsys):
    board = _load("git_anchor_board_cli", BOARD_PY)
    home, _slot = topology
    rc = board.main(["git-anchor", "--session", "S", "--cwd", str(home), "--command", "git add tests/shared.txt"])
    got = json.loads(capsys.readouterr().out)
    assert rc == 0 and got["verdict"] == "deny"


def test_claude_hook_prefilter_warn_and_deny(monkeypatch, topology):
    driver = _load("git_anchor_claude", CLAUDE_DRIVER)
    home, _slot = topology
    calls = []

    class FakeBoard:
        @staticmethod
        def judge_git_anchor_command(root, session, cwd, command, grants=False):
            calls.append((cwd, command))
            verdict = "warn" if "$WRAPPER" in command else ("deny" if "tests/" in command else "warn")
            return {"verdict": verdict, "cwd_identity": "pm-home", "reason": "fixture"}

    monkeypatch.setattr(driver, "_load_board", lambda _root: FakeBoard)
    base = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(home)}
    assert driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "python3 build.py"}}, home
    ) is None
    assert calls == []  # 성능 DoD: 비-git은 seam import/호출 0.
    warn = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "git commit -m x"}}, home
    )["hookSpecificOutput"]
    deny = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "git add tests/x"}}, home
    )["hookSpecificOutput"]
    attached = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "git>/tmp/out add tests/x"}}, home
    )["hookSpecificOutput"]
    fragmented = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "g''it add tests/x"}}, home
    )["hookSpecificOutput"]
    escaped = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": 'echo \\"; git\\\n add tests/x'}}, home
    )["hookSpecificOutput"]
    dynamic = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "$WRAPPER git add tests/x"}}, home
    )["hookSpecificOutput"]
    assert "additionalContext" in warn and "permissionDecision" not in warn
    assert deny["permissionDecision"] == "deny" and "permissionDecisionReason" in deny
    assert attached["permissionDecision"] == "deny"
    assert fragmented["permissionDecision"] == "deny"
    assert escaped["permissionDecision"] == "deny"
    assert "additionalContext" in dynamic and "permissionDecision" not in dynamic


@pytest.mark.parametrize("failure_site", ["import", "judge"])
def test_claude_hook_system_exit_is_warn_and_rc0(
    monkeypatch, topology, capsys, failure_site,
):
    driver = _load(f"git_anchor_claude_system_exit_{failure_site}", CLAUDE_DRIVER)
    home, _slot = topology

    class ExitBoard:
        @staticmethod
        def judge_git_anchor_command(_root, _session, _cwd, _command, grants=False):
            raise SystemExit(23)

    if failure_site == "import":
        def fail_load(_root):
            raise SystemExit(17)
        monkeypatch.setattr(driver, "_load_board", fail_load)
    else:
        monkeypatch.setattr(driver, "_load_board", lambda _root: ExitBoard)
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(home),
        "tool_input": {"command": "git add tests/shared.txt"},
    }
    monkeypatch.setattr(driver.sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = driver.run_git_anchor_hook(home)
    got = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "additionalContext" in got["hookSpecificOutput"]
    assert "판정 불가" in got["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("failure_site", ["usage", "root"])
def test_claude_hook_mode_outer_boundary_is_warn_rc0(monkeypatch, capsys, failure_site):
    driver = _load(f"git_anchor_claude_outer_{failure_site}", CLAUDE_DRIVER)
    if failure_site == "usage":
        argv = ["--git-anchor-hook", "--bogus"]
    else:
        monkeypatch.setattr(
            driver.ctx_guard, "repo_root", lambda _path: (_ for _ in ()).throw(SystemExit(31)),
        )
        argv = ["--git-anchor-hook"]
    rc = driver.main(argv)
    got = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "additionalContext" in got["hookSpecificOutput"]
    assert "판정 불가" in got["hookSpecificOutput"]["additionalContext"]


# ── 세션 내 완전일치 중복 억제 (T-0764) ────────────────────────────────────
# 발화 문구·차단 동작은 그대로 두고 *같은 말을 두 번 하지 않는* 것만 본다.
# stdin 형상은 라이브 훅과 같다 — `session_id` 는 ctx_stop_hook 이 marker 파일명으로 쓰는 그 필드다
# (실 세션 marker `.project_manager/.local/ctx-stop/<uuid>.nudge` 가 그 값의 실재 증거).

def _hook_stdin(monkeypatch, driver, home: Path, command: str, session_id: str) -> dict:
    payload = {
        "session_id": session_id,
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(home),
        "tool_input": {"command": command},
    }
    monkeypatch.setattr(driver.sys, "stdin", io.StringIO(json.dumps(payload)))
    return payload



def test_claude_hook_emits_once_per_session_for_identical_message(
    monkeypatch, topology, capsys,
):
    """같은 세션·같은 문구는 첫 1회만 발화하고, 이후 호출의 stdout 은 0바이트다."""
    driver = _load("git_anchor_claude_once", CLAUDE_DRIVER)
    monkeypatch.setattr(
        driver, "_load_board", lambda _root: _load("git_anchor_board_once", BOARD_PY),
    )
    home, _slot = topology

    # 명령은 두 OS 모두 warn 인 git mutation 두 종으로 둔다 — cd 잔존 advisory 는 POSIX 셸
    # 문법 축이라 Windows 판정에 없다(플랫폼 의존 픽스처 금지·VM 실측).
    _hook_stdin(monkeypatch, driver, home, "git commit -m x", "sess-a")
    assert driver.run_git_anchor_hook(home) == 0
    first = json.loads(capsys.readouterr().out)[
        "hookSpecificOutput"]["additionalContext"]
    assert first.startswith("[git-anchor/warn]") and "git commit" in first

    # ① 완전일치 재호출 = 침묵(빈 JSON 도 쓰지 않는다).
    _hook_stdin(monkeypatch, driver, home, "git commit -m x", "sess-a")
    assert driver.run_git_anchor_hook(home) == 0
    assert capsys.readouterr().out == ""

    # ② 문구가 다르면 그대로 발화한다(클래스 전면 침묵 아님).
    _hook_stdin(monkeypatch, driver, home, "git push", "sess-a")
    assert driver.run_git_anchor_hook(home) == 0
    other = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert other != first and "실행 앵커=" in other

    # ③ 세션이 다르면 같은 문구도 그 세션의 첫 1회로 발화한다(세션 격리).
    _hook_stdin(monkeypatch, driver, home, "git commit -m x", "sess-b")
    assert driver.run_git_anchor_hook(home) == 0
    assert json.loads(capsys.readouterr().out)[
        "hookSpecificOutput"]["additionalContext"] == first

    # 상태는 기존 marker 디렉토리 안 세션별 멤버십 파일 1개 — 새 디렉토리·키별 파일 0.
    marker_dir = home / ".project_manager" / ".local" / "ctx-stop"
    assert sorted(path.name for path in marker_dir.iterdir()) == [
        "sess-a.git-anchor", "sess-b.git-anchor",
    ]
    assert len(
        (marker_dir / "sess-a.git-anchor").read_text(encoding="utf-8").splitlines()
    ) == 2


def test_claude_hook_never_suppresses_repeated_deny(monkeypatch, topology, capsys):
    """차단(deny)은 중복이어도 매번 발화한다 — 억제하면 두 번째 위험 명령이 통과한다."""
    driver = _load("git_anchor_claude_deny_repeat", CLAUDE_DRIVER)
    monkeypatch.setattr(
        driver, "_load_board", lambda _root: _load("git_anchor_board_deny_repeat", BOARD_PY),
    )
    home, _slot = topology

    emitted = []
    for _attempt in range(3):
        _hook_stdin(monkeypatch, driver, home, "git add tests/shared.txt", "sess-deny")
        assert driver.run_git_anchor_hook(home) == 0
        emitted.append(json.loads(capsys.readouterr().out)["hookSpecificOutput"])

    assert all(item["permissionDecision"] == "deny" for item in emitted)
    assert len({item["permissionDecisionReason"] for item in emitted}) == 1
    assert "tests/shared.txt" in emitted[0]["permissionDecisionReason"]
    # deny 는 멤버십 장부에 들어가지도 않는다(억제 대상 아님).
    assert not (home / ".project_manager" / ".local" / "ctx-stop").exists()


def test_claude_hook_keeps_uncertain_downgrade_channel_open(
    monkeypatch, topology, capsys,
):
    """판정불능(deny→warn 강등) 발화는 유지된다 — 사유 전문이 그대로 실린다."""
    driver = _load("git_anchor_claude_uncertain", CLAUDE_DRIVER)
    monkeypatch.setattr(
        driver, "_load_board", lambda _root: _load("git_anchor_board_uncertain", BOARD_PY),
    )
    home, _slot = topology

    loop = "for f in x; do git add tests/shared.txt; done"
    _hook_stdin(monkeypatch, driver, home, loop, "sess-uncertain")
    assert driver.run_git_anchor_hook(home) == 0
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "조건/cwd를 정적으로 단일 증명할 수 없음 — 차단하지 않음" in text
    assert "PM 홈에서 등록 worktree와 양쪽에 실재하는 코드 경로(tests/shared.txt)" in text

    # 완전일치 재호출만 침묵이고, 사유가 다른 판정불능은 계속 나온다.
    _hook_stdin(monkeypatch, driver, home, loop, "sess-uncertain")
    assert driver.run_git_anchor_hook(home) == 0
    assert capsys.readouterr().out == ""

    other_loop = "for f in x; do git add templates/shared.txt; done"
    _hook_stdin(monkeypatch, driver, home, other_loop, "sess-uncertain")
    assert driver.run_git_anchor_hook(home) == 0
    other = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "조건/cwd를 정적으로 단일 증명할 수 없음 — 차단하지 않음" in other
    assert "templates/shared.txt" in other


def test_claude_hook_keeps_emitting_when_membership_io_fails(
    monkeypatch, topology, capsys,
):
    """멤버십 파일 I/O 실패는 fail-open — 같은 문구라도 매번 발화한다."""
    driver = _load("git_anchor_claude_marker_failure", CLAUDE_DRIVER)
    monkeypatch.setattr(
        driver, "_load_board", lambda _root: _load("git_anchor_board_marker_failure", BOARD_PY),
    )
    home, _slot = topology
    # marker 디렉토리 자리를 파일이 점유 = mkdir 실패(플랫폼 무관·chmod 불요).
    blocked = home / ".project_manager" / ".local" / "ctx-stop"
    blocked.write_text("not a directory\n", encoding="utf-8")

    # 명령은 두 OS 모두 warn 인 git mutation 으로 둔다 — cd 잔존 advisory 는 POSIX 셸 문법
    # 축이라 Windows 판정에 없다(플랫폼 의존 픽스처가 이 테스트의 실패 원인이었다·VM 실측).
    emitted: list[str] = []
    for _attempt in range(2):
        _hook_stdin(monkeypatch, driver, home, "git commit -m x", "sess-io")
        assert driver.run_git_anchor_hook(home) == 0
        emitted.append(json.loads(capsys.readouterr().out)[
            "hookSpecificOutput"]["additionalContext"])
    assert emitted[0].startswith("[git-anchor/warn]")
    assert "git commit" in emitted[0]
    # fail-open 의 핵심: 완전히 같은 문구가 두 번째에도 그대로 발화된다(억제 없음).
    assert emitted[1] == emitted[0]
    assert blocked.is_file()


def test_claude_hook_infra_failure_warn_is_never_suppressed(
    monkeypatch, topology, capsys,
):
    """판정 인프라 손상(`판정 불가`) 발화는 중복 억제를 지나지 않는다(fail-open 무조건)."""
    driver = _load("git_anchor_claude_infra_repeat", CLAUDE_DRIVER)
    home, _slot = topology

    def fail_load(_root):
        raise SystemExit(17)

    monkeypatch.setattr(driver, "_load_board", fail_load)
    for _attempt in range(2):
        _hook_stdin(monkeypatch, driver, home, "git add tests/shared.txt", "sess-infra")
        assert driver.run_git_anchor_hook(home) == 0
        got = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert "판정 불가" in got["additionalContext"]


def test_claude_settings_and_self_resolving_wrapper_are_wired():
    wrapper = CLAUDE_WRAPPER.read_text(encoding="utf-8")
    assert "--git-anchor-hook" in wrapper and "pm_orch_claude.py" in wrapper
    root = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert any(row.get("matcher") == "Bash" and "--git-anchor-hook" in json.dumps(row)
               for row in root["hooks"]["PreToolUse"])
    # 출하 template 은 파일 쓰기·편집·위임 도구 입력의 허용 위조도 보고, 사용자 프롬프트에서 허용을 기록한다.
    template = json.loads(
        (REPO / "templates" / "claude_code" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    anchor_rows = [row for row in template["hooks"]["PreToolUse"]
                   if "--git-anchor-hook" in json.dumps(row)]
    assert [row["matcher"] for row in anchor_rows] == [
        "Bash|Write|Edit|MultiEdit|NotebookEdit|Agent|Task"]
    assert any("--git-anchor-hook" in json.dumps(row)
               for row in template["hooks"]["UserPromptSubmit"])


def test_codex_execpolicy_stays_static_while_the_hook_axis_carries_the_dynamic_guard():
    """execpolicy는 여전히 정적 판정만 갖고(과차단 방지), 동적 판정은 PreToolUse 훅 축의
    git-anchor 등록 기능이 담당한다 — [[T-0765]] 가 배선을 더한 뒤에는 사유 문구가 아니라
    실 registry 값을 요구한다(부재를 강제하던 옛 봉인은 배선 시 red 가 나지 않았다)."""
    text = CODEX_RULES.read_text(encoding="utf-8")
    assert "false-deny 0" in text
    assert "pm_orch_codex.py" in text and "git-anchor" in text

    dispatcher = _load("git_anchor_codex_dispatcher_seal", CODEX_DISPATCHER)
    features = [feature for feature in dispatcher.CODEX_HOOK_FEATURES
                if feature.feature_id == "git-anchor"]
    assert len(features) == 1, dispatcher.CODEX_HOOK_FEATURES
    feature = features[0]
    assert feature.event == "PreToolUse"
    assert feature.tool_pattern == "^(Bash|apply_patch)$"


# ── codex Bash 축 배선 (T-0765 — claude_code·opencode 2타깃엔 있고 codex 엔 없던 gap) ──────────
# 판정 로직은 새로 만들지 않는다 — 세 하네스가 공유하는 `board.judge_git_anchor_command` 를
# 그대로 재사용한다. 아래는 codex PreToolUse(Bash) payload → 그 판정 → codex 엔벨로프 변환·배선만
# 고정한다. payload 는 조립 dict 가 아니라 라이브 캡처 픽스처(`codex_0_147_0_live_hook_payloads.
# json`)의 Bash 이벤트를 그대로 쓴다(cwd·command 만 시나리오별로 덮어쓴다).


def _codex_live_bash_event() -> dict:
    """라이브 캡처 픽스처(codex-cli 0.147.0)의 유일한 Bash PreToolUse 이벤트."""
    data = json.loads(CODEX_LIVE_HOOK_FIXTURE.read_text(encoding="utf-8"))
    events = [event for event in data["events"] if event.get("tool_name") == "Bash"]
    assert len(events) == 1, events
    return dict(events[0])


def test_codex_explicit_tool_coordinate_precedes_session_cwd(monkeypatch, tmp_path):
    dispatcher = _load("git_anchor_codex_coordinate_priority", CODEX_DISPATCHER)
    calls = []

    class FakeBoard:
        @staticmethod
        def judge_git_anchor_command(root, session, cwd, command, grants=False):
            calls.append((cwd, command))
            return {"verdict": "ok", "cwd_identity": "non-repo", "reason": "fixture"}

    monkeypatch.setattr(dispatcher, "_load_board", lambda _root: FakeBoard)
    session = tmp_path / "session"
    session.mkdir()
    payload = _codex_live_bash_event()
    payload["cwd"] = str(session)
    payload["tool_input"] = {
        "command": "git status", "cwd": "tool-cwd", "workdir": "tool-workdir",
    }
    assert dispatcher.git_anchor_hook_evaluate(payload, session) == {}
    assert calls[-1][0] == str((session / "tool-workdir").resolve())

    payload["tool_input"].pop("workdir")
    assert dispatcher.git_anchor_hook_evaluate(payload, session) == {}
    assert calls[-1][0] == str((session / "tool-cwd").resolve())


def test_codex_live_bash_without_internal_workdir_warns_once_for_pm_home_relative_pytest(
        monkeypatch, topology_without_home_tests):
    dispatcher = _load("git_anchor_codex_missing_workdir", CODEX_DISPATCHER)
    monkeypatch.setattr(
        dispatcher, "_load_board",
        lambda _root: _load("git_anchor_board_codex_missing_workdir", BOARD_PY),
    )
    home, _slot = topology_without_home_tests
    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["session_id"] = "t0872-missing-workdir"
    payload["tool_input"] = {"command": "python3 -m pytest tests/ -q -n auto"}

    first = dispatcher.git_anchor_hook_evaluate(payload, home)
    second = dispatcher.git_anchor_hook_evaluate(payload, home)
    assert first["systemMessage"].startswith("[git-anchor/warn]")
    assert "decision" not in first
    assert second == {}


def test_codex_missing_coordinate_keeps_real_git_mutation_deny(
        monkeypatch, topology_without_home_tests):
    dispatcher = _load("git_anchor_codex_missing_coordinate_mutation", CODEX_DISPATCHER)
    monkeypatch.setattr(
        dispatcher, "_load_board",
        lambda _root: _load("git_anchor_board_codex_missing_coordinate_mutation", BOARD_PY),
    )
    home, _slot = topology_without_home_tests
    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "git add templates/shared.txt"}
    assert dispatcher.git_anchor_hook_evaluate(payload, home)["decision"] == "block"

    payload["tool_input"] = {
        "command": "python3 -m pytest tests/ -q; git add templates/shared.txt",
    }
    compound = dispatcher.git_anchor_hook_evaluate(payload, home)
    assert compound["decision"] == "block"


def test_codex_explicit_coordinate_keeps_engine_mutation_and_pytest_denies(
        monkeypatch, topology_without_home_tests):
    dispatcher = _load("git_anchor_codex_explicit_coordinate_denies", CODEX_DISPATCHER)
    monkeypatch.setattr(
        dispatcher, "_load_board",
        lambda _root: _load("git_anchor_board_codex_explicit_coordinate_denies", BOARD_PY),
    )
    home, slot = topology_without_home_tests
    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {
        "command": "python3 -m pytest tests/ -q", "workdir": str(home),
    }
    assert dispatcher.git_anchor_hook_evaluate(payload, home)["decision"] == "block"

    _seed_engine_copy(slot, "slot-rev")
    payload["tool_input"] = {
        "command": "python3 .project_manager/tools/board.py claim T-0001",
        "workdir": str(slot),
    }
    assert dispatcher.git_anchor_hook_evaluate(payload, home)["decision"] == "block"


def test_codex_bash_axis_deny_matches_the_measured_five_field_envelope(monkeypatch, topology):
    """라이브 픽스처의 Bash 이벤트로 판정을 호출하고 deny 엔벨로프 5필드가 값으로 일치한다."""
    dispatcher = _load("git_anchor_codex_deny", CODEX_DISPATCHER)
    monkeypatch.setattr(
        dispatcher, "_load_board",
        lambda _root: _load("git_anchor_board_codex_deny", BOARD_PY),
    )
    home, _slot = topology
    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "git add tests/shared.txt"}

    envelope = dispatcher.git_anchor_hook_evaluate(payload, home)

    reason = envelope.get("reason", "")
    assert reason.startswith("[git-anchor/deny]")
    assert "tests/shared.txt" in reason
    assert envelope == {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
        "suppressOutput": False,
    }


def test_codex_bash_axis_ok_stays_the_measured_allow_shape_not_zero_bytes(topology):
    """ok·정상 미매칭은 codex 축에서 `{}`(측정된 allow)다 — claude 의 0바이트와 다르다.

    디스패처 `_run_hook_feature` 는 빈 stdout 을 실패로 읽어 adapter-fallback 으로 강등한다 —
    0바이트를 쓰면 정상 통과마다 경고가 나간다(T-0765 §역방향 확인)."""
    dispatcher = _load("git_anchor_codex_ok", CODEX_DISPATCHER)
    home, _slot = topology
    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "ls -la"}
    assert dispatcher.git_anchor_hook_evaluate(payload, home) == {}

    non_bash = dict(payload)
    non_bash["tool_name"] = "collaborationwait_agent"
    non_bash["tool_input"] = {}
    assert dispatcher.git_anchor_hook_evaluate(non_bash, home) == {}


def test_codex_prefilter_skips_board_import_for_non_git_bash(monkeypatch, topology):
    """git·엔진·pytest·cd 관련이 아닌 Bash 명령은 board import 조차 하지 않는다(핫패스 비용 0
    — claude `git_anchor_hook_evaluate` 와 같은 선필터를 그대로 옮겨왔다)."""
    dispatcher = _load("git_anchor_codex_prefilter", CODEX_DISPATCHER)
    home, _slot = topology
    calls = []

    class FakeBoard:
        @staticmethod
        def judge_git_anchor_command(root, session, cwd, command, grants=False):
            calls.append((cwd, command))
            return {"verdict": "warn", "cwd_identity": "pm-home", "reason": "fixture"}

    monkeypatch.setattr(dispatcher, "_load_board", lambda _root: FakeBoard)
    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "python3 build.py"}

    assert dispatcher.git_anchor_hook_evaluate(payload, home) == {}
    assert calls == []

    payload["tool_input"] = {"command": "git commit -m x"}
    warn = dispatcher.git_anchor_hook_evaluate(payload, home)
    assert warn == {"systemMessage": "[git-anchor/warn] fixture", "suppressOutput": False}
    assert calls == [(str(home), "git commit -m x")]


def test_codex_dispatcher_routes_bash_tool_names_to_the_self_invoking_child(topology):
    """`dispatch_hook` 이 Bash tool_name 이면 git-anchor 기능(자기참조 `--git-anchor-hook`)을
    스폰한다(등록 축은 tool_name 만 본다 — git 관련성 선별은 그 자식 안에서 돈다). 다른
    tool_name 은 스폰하지 않는다(배선 자체의 값 확인 — 판정은 위 테스트가 별도로 본다).

    `{self}`는 root 파생이 아니라 실행 중인 파일 자신이다(T-0845) — 이 축을 재려면 디스패처를
    home 의 **출하 레이아웃**(`<home>/.codex/pm_orch_codex.py`)에 실제로 복사해 그 사본에서
    로드해야, "실행 중인 파일 좌표"가 곧 그 출하 경로가 된다. 원본 template 경로에서 로드하면
    `{self}`가 그 template 경로로 전개되는 게 옳은 동작이라 옛 root 재구성 기대와 어긋난다."""
    home, _slot = topology
    shipped_dispatcher = home / ".codex" / "pm_orch_codex.py"
    shipped_dispatcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CODEX_DISPATCHER, shipped_dispatcher)
    dispatcher = _load("git_anchor_codex_route", shipped_dispatcher)
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"{}", stderr=b"")

    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "git add tests/shared.txt"}

    result = dispatcher.dispatch_hook(
        "PreToolUse", json.dumps(payload).encode("utf-8"), home, runner=runner)

    assert result == {}
    assert len(calls) == 1
    assert calls[0] == [sys.executable, str(home / ".codex" / "pm_orch_codex.py"),
                        dispatcher.GIT_ANCHOR_HOOK_FLAG]

    calls.clear()
    non_bash = dict(payload)
    non_bash["tool_name"] = "collaborationwait_agent"
    assert dispatcher.dispatch_hook(
        "PreToolUse", json.dumps(non_bash).encode("utf-8"), home, runner=runner) == {}
    assert calls == []


def test_codex_bash_axis_warn_is_suppressed_once_per_session(monkeypatch, topology):
    """같은 세션·같은 문구는 첫 1회만 발화하고, 이후는 `{}`([[T-0764]] 침묵 기준 승계).

    deny 는 중복이어도 매번 발화한다(억제하면 두 번째 위험 명령이 통과한다)."""
    dispatcher = _load("git_anchor_codex_once", CODEX_DISPATCHER)
    monkeypatch.setattr(
        dispatcher, "_load_board",
        lambda _root: _load("git_anchor_board_codex_once", BOARD_PY),
    )
    home, _slot = topology
    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "git commit -m x"}

    first = dispatcher.git_anchor_hook_evaluate(payload, home)
    assert first["systemMessage"].startswith("[git-anchor/warn]")

    second = dispatcher.git_anchor_hook_evaluate(payload, home)
    assert second == {}  # 완전일치 중복 — 측정된 allow 형태로 침묵.

    other_command = dict(payload)
    other_command["tool_input"] = {"command": "git push"}
    other = dispatcher.git_anchor_hook_evaluate(other_command, home)
    assert other["systemMessage"] != first["systemMessage"]  # 문구가 다르면 그대로 발화.

    other_session = dict(payload)
    other_session["session_id"] = "different-session"
    assert dispatcher.git_anchor_hook_evaluate(other_session, home)["systemMessage"] == \
        first["systemMessage"]  # 세션이 다르면 같은 문구도 첫 1회.

    deny_payload = dict(payload)
    deny_payload["tool_input"] = {"command": "git add tests/shared.txt"}
    deny_first = dispatcher.git_anchor_hook_evaluate(deny_payload, home)
    deny_second = dispatcher.git_anchor_hook_evaluate(deny_payload, home)
    assert deny_first["decision"] == "block"
    assert deny_second == deny_first  # deny 는 중복 억제 대상이 아니다.


@pytest.mark.parametrize("failure_site", ["import", "judge"])
def test_codex_git_anchor_hook_infra_failure_is_warn_rc0_and_bypasses_dedup(
    monkeypatch, topology, failure_site,
):
    """판정 인프라 손상은 rc0 + warn 으로 강등되고, 중복 억제 게이트를 지나지 않는다(fail-open —
    claude `run_git_anchor_hook` 동형)."""
    dispatcher = _load(f"git_anchor_codex_infra_{failure_site}", CODEX_DISPATCHER)
    home, _slot = topology

    class ExitBoard:
        @staticmethod
        def judge_git_anchor_command(_root, _session, _cwd, _command, grants=False):
            raise SystemExit(23)

    if failure_site == "import":
        def fail_load(_root):
            raise SystemExit(17)
        monkeypatch.setattr(dispatcher, "_load_board", fail_load)
    else:
        monkeypatch.setattr(dispatcher, "_load_board", lambda _root: ExitBoard)

    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "git add tests/shared.txt"}

    for _attempt in range(2):  # 두 번 호출해도 매번 발화(중복 억제 미적용).
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        rc = dispatcher.run_git_anchor_hook(home, stdin=stdin, stdout=stdout)
        assert rc == 0
        got = json.loads(stdout.getvalue())
        assert "판정 불가" in got["systemMessage"]
        assert got["suppressOutput"] is False


def test_codex_run_git_anchor_hook_writes_the_measured_allow_shape_for_ok(topology):
    """`--git-anchor-hook` CLI 엔트리(stdin→stdout)도 ok 에서 `{}`(0바이트 아님)를 쓴다."""
    dispatcher = _load("git_anchor_codex_hook_ok", CODEX_DISPATCHER)
    home, _slot = topology
    payload = _codex_live_bash_event()
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "ls -la"}
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()

    rc = dispatcher.run_git_anchor_hook(home, stdin=stdin, stdout=stdout)

    assert rc == 0
    assert stdout.getvalue() == "{}\n"


def test_claude_and_opencode_prefilter_target_sets_match(monkeypatch, topology):
    """R2 26명령과 F-006 4명령의 양 어댑터 선필터 집합은 동형이어야 한다."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    driver = _load("git_anchor_prefilter_parity", CLAUDE_DRIVER)
    home, _slot = topology
    cases = [
        ("python3 build.py", False),
        ("echo x && git status", True),
        ("/usr/bin/git>out status", True),
        ("g''it add tests/shared.txt", True),
        ("git\\\n add tests/shared.txt", True),
        ('echo \\"; git\\\n add tests/shared.txt', True),
        ("$WRAPPER git add tests/shared.txt", True),
        ("python3 .project_manager/tools/board.py list", True),
        (r"python .project_manager\tools\board.py list", True),
        ("python3 -m pytest tests/ -q", True),
        ("pytest tests/", True),
        ("cd /tmp && python3 build.py", True),
        ("sed -n '1,5p' .project_manager/tools/board.py", True),
        ("find . -name '*.py' | xargs grep -l pytest", True),
        ("docker run img pytest tests/", True),
        ("npm test -- --grep 'cd foo && bar'", False),
        ("echo '.project_manager/tools/'", True),
        ("(cd /tmp && ls)", False),
        ("cat <<'EOF'\ncd /tmp && ls\nEOF", False),
        ("cat <<'EOF'\ngit status\nEOF", True),
        ("env -C /elsewhere pytest tests/", True),
        ("command git status", True),
        ("echo git status", True),
        ("legit status", False),
        ('python3 ".project_manager/tools/board.py" list', True),
        ("printf cd", False),
        # F-006: R2의 26명령 대조에 추가한 과소 매칭 네 형태.
        ("python3 .project_manager//tools/board.py claim T-1", True),
        ('cd "/tmp/my dir" && ls', True),
        ("cd X; cmd", True),
        ("cmd && cd X", True),
        # 파일 삭제·이동 명령·삭제 API·허용 표식·허용 기록 폴더(슬롯·공용 저장소 우회와 허용 위조).
        ("rm -rf work/product_1", True),
        ("sudo /bin/mv a b", True),
        ("find . -delete", True),
        ("rsync -a --delete src/ dst/", True),
        ("python3 -c \"import shutil; shutil.rmtree('x')\"", True),
        ("python3 -c \"subprocess.run(['git', 'branch', '-D', 'x'])\"", True),
        ("node -e \"require('fs').rmSync('x')\"", True),
        ("echo 'git 허용: git branch -D x'", True),
        ("cat .project_manager/.local/git-grants/a.json", True),
        ("echo confirm firmware removed", False),
        ("npm run format", False),
        # 명령 속 슬롯 경로(셸 쓰기 경고 대상).
        ("sed -i s/a/b/ /srv/pm/work/app_1/a.txt", True),
    ]
    commands = [command for command, _expected in cases]
    expected_matches = [expected for _command, expected in cases]
    calls = []

    class FakeBoard:
        @staticmethod
        def judge_git_anchor_command(root, session, cwd, command, grants=False):
            calls.append((cwd, command))
            return {"verdict": "ok", "cwd_identity": "pm-home", "reason": "fixture"}

    monkeypatch.setattr(driver, "_load_board", lambda _root: FakeBoard)
    claude_matches = []
    for command in commands:
        before = len(calls)
        payload = {
            "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(home),
            "tool_input": {"command": command},
        }
        assert driver.git_anchor_hook_evaluate(payload, home) is None
        claude_matches.append(len(calls) == before + 1)

    script = r'''
const m = require("./git-anchor-core.cjs");
const commands = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(commands.map((command) => m.containsGitCommand(command))));
'''
    result = subprocess.run(
        [node, "-e", script, json.dumps(commands)], cwd=OPEN_CORE.parent, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    opencode_matches = json.loads(result.stdout)

    dispatcher = _load("git_anchor_prefilter_parity_codex", CODEX_DISPATCHER)
    codex_calls = []

    class CodexFakeBoard:
        @staticmethod
        def judge_git_anchor_command(root, session, cwd, command, grants=False):
            codex_calls.append((cwd, command))
            return {"verdict": "ok", "cwd_identity": "pm-home", "reason": "fixture"}

    monkeypatch.setattr(dispatcher, "_load_board", lambda _root: CodexFakeBoard)
    codex_matches = []
    for command in commands:
        before = len(codex_calls)
        payload = _codex_live_bash_event()
        payload["cwd"] = str(home)
        payload["tool_input"] = {"command": command}
        dispatcher.git_anchor_hook_evaluate(payload, home)
        codex_matches.append(len(codex_calls) == before + 1)
    assert claude_matches == opencode_matches == codex_matches == expected_matches


def test_t0934_board_spawn_once_prefix_input_and_failure(tmp_path):
    node = shutil.which("node")
    assert node is not None, "Node가 있어야 board spawn 계약을 검증할 수 있다"
    root = tmp_path / "engine"
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    shutil.copyfile(BOARD_PY.with_name("python_floor.py"), tools / "python_floor.py")
    script = r'''
const assert = require("node:assert");
const path = require("node:path");
const fs = require("node:fs");
const m = require("./git-anchor-core.cjs");
const leaf = require("./delegate-channel-core.cjs");
const root = process.argv[1];
const conf = path.join(root, ".project_manager", "local.conf");
const floor = path.join(root, ".project_manager", "tools", "python_floor.py");
const board = path.join(root, ".project_manager", "tools", "board.py");
for (const platform of ["linux", "win32"]) {
  for (const configured of [false, true]) {
    const runtime = path.join(root, "Python with space", "python.exe");
    fs.writeFileSync(conf, "runtime.py=" + (configured ? runtime : "") + "\n");
    const chosen = configured ? {command:runtime, prefixArgs:[]} : leaf.pythonCandidates(platform)[0];
    for (const operation of ["command", "tool", "grant"]) {
      for (const outcome of ["success", "rc", "missing", "timeout", "throw", "json", "null"]) {
        const calls = [];
        const prompt = "git 허용: git reset --hard\r\n원문";
        const payload = {filePath: "원문\r\n파일", content:"UTF-8 원문"};
        const argv = operation === "command"
          ? ["git-anchor", "--session", "SID", "--cwd", root, "--command", "git reset --hard", "--grants"]
          : operation === "tool"
          ? ["git-tool-input", "--session", "SID", "--cwd", root, "--tool", "write"]
          : ["git-grant", "--session", "SID", "--cwd", root];
        const input = operation === "command" ? undefined : operation === "tool" ? JSON.stringify(payload) : prompt;
        const spawnSync = (command, args, options) => {
          const probe = args[args.length-1] === floor;
          calls.push({probe,command,args,options});
          assert.strictEqual(command, chosen.command);
          if (probe) {
            assert.deepStrictEqual(args, [...chosen.prefixArgs, floor]);
            assert.deepStrictEqual(options, {encoding:"utf8", timeout:10000, input:""});
            return {status:0, stdout:"Python 3.12\n"};
          }
          assert.deepStrictEqual(args, [...chosen.prefixArgs, board, ...argv]);
          assert.deepStrictEqual(options, {encoding:"utf8", timeout:10000, input});
          if (outcome === "throw") throw new Error("engine throw");
          if (outcome === "null") return null;
          if (outcome === "rc") return {status:7, stderr:"engine rc"};
          if (outcome === "missing") return {status:null, error:{code:"ENOENT", message:"engine missing"}};
          if (outcome === "timeout") return {status:null, error:{code:"ETIMEDOUT", message:"engine timeout"}};
          return {status:0, stdout:outcome === "json" ? "bad JSON\r\n" : JSON.stringify(
            operation === "grant" ? {granted:["git reset --hard"]}
              : {verdict:"deny", cwd_identity:"slot", reason:"원문\r\n거부"}) + "\r\n"};
        };
        const options = {spawnSync, platform, session:"SID", cwd:root, grants:true};
        const call = () => operation === "command" ? m.judgeCommand(root, root, "git reset --hard", options)
          : operation === "tool" ? m.judgeToolInput(root, "write", payload, options)
          : m.recordGrants(root, "SID", root, prompt, options);
        if (outcome === "throw") assert.throws(call, /engine throw/);
        else if (operation === "grant" && outcome !== "success") assert.throws(call);
        else {
          const got = call();
          if (operation === "grant") assert.deepStrictEqual(got, ["git reset --hard"]);
          else if (outcome === "success") assert.deepStrictEqual(got, {
            verdict:"deny", cwd_identity:"slot", reason:"원문\r\n거부"});
          else assert.strictEqual(got.verdict, "warn");
        }
        assert.strictEqual(calls.length, 2, "one probe, one actual engine, no retry");
        assert.strictEqual(calls[1].probe, false);
      }
    }
  }
}
fs.writeFileSync(conf, "runtime.py=broken-runtime\n");
let calls = 0;
const spawnSync = () => {calls += 1; return {status:9009, error:null, stderr:"Store alias"};};
const warned = m.judgeCommand(root, root, "git reset --hard", {spawnSync,platform:"win32"});
assert.strictEqual(warned.verdict, "warn");
assert.match(warned.reason, /broken-runtime.*rc=9009/);
assert.strictEqual(calls, 1, "failed explicit runtime must not execute board or another program");
console.log("T0934_BOARD_ONCE_OK");
'''
    result = subprocess.run([node, "-e", script, str(root)], cwd=OPEN_CORE.parent,
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "T0934_BOARD_ONCE_OK" in result.stdout


def test_opencode_thin_plugin_and_core_node_selfcheck():
    assert OPEN_PLUGIN.is_file() and OPEN_CORE.is_file()
    plugin = OPEN_PLUGIN.read_text(encoding="utf-8")
    assert plugin.count("export const") == 1 and "GitAnchorPlugin" in plugin
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    script = r'''
const m = require("./git-anchor-core.cjs");
const assert = require("node:assert");
assert.strictEqual(m.containsGitCommand("python3 build.py"), false);
assert.strictEqual(m.containsGitCommand("echo x && git status"), true);
assert.strictEqual(m.containsGitCommand("/usr/bin/git>out status"), true);
assert.strictEqual(m.containsGitCommand("g''it add tests/shared.txt"), true);
assert.strictEqual(m.containsGitCommand("git\\\n add tests/shared.txt"), true);
assert.strictEqual(m.containsGitCommand('echo \\"; git\\\n add tests/shared.txt'), true);
assert.strictEqual(m.containsGitCommand('$WRAPPER git add tests/shared.txt'), true);
assert.strictEqual(m.containsGitCommand("python3 .project_manager/tools/board.py list"), true);
assert.strictEqual(m.containsGitCommand("python3 -m pytest tests/ -q"), true);
assert.strictEqual(m.containsGitCommand("cd /tmp && python3 build.py"), true);
assert.strictEqual(m.containsGitCommand("python3 .project_manager//tools/board.py claim T-1"), true);
assert.strictEqual(m.containsGitCommand('cd "/tmp/my dir" && ls'), true);
assert.strictEqual(m.containsGitCommand("cd X; cmd"), true);
assert.strictEqual(m.containsGitCommand("cmd && cd X"), true);
let calls = 0;
const fake = (py, argv, opts) => {
  if (argv[argv.length-1].endsWith("python_floor.py")) return {status:0, stdout:"Python 3.12\n"};
  calls += 1;
  return {status:0, stdout:JSON.stringify({verdict:"warn",cwd_identity:"pm-home",reason:"fixture"})+"\n", stderr:""};
};
assert.strictEqual(m.judgeCommand("/r", "/r", "python3 build.py", {spawnSync: fake}).verdict, "ok");
assert.strictEqual(calls, 0);
assert.strictEqual(m.judgeCommand("/r", "/r", "git commit -m x", {spawnSync: fake}).verdict, "warn");
assert.strictEqual(calls, 1);
assert.strictEqual(m.judgeCommand("/r", "/r", "python3 .project_manager/tools/board.py list", {spawnSync: fake}).verdict, "warn");
assert.strictEqual(m.judgeCommand("/r", "/r", "python3 -m pytest tests/ -q", {spawnSync: fake}).verdict, "warn");
assert.strictEqual(m.judgeCommand("/r", "/r", "cd /tmp && python3 build.py", {spawnSync: fake}).verdict, "warn");
assert.strictEqual(calls, 4);
assert.strictEqual(typeof m.GitAnchorPlugin, "function");
assert.strictEqual(typeof m.makeGitAnchorPlugin, "function");

(async () => {
  const toasts = [];
  const judge = (_root, cwd, command) => ({
    verdict: command.includes("git add") ? "deny" : "warn",
    cwd_identity: cwd.endsWith("slot") ? "slot" : "pm-home",
    reason: cwd.endsWith("slot") ? "slot fixture" : "home fixture",
  });
  const factory = m.makeGitAnchorPlugin(judge);
  const hooks = await factory({
    directory: "/repo",
    worktree: "/repo",
    client: {tui: {showToast: async (value) => toasts.push(value)}, session: {get: async ({path}) => ({data: {id: path.id}})}},
  });
  await hooks["tool.execute.before"](
    {tool:"bash", sessionID:"A"}, {args:{command:"git commit -m a", cwd:"/repo"}}
  );
  await hooks["tool.execute.before"](
    {tool:"bash", sessionID:"B"}, {args:{command:"git commit -m b", cwd:"/repo/slot"}}
  );
  const a1 = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID:"A"}, a1);
  assert.strictEqual(a1.system.length, 1);
  assert.match(a1.system[0], /home fixture/);
  const a2 = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID:"A"}, a2);
  assert.strictEqual(a2.system.length, 0);
  const b1 = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID:"B"}, b1);
  assert.strictEqual(b1.system.length, 1);
  assert.match(b1.system[0], /slot fixture/);
  assert.strictEqual(toasts.length, 2);
  await assert.rejects(
    hooks["tool.execute.before"](
      {tool:"bash", sessionID:"A"}, {args:{command:"git add tests/shared.txt", cwd:"/repo"}}
    ),
    /git-anchor\/deny/
  );

  const errorToasts = [];
  const failingFactory = m.makeGitAnchorPlugin(() => { throw new Error("fixture boom"); });
  const failingHooks = await failingFactory({
    directory: "/repo",
    worktree: "/repo",
    client: {tui: {showToast: async (value) => errorToasts.push(value)}, session: {get: async ({path}) => ({data: {id: path.id}})}},
  });
  await failingHooks["tool.execute.before"](
    {tool:"bash", sessionID:"ERR"}, {args:{command:"git add tests/shared.txt", cwd:"/repo"}}
  );
  const errorContext = {system: []};
  await failingHooks["experimental.chat.system.transform"]({sessionID:"ERR"}, errorContext);
  assert.strictEqual(errorContext.system.length, 1);
  assert.match(errorContext.system[0], /git-anchor\/warn/);
  assert.match(errorContext.system[0], /판정 인프라 예외\(fixture boom\)/);
  assert.strictEqual(errorToasts.length, 1);
  console.log("GIT_ANCHOR_CORE_OK");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], cwd=OPEN_CORE.parent, check=True,
                            capture_output=True, text=True, encoding="utf-8")
    assert "GIT_ANCHOR_CORE_OK" in result.stdout


def test_opencode_git_anchor_blocks_when_engine_absent_at_install_root(tmp_path):
    """설치 트리에 board.py 가 없으면 경고로 낮추지 않고 `tool.execute.before` 에서 막는다.

    payload `directory` 와 조상(이 저장소)에는 board.py 가 있다 — 그 둘 중 하나로 엔진 루트를
    잡는 코드라면 판정이 호출돼 통과한다. 자기 설치 트리를 보는 코드만 차단한다.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    lib = tmp_path / "install" / ".opencode" / "lib"
    lib.mkdir(parents=True)
    for name in ("git-anchor-core.cjs", "delegate-channel-core.cjs", "warning-channel-core.cjs"):
        shutil.copyfile(OPEN_CORE.parent / name, lib / name)
    assert not (tmp_path / "install" / ".project_manager").exists()

    script = r'''
const assert = require("node:assert");
const m = require("./git-anchor-core.cjs");
const repoWithEngine = process.argv[1];

(async () => {
  const toasts = [];
  const judge = () => { throw new Error("설치가 깨졌는데 판정을 호출함"); };
  const hooks = await m.makeGitAnchorPlugin(judge)({
    directory: repoWithEngine,
    worktree: repoWithEngine,
    client: {tui: {showToast: async (value) => toasts.push(value)}, session: {get: async ({path}) => ({data: {id: path.id}})}},
  });
  // 설치 확인은 선필터 뒤다 — git 이 아닌 명령은 그대로 지나간다.
  await hooks["tool.execute.before"](
    {tool:"bash", sessionID:"S"}, {args:{command:"echo hello"}},
  );
  await assert.rejects(
    hooks["tool.execute.before"](
      {tool:"bash", sessionID:"S"}, {args:{command:"git commit -m x"}},
    ),
    /\[git-anchor\/설치\]/,
  );
  const context = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID:"S"}, context);
  assert.deepStrictEqual(context.system, [], "차단 대신 경고로 강등됨");
  assert.deepStrictEqual(toasts, [], "차단 대신 toast 로 강등됨");
  console.log("GIT_ANCHOR_INSTALL_BLOCK_OK");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", script, str(REPO)], cwd=lib, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert "GIT_ANCHOR_INSTALL_BLOCK_OK" in result.stdout


# ── 손 git 파괴 형태 거부 (슬롯·공용 저장소 대상 · 세 하네스 공통 판정) ─────────────────────────
# 브랜치·슬롯은 엔진만 만들고 지운다. 명령 표마다 거부 양성, 읽기 전용·기존 브랜치 전환 음성,
# 옵션 묶음·`--옵션=값`·git alias 풀이, 대상을 정적으로 못 정한 호출의 거부, 무관한 저장소·PM 홈
# 통과를 고정한다. topology 슬롯 `work/product_1` 의 로컬 브랜치는 `main`·`product_1` 둘이다.

_SLOT_DESTRUCTIVE_CASES = [
    (["branch", "feature"], "브랜치 만들기"),
    (["branch", "-f", "feature", "main"], "브랜치 만들기"),
    # `-v` 는 목록 모드를 켜지 않는다 — git 은 operand 를 새 브랜치 이름으로 읽는다.
    (["branch", "-v", "feature"], "브랜치 만들기"),
    (["branch", "-c", "main", "copy"], "브랜치 만들기"),
    (["checkout", "-b", "feature"], "브랜치 만들기"),
    (["checkout", "-B", "feature"], "브랜치 만들기"),
    (["checkout", "-qb", "feature"], "브랜치 만들기"),
    (["checkout", "--orphan=feature"], "브랜치 만들기"),
    (["checkout", "-t", "origin/feature"], "브랜치 만들기"),
    (["switch", "-c", "feature"], "브랜치 만들기"),
    (["switch", "-C", "feature"], "브랜치 만들기"),
    (["switch", "--create=feature"], "브랜치 만들기"),
    (["switch", "feature"], "원격 추적 브랜치로 브랜치 만들기"),
    (["worktree", "add", "-b", "feature", "../elsewhere"], "브랜치 만들기"),
    (["worktree", "add", "../brand-new"], "브랜치 만들기"),
    (["branch", "-d", "main"], "브랜치 지우기"),
    (["branch", "-D", "main"], "브랜치 지우기"),
    (["branch", "--delete", "--force", "main"], "브랜치 지우기"),
    (["branch", "-Dq", "main"], "브랜치 지우기"),
    (["branch", "--del", "main"], "브랜치 지우기"),
    (["branch", "-m", "renamed"], "브랜치 이름 바꾸기"),
    (["branch", "-M", "main", "renamed"], "브랜치 이름 바꾸기"),
    (["branch", "--move", "main", "renamed"], "브랜치 이름 바꾸기"),
    (["reset", "--hard"], "변경 버리기"),
    (["reset", "-q", "--hard", "HEAD"], "변경 버리기"),
    (["reset", "--har"], "변경 버리기"),
    (["checkout", "-f", "main"], "변경 버리기"),
    (["checkout", "--force"], "변경 버리기"),
    (["checkout", "--", "tests/shared.txt"], "변경 버리기"),
    (["checkout", "main", "--", "tests/shared.txt"], "변경 버리기"),
    (["checkout", "tests/shared.txt"], "경로 복원"),
    (["restore", "tests/shared.txt"], "변경 버리기"),
    (["restore", "--staged", "--worktree", "tests/shared.txt"], "변경 버리기"),
    (["restore", "-SW", "tests/shared.txt"], "변경 버리기"),
    (["restore", "--source=HEAD", "tests/shared.txt"], "변경 버리기"),
    (["clean", "-f"], "변경 버리기"),
    (["clean", "-fdx"], "변경 버리기"),
    (["clean", "--force", "-d"], "변경 버리기"),
    (["stash", "drop"], "변경 버리기"),
    (["stash", "drop", "stash@{0}"], "변경 버리기"),
    (["stash", "clear"], "변경 버리기"),
    (["worktree", "remove", "--force", "../product_2"], "슬롯 지우기"),
    (["push", "--force"], "원격 덮어쓰기"),
    (["push", "-f", "origin", "main"], "원격 덮어쓰기"),
    (["push", "-uf", "origin", "main"], "원격 덮어쓰기"),
    (["push", "--force-with-lease", "origin", "main"], "원격 덮어쓰기"),
    (["push", "--force-with-lease=main:abc123", "origin", "main"], "원격 덮어쓰기"),
    (["push", "origin", "+main"], "원격 덮어쓰기"),
    (["push", "--mirror", "origin"], "원격 덮어쓰기"),
]

_SLOT_ALLOWED_CASES = [
    ["branch"], ["branch", "--list"], ["branch", "--list", "feat*"], ["branch", "-a"],
    ["branch", "-r"], ["branch", "-v"], ["branch", "-vv"], ["branch", "--show-current"],
    ["branch", "--contains", "HEAD"], ["branch", "--merged", "main"],
    ["branch", "--format=%(refname:short)"], ["branch", "--sort", "-committerdate"],
    ["branch", "-u", "origin/main"], ["branch", "--unset-upstream"],
    ["checkout"], ["checkout", "main"], ["checkout", "product_1"], ["checkout", "-"],
    ["checkout", "--detach", "HEAD"],
    ["switch", "main"], ["switch", "-"], ["switch", "--detach", "HEAD"], ["switch", "-d", "HEAD"],
    ["stash"], ["stash", "list"], ["stash", "push", "-m", "drop"], ["stash", "pop"],
    ["worktree", "list"], ["worktree", "prune"], ["worktree", "add", "--detach", "../detached"],
    ["worktree", "add", "../main"], ["worktree", "add", "../other", "main"],
    ["restore", "--staged", "tests/shared.txt"], ["restore", "-S", "tests/shared.txt"],
    ["clean", "-n"], ["clean", "-nd"], ["clean", "--dry-run", "-x"],
    ["reset"], ["reset", "--soft", "HEAD"], ["reset", "tests/shared.txt"],
    ["push"], ["push", "origin", "main"], ["push", "-u", "origin", "product_1"],
    # 원격 브랜치 삭제는 이번 거부 범위 밖이다.
    ["push", "--delete", "origin", "old"],
    ["status"], ["log", "--oneline"],
]


@pytest.fixture(scope="module")
def destructive_board():
    return _load("git_destructive_board", BOARD_PY)


@pytest.mark.parametrize(
    ("args", "label"), _SLOT_DESTRUCTIVE_CASES,
    ids=[" ".join(args) for args, _label in _SLOT_DESTRUCTIVE_CASES],
)
def test_slot_destructive_git_forms_are_denied(topology, destructive_board, args, label):
    home, slot = topology
    got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", *args])
    assert got["verdict"] == "deny", got
    assert got["cwd_identity"] == "slot"
    assert got["code"] == git_policy.GIT_DESTRUCTIVE_DENY_CODE
    assert label in got["reason"]
    assert f"`git {' '.join(args)}`" in got["reason"]


@pytest.mark.parametrize(
    "args", _SLOT_ALLOWED_CASES, ids=[" ".join(args) for args in _SLOT_ALLOWED_CASES],
)
def test_slot_read_only_and_existing_branch_forms_are_not_denied(
    topology, destructive_board, args,
):
    """과차단 음성 — 목록·조회·기존 브랜치 전환·미리보기·index 만 되돌리기는 슬롯에서도 통과."""
    home, slot = topology
    got = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", *args])
    assert got["verdict"] == "ok", got
    assert got["cwd_identity"] == "slot"


_STOP_LINE = "다른 명령으로 같은 일을 하지 마라. 여기서 멈추고 사용자에게 물어라."
_ACTION_BRANCH_CREATE = "브랜치는 `pm-config task branch <명의> <슬롯> <브랜치> --from <부모>` 로 만든다."
_ACTION_SLOT_REMOVE = (
    "사용자에게 슬롯 제거 승인을 묻고, 승인하면 `pm-config worktree remove <slot> --user-ack <slot>` 로 지운다."
)
_ACTION_ASK_WITHOUT_GRANT = (
    "사용자에게 이 명령을 실행할지 묻는다. 이 하네스에는 1회 허용 수단이 없으므로, 허용하면 사용자가 "
    "터미널에서 직접 실행한다."
)
_ACTION_ASK_WITH_GRANT = (
    "사용자에게 이 명령을 실행할지 묻는다. 허용하면 사용자가 `git 허용: <명령 원문>` 한 줄을 보낸 뒤 "
    "같은 명령을 그대로 한 번 다시 실행한다."
)
_ACTION_FOLDER_UNKNOWN = "대상 폴더를 절대경로로 적어 다시 실행한다(git 은 `git -C <절대경로> …`)."


def test_destructive_deny_reasons_prescribe_the_engine_route(topology, destructive_board):
    """거부 문구 = 멈춤 지시 첫 줄 + 경우별 다음 행동 한 줄 + 거부한 호출."""
    home, slot = topology
    expectations = {
        "git checkout -b feature": _ACTION_BRANCH_CREATE,
        "git worktree remove ../product_2": _ACTION_SLOT_REMOVE,
        "git branch -D main": _ACTION_ASK_WITHOUT_GRANT,
        "git reset --hard": _ACTION_ASK_WITHOUT_GRANT,
        "git push --force": _ACTION_ASK_WITHOUT_GRANT,
    }
    for command, action in expectations.items():
        lines = destructive_board.judge_git_anchor_command(home, "S", str(slot), command)["reason"].splitlines()
        assert lines[0] == _STOP_LINE
        assert lines[1] == action, lines
        assert lines[2].startswith("거부한 호출: 손 git ") and f"`{command}`" in lines[2]
        assert len(lines) == 3


def test_shared_repository_and_unleased_worktree_are_protected_targets(topology):
    board = _load("git_destructive_shared_repo", BOARD_PY)
    home, slot = topology
    bare = home / ".repos" / "product.git"
    direct = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(bare), ["git", "branch", "-D", "main"])
    assert direct["verdict"] == "deny" and direct["cwd_identity"] == "shared-repo"
    via_option = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S",
        str(home), ["git", "-C", ".repos/product.git", "worktree", "remove", str(slot)],
    )
    assert via_option["verdict"] == "deny" and via_option["cwd_identity"] == "shared-repo"
    listing = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(bare), ["git", "branch"])
    assert listing["verdict"] == "warn" and listing["cwd_identity"] == "shared-repo"
    assert engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(bare), ["git", "fetch"])["verdict"] == "ok"

    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.write_text(json.dumps({"leases": [{"slot": "work/product_1", "repo": "product",
                                               "state": "idle"}]}) + "\n", encoding="utf-8")
    idle = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", "stash", "clear"])
    assert idle["verdict"] == "deny" and idle["cwd_identity"] == "worktree"


def test_destructive_forms_outside_slot_and_shared_repository_are_not_denied(
    topology, tmp_path, destructive_board,
):
    home, _slot = topology
    unrelated = tmp_path / "unrelated"
    _seed_repo(unrelated)
    # 실재하지 않는 경로 — 테스트 임시 폴더는 이 저장소 worktree 안이라 실재 폴더는 그 git 으로 풀린다.
    plain = tmp_path / "not-a-repo"
    for args in (
        ["branch", "-D", "main"], ["reset", "--hard"], ["push", "--force"],
        ["worktree", "remove", "x"], ["checkout", "-b", "x"], ["stash", "clear"], ["clean", "-fd"],
    ):
        foreign = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(unrelated), ["git", *args])
        assert foreign["verdict"] == "warn" and foreign["cwd_identity"] == "foreign", foreign
        at_home = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", *args])
        assert at_home["verdict"] == "warn" and at_home["cwd_identity"] == "pm-home", at_home
        non_repo = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(plain), ["git", *args])
        assert non_repo["verdict"] == "warn" and non_repo["cwd_identity"] == "non-repo", non_repo
    shell = destructive_board.judge_git_anchor_command(home, "S", 
        str(unrelated), "git branch -D main && git push --force",
    )
    assert shell["verdict"] == "warn"


def test_git_alias_is_resolved_against_the_target_repository(topology):
    board = _load("git_destructive_alias", BOARD_PY)
    home, slot = topology
    _git(slot, "config", "alias.nuke", "branch -D")
    _git(slot, "config", "alias.chain", "wipe-stash")
    _git(slot, "config", "alias.wipe-stash", "stash clear")

    repo_alias = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", "nuke", "main"])
    assert repo_alias["verdict"] == "deny"
    assert "브랜치 지우기" in repo_alias["reason"] and "`git branch -D main`" in repo_alias["reason"]
    chained = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", "chain"])
    assert chained["verdict"] == "deny" and "`git stash clear`" in chained["reason"]
    inline = board.judge_git_anchor_command(home, "S", str(slot), "git -c alias.gone='reset --hard' gone")
    assert inline["verdict"] == "deny" and "`git reset --hard`" in inline["reason"]
    shell_alias = board.judge_git_anchor_command(home, "S", 
        str(slot), "git -c alias.wipe='!git clean -fd' wipe",
    )
    assert shell_alias["verdict"] == "deny"
    assert "대상을 정적으로 정할 수 없어" in shell_alias["reason"]

    # git 명령 이름을 가리는 alias 는 git 이 무시한다 — 가드도 풀지 않는다.
    shadowed = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", "-c", "alias.status=branch -D main", "status"])
    assert shadowed["verdict"] == "ok"
    harmless = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(slot), ["git", "-c", "alias.lg=log --oneline", "lg"])
    assert harmless["verdict"] == "ok"
    # PM 홈은 파괴 형태 거부 대상이 아니므로 alias 도 풀지 않는다(종전 판정).
    at_home = engine_guard.judge_git_anchor(pm_paths.home_at(home), "S", str(home), ["git", "-c", "alias.nuke=branch -D", "nuke", "main"])
    assert at_home["verdict"] == "ok"


@pytest.mark.parametrize(
    "command",
    [
        "GIT_DIR={bare} git branch -D main",
        "export GIT_DIR={bare}; git branch -D main",
        'git -C "$SLOT" stash drop',
        "cd $TARGET && git reset --hard",
        "(cd {slot} && git checkout -b feature)",
        "echo $(cd {slot} && git branch -D main)",
        "for b in one two; do git branch -D $b; done",
        "$WRAPPER git worktree remove ../product_2",
        "sudo -u root git push --force",
        "bash -c 'git reset --hard'",
        'sh -c "cd {slot} && git clean -fd"',
        "eval git stash clear",
        "git branch --merged | xargs git branch -d",
        "find . -name '*.txt' -exec git checkout -- {{}} \\;",
        "python3 -c \"import os; os.system('git push --force origin main')\"",
        "alias gbd='git branch -D'; gbd main",
        "(git branch -D main)",
        "git branch -D main &",
    ],
)
def test_unresolvable_calls_with_destructive_forms_are_denied(
    topology, destructive_board, command,
):
    """셸 alias·스크립트·GIT_DIR·변수 -C·subshell cd 등 대상을 못 정한 파괴 형태는 PM 홈 cwd 에서도 거부."""
    home, slot = topology
    rendered = command.format(slot=slot, bare=home / ".repos" / "product.git")
    got = destructive_board.judge_git_anchor_command(home, "S", str(home), rendered)
    assert got["verdict"] == "deny", got
    assert "손 git" in got["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "GIT_DIR={bare} git push origin main",
        "cd $TARGET && git branch",
        "(cd {slot} && git switch --detach HEAD)",
        "bash -c 'git status && git log --oneline'",
        "git branch --merged | xargs git show",
        "git branch --list &",
    ],
)
def test_unresolvable_calls_without_destructive_forms_keep_the_previous_verdicts(
    topology, destructive_board, command,
):
    home, slot = topology
    rendered = command.format(slot=slot, bare=home / ".repos" / "product.git")
    assert destructive_board.judge_git_anchor_command(home, "S", str(home), rendered)["verdict"] != "deny"


@pytest.mark.parametrize(
    "command",
    [
        "echo git branch -D main",
        "git commit -m 'git reset --hard 는 사용자가 한다'",
        "grep -rn 'git push --force' docs",
        "cat <<'EOF'\ngit worktree remove ../product_2\nEOF",
        "git log --format='%h (%s)' && git checkout main",
        'git switch main && echo "(done)"',
    ],
)
def test_destructive_git_text_as_data_is_not_denied(topology, destructive_board, command):
    home, slot = topology
    assert destructive_board.judge_git_anchor_command(home, "S", str(slot), command)["verdict"] != "deny"


def test_shell_expansion_in_destructive_capable_arguments(topology, destructive_board):
    """인자 속 셸 변수는 슬롯에서만 형태 미확정 거부다. 대상이 확정된 PM 홈 push 는 종전 warn."""
    home, slot = topology
    judge = destructive_board.judge_git_anchor_command
    assert judge(home, "S", str(slot), 'git push origin "$BRANCH"')["verdict"] == "deny"
    assert judge(home, "S", str(home), 'git push origin "$BRANCH"')["verdict"] == "warn"
    assert judge(home, "S", str(home), "git push origin HEAD && echo $?")["verdict"] == "warn"
    kept = judge(home, "S", str(slot), "git branch -D main; echo $HOME")
    assert kept["verdict"] == "deny"
    assert kept["code"] == git_policy.GIT_DESTRUCTIVE_DENY_CODE
    assert judge(home, "S", str(slot), 'git commit -m "$MESSAGE"')["verdict"] == "warn"


def test_engine_branch_and_slot_commands_are_not_blocked_by_the_git_guard(
    topology, destructive_board,
):
    """엔진 명령은 git 호출이 아니다 — 엔진이 안에서 부르는 git 은 하네스 훅을 거치지 않는다."""
    home, slot = topology
    engine = home / ".project_manager" / "tools" / "pm_config.py"
    for command in (
        f"python3 {engine} task branch product_1 product_1 feature --from main",
        f"python3 {engine} worktree remove product_1 --user-ack product_1",
    ):
        assert destructive_board.judge_git_anchor_command(home, "S", str(slot), command)["verdict"] != "deny"


def test_claude_hook_denies_destructive_slot_git_with_permission_decision(
    monkeypatch, topology, capsys,
):
    driver = _load("git_destructive_claude", CLAUDE_DRIVER)
    monkeypatch.setattr(
        driver, "_load_board", lambda _root: _load("git_destructive_claude_board", BOARD_PY),
    )
    home, slot = topology
    payload = {
        "session_id": "S", "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "cwd": str(slot), "tool_input": {"command": "git branch -D main"},
    }
    monkeypatch.setattr(driver.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert driver.run_git_anchor_hook(home) == 0
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    reason_lines = output["permissionDecisionReason"].splitlines()
    assert reason_lines[0] == f"[git-anchor/deny] {_STOP_LINE}"
    # claude 는 사용자 1회 허용을 쓰는 하네스다 — 세션이 있으면 허용 입력 형식을 안내한다.
    assert reason_lines[1] == _ACTION_ASK_WITH_GRANT
    assert "손 git 브랜치 지우기" in reason_lines[2]

    read_only = {**payload, "tool_input": {"command": "git branch --show-current"}}
    monkeypatch.setattr(driver.sys, "stdin", io.StringIO(json.dumps(read_only)))
    assert driver.run_git_anchor_hook(home) == 0
    assert capsys.readouterr().out == ""


def test_codex_hook_blocks_destructive_slot_git_with_measured_envelope(monkeypatch, topology):
    dispatcher = _load("git_destructive_codex", CODEX_DISPATCHER)
    monkeypatch.setattr(
        dispatcher, "_load_board", lambda _root: _load("git_destructive_codex_board", BOARD_PY),
    )
    home, slot = topology
    payload = _codex_live_bash_event()
    payload["session_id"] = "S"  # 슬롯 주인 task 를 연 세션(topology 장부).
    payload["cwd"] = str(home)
    payload["tool_input"] = {"command": "git reset --hard", "workdir": str(slot)}
    stdout = io.StringIO()
    assert dispatcher.run_git_anchor_hook(
        home, stdin=io.StringIO(json.dumps(payload)), stdout=stdout,
    ) == 0
    envelope = json.loads(stdout.getvalue())
    reason = envelope["reason"]
    # codex 는 1회 허용 수단이 없다 — 사용자가 터미널에서 직접 실행하도록 안내한다.
    assert reason.splitlines()[:2] == [f"[git-anchor/deny] {_STOP_LINE}", _ACTION_ASK_WITHOUT_GRANT]
    assert "손 git 변경 버리기" in reason
    assert envelope == {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
        "suppressOutput": False,
    }

    payload["tool_input"] = {"command": "git stash list", "workdir": str(slot)}
    assert dispatcher.git_anchor_hook_evaluate(payload, home) == {}


def test_opencode_plugin_rejects_destructive_slot_git_through_the_real_board(topology):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    home, slot = topology
    script = r'''
const assert = require("node:assert");
const m = require("./git-anchor-core.cjs");
const [engineRoot, slot] = process.argv.slice(1);

(async () => {
  const toasts = [];
  const judge = (_root, cwd, command) => m.judgeCommand(engineRoot, cwd, command);
  const hooks = await m.makeGitAnchorPlugin(judge)({
    directory: slot,
    worktree: slot,
    client: {tui: {showToast: async (value) => toasts.push(value)}, session: {get: async ({path}) => ({data: {id: path.id}})}},
  });
  await assert.rejects(
    hooks["tool.execute.before"](
      {tool:"bash", sessionID:"S"}, {args:{command:"git worktree remove ../product_2", cwd:slot}},
    ),
    /\[git-anchor\/deny\] 다른 명령으로 같은 일을 하지 마라\. 여기서 멈추고 사용자에게 물어라\.\n사용자에게 슬롯 제거 승인을 묻고[^\n]*\n거부한 호출: 손 git 슬롯 지우기/,
  );
  await hooks["tool.execute.before"](
    {tool:"bash", sessionID:"S"}, {args:{command:"git worktree list", cwd:slot}},
  );
  const context = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID:"S"}, context);
  assert.deepStrictEqual(context.system, []);
  console.log("GIT_ANCHOR_DESTRUCTIVE_BLOCK_OK");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", script, str(REPO), str(slot)], cwd=OPEN_CORE.parent, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert "GIT_ANCHOR_DESTRUCTIVE_BLOCK_OK" in result.stdout


# ── 멈춤 거부 문구 · 사용자 1회 허용 · 우회 경로 (2026-09-15 사용자 결정) ─────────────────────────
# 거부 문구는 멈춤 지시 첫 줄 + 경우별 다음 행동 하나다. 1회 허용은 사용자 프롬프트 훅만 만들고,
# (세션·실행 폴더·정규화 argv) 가 정확히 같은 호출 한 번에만 소비된다. 우회 경로(git 저수준 명령,
# git 이 아닌 파일 삭제·이동, 셸·인터프리터로 감싼 호출)는 같은 판정 자리에서 거부한다.

_ACTION_LITERAL_ARGUMENTS = "셸 변수·명령 치환을 값 그대로 바꿔 적어 다시 실행한다."
_ACTION_GRANT_FORGERY = (
    "허용 줄·허용 기록은 사용자 입력으로만 만든다. 필요하면 사용자에게 `git 허용: <명령 원문>` 입력을 요청한다."
)
_GRANTED = "사용자 1회 허용으로 실행한다"


@pytest.fixture
def grant_board():
    return _load("git_grant_board", BOARD_PY)


def _grant_files(home: Path) -> list[Path]:
    directory = home / ".project_manager" / ".local" / "git-grants"
    return sorted(directory.iterdir()) if directory.is_dir() else []


@pytest.mark.parametrize(
    ("cwd_key", "command", "action"),
    [
        ("slot", "git branch -D main", _ACTION_ASK_WITH_GRANT),
        ("slot", "git checkout -b feature", _ACTION_BRANCH_CREATE),
        ("slot", "git worktree remove ../product_2", _ACTION_SLOT_REMOVE),
        ("home", "GIT_DIR={bare} git branch -D main", _ACTION_FOLDER_UNKNOWN),
        ("home", "bash -c 'git reset --hard'", _ACTION_FOLDER_UNKNOWN),
        ("slot", 'git push origin "$BRANCH"', _ACTION_LITERAL_ARGUMENTS),
        ("home", "printf x > .project_manager/.local/git-grants/k.json", _ACTION_GRANT_FORGERY),
    ],
    ids=["ask", "branch-create", "slot-remove", "git-dir", "wrapped", "variable", "forgery"],
)
def test_stop_reason_is_stop_line_then_one_next_action(topology, grant_board, cwd_key, command, action):
    home, slot = topology
    cwd = slot if cwd_key == "slot" else home
    rendered = command.format(bare=home / ".repos" / "product.git")
    got = grant_board.judge_git_anchor_command(home, "S", str(cwd), rendered, grants=True)
    lines = got["reason"].splitlines()
    assert got["verdict"] == "deny", got
    assert lines[0] == _STOP_LINE
    assert lines[1] == action
    assert lines[-1].startswith("거부한 호출: ") and len(lines) == 3
    assert "case" not in got


def test_compound_command_lists_each_next_action_once(topology, grant_board):
    home, slot = topology
    got = grant_board.judge_git_anchor_command(home, "S", 
        str(slot), "git checkout -b feature; git branch -D main; git branch -D other", grants=True,
    )
    lines = got["reason"].splitlines()
    assert lines[:3] == [_STOP_LINE, _ACTION_BRANCH_CREATE, _ACTION_ASK_WITH_GRANT]
    assert lines[3].startswith("거부한 호출: 호출 1 [slot/deny]")
    assert "code" not in got and "case" not in got


def test_user_grant_passes_the_exact_command_once(topology, grant_board):
    home, slot = topology
    judge = grant_board.judge_git_anchor_command
    assert judge(home, "S", str(slot), "git branch -D main", grants=True)["verdict"] == "deny"
    granted = grant_board.record_git_grants(
        home, "S", str(slot), "지워도 됩니다.\ngit 허용: git branch -D 'main'\n",
    )
    assert granted == ["git branch -D main"]
    assert len(_grant_files(home)) == 1

    first = judge(home, "S", str(slot), "git branch -D main", grants=True)
    assert first["verdict"] == "warn" and first["reason"].startswith(_GRANTED)
    assert _grant_files(home) == []
    second = judge(home, "S", str(slot), "git branch -D main", grants=True)
    assert second["verdict"] == "deny" and second["reason"].splitlines()[0] == _STOP_LINE


def test_user_grant_is_bound_to_argv_folder_and_session(topology, grant_board):
    home, slot = topology
    judge = grant_board.judge_git_anchor_command
    grant_board.record_git_grants(home, "S", str(slot), "git 허용: git branch -D main")
    bare = home / ".repos" / "product.git"
    assert judge(home, "S", str(slot), "git branch -d main", grants=True)["verdict"] == "deny"
    assert judge(home, "S", str(bare), "git branch -D main", grants=True)["verdict"] == "deny"
    assert judge(home, "S2", str(slot), "git branch -D main", grants=True)["verdict"] == "deny"
    # 허용 수단이 없는 하네스(세션 미전달)는 기록을 소비하지 않는다.
    no_grant = judge(home, "S", str(slot), "git branch -D main")
    assert no_grant["verdict"] == "deny" and _ACTION_ASK_WITHOUT_GRANT in no_grant["reason"]
    assert len(_grant_files(home)) == 1
    assert judge(home, "S", str(slot), "git branch -D main", grants=True)["verdict"] == "warn"


def test_user_grant_does_not_open_engine_routes_or_compound_commands(topology, grant_board):
    home, slot = topology
    judge = grant_board.judge_git_anchor_command
    grant_board.record_git_grants(
        home, "S", str(slot),
        "git 허용: git checkout -b feature\ngit 허용: git worktree remove ../product_2\n"
        "git 허용: git branch -D main",
    )
    assert judge(home, "S", str(slot), "git checkout -b feature", grants=True)["verdict"] == "deny"
    assert judge(home, "S", str(slot), "git worktree remove ../product_2", grants=True)["verdict"] == "deny"
    compound = judge(home, "S", str(slot), "git branch -D main && git reset --hard", grants=True)
    assert compound["verdict"] == "deny"
    assert len(_grant_files(home)) == 3


def test_user_grant_covers_protected_file_removal_too(topology, grant_board):
    home, _slot = topology
    judge = grant_board.judge_git_anchor_command
    command = "rm -rf .repos/product.git"
    assert _ACTION_ASK_WITH_GRANT in judge(home, "S", str(home), command, grants=True)["reason"]
    grant_board.record_git_grants(home, "S", str(home), f"git 허용: {command}")
    assert judge(home, "S", str(home), command, grants=True)["verdict"] == "warn"
    assert judge(home, "S", str(home), command, grants=True)["verdict"] == "deny"


@pytest.mark.parametrize(
    "line",
    [
        "git 허용: ",
        "git 허용: git branch -D a && git branch -D b",
        "git 허용: git branch -D $BRANCH",
        "git 허용: git branch -D 'feat*'",
        "git 허용: git branch -D 'unterminated",
        "git 허용: rm -rf ~/work",
        "git 허용: git branch -D x > out.txt",
        "git 허용: git branch -D x # 주석",
    ],
)
def test_malformed_grant_line_records_nothing(topology, grant_board, line):
    home, slot = topology
    with pytest.raises(git_policy.GitGrantShapeError):
        grant_board.record_git_grants(home, "S", str(slot), f"git 허용: git branch -D ok\n{line}")
    assert _grant_files(home) == []


def test_grant_line_accepts_git_revision_notation(topology, grant_board):
    home, slot = topology
    granted = grant_board.record_git_grants(
        home, "S", str(slot), "git 허용: git reset --hard HEAD~1\n  git 허용 : git stash drop stash@{0}",
    )
    # 반환은 정규화 argv 의 셸 인용 표기(shlex.join)다.
    assert granted == ["git reset --hard 'HEAD~1'", "git stash drop 'stash@{0}'"]
    with pytest.raises(ValueError):
        grant_board.record_git_grants(home, "", str(slot), "git 허용: git branch -D main")


def test_forged_or_corrupt_grant_record_is_discarded_and_denied(topology, grant_board):
    home, slot = topology
    judge = grant_board.judge_git_anchor_command
    note = "같은 명령의 허용 기록이 사용자 프롬프트 훅이 쓴 형식과 달라 폐기했다."

    grant_board.record_git_grants(home, "S", str(slot), "git 허용: git branch -D main")
    (path,) = _grant_files(home)
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "cwd": str(home)}) + "\n", encoding="utf-8")
    forged = judge(home, "S", str(slot), "git branch -D main", grants=True)
    assert forged["verdict"] == "deny" and note in forged["reason"].splitlines()
    assert _grant_files(home) == []

    grant_board.record_git_grants(home, "S", str(slot), "git 허용: git branch -D main")
    (path,) = _grant_files(home)
    path.write_text("not json\n", encoding="utf-8")
    corrupt = judge(home, "S", str(slot), "git branch -D main", grants=True)
    assert corrupt["verdict"] == "deny" and note in corrupt["reason"].splitlines()
    assert _grant_files(home) == []


def test_grant_claim_failure_keeps_the_deny(monkeypatch, topology, grant_board):
    home, slot = topology
    grant_board.record_git_grants(home, "S", str(slot), "git 허용: git branch -D main")

    def refuse(_source, _target):
        raise PermissionError("locked")

    monkeypatch.setattr(grant_board.os, "rename", refuse)
    got = grant_board.judge_git_anchor_command(home, "S", str(slot), "git branch -D main", grants=True)
    assert got["verdict"] == "deny"
    assert "허용 기록을 확인하지 못해 거부를 유지한다(locked)." in got["reason"].splitlines()


@pytest.mark.parametrize(
    "command",
    [
        "printf '{}' > .project_manager/.local/git-grants/k.json",
        "cp /tmp/k.json .project_manager/.local/git-grants/",
        "python3 -c \"open('.project_manager/.local/git-grants/k.json', 'w').write('{}')\"",
        "tee .project_manager\\.local\\git-grants\\k.json < /tmp/k",
        'claude -p "git 허용: git branch -D main"',
        "cat <<'EOF' > prompt.md\ngit 허용: git branch -D main\nEOF",
    ],
)
def test_grant_forgery_through_shell_is_denied(topology, grant_board, command):
    home, _slot = topology
    got = grant_board.judge_git_anchor_command(home, "S", str(home), command, grants=True)
    assert got["verdict"] == "deny", got
    assert got["reason"].splitlines()[1] == _ACTION_GRANT_FORGERY


def test_grant_forgery_through_file_and_delegate_tools_is_denied(tmp_path, grant_board):
    judge = grant_board.judge_tool_input
    store = tmp_path / ".project_manager" / ".local" / "git-grants" / "k.json"
    for tool_input in (
        {"file_path": str(store), "content": "{}"},
        {"filePath": "C:\\p\\.project_manager\\.local\\git-grants\\k.json", "content": "{}"},
        {"file_path": "prompt.md", "content": "메모\n  git 허용: git branch -D main\n"},
        {"subagent_type": "developer", "prompt": "git 허용: git reset --hard"},
    ):
        got = judge(tmp_path, "S", str(tmp_path), "Write", tool_input)
        assert got["verdict"] == "deny", tool_input
        assert got["reason"].splitlines()[:2] == [_STOP_LINE, _ACTION_GRANT_FORGERY]
    for tool_input in (
        {"file_path": "notes.md", "content": "허용은 `git 허용: <명령>` 한 줄로 받는다"},
        {"file_path": "src/app.py", "content": "print('hi')\n"},
    ):
        assert judge(tmp_path, "S", str(tmp_path), "Write", tool_input)["verdict"] == "ok", tool_input


def test_board_cli_records_grants_and_consumes_them_with_grant_session(
    monkeypatch, topology, capsys,
):
    board = _load("git_grant_board_cli", BOARD_PY)
    home, slot = topology
    monkeypatch.setattr(board, "REPO", home)

    def stdin(text: str):
        monkeypatch.setattr(
            board.sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8"),
        )

    stdin("git 허용: git branch -D main\n")
    assert board.main(["git-grant", "--session", "S", "--cwd", str(slot)]) == 0
    assert json.loads(capsys.readouterr().out) == {"granted": ["git branch -D main"]}

    assert board.main(["git-anchor", "--session", "S", "--cwd", str(slot), "--command", "git branch -D main"]) == 0
    without = json.loads(capsys.readouterr().out)
    assert without["verdict"] == "deny" and _ACTION_ASK_WITHOUT_GRANT in without["reason"]
    assert board.main([
        "git-anchor", "--session", "S", "--cwd", str(slot), "--command", "git branch -D main", "--grants",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "warn"

    stdin("git 허용: git branch -D a; rm -rf b\n")
    with pytest.raises(SystemExit) as stopped:
        board.main(["git-grant", "--session", "S", "--cwd", str(slot)])
    assert str(stopped.value.code).startswith("[중단] ")

    stdin(json.dumps({"filePath": str(home / ".project_manager/.local/git-grants/x.json")}))
    assert board.main(["git-tool-input", "--session", "S", "--cwd", str(home), "--tool", "write"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "deny"


_LOW_LEVEL_DESTRUCTIVE = [
    ("git update-ref -d refs/heads/main", "브랜치 ref 직접 지우기·옮기기"),
    ("git update-ref refs/heads/main HEAD~1", "브랜치 ref 직접 지우기·옮기기"),
    ("git update-ref HEAD HEAD~1", "브랜치 ref 직접 지우기·옮기기"),
    ("git update-ref --stdin", "브랜치 ref 직접 지우기·옮기기"),
    ("git symbolic-ref HEAD refs/heads/other", "브랜치 ref 직접 지우기·옮기기"),
    ("git symbolic-ref -d HEAD", "브랜치 ref 직접 지우기·옮기기"),
    ("git checkout-index -f -a", "변경 버리기"),
    ("git checkout-index --force --all", "변경 버리기"),
    ("git read-tree --reset -u HEAD", "변경 버리기"),
    ("git read-tree --reset HEAD", "변경 버리기"),
    ("git worktree move . ../elsewhere", "슬롯 옮기기"),
    ("git for-each-ref --format='delete %(refname)' refs/heads | git update-ref --stdin",
     "브랜치 ref 직접 지우기·옮기기"),
]


@pytest.mark.parametrize(
    ("command", "label"), _LOW_LEVEL_DESTRUCTIVE, ids=[c for c, _l in _LOW_LEVEL_DESTRUCTIVE],
)
def test_low_level_git_forms_with_the_same_effect_are_denied(topology, grant_board, command, label):
    home, slot = topology
    got = grant_board.judge_git_anchor_command(home, "S", str(slot), command, grants=True)
    assert got["verdict"] == "deny", got
    assert label in got["reason"]
    assert got["reason"].splitlines()[1] == _ACTION_ASK_WITH_GRANT


@pytest.mark.parametrize(
    "command",
    [
        "git update-ref refs/tags/v1 HEAD", "git symbolic-ref HEAD", "git symbolic-ref --short HEAD",
        "git checkout-index -a", "git read-tree HEAD", "git for-each-ref refs/heads",
    ],
)
def test_low_level_git_reads_are_not_denied(topology, grant_board, command):
    home, slot = topology
    assert grant_board.judge_git_anchor_command(home, "S", str(slot), command)["verdict"] != "deny"


def test_low_level_git_forms_outside_slot_and_shared_repository_are_not_denied(
    topology, tmp_path, grant_board,
):
    home, _slot = topology
    unrelated = tmp_path / "unrelated"
    _seed_repo(unrelated)
    for command in ("git update-ref -d refs/heads/main", "git symbolic-ref HEAD refs/heads/x",
                    "git checkout-index -f -a", "git read-tree --reset -u HEAD"):
        assert grant_board.judge_git_anchor_command(home, "S", str(unrelated), command)["verdict"] != "deny"
        assert grant_board.judge_git_anchor_command(home, "S", str(home), command)["verdict"] != "deny"


_REMOVAL_DENIED = [
    ("home", "rm -rf work/product_1", _ACTION_SLOT_REMOVE),
    ("home", "rm -rf work", _ACTION_SLOT_REMOVE),
    ("home", "rm -rf {slot}/", _ACTION_SLOT_REMOVE),
    ("home", "rm -rf work/*", _ACTION_SLOT_REMOVE),
    ("home", "rm -rf .repos/product.git", _ACTION_ASK_WITH_GRANT),
    ("home", "rmdir .repos", _ACTION_ASK_WITH_GRANT),
    ("slot", "rm .git", _ACTION_ASK_WITH_GRANT),
    ("home", "mv work/product_1 /tmp/elsewhere", _ACTION_SLOT_REMOVE),
    ("home", "mv /tmp/x work/product_1/.git", _ACTION_ASK_WITH_GRANT),
    ("slot", "find . -type f -delete", _ACTION_ASK_WITH_GRANT),
    ("slot", "find . -name .git -delete", _ACTION_ASK_WITH_GRANT),
    ("home", "find work -exec rm -rf {{}} +", _ACTION_SLOT_REMOVE),
    ("home", "rsync -a --delete /tmp/empty/ work/product_1/", _ACTION_ASK_WITH_GRANT),
    ("home", "sudo rm -rf {slot}", _ACTION_SLOT_REMOVE),
    ("home", "env A=1 timeout 5 rm -rf {slot}", _ACTION_SLOT_REMOVE),
    ("home", "bash -c 'rm -rf work/product_1'", _ACTION_SLOT_REMOVE),
    ("home", "eval rm -rf .repos", _ACTION_ASK_WITH_GRANT),
    ("home", "python3 -c \"import shutil; shutil.rmtree('{home}/.repos')\"", _ACTION_ASK_WITH_GRANT),
    ("home", "node -e \"require('fs').rmSync('{slot}', {{recursive: true}})\"", _ACTION_SLOT_REMOVE),
    ("home", "python3 -c \"import subprocess; subprocess.run(['rm', '-rf', 'work/product_1'])\"",
     _ACTION_SLOT_REMOVE),
    # 실행 폴더를 바꾸거나 대상이 입력·변수에서 오면 대상을 정적으로 풀 수 없다.
    ("home", "sh -c 'cd work && rm -rf product_1'", _ACTION_FOLDER_UNKNOWN),
    ("home", "(cd work && rm -rf product_1)", _ACTION_FOLDER_UNKNOWN),
    ("home", "ls work | xargs rm -rf", _ACTION_FOLDER_UNKNOWN),
    ("home", 'rm -rf "$TARGET"', _ACTION_FOLDER_UNKNOWN),
]


@pytest.mark.parametrize(
    ("cwd_key", "command", "action"), _REMOVAL_DENIED, ids=[c for _k, c, _a in _REMOVAL_DENIED],
)
def test_file_commands_removing_slots_or_shared_repositories_are_denied(
    topology, grant_board, cwd_key, command, action,
):
    home, slot = topology
    cwd = slot if cwd_key == "slot" else home
    rendered = command.format(slot=slot, home=home)
    got = grant_board.judge_git_anchor_command(home, "S", str(cwd), rendered, grants=True)
    assert got["verdict"] == "deny", got
    assert got["reason"].splitlines()[:2] == [_STOP_LINE, action]
    assert got.get("code") == git_policy.GIT_DESTRUCTIVE_DENY_CODE


_REMOVAL_ALLOWED = [
    ("home", "rm -rf {outside}/scratch"),
    ("slot", "rm -f build.log"),
    ("slot", "rm -rf node_modules dist"),
    ("slot", "find . -name '*.pyc' -delete"),
    ("slot", "mv a.txt b.txt"),
    ("home", "mv /tmp/report.txt work/product_1/"),
    ("home", "rsync -a /tmp/src/ work/product_1/"),
    ("home", "rsync -a --delete /tmp/src/ work/product_1/build/"),
    ("home", "echo rm -rf work/product_1"),
    ("home", "grep -rn 'rm -rf .repos' docs"),
    ("home", "cat <<'EOF'\nrm -rf work/product_1\nEOF"),
    ("home", "false && rm -rf work"),
    ("home", "python3 -c \"print('cleanup')\""),
    ("home", "python3 -c \"import os; os.remove('/tmp/x')\""),
]


@pytest.mark.parametrize(
    ("cwd_key", "command"), _REMOVAL_ALLOWED, ids=[c for _k, c in _REMOVAL_ALLOWED],
)
def test_file_commands_outside_protected_paths_are_not_denied(
    topology, tmp_path, grant_board, cwd_key, command,
):
    """과차단 음성 — 슬롯 밖 경로·슬롯 안 일반 파일·데이터로 쓰인 명령 문자열은 통과한다."""
    home, slot = topology
    cwd = slot if cwd_key == "slot" else home
    rendered = command.format(outside=tmp_path / "outside")
    assert grant_board.judge_git_anchor_command(home, "S", str(cwd), rendered)["verdict"] != "deny"


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"import subprocess; subprocess.run(['git', 'branch', '-D', 'main'])\"",
        "node -e \"require('child_process').execFileSync('git', ['reset', '--hard'])\"",
        "bash -c 'git update-ref -d refs/heads/main'",
        "sh -c \"git symbolic-ref HEAD refs/heads/x\"",
    ],
)
def test_interpreter_wrapped_git_forms_are_denied_as_unknown_target(topology, grant_board, command):
    home, _slot = topology
    got = grant_board.judge_git_anchor_command(home, "S", str(home), command, grants=True)
    assert got["verdict"] == "deny", got
    assert got["reason"].splitlines()[:2] == [_STOP_LINE, _ACTION_FOLDER_UNKNOWN]


def test_claude_prompt_hook_records_grant_and_bash_consumes_it_once(monkeypatch, topology, capsys):
    driver = _load("git_grant_claude", CLAUDE_DRIVER)
    monkeypatch.setattr(driver, "_load_board", lambda _root: _load("git_grant_claude_board", BOARD_PY))
    home, slot = topology

    def run(payload: dict):
        monkeypatch.setattr(driver.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert driver.run_git_anchor_hook(home) == 0
        out = capsys.readouterr().out
        return json.loads(out) if out else None

    bash = {
        "session_id": "sess", "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "cwd": str(slot), "tool_input": {"command": "git branch -D main"},
    }
    prompt = {
        "session_id": "sess", "hook_event_name": "UserPromptSubmit", "cwd": str(slot),
        "prompt": "네, 지워도 됩니다.\ngit 허용: git branch -D main",
    }
    assert run(bash)["hookSpecificOutput"]["permissionDecision"] == "deny"
    # 다른 세션의 허용은 이 세션 호출의 근거가 되지 않는다.
    run({**prompt, "session_id": "other"})
    assert run(bash)["hookSpecificOutput"]["permissionDecision"] == "deny"

    granted = run(prompt)["hookSpecificOutput"]
    assert granted["hookEventName"] == "UserPromptSubmit"
    assert "`git branch -D main`" in granted["additionalContext"]
    passed = run(bash)["hookSpecificOutput"]
    assert "permissionDecision" not in passed
    assert passed["additionalContext"].startswith(f"[git-anchor/warn] {_GRANTED}")
    assert run(bash)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_prompt_hook_blocks_malformed_grant_and_skips_ordinary_prompts(monkeypatch, topology):
    driver = _load("git_grant_claude_prompt", CLAUDE_DRIVER)
    home, slot = topology
    loads = []
    monkeypatch.setattr(driver, "_load_board", lambda _root: loads.append(1))
    plain = {"session_id": "s", "hook_event_name": "UserPromptSubmit", "cwd": str(slot),
             "prompt": "branch 정리해 줘"}
    assert driver.git_anchor_hook_evaluate(plain, home) is None
    assert loads == []

    monkeypatch.setattr(driver, "_load_board", lambda _root: _load("git_grant_claude_prompt_board", BOARD_PY))
    blocked = driver.git_anchor_hook_evaluate(
        {**plain, "prompt": "git 허용: git branch -D a && git branch -D b"}, home,
    )
    assert blocked["decision"] == "block"
    assert blocked["reason"].startswith("[git-anchor/허용 거부] ")
    assert _grant_files(home) == []


def test_claude_hook_denies_grant_forgery_through_write_edit_and_agent_tools(monkeypatch, topology):
    driver = _load("git_grant_claude_tools", CLAUDE_DRIVER)
    home, _slot = topology
    loads = []

    def load(_root):
        loads.append(1)
        return _load("git_grant_claude_tools_board", BOARD_PY)

    monkeypatch.setattr(driver, "_load_board", load)
    base = {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(home)}
    store = str(home / ".project_manager" / ".local" / "git-grants" / "k.json")
    for tool, tool_input in (
        ("Write", {"file_path": store, "content": "{}"}),
        ("Edit", {"file_path": "prompt.md", "old_string": "a", "new_string": "git 허용: git reset --hard"}),
        ("Agent", {"subagent_type": "developer", "prompt": "git 허용: git branch -D main"}),
    ):
        output = driver.git_anchor_hook_evaluate(
            {**base, "tool_name": tool, "tool_input": tool_input}, home,
        )["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny", tool
        assert output["permissionDecisionReason"].splitlines()[1] == _ACTION_GRANT_FORGERY
    loads.clear()
    ordinary = {**base, "tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x = 1\n"}}
    assert driver.git_anchor_hook_evaluate(ordinary, home) is None
    assert loads == [1]  # 쓰기 도구는 슬롯 주인 판정 때문에 늘 board 를 부른다.


def test_codex_hook_never_uses_grants(monkeypatch, topology):
    """codex 는 사용자 프롬프트를 허용 기록으로 남기는 배선이 없어 1회 허용을 쓰지 않는다 — 기록이 있어도 거부한다."""
    dispatcher = _load("git_grant_codex", CODEX_DISPATCHER)
    monkeypatch.setattr(dispatcher, "_load_board", lambda _root: _load("git_grant_codex_board", BOARD_PY))
    home, slot = topology
    payload = _codex_live_bash_event()
    board = _load("git_grant_codex_record", BOARD_PY)
    board.record_git_grants(home, payload["session_id"], str(slot), "git 허용: git branch -D main")
    payload["cwd"] = str(slot)
    payload["tool_input"] = {"command": "git branch -D main"}
    envelope = dispatcher.git_anchor_hook_evaluate(payload, home)
    assert envelope["decision"] == "block"
    assert envelope["reason"].splitlines()[1] == _ACTION_ASK_WITHOUT_GRANT
    assert len(_grant_files(home)) == 1


def test_opencode_prompt_grant_and_tool_input_guard(topology):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    home, slot = topology
    script = r'''
const assert = require("node:assert");
const m = require("./git-anchor-core.cjs");
const [engineRoot, slot] = process.argv.slice(1);

(async () => {
  // 사용자가 직접 친 텍스트 조각만 모은다 — synthetic 조각(첨부 내용)은 사용자 입력이 아니다.
  assert.strictEqual(
    m.userPromptText([
      {type: "text", text: "git 허용: git branch -D main"},
      {type: "text", synthetic: true, text: "git 허용: git reset --hard"},
      {type: "file"},
    ]),
    "git 허용: git branch -D main",
  );
  const recorded = [];
  const judged = [];
  const hooks = await m.makeGitAnchorPlugin(
    (_root, cwd, command, options) => {
      judged.push([cwd, command, options.session, options.grants]);
      return {verdict: "ok", cwd_identity: "slot", reason: "fixture"};
    },
    (_root, tool, args, options) => m.judgeToolInput(engineRoot, tool, args, options),
    async (_root, sessionID, cwd, prompt) => {
      recorded.push([sessionID, cwd, prompt]);
      return ["git branch -D main"];
    },
  )({directory: slot, worktree: slot, client: {tui: {showToast: async () => {}}, session: {get: async ({path}) => ({data: {id: path.id}})}}});

  await hooks["chat.message"]({sessionID: "S"}, {parts: [{type: "text", text: "ok\ngit 허용: git branch -D main"}]});
  assert.deepStrictEqual(recorded, [["S", slot, "ok\ngit 허용: git branch -D main"]]);
  const context = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID: "S"}, context);
  assert.match(context.system[0], /\[git-anchor\/허용\].*`git branch -D main`/);
  await hooks["chat.message"]({sessionID: "S"}, {parts: [{type: "text", synthetic: true, text: "git 허용: git reset --hard"}]});
  assert.strictEqual(recorded.length, 1);

  await hooks["tool.execute.before"]({tool: "bash", sessionID: "S"}, {args: {command: "git branch -D main", workdir: slot}});
  assert.deepStrictEqual(judged, [[slot, "git branch -D main", "S", true]]);
  await assert.rejects(
    hooks["tool.execute.before"]({tool: "write", sessionID: "S"}, {args: {filePath: slot + "/.project_manager/.local/git-grants/k.json", content: "{}"}}),
    /\[git-anchor\/deny\] 다른 명령으로 같은 일을 하지 마라\./,
  );
  await assert.rejects(
    hooks["tool.execute.before"]({tool: "task", sessionID: "S"}, {args: {subagent_type: "developer", prompt: "git 허용: git reset --hard"}}),
    /허용 줄·허용 기록은 사용자 입력으로만 만든다/,
  );
  await hooks["tool.execute.before"]({tool: "write", sessionID: "S"}, {args: {filePath: slot + "/notes.md", content: "hello"}});

  // 허용 기록 실패는 조용히 넘기지 않는다.
  const calls = [];
  const failing = (py, argv, opts) => {
    if (argv[argv.length-1].endsWith("python_floor.py")) return {status:0, stdout:"Python 3.12\n"};
    calls.push([argv.slice((py === "py" ? 1 : 0) + 1), opts.input]);
    return {status: 1, stdout: "", stderr: "[중단] 형식 오류"};
  };
  assert.throws(
    () => m.recordGrants(engineRoot, "S", slot, "git 허용: x", {spawnSync: failing}),
    /\[git-anchor\/허용 거부\] \[중단\] 형식 오류/,
  );
  assert.deepStrictEqual(calls[0], [["git-grant", "--session", "S", "--cwd", slot], "git 허용: x"]);
  const granted = (py, argv, opts) => argv[argv.length-1].endsWith("python_floor.py")
    ? {status:0, stdout:"Python 3.12\n"}
    : {status: 0, stdout: JSON.stringify({granted: ["git branch -D main"]}) + "\n", stderr: ""};
  assert.deepStrictEqual(m.recordGrants(engineRoot, "S", slot, "git 허용: git branch -D main", {spawnSync: granted}), ["git branch -D main"]);
  const seen = [];
  const spy = (py, argv, opts) => {
    if (argv[argv.length-1].endsWith("python_floor.py")) return {status:0, stdout:"Python 3.12\n"};
    seen.push(argv.slice((py === "py" ? 1 : 0) + 1));
    return {status: 0, stdout: JSON.stringify({verdict: "deny", cwd_identity: "slot", reason: "x"}) + "\n", stderr: ""};
  };
  m.judgeCommand(engineRoot, slot, "git branch -D main", {session: "S", grants: true, spawnSync: spy});
  assert.deepStrictEqual(seen[0], ["git-anchor", "--session", "S", "--cwd", slot, "--command", "git branch -D main", "--grants"]);
  console.log("GIT_GRANT_OPENCODE_OK");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(
        [node, "-e", script, str(REPO), str(slot)], cwd=OPEN_CORE.parent, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert "GIT_GRANT_OPENCODE_OK" in result.stdout


def test_slot_git_writes_pass_only_for_the_session_whose_task_holds_the_slot(topology):
    board = _load("git_slot_owner_board", BOARD_PY)
    home, slot = topology
    no_task = git_policy._GIT_GUARD_ACTION_LINES[git_policy._GIT_GUARD_CASE_SLOT_NO_TASK]
    not_owned = git_policy._GIT_GUARD_ACTION_LINES[git_policy._GIT_GUARD_CASE_SLOT_NOT_OWNED]
    for command in ("git add -- tests/shared.txt", "git commit -m x", "git stash", "git merge main",
                    "git restore --staged tests/shared.txt"):
        assert board.judge_git_anchor_command(home, "S", str(slot), command)["verdict"] == "ok", command
        got = board.judge_git_anchor_command(home, "S-other", str(slot), command)
        assert got["verdict"] == "deny", command
        assert got["reason"].splitlines()[:2] == [_STOP_LINE, no_task]
    # 보기만 하는 형태는 막지 않는다 — 남의 슬롯이면 셸 경고만 한다.
    for command in ("git stash list", "git clean -n", "git status"):
        assert board.judge_git_anchor_command(home, "S-other", str(slot), command)["verdict"] == "warn", command
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["tasks"].append({"name": "t2", "session": "S2"})
    ledger.write_text(json.dumps(data) + "\n", encoding="utf-8")
    other = board.judge_git_anchor_command(home, "S2", str(home), f"git -C {slot} commit -m x")
    assert other["verdict"] == "deny"
    assert other["reason"].splitlines()[1] == not_owned


def _t0931_guard_owner(home: Path, mode: str):
    ledger = home / '.project_manager/.local/worktree-leases.json'
    data = json.loads(ledger.read_text(encoding='utf-8'))
    row = data['leases'][0]
    data['tasks'].append({'name': 't2', 'session': 'F'})
    if mode == 'foreign':
        row['session'] = 't2'
    elif mode == 'idle':
        row.update(state='idle', session='')
    elif mode == 'readonly':
        row.update(role='readonly', session='')
    ledger.write_text(json.dumps(data), encoding='utf-8')


@pytest.mark.parametrize('mode', ['own', 'foreign', 'idle', 'readonly', 'no-task'])
@pytest.mark.parametrize('command', [
    ['sparse-checkout', 'set', '--', 'status'],
    ['submodule', '--quiet', '--cached', 'update', '--', 'status'],
    ['-c', 'alias.inner=submodule --quiet update -- status', '-c', 'alias.outer=inner', 'outer'],
    ['-c', 'alias.widen=sparse-checkout add src', 'widen'],
])
def test_t0931_slot_paths_global_options_alias_and_ownership(topology, mode, command):
    home, slot = topology
    _t0931_guard_owner(home, mode)
    board = _load('t0931_anchor_alias', BOARD_PY)
    result = engine_guard.judge_git_anchor(pm_paths.home_at(home), 'missing' if mode == 'no-task' else 'S', str(home), ['git', '-C', str(slot), *command])
    assert result['verdict'] == ('ok' if mode == 'own' else 'deny'), result


@pytest.mark.parametrize('mode', ['own', 'foreign', 'idle', 'readonly', 'no-task'])
@pytest.mark.parametrize('command', [['sparse-checkout', 'add', 'src'], ['submodule', 'update', '--', 'status']])
def test_t0931_slot_paths_populated_nested_submodule_joins_parent(topology, tmp_path, mode, command):
    """중첩 Git root도 실제 소유 슬롯에 조인한다. update/clone/fetch는 실행하지 않는다."""
    home, slot = topology
    _t0931_guard_owner(home, mode)
    # 외부 clone 없이 gitlink/.gitmodules/독립 Git 포인터로 populated submodule을 선언한다.
    module = slot / 'vendor/status'
    module.mkdir(parents=True)
    git_dir = tmp_path / 'module-git-dir'
    _git(module, 'init', '-q', '--separate-git-dir', str(git_dir), '-b', 'main')
    _git(module, 'commit', '-qm', 'module seed', '--allow-empty')
    from _git_fixture import git
    head = git(module, 'rev-parse', 'HEAD').stdout.strip()
    (slot / '.gitmodules').write_text('[submodule "vendor/status"]\n\tpath = vendor/status\n\turl = ./local-fixture\n', encoding='utf-8')
    _git(slot, 'add', '.gitmodules')
    _git(slot, 'update-index', '--add', '--cacheinfo', f'160000,{head},vendor/status')
    _git(slot, 'config', 'submodule.vendor/status.url', './local-fixture')
    status = git(slot, 'submodule', 'status', '--', 'vendor/status').stdout
    assert head in status and not status.startswith('-')
    board = _load('t0931_nested_root', BOARD_PY)
    result = engine_guard.judge_git_anchor(pm_paths.home_at(home), 'missing' if mode == 'no-task' else 'S', str(slot), ['git', '-C', 'vendor/status', *command])
    assert result['verdict'] == ('ok' if mode == 'own' else 'deny'), result
    read = engine_guard.judge_git_anchor(pm_paths.home_at(home), 'S', str(module), ['git', 'submodule', '--quiet', 'status'])
    assert read['verdict'] != 'deny'


@pytest.mark.parametrize('harness', ['claude', 'codex', 'opencode'])
def test_t0931_harness_hooks_share_owner_and_query_contract(topology, monkeypatch, capsys, harness):
    """실제 hook 입력/출력 배선을 사용하며 git 변경 명령 자체는 실행하지 않는다."""
    home, slot = topology
    _t0931_guard_owner(home, 'foreign')
    cases = [('git sparse-checkout set src', True), ('git submodule --quiet update -- status', True), ('git sparse-checkout list', False), ('git submodule', False)]
    if harness == 'claude':
        driver = _load('t0931_claude_hook', CLAUDE_DRIVER)
        monkeypatch.setattr(driver, '_load_board', lambda root: _load('t0931_claude_board', BOARD_PY))
        for command, denied in cases:
            payload = {'session_id': 'S', 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'cwd': str(slot), 'tool_input': {'command': command}}
            monkeypatch.setattr(driver.sys, 'stdin', io.StringIO(json.dumps(payload)))
            assert driver.run_git_anchor_hook(home) == 0
            raw = capsys.readouterr().out
            result = json.loads(raw) if raw else {}
            assert (result.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny') is denied
        _t0931_guard_owner(home, 'own')
        data = json.loads((home / '.project_manager/.local/worktree-leases.json').read_text(encoding='utf-8'))
        data['leases'][0].update(session='t1', state='leased', role='work')
        (home / '.project_manager/.local/worktree-leases.json').write_text(json.dumps(data), encoding='utf-8')
        payload['tool_input'] = {'command': 'git sparse-checkout add src'}
        monkeypatch.setattr(driver.sys, 'stdin', io.StringIO(json.dumps(payload)))
        assert driver.run_git_anchor_hook(home) == 0
        raw = capsys.readouterr().out
        assert not raw or json.loads(raw).get('hookSpecificOutput', {}).get('permissionDecision') != 'deny'
    elif harness == 'codex':
        dispatcher = _load('t0931_codex_hook', CODEX_DISPATCHER)
        monkeypatch.setattr(dispatcher, '_load_board', lambda root: _load('t0931_codex_board', BOARD_PY))
        for command, denied in cases:
            payload = _codex_live_bash_event()
            payload.update(session_id='S', cwd=str(slot), tool_input={'command': command})
            out = io.StringIO()
            assert dispatcher.run_git_anchor_hook(home, stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
            assert (json.loads(out.getvalue()).get('decision') == 'block') is denied
        ledger = home / '.project_manager/.local/worktree-leases.json'
        data = json.loads(ledger.read_text(encoding='utf-8')); data['leases'][0].update(session='t1', role='work', state='leased')
        ledger.write_text(json.dumps(data), encoding='utf-8')
        payload['tool_input'] = {'command': 'git sparse-checkout add src'}
        assert dispatcher.git_anchor_hook_evaluate(payload, home).get('decision') != 'block'
    else:
        node = shutil.which('node')
        assert node is not None, '실제 OpenCode hook 검증에 node가 필요하다'
        shutil.copytree(BOARD_PY.parent, home / '.project_manager/tools')
        script = r'''
const assert = require('node:assert');
const core = require('./git-anchor-core.cjs');
const fs = require('node:fs');
const path = require('node:path');
const [home, slot] = process.argv.slice(1);
(async () => {
 const judge = (_root, cwd, command, options) => core.judgeCommand(home, cwd, command, options);
 const hooks = await core.makeGitAnchorPlugin(judge)({directory:slot, worktree:slot, client:{
  tui:{showToast:async () => {}}, session:{get:async ({path}) => ({data:{id:path.id}})}
 }});
 for (const command of ['git sparse-checkout set src', 'git submodule --quiet update -- status'])
  await assert.rejects(hooks['tool.execute.before']({tool:'bash',sessionID:'S'},{args:{command,cwd:slot}}), /git-anchor\/deny/);
 for (const command of ['git sparse-checkout list', 'git submodule'])
  await hooks['tool.execute.before']({tool:'bash',sessionID:'S'},{args:{command,cwd:slot}});
 const file = path.join(home,'.project_manager','.local','worktree-leases.json');
 const data = JSON.parse(fs.readFileSync(file,'utf8'));
 Object.assign(data.leases[0], {session:'t1',role:'work',state:'leased'});
 fs.writeFileSync(file,JSON.stringify(data));
 await hooks['tool.execute.before']({tool:'bash',sessionID:'S'},{args:{command:'git sparse-checkout add src',cwd:slot}});
 console.log('T0931_HOOK_OK');
})().catch(error => { console.error(error); process.exitCode=1; });
'''
        result = subprocess.run([node, '-e', script, str(home), str(slot)], cwd=OPEN_CORE.parent, capture_output=True, text=True, encoding='utf-8')
        assert result.returncode == 0, result.stderr
        assert 'T0931_HOOK_OK' in result.stdout


@pytest.mark.parametrize('mode', ['own', 'foreign', 'idle', 'readonly', 'no-task'])
@pytest.mark.parametrize('help_arg', ['-h', '--help'])
@pytest.mark.parametrize('form', ['direct', 'chdir', 'alias'])
def test_t0931_foreach_payload_help_ownership(topology, mode, help_arg, form):
    home, slot = topology
    _t0931_guard_owner(home, mode)
    board = _load('t0931_foreach_owner', BOARD_PY)
    command = ['submodule', '--quiet', 'foreach', '--recursive', 'printf', '%s', help_arg]
    cwd = slot
    if form == 'chdir':
        cwd = home
        command = ['-C', str(slot), *command]
    elif form == 'alias':
        command = ['-c', 'alias.payload=submodule --quiet foreach --recursive printf %s', 'payload', help_arg]
    result = engine_guard.judge_git_anchor(pm_paths.home_at(home), 'missing' if mode == 'no-task' else 'S', str(cwd), ['git', *command])
    assert result['verdict'] == ('ok' if mode == 'own' else 'deny'), result


@pytest.mark.parametrize('mode', ['own', 'foreign', 'idle', 'readonly', 'no-task'])
@pytest.mark.parametrize('help_arg', ['-h', '--help'])
def test_t0931_foreach_payload_help_populated_nested_root(topology, tmp_path, mode, help_arg):
    """로컬 populated submodule에서 printf만 실행해 payload 전달과 중첩 소유 판정을 검증한다."""
    home, slot = topology
    _t0931_guard_owner(home, mode)
    module = slot / 'vendor/status'
    module.mkdir(parents=True)
    _git(module, 'init', '-q', '--separate-git-dir', str(tmp_path / 'foreach-module-git'), '-b', 'main')
    _git(module, 'commit', '-qm', 'module seed', '--allow-empty')
    from _git_fixture import git
    head = git(module, 'rev-parse', 'HEAD').stdout.strip()
    (slot / '.gitmodules').write_text('[submodule "vendor/status"]\n\tpath = vendor/status\n\turl = ./local-fixture\n', encoding='utf-8')
    _git(slot, 'add', '.gitmodules')
    _git(slot, 'update-index', '--add', '--cacheinfo', f'160000,{head},vendor/status')
    _git(slot, 'config', 'submodule.vendor/status.url', './local-fixture')
    actual = git(slot, 'submodule', 'foreach', 'printf', '%s', help_arg)
    assert actual.returncode == 0 and actual.stdout.endswith(help_arg), actual
    board = _load('t0931_foreach_nested', BOARD_PY)
    session = 'missing' if mode == 'no-task' else 'S'
    for cwd, prefix in [(slot, []), (slot, ['-C', 'vendor/status']), (module, [])]:
        result = engine_guard.judge_git_anchor(pm_paths.home_at(home), session, str(cwd), ['git', *prefix, 'submodule', 'foreach', 'printf', '%s', help_arg])
        assert result['verdict'] == ('ok' if mode == 'own' else 'deny'), result


@pytest.mark.parametrize('command', [
    ['submodule', '-h'], ['submodule', '--help'],
    ['submodule', '--quiet', 'foreach', '-h'],
    ['submodule', 'foreach', '--recursive', '--help'],
    ['submodule', '--help', 'foreach', 'printf', '%s', '--help'],
    ['submodule', 'status'], ['submodule', 'summary'],
    ['sparse-checkout', '--help'], ['sparse-checkout', 'list'],
])
def test_t0931_foreach_payload_help_git_query(topology, command):
    home, slot = topology
    _t0931_guard_owner(home, 'foreign')
    board = _load('t0931_foreach_query', BOARD_PY)
    result = engine_guard.judge_git_anchor(pm_paths.home_at(home), 'S', str(slot), ['git', *command])
    assert result['verdict'] == 'ok', result


@pytest.mark.parametrize('harness', ['claude', 'codex', 'opencode'])
@pytest.mark.parametrize('help_arg', ['-h', '--help'])
def test_t0931_foreach_payload_help_harness_hooks(topology, monkeypatch, capsys, harness, help_arg):
    """각 실제 hook에 소유 행렬과 Git 자체 help를 전달한다. payload는 실행하지 않는다."""
    home, slot = topology
    ledger = home / '.project_manager/.local/worktree-leases.json'
    original = ledger.read_text(encoding='utf-8')
    command = f'git submodule foreach printf %s {help_arg}'
    if harness == 'claude':
        driver = _load('t0931_foreach_claude', CLAUDE_DRIVER)
        monkeypatch.setattr(driver, '_load_board', lambda root: _load('t0931_foreach_claude_board', BOARD_PY))
    elif harness == 'codex':
        driver = _load('t0931_foreach_codex', CODEX_DISPATCHER)
        monkeypatch.setattr(driver, '_load_board', lambda root: _load('t0931_foreach_codex_board', BOARD_PY))
    else:
        node = shutil.which('node')
        assert node is not None, '실제 OpenCode hook 검증에 node가 필요하다'
        shutil.copytree(BOARD_PY.parent, home / '.project_manager/tools')
    for mode in ['own', 'foreign', 'idle', 'readonly', 'no-task']:
        ledger.write_text(original, encoding='utf-8')
        _t0931_guard_owner(home, mode)
        session = 'missing' if mode == 'no-task' else 'S'
        for text, denied in [(command, mode != 'own'), ('git submodule foreach --help', False)]:
            if harness == 'claude':
                payload = {'session_id': session, 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'cwd': str(slot), 'tool_input': {'command': text}}
                monkeypatch.setattr(driver.sys, 'stdin', io.StringIO(json.dumps(payload)))
                assert driver.run_git_anchor_hook(home) == 0
                raw = capsys.readouterr().out
                result = json.loads(raw) if raw else {}
                assert (result.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny') is denied, (mode, result)
            elif harness == 'codex':
                payload = _codex_live_bash_event()
                payload.update(session_id=session, cwd=str(slot), tool_input={'command': text})
                out = io.StringIO()
                assert driver.run_git_anchor_hook(home, stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
                assert (json.loads(out.getvalue()).get('decision') == 'block') is denied, (mode, out.getvalue())
            else:
                script = r"""
const assert = require('node:assert');
const core = require('./git-anchor-core.cjs');
const [home, slot, session, command, denied] = process.argv.slice(1);
(async () => {
 const judge = (_root, cwd, command, options) => core.judgeCommand(home, cwd, command, options);
 const hooks = await core.makeGitAnchorPlugin(judge)({directory:slot, worktree:slot, client:{
  tui:{showToast:async () => {}}, session:{get:async ({path}) => ({data:{id:path.id}})}
 }});
 const run = () => hooks['tool.execute.before']({tool:'bash',sessionID:session},{args:{command,cwd:slot}});
 if (denied === 'true') await assert.rejects(run(), /git-anchor\/deny/);
 else await run();
 console.log('T0931_FOREACH_HOOK_OK');
})().catch(error => { console.error(error); process.exitCode=1; });
"""
                result = subprocess.run([node, '-e', script, str(home), str(slot), session, text, str(denied).lower()], cwd=OPEN_CORE.parent, capture_output=True, text=True, encoding='utf-8')
                assert result.returncode == 0, (mode, result.stderr)
                assert 'T0931_FOREACH_HOOK_OK' in result.stdout


def test_t0931_foreach_payload_help_option_value():
    """공유 분류기의 옵션 값 소비 계약이다. 이 argv의 실제 Git 유효성을 주장하거나 실행하지 않는다."""
    assert git_policy._git_writes_slot('submodule', ['--path', 'vendor/status', 'foreach', 'printf', '%s', '--help'])
