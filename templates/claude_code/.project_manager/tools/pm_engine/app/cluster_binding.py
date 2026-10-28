"""클러스터 결속 및 발행 재료 (층: app)."""
from __future__ import annotations

from typing import Any
from pathlib import Path
from collections.abc import Sequence
from pm_engine.infra import branch_store
from pm_engine.domain import cluster_rules
from pm_engine.infra import cluster_store
from pm_engine.infra import git
from pm_engine.domain import identity_args
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths
from pm_engine.infra import repo_coordinates
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store
from pm_engine.infra import worktree_git


_LIST_ACTIVE_STATUSES: tuple[str, ...] = ticket_rules.ACTIVE_STATUS_DIRS

_PUBLISH_OVERLAP_HEADER = '=== ⚠ 발행 touches 겹침 (다른 활성/draft 티켓) ==='

_PUBLISH_OVERLAP_PATH_LIMIT = 8

_PUBLISH_OVERLAP_ID_LIMIT = 6

_PUBLISH_OVERLAP_TOTAL_LINE_BUDGET = 9

def _publish_overlap_candidates(home, exclude_ids: Sequence[str]) -> list[tuple[str, dict[str, Any]]]:
    """겹침 판정 후보 집합 — 활성 3상태(open/claimed/blocked) + drafts, 제외 ID 빼고.

    제외 집합이 복수인 이유는 묶음(클러스터) 발행 재료 때문이다 — 멤버끼리의 겹침은 묶음의
    근거라 후보에서 빼고 별도 줄로 표기한다(호출부가 그 줄을 만든다).

    done 은 후보가 아니다(완료된 과거 작업과의 겹침은 판단 재료가 아니다). draft 는
    `board.md`·`list`·board-git 어디에도 안 보이면서 좌표를 이미 점유하므로 포함한다(다른
    draft 가 이미 board.py 같은 엔진 파일을 touch 하고 있는 형상이 실제로 있었다). 손상
    frontmatter 는 `load_ticket_soft` 가 경고 1줄과 함께 skip(fail-soft·rc 불변).
    """
    excluded = frozenset(exclude_ids)
    paths: list[Path] = []
    for status in _LIST_ACTIVE_STATUSES:
        status_dir = pm_paths.tickets_dir(home) / status
        if status_dir.is_dir():
            paths.extend(sorted(status_dir.glob('*.md')))
    drafts = pm_paths.drafts_dir(home)
    if drafts.is_dir():
        paths.extend(sorted(drafts.glob('*.md')))
    candidates: list[tuple[str, dict[str, Any]]] = []
    for path in paths:
        loaded = ticket_store.load_ticket_soft(home, path)
        if loaded is None:
            continue
        fm = loaded[0]
        tid = str(fm.get('id') or '').strip()
        if not tid or tid in excluded:
            continue
        candidates.append((tid, fm))
    return candidates

def _fold_publish_touches(home, items: list[str]) -> tuple[str, ...]:
    """touches 선언을 발행 시점(`workspace` 없음) 좌표로 접는다 — 검증 실패 항목은 드롭.

    `pm_delegate._folded_touch_pairs` 와 같은 정규화 순서(`normalize_repo_path` →
    `canonicalize_path_notation` → `_touch_notation`)이되, 그 함수는 `workspace` 를 무조건
    요구해 발행 시점(claim 이전·workspace 미정)에는 못 쓴다 — `_touches_existence_issues` 와
    동형으로 `workspace=None`(lease 장부 매핑으로 검증)을 쓴다. 다른 슬롯 선언·미실재 슬롯은
    항목 단위로 드롭한다(크래시 0).
    """
    folded: list[str] = []
    for item in items:
        try:
            normalized = repo_coordinates.normalize_repo_path(str(item), pm_root=home.root)
        except repo_coordinates.RepoCoordinateError:
            continue
        value = ticket_rules._touch_notation(repo_coordinates.canonicalize_path_notation(str(normalized)))
        if value and value != '.':
            folded.append(value)
    return tuple(dict.fromkeys(folded))

