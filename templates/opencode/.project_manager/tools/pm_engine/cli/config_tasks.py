"""task 생명주기·todo·prefix CLI."""
from __future__ import annotations

import argparse
import sys
from pm_engine.infra import fs, lease_store
from pm_engine.app import branch_checkout
from pm_engine.infra import pool_store
from pm_engine.app import task_lifecycle
from pm_engine.domain import task_rules
from pm_engine.infra import task_store
from pm_engine.infra import worktree_paths
from pm_engine.domain import worktree_records
from pm_engine.app import areas_registry
from pm_engine.app import ticket_access
from pm_engine.domain import ticket_rules
from pm_engine.cli import config_slots


def cmd_task_new(
    home,
    args: argparse.Namespace) -> int:
    """`task new <task>` — task 정체성만 생성(세션 묶기 없음·닫힘·슬롯 0).

    `task_lifecycle.create_task` 로 장부 `tasks[]` 행(`prefix=None`·닫힘)과 서술 pm_state 를
    만든다 — 관리자/사용자가 아이들 상태에서 task 목록을 조정하는 진입이다. 세션에 묶는 것은
    부트스트랩 몫이고, 닫힘은 종료 기록과 같은 값이라 이어지는 `/pm-bootstrap --task <task>` 이
    **resume** 으로 이어받는다(신규 생성 아님).

    `--prefix` 는 두지 않는다 — 새 prefix 신설은 `task prefix` 의 `--user-ack` 승인 게이트
    단일 지점이고, 여기에 같은 인자를 두면 그 게이트를 우회한다. 생성 후
    `pm-config task prefix <task> <p>` 로 지정한다(출력이 그 다음 단계를 안내한다).

    거부(각각 rc1·장부 무변경): 부적합 이름(`InvalidTaskName` — traversal·`<repo>_<N>` 예약) ·
    동명 종료 archive(`TaskArchived` — `task reopen` 안내) · **이미 있는 이름**(재개는 bootstrap
    몫이라 여기서 열림 표시를 만지지 않는다).

    worktree_pool의 실제 함수 seam으로 시험을 격리한다. 예약패턴 판별 근거는 config_slots._registered_repos가
    해소한다(읽기 실패면 None → traversal 검증만·`cmd_alloc` 과 같은 한 자리).
    """

    name = args.name

    # 예약패턴(`<repo>_<N>`) 판별 근거 — areas fail-soft 해소(실패면 None·traversal 검증은 유지).
    registered = config_slots._registered_repos(home)

    try:
        task = task_lifecycle.create_task(home, name, registered_repos=registered)
    except worktree_records.InvalidTaskName as exc:
        print(
            f"[중단] 부적합 task 명 {name!r} — {exc.reason}. task 이름은 안전한 단일 이름이어야 "
            "하고 슬롯 예약패턴(`<repo>_<N>`)은 쓸 수 없다.",
            file=sys.stderr,
        )
        return 1
    except worktree_records.TaskArchived as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        print(
            f"  → `pm-config task reopen {name}` (복수 후보면 `--archive <basename>` 추가) 로 "
            "같은 정체성을 복원하라.",
            file=sys.stderr,
        )
        return 1

    if task is None:
        print(
            f"[중단] task {name!r} 이(가) 이미 있다 — `task new` 는 정체성을 새로 만들 뿐 "
            f"재개하지 않는다. 재개는 `{lease_store._runtime_skill_entry('pm-bootstrap')} --task {name}` 다.",
            file=sys.stderr,
        )
        return 1

    print(f"✓ task 생성: {name!r} (닫힘 · 슬롯 0) — 정체성만 만들었다(세션 묶기 없음).")
    print(
        f"  다음 진입: `{lease_store._runtime_skill_entry('pm-bootstrap')} --task {name}` "
        "(이 정체성을 재개로 이어받는다)."
    )
    print(
        f"  슬롯 대여: `pm-config alloc <repo> --task {name}` · "
        f"prefix 지정: `pm-config task prefix {name} <p>`."
    )
    return 0


