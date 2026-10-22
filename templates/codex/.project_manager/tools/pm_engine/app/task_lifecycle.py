"""task 생성·결합·todo 수정·종료 전이."""
from __future__ import annotations

import shutil
from pathlib import Path

from pm_engine.domain import task_rules, worktree_records
from pm_engine.infra import clock, pm_paths, pool_store, task_store, worktree_git


def _create_task_locked(home, tasks: list[worktree_records.Task], name: str, *, session: str) -> worktree_records.Task:
    """task 정체성을 신규 생성해 장부에 기록한다 — **_lease_lock 보유 전제**.

    task 생성 진입점 둘(`bind_task` 의 신규 가지 = 세션 진입 겸 생성 · `create_task` = 정체성만
    생성)이 **같은 생성 로직**을 쓰도록 락 안쪽으로 뺀 헬퍼다. 락을 잡는 층은 공개 함수에만
    둔다 — 공개 `create_task` 를 `bind_task` 안에서 부르면 같은 락 파일을 두 fd 로 잡아
    (재진입) 블로킹 획득이 그 자리에 멈춘다.

    두 진입점이 갈리는 값은 `session` 하나다 — 세션 진입은 그 세션 ID, 정체성만 생성은 빈 값(닫힘)
    (`close_task_session` 와 같은 값이라 다음 bind 가 그대로 재개한다). 동명 종료
    archive 가 있으면 부작용 이전에 `TaskArchived`(복원은 `task reopen`)로 거부하고, pm_state 는
    `ensure_task_pm_state` 가 만든다(템플릿 복제 금지). 장부 write 는 이 함수에서 1회다.
    """
    archives = task_store.task_archive_candidates(home, name)
    if archives:
        raise worktree_records.TaskArchived(name, archives)
    task = worktree_records.Task(name=name, prefix=None, session=session, started=clock.now_utc())
    task_store.ensure_task_pm_state(home, name)
    tasks.append(task)
    pool_store._write_tasks(home, tasks)
    return task


def create_task(home, 
    name: str, *, registered_repos: "list[str] | set[str] | None" = None
) -> "Task | None":
    """task 정체성만 만든다 — 세션에 묶지 않는다(`session` 빈 값). 이미 있으면 `None`.

    `pm-config task new <task>` 의 write 백엔드다. 장부 `tasks[]` 행(`prefix=None`·`session` 빈 값)과
    `.local/tasks/<task>/pm_state.md`(`ensure_task_pm_state` — `bind_task` 신규 생성분과 같은
    산출)까지만 만들고 끝낸다: 슬롯 대여(`alloc` 소관)도 세션 묶기(bootstrap 소관)도 하지
    않는다. 이후 `pm-bootstrap --task <task>` 이 그 행을 **resumed** 로 이어받는다 — 닫힘
    (`session` 빈 값)은 `bind_task` 가 그대로 재개하는 값이라 새 상태값이 없다.

    이미 있는 이름은 `None`(호출부가 rc1 안내)이다 — 재개는 bootstrap 몫이라 여기서 기존
    레코드의 `session` 을 만지지 않는다(`set_task_prefix` 의 부재-`None` 과 같은 계약·새 예외 0).
    동명 종료 archive 는 `TaskArchived`(`task reopen` 안내), 부적합 이름은 `InvalidTaskName`.

    **명 검증(must-fix)**: `_validate_task_name` 을 mkdir·장부 write **이전**에 돌린다
    (`bind_task` 동형 — CLI 를 우회한 직접 소비도 닫는다).
    """
    task_rules._validate_task_name(name, registered_repos)   # mkdir·장부 write 이전 fail-loud(부작용 0).
    with pool_store._lease_lock(home):
        tasks = pool_store._read_tasks_strict(home)
        if any(task.name == name for task in tasks):
            return None
        # 닫힘 = 아무 세션도 열지 않음 — 핸드오프 종료 기록과 같은 값.
        return _create_task_locked(home, tasks, name, session="")


