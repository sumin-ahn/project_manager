"""티켓 소유 조회와 task 정체성 검증 (층: app)."""
from __future__ import annotations

from pm_engine.infra import areas_store
from pm_engine.domain import identity_args
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths
import sys
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store


def _ticket_area_owner(home, tid: str) -> str | None:
    """티켓의 area `area_owner`(user 소유) 해소 — 미상이면 None (`--mine` (a) 입력).

    매핑 경로: ID prefix(`_ticket_prefix`) → areas.md 의 그 prefix 행(`_areas_row_for_prefix`)에서
    `area_owner` 를 *직접* 읽는다(`_active_test_cmd`/line 737 의 prefix-행 직접-읽기와 동형).
    미등록 prefix·area_owner 빈값은 None(area 비소유 처리).

    prefix 행에서 직접 읽는 이유(repo 칼럼 경유 재스캔 금지): areas registry 는 prefix-unique 만
    보장하고 repo-unique 는 아니다 — 두 prefix 가 같은 `repo` 칼럼값을 공유하면 `repo` 로 재스캔할
    경우 *그 repo 의 첫 행* area_owner 를 돌려줘 잘못된 소유자가 나온다. prefix 로 이미 정확한 행을
    잡았으니 그 행에서 바로 읽는다(이중 스캔도 제거).

    **no-prefix(단일 self-host) 폴백 (sole-area)**: 단일 self-host(
    prefix-불요)는 티켓이 `T-NNNN`(prefix 없음)이라 `_ticket_prefix` None 이다. no-prefix 티켓 ⟹
    단일-repo(id_prefix None) ⟹ areas registry 의 *단일 area* 가 그 티켓의 area 다 — prefix
    매핑은 multi-repo 메커니즘이므로 단일-repo 엔 sole-area 폴백이 맞다. areas 에 area 가 **정확히 1개**면
    그 단일 area 의 area_owner 를 돌려준다(migration 이 area_owner 를 채운 단일-repo 보드에서 `--mine`
    (a) 가 no-prefix open 티켓을 잡게). area 가 여러 개면(multi-repo 인데 no-prefix 티켓 = 모순적/
    희귀) 모호하므로 None 유지(기존 동작). prefix 가 *있는* 티켓은 이 폴백을 안 타고 기존 prefix
    경로 그대로(multi-repo 정합·무회귀).
    """
    prefix = ticket_rules._ticket_prefix(tid)
    if not prefix:
        _header, rows = areas_store._parse_areas(home)
        if len(rows) == 1:
            return rows[0].get("area_owner") or None
        return None
    row = areas_store._areas_row_for_prefix(home, prefix)
    if not row:
        return None
    return row.get("area_owner") or None


def _area_owner_in_use(home) -> bool:
    """areas.md 에 non-empty `area_owner` 행이 **하나라도** 있는가.

    `--mine` (a) 풀(내 area 의 open)을 area_owner 로 좁히는 건 *소유권 데이터가 보드에 실제로
    구성돼 있을 때만* 의미가 있다. 이건 **전역**(per-user 아님) 1회 판정이다 — areas.md 전체를
    한 번 스캔해 `area_owner` 칼럼이 어디든 채워져 있으면 True. 채워져 있으면 area_owner 파티션이
    운영 중(마이그레이션됨·multi-user)이라 (a) 를 area_owner==me 로 좁히고, 비어 있으면(미마이그레이션
    채택자) (a) 를 전체 open 으로 degrade 한다(빈 보드 금지·plain list 처럼).

    이전 per-user `_owns_any_area(my_user)`(내 소유 area ≥1 인가)를 대체한다 — 데이터 정합은
    마이그레이션 도구(`board migrate-identity`)가 책임지고, 런타임 폴백은 **전역 플래그
    하나**로 최소화한다. area_owner 가 운영 중인데 *내* area 가 0개면
    (a) 는 자연히 빈다 — 그건 회귀가 아니라 '내 area 의 open 이 없음'이라는 올바른 결과다.

    areas.md 부재·모든 area_owner 빈 값이면 False. ≥1 채워짐이면 True.
    """
    _header, rows = areas_store._parse_areas(home)
    return any((row.get("area_owner") or "").strip() for row in rows)


