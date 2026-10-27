"""슬롯 생성·대여·반납·제거 작업 조정."""
from __future__ import annotations

import sys

from pm_engine.domain import worktree_records
from pm_engine.infra import branch_store, clock, fs, lease_store, pm_paths, pool_store, slot_snapshot, sparse_checkout, worktree_git, worktree_paths
from pm_engine.app import branch_checkout


def alloc(home, 
    repo: str,
    *,
    owner_task: str,
    branch: str | None = None,
    resume: str | None = None,
    git_runner: worktree_records.GitRunner | None = None,
    folders: list[str] | None = None,
) -> worktree_records.Lease:
    """repo 슬롯을 task 명의로 리스한다.

    항상 **신규 idle 슬롯을 대여**한다 — 같은 task 가 같은 repo 슬롯을 이미 보유해도 조용히 그
    슬롯을 재반환(silent aliasing)하지 않고 다른 idle 슬롯(min-number)을 대여해 **같은 repo 복수
    보유**(병렬 dev 격리 등)를 자연 지원한다. lease.session 은 owner_task 로 기록된다.
    `owner_task` 는 필수다 — 세션 명의 대여(같은 세션 재대여·브랜치 재부착)는 없다
    (`pm-config alloc` 은 `--task` 를 요구한다).

    - **idle 슬롯 리스** — idle 슬롯을 leased 로 전이(필요 시 branch checkout).
    - **풀 소진 → `NeedsCreate`** — idle 슬롯이 없으면 raise 한다(호출부 `pm-config alloc` 이
      사용자 승인 명령을 안내한다).

    자동 회수는 없다 — 종료 기록 없이 남은 슬롯은 사용자가 `release --force` 로 닫는다.
    `branch` 와 `resume` 은 동의어 역할(둘 다 작업스트림 식별) — 명시된 쪽을 쓴다.
    """
    if not owner_task:
        raise ValueError("alloc: owner_task(task 명의)가 필요하다 — 세션 명의 대여는 없다")
    if folders is not None:
        folders = sparse_checkout._normalize_folders(folders, target=f"repo {repo!r}")
    sess = owner_task
    target_branch = branch if branch is not None else resume

    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)

        def _finalize(lease: worktree_records.Lease, *, write_ledger: bool) -> worktree_records.Lease:
            if write_ledger:
                pool_store._write_ledger(home, leases)
            return lease

        # 3) idle 슬롯 리스 — **최소 번호 우선**(결정론 ⓒ·번호 안정). 같은 repo 의 idle
        #    후보를 슬롯 번호 오름차순으로 정렬해 최소 가용 번호부터 대여한다(대여 중 불변·반납 후
        #    재사용). 옛 코드는 장부 파일 순서(대개 생성순이나 remove+재생성 후 뒤섞일 수 있음)로
        #    첫 idle 을 골라 번호가 비결정적이었다 — 정렬로 못박아 alloc CLI 의 "최소 번호 대여"를
        #    보장한다. 비-숫자 tail 슬롯(드문 커스텀)은 뒤로 밀어 숫자 슬롯 우선(`_slot_number`).
        idle_for_repo = sorted(
            (l for l in leases if l.repo == repo and l.state == "idle"),
            key=lambda l: worktree_records._slot_number(repo, l),
        )
        for lease in idle_for_repo:
            # **위험차단 (must-fix)**: worktree 물리 부재 슬롯은 리스하지 않는다. stale
            # 엔트리(worktree dir 삭제/prune)가 force_release 등으로 idle 이 되면, 이 재사용
            # 루프가 *없는 worktree* 를 leased 로 넘겨 이후 코드가 깨진다(codex 실측). fs 존재
            # 가드는 **실경로(git_runner 미주입)에서만** 본다 — 주입 runner(hermetic 테스트)는
            # 슬롯 존재를 모델링하는 권위라 건너뛴다(current_branch/slot_status 동형 규율). 부재
            # 슬롯은 skip → 다음 후보/NeedsCreate(dangling idle 을 leased 로 승격하지 않음).
            if git_runner is None and not pm_paths.slot_path(home, lease.slot).exists():
                continue
            # 지정 폴더의 검증은 현재 슬롯을 바꾸기 전에 완료한다. 실재는 도착할 ref에서 읽는다.
            if folders is not None:
                runner = git_runner or worktree_git._real_git_runner(pm_paths.slot_path(home, lease.slot))
                sparse_checkout._require_sparse_git(runner, target=lease.slot)
                sparse_checkout._require_tree_folders(folders, target_branch or "HEAD", runner, target=lease.slot)
                sparse_checkout._require_clean_for_folders(home, lease.slot, runner)
            # 슬롯이 이미 target_branch 가 아니면 재체크아웃(live HEAD 비교). 없는 브랜치는 만들지
            # 않고 CheckoutFailed — 장부엔 branch 를 쓰지 않는다.
            if (target_branch is not None
                    and worktree_git.current_branch(home, lease.slot, git_runner=git_runner) != target_branch):
                # checkout 을 먼저 — 실패하면 raise(idle 슬롯 상태 보존·부분 leased 전이 차단).
                branch_checkout._checkout_required(home, lease.slot, target_branch, git_runner=git_runner)
            branch_checkout._apply_slot_folders(home, lease.slot, folders, git_runner=git_runner)
            lease.state = "leased"
            lease.session = sess
            lease.started = clock.now_utc()
            # pool 대여 — 사람 bind 마커 해제.
            lease.bound = False
            slot_snapshot._apply_git_snapshot(home, lease, git_runner=git_runner)  # arrival 스냅(기대 baseline·base 보존).
            return _finalize(lease, write_ledger=True)

        # 4) 풀 소진 — idle 슬롯 없음. 새 슬롯 생성은 fs 행위라 사용자 게이트(호출부).
        raise worktree_records.NeedsCreate(repo)


