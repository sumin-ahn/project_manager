"""묶음 브랜치와 대여 기록에 따른 코드 트리 해소 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import finish_rules, identity_args
from pm_engine.infra import cluster_store, finish_git, lease_store, pm_paths


def resolve_code_tree(home, cluster: str, *, run_git_stdout_at=None) -> tuple[Path | None, str | None]:
    """이번 종결이 도는 작업 트리 — `(트리, None)` 또는 `(None, 멈춤 사유)`.

    명시 묶음 기록의 `branch`를 체크아웃한 작업 트리가 **정확히
    하나**일 때만 그 트리다. 찾는 곳은 대여 기록 행의 작업 폴더 가운데 git 작업 트리인 것이
    속한 저장소의 작업 트리 전부다(이미 목록에 나온 폴더는 다시 묻지 않는다). PM 홈 자신은
    찾는 곳이 아니다 — 홈이 작업 폴더와 같은 저장소일 때만 그 목록에 들어 후보가 된다.
    찾은 트리가 작업 폴더면 그 행의 경로(`PM 홈/<slot>`)로 넘긴다 — git 목록은 실경로를 내므로
    심링크 슬롯을 실경로로 넘기면 슬롯 좌표(`work/<repo>_<N>/` touches)를 풀 수 없다.

    task 정체성은 보지 않는다 — 누가 그 트리를 빌렸는지는 작업 트리 선택의 입력이
    아니다(소유 확인은 멤버 `claimed_by` 대조가 따로 한다). "한 브랜치는 한 트리" 는 한
    저장소 안에서만 성립하고 우회도 있으므로 없음·둘 이상은 고르지 않고 멈춘다. 대여 기록
    판독 불능·목록 조회 실패는 관측 실패(정지)다.
    """
    runner = run_git_stdout_at or finish_git._default_run_git_stdout_at
    ledger = cluster_store.load_cluster(home, cluster)
    raw = (ledger or {}).get("branch")
    key, branch = "branch", (raw.strip() or None) if isinstance(raw, str) else None
    if branch is None:
        return None, (f"{cluster}는 제품 브랜치 미결속 장부다. 기존 "
                      "`board.py cluster new <ticket-group> --tickets <티켓들> --task <task>`"
                      "(task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가)으로 명시 재결속하라")
    ledger_status, rows = lease_store._read_lease_rows_status(pm_paths.leases_file(home))
    if ledger_status == identity_args.LEDGER_UNREADABLE:
        raise finish_rules._CloseObservationFailure(
            f"대여 기록을 읽지 못했다 ({pm_paths.leases_file(home)}) — 작업 트리를 찾을 곳을 정할 수 없다.")
    # 작업 폴더(실경로 → 대여 행 경로) — git 작업 트리인 것만, 대여 기록 순서대로.
    folders: dict[Path, Path] = {}
    for row in rows:
        slot = str(row.get("slot") or "").strip()
        folder = home.local_dir.parent.parent / slot
        if slot and (folder / ".git").exists():
            folders.setdefault(folder.resolve(), folder)
    target = f"refs/heads/{branch}"
    listed: set[Path] = set()
    holders: list[Path] = []
    for resolved_folder, folder in folders.items():
        if resolved_folder in listed:
            continue
        for tree, ref in finish_git._worktree_list(folder, run_git_stdout_at=runner):
            resolved = tree.resolve()
            if resolved in listed:
                continue
            listed.add(resolved)
            if ref == target:
                holders.append(folders.get(resolved, tree))
    if not holders:
        return None, finish_rules._CLOSE_CODE_TREE_MISSING.format(
            key=key, branch=branch, folders=len(folders))
    if len(holders) > 1:
        return None, finish_rules._CLOSE_TREE_AMBIGUOUS.format(
            branch=branch, count=len(holders), trees=", ".join(map(str, holders)))
    print(f"  작업 트리 → {holders[0]} (묶음 기록 {key} {branch})")
    return holders[0], None
