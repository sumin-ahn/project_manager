"""세션 장부 관측과 dashboard 락."""
from __future__ import annotations

from typing import Iterator
from pathlib import Path
import contextlib
from pm_engine.infra import fs
import json
from pm_engine.infra import pm_paths
import re


@contextlib.contextmanager
def _dashboard_lock(home) -> Iterator[None]:
    """대시보드 파일 read-modify-write 를 직렬화하는 OS 파일락 컨텍스트매니저 (MF-2).

    `.project_manager/.local/dashboard.lock` 에 배타 OS 락. 프로세스가 죽으면 OS 가 자동
    해제(stale-lock 없음·worktree_pool `_lease_lock` 동형). **재진입 금지** — 대시보드의 모든
    read→upsert→write 가 이 한 구간 안에서 일어나 cross-slot lost update 를 막는다.

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 `fs` seam 이 소유하고
    (모듈 상단 import 시점 바인딩) 이 도구는 *어느 파일에 거는지*(경로 규약)만 정한다.
    """
    with fs.exclusive_file_lock(pm_paths._dashboard_lock_file(home)):
        yield


_TICKET_FRONT_ID_RE = re.compile(r"^id:\s*(\S+)", re.MULTILINE)


_TICKET_FRONT_STATUS_RE = re.compile(r"^status:\s*(\S+)", re.MULTILINE)


_TICKET_FRONT_CLAIMED_BY_RE = re.compile(r"^claimed_by:\s*(\S+)", re.MULTILINE)


def _strip_yaml_scalar(value: str) -> str:
    """frontmatter 스칼라 값에서 surrounding 따옴표(`"`·`'`)를 벗긴다 (YAML 따옴표 값 방어)."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "'"):
        return v[1:-1]
    return v


def _claimed_tickets_for_session(home, session: str, tickets_dir: Path | None=None) -> list[str]:
    """활성 슬롯(`<repo>_<N>`)이 claim 한 티켓 ID 목록을 board tickets 에서 가볍게 스캔한다.

    board tickets(`_tickets_dir()`)가 status-subdir 레이아웃이면 `claimed/` 하위를, flat
    레이아웃이면 `status: claimed` frontmatter 를 대상으로, `claimed_by:`(`<email>/<session>`)의
    `/` 뒤 세션 파트가 `session` 과 같은 티켓의 `id:` 를 모은다. board 를 import 하지 않고
    (touches 격리·`_registered_repos`/leases 직접 read 동형) frontmatter 를 라인 스캔한다.
    디렉터리 부재는 빈 목록, 개별 티켓 읽기 OSError 는 그 티켓만 건너뛴다. 그 밖의 예외는 전파한다.
    """
    if tickets_dir is None:
        tickets_dir = pm_paths.tickets_dir(home)
    if not tickets_dir.is_dir():
        return []
    claimed_dir = tickets_dir / "claimed"
    if claimed_dir.is_dir():
        candidates = sorted(claimed_dir.glob("*.md"))
        require_status_claimed = False  # subdir 자체가 status 신호.
    else:
        candidates = sorted(tickets_dir.glob("*.md"))
        require_status_claimed = True
    result: list[str] = []
    for f in candidates:
        try:
            head = fs.read_text_shared(f, encoding="utf-8")[:2000]  # frontmatter 만 필요.
        except OSError:
            continue
        if require_status_claimed:
            sm = _TICKET_FRONT_STATUS_RE.search(head)
            # status 값도 따옴표 strip — `status: "claimed"`(YAML 따옴표) 인식 일관성.
            if sm is None or _strip_yaml_scalar(sm.group(1)) != "claimed":
                continue
        cb = _TICKET_FRONT_CLAIMED_BY_RE.search(head)
        if cb is None:
            continue
        # YAML 따옴표 값(`claimed_by: "…/my_app_1"`) 방어 — surrounding 따옴표를
        # 벗겨 `/` 뒤 세션 파트를 비교한다(현행 무해하나 견고화·codex suggestion).
        if _strip_yaml_scalar(cb.group(1)).rsplit("/", 1)[-1] != session:
            continue
        idm = _TICKET_FRONT_ID_RE.search(head)
        result.append(_strip_yaml_scalar(idm.group(1)) if idm else f.stem)
    return sorted(set(result))


def _read_json_object(path: Path) -> dict | None:
    """JSON object point-read. 부재·손상·스키마 불일치는 fail-soft ``None``."""
    try:
        value = json.loads(fs.read_text_shared(Path(path), encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _lease_slot_path(home, row: dict) -> Path | None:
    raw = row.get("slot")
    if not isinstance(raw, str) or not raw.strip():
        return None
    slot = Path(raw.strip())
    return slot if slot.is_absolute() else home.local_dir.parent.parent / slot


def _lease_rows(home) -> list[dict]:
    data = _read_json_object(
        pm_paths.leases_file(home)
    )
    rows = data.get("leases") if data else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _active_tasks(home) -> list[str]:
    """task 서술 디렉토리의 활성 이름. 종료 보관소 ``_ended``와 숨김 보조 디렉토리는 제외."""
    tasks_dir = pm_paths.tasks_dir(home)
    try:
        return sorted(
            child.name for child in tasks_dir.iterdir()
            if child.is_dir() and child.name != "_ended" and not child.name.startswith(".")
        )
    except OSError:
        return []