def release(home, 
    slot: str,
    *,
    owner_task: str | None = None,
    git_runner: worktree_records.GitRunner | None = None,
) -> worktree_records.Lease:
    """슬롯을 반납한다 — 작업완료 시. idle 로 전이한 Lease 반환.

    - **커밋 안 한 변경이 있으면 `ReleaseRefused`** — 엔진은 stash 하지 않는다(사용자가 커밋하거나
      요청한 stash 로 치운 뒤 다시 반납·작업 유실 방지).
    - **owner_task 주면 소유검사** — 그 슬롯의 leased session 이 owner_task 와 다르면
      `NotTaskOwner`(다른 task 의 슬롯 반납 차단). 검사는 dirty 판정보다 먼저(내 것이 아니면
      dirty 여부를 볼 이유가 없다). `--task` 미지정(owner_task=None·slot-only 백스톱)은 안 탄다.

    슬롯은 idle 로 전이(재사용 컨테이너로 풀에 반납)하고 session 을 비운다(=종료 기록) —
    worktree 폴더 자체는 유지(다음 리스가 재사용·remove 는 force_release/수동).
    """
    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            raise KeyError(f"no lease for slot {slot!r}")

        # readonly 공유 슬롯은 반납(idle 화) 대상이 아니다 — 무소유 공유 자산이라
        # release 하면 idle 이 돼 alloc 이 work 슬롯으로 점유(role 유실). 보유 중인 lease.role 을 직접
        # 검사한다(`_reject_readonly_mutation`/`_slot_role` 은 lock 재취득 → non-reentrant 데드락).
        if target.role == pool_store._LEASE_ROLE_READONLY:
            raise pool_store.ReadonlySlotNotLeasable(slot, "release")

        # task 소유검사 — dirty 판정보다 먼저. 내 task 명의(session)가 아니면
        # 아무것도 바꾸지 않고 raise(다른 세션 작업을 실수로 idle 화하지 않음).
        if owner_task is not None and target.session != owner_task:
            raise worktree_records.NotTaskOwner(slot, owner_task, target.session)

        path = pm_paths.slot_path(home, slot)
        if path.exists() and worktree_git._is_dirty(path, git_runner=git_runner):
            raise worktree_records.ReleaseRefused(slot)

        target.state = "idle"
        target.session = ""       # 종료 기록 — 이후 `lease_is_open` 은 닫힘으로 읽는다.
        target.git = None    # release 시 정리 — idle 슬롯은 활성 git 기대가 없다(다음 alloc 이 재스냅).
        target.bound = False  # 명시 반납 = bind 점유 종료 — 사람 bind 마커 해제(git=None 동형 teardown).
        pool_store._write_ledger(home, leases)
        return target


def force_release(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None) -> worktree_records.Lease | None:
    """수동 백스톱 — 점유(leased)·소유 여부를 무시하고 슬롯을 강제로 idle 화.

    커밋 안 한 변경이 있으면 force 여도 `ReleaseRefused` 다(stash 하지 않는다). 장부에 슬롯이
    없으면 None 반환(이미 정리됨·무해). `pm-config release --force` 백스톱의 엔진 진입점.
    """
    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            return None
        # readonly 공유 슬롯은 강제 반납도 대상이 아니다 — 무소유
        # 공유 자산이라 idle 화하면 alloc 이 work 슬롯으로 점유(role 유실). 보유 lease.role 직접 검사.
        if target.role == pool_store._LEASE_ROLE_READONLY:
            raise pool_store.ReadonlySlotNotLeasable(slot, "force-release")
        path = pm_paths.slot_path(home, slot)
        if path.exists() and worktree_git._is_dirty(path, git_runner=git_runner):
            raise worktree_records.ReleaseRefused(slot)
        target.state = "idle"
        target.session = ""       # 종료 기록 — 사용자가 강제로 닫는 경로(엔진 자동 회수 없음).
        target.git = None    # release 시 정리(force 백스톱도 동일·다음 alloc 이 재스냅).
        target.bound = False  # 강제 반납도 bind 점유 종료 — 사람 bind 마커 해제(release 동형).
        pool_store._write_ledger(home, leases)
        return target


