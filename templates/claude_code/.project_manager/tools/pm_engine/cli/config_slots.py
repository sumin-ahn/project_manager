"""작업 슬롯 생성·대여·반납·제거 CLI."""
from __future__ import annotations

import argparse
import shlex
import sys
from typing import Callable
from pm_engine.infra import areas_store, console, lease_store, pm_paths
from pm_engine.app import repo_protection
from pm_engine.infra import pool_store
from pm_engine.infra import sparse_checkout
from pm_engine.domain import task_rules
from pm_engine.infra import worktree_git
import pm_engine.app.worktree_pool
from pm_engine.domain import worktree_records
from pm_engine.cli import config_repo


def _prompt_test_cmd(input_fn: Callable[[str], str], *, default: str) -> str | None:
    """worktree add 빌드명령 프롬프트 — `빌드/테스트 명령? [Enter=repo 기본값 유지 <default>]:`.

    **빈 입력(엔터만) → None(슬롯 미바인딩)** — 슬롯 리스 test_cmd 가 board 의 해소 체인서
    areas per-repo test_cmd 보다 *우선*이라, 빈입력에 기본값을 박으면 areas 의
    그 repo test_cmd(예: `go test ./...`)를 잘못 덮는다(must-fix 1·codex). 빈입력은 None 으로
    슬롯을 비워 해소 체인이 areas/local.conf 로 폴백하게 한다(기존 동작 보존). `default` 는
    *표시 전용* — Enter 시 적용될 repo 폴백값(areas→local.conf→pytest-q)을 투명하게 보일 뿐
    슬롯엔 안 박는다. 비어있지 않은 입력만 그 값으로 슬롯에 바인딩된다.

    `EOFError`/`KeyboardInterrupt` 는 우아하게 None 으로 흡수(미지정 폴백·크래시 0). input_fn
    주입으로 hermetic 테스트(라이브 input 블록 0·주입 시퀀스로 결정적).
    """
    try:
        raw = input_fn(f"빌드/테스트 명령? [Enter=repo 기본값 유지: {default}]: ")
    except (EOFError, KeyboardInterrupt):
        return None
    cmd = raw.strip()
    return cmd if cmd else None


def _require_worktree_add_user_ack(args: argparse.Namespace) -> bool:
    """물리 슬롯 생성에 repo 값-결속 사용자 승인 토큰을 요구한다.

    CLI의 ``--user-ack <repo>``와 콘솔 ``[w]``의 repo 재입력 확인이 이 한 sink로
    수렴한다. 토큰은 자가-증언형 감사/마찰 장치이며 raw git/장부 직접 편집 같은 적대적
    우회까지 막는 보안 경계는 아니다.
    """
    repo = args.repo
    user_ack = getattr(args, "user_ack", None)
    if user_ack == repo:
        print(f"[승인 감사] worktree add: 사용자 승인 토큰이 repo {repo!r}에 값-결속됨.")
        return True
    mismatch = (
        f" 제공된 값 {user_ack!r}은 repo {repo!r}에 결속되지 않았다."
        if user_ack is not None
        else ""
    )
    print(
        f"[중단] worktree add {repo!r}: 물리 슬롯 생성에는 사용자 명시 승인이 필요하다.{mismatch}\n"
        f"  1순위: 사용자에게 repo {repo!r}의 새 슬롯 생성 승인을 요청하라.\n"
        f"  부차 수단: 승인한 사용자만 `--user-ack {repo}`를 대상값 그대로 붙여 실행하라"
        "(세션 자동 부착 금지).",
        file=sys.stderr,
    )
    return False


