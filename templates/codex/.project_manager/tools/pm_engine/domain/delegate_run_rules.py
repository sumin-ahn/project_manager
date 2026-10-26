"""위임 실행과 재개 값 (층: domain)."""
from __future__ import annotations

import datetime

from typing import NamedTuple
from pathlib import Path
from collections.abc import Sequence
from pm_engine.domain import conf_schema
from pm_engine.domain import delegate_errors
import re



# ── 도메인 상수 ────────────────────────────────────────────────────────────

TIER_CHOICES: tuple[str, ...] = ("normal", "hard")


_ARCHITECT_TEST_ID_RE = re.compile(r"AT-[0-9]{3,}")
_CONTRACT_TEST_TARGET_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?P<path>tests/[A-Za-z0-9_./-]+\.py)"
    r"(?=::|(?:(?:에서|에는|에도|에게|으로|에|은|는|이|가|을|를|와|과|의|로))?"
    r"(?:$|[\s,.;:!?…·，。)\]}>'\"`]))",
)
# 같은 경계 규칙의 확장 — 확장자 있는 repo-relative 파일 경로면 트리 어디든 잡는다.
# 선행 부정 lookbehind 가 절대경로의 꼬리를 잘라 오는 것을 막는다(루트 시작 경로는 미매치).
_CONTRACT_REPO_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?P<path>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
    r"(?=::|(?:(?:에서|에는|에도|에게|으로|에|은|는|이|가|을|를|와|과|의|로))?"
    r"(?:$|[\s,.;:!?…·，。)\]}>'\"`]))",
)


# 권한 역할축 — write=저장소 파일 쓰기·read=저장소 read-only(+reviewer 는 테스트 실행).
WRITE_ROLES: frozenset[str] = frozenset({"developer", "architect"})
READ_ROLES: frozenset[str] = frozenset({"researcher", "code-reviewer"})
# 하네스의 기본 권한축과 resume 재실행 안전성은 같은 분류가 아니다. read 역할(code-reviewer·
# researcher)은 제품 worktree에는 read지만 run-dir 의 라운드 파일 `NN-<role>.md` 는 write한다.
# 세션 불일치 뒤 fresh 재실행하면 새 라운드가 하나 더 생기므로 resume 축에서는 mutating으로 다룬다.
RESUME_MUTATING_ROLES: frozenset[str] = WRITE_ROLES | frozenset(
    {"code-reviewer", "researcher"}
)

