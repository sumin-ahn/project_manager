"""ctx 임계 디폴트 3-사이트 미러 정합 가드 (T-0207).

nudge/stop 디폴트 상수는 세 곳에 손으로 미러링돼 있다 — 엔진 board.py(fresh init 이
local.conf 에 쓰는 값) + 두 어댑터 훅(claude ctx_guard.py·opencode ctx-guard.js). 어댑터는
board.py 를 import 하지 않고(touches 격리·의존 최소) 리터럴을 보유하므로, 한 곳만 바꾸고
미러를 잊으면 board 가 기록한 값과 훅 판정 임계가 어긋난다.

이 가드는 세 파일을 정규식으로 파싱(언어 무관·hermetic·라이브/import 없음)해 세 사이트의
디폴트가 서로 **일치**함을 강제한다. 구체 값(현재 30/20)은 각 사이트별 단위테스트가 핀
(test_handoff_trigger·test_claude_ctx_guard·test_opencode_ctx_guard) — 여기서는 셋의 합의만
검사해 값 변경 시 이 가드를 매번 손대지 않아도 되게 한다.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / ".project_manager" / "tools" / "board.py"
CLAUDE_GUARD = REPO / "templates" / "claude_code" / ".claude" / "ctx_guard.py"
OPENCODE_GUARD = REPO / "templates" / "opencode" / ".opencode" / "plugins" / "ctx-guard.js"


def _grab(path: Path, pattern: str) -> int:
    match = re.search(pattern, path.read_text(encoding="utf-8"))
    assert match, f"{path.name}: 디폴트 상수 못 찾음 (패턴 {pattern!r})"
    return int(match.group(1))


def test_ctx_nudge_default_mirrors_across_three_sites():
    board = _grab(BOARD, r"CTX_NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    claude = _grab(CLAUDE_GUARD, r"CTX_NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    assert board == claude == opencode, (
        f"nudge 디폴트 미러 불일치: board={board} claude={claude} opencode={opencode}"
    )


def test_ctx_stop_default_mirrors_across_three_sites():
    board = _grab(BOARD, r"CTX_STOP_PCT_DEFAULT\s*=\s*(\d+)")
    claude = _grab(CLAUDE_GUARD, r"CTX_STOP_PCT_DEFAULT\s*=\s*(\d+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+STOP_PCT_DEFAULT\s*=\s*(\d+)")
    assert board == claude == opencode, (
        f"stop 디폴트 미러 불일치: board={board} claude={claude} opencode={opencode}"
    )


def test_ctx_default_sanity_stop_below_nudge():
    """디폴트 자체가 sanity(0 < stop <= nudge < 100)를 만족 — 어댑터 sanity 폴백이
    엔진 기본으로 떨어질 때 무한/역전이 없도록."""
    nudge = _grab(BOARD, r"CTX_NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    stop = _grab(BOARD, r"CTX_STOP_PCT_DEFAULT\s*=\s*(\d+)")
    assert 0 < stop <= nudge < 100, f"디폴트 sanity 위반: nudge={nudge} stop={stop}"
