#!/usr/bin/env python3
"""pm_delegate — cross-harness 역할 위임 채널 (ADR-0075 · sealed spike cross-harness-delegate).

PM 메인세션(claude/codex/opencode 어디든)이 세션을 떠나지 않고 역할 노동
(developer/researcher/architect/code-reviewer)을 **다른 하네스 CLI subprocess** 로 위임하는
순수 CLI. N×N 대칭 — 호출측 하네스 조건 0 (external_review 와 동형 seam).

이 도구는 **엔진 코어**만 담는다 (설계 spike §3~§6):
  · config 해소  — `delegate.<role>[.<tier>].harness/.model/.reasoning` 3키를 **원자 tuple**
                  `(harness, model, reasoning)` 로 해소(티어 세트 통째·혼합 상속/부분 override 금지).
  · 3 드라이버   — codex(`-a never -s <mode> exec --json`·stdin)·claude(`-p --tools`·stdin)·
                  opencode(`run --file --agent --dir`). reasoning 은 드라이버별 플래그(§6).
  · 권한 매핑    — 역할축(write=developer/architect·read=researcher/code-reviewer)을 argv/sandbox 로
                  강제하되 보장 수준을 정직 표기(§3.5).
  · 쓰기-타깃 axis — 엔진 코드(`.project_manager/tools/`) write 위임이 PM 홈 cwd 면 canonical
                  worktree 재앵커 fail-loud(§4.6·external_review `_pm_home_reanchor` 재사용).
  · 시크릿 통제  — 합성 프롬프트 denylist 스캔 + 전 탐지를 본 사람의 건별 CLI ack + subprocess env
                  allowlist 정제 + prompt-file containment(§4.7). ack digest는 해소된 primary
                  harness:model과 합성 전문에 결속한다. 단, ack 통과 뒤 primary 인프라 실패로 명시
                  설정된 loud 폴백이 발동하면 타 하네스 수신자가 추가될 수 있다.
  · 결과 수집    — 최종 reply 텍스트만 stdout·raw+메타는 O_EXCL·0600·PID/UUID 파일 박제(§3.4).
  · loud 폴백    — 역할/티어별 명시 fallback tuple 이 있을 때만 인프라 실패를 양성 분류해 1회 실행하고
                  실행 provenance 를 reply/raw 에 남김(미설정·비-인프라 실패는 기존 fail-loud).
                  **시간 예산**: 폴백은 primary 와 별개로 turn timeout 을 새로 쓴다 — 최악 소요는
                  primary·폴백 **각 하네스 예산의 합**이다(codex/claude=timeout · opencode 는
                  첫-이벤트 워치독 재시도분이 더 붙는다·_harness_timeout_budget). 호출부(스킬·CI)의
                  대기 예산은 --dry-run 이 찍는 실수치로 잡아라.
  · opt-in 게이트 — `delegate_enabled`(기본 OFF) 비활성 시 rc=3 + stderr 안내(§5.4·false-green 차단).

설정 시드/lint(T-0446)·어댑터 배선(T-0447)·라이브 실측(T-0449)은 별도 티켓. 이 티켓은 라이브 CLI 를
호출하지 않는다 — 단위 테스트는 전부 mock(run_fn DI).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import functools
import hashlib
import hmac
import importlib.util
import math
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, NamedTuple


# ── REPO 앵커 (external_review 동형·상향 탐색·hermetic 테스트 monkeypatch seam) ────────
# 하드코딩 parents[2] 대신 `.project_manager` 를 품은 첫 조상을 REPO 로 삼는다(채택자/worktree 등
# 다른 깊이여도 견고). module-level 상수라 테스트가 monkeypatch 할 수 있다.

def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / ".project_manager").is_dir():
            return ancestor
    return here.parents[2]


REPO = _find_repo_root()
LOCAL_CONF = REPO / ".project_manager" / "local.conf"
# ticket frontmatter(touches) 조회용 board 진입점 — 범위 밖 변경 판정 입력(T-0462).
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"


# ── 도메인 상수 ────────────────────────────────────────────────────────────

HARNESS_CHOICES: tuple[str, ...] = ("claude", "codex", "opencode")
ROLE_CHOICES: tuple[str, ...] = ("developer", "researcher", "architect", "code-reviewer")
TIER_CHOICES: tuple[str, ...] = ("normal", "hard")

# 권한 역할축(§3.5) — write=저장소 파일 쓰기·read=저장소 read-only(+reviewer 는 테스트 실행).
WRITE_ROLES: frozenset[str] = frozenset({"developer", "architect"})
READ_ROLES: frozenset[str] = frozenset({"researcher", "code-reviewer"})

# 위임 turn 기본 타임아웃(초) — dev 는 reasoning+다중 편집으로 길다(codex driver TURN_TIMEOUT 600 보다
# 큼·§5.3). `--timeout`·local.conf `delegate_timeout` 로 override.
# 폴백이 발동하면 primary 소진 후 1회 더 실행한다 — 실행 1회의 최악 소요는 하네스마다 달라서
# _harness_timeout_budget 이 계산한다(2차 폴백 없음 — 상한은 두 시도 예산의 합으로 닫힌다).
DELEGATE_TIMEOUT_SECONDS = 1800

# 인프라 실패 클래스 라벨(loud 메시지·raw provenance 에 그대로 실리는 안정 문자열).
FAILURE_CLASS_LAUNCH = "스폰 실패/바이너리 부재"
FAILURE_CLASS_TIMEOUT = "타임아웃"
FAILURE_CLASS_STALL = "첫-이벤트 stall(재시도 소진)"
FAILURE_CLASS_QUOTA = "한도/레이트리밋"
FAILURE_CLASS_AUTH = "인증 실패"

# RunResult 의 **명시 실패 신호** 키(rc 값 추론 금지·codex must-fix). 엔진(_default_run_fn·
# _execute_attempt)만 세팅한다 — 하네스가 우연히 같은 rc 를 내도 분류되지 않는다.
RUN_RESULT_LAUNCH_FAILED = "launch_failed"   # 바이너리 부재/PATH/exec 권한 — 프로세스가 뜨지 못함
RUN_RESULT_STALLED = "stalled"               # opencode 첫-이벤트 stall(유한 재시도 소진·pm_relay)

# opencode 첫-이벤트 stall 을 stderr 에 찍는 엔진 마커(단일 출처) — 분류기의 백스톱 신호로도 쓴다.
OPENCODE_STALL_MARKER = "[opencode 첫-이벤트 stall:"

# opt-in 게이트 키(기본 OFF·per-clone·ADR-0004 상속·§5.4).
DELEGATE_ENABLED_KEY = "delegate_enabled"

# 합성 프롬프트 전문에 묶는 건별 시크릿 스캔 승인 토큰(T-0476). SHA-256 전체 계산 뒤 앞 96bit만
# 표시한다 — 사람이 재실행 커맨드로 옮길 만큼 짧지만, 이 값은 인증 secret이 아니라 "방금 검토한
# 프롬프트와 현재 프롬프트가 같은가"를 묶는 변경 감지 토큰이다. conf 키는 의도적으로 두지 않는다.
SECRET_SCAN_ACK_HEX_LENGTH = 24
# 사람 검토용 stderr/dry-run 목록은 이 수까지만 표시한다. 승인 뒤 raw 감사에는 중복 제거된 전 hit를
# 한 줄씩 모두 남겨, 대량 탐지가 터미널과 단일 메타 헤더를 비대하게 만들지 않으면서도 감사 완결성을
# 보존한다.
SECRET_SCAN_HIT_DISPLAY_LIMIT = 20

# reasoning 드라이버별 허용집합(§6) — **T-0449 라이브 실측으로 박제**(codex-cli 0.145.0·claude 2.1.218·
# opencode 1.18.4). 허용집합 밖 `.reasoning` 지정은 fail-loud(조용한 무시/강등 금지):
#   · codex(`-c model_reasoning_effort`): low/medium/high/xhigh — 0.145.0 xhigh 실측 수용(exec 앞/뒤
#     위치 무관 rc=0·T-0449).
#   · claude(`--effort`): low/medium/high/xhigh/max — claude CLI 가 미지원값에 "Valid values: low,
#     medium, high, xhigh, max" 경고를 뱉어 **CLI-authoritative 실측**(T-0449).
#   · opencode(`--variant`): minimal/low/medium/high/max — opencode 는 `--variant` 를 **CLI 검증하지
#     않고 provider 로 passthrough**(valid/invalid 모두 rc=0·미지원값은 provider 가 silent-ignore·
#     reasoning:0·T-0449 실측). 그래서 이 집합은 opencode 의 문서화 ladder(help: "e.g. high, max,
#     minimal")를 pm_delegate 의 **typo-guard** 로 인코딩한 것 — silent no-op(§3.2)을 전송 전에 차단한다.
#     provider 별 실지원은 다를 수 있다(passthrough 특성).
_REASONING_ALLOWED: dict[str, frozenset[str]] = {
    "codex": frozenset({"low", "medium", "high", "xhigh"}),
    "claude": frozenset({"low", "medium", "high", "xhigh", "max"}),      # T-0449 실측(CLI-authoritative)
    "opencode": frozenset({"minimal", "low", "medium", "high", "max"}),  # T-0449 실측(문서 ladder·passthrough)
}

# reasoning 드라이버별 argv 플래그(§6) — 매핑만 보유(값 검증은 _REASONING_ALLOWED).
#   codex `-c model_reasoning_effort=<r>` · claude `--effort <r>` · opencode `--variant <r>`.

# codex sandbox 모드(§3.5·§5) — write=workspace-write·read=read-only.
# **T-0449 실측(0.145.0)**:
#   · read-only 는 worktree 밖 `/tmp` 쓰기까지 **차단**("Read-only file system") → pytest 는 tmp 캡처
#     파일을 못 만들어 **아예 시작 불가**("No usable temporary directory found"). 즉 **codex 의
#     code-reviewer(read axis→read-only)는 read-only 로는 pytest 를 못 돌린다**(§3.5 §주1 우려 실현) —
#     테스트 실행이 필요한 리뷰는 workspace-write 상향이 필요(보장 수준=기계적→규율 하향). 이 매핑
#     조정은 §3.5 보장-모델 결정이라 PM 판단으로 보류(researcher=순수읽기는 read-only 로 정상).
#   · `-a never` 하 `git push` 는 **hang/승인-refusal 없이 즉시 실행**되어 git 레벨에서 실패한다
#     (도달 불가 원격→rc=128·refspec 불일치→rc=1). 승인 deadlock(§5·§10 우려)은 없음 — 그래서 push
#     방어선은 sandbox/approval 이 아니라 **role prompt(위임 역할은 push 안 함)**다(§5 설계대로).
_CODEX_SANDBOX = {"write": "workspace-write", "read": "read-only"}
# opencode 권한 agent(§3.3·D2) — write=build·read=plan.
_OPENCODE_AGENT = {"write": "build", "read": "plan"}
# opencode `run` 은 첨부 `--file` 이 있어도 **비어있지 않은 positional message 를 요구**한다(실측·codex
# must-fix — message 부재 시 rc=1). 실 지시는 `--file` 프롬프트에 있으므로 고정 안내 message 를 positional
# 로 준다(프롬프트 파일을 가리키는 얇은 지시).
_OPENCODE_ATTACHED_MSG = "첨부된 프롬프트 파일(--file)의 지시를 그대로 수행하라."

# claude 가용 도구셋(§3.5·§5) — `--tools`(가용성 제한, `--allowedTools` 아님·R2 교정). 역할축:
#   write(developer/architect) = 편집 도구 포함 · researcher = 순수읽기(Bash 제외·기계적) ·
#   code-reviewer = 읽기+Bash(pytest·Write/Edit 제외·규율 수준). 콤마-구분 단일 인자로 전달.
_CLAUDE_TOOLS_WRITE = "Read,Glob,Grep,Bash,Write,Edit"
_CLAUDE_TOOLS_RESEARCHER = "Read,Glob,Grep"
_CLAUDE_TOOLS_REVIEWER = "Read,Glob,Grep,Bash"

# subprocess env allowlist(§4.7) — PM 세션 환경을 통째 상속시키지 않고 최소 키만 전달(타 크리덴셜
# 미상속). base + LC_* 접두 + 하네스별 인증 키. **T-0449 실측으로 조정** 가능하게 상수로 둔다.
_ENV_ALLOWLIST_BASE: tuple[str, ...] = (
    "PATH", "HOME", "LANG", "TERM", "USER", "LOGNAME", "TMPDIR",
)
_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_",)
# 하네스별 인증/구동 필수 env(§4.7 — 하네스-필수 마커만 명시 통과). 실 API key 는 각 하네스
# config/auth 파일(HOME 앵커 격리 홈)로 흐르므로 여기엔 경로/토글 키 위주로 최소 둔다.
# **T-0449 실측(3방향 완주 확인)**: 세 하네스 모두 **HOME 기반 파일 auth**(~/.codex·~/.claude·opencode
# config)로 완주했다 — OPENAI/ANTHROPIC_API_KEY env 는 부재해도 무방(파일 auth 경로). load-bearing 키 =
# base 의 HOME + opencode 의 OPENCODE_CONFIG_DIR(ollama provider config 위치). API-key 항목은 env-auth
# adopter 용 보험이라 유지(존재 시만 통과·과잉 아님). 이 allowlist 로 충분(키 추가/축소 불요).
_HARNESS_AUTH_ENV: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_HOME", "CODEX_SANDBOX_NETWORK_DISABLED", "OPENAI_API_KEY"),
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"),
    "opencode": ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"),
}

# role preamble(§4.3·§9) — 최소 4개(정체성 1줄 + 금지사항 + 결과 보고). identity 는 codex `exec`
# `--agent` 부재로 prompt 합성해야 하므로 엔진이 harness-중립 최소본을 소유한다. T-0447 이 카드/문구를
# 다듬는다(이 티켓은 얇은 계약만·과설계 금지). 합성 = preamble + "\n\n" + prompt-file 내용.
_PROHIBITION = (
    "금지: commit/push/force/reset/rm 등 git 비가역 조작·board 조작·어댑터 디렉토리"
    "(.claude/.codex/.opencode) 수정을 하지 마라(PM 이 결과 회수 후 담당). 결과는 최종 텍스트로 보고하라."
)
ROLE_PREAMBLES: dict[str, str] = {
    "developer":
        "너는 이 프로젝트의 developer 서브에이전트다 — 단일 작업을 구현하고 테스트까지 낸다.\n"
        + _PROHIBITION,
    "researcher":
        "너는 이 프로젝트의 researcher 서브에이전트다 — 조사·분석만 하고 코드를 수정하지 않는다.\n"
        + _PROHIBITION,
    "architect":
        "너는 이 프로젝트의 architect 서브에이전트다 — 설계 초안을 낸다(발행은 PM/사용자 게이트).\n"
        + _PROHIBITION,
    "code-reviewer":
        "너는 이 프로젝트의 code-reviewer 서브에이전트다 — 변경을 검토하고 테스트를 실행해 판정한다"
        "(코드를 수정하지 않는다).\n" + _PROHIBITION,
}


class DelegateError(Exception):
    """config 해소·검증·containment·재앵커 등의 fail-loud 오류 (main 이 rc=1 로 변환)."""


# ── 형제 모듈 deep-import seam (pm_import._load_watchdog 관례·PYTHONPATH 무의존) ─────

def _load_external_review():
    """엔진 external_review 를 importlib 로 직접 로드 — local_config·denylist·PM 홈 재앵커 판정
    (`_pm_home_reanchor`·`_matching_denylist_pattern`)을 복붙 없이 재사용(형제 `.project_manager/tools/`)."""
    path = Path(__file__).resolve().parent / "external_review.py"
    spec = importlib.util.spec_from_file_location("external_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_relay():
    """엔진 pm_relay 를 importlib 로 직접 로드 — 3-하네스 파서(parse_stream_json·parse_codex_json·
    parse_opencode_json)·첫-이벤트 워치독·프로세스그룹 kill 을 재사용(T-0336 deep-import seam 동형)."""
    path = Path(__file__).resolve().parent / "pm_relay.py"
    spec = importlib.util.spec_from_file_location("pm_relay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_delegate_scope():
    """엔진 delegate_scope 를 importlib 로 직접 로드 — 위임 전·후 worktree 상태 비교 판정을
    재사용(T-0462·형제 `.project_manager/tools/`·_load_relay 동형)."""
    path = Path(__file__).resolve().parent / "delegate_scope.py"
    spec = importlib.util.spec_from_file_location("delegate_scope", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 설정 ──────────────────────────────────────────────────────────────────

def local_config() -> dict[str, str]:
    """per-clone local.conf 를 KEY=value 로 읽는다(external_review.local_config 재사용).

    독립 주석 라인(`#` 시작)만 처리하고 값 안의 `#` 은 제거하지 않는다 — `delegate.*` 값은 inline
    주석 금지(독립 주석 라인만·§3.2). REPO 를 호출 시점 읽어 테스트 monkeypatch 를 추종한다."""
    er = _load_external_review()
    er.REPO = REPO
    er.LOCAL_CONF = REPO / ".project_manager" / "local.conf"
    return er.local_config()


def _is_enabled(conf: dict[str, str]) -> bool:
    return conf.get(DELEGATE_ENABLED_KEY, "false").strip().lower() in ("true", "1", "yes", "on")


# ── config 해소 (원자 tuple·§3.2) ────────────────────────────────────────────

def _validate_harness(harness: str) -> str:
    if harness not in HARNESS_CHOICES:
        raise DelegateError(
            f"미지원 harness {harness!r} — 지원: {', '.join(HARNESS_CHOICES)}. "
            "조용한 폴백 금지(명시 등록 요구·ADR-0070 D5)."
        )
    return harness


def _validate_reasoning(harness: str, reasoning: str | None) -> str | None:
    """reasoning 값을 드라이버별 허용집합으로 검증(§6). 미지정=None(플래그 생략). 허용집합 밖이거나
    capability 미확정(claude/opencode·T-0449 전)이면 fail-loud — 조용한 무시/자동 강등 금지."""
    if reasoning is None or not reasoning.strip():
        return None
    reasoning = reasoning.strip()
    allowed = _REASONING_ALLOWED.get(harness, frozenset())
    if reasoning not in allowed:
        known = ", ".join(sorted(allowed)) if allowed else "(미확정·T-0449 라이브 실측 전)"
        raise DelegateError(
            f"reasoning {reasoning!r} 은 {harness} 드라이버 허용집합 {known} 밖 — "
            "조용한 무시/강등 금지·명시 설정을 요구한다(§6)."
        )
    return reasoning


def resolve_delegate(
    conf: dict[str, str],
    role: str,
    tier: str,
    cli_harness: str | None,
    cli_model: str | None,
    cli_reasoning: str | None,
) -> tuple[str, str, str | None]:
    """(harness, model, reasoning) 원자 tuple 을 해소한다(§3.2 단일 알고리즘).

    CLI 완전지정(--harness AND --model)이면 설정 미참조(원자 override). 아니면 티어 키 세트를 통째로
    읽는다(`delegate.<role>[.<tier>].{harness,model,reasoning}` — 혼합 상속 금지). harness/model 부재면
    fail-loud(hard 미설정=normal 강등 금지·normal 미설정=조용한 claude 폴백 금지). CLI 부분 override
    (--harness 만/--model 만)는 호출 전 usage error 로 걸러진 전제(여기선 방어적 재검).
    """
    if cli_harness or cli_model:
        if not (cli_harness and cli_model):
            raise DelegateError("--harness 와 --model 은 동반 필수(부분 override 금지·원자 tuple).")
        harness = _validate_harness(cli_harness)
        reasoning = _validate_reasoning(harness, cli_reasoning)
        return harness, cli_model, reasoning

    key = f"delegate.{role}" + (".hard" if tier == "hard" else "")
    harness = (conf.get(f"{key}.harness") or "").strip()
    model = (conf.get(f"{key}.model") or "").strip()
    reasoning = conf.get(f"{key}.reasoning")

    if not harness or not model:
        if tier == "hard":
            raise DelegateError(
                f"hard 프로필 미설정({key}.harness/.model) — normal 강등 금지·명시 설정을 요구한다"
                "(§3.2). local.conf 에 hard 티어 세트를 통째로 설정하라."
            )
        raise DelegateError(
            f"역할 매핑 미설정({key}.harness/.model) — 조용한 폴백 금지(ADR-0070 D5·§3.2). "
            "local.conf 에 delegate.<role>.harness/.model 을 설정하라."
        )
    harness = _validate_harness(harness)
    reasoning = _validate_reasoning(harness, reasoning)
    return harness, model, reasoning


def resolve_fallback(
    conf: dict[str, str],
    role: str,
    tier: str,
) -> tuple[str, str, str | None] | None:
    """명시된 1단 폴백 tuple 을 해소한다.

    기존 역할 매핑과 동형인
    `delegate.<role>[.hard].fallback.{harness,model,reasoning}` 세트를 통째로 읽는다. 세 키가 모두
    없으면 폴백 미설정(None)이고, 하나라도 있으면 harness/model 완전 세트를 요구한다. hard 는 normal
    폴백을 상속하지 않는다 — 티어 혼합 상속은 주 매핑과 똑같이 금지한다. 엔진 기본 폴백은 없으며,
    미설정은 기존 fail-loud 를 보존한다(ADR-0070 D5).
    """
    key = f"delegate.{role}" + (".hard" if tier == "hard" else "") + ".fallback"
    harness = (conf.get(f"{key}.harness") or "").strip()
    model = (conf.get(f"{key}.model") or "").strip()
    reasoning_raw = conf.get(f"{key}.reasoning")
    configured = bool(harness or model or (reasoning_raw and reasoning_raw.strip()))
    if not configured:
        return None
    if not harness or not model:
        raise DelegateError(
            f"폴백 매핑 불완전({key}.harness/.model) — 폴백은 원자 tuple 로 명시해야 한다. "
            "부분 설정/조용한 기본값은 허용하지 않는다."
        )
    harness = _validate_harness(harness)
    reasoning = _validate_reasoning(harness, reasoning_raw)
    return harness, model, reasoning


# ── 3 드라이버 argv 빌더 (§3.3·pm_import._build_runner_argv 확장형) ─────────────────

def _perm_axis(role: str) -> str:
    """역할 → 권한축('write' | 'read')."""
    return "write" if role in WRITE_ROLES else "read"


def _claude_tools(role: str) -> str:
    if role in WRITE_ROLES:
        return _CLAUDE_TOOLS_WRITE
    if role == "researcher":
        return _CLAUDE_TOOLS_RESEARCHER
    return _CLAUDE_TOOLS_REVIEWER  # code-reviewer — 읽기 + Bash(pytest)


def build_codex_argv(model: str, reasoning: str | None, role: str, cwd: str) -> list[str]:
    """codex argv(§3.3·§5). `-a never -s <mode>` 는 exec **앞** 전역 옵션(exec 뒤는 rc=2·0.145.0 실측).

    프롬프트는 stdin 주입(external_review 동형·argv positional 아님). cwd 는 `-C` 로 핀."""
    mode = _CODEX_SANDBOX[_perm_axis(role)]
    argv = ["codex", "-a", "never", "-s", mode, "exec", "--json", "--skip-git-repo-check"]
    argv += ["-C", str(cwd)]
    if model:
        argv += ["-m", model]
    if reasoning:
        # T-0449 실측(0.145.0): `-c model_reasoning_effort=<r>` 는 `-a`/`-s`(exec 뒤=rc=2)와 달리
        # exec **앞/뒤 모두 rc=0 수용**된다(xhigh 로 전/후 위치 각 1회 실측). 그래서 exec 뒤 이 위치를
        # 유지한다(옮길 필요 없음). reasoning 미지정 시 무영향.
        argv += ["-c", f"model_reasoning_effort={reasoning}"]
    return argv


def build_claude_argv(model: str, reasoning: str | None, role: str) -> list[str]:
    """claude argv(§3.3·§3.5·§5). 프롬프트 stdin 주입·cwd 존중(플래그 불요). `--tools` 로 역할별 가용
    도구 제한, write 역할은 `--permission-mode acceptEdits` 로 무프롬프트 완주."""
    argv = ["claude", "-p", "--output-format", "json", "--model", model,
            "--tools", _claude_tools(role)]
    if reasoning:
        argv += ["--effort", reasoning]
    if role in WRITE_ROLES:
        argv += ["--permission-mode", "acceptEdits"]
    return argv


def build_opencode_argv(
    model: str, reasoning: str | None, role: str, cwd: str, prompt_file: str,
) -> list[str]:
    """opencode argv(§3.3·D2). 프롬프트는 `--file`(실존 인터페이스·길이/ps 노출 회피)·cwd 는 `--dir`
    로 핀(opencode 는 subprocess cwd 무시). `--agent build|plan` 으로 권한 강제."""
    agent = _OPENCODE_AGENT[_perm_axis(role)]
    # message positional 필수(비어있으면 rc=1·실측) — `run` 뒤에 고정 안내 message 를 둔다.
    argv = ["opencode", "run", _OPENCODE_ATTACHED_MSG, "--file", str(prompt_file),
            "--agent", agent, "--format", "json", "--dir", str(cwd)]
    if model:
        argv += ["-m", model]
    if reasoning:
        argv += ["--variant", reasoning]
    return argv


# ── 시크릿 통제 (§4.7) ──────────────────────────────────────────────────────
#
# 프롬프트 스캔은 **파일 경로/이름 + 시크릿 값**만 겨냥한다(T-0472 — 문맥 무시 substring 판정 폐기).
# external_review 의 denylist(`*token*`·`*secret*` …)는 원래 *파일 경로* 필터라, 그 substring glob 을
# 산문·식별자에 그대로 대면 정상 conf 키(`ctx_window_tokens_opencode`)·변수명·"토큰 수" 서술이 전부
# 걸린다(PM 12차 실측 — 위임 발사가 차단됐고 우회는 키명을 풀어 쓰는 것뿐이었다). 그래서 판정을
# **양성매칭 2축**으로 바꾼다(T-0465 파서 양성매칭 전환 동형):
#   ⓐ 경로축 — 토큰이 *경로 형태*(구분자·확장자·닷파일) 또는 알려진 시크릿 파일명일 때만 denylist 적용
#             (그것도 **파일 이름 앵커** + 이름-substring 패턴은 시크릿 데이터 확장자일 때만).
#   ⓑ 값축   — 알려진 시크릿 값 prefix(ghp_·AKIA…)·PEM 개인키 블록·URL 내장 자격증명·시크릿 키명
#             할당의 고엔트로피 값.
# **미탐 방향 금지**(T-0466 동형): 실 시크릿(파일 경로·값)은 ⓐⓑ 로 계속 차단된다. 완화되는 건 ①
# "경로도 값도 아닌 식별자/산문"과 ② 이름-substring 패턴만 걸린 **소스/문서 파일**
# (`tests/test_adapter_token_substitution.py`)이다 — ②는 파일 *언급*일 뿐이라 ③ 합성 프롬프트
# 스캔에서만 완화하고, 파일 내용이 통째로 전송되는 ④ prompt-file 게이트
# (`_prompt_file_denylist_pattern`)에는 적용하지 않는다(확장자 무관 이름 앵커·내부 리뷰 SF1 —
# 단 프롬프트 문서 확장자 `.md`/`.markdown`/`.rst` 는 면제, 내용은 ③ 이 다시 훑는다).
# ⓐⓑ 의 조임/완화 폭은 실 코퍼스(PM 문서 167 + 제품 문서 181 + 엔진/테스트 소스 = 512 파일·15만 줄)
# **라인 단위** 와 통제 케이스로 **양방향 실측**해 확정했다 — 오탐(`part.tokens.input`·
# ``key/token(다른`` 류 산문 조각·`auth_url=https://…/oauth/token`·glob 인용 `"*.key"`·한국어 산문
# 슬래시 조각) 0 유지 + 미탐(조사 밀착 `~/.aws/credentials를`·비ASCII 경로 성분 `/path/to/사용자/.env`·
# URL 안 크리덴셜·`.properties`·JSON/camelCase 키) 폐쇄. **경로축 면제가 값축을 끄지 않는다**는 게
# URL 처리의 불변식이다(리뷰 R2 — 면제는 엔드포인트 오탐용이지 크리덴셜 눈감기가 아니다).

# ⓐ 경로 형태 판정 — 구분자 포함 / 확장자 보유 / 닷파일. 확장자 길이 상한은 **실 확장자 집합에
# 맞춘다**: 8자 상한은 `.properties`(10자)를 확장자로 못 봐 `client_secret.properties`·
# `access_token.properties` 가 통째로 통과했다(외부 리뷰 R2·구 denylist 는 차단하던 것).
_PATH_SEPARATOR_RE = re.compile(r"[/\\]")
_MAX_FILE_EXTENSION_CHARS = 12  # `properties`(10) + 여유 · 산문 조각을 확장자로 오인하지 않는 선
_FILE_EXTENSION_RE = re.compile(rf"\.[A-Za-z0-9]{{1,{_MAX_FILE_EXTENSION_CHARS}}}$")
_DOTFILE_RE = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]*$")
# 경로로 볼 수 있는 문자만으로 이뤄진 **파일 이름**인가 — 한글·백틱·괄호·화살표가 섞인 산문 조각
# (``key/token(다른``·`` `json`→token/input/output/cost ``)의 이름 성분은 파일 이름이 아니다(실 코퍼스
# 오탐 다수). 판정 대상이 토큰 전문이 아니라 **basename** 인 이유(T-0472 fix·리뷰 실측): 전문에 대면
# 경로 성분 하나만 비ASCII/`@`/`$`/`{}` 여도 전 축이 skip 돼 옛 판정이 잡던 실 시크릿 경로가 통과했다
# (`/path/to/사용자/.env`·`node_modules/@scope/pkg/.env`·`${HOME}/.aws/credentials`·미탐 회귀).
_STRICT_PATH_NAME_RE = re.compile(r"^[A-Za-z0-9_~.-]+$")
# 토큰 **안** 에 남은 산문/마크다운 마커 — 이름은 깨끗해도 앞 성분이 문장 조각이면 경로가 아니다
# (실 코퍼스 오탐 `부재[insteadOf/credential` — basename `credential` 만 보면 시크릿처럼 보인다).
# 비ASCII(한국어 디렉토리)·`$`·`{}`·`@` 는 실 경로에 나오므로 마커에 넣지 않는다
# (`/path/to/사용자/.env`·`${HOME}/.aws/credentials`·`node_modules/@scope/pkg/.env`).
_PROSE_MARKER_RE = re.compile(r"""[`"'()\[\]<>,;!?*|]""")
# 토큰 양끝에서 벗기는 문장 부호/마크다운 wrapper. `*` 는 **여기 넣지 않는다** — 끝의 `*` 를 무조건
# 벗기면 glob 패턴 인용(`"*.key"`·`*.pem`·denylist 를 논하는 프롬프트)이 `.key`·`.pem` 경로로 둔갑해
# 차단된다(라인 단위 코퍼스 실측 오탐). 강조는 대칭형일 때만 `_MARKDOWN_EMPHASIS_RE` 로 벗긴다.
_CANDIDATE_STRIP_CHARS = "\"'`()[]{}<>,;!? "
# 마크다운 강조 대칭 wrapper — `**~/.aws/credentials**` 처럼 **앞뒤 모두** `*` 일 때만 안쪽을 취한다.
_MARKDOWN_EMPHASIS_RE = re.compile(r"^\*+(?P<inner>[^*]+)\*+$")
# 토큰 끝의 비ASCII 런 = 한국어 조사 밀착(`~/.aws/credentials를`·`/opt/앱/id_rsa를`). 경로 이름은
# ASCII denylist 로만 판정하므로 조사를 떼야 옛 판정이 잡던 경로가 계속 잡힌다(미탐 폐쇄).
_TRAILING_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]+$")
# 조사를 뗀 뒤 드러나는 **닫는** wrapper 만 추가로 벗긴다(`` `.env`를 `` → `.env`). **여는** 괄호는
# 남긴다 — 끝 구두점을 전부 벗기면 ``key/token(다른`` 이 `key/token` 이 돼 산문 오탐이 재발한다
# (1차분 실 코퍼스 실측 근거).
_CLOSING_WRAPPER_CHARS = "\"'`)]}>"
# 경로 토큰의 **첫 성분**은 ASCII 경로 이름이어야 한다(절대경로면 비어 있음) — 중간 성분의 비ASCII 는
# 허용하되(`/path/to/사용자/.env`) 한국어 산문이 슬래시로 이어진 조각
# (``빈/leading-dash/credential-in-URL/비허용`` — 라인 단위 코퍼스 실측 오탐)은 경로로 보지 않는다.
# `$`·`{}`·`@` 는 실 경로 관용형(`${HOME}/…`·`node_modules/@scope/…`)이라 허용한다.
_PATH_ROOT_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_~.${}@-]*$")
# 후보 조각 경계 — 괄호/대괄호(마크다운 링크 `[label](path)`·산문 삽입구 `파일(~/.aws/credentials)을`)
# 에서 조각을 나눠 **각 조각을 따로 판정**한다(리뷰 R3). 마커를 만나면 토큰을 통째로 버리던 방식은
# wrapper 안 경로를 통째로 놓쳤다. 조각 분리가 옛 산문 오탐(``key/token(다른``)을 되살리지 않는 건
# `_ANCHORED_PATH_RE`(무확장자 상대경로 배제) 덕이다 — 둘은 한 쌍으로 봐야 한다.
_CANDIDATE_FRAGMENT_RE = re.compile(r"[()\[\]]+")
# 경로로 인정할 **앵커** — 절대(`/`·`\`)·홈(`~`)·상대 명시(`./`·`../`)·env 전개(`$`·`${}`)·닷파일.
# 무확장자 상대경로(``key/token``·``char/token``)는 산문 조각과 기계적으로 못 가르므로 앵커나 확장자
# (또는 닷파일 basename) 중 하나는 있어야 경로축을 태운다(라인 코퍼스 실측 — 이 요건이 없으면 조각
# 분리가 산문 오탐을 되살린다).
_ANCHORED_PATH_RE = re.compile(r"^(?:[/\\~$]|\.{1,2}[/\\])")