# read 역할의 회귀용 임시 쓰기 표면 — 아래 설치본에서 **직접 실측한 값**이다.
#   · codex-cli 0.147.0 실 CLI: 동적 `default_permissions`/`permissions.<name>` override는
#     argv에 실려도 `codex exec`의 patch 도구가 read-only로 남아 ticket-copy write를 거부했다.
#     공개 CLI 계약인 `-s workspace-write` + `--add-dir <ticket-copy-dir>`는 attempt tmp를 `-C`로
#     재앵커했을 때 tmp와 정확한 copy run-dir만 writable로 만들고 원 worktree는 read-only로 남긴다.
#     named profile 문자열의 존재가 아니라 실제 모델 edit→harvest를 release live가 단언한다.
#   · claude 2.1.227: `--help`의 `--add-dir <directories...>`가 추가 tool-access 루트를 받는다.
#   · opencode 1.18.16: `run --help`에는 추가-dir 플래그가 없다. custom 역할 카드를 `mode: all`로
#     출하해 native task와 cross `run --agent <role>`가 같은 정의를 쓰며, code-reviewer 카드는
#     run-dir 라운드 파일 기록을 위해 edit 가능하다. repo 쓰기 표면은 경고와 위임 전후 감사로
#     관리한다.
#     researcher 카드의 read-only 권한에는
#     TMPDIR=/tmp/pm_delegate_probe일 때
#     `external_directory /tmp/pm_delegate_probe/opencode/* = allow`, `edit * = deny`가 나왔다.
#     즉 고정 `/tmp/opencode/*`가 아니라 `${TMPDIR}/opencode/*`이므로 attempt의 그 하위만 안내한다.
# 버전 문자열을 런타임 추측해 분기하지 않는다. 위 값이 바뀌면 이 선언·회귀를 함께 갱신한다.
_OPENCODE_READ_TMP_PARENT = "opencode"
_OPENCODE_ALLOWED_TMP_COMPONENT = "opencode"
_READ_TMP_PREFIX = "pm_delegate_read_"
# attempt 이름의 16진 꼬리 길이. 유일성은 엔트로피가 아니라 **배타 mkdir**(선점된 이름이면
# FileExistsError 로 끝난다)이 소유하므로 이름은 짧게 유지한다 — 격리 자리가 프로젝트 안으로
# 들어오면서 접두가 길어졌고(Windows 실측: `%TEMP%` 33자 → PM 홈 임시 루트 49~62자), 잔존 트리
# 실측 꼬리 158자 기준으로 32자 이름은 MAX_PATH(259)를 넘겼다. 12자면 그 예산 안에 든다
# (같은 파일의 `_WRITE_PROBE_SUFFIX_HEX` 와 같은 사유).
_READ_TMP_SUFFIX_HEX = 12
# 정리가 끝내 실패해 잔존 트리가 남은 실행의 rc. 하네스 turn 이 성공(rc=0)이어도 그 실행은
# 실패다. 인프라 실패 분류(폴백 대상)를 타지 않는 엔진 측 실패라 `_midrun_failure_result` 와
# 같은 1 이다.
_READ_TMP_RESIDUE_RC = 1
_READ_TMP_ENV_KEYS: tuple[str, ...] = ("TMPDIR", "TMP", "TEMP")
# 하네스별 차이는 orchestration 함수 안의 이름 분기가 아니라 이 측정 선언이 소유한다.
_READ_TMP_PARENT_COMPONENT_BY_HARNESS: dict[str, str | None] = {
    "codex": None,
    "claude": None,
    "opencode": _OPENCODE_READ_TMP_PARENT,
}
_READ_TMP_ARGV_MODE_BY_HARNESS: dict[str, str] = {
    "codex": "workspace-add-dir",
    "claude": "add-dir",
    "opencode": "role-agent",
}
_READ_TMP_WRITABLE_COMPONENT_BY_HARNESS: dict[str, str | None] = {
    "codex": None,
    "claude": None,
    "opencode": _OPENCODE_ALLOWED_TMP_COMPONENT,
}
_READ_TMP_TMP_TEMP_USE_WRITABLE_PATH_BY_HARNESS: dict[str, bool] = {
    "codex": False,
    "claude": False,
    "opencode": True,
}
_READ_TMP_PYTEST_REL_BY_HARNESS: dict[str, str] = {
    "codex": "pytest",
    "claude": "pytest",
    "opencode": "opencode/pytest",
}
_READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS: dict[str, bool] = {
    "codex": True,
    "claude": False,
    "opencode": False,
}

# read tmp 생성/회수 수단 — 위 primitive 가 없다고 **권한을 강등하지 않는다**. ACL 플랫폼
# (Windows)은 같은 보장을 다른 수단으로 낸다: 예측 불가 이름의 배타 생성(존재하면 실패) +
# 소유자 전용 ACL(`fs.restrict_to_owner`) + 생성 identity 재검증 + 잔재를 남기지 않는
# 회수(`fs.force_rmtree`). 두 수단이 **모두** 없는 플랫폼만 회귀-불가 경로로 간다.
_READ_TMP_STRATEGY_FD = "fd"
_READ_TMP_STRATEGY_OWNER_ACL = "owner-acl"

READ_REGRESSION_UNAVAILABLE_NOTE = (
    "이 read 역할 실행은 격리된 쓰기 가능 임시 디렉터리를 안전하게 만들 수 없어 회귀를 "
    "직접 돌릴 수 없다. 회귀 숫자는 developer 보고값을 인용하고, 직접 실행하지 못했다는 "
    "사실과 이유를 최종 보고서에 명시하라. 침묵하거나 직접 실행한 것처럼 쓰지 마라."
)

# 위임 turn 의 시간 예산(무진행 상한 + 벽시계 백스톱)은 **하네스 프로필**이 소유한다
# (`watchdog.HARNESS_PROFILES` — 클라우드 축 codex/claude 와 로컬 GPU 축 opencode 의 실측 근거가
# 거기 주석에 있다). 이 모듈에 단일 상수를 두지 않는 이유: 값이 두 군데면 규칙이 둘이 되고,
# 실제로 초기 구현의 단일 기본값(클라우드 표본 기반)이 3시간짜리 로컬 opencode 위임을 죽였다.
# 해소 순서: `--timeout`(일회성) > local.conf `harness.<name>.wall_timeout` > `delegate.timeout`
# (표면-flat legacy) > 프로필 선언. 무진행 축은 `harness.<name>.idle_timeout` >
# `delegate.idle_timeout` > 프로필 선언.
# 폴백이 발동하면 primary 소진 후 1회 더 실행한다 — 실행 1회의 최악 소요는 하네스마다 달라서
# _harness_timeout_budget 이 계산한다(추가 폴백 없음 — 상한은 두 시도 예산의 합으로 닫히고,
# 세션 재사용 라운드는 미일치 fresh 재실행분 primary 예산 1회가 더 가산된다).