def _publish_idle_slot_count(home, repo: str) -> int | None:
    """`repo`(canonical 등록 repo 정체성 — 물리 폴더명 아님)의 가용(idle) 슬롯 수.

    장부 미해소(부재/손상)면 `None`(재료 줄 생략). 데이터 원천은 lease 장부 하나, 읽기는
    `lease_store.repo_slot_state_counts` 술어 하나뿐이다(board 안에 새 장부 read 를 만들지
    않는다). `creating` 은 idle 로 세지 않는다.

    `repo` 는 호출부가 이미 해소한 canonical 값을 받는다 — `new` 는 `--repo`/`--slot` 로 명시된
    args 정체성(`identity_args.parse_identity`), `promote` 는 티켓 `created_by` provenance 를
    `lease_store.session_coordinates` 로 재해소한 장부 등록 repo 다. 물리 폴더명(`REPO.name`)은
    `--repo` 로 다른 등록명을 공식 지원하는 `new` 형상에서 장부 repo 와 어긋날 수 있어(리뷰 실측),
    이 함수 안에서 다시 유도하지 않는다 — 새 해소 축을 만들지 않고 호출부의 값을 그대로 쓴다.
    """
    counts = lease_store.repo_slot_state_counts(repo, pm_paths.leases_file(home))
    if counts is None:
        return None
    return counts.get('idle', 0)

def _format_publish_overlap_material(per_path: dict[str, set[str]], idle_count: int | None) -> list[str]:
    """경로별 겹침 집계 → stderr 줄 목록. 겹침 0 이면 슬롯 줄까지 포함해 **전부 침묵**(never-block).

    총 줄 수(헤더 + 경로 표시분 + 잔여-요약 1줄(있으면) + 슬롯 줄(있으면))는
    `_PUBLISH_OVERLAP_TOTAL_LINE_BUDGET` 을 넘지 않는다 — 슬롯 줄이 붙으면 그만큼 경로 표시
    예산이 줄어든다. `_PUBLISH_OVERLAP_PATH_LIMIT`(8)은 슬롯 줄이 없을 때 도달 가능한 상한이지
    항상 보장되는 값이 아니다(리뷰 F-003).
    """
    if not per_path:
        return []
    ordered = sorted(per_path.items(), key=lambda kv: (len(kv[1]), kv[0]))
    slot_line: str | None = None
    if idle_count is not None:
        slot_line = f'  가용(idle) 슬롯 {idle_count}개 — 겹침이 곧 직렬 근거는 아니다(슬롯 분리로 병렬 가능)'
    reserved = 1 + (1 if slot_line is not None else 0)
    path_budget = min(_PUBLISH_OVERLAP_PATH_LIMIT, max(0, _PUBLISH_OVERLAP_TOTAL_LINE_BUDGET - reserved))
    if len(ordered) <= path_budget:
        shown, hidden = (ordered, 0)
    else:
        shown = ordered[:max(0, path_budget - 1)]
        hidden = len(ordered) - len(shown)
    lines = [_PUBLISH_OVERLAP_HEADER]
    for path, ids in shown:
        id_list = sorted(ids)
        head = ', '.join(id_list[:_PUBLISH_OVERLAP_ID_LIMIT])
        remainder = len(id_list) - _PUBLISH_OVERLAP_ID_LIMIT
        suffix = f' 외 {remainder}건' if remainder > 0 else ''
        lines.append(f'  {path}: {len(ids)}건 겹침 ({head}{suffix})')
    if hidden > 0:
        lines.append(f'  … 외 {hidden}개 경로')
    if slot_line is not None:
        lines.append(slot_line)
    return lines

def _publish_overlap_material(home, tid: str, touches: object, repo: str) -> list[str]:
    """`new`/`promote` 공용 진입점 — touches 겹침 + 가용 슬롯 재료를 stderr 줄 목록으로 렌더.

    `repo` 는 슬롯 재료(`_publish_idle_slot_count`)가 lease 장부를 조회할 canonical 정체성 —
    호출부가 이미 해소한 값을 그대로 받는다(이 함수 안에서 유도하지 않는다).

    touches 형식 불명은
    이 축 전체를 판정불능으로 세어 침묵한다(통과로 위장하지 않되 차단도 하지 않는다 — 부분
    동기 사본에서 이 축만 시야 밖이어도 `new`/`promote` 자체는 막지 않는다).
    """
    own_items = ticket_rules._normalized_touches(touches)
    if not own_items:
        return []
    own_folded = _fold_publish_touches(home, own_items)
    if not own_folded:
        return []
    candidates = _publish_overlap_candidates(home, (tid,))
    per_path: dict[str, set[str]] = {path: set() for path in own_folded}
    for other_id, other_fm in candidates:
        other_items = ticket_rules._normalized_touches(other_fm.get('touches'))
        if not other_items:
            continue
        other_folded = _fold_publish_touches(home, other_items)
        if not other_folded:
            continue
        for own_path in own_folded:
            if any((ticket_rules._touch_paths_overlap(own_path, other_path) for other_path in other_folded)):
                per_path[own_path].add(other_id)
    nonzero = {path: ids for path, ids in per_path.items() if ids}
    return _format_publish_overlap_material(nonzero, _publish_idle_slot_count(home, repo))

