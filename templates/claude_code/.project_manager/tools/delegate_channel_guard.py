#!/usr/bin/env python3
"""Harness-neutral native delegation-channel guard entry point and Claude hook.

The decision CLI and hook protocol live in ``pm_engine.cli.delegate_channel_guard``.
The argument-based ``decide`` CLI always emits one JSON line and exits successfully,
including usage and infrastructure failures.  With no arguments, the historical
Claude ``PreToolUse`` stdin/stdout hook protocol remains intact.  This file only
configures the console and calls the CLI with the install-anchor Home.
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