def bind_task(home, name: str, *, session: str,
              registered_repos: "list[str] | set[str] | None" = None) -> tuple[worktree_records.Task, str]:
    """task 를 신규/resume 바인딩한다 — 명 검증(must-fix) + 열림 기록 거부 + 서술 디렉토리 신설.

    반환 `(task, action)` — action ∈ {"created", "resumed"}:
      - **created**  — 장부에 없던 task 를 신규 생성(prefix=None·기본 없음·session=호출 세션·슬롯 0개 시작).
      - **resumed**  — 닫힌 기록(핸드오프가 남긴 종료 기록·`task close`·`task new` 직후)을 재개.

    **명 검증(must-fix)**: `_validate_task_name` 을 mkdir·장부 write **이전**에 돌려 traversal/절대경로/
    빈 이름/예약패턴(`registered_repos` 주면)을 fail-loud(`InvalidTaskName`) — 엔진층 배치
    0357 의 직접 소비도 우회 못 한다.

    **세션**: `session` = 이 부트스트랩을 부른 하네스 세션 ID(가드 훅이 남긴 기록에서 온다 —
    `take_task_session_claim`). 같은 세션의 재진입은 거부하지 않고 재개한다(압축·재부트스트랩).

    **열림 기록 거부**: 장부 행이 **다른 세션**으로 열려 있으면 `TaskActiveElsewhere` 로 거부한다(부트스트랩이
    dump 이전에 거부·판정에 프로세스 조회 0회). 해소는 사용자 행위다 — 그 창을 쓰거나,
    `pm-config task close <task>` 으로 기록을 닫거나, 부트스트랩 `--force` 로 이어받는다
    (강제 경로도 기록을 먼저 닫고 이 함수를 그대로 탄다 — 강제 전용 분기 0).

    prefix 는 여기서 안 만진다
    — 생성 시 None·재개 시 기존 값 유지(변경 = `task prefix`). 신규/재개 모두
    `ensure_task_pm_state`로 `.local/tasks/<name>/pm_state.md`를 즉시 보장한다. task state는
    slot 유무와 무관한 연속성 앵커이며 첫 handoff까지 생성을 미루지 않는다.
    """
    task_rules._validate_task_name(name, registered_repos)   # mkdir·장부 write 이전 fail-loud(부작용 0).
    if not isinstance(session, str) or not session:
        raise ValueError(f"task {name!r} 를 열 하네스 세션 ID 가 비어 있다")
    with pool_store._lease_lock(home):
        tasks = pool_store._read_tasks_strict(home)
        existing = next((t for t in tasks if t.name == name), None)
        if existing is None:
            # 생성은 `create_task`(`task new`)와 공유하는 락 안쪽 헬퍼가 한다 — 여기선 열림으로
            # 연다(재진입 없음·공개 create_task 를 부르지 않는다).
            return _create_task_locked(home, tasks, name, session=session), "created"
        # 열림 기록 거부 — 다른 세션이 종료 기록 없이 열어 두었으면 두 번째 창을 열지 않는다.
        if existing.session and existing.session != session:
            raise worktree_records.TaskActiveElsewhere(name, existing.started)
        # 구 엔진에서 만들어져 task 디렉토리만 있고 state가 없는 레코드도 재개 시 즉시 backfill.
        # state 보장 실패 시 장부를 갱신하지 않아 반쪽 resume을 만들지 않는다.
        task_store.ensure_task_pm_state(home, name)
        existing.session = session  # 열림 기록 — 종료 기록(핸드오프·task close·end)이 닫는다.
        pool_store._write_tasks(home, tasks)
        return existing, "resumed"


