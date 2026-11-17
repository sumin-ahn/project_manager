#!/usr/bin/env python3
"""Harness-neutral native delegation-channel guard entry point.

The decision CLI and hook protocols live in ``pm_engine.cli.delegate_channel_guard``:
``decide`` for the opencode plugin, ``codex-hook``·``codex-subagent-observe`` for the
Codex dispatcher and, with no arguments, the Claude ``PreToolUse`` hook protocol for
the Claude driver ``pm_orch_claude.py``.  Failures exit non-zero and the adapter that
started this process blocks the call.  This file only configures the console and
calls the CLI with the Home where the tool is installed.
"""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import delegate_channel_guard
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return delegate_channel_guard.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
