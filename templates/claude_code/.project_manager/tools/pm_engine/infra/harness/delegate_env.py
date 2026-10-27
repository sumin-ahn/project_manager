"""위임 자식 환경과 실행 예산 (층: infra)."""
from __future__ import annotations

from typing import Callable
from collections.abc import Mapping
from pathlib import Path
from collections.abc import Sequence
import argparse
import base64
from pm_engine.infra import conf_store
from pm_engine.infra.harness import contract
from pm_engine.domain import delegate_errors
from pm_engine.domain import delegate_run_rules
import os
from pm_engine.infra import raw_output
import re
import shlex
import subprocess
from pm_engine.infra.harness import watchdog


# ── 드라이버 관측 능력 선언 (분기 특례가 아니라 테이블) ────────────────────────────
#
# 무진행 판정도 startup stall 워치독도 시간 예산도 "드라이버가 무엇을 관측시켜 주느냐 + 어느 축에서
# 도느냐"에만 의존한다. 하니스별 if 분기를 만들지 않고 **선언 테이블**을 읽는다.
#   · progress_signal — 실측 형상:
#       codex    `exec --json`               → 줄 단위 이벤트 스트림.
#       opencode `run --format json`         → 줄 단위 이벤트(각 이벤트에 timestamp 내장).
#       claude   `-p --output-format stream-json --verbose` → 줄 단위 이벤트(옛 `json` =
#                종료 시 단일 덩어리 = 신호 없음이었다).
#   · startup_watchdog — 첫-stdout-이벤트 창 + 유한 재시도. **opencode 전용**인 이유는
#     특례가 아니라 근거다: startup network fetch stall(upstream #13841)이 opencode 축에서만
#     실측됐고, 첫-이벤트 창을 신호 축 전부에 켜면 기동이 느린 실행을 새로 false-kill 한다.
#   · idle_timeout / wall_timeout — 클라우드 축(codex·claude)과 로컬 GPU 축(opencode)의 값이 다르다
#     (근거 수치는 `watchdog` 선언부 주석). 값이 갈리는 건 허용, **코드가 갈리면 위반**이다.
# 테이블은 **`watchdog` 가 단일 소유**한다 — additional_reviewer 도 같은 테이블을 읽어야 리뷰어 축과 위임
# 축의 규칙이 갈리지 않는다(값이 두 군데면 규칙이 둘).


def harness_profile(home, harness: str, conf: dict[str, str] | None = None):
    """하네스 → local.conf override 가 해소된 프로필(`watchdog` 단일 테이블 재사용).

    conf 미지정이면 per-clone local.conf 를 읽는다(판독 실패는 빈 conf 로 fail-soft — 설정 파일
    문제로 위임이 죽지 않는다). 해소 순서는 watchdog.resolve_harness_profile 이 소유한다."""
    if conf is None:
        try:
            conf = conf_store.local_config(home)
        except (OSError, delegate_errors.DelegateError):
            conf = {}
    return watchdog.resolve_harness_profile(
        harness, conf,
        legacy_idle_key=delegate_run_rules.DELEGATE_IDLE_TIMEOUT_KEY,
        legacy_wall_key=delegate_run_rules.DELEGATE_TIMEOUT_KEY,
    )


# 처방 커맨드 렌더 규칙. 처방은 사람이 붙여넣어 **실제로 실행**하는 줄이므로 장부
# 직렬화(POSIX 표기)와 달리 실행 셸의 인용 규칙을 따른다. Windows argv 인용(공백·따옴표)은
# `subprocess.list2cmdline` 이 그대로 담당하고, 아래 상수는 그 규칙 밖의 셸 축만 다룬다.
#   · argv 규칙엔 인용이 불필요해도 셸이 먼저 재해석하는 문자 — 큰따옴표로 중화된다
#     (cmd.exe·PowerShell 공통).
_WINDOWS_SHELL_METACHARACTERS = frozenset("&|<>^()")
#   · 어느 인용으로도 cmd.exe와 PowerShell 양쪽에서 동시에 리터럴화할 수 없는 문자.
#     cmd.exe는 큰따옴표 안에서도 `%VAR%`를 전개하고, PowerShell은 큰따옴표 안에서 `$`/백틱을
#     전개한다. 이 문자가 있으면 재해석 없는 EncodedCommand 형태로 올린다.
_WINDOWS_UNQUOTABLE_CHARACTERS = frozenset("%$`'\r\n")
#   · PowerShell 인자 위치에서 인용 없이 그대로 읽히는 토큰 (그 외는 단일따옴표 리터럴).
_POWERSHELL_BARE_TOKEN_PATTERN = re.compile(r"\A[A-Za-z0-9_%+=:./\\-]+\Z")


