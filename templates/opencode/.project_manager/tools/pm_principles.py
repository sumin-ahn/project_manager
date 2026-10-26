#!/usr/bin/env python3
"""판단 원칙 조회·recall·재무장 CLI 진입점."""
from __future__ import annotations

import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import principles
from pm_engine.infra import console


def main(argv: list[str] | None = None) -> int:
    console.configure_console_utf8()
    return principles.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
