"""슬롯 브랜치 전환·갱신·재동기 작업 조정."""
from __future__ import annotations

import sys
from pathlib import Path

from pm_engine.domain import worktree_records
from pm_engine.infra import branch_store, lease_store, pm_paths, pool_store, slot_snapshot, sparse_checkout, submodule_git, worktree_git


def _apply_slot_folders(home, slot: str, folders: "list[str] | None", *,
                        git_runner: worktree_records.GitRunner | None) -> None:
    """idle 대여가 슬롯의 폴더 상태를 그 대여가 말한 대로 맞춘다. 목록이면 그 cone, 없으면 전체.

    폴더 없는 대여는 슬롯이 제한 상태일 때만 푼다(`disable`). 제한이 없던 슬롯에는 제한 여부를
    읽을 뿐 폴더 제한 명령을 부르지 않는다. `disable` 은 제한 없는 슬롯에서도 공용 설정을 바꾼다.
    지정 폴더의 버전·최종 ref 트리·현재 dirty 검증은 alloc의 checkout 이전 단계에서 완료한다."""
    runner = git_runner or worktree_git._real_git_runner(pm_paths.slot_path(home, slot))
    if folders is None:
        if not sparse_checkout._config_bool(runner, "core.sparseCheckout", target=slot):
            return
        sparse_checkout._require_clean_for_folders(home, slot, runner)
        sparse_checkout._run_sparse_checkout(runner, ["disable"], target=slot)
        submodule_git._resync_submodules_selective(pm_paths.slot_path(home, slot), git_runner=runner)
        return
    sparse_checkout._run_sparse_checkout(runner, ["set", "--cone", "--", *folders], target=slot)
    submodule_git._resync_submodules_selective(pm_paths.slot_path(home, slot), git_runner=runner)


