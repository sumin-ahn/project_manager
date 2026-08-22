#!/usr/bin/env python3
"""claude 어댑터 ctx 밴드·넛지 공유 코어 (stdlib only).

statusLine 과 PreToolUse/UserPromptSubmit 넛지가 **같은 임계 로직**을 공유하게 한 모듈.
두 진입점(``ctx_statusline.py`` · ``ctx_stop_hook.py``)이 여기 함수를 호출한다.

엔진 계약 (임계 상향):
  - 임계값 = local.conf ``ctx_nudge_pct`` / ``ctx_stop_pct`` (없으면 엔진 기본 30/20).
    훅/statusline 은 board.py 를 import 하지 않고 **local.conf 를 직접 파싱**한다
    (어댑터는 엔진 사본 경로에 묶이지 않게 — ticket §인터페이스 "local.conf 직접 파싱 권장").
  - ``stop`` 분류는 statusline/relay 소비를 위해 유지하지만 훅에서는 최종 비차단 넛지로 소비한다.

컨텍스트 % 모델 (분모 = 해소된 예산 하나·물리 window% 폐기):
  - 분모 예산 = ``resolve_budget(conf, harness)`` = ``ctx_window_tokens_<harness>`` >
    generic ``ctx_window_tokens`` > 200000 (각 층 >0 sanity). statusLine·hook 이 **같은 예산**
    을 분모로 써 표시와 넛지 밴드 판정을 일치시킨다(claude·opencode 오버라이드 키는 독립).
  - statusLine stdin 의 ``context_window`` 에서 used_tokens = current_usage(input+cache 합) >
    total_input_tokens. current_usage null/부재(세션초·/compact 직후)면 0% graceful. native
    ``used_percentage``(물리%)는 **안 읽는다**.
  - 훅 stdin 엔 ``context_window`` 가 **없을 수 있다**(statusline 전용) → 훅은
    ``transcript_path`` JSONL 을 읽어 자체 산출 (마지막 assistant usage 의 입력+캐시
    토큰 = 현재 컨텍스트 점유; omc sessionTotalTokens 선례). 단, 마지막 compact 경계
    뒤의 usage 만 현재 사이클 실측으로 인정한다.

여기서 다루는 % 는 모두 **잔여(remaining)** 가 아니라 **사용(used)** 비율이다.
``stop`` 분류는 "잔여 <= stop_pct" 로 판정하며, 훅은 이 밴드를 최종 비차단 넛지로 소비한다.
"""
from __future__ import annotations

import json
from pathlib import Path

# ── 엔진 기본 임계 (board.py CTX_*_PCT_DEFAULT 와 동일 — 어댑터는 import 안 하고 미러) ──
# 임계 상향(20/10→30/20): auto-compact 전에 checkpoint를 남길 여유를 더 확보한다.
CTX_NUDGE_PCT_DEFAULT = 30  # 잔여 <= 이 % → ticket/checkpoint 넛지.
CTX_STOP_PCT_DEFAULT = 20   # 잔여 <= 이 % → 최종 넛지(키 이름은 호환성 때문에 유지).

# 2단(strong) nudge 임계 마진 (%p·파생값). nudge2 밴드 = stop_pct < 잔여 <=
# min(stop_pct + 이 마진, nudge_pct) — 최종 밴드 직전 강화 유도(1단을 모델이 무시해도 재안내).
# config 노브 신설 없이 stop_pct 에서 파생(config surface 최소). opencode ctx-guard-core.cjs
# NUDGE2_MARGIN_PCT 와 미러(양 하네스 파리티).
CTX_NUDGE2_MARGIN_PCT = 3