def cmd_worktree_add(
    home,
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    is_tty: Callable[[], bool] | None = None,
    clone_runner: worktree_records.GitRunner | None = None,
) -> int:
    """`worktree add <repo> [--test "<cmd>"] [--task <task>] [--readonly]` — 새 슬롯 생성 + submodule init.

    **`--task <task>`**: 생성 직후 그 슬롯을 task 명의로 대여한다(session=task·alloc
    동형·reclaim/재부착 보호는 tasks 장부 조인). 풀 소진 시 alloc 이 안내하는 "생성+대여 한 흐름" — min-idle 재탐색의
    오슬롯 리스크가 없다. task 명 검증 + 기바인딩은 alloc 과 같은 헬퍼로. `--readonly` 와 상호배타.

    pm_engine.app.worktree_pool.create_slot(repo, base=) 를 호출한다 — `<repo>_<N>`(브랜치 무관 재사용
    컨테이너) 슬롯을 `git worktree add` 로 만들고 `git submodule update --init` 한다.
    브랜치 무관(슬롯 브랜치는 `task branch`·`worktree_pool.py switch` 가 바꾼다).

    **base 브랜치**: areas.md 의 그 repo base(`pm-config repo add --base`/clone-time
    bare HEAD 가 기록)를 `areas_store._repo_base(home, repo)`로 읽어 `create_slot(base=)` 로 전달한다 — 슬롯
    브랜치 `<repo>_<N>` 가 그 base(develop 등)에서 파생되고 base 가 그 브랜치의 부모로 기록된다.
    areas 에 base 가 없으면 부모를 적을 수 없어 슬롯을 만들지 않는다(rc 1).

    test_cmd(슬롯 리스 바인딩) 해소:
      - `--test "<cmd>"` 명시 → 그 값을 바인딩(현행·CLI 정확작업·CI).
      - `--test` 미지정 + **tty** → 슬롯 생성 후 빌드명령 프롬프트. **빈입력(Enter) → None
        (슬롯 미바인딩)** → 해소 체인이 areas/local.conf 로 폴백(기존 동작 보존·must-fix 1).
        프롬프트 `[Enter=repo 기본값 유지: …]` 표시값은 그 repo 의 *실제 폴백*(areas test_cmd
        → local.conf → pytest -q·`_resolve_repo_test_cmd`)이라 Enter 가 무엇을 적용하는지 투명.
      - `--test` 미지정 + 비-tty(CI/파이프) → 프롬프트 생략·None(현행·repo areas/local.conf 로 해소).
    regression._test_cmd 가 활성 슬롯의 이 값을 areas 위 레이어로 읽으므로, 빈입력에 기본값을 박으면
    areas per-repo test_cmd 를 잘못 덮는다 → 빈입력은 반드시 None(슬롯 미바인딩)이어야 한다.

    **성공 출력 다음 단계**: task 없이 만든 슬롯은 idle 이다. 출력은 그 슬롯을 쓰는 순서
    (`task new` → `alloc <repo> --task <task>`)와 세션 시작(`/pm-bootstrap --task <task>`, 사용자
    몫)을 안내한다. `--task` 로 만든 슬롯은 생성 직후 그 task 명의로 대여되고 반납 명령을 안내한다.

    **bare 준비**: 모든 검사 뒤 슬롯 생성 직전에 `.repos/<repo>.git` 이 없으면 등록 저장소
    hydrate(`cmd_repo_add` 를 이름만으로)로 areas `git` 주소에서 bare 를 만든다. 원본에 커밋이
    없거나 조회에 실패하면 그 rc 로 멈추고 bare 를 만들지 않는다. bare 경로가 있으나 무효면
    `create_slot` 의 BareRepoMissing 안내가 그대로 나간다(자동 삭제 없음).

    Home으로 registry와 풀 좌표를 전달한다. input_fn/is_tty/clone_runner는 실제 입력·IO seam이다.
    `clone_runner`는 hydrate의 `config_repo.cmd_repo_add`에 전달하는 Python 키워드다.
    """
    if not _require_worktree_add_user_ack(args):
        return 1


    # test_cmd 해소: --test 명시면 그 값. 미지정 + tty 면 프롬프트(빌드명령). 비-tty 면 None.
    # 프롬프트 표시 기본값은 그 repo 의 실제 폴백(areas→local.conf→pytest-q)을 resolve 해
    # 보여준다(Enter 시 적용될 값 투명화) — 빈입력은 그래도 None 으로 슬롯을 비운다(must-fix 1).
    tty_check = is_tty if is_tty is not None else console._stdin_is_tty
    test_cmd = getattr(args, "test", None)
    if test_cmd is None and tty_check():
        display_default = repo_protection._resolve_repo_test_cmd(home, args.repo)
        test_cmd = _prompt_test_cmd(input_fn, default=display_default)

    # base 해소 — areas.md 의 그 repo base 를 읽어 create_slot(base=) 로 전달한다.
    # 슬롯 브랜치 `<repo>_<N>` 가 그 base 에서 파생되고 base 가 그 브랜치의 부모로 기록된다.
    # base 가 없으면 부모를 적을 수 없으므로 만들지 않는다.
    # base 조회는 전달받은 Home의 areas_store 공급자를 직접 사용한다.
    base = areas_store._repo_base(home, args.repo)
    if base is None:
        print(
            f"[중단] repo {args.repo!r} 등록에 base 브랜치가 없다 — 슬롯 브랜치의 부모를 적을 수 "
            "없어 슬롯을 만들지 않는다. areas.md 의 그 repo 행 `base` 칸을 채운 뒤 다시 실행하라.",
            file=sys.stderr,
        )
        return 1

    # readonly 공유 슬롯 — research 전용 read-only 기준면. detached HEAD·role="readonly"·
    # session 없음. 생성 자체는 사용자 승인 flow(디스크=코드 전체 사본).
    readonly = getattr(args, "readonly", False)

    # --task <task> (ⓓB) — 새 슬롯을 만들고 **생성 직후 그 슬롯을** task 명의로 대여한다
    # (풀 소진 시 alloc 이 안내하는 생성+대여 한 흐름·min-idle 재탐색 오슬롯 리스크 제거). readonly 와
    # 상호배타(readonly=무소유 공유 자산). task 명 검증 + 기바인딩은 alloc 과 동일 헬퍼로.
    folders_arg = getattr(args, "folders", None)
    folders = None if folders_arg is None else [item.strip() for item in folders_arg.split(",")]
    if readonly and folders is not None:
        print("[중단] `--readonly` 와 `--folders` 는 함께 쓸 수 없다.", file=sys.stderr)
        return 1
    task = getattr(args, "task", None)
    if task is not None:
        if readonly:
            print(
                "[중단] `--readonly` 와 `--task` 는 함께 쓸 수 없다 — readonly 공유 슬롯은 무소유"
                "(session 없음·배타 대여 없음)라 task 명의 대여 대상이 아니다.",
                file=sys.stderr,
            )
            return 1
        rc = _validate_prebound_task(home, task)
        if rc:
            return rc

    # bare 부재 = 등록 저장소 hydrate(areas `git` 주소로 clone). 커밋 0개 원본은 그 안에서 거부된다.
    if not pm_paths.bare_repo_path(home, args.repo).exists():
        hydrated = config_repo.cmd_repo_add(home,
            argparse.Namespace(name=args.repo, git=None, test=None, base=None, owner=None,
                               user=None, protected=None),
            clone_runner=clone_runner)
        if hydrated:
            return hydrated
    try:
        lease = pm_engine.app.worktree_pool.create_slot(home, args.repo, base=base, test_cmd=test_cmd, readonly=readonly,
                               owner_task=task, folders=folders)
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"[중단] worktree 슬롯 생성 실패: {exc}", file=sys.stderr)
        return 1
    slot_path = pm_paths.slot_path(home, lease.slot)
    test_line = f"\n  test_cmd 바인딩: {lease.test_cmd!r} (이 슬롯 회귀명령)" if lease.test_cmd else ""
    # 슬롯 번호 N(`lease.slot` = `work/<repo>_<N>`) 파싱. readonly 슬롯 refresh 안내에 쓴다.
    # 이미 보유한 lease 에서만 뽑고, 형식이 다르면 슬롯 식별자를 그대로 쓴다.
    slot_prefix = f"work/{lease.repo}_"
    slot_tail = lease.slot[len(slot_prefix):] if lease.slot.startswith(slot_prefix) else ""
    slot_num = slot_tail if slot_tail.isdigit() else lease.slot
    if readonly:
        # readonly 공유 슬롯 — 대여 대상 아님(무소유·공유 자산). 갱신은 refresh 로만.
        print(
            f"✓ readonly 공유 슬롯 생성: {lease.slot} (repo={lease.repo}·role=readonly) → {slot_path}\n"
            "  research 전용 read-only 기준면(detached HEAD·배타 대여 없음·session 없음). 코드를\n"
            "  *읽어* PM 홈 wiki(domain·architecture·status)를 쓰는 역할의 읽기 기준면이다.\n"
            f"  갱신은 `{lease_store._runtime_skill_entry('pm-worktree')} refresh "
            f"{lease.repo}_{slot_num}` 로만(fetch→detach 이동·dirty=거부). "
            "rebase/dev/sync 는 거부된다."
        )
    elif task is not None:
        # task-명의 생성(ⓓB) — 생성 직후 그 슬롯을 task 명의로 leased(별도 alloc 불요).
        print(
            f"✓ worktree 슬롯 생성 + task 대여: {lease.slot} (repo={lease.repo} · task={task!r}) "
            f"→ {slot_path}{test_line}\n"
            f"  생성 직후 이 슬롯을 task {task!r} 명의로 leased — 코드 작업은 이 슬롯 cwd 에서. "
            "보드/wiki 는 multi-PM 공유 `.project_manager`.\n"
            f"  반납: `pm-config release {lease.slot} --task {task}` (작업완료 시 idle 반납)."
        )
    else:
        print(
            f"✓ worktree 슬롯 생성: {lease.slot} (repo={lease.repo}) → {slot_path}{test_line}\n"
            "  코드 작업은 이 슬롯 cwd 에서 — 보드/wiki 는 multi-PM 공유 `.project_manager`.\n"
            f"  이 슬롯은 idle 이다. task 가 빌려 쓴다: `pm-config alloc {lease.repo} --task <task>` "
            "(task 가 없으면 먼저 `pm-config task new <task>`)."
        )
        print(
            f"  세션 시작은 사용자가 `{lease_store._runtime_skill_entry('pm-bootstrap')} --task <task>` 로 한다. "
            "세션이 이 안내를 읽고 스스로 실행하지 않는다."
        )
    try:
        actual_folders = sparse_checkout.slot_folders(home, lease.slot)
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print("  폴더: " + (", ".join(actual_folders) if actual_folders is not None else "(제한 없음)"))
    # 보호 브랜치 pre-push 훅 (재)설치 (멱등 자가치유) — 슬롯 op 마다 (재)설치해 엔진
    # update 후 기존 repo 도 다음 worktree add 에 훅을 얻는다(별도 명령 불요·회사 repo 무영향).
    # 설치 실패는 조용히 넘기지 않는다 — repo add·repo protected 와 **같은 공용 깔때기**
    # (훅 설치 False 침묵은 보호 가드 무력화를 감춘다).
    repo_protection._install_protected_hook_reporting(home,
        args.repo, action="(재)설치")
    # 사본 루트 ignore 규칙 (재)설치 (멱등 자가치유) — 훅과 같은 자리에서 슬롯 op
    # 마다 (재)설치해, 규칙 없이 만들어진 기존 풀도 다음 worktree add 에 규칙을 얻는다.
    config_repo._ensure_bare_ticket_copy_exclude(pm_paths.bare_repo_path(home, args.repo))
    # task-명의 생성이면 집합 변경 직후 재열거 — 지금 보유한 슬롯 집합을 surface.
    if task is not None:
        _render_task_slots(home, task)
    return 0


