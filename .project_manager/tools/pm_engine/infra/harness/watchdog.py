"""하네스 자식 프로세스 워치독과 시간 예산 (층: infra).

첫 이벤트 stall 재시도, 무진행(idle)·벽시계(wall) 판정, 프로세스 그룹 정리, 하네스별 시간 예산 실측 근거,
출력 cap-hit 판정을 소유한다. 위임·추가 리뷰어·fill 이 같은 판정 코드를 쓴다.
"""
from __future__ import annotations

import codecs
import math
import os
import signal
import subprocess
import sys
import threading
import time
from typing import NamedTuple

from pm_engine.domain import conf_schema
from pm_engine.infra.harness import contract


# ── opencode 첫-이벤트 stall 워치독 (하니스-무관 순수 헬퍼·parse_opencode_json 동거) ──
# opencode `run` 이 스타트업 network fetch stall 에 빠지면 `--format json` stdout 이벤트가 0바이트로
# **영원히** 멈춘다(정상 run 은 첫 이벤트 ~0.2–2초·헝 run 은 240초+ 창 지나도 자체
# 회복 없음·upstream #13841 진단 일치). 호출층에서 이를 닫는다: 첫 stdout 이벤트가 N초 내 안 오면
# 프로세스 그룹째 kill → 재시도(M회) → 소진 시 fail-loud. provider/원인 무관(내부 어느 fetch 가
# 멈추든 동작). mid-turn(정상 긴 생성) 침묵은 이번 범위 밖 — overall_timeout(호출부의 기존 hard
# 가드·예: pm_orch_opencode.TURN_TIMEOUT_SEC=600)이 그대로 백스톱. provider 노브(headerTimeout 등)는
# stall 이 provider 스트림 fetch *밖*에서 발생해 무효 실측 — 워치독이 클래스를 커버한다.

# env 노브 (worktree_pool 의 PM_GIT_TIMEOUT 네이밍 결). 세 표면(opencode driver·pm_import fill·
# release 라이브 헬퍼)이 아래 두 해소기로 이 기본값을 공유한다. 값을 바꾸려면 export 후 재실행.
FIRST_EVENT_TIMEOUT_ENV = "PM_OC_FIRST_EVENT_TIMEOUT"   # 첫-이벤트 대기 상한(초).
STALL_RETRIES_ENV = "PM_OC_STALL_RETRIES"               # stall 시 재시도 횟수.
# 기본 90초 = 느린 cloud 시작 대비 보수적(정상 첫 이벤트 ~0.2–2초). 재시도 기본 2회.
DEFAULT_FIRST_EVENT_TIMEOUT_SEC = 90.0
DEFAULT_STALL_RETRIES = 2
# 워치독 폴 간격·kill 후 grace(자식 잔존 방지·짧게). 매직넘버 회피 상수.
_WATCHDOG_POLL_INTERVAL_SEC = 0.1
_KILL_GRACE_SEC = 5.0
# timeout/stall 정리는 부모 종료 대기와 파이프 EOF drain이 **연속**으로 각각 grace를 다 쓸 수 있다.
# 하네스 상한 계산이 한 단계만 세면 부분 산출물 수확 전에 호출층이 먼저 프로세스를 죽일 수 있다.
_PROCESS_CLEANUP_GRACE_PHASES = 2

# 정리 뒤 분류·raw 박제·범위 감사에 남기는 공용 여유. 두 소비 표면(pm_delegate/additional_reviewer)이
# 같은 `harness_cap_required_budget`을 써야 상한 계산 규칙이 갈리지 않는다. 실측 보조 단계 합 7초를
# 플랫폼 편차를 위해 다음 10초 경계로 올린 값이다.
HARNESS_CAP_MEASURED_AUX_BUDGET_SEC = 7
HARNESS_CAP_MARGIN_SEC = (
    (HARNESS_CAP_MEASURED_AUX_BUDGET_SEC + 9) // 10
) * 10


def process_cleanup_budget_per_attempt() -> float:
    """timeout/stall 1회 정리의 최악 예산 — 부모 wait + pipe drain의 연속 grace."""
    return _PROCESS_CLEANUP_GRACE_PHASES * _KILL_GRACE_SEC


def watchdog_execution_budget(
    overall_timeout: float,
    *,
    first_event_timeout: float | None,
    retries: int,
) -> float:
    """공용 워치독 한 번의 최악 실행+정리 예산.

    startup 창이 wall보다 먼저 발화할 때만 실패한 각 startup 시도를 재실행한다. wall이 먼저거나
    동시에 발화하면 첫 시도에서 끝나므로 재시도 예산을 더하지 않는다. startup 창이 꺼진 축도
    전달된 retries를 무시해 기존 단일 시도 계약을 보존한다.
    """
    retry_count = (
        max(0, int(retries))
        if first_event_timeout is not None
        and float(first_event_timeout) < float(overall_timeout)
        else 0
    )
    retry_runtime = (
        retry_count * min(float(first_event_timeout), float(overall_timeout))
        if first_event_timeout is not None else 0.0
    )
    cleanup = (retry_count + 1) * process_cleanup_budget_per_attempt()
    return float(overall_timeout) + retry_runtime + cleanup


def harness_cap_required_budget(execution_budget: float) -> float:
    """엔진 실행+정리 뒤 진단/박제까지 마치기 위한 호출층 최소 상한."""
    return float(execution_budget) + HARNESS_CAP_MARGIN_SEC


def format_partial_output(head: str, exc: BaseException) -> str:
    """실패 진단 뒤에 kill 시점 stdout/stderr를 안전하게 붙인다.

    이미 실패한 timeout/cleanup 경로에서 쓰이므로, 예외 객체의 비정상 속성이나 비-UTF8 bytes가
    원래 진단을 가리는 일은 허용하지 않는다. 문자열 입력의 형식은 기존 소비처 형식을 보존한다.

    | 입력 ``output=b\"ok\\xff\"`` | 이전 additional-reviewer | 현재 공용 포맷터 |
    | --- | --- | --- |
    | 표시 | ``b'ok\\xff'`` (repr) | ``ok�`` (UTF-8 replacement decode) |

    pm_import fill은 통합 전에도 replacement decode를 사용했으므로 이 bytes 처리에는 변화가 없다.
    """
    def as_text(value) -> str:
        try:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return str(value) if value else ""
        except Exception:  # noqa: BLE001 - formatter failure must not erase the diagnosis.
            return "[부분 산출물 표시 불가]"

    try:
        stdout = as_text(getattr(exc, "output", ""))
        stderr = as_text(getattr(exc, "stderr", ""))
        parts = [head]
        if stdout:
            parts.append(f"\n[중단 시점까지의 stdout — {len(stdout)}자 보존]\n{stdout}")
        if stderr:
            parts.append(f"\n[중단 시점까지의 stderr — {len(stderr)}자 보존]\n{stderr}")
        return "".join(parts)
    except Exception:  # noqa: BLE001 - `head` itself may be a hostile str subclass.
        return head if isinstance(head, str) else "[실패 진단]"


def harness_cap_advisory(
    env: dict[str, str], *, execution_budget: float,
    session_markers: dict[str, tuple[str, ...]], cap_env: dict[str, str | None],
    render_missing, render_invalid, render_low,
) -> str | None:
    """세 실행 표면이 공유하는 호출층 상한 판정; 표면별 기존 문구는 renderer가 소유한다."""
    required = int(harness_cap_required_budget(execution_budget))
    warnings = []
    for harness, markers in session_markers.items():
        if not any(env.get(marker) for marker in markers):
            continue
        cap_key = cap_env.get(harness)
        if cap_key is None:
            continue
        raw = env.get(cap_key)
        if raw is None and render_missing is not None:
            warnings.append(render_missing(harness, cap_key, required))
            continue
        try:
            cap_seconds = int(raw) / 1000.0
        except (TypeError, ValueError, OverflowError):
            warnings.append(render_invalid(harness, cap_key, raw, required))
            continue
        if not math.isfinite(cap_seconds) or cap_seconds < required:
            warnings.append(render_low(harness, cap_key, cap_seconds, required))
    return "\n".join(warnings) or None


class StallWatchdogError(RuntimeError):
    """첫-이벤트 워치독이 모든 재시도를 소진(반복 startup stall) → fail-loud.

    헬퍼는 raise 만 하고 *정책은 호출부가* 정한다:
      - opencode driver `_turn`: catch → loud stderr + fail-soft turn(None,None·relay 루프 생존).
      - pm_import fill(`_real_harness_runner`): catch → (False, 에러텍스트)(기존 fail-soft 계약).
      - release 라이브 헬퍼: uncatch → 테스트 fail-loud(라이브 환경 문제 가시화).
    """

    def __init__(self, message: str, *, timeout_axis: str | None = None,
                 threshold_seconds: float | None = None,
                 silence_seconds: float | None = None,
                 output: str | None = None, stderr: str | None = None) -> None:
        super().__init__(message)
        self.timeout_axis = timeout_axis
        self.threshold_seconds = threshold_seconds
        self.silence_seconds = silence_seconds
        self.output = output or ""
        self.stderr = stderr or ""


class ProcessCleanupError(RuntimeError):
    """프로세스 그룹 종료/파이프 drain 실패 — 부분 산출물을 싣는 loud sentinel."""

    def __init__(self, message: str, *, output: str | None = None,
                 stderr: str | None = None) -> None:
        super().__init__(message)
        self.process_cleanup_failed = True
        self.output = output or ""
        self.stderr = stderr or ""


