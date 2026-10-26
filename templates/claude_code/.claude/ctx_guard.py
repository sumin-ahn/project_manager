#!/usr/bin/env python3
"""claude 어댑터 ctx native 관측 코어 (stdlib only).

statusLine(``ctx_statusline.py``)과 ctx 훅(``ctx_stop_hook.py``)이 Claude Code 입력에서 **현재 컨텍스트
점유 토큰**을 얻는 자리다. 이 모듈은 엔진을 import 하지 않고 판정도 하지 않는다 — 적용 한도 E 대비
쓴 %·밴드·안내·marker 는 엔진 공개 진입(``pm_engine.cli.hooks``의 ``context_status``·``judge_context``)이
소유한다.

점유 토큰 관측 (분모 = 적용 한도 E 하나·물리 window% 폐기):
  - statusLine stdin 의 ``context_window`` 에서 current_usage(input+cache 합). current_usage
    null/부재(세션초·/compact 직후)면 0(미측정). native ``used_percentage``(물리%)는 **안 읽는다**.
  - 훅 stdin 엔 ``context_window`` 가 **없을 수 있다**(statusline 전용) → 훅은
    ``transcript_path`` JSONL 을 읽어 자체 산출한다(마지막 assistant usage 의 입력+캐시
    토큰 = 현재 컨텍스트 점유; omc sessionTotalTokens 선례). 단, 마지막 compact 경계
    뒤의 usage 만 현재 사이클 실측으로 인정한다.
"""
from __future__ import annotations

import json
from pathlib import Path


def repo_root(start: Path) -> Path:
    """스크립트 위치(``<root>/.claude/``)에서 프로젝트 루트를 낸다 — 그 부모다.

    어댑터 사본은 항상 ``<root>/.claude/`` 에 설치되므로 루트는 그 자리의 함수다. 조상을
    훑어 ``.project_manager/local.conf``/``.git`` 을 찾으면 훅이 자기 트리가 아니라 위에
    있는 남의 PM 홈에 착지한다(실측: 격리 트리의 훅이 바깥 PM 홈 log 에 기록).
    """
    return start.resolve().parent


# ── statusLine: context_window → 현재 점유 토큰 ──────────

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