def _windows_argv_token(token: str) -> str:
    """Windows argv 규칙으로 토큰 하나를 렌더한다 — 불필요한 인용은 붙이지 않는다."""
    rendered = subprocess.list2cmdline([token])
    if rendered != token:
        # list2cmdline이 공백/따옴표 규칙으로 이미 인용·이스케이프했다.
        return rendered
    if _WINDOWS_SHELL_METACHARACTERS.intersection(token):
        trailing_backslashes = len(token) - len(token.rstrip("\\"))
        return '"' + token + "\\" * trailing_backslashes + '"'
    return token


def _powershell_literal(token: str) -> str:
    """PowerShell 인자 하나 — 인용이 필요한 토큰만 단일따옴표 리터럴로 감싼다."""
    if _POWERSHELL_BARE_TOKEN_PATTERN.match(token):
        return token
    return "'" + token.replace("'", "''") + "'"


def _windows_encoded_command(command: Sequence[str]) -> str:
    """cmd.exe/PowerShell 어느 쪽에서도 메타문자 재해석 없는 Windows 실행 줄을 만든다.

    argv를 PowerShell 인자로 렌더한 뒤 UTF-16LE `-EncodedCommand`로 감싼다. 복사용 줄은
    인코딩본을 유지하고, 사람이 실제 승인 대상을 확인할 수 있도록 바로 아래에 디코드된 줄도 표시한다."""
    script = "& " + " ".join(_powershell_literal(token) for token in command)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    encoded_command = (
        f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"
    )
    return (
        f"{encoded_command}\n"
        f"    PowerShell 디코드(검토용·복사는 위 인코딩본): {script}"
    )


def _windows_command_is_paste_safe(tokens: Sequence[str]) -> bool:
    """평문 한 줄이 cmd.exe와 PowerShell 양쪽에 그대로 붙여넣어지는지 판정한다."""
    if any(_WINDOWS_UNQUOTABLE_CHARACTERS.intersection(token) for token in tokens):
        return False
    # PowerShell은 인용된 첫 토큰을 명령이 아니라 문자열 식으로 읽어 호출 연산자 `&`가 필요하고,
    # cmd.exe는 그 `&`를 못 받는다. 프로그램 토큰에 인용이 붙는 순간 공통 평문 줄은 없다.
    return not tokens or _windows_argv_token(tokens[0]) == tokens[0]


def render_shell_token(value: str, *, windows: bool | None = None) -> str:
    """처방 커맨드에 끼워 넣는 인자 하나를 실행 셸의 인용 규칙으로 렌더한다.

    문장 안에 이미 조립된 커맨드의 한 자리(예: 훅 처방의 `--cwd <경로>`)를 채울 때 쓴다.
    전체 커맨드를 새로 만들 수 있으면 `render_shell_command`를 쓴다 — 그쪽만 재해석 불가
    토큰을 EncodedCommand로 올릴 수 있다."""
    if windows is None:
        windows = contract.running_on_windows()
    text = str(value)
    return _windows_argv_token(text) if windows else shlex.quote(text)


def render_shell_command(argv: Sequence[str]) -> str:
    """처방 커맨드 한 줄을 실행 셸 규칙으로 렌더한다 (붙여넣으면 그대로 실행돼야 한다).

    POSIX는 `shlex.join`, Windows는 `subprocess.list2cmdline` 등가 argv 규칙 + 셸 메타문자
    인용이다. 두 Windows 셸 공통 평문이 불가능한 입력만 EncodedCommand 한 줄로 올린다
    (`_windows_command_is_paste_safe`). 어느 경로도 `&&` 체이닝을 만들지 않는다 —
    PowerShell 5.x가 지원하지 않는다."""
    windows = contract.running_on_windows()
    tokens = [str(token) for token in argv]
    if not windows:
        return shlex.join(tokens)
    if not _windows_command_is_paste_safe(tokens):
        return _windows_encoded_command(tokens)
    return " ".join(_windows_argv_token(token) for token in tokens)


def _prescribed_interpreter() -> str:
    """처방 커맨드가 쓰는 인터프리터 표기 — 진입점 prefix 와 **같은 규칙**을 쓴다.

    Windows 의 `python3`/`python` 은 WindowsApps 가짜 shim 일 수 있어 런처가 1순위다. 그 판정은
    `contract.entrypoint_command` 하나가 소유하고, 여기서는 해소값의 인터프리터 토큰만 꺼내
    쓴다 — 표기가 두 군데면 규칙이 둘이 된다."""
    interpreter, _entry = contract.entrypoint_command(
        contract.DELEGATE_ENTRYPOINT, windows=contract.running_on_windows(),
    )
    return interpreter


