"""등록부 변경·자기 행·prefix 승인 재고 (층: app)."""
from __future__ import annotations

from typing import Any
from pathlib import Path
from pm_engine.domain import areas_rules
from pm_engine.infra import areas_store
from pm_engine.app import board_sync
from pm_engine.infra import fs
import functools
from pm_engine.infra import git
import json
from pm_engine.infra import pm_paths
import sys
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store
import urllib.parse
import yaml

import contextlib


def areas_append(home, prefix: str, area: str, owner: str,
                 *, repo: str | None = None, git: str | None = None,
                 test_cmd: str | None = None, base: str | None = None,
                 protected: str | None = None, area_owner: str | None = None) -> None:
    """Register a prefix in areas.md (append-only; create with header if missing).

    Append-only + `merge=union` (.gitattributes) → concurrent registrations from
    different clones never conflict.

    헤더 최초 생성(if-absent) + row append 를 **하나의 `board_lock()`** 구간으로
    원자화한다. 락이 없으면 동시 최초 등록 2개가 둘 다 "not exists" 를
    보고 → 둘 다 헤더를 write_text 해 한쪽이 다른쪽 append row 를 클로버한다(row 만
    O_APPEND 라도 헤더 race 가 남음). 락으로 감싸면 동시 최초 등록에도 헤더 1회·모든
    row 보존.

    스키마: per-repo 레지스트리
    `| repo | prefix | git | test_cmd | owner | base | protected | area_owner |`.
    `owner` = **등록 식별자(registrant)** — 협업 소유자(다중-사람)가 아니라 single user
    의 등록 출처 표식이다. 기본 = 현 세션. 컬럼/형식은 보존
    (test_path 바인딩·regression 게이트가 의존) — 의미만 재정의.
    `repo`/`git`/`test_cmd`/`base`/`protected`/`area_owner` 미지정 시 빈 칼럼으로 채운다
    (부분 등록 허용·하위호환). `base`는 worktree 슬롯 브랜치가 파생될 base 브랜치
    — 빈 값/누락이면 `_repo_base` 가 None 폴백(worktree add 가 현행 bare HEAD 동작).
    `protected`는 PM 이 자율 commit/push 못 하는 보호 브랜치(쉼표분리) — 빈 값/
    누락이면 `_repo_protected` 가 `DEFAULT_PROTECTED`(main/master/develop) 폴백.
    `area_owner`는 그 area 의 *user* 소유(`--mine` 풀 입력) — `owner`
    (registrant)와 별개 칼럼(overload 금지). 빈 값/누락이면 소비자가 None 으로 읽는다.
    `area`(구 스키마 칼럼)는 신 스키마에 칼럼이 없어 무시한다 — 호출 시그니처는
    하위호환을 위해 유지(기존 `cmd_init`·테스트가 positional 로 area 를 넘김).

    **재진입 금지**(board_lock docstring) — board_lock 보유 중에는 부르지 않는다.
    이미 락을 보유한 `cmd_init` 은 ``_areas_append_locked`` 변형을 직접 쓴다.
    """
    with fs.board_lock(home):
        areas_store._areas_append_locked(
            home, prefix, owner, repo=repo, git=git, test_cmd=test_cmd, base=base,
            protected=protected, area_owner=area_owner,
        )
    invalidate_known_prefixes_cache()