# 경로 형태(확장자)가 없어도 그 자체로 시크릿 파일인 이름 — external_review denylist 는 확장자/
# substring 위주라 이 계열(ssh 개인키·rc 파일)을 안 담는다. 프롬프트 스캔 전용 보강(미탐 폐쇄).
# `.npmrc`/`.netrc` 는 정확 파일명으로 여기 둔다 — `_SECRET_DATA_EXTENSIONS` 의 동명 확장자만으로는
# denylist 패턴이 하나도 안 걸려 도달 불가였다(외부 리뷰 MF4·데드 상수).
_SECRET_FILENAME_PATTERNS: tuple[str, ...] = (
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "*.ppk", "*.jks", "*.keystore", "*.p8",
    ".npmrc", ".netrc",
)

# 경로 *성분* 이 이 이름과 **정확히** 같으면 그 아래 파일은 이름과 무관하게 시크릿으로 본다 —
# `/run/secrets/db_password`·`secrets/config.json` 은 basename 만 봐서는 안 걸렸다(외부 리뷰 R3).
# **정확-세그먼트 매칭이지 substring 이 아니다**: pytest tmp 디렉토리(`…/test_secret_scan0/prompt.md`)의
# "secret" 은 성분 *안* 의 substring 이라 걸리지 않는다(fix1 이 없앤 ④ 조상-디렉토리 오탐 재발 금지).
_SECRET_DIRECTORY_NAMES: frozenset[str] = frozenset({
    "secrets", "credentials", ".aws", ".ssh", ".gnupg", ".password-store",
})

# 이름-substring 패턴(`*token*`·`*secret*`·`*credential*`)으로 시크릿 파일을 지목할 때의 확장자 조건 —
# 시크릿이 실제로 담기는 데이터/설정 확장자이거나 **무확장자**(`~/.aws/credentials`·`.git-credentials`)
# 일 때만 인정한다. 소스/문서 확장자(`test_adapter_token_substitution.py`·`secret-scan.md`)는 *주제가*
# 시크릿일 뿐 시크릿 파일이 아니다 — 개발 프롬프트의 최대 오탐원이었다.
# ④ prompt-file 게이트에서 이름-substring 패턴을 면제하는 프롬프트 문서 확장자 — 위임 프롬프트는
# 원래 마크다운 문서고, 티켓 주제어(`token`·`secret`)가 파일명에 들어가는 게 정상이다(리뷰 R2).
_PROMPT_DOC_EXTENSIONS: frozenset[str] = frozenset({"md", "markdown", "rst"})
_SECRET_DATA_EXTENSIONS: frozenset[str] = frozenset({
    "env", "json", "yaml", "yml", "ini", "conf", "cfg", "toml", "properties",
    "txt", "xml", "csv", "tsv", "pem", "key", "p12", "pfx", "jks", "keystore",
    "pickle", "pkl", "enc", "gpg", "asc", "netrc", "npmrc", "creds", "secret",
})

# ⓑ 값축 — 알려진 크리덴셜 값 prefix(발급기관 형식). 토큰 단독으로도 발화한다(키명 불요).
_SECRET_VALUE_PREFIX_RE = re.compile(
    r"^(?:"
    r"gh[pousr]_[A-Za-z0-9]{16,}"           # GitHub PAT/OAuth/user/server/refresh
    r"|github_pat_[A-Za-z0-9_]{20,}"        # GitHub fine-grained PAT
    r"|glpat-[A-Za-z0-9_-]{16,}"            # GitLab PAT
    r"|sk-(?:ant-)?[A-Za-z0-9_-]{16,}"      # OpenAI/Anthropic 류 API key
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"        # Slack
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"           # AWS access key id
    r"|AIza[A-Za-z0-9_-]{20,}"              # Google API key
    r"|ya29\.[A-Za-z0-9_-]{20,}"            # Google OAuth access token
    r"|npm_[A-Za-z0-9]{20,}"                # npm token
    r"|dop_v1_[A-Za-z0-9]{32,}"             # DigitalOcean
    r")$"
)
_PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")

# 할당 좌변이 시크릿 키명인가 — **성분 경계**로 판정한다(substring 아님). `GITHUB_TOKEN` 은 걸리고
# `ctx_window_tokens_opencode`("tokens" — 경계 뒤가 s)는 안 걸린다. 광범위한 `auth` 단독은 제외
# (auth_url·authors 오탐) — `auth_token`·`authorization`·`bearer` 처럼 크리덴셜 확정형만.
_SECRET_KEY_WORDS = (
    r"token|secret|credential|password|passwd|passphrase"
    r"|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key"
    r"|auth[_-]?token|authorization|bearer"
)
_SECRET_KEY_NAME_RE = re.compile(
    rf"(?:^|[^A-Za-z0-9])(?:{_SECRET_KEY_WORDS})(?:[^A-Za-z0-9]|$)",
    re.IGNORECASE,
)
# camelCase 키(`accessToken`·`dbPassword`·`clientSecret`·`XSRFToken`) — 위 성분 경계는 `_`/`-` 만 보므로
# hump 경계를 못 읽어 통째로 미탐이었다(외부 리뷰 R2). **대소문자 민감**으로 따로 둔다: IGNORECASE 를
# hump 규칙에 섞으면 뒤 경계 `(?![a-z])` 가 소문자에도 걸려 `ctx_window_tokens_opencode`(=T-0472 원인
# 오탐)가 되살아난다. 앞 경계에 대문자를 포함하는 건 두문자어 접두(`XSRFToken`·`APIToken`·`AWSSecret`·
# `JWTSecret`)를 잡기 위함이고(내부 리뷰 R3 실측), 뒤에 소문자가 이어지는 복수/합성형(`accessTokens`·
# `tokenizerName`)은 제외한다 — `tokens` 배제와 같은 규칙.
_SECRET_KEY_CAMEL_RE = re.compile(
    r"(?<=[A-Za-z0-9])(?:Token|Secret|Credential|Password|Passwd|Passphrase"
    r"|Authorization|Bearer)(?![a-z])"
)
# `KEY=value` / `KEY: value`(공백 허용) 할당 추출 — 값축 문맥 판정의 입력. 좌변은 JSON/YAML 의
# **따옴표 키**(`"token": "…"`)도 받는다(외부 리뷰 R2 — 따옴표 때문에 키를 못 읽어 통째로 미탐이었다).
# **따옴표로 감싼 값은 닫는 따옴표까지** 통째로 잡는다(공백 포함) — 옛 정규식은 값을 공백에서 끊어
# `db_password="A1pha Bravo C3arlie Delta"` 가 `A1pha`(길이 미달)로 잘려 미탐이었다(외부 리뷰 MF3).
_ASSIGNMENT_RE = re.compile(
    r"""["'`]?(?P<key>[A-Za-z][A-Za-z0-9_.\-]{2,})["'`]?\s*[:=]\s*"""
    r"""(?:"(?P<dquoted>[^"\n]+)"|'(?P<squoted>[^'\n]+)'|`(?P<bquoted>[^`\n]+)`"""
    r"|(?P<bare>[^\s\"'`,;)\]}]+))"
)
_ASSIGNMENT_VALUE_GROUPS: tuple[str, ...] = ("dquoted", "squoted", "bquoted", "bare")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
# 텍스트 *안* 의 URL(할당 우변 포함) — 경로축 비대상 판정용. `auth_url=https://idp.example.com/oauth/
# token` 은 `:` 분리 후 남는 조각의 basename(`token`·무확장자)이 `*token*` 에 걸려 오탐이었다(외부
# 리뷰 MF1) — URL 여부를 **분리 전 원문**에서 보고 **경로축에서만** 뺀다(값축은 원문 그대로 본다).
_URL_IN_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
# URL userinfo(`https://user:pass@host/…`·`https://ghp_…@github.com/o/r.git`) — 전송 텍스트에 크리덴셜이
# 실려 있으므로 경로축 면제 대상이 아니라 값축 **차단** 대상이다. `user:pass` 콜론형만 보면 실전에서
# 가장 흔한 **username-only PAT** 형을 놓친다(외부 리뷰 R2).
_URL_USERINFO_RE = re.compile(r"://(?P<userinfo>[^/\s@]+)@")
# URL authority(호스트[:포트]) — 경로/쿼리 판정에서 잘라낸다. 경로가 없어도(`https://host?file=.env`)
# 쿼리는 검사해야 하므로 `/` 유무로 조기 반환하지 않는다(외부 리뷰 R4).
_URL_AUTHORITY_RE = re.compile(r"^[^/?#]*")
# URL 쿼리/fragment 파라미터 — 값 마스킹(발췌)과 값 후보 추출(경로축)에 공유한다.
_URL_PARAMETER_RE = re.compile(
    r"(?P<lead>[?&;#])(?P<key>[^=&;#?]*)(?P<equals>=?)(?P<value>[^&;#?]*)")
