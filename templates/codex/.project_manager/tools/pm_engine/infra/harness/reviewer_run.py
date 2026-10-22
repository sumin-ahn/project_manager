"""추가 리뷰어 러너와 스폰 경계 (층: infra)."""
from __future__ import annotations

from typing import Callable
from typing import NamedTuple
from pathlib import Path
from typing import Sequence
import inspect
import functools
import os
from pm_engine.domain import reviewer_budget
from pm_engine.infra import reviewer_target
import shlex
import subprocess
from pm_engine.infra.harness import watchdog


def _reviewer_idle_timeout(home, reviewer_cmd: str, idle_timeout: float | None) -> float | None:
    """리뷰어 축의 무진행 상한 해소 — 공용 선언 테이블(`idle_timeout_for_signal`)을 그대로 탄다.

    리뷰어 기본 커맨드(`codex exec --sandbox read-only`)는 `--json` 이 없어 이벤트 스트림이 아니라
    **평문 증분** 축이다. 그래도 진행 신호는 있다 — 실측상 진행 로그(hook/exec/succeeded 라인)가
    stderr 로 촘촘히 흐르고(리뷰 1건 12,233줄) stdout 은 최종 회신뿐이라(498~759 바이트), chunk
    도착 자체를 신호로 보면 파서 없이 같은 판정이 선다. 특례 분기 없이 축 선언만 다르다.
    명시값이 없으면 해소 command가 가리키는 하네스 프로필 값을 쓴다(위임 축과 같은 테이블)."""
    profile = reviewer_target.reviewer_profile(home, reviewer_cmd)
    resolved = idle_timeout if idle_timeout is not None else profile.idle_timeout
    return watchdog.idle_timeout_for_signal(profile.progress_signal, resolved)


def _reviewer_watchdog_settings(home, reviewer_cmd: str) -> tuple[float | None, int]:
    """해소된 리뷰어 프로필의 startup 선언을 공용 워치독 인자로 변환한다."""
    profile = reviewer_target.reviewer_profile(home, reviewer_cmd)
    first_event_timeout = (
        watchdog.first_event_timeout_default() if profile.startup_watchdog else None
    )
    retries = watchdog.stall_retries_default() if profile.startup_watchdog else 0
    return first_event_timeout, retries


def _reviewer_execution_budget(home, reviewer_cmd: str, timeout: int) -> int:
    """리뷰 1회의 실행+재시도별 실제 최악 정리 예산(위임 축과 같은 공용 식)."""
    first_event_timeout, retries = _reviewer_watchdog_settings(home, reviewer_cmd)
    return int(watchdog.watchdog_execution_budget(
        timeout,
        first_event_timeout=first_event_timeout,
        retries=retries,
    ))


def _watchdog_reviewer_run(home, argv, *, input=None, timeout=None, idle_timeout=None,
                           cwd=None, env=None, on_spawn_attempt=None,
                           **_ignored) -> subprocess.CompletedProcess:
    """기본 리뷰어 러너 — `watchdog` 공용 워치독 경유(무진행 주 판정 + 벽시계 백스톱).

    `subprocess.run(..., timeout=)` 을 대체한다. 그 단일 호출은 증분 관측이 아예 없어서 (a) 정상
    진행을 벽시계로 죽이고 (b) `TimeoutExpired.stdout` 에 실려 온 부분 산출물을 아무도 안 읽어
    통째로 버렸다(kill 된 리뷰의 raw 가 헤더 138바이트뿐이던 실측). startup 창/재시도는 리뷰어
    이름 특례가 아니라 **해소된 공유 프로필 선언**을 따른다. 따라서 기본 codex(False)는 종전처럼
    꺼져 있고, 알려진 startup stall 축인 opencode(True)만 유한 재시도를 얻는다.

    `cwd`/`env` 는 리뷰어 실행 조건이다 — 시그니처에 명시해 watchdog 워치독까지 내려보낸다
    (`**_ignored` 가 삼키면 실행 조건이 조용히 사라진다).

    `on_spawn_attempt` 도 같은 이유로 명시한다 — 이 러너의 **앞부분**(프로필 해소·
    워치독 준비)은 자식이 아직 없는 구간이라, 소유권 이전 콜백을 이 함수 입구에서 태우면 스폰 0·
    호출 0 으로 끝난 준비 실패가 예산을 먹는다. 콜백은 watchdog 워치독의 실 스폰 경계까지 그대로
    내려간다."""
    reviewer_cmd = shlex.join(argv)
    first_event_timeout, retries = _reviewer_watchdog_settings(home, reviewer_cmd)
    return watchdog.run_with_first_event_watchdog(
        argv,
        first_event_timeout=first_event_timeout,
        overall_timeout=float(timeout),
        retries=retries,
        idle_timeout=idle_timeout,
        input_text=input,
        cwd=cwd,
        env=env,
        on_spawn_attempt=on_spawn_attempt,
    )  # text 는 워치독 기본(True·utf-8/replace 고정) — 인코딩은 _WatchedPopen 이 소유.


