#!/usr/bin/env python3
"""claude statusLine — ctx% 표시 + 임계 넛지 (T-0015 · stdlib only).

claude Code 가 statusLine 입력 JSON 을 stdin 으로 준다 (``context_window`` 포함).
이 스크립트는 컨텍스트 **사용** % 를 산출해 statusline 한 줄을 stdout 에 낸다.
임계(local.conf ``ctx_nudge_pct``/``ctx_stop_pct``)에 닿으면 색·문구로 경고한다.

  - ok    : 회색 ctx N%
  - nudge : 노랑 "ctx N% — 곧 정지(핸드오프 준비)"
  - stop  : 빨강 "ctx N% — 정지 임계·핸드오프"

statusLine 은 **흐름을 안 끊는다**(가시화만) — 하드 정지는 PreToolUse 훅(ctx_stop_hook.py).
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

_COLOR = {"ok": _GREY, "nudge": _YELLOW, "stop": _RED}


def render_line(used_pct: int, state: str) -> str:
    """used % + 분류 → ANSI statusline 문자열 (색·문구)."""
    color = _COLOR.get(state, _GREY)
    if state == "stop":
        body = f"ctx {used_pct}% — 정지 임계·핸드오프"
    elif state == "nudge":
        body = f"ctx {used_pct}% — 곧 정지(핸드오프 준비)"
    else:
        body = f"ctx {used_pct}%"
    return f"{color}{body}{_RESET}"


def build_statusline(stdin: dict, conf: dict) -> str:
    used = ctx_guard.context_used_pct_from_statusline(stdin)
    thresholds = ctx_guard.ctx_thresholds(conf)
    state = ctx_guard.classify(used, thresholds)
    return render_line(used, state)


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.read()
    try:
        stdin = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        stdin = {}
    if not isinstance(stdin, dict):
        stdin = {}
    root = ctx_guard.repo_root(Path(__file__).resolve().parent)
    conf = ctx_guard.load_local_config(root)
    sys.stdout.write(build_statusline(stdin, conf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