def cmd_task_branch(
    home,
    args: argparse.Namespace) -> int:
    """`task branch <task> <슬롯> <브랜치> --from <부모>` — 슬롯에 새 브랜치를 만들고 부모를 기록한다.

    슬롯 안에서 새 브랜치를 만드는 유일한 명령이다(`switch` 는 기존 브랜치 전환만 한다). 브랜치는
    부모 브랜치의 최신 커밋에서 만들고, 부모 이름과 갈라진 커밋을 git 저장소 설정에 적는다
    (`branch_checkout.create_task_branch`). `<task>` 은 그 슬롯을 쥔 task 다.
    슬롯은 늘 적는다(`release <slot>` 과 같은 위치 인자·암묵 선택 없음). 엔진 거부는 사유 문구 그대로 rc 1 이다.
    """

    try:
        slot = worktree_paths._normalize_slot(home, args.slot)
    except worktree_records.SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    try:
        result = branch_checkout.create_task_branch(home, slot, args.branch, parent=args.parent, owner_task=args.name)
    except (worktree_records.TaskBranchRefused, pool_store.ReadonlySlotMutation) as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print(
        f"✓ 슬롯 {slot} 에 새 브랜치 `{result.branch}` 를 만들었다 — 부모 `{args.parent}` "
        f"@ {worktree_records._short_sha(result.head)} 에서 갈라짐(부모 기록 완료)."
    )
    return 0


def cmd_task_close(
    home,
    args: argparse.Namespace) -> int:
    """`task close <task>` — 종료 기록 없이 열린 채 남은 task 세션 기록을 닫는다.

    **기록만 닫는다** — 복원(claim 티켓·라운드 단계 등)은 이 경로의 관심사가 아니다(그건
    부트스트랩 `--force` 인계 몫). 세션이 핸드오프 없이 끝나면 장부 task 행이 열린 채 남아
    다음 부트스트랩이 거부하는데, 그것을 닫는 것은 **사용자 행위**다(엔진 자동 회수·만료 시각
    없음). 작업 폴더 쪽 짝은 기존 `pm-config release <slot> --force` 다 — 새로 만들지 않았다.

    write 는 기존 프리미티브 `task_lifecycle.close_task_session` 하나만 부른다(장부 write 재구현 0).
    값은 열림/닫힘 둘뿐이라 세 번째 상태값도, 만료/유예 인자도 없다.
    """

    name = args.name
    try:
        record = pool_store.find_task(home, name)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    if record is None:
        print(f"[중단] 등록 task 가 아니다 — {name!r}.", file=sys.stderr)
        return 1
    if not record.open:
        print(f"  task {name!r} 은 이미 닫혀 있다 — 바꿀 기록 없음(무해).")
        return 0
    task_lifecycle.close_task_session(home, name)
    print(f"✓ task {name!r} 세션 기록을 닫았다 (열림 → 닫힘) — 복원 없음(기록만).")
    print(
        f"  이어서 진입하려면: `{lease_store._runtime_skill_entry('pm-bootstrap')} --task {name}`."
    )
    return 0


