"""슬롯 장부와 live git 상태의 조회 결과 조립."""
from __future__ import annotations


from pm_engine.domain import worktree_records
from pm_engine.infra import branch_store, lease_store, pm_paths, pool_store, slot_snapshot, sparse_checkout, submodule_git, worktree_git, worktree_paths


def slot_status(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None) -> worktree_records.SlotStatus:
    """슬롯 worktree 의 상태(branch + upstream + submodule 역할)를 live 로 읽는다.

    부트스트랩이 현재 슬롯 상태를 1회 surface 하는 backbone.
    판별을 재사용**한다(중복 구현 금지):
      - `current_branch(slot)` — 브랜치(live·`symbolic-ref HEAD` full ref).
      - `_upstream_status` — `@{upstream}` 해소 여부.
      - `_submodule_statuses` — `git submodule status`(`_parse_submodule_entries`) + submodule 당
        `git -C <sub> symbolic-ref -q HEAD`(on-branch/detached) + `_submodule_dirty` 로 역할 판정
        (`_resync_submodules_selective` 와 *같은* primitive). detached & pin≠working=drift(경고)·
        on-branch=dev-ahead(정보)·detached & pin==working=pinned.

    **전부 fail-soft — 예외 raise 안 함**(형제 backbone `current_branch`·`_resync_submodules_
    selective`·`_rollback_worktree` 와 같은 규율·미래 호출부[on-demand 전체-풀 status·sync]가
    이 계약에 기댄다): `current_branch` 는 자체 흡수. upstream/submodule 은 rc≠0 이면 미해소/
    빈목록이고, `git_runner` 자체가 **예외를 던져도** 본문 try/except 로 흡수한다(upstream 미해소·
    submodule 빈목록으로 보수적 degrade — 각각 독립 흡수라 한쪽 실패가 다른쪽을 안 가린다).
    실 `_real_git_runner` 는 이미 예외를 (1, str) 로 감싸 rc≠0 경로로 오지만, 주입 runner
    (DI seam·테스트/미래 호출부)가 raise 해도 이 계약이 지켜진다. `git_runner` 미주입 시 슬롯
    worktree 바인딩 `_real_git_runner(slot_path(slot))` 로 해소(hermetic 테스트는 mock 주입).
    슬롯 경로 부재 가드는 **실경로(미주입)에서만** 본다 — 주입 runner 는 존재/rc/HEAD 를
    전부 모델링하는 권위이므로 fs 가드를 건너뛴다(`current_branch` 동형).
    """
    runner = git_runner
    if runner is None:
        path = pm_paths.slot_path(home, slot)
        if not path.exists():
            return worktree_records.SlotStatus(slot, branch=None, upstream=None, upstream_ok=False, submodules=[])
        runner = worktree_git._real_git_runner(path)
    branch = worktree_git.current_branch(home, slot, git_runner=runner)
    # upstream/submodule 조회는 runner 예외까지 흡수한다(docstring "예외 raise 안 함" 계약).
    # 각각 독립 try/except — 한쪽 raise 가 다른쪽 정보를 가리지 않게(부분 degrade).
    try:
        upstream, upstream_ok = submodule_git._upstream_status(runner)
    except Exception:  # noqa: BLE001 — fail-soft: runner raise → 미해소로 흡수(경고 surface).
        upstream, upstream_ok = None, False
    try:
        submodules = submodule_git._submodule_statuses(runner, target=slot)
    except Exception:  # noqa: BLE001 — fail-soft: runner raise → 빈목록(부트스트랩 줄 생략).
        submodules = []
    return worktree_records.SlotStatus(
        slot, branch=branch, upstream=upstream, upstream_ok=upstream_ok, submodules=submodules
    )


def _slot_runner(home, slot: str, git_runner: "GitRunner | None") -> "GitRunner | None":
    """주입 runner, 아니면 슬롯 실경로 runner(슬롯 폴더가 없으면 None)."""
    if git_runner is not None:
        return git_runner
    return worktree_git._real_git_runner(pm_paths.slot_path(home, slot)) if pm_paths.slot_path(home, slot).exists() else None


