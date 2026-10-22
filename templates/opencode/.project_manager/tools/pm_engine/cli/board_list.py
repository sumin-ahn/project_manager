"""보드 freshness와 티켓 원격 상태 표시 (층: cli)."""
from __future__ import annotations

from pm_engine.infra import board_git
import sys
from pm_engine.domain import ticket_rules


def _print_board_freshness(home) -> bool:
    """board freshness 1줄을 **stderr** 로 표면화하고 스냅샷 검증 여부를 돌려준다 (advisory).

    stdout 이 아니라 stderr 인 이유: `board list` stdout 를 파싱하는 소비처(회귀 파서·
    pm_bootstrap counts·아래 anti-degrade warn 과 동형)를 오염시키지 않는다. board 비-git이면 line=None → 무출력(조용히 생략·오탐 0).

    반환값은 **이 줄이 근거로 쓴 원격-추적 스냅샷이 실측 검증됐나**(TTL 이내 재사용 또는 이번
    fetch 성공)다 — 같은 흐름에서 이어지는 티켓 단위 대조가 검증되지 않은 캐시로 단정하지
    않게 한다. `list` 는 이 값을 쓰지 않는다(현행 동작 무변경)."""
    freshness = board_git._board_git_freshness(home)
    if freshness.line is not None:
        print(freshness.line, file=sys.stderr)
    return freshness.verified


def _print_ticket_remote_mismatch(home, ticket_id: str, local_status: str, *,
                                  snapshot_verified: bool) -> None:
    """이 티켓의 로컬 status ↔ 원격-추적 status 불일치를 1줄(stderr) 표면화한다.

    `show` 전용 추가 표면 — 전역 요약(`_print_board_freshness`)만으로는 "이 티켓" 이 stale
    한지 알 수 없다(다중 clone 에서 local open 인데 원격은 이미 done 인 사례가 실사고).
    판정은 claim 이 이미 쓰는 `_board_git_remote_ticket_status`(ls-tree·네트워크 0) 를
    그대로 재사용한다(새 판정 금지) — **fetch 는 새로 하지 않는다**: 직전
    `_print_board_freshness()` 호출이 TTL 가드(`_board_fetch_head_fresh`)를 통과했을 때만
    이미 advisory fetch 를 했으므로, 여기선 그 결과로 갱신된 원격-추적 ref 를 읽기만 한다.

    `snapshot_verified` 가 False 면 **판정 자체를 내지 않는다**(문구만 약하게 하지 않는다):
    원격-추적 ref 는 남아 있어도 이번 조회에서 검증되지 않은 캐시라, 그것으로 "로컬 사본
    stale" 을 말하면 판정 불능을 판정으로 위장하게 된다. 그 형상의 강등 진술은 전역
    freshness 줄("판정불가 — 스냅샷일 수 있음")이 이미 낸다 — 같은 사실을 두 줄로 말하지
    않는다.

    무출력(오탐 0) 조건: 스냅샷 미검증(fetch 실패·TTL 밖) · board 비-git · upstream 미설정 ·
    원격 판정 불가(`None`·원격에 그 티켓 없음 포함) · local_status 가 STATUS_DIRS 밖(draft 는
    board-git 추적 대상이 아님) · 로컬==원격. 이 경우들은 "판정불가/무관"일 뿐 "최신" 을
    주장하지 않으므로 오탐이 아니다.
    """
    if not snapshot_verified:
        return
    if local_status not in ticket_rules.STATUS_DIRS:
        return
    if not board_git._board_git_enabled(home):
        return
    upstream = board_git._board_git_upstream(home)
    if upstream is None:
        return
    remote_status = board_git._board_git_remote_ticket_status(home, upstream.tracking, ticket_id)
    if remote_status is None or remote_status == local_status:
        return
    behind = board_git._board_git_behind(home, upstream.tracking)
    # behind 계산 불가면 수치를 지어내지 않는다(F-003) — 불일치 사실 자체는 검증된
    # 스냅샷에서 읽었으므로 그대로 말한다.
    gap = f"behind {behind}" if behind is not None else "behind 판정불가"
    print(
        f"⚠ {ticket_id} 로컬 사본 stale — 로컬 status={local_status} · "
        f"원격-추적 status={remote_status}({gap}). "
        f"`board.py list` 로 board-git freshness 를 확인하거나 board pull 후 재조회하라.",
        file=sys.stderr,
    )

from pm_engine.app import cluster_binding
from pm_engine.app import round_seed
from pm_engine.app import ticket_access
from pm_engine.domain import review_state
from pm_engine.domain import round_rules
from pm_engine.infra import fs
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths
from pm_engine.infra import ticket_store
import argparse