def areas_set_cell(home, repo: str, column: str, value: str) -> tuple[str, str]:
    """areas.md 의 `repo` 행 `column` 셀을 `value` 로 in-place 교체한다 — (옛 값, 새 값) 반환.

    `board_lock()` 하 read→transform→atomic write(temp + `fs.atomic_replace`) 이다 — `areas_append`
    (O_APPEND 등록)와 같은 락으로 직렬화해 동시 등록의 lost-update 를 막는다(
    `_migrate_areas_apply` 동형). 변환이 no-op(값 동일·`_areas_set_cell_text` 결과가 원문과
    같음)이면 **쓰지 않는다**(멱등).

    `column` 은 `_AREAS_COLUMNS` 의 칼럼명이어야 하고, `value` 는 표를 깨는 `|`·개행을 담을 수
    없다(둘 다 `ValueError`). 대상 행 부재 → `AreasRepoNotFound`, 중복 행 → `AreasDuplicateRepo`
    (둘 다 쓰기 이전·부작용 0).

    `value` 는 **strip 해서** 기록한다 — 범용 백엔드라 호출자가 정규화했다고 가정할 수 없고,
    `"  main , release  "` 를 verbatim 쓰면 셀 앞뒤 공백이 표에 남는다(`_parse_areas` 는 셀을
    strip 해 읽으므로 읽기값은 같지만 파일이 지저분해진다). 반환 `(old, new)` 의 new 도 그 값이다.

    **재진입 금지**(board_lock docstring) — board_lock 보유 중에는 부르지 않는다.
    이미 락을 보유한 호출부(`cmd_init` 의 등록 판정)는 ``_areas_set_cell_locked`` 를 직접 쓴다
    (`areas_append`/`_areas_append_locked` 와 같은 분리).
    """
    with fs.board_lock(home):
        old_value, value = areas_store._areas_set_cell_locked(home, repo, column, value)
    if column == "prefix" and old_value != value:
        invalidate_known_prefixes_cache()
    return old_value, value


def self_repo_coordinates(home) -> tuple[str, str] | None:
    """이 clone 자기 행(폴더 이름 행)에 적을 (git, base) — 값은 이 clone 에서만 나온다.

    git = `origin` 주소(없으면 이 clone 의 절대경로), base = 현재 브랜치(`symbolic-ref HEAD` 의 full
    ref 에서 `refs/heads/` 접두만 벗긴 이름 · 커밋 0개에서도 해소되고 같은 이름 태그와 무관하다).
    다음이면 None(호출부가 빈 칸 + `_SELF_COORDINATES_UNKNOWN` 한 줄):
    이 폴더가 자기 git 저장소의 최상위가 아니다(git 저장소가 아니거나 상위 저장소 안) · HEAD 가
    브랜치를 가리키지 않는다(detached) · origin 이 http·https 주소인데 사용자 정보(`@` 앞)가 있다
    (자격증명 · areas.md 는 커밋·push 되는 공유 등록부다) · 값에 areas 셀을 깨는 문자(`|`·개행)가 있다.
    """
    rc, top = git._git_scope_run(home.root, ["rev-parse", "--show-toplevel"])
    if rc != 0 or Path(top.strip()).resolve() != home.root.resolve():
        return None
    rc, ref = git._git_scope_run(home.root, ["symbolic-ref", "-q", "HEAD"])
    ref = ref.strip()
    if rc != 0 or not ref.startswith(areas_rules._SYMREF_BRANCH_PREFIX):
        return None
    base = ref[len(areas_rules._SYMREF_BRANCH_PREFIX):]
    rc, origin = git._git_scope_run(home.root, ["remote", "get-url", "origin"])
    git_url = origin.strip() if rc == 0 and origin.strip() else str(home.root)
    origin_address = urllib.parse.urlsplit(git_url)
    if origin_address.scheme in areas_rules._ORIGIN_SCHEMES_WITH_SECRET_USERINFO and "@" in origin_address.netloc:
        return None
    if any(ch in value for value in (git_url, base) for ch in areas_rules._AREAS_CELL_BREAKERS):
        return None
    return git_url, base