def slot_git_status(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None,
                    repo_reads: "dict | None" = None) -> dict:
    """슬롯 git 구성 조회 — branch·head·부모·자식·stash·폴더·dirty (`/pm-worktree status` 백본).

    반환 dict 키: `slot`·`branch`·`head`(live)·`parent`(`_slot_parent_status`)·`children`(작업 슬롯
    브랜치의 자식 브랜치 목록)·`stashes`(그 브랜치 이름 stash 수)·`folders`(cone 폴더 목록 또는
    None)·`dirty`(bool·`_is_dirty`). 슬롯 경로가 없어 runner 를 못 만들면 branch/head None·parent
    None·children []·stashes 0·dirty False. 부모·자식·stash 조회의 git 실패는 `BranchParentError`
    로 올린다. submodule 판정은 그것을 그리는 `status()` 가 붙인다.

    `repo_reads` 는 여러 슬롯을 한 번에 그리는 호출부가 만든 dict 하나다. 부모 기록 전체·stash 제목·
    부모 이름별 비교 ref 는 저장소 공용 값이라 저장소(대여 기록 행이 없으면 슬롯)마다 처음 한 번만
    묻고 다음 슬롯이 그 값을 쓴다. 주지 않으면 이 호출만의 새 dict 다. 파일에 남지 않는다."""
    runner = _slot_runner(home, slot, git_runner)
    if runner is None:
        return {"slot": slot, "branch": None, "head": None, "parent": None, "children": [],
                "stashes": 0, "dirty": False, "folders": None}
    lease = pool_store.read_lease(home, slot)
    readonly = lease is not None and lease.role == pool_store._LEASE_ROLE_READONLY
    repo_reads = {} if repo_reads is None else repo_reads
    shared = repo_reads.setdefault(lease.repo if lease is not None else slot, {"refs": {}})
    branch = worktree_git.current_branch(home, slot, git_runner=runner)
    work_branch = None if readonly else branch
    return {
        "slot": slot,
        "branch": branch,
        "head": slot_snapshot._slot_head(runner),
        "parent": branch_store._slot_parent_status(branch, lease, git_runner=runner, shared=shared),
        "children": (branch_store._children_of(work_branch, branch_store._shared_parent_records(shared, runner))
                     if work_branch else []),
        "stashes": (branch_store._branch_stash_count(branch, branch_store._shared_stash_subjects(shared, runner))
                    if branch else 0),
        "folders": sparse_checkout.slot_folders(home, slot, git_runner=runner),
        "dirty": worktree_git._is_dirty(pm_paths.slot_path(home, slot), git_runner=runner),
    }


def status(home, *, task: "str | None" = None, slot: "str | None" = None,
           git_runner: worktree_records.GitRunner | None = None) -> "list[dict]":
    """슬롯 git 구성 일괄 조회 — 단일 슬롯 / `--task` 전 슬롯 / 무인자=내 task 전 슬롯.

    대상 슬롯 해소(택일):
      - `slot` 명시 → 그 슬롯 하나(`_normalize_slot` 형식 검증·traversal 차단).
      - `task` 명시 → `slots_for_task(task)`(session==task 이고 leased 인 슬롯).
      - 둘 다 생략 → 내 task 전 슬롯(`slots_for_task(_default_session())` — env/단일-lease 유입
        세션 정체성이 보유한 leased 슬롯· "무인자=내 task 전체").
    각 슬롯을 `slot_git_status`(branch·head·부모·자식·stash·폴더·dirty)로 조회하고 `role`(work/
    readonly)과 `submodules`(submodule pin/drift·`_submodule_statuses`)를 얹어 슬롯별 dict 리스트로
    돌려준다. 손-git 불요·조회 전용
    (부작용 0).

    `git_runner` 주입 시 그 runner(테스트 hermetic — 모든 슬롯 공용)·미주입이면 슬롯별 실 runner
    (`slot_git_status` 가 슬롯 worktree 바인딩)."""
    if slot is not None:
        slots = [worktree_paths._normalize_slot(home, slot)]
    elif task is not None:
        slots = [l.slot for l in pool_store.slots_for_task(home, task)]
    else:
        slots = [l.slot for l in pool_store.slots_for_task(home, lease_store._default_session(home))]
    rows: list[dict] = []
    repo_reads: dict = {}
    for s in slots:
        row = slot_git_status(home, s, git_runner=git_runner, repo_reads=repo_reads)
        row["role"] = pool_store._slot_role(home, s)
        runner = _slot_runner(home, s, git_runner)
        row["submodules"] = [] if runner is None else submodule_git._submodule_statuses(runner, target=s)
        rows.append(row)
    return rows
