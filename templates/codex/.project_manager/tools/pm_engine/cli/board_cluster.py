"""클러스터 명령 (층: cli)."""
from __future__ import annotations

from typing import Any
from pathlib import Path
from collections.abc import Sequence
import argparse
from pm_engine.app import board_sync
from pm_engine.app import cluster_binding
from pm_engine.domain import cluster_rules
from pm_engine.infra import cluster_store
import contextlib
from pm_engine.infra import fs
from pm_engine.infra import git
from pm_engine.domain import identity_args
from pm_engine.infra import pm_paths
from pm_engine.infra import repo_coordinates
import sys
from pm_engine.cli import task_args
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store


def _cluster_auto_attach(home, tid: str, args: argparse.Namespace, *, publish_basis: tuple[str | None, str | None] | None=None) -> tuple[str, Path]:
    """발행 시점 크기 1 장부 생성 — `(클러스터 id, 장부 경로)`.

    새 티켓은 **자기 장부**를 갖는다. 여러 티켓을 한 묶음으로 묶는 것은 사람 선언
    (`cluster new`)이고 엔진은 고르지 않는다 — 활성 묶음에 새 티켓을 자동으로 끼우면 선언한 적
    없는 묶음이 생기고, 그 묶음의 예산·기준 브랜치가 끼워진 티켓의 판정을 대신한다.

    명시 제품 기준 브랜치와 예산을 기록한다. branch 선언은 기존 cluster new의 몫이며,
    실행·종결은 그 명시 결속 전까지 거부한다. 재개방의 무선택 경로는 기준을 추측하지 않는다.

    board_lock 보유 전제다(발행·재개방 임계구역 안에서 부른다).
    """
    _tree, base_branch = _cluster_publish_base_branch(home, args) if publish_basis is None else publish_basis
    fm = cluster_rules._new_cluster_fm(f'{cluster_rules.CLUSTER_ID_PREFIX}{tid}', [tid], base_branch=base_branch)
    return (str(fm['id']), cluster_store.dump_cluster(home, fm))

def _cluster_publish_base_branch(home, args: argparse.Namespace) -> tuple[str | None, str | None]:
    """장부에 박을 `(코드 트리, 기준 브랜치)` — 트리 미해소·브랜치 없는 트리(detached·비-git)는 None.

    `--task` 가 보유한 폴더만 `_claim_code_tree`로 해소한다. task 가 없으면 제품 결속의 증거가
    없으므로 기준 브랜치를 기록하지 않는다.
    """
    tree = task_args._claim_code_tree(home, args)
    if not tree or not (Path(tree) / '.git').exists() or (not git._git_head_at(tree)):
        return (None, None)
    top = git._git_run(['rev-parse', '--show-toplevel'], repo=Path(tree))
    if top is None or top[0] != 0 or Path(top[1].strip()).resolve() != Path(tree).resolve():
        return (None, None)
    return (tree, git._cluster_current_branch(tree) if tree else None)

def _cluster_release_member(home, cluster: str, tid: str) -> Path | None:
    """흡수된 멤버를 옛 장부에서 뺀다 — 갱신/제거한 경로(해소 불가면 None).

    발행이 자동으로 만든 크기 1 장부가 비면 그 파일을 지운다: 엔진이 방금 만들고 엔진이 비운
    산출물이고, 남겨 두면 멤버 0 인 장부가 lint 잡음으로 상주한다. 사람이 선언한 묶음(이름이
    티켓 ID 가 아니거나 브랜치가 있는 장부)은 비어도 지우지 않는다.

    삭제 실패에서 관용하는 것은 **부재(`FileNotFoundError`)뿐**이다 — 이미 없으면 이 호출이
    원하는 상태라 멱등이다. 그 밖의 삭제 실패는 올린다: 삼키면 옛 장부가 멤버를 그대로 쥔 채
    새 장부와 티켓 명세만 갱신돼 명령이 성공을 선언한 뒤 중복 귀속이 남는다(조용한 모순).
    삭제 뒤 부재도 확인한다 — 성공 선언의 근거는 반환값이 아니라 파일이 없다는 관측이다.
    """
    fm = cluster_store.load_cluster(home, cluster)
    if fm is None:
        return None
    members = [item for item in cluster_store.cluster_tickets(fm) if item != tid]
    fm['tickets'] = members
    path = cluster_store.cluster_ledger_path(home, cluster)
    auto_shape = not members and cluster_rules.is_auto_cluster_id(cluster) is not None and (not fm.get('branch'))
    if auto_shape:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        if path.exists():
            raise OSError(f'자동 장부 삭제 뒤에도 파일이 남아 있다: {path}')
        return path
    return cluster_store.dump_cluster(home, fm)

