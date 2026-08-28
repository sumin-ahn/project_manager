"""Test-only commands that invoke the interpreter running this test suite."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys


def python_argv_command(*args: str) -> str:
    """Build a command consumed with ``shlex.split`` and ``shell=False``."""
    return shlex.join((sys.executable, *args))


def python_shell_command(*args: str) -> str:
    """Build a platform-native command string for the test's shell consumer."""
    argv = (sys.executable, *args)
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
