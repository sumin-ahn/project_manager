#!/usr/bin/env python3
"""PM-home ticket 경로를 소유 repo 상대 좌표로 정규화한다 (T-0473).

Ticket ``touches`` 는 PM 홈 기준이라 ``work/<repo>_<N>/path`` 형태일 수 있다. 코드
소비자는 소유 worktree 안에서 ``path`` 를 써야 하므로, 검증된 슬롯 접두만 제거한다.

검증 소스는 두 가지다.

* ``workspace`` 가 주어지면 그 실재 디렉토리가 정확히 ``pm_root/work/<repo>_<N>`` 인지 본다.
  ``ticket_finish --task`` 처럼 호출부가 이미 lease 소유검사를 마친 경로가 이 seam 을 쓴다.
* ``workspace`` 가 없으면 PM 홈의 lease 장부에서 같은 지속 slot↔repo 매핑을 찾고, 실제
  디렉토리도 확인한다. lease의 활동 상태는 완료 직후 ``idle``일 수 있으므로 보지 않는다.
  ``domain affected --ticket`` 같이 task 인자를 받지 않는 조회 표면이 이 경로다.

``work/<repo>_<N>/`` 접두가 없으면 문자열을 그대로 돌려준다. 접두가 있는데 검증된 슬롯과
다르면 조용히 strip하지 않고 ``RepoCoordinateError`` 를 낸다. 잘못된 stage 귀속이 recall
누락보다 위험하므로 fail-loud가 이 모듈의 핵심 불변식이다.

좌표계 surface grep 감사 메모 (T-0473, 2026-07-26):
``rg -n 'def (_ticket_touches|_scope_args)|pages_for_touches\\(|get_ticket_touches\\(|repo / touch|\
paths = touches|_freshness_owner_repo|--ticket|--paths' .project_manager/tools/{board.py,domain.py,\
ticket_finish.py,external_review.py}`` 로 ticket 경로 생산·소비와 scoped regression 지점을 함께
확인했다. 정확 좌표가 필요한 활성 소비는 domain의 affected/capture·ticket_finish의 완료
domain 알림/task stage이며 모두 이 normalizer를 지난다. board scoped regression은
``_ticket_touches``→``_scope_args``에서 ``Path(t).stem``만 써 좌표 무관, freshness는
T-0470의 owner-repo clock을 쓴다. external_review는 **canonical worktree에서 ``--ticket``은
접두 경로→빈 diff 차단·엔진 티켓 codex 게이트는 ``--paths`` 필수**다.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


_WORKTREE_PREFIX = re.compile(
    r"^(?P<slot>work/(?P<repo>[^/]+)_(?P<number>\d+))(?:/(?P<relative>.*))?$"
)

# T-0397 baked stamp. 소비처는 이 값을 자기 rev와 대조해 부분 동기된 구 사본을
# RepoCoordinateError 속성 접근 전에 명시적인 sibling-skew 오류로 막는다.
ENGINE_REV = "v1.4.5"


class RepoCoordinateError(ValueError):
    """worktree 접두를 검증된 workspace/lease에 귀속할 수 없는 오류."""


class NormalizedRepoPath(str):
    """repo-relative 문자열이면서 원래 worktree의 소유 repo/channel을 보존하는 좌표.

    ``str`` 서브클래스라 기존 ``Path / touch``·문자열 비교 소비자는 그대로 동작한다.
    worktree 접두는 PM topology상 canonical code checkout이므로 owner channel은
    ``upstream``이다. ``repo``는 lease/slot의 논리 repo 이름, ``workspace``는 검증한
    checkout의 절대 실경로다. domain 매칭은 페이지 ``repo:`` 채널이 해소한 checkout과
    ``workspace``의 git common-dir 저장소 정체성을 대조해, 같은 저장소의 다른 정상 슬롯은
    허용하고 같은 채널에 같은 상대경로가 있는 다른 repo는 구분한다.
    """

    repo: str
    owner: str
    workspace: Path

    def __new__(
            cls,
            relative: str,
            *,
            repo: str,
            workspace: Path | str,
            owner: str = "upstream",
    ):
        obj = super().__new__(cls, relative)
        obj.repo = repo
        obj.owner = owner
        obj.workspace = Path(workspace).resolve()
        return obj


def canonicalize_path_notation(path: str) -> str:
    """접두 판정 전 표기 변형을 POSIX형 한 좌표로 모은다."""
    norm = path.replace(os.sep, "/").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def has_worktree_prefix(path: str) -> bool:
    """표기 정규화 뒤 ``work/<repo>_<N>`` slot 접두가 있는지 판정한다."""
    return _WORKTREE_PREFIX.match(canonicalize_path_notation(path)) is not None


_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _normalize_relative(relative: str, original: str) -> str:
    """slot 접두 뒤 상대부를 안전한 POSIX repo-relative 경로로 정규화한다.

    중복 ``/``와 ``.`` 세그먼트는 접고, 그 결과가 repo 루트이거나 상대 좌표가 아닌
    절대/drive/UNC 표기면 slot 전체 선언과 같은 위험으로 fail-loud 한다. ``..``는 접어서
    다른 위치로 재해석하지 않고 원문 세그먼트가 하나라도 있으면 traversal로 거부한다.
    """
    segments = relative.split("/")
    if ".." in segments:
        raise RepoCoordinateError(
            f"worktree touches 경로 traversal은 허용하지 않는다: {original!r}"
        )
    if relative.startswith("//"):
        raise RepoCoordinateError(
            f"worktree slot 상대부에 UNC 경로를 선언할 수 없다: {original!r}"
        )
    if relative.startswith("/"):
        raise RepoCoordinateError(
            f"worktree slot 상대부에 절대경로를 선언할 수 없다: {original!r}"
        )

    normalized = "/".join(segment for segment in segments if segment not in ("", "."))
    if not normalized:
        raise RepoCoordinateError(
            f"worktree slot 전체를 touches로 선언할 수 없다"
            f"(소유 repo 상대 경로가 비어 있음): {original!r}"
        )
    if _WINDOWS_DRIVE_PREFIX.match(normalized):
        raise RepoCoordinateError(
            f"worktree slot 상대부에 drive 경로를 선언할 수 없다: {original!r}"
        )
    return normalized


def _workspace_slot(workspace: Path | str, pm_root: Path) -> tuple[str, Path]:
    """workspace를 PM 홈 상대 slot 문자열로 바꾼다. PM 홈 밖/부재면 fail-loud."""
    root = Path(pm_root).resolve()
    candidate = Path(workspace)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        slot = candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise RepoCoordinateError(
            f"workspace가 PM 홈 밖이다: workspace={candidate}, pm_root={root}"
        ) from exc
    if not candidate.is_dir():
        raise RepoCoordinateError(f"workspace 디렉토리가 실재하지 않는다: {candidate}")
    return slot, candidate


def _registered_lease_slot(slot: str, repo: str, leases_file: Path) -> bool:
    """장부에서 state와 무관한 지속 slot↔repo 소유 매핑이 정확히 존재하는지 조회한다."""
    try:
        data = json.loads(leases_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepoCoordinateError(f"worktree lease 장부가 없다: {leases_file}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RepoCoordinateError(f"worktree lease 장부를 읽을 수 없다: {leases_file}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("leases", []), list):
        raise RepoCoordinateError(f"worktree lease 장부 형식이 잘못됐다: {leases_file}")

    matches = [
        row for row in data.get("leases", [])
        if isinstance(row, dict)
        and row.get("slot") == slot
    ]
    if len(matches) != 1:
        return False
    recorded_repo = matches[0].get("repo")
    return recorded_repo == repo


def normalize_repo_path(
    path: str,
    *,
    pm_root: Path,
    workspace: Path | str | None = None,
    leases_file: Path | None = None,
) -> str:
    """한 ticket 경로를 소유 repo 상대 좌표로 정규화한다.

    비-worktree 경로는 byte-for-byte 통과한다. ``work/<repo>_<N>/`` 접두 경로는
    ``workspace`` 또는 지속 lease 매핑 + 실재 디렉토리로 slot을 검증한 뒤에만 접두를
    제거하고, 반환 문자열에 소유 ``repo``/``owner`` 메타데이터를 보존한다.
    """
    canonical = canonicalize_path_notation(path)
    match = _WORKTREE_PREFIX.match(canonical)
    if match is None:
        return path

    slot = match.group("slot")
    repo = match.group("repo")
    number = match.group("number")
    relative = match.group("relative") or ""
    if not number.isdigit() or int(number) < 1:
        raise RepoCoordinateError(f"유효하지 않은 worktree slot 접두: {slot!r}")
    relative = _normalize_relative(relative, path)

    root = Path(pm_root).resolve()
    if workspace is not None:
        expected_slot, candidate = _workspace_slot(workspace, root)
        if expected_slot != slot:
            raise RepoCoordinateError(
                f"touches slot 불일치: 선언={slot!r}, 검증 workspace={expected_slot!r}"
            )
        return NormalizedRepoPath(relative, repo=repo, workspace=candidate)

    ledger = Path(leases_file) if leases_file is not None else (
        root / ".project_manager" / ".local" / "worktree-leases.json"
    )
    if not _registered_lease_slot(slot, repo, ledger):
        raise RepoCoordinateError(
            f"touches slot 불일치: 선언={slot!r}, lease 장부의 지속 slot↔repo 매핑에 "
            f"일치 항목 없음 ({ledger})"
        )
    slot_path = root / slot
    if not slot_path.is_dir():
        raise RepoCoordinateError(
            f"touches slot lease는 있으나 workspace 디렉토리가 없다: {slot_path}"
        )
    return NormalizedRepoPath(relative, repo=repo, workspace=slot_path)


def normalize_repo_paths(
    paths: list[str],
    *,
    pm_root: Path,
    workspace: Path | str | None = None,
    leases_file: Path | None = None,
) -> list[str]:
    """경로 목록에 ``normalize_repo_path``의 단일 불변식을 순서대로 적용한다."""
    return [
        normalize_repo_path(
            path,
            pm_root=pm_root,
            workspace=workspace,
            leases_file=leases_file,
        )
        for path in paths
    ]
