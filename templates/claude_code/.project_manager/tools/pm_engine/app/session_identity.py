"""Home 데이터 기반 세션 정체성 해소."""
from __future__ import annotations

from pathlib import Path
from pm_engine.infra import areas_store
from pm_engine.infra import fs
import json
from pm_engine.infra import pm_paths
from pm_engine.domain import session_identity
from pm_engine.infra import session_store
from pm_engine.domain import task_rules


def validate_task_name_engine(home, task: str) -> None:
    """task 이름과 등록 repo 예약패턴의 공용 검증.

CLI와 직접 호출 모두 데이터 소비 전에 task_rules 검증을 수행한다.
등록부 판독 실패 시 예약패턴 검사만 완화하며 경로·문자 검증은 유지한다."""

    try:
        registered = areas_store.registered_repos(home)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 None(예약패턴만 완화·traversal 유지).
        registered = None
    task_rules._validate_task_name(task, registered)


def _handoff_identity_source_contract() -> dict[str, frozenset[str]]:
    """domain.session_identity의 해소 층을 handoff 실행/미해소 역할로 완전 분할한다.

    생산자에 새 층이 추가됐는데 어느 역할에도 속하지 않거나 두 역할이 겹치면 즉시 중단한다.
    handoff는 사람 표시용 source 문자열을 복제하지 않고 이 계약에서만 승격 여부를 파생한다.
    """
    task_mode = frozenset(session_identity.HANDOFF_TASK_MODE_IDENTITY_SOURCES)
    unresolved = frozenset({session_identity.IDENTITY_SOURCE_UNRESOLVED})
    known = frozenset(session_identity.SNAPSHOT_IDENTITY_SOURCES)
    roles = {"task_mode": task_mode, "unresolved": unresolved}
    if task_mode & unresolved or (task_mode | unresolved) != known:
        raise RuntimeError(
            "pm_log/pm_handoff 정체성 해소 층 계약 불일치 — 생산자 source 집합과 "
            "handoff 역할 분류를 함께 갱신하라."
        )
    return roles


def _is_within(path: Path, parent: Path) -> bool:
    """``path``가 ``parent`` 자신/하위인지 lexical+resolve 기준으로 판정한다."""
    try:
        Path(path).resolve(strict=False).relative_to(Path(parent).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def resolve_snapshot_identity(home, cwd: Path) -> tuple[str | None, str]:
    """cwd lease → 단일 활성 task 순으로 pm_handoff·ticket_finish 무인자 진입의 정체성을 해소한다.

    압축 경로(checkpoint·snapshot)는 이 함수를 부르지 않는다 — 그 경로는 `resolve_session_task`
    하나로 판정한다.

    두 층이 전부다 — "장부가 아무 말도 없으면 이 홈은 단일 정체성"이라는 층은 없다(그 추론은
    실제로 슬롯을 여럿 쓰는 홈에서 오귀속이었다). 세션은 자기 작업 폴더에서 돌고 그 폴더가 곧
    lease 행이라 첫 층이 답을 준다. 작업 폴더가 없는 홈 루트 세션은 이름 붙일 세션이 없으므로
    미해소가 정답이고, 그 사실을 경고로 말한다(조용한 생략 아님).

    per-clone ``local.conf``의 ``session=``은 층이 아니다 — slot 종속 값이 프로젝트 공용
    conf 에 있던 범위 오류라 엔진 어디서도 읽지 않는다."""
    matches: list[tuple[int, str]] = []
    lease_rows = session_store._lease_rows(home)
    for row in lease_rows:
        if row.get("state") != "leased":
            continue
        session = row.get("session")
        slot = session_store._lease_slot_path(home, row)
        if not isinstance(session, str) or not session.strip() or slot is None:
            continue
        if _is_within(cwd, slot):
            matches.append((len(slot.resolve(strict=False).parts), session.strip()))
    if matches:
        deepest = max(depth for depth, _session in matches)
        sessions = sorted({session for depth, session in matches if depth == deepest})
        if len(sessions) == 1:
            return sessions[0], session_identity.IDENTITY_SOURCE_CWD_LEASE

    active = session_store._active_tasks(home)
    if len(active) == 1:
        return active[0], session_identity.IDENTITY_SOURCE_SINGLE_ACTIVE_TASK
    return None, session_identity.IDENTITY_SOURCE_UNRESOLVED


def resolve_session_task(home, session_id: str | None) -> tuple[str | None, str | None]:
    """압축 경로(checkpoint·snapshot)의 정체성 — 하네스 세션 ID 로 그 세션이 연 task 를 찾는다.

    입력은 세션 ID 하나다. cwd·열린 task 수·위임 환경 변수는 보지 않는다. task 기록
    (`worktree-leases.json` 최상위 `tasks[]`)의 `session` 은 부트스트랩이 남긴 하네스 세션 ID
    원값이고, 그 값이 같은 항목이 정확히 1개일 때만 해소다. 반환은 해소면 ``(task, None)``,
    미해소면 ``(None, 사유)``.

    task 기록 파일이 없는 것만 "task 없음" 이다. 읽기 실패·JSON 파손·모양이 다른 파일은 판정할
    근거가 아니라 고장이므로 예외로 끝난다.
    """
    wanted = session_id.strip() if isinstance(session_id, str) else ""
    if not wanted:
        return None, session_identity.SESSION_TASK_REASON_NO_SESSION
    ledger = pm_paths.leases_file(home)
    try:
        text = fs.read_text_shared(ledger, encoding="utf-8")
    except FileNotFoundError:
        return None, session_identity.SESSION_TASK_REASON_NO_TASK.format(session=wanted)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{ledger}: task 기록 파일 최상위가 JSON 객체가 아님")
    rows = data.get("tasks", [])
    if not isinstance(rows, list):
        raise ValueError(f"{ledger}: task 기록 `tasks` 가 목록이 아님")
    names = sorted(
        row["name"] for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("name"), str) and row["name"].strip()
        and isinstance(row.get("session"), str)
        and row["session"].strip() == wanted
    )
    if not names:
        return None, session_identity.SESSION_TASK_REASON_NO_TASK.format(session=wanted)
    if len(names) > 1:
        return None, session_identity.SESSION_TASK_REASON_MANY_TASKS.format(
            session=wanted, names=", ".join(names),
        )
    return names[0], None