def _distinct_area_owners(home) -> int:
    """areas.md 의 non-empty `area_owner` 칼럼에서 해소되는 *distinct user* 수.

    `_area_owner_in_use`(채워졌나 bool)와 달리 **다중성**을 센다 — `multi_user` 신호의 두 번째
    축이다. `_distinct_ticket_users`(티켓 귀속만 셈)는 다중-owner 보드라도 claim 이 전부 legacy
    슬롯-only(user 토큰 0)면 ≤1 로 떨어져 그 보드를 단일 사용자로 오판한다(→ legacy 슬롯-only 포함 경로가
    발동해 `--slot N` 이 타 area 의 legacy `<repo>_N` 을 suffix 매칭으로 끌어오는 누출).
    areas 에 area_owner 가 2명 이상이면 티켓 user 토큰이 비어도 multi-user 보드다 — 그
    다중성을 여기서 세어 `multi_user = distinct ticket-user >1 OR distinct area_owner >1` 로
    단일 사용자 정의를 완결한다. areas.md 부재/전부 빈 값이면 0(단일 사용자 신호 보존·회귀 0).
    """
    _header, rows = areas_store._parse_areas(home)
    return len({owner for row in rows if (owner := (row.get("area_owner") or "").strip())})


def _ticket_owner(home, fm: dict, area_owner_in_use: bool) -> str | None:
    """open 티켓의 소유 user — `area_owner`(1차) ?? `created_by.user`(2차·항상-존재).

    세션 뷰 (a) "내 소유 open" 판정의 소유자를 완전-데이터로 해소한다:
      - area_owner 파티션이 운영 중(`area_owner_in_use`)이면 그 티켓 area 의 `area_owner`
        (`_ticket_area_owner`)를 1차로 쓰고, 미운영/미해소(경계-교차·미등록 area·미마이그 채택자)면
      - `created_by` 의 user(`_created_by_user`·항상-존재 폴백)로 떨어진다.
    둘 다 미상이면 None — 호출부(`_ticket_is_mine`)가 단일 사용자=degrade / multi=strict 로 가른다.
    미해소 read 유출을 없앤다([[prefer-data-migration-over-fallback]]).
    """
    tid = fm.get("id") or ""
    area_owner = _ticket_area_owner(home, tid) if area_owner_in_use else None
    return area_owner or ticket_rules._created_by_user(fm.get("created_by"))


def _distinct_ticket_users(home) -> int:
    """보드 전체 티켓의 `created_by`/`claimed_by` 에서 해소되는 *distinct user* 수.

    **데이터-유도 다중사용자 신호**다(config 플래그 아님) — 세션 뷰 격리(`_ticket_is_mine`)가 소유
    미해소 open 을 단일 사용자(≤1)면 all-open degrade(회귀 0), 다중(≥2)이면 strict-exclude 로 가르는 게이트다.
    전 status 디렉토리를 1회 스캔해 `created_by`(→`_created_by_user`)·`claimed_by`(→`_claimed_by_user`)의
    user 토큰을 집합에 모아 크기를 센다 — 슬롯-only claimed_by·미상은 집합에 안 든다(graceful). 깨진
    티켓은 신호 산정에서 skip(fail-soft·크래시 0). areas 의 area_owner 가 아니라 *티켓* 귀속만 세는 건
    정의(소유 데이터가 티켓에 실려야 다중사용자로 본다)다.
    """
    users: set[str] = set()
    for status in ticket_rules.STATUS_DIRS:
        for p in (pm_paths.tickets_dir(home) / status).glob("T-*.md"):
            # 순회 소비자는 **공용 soft 로더**를 쓴다(사본 금지) — 자체 except 목록은 비-dict
            # frontmatter(스칼라·리스트: 파싱은 성공)를 걸러내지 못해 바로 아래 `fm.get` 이
            # AttributeError 로 죽었다. 기본 `list` 가 이 함수를 먼저 부르므로 보드 조회 전체가
            # 티켓 한 건 때문에 traceback 이 된다.
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                continue
            fm, _ = loaded
            cb_user = ticket_rules._created_by_user(fm.get("created_by"))
            if cb_user:
                users.add(cb_user)
            claim_user = ticket_rules._claimed_by_user(fm.get("claimed_by"))
            if claim_user:
                users.add(claim_user)
    return len(users)


