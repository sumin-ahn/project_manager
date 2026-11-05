"""Linux 독 아이콘 등록 — 창 이름 고정과 `.desktop` 파일·아이콘 파일 두기.

시험은 사용자 실제 홈에 쓰지 않는다. 등록 함수는 임시 폴더를 받고, 창 실행 시험은 데이터 폴더를
임시 폴더로 바꾼다.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

REPO = Path(__file__).resolve().parents[1]

from manager import REPO_ROOT, desktop_entry

_needs_gi = pytest.mark.skipif(
    sys.platform != "linux" or importlib.util.find_spec("gi") is None,
    reason="Linux 와 PyGObject(gi) 가 있어야 한다")

ICON_BYTES = b"\x89PNG fake icon"
DESKTOP_NAME = "pm-manager.desktop"
ICON_RELATIVE = Path("icons") / "hicolor" / "256x256" / "apps" / "pm-manager.png"

# `.desktop` 은 Linux 파일이라 경로를 Linux 표기로 만든다(Windows 에서 돌아도 같은 글).
ICON_FILE = PurePosixPath("/data", *ICON_RELATIVE.parts)

BUNDLE_ENTRY_TEXT = """[Desktop Entry]
Type=Application
Name=Project Manager
Exec="/opt/My Apps/pm-manager-linux-x86_64"
Icon=/data/icons/hicolor/256x256/apps/pm-manager.png
StartupWMClass=pm-manager
Terminal=false
Categories=Development;
"""
SOURCE_ENTRY_TEXT = """[Desktop Entry]
Type=Application
Name=Project Manager
Exec="/home/u/venv dir/bin/python3" -m manager
Path=/home/u/work/project_manager_5
Icon=/data/icons/hicolor/256x256/apps/pm-manager.png
StartupWMClass=pm-manager
Terminal=false
Categories=Development;
"""


@_needs_gi
def test_the_window_name_is_fixed_before_gtk_starts():
    script = (
        "from manager import deps\n"
        "deps.missing_renderer_message()\n"
        "from gi.repository import GLib, Gdk\n"
        "print(GLib.get_prgname(), Gdk.get_program_class())\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    for key in ("DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(key, None)

    completed = subprocess.run([sys.executable, "-c", script], cwd=str(REPO), env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)

    assert completed.stdout.strip().splitlines()[-1] == "pm-manager Pm-manager", (
        completed.stdout + completed.stderr)


def test_the_entry_text_for_the_bundle_and_the_source_tree():
    bundle = desktop_entry.launch_command(
        executable="/opt/My Apps/pm-manager-linux-x86_64", frozen=True, repo_root=Path("/repo"))
    source = desktop_entry.launch_command(
        executable="/home/u/venv dir/bin/python3", frozen=False,
        repo_root=PurePosixPath("/home/u/work/project_manager_5"))

    assert bundle == (["/opt/My Apps/pm-manager-linux-x86_64"], None)
    assert source == (["/home/u/venv dir/bin/python3", "-m", "manager"],
                      PurePosixPath("/home/u/work/project_manager_5"))
    assert desktop_entry.entry_text(*bundle, "Project Manager", ICON_FILE) == BUNDLE_ENTRY_TEXT
    assert desktop_entry.entry_text(*source, "Project Manager", ICON_FILE) == SOURCE_ENTRY_TEXT


@_needs_gi
def test_gio_reads_back_the_command_from_the_entry(tmp_path):
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    cases = [
        (tmp_path / "My Apps" / "pm-manager-linux-x86_64", True),
        (tmp_path / 'a "q" $HOME `x` back\\slash' / "pm", False),
    ]
    for index, (executable, frozen) in enumerate(cases):
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        workdir = tmp_path / "work dir"
        argv, workdir_value = desktop_entry.launch_command(
            executable=str(executable), frozen=frozen, repo_root=workdir)
        entry = tmp_path / f"entry{index}.desktop"
        icon_file = tmp_path / ICON_RELATIVE
        entry.write_text(desktop_entry.entry_text(argv, workdir_value, "Project Manager", icon_file),
                         encoding="utf-8")

        info = Gio.DesktopAppInfo.new_from_filename(str(entry))

        assert info is not None
        assert GLib.shell_parse_argv(info.get_commandline())[1] == argv
        assert info.get_string("Path") == (None if workdir_value is None else str(workdir_value))
        assert info.get_string("StartupWMClass") == "pm-manager"
        assert info.get_string("Icon") == str(icon_file)
        assert info.get_icon().get_file().get_path() == str(icon_file)


def test_register_writes_once_and_rewrites_only_on_change(tmp_path):
    entry_path = tmp_path / "applications" / DESKTOP_NAME
    icon_path = tmp_path / ICON_RELATIVE
    expected_argv, expected_workdir = desktop_entry.launch_command(
        executable=sys.executable, frozen=False, repo_root=REPO_ROOT)

    written = desktop_entry.register(ICON_BYTES, "Project Manager", tmp_path)

    assert written == [entry_path, icon_path]
    assert entry_path.read_text(encoding="utf-8") == desktop_entry.entry_text(
        expected_argv, expected_workdir, "Project Manager", icon_path)
    assert icon_path.read_bytes() == ICON_BYTES
    before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in written}

    assert desktop_entry.register(ICON_BYTES, "Project Manager", tmp_path) == []
    assert {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in written} == before

    entry_path.write_text("[Desktop Entry]\nExec=/moved/pm-manager\n", encoding="utf-8")
    assert desktop_entry.register(ICON_BYTES, "Project Manager", tmp_path) == [entry_path]
    assert entry_path.read_text(encoding="utf-8") == desktop_entry.entry_text(
        expected_argv, expected_workdir, "Project Manager", icon_path)
    assert [path.name for path in entry_path.parent.iterdir()] == [DESKTOP_NAME]
    assert [path.name for path in icon_path.parent.iterdir()] == ["pm-manager.png"]


def test_the_data_folder_follows_xdg_data_home(monkeypatch, tmp_path):
    default = Path.home() / ".local" / "share"

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert desktop_entry.data_home() == tmp_path
    for unusable in ("", "relative/share"):
        monkeypatch.setenv("XDG_DATA_HOME", unusable)
        assert desktop_entry.data_home() == default
    monkeypatch.delenv("XDG_DATA_HOME")
    assert desktop_entry.data_home() == default


class _FakeWindow:
    """창을 띄우지 않는 대역 — 시작 요청만 센다."""
    started = 0

    def __init__(self, **_):
        pass

    def start(self):
        type(self).started += 1


@pytest.fixture
def window_run(monkeypatch, tmp_path):
    from manager import window

    index = tmp_path / "dist" / "index.html"
    index.parent.mkdir()
    index.write_text("<html></html>", encoding="utf-8")
    data_dir = tmp_path / "data"
    _FakeWindow.started = 0
    monkeypatch.setattr(window, "dist_index", lambda: index)
    monkeypatch.setattr(window, "ManagerWindow", _FakeWindow)
    monkeypatch.setattr(desktop_entry, "data_home", lambda: data_dir)
    return window, data_dir


def test_the_window_run_registers_on_linux_only(window_run, monkeypatch):
    window, data_dir = window_run
    entry_path = data_dir / "applications" / DESKTOP_NAME

    for system in ("Windows", "Darwin"):
        monkeypatch.setattr(window.platform, "system", lambda system=system: system)
        assert window.open_window() == 0
        assert not entry_path.exists()
    monkeypatch.setattr(window.platform, "system", lambda: "Linux")
    assert window.open_window() == 0

    assert _FakeWindow.started == 3
    assert entry_path.is_file()
    assert (data_dir / ICON_RELATIVE).is_file()


def test_a_registration_failure_warns_and_still_opens_the_window(
        window_run, monkeypatch, tmp_path, capsys):
    window, _ = window_run
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setattr(desktop_entry, "data_home", lambda: blocker / "share")
    monkeypatch.setattr(window.platform, "system", lambda: "Linux")

    assert window.open_window() == 0

    captured = capsys.readouterr()
    assert _FakeWindow.started == 1
    assert captured.err == ""
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1, lines
    assert lines[0].startswith(
        "[경고] 독 아이콘용 .desktop 파일을 쓰지 못해 독에는 기본 아이콘이 나온다: ")
    assert "not-a-folder" in lines[0]


def test_a_failed_replace_leaves_no_temporary_file(monkeypatch, tmp_path):
    target = tmp_path / "applications" / DESKTOP_NAME

    def failing_replace(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(desktop_entry.os, "replace", failing_replace)

    with pytest.raises(OSError, match="disk full"):
        desktop_entry.register(ICON_BYTES, "Project Manager", tmp_path)

    assert list(target.parent.iterdir()) == []