def remove_slot(home, 
    slot: str,
    *,
    force: bool = False,
    acked_branches: "frozenset[str]" = frozenset(),
    git_runner: worktree_records.GitRunner | None = None,
) -> "RemoveResult | None":
    """슬롯을 *통째로* 제거한다 — worktree remove + 브랜치 삭제 판정 + 장부 엔트리 삭제.

    `create_slot` 의 역연산 — 슬롯 lifecycle 에서 *제거 본체*가 수동 `git worktree remove`
    위임이던 gap 을 닫는다(PM 69 footgun 체인: 수동 remove → dangling 장부 → `add` 가 번호
    skip → 뒤늦은 prune). 이 한 커맨드가 리스 확인 → 등록 조회 → 커밋 안 한 변경 검사 →
    `git worktree remove --force`(+ `git worktree prune`) → 브랜치 삭제 판정 → 장부 엔트리 제거를 잇는다.

    **사용자 승인값 전제** — 슬롯 제거는 사용자 승인 행위다. 슬롯 승인값(`--user-ack <슬롯>`)은
    호출부 CLI(`pm-config worktree remove`)가 엔진을 부르기 전에 확인하고, 합쳐지지 않은 브랜치를
    지워도 된다는 승인값만 `acked_branches` 로 넘어온다. `prune-stale`(worktree 부재 장부만 정리)과
    달리 실 worktree 를 지운다. orphan worktree(장부 미등록)는 여전히 `git worktree remove`.

    시퀀스:
      리스/장부 확인 — 엔트리 없으면 `None` 반환(무해 종료·이미 정리됨·orphan 은 위 참조).
      활성 리스 확인 — `state != "idle"`(leased/creating·사용 중)이면 `RemoveRefused`
         ("active-lease"). **`force=True` 가 무시하는 것은 이 점유 하나뿐이다**(원래 state 를
         `RemoveResult.forced_state` 에 실어 CLI 가 강제-회수 경고).
      **슬롯 트리 부재** — 지울 디렉터리가 애초에 없으면 `prune` + 장부 정리로 끝낸다
         (dangling 장부 회수·브랜치 판정 없음 — 그 트리에서 브랜치를 물을 수 없다).
      **등록 조회**(`_worktree_registered`) — git 이 이 경로를 worktree 로 아는가. **모르는데
         디렉터리가 있으면** 두 갈래다: pool 슬롯 자리가 아니면(PM 홈 자신을 가리키는 행 등)
         지울 자리가 아니므로 파일시스템을 건드리지 않고 `RuntimeError`. pool 슬롯 자리면 git
         이 등록만 지우고 남긴 잔재이지만 그 트리의 커밋 안 한 변경을 물을 곳이 없으므로 force 와
         무관하게 `RemoveRefused`("unregistered-tree", path=)로 거부한다 — 파일 삭제는 사용자
         몫이다(직접 확인·삭제 뒤 `pm-config worktree prune-stale` 이 장부를 정리한다).
      **커밋 안 한 변경** — `_is_dirty` 면 force 와 무관하게 `RemoveRefused`("dirty"). 하위 모듈
         안의 미추적·수정 파일도 `git status --porcelain` 이 ` M <하위 모듈>` 로 잡는다(실측 git 2.43).
      `git worktree remove --force <path>`(+ `git worktree prune`) — `.repos/<repo>.git` bare
         컨텍스트(공유 .git 원). 변경 없음을 엔진이 위에서 확인했으므로 `--force` 가 날리는 작업은
         없다. `--force` 가 필요한 이유는 하위 모듈이 있는 슬롯이다 — 변경이 없어도 `--force` 없이는
         rc 128("하위 모듈이 포함된 작업 폴더는 옮기거나 제거할 수 없습니다")이다(실측 git 2.43).
         실패(rc≠0)면 **등록을 다시 물어** 사실대로 갈린다: 등록이 남아 있으면 아무것도 파괴되지
         않았으므로 `RuntimeError`(git 메시지 그대로), 등록이 사라졌으면 git 이 판정을 통과시킨 뒤
         트리만 못 지운 것이라 `force_rmtree` 로 마저 지우고 계속한다(읽기 전용 잔재 실측 형상).
      브랜치 — `delete_branch_by_rule`(보호·기본 브랜치·자식·부모 기록·합쳐짐·승인값·끝 커밋
         재확인)의 결과를 `RemoveResult.branch_deletion` 에 싣는다. detached 면 판별 불가(None).
         보존이면 `record_preserved_branch` 로 쓰던 슬롯·보존 시각·사유를 공용 git 설정에 기록한다
         (쓰기 실패는 `BranchParentError`·장부 행은 남는다). 보호·기본 브랜치 보존은 기록하지 않는다 —
         슬롯 수명과 무관한 브랜치다(`_UNRECORDED_PRESERVED_REASONS`).
      장부 엔트리 제거 — `add` 가 빈 번호를 **재사용**(번호 skip footgun 종결·이 티켓 핵심).

    ⚠️ **보존 브랜치 상호작용**: 합쳐지지 않았거나 판정할 수 없어 전용
    브랜치 `<repo>_<N>` 를 보존하면, 나중에 같은 번호의 슬롯을 재생성할 수 없다(`-b <repo>_<N>`
    대상 브랜치가 이미 존재). create_slot 은 이를 **선-검출해 fail-loud**(`SlotBranchExists`) —
    정확한 원인(브랜치 잔존) + 두 갈래(그 브랜치 정리[머지/삭제] 후 새 슬롯 재생성 / 다른 슬롯에서
    `switch` 로 그 브랜치를 재개·리셋 없음)를 안내한다. 데이터 유실 없음.

    **트리 실재는 파일시스템이, 등록은 git 이 답한다** — 지울 디렉터리가 있는지는 fs 가
    유일한 진실이라 주입 runner 여부와 무관하게 본다(hermetic 테스트도 슬롯 디렉터리를 세운다).
    `cmd_release`/`cmd_worktree_prune_stale` 패턴대로 `git_runner` 주입으로 실 git 없이 배선
    검증(DI seam).
    """
    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)
        target = next((l for l in leases if l.slot == slot), None)
        # 리스/장부 확인 — 엔트리 없으면 무해 종료(orphan 은 git worktree remove·prune-stale).
        if target is None:
            return None

        # 활성 리스 확인 — leased/creating(사용 중·in-flight)은 force 로만(release 먼저가 정석).
        # force override 면 원래 활성 state 를 실어 CLI 가 강제-회수 경고를 낸다(reviewer should-fix).
        forced_state = target.state if target.state != "idle" else None
        if forced_state is not None and not force:
            raise worktree_records.RemoveRefused(slot, "active-lease", state=forced_state)

        path = pm_paths.slot_path(home, slot)
        bare = pm_paths.bare_repo_path(home, target.repo)
        bare_runner = git_runner or worktree_git._real_git_runner(bare)

        def _drop_ledger_row() -> None:
            """장부 엔트리 제거 — `add` 가 빈 번호를 재사용(락 보유 구간 안)."""
            leases[:] = [l for l in leases if l.slot != slot]
            pool_store._write_ledger(home, leases)

        # 슬롯 트리 부재 — 지울 디렉터리가 없으면 파괴할 것도, 등록을 물을 이유도 없다.
        # prune 이 남은 등록을 회수하고 장부만 정리한다(dangling 회수·prune-stale 와 겹치는 방어).
        if not path.exists():
            bare_runner(["worktree", "prune"])
            _drop_ledger_row()
            return worktree_records.RemoveResult(slot=slot, branch=None, branch_deletion=None,
                                tree_cleaned=False, forced_state=forced_state)

        # 등록 조회 — git 이 이 경로를 worktree 로 아는가(파괴 판단의 입력·변경 검사보다 먼저).
        # 모르는데 디렉터리가 있으면 git 에게 커밋 안 한 변경을 물을 수 없다(그 자리는 이미
        # worktree 가 아니다). 그 트리를 지우면 미커밋·미추적 내용이 그대로 사라지므로 엔진은
        # 지우지 않는다 — pool 슬롯 자리면 사용자에게 경로를 넘기고, 아니면(PM 홈 자신을 가리키는
        # 행 등) 지울 자리도 아니다.
        if not worktree_git._worktree_registered(home, path, git_runner=bare_runner):
            if not worktree_paths._is_pool_worktree_slot(home, slot):
                raise RuntimeError(
                    f"slot {slot!r} 은 git worktree 등록이 없고 그 경로({path})는 pool 슬롯 자리도 "
                    "아니다 — 지울 등록도 지울 자리도 없어 아무것도 하지 않았다(장부/트리 미변경)."
                )
            raise worktree_records.RemoveRefused(slot, "unregistered-tree", path=path)

        # 커밋 안 한 변경 — force 와 무관하게 거부(엔진은 stash 하지 않는다).
        if worktree_git._is_dirty(path, git_runner=git_runner):
            raise worktree_records.RemoveRefused(slot, "dirty")

        # 브랜치 판별은 worktree remove *전에* — 제거 후엔 슬롯 HEAD 조회 불가.
        branch = worktree_git.current_branch(home, slot, git_runner=git_runner)

        # git worktree remove --force (+ prune) — bare 컨텍스트(공유 .git 원·_rollback_worktree 동형).
        # 변경 없음은 위에서 확인했다 — `--force` 는 하위 모듈이 있는 깨끗한 슬롯 때문이다. 실패는
        # 등록을 다시 물어 갈린다: 등록이 남아 있으면 아무것도 파괴되지 않았으므로 raise, 등록이
        # 사라졌으면 git 이 트리만 못 지운 것이라(읽기 전용 잔재 실측) 잔재를 마저 지우고 계속한다.
        rc, out = bare_runner(["worktree", "remove", "--force", str(path)])
        tree_cleaned = False
        if rc != 0:
            if worktree_git._worktree_registered(home, path, git_runner=bare_runner):
                raise RuntimeError(
                    f"git worktree remove failed for {slot!r} (rc={rc}, out={out!r}) — "
                    "git 등록·장부·브랜치가 그대로다."
                )
            tree_cleaned = path.exists()
            fs.force_rmtree(path)
        bare_runner(["worktree", "prune"])  # best-effort 등록 정리(remove 후 dangling admin).

        # 브랜치 — 브랜치 수명은 슬롯 수명이다. 삭제 여부는 공용 판정 규칙이 정한다.
        # 보존하면 슬롯이 사라진 뒤에도 어느 슬롯에서 왜 남았는지 알 수 있게 기록을 남긴다.
        branch_deletion = None
        if branch is not None:
            branch_deletion = branch_store.delete_branch_by_rule(home, 
                branch, repo=target.repo, acked_branches=acked_branches, git_runner=bare_runner)
            if (branch_deletion.action == worktree_records.BRANCH_PRESERVED
                    and branch_deletion.reason not in worktree_records._UNRECORDED_PRESERVED_REASONS):
                branch_store.record_preserved_branch(branch, slot=slot, reason=branch_deletion.reason,
                                        git_runner=bare_runner)

        _drop_ledger_row()
        return worktree_records.RemoveResult(slot=slot, branch=branch, branch_deletion=branch_deletion,
                            tree_cleaned=tree_cleaned, forced_state=forced_state)