def cmd_worktree_prune_stale(
    home,
    args: argparse.Namespace) -> int:
    """`worktree prune-stale` — worktree dir 이 사라진 dangling 장부 엔트리를 안전 정리.

    status reconcile 이 surface 한 stale/incomplete 중 **worktree 가 물리적으로 부재**한 장부
    엔트리를 제거한다(worktree_pool.prune_stale_leases). **안전**: 지울 worktree 파일이 없는
    dangling 기록만 정리 — 사용자 데이터/worktree 삭제가 아니라 삭제-위임 원칙 위반이 아니다.
    orphan *worktree*(git 측·disk 존재·작업 가능)는 손대지 않는다(그건 `git worktree remove` 로
    사용자가). reconcile(status)이 조회-전용인 것과 분리된 **명시 user-invoked 정리 진입점**이다.

    worktree_pool의 실제 함수 seam으로 hermetic 테스트(실 장부 쓰기 없이 배선 검증).
    """

    pruned = pm_engine.app.worktree_pool.prune_stale_leases(home)
    if pruned:
        print(
            f"✓ dangling 장부 엔트리 {len(pruned)}개 정리 (worktree 부재·이미 사라진 기록): "
            f"{', '.join(pruned)}"
        )
    else:
        print("정리할 dangling 엔트리 없음 (worktree 부재 장부 엔트리 0).")
    return 0