def _temp_env_reference(windows: bool | None = None) -> str:
    """처방 문구가 임시 디렉터리를 가리킬 때 쓰는 셸 변수 표기."""
    if windows is None:
        windows = contract.running_on_windows()
    return delegate_run_rules._TEMP_ENV_REFERENCE_WINDOWS if windows else delegate_run_rules._TEMP_ENV_REFERENCE_POSIX


def _temp_env_keys(windows: bool) -> tuple[str, ...]:
    """이 플랫폼에서 임시 디렉터리를 해소하는 env 키 이름들(우선순위 순)."""
    return delegate_run_rules._TEMP_ENV_KEYS_WINDOWS if windows else delegate_run_rules._TEMP_ENV_KEYS_POSIX


def _required_child_env_keys(windows: bool) -> tuple[str, ...]:
    """이 플랫폼의 하네스 필수 env 키 집합."""
    return (
        delegate_run_rules._REQUIRED_CHILD_ENV_KEYS_WINDOWS if windows else delegate_run_rules._REQUIRED_CHILD_ENV_KEYS_POSIX
    )


def _env_source_value(
    source: Mapping[str, str], key: str, *, windows: bool,
) -> str | None:
    """부모 env 에서 키 하나를 읽는다(Windows 는 이름 대소문자를 구분하지 않는다).

    `os.environ` 자체는 Windows 에서 대소문자 무시 매핑이지만, 사본 dict 로 넘어오면 그 성질이
    사라진다 — 그 자리에서 `SystemRoot` 를 못 찾아 필수 키를 떨구는 일이 없게 여기서 흡수한다.
    """
    if key in source:
        return source[key]
    if not windows:
        return None
    folded = key.casefold()
    for name, value in source.items():
        if name.casefold() == folded:
            return value
    return None


def _with_resolved_child_env(
    env: dict[str, str], *, harness: str, windows: bool, source: Mapping[str, str],
) -> dict[str, str]:
    """정제 env 의 임시 디렉터리 축을 해소하고 필수 키 누락을 fail-loud 로 세운다.

    임시 디렉터리는 **이름이 플랫폼마다 다르다** — 부모 env 에 그 이름이 하나도 없으면 자식을
    빈 채로 보내지 않고 실측값(`_gettempdir`)으로 채운다. 필수 키가 그래도 없으면 실행하지 않는다:
    정제가 떨어뜨린 것(부모엔 있었다)인지 환경 자체가 비어 있는 것인지를 사유에 함께 남긴다.
    """
    adjusted = dict(env)
    temp_keys = _temp_env_keys(windows)
    if not any(key in adjusted for key in temp_keys):
        measured = raw_output._gettempdir()
        if not measured:
            raise delegate_errors.DelegateError(
                f"{harness} 위임의 임시 디렉터리를 해소하지 못했습니다 — 후보 키="
                f"{', '.join(temp_keys)} · 실측 temp 디렉터리도 비어 있습니다. "
                "빈 임시 경로로 스폰하지 않습니다."
            )
        adjusted[temp_keys[0]] = measured
    missing = [key for key in _required_child_env_keys(windows) if key not in adjusted]
    if missing:
        detail = ", ".join(
            "{}(부모 env {})".format(
                key,
                "있음 — 정제가 떨굼"
                if _env_source_value(source, key, windows=windows) is not None
                else "없음",
            )
            for key in missing
        )
        raise delegate_errors.DelegateError(
            f"{harness} 위임의 정제 env 에 하네스 필수 키가 없습니다: {detail} · "
            f"플랫폼={'windows' if windows else 'posix'}. 빈 값으로 스폰하면 하네스가 "
            "원인 불명으로 죽으므로 여기서 중단합니다."
        )
    return adjusted