def create_slot(home, 
    repo: str,
    *,
    base: str,
    owner_task: str | None = None,
    init_submodules: bool = True,
    git_runner: worktree_records.GitRunner | None = None,
    test_cmd: str | None = None,
    readonly: bool = False,
    folders: list[str] | None = None,
) -> worktree_records.Lease:
    """새 슬롯을 *생성*한다 — 풀 확장 (NeedsCreate 게이트 통과 후).

    확정 상태는 세 갈래다: task 명의 생성은 그 task 명의 `leased`, `readonly` 는 무소유
    공유 슬롯(leased·session 없음), 그 밖은 **무소유 `idle`**(풀에 들어가는 슬롯 —
    점유는 `alloc` 이 한다).

    **task-명의 생성 (`owner_task` 주어짐)**: 풀 소진 시 `worktree add <repo> --task
    <task>` 이 새 슬롯을 만들고 **생성 직후 그 슬롯을** task 명의로 대여한다(min-idle 재탐색의
    오슬롯 리스크 없이 생성분 직결). lease.session=owner_task. owner_task 는 기바인딩 task 요구(cmd 층 `find_task`)라 항상 tasks
    장부에 있다. readonly 와는 상호배타(readonly=무소유 공유 슬롯). owner_task 미지정은 무소유
    idle pool 슬롯이다(provisional 마커도 무소유).

    `test_cmd` 가 주어지면 그 슬롯 리스에 회귀/빌드명령을 바인딩한다(
    amend) — 같은 repo 의 슬롯들이 서로 다른 빌드 타깃(HIL config 등)을 가질 수 있게.
    regression._test_cmd 가 활성 슬롯의 이 필드를 areas 위 레이어로 읽는다(미지정=None·현행).

    `git worktree add` 는 fs 행위라 사용자 게이트(NeedsCreate) 통과 후에만 불린다 —
    pm-config worktree add 사용자 승인이 호출부. 다음을 한다:
      0. **base 필수** — repo 등록 base 가 없으면(None·빈 값) 부작용 전에 `ValueError`.
      1. **bare 부재 가드** — `.repos/<repo>.git` 가 없으면 `BareRepoMissing` raise(multi-PM
         worktree 침묵 폴백 금지).
      2. 다음 슬롯 번호 결정(`<repo>_<N>`·기존 번호 회피).
      3. `git worktree add --no-track -b <repo>_<N> <path> <ref>` — **`.repos/<repo>.git` bare
         컨텍스트**에서 실행해 슬롯이 그 family repo 의 worktree 가 되게 한다. 먼저 `git fetch
         origin`(best-effort) 후 슬롯 브랜치 `<repo>_<N>` 를 *`origin/<base>` 최신에서 파생*한다.
         repo 등록 base(areas.md·`pm-config worktree add` 가 전달)에서 일관되게 따게 한다 — bare
         HEAD 가 아닌 의도한 base(develop 등). fetch 실패/origin ref 미해소면 로컬 `<base>`(동결
         head) 폴백(refspec 은 origin/* 만 갱신·로컬 heads 는 동결·fail-soft). `--no-track` = 슬롯
         브랜치에 origin/<base> upstream 자동설정 억제(슬롯=작업스트림).
         add 직후 **부모 기록** — `<repo>_<N>` 의 부모 = base, 갈라진 커밋 = 파생 ref 의 커밋
         (`record_branch_parent`). 실패는 아래 롤백을 탄다.
      4. submodule init — `git worktree add` 는 submodule 자동 init 안 함 → `git submodule update --init --recursive --force`(슬롯 worktree cwd).
         `--force` 는 worktree+submodule edge(bare 에서 만든 fresh 슬롯)서 plain `--init` 이
         체크아웃 못 하는 상태를 강제 init — fresh 슬롯이라 잃을 로컬 변경 0.
      5. 장부 엔트리 확정(위 세 갈래 — task 명의 leased · readonly 무소유 leased · 그 밖 idle).

    **`readonly=True`**: research 전용 read-only 공유 슬롯을 만든다 —
    슬롯 전용 브랜치를 파지 않고 `git worktree add --detach <path> <base sha>` 로 **detached
    HEAD** 로 만든다(`git worktree add <path> main` 은 같은 브랜치를 두 worktree 가
    점유 못 해 `fatal: 'main' is already used by worktree at …` 로 죽는다 → `--detach` 필수).
    `base` = released `main` 의 기준면(문서 검증 기준·released 지점). lease 는 `role="readonly"`·
    **session 없음**(공유 자산·배타 대여 안 함)·alloc/release 대상 제외(공유가 정상).
    브랜치가 없으므로 따라갈 부모 브랜치는 스냅(`lease.git.base`)에 기록한다(refresh 가 그 최신으로 이동).

    git_runner 가 주입되면 그 runner 로 모든 git 호출(테스트 hermetic). 미주입이면
    `.repos/<repo>.git` bare 컨텍스트의 실 git 으로 worktree add 후, 슬롯 경로 컨텍스트로
    submodule init.
    """
    if folders is not None:
        if readonly:
            raise ValueError("create_slot: readonly 와 folders 는 함께 쓸 수 없다")
        folders = sparse_checkout._normalize_folders(folders, target=f"repo {repo!r}")
    # task-명의 생성이면 session=task 이름(bound 아님). owner_task 미지정이면 provisional 마커도 무소유(빈 session)이고 확정은 무소유 idle 이다.
    task_created = owner_task is not None
    # owner_task + readonly 는 모순(무소유 공유 자산 vs task 명의 배타 대여) — CLI 가드만 믿지 않고
    # 엔진 자체에서 fail-loud(codex suggestion·타 호출부/미래 경로 방어). 부작용(worktree add) 이전.
    if task_created and readonly:
        raise ValueError(
            "create_slot: owner_task 와 readonly 는 상호배타다 — readonly 공유 슬롯은 무소유"
            "(session 없음·배타 대여 없음)라 task 명의 대여 대상이 아니다."
        )
    if not base:
        raise ValueError(
            f"create_slot: repo {repo!r} 등록에 base 브랜치가 없다 — 슬롯 브랜치를 어느 부모에서 팔지 "
            "정할 수 없다. areas.md 의 그 repo base 를 채운 뒤 다시 실행하라."
        )
    sess = owner_task or ""

    # bare 부재/무효 가드 — worktree 의 공유 .git 원이 없거나 무효면 base 가 없다.
    # 침묵 폴백(multi-PM 루트 worktree)으로 가면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해
    # 토폴로지가 깨진다 → 명시 raise 로 `pm-config repo add` 선행 안내(fail-soft 규율).
    #   1) 경로부재 → BareRepoMissing (종전·hydrate 안내).
    #   2) 경로존재 but 무효(부분/깨진 bare) → BareRepoMissing(broken=True). 중단된 clone 이
    #      남긴 부분 bare 는 exists()=True 지만 worktree add 의 base 로 못 써 날 git 에러로 죽는다
    #      (audit #1→#4). `_is_valid_bare`(rev-parse)로 실 bare 를 판정해 그 조용한 통과를 fail-loud
    #      진단으로 닫는다. 주입된 git_runner(테스트 mock)도 같은 가드를 거친다 — mock 이 rev-parse
    #      로 유효/무효 bare 를 모델링(DI seam 보존·실 git 안 탐).
    bare = pm_paths.bare_repo_path(home, repo)
    if not bare.exists():
        raise worktree_records.BareRepoMissing(repo, bare)
    if not worktree_git._is_valid_bare(bare, runner=git_runner or worktree_git._real_git_runner(bare)):
        raise worktree_records.BareRepoMissing(repo, bare, broken=True)

    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)
        # 슬롯번호 = **ledger ∪ 실 git worktree** 병합 (audit #4). ledger 만 보면 orphan
        # (worktree add 성공 후 lease 기록 전 죽어 disk 엔 있으나 ledger 엔 없는 슬롯·audit #2)
        # 번호를 재사용해 `git worktree add` "already exists" 암호 에러가 난다. git worktree 실측을
        # 합쳐 orphan 번호까지 회피한다(주입 runner 존중·DI seam). git 조회 실패는 fail-soft(빈 집합).
        used = worktree_records._existing_slot_numbers(repo, leases) | worktree_git._git_slot_numbers(home, repo, git_runner=git_runner)
        n = 1
        while n in used:
            n += 1
        slot = worktree_records._slot_for(repo, n)
        path = pm_paths.slot_path(home, slot)

        # **provisional lease 선기록 (중단-안전)** — worktree add *전에* `state="creating"`
        # lease 를 장부에 넣어 disk 에 흔적을 남긴다. 성공 시 `leased` 로 확정하고, 실패/예외/
        # KeyboardInterrupt 는 아래 try/except 가 fs 롤백 + provisional 제거로 청소한다. **SIGKILL
        # (kill -9)** 은 except/finally 가 안 돌지만 이 provisional 이 disk 에 남아 다음
        # status/reconcile 이 incomplete 로 surface(→ 사용자 정리) — finally-rollback 만으론 못
        # 잡던 SIGKILL 을 provisional 이 커버한다(결정 §provisional 우선). 슬롯번호도 이 엔트리로
        # 예약된다(`_existing_slot_numbers` 가 세므로 후속 create_slot 이 재사용 안 함).
        provisional = worktree_records.Lease(
            slot=slot, repo=repo, session=sess,
            started=clock.now_utc(), state="creating", test_cmd=test_cmd,
        )
        # 폴더 검사는 creating 장부 쓰기보다 먼저 완료한다. 전체 생성의 기존 순서는 유지한다.
        provisional_recorded = folders is None
        if provisional_recorded:
            leases.append(provisional)
            pool_store._write_ledger(home, leases)

        # add 성공(=fs 에 worktree 생성) 여부 — except 청소가 롤백(remove) 대상을 판정한다. add 자체
        # 실패면 지울 worktree 가 없어 remove 를 안 부른다.
        worktree_created = False
        try:
            # prep(선-검출·fetch/show-ref·fork 해소·부모 기록)은 bare 컨텍스트 **capture 러너** —
            # fetch out 을 경고 detail 로 쓰고 rc 로 origin 해소를 판정한다(짧은 로컬 op·인터랙티브
            # 아님). worktree add *자체* 만 아래서 console-visible 러너로 실행한다. 주입된
            # git_runner(mock)면 그대로(DI seam).
            prep_runner = git_runner or worktree_git._real_git_runner(bare)
            slot_branch = f"{repo}_{n}"
            # 슬롯 전용 브랜치 `<repo>_<N>` 선-검출 (결정 (b)) — 작업 슬롯은 그 브랜치를 판다.
            # 미머지-보존 브랜치(작업 유실 방지로 보존)가 잔존하면 `worktree add` 가
            # `fatal: a branch named '<repo>_<N>' already exists`(rc≠0)로 죽는다 — 슬롯번호 병합
            # (ledger∪git-worktree)은 **worktree 없이 잔존하는 브랜치**를 못 본다(브랜치 축은
            # 슬롯번호 축과 독립). 여기서 선-검출해 정확한 원인+두 갈래를 fail-loud 로 준다(오귀인
            # orphan-worktree 진단 제거). try 안에서 raise → 아래 `except` 가 provisional lease 를
            # 롤백한다(worktree_created=False 라 롤백할 worktree 는 없음·중단-안전). 검출은
            # `_slot_branch_exists`(color-safe `--format=%(refname:short)`·splitlines 정확-일치·rc 무시 —
            # 평문 `branch --list` 는 `color.branch=always` 서 ANSI 오염·rc 기반은 주입 runner generic
            # 폴백 rc0 오탐).
            if not readonly and worktree_git._slot_branch_exists(prep_runner, slot_branch):
                raise worktree_records.SlotBranchExists(slot, slot_branch, base)
            # base 파생 — 슬롯을 *origin 최신*에서 시작한다. refspec
            # (`+refs/heads/*:refs/remotes/origin/*`)은 origin/* 만 갱신하고 로컬 `refs/heads/<base>`
            # 는 clone 시점 동결이라, 로컬 base 에서 파면 슬롯이 origin 보다 stale 하게 시작한다.
            # worktree add 전에 origin 을 fetch 하고(best-effort) `origin/<base>`(존재 시)에서 판다.
            #   - fetch 실패(오프라인)면 경고 후 로컬 `<base>` 폴백 — 슬롯 생성은 막지 않는다
            #     (worktree add 핵심 부작용 보존·`_set_bare_fetch_refspec` fail-soft 규율 정합).
            #   - 파생 기준 ref = fetch 성공 + `refs/remotes/origin/<base>` resolvable → `origin/<base>`
            #     (fetch 로 갱신된 최신), 아니면 로컬 `<base>`(폴백). 로컬 `refs/heads/<base>` 동결은
            #     손대지 않는다(pool 슬롯은 자기 슬롯 브랜치를 파므로 로컬 base 직접 체크아웃 안 함·
            #     fast-forward 는 범위 밖·리스크 회피).
            ref = base
            rc, out = prep_runner(["fetch", "origin"])
            if rc != 0:
                if readonly:
                    tail = (f"에서 readonly 슬롯을 detach 한다 — 네트워크 복구 후 "
                            f"`{lease_store._runtime_skill_entry('pm-worktree')} refresh` 로 최신 released tip "
                            "으로 갱신하라 (fail-soft).")
                else:
                    tail = "에서 슬롯을 판다 — 네트워크 복구 후 새 슬롯은 origin 최신에서 시작한다 (fail-soft)."
                print(
                    f"[경고] `git -C {bare} fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
                    f"  {worktree_records._frozen_fallback_label(base)}{tail}",
                    file=sys.stderr,
                )
            else:
                rc, _ = prep_runner(
                    ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{base}"]
                )
                if rc == 0:
                    ref = f"origin/{base}"
            if folders is not None:
                sparse_checkout._require_sparse_git(prep_runner, target=slot)
                sparse_checkout._require_tree_folders(folders, ref, prep_runner, target=slot)
                leases.append(provisional)
                pool_store._write_ledger(home, leases)
                provisional_recorded = True
            # worktree add 는 `.repos/<repo>.git` bare 컨텍스트에서 — 슬롯이 그 family repo 의
            # worktree 가 되게 한다. bare repo 도 `git -C <bare> worktree add <abs
            # path>` 가 동작한다(슬롯 path 는 절대).
            if readonly:
                # readonly 공유 슬롯 — 슬롯 전용 브랜치를 파지 않고 **detached HEAD**
                # 로 만든다. 같은 브랜치(main 등)를 두 worktree 가 점유 못 하는 git 제약을
                # detached 로 우회하고, 부수효과로 브랜치 미점유·실수 커밋이 브랜치를 안 움직여 피해가
                # 국소화된다(submodule pin=detached 모델 동형).
                add_argv = ["worktree", "add", "--detach", str(path), ref]
            else:
                # 슬롯 전용 브랜치 `<repo>_<N>` 를 파생 ref 에서 판다. `add <path> <ref>` 가 아니라
                # `-b`(브랜치 생성)인 이유: ref 만 주면 detached 거나 base 브랜치 자체에 붙어 슬롯 작업이
                # base 를 오염한다. **`--no-track` 필수**: remote-tracking ref(origin/<base>)에서 `-b` 로
                # 브랜치를 파면 git 기본 `branch.autoSetupMerge=true` 가 그 슬롯 브랜치에 upstream 을
                # *자동* 설정한다(A_1@{upstream}=origin/<base>·`git status`/무인자 `git pull` 이 슬롯 작업을
                # base 에 묶음). `--no-track` 으로 그 자동설정을 억제한다(로컬 <base> 폴백 경로에서도
                # 안전 — no-op·codex 게이트 포착 결함).
                add_argv = ["worktree", "add", "--no-track", "-b", slot_branch, str(path), ref]
                if folders is not None:
                    add_argv.insert(2, "--no-checkout")

            # worktree add *자체* 는 **console-visible(인터랙티브) 러너**로 실행한다. 대형 repo 의
            # full checkout(로컬 bare→worktree·느린 디스크/VPN/Windows)이 옛 captured 120s 에 false-kill
            # 되던 블로커 해소 — 진행상황이 콘솔에 실시간 보이고 timeout 이 GIT_TIMEOUT_SECONDS(1800s·env
            # `PM_GIT_TIMEOUT`·`none`=무제한)라 관대하다. submodule 단계와 동일 패턴(_real_git_runner_
            # interactive). **DI seam 보존**: 주입된 git_runner(테스트 mock)가 있으면 그걸 쓴다(현행 테스트
            # 무영향) — 인터랙티브는 `git_runner is None` 실경로만. 인터랙티브는 `(rc, "")` 반환(출력은 콘솔로
            # 직접)이라 실패 메시지는 rc 기반 + 아래 트립 안내로 조정한다(git stderr 는 이미 콘솔에 감).
            add_runner = git_runner or worktree_git._real_git_runner_interactive(bare, timeout=worktree_git.GIT_TIMEOUT_SECONDS)
            rc, out = add_runner(add_argv)
            if rc != 0:
                # 원인 힌트 (#4-bare) — 식별 가능한 원인을 raw git out 앞에 붙인다. 상단 가드가
                # broken bare 를 이미 걸러내지만, upfront rev-parse 는 통과했는데 objects 결손 등으로
                # worktree add 만 죽는 잔여 부분-bare(또는 op 도중 손상)를 여기서 재판정해 안내한다.
                bare_hint = ""
                if not worktree_git._is_valid_bare(bare, runner=git_runner or worktree_git._real_git_runner(bare)):
                    bare_hint = (
                        f"`.repos/{repo}.git` 가 유효 bare 가 아니다(부분/깨진 bare 가능성) — 수동 삭제 후 "
                        f"`pm-config repo add {repo}` 로 재hydrate 하라. "
                    )
                # orphan/already-exists 진단 (#4-충돌) — 슬롯번호를 git 병합으로 회피하지만,
                # 병합 후 나타난 orphan·수동 add 잔존이 `add` 를 "already exists" 로 죽일 수 있다.
                # out 문자열(captured/injected) 또는 실 git worktree 목록(인터랙티브는 out 이 콘솔 직행이라
                # 실측)에 이 슬롯 경로가 이미 등록됐으면 orphan 정리 경로를 안내한다.
                #
                # ⚠️ **오귀인 정정**: `git worktree add` 의 "already exists" 는 두 원인이 있다 —
                # (1) worktree **경로** 잔존(orphan·이 블록의 대상), (2) 슬롯 전용 **브랜치**(`<repo>_<N>`)
                # 잔존(`a branch named '<repo>_<N>' already exists`·미머지-보존 브랜치). 옛 코드는 "already
                # exists" 부분매치만 보아 (2)를 (1)로 **오귀인**해 orphan 정리 안내를 냈다. 위
                # 선-검출(SlotBranchExists)이 (2)를 먼저 잡지만, 여기서도 브랜치-존재 에러를 orphan 으로
                # 낚지 않게 원인을 분리 판정한다(클래스-fix·잔여/미래 경로 방어).
                out_l = str(out).lower()
                branch_exists_err = "a branch named" in out_l and "already exists" in out_l
                branch_hint = ""
                orphan_hint = ""
                if branch_exists_err:
                    branch_hint = (
                        f"슬롯 전용 브랜치 `{repo}_{n}` 가 이미 존재한다(미머지-보존 브랜치 잔존 가능성·"
                        f"remove_slot) — 사용자가 터미널에서 그 브랜치를 정리(머지 또는 폐기 — 세션의 손 git "
                        f"브랜치 삭제는 가드가 거부한다) 후 새 슬롯 재생성하거나, 미머지 작업을 이어가려면 다른 슬롯에서 "
                        f"`switch <slot> {repo}_{n}` 로 전환해 재개하라(리셋 없음). "
                    )
                else:
                    # worktree-path orphan — (브랜치 아닌) "already exists" 또는 실 git worktree 목록에 슬롯 등록.
                    already = "already exists" in out_l
                    if not already:
                        already = any(
                            w.slot == slot for w in worktree_git.list_git_worktrees(home, repo, git_runner=git_runner)
                        )
                    if already:
                        orphan_hint = (
                            f"경로 `{path}` 에 worktree 가 이미 등록돼 있다(orphan·중단된 create/수동 add "
                            f"잔존 가능성) — `pm-config status` 로 orphan/stale 을 확인하고 정리"
                            f"(worktree prune 또는 수동 삭제)하라. "
                        )
                # 트립/실패 안내 — 하네스 자동 호출이 timeout/실패로 죽었을 때 사용자에게 다음
                # 행동(터미널 직접 실행·무제한 opt-in)을 준다. rc 기반(out 은 인터랙티브면 빈 문자열).
                timeout_desc = "무제한" if worktree_git.GIT_TIMEOUT_SECONDS is None else f"{worktree_git.GIT_TIMEOUT_SECONDS}초"
                raise RuntimeError(
                    f"git worktree add failed for {slot!r} (rc={rc}, out={out!r}). {bare_hint}{branch_hint}{orphan_hint}"
                    f"매우 느린 op(대형 repo·느린 디스크/VPN)이면 timeout({timeout_desc}·PM_GIT_TIMEOUT)에 "
                    f"걸렸을 수 있다 — 먼저 사용자에게 repo {repo!r} 슬롯 생성 승인을 "
                    f"요청하라. 승인한 사용자만 터미널에서 `pm-config worktree add {repo} "
                    f"--user-ack {repo}`를 대상값에 결속해 직접 실행하면 진행상황이 보이고, "
                    f"`PM_GIT_TIMEOUT=none` 으로 무제한 실행할 수 있다(세션 자동 부착 금지)."
                )
            # add 성공 — fs 에 worktree 존재. 이후 단계(submodule)/interrupt 실패 시 except 가 이 슬롯을
            # 롤백(remove)한다. add *자체* 실패면 worktree_created=False 라 remove 를 안 부른다(범위 유지).
            worktree_created = True

            # 부모 기록 — 방금 판 슬롯 브랜치의 부모 = base, 갈라진 커밋 = 파생 ref 의 커밋. 브랜치가
            # 기록 없이 남지 않도록 add 직후에 한다(실패는 raise → 아래 롤백).
            if not readonly:
                rc, out = prep_runner(["rev-parse", "--verify", f"{ref}^{{commit}}"])
                fork = str(out).strip()
                if rc != 0 or not fork:
                    raise branch_store.BranchParentError(
                        f"슬롯 브랜치 {slot_branch!r} 의 갈라진 커밋(`{ref}`)을 해소하지 못했다 "
                        f"(rc={rc}): {fork[:200]}"
                    )
                branch_store.record_branch_parent(slot_branch, base, fork, git_runner=prep_runner)

            if folders is not None:
                sparse_checkout._checkout_folders(slot, path, folders, git_runner=git_runner)

            # submodule init — worktree add 는 submodule 자동 init 안 함.
            # `--force`: bare 에서 만든 fresh 슬롯의 worktree+submodule edge 에서 plain `--init` 이
            # 체크아웃 못 하는 상태(`git submodule init failed: ''` — 실 Windows multi-PM 파일럿서 빈
            # 에러로 죽음)를 강제 init 한다. create_slot 은 *새 슬롯 생성 때만* 호출되고
            # (기존 슬롯 재사용은 alloc·재init 안 함) fresh worktree 라 잃을 로컬 변경이 없으므로
            # `--force` 안전. submodule 없는 repo 는 `--init --recursive --force` 가 no-op rc 0.
            #
            # **인터랙티브 러너**: 실경로(git_runner 미주입)에선 capture 러너 대신
            # `_real_git_runner_interactive`(stdio 콘솔 상속·SUBMODULE_TIMEOUT 3600s)로 돈다 —
            # 대형 submodule clone 이 600s 초과해 TimeoutExpired→(1,"")로 죽던 블로커 해소(진행
            # 상황 화면 표시·credential 프롬프트·대형 clone 완주). worktree add 도 같은 이유로
            # console-visible(GIT_TIMEOUT_SECONDS)이고, 짧은 captured git(status·checkout·
            # dirty·stash)만 capture 러너 그대로. **DI seam 보존**: 주입된 git_runner(테스트 mock)가
            # 있으면 그걸 쓴다(현행 테스트 무영향) — 인터랙티브는 `git_runner is None` 실경로만.
            #
            # **원자적 롤백**: 실패(rc≠0)면 raise 만 하고, 롤백(worktree remove)과
            # provisional 제거는 아래 `except` 가 단일 경로로 처리한다 — worktree add 는 *이미 성공*
            # 했으므로 그 worktree 를 지워야 댕글링("슬롯 없음"+재시도 "이미 존재")이 안 남는다. 빈 out
            # (Windows 인코딩 캡처 유실·인터랙티브는 항상 빈 out)에도 막히지 않게 rc + argv surface.
            if init_submodules:
                sub_runner = git_runner or worktree_git._real_git_runner_interactive(path)
                sub_argv = ["submodule", "update", "--init", "--recursive", "--force"]
                if folders is not None:
                    tags = sparse_checkout._index_gitlink_tags(git_runner or worktree_git._real_git_runner(path), target=slot)
                    shown = [sub for sub, tag in tags.items() if tag != sparse_checkout._SKIP_WORKTREE_TAG]
                    sub_argv += ["--", *shown]
                    rc, out = sub_runner(sub_argv) if shown else (0, "")
                else:
                    rc, out = sub_runner(sub_argv)
                if rc != 0:
                    raise RuntimeError(
                        f"git submodule init failed for {slot!r}: "
                        f"rc={rc}, argv={sub_argv!r}, out={out!r}"
                    )

            # 성공 — provisional 을 leased 로 **확정**한다(2차 write·branch 는 장부 미저장
            # git=진실·조회는 live current_branch). provisional 을 그대로 반환(같은 필드).
            # 단, leased 는 **명의가 있을 때**다: owner_task 는 그 task 명의로, readonly 는
            # 무소유 공유로 확정한다. 명의가 없는 생성(그 밖)은 leased 가 아니라 **idle** 이다 —
            # 생성은 풀 확장이고 점유는 alloc 이 한다 — CLI 안내("이 슬롯은 idle 이다. task 가 빌려
            # 쓴다")와도 같은 뜻이다.
            provisional.state = "leased"
            if readonly:
                # readonly 공유 슬롯 — 무소유 확정: session 을 비우고 role 을 박는다.
                # state 는 "leased"(alloc idle-탐색·release 소유탐색이 session 매칭이라 무소유는 자연
                # 제외). role 이 0단계 carve-out·mutation 거부의 축.
                provisional.session = ""
                provisional.role = "readonly"
            elif not task_created:
                # 풀 슬롯 — 무소유 idle 확정: readonly 와 같은 "무소유" 표현(session 빈 문자열)을
                # 쓰고 state 만 idle 이라 alloc 의 idle-탐색이 곧바로 집는다.
                provisional.state = "idle"
                provisional.session = ""
            # create git 스냅 — readonly 는 따라갈 부모 브랜치를 base 로 함께 기록한다(commit=방금
            # detach 한 tip). 작업 슬롯의 부모는 위 git 설정 기록이 진실이라 스냅이 base 를 두지 않는다.
            # 스냅 예외는 전파한다(아래 청소가 provisional 을 지운다).
            slot_snapshot._apply_git_snapshot(home, provisional, base_branch=base, git_runner=git_runner)
            pool_store._write_ledger(home, leases)
            return provisional
        except BaseException:
            # **중단-안전 청소** — 실패(rc≠0 raise)·예외·KeyboardInterrupt 는 여기서 단일
            # 경로로 정리한다: (1) worktree add 가 성공했으면(worktree_created) 그 worktree 를
            # `_rollback_worktree`(remove --force) 로 지운다(add 자체 실패면 지울 게 없어
            # (2) provisional("creating") 엔트리를 장부에서 제거하고
            # 다시 쓴다(불완전 슬롯 미등록·기존 계약). 두 단계를 모두 시도하고, 롤백 실패는 전파하되
            # 원래 에러는 traceback 의 `__context__` 로 남는다.
            # (SIGKILL 은 여기 못 옴 — provisional 이 disk 에 남아 reconcile 이 incomplete 로 잡는다.)
            try:
                if worktree_created:
                    worktree_git._rollback_worktree(home, repo, path, git_runner=git_runner)
            finally:
                if provisional_recorded:
                    leases[:] = [l for l in leases if l.slot != slot]
                    pool_store._write_ledger(home, leases)
            raise