def _require_worktree_remove_user_ack(args: argparse.Namespace) -> bool:
    """슬롯 제거에 슬롯 값-결속 사용자 승인 토큰을 요구한다(`_require_worktree_add_user_ack` 와 같은 모양).

    `--user-ack` 는 반복 가능하다. 입력한 slot 인자가 값 중에 그대로 있어야 엔진을 부르고, 값 전부가
    합쳐지지 않은 브랜치 삭제 승인값으로 엔진에 넘어간다(`remove_slot(acked_branches=)`). 토큰은
    자가-증언형 감사/마찰 장치이며 적대적 우회까지 막는 보안 경계는 아니다.
    """
    slot = args.slot
    user_acks = list(getattr(args, "user_ack", None) or [])
    if slot in user_acks:
        print(f"[승인 감사] worktree remove: 사용자 승인 토큰이 슬롯 {slot!r}에 값-결속됨.")
        return True
    mismatch = (
        f" 제공된 값 {user_acks!r}에 슬롯 {slot!r}이 없다."
        if user_acks
        else ""
    )
    print(
        f"[중단] worktree remove {slot!r}: 슬롯 제거에는 사용자 명시 승인이 필요하다.{mismatch}\n"
        f"  1순위: 사용자에게 슬롯 {slot!r}의 제거 승인을 요청하라.\n"
        f"  부차 수단: 승인한 사용자만 `--user-ack {slot}`를 대상값 그대로 붙여 실행하라"
        "(세션 자동 부착 금지). 합쳐지지 않은 브랜치도 지우려면 그 브랜치 이름을 "
        "`--user-ack <브랜치>` 로 함께 붙인다.",
        file=sys.stderr,
    )
    return False


# 슬롯 제거 출력에 싣는 합쳐지지 않은 커밋 줄 상한 — 넘으면 "외 N개" 로 접는다.
_UNMERGED_COMMITS_SHOWN = 10


def _print_branch_deletion(deletion) -> None:
    """`BranchDeletion` 을 출력한다 — 행동·사유 한 줄 + 근거(합쳐지지 않은 커밋 목록)."""
    print(f"  {deletion.summary()}")
    commits = deletion.unmerged_commits
    for line in commits[:_UNMERGED_COMMITS_SHOWN]:
        print(f"    {line}")
    if len(commits) > _UNMERGED_COMMITS_SHOWN:
        print(f"    … 외 {len(commits) - _UNMERGED_COMMITS_SHOWN}개")


