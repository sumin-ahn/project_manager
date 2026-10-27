"""Home별 부트스트랩 이력과 활성 세션 조회."""
from __future__ import annotations
from pm_engine.infra import lease_store
import json
from pm_engine.infra import fs, pm_paths
import re
from pathlib import Path
from pm_engine.domain import log_entries
from pm_engine.domain import bootstrap_rules


def parse_log_last_entry(log_text: str) -> dict[str, str] | None:
    """log/current.md 에서 마지막 ## 항목의 date/type/title 을 파싱한다.

    log/current.md 포맷: `## [YYYY-MM-DD] type | title`

    반환: {"date": "...", "type": "...", "title": "..."} 또는 None.
    """
    pattern = re.compile(
        "^## \\[(\\d{4}-\\d{2}-\\d{2})\\]\\s+(\\S+)\\s+\\|\\s+(.+)$", re.MULTILINE
    )
    matches = list(pattern.finditer(log_text))
    if not matches:
        return None
    last = matches[-1]
    return {
        "date": last.group(1),
        "type": last.group(2),
        "title": last.group(3).strip(),
    }


def extract_last_log_entry_body(log_text: str) -> str | None:
    """log_entries로 분할한 마지막 로그 본문을 반환한다. 항목 부재는 None, 분할 예외는 전파."""
    _preamble, entries = log_entries.split_entries(log_text)
    if not entries:
        return None
    return entries[-1][1].rstrip()


def _session_slot_number(
    home, bound_session: str, leases_file: Path | None = None
) -> int | None:
    """세션 명의가 점유한 슬롯 번호 — **장부 행 값**(`lease_store.session_coordinates`).

    장부에 그 세션 명의 행이 없으면 pool 명명 규약(키 분해)으로 폴백한다(그 형상에선 이름이
    유일한 진실).
    """
    if leases_file is None:
        leases_file = pm_paths.leases_file(home)
    _repo, number = lease_store.session_coordinates(bound_session, leases_file)
    return number


def _session_owns_untagged(
    home, bound_session: str | None, leases_file: Path | None = None
) -> bool:
    """무태그 handoff entry 가 이 세션 소유인지 판정한다 — slot-1 만 True.

    무태그 entry = 태그 도입 이전 로그 → slot-1 귀속(연속성 보존·제로 마이그레이션).
    **slot-2+ 는 무태그를 무시**하고 자기 태그 entry 만 센다(핵심 회귀 가드). 슬롯을 하나만
    쓰는 홈도 그 홈 자신이 1번 슬롯 행이라 같은 규칙 하나로 판정된다 — 별도 특례가 없다.
    **장부가 그 세션에 준 슬롯 번호가 1**이면 True, 그 외(slot-2+·번호 미해소·정체성 미해소)면
    False.

    번호를 세션 **이름의 말단**에서 뜯지 않는 이유: 행의 repo/경로와 어긋난 session
    (`repo=A · slot=work/A_1 · session=B_7`)에서 이름은 7 을 주지만 그 세션이 실제로 들고 있는
    슬롯은 A 의 1번이다(리뷰 F-002 와 같은 축). 장부에 행이 없으면 이름이 유일한 진실이라 종전
    파싱과 같은 값이 나온다.

    **task 세션(`task:<name>`)은 항상 False** — task 는 자기 태그(`(task:<name>)`) entry 만
    소유하고 태그-도입 이전 무태그 legacy(slot-1 계보)는 자기 것이 아니다. sentinel 판별을
    번호 판정보다 **앞에** 둔다: task 명이 우연히 `_1` 로 끝나면(`task:foo_1`) 번호 판정이
    slot-1 로 오판(True)하기 때문(회귀 가드)."""
    if bound_session is None:
        return False
    if bound_session.startswith(bootstrap_rules._TASK_TAG_PREFIX):
        return False
    return _session_slot_number(home, bound_session, leases_file) == 1


def parse_last_handoff_session_num(
    home, log_text: str | None, *, bound_session: str | None = None
) -> int | None:
    """log/current.md 에서 **자기 슬롯** handoff entry 제목의 최고 `PM N차` N 을 반환한다.

    handoff type entry 로 한정(complete/fix/note 는 제목 `PM N차` 부재/불규칙 — 오파싱 차단).
    `bound_session`(`<repo>_<N>`)이 **양성 해소**되면 자기 슬롯 태그 entry 만 필터해 그 중 최고차
    (append-only 로그라 max=최신)를 취한다 — 전역 max 가 아니다("두 슬롯 같은 N차" 해소·slot-2+ 는
    무태그/타 슬롯 무시). **bound 가 None(정체성 미해소)이면 전역 tag-agnostic
    파싱**으로 폴백한다: fresh clone(lease 부재)에 tracked 로그가 태그를 가져도
    log-derived 차수를 잃지 않게(user 연속성 surface 와 동일 원리 — 양성 슬롯일 때만
    필터). handoff entry 부재·log 부재 → None, 분할 예외는 전파.
    """
    if not log_text:
        return None
    owns_untagged = _session_owns_untagged(home, bound_session)
    nums: list[int] = []
    for m in bootstrap_rules._LOG_HANDOFF_HEADER_RE.finditer(log_text):
        if bound_session is None or bootstrap_rules._handoff_entry_owned(
            m, bound_session, owns_untagged
        ):
            nums.append(int(m.group("num")))
    if not nums:
        return None
    return max(nums)