# 값축 전용 세분 토큰화 — URL 안(userinfo·경로 세그먼트·쿼리 파라미터)에 실린 크리덴셜을 꺼낸다.
# 경로축 토큰화(`[=:]` 분리)로는 `https://…/?access_token=ghp_…` 의 값이 통째 조각에 묻힌다.
# 괄호/대괄호도 경계다 — 경로축에만 조각 분리를 넣으면 한국어 관용 표기(`토큰(ghp_…)을`·`키[ghp_…]를`)
# 에서 값축만 못 잡는 비대칭이 생긴다(내부 리뷰 R4). 발급기관 prefix 판정이라 세분화는 오탐을 안 낳는다.
_VALUE_CANDIDATE_SPLIT_RE = re.compile(r"[=:/\\?&@#|()\[\]]+")

# 값이 크리덴셜 형태인지의 문턱 — 짧은 값(`180000`)·자연어 식별자(`ctx_window_tokens_opencode`)를
# 배제하고 랜덤 시크릿만 남긴다. 엔트로피는 bits/char(Shannon). 임계 3.0 근거: 16자 랜덤
# 영숫자(≈4.0)·발급기관 토큰(≈4.5)은 넉넉히 넘고, 사람이 쓴 영문 식별자/한국어 산문 조각(≈2.5~3.5)
# 중 *구성 조건까지 통과한 것* 만 남기는 하한 — 즉 엔트로피는 단독 판정이 아니라 구성 조건
# (`_has_secret_value_charset`)과 AND 로 걸린다.
_MIN_SECRET_VALUE_LENGTH = 16
_MIN_SECRET_VALUE_ENTROPY = 3.0
# 무숫자 값 완화(외부 리뷰 MF3)의 조임 — 단어 구분자 부재 + 대소문자 혼합 + 문자 클래스 **교대 밀도**.
# 엔트로피는 여기서 무력하다(랜덤 `XkwPqrLmZvTbNhGf`·CamelCase `ValueFormatKnownPrefix` 둘 다 4.0).
# 임계 0.3 근거(실측): 랜덤 무숫자 비밀번호 2000 샘플 중 97%가 ≥0.3(중앙값 0.5)이고, 대문자 하나로
# 시작하는 산문형 값(`Thisisaverylongnote` ≈0.05)은 배제된다. **완전 분리는 아니다** — 다중 hump
# CamelCase 식별자(≈0.33)는 랜덤 하위 꼬리와 겹쳐 차단 쪽(오탐 방향)에 남는다. 이 완화가 발화하려면
# 좌변이 시크릿 키명이어야 하고, 그런 할당은 실 코퍼스(PM 문서 167 + 제품 문서 181 + 엔진/테스트
# 소스)에 0건이라 오탐 재발 없음을 실측으로 확인했다.
_WORD_SEPARATOR_RE = re.compile(r"[\s_\-./\\]")
_MIN_CHAR_CLASS_ALTERNATION_RATIO = 0.3

# 차단 메시지 발췌 — 값은 앞 4자만 남기고 마스킹(로그에 크리덴셜 미잔존), 길이 상한은 발췌 가독성.
_EXCERPT_MAX_CHARS = 80
_VALUE_MASK_HEAD_CHARS = 4
_SECRET_RULE_VALUE_PREFIX = "값-형태:알려진 시크릿 prefix"
_SECRET_RULE_PEM = "값-형태:PEM 개인키 블록"
_SECRET_RULE_ASSIGNMENT = "값-형태:시크릿 키명 할당(고엔트로피 값)"
_SECRET_RULE_URL_CREDENTIALS = "값-형태:URL 내장 자격증명"
_SECRET_RULE_DIRECTORY = "경로:시크릿 디렉토리 성분"
_SECRET_AXIS_PATH = "경로"
_SECRET_AXIS_VALUE = "값"


class PromptSecretHit(NamedTuple):
    """프롬프트 시크릿 스캔 매칭 — 발췌(값은 마스킹됨)·걸린 판정 이름·판정축(`경로`/`값`).

    발췌를 담는 이유(T-0472): 옛 반환은 패턴명만 노출해 *프롬프트의 어느 텍스트가* 걸렸는지 추측이
    필요했다(관측 가능성 결함). 경로/파일명은 그대로 보여야 고칠 수 있고, 크리덴셜 값은
    `_mask_secret_value` 로 마스킹해 stderr/로그에 남기지 않는다."""

    excerpt: str
    pattern: str
    axis: str


def _secret_path_candidates(raw: str) -> list[str]:
    """외부 전송 텍스트 토큰에서 경로/값 후보를 추출한다(시크릿 스캔용·강화 토큰화).

    공백-토큰 하나를 (a) `=`/`:` 할당문 분리(예 `path=.env`·`key:secret.pem`) → 조각별로, (b) 양끝
    구두점/마크다운 강조 트리밍(마침표는 leading `.env` 보존 위해 trailing 만·`foo.pem.`→`foo.pem`·
    `**~/.aws/credentials**`→`~/.aws/credentials`), (c) 끝 비ASCII 런 제거(한국어 조사 밀착
    `~/.aws/credentials를`→`~/.aws/credentials`) 후 후보로 낸다. 각 후보는 원문 + 소문자 정규화 2형을
    담아 대소문자 표기(`.ENV`)도 잡는다(원문형을 먼저 담아 대소문자 민감한 값 prefix(`AKIA…`) 판정이
    살아있다). 괄호/대괄호(`[설정](~/.aws/credentials)`·`파일(~/.aws/credentials)을`)는 조각으로 나눠
    **각각 재판정**한다(리뷰 R3) — 옛 방식은 마커가 보이면 토큰을 통째로 버려 wrapper 안 경로를 놓쳤다.
    조각 분리로 산문 오탐(``key/token(다른``)이 되살아나지 않는 건 무확장자 상대경로를 배제하는
    `_ANCHORED_PATH_RE` 요건 덕이다(`_matching_secret_path_pattern` 참조·둘은 한 쌍)."""
    candidates: list[str] = []
    for chunk in _CANDIDATE_FRAGMENT_RE.split(raw):
        for piece in re.split(r"[=:]", chunk):
            token = _trim_candidate(piece)
            if not token:
                continue
            candidates.append(token)
            lowered = token.lower()
            if lowered != token:
                candidates.append(lowered)
    return candidates


def _trim_candidate(piece: str) -> str:
    """후보 조각의 양끝 트리밍(구두점 → 끝 조사 → 조사 뒤 드러난 닫는 wrapper → 대칭 강조) 단일 소스."""
    token = piece.strip().strip(_CANDIDATE_STRIP_CHARS).rstrip(".")
    token = _TRAILING_NON_ASCII_RE.sub("", token)
    token = token.rstrip(_CLOSING_WRAPPER_CHARS).rstrip(".")
    emphasis = _MARKDOWN_EMPHASIS_RE.match(token)
    if emphasis is not None:
        token = emphasis.group("inner").strip(_CANDIDATE_STRIP_CHARS).rstrip(".")
    return token


def _secret_value_candidates(raw: str) -> list[str]:
    """값축(알려진 prefix) 전용 후보 — URL 구분자까지 세분 분리한다(경로축 토큰화보다 잘게).

    `https://ghp_…@github.com/o/r.git`·`…?access_token=ghp_…`·`…/services/xoxb-…` 처럼 URL 안에 실린
    크리덴셜을 꺼내려면 `/`·`?`·`&`·`@` 까지 경계로 봐야 한다. 값축 판정은 발급기관 prefix 정규식이라
    세분화가 오탐을 늘리지 않는다(반대로 경로축은 세분하면 경로가 조각나 못 쓴다)."""
    candidates: list[str] = []
    for piece in _VALUE_CANDIDATE_SPLIT_RE.split(raw):
        token = _trim_candidate(piece)
        if token:
            candidates.append(token)
    return candidates


def _is_path_shaped(token: str) -> bool:
    """토큰이 파일 경로/이름 형태인가 — denylist substring glob(`*token*`)은 여기에만 적용한다.

    경로 구분자 포함(`~/.aws/credentials`)·확장자 보유(`credentials.env`·`foo.pem`)·닷파일(`.env`)
    중 하나면 경로 형태로 본다. `ctx_window_tokens_opencode` 같은 식별자·산문 단어는 셋 다 아니라
    통과한다(T-0472 오탐 근본)."""
    if _PATH_SEPARATOR_RE.search(token):
        return True
    if _DOTFILE_RE.match(token):
        return True
    return bool(_FILE_EXTENSION_RE.search(token))


def _is_name_substring_pattern(pattern: str) -> bool:
    """`*token*` 처럼 *이름 어디든* 걸리는 substring 패턴인가(`*.key` 확장자·`.env` 정확명과 구분)."""
    return pattern.startswith("*") and pattern.endswith("*") and not pattern.startswith("*.")


def _has_secret_data_extension(name: str) -> bool:
    """파일명이 시크릿을 담는 확장자(또는 무확장자)인가 — 이름-substring 판정의 동반 조건."""
    match = _FILE_EXTENSION_RE.search(name)
    if match is None:
        return True  # 무확장자(`credentials`·`.git-credentials`)는 시크릿 파일 관용형
    return match.group(0)[1:].lower() in _SECRET_DATA_EXTENSIONS


_NAME_PATTERN_CACHE_SIZE = 8  # denylist 종류는 기본 + conf 확장(`review_denylist_extra`) 몇 개뿐


@functools.lru_cache(maxsize=_NAME_PATTERN_CACHE_SIZE)
def _name_anchored_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """이름 앵커로 판정할 denylist 패턴(디렉토리 형태 제외) — 토큰마다 재생성하지 않도록 캐시한다.

    입력 tuple 은 호출부가 로드한 denylist(기본 + conf `review_denylist_extra`)라 종류가 몇 개뿐이다."""
    return tuple(p for p in patterns if "/" not in p)


@functools.lru_cache(maxsize=_NAME_PATTERN_CACHE_SIZE)
def _exact_name_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """정확-이름/확장자 패턴만(`.env`·`*.pem`·`*.key` + 알려진 시크릿 파일명) — 이름-substring 제외.

    원격 URL 경로 판정(`_url_path_secret_pattern`)처럼 substring 패턴을 대면 엔드포인트 오탐이 나는
    자리에서 쓴다."""
    return _SECRET_FILENAME_PATTERNS + tuple(
        p for p in _name_anchored_patterns(patterns) if not _is_name_substring_pattern(p))


def _matching_secret_name_pattern(
    name: str, patterns: tuple[str, ...], match: Callable, require_data_ext: bool = True,
) -> str | None:
    """파일 *이름* 이 시크릿 파일 판정에 걸리는 첫 패턴(없으면 None).

    ① 알려진 시크릿 파일명(`id_rsa`·`.npmrc` 류)은 즉시 매칭. ② denylist 패턴 매칭 — 단 이름-substring
    패턴(`*token*`)은 `require_data_ext` 일 때 시크릿 데이터 확장자(또는 무확장자)까지 요구한다.
    디렉토리 형태 패턴(`secrets/` 류)은 이름이 아니라 전체 경로로 판정해야 하므로 여기서 제외한다
    (호출부가 별도 처리).

    `require_data_ext` 를 나누는 이유(내부 리뷰 SF1): 합성 프롬프트 스캔은 파일 *언급* 이라
    `test_adapter_token_substitution.py` 같은 소스/문서를 시크릿으로 보면 오탐이지만, prompt-file
    게이트는 그 파일 **내용이 통째로 전송**되는 지점이라 확장자로 무해를 전제할 수 없다(`secrets.py`·
    `token.sh`·`app_credentials.log`)."""
    pattern = match(name, _SECRET_FILENAME_PATTERNS)
    if pattern is not None:
        return pattern
    pattern = match(name, _name_anchored_patterns(patterns))
    if pattern is None:
        return None
    if require_data_ext and _is_name_substring_pattern(pattern) \
            and not _has_secret_data_extension(name):
        return None
    return pattern


def _matching_secret_path_pattern(
    token: str, patterns: tuple[str, ...], match_fn: Callable | None = None,
) -> str | None:
    """토큰이 시크릿 *파일 경로/이름* 판정에 걸리는 첫 패턴(없으면 None·ⓐ 경로축).

    조임(T-0472): (1) 토큰에 산문/마크다운 마커가 없고 **첫 성분**이 ASCII 경로 이름인가(산문 조각
    배제) → (2) **디렉토리 성분**이 알려진 시크릿 디렉토리면 즉시 발화(`/run/secrets/db_password` —
    이름엔 단서가 없다·정확 세그먼트 매칭) → (3) **파일 이름**(basename)이 경로 문자만으로 됐고 무확장자
    상대경로가 아닌가 → (4) 파일 이름 앵커로 denylist 판정
    (`tests/test_adapter_token_substitution.py` 의 디렉토리/줄기 오탐 배제). (1)의 문자 판정을 토큰
    전문이 아니라 basename+마커로 나눈 이유는 `_STRICT_PATH_NAME_RE`·`_PROSE_MARKER_RE` 주석 참조 —
    전문 strict 판정은 경로 성분 하나의 비ASCII/`@`/`$` 로 전 축을 skip 시켜 실 시크릿 경로를 통과시켰다.
    `match_fn` 은 호출부가 로드해둔 matcher 주입 — 토큰마다 형제 모듈을 재-import 하지 않는다."""
    match = match_fn or _load_external_review()._matching_denylist_pattern
    if _PROSE_MARKER_RE.search(token):
        return None
    normalized = token.replace("\\", "/")
    if not _PATH_ROOT_COMPONENT_RE.match(normalized.split("/", 1)[0]):
        return None
    if _secret_directory_segment(normalized) is not None:
        return _SECRET_RULE_DIRECTORY
    name = PurePosixPath(normalized).name
    if not _STRICT_PATH_NAME_RE.match(name):
        return None
    pattern = _matching_secret_name_pattern(name, patterns, match)
    if pattern is None:
        return None
    # 정확 시크릿 파일명(`deploy/id_rsa`·`keys/id_ed25519`)은 이름 자체가 비모호하므로 앵커/경로 형태
    # 요건을 면제한다(내부 리뷰 R4 — 앵커 요건이 fix2 까지 잡던 이 형태를 막고 있었다).
    if pattern not in _SECRET_FILENAME_PATTERNS:
        if not _is_named_path_shape(normalized, name):
            return None
        if not _is_path_shaped(token):
            return None
    return pattern


def _secret_directory_segment(normalized: str) -> str | None:
    """경로 *성분* 중 알려진 시크릿 디렉토리 이름(정확 일치)을 돌려준다(없으면 None).

    `/run/secrets/db_password`·`secrets/config.json` 처럼 **파일 이름엔 단서가 없고 디렉토리가 말해주는**
    시크릿을 잡는다(외부 리뷰 R3). 마지막 성분(=파일 이름)은 제외한다 — 산문의 맨 단어 `secrets` 까지
    경로로 보면 오탐이다. 매칭은 **정확 세그먼트**라 `…/test_secret_scan0/` 같은 substring 은 안 걸린다."""
    segments = normalized.split("/")
    for segment in segments[:-1]:
        if segment.lower() in _SECRET_DIRECTORY_NAMES:
            return segment
    return None


def _is_named_path_shape(normalized: str, name: str) -> bool:
    """이름 기반 denylist 판정을 태워도 되는 경로 형태인가 — 무확장자 **상대** 경로는 제외.

    `key/token`·`char/token` 같은 조각은 확장자도 앵커도 없어 산문과 기계적으로 못 가른다(라인 코퍼스
    실측 오탐). 앵커(`/`·`~`·`$`·`./`)나 확장자/닷파일이 하나라도 있으면 경로로 인정한다 —
    `~/.aws/credentials`·`/path/to/사용자/.env`·`docs/secret-scan.md` 는 그대로 통과한다."""
    if not _PATH_SEPARATOR_RE.search(normalized):
        return True   # 단일 이름(`credentials.env`·`id_rsa`)은 이름 판정이 전담
    if _ANCHORED_PATH_RE.match(normalized):
        return True
    return _DOTFILE_RE.match(name) is not None or _FILE_EXTENSION_RE.search(name) is not None