def reconcile_worktrees(home, *, git_runner: worktree_records.GitRunner | None = None) -> worktree_records.ReconcileResult:
    """리스 장부 × `list_git_worktrees` 대조로 drift 를 판정한다 — 조회 전용·부작용 0.

    - **orphan** = git worktree(슬롯 경로·non-bare)인데 장부에 없음 — 중단된 create/수동 add 잔존
      (audit #2). 다음 create_slot 번호 충돌(#4)·status blind(#3)의 근원.
    - **stale** = 장부 확정 리스(leased/idle)이고 **pool worktree 자리**(`work/` 직계 하위)를
      주장하는데 대응 git worktree 없음 — worktree dir 삭제/prune (audit #3). pool 자리가 아닌
      행(PM 홈 자신을 가리키는 행 등)은 git worktree 목록의 값 域 밖이라 판정 대상이 아니다.
    - **incomplete** = provisional("creating") 리스 — worktree add 후 확정 전 중단된 create 흔적
      (SIGKILL 커버). 별도 카테고리라 stale 과 이중 계상하지 않는다.

    **부작용 없음**(삭제/이동/prune 안 함) — surface + 복구 안내만(자동삭제는 사용자 위임·파일
    삭제 원칙). 실 git 실패는 `list_git_worktrees` 가 fail-soft(빈 리스트)라 그 repo 확정 리스가
    보수적으로 stale 로 degrade 할 수 있으나 advisory·조회 전용이라 무해.
    """
    leases = pool_store.list_leases(home)
    git_wts = worktree_git.list_git_worktrees(home, git_runner=git_runner)
    slot_wts = [w for w in git_wts if w.slot and not w.bare]
    lease_slots = {l.slot for l in leases}
    git_slots = {w.slot for w in slot_wts}
    orphans = [w for w in slot_wts if w.slot not in lease_slots]
    incomplete = [l for l in leases if l.state == "creating"]
    # stale 판정 대상은 **pool worktree 자리(`work/` 직계 하위)를 주장하는 행**뿐이다. 대조
    # 소스(`list_git_worktrees`)가 낼 수 있는 값이 그 자리뿐이라, 그 밖의 행(PM 홈 자신을
    # 가리키는 행 등)을 "git 목록에 없다"로 판정하면 데이터가 뒷받침하지 않는 결론을 영구
    # surface 한다. 위치는 경로 **값**으로 가른다(이름 모양 무관).
    stale = [
        l for l in leases
        if l.state != "creating" and worktree_paths._is_pool_worktree_slot(home, l.slot) and l.slot not in git_slots
    ]
    return worktree_records.ReconcileResult(orphans=orphans, stale=stale, incomplete=incomplete)