def set_task_prefix(home, name: str, prefix: "str | None") -> "Task | None":
    """task 레코드의 board prefix 를 지정/변경/해제한다 — 갱신된 `Task`·task 부재 시 `None`.

    `pm-config task prefix <task> <p|none>` 의 write 백엔드다 — 장부 top-level `tasks` 레코드
    의 `prefix` 필드를 `prefix`(문자열=지정/변경·None=해제) 로 덮는다. **중간 변경 자유**
    (task 종속 없음) — bind_task 는 prefix 를 안 만지고(생성=None·재개=유지) 변경은 여기 단일
    지점이다. `board.py new --task <task>` 이 `lease_store.task_prefix` 로 이 값을 read 해
    해소(명시 `--prefix` > task 설정 > 기본 없음)에 쓴다.

    **장부 IO 는 이 모듈이 단일 소유**(직접 JSON read/write 금지·flock/스키마) — `_lease_lock`
    아래 `_read_tasks`/`_write_tasks`(형제 `leases`·미지 top-level 키 무손실 round-trip)로 atomic 하게
    갱신한다. task 부재면 `None`(호출부가 rc1 안내) — 생성은 `create_task`(`task new`)와
    `bind_task`(bootstrap) 소관이라 여기서 task 를 만들지 않는다.

    **명 검증(must-fix)**: `_validate_task_name` 을 장부 write **이전**에 돌려 traversal/절대경로/빈
    이름을 fail-loud(`InvalidTaskName`) — write-capable 엔진 진입점이라 CLI 우회(직접 소비)도 닫는다.
    prefix *형식* sanity(`[a-z0-9_]`·`none` 예약)는 CLI 입력측(pm_config)이 소비 grammar
    단일 진실(`board._validate_prefix`)로 선검증한다 — 여기선 저장만(board.py new 가 신뢰)."""
    task_rules._validate_task_name(name)   # 장부 write 이전 fail-loud(부작용 0·must-fix).
    with pool_store._lease_lock(home):
        tasks = pool_store._read_tasks_strict(home)
        target = next((t for t in tasks if t.name == name), None)
        if target is None:
            return None
        target.prefix = prefix
        pool_store._write_tasks(home, tasks)
        return target


def add_task_todo(home, name: str, text: str, *, now=clock.now_utc) -> "list[dict] | None":
    """task 에 열린 할 일을 하나 더한다 — 갱신된 목록·task 부재 시 `None`.

    `pm-config task todo add <task> <내용>` 의 write 백엔드다. 같은 내용을 두 번 넣는 것은 막지
    않는다 — 같은 문장이 서로 다른 일을 가리킬 수 있고, 중복 여부는 기계가 판정할 수 없다.

    ID 는 `next_id` 에서 받아 **재사용하지 않는다**. 지운 번호를 다시 쓰면 사람이 화면에서 읽은
    번호와 실제가 어긋난다. 저장은 task 별 파일(`todo.json`)이고 장부는 건드리지 않는다.

    **명 검증(must-fix)**: `_validate_task_name` 을 파일 write **이전**에 돌린다(형제 write
    진입점 `set_task_prefix`·`close_task_session` 동형). 빈 내용은 `ValueError` — 가리키는 일이
    없는 todo 를 만들면 쓰기 판정의 경고가 그 빈 줄 하나로 꺼져 버린다.
    """
    task_rules._validate_task_name(name)   # 파일 write 이전 fail-loud(부작용 0·must-fix).
    body = text.strip()
    if not body:
        raise ValueError(f"task {name!r} 에 넣을 todo 내용이 비어 있다")
    if pool_store.find_task(home, name) is None:
        return None             # 할 일은 장부에 있는 task 에만 단다.
    with task_store._task_todo_lock(home, name):
        doc = task_store._read_todo_doc(home, name)
        doc["todos"].append({"id": doc["next_id"], "text": body, "created": now()})
        doc["next_id"] += 1
        task_store._write_todo_doc(home, name, doc)
        return list(doc["todos"])