def cmd_list(home, args: argparse.Namespace) -> int:
    # `--mine` / `--repo`·`--slot`
    # 뷰: 단일 공유 보드의 렌즈 — **현재 사용자**의 area open + claim.
    # identity 입력을 한 번 해소해 행마다 재계산 안 함. **무인자 list 는 세션 기본 뷰**
    # (내 세션 스트림만 = 생성 open + 내 claim·타 세션분 완전 비노출·아래 default_view)이고,
    # 필터 없는 전체(모든 세션·타 사용자)는 `--all` 이다(status 셀렉터는 어느 뷰에도 적용).
    #
    # user-first: 필터 뷰의 "me" 는 **항상 현재 사용자**(`user_name()` =
    # local.conf identity.user= > git config user.email)다. 렌즈의 my_user 를
    # area_owner-derived(`_area_owner_from_session`/`_area_owner_for_single_area`)로
    # area_owner 미설정(흔함)이면 my_user=None 이 돼 slot-only 매칭·
    # all-open degrade 로 타 슬롯 claim·타 사용자 티켓이 유출되던 근본을 없앤다. area_owner 는
    # 이제 open-티켓 *소유* 정의(`_ticket_owner`)로만 남고, "누가 조회하는가" 와 무관하다.
    #
    # 뷰 스코프:
    #   - `--mine` = 내 것(내 claim ∪ 내 open), **전 task**(slot_scoped=False).
    #   - `--task <task>` = 내 것 **∩ 그 task**(slot_scoped=True·
    #     완전 일치) — 멤버십 = claimed_by 바인딩이 그 task(`<user>/<task>`)인
    #     claim + 내 소유 open backlog. `_slot_matches` exact 가 claimed_by 의 `/` 뒤 토큰을 task 와
    #     대조한다. `_ticket_is_mine` 단일 predicate 를 그대로 상속(point-patch 금지).
    #   `--mine`·`--task` 는 상호 배타(뷰 스코프는 하나만) — 타 사용자는 어느
    #   필터 뷰에도 안 나온다(`list --all` 전용). 슬롯·repo 렌즈는 없다.
    explicit_mine = bool(getattr(args, "mine", False))
    task_lens = getattr(args, "task", None)
    if task_lens is not None:
        # read 경로도 깔때기 소비 — 미검증이면 `--task alpha_1`(폴더
        # 이름 패턴) 입력이 옛 슬롯 claim 을 task 처럼 매칭한다. 부작용 0 조회지만 소비 지점
        # 폐쇄(전 surface 단일 규칙)에 합류한다.
        ticket_access._validate_actor_task_or_exit(home, task_lens)
    if task_lens is not None and explicit_mine:
        sys.exit("[중단] --task 는 --mine 과 함께 쓸 수 없다 — 뷰 스코프는 하나만 지정하라.")
    mine = explicit_mine or task_lens is not None
    # `--all`= 기존 무인자 전체 뷰(모든 세션·타 사용자 포함)의 이관. 무인자
    # 기본은 이제 **세션 기본 뷰**(default_view — 내 세션 스트림만·타 세션분 완전 비노출)다. `--all` 은
    # 렌즈(`--mine`/`--task`)와 상호 배타(뷰 스코프는 하나만). default_view 는
    # 어떤 렌즈도 `--all` 도 없을 때(순수 무인자) 발동한다.
    all_flag = bool(getattr(args, "all", False))
    if all_flag and mine:
        sys.exit("[중단] --all 은 --mine/--task 와 함께 쓸 수 없다 — 뷰 스코프는 하나만 지정하라.")
    default_view = not all_flag and not mine
    # 세션 뷰 = **무인자 기본 뷰**. "그 세션 스트림"(생성 open + 그 세션 claim)만 출력한다
    # (`_in_default_view`). `--mine`(user-wide)·`--task`(task 렌즈)는 세션 뷰가 아니라 기존
    # `_ticket_is_mine` 렌즈(의미론 불변).
    session_view = default_view
    # task 렌즈는 claim 을 그 task 로 교집합한다(--mine 은 전 task).
    slot_scoped = task_lens is not None
    if task_lens is not None:
        my_user = lease_store.user_name(home)
        my_slot = task_lens
    else:
        # 무렌즈 경로 — `--mine`(user-wide) 또는 기본 뷰(default_view·세션 스코프). 둘 다
        # 현재 사용자/세션을 해소한다(전체 뷰 `--all` 만 정체성 무해소·additive). 기본 뷰의 my_slot
        # (=현 세션)은 내 세션 claim 매칭과 생성-세션 스트림 판별(created_by 세션 일치)의 입력이다.
        resolve_identity = mine or default_view
        my_user = lease_store.user_name(home) if resolve_identity else None
        my_slot = lease_store.session_name(home) if resolve_identity else ""
        if mine and my_slot is None:
            # 세션 미바인딩(surface) — claim 필터를 못 좁힌다.
            # 안내는 stderr 로 내 stdout 티켓 목록 포맷을 오염시키지 않는다(소유 open 은 계속 표시).
            print("(비바인딩 — 세션 미해소·claim 필터 비활성; `--task <task>` 로 지정)",
                  file=sys.stderr)
    # graceful degrade: (a) 풀(내 소유 open) 필터는 보드에 area_owner 가 *운영
    # 중일 때만* 그 파티션을 우선 소유로 쓴다. areas.md 에 area_owner 가 하나도 안 채워졌으면
    # (미마이그레이션 채택자) area_owner_in_use=False → 소유는 다음 순위인 created_by.user로
    # 해소한다(`_ticket_owner`).
    #
    # 다중사용자 판정(`multi_user`·단일 사용자 정의 완결): **티켓 user 토큰이든 area_owner 든
    # 둘 중 하나라도 distinct ≥2 면 multi-user**. `_distinct_ticket_users`(티켓 귀속만 셈) 단독이면
    # 다중-owner 보드라도 claim 이 전부 legacy 슬롯-only(user 토큰 0)일 때 ≤1 로 떨어져 단일 사용자로
    # 오판 → legacy 슬롯-only 포함 경로가 발동해 (당시) bare `--slot N`(repo 불문 cross-repo
    # suffix 매칭은 제거됨)이 타 area 의 legacy `<repo>_N` 을 끌어오는 누출이 났다.
    # `_distinct_area_owners`(areas 소유 다중성)를 OR 로 더해 그 클래스를 닫는다.
    # 단일 사용자(둘 다 ≤1)면 미해소 open all-open degrade + legacy 슬롯-only 포함 보존, 다중이면
    # strict-exclude한다.
    area_owner_in_use = (mine or default_view) and ticket_access._area_owner_in_use(home)
    multi_user = (mine or default_view) and (
        ticket_access._distinct_ticket_users(home) > 1 or ticket_access._distinct_area_owners(home) > 1)
    # status 뷰 셀렉터: 기본(무-status)=활성만(done 접기) · `--status all`=전체(done 포함)
    # · `--status <특정>`=그 status 만(기존 동작 무변경).
    status_arg = getattr(args, "status", None)
    if status_arg == "all":
        allowed_statuses = ticket_rules.STATUS_DIRS
    elif status_arg:
        allowed_statuses = (status_arg,)
    else:
        allowed_statuses = cluster_binding._LIST_ACTIVE_STATUSES
    rows: list[tuple[str, dict]] = []
    # 세션격리 strict-exclude 신호: 다중사용자 판정 때문에 귀속 미해소/불일치 티켓을 이 뷰에서
    # 조용히 드롭했는지 잡는다. 단일 사용자면 재평가 자체를 생략해 오버헤드와 오탐을 피한다.
    strict_exclude_fired = False
    for status in ticket_rules.STATUS_DIRS:
        if status not in allowed_statuses:
            continue
        for p in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                continue
            fm, _ = loaded
            if args.tag and args.tag not in ticket_rules._tag_values(fm):
                continue
            if session_view:
                # 세션 뷰(무인자 기본) — 그 세션 스트림(생성 open +
                # 그 세션 claim)만. 타 세션분은 카운트 줄 포함 완전 비노출(전체는 `--all`).
                in_view = ticket_access._in_default_view(
                    home, status, fm, my_user, my_slot, area_owner_in_use, multi_user)
                if in_view:
                    rows.append((status, fm))
                elif multi_user and not strict_exclude_fired:
                    # 같은 predicate 를 단일 사용자로 재평가해 다중사용자 strict-exclude 때문에 숨긴
                    # 티켓인지 판정한다. 해소된 사용자 값이 과거 스탬프와 어긋난 경우도 같은
                    # 세션 후보인지 확인하도록 조회 user만 미해소 상태로 완화한다. 세션 축은
                    # 그대로이므로 타 세션의 정상 제외는 경고 신호가 되지 않는다.
                    strict_exclude_fired = (
                        ticket_access._in_default_view(
                            home, status, fm, my_user, my_slot, area_owner_in_use, False)
                        or ticket_access._in_default_view(
                            home, status, fm, None, my_slot, area_owner_in_use, False)
                    )
                continue
            if mine and not ticket_access._ticket_is_mine(home, status, fm, my_user, my_slot,
                                            area_owner_in_use, multi_user,
                                            slot_scoped=slot_scoped):
                # 이 제외가 *strict-exclude* 였는지 판정: 같은 predicate 를 multi_user=False 로
                # 재평가해 단일 사용자(all-open degrade)라면 포함됐을 open 이면 = 다중사용자라서 드롭한
                # 것(`_ticket_is_mine` 미해소 분기). 판정을 복제하지 않고 단일 predicate 를 재사용
                # 해 실 드롭만 신호로 잡는다. 이미 발동했으면 재평가를 생략한다. 소유 해소된 타
                # 사용자 티켓 제외는 단일 사용자에서도 제외라 무신호다.
                if multi_user and not strict_exclude_fired and ticket_access._ticket_is_mine(
                        home, status, fm, my_user, my_slot,
                        area_owner_in_use, False,
                        slot_scoped=slot_scoped):
                    strict_exclude_fired = True
                continue
            rows.append((status, fm))
    # anti-degrade loud-warn: 다중사용자 격리가 조용히 티켓을 드롭했거나 정체성이 미해소면 목록
    # 출력 전에 stderr로 한 줄 경고한다. stdout 목록 포맷은 그대로이며 단일 사용자는 경고하지 않는다.
    if (mine or session_view) and multi_user and (
            strict_exclude_fired or my_user is None):
        print(
            "⚠ 세션격리(strict-exclude): 다중사용자 보드에서 귀속이 미해소되거나 현재 사용자와 "
            "어긋난 티켓을 이 뷰에서 제외했다. 단일 사용자인데 email(git config user.email)을 바꿨다면 "
            "옛 티켓 귀속(old→new)이 어긋나 2인으로 오판된 것 — created_by/claimed_by 를 backfill "
            "로 정합: `python3 .project_manager/tools/board.py migrate-identity --task <task> --dry-run` "
            "(단일-세션 op·다른 세션 claim 중 실행 금지). 진짜 다중사용자면 정체성 설정 "
            "`board init --owner <you>`.",
            file=sys.stderr,
        )
    # board-git freshness 표면화 (advisory·stderr) — 각 list 변형 공통·양 return 경로
    # ("(no tickets)"·행 있음)를 커버하도록 여기서 한 번 소환. board 비-git 이면 무출력.
    _print_board_freshness(home)
    if not rows:
        print("(no tickets)")
        return 0
    for status, fm in rows:
        tags = ",".join(ticket_rules._tag_values(fm))
        claimed = fm.get("claimed_by") or ""
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:7s}] {fm['id']}  {title:60s}  {claimed:18s}  {tags}")
    return 0