def scan_task_tickets(home, user: str | None, task: str,
                      prefix: str | None = None) -> dict[str, list[dict]]:
    """task end 소진 게이트용 티켓 스캔 (자체 스캔·조회 전용·부작용 0).

    `board list --task` 렌즈(wave 3)가 아직 없으므로 `pm-config task end` 가 이걸로
    자체 스캔한다. 두 축을 한 번의 status 디렉토리 순회로 모은다:

      - **claimed** — **진행 중(open/claimed/blocked)** 티켓 중 `claimed_by` 의 slot 토큰(마지막 `/`
        뒤·`_slot_matches`)이 `task` 와 일치하는 것. `task` 세션의 claim 형태 = `<user>/<task>`
        (identity_tag slot 값 `<repo>_<N>` 예약과 기계 판별·task 명은 자유 포맷). `user` 가 주어지고
        claimed_by 에 user 토큰이 있으면 그 user 도 일치해야 한다(교차사용자 동명 task 오귀속 방지).
        slot-only claim(user 토큰 없음)은 graceful 포함. **terminal status(`done`)는 제외한다** —
        `cmd_complete` 는 done 이동 시 `claimed_by` 를 지우지 않으므로(status/completed_at 만),
        `<user>/<task>` 로 claim→complete 한 티켓이 done/ 에 claimed_by 를 남긴다. done 을 담으면 그
        티켓은 `unclaim`(status=="claimed" 요구)도 불가라 **해소 수단이 없어 task end 가 영구 차단**된다
        ("해소=complete 또는 unclaim"). 완료된 작업은 소진 게이트 대상이 아니다.
        처분 종결(`discarded`)도 같은 이유로 제외한다 — 종결 status 전수가 대상 밖이다. 이게
        비어야 task end 가 반납/이동으로 진행한다(거부 게이트).
      - **prefix_open** — `prefix` 가 주어지면 그 prefix(`_ticket_prefix`)의 `open` 티켓. **정보
        표시만**(차단 안 함·prefix≠경계) — task 지정 prefix 의 backlog 를 참고로 보여줄 뿐.

    각 row = {"id", "title", "status"}. 깨진 티켓은 skip(fail-soft). 실행 패키지의 소비자가 명시 Home을 전달한다.
    """
    claimed: list[dict] = []
    prefix_open: list[dict] = []
    for status in ticket_rules.STATUS_DIRS:
        for p in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            # 순회 소비자 공통 규약 — 손상/비-dict frontmatter 는 공용 soft 로더가 경고 1줄과
            # 함께 걸러낸다(`_distinct_ticket_users` 동형·자체 except 사본 금지).
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                continue
            fm, _ = loaded
            tid = fm.get("id") or p.stem
            row = {"id": tid, "title": fm.get("title") or "", "status": status}
            cb = fm.get("claimed_by") or ""
            # 종결 status(done·discarded)는 claimed 축에서 제외 — 종결 티켓은 claimed_by 가
            # 남아 있어도 끝난 작업이고, 담으면 해소 수단이 없어 task end 가 영구 차단된다.
            if status not in ticket_rules.TERMINAL_STATUS_DIRS and cb and ticket_rules._slot_matches(cb, task):
                cb_user = ticket_rules._claimed_by_user(cb)
                if user is None or cb_user is None or cb_user == user:
                    claimed.append(row)
            if prefix and status == "open" and ticket_rules._ticket_prefix(tid) == prefix:
                prefix_open.append(row)
    return {"claimed": claimed, "prefix_open": prefix_open}


