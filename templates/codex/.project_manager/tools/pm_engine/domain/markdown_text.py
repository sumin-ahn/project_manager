"""마크다운 본문 판독 (층: domain).

개행 표기 정규화, 이름으로 찾는 `## ` 절 본문 슬라이스, 코드 span·fence 제거를 소유한다.
"""
from __future__ import annotations

import re
from collections.abc import Callable


def _normalized_newlines(text: str) -> str:
    """개행 표기만 LF 로 접는다 — 표기 차이를 내용 변경으로 읽지 않기 위해서다."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _section_text(body: str, heading: str, *, exact: bool = False) -> str | None:
    """`<heading>` 절 본문(다음 `## ` 헤딩 직전까지) — 절이 없으면 None.

    **헤딩 탐색 전에** fenced 코드블록을 먼저 제거한다. 펜스를 무시하면 판정이 양방향으로
    틀어진다 — 코드블록 안에 인용된 `## 완료 조건` 이 절 시작으로 잡혀 실제 DoD 를 못 보거나,
    코드블록 안 `## …` 한 줄이 절 끝으로 잡혀 뒤쪽 미체크 항목이 통째로 빠진다. 제거 후 남는
    예시 체크박스도 함께 사라진다.

    DoD 판정과 설계 절 판정이 같은 슬라이서를 쓴다 — 절 경계 규칙이 두 벌이면 한쪽만 펜스를
    보거나 한쪽만 본문 전체를 스캔하는 절반 배선이 생긴다.

    `exact` 면 헤딩 줄이 그 이름 자체이거나 **괄호 부제만** 뒤에 붙어야 절 시작이다
    (`## 설계` · `## 설계 (DRAFT — 초안)`). 자유 접두 일치는 `## 설계` 같은 짧은 이름에서 다른
    절(`## 설계 검토 이력`)을 그 절로 오인해, 산문 한 줄만으로 "절이 있다"가 성립하는 우회를
    남긴다. 괄호 부제를 허용하는 이유는 그게 실재 표기이고(같은 절에 상태를 병기), 괄호 없이
    이어지는 말은 다른 절 제목이기 때문이다. 이름 뒤에 자유 부제가 붙는 절
    (`## 완료 조건 (Definition of Done)`)은 접두 일치가 정상이라 기본값은 종전(False)이다.
    """
    sections = _section_texts(body, heading, exact=exact)
    return sections[0] if sections else None


def _section_texts(body: str, heading: str, *, exact: bool = False) -> list[str]:
    """그 이름의 **모든** 절 본문 (없으면 빈 목록) — `_section_text` 의 전-절 형태.

    같은 이름의 절이 두 번 나오는 본문에서 첫 절만 보면, 앞에 빈(또는 전부 체크된) 절을 하나 두고
    뒤 절에 미체크 항목을 남기는 우회가 성립한다. 판정이 '있는 항목 전부'여야 하는 소비자
    (DoD 마감 검사)는 이 함수로 **합산**한다. 절 경계·펜스 제거 규칙은 `_section_text` 와 같은
    한 지점이다(두 벌이면 한쪽만 펜스를 보는 절반 배선이 생긴다).
    """
    lines = _FENCED_CODE_RE.sub("", body).splitlines()
    if exact:
        heading_re = re.compile(rf"{re.escape(heading)}(?:\s*\(.*\))?\s*$")
        matches: Callable[[str], bool] = lambda line: heading_re.match(line) is not None
    else:
        matches = lambda line: line.startswith(heading)  # noqa: E731
    sections: list[str] = []
    for start, line in enumerate(lines):
        if not matches(line):
            continue
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        sections.append("\n".join(lines[start + 1:end]))
    return sections


# 코드 span/fence 안의 *예시* 링크·wikilink 는 실제 참조가 아니므로 스캔 전 제거(오탐 0).
# 문서가 "나쁜 예시"로 `[x](decisions/9999-ghost.md)` 를 코드로 보여줘도 게이트를 막지 않게 한다.
# fenced(``` … ``` · ~~~ … ~~~) 를 먼저(여러 줄·비-greedy), 그 다음 inline(`…`·한 줄) 을 지운다.
_FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """markdown 코드 span/fence 를 공백으로 치환 — 그 안의 예시 링크가 lint 에 안 걸리게."""
    return _INLINE_CODE_RE.sub(" ", _FENCED_CODE_RE.sub(" ", text))
