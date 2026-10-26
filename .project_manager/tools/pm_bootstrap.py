#!/usr/bin/env python3
"""PM 세션 부트스트랩 CLI 진입점."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import bootstrap
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    console.configure_console_utf8()
    return bootstrap.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main())
