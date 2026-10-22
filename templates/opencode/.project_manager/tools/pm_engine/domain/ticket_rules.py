"""티켓 상태 디렉터리·ID 문법·touches·frontmatter 문법 (층: domain).

파일·프로세스에 닿지 않는다. 티켓 파일 읽기는 `pm_engine.infra.ticket_store` 가 한다.
"""
from __future__ import annotations

from collections.abc import Sequence

import re
from pathlib import Path
from typing import Any

from typing import NamedTuple


# 티켓 상태 디렉토리 — 이 튜플이 조회·렌더·집계·원격 race 판정의 단일 진실이다(board.py 안
# 소비처는 전부 여기를 순회한다). `discarded` = **처분 종결**(병합·폐기): 구현이 끝나서 닫힌
# `done` 과 달리 "이 티켓은 다른 티켓으로 흡수됐거나 폐기됐다"는 종결이라, 둘을 같은 디렉토리에
# 담으면 done 이 "작업 완료" 를 뜻하지 못한다.
STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done", "discarded")

# 종결 상태 — 더 이상 진행되지 않는 두 종류(구현 완료 · 처분). 활성 뷰·task 소진 게이트가
# 이 집합을 뺀다. "done 하나만 종결" 을 가정한 옛 판정은 전부 이 상수를 소비해야 한다.
TERMINAL_STATUS_DIRS: tuple[str, ...] = ("done", "discarded")

# 활성(비-종결) 상태 — 기본 `list` 뷰와 처분(discard) 출발 상태가 같은 유도를 공유한다.
ACTIVE_STATUS_DIRS: tuple[str, ...] = tuple(
    status for status in STATUS_DIRS if status not in TERMINAL_STATUS_DIRS)

# 처분 종류(frontmatter `disposition`) — 병합(다른 티켓으로 흡수) · 폐기(취소·대체).
DISPOSITION_MERGED = "merged"
DISPOSITION_DROPPED = "dropped"
DISPOSITION_KINDS: tuple[str, ...] = (DISPOSITION_MERGED, DISPOSITION_DROPPED)

# Ideas have a simpler lifecycle than tickets — no claim/complete middle
# states, just `open → promoted|killed`.
IDEA_STATUS_DIRS: tuple[str, ...] = ("open", "promoted", "killed")

# 티켓 ID prefix 추출 — `_next_id` ID 발행 규칙의 *정확한 역*:
#   prefixed = `T-{prefix}-{NNN}` (`_next_id` line 1011·prefix 는 리터럴 삽입·끝 -NNN 은 숫자),
#   legacy   = `T-{NNNN}`        (`_next_id` line 1013·하이픈 1개·prefix 없음).
# prefix 문법은 **등록/검증측(`pm_config._REPO_NAME_RE`·`^[A-Za-z0-9][A-Za-z0-9_-]*$`)과 정합**한다
# — repo add·init `--prefix` 가 그 패턴으로 prefix 를 검증·등록하므로 소비측도 같은 grammar 여야
# 한다(소비 grammar 가 등록 grammar 보다 좁으면 `123` 같은 순수-숫자
# prefix 가 등록은 되는데 소비측에서 legacy 로 오인돼 `_ticket_prefix`/wikilink/bootstrap 이 prefix
# 로 인식 못 함). 영숫자로 시작(leading `-` 배제)·이후 영숫자/`_`/`-` — 그래서 `P0`(숫자 포함)·
# `service-a`(family-scope·하이픈)·`123`(순수 숫자) prefix 모두 발행·해소된다(`T-P0-001`·
# `T-service-a-001`·`T-123-001`). 역파서는 끝의 `-NNN`(숫자) 한 마디만 떼고 나머지를 prefix 로 잡는다.
# legacy 와의 구분은 **구조적**으로 유지된다(prefix grammar 가 순수 숫자를 포함해도): full-ID
# regex `^T-(prefix)-\d+$` 가 *내부 하이픈*(prefix-NNN 2세그먼트)을 요구하므로 `T-0164`(하이픈
# 1개)는 매칭 안 됨 → None(legacy), `T-123-001`(하이픈 2개)는 prefix `123` 으로 갈린다. 발행측이
# legacy 를 prefix 없는 `T-NNNN` 단일 하이픈으로만 내므로 이 하이픈-수 경계가 둘을 정확히 가른다.
# ID grammar 의 단일 진실 — prefix 마디 본체. `_TICKET_PREFIX_RE` + prefixed-ID 를 매칭하는 다른
# 파서(`_ticket_id_from_filename`·wikilink lint·bootstrap `_TICKET_ID`)가 *전부* 이 한 조각(또는
# 동형 grammar)을 쓴다 — grammar drift 방지(round-3 클래스: 한 곳 고치면 같은 가정의 다른
# 파서가 어긋남). `P0`(숫자)·`service-a`(하이픈)·`x_y`(언더스코어)·`123`(순수 숫자) prefix 포섭.
_TICKET_PREFIX_BODY = r"[A-Za-z0-9][A-Za-z0-9_-]*"

