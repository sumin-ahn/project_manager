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
    뒤의 usage 만 현재 사이클 실측으로 인정한다. 기록은 끝에서부터 읽어 훅 1회 메모리·시간이
    기록 크기와 무관하다.

훅 입력이 서브에이전트 발화인지(``is_subagent_hook``)도 이 모듈이 읽는다 — ctx 훅과 git-anchor 훅이
같은 규칙을 쓴다.
"""
from __future__ import annotations

import json
import os
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


# 대화 기록은 끝에서부터 이 단위로 읽는다. 훅 1회 메모리는 읽기 단위와 읽은 범위 안 가장 긴 줄에만 비례한다.
_READ_CHUNK_BYTES = 64 * 1024
# usage 를 찾은 뒤 마지막 compact 경계를 더 찾는 끝부분 범위. 압축 뒤 첫 주 스레드 훅 시점의 경계는
# 기록 끝에서 최대 461,405 B 안에 있었다(Claude Code 2.1.271~2.1.302 경계 28건 실측). 그 약 9배다.
_TAIL_WINDOW_BYTES = 4 * 1024 * 1024
_BOUNDARY_TOKEN = b'"compact_boundary"'


def context_tokens_from_transcript(transcript_path) -> tuple[int, str | None, str | None]:
    """transcript JSONL 끝부분을 읽어 **현재 컨텍스트 점유 토큰**, 그 요청의 모델(``message.model``),
    마지막 compact 경계의 ``uuid``(찾는 범위 안에 없으면 None)를 낸다.

    점유는 마지막 compact 경계 이후 가장 최근 assistant 메시지의 usage 입력 토큰합이다 — 그게 그 시점의
    실제 컨텍스트 점유다. 경계 뒤 usage 가 없거나 어떤 usage 도 못 찾으면 (0, None). 훅 입력에는 모델이
    없어(PreToolUse·UserPromptSubmit) 세션 모델은 이 항목의 것을 쓴다.

    기록은 끝에서부터 ``_READ_CHUNK_BYTES`` 단위로 읽는다. 끝에서 처음 만나는 usage 줄 또는 경계에서
    점유가 정해지고, usage 를 찾은 뒤에는 문자열 ``"compact_boundary"`` 가 든 줄만 해석하며 끝에서
    ``_TAIL_WINDOW_BYTES`` 를 넘게 읽으면 멈춘다. 그 앞 기록은 읽지 않는다.

    저장 transcript 경계 형식은 top-level ``{"type":"system", "subtype":"compact_boundary",
    "uuid":..., "compactMetadata":{"trigger":..., "preTokens":..., "postTokens":...}}`` 이다
    (2.1.271~2.1.302 28건 모두 ``uuid`` 보유·파일 안 유일). ``compactMetadata.postTokens`` 는 새
    assistant 요청의 ``message.usage`` 실측이 아니므로, 경계 뒤 usage 가 아직 없는 첫 훅에서는
    0(측정불가)을 반환해 ``raw_tokens > 0`` measured 재무장 신호와 marker 보존 계약을 지킨다.

    경계는 주 대화 기록에서 주 세션 압축만 남긴다(서브에이전트 압축의 경계는 서브에이전트 기록에
    남는다). 훅은 이 uuid 를 경계로 넘기고 엔진이 세션마다 한 번 처리한다. 경계에 ``uuid`` 가 없으면
    형식이 바뀐 것이라 ValueError 를 낸다. 열기 실패(없음·디렉터리·권한)는 ``(0, None, None)``
    (측정불가)이고, 연 뒤 읽는 중 오류는 올린다.
    """
    try:
        stream = open(transcript_path, "rb")
    except OSError:
        return 0, None, None
    with stream:
        tokens, model, measured = 0, None, False
        scanned = 0
        for raw in _reverse_lines(stream):
            scanned += len(raw) + 1
            if not measured or _BOUNDARY_TOKEN in raw:
                for line in reversed(_decoded_lines(raw)):
                    entry = _json_object(line)
                    if entry is None:
                        continue
                    # compact 경계를 usage 보다 먼저 판정한다. 경계 항목 자체에 usage 같은 필드가 붙어도
                    # 압축 전 assistant usage 로 넘어가지 않도록 이 지점에서 역탐색을 끝낸다.
                    if _is_compact_boundary_entry(entry):
                        return tokens, model, _boundary_uuid(entry)
                    if not measured:
                        tokens, model = _usage_tokens_and_model(entry)
                        measured = tokens > 0
            if measured and scanned >= _TAIL_WINDOW_BYTES:
                break
    return tokens, model, None


def _reverse_lines(stream):
    """바이너리 스트림을 끝에서부터 개행 바이트로 나눈 줄(개행 제외) 단위로 낸다.

    읽기 단위보다 긴 줄은 조각을 목록에 모았다가 줄 시작을 만났을 때 한 번만 잇는다."""
    stream.seek(0, os.SEEK_END)
    position = stream.tell()
    pieces: list[bytes] = []
    while position > 0:
        size = min(_READ_CHUNK_BYTES, position)
        position -= size
        stream.seek(position)
        parts = stream.read(size).split(b"\n")
        pieces.append(parts[-1])
        if len(parts) == 1:
            continue
        yield b"".join(reversed(pieces))
        yield from reversed(parts[1:-1])
        pieces = [parts[0]]
    yield b"".join(reversed(pieces))


def _decoded_lines(raw: bytes) -> list[str]:
    """기록 한 줄(bytes)의 텍스트 줄 목록. 줄 나눔은 전체 텍스트 ``splitlines`` 와 같다.

    UTF-8 로 못 읽는 줄(쓰는 중인 마지막 줄이 다중 바이트 문자 중간에서 끊긴 경우 등)은 깨진 JSON
    줄과 같이 건너뛴다."""
    try:
        return raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []


def _json_object(line: str) -> dict | None:
    """기록 한 줄의 JSON 객체. 빈 줄·깨진 JSON·객체 아닌 값은 None."""
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except (ValueError, TypeError):
        return None
    return entry if isinstance(entry, dict) else None


def _usage_tokens_and_model(entry: dict) -> tuple[int, str | None]:
    """항목의 usage 입력 토큰합과 모델. usage 가 없거나 합이 0 이하면 (0, None)."""
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    tokens = _usage_input_tokens(usage) if usage is not None else None
    if tokens is None or tokens <= 0:
        return 0, None
    model = message.get("model")
    return tokens, model if isinstance(model, str) else None


def _is_compact_boundary_entry(entry) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("type") == "system"
        and entry.get("subtype") == "compact_boundary"
    )


def _boundary_uuid(entry: dict) -> str:
    """경계 항목의 uuid. 없거나 빈 문자열이면 경계를 가릴 수 없으므로 ValueError."""
    value = entry.get("uuid")
    if not isinstance(value, str) or not value:
        raise ValueError(f"compact 경계 항목에 uuid 가 없음(timestamp={entry.get('timestamp')!r})")
    return value


def is_subagent_hook(stdin: dict) -> bool:
    """서브에이전트 안에서 발화한 훅인가 — ``agent_id`` 하나로 판정한다.

    서브에이전트 훅 입력의 ``session_id``·``transcript_path`` 는 주 세션과 같아 그 둘로는 가를 수
    없다. ``agent_type`` 은 주 스레드 ``--agent`` 세션에도 실리므로 판정에 쓰지 않는다. ctx 훅과
    git-anchor 훅이 함께 쓴다."""
    agent_id = stdin.get("agent_id")
    return isinstance(agent_id, str) and bool(agent_id.strip())