def close_task_todo(home, name: str, todo_id: int) -> "tuple[list[dict], dict] | None":
    """열린 할 일 하나를 ID 로 지운다 — `(남은 목록, 지운 항목)`·task 부재 시 `None`.

    `pm-config task todo done <task> <ID>` 의 write 백엔드다. `todo_id` 는 `task todo list` 가
    보여주는 `#N` 이다 — 목록이 줄어도 그 번호는 그 일을 계속 가리킨다. 없는 ID 면 `LookupError`
    로 멈춘다(호출부가 rc1 안내): 조용히 무시하면 지운 줄 알고 넘어간다.

    완료 표시를 남기지 않는다 — 무엇을 했는지의 단일 진실은 log handoff entry 이고, 여기에
    완료 목록을 쌓으면 같은 사실이 두 곳에 산다.
    """
    task_rules._validate_task_name(name)   # 파일 write 이전 fail-loud(부작용 0·must-fix).
    if pool_store.find_task(home, name) is None:
        return None
    with task_store._task_todo_lock(home, name):
        doc = task_store._read_todo_doc(home, name)
        index = next(
            (i for i, todo in enumerate(doc["todos"]) if todo["id"] == todo_id), None)
        if index is None:
            open_ids = ", ".join(f"#{todo['id']}" for todo in doc["todos"]) or "없음"
            raise LookupError(
                f"todo #{todo_id} 는 task {name!r} 에 없다 (열린 ID: {open_ids})")
        removed = doc["todos"].pop(index)
        task_store._write_todo_doc(home, name, doc)
        return list(doc["todos"]), removed


def close_task_session(home, name: str) -> "Task | None":
    """task 종료 기록 — 장부 task 레코드 `session` 을 비운다.

    pm_handoff task 모드가 log 쓰기 전 load-bearing 종료 경계로 호출하고, `pm-config task close
    <task>` 이 같은 프리미티브로 **종료 기록 없이 남은 세션**을 사용자가 닫는다(복원 없음).
    이 기록이 없으면 다음 부트스트랩이 그 task 를 "열려 있음"으로 보고 거부한다 —
    닫아 두면 그대로 재개(`resumed`)한다. task end(`end_task`)와는 별개 — end 는 레코드 제거·
    아카이브, 이건 세션 인계라 레코드는 유지하고 열림 표시만 내린다.

    반환 = 갱신된 `Task`. task 부재면 primitive는 `None`을 반환하지만, load-bearing
    pm_handoff 호출부는 이를 log 무접촉 fail-loud로 올린다. 신규 상태/필드 없이 기존
    두 값(열림/닫힘)뿐인 기존 필드를 쓰는 최소 변경 — 세 번째 상태값을 만들지 않는다.

    **장부 IO 는 이 모듈이 단일 소유**(직접 JSON 금지·flock/스키마) — `_lease_lock` 아래
    `_read_tasks`/`_write_tasks`(형제 `leases`·미지 top-level 키 무손실 round-trip)로 atomic 갱신한다.

    **명 검증**: `_validate_task_name` 을 장부 write **이전**에 돌려 traversal/
    절대경로/빈 이름을 fail-loud(`InvalidTaskName`) — 형제 write 진입점(`set_task_prefix`·`end_task`)과
    동형 방어. 예약패턴(`<repo>_<N>`)은 생성 관심사라 여기선 registered_repos 미요구(end_task 동형·
    종료엔 path-safety 만 필요·session 은 이미 장부에 있음)."""
    task_rules._validate_task_name(name)   # 장부 write 이전 fail-loud(부작용 0·must-fix).
    with pool_store._lease_lock(home):
        tasks = pool_store._read_tasks_strict(home)
        target = next((t for t in tasks if t.name == name), None)
        if target is None:
            return None
        target.session = ""        # 종료 기록 — 다음 부트스트랩이 그대로 재개(resumed).
        pool_store._write_tasks(home, tasks)
        return target