def _cluster_overlap_material(home, members: Sequence[tuple[str, object]], repo: str) -> list[str]:
    """`cluster new` 판단 재료 — 묶음 **밖** 겹침 + 가용 슬롯 + 묶음 **안** 겹침 1줄.

    후보에서 멤버 전부를 뺀다 — 멤버끼리의 겹침은 묶음을 반대할 근거가 아니라 묶음의 근거라
    별도 줄로 표기한다(침묵시키면 재료가 준다). 좌표 정규화·비교 규칙 소유자는 발행 재료와
    같다(`repo_coordinates`·`ticket_rules` · 사본 0). 판정불능(형식 불명)은
    이 축 전체를 침묵으로 접는다 — 차단하지 않되 통과로 위장하지도 않는다(재료 축 그대로).
    """
    folded: dict[str, tuple[str, ...]] = {}
    for tid, touches in members:
        items = ticket_rules._normalized_touches(touches)
        folded[tid] = cluster_binding._fold_publish_touches(home, items) if items else ()
    own_paths = tuple(dict.fromkeys((path for paths in folded.values() for path in paths)))
    if not own_paths:
        return []
    per_path: dict[str, set[str]] = {path: set() for path in own_paths}
    for other_id, other_fm in cluster_binding._publish_overlap_candidates(home, tuple(folded)):
        other_items = ticket_rules._normalized_touches(other_fm.get('touches'))
        if not other_items:
            continue
        other_folded = cluster_binding._fold_publish_touches(home, other_items)
        if not other_folded:
            continue
        for own_path in own_paths:
            if any((ticket_rules._touch_paths_overlap(own_path, other_path) for other_path in other_folded)):
                per_path[own_path].add(other_id)
    lines = cluster_binding._format_publish_overlap_material({path: ids for path, ids in per_path.items() if ids}, cluster_binding._publish_idle_slot_count(home, repo))
    internal: dict[str, list[str]] = {}
    for path in own_paths:
        sharing = sorted((tid for tid, paths in folded.items() if any((ticket_rules._touch_paths_overlap(path, item) for item in paths))))
        if len(sharing) > 1:
            internal[path] = sharing
    if internal:
        shown = sorted(internal.items())[:cluster_rules._CLUSTER_INTERNAL_OVERLAP_PATH_LIMIT]
        detail = ' · '.join((f'{path}: {', '.join(ids)}' for path, ids in shown))
        remainder = len(internal) - len(shown)
        suffix = f' 외 {remainder}개 경로' if remainder > 0 else ''
        if not lines:
            lines.append(cluster_binding._PUBLISH_OVERLAP_HEADER)
        lines.append(f'  클러스터 내부 겹침 {len(internal)}개 경로 ({detail}{suffix}) — 묶음 근거(반대 근거 아님)')
    return lines

