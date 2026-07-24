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
  · 시크릿 통제  — 합성 프롬프트 denylist 스캔 + subprocess env allowlist 정제 + prompt-file
                  containment(§4.7).
  · 결과 수집    — 최종 reply 텍스트만 stdout·raw+메타는 O_EXCL·0600·PID/UUID 파일 박제(§3.4).
  · opt-in 게이트 — `delegate_enabled`(기본 OFF) 비활성 시 rc=3 + stderr 안내(§5.4·false-green 차단).

설정 시드/lint(T-0446)·어댑터 배선(T-0447)·라이브 실측(T-0449)은 별도 티켓. 이 티켓은 라이브 CLI 를
호출하지 않는다 — 단위 테스트는 전부 mock(run_fn DI).
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable


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


# ── 도메인 상수 ────────────────────────────────────────────────────────────

HARNESS_CHOICES: tuple[str, ...] = ("claude", "codex", "opencode")
ROLE_CHOICES: tuple[str, ...] = ("developer", "researcher", "architect", "code-reviewer")
TIER_CHOICES: tuple[str, ...] = ("normal", "hard")

# 권한 역할축(§3.5) — write=저장소 파일 쓰기·read=저장소 read-only(+reviewer 는 테스트 실행).
WRITE_ROLES: frozenset[str] = frozenset({"developer", "architect"})
READ_ROLES: frozenset[str] = frozenset({"researcher", "code-reviewer"})

# 위임 turn 기본 타임아웃(초) — dev 는 reasoning+다중 편집으로 길다(codex driver TURN_TIMEOUT 600 보다
# 큼·§5.3). `--timeout`·local.conf `delegate_timeout` 로 override.
DELEGATE_TIMEOUT_SECONDS = 1800

# opt-in 게이트 키(기본 OFF·per-clone·ADR-0004 상속·§5.4).
DELEGATE_ENABLED_KEY = "delegate_enabled"

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

def _secret_path_candidates(raw: str) -> list[str]:
    """외부 전송 텍스트 토큰에서 경로 후보를 추출한다(denylist 스캔용·강화 토큰화).

    공백-토큰 하나를 (a) `=`/`:` 할당문 분리(예 `path=.env`·`key:secret.pem`) → 조각별로, (b) 양끝
    구두점 트리밍(마침표는 leading `.env` 보존 위해 trailing 만·`foo.pem.`→`foo.pem`·`.env.`→`.env`)
    후 후보로 낸다. 각 후보는 원문 + 소문자 정규화 2형을 담아 대소문자 표기(`.ENV`)도 잡는다."""
    candidates: list[str] = []
    for piece in re.split(r"[=:]", raw):
        token = piece.strip().strip("\"'`()[]{}<>,;!? ").rstrip(".")
        if not token:
            continue
        candidates.append(token)
        lowered = token.lower()
        if lowered != token:
            candidates.append(lowered)
    return candidates


def scan_prompt_secrets(prompt: str) -> tuple[str, str] | None:
    """합성 프롬프트에서 시크릿 denylist 패턴에 매칭되는 토큰을 찾는다(전송 전 차단·§4.7).

    external_review `_matching_denylist_pattern`(fnmatch 기반·`.env`·`*secret*`·`*.key` 등)을 재사용해
    프롬프트 텍스트를 스캔한다. 외부 전송 전 통제선이므로 토큰화를 강화한다(codex must-fix): `=`/`:`
    할당문 분리·양끝 구두점 트리밍·소문자 정규화 후 경로 후보를 추출해 패턴 매칭 — `.env`.·`path=.env`·
    `foo.pem.` 같은 표기를 놓치지 않는다. 반환: 매칭 시 (토큰, 패턴), 없으면 None. **한계 정직 표기**
    (§4.7): denylist 는 전송 텍스트 필터일 뿐 위임 프로세스의 cwd 파일 직접 읽기·env 상속은 못 막는다."""
    er = _load_external_review()
    patterns = er._SECRET_DENYLIST_PATTERNS
    for raw in prompt.split():
        for token in _secret_path_candidates(raw):
            pattern = er._matching_denylist_pattern(token, patterns)
            if pattern is not None:
                return token, pattern
    return None


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


def _prompt_file_denylist_pattern(prompt_file: Path) -> str | None:
    """prompt-file 이 시크릿 denylist 패턴에 걸리는가 — **원본 경로 + resolve() 해소 경로 양쪽** 검사
    (내용 읽기 전 차단·§4.7 b·symlink 우회 폐쇄).

    `prompt.md → <cwd>/.env` 같은 symlink 는 원본 이름(prompt.md)이 clean 이라 통과하나 resolve() 해소
    경로(.env)는 denylist 에 걸린다 — 양쪽을 검사해 symlink 를 통한 secret 읽기를 차단한다(codex must-fix).
    external_review `_matching_denylist_pattern`(fnmatch·`.env`·`*credential*`·`*.key`) 재사용. 걸린
    패턴명 반환(원문 토큰 미노출)."""
    er = _load_external_review()
    patterns = er._SECRET_DENYLIST_PATTERNS
    candidates = [str(prompt_file)]
    try:
        candidates.append(str(prompt_file.resolve()))
    except OSError:
        pass
    for cand in candidates:
        pattern = er._matching_denylist_pattern(cand, patterns)
        if pattern is not None:
            return pattern
    return None


