"""repo-owned 파일 열거 seam과 출하 소비처의 모드 계약 회귀."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

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
        ["ls-files", "-z", "--cached", "--", ":(literal)pkg*[x]"],
        [
            "ls-files", "-z", "--cached", "--others", "--exclude-standard",
            "--", ":(literal)pkg*[x]",
        ],
    ]


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


def test_git_failure_filesystem_fallback_is_loud_and_excludes_derivatives(
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
            mode=repo_files.TRACKED_ONLY,
            git_runner=lambda _argv: (1, "not a repository"),
        )

    assert [path.as_posix() for path in got] == ["src/pkg/kept.py"]


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


def test_pm_import_non_git_dest_fallback_warning_is_visible(tmp_path):
    pm_import = _load("pm_import_repo_files_nongit_test", "pm_import.py")
    repo_files = pm_import._load_repo_owned_files()
    dest = tmp_path / "adopter"
    dest.mkdir()
    (dest / "copied.md").write_text("copied\n", encoding="utf-8")

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
