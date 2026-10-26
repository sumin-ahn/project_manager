#!/usr/bin/env python3
"""claude statusLine — ctx% 표시 + 임계 넛지 (표시 전용).

claude Code 가 statusLine 입력 JSON 을 stdin 으로 준다 (``context_window`` 포함).
이 스크립트는 native current_usage 점유 토큰과 ``model.id`` 를 엔진 ``context_status``(읽기 전용)에
넘기고, 돌아온 쓴 %·밴드를 statusline 한 줄로 낸다. 적용 한도 E·임계·밴드 판정은 엔진 소유다.

  - ok    : 회색 ctx N%
  - nudge : 노랑 "ctx N% — checkpoint 준비"
  - nudge2: 빨강 "ctx N% — checkpoint 권고" (2단 strong)
  - stop  : 빨강 "ctx N% — checkpoint 최종 알림"

statusLine 은 **흐름을 안 끊는다**(가시화만). marker 를 쓰지 않는다 — 모델-facing 안내는
ctx_stop_hook.py 가 같은 밴드에서 비차단으로 주입한다.
배선은 settings.json ``statusLine.command`` 가 이 스크립트를 가리킨다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_guard  # noqa: E402  (같은 디렉토리 공유 코어)

# ANSI 색 (statusline 은 ANSI 를 렌더). reset 으로 닫는다.
_RESET = "\033[0m"
_GREY = "\033[90m"
_YELLOW = "\033[33m"
_RED = "\033[31m"

_COLOR = {"ok": _GREY, "nudge": _YELLOW, "nudge2": _RED, "stop": _RED}


def render_line(used_pct: int, state: str) -> str:
    """used % + 분류 → ANSI statusline 문자열 (색·문구)."""
    color = _COLOR.get(state, _GREY)
    if state == "stop":
        body = f"ctx {used_pct}% — checkpoint 최종 알림"
    elif state == "nudge2":
        body = f"ctx {used_pct}% — checkpoint 권고"
    elif state == "nudge":
        body = f"ctx {used_pct}% — checkpoint 준비"
    else:
        body = f"ctx {used_pct}%"
    return f"{color}{body}{_RESET}"


def build_statusline(stdin: dict, *, home) -> str:
    """statusLine 입력 → ANSI 한 줄. 점유는 native current_usage, 판정은 엔진 ``context_status``."""
    from pm_engine.cli import hooks

    model = stdin.get("model")
    window = stdin.get("context_window")
    tokens = ctx_guard._statusline_used_tokens(window) if isinstance(window, dict) else 0
    status = hooks.context_status(
        home, harness="claude", tokens=tokens,
        model=model.get("id") if isinstance(model, dict) and isinstance(model.get("id"), str) else None,
    )
    if status["status"] == "unavailable":  # 압축 한도 기록 부재·손상·입력 불일치
        return f"{_RED}ctx 한도 기록 오류 — {status['notice']}{_RESET}"
    line = render_line(status["used_pct"], status["level"])
    return f"{line} {_YELLOW}{status['notice']}{_RESET}" if status["notice"] else line


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.read()
    try:
        stdin = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        stdin = {}
    if not isinstance(stdin, dict):
        stdin = {}
    root = ctx_guard.repo_root(Path(__file__).resolve().parent)
    tools = str(root / ".project_manager" / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from pm_engine.cli import hooks

    sys.stdout.write(build_statusline(stdin, home=hooks.adapter_home(root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