def reopen_task(home, name: str, archive: "str | None" = None) -> worktree_records.ReopenTaskResult:
    """종료 archive를 같은 task identity로 복원한다.

    모든 선검사를 마친 뒤 archive 디렉터리를 active 경로로 rename하고, 장부 raw dict의
    ``tasks``만 갱신해 단 한 번 atomic replace한다. 장부 write 실패 시 원 archive 경로로
    보상 rename하며, 보상도 실패하면 양쪽 경로를 포함해 fail-loud한다.
    """
    task_rules._validate_task_name(name)
    if archive is not None:
        if (not isinstance(archive, str) or not archive or archive in {".", ".."}
                or Path(archive).is_absolute() or "/" in archive or "\\" in archive
                or Path(archive).name != archive):
            raise worktree_records.TaskReopenRefused(name, f"부적합 archive basename {archive!r}")

    with pool_store._lease_lock(home):
        data = pool_store._read_ledger_raw_strict(home)
        rows = data.get("tasks", [])
        if not isinstance(rows, list):
            raise ValueError("worktree lease 장부의 'tasks' 값이 list가 아님")
        tasks: list[worktree_records.Task] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not row.get("name"):
                raise ValueError(
                    f"worktree lease 장부의 tasks[{index}] 값이 유효한 task object가 아님"
                )
            tasks.append(worktree_records.Task.from_dict(row))

        if any(task.name == name for task in tasks):
            raise worktree_records.TaskReopenRefused(name, "활성 task 장부 레코드가 이미 존재함")
        active = pm_paths.task_dir(home, name)
        if active.exists() or active.is_symlink():
            raise worktree_records.TaskReopenRefused(name, f"활성 task 폴더가 이미 존재함: {active}")

        candidates = task_store.task_archive_candidates(home, name)
        if not candidates:
            raise worktree_records.TaskReopenRefused(name, "복원할 종료 archive가 없음")
        if archive is None:
            if len(candidates) != 1:
                raise worktree_records.TaskReopenRefused(
                    name,
                    "archive 후보가 여러 개라 --archive exact basename이 필요함: "
                    + ", ".join(path.name for path in candidates),
                    archives=candidates,
                )
            selected = candidates[0]
        else:
            selected = next((path for path in candidates if path.name == archive), None)
            if selected is None:
                raise worktree_records.TaskReopenRefused(
                    name,
                    f"archive 후보에 없는 basename {archive!r}",
                    archives=candidates,
                )

        task = worktree_records.Task(name=name, prefix=None, session="", started=clock.now_utc())
        try:
            selected.rename(active)
        except OSError as exc:
            raise worktree_records.TaskReopenRefused(
                name, f"archive rename 실패: {exc}", archives=candidates,
                moved_from=selected, moved_to=active,
            ) from exc

        data["tasks"] = [task_row.to_dict() for task_row in tasks] + [task.to_dict()]
        try:
            pool_store._write_ledger_raw(home, data)
        except Exception as write_exc:
            try:
                active.rename(selected)
            except Exception as rollback_exc:
                raise worktree_records.TaskReopenRefused(
                    name,
                    f"장부 replace 실패({write_exc}); 보상 rename도 실패({rollback_exc})",
                    archives=candidates, moved_from=selected, moved_to=active,
                ) from rollback_exc
            raise worktree_records.TaskReopenRefused(
                name, f"장부 replace 실패; archive 원위치 보상 완료: {write_exc}",
                archives=candidates, moved_from=selected, moved_to=active,
            ) from write_exc
        return worktree_records.ReopenTaskResult(name, selected, active, task)