def _ticket_is_mine(home, status: str, fm: dict, my_user: str | None,
                    my_slot: str, area_owner_in_use: bool, multi_user: bool,
                    *, slot_scoped: bool = False) -> bool:
    """이 티켓이 `--mine`/`--task` 뷰에 들어가는지 — (b) 내 claim ∨ (a) 내 소유 open.

    **단일 불변식**(전 surface 수렴·point-patch 금지): 필터 뷰 멤버십 = (내
    claim) ∪ (내 소유 open). 타 사용자의 claim·미claim open 은 **어떤 필터 뷰에도 안 나온다** —
    전체는 `list --all` 전용(기존 무필터 무인자 `list` 의 이관). querying identity(`my_user`)는 항상 **현재 사용자**(cmd_list 가
    `user_name()` 해소)다. degrade("전체 open=mine")는
    **단일 사용자(distinct user ≤1·`multi_user` False)에서만** 허용한다.

    (b) 내 claim — 상태 무관 연속성 (user-first). `claimed_by` 의 user 토큰 유무로 가른다:
      - **user-qualified**(`_claimed_by_user(cb)` non-None·`<user>/<session>`): 내 것 iff `cb_user ==
        my_user`. **task 뷰**(`--task`)면 `_slot_matches` AND 로 *내 것
        ∩ 그 task*(타 task 의 내 claim 은 task 뷰서 제외·`--mine`
        엔 나옴). **비제약 뷰**(`--mine`)면 user 만(전 task). 타 사용자 claim(user 불일치)·my_user
        미해소(귀속 불가)는 제외 — 남의 user-claim 을 slot 번호로 끌어오는 누출 0.
      - **legacy 슬롯-only**(`cb_user is None`·user 토큰 없음): **진짜 단일 사용자(distinct user ≤1·
        `not multi_user`)에서만** `_slot_matches`(내 슬롯)로 포함한다. 게이트가 `my_user is None` 이
        아니라 `not multi_user` 인 건 `user_name()` 이 git email 폴백으로 단일 사용자도 my_user 를
        해소할 수 있어(흔함) — my_user proxy 면 그 보드의 자기 슬롯 legacy claim 을 잘못 숨긴다.
        multi_user 면 legacy 는 ambiguous → strict-exclude(migrate-identity backfill).
    (a) 내 소유 open — status==open 한정. `owner = _ticket_owner(fm, area_owner_in_use)`
        (area_owner ?? created_by.user):
      - my_user·owner 둘 다 해소 → strict `owner == my_user`(유출 0·유일 포함 규칙).
      - 미해소(my_user None ∨ owner None) + `multi_user` → `return False`(strict-exclude).
      - 미해소 + 단일 사용자(¬multi_user) → `return True`(all-open degrade 보존·빈 보드 금지·회귀 0).
    open 은 미claim 이라 세션이 없다 — task 뷰에서도 (a) 는 task 무관 backlog(`--mine` 과
    동일 풀)로 두고 task 로 좁히지 않는다.
    """
    cb = fm.get("claimed_by") or ""
    # (b) 내 claim — user-first. user 토큰 유무로 가른다.
    if cb:
        cb_user = ticket_rules._claimed_by_user(cb)
        if cb_user is not None:
            # user-qualified claim(`<user>/<slot>`) — 내 것 iff user 일치(+ slot-scoped 면 그 슬롯).
            # my_user 미해소(None)면 귀속 불가 → 미포함(남의 user-claim 을 slot 번호로 끌어오는 누출 0).
            if my_user is not None and cb_user == my_user and (
                    not slot_scoped or ticket_rules._slot_matches(cb, my_slot)):
                return True
        elif not multi_user and ticket_rules._slot_matches(cb, my_slot):
            # legacy 슬롯-only claim(user 토큰 없음) — **진짜 단일 사용자(distinct user ≤1·not multi_user)**
            # 에서만 slot 매칭으로 포함. 게이트가 `my_user is None` 이 아니라 `not multi_user` 인 건
            # `user_name()` 이 git email 폴백으로 단일 사용자도 my_user 를 해소할 수 있어(흔함)
            # my_user proxy 면 그 보드의 자기 슬롯 legacy claim 을 잘못 숨기기 때문이다. multi_user 면
            # legacy 는 ambiguous → strict-exclude(migrate-identity backfill).
            return True
    # (a) 내 소유의 open.
    if status == "open":
        owner = _ticket_owner(home, fm, area_owner_in_use)
        if my_user and owner:
            return owner == my_user       # 소유 해소 → strict(유출 0)
        if multi_user:
            return False                  # 다중사용자 + 미해소 → strict-exclude
        return True                       # 단일 사용자 → all-open degrade 보존(회귀 0)
    return False


