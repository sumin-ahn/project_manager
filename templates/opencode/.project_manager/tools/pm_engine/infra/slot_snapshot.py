"""슬롯 git 스냅 기록과 live 상태 비교."""
from __future__ import annotations


from pm_engine.domain import worktree_records
from pm_engine.infra import clock, pm_paths, pool_store, submodule_git, worktree_git


def _slot_head(git_runner: worktree_records.GitRunner) -> "str | None":
    """슬롯 worktree HEAD 커밋 sha (live·`git rev-parse HEAD`·rc≠0 은 None·runner 예외는 전파)."""
    rc, out = git_runner(["rev-parse", "HEAD"])
    if rc != 0:
        return None
    return out.strip() or None


def _snapshot_submodule_pins(git_runner: worktree_records.GitRunner) -> "list[dict]":
    """`git submodule status` → `[{path, pin}]` (기록=기대 축).

    pin = submodule 의 현재 체크아웃 sha(선두 flag 제거) — "여기 두고 간다"의 기준값(재개 시 이
    sha 와 달라지면 drift·`_submodule_pin_drift`). `_submodule_statuses`와 같은 `submodule
    status` primitive 를 읽되 sha 만 뽑는다. rc≠0(조회 불가/submodule 없음) → 빈 목록, runner 예외는 전파.
    """
    rc, out = git_runner(["submodule", "status"])
    if rc != 0:
        return []
    pins: list[dict] = []
    for line in out.splitlines():
        # 경로는 공용 파서 단일 진실(`_parse_submodule_entries`) — 공백 경로·describe 를
        # 손실 없이 처리한다. sha 는 첫 토큰(flag+sha)에서 뽑는다(경로 공백과 무관).
        entries = submodule_git._parse_submodule_entries(line)
        if not entries:
            continue
        raw = line.split(maxsplit=1)[0]
        sha = raw[1:] if raw[:1] in ("+", "-", "U") else raw
        _flag, path = entries[0]
        pins.append({"path": path, "pin": sha})
    return pins


def _snapshot_slot_git(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None) -> "dict | None":
    """슬롯의 live git 상태를 스냅한다 — `{branch, head, submodules, recorded_at}` (base 제외).

    write 프리미티브의 원료(base 는 호출부가 결정: create=새 기록·alloc/bind=기존 보존). git rc≠0
    은 각 칸의 None·빈 값이고 runner 예외는 전파한다. 슬롯 경로 부재 가드는 **실경로
    (git_runner 미주입)에서만** 본다 — 주입 runner(hermetic 테스트)는 존재/HEAD 를 모델링하는
    권위라 건너뛴다. 슬롯 부재(실경로)면 None(스냅 불가 → 호출부가 기존 git 을 유지)."""
    runner = git_runner
    if runner is None:
        path = pm_paths.slot_path(home, slot)
        if not path.exists():
            return None
        runner = worktree_git._real_git_runner(path)
    return {
        "branch": worktree_git.current_branch(home, slot, git_runner=runner),
        "head": _slot_head(runner),
        "submodules": _snapshot_submodule_pins(runner),
        "recorded_at": clock._now_local(),
    }


def _apply_git_snapshot(home, lease: "Lease", *, base_branch: "str | None" = None,
                        git_runner: worktree_records.GitRunner | None = None) -> None:
    """`lease.git` 을 live 슬롯 스냅으로 갱신한다 — write 프리미티브 본체(in-place).

    base 는 **readonly 슬롯 전용**이다(브랜치가 없어 따라갈 부모 브랜치를 장부에 둔다). readonly lease 에
    `base_branch` 주어짐(생성·refresh) = base 를 `{branch, commit=방금 스냅한 head}` 로 **새로** 기록 /
    미주어짐 = 기존 `lease.git.base` **보존**. 작업 슬롯은 `base_branch` 와 무관하게 base 가 없다(부모는
    git 설정 `branch.<branch>.pm-parent` 가 진실 — 옛 장부 base 는 다음 스냅에서 빠진다).
    스냅 불가(슬롯 부재 등 None)면 기존 git 을 clobber 하지 않고 그대로 둔다(silent 손실 방지).
    스냅 예외는 전파한다.
    """
    snap = _snapshot_slot_git(home, lease.slot, git_runner=git_runner)
    if snap is None:
        return  # 슬롯 부재/스냅 불가 → 기존 git 유지(clobber 안 함).
    if lease.role != pool_store._LEASE_ROLE_READONLY:
        base = None
    elif base_branch is not None:
        base = {"branch": base_branch, "commit": snap.get("head")}
    else:
        base = lease.git.get("base") if isinstance(lease.git, dict) else None
    if base is not None:
        snap = {"base": base, **snap}   # base 를 앞에(스키마 순서·cosmetic).
    lease.git = snap


