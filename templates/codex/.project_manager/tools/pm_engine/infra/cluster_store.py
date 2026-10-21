"""묶음(cluster) 장부 읽기 (층: infra).

장부 디렉터리 위치와 ID 접두, 장부의 멤버 목록 판독, 장부 전체 조회(손상 파일은 경고 뒤 건너뜀)를 소유한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pm_engine.infra import pm_paths, ticket_store


# ── 클러스터 장부 (운영 단위 = 티켓 묶음 · 크기 1 도 클러스터) ─────────────────
#
# 설계·리뷰·fix 를 묶음당 1회로 도는 운영 단위를 board 가 소유한다. 장부는
# `tickets/clusters/<id>.md`(frontmatter 만)이고 STATUS_DIRS 밖 sibling 이라
# (`tickets/rounds/` 와 같은 형상) 상태 순회·board.md 렌더에 섞이지 않는다. 장부가 담는 것은
# 멤버십·통합 브랜치·설계 spike·고정 라운드 예산뿐이다 — 라운드 산출 자리는 종전대로
# `tickets/rounds/<티켓>/` 이고 장부는 그것을 담지 않는다.
#
# **특례가 없다** — 티켓 하나짜리 묶음도 크기 1 클러스터다. `new` 는 그 티켓 이름의 크기 1
# 장부를 함께 만들고 기준 브랜치·예산을 그 자리에서 박는다(빈 값 0 · 판정 표면은 선언된 값만
# 읽는다). 여러 티켓을 한 묶음으로 묶는 것은 사람 선언(`cluster new`)뿐이다 — 엔진이 고르면
# 선언한 적 없는 묶음이 생긴다. 장부는 open 진입 세 경로(발행 승격 · `promote` · `reopen`)가
# 만들고 멤버 조회(`cluster_members`)는 그 선언만 읽는다 — 장부 없는 묶음은 멤버가 없다.
CLUSTERS_DIRNAME = "clusters"
CLUSTER_ID_PREFIX = "C-"                  # 장부 파일명·frontmatter `id` 의 접두


def clusters_dir(home) -> Path:
    """클러스터 장부 디렉토리 — `tickets_dir()/clusters` (board_root 추종·STATUS_DIRS 밖)."""
    return pm_paths.tickets_dir(home) / CLUSTERS_DIRNAME


def cluster_tickets(fm: dict[str, Any]) -> list[str]:
    """장부의 멤버 목록 — 문자열 하나로 쓴 형상도 1건으로 접는다(형식 관용)."""
    raw = fm.get("tickets")
    items = [raw] if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
    return [str(item).strip() for item in items if str(item).strip()]


def all_clusters(home) -> list[dict[str, Any]]:
    """장부 전체 — id 순. 손상 파일은 경고 1줄과 함께 빠진다(한 파일이 순회를 죽이지 않는다)."""
    directory = clusters_dir(home)
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob(f"{CLUSTER_ID_PREFIX}*.md")):
        loaded = ticket_store.load_ticket_soft(home, path)
        if loaded is None:
            continue
        fm = loaded[0]
        if str(fm.get("id") or "").strip():
            out.append(fm)
    return out