def cluster_complete_binding_problem(home, tid: str, ticket_fm: dict[str, Any], cluster_close: str | None) -> str | None:
    """완료 호출이 티켓의 정확한 cluster에 결속되지 않은 이유를 돌려준다.

    ``--cluster-close``는 인증 토큰이 아니라 path-binding이다. 인자·티켓 역참조·유일한
    ledger owner가 모두 일치하고 장부 전 멤버의 양방향 귀속이 온전할 때만 통과한다.
    장부도 ``cluster`` 선언도 없는 legacy 티켓은 `ticket_cluster` 의 이름 해석과 **같은
    규칙**을 쓴다 — 그 티켓의 크기 1 해석 id(`C-` + 티켓 ID)와 정확히 같은 결속만 통과한다.
    """
    binding = cluster_rules.cluster_id_for_name(cluster_close) if cluster_close else None
    declared_raw = str(ticket_fm.get('cluster') or '').strip()
    declared = cluster_rules.cluster_id_for_name(declared_raw) if declared_raw else None
    ledgers, catalog_problem = cluster_store.all_clusters_strict(home)
    if catalog_problem is not None:
        return catalog_problem
    owners = [ledger for ledger in ledgers if tid in cluster_store.cluster_tickets(ledger)]
    if not owners and declared is None:
        legacy_binding = cluster_rules.cluster_id_for_name(tid)
        if binding is not None and binding != legacy_binding:
            return f'cluster 결속 {binding}을 검증할 장부가 없다 — 장부 없는 legacy 티켓의 크기 1 해석은 {legacy_binding}이다'
        return None
    if len(owners) != 1:
        owner_ids = [str(row.get('id') or '<id 없음>') for row in owners]
        return f'cluster ledger owner가 정확히 하나가 아니다: {owner_ids or ['없음']} — 멤버십을 복구한 뒤 묶음 종결을 다시 실행하라'
    ledger = owners[0]
    owner_raw = str(ledger.get('id') or '').strip()
    owner = cluster_rules.cluster_id_for_name(owner_raw) if owner_raw else ''
    members = cluster_store.cluster_tickets(ledger)
    if not owner or len(members) != len(set(members)) or (not members):
        return f'{owner or '<id 없음>'} 장부의 멤버 집합이 비었거나 중복됐다'
    if declared != owner:
        return f'티켓 cluster 역참조({declared or '없음'})와 ledger owner({owner})가 일치하지 않는다'
    if binding != owner:
        return f'활성 cluster 멤버는 direct complete할 수 없다 — ticket_finish 묶음 종결의 exact 결속 {owner}가 필요하다'
    if str(ledger.get('status') or '').strip() == 'closed':
        return f'{owner} 장부는 closed인데 {tid}가 claimed다 — 모순을 complete로 덮지 않는다'
    for member in members:
        found = ticket_store.find_ticket_exact(home, member)
        if found is None:
            return f'{owner} 멤버 {member} 티켓을 찾지 못했다'
        loaded = ticket_store.load_ticket_soft(home, found[1])
        member_cluster = str((loaded[0] if loaded else {}).get('cluster') or '').strip()
        if not member_cluster or cluster_rules.cluster_id_for_name(member_cluster) != owner:
            return f'{owner} 멤버 {member}의 cluster 역참조가 일치하지 않는다: {member_cluster or '없음'}'
        member_owners = [str(row.get('id') or '') for row in ledgers if member in cluster_store.cluster_tickets(row)]
        if member_owners != [owner]:
            return f'{member}가 여러 cluster ledger에 귀속됐다: {member_owners}'
    return None

def cluster_binding_problem(home, cluster: str, *, tree: str | None=None) -> str | None:
    """명시 장부의 제품 branch/base 결속을 읽기로 검증한다 (추정·재작성 없음)."""
    ledger = cluster_store.load_cluster(home, cluster)
    branch = str((ledger or {}).get('branch') or '').strip()
    base = str((ledger or {}).get('base_branch') or '').strip()
    if not branch or not base:
        return f'{cluster}는 제품 브랜치 미결속 장부다. 기존 `board.py cluster new <ticket-group> --tickets <티켓들> --task <task>`(task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가)으로 명시 재결속하라'
    if tree is not None:
        return None if _cluster_branch_state(tree, branch) is True and _cluster_branch_state(tree, base) is True else f'{cluster}의 branch {branch} 또는 base_branch {base}를 제품 트리 {tree}에서 확인하지 못했다'
    status, rows = lease_store._read_lease_rows_status(pm_paths.leases_file(home))
    if status == identity_args.LEDGER_UNREADABLE:
        return f'{cluster} 제품 결속을 검증할 대여 기록을 읽지 못했다'
    repositories: set[str] = set()
    for row in rows:
        slot = str(row.get('slot') or '').strip()
        folder = home.root / slot
        if not slot or not (folder / '.git').exists():
            continue
        branch_state = _cluster_branch_state(str(folder), branch)
        base_state = _cluster_branch_state(str(folder), base)
        if branch_state is None or base_state is None:
            return f'{cluster}의 제품 브랜치를 관측하지 못했다: {folder}'
        if branch_state and base_state:
            common = git._git_run(['rev-parse', '--git-common-dir'], repo=folder)
            if common is None or common[0] != 0:
                return f'{cluster} 제품 저장소 근거를 읽지 못했다: {folder}'
            repositories.add(str((folder / common[1].strip()).resolve()))
    if len(repositories) != 1:
        return f'{cluster}의 branch {branch}·base_branch {base}를 가진 제품 저장소가 정확히 하나가 아니다 ({len(repositories)}개). 명시 결속을 확인하라'
    return None