def cmd_worktree_remove(
    home,
    args: argparse.Namespace) -> int:
    """`worktree remove <slot> --user-ack <slot> [--user-ack <브랜치> ...] [--force]` — 슬롯 통째 제거.

    pm_engine.app.worktree_pool.remove_slot(slot, force=, acked_branches=) 로 리스 확인 → 등록 조회 → 커밋 안 한
    변경 검사 → `git worktree remove` → 브랜치 삭제 판정 → 장부 엔트리 제거를 한 번에 한다.
    footgun 체인(수동 remove → dangling 장부 → `add` 가 번호 skip → 뒤늦은 prune)을 원천
    종결한다 — 장부 엔트리 제거로 `add` 가 빈 번호를 재사용한다.

    **사용자 승인값 필수** — `--user-ack` 값에 입력한 슬롯이 없으면 엔진을 부르지 않고 rc1
    (`_require_worktree_remove_user_ack`). `prune-stale`(worktree 부재 장부만 정리)과 달리 **실
    worktree 를 지운다**. orphan worktree(장부 미등록)는 여전히 `git worktree remove` 로 사용자가.

    - 활성 리스(leased·사용 중) → 거부. `--force` 가 넘는 것은 이 점유 하나뿐이다.
    - 커밋 안 한 변경·git 미등록 트리 → `--force` 와 무관하게 거부(RemoveRefused·rc1). 엔진은
      stash 하지 않고 미등록 트리를 지우지 않는다.
    - 장부에 슬롯 없으면 무해 종료(rc0·이미 정리됨).
    - 브랜치: `BranchDeletion` 을 그대로 출력한다(삭제/보존 사유·자식·합쳐지지 않은 커밋·origin).
      합쳐지지 않은 브랜치는 그 브랜치 이름이 `--user-ack` 값에 있을 때만 지운다.
    - 엔진 RuntimeError(worktree remove 실패·브랜치 조회 실패) → rc1. 그 실패가 무엇을 남겼는지는
      엔진이 등록을 다시 물어 판정하므로, 여기서 재시도 방법을 지어내지 않는다.

    worktree_pool의 실제 함수 seam으로 hermetic 테스트(실 worktree remove·장부 쓰기 없이 배선 검증).
    """
    if not _require_worktree_remove_user_ack(args):
        return 1


    try:
        result = pm_engine.app.worktree_pool.remove_slot(home, args.slot, force=args.force,
                                acked_branches=frozenset(args.user_ack))
    except worktree_records.RemoveRefused as exc:
        if exc.reason == "active-lease":
            print(
                f"[중단] 슬롯 {args.slot!r} 이 활성(사용 중·state={exc.state}) — 제거 거부. "
                "먼저 `release` 로 반납하거나 `worktree remove --force`(점유 무시).",
                file=sys.stderr,
            )
        elif exc.reason == "dirty":
            print(
                f"[중단] 슬롯 {args.slot!r} 에 커밋 안 한 변경이 있다 — 제거 거부(엔진은 stash 하지 "
                "않는다). 사용자에게 물어 커밋하거나 사용자가 요청하면 stash 한 뒤 다시 실행하라. "
                "`--force` 는 이 거부를 넘지 않는다.",
                file=sys.stderr,
            )
        else:
            print(
                f"[중단] 슬롯 {args.slot!r} 의 트리 {exc.path} 는 git 이 worktree 로 등록하지 않았다 — "
                "커밋 안 한 변경을 물을 수 없어 엔진이 지우지 않는다. 사용자가 그 경로를 직접 확인·"
                "삭제한 뒤 `pm-config worktree prune-stale` 로 장부를 정리하라.",
                file=sys.stderr,
            )
        return 1
    except RuntimeError as exc:
        print(f"[중단] worktree 슬롯 제거 실패: {exc}", file=sys.stderr)
        return 1

    if result is None:
        print(
            f"✓ 슬롯 {args.slot!r} 장부에 없음 — 이미 정리됨(무해). "
            "orphan worktree(git 측·장부 미등록)는 `git worktree remove` 로 사용자가."
        )
        return 0

    # ⚠ 활성 리스 강제 회수 경고 — `--force` 로 leased/creating(사용 중) 슬롯을 override 했을 때
    # (reviewer should-fix·티켓 결정 §활성 리스 문구 정합). 다른 세션이 쓰던 슬롯일 수 있음을 stderr 로.
    if getattr(result, "forced_state", None):
        print(
            f"⚠ 활성(state={result.forced_state}) 슬롯 {args.slot!r} 을 강제 회수 "
            "(--force override — 다른 세션이 사용 중이었을 수 있다).",
            file=sys.stderr,
        )

    print(f"✓ 슬롯 {args.slot!r} 제거 — worktree remove + 장부 엔트리 삭제(빈 번호 재사용 가능).")
    # git 이 등록만 지우고 트리를 남긴 형상(읽기 전용 잔재 등) — 엔진이 마저 지웠음을 고지한다.
    if result.tree_cleaned:
        print("  git 이 등록만 지우고 남긴 디렉터리 잔재를 엔진이 마저 정리했다.")
    deletion = result.branch_deletion
    if deletion is None:
        return 0
    _print_branch_deletion(deletion)
    # 보존 브랜치 캐비앗 — 같은 번호 base-경로 재생성이 막힌다(SlotBranchExists 선-검출).
    if deletion.action == worktree_records.BRANCH_PRESERVED:
        print(
            f"  ⚠ 브랜치 {deletion.branch} 보존됨 — 같은 번호 슬롯 재생성은 브랜치 잔존을"
            " 선-검출해 명확히 멈춘다(SlotBranchExists). 그 브랜치 작업을 이어가려면 다른 슬롯에서"
            f" `worktree_pool.py switch <슬롯> {deletion.branch}`(리셋 없음)."
        )
    return 0


