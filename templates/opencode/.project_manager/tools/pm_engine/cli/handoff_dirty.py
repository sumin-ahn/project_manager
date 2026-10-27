"""보유 슬롯 스냅샷 기반 dirty gate."""
from __future__ import annotations

from pathlib import Path
from pm_engine.infra import handoff_git
from pm_engine.domain import handoff_text
from pm_engine.app import slot_resolution
import sys


def _dirty_gate_trees(home, *, held_slots) -> list[str]:
    """게이트가 판정할 실행 앵커 트리 목록 — PM 홈 + **활성 worktree 전수**.

    판정 대상 = PM 홈 + 이 task 가 보유한 슬롯 전수. run() 진입부가 영속 변경 전에 한 번
    해소해 둔 스냅샷(`_task_slots_snapshot`)을 그대로 쓴다 — 게이트가 장부를 다시 조회해
    다른 집합을 보는 비대칭을 만들지 않는다.
    PM 홈 `.gitignore` 는 보통 `work/` 를 ignore 하므로 슬롯 트리는 PM 홈 판정에 **절대**
    안 잡힌다 — 슬롯을 따로 넣지 않으면 그 트리의 미커밋 산출은 게이트를 통과한다.
    슬롯 → 트리 변환은 회귀/출하와 같은 `_regression_cwd`(stale 슬롯은 REPO 폴백)라 중복이
    생길 수 있어 순서 보존 dedup 한다.

    반환한 각 앵커 아래 등록된 submodule working tree 전수도 `_dirty_tree_gate` 가
    `submodule status --recursive` 로 확장 판정한다. 앵커 목록 자체는 기존 list[str] 계약을
    유지해 task/slot 해소와 submodule 열거 책임을 섞지 않는다.
    """
    trees = [str(home.root)]
    trees.extend((slot_resolution._regression_cwd(home, slot) for slot in held_slots))
    ordered: list[str] = []
    for tree in trees:
        if tree not in ordered:
            ordered.append(tree)
    return ordered


def _handoff_slot_trees(home, *, held_slots) -> dict[str, str]:
    """핸드오프가 다룰 슬롯의 트리 → 슬롯 — 커밋 안 한 변경이면 기록 전에 사용자에게 묻는 대상.

    대상은 이 task 가 보유한 슬롯 전수(`_task_slots_snapshot`)다. 트리 해소는
    `_dirty_gate_trees` 와 같은 `_regression_cwd` 이고, 디렉터리가 없어 PM 홈으로 폴백한
    슬롯은 슬롯 트리가 아니므로 뺀다.
    """
    slot_trees: dict[str, str] = {}
    for slot in held_slots:
        tree = slot_resolution._regression_cwd(home, slot)
        if tree != str(home.root):
            slot_trees[tree] = slot
    return slot_trees