def prune_stale_leases(home) -> list[str]:
    """worktree 가 **확정 부재**인 dangling 장부 엔트리를 제거한다 — user-invoked 안전 cleanup.

    reconcile 이 surface 한 stale/incomplete 중 **worktree dir 이 물리적으로 사라진**(`slot_path
    (slot).exists()` False) 엔트리를 장부에서 삭제한다. 제거된 슬롯 식별자 리스트를 반환한다.

    **왜 안전한가 (삭제-위임 원칙 위반 아님)**: 이건 *사용자 데이터/worktree 삭제*가 아니라 이미
    사라진 worktree 의 **dangling 기록 정리**다 — 지울 파일이 없다(dir 부재가 전제). 그래서:
      - orphan **worktree**(git 측·disk 에 존재·작업이 있을 수 있음)는 **손대지 않는다** — 그건
        `git worktree remove <path>` 로 사용자가 판단해 지운다(status reconcile 이 안내).
      - worktree 가 **존재**하는 leased/idle/creating 엔트리도 손대지 않는다(dir 존재 → prune 안 함).
        (leased 재사용·incomplete 정리는 release/사용자 몫.)

    조회-전용 reconcile 과 분리한 **명시 user-invoked 프리미티브**(`pm-config worktree prune-stale`)라
    status 는 여전히 부작용 0 다. `_lease_lock` + `_write_ledger`(atomic·다른 장부 op 와 동일 직렬화).
    fs 존재 판정뿐이라 git 불요(hermetic).
    """
    pruned: list[str] = []
    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)
        kept = [l for l in leases if pm_paths.slot_path(home, l.slot).exists()]
        pruned = [l.slot for l in leases if not pm_paths.slot_path(home, l.slot).exists()]
        if pruned:
            pool_store._write_ledger(home, kept)
    return pruned


