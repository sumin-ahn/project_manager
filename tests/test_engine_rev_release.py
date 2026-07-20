"""T-0397 — engine_rev.ENGINE_REV ↔ CHANGELOG 최신 릴리스 버전 정합 (릴리즈 bump 기계화).

`engine_rev.ENGINE_REV`(사본 skew 대조의 단일 진실·릴리즈마다 bump 하는 유일 지점)가
CHANGELOG 의 최신 *릴리스* 절 버전과 일치하는지 릴리즈 게이트에서 기계 단언한다. rev bump
누락(= skew 검출값이 옛 버전에 고정)을 릴리즈 절차가 조용히 통과하지 못하게 못박는다.

**release 마커**라 평시 회귀(`pytest tests/ -q`)엔 걸리지 않고(PM_ORCH_LIVE_RELEASE 미설정 시
skip), `pytest -m release`(릴리즈 게이트·PM_ORCH_LIVE_RELEASE=1)에서만 실행된다. 라이브 LLM
불요·순수 기계지만 release 티어에 편입해 릴리즈마다 rev↔CHANGELOG 정합을 강제한다
(test_task_cycle_e2e 동형 — 기계 e2e 를 릴리즈 게이트에 편입한 선례).

시퀀스(의도된 red→green): 코드 wave 가 `ENGINE_REV="vX.Y.Z"`(다음 릴리스)로 먼저 bump 되면
CHANGELOG 최신 릴리스 절은 아직 이전 버전이라 이 테스트가 릴리즈 게이트에서 **red** 다(정상)
— PM 이 릴리즈 부기에서 CHANGELOG `## [X.Y.Z]` 절을 쓰면 **green** 이 된다. 즉 "CHANGELOG 를
안 쓰면 릴리즈 못 나감"을 기계로 보장한다.
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE_REV_PY = REPO / ".project_manager" / "tools" / "engine_rev.py"
CHANGELOG = REPO / "CHANGELOG.md"

# `## [1.3.4] - 2026-07-20` 같은 릴리스 절 헤더 — `[Unreleased]`(버전 아님)는 매칭 안 됨.
_RELEASE_HEADER_RE = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]")


def _latest_changelog_version(text: str) -> str | None:
    """CHANGELOG 에서 최신(맨 위) *릴리스* 절 버전(`X.Y.Z`)을 반환 — `[Unreleased]` 는 건너뛴다."""
    for line in text.splitlines():
        m = _RELEASE_HEADER_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


def _load_engine_rev():
    spec = importlib.util.spec_from_file_location("engine_rev", ENGINE_REV_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.release
@pytest.mark.skipif(
    not os.environ.get("PM_ORCH_LIVE_RELEASE"),
    reason="release bump 게이트 — PM_ORCH_LIVE_RELEASE=1 (릴리즈 절차). 평시 회귀엔 skip.",
)
def test_engine_rev_matches_latest_changelog_release():
    """`engine_rev.ENGINE_REV` == CHANGELOG 최신 릴리스 버전 (rev bump 누락 릴리즈 차단·T-0397)."""
    engine_rev = _load_engine_rev()
    rev = engine_rev.ENGINE_REV.lstrip("v")  # "v1.3.5" → "1.3.5" (CHANGELOG 는 접두 v 없음)

    changelog_version = _latest_changelog_version(CHANGELOG.read_text(encoding="utf-8"))
    assert changelog_version is not None, (
        f"CHANGELOG({CHANGELOG})에서 릴리스 절(`## [X.Y.Z]`)을 못 찾았다."
    )
    assert rev == changelog_version, (
        f"engine_rev.ENGINE_REV(={engine_rev.ENGINE_REV!r} → {rev!r})가 CHANGELOG 최신 릴리스 "
        f"버전(={changelog_version!r})과 불일치 — 릴리즈 시 CHANGELOG `## [{rev}]` 절을 쓰거나 "
        f"ENGINE_REV 를 CHANGELOG 최신에 맞춰 bump 하라(둘은 릴리즈마다 함께 움직인다)."
    )
