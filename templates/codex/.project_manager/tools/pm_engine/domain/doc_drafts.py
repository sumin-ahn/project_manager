"""domain 페이지 초안 양식 (층: domain).

capture-draft 가 만드는 초안의 slug, frontmatter, 본문(출처 산문의 h2 강등) 렌더를 소유한다.
"""
from __future__ import annotations

import re

from pm_engine.domain import doc_pages


DEFAULT_DRAFT_TYPE = "research"

# capture-draft 출력 파일 suffix(`.draft.md`) — 사람 가독 보조. **필터 기준 아님**
# (index 제외는 frontmatter `status: draft`). suffix 는 PM 가 promote 시 `.md` 로 rename.
DRAFT_SUFFIX = ".draft.md"

# source: 가 repo 밖/일시경로/stdin/미지정일 때 박는 자유서술 placeholder(절대경로 박제 금지).
# 기존 frontmatter 자유서술 토큰 관례(`<!-- TODO PM: ... -->`)와 정합 — promote 전 PM 손.
SOURCE_TODO_PLACEHOLDER = "<!-- TODO PM: 출처 -->"

# kebab 슬러그 변환 — 영숫자만 남기고 그 외(공백·기호·한글)는 하이픈, 중복/양끝 하이픈 정리.
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """title 을 kebab-case 슬러그로 변환한다 (--slug 미지정 시 파일명 도출).

    소문자화 후 영숫자(ascii) 외 문자열을 단일 하이픈으로 접고 양끝 하이픈을 제거한다.
    한글 등 비-ascii 는 영숫자가 아니라 하이픈으로 접히므로, 영숫자가 전혀 없는 title
    (순한글 등)은 빈 슬러그가 된다 → 호출부(cmd)가 `draft` 기본 슬러그로 대체한다.
    """
    slug = _SLUG_NON_ALNUM_RE.sub("-", title.lower()).strip("-")
    return slug


# 코드펜스 토글 — 라인-시작 ``` 또는 ~~~ (3+ 백틱/틸드)이 펜스 경계(CommonMark 는 둘 다 펜스).
# 펜스 안의 `## ` 는 마크다운 헤딩이 아니라 코드/주석(예 shell `## comment`)이므로 강등에서 제외
# (주석/마크다운 펜스 보호). group(1)=펜스 문자 — CommonMark 정합상 여는 펜스와 닫는 펜스 문자가
# 같아야 닫힌다(``` 로 열면 ``` 로 닫힘·~~~ 로 열면 ~~~ 로 닫힘) → mixed 펜스 오토글 방지.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

# 라인-시작 `## ` (정확히 2개 — `###`+ 는 더 깊어 scaffold 절과 충돌 안 함). `##` 만 한 단계
# 강등(dogfood 사례 충분·단순). `###`+ 전체 깊이 시프트는 over-engineering 이라 생략.
_PROSE_H2_RE = re.compile(r"^## (?!#)")


def _demote_prose_headings(prose: str) -> str:
    """prose 본문의 라인-시작 `## ` 헤딩을 `### ` 로 한 단계 강등한다(scaffold 절 충돌 방지).

    capture-draft 는 prose 를 `## 조사 결과` 절 *아래* verbatim 배치하는데, prose 가 자체
    `## ` 헤딩을 가지면 그게 페이지 절(`## 한 줄`·`## gotcha`)과 같은 레벨 형제로 떠 구조가
    어긋난다. `## `(정확히 H2)만 `### ` 로 강등해 scaffold 절 하위로 일관 배치.

    코드펜스(```·~~~) 안의 `## ` 는 마크다운 헤딩이 아니라 코드/주석이므로 제외한다 — 펜스
    토글을 추적해 펜스 밖 라인만 강등. CommonMark 정합상 닫는 펜스는 *여는 펜스와 같은 문자*
    여야 닫히므로(`~~~` 안의 ``` 는 펜스를 닫지 않음), 여는 펜스 문자(`fence_char`)를 기억해
    동일 문자에서만 닫는다(mixed 펜스 오토글 방지). `###`+ 는 이미 더 깊어 scaffold 절과 충돌
    하지 않으므로 손대지 않는다(상대 깊이 시프트는 dogfood 불요).
    """
    fence_char = ""  # "" = 펜스 밖 · "`"/"~" = 그 문자로 연 펜스 안.
    out_lines = []
    for line in prose.split("\n"):
        fence = _FENCE_RE.match(line)
        if fence:
            char = fence.group(1)[0]  # 펜스 문자(백틱/틸드).
            if not fence_char:
                fence_char = char       # 펜스 진입(여는 문자 기억).
            elif char == fence_char:
                fence_char = ""         # 같은 문자에서만 펜스 종료.
            # (펜스 안에서 다른 문자 펜스 라인 → 코드 내용·토글 안 함.)
            out_lines.append(line)
            continue
        if not fence_char and _PROSE_H2_RE.match(line):
            line = "#" + line  # `## ` → `### ` (한 단계 강등).
        out_lines.append(line)
    return "\n".join(out_lines)