# 기본 ctx 예산(분모) — resolve_budget 의 최종 폴백(오버라이드·generic 미설정 시).
# claude 기본 200k. local.conf ``ctx_window_tokens_<harness>``/``ctx_window_tokens`` 로 조정.
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

    codex 인계: nudge/stop 이 비정상(음수·범위 밖·stop>nudge)이면 엔진 기본 폴백.
    """
    nudge = _int_conf(conf, "ctx_nudge_pct", CTX_NUDGE_PCT_DEFAULT)
    stop = _int_conf(conf, "ctx_stop_pct", CTX_STOP_PCT_DEFAULT)
    # sanity: 0 < stop <= nudge < 100. 위반 시 기본으로 폴백 (오타·역전에 robust).
    if not (0 < stop <= nudge < 100):
        nudge, stop = CTX_NUDGE_PCT_DEFAULT, CTX_STOP_PCT_DEFAULT
    return {"nudge_pct": nudge, "stop_pct": stop}


def resolve_budget(conf: dict[str, str], harness: str = "claude") -> int:
    """ctx 예산(분모)을 per-harness precedence 로 해소.

    ``ctx_window_tokens_{harness}`` > generic ``ctx_window_tokens`` > ``CTX_WINDOW_TOKENS_DEFAULT``.
    각 층 >0 sanity — ≤0·비정수면 다음 층 폴백(물리한도/0-특수의미 없음). claude·opencode
    오버라이드 키는 완전 독립(동시 운용 시 하네스별 예산). statusLine·hook 이 이 값을 공유 분모로.
    """
    for key in (f"ctx_window_tokens_{harness}", "ctx_window_tokens"):
        size = _int_conf(conf, key, 0)
        if size > 0:
            return size
    return CTX_WINDOW_TOKENS_DEFAULT


# ── statusLine: context_window → used % (분모 = 해소된 예산) ──────────

def _clamp_pct(value: float) -> int:
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf 가드
        return 0
    return max(0, min(100, round(value)))


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


def _statusline_used_tokens(cw: dict) -> int:
    """statusLine 의 현재 컨텍스트 점유 토큰 — current_usage(input+cache) 단일 소스.

    current_usage null/부재/빈 dict(세션초·/compact 직후)면 0 (0% graceful — 정보 없음 =
    넛지 밴드로 판정하지 않음). total_input_tokens 폴백은 채택 안 함 —
    current_usage 가 null 인 바로 그 순간(post-compact) total_input 은 누적성/버전 의존이라
    과대 표시→넛지 오판정 위험. current_usage 있으면 total_input 은 중복이라 불필요.
    native 물리%(used_percentage)는 폐기(안 읽음).
    """
    return max(0, _current_usage_tokens(cw))


def context_used_pct_from_statusline(stdin: dict, budget: int) -> int:
    """statusLine stdin JSON → 컨텍스트 **사용** % (분모 = 해소된 예산).

    used_tokens(current_usage input+cache 단일 소스) / budget. 물리 window%(native
    used_percentage)는 폐기 — hook 과 같은 예산 분모로 표시와 넛지 판정을 일치시킨다. 신호 없으면 0
    (세션초·/compact 직후 graceful). budget<=0(비정상)도 0.
    """
    if budget <= 0:
        return 0
    cw = stdin.get("context_window")
    if not isinstance(cw, dict):
        return 0
    tokens = _statusline_used_tokens(cw)
    if tokens <= 0:
        return 0
    return _clamp_pct(tokens / float(budget) * 100)


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

    마지막 compact 경계 이후 가장 최근 assistant 메시지의 usage 입력 토큰합을 쓴다 — 그게
    그 시점의 실제 컨텍스트 점유다 (omc 는 누적합도 쓰지만 컨텍스트 점유는 last-request 입력이
    정확). 경계 뒤 usage 가 없거나 어떤 usage 도 못 찾으면 0.

    Claude Code 2.1.222 환경에서 확인한 저장 transcript 경계 형식은 top-level
    ``{"type":"system", "subtype":"compact_boundary", "compactMetadata":{
    "trigger":"manual", "preTokens":655736, "postTokens":11387, ...}}`` 이다.
    ``compactMetadata.postTokens`` 는 compact 결과 크기로 참고 가능하지만 새 assistant 요청의
    ``message.usage`` 실측은 아니다. 따라서 경계 뒤 usage 가 아직 없는 첫 UserPromptSubmit 에서는
    0(측정불가)을 반환해 r4의 ``raw_tokens > 0`` measured 재무장 신호와 marker 보존 계약을 지킨다.
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
        # compact 경계를 usage 보다 먼저 판정한다. 경계 엔트리 자체에 usage-like 필드가 추가돼도
        # 압축 전 assistant usage 로 넘어가지 않도록 이 지점에서 역탐색을 끝낸다.
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            return 0
        message = entry.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        tokens = _usage_input_tokens(usage) if usage is not None else None
        if tokens is not None and tokens > 0:
            return tokens
    return 0


def context_used_pct_from_tokens(tokens: int, window_tokens: int) -> int:
    """raw 점유 토큰 / 윈도 크기 → 사용 % (정수 반올림·0%도 유효 측정일 수 있음)."""
    if window_tokens <= 0:
        return 0
    if tokens <= 0:
        return 0
    return _clamp_pct(tokens / float(window_tokens) * 100)


def context_used_pct_from_transcript(transcript_path, window_tokens: int) -> int:
    """transcript 점유 토큰 / 윈도 크기 → 사용 %."""
    return context_used_pct_from_tokens(
        context_tokens_from_transcript(transcript_path), window_tokens)


# ── 서브에이전트(sidechain) 감지 (메인 세션만 checkpoint 넛지) ──────────────

def transcript_is_sidechain(transcript_path) -> bool:
    """transcript JSONL 이 서브에이전트(sidechain) 세션의 것인가.

    claude 는 서브에이전트(Task) 대화를 ``<parent>/subagents/agent-*.jsonl`` 에 기록하고 그 엔트리를
    ``isSidechain: true`` 로 표시한다 — 메인 세션 transcript ``<session>.jsonl`` 은 전 엔트리
    ``isSidechain: false`` (실측 확인: 서브에이전트 파일은 전 엔트리 true·메인은 전 엔트리 false 로
    깨끗이 분리). 훅은 stdin ``transcript_path`` 가 가리키는 이 파일을 읽어 세션 성격을 판정한다.

    파일 끝(최신)에서부터 첫 ``isSidechain`` boolean 을 찾아 반환한다. 파일 없음·읽기 실패·신호 부재·
    파싱 불가는 모두 **False**(메인 취급) — 면제는 sidechain 이 *확실할 때만*(보수적 fail-safe·감지
    모호 시 메인 세션 넛지 동작 유지). 에이전트 종류는 보지 않는다(isSidechain 단일 기준 — 미래
    에이전트 자동 커버·ticket §결정).
    """
    path = Path(transcript_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return False
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("isSidechain"), bool):
            return entry["isSidechain"]
    return False


# ── 임계 판정 (statusLine·훅 공유) ──────────────────────────────────────────

def remaining_pct(used_pct: int) -> int:
    return max(0, 100 - used_pct)


def nudge2_threshold(thresholds: dict[str, int]) -> int:
    """2단(strong) nudge 임계(%p) — stop_pct + margin 파생, nudge_pct 로 캡.

    nudge2 밴드 = stop_pct < 잔여 <= 이 값. margin(+3)이 nudge 밴드를 넘지 않게 min 으로 캡해
    nudge2 가 nudge 밴드 밖(ok 영역)으로 새지 않는다. opencode nudge2Threshold 와 동형.
    """
    return min(thresholds["stop_pct"] + CTX_NUDGE2_MARGIN_PCT, thresholds["nudge_pct"])


def classify(used_pct: int, thresholds: dict[str, int]) -> str:
    """used % → 'ok' | 'nudge' | 'nudge2' | 'stop' (잔여 기준·2단 nudge).

    잔여 <= stop_pct → 'stop'. stop_pct < 잔여 <= nudge2_threshold → 'nudge2'(strong·최종 밴드 직전).
    nudge2_threshold < 잔여 <= nudge_pct → 'nudge'(soft·1단). 그 외 'ok'.
    """
    remaining = remaining_pct(used_pct)
    if remaining <= thresholds["stop_pct"]:
        return "stop"
    if remaining <= nudge2_threshold(thresholds):
        return "nudge2"
    if remaining <= thresholds["nudge_pct"]:
        return "nudge"
    return "ok"


# thresholds 는 세 guidance builder의 시그니처 대칭·미러 파리티를 위해 1·2단에서도 유지한다.
def build_nudge_guidance(used_pct: int, thresholds: dict[str, int]) -> str:
    """1단 넛지 — ticket 경계의 complete/checkpoint 기록 시점을 안내."""
    remaining = remaining_pct(used_pct)
    return (
        f"[ctx-nudge] 컨텍스트 사용 {used_pct}% (잔여 {remaining}%). checkpoint 준비 구간이며 "
        "직전 박제 경계 이후 구간이 미기록 상태다. 현재 ticket 경계의 결과는 complete entry 로 "
        "남기는 것이 권고된다. 다음 단계 경계에서 "
        "`python3 .project_manager/tools/pm_log.py checkpoint --task <이름>`(Windows는 `py -3`) "
        "기록이 이 프로젝트의 규약이다. `<이름>`에는 현재 task 이름을 사용한다."
    )


def build_nudge2_guidance(used_pct: int, thresholds: dict[str, int]) -> str:
    """2단 넛지 — 강화 checkpoint 기록 시점을 안내."""
    remaining = remaining_pct(used_pct)
    return (
        f"[ctx-nudge/강화] 컨텍스트 사용 {used_pct}% (잔여 {remaining}%). 강화 checkpoint 구간이며 "
        "직전 박제 경계 이후 구간이 미기록 상태다. 다음 단계 경계에서 "
        "`python3 .project_manager/tools/pm_log.py checkpoint --task <이름>`(Windows는 `py -3`)으로 "
        "checkpoint entry 의 구간·서사를 기록하는 것이 이 프로젝트의 규약이다. "
        "`<이름>`에는 현재 task 이름을 사용한다."
    )


def build_final_guidance(used_pct: int, thresholds: dict[str, int]) -> str:
    """최종 넛지 — 구 stop 밴드에서 checkpoint와 auto-compact 임박을 비차단 안내."""
    remaining = remaining_pct(used_pct)
    return (
        f"[ctx-nudge/최종] 컨텍스트 사용 {used_pct}% (잔여 {remaining}% ≤ "
        f"{thresholds['stop_pct']}%). 최종 checkpoint 구간이며 직전 박제 경계 이후 구간이 "
        "미기록 상태다. 다음 단계 경계에서 "
        "`python3 .project_manager/tools/pm_log.py checkpoint --task <이름> --trigger compaction`"
        "(Windows는 `py -3`) 기록이 이 프로젝트의 규약이다. "
        "`<이름>`에는 현재 task 이름을 사용한다. auto-compact 가 임박한 상태이며, "
        "checkpoint는 압축 후 서사 복구 경계다."
    )