# ── 쓰기-타깃 axis 재앵커 (§4.6) ─────────────────────────────────────────────

def _prompt_targets_engine_code(prompt: str) -> bool:
    """위임 프롬프트가 엔진 코드 경로(`.project_manager/tools/`)를 write 대상으로 하는가(§4.6).

    정확 문자열 매칭이 아니라 **경로를 정규화해 성분 시퀀스**로 판정한다(codex must-fix) — 각 토큰을
    PurePosixPath 로 정규화(`.`/중복 슬래시 접힘)해 `.project_manager` 직후 `tools` 성분이 오면 True.
    이로써 `.project_manager/tools`(trailing slash 없음)·`.project_manager/./tools/x.py` 같은 우회 표기를
    닫는다. write 역할 + PM 홈 cwd 조합에서만 재앵커 게이트로 쓰인다(PM-doc/wiki write 는 PM 홈 정당)."""
    for raw in prompt.split():
        token = raw.strip("\"'`()[]{}<>,;:!?").rstrip(".")
        if not token or ".project_manager" not in token:
            continue
        parts = PurePosixPath(token.replace("\\", "/")).parts
        for i in range(len(parts) - 1):
            if parts[i] == ".project_manager" and parts[i + 1] == "tools":
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
                 elapsed: float, stdout: str, stderr: str) -> str:
    """raw 박제 본문 — 메타(argv·rc·모델·소요) 헤더 + 원문."""
    header = [
        "# pm_delegate raw 출력 (감사)",
        f"# harness: {harness}",
        f"# model: {model}",
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


def _default_run_fn(
    argv: list[str], *, stdin_text: str | None, cwd: str, env: dict[str, str],
    timeout: int, harness: str,
) -> RunResult:
    """실 subprocess 실행(테스트는 이 seam 을 mock). timeout 시 **프로세스그룹 종료**(3드라이버 공통·
    start_new_session + killpg·자식[모델 fetch·pytest 등] 잔존 방지·§5.3).

    opencode 는 첫-이벤트 워치독 경유(startup stall 유한 재시도·pm_relay 재사용·프롬프트는 --file 이라
    stdin 불요). codex/claude 는 stdin 으로 프롬프트 주입.

    **launch 오류 정규화**(codex must-fix): 하네스 바이너리 미설치/실행 불가(FileNotFoundError·OSError)는
    traceback 으로 전파하지 않고 RunResult(rc≠0·진단 stderr)로 감싼다 — 3드라이버 공통(external_review.
    run_reviewer 의 FileNotFoundError fail-soft 계약 동형). 이로써 main 의 rc=1+진단+raw 실패 계약을
    우회하는 traceback 을 원천 차단한다."""
    relay = _load_relay()
    try:
        if harness == "opencode":
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
                return {"returncode": 1, "stdout": "", "stderr": f"[opencode 첫-이벤트 stall: {exc}]",
                        "timed_out": False}
            except subprocess.TimeoutExpired:
                # 워치독이 프로세스그룹째 kill 후 TimeoutExpired 전파(§5.3·kill 은 워치독 소관).
                return {"returncode": 1, "stdout": "", "stderr": f"[opencode timeout {timeout}s]",
                        "timed_out": True}

        # codex / claude — stdin 주입 + 프로세스그룹 kill
        popen_kwargs: dict = dict(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(cwd), env=env, text=True, encoding="utf-8", errors="replace",
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover — POSIX 회귀 환경
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(argv, **popen_kwargs)
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
    except (FileNotFoundError, OSError) as exc:
        # launch 실패(바이너리 부재·PATH·실행 권한 등) — 3드라이버 공통 정규화(traceback 금지).
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": f"하네스 {harness} 실행 불가: {exc} — 설치/PATH 확인",
            "timed_out": False,
        }


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
    parser.add_argument("--dry-run", action="store_true",
                        help="합성 프롬프트 요약 + argv 만 출력·미실행(비활성이어도 허용)")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """CLI 검증 — usage error(rc=2). 원자 tuple·tier 역할 제한·cwd 절대경로/비-루트·timeout 양수."""
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


def _reconfigure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def main(argv: list[str] | None = None, run_fn: Callable | None = None,
         git_run_fn: Callable | None = None) -> int:
    _reconfigure_streams()
    # `lint` 서브커맨드 — flat 위임 옵션(--role/--prompt-file/--cwd required)과 분리한 별도 경로.
    # 위임과 인자 형상이 다르므로 build_arg_parser 앞에서 분기(subparsers 로 위임 required 를 흩지
    # 않는다). never-block(§3.7·항상 rc=0).
    resolved = list(sys.argv[1:] if argv is None else argv)
    if resolved and resolved[0] == "lint":
        return _cmd_lint(resolved[1:])
    parser = build_arg_parser()
    args = parser.parse_args(argv)
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
    except DelegateError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

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
    if harness == "codex":
        argv_display = build_codex_argv(model, reasoning, args.role, str(cwd))
    elif harness == "claude":
        argv_display = build_claude_argv(model, reasoning, args.role)
    else:  # opencode
        argv_display = build_opencode_argv(model, reasoning, args.role, str(cwd), str(prompt_file))

    # ⑦ dry-run — 합성 프롬프트 요약 + argv 출력·미실행 (비활성이어도 허용·rc=0)
    if args.dry_run:
        print("=== [dry-run] pm_delegate 미리보기 (미실행) ===")
        print(f"role: {args.role} · tier: {tier} · 권한축: {_perm_axis(args.role)}")
        print(f"해소: harness={harness} model={model} reasoning={reasoning}")
        print(f"cwd: {cwd}")
        print(f"argv: {' '.join(argv_display)}")
        if harness == "opencode":
            print("  (opencode: 실행 시 role preamble 합성 프롬프트를 임시 파일로 --file 전달)")
        print("--- 합성 프롬프트 ---")
        print(prompt)
        print("=== [dry-run] 외부 호출 생략 ===")
        return 0

    # ⑧ 시크릿 denylist 스캔 (합성 프롬프트·전송 전 차단·패턴명만 노출·§4.7)
    secret = scan_prompt_secrets(prompt)
    if secret is not None:
        _token, pattern = secret
        print(
            "오류: 합성 프롬프트가 시크릿 denylist 패턴에 매칭됩니다 — 전송 전 차단합니다(§4.7).\n"
            f"  · 패턴 '{pattern}' 매칭. secret 파일 경로/이름을 프롬프트에서 제거하세요.",
            file=sys.stderr,
        )
        return 1

    # ⑨ 비용/송신 경고 + native advisory (§5.4·§3.6)
    print(f"외부 하네스 {harness}(model={model}) 로 프롬프트 전송 중 (과금·외부 송신).", file=sys.stderr)
    advisory = native_advisory(harness)
    if advisory is not None:
        print(advisory, file=sys.stderr)

    # ⑩ env allowlist 정제 + 실행 (§4.7·§3.3)
    env = build_env(harness)
    timeout = _resolve_timeout(args, conf)
    output_dir = Path(args.output_dir) if args.output_dir else None
    _run = run_fn or _default_run_fn

    # opencode 는 합성 프롬프트를 임시 파일로 박제해 --file 전달(§3.3), codex/claude 는 stdin.
    stdin_text: str | None = None
    prompt_path: Path | None = None
    try:
        if harness == "opencode":
            prompt_path = save_raw_output("opencode_prompt", prompt, output_dir)
            argv = build_opencode_argv(model, reasoning, args.role, str(cwd), str(prompt_path))
        else:
            argv = argv_display
            stdin_text = prompt
    except OSError as exc:
        print(f"오류: 합성 프롬프트 임시 파일 박제 실패: {exc}", file=sys.stderr)
        return 1

    # 합성 프롬프트 임시 파일은 run 후 finally 에서 삭제한다(suggestion — raw 결과만 보존·프롬프트
    # 내용이 tmp 에 잔존하지 않게). raw 출력 박제는 아래에서 별도 파일로 남긴다.
    started = time.monotonic()
    try:
        result = _run(argv, stdin_text=stdin_text, cwd=str(cwd), env=env,
                      timeout=timeout, harness=harness)
    finally:
        elapsed = time.monotonic() - started
        if prompt_path is not None:
            try:
                prompt_path.unlink()
            except OSError:
                pass

    # ⑪ 결과 수집 — raw 박제 + reply 추출 (§3.4). raw 저장·reply 추출의 OSError/UnicodeError·파싱
    # 예외도 rc=1 진단으로 정규화(traceback 금지·suggestion).
    rc = result.get("returncode", 1)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    timed_out = result.get("timed_out", False)
    try:
        raw_path = save_raw_output(
            harness, _format_meta(argv, rc, harness, model, elapsed, stdout, stderr), output_dir)
    except OSError as exc:
        print(f"오류: raw 출력 박제 실패: {exc}", file=sys.stderr)
        return 1

    try:
        reply = extract_reply(harness, stdout) if stdout else None
    except (ValueError, UnicodeError, DelegateError) as exc:
        print(f"오류: reply 추출 실패: {exc}. raw: {raw_path}", file=sys.stderr)
        return 1

    # timeout·rc≠0·빈 reply·파싱 실패 → fail-loud(rc=1 + stderr + raw 경로·§3.4)
    if timed_out:
        print(f"오류: 위임 turn 타임아웃({timeout}s) — 프로세스그룹 종료. raw: {raw_path}",
              file=sys.stderr)
        return 1
    if rc != 0:
        print(f"오류: 위임 하네스 실패(rc={rc}). raw: {raw_path}\n"
              "  네이티브/다른 하네스로 재시도를 검토하세요.", file=sys.stderr)
        return 1
    if not reply or not reply.strip():
        print(f"오류: 위임 reply 미추출(빈 출력·파싱 실패). raw: {raw_path}", file=sys.stderr)
        return 1

    print(reply)
    print(f"[pm-delegate] raw: {raw_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
