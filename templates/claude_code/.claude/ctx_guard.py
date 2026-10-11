#!/usr/bin/env python3
"""claude 어댑터 ctx 밴드·넛지 공유 코어 (stdlib only).

statusLine 과 PreToolUse/UserPromptSubmit 넛지가 **같은 임계 로직**을 공유하게 한 모듈.
두 진입점(``ctx_statusline.py`` · ``ctx_stop_hook.py``)이 여기 함수를 호출한다.

엔진 계약:
  - 분모(적용 한도 E)와 임계(``ctx.checkpoint_at_pct`` / ``ctx.precompact_at_pct`` · 적용 한도 대비
    **쓴** %)는 엔진의 압축 한도 기록에서 읽는다(``local_conf.read_ctx_budget``). 기록이 없거나
    손상됐거나 local.conf 와 다르면 ``CtxBudgetError`` 이고, 세션 모델이 기록 모델과 다르면 값은 그대로
    쓰되 경고를 낸다. 훅/statusline 은 board.py 를 import 하지 않고 **local.conf 를 직접 파싱**한다.
  - ``stop`` 분류는 statusline/relay 소비를 위해 유지하지만 훅에서는 최종 비차단 넛지로 소비한다.
  - 압축 비율(``ctx.compact_at_pct``)은 여기서 읽지 않는다 — 엔진이 그 값으로 ``settings.json`` 의
    ``autoCompactWindow`` 를 쓰고(``pm_import.apply_harness_ctx``) 압축은 Claude Code 가 한다.

컨텍스트 % 모델 (분모 = 적용 한도 E 하나·물리 window% 폐기):
  - statusLine stdin 의 ``context_window`` 에서 used_tokens = current_usage(input+cache 합) >
    total_input_tokens. current_usage null/부재(세션초·/compact 직후)면 0% graceful. native
    ``used_percentage``(물리%)는 **안 읽는다**.
  - 훅 stdin 엔 ``context_window`` 가 **없을 수 있다**(statusline 전용) → 훅은
    ``transcript_path`` JSONL 을 읽어 자체 산출 (마지막 assistant usage 의 입력+캐시
    토큰 = 현재 컨텍스트 점유; omc sessionTotalTokens 선례). 단, 마지막 compact 경계
    뒤의 usage 만 현재 사이클 실측으로 인정한다.

여기서 다루는 % 는 모두 **사용(used)** 비율이다. ``stop`` 분류는 "사용 >= precompact_at_pct" 로
판정하며, 훅은 이 밴드를 최종 비차단 넛지로 소비한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 2단(strong) nudge 임계 마진 (%p·파생값). nudge2 밴드 = max(precompact_at − 이 마진,
# checkpoint_at) <= 사용 < precompact_at — 최종 밴드 직전 강화 유도(1단을 모델이 무시해도 재안내).
# config 노브 신설 없이 precompact_at 에서 파생(config surface 최소). opencode
# ctx-guard-core.cjs NUDGE2_MARGIN_PCT 와 미러(양 하네스 파리티).
CTX_NUDGE2_MARGIN_PCT = 3


# ── local.conf 직접 파싱 (board.local_config 와 동일 포맷·KEY=value) ──────────

# 차단 구키 목록은 엔진이 **생성**한다(어댑터가 매핑표를 복제하면 표와 파서가 갈린다):
#   python3 .project_manager/tools/local_conf.py --render-adapter-block python
# 생성 시작 — 차단 구키 (local_conf.render_adapter_block · 손편집 금지)
LEGACY_CONF_KEYS = (
    "additional_reviewer.enabled",
    "additional_reviewer_enabled",
    "additional_reviewer_incomplete_round_limit",
    "additional_reviewer_round_limit",
    "additional_reviewer_wave_budget",
    "ctx.checkpoint_pct",
    "ctx.nudge_pct",
    "ctx.precompact_pct",
    "ctx.stop_pct",
    "ctx.window_tokens",
    "ctx_nudge_pct",
    "ctx_stop_pct",
    "ctx_window_tokens",
    "date",
    "delegate.code-reviewer.rounds_max",
    "delegate_enabled",
    "delegate_idle_timeout",
    "delegate_timeout",
    "diff_cap.large",
    "diff_cap.medium",
    "diff_cap.small",
    "external_review_enabled",
    "external_review_idle_timeout",
    "external_review_incomplete_round_limit",
    "external_review_progress_signal",
    "external_review_round_limit",
    "external_review_timeout",
    "external_review_wave_budget",
    "opencode_pro_model",
    "project_name",
    "project_root",
    "project_tagline",
    "py",
    "regression_min_collected",
    "review_denylist_extra",
    "review_paths",
    "review_rounds_max",
    "reviewer_cmd",
    "reviewer_env_keep_extra",
    "reviewer_home_artifacts_extra",
    "test_cmd",
    "upstream",
    "upstream_rev",
    "upstream_seen_rev",
    "user",
)
LEGACY_CONF_KEY_PREFIX = "ctx_window_tokens_"
# 생성 끝 — 차단 구키


def _assert_no_legacy_conf(conf: dict[str, str], path: Path) -> None:
    """구표기 키가 남아 있으면 **값 해소 전에** 멈춘다 (조용한 기본값 강등 차단).

    어댑터는 엔진을 import 하지 않아 신표기 이름을 말하지 못한다 — 무엇이 걸렸는지만 말하고
    전수 지목은 엔진 도구(`board.py lint`·`pm_update.py` 안내)에 맡긴다. 여기서 강등하면 채택자는
    conf 를 고쳤는데 아무 일도 안 일어나는 상태(임계·예산이 전부 엔진 기본값)를 본다."""
    found = sorted(key for key in conf
                   if key in LEGACY_CONF_KEYS
                   or (key.startswith(LEGACY_CONF_KEY_PREFIX)
                       and len(key) > len(LEGACY_CONF_KEY_PREFIX)))
    if not found:
        return
    print(f"오류: local.conf 에 구표기 키가 남아 있습니다 ({path}) — "
          f"{', '.join(found)}. 값이 조용히 기본값으로 떨어지지 않도록 여기서 멈춥니다. "
          "전수 지목은 `board.py lint` 또는 `pm_update.py` 안내가 냅니다.", file=sys.stderr)
    raise SystemExit(1)


def repo_root(start: Path) -> Path:
    """스크립트 위치(``<root>/.claude/``)에서 프로젝트 루트를 낸다 — 그 부모다.

    어댑터 사본은 항상 ``<root>/.claude/`` 에 설치되므로 루트는 그 자리의 함수다. 조상을
    훑어 ``.project_manager/local.conf``/``.git`` 을 찾으면 훅이 자기 트리가 아니라 위에
    있는 남의 PM 홈에 착지한다(실측: 격리 트리의 훅이 바깥 PM 홈 log 에 기록).
    """
    return start.resolve().parent


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
    _assert_no_legacy_conf(conf, path)
    return conf


def _ctx_leaf(root):
    import importlib.util
    path = Path(root) / ".project_manager/tools/repo_owned_files.py"
    spec = importlib.util.spec_from_file_location("ctx_owned_loader", path)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    def verify(module, filename):
        if getattr(module, "ENGINE_REV", None) != getattr(loader, "ENGINE_REV", None):
            raise RuntimeError(f"ctx 엔진 사본 불일치: {filename}")
    return loader.load_module(path.with_name("local_conf.py"), "local_conf.py", verifier=verify, cache=True)


def read_budget(root, conf: dict[str, str], session_model: str | None) -> dict:
    """claude 압축 한도 기록 — E·경고 비율·경고(``local_conf.read_ctx_budget``). 기록 문제는 ``CtxBudgetError``."""
    return _ctx_leaf(root).read_ctx_budget(root, conf, "claude", session_model)


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


def context_tokens_from_transcript(transcript_path) -> tuple[int, str | None, int]:
    """transcript JSONL 을 읽어 **현재 컨텍스트 점유 토큰**, 그 요청의 모델(``message.model``),
    파일 전체의 compact 경계 수를 낸다.

    마지막 compact 경계 이후 가장 최근 assistant 메시지의 usage 입력 토큰합을 쓴다 — 그게
    그 시점의 실제 컨텍스트 점유다 (omc 는 누적합도 쓰지만 컨텍스트 점유는 last-request 입력이
    정확). 경계 뒤 usage 가 없거나 어떤 usage 도 못 찾으면 (0, None). 훅 입력에는 모델이 없어
    (PreToolUse·UserPromptSubmit) 세션 모델은 이 항목의 것을 쓴다.

    Claude Code 2.1.222 환경에서 확인한 저장 transcript 경계 형식은 top-level
    ``{"type":"system", "subtype":"compact_boundary", "compactMetadata":{
    "trigger":"manual", "preTokens":655736, "postTokens":11387, ...}}`` 이다.
    ``compactMetadata.postTokens`` 는 compact 결과 크기로 참고 가능하지만 새 assistant 요청의
    ``message.usage`` 실측은 아니다. 따라서 경계 뒤 usage 가 아직 없는 첫 UserPromptSubmit 에서는
    0(측정불가)을 반환해 r4의 ``raw_tokens > 0`` measured 재무장 신호와 marker 보존 계약을 지킨다.

    경계 수는 주 대화 기록에서 주 세션 압축만 늘린다(서브에이전트 압축의 경계는 서브에이전트 기록에
    남는다). 훅은 이 수의 증가로 압축을 감지한다. 문자열 ``compact_boundary`` 가 든 줄만 JSON 으로
    해석해 같은 1회 읽기 안에서 센다. 파일 없음·읽기 실패는 ``(0, None, 0)``.
    """
    path = Path(transcript_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return 0, None, 0
    boundaries = sum(1 for line in lines if _is_compact_boundary_line(line))
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
        if _is_compact_boundary_entry(entry):
            return 0, None, boundaries
        message = entry.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        tokens = _usage_input_tokens(usage) if usage is not None else None
        if tokens is not None and tokens > 0:
            model = message.get("model")
            return tokens, model if isinstance(model, str) else None, boundaries
    return 0, None, boundaries


def _is_compact_boundary_entry(entry) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("type") == "system"
        and entry.get("subtype") == "compact_boundary"
    )


def _is_compact_boundary_line(line: str) -> bool:
    """그 줄이 compact 경계 항목인가 — 문자열이 든 줄만 JSON 해석한다(대화 기록 전체 해석 회피)."""
    if '"compact_boundary"' not in line:
        return False
    try:
        return _is_compact_boundary_entry(json.loads(line))
    except (ValueError, TypeError):
        return False


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
        context_tokens_from_transcript(transcript_path)[0], window_tokens)


# ── 임계 판정 (statusLine·훅 공유) ──────────────────────────────────────────

def nudge2_threshold(thresholds: dict[str, int]) -> int:
    """2단(strong) nudge 임계(쓴 %) — precompact_at − margin 파생, checkpoint_at 으로 하한.

    nudge2 밴드 = 이 값 <= 사용 < precompact_at. margin(3)이 nudge 밴드 아래로 새지 않게 max 로
    하한을 둔다. opencode nudge2Threshold 와 동형.
    """
    return max(thresholds["precompact_at_pct"] - CTX_NUDGE2_MARGIN_PCT,
               thresholds["checkpoint_at_pct"])


def classify(used_pct: int, thresholds: dict[str, int]) -> str:
    """used % → 'ok' | 'nudge' | 'nudge2' | 'stop' (쓴 % 기준·2단 nudge).

    사용 >= precompact_at → 'stop'. nudge2_threshold <= 사용 → 'nudge2'(strong·최종 밴드 직전).
    checkpoint_at <= 사용 → 'nudge'(soft·1단). 그 외 'ok'.
    """
    if used_pct >= thresholds["precompact_at_pct"]:
        return "stop"
    if used_pct >= nudge2_threshold(thresholds):
        return "nudge2"
    if used_pct >= thresholds["checkpoint_at_pct"]:
        return "nudge"
    return "ok"
