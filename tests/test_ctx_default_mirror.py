"""ctx 임계 디폴트 미러 정합 가드 (T-0207·T-0550·T-0770).

nudge/stop 디폴트 상수는 여러 곳에 손으로 미러링돼 있다 — 엔진 board.py(fresh init 이
local.conf 에 쓰는 값) + 세 어댑터 훅(claude ctx_guard.py·opencode ctx-guard-core.cjs·codex
pm_orch_codex.py). 어댑터는 board.py 를 import 하지 않고(touches 격리·의존 최소) 리터럴을
보유하므로, 한 곳만 바꾸고 미러를 잊으면 board 가 기록한 값과 훅 판정 임계가 어긋난다.

이 가드는 네 파일을 정규식으로 파싱(언어 무관·hermetic·라이브/import 없음)해 사이트들의
디폴트가 서로 **일치**함을 강제한다. ``stop`` 이름은 호환성을 위해 유지하며 Claude/codex 훅에서는
최종 비차단 넛지 밴드, relay 에서는 회전 임계로 소비한다. 구체 값(현재 30/20)은 각 사이트별 단위테스트가 핀
(test_handoff_trigger·test_claude_ctx_guard·test_opencode_ctx_guard·test_codex_ctx_guard) — 여기서는
사이트들의 합의만 검사해 값 변경 시 이 가드를 매번 손대지 않아도 되게 한다.

codex 사이트는 T-0770 이 세션 안 넛지를 배선하며 4번째 미러로 들어왔다. 그 전까지 codex 는
stop/window 만 미러하고 nudge 축이 없어(relay 회전만 판정) 이 가드 밖에 있었다.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / ".project_manager" / "tools" / "board.py"
CLAUDE_GUARD = REPO / "templates" / "claude_code" / ".claude" / "ctx_guard.py"
# opencode ctx-guard 상수는 진입점 shim(plugins/ctx-guard.js)이 아니라 core 모듈에 산다 — 진입점은
# opencode 로드 규약(export=단일 함수·T-0283)을 위해 팩토리만 export 하는 얇은 shim 이고, 순수 헬퍼·
# 상수·팩토리 본체는 lib/ctx-guard-core.cjs(opencode 미스캔·node require 대상)로 분리됐다.
OPENCODE_GUARD = REPO / "templates" / "opencode" / ".opencode" / "lib" / "ctx-guard-core.cjs"
# codex 는 옆에 ctx_guard 모듈이 없어 relay driver 파일이 곧 어댑터 ctx 사이트다 — 같은 파일의
# 훅 축(ctx_thresholds·classify)이 이 상수들을 소비한다(T-0770).
CODEX_GUARD = REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py"


def _grab(path: Path, pattern: str) -> int:
    match = re.search(pattern, path.read_text(encoding="utf-8"))
    assert match, f"{path.name}: 디폴트 상수 못 찾음 (패턴 {pattern!r})"
    return int(match.group(1).replace("_", ""))  # 파이썬 숫자 리터럴 언더스코어(200_000) 허용.


def test_ctx_nudge_default_mirrors_across_all_sites():
    board = _grab(BOARD, r"CTX_NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    claude = _grab(CLAUDE_GUARD, r"CTX_NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    codex = _grab(CODEX_GUARD, r"CTX_NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    assert board == claude == opencode == codex, (
        f"nudge 디폴트 미러 불일치: board={board} claude={claude} "
        f"opencode={opencode} codex={codex}"
    )


def test_ctx_stop_default_mirrors_across_all_sites():
    board = _grab(BOARD, r"CTX_STOP_PCT_DEFAULT\s*=\s*(\d+)")
    claude = _grab(CLAUDE_GUARD, r"CTX_STOP_PCT_DEFAULT\s*=\s*(\d+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+STOP_PCT_DEFAULT\s*=\s*(\d+)")
    codex = _grab(CODEX_GUARD, r"CTX_STOP_PCT_DEFAULT\s*=\s*(\d+)")
    assert board == claude == opencode == codex, (
        f"stop 디폴트 미러 불일치: board={board} claude={claude} "
        f"opencode={opencode} codex={codex}"
    )


def test_ctx_window_tokens_default_mirrors_across_all_sites():
    """ctx 예산 디폴트(200000)도 미러 (T-0236·ADR-0041).

    ADR-0041 이 분모=예산 통일을 확정하며 opencode(ctx-guard.js)에도
    CTX_WINDOW_TOKENS_DEFAULT 미러가 생겼다(T-0235) — nudge/stop 과 동형으로 사이트들의
    합의를 강제한다(한 곳만 바꾸면 하네스별 기본 예산이 어긋남)."""
    board = _grab(BOARD, r"CTX_WINDOW_TOKENS_DEFAULT\s*=\s*(\d+)")
    claude = _grab(CLAUDE_GUARD, r"CTX_WINDOW_TOKENS_DEFAULT\s*=\s*([\d_]+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+CTX_WINDOW_TOKENS_DEFAULT\s*=\s*(\d+)")
    codex = _grab(CODEX_GUARD, r"CTX_WINDOW_TOKENS_DEFAULT\s*=\s*([\d_]+)")
    assert board == claude == opencode == codex, (
        f"window tokens 디폴트 미러 불일치: board={board} claude={claude} "
        f"opencode={opencode} codex={codex}"
    )


def test_ctx_nudge2_margin_mirrors_across_adapter_sites():
    """2단 넛지 마진(%p)도 세 어댑터가 같은 값을 갖는다 (T-0328·T-0770).

    nudge2 밴드 = stop_pct < 잔여 <= min(stop_pct + 마진, nudge_pct) 이라 마진이 어긋나면 세
    하네스의 밴드 경계가 갈린다. board.py 엔 이 파생 상수가 없어(엔진은 nudge/stop 만 기록)
    어댑터 3사이트만 대조한다."""
    claude = _grab(CLAUDE_GUARD, r"CTX_NUDGE2_MARGIN_PCT\s*=\s*(\d+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+NUDGE2_MARGIN_PCT\s*=\s*(\d+)")
    codex = _grab(CODEX_GUARD, r"CTX_NUDGE2_MARGIN_PCT\s*=\s*(\d+)")
    assert claude == opencode == codex, (
        f"nudge2 마진 미러 불일치: claude={claude} opencode={opencode} codex={codex}"
    )


def test_ctx_default_sanity_stop_below_nudge():
    """디폴트 자체가 sanity(0 < stop <= nudge < 100)를 만족 — 어댑터 sanity 폴백이
    엔진 기본으로 떨어질 때 무한/역전이 없도록."""
    nudge = _grab(BOARD, r"CTX_NUDGE_PCT_DEFAULT\s*=\s*(\d+)")
    stop = _grab(BOARD, r"CTX_STOP_PCT_DEFAULT\s*=\s*(\d+)")
    assert 0 < stop <= nudge < 100, f"디폴트 sanity 위반: nudge={nudge} stop={stop}"
