"""위임 라운드 값과 기록 문법 (층: domain)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple
from pathlib import Path
from collections.abc import Sequence
from pm_engine.domain import delegate_errors
from pm_engine.domain import markdown_text
import re
from pm_engine.domain import review_form



class TerminalFixHarvestError(delegate_errors.DelegateError):
    """마지막 fix 회수 거부 — 증거는 보존하되 복구 라운드는 없는 종단 상태."""
# 사본 루트를 숨기는 ignore 규칙의 정본 위치 — 이 파일 유래가 아니면 로컬 전용
# 소스(`.git/info/exclude`·전역 excludesFile 등)로 보고 fail-loud 한다.
TICKET_COPY_IGNORE_TRACKED_SOURCE = Path(".project_manager") / ".gitignore"
# 두-git 풀 슬롯(앱 저장소에 `.project_manager` 가 없는 형상)의 정본은 그 풀 bare 의
# `info/exclude` 에 있는 이 줄이다. bare 의 exclude 는 `$GIT_COMMON_DIR/info/exclude` 라 그 풀의
# 모든 linked worktree 가 공유하므로, 위 tracked 정본이 지키려는 성질(사본 루트를 숨기는 규칙이
# 이 코드 트리의 모든 사본에 있다)이 이 형상에서도 그대로 성립한다. 규칙을 심는 쪽은
# `pm_config._ensure_bare_ticket_copy_exclude` 이고 두 값은 같은 문자열이어야 한다
# (불변식은 테스트가 고정). 루트 앵커 디렉터리 규칙이라 저장소가 추적하는
# `.project_manager/tools`·`wiki` 의 새 파일은 숨기지 않는다.
TICKET_COPY_POOL_IGNORE_RULE = "/.project_manager/.local/"


# 준비가 슬롯 run-dir 에 까는 읽기 전용 입력. 쓸 수 있는 파일은 라운드 파일 하나뿐이라
# (`NN-<role>.md`) 회수는 그 하나만 본다 — "절 밖 bytes 대조" 가 필요한 자리가 없다.
TICKET_COPY_SPEC_NAME = "spec.md"
TICKET_COPY_ROUNDS_DIRNAME = "rounds"
# 증거 없는 예약을 지우는 유일한 통로 — 무엇을 강제하는지가 아니라 **운영자가 무엇을 확인했는지**
# 를 말하는 이름이다. 문구와 CLI 가 같은 값을 쓰도록 여기 한 자리에 둔다.
ABANDON_ASSUME_DEAD_FLAG = "--assume-dead"
# 재실행으로 대체된 라운드의 "시드 그대로" 거부를 여는 유일한 통로 — 값은 대체본 라운드의
# ordinal 이다(자기 자신 참조는 거부). `--assume-dead` 와 같은 문구/CLI 단일 출처 원칙.
ABANDON_SUPERSEDED_BY_FLAG = "--superseded-by"
# 재실행 대체가 아닌 **폐기**로 처분하는 유일한 통로 — 값은 사람이 읽는 한 줄 사유다. 처분은
# 회수 계약과 독립이지만 선언 없는 처분은 조용한 파괴라, 이 값이 그 라운드를 왜 지웠는지에 대한
# 유일한 기록이 된다(`--superseded-by` 와 배타 · 같은 문구/CLI 단일 출처 원칙).
ABANDON_DISCARD_REASON_FLAG = "--discard-reason"
# 준비가 장부 행에 `base_rev`(슬롯 HEAD)를 남기는 역할.
_BASE_REV_ROLES: frozenset[str] = frozenset({"developer", review_form.INTERNAL_REVIEW_ROLE})


class TicketCopyPlan(NamedTuple):
    """준비 1회의 좌표 — 슬롯 라운드 파일 하나와 그 run-dir, board 예약 결과.

    `run_id` 는 delegate-rounds 장부의 같은 필드와 값이 같다 — cross 실위임 raw 행이 이 값을
    `extra` 로 실으면 두 장부가 문자열 일치로 결속된다."""

    path: Path
    run_dir: Path
    ticket: str
    role: str
    ordinal: int
    board_path: Path
    run_id: str
    # 이 라운드가 쓸 첫 finding ID 실값(리뷰 채널이 아니면 빈 값). 준비가 시드에 실은 것과 같은
    # 값이라 사본 프리앰블이 다시 계산하지 않는다 — 프롬프트와 시드가 다른 번호를 말하지 않는다.
    # 기본값이 없다: 값을 넘기지 않은 조립은 옛 프롬프트로 접히지 않고 TypeError 로 터진다.
    next_finding_id: str
    # 이 준비가 속한 묶음 — 크기 1 도 묶음이라 항상 실값이다(경로·장부 행이 이 값으로 갈린다).
    cluster: str = ""
    # 이 라운드가 속한 **묶음 run-dir**(`ClusterCopyPlan.run_dir`) — 위 `run_dir` 은 이 티켓
    # 자리이고, 이 값은 그 자리들을 담는 run 전체다. 쓰기 허용 범위가 run-dir 전체라 하네스가
    # 여는 좌표는 이 값이다(자리 경로에서 역산하지 않는다 — 역산은 layout 이 바뀌면 조용히
    # 어긋난다).
    cluster_run_dir: Path | None = None


class ClusterCopyPlan(NamedTuple):
    """준비 1회의 묶음 좌표 — run-dir 하나와 그 안의 티켓별 라운드 좌표들.

    `rounds` 는 선언 순서(멤버 순서)를 지킨다 — 크기 1 이면 원소 하나이고 그 원소가 종전
    `prepare_ticket_copy` 의 반환값이다.
    """

    run_dir: Path
    cluster: str
    run_id: str
    role: str
    rounds: tuple[TicketCopyPlan, ...]


class TicketHarvestResult(NamedTuple):
    changed: bool
    sync_ready: bool
    # 회수 machine line 의 안정된 key 자리 — verify 판정은 회수 게이트
    # (`_developer_round_harvest_problem`) 한 층이 소유하므로 회수까지 온 산출에는 실을 값이
    # 없다(항상 빈 tuple). 태만한 accepted 행의 이름은 `review verify-template` 이 낸다.
    verify_missing: tuple[str, ...] = ()


class TicketAbandonResult(NamedTuple):
    """포기 처분의 **수렴 보고** — 상태 선언이 아니라 세 자산 재판독의 결과다.

    `changed` 는 이번 호출이 실제로 무언가를 바꿨는가이고, 나머지 셋은 호출이 끝난 뒤 다시 읽은
    값이다. `board_removed` 는 중간 순번 보존일 때 False 다(보존이 정상 종결이다).
    """

    changed: bool
    sync_ready: bool
    board_removed: bool = False
    run_dir_removed: bool = False
    converged: bool = False


_TICKET_PREPARE_STATUSES: tuple[str, ...] = ("open", "claimed")


# ── 묶음 고정 예산 ────────────────────────────────────────────────────────
# 묶음 하나가 도는 라운드는 정해져 있다: 설계(architect) → 구현(developer · 티켓마다 1) →
# 리뷰(code-reviewer) → fix(developer). 장부 `budget` 의 4키가 그 수열의 **길이**를 값으로
# 말하고, 아래 표가 키↔역할 대응과 순서를 소유한다. 초과나 순서 밖 요청은 티켓을 정지시키며
# 예산 리셋·추가 설계·추가 fix로 다시 열 수 없다.
CLUSTER_BUDGET_ROLE_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("architect", "architect"),
    ("developer", "developer_per_ticket"),
    (review_form.INTERNAL_REVIEW_ROLE, "code-reviewer"),
    ("developer", "fix"),
)
_CLUSTER_BUDGET_OVER = (
    "묶음 고정 라운드 종료 — {cluster} · {ticket} role={role} · 라운드 "
    "{count}건(예산 {limit}건)\n"
    "  고정 수열: {sequence}\n"
    "  · fix가 마지막 사람 라운드입니다. 티켓을 완료하지 못하면 추가 라운드 없이 정지·보고합니다."
)
_CLUSTER_BUDGET_ORDER = (
    "묶음 라운드 순서 밖 역할 — {cluster} · {ticket} 다음 라운드는 {expected} 인데 "
    "{role} 을 요청했습니다\n"
    "  고정 수열: {sequence}\n"
    "  · 순번이 곧 단계입니다 — 단계를 건너뛰거나 되돌리는 경로는 없습니다."
)


_CLUSTER_LEDGER_ABSENT = (
    "묶음 장부가 없습니다: {cluster} — 고정 라운드 수열은 장부가 소유하고, 준비는 "
    "그 선언만 읽습니다(선언 없는 묶음은 판정 입력이 아니라 정지 사유입니다).\n"
    "  · `python3 .project_manager/tools/board.py cluster show {cluster}` 로 장부를 확인하고, "
    "없으면 `board.py cluster new <ticket-group> --tickets <T-...> --task <task>`"
    "(task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가) 으로 선언하세요 — 통합 "
    "브랜치를 둘 코드 트리는 명시가 필수입니다."
)
_CLUSTER_BUDGET_UNDECLARED = (
    "묶음 라운드 예산이 선언되지 않았습니다: {cluster} · 장부 `budget` 의 {key} 가 없거나 "
    "정수가 아닙니다\n"
    "  · 예산은 장부를 만들 때 박습니다(`cluster new` · 발행이 만드는 크기 1 장부도 같은 "
    "기본값) — 없는 값을 기본값으로 지어내지 않습니다.\n"
    "  · `python3 .project_manager/tools/board.py cluster show {cluster}` 로 확인하고 장부의 "
    "`budget` 4키를 채운 뒤 다시 실행하세요."
)


def cluster_round_sequence(budget: object, *, cluster: str) -> tuple[str, ...]:
    """예산 4키를 라운드 역할 **수열**로 편다 — 장부 값이 곧 순서다.

    네 값은 모두 정확히 1이어야 한다. 묶음마다 예산을 늘이거나 단계를 생략하면 고정 수열이
    다시 가변 루프가 되므로 장부 손상으로 거부한다.
    """
    if not isinstance(budget, Mapping):
        raise delegate_errors.DelegateError(_CLUSTER_BUDGET_UNDECLARED.format(
            cluster=cluster, key="(없음)"))
    sequence: list[str] = []
    for role, key in CLUSTER_BUDGET_ROLE_SEQUENCE:
        value = budget.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise delegate_errors.DelegateError(_CLUSTER_BUDGET_UNDECLARED.format(
                cluster=cluster, key=key))
        if value != 1:
            raise delegate_errors.DelegateError(
                f"묶음 고정 라운드 예산은 {key}=1이어야 합니다: {cluster} · got={value}"
            )
        sequence.append(role)
    return tuple(sequence)


def _cluster_cycle_roles(
    existing: Sequence, roles: frozenset[str],
) -> tuple[str, ...]:
    """예약된 라운드 중 고정 수열 역할만 — 순번 오름차순.

    산출 유무는 보지 않는다(예약 자체가 한 단계 소비다 · per-ticket 상한과 같은 규칙).
    수열에 없는 역할(추가 리뷰어 채널 등)은 단계가 아니라 주기 자리를 먹지 않는다.
    """
    return tuple(
        item.role for item in sorted(existing, key=lambda entry: entry.ordinal)
        if item.role in roles
    )


def _cluster_budget_refusal(
    *, cluster: str, ticket: str, role: str, existing: Sequence,
    sequence: Sequence[str],
) -> str | None:
    """이 티켓에 그 역할 라운드를 더 예약할 수 있는가 — 거부 사유 문자열 또는 None."""
    cycle = _cluster_cycle_roles(existing, frozenset(sequence))
    rendered = " → ".join(cycle) if cycle else "(없음)"
    if len(cycle) >= len(sequence):
        return _CLUSTER_BUDGET_OVER.format(
            cluster=cluster, ticket=ticket, role=role, count=len(cycle),
            limit=len(sequence), sequence=rendered,
        )
    expected = sequence[len(cycle)]
    if role != expected:
        return _CLUSTER_BUDGET_ORDER.format(
            cluster=cluster, ticket=ticket, role=role, expected=expected,
            sequence=rendered,
        )
    return None


class DeveloperRegressionRecord(NamedTuple):
    command: str
    result: str


_DEVELOPER_REGRESSION_HEADING = "## 회귀"
_DEVELOPER_REGRESSION_COMMAND_PREFIX = "- 커맨드: `"
_DEVELOPER_REGRESSION_RESULT_PREFIX = "- 결과: "
_DEVELOPER_REGRESSION_GREEN_RE = re.compile(r"(?:^|[ ·])rc\s*=\s*0(?:$|[ ·])")


def parse_developer_regression_record(text: str) -> DeveloperRegressionRecord:
    """developer가 직접 실행해 라운드에 남긴 stage-exit 전체 회귀 기록."""
    lines = markdown_text._normalized_newlines(text).splitlines()
    headings = [index for index, line in enumerate(lines) if line == _DEVELOPER_REGRESSION_HEADING]
    if len(headings) != 1:
        raise delegate_errors.DelegateError("developer 전체 회귀 기록은 `## 회귀` 절 정확히 1개여야 합니다")
    start = headings[0]
    section_end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    section = [line for line in lines[start + 1:section_end] if line.strip()]
    if len(section) != 2:
        raise delegate_errors.DelegateError(
            "developer 전체 회귀 기록은 커맨드·결과 두 행이어야 합니다"
        )
    command_line, result_line = section
    if not (
        command_line.startswith(_DEVELOPER_REGRESSION_COMMAND_PREFIX)
        and command_line.endswith("`")
    ):
        raise delegate_errors.DelegateError("developer 전체 회귀 커맨드 기록 형식이 올바르지 않습니다")
    if not result_line.startswith(_DEVELOPER_REGRESSION_RESULT_PREFIX):
        raise delegate_errors.DelegateError("developer 전체 회귀 결과 기록 형식이 올바르지 않습니다")
    command = command_line[len(_DEVELOPER_REGRESSION_COMMAND_PREFIX):-1].strip()
    result = result_line[len(_DEVELOPER_REGRESSION_RESULT_PREFIX):].strip()
    for value, field in ((command, "커맨드"), (result, "결과")):
        if not value or review_form._CONTRACT_PLACEHOLDER_RE.search(value):
            raise delegate_errors.DelegateError(
                f"developer 전체 회귀 {field}를 placeholder가 아닌 실값으로 기록해야 합니다"
            )
    if not _DEVELOPER_REGRESSION_GREEN_RE.search(result):
        raise delegate_errors.DelegateError(
            "developer 전체 회귀 결과가 green이 아닙니다 — `rc=0` 관측을 기록해야 합니다"
        )
    return DeveloperRegressionRecord(command, result)