def harness_cap_advisory(
    env: dict[str, str] | None = None, *, execution_budget: int
) -> str | None:
    """리뷰 엔진보다 먼저 끝나는 외부 Bash 명시호출 최대상한을 런타임에 loud 표면화한다.

    문서의 호출층 timeout 계약이 누락된 기존 채택자도 진단을 얻는 백스톱이다. 실행은 차단하지
    않으며 Codex처럼 공개 상한 env가 없는 표면은 판정하지 않는다.
    """
    env = os.environ if env is None else env
    return watchdog.harness_cap_advisory(
        env, execution_budget=execution_budget,
        session_markers=watchdog.HARNESS_SESSION_MARKERS, cap_env=watchdog.HARNESS_CAP_ENV,
        render_missing=None,
        render_invalid=lambda harness, cap_key, raw, required: (
            f"[additional-reviewer] 경고: {harness} 호출층 상한 {cap_key}={raw!r} 해석 불가 — "
            f"리뷰 실행+재시도별 정리+박제 여유 {required}s 이상을 Bash tool timeout으로 명시하세요."
        ),
        render_low=lambda harness, cap_key, cap_seconds, required: (
            f"[additional-reviewer] 경고: {harness} 호출층 최대상한 "
            f"{cap_key}={cap_seconds:g}s < "
            f"리뷰 실행+재시도별 정리+박제 여유 {required}s — 엔진 진단/부분 산출물 보존 전에 하네스가 kill할 수 "
            "있습니다. Bash tool 호출에 장시간 timeout을 명시하세요."
        ),
    )


def _timeout_output(timeout: int, exc: subprocess.TimeoutExpired) -> str:
    """타임아웃 실패 본문 — 사유(무진행/벽시계) + **kill 시점까지 받은 부분 산출물**.

    부분 산출물을 붙이는 게 핵심이다: 판정 기준을 고쳐도 감지기가 늦게 울리는 실행은
    남으므로, 그때 수백 초어치 리뷰가 0바이트가 되는 현행 동작을 그대로 두면 안 된다."""
    idle_seconds = getattr(exc, "idle_seconds", None)
    silence_seconds = getattr(exc, "silence_seconds", idle_seconds)
    timeout_axis = getattr(
        exc, "timeout_axis", "idle" if idle_seconds is not None else "wall"
    )
    threshold = float(getattr(exc, "threshold_seconds", exc.timeout or timeout))
    if timeout_axis != "idle":
        silence_label = (
            f" · 중단 시 실측 침묵 {silence_seconds:.0f}초"
            if silence_seconds is not None else " · 중단 시 침묵 관측 불가"
        )
        head = (f"[리뷰어 타임아웃 — 벽시계 백스톱 {threshold:.0f}초 초과"
                f"{silence_label}] "
                "재시도: `--timeout <초>` 또는 local.conf "
                "`additional_reviewer.timeout=<초>` (양의 정수).")
    else:
        measured = idle_seconds if idle_seconds is not None else silence_seconds
        measured_label = f"{measured:.0f}초" if measured is not None else "관측 불가"
        head = (f"[리뷰어 타임아웃 — 무진행 임계 {threshold:.0f}초 발화"
                f" · 실측 침묵 {measured_label}] "
                f"벽시계 상한 {timeout}초는 미도달. 재시도: `--idle-timeout <초>` 또는 local.conf "
                f"`{reviewer_budget.ADDITIONAL_REVIEWER_IDLE_TIMEOUT_KEY}=<초>` (양의 정수).")
    try:
        return watchdog.format_partial_output(head, exc)
    except Exception as formatter_exc:  # noqa: BLE001 - timeout diagnosis must survive formatter failure.
        return head


