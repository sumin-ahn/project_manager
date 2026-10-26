"""위임 하네스 실행과 결과 수집 (층: infra)."""
from __future__ import annotations

from typing import NamedTuple
from pathlib import Path
from pm_engine.infra.harness import contract
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_errors
from pm_engine.domain import delegate_run_rules
import re
from pm_engine.domain import reviewer_result
import subprocess
from pm_engine.infra.harness import watchdog



def _observed_must_fix_items(reply: str | None) -> list[str]:
    """회신에서 must_fix 항목을 **생산 시점에** 뽑아 장부에 실을 목록으로 만든다.

    다음 라운드의 delta 는 이 목록만 읽는다 — raw 박제 텍스트를 나중에 다시 파싱하지 않는다.
    추출기는 추가 리뷰어 축이 이미 쓰는 것을 그대로 재사용한다(파서를 새로 만들지 않는다).

    **통과 응답의 `- 없음` 은 항목이 아니다.** 그 표기를 그대로 실으면 장부에 `["없음"]` 이 남고,
    다음 라운드 delta 가 "없음"을 고칠 지적으로 되읽는다. 정규화도 추가 리뷰 경로와 **같은
    술어**(`_is_none_items`)를 쓴다 — 두 축이 각자 판별하면 같은 회신이 축마다 다르게 박제된다.
    """
    if not reply or not reply.strip():
        return []
    items = reviewer_result._extract_must_fix_items(reply)
    items = [item for item in items if item and item.strip()]
    return [] if reviewer_result._is_none_items(items) else items


# ── reply 추출 (contract 3파서 재사용) ────────────────────────────────────

def extract_reply(harness: str, stdout: str) -> str | None:
    """하네스 stdout 에서 최종 reply 텍스트를 추출한다(공용 추출기 wrapper).

    claude=parse_stream_json → result · codex=parse_codex_json → reply ·
    opencode=parse_opencode_json → reply. reply 미추출(파싱 실패·빈 출력)은 None(호출자 fail-loud)."""
    try:
        return contract.extract_harness_reply(harness, stdout)
    except contract.HarnessContractError as exc:
        raise delegate_errors.DelegateError(str(exc)) from exc