def cmd_set_test_cmd(
    home,
    slot: str,
    cmd: str | None) -> int:
    """슬롯 빌드/테스트 명령 설정·변경 — 콘솔 `[b]`·"나중에 변경".

    pm_engine.app.worktree_pool.set_test_cmd(slot, cmd) 로 기존 슬롯 리스의 test_cmd 를 갱신한다(flock +
    atomic write·worktree_pool 책임). 별도 CLI 서브커맨드는 없다 — 콘솔 `[b]` 와 worktree
    add 프롬프트가 변경 경로를 흡수(결정 §setter 단순화). 장부에 슬롯이 없으면(`KeyError`)
    명시 에러 rc 1(침묵 무력화 금지). `cmd=None`/빈 문자열이면 바인딩 해제(폴백·현행).

    worktree_pool의 실제 함수 seam으로 hermetic 테스트(실 장부 쓰기 없이 배선 검증).
    """

    normalized = (cmd.strip() or None) if cmd else None
    try:
        lease = pm_engine.app.worktree_pool.set_test_cmd(home, slot, normalized)
    except KeyError:
        print(
            f"[중단] 슬롯 {slot!r} 에 대한 리스가 없다 — 새 슬롯이 필요하면 먼저 사용자에게 "
            "생성 승인을 요청하라. 승인한 사용자만 대상 repo와 같은 `--user-ack <repo>`로 "
            "`worktree add`를 직접 실행한다(세션 자동 실행 금지).",
            file=sys.stderr,
        )
        return 1
    if lease.test_cmd:
        print(f"✓ 슬롯 {slot} 빌드/테스트 명령 설정: {lease.test_cmd!r} (이 슬롯 회귀명령).")
    else:
        print(f"✓ 슬롯 {slot} 빌드/테스트 명령 해제 — repo areas/local.conf 로 폴백(현행).")
    return 0


def cmd_release(
    home,
    args: argparse.Namespace) -> int:
    """`release <slot> [--task <task>] [--force]` — 작업완료 반납 / 수동 강제 백스톱.

    - 기본: pm_engine.app.worktree_pool.release(slot) — 커밋 안 한 변경이 있으면 ReleaseRefused(엔진은 stash 하지 않는다).
    - `--task <task>`: 소유검사 — 이 슬롯이 그 task 명의(lease.session)가 아니면 `NotTaskOwner`
      로 거부(다른 task/세션 슬롯을 실수로 idle 화 방지). dirty 판정보다 먼저. `--task` 미지정은
      현행 slot-only 반납(백스톱).
    - `--force`: pm_engine.app.worktree_pool.force_release(slot) — 점유(leased)·소유 **무시 강제** idle 화.
      커밋 안 한 변경은 force 여도 ReleaseRefused. 장부에 슬롯 없으면 무해 종료. force 는 순수
      백스톱이라 `--task` 소유검사도 건너뛴다(override 의도).

    부트스트랩·핸드오프는 슬롯을 반납하지 않는다. 실제 worktree_pool 함수 seam으로 시험을 격리한다.
    """


    if args.force:
        try:
            lease = pm_engine.app.worktree_pool.force_release(home, args.slot)
        except pool_store.ReadonlySlotNotLeasable as exc:
            print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — 강제 반납도 대상 아님.
            return 1
        except worktree_records.ReleaseRefused:
            print(
                f"[중단] 슬롯 {args.slot!r} 에 커밋 안 한 변경이 있다 — 강제 반납도 거부(엔진은 stash "
                "하지 않는다). 사용자에게 물어 커밋하거나 사용자가 요청하면 stash 한 뒤 다시 실행하라.",
                file=sys.stderr,
            )
            return 1
        if lease is None:
            print(f"✓ 슬롯 {args.slot!r} 장부에 없음 — 이미 정리됨(무해).")
        else:
            print(f"✓ 슬롯 {args.slot!r} 강제 반납(idle 화).")
        return 0

    owner_task = getattr(args, "task", None)
    try:
        pm_engine.app.worktree_pool.release(home, args.slot, owner_task=owner_task)
    except KeyError:
        print(f"[중단] 슬롯 {args.slot!r} 에 대한 리스가 없다.", file=sys.stderr)
        return 1
    except pool_store.ReadonlySlotNotLeasable as exc:
        print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — 반납 대상 아님.
        return 1
    except worktree_records.NotTaskOwner as exc:
        held = f"session {exc.holder!r} 보유 중" if exc.holder else "미점유(idle)"
        print(
            f"[중단] 슬롯 {args.slot!r} 은 task {owner_task!r} 소유가 아니다 — 반납 거부({held}). "
            "내 task 가 대여한 슬롯만 `--task` 로 반납할 수 있다(다른 task 슬롯 보호).",
            file=sys.stderr,
        )
        return 1
    except worktree_records.ReleaseRefused:
        print(
            f"[중단] 슬롯 {args.slot!r} 에 커밋 안 한 변경이 있다 — 반납 거부(작업 유실 방지·엔진은 "
            "stash 하지 않는다). 사용자에게 물어 커밋하거나 사용자가 요청하면 stash 한 뒤 다시 "
            "실행하라. `release --force` 는 점유·소유검사만 넘는다.",
            file=sys.stderr,
        )
        return 1
    task_line = f" (task {owner_task!r} 소유검사 통과)" if owner_task else ""
    print(f"✓ 슬롯 {args.slot!r} 작업완료 반납(idle 화){task_line} — 풀에 재사용 컨테이너로 반환.")
    # task-명의 반납이면 집합 변경 직후 재열거 — 남은 보유 슬롯 집합을 surface.
    # slot-only 백스톱(owner_task=None)은 대상 task 가 없어 재열거 없음.
    if owner_task:
        _render_task_slots(home, owner_task)
    return 0


