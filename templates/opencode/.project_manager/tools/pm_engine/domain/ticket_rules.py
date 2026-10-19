"""티켓 상태 디렉터리·ID 문법·touches·frontmatter 문법 (층: domain).

파일·프로세스에 닿지 않는다. 티켓 파일 읽기는 `pm_engine.infra.ticket_store` 가 한다.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


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