# 폴백 발동용 실패 분류 — **양성 패턴만** 열거한다. 정상 판정(반려/must-fix)이나 임의 rc≠0를
# "인프라"로 추론하지 않는다. 세 하네스 CLI 관측/공식 upstream 표기:
#   · 한도: `rate_limit_reached` / `rate_limit_exceeded`, "Rate limit reached …",
#           "You've hit your usage limit.", `insufficient_quota` (HTTP 429 계열).
#   · 인증: `unexpected status 401 Unauthorized`, `invalid_api_key`, "not logged in" /
#           "please run codex login", OAuth `invalid_state`.
# **커버리지 경계(§help 에도 명시)**: 기존 codex 실근거에 claude 2.1.220 stdout JSONL 실측/
# 바이너리 enum, opencode 1.18.4 provider responseBody passthrough 실측과 Anthropic API enum을
# 편입했다. opencode 연결 거부·첫 provider 패키지 fetch stall은 stdout/stderr 무진단 침묵이라
# 패턴이 아니라 기존 첫-이벤트 stall 축이 커버한다. `Model not found` 같은 카탈로그/config 오류는
# 한도/인증이 아니므로 의도적으로 미분류해 fail-loud 를 유지한다.
# 스폰 실패/타임아웃/stall 은 패턴이 아니라 **엔진이 세팅한 명시 신호**(RUN_RESULT_* 키)로만 잡는다.
# 형제-가지 여집합: `overloaded`/`server_error`(transient지만 한도/인증 아님), `invalid_request`,
# `oauth_org_not_allowed`(org 정책·실측 없음), `APIError`(5xx도 쓰는 과광범 name), 중복 진단인
# "API key is invalid"는 추가하지 않는다. 그 밖/오분류 시 보수 방향은 None(폴백 없이 fail-loud)이다.
_INFRA_QUOTA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brate_limit_(?:reached|exceeded)\b", re.IGNORECASE),
    re.compile(r"\brate limit (?:has been )?(?:reached|exceeded)\b", re.IGNORECASE),
    re.compile(r"\byou(?:'ve| have) hit your usage limit\b", re.IGNORECASE),
    re.compile(r"\busage limit (?:has been )?reached\b", re.IGNORECASE),
    re.compile(r"\binsufficient_quota\b", re.IGNORECASE),
    re.compile(r"\b429\b[^\n]{0,120}\btoo many requests\b", re.IGNORECASE),
    re.compile(r"\btoo many requests\b[^\n]{0,120}\b429\b", re.IGNORECASE),
    # claude **내부 enum** 계열은 **단독 줄 앵커** — _failure_scan_text 가 진단 문자열을 줄로
    # 연결하므로 독립 JSON 값(`"error":"rate_limit"` 류)은 항상 단독 줄이고, 스캔 대상 비-reply
    # 필드(예: permission_denials 의 command 에코)에 실린 산문 문장-중간 표기는 구조적으로
    # 배제된다(짧은 단독 토큰의 오분류=부당 폴백 차단·`\s*` 는 CRLF/공백 보험).
    # 반면 opencode passthrough 계열(아래 rate_limit_error 등)은 responseBody **JSON 문자열 안에**
    # 박혀 오므로 앵커 금지 — substring `\b` 를 유지해야 실측 양성이 산다(비대칭은 의도).
    # claude 2.1.220 바이너리의 429/usage-limit/credits 경로 `_u({error:"rate_limit"})`.
    re.compile(r"^\s*rate_limit\s*$", re.IGNORECASE | re.MULTILINE),
    # claude 2.1.220 바이너리의 credit-balance HTTP 400 경로 error enum.
    re.compile(r"^\s*billing_error\s*$", re.IGNORECASE | re.MULTILINE),
    # Anthropic 공식 429 enum; opencode는 상류 API enum을 responseBody에 verbatim passthrough한다.
    re.compile(r"\brate_limit_error\b", re.IGNORECASE),
    # Anthropic 공식 메시지이자 claude 2.1.220 바이너리 자체 감지 regex와 동형.
    re.compile(r"\bcredit balance (?:is )?too low\b", re.IGNORECASE),
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
    # claude 2.1.220 미로그인 assistant/error 및 무효-key api_retry 이벤트의 error enum —
    # claude 내부 enum 이라 단독-줄 앵커(사유는 _INFRA_QUOTA_PATTERNS 블록 주석).
    re.compile(r"^\s*authentication_failed\s*$", re.IGNORECASE | re.MULTILINE),
    # opencode 1.18.4 responseBody passthrough 실측 + Anthropic 공식 401 error enum.
    re.compile(r"\bauthentication_error\b", re.IGNORECASE),
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


def _failure_scan_text(result: watchdog.RunResult) -> str:
    """한도/인증 패턴 스캔 대상 — stderr 전문 + stdout 의 **error 이벤트 진단 필드만**.

    stdout 전문을 스캔하면 **에이전트 reply·프롬프트 에코가 한도 문구를 인용하기만 해도** 폴백이
    발동한다(실측 재현 2건: codex `agent_message` 본문·user 프롬프트 에코 — 이 규칙 자체를 리뷰
    위임하면 자기참조로 재현된다). 하네스 stdout 은 JSONL 이벤트 스트림이고 진단은 error 이벤트에만
    담기므로, 스캔을 그 이벤트의 비-reply 필드로 좁혀 에코 유입을 원천 차단한다. 비-JSON/비-dict
    라인은 무시한다(`contract` 3파서와 동일 robust 정책).
    """
    import json  # 지연 import — 분류 경로에서만 쓴다(`contract` 파서 대칭).

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