# 위임 벽시계와 하네스 Bash 상한 사이에 남겨 두는 최소 여유(초) — kill·수확·분류·박제의 예산.
# 부등식은 `max(유효 primary+fallback 실행 경로) + 여유 ≤ 하네스 상한` 이고
# tests/test_settings_hygiene.py 가 전 표면(claude settings·opencode shell-export 지침)을 기계로
# 단언한다. 여유가 0이면 엔진이 자기 타임아웃을 분류(인프라 실패 → 폴백)하기 전에 하네스가 먼저
# 죽여 진단이 통째로 사라진다(원인 불명 kill 8회의 한 축).
#
# 이 트리에서 `pytest ... --durations=30`으로 실제 종료 경로를 재었다: git scope audit 0.03s,
# fallback raw 체인 0.02s, 나머지 분류·prompt 합성은 각 0.01s 미만. 이 보조 7단계는
# 관측 최댓값을 초 단위로 올려 7s, 플랫폼 편차를 위해 다음 10초 경계로 올린다. 프로세스 정리는
# 이 margin에 뭉뚱그리지 않는다 — `watchdog` 공용 식이 부모 wait 5s + pipe drain 5s를 **시도마다**
# 실행 예산에 직접 산입한다(startup 재시도 포함).
_HARNESS_CAP_KILL_GRACE_BUDGET_SEC = 10  # 호환/감사용: 정리 1회의 연속 wait+drain 최악값.
_HARNESS_CAP_MEASURED_AUX_BUDGET_SEC = 7
_HARNESS_CAP_MARGIN_SEC = 10

# 인프라 실패 클래스 라벨(loud 메시지·raw provenance 에 그대로 실리는 안정 문자열).
FAILURE_CLASS_LAUNCH = "스폰 실패/바이너리 부재"
FAILURE_CLASS_TIMEOUT = "타임아웃"
FAILURE_CLASS_STALL = "첫-이벤트 stall(재시도 소진)"
FAILURE_CLASS_CLEANUP = "프로세스 정리 실패"
FAILURE_CLASS_QUOTA = "한도/레이트리밋"
FAILURE_CLASS_AUTH = "인증 실패"

# RunResult 의 **명시 실패 신호** 키(rc 값 추론 금지·codex must-fix). 엔진(_default_run_fn·
# _execute_attempt)만 세팅한다 — 하네스가 우연히 같은 rc 를 내도 분류되지 않는다.
RUN_RESULT_LAUNCH_FAILED = "launch_failed"   # 바이너리 부재/PATH/exec 권한 — 프로세스가 뜨지 못함
RUN_RESULT_STALLED = "stalled"               # opencode 첫-이벤트 stall(유한 재시도 소진·watchdog)
RUN_RESULT_CLEANUP_FAILED = "cleanup_failed" # kill/drain 실패 — 부분 산출물 보존 + 자동 폴백 금지
# 관측 침묵 초(마지막 진행 이벤트 이후) — 실패 신호가 아니라 **감사 관측치**다. 감사 헤더에 실려
# 다음 kill 의 원인(하네스 상한 vs 무진행 vs 기타)을 사후 확정 가능하게 한다.
RUN_RESULT_SILENCE_SEC = "silence_sec"
# 무진행 판정으로 죽었는지(벽시계 백스톱과 구분) — 감사 헤더 사유 표기 입력.
RUN_RESULT_IDLE_KILLED = "idle_killed"
# 중단 진단은 호출부의 primary/default 값을 재사용하지 않고 워치독이 싣는 실제 발화값을 전달한다.
RUN_RESULT_TIMEOUT_AXIS = "timeout_axis"
RUN_RESULT_TIMEOUT_THRESHOLD_SEC = "timeout_threshold_sec"

# opencode 첫-이벤트 stall 을 stderr 에 찍는 엔진 마커(단일 출처) — 분류기의 백스톱 신호로도 쓴다.
OPENCODE_STALL_MARKER = "[opencode 첫-이벤트 stall:"

