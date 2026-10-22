#!/usr/bin/env python3
"""worktree CLI 진입점.

슬롯·task·장부·git 작업은 pm_engine 패키지가 소유한다.
CLI는 스크립트 위치에서 Home을 만들고 실제 소유 모듈을 직접 호출한다.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.domain import identity_args  # noqa: E402
from pm_engine.infra import console, lease_store, repo_files  # noqa: E402
from pm_engine.infra import pm_paths
from pm_engine.infra import worktree_paths
from pm_engine.infra import pool_store
from pm_engine.app import slot_resolution
from pm_engine.app import worktree_status
from pm_engine.infra import slot_snapshot
from pm_engine.app import branch_checkout
from pm_engine.infra import worktree_git
from pm_engine.domain import worktree_records


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.8.1"


def _is_engine_rev_skew(exc):
    """fail-soft 소비 지점에서 rev skew만 재-raise하기 위한 구조화 판정."""
    return getattr(exc, "_engine_rev_skew", False)


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·pm_*.py 와 동일 앵커 관례.
# multi-PM 모델에서 이 도구가 어느 worktree cwd 에서 호출돼도 자기 위치(multi-PM 루트 .project_manager)를
# 자동 타깃한다.
REPO = Path(__file__).resolve().parents[2]
LOCAL_DIR = REPO / ".project_manager" / ".local"               # per-clone scratch (git-ignored)


# ── task 컬렉션 (top-level `tasks`·슬롯과 직교) ───────────────────────
# 리스 장부 파일의 top-level `tasks` 배열에 산다(leases 와 형제·같은 `_lease_lock`/atomic).
# 슬롯 0개로도 존재 가능(task 는 alloc 전에 먼저 생긴다). 동시 세션 거부는 열림 기록(`open`)
# 하나로 판정한다 — 프로세스 조회 0.


# ── 열린 할 일 (task 별 파일) ────────────────────────────────────────────────
# 저장 위치가 장부가 아니라 task 폴더인 이유: 할 일은 세션 중 수시로 바뀌고 슬롯 대여는 거의
# 안 바뀐다. 한 파일에 두면 할 일 하나 넣을 때마다 남의 대여 행까지 통째로 다시 쓰고, 세 task 가
# 같은 락을 두고 겨룬다. 폴더 안에 두면 task 종료(`end_task` 의 폴더 이동)에 그대로 따라간다.


# ── git worktree × 장부 정합 (reconcile·중단-안전 슬롯번호) ────────────────
# 장부(Lease)는 우리 메타(session/test_cmd)이고, 실제 worktree 는 git 이 소유한다
# (`.repos/<repo>.git` bare 의 worktree 목록). create_slot 이 worktree add 성공
# 후 lease 기록 전에 죽으면 둘이 어긋난다(orphan·audit #2). git worktree 목록을 *실-git 소스*
# 로 삼아 (a) 슬롯번호를 orphan 까지 회피(#4)하고 (b) status 가 drift 를 surface(#3)한다.


# ── 슬롯 git 진실: 스냅 기록(write) + 비교(compare) + is-ancestor 판정 ───────
# 슬롯 git 상태를 *기대*(drift 감지 기준) 축으로 lease 장부에 기계 기록한다(live 표시는
# 그대로·2축). submodule 의 pin/drift 모델을 본체(superproject)로 대칭 확장 — 개념
# write=alloc·핸드오프·create(release 시 정리)·compare=0단계.


# ── 브랜치 부모 기록 (공용 git 저장소 설정 `branch.<브랜치>.pm-parent`·`pm-fork`) ──
# 부모 브랜치 = 이 브랜치를 만든 브랜치이자 다시 합칠 곳 · 갈라진 커밋 = 만든 시점의 부모 커밋.
# 기록은 브랜치를 만드는 엔진 명령(create_slot·create_task_branch)과 rebase 만 쓴다. 저장 위치가 git
# 설정이라 `git branch -m` 은 기록을 새 이름으로 옮기고 `git branch -d` 는 함께 지운다(장부 동기화
# 코드 없음). worktree 에서 부른 `git config` 도 공용 저장소 설정에 쓴다. 기록 없음은 추정하지 않는다.


# ── 슬롯 없는 보존 브랜치 기록 (공용 git 저장소 설정 `branch.<브랜치>.pm-slot` 등) ──
# 슬롯 제거가 브랜치를 보존하면 어느 슬롯에서 쓰던 브랜치인지·언제·왜 남았는지를 부모 기록과 같은
# 자리에 쓴다. 보호·기본 브랜치 보존은 쓰지 않는다 — 슬롯 수명과 무관한 브랜치라 "슬롯 없는 보존
# 브랜치" 가 아니다. 브랜치가 지워지면 git 이 설정도 함께 지우고, 그 브랜치가 다시 슬롯에
# 체크아웃되면(`_checkout_required`) 엔진이 지운다. 조회는 `preserved_branches` 한 곳이다.


# ── readonly 공유 슬롯 — role 조회 / mutation 거부 / refresh ──
# readonly 슬롯은 코드를 *읽어* PM 홈 wiki(domain·architecture·status)를 쓰는 research 기준면이다:
# 슬롯 자체는 read-only(detached·배타 대여 없음)이고, mutation op(rebase·switch·dev·sync)은 **엔진
# 경로에서 거부**한다(fs 레벨 쓰기 차단은 안 함). 갱신은 `refresh`(fetch → detach 이동·dirty=
# 거부+loud)만 허용한다 — read-only 슬롯의 dirty 는 "누군가 여기 썼다"는 신호라 조용히 reset 하지 않는다.


# ── rebase (단일/일괄·선-검사·충돌 그대로+loud·부모 기록 갱신·자동 rebase 없음) ──
# 슬롯 브랜치를 부모 브랜치 최신 위로 사용자 명시로만 옮긴다(자동 rebase 없음). 슬롯마다 독립 처리:
# 선-검사(소유/dirty/rebase 진행중/브랜치/부모) → 실패면 스킵+loud, 통과면 부모 최신 fetch → git rebase.
# 충돌은 **그 상태 그대로 두고 fail-loud**(엔진 임의 abort 금지 — 해소는 사용자 git rebase --continue|
# --abort·다음 부트스트랩 0단계가 "rebase 진행 중" 으로 감지·안내). 기록 갱신은 **성공 시에만**
# (부모 기록 fork=새 부모 tip·`--onto` 면 부모 이름도 교체 → 장부 스냅 head/recorded_at). 부모 기록
# 없음 + --onto 없음 = 거부(추론 금지).


# ── switch (기존 브랜치 전환 + 장부 스냅 재기록 **원자**) · create_task_branch (새 브랜치 + 부모 기록) ──
# 엔진이 매개하는 브랜치/head 전환은 전부 자기가 스냅을 재기록한다(bind·create·rebase·refresh·
# handoff). raw git 전환은 장부 스냅이 옛 브랜치를 가리킨 채라 **곧바로 0단계 '기록↔live diverged'**
# 차단을 부른다. `switch` 는 기존 브랜치 전환과 스냅 재기록을 **한 호출**에 담고, 새 브랜치는
# `create_task_branch` 만 만든다(생성 시점에 부모 기록·task 소유 슬롯 한정).
#   - diverged 검사 자체는 **무변경** — 사람이 raw `git switch` 를 직접 하면 여전히 FAIL-LOUD 로
#     잡힌다(외부 개입 탐지력 손실 0). 엔진 경로만 스냅을 동반한다.
#   - 보호목록 브랜치로의 전환은 **거부** — 이 커맨드의 목적(main-참조 해소)과 정반대다.


# ── 운영중 관리 backbone: dev/sync + CLI ─────────
# worktree/submodule 운영 *중* 관리를 명령어化 하기 위한 두 backbone + argparse 진입점.
# pm-worktree 스킬(어댑터·PM authoring)이 이 커맨드를 얇게 래핑한다 — 백본 로직은 전부 여기.
#   - dev <sub> <branch> : 슬롯 worktree 의 submodule 을 dev 브랜치로 지정(on-branch 화) → 이후
#       selective resync(`_resync_submodules_selective`)가 그 submodule 을 dev 로 판별해 skip
#       (detached pin 으로 낚아채지 않음·live-HEAD 모델). "내가 작업 중" 선언.
#   - sync              : 현재 슬롯의 `_resync_submodules_selective` 를 수동 트리거(브랜치 전환
#       없이 명시 재동기·detached=pin 재동기·on-branch=skip·dirty=skip+경고).
# 얇게 — 기존 primitive(`_resync_submodules_selective`·submodule 판별)를 재사용한다(중복 금지).


# ── status CLI 핸들러 (위치인자 <slot> — pool-management op·명시 슬롯) ──
# dev/sync 의 --repo/--slot identity 와 달리 대상 슬롯을 **위치인자**로 직접 받는다: status 는 자기
# 세션 슬롯이 아닌 임의 슬롯도 조회 대상이라(pool 관리) 슬롯을 명시 지정한다. 위치인자 생략 시
# cwd/세션 leased 로 해소(무인자=내 슬롯).


def _print_status_row(row: dict) -> None:
    """슬롯 git 구성 dict 렌더(role·branch·head·부모·자식·stash·dirty·submodule pin/drift).

    부모는 `<parent>@<갈라진 커밋 12자> · 앞 N · 뒤 M`, 기록 없음은 `(부모 기록 없음)`, 부모 브랜치가
    지워졌으면 `<parent>@<커밋> (부모 브랜치 없음)`. submodule 은 역할별 경고 마크(⚠=drift/
    uninitialized··=pinned/dev-ahead)로 표시(빈 목록=submodule 없는 슬롯 → 줄 생략)."""
    slot = row["slot"]
    parent = row.get("parent")
    if parent is None:
        parent_str = "(부모 기록 없음)"
    else:
        parent_str = f"{parent['name']}@{(parent.get('fork') or '?')[:12]}"
        if parent.get("ref") is None:
            parent_str += " (부모 브랜치 없음)"
        else:
            parent_str += f" · 앞 {parent['ahead']} · 뒤 {parent['behind']}"
    children = row.get("children") or []
    print(f"# 슬롯 {slot} git 구성 (조회 — 손-git 불요)")
    print(f"  role:   {row.get('role') or pool_store._slot_role(pm_paths.home_at(REPO), slot)}")   # work | readonly
    print(f"  branch: {row.get('branch') or '(detached/미상)'}")
    print(f"  head:   {(row.get('head') or '(미상)')[:12]}")
    print(f"  parent: {parent_str}")
    print(f"  children: {', '.join(children) if children else '(없음)'}")
    print(f"  stashes: {row.get('stashes', 0)}")
    print("  folders: " + (", ".join(row["folders"]) if row.get("folders") is not None else "(제한 없음)"))
    print(f"  dirty:  {'예 (미커밋 변경 있음)' if row.get('dirty') else '아니오 (clean)'}")
    for s in (row.get("submodules") or []):
        mark = "⚠" if getattr(s, "warning", False) else "·"
        dirty_tag = " ·dirty" if getattr(s, "dirty", False) else ""
        print(f"  submodule {mark} {s.path}: {s.kind}{dirty_tag}")


def _cmd_status(args) -> int:
    """`status [<slot>] [--task <task>]` CLI 핸들러 — 슬롯 git 구성 조회 단일/일괄.

    대상: `--task <task>`(그 task 보유 전 슬롯 일괄) > 위치인자 `<slot>`(단일) > 무인자(내 task 전
    슬롯·`_default_session` 유입). 슬롯별 branch·head·부모·자식·stash·submodule pin/drift·dirty·role 을
    조회한다(`status` 백본)."""
    task = getattr(args, "task", None)
    slot_arg = getattr(args, "slot", None)
    if task and slot_arg:
        print("[중단] status 는 `<slot>`(단일) 또는 `--task <task>`(일괄) 중 하나만 받는다.",
              file=sys.stderr)
        return 1
    try:
        if task:
            rows = worktree_status.status(pm_paths.home_at(REPO), task=task)
        elif slot_arg:
            rows = worktree_status.status(pm_paths.home_at(REPO), slot=slot_arg)
        else:
            rows = worktree_status.status(pm_paths.home_at(REPO))   # 무인자 = 내 task 전 슬롯.
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except worktree_records.SlotResolutionError as exc:
        print(f"[중단] 대상 슬롯 해소 실패 — {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("# 조회 대상 슬롯 없음 — 내 세션 명의로 보유한 leased 슬롯이 없다 "
              "(task 모드면 `--task <task>`, 아니면 `<slot>` 으로 대상을 명시하라).")
        return 0
    for i, row in enumerate(rows):
        if i:
            print()   # 슬롯 간 구분 공백(일괄).
        _print_status_row(row)
    return 0


def _cmd_rebase(args) -> int:
    """`rebase <slot> [--task <task>] [--onto <b>]` (단일) · `rebase --task <task> [--onto <b>]` (일괄)
    CLI 핸들러.

    슬롯 독립 처리 — 선-검사(소유/dirty/rebase 진행중) 스킵 + 충돌 그대로 fail-loud(엔진 abort 안
    함) + 성공 시 장부 원자 갱신. 끝에 성공/스킵/충돌 요약. 단일은 성공해야 rc 0(스킵/충돌=rc 1),
    일괄은 충돌이 있으면 rc 1(주의 필요·나머지는 독립 진행).

    **`--task` 는 두 역할이고 위치인자와 배타가 아니다**: 대상 *선택*(위치인자 없으면
    그 task 보유 전 슬롯 일괄) 소유 *명의*(`rebase(owner_task=)` — release 동형). 그래서
    `rebase <slot> --task <task>` = 그 task 명의로 **단일** 슬롯 rebase 다 — 종전엔 둘이 배타라
    task 명의 슬롯을 단일 지정으로 rebase 할 방법이 아예 없었다. 위치인자만이면 종전대로 세션
    명의로 판정한다(슬롯-세션 모드 불변).

    ⚠️ **선행조건**: 활성 백그라운드 위임(dev) 중인 슬롯은 rebase 하지 마라 — 서브에이전트는
    하네스 안 프로세스라 엔진이 못 본다(기계 신호 부재·[[parallel-dev-shared-tree-clobber]] 변형).
    스킬/카드에 명문화·실행 전 사용자 확인."""
    task = args.task
    slot_arg = args.slot
    if not task and not slot_arg:
        print("[중단] rebase 대상을 지정하라 — `<slot>`(단일) 또는 `--task <task>`(일괄).",
              file=sys.stderr)
        return 1
    batch = bool(task) and not slot_arg   # `--task` 단독일 때만 일괄(위치인자 동반 = 단일 지정).
    if batch:
        try:
            slots = [lease.slot for lease in pool_store.slots_for_task_strict(pm_paths.home_at(REPO), task)]
        except Exception as exc:  # noqa: BLE001 — mutation 대상 해소 실패는 0개 성공이 아님.
            print(
                f"[중단] task {task!r} 보유 슬롯 장부 조회 실패 — {exc}. "
                "rebase 대상을 0개로 간주하지 않는다.",
                file=sys.stderr,
            )
            return 1
        if not slots:
            print(f"# task {task!r} 이(가) 보유한 leased 슬롯이 없다 — rebase 대상 0.")
            return 0
    else:
        try:
            slots = [worktree_paths._normalize_slot(pm_paths.home_at(REPO), slot_arg)]
        except worktree_records.SlotResolutionError as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1

    # 소유 명의 = `--task` 주어지면 그 task(단일·일괄 공통), 아니면 세션(`rebase` 내부 해소).
    results = branch_checkout.rebase(pm_paths.home_at(REPO), slots, onto=args.onto, owner_task=task)
    n_ok = n_skip = n_conflict = 0
    for r in results:
        if r.outcome == worktree_records.REBASE_REBASED:
            n_ok += 1
            print(f"✓ 슬롯 {r.slot} rebase 성공 — 부모 {r.base} 최신 위로 이동 "
                  f"(head {(r.new_head or '?')[:12]} · 부모 기록 fork·장부 head/recorded_at 갱신).")
        elif r.outcome == worktree_records.REBASE_CONFLICT:
            n_conflict += 1
            print(f"✗ 슬롯 {r.slot} rebase 충돌 — **그 상태 그대로** 두었다(엔진 임의 abort 안 함). "
                  f"해소는 사용자: 슬롯에서 `git rebase --continue`(충돌 해결 후) 또는 `git rebase "
                  f"--abort`(취소). 부모 기록·장부 미갱신 → 다음 부트스트랩 0단계가 '진행 중' 감지. "
                  f"git: {r.reason}", file=sys.stderr)
        else:
            n_skip += 1
            print(f"— 슬롯 {r.slot} rebase 스킵 ({branch_checkout._rebase_skip_reason(pm_paths.home_at(REPO), r.reason)}).",
                  file=sys.stderr)
    print(f"\n# rebase 요약: 성공 {n_ok} · 스킵 {n_skip} · 충돌 {n_conflict} "
          "(일괄=슬롯 독립·한 충돌이 나머지를 안 막음).")
    if batch:
        return 1 if n_conflict else 0
    return 0 if n_ok == 1 else 1   # 단일 — 성공해야 rc 0(스킵/충돌=요청 미수행·rc 1).


def _cmd_refresh(args) -> int:
    """`refresh <slot> [--onto <branch>]` CLI 핸들러 — readonly 슬롯 갱신.

    fetch → detached HEAD 를 기준(onto 또는 기록된 base.branch) 최신 tip 으로 이동한다. `--onto` 는 준
    ref 를 그대로 해소하고(자동 대체 없음·원격 기준은 `origin/<branch>` 로 명시), 생략 시에만 기록된
    base.branch 를 origin 우선으로 해소한다. 성공 메시지는 백본이 돌려준 **실제 이동 ref + HEAD sha**
    를 그대로 찍는다. dirty(누군가 씀·신호)·미readonly·base 미해소·ref 해소 실패는 rc 1 로 명시
    실패(`RefreshRefused`·조용히 reset 안 함)."""
    try:
        slot = worktree_paths._normalize_slot(pm_paths.home_at(REPO), args.slot)
    except worktree_records.SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    try:
        ref, head = branch_checkout.refresh(pm_paths.home_at(REPO), slot, onto=args.onto)
    except worktree_records.RefreshRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print(f"✓ 슬롯 {slot} refresh: detached HEAD → {ref} @ {(head or '?')[:12]} "
          "로 이동(fetch→detach).")
    return 0


def _cmd_record(args) -> int:
    """`record <slot>` CLI 핸들러 — 도착 스냅(lease.git)을 live 로 재기록 (base 보존).

    0단계 record-vs-live diverged FAIL-LOUD 를 사용자가 정당(의도한 브랜치 전환·릴리즈 등)이라
    판단했을 때의 명시 재동기 진입 — `record_git_snapshot(slot)`(base 미전달=기존 base 보존·
    arrival 동형) 프리미티브를 CLI 로 노출한다(감지=기계·해소=사용자·자동 실행 없음). 슬롯 형식
    오류·장부 미등록·스냅 불가는 rc 1 로 명시 실패(silent 무변경 방지)."""
    try:
        slot = worktree_paths._normalize_slot(pm_paths.home_at(REPO), args.slot)
    except worktree_records.SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    before = pool_store.read_lease_strict(pm_paths.home_at(REPO), slot)
    before_git = before.git if (before is not None and isinstance(before.git, dict)) else None
    lease = slot_snapshot.record_git_snapshot(pm_paths.home_at(REPO), slot)
    if lease is None:
        print(f"[중단] 슬롯 {slot} 이 리스 장부에 없다 — record 는 등록된 슬롯에만 스냅을 기록한다.",
              file=sys.stderr)
        return 1
    after_git = lease.git if isinstance(lease.git, dict) else None
    if after_git is None or after_git == before_git:
        # 스냅 불가(슬롯 worktree 경로 부재 등 → `_apply_git_snapshot` 이 기존 git 보존)·무변경.
        print(f"[중단] 슬롯 {slot} git 스냅 재기록 실패 — 슬롯 worktree 를 스냅할 수 없다(경로 부재/git 오류). "
              "슬롯이 실재하는지 확인하라.", file=sys.stderr)
        return 1
    print(f"✓ 슬롯 {slot} 도착 스냅 재기록: branch=`{after_git.get('branch')}` "
          f"head=`{(after_git.get('head') or '?')[:12]}` (base 보존·이후 0단계 정합이 이 스냅 기준).")
    return 0


def _cmd_switch(args) -> int:
    """`switch <slot> <branch>` CLI 핸들러 — 브랜치 전환 + 스냅 재기록 원자.

    0단계 main-참조 fault 의 remedy 진입점. 선-검사 거부(`SwitchRefused`)·readonly
    (`ReadonlySlotMutation`)·슬롯 형식 오류는 rc 1 + 사유(loud). 성공 시 무엇을 했는지(전환 형태·
    재기록 head)를 stdout 으로 surface 한다 — 이 한 커맨드로 0단계 재진입이 열린다(2차 diverged
    차단 없음)."""
    try:
        slot = worktree_paths._normalize_slot(pm_paths.home_at(REPO), args.slot)
    except worktree_records.SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    try:
        result = branch_checkout.switch(pm_paths.home_at(REPO), slot, args.branch)
    except (worktree_records.SwitchRefused, pool_store.ReadonlySlotMutation) as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    # 원문이 revspec 이라 다른 이름으로 해소됐으면 성공 보고에도 실값을 싣는다(조용한 오전환 방지).
    asked = f" (입력 `{result.requested}` → 해소)" if result.expanded else ""
    print(f"✓ 슬롯 {slot} → 기존 브랜치 `{result.branch}`{asked} 전환 + 도착 스냅 재기록 "
          f"(head=`{(result.head or '?')[:12]}`·원자) — 0단계 재진입이 "
          f"'기록↔live diverged' 로 막히지 않는다.")
    return 0


def _main(argv: "list[str] | None" = None) -> int:
    """argparse 진입점 — pm-worktree 스킬이 래핑할 `dev`/`sync`/`status`/`rebase`/`refresh`/`record`/`switch` 커맨드

    라이브러리 모듈에 얇은 CLI 를 얹는다(`if __name__ == "__main__"` 가드로 import 안전 — 다른
    도구의 `spec_from_file_location` import 를 안 깬다). 스킬이
    `python3 .project_manager/tools/worktree_pool.py dev <sub> <branch> [--repo <name> [--slot <N>]]` /
    `... sync [--repo <name> [--slot <N>]]` /
    `... status [<slot>]` 로 부른다. CLI 는 **실경로 wiring**(git_runner 미주입)이고, 함수 레벨 DI
    seam(`dev`/`sync`/`slot_git_status` 의 git_runner)은 테스트가 쓴다. 사람이 읽는
    stdout(무엇을 했는지)·skip/경고 사유는 backbone 이 stderr·실패는 rc 1 + 메시지.

    **두 인자 표면**: `dev`/`sync` 는 --repo/--slot identity(자기 세션 슬롯 대상)·
    `status`/`rebase`/`refresh`/`record`/`switch` 는 위치인자 `<slot>`(임의 슬롯 pool 관리). 위치인자
    커맨드는 identity 파싱 전에 분기 처리한다(그 args 표면 없음).

    정체성 인자는 공용 `identity_args`(`add_identity_args`·`parse_identity`)로 통일한다(
    분해형 `--repo <name>
    [--slot <N>]` 만 받는다. `parse_identity` 의 discriminated `kind` 로 해소 경로가 갈린다:
      - `kind="slot"`(`--repo`+`--slot` 모두 명시) → `<repo>_<N>` 조립 후 `_resolve_current_slot`
        (기존 명시-슬롯 정규화/검증 경로 재사용).
      - `kind="repo"`(`--repo` 단독) → actor 해소(`_resolve_actor_slot_for_repo` — 활성 슬롯 1개면
        해소·0개/≥2개 fail-loud). bare `--slot`(`--repo` 없이)은 `parse_identity` 가 `ValueError`
        로 fail-loud(DoD).
      - `kind="none"`(인자 전무) → 기존 no-flag 체인(`_resolve_current_slot(None)`·cwd→세션
        leased).
    """
    console.configure_console_utf8()
    parser = argparse.ArgumentParser(
        prog="worktree_pool.py",
        description="worktree/submodule 운영중 관리 backbone (dev/sync).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_dev = subparsers.add_parser(
        "dev", help="submodule 을 dev 브랜치로 지정(on-branch 화 → selective resync 가 skip).")
    p_dev.add_argument("submodule", help="슬롯 worktree 상대 submodule 경로(예 vendor/sub).")
    p_dev.add_argument("branch", help="지정할 dev 브랜치명(없으면 생성·있으면 전환).")
    identity_args.add_identity_args(p_dev)

    p_sync = subparsers.add_parser(
        "sync", help="현재 슬롯 submodule 을 pin 에 selective 재동기(수동 트리거).")
    identity_args.add_identity_args(p_sync)

    # status — 위치인자 <slot>(dev/sync 의 --repo/--slot identity 와 다른 표면·pool 관리).
    p_status = subparsers.add_parser(
        "status",
        help="슬롯 git 구성 조회(role·branch·head·부모·자식·stash·submodule pin/drift·dirty·단일/일괄).")
    p_status.add_argument("slot", nargs="?", default=None,
                          help="대상 슬롯(단일·생략 시 내 task 전 슬롯 일괄).")
    p_status.add_argument("--task", default=None,
                          help="그 task 보유 전 슬롯 일괄 조회(`<slot>` 과 배타).")

    # rebase <slot> [--task <task>] [--onto <b>] (단일) · rebase --task <task> [--onto <b>] (일괄)
    # — 위치인자/pool 관리. `--task` 는 선택 축이자 **소유 명의** 축이라 위치인자와 배타가 아니다.
    p_rebase = subparsers.add_parser(
        "rebase",
        help="슬롯 브랜치를 부모 브랜치 최신 위로 사용자 명시로 rebase(단일/일괄·선-검사·충돌 그대로+loud·"
             "성공 시 부모 기록 갱신).")
    p_rebase.add_argument("slot", nargs="?", default=None,
                          help="대상 슬롯(단일 지정·`--task` 와 함께 주면 그 task 명의로 이 슬롯만).")
    p_rebase.add_argument("--task", default=None,
                          help="task 명의(소유검사 축). `<slot>` 없이 주면 "
                               "그 task 보유 전 슬롯 일괄 rebase.")
    p_rebase.add_argument("--onto", default=None,
                          help="옮길 부모 브랜치(생략 시 기록된 부모 최신·기록 없으면 거부). "
                               "주면 준 값 그대로 해소하고(로컬 브랜치명=로컬 tip·자동 대체 없음) "
                               "성공 시 그 값을 새 부모로 기록한다. 생략 시 origin 을 우선하되 "
                               "로컬이 더 앞서 있으면 대체하지 않고 로컬 tip 을 쓴다.")

    # refresh <slot> [--onto <branch>] — readonly 공유 슬롯 갱신(위치인자 <slot>).
    p_refresh = subparsers.add_parser(
        "refresh",
        help="readonly 공유 슬롯을 released 최신으로 갱신(fetch→detach 이동·dirty=거부+loud).")
    p_refresh.add_argument("slot", help="대상 readonly 슬롯(`work/<repo>_<N>` 또는 접두 생략 `<repo>_<N>`).")
    p_refresh.add_argument("--onto", default=None,
                           help="갱신 기준 ref — 준 값 그대로 해소한다(로컬 브랜치명=로컬 tip·원격 기준은 "
                                "`origin/<branch>` 로 명시·자동 대체 없음). 생략 시 기록된 base.branch 의 "
                                "origin 을 우선하되 로컬이 더 앞서 있으면 대체하지 않고 로컬 tip 을 쓰며, "
                                "둘 다 없으면 거부.")

    # record <slot> — 도착 기대 스냅(lease.git)을 live 로 재기록(base 보존·위치인자 <slot>).
    # 0단계 record-vs-live diverged FAIL-LOUD 를 사용자가 정당(의도한 브랜치 전환 등)이라 판단했을
    # 때의 명시 재동기 진입 — `record_git_snapshot` 프리미티브의 CLI 노출(감지=기계·해소=사용자).
    p_record = subparsers.add_parser(
        "record",
        help="슬롯 도착 스냅(lease.git·branch/head)을 live 로 재기록(base 보존) — 0단계 diverged 정당 판단 시 명시 재동기.")
    p_record.add_argument("slot", help="대상 슬롯(`work/<repo>_<N>` 또는 접두 생략 `<repo>_<N>`).")

    # switch <slot> <branch> — 기존 브랜치 전환 + 장부 스냅 재기록을 **원자**로(위치인자 <slot>).
    # raw `git switch` 는 스냅을 안 남겨 곧바로 차단(기록↔live diverged)을 부른다. 보호목록 브랜치로의
    # 전환은 거부하고, 없는 브랜치는 만들지 않는다.
    p_switch = subparsers.add_parser(
        "switch",
        help="슬롯을 기존 브랜치로 전환 + 도착 스냅 재기록(원자·보호브랜치 거부·브랜치 생성 안 함).")
    p_switch.add_argument("slot", help="대상 슬롯(`work/<repo>_<N>` 또는 접두 생략 `<repo>_<N>`).")
    p_switch.add_argument("branch", help="전환할 기존 브랜치(없으면 거부 — 새 브랜치는 `pm-config task branch`).")

    args = parser.parse_args(argv)

    # status / rebase / refresh / record / switch — 위치인자 <slot> 경로(identity 파싱 미진입·pool 관리).
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "rebase":
        return _cmd_rebase(args)
    if args.command == "refresh":
        return _cmd_refresh(args)
    if args.command == "record":
        return _cmd_record(args)
    if args.command == "switch":
        return _cmd_switch(args)

    try:
        identity = identity_args.parse_identity(args)
    except ValueError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1

    try:
        if identity.kind == "slot":
            slot = slot_resolution._resolve_current_slot(pm_paths.home_at(REPO), worktree_paths._explicit_slot_path(pm_paths.home_at(REPO), identity.repo, identity.slot))
        elif identity.kind == "repo":
            slot = worktree_paths._resolve_actor_slot_for_repo(pm_paths.home_at(REPO), identity.repo)
        else:  # kind == "none" — 인자 전무, 기존 no-flag 체인.
            slot = slot_resolution._resolve_current_slot(pm_paths.home_at(REPO), None)
    except worktree_records.SlotResolutionError as exc:
        print(f"[중단] 대상 슬롯 해소 실패 — {exc}", file=sys.stderr)
        return 1

    if args.command == "dev":
        try:
            rc, out = branch_checkout.dev(pm_paths.home_at(REPO), slot, args.submodule, args.branch)
        except pool_store.ReadonlySlotMutation as exc:
            print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — mutation 거부.
            return 1
        except worktree_records.SubmoduleNotInSlot as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
        if rc != 0:
            print(
                f"[중단] 슬롯 {slot} · submodule {args.submodule!r} 을 dev 브랜치 "
                f"{args.branch!r} 로 지정 실패 (rc={rc}): {str(out).strip()[:200]}",
                file=sys.stderr,
            )
            return 1
        print(
            f"✓ 슬롯 {slot} · submodule {args.submodule} → dev 브랜치 {args.branch!r} "
            "(on-branch·이후 selective resync 가 이 submodule 을 skip)."
        )
        return 0

    # args.command == "sync"
    print(
        f"# 슬롯 {slot} submodule selective 재동기 "
        "(detached=pin 재동기·on-branch=skip·dirty=skip+경고)"
    )
    try:
        branch_checkout.sync(pm_paths.home_at(REPO), slot)
    except pool_store.ReadonlySlotMutation as exc:
        print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — mutation 거부.
        return 1
    print(f"✓ 슬롯 {slot} 재동기 완료 (skip/경고 사유는 위 stderr 참조).")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 복구 안내로 번역한다."""
    try:
        console.configure_console_utf8()
        return _main(argv)
    except Exception as exc:  # noqa: BLE001 — marked skew만 사용자 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return _report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