def _shannon_entropy(text: str) -> float:
    """문자 분포 Shannon 엔트로피(bits/char) — 랜덤 크리덴셜과 사람이 쓴 식별자를 가른다."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _is_known_secret_value(value: str) -> bool:
    """값이 알려진 크리덴셜 형식인가(`ghp_…`·`AKIA…`·`sk-…`) — 키명 문맥 없이도 확정 시크릿."""
    return _SECRET_VALUE_PREFIX_RE.match(value) is not None


def _char_class(char: str) -> str:
    """문자 클래스(소문자/대문자/숫자/기타) — 랜덤 문자열의 클래스 교대 밀도 계산용."""
    if char.islower():
        return "lower"
    if char.isupper():
        return "upper"
    if char.isdigit():
        return "digit"
    return "other"


def _char_class_alternation_ratio(value: str) -> float:
    """인접 문자쌍 중 클래스가 바뀌는 비율 — 랜덤 시크릿은 높고(중앙 ≈0.5) 산문형 값은 낮다(≈0.05)."""
    if len(value) < 2:
        return 0.0
    changes = sum(1 for prev, cur in zip(value, value[1:])
                  if _char_class(prev) != _char_class(cur))
    return changes / (len(value) - 1)


def _has_secret_value_charset(value: str) -> bool:
    """값의 문자 구성이 크리덴셜형인가 — ① 영문+숫자 혼합, 또는 ② 무숫자 랜덤 대소문자 혼합.

    ① 의 digit 요건은 영문 식별자/산문 오탐(`ctx_window_tokens_opencode`·`enforce_minimum_length`)을
    걸러온 실측 근거가 있어 유지한다. ② 는 그 요건이 무숫자 랜덤 비밀번호(`XkwPqrLmZvTbNhGf`)를
    통과시키던 갭(외부 리뷰 MF3)만 닫는 좁은 완화 — 단어 구분자·단일 케이스·산문형 값을 배제하는
    조임은 `_MIN_CHAR_CLASS_ALTERNATION_RATIO` 주석(임계 근거·잔여 겹침) 참조."""
    if any(c.isalpha() for c in value) and any(c.isdigit() for c in value):
        return True
    if _WORD_SEPARATOR_RE.search(value):
        return False
    if not (any(c.islower() for c in value) and any(c.isupper() for c in value)):
        return False
    return _char_class_alternation_ratio(value) >= _MIN_CHAR_CLASS_ALTERNATION_RATIO


def _looks_like_secret_value(value: str) -> bool:
    """값이 크리덴셜 형태인가 — 알려진 prefix, 또는 (충분 길이 + 크리덴셜형 문자 구성 + 고엔트로피).

    자격증명 없는 URL(문서 링크·엔드포인트)은 제외한다. 숫자만(`180000`)·영문 식별자만
    (`ctx_window_tokens_opencode`)은 문자 구성 조건에서 걸러진다."""
    if _is_known_secret_value(value):
        return True
    if _URL_SCHEME_RE.match(value) and "@" not in value:
        return False
    if len(value) < _MIN_SECRET_VALUE_LENGTH:
        return False
    if not _has_secret_value_charset(value):
        return False
    return _shannon_entropy(value) >= _MIN_SECRET_VALUE_ENTROPY


def _assignment_value(match: re.Match) -> str:
    """할당 매칭에서 값 문자열 — 따옴표로 감싼 값은 닫는 따옴표까지(공백 포함) 통째로."""
    for group in _ASSIGNMENT_VALUE_GROUPS:
        value = match.group(group)
        if value:
            return value
    return ""


def _is_secret_key_name(key: str) -> bool:
    """할당 좌변이 시크릿 키명인가 — 성분 경계(`GITHUB_TOKEN`) 또는 camelCase hump(`accessToken`)."""
    return (_SECRET_KEY_NAME_RE.search(key) is not None
            or _SECRET_KEY_CAMEL_RE.search(key) is not None)


def _is_credential_userinfo(userinfo: str) -> bool:
    """URL userinfo 가 자격증명인가 — `user:pass` 형태이거나, username 단독이라도 값-형태.

    username 단독형(`https://ghp_…@github.com/o/r.git`)이 실전 PAT clone URL 의 기본형이다. 반대로
    평범한 사용자명(`https://username@bitbucket.org/…`)은 값-형태가 아니라 통과한다(오탐 방지)."""
    if ":" in userinfo:
        return True
    return _looks_like_secret_value(userinfo)


def _mask_userinfo(userinfo: str) -> str:
    """URL userinfo 마스킹 — password 는 항상, username 도 값-형태(PAT 등)면 마스킹한다.

    username-only PAT 을 안 가리면 차단 메시지에 크리덴셜 전문이 그대로 남는다(외부 리뷰 R2)."""
    user, separator, password = userinfo.partition(":")
    masked_user = _mask_secret_value(user) if _looks_like_secret_value(user) else user
    if not separator:
        return masked_user
    return f"{masked_user}:{_mask_secret_value(password)}"


def _url_credentials_excerpt(url: str) -> str:
    """URL 내장 자격증명 발췌 — userinfo **와 쿼리/fragment 의 자격증명성 값**을 마스킹한다.

    호스트/경로/파라미터 *이름* 은 남겨 위치를 특정하되 값은 남기지 않는다. userinfo 만 가리면
    `https://user:pass@host/?access_token=…` 의 토큰 원문이 차단 메시지에 그대로 남는다(외부 리뷰 R4)."""
    masked = _URL_USERINFO_RE.sub(lambda m: f"://{_mask_userinfo(m.group('userinfo'))}@", url)
    return _truncate_excerpt(_mask_url_parameter_values(masked))


def _mask_url_parameter_values(url: str) -> str:
    """URL 쿼리/fragment 에서 자격증명성 값만 마스킹한다(`?file=.env` 같은 경로 값은 그대로 노출)."""
    def _replace(match: re.Match) -> str:
        lead, key, equals, value = (match.group("lead"), match.group("key"),
                                    match.group("equals"), match.group("value"))
        if not equals:   # `#XkwPqr…` 처럼 값만 있는 fragment/세그먼트
            return f"{lead}{_mask_secret_value(key)}" if _looks_like_secret_value(key) else match.group(0)
        if value and (_looks_like_secret_value(value) or _is_secret_key_name(key)):
            return f"{lead}{key}={_mask_secret_value(value)}"
        return match.group(0)

    return _URL_PARAMETER_RE.sub(_replace, url)


def _url_path_secret_pattern(
    url: str, patterns: tuple[str, ...], match: Callable,
) -> str | None:
    """원격 URL 이 시크릿 *파일* 을 가리키는가 — **정확-이름/확장자 패턴만** 적용(없으면 None).

    `https://raw.example.com/o/r/main/deploy/.env` 처럼 URL 경로 끝이 시크릿 파일이면 그 URL 을 넘기는
    것 자체가 시크릿 유출 경로다. 이름-substring 패턴(`*token*`)은 여기 **쓰지 않는다** — 그게 URL
    엔드포인트 오탐(`/oauth/token`·`/tokens/list`·외부 리뷰 MF1)의 원인이라 접점을 두지 않는다.

    판정 대상은 (a) 경로 basename 과 (b) **쿼리/fragment 파라미터 값**(`?file=.env`·외부 리뷰 R3)이며,
    일반 경로축과 같은 **소문자 정규화**를 거친다(`/DEPLOY.PEM`). 정확-이름/확장자 패턴만 쓰므로
    `?page=token` 같은 엔드포인트 파라미터는 영향받지 않는다. authority 뒤에 경로가 없어도
    (`https://files.example?file=.env`) 쿼리는 검사한다 — 경로 부재로 조기 반환하면 우회였다(R4)."""
    remainder = _URL_AUTHORITY_RE.sub("", url.split("://", 1)[1], count=1)
    path, separator, query_and_fragment = remainder.partition("?")
    if not separator:
        path, _, query_and_fragment = remainder.partition("#")
    names = [PurePosixPath(_trim_candidate(path.split("#")[0])).name]
    for parameter in re.split(r"[&;#]", query_and_fragment):
        key, equals, value = parameter.partition("=")
        names.append(PurePosixPath(_trim_candidate(value if equals else key)).name)
    exact_patterns = _exact_name_patterns(patterns)
    for name in names:
        if not name or not _STRICT_PATH_NAME_RE.match(name):
            continue
        pattern = match(name.lower(), exact_patterns)
        if pattern is not None:
            return pattern
    return None


def _mask_secret_value(value: str) -> str:
    """크리덴셜 값을 앞 몇 자만 남기고 마스킹 — 발췌로 위치는 특정하되 값은 로그에 남기지 않는다."""
    return f"{value[:_VALUE_MASK_HEAD_CHARS]}***(값 마스킹·{len(value)}자)"


def _truncate_excerpt(text: str) -> str:
    """발췌 길이 상한(메시지 가독성) — 넘치면 말줄임."""
    if len(text) <= _EXCERPT_MAX_CHARS:
        return text
    return text[:_EXCERPT_MAX_CHARS] + "…"


def _iter_prompt_secret_hits(prompt: str) -> Iterator[PromptSecretHit]:
    """합성 프롬프트 시크릿 판정을 기존 우선순서대로 yield한다.

    `scan_prompt_secrets()`의 첫-hit 판정 계약은 이 iterator의 첫 원소로 유지한다. 사람 승인 차단
    경로만 끝까지 소비해 모든 탐지를 표시한다(T-0476). 즉 패턴·축·마스킹 로직은 바꾸지 않고,
    조기 반환만 exhaustive 수집 가능한 yield로 푼다.
    """
    er = _load_external_review()
    patterns = er._SECRET_DENYLIST_PATTERNS
    for pem in _PEM_PRIVATE_KEY_RE.finditer(prompt):
        yield PromptSecretHit(
            f"{pem.group(0)} …(본문 마스킹)", _SECRET_RULE_PEM, _SECRET_AXIS_VALUE,
        )
    for raw in prompt.split():
        # ⓑ 값축(알려진 prefix)은 **URL 제거 전 원문**에서 본다(외부 리뷰 R2 must-fix) — URL 면제는
        # 경로축 오탐(`/oauth/token`)을 없애려는 것이지, URL 안에 실린 크리덴셜
        # (`https://ghp_…@github.com/o/r.git`·`?access_token=ghp_…`·`/services/xoxb-…`)까지 눈감으라는
        # 게 아니다. 값축을 먼저, 원문에 대고 돌려 그 회귀를 막는다.
        for token in _secret_value_candidates(raw):
            if _is_known_secret_value(token):
                yield PromptSecretHit(
                    _mask_secret_value(token), _SECRET_RULE_VALUE_PREFIX, _SECRET_AXIS_VALUE,
                )
        scannable = raw
        # URL 스캔은 scheme 구분자가 있는 토큰에만(긴 프롬프트에서 토큰마다 정규식 돌리지 않도록).
        for url in _URL_IN_TEXT_RE.finditer(raw) if "://" in raw else ():
            userinfo = _URL_USERINFO_RE.search(url.group(0))
            if userinfo is not None and _is_credential_userinfo(userinfo.group("userinfo")):
                yield PromptSecretHit(
                    _url_credentials_excerpt(url.group(0)),
                    _SECRET_RULE_URL_CREDENTIALS, _SECRET_AXIS_VALUE,
                )
            pattern = _url_path_secret_pattern(
                url.group(0), patterns, er._matching_denylist_pattern)
            if pattern is not None:
                yield PromptSecretHit(
                    # 경로축 판정이어도 발췌는 같은 URL 표시층을 탄다. 원문 URL을 그대로 내보내면
                    # userinfo password와 query/fragment 자격증명이 값축 발췌에서는 가려져도 이
                    # 경로축 발췌를 통해 stderr/raw에 다시 노출된다(T-0476 fix2).
                    _url_credentials_excerpt(url.group(0)), pattern, _SECRET_AXIS_PATH,
                )
            # 자격증명·시크릿 파일이 아닌 URL(엔드포인트·문서 링크)만 경로축 비대상 — `:` 분리 뒤 남는
            # URL 경로의 basename(`/oauth/token`·무확장자)이 `*token*` 에 걸리던 오탐(외부 MF1) 폐쇄.
            scannable = scannable.replace(url.group(0), " ")
        for token in _secret_path_candidates(scannable):
            pattern = _matching_secret_path_pattern(
                token, patterns, er._matching_denylist_pattern)
            if pattern is not None:
                # 경로축이라도 발췌 토큰이 값-형태면 마스킹한다(값축 원칙과 일관·내부 리뷰 SF2) —
                # `*secret*` 류 패턴은 크리덴셜 *값* 에도 걸릴 수 있다.
                excerpt = (_mask_secret_value(token) if _looks_like_secret_value(token)
                           else _truncate_excerpt(token))
                yield PromptSecretHit(excerpt, pattern, _SECRET_AXIS_PATH)
    for match in _ASSIGNMENT_RE.finditer(prompt):
        key, value = match.group("key"), _assignment_value(match)
        if _is_secret_key_name(key) and _looks_like_secret_value(value):
            yield PromptSecretHit(
                f"{_truncate_excerpt(key)}={_mask_secret_value(value)}",
                _SECRET_RULE_ASSIGNMENT, _SECRET_AXIS_VALUE,
            )


def scan_prompt_secret_hits(prompt: str) -> tuple[PromptSecretHit, ...]:
    """합성 프롬프트의 고유 시크릿 판정을 기존 판정 순서로 수집한다(승인 차단 경로 전용).

    한 텍스트가 토큰화/할당 스캔 양쪽에서 같은 판정을 내도 사람에게 같은 승인 항목을 반복시키지
    않도록 `(axis, pattern, excerpt)` 완전 동일 hit만 제거한다. 서로 다른 축/판정은 보존한다."""
    unique: list[PromptSecretHit] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in _iter_prompt_secret_hits(prompt):
        key = (hit.axis, hit.pattern, hit.excerpt)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return tuple(unique)


def scan_prompt_secrets(prompt: str) -> PromptSecretHit | None:
    """합성 프롬프트에서 첫 시크릿(파일 경로/이름 · 크리덴셜 값)을 찾는다(전송 전 차단·§4.7).

    양성매칭 2축(T-0472): ⓐ **경로축** — `=`/`:` 분리·구두점 트리밍·조사 제거·소문자 정규화로 토큰
    후보를 낸 뒤 *경로 형태* 후보에만 external_review denylist(`.env`·`*secret*`·`*.key` …)를 적용 +
    알려진 시크릿 파일명(`id_rsa`·`.npmrc`) 매칭 + 원격 URL 경로는 정확-이름/확장자 패턴만.
    ⓑ **값축** — PEM 개인키 블록·알려진 값 prefix(`ghp_…`·URL 안까지)·URL userinfo 자격증명·시크릿
    키명(성분 경계 + camelCase hump) 할당의 고엔트로피 값. 반환: 매칭 시 `PromptSecretHit`(발췌·
    판정명·축), 없으면 None.

    **한계 정직 표기**(§4.7): ① 이 스캔은 전송 텍스트 필터일 뿐 위임 프로세스의 cwd 파일 직접 읽기·env
    상속은 못 막는다. ② 값축은 *형식이 있는* 크리덴셜만 겨냥한다 — 불투명 Bearer 토큰(`Bearer
    9f3a…`·발급기관 prefix 없음)·Basic base64(`Basic dXNlcjpwYXNz`)·사전 단어 조합/저엔트로피
    비밀번호(`correct horse battery staple`)는 산문과 기계적으로 못 가르므로 **의도적으로** 통과시킨다
    (오탐 방향으로 보수적 — 이 게이트의 오탐은 위임 자체를 막는다). 반대편 잔여 겹침도 있다 — 시크릿
    키명에 할당된 CamelCase 영단어 값은 랜덤 비밀번호와 못 갈라 차단 쪽에 남는다(실 코퍼스 0건).
    ③ URL 은 경로축에서 **정확-이름/확장자 패턴만** 본다(`…/deploy/.env` 차단·`/oauth/token` 통과) —
    이름-substring 패턴이 가리키는 원격 경로(`https://host/api/secret-store`)는 안 걸린다.
    ④ **상대경로의 첫 성분이 비ASCII 면 경로축 비대상**이다(`문서/설정/.env` 통과·`/path/to/사용자/.env` 는
    차단) — 한국어 산문이 슬래시로 이어진 조각과 기계적으로 못 갈라 앵커 쪽으로 보수화한 경계다.
    같은 이유로 **앵커도 확장자도 없는 상대경로의 substring 이름**(`etc/credentials`)은 안 걸린다 —
    산문 조각(`key/token`)과 형태가 같다. 정확 시크릿 파일명(`deploy/id_rsa`)은 이름이 비모호해 면제다.
    ⑤ ④ prompt-file 게이트의 문서 확장자 면제는 이름만으로 "시크릿을 다루는 문서 vs 시크릿이 담긴
    문서"를 못 가르는 수렴 불가 지점이라 **면제 유지**로 종결했다(위협모델·근거는
    `_prompt_file_denylist_pattern` 참조 — 내용은 이 스캔이 다시 훑고, 시크릿 디렉토리 아래 문서는 잡힌다)."""
    return next(_iter_prompt_secret_hits(prompt), None)


def secret_scan_prompt_digest(prompt: str, harness: str, model: str) -> str:
    """합성 프롬프트 **전문 + 해소된 primary 수신자**의 짧은 승인 digest(T-0476).

    발췌만 해시하면 같은 문제 문자열이 든 다른 프롬프트에 승인을 재사용할 수 있으므로 role preamble과
    prompt-file 본문을 합친 UTF-8 바이트 전체에 `harness:model`을 함께 결속한다. 도메인 구분자와
    NUL 필드 구분으로 접합 모호성을 없앤다. 출력은 CLI 전사용 96bit hex다.

    이 결속은 primary 수신자를 바꾸는 ack 재사용을 막는다. ack 통과 후 명시 설정된 loud 인프라 폴백이
    발동해 타 하네스로 갈 수 있는 잔여 창은 수용한다(모듈 docstring의 시크릿 통제 한계)."""
    material = b"\0".join((
        b"pm_delegate.secret-scan-ack.v2",
        harness.encode("utf-8"),
        model.encode("utf-8"),
        prompt.encode("utf-8"),
    ))
    return hashlib.sha256(material).hexdigest()[:SECRET_SCAN_ACK_HEX_LENGTH]


def _format_secret_scan_hits(hits: tuple[PromptSecretHit, ...]) -> str:
    """사람 검토용 탐지 목록 — 값 발췌는 판정 단계에서 이미 마스킹됐다."""
    lines: list[str] = []
    displayed = hits[:SECRET_SCAN_HIT_DISPLAY_LIMIT]
    for index, hit in enumerate(displayed, start=1):
        lines += [
            f"  · 탐지 {index}/{len(hits)}: {hit.axis}축 판정 '{hit.pattern}' 매칭",
            f"    발췌: {hit.excerpt}   (크리덴셜 값은 마스킹·식별 가능한 경로/이름은 표시)",
        ]
    remaining = len(hits) - len(displayed)
    if remaining:
        lines.append(f"  · … {remaining}건 더 · 전체는 raw 감사줄에서 확인")
    return "\n".join(lines)


def _windows_retry_command(command: list[str]) -> str:
    """cmd.exe/PowerShell 어느 쪽에서도 메타문자 재해석 없는 Windows 재실행 줄을 만든다.

    argv를 PowerShell 단일따옴표 리터럴로 만든 뒤 UTF-16LE `-EncodedCommand`로 감싼다. 복사용 줄은
    인코딩본을 유지하고, 사람이 실제 승인 대상을 확인할 수 있도록 바로 아래에 디코드된 줄도 표시한다."""
    script = "& " + " ".join(
        "'" + token.replace("'", "''") + "'" for token in command
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    encoded_command = (
        f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"
    )
    return (
        f"{encoded_command}\n"
        f"    PowerShell 디코드(검토용·복사는 위 인코딩본): {script}"
    )


def _running_on_windows() -> bool:
    return os.name == "nt"


def _secret_scan_retry_command(argv: list[str], digest: str) -> str:
    """기존 ack를 제거하고 현재 digest 하나만 붙인 안전한 재실행 커맨드 표시를 만든다."""
    cleaned: list[str] = []
    skip_value = False
    for token in argv:
        if skip_value:
            skip_value = False
            continue
        if token == "--secret-scan-ack":
            skip_value = True
            continue
        if token.startswith("--secret-scan-ack="):
            continue
        cleaned.append(token)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        *cleaned,
        "--secret-scan-ack",
        digest,
    ]
    return _windows_retry_command(command) if _running_on_windows() else shlex.join(command)


def build_env(harness: str) -> dict[str, str]:
    """subprocess env 를 allowlist 로 정제한다(§4.7) — PM 세션 환경 통째 상속 금지(타 크리덴셜 미상속).

    base 키 + LC_* 접두 + 하네스별 인증 키만 전달한다. 존재하는 키만 담아 새 env dict 를 구성한다
    (os.environ 미상속). 목록은 상수(_ENV_ALLOWLIST_*·_HARNESS_AUTH_ENV)로 T-0449 실측 조정 가능."""
    src = os.environ
    out: dict[str, str] = {}
    for key in _ENV_ALLOWLIST_BASE:
        if key in src:
            out[key] = src[key]
    for key, value in src.items():
        if any(key.startswith(prefix) for prefix in _ENV_ALLOWLIST_PREFIXES):
            out[key] = value
    for key in _HARNESS_AUTH_ENV.get(harness, ()):
        if key in src:
            out[key] = src[key]
    return out


def _prompt_file_contained(prompt_file: Path, cwd: Path) -> bool:
    """prompt-file 이 (a) 해소된 cwd(realpath) 하위 또는 (b) 이 repo PM 홈(REPO/.project_manager)
    하위인가(§3.1·§4.6·containment).

    realpath 로 해소(심볼릭·`..` 이탈 차단·resolve 가 symlink 를 실경로로 편다)한 뒤 **두 신뢰 루트에
    대해서만** relative_to 판정한다. 옛 '경로 성분 `.project_manager` 매칭'은 **폐기** — 임의 외부
    `.project_manager/` 경로(예 `/outside/.project_manager/secret.txt`)를 cwd 와 무관하게 통과시키던
    우회였다(codex must-fix). 루트 cwd(`/`)는 _validate_args 가 usage error 로 앞서 거른다(cwd=`/`
    가 전 파일시스템을 (a) 로 열어버리는 우회 차단)."""
    try:
        resolved = prompt_file.resolve()
    except OSError:
        return False
    cwd_resolved = cwd.resolve()
    pm_home = (REPO / ".project_manager").resolve()
    return _is_relative_to(resolved, cwd_resolved) or _is_relative_to(resolved, pm_home)


def _is_relative_to(path: Path, base: Path) -> bool:
    """Path.is_relative_to (3.9 호환 래퍼)."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _cwd_in_git_repo(cwd: Path, run_fn: Callable | None = None) -> bool:
    """--cwd 가 git 저장소 루트이거나 그 하위인가(`git rev-parse --show-toplevel` 성공·경계 보강).

    광범위 경로(홈 디렉토리 등 non-repo)를 신뢰 작업공간으로 삼는 것을 차단한다(codex must-fix) —
    실제 허용 작업공간을 git repo 로 조여 cwd (a) 신뢰 루트가 과도하게 넓어지는 것을 막는다. git 미설치·
    실행 불가·비-repo 는 False(호출부 fail-loud). run_fn 주입(테스트 mock·external_review 동형 seam)."""
    _run = run_fn or subprocess.run
    try:
        result = _run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return False
    return getattr(result, "returncode", 1) == 0 and bool((result.stdout or "").strip())


def _prompt_file_name_pattern(
    name: str, patterns: tuple[str, ...], match: Callable,
) -> str | None:
    """prompt-file **이름** 판정(④ 게이트 전용) — 확장자 조건 없이, 단 프롬프트 문서 확장자는 예외.

    이름-substring 패턴(`*token*`)이 걸린 `.md`/`.markdown`/`.rst` 는 통과시킨다(`T-0472-token-guard.md`
    — 티켓 주제어를 담은 정상 프롬프트 파일명이 이 게이트의 최대 오탐원). 정확 이름/확장자 패턴
    (`.env`·`*.pem`·`id_rsa`)은 문서 확장자여도 차단이다."""
    pattern = _matching_secret_name_pattern(name, patterns, match, require_data_ext=False)
    if pattern is None:
        return None
    if _is_name_substring_pattern(pattern) and _has_prompt_doc_extension(name):
        return None
    return pattern


def _has_prompt_doc_extension(name: str) -> bool:
    """파일명이 프롬프트 문서 확장자인가(`.md`·`.markdown`·`.rst`)."""
    match = _FILE_EXTENSION_RE.search(name)
    return match is not None and match.group(0)[1:].lower() in _PROMPT_DOC_EXTENSIONS