# prefixed | legacy 둘 다 매칭하는 ID 본체 (anchor 없음 — 호출측이 ^…$·\b 등으로 감싼다).
# 파일명/wikilink 파서가 공유한다(자체 `[A-Za-z]+` regex 두지 말 것 — `P0`/`service-a` 누락).
_TICKET_ID_BODY = rf"T-(?:{_TICKET_PREFIX_BODY}-\d+|\d+)"

# 명시 NEW-ID(`reid`) 형식 sanity 용 full-match 앵커 — 발행 문법(`_TICKET_ID_BODY`)을 그대로
# 감싸 `T-NNNN`·`T-<pfx>-NNN` 둘 다 받고 그 외(빈 문자열·`foo`·`T-`·`X-1`)는 거른다. prefix 마디는
# 소비측 grammar(`_TICKET_PREFIX_BODY`·대문자/하이픈 legacy 포함)라 **자유 입력**이다 — 기존
# 네임스페이스로의 relabel 을 위해 좁은 `_validate_prefix`(소문자 카테고리)를 적용하지 않는다.
# 앵커는 `\A…\Z` — `$` 는 Python 에서 trailing newline 앞에서도 매치해 `T-0250\n`
# 같은 개행-포함 ID 를 통과시킨다(파일명/frontmatter/참조에 깨진 ID 기록 위험). `\Z` 는 문자열 끝에서만.
_FULL_TICKET_ID_RE = re.compile(rf"\A{_TICKET_ID_BODY}\Z")


def _is_valid_ticket_id(tid: str) -> bool:
    """`tid` 가 발행 규칙 ID 문법(`T-NNNN`·`T-<pfx>-NNN`)에 완전 일치하는지 (reid NEW-ID sanity)."""
    return bool(_FULL_TICKET_ID_RE.match(tid))


def _fold_lookup(prefix: str, pool: set[str]) -> str | None:
    """`pool` 에서 `prefix` 와 case-insensitive fold 로 같은 항목을 찾아 반환·없으면 None.

    prefix 동일성은 소문자 fold 비교다 — 등록/발행된 canonical case 를 대소문자 무관하게 되찾는다
    (`aaa` 입력 → 등록 `AAA` 반환). 오염된 입력에서도 호출 결과 자체는 프로세스 hash seed에
    흔들리지 않도록 exact case를 tie-break 키로 정렬한다. 신규-prefix 승인 게이트는 이 helper의
    단일값 선택에 기대지 않고, fold 변형이 둘 이상이면 출처와 함께 fail-loud한다.
    """
    fold = prefix.lower()
    return next((p for p in sorted(pool, key=lambda value: (value.lower(), value))
                 if p.lower() == fold), None)