# raw 박제/진단 **표시**에만 쓰는 구분자. 판정 경계는 이 문자열이 아니라 아래 두 필드다 —
# 표시 형식으로 경계를 되찾으려 하면 모델 회신이나 재인용된 옛 raw 에 같은 문자열이 들어오는 순간
# `옛 통과 → 구분자 → 이번 반려` 가 '깨끗한 통과'로 잘린다(구분자 파싱 = 신뢰 경계로 부적격).
_STDERR_SECTION_MARKER = "\n[stderr]\n"


class ReviewerOutput(NamedTuple):
    """리뷰어 실행 산출물 — 두 채널을 **구조로** 분리 보관한다.

    `answer` = 리뷰어 회신(stdout). 판정·오염 검출은 오직 이것만 본다.
    `log`    = 진행 로그(stderr). 프롬프트 전문과 검토 대상 diff 원문이 그대로 실려 오므로 판정
               입력이 될 수 없다(실측). raw 박제와 사람 진단에만 쓴다.
    """

    answer: str
    log: str = ""

    @property
    def combined(self) -> str:
        """사람이 읽는 박제/진단 표시 — 표시 전용이고 다시 파싱하지 않는다."""
        return self.answer + (f"{_STDERR_SECTION_MARKER}{self.log}" if self.log else "")


def _as_reviewer_output(value: object) -> ReviewerOutput:
    """주입 러너/스텁이 돌려준 산출물을 두 채널 구조로 정규화한다(문자열 = 회신만)."""
    return value if isinstance(value, ReviewerOutput) else ReviewerOutput(str(value))


class ReviewerRunSeamError(TypeError):
    """주입 runner 의 호출 계약 불일치 — 리뷰어 프로세스 오류와 구분하는 loud sentinel."""


