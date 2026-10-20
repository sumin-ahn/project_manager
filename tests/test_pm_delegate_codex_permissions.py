"""codex 위임 권한 보정 — git 메타데이터 쓰기 루트·실행별 네트워크 override·전역 --add-dir 배치.

원 작업공간을 workspace-write 로 여는 codex 실행은 git 이 답한 common_dir·git_dir 를 최소 쓰기
루트로 더한다(worktree·일반 clone·submodule 공통). 읽기 역할의 tmp 재앵커 실행은 원 트리·원 git
메타데이터를 열지 않는다. 모든 `--add-dir` 는 exec 앞 전역 위치에 둔다(재개 하위 명령 뒤는 CLI 가
거부한다). 조회 실패는 빈 루트로 바꾸지 않고 호출 전에 멈춘다.

선택 문자열(metadata·scope·failure·resume·dry_run·cli_formats)은 설계의 필수 시험 계약과 같다.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from _git_fixture import init_git_repo
from pm_engine.domain import delegate_errors
from pm_engine.infra.harness import contract
import test_harness_parity_guard as parity_guard

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

NETWORK_OVERRIDE = ["-c", "sandbox_workspace_write.network_access=true"]
SESSION_ID = "0199a9c4-3f7e-7c12-9a3b-2b4c5d6e7f80"
READ_ROLES = ("researcher", "code-reviewer")
WRITE_ROLES = ("developer", "architect")
REGISTERED_SITE_OCCURRENCES = 107


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("pm_delegate", TOOLS / "pm_delegate.py")


@pytest.fixture(autouse=True)
def _fresh_config_anchor(pd, monkeypatch):
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", None)


def _committed_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    return init_git_repo(root, commit="base")


def _git(*args: str) -> None:
    """픽스처 git 명령 — 실패하면 git 진단을 그대로 보여 준다."""
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)


def _real(path: Path) -> Path:
    return path.resolve(strict=True)


def _global_added_dirs(argv: list[str]) -> list[str]:
    """모든 `--add-dir` 값 — 하나라도 exec 뒤에 있으면 실패한다."""
    exec_index = argv.index("exec")
    positions = [index for index, token in enumerate(argv) if token == "--add-dir"]
    assert all(index < exec_index for index in positions), argv
    return [argv[index + 1] for index in positions]


def _network_override_count(argv: list[str]) -> int:
    starts = [index for index in range(len(argv)) if argv[index:index + 2] == NETWORK_OVERRIDE]
    assert all(index < argv.index("exec") for index in starts), argv
    return len(starts)


def _write_argv(pd, cwd: Path, resume: bool = False) -> list[str]:
    return pd.build_codex_argv("gpt-x", None, "developer", str(cwd),
                               SESSION_ID if resume else None)


def _forbid_git_metadata_lookup(pd, monkeypatch):
    def _forbidden(*_args, **_kwargs):
        pytest.fail("재앵커·read-only 실행이 원 git 메타데이터를 조회했다")
    monkeypatch.setattr(pd, "_git_metadata_write_roots", _forbidden)


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner_returning(result=None, exception: BaseException | None = None):
    calls: list[dict] = []

    def run(command, **kwargs):
        calls.append({"command": list(command), **kwargs})
        if exception is not None:
            raise exception
        return result
    run.calls = calls
    return run


# ── metadata ─────────────────────────────────────────────────────────────

def test_metadata_worktree_opens_common_dir_once(pd, tmp_path):
    """linked worktree 의 git_dir 는 common_dir 아래라 common_dir 하나만 연다(공백·한글 경로)."""
    main = _committed_repo(tmp_path / "주 저장소 공백")
    worktree = tmp_path / "작업 슬롯 1"
    _git("-C", str(main), "worktree", "add", "-q", "-b", "slot-branch", str(worktree))
    roots = pd._git_metadata_write_roots(worktree)
    assert roots == (_real(main / ".git"),)
    assert _real(tmp_path) not in roots


def test_metadata_plain_clone_keeps_in_workspace_dot_git(pd, tmp_path):
    """작업공간 안에 있다는 이유로 일반 clone 의 `.git` 을 생략하지 않는다."""
    repo = _committed_repo(tmp_path / "plain")
    assert pd._git_metadata_write_roots(repo) == (_real(repo / ".git"),)


def test_metadata_submodule_opens_only_its_module_dir(pd, tmp_path):
    """submodule 은 자기 메타데이터 디렉터리만 연다 — 상위 clone 의 `.git` 전체로 넓히지 않는다."""
    upstream = _committed_repo(tmp_path / "upstream")
    parent = _committed_repo(tmp_path / "parent")
    _git("-c", "protocol.file.allow=always", "-C", str(parent), "submodule", "add", "-q",
         str(upstream), "child")
    roots = pd._git_metadata_write_roots(parent / "child")
    assert roots == (_real(parent / ".git" / "modules" / "child"),)
    assert _real(parent / ".git") not in roots


def test_metadata_subdirectory_cwd_resolves_to_repo_metadata(pd, tmp_path):
    repo = _committed_repo(tmp_path / "repo")
    sub = repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    assert pd._git_metadata_write_roots(sub) == (_real(repo / ".git"),)


def test_metadata_relative_gitdir_pointer(pd, tmp_path):
    """`.git` 포인터 파일이 상대경로여도 git 이 답한 실제 위치를 연다."""
    work = tmp_path / "work"
    separate = tmp_path / "meta" / "repo.git"
    work.mkdir()
    separate.parent.mkdir()
    _git("init", "-q", "--separate-git-dir", str(separate), str(work))
    (work / ".git").write_text("gitdir: ../meta/repo.git\n", encoding="utf-8")
    assert pd._git_metadata_write_roots(work) == (_real(separate),)


@pytest.mark.parametrize("shape,expected", [
    ("same", ("common",)),
    ("git_inside_common", ("common",)),
    ("common_inside_git", ("git",)),
    ("independent", ("common", "git")),
])
def test_metadata_minimal_root_set(pd, tmp_path, shape, expected):
    """중복·포함 관계면 바깥 하나, 서로 독립이면 둘 — 부모 경로를 합치지 않는다."""
    common = tmp_path / "common"
    git_dir = {
        "same": common,
        "git_inside_common": common / "worktrees" / "slot",
        "common_inside_git": tmp_path / "gitdir",
        "independent": tmp_path / "elsewhere" / "gitdir",
    }[shape]
    if shape == "common_inside_git":
        common = git_dir / "common"
    common.mkdir(parents=True, exist_ok=True)
    git_dir.mkdir(parents=True, exist_ok=True)
    runner = _runner_returning(_Completed(0, f"{common}\n{git_dir}\n"))
    roots = pd._git_metadata_write_roots(tmp_path, git_run_fn=runner)
    by_name = {"common": _real(common), "git": _real(git_dir)}
    assert roots == tuple(by_name[name] for name in expected)
    assert _real(tmp_path) not in roots
    call = runner.calls[0]
    assert call["command"] == [
        "git", "-C", str(tmp_path), "rev-parse", "--path-format=absolute",
        "--git-common-dir", "--git-dir",
    ]
    assert call["errors"] == "strict" and call["encoding"] == "utf-8"


@pytest.mark.parametrize("role", WRITE_ROLES)
def test_metadata_write_argv_adds_global_root_and_network(pd, tmp_path, role):
    main = _committed_repo(tmp_path / "main")
    worktree = tmp_path / "slot"
    _git("-C", str(main), "worktree", "add", "-q", "-b", "slot-branch", str(worktree))
    argv = pd._apply_workspace_git_dirs(
        pd.build_codex_argv("gpt-x", None, role, str(worktree)), "workspace-add-dir", worktree,
    )
    assert _global_added_dirs(argv) == [str(_real(main / ".git"))]
    assert _network_override_count(argv) == 1
    assert argv[argv.index("-C") + 1] == str(worktree)


def test_metadata_write_attempt_transport_adds_git_root(pd, tmp_path):
    repo = _committed_repo(tmp_path / "repo")
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "developer", repo, "task",
    )
    assert read_tmp is None and prompt_path is None
    assert _global_added_dirs(argv) == [str(_real(repo / ".git"))]


# ── scope ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", READ_ROLES)
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("with_run_dir", [False, True])
def test_scope_read_reanchor_never_opens_source_git(
        pd, tmp_path, monkeypatch, role, resume, with_run_dir):
    """tmp 가 저장소(PM 홈 형상) 안이어도 부모 저장소를 탐색하지 않고 원 git·원 트리를 열지 않는다."""
    home = _committed_repo(tmp_path / "home")
    source = _committed_repo(home / "work" / "slot")
    temp_root = home / ".project_manager" / ".local" / "tmp"
    temp_root.mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(pd, "_read_tmp_root", lambda: temp_root)
    _forbid_git_metadata_lookup(pd, monkeypatch)
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, role, source, "review",
        SESSION_ID if resume else None, run_dir if with_run_dir else None,
    )
    try:
        assert read_tmp is not None
        assert argv[argv.index("-s") + 1] == "workspace-write"
        assert argv[argv.index("-C") + 1] == str(read_tmp.path)
        added = _global_added_dirs(argv)
        assert added == ([str(run_dir)] if with_run_dir else [])
        assert str(source) not in added
        assert str(_real(source / ".git")) not in added
        assert str(_real(home / ".git")) not in added
        assert _network_override_count(argv) == 1
        if resume:
            assert argv[-2:] == [SESSION_ID, "-"]
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


@pytest.mark.parametrize("role", READ_ROLES)
def test_scope_read_without_tmp_stays_read_only_without_git_roots(
        pd, tmp_path, monkeypatch, role):
    """tmp 를 준비하지 못한 실행에 원 git 쓰기를 여는 보상 동작이 없다."""
    source = _committed_repo(tmp_path / "source")
    monkeypatch.setattr(pd, "_create_read_role_temp", lambda *_a, **_k: None)
    _forbid_git_metadata_lookup(pd, monkeypatch)
    argv, _stdin, _prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, role, source, "review",
    )
    assert read_tmp is None
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--add-dir" not in argv
    assert _network_override_count(argv) == 1


def test_scope_write_role_with_other_exec_root_skips_lookup(pd, tmp_path, monkeypatch):
    """판정은 역할이 아니라 최종 `-C` 실경로다 — 원 작업공간이 아니면 원 git 을 조회하지 않는다."""
    _forbid_git_metadata_lookup(pd, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    argv = _write_argv(pd, other)
    assert pd._apply_workspace_git_dirs(argv, "workspace-add-dir", tmp_path) == argv


# ── failure ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exception", [
    FileNotFoundError(2, "No such file or directory", "git"),
    subprocess.TimeoutExpired(["git"], 5),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
])
def test_failure_runner_exception_is_loud(pd, tmp_path, exception):
    runner = _runner_returning(exception=exception)
    with pytest.raises(delegate_errors.DelegateError, match="git 메타데이터 위치 조회 실패") as info:
        pd._git_metadata_write_roots(tmp_path, git_run_fn=runner)
    assert info.value.__cause__ is exception


def test_failure_nonzero_exit_carries_code_and_diagnostic(pd, tmp_path):
    runner = _runner_returning(_Completed(128, "", "fatal: not a git repository"))
    with pytest.raises(delegate_errors.DelegateError, match="rc=128.*not a git repository"):
        pd._git_metadata_write_roots(tmp_path, git_run_fn=runner)


@pytest.mark.parametrize("stdout", ["", "\n", "/only/one\n", "/a\n/b\n/c\n", "/a\n\n"])
def test_failure_malformed_output_is_not_empty_roots(pd, tmp_path, stdout):
    runner = _runner_returning(_Completed(0, stdout))
    with pytest.raises(delegate_errors.DelegateError, match="두 경로가 아님"):
        pd._git_metadata_write_roots(tmp_path, git_run_fn=runner)


def test_failure_relative_path(pd, tmp_path):
    runner = _runner_returning(_Completed(0, f"{tmp_path}\n.git\n"))
    with pytest.raises(delegate_errors.DelegateError, match="절대경로가 아님"):
        pd._git_metadata_write_roots(tmp_path, git_run_fn=runner)


def test_failure_missing_directory(pd, tmp_path):
    runner = _runner_returning(_Completed(0, f"{tmp_path / 'gone'}\n{tmp_path}\n"))
    with pytest.raises(delegate_errors.DelegateError, match="해소 실패"):
        pd._git_metadata_write_roots(tmp_path, git_run_fn=runner)


def test_failure_not_a_directory(pd, tmp_path):
    plain_file = tmp_path / "file"
    plain_file.write_text("x", encoding="utf-8")
    runner = _runner_returning(_Completed(0, f"{tmp_path}\n{plain_file}\n"))
    with pytest.raises(delegate_errors.DelegateError, match="디렉터리가 아님"):
        pd._git_metadata_write_roots(tmp_path, git_run_fn=runner)


@pytest.mark.parametrize("argv,match", [
    (["codex", "-a", "never", "-s", "danger-full-access", "exec", "-C", "/w"], "선언 밖"),
    (["codex", "-a", "never", "-s", "workspace-write", "exec", "--json"], "-C 실행 root"),
    (["codex", "-a", "never", "-s", "workspace-write", "-C", "/w"], "exec"),
    (["codex", "-a", "never", "-s"], "값이 없음"),
    (["codex", "--json", "x", "-s", "read-only", "exec", "-C", "/w"], "손상"),
])
def test_failure_corrupt_argv(pd, tmp_path, argv, match):
    with pytest.raises(delegate_errors.DelegateError, match=match):
        pd._apply_workspace_git_dirs(argv, "workspace-add-dir", tmp_path)


@pytest.mark.parametrize("argv", [
    ["codex", "-a", "never", "--json", "exec"],
    ["codex", "-a", "never", "-s", "workspace-write"],
    ["codex", "-a"],
])
def test_failure_formatter_rejects_corrupt_global_section(argv):
    with pytest.raises(contract.HarnessContractError):
        contract.with_codex_workspace_dirs(argv, ["/r"])


def test_failure_formatter_rejects_empty_root():
    with pytest.raises(contract.HarnessContractError, match="비었다"):
        contract.with_codex_workspace_dirs(["codex", "exec"], [""])


def test_failure_git_error_stops_before_child_spawn(pd, tmp_path, monkeypatch):
    """git 조회 실패는 자식 호출 0회로 끝난다 — 빈 루트로 바꿔 실행을 잇지 않는다."""
    repo = _committed_repo(tmp_path / "repo")
    real_run = pd.subprocess.run

    def run(argv, **kwargs):
        if "--git-common-dir" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: boom")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(pd.subprocess, "run", run)
    spawned: list = []
    with pytest.raises(delegate_errors.DelegateError, match="fatal: boom"):
        pd._execute_attempt(
            harness="codex", model="gpt-x", reasoning=None, role="developer", cwd=repo,
            prompt="task", timeout=30, output_dir=tmp_path / "raw",
            run_fn=lambda *a, **k: spawned.append(a), attempt="primary",
        )
    assert spawned == []


def test_failure_after_read_tmp_cleans_the_attempt_tmp(pd, tmp_path, monkeypatch):
    """finalizer 실패도 기존 정리 경계 안이다 — 만든 attempt tmp 가 남지 않는다."""
    source = _committed_repo(tmp_path / "source")
    temp_root = tmp_path / "pm-home-temp"
    temp_root.mkdir()
    monkeypatch.setattr(pd, "_read_tmp_root", lambda: temp_root)

    def _broken(*_args, **_kwargs):
        raise delegate_errors.DelegateError("finalizer 실패")

    monkeypatch.setattr(pd, "_apply_workspace_git_dirs", _broken)
    with pytest.raises(delegate_errors.DelegateError, match="finalizer 실패"):
        pd._prepare_attempt_transport("codex", "gpt-x", None, "code-reviewer", source, "r")
    assert list(temp_root.iterdir()) == []


# ── resume ───────────────────────────────────────────────────────────────

def test_resume_write_argv_keeps_add_dir_global_and_positionals_last(pd, tmp_path):
    repo = _committed_repo(tmp_path / "repo")
    argv = pd._apply_workspace_git_dirs(_write_argv(pd, repo, resume=True),
                                        "workspace-add-dir", repo)
    assert _global_added_dirs(argv) == [str(_real(repo / ".git"))]
    assert argv[argv.index("exec"):argv.index("exec") + 2] == ["exec", "resume"]
    assert argv[-2:] == [SESSION_ID, "-"]
    assert _network_override_count(argv) == 1


def test_resume_read_run_dir_is_global_not_resume_option(pd, tmp_path, monkeypatch):
    """run_dir 옵션이 `exec resume` 뒤(CLI rc=2)로 내려가지 않는다."""
    source = _committed_repo(tmp_path / "source")
    temp_root = tmp_path / "pm-home-temp"
    temp_root.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(pd, "_read_tmp_root", lambda: temp_root)
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "code-reviewer", source, "r", SESSION_ID, run_dir,
    )
    try:
        resume_index = argv.index("resume")
        assert "--add-dir" not in argv[resume_index:]
        assert _global_added_dirs(argv) == [str(run_dir)]
        assert argv[-2:] == [SESSION_ID, "-"]
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


def test_resume_new_and_resume_share_the_same_formatter(pd, tmp_path):
    fresh = contract.with_codex_workspace_dirs(_write_argv(pd, tmp_path), ["/r1", "/r2"])
    resumed = contract.with_codex_workspace_dirs(_write_argv(pd, tmp_path, True), ["/r1", "/r2"])
    for argv in (fresh, resumed):
        assert _global_added_dirs(argv) == ["/r1", "/r2"]
        exec_index = argv.index("exec")
        assert argv[exec_index - 4:exec_index] == ["--add-dir", "/r1", "--add-dir", "/r2"]


# ── dry_run ──────────────────────────────────────────────────────────────

def _codex_conf(role: str) -> dict:
    return {"delegate.enabled": "false",
            f"delegate.{role}.harness": "codex", f"delegate.{role}.model": "gpt-x"}


@pytest.fixture
def dry_run_argv(pd, monkeypatch, tmp_path, capsys):
    def run(role: str, cwd: Path) -> list[str]:
        prompt = tmp_path / "prompt.md"
        prompt.write_text("작업 지시", encoding="utf-8")
        monkeypatch.setattr(pd, "local_config", lambda: _codex_conf(role))

        def _no_side_effect(*_args, **_kwargs):
            pytest.fail("dry-run 이 임시 디렉터리·쓰기 프로브를 만들었다")

        monkeypatch.setattr(pd, "_create_read_role_temp", _no_side_effect)
        monkeypatch.setattr(pd, "_probe_writable_target", _no_side_effect)
        spawned: list = []
        rc = pd.main(["--role", role, "--prompt-file", str(prompt), "--cwd", str(cwd),
                      "--output-dir", str(tmp_path / "raw"), "--dry-run"],
                     run_fn=lambda *a, **k: spawned.append(a))
        out = capsys.readouterr().out
        assert rc == 0, out
        assert spawned == []
        line = next(line for line in out.splitlines() if line.startswith("argv: "))
        return line[len("argv: "):].split(" "), out
    return run


def test_dry_run_write_shows_git_root(pd, tmp_path, dry_run_argv):
    repo = _committed_repo(tmp_path / "repo")
    argv, _out = dry_run_argv("developer", repo)
    assert _global_added_dirs(argv) == [str(_real(repo / ".git"))]
    assert _network_override_count(argv) == 1


def test_dry_run_read_hides_source_git_root_and_keeps_preamble(
        pd, tmp_path, monkeypatch, dry_run_argv):
    repo = _committed_repo(tmp_path / "repo")
    _forbid_git_metadata_lookup(pd, monkeypatch)
    argv, out = dry_run_argv("researcher", repo)
    assert "--add-dir" not in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert _network_override_count(argv) == 1
    if pd._read_tmp_strategy() is not None:
        predicted_exec_root, _writable = pd._predict_read_tmp_paths("codex")
        assert str(predicted_exec_root.parent) in out   # 예측 프리앰블 보존(이름 꼬리는 무작위)


# ── cli_formats ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", WRITE_ROLES + READ_ROLES)
def test_cli_formats_claude_and_opencode_argv_unchanged(pd, tmp_path, role):
    """다른 CLI 형식 argv 는 finalizer 를 지나도 바이트 그대로다(git 조회 없음)."""
    claude = pd.build_claude_argv("opus", "high", role)
    opencode = pd.build_opencode_argv("prov/m", "high", role, str(tmp_path),
                                      str(tmp_path / "p.md"))
    assert pd._apply_workspace_git_dirs(
        list(claude), pd._READ_TMP_ARGV_MODE_BY_HARNESS["claude"], tmp_path) == claude
    assert pd._apply_workspace_git_dirs(
        list(opencode), pd._READ_TMP_ARGV_MODE_BY_HARNESS["opencode"], tmp_path) == opencode
    assert "--add-dir" not in claude and "--add-dir" not in opencode
    assert "sandbox_workspace_write.network_access=true" not in claude + opencode


def test_cli_formats_codex_mode_comes_from_existing_declaration(pd):
    assert pd._READ_TMP_ARGV_MODE_BY_HARNESS == {
        "codex": "workspace-add-dir", "claude": "add-dir", "opencode": "role-agent",
    }


def test_cli_formats_new_functions_add_no_harness_or_role_branch():
    """새 함수에 하네스 이름·역할축 분기가 없다(기존 파리티 가드 측정 함수로 판정)."""
    measured = parity_guard._measured_sites(parity_guard._engine_sources())
    new_functions = {
        ("pm_delegate.py", "_git_metadata_write_roots"),
        ("pm_delegate.py", "_apply_workspace_git_dirs"),
        ("pm_delegate.py", "_codex_workspace_dirs"),
        ("contract.py", "read_codex_global_section"),
        ("contract.py", "with_codex_workspace_dirs"),
    }
    offenders = [key for key in measured if key[:2] in new_functions]
    assert offenders == []
    # 등록 발생 수는 늘지 않는다(원장 면제·상한 인상 없이 107 유지).
    assert sum(measured.values()) == REGISTERED_SITE_OCCURRENCES
    assert sum(site.expected_count for site in parity_guard.ledger().values()) == (
        REGISTERED_SITE_OCCURRENCES)
    assert REGISTERED_SITE_OCCURRENCES <= parity_guard.LEDGER_CEILING


def test_cli_formats_no_legacy_mode_knob(pd, tmp_path, monkeypatch):
    """옛 동작을 고르는 설정이 없다 — 네트워크 키를 설정에 넣어도 override 는 그대로 한 번 실린다."""
    repo = _committed_repo(tmp_path / "repo")
    conf = {**_codex_conf("developer"), "delegate.codex.network": "false"}
    monkeypatch.setattr(pd, "local_config", lambda: conf)
    argv, _stdin, _prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "developer", repo, "task",
    )
    assert read_tmp is None
    assert _network_override_count(argv) == 1
