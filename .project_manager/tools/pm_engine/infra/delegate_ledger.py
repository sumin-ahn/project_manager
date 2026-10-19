"""위임 라운드 장부 읽기와 run-dir 자리 (층: infra).

`delegate-rounds.jsonl` 행 형식 검증과 읽기, copy 별 최신 행 조회, 묶음·티켓 run-dir 상대 경로를 소유한다.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath

from pm_engine.domain import delegate_errors, review_form, ticket_rules
from pm_engine.infra import fs


# PM 개발 프로세스에 참여하는 모든 역할은 자기 산출을 라운드 파일로 남긴다 — 라운드 파일명이
# 허용하는 역할 집합이다(`conf_schema.ROLE_CHOICES` ⊆ 이 집합 · 불변식은 테스트가 고정).
TICKET_COPY_ROLES: frozenset[str] = frozenset(
    {"developer", "code-reviewer", "architect", "researcher", review_form.ADDITIONAL_REVIEWER_ROLE}
)
TICKET_COPY_REL_ROOT = Path(".project_manager") / ".local" / "delegate-ticket-copies"

# 신뢰 뿌리 = PM 홈 장부(슬롯 밖). 슬롯 안에는 서명·metadata·baseline 을 두지 않는다 — 회수는
# 이 장부에 준비 기록이 있는 경로만 받는다.
DELEGATE_ROUNDS_LEDGER_REL_PATH = (
    Path(".project_manager") / ".local" / "delegate-rounds.jsonl"
)
_DELEGATE_ROUNDS_LEDGER_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"ticket", "role", "ordinal", "run_id", "copy", "board_rel", "prepared_at",
     "harvested_at"}
)

# 선택 키 셋은 하위 호환이다(기존 8키 행에는 없다) — `abandoned_at` 은 kill 잔여를 명시적으로
# 포기(abandon)한 행에, `owner_pid` 는 준비 프로세스가 그 run 의 소유자일 때(prepare·실행·회수가
# 한 프로세스인 cross 위임)에만, `superseded_by_ordinal` 은 산출이 시드와 달라도 재실행 대체본을
# 운영자가 명시해 포기를 허용받은 행에만, `discard_reason` 은 재실행 대체가 아니라 **폐기**를
# 밝혀 포기한 행에만 붙는다(그 사유가 처분의 유일한 기록이다). `cluster` 는 run-dir 이 묶음
# 키로 갈린 세대의 행에 붙는다 — **없으면 종전 티켓 키 경로로 조립한다**(엔진 교체 시점에 열려 있던 준비가
# 회수 불가가 되지 않게). 검증은 필수-집합 정확 일치가 아니라 상한-집합 포함으로 완화한다:
# 정확-일치였다면 이 키를 추가하는 순간 기존 행 전부가 schema 불일치로 건너뛰어진다.
_DELEGATE_ROUNDS_LEDGER_OPTIONAL_FIELDS: frozenset[str] = frozenset(
    {"abandoned_at", "owner_pid", "superseded_by_ordinal", "discard_reason", "cluster",
     "base_rev"}
)

# 묶음 id 는 run-dir 의 **경로 성분**이 된다 — board 이름 문법과 같은 보수적 집합만 받는다
# (경로 구분자·상위 참조 배제). 회수측이 같은 형식을 다시 확인해 장부 값 하나로 경로가
# 넓어지지 않게 한다.
_CLUSTER_ID_RE = re.compile(r"C-[A-Za-z0-9][A-Za-z0-9._-]*")
_DELEGATE_ROUNDS_LEDGER_FIELDS: frozenset[str] = (
    _DELEGATE_ROUNDS_LEDGER_REQUIRED_FIELDS | _DELEGATE_ROUNDS_LEDGER_OPTIONAL_FIELDS
)


def _delegate_rounds_ledger_path(pm_home: Path) -> Path:
    return Path(pm_home).resolve() / DELEGATE_ROUNDS_LEDGER_REL_PATH


def _delegate_rounds_ledger_row(row: object, *, line_number: int) -> dict:
    """장부 1행의 schema·값 형식을 검증한다 (손상은 그 행만 거부한다).

    schema 판정은 필수 8키 **정확 일치**가 아니라 **상한-집합 포함**이다 — 필수 키 전부가 있고,
    그 위에 선택 키(`abandoned_at`·`owner_pid`) 가 더해질 수 있다. 정확-일치였다면 선택 키 도입
    자체가 그 키 없는 기존 행 전부를 schema 불일치로 만든다.
    """
    if (
        not isinstance(row, dict)
        or not _DELEGATE_ROUNDS_LEDGER_REQUIRED_FIELDS <= set(row) <= _DELEGATE_ROUNDS_LEDGER_FIELDS
    ):
        raise delegate_errors.DelegateError(f"delegate-rounds 장부 schema 불일치: line={line_number}")
    if (
        not isinstance(row["ticket"], str)
        or not ticket_rules._is_valid_ticket_id(row["ticket"])
        or row["role"] not in TICKET_COPY_ROLES
        or not isinstance(row["ordinal"], int)
        or isinstance(row["ordinal"], bool)
        or row["ordinal"] < 1
        or not isinstance(row["run_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", row["run_id"]) is None
        or not isinstance(row["copy"], str)
        or not Path(row["copy"]).is_absolute()
        or not isinstance(row["board_rel"], str)
        or not row["board_rel"]
        or Path(row["board_rel"]).is_absolute()
        or ".." in PurePosixPath(row["board_rel"]).parts
        or not isinstance(row["prepared_at"], str)
        or not row["prepared_at"]
        or not (
            row["harvested_at"] is None
            or isinstance(row["harvested_at"], str) and row["harvested_at"]
        )
        or ("abandoned_at" in row and not (
            isinstance(row["abandoned_at"], str) and row["abandoned_at"]
        ))
        # `owner_pid` 는 생존 조회에 그대로 들어가는 값이다 — bool/0 이하를 통과시키면 조회 seam
        # 이 그 값을 "부재"로 정규화해 증거 있는 행이 조용히 증거 없는 행으로 퇴화한다.
        or ("owner_pid" in row and not (
            isinstance(row["owner_pid"], int)
            and not isinstance(row["owner_pid"], bool)
            and row["owner_pid"] > 0
        ))
        # `superseded_by_ordinal` 도 같은 이유(생존/재확인 조회에 그대로 들어가는 값)로 owner_pid
        # 와 같은 형식을 요구한다 — 자기 자신 참조 배제는 쓰기 시점(`abandon_ticket_copy`)의 몫이다.
        or ("superseded_by_ordinal" in row and not (
            isinstance(row["superseded_by_ordinal"], int)
            and not isinstance(row["superseded_by_ordinal"], bool)
            and row["superseded_by_ordinal"] > 0
        ))
        # `discard_reason` 은 그 처분의 **유일한 기록**이라 값 형식을 행 판독 자리에서 닫는다 —
        # 빈 값·여러 줄을 통과시키면 사람이 읽을 사유가 없는 행이 "밝힌 처분" 으로 남는다.
        or ("discard_reason" in row and not _is_valid_discard_reason(row["discard_reason"]))
        # 묶음 키는 경로 성분이 되므로 값 형식을 행 판독 자리에서 닫는다.
        or ("cluster" in row and not (
            isinstance(row["cluster"], str) and _is_valid_cluster_id(row["cluster"])
        ))
    ):
        raise delegate_errors.DelegateError(f"delegate-rounds 장부 값 형식 불일치: line={line_number}")
    return dict(row)


def _delegate_rounds_ledger_records(pm_home: Path) -> list[dict]:
    """append-only 장부를 기록 순서대로 읽는다 (부재는 빈 목록 · 손상 행은 경고 후 건너뜀).

    손상 행 하나가 다른 run 의 회수를 막지 않는다 — 조회는 `copy` 경로 일치로 하고, 그 경로
    행이 손상이면 아래 `_delegate_round_record` 가 "준비 기록 없음" 으로 거부한다(fail-closed).
    """
    path = _delegate_rounds_ledger_path(pm_home)
    try:
        raw = fs.read_bytes_shared(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise delegate_errors.DelegateError(f"delegate-rounds 장부 읽기 실패: {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise delegate_errors.DelegateError(f"delegate-rounds 장부 비-UTF8: {path}: {exc}") from exc
    rows: list[dict] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(
                _delegate_rounds_ledger_row(json.loads(line), line_number=line_number)
            )
        except (ValueError, delegate_errors.DelegateError) as exc:
            print(
                f"경고: delegate-rounds 장부 손상 행 건너뜀: {path}: line={line_number} · {exc}",
                file=sys.stderr,
            )
    return rows


def ticket_copy_records(
    pm_home: Path, *, ticket: str | None = None, unharvested: bool = False,
) -> list[dict]:
    """copy 별 최신 append snapshot 을 prepare 최신순으로 반환한다 (`ticket copies` 입력).

    `unharvested` 는 "아직 처분되지 않은 준비"다 — 회수(`harvested_at`)도 포기(`abandoned_at`)도
    거치지 않은 행만 남긴다. 포기된 행을 미회수로 계속 세면 abandon 이 잔여를 지워도 조회면에는
    그대로 남아 정리 수단이 무효로 보인다.
    """
    latest: dict[str, dict] = {}
    for row in _delegate_rounds_ledger_records(pm_home):
        latest[row["copy"]] = row
    rows = [
        row for row in latest.values()
        if (ticket is None or row["ticket"] == ticket)
        and (
            not unharvested
            or (row["harvested_at"] is None and "abandoned_at" not in row)
        )
    ]
    return sorted(rows, key=lambda row: row["prepared_at"], reverse=True)


def _is_valid_cluster_id(value: object) -> bool:
    """묶음 id 가 경로 성분으로 안전한 형식인가(회수·예약 양쪽이 쓰는 단일 술어)."""
    return bool(_CLUSTER_ID_RE.fullmatch(str(value or "")))


def _is_valid_discard_reason(value: object) -> bool:
    """폐기 사유가 조회면에 그대로 실릴 한 줄인가 (쓰기·행 판독이 쓰는 단일 술어)."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\n" not in value
        and "\r" not in value
    )