def _prompt_file_denylist_pattern(prompt_file: Path) -> str | None:
    """prompt-file 이 시크릿 denylist 패턴에 걸리는가 — **원본 경로 + resolve() 해소 경로 양쪽** 검사
    (내용 읽기 전 차단·§4.7 b·symlink 우회 폐쇄).

    `prompt.md → <cwd>/.env` 같은 symlink 는 원본 이름(prompt.md)이 clean 이라 통과하나 resolve() 해소
    경로(.env)는 denylist 에 걸린다 — 양쪽을 검사해 symlink 를 통한 secret 읽기를 차단한다(codex must-fix).
    external_review `_matching_denylist_pattern`(fnmatch·`.env`·`*credential*`·`*.key`) 재사용. 걸린
    패턴명 반환(원문 토큰 미노출).

    판정 대상은 **파일 이름**이다(T-0472·`_matching_secret_name_pattern` 공유) — 옛 전체 경로 fnmatch 는
    *조상 디렉토리* 이름의 substring 까지 삼켜(`/tmp/…/test_secret_scan0/prompt.md`) 정상 프롬프트를
    읽기도 전에 차단했다(합성 프롬프트 스캔과 같은 문맥 무시 substring 오탐 가족·기존 테스트가 이 조기
    차단으로 false-green 이었다). 디렉토리 형태 패턴(`secrets/` 류)만 전체 경로로 판정한다.

    단 이름 판정은 **확장자 조건 없이**(`require_data_ext=False`) 적용한다(내부 리뷰 SF1) — 여기는 파일
    *언급* 이 아니라 **내용이 통째로 전송**되는 지점이라 "소스/문서 확장자는 시크릿이 아니다"라는 ③
    합성 프롬프트 스캔의 전제가 성립하지 않는다(`secrets.py`·`token.sh`·`app_credentials.log` 차단
    유지). **예외는 프롬프트 문서 확장자**(`_PROMPT_DOC_EXTENSIONS`)뿐이다 — 이름-substring 패턴만
    걸린 `T-0472-token-guard.md`(PM 12차 실사용 프롬프트 이름)까지 막으면 이 티켓이 없애려던 오탐
    클래스를 ④에서 재생산한다(외부 리뷰 R2). 문서는 막지 않되 **내용은 ⑧ 값축 스캔이 다시 훑는다**
    (실 크리덴셜이 들었으면 거기서 걸린다). 정확 이름/확장자 패턴(`.env`·`*.pem`·`id_rsa`·`.npmrc`)은
    문서 확장자여도 그대로 차단된다.

    **면제의 잔여 한계(수용·PM 판정 R3)**: 이름만으로는 "시크릿을 *다루는* 문서"(`T-0472-token-guard.md`)와
    "시크릿이 *담긴* 문서"(`prod-secrets.md`)를 못 가른다 — 어느 쪽으로 정해도 반대편 리뷰가 반려하는
    수렴 불가 지점이라 **면제 유지**로 종결했다. 근거는 위협모델이다: ④ 는 PM 이 *실수로* 시크릿 파일을
    넘기는 걸 막는 방어심층이지 적대적 PM 을 막는 층이 아니고(위임 프로세스는 어차피 cwd 를 직접 읽는다·
    §4.7 ①), 문서 안의 실 크리덴셜은 ⑧ 값축이 다시 잡는다. 단 **디렉토리 성분 검사는 문서에도 적용**되어
    `secrets/`·`credentials/`·`.aws/` 아래 파일은 확장자와 무관하게 차단된다(아래 `_secret_directory_segment`)."""
    er = _load_external_review()
    patterns = er._SECRET_DENYLIST_PATTERNS
    # 기본 denylist 엔 `/` 패턴이 없다 — 이 분기는 conf 확장(`review_denylist_extra` 에 `secrets/` 류를
    # 넣은 채택자)에서만 도달한다. 기본 형상에서 dead 로 보이는 건 그 때문(관측 메모).
    dir_patterns = tuple(p for p in patterns if "/" in p)
    candidates = [prompt_file]
    try:
        candidates.append(prompt_file.resolve())
    except OSError:
        pass
    for cand in candidates:
        # 이름은 **소문자 정규화** 후 판정한다 — `.ENV`·`DEPLOY.PEM`·`Credentials.env` 가 내용을 읽기도
        # 전에 통과하던 대소문자 우회 폐쇄(외부 리뷰 R4·합성 프롬프트 경로축과 같은 정규화).
        pattern = _prompt_file_name_pattern(
            cand.name.lower(), patterns, er._matching_denylist_pattern)
        if pattern is not None:
            return pattern
        # 시크릿 디렉토리 성분(정확 일치)은 이름/확장자와 무관하게 차단 — `<cwd>/secrets/prompt.md`
        # (외부 리뷰 R3). substring 이 아니라 성분 단위라 pytest tmp `…/test_secret_scan0/` 는 무영향.
        if _secret_directory_segment(str(cand).replace("\\", "/")) is not None:
            return _SECRET_RULE_DIRECTORY
        if dir_patterns:
            pattern = er._matching_denylist_pattern(str(cand), dir_patterns)
            if pattern is not None:
                return pattern
    return None


# ── 쓰기-타깃 axis 재앵커 (§4.6) ─────────────────────────────────────────────