def _draft_frontmatter(title: str, ptype: str, covers: list[str],
                       source: str, today: str) -> str:
    """draft 페이지 frontmatter(scaffold) 문자열을 만든다.

    `status: draft`(index 제외 진실)·`derived: false`(사람 author)·`source`(provenance).
    covers 가 비면 빈 리스트(`covers: []`)로 두고 body 에 TODO placeholder 를 띄운다(아래
    _draft_body). yaml 안전을 위해 title/source 는 따옴표로 감싼다(콜론·특수문자 방어).
    """
    covers_yaml = "[" + ", ".join(covers) + "]" if covers else "[]"
    return (
        f"title: {_yaml_quote(title)}\n"
        f"type: {ptype}\n"
        f"covers: {covers_yaml}\n"
        f"repo: self\n"
        f"derived: false\n"
        f"status: {doc_pages.DRAFT_STATUS}\n"
        f"updated: {today}\n"
        f"source: {_yaml_quote(source)}\n"
    )


def _yaml_quote(value: str) -> str:
    """frontmatter 스칼라를 큰따옴표로 감싼다(콜론·`#` 등 yaml 메타 방어·내부 `"`·`\\` escape)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _draft_body(title: str, covers: list[str], prose: str, guide: str = "") -> str:
    """draft 페이지 body(scaffold) — `_template.md` 골격 + prose **verbatim 배치**.

    조사 prose 를 요약/구조화하지 않고 `## 조사 결과` 아래 *그대로* 배치한다(기계는 배치만·
    의미 판단은 PM/LLM). covers 미지정 시 `## 조사 결과` 앞에 `TODO PM: covers 글롭` 을
    띄운다. 한 줄 요약·gotcha·관련 절은 TODO placeholder 로 PM 손을 기다린다.

    `guide`(스캐폴드의 현재-진실/히스토리 안내)는 제목 **앞**에 놓는다 — 손으로 복사한
    페이지와 같은 자리다. body 는 frontmatter *뒤*에 붙으므로 `---` 시작은 유지된다
    (안내가 frontmatter 앞에 가면 load_pages 가 페이지 아님으로 조용히 skip 한다).
    """
    covers_todo = "" if covers else "<!-- TODO PM: covers 글롭 (담당 코드) -->\n\n"
    guide_block = f"{guide.strip()}\n\n" if guide.strip() else ""
    # prose 의 `## ` 헤딩을 `### ` 로 강등 — `## 조사 결과` 절 하위로 일관 배치(페이지 절과 미충돌).
    prose = _demote_prose_headings(prose) if prose.strip() else prose
    prose_block = prose if prose.strip() else "<!-- TODO PM: 조사 prose (--source) -->"
    return (
        f"{guide_block}"
        f"# {title}\n\n"
        f"## 한 줄\n"
        f"<!-- TODO PM: 한 줄 요약 -->\n\n"
        f"{covers_todo}"
        f"## 조사 결과\n"
        f"{prose_block}\n\n"
        f"## gotcha · 디버깅\n\n"
        f"## 관련\n"
    )
