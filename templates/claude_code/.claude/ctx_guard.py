#!/usr/bin/env python3
"""claude 어댑터 ctx 정지-핸드오프 공유 코어 (T-0015 · stdlib only).

statusLine 넛지와 PreToolUse 하드 정지가 **같은 임계 로직**을 공유하게 한 모듈.
두 진입점(``ctx_statusline.py`` · ``ctx_stop_hook.py``)이 여기 함수를 호출한다.

엔진 계약 (T-0013):
  - 임계값 = local.conf ``ctx_nudge_pct`` / ``ctx_stop_pct`` (없으면 엔진 기본 20/10).
    훅/statusline 은 board.py 를 import 하지 않고 **local.conf 를 직접 파싱**한다
    (어댑터는 엔진 사본 경로에 묶이지 않게 — ticket §인터페이스 "local.conf 직접 파싱 권장").
  - 정지 시 handoff = ``python3 .project_manager/tools/pm_handoff.py --trigger
    --reason ctx-stop --ctx-pct <N>`` shell-out (rc0=박제). 실제 정지는 훅이 deny 로.

컨텍스트 % 모델 (omc HUD getContextPercent 선례 — 복제 아닌 자체 구현):
  - statusLine stdin 은 ``context_window`` 필드를 준다. 우선순위:
      1) native ``used_percentage`` (양수면 그대로)
      2) manual: current_usage 토큰합 / context_window_size
      3) total_input: total_input_tokens / context_window_size
  - 훅 stdin 엔 ``context_window`` 가 **없을 수 있다**(statusline 전용) → 훅은
    ``transcript_path`` JSONL 을 읽어 자체 산출 (마지막 assistant usage 의 입력+캐시
    토큰 = 현재 컨텍스트 점유; omc sessionTotalTokens 선례).

여기서 다루는 % 는 모두 **잔여(remaining)** 가 아니라 **사용(used)** 비율이다.
임계는 "잔여 <= stop_pct" 로 판정하므로 used % >= (100 - stop_pct) 가 정지 조건.
"""
from __future__ import annotations

import json
from pathlib import Path

# ── 엔진 기본 임계 (board.py CTX_*_PCT_DEFAULT 와 동일 — 어댑터는 import 안 하고 미러) ──
CTX_NUDGE_PCT_DEFAULT = 20  # 잔여 <= 이 % → "곧 정지" 넛지 (아직 일은 계속).
CTX_STOP_PCT_DEFAULT = 10   # 잔여 <= 이 % → 정지·핸드오프 트리거.

# 기본 컨텍스트 윈도 크기 (statusLine 이 size 를 안 주거나 훅 transcript 경로일 때).
# claude 기본 200k. local.conf ``ctx_window_tokens`` 로 조정 가능.
CTX_WINDOW_TOKENS_DEFAULT = 200_000


# ── local.conf 직접 파싱 (board.local_config 와 동일 포맷·KEY=value) ──────────

def repo_root(start: Path) -> Path:
    """스크립트 위치(.claude/)에서 프로젝트 루트를 찾는다.

    ``.project_manager/local.conf`` 가 있는 가장 가까운 조상을 루트로 본다.
    없으면 .git 디렉토리, 그것도 없으면 start 의 부모(.claude/ → 루트).
    """
    start = start.resolve()
    for cand in (start, *start.parents):
        if (cand / ".project_manager" / "local.conf").exists():
            return cand
        if (cand / ".git").exists():
            return cand
    # 폴백: .claude/ 의 부모 = 프로젝트 루트.
    return start.parents[0] if start.parents else start


def load_local_config(root: Path) -> dict[str, str]:
    """``.project_manager/local.conf`` 를 KEY=value dict 로. 없으면 {}.

    board.local_config 와 동일 규칙 — `#` 주석·빈 줄 무시. 어댑터는 엔진을
    import 하지 않으므로 같은 파싱을 작게 재현한다 (ticket §결정: 직접 파싱).
    """
    conf: dict[str, str] = {}
    path = root / ".project_manager" / "local.conf"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return conf
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip()
    return conf


def _int_conf(conf: dict[str, str], key: str, default: int) -> int:
    raw = conf.get(key)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (ValueError, AttributeError):
        return default


def ctx_thresholds(conf: dict[str, str]) -> dict[str, int]:
    """nudge_pct / stop_pct 를 conf 에서 읽는다. sanity 검증 포함.

    codex T-0013 인계: nudge/stop 이 비정상(음수·범위 밖·stop>nudge)이면 엔진 기본 폴백.
    """
    nudge = _int_conf(conf, "ctx_nudge_pct", CTX_NUDGE_PCT_DEFAULT)
    stop = _int_conf(conf, "ctx_stop_pct", CTX_STOP_PCT_DEFAULT)
    # sanity: 0 < stop <= nudge < 100. 위반 시 기본으로 폴백 (오타·역전에 robust).
    if not (0 < stop <= nudge < 100):
        nudge, stop = CTX_NUDGE_PCT_DEFAULT, CTX_STOP_PCT_DEFAULT
    return {"nudge_pct": nudge, "stop_pct": stop}


def ctx_window_tokens(conf: dict[str, str]) -> int:
    size = _int_conf(conf, "ctx_window_tokens", CTX_WINDOW_TOKENS_DEFAULT)
    return size if size > 0 else CTX_WINDOW_TOKENS_DEFAULT