# 위임 마스터 스위치 키(per-clone·**기본 ON**). "PM 이 위임을 해도 되는가" 하나만 판정하며 채널
# (native/cross)로 갈리지 않는다 — 호출을 별도 동의 축으로 게이트하지 않는다.
DELEGATE_ENABLED_KEY = "delegate.enabled"

# 표면-flat legacy 시간 노브(하네스 무관·기존 채택자 설정 보존). 하네스별 키
# `harness.<name>.wall_timeout`/`.idle_timeout` 이 설정돼 있으면 그쪽이 이긴다(더 구체적인 선언).
DELEGATE_TIMEOUT_KEY = "delegate.timeout"
DELEGATE_IDLE_TIMEOUT_KEY = "delegate.idle_timeout"

# 위임 채널 실패 안내의 공통 꼬리 — **무음 대체 금지**. 실행 실패는 fail-loud rc 로 끝나고,
# 이 호출은 명시 설정(`delegate.<role>[.hard].fallback.*` 원자 tuple) 밖의 다른 하네스/모델로
# 자동 대체하지 않는다. 실사고: 실패 안내가 "네이티브/다른 하네스로 재시도를 검토하라"였고, 그
# 문구를 읽은 세션의 native 모델이 위임 대상 작업을 조용히 대행했다 — 장부엔 그 대행 기록이
# 없어 사후에 누가 무엇을 했는지 재구성할 수 없었다. 그래서 안내는 **명시 재호출**만 지시한다
# (실패 자체는 raw 장부의 그 레코드에 rc 와 함께 남는다).
NO_SILENT_SUBSTITUTE_NOTE = (
    "  재위임은 명시 재호출만 — 이 호출은 설정된 폴백 밖의 다른 하네스/모델로 자동 대체하지 "
    "않습니다. 현 세션의 native 모델이 대신 수행하지 마세요(실패는 raw 장부에 남습니다: "
    "`pm_delegate.py raw --limit 5`)."
)

# 드라이버 계약 테이블(reasoning 허용집합·codex sandbox 모드·opencode agent·claude `--tools`·
# opencode 첨부 message)은 **`contract` 가 단일 소유**한다 — 같은 세 CLI 를 추가 리뷰어 표면도
# 스폰하므로 값이 두 군데면 규칙이 둘이 된다. 실측 근거 주석도 그 선언부에 있다. 이 모듈의
# `_validate_*`/`build_*_argv` 는 그 계약을 부르는 얇은 wrapper 다.

