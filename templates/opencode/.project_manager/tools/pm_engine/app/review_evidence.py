"""설계 계약 수집과 PM 확인 기입 (층: app)."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
from pm_engine.domain import delegate_errors
from pm_engine.infra import fs
from pm_engine.infra import pm_paths
from pm_engine.domain import review_contract
from pm_engine.domain import review_form
from pm_engine.app import round_maintenance
from pm_engine.app import round_seed
from pm_engine.infra import ticket_store



def architect_tests_from_rounds(
    rounds: Sequence, *, required: bool = True,
) -> tuple[review_contract.ArchitectTest, ...]:
    """마지막 architect 산출의 테스트 계약. 신규 준비면 부재도 fail-loud한다."""
    candidates = [
        item for item in rounds
        if item.role == "architect" and not getattr(item, "pending", False)
    ]
    if not candidates:
        if required:
            raise delegate_errors.DelegateError("developer 착수 전 architect 테스트 계약이 없습니다")
        return ()
    latest = max(candidates, key=lambda item: item.ordinal)
    if review_form.ARCHITECT_TEST_BLOCK not in latest.text and not required:
        return ()
    return review_contract.parse_architect_tests(latest.text)


def append_pm_review_confirmation(
    home, ticket: str, section: str, *, rounds: Sequence | None = None,
) -> Path:
    """확인 절을 티켓 명세 PM 영역에 append/동일 round 병합하고 board-git 에 싣는다.

    명세 쓰기 seam 은 새로 만들지 않는다 — 기존 조합(`ticket_store.load_ticket` →
    `ticket_store.dump_ticket_atomic` → `round_maintenance._rounds_mutation_sync_paths`)을 그대로 쓴다.
    """
    changed = False
    with fs.board_lock(home):
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
        _status, path = found
        fm, body = ticket_store.load_ticket(path)
        if rounds is None:
            rounds = round_seed.load_rounds(
                pm_paths.tickets_dir(home), ticket, ticket_text=body,
            )
        merged, changed = review_contract._merge_pm_review_confirmation_section(
            body, section, rounds=rounds,
        )
        if changed:
            ticket_store.dump_ticket_atomic(path, fm, merged)
    if changed:
        round_maintenance._rounds_mutation_sync_paths(home, f"pm-review confirmation {ticket}", (path,))
    return path