def _registered_repos(home) -> set[str]:
    """등록 repo 이름 — task 명 예약패턴(`<repo>_<N>`) 판별 근거. areas.md 에서 해소한다.

    areas.md 판독 실패는 전파한다. 해소 규칙이 이 한 자리라 `task new`(생성)와
    `alloc`/`worktree add --task`(기바인딩 요구)가 같은 근거로 판정한다.
    """
    return areas_store.registered_repos(home)


def _validate_prebound_task(home, task: str) -> int:
    """task 명 검증(예약패턴·traversal) + 기바인딩 요구 — `alloc`/`worktree add --task` 공용.

    task 이름이 lease session 으로 **기록**되는 write-capable 경로의 선검증이자,
    정체성 *생성*(`task new`·bootstrap)이 슬롯 부착보다 먼저라는 순서를 강제한다. 통과면 0,
    실패면 1(진단은 이 함수가 stderr 로 출력). 예약패턴 판별 근거는 `_registered_repos` 하나가
    해소한다.
    """
    registered = _registered_repos(home)
    try:
        task_rules._validate_task_name(task, registered)
    except worktree_records.InvalidTaskName as exc:
        print(
            f"[중단] 부적합 task 명 {task!r} — {exc.reason}. `--task` 는 안전한 단일 이름이어야 "
            "하고 슬롯 예약패턴(`<repo>_<N>`)은 쓸 수 없다.",
            file=sys.stderr,
        )
        return 1
    # 기바인딩 요구 — 생성은 `task new`·bootstrap 이 하고 여기선 붙이기만 한다. 미바인딩이면 안내 rc1.
    if pool_store.find_task(home, task) is None:
        print(
            f"[중단] task {task!r} 이(가) 아직 없다 — alloc/add 는 기존 task 에 슬롯을 붙일 뿐 "
            f"정체성을 생성하지 않는다. 먼저 `pm-config task new {task}`(정체성만) 또는 "
            f"`{lease_store._runtime_skill_entry('pm-bootstrap')} --task {task}`(생성+진입) 로 task 를 만든 뒤 "
            "다시 시도하라.",
            file=sys.stderr,
        )
        return 1
    return 0


def _slot_live_branch(home, slot: str) -> str:
    """슬롯 worktree 의 live 브랜치를 조회한다.

    `current_branch(slot)`(git=진실·장부 저장 폐지)를 부른다 — detached 는 표시 문자열로
    내고, 조회 실패는 전파한다.
    """
    return worktree_git.current_branch(home, slot) or "(detached)"


def _render_task_slots(home, task_name: str) -> None:
    """task 보유 슬롯 **집합**을 재열거해 surface 한다.

    집합을 바꾸는 연산(alloc·release·task end·`worktree add --task`) **직후** 결과 집합(슬롯·
    repo·branch·state 행렬)을 출력한다 — 사용자/PM 이 매 연산 후 현재 보유 집합을 한눈에 본다
    (묶음 처리 1급화). 부트스트랩 진입 열거와 같은 렌더 문법을 공유하려는 공용 헬퍼다.
    `slots_for_task`(session==task 명의 leased·장부 진실)에서 파생 — 신설 저장소 0·부작용 0.
    """
    slots = list(pool_store.slots_for_task(home, task_name) or [])
    label = f"작업공간 (task {task_name!r} 보유 {len(slots)})"
    if not slots:
        print(f"{label}: (없음)")
        return
    print(f"{label}:")
    for l in sorted(slots, key=lambda x: x.slot):
        branch = _slot_live_branch(home, l.slot)
        print(f"  - {l.slot} · repo={l.repo} · branch={branch} · {l.state}")