def extract_slot_handoff_entry(
    home, log_text: str | None, *, bound_session: str | None = None
) -> dict[str, str] | None:
    """자기 슬롯의 마지막 handoff 항목과 본문을 반환한다. log_entries와 공통 소유 규칙을 사용한다. 항목 부재는 None, 분할 예외는 전파."""
    if not log_text:
        return None
    _preamble, entries = log_entries.split_entries(log_text)
    owns_untagged = _session_owns_untagged(home, bound_session)
    selected: str | None = None
    for _date, entry_text in entries:
        m = bootstrap_rules._LOG_HANDOFF_HEADER_RE.search(entry_text)
        if m is None:
            continue
        if bound_session is None or bootstrap_rules._handoff_entry_owned(
            m, bound_session, owns_untagged
        ):
            selected = entry_text
    if selected is None:
        return None
    entry = parse_log_last_entry(selected)
    if entry is None:
        return None
    entry = dict(entry)
    entry["body"] = selected.rstrip()
    return entry


def last_handoff_header_line(
    home, log_text: str | None, *, bound_session: str | None = None
) -> str | None:
    """**자기 슬롯** 마지막 handoff entry 의 헤더 줄 전체를 반환한다 (user 연속성 pickaxe needle).

    `parse_last_handoff_session_num` 과 같은 grammar·같은 소유 sink(`_handoff_entry_owned`)로
    헤더 줄을 잡는다 — 그 줄을 담은 commit 을 `git log -S<line>`(pickaxe)로 찾아 author email 을
    얻기 위함. `bound_session`(`<repo>_<N>`·미해소면 None)으로 **자기 슬롯 태그 entry 만 필터**한다
    ("타 슬롯 최소 유입") — slot-2 부트스트랩이 전역 마지막(=slot-1) handoff 로
    "직전 작성자" 를 오판정하지 않게. 무태그=slot-1 귀속. 부재/파싱 실패 → None.
    """
    if not log_text:
        return None
    owns_untagged = _session_owns_untagged(home, bound_session)
    last: re.Match | None = None
    for m in bootstrap_rules._LOG_HANDOFF_HEADER_RE.finditer(log_text):
        if bound_session is not None and (
            not bootstrap_rules._handoff_entry_owned(m, bound_session, owns_untagged)
        ):
            continue
        last = m
    if last is None:
        return None
    return last.group("line")


def _read_leased_sessions_slots(home) -> list[tuple[str, str]] | None:
    """lease 장부에서 leased 엔트리의 `(session, slot)` 목록을 직접 read 한다 (touches 격리).

    session 은 엔트리 `session` 또는 슬롯(`work/<repo>_<N>`→`<repo>_<N>`)에서 유도(대시보드
    키와 동형). worktree_pool 미import(`lease_store.repo_slot_numbers` 동형·stdlib json). **장부 부재/
    깨짐/스키마 불일치 → None**("활성 슬롯을 알 수 없음"·MF-1 필터가 no-op 폴백에 씀); 정상
    read 인데 leased 0개면 `[]`. `_lease_others`(폴백 dump)·`_active_leased_sessions`(교집합
    필터) 가 공유한다.
    """
    leases_file = pm_paths.leases_file(home)
    if not leases_file.exists():
        return None
    try:
        data = json.loads(fs.read_text_shared(leases_file, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("leases"), list):
        return None
    pairs: list[tuple[str, str]] = []
    for row in data["leases"]:
        if not isinstance(row, dict) or row.get("state", "leased") != "leased":
            continue
        slot = str(row.get("slot") or "")
        session = row.get("session") or (
            slot[len("work/") :] if slot.startswith("work/") else slot
        )
        if session:
            pairs.append((session, slot or "(미상)"))
    return pairs


def _active_leased_sessions(home) -> set[str] | None:
    """활성(leased) 슬롯 세션 키 집합 (MF-1 대시보드 교집합 필터·codex).

    장부 read 성공 → leased 세션 집합. 장부 부재/불가(None) → **None**(활성 여부 판정
    불가 → 필터 no-op·현행 표시 보존). `--done` release 로 idle 된 슬롯을 이 집합이 배제하므로
    대시보드에 stale 섹션이 남아도 부트스트랩 dump 에서 사라진다 — release 누락/크래시에도
    idle 노출 0(옵션 b·display-robust).
    """
    pairs = _read_leased_sessions_slots(home)
    return {s for s, _ in pairs} if pairs is not None else None


def _lease_others(home, task_name) -> list[dict]:
    """자기 제외 leased 슬롯 목록을 lease 장부에서 직접 read 한다 (대시보드 부재 폴백).

    `wiki/log/dashboard.md` 부재 시 폴백 — leased 엔트리에서 자기 task 제외분을
    `[{"session","slot"}]` 로 반환한다. 장부 부재/깨짐/leased 0개는 빈 목록.
    """
    pairs = _read_leased_sessions_slots(home)
    if not pairs:
        return []
    bound = task_name
    return [{"session": s, "slot": slot} for s, slot in pairs if s != bound]