def _cluster_run_relative_dir(cluster: str, run_id: str) -> Path:
    """run-dir 의 cwd 상대 경로 — `<root>/<묶음>/<run_id>/`. run 이 유일 단위라 `<role>`
    세그먼트가 없다. 같은 슬롯·같은 묶음·같은 역할을 동시에 위임해도 run_id 가 갈라 놓는다.
    """
    return TICKET_COPY_REL_ROOT / cluster / run_id


def _ticket_run_relative_dir(cluster: str, run_id: str, ticket: str) -> Path:
    """run-dir 안 **그 티켓의 자리** — 라운드 파일·명세·이전 라운드가 여기 산다.

    묶음 하나의 준비가 run-dir 하나를 쓰고 그 안에서 티켓별로 갈린다(크기 1 이면 자리 하나).
    """
    return _cluster_run_relative_dir(cluster, run_id) / ticket


def _legacy_ticket_run_relative_dir(ticket: str, run_id: str) -> Path:
    """묶음 키가 없던 세대의 자리 — `<root>/<티켓>/<run_id>/`.

    장부 행에 `cluster` 가 없으면 이 규약으로 조립한다. 엔진 교체 시점에 **열려 있던** 준비가
    회수 불가가 되면 board 라운드만 남으므로, 옛 행의 좌표를 그대로 살려 둔다.
    """
    return TICKET_COPY_REL_ROOT / ticket / run_id


def _row_ticket_relative_dir(row: dict) -> Path:
    """장부 행이 인가하는 티켓 자리 — 새 세대는 묶음 키로, 옛 행은 종전 규약으로 조립."""
    cluster = str(row.get("cluster") or "").strip()
    if cluster:
        return _ticket_run_relative_dir(cluster, row["run_id"], row["ticket"])
    return _legacy_ticket_run_relative_dir(row["ticket"], row["run_id"])