# ── 재개 대상 세션 부재 (양성 패턴·재실행 자격) ────────────────────────────────
# 재개 실패의 **재실행 자격**은 두 가지뿐이다: 깨끗한 완료의 명시적 세션 id 불일치, 그리고 여기
# 잡히는 "그 세션이 없다"는 확정 오류다. 세션이 없으면 delta 프롬프트는 소비되지 않았으므로
# (호출 자체가 성립하지 않는다) full payload 재실행은 중복 과금·중복 호출이 아니다.
# 그 밖의 미분류 rc≠0 은 **호출 후 실패**일 수 있어 재실행하지 않는다(기존 fail-loud).
# 스캔 범위는 인프라 분류와 같은 `_failure_scan_text` 다 — reply 에코가 자격을 지어내지 못한다.
_RESUME_SESSION_MISSING_PATTERNS: tuple[re.Pattern[str], ...] = (
    # claude: `--resume <id>` 대상이 없을 때(다른 cwd·만료·오타) — 실측 문구.
    re.compile(r"\bno conversation found\b", re.IGNORECASE),
    # opencode `-s <id>` / 일반 하네스 표기.
    re.compile(r"\bsession not found\b", re.IGNORECASE),
    # codex 계열 표기(대화 id 미존재).
    re.compile(r"\bconversation not found\b", re.IGNORECASE),
    # codex-cli 0.147.0 실측(2026-08-12):
    # `Error: ... no rollout found for thread id <uuid> (code -32600)`.
    # phrase만 비슷한 다른 RPC 실패를 세션 부재로 추정하지 않도록 실측 code까지 함께 요구한다.
    re.compile(
        r"\bno rollout found for thread id\b[^\r\n]*\(code\s+-32600\)",
        re.IGNORECASE,
    ),
)
# 재실행 자격 사유 — loud 안내가 "무엇을 보고 다시 태우는지" 그대로 말한다.
RESUME_RERUN_ID_MISMATCH = "회신 세션 id 불일치"
RESUME_RERUN_SESSION_MISSING = "재개 대상 세션 없음"


def is_resume_session_missing(result: watchdog.RunResult) -> bool:
    """재개 대상 세션이 **없다고 확정된** 실패인가 (양성 패턴 일치·아니면 False).

    rc=0 정상 완료는 대상이 아니다 — 그쪽 판정은 회신 세션 id 일치가 소유한다.
    """
    if result.get("returncode", 1) == 0:
        return False
    text = _failure_scan_text(result)
    return any(pattern.search(text) for pattern in _RESUME_SESSION_MISSING_PATTERNS)


def resume_rerun_reason(
    result: watchdog.RunResult, observed_session_id: str | None, requested_session_id: str,
) -> str | None:
    """fresh + full payload 재실행 자격 (없으면 None) — 자격 사유 문자열은 안내에 그대로 쓴다.

    자격은 둘뿐이다:
      · **깨끗한 완료의 명시적 불일치** — rc=0 으로 끝났는데 회신이 말한 세션 id 가 요청한 id 와
        다르다. 맥락 없는 세션이 delta 만 받고 답한 라운드라 그대로 두면 수렴 추이가 오염된다.
      · **확정된 세션 없음** — 재개 대상이 없다는 오류(`is_resume_session_missing`). 세션이 없으니
        delta 는 소비되지 않았고, 재실행이 중복 호출이 아니다.

    나머지(미분류 `rc≠0`·관측 실패로 세션 id 를 못 읽은 rc=0)는 **재실행하지 않는다**. 호출 후에
    죽은 실행일 수 있어 full payload 를 다시 태우면 같은 라운드가 두 번 과금·호출된다.
    """
    if is_resume_session_missing(result):
        return RESUME_RERUN_SESSION_MISSING
    if result.get("returncode", 1) != 0:
        return None
    if observed_session_id is None or observed_session_id == requested_session_id:
        return None
    return RESUME_RERUN_ID_MISMATCH


