"""세션 복구 스냅샷 구성."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import ctx_recovery
from pm_engine.infra import delegate_ledger
import os
from pm_engine.infra import pm_paths
from pm_engine.infra import raw_output
from pm_engine.app import session_identity
from pm_engine.infra import session_store
import shutil
import subprocess
from pm_engine.infra import task_store
from pm_engine.domain import ticket_rules
import time


def _identity_section(home, session_id: str, task: str) -> str:
    state_path = pm_paths.task_pm_state_file(home, task)
    return (
        f"{ctx_recovery._SNAPSHOT_IDENTITY_HEADING}\n"
        f"- task: {task}\n"
        f"- 세션: {session_id.strip()}\n"
        f"- PM 홈: {home.local_dir.parent.parent.resolve(strict=False)}\n"
        f"- pm_state: {state_path}\n"
    )


def _ticket_counts(home) -> tuple[Path, dict[str, int]]:
    """`pm_home` board 의 상태별 티켓 수 — 버킷은 `ticket_rules.STATUS_DIRS`(단일 진실) 파생이다.

    상태 디렉토리 스캔 자체는 board.py 호출 없이 디렉토리 존재만 본다(집합만 `ticket_rules` 에서 온다).
    """
    manager = home.local_dir.parent
    # board 분리 형상 우선, legacy는 wiki/tickets.
    board_root = manager / "board"
    tickets = (board_root if board_root.is_dir() else manager / "wiki") / "tickets"
    counts: dict[str, int] = {}
    for status in ticket_rules.STATUS_DIRS:
        try:
            counts[status] = sum(1 for path in (tickets / status).glob("T-*.md") if path.is_file())
        except OSError:
            counts[status] = 0
    return tickets, counts


def _lease_task_slots(home, task: str) -> list[tuple[str, Path]]:
    """현재 task 가 leased 로 보유한 슬롯 — (장부 표기, 해소 경로) 쌍.

    ``_ledger_section``(장부 표기 문자열만)과 슬롯 WIP 프로브(해소 경로가 필요)가 같은
    lease 판정 한 번을 공유한다 — 판정을 두 곳에 복제하면 갈릴 수 있다.
    """
    slots: list[tuple[str, Path]] = []
    for row in session_store._lease_rows(home):
        if row.get("state") == "leased" and row.get("session") == task and row.get("slot"):
            resolved = session_store._lease_slot_path(home, row)
            if resolved is not None:
                slots.append((str(row["slot"]), resolved))
    return slots


def _ledger_section(home, task: str) -> str:
    active = session_store._active_tasks(home)
    leases = session_store._lease_rows(home)
    states: dict[str, int] = {}
    for row in leases:
        state = str(row.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
    task_slots = [label for label, _path in _lease_task_slots(home, task)]
    tickets, counts = _ticket_counts(home)
    state_text = ", ".join(f"{key} {states[key]}" for key in sorted(states)) or "장부 없음"
    # 버킷 순서·집합은 `ticket_rules.STATUS_DIRS` 그대로다 — 새 상태는 손대지 않고
    # 뒤에 붙고, 기존 status 표기는 바뀌지 않는다.
    count_text = " / ".join(f"{key} {value}" for key, value in counts.items())
    return (
        "## 장부 포인터\n"
        f"- 활성 tasks ({len(active)}): {', '.join(active) if active else '(없음)'}\n"
        f"- worktree leases: {state_text}\n"
        f"- 현재 task 슬롯: {', '.join(sorted(task_slots)) if task_slots else '(없음)'}\n"
        f"- board tickets: {count_text}\n"
        f"- tickets 경로: {tickets}\n"
    )


def _todo_section(home, task: str) -> str:
    """대상 Home의 열린 todo 조회와 표시.