def cmd_show(home, args: argparse.Namespace) -> int:
    """명세 전문 + 라운드 파일들을 순번 순으로 이어 보여준다.

    티켓 하나는 명세 파일 하나와 라운드 파일 N 개다 — 조회는 그 둘을 한 화면에 조립하는
    자리다. 라운드 렌더(구분선·산출 없음 표시)는 사이드카 seam 이 소유하고, 리뷰 finding
    요약은 versioned 블록에서만 파생한다(산문에 같은 항목을 다시 적지 않는다).
    """
    try:
        status, path = ticket_store.find_ticket(home, args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    # board-git freshness 표면화 (advisory·stderr) — list 와 같은 seam 소환 + 이
    # 티켓 단위 로컬↔원격 status 불일치 1줄.
    #
    # 대조할 원격이 아예 없는 형상(board 비-git · upstream 미설정)은 **둘 다 무출력**이다:
    # 신선도라는 개념 자체가 없는데 advisory 를 내면 조회 표면에 무의미한 경고가 상주한다
    # (upstream 판별은 네트워크 0 인 기존 `_board_git_upstream` seam 이 한다). `list` 는
    # 현행 동작 그대로 둔다(이 게이트는 show 경로에만 있다).
    if board_git._board_git_enabled(home) and board_git._board_git_upstream(home) is not None:
        snapshot_verified = _print_board_freshness(home)
        _print_ticket_remote_mismatch(home, args.id, status,
                                      snapshot_verified=snapshot_verified)
    print(f"-- {args.id} ({status}/) --\n")
    body = fs.read_text_shared(path, encoding="utf-8")
    print(body)
    try:
        rounds = round_seed.load_rounds(pm_paths.tickets_dir(home), args.id, ticket_text=body)
    except round_rules.RoundsError as exc:
        # 표시면은 조회를 막지 않는다 — 사유는 그 자리에 loud 하게 남기고 판정은 lint·
        # 완료 게이트(같은 seam)가 한다.
        print(f"⚠ 라운드 조회 실패: {exc}", file=sys.stderr)
        return 0
    if rounds:
        print(f"-- 라운드 {len(rounds)}건 --\n")
        print(round_rules.render_rounds_for_show(rounds))
        summary = review_state.render_pm_review_summary(body, rounds)
        if summary:
            print(summary)
        pending = [item for item in rounds if item.pending]
        if pending:
            print(f"-- 미회수 {len(pending)}건: "
                  + ", ".join(item.path.name for item in pending) + " --")
    return 0
