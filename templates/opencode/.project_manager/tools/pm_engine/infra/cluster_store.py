"""묶음(cluster) 장부 읽기 (층: infra).

장부 디렉터리 위치와 장부의 멤버 목록 판독, 장부 전체 조회(손상 파일은 경고 뒤 건너뜀)를 소유한다.
"""
from __future__ import annotations

from pm_engine.domain import cluster_rules
from pm_engine.domain import ticket_rules

from pathlib import Path
from typing import Any

from pm_engine.infra import pm_paths, ticket_store


# ── 클러스터 장부 (운영 단위 = 티켓 묶음 · 크기 1 도 클러스터) ─────────────────
#
# 설계·리뷰·fix의 묶음 장부 IO는 이 모듈이, ID·멤버 규칙은 domain.cluster_rules가 소유한다. 장부는
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
    for path in sorted(directory.glob(f"{cluster_rules.CLUSTER_ID_PREFIX}*.md")):
        loaded = ticket_store.load_ticket_soft(home, path)
        if loaded is None:
            continue
        fm = loaded[0]
        if str(fm.get("id") or "").strip():
            out.append(fm)
    return out


def cluster_ledger_path(home, cluster: str) -> Path:
    """장부 파일 경로 — 이름/장부 id 어느 표기로 불러도 같은 파일을 가리킨다."""
    return clusters_dir(home) / f'{cluster_rules.cluster_id_for_name(cluster)}.md'

def load_cluster(home, cluster: str) -> dict[str, Any] | None:
    """장부 frontmatter — 부재는 None, 손상은 경고 1줄 뒤 None(`load_ticket_soft` 동형)."""
    path = cluster_ledger_path(home, cluster)
    if not path.is_file():
        return None
    loaded = ticket_store.load_ticket_soft(home, path)
    return loaded[0] if loaded is not None else None

def dump_cluster(home, fm: dict[str, Any]) -> Path:
    """장부를 키 선언 순서로 쓴다 — 본문 없음(라운드 산출은 장부에 담지 않는다)."""
    ordered: dict[str, Any] = {key: fm.get(key) for key in cluster_rules.CLUSTER_LEDGER_KEYS}
    for key, value in fm.items():
        if key not in ordered:
            ordered[key] = value
    path = cluster_ledger_path(home, str(ordered.get('id') or ''))
    ticket_store.dump_ticket(path, ordered, '')
    return path

def all_clusters_strict(home) -> tuple[list[dict[str, Any]], str | None]:
    """lifecycle mutation 판정용 장부 전체 — `(장부 목록, 판독 불능 사유)`.

    조회용 `all_clusters()` 는 손상 파일을 경고 뒤 건너뛴다(한 파일이 순회를 죽이지 않는다).
    귀속 유일성 같은 mutation 판정은 그 관용을 쓸 수 없다 — 빠진 파일이 그 티켓을 담고 있을
    가능성을 배제하지 못한 채 "owner 가 하나"라고 말하게 된다. 판정 불능은 통과가 아니라
    정지라 사유를 값으로 돌려주고, 호출자가 **첫 write 앞에서** 거부한다.
    """
    directory = clusters_dir(home)
    if not directory.is_dir():
        return ([], None)
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob(f'{cluster_rules.CLUSTER_ID_PREFIX}*.md')):
        loaded = ticket_store.load_ticket_soft(home, path)
        if loaded is None:
            return ([], f'cluster 장부를 읽지 못했다: {pm_paths._rel_to_repo(home, path)} — 귀속 유일성을 전수 확인할 수 없어 정지한다(장부를 고친 뒤 다시 실행하라)')
        fm = loaded[0]
        if not str(fm.get('id') or '').strip():
            return ([], f'cluster 장부에 id 선언이 없다: {pm_paths._rel_to_repo(home, path)} — 귀속 유일성을 전수 확인할 수 없어 정지한다(장부를 고친 뒤 다시 실행하라)')
        out.append(fm)
    return (out, None)

def ticket_cluster_from_text(tid: str, ticket_text: str) -> str:
    """명세 텍스트에서 그 티켓의 클러스터 id — 라운드 준비가 쓰는 읽기 seam.

    파싱 실패·필드 부재는 크기 1 로 접는다(`ticket_cluster` 와 같은 규칙). 준비는 이 값 하나로
    run-dir 을 가르므로, 손상 명세에서도 좌표가 결정적이어야 한다.
    """
    import yaml

    fm: Any = {}
    try:
        fm, _body = ticket_rules._parse_ticket_text(ticket_text, '<티켓 명세>')
    except (ValueError, yaml.YAMLError):
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return cluster_rules.ticket_cluster(tid, fm)

def cluster_members(home, cluster: str) -> tuple[str, ...]:
    """멤버 티켓 ID — **장부가 선언한 목록**(장부가 없으면 빈 튜플).

    멤버를 합성하지 않는다. 이름만 보고 접으면 선언한 적 없는 묶음이 조회·준비 표면에 생기고,
    장부 삭제가 곧 그 묶음의 판정 우회 수단이 된다. 장부는 open 진입 세 경로(발행 승격·
    `promote`·`reopen`)가 만들므로 열린 티켓은 언제나 자기 장부를 갖고, 장부 없는 이름의 빈
    튜플은 조회 부재(rc≠0)와 준비 거부의 입력이다.
    """
    fm = load_cluster(home, cluster)
    return tuple(cluster_tickets(fm)) if fm is not None else ()

def cluster_live_members(home, cluster: str) -> tuple[str, ...]:
    """선언 멤버 중 처분 종결(`discarded`)이 아닌 멤버 — 라운드 준비·리뷰·처분의 대상.

    처분된 멤버는 코드 산출이 없어 라운드·리뷰 입력·확인 처분의 대상이 아니다(종결의 "처분 종결
    멤버"와 같은 규칙). 선언 목록(`cluster_members`)은 장부 그대로 두고, 대상 판정은 이 함수
    하나가 한다. board 에서 찾지 못한 멤버는 남겨 두어 소비처가 부재를 그대로 드러내게 한다.
    """
    live = []
    for tid in cluster_members(home, cluster):
        found = ticket_store.find_ticket_exact(home, tid)
        if found is not None and found[0] == 'discarded':
            continue
        live.append(tid)
    return tuple(live)

def _cluster_of_record(home, tid: str) -> str | None:
    """**장부 파일이** 이 티켓을 멤버로 담고 있는 클러스터 id (없으면 None).

    티켓 frontmatter 의 선언과 별개 축이다 — 흡수·중복 귀속 판정은 파일 쪽을 본다.
    """
    for fm in all_clusters(home):
        if tid in cluster_tickets(fm):
            return str(fm.get('id') or '').strip() or None
    return None