def set_test_cmd(home, slot: str, cmd: str | None) -> worktree_records.Lease:
    """기존 슬롯 리스의 test_cmd 를 갱신한다 (idle/leased 무관).

    `pm-config` 콘솔의 `[b]`(슬롯 빌드명령 설정/변경)·worktree add 후의 "나중에 변경"
    경로가 부르는 setter — 슬롯에 바인딩된 회귀/빌드명령(HIL config 등)을 사후에 바꾼다
    (regression._test_cmd 가 활성 슬롯의 이 필드를 areas 위 레이어로 읽는다). 별도
    CLI 서브커맨드는 만들지 않는다(콘솔 `[b]` + worktree add 프롬프트로 충분·결정 §setter 단순화).

    create_slot 의 lease test_cmd 바인딩과 *같은* flock + atomic write 패턴을 재사용한다
    (`_lease_lock` 으로 read-modify-write 직렬화 → `_write_ledger` atomic replace). 장부에
    슬롯이 없으면 **`KeyError`** raise(침묵 무력화 금지 — 호출부가 명시 안내). 갱신된
    Lease 를 반환한다. `cmd=None` 이면 바인딩 해제(repo areas/local.conf 로 폴백·현행).
    """
    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            raise KeyError(f"no lease for slot {slot!r}")
        target.test_cmd = cmd
        pool_store._write_ledger(home, leases)
        return target
