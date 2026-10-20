"""하네스 드라이버 계약 (층: infra).

claude·codex·opencode CLI 의 argv 조립, 출력(stream-json·jsonl) 판독, 회신 추출, 세션 재사용, 모델 원천
조회, 추론 값 검증, 스폰 진입점 표기를 위임·추가 리뷰어 공용으로 한 번 소유한다.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple, Sequence

from pm_engine.domain import conf_schema


# claude assistant usage wire → 원장 usage 4필드 이름. 한 스칼라로 접으면 캐시 **재적재**
# (cache_read)와 **새 적재**(cache_creation)가 구분되지 않아 세션 재사용의 비용 근거가 사라진다.
CLAUDE_USAGE_WIRE_FIELDS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input"),
    ("cache_creation_input_tokens", "cache_creation"),
    ("cache_read_input_tokens", "cache_read"),
    ("output_tokens", "output"),
)


def _claude_usage_fields(usage: object) -> dict[str, int] | None:
    """claude `message.usage` wire → 원장 4필드(있는 값만·음이 아닌 정수만).

    **없는 필드는 적지 않는다** — 0 은 "0 토큰"이라는 다른 사실이고, 수집 못 한 자리를 0 으로
    채우면 비용 표가 false-정밀해진다. 값 하나라도 형식이 깨졌으면 그 관측 전체를 미수집으로
    본다(부분 신뢰 값이 사실로 굳는 걸 막는다)."""
    if not isinstance(usage, dict):
        return None
    fields: dict[str, int] = {}
    for wire_key, field in CLAUDE_USAGE_WIRE_FIELDS:
        if wire_key not in usage:
            continue
        value = usage.get(wire_key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        fields[field] = value
    return fields or None


def parse_stream_json(lines) -> tuple[str | None, str | None, int | None]:
    """`claude -p --output-format stream-json` 출력에서 (session_id, result, used_tokens) 추출.

    회전 판정용 3-tuple facade — 같은 1회 파싱의 usage 분해까지 쓰려면
    `_parse_stream_json_events`(원장·세션 재사용 축)를 쓴다. 파서는 하나다.

    - session_id: `system/init` 이벤트의 `session_id`(이후 모든 이벤트에도 실리나 init 우선).
      init 가 없으면 `result` 이벤트의 session_id 로 폴백.
    - result: `result` 이벤트의 `result` 필드(= 최종 reply 텍스트).
    - used_tokens: 마지막 `assistant` 이벤트의 `message.usage` 입력 계열
      (`input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`) +
      `output_tokens` 합 — 회전(post-turn) 판정은 *다음* turn 이 지게 될 컨텍스트가 기준이라
      이번 응답 출력을 포함한다(훅이 도구 실행 전에 보는 "현재 점유"와 용도가 다름·codex 게이트).
      각 값은 음이 아닌 정수만 인정하며 신호가 하나도 없으면 None.
    - JSONDecodeError 라인은 skip(부분/비-JSON 라인에 robust).

    PoC(`scratch/poc/orchestrator_claude_relay_swap.py`)의 run_turn 파싱 골격을
    순수 함수로 추출 — driver 가 호출하고 테스트가 직접 검증한다.
    """
    session_id, result, used_tokens, _usage = _parse_stream_json_events(lines)
    return session_id, result, used_tokens


def _parse_stream_json_events(
    lines,
) -> tuple[str | None, str | None, int | None, dict[str, int] | None]:
    """claude stream-json 1회 파싱 — (session_id, result, used_tokens, usage 4필드).

    `parse_stream_json` 의 본체다. 회전 판정은 스칼라 합만 쓰고 원장은 분해를 쓰므로 두
    소비면이 같은 한 번의 순회를 나눠 갖는다(두 번째 파서를 만들지 않는다)."""
    import json  # 지연 import — 순수 헬퍼만 쓰는 경로의 import 비용 회피.

    session_id: str | None = None
    result: str | None = None
    used_tokens: int | None = None
    usage_fields: dict[str, int] | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue  # 비-JSON / 부분 라인 skip.
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id") or session_id
        if event.get("type") == "assistant":
            message = event.get("message")
            usage_fields = _claude_usage_fields(
                message.get("usage") if isinstance(message, dict) else None
            )
            used_tokens = sum(usage_fields.values()) if usage_fields else None
        if event.get("type") == "result":
            result = event.get("result")
            session_id = session_id or event.get("session_id")
    return session_id, result, used_tokens, usage_fields


def parse_opencode_json(lines) -> tuple[str | None, str | None, int | None]:
    """`opencode run --format json` 출력에서 (session_id, reply, used_tokens) 추출.

    claude `parse_stream_json` 과 **대칭** 위치의 opencode 어댑터용 순수 헬퍼 —
    하니스가 다른 한 줄=한 이벤트 JSON 스트림을 같은 3-tuple 규격으로 흡수한다.
    opencode driver(`pm_orch_opencode.py`)가 DI 로 주입받아 쓴다(엔진은 파싱만 보유).

    - session_id: 모든 이벤트 top-level `sessionID`(실측 — 매 이벤트에 실린다). 첫 등장값
      을 잡는다(opencode 가 sid 를 발급 — claude 와 달리 사전지정 불가, 출력 파싱으로 획득).
    - reply: `type:"text"` 이벤트의 `part.text` 를 등장 순서대로 누적(멀티-part 답변 대응).
      reply 가 없으면(text part 0) None.
    - used_tokens: 마지막 `step_finish` 이벤트의 `part.tokens` 에서 유효한 `total`(양의 정수)을
      우선하고, 없으면 `input + output + reasoning + cache.read + cache.write`로 폴백한다. 이때
      reasoning을 별도 가산해 보수적 상위집합으로 잡는다. tokens가 없거나 어느 값/cache 형상이
      비정상이면 None.
      설치 opencode 1.18.5 바이너리 실측도 total 우선 + cache 합산 폴백을 확인했다.
    - 비-JSON / 비-dict 라인은 skip(부분/노이즈 라인에 robust — claude 파서와 동일 정책).

    실측 이벤트 형식(opencode 1.17.6):
      {"type":"text","sessionID":"ses_...","part":{"type":"text","text":"PONG",...}}
    """
    import json  # 지연 import — 순수 헬퍼만 쓰는 경로의 import 비용 회피(claude 파서 대칭).

    session_id: str | None = None
    reply_parts: list[str] = []
    used_tokens: int | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue  # 비-JSON / 부분 라인 skip.
        if not isinstance(event, dict):
            continue
        sid = event.get("sessionID")
        if session_id is None and isinstance(sid, str) and sid:
            session_id = sid
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    reply_parts.append(text)
        if event.get("type") == "step_finish":
            part = event.get("part")
            tokens = part.get("tokens") if isinstance(part, dict) else None
            if isinstance(tokens, dict):
                total = tokens.get("total")
                if isinstance(total, int) and not isinstance(total, bool) and total > 0:
                    used_tokens = total
                else:
                    cache = tokens.get("cache", {})
                    values = [tokens.get(key, 0) for key in ("input", "output", "reasoning")]
                    if isinstance(cache, dict):
                        values.extend(cache.get(key, 0) for key in ("read", "write"))
                    else:
                        values.append(None)
                    # Upstream v1.18.5 근거 파일: session.ts, overflow.ts.
                    used_tokens = (
                        sum(values)
                        if all(isinstance(value, int) and not isinstance(value, bool)
                               and value >= 0 for value in values)
                        else None
                    )
            else:
                used_tokens = None
    reply = "".join(reply_parts) if reply_parts else None
    return session_id, reply, used_tokens


def _codex_usage_contract(wire: dict) -> dict:
    """codex wire usage(`*_tokens` 키)를 엔진 usage contract로 정규화.

    실측(codex 0.144.6·PM 라이브 프로브 5회): `turn.completed.usage` 의 실 wire 키는 접미사 `_tokens`
    형(`input_tokens`·`cached_input_tokens`·`cache_write_input_tokens`·`output_tokens`·
    `reasoning_output_tokens` — 예 input_tokens=
    12481·cached_input_tokens=9600·output_tokens=105·reasoning_output_tokens=92). 파서가 이 **wire→contract
    경계를 소유** 해 driver 인터페이스(접미사 없는 contract 키)를 하니스-무관으로 유지한다 — driver 는
    codex wire 형태를 몰라도 된다(claude/opencode 파서가 각자 wire 를 흡수하는 것과 동형). 비-정수/누락
    키는 0. cached_input/cache_write_input은 contract에 실어 관측 가능하게 두되 driver 사용 합산에서는
    제외한다. cached_input의 input 포함 관계는 실측됐지만 cache_write_input은 관측만 했고 포함 관계를
    검증하지 않았으므로 비합산 정책 자체만 보존한다(codex driver `_maybe_mark_ctx`).
    """
    def _n(key: str) -> int:
        val = wire.get(key)
        return val if isinstance(val, int) and not isinstance(val, bool) else 0

    return {
        "input": _n("input_tokens"),
        "cached_input": _n("cached_input_tokens"),
        "cache_write_input": _n("cache_write_input_tokens"),
        "output": _n("output_tokens"),
        "reasoning_output": _n("reasoning_output_tokens"),
    }


def parse_codex_json(lines) -> tuple[str | None, str | None, dict | None]:
    """`codex exec --json` 출력(JSONL)에서 (thread_id, reply, usage) 추출.

    claude `parse_stream_json`·opencode `parse_opencode_json` 과 **대칭** 위치의 codex 어댑터용
    순수 헬퍼 — codex 는 thread_id 를 사전지정 못 하고 `thread.started` 이벤트로 발급하므로
    (opencode sid 동형) 출력 파싱으로 획득한다. usage 는 codex driver 의 driver-side 기계 ctx
    가드(relay 경로엔 opencode plugin 같은 marker 채널이 없어 driver 가 예산을
    직접 판정)의 원천이다. 세 파서 모두 3번째 값으로 usage 를 낸다(v1.6.0 일원화 — codex 는 dict·claude/opencode 는 정수).

    - thread_id: `thread.started` 이벤트의 `thread_id`(첫 등장값 = resume 권위 id). 없으면 None
      (driver 가 치명 처리 — resume 불가).
    - reply: `item.*` 이벤트의 agent_message(`item.type == "agent_message"`) `text` — 최종
      (마지막 비어있지-않은) 값. item.started→completed 스트리밍 시 completed 가 최종 전체
      텍스트로 덮어쓴다. reasoning 등 다른 item 은 제외. 없으면 None.
      라이브 smoke 가 확정 — item 의 텍스트 필드가 다르면 여기 한 지점만 조정.)
    - usage: `turn.completed` 이벤트의 `usage`(마지막 등장값)를 wire(`*_tokens`)→contract
      (`{input,cached_input,cache_write_input,output,reasoning_output}`)로
      `_codex_usage_contract` 가 정규화한 값.
      없으면 None. (**실측 wire 키는 접미사 `_tokens`** — 접미사 없는 키를 읽으면 used==0 으로
      ctx 가드가 영구 사멸한다·PM 라이브 프로브 5회 권위·codex/opencode 리뷰 수렴 결함.)
    - 비-JSON / 비-dict 라인은 skip(claude·opencode 파서와 동일 robust 정책).
    """
    import json  # 지연 import — 순수 헬퍼만 쓰는 경로의 import 비용 회피(claude·opencode 파서 대칭).

    thread_id: str | None = None
    reply: str | None = None
    usage: dict | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue  # 비-JSON / 부분 라인 skip.
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "thread.started":
            tid = event.get("thread_id")
            if thread_id is None and isinstance(tid, str) and tid:
                thread_id = tid
        elif isinstance(etype, str) and etype.startswith("item"):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    reply = text  # 최종(마지막) agent_message 전체 텍스트로 갱신.
        elif etype == "turn.completed":
            u = event.get("usage")
            if isinstance(u, dict):
                usage = _codex_usage_contract(u)  # wire(_tokens) → contract 정규화.
    return thread_id, reply, usage


# ── 하네스 드라이버 계약 (검증·argv·회신 추출) — 위임/추가 리뷰어 공용 단일 소유 ────────
#
# 위임(pm_delegate)과 추가 리뷰어(additional_reviewer)는 **같은 세 하네스 CLI**를 스폰한다. 값 테이블과
# argv 조립이 두 표면에 각각 있으면 규칙이 둘로 갈리므로(진행신호 테이블·시간 프로필과 같은 이유)
# 이 leaf 모듈이 단일 소유한다. 이 모듈은 다른 엔진 도구를 import 하지 않아(cycle-free) 양쪽이
# 안전하게 deep-import 할 수 있다 — additional_reviewer 는 pm_delegate 를 import 하지 않는다(역방향
# deep-import 가 이미 있어 순환이 된다).

# 권한축 — write(편집 가능) / read(읽기 전용). 역할이 아니라 **축**이 드라이버 플래그를 정한다.
WRITE_ROLES: frozenset[str] = frozenset({"developer", "architect"})
READ_ROLES: frozenset[str] = frozenset({"researcher", "code-reviewer"})

class ModelSource(NamedTuple):
    """하네스별 **모델 값의 원천** 선언 — 목록을 엔진에 박지 않고 어디서 읽는지만 적는다.

    모델은 자주 바뀌므로 하드코딩은 곧 stale 이다. `kind` 가 읽는 방식을 정하고 소비자는 그 값으로만
    갈린다(하네스 이름 분기 0):
      · `alias`  — CLI 가 계약한 별칭이 `values` 에 있다(그 CLI `--help` 실측).
      · `live`   — `command` 가 목록을 준다. 산출 형식은 CLI 마다 달라 `parse` 가 **값으로**
                   고른다(하네스 이름 분기 0). 조회 실행은 `model_listing` 하나다. 판독 이름은
                   `json-slug`(모델 목록만)와 `opencode-verbose`(모델 목록과 모델별 variants)다.

    `value_format`·`how_to_check` 는 사람 안내 문구다(`pm_config` 의 `[m]` 위임 모델 안내).
    """

    kind: str
    values: tuple[str, ...]
    command: tuple[str, ...] | None    # `live` 의 조회 명령 (alias 는 None)
    parse: str | None                  # 그 산출을 읽는 방식 이름 (alias 는 None)
    value_format: str
    how_to_check: str


# 모델 원천 표 — 설정 키 레지스트리의 선택지·`pm-config delegate models`·`[m]` 안내가
# **이 표 하나**를 읽는다(값 표는 한 자리).
MODEL_SOURCE: dict[str, ModelSource] = {
    # `claude --help` 실측: "Provide an alias for the latest model (e.g. 'fable', 'opus', or
    # 'sonnet') or a model's full name (e.g. 'claude-fable-5')."
    "claude": ModelSource("alias", ("fable", "opus", "sonnet"), None, None,
                          "별칭 또는 풀네임", "`claude --help` 의 모델 옵션 설명"),
    # `codex debug models` 가 카탈로그를 JSON 으로 낸다(`{"models":[{"slug":…}]}`). 카탈로그는
    # 바이너리에 번들돼 있어 사실상 로컬 조회다 — 실측 0.34초(cold) · 0.02초(warm).
    "codex": ModelSource("live", (), ("codex", "debug", "models"), "json-slug",
                         "카탈로그의 slug 또는 풀네임", "`codex debug models`"),
    # `--verbose` 는 모델마다 머리 줄 `provider/id` 뒤에 모델 객체 JSON 을 낸다. 그 객체의
    # `variants` 키가 그 모델이 받는 추론 강도다(1.18.5·1.18.34 같은 코드).
    "opencode": ModelSource("live", (), ("opencode", "models", "--verbose"), "opencode-verbose",
                            "provider/model", "`opencode models --verbose`"),
}


# 모델 목록 조회 시간 상한(초). 회사 Pro·원격 게이트웨이는 첫 호출 지연이 커 15초로는 모자랐다.
# 코드 수정 없이 `PM_OPENCODE_MODELS_TIMEOUT`(초)로 늘린다. 값이 있으면 양의 정수만 받고 그 밖은 멈춘다
# (늘렸다고 믿는 값을 기본값으로 바꾸지 않는다). 미설정만 기본값이다. 빠른 로컬 조회라 무제한은 두지 않는다.
MODELS_TIMEOUT_SECONDS = 60


def models_timeout() -> int:
    """모델 목록 조회 시간 상한 — env `PM_OPENCODE_MODELS_TIMEOUT` > 기본 `MODELS_TIMEOUT_SECONDS`.

    env 가 있는데 양의 정수(앞뒤 공백 허용)가 아니면 `HarnessContractError` 다."""
    raw = os.environ.get("PM_OPENCODE_MODELS_TIMEOUT")
    if raw is None:
        return MODELS_TIMEOUT_SECONDS
    text = raw.strip()
    if not (text.isascii() and text.isdigit()) or int(text) <= 0:
        raise HarnessContractError(
            f"PM_OPENCODE_MODELS_TIMEOUT 값 {raw!r} 이 양의 정수가 아니다 — 초 단위 양의 정수로 "
            "고치거나 지운 뒤 다시 실행한다")
    return int(text)


class ModelListing(NamedTuple):
    """모델 목록 조회 한 번의 결과.

    `variants` 는 모델별 추론 강도를 내는 판독(`opencode-verbose`)만 채운다 — 모델 이름 →
    variants 키(조회 산출 순서). 별칭 원천과 `json-slug` 판독은 `None` 이다(모델별 값이 없다).
    """

    models: tuple[str, ...]
    variants: dict[str, tuple[str, ...]] | None


def _parse_json_slug(output: str) -> ModelListing:
    """`codex debug models` 산출 → `models[].slug`. 형식이 다르면 `HarnessContractError`."""
    try:
        document = json.loads(output)
    except ValueError as exc:
        raise HarnessContractError(f"JSON 이 아니다 ({exc})") from exc
    entries = document.get("models") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise HarnessContractError("`models` 배열이 없다")
    slugs: list[str] = []
    for entry in entries:
        slug = entry.get("slug") if isinstance(entry, dict) else None
        if not isinstance(slug, str) or not slug.strip():
            raise HarnessContractError(f"slug 형식이 아닌 항목 {entry!r}")
        slugs.append(slug)
    return ModelListing(tuple(slugs), None)


def _parse_opencode_verbose(output: str) -> ModelListing:
    """`opencode models --verbose` 산출 → 모델 목록과 모델별 variants.

    형식(실행 파일 판독): 모델마다 머리 줄 `provider/id` 뒤에 `JSON.stringify(모델, null, 2)`.
    모델 객체는 늘 `variants` 객체를 갖는다(없는 모델은 `{}`). 머리 줄 순서와 variants 키 순서를
    그대로 지킨다. 형식이 다르면 `HarnessContractError` 다(빈 목록으로 바꾸지 않는다).
    """
    decoder = json.JSONDecoder()
    models: list[str] = []
    variants: dict[str, tuple[str, ...]] = {}
    position = 0
    length = len(output)
    while True:
        while position < length and output[position].isspace():
            position += 1
        if position >= length:
            break
        line_end = output.find("\n", position)
        header = output[position:line_end if line_end != -1 else length].strip()
        if line_end == -1 or "/" not in header or header.startswith("{"):
            raise HarnessContractError(f"모델 머리 줄 `provider/id` 가 아니다: {header[:80]!r}")
        position = line_end + 1
        while position < length and output[position].isspace():
            position += 1
        try:
            model, position = decoder.raw_decode(output, position)
        except json.JSONDecodeError as exc:
            raise HarnessContractError(f"{header} 의 모델 객체가 JSON 이 아니다 ({exc})") from exc
        named = model.get("variants") if isinstance(model, dict) else None
        if not isinstance(named, dict):
            raise HarnessContractError(f"{header} 의 모델 객체에 `variants` 객체가 없다")
        models.append(header)
        variants[header] = tuple(named)
    return ModelListing(tuple(models), variants)


def resolve_executable(name: str, env: dict[str, str] | None) -> str:
    """실행할 프로그램 이름 → 실행 파일 경로 — 하네스 CLI 를 실행하는 자리가 모두 이 함수를 쓴다.

    자식이 받을 `env` 의 실행 경로 목록(`os.get_exec_path(env)` — None 이면 이 프로세스 env, PATH 가 없으면
    `os.defpath`. POSIX `subprocess` 가 이름을 찾는 목록과 같다)에서 `shutil.which` 로 찾는다. Windows 는 PATHEXT 까지
    붙여 찾으므로 npm 셈(`opencode.cmd`)도 해소된다 — 이름 그대로 넘기면 CreateProcess 는 `.exe` 만
    붙여 찾아 실패한다. 경로 성분이 있는 이름은 그 경로만 본다. POSIX 결과는 이름 그대로 실행할 때
    찾는 파일과 같다.

    찾지 못하면 `FileNotFoundError`(filename=이름)다 — 호출부가 이미 다루는 "실행 파일 없음" 예외와
    같은 종류라 확정 기동 실패·PATH 부재 문구 처리가 그대로 선다. 빈 값으로 바꾸지 않는다.
    실행 인자가 `.cmd`·`.bat` 을 거쳐 cmd.exe 가 해석해도 깨지지 않도록 엔진의 실행 인자에는 여러 줄
    값을 싣지 않는다(프롬프트는 표준 입력이나 `--file`)."""
    found = shutil.which(name, path=os.pathsep.join(os.get_exec_path(env)))
    if found is None:
        raise FileNotFoundError(
            errno.ENOENT,
            f"{name} 실행 파일 PATH 부재 — 이 프로세스의 PATH 에서 찾지 못했다. {name} 를 설치하거나 "
            "PATH 에 넣은 뒤 다시 실행한다",
            name)
    return found


# 판독 이름 → 판독 함수. 키는 산출 형식 이름이다(하네스 표가 아니다 — `MODEL_SOURCE.parse` 가 고른다).
_MODEL_LISTING_PARSERS = {
    "json-slug": _parse_json_slug,
    "opencode-verbose": _parse_opencode_verbose,
}

# 조회 기억 — 한 프로세스 안에서 같은 조건의 조회를 다시 실행하지 않는다. 키는 (명령, cwd 절대경로,
# env 전체)다. 실행 파일 위치는 env 의 PATH 가 정하므로 따로 넣지 않는다. 디스크에는 두지 않는다 —
# opencode 판·카탈로그 갱신으로 낡은 기록이 허용집합 밖 값을 통과시키면 조용한 무시가 되살아난다.
_MODEL_LISTING_MEMO: dict[tuple, ModelListing] = {}


def model_listing(source: ModelSource, *, cwd: Path | str, env: dict[str, str]) -> ModelListing:
    """모델 원천 한 행의 모델 목록(과 모델별 variants) — 모델 목록 조회의 유일한 실행 자리다.

    `alias` 는 표의 값 그대로다. `live` 는 `source.command` 를 `cwd`·`env` 로 실행한다(첫 인자는
    `resolve_executable` 로 `env` 의 PATH 에서 찾는다 — cross 실행과 같은 실행 파일 찾기). 실행 파일 없음·시간 초과·rc≠0·판독 실패·
    모델 0개는 전부 `HarnessContractError` 다. 빈 목록이나 기본값으로 바꾸지 않는다.
    """
    if source.kind == "alias":
        return ModelListing(tuple(source.values), None)
    if source.kind != "live":
        raise HarnessContractError(
            f"모르는 모델 원천 kind {source.kind!r} — MODEL_SOURCE 선언과 이 판정이 갈렸다.")
    parser = _MODEL_LISTING_PARSERS.get(source.parse)
    if parser is None or not source.command:
        raise HarnessContractError(
            f"모르는 판독 이름 {source.parse!r} 또는 조회 명령 없음 — MODEL_SOURCE 선언과 이 판정이 "
            "갈렸다.")
    command = tuple(source.command)
    key = (command, str(Path(cwd).resolve()), tuple(sorted(env.items())))
    remembered = _MODEL_LISTING_MEMO.get(key)
    if remembered is not None:
        return remembered
    shown = " ".join(command)
    timeout = models_timeout()
    try:
        result = subprocess.run(
            [resolve_executable(command[0], env), *command[1:]], cwd=str(cwd), env=dict(env),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
    except FileNotFoundError as exc:
        if exc.filename not in (None, command[0]):
            raise  # 실행 파일이 아니라 다른 경로(cwd 등)가 없다 — 이 판정의 몫이 아니다.
        raise HarnessContractError(
            f"{command[0]} 실행 파일 PATH 부재 — 이 프로세스의 PATH 에서 찾지 못해 모델 목록과 "
            f"variant 를 확인할 수 없다. {command[0]} 를 설치하거나 PATH 에 넣은 뒤 다시 실행한다"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HarnessContractError(
            f"{shown} {timeout}s timeout 초과 — PM_OPENCODE_MODELS_TIMEOUT 로 늘린다") from exc
    except UnicodeDecodeError as exc:
        raise HarnessContractError(f"{shown} 출력 판독 실패 — UTF-8 이 아니다 ({exc})") from exc
    if result.returncode != 0:
        raise HarnessContractError(
            f"{shown} 실패 rc={result.returncode}: {(result.stderr or '').strip()[:200]}")
    try:
        listing = parser(result.stdout or "")
    except HarnessContractError as exc:
        raise HarnessContractError(f"{shown} 출력 판독 실패 — {exc}") from exc
    if not listing.models:
        raise HarnessContractError(f"{shown} 출력에서 모델 0개 — 형식을 확인한다")
    _MODEL_LISTING_MEMO[key] = listing
    return listing


# codex sandbox 모드 — write=workspace-write·read=read-only.
# **실측(0.145.0)**: read-only 는 worktree 밖 `/tmp` 쓰기까지 차단해 pytest 기동이 막힌다(테스트
# 실행이 필요한 리뷰는 workspace-write 상향 필요). push 방어선은 sandbox 가 아니라 역할 프롬프트와
# 금지 명령 규칙이다.
CODEX_SANDBOX: dict[str, str] = {"write": "workspace-write", "read": "read-only"}
# 승인 없는 위임(`-a never`)은 네트워크 명령의 건별 승격을 요청할 수 없다. 그래서 workspace-write
# 실행의 명령 네트워크를 실행 단위 override 로 연다(프로젝트 config 의 대화형 기본값보다 우선하고,
# 읽기 역할의 tmp 재앵커처럼 원 프로젝트 config 를 읽지 않는 실행에도 같게 실린다). 설정 노브가
# 아니라 CLI 형식 선언이며 모든 역할·신규/재개 실행에 exec 앞 전역 위치로 정확히 한 번 들어간다.
CODEX_WORKSPACE_EXEC_OPTIONS: tuple[str, ...] = (
    "-c", "sandbox_workspace_write.network_access=true",
)
# exec 앞 전역 위치에 오는 값-동반 옵션 — `read_codex_global_section` 이 이 집합으로만 전역 구간을
# 판독한다(모르는 토큰을 만나면 손상 argv 로 멈춘다).
_CODEX_GLOBAL_VALUE_OPTIONS: frozenset[str] = frozenset({"-a", "-s", "-c", "-C", "--add-dir"})
_CODEX_EXEC_SUBCOMMAND = "exec"
# opencode 역할 agent — 네이티브 task와 cross `run --agent`가 같은 custom 정의를 쓴다.
# 네 역할 카드는 `mode: all`이라 subagent/primary 양쪽에서 유효하다. build/plan으로 축약하면
# 역할 prompt·모델·권한을 잃고(특히 reviewer가 Plan Mode로 사본을 못 씀) default agent로 오동작한다.
OPENCODE_AGENT: dict[str, str] = {
    "developer": "developer",
    "researcher": "researcher",
    "architect": "architect",
    "code-reviewer": "code-reviewer",
}
# Cross-harness 실행은 수신 저장소가 OpenCode adopter라고 가정할 수 없다. 예를 들어 Claude
# adopter에서 `opencode run --agent code-reviewer`를 호출하면 프로젝트 `.opencode/agents/`가
# 없으므로 OpenCode가 default build agent로 조용히 폴백한다. argv의 역할 이름만 정확해도 역할은
# 보존되지 않는 이유다. 따라서 공용 relay가 **이번 역할 하나**의 mode/permission을 런타임 config로
# 만든다. 역할 지시는 pm_delegate가 첨부 prompt의 preamble으로 이미 싣고, 여기서는 하네스가 역할을
# 실제 선택하고 그 권한으로 실행하는 데 필요한 최소 기계 계약만 소유한다.
_OPENCODE_DANGEROUS_BASH_PERMISSION: dict[str, str] = {
    "*": "allow",
    "rm *": "deny",
    "git push --force*": "deny",
    "git push -f*": "deny",
    "git clean -f*": "deny",
    "git reset --hard*": "ask",
}


def opencode_runtime_role_config(role: str) -> str:
    """Return deterministic inline config that makes ``--agent <role>`` self-contained.

    The caller places this engine-owned value in ``OPENCODE_CONFIG_CONTENT`` *after* environment
    allowlisting. Incoming content is intentionally not merged or inherited: it is an arbitrary runtime
    override surface and may contain plugins, commands, or secrets. Normal user/project config files still
    load through the separately allowlisted ``OPENCODE_CONFIG``/``OPENCODE_CONFIG_DIR`` anchors; this
    highest-precedence fragment only pins the delegated role's identity and permissions.
    """
    perm_axis(role)  # shared role allowlist and wording
    # ``edit`` is allowed for every role, researcher included: all participating roles record
    # their own output in the ticket copy's role section, and the shipped opencode researcher
    # card carries the same ``edit: allow`` / ``bash: deny`` axis. This fragment is the
    # highest-precedence config, so a researcher ``edit: deny`` here would override the card
    # and leave the harvested section unwritten. Path narrowing is not an opencode
    # edit-permission axis; the pre/post delegation touches audit holds it.
    permission: dict[str, object] = {
        "read": "allow",
        "edit": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "task": "deny",
        # researcher 도 위험명령 제외 bash 를 허용한다(T-opencode-002): write 단일
        # 경로 강제 시 대형·반복 산출에서 NO-WRITE 멈춤이 관측됐고(code-reviewer 선례와 동일
        # 패턴맵으로 쓰기 사다리를 닫지 않게 한다), pre/post delegation touches 감사가 남는다.
        "bash": dict(_OPENCODE_DANGEROUS_BASH_PERMISSION),
        "webfetch": "deny",
    }
    config = {
        "agent": {
            OPENCODE_AGENT[role]: {
                "description": f"project-manager runtime {role} delegation role",
                "mode": "all",
                "permission": permission,
            },
        },
    }
    return json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def with_opencode_runtime_role(env: dict[str, str] | None, role: str) -> dict[str, str]:
    """Copy ``env`` and bind the engine-owned OpenCode role fragment without mutating the caller."""
    configured = dict(env or {})
    configured["OPENCODE_CONFIG_CONTENT"] = opencode_runtime_role_config(role)
    return configured


_RUNTIME_ROLE_CONFIG_BUILDERS = {
    "claude": None,
    "codex": None,
    "opencode": opencode_runtime_role_config,
}


def with_harness_runtime_role(
    env: dict[str, str] | None, harness: str, role: str,
) -> dict[str, str]:
    """Apply the declared harness runtime-role fragment without caller-side branching."""
    validate_harness(harness)
    perm_axis(role)
    configured = dict(env or {})
    builder = _RUNTIME_ROLE_CONFIG_BUILDERS[harness]
    if builder is not None:
        configured["OPENCODE_CONFIG_CONTENT"] = builder(role)
    return configured


# opencode `run` 은 첨부 `--file` 이 있어도 **비어있지 않은 positional message 를 요구**한다(실측·
# message 부재 시 rc=1). 실 지시는 `--file` 프롬프트에 있으므로 고정 안내 message 를 준다.
OPENCODE_ATTACHED_MSG = "첨부된 프롬프트 파일(--file)의 지시를 그대로 수행하라."

# claude 가용 도구셋 — `--tools`(가용성 제한, `--allowedTools` 아님). 역할축:
#   write(developer/architect) = 편집 도구 포함 · researcher = 읽기 + 라운드 파일
#   (`NN-<role>.md`) 기록용 Edit(Write/Bash 없음) · code-reviewer = 읽기+Bash(pytest·Write/Edit
#   제외). 콤마-구분 단일 인자.
#
# researcher 의 Edit 는 "모든 참여 역할이 자기 산출을 라운드 파일 하나로 남긴다"의
# 최소 수단이다. **보장 수준을 정확히 적는다**: `--tools` 는 도구 *가용성* 축이라 claude 축에서는
# 편집 대상 경로를 좁히지 못한다(규율은 역할 카드 문구와 위임 전후 git/touches 감사가 든다).
# 경로를 기계로 좁히는 것은 다른 두 축이다 — codex 는 read-only sandbox + `--add-dir <copy dir>`,
# opencode 는 역할 agent 의 permission. code-reviewer 도 같은 사본 절을 쓰지만 수단이 다르다:
# `--tools` 에 Edit 가 없고 Bash 로 쓴다. 조사 역할의 도구 축소 근거는 역할 카드의 도구 가드다.
CLAUDE_TOOLS_WRITE = "Read,Glob,Grep,Bash,Write,Edit"
CLAUDE_TOOLS_RESEARCHER = "Read,Glob,Grep,Edit"
CLAUDE_TOOLS_REVIEWER = "Read,Glob,Grep,Bash"


class HarnessContractError(RuntimeError):
    """하네스/reasoning 값이 드라이버 허용집합 밖 — 스폰 전 fail-loud.

    소비 표면은 자기 예외(DelegateError·ReviewerTargetError)로 감싸되 **문구는 이 모듈이 소유**한다
    (같은 오설정이 두 표면에서 다르게 설명되지 않게).
    """


def validate_harness(harness: str) -> str:
    """지원 하네스만 통과시킨다(조용한 폴백 금지·명시 등록 요구)."""
    if harness not in conf_schema.HARNESS_CHOICES:
        raise HarnessContractError(
            f"미지원 harness {harness!r} — 지원: {', '.join(conf_schema.HARNESS_CHOICES)}. "
            "조용한 폴백 금지(명시 등록 요구)."
        )
    return harness


# 정적 허용값이 없는 하네스 — 추론 허용값을 모델 목록 조회의 `variants` 에서 얻는다(파생·이름 리터럴 0).
PER_MODEL_REASONING_HARNESSES: tuple[str, ...] = tuple(
    name for name in conf_schema.HARNESS_CHOICES if name not in conf_schema.REASONING_ALLOWED)


def normalize_reasoning(value: str | None) -> str | None:
    """추론 값 정리 — 앞뒤 공백 제거, 빈 값은 None(플래그 생략). 확인은 하지 않는다."""
    if value is None:
        return None
    return value.strip() or None


def reasoning_allowed(
    harness: str, model: str | None, *, cwd: Path | str, env: dict[str, str],
) -> tuple[str, ...]:
    """그 하네스·모델이 받는 추론 강도 — 정적 허용값이 있으면 그 표, 없으면 모델 목록 조회의 variants.

    모델별 허용값 하네스에서 모델 값이 없거나, 조회 결과에 그 모델이 없거나, 원천이 variants 를
    내지 않으면 판정할 수 없으므로 `HarnessContractError` 다(조회 실패도 그대로 올라간다).
    """
    validate_harness(harness)
    static = conf_schema.REASONING_ALLOWED.get(harness)
    if static is not None:
        return static
    source = MODEL_SOURCE[harness]
    name = (model or "").strip()
    if not name:
        raise HarnessContractError(
            f"{harness} 추론 강도는 모델마다 다르다 — 모델 값이 없어 허용집합을 정할 수 없다. "
            "같은 프로필의 모델을 채운다.")
    listing = model_listing(source, cwd=cwd, env=env)
    if listing.variants is None:
        raise HarnessContractError(
            f"{harness} 모델 원천({source.how_to_check})은 모델별 추론 강도를 내지 않는다 — "
            "MODEL_SOURCE 선언과 이 판정이 갈렸다.")
    if name not in listing.variants:
        raise HarnessContractError(
            f"{harness} 모델 목록({source.how_to_check})에 {name} 이 없다 — 그 모델의 추론 강도를 "
            "확인할 수 없다. 모델 이름을 고치거나 그 모델을 쓸 수 있게 설정한다.")
    return listing.variants[name]


def validate_reasoning(
    harness: str, reasoning: str | None, *, model: str | None, cwd: Path | str,
    env: dict[str, str],
) -> str | None:
    """실행 직전 확인 — reasoning 이 그 하네스·모델의 허용집합 원소인가. 미지정=None(조회 없음).

    허용집합은 `reasoning_allowed` 가 정한다(실행과 같은 cwd·env 로 조회). 밖이면 fail-loud 다 —
    하네스는 허용집합 밖 값을 오류 없이 버리므로 조용한 무시를 호출 전에 막는다."""
    value = normalize_reasoning(reasoning)
    if value is None:
        return None
    allowed = reasoning_allowed(harness, model, cwd=cwd, env=env)
    if value not in allowed:
        known = ", ".join(allowed) if allowed else "없음"
        raise HarnessContractError(
            f"reasoning {value!r} 은 {harness} {model} 허용집합 ({known}) 밖 — 하네스는 허용집합 "
            "밖 값을 오류 없이 버리므로 멈춘다. 추론 강도를 비우거나 허용집합 안 값으로 고친다."
        )
    return value


def perm_axis(role: str) -> str:
    """역할 → 권한축('write' | 'read'). 미등록 역할은 보수적으로 read."""
    return "write" if role in WRITE_ROLES else "read"


def claude_tools(role: str) -> str:
    """역할 → claude `--tools` 값(code-reviewer = 읽기 + Bash·Write/Edit 없음)."""
    if role in WRITE_ROLES:
        return CLAUDE_TOOLS_WRITE
    if role == "researcher":
        return CLAUDE_TOOLS_RESEARCHER
    return CLAUDE_TOOLS_REVIEWER


def build_codex_argv(
    model: str,
    reasoning: str | None,
    role: str,
    cwd: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """codex argv. `-a never -s <mode>` 는 exec **앞** 전역 옵션(exec 뒤는 rc=2).

    프롬프트는 stdin 주입(argv positional 아님). `cwd` 를 주면 `-C` 로 핀하고, 생략하면 프로세스
    cwd 를 그대로 쓴다(추가 리뷰어는 격리 거울을 subprocess cwd 로 이미 고정한다)."""
    mode = CODEX_SANDBOX[perm_axis(role)]
    argv = ["codex", "-a", "never", "-s", mode, *CODEX_WORKSPACE_EXEC_OPTIONS]
    if resume_session_id is None:
        argv += ["exec", "--json", "--skip-git-repo-check"]
        if cwd is not None:
            argv += ["-C", str(cwd)]
    else:
        if not is_resumable_session_id(resume_session_id):
            raise HarnessContractError(
                f"세션 id 형식 불일치로 재개 argv 를 만들지 않는다: "
                f"{resume_session_id!r} — 형식(uuid 표기)이 맞는 값만 실린다"
                "(플래그 오소비 차단)."
            )
        # codex-cli 0.147.0 라이브 실측(2026-08-12): `codex exec --help`가 resume
        # 하위 커맨드를, `codex exec resume --help`가 `--json`과 `[SESSION_ID]
        # [PROMPT](`-`이면 stdin)을 함께 선언한다. resume 하위 커맨드에는 `-C`가 없으므로
        # 전역 `-C`를 exec 앞에 두고, 기존 JSONL 파이프라인과 같은 stdin을 `-`로 명시한다.
        if cwd is not None:
            argv += ["-C", str(cwd)]
        argv += ["exec", "resume", "--json", "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    if reasoning:
        # `-c model_reasoning_effort=<r>` 는 `-a`/`-s`(exec 뒤=rc=2)와 달리 exec **앞/뒤 모두
        # rc=0 수용**된다(실측). reasoning 미지정 시 무영향.
        argv += ["-c", f"model_reasoning_effort={reasoning}"]
    if resume_session_id is not None:
        argv += [resume_session_id, "-"]
    return argv


def read_codex_global_section(argv: list[str]) -> tuple[int, tuple[tuple[str, str], ...]]:
    """codex argv 의 exec 위치와 그 앞 전역 구간의 (옵션, 값) 쌍들.

    실행 파일 토큰 뒤 전역 구간은 `_CODEX_GLOBAL_VALUE_OPTIONS` 의 값-동반 옵션 쌍으로만 이뤄져야
    한다. 모르는 토큰·값 없는 옵션·exec 부재는 손상 argv 로 멈춘다."""
    pairs: list[tuple[str, str]] = []
    index = 1
    while index < len(argv) and argv[index] != _CODEX_EXEC_SUBCOMMAND:
        if argv[index] not in _CODEX_GLOBAL_VALUE_OPTIONS:
            raise HarnessContractError(
                f"codex argv 의 exec 앞 전역 구간이 손상됨(위치 {index}: {argv[index]!r})"
            )
        if index + 1 >= len(argv):
            raise HarnessContractError(f"codex argv 의 전역 옵션 {argv[index]!r} 에 값이 없음")
        pairs.append((argv[index], argv[index + 1]))
        index += 2
    if index >= len(argv):
        raise HarnessContractError("codex argv 에 exec 하위 명령이 없음")
    return index, tuple(pairs)


def with_codex_workspace_dirs(argv: list[str], roots: Sequence[str]) -> list[str]:
    """codex argv 의 exec 앞 전역 위치에 경로마다 `--add-dir <경로>` 를 넣은 새 argv.

    `--add-dir` 는 전역 옵션이다 — `codex exec resume` 뒤에 붙으면 CLI 가 rc=2 로 거부한다
    (`unexpected argument '--add-dir'`·0.163.0 실측). 그래서 신규·재개 모두 exec 앞에 둔다.
    전역 구간이 손상이거나 경로가 비면 멈춘다."""
    index, _global_options = read_codex_global_section(argv)
    inserted: list[str] = []
    for root in roots:
        if not root:
            raise HarnessContractError("codex --add-dir 경로가 비었다")
        inserted += ["--add-dir", str(root)]
    return [*argv[:index], *inserted, *argv[index:]]


def build_claude_argv(model: str, reasoning: str | None, role: str,
                      resume_session_id: str | None = None) -> list[str]:
    """claude argv. 프롬프트 stdin 주입·cwd 존중(플래그 불요). `--tools` 로 역할별 가용
    도구 제한, write 역할은 `--permission-mode acceptEdits` 로 무프롬프트 완주.

    **출력 형식 = `stream-json`**: `json` 은 종료 시 단일 덩어리라 진행 신호가 0이다. `--verbose`
    는 CLI 강제 요구다(미동반 시 즉시 rc≠0·2.1.220 실측). 회신 추출은 parse_stream_json 이며 이
    파서는 두 형식을 모두 통과시킨다.

    `resume_session_id` 를 주면 그 세션을 이어받는 turn 이 된다(직전 컨텍스트가 프롬프트 캐시
    단가로 재적재되고 도구 재읽기가 사라진다 — "재적재 0" 이 아니다). 값은 형식 검증을 통과할
    때만 실린다: 재개 id 는 argv 에 실리는 유일한 **외부 유래 토큰**이라, 손상 장부의 `--` 시작
    문자열이 그대로 나가면 CLI 가 그걸 플래그로 소비한다."""
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", model, "--tools", claude_tools(role)]
    if reasoning:
        argv += ["--effort", reasoning]
    if role in WRITE_ROLES:
        argv += ["--permission-mode", "acceptEdits"]
    if resume_session_id is not None:
        if not is_resumable_session_id(resume_session_id):
            raise HarnessContractError(
                f"세션 id 형식 불일치로 재개 argv 를 만들지 않는다: {resume_session_id!r} — "
                "형식(uuid 표기)이 맞는 값만 실린다(플래그 오소비 차단)."
            )
        argv += ["--resume", resume_session_id]
    return argv


def build_opencode_argv(
    model: str, reasoning: str | None, role: str, cwd: str, prompt_file: str,
) -> list[str]:
    """opencode argv. 프롬프트는 `--file`(실존 인터페이스·길이/ps 노출 회피)·cwd 는 `--dir`
    로 핀하고, `--agent <role>`로 네이티브와 같은 custom 역할 정의를 선택한다."""
    perm_axis(role)  # 공용 역할 allowlist 검증(예외 문구 단일 진실).
    agent = OPENCODE_AGENT[role]
    argv = ["opencode", "run", OPENCODE_ATTACHED_MSG, "--file", str(prompt_file),
            "--agent", agent, "--format", "json", "--dir", str(cwd)]
    if model:
        argv += ["-m", model]
    if reasoning:
        argv += ["--variant", reasoning]
    return argv


def assert_opencode_prompt_in_cwd(cwd: str | Path, prompt_file: str | Path) -> None:
    """opencode ``--file`` 이 ``--dir`` sandbox의 실제 하위 경로인지 단언한다.

    argv 빌더 자체는 추가 리뷰어의 표시용 placeholder도 조립하므로 순수 함수로 둔다. 실제 파일을
    넘기는 호출부가 이 가드를 직전에 호출한다. 양쪽 경로를 resolve해 실제 containment를 판정하며,
    해소 자체가 실패하면 공용 계약 예외로 번역한다. 따라서 ``--dir`` 안의 symlink가 바깥 파일을
    가리키는 형상도 거부하며, 파일 경로가 sandbox 루트와 같은 값인 형상도 하위로 인정하지 않는다.
    """
    try:
        sandbox = Path(cwd).resolve()
        attached = Path(prompt_file).resolve()
    except (OSError, RuntimeError) as exc:
        raise HarnessContractError(
            "opencode --file/--dir 실경로 해소 실패 — "
            f"--file={prompt_file} · --dir={cwd} · {type(exc).__name__}: {exc}"
        ) from exc
    try:
        relative = attached.relative_to(sandbox)
    except ValueError as exc:
        raise HarnessContractError(
            "opencode 전달 프롬프트가 --dir sandbox 밖입니다 — "
            f"--file={attached} · --dir={sandbox}"
        ) from exc
    if not relative.parts:
        raise HarnessContractError(
            "opencode 전달 프롬프트는 --dir sandbox의 파일 하위 경로여야 합니다 — "
            f"--file={attached} · --dir={sandbox}"
        )


# ── 세션 재사용(resume) 계약 ─────────────────────────────────────────────────
#
# 재개는 **직전 세션 컨텍스트의 프롬프트 캐시 재적재 + 도구 재읽기 0**이다(재적재 비용 0 아님).
# 지원 여부는 분기가 아니라 선언표다:
#   · claude — `-p` 재개 플래그로 같은 세션 id 를 이어받는다. 성공 판정은 **회신 세션 id 일치**
#     (rc 로 판정하지 않는다 — 만료/부분 손상이 rc=0 새 세션으로 나오는 축은 미측정이다).
#   · codex — 설치 codex-cli 0.147.0 라이브 실측(2026-08-12): `codex exec --help`에
#     `resume` 하위 커맨드가 있고, `codex exec resume --help`가 `[SESSION_ID] [PROMPT]`와
#     `--json`을 함께 선언한다. `codex -a never -s read-only -C /tmp exec resume --json
#     --skip-git-repo-check <uuid> -`도 인자 파싱을 통과해 app-server 초기화까지 진입했다
#     (현재 read-only 상위 sandbox에서는 상태 파일 생성 EROFS로 라이브 turn 전 중단). 따라서
#     위임 드라이버는 `SESSION_ID -`로 stdin delta를 보내고 기존 JSONL 파서를 그대로 쓴다.
#   · opencode — 위임 드라이버에 재개 배선 없음.
HARNESS_RESUME_SUPPORT: dict[str, bool] = {
    "claude": True, "codex": True, "opencode": False,
}

# 세션 id 형식(uuid 표기) 가드. 재개 id 는 장부에서 온 값이라 형식이 맞을 때만 argv 에 싣는다.
SESSION_ID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z"
)


def harness_supports_resume(harness: str) -> bool:
    """이 하네스로 세션 재사용을 시도해도 되는가(미등록/미지원은 False = fresh)."""
    return bool(HARNESS_RESUME_SUPPORT.get(harness, False))


def is_resumable_session_id(value: object) -> bool:
    """재개 argv 에 실을 수 있는 세션 id 형식인가(uuid 표기 완전일치)."""
    return isinstance(value, str) and SESSION_ID_RE.match(value) is not None


class HarnessReply(NamedTuple):
    """하네스 wire 1회의 구조화 관측 — 최종 회신·세션 id·원장 usage.

    세 파서가 각자 wire 를 흡수한 결과를 한 형상으로 모은다. 종전 reply 전용 seam 은 이 관측의
    한 필드를 꺼내는 얇은 facade 다(세션 id·usage 를 버리던 자리)."""

    reply: str | None
    session_id: str | None
    usage: dict[str, int] | None


def _claude_harness_reply(lines) -> HarnessReply:
    session_id, reply, _used_tokens, usage = _parse_stream_json_events(lines)
    return HarnessReply(reply, session_id, usage)


def _codex_harness_reply(lines) -> HarnessReply:
    """thread id 가 세션 id 자리다. usage 는 `_codex_usage_contract` 5필드
    (`input`·`cached_input`·`cache_write_input`·`output`·`reasoning_output`)를 **접지 않고**
    그대로 싣는다 — `cached_input` 이 `input` 에 포함되고 cache write 의 포함 관계는 미검증이라,
    같은 필드 이름(claude 의 4필드)으로 접으면 합산 의미가 하네스마다 달라진다. 소비자는 장부
    행의 `harness` 를 보고 해석한다. 이 5필드 중 missing/비-정수 키는 `_codex_usage_contract`
    가 0 으로 채운다 — claude 4필드 축의 "부재=미관측"(값 자체를 만들지 않음)과 다른 규칙이라
    claude 처럼 "0 은 실측"으로 읽으면 안 된다."""
    thread_id, reply, usage = parse_codex_json(lines)
    return HarnessReply(reply, thread_id, usage)


def _opencode_harness_reply(lines) -> HarnessReply:
    """총합 스칼라만 내는 파서 축 — wire `part.tokens` 에는 실제로 input/output/reasoning/cache
    분해가 있지만(`parse_opencode_json`) 파서가 스칼라 하나로 접어, 원장에 실을 하네스 contract가
    없다. 그 스칼라도 두 갈래 의미(`total` 원값 또는 보수적 상위집합 합)라 값 형태로 실으면 같은
    이름 아래 다른 사실이 섞인다. 이번 범위 밖(별건 후보) — 원장 usage 는 필드 부재로 남긴다."""
    session_id, reply, _used_tokens = parse_opencode_json(lines)
    return HarnessReply(reply, session_id, None)


# 하네스 → wire 관측 어댑터(선언표 — 소비면에 하네스 분기를 만들지 않는다).
HARNESS_REPLY_ADAPTERS = {
    "claude": _claude_harness_reply,
    "codex": _codex_harness_reply,
    "opencode": _opencode_harness_reply,
}


def extract_harness_result(harness: str, stdout: str) -> HarnessReply:
    """하네스 stdout(구조화 wire)에서 회신·세션 id·usage 를 한 번에 관측한다.

    reply 의 **타입 계약은 이 seam 이 소유한다**(아래 `extract_harness_reply` 주석 참조) — 그
    판정을 여기서 한 번 세우고 두 소비면(회신 전용 facade·원장/세션 재사용)이 같은 결과를 쓴다.
    미지원 하네스는 fail-loud(조용한 폴백 금지)."""
    adapter = HARNESS_REPLY_ADAPTERS.get(harness)
    if adapter is None:
        raise HarnessContractError(f"미지원 harness {harness!r} — reply 추출 불가.")
    observed = adapter(stdout.splitlines())
    reply = observed.reply
    return observed._replace(
        reply=reply if isinstance(reply, str) and reply.strip() else None
    )


def extract_harness_reply(harness: str, stdout: str) -> str | None:
    """하네스 stdout(구조화 wire)에서 최종 reply 텍스트만 추출한다.

    claude=parse_stream_json → result · codex=parse_codex_json → reply ·
    opencode=parse_opencode_json → reply. 미추출(파싱 실패·빈 출력)은 None — 호출자가 fail-loud
    한다(wire 원문을 그대로 판정에 넣지 않는다). 세션 id·usage 까지 필요한 호출자는
    `extract_harness_result` 를 쓴다(같은 관측의 다른 필드다).

    **최종 회신의 타입 계약은 이 seam 이 소유한다** — 내용이 있는 `str` 하나이고, 그 밖의 값
    (dict/list/수·null·빈 문자열·공백만)은 전부 미추출(None)이다. wire 파서는 "wire 가 무엇을 말했는지"를
    그대로 옮기는 층이라 claude `result` 필드처럼 스키마상 임의 JSON 값이 올 수 있는 자리를
    통과시킨다(실측 형상: `{"type":"result","result":{...}}`). 그 값이 소비 표면까지 흘러가면
    판정 파싱·전사 단계에서 AttributeError 로 터지는데, 그 시점은 raw 박제·장부 마감 **전**이라
    터진 실행은 원문도 장부도 남기지 않는다. 그래서 타입은 두 표면(위임·추가 리뷰어)이 공유하는
    이 한 지점에서 세운다 — 형상 붕괴는 "회신 없음"으로 환원되고 호출자의 기존 fail-loud 경로가
    그대로 받는다.

    **공백만 있는 회신은 빈 회신과 같다.** 참/거짓만 보면 `" "`·`"\\n"` 이 통과해 리뷰어가 아무 말도
    하지 않은 실행이 '회신 있음'으로 기록되고, 그 뒤의 판정 파싱은 아무 토큰도 찾지 못한 채 판정
    불명확으로만 끝난다 — 폴백 신호(`reply_extraction_failed`)가 서지 않아 호출자가 내부 리뷰어로
    갈아탈 근거를 잃는다. 그래서 존재 판정은 `strip()` 으로 하고, **돌려주는 값은 원문 그대로**다
    (앞뒤 공백까지 회신 바이트의 일부라 판정·전사 표면이 wire 와 어긋나면 안 된다)."""
    return extract_harness_result(harness, stdout).reply


# ── 스폰 진입점 표기 (도구 승인 prefix·처방 커맨드가 공유) ──────────────────
#
# 처방·재실행 안내가 찍는 2-token(`인터프리터 + 진입점`)은 도구 승인 prefix 와 **같은 표기**여야
# 재사용 승인을 소비한다. Windows 의 `python3`/`python` 은 WindowsApps 가짜 shim 일 수 있어
# 런처(`py`)가 1순위다 — 그 규칙은 이 한 함수가 소유한다(사본을 만들면 표기가 갈린다).

# 표면별 진입점 — 도구 재사용 승인 prefix 가 되는 2-token 이다.
DELEGATE_ENTRYPOINT = ".project_manager/tools/pm_delegate.py"
ADDITIONAL_REVIEWER_ENTRYPOINT = ".project_manager/tools/additional_reviewer.py"


def running_on_windows() -> bool:
    """진입점 인터프리터 표기 판정(Windows 는 런처 `py`)."""
    return os.name == "nt"


def entrypoint_command(script: str, *,
                       windows: bool | None = None) -> tuple[str, str]:
    """도구 승인 prefix·처방 커맨드가 공유하는 2-token(인터프리터, 진입점)."""
    on_windows = running_on_windows() if windows is None else windows
    return ("py" if on_windows else "python3", script)
