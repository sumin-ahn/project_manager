"""PROTO T-1013: 백업 폴더 `.pm_import_backups/` 는 폴더 안 `.gitignore`(`*`) 로 git 이 무시한다."""
from __future__ import annotations

import datetime
import hashlib
import shutil
from pathlib import Path

import pytest

from _git_fixture import git, init_git_repo
from pm_engine.app import adapter_accept, entry_doc_migration
from pm_engine.domain import install_rules, manifest_rules
from pm_engine.infra import install_conf, install_files, retired_paths

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git 부재")

_IGNORE_TEXT = "*\n"


def _status_lines(dest: Path) -> list[str]:
    out = git(dest, "status", "--porcelain", "--untracked-files=all").stdout
    return [line for line in out.splitlines() if install_rules.BACKUP_DIR_NAME in line]


def _today_root(dest: Path) -> Path:
    return dest / install_rules.BACKUP_DIR_NAME / datetime.date.today().isoformat()


def _copy_action(dest: Path, tmp_path: Path) -> tuple[Path, bytes]:
    (dest / "CLAUDE.md").write_text("adopter edit\n", encoding="utf-8")
    src = tmp_path / "src_CLAUDE.md"
    src.write_text("template\n", encoding="utf-8")
    backup = _today_root(dest) / "CLAUDE.md"
    install_files.CopyAction(src, dest / "CLAUDE.md", backup, dest_root=dest, existed=True,
                             is_symlink=False, backup_dir_name=install_rules.BACKUP_DIR_NAME).run()
    return backup, b"adopter edit\n"


def _inplace(dest: Path, tmp_path: Path) -> tuple[Path, bytes]:
    (dest / "AGENTS.md").write_text("instance doc\n", encoding="utf-8")
    outcome = install_files._backup_before_inplace_edit(
        dest, [Path("AGENTS.md")], _today_root(dest), set())
    assert outcome.backed_up == ["AGENTS.md"]
    return _today_root(dest) / "AGENTS.md", b"instance doc\n"


def _local_conf(dest: Path, tmp_path: Path) -> tuple[Path, bytes]:
    conf = dest / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("runtime.py=python3\n", encoding="utf-8")
    install_conf.backup_existing_local_conf(dest, _today_root(dest))
    return _today_root(dest) / ".project_manager" / "local.conf", b"runtime.py=python3\n"


def _entry_doc(dest: Path, tmp_path: Path) -> tuple[Path, bytes]:
    (dest / "AGENTS.md").write_text("old entry doc\n", encoding="utf-8")
    entry_doc_migration._entry_doc_backup(dest, "AGENTS.md", entry_doc_migration._entry_doc_backup_root(dest))
    return _today_root(dest) / "AGENTS.md", b"old entry doc\n"


def _retired(dest: Path, tmp_path: Path) -> tuple[Path, bytes]:
    old_rel = ".project_manager/tools/external_review.py"
    new_rel = ".project_manager/tools/additional_reviewer.py"
    source = tmp_path / "source"
    (source / new_rel).parent.mkdir(parents=True)
    (source / new_rel).write_bytes(b"new reviewer\n")
    manifest_path = source / ".project_manager" / "engine.manifest"
    manifest_path.write_text(f"{new_rel}\n# pm-retired-path: {old_rel} -> {new_rel}\n", encoding="utf-8")
    (dest / old_rel).parent.mkdir(parents=True, exist_ok=True)
    (dest / old_rel).write_bytes(b"old reviewer\n")
    (dest / new_rel).write_bytes(b"new reviewer\n")
    moved = retired_paths.retire_manifest_paths(
        dest, source, [manifest_rules.ManifestEntry(new_rel)], [manifest_path], write=True)
    assert len(moved) == 1
    return _today_root(dest) / old_rel, b"old reviewer\n"


def _adapter_accept(dest: Path, tmp_path: Path) -> tuple[Path, bytes]:
    from test_pm_update import _ADAPTER_HOOKS_REL, _EDITED_HOOKS, _make_adapter_config_case
    case_dest, source = _make_adapter_config_case(tmp_path / "case", hooks=_EDITED_HOOKS)
    for item in case_dest.iterdir():
        shutil.move(str(item), str(dest / item.name))
    outcome = adapter_accept.accept_adapter_config(
        dest, source, _ADAPTER_HOOKS_REL,
        expected_sha256=hashlib.sha256(_EDITED_HOOKS.encode("utf-8")).hexdigest())
    assert outcome.status == "accepted", outcome.detail
    return _today_root(dest) / _ADAPTER_HOOKS_REL, _EDITED_HOOKS.encode("utf-8")


_WRITERS = {
    "copy-action": _copy_action,
    "inplace-edit": _inplace,
    "local-conf": _local_conf,
    "entry-doc": _entry_doc,
    "retired-path": _retired,
    "adapter-accept": _adapter_accept,
}


@requires_git
@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_every_backup_writer_leaves_git_status_clean(tmp_path, writer):
    dest = init_git_repo(tmp_path / "adopter", commit="seed")
    backup, original = _WRITERS[writer](dest, tmp_path)
    assert backup.read_bytes() == original
    assert (dest / install_rules.BACKUP_DIR_NAME / ".gitignore").read_text(encoding="utf-8") == _IGNORE_TEXT
    assert _status_lines(dest) == []
    assert not (dest / ".gitignore").exists()


@requires_git
def test_existing_backup_dir_without_ignore_file_is_covered_on_next_backup(tmp_path):
    dest = init_git_repo(tmp_path / "adopter", commit="seed")
    stale = dest / install_rules.BACKUP_DIR_NAME / "2026-08-10" / "AGENTS.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old backup\n", encoding="utf-8")
    assert _status_lines(dest) != []
    _inplace(dest, tmp_path)
    assert _status_lines(dest) == []
    assert stale.read_text(encoding="utf-8") == "old backup\n"


def test_non_git_dest_gets_the_same_ignore_file_and_no_root_gitignore(tmp_path):
    dest = tmp_path / "plain"
    dest.mkdir()
    _local_conf(dest, tmp_path)
    assert (dest / install_rules.BACKUP_DIR_NAME / ".gitignore").read_text(encoding="utf-8") == _IGNORE_TEXT
    assert not (dest / ".gitignore").exists()


def test_existing_ignore_file_is_left_as_is(tmp_path):
    dest = tmp_path / "plain"
    ignore = dest / install_rules.BACKUP_DIR_NAME / ".gitignore"
    ignore.parent.mkdir(parents=True)
    ignore.write_text("# adopter note\n*\n", encoding="utf-8")
    _entry_doc(dest, tmp_path)
    assert ignore.read_text(encoding="utf-8") == "# adopter note\n*\n"
