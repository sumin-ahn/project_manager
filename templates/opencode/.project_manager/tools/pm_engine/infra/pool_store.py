"""Home별 슬롯·task 장부 판독·원자 저장·잠금."""
from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator

from pm_engine.domain import identity_args, worktree_records
from pm_engine.infra import fs, lease_store, pm_paths, worktree_paths


# ── 리스 장부 파일락 (경로 규약만 소유·플랫폼 분기는 공용 `fs` seam) ──────────
# 장부 read-modify-write 를 직렬화한다. 프로세스가 죽으면 OS 가 락을 자동 해제(stale-lock
# 없음). 플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 seam 이 소유하고
# 이 도구는 *어느 파일에 거는지*(`.local/worktree-leases.lock`)만 정한다.
# board 를 top-level import 하지 않는다는 원칙은 그대로다 — seam 은 형제를 로드하지 않는
# leaf 라 그 원칙과 무관하게 공유할 수 있다.


@contextlib.contextmanager
def _lease_lock(home) -> Iterator[None]:
    """리스 장부 write 를 직렬화하는 OS 파일락 컨텍스트매니저.

    `.project_manager/.local/worktree-leases.lock` 에 배타 OS 락. 프로세스가 죽으면 OS 가
    자동 해제(stale-lock 없음). **재진입 금지** — 같은 프로세스가 이 컨텍스트를 중첩하면
    안 된다(flock 재진입 동작은 OS 별로 다름). 장부의 모든 read-modify-write 가 이 한
    구간 안에서 일어난다.
    """
    with fs.exclusive_file_lock(pm_paths.leases_lock(home)):
        yield


# ── 장부 읽기/쓰기 (락 보유 전제) ────────────────────────────────────────────