def _normalized_touches(raw: object) -> list[str] | None:
    """frontmatter `touches` → 경로 목록(**리스트[str] 강제**) · 형식이 불명이면 None.

    선언이 스칼라 문자열이면(`touches: src/pay.py` — YAML 로는 유효하다) `list(...)` 가 **문자
    목록**을 만든다: `-k "a or c or p or …"` 같은 오염된 회귀 스코프가 서고, 그 실행의 rc0 은
    아무 것도 검증하지 않은 '가짜 green' 이다. 비문자열 원소(숫자·매핑)는 그 뒤 경로 연산에서
    크래시한다.

    그래서 반환은 셋 중 하나다:
      · `list[str]` — 정상 선언(빈 문자열 원소는 걷는다).
      · `[]` — 선언 없음(`None`·빈 목록).
      · `None` — **형식 불명**. 호출부는 강등을 취소하고 FULL 로 남긴다(강등 fail-closed 규칙과
        동형 — 스코프를 확정하지 못한 채 좁히면 안 된다).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    normalized: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if text:
            normalized.append(text)
    return normalized


def _parse_ticket_text(text: str, source: Path | str) -> tuple[dict[str, Any], str]:
    """`load_ticket`의 실제 frontmatter 문법으로 문자열을 파싱한다.

    파일 I/O와 파서를 분리해, 쓰기 전 변환 결과도 런타임 소비자와 완전히 같은 문법으로
    검증할 수 있게 한다. `source`는 오류 메시지의 파일 식별자일 뿐 파싱에는 관여하지 않는다.
    """
    if not text.startswith("---\n"):
        raise ValueError(f"missing frontmatter: {source}")
    # Split on the FIRST closing '---' after the opener
    after_open = text[4:]
    end = after_open.find("\n---\n")
    if end == -1:
        raise ValueError(f"unterminated frontmatter: {source}")
    fm_text = after_open[:end]
    body = after_open[end + 5:]
    # PyYAML 은 파싱할 때 읽는다. 이 모듈을 import 하는 도구 중 훅 인터프리터(PyYAML 없음)가
    # 적재하는 것(위임 채널 가드의 pm_delegate·Stop 훅의 pm_log)이 모듈 적재에서 멈추지 않게 한다.
    import yaml

    fm = yaml.safe_load(fm_text) or {}
    return fm, body


def _ticket_id_from_filename(filename: str) -> str | None:
    """ticket 파일명에서 canonical ID 추출. 없으면 None.

    prefixed(`T-PAY-001-foo.md` → `T-PAY-001`·`T-service-a-001-…`)도 추출 — 발행측
    `_next_id` 가 prefixed 파일을 만드므로. grammar 는 `_TICKET_ID_BODY` 공유.
    """
    # ID 뒤에는 filename slug 구분자(`-`) 또는 정확한 `.md` 종료만 올 수 있다. 이 경계가
    # 없으면 숫자로 시작하는 legacy slug(`T-0683-3하네스.md`)를 prefixed ID
    # `T-0683-3`으로 탐욕 소비한다. 공용 ID/prefix 문법은 그대로 두고 filename 소비처에서만
    # backtrack을 허용한다.
    m = re.match(rf"({_TICKET_ID_BODY})(?=-|\.md\Z)", filename)
    return m.group(1) if m else None


def _touch_notation(item: str) -> str | None:
    """접힌 경로를 접두 비교용 표기로 마무리한다(빈 항목은 None)."""
    value = str(item).strip().rstrip("/")
    return value or None


def _touch_paths_overlap(left: str, right: str) -> bool:
    """디렉토리 접두를 **양방향**으로 본다 — `tests/` 는 `tests/x.py` 를 덮고 그 역도 겹침이다."""
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


_TICKET_PREFIX_RE = re.compile(
    rf"^T-(?P<prefix>{_TICKET_PREFIX_BODY})-\d+$")


def _ticket_prefix(tid: str) -> str | None:
    """티켓 ID 에서 네임스페이스 prefix 추출. legacy `T-NNNN`(prefix 없음) → None.

    `_next_id` 의 ID 발행 규칙의 역이다: prefixed = `T-<PREFIX>-NNN`, legacy = `T-NNNN`.
    PREFIX 문법은 등록/검증측(`pm_config._REPO_NAME_RE`·`[A-Za-z0-9][A-Za-z0-9_-]*`)과 정합이라
    숫자(`P0`)·하이픈(`service-a`)·순수 숫자(`123`)를 포함할 수 있고 그런 ID(`T-P0-001`·
    `T-service-a-001`·`T-123-001`)도 해소된다. legacy 4자리 숫자 ID는 full-ID regex
    가 *내부 하이픈*(prefix-NNN)을 요구하는데 하이픈이 1개뿐이라 매칭 안 됨 → None(구조적 구분).
    """
    if not tid:
        return None
    m = _TICKET_PREFIX_RE.match(tid)
    return m.group("prefix") if m else None


# legacy `T-NNNN` 의 순번 추출 — prefixed(`T-<p>-NNN`)는 마지막 하이픈 뒤 숫자로 뗀다.
_LEGACY_TICKET_ID_RE = re.compile(r"^T-(\d+)$")


def _ticket_id_number(tid: str) -> int | None:
    """티켓 ID 의 순번(정수). `T-NNNN`→NNNN / `T-<p>-NNN`→NNN. 미매치→None (prefix list 범위용)."""
    if _TICKET_PREFIX_RE.match(tid):        # prefixed — 순번 = 마지막 `-` 뒤 숫자 마디.
        return int(tid.rsplit("-", 1)[1])
    m = _LEGACY_TICKET_ID_RE.match(tid)
    return int(m.group(1)) if m else None


# 명시 `--prefix` 입력측 sanity — 소비측 grammar(`_TICKET_PREFIX_BODY`·
# 대문자/하이픈 포함·legacy ID 역파싱용)와 **의도적으로 다르다**. 소비측은 기존 발행분(대문자·
# 하이픈 acronym)을 *해소*해야 하므로 넓지만, *새* 카테고리 prefix 입력은 좁게 권장형식으로 못박아
# (작업 카테고리 = 짧은 소문자·언더스코어). 유도/등록된 legacy prefix 는 검증
# 안 한다(cmd_new 는 명시 override 만·cmd_init 은 명시 --prefix 만 검사).
_PREFIX_RESERVED: frozenset[str] = frozenset({"none"})   # `none`=무prefix(T-NNNN) 1급 인자·실 prefix 예약 (case-insensitive)


_PREFIX_FORMAT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*$")    # 형식 sanity (영숫자 시작·이후 영숫자/`_`·대소문자 허용)


def _validate_prefix(prefix: str) -> str | None:
    """명시 `--prefix` 형식 sanity — 위반 사유 메시지 반환, 정상이면 None.

    - 예약어(`none`·대소문자 무관): 무prefix(`T-NNNN`) 네임스페이스의 1급 인자로 예약(rename/
      merge 의 from/to/into)이라 실 prefix 로 등록/사용 금지. prefix 동일성이
      case-insensitive fold 이므로 `NONE`/`None` 도 같은 예약어로 fold 돼 거부된다.
    - 형식 `[A-Za-z0-9][A-Za-z0-9_]*`(첫 글자 영숫자·이후 영숫자/`_`): **대소문자 모두 허용**
      (prefix 동일성은 case-insensitive fold 이되 canonical case 는 보존). 하이픈·
      공백·특수문자·빈 문자열은 fail-loud(하이픈은 ID 구분자와 충돌 — 이번 변경 범위 아님).
    """
    if prefix.lower() in _PREFIX_RESERVED:
        return (f"prefix {prefix!r} 은 예약어 — 무prefix(T-NNNN) 네임스페이스의 1급 인자다"
                "(rename/merge·`none` 은 case-insensitive). 실 prefix 로 쓸 수 없다.")
    if not _PREFIX_FORMAT_RE.match(prefix):
        return (f"prefix {prefix!r} 형식 위반 — 작업 카테고리는 "
                "`[A-Za-z0-9][A-Za-z0-9_]*`(첫 글자 영숫자·이후 영숫자/`_`·대소문자 허용·"
                "). 하이픈·특수문자·공백·빈 문자열은 금지"
                "(하이픈은 `T-{prefix}-NNN` ID 구분자와 충돌).")
    return None


def _fold_key(prefix: str | None) -> str | None:
    """prefix 의 네임스페이스 동일성 키 — 문자열은 소문자 fold, None(무prefix)은 그대로.

    prefix 동일성은 case-insensitive fold 이므로 `AAA`≡`aaa` 는 같은 키(`aaa`)로, legacy 무prefix
    (None)는 None 과만 같은 키다. rename/merge/delete 의 **source-측 매칭·collision 정규화**가 이
    키로 비교해 `T-AAA-*` 를 `aaa` source 로 잡고, `T-AAA-001`/`T-aaa-001` 오염 공존을 collision 으로
    잡는다(canonical case 보존과 별개 — 여긴 *비교* 층이라 소문자 fold 만).
    """
    return prefix.lower() if prefix is not None else None


# prefix rename/merge가 소비하는 순수·hermetic 프리미티브. old→new ID 맵을 받아
# 대상 파일(board tickets 본문·wiki/·log/)의 참조를 **토큰단위 정확치환**한다 — frontmatter
# `depends_on`·`[[wikilink]]`·bare inline·산문 임베드(`**A·B(...)**`)가 전부
# 리터럴 ID 토큰이라 한 rewriter 로 전부 커버된다.
#
# 경계 규칙(부분매치 방지): 매치된 old ID 양옆이 식별자 문자면 치환하지
# 않는다 — (1) 뒤 char ∈ `[A-Za-z0-9_-]` → `티켓 ID` 이 `티켓 ID1`(뒤 숫자)·`티켓 ID-2`(뒤 하이픈)·
# `티켓 ID_legacy`(뒤 언더스코어)·`티켓 IDabc`(뒤 알파벳)에 안 걸림, (2) 앞 char ∈ `[A-Za-z0-9_-]`(lookbehind) →
# `foo티켓 ID`·`NO티켓 ID`(앞 식별자 문자)에 안 걸림. old ID 가 리터럴 `T-` 로 시작하는 것만으론
# 왼쪽 부분매치(`foo티켓 ID`)를 못 막으므로 lookbehind 로 왼쪽 경계를 명시한다(codex must-fix).
# 한글·괄호·공백 등 비-식별자 인접(`티켓 ID다음`·`(티켓 ID)`·`depends_on: 티켓 ID`)은 양옆이
# 경계라 정상 치환된다. 무prefix `T-NNNN`·prefix `T-<p>-NNN` 두 표기형 모두 map 키를 리터럴
# escape 해 자연 포섭한다.
_REWRITE_LEADING_BOUNDARY = r"(?<![A-Za-z0-9_-])"   # old ID 앞 char ∉ [A-Za-z0-9_-] (왼쪽 부분매치 방지)


_REWRITE_TRAILING_BOUNDARY = r"(?![A-Za-z0-9_-])"   # old ID 뒤 char ∉ [A-Za-z0-9_-] (오른쪽 부분매치 방지·`티켓 IDabc` 차단)


def _claimed_by_user(claimed_by: str | None) -> str | None:
    """`claimed_by`(`<user>/<pm-slot>`)에서 *user* 토큰 추출 — 슬롯-only/빈값은 None (codex sug).

    `claimed_by` 는 이제 `<user>/<slot>` 또는 구 슬롯-only(`<slot>`)다.
    user 추출은 **마지막 `/` 분리** 규칙(`rsplit('/', 1)[0]`) — slot 이 마지막 토큰이므로 user 에
    `/` 가 들어가도(이메일은 보통 없지만 안전) 정확히 분리한다. `/` 가 없으면(구 슬롯-only·user
    미상) None 을 반환해 (b) 매칭에서 graceful 제외한다.
    """
    if not claimed_by or "/" not in claimed_by:
        return None
    return claimed_by.rsplit("/", 1)[0] or None


def _created_by_user(created_by: str | None) -> str | None:
    """`created_by`(`<user>/<pm-slot>`·`<user>`·슬롯-only)에서 *user* 토큰 추출.

    `created_by` 는 `identity_tag` 산출(`<user>/<slot>`·session 미바인딩 시 user-only·user 미상
    시 슬롯-only) 또는 `migrate-identity` backfill(부재 → *순수 user*·line `_migrate_ticket_fm`)이다.
    `_ticket_owner` 의 2차 폴백 소유자(area_owner 미해소 시 항상-존재 소유)로 쓴다.

    `_claimed_by_user`(슬롯-only=None)와 갈리는 지점: **`/` 없는 값을 user 로 본다** — backfill 이
    부재 created_by 를 슬롯 없는 순수 user 로 채우므로(다중사용자 보드 migrate-identity 후 흔함) 그
    항상-존재 소유자를 살려야 유출을 없앤다.
    `<user>/<slot>` 은 마지막 `/` 분리로 user 를 뽑고, 빈/None 은 None(미상). 드물게 `/` 없는 슬롯-only
    created_by(user 미상 생성)를 user 로 오인할 수 있으나 — 그런 보드는 다중사용자 신호가 안 서면
    단일 사용자 degrade 라 무해하고, 서면 strict-exclude 라 안전하다(회귀 0).
    """
    if created_by is None:
        return None
    cb = str(created_by).strip()
    if not cb:
        return None
    if "/" in cb:
        return cb.rsplit("/", 1)[0] or None
    return cb


def _created_by_session(created_by: str | None) -> str | None:
    """`created_by`(`<user>/<session>`·`<user>`)에서 *session* 토큰 추출.

    session = `created_by` 의 `/` 뒤 마지막 토큰(task 이름). 세션 기본
    뷰(무인자 `list`)의 "내 스트림 open" 판정에 쓴다 — 현 세션(`session_name`)과
    이 값이 일치하는 open 이 내 스트림이다. 이전 task-prefix 판정을 대체한다(prefix 는 ID 라벨일
    뿐 스트림 판정 아님).

    `/` 없는 값은 세션 토큰이 없는 것으로 본다(None) — 이름 모양으로 세션을 추측하지 않는다.
    옛 슬롯 세션 토큰은 `migrate-identity --task <task>` 로 옮긴다.
    """
    if created_by is None:
        return None
    cb = str(created_by).strip()
    if not cb:
        return None
    if "/" in cb:
        return cb.rsplit("/", 1)[-1] or None
    return None


def _slot_matches(claimed_by: str, my_slot: str) -> bool:
    """`claimed_by`(`<user>/<session>` 또는 legacy 세션-only)의 세션 토큰이 `my_slot` 과 완전 일치하는가."""
    if not claimed_by:
        return False
    slot_token = claimed_by.rsplit("/", 1)[-1]
    return slot_token == my_slot


class _OwnershipVerdict(NamedTuple):
    """소유 대조 결과 — 판정(ok)과 **왜**(owner·session·unresolved)를 함께 나른다.

    `owner` = 티켓 `claimed_by` 원값(소유자 없으면 None) · `session` = 이 실행이 해소한 세션 ·
    `unresolved` = 세션을 해소하지 못해 소유를 증명할 수 없는 경우(=거부·"검사 skip" 폴백 없음).
    거부 문구는 호출 surface 마다 다르지만 판정은 이 하나다.
    """

    ok: bool
    owner: str | None
    session: str | None
    unresolved: bool


def _owner_session_remedy(claimed_by: str) -> str:
    """소유 task 를 다시 잡는 처방 문장 — 소유 토큰을 `--task` 로 그대로 싣는다(토큰을 분해하지 않는다)."""
    owner_token = claimed_by.rsplit("/", 1)[-1]
    return (f"소유 task 에서 `--task {owner_token}` 로 재실행하라(소유 표기가 옛 슬롯 이름이면 "
            "소유자가 먼저 `board.py migrate-identity --task <task>` 로 옮긴다)")


def _ownership_rejection(action: str, tid: str,
                         verdict: _OwnershipVerdict) -> str:
    """소유 불일치 거부 문구 — 소유자 · unclaim→claim 이동 경로 · takeover 를 함께 낸다.

    거부만 하고 끝내면 사람은 그 자리에서 할 수 있는 일이 없다(실사고에서 두 세션이 서로
    막힌 형상). 문구가 (1) 누구 소유인지 (2) 소유 세션이 살아 있을 때의 정상 이전
    (3) 소유 세션이 없을 때의 단일 우회(`unclaim --takeover`)를 모두 지목한다.
    """
    owner = verdict.owner or ""
    remedy = _owner_session_remedy(owner)
    head = (f"[중단] {action} {tid}: 세션 미해소 — 이 티켓은 `{owner}` 소유다"
            if verdict.unresolved else
            f"[중단] {action} {tid}: 이 티켓은 `{owner}` 소유다"
            f"(현재 세션 `{verdict.session}`)")
    lines = [head + "."]
    if verdict.unresolved:
        lines.append(f"  → {remedy}.")
    else:
        lines.append(f"  → 이동: {remedy} — `board.py unclaim {tid}` 후 이 "
                     f"세션에서 `board.py claim {tid} --task <task>`.")
    lines.append(f"  → 소유 세션이 없으면: `board.py unclaim {tid} --takeover "
                 "--reason <사유> --task <task>` (소유만 해제·사유는 티켓 본문과 board-git 에 남는다).")
    return "\n".join(lines)


# ID 스캔용 status 튜플 — STATUS_DIRS + draft 격리 디렉토리(`.drafts`). draft 도 이미
# 발행된 ID(파일명에 박제)이므로 스캔에서 빠지면 promote 전 다음 `new` 가 같은 번호를 재사용해
# 충돌한다. `next_numeric_id` 는 `base_dir / d` 로 순회하므로 디렉토리명 그대로 넣으면 된다.
_ID_SCAN_STATUSES: tuple[str, ...] = (*STATUS_DIRS, ".drafts")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9가-힣-]+", "-", text.lower()).strip("-")
    return s[:40].rstrip("-") or "ticket"      # slug 최대 길이(종전 기본값)


def _tag_values(fm: dict[str, Any]) -> list[str]:
    """frontmatter 의 tags 를 전부 str 로 캐스팅한 리스트.

    YAML 은 `tags: [2026, cleanup]` 의 `2026` 을 int 로 로드한다 — 이를 그대로
    `str.join` 하면 TypeError 로 크래시하고(cmd_list·마크다운 렌더), 문자열 `--tag`
    와 `in` 비교하면 조용히 매치 실패한다(필터). tags 를 표시·필터하는 전 호출부가
    이 str 캐스팅을 단일 지점에서 거치게 해 두 결함을 함께 없앤다. 문자열 태그는
    str→str 로 무변경(형식·매치 회귀 없음).
    """
    return [str(t) for t in (fm.get("tags") or [])]


def _table_cell(value: Any) -> str:
    """자유 문장을 markdown 표 셀 한 칸으로 안전하게 만든다 (빈 값은 `—`).

    처분 사유는 사람이 쓰는 자유 산문이라 개행·파이프가 들어올 수 있다 — 그대로 넣으면 표가
    깨져 board.md 렌더가 사람에게 잘못 읽힌다. 개행은 공백으로, 파이프는 이스케이프한다.
    """
    text = " ".join(str(value or "").split())
    return text.replace("|", "\\|") if text else "—"


TICKET_TIERS: frozenset[str] = frozenset({'pm-direct', 'normal', 'hard'})

class TierSignals(NamedTuple):
    """touches에서 기계적으로 확정 가능한 tier 보조 신호."""
    tool_modules: tuple[str, ...]
    shared_code: tuple[str, ...]
    docs_only: bool
    unresolved_directories: tuple[str, ...] = ()

    @property
    def h1(self) -> bool:
        """2개+ 모듈 또는 파일 수 미해소(상향 기본값)."""
        return len(self.tool_modules) >= 2 or bool(self.unresolved_directories)

class TouchFileExpansion(NamedTuple):
    """directory touches를 repo 소유 파일로 펼 결과와 미해소 선언."""
    paths: tuple[str, ...]
    unresolved_directories: tuple[str, ...]

def _canonical_tier_touch(value: str) -> str:
    """touches 경로를 repo-relative POSIX 표기로 접는다."""
    value = value.strip().replace('\\', '/')
    while value.startswith('./'):
        value = value[2:]
    return value.rstrip('/')

def _is_tool_test_file(path: str) -> bool:
    """tools/ 안의 테스트 파일을 모듈 수에서 제외한다."""
    parts = Path(path).parts
    name = parts[-1] if parts else ''
    return 'tests' in parts or name.startswith('test_') or name.endswith('_test.py')

def _tier_tool_module_touches(touches: Sequence[str]) -> tuple[str, ...]:
    prefix = '.project_manager/tools/'
    modules = {path for raw in touches if (path := _canonical_tier_touch(raw)).startswith(prefix) and path.endswith('.py') and (not _is_tool_test_file(path[len(prefix):]))}
    return tuple(sorted(modules))

_ENGINE_PACKAGE = 'pm_engine'

def _is_tier_document(path: str) -> bool:
    path = _canonical_tier_touch(path)
    if not path:
        return False
    parts = Path(path).parts
    name = parts[-1].upper()
    if name in {'README', 'README.MD', 'CHANGELOG', 'CHANGELOG.MD'}:
        return True
    if path == '.project_manager/wiki' or path.startswith('.project_manager/wiki/'):
        return True
    if 'skills' in parts and parts[-1] == 'SKILL.md':
        return True
    if name == 'AGENTS.MD':
        return True
    return 'agents' in parts and Path(path).suffix.lower() in {'.md', '.toml'}