def _env_positive_float(name: str, default: float) -> float:
    """env 노브를 양수 float 로 해소(빈/불량/비양수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_nonneg_int(name: str, default: int) -> int:
    """env 노브를 음이 아닌 int 로 해소(빈/불량/음수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_positive_int(name: str, default: int) -> int:
    """env 노브를 양의 int 로 해소(빈/불량/비양수는 default·fail-soft)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def first_event_timeout_default() -> float:
    """PM_OC_FIRST_EVENT_TIMEOUT env(초) 또는 기본 90. 세 표면 공유 해소기."""
    return _env_positive_float(FIRST_EVENT_TIMEOUT_ENV, DEFAULT_FIRST_EVENT_TIMEOUT_SEC)


def stall_retries_default() -> int:
    """PM_OC_STALL_RETRIES env 또는 기본 2. 세 표면 공유 해소기."""
    return _env_nonneg_int(STALL_RETRIES_ENV, DEFAULT_STALL_RETRIES)


# ── 무진행(idle) 판정 — 벽시계 단독 판정의 false-kill 폐쇄 ──────────────────────
# 자식 프로세스를 "시작 후 경과 시간"으로 죽이면 **정상 진행 중인 작업**이 잘린다. 값 튜닝으로는
# 못 닫는다 — 동일 입력이 900초를 넘기도 하고 안 넘기도 한 직접 증거(추가 리뷰 채널 실측 2회
# 실행·입력 동일·한 번은 kill/한 번은 성공)가 임계값이 정상 작업의 **분산 대역 안**에 있음을 보였다.
# 그래서 판정 기준을 **마지막 진행 이벤트 이후 무진행 시간**으로 교체하고 벽시계는 백스톱으로
# 강등한다(worktree_pool.py:84-100 "진행이 보이면 관대하게, 안 보이면 유한하게"의 승계).
# ── 시간 예산 실측 근거 (추측 상수 금지) ────────────────────────────────────────────
# **클라우드 축**(codex·claude — 원격 추론). 재료 = /tmp 잔존 위임 raw 코퍼스(pm_delegate_codex_*
# 5,100건·pm_delegate_claude_* 749건) + additional_reviewer raw(codex 평문 축 207건).
#   ⓐ 도구 실행 침묵(직접 실측·N=860 표본/89 파일) — codex 는 command_execution 을 item.started →
#      (그 도구가 도는 동안 stdout 이벤트 0) → item.completed 로 내므로 **한 도구 실행의 소요 =
#      그 구간의 stdout 침묵**이다. 도구가 스스로 찍은 소요(pytest "in NNNs")를 표본으로:
#      p50 0.5s · p90 82.6s · p95 102.7s · p99 124.9s · **max 254.6s**.
#   ⓑ 이벤트 간 평균 간격(비둘기집 하한·rc=0 완주 153건) — 총 소요/이벤트 수 ≤ 최대 간격:
#      p50 9.0s · p99 16.4s · max 17.7s. (총 소요 자체는 p50 370.2s·p99 1036.6s·max 1429.1s.)
#   ⓒ 평문 축(additional_reviewer 의 `codex exec` — `--json` 없음) — 진행 로그가 **stderr** 로 촘촘히
#      흐른다(리뷰 1건 stderr 12,233줄·hook/exec/succeeded 라인이 도구마다). stdout 은 최종 회신
#      뿐이라(실측 498~759 바이트) **stdout 단독 관측은 이 축을 전량 false-kill 한다** — 그래서
#      진행 신호는 stdout·stderr **양쪽 chunk 도착**으로 본다.
CLOUD_IDLE_TIMEOUT_SEC = 900.0    # 관측 최대 침묵(254.6s)의 3.5배.
CLOUD_WALL_TIMEOUT_SEC = 3600.0   # 관측 최대 완주(1429.1s)의 2.5배 — 백스톱이라 여유를 크게.

# **로컬 GPU 축**(opencode — 로컬 모델 추론). 위 코퍼스에 opencode 표본은 **0건**이라 값을 클라우드
# 측정치로 잡으면 안 된다(초기 구현이 그렇게 했다가 전제가 깨졌다). 근거는 사용자 실측 증언이다:
# 회사 환경(로컬 GPU 부족)의 opencode 위임은 **3시간까지** 걸리고, 관찰된 진행 양상은
# **"길게 멈췄다 가끔 움직임"** 이다 — 즉 총 소요만 긴 게 아니라 **침묵 구간 자체가 길다**.
# 클라우드 값(idle 900s)으로는 그 정상 작업을 죽인다.
#   · wall 4시간 = 관측된 3시간 완주 + 33% 여유(요구: "3시간 위").
#   · idle 1.5시간 = 3시간 실행에서 한 침묵이 전체의 절반에 달해도 통과. 이 이상 조용하면 총 소요의
#     절반을 무출력으로 보낸 것이라 정지로 본다. 하드 표본이 없는 축이라 **설정 안 해도 안 죽는
#     쪽**으로 잡았다 — 미설정 채택자의 false-kill 이 이 판정 전환의 실패 조건이기 때문이다.
#     GPU 가 넉넉한 배포는 환경 조건이 다르므로 local.conf `harness.opencode.*` 로 조인다.
LOCAL_GPU_IDLE_TIMEOUT_SEC = 5400.0    # 1.5시간
LOCAL_GPU_WALL_TIMEOUT_SEC = 14400.0   # 4시간

# 호출층 Bash 상한을 관측할 수 있는 세션 축의 단일 선언. 공개 상한이 없는 codex는 None으로
# 남기며, 그 자체는 advisory 대상이 아니다.
HARNESS_SESSION_MARKERS: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_THREAD_ID", "CODEX_CI"),
    "claude": ("CLAUDECODE",),
    "opencode": ("OPENCODE", "OPENCODE_PID"),
}
HARNESS_CAP_ENV: dict[str, str | None] = {
    "codex": None,
    "claude": "BASH_MAX_TIMEOUT_MS",
    "opencode": "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
}


class HarnessProfile(NamedTuple):
    """하네스 축 선언 — 관측 능력 + 시간 예산. **값만 갈리고 판정 코드는 하나다.**

    "하네스별 특례 분기 금지"의 금지 대상은 *코드 분기*이고, 드라이버별 *선언된 값*은 이 테이블의
    존재 이유다. 코드가 갈리면(하네스 이름으로 판정 경로가 바뀌면) 그건 위반이다."""

    progress_signal: str      # conf_schema.PROGRESS_SIGNAL_* — 무진행 판정 가능 여부
    startup_watchdog: bool    # 첫-stdout-이벤트 창 + 유한 재시도 적용 여부
    idle_timeout: float       # 무진행 상한(주 판정·초)
    wall_timeout: float       # 벽시계 백스톱(초)


HARNESS_PROFILES: dict[str, HarnessProfile] = {
    # 클라우드 축 — 원격 추론이라 침묵/완주 모두 짧다(위 ⓐⓑ 실측).
    "codex": HarnessProfile(conf_schema.PROGRESS_SIGNAL_EVENT_STREAM, False,
                            CLOUD_IDLE_TIMEOUT_SEC, CLOUD_WALL_TIMEOUT_SEC),
    "claude": HarnessProfile(conf_schema.PROGRESS_SIGNAL_EVENT_STREAM, False,
                             CLOUD_IDLE_TIMEOUT_SEC, CLOUD_WALL_TIMEOUT_SEC),
    # 로컬 GPU 축 — 긴 침묵 + 장시간 완주(사용자 실측 증언). startup stall 워치독은 이 축에서만
    # 실측된 결함(upstream #13841)이라 여기만 True 다.
    "opencode": HarnessProfile(conf_schema.PROGRESS_SIGNAL_EVENT_STREAM, True,
                               LOCAL_GPU_IDLE_TIMEOUT_SEC, LOCAL_GPU_WALL_TIMEOUT_SEC),
}

# 프로필을 모르는 축의 기본 = **테이블에 선언된 값 중 가장 관대한 쪽**(false-kill 방향 금지).
# 특정 프로필 상수의 alias 로 두면 더 관대한 축을 추가할 때 조용히 stale 해지므로 반드시 테이블에서
# 파생한다. 이 두 이름은 기존 호출자의 공개 호환 표면이라 유지하되 값의 소유권은 HARNESS_PROFILES 다.
DEFAULT_IDLE_TIMEOUT_SEC = max(profile.idle_timeout for profile in HARNESS_PROFILES.values())
DEFAULT_WALL_TIMEOUT_SEC = max(profile.wall_timeout for profile in HARNESS_PROFILES.values())

# 미지 하네스(설정 오류·신규 추가 누락) — 진행 신호를 **모르므로** 무진행 판정 대상이 아니고
# (모르는 드라이버를 무진행으로 죽이지 않는다), 벽시계는 가장 관대한 선언값으로 유한하게 남는다.
UNKNOWN_HARNESS_PROFILE = HarnessProfile(conf_schema.PROGRESS_SIGNAL_NONE, False,
                                         DEFAULT_IDLE_TIMEOUT_SEC, DEFAULT_WALL_TIMEOUT_SEC)


# 미지 **리뷰어** 커맨드 — 출력 형식을 모르므로 진행 신호가 있다고 가정하지 않는다. 종료 시 한 번에
# 출력하는 CLI를 평문 증분 축으로 오인하면 정상 침묵 구간이 곧 idle kill 대상이 된다. 모를 때
# 신호 없음(벽시계만)이 false-kill에 안전하고, 값은 가장 관대한 쪽으로 유한하게 남긴다.
REVIEWER_FALLBACK_PROFILE = HarnessProfile(conf_schema.PROGRESS_SIGNAL_NONE, False,
                                           DEFAULT_IDLE_TIMEOUT_SEC, DEFAULT_WALL_TIMEOUT_SEC)


# local.conf 하네스별 override 키 — GPU 여유/네트워크 같은 **환경 조건**은 엔진 속성이 아니라
# 배포 속성이라 per-clone 으로 받는다. 두 표면(위임·추가 리뷰)이 같은 키를 읽는다.
HARNESS_IDLE_TIMEOUT_KEY = "harness.{harness}.idle_timeout"
HARNESS_WALL_TIMEOUT_KEY = "harness.{harness}.wall_timeout"


def normalize_timeout_seconds(raw) -> int | None:
    """외부 timeout 입력을 **유한한 정수 초**로 정규화한다.

    CLI·local.conf 등 문자열/수치 입력의 단일 경계다. bool·NaN·±inf·분수·최솟값 미만은 None 으로
    거부한다. 소비처에서 `int()` 로 다시 자르지 않으므로 0.5→0초, int(nan/inf) 예외,
    `nan` 비교 우회로 idle watchdog 비활성화가 모두 같은 원인에서 닫힌다.
    """
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or not value.is_integer():
        return None
    integer = int(value)
    return integer if integer >= 1 else None


def _conf_positive_float(conf: dict, key: str | None) -> float | None:
    """local.conf timeout 을 정수 초로 해소(미설정/깨진 값은 None + 경고·실행을 막지 않는다)."""
    if not key:
        return None
    raw = (conf.get(key) or "").strip()
    if not raw:
        return None
    value = normalize_timeout_seconds(raw)
    if value is None:
        sys.stderr.write(
            f"경고: local.conf {key}={raw!r} 은 유한한 정수 초(최소 1)가 아님 "
            "— 선언 기본값 사용.\n"
        )
        return None
    return float(value)


def resolve_harness_profile(harness: str, conf: dict | None = None, *,
                            fallback: HarnessProfile | None = None,
                            legacy_idle_key: str | None = None,
                            legacy_wall_key: str | None = None) -> HarnessProfile:
    """하네스 → 시간 예산이 해소된 프로필. **두 표면이 이 한 함수를 쓴다.**

    해소 순서(뒤가 이긴다): 선언 기본 → 표면-flat legacy 키(`delegate.timeout`·
    `additional_reviewer.timeout` 등 기존 노브) → 하네스별 키(`harness.<name>.*`). CLI 일회성 override 는
    호출부가 이 결과 위에 얹는다(가장 강함). 미지 하네스는 `fallback`(없으면 UNKNOWN_HARNESS_PROFILE).
    """
    conf = conf or {}
    profile = HARNESS_PROFILES.get(harness) or fallback or UNKNOWN_HARNESS_PROFILE
    idle, wall = profile.idle_timeout, profile.wall_timeout
    for key in (legacy_idle_key, HARNESS_IDLE_TIMEOUT_KEY.format(harness=harness)):
        value = _conf_positive_float(conf, key)
        if value is not None:
            idle = value
    for key in (legacy_wall_key, HARNESS_WALL_TIMEOUT_KEY.format(harness=harness)):
        value = _conf_positive_float(conf, key)
        if value is not None:
            wall = value
    return profile._replace(idle_timeout=idle, wall_timeout=wall)


def idle_timeout_for_signal(progress_signal: str,
                            configured: float | None = None) -> float | None:
    """진행-신호 선언 → 적용할 무진행 상한(초). 신호 없는 축은 None(=벽시계 유지).

    configured=None 이면 테이블 파생 기본을 쓴다. 미지의 선언은 **보수적으로** 신호 없음으로
    취급한다(모르는 드라이버를 무진행으로 죽이지 않는다 — false-kill 방향 금지)."""
    if (progress_signal not in conf_schema.PROGRESS_SIGNAL_KINDS
            or progress_signal == conf_schema.PROGRESS_SIGNAL_NONE):
        return None
    value = DEFAULT_IDLE_TIMEOUT_SEC if configured is None else configured
    normalized = normalize_timeout_seconds(value)
    if normalized is None:
        raise ValueError(f"idle_timeout={value!r}: 유한한 정수 초(최소 1) 필요")
    return float(normalized)


# taskkill 이 대상 PID 를 찾지 못했을 때의 종료 코드(Windows 11 실측).
_TASKKILL_NOT_FOUND_RC = 128


def _kill_process_group(proc, process_group_id: int | None = None) -> None:
    """저장된 process-group ID를 부모 종료 여부와 무관하게 kill한다.

    부모 `poll()`은 그룹 생존 판정이 아니다. 부모가 rc=0으로 먼저 끝나도 자식이 파이프를 쥔 채
    남을 수 있으므로 POSIX에서는 생성 직후 저장한 pgid로 항상 `killpg`한다. ESRCH는 그룹이 이미
    사라진 성공 상태지만 권한/플랫폼 오류와 kill 후 부모 wait 실패는 조용히 삼키지 않는다.
    """
    if os.name == "posix":
        pgid = process_group_id
        if pgid is None:
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                return
            except OSError as exc:
                raise ProcessCleanupError(
                    f"프로세스 그룹 ID 해소 실패(pid={proc.pid}): {exc}"
                ) from exc
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise ProcessCleanupError(
                f"프로세스 그룹 kill 실패(pgid={pgid}): {exc}"
            ) from exc
    elif proc.poll() is None:  # pragma: no cover — POSIX 회귀 환경
        # 직속 자식이 `opencode.cmd` 를 돌리는 cmd.exe 면 그것만 죽여서는 손자(opencode.exe)가
        # 파이프를 쥔 채 남는다 — 프로세스 트리 전체를 taskkill 로 끝낸다.
        # 출력은 받지 않는다 — Windows 의 출력 수집은 읽기 스레드를 띄우는데, 이 정리는 스레드
        # 시작이 실패한 경로에서도 돌아야 한다.
        try:
            taskkill_rc = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            ).returncode
        except OSError as exc:
            raise ProcessCleanupError(f"프로세스 kill 실패(pid={proc.pid}): {exc}") from exc
        # 찾지 못함은 poll 과 taskkill 사이에 스스로 끝난 형상일 때만 성공이다.
        already_exited = (taskkill_rc == _TASKKILL_NOT_FOUND_RC
                          and proc.poll() is not None)
        if taskkill_rc != 0 and not already_exited:
            raise ProcessCleanupError(
                f"프로세스 kill 실패(pid={proc.pid}): taskkill rc={taskkill_rc}"
            )

    if proc.poll() is None:
        try:
            proc.wait(timeout=_KILL_GRACE_SEC)
        except subprocess.TimeoutExpired as exc:
            raise ProcessCleanupError(
                f"프로세스 그룹 kill 후 부모가 {_KILL_GRACE_SEC:g}s 안에 종료하지 않음"
            ) from exc


# 부모가 쥔 파이프 래퍼는 kill·wait 로 닫히지 않는다. 자식이 죽어도 래퍼가 살아 있는 동안 그
# 핸들은 열려 있고, Windows 는 열린 핸들이 있는 파일·디렉터리 삭제를 계속 거부한다(자식이 쓰던
# 작업 트리를 지우는 `force_rmtree` 재시도가 그 자리에서 소진된다). POSIX 는 GC 가 늦게라도
# 회수하지만 kill 경로는 예외 traceback 이 프레임(=proc)을 붙잡고 있어 그 회수도 늦다.
_CHILD_PIPE_ATTRS = ("stdout", "stderr", "stdin")


def _close_child_pipes(proc, *, argv) -> None:
    """자식 파이프 래퍼(stdout/stderr/stdin)를 **명시적으로** 닫는다 — 실패는 삼키지 않는다.

    호출 조건은 하나다: **그 파이프를 읽는 스레드가 더는 없을 때**. 남의 blocking read 밑에서
    핸들을 뽑는 대신, 리더가 살아 있는 형상(드레인 타임아웃)은 호출부가 loud 로 올린다.

    닫히지 않은 래퍼는 조용히 사라지지 않고 남은 핸들로 다음 단계(작업 트리 삭제)를 깨뜨리므로
    close 실패는 부분 성공으로 감추지 않고 `ProcessCleanupError` 로 올린다. 유일한 예외는 **stdin
    의 `BrokenPipeError`** 다 — kill 뒤 읽는 쪽이 이미 사라진 정상 형상이고(버퍼를 비우다 파손을
    만난다) 래퍼 자체는 그 자리에서 닫힌다.
    """
    failures: list[str] = []
    for name in _CHILD_PIPE_ATTRS:
        stream = getattr(proc, name, None)
        if stream is None or getattr(stream, "closed", False):
            continue
        try:
            stream.close()
        except BrokenPipeError as exc:
            if name != "stdin":
                failures.append(f"{name}: {exc!r}")
        except (OSError, ValueError) as exc:
            failures.append(f"{name}: {exc!r}")
    if failures:
        raise ProcessCleanupError(
            f"자식 파이프 래퍼 close 실패({', '.join(failures)}): {argv!r}"
        )


def _cleanup_failed_watched_spawn(proc, process_group_id: int | None, *, argv,
                                  threads: list[threading.Thread]) -> None:
    """`Popen` 성공 뒤 어댑터 초기화 실패를 그룹 kill + pipe drain + pipe close 로 롤백한다.

    드레인(`communicate`)이 파이프를 닫아 주는 것은 **그 드레인이 끝까지 돈 경우뿐**이다. Windows
    의 `Popen._communicate` 는 스트림마다 리더 **스레드**를 만들어 돌리므로, 스레드/시그널 축이
    깨진 초기화 실패(이 함수를 부른 그 실패)에서는 드레인이 스레드 생성 자리에서 그대로 터져
    파이프가 열린 채 남는다(POSIX 의 selector 드레인엔 없는 형상). 그래서 드레인이 어떤 예외로
    끝나든 래퍼는 이 함수가 직접 닫는다 — 리더가 아직 읽고 있는 타임아웃만 예외다.
    """
    _kill_process_group(proc, process_group_id)
    drain_timed_out = False
    try:
        proc.communicate(timeout=_KILL_GRACE_SEC)
    except subprocess.TimeoutExpired as exc:
        # 타임아웃 = 리더가 **아직 파이프를 읽고 있다**(CPython 은 재호출을 위해 fd 를 연 채 둔다).
        # 그 밑에서 핸들을 닫지 않고 정리 실패를 그대로 올린다.
        drain_timed_out = True
        raise ProcessCleanupError(
            f"프로세스 생성 후 초기화 실패 정리 중 파이프가 "
            f"{_KILL_GRACE_SEC:g}s 안에 닫히지 않음: {argv!r}",
            output=exc.output, stderr=exc.stderr,
        ) from exc
    except (OSError, ValueError) as exc:
        raise ProcessCleanupError(
            f"프로세스 생성 후 초기화 실패 정리 중 파이프 drain 실패: {argv!r}: {exc}"
        ) from exc
    finally:
        if not drain_timed_out:
            _close_child_pipes(proc, argv=argv)

    # Thread.start()가 성공한 뒤 다음 초기화 단계가 실패한 경우 reader/writer까지 회수한다.
    # 시작 전 Thread.join()은 RuntimeError이므로 ident가 생긴 스레드만 기다린다.
    for thread in threads:
        if thread.ident is None:
            continue
        thread.join(timeout=_KILL_GRACE_SEC)
        if thread.is_alive():
            raise ProcessCleanupError(
                f"프로세스 생성 후 초기화 실패 정리 중 I/O 스레드가 "
                f"{_KILL_GRACE_SEC:g}s 안에 종료하지 않음: {argv!r}"
            )


SPAWN_FAILED_ATTR = "spawn_failed"


def _mark_spawn_failed(exc: BaseException, failed: bool) -> None:
    """예외에 "자식이 떴는가" 표식을 붙인다 — 이미 붙어 있으면 그대로 둔다(최초 관측이 진실).

    표식의 소유자는 **스폰 경계를 실제로 본 층**이다: `_WatchedPopen` 은 실 `Popen` 호출의 앞뒤를
    보고 preflight 거절(NUL 입력)과 확정 기동 실패에 True(자식 없음)를, 그 밖의 `Popen` 예외와
    `Popen` 반환 뒤의 실패에 False(자식 있었음·보수)를 붙이고, 워치독 루프는 표식 없는 주입
    어댑터의 exec 실패(OSError)에 True 를 붙인다.
    상위(추가 리뷰어 러너)는 이 표식이 있으면 그것을, 없으면 예외 종류만 보고 보수적으로 판정한다.
    속성을 못 받는 예외(슬롯 정의)는 조용히 넘긴다 — 표식 없음은 '판정 유보'라 안전쪽이다.
    """
    if getattr(exc, SPAWN_FAILED_ATTR, None) is not None:
        return
    try:
        setattr(exc, SPAWN_FAILED_ATTR, failed)
    except (AttributeError, TypeError):
        pass


# `Popen` 이 던졌을 때 "자식이 뜬 적 없음"을 **확정**할 수 있는 종류. exec 자체가 실패한 형상들이고
# (파일 부재·실행 권한·경로 형상), CPython 은 이때 자기가 만든 자식을 errpipe 로 확인한 뒤 회수한다.
# additional_reviewer 의 `_DEFINITE_LAUNCH_FAILURES` 와 같은 집합이다(양쪽 테스트가 동치를 못박는다).
_DEFINITE_LAUNCH_FAILURES = (
    FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError,
)

# 문자열/경로/환경값에 섞이면 fork/exec **전에** 거절이 확정되는 바이트.
_NUL = "\x00"


def _nul_in(value) -> bool:
    """문자열·바이트·PathLike 어디에 실려 오든 embedded NUL 을 본다(판정 불가면 False)."""
    if isinstance(value, (bytes, bytearray)):
        return b"\x00" in value
    if isinstance(value, str):
        return _NUL in value
    try:
        return _NUL in os.fspath(value)
    except (TypeError, ValueError):
        return False


def _preflight_spawn_inputs(argv, *, cwd, env) -> None:
    """fork/exec **전에** 확정 판정할 수 있는 입력만 명시적으로 거절한다 (embedded NUL).

    `Popen` 이 던졌다는 사실만으로는 자식 유무를 확정할 수 없다 — 그 생성자는 fork/exec 앞의 인자
    검증도, 자식이 뜬 **뒤**의 부모측 초기화도 같은 자리에서 올린다. 그래서 "호출 0" 판정의 근거를
    예외를 받는 자리가 아니라 **부르기 전**으로 옮긴다: argv·cwd·env 의 NUL 은 이 함수가 보고
    거절하므로 그 거절에 붙는 '자식 없음' 표식은 관측이지 추측이 아니다(환불 계약 보존).

    검사 범위는 *확정 가능한 것* 뿐이다 — 여기서 통과한 입력이 `Popen` 안에서 무엇으로 실패하든
    그 판정은 호출부의 보수 규칙이 맡는다(넓히면 다시 추측이 된다).
    """
    for index, item in enumerate(argv or ()):
        if _nul_in(item):
            _reject_pre_child(f"argv[{index}]")
    if cwd is not None and _nul_in(cwd):
        _reject_pre_child("cwd")
    for key, value in (env or {}).items():
        if _nul_in(key):
            _reject_pre_child(f"env key {key!r}")
        if _nul_in(value):
            _reject_pre_child(f"env[{key!r}]")


def _reject_pre_child(where: str):
    """NUL 입력 거절 — `Popen` 과 같은 종류(`ValueError`)에 '자식 없음' 표식을 붙여 올린다."""
    exc = ValueError(f"embedded null byte: {where}")
    _mark_spawn_failed(exc, True)
    raise exc


def _mark_child_existed(exc: BaseException) -> None:
    """이 **실행**에 자식이 한 번이라도 있었음을 못박는다 — 기존 표식도 덮는다.

    `_mark_spawn_failed` 는 최초 관측이 이긴다(같은 시도 안에서 층끼리 서로 덮지 않게). 이 선언은
    시도를 가로지르는 상위 사실이라 그 규칙 위에 선다: 앞 시도가 자식을 띄웠다면 뒤 시도가 exec 에
    실패해도 실행 전체는 '자식 없음'이 아니다(앞 자식이 프롬프트를 이미 보냈을 수 있다). 방향은
    True→False 한쪽뿐이라 단조롭고, 그 쪽이 보수적(호출됐을 수 있음)이다.
    """
    try:
        setattr(exc, SPAWN_FAILED_ATTR, False)
    except (AttributeError, TypeError):
        pass


class _WatchedPopen:
    """실 subprocess.Popen 을 감싸 첫-stdout-이벤트 관측 + 진행(chunk) 관측 + 프로세스그룹 kill 제공.

    reader 스레드가 stdout/stderr 를 **실제 도착 chunk 단위**로 읽어 누적한다. stdout 의 완성된
    비어있지-않은 라인은 별도 line buffer 로 파싱해 first_event 를 set 한다(startup stall = 첫
    이벤트 라인조차 영원히 안 옴). stall 시 메인 루프가 kill 하면 blocking read 가 EOF 로 풀린다.
    stderr 도 별도 스레드로 드레인(파이프 버퍼 데드락 방지).
    fake(테스트)로 대체 가능한 얇은 어댑터 — 워치독 로직은 run_with_first_event_watchdog 이 쥔다.

    **진행 관측**: stdout·stderr **어느 쪽이든** chunk 가 도착한 시각을 `last_event_at()`
    로 노출한다(무진행 판정의 입력). 양쪽을 보는 이유는 실측이다 — `codex exec` 평문 축은 진행
    로그가 전부 stderr 로 흐르고 stdout 은 최종 회신뿐이라, stdout 만 보면 정상 진행을 무진행으로
    오판한다. `first_event_ready()` 는 **stdout 전용 그대로** 둔다(opencode startup stall 의미론
    보존).

    **stdin 주입**: `input_text` 를 주면 stdin=PIPE 로 열고 별도 스레드가 전문을 쓴 뒤 **닫는다**
    (EOF 전달 — codex/claude 는 stdin EOF 까지 프롬프트를 읽는다). None 이면 DEVNULL 로 닫아
    supervisor stdin 을 상속하지 않는다(relay 사용자 입력을 child 가 삼키는 fail-silent 방지).
    """

    def __init__(self, argv, *, cwd=None, env=None, text=True,
                 input_text: str | None = None, clock=None):
        # fork/exec 전에 확정 판정 가능한 입력(NUL)은 여기서 거절한다 — 그 거절의 '자식 없음'
        # 표식은 `Popen` 예외 종류를 보고 추측한 것이 아니라 경계 **앞**의 관측이다.
        _preflight_spawn_inputs(argv, cwd=cwd, env=env)
        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
        )
        popen_kwargs["stdin"] = (
            subprocess.PIPE if input_text is not None else subprocess.DEVNULL
        )
        if text:
            popen_kwargs.update(text=True, encoding="utf-8", errors="replace")
        # 프로세스 그룹 분리 — kill 시 자식(모델 fetch 서브프로세스 등)까지 그룹째 정리.
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover — POSIX 회귀 환경
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._clock = clock if clock is not None else time.monotonic
        spawned = None
        io_threads: list[threading.Thread] = []
        try:
            # 실제 자식 생성도 try 안에 둔다. Popen이 핸들을 반환한 직후부터 아래 후속 초기화
            # (Event/Lock/Thread 생성·start)가 실패해도 같은 트랜잭션에서 자식을 회수한다.
            try:
                # 첫 인자는 자식 env 의 PATH 로 해소한다(Windows npm 셈 `.cmd` — `resolve_executable`).
                # 해소 실패는 `FileNotFoundError` 라 아래 확정 기동 실패로 같이 표식된다.
                spawned = subprocess.Popen(
                    [contract.resolve_executable(argv[0], env), *argv[1:]], **popen_kwargs)
            except _DEFINITE_LAUNCH_FAILURES as launch_failure:
                # exec 자체가 실패한 확정 기동 실패다 — CPython 은 이때 자기가 만든 자식을
                # errpipe 로 확인하고 회수하며, 프롬프트를 밀어 넣는 stdin 펌프는 `Popen` 이
                # 돌아온 **뒤에야** 시작한다. 호출 0·과금 0 이므로 환불 계약을 유지한다.
                _mark_spawn_failed(launch_failure, True)
                raise
            except Exception as unknown_boundary:
                # `Popen` 이 던졌다는 사실만으로는 자식 유무를 확정할 수 없다. 이 생성자는
                # fork/exec 앞의 인자 검증(그건 위 preflight 가 이미 걷어냈다)과 자식이 뜬
                # **뒤**의 부모측 초기화(errpipe 읽기·fd 정리)를 같은 자리에서 올린다. 확정할 수
                # 없는 나머지는 보수쪽으로 못박는다 — '자식 있었음'(False)이면 최악의 경우
                # 라운드를 한 번 더 세지만, 반대로 틀리면 이미 나간 호출이 예산에서 사라진다.
                # `BaseException`(Ctrl-C 등)은 표식 없이 그대로 올린다: 판정 유보 = 보수쪽이다.
                _mark_spawn_failed(unknown_boundary, False)
                raise
            self._proc = spawned
            self._argv = argv
            # start_new_session=True이므로 POSIX 그룹장은 곧 pid다. 부모 종료 후에는 getpgid(pid)를
            # 해소할 수 없으므로 생성 시점 값을 저장하는 것이 잔존 자식 정리의 load-bearing 계약이다.
            self._process_group_id = self._proc.pid if os.name == "posix" else None
            self._first_event = threading.Event()
            self._stdout_chunks: list[str] = []
            self._stderr_chunks: list[str] = []
            # 리더 스레드(2)와 메인 루프가 공유하는 상태(chunk 누적·마지막 도착 시각)의 단일 락.
            # 실제 read chunk 당 1회 획득이라 비용은 무시 가능하고, 스냅샷이 append 와 경합하지 않는다.
            self._progress_lock = threading.Lock()
            self._last_event_at: float | None = None
            self._stdout_reader = threading.Thread(target=self._pump_stdout, daemon=True)
            self._stderr_reader = threading.Thread(target=self._pump_stderr, daemon=True)
            io_threads.extend((self._stdout_reader, self._stderr_reader))
            self._stdin_writer: threading.Thread | None = None
            if input_text is not None:
                self._stdin_writer = threading.Thread(
                    target=self._pump_stdin, args=(input_text,), daemon=True
                )
                io_threads.append(self._stdin_writer)
                self._stdin_writer.start()
            self._stdout_reader.start()
            self._stderr_reader.start()
        except BaseException as primary:
            if spawned is not None:
                # 자식이 **실제로 떴다** — 뒤이은 초기화 실패는 '스폰 전'이 아니다. 회수는 하되
                # 그 사실을 예외에 표식으로 남긴다(호출부가 exec 실패와 구분해 호출 가능성을
                # 보수적으로 판정한다). 표식을 못 붙이는 예외는 표식 없음 = 판정 유보다.
                _mark_spawn_failed(primary, False)
                process_group_id = spawned.pid if os.name == "posix" else None
                try:
                    _cleanup_failed_watched_spawn(
                        spawned, process_group_id, argv=argv, threads=io_threads
                    )
                except Exception as cleanup_error:
                    # 원래 KeyboardInterrupt/SystemExit/초기화 오류의 identity를 유지한다.
                    raise primary from cleanup_error
            raise

    def _pump_stdin(self, text: str) -> None:
        """프롬프트 전문을 stdin 에 쓰고 **닫는다**(EOF). 파이프 파손은 삼킨다(자식 조기 종료)."""
        stream = self._proc.stdin
        if stream is None:
            return
        try:
            stream.write(text)
            stream.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def _note_arrival(self, chunks: list[str], text: str) -> None:
        """실제 byte chunk 도착 시각 갱신 + 디코딩된 텍스트 누적(단일 락).

        UTF-8 다중바이트 문자의 중간 조각은 디코더가 아직 텍스트를 못 내도 **바이트는 도착한
        것**이므로 시각은 갱신한다. 진행 판정은 텍스트/라인 완성 여부와 무관하다."""
        with self._progress_lock:
            if text:
                chunks.append(text)
            self._last_event_at = self._clock()

    @staticmethod
    def _read_chunk(stream):
        """파이프에서 지금 도착한 chunk 를 읽는다 — 줄 경계를 기다리지 않는다."""
        raw_stream = getattr(stream, "buffer", stream)
        read1 = getattr(raw_stream, "read1", None)
        return read1(4096) if callable(read1) else raw_stream.read(4096)

    def _pump_stream(self, stream, chunks: list[str], *, stdout: bool) -> None:
        if stream is None:
            if stdout:
                self._first_event.set()
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending_line = ""
        try:
            while True:
                raw = self._read_chunk(stream)
                if raw in (b"", ""):
                    break
                text = decoder.decode(raw) if isinstance(raw, bytes) else raw
                self._note_arrival(chunks, text)
                if stdout:
                    pending_line += text
                    while "\n" in pending_line:
                        line, _newline, pending_line = pending_line.partition("\n")
                        if line.strip():
                            self._first_event.set()
            tail = decoder.decode(b"", final=True)
            if tail:
                with self._progress_lock:
                    chunks.append(tail)
                pending_line += tail
            if stdout and pending_line.strip():
                self._first_event.set()  # EOF 로 끝난 마지막 비개행 이벤트 라인.
        except (ValueError, OSError):
            pass
        finally:
            if stdout:
                self._first_event.set()  # EOF/읽기 종료(빈 출력 포함) — startup 대기 해제.

    def _pump_stdout(self) -> None:
        self._pump_stream(self._proc.stdout, self._stdout_chunks, stdout=True)

    def _pump_stderr(self) -> None:
        self._pump_stream(self._proc.stderr, self._stderr_chunks, stdout=False)

    def first_event_ready(self) -> bool:
        return self._first_event.is_set()

    def last_event_at(self) -> float | None:
        """마지막 chunk(stdout/stderr) 도착 시각(단조 시계) — 한 번도 없으면 None.

        무진행 판정의 유일한 입력. 리더 스레드와 같은 락으로 읽어 경합-안전하다."""
        with self._progress_lock:
            return self._last_event_at

    def partial_output(self) -> tuple[str, str]:
        """지금까지 받은 (stdout, stderr) 스냅샷 — kill 시점 부분 산출물 보존용(전량 폐기 폐쇄)."""
        with self._progress_lock:
            return "".join(self._stdout_chunks), "".join(self._stderr_chunks)

    def poll(self):
        return self._proc.poll()

    def drain_complete(self) -> bool:
        """부모 종료 + stdin 전달 + stdout/stderr EOF가 모두 끝났을 때만 실행 완료."""
        return (
            self._proc.poll() is not None
            and (self._stdin_writer is None or not self._stdin_writer.is_alive())
            and not self._stdout_reader.is_alive()
            and not self._stderr_reader.is_alive()
        )

    def kill(self) -> None:
        _kill_process_group(self._proc, self._process_group_id)

    def communicate(self, timeout=None):
        """전체 deadline 동안 부모 **및 파이프 EOF**를 기다리고, 끝나면 래퍼를 닫는다.

        리더가 EOF 를 본 뒤에도 래퍼는 열려 있다 — 그 핸들이 남는 동안 Windows 는 자식이 쓰던
        작업 트리 삭제를 거부한다(`force_rmtree` 재시도 소진). 드레인이 끝났다는 것은 그 파이프를
        읽는 스레드가 없다는 뜻이라, 닫는 자리는 여기다(타임아웃은 리더가 살아 있어 닫지 않는다).
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        self._proc.wait(timeout=remaining())
        if self._stdin_writer is not None:
            self._stdin_writer.join(timeout=remaining())
        self._stdout_reader.join(timeout=remaining())
        self._stderr_reader.join(timeout=remaining())
        if (
            (self._stdin_writer is not None and self._stdin_writer.is_alive())
            or self._stdout_reader.is_alive()
            or self._stderr_reader.is_alive()
        ):
            stdout, stderr = self.partial_output()
            raise subprocess.TimeoutExpired(
                self._argv, timeout, output=stdout, stderr=stderr
            )
        _close_child_pipes(self._proc, argv=self._argv)
        return self.partial_output()

    @property
    def returncode(self):
        return self._proc.returncode


def _default_watchdog_log(message: str) -> None:
    """워치독 loud 1줄 sink(기본 stderr — stdout 은 PM 대화 채널 보존)."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


TIMEOUT_AXIS_IDLE = "idle"
TIMEOUT_AXIS_WALL = "wall"
TIMEOUT_AXIS_FIRST_EVENT = "first-event"
TIMEOUT_AXIS_ATTR = "timeout_axis"
TIMEOUT_THRESHOLD_ATTR = "threshold_seconds"


class WatchdogTimeoutExpired(subprocess.TimeoutExpired):
    """공용 워치독 중단의 구조화된 진단 계약.

    소비자는 호출 시 인자로 받았던 값(하네스별 해소 전/primary 값일 수 있음)을 다시 추정하지 않고,
    예외가 싣는 **실제 발화 축·임계·중단 시 실측 침묵**을 읽는다."""

    def __init__(self, cmd, timeout, *, timeout_axis: str,
                 silence_seconds: float | None = None,
                 output=None, stderr=None) -> None:
        super().__init__(cmd, timeout, output=output, stderr=stderr)
        self.timeout_axis = timeout_axis
        self.threshold_seconds = float(timeout)
        self.silence_seconds = silence_seconds


class IdleTimeoutExpired(WatchdogTimeoutExpired):
    """무진행(마지막 출력 이후 침묵) 판정으로 중단 — 벽시계 초과와 구분되는 sentinel.

    `subprocess.TimeoutExpired` 를 **상속** 해 기존 호출부의 `except subprocess.TimeoutExpired`
    가 그대로 잡는다(분류/폴백 경로 회귀 0). 구분이 필요한 지점(감사 헤더·실패 메시지)은
    `idle_seconds`(실측 침묵 초) 속성 유무로 본다."""

    def __init__(self, cmd, timeout, *, idle_seconds: float,
                 output=None, stderr=None) -> None:
        super().__init__(
            cmd, timeout, timeout_axis=TIMEOUT_AXIS_IDLE, silence_seconds=idle_seconds,
            output=output, stderr=stderr,
        )
        self.idle_seconds = idle_seconds


class WallTimeoutExpired(WatchdogTimeoutExpired):
    """벽시계 백스톱 중단 — 실제 벽시계 임계와 kill 시점 침묵을 함께 보존."""

    def __init__(self, cmd, timeout, *, silence_seconds: float | None,
                 output=None, stderr=None) -> None:
        super().__init__(
            cmd, timeout, timeout_axis=TIMEOUT_AXIS_WALL, silence_seconds=silence_seconds,
            output=output, stderr=stderr,
        )


# CompletedProcess 에 실어 보내는 관측 침묵 초 속성명 — 감사 헤더(_write_reserved_raw)가 읽는다.
SILENCE_SEC_ATTR = "silence_sec"


def _proc_partial_output(proc) -> tuple[str, str]:
    """proc 가 지금까지 받은 (stdout, stderr) 스냅샷. 어댑터가 미지원이면 ("","")."""
    getter = getattr(proc, "partial_output", None)
    if not callable(getter):
        return "", ""
    try:
        stdout, stderr = getter()
    except Exception:  # noqa: BLE001 — 부분 산출물 회수 실패가 kill 경로를 깨면 안 된다.
        return "", ""
    return stdout or "", stderr or ""


def _attach_partial_output(exc: subprocess.TimeoutExpired, proc) -> None:
    """TimeoutExpired 에 kill 시점까지의 출력을 실어 준다(부분 산출물 전량 폐기 폐쇄)."""
    stdout, stderr = _proc_partial_output(proc)
    if not exc.output:
        exc.output = stdout
    if not exc.stderr:
        exc.stderr = stderr


def _attach_cleanup_output(exc: ProcessCleanupError, proc) -> None:
    """정리 실패 sentinel에도 kill 시점까지의 부분 산출물을 보존한다."""
    stdout, stderr = _proc_partial_output(proc)
    if not exc.output:
        exc.output = stdout
    if not exc.stderr:
        exc.stderr = stderr


def _proc_drain_complete(proc) -> bool:
    """부모+입출력 채널 완료 판정. 구형/fake 어댑터는 기존 poll 계약으로 호환."""
    checker = getattr(proc, "drain_complete", None)
    return bool(checker()) if callable(checker) else proc.poll() is not None


def _terminate_and_drain(proc, *, argv) -> tuple[str, str]:
    """그룹 kill 후 파이프 EOF까지 수확한다. 정리 실패는 부분 성공으로 숨기지 않는다."""
    try:
        proc.kill()
    except ProcessCleanupError as exc:
        _attach_cleanup_output(exc, proc)
        raise
    try:
        return proc.communicate(timeout=_KILL_GRACE_SEC)
    except subprocess.TimeoutExpired as exc:
        _attach_partial_output(exc, proc)
        # DI용 legacy fake는 kill 뒤 상태/파이프 완료를 모델링하지 않고 같은 예외 객체를 영구
        # 재발시키기도 한다. 실제 어댑터만 정리 실패를 판정하고, 구형 fake는 기존 부분출력 계약을
        # 유지한다(프로덕션 loud 보장을 약화하지 않음).
        if not isinstance(proc, _WatchedPopen):
            return _proc_partial_output(proc)
        raise ProcessCleanupError(
            f"프로세스 그룹 kill 후 파이프가 {_KILL_GRACE_SEC:g}s 안에 닫히지 않음: {argv!r}",
            output=exc.output, stderr=exc.stderr,
        ) from exc


def _silence_seconds(proc, now: float, start: float) -> float | None:
    """마지막 진행(chunk 도착) 이후 침묵 초. **관측 불가 어댑터면 None**(판정 skip).

    출력이 한 번도 없었으면 시작 시각을 기준으로 잰다(시작부터 전부 침묵). `last_event_at` 를
    노출하지 않는 proc 어댑터는 진행을 볼 수 없으므로 무진행 판정 대상이 아니다 —
    "신호 없으면 벽시계" 원칙(모르는 드라이버를 무진행으로 죽이지 않는다)."""
    getter = getattr(proc, "last_event_at", None)
    if not callable(getter):
        return None
    last = getter()
    # reader가 `now` 취득 직후 더 최신 chunk 시각을 기록할 수 있다. 그 경합은 진행이 미래에서
    # 왔다는 뜻이 아니라 관측 순서 차이이므로 감사값/판정 입력을 0 아래로 내리지 않는다.
    return max(0.0, now - (start if last is None else last))


def _drain_with_idle_judgment(proc, *, argv, start: float, idle_timeout: float,
                              overall_timeout: float, overall_deadline: float,
                              clock, sleep, log, poll_interval: float):
    """드레인 구간을 폴하며 **무진행** 을 주 판정으로 본다(벽시계는 백스톱).

    - 무진행 초과 → 프로세스 그룹째 kill + loud 1줄 + IdleTimeoutExpired(부분 산출물 동반).
    - 벽시계 초과 → 같은 kill + TimeoutExpired(부분 산출물 동반). 감지기가 고장나도 유한하게
      닫는다(무제한 금지·worktree_pool 의 captured 러너 폴백-캡과 동형).
    """
    while not _proc_drain_complete(proc):
        now = clock()
        silence = _silence_seconds(proc, now, start)
        if silence is not None and silence >= idle_timeout:
            stdout, stderr = _terminate_and_drain(proc, argv=argv)
            log(f"[pm-orch] idle watchdog: {silence:.0f}s 무진행(임계 {idle_timeout:.0f}s) "
                "— 프로세스 그룹 kill. 부분 산출물은 보존.")
            raise IdleTimeoutExpired(argv, idle_timeout, idle_seconds=silence,
                                     output=stdout, stderr=stderr)
        if now >= overall_deadline:
            stdout, stderr = _terminate_and_drain(proc, argv=argv)
            log(f"[pm-orch] overall watchdog: 벽시계 백스톱 {overall_timeout:.0f}s 초과 "
                "— 프로세스 그룹 kill. 부분 산출물은 보존.")
            raise WallTimeoutExpired(
                argv, overall_timeout, silence_seconds=silence,
                output=stdout, stderr=stderr,
            )
        sleep(poll_interval)
    return proc.communicate(timeout=max(0.0, overall_deadline - clock()))


def run_with_first_event_watchdog(
    argv,
    *,
    first_event_timeout: float | None,
    overall_timeout: float,
    retries: int,
    idle_timeout: float | None = None,
    cwd=None,
    env=None,
    text: bool = True,
    input_text: str | None = None,
    on_spawn_attempt=None,
    popen=None,
    clock=None,
    sleep=None,
    log=None,
    poll_interval: float = _WATCHDOG_POLL_INTERVAL_SEC,
):
    """argv 를 관측 워치독으로 실행 — startup stall(유한 재시도) + 무진행을 유한하게 닫는다.

    각 시도: 프로세스 시작 → stdout 첫 이벤트를 first_event_timeout 초 내 관측하는지 감시.
      - 관측(또는 첫 이벤트 없이 빠른 종료) → 완료까지 드레인 후
        subprocess.CompletedProcess(returncode·stdout·stderr) 반환.
      - 미관측(stall) → 프로세스 그룹째 kill·loud 1줄·다음 시도.
    모든 시도(= retries+1) 소진 → StallWatchdogError(fail-loud·호출부가 정책 결정).

    `first_event_timeout=None` = **startup 창 미적용**(첫-이벤트 감시 없이 바로 드레인). 첫 stdout
    이 종료 직전에야 오는 축(codex exec 평문 리뷰어)이 이 창에 걸려 죽지 않게 하는 선언이다 —
    하니스별 if 분기가 아니라 호출부의 능력 선언(idle_timeout_for_signal 과 짝).

    `idle_timeout=None` = **무진행 판정 미적용**(현행 동작 불변) — 드레인은 종전처럼 단일 블로킹
    `communicate(overall 백스톱)` 이다. 값이 있으면 드레인이 폴 루프로 바뀌어 마지막 진행 이후
    침묵이 그 값을 넘을 때 kill 한다(벽시계는 백스톱으로 잔류).

    DI seam(hermetic 테스트·바이너리 불요):
      popen : argv -> proc(first_event_ready()/poll()/kill()/communicate(timeout)/returncode
              [+ last_event_at()/partial_output() — 무진행 판정·부분 보존]).
              기본 = _WatchedPopen(cwd/env/text/input_text/clock 바인딩).
      clock : `()` -> 초(단조). 기본 time.monotonic.
      sleep : (초) -> None. 기본 time.sleep(폴 간격 양보). fake 는 여기서 clock 을 전진시킨다.
      log   : (str) -> None. 기본 stderr 1줄.

    `on_spawn_attempt` = **첫 자식 생성 직전** 1회 호출되는 seam(기본 None = 종전 동작). 호출
    지점은 인자 검증·프로필 해소·워치독 준비가 모두 끝나 **다음 문장이 자식을 띄우는** 자리다 —
    그보다 앞의 실패(준비 구간)는 호출이 확실히 0 이라, 이 콜백으로 예산 소유권을 옮기는 호출부가
    그 구간을 환불 대상으로 유지할 수 있다. stall 재시도의 2회차 이후에는 다시 호출하지 않는다
    (소유권 이전은 실행당 1회이고, 그때는 이미 자식이 한 번 떴다).

    스폰 실패의 "자식 없음" 표식(`spawn_failed`)은 두 층이 나눠 붙인다. 실 어댑터
    (`_WatchedPopen`)는 `Popen` 경계를 직접 보므로 preflight 거절(argv/cwd/env 의 NUL)과 확정 기동
    실패(exec 이 실패한 `FileNotFoundError`·`PermissionError`·`NotADirectoryError`·
    `IsADirectoryError`)에 True 를, 그 밖의 `Popen` 예외와 `Popen` 성공 뒤의 초기화 실패에는 False
    를 붙인다(자식 유무를 확정할 수 없으면 보수쪽). 이 루프는 표식 없는 주입 어댑터의 `OSError`
    에만 True 를 덧붙이고, **자식이 한 번이라도 떴으면** 그 뒤의 실패에 표식을 False 로 못박는다:
    앞선 시도의 자식이 있는데 뒤 시도가 기동에 실패한 경우도, `popen` 이 돌아온 **뒤** 본문
    (시계·first_event·poll·communicate·정리)이 무엇으로 실패한 경우도 같다. 실행 단위 보수 규칙이
    시도 단위 관측을 이긴다(이미 나갔을 호출이 환불되지 않게).
    overall_timeout 은 호출부의 벽시계 백스톱이다 — 무진행 판정이 켜지면 주 판정 자리를 내주고
    "감지기가 고장난 경우"의 유한 상한으로만 남는다.
    """
    clock = clock if clock is not None else time.monotonic
    sleep = sleep if sleep is not None else time.sleep
    log = log if log is not None else _default_watchdog_log
    if idle_timeout is not None:
        normalized_idle = normalize_timeout_seconds(idle_timeout)
        if normalized_idle is None:
            raise ValueError(
                f"idle_timeout={idle_timeout!r}: 유한한 정수 초(최소 1) 필요"
            )
        idle_timeout = float(normalized_idle)
    if popen is None:
        def popen(_argv):  # 기본 실 Popen 어댑터(cwd/env/text/stdin/clock 클로저).
            return _WatchedPopen(_argv, cwd=cwd, env=env, text=text,
                                 input_text=input_text, clock=clock)

    attempts = retries + 1  # retries=재시도 횟수 → 총 시도 = retries+1(최초 1 + 재시도 M).
    last_reason = ""
    last_axis: str | None = None
    last_threshold: float | None = None
    last_silence: float | None = None
    stalled_stdout: list[str] = []
    stalled_stderr: list[str] = []
    # 이 **실행**에서 자식이 한 번이라도 실제로 떴는가. 재시도 2회차 이후의 기동 실패가 1회차
    # 자식(이미 호출·과금됐을 수 있다)을 '자식 없음'으로 되돌리지 못하게 막는 상위 사실이다.
    child_created = False
    for attempt in range(1, attempts + 1):
        # ── 실 스폰 경계 ────────────────────────────────────────────────
        # 여기까지가 "확실히 호출 전"이다(인자 검증·프로필 해소·워치독 준비). 다음 문장이 자식을
        # 띄우므로 소유권 이전 콜백은 이 자리에 선다. 재시도 2회차부터는 이미 자식이 한 번 떴으니
        # 다시 넘기지 않는다.
        if on_spawn_attempt is not None and attempt == 1:
            on_spawn_attempt()
        try:
            proc = popen(argv)
        except BaseException as exc:
            if child_created:
                # 앞선 시도의 자식이 이미 떴다 — 그 자식이 프롬프트를 보냈을 수 있으므로 이번
                # 시도가 무엇으로 실패했든 **실행 전체**를 '자식 없음'으로 만들 수 없다. 여기서
                # 되돌리면 이미 나간 호출이 예산에서 사라진다(재시도 2회차의 기동 실패가 1회차
                # 호출을 환불하던 구멍).
                _mark_child_existed(exc)
                raise
            # 첫 자식을 만들다 실패했다 — 자식은 아직 없었다. `_WatchedPopen` 이 실 `Popen`
            # 경계에서 붙인 표식(pre-child 거절 True·`Popen` 성공 뒤 실패 False)이 1순위고,
            # 표식 없는 주입 어댑터는 종전 규칙대로 exec 실패 종류(OSError)만 '자식 없음'으로
            # 확정한다(그 밖의 종류는 표식 없이 올려 상위가 보수적으로 판정한다).
            if isinstance(exc, OSError):
                _mark_spawn_failed(exc, True)
            raise
        child_created = True
        cleaned = False
        try:
            start = clock()
            overall_deadline = start + overall_timeout
            stalled = False
            wall_expired = False
            if first_event_timeout is not None:
                first_deadline = start + first_event_timeout
                while True:
                    if proc.first_event_ready():
                        break  # 첫 이벤트 관측 → 드레인 단계로.
                    if proc.poll() is not None:
                        break  # 첫 이벤트 없이 종료(빠른 exit·에러) → 드레인이 결과 수습.
                    now = clock()
                    first_expired = now >= first_deadline
                    wall_expired = now >= overall_deadline
                    if first_expired and (
                        not wall_expired or first_deadline < overall_deadline
                    ):
                        # coarse poll로 둘 다 지났더라도 더 이른 first-event 축을 최종 선택한다.
                        wall_expired = False
                        stalled = True
                        last_axis = TIMEOUT_AXIS_FIRST_EVENT
                        last_threshold = float(first_event_timeout)
                        last_silence = _silence_seconds(proc, now, start)
                        last_reason = f"첫 이벤트 임계 {first_event_timeout:.0f}s 초과"
                        break
                    if wall_expired:
                        last_silence = _silence_seconds(proc, now, start)
                        break
                    sleep(poll_interval)
            if wall_expired:
                stdout, stderr = _terminate_and_drain(proc, argv=argv)
                cleaned = True
                log(f"[pm-orch] overall watchdog: 벽시계 백스톱 {overall_timeout:.0f}s 초과 "
                    "— 프로세스 그룹 kill. 부분 산출물은 보존. 자동 재시도 안 함.")
                raise WallTimeoutExpired(
                    argv, overall_timeout, silence_seconds=last_silence,
                    output=stdout, stderr=stderr,
                )
            if stalled:
                stdout, stderr = _terminate_and_drain(proc, argv=argv)
                cleaned = True
                if stdout:
                    stalled_stdout.append(stdout)
                if stderr:
                    stalled_stderr.append(stderr)
                log(
                    f"[pm-orch] stall watchdog: {last_reason} kill·재시도 {attempt}/{attempts}"
                )
                continue
            if idle_timeout is None:
                remaining = overall_deadline - clock()
                if remaining < 0:
                    remaining = 0.0
                try:
                    stdout, stderr = proc.communicate(timeout=remaining)
                except subprocess.TimeoutExpired as exc:
                    stdout, stderr = _terminate_and_drain(proc, argv=argv)
                    cleaned = True
                    if not stdout:
                        stdout = exc.output or ""
                    if not stderr:
                        stderr = exc.stderr or ""
                    raise WallTimeoutExpired(
                        argv, overall_timeout,
                        silence_seconds=_silence_seconds(proc, clock(), start),
                        output=stdout, stderr=stderr,
                    ) from exc
            else:
                stdout, stderr = _drain_with_idle_judgment(
                    proc, argv=argv, start=start, idle_timeout=idle_timeout,
                    overall_timeout=overall_timeout, overall_deadline=overall_deadline,
                    clock=clock, sleep=sleep, log=log, poll_interval=poll_interval,
                )
            completed = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
            # 감사용 관측치 — 완주 시점의 침묵 초(다음 kill 의 원인을 사후 확정하는 입력).
            setattr(completed, SILENCE_SEC_ATTR, _silence_seconds(proc, clock(), start))
            return completed
        except BaseException as primary:
            # 여기까지 왔다는 것은 `popen(argv)` 가 이미 핸들을 돌려줬다는 뜻이다 — 이 실행에는
            # 자식이 **있었다**. 그러니 본문(시계·first_event·poll·communicate·정리)에서 무엇이
            # 나오든 실행 전체를 '자식 없음'으로 읽히게 두면 안 된다. 종류만 보는 상위(추가 리뷰어
            # 러너)는 `PermissionError` 같은 확정-기동-실패 종류가 여기서 올라오면 호출 0 으로
            # 오분류해 이미 나갔을 프롬프트를 환불한다. 정리·재전파 **전에** 못박고(정리가 실패해
            # cause 로 매달려도 원 예외 객체는 그대로라 표식이 보존된다), 방향은 True→False 한쪽뿐이라
            # 단조롭다(`_mark_child_existed` 는 앞선 표식도 덮는 실행 단위 사실이다).
            _mark_child_existed(primary)
            if isinstance(primary, (WatchdogTimeoutExpired, ProcessCleanupError)):
                # 이 sentinel들은 해당 판정 경로가 이미 그룹 kill+drain을 끝냈거나 그 정리 자체가
                # 실패했음을 뜻한다. 기존 timeout 의미론을 그대로 재전파한다.
                raise
            # Ctrl-C/SystemExit 및 실행 중 예상 밖 오류 모두 새 세션의 자식을 남기지 않는다.
            # 정리 실패 시에도 원래 BaseException을 재전파하되 cause로 안전 실패를 노출한다.
            if not cleaned:
                try:
                    _terminate_and_drain(proc, argv=argv)
                except Exception as cleanup_error:
                    raise primary from cleanup_error
            raise

    silence_label = (
        f", 중단 시 침묵 {last_silence:.1f}s" if last_silence is not None else ""
    )
    headline = (
        f"startup stall 이 {attempts}회 연속 발생"
        if last_axis == TIMEOUT_AXIS_FIRST_EVENT
        else f"overall watchdog가 {attempts}회 연속 발화"
    )
    diagnosis = (
        " startup network fetch stall 의심(upstream #13841)."
        if last_axis == TIMEOUT_AXIS_FIRST_EVENT else ""
    )
    exhausted = StallWatchdogError(
        f"{headline}("
        f"실제 발화: {last_reason}{silence_label}) — 재시도 소진. "
        f"{diagnosis}".rstrip(),
        timeout_axis=last_axis,
        threshold_seconds=last_threshold,
        silence_seconds=last_silence,
        output="".join(stalled_stdout),
        stderr="".join(stalled_stderr),
    )
    # 여기 도달했다는 것은 모든 시도가 자식을 띄우고 stall 로 kill 됐다는 뜻이다 — 표식을 명시해
    # 상위가 종류 추론으로 이 실행을 '호출 0' 으로 읽을 여지를 남기지 않는다.
    _mark_child_existed(exhausted)
    raise exhausted


# ── opencode 출력 cap-hit(32k 절단) detector (하니스-무관 순수 헬퍼) ──────────────────
# opencode 는 outbound 응답이 출력 cap(32000 토큰·`opencode.jsonc` 실효 = min(limit.output,32000))을
# 넘으면 응답을 **조용히 절단** 하고 finish 를 "stop" 으로 위장한다 — 수신자(PM)는
# 절단을 감지하지 못한다. 파일-전달 규약이 우회책이나, *절단이 실제로 일어났는지* 알 장치가
# 없으면 우회 실패가 조용히 지나간다. 이 detector 가 출력 소비 지점(Supervisor.run_loop)에서 응답이
# cap 근방인지 보고 loud advisory 를 낸다. **advisory·never-block** — 경고+로그만·파이프라인 무중단
# (오탐이 relay 를 죽이면 안 됨). claude 는 범위 밖: claude 는 truncation 을 stop_reason=max_tokens 로
# *네이티브 노출* 하므로 silent 절단 클래스가 아니다 — run_loop 배선은
# 하니스-무관 크기 advisory 라 claude 응답도 지나가나 임계가 정상 응답보다 한참 위라 무영향.

CAP_TOKENS = 32000                   # opencode 실효 출력 cap(opencode.jsonc limit.output).
# 정확 토크나이저는 쓰지 않는다(무거운 의존·하니스-무관 유지·ticket §결정). char 수를 보수적 token
# 근사로 환산한다: char↔token 비는 내용마다 다르나(영어/코드 ≈3.5–4·한글 ≈1.5–2.5 char/token), 순수
# 한글은 실 토크나이저에서 그보다 dense(<1.5)일 수 있어 32000~43200자 절단을 놓칠 위험이 있다. 그
# 한글-dominant false-negative 창을 닫기 위해 char 길이 하한을 **극단 dense CJK(~1.2 char/token)**
# 기준으로 보수 하향한다 — genuine 32k-token 응답은 이 밀도에서도 ≈38400 char 다.
CAP_HIT_MIN_CHARS_PER_TOKEN = 1.2    # char/token 보수적 하한(극단 dense CJK 기준·상한 token 근사).
CAP_HIT_RATIO = 0.90                 # cap 근방 밴드 = char 하한의 90% 이상이면 절단 의심.
# char 임계 = 32000 × 1.2 × 0.90 = 34560 char. 이 값은 (a) 내용 혼합 전반의 genuine 절단 char 길이
# (영어 128k·코드 112k·혼합 80k·한글 48k·극단 dense CJK 38k) *아래* 라 절단을 놓치지 않고
# (false-negative 회피 — 순수 한글 dense 창 포함), (b) 정상 PM relay 응답(수백~수 KB·verbose handoff
# ~15KB)보다 여전히 >2x 위라 오탐 0(false-positive 회피). 두 경계 폭이 넓어 단일 char 임계로 양쪽을
# 만족한다(정확 token 수 불필요·advisory 목적엔 절단 *의심* 표면화면 충분).
CAP_HIT_CHAR_THRESHOLD = int(CAP_TOKENS * CAP_HIT_MIN_CHARS_PER_TOKEN * CAP_HIT_RATIO)  # 34560
CAP_HIT_THRESHOLD_ENV = "PM_OC_CAP_HIT_THRESHOLD"  # char 임계 override(필요 시만·양의 정수).


def cap_hit_char_threshold_default() -> int:
    """PM_OC_CAP_HIT_THRESHOLD env(양의 char 수) 또는 기본 34560. cap-hit 감지 char 임계 해소기."""
    return _env_positive_int(CAP_HIT_THRESHOLD_ENV, CAP_HIT_CHAR_THRESHOLD)


def detect_output_cap_hit(text, *, char_threshold: int | None = None) -> tuple[bool, str]:
    """outbound 응답이 opencode 출력 cap(≈32k 토큰) 근방인지 감지 — silent 절단 의심.

    반환 (hit, reason). hit=True 면 응답이 cap-hit 임계(char) 이상이라 절단 의심 → 호출부가 loud
    advisory 를 낸다(never-block). hit=False 면 reason="". 정확 token 수는 근사(보수적 상한) —
    char↔token 비가 내용마다 달라 exact 는 못 주나 절단 *의심* 표면화엔 충분(advisory 목적).

    char_threshold=None 이면 env 노브(PM_OC_CAP_HIT_THRESHOLD) 또는 기본을 쓴다. text 가 비거나
    None 이면 무발화(정상 크기 = 무조건 통과·오탐 0 보장의 하한 경로).
    """
    if char_threshold is None:
        char_threshold = cap_hit_char_threshold_default()
    length = len(text) if text else 0
    if length < char_threshold:
        return False, ""
    approx_tokens = int(length / CAP_HIT_MIN_CHARS_PER_TOKEN)  # 보수적 상한 근사(token).
    reason = (
        f"응답 {length} 자(≈{approx_tokens} tok 보수적 근사) ≥ 임계 {char_threshold} 자 — "
        f"opencode 출력 cap {CAP_TOKENS} tok 근방"
    )
    return True, reason


def cap_hit_warning_message(reason: str) -> str:
    """cap-hit loud advisory 1줄.

    run_loop 배선은 하니스-무관이라 claude 대형 응답에도 발화할 수 있다 — opencode 한정으로 단정하지
    않고 **조건부**('opencode 하니스라면')로 문구를 중립화해 오해 소지를 없앤다(claude 는 truncation 을
    stop_reason 으로 네이티브 노출하므로 이 silent-절단 클래스가 아님·§메모). 규약 안내는 유지한다.
    stdout(=PM 대화 채널)은 오염하지 않는다 — 호출부가 이 문자열을 stderr/log 로 낸다."""
    return (
        f"[pm-orch] ⚠ 출력 상한(32k tok) 근방: {reason}. **opencode 하니스라면** 이 응답이 silent "
        "절단됐을 가능성이 있다 — opencode 는 32k 출력 토큰에서 응답을 조용히 자르고 finish 를 'stop' "
        "으로 위장한다(수신자 감지 불가). 잘렸다면 파일-전달 규약으로 재시도하라: 대형 "
        "산출물은 파일로 쓰고(opencode 는 safe_write 16KB 청크·write 는 64KB 초과 거부), 응답엔 절대경로 "
        "+ 핵심 요약 ≤10줄만 반환."
    )
