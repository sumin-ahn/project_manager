#!/usr/bin/env python3
"""claude 어댑터 ctx 정지-핸드오프 공유 코어 (T-0015 · stdlib only).

statusLine 넛지와 PreToolUse 하드 정지가 **같은 임계 로직**을 공유하게 한 모듈.
두 진입점(``ctx_statusline.py`` · ``ctx_stop_hook.py``)이 여기 함수를 호출한다.

엔진 계약 (T-0013·T-0207 임계 상향):
  - 임계값 = local.conf ``ctx_nudge_pct`` / ``ctx_stop_pct`` (없으면 엔진 기본 30/20).
    훅/statusline 은 board.py 를 import 하지 않고 **local.conf 를 직접 파싱**한다
    (어댑터는 엔진 사본 경로에 묶이지 않게 — ticket §인터페이스 "local.conf 직접 파싱 권장").
  - 정지 시 handoff = ``python3 .project_manager/tools/pm_handoff.py --trigger
    --reason ctx-stop --ctx-pct <N>`` shell-out (rc0=박제). 실제 정지는 훅이 deny 로.

컨텍스트 % 모델 (ADR-0041 — 분모 = 해소된 예산 하나·물리 window% 폐기):
  - 분모 예산 = ``resolve_budget(conf, harness)`` = ``ctx_window_tokens_<harness>`` >
    generic ``ctx_window_tokens`` > 200000 (각 층 >0 sanity). statusLine·hook 이 **같은 예산**
    을 분모로 써 표시=정지 일관(claude·opencode 오버라이드 키는 독립).
  - statusLine stdin 의 ``context_window`` 에서 used_tokens = current_usage(input+cache 합) >
    total_input_tokens. current_usage null/부재(세션초·/compact 직후)면 0% graceful. native
    ``used_percentage``(물리%)는 ADR-0041 로 **안 읽는다**.
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
# T-0207 상향(20/10→30/20): 잔여 10% 정지는 rich 핸드오프 돌릴 컨텍스트가 아슬(PM 47 실측).
CTX_NUDGE_PCT_DEFAULT = 30  # 잔여 <= 이 % → "곧 정지" 넛지 (아직 일은 계속).
CTX_STOP_PCT_DEFAULT = 20   # 잔여 <= 이 % → 정지·핸드오프 트리거.

# 2단(strong) nudge 임계 마진 (%p·파생값·T-0328·ADR-0037). nudge2 밴드 = stop_pct < 잔여 <=
# min(stop_pct + 이 마진, nudge_pct) — hard-stop 직전 강한 유도(1단 soft 를 모델이 무시해도 재안내).
# config 노브 신설 없이 stop_pct 에서 파생(config surface 최소). opencode ctx-guard-core.cjs
# NUDGE2_MARGIN_PCT 와 미러(양 하네스 파리티).
CTX_NUDGE2_MARGIN_PCT = 3

# 기본 ctx 예산(분모) — resolve_budget 의 최종 폴백(오버라이드·generic 미설정 시).
# claude 기본 200k. local.conf ``ctx_window_tokens_<harness>``/``ctx_window_tokens`` 로 조정(ADR-0041).
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
    """generic-only 예산 헬퍼(back-compat) — per-harness 해소는 ``resolve_budget``.

    generic ``ctx_window_tokens`` 만 읽는다(하네스 오버라이드 무시). 유지하되 statusLine/hook
    호출부는 ``resolve_budget`` 를 쓴다(ADR-0041). 비정상(≤0·비정수) → 기본.
    """
    size = _int_conf(conf, "ctx_window_tokens", CTX_WINDOW_TOKENS_DEFAULT)
    return size if size > 0 else CTX_WINDOW_TOKENS_DEFAULT


def resolve_budget(conf: dict[str, str], harness: str = "claude") -> int:
    """ctx 예산(분모)을 per-harness precedence 로 해소 (ADR-0041 Decision 1).

    ``ctx_window_tokens_{harness}`` > generic ``ctx_window_tokens`` > ``CTX_WINDOW_TOKENS_DEFAULT``.
    각 층 >0 sanity — ≤0·비정수면 다음 층 폴백(물리한도/0-특수의미 없음). claude·opencode
    오버라이드 키는 완전 독립(동시 운용 시 하네스별 예산). statusLine·hook 이 이 값을 공유 분모로.
    """
    for key in (f"ctx_window_tokens_{harness}", "ctx_window_tokens"):
        size = _int_conf(conf, key, 0)
        if size > 0:
            return size
    return CTX_WINDOW_TOKENS_DEFAULT


# ── statusLine: context_window → used % (분모 = 해소된 예산·ADR-0041) ──────────

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
    넛지/정지 안 함). total_input_tokens 폴백은 채택 안 함(codex T-0234 must-fix) —
    current_usage 가 null 인 바로 그 순간(post-compact) total_input 은 누적성/버전 의존이라
    과대 표시→넛지 오판정 위험. current_usage 있으면 total_input 은 중복이라 불필요.
    native 물리%(used_percentage)는 ADR-0041 로 폐기(안 읽음).
    """
    return max(0, _current_usage_tokens(cw))


def context_used_pct_from_statusline(stdin: dict, budget: int) -> int:
    """statusLine stdin JSON → 컨텍스트 **사용** % (분모 = 해소된 예산·ADR-0041).

    used_tokens(current_usage input+cache 단일 소스) / budget. 물리 window%(native
    used_percentage)는 폐기 — hook 과 같은 예산 분모로 표시=정지 일관. 신호 없으면 0
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


def nudge2_threshold(thresholds: dict[str, int]) -> int:
    """2단(strong) nudge 임계(%p) — stop_pct + margin 파생, nudge_pct 로 캡 (T-0328).

    nudge2 밴드 = stop_pct < 잔여 <= 이 값. margin(+3)이 nudge 밴드를 넘지 않게 min 으로 캡해
    nudge2 가 nudge 밴드 밖(ok 영역)으로 새지 않는다. opencode nudge2Threshold 와 동형.
    """
    return min(thresholds["stop_pct"] + CTX_NUDGE2_MARGIN_PCT, thresholds["nudge_pct"])


def classify(used_pct: int, thresholds: dict[str, int]) -> str:
    """used % → 'ok' | 'nudge' | 'nudge2' | 'stop' (잔여 기준·T-0328 2단 nudge).

    잔여 <= stop_pct → 'stop'. stop_pct < 잔여 <= nudge2_threshold → 'nudge2'(strong·stop 직전).
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


def build_nudge2_guidance(used_pct: int, thresholds: dict[str, int]) -> str:
    """2단(strong) nudge 안내문 — stop 직전 능동 유도 (ADR-0037·T-0328). 여전히 비차단 안내.

    1단(soft)이 통했으면 안 오지만, 모델이 1단을 무시했거나 1단 창을 건너뛴 세션에 hard-stop
    직전 강하게 재안내한다 — "새 tool 작업 시작 말고 지금 즉시 /pm-handoff". hard-stop 과 달리
    아직 차단은 아니다(안내만·엔진 박제 X). 1단 문구는 무변경 — 이건 2단 추가.
    """
    remaining = remaining_pct(used_pct)
    return (
        f"[ctx-nudge/최종] 잔여 {remaining}% — hard-stop 직전. 새 tool 작업을 시작하지 말고 "
        f"지금 즉시 `/pm-handoff` 를 실행하라. 잔여 {thresholds['stop_pct']}% 도달 시 강제 정지된다 (ADR-0037)."
    )