def classify_infrastructure_failure(result: watchdog.RunResult) -> str | None:
    """하네스 결과를 폴백 가능한 인프라 실패 클래스로 보수 분류한다.

    반환값은 loud 메시지/감사 provenance 에 쓰는 안정 문자열이다. 분류 근거는 둘뿐이다 —
    엔진이 세팅한 명시 신호(launch 실패·timeout·opencode 첫-이벤트 stall), 실패 결과(rc≠0)의
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
    if result.get(delegate_run_rules.RUN_RESULT_LAUNCH_FAILED):
        return delegate_run_rules.FAILURE_CLASS_LAUNCH
    if bool(result.get("timed_out", False)):
        return delegate_run_rules.FAILURE_CLASS_TIMEOUT
    # stall 은 엔진 신호가 1순위, stderr 마커는 백스톱(둘 다 엔진이 직접 찍는다 — 오분류 위험 0).
    if result.get(delegate_run_rules.RUN_RESULT_STALLED) or delegate_run_rules.OPENCODE_STALL_MARKER in (result.get("stderr", "") or ""):
        return delegate_run_rules.FAILURE_CLASS_STALL
    output = _failure_scan_text(result)
    if any(pattern.search(output) for pattern in _INFRA_QUOTA_PATTERNS):
        return delegate_run_rules.FAILURE_CLASS_QUOTA
    if any(pattern.search(output) for pattern in _INFRA_AUTH_PATTERNS):
        return delegate_run_rules.FAILURE_CLASS_AUTH
    return None


def _timeout_failure_result(harness: str, timeout: int,
                            exc: subprocess.TimeoutExpired) -> watchdog.RunResult:
    """타임아웃(무진행/벽시계) 결과 정규화 — **kill 시점까지 받은 출력을 보존**한다.

    워치독이 프로세스 그룹째 kill 한 뒤 부분 산출물을 예외에 실어 올린다. 그걸 버리면 수십 분어치
    작업이 0바이트가 된다(kill 된 실행의 raw 가 헤더 138바이트뿐이던 실측). 무진행 kill 은 `idle_seconds` 로
    벽시계 백스톱과 구분해 사유·침묵 초를 감사에 남긴다."""
    idle_seconds = getattr(exc, "idle_seconds", None)
    silence_seconds = getattr(exc, "silence_seconds", idle_seconds)
    timeout_axis = getattr(
        exc, "timeout_axis", "idle" if idle_seconds is not None else "wall"
    )
    threshold = float(getattr(exc, "threshold_seconds", exc.timeout or timeout))
    if timeout_axis == "idle":
        measured = idle_seconds if idle_seconds is not None else silence_seconds
        measured_label = (
            f"{measured:.0f}s" if measured is not None else "관측 불가"
        )
        reason = (f"[{harness} 무진행 임계 {threshold:.0f}s 발화 · 실측 침묵 {measured_label} "
                  f"— 정상 작업이었다면 local.conf harness.{harness}.idle_timeout=<초> 로 상향]")
    else:
        silence_label = (
            f" · 중단 시 실측 침묵 {silence_seconds:.0f}s"
            if silence_seconds is not None else " · 중단 시 침묵 관측 불가"
        )
        reason = (f"[{harness} 벽시계 백스톱 {threshold:.0f}s 초과{silence_label} "
                  f"— 늘리려면 local.conf harness.{harness}.wall_timeout=<초>]")
    result: watchdog.RunResult = {
        "returncode": 1,
        "stdout": exc.output or "",
        "stderr": f"{reason} {exc.stderr or ''}".rstrip(),
        "timed_out": True,
        delegate_run_rules.RUN_RESULT_TIMEOUT_AXIS: timeout_axis,
        delegate_run_rules.RUN_RESULT_TIMEOUT_THRESHOLD_SEC: threshold,
    }
    if silence_seconds is not None:
        result[delegate_run_rules.RUN_RESULT_SILENCE_SEC] = silence_seconds
    if timeout_axis == "idle":
        result[delegate_run_rules.RUN_RESULT_IDLE_KILLED] = True
    return result


def _timeout_result_summary(result: watchdog.RunResult, *, fallback_timeout: int) -> str:
    """RunResult의 실제 발화 축·임계를 사용자 안내 한 줄로 만든다.

    주입 runner가 구형 최소 dict만 반환한 경우에만 그 **해당 attempt**의 해소 timeout으로
    폴백한다. primary 값을 fallback 안내에 재사용하지 않는다."""
    axis = result.get(delegate_run_rules.RUN_RESULT_TIMEOUT_AXIS, "wall")
    threshold = result.get(delegate_run_rules.RUN_RESULT_TIMEOUT_THRESHOLD_SEC, fallback_timeout)
    silence = result.get(delegate_run_rules.RUN_RESULT_SILENCE_SEC)
    if axis == "idle":
        base = f"무진행 임계 {float(threshold):g}s"
    else:
        base = f"벽시계 백스톱 {float(threshold):g}s"
    if silence is not None:
        base += f" · 실측 침묵 {float(silence):g}s"
    return base


def _default_run_fn(
    home, argv: list[str], *, stdin_text: str | None, cwd: str, env: dict[str, str],
    timeout: int, harness: str,
) -> watchdog.RunResult:
    """실 subprocess 실행(테스트는 이 seam 을 mock). timeout 시 **프로세스그룹 종료**(3드라이버 공통·
    start_new_session + killpg·자식[모델 fetch·pytest 등] 잔존 방지).

    **3 드라이버 전부 `watchdog` 워치독 경로** — codex/claude 도 증분 관측 대상이라야
    무진행 판정이 선다(옛 구조는 codex/claude 만 `Popen`+`communicate(timeout)` 벽시계 단독이었다).
    드라이버별로 다른 건 코드 분기가 아니라 **선언**(`watchdog.HARNESS_PROFILES`)이다: 신호 있는 축은
    무진행 판정, startup stall 이 실측된 축(opencode)만 첫-이벤트 창 + 재시도. 프롬프트는 codex/
    claude 가 stdin(워치독의 `input_text`), opencode 는 `--file`(stdin 불요).

    **launch 오류 정규화**(codex must-fix): 하네스 바이너리 미설치/실행 불가(FileNotFoundError·
    PermissionError 등 **스폰 단계** 오류)는 traceback 으로 전파하지 않고 RunResult(rc≠0·진단 stderr)로
    감싼다 — 3드라이버 공통(additional_reviewer._run_reviewer_ex 의 FileNotFoundError fail-soft 계약 동형).

    **스폰 단계 한정**: 프롬프트를 이미 보낸 뒤의 I/O 오류(communicate 중 EPIPE 등)를 launch
    실패로 표시하면 폴백이 발동해 **같은 프롬프트가 중복 호출**된다. 그래서 launch 신호는
    스폰 지점 예외에만 붙이고, 실행-중 OSError 는 미분류 실패(rc=1)로 남겨 기존 fail-loud 를 태운다."""
    profile = delegate_env.harness_profile(home, harness)
    # 워치독이 내부에서 스폰한다 — 바이너리/권한 계열(_LAUNCH_STAGE_ERRORS)만 launch 로 보고
    # 나머지 OSError 는 실행-중으로 간주한다(호출 후 중복 호출 금지).
    try:
        completed = watchdog.run_with_first_event_watchdog(
            argv,
            first_event_timeout=(watchdog.first_event_timeout_default()
                                 if profile.startup_watchdog else None),
            overall_timeout=timeout,
            retries=(watchdog.stall_retries_default()
                     if profile.startup_watchdog else 0),
            idle_timeout=watchdog.idle_timeout_for_signal(profile.progress_signal,
                                                       profile.idle_timeout),
            cwd=str(cwd),
            env=env,
            input_text=stdin_text,
        )
        result: watchdog.RunResult = {
            "returncode": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "timed_out": False,
        }
        silence = getattr(completed, watchdog.SILENCE_SEC_ATTR, None)
        if silence is not None:
            result[delegate_run_rules.RUN_RESULT_SILENCE_SEC] = silence
        return result
    except watchdog.StallWatchdogError as exc:
        timeout_axis = getattr(exc, "timeout_axis", None)
        threshold = getattr(exc, "threshold_seconds", None)
        silence = getattr(exc, "silence_seconds", None)
        if timeout_axis == watchdog.TIMEOUT_AXIS_WALL:
            wall_exc = subprocess.TimeoutExpired(
                argv, threshold if threshold is not None else timeout,
                output=getattr(exc, "output", "") or "",
                stderr=getattr(exc, "stderr", "") or "",
            )
            wall_exc.timeout_axis = timeout_axis
            wall_exc.threshold_seconds = (
                float(threshold) if threshold is not None else float(timeout)
            )
            wall_exc.silence_seconds = silence
            return _timeout_failure_result(harness, timeout, wall_exc)
        # first-event 축만 startup stall 인프라 실패다. 구형 sentinel(axis 없음)은 기존 계약을
        # 보존해 stall로 취급한다.
        partial_stderr = getattr(exc, "stderr", "") or ""
        result = {"returncode": 1, "stdout": getattr(exc, "output", "") or "",
                  "stderr": f"{delegate_run_rules.OPENCODE_STALL_MARKER} {exc}] {partial_stderr}".rstrip(),
                  "timed_out": False, delegate_run_rules.RUN_RESULT_STALLED: True}
        if timeout_axis is not None:
            result[delegate_run_rules.RUN_RESULT_TIMEOUT_AXIS] = timeout_axis
        if threshold is not None:
            result[delegate_run_rules.RUN_RESULT_TIMEOUT_THRESHOLD_SEC] = threshold
        if silence is not None:
            result[delegate_run_rules.RUN_RESULT_SILENCE_SEC] = silence
        return result
    except subprocess.TimeoutExpired as exc:
        # 워치독이 프로세스그룹째 kill 후 전파(kill 은 워치독 소관). 무진행/벽시계 공통 경로 —
        # IdleTimeoutExpired 는 TimeoutExpired 하위라 분류(FAILURE_CLASS_TIMEOUT)가 그대로 선다.
        return _timeout_failure_result(harness, timeout, exc)
    except _LAUNCH_STAGE_ERRORS as exc:
        return _launch_failure_result(harness, exc)
    except OSError as exc:
        return _midrun_failure_result(harness, exc)
    except watchdog.ProcessCleanupError as exc:
        return _cleanup_failure_result(harness, exc)


# 스폰 단계로 확신할 수 있는 예외들(바이너리 부재·실행 권한·경로 형상). 그 밖의 OSError 는 실행 중
# 발생했을 수 있으므로 launch 로 표시하지 않는다.
_LAUNCH_STAGE_ERRORS = (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError)


def _launch_failure_result(harness: str, exc: BaseException) -> watchdog.RunResult:
    """스폰 실패를 명시 신호 + 진단이 붙은 RunResult 로 정규화한다(단일 출처)."""
    return {
        "returncode": 127,
        "stdout": "",
        "stderr": f"하네스 {harness} 실행 불가: {exc} — 설치/PATH 확인",
        "timed_out": False,
        delegate_run_rules.RUN_RESULT_LAUNCH_FAILED: True,
    }


def _midrun_failure_result(harness: str, exc: BaseException) -> watchdog.RunResult:
    """호출 후 실행-중 I/O 오류 — **분류 신호 없이** 실패로 남긴다(폴백 금지·중복 호출 차단)."""
    return {
        "returncode": 1,
        "stdout": "",
        "stderr": (f"하네스 {harness} 실행 중 I/O 오류: {exc} — 프롬프트가 이미 넘어갔을 수 있어 "
                   "자동 폴백하지 않습니다(중복 호출 차단). 결과를 확인하고 수동 재시도하세요."),
        "timed_out": False,
    }


def _cleanup_failure_result(harness: str, exc: BaseException) -> watchdog.RunResult:
    """kill/drain 실패를 부분 산출물과 **폴백 비대상** 명시 신호가 붙은 결과로 정규화한다."""
    return {
        "returncode": 1,
        "stdout": getattr(exc, "output", "") or "",
        "stderr": (
            f"[{harness} 프로세스 정리 실패: {exc} — 잔존 프로세스 가능성 때문에 자동 폴백 금지, "
            "사람 확인 필요] "
            f"{getattr(exc, 'stderr', '') or ''}"
        ).rstrip(),
        "timed_out": False,
        delegate_run_rules.RUN_RESULT_CLEANUP_FAILED: True,
    }


class DelegateAttempt(NamedTuple):
    """단일 하네스 실행과 감사 raw 결과(폴백 재귀 없이 primary/fallback 각 1회).

    `session_id` = 회신 wire 가 말한 세션 id(관측 실패는 None). 세션 재사용 성공 판정은 이
    값과 요청 id 의 일치뿐이다."""

    harness: str
    model: str
    argv: list[str]
    result: watchdog.RunResult
    raw_path: Path
    session_id: str | None = None


def _build_target_argv(
    harness: str,
    model: str,
    reasoning: str | None,
    role: str,
    cwd: Path,
    prompt_file: Path,
    resume_session_id: str | None = None,
) -> list[str]:
    if harness == "codex":
        return contract.build_codex_argv(
            model, reasoning, role, str(cwd), resume_session_id,
        )
    if harness == "claude":
        return contract.build_claude_argv(model, reasoning, role, resume_session_id)
    return contract.build_opencode_argv(
        model, reasoning, role, str(cwd), str(prompt_file),
    )


def _observe_harness_result(harness: str, stdout: str):
    """회신 wire 1회 관측(회신·세션 id·usage). 빈 stdout 은 관측 없음, 추출 예외는 전파한다.

    회수 경로의 fail-loud 는 종전 그대로 `extract_reply` 가 소유한다."""
    if not stdout:
        return contract.HarnessReply(None, None, None)
    return contract.extract_harness_result(harness, stdout)


def _finished_ledger_fields(
    observed, resume_session_id: str | None,
) -> dict[str, object]:
    """실행 **후**에야 아는 구조화 필드만 모은다(수집 못 한 값은 필드 자체를 안 만든다).

    `usage` 는 하네스가 분해 관측을 준 축에서만 실린다 — 0 채우기·추정 매핑은 비용 표를
    false-정밀하게 만든다. `must_fix_items` 는 생산 시점 추출이라 다음 라운드 delta 가 raw 텍스트를
    재파싱하지 않아도 된다.

    `reply_extracted` 는 **판정이라 항상 싣는다**(True/False 둘 다) — 이 실행이 회신 검증까지
    통과한 유효 성공인지를 rc 와 분리해 기록하는 유일한 자리다. 부재로 두면 '이 축을 모르는
    구레코드'와 구분되지 않아 다음 재개가 실패 실행을 성공 후보로 집는다."""
    fields: dict[str, object] = {delegate_run_rules.RESUME_FIELD_REPLY_EXTRACTED: observed.reply is not None}
    if observed.session_id is not None:
        fields[delegate_run_rules.RESUME_FIELD_SESSION_ID] = observed.session_id
    if observed.usage:
        fields[delegate_run_rules.RESUME_FIELD_USAGE] = dict(observed.usage)
    items = _observed_must_fix_items(observed.reply)
    if items:
        fields[delegate_run_rules.RESUME_FIELD_MUST_FIX] = items
    if resume_session_id is not None:
        fields[delegate_run_rules.RESUME_FIELD_RESUME_MATCHED] = (
            observed.session_id == resume_session_id
        )
    return fields
