"""티켓 스냅샷의 파생 보드 갱신 (층: app)."""
from __future__ import annotations

from pm_engine.infra import clock
from pm_engine.infra import fs
from pm_engine.infra import pm_paths
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store


def refresh_board(home) -> None:
    """Regenerate .project_manager/wiki/board.md.

    scan(tickets/) + render + write 를 *하나의* `board_lock()` 구간 안에서 한다
    (공유 단일파일 lost-update 방지). write 만 감싸면, 동시 변경 시
    A 가 stale 스냅샷을 떠 둔 사이 B 가 scan+write 를 끝내도, A 가 락을 잡아 자기
    stale 스냅샷으로 board.md 를 덮어써 B 의 갱신을 유실한다. scan 까지 락 안에서
    하면 *마지막 writer 가 모든 선행 write 이후의 ticket 상태를 scan* 하므로
    board.md 가 항상 최신을 반영한다.

    **재진입 금지**(board_lock docstring) — board_lock 보유 중에는 부르지 않는다.
    일반 호출자(cmd_new·complete·block·unclaim·unblock·refresh)는 락 밖에서 부른다.
    strict claim은 confirm/rollback까지 lock을 유지해야 하므로 public wrapper 대신
    `_refresh_board_locked()`를 직접 호출한다.
    """
    with fs.board_lock(home):
        _refresh_board_locked(home)


def _refresh_board_locked(home) -> None:
    """board.md 재생성의 scan+render+write 본체. **board_lock 보유 전제**."""
    by_status: dict[str, list[dict]] = {s: [] for s in ticket_rules.STATUS_DIRS}
    for status in ticket_rules.STATUS_DIRS:
        for p in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                continue        # 손상 1건이 board.md 재생성 전체를 죽이지 않는다(경고는 위에서).
            by_status[status].append(loaded[0])

    lines: list[str] = [
        "---",
        "title: Ticket Board",
        "type: dashboard",
        f"updated: {clock.now_utc()}",
        "---",
        "",
        "# Ticket Board",
        "",
        "> 자동 생성 파생물 (git-untracked) — `board.py` 변경 명령마다 로컬 갱신 · `board.py refresh` 로 재생성. 단일 진실은 `tickets/`, 라이브 상태는 `board.py list`. 수동 편집 금지.",
        "> 작업 흐름: [`tickets/README.md`](tickets/README.md).",
        "",
    ]
    totals = " · ".join(f"{s}={len(by_status[s])}" for s in ticket_rules.STATUS_DIRS)
    lines.append(f"**현황:** {totals}")
    lines.append("")

    # STATUS_DIRS 자동 추종의 **유일한 예외** — 새 상태를 STATUS_DIRS 에 더하면 여기 한 줄도
    # 함께 더해야 한다(빠뜨리면 이 렌더가 KeyError 로 죽는다).
    emoji = {"open": "🟢", "claimed": "🟡", "blocked": "🔴", "done": "✅",
             "discarded": "🗃️"}

    for status in ticket_rules.STATUS_DIRS:
        items = by_status[status]
        # 종결 섹션(done·discarded)은 비면 헤더도 접는다 — 보드가 진행 중 작업에 집중하게.
        if status in ticket_rules.TERMINAL_STATUS_DIRS and not items:
            continue
        lines.append(f"## {emoji[status]} {status.upper()} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("*없음*")
            lines.append("")
            continue
        if status == "open":
            lines.append("| ID | Title | depends_on | touches | tags |")
            lines.append("|---|---|---|---|---|")
            for fm in items:
                dep = ", ".join(fm.get("depends_on") or []) or "—"
                touches = fm.get("touches")
                if touches is None:
                    tch = "—"
                elif isinstance(touches, list) and all(isinstance(item, str) for item in touches):
                    tch = ", ".join(touches[:3]) or "—"
                else:
                    # 파생 화면만 오류를 표시한다. 명세 원값과 scope 검증 입력은 보존한다.
                    tch = "touches 형식 오류"
                tag = ", ".join(ticket_rules._tag_values(fm))
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | {dep} | {tch} | {tag} |"
                )
        elif status == "claimed":
            lines.append("| ID | Title | Claimed by | Since (UTC) |")
            lines.append("|---|---|---|---|")
            for fm in items:
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | "
                    f"`{fm.get('claimed_by','')}` | {(fm.get('claimed_at') or '')[:19]} |"
                )
        elif status == "blocked":
            lines.append("| ID | Title | (reason at the bottom of the file) |")
            lines.append("|---|---|---|")
            for fm in items:
                lines.append(f"| {fm['id']} | {fm.get('title','')} | — |")
        elif status == "done":
            # Show most-recent 10
            lines.append("| ID | Title | Completed (UTC) |")
            lines.append("|---|---|---|")
            recent = sorted(items, key=lambda f: f.get("completed_at") or "",
                            reverse=True)[:10]
            for fm in recent:
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | "
                    f"{(fm.get('completed_at') or '')[:19]} |"
                )
        elif status == "discarded":
            # 처분 종결 — done 과 섞이지 않게 별도 섹션에 처분 종류·사유를 함께 낸다.
            lines.append("| ID | Title | 처분 | 사유 |")
            lines.append("|---|---|---|---|")
            for fm in sorted(items, key=lambda f: f.get("id") or ""):
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | "
                    f"{fm.get('disposition') or '—'} | "
                    f"{ticket_rules._table_cell(fm.get('disposition_reason'))} |"
                )
        lines.append("")

    # scan+render+write 가 모두 호출자(refresh_board)의 board_lock 구간 안이다 —
    # 마지막 writer 가 모든 선행 write 이후 상태를 scan 하므로 stale write 가 없다.
    # claim은 재진입을 피하려 이 locked 본체를 직접 호출한다. fresh/부분 scaffold에서도 public
    # wrapper에 기대지 않고 쓸 수 있게 destination parent 생성도 본체의 write 계약에 포함한다.
    (pm_paths.wiki_dir(home) / "board.md").parent.mkdir(parents=True, exist_ok=True)
    (pm_paths.wiki_dir(home) / "board.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
