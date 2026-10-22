"""현재 시각 (층: infra)."""

from __future__ import annotations

import datetime


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _now_local() -> str:
    """벽시계(로컬 tz) ISO8601 — git 스냅 `recorded_at` 용 (`+09:00` 정합).

    리스 `started` 는 UTC(`_now_utc`)지만 `recorded_at` 은 **로컬 벽시계**로 둔다 — 사람이
    "여기 두고 간다"고 인지하는 스냅 시각이라 벽시계가 자연스럽고, `+09:00`
    (로컬 offset)로 예시한다. `.astimezone()` 로 tz-aware(offset 포함)라 UTC 와 무손실 상호변환
    가능 — 둘 다 명시 offset ISO8601 이라 모호성 없음(표시 tz 만 다름·비교엔 무관)."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


try:
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except (ImportError, KeyError):
    KST = datetime.timezone(datetime.timedelta(hours=9))