# subprocess env allowlist — PM 세션 환경을 통째 상속시키지 않고 최소 키만 전달(타 크리덴셜
# 미상속). base + 플랫폼 키 + LC_* 접두 + 하네스별 인증 키. 가능하게 상수로 둔다.
_ENV_ALLOWLIST_BASE: tuple[str, ...] = (
    "PATH", "HOME", "LANG", "TERM", "USER", "LOGNAME", "TMPDIR",
)
# Windows 에서만 추가로 흘리는 키. base 는 POSIX 이름만 담고 있어서, 이 목록이 없으면 정제 env 가
# 하네스 실행에 필요한 값을 **통째로 떨군다**(Windows 실측: opencode 자식 env 에 `TMP` 부재).
#   · TEMP/TMP — Windows 에는 POSIX 의 `/tmp` 같은 규약 기본값이 없다. 없으면 세 하네스와 그
#     자식(pytest·node)이 임시 파일 위치를 잃는다.
#   · SystemRoot/SystemDrive/windir/ComSpec/PATHEXT — 프로세스 스폰·시스템 DLL 로딩·실행 확장자
#     해소의 최소 집합(Node 기반 CLI 가 SystemRoot 없이는 뜨지 않는다).
#   · USERPROFILE/HOMEDRIVE/HOMEPATH/APPDATA/LOCALAPPDATA/USERNAME — 세 하네스의 **파일 auth**
#     (POSIX HOME 앵커의 Windows 등가)와 설정/캐시 위치.
_ENV_ALLOWLIST_WINDOWS: tuple[str, ...] = (
    "TEMP", "TMP", "SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "USERNAME",
)
_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_",)
# 정제 env 가 자식에게 **반드시** 넘겨야 하는 키(플랫폼 축). 누락은 조용히 통과시키지 않는다 —
# 빈 값으로 스폰한 하네스는 원인 불명으로 죽고, 그 실패는 위임 실패로만 보인다.
_REQUIRED_CHILD_ENV_KEYS_POSIX: tuple[str, ...] = ("PATH",)
_REQUIRED_CHILD_ENV_KEYS_WINDOWS: tuple[str, ...] = ("PATH", "SystemRoot", "ComSpec")
# 임시 디렉터리 축은 플랫폼마다 **이름이 다르다**. 이 중 최소 하나가 자식 env 에 있어야 하고,
# 부모 env 에 하나도 없으면 실측 temp 디렉터리(`_gettempdir`)로 선두 키를 채운다.
_TEMP_ENV_KEYS_POSIX: tuple[str, ...] = ("TMPDIR",)
_TEMP_ENV_KEYS_WINDOWS: tuple[str, ...] = ("TEMP", "TMP")
# read 역할 preamble 이 부르는 임시 디렉터리 **변수 표기** — 셸마다 전개 문법이 다르다.
# PowerShell 5.x 에서 `$TMPDIR` 은 env 가 아니라 빈 PowerShell 변수라, 그대로 두면 처방된
# `--basetemp` 가 드라이브 루트로 튄다.
_TEMP_ENV_REFERENCE_POSIX = "$TMPDIR"
_TEMP_ENV_REFERENCE_WINDOWS = "$env:TMPDIR"
# 하네스별 인증/구동 필수 env(하네스-필수 마커만 명시 통과). 실 API key 는 각 하네스
# config/auth 파일(HOME 앵커 격리 홈)로 흐르므로 여기엔 경로/토글 키 위주로 최소 둔다.
# 세 하네스 모두 **HOME 기반 파일 auth**(~/.codex·~/.claude·opencode
# config)로 완주했다 — OPENAI/ANTHROPIC_API_KEY env 는 부재해도 무방(파일 auth 경로). load-bearing 키 =
# base 의 HOME + opencode 의 OPENCODE_CONFIG_DIR(ollama provider config 위치). API-key 항목은 env-auth
# adopter 용 보험이라 유지(존재 시만 통과·과잉 아님). 이 allowlist 로 충분(키 추가/축소 불요).
# 인증/설정 전달 allowlist이며 세션 감지 마커가 아니다 — 감지 선언은 watchdog.HARNESS_SESSION_MARKERS 가 소유한다.
_HARNESS_AUTH_ENV: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_HOME", "OPENAI_API_KEY"),
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"),
    "opencode": ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"),
}


def _conf_default_int(key: str) -> int:
    """이 conf 키의 엔진 기본값(정수) — 선언이 없으면 `TypeError` 로 멈춘다.

    이 모듈은 conf **값**은 additional_reviewer 의 해소를 재사용하고, 키 **선언**(기본값)은
    레지스트리에서 직접 읽는다 — 기본값이 두 군데면 규칙이 둘이 된다."""
    return int(conf_schema.default_for(key))


FRESH_REASON_FIELD = "fresh_reason"
ATTACH_RAW_SECTION_TITLE = "## 직전 검토 보고 원문"
_DELEGATE_RAW_STDOUT_MARKER = "\n## stdout\n"
_DELEGATE_RAW_STDERR_MARKER = "\n\n## stderr\n"
_ADDITIONAL_REVIEWER_STDERR_MARKER = "\n[stderr]\n"


class AttachedRaw(NamedTuple):
    """첨부 대상으로 확정한 마감 레코드와 그 실행의 최종 reply 원문."""

    record_id: str
    reply: str


def select_attached_raw_record(rows: Sequence[dict], selector: str) -> dict:
    """`--attach-raw` 지시자의 마감 레코드 1건을 확정한다.

    티켓 표기는 그 티켓의 started_at 최신 **마감** 행, 그 밖은 id 정확일치다. id가 실재하지만
    미마감이면 '미존재'로 뭉개지 않고 별도 오류를 낸다.
    """
    if selector.startswith(RESUME_TICKET_PREFIX):
        matching = [
            row for row in rows
            if isinstance(row, dict) and row.get(RESUME_FIELD_TICKET) == selector
        ]
        completed = [row for row in matching if row.get("finished_at") is not None]
        if not completed:
            detail = "(해당 티켓 레코드는 모두 미마감)" if matching else "(티켓 기록 미존재)"
            raise delegate_errors.DelegateError(
                f"--attach-raw {selector!r}의 완료 레코드가 없음 {detail}"
            )
        return max(completed, key=lambda row: str(row.get("started_at", "")))

    matching = [
        row for row in rows
        if isinstance(row, dict) and row.get("id") == selector
    ]
    if not matching:
        raise delegate_errors.DelegateError(f"--attach-raw 레코드 미발견: {selector}")
    record = matching[0]
    if record.get("finished_at") is None:
        raise delegate_errors.DelegateError(f"--attach-raw 레코드가 미마감이라 첨부할 수 없음: {selector}")
    return record


