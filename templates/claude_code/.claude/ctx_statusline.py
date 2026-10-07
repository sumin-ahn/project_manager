#!/usr/bin/env python3
"""claude statusLine — ctx% 표시 + 임계 넛지 (stdlib only).

claude Code 가 statusLine 입력 JSON 을 stdin 으로 준다 (``context_window`` 포함).
이 스크립트는 컨텍스트 **사용** % 를 산출해 statusline 한 줄을 stdout 에 낸다.
임계(local.conf ``ctx.checkpoint_at_pct``/``ctx.precompact_at_pct`` · 사용 한도 대비 쓴 %)에 닿으면 색·문구로
checkpoint 시점을 알린다.

  - ok    : 회색 ctx N%
  - nudge : 노랑 "ctx N% — checkpoint 준비"
  - nudge2: 빨강 "ctx N% — checkpoint 권고" (2단 strong)
  - stop  : 빨강 "ctx N% — checkpoint 최종 알림"

statusLine 은 **흐름을 안 끊는다**(가시화만). ctx_stop_hook.py도 같은 밴드에서 모델-facing
checkpoint 안내만 비차단 주입한다.
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


def build_statusline(stdin: dict, conf: dict, *, root: Path) -> str:
    # 분모 = 적용 한도 E(압축 한도 기록) — hook 과 같은 기록으로 표시=밴드 일관.
    model = stdin.get("model")
    try:
        budget = ctx_guard.read_budget(root, conf, model.get("id") if isinstance(model, dict) else None)
    except ValueError as exc:  # 기록 부재·손상·입력 불일치
        return f"{_RED}ctx 한도 기록 오류 — {exc}{_RESET}"
    used = ctx_guard.context_used_pct_from_statusline(stdin, budget["E"])
    line = render_line(used, ctx_guard.classify(used, budget))
    return f"{line} {_YELLOW}{budget['warning']}{_RESET}" if budget["warning"] else line


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
    sys.stdout.write(build_statusline(stdin, conf, root=root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