def _cmd_cluster_new(home, args: argparse.Namespace) -> int:
    """장부 + 통합 브랜치 생성 · 멤버 귀속 · 겹침/슬롯 재료.

    거부 판정은 전부 첫 쓰기 앞에 둔다(부분 생성 없음). 발행이 만든 크기 1 장부는 흡수하고,
    이미 **여러 티켓의** 묶음에 속한 티켓은 옮기지 않는다 — 소속 이동은 사람 판정이라 엔진이
    조용히 하지 않는다.

    코드 트리는 **`--task` 가 보유한 폴더로만** 받는다(폴더가 2개 이상이면 `--repo/--slot` 위치
    인자로 고른다) — 이 명령은 git 브랜치를
    만들고 그 트리의 현재 브랜치를 기점으로 장부에 박는 쓰기라, 트리를 추측하면 엉뚱한
    저장소에 브랜치가 생기고 다른 묶음의 브랜치가 기점으로 실린다.
    """
    reason = cluster_rules._validate_cluster_name(args.name)
    if reason:
        print(f'[중단] {reason}', file=sys.stderr)
        return 1
    if task_args._claim_code_tree(home, args) is None:
        print('[중단] cluster new 는 통합 브랜치를 만들 코드 트리를 명시로 받는다 — `--task <task>`(task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가)를 지정하라.', file=sys.stderr)
        return 1
    identity = identity_args.parse_identity(args)
    cluster = cluster_rules.cluster_id_for_name(args.name)
    raw = (getattr(args, 'tickets', None) or '').strip()
    if not raw:
        print('[중단] cluster new 에는 `--tickets T-...,T-...` 가 필수다 — 묶음 선언은 PM 이 한다(엔진은 자동으로 묶지 않는다).', file=sys.stderr)
        return 1
    members: list[str] = []
    for item in raw.split(','):
        tid = item.strip()
        if not tid or tid in members:
            continue
        if not ticket_rules._is_valid_ticket_id(tid):
            print(f'[중단] 티켓 ID 형식이 아니다: {tid!r}', file=sys.stderr)
            return 1
        members.append(tid)
    if not members:
        print('[중단] `--tickets` 에 유효한 티켓 ID 가 없다', file=sys.stderr)
        return 1
    spike = (getattr(args, 'spike', None) or '').strip() or None
    branch = cluster_rules.cluster_branch_name(cluster)
    touched: list[Path] = []
    with fs.board_lock(home):
        ledger_path = cluster_store.cluster_ledger_path(home, cluster)
        if ledger_path.exists():
            print(f'[중단] 이미 있는 클러스터 장부: {pm_paths._rel_to_repo(home, ledger_path)} (멤버 조회는 `cluster show`)', file=sys.stderr)
            return 1
        resolved: list[tuple[str, Path, dict[str, Any], str, str | None]] = []
        drafts: list[str] = []
        for tid in members:
            found = ticket_store.find_ticket_exact(home, tid)
            if found is None:
                print(f'[중단] ticket not found: {tid}', file=sys.stderr)
                return 2
            status, path = found
            if status in cluster_rules._CLUSTER_MEMBER_REJECT_STATUSES:
                print(f'[중단] 종결 티켓은 클러스터 멤버가 아니다: {tid} ({status}/)', file=sys.stderr)
                return 1
            fm, body = ticket_store.load_ticket(path)
            owner = cluster_store._cluster_of_record(home, tid)
            if owner is not None and owner != cluster and (len(cluster_store.cluster_members(home, owner)) > 1):
                print(f'[중단] {tid} 는 이미 다른 묶음의 멤버다: {owner}(멤버 {len(cluster_store.cluster_members(home, owner))}) — 소속 이동은 엔진이 조용히 하지 않는다(옛 묶음을 먼저 정리하라).', file=sys.stderr)
                return 1
            if status == 'draft':
                drafts.append(tid)
            resolved.append((tid, path, fm, body, owner))
        tree, base_branch = _cluster_publish_base_branch(home, args)
        if tree is None:
            print(f'[중단] 코드 트리를 해소하지 못했다 — 정체성 (repo={identity.repo} · slot={identity.slot} · task={identity.task}) 이 리스 장부의 활성 슬롯과 조인되지 않는다. `pm_handoff`/`worktree_pool` 로 슬롯을 확인한 뒤 다시 실행하라.', file=sys.stderr)
            return 1
        branch_note = cluster_binding._cluster_ensure_branch(tree, branch, base_branch)
        touched.append(cluster_store.dump_cluster(home, cluster_rules._new_cluster_fm(cluster, members, base_branch=base_branch, branch=branch, spike=spike)))
        for tid, path, fm, body, owner in resolved:
            if str(fm.get('cluster') or '').strip() != cluster:
                fm['cluster'] = cluster
                ticket_store.dump_ticket(path, fm, body)
                touched.append(path)
            if owner is not None and owner != cluster:
                try:
                    released = _cluster_release_member(home, owner, tid)
                except OSError as exc:
                    print(f'[중단] 옛 장부에서 {tid} 를 빼지 못했다: {pm_paths._rel_to_repo(home, cluster_store.cluster_ledger_path(home, owner))}: {exc} — {cluster} 와 중복 귀속 상태다. 옛 장부를 정리한 뒤 `lint` 로 확인하라(새 장부·티켓 명세는 이미 갱신됐다).', file=sys.stderr)
                    return 1
                if released is not None:
                    touched.append(released)
    print(f'created {cluster} ({pm_paths._rel_to_repo(home, cluster_store.cluster_ledger_path(home, cluster))}) — 멤버 {len(members)}: {', '.join(members)}')
    print(f'  통합 브랜치 {branch} — {branch_note}')
    if spike:
        print(f'  설계 spike: {spike}')
    ready = board_sync._board_git_sync_best_effort(home, f'cluster new {cluster}', touched)
    if not ready:
        print('  ⚠ board-git 기록 보류: local-only/uncommitted', file=sys.stderr)
    if drafts:
        print(f'  ⚠ draft 멤버 {len(drafts)}건({', '.join(drafts)}) — 장부는 공유 board 에 있으나 draft 명세는 승격 전까지 공유되지 않는다(다른 클론에서 멤버 부재 advisory). `promote` 로 승격하라.', file=sys.stderr)
    material = _cluster_overlap_material(home, [(tid, fm.get('touches')) for tid, _path, fm, _body, _owner in resolved], identity.repo or home.root.name)
    for line in material:
        print(line, file=sys.stderr)
    return 0