def fill_self_repo_row(home, *, write: bool) -> str | None:
    """갱신 이관 — 폴더 이름 행의 `git`·`base` 가 **둘 다 비어** 있으면 이 clone 의 값으로 채운다.

    설치(`cmd_init`)가 자기 행에 두 칸을 기록하기 전에 만들어진 채택자의 데이터 이관이다(갱신 완료
    단계 `pm_update.fill_self_repo_row` 가 부른다). 대상 행이 없거나 어느 한 칸이라도 차 있으면
    아무것도 하지 않는다(덮어쓰지 않는다·다른 repo 행은 보지 않는다). 같은 이름 행이 둘 이상이면
    `AreasDuplicateRepo` 를 그대로 올린다(`board.py lint` 의 areas-duplicate-repo 와 같은 처방).
    값을 정하지 못하면 알림 한 줄 뒤 무동작. `write=False` 는 판정만 한다.
    반환 = 채운(채울) 값 설명 또는 None.
    """
    with fs.board_lock(home) if write else contextlib.nullcontext():
        _header, rows = areas_store._parse_areas(home)
        own = [row for row in rows if row.get("repo") == home.root.name]
        if len(own) > 1:
            raise areas_rules.AreasDuplicateRepo(home.root.name, len(own))
        if not own or (own[0].get("git") or "").strip() or (own[0].get("base") or "").strip():
            return None
        coordinates = self_repo_coordinates(home)
        if coordinates is None:
            print(areas_rules._SELF_COORDINATES_UNKNOWN, file=sys.stderr)
            return None
        git, base = coordinates
        if write:
            areas_store._areas_set_cell_locked(home, home.root.name, "git", git)
            areas_store._areas_set_cell_locked(home, home.root.name, "base", base)
    if write:
        # board_lock 밖에서 부른다(락 순서: board_git_lock → board_lock). inline board 는 무동작.
        board_sync._board_git_sync_best_effort(home, "areas 자기 행 git·base", (pm_paths.areas_file(home),))
    return f"repo={home.root.name} | git={git} | base={base}"


def _parse_prefix_arg(raw: str) -> str | None:
    """CLI prefix 인자 → 실 prefix(str) 또는 None(무prefix). 예약어 `none`(대소문자 무관) → None.

    `none` 은 이름 없는(`T-NNNN`) 네임스페이스의 1급 인자라 from/to/into 어디서든
    `None`(무prefix) 로 해소된다. prefix 동일성이 case-insensitive fold 이므로 `NONE`/`None` 도
    같은 예약어로 fold 된다. `none` 이 실 prefix 로 등록될 수 없게 예약돼 있어
    (`_validate_prefix`) 실 카테고리와 충돌하지 않는다.
    """
    return None if raw.lower() in ticket_rules._PREFIX_RESERVED else raw


def _format_ticket_id(prefix: str | None, num: int) -> str:
    """prefix(또는 None=무prefix)와 순번으로 canonical ID 생성 (`_next_id` 발행 규칙과 정합).

    None → `T-NNNN`(최소 4자리) · prefix → `T-<p>-NNN`(최소 3자리). zero-pad 는 *최소* 폭이라
    번호가 커지면 자연 확장된다(`_next_id` 와 동형).
    """
    if prefix:
        return f"T-{prefix}-{num:03d}"
    return f"T-{num:04d}"


def _scan_prefix_tickets(home, skipped: list[Path] | None = None) -> list[dict[str, Any]]:
    """STATUS_DIRS 전 티켓을 relabel 용 레코드로 수집한다 (id·num·prefix·status·path·created).

    `created` 는 merge 시간순 정렬 키(문자열 ISO 로 정규화 — yaml date/str 양쪽 흡수). ID 미해소
    (파일명·frontmatter 둘 다 실패)나 순번 미상 파일은 건너뛴다(`cmd_prefix_list` 버킷팅과 동형).

    `skipped` 를 주면 **읽지 못한 티켓 경로**를 거기 싣는다. 이 스캔은 조회(prefix list)와
    mutation(rename·merge·reid·delete)이 함께 쓰는데, 못 읽은 티켓의 의미가 두 쪽에서 다르다:
    조회는 한 줄 빠질 뿐이지만 mutation 은 그 티켓의 **ID 를 못 본 채** 유일성을 판정하므로
    그 ID 위로 다른 티켓을 덮어쓴다. 그래서 skip 사실을 삼키지 않고 호출부에 돌려준다
    (`_prefix_scan` 이 mutation 쪽 판정을 소유).
    """
    out: list[dict[str, Any]] = []
    # .drafts 포함 — draft 도 이미 발행된 ID(find_ticket/_next_id 인지).
    # relabel 이 draft 를 놓치면 old-prefix draft 가 잔존, promote 시 혼재가 보드로 재유입된다.
    for status in (*ticket_rules.STATUS_DIRS, ".drafts"):
        for p in sorted((pm_paths.tickets_dir(home) / status).glob("T-*.md")):
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                if skipped is not None:
                    skipped.append(p)
                continue
            fm, _ = loaded
            tid = ticket_store._issued_ticket_id(home, p, fm)
            if not tid:
                continue
            num = ticket_rules._ticket_id_number(tid)
            if num is None:
                continue
            out.append({
                "id": tid, "num": num, "prefix": ticket_rules._ticket_prefix(tid),
                "status": status, "path": p,
                "created": str(fm.get("created") or ""),
            })
    return out