def end_task(home, name: str, *, git_runner: worktree_records.GitRunner | None = None) -> worktree_records.EndTaskResult:
    """task 를 종료한다 — 보유 슬롯 dirty 검사 → (clean 이면) 일괄 idle 반납 + 장부 제거 + 서술 폴더 아카이브 이동.

    **claimed 티켓 소진 게이트는 여기서 보지 않는다** — board 스캔은 board 소유(pm_config 가
    board 로드로 선-검사하고, 통과 시에만 이 함수를 부른다·import 격리). 이 함수는
    worktree/장부/서술 폴더만 다룬다:

      1. `slots_for_task(name)` 보유 슬롯 중 **dirty** 가 하나라도 있으면 → 거부(`EndTaskResult.
         refused`·released/moved 없음·아무 부작용 0). 사용자 정리 후 재시도.
      2. 전부 clean → 보유 슬롯을 일괄 **idle 반납**(worktree 폴더는 유지·삭제 안 함·release 와
         동일 전이: session 비우고 git 스냅 정리) → 장부의 task 레코드 제거 → 서술 폴더
         `.local/tasks/<name>/` 를 `_ended/<name>-<날짜>/` 로 **이동**(삭제 아님).

    **명 검증**: `_validate_task_name` 을 장부 write·`shutil.move`
    **이전**에 돌려 traversal/절대경로/빈 이름을 fail-loud(`InvalidTaskName`) — `bind_task` 와 동형의
    엔진 진입점 방어다. 무검증이면 `end_task("../evil")` 이 `_archive_dest` 파생 후 `.local/tasks` 밖으로
    `shutil.move` 한다. 예약패턴(`<repo>_<N>`)은 생성
    시점(bind_task/cmd_alloc)에서 걸리는 **생성 관심사**라 여기선 registered_repos 를 요구하지 않는다
    (종료엔 path-safety 만 필요·session 은 이미 장부에 있음). dirty 검사·반납·장부/task write와
    archive 목적지 결정·폴더 이동을 **한 `_lease_lock` 안에서** 직렬화한다. 장부 제거와 archive 공개
    사이에 동명 bind/reopen이 끼어 identity와 pm_state가 분리되는 락 공백을 두지 않는다.
    git_runner 주입으로 hermetic.
    """
    task_rules._validate_task_name(name)   # 장부 write·shutil.move 이전 fail-loud
    with pool_store._lease_lock(home):
        leases = pool_store._read_ledger_strict(home)
        # task 컬렉션도 어떤 lease/fs 변경보다 먼저 strict로 스냅샷한다. 뒤늦게 손상을 발견하면
        # 슬롯은 이미 idle로 썼는데 task 레코드는 남는 반쪽 end가 되므로 선검증이 필수다.
        tasks = pool_store._read_tasks_strict(home)
        target = next((task for task in tasks if task.name == name), None)
        if target is None:
            raise worktree_records.TaskEndRefused(name, "등록 task가 아님")
        if target.open:
            raise worktree_records.TaskEndRefused(name, "종료 기록이 없음(세션이 열린 채로 남아 있음)")
        owned = [l for l in leases if l.state == "leased" and l.session == name]

        # 1) dirty 검사 — 하나라도 dirty 면 거부(부작용 0·장부 미변경).
        dirty: list[str] = []
        for lease in owned:
            path = pm_paths.slot_path(home, lease.slot)
            if path.exists() and worktree_git._is_dirty(path, git_runner=git_runner):
                dirty.append(lease.slot)
        if dirty:
            return worktree_records.EndTaskResult(name, released=[], dirty=sorted(dirty),
                                 moved_from=None, moved_to=None)

        # 2a) 전부 clean → 보유 슬롯 일괄 idle 반납(worktree 유지·release 동형 전이).
        released: list[str] = []
        for lease in owned:
            lease.state = "idle"
            lease.session = ""
            lease.git = None    # release 와 동일 — idle 슬롯은 활성 git 기대 없음(다음 alloc 재스냅).
            lease.bound = False  # task 종료 = bind 점유 종료 — 사람 bind 마커 해제(release 동형 lifecycle).
            released.append(lease.slot)
        if released:
            pool_store._write_ledger(home, leases)

        # 2b) 장부의 task 레코드 제거(같은 락·형제 leases 는 위에서 이미 반영됨).
        pool_store._write_tasks(home, [task for task in tasks if task.name != name])

        # 2c) 서술 폴더 이동도 같은 lifecycle 임계구역에서 완료한다. archive가 공개된 뒤에만
        # bind_task가 락을 얻어 TaskArchived로 수렴하므로 동명 신규 생성 TOCTOU가 없다.
        src = pm_paths.task_dir(home, name)
        dest: "Path | None" = None
        if src.exists():
            dest = task_store._archive_dest(home, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
    return worktree_records.EndTaskResult(name, released=sorted(released), dirty=[],
                         moved_from=src if dest is not None else None, moved_to=dest)