def _delegate_raw_stdout(content: str, raw_path: Path) -> str:
    """pm_delegate 감사 raw에서 stdout 원문을 표시 경계의 **마지막** stderr 절로 분리한다."""
    _header, marker, remainder = content.partition(_DELEGATE_RAW_STDOUT_MARKER)
    if not marker:
        raise delegate_errors.DelegateError(f"첨부 raw의 stdout 절이 없음: {raw_path}")
    stdout, marker, _stderr = remainder.rpartition(_DELEGATE_RAW_STDERR_MARKER)
    if not marker:
        raise delegate_errors.DelegateError(f"첨부 raw의 stderr 경계가 없음: {raw_path}")
    return stdout


def _additional_reviewer_raw_answer(content: str) -> str:
    """신구 추가 리뷰 감사 헤더와 stderr 꼬리를 떼고 wire 원문만 반환한다."""
    body = content
    headers = (
        "# additional_reviewer raw 출력 (감사)\n",
        "# external_review raw 출력 (감사)\n",
    )
    if content.startswith(headers):
        lines = content.splitlines(keepends=True)
        index = 0
        while index < len(lines) and lines[index].startswith("#"):
            index += 1
        if index < len(lines) and not lines[index].strip():
            index += 1
        body = "".join(lines[index:])
    answer, marker, _log = body.rpartition(_ADDITIONAL_REVIEWER_STDERR_MARKER)
    return answer if marker else body


# ── 세션 재사용 (resume 라운드) ───────────────────────────────────────────
#
# 재위임/다라운드 재작업이 직전 위임 세션을 이어받으면 그 컨텍스트가 프롬프트 캐시 단가로
# 재적재되고 도구 재읽기가 사라진다(재적재 비용 0 이 아니라 **단가가 내려간다**). 배선 축은
# 위임 채널 하나뿐이다 — 추가 리뷰어 축은 라운드마다 임시 홈·전사 없는 환경으로 도는 오염 통제가
# 목적이라 세션 연속성을 의도적으로 끊으며, 그 정책을 완화하지 않는다.
#
# 저장 축도 하나다: 세션 id·usage 분해·직전 must_fix 항목·기준 rev 는 **raw 장부의 그 실행
# 레코드 행**에 실린다. 리뷰 라운드 장부(수렴 게이트 소유)는 건드리지 않는다 — 두 장부를 조인할
# 키를 만들지 않으면 어긋날 수도 없다. 그래서 재사용 가용 창의 상한은 raw 장부의 완료 레코드
# 보존 창(기간·건수)이며, 창 밖 재위임은 후보 부재로 자연히 fresh 다(결함 아님).
#
# 회계는 **기록만** 한다 — 위임 라운드에는 새 상한도 거부 rc 도 두지 않는다(리뷰 게이트의 라운드
# 상한·must_fix 추이·wave 예산과 서로 오염되지 않게 네임스페이스를 분리한다).

# `--resume-from` 후보 지시자 — 티켓 표기면 그 티켓의 최근 위임, 아니면 레코드 id 정확일치.
RESUME_TICKET_PREFIX = "T-"
# 위임 축 raw 장부 표면 이름 — 재사용 후보는 이 표면 레코드만이다(리뷰 축 레코드는 후보 아님).
DELEGATE_RAW_SURFACE = "delegate"
# opencode wire 사본 좌표 — 같은 행의 ``raw_path``(PM 홈 감사 원본)와 함께 실행을 재구성한다.
OPENCODE_TRANSPORT_PROMPT_FIELD = "transport_prompt_path"
# 후보가 만족해야 하는 마감 rc(성공 마감만 이어받는다).
RESUME_REQUIRED_RC = 0
# raw 장부 행에 싣는 구조화 필드 이름(조회면/테스트가 같은 이름을 본다).
RESUME_FIELD_SESSION_ID = "session_id"
RESUME_FIELD_MUST_FIX = "must_fix_items"
RESUME_FIELD_BASE_REV = "base_rev"
RESUME_FIELD_USAGE = "usage"
RESUME_FIELD_TICKET = "ticket"
RESUME_FIELD_RESUME_FROM = "resume_from_session_id"
RESUME_FIELD_RESUME_MATCHED = "resume_matched"
# cross 실위임 결속 키 — delegate-rounds 장부의 같은 이름 필드와 값이 그대로 같아야 두 장부가
# 조인된다. native 위임(라운드 준비가 없는 호출)은 이 값이 없어 raw 행에도 실리지 않는다 —
# 부재는 "결속 대상 없음"이지 손상이 아니다.
RESUME_FIELD_RUN_ID = "run_id"
RESUME_FIELD_TICKET_COPY = "copy"
# 회신 검증까지 통과한 **유효 성공**인가 — rc(자식 종료 코드)만으로는 못 세는 축이다.
RESUME_FIELD_REPLY_EXTRACTED = "reply_extracted"
# attempt 라벨 — raw 헤더/장부가 이 실행이 어떤 라운드였는지 그대로 말한다.
RESUME_ATTEMPT = "resume"
RESUME_FRESH_FALLBACK_ATTEMPT = "fresh-after-resume-mismatch"