def _in_default_view(home, status: str, fm: dict, my_user: str | None,
                     my_session: str | None, area_owner_in_use: bool,
                     multi_user: bool) -> bool:
    """세션 기본 뷰(무인자 `list`) 멤버십 — **내 user ∧ session 스트림**.

    세션의 기본 화면은 그 세션이 **생성한 open + 그 세션 claim** 만 출력한다 — 단,
    user-qualified 귀속은 **현재 user 도 일치**해야 한다. 동명 task(`alice/main` 대 `bob/main`)와
    슬롯 재대여 뒤 이전 보유자 티켓이 같은 session 라벨만으로 섞이지 않게 open/claim 양쪽을
    user ∧ session 복합축으로 판정한다(전체는 명시 `--all`).

      - **바인딩 세션**(`my_session` 해소): 먼저 open 은 `_created_by_session`, claim(비-open)은
        `_slot_matches` exact 로 session 을 맞춘다. user-qualified 값은 각각 `_created_by_user` /
        `_claimed_by_user` 로 user 를 해소해 `my_user` 와도 strict 일치해야 한다.
      - **user 미해소 또는 legacy user 토큰 부재**: 다중사용자(`multi_user`)면 모호한 귀속을
        strict-exclude하고, 단일 사용자면 session-only 로 degrade한다. claim 분기는 의도적으로
        `_ticket_is_mine` 보다 관대하다. 단일 사용자에서 `my_user` 만 미해소된 user-qualified claim은
        기본 뷰에 보이지만 `--mine`에서는 제외된다. 조회 정체성을 잠시 못 구했다는 이유로 자기
        세션 티켓을 숨기지 않기 위한 보호이며, 다중사용자에서는 이 완화를 허용하지 않는다.
      - **무바인딩**(`my_session` None): user-단위 폴백 = `--mine`(내 소유 open + 내 claim·
        전 슬롯). N=1 은 부분집합·특례 아님 — user 스트림=세션 스트림이라 등가다.
        `_ticket_is_mine`(slot_scoped=False)를 그대로 상속한다.
    """
    if not my_session:
        # 무바인딩 — user-단위 폴백(--mine 동형·전 슬롯). open 소유·claim 판정은 단일
        # predicate 재사용(point-patch 금지). my_slot="" 은 slot 매칭 미발동(무바인딩=슬롯 없음).
        return _ticket_is_mine(home, status, fm, my_user, "", area_owner_in_use,
                               multi_user, slot_scoped=False)
    if status == "open":
        created_by = str(fm.get("created_by") or "").strip()
        if ticket_rules._created_by_session(created_by) != my_session:
            return False
        # 바인딩 세션 스트림은 생성자 축으로 판정한다. area_owner 는 backlog 소유 축이라
        # 의도적으로 참여시키지 않는다.
        # `_created_by_user` 는 slash 없는 migrate backfill 값을 user 로도 해석하므로, `/` 유무는
        # user-qualified 형상인지(legacy session-only인지)만 가른다. 토큰 파싱은 기존 헬퍼가 맡는다.
        created_user = ticket_rules._created_by_user(created_by) if "/" in created_by else None
        if my_user is not None and created_user is not None:
            return created_user == my_user
        return not multi_user
    # 비-open(claimed/blocked/done) — claim session AND user. `_claimed_by_user` 가 None 인
    # legacy 슬롯-only와 my_user 미해소는 multi-user strict-exclude / 단일 사용자 session-only degrade.
    claimed_by = fm.get("claimed_by") or ""
    if not ticket_rules._slot_matches(claimed_by, my_session):
        return False
    claimed_user = ticket_rules._claimed_by_user(claimed_by)
    if my_user is not None and claimed_user is not None:
        return claimed_user == my_user
    return not multi_user