# ID 재부여 표면의 손상-티켓 차단 안내 — 조회 표면과 갈리는 지점을 문구가 스스로 밝힌다.
_UNREADABLE_SCAN_BLOCK = (
    "[중단] {op}: 읽지 못한 티켓 {count}건이 있어 ID 유일성을 판정할 수 없다 — {sample}\n"
    "  · 못 본 ID 는 곧 clobber 다 — 유일성 검사와 번호 이어붙이기(`start=max(...)`)가 그 ID 를 "
    "모른 채 전진해 다른 티켓을 그 위에 덮어쓴다.\n"
    "  · 위 경고의 frontmatter 오류를 고친 뒤 다시 실행하라. 조회 표면"
    "(`list`·`prefix list`·`lint`)은 그 티켓만 빼고 계속 동작한다."
)


def _prefix_scan(home, op: str) -> tuple[list[dict[str, Any]] | None, int]:
    """ID 재부여 표면용 전수 스캔 — 읽지 못한 티켓이 1건이라도 있으면 `(None, 1)` 로 막는다.

    조회는 손상 1건을 건너뛰고 계속 도는 게 옳지만(보드 전체가 조회 불능이 되면 안 된다),
    **ID 를 재부여하는 표면은 반대**다: 못 본 ID 는 유일성 판정에서 빠져 그 위로 다른 티켓이
    덮인다. 같은 스캔을 쓰되 skip 의 의미만 표면별로 가른다.
    """
    skipped: list[Path] = []
    tickets = _scan_prefix_tickets(home, skipped)
    if not skipped:
        return tickets, 0
    sample = ", ".join(pm_paths._rel_to_repo(home, p) for p in skipped[:5])
    if len(skipped) > 5:
        sample += " …"
    print(_UNREADABLE_SCAN_BLOCK.format(op=op, count=len(skipped), sample=sample),
          file=sys.stderr)
    return None, 1


class PrefixSourcesUnreadable(RuntimeError):
    """prefix 승인 게이트의 3소스 중 하나를 신뢰할 수 없어 fail-loud 해야 함."""