class ResumePlan(NamedTuple):
    """확정된 세션 재사용 1건 — 이어받을 세션 id 와 그 세션에 보낼 delta payload.

    `ticket` 은 이어받은 **레코드가 기록한** 티켓 식별자다(없을 수 있다). 이 라운드의 새 레코드가
    그 값을 계승해야 다음 재개의 티켓 지시자 선택이 유지된다."""

    session_id: str
    record_id: str
    delta_prompt: str
    ticket: str | None = None


def _resume_selector_matches(row: dict, selector: str) -> bool:
    """지시자와 레코드의 대응 — 티켓 표기면 기록된 ticket, 아니면 레코드 id 정확일치."""
    if selector.startswith(RESUME_TICKET_PREFIX):
        return row.get(RESUME_FIELD_TICKET) == selector
    return row.get("id") == selector


def _resume_candidate_is_reusable(row: dict) -> bool:
    """그 레코드에서 **이어받을 세션**을 얻을 수 있는가 (후보 정밀화 축).

    세 가지를 본다:
      · 세션 id 가 실제로 남았는가 — 이 필드는 회신 wire 관측으로만 채워진다. rc 0 이어도 회신을
        못 읽은 라운드는 이어받을 세션을 모른다.
      · `resume_matched` 가 False 가 아닌가 — False 면 그 라운드는 **다른 세션**이 답한 실행이고,
        기록된 세션 id 는 그 남의 세션 것이다. 후보로 두면 다음 재개가 맥락 없는 세션을 이어받아
        fresh 재실행 비용을 다시 지불한다.
      · `reply_extracted` 가 False 가 아닌가 — 장부의 `rc` 는 **자식 프로세스 종료 코드**라 회신
        검증보다 먼저 확정된다. 세션 id 만 관측되고 유효 회신이 없던 실행(wire 절단·형상 붕괴)도
        rc 0 으로 남으므로, 그 축을 안 보면 *실패한 라운드*가 성공 후보로 재개된다. 필드가 아예
        없는 **구레코드는 종전대로 rc 기준**이다(하위호환 — 이 축이 없던 시절 기록을 일괄 탈락
        시키면 정상 재개가 회귀한다).
    이 셋을 선택 *전에* 거르는 이유는 결정 규칙이 "최신 1건"이기 때문이다 — 뒤에서 걸러 봐야
    최신이 탈락하면 유효한 이전 후보가 있어도 재사용이 통째로 불가로 접힌다.
    """
    session_id = row.get(RESUME_FIELD_SESSION_ID)
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    if row.get(RESUME_FIELD_REPLY_EXTRACTED) is False:
        return False
    return row.get(RESUME_FIELD_RESUME_MATCHED) is not False


def _resume_axis_matches(row: dict, *, selector: str, role: str, harness: str) -> bool:
    """지시자·role·하네스·성공 마감이 맞는가 (재사용 가능성은 별개 축)."""
    return (
        isinstance(row, dict)
        and row.get("surface") == DELEGATE_RAW_SURFACE
        and row.get("role") == role
        and row.get("harness") == harness
        and row.get("rc") == RESUME_REQUIRED_RC
        and _resume_selector_matches(row, selector)
    )