def _ticket_ownership(home, fm: dict, *, task=None, user=None) -> ticket_rules._OwnershipVerdict:
    """이 실행 정체성이 `fm` 티켓의 claim 소유자인가 (claimed_by 부재면 무대상=통과).

    - `claimed_by` 없음 → 소유자 자체가 없으므로 통과(새 차단 아님·open 티켓의 claim/block 무영향).
    - 세션 미해소 → 거부. claim 이 `--task` 없이는 멈추는 조건과
      대칭이다 — 소유를 증명할 수 없는 상태에서 남의 티켓을 옮기는 쪽이 더 나쁘다.
    - user-qualified `claimed_by`(`<user>/<slot>`) → user 와 slot 이 **둘 다** 일치해야 한다.
    - 레거시 슬롯-only `claimed_by` → 다중사용자 보드면 귀속이 모호하므로 거부하고, solo
      (`_distinct_ticket_users` ≤1 ∧ `_distinct_area_owners` ≤1)에서만 slot 일치로 통과한다.
    """
    claimed_by = str(fm.get("claimed_by") or "").strip()
    if not claimed_by:
        return ticket_rules._OwnershipVerdict(True, None, None, False)
    if task:
        _validate_actor_task_or_exit(home, task)
    session = lease_store.session_name(home, task)
    if session is None:
        return ticket_rules._OwnershipVerdict(False, claimed_by, None, True)
    owner_user = ticket_rules._claimed_by_user(claimed_by)
    if owner_user is not None:
        my_user = lease_store.user_name(home, user)
        ok = (my_user is not None and owner_user == my_user
              and ticket_rules._slot_matches(claimed_by, session))
    else:
        # 슬롯-only 는 user 축이 없다 — 다중사용자 보드에서 슬롯 이름만으로 소유를 인정하면
        # 동명 슬롯의 타 사용자 claim 을 열어 준다(조회 뷰와 같은 strict-exclude).
        multi_user = _distinct_ticket_users(home) > 1 or _distinct_area_owners(home) > 1
        ok = (not multi_user) and ticket_rules._slot_matches(claimed_by, session)
    return ticket_rules._OwnershipVerdict(ok, claimed_by, session, False)


def _validate_actor_task_or_exit(home, task: str) -> None:
    """task 명 공유 validator 소비 — 불법이면 fail-loud (깔때기 단일 지점).

    정체성 깔때기(`_actor_session_override`·귀속)와 실행-위치 해소(`_resolve_task_workspace_cwd`)가
    **같은 한 지점**을 소비해, 무검증 task 명이 created_by/claimed_by/lease-session 으로 영속되거나
    실행-위치로 쓰이기 **이전** 부작용 0 로 거부한다. 공유 validator = `identity_args.validate_task_name`
    (worktree_pool 엔진 validator 와 동형·로직 중복 0). 예약패턴(`<repo>_<N>`) 판정용 registered_repos
    는 areas 에서 fail-soft 해소(파싱 실패 시 char/traversal 검증만).
    """
    try:
        registered = areas_store.registered_repos(home)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 예약패턴 검증만 완화(char/traversal 유지).
        registered = None
    try:
        identity_args.validate_task_name(task, registered)
    except identity_args.InvalidTaskName as e:
        sys.exit(
            f"[중단] {e} — `--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴"
            f"(`<repo>_<N>`)은 쓸 수 없다."
        )