def _dirty_tree_gate(home, *, held_slots, run_git_fn, ack_dirty: str | None, auto_trigger: bool, force_handoff: bool, dry_run: bool) -> tuple[int, str | None]:
    """[0/7] 미커밋 잔여를 **첫 mutation 전에** 판정한다. 반환: (rc, entry 박제 사유).

    rc 1 = 차단(어떤 파일도 건드리지 않은 상태). 잔여는 자리에 따라 둘로 갈린다:
      - **핸드오프가 다룰 슬롯**(`_handoff_slot_trees`)의 커밋 안 한 변경 → 사용자에게 "커밋할까요?
        강제 핸드오프할까요?" 를 묻게 하고 멈춘다. 넘는 수단은 `--force-handoff` 하나다 — 통과 +
        entry 에 `커밋 안 한 변경 있음, 슬롯 유지: <slot>` 을 적는다. `--ack-dirty`·`--auto-trigger` 는
        슬롯 쪽 변경을 넘지 않는다.
      - **그 밖**(PM 홈 등) — `--ack-dirty "<사유>"` → 통과 + 사유를 handoff entry 에 박제.
        `--auto-trigger`(사용자 명시 핸드오프 호출부 호환 신호) → 차단 대신 loud 경고 +
        사유 자동 박제. 이 플래그 자체는 실행 승인이나 자동 핸드오프 트리거가 아니다.
      - `--dry-run` → 판정 결과 미리보기만(차단 없음·기존 dry-run 의미 유지).
    판정 불가 트리(비-git·git 이상)는 비차단 경고 1줄로 stderr surface 한다 — 판정을 못 한
    것을 dirty 로 단정해 정상 핸드오프를 막지 않는다(false-block 회피). 커밋 0(unborn HEAD)은
    판정 불가가 아니라 **전량 미커밋**이라 untracked 목록으로 정상 판정한다.
    """
    trees = _dirty_gate_trees(home, held_slots=held_slots)
    dirty: list[tuple[str, list[str]]] = []
    unresolved: list[str] = []
    unresolved_submodules: list[str] = []
    judged_tree_count = 0
    for tree in trees:
        tree_paths = handoff_git._dirty_paths_in_tree(tree, run_git_fn)
        if tree_paths is None:
            unresolved.append(tree)
            paths: list[str] = []
        else:
            judged_tree_count += 1
            paths = list(tree_paths)

        submodules = handoff_git._submodule_paths_in_tree(tree, run_git_fn)
        if submodules is None:
            # 앵커 자체도 판정 불가면 기존 경고 한 줄이 이미 전체 트리를 대표한다. 앵커는
            # 판정됐는데 열거만 실패한 경우에만 별도 경고해 silent skip 을 막는다.
            if tree_paths is not None:
                unresolved_submodules.append(tree)
        else:
            for submodule in submodules:
                sub_tree = str(Path(tree) / submodule)
                sub_paths = handoff_git._dirty_paths_in_tree(sub_tree, run_git_fn)
                if sub_paths is None:
                    unresolved.append(sub_tree)
                    continue
                judged_tree_count += 1
                prefix = submodule.rstrip("/")
                paths.extend(
                    f"{prefix}/{path.lstrip('/')}" for path in sub_paths
                )

        if paths:
            dirty.append((tree, list(dict.fromkeys(paths))))
    for tree in unresolved:
        # 게이트의 다른 판정 메시지(차단·강등 경고)와 같은 채널(stderr)로 낸다 — "게이트가
        # 못 본 트리" 는 성공 로그가 아니라 경고다.
        print(
            f"  ⚠ dirty 판정 불가 — {tree} (비-git 트리·git 부재). "
            "이 트리는 게이트가 보지 못했다.",
            file=sys.stderr,
        )
    for tree in unresolved_submodules:
        print(
            f"  ⚠ dirty 판정 불가 — {tree} (submodule status --recursive 실패). "
            "이 앵커의 서브모듈은 게이트가 보지 못했다.",
            file=sys.stderr,
        )
    dirty_count = sum(len(paths) for _tree, paths in dirty)
    if not dirty:
        if ack_dirty:
            print("  --ack-dirty 무시 — 판정 결과 clean(박제할 잔여 없음).")
        if force_handoff:
            print("  --force-handoff 무시 — 판정 결과 clean(유지할 슬롯 없음).")
        print(f"  ✓ clean — 미커밋/untracked 없음 (판정 트리 {judged_tree_count}개).")
        return 0, None

    print(f"  미커밋 잔여 {dirty_count}건 (판정 트리 {judged_tree_count}개):")
    for tree, paths in dirty:
        print(f"  ▷ {tree} — {len(paths)}건:")
        for line in handoff_text._format_dirty_paths(paths):
            print(line)

    slot_trees = _handoff_slot_trees(home, held_slots=held_slots)
    dirty_slots = tuple(slot_trees[tree] for tree, _paths in dirty if tree in slot_trees)
    other_count = sum(len(paths) for tree, paths in dirty if tree not in slot_trees)
    if force_handoff and not dirty_slots:
        print("  --force-handoff 무시 — 커밋 안 한 변경이 있는 슬롯 없음(유지할 슬롯 없음).")
    kept_slots = dirty_slots if force_handoff else ()
    notes: list[str] = []
    if kept_slots:
        notes.append(handoff_text.FORCE_HANDOFF_ENTRY_NOTE.format(slots=", ".join(kept_slots)))
    if other_count and ack_dirty:
        notes.append(handoff_text.build_dirty_ack_note(ack_dirty, other_count, auto=False))
    elif other_count and auto_trigger:
        notes.append(handoff_text.build_dirty_ack_note("", other_count, auto=True))
    note = " · ".join(notes) or None

    if dry_run:
        # 미리보기 — dry-run 은 아무것도 쓰지 않으므로 차단할 이유가 없다(기존 의미 유지).
        print("  [dry-run] 판정 결과 미리보기 — 차단하지 않는다.")
        return 0, note

    if dirty_slots and not force_handoff:
        print(
            f"\n[중단] 슬롯 {', '.join(dirty_slots)} 에 커밋 안 한 변경이 있다 — 사용자에게 물어라: "
            "커밋할까요? 강제 핸드오프할까요?\n"
            "  커밋: 그 슬롯에서 커밋한 뒤 핸드오프를 다시 실행한다.\n"
            "  강제 핸드오프: `--force-handoff` 를 붙여 다시 실행한다 — 커밋 없이 핸드오프 기록만 "
            "하고 변경은 제자리에 둔다(entry 에 적는다).\n"
            "  log/current.md·pm_state.md 어떤 것도 건드리지 않는다.",
            file=sys.stderr,
        )
        return 1, None

    if kept_slots:
        print("  ⚠ --force-handoff — 커밋 안 한 변경을 두고 나간다: "
              f"{', '.join(kept_slots)}")

    if other_count and ack_dirty:
        print(f"  ⚠ --ack-dirty override — 사유를 handoff entry 에 박제한다: {note}")
    elif other_count and auto_trigger:
        print(
            "  ⚠ 사용자 명시 핸드오프 호환 신호(--auto-trigger) — dirty를 차단하지 않고 "
            "사유를 자동 박제한다: "
            f"{note} 다음 세션이 이 잔여를 먼저 처리해야 한다.",
            file=sys.stderr,
        )
    elif other_count:
        print(
            f"\n[중단] 핸드오프 시작 시점에 미커밋 잔여 {other_count}건 — 핸드오프 불가. "
            "세션 산출물을 먼저 커밋하라(티켓 산출은 티켓별 커밋이 관례). "
            "의도적으로 두고 나간다면 `--ack-dirty \"<사유>\"` 로 사유를 남겨라. "
            "log/current.md·pm_state.md 어떤 것도 건드리지 않는다.",
            file=sys.stderr,
        )
        return 1, None
    elif ack_dirty:
        print("  --ack-dirty 무시 — 슬롯 밖 미커밋 잔여 없음(슬롯 쪽은 --force-handoff 몫).")
    return 0, note
