"""repo-owned 파일 열거 seam과 출하 소비처의 모드 계약 회귀."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

import _repo_owned_inventory as test_inventory
from _win_skip import _can_symlink


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
requires_symlink = pytest.mark.skipif(
    not _can_symlink(),
    reason="Windows: symlink requires Developer Mode/admin",
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def repo_files():
    return _load("repo_owned_files_test", "repo_owned_files.py")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_modes_declare_exact_git_argv_and_literal_pathspec(repo_files, tmp_path):
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(argv)
        return 0, ""

    repo_files.list_repo_owned_files(
        tmp_path, "pkg*[x]", mode=repo_files.TRACKED_ONLY, git_runner=runner)
    repo_files.list_repo_owned_files(
        tmp_path, "pkg*[x]", mode=repo_files.OWNED, git_runner=runner)

    assert calls == [
        [
            "ls-files",
            "-z",
            "--stage",
            "--cached",
            "--",
            ":(literal)pkg*[x]",
        ],
        [
            "ls-files", "-z", "--cached", "--others", "--exclude-standard",
            "--", ":(literal)pkg*[x]",
        ],
    ]


def test_tracked_only_works_with_pre_238_git_runner(repo_files, tmp_path):
    """구 git shim: 신형 출력 옵션은 rc 129지만 오래된 --stage 호출은 tracked 결과를 보존한다."""
    ship = tmp_path / "ship"
    ship.mkdir()
    (ship / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (ship / "machine.local").write_text("machine\n", encoding="utf-8")
    calls: list[list[str]] = []
    unsupported_option = "--" + "format"

    def pre_238_runner(argv):
        calls.append(argv)
        if any(arg.startswith(unsupported_option) for arg in argv):
            return 129, "error: unknown option `format'"
        return (
            0,
            "100644 1111111111111111111111111111111111111111 0\t"
            "ship/tracked.txt\0",
        )

    entries = repo_files.list_repo_owned_entries(
        tmp_path,
        "ship",
        mode=repo_files.TRACKED_ONLY,
        git_runner=pre_238_runner,
    )
    assert entries == [
        repo_files.RepoOwnedEntry(Path("ship/tracked.txt"), "100644")
    ]
    assert all("--stage" in call for call in calls)
    assert not any(
        arg.startswith(unsupported_option)
        for call in calls
        for arg in call
    )
    assert Path("ship/machine.local") not in {entry.path for entry in entries}


def test_damaged_stage_record_diagnostic_preserves_whole_record(repo_files, tmp_path):
    damaged = "100644 deadbeef\tship/broken.txt"

    with pytest.raises(RuntimeError, match=r"100644 deadbeef.*ship/broken.txt"):
        repo_files.list_repo_owned_entries(
            tmp_path,
            "ship",
            mode=repo_files.TRACKED_ONLY,
            git_runner=lambda _argv: (0, damaged + "\0"),
        )


def test_git_entries_are_nul_split_without_worktree_is_file_recheck(repo_files, tmp_path):
    output = "entries/dangling\0entries/directory-link\0entries/submodule\0"

    got = repo_files.list_repo_owned_files(
        tmp_path,
        "entries",
        mode=repo_files.OWNED,
        git_runner=lambda _argv: (0, output),
    )

    assert [path.as_posix() for path in got] == [
        "entries/dangling",
        "entries/directory-link",
        "entries/submodule",
    ]
    assert not any((tmp_path / path).exists() for path in got)


@pytest.mark.parametrize(
    ("mode", "stdout", "expected"),
    [
        ("owned", "ship/real.txt\0", [Path("ship/real.txt")]),
        (
            "tracked_only",
            "100644 1111111111111111111111111111111111111111 0"
            "\tship/real.txt\0",
            [Path("ship/real.txt")],
        ),
    ],
)
def test_successful_git_enumeration_uses_stdout_paths_and_isolates_stderr(
        repo_files, tmp_path, monkeypatch, mode, stdout, expected):
    calls = []
    monkeypatch.setattr(repo_files.shutil, "which", lambda _name: "/usr/bin/git")
    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=stdout,
            stderr="trace: built-in: git ls-files ship/trace-noise.txt\n",
        )
    monkeypatch.setattr(repo_files.subprocess, "run", fake_run)

    got = repo_files.list_repo_owned_files(
        tmp_path,
        "ship",
        mode=getattr(repo_files, mode.upper()),
    )

    assert got == expected
    assert calls[0][1]["env"]["LC_ALL"] == "C"
    assert calls[0][1]["env"]["LANGUAGE"] == ""


def test_tracked_only_real_git_excludes_untracked_and_ignored(repo_files, tmp_path):
    repo = _repo(tmp_path)
    ship = repo / "ship"
    ship.mkdir()
    (repo / ".gitignore").write_text("*.derived\n", encoding="utf-8")
    (ship / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (ship / "machine.local").write_text("local\n", encoding="utf-8")
    (ship / "cache.derived").write_text("derived\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "ship/tracked.txt")

    got = repo_files.list_repo_owned_files(
        repo, "ship", mode=repo_files.TRACKED_ONLY)

    assert [path.as_posix() for path in got] == ["ship/tracked.txt"]


def test_tracked_only_empty_is_loud_when_disk_subtree_is_nonempty(repo_files, tmp_path):
    repo = _repo(tmp_path)
    ship = repo / "ship"
    ship.mkdir()
    (ship / "untracked.txt").write_text("local\n", encoding="utf-8")

    with pytest.warns(repo_files.RepoFilesEmptyWarning, match="빈 결과.*비어 있지 않음"):
        got = repo_files.list_repo_owned_files(
            repo, "ship", mode=repo_files.TRACKED_ONLY)

    assert got == []


def test_owned_real_git_includes_nonignored_untracked_only(repo_files, tmp_path):
    repo = _repo(tmp_path)
    scan = repo / "scan"
    scan.mkdir()
    (repo / ".gitignore").write_text("*.derived\n", encoding="utf-8")
    (scan / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (scan / "new.txt").write_text("new\n", encoding="utf-8")
    (scan / "cache.derived").write_text("derived\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "scan/tracked.txt")

    got = repo_files.list_repo_owned_files(repo, "scan", mode=repo_files.OWNED)

    assert [path.as_posix() for path in got] == [
        "scan/new.txt",
        "scan/tracked.txt",
    ]


def test_owned_git_failure_filesystem_fallback_is_loud_and_excludes_derivatives(
        repo_files, tmp_path):
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "kept.py").write_text("kept\n", encoding="utf-8")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "lost.pyc").write_bytes(b"x")

    with pytest.warns(repo_files.RepoFilesFallbackWarning, match="보장을 적용할 수 없음"):
        got = repo_files.list_repo_owned_files(
            tmp_path,
            "src",
            mode=repo_files.OWNED,
            git_runner=lambda _argv: (1, "not a repository"),
        )

    assert [path.as_posix() for path in got] == ["src/pkg/kept.py"]


def test_tracked_only_rc129_is_loud_without_filesystem_shipping(
        repo_files, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "machine.local").write_text("machine\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"tracked_only.*rc=129"):
        repo_files.list_repo_owned_entries(
            tmp_path,
            "src",
            mode=repo_files.TRACKED_ONLY,
            git_runner=lambda _argv: (129, "unknown option"),
        )


def test_tracked_only_localized_non_repository_uses_structural_probe_and_falls_back(
        repo_files, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "copied.txt").write_text("copied\n", encoding="utf-8")

    calls = []

    def localized_runner(argv):
        calls.append(argv)
        if argv == ["rev-parse", "--git-dir"]:
            return 128, "fatal: 깃 저장소가 아닙니다"
        return 128, "fatal: 깃 저장소가 아닙니다"

    with pytest.warns(
        repo_files.RepoFilesFallbackWarning,
        match=r"filesystem 전수 순회.*rc=128",
    ):
        got = repo_files.list_repo_owned_files(
            tmp_path,
            "src",
            mode=repo_files.TRACKED_ONLY,
            git_runner=localized_runner,
        )

    assert got == [Path("src/copied.txt")]
    assert calls[-1] == ["rev-parse", "--git-dir"]


def test_tracked_only_rc128_corrupt_index_remains_loud(repo_files, tmp_path):
    calls = []

    def corrupt_index_runner(argv):
        calls.append(argv)
        if argv == ["rev-parse", "--git-dir"]:
            return 0, ".git\n"
        return 128, "fatal: .git/index: index file smaller than expected"

    with pytest.raises(
        repo_files.RepoFilesGitError,
        match=r"tracked_only.*rc=128.*index file smaller than expected",
    ):
        repo_files.list_repo_owned_files(
            tmp_path,
            "src",
            mode=repo_files.TRACKED_ONLY,
            git_runner=corrupt_index_runner,
        )
    assert calls[-1] == ["rev-parse", "--git-dir"]


def test_tracked_only_missing_git_warns_and_falls_back(
        repo_files, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "copied.txt").write_text("copied\n", encoding="utf-8")
    monkeypatch.setattr(repo_files.shutil, "which", lambda _name: None)

    with pytest.warns(repo_files.RepoFilesFallbackWarning, match=r"rc=127"):
        got = repo_files.list_repo_owned_files(
            tmp_path,
            "src",
            mode=repo_files.TRACKED_ONLY,
        )

    assert got == [Path("src/copied.txt")]


def test_git_exec_file_not_found_race_maps_to_missing_binary_rc(
        repo_files, tmp_path, monkeypatch):
    monkeypatch.setattr(repo_files.shutil, "which", lambda _name: "/vanished/git")

    def vanished_binary(*_args, **_kwargs):
        raise FileNotFoundError("git disappeared after which")

    monkeypatch.setattr(repo_files.subprocess, "run", vanished_binary)

    rc, detail = repo_files._real_git_runner(tmp_path)(["status"])

    assert rc == 127
    assert "git disappeared after which" in detail


@pytest.mark.parametrize(
    ("missing_rc", "timeout", "output_mode", "force_c_locale", "expected"),
    (
        (127, 120, "stdout_or_error", True, "stderr-data"),
        (1, 120, "stdout", False, "stdout-data"),
        (1, 1800, "stdout_stderr", False, "stdout-datastderr-data"),
    ),
)
def test_shared_git_runner_preserves_pre_consolidation_policy_matrix(
        repo_files, tmp_path, missing_rc, timeout, output_mode,
        force_c_locale, expected):
    """T-0507 표의 세 captured runner 의미를 공용 옵션이 손실 없이 표현한다."""
    captured = {}

    class _Result:
        returncode = 7
        stdout = "stdout-data"
        stderr = "stderr-data"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _Result()

    runner = repo_files.real_git_runner(
        tmp_path,
        missing_binary_rc=missing_rc,
        timeout=timeout,
        output_mode=output_mode,
        force_c_locale=force_c_locale,
        which=lambda _name: "/usr/bin/git",
        run=fake_run,
    )

    assert runner(["status"]) == (7, expected)
    assert captured["command"] == ["/usr/bin/git", "-C", str(tmp_path), "status"]
    assert captured["kwargs"]["timeout"] == timeout
    if force_c_locale:
        assert captured["kwargs"]["env"]["LC_ALL"] == "C"
        assert captured["kwargs"]["env"]["LANGUAGE"] == ""
    else:
        assert "env" not in captured["kwargs"]

    missing = repo_files.real_git_runner(
        tmp_path,
        missing_binary_rc=missing_rc,
        timeout=timeout,
        output_mode=output_mode,
        which=lambda _name: None,
    )
    assert missing(["status"])[0] == missing_rc


def test_unknown_mode_is_rejected_before_enumeration(repo_files, tmp_path):
    with pytest.raises(ValueError, match="알 수 없는 repo 파일 열거 mode"):
        repo_files.list_repo_owned_files(tmp_path, ".", mode="consumer_a")


def test_pm_update_directory_shipping_uses_tracked_only(repo_files, tmp_path):
    pm_update = _load("pm_update_repo_files_test", "pm_update.py")
    repo = _repo(tmp_path)
    engine = repo / "engine"
    engine.mkdir()
    (engine / "tracked.py").write_text("tracked\n", encoding="utf-8")
    (engine / "machine.local").write_text("local\n", encoding="utf-8")
    _git(repo, "add", "engine/tracked.py")

    got = [rel for rel, _path in pm_update._iter_files(repo, "engine")]

    assert got == ["engine/tracked.py"]


def test_pm_update_nested_checkout_keeps_git_ignore_guarantee(tmp_path):
    pm_update = _load("pm_update_nested_checkout_test", "pm_update.py")
    outer = _repo(tmp_path)
    framework = outer / "vendor" / "framework"
    engine = framework / "engine"
    engine.mkdir(parents=True)
    (outer / ".gitignore").write_text(
        "vendor/framework/engine/machine.local\n",
        encoding="utf-8",
    )
    (engine / "tracked.py").write_text("tracked\n", encoding="utf-8")
    (engine / "machine.local").write_text("machine\n", encoding="utf-8")
    _git(outer, "add", ".gitignore", "vendor/framework/engine/tracked.py")

    got = [rel for rel, _path in pm_update._iter_files(framework, "engine")]

    assert got == ["engine/tracked.py"]


def test_pm_update_non_git_directory_rc128_warns_and_falls_back(tmp_path, monkeypatch):
    pm_update = _load("pm_update_nongit_fallback_test", "pm_update.py")
    repo_files = pm_update._load_repo_owned_files()
    source = tmp_path / "source"
    ship = source / "ship"
    ship.mkdir(parents=True)
    (ship / "fallback.txt").write_text("fallback\n", encoding="utf-8")
    # "이 트리는 git checkout 이 아니다"가 이 테스트의 입력이다 — 픽스처 위치가 그 답을
    # 정하지 않도록 엔진의 runner 주입 seam 으로 비-repo(rc 128)를 명시한다.
    monkeypatch.setattr(repo_files, "_real_git_runner", lambda _cwd: lambda _argv: (128, ""))

    with pytest.warns(
        repo_files.RepoFilesFallbackWarning,
        match=r"tracked_only.*rc=128",
    ):
        got = list(pm_update._iter_files(source, "ship"))

    assert [rel for rel, _path in got] == ["ship/fallback.txt"]


def test_pm_update_skips_deleted_tracked_entry_loudly(tmp_path):
    pm_update = _load("pm_update_deleted_tracked_test", "pm_update.py")
    repo = _repo(tmp_path)
    ship = repo / "ship"
    ship.mkdir()
    deleted = ship / "deleted.txt"
    deleted.write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "ship/deleted.txt")
    deleted.unlink()

    with pytest.warns(
        pm_update.SkippedRepoShippingEntryWarning,
        match="working tree에서 삭제됨.*ship/deleted.txt",
    ):
        got = list(pm_update._iter_files(repo, "ship"))

    assert got == []


@pytest.mark.parametrize(
    ("index_mode", "warning_fragment"),
    [
        ("120000", "symlink"),
        ("160000", "gitlink"),
    ],
)
def test_pm_update_skips_nonregular_index_mode_without_symlink_permission(
        tmp_path, index_mode, warning_fragment):
    pm_update = _load(f"pm_update_index_mode_{index_mode}_test", "pm_update.py")
    repo = _repo(tmp_path)
    ship = repo / "ship"
    ship.mkdir()
    materialized = ship / f"mode-{index_mode}"
    materialized.write_text("materialized as a regular file\n", encoding="utf-8")
    blob_oid = _git(repo, "hash-object", "-w", materialized.relative_to(repo).as_posix())
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"{index_mode},{blob_oid.stdout.strip()},{materialized.relative_to(repo).as_posix()}",
    )
    assert materialized.is_file()
    assert not materialized.is_symlink()

    with pytest.warns(
        pm_update.SkippedRepoShippingEntryWarning,
        match=warning_fragment,
    ):
        got = list(pm_update._iter_files(repo, "ship"))

    assert got == []


@pytest.mark.parametrize("index_mode", ["120000", "160000"])
def test_tracked_only_seam_preserves_special_index_modes(
        repo_files, tmp_path, index_mode):
    record = (
        f"{index_mode} 1111111111111111111111111111111111111111 0"
        f"\tship/mode-{index_mode}\0"
    )

    got = repo_files.list_repo_owned_entries(
        tmp_path,
        "ship",
        mode=repo_files.TRACKED_ONLY,
        git_runner=lambda _argv: (0, record),
    )

    assert got == [
        repo_files.RepoOwnedEntry(Path(f"ship/mode-{index_mode}"), index_mode)
    ]


def test_test_inventory_adapter_preserves_git_entries_without_worktree_recheck(
        tmp_path, monkeypatch):
    """테스트 공용 어댑터도 삭제 tracked·symlink·gitlink를 조용히 드롭하지 않는다."""
    entries = [
        test_inventory.REPO_FILES.RepoOwnedEntry(Path("ship/deleted.txt"), "100644"),
        test_inventory.REPO_FILES.RepoOwnedEntry(Path("ship/link"), "120000"),
        test_inventory.REPO_FILES.RepoOwnedEntry(Path("ship/submodule"), "160000"),
    ]
    monkeypatch.setattr(
        test_inventory.REPO_FILES,
        "list_repo_owned_entries",
        lambda *_args, **_kwargs: entries,
    )

    got = test_inventory.repo_owned_paths(
        tmp_path, "ship", mode=test_inventory.TRACKED_ONLY
    )

    assert got == [tmp_path.resolve() / entry.path for entry in entries]
    assert not any(path.exists() for path in got)


def test_unmerged_index_path_stops_shipping_loudly(repo_files, tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "config", "user.name", "Repo Files Test")
    _git(repo, "config", "user.email", "repo-files@example.invalid")
    ship = repo / "ship"
    ship.mkdir()
    conflicted = ship / "f.txt"
    conflicted.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "ship/f.txt")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "other")
    conflicted.write_text("other\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "other")
    _git(repo, "checkout", "-q", "-")
    conflicted.write_text("main\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "main")
    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "other"],
        capture_output=True,
        text=True,
    )
    assert merge.returncode != 0

    with pytest.raises(RuntimeError, match=r"unmerged.*ship/f\.txt"):
        repo_files.list_repo_owned_entries(
            repo,
            "ship",
            mode=repo_files.TRACKED_ONLY,
        )


@requires_symlink
def test_pm_update_skips_tracked_directory_symlink_loudly(tmp_path):
    pm_update = _load("pm_update_directory_symlink_test", "pm_update.py")
    repo = _repo(tmp_path)
    ship = repo / "ship"
    outside = tmp_path / "outside"
    ship.mkdir()
    outside.mkdir()
    (outside / "payload.txt").write_text("outside\n", encoding="utf-8")
    (ship / "directory-link").symlink_to(outside, target_is_directory=True)
    _git(repo, "add", "ship/directory-link")

    with pytest.warns(
        pm_update.SkippedRepoShippingEntryWarning,
        match=r"symlink.*ship/directory-link",
    ):
        got = list(pm_update._iter_files(repo, "ship"))

    assert got == []


@requires_symlink
def test_pm_update_skips_manifest_entry_that_is_itself_symlink(tmp_path):
    pm_update = _load("pm_update_manifest_symlink_test", "pm_update.py")
    repo = _repo(tmp_path)
    target = repo / "target.txt"
    target.write_text("payload\n", encoding="utf-8")
    manifest_entry = repo / "manifest-link.txt"
    manifest_entry.symlink_to(target)

    with pytest.warns(
        pm_update.SkippedRepoShippingEntryWarning,
        match=r"manifest 엔트리 제외.*manifest-link.txt",
    ):
        got = list(pm_update._iter_files(repo, "manifest-link.txt"))

    assert got == []


def test_pm_update_reports_untracked_exclusion_count(tmp_path, capsys):
    pm_update = _load("pm_update_untracked_signal_test", "pm_update.py")
    repo = _repo(tmp_path)
    ship = repo / "ship"
    ship.mkdir()
    (ship / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (ship / "untracked.txt").write_text("local\n", encoding="utf-8")
    _git(repo, "add", "ship/tracked.txt")

    got = list(pm_update._iter_files(repo, "ship"))

    assert [rel for rel, _path in got] == ["ship/tracked.txt"]
    assert (
        "pm-update: untracked 1건 제외 — git add 후 전파됨"
        in capsys.readouterr().err
    )


def test_pm_update_untracked_single_file_manifest_entry_is_not_shipped(
        tmp_path, capsys):
    pm_update = _load("pm_update_untracked_single_file_test", "pm_update.py")
    repo = _repo(tmp_path)
    ship = repo / "ship"
    ship.mkdir()
    (ship / "single.txt").write_text("machine\n", encoding="utf-8")

    with pytest.raises(
        pm_update.EmptyShippingInventoryError,
        match=r"pm-update 출하 인벤토리가 0건임.*subtree='ship/single\.txt'",
    ):
        list(pm_update._iter_files(repo, "ship/single.txt"))

    assert (
        "pm-update: untracked 1건 제외 — git add 후 전파됨"
        in capsys.readouterr().err
    )


def test_pm_update_ignored_single_file_manifest_entry_is_not_shipped_loudly(
        tmp_path, capsys):
    pm_update = _load("pm_update_ignored_single_file_test", "pm_update.py")
    repo = _repo(tmp_path)
    ship = repo / "ship"
    ship.mkdir()
    (repo / ".gitignore").write_text("ship/machine.local\n", encoding="utf-8")
    (ship / "machine.local").write_text("machine\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")

    with pytest.raises(
        pm_update.EmptyShippingInventoryError,
        match=r"pm-update 출하 인벤토리가 0건임.*subtree='ship/machine\.local'",
    ):
        list(pm_update._iter_files(repo, "ship/machine.local"))
    assert (
        "manifest 선언 단일 파일이 source에서 gitignore되어 출하되지 않음: "
        "ship/machine.local"
        in capsys.readouterr().err
    )


def test_shippable_entries_reject_bare_path_instead_of_lstat_only_fallback(tmp_path):
    pm_update = _load("pm_update_typed_entries_test", "pm_update.py")
    relative = Path("ship/file.txt")
    source = tmp_path / relative
    source.parent.mkdir()
    source.write_text("payload\n", encoding="utf-8")

    with pytest.raises(AttributeError, match=r"has no attribute 'path'"):
        pm_update._shippable_tracked_entries(tmp_path, [relative])


@requires_symlink
def test_pm_update_untracked_count_excludes_unshippable_symlink(tmp_path, capsys):
    pm_update = _load("pm_update_untracked_symlink_count_test", "pm_update.py")
    repo = _repo(tmp_path)
    ship = repo / "ship"
    outside = tmp_path / "outside.txt"
    ship.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    (ship / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (ship / "regular.txt").write_text("regular\n", encoding="utf-8")
    (ship / "untracked-link").symlink_to(outside)
    _git(repo, "add", "ship/tracked.txt")

    got = list(pm_update._iter_files(repo, "ship"))

    assert [rel for rel, _path in got] == ["ship/tracked.txt"]
    assert (
        "pm-update: untracked 1건 제외 — git add 후 전파됨"
        in capsys.readouterr().err
    )


def test_pm_import_dest_fallback_uses_owned_mode(repo_files, tmp_path):
    pm_import = _load("pm_import_repo_files_test", "pm_import.py")
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("*.derived\n", encoding="utf-8")
    (repo / "tracked.md").write_text("tracked\n", encoding="utf-8")
    (repo / "new.md").write_text("new\n", encoding="utf-8")
    (repo / "cache.derived").write_text("derived\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "tracked.md")

    got = pm_import._resolve_fill_scope(repo, None)

    assert got == {Path(".gitignore"), Path("tracked.md"), Path("new.md")}


def test_pm_import_non_git_dest_fallback_warning_is_visible(tmp_path, monkeypatch):
    pm_import = _load("pm_import_repo_files_nongit_test", "pm_import.py")
    repo_files = pm_import._load_repo_owned_files()
    dest = tmp_path / "adopter"
    dest.mkdir()
    (dest / "copied.md").write_text("copied\n", encoding="utf-8")
    # "이 트리는 git checkout 이 아니다"가 이 테스트의 입력이다 — 픽스처 위치가 그 답을
    # 정하지 않도록 엔진의 runner 주입 seam 으로 비-repo(rc 128)를 명시한다.
    monkeypatch.setattr(repo_files, "_real_git_runner", lambda _cwd: lambda _argv: (128, ""))

    with pytest.warns(
        repo_files.RepoFilesFallbackWarning,
        match="filesystem 전수 순회에 강등",
    ):
        assert pm_import._resolve_fill_scope(dest, None) == {Path("copied.md")}


@requires_symlink
def test_pm_import_dest_walk_excludes_directory_symlink(repo_files, tmp_path):
    pm_import = _load("pm_import_repo_files_symlink_test", "pm_import.py")
    repo = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.md").write_text("outside\n", encoding="utf-8")
    (repo / "kept.md").write_text("kept\n", encoding="utf-8")
    (repo / "directory-link").symlink_to(outside, target_is_directory=True)
    _git(repo, "add", "kept.md", "directory-link")

    assert pm_import._resolve_fill_scope(repo, None) == {Path("kept.md")}


def test_repo_owned_loader_cache_preserves_warning_class_identity():
    pm_update = _load("pm_update_repo_files_identity_test", "pm_update.py")
    pm_import = _load("pm_import_repo_files_identity_test", "pm_import.py")
    domain = _load("domain_repo_files_identity_test", "domain.py")

    update_first = pm_update._load_repo_owned_files()
    update_second = pm_update._load_repo_owned_files()
    import_module = pm_import._load_repo_owned_files()
    domain_module = domain._load_repo_owned_files()

    assert update_first is update_second is import_module is domain_module
    assert (
        update_first.RepoFilesFallbackWarning
        is import_module.RepoFilesFallbackWarning
        is domain_module.RepoFilesFallbackWarning
    )


def test_pm_import_normal_copied_scope_never_enters_dest_walk(tmp_path, monkeypatch):
    pm_import = _load("pm_import_repo_files_main_path_test", "pm_import.py")
    copied = {Path(".project_manager/tools/board.py"), Path("AGENTS.md")}
    monkeypatch.setattr(
        pm_import,
        "_load_repo_owned_files",
        lambda: pytest.fail("정상 import copied_relpaths 경로가 dest 전수 열거에 진입함"),
    )

    assert pm_import._resolve_fill_scope(tmp_path, copied) is copied