def resume_unusable_reason(
    rows: Sequence[dict], *, selector: str, role: str, harness: str,
) -> str | None:
    """지시자에 맞는 레코드는 **있는데** 이어받을 수 없는 사유 (해당 없으면 None).

    후보 0 의 사유가 두 가지인데 안내가 하나면 진단이 사람을 엉뚱한 데로 보낸다: 정말 없는 것
    (보존 창 밖·아직 없는 라운드)과, 있는데 그 레코드로는 이어받을 수 없는 것(세션 id 미관측·
    세션 불일치 라운드)은 처방이 다르다. 특히 레코드 id 를 **직접 지목**한 실행에 "보존 창 밖"이라
    답하면 방금 눈으로 본 레코드를 없다고 하는 셈이다."""
    unusable = [
        row for row in rows
        if _resume_axis_matches(row, selector=selector, role=role, harness=harness)
        and not _resume_candidate_is_reusable(row)
    ]
    if not unusable:
        return None
    row = max(unusable, key=lambda item: str(item.get("started_at", "")))
    if row.get(RESUME_FIELD_RESUME_MATCHED) is False:
        return (f"레코드 {row.get('id')} 는 세션 불일치로 끝난 라운드다(resume_matched=false) — "
                "기록된 세션 id 는 남의 세션 것이라 이어받을 수 없음")
    if row.get(RESUME_FIELD_REPLY_EXTRACTED) is False:
        return (f"레코드 {row.get('id')} 는 유효 회신이 없던 라운드다(reply_extracted=false) — "
                "rc 0 은 자식 종료 코드일 뿐이라 성공 후보가 아님")
    return (f"레코드 {row.get('id')} 에 세션 id 가 관측되지 않았다(회신 wire 미기록) — "
            "이어받을 세션을 알 수 없음")


def select_resume_record(
    rows: Sequence[dict], *, selector: str, role: str, harness: str,
) -> dict | None:
    """재사용 후보 1건 — 같은 지시자·role·하네스의 **성공 마감 레코드 중 started_at 최신**.

    결정적 1줄 규칙이다(후보가 여럿이어도 고르는 값이 하나로 정해진다). 하네스가 선택 키인
    이유는 세션 id 가 발급 축 안에서만 뜻이 있기 때문이다 — 다른 축의 id 도 형식 가드를 통과할
    수 있어(codex thread id 가 uuid 표기다) 형식만으로는 남의 세션을 거르지 못하고, 그대로
    보내면 확정 실패 뒤 fresh 재실행으로 한 라운드를 더 지불한다.

    후보 조건에 "이어받을 세션이 실제로 있는가"(`_resume_candidate_is_reusable`)를 함께 건다 —
    회신을 못 읽은 라운드와 세션 불일치로 끝난 라운드는 rc 0 이어도 재개의 입력이 아니다.
    """
    matches = [
        row for row in rows
        if _resume_axis_matches(row, selector=selector, role=role, harness=harness)
        and _resume_candidate_is_reusable(row)
    ]
    if not matches:
        return None
    return max(matches, key=lambda row: str(row.get("started_at", "")))


def build_resume_delta_payload(
    *, must_fix_items: Sequence[str], base_rev: str | None, task_text: str,
) -> str:
    """이어받는 turn 에 보낼 delta payload.

    입력은 **장부의 구조화 필드**(직전 라운드 must_fix 항목·기준 rev)와 호출자가 넘긴 원문뿐이다.
    raw 박제 파일을 다시 읽거나 자유 서술을 파싱하지 않으며, 해소 주장은 호출자의 불투명 문자열로
    그대로 실어 보낸다(엔진이 그 내용을 해석하지 않는다). 역할 preamble 은 붙이지 않는다 — 이어받는
    세션엔 이미 있다(그래서 재사용이 실패하면 full payload 로 돌아가야 한다).
    """
    lines = [
        "## 이어지는 라운드 (직전 위임 세션 재사용)",
        "이 세션은 직전 위임 라운드를 이어받는다 — 역할·금지·이미 읽은 파일은 그대로 유효하다.",
        "아래 '직전 라운드 기록'은 장부에 남은 구조화 값이고, 그 뒤 본문은 호출자가 보낸 원문이다.",
        "",
        "### 직전 라운드 기록",
        f"- 기준 rev: {base_rev}" if base_rev else "- 기준 rev: 장부에 기록 없음",
    ]
    items = [str(item) for item in must_fix_items if str(item).strip()]
    if items:
        lines.append("- must_fix 항목:")
        lines.extend(
            f"  {index}. {item}" for index, item in enumerate(items, start=1)
        )
    else:
        lines.append("- must_fix 항목: 장부에 기록 없음")
    lines += ["", "### 이번 라운드 입력 (호출자 원문)", "", task_text]
    return "\n".join(lines)



def _parse_ledger_timestamp(value: object) -> datetime.datetime | None:
    """장부 시각 문자열 → datetime(판독 불가는 None · 판정에서 그 조건만 뺀다)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