_ENGINE_PATH_CANDIDATE_RE = re.compile(
    r"[A-Za-z0-9._/\\-]*\.project_manager[A-Za-z0-9._/\\-]*"
)
_PATH_LAYOUT_CHAR_CLASS = r"A-Za-z0-9._/\\-"
_PATH_LAYOUT_CHAR_RE = re.compile(rf"[{_PATH_LAYOUT_CHAR_CLASS}]")
_PATH_LAYOUT_GAP_RE = re.compile(
    r"[ \t]*(?:\\[ \t]*)?(?:\r\n|\r|\n)[ \t]*|[ \t]+"
)
_PATH_WRAPPER_RE = r"""[`'"()\[\]{}<>]*"""
_KOREAN_PATH_PARTICLE_RE = r"(?:은|는|을|를|에|에는|만|만은)?"
_KOREAN_WRITE_VERBS = (
    "수정", "편집", "변경", "고치", "고쳐", "건드리", "건드려", "손보", "손봐",
    "손대", "지우", "지워", "바꾸", "바꿔", "삭제", "추가", "구현",
    "덮어쓰", "덮어써", "대체", "재작성", "패치", "리팩터",
)
_ASCII_WRITE_VERBS = (
    "modify", "modified", "edit", "edited", "touch", "touched",
    "change", "changed", "rewrite", "rewritten", "replace", "replaced",
    "overwrite", "overwritten", "update", "updated", "fix", "fixed",
    "write", "written", "delete", "deleted", "alter", "altered",
    "implement", "implemented", "patch", "refactor",
)
# 한국어 write stem은 활용어미 앞에서도 잡되, `미수정`·`무변경`·`재수정`처럼 상태/반복을
# 나타내는 접두 합성어 안의 부분 문자열은 write 지시로 보지 않는다.
_KOREAN_NON_COMMAND_PREFIX_CHARS = "미무비불재"
_WRITE_VERB_PATTERN = (
    rf"(?:(?<![{_KOREAN_NON_COMMAND_PREFIX_CHARS}])(?:"
    + "|".join(
        re.escape(verb)
        for verb in sorted(_KOREAN_WRITE_VERBS, key=len, reverse=True)
    )
    + r")|\b(?:"
    + "|".join(
        re.escape(verb)
        for verb in sorted(_ASCII_WRITE_VERBS, key=len, reverse=True)
    )
    + r")\b)"
)
_KOREAN_DIRECT_NEGATION_AFTER_RE = re.compile(
    rf"^\s*{_PATH_WRAPPER_RE}\s*{_KOREAN_PATH_PARTICLE_RE}\s*"
    r"(?:"
    r"(?:건드리지|손대지|수정하지|편집하지|변경하지)\s*"
    r"(?:마라|말라|마세요|말\s*것)"
    r"|수정\s*금지"
    r")",
    re.IGNORECASE,
)
_ENGLISH_DIRECT_NEGATION_AFTER_RE = re.compile(
    rf"^\s*{_PATH_WRAPPER_RE}\s*"
    r"(?:must\s+not|should\s+not|do\s+not|don't|never)\s+"
    rf"(?:be\s+)?{_WRITE_VERB_PATTERN}",
    re.IGNORECASE,
)
_DIRECT_NEGATION_BEFORE_RE = re.compile(
    rf"(?:"
    r"수정\s*금지\s*[:：]?"
    rf"|(?:do\s+not|don't|must\s+not|should\s+not|never)\s+{_WRITE_VERB_PATTERN}"
    r")"
    rf"\s*{_PATH_WRAPPER_RE}\s*$",
    re.IGNORECASE,
)
_POST_NEGATION_CLAUSE_PREFIX = (
    r"""^\s*[`'")\]}>]*\s*(?:[;；。！？!?.]\s*)?"""
)
_NEGATION_IGNORE_MARKER_RE = re.compile(
    r"(?:는\s*말은\s*)?(?:무시|\b(?:ignore|disregard|override)\b)",
    re.IGNORECASE,
)
_NEGATION_FOLLOWUP_PRONOUN_WRITE_RE = re.compile(
    _POST_NEGATION_CLAUSE_PREFIX
    + rf"(?:and|but)\s+"
    rf"{_WRITE_VERB_PATTERN}\s+(?:it|this|that)\b",
    re.IGNORECASE,
)
_NEGATION_INSTEAD_RE = re.compile(
    _POST_NEGATION_CLAUSE_PREFIX
    + r"(?:그\s*)?(?:대신|instead(?:\s+of\s+(?:that|this))?)",
    re.IGNORECASE,
)
_OVERRIDE_CLAUSE_END_RE = re.compile(
    r"[;；。！？!?\r\n]|\.(?=\s|$)"
)
_EXPLICIT_PATH_RE = re.compile(
    r"(?:\.{0,2}[/\\])?[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+|\bwiki\b",
    re.IGNORECASE,
)
_WRITE_NEAR_READ_ONLY_CALL_RE = re.compile(
    _WRITE_VERB_PATTERN,
    re.IGNORECASE,
)
_READ_ONLY_CALL_FILE_REFERENCE_WRITE_RE = re.compile(
    r"(?:"
    r"(?:위|이)\s*(?:스크립트|파일)(?:을|를|은|는)?"
    r"[\s\S]{0,40}?"
    rf"{_WRITE_VERB_PATTERN}"
    r"|"
    rf"{_WRITE_VERB_PATTERN}"
    r"[\s\S]{0,40}?"
    r"(?:"
    r"that\s+(?:script|file)(?:\s+above)?"
    r"|the\s+(?:(?:script|file)\s+above|above\s+(?:script|file))"
    r")"
    r")",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY_RE = re.compile(
    r"[。！？!?]|\.(?=\s|$)"
)
_READ_ONLY_CALL_SHELL_TAIL_RE = re.compile(
    r"(?:&&|\|\||[|;&])"
)
_READ_ONLY_CALL_INVALID_TICKET_ARG_RE = re.compile(
    r"""^[ \t]*[`'")\]}>.,!?。！？]*[ \t]*T-(?:[A-Za-z0-9_-]+-\d+|\d+)\b""",
    re.IGNORECASE,
)
_PYTHON_LAUNCHER_BEFORE_PATH_RE = re.compile(
    r"\b(?:python3|python|py(?:[ \t]+-\d+(?:\.\d+)?)?)[ \t]+$",
    re.IGNORECASE,
)
_READ_ONLY_BOARD_CALL_RE = re.compile(
    r"""
    [ \t]+(?:
        show[ \t]+(?:T-NNNN|<T-NNNN>|T-(?:[A-Za-z0-9][A-Za-z0-9_-]*-\d+|\d+))
        |
        list(?:
            [ \t]+(?:
                --(?:mine|all)
                |--status[ \t]+(?:open|claimed|blocked|done|all)
                |--(?:tag|repo|task|user)[ \t]+[^\s`'"()\[\]{}<>|&;]+
                |--slot[ \t]+\d+
            )
        )*
        |
        lint(?:[ \t]+--gate)?
        |
        idea[ \t]+list
        |
        prefix[ \t]+list
        |
        regression[ \t]+check
        |
        livegate[ \t]+check
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _clean_prompt_path_token(raw: str) -> str:
    """프롬프트의 공백 토큰에서 경로 바깥 인용·구두점만 제거한다."""
    return raw.strip("\"'`()[]{}<>,;:!?").rstrip(".")


def _engine_path_parts(raw: str) -> tuple[str, ...] | None:
    """토큰이 `.project_manager/tools` 경로면 정규화한 성분을 반환한다."""
    token = _clean_prompt_path_token(raw)
    if not token or ".project_manager" not in token:
        return None
    parts = PurePosixPath(token.replace("\\", "/")).parts
    for i in range(len(parts) - 1):
        if parts[i] == ".project_manager" and parts[i + 1] == "tools":
            return parts
    return None


def _path_layout_gap_is_internal(text: str, start: int, end: int) -> bool:
    """layout gap 이 이미 시작된 경로 후보 내부의 접힘 지점인가."""
    if start == 0 or end >= len(text):
        return False
    next_char = text[end]
    if _PATH_LAYOUT_CHAR_RE.fullmatch(next_char) is None:
        return False
    if text[start - 1] in "/\\":
        return True
    if next_char not in "/\\":
        return False

    # separator 앞 gap 은 왼쪽 토큰이 이미 `.project_manager` 경로일 때만 접는다.
    # 따라서 `Modify\n.project_manager/...` 같은 일반 단어→경로 경계는 보존된다.
    left_match = re.search(rf"[{_PATH_LAYOUT_CHAR_CLASS}]+$", text[:start])
    if left_match is None:
        return False
    left_parts = PurePosixPath(left_match.group().replace("\\", "/")).parts
    return ".project_manager" in left_parts


def _normalize_prompt_path_layout(prompt: str) -> tuple[str, list[int]]:
    """긴 경로의 자연 줄바꿈을 제거한 매칭 뷰와 원문 offset map 을 반환한다.

    직전 조각이 separator 로 끝나거나 이미 `.project_manager` 경로이고 다음 조각이 separator 로
    시작할 때만 개행(plain 또는 shell식 ``\\\n`` 연속)/수평 공백을 접는다. 일반 단어와 경로 시작
    사이 및 독립된 여러 경로 사이의 layout 은 보존한다.
    """
    remove = [False] * len(prompt)
    for match in _PATH_LAYOUT_GAP_RE.finditer(prompt):
        remove_start = match.start()
        if not _path_layout_gap_is_internal(prompt, remove_start, match.end()):
            # Windows식 경로 separator 자체가 줄끝에 온
            # `.project_manager\\\ntools\\x.py`에서는 `\`를 보존하고 개행만 접는다.
            # `/\\\n`의 두 번째 `\`는 shell 연속 문자이므로 기존처럼 gap 전체를 제거한다.
            backslash = prompt.rfind("\\", match.start(), match.end())
            if (backslash < 0
                    or not _path_layout_gap_is_internal(
                        prompt, backslash + 1, match.end()
                    )):
                continue
            remove_start = backslash + 1
        remove[remove_start:match.end()] = [True] * (
            match.end() - remove_start
        )
    kept = [i for i, discarded in enumerate(remove) if not discarded]
    return (
        "".join(prompt[i] for i in kept),
        kept,
    )


def _engine_path_occurrences(
    prompt: str,
) -> list[tuple[int, int, tuple[str, ...]]]:
    """엔진 경로 후보를 공백 토큰이 아닌 실제 경로 span 단위로 반환한다.

    한국어 조사·공백 없는 문장부호가 경로 토큰에 붙어도 span 은 경로 끝에서 닫힌다. 따라서
    바로 뒤의 금지/쓰기 표현을 해당 출현에만 결합할 수 있고, 같은 절의 타 경로로 전파하지 않는다.
    """
    occurrences: list[tuple[int, int, tuple[str, ...]]] = []
    normalized, original_offsets = _normalize_prompt_path_layout(prompt)
    for match in _ENGINE_PATH_CANDIDATE_RE.finditer(normalized):
        raw = match.group().rstrip(".")
        parts = _engine_path_parts(raw)
        if parts is not None:
            raw_end = match.start() + len(raw)
            occurrences.append((
                original_offsets[match.start()],
                original_offsets[raw_end - 1] + 1,
                parts,
            ))
    return occurrences


def _is_pure_read_only_board_call(
    prompt: str, start: int, end: int, path_parts: tuple[str, ...],
) -> bool:
    """현재 경로 span 이 독립된 read-only board 호출인가(A).

    각 서브커맨드의 실제 read-only 인자만 받고, 명령 뒤 같은 줄의 자연어 tail은 write 동사가
    없을 때 허용한다. 면제 범위는 이 명령 span 하나뿐이다. shell 연산/후속 명령, 같은 줄의
    수정 지시, 호출을 목적어로 삼은 앞쪽 write 동사는 모호한 write 로 보아 면제하지 않는다.
    """
    if path_parts[-1] != "board.py":
        return False

    line_start = max(prompt.rfind("\n", 0, start), prompt.rfind("\r", 0, start)) + 1
    newline_positions = [pos for pos in (prompt.find("\n", end), prompt.find("\r", end))
                         if pos >= 0]
    line_end = min(newline_positions) if newline_positions else len(prompt)
    before_path = prompt[line_start:start]
    python_match = _PYTHON_LAUNCHER_BEFORE_PATH_RE.search(before_path)
    if python_match is None:
        return False

    after_path = prompt[end:line_end]
    call_match = _READ_ONLY_BOARD_CALL_RE.match(after_path)
    if call_match is None:
        return False
    command_tail = after_path[call_match.end():]
    if (_READ_ONLY_CALL_SHELL_TAIL_RE.search(command_tail) is not None
            or _READ_ONLY_CALL_INVALID_TICKET_ARG_RE.match(command_tail) is not None
            or _WRITE_NEAR_READ_ONLY_CALL_RE.search(command_tail) is not None):
        return False
    command_start = line_start + python_match.start()
    command_end = end + call_match.end()

    # `modify "python3 ... show T-NNNN"`처럼 명령 span 자체가 앞선 write 동사의
    # 목적어인 경우를 닫는다. 앞쪽 별도 경로를 특정한 write 는 이 출현의 목적어가 아니다.
    before_command = before_path[:python_match.start()]
    nearby_prefix = before_command[-120:]
    if (_WRITE_NEAR_READ_ONLY_CALL_RE.search(nearby_prefix) is not None
            and not re.search(r"[A-Za-z0-9._/\\-]+\.[A-Za-z0-9_-]+"
                              r"[\s\S]{0,40}$", nearby_prefix)):
        return False
    reference_prefix = prompt[max(0, command_start - 240):command_start]
    prefix_boundaries = list(_SENTENCE_BOUNDARY_RE.finditer(reference_prefix))
    if prefix_boundaries:
        reference_prefix = reference_prefix[prefix_boundaries[-1].end():]
    reference_suffix = prompt[
        command_end:min(len(prompt), command_end + 240)
    ]
    suffix_boundary = _SENTENCE_BOUNDARY_RE.search(reference_suffix)
    if suffix_boundary is not None:
        reference_suffix = reference_suffix[:suffix_boundary.start()]
    reference_window = (
        reference_prefix
        + "\n<READ_ONLY_CALL>\n"
        + reference_suffix
    )
    if _READ_ONLY_CALL_FILE_REFERENCE_WRITE_RE.search(reference_window) is not None:
        return False
    return True


def _same_engine_path(
    candidate: str, path_parts: tuple[str, ...],
) -> bool:
    """명시 경로가 현재 금지된 엔진 경로와 같은 engine-relative 경로인가."""
    candidate_parts = _engine_path_parts(candidate)
    if candidate_parts is None:
        return False
    current_idx = path_parts.index(".project_manager")
    candidate_idx = candidate_parts.index(".project_manager")
    return (
        path_parts[current_idx:] == candidate_parts[candidate_idx:]
    )


def _redirect_targets_current_engine_path(
    redirect: str, path_parts: tuple[str, ...],
) -> bool:
    """redirect의 명시 대상이 없거나 현재 엔진 경로를 다시 가리키는가.

    `wiki 문서`처럼 최상위 문서 영역만 적은 형태도 명시 비-엔진 대상으로 본다. 대명사나
    목적어 생략은 기존처럼 금지 경로를 가리킬 수 있으므로 보수적으로 True다.
    """
    explicit_paths = _EXPLICIT_PATH_RE.findall(redirect)
    if not explicit_paths:
        return True
    return any(
        _same_engine_path(candidate, path_parts)
        for candidate in explicit_paths
    )


def _negation_is_overridden_for_path(
    prompt: str, start: int, negation_end: int, path_parts: tuple[str, ...],
) -> bool:
    """금지 뒤 write override가 현재 엔진 경로를 다시 대상으로 삼는가.

    ignore/disregard/override 마커는 인용된 금지 앞에도 올 수 있어 pre+post에서 찾되, 실제 write
    동사는 반드시 금지 뒤에 있어야 한다. ignore 및 `대신`/`instead` 모두 후속 write 절에 명시
    경로가 없거나 같은 엔진 경로일 때만 현재 금지를 폐기한다. 명시된 비-엔진 경로(예:
    wiki/roadmap.md)로 redirect하면 현재 엔진 경로의 금지는 유지한다.
    """
    post_window = prompt[negation_end:min(len(prompt), negation_end + 240)]
    if _NEGATION_FOLLOWUP_PRONOUN_WRITE_RE.search(post_window) is not None:
        return True

    write_match = re.search(_WRITE_VERB_PATTERN, post_window, re.IGNORECASE)
    marker_window = prompt[
        max(0, start - 160):min(len(prompt), negation_end + 240)
    ]
    if (write_match is not None
            and _NEGATION_IGNORE_MARKER_RE.search(marker_window) is not None):
        clause_end = _OVERRIDE_CLAUSE_END_RE.search(
            post_window, write_match.end(),
        )
        redirect = (
            post_window[:clause_end.start()]
            if clause_end is not None else post_window
        )
        return _redirect_targets_current_engine_path(redirect, path_parts)

    instead = _NEGATION_INSTEAD_RE.search(post_window)
    if instead is None:
        return False

    clause_tail = post_window[instead.end():]
    clause_end = _OVERRIDE_CLAUSE_END_RE.search(clause_tail)
    if clause_end is not None:
        clause_tail = clause_tail[:clause_end.start()]
    redirect = (
        post_window[instead.start():instead.end()]
        + clause_tail
    )
    if re.search(_WRITE_VERB_PATTERN, redirect, re.IGNORECASE) is None:
        return False
    return _redirect_targets_current_engine_path(redirect, path_parts)


def _path_has_direct_negative_write_context(
    prompt: str, start: int, end: int, path_parts: tuple[str, ...],
) -> bool:
    """금지 표현이 현재 경로 출현에 직접 결합하며 뒤에서 폐기되지 않았는가(B).

    앞/뒤 1개 결합 패턴만 인정한다. 절 전체 검색을 하지 않으므로 타 경로의 금지 표현은 전파되지
    않으며, 경로와 금지 사이에 write 동사·타 경로가 끼면 자연스럽게 매치되지 않는다.
    """
    before = prompt[max(0, start - 160):start]
    after = prompt[end:min(len(prompt), end + 320)]
    after_match = _KOREAN_DIRECT_NEGATION_AFTER_RE.match(after)
    if after_match is None:
        after_match = _ENGLISH_DIRECT_NEGATION_AFTER_RE.match(after)

    if after_match is not None:
        negation_end = end + after_match.end()
    elif _DIRECT_NEGATION_BEFORE_RE.search(before) is not None:
        negation_end = end
    else:
        return False

    # “금지라는 말은 무시하고 수정하라” / “ignore that and edit it”는 금지가 아니다.
    return not _negation_is_overridden_for_path(
        prompt, start, negation_end, path_parts,
    )


def _prompt_targets_engine_code(prompt: str) -> bool:
    """위임 프롬프트가 엔진 코드 경로(`.project_manager/tools/`)를 write 대상으로 하는가(§4.6).

    정확 문자열 매칭이 아니라 **경로를 정규화해 성분 시퀀스**로 판정한다(codex must-fix) — 각 토큰을
    PurePosixPath 로 정규화(`.`/중복 슬래시 접힘)하고 긴 경로의 공백·개행/``\\\n`` 연속을 먼저
    접어 `.project_manager` 직후 `tools` 성분이 오면 True. 이로써 trailing slash 없음·`./`·자연
    줄바꿈 우회를 닫는다. 단, 독립된 순수 read-only `board.py show|list|lint` 호출(A)과 경로에 직접
    결합한 수정 금지(B)의 **그 경로 span만** 제외한다. 절/문서의 금지를 공유하지 않으며, 다른
    출현이 실제 write 지시면 True를 유지한다.

    위협 모델은 적대 프롬프트 의미 분석이 아니라 PM의 실수 방지다. 따라서 read-only 호출 다음 줄의
    대명사/일반 지시(예: ``본문대로 구현하라``)만으로 그 경로를 write 대상으로 추론하지 않는다
    (정당 관용구와 텍스트만으로 구분 불가). 명시 경로 재등장은 계속 차단한다. 잔여 경계는
    (1) 같은 엔진 경로에 금지와 write를 함께 붙인 자기모순 shape, (2) 경로 separator 없이
    `.project_manager``와 ``tools``를 개행 분할한 shape, (3) ASCII slash 대신 유니코드 동형
    slash를 쓴 shape다. 이 잔여 신형/적대 표현은 role preamble 금지와 T-0462의 사후 범위-밖
    변경 감지가 닫는다.
    write 역할 + PM 홈 cwd 조합에서만 재앵커 게이트로 쓰인다(PM-doc/wiki write 는 PM 홈 정당)."""
    for start, end, parts in _engine_path_occurrences(prompt):
        if _is_pure_read_only_board_call(prompt, start, end, parts):
            continue
        if _path_has_direct_negative_write_context(
            prompt, start, end, parts,
        ):
            continue
        return True
    return False


def check_write_target_reanchor(role: str, cwd: Path, prompt: str) -> Path | None:
    """write 역할이 PM 홈 cwd 에서 엔진 코드(import 사본)를 write 타깃하면 재앵커 대상 worktree 반환(§4.6).

    재앵커는 cwd 자체가 아니라 **쓰기-타깃 axis** 로 판정 — PM-doc(wiki/ADR/spike) 작업은 PM 홈 cwd
    정당. 판정 = external_review `_pm_home_reanchor`(실 board 소유 + `work/*` canonical 보유·파일 존재
    휴리스틱 금지) 재사용. read 역할·비-엔진-코드 타깃·PM 홈 아닌 cwd 는 None(통과)."""
    if role not in WRITE_ROLES:
        return None
    if not _prompt_targets_engine_code(prompt):
        return None
    er = _load_external_review()
    return er._pm_home_reanchor(cwd)


# ── 결과 박제 (§3.4·O_EXCL·0600·PID/UUID) ─────────────────────────────────────

def save_raw_output(
    harness: str, content: str, output_dir: Path | None = None,
) -> Path:
    """raw 하네스 출력 + 메타를 파일로 박제한다 — O_EXCL·mode 0600·PID/UUID 원자 파일명(§3.4).

    external_review.save_output 형이나 보안 요구(원자 생성·0600 권한)를 더한다 — 감사용·충돌/권한
    유출 회귀 가드. 반환: 박제 파일 경로."""
    base_dir = output_dir or Path(_gettempdir())
    base_dir.mkdir(parents=True, exist_ok=True)
    name = f"pm_delegate_{harness}_{os.getpid()}_{uuid.uuid4().hex}.txt"
    dest = base_dir / name
    fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return dest


def _gettempdir() -> str:
    import tempfile
    return tempfile.gettempdir()


def _format_meta(argv: list[str], rc: int, harness: str, model: str,
                 elapsed: float, stdout: str, stderr: str, *,
                 attempt: str = "primary", primary_raw: str | None = None,
                 secret_scan_ack_digest: str | None = None,
                 secret_scan_ack_hits: tuple[PromptSecretHit, ...] = (),
                 secret_scan_ack_primary_recipient: str | None = None) -> str:
    """raw 박제 본문 — 메타(argv·rc·모델·소요) 헤더 + 원문.

    폴백 attempt 는 `# primary_raw:` 로 앞선 primary raw 경로를 적어 **raw 파일 하나만 봐도** 감사
    체인(왜 이 하네스로 갔는가)이 닫히게 한다."""
    header = [
        "# pm_delegate raw 출력 (감사)",
        f"# harness: {harness}",
        f"# model: {model}",
        f"# attempt: {attempt}",
    ]
    if primary_raw:
        header.append(f"# primary_raw: {primary_raw}")
    if secret_scan_ack_digest:
        header.append(
            f"# secret_scan_ack: explicit override · digest={secret_scan_ack_digest}"
            f" · 전 탐지={len(secret_scan_ack_hits)}"
        )
        if attempt != "primary" and secret_scan_ack_primary_recipient is not None:
            header.append(
                "# secret_scan_ack_binding: "
                f"결속=primary <{secret_scan_ack_primary_recipient}> "
                "· 이 attempt 는 폴백(재승인 없음)"
            )
        header.extend(
            f"# secret_scan_ack_hit: {index}/{len(secret_scan_ack_hits)} "
            f"· {hit.axis}축 판정 '{hit.pattern}' · 발췌 <{hit.excerpt}>"
            for index, hit in enumerate(secret_scan_ack_hits, start=1)
        )
    header += [
        f"# argv: {' '.join(argv)}",
        f"# rc: {rc}",
        f"# elapsed_sec: {elapsed:.1f}",
        f"# at: {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        "## stdout",
        stdout,
        "",
        "## stderr",
        stderr,
    ]
    return "\n".join(header)


# ── reply 추출 (§3.4·pm_relay 3파서 재사용) ────────────────────────────────────

def extract_reply(harness: str, stdout: str) -> str | None:
    """하네스 stdout 에서 최종 reply 텍스트를 추출한다(§3.4·pm_relay 파서 재사용).

    claude=parse_stream_json(session_id, result) → result · codex=parse_codex_json → reply ·
    opencode=parse_opencode_json → reply. reply 미추출(파싱 실패·빈 출력)은 None(호출자 fail-loud)."""
    relay = _load_relay()
    lines = stdout.splitlines()
    if harness == "claude":
        _sid, result = relay.parse_stream_json(lines)
        return result
    if harness == "codex":
        _tid, reply, _usage = relay.parse_codex_json(lines)
        return reply
    if harness == "opencode":
        _sid, reply = relay.parse_opencode_json(lines)
        return reply
    raise DelegateError(f"미지원 harness {harness!r} — reply 추출 불가.")


# ── 실행 seam (run_fn DI·§3.3·§5.3) ──────────────────────────────────────────

RunResult = dict  # {"returncode": int, "stdout": str, "stderr": str, "timed_out": bool}

# 폴백 발동용 실패 분류 — **양성 패턴만** 열거한다. 정상 판정(반려/must-fix)이나 임의 rc≠0를
# "인프라"로 추론하지 않는다. Codex CLI 관측/공식 upstream 표기:
#   · 한도: `rate_limit_reached` / `rate_limit_exceeded`, "Rate limit reached …",
#           "You've hit your usage limit.", `insufficient_quota` (HTTP 429 계열).
#   · 인증: `unexpected status 401 Unauthorized`, `invalid_api_key`, "not logged in" /
#           "please run codex login", OAuth `invalid_state`.
# **커버리지 경계(§help 에도 명시)**: 위 표기는 전부 **codex CLI 축의 실근거**다. claude
# ("Credit balance is too low" 류)·opencode(provider passthrough 오류) 고유 표기는 실측/문서 근거를
# 확보하기 전까지 **추가하지 않는다** — 추측 패턴은 오분류(=부당 폴백·요청 밖 하네스로 유료 재송신)
# 위험만 키운다. 미커버 표기는 미분류로 남아 기존 fail-loud 를 탄다(보수 방향·후속 실측 티켓).
# 스폰 실패/타임아웃/stall 은 패턴이 아니라 **엔진이 세팅한 명시 신호**(RUN_RESULT_* 키)로만 잡는다.
# 오분류 시 보수 방향은 None(폴백 안 함·기존 fail-loud)이다.
_INFRA_QUOTA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brate_limit_(?:reached|exceeded)\b", re.IGNORECASE),
    re.compile(r"\brate limit (?:has been )?(?:reached|exceeded)\b", re.IGNORECASE),
    re.compile(r"\byou(?:'ve| have) hit your usage limit\b", re.IGNORECASE),
    re.compile(r"\busage limit (?:has been )?reached\b", re.IGNORECASE),
    re.compile(r"\binsufficient_quota\b", re.IGNORECASE),
    re.compile(r"\b429\b[^\n]{0,120}\btoo many requests\b", re.IGNORECASE),
    re.compile(r"\btoo many requests\b[^\n]{0,120}\b429\b", re.IGNORECASE),
)
_INFRA_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:unexpected status\s+)?401 unauthorized\b", re.IGNORECASE),
    re.compile(r"\binvalid_api_key\b", re.IGNORECASE),
    re.compile(r"\bnot logged in\b", re.IGNORECASE),
    re.compile(r"\blogin required\b", re.IGNORECASE),
    re.compile(r"\bplease run [`'\"]?codex login\b", re.IGNORECASE),
    re.compile(
        r"(?:\bauthentication\b[^\n]{0,80}\binvalid_state\b"
        r"|\binvalid_state\b[^\n]{0,80}\bauthentication\b)",
        re.IGNORECASE,
    ),
)


# reply/프롬프트 본문을 실어 나르는 이벤트 필드 — error 이벤트 안에 있어도 스캔에서 제외한다
# (claude 는 실패 turn 의 최종 텍스트를 `result` 에, codex agent_message 는 `text` 에 싣는다).
_REPLY_TEXT_KEYS: frozenset[str] = frozenset({"result", "text", "content", "reply"})


def _is_error_event(event: dict) -> bool:
    """JSONL 이벤트가 하네스의 **진단(error) 이벤트**인지 — reply/echo 이벤트와 구분."""
    if event.get("is_error") is True:
        return True
    if event.get("error"):
        return True
    for key in ("type", "subtype"):
        value = event.get(key)
        if isinstance(value, str) and "error" in value.lower():
            return True
    item = event.get("item")
    if isinstance(item, dict):
        item_type = item.get("type")
        if isinstance(item_type, str) and "error" in item_type.lower():
            return True
    return False


def _diagnostic_strings(node, key: str | None = None) -> list[str]:
    """이벤트에서 진단 문자열만 수집(reply 본문 필드는 버림)."""
    if isinstance(node, str):
        return [] if key in _REPLY_TEXT_KEYS else [node]
    if isinstance(node, dict):
        collected: list[str] = []
        for child_key, child in node.items():
            collected.extend(_diagnostic_strings(child, child_key))
        return collected
    if isinstance(node, list):
        collected = []
        for child in node:
            collected.extend(_diagnostic_strings(child, key))
        return collected
    return []


def _failure_scan_text(result: RunResult) -> str:
    """한도/인증 패턴 스캔 대상 — stderr 전문 + stdout 의 **error 이벤트 진단 필드만**.

    stdout 전문을 스캔하면 **에이전트 reply·프롬프트 에코가 한도 문구를 인용하기만 해도** 폴백이
    발동한다(실측 재현 2건: codex `agent_message` 본문·user 프롬프트 에코 — 이 규칙 자체를 리뷰
    위임하면 자기참조로 재현된다). 하네스 stdout 은 JSONL 이벤트 스트림이고 진단은 error 이벤트에만
    담기므로, 스캔을 그 이벤트의 비-reply 필드로 좁혀 에코 유입을 원천 차단한다. 비-JSON/비-dict
    라인은 무시한다(pm_relay 3파서와 동일 robust 정책).
    """
    import json  # 지연 import — 분류 경로에서만 쓴다(pm_relay 파서 대칭).

    parts: list[str] = [result.get("stderr", "") or ""]
    for raw in (result.get("stdout", "") or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict) and _is_error_event(event):
            parts.extend(_diagnostic_strings(event))
    return "\n".join(parts)


def classify_infrastructure_failure(result: RunResult) -> str | None:
    """하네스 결과를 폴백 가능한 인프라 실패 클래스로 보수 분류한다.

    반환값은 loud 메시지/감사 provenance 에 쓰는 안정 문자열이다. 분류 근거는 둘뿐이다 —
    ① 엔진이 세팅한 명시 신호(launch 실패·timeout·opencode 첫-이벤트 stall), ② 실패 결과(rc≠0)의
    한도/인증 **양성 패턴**(스캔 범위는 _failure_scan_text — reply 에코 제외). rc=0 정상 완료는 출력
    내용과 무관하게 분류하지 않으며(반려/must-fix 판정은 PM 몫), 알려지지 않은 rc≠0 도 None 으로
    남겨 기존 fail-loud 를 유지한다.

    **rc=0 검사가 맨 앞**이다 — "정상 완료는 절대 폴백 안 함"은 문서 계약이므로 신호 세팅에 버그가
    나도(rc=0 인데 신호가 붙는 조합) 계약이 먼저 이긴다(codex suggestion·방어적 보장). 실제 엔진은
    성공 turn 에 신호를 붙이지 않는다(timeout=rc1·launch=rc127·stall=rc1).
    """
    rc = result.get("returncode", 1)
    if rc == 0:
        return None
    if result.get(RUN_RESULT_LAUNCH_FAILED):
        return FAILURE_CLASS_LAUNCH
    if bool(result.get("timed_out", False)):
        return FAILURE_CLASS_TIMEOUT
    # stall 은 엔진 신호가 1순위, stderr 마커는 백스톱(둘 다 엔진이 직접 찍는다 — 오분류 위험 0).
    if result.get(RUN_RESULT_STALLED) or OPENCODE_STALL_MARKER in (result.get("stderr", "") or ""):
        return FAILURE_CLASS_STALL
    output = _failure_scan_text(result)
    if any(pattern.search(output) for pattern in _INFRA_QUOTA_PATTERNS):
        return FAILURE_CLASS_QUOTA
    if any(pattern.search(output) for pattern in _INFRA_AUTH_PATTERNS):
        return FAILURE_CLASS_AUTH
    return None


def _default_run_fn(
    argv: list[str], *, stdin_text: str | None, cwd: str, env: dict[str, str],
    timeout: int, harness: str,
) -> RunResult:
    """실 subprocess 실행(테스트는 이 seam 을 mock). timeout 시 **프로세스그룹 종료**(3드라이버 공통·
    start_new_session + killpg·자식[모델 fetch·pytest 등] 잔존 방지·§5.3).

    opencode 는 첫-이벤트 워치독 경유(startup stall 유한 재시도·pm_relay 재사용·프롬프트는 --file 이라
    stdin 불요). codex/claude 는 stdin 으로 프롬프트 주입.

    **launch 오류 정규화**(codex must-fix): 하네스 바이너리 미설치/실행 불가(FileNotFoundError·
    PermissionError 등 **스폰 단계** 오류)는 traceback 으로 전파하지 않고 RunResult(rc≠0·진단 stderr)로
    감싼다 — 3드라이버 공통(external_review.run_reviewer 의 FileNotFoundError fail-soft 계약 동형).

    **스폰 단계 한정**(codex R2): 프롬프트를 이미 보낸 뒤의 I/O 오류(communicate 중 EPIPE 등)를 launch
    실패로 표시하면 폴백이 발동해 **같은 프롬프트가 외부로 중복 전송**된다. 그래서 launch 신호는
    스폰 지점 예외에만 붙이고, 실행-중 OSError 는 미분류 실패(rc=1)로 남겨 기존 fail-loud 를 태운다."""
    relay = _load_relay()
    if harness == "opencode":
        # 워치독이 내부에서 스폰한다 — 바이너리/권한 계열(_LAUNCH_STAGE_ERRORS)만 launch 로 보고
        # 나머지 OSError 는 실행-중으로 간주한다(전송 후 중복 송신 금지).
        try:
            completed = relay.run_with_first_event_watchdog(
                argv,
                first_event_timeout=relay.first_event_timeout_default(),
                overall_timeout=timeout,
                retries=relay.stall_retries_default(),
                cwd=str(cwd),
                env=env,
            )
            return {
                "returncode": completed.returncode,
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
                "timed_out": False,
            }
        except relay.StallWatchdogError as exc:
            # 첫-이벤트 stall(유한 재시도 소진) = 인프라 실패다 — rc=1/timed_out=False 로만
            # 정규화하면 분류가 누락돼 폴백이 불발한다. 명시 신호로 실어 보낸다.
            return {"returncode": 1, "stdout": "",
                    "stderr": f"{OPENCODE_STALL_MARKER} {exc}]",
                    "timed_out": False, RUN_RESULT_STALLED: True}
        except subprocess.TimeoutExpired:
            # 워치독이 프로세스그룹째 kill 후 TimeoutExpired 전파(§5.3·kill 은 워치독 소관).
            return {"returncode": 1, "stdout": "", "stderr": f"[opencode timeout {timeout}s]",
                    "timed_out": True}
        except _LAUNCH_STAGE_ERRORS as exc:
            return _launch_failure_result(harness, exc)
        except OSError as exc:
            return _midrun_failure_result(harness, exc)

    # codex / claude — stdin 주입 + 프로세스그룹 kill
    popen_kwargs: dict = dict(
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(cwd), env=env, text=True, encoding="utf-8", errors="replace",
    )
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover — POSIX 회귀 환경
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)   # ← 스폰 지점(여기서만 launch 신호)
    except OSError as exc:
        return _launch_failure_result(harness, exc)
    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
        return {"returncode": proc.returncode, "stdout": stdout or "", "stderr": stderr or "",
                "timed_out": False}
    except subprocess.TimeoutExpired:
        relay._kill_process_group(proc)  # 그룹째 종료(자식 잔존 방지·§5.3)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 — 이미 kill 됨·수확 실패는 무시
            stdout, stderr = "", ""
        return {"returncode": 1, "stdout": stdout or "", "stderr": stderr or "", "timed_out": True}
    except OSError as exc:
        # 프롬프트 전송 후 I/O 오류 — 폴백 대상 아님(중복 외부 전송 차단·fail-loud).
        relay._kill_process_group(proc)
        return _midrun_failure_result(harness, exc)


# 스폰 단계로 확신할 수 있는 예외들(바이너리 부재·실행 권한·경로 형상). 그 밖의 OSError 는 실행 중
# 발생했을 수 있으므로 launch 로 표시하지 않는다.
_LAUNCH_STAGE_ERRORS = (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError)


def _launch_failure_result(harness: str, exc: BaseException) -> RunResult:
    """스폰 실패를 명시 신호 + 진단이 붙은 RunResult 로 정규화한다(단일 출처)."""
    return {
        "returncode": 127,
        "stdout": "",
        "stderr": f"하네스 {harness} 실행 불가: {exc} — 설치/PATH 확인",
        "timed_out": False,
        RUN_RESULT_LAUNCH_FAILED: True,
    }


def _midrun_failure_result(harness: str, exc: BaseException) -> RunResult:
    """전송 후 실행-중 I/O 오류 — **분류 신호 없이** 실패로 남긴다(폴백 금지·중복 송신 차단)."""
    return {
        "returncode": 1,
        "stdout": "",
        "stderr": (f"하네스 {harness} 실행 중 I/O 오류: {exc} — 프롬프트가 이미 전송됐을 수 있어 "
                   "자동 폴백하지 않습니다(중복 외부 전송 차단). 결과를 확인하고 수동 재시도하세요."),
        "timed_out": False,
    }


class DelegateAttempt(NamedTuple):
    """단일 하네스 실행과 감사 raw 결과(폴백 재귀 없이 primary/fallback 각 1회)."""

    harness: str
    model: str
    argv: list[str]
    result: RunResult
    raw_path: Path


def _build_target_argv(
    harness: str,
    model: str,
    reasoning: str | None,
    role: str,
    cwd: Path,
    prompt_file: Path,
) -> list[str]:
    if harness == "codex":
        return build_codex_argv(model, reasoning, role, str(cwd))
    if harness == "claude":
        return build_claude_argv(model, reasoning, role)
    return build_opencode_argv(model, reasoning, role, str(cwd), str(prompt_file))


def _execute_attempt(
    *,
    harness: str,
    model: str,
    reasoning: str | None,
    role: str,
    cwd: Path,
    prompt: str,
    timeout: int,
    output_dir: Path | None,
    run_fn: Callable,
    attempt: str,
    primary_raw: str | None = None,
    secret_scan_ack_digest: str | None = None,
    secret_scan_ack_hits: tuple[PromptSecretHit, ...] = (),
    secret_scan_ack_primary_recipient: str | None = None,
) -> DelegateAttempt:
    """하네스 1회를 실행하고 raw를 박제한다.

    폴백도 같은 드라이버/권한축/env allowlist/timeout 계약을 탄다(그래서 폴백이 발동한 실행의 최악
    소요는 두 시도의 하네스별 예산 합이다·_harness_timeout_budget). opencode의 합성 prompt-file은
    attempt마다 만들고 즉시 정리한다. DI run_fn이 예외를 직접 raise해도 _default_run_fn과 **같은
    분류 신호**로 정규화한다 — 스폰 단계 예외(_LAUNCH_STAGE_ERRORS)만 RUN_RESULT_LAUNCH_FAILED 이고,
    그 밖의 OSError는 전송 후일 수 있어 미분류 실패로 남긴다(폴백 금지·중복 외부 전송 차단).
    """
    env = build_env(harness)
    stdin_text: str | None = None
    prompt_path: Path | None = None
    if harness == "opencode":
        prompt_path = save_raw_output("opencode_prompt", prompt, output_dir)
        argv = _build_target_argv(harness, model, reasoning, role, cwd, prompt_path)
    else:
        # prompt_file 인자는 opencode에서만 소비된다.
        argv = _build_target_argv(harness, model, reasoning, role, cwd, Path())
        stdin_text = prompt

    started = time.monotonic()
    try:
        try:
            result = run_fn(
                argv, stdin_text=stdin_text, cwd=str(cwd), env=env,
                timeout=timeout, harness=harness,
            )
        except _LAUNCH_STAGE_ERRORS as exc:
            result = _launch_failure_result(harness, exc)
        except OSError as exc:
            result = _midrun_failure_result(harness, exc)
    finally:
        elapsed = time.monotonic() - started
        if prompt_path is not None:
            try:
                prompt_path.unlink()
            except OSError:
                pass

    rc = result.get("returncode", 1)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    raw_path = save_raw_output(
        harness,
        _format_meta(
            argv, rc, harness, model, elapsed, stdout, stderr, attempt=attempt,
            primary_raw=primary_raw, secret_scan_ack_digest=secret_scan_ack_digest,
            secret_scan_ack_hits=secret_scan_ack_hits,
            secret_scan_ack_primary_recipient=secret_scan_ack_primary_recipient,
        ),
        output_dir,
    )
    return DelegateAttempt(harness, model, argv, result, raw_path)


# ── 위임 범위 밖 변경 감지 훅 (T-0462·delegate_scope 판정 재사용·never-block) ──────

class ScopeAudit(NamedTuple):
    """위임 **전체 단위**(primary + 폴백 attempt 포함) 범위 판정 입력."""

    scope: object                 # delegate_scope 모듈
    touches: tuple[str, ...]      # ticket frontmatter touches(미지정 = 허용 0)
    before: object                # delegate_scope.WorktreeState
    workspace: Path               # git toplevel(=--cwd 가 하위 디렉토리여도 판정 기준은 루트)


def _warn_dropped_touch(item: str, reason: str) -> None:
    """정규화 못 한 touches 항목을 loud 하게 알린다(드롭 = 그만큼 허용 범위가 좁아진다)."""
    print(
        f"경고: touches 항목 '{item}' 을 이 workspace 좌표로 해소하지 못해 범위 판정에서 뺍니다"
        f"({reason}).",
        file=sys.stderr,
    )


def begin_scope_audit(ticket: str | None, cwd: Path) -> ScopeAudit | None:
    """위임 실행 **직전** worktree 상태를 캡처한다(T-0462).

    호출 시점은 전송-전 게이트(opt-in·매핑·containment·denylist·재앵커·dry-run)를 **모두 통과한
    뒤**다 — 아무것도 실행하지 않은 경로에서 판정을 켜면 무의미한 git 호출·오탐만 는다.
    캡처/정규화 기준은 `--cwd` 가 아니라 **git toplevel** 이다 — repo 하위 디렉토리를 --cwd 로 주면
    슬롯 루트와 좌표가 어긋나 판정이 통째로 꺼진다. `--ticket` 이 없으면 touches=() 라 허용 경로가
    0이다(delegate_scope 계약 — 변경이 있으면 전부 경고). 판정 **준비** 실패(toplevel 해소·board
    로드·ticket 부재·git status 실패)는 위임을 막지 않는다 — loud 1줄 후 None(판정 생략)."""
    try:
        scope = _load_delegate_scope()
        workspace = scope.resolve_workspace_root(cwd)
        touches = scope.ticket_touches(BOARD_PY, ticket, pm_root=REPO) if ticket else ()
        before = scope.capture_worktree_state(workspace)
        # 해시 대상이 있는데 지문을 하나도 못 구했으면 이미 dirty 한 파일의 재수정을 못 잡는다 —
        # 강등된 채로 조용히 통과시키지 않는다(축소된 감지력을 PM 이 알아야 한다).
        degraded = scope.content_signal_missing(before, workspace)
    except Exception as exc:  # noqa: BLE001 — 판정 준비 실패는 비차단(traceback 대신 진단 1줄).
        print(f"경고: 위임 범위 판정 준비 실패({exc}) — 비차단 진행.", file=sys.stderr)
        return None
    if degraded:
        print(
            "경고: 내용 해시 보강 신호 없음 — 이미 dirty 한 파일의 재수정은 감지 불가"
            "(상태코드/mode 신호로만 판정).",
            file=sys.stderr,
        )
    return ScopeAudit(scope, touches, before, workspace)


def report_scope_audit(audit: ScopeAudit | None, role: str) -> None:
    """위임 **회수 시점**(모든 attempt 종료 후 1회)의 범위 밖 변경을 loud 경고한다(차단 아님).

    반환값/rc 를 바꾸지 않는다 — 격리/복원/수용 판정은 PM 몫이다(T-0462). 판정 자체가 실패해도
    위임 결과를 바꾸지 않는다(비차단 보험). 쓰기 허용 역할집합은 이 모듈의 WRITE_ROLES 를 주입해
    단일 출처로 쓴다(감지기 기본값과의 드리프트는 테스트가 막는다). raw 박제는 기본 /tmp 라 판정에
    안 잡히지만, `--output-dir` 를 repo 안으로 주면 그 산출물도 '위임이 만든 변경'으로 잡힌다(의도)."""
    if audit is None:
        return
    try:
        after = audit.scope.capture_worktree_state(audit.workspace)
        if audit.scope.head_moved(audit.before, after):
            # 커밋 자체가 역할 계약 위반 신호다(위임 역할은 commit/push 금지) — 범위 안이든 밖이든
            # 별도로 알린다. 커밋된 경로는 아래 판정 입력에 합산된다(worktree 는 clean 이라 무증거).
            print(
                "경고: 위임 중 커밋이 발생했습니다(위임 역할은 commit 금지) — "
                f"HEAD {audit.before.head or '(없음)'} → {after.head or '(없음)'}. "
                "커밋된 변경도 범위 판정에 합산합니다.",
                file=sys.stderr,
            )
        paths = audit.scope.out_of_scope_changes(
            audit.before, after,
            touches=audit.touches, role=role, pm_root=REPO, workspace=audit.workspace,
            write_roles=WRITE_ROLES, on_drop=_warn_dropped_touch,
        )
        warning = audit.scope.format_warning(paths)
    except Exception as exc:  # noqa: BLE001 — 판정 실패도 위임 결과를 바꾸지 않는다.
        print(f"경고: 위임 범위 판정 실패({exc}) — 비차단 진행.", file=sys.stderr)
        return
    if warning:
        print(warning, file=sys.stderr)


# ── native 단락 advisory (§3.6·never-block 백스톱) ────────────────────────────

def native_advisory(harness: str) -> str | None:
    """target 하네스 == PM 하네스면 "네이티브가 더 저렴" advisory 1줄(§3.6·never-block).

    PM 하네스 env 마커(codex CODEX_THREAD_ID·claude/opencode 마커)를 감지해 same-harness 위임이면
    경고 문자열을 반환한다(호출부가 stderr 로 냄). 1차 판정은 어댑터 스킬 카드(§3.6)·이건 백스톱."""
    pm_harness = None
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI"):
        pm_harness = "codex"
    elif os.environ.get("OPENCODE") or os.environ.get("OPENCODE_CONFIG"):
        pm_harness = "opencode"
    elif os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CONFIG_DIR"):
        pm_harness = "claude"
    if pm_harness == harness:
        return (f"[pm-delegate] target 하네스({harness}) == PM 하네스 — 네이티브 위임이 더 저렴하다"
                "(subprocess 스폰 불요). 어댑터 스킬 카드의 native 단락을 우선하라(§3.6·advisory).")
    return None


# ── config lint (동일-모델 dev/reviewer 경고·§3.7·never-block) ────────────────

# alias 정규화 테이블 키 접두 — `delegate.model_alias.<name> = m1, m2, …`(§3.7).
_MODEL_ALIAS_PREFIX = "delegate.model_alias."


def _lint_role_model(conf: dict[str, str], role: str, tier: str = "normal") -> str | None:
    """lint 용 역할 모델 해소 — 설정 키만 읽고 **fail-loud 하지 않는다**(미설정=None·skip).

    resolve_delegate 는 미설정 시 fail-loud 라 lint(설정 점검)엔 부적합하다 — lint 는 강제가
    아니라 정합 권고이므로 미매핑 역할은 조용히 건너뛴다(경고 대상 아님·§3.7)."""
    key = f"delegate.{role}" + (".hard" if tier == "hard" else "")
    model = (conf.get(f"{key}.model") or "").strip()
    return model or None


def _lint_alias_sets(conf: dict[str, str]) -> dict[str, set[str]]:
    """`delegate.model_alias.<name> = m1, m2, …` 를 (모델문자열 → 그 모델이 속한 alias명 **집합**)으로
    뒤집는다.

    서로 다른 표기(하네스별 이름·프로바이더 경로)의 같은 기반 모델을 alias 로 묶어 비교한다. 한 모델이
    **여러 alias 에 속할 수 있으므로 집합으로 모은다**(마지막 alias 가 덮어써 경고를 놓치던 문제 폐쇄·
    codex suggestion). 문자열 비교 + 명시 매핑 이상은 과설계 금지(family 자동추론·버전 파싱 없음·§3.7)."""
    out: dict[str, set[str]] = {}
    for key, value in conf.items():
        if not key.startswith(_MODEL_ALIAS_PREFIX):
            continue
        alias = key[len(_MODEL_ALIAS_PREFIX):].strip()
        if not alias:
            continue
        for member in re.split(r"[,\s]+", value or ""):
            member = member.strip()
            if member:
                out.setdefault(member, set()).add(alias)
    return out


def _lint_models_match(a: str, b: str, alias_sets: dict[str, set[str]]) -> bool:
    """두 모델 문자열이 같은 기반 모델인가 — 동일 문자열이거나 **alias 집합이 교차**하면 True(§3.7·
    하네스 무관).

    집합 교차라 한 모델이 여러 alias 에 속해도 공유 alias 를 놓치지 않는다(단일 대표 alias 덮어쓰기
    회귀 폐쇄). 비교는 alias **멤버십**(모델→속한 alias명)이지 이름-값이 아니므로, 실제 모델명이 우연히
    어떤 alias 명과 같은 문자열이어도 오인 매칭하지 않는다(collision-safe)."""
    if a == b:
        return True
    sa = alias_sets.get(a)
    sb = alias_sets.get(b)
    return bool(sa and sb and (sa & sb))


def lint_same_model(conf: dict[str, str]) -> list[tuple[str, str]]:
    """dev(normal+hard)와 code-reviewer 해소 모델이 같으면 (label, detail) 경고 리스트 반환(§3.7).

    **하네스 무관 모델 문자열 비교**(+선택적 alias 동치류 교차) — 같은 기반 모델을 서로 다른 하네스로
    돌려도 generate≈evaluate 침식은 동일하므로 `harness:model` 완전일치가 아니라 `.model` 문자열
    (또는 공유 alias)로 비교한다. 같으면 경고 1줄. **never-block** — lint 는 설정 점검이지 강제가
    아니다. 미설정 역할(reviewer 또는 특정 dev tier)은 조용히 skip(경고 대상 아님). **순수 함수**
    (I/O·board import 없음) — board lint 가 이 함수를 deep-import 로 호출해 advisory 로 표면화한다
    (순환 import 방지 — pm_delegate 는 board 를 import 하지 않는다)."""
    reviewer = _lint_role_model(conf, "code-reviewer")
    if reviewer is None:
        return []
    alias_sets = _lint_alias_sets(conf)
    findings: list[tuple[str, str]] = []
    for tier, label in (("normal", "developer"), ("hard", "developer.hard")):
        dev = _lint_role_model(conf, "developer", tier)
        if dev is None:
            continue
        if _lint_models_match(dev, reviewer, alias_sets):
            via = " (alias 경유)" if dev != reviewer else ""
            findings.append((
                f"delegate.{label}/code-reviewer",
                f"delegate.{label}(model={dev}) 와 delegate.code-reviewer(model={reviewer}) 가 "
                f"같은 모델로 해소됩니다{via} — generate≠evaluate 침식(같은 모델이 자기 산출을 "
                "검토). dev/reviewer 를 서로 다른 모델로 두길 권장합니다(하네스 무관·never-block)."
            ))
    return findings


def _cmd_lint(argv: list[str]) -> int:
    """`pm_delegate.py lint` — 동일-모델 dev/reviewer 경고(§3.7·never-block·항상 rc=0).

    설정 정합 점검일 뿐 강제가 아니다 — 경고가 있어도 rc=0(차단 금지). 경고는 stderr, 정합 시
    안내는 stdout. board lint advisory 훅과 짝을 이루는 명시 진입점."""
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py lint",
        description="delegate 설정 정합 점검 — 동일-모델 dev/reviewer 경고(never-block).")
    parser.parse_args(argv)  # 현재 플래그 0(미래 확장 여지·미지원 인자는 usage error).
    conf = local_config()
    findings = lint_same_model(conf)
    if not findings:
        print("delegate lint: 동일-모델 dev/reviewer 경고 없음(설정 정합).")
        return 0
    for _label, detail in findings:
        print(f"경고: {detail}", file=sys.stderr)
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py",
        description="cross-harness 역할 위임 채널 (ADR-0075·기본 OFF·delegate_enabled opt-in)",
        epilog=(
            "local.conf loud 폴백 예시(엔진 기본값 아님):\n"
            "  delegate.developer.fallback.harness=claude\n"
            "  delegate.developer.fallback.model=opus\n"
            "hard 티어는 delegate.developer.hard.fallback.* 처럼 별도 완전 세트를 설정합니다.\n"
            "폴백은 인프라 실패(스폰 실패·타임아웃·opencode 첫-이벤트 stall·한도/인증)에만 1회 —\n"
            "정상 완료 판정(반려·must-fix)은 대상이 아니고, --harness/--model 완전지정 실행이나\n"
            "폴백이 primary 와 같은 하네스/모델이면 loud 로 건너뜁니다. 폴백이 발동하면 최악 소요는\n"
            "primary·폴백 각 하네스 예산의 합입니다 — codex/claude 는 timeout, opencode 는 첫-이벤트\n"
            "워치독 재시도분(retries×창)이 더 붙습니다. --dry-run 이 실수치를 표시합니다(2차 폴백 없음).\n"
            "실패 분류 커버리지: 한도/인증 패턴은 현재 **codex CLI 축 실근거**만 담습니다 —\n"
            "claude/opencode 고유 표기(Credit balance 류)는 후속 실측 전까지 미포함이며, 미분류는\n"
            "폴백 없이 기존 fail-loud 로 처리됩니다."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--role", required=True, choices=ROLE_CHOICES,
                        help="위임 역할(권한축 자동 매핑)")
    parser.add_argument("--prompt-file", required=True, metavar="PATH",
                        help="PM 이 만든 task 프롬프트 파일(자족·repo 경계 안)")
    parser.add_argument("--cwd", required=True, metavar="ABSPATH",
                        help="위임 대상 작업공간(절대경로·모든 역할 필수)")
    parser.add_argument("--tier", default=None, choices=TIER_CHOICES,
                        help="developer 2티어(normal/hard·비-개발 역할 지정 시 usage error)")
    parser.add_argument("--harness", default=None, choices=HARNESS_CHOICES,
                        help="CLI override(--model 동반 필수·원자 tuple)")
    parser.add_argument("--model", default=None, metavar="PROFILE",
                        help="CLI override(--harness 동반 필수)")
    parser.add_argument("--reasoning", default=None, metavar="VAL",
                        help="reasoning override(--harness/--model 동반 시만·드라이버별 허용값·§6)")
    parser.add_argument("--timeout", type=int, default=None, metavar="SEC",
                        help=f"위임 turn 타임아웃(초·기본 {DELEGATE_TIMEOUT_SECONDS})")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="raw 출력 박제 디렉토리(기본 /tmp)")
    parser.add_argument("--ticket", default=None, metavar="T-NNNN",
                        help="위임 대상 ticket — touches 로 범위 밖 변경을 경고 판정"
                             "(생략 시 허용 경로 0·차단 아님)")
    parser.add_argument("--secret-scan-ack", default=None, metavar="DIGEST",
                        help="이번 합성 프롬프트의 §4.7 차단을 사람이 발췌 확인 후 건별 승인"
                             "(digest는 합성 프롬프트 전문 + 해소된 primary 수신자 harness:model에 결속"
                             "·차단 출력 digest와 정확히 일치할 때만 유효·CLI 전용)")
    parser.add_argument("--dry-run", action="store_true",
                        help="합성 프롬프트 요약 + argv 만 출력·미실행(비활성이어도 허용)")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """CLI 검증 — usage error(rc=2). 원자 tuple·tier 역할·cwd·timeout·ack 형식."""
    cwd_path = Path(args.cwd)
    if not cwd_path.is_absolute():
        parser.error("--cwd 는 절대경로여야 한다(모든 역할 필수·기본값 없음).")
    resolved_cwd = cwd_path.resolve()
    if resolved_cwd == resolved_cwd.parent:  # 파일시스템 루트(`/`·`C:\`)
        parser.error("--cwd 는 파일시스템 루트일 수 없다(작업공간 절대경로 요구·containment 우회 차단).")
    if args.tier is not None and args.role != "developer":
        parser.error("--tier 는 developer 전용이다(비-개발 역할 지정 = usage error·무시 아님).")
    if bool(args.harness) != bool(args.model):
        parser.error("--harness 와 --model 은 동반 필수(부분 override 금지·원자 tuple).")
    if args.reasoning is not None and not (args.harness and args.model):
        parser.error("--reasoning 은 --harness/--model 동반 시만 허용된다(§3.1).")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout 은 양의 정수여야 한다(0/음수 금지).")
    if (args.secret_scan_ack is not None
            and re.fullmatch(r"[0-9a-f]{24}", args.secret_scan_ack) is None):
        parser.error("--secret-scan-ack 승인 토큰은 24자리 소문자 hex여야 한다.")


def _resolve_timeout(args: argparse.Namespace, conf: dict[str, str]) -> int:
    """위임 turn 타임아웃(초)을 양의 정수로 해소한다(suggestion — traceback 방지).

    우선순위: `--timeout`(양수 — _validate_args 가 보장) > local.conf `delegate_timeout` > 기본. conf
    값은 CLI 가 아니라 usage error 가 부적합하므로 fail-soft(비정수/≤0 은 stderr 경고 후 기본·
    pm_import `_opencode_models_timeout` 선례). int() 예외로 죽던 경로를 닫는다."""
    if args.timeout is not None:
        return args.timeout  # _validate_args 가 >0 보장
    raw = (conf.get("delegate_timeout") or "").strip()
    if not raw:
        return DELEGATE_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        print(f"경고: local.conf delegate_timeout={raw!r} 비정수 — 기본 "
              f"{DELEGATE_TIMEOUT_SECONDS}s 사용.", file=sys.stderr)
        return DELEGATE_TIMEOUT_SECONDS
    if val <= 0:
        print(f"경고: local.conf delegate_timeout={val} ≤0 — 기본 "
              f"{DELEGATE_TIMEOUT_SECONDS}s 사용.", file=sys.stderr)
        return DELEGATE_TIMEOUT_SECONDS
    return val


def _harness_timeout_budget(harness: str, timeout: int) -> int:
    """하네스 **1회 실행**의 최악 소요 예산(초).

    codex/claude 는 turn timeout 그대로다. opencode 만 다르다 — 첫-이벤트 워치독
    (pm_relay.run_with_first_event_watchdog)이 **시도마다** overall 예산을 새로 잡으므로 단일 실행이
    timeout 을 넘을 수 있다. 다만 stall 로 죽는 시도는 overall 이 아니라 첫-이벤트 창에서 kill 되므로
    (`now >= first_deadline` 분기) 예산은 `timeout + retries×min(첫-이벤트 창, timeout)` 이다
    (기본 1800 + 2×90 = 1980s). relay 노브(env PM_OC_STALL_RETRIES·PM_OC_FIRST_EVENT_TIMEOUT)를 못
    읽으면 보수적으로 timeout 을 그대로 쓴다."""
    if harness != "opencode":
        return timeout
    try:
        relay = _load_relay()
        retries = max(0, int(relay.stall_retries_default()))
        first_event_window = float(relay.first_event_timeout_default())
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        return timeout
    return int(timeout + retries * min(first_event_window, timeout))


def main(argv: list[str] | None = None, run_fn: Callable | None = None,
         git_run_fn: Callable | None = None) -> int:
    _console_spec = importlib.util.spec_from_file_location(
        "_console_encoding", Path(__file__).resolve().with_name("console_encoding.py")
    )
    _console_encoding = importlib.util.module_from_spec(_console_spec)
    _console_spec.loader.exec_module(_console_encoding)
    _console_encoding.configure_console_utf8()
    # `lint` 서브커맨드 — flat 위임 옵션(--role/--prompt-file/--cwd required)과 분리한 별도 경로.
    # 위임과 인자 형상이 다르므로 build_arg_parser 앞에서 분기(subparsers 로 위임 required 를 흩지
    # 않는다). never-block(§3.7·항상 rc=0).
    resolved = list(sys.argv[1:] if argv is None else argv)
    if resolved and resolved[0] == "lint":
        return _cmd_lint(resolved[1:])
    parser = build_arg_parser()
    args = parser.parse_args(resolved)
    _validate_args(parser, args)

    conf = local_config()
    tier = args.tier if (args.role == "developer" and args.tier) else "normal"
    cwd = Path(args.cwd)

    # ① opt-in 게이트 (비-dry-run·기본 OFF·disabled = rc=3·§5.4). **매핑 해소보다 앞** — 기본 OFF +
    # 매핑 없는 새 설치는 "매핑 미설정"(rc=1)이 아니라 disabled(rc=3)로 응답해야 한다(codex must-fix).
    # dry-run 은 항상 미리보기 허용(미전송)이라 이 게이트를 통과시킨다.
    if not args.dry_run and not _is_enabled(conf):
        print(
            "delegate 비활성 — 외부 하네스 송신이 꺼져 있습니다 "
            f"(local.conf {DELEGATE_ENABLED_KEY}=false).\n"
            f"켜기: local.conf 에 `{DELEGATE_ENABLED_KEY}=true` 추가(외부 송신·과금 수용 계약). "
            "미리보기는 `--dry-run`.",
            file=sys.stderr,
        )
        return 3  # 명시적 비성공(rc=0 no-op 금지·빈 stdout 성공 오인 차단)

    # ② config 해소 (원자 tuple·fail-loud rc=1)
    try:
        harness, model, reasoning = resolve_delegate(
            conf, args.role, tier, args.harness, args.model, args.reasoning)
        fallback = resolve_fallback(conf, args.role, tier)
    except DelegateError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    # 폴백 **비발동** 판정(loud skip·설정은 그대로 두고 이번 실행만 끈다). 설정 자체는 위에서 해소해
    # 불완전 폴백 설정을 fail-loud 로 잡되(설정 정합은 override 와 무관), 아래 사유면 이번 실행에선
    # 쓰지 않는다 — "설정돼 있는데 안 썼다"를 정확히 말할 수 있어야 loud skip 이 성립한다:
    #   ① CLI 완전지정(--harness AND --model) = 설정 미참조 원자 override(resolve_delegate 불변) —
    #      일회성 명시 실행이 요청 밖 하네스로 넘어가면 그 불변이 깨진다.
    #   ② 폴백 tuple 의 하네스/모델이 primary 와 동일 — 한도 소진된 같은 채널을 유료로 재타격할 뿐이다
    #      (reasoning 만 다른 경우도 같은 계정/모델 한도라 skip 한다).
    fallback_skip: str | None = None
    if fallback is not None:
        if args.harness and args.model:
            fallback_skip = ("CLI 완전지정(--harness/--model)은 설정 미참조 원자 override — "
                             "설정 폴백을 쓰지 않는다")
            fallback = None
        elif (fallback[0], fallback[1]) == (harness, model):
            fallback_skip = (f"폴백 tuple 이 primary 와 동일({harness}/{model}) — "
                             "한도 소진된 같은 채널 재타격 금지")
            fallback = None

    # ③ cwd = git 저장소 루트/하위 검증 (광범위 홈 디렉토리 등 거부·경계 보강)
    if not _cwd_in_git_repo(cwd, git_run_fn):
        print(
            f"오류: --cwd 는 git 저장소 루트이거나 그 하위여야 합니다: {cwd}\n"
            "  광범위 경로(홈 디렉토리 등 non-repo)는 신뢰 작업공간이 아닙니다 — worktree/repo 를 지정하세요.",
            file=sys.stderr,
        )
        return 1

    # ④ prompt-file 존재 + 경로 자체 denylist(내용 읽기 전) + containment (repo 경계 안·유출 차단)
    prompt_file = Path(args.prompt_file)
    if not prompt_file.is_file():
        print(f"오류: --prompt-file 이 없음: {prompt_file}", file=sys.stderr)
        return 1
    path_pattern = _prompt_file_denylist_pattern(prompt_file)
    if path_pattern is not None:
        print(
            "오류: --prompt-file 경로/이름이 시크릿 denylist 패턴에 걸립니다 — 내용 읽기 전 차단합니다(§4.7).\n"
            f"  · 패턴 '{path_pattern}' 매칭. secret 파일을 프롬프트 소스로 넘기지 마세요.",
            file=sys.stderr,
        )
        return 1
    if not _prompt_file_contained(prompt_file, cwd):
        print(
            f"오류: --prompt-file 이 repo 경계 밖입니다: {prompt_file}\n"
            "  해소된 --cwd 하위 또는 이 repo PM 홈(.project_manager/) 하위만 허용됩니다(유출 경로 차단·§4.6).",
            file=sys.stderr,
        )
        return 1
    # 검증된 해소 경로에서 읽는다(symlink 우회 폐쇄 — denylist 는 원본+해소 양쪽을 이미 통과·must-fix 1).
    try:
        read_target = prompt_file.resolve()
    except OSError:
        read_target = prompt_file
    try:
        task_text = read_target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"오류: --prompt-file 읽기 실패: {exc}", file=sys.stderr)
        return 1

    # ⑤ 프롬프트 합성 (role preamble + task·§4.3)
    prompt = ROLE_PREAMBLES[args.role] + "\n\n" + task_text

    # ⑥ 쓰기-타깃 axis 재앵커 게이트 (엔진 코드 write + PM 홈 cwd·§4.6·dry-run 전 = 미리보기서 노출)
    reanchor = check_write_target_reanchor(args.role, cwd, prompt)
    if reanchor is not None:
        print(
            "오류: 엔진 코드(.project_manager/tools/) write 위임을 adopter#0 PM 홈 cwd 에서 실행했습니다 —\n"
            "  import 사본을 수정하면 canonical worktree 와 갈려 stale·false-green 이 납니다(§4.6).\n"
            f"  · canonical worktree 로 재앵커하세요:  --cwd {reanchor}\n"
            "  · PM-doc(wiki/ADR/spike) 작업이면 PM 홈 cwd 가 정당합니다 — 그 경우 프롬프트가 엔진 코드\n"
            "    경로를 write 타깃으로 지목하지 않게 하세요.",
            file=sys.stderr,
        )
        return 1

    # 드라이버 argv 준비 (dry-run·실행 공용). opencode 는 실행 시 합성 프롬프트를 임시 파일로 --file
    # 전달하므로, dry-run argv 는 사용자 prompt-file 경로로 표시(실행 시 합성 파일로 대체).
    argv_display = _build_target_argv(
        harness, model, reasoning, args.role, cwd, prompt_file,
    )

    # 타임아웃은 dry-run 미리보기(폴백 시간 예산 표시)와 실행이 같은 값을 쓴다.
    timeout = _resolve_timeout(args, conf)

    # 시크릿 판정은 dry-run과 실행이 같은 exhaustive 결과를 쓴다. 기존 첫-hit API/판정 순서는
    # `scan_prompt_secrets()`에 그대로 남고, 사람 승인 경로만 모든 hit를 끝까지 수집한다.
    secret_hits = scan_prompt_secret_hits(prompt)
    prompt_digest = (
        secret_scan_prompt_digest(prompt, harness, model) if secret_hits else None
    )

    # ⑦ dry-run — 합성 프롬프트 요약 + argv 출력·미실행 (비활성이어도 허용·rc=0)
    if args.dry_run:
        print("=== [dry-run] pm_delegate 미리보기 (미실행) ===")
        print(f"role: {args.role} · tier: {tier} · 권한축: {_perm_axis(args.role)}")
        print(f"해소: harness={harness} model={model} reasoning={reasoning}")
        if fallback_skip is not None:
            print(f"폴백: 비발동 — {fallback_skip}")
        elif fallback is None:
            print("폴백: 미설정 (인프라 실패 시 기존 fail-loud)")
        else:
            fallback_harness, fallback_model, fallback_reasoning = fallback
            primary_budget = _harness_timeout_budget(harness, timeout)
            fallback_budget = _harness_timeout_budget(fallback_harness, timeout)
            note = (" · opencode 는 첫-이벤트 워치독 재시도분 포함"
                    if "opencode" in (harness, fallback_harness) else "")
            print(
                "폴백: "
                f"harness={fallback_harness} model={fallback_model} "
                f"reasoning={fallback_reasoning} (인프라 실패에만 1회)"
            )
            print(
                f"폴백 시간 예산: 최악 primary {primary_budget}s + 폴백 {fallback_budget}s = "
                f"{primary_budget + fallback_budget}s (2차 폴백 없음{note})"
            )
            # 본체 타임아웃 예산만 — 프로세스 kill/wait·출력 회수 등 정리 오버헤드(수초~수십초)는
            # 하네스/플랫폼 의존이라 산입하지 않는다(codex R3). 외부 감시자가 이 수치를 하드
            # 데드라인으로 쓰면 완료 직전 종료될 수 있어 표기로 경고한다.
            print("  (본체 예산만 — kill/정리 오버헤드 수초~수십초 별도 · 외부 하드 데드라인으로 쓰지 말 것)")
        print(f"cwd: {cwd}")
        print(f"argv: {' '.join(argv_display)}")
        if harness == "opencode":
            print("  (opencode: 실행 시 role preamble 합성 프롬프트를 임시 파일로 --file 전달)")
        if secret_hits:
            print(f"시크릿 스캔: 탐지 {len(secret_hits)}건 — 실 실행은 전송 전 차단")
            print(_format_secret_scan_hits(secret_hits))
            print(f"승인 digest 미리보기: {prompt_digest}")
        else:
            print("시크릿 스캔: 통과 (탐지 0건)")
        print("--- 합성 프롬프트 ---")
        print(prompt)
        print("=== [dry-run] 외부 호출 생략 ===")
        return 0

    # ⑧ 시크릿 denylist 스캔 (합성 프롬프트·전송 전 차단·매칭 발췌 표시·값 마스킹·§4.7·T-0472)
    secret_scan_ack_digest: str | None = None
    secret_scan_ack_hits: tuple[PromptSecretHit, ...] = ()
    if secret_hits:
        if prompt_digest is None:
            print(
                "오류: 시크릿 탐지는 있었지만 승인 digest를 생성하지 못했습니다 — 전송 전 차단합니다.",
                file=sys.stderr,
            )
            return 1
        ack_matches = (
            args.secret_scan_ack is not None
            and hmac.compare_digest(args.secret_scan_ack, prompt_digest)
        )
        if ack_matches:
            secret_scan_ack_digest = prompt_digest
            secret_scan_ack_hits = secret_hits
            print(
                "시크릿 스캔 차단을 명시 승인으로 통과 — "
                f"발췌 <{secret_hits[0].excerpt}> · digest <{prompt_digest}> "
                f"· 전 탐지 {len(secret_hits)}건 확인\n"
                f"{_format_secret_scan_hits(secret_hits)}",
                file=sys.stderr,
            )
        else:
            mismatch = (
                "  · 승인 digest 불일치 — 프롬프트 또는 해소된 수신자 "
                "(harness:model)가 바뀜 · 발췌 재확인\n"
                if args.secret_scan_ack is not None else ""
            )
            print(
                "오류: 합성 프롬프트가 시크릿 denylist 판정에 걸렸습니다 — 전송 전 차단합니다(§4.7).\n"
                f"  · 전 탐지 {len(secret_hits)}건 — 아래 전체를 확인한 뒤에만 승인하세요.\n"
                f"{_format_secret_scan_hits(secret_hits)}\n"
                "  해당 텍스트를 프롬프트에서 제거하세요. 정상 식별자(conf 키 등)가 걸렸다면 판정 버그입니다.\n"
                f"{mismatch}"
                f"  · 승인 토큰: {prompt_digest}\n"
                f"  · 재실행: {_secret_scan_retry_command(resolved, prompt_digest)}",
                file=sys.stderr,
            )
            return 1

    # ⑨ 비용/송신 경고 + native advisory (§5.4·§3.6)
    print(f"외부 하네스 {harness}(model={model}) 로 프롬프트 전송 중 (과금·외부 송신).", file=sys.stderr)
    advisory = native_advisory(harness)
    if advisory is not None:
        print(advisory, file=sys.stderr)

    # ⑩ env allowlist 정제 + 실행 (§4.7·§3.3). 인프라 실패일 때만 명시 폴백을 같은 드라이버 계약으로
    # 1회 실행한다(최악 소요 = 두 시도의 하네스별 예산 합·2차 폴백 없음). 보안/재앵커 게이트는 이
    # 지점보다 앞이라 폴백 대상이 될 수 없다.
    output_dir = Path(args.output_dir) if args.output_dir else None
    _run = run_fn or _default_run_fn

    # ⑩-a 범위 판정 캡처 (T-0462) — **위임 전체 단위**로 1회. 폴백 attempt 도 같은 위임의 일부라
    # attempt 마다 재캡처하지 않는다(PM 이 회수 시점에 "이 위임이 범위 밖을 만졌나"를 본다). 아래
    # 실행·회수 블록의 모든 종료 경로(성공·폴백·fail-loud·예외)에서 finally 가 정확히 1회 보고한다.
    scope_audit = begin_scope_audit(args.ticket, cwd)
    try:
        return _execute_and_collect(
            args=args, harness=harness, model=model, reasoning=reasoning,
            fallback=fallback, fallback_skip=fallback_skip, cwd=cwd, prompt=prompt,
            timeout=timeout, output_dir=output_dir, run_fn=_run,
            secret_scan_ack_digest=secret_scan_ack_digest,
            secret_scan_ack_hits=secret_scan_ack_hits,
        )
    finally:
        report_scope_audit(scope_audit, args.role)


def _execute_and_collect(
    *,
    args: argparse.Namespace,
    harness: str,
    model: str,
    reasoning: str | None,
    fallback: tuple[str, str, str | None] | None,
    fallback_skip: str | None,
    cwd: Path,
    prompt: str,
    timeout: int,
    output_dir: Path | None,
    run_fn: Callable,
    secret_scan_ack_digest: str | None,
    secret_scan_ack_hits: tuple[PromptSecretHit, ...],
) -> int:
    """primary(+선택적 폴백) 실행과 결과 회수 — main 의 종료 rc 를 그대로 낸다(§3.4·§5.3).

    main 에서 분리한 이유는 위임 범위 판정(T-0462)이 **모든 종료 경로에서 정확히 1회** 돌아야 하기
    때문이다 — 호출부의 try/finally 가 그 경계다."""
    try:
        primary = _execute_attempt(
            harness=harness,
            model=model,
            reasoning=reasoning,
            role=args.role,
            cwd=cwd,
            prompt=prompt,
            timeout=timeout,
            output_dir=output_dir,
            run_fn=run_fn,
            attempt="primary",
            secret_scan_ack_digest=secret_scan_ack_digest,
            secret_scan_ack_hits=secret_scan_ack_hits,
            secret_scan_ack_primary_recipient=f"{harness}:{model}",
        )
    except OSError as exc:
        print(f"오류: 위임 실행 준비/raw 박제 실패: {exc}", file=sys.stderr)
        return 1

    # ⑪ 실패 분류 → 선택적 loud 폴백. rc=0 reply(반려/must-fix 포함)는 분류 함수가 절대 폴백시키지
    # 않는다. 알려지지 않은 rc≠0도 기존 fail-loud로 남는다(오분류 보수 방향).
    result = primary.result
    raw_path = primary.raw_path
    rc = result.get("returncode", 1)
    stdout = result.get("stdout", "")
    timed_out = result.get("timed_out", False)
    failure_class = classify_infrastructure_failure(result)

    if failure_class is not None and fallback is None and fallback_skip is not None:
        # 설정은 있으나 이번 실행에선 폴백을 끈다 — 조용히 지나가지 않는다(loud skip·ADR-0070 D5).
        print(
            f"폴백 비발동: 인프라 실패({failure_class})이지만 {fallback_skip}. "
            "기존 fail-loud 로 진행한다.",
            file=sys.stderr,
        )

    if failure_class is not None and fallback is not None:
        fallback_harness, fallback_model, fallback_reasoning = fallback
        loud = (
            f"폴백: {harness}→{fallback_harness}({fallback_model}) — "
            f"사유: {failure_class}"
        )
        print(loud, file=sys.stderr)
        print(
            f"외부 하네스 {fallback_harness}(model={fallback_model}) 로 폴백 프롬프트 전송 중 "
            "(과금·외부 송신·1단 폴백).",
            file=sys.stderr,
        )
        advisory = native_advisory(fallback_harness)
        if advisory is not None:
            print(advisory, file=sys.stderr)
        try:
            fallback_attempt = _execute_attempt(
                harness=fallback_harness,
                model=fallback_model,
                reasoning=fallback_reasoning,
                role=args.role,
                cwd=cwd,
                prompt=prompt,
                timeout=timeout,
                output_dir=output_dir,
                run_fn=run_fn,
                attempt=f"fallback-from-{harness}:{failure_class}",
                primary_raw=str(raw_path),
                secret_scan_ack_digest=secret_scan_ack_digest,
                secret_scan_ack_hits=secret_scan_ack_hits,
                secret_scan_ack_primary_recipient=f"{harness}:{model}",
            )
        except OSError as exc:
            print(
                f"오류: 폴백 실행 준비/raw 박제 실패: {exc}. primary raw: {raw_path}",
                file=sys.stderr,
            )
            return 1

        fallback_result = fallback_attempt.result
        fallback_rc = fallback_result.get("returncode", 1)
        fallback_stdout = fallback_result.get("stdout", "")
        if fallback_result.get("timed_out", False):
            print(
                f"오류: 폴백 위임 turn 타임아웃({timeout}s) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}",
                file=sys.stderr,
            )
            return 1
        if fallback_rc != 0:
            print(
                f"오류: 폴백 하네스 실패(rc={fallback_rc}) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}",
                file=sys.stderr,
            )
            return 1
        try:
            fallback_reply = (
                extract_reply(fallback_harness, fallback_stdout) if fallback_stdout else None
            )
        except (ValueError, UnicodeError, DelegateError) as exc:
            print(
                f"오류: 폴백 reply 추출 실패: {exc}. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}",
                file=sys.stderr,
            )
            return 1
        if not fallback_reply or not fallback_reply.strip():
            print(
                "오류: 폴백 위임 reply 미추출(빈 출력·파싱 실패) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}",
                file=sys.stderr,
            )
            return 1

        # stdout 결과에 provenance를 넣어 PM 회수 reply 자체가 폴백 사실을 보존한다.
        print(
            "[pm-delegate] 실행 provenance: "
            f"fallback={fallback_harness}(model={fallback_model}) · "
            f"primary={harness}(model={model}) · 사유={failure_class}"
        )
        if secret_scan_ack_digest is not None:
            print(
                "[pm-delegate] 실행 provenance: 시크릿 스캔 명시 승인 통과 · "
                f"digest={secret_scan_ack_digest}"
            )
        print(fallback_reply)
        print(
            f"[pm-delegate] primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}",
            file=sys.stderr,
        )
        return 0

    # 미설정 또는 비-인프라 실패 → 현행 fail-loud(rc=1 + stderr + raw 경로·§3.4).
    if timed_out:
        print(f"오류: 위임 turn 타임아웃({timeout}s) — 프로세스그룹 종료. raw: {raw_path}",
              file=sys.stderr)
        return 1
    if rc != 0:
        print(f"오류: 위임 하네스 실패(rc={rc}). raw: {raw_path}\n"
              "  네이티브/다른 하네스로 재시도를 검토하세요.", file=sys.stderr)
        return 1

    try:
        reply = extract_reply(harness, stdout) if stdout else None
    except (ValueError, UnicodeError, DelegateError) as exc:
        print(f"오류: reply 추출 실패: {exc}. raw: {raw_path}", file=sys.stderr)
        return 1
    if not reply or not reply.strip():
        print(f"오류: 위임 reply 미추출(빈 출력·파싱 실패). raw: {raw_path}", file=sys.stderr)
        return 1

    if secret_scan_ack_digest is not None:
        print(
            "[pm-delegate] 실행 provenance: 시크릿 스캔 명시 승인 통과 · "
            f"digest={secret_scan_ack_digest}"
        )
    print(reply)
    print(f"[pm-delegate] raw: {raw_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