def _cluster_branch_state(tree: str, branch: str) -> bool | None:
    """그 트리에 브랜치가 있는가 — 판정 불능(git 부재·비-git·fatal)은 None."""
    result = git._git_run(['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], repo=Path(tree))
    if result is None or result[0] > 1:
        return None
    return result[0] == 0

def _cluster_new_base_branch(tree: str, branch: str, current: str | None) -> tuple[str | None, str | None]:
    """`cluster new` 가 장부에 적을 기준 브랜치 — `(기준, None)` 또는 `(None, 멈춤 사유)`.

    묶음 브랜치가 이미 있으면 그 부모 기록(`branch.<묶음브랜치>.pm-parent`)이 기준이다. 지목 트리가
    이미 묶음 브랜치에 앉아 있어도(task branch 로 먼저 만든 경로) 통합 브랜치가 기록된다. 아직 없으면
    지목 트리의 현재 브랜치다(`_cluster_ensure_branch` 가 그 브랜치를 부모로 기록하며 만든다).
    기준을 정하지 못하면 사유를 돌려주고 호출부가 첫 쓰기 전에 멈춘다.
    """
    state = _cluster_branch_state(tree, branch)
    if state is None:
        return (None, f'묶음 브랜치 {branch} 의 실재를 판정하지 못했다(코드 git 판정 불능: {tree}) — 아무것도 쓰지 않았다.')
    if state:
        record = branch_store.read_branch_parent(branch, git_runner=worktree_git._real_git_runner(Path(tree)))
        if record is None:
            return (None, f'묶음 브랜치 {branch} 가 이미 있는데 부모 기록 `branch.{branch}.pm-parent` 가 없어 기준 브랜치를 정하지 못한다(지목 트리: {tree}) — 아무것도 쓰지 않았다. 그 브랜치를 든 작업 폴더에서 `worktree_pool.py rebase <슬롯> --task <task이름> --onto <통합브랜치>` 로 부모를 기록하거나, 쓰지 않는 브랜치면 지운 뒤 다시 실행하라.')
        return (record['name'], None)
    if not current:
        return (None, f'지목 트리 {tree} 의 현재 브랜치를 읽지 못해 묶음 브랜치 {branch} 의 기점을 정하지 못한다 — 아무것도 쓰지 않았다.')
    return (current, None)

def _cluster_ensure_branch(tree: str, branch: str, base: str) -> str:
    """통합 브랜치를 만든다(있으면 그대로) — 결과를 사람이 읽는 1줄로 돌려준다.

    기준 브랜치 `base` 는 `_cluster_new_base_branch` 가 첫 쓰기 전에 정한 값이다. 브랜치 생성 실패는
    장부 생성을 막지 않는다 — 이 1줄이 `cluster new` 출력에 그대로 실리고, 그 뒤의 브랜치 부재는
    **트리를 든 표면**(확인 실행 `cli.delegate_cmd._cluster_confirmation_tree` ·
    공용 트리 해소 `app.cluster_code_tree.resolve_code_tree`)가 자기 자리에서 판정한다(조용한 통과 아님).
    """
    if _cluster_branch_state(tree, branch):
        return f'이미 있음 ({tree})'
    result = git._git_run(['branch', branch, base], repo=Path(tree))
    if result is None or result[0] != 0:
        return f'생성 실패(기점 {base} · {tree})'
    runner = worktree_git._real_git_runner(Path(tree))
    rc, fork = runner(['rev-parse', '--verify', f'{base}^{{commit}}'])
    if rc != 0:
        raise branch_store.BranchParentError(f'{branch} 를 만들었지만 기점 {base} 커밋을 읽지 못해 부모를 기록하지 못했다: {fork.strip()}')
    branch_store.record_branch_parent(branch, base, fork.strip(), git_runner=runner)
    return f'생성됨 (기점 {base} · 부모 기록 · {tree})'