def cmd_task_end(
    home,
    args: argparse.Namespace,
) -> int:
    """`task end <task>` — task 종료: claimed 소진 게이트 → dirty 게이트 → clean 시 일괄 반납 + 아카이브 이동.

    순서(각 단계 통과해야 다음):
      1. **claimed 티켓 소진 게이트** — board 스캔(`scan_task_tickets`·자체 스캔·`list --task`
         이 task 명의(`<user>/<task>`)로 claimed 인 티켓이 남아있으면 목록 +
         **거부**. 해소 = `board complete`(완료) 또는 `board unclaim`(claimed→open) — **사용자 판단**
         이라 task end 가 자동 실행하지 않는다(목록+거부까지). task 지정 prefix 의 open 티켓은
         **정보 표시만**(차단 안 함·prefix≠경계).
      2. **dirty 게이트** — 보유 작업공간(session==task)에 미커밋 변경이 있으면 목록 + 거부(사용자
         정리 후 재시도). task_lifecycle.end_task 가 판정(아무 부작용 0).
      3. **전부 clean** → 보유 슬롯 일괄 idle 반납(worktree **삭제 안 함**·반납=idle) → 장부 task
         레코드 제거 → 서술 폴더 `.local/tasks/<task>/` 를 `_ended/<task>-<날짜>/` 로 **이동**
         (삭제 아님·이름 재사용 시 옛 pm_state resume 오염 방지).

    티켓은 ticket_access가 명시 Home에서 스캔한다. 실제 스캔 실패는 기존 정책대로
    claimed 게이트를 생략하며 dirty 판정과 반납은 계속한다.
    """

    name = args.name

    # 명 검증 — end_task 가 장부 write·`shutil.move` 하는 write-capable 진입점이라
    # 엔진이 자체 검증(fail-loud)하지만, CLI 도 board 스캔 이전에 선-fail 로 clean 하게 rc1 한다
    # traversal/절대경로/빈 이름은 ticket_access 스캔·task 이동 이전에 거부한다.
    try:
        task_rules._validate_task_name(name)
    except worktree_records.InvalidTaskName as exc:
        print(
            f"[중단] 부적합 task 명 {name!r} — {exc.reason}. task 이름은 안전한 단일 이름이어야 한다.",
            file=sys.stderr,
        )
        return 1

    # 1) claimed 소진 게이트 — board 스캔. task 레코드 prefix 로 prefix-open 정보도 함께.
    task_rec = pool_store.find_task(home, name)
    prefix = task_rec.prefix if task_rec is not None else None
    scan = None
    try:
        scan = ticket_access.scan_task_tickets(home, lease_store.user_name(home), name, prefix)
    except Exception:  # noqa: BLE001 — 실제 스캔 실패는 기존 claimed 게이트 skip 정책 유지.
        scan = None
    if scan and scan.get("claimed"):
        print(
            f"[중단] task {name!r} 명의로 claimed 인 티켓이 남아있다 — 종료 거부(소진 게이트):",
            file=sys.stderr,
        )
        for row in scan["claimed"]:
            print(f"  - {row['id']} [{row['status']}] {row['title']}", file=sys.stderr)
        print(
            "  해소: 각 티켓을 `board complete`(완료) 또는 `board unclaim`(claimed→open) 처리 후 "
            "재시도하라. task end 는 자동 unclaim/complete 하지 않는다(완료/보류는 사용자 판단).",
            file=sys.stderr,
        )
        return 1

    # 2) dirty 게이트 + 3) clean 시 일괄 반납 + 서술 폴더 아카이브 이동 — 엔진 end_task.
    try:
        result = task_lifecycle.end_task(home, name)
    except worktree_records.TaskEndRefused as exc:
        print(
            f"[중단] task {name!r} 종료 거부 — {exc.reason}. "
            "task end 는 종료 기록(핸드오프 또는 `task close`) 뒤에만 허용된다.",
            file=sys.stderr,
        )
        return 1
    if result.refused:
        print(
            f"[중단] task {name!r} 보유 작업공간에 dirty(미커밋 변경)가 있다 — 종료 거부:",
            file=sys.stderr,
        )
        for slot in result.dirty:
            print(f"  - {slot}", file=sys.stderr)
        print(
            "  각 슬롯에서 커밋(또는 사용자가 요청하면 stash) 후 재시도하라.",
            file=sys.stderr,
        )
        return 1

    # 성공 — 반납 + 이동 결과 surface.
    if result.released:
        print(
            f"✓ task {name!r} 보유 슬롯 {len(result.released)}개 idle 반납(worktree 유지·풀 재사용): "
            f"{', '.join(result.released)}"
        )
    else:
        print(f"✓ task {name!r} 보유 슬롯 없음(반납 대상 0).")
    if result.moved_to is not None:
        print(f"✓ 서술 폴더 아카이브 이동(삭제 아님·PM 홈): {result.moved_from} → {result.moved_to}")
    else:
        print(f"  서술 폴더 없음(.local/tasks/{name}/ 부재) — 장부 task 레코드만 제거.")

    # task 지정 prefix 의 open 티켓 = 정보 표시만(차단 안 함·prefix≠경계).
    if scan and scan.get("prefix_open"):
        print(f"ℹ task 지정 prefix {prefix!r} 의 open 티켓(정보·경계 아님·차단 안 함):")
        for row in scan["prefix_open"]:
            print(f"  - {row['id']} {row['title']}")

    print(
        f"✓ task {name!r} 종료 완료 — 이름 재사용 안전(아카이브 이동으로 옛 pm_state 오염 없음·PM 홈)."
    )
    # 집합 변경(일괄 반납) 직후 재열거 — 종료 후 보유 슬롯 0(전부 idle 반납)을 확인.
    config_slots._render_task_slots(home, name)
    return 0