def refresh(home, slot: str, *, onto: "str | None" = None,
            git_runner: worktree_records.GitRunner | None = None) -> "tuple[str, str]":
    """readonly 공유 슬롯을 released 최신으로 갱신한다 — fetch → detached HEAD 이동.

    read-only 슬롯(detached·문서 검증 기준면)을 최신 released tip 으로 fast-forward 하는 유일 경로다
    (mutation op 은 거부·`refresh` 만 허용). 순서:
      1. **readonly 확인** — 대상 슬롯이 readonly 가 아니면 `RefreshRefused("not-readonly")`(작업 슬롯을
         detach 이동하면 브랜치 위치 유실).
      2. **기준 해소** — `onto`(명시) > 기록된 `base.branch`. 둘 다 없으면 `RefreshRefused("no-base")`
         (추론 금지).
      3. **dirty 거부 + loud** — 슬롯 worktree 에 미커밋 변경이 있으면 `RefreshRefused("dirty")`. read-only
         슬롯의 dirty 는 "누군가 썼다"는 신호라 조용히 reset 하지 않는다(감지=기계·해소=사용자).
      4. **fetch → detach 이동** — `git fetch origin`(best-effort) 후 기준 ref 로 `git checkout
         --detach <ref>`(detached HEAD 이동). **기준 ref 해소는 두 경로가 다르다**:
           - `onto` **명시** = 준 ref 를 **그대로** 쓴다(로컬 브랜치명이면 로컬 tip·자동 대체 없음).
             명시 인자를 `origin/<branch>` 로 조용히 바꾸면 미push 로컬 tip 으로 갱신할 길이 없고,
             사용자가 지정한 기준과 실제 이동 지점이 어긋난 채 성공 보고된다. 원격 기준을 원하면
             `origin/<branch>` 로 적는다. 해소 실패는 대체 없이 fail-loud(`RefreshRefused`).
           - `onto` **미지정**(기록된 base.branch) = `origin/<branch>` 선호를 유지하되, 로컬
             `<branch>` 가 그보다 앞서 있으면(origin 에 없는 커밋 보유) 대체하지 않고 로컬 tip 을
             쓴다(stderr 1줄 고지). 그 외(로컬 없음/동일/origin 이 앞섬)는 origin 채택. 해소는
             `rebase` 무인자와 **같은 helper**(`_origin_or_local_base_ref`)를 써서 같은 도구·같은
             인자에 규칙이 갈리지 않는다.
      4b. **submodule 재동기** — `git submodule update --init --recursive --force`(gitlink 옛 pin 잔존
         →stale+dirty 자가 잠금 방지·readonly=dev submodule 없어 전체 재동기 안전).
      5. **스냅 갱신** — `record_git_snapshot(base_branch=기준 branch)` — 기준 branch 는 onto(명시 시)
         또는 기록된 base.branch 이며(무인자 경로에서 ref 가 origin/<branch> 로 해소돼도 장부에는 논리
         branch 가 남는다), base.commit 을 새 head 로 갱신한다(onto 생략에도 갱신 — HEAD 이동했는데
         장부 base.commit 옛값 잔존 불일치 방지). refresh 는 readonly 슬롯 base 가 바뀌는 유일
         지점이다. 주의: 명시 `--onto` 는 비-고착 — 장부에 남는 건 그 branch 명이라
         다음 무인자 refresh 는 `_origin_or_local_base_ref` 로 다시 해소된다(origin 이 앞서거나 같으면
         origin/<branch>·로컬이 앞서면 로컬 그대로 — 성공 메시지의 실측 ref/sha 로 확인).
    반환 = `(이동한 기준 ref, 이동 후 HEAD 커밋 sha)` — 본체 CLI 가 이 실측값을 그대로 보고한다(조용한
    ref 대체 제거의 짝: 결과 메시지도 추정이 아니라 실제 해소값). sha 조회 불가는 빈 문자열(fail-soft·
    이동 자체는 성공). `git_runner` 주입 시 그 runner(테스트 hermetic·미주입이면 슬롯 worktree 바인딩
    실 runner)."""
    lease = pool_store.read_lease_strict(home, slot)
    if lease is None or lease.role != pool_store._LEASE_ROLE_READONLY:
        raise worktree_records.RefreshRefused(slot, "not-readonly")
    # 기준 해소 — onto 명시 > 기록된 base.branch (추론 금지).
    base_branch = onto
    if base_branch is None:
        recorded = lease.git.get("base") if isinstance(lease.git, dict) else None
        if not isinstance(recorded, dict):
            recorded = None
        if recorded and recorded.get("branch"):
            base_branch = recorded["branch"]
    if not base_branch:
        raise worktree_records.RefreshRefused(slot, "no-base")

    runner = git_runner
    if runner is None:
        p = pm_paths.slot_path(home, slot)
        if not p.exists():
            raise worktree_records.RefreshRefused(slot, "git-error",
                                 detail=f"슬롯 worktree 경로 {p} 가 없다")
        runner = worktree_git._real_git_runner(p)

    # dirty = 거부 + loud (조용히 reset 금지·신호 보존).
    if worktree_git._is_dirty(pm_paths.slot_path(home, slot), git_runner=runner):
        raise worktree_records.RefreshRefused(slot, "dirty")

    # fetch → 기준 ref 해소 → detach 이동 (해소 규칙은 docstring 4 — 명시 인자는 그대로·기본 경로만
    # origin 우선). 명시 인자를 조용히 다른 ref 로 바꾸면 사용자가 지정한 기준과 실제 이동 지점이
    # 어긋난 채 성공 보고된다(미push 로컬 tip 갱신 불가). 대체가 필요한 상황이면 loud 실패로 남긴다.
    ref = base_branch
    rc, out = runner(["fetch", "origin"])
    if rc != 0:
        print(
            f"[경고] 슬롯 {slot} `git fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
            f"  {worktree_records._frozen_fallback_label(base_branch)}로 detach 이동한다 — 네트워크 복구 후 재-refresh 하라 "
            "(fail-soft).",
            file=sys.stderr,
        )
    elif onto is None and not base_branch.startswith("origin/"):
        # `--onto` 미지정 기본 경로 한정 — 대상 ref 해소는 `rebase` 무인자와 같은 helper 로 — origin/
        # <branch> 선호를 유지하되 로컬이 앞서 있으면 대체하지 않는다(stderr 1줄 고지).
        ref = branch_store._origin_or_local_base_ref(base_branch, slot=slot, runner=runner)
    rc, out = runner(["checkout", "--detach", ref])
    if rc != 0:
        # 명시 인자 해소 실패는 자동 대체 없이 여기서 끝난다 — 대안(원격 기준)은 사용자가 명시한다.
        hint = ("" if onto is None else
                " — `--onto` 는 준 ref 를 그대로 해소한다(자동 대체 없음). 원격 기준을 원하면 "
                "`origin/<branch>` 로 명시하라")
        raise worktree_records.RefreshRefused(slot, "git-error",
                             detail=f"`git checkout --detach {ref}` 실패 (rc={rc}): "
                                    f"{str(out).strip()[:200]}{hint}")
    # submodule 재동기 — `checkout --detach` 는 superproject HEAD 만 옮기고
    # submodule gitlink 는 **옛 pin 잔존** → readonly 기준면이 stale + `git status` dirty(gitlink 변경)
    # 로 남아 **다음 refresh 가 자기 dirty 거부에 걸리는 자가 잠금**이 된다(create_slot 은 init 하는데
    # refresh 는 안 하던 비대칭). readonly 슬롯은 mutation(dev) 거부라 on-branch(dev) submodule 이
    # 존재할 수 없으니(보호 대상 0), selective 가 아닌 **전체 재동기**(`--init --recursive --force`·
    # create_slot 관례 정합)가 안전하다. rc≠0 은 fail-loud(기준면이 반쯤 갱신된 채 성공 보고 금지).
    rc, out = runner(["submodule", "update", "--init", "--recursive", "--force"])
    if rc != 0:
        raise worktree_records.RefreshRefused(slot, "git-error",
                             detail=f"submodule 재동기 실패 (rc={rc}): {str(out).strip()[:200]}")
    # 스냅 갱신 — base 를 **기준 branch(onto 또는 기록된 base.branch)로 재기록**해 base.commit=새 head 로
    # 갱신한다. onto 생략(기록된 base.branch 로 refresh)에도 base_branch 를 넘겨야, HEAD 는 최신
    # origin/<base> 로 이동했는데 장부 base.commit 은 옛 커밋으로 남아 status 부모 분기 커밋·기준면 기록이
    # 실제와 어긋나는 불일치를 막는다. refresh 는 readonly 슬롯 base 가 바뀌는 유일 지점이다(detached
    # 기준면 이동이 곧 base 이동). `_apply_git_snapshot` 이 방금 스냅한 head 를 commit 으로 쓴다.
    slot_snapshot.record_git_snapshot(home, slot, base_branch=base_branch, git_runner=git_runner)
    # 이동 후 실 HEAD sha — 결과 메시지가 "어느 ref 의 어느 커밋으로 갔는지"를 실측으로 보고하게 한다
    # (해소 규칙을 사용자 명시 존중으로 바꾼 것의 짝). 조회 불가는 "" fail-soft(이동 자체는 성공).
    return ref, (slot_snapshot._slot_head(runner) or "")