def _read_ledger_raw(home) -> dict:
    """장부 파일의 **최상위 dict 원본**을 읽는다. 부재/손상/비-dict → 빈 dict(fail-soft).
    **_lease_lock 보유 전제**.

    `leases`(슬롯 리스)·`tasks`·향후 additive 최상위 컬렉션이 한 파일에 공존하므로,
    특정 컬렉션만 갱신할 때(`_write_ledger`/`_write_tasks`) 나머지 최상위 키를 무손실 보존하는
    read-modify-write 의 read 측이다. Lease-내부 미지 키 보존(`extra`)의 **최상위판** —
    구 엔진(신규 최상위 키를 모르는 import 사본 lag)이 아무 op 해도 형제 컬렉션이 안 날아가게
    한다([[robustness-value-connections-before-ship]] silent degrade 방지)."""
    if not pm_paths.leases_file(home).exists():
        return {}
    try:
        data = json.loads(fs.read_text_shared(pm_paths.leases_file(home), encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_ledger_raw_strict(home) -> dict:
    """장부 최상위 dict를 읽되 **부재만** 빈 dict로 본다. **_lease_lock 보유 전제**.

    bootstrap/handoff처럼 ``0슬롯``과 ``장부를 읽을 수 없음``을 구분해야 하는 안전 게이트용
    파서다. JSON 손상·읽기 오류는 원 예외를 전파하고, JSON 최상위가 dict가 아니면
    ``ValueError``를 낸다. 기존 ``_read_ledger_raw``과 그 fail-soft 소비자는 그대로 둔다.
    """
    if not pm_paths.leases_file(home).exists():
        return {}
    data = json.loads(fs.read_text_shared(pm_paths.leases_file(home), encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("worktree lease 장부 최상위 값이 object가 아님")
    return data


def _write_ledger_raw(home, data: dict) -> None:
    """최상위 dict 를 atomic replace 로 쓴다 (tmp→원자 교체·부분쓰기 방지). **_lease_lock 보유 전제**."""
    pm_paths.leases_file(home).parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = pm_paths.leases_file(home).with_suffix(".json.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8", newline="\n")
    fs.atomic_replace(tmp, pm_paths.leases_file(home))


def _read_ledger(home) -> list[worktree_records.Lease]:
    """리스 장부의 `leases` 컬렉션을 읽는다. 부재/손상 → 빈 리스트(fail-soft). **_lease_lock 보유 전제**."""
    rows = _read_ledger_raw(home).get("leases", [])
    if not isinstance(rows, list):
        return []
    return [worktree_records.Lease.from_dict(d) for d in rows]


def _read_ledger_strict(home) -> list[worktree_records.Lease]:
    """리스 컬렉션 strict 조회 — 손상/읽기 오류/잘못된 컬렉션·행을 예외로 전파한다."""
    rows = _read_ledger_raw_strict(home).get("leases", [])
    if not isinstance(rows, list):
        raise ValueError("worktree lease 장부의 'leases' 값이 list가 아님")
    leases: list[worktree_records.Lease] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"worktree lease 장부의 leases[{index}] 값이 object가 아님")
        leases.append(worktree_records.Lease.from_dict(row))
    return leases


def _write_ledger(home, leases: list[worktree_records.Lease]) -> None:
    """`leases` 컬렉션만 교체해 장부를 atomic replace 로 쓴다. **_lease_lock 보유 전제**.

    **형제 최상위 키 보존**(top-level round-trip): 현 파일 원본을 읽어(`_read_ledger_raw`)
    `leases` 키만 새 값으로 덮고 나머지(`tasks`·미지 additive 컬렉션)는 그대로 재방출한다 — 옛
    `{"leases": [...]}` 통짜 쓰기는 형제 컬렉션을 조용히 드롭했다(silent drop). read-modify-write
    가 같은 `_lease_lock` 안에서 직렬화되므로 read↔write 사이 파일 변동은 없다.
    """
    # write-capable 최종 경계도 strict 원본을 재확인한다. 호출부가 실수로 fail-soft read 결과를
    # 넘기거나 read 이후 장부가 손상돼도 빈 dict로 축약해 형제 컬렉션을 덮어쓰지 않는다.
    data = _read_ledger_raw_strict(home)
    data["leases"] = [l.to_dict() for l in leases]
    _write_ledger_raw(home, data)


def _read_tasks(home) -> list[worktree_records.Task]:
    """장부의 `tasks` 컬렉션을 읽는다. 부재/손상 → 빈 리스트(fail-soft). **_lease_lock 보유 전제**."""
    rows = _read_ledger_raw(home).get("tasks", [])
    if not isinstance(rows, list):
        return []
    return [worktree_records.Task.from_dict(d) for d in rows if isinstance(d, dict) and d.get("name")]


def _read_tasks_strict(home) -> list[worktree_records.Task]:
    """task 컬렉션 strict 조회 — 장부 손상/읽기 오류/잘못된 행을 예외로 전파한다."""
    rows = _read_ledger_raw_strict(home).get("tasks", [])
    if not isinstance(rows, list):
        raise ValueError("worktree lease 장부의 'tasks' 값이 list가 아님")
    tasks: list[worktree_records.Task] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("name"):
            raise ValueError(f"worktree lease 장부의 tasks[{index}] 값이 유효한 task object가 아님")
        tasks.append(worktree_records.Task.from_dict(row))
    return tasks


def _write_tasks(home, tasks: list[worktree_records.Task]) -> None:
    """`tasks` 컬렉션만 교체해 장부를 atomic replace 로 쓴다 — 형제 `leases`·미지 키 보존(top-level
    round-trip). **_lease_lock 보유 전제**."""
    # `_write_ledger`와 같은 최종 방어: 손상 원본을 빈 dict로 축약한 뒤 tasks만 쓰는
    # destructive rewrite를 허용하지 않는다.
    data = _read_ledger_raw_strict(home)
    data["tasks"] = [t.to_dict() for t in tasks]
    _write_ledger_raw(home, data)


def list_tasks(home) -> list[worktree_records.Task]:
    """전 task 레코드 (조회 전용·부작용 0). 단일 파일 스캔 소비."""
    with _lease_lock(home):
        return _read_tasks(home)


def find_task(home, name: str) -> "Task | None":
    """이름으로 task 레코드를 찾는다 — 없으면 None."""
    with _lease_lock(home):
        for t in _read_tasks(home):
            if t.name == name:
                return t
    return None


def find_task_strict(home, name: str) -> "Task | None":
    """이름으로 task를 strict 조회한다 — 장부 손상/읽기 오류는 예외로 전파한다."""
    with _lease_lock(home):
        for task in _read_tasks_strict(home):
            if task.name == name:
                return task
    return None


def slots_for_task(home, name: str) -> list[worktree_records.Lease]:
    """이 task(session==name) 명의로 **leased** 인 슬롯 리스트 (조회 전용·부작용 0).

    슬롯↔task 연결 = `lease.session == task 이름` — alloc `--task <task>` 이 session=이름 으로
    슬롯을 리스하므로(직교 정체성이 lease 의 session 축에 실린다). release `--task` 소유검사와
    `end_task` 의 dirty 검사·일괄 반납 대상이 이걸 본다. idle/미점유 슬롯은 session 이 비어 제외.
    """
    with _lease_lock(home):
        return [l for l in _read_ledger(home) if l.state == "leased" and l.session == name]


def slots_for_task_strict(home, name: str) -> list[worktree_records.Lease]:
    """``slots_for_task``의 안전 게이트용 strict 변형.

    실제 빈 장부/보유 0개는 ``[]``이지만 JSON 손상·읽기 오류·스키마 손상은 예외로 전파한다.
    일반 조회 소비자는 기존 fail-soft ``slots_for_task``를 계속 사용한다.
    """
    with _lease_lock(home):
        leases = [
            lease
            for lease in _read_ledger_strict(home)
            if lease.state == "leased" and lease.session == name
        ]
    # 이 값은 bootstrap에서 cwd로 직접 이어져 git/pytest를 실행한다. 장부 유입도 caller 입력과
    # 같은 canonical 경계를 통과시켜 절대경로/traversal/repo 불일치가 REPO 밖 cwd가 되는 것을 막는다.
    for lease in leases:
        slot = lease.slot
        repo = lease.repo
        if not isinstance(slot, str):
            raise ValueError(
                f"task {name!r} 보유 슬롯 식별자 {slot!r} 형식 오류 — 문자열 경로여야 한다"
            )
        try:
            worktree_paths._normalize_slot(home, slot)   # 값-경계(구문·위치·해소) — cwd 로 이어지기 전 단일 불변식.
        except worktree_records.SlotResolutionError as exc:
            raise ValueError(
                f"task {name!r} 보유 슬롯 식별자 {slot!r} 형식 오류 — {exc}"
            ) from exc
        # 행의 repo 값이 슬롯 이름이 주장하는 repo 와 어긋나면 장부 모순이다. 이름이 repo 를
        # 주장하지 않는 슬롯(`.` 등)은 대조 대상이 아니다 — 이름 모양을 강제하지 않는다.
        claimed_repo, separator, slot_number = identity_args.slot_path_name(slot).rpartition("_")
        if not isinstance(repo, str) or not repo:
            raise ValueError(
                f"task {name!r} 보유 슬롯 {slot!r}의 repo 값이 비었다 — 장부 행이 불완전하다"
            )
        if separator and slot_number.isdigit() and claimed_repo != repo:
            raise ValueError(
                f"task {name!r} 보유 슬롯 {slot!r}의 repo {repo!r}가 "
                "슬롯 식별자의 repo와 일치하지 않음"
            )
    return leases


def list_leases(home) -> list[worktree_records.Lease]:
    """현재 리스 장부 전체를 읽어 반환한다 (조회·진단용·pm-config status)."""
    with _lease_lock(home):
        return _read_ledger(home)


def read_lease(home, slot: str) -> "Lease | None":
    """슬롯 lease 를 장부에서 읽어 반환한다 — read-only 조회 프리미티브 (`record_git_snapshot` 짝).

    `record_git_snapshot` 이 갱신하는 `lease.git`(도착 기대 스냅)을 *갱신 전에* 조회하려는
    호출부(cli.handoff.PmHandoff._record_slot_snapshot 의 실갱신/무변경 판별)를 위한 얇은 단일-lease
    read. `_read_ledger` 를 짧게 lock 안에서 훑어 slot 일치 lease 를 반환한다(없으면 None).
    부작용/git 조회 없음(순수 장부 read)."""
    with _lease_lock(home):
        leases = _read_ledger(home)
        return next((l for l in leases if l.slot == slot), None)


def read_lease_strict(home, slot: str) -> "Lease | None":
    """mutation 안전 게이트용 단일 lease strict 조회 — 손상/읽기 오류를 예외로 전파한다."""
    with _lease_lock(home):
        leases = _read_ledger_strict(home)
        return next((lease for lease in leases if lease.slot == slot), None)


def lease_owned_by_task_strict(home, slot: str, task: str) -> bool:
    """슬롯이 현재도 ``task``의 leased 소유인지 락 안에서 strict 확인한다."""
    with _lease_lock(home):
        lease = next((item for item in _read_ledger_strict(home) if item.slot == slot), None)
        return lease is not None and lease.state == "leased" and lease.session == task


_LEASE_ROLE_READONLY = "readonly"   # role 값(identity_args._LEASE_ROLE_READONLY 정합·모듈 격리 inline).


def _slot_role(home, slot: str) -> str:
    """슬롯의 role 을 장부에서 읽는다 — 미등록/미기록이면 "work" (mutation 거부/status 소비).

    `compare_slot_git` 과 동형으로 `_lease_lock` 은 장부 read 동안만 짧게 잡는다. 슬롯이 장부에
    없거나 role 필드가 없으면(구 장부) "work"(작업 슬롯·하위호환·fail-soft)."""
    with _lease_lock(home):
        leases = _read_ledger(home)
    target = next((l for l in leases if l.slot == slot), None)
    return target.role if target is not None else "work"


class ReadonlySlotMutation(RuntimeError):
    """readonly 공유 슬롯(role="readonly")에 mutation op(rebase·switch·task branch·dev·sync)을 시도함 — 거부.

    readonly 슬롯은 문서 검증 기준면(released base)이라 슬롯의 git 상태를 바꾸는 엔진 op 을 거부한다
    (갱신은 `refresh` 만·fetch→detach 이동). fs 레벨 쓰기 차단은 안 한다(엔진 경로 한정).
    `op` = 거부된 연산명(진단용). (`RuntimeError` 서브클래스 — `BareRepoMissing` 동형·파사드 rc 1.)"""

    def __init__(self, slot: str, op: str):
        self.slot = slot
        self.op = op
        super().__init__(
            f"슬롯 {slot!r} 은 readonly 공유 슬롯(role=readonly)이라 `{op}` 를 거부한다 — 문서 검증 "
            f"기준면(released base·detached)이라 git 상태를 바꾸는 op 은 불가하다. 갱신은 "
            f"`{lease_store._runtime_skill_entry('pm-worktree')} refresh {slot} "
            f"[--onto <branch>]`(fetch→detach 이동)로만 한다."
        )


def _reject_readonly_mutation(home, slot: str, op: str, *, git_runner: worktree_records.GitRunner | None = None) -> None:
    """슬롯이 readonly면 `ReadonlySlotMutation` raise — switch/task branch/dev/sync 진입 가드.

    엔진 경로 한정 거부. 판별 축은 canonical `lease.role`(0단계 carve-out 예외와 동일 축).
    git_runner 는 시그니처 정합용(현 판별은 장부만 읽어 미사용). mutation 선행 판정이므로
    `read_lease_strict`를 써 손상 장부를 기본 work role로 축약한 뒤 Git 변경을 시작하지 않는다.
    이미 락을 쥔 호출부(release)는 이 헬퍼 대신 보유 중인 `lease.role` 을 직접 검사한다
    (non-reentrant flock 재취득 = 데드락)."""
    lease = read_lease_strict(home, slot)
    if lease is not None and lease.role == _LEASE_ROLE_READONLY:
        raise ReadonlySlotMutation(slot, op)


class ReadonlySlotNotLeasable(RuntimeError):
    """readonly 공유 슬롯에 lease-lifecycle op(release·force_release)을 시도함 — 거부.

    readonly 슬롯은 무소유 공유 자산이라 대여·반납 대상이 아니다. 명시 지정(`release <slot>`·
    `force_release`)을 거부한다 — idle 화되면 alloc 이 그 슬롯을 work 슬롯으로 점유해 role 이
    유실되는 깨진 상태를 부른다. `op` = 거부된 연산명(진단용). 제거는 `worktree remove --force`,
    갱신은 `refresh`. (`RuntimeError` — 파사드 rc 1.)"""

    def __init__(self, slot: str, op: str):
        self.slot = slot
        self.op = op
        super().__init__(
            f"슬롯 {slot!r} 은 readonly 공유 슬롯(role=readonly)이라 `{op}`(대여/반납) 대상이 "
            f"아니다 — 무소유 공유 자산(배타 대여 없음·session 없음). 제거하려면 "
            f"`worktree remove {slot} --force`, 최신 갱신은 "
            f"`{lease_store._runtime_skill_entry('pm-worktree')} refresh {slot}`."
        )


def _is_registered_task(home, name: str) -> bool:
    """`name` 이 tasks 장부에 등록된 task 인가 — 스킵 안내 전용 조회 (부작용 0).

    ⚠ **`_lease_lock` 밖에서만 호출**한다 — 내부의 `find_task` 가 락을 잡는데 `_lease_lock` 은
    비재진입이라 락 보유 중 호출하면 데드락이다(현 호출부 = CLI 요약 루프·락 밖·내부 리뷰 지적).

    not-owner 스킵의 보유자가 **task 명의**면 해소는 `--task <task>`(소유 축을 밝히는 것)이다 —
    사용자가 그걸 추측하지 않게 CLI 안내에 실값을 싣기 위한 판정. 장부 IO/파싱 실패는 False
    (fail-soft — 안내 문구가 없을 뿐 스킵 자체는 그대로 loud). 안내용 조회가 CLI 를 죽이지
    않는다(`pm_config.cmd_status` 의 task 축 fail-soft 와 동형)."""
    try:
        return find_task(home, name) is not None
    except Exception:  # noqa: BLE001 — 안내 전용 조회·실패는 문구 생략(스킵 판정엔 무영향).
        return False