def _declares_keyword(signature: inspect.Signature, name: str) -> bool:
    """runner 가 이 키워드를 **이름으로 선언**했는가(`**kwargs` 흡수는 선언이 아니다)."""
    parameter = signature.parameters.get(name)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _reviewer_run_kwargs(run_fn: Callable, argv: list[str], *,
                         prompt: str, timeout: int, idle_timeout: float | None,
                         cwd: str | None = None,
                         env: dict[str, str] | None = None,
                         on_spawn_attempt: Callable[[], None] | None = None) -> dict:
    """기존 subprocess.run 호환 seam 을 보존하고 명시 지원 runner 에만 확장 키를 전달한다.

    `**kwargs` 만으로는 새 키를 실제 소비하는지 알 수 없다(`subprocess.run` 은 **kwargs 를 Popen 에
    넘겨 뒤늦게 TypeError). 따라서 시그니처에 `idle_timeout`·`on_spawn_attempt` 가 명시된 runner
    에만 전달한다. 호출 전에 bind 해 seam skew 를 프로세스 실행 오류와 분리한다.

    `on_spawn_attempt`(스폰 경계 seam)를 선언하지 않은 기존 주입 러너의 kwargs 는 종전과 바이트
    단위로 같다 — 그 러너의 스폰 경계는 호출부가 알 수 없으므로 콜백은 호출 직전에 태운다.

    `cwd`/`env`(리뷰어 실행 조건)는 **값이 있을 때 무조건** 넘긴다 — 조건부로 넘기면 그 키를
    선언하지 않은 runner 에서 실행 조건이 조용히 사라져(silent degrade) 위임과 갈린다.
    받지 못하는 runner 는 아래 bind 검사가 loud seam 오류로 세운다. 값이 없는 실행(None)의 kwargs 는
    종전과 바이트 단위로 같다.
    """
    kwargs = {
        "input": prompt,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env
    try:
        signature = inspect.signature(run_fn)
    except (TypeError, ValueError):
        return kwargs  # introspection 불가 callable 은 기존 seam 만 보수적으로 전달.
    if _declares_keyword(signature, "idle_timeout"):
        kwargs["idle_timeout"] = idle_timeout
    if on_spawn_attempt is not None and _declares_keyword(signature, "on_spawn_attempt"):
        kwargs["on_spawn_attempt"] = on_spawn_attempt
    try:
        signature.bind(argv, **kwargs)
    except TypeError as exc:
        raise ReviewerRunSeamError(str(exc)) from exc
    return kwargs


# exec 자체가 실패해 자식이 **뜬 적 없는** 확정 기동 실패 — 호출 0·과금 0 이라 예약 환불 대상이다.
#   FileNotFoundError    실행 파일 부재/PATH 미해소
#   PermissionError      실행 권한 없음(비실행 파일·noexec 마운트)
#   NotADirectoryError   경로 중간 요소가 디렉토리가 아님(cwd·실행 파일 경로)
#   IsADirectoryError    실행 대상이 디렉토리
# 종류로 잡는 이유는 주입 러너(`subprocess.run` 호환)가 표식 없이 이 예외들을 그대로 올리기
# 때문이고, watchdog 표식이 있으면 그쪽이 이긴다(`_launch_failed_definitely`).
_DEFINITE_LAUNCH_FAILURES = (
    FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError,
)


def _launch_failed_definitely(exc: BaseException) -> bool:
    """이 예외가 "자식이 뜬 적 없음"을 **확정**하는가.

    1순위는 watchdog 이 스폰 경계에서 붙인 표식(`spawn_failed`)이다 — 그 층만 `Popen` 반환 전후를
    실제로 본다. 그래서 종류표에 없는 pre-child 거절(argv NUL 의 `ValueError`)도 표식이 True 면
    여기서 True 이고, 반대로 `Popen` 성공 뒤의 실패에 종류표의 예외가 실려 와도 표식이 False 라
    여기서도 False 다(재시도 도중 자식이 한 번이라도 떴던 실행 포함).
    표식이 없는 경로(주입 러너·직접 호출)는 예외 종류만으로 보수적으로 판정한다.
    """
    marked = getattr(exc, "spawn_failed", None)
    if marked is not None:
        return bool(marked)
    return isinstance(exc, _DEFINITE_LAUNCH_FAILURES)


def _started_after(exc: BaseException, seam_reached: bool) -> bool:
    """스폰 표식이 있으면 그것으로, 없으면 seam 위치로 `started` 를 정한다 (단일 우선순위).

    우선순위는 한 줄이다 — `spawn_failed=True` → started=False(자식 없음·환불 대상),
    `spawn_failed=False` → started=True(자식 있었음·환불 금지), **표식 없음** → 종전대로 스폰 경계
    콜백이 돌았는지(`seam_reached`). 표식은 `Popen` 앞뒤를 실제로 본 층(watchdog)이 붙이므로 경계
    콜백의 위치 추정보다 강하다: 콜백이 아직 안 돌았어도 watchdog 이 '자식 있었음'을 봤으면 그
    실행은 이미 호출됐을 수 있고, 반대로 콜백이 돈 뒤라도 watchdog 이 '자식 없음'을 확정했으면 호출
    0 이다. 이 판정을 분기마다 다시 쓰면 한 분기만 표식을 빠뜨려도 예산이 조용히 새므로 여기 한
    곳이 소유한다.
    """
    marked = getattr(exc, "spawn_failed", None)
    if marked is not None:
        return not bool(marked)
    return seam_reached


# 두 사건의 진단 표식 — 흡수되면 안 되는 경계다. "설정상 리뷰어 없음"(채택자가 리뷰어를 아예
# 선언하지 않음)은 정상 형상이지만, "실행 파일 해소 실패"(선언은 있는데 그 실행 파일로 자식을
# 띄우지 못함)는 **교차검증이 돌지 않은 결함**이다. 한 문구로 합치면 후자가 전자로 흡수돼
# 조용히 환불된다 — Windows 실행 경로 분해 결함이 여러 릴리즈 동안 숨어 있던 경로다.
NO_REVIEWER_CONFIGURED_MARKER = "추가 리뷰어 미설정"
REVIEWER_LAUNCH_FAILURE_MARKER = "리뷰어 실행 파일 해소 실패"
NO_REVIEWER_CONFIGURED_OUTPUT = (
    f"[{NO_REVIEWER_CONFIGURED_MARKER} — 해소된 대상에 실행할 명령이 없습니다"
    " (local.conf 확인). 호출은 일어나지 않았습니다]"
)


def _launch_failure_output(
    argv: list[str], exc: BaseException, reviewer_cmd: str | None = None,
) -> str:
    """확정 기동 실패의 사람 진단 1줄 — 원인축(해소 실패 vs 실행 불가)을 구분해 말한다.

    "설정상 리뷰어 없음"(`NO_REVIEWER_CONFIGURED_OUTPUT`)과 갈라진 문구를 쓴다 — 이 실행은 리뷰어가
    선언돼 있는데도 교차검증이 돌지 않은 실행이다. 선언 문자열과 실제로 시도한 실행 파일이 다르면
    (분해·인용 사고) 그 대조를 같이 낸다: Windows 채택자가 본 증상이 "설치 또는 PATH 확인"뿐이라,
    엔진이 경로를 뭉갠 실행과 리뷰어를 안 깐 실행을 구분할 수 없었다.
    """
    attempted = argv[0] if argv else ""
    if isinstance(exc, FileNotFoundError):
        return (
            f"[{REVIEWER_LAUNCH_FAILURE_MARKER}: '{attempted}' 로 자식을 띄우지 못했습니다 — "
            f"설치·PATH·경로 표기 확인{_declared_command_mismatch(attempted, reviewer_cmd)}. "
            "추가 리뷰어 교차검증은 실행되지 않았습니다(호출 없음)]"
        )
    return (
        f"[리뷰어 명령 '{attempted}' 를 실행할 수 없음 ({type(exc).__name__}: {exc}) — "
        f"실행 권한·경로 확인{_declared_command_mismatch(attempted, reviewer_cmd)}. "
        "호출은 일어나지 않았습니다]"
    )


def _declared_command_mismatch(attempted: str, reviewer_cmd: str | None) -> str:
    """시도한 실행 파일이 선언 문자열에 없으면 그 대조를 진단에 덧붙인다 (빈 문자열 = 일치).

    분해가 경로를 훼손하면 argv[0] 은 엔진이 구조화 키(`additional_reviewer.harness`·`.model`·
    `.reasoning`)로 해소한 커맨드의 어떤 부분문자열도 아니다 — "설치 안 됨"과 "엔진이 커맨드를
    잘못 분해함"을 사람이 그 자리에서 가를 수 있는 유일한 사실이다.
    """
    if not reviewer_cmd or not attempted or attempted in reviewer_cmd:
        return ""
    return (
        f" · 해소된 커맨드에 없는 실행 파일로 분해됐습니다(커맨드: '{reviewer_cmd}') — "
        "경로 구분자·인용을 확인하세요"
    )


def _run_reviewer_ex(
    home, prompt: str,
    reviewer_cmd: str,
    timeout: int | None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None,
    idle_timeout: float | None = None,
    metrics: dict[str, object] | None = None,
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    argv: Sequence[str] | None = None,
    stdin_text: str | None = None,
    on_spawn_attempt: Callable[[], None] | None = None,
) -> tuple[bool, ReviewerOutput, bool]:
    """리뷰어 실행 본체 + 자식 프로세스 스폰 여부(started) 신호.

    반환: (성공 여부, `ReviewerOutput`(회신/로그 **분리 보관**), started). started=False = 자식 프로세스가 *확실히 시작되지
    않음*(호출 0·과금 0) — 빈 reviewer_cmd·seam 계약 오류·**스폰 경계 전의 준비 실패**·확정 기동
    실패(exec 이 실패한 `FileNotFoundError`·`PermissionError`·`NotADirectoryError`·
    `IsADirectoryError`). started=True = 스폰됨(프롬프트가 호출·과금됐을 수 있음) — 정상 종료
    (비-0 rc 포함)·타임아웃·스폰 뒤의 실행 오류. 타임아웃/불확실은 이미 호출됐을 수 있으므로
    보수적으로 started=True — 라운드 환불은 started=False 일 때만 해(반복 타임아웃으로 상한을
    무한 우회하지 못하게). 확실히 호출 전인 경우만 환불한다.

    **판정 기준**: 기본 러너가 `subprocess.run` 단일 호출에서 `watchdog` 공용 워치독으로
    바뀌어, 주 판정이 "시작 후 경과"가 아니라 "마지막 진행 이후 무진행"이다(벽시계는 백스톱).
    `idle_timeout=None` 이면 reviewer_cmd 프로필 선언이 적용된다.

    `run_fn` 주입의 기존 계약은 subprocess.run 호환 키까지다. 새 `idle_timeout` 은 시그니처에 그
    이름을 명시한 runner 에만 조건부 전달한다. 호출 계약 불일치는 일반 "리뷰어 실행 오류"로
    삼키지 않고 seam 오류로 loud 구분한다.

    `argv`/`stdin_text` = 구조화 대상의 wire transport 주입. 미지정이면 종전대로
    `_split_reviewer_argv(reviewer_cmd)`(실행 플랫폼 규칙 분해) + 프롬프트 stdin 이다.

    `on_spawn_attempt` = **실제 자식 생성 직전** 1회 호출되는 seam(기본 None = 종전 동작). 라운드
    예약 소유권 이전(`_PreSpawnReservation.hand_off`)이 이 콜백을 탄다. 호출 지점은 러너가 스폰
    경계를 선언했는지에 따라 두 자리다:

    · 선언한 러너(기본 워치독 러너 — `on_spawn_attempt` 키워드를 이름으로 가짐)면 콜백을 **러너
      안까지** 내려보내, watchdog 워치독이 `Popen` 하기 바로 앞에서 돈다. 러너 입구~`Popen` 사이의
      준비 구간(프로필 해소·워치독 셋업)에서 실패하면 콜백이 아직 돌지 않았으므로
      이 함수는 `started=False` 로 돌아간다 — 스폰 0·호출 0 인 실행이 예산을 먹지 않는다.
    · 선언하지 않은 기존 주입 러너면 종전 그대로 **호출 직전**에 한 번 돈다(호환 보존). 그 러너의
      내부 스폰 경계는 알 수 없으므로 그 뒤의 불확실 실패는 보수적으로 started=True 다.

    콜백은 어느 경로로도 최대 1회만 돈다(멱등 래퍼). 러너가 정상 반환했는데 콜백을 부르지 않은
    형상은 자식이 있었던 것으로 보고 반환 직후 한 번 돌린다 — 그러지 않으면 스폰된 실행의 예산이
    조용히 환불된다.

    콜백이 **돈 뒤에** 돌아온 `started=False`(확정 기동 실패)는 자식이 없었다고 판명된 결과라,
    호출부(`run_review` 의 `on_no_spawn`)가 그 자리에서 소유권을 되찾는다 — 판정 자체는 이 함수의
    반환값 하나가 소유하고, 되돌림 시점만 호출부 계약이다. 확정 기동 실패의 판정 입력은 watchdog 이
    붙이는 스폰 표식(`spawn_failed`)이 1순위, 없으면 예외 종류다 — `Popen` 성공 **뒤**의 실패에
    같은 예외 종류가 실려 오면 watchdog 표식이 그것을 post-spawn 으로 못박아 보수적 started=True 가
    유지된다. 그 우선순위(표식 True→False·표식 False→True·표식 없음→스폰 경계 위치)는 확정 기동
    실패 분기만이 아니라 seam 호출 오류(`TypeError`)·일반 실행 오류 분기에도 **같이** 적용된다
    (`_started_after` 한 곳이 소유). 한 분기만 표식을 무시해도 그 자리로 예산이 샌다."""
    if metrics is not None:
        metrics.clear()
        metrics.update({"rc": 1, "silence_sec": None})
    _run = run_fn or functools.partial(_watchdog_reviewer_run, home)
    argv = list(argv) if argv is not None else reviewer_target._split_reviewer_argv(reviewer_cmd)
    if not argv:
        # "설정상 리뷰어 없음" — 실행 파일 해소 실패(`_launch_failure_output`)와 **다른 사건**이다.
        return False, ReviewerOutput(NO_REVIEWER_CONFIGURED_OUTPUT), False
    if timeout is None:  # 미지정 호출(공개 facade) — 리뷰어 프로필의 벽시계 백스톱.
        timeout = int(reviewer_target.reviewer_profile(home, reviewer_cmd).wall_timeout)
    # 스폰 경계를 실제로 지났는가 — 확실치 않은 실패의 started 판정 입력이다. 경계 전이면 자식이
    # 없으므로 호출 0 이고, 경계 뒤면 프롬프트가 이미 나갔을 수 있다.
    spawn_reached = False

    def _spawn_seam() -> None:
        """스폰 경계 1회 통과 — 예약 소유권을 호출부 계약대로 넘긴다(멱등)."""
        nonlocal spawn_reached
        if spawn_reached:
            return
        spawn_reached = True
        if on_spawn_attempt is not None:
            on_spawn_attempt()

    try:
        kwargs = _reviewer_run_kwargs(
            _run, argv,
            prompt=prompt if stdin_text is None else stdin_text,
            timeout=timeout,
            idle_timeout=_reviewer_idle_timeout(home, reviewer_cmd, idle_timeout),
            cwd=None if cwd is None else str(cwd), env=env,
            on_spawn_attempt=_spawn_seam,
        )
        # ── 스폰 경계 seam ──────────────────────────────────────────────
        # 스폰 경계를 **이름으로 선언한** 러너면 콜백이 그 안(watchdog 의 `Popen` 직전)에서 돈다 —
        # 러너 입구~Popen 사이의 준비 실패는 스폰 0·호출 0 이라 예약이 환불 대상으로 남는다.
        # 선언하지 않은 기존 주입 러너는 종전 자리(호출 직전)에서 한 번 돈다(호환 보존).
        if "on_spawn_attempt" not in kwargs:
            _spawn_seam()
        result = _run(argv, **kwargs)
        # 러너가 결과를 돌려줬다 = 자식이 떴다. 스폰 경계를 선언해 놓고 부르지 않은 러너가 있어도
        # 여기서 소유권이 넘어간다 — 스폰된 실행의 예산이 조용히 환불되지 않게 하는 백스톱이다.
        _spawn_seam()
        if metrics is not None:
            metrics["rc"] = int(result.returncode)
            metrics["silence_sec"] = getattr(result, "silence_sec", None)
        # 두 채널을 합치지 않는다 — 합치면 경계가 문자열이 되고, 그 문자열은 모델 회신이나
        # 재인용된 옛 raw 에 그대로 나타날 수 있다(판정 경계로 부적격).
        return (
            result.returncode == 0,
            ReviewerOutput(result.stdout or "", result.stderr or ""),
            True,
        )
    except subprocess.TimeoutExpired as exc:
        # 프로세스가 시작돼 실행 중 타임아웃 — 프롬프트가 이미 호출·과금됐을 수 있다 → started=True.
        # 타임아웃은 정의상 "자식을 기다리다 만료"라 스폰 경계 신호와 무관하게 보수적으로 True 다
        # (반복 타임아웃으로 상한을 무한 우회하는 길을 열지 않는다·MF-A).
        if metrics is not None:
            metrics["silence_sec"] = getattr(
                exc, "silence_seconds", getattr(exc, "idle_seconds", None)
            )
        return False, ReviewerOutput(_timeout_output(timeout, exc)), True
    except _DEFINITE_LAUNCH_FAILURES as exc:
        # exec 자체가 실패해 자식이 뜬 적 없다(파일 부재·실행 권한·경로 형상) → started=False
        # (환불 대상). 단 watchdog 표식이 "Popen 성공 뒤"라고 못박은 예외는 예외다 — 그건 자식이
        # 있었던 실행이라 보수 규칙으로 내려보낸다.
        if not _launch_failed_definitely(exc):
            return False, ReviewerOutput(f"[리뷰어 실행 오류: {exc}]"), True
        if metrics is not None:
            metrics["rc"] = 127 if isinstance(exc, FileNotFoundError) else 126
        return (False,
                ReviewerOutput(_launch_failure_output(argv, exc, reviewer_cmd)),
                False)
    except ReviewerRunSeamError as exc:
        # 호출 전 bind 실패 — 자식 프로세스는 확실히 시작되지 않았다. 라운드 환불 가능.
        return False, ReviewerOutput(
            f"[리뷰어 runner seam 계약 오류 — 호출 전 차단: {exc}]"), False
    except TypeError as exc:
        # runner 내부에서 발생한 키워드/시그니처 skew. 판정은 다른 분기와 **같은 우선순위**다 —
        # watchdog 표식이 있으면 그것이 이기고(True→환불·False→환불 금지), 없을 때만 스폰 경계 위치를
        # 본다. 표식을 무시하고 경계 위치만 보면, `Popen` 뒤에 실려 온 TypeError 가 콜백이 아직 안
        # 돈 러너에서 올라올 때 이미 나간 호출이 환불된다.
        return (False, ReviewerOutput(f"[리뷰어 runner seam 호출 오류: {exc}]"),
                _started_after(exc, spawn_reached))
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "process_cleanup_failed", False) is True:
            output = (
                f"[리뷰어 프로세스 정리 실패: {exc} — 잔존 프로세스 가능성 때문에 자동 재시도/"
                "폴백 금지, 사람 확인 필요. 부분 산출물 보존]"
            )
            if getattr(exc, "output", ""):
                output += f"\n{exc.output}"
            return False, ReviewerOutput(output, getattr(exc, "stderr", "") or ""), True
        if _launch_failed_definitely(exc):
            # watchdog 이 스폰 경계에서 "자식 없음"으로 표식한 실패 — 종류는 exec `OSError` 만이
            # 아니다(argv NUL 의 `ValueError` 처럼 `Popen` 이 fork 전에 거절한 형상도 여기 온다).
            # 자식이 없었으므로 호출 0·과금 0 이고 환불 대상이다.
            return (False,
                    ReviewerOutput(_launch_failure_output(argv, exc, reviewer_cmd)),
                    False)
        # 스폰 경계 뒤의 실패는 시작 여부가 불확실하다 — 보수적으로 started=True (상한 우회 방지 >
        # 과잉 카운트). 경계 **전**의 실패는 자식이 아직 없었다는 사실이 확정이라 False 다. 단
        # watchdog 이 '자식 있었음'(False)으로 못박은 예외는 경계 콜백이 아직 안 돌았어도 True 다 —
        # 위 `_DEFINITE_LAUNCH_FAILURES` 분기와 같은 우선순위(표식 > 위치)를 여기서도 쓴다.
        return (False, ReviewerOutput(f"[리뷰어 실행 오류: {exc}]"),
                _started_after(exc, spawn_reached))