def record_git_snapshot(home, slot: str, *, base_branch: "str | None" = None,
                        git_runner: worktree_records.GitRunner | None = None) -> "Lease | None":
    """슬롯 현재 git 상태를 `lease.git` 에 기록하는 독립 write 프리미티브 (핸드오프·rebase·switch).

    alloc·create 는 자기 lock 안에서 `_apply_git_snapshot` 을 인라인 호출하지만,
    핸드오프("여기 두고 간다")·rebase·switch·refresh 처럼 lifecycle op 밖에서 스냅을 찍는 호출부를
    위한 standalone 진입점이다. `base_branch` 는 readonly 전용(refresh — 따라갈 부모 브랜치 재기록).
    `_lease_lock`+`_write_ledger`(atomic) — alloc/release 와 동일한 read-modify-write
    직렬화. 장부에 슬롯이 없으면 None(무해). 갱신된 Lease 반환."""
    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            return None
        _apply_git_snapshot(home, target, base_branch=base_branch, git_runner=git_runner)
        pool_store._write_ledger(home, leases)
        return target


def _is_ancestor(git_runner: worktree_records.GitRunner, ancestor: str, descendant: str) -> bool:
    """`git merge-base --is-ancestor <ancestor> <descendant>` — ancestor 가 descendant 의 조상인가.

    rc0=조상(True)·rc1=아님(False)·rc>1=False(runner 예외는 전파·보수적 — 판정 불가는 "후손 아님"으로 →
    FAIL-LOUD 쪽으로 기운다·경보를 놓치기보다 잘못 울리는 게 낫다). git 결정론 판정(추측 아님)."""
    rc, _out = git_runner(["merge-base", "--is-ancestor", ancestor, descendant])
    return rc == 0


def _head_relation(recorded_branch: "str | None", recorded_head: "str | None",
                   live_branch: "str | None", live_head: "str | None",
                   *, git_runner: worktree_records.GitRunner | None) -> str:
    """기록 head vs live head 관계 판정 (`merge-base --is-ancestor` 완화).

    - head 한쪽이라도 미상 → `unknown`(판정 불가·통과 취급).
    - 브랜치 변경 → `diverged`(사고·FAIL-LOUD).
    - 같은 branch·head 동일 → `match`(통과).
    - 같은 branch·head 다름 & live 가 기록 head 의 **후손** → `descendant`(crash 후 재개·내 커밋·
      notice·통과). crash 후 재개(핸드오프 못 하고 죽음)를 경보 소음으로 만들지 않는다.
    - 같은 branch·head 다름 & **비후손**(리셋·되감기·divergent) → `diverged`(FAIL-LOUD)."""
    if not recorded_head or not live_head:
        return worktree_records.HEAD_UNKNOWN
    if recorded_branch != live_branch:
        return worktree_records.HEAD_DIVERGED
    if recorded_head == live_head:
        return worktree_records.HEAD_MATCH
    if git_runner is not None and _is_ancestor(git_runner, recorded_head, live_head):
        return worktree_records.HEAD_DESCENDANT
    return worktree_records.HEAD_DIVERGED


def _submodule_pin_drift(recorded_subs: "list", live_subs: "list") -> "list[str]":
    """기록 submodule pin ≠ live pin 인 path 목록 (pin/drift 대칭·경고 축)."""
    live_map = {
        s["path"]: s.get("pin") for s in live_subs
        if isinstance(s, dict) and "path" in s
    }
    drift: list[str] = []
    for s in recorded_subs or []:
        if not isinstance(s, dict) or "path" not in s:
            continue
        if live_map.get(s["path"]) != s.get("pin"):
            drift.append(s["path"])
    return drift


def compare_slot_git(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None) -> worktree_records.GitCompareResult:
    """기록된 `lease.git`(기대) vs 슬롯 live 상태를 비교한다 — compare 프리미티브 (0단계 소비).

    장부에서 슬롯 lease 를 읽어 그 `git` 스냅(기대)을 live 스냅과 대조한다. `_lease_lock` 은 장부
    read 동안만 짧게 잡고 git 조회(subprocess)는 lock 밖에서 한다. 미기록(git 필드 없음/슬롯 없음)
    이면 `unrecorded=True`(drift 감지 비활성). head 비교는 `merge-base --is-ancestor` 로
    완화(crash 후 재개를 경보 소음으로 안 만든다). 판정 정책(FAIL/notice/질의)은 0단계
    가 `ok`/`fail_loud`/`unrecorded`/`head_relation` 을 읽어 결정한다(엔진=surface·PM=확인)."""
    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger(home)
    target = next((l for l in leases if l.slot == slot), None)
    recorded = target.git if (target is not None and isinstance(target.git, dict)) else None
    live = _snapshot_slot_git(home, slot, git_runner=git_runner) or {}
    if recorded is None:
        return worktree_records.GitCompareResult(
            slot, unrecorded=True, recorded=None, live=live,
            branch_match=False, head_relation=worktree_records.HEAD_UNKNOWN, submodule_drift=[],
        )
    # head 비교용 runner — 주입 우선, 아니면 슬롯 실경로(부재면 None → is-ancestor 스킵·unknown).
    runner = git_runner
    if runner is None and pm_paths.slot_path(home, slot).exists():
        runner = worktree_git._real_git_runner(pm_paths.slot_path(home, slot))
    return worktree_records.GitCompareResult(
        slot,
        unrecorded=False,
        recorded=recorded,
        live=live,
        branch_match=recorded.get("branch") == live.get("branch"),
        head_relation=_head_relation(
            recorded.get("branch"), recorded.get("head"),
            live.get("branch"), live.get("head"), git_runner=runner,
        ),
        submodule_drift=_submodule_pin_drift(
            recorded.get("submodules") or [], live.get("submodules") or []
        ),
    )