실행 패키지의 task_store로 대상 Home 데이터를 읽는다. 대상 홈의 엔진 파일은 실행하지 않는다.
없는 todo는 빈 목록으로 표시하고 손상 JSON·스키마 오류는 호출부로 전파한다."""
    todos = task_store.read_task_todos(home, task)
    if not todos:
        return (
            "## 열린 todo (파일 실측)\n"
            f"- 없음 — 파일 수정 전에 `pm-config task todo add {task} <할 일 한 줄>` 로 등록한다"
            " (등록 전에 파일을 고치면 쓰기 판정이 경고를 붙인다).\n"
        )
    lines = [
        f"- {task_store.format_todo_line(todo)}"
        for todo in todos[:ctx_recovery._TODO_MAX_SHOWN]
    ]
    hidden = len(todos) - len(lines)
    if hidden > 0:
        lines.append(f"- 외 {hidden}건 — `pm-config task todo list {task}`")
    return "## 열린 todo (파일 실측)\n" + "\n".join(lines) + "\n"


def _recovery_section(task: str) -> str:
    bootstrap = f"python3 .project_manager/tools/pm_bootstrap.py --task {task}"
    return (
        "## 복구 포인터\n"
        f"- `{bootstrap}`로 장부를 다시 펼친다.\n"
        "- 자동 생성된 compaction checkpoint 골격의 구간·서사 불릿은 PM 판단으로 채운다.\n"
        f"- {ctx_recovery.CTX_GUARD_CONTINUITY_GUIDANCE}\n"
    )


def _hearsay_section() -> str:
    """always-keep 절 — ``build_snapshot`` 의 deadline 폐기 순서에서도 살아남는다."""
    return ctx_recovery.SNAPSHOT_HEARSAY_WARNING


def _git_status_counts(path: Path, timeout: float) -> tuple[int, int, int] | None:
    """``git --no-optional-locks status --porcelain`` 1회 — staged/미staged/untracked 개수.

    git 부재·예산 0 → ``None``(spawn 없음), rc≠0(비-repo 등) → ``None``. 실행 예외(timeout 포함)는
    전파한다.
    """
    if timeout <= 0 or shutil.which("git") is None:
        return None
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )
    if result.returncode != 0:
        return None
    staged = unstaged = untracked = 0
    for line in result.stdout.splitlines():
        if not line:
            continue
        if line.startswith("??"):
            untracked += 1
            continue
        index_state = line[0]
        worktree_state = line[1] if len(line) > 1 else " "
        if index_state not in (" ", "?"):
            staged += 1
        if worktree_state not in (" ", "?"):
            unstaged += 1
    return staged, unstaged, untracked


def _wip_probe_targets(home, task: str) -> tuple[list[tuple[str, Path]], int]:
    """WIP 프로브 대상 — PM 홈 자리를 먼저 예약한 뒤 leased 슬롯으로 나머지를 채운다.

    반환은 ``(프로브할 대상, 상한 때문에 못 본 leased 슬롯 수)``. leased 슬롯이 상한보다 많을
    때 뒤에 append 되는 PM 홈이 slice 로 조용히 밀려나던 결함(4-slot 프로브
    실측: PM 홈·slot-d 소실)을 막는다 — PM 홈이 기존 슬롯과 겹치지 않을 때만 자리를 예약해
    상한을 낭비하지 않는다. 못 본 슬롯은 spawn 없이 개수만 돌려준다(``_wip_slot_line`` 이
    "외 N개(프로브 생략)"로 표면화 — 침묵 누락 금지).
    """
    slots = list(_lease_task_slots(home, task))
    seen = {os.path.normcase(str(path.resolve(strict=False))) for _label, path in slots}
    pm_home_path = home.local_dir.parent.parent
    pm_home_key = os.path.normcase(str(pm_home_path.resolve(strict=False)))
    pm_home_distinct = pm_home_key not in seen
    slot_budget = max(0, ctx_recovery._WIP_PROBE_MAX_CALLS - (1 if pm_home_distinct else 0))
    probed_slots = slots[:slot_budget]
    skipped = len(slots) - len(probed_slots)
    targets = list(probed_slots)
    if pm_home_distinct:
        targets.append(("PM 홈", pm_home_path))
    return targets, skipped


def _wip_slot_line(home, task: str, *, deadline: float, monotonic=time.monotonic) -> str:
    """슬롯 WIP 프로브 결과 불릿 — subprocess 0 계약의 유일한 예외(장부 조회는 여전히 spawn 0).

    각 프로브 **직전**에 ``deadline - monotonic()`` 으로 잔여 예산을 다시 계산한다 — 한 번 계산한
    값을 여러 호출에 재사용하면(4-slot·잔여 0.2s 프로브에서 timeout 합계 0.6s
    실측) 앞선 호출의 실제 소요가 뒤 호출 예산을 깎지 않는다는 착시가 생겨 snapshot 자체의
    deadline 을 넘길 수 있다. 잔여가 0 이하면 그 대상부터는 spawn 하지 않고 "예산 소진"으로
    표기한다(장부 조회 subprocess 0 계약과 별개로, 프로브 자신의 시간 계약).
    """
    targets, skipped = _wip_probe_targets(home, task)
    parts: list[str] = []
    for label, path in targets:
        safe_label = ctx_recovery._inflight_safe_text(label)
        remaining = deadline - monotonic()
        if remaining <= 0:
            parts.append(f"{safe_label} 예산 소진")
            continue
        timeout = min(remaining, ctx_recovery._WIP_PROBE_TIMEOUT_SECONDS)
        counts = _git_status_counts(path, timeout)
        if counts is None:
            parts.append(f"{safe_label} 조회 불가")
        else:
            staged, unstaged, untracked = counts
            parts.append(
                f"{safe_label} staged {staged} / 미staged {unstaged} / untracked {untracked}"
            )
    content = ctx_recovery._fold_join(parts, ctx_recovery._INFLIGHT_SLOT_SHOWN, "개") if parts else "대상 없음"
    if skipped > 0:
        content += f" 외 {skipped}개(프로브 생략)"
    return ctx_recovery._inflight_capped_line(prefix="- 슬롯 WIP: ", content=content, suffix="")


def _inflight_section(home, task: str, *, deadline: float, monotonic=time.monotonic) -> str:
    """미회수 라운드 준비·미마감 raw·claimed 티켓·슬롯 WIP 을 in-process 조회로 편입한다.

    장부 조회는 subprocess 0(``delegate_ledger.ticket_copy_records``·``raw_output.raw_records``
    in-process 호출) — WIP git 프로브(``_wip_slot_line``)만 유일한 예외(자체 상한). 조회 예외는
    전파해 snapshot 명령이 실패한다.
    각 불릿은 ``_inflight_safe_text``/``_inflight_capped_line`` 으로 이미 단일 줄이지만, 물리
    행 수도 조립 단계에서 실제로 강제한다(``_INFLIGHT_MAX_LINES`` — 선언만
    되고 조립에서 미강제였던 결함).
    """
    unharvested_rows = delegate_ledger.ticket_copy_records(home.local_dir.parent.parent, unharvested=True)
    _raw_dir, raw_ledger = raw_output.raw_storage_paths(home.local_dir.parent.parent, "delegate")
    unfinished_raw_rows = raw_output.unfinished_raw_records(raw_ledger, lock=False)
    tickets_dir, _counts = _ticket_counts(home)
    claimed_ids = [
        path.stem for path in (tickets_dir / "claimed").glob("T-*.md")
        if path.is_file()
    ]

    lines = [
        ctx_recovery._unharvested_copies_line(unharvested_rows),
        ctx_recovery._unfinished_raw_line(unfinished_raw_rows, raw_ledger),
        ctx_recovery._claimed_tickets_line(claimed_ids),
        _wip_slot_line(home, task, deadline=deadline, monotonic=monotonic),
    ]
    body_lines = "\n".join(lines).splitlines()
    body = "\n".join(body_lines[:ctx_recovery._INFLIGHT_MAX_LINES])
    return "## 진행 중 작업 (장부 실측)\n" + body + "\n"


def build_snapshot(home, session_id: str | None, *, monotonic=time.monotonic) -> tuple[str | None, str | None]:
    """주입 최종 텍스트를 조립한다. 반환은 ``(text, warning)``.

    정체성은 `resolve_session_task`(세션 판정) 하나다. 장부 조회는 subprocess·lock 0 (진행 중
    작업 절의 WIP git 프로브만 상한 3회의 유일한 예외 — `_wip_slot_line` 참고). 절 순서: 전언
    경고(always-keep) → 장부 포인터 → 진행 중 작업 → 열린 todo → 복구 포인터.
    """
    started = monotonic()
    identity_name, reason = session_identity.resolve_session_task(home, session_id)
    if identity_name is None:
        return None, (
            f"[pm-snapshot] 세션 task 미해소: {reason}. 재주입 생략. "
            "이 세션에서 pm_bootstrap.py --task 이름 으로 task 를 연다."
        )
    identity = _identity_section(home, session_id, identity_name)
    sections: list[str] = []
    # 전언 경고는 always-keep 접두 — 뒤 절부터 폐기하는 timeout 분기·총량 cap 경로 모두에서
    # ``required=always_keep`` 로 cap_snapshot_sections 에 보존 경계를 명시한다.
    sections.append(_hearsay_section())
    always_keep = len(sections)
    # WIP 프로브(F-002)가 절 진입 전이 아니라 **호출 직전마다** 잔여를 재는 절대 deadline.
    deadline = started + ctx_recovery.SNAPSHOT_TIMEOUT_SECONDS
    if monotonic() - started >= ctx_recovery.SNAPSHOT_TIMEOUT_SECONDS:
        return ctx_recovery.cap_snapshot_sections(identity, sections, required=always_keep), None

    sections.append(_ledger_section(home, identity_name))
    if monotonic() - started >= ctx_recovery.SNAPSHOT_TIMEOUT_SECONDS:
        return ctx_recovery.cap_snapshot_sections(
            identity, sections[:always_keep], required=always_keep,
        ), None

    sections.append(
        _inflight_section(home, identity_name, deadline=deadline, monotonic=monotonic)
    )
    if monotonic() - started >= ctx_recovery.SNAPSHOT_TIMEOUT_SECONDS:
        return ctx_recovery.cap_snapshot_sections(
            identity, sections[:always_keep], required=always_keep,
        ), None

    sections.append(_todo_section(home, identity_name))
    if monotonic() - started >= ctx_recovery.SNAPSHOT_TIMEOUT_SECONDS:
        return ctx_recovery.cap_snapshot_sections(
            identity, sections[:always_keep], required=always_keep,
        ), None
    sections.append(_recovery_section(identity_name))
    return ctx_recovery.cap_snapshot_sections(identity, sections, required=always_keep), None