def _cmd_cluster_show(home, args: argparse.Namespace) -> int:
    """장부 1건 조회 — 선언값 그대로 + 멤버의 현재 status.

    브랜치 실재는 재지 않는다 — 이 표면은 트리를 들지 않고, 장부에도 코드 트리를 적는 자리가
    없어(`CLUSTER_LEDGER_KEYS`) 어느 저장소에서 찾을지 지목할 수 없다.
    """
    cluster = cluster_rules.cluster_id_for_name(args.name)
    fm = cluster_store.load_cluster(home, cluster)
    if fm is None:
        print(f'cluster not found: {cluster}', file=sys.stderr)
        return 2
    declared = cluster_store.cluster_tickets(fm)
    status = str(fm.get('status') or cluster_rules.CLUSTER_STATUS_OPEN)
    print(f'-- {cluster} ({status} · 멤버 {len(declared)}) --')
    print(f'  base_branch: {fm.get('base_branch') or '—'}')
    print(f'  branch: {str(fm.get('branch') or '').strip() or '—'}')
    print(f'  spike: {fm.get('spike') or '—'}')
    budget = fm.get('budget') if isinstance(fm.get('budget'), dict) else {}
    print('  budget: ' + (' · '.join((f'{key}={value}' for key, value in budget.items())) or '—'))
    for tid in declared:
        found = ticket_store.find_ticket_exact(home, tid)
        if found is None:
            print(f'  {tid}  (부재)')
            continue
        member_status, path = found
        loaded = ticket_store.load_ticket_soft(home, path)
        title = str((loaded[0] if loaded else {}).get('title') or '')
        print(f'  {tid}  {member_status}  {title}')
    return 0

def cmd_cluster(home, args: argparse.Namespace) -> int:
    """`cluster new|show` — 고정 5단계 운영 단위 장부의 생성·조회."""
    leaf = getattr(args, 'cluster_cmd', '')
    if leaf == 'new':
        return _cmd_cluster_new(home, args)
    return _cmd_cluster_show(home, args)