# ── statusLine: context_window → used % (omc getContextPercent 자체 구현) ─────

def _clamp_pct(value: float) -> int:
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf 가드
        return 0
    return max(0, min(100, round(value)))


def _native_used_pct(cw: dict) -> int | None:
    native = cw.get("used_percentage")
    if not isinstance(native, (int, float)) or native != native or native <= 0:
        return None
    return _clamp_pct(float(native))


def _current_usage_tokens(cw: dict) -> int:
    usage = cw.get("current_usage") or {}
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)) and val == val:
            total += int(val)
    return total


def _manual_used_pct(cw: dict) -> int | None:
    size = cw.get("context_window_size")
    if not isinstance(size, (int, float)) or size <= 0:
        return None
    tokens = _current_usage_tokens(cw)
    if tokens <= 0:
        return None
    return _clamp_pct(tokens / float(size) * 100)


def _total_input_used_pct(cw: dict) -> int | None:
    size = cw.get("context_window_size")
    if not isinstance(size, (int, float)) or size <= 0:
        return None
    total_input = cw.get("total_input_tokens")
    if not isinstance(total_input, (int, float)) or total_input <= 0:
        return None
    return _clamp_pct(total_input / float(size) * 100)


def context_used_pct_from_statusline(stdin: dict) -> int:
    """statusLine stdin JSON → 컨텍스트 **사용** %.

    omc getContextPercent 선례의 native → manual → total_input 폴백을 자체 구현.
    아무 신호도 없으면 0 (정보 없음 = 넛지/정지 안 함).
    """
    cw = stdin.get("context_window")
    if not isinstance(cw, dict):
        return 0
    for fn in (_native_used_pct, _manual_used_pct, _total_input_used_pct):
        pct = fn(cw)
        if pct is not None:
            return pct
    return 0


# ── 훅: transcript JSONL → used % (omc sessionTotalTokens 선례 자체 구현) ──────

def _usage_input_tokens(usage: dict) -> int | None:
    """한 메시지 usage 의 컨텍스트 점유 입력 토큰 (입력 + 캐시 생성 + 캐시 읽기).

    컨텍스트 점유 = 그 요청이 모델에 보낸 입력 총량. output 은 다음 턴에야 입력이
    되므로 '현재 점유'엔 입력 계열만 센다 (omc getTotalTokens 와 동일 키).
    """
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)) and val == val:
            total += int(val)
            seen = True
    return total if seen else None


def context_tokens_from_transcript(transcript_path) -> int:
    """transcript JSONL 을 읽어 **현재 컨텍스트 점유 토큰**을 산출.

    가장 최근(파일 끝 쪽) assistant 메시지의 usage 입력 토큰합을 쓴다 — 그게 그
    시점의 실제 컨텍스트 점유다 (omc 는 누적합도 쓰지만 컨텍스트 점유는 last-request
    입력이 정확). 어떤 usage 도 못 찾으면 0.
    """
    path = Path(transcript_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return 0
    # 파일 끝에서부터 첫 usable usage 를 찾는다 (가장 최신 요청 = 현재 점유).
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        tokens = _usage_input_tokens(usage) if usage is not None else None
        if tokens is not None and tokens > 0:
            return tokens
    return 0


def context_used_pct_from_transcript(transcript_path, window_tokens: int) -> int:
    """transcript 점유 토큰 / 윈도 크기 → 사용 %."""
    if window_tokens <= 0:
        return 0
    tokens = context_tokens_from_transcript(transcript_path)
    if tokens <= 0:
        return 0
    return _clamp_pct(tokens / float(window_tokens) * 100)


# ── 임계 판정 (statusLine·훅 공유) ──────────────────────────────────────────

def remaining_pct(used_pct: int) -> int:
    return max(0, 100 - used_pct)


def classify(used_pct: int, thresholds: dict[str, int]) -> str:
    """used % → 'ok' | 'nudge' | 'stop' (잔여 기준).

    잔여 <= stop_pct → 'stop'. 잔여 <= nudge_pct → 'nudge'. 그 외 'ok'.
    """
    remaining = remaining_pct(used_pct)
    if remaining <= thresholds["stop_pct"]:
        return "stop"
    if remaining <= thresholds["nudge_pct"]:
        return "nudge"
    return "ok"


def build_nudge_guidance(used_pct: int, thresholds: dict[str, int]) -> str:
    """nudge 안내문 — 모델-facing 비차단 주입용 (ADR-0037 graceful handoff nudge).

    조건부 권고(지시 아님): *현 단계 마무리 후* 핸드오프를 유도해 wave 중간 끊김(premature
    interrupt)을 피한다. hard-stop(잔여 stop_pct)과 달리 모델이 살아있는 채로 받아 스스로
    `/pm-handoff`(rich·모델-주도) 하게 한다. 멈추지 않는다(안내만·엔진 박제 X).
    """
    remaining = remaining_pct(used_pct)
    return (
        f"[ctx-nudge] 컨텍스트 사용 {used_pct}% (잔여 {remaining}%) — 핸드오프 준비 구간. "
        f"지금 진행 중인 단계(ticket/wave)를 마무리한 뒤, 새 큰 작업을 시작하지 말고 "
        f"`/pm-handoff` 로 핸드오프하라. 잔여 {thresholds['stop_pct']}% 도달 시 자동 정지된다 (ADR-0037)."
    )