def _rebase_in_progress(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None) -> bool:
    """슬롯 worktree 에 rebase 가 진행 중인가 — `.git/rebase-merge` | `rebase-apply` 존재.

    worktree 슬롯의 per-worktree git 디렉토리 위치를 `git rev-parse --git-path <name>` 로 해소하고
    (worktree 는 `.git` 이 gitdir 를 가리키는 파일이라 직접 경로 조합 불가) 그 경로 실재를 본다. 둘
    중 하나라도 있으면 rebase 진행 중(선-검사 스킵 사유). git 조회 rc≠0 → 그 이름은 미검출(진행중
    미검출 시 rebase 시도가 git 자체 'already a rebase in progress' 로 rc≠0 → conflict 로
    수렴·이중 안전). git 실행 예외는 전파한다. `git_runner` 미주입이면 슬롯 worktree 바인딩 실
    runner(부재면 False)."""
    runner = git_runner
    if runner is None:
        p = pm_paths.slot_path(home, slot)
        if not p.exists():
            return False
        runner = worktree_git._real_git_runner(p)
    for name in ("rebase-merge", "rebase-apply"):
        rc, out = runner(["rev-parse", "--git-path", name])
        if rc != 0:
            continue
        raw = (out or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = pm_paths.slot_path(home, slot) / raw   # rev-parse --git-path 는 cwd(슬롯) 상대일 수 있다.
        if path.exists():
            return True
    return False


def _rebase_one(home, slot: str, *, onto: "str | None", owner: str,
                git_runner: worktree_records.GitRunner | None = None) -> worktree_records.RebaseSlotResult:
    """슬롯 하나 rebase — 선-검사 → fetch → git rebase → 성공 시 부모 기록·장부 스냅 갱신.

    선-검사(스킵+loud·순서): readonly(공유 기준면·mutation 불가) → 소유(`owner` 명의 leased
    아님 — `owner` 는 세션 또는 task 명의·해소는 `rebase`) → worktree 부재 → dirty(clean 전제) →
    rebase 진행 중 → 브랜치 없음(detached·`no-branch`). 통과하면 옮길 부모를 해소한다:
      - `onto` **명시** = 그 ref 가 해소돼야 하고(`unresolvable-onto`) 준 값을 **그대로** rebase 대상
        이자 새 부모 이름으로 쓴다. `origin/<ref>` 로 조용히 바꾸지 않는다(로컬 브랜치를 지정했는데
        같은 이름의 stale `origin/<ref>` 위로 rebase 되는 사고 차단).
      - `onto` **생략** = 브랜치 부모 기록(`read_branch_parent`)의 부모. 기록 없으면 `no-parent`
        스킵(추론 금지). fetch 뒤 대상 ref 는 `refresh` 무인자와 **같은 helper**
        (`_origin_or_local_base_ref`)로 해소한다 — origin 선호·로컬이 앞서면 로컬 tip(stderr 고지).
    이후 `git rebase <target>`. rc≠0(충돌 등) = **그 상태 그대로 두고 conflict**(엔진 임의 abort 금지·
    부모 기록·장부 불변). 성공 = fork=대상 tip 으로 부모 기록을 다시 쓰고(`record_branch_parent`)
    장부 스냅(head·recorded_at)을 갱신한다. 선-검사·충돌은 RebaseSlotResult 로 돌려 일괄 독립성을
    보장하고, rebase 성공 뒤 기록 실패는 `BranchParentError` 로 올린다(기록 없이 성공 보고 금지)."""
    def skip(reason: str) -> worktree_records.RebaseSlotResult:
        return worktree_records.RebaseSlotResult(slot, worktree_records.REBASE_SKIPPED, reason=reason)

    # ── 선-검사 (스킵 + loud·독립) ─────────────────────────────────────────
    lease = pool_store.read_lease_strict(home, slot)
    if lease is not None and lease.role == pool_store._LEASE_ROLE_READONLY:
        return skip("readonly")   # 공유 기준면 — mutation 불가(·refresh 로만 갱신).
    # 소유 판정 = leased lease.session == 내 명의(`owner`) — `release`(`target.session !=
    # owner_task`)와 **같은 장부·같은 비교**다. 명의가 세션이냐 task 냐는 `rebase` 가
    # 해소하고(단일 지점), 여기 판정은 축과 무관하게 하나다.
    if lease is None or lease.state != "leased" or lease.session != owner:
        holder = lease.session if (lease is not None and lease.state == "leased") else ""
        return skip("not-owner" if not holder else f"not-owner:{holder}")
    runner = git_runner
    if runner is None:
        p = pm_paths.slot_path(home, slot)
        if not p.exists():
            return skip("no-worktree")
        runner = worktree_git._real_git_runner(p)
    if worktree_git._is_dirty(pm_paths.slot_path(home, slot), git_runner=runner):
        return skip("dirty")      # rebase 는 clean 전제(미커밋 변경 유실 방지).
    if _rebase_in_progress(home, slot, git_runner=runner):
        return skip("in-progress")  # 이미 진행 중 — 사용자 해소(continue/abort) 먼저.
    branch = worktree_git.current_branch(home, slot, git_runner=runner)
    if branch is None:
        return skip("no-branch")  # detached — rebase 할 브랜치·부모 기록이 없다.

    # ── 부모 해소 (onto 명시=그 값·생략=부모 기록·기록 없음=거부) ──────────────
    # 기록은 rebase 성공 시에만 — 이후 충돌/abort 시 기록이 새 부모를 거짓 주장하지 않게 한다.
    if onto is not None:
        if branch_store._resolve_base_commit(home, slot, onto, git_runner=runner) is None:
            return skip("unresolvable-onto")  # --onto ref 해소 실패(오타·미fetch).
        parent_name = onto
    else:
        record = branch_store.read_branch_parent(branch, git_runner=runner)
        if record is None:
            return skip("no-parent")   # 부모 기록 없음 + --onto 없음 → 거부(추론 금지).
        parent_name = record["name"]

    # ── 부모 최신 fetch → git rebase ───────────────────────────────────────
    target = parent_name
    rc, out = runner(["fetch", "origin"])
    if rc != 0:
        print(
            f"[경고] 슬롯 {slot} `git fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
            f"  {worktree_records._frozen_fallback_label(parent_name)}로 rebase 를 시도한다 — 네트워크 복구 후 재시도 권장 "
            "(fail-soft).",
            file=sys.stderr,
        )
    elif onto is None:
        # `--onto` 생략 경로 한정 — 대상 ref 해소는 `refresh` 무인자와 같은 helper. `onto` 명시 경로는
        # 이 elif 를 안 타므로 준 값 그대로다(조용한 origin 대체 없음·refresh 명시-onto 계약과 동형).
        target = branch_store._origin_or_local_base_ref(parent_name, slot=slot, runner=runner)
    rc, out = runner(["rebase", target])
    if rc != 0:
        # 충돌(또는 실패) — **그 상태 그대로 두고 fail-loud**. 엔진이 임의 abort 하지 않는다
        # (해소는 사용자 git rebase --continue|--abort). 부모 기록·장부는 **완전 불변** → 다음
        # 부트스트랩 0단계가 "rebase 진행 중" 으로 감지·안내한다.
        return worktree_records.RebaseSlotResult(slot, worktree_records.REBASE_CONFLICT, base=parent_name,
                                reason=str(out).strip()[:200])

    # ── 성공 → 부모 기록(fork=대상 tip) + 장부 스냅 갱신(**유일 기록 지점**) ──────
    fork = branch_store._resolve_base_commit(home, slot, target, git_runner=runner)
    if fork is None:
        raise branch_store.BranchParentError(
            f"슬롯 {slot} rebase 는 성공했으나 부모 `{target}` 커밋을 해소하지 못해 브랜치 {branch!r} "
            "부모 기록을 갱신하지 못했다"
        )
    branch_store.record_branch_parent(branch, parent_name, fork, git_runner=runner)
    slot_snapshot.record_git_snapshot(home, slot, git_runner=runner)
    return worktree_records.RebaseSlotResult(slot, worktree_records.REBASE_REBASED, base=parent_name,
                            new_head=slot_snapshot._slot_head(runner))


def rebase(home, slots: "list[str]", *, onto: "str | None" = None,
           owner_task: "str | None" = None,
           git_runner: worktree_records.GitRunner | None = None) -> "list[RebaseSlotResult]":
    """슬롯 브랜치를 부모 위로 사용자 명시로 rebase — 단일/일괄·슬롯 독립·자동 rebase 없음.

    `slots` = 대상 슬롯 식별자 리스트(단일이면 1개·일괄이면 `slots_for_task` 결과). 각 슬롯을
    `_rebase_one` 로 **독립** 처리한다 — 한 슬롯의 충돌/스킵이 나머지를 막지 않는다(일괄 독립성·
    ). `onto` 생략 = 기록된 부모 브랜치 최신으로 rebase(부모 기록 없으면 거부). 반환 =
    슬롯별 `RebaseSlotResult` 리스트(호출부가 성공/스킵/충돌 요약).

    **소유 판별 축** = `owner_task`(명시 task 명의) > `_default_session()`(세션 명의):
      - `owner_task` 주어짐(CLI `--task <task>`·단일/일괄 공통) → **그 task 명의**가 '내 것'의
        정의다. `release(owner_task=)` 의 소유검사와 **동형**(같은 장부·같은 의미: 그 슬롯의
        leased `lease.session` 이 그 명의인가) — 같은 소유를 두 도구가 다르게 판정하던 게 결함의
        본질이었다(이 task 를 1급 축으로 올렸는데 rebase 의 소유 축만 슬롯 세션에 머묾).
      - 미지정 → 세션 명의(`_default_session()` — env/단일-lease 유입·`_resolve_current_
        slot` 동형)로 판정.
    어느 축이든 **검사 자체는 그대로**다 — 그 명의가 leased 로 보유하지 않은 슬롯(다른 task·다른
    슬롯 세션·미점유)은 여전히 `not-owner` 스킵(loud). 축 확장이지 검사 제거가 아니다."""
    owner = owner_task or lease_store._default_session(home)
    return [_rebase_one(home, s, onto=onto, owner=owner, git_runner=git_runner) for s in slots]


def _rerecord_branch_snapshot(home, slot: str, target: str, lease: "Lease", *,
                              git_runner: worktree_records.GitRunner | None,
                              refuse: "Callable[[str, str], RuntimeError]") -> dict:
    """브랜치를 바꾼 **뒤** 장부 스냅을 재기록하고 그 기록을 돌려준다 — `switch`·`create_task_branch` 공용.

    여기서부터는 브랜치 변경이 이미 일어난 뒤다. 어떤 실패든 조용히 통과시키면 장부가 stale 인 채
    성공 보고가 나가고, 사용자는 다음 0단계에서 diverged 차단을 만난다 → 전부
    `refuse("record-failed", detail)` 로 수렴해 해소 안내를 싣는다 — **예외**(장부 IO·권한·락)까지
    포함(내부 리뷰 must-fix: 불일치 dict 반환만 잡으면 가장 흔한 실패인 예외가 원시 traceback 으로
    샌다). 판정은 `_cmd_record` 와 **동형** — 무변경(스냅 불가로 기존 git 보존)·branch 불일치·None 전부
    실패다. branch 만 보면 "기록=X·live=main diverged 에서 switch X" 처럼 **이미 X 로 기록된** 장부가
    stale head 를 지닌 채 성공으로 통과한다(내부 리뷰 should-fix). 이미 그 브랜치·완전 동일 스냅이면
    보수적 loud(=record 동형·재실행 안내)로 기운다 — 조용한 stale 보다 낫다."""
    before_git = lease.git if isinstance(getattr(lease, "git", None), dict) else None
    try:
        updated = slot_snapshot.record_git_snapshot(home, slot, git_runner=git_runner)
    except Exception as exc:  # noqa: BLE001 — must-fix: 장부 IO/권한/락 예외도 안내 채널로 수렴.
        converted = refuse("record-failed", f"장부 기록 예외: {exc}")
        raise converted from exc
    recorded = updated.git if (updated is not None and isinstance(updated.git, dict)) else None
    if recorded is None or recorded == before_git or recorded.get("branch") != target:
        unchanged = recorded is not None and recorded == before_git
        raise refuse("record-failed",
                     f"기록된 branch=`{(recorded or {}).get('branch')}`"
                     f"{'·무변경(스냅 불가)' if unchanged else ''}")
    return recorded


def switch(home, slot: str, branch: str, *, protected: "list[str] | None" = None,
           git_runner: worktree_records.GitRunner | None = None) -> worktree_records.SwitchResult:
    """슬롯을 **기존** 브랜치로 전환하고 **같은 호출 안에서** 장부 스냅을 재기록한다 — 원자.

    순서(선-검사는 전부 부작용 0 — 하나라도 걸리면 전환/기록 어느 것도 하지 않는다):
      1. **readonly 거부**(·`_reject_readonly_mutation`) — 공유 기준면은 mutation 불가.
      2. **장부 등록 확인** — 미등록이면 `SwitchRefused("unregistered")`(스냅 기록 대상 아님).
      3. **worktree/runner 해소** — 실경로(runner 미주입)에서 슬롯 부재면 `no-worktree`.
      4. **dirty 거부 + rebase 진행중 거부** — 전환이 WIP 를 흔든다·rebase 중 전환은 꼬인다
         (`_rebase_one` 선-검사와 같은 프리미티브 `_is_dirty`/`_rebase_in_progress` 재사용).
         ⚠ **소유(내 세션 leased) 검사는 없다** — `record`/`refresh` 와 같은 위치인자
         pool 관리 표면이고, bind 경로 remedy 는 **바인딩 이전**에 실행되므로 소유를 요구하면
         이 커맨드의 주 용처가 막힌다(rebase 의 not-owner 스킵은 task 일괄 표면 특성).
      5. **브랜치명 정규화**(`_normalize_branch_name` — `check-ref-format --branch` 출력) —
         부적합 이름 거부 + **revspec 확장 해소**(`@{-1}`→실명). 이후 **모든 단계가 이 정규화
         이름 기준**이다(codex must-fix — 원문 기준이면 `@{-1}` 이 보호목록 비교를 우회해 이
         커맨드가 스스로 슬롯을 보호브랜치에 앉힌다). 원문과 다르면 stderr 로 **loud 고지**
         (조용히 다른 브랜치로 가지 않는다).
      6. **보호목록 거부** — 정규화된 브랜치가 그 repo 보호목록이면 거부.
         `protected` 주입 시 그 목록(테스트 hermetic), 아니면 `_resolve_protected(repo)`.
      7. **존재 판정**(`refs/heads/<정규화>`) → **미존재면 `not-found`**(switch 는 브랜치를 만들지
         않는다 — 새 브랜치는 부모를 기록하는 `create_task_branch`) · **타 worktree 점유 거부**
         (`checked-out-elsewhere` — 다른 worktree 가 이미 checkout 중이면 그 경로를 실어 거부·
         우회 없음) + **`@{upstream}` 보호 추적 거부**(`protected-upstream`).
      8. **전환** — 기존 프리미티브 `_checkout_required(slot, <정규화>)` **조합**(raw git 금지):
         `_checkout`(`checkout --no-recurse-submodules <b>`·기존 브랜치를 리셋하지 않는다 + ambient
         `submodule.recurse=true` override) + 성공 직후 `_resync_submodules_selective`
         (detached=pin 재동기·**on-branch(dev)=skip**·dirty=skip+경고·fail-soft). alloc 의 브랜치
         전환 3분기가 쓰는 그 경로다 — 새 프리미티브를 쓰면 이 submodule 보호가 통째로 빠진다
         (codex 게이트 must-fix). 실패는 `CheckoutFailed` → `git-error`. 성공하면 그 브랜치의
         보존 기록(슬롯 제거가 남긴 `pm-slot` 등)을 지운다 — 다시 슬롯에 들어온 브랜치다. 그 지우기
         실패(`BranchParentError`)는 전환이 이미 일어난 뒤라 `record-failed` 로 거부한다.
      9. **스냅 재기록** — 전환·resync **후에** `_rerecord_branch_snapshot`(`record` 서브커맨드와
         동형 판정·실패는 전부 `record-failed`).
    반환 = `SwitchResult`(branch=**정규화 이름**·requested=원문·head). **diverged 검사는
    무변경** — 사람이 raw `git switch` 를 직접 하면 여전히 FAIL-LOUD 로 잡힌다(탐지력 손실 0)."""
    pool_store._reject_readonly_mutation(home, slot, "switch", git_runner=git_runner)
    lease = pool_store.read_lease_strict(home, slot)
    if lease is None:
        raise worktree_records.SwitchRefused(slot, "unregistered")
    repo = getattr(lease, "repo", None) or slot.rpartition("/")[2].rsplit("_", 1)[0]

    runner = git_runner
    if runner is None:
        p = pm_paths.slot_path(home, slot)
        if not p.exists():
            raise worktree_records.SwitchRefused(slot, "no-worktree", detail=str(p))
        runner = worktree_git._real_git_runner(p)

    if worktree_git._is_dirty(pm_paths.slot_path(home, slot), git_runner=runner):
        raise worktree_records.SwitchRefused(slot, "dirty")
    if _rebase_in_progress(home, slot, git_runner=runner):
        raise worktree_records.SwitchRefused(slot, "rebase-in-progress")   # `_rebase_one` 선-검사와 동형 프리미티브.

    # 브랜치명 정규화 — **이 아래 전 단계의 단일 기준**(원문 아님·revspec 확장 해소·codex must-fix).
    target = branch_store._normalize_branch_name(branch, git_runner=runner)
    if target is None:
        raise worktree_records.SwitchRefused(slot, "invalid-ref", detail=f"`{branch}`")
    expansion = "" if target == branch else f"입력 `{branch}` → `{target}` 로 해소됨(revspec 확장)"
    if expansion:
        # 조용한 오전환 방지 — 사용자가 친 문자열과 실제 대상이 다르면 즉시 고지(거부 경로 포함).
        print(f"[알림] 슬롯 {slot} switch: {expansion} — 보호목록 검사·전환·장부 기록은 전부 "
              f"`{target}` 기준으로 진행한다.", file=sys.stderr)

    protected_list = protected if protected is not None else branch_store._resolve_protected(home, repo)
    if target in protected_list:
        detail = f"`{target}`" + (f" ({expansion})" if expansion else "")
        raise worktree_records.SwitchRefused(slot, "protected", detail=detail)

    if not branch_store._local_branch_exists(target, git_runner=runner):
        raise worktree_records.SwitchRefused(slot, "not-found", detail=f"`{target}`")
    # 타 worktree 가 이미 checkout 중인 기존 브랜치로의 전환은 부작용 0 으로 거부한다 —
    # 강행하면 plain checkout 이 실패하고 옛 `-B` 폴백이 그 브랜치 ref 를 이 슬롯 HEAD 로
    # 리셋하는 공유 통합 브랜치 clobber 클래스였다. self(같은 슬롯)가 보유한 브랜치로의
    # 재-switch 는 값 비교로 통과한다(protected-upstream **앞** — 데이터 안전 클래스가 먼저).
    elsewhere = branch_store._branch_checked_out_elsewhere(home, target, slot, git_runner=runner)
    if elsewhere is not None:
        raise worktree_records.SwitchRefused(slot, "checked-out-elsewhere",
                            detail=f"`{target}` → {elsewhere}")
    # 보호브랜치 원격을 추적하는 기존 브랜치로 가면 다음 0단계가 다시 main-참조로
    # 막는다(remedy 가 다른 축의 fault 를 만든다).
    tracked = branch_store._branch_protected_upstream(target, protected_list, git_runner=runner)
    if tracked is not None:
        raise worktree_records.SwitchRefused(slot, "protected-upstream",
                            detail=f"`{target}` → `{tracked}` 추적")

    # ── 전환 = 기존 프리미티브 조합(raw git 금지) ────────────────────────────
    # `_checkout_required` = `_checkout`(--no-recurse-submodules·plain checkout 만) +
    # `_resync_submodules_selective`(on-branch dev 보호·detached 재동기·dirty skip+경고·fail-soft).
    # ambient `submodule.recurse=true` 환경에서 raw 전환은 작업 중 submodule 을
    # detached pin 으로 파괴한다 — 그 보호는 이 쌍에만 있다(alloc 전환 3분기와 동일 경로).
    # `_checkout` 은 `-b`·`-B` 를 쓰지 않는다 — 판정↔실행 경합·타 worktree 점유에도 절대 reset 하지
    # 않는다(불변식 1·프리미티브 층).
    try:
        _checkout_required(home, slot, target, git_runner=runner)
    except worktree_records.CheckoutFailed as exc:
        raise worktree_records.SwitchRefused(slot, "git-error",
                            detail=f"브랜치 전환 실패: {str(exc.output).strip()[:200]}") from exc
    except branch_store.BranchParentError as exc:
        # 보존 기록 지우기 실패 — 전환은 이미 일어났으므로 스냅 재기록 실패와 같은 채널로 거부한다.
        raise worktree_records.SwitchRefused(slot, "record-failed", detail=str(exc)) from exc

    recorded = _rerecord_branch_snapshot(home, 
        slot, target, lease, git_runner=git_runner,
        refuse=lambda reason, detail: worktree_records.SwitchRefused(slot, reason, detail=detail))
    return worktree_records.SwitchResult(slot, target, head=recorded.get("head"), requested=branch)


def create_task_branch(home, slot: str, branch: str, *, parent: str, owner_task: str,
                       protected: "list[str] | None" = None,
                       git_runner: worktree_records.GitRunner | None = None) -> worktree_records.SwitchResult:
    """task 소유 슬롯에 부모 브랜치 끝 커밋에서 새 브랜치를 만들고 부모를 기록한다.

    슬롯 안 새 브랜치 생성의 유일한 엔진 경로다(`switch` 는 기존 브랜치 전환만). 순서 — 앞 단계
    거부는 부작용 0:
      1. readonly 거부(`ReadonlySlotMutation`) · 장부 미등록 `unregistered` · `owner_task` 가 leased 로
         보유한 슬롯이 아니면 `not-owner`.
      2. worktree 부재 `no-worktree` · dirty `dirty` · rebase 진행 중 `rebase-in-progress`.
      3. 브랜치명 정규화(`_normalize_branch_name`) 실패 `invalid-ref` — 이후 전부 정규화 이름 기준.
      4. 보호목록 `protected`(`protected` 주입 또는 `_resolve_protected(repo)`) · 로컬 브랜치 존재
         `exists` · 로컬 브랜치 아닌 ref 로 해소 `ambiguous-ref` · 접두 부모 D/F 충돌 `df-conflict`.
      5. 부모 비교 ref(`parent_ref` — 원격/로컬 중 최신) 없음 `parent-missing` · 그 끝 커밋(fork) 해소
         실패 `git-error`.
      6. `checkout --no-recurse-submodules -b <branch> <fork>` 실패 `git-error` → 성공 뒤
         `_resync_submodules_selective`(switch 전환과 같은 submodule 보호).
      7. 부모 기록(`record_branch_parent`) · 장부 스냅 재기록(`_rerecord_branch_snapshot` — switch 와 같은
         판정) 실패는 `record-failed`(브랜치는 이미 만들어졌다).
    반환 = `SwitchResult`(branch=정규화 이름·head=기록 head·requested=원문)."""
    pool_store._reject_readonly_mutation(home, slot, "task branch", git_runner=git_runner)
    lease = pool_store.read_lease_strict(home, slot)
    if lease is None:
        raise worktree_records.TaskBranchRefused(slot, "unregistered")
    if lease.state != "leased" or lease.session != owner_task:
        raise worktree_records.TaskBranchRefused(slot, "not-owner",
                                detail=f"task {owner_task!r} · 보유 명의 {lease.session!r}")

    runner = git_runner
    if runner is None:
        p = pm_paths.slot_path(home, slot)
        if not p.exists():
            raise worktree_records.TaskBranchRefused(slot, "no-worktree", detail=str(p))
        runner = worktree_git._real_git_runner(p)
    if worktree_git._is_dirty(pm_paths.slot_path(home, slot), git_runner=runner):
        raise worktree_records.TaskBranchRefused(slot, "dirty")
    if _rebase_in_progress(home, slot, git_runner=runner):
        raise worktree_records.TaskBranchRefused(slot, "rebase-in-progress")

    target = branch_store._normalize_branch_name(branch, git_runner=runner)
    if target is None:
        raise worktree_records.TaskBranchRefused(slot, "invalid-ref", detail=f"`{branch}`")
    protected_list = protected if protected is not None else branch_store._resolve_protected(home, lease.repo)
    if target in protected_list:
        raise worktree_records.TaskBranchRefused(slot, "protected", detail=f"`{target}`")
    if branch_store._local_branch_exists(target, git_runner=runner):
        raise worktree_records.TaskBranchRefused(slot, "exists", detail=f"`{target}`")
    kind = branch_store._non_branch_ref_kind(target, git_runner=runner)
    if kind is not None:
        raise worktree_records.TaskBranchRefused(slot, "ambiguous-ref", detail=f"`{target}` = {kind}")
    conflict = branch_store._branch_df_conflict(target, git_runner=runner)
    if conflict is not None:
        raise worktree_records.TaskBranchRefused(slot, "df-conflict",
                                detail=f"`{target}` vs 기존 브랜치 `{conflict}`")

    ref = branch_store.parent_ref(parent, git_runner=runner)
    if ref is None:
        raise worktree_records.TaskBranchRefused(slot, "parent-missing", detail=f"`{parent}`")
    rc, out = runner(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    fork = str(out).strip()
    if rc != 0 or not fork:
        raise worktree_records.TaskBranchRefused(slot, "git-error",
                                detail=f"부모 `{ref}` 커밋 해소 실패 (rc={rc}): {fork[:200]}")
    rc, out = runner(["checkout", "--no-recurse-submodules", "-b", target, fork])
    if rc != 0:
        raise worktree_records.TaskBranchRefused(slot, "git-error",
                                detail=f"`checkout -b {target}` 실패 (rc={rc}): {str(out).strip()[:200]}")
    submodule_git._resync_submodules_selective(pm_paths.slot_path(home, slot), git_runner=runner)

    try:
        branch_store.record_branch_parent(target, parent, fork, git_runner=runner)
    except branch_store.BranchParentError as exc:
        raise worktree_records.TaskBranchRefused(slot, "record-failed",
                                detail=f"`{target}` 부모 기록 실패: {exc}") from exc
    recorded = _rerecord_branch_snapshot(home, 
        slot, target, lease, git_runner=git_runner,
        refuse=lambda reason, detail: worktree_records.TaskBranchRefused(slot, reason, detail=detail))
    return worktree_records.SwitchResult(slot, target, head=recorded.get("head"), requested=branch)


def _checkout_required(home, slot: str, branch: str, *,
                       git_runner: worktree_records.GitRunner | None = None) -> None:
    """`_checkout` 을 부르고 실패(rc≠0)면 `CheckoutFailed` raise.

    fail-soft 로 무시하면 호출부가 장부 branch/state 를 성공처럼 갱신해 장부↔실제 worktree
    branch 가 어긋난다. 성공해야만 호출부가 장부를 갱신하도록 강제하는 가드.

    체크아웃 성공 직후 `_resync_submodules_selective` 로 submodule 을 새 브랜치 pin 에 selective
    재동기한다(detached=consume 만 재동기·on-branch=dev skip·dirty
    skip+경고). *브랜치 전환* 경로(alloc 세 checkout 분기·switch)에만 붙는다 — `create_slot` 최초 init
    은 이 함수를 안 타고 자체 `submodule update --init --recursive --force`(fresh=전부 detached)를
    유지한다. 재동기는 fail-soft(raise 안 함)라 checkout 성공을 되돌리지 않는다.

    없는 브랜치·타 worktree 점유 브랜치는 `CheckoutFailed` 로 loud 실패한다(브랜치를 만들거나 리셋하지
    않는다 — alloc `--branch` 에 없는 이름을 줘도 만들지 않는다).

    체크아웃 성공 뒤 도착한 브랜치의 보존 기록(`clear_preserved_branch`)을 지운다 — 기존 브랜치가
    슬롯에 들어오는 엔진 경로(switch·alloc)가 모두 이 함수를 지나므로, 다시 쓰이는 브랜치는 "슬롯 없는
    보존 브랜치" 목록에서 빠진다. 지우기 실패는 `BranchParentError` 로 올린다.
    """
    rc, out = submodule_git._checkout(pm_paths.slot_path(home, slot), branch, git_runner=git_runner)
    if rc != 0:
        raise worktree_records.CheckoutFailed(slot, branch, out)
    arrived = worktree_git.current_branch(home, slot, git_runner=git_runner)
    if arrived is not None:
        branch_store.clear_preserved_branch(arrived, git_runner=git_runner or worktree_git._real_git_runner(pm_paths.slot_path(home, slot)))
    submodule_git._resync_submodules_selective(pm_paths.slot_path(home, slot), git_runner=git_runner)


def dev(home, slot: str, sub: str, branch: str, *, git_runner: worktree_records.GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 의 submodule 을 dev 브랜치로 지정한다 — on-branch 화.

    `git -C <sub> checkout -b <branch>`(신규 생성)로 submodule 을 on-branch 로 만든다. 브랜치가
    이미 있으면 `-b` 가 rc≠0 이므로 `git -C <sub> checkout <branch>`(전환)로 폴백한다(`_checkout`
    의 create-or-switch 정신과 정합·단 submodule 컨텍스트). 결과적으로 그 submodule 의 live git
    HEAD 가 symbolic ref(on-branch)가 되어, 이후 `_resync_submodules_selective`가 그
    submodule 을 **dev 역할로 판별해 skip** 한다(detached pin 으로 낚아채지 않음)
    — 즉 "이 submodule 은 내가 작업 중이니 pin 재동기로 건드리지 마" 선언(무스키마·역할은 별도
    장부 없이 submodule HEAD 로 정함).

    **`sub` 슬롯-경계 검증(codex must-fix)**: `dev` 는 실 git side-effect(`checkout`)를
    caller-공급 경로에 낸다. 이 도구는 LLM-구동 pm-internal 이라 `sub` 가 자연어에서 구성된다
    (hallucination/오타) — 절대경로(`/etc/...`)·`..` traversal·목록 밖 경로를 그대로 `git -C <sub>
    checkout` 에 넘기면 **슬롯 밖 다른 git repo 에 checkout** 을 실행해 "대상 슬롯 운영" 경계를
    깬다. 그래서 checkout 전에 `sub` 를 그 슬롯의 실제 submodule 목록(`git submodule status` →
    `_parse_submodule_paths`)과 **대조(allowlist)** 하고, 목록 밖이면 `SubmoduleNotInSlot` raise
    (fail-closed — status 조회 실패/submodule 없음도 빈 목록 → 거부). allowlist 라 절대경로·
    traversal 은 자동 거부(목록의 값은 항상 슬롯-상대 submodule 경로).

    `sub` 는 **슬롯 worktree 상대 submodule 경로**(`git submodule status` 가 나열하는 경로·예
    `vendor/sub`). runner 는 슬롯 worktree 바인딩(`_real_git_runner(slot_path(slot))`) + 다중 `-C`
    (`["-C", sub, ...]`)로 superproject cwd 기준 submodule 에 재진입한다(`_submodule_dirty`·
    `_resync_submodules_selective` 와 동형 배선). `(rc, out)` 반환 — 호출부(CLI)가 rc≠0 를 명시
    에러로 surface 한다. `git_runner` 주입 시 그 runner(테스트 mock·DI seam 보존).

    **readonly 거부**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise —
    dev 는 submodule 을 on-branch 로 checkout 하는 mutation 이라 read-only 기준면엔 불가(진입 가드).
    """
    pool_store._reject_readonly_mutation(home, slot, "dev", git_runner=git_runner)
    runner = git_runner or worktree_git._real_git_runner(pm_paths.slot_path(home, slot))
    # 슬롯 경계 검증(must-fix 1) — sub 를 슬롯 실제 submodule 목록과 대조(allowlist·fail-closed).
    rc_st, out_st = runner(["submodule", "status"])
    known = submodule_git._parse_submodule_paths(out_st) if rc_st == 0 else []
    if sub not in known:
        raise worktree_records.SubmoduleNotInSlot(slot, sub, known)
    classified = submodule_git._classify_unset_submodules(runner, submodule_git._parse_submodule_entries(out_st), target=slot)
    if classified.get(sub) in ("outside", "uninitialized"):
        return 1, f"submodule {sub!r} 은 폴더 밖이거나 초기화 안 됨, sync 후 dev를 지정한다"
    rc, out = runner(["-C", sub, "checkout", "-b", branch])
    if rc != 0:
        # `-b` 실패 원인 판별(codex bundle) — 브랜치가 *이미 존재*할 때만 전환 폴백한다. 그 외
        # (충돌/lock/잘못된 브랜치명)의 `-b` 실패까지 폴백하면 진단이 흐려지므로, `show-ref
        # --verify --quiet refs/heads/<branch>` rc0(존재)일 때만 plain `checkout <branch>` 로
        # 전환하고, 미존재(rc≠0)면 원 `-b` 실패(rc/out)를 그대로 전파한다(원인 명확).
        rc_ref, _out_ref = runner(
            ["-C", sub, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
        )
        if rc_ref == 0:
            rc, out = runner(["-C", sub, "checkout", branch])
    return rc, out


def sync(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None) -> None:
    """현재 슬롯의 submodule 을 superproject pin 에 selective 재동기(수동 트리거) —.

    `_resync_submodules_selective` 를 **브랜치 전환 없이** 수동으로 부른다 — 브랜치를
    바꾸지 않고 명시적으로 submodule 상태를 pin 에 맞추고 싶을 때의 진입(부트스트랩/checkout 경로
    밖). 판별·거동은 전부 그 backbone 이 소유한다(중복 구현 금지·얇은 트리거):
      - detached(consume) & clean → `git submodule update --init --recursive --force -- <sub>` 로 pin 재동기.
      - on-branch(dev) → skip(dev 작업 보호·크럭스 A).
      - detached & dirty → skip + 경고(미커밋 작업 유실 방지).
      - submodule 없는 슬롯 → no-op.
    fail-soft(raise 금지·경고는 stderr) — `_resync_submodules_selective` 계약 상속. `git_runner`
    주입 시 그 runner(테스트 mock·DI seam), 미주입이면 슬롯 worktree 바인딩 실 runner.

    **readonly 거부**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise —
    sync 는 submodule 을 pin 으로 재동기(checkout)하는 mutation 이라 read-only 기준면엔 불가(진입
    가드·fail-soft 계약보다 우선 — 이건 op 전면 거부지 조회 실패가 아니다).
    """
    pool_store._reject_readonly_mutation(home, slot, "sync", git_runner=git_runner)
    submodule_git._resync_submodules_selective(pm_paths.slot_path(home, slot), git_runner=git_runner)


def _rebase_skip_reason(home, reason: "str | None") -> str:
    """rebase 스킵 사유 코드 → 사람이 읽는 loud 설명 (CLI)."""
    if reason and reason.startswith("not-owner:"):
        holder = reason.split(":", 1)[1]
        # 보유자 명의는 세션일 수도 task 일 수도 있다 — "세션" 으로 단정하면 task 모드에서
        # 안내가 틀린 축을 가리킨다(codex 게이트 suggestion).
        msg = f"명의 {holder!r} 이(가) 보유 — 내 슬롯 아님(rebase 차단·소유검사)"
        if pool_store._is_registered_task(home, holder):
            # 보유자가 등록 task = 소유 축이 task 다— 해소 커맨드를 실값으로 안내.
            msg += (f" · 이 슬롯이 내 task 명의면 `--task {holder}` 를 붙여 그 명의로 rebase 하라"
                    " (소유검사)")
        return msg
    return {
        "readonly": "readonly 공유 슬롯 — mutation 불가(refresh 로만 갱신·)",
        "not-owner": "내 명의(세션/task) 소유(leased) 슬롯이 아니다 — 남의/미점유 슬롯 rebase 차단",
        "dirty": "미커밋 변경(dirty) — rebase 는 clean 전제(정리 후 재시도)",
        "in-progress": "이미 rebase 진행 중 — `git rebase --continue|--abort` 로 먼저 해소",
        "no-branch": "슬롯이 브랜치 위에 있지 않다(detached) — rebase 할 브랜치가 없다",
        "no-parent": "브랜치 부모 기록 없음 + --onto 없음 — `--onto <부모 브랜치>` 로 옮길 부모를 지정하라(추론 금지)",
        "unresolvable-onto": "`--onto` ref 해소 실패(오타·미fetch — 실재 브랜치/커밋 지정)",
        "no-worktree": "슬롯 worktree 경로 부재",
    }.get(reason or "", reason or "미상")
