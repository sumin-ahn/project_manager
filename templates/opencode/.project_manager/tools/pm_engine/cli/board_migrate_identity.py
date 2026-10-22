"""티켓과 등록부 정체성 이관 명령 (층: cli)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.app import board_dashboard
from pm_engine.cli import task_args
from pm_engine.domain import areas_rules
from pm_engine.domain import identity_args
from pm_engine.domain import ticket_rules
from pm_engine.infra import areas_store
from pm_engine.infra import fs
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths
from pm_engine.infra import ticket_store
import argparse
import sys


def _migrate_areas_text(text: str, user: str) -> tuple[str, list[str]]:
    """areas.md 텍스트에서 빈 `area_owner` 셀을 user 로 채운 (새 텍스트, per-row 요약) 반환.

    표/주석/빈 줄은 verbatim 보존하고 *데이터 행의 area_owner 셀만* 채운다(비파괴·멱등).
    헤더(첫 table row)에서 `area_owner` 칼럼 인덱스를 찾는다 — 이미 채워진 행은 건드리지 않는다.

    **구-헤더 업그레이드**: areas.md 는 `area_owner` 칼럼
    자체가 없는 구 스키마다(`repo|prefix|git|test_cmd|owner`[5]·`…|base`[6]·`…|protected`[7] 또는
    멀티-clone `prefix|area|owner`[3] variant). 헤더에 area_owner 칼럼이 없으면 단순히 채울 자리가
    없어 no-op 이 되어버리면 migrate 의 본래 목적(구 데이터를 `--mine` 가능하게)이 가장 구형
    스키마에서 작동하지 않는다. 그래서 area_owner 칼럼이 없는 구 헤더를 만나면 **canonical 8칼럼
    헤더(`_areas_header_line`·`_AREAS_COLUMNS` 단일 진실)로 업그레이드**한다 — 헤더 행 교체 +
    바로 뒤 구분선(`|---|`) 행을 canonical 폭으로 교체 + 각 데이터 행에 area_owner 칼럼(빈값 →
    user) 추가. 기존 칼럼·값·정렬·표 밖 텍스트(주석)는 보존하고 area_owner 만 *append* 한다
    (비파괴). 멱등: 이미 area_owner 칼럼이 있으면 헤더 업그레이드 없이 빈 셀만 채운다.

    셀 수가 헤더보다 많은 wider row(비-canonical 구 헤더 아래에 canonical 8칼럼 row 가 append 된
    경우 포함)는 `upgrade` 여부와 무관하게 `_AREAS_COLUMNS` 의 area_owner 인덱스(7)로 area_owner
    셀을 찾는다 — `_parse_areas` 가 wider row 를 헤더 무시하고 canonical 순서로 매핑하는 것과
    정확히 동형(must-fix). 그렇지 않으면 헤더 폭으로 읽다 index 3(`test_cmd`)을 area_owner 로
    오인해 backfill 을 놓친다.

    인덱스 해소·구-헤더 업그레이드 계획·wider-row 보정은 **공용 헬퍼**(`_areas_column_index`·
    `_areas_row_cell_index`)에 있고 `areas_set_cell`(범용 셀 변경)과 공유한다 — 두 벌로 갈라지면
    칼럼 오매핑.
    """
    lines = text.splitlines(keepends=True)
    header_cells: list[str] | None = None
    ao_idx: int | None = None
    # 구-헤더 업그레이드 모드: 헤더에 area_owner 칼럼이 없을 때 켜진다. 켜지면 헤더 행을
    # 갈아끼우고, 바로 뒤 구분선 1개를 같은 폭으로 교체하고, 데이터 행마다 area_owner 칼럼을
    # append 한다. ao_idx 는 새 area_owner 칼럼 위치로 고정된다.
    upgrade = False
    sep_cols = 0     # 업그레이드 후 구분선이 가질 칼럼 수(canonical 8 또는 구 헤더+1).
    sep_replaced = False  # 헤더 직후 구분선 1회만 교체(이후 구분선은 verbatim).
    canonical_ao = areas_rules._AREAS_COLUMNS.index("area_owner")
    changes: list[str] = []
    out: list[str] = []
    for line in lines:
        # 줄바꿈을 떼어 셀을 검사하고, 재조립 시 원래 종결자를 복원한다(비파괴).
        nl = ""
        body = line
        if line.endswith("\r\n"):
            nl, body = "\r\n", line[:-2]
        elif line.endswith("\n"):
            nl, body = "\n", line[:-1]
        cells = areas_rules._split_areas_row(body)
        if cells is None:
            # 비-table 줄(주석·빈 줄·구분선)은 기본 verbatim. 단 업그레이드 모드에서 헤더
            # 직후 첫 구분선(`|---|`)은 새 칼럼 수에 맞춰 교체한다(헤더 폭과 정합).
            if upgrade and not sep_replaced and areas_rules._AREAS_SEP_RE.match(body.strip()):
                out.append("|" + "|".join("---" for _ in range(sep_cols)) + "|" + nl)
                sep_replaced = True
            else:
                out.append(line)
            continue
        if header_cells is None:
            header_cells = [c.lower() for c in cells]
            # 공용 헬퍼 — 칼럼이 있으면 그 인덱스(헤더 verbatim), 없으면 구-헤더 업그레이드
            # 계획(canonical 8칼럼 교체 또는 기존 헤더 끝 append)을 함께 돌려준다.
            ao_idx, new_header, sep_cols = areas_store._areas_column_index(header_cells, "area_owner")
            if new_header is None:
                out.append(line)  # 이미 신 스키마 헤더 — verbatim.
            else:
                upgrade = True
                out.append(new_header + nl)
            continue
        # 헤더보다 넓은 row 는 `upgrade` 여부와 무관하게 canonical area_owner 인덱스(7)로
        # 매핑한다(`_areas_row_cell_index` 공용 보정) — `_parse_areas` 가 wider row 를 헤더
        # 무시하고 `_AREAS_COLUMNS` 순서로 매핑하는 것과 정확히 동형이다. 비-canonical 구
        # 헤더(예 멀티-clone `prefix|area|owner`[3]) 아래에 canonical 8칼럼 row 가 append 된
        # 케이스에서 `ao_idx`(=헤더 폭) 로 읽으면 index 3(`test_cmd`)을 area_owner 로 오인해
        # backfill 못 한다(must-fix·_parse_areas 정합).
        idx = areas_store._areas_row_cell_index(
            header_cells, cells,
            ao_idx if ao_idx is not None else canonical_ao, "area_owner")
        prefix = cells[1] if len(cells) > 1 else "?"
        cur = cells[idx] if idx < len(cells) else ""
        if cur.strip():
            out.append(line)  # 이미 채워짐 — 멱등(보존).
            continue
        # 빈 셀 채움. 셀이 모자라면 빈 칸으로 패딩해 인덱스를 확보(비파괴 append).
        while len(cells) <= idx:
            cells.append("")
        cells[idx] = user
        out.append("| " + " | ".join(cells) + " |" + nl)
        changes.append(f"area {prefix}: area_owner → {user}")
    return "".join(out), changes


def _migrate_ticket_fm(fm: dict, user: str, task: str, status: str,
                       registered: set[str]) -> tuple[list[str], list[str]]:
    """티켓 frontmatter 를 *제자리* backfill 하고 `(변경 요약, 바꾸지 않은 타 사용자 토큰)` 을 반환.

    멱등·비파괴: 부재/빈 `created_by` 만 user 로, 슬롯-only(`/` 없음·non-empty) `claimed_by`
    만 `<user>/<토큰>` 으로 채운다. 기존 non-empty 값(이미 `<user>/<토큰>` 형태 포함)은 불변.
    키 순서는 dict 제자리 수정이라 보존(없던 created_by 추가는 끝에 붙음 → dump 순서 유지).

    **슬롯 세션 토큰 이전**: 비종결(open·claimed·blocked)과 draft 의 `created_by`·`claimed_by` 중
    마지막 `/` 뒤 토큰이 등록 repo 의 폴더 이름 패턴(`<repo>_<N>`)인 값은 사용자 부분이 실행
    사용자이거나 없으면 `<user>/<task>` 로 옮긴다. 다른 사용자의 토큰은 바이트 그대로 두고 두 번째
    목록으로 돌려준다(그 사용자가 자기 task 로 실행한다). done·discarded 는 옮기지 않는다.
    """
    changes: list[str] = []
    foreign: list[str] = []
    created_by = fm.get("created_by")
    # 부재(키 없음·None)거나 빈/공백 문자열이면 backfill. 기존 non-empty 값은 불변(멱등).
    if not (str(created_by).strip() if created_by is not None else ""):
        fm["created_by"] = user
        changes.append(f"created_by → {user}")
    if status not in ticket_rules.TERMINAL_STATUS_DIRS:
        for field in ("created_by", "claimed_by"):
            value = fm.get(field)
            if not (isinstance(value, str) and value.strip()):
                continue
            owner_user, _sep, token = value.strip().rpartition("/")
            if not identity_args.is_reserved_task_name(token, registered):
                continue
            if owner_user and owner_user != user:
                foreign.append(f"{field}={value}")
                continue
            fm[field] = f"{user}/{task}"
            changes.append(f"{field} {value} → {fm[field]}")
    cb = fm.get("claimed_by")
    if isinstance(cb, str) and cb.strip() and "/" not in cb:
        # 슬롯-only(구 형태·user 차원 없음) → user 차원 prepend(슬롯값 보존).
        fm["claimed_by"] = f"{user}/{cb}"
        changes.append(f"claimed_by {cb} → {user}/{cb}")
    return changes, foreign


def _print_migrate_foreign(tid: str, status: str, foreign: list[str]) -> None:
    """바꾸지 않은 타 사용자 슬롯 토큰을 stderr 로 알린다(rc 불변)."""
    for item in foreign:
        print(f"  {tid} ({status}/): {item} — 다른 사용자의 옛 슬롯 표기라 바꾸지 않는다"
              "(그 사용자가 `board.py migrate-identity --task <task>` 로 옮긴다)", file=sys.stderr)


def _migrate_scan_dirs(home, statuses: tuple[str, ...]) -> list[tuple[str, Path]]:
    """migrate 스캔 디렉토리 — 요청 status 디렉토리 + draft 격리 디렉토리(pseudo-status `draft`)."""
    return [(status, pm_paths.tickets_dir(home) / status) for status in statuses] + [("draft", pm_paths.drafts_dir(home))]


def _migrate_identity_preview(home,
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """--dry-run 경로: read-only 스캔 + per-file 미리보기. 락·쓰기 0.

    어떤 파일도 옮기거나 쓰지 않으므로 board_lock 을 *전혀* 잡지 않는다(read-only 보장).
    반환 `(total, wrote)` 에서 wrote 는 항상 False(쓰기 없음 → refresh_board 미호출).
    """
    total = 0
    # areas.md 미리보기(읽기 전용).
    af = pm_paths.areas_file(home)
    if af.exists():
        text = fs.read_text_shared(af, encoding="utf-8")
        _, area_changes = _migrate_areas_text(text, user)
        for c in area_changes:
            print(f"  areas.md: {c}")
        total += len(area_changes)
    # 티켓 미리보기 — glob 스캔 후 변경 산출만(쓰기 없음).
    registered = areas_store.registered_repos(home)
    for status, directory in _migrate_scan_dirs(home, statuses):
        for p in sorted(directory.glob("T-*.md")):
            fm, _body = ticket_store.load_ticket(p)
            changes, foreign = _migrate_ticket_fm(fm, user, slot, status, registered)
            tid = fm.get("id") or p.stem
            _print_migrate_foreign(tid, status, foreign)
            if not changes:
                continue
            total += len(changes)
            for c in changes:
                print(f"  {tid} ({status}/): {c}")
    return total, False


def _migrate_areas_apply(home, user: str) -> tuple[int, bool]:
    """areas.md 의 빈 area_owner backfill (read→transform→write)을 board_lock 으로 보호.

    areas.md 는 `areas_append`가 *진짜* 공유 mutation 으로
    board_lock 을 잡고 쓰는 단일 파일이라, 본 RMW 의 write 도 같은 락으로 직렬화해야
    동시 repo-add 의 lost-update(전체 write_text 가 append 된 row 를 클로버)를 막는다.
    이 락은 areas 구간 *한정* — 티켓 backfill은 뒤의 독립 board_lock 구간에서 수행한다.

    **재진입 금지**: board_lock 은 OS flock(non-reentrant). 락 안에서 부르는 IO
    (`_migrate_areas_text`·AREAS_FILE read/write)는 락을 다시 잡지 않는다. 반환 `(total, wrote)`.
    """
    af = pm_paths.areas_file(home)
    if not af.exists():
        return 0, False
    total = 0
    wrote = False
    with fs.board_lock(home):
        text = fs.read_text_shared(af, encoding="utf-8")
        new_text, area_changes = _migrate_areas_text(text, user)
        for c in area_changes:
            print(f"  areas.md: {c}")
        if area_changes:
            total += len(area_changes)
            if new_text != text:
                af.write_text(new_text, encoding="utf-8", newline="\n")
                wrote = True
    return total, wrote


def _migrate_tickets_apply(home,
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """티켓 backfill — scan/read/transform/relookup/atomic dump 전체를 board_lock으로 직렬화.

    migrate는 status 이동 없이 frontmatter만 고치는 in-place ticket writer다. tier writer가 값을
    기록한 뒤 migration이 lock 밖의 옛 ``fm/body`` snapshot을 dump하면 그 기록이 유실된다.
    따라서 lifecycle/tier writer와 같은 board_lock을 후보 scan부터 마지막 dump까지 유지한다.

    lock 안 재조회는 방어적으로 유지한다. 엔진 writer는 모두 같은 lock을 따르므로 정상 경합에서는
    경로가 바뀌지 않지만, 수동/구버전 writer가 경로를 옮긴 경우 stale write 대신 skip+loud한다.
    dump는 temp+``fs.atomic_replace`` 원자 교체다. refresh는 호출자 ``cmd_migrate_identity``가 이 lock을
    놓은 뒤 1회 수행한다(재진입 없음). board-git sync/다른 lock 호출은 이 함수에 없다.
    반환 ``(total, wrote)``.
    """
    total = 0
    wrote = False
    registered = areas_store.registered_repos(home)
    with fs.board_lock(home):
        for status, directory in _migrate_scan_dirs(home, statuses):
            for p in sorted(directory.glob("T-*.md")):
                try:
                    fm, body = ticket_store.load_ticket(p)
                except FileNotFoundError:
                    print(f"  skip {p.name}: 스캔 후 사라짐(수동/구버전 writer 이동) — "
                          "backfill 안 함", file=sys.stderr)
                    continue
                changes, foreign = _migrate_ticket_fm(fm, user, slot, status, registered)
                tid = fm.get("id") or p.stem
                _print_migrate_foreign(tid, status, foreign)
                if not changes:
                    continue
                try:
                    cur_status, cur_path = ticket_store.find_ticket_for_mutation(home, tid)
                except FileNotFoundError:
                    print(f"  skip {tid}: 재조회 시 없음(수동/구버전 writer 완료/삭제) — "
                          "backfill 안 함", file=sys.stderr)
                    continue
                if cur_path != p:
                    print(f"  skip {tid}: {status}/ → {cur_status}/ 이동됨(쓰기 직전) — "
                          f"stale 쓰기 안 함", file=sys.stderr)
                    continue
                total += len(changes)
                for c in changes:
                    print(f"  {tid} ({status}/): {c}")
                ticket_store.dump_ticket_atomic(cur_path, fm, body)
                wrote = True
    return total, wrote


def _migrate_identity_apply(home,
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """비-dry-run 경로: areas + 티켓 backfill을 서로 독립된 board_lock 구간으로 수행.

    - **areas.md**: `_migrate_areas_apply` 가 board_lock 으로 RMW 를 보호한다(`areas_append`
      와의 lost-update 방지·진짜 공유 mutation).
    - **티켓**: `_migrate_tickets_apply`가 scan/read/transform/relookup/dump 전체를 같은 board_lock으로
      보호해 lifecycle/tier writer와 stale overwrite 없이 직렬화한다.

    **재진입 금지**: areas와 ticket helper가 각각 락을 획득하고 반환한 뒤 다음 helper로 간다.
    board.md 재생성(`refresh_board` — 자체 board_lock)은 **호출자(`cmd_migrate_identity`)가
    락 밖에서 1회** 한다(데드락 방지). 반환 `(total, wrote)`.
    """
    area_total, area_wrote = _migrate_areas_apply(home, user)
    ticket_total, ticket_wrote = _migrate_tickets_apply(home, user, slot, statuses)
    return area_total + ticket_total, area_wrote or ticket_wrote


def cmd_migrate_identity(home, args: argparse.Namespace) -> int:
    """backfill — areas area_owner·ticket created_by/claimed_by.

    `--user` override > `user_name()`(local.conf identity.user= / git config user.email). 미해소(None)면
    abort(식별자 없이는 backfill 불가). `--dry-run` 은 쓰기 0·per-file 미리보기. `--scope`
    active(open+claimed) | all(기본·done 포함). 멱등(빈 필드만)·비파괴(순서/body/표 보존).

    `--task` 는 필수다(귀속 쓰기). 비종결·draft 티켓의 옛 슬롯 세션 토큰(`<repo>_<N>`)은 실행
    사용자 몫(사용자 부분이 같거나 없음)이면 `<user>/<task>` 로 옮기고, 다른 사용자 몫은 그대로
    두고 stderr 로 알린다(rc 0). 그 밖의 슬롯-only `claimed_by`(`pm-1` 같은 `/` 없는 값)는 user
    차원만 prepend 하고 *기존 토큰을 보존*한다(`pm-1` → `<user>/pm-1`). done·discarded 는 사용자
    backfill 외에 바꾸지 않는다.

    **단일-세션 업그레이드 op (동시성 모델)**: 일회성 전체 backfill이라는 운영 성격은 유지한다.
    다만 areas와 티켓 모두 board_lock writer이며, 티켓 scan/read/transform/dump는 lifecycle/tier와
    하나의 순서로 관측된다. 두 helper의 lock 구간은 중첩되지 않고 board.md 재생성은 모두 끝난 뒤
    lock 밖에서 1회 한다.
    """
    user = lease_store.user_name(home, getattr(args, "user", None))
    if not user:
        print("[중단] user 식별자 미해소 — `--user <id>` 를 주거나 local.conf identity.user= / "
              "git config user.email 를 설정하라(식별자 없이는 backfill 불가).",
              file=sys.stderr)
        return 1
    # 이전 대상 task = 귀속 쓰기(claimed_by·created_by 에 박음) — `--task` 가 없으면 어떤 쓰기보다
    # 먼저 멈춘다(환경변수·단일 대여 값으로 대신하지 않는다).
    slot = task_args._actor_session_override(home, args)
    if slot is None:
        sys.exit(task_args.TASK_REQUIRED.format(action="migrate-identity",
                                      command="board.py migrate-identity"))
    dry_run = bool(getattr(args, "dry_run", False))
    scope = getattr(args, "scope", "all") or "all"
    statuses = ("open", "claimed") if scope == "active" else ticket_rules.STATUS_DIRS

    tag = "[dry-run] " if dry_run else ""
    # scope 문구 정합: migrate 는 소유 무관 *전 티켓*을 스캔·backfill 한다
    # (all=done 포함 전체·active=open+claimed, 둘 다 draft 포함). `list --mine`/`--task`(=현재-사용자
    # 뷰)와 스캔 대상이 다르므로 "전체 스캔" 을 명시해 두 기준이 어긋나 보이는 오독을 없앤다.
    scope_note = "전체 스캔·소유 무관" if scope == "all" else "open+claimed 스캔·소유 무관"
    print(f"{tag}migrate-identity — user={user} · task={slot} · scope={scope} ({scope_note})")

    if dry_run:
        total, wrote = _migrate_identity_preview(home, user, slot, statuses)
    else:
        total, wrote = _migrate_identity_apply(home, user, slot, statuses)

    if total == 0:
        print("  (변경 없음 — 이미 마이그레이션됨이거나 backfill 대상 없음)")
    else:
        verb = "변경 예정" if dry_run else "변경 완료"
        print(f"{tag}{total}건 {verb}.")
    if dry_run:
        print("[dry-run] 쓰기 0 — 적용하려면 --dry-run 없이 재실행.")
    # 파생 board.md 갱신("board.py 변경 명령마다 파생 보드 갱신" 보장·codex sug). migrate 가
    # claimed_by 를 바꾸면 board.md claimed 표시도 달라진다 — 실제 쓰기가 있었고 dry-run 이
    # 아닐 때만 1회 재생성한다(dry-run 은 파생물도 안 건드림·읽기-only 미리보기 보장).
    if wrote:
        board_dashboard.refresh_board(home)
    return 0