def build_env(
    harness: str, *, windows: bool | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """subprocess env 를 allowlist 로 정제한다 — PM 세션 환경 통째 상속 금지(타 크리덴셜 미상속).

    base 키 + 플랫폼 키 + LC_* 접두 + 하네스별 인증 키만 전달한다. 존재하는 키만 담아 새 env dict 를
    구성한다(os.environ 미상속). 목록은 상수(_ENV_ALLOWLIST_*·_HARNESS_AUTH_ENV)로 조정 가능.

    `windows`/`source` 는 주입 지점이다 — 플랫폼별 키 집합과 필수 키 판정을 실제 실행 플랫폼과
    무관하게 태울 수 있어야 회귀가 두 축을 모두 검증한다(기본은 실제 플랫폼·실제 os.environ)."""
    if windows is None:
        windows = contract.running_on_windows()
    src = os.environ if source is None else source
    allowlist = delegate_run_rules._ENV_ALLOWLIST_BASE + (delegate_run_rules._ENV_ALLOWLIST_WINDOWS if windows else ())
    out: dict[str, str] = {}
    for key in allowlist:
        value = _env_source_value(src, key, windows=windows)
        if value is not None:
            out[key] = value
    for key, value in src.items():
        if any(key.startswith(prefix) for prefix in delegate_run_rules._ENV_ALLOWLIST_PREFIXES):
            out[key] = value
    for key in delegate_run_rules._HARNESS_AUTH_ENV.get(harness, ()):
        value = _env_source_value(src, key, windows=windows)
        if value is not None:
            out[key] = value
    return _with_resolved_child_env(
        out, harness=harness, windows=windows, source=src,
    )


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
    실행 불가·비-repo 는 False(호출부 fail-loud). run_fn 주입(테스트 mock·additional_reviewer 동형 seam)."""
    _run = run_fn or subprocess.run
    try:
        result = _run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return False
    return getattr(result, "returncode", 1) == 0 and bool((result.stdout or "").strip())


def _resolve_timeout(home, args: argparse.Namespace, conf: dict[str, str], harness: str) -> int:
    """위임 turn **벽시계 백스톱**(초)을 양의 정수로 해소한다.

    우선순위: `--timeout`(양수 — _validate_args 가 보장) > 하네스 프로필 해소값. 프로필 해소는
    `watchdog.resolve_harness_profile`(선언 기본 → `delegate.timeout` legacy → `harness.<name>.
    wall_timeout`)이 소유하므로 이 함수는 CLI 우선만 얹는다 — 값 규칙이 두 군데로 갈리지 않는다.
    깨진 conf 값은 그 해소기가 stderr 경고 후 선언 기본으로 fail-soft 한다(usage error 부적합)."""
    if args.timeout is not None:
        return args.timeout  # _validate_args 가 >0 보장
    return int(harness_profile(home, harness, conf).wall_timeout)


def _harness_timeout_budget(home, harness: str, timeout: int) -> int:
    """하네스 **1회 실행+정리**의 최악 소요 예산(초).

    첫-이벤트 워치독을 **선언한 축**(프로필 startup_watchdog=True·현재 opencode)만 다르다 —
    워치독이 **시도마다** overall 예산을 새로 잡으므로 단일 실행이 timeout 을 넘을 수 있다.
    다만 stall 로 죽는 시도는 overall 이 아니라 첫-이벤트 창에서 kill 되므로(`now >= first_deadline`
    분기), 실행 본체는 `timeout + retries×min(첫-이벤트 창, timeout)` 이다. 여기에 부모 wait 5초와
    pipe drain 5초의 연속 정리를 **모든 시도(retries+1)** 에 더한다. 나머지 축도 timeout 뒤 정리
    1회를 같은 공용 식으로 센다. watchdog 노브(env PM_OC_STALL_RETRIES·PM_OC_FIRST_EVENT_TIMEOUT)를
    못 읽으면 재시도 0인 공용 식으로 계산한다.
    (무진행 판정은 벽시계 *안쪽* 에서만 앞당겨 끝내므로 이 상한을 늘리지 않는다.)"""
    try:
        startup_watchdog = harness_profile(home, harness).startup_watchdog
        retries = max(0, int(watchdog.stall_retries_default())) if startup_watchdog else 0
        first_event_window = (
            float(watchdog.first_event_timeout_default()) if startup_watchdog else None
        )
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        # 프로필/노브를 못 읽는 진단 경로에서도 호출층 상한을 낮게 예고하지 않는다. 공유 기본의
        # 현재 최대 재시도(2회)가 각자 wall 전부를 쓸 수 있다고 잡고 정리 3회를 더하는 안전 상한.
        return int(timeout * 3 + 3 * delegate_run_rules._HARNESS_CAP_KILL_GRACE_BUDGET_SEC)
    return int(watchdog.watchdog_execution_budget(
        timeout,
        first_event_timeout=first_event_window,
        retries=retries,
    ))


def max_declared_execution_path_budget(home) -> int:
    """기본 선언으로 가능한 primary→fallback 경로의 최악 본체 예산(초).

    폴백은 자기 turn 예산을 새로 쓰며, 같은 하네스의 다른 모델로도 유효하다. 따라서 단일 프로필
    최댓값이 아니라 **모든 선언 축 두 시도의 합**을 본다. 2단 폴백은 없으므로 깊이는 정확히 2다.
    startup watchdog 재시도 창도 각 시도의 예산에 포함한다.
    """
    budgets = [
        _harness_timeout_budget(home, harness, int(profile.wall_timeout))
        for harness, profile in watchdog.HARNESS_PROFILES.items()
    ]
    return max(primary + fallback for primary in budgets for fallback in budgets)


def _known_harnesses() -> frozenset[str]:
    """Return the declared harness set from the watchdog module's shared capability table."""
    return frozenset(
        str(name).strip() for name in watchdog.HARNESS_SESSION_MARKERS if str(name).strip()
    )
