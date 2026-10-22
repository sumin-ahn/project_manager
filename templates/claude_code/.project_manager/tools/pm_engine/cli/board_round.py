"""라운드 및 tier 명령 (층: cli)."""
from __future__ import annotations

import argparse
from pm_engine.infra import board_git
import datetime
from pm_engine.domain import design_rules
from pm_engine.infra import fs
from pm_engine.infra import pm_paths
from pm_engine.app import round_maintenance
from pm_engine.domain import round_rules
from pm_engine.app import round_seed
from pm_engine.infra import round_store
import sys
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store
from pm_engine.app import tier_signals


def cmd_section_add(home, args: argparse.Namespace) -> int:
    """다음 라운드를 시드로 예약한다 — 슬롯 없는 준비(PM 이 직접 채우는 자리).

    쓰는 것은 `tickets/rounds/<티켓>/NN-<role>.md` 하나뿐이다. 명세 파일은 건드리지 않으므로
    lifecycle writer 와 겹치지 않고, 순번 채번+배타 생성만 짧은 board_lock 이 직렬화한다
    (락 컨텍스트는 seam 이 잡는다 — board_lock 은 재진입이 없어 호출자가 잡은 채 넘기지 않는다).
    """
    role = args.role
    label = round_rules._section_label(role, args.label)
    if label is None:
        print('cannot section-add: --label 은 비어 있거나 개행을 포함할 수 없다', file=sys.stderr)
        return 1
    today = datetime.date.today().isoformat()
    with fs.board_lock(home):
        rc, path = round_maintenance._active_ticket_path(home, args.id, 'section-add', role=role)
        if path is None:
            return rc
        ticket_store.load_ticket(path)
        is_draft = path.resolve().parent == pm_paths.drafts_dir(home).resolve()
        with fs.open_shared(path, binary=False, encoding='utf-8', newline='') as handle:
            spec_text = handle.read()
    try:
        existing = round_seed.load_rounds(pm_paths.tickets_dir(home), args.id, ticket_text=spec_text)
        seed = round_seed.render_round_seed(role, spec_text, today=today, previous_round=round_rules.previous_round_of_role(existing, role), rounds=existing)
        round_path = round_store.reserve_round(pm_paths.tickets_dir(home), args.id, role, content=round_rules._round_seed_header_override(seed, label, role, today), lock=fs.board_lock(home))
    except round_rules.RoundsError as exc:
        print(f'cannot section-add: {exc}', file=sys.stderr)
        return 1
    if is_draft:
        print(f'section added {args.id}: {label} ({role} · {today}) → {pm_paths._rel_to_repo(home, round_path)} (local draft; promote가 출하 소유)')
    else:
        ready = round_maintenance._rounds_mutation_sync_paths(home, f'section-add {args.id} {role}', (round_path,))
        print(f'section added {args.id}: {label} ({role} · {today}) → {pm_paths._rel_to_repo(home, round_path)}{board_git._board_git_mutation_state_suffix(ready)}')
    return 0

def cmd_tier(home, args: argparse.Namespace) -> int:
    """선택 frontmatter `tier`를 기록/갱신한다. 허용값 외 입력은 파일 읽기 전 rc=1."""
    if args.tier not in ticket_rules.TICKET_TIERS:
        print(f'cannot tier: {args.tier!r} — 허용값: {', '.join(sorted(ticket_rules.TICKET_TIERS))}', file=sys.stderr)
        return 1
    with fs.board_lock(home):
        rc, path = round_maintenance._active_ticket_path(home, args.id, 'tier')
        if path is None:
            return rc
        fm, body = ticket_store.load_ticket(path)
        fm['tier'] = args.tier
        ticket_store.dump_ticket_atomic(path, fm, body)
    ready = round_maintenance._rounds_mutation_sync_paths(home, f'tier {args.id} {args.tier}', (path,))
    print(f'tier set {args.id}: {args.tier}{board_git._board_git_mutation_state_suffix(ready)}')
    return 0

def cmd_design(home, args: argparse.Namespace) -> int:
    """발행 후 frontmatter `design`을 기록/갱신하는 1급 수단(`cmd_tier` 동형).

    `--design`은 `new` 발행 시점 1회뿐이라 이후 상태 갱신은 YAML 손편집이 유일한 경로였다.
    값 검증은 `_validate_design` 재사용 — 인식 불가 값(폐지된 면제 표기 포함)은 파일을 읽기
    전에 거부한다(rc=1). 대상은 draft/open/claimed를 허용한다(`_active_ticket_path`의
    design 축) — `design: required`가 promote를 막으므로 draft에서 상태를 올릴 수 없으면
    setter가 있어도 실제 막힌 상황을 풀지 못한다. 값은 정규화·소문자화 없이 원문 그대로
    기록한다(`_design_state`가 이미 대소문자 무시로 판정한다).
    """
    reason = design_rules._validate_design(args.value)
    if reason:
        print(reason, file=sys.stderr)
        return 1
    with fs.board_lock(home):
        rc, path = round_maintenance._active_ticket_path(home, args.id, 'design')
        if path is None:
            return rc
        fm, body = ticket_store.load_ticket(path)
        fm['design'] = args.value
        ticket_store.dump_ticket_atomic(path, fm, body)
        is_draft = path.resolve().parent == pm_paths.drafts_dir(home).resolve()
    if is_draft:
        print(f'design set {args.id}: {args.value} (local draft; promote가 출하 소유)')
        return 0
    ready = round_maintenance._rounds_mutation_sync_paths(home, f'design {args.id} {args.value}', (path,))
    print(f'design set {args.id}: {args.value}{board_git._board_git_mutation_state_suffix(ready)}')
    return 0

def cmd_tier_signals(home, args: argparse.Namespace) -> int:
    """touches 기반 h1·h2·문서-전용 신호를 advisory로 표시한다."""
    found = ticket_store.find_ticket_exact(home, args.id)
    if found is None:
        print(f'ticket not found: {args.id}', file=sys.stderr)
        return 1
    _status, path = found
    fm, _body = ticket_store.load_ticket(path)
    touches = fm.get('touches')
    if not isinstance(touches, list):
        touches = []
    clean_touches = [item.strip() for item in touches if isinstance(item, str) and item.strip()]
    signals = tier_signals.tier_signals(clean_touches, home.root / '.project_manager' / 'tools')
    modules = ', '.join(signals.tool_modules) or '(none)'
    shared = ', '.join(signals.shared_code) or '(none)'
    module_count = 'unknown' if signals.unresolved_directories else str(len(signals.tool_modules))
    unresolved = f' · unresolved={', '.join(signals.unresolved_directories)}' if signals.unresolved_directories else ''
    print(f'tier signals {args.id} (advisory; PM confirms tier)')
    print(f'  h1 tool modules: {module_count} (2+={('yes' if signals.h1 else 'no')}) — {modules}{unresolved}')
    print(f'  h2 shared code: {('yes' if signals.shared_code else 'no')} — {shared}')
    print(f'  docs-only: {('yes' if signals.docs_only else 'no')}')
    return 0
