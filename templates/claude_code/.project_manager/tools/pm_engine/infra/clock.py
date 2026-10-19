"""현재 시각 (층: infra).
"""
from __future__ import annotations

import datetime


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
