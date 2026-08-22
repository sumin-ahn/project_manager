"""T-0587 — raw git cwd-misanchor 판정 seam + 3하네스 배선."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
CLAUDE_DRIVER = REPO / "templates" / "claude_code" / ".claude" / "pm_orch_claude.py"
CLAUDE_WRAPPER = REPO / "templates" / "claude_code" / ".claude" / "ctx_stop_hook.sh"
OPEN_CORE = REPO / "templates" / "opencode" / ".opencode" / "lib" / "git-anchor-core.cjs"
OPEN_PLUGIN = REPO / "templates" / "opencode" / ".opencode" / "plugins" / "git-anchor.js"
CODEX_RULES = REPO / "templates" / "codex" / ".codex" / "rules" / "default.rules"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    slot = home / "work" / "product_1"
    slot.parent.mkdir(parents=True)
    _git(source, "worktree", "add", "-q", str(slot))
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"leases": [{"slot": "work/product_1", "repo": "product",
                                               "state": "leased"}]}) + "\n", encoding="utf-8")
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
    got = board.judge_git_anchor(str(home), ["git", "add", "tests/shared.txt"])
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"
    assert "tests/shared.txt" in got["reason"]


def test_false_deny_zero_for_normal_home_slot_and_missing_path(topology, tmp_path):
    board = _load("git_anchor_board_normal", BOARD_PY)
    home, slot = topology
    normal_home = board.judge_git_anchor(
        str(home), ["git", "commit", "-m", "done", "--", ".project_manager/wiki/status.md"]
    )
    normal_slot = board.judge_git_anchor(
        str(slot), ["git", "commit", "-m", "code", "--", "tests/shared.txt"]
    )
    missing = board.judge_git_anchor(str(home), ["git", "add", "tests/not-there.txt"])
    reset_cwd = board.judge_git_anchor(str(tmp_path / "not-a-repo"), ["git", "commit", "-m", "x"])
    assert normal_home["verdict"] == "warn" and normal_home["cwd_identity"] == "pm-home"
    assert normal_slot["verdict"] == "ok" and normal_slot["cwd_identity"] == "slot"
    assert missing["verdict"] == "warn"
    assert reset_cwd["verdict"] == "warn" and reset_cwd["cwd_identity"] == "non-repo"


def test_read_git_is_ok_and_git_dash_c_changes_anchor(topology):
    board = _load("git_anchor_board_read", BOARD_PY)
    home, slot = topology
    assert board.judge_git_anchor(str(home), ["git", "status"])["verdict"] == "ok"
    got = board.judge_git_anchor(str(home), ["git", "-C", str(slot), "add", "tests/shared.txt"])
    assert got["verdict"] == "ok" and got["cwd_identity"] == "slot"


@pytest.mark.parametrize("path_notation", ["posix", "windows"])
def test_shell_parser_uses_worst_git_judgment(topology, path_notation):
    board = _load("git_anchor_board_shell", BOARD_PY)
    home, slot = topology
    shell_slot = _shell_slot_path(home, slot, path_notation)
    got = board.judge_git_anchor_command(
        str(home), "echo pre && git status && git add -- tests/shared.txt"
    )
    assert got["verdict"] == "deny"
    reversed_calls = board.judge_git_anchor_command(
        str(home), "git add -- tests/shared.txt && git status"
    )
    assert reversed_calls["verdict"] == "deny"
    assert board.judge_git_anchor_command(str(home), "python3 build.py")["verdict"] == "ok"
    # git 자체는 정상 slot 판정이지만 cwd 잔존 패턴은 별도 warn으로 승격한다.
    changed = board.judge_git_anchor_command(
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
    got = board.judge_engine_invocation(str(home), argv)
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
    got = board.judge_engine_invocation(str(home), argv)
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"
    assert "cwd의 tests/ 디렉터리가 실재하지 않음" in got["reason"]


def test_engine_guard_denies_worktree_board_mutation(topology):
    """(b) board mutation 소유자는 PM 홈이며 canonical 사본 선택은 deny."""
    board = _load("engine_anchor_slot_board_mutation", BOARD_PY)
    home, slot = topology
    _seed_engine_copy(home, "v-old")
    _seed_engine_copy(slot, "v-new")
    got = board.judge_engine_invocation(
        str(slot), ["python3", ".project_manager/tools/board.py", "complete", "T-0001"],
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "slot"
    assert "board mutation 'complete'" in got["reason"]
    assert str(slot / ".project_manager" / "tools" / "board.py") in got["reason"]
    assert "board mutation은 PM 홈 사본" in got["reason"]
    via_shell = board.judge_git_anchor_command(
        str(slot), "python3 .project_manager/tools/board.py complete T-0001",
    )
    assert via_shell["verdict"] == "deny"
    repeated_slash = board.judge_git_anchor_command(
        str(slot), "python3 .project_manager//tools/board.py claim T-1",
    )
    assert repeated_slash["verdict"] == "deny"

    # cwd가 아니라 선택한 script 사본이 엔진 repo 앵커를 정한다.
    slot_script = slot / ".project_manager" / "tools" / "board.py"
    from_home = board.judge_engine_invocation(
        str(home), ["python3", str(slot_script), "complete", "T-0001"],
    )
    assert from_home["verdict"] == "deny"
    home_script = home / ".project_manager" / "tools" / "board.py"
    pm_owned = board.judge_engine_invocation(
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
    got = board.judge_engine_invocation(
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
    via_shell = board.judge_git_anchor_command(
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
    got = board.judge_engine_invocation(
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
    got = board.judge_engine_invocation(
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
    got_same = board.judge_engine_invocation(
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
    got_differs = board.judge_engine_invocation(
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

    got = board.judge_engine_invocation(
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
    got = board.judge_engine_invocation(
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
    note = board._engine_copy_freshness(
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
        got = board.judge_git_anchor_command(
            str(home),
            f"python3 {token}/.project_manager/tools/pm_delegate.py status",
        )
        assert got["verdict"] == "warn"
        assert f"미확장 토큰={token}" in got["reason"]
        assert "확장 불가 — 실제 사본 미상" in got["reason"]
        assert "실행 사본=" not in got["reason"]
        assert "repo 앵커=" not in got["reason"]

    denied = board.judge_git_anchor_command(
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
    monkeypatch.setattr(board.file_lock, "read_bytes_shared", unreadable)
    got = board.judge_engine_invocation(
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
    got = board.judge_engine_invocation(str(home), ["python3", str(script), "status"])
    assert got["verdict"] == "ok"
    assert f"절대경로 엔진 사본={script}" in got["reason"]
    assert f"repo 앵커={slot} (slot)" in got["reason"]


def test_mixed_git_and_engine_calls_use_strongest_judgment(topology):
    """(e) PM 홈에 tests/가 실재하면 혼합 호출에서도 false-deny가 없다."""
    board = _load("engine_anchor_mixed_rank", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(
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
    got = board.judge_git_anchor_command(
        str(home), "git status && python3 -m pytest tests/",
    )
    assert got["verdict"] == "deny"
    assert "호출 1 [pm-home/ok]" in got["reason"]
    assert "호출 2 [pm-home/deny]" in got["reason"]


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
    got = board.judge_git_anchor_command(str(home), command)
    assert got["verdict"] == "warn"
    assert "대상을 절대경로로 지정하라" in got["reason"]
    assert "cd가 꼭 필요하면 이 호출 안에서만 사용" in got["reason"]
    assert "다음 호출은 cwd를 가정하지 마라" in got["reason"]
    assert "workdir 파라미터" not in got["reason"]

    contextual = board.judge_git_anchor_command(
        str(home), f"echo pre{separator}cd {shell_slot}{separator}git status",
    )
    assert contextual["verdict"] == "warn"
    assert "호출 1 [slot/ok]" in contextual["reason"]


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
    assert board.judge_git_anchor_command(str(home), command)["verdict"] == "ok"


def test_existing_git_mutation_deny_is_unchanged(topology):
    """(g) 엔진 합류 뒤에도 기존 PM 홈 cross-pathspec git deny는 불변."""
    board = _load("engine_anchor_git_regression", BOARD_PY)
    home, _slot = topology
    direct = board.judge_git_anchor(str(home), ["git", "add", "tests/shared.txt"])
    shell = board.judge_git_anchor_command(str(home), "git add tests/shared.txt")
    assert shell == direct
    assert shell["verdict"] == "deny"


def test_posix_escaped_path_operand_keeps_shell_meaning(topology):
    """Windows 표기 정규화가 POSIX ``\\`` escape를 경로 구분자로 바꾸지 않는다."""
    board = _load("engine_anchor_posix_escaped_path", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(
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
    got = board.judge_git_anchor_command(
        str(home), command.format(home=home, slot=slot)
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == identity


@pytest.mark.parametrize("path_notation", ["posix", "windows"])
def test_shell_if_and_multiple_git_calls_keep_context(topology, path_notation):
    board = _load("git_anchor_shell_if_multi", BOARD_PY)
    home, slot = topology
    shell_slot = _shell_slot_path(home, slot, path_notation)
    conditional = board.judge_git_anchor_command(
        str(home), f"if cd {shell_slot}; then git add tests/shared.txt; fi"
    )
    assert conditional["verdict"] == "ok"
    assert conditional["cwd_identity"] == "slot"

    multiple = board.judge_git_anchor_command(
        str(home), f"git -C {shell_slot} commit -m slot && git commit -m home"
    )
    assert multiple["verdict"] == "warn"
    assert "호출 1 [slot/ok]" in multiple["reason"]
    assert "호출 2 [pm-home/warn]" in multiple["reason"]

    reversed_calls = board.judge_git_anchor_command(
        str(home), f"git commit -m home && git -C {shell_slot} commit -m slot"
    )
    assert reversed_calls["verdict"] == "warn"
    assert "호출 1 [pm-home/warn]" in reversed_calls["reason"]
    assert "호출 2 [slot/ok]" in reversed_calls["reason"]


def test_dynamic_cd_is_ambiguous_and_never_blocks(topology):
    board = _load("git_anchor_shell_dynamic_cd", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(
        str(home), "cd $TARGET; git add tests/shared.txt"
    )
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]


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
    got = board.judge_git_anchor_command(str(home), command.format(slot=slot))
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]


def test_unexecuted_cwd_changer_preserves_proven_home_anchor(topology):
    board = _load("git_anchor_shell_unexecuted_pushd", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(
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
    got = board.judge_git_anchor_command(str(home), command)
    assert got["verdict"] in {"ok", "warn"}


def test_real_commands_around_heredoc_are_still_judged(topology):
    board = _load("git_anchor_heredoc_surrounding", BOARD_PY)
    home, _slot = topology
    before = board.judge_git_anchor_command(
        str(home), "git add tests/shared.txt; cat <<EOF\ngit status\nEOF"
    )
    after = board.judge_git_anchor_command(
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
    assert board.judge_git_anchor_command(str(home), command)["verdict"] == "deny"


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
    got = board.judge_git_anchor_command(str(home), command)
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
    got = board.judge_git_anchor_command(str(home), command)
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
    got = board.judge_git_anchor_command(str(home), command)
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]


@pytest.mark.parametrize("path_notation", ["posix", "windows"])
def test_newline_is_a_sequential_shell_boundary(topology, path_notation):
    board = _load("git_anchor_shell_newline", BOARD_PY)
    home, slot = topology
    shell_slot = _shell_slot_path(home, slot, path_notation)
    got = board.judge_git_anchor_command(
        str(home), f"git -C {shell_slot} status\ngit add tests/shared.txt"
    )
    assert got["verdict"] == "deny"
    assert "호출 1 [slot/ok]" in got["reason"]
    assert "호출 2 [pm-home/deny]" in got["reason"]


def test_if_else_tracks_failed_cd_branch_cwd(topology):
    board = _load("git_anchor_shell_else", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(
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
    got = board.judge_git_anchor(str(home), argv)
    assert got["verdict"] == "deny"


def test_shell_redirection_targets_are_not_git_pathspecs(topology):
    board = _load("git_anchor_shell_redirection", BOARD_PY)
    home, _slot = topology
    commit = board.judge_git_anchor_command(
        str(home), "git commit -m msg > tests/shared.txt"
    )
    add = board.judge_git_anchor_command(
        str(home), "git add src/app.py 2> tests/shared.txt"
    )
    still_dangerous = board.judge_git_anchor_command(
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
    assert board.judge_git_anchor_command(str(home), command)["verdict"] == "deny"


def test_pathspecs_resolve_from_invocation_cwd(topology):
    board = _load("git_anchor_pathspec_cwd", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor(str(home / "tests"), ["git", "add", "shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor(str(home), ["git", "-C", "tests", "add", "shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor(str(home / "templates"), ["git", "add", "../tests/shared.txt"])["verdict"] == "deny"


def test_work_tree_and_git_dir_overrides_are_never_approved_or_denied(topology):
    board = _load("git_anchor_work_tree_override", BOARD_PY)
    home, slot = topology
    cases = [
        (str(slot), ["git", "--work-tree", str(home), "add", "tests/shared.txt"]),
        (str(home), ["git", "--work-tree", str(slot), "add", "tests/shared.txt"]),
        (str(slot), [f"GIT_WORK_TREE={home}", "git", "add", "tests/shared.txt"]),
        (str(slot), [f"GIT_DIR={home / '.git'}", "git", "add", "tests/shared.txt"]),
    ]
    assert all(board.judge_git_anchor(cwd, argv)["verdict"] == "warn" for cwd, argv in cases)


def test_git_clean_exclude_values_are_not_pathspecs(topology):
    board = _load("git_anchor_clean_exclude", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor(str(home), ["git", "clean", "-e", "tests/shared.txt"])["verdict"] == "warn"
    assert board.judge_git_anchor(str(home), ["git", "clean", "--exclude", "tests/shared.txt"])["verdict"] == "warn"
    assert board.judge_git_anchor(str(home), ["git", "clean", "-ne", "tests/shared.txt"])["verdict"] == "warn"


def test_r8_parser_boundaries(topology):
    board = _load("git_anchor_r8", BOARD_PY)
    home, slot = topology
    assert board.judge_git_anchor(str(home / "tests"), ["git", "add", ":(top)tests/shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor(str(home / "tests"), ["git", "add", ":/tests/shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor_command(str(home), "echo x# <<EOF\ngit add tests/shared.txt\nEOF")["verdict"] != "deny"
    assert board.judge_git_anchor_command(str(home), "cat <<''\ngit add tests/shared.txt\n\n")["verdict"] != "deny"
    assert board.judge_git_anchor_command(str(home), "'git' add tests/shared.txt")["verdict"] == "deny"
    assert board.judge_git_anchor_command(str(home), "git\\\n add tests/shared.txt")["verdict"] == "deny"
    assert board.judge_git_anchor_command(str(slot), f"env GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt")["verdict"] == "warn"
    assert board.judge_git_anchor_command(str(home), "command git add tests/shared.txt")["verdict"] == "deny"


def test_r9_heredoc_data_preserves_backslash_newline(topology):
    board = _load("git_anchor_r9_heredoc", BOARD_PY)
    home, _slot = topology
    command = "cat <<'EOF'\nEO\\\nF\ngit add tests/shared.txt\nEOF"
    assert board.judge_git_anchor_command(str(home), command)["verdict"] == "ok"
    assert board._normalize_shell_line_continuations("git\\\n add") == "git add"
    quoted = "'git\\\n add'"
    assert board._normalize_shell_line_continuations(quoted) == quoted


def test_r9_wrapper_and_fragmented_command_words(topology):
    board = _load("git_anchor_r9_wrappers", BOARD_PY)
    home, slot = topology
    overrides = [
        f"env -i PATH=/usr/bin:/bin GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
        f"env -- GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
        f"/usr/bin/env GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(str(slot), command)["verdict"] == "warn"
        for command in overrides
    )
    home_mutations = [
        "command -- git add tests/shared.txt",
        "command -p git add tests/shared.txt",
        "g''it add tests/shared.txt",
        "/usr/bin/g''it add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(str(home), command)["verdict"] == "deny"
        for command in home_mutations
    )
    assert board.judge_git_anchor_command(
        str(slot), "command -p git add tests/shared.txt"
    )["verdict"] == "ok"
    assert board.judge_git_anchor_command(
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
        board.judge_git_anchor_command(str(home), command)["verdict"] == "deny"
        for command in wrappers
    )
    assert all(
        board.judge_git_anchor_command(str(slot), command)["verdict"] == "ok"
        for command in wrappers
    )
    assert board.judge_git_anchor_command(
        str(home), "sudo -u root git add tests/shared.txt"
    )["verdict"] == "warn"
    escaped_quote = 'echo \\"; git\\\n add tests/shared.txt'
    assert board.judge_git_anchor_command(str(home), escaped_quote)["verdict"] == "deny"


def test_r11_dynamic_command_word_is_ambiguity_not_ok(topology):
    board = _load("git_anchor_r11_dynamic_wrapper", BOARD_PY)
    home, slot = topology
    dynamic = [
        "$WRAPPER git add tests/shared.txt",
        '"$WRAPPER" git add tests/shared.txt',
    ]
    for cwd in (home, slot):
        assert all(
            board.judge_git_anchor_command(str(cwd), command)["verdict"] == "warn"
            for command in dynamic
        )
    assert board.judge_git_anchor_command(
        str(slot), "git add tests/shared.txt"
    )["verdict"] == "ok"
    data_only = [
        "echo $WRAPPER git add tests/shared.txt",
        "printf %s '$WRAPPER git add tests/shared.txt'",
        "# $WRAPPER git add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(str(home), command)["verdict"] == "ok"
        for command in data_only
    )


def test_pathspec_file_and_ambiguous_ref_forms_fail_open(topology):
    board = _load("git_anchor_pathspec_fail_open", BOARD_PY)
    home, _slot = topology
    from_file = board.judge_git_anchor(
        str(home), ["git", "add", "--pathspec-from-file", "templates/shared.txt"]
    )
    checkout = board.judge_git_anchor(str(home), ["git", "checkout", "tests/shared.txt"])
    reset = board.judge_git_anchor(str(home), ["git", "reset", "tests/shared.txt"])
    combined_message = board.judge_git_anchor(
        str(home), ["git", "commit", "-am", "tests/shared.txt"]
    )
    top_magic_shell = board.judge_git_anchor_command(
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
    assert board.judge_git_anchor(
        str(home), ["git", "add", "tests/shared.txt"]
    )["verdict"] == "warn"

    outside = tmp_path / "product-source"
    symlink_slot = home / "work" / "evil_1"
    symlink_slot.symlink_to(outside, target_is_directory=True)
    ledger.write_text(json.dumps({"leases": [
        {"slot": "work/evil_1", "repo": "evil", "state": "leased"},
    ]}) + "\n", encoding="utf-8")
    assert board.judge_git_anchor(
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
    got = board.judge_git_anchor(str(slot), ["git", "commit", "-m", "normal"])
    assert got["verdict"] == "warn"
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

    alias_got = board.judge_git_anchor(str(alias), ["git", "commit", "-m", "alias"])
    target_got = board.judge_git_anchor(str(slot), ["git", "commit", "-m", "target"])
    assert alias_got["verdict"] == "warn" and alias_got["cwd_identity"] == "worktree"
    assert target_got["verdict"] == "warn" and target_got["cwd_identity"] == "worktree"


def test_registered_slot_mutation_is_explicitly_allowed(topology):
    board = _load("git_anchor_registered_slot_allow", BOARD_PY)
    _home, slot = topology
    got = board.judge_git_anchor(str(slot), ["git", "add", "tests/shared.txt"])
    assert got["verdict"] == "ok"
    assert got["cwd_identity"] == "slot"


@pytest.mark.parametrize("subcommand", ["rm", "mv", "clean", "apply", "stash"])
def test_additional_mutations_warn_at_home_and_allow_registered_slot(topology, subcommand):
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
    home_got = board.judge_git_anchor(str(home), argv)
    slot_got = board.judge_git_anchor(str(slot), argv)
    expected_home = {
        "rm": "deny", "mv": "deny", "clean": "deny", "apply": "warn", "stash": "warn",
    }
    assert home_got["verdict"] == expected_home[subcommand]
    assert slot_got["verdict"] == "ok" and slot_got["cwd_identity"] == "slot"


def test_board_cli_emits_hook_json(topology, capsys):
    board = _load("git_anchor_board_cli", BOARD_PY)
    home, _slot = topology
    rc = board.main(["git-anchor", "--cwd", str(home), "--command", "git add tests/shared.txt"])
    got = json.loads(capsys.readouterr().out)
    assert rc == 0 and got["verdict"] == "deny"


def test_claude_hook_prefilter_warn_and_deny(monkeypatch, topology):
    driver = _load("git_anchor_claude", CLAUDE_DRIVER)
    home, _slot = topology
    calls = []

    class FakeBoard:
        @staticmethod
        def judge_git_anchor_command(cwd, command):
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
        def judge_git_anchor_command(_cwd, _command):
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
    for settings in (REPO / ".claude" / "settings.json",
                     REPO / "templates" / "claude_code" / ".claude" / "settings.json"):
        data = json.loads(settings.read_text(encoding="utf-8"))
        hooks = data["hooks"]["PreToolUse"]
        assert any(row.get("matcher") == "Bash" and "--git-anchor-hook" in json.dumps(row)
                   for row in hooks)


def test_codex_execpolicy_records_capability_gap_without_overbroad_rule():
    text = CODEX_RULES.read_text(encoding="utf-8")
    assert "raw git cwd-anchor 파리티 예외(T-0587)" in text
    assert "cwd predicate" in text and "false-deny 0" in text


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
    ]
    commands = [command for command, _expected in cases]
    expected_matches = [expected for _command, expected in cases]
    calls = []

    class FakeBoard:
        @staticmethod
        def judge_git_anchor_command(cwd, command):
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
    assert claude_matches == opencode_matches == expected_matches


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
  calls += 1;
  return {status:0, stdout:JSON.stringify({verdict:"warn",cwd_identity:"pm-home",reason:"fixture"})+"\n", stderr:""};
};
assert.strictEqual(m.judgeCommand("/r", "/r", "python3 build.py", fake).verdict, "ok");
assert.strictEqual(calls, 0);
assert.strictEqual(m.judgeCommand("/r", "/r", "git commit -m x", fake).verdict, "warn");
assert.strictEqual(calls, 1);
assert.strictEqual(m.judgeCommand("/r", "/r", "python3 .project_manager/tools/board.py list", fake).verdict, "warn");
assert.strictEqual(m.judgeCommand("/r", "/r", "python3 -m pytest tests/ -q", fake).verdict, "warn");
assert.strictEqual(m.judgeCommand("/r", "/r", "cd /tmp && python3 build.py", fake).verdict, "warn");
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
    client: {tui: {showToast: async (value) => toasts.push(value)}},
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
    client: {tui: {showToast: async (value) => errorToasts.push(value)}},
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