def cmd_task_reopen(
    home,
    args: argparse.Namespace) -> int:
    """`task reopen <task> [--archive <basename>]` — 종료 task identity 복원."""

    try:
        result = task_lifecycle.reopen_task(home, args.name, archive=args.archive)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {args.name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    except worktree_records.TaskReopenRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        if exc.archives:
            print(
                "  archive 후보: " + ", ".join(path.name for path in exc.archives),
                file=sys.stderr,
            )
        return 1

    print(f"✓ task {args.name!r} 동일 정체성 복원: {result.moved_from} → {result.moved_to}")
    print(
        "  장부 재등록: prefix=None(reset) · 닫힘(종료 기록 상태) · "
        f"started={result.task.started}"
    )
    print(
        f"  다음 진입: `{lease_store._runtime_skill_entry('pm-bootstrap')} --task {args.name}`"
    )
    return 0


# `none` = prefix 해제 리터럴(무prefix·T-NNNN 발행) — board `_PREFIX_RESERVED` 와 동형(case-insensitive
# fold). setter 는 이 리터럴을 board `_validate_prefix`(예약어 거부) 이전에 가로채 해제로 번역한다.
_TASK_PREFIX_NONE_LITERAL = "none"


def _todo_missing_task(name: str) -> int:
    """todo 대상 task 가 장부에 없을 때의 단일 안내(세 명령 공용·문구 사본 0)."""
    print(
        f"[중단] task {name!r} 이(가) 장부에 없다 — todo 는 기존 task 레코드에 달린다. "
        f"먼저 `pm-config task new {name}`(정체성만) 또는 "
        f"`{lease_store._runtime_skill_entry('pm-bootstrap')} --task {name}`(생성+진입) 로 task 를 만들어라.",
        file=sys.stderr,
    )
    return 1


def _print_open_todos( name: str, todos) -> None:
    """열린 todo 목록 출력 — 세 명령 공용(문구·형식 사본 0)."""
    print(f"task {name} 열린 todo {len(todos)}건:")
    for todo in todos:
        print(f"  {task_store.format_todo_line(todo)}")


def cmd_task_todo_add(
    home,
    args: argparse.Namespace) -> int:
    """`task todo add <task> <내용>` — 열린 todo 를 하나 더한다.

    쓰기 판정이 "열린 todo 가 없으면 경고" 를 이 목록으로 내므로, 이 명령이 작업을 시작하는
    진입이다. 등록 시각(UTC ISO)과 ID 는 엔진이 박고 사용자가 주지 않는다 — 묵은 정도의 근거를
    손으로 적게 하면 그 값이 곧 틀린다.
    """

    try:
        updated = task_lifecycle.add_task_todo(home, args.name, args.text)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {args.name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    if updated is None:
        return _todo_missing_task(args.name)
    _print_open_todos( args.name, updated)
    return 0


def cmd_task_todo_list(
    home,
    args: argparse.Namespace) -> int:
    """`task todo list <task>` — 열린 todo 를 ID·등록일·묵은 일수와 함께 본다."""

    try:
        task = pool_store.find_task(home, args.name)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {args.name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    if task is None:
        return _todo_missing_task(args.name)
    todos = task_store.read_task_todos(home, task.name)
    if not todos:
        print(f"task {task.name} 열린 todo 없음 — `pm-config task todo add {task.name} <내용>`")
        return 0
    _print_open_todos( task.name, todos)
    return 0


def cmd_task_todo_done(
    home,
    args: argparse.Namespace) -> int:
    """`task todo done <task> <ID>` — 열린 todo 하나를 완료 처리한다(목록에서 제거).

    완료 표시를 남기지 않는다 — 무엇을 했는지의 단일 진실은 log handoff entry 다. ID 는 재사용
    하지 않으므로 목록이 줄어도 남은 줄의 번호가 밀리지 않는다. 없는 ID 면 rc1 로 멈춘다
    (무시하면 지운 줄 알고 넘어간다).
    """

    try:
        result = task_lifecycle.close_task_todo(home, args.name, args.todo_id)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {args.name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    except LookupError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    if result is None:
        return _todo_missing_task(args.name)
    updated, removed = result
    print(f"완료: {str(removed.get('text', '')).strip()}")
    if not updated:
        print(f"task {args.name} 열린 todo 없음 — 이 상태에서는 파일 수정에 경고가 붙는다.")
        return 0
    _print_open_todos( args.name, updated)
    return 0


def cmd_task_prefix(
    home,
    args: argparse.Namespace,
) -> int:
    """`task prefix <task> <p|none>` — task 의 ticket prefix 지정/변경/해제 (중간 변경 자유).

    task 레코드(장부 top-level `tasks`)의 board prefix 를 opt-in 으로 지정/변경(`<p>`)하거나
    해제(`none`)한다 — prefix 는 task 와 **완전 독립·분류 라벨이지 경계 아님**(claim 강제 없음). 지정
    시 `board.py new --task <task>` 이 3단 해소(명시 `--prefix` > task 설정 > 기본 없음)로 그 prefix 를
    자동으로 단다. **중간 변경 자유**(task 종속으로 못 바꾸는 설계 금지) — 진행 중 언제든 바꾼다.

    검증 순서(각 통과해야 다음·부작용 이전 fail-loud rc1):
      1. **task 명 검증** — 공유 엔진 validator(`_validate_task_name`·예약패턴엔 등록 repo 전달)로
         traversal/절대경로/빈 이름/`<repo>_<N>` 예약을 거부. alloc/task end 와 동일 깔때기.
      2. **prefix 값 해소** — `none`(대소문자 무관)은 **해제 리터럴** → None(무prefix).
         그 외는 board **소비측 grammar 단일 진실**(`_validate_prefix` `[a-z0-9_]` 형식·예약어)로
         선검증 — `board.py new` 가 task prefix 를 재검증 없이 신뢰하므로 여기가 유일 입력 게이트다.
      3. **저장** — `task_lifecycle.set_task_prefix`(장부 flock/스키마 단일 소유·직접 JSON write 금지)로
         atomic 갱신. task 부재면 rc1 안내(생성은 `task new`·bootstrap 소관).

    실행 패키지의 prefix 검증·승인·락 안 fresh 재검증을 통과한 경우에만 저장한다.
    단, task 예약명 검증에 넘기는 registered_repos 조회만 실패 시 None으로 완화하고
    traversal 검증은 엔진 validator로 계속 강제한다.
    """

    name = args.name
    value = args.value

    # 1) task 명 검증(must-fix) — 공유 엔진 validator. registered_repos 는 예약패턴(`<repo>_<N>`)
    # 거부용(areas 읽기 실패면 None, traversal 검증은 유지). InvalidTaskName → rc1(부작용 이전).
    registered = config_slots._registered_repos(home)
    try:
        task_rules._validate_task_name(name, registered)
    except worktree_records.InvalidTaskName as exc:
        print(
            f"[중단] 부적합 task 명 {name!r} — {exc.reason}. task 이름은 안전한 단일 이름이어야 한다.",
            file=sys.stderr,
        )
        return 1

    # 2) prefix 값 해소 — `none`(fold)은 해제, 그 외는 board `_validate_prefix`(소비 grammar 단일 진실).
    if value.lower() == _TASK_PREFIX_NONE_LITERAL:
        new_prefix: str | None = None
    else:
        reason = ticket_rules._validate_prefix(value)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
        new_prefix = areas_registry.require_prefix_user_ack(
            home, value,
            getattr(args, "user_ack", None),
            surface="pm-config task prefix",
        )
        if new_prefix is None:
            return 1

    # 3) 저장 — 신규/기존 prefix 지정은 board_lock 안 fresh 3소스 snapshot을 선판정과
    # 대조한 직후 worktree_pool 장부 락으로 내려간다. lock 순서는 board→lease로 고정한다
    # (worktree_pool은 board를 import/호출하지 않아 역순 경로 없음). 엔진 primitive의 직접
    # 소비는 사용자 승인 CLI 표면이 아니므로 이 재검증을 성립시킬 수 없고, 설계대로 CLI 단일
    # 깔때기에서만 강제한다. `none` 해제는 신규 생성 축이 아니어서 종전 경로 그대로다.
    if new_prefix is not None:
        with fs.board_lock(home):
            fresh = areas_registry.revalidate_prefix_user_ack(
                home, value,
                getattr(args, "user_ack", None),
                new_prefix,
                surface="pm-config task prefix",
            )
            if fresh is None:
                return 1
            updated = task_lifecycle.set_task_prefix(home, name, fresh)
            new_prefix = fresh
    else:
        updated = task_lifecycle.set_task_prefix(home, name, None)
    if updated is None:
        print(
            f"[중단] task {name!r} 이(가) 아직 없다 — prefix 설정은 기존 task 레코드를 갱신할 뿐 "
            f"정체성을 생성하지 않는다. 먼저 `pm-config task new {name}`(정체성만) 또는 "
            f"`{lease_store._runtime_skill_entry('pm-bootstrap')} --task {name}`(생성+진입) 로 task 를 만든 뒤 "
            "다시 설정하라.",
            file=sys.stderr,
        )
        return 1

    areas_registry.invalidate_known_prefixes_cache()

    if new_prefix is None:
        print(
            f"✓ task {name!r} prefix 해제 — 이후 `board.py new --task {name}` 은 무prefix(T-NNNN)로 발행 "
            "(명시 `--prefix` 는 여전히 1회 오버라이드)."
        )
    else:
        print(
            f"✓ task {name!r} prefix = {new_prefix!r} — 이후 `board.py new --task {name}` 이 이 prefix 를 "
            "자동으로 단다(명시 `--prefix` 가 이김 3단 해소). 분류 라벨이지 경계 아님(claim 강제 없음)."
        )
    return 0