def cmd_alloc(
    home,
    args: argparse.Namespace) -> int:
    """`alloc <repo> --task <task>` — 기바인딩 task 명의로 idle 최소 번호 슬롯 대여.

    pm_engine.app.worktree_pool.alloc(repo, owner_task=<task>) 로 idle **최소 번호** 슬롯을 leased 로 전이한다 —
    session 축에 task 정체성을 실어 이후 `release --task`/`task end` 소유검사가 매칭한다.
    **항상 신규 대여**: 같은 task 가 이미 이 repo 슬롯을 보유해도 그걸 재반환하지 않고
    다른 idle 슬롯을 대여해 **같은 repo 복수 보유**(병렬 dev 격리
    등)를 자연 지원한다. 대여한 lease 의 reclaim/재부착 보호는 **tasks 장부 조인**(session ∈ tasks
    장부)이 담당한다 — 자동 회수는 없다. idle 슬롯이 없으면 NeedsCreate → **대여 실패 + 사용자 생성 요청**
    (자동 생성 금지·`worktree add --task` 안내로 생성+대여 한 흐름). 새 슬롯은 디스크(코드 전체
    사본×슬롯)라 물리층이고 사용자 승인(`worktree add`)이 게이트다 — alloc/release=PM 자율(논리
    층)·create/remove=사용자 승인(물리층)의 2층 분리를 CLI 로 표면화한다.

    **명 검증 + 기바인딩 요구**: `--task` 이름이 lease session 으로 **기록**되므로 write
    -capable 진입점이다 — 공유 헬퍼 `_validate_prebound_task`(`worktree add --task` 와 동일 계약·엔진
    validator `_validate_task_name` + `find_task` 를 묶음)로 선검증해 traversal/절대경로/빈 이름/
    `<repo>_<N>` 예약(기계판별 구멍)을 fail-loud rc1 하고, **task 는 기바인딩이어야 한다**를
    강제한다 — 미바인딩이면 "먼저 `pm-config task new <task>` 또는 `/pm-bootstrap --task <task>` 으로
    task 를 만들라" 안내(정체성 *생성*은 그 둘 소관·alloc은 이미 존재하는 task 에 슬롯을 붙일 뿐).
    예약패턴 판별 근거(등록 repo)는 `_registered_repos` 가 areas.md 에서 해소한다(판독 실패는 전파).

    worktree_pool의 실제 함수 seam으로 hermetic 테스트(실 git alloc 없이 배선·분기 검증).
    """


    # 명 검증 + 기바인딩 요구 — 공유 헬퍼(`worktree add --task` 와 동일 규율).
    # InvalidTaskName/미바인딩 → rc1(부작용·alloc 이전·진단은 헬퍼가 출력).
    rc = _validate_prebound_task(home, args.task)
    if rc:
        return rc

    try:
        # owner_task = task-명의 alloc — 항상 신규 idle 슬롯 대여(같은 repo
        # 복수 보유). session=owner_task 로 기록되고, 그 슬롯의 reclaim/재부착 보호는 tasks 장부 조인이
        # 담당한다(bound 아님).
        folders_arg = getattr(args, "folders", None)
        folders = None if folders_arg is None else [item.strip() for item in folders_arg.split(",")]
        lease = pm_engine.app.worktree_pool.alloc(home, args.repo, owner_task=args.task, folders=folders)
    except worktree_records.NeedsCreate:
        folders_option = f" --folders {shlex.quote(folders_arg)}" if folders_arg is not None else ""
        print(
            f"[중단] repo {args.repo!r} 에 대여 가능한 idle 슬롯이 없다 — 풀 소진. 새 슬롯은 "
            "디스크(코드 전체 사본×슬롯·clone 시간)라 자동 생성하지 않는다(물리층=사용자 승인). "
            f"사용자에게 repo {args.repo!r} 슬롯 생성 승인을 요청하라. 승인한 사용자만 "
            f"`pm-config worktree add {args.repo} --task {args.task}{folders_option} --user-ack {args.repo}` 를 "
            "직접 실행해 새 슬롯을 만들며 곧바로 이 task 명의로 대여한다"
            "(생성+대여 한 흐름·ⓓB·오슬롯 없음·세션 자동 실행 금지).",
            file=sys.stderr,
        )
        return 1
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except worktree_records.BareRepoMissing as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except worktree_records.CheckoutFailed as exc:
        print(f"[중단] 슬롯 체크아웃 실패: {exc}", file=sys.stderr)
        return 1

    slot_path = pm_paths.slot_path(home, lease.slot)
    print(
        f"✓ 슬롯 대여: {lease.slot} (repo={lease.repo} · task={args.task}) → {slot_path}\n"
        f"  이 슬롯은 task {args.task!r} 명의로 leased — 코드 작업은 이 슬롯 cwd 에서. "
        f"보드/wiki 는 multi-PM 공유 `.project_manager`.\n"
        f"  반납: `pm-config release {lease.slot} --task {args.task}` (작업완료 시 idle 반납)."
    )
    try:
        actual_folders = sparse_checkout.slot_folders(home, lease.slot)
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print("  폴더: " + (", ".join(actual_folders) if actual_folders is not None else "(제한 없음)"))
    if folders is not None:
        print("  폴더를 줄이면 빠진 폴더 안의 추적 밖 파일(빌드 결과물 등)은 git 이 지운다")
    # 집합 변경 직후 재열거 — task 가 지금 보유한 슬롯 집합을 surface.
    _render_task_slots(home, args.task)
    return 0