@functools.lru_cache(maxsize=1)
def _cheap_prefix_inventory(home) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """값싼 2소스(areas·task) prefix 재고를 한 번 읽는다.

    3소스 합성의 입력이자 전체 티켓 inventory가 손상됐을 때의 진단 fallback이다.
    값싼 소스 자체의 fold case 오염은 전부 보존해, 티켓 소스를 끝내 읽지 못해도
    `_prefix_target_snapshot`이 이미 확인한 case 변형과 출처를 잃지 않게 한다.
    """
    try:
        area_prefixes = set(areas_store.registered_prefixes(home))

        task_prefixes: set[str] = set()
        if pm_paths.leases_file(home).exists():
            try:
                ledger = json.loads(fs.read_text_shared(pm_paths.leases_file(home), encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise PrefixSourcesUnreadable(
                    f"task 장부 {pm_paths.leases_file(home)}를 읽을 수 없음: {exc}"
                ) from exc
            if not isinstance(ledger, dict):
                raise PrefixSourcesUnreadable("task 장부 최상위 값이 object가 아님")
            tasks = ledger.get("tasks", [])
            if not isinstance(tasks, list):
                raise PrefixSourcesUnreadable("task 장부의 tasks 값이 list가 아님")
            for index, task in enumerate(tasks):
                if not isinstance(task, dict) or not task.get("name"):
                    raise PrefixSourcesUnreadable(
                        f"task 장부 tasks[{index}]가 유효한 task object가 아님"
                    )
                prefix = task.get("prefix")
                if prefix in (None, ""):
                    continue
                if not isinstance(prefix, str):
                    raise PrefixSourcesUnreadable(
                        f"task 장부 tasks[{index}].prefix가 문자열/null이 아님"
                    )
                reason = ticket_rules._validate_prefix(prefix)
                if reason:
                    raise PrefixSourcesUnreadable(
                        f"task 장부 tasks[{index}].prefix 손상: {reason}"
                    )
                task_prefixes.add(prefix)

        by_prefix: dict[str, set[str]] = {}
        for source, prefixes in (
            ("areas.md", area_prefixes),
            ("task 장부", task_prefixes),
        ):
            for prefix in prefixes:
                by_prefix.setdefault(prefix, set()).add(source)
        return tuple(
            (prefix, tuple(sorted(sources)))
            for prefix, sources in sorted(
                by_prefix.items(), key=lambda item: (item[0].lower(), item[0])
            )
        )
    except PrefixSourcesUnreadable:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise PrefixSourcesUnreadable(f"prefix 소스를 읽을 수 없음: {exc}") from exc


@functools.lru_cache(maxsize=1)
def _known_prefix_inventory(home) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """3소스 prefix와 각 출처를 immutable·결정적 snapshot으로 한 번 읽는다.

    소스는 (1) areas.md prefix, (2) 기발행 티켓 ID prefix, (3) worktree task 장부의
    모든 ``Task.prefix``다. per-clone ``local.conf`` 는 소스가 아니다 — clone-local 라벨은
    ``id_prefix`` 해소에도 없으므로 승인 게이트에도 whitelist하지 않는다.

    board는 worktree_pool 엔진을 import하지 않는 기존 격리를 지킨다. worktree_pool이
    atomic-replace하는 JSON 장부를 strict point-read하고, 손상은 빈 집합으로 완화하지 않는다.
    case 충돌 판정은 값싼 소스의 fold-match 여부와 무관하게 항상 티켓까지 3소스를
    확인한다. 첫 전수 스캔 결과는 이 함수의 프로세스 캐시에 보관해 후속 판정 비용을
    회수하며, 티켓 손상은 빈 집합으로 완화하지 않고 fail-loud한다.
    동일 문자열이 여러 소스에 있으면 출처를 합치고, case 변형은 서로 다른 항목으로 보존한다.
    그래야 ``AAA``/``aaa`` 오염을 union 과정에서 숨기지 않고 발견된 변형과 소스를 전부
    fail-loud할 수 있다. 반환 순서는 ``(lower, exact)``로 고정해 set/hash 순서와 무관하다.
    """
    try:
        skipped: list[Path] = []
        ticket_rows = _scan_prefix_tickets(home, skipped)
        if skipped:
            sample = ", ".join(pm_paths._rel_to_repo(home, path) for path in skipped[:5])
            if len(skipped) > 5:
                sample += " …"
            raise PrefixSourcesUnreadable(
                f"읽지 못한 티켓 {len(skipped)}건({sample}) 때문에 기발행 prefix를 확정할 수 없음"
                "(누락 판정은 신규 ID clobber 위험)"
            )
        ticket_prefixes = {
            prefix for row in ticket_rows if (prefix := row.get("prefix"))
        }

        by_prefix = {
            prefix: set(sources) for prefix, sources in _cheap_prefix_inventory(home)
        }
        for prefix in ticket_prefixes:
            by_prefix.setdefault(prefix, set()).add("기발행 티켓")
        return tuple(
            (prefix, tuple(sorted(sources)))
            for prefix, sources in sorted(
                by_prefix.items(), key=lambda item: (item[0].lower(), item[0])
            )
        )
    except PrefixSourcesUnreadable:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise PrefixSourcesUnreadable(f"prefix 소스를 읽을 수 없음: {exc}") from exc


@functools.lru_cache(maxsize=128)
def known_prefixes(home, target: str | None = None) -> frozenset[str]:
    """신규-prefix 판정의 단일 진실인 3소스 합집합(canonical case 보존).

    ``target``은 기존 호출 호환용이며 판정 범위를 줄이지 않는다. 값싼 소스에
    fold/exact match가 있어도 다른 case의 기발행 티켓이 있으면 canonical이 충돌하고,
    `_next_prefixed_id`가 티켓 case를 이어 쓸 수 있으므로 반드시 3소스를 모두 읽는다.
    전수 스캔은 `_known_prefix_inventory`의 프로세스 1회 캐시로 상쇄한다.
    """
    return frozenset(prefix for prefix, _sources in _known_prefix_inventory(home))


def invalidate_known_prefixes_cache() -> None:
    """이 프로세스가 prefix 소스를 변경한 뒤 1회 캐시를 무효화한다."""
    known_prefixes.cache_clear()
    _known_prefix_inventory.cache_clear()
    _cheap_prefix_inventory.cache_clear()


def _prefix_target_snapshot(
    home, prefix: str,
    *,
    surface: str,
) -> tuple[str | None, bool]:
    """현재 snapshot에서 ``(canonical, is_new)``를 반환한다. 판정 불능이면 ``(None, False)``.

    한 fold에 case 변형이 둘 이상이면 어느 항목도 canonical로 임의 선택하지 않는다. 발견된
    모든 변형과 3소스 출처를 정렬 출력하고, 먼저 한 case로 수렴시키라는 처방과 함께 막는다.
    """
    try:
        known = known_prefixes(home, prefix)
    except PrefixSourcesUnreadable as exc:
        # 전체 티켓 inventory가 손상됐어도 값싼 소스만으로 이미 case 충돌이 확정됐다면
        # 그 출처를 버리지 않는다. 4번째 소스를 읽지 못한 사실도 함께 밝혀 쓰기는 계속 막는다.
        try:
            fallback_inventory = _cheap_prefix_inventory(home)
        except PrefixSourcesUnreadable:
            fallback_inventory = ()
        fallback_matches = sorted(
            (
                candidate for candidate, _sources in fallback_inventory
                if candidate.lower() == prefix.lower()
            ),
            key=lambda value: (value.lower(), value),
        )
        if len(fallback_matches) > 1:
            _print_prefix_case_conflict(
                prefix,
                fallback_matches,
                dict(fallback_inventory),
                surface=surface,
                inventory_error=exc,
            )
        else:
            print(
                f"[중단] {surface}: prefix 3소스 합집합을 신뢰할 수 없다 — {exc}. "
                "소스 손상을 고친 뒤 다시 실행하라.",
                file=sys.stderr,
            )
        return None, False

    matches = sorted(
        (candidate for candidate in known if candidate.lower() == prefix.lower()),
        key=lambda value: (value.lower(), value),
    )
    if len(matches) > 1:
        try:
            source_map = dict(_known_prefix_inventory(home))
            inventory_error = None
        except PrefixSourcesUnreadable as exc:
            # known_prefixes가 monkeypatch/동시 변경 등으로 먼저 값을 돌려준 뒤 full source
            # map만 실패하는 경로에서도 확인된 값싼 출처를 진단 fallback으로 보존한다.
            try:
                source_map = dict(_cheap_prefix_inventory(home))
            except PrefixSourcesUnreadable:
                source_map = {}
            inventory_error = exc
        _print_prefix_case_conflict(
            prefix,
            matches,
            source_map,
            surface=surface,
            inventory_error=inventory_error,
        )
        return None, False
    if matches:
        return matches[0], False
    return prefix, True


def _print_prefix_case_conflict(
    prefix: str,
    matches: list[str],
    source_map: dict[str, tuple[str, ...]],
    *,
    surface: str,
    inventory_error: PrefixSourcesUnreadable | None = None,
) -> None:
    """case 충돌을 출처 보존·결정적 순서로 출력한다."""
    variants = "; ".join(
        f"{candidate!r} ← {', '.join(source_map.get(candidate, ('소스 미상',)))}"
        for candidate in matches
    )
    incomplete = (
        f"\n  진단 보존: 티켓 inventory 오류({inventory_error})로 3소스 출처를 모두 "
        "확정하지 못했지만, 위 값싼 소스 출처만으로도 case 충돌은 확정적이다."
        if inventory_error is not None
        else ""
    )
    print(
        f"[중단] {surface}: prefix {prefix!r}의 case 변형이 2개 이상 존재해 canonical을 "
        f"결정할 수 없다 — {variants}.{incomplete}\n"
        "  정리 처방: 하나의 canonical case를 고른 뒤 areas.md·task 장부를 "
        "그 case로 맞추고, 기발행 티켓은 `board.py prefix rename/merge`로 같은 case에 "
        "수렴시킨 다음 재실행하라. 임의 변형 선택은 새 ID 네임스페이스를 분할하므로 "
        "수행하지 않는다.",
        file=sys.stderr,
    )


def require_prefix_user_ack(
    home, prefix: str,
    user_ack: str | None,
    *,
    surface: str,
) -> str | None:
    """기존 prefix의 canonical case 또는 승인된 신규 prefix를 반환하고, 거부면 None.

    prefix 동일성은 3소스 합집합에 case-insensitive fold로 대조한다. 신규 라벨은
    ``--user-ack <prefix>`` 값이 대상과 정확히 결속될 때만 통과한다. 이 토큰은 적대적
    우회 방지가 아니라 사용자 승인 마찰과 감사 surface이며, 승인 주체가 아닌 세션이
    자가 부착할 수 있다는 위협 모델은 의도적으로 숨기지 않는다.
    """
    canonical, is_new = _prefix_target_snapshot(home, prefix, surface=surface)
    if canonical is None:
        return None
    if not is_new:
        return canonical
    if user_ack == prefix:
        print(
            f"[승인 감사] {surface}: 사용자 승인 토큰이 새 prefix {prefix!r}에 값-결속됨."
        )
        return prefix

    known = known_prefixes(home)
    categories = ", ".join(
        sorted(known, key=lambda value: (value.lower(), value))
    ) or "(없음)"
    mismatch = (
        f" 제공된 값 {user_ack!r}은 대상에 결속되지 않았다."
        if user_ack is not None
        else ""
    )
    print(
        f"[중단] {surface}: 새 prefix {prefix!r} 신설에는 사용자 명시 승인이 필요하다.{mismatch}\n"
        f"  1순위: 사용자에게 새 카테고리 {prefix!r} 승인을 요청하라.\n"
        f"  현재 카테고리(areas ∪ 티켓 ∪ task): {categories}\n"
        "  정상 등록 경로: areas.md prefix 칼럼 또는 `pm-config task prefix`로 먼저 등록한다. "
        "두 경로도 신규 라벨이면 최초 1회 사용자 승인을 지나야 한다.\n"
        f"  부차 수단: 승인한 사용자만 원 명령에 `--user-ack {prefix}`를 대상값 그대로 붙여 "
        "실행하라(세션 자동 부착 금지).",
        file=sys.stderr,
    )
    return None


def revalidate_prefix_user_ack(
    home, prefix: str,
    user_ack: str | None,
    expected: str,
    *,
    surface: str,
) -> str | None:
    """쓰기 락 안에서 캐시를 버리고 승인 판정을 다시 해 선판정과 같을 때만 반환한다."""
    invalidate_known_prefixes_cache()
    fresh = require_prefix_user_ack(home, prefix, user_ack, surface=f"{surface} (락 안 fresh 재검증)")
    if fresh == expected:
        return fresh
    print(
        f"[중단] {surface}: prefix canonical 판정이 선판정 {expected!r}에서 "
        f"fresh snapshot {fresh!r}로 달라졌다 — 동시 세션이 prefix 소스를 변경했을 수 있어 "
        "쓰기 0으로 중단한다. 현황을 정리·재확인한 뒤 원 명령을 다시 실행하라.",
        file=sys.stderr,
    )
    return None
