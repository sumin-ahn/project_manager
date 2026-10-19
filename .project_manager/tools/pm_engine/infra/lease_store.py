"""리스 장부(`worktree-leases.json`) 읽기와 장부 기반 해소 (층: infra).

장부 행 판정 규칙과 결과 형식은 `pm_engine.domain.identity_args` 가 소유하고, 이 모듈은 장부를 공유 읽기로
읽어 그 규칙을 적용한다. 장부는 `worktree_pool` 이 원자 교체하는 파일이라 판독도
`fs.read_text_shared` 를 지난다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pm_engine.domain import identity_args
from pm_engine.infra import fs


_CODEX_HARNESS_SESSION_MARKERS: tuple[str, ...] = (
    "CODEX_THREAD_ID",
    "CODEX_CI",
)


def _runtime_skill_entry_prefix() -> str:
    """현재 실행 하네스의 스킬 진입 접두사. Codex env marker 외 경로는 slash다."""
    return "$" if any(os.environ.get(key) for key in _CODEX_HARNESS_SESSION_MARKERS) else "/"


def _runtime_skill_entry(skill: str) -> str:
    """사용자에게 제시할 현재 하네스의 PM 스킬 호출 표기."""
    return f"{_runtime_skill_entry_prefix()}{skill}"


# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py·pm_config.py 와 동일
# 앵커 관례(어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).


# ── 공유 읽기 (원자 교체 대상 장부) ──────────────────────────────────────
# 이 모듈이 읽는 리스 장부(`worktree-leases.json`)는 `worktree_pool` 이 **원자 교체**하는
# 파일이다. 일반 `open` 리더가 하나라도 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막으므로,
# 판독도 공용 seam 의 공유 읽기(`fs.read_text_shared`)를 지난다.

def task_prefix(name: str, leases_file: Path) -> str | None:
    """장부 top-level `tasks` 컬렉션에서 task `name` 의 board prefix 를 읽는다 — 없으면 None.

    `board.py new --task <task>` 이 `--prefix` 명시가 없을 때 task 설정 prefix(`task prefix`
    로 설정·기본 None)를 참조한다. 순수 point-read(부작용 0·`worktree_pool` 미import·
    데이터 결합) — 부재/손상/미설정 → None(fail-soft·caller 가 유도 체인으로 폴백).
    """
    try:
        data = json.loads(fs.read_text_shared(leases_file, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    for t in tasks:
        if isinstance(t, dict) and t.get("name") == name:
            return t.get("prefix") or None
    return None


# ── 리스 IO 층 (worktree-leases.json 원장 point-read·격리 관성) ──────────


def _load_lease_rows(leases_file: Path) -> list[dict] | None:
    """`leases_file` 의 `leases` 배열을 dict 리스트로 읽는다 — `leased_sessions`·`repo_slot_numbers`
    가 공유하는 내부 IO 프리미티브(모듈 내 DRY).

    부재/JSON 깨짐/스키마 불일치(최상위 dict 아님·`leases` 가 list 아님) → `None`(fail-soft 신호 —
    "읽을 수 없음"). 두 공개 함수가 이 `None` 을 각자의 관례로 번역한다.
    """
    try:
        data = json.loads(fs.read_text_shared(leases_file, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("leases", [])
    if not isinstance(rows, list):
        return None
    return rows


def _read_lease_rows_status(leases_file: Path) -> "tuple[str, list[dict]]":
    """장부 `leases` dict 행 + **읽기 축 판정**(`LEDGER_*`) — 부재/손상/정상을 구분한다.

    `_lease_dict_rows`(fail-soft·빈 리스트)의 판정형 짝이다. 해소 결과가 "판정 불능"일 때
    호출부가 *왜* 불능인지(부재냐 손상이냐)를 그대로 사용자에게 낼 수 있게 사유를 나른다.
    """
    try:
        exists = leases_file.exists()
    except OSError:
        return identity_args.LEDGER_UNREADABLE, []
    if not exists:
        return identity_args.LEDGER_MISSING, []
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return identity_args.LEDGER_UNREADABLE, []
    return identity_args.LEDGER_OK, [row for row in rows if isinstance(row, dict)]


def leased_sessions(leases_file: Path) -> list[str]:
    """lease 장부에서 `state=="leased"` 행들의 session 목록 (count-based 유도용).

    `board._leased_sessions`·`pm_config._leased_sessions` 흡수(동형·byte-for-byte 동작 보존) —
    `state` 가 **정확히 "leased"** 인 행만 센다(back-compat 기본값 없음 — 원 두 사본과 동형).
    장부 부재/파싱실패/손상 → 빈 리스트(fail-soft — 세션 해소가 장부 손상으로 죽지 않게). session
    이 빈/None 인 행은 제외.

    주의(의도된 비대칭): `repo_slot_numbers` 는 `state` 키 **부재**를 `"leased"` 로 back-compat
    처리한다(원 `pm_bootstrap._repo_slot_numbers` 동형) — 이 함수는 그러지 않는다(원 두 `_leased_
    sessions` 사본 동형).
    """
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return []
    sessions: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("state") == "leased":
            sess = row.get("session")
            if sess:
                sessions.append(sess)
    return sessions


def task_names(leases_file: Path) -> set[str]:
    """장부 top-level `tasks` 컬렉션의 이름 집합 — task 축의 단일 진실(부재/손상 → 빈 집합).

    "이 세션이 task 인가"를 **이름 모양**(`foo_1` 형상 추측)이 아니라 등록 membership 으로 묻기
    위한 point-read (`worktree_pool.reclaim_stale`·`_ensure_slot_pm_state_locked` 가 이미 쓰는
    근거와 같은 축). fail-soft — 장부가 없으면 등록 task 도 없다.
    """
    try:
        data = json.loads(fs.read_text_shared(leases_file, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    rows = data.get("tasks", [])
    if not isinstance(rows, list):
        return set()
    return {t["name"] for t in rows
            if isinstance(t, dict) and isinstance(t.get("name"), str) and t.get("name")}


def repo_slot_numbers(repo: str, leases_file: Path) -> list[int] | None:
    """`leases_file` 장부에서 `repo` 의 **활성(leased)** 슬롯이 점유한 번호 집합.

    번호는 **장부 행의 값**에서 계상한다 — 행의 slot 경로(`work/<repo>_<N>`)와 행의 session
    (`<repo>_<N>`) 양쪽(`_row_slot_numbers`). 경로에 번호가 없는 행도 session 값으로 자기
    번호를 점유하므로, 번호 조회/충돌 회피가 그 행을 빠뜨리지 않는다. `state` 가 `"leased"` 인 엔트리만 센다 — **`state` 키 부재는 `"leased"` 로
    back-compat**(`worktree_pool.from_dict` default 동형·`leased_sessions` 와의 의도된 비대칭은
    위 docstring 참고). 같은 번호의 중복 장부 엔트리는 dedup(정렬된 unique 목록).

    파일 부재/JSON 깨짐/스키마 불일치 → `None`(fail-soft·"읽을 수 없음"); 정상 read 인데 그 repo
    의 leased 슬롯이 0개면 빈 리스트 `[]`("읽었으나 활성 슬롯 없음"). 호출부는 두 경우를 구분한다.

    **폴더 해소는 이 번호 축을 타지 않는다** — `resolve_actor_workspace` 는 장부 행 값(session/slot)
    으로 해소한다(번호에서 세션을 재구성하지 않는다·아래).
    """
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return None
    slot_nums: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("repo") != repo:
            continue
        # 활성(leased) 슬롯만 — idle(반납)은 죽은 세션이라 라우팅 대상 아님(codex must-fix 원 흡수).
        # state 키 부재는 leased 로 본다(worktree_pool from_dict default·back-compat).
        if row.get("state", "leased") != "leased":
            continue
        slot_nums |= identity_args._row_slot_numbers(repo, row.get("slot"), row.get("session"))
    return sorted(slot_nums)


def repo_slot_state_counts(repo: str, leases_file: Path) -> "dict[str, int] | None":
    """`leases_file` 장부에서 `repo` 슬롯을 `state` 값별로 센다 — 발행 표면(`board.py new`/
    `promote`)의 "가용(idle) 슬롯 수" 재료가 쓰는 유일한 집계 지점이다.

    `state` 는 3값(`leased`|`idle`|`creating`·`worktree_pool.py`)이고 그 값을 **그대로** 버킷
    키로 쓴다 — `creating`(provisional)을 `idle`로 세지 않는다. `state` 키 부재는
    `repo_slot_numbers` 와 동형으로 `"leased"` back-compat 처리한다(장부의 다수 소비자가 이미
    같은 default 를 쓴다 — 오래된 행을 idle 로 잘못 세지 않는 안전한 방향).

    파일 부재/JSON 깨짐/스키마 불일치(`_load_lease_rows` 가 이미 판정한 축) → `None`
    (판정불능 — 호출부가 슬롯 재료 줄 자체를 생략한다. 모르는 값을 0 으로 주장하지 않는다).
    정상 read 인데 그 repo 행이 0개면 빈 `dict`(idle 0 을 정당하게 보고한다).
    """
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return None
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("repo") != repo:
            continue
        state = row.get("state", "leased")
        key = state if isinstance(state, str) and state else "leased"
        counts[key] = counts.get(key, 0) + 1
    return counts


def slot_path_resolution(repo: str, number: int, leases_file: Path) -> identity_args.SlotPathResolution:
    """`repo` 슬롯 번호 `number` → **활성 행의 slot 경로 값** + 판정 사유(discriminated).

    명시 `--repo X --slot N` 이 실행 슬롯 경로를 `work/<repo>_<N>` 로 **재조립**하던 자리를 장부
    값으로 바꾼다. 장부가 그 번호를 다른 경로로 들고 있으면(`nested/X_1`·PM 홈 자신을 가리키는
    `.`) 재조립 값은 존재하지 않는 경로다. 번호 점유 판정은 `repo_slot_numbers` 와 같은 규칙
    (`_row_slot_numbers` — 경로 값 ∪ session 값)을 공유한다.

    같은 번호를 서로 다른 경로가 주장하면 `SLOT_PATH_CONFLICT` — 임의 선택도, canonical 추측도
    하지 않는다. 쓰기 진입은 이 값을 fail-loud 로 번역한다.
    """
    status, rows = _read_lease_rows_status(leases_file)
    if status != identity_args.LEDGER_OK:
        return identity_args.SlotPathResolution(status, None, f"장부를 읽을 수 없다({leases_file})")
    slots: set[str] = set()
    for row in rows:
        if row.get("repo") != repo:
            continue
        if row.get("state", "leased") != "leased":
            continue
        if number in identity_args._row_slot_numbers(repo, row.get("slot"), row.get("session")):
            slot = identity_args.normalize_slot_value(row.get("slot"))
            if slot:
                slots.add(slot)
    if len(slots) == 1:
        return identity_args.SlotPathResolution(identity_args.SLOT_PATH_OK, next(iter(slots)))
    if not slots:
        return identity_args.SlotPathResolution(
            identity_args.SLOT_PATH_ROW_ABSENT, None,
            f"repo {repo!r} 의 활성 행 중 슬롯 번호 {number} 를 점유한 행이 없다")
    listing = ", ".join(sorted(slots))
    return identity_args.SlotPathResolution(
        identity_args.SLOT_PATH_CONFLICT, None,
        f"repo {repo!r} 슬롯 번호 {number} 를 서로 다른 경로 {len(slots)}개({listing})가 "
        f"주장한다 — 장부 모순이라 임의 선택하지 않는다")


def resolve_actor_workspace(repo: str, leases_file: Path) -> "identity_args.Workspace | None":
    """actor(`--repo`-단독) 작업공간 해소 — `repo` 의 활성(leased) 슬롯이 정확히 1개면 **그 행**.

    **정체성은 행의 `session`, 경로는 행의 `slot`** — 어느 쪽도 다른 쪽에서 재구성하지 않는다.
    이전 구현은 번호 집합(`repo_slot_numbers`)으로 해소한 뒤 세션을 `f"{repo}_{N}"` 로 **재조립**
    해서, 장부의 실제 session 과 갈렸다(task 이름으로 대여된 슬롯을 `<repo>_<N>` 로 오귀속). 여기서
    행 값을 그대로 내고, 경로가 필요한 호출부(handoff 실행 슬롯·worktree_pool dev/sync)는
    `.slot` 을 쓴다 — 세션에서 `work/<session>` 을 만들지 않는다.

    - 활성 슬롯 0개(장부 부재/파싱실패/그 repo 활성 슬롯 없음) → `None`(fail-soft·미해소 신호).
    - 활성 슬롯 ≥2 → `SlotResolutionError`(fail-loud·모호).
    - 같은 슬롯의 중복 행이 서로 **다른 session** 을 주장하면(장부 모순) 임의 선택하지 않고
      `SlotResolutionError` — 판정 불능은 통과가 아니다.
    """
    by_slot: dict[str, list[dict]] = {}
    for row in _lease_dict_rows(leases_file):
        if row.get("repo") != repo:
            continue
        # 활성(leased)만 — state 키 부재는 leased(worktree_pool from_dict default·back-compat).
        if row.get("state", "leased") != "leased":
            continue
        slot = identity_args.normalize_slot_value(row.get("slot"))
        if not slot:
            continue
        by_slot.setdefault(slot, []).append(row)
    if not by_slot:
        return None
    if len(by_slot) > 1:
        listing = ", ".join(sorted(
            by_slot, key=lambda s: (identity_args.slot_number_for_repo(repo, identity_args.slot_path_name(s)) or 0, s)))
        raise identity_args.SlotResolutionError(
            f"repo '{repo}' 활성 슬롯 {len(by_slot)}개({listing}) 중 하나로 특정할 수 없다 — "
            f"`--slot <N>` 으로 명시하라."
        )
    rows = next(iter(by_slot.values()))
    sessions = {str(row.get("session") or "") for row in rows}
    if len(sessions) > 1:
        listing = ", ".join(sorted(repr(s) for s in sessions))
        raise identity_args.SlotResolutionError(
            f"repo '{repo}' 슬롯 {rows[0].get('slot')!r} 에 session 이 서로 다른 장부 행이 "
            f"{len(sessions)}개({listing}) — 장부 모순이라 임의 선택하지 않는다. "
            f"`--slot <N>` 으로 명시하거나 장부를 정리하라."
        )
    row = rows[0]
    return identity_args.Workspace(
        slot=str(row.get("slot")),
        repo=row.get("repo") or repo,
        session=(row.get("session") or None),
        test_cmd=row.get("test_cmd"),
        readonly=row.get("role") == identity_args._LEASE_ROLE_READONLY,
    )


def session_coordinates(session: object, leases_file: Path) -> "tuple[str | None, int | None]":
    """세션 명의 → 재접속 좌표 `(repo, number)` — **장부 행 값 1순위**, 행이 없으면 키 분해.

    세션 키를 `rpartition("_")` 로 뜯어 `(repo, N)` 을 만들면 행의 repo 와 어긋난 session
    (`repo=A · slot=work/A_1 · session=B_7`)에서 장부에 없는 repo `B` 가 나온다 — 그 좌표로 만든
    지시(`--repo B --slot 7`·board 렌즈·remedy 안내)는 즉시 조인 실패한다(리뷰 F-002). 장부가
    그 세션을 명의로 가진 행을 들고 있으면 **행의 repo 와 행 번호**가 좌표이고, 그런 행이 없을
    때만 pool 명명 규약(키 분해)이 유일한 진실이다.

    좌표가 유일하지 않으면(행 repo 가 갈림·번호가 갈림) 해당 축은 `None` — 지어내지 않는다.
    """
    if not isinstance(session, str) or not session:
        return None, None
    rows = [row for row in _lease_dict_rows(leases_file) if row.get("session") == session]
    return identity_args._identity_coordinates(session, rows)


def slot_identity_resolution(slot: object, leases_file: Path) -> identity_args.SlotIdentityResolution:
    """슬롯 경로 값 → 슬롯 정체성 + 판정 사유. **장부 행 session 이 1순위**, pool 규약이 2순위.

    해소 순서:
      1. 장부에서 그 slot 값을 가진 행을 찾아(표기 정규화 후 대조) 그 행의 `session` 이 슬롯 키
         형식이면 **그 값이 정체성**이다 — 경로 basename 을 파싱하지 않는다. 경로에 이름이 없는
         슬롯(`slot="."`)도 이 층에서 해소된다.
      2. 장부가 그 슬롯에 슬롯 축 정체성을 주지 않으면(미등록 · task 소유 session · 무소유 빈
         session · `<host>-<pid>` 임시 명명) pool 명명 규약대로 **경로의 마지막 마디**를 키로
         쓴다. 슬롯 상태 앵커는 *슬롯의* 속성이라 대여자(session)가 바뀌어도 같은 키여야 한다 —
         이 층이 없으면 재바인딩마다 pm_state 연속성이 끊긴다.
      3. 둘 다 없으면 미해소 — 왜 못 했는지(부재/손상/행 부재)를 `status` 로 나른다.

    같은 slot 값의 행이 **서로 다른 슬롯 키 session** 을 주장하면 `SLOT_IDENTITY_CONFLICT` 다 —
    2층으로 조용히 내려가 경로 마디를 키로 쓰면 판정 불능이 판정으로 위장된다(리뷰 F-004). 읽기
    표시가 쓸 수 있게 규약 값은 `fallback` 으로 함께 나르되 `identity` 는 비운다.
    """
    if not isinstance(slot, str) or not slot.strip():
        return identity_args.SlotIdentityResolution(identity_args.SLOT_IDENTITY_NO_SLOT, None, "슬롯 값이 비었다")
    value = identity_args.normalize_slot_value(slot)
    ledger_status, all_rows = _read_lease_rows_status(leases_file)
    rows = [row for row in all_rows if identity_args.normalize_slot_value(row.get("slot")) == value]
    name = identity_args.slot_path_name(value)
    fallback = None
    if identity_args.is_slot_key(name):
        repo, number = identity_args._identity_coordinates(name, rows)
        fallback = identity_args.SlotIdentity(key=name, slot=slot, source=identity_args.IDENTITY_FROM_SLOT_PATH,
                                repo=repo, number=number)
    sessions = {row.get("session") for row in rows if identity_args.is_slot_key(row.get("session"))}
    if len(sessions) > 1:
        listing = ", ".join(sorted(repr(s) for s in sessions))
        return identity_args.SlotIdentityResolution(
            identity_args.SLOT_IDENTITY_CONFLICT, None,
            f"슬롯 {slot!r} 에 session 이 서로 다른 장부 행이 {len(sessions)}개({listing}) — "
            f"장부 모순이라 임의 선택하지 않는다",
            fallback=fallback)
    if len(sessions) == 1:
        session = str(next(iter(sessions)))
        repo, number = identity_args._identity_coordinates(session, rows)
        return identity_args.SlotIdentityResolution(
            identity_args.SLOT_IDENTITY_OK,
            identity_args.SlotIdentity(key=session, slot=slot, source=identity_args.IDENTITY_FROM_LEDGER_SESSION,
                         session=session, repo=repo, number=number))
    if fallback is not None:
        return identity_args.SlotIdentityResolution(identity_args.SLOT_IDENTITY_OK, fallback)
    if ledger_status != identity_args.LEDGER_OK:
        return identity_args.SlotIdentityResolution(
            ledger_status, None,
            f"장부를 읽을 수 없고({leases_file}) 경로 {slot!r} 에도 슬롯 키 마디가 없다",
            fallback=None)
    return identity_args.SlotIdentityResolution(
        identity_args.SLOT_IDENTITY_ROW_ABSENT, None,
        f"장부에 슬롯 {slot!r} 의 정체성 행이 없고 경로에도 슬롯 키 마디가 없다")


def resolve_slot_identity(slot: object, leases_file: Path) -> "identity_args.SlotIdentity | None":
    """`slot_identity_resolution` 의 관용 wrapper — 정체성 또는 `None`.

    **읽기/표시 호출부 전용**이다. 장부 모순(`SLOT_IDENTITY_CONFLICT`)에서는 종전대로 pool 명명
    규약 폴백을 내어 표시 연속성을 지키지만, **쓰기 진입은 `slot_identity_resolution` 을 직접
    소비**해 모순을 fail-loud 로 번역해야 한다(리뷰 F-004 — 읽기 관용/쓰기 엄격).
    """
    result = slot_identity_resolution(slot, leases_file)
    return result.identity if result.identity is not None else result.fallback


def _lease_dict_rows(leases_file: Path) -> list[dict]:
    """장부 `leases` 배열의 dict 행만 (해소 원천·조회 전용). 부재/손상 → 빈 리스트(fail-soft)."""
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return []
    return [r for r in rows if isinstance(r, dict)]


def resolve_task_workspace(identity: identity_args.Identity, leases_file: Path) -> identity_args.Workspace:
    """task 보유 집합과 명시 repo/slot 좌표를 조인한다. readonly 참조는 역할을 보존한다.

    실행 소비는 반환값의 readonly를 검사한다. 장부 부재·손상·중복 모순은 경로 추측 없이
    거부하며, 선택 없는 다중 슬롯은 기존 --repo/--slot 명시를 요구한다.
    """
    task = identity.task
    status, rows = _read_lease_rows_status(leases_file)
    if status != identity_args.LEDGER_OK:
        raise identity_args.WorkspaceResolutionError(f"task {task!r} 작업공간 장부를 읽을 수 없다: {leases_file} ({status})")
    active = [r for r in rows if r.get("state", "leased") == "leased"]
    held = [r for r in active if r.get("session") == task]
    if identity.kind == "slot":
        candidates = [r for r in active if r.get("repo") == identity.repo
                      and identity.slot in identity_args._row_slot_numbers(identity.repo, r.get("slot"), r.get("session"))]
    elif identity.kind == "repo":
        candidates = [r for r in held if r.get("repo") == identity.repo]
    else:
        candidates = held
    if not candidates:
        raise identity_args.WorkspaceResolutionError(
            f"선택 작업공간은 task {task!r} 보유가 아니거나 보유 작업공간이 없다. "
            f"기존 leased 슬롯의 --repo <repo> --slot <N>을 지정하거나 {_runtime_skill_entry('pm-env')} alloc으로 먼저 대여하라."
        )
    if len(candidates) != 1:
        raise identity_args.WorkspaceResolutionError(
            f"task {task!r} 작업공간 {len(candidates)}개가 해소돼 모호하다. "
            "암묵 선택 없이 --repo <repo> --slot <N>으로 기존 슬롯을 명시하라."
        )
    row = candidates[0]
    slot = identity_args.normalize_slot_value(row.get("slot"))
    repo = row.get("repo")
    role = row.get("role", "work")
    if (not isinstance(row.get("slot"), str) or not slot
            or not isinstance(repo, str) or not repo
            or role not in ("work", identity_args._LEASE_ROLE_READONLY)):
        raise identity_args.WorkspaceResolutionError(f"task {task!r} 작업공간의 경로·repo·role을 해소할 수 없다: {row}")
    # 같은 경로를 다른 repo/소유/상태로 주장하는 행도 첫 행으로 덮지 않는다.
    if sum(identity_args.normalize_slot_value(r.get("slot")) == slot for r in rows) != 1:
        raise identity_args.WorkspaceResolutionError(f"작업공간 {slot}의 장부 행이 중복돼 소유·좌표를 확정할 수 없다")
    numbers = identity_args._row_slot_numbers(repo, row.get("slot"), row.get("session"))
    if identity.kind == "slot" and numbers != {identity.slot}:
        raise identity_args.WorkspaceResolutionError(f"작업공간 {slot}의 repo/slot 번호가 서로 일치하지 않는다")
    readonly = role == identity_args._LEASE_ROLE_READONLY
    if not readonly and row.get("session") != task:
        raise identity_args.WorkspaceResolutionError(f"작업공간 {slot}은 task {task!r} 보유가 아니다, 소유검사 거부")
    return identity_args.Workspace(slot=slot, repo=repo, session=row.get("session") or None,
                     test_cmd=row.get("test_cmd"), readonly=readonly)
