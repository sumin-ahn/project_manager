"""라운드 변경 및 동기화 (층: app)."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
from pm_engine.app import board_dashboard
from pm_engine.app import board_sync
from pm_engine.domain import cluster_rules
from pm_engine.infra import cluster_store
from pm_engine.infra import delegate_ledger
from pm_engine.infra import pm_paths
from pm_engine.domain import round_rules
from pm_engine.app import round_seed
import sys
from pm_engine.infra import ticket_store


def _section_add_owning_cluster(home, tid: str, path: Path) -> str | None:
    """그 티켓의 라운드 수열을 **소유하는 묶음 장부 id**(없으면 None).

    장부가 있으면 그 티켓의 라운드는 고정 예산 수열이 지배한다 — 라운드를 여는 표면은 위임
    준비(`pm_delegate.py ticket prepare`) 하나뿐이고, 그 판정을 거치지 않는 `section-add` 가
    같은 자리를 열면 예산이 세는 수열 밖에서 라운드가 늘어난다. 장부가 없는 티켓(발행 전
    draft)은 예산 판정의 입력 자체가 없어 이 명령이 유일한 예약 표면이다.

    손상 명세는 여기서 판정하지 않는다 — 클러스터 해소는 `ticket_cluster_from_text` 와 같은
    규칙(필드 부재·파싱 실패는 크기 1)이라 `load_ticket_soft` 로 읽는다. 이 판정이 호출부
    (`cmd_section_add`)의 `load_ticket` **앞**이므로, 장부를 가진 티켓은 명세 YAML 이 손상돼도
    loud 판독 대신 아래 묶음 소유 거부 문구를 받는다 — 둘 다 첫 쓰기 앞 rc=1 이라 갈리는 것은
    진단 문구뿐이다. 장부가 없는 티켓은 종전대로 `load_ticket` 이 손상을 loud 하게 낸다.
    """
    loaded = ticket_store.load_ticket_soft(home, path)
    fm = loaded[0] if loaded is not None else {}
    cluster = cluster_rules.ticket_cluster(tid, fm if isinstance(fm, dict) else {})
    return cluster if cluster_store.load_cluster(home, cluster) is not None else None

def _active_ticket_path(home, tid: str, action: str, *, role: str | None=None) -> tuple[int, Path | None]:
    """section-add/tier/design 대상 해소 — open/claimed는 공통, draft는 action별로 다르게 연다.

    tier는 계속 open/claimed로 닫는다. section-add는 draft×architect만 연다(설계 bootstrap).
    design은 draft 전체를 연다 — `design: required`가 promote를 막으므로 draft 단계에서
    상태를 `done`으로 올릴 수 없으면 setter가 있어도 실제 막힌 상황을 풀지 못한다.
    blocked/done 및 위 두 draft 예외 밖(section-add×developer|code-reviewer 등)은 같은 경계에서
    거부한다. directory 상태가 lifecycle 단일 진실인 기존 mutation 규약을 그대로 쓴다.

    section-add 는 그 위에 조건이 하나 더 있다 — **묶음 장부가 없는 티켓**만 연다. 라운드
    예약 표면은 둘이고 정의역이 겹치지 않는다: 장부가 있는 티켓은 위임 준비만(고정 예산이
    수열을 지배한다), 장부가 없는 티켓은 이 명령만.
    """
    try:
        status, path = ticket_store.find_ticket_for_mutation(home, tid)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return (2, None)
    draft_architect = action == 'section-add' and status == 'draft' and (role == 'architect')
    draft_design = action == 'design' and status == 'draft'
    if status not in ('open', 'claimed') and (not (draft_architect or draft_design)):
        if action == 'section-add':
            allowed = 'open/claimed 티켓 또는 draft×architect'
        elif action == 'design':
            allowed = 'open/claimed/draft 티켓'
        else:
            allowed = 'open/claimed 티켓'
        print(f'cannot {action} {tid}: currently in {status}/ ({allowed}만 허용)', file=sys.stderr)
        return (1, None)
    if action == 'section-add':
        cluster = _section_add_owning_cluster(home, tid, path)
        if cluster is not None:
            print(f'cannot section-add {tid}: 묶음 장부 {cluster} 가 이 티켓의 라운드 수열을 소유한다 — 라운드는 위임 준비가 연다(고정 예산 판정)\n  · python3 .project_manager/tools/pm_delegate.py ticket prepare --ticket {tid} --role {role} --cwd <작업공간 절대경로>\n  · 이어 시키는 단계를 같은 순번에 다시 여는 인자는 그 명령의 --help 가 말한다.', file=sys.stderr)
            return (1, None)
    return (0, path)

def _rounds_mutation_sync_paths(home, message: str, paths: Sequence[Path]) -> bool:
    """라운드/티어 mutation이 만진 경로들을 한 스코프 커밋으로 기록한다.

    호출자는 board_lock 밖에서 공개할 경로만 전달한다. True는 로컬 기록 준비 완료 또는
    별도 board-git이 없는 no-op이며, 원격 공유 완료를 뜻하지 않는다. 원격 보류는 기존
    best-effort 출력이 안내하고 draft 라운드의 공개는 promote가 소유한다.

    준비·회수·포기가 직접 호출하는 두 인자 시그니처를 유지한다. 인자를 늘리면 부분 동기된
    PM 홈 사본에서 라운드를 반영한 뒤 CLI가 TypeError로 실패할 수 있다.
    """
    board_dashboard.refresh_board(home)
    return board_sync._board_git_sync_best_effort(home, message, tuple(paths))

_ROUND_PENDING_UNHARVESTED_QUERY = 'python3 .project_manager/tools/pm_delegate.py ticket copies --unharvested'

def _round_pending_ledger_owner(home) -> Path:
    """명시 Home의 코드 좌표에서 미회수 라운드 장부를 소유한 PM 홈을 해소한다.

    해소 실패는 전파하며 다른 모듈의 실행 위치나 전역 재앵커에 의존하지 않는다."""
    return Path(pm_paths.resolve_pm_home_for_repo(home.root)).resolve()

def _round_pending_abandon_command(home, tid: str, ordinal: int, role: str) -> str | None:
    """미회수 장부에서 이 pending 라운드의 실 `--copy`/`--cwd` 를 해소해 완성 abandon 커맨드를 낸다.

    장부 부재·PM 홈 미확정·이 (순번, 역할)의 미회수 행 부재·경로 형태 불일치는 전부 해소
    불능으로 `None`(호출부가 `ticket copies --unharvested` 조회 처방으로 대체한다)."""
    owner = _round_pending_ledger_owner(home)
    try:
        rows = delegate_ledger.ticket_copy_records(owner, ticket=tid, unharvested=True)
    except Exception:
        return None
    match = next((row for row in rows if row.get('ordinal') == ordinal and row.get('role') == role), None)
    if match is None:
        return None
    copy_path = Path(match['copy'])
    relative = delegate_ledger._row_ticket_relative_dir(match) / round_rules.round_filename(match['ordinal'], match['role'])
    rel_parts = relative.parts
    if copy_path.parts[-len(rel_parts):] != rel_parts:
        return None
    cwd = Path(*copy_path.parts[:-len(rel_parts)])
    return f'python3 .project_manager/tools/pm_delegate.py ticket abandon --copy {copy_path} --cwd {cwd} --assume-dead'

def _discard_round_pending_notice(home, tid: str) -> None:
    """미회수(round-pending) 라운드가 있어도 discard 를 막지 않고 ⓘ 로 회수 처방만 안내한다.

    완료 게이트(`_round_completion_problems`)와 같은 축(round-pending=비차단·표시용)이다.
    여러 pending 이 있어도 안내는 **정확히 1줄**(건수 + 첫 pending 의 처방)로 접는다 — pending
    마다 자리표시자 줄을 찍던 옛 형태는 실행 가능한 값이 아니었다(reviewer). round-gap·
    round-dup·round-name 은 완료 게이트가 이미 다루는 축이라 여기서 재확인하지 않는다(discard
    는 그 축을 차단하지 않는다).
    """
    pending = [item for item in round_seed.load_rounds(pm_paths.tickets_dir(home), tid) if item.pending]
    if not pending:
        return
    first = pending[0]
    resolved = _round_pending_abandon_command(home, tid, first.ordinal, first.role)
    prescription = resolved if resolved is not None else _ROUND_PENDING_UNHARVESTED_QUERY
    print(f'ⓘ  round-pending {len(pending)}건({first.path.name}) — 회수하려면 `{prescription}`', file=sys.stderr)
