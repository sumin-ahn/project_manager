"""domain 페이지 문서 규칙 (층: domain).

covers 글롭 표기와 매칭, `updated`·커밋 날짜 판독, 본문 wikilink·slug, 현재-진실 페이지에 쌓인 변경 이력
lead-in 판정을 소유한다.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path


# load_pages 가 스킵하는 비-페이지 파일(인덱스 README·복사용 템플릿).
_NON_PAGE_FILES = frozenset({"README.md", "_template.md"})

# ── history 축 (현재-진실 페이지에 쌓이는 변경 이력 검출) ─────────────────────
# domain 페이지는 *지금 무엇인가*를 담는 현재-진실 문서다 — 현재-진실 doc 과 히스토리를 가르는
# 구분(architecture·status 에 세운 것)의 domain 확장이다. "언제 왜 그렇게 바뀌었나"는 log/ADR
# 소관이고 페이지는 링크만 건다. 실측에서
# 한 페이지가 세션당 12.3줄씩 세션-delta blockquote 로 자라 200줄 임계를 반복해 넘겼고, 그
# 압박이 지식 자체를 손상시켰다(무관한 bullet 안으로 접힌 문장 1건). 그래서 판정 대상은
# **페이지 크기가 아니라 히스토리 항목의 존재**다 — "쪼개라"가 아니라 "안 쌓는다".
#
# 판정 = 블록의 lead-in(헤딩 텍스트 · blockquote 첫 줄 · 줄머리 굵은 도입부)이 *무엇인가*가
# 아니라 *언제 있었나*로 라벨돼 있는가. 세 축 중 하나면 히스토리 항목이다:
#   ⓐ lead-in 이 시점 스탬프로 **시작**한다 (`YYYY-MM-DD …` · `PM <N>차 …` · `vX.Y.Z …` ·
#      `T-NNNN …`) — 개념이 아니라 변경 단위로 절을 조직한 것.
#   ⓑ lead-in 에 시점 스탬프와 변경-기록 라벨이 **함께** 있다 (`YYYY-MM-DD (PM <N>차) delta` ·
#      `vX.Y.Z 코드 재검증 delta` · `T-NNNN verification checkpoint`).
#   ⓒ **헤딩 제목 자체가 이력 절 이름**이다 (`## 변경 이력` · `## changelog` · `## 히스토리`) —
#      스탬프가 한 줄도 없어도 그 절의 목적이 히스토리 축적이라 ⓐ·ⓑ 가 놓친다.
# 세 축 모두 lead-in 만 본다 — 본문이 날짜를 인용하거나(`1.18.4 실측`) 근거 티켓을 다는 건
# 정상 현재-진실 서술이라 대상이 아니다.
#
# 스탬프 문법은 **한 곳에서만** 선언한다 — 아래 세 정규식이 같은 조각을 조합해 쓰므로 축을
# 넓히거나 좁힐 때 두 벌을 고칠 일이 없다(두 벌 = 한쪽만 고쳐 조용히 갈리는 재발 클래스).
_TIME_STAMP_PATTERN = (
    r"\d{4}-\d{2}-\d{2}"              # YYYY-MM-DD
    r"|PM\s*\d+\s*차"                 # PM <N>차
    r"|v\d+\.\d+(?:\.\d+)?"           # vX.Y.Z (접두 v 필수 — 외부 도구 버전 `1.18.4` 인용은 제외)
)
_WORK_STAMP_PATTERN = r"T-\d{3,}"       # T-NNNN
HISTORY_STAMP_RE = re.compile(rf"(?:{_TIME_STAMP_PATTERN}|{_WORK_STAMP_PATTERN})")
HISTORY_STAMP_HEAD_RE = re.compile(
    rf"^\W*(?:{_TIME_STAMP_PATTERN}|{_WORK_STAMP_PATTERN})"
)

# 구분 줄 없이 이어 쌓은 이력 목록의 항목 머리 — **시점** 스탬프만 인정한다(티켓 ID 제외).
# 줄바꿈으로 이어진 산문이 `[[T-NNNN]]` 로 시작하는 건 흔한 정상 서술이라 그 축은 뺀다.
HISTORY_TIME_STAMP_HEAD_RE = re.compile(rf"^\W*(?:{_TIME_STAMP_PATTERN})")
HISTORY_LABEL_RE = re.compile(
    r"(?:delta|changelog|재검증|checkpoint|변경\s*이력|갱신\s*이력|히스토리|변경\s*기록)",
    re.IGNORECASE,
)

# ⓒ 축 — 헤딩 제목이 이력 절 이름으로 **시작**할 때만. 시작-앵커라 "호출 이력 조회 API" 같은
# 기능 서술 헤딩은 안 걸리고, 산문/불릿의 `history` 언급도 대상이 아니다(헤딩 전용 축).
HISTORY_SECTION_TITLE_RE = re.compile(
    r"^\W*(?:변경\s*이력|갱신\s*이력|수정\s*이력|변경\s*기록|이력|히스토리|연혁"
    r"|changelog|change\s+log|revision\s+history|history)",
    re.IGNORECASE,
)

# lead-in 추출 — 헤딩(`## …`) · blockquote 첫 줄(`> …`) · 줄머리 굵은 도입부(`**…**`).
_HISTORY_HEADING_RE = re.compile(r"^\s{0,3}#{2,6}\s+(.*)$")
_HISTORY_QUOTE_RE = re.compile(r"^\s{0,3}>\s*(.*)$")
_HISTORY_BOLD_RE = re.compile(r"^\s{0,3}(?:[-*+]\s+)?\*\*(.+?)\*\*")

# lead-in 판정에 쓰는 앞부분 길이 — 멀리 떨어진 스탬프와 라벨이 우연히 짝지어 오탐하는 걸 막는다.
HISTORY_LEADIN_CHARS = 120

# lead-in 을 finding detail 에 몇 개까지 인용하나 / 한 인용의 최대 길이.
HISTORY_DETAIL_LIMIT = 3
HISTORY_DETAIL_WIDTH = 60

# capture-draft scaffold 기본값 — frontmatter status 진실(draft 제외 기준)·type 기본.
DRAFT_STATUS = "draft"


# ── covers 글롭 매칭 (작은 glob→regex·stdlib) ────────────────────────────────


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """covers 글롭을 anchored full-match regex 로 변환한다.

    `**` = 0+ 세그먼트 재귀(슬래시 횡단) · `*` = 한 세그먼트 내(`[^/]*`) ·
    그 외 문자는 리터럴(`re.escape`). fnmatch 단독은 `**` 가 부정확하니 직접 변환한다.
    `src/analysis/**` ⊇ `src/analysis/factor_beta.py`, ⊉ `src/core/x.py`.

    `**` 는 **0+ 세그먼트**라 인접 슬래시를 함께 흡수한다(segment-aware):
      - trailing `/**`   → `(?:/.*)?`  : `src/analysis/**` 가 `src/analysis`(디렉토리 자체)·하위 모두 매치
      - leading/middle `**/` → `(?:.*/)?` : `src/**/x.py` 가 `src/x.py`(0 세그먼트)·`src/a/x.py` 매치
    그래서 0-세그먼트도 매치된다(`**/x.py` ⊇ `x.py`). escape 는 보존되어 `a.b/**` 의
    `.` 가 임의 문자로 새지 않는다.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # `**` = 0+ 세그먼트. 인접 슬래시를 흡수해 0-세그먼트도 매치.
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")  # leading/middle `**/` → 0+ 세그먼트(+슬래시)
                    i += 3
                elif out and out[-1] == "/":
                    out[-1] = "(?:/.*)?"     # trailing `/**` → 0+ 세그먼트(+선행 슬래시)
                    i += 2
                else:
                    out.append(".*")         # 그 외 단독 `**` → 세그먼트 횡단
                    i += 2
            else:
                out.append("[^/]*")  # `*` → 한 세그먼트 내(슬래시 제외)
                i += 1
        elif ch == "/":
            out.append("/")          # 슬래시는 리터럴(trailing `/**` 흡수 위해 escape 안 함)
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _path_matches_covers(path: str, covers: list[str]) -> bool:
    """repo-relative 코드 경로가 covers 글롭 중 하나라도 anchored full-match 하면 True."""
    return any(_glob_to_regex(glob).match(path) for glob in covers)


def _escape_glob_literals(g: str) -> str:
    """git `:(glob)` wildmatch 특수문자 중 **우리 covers 시맨틱이 와일드카드로 안 쓰는** 것을
    백슬래시 이스케이프한다 — `*`/`**` 만 와일드카드로 보존한다.

    우리 covers 매처는 `*`(세그먼트 내)·`**`(재귀)만 와일드카드로 보고 `?`·`[`·`]` 는 리터럴로
    본다. 그런데 git `:(glob)` 은 `?`(임의 1자)·`[...]`(문자클래스)도 와일드카드라, 리터럴
    특수문자가 든 covers 경로가 다른 파일에 오매칭(거짓 stale)되거나 실제 변경을 놓친다(false-green).
    `?`·`[`·`]` 와 백슬래시(이스케이프 문자 자체)를 백슬래시로 이스케이프해 리터럴로 만든다 —
    백슬래시를 **먼저** 처리해 중복 이스케이프를 피한다. `*` 는 손대지 않는다(우리 와일드카드).
    git 이 이스케이프된 `?`/`[`/`]` 를 리터럴로 처리함은 실측 확인."""
    g = g.replace("\\", "\\\\")   # 백슬래시 자체 먼저(뒤에 추가할 이스케이프와 중복 회피)
    for ch in "?[]":
        g = g.replace(ch, "\\" + ch)
    return g


def _is_supported_covers_glob(g: str) -> bool:
    """`g`(비-공백 stripped 글롭)가 **우리 matcher 와 git `:(glob)` 이 동일 의미인** 형태인가.

    엣지별 추격 대신 **지원 문법을 명시 검증**하고, 두 방언에서 의미가 증명된 형태만 변환한다.
    지원(실 git property 테스트로 동일성 증명·`tests/test_domain_freshness.py`):
      - 와일드카드 없는 **정확 경로**(파일 지목 — git 에서 dir 이면 하위 subtree 매칭돼 우리 exact
        매치보다 넓다·freshness 는 over-warn 이라 안전; 실사용=파일).
      - 단일 `*`(세그먼트 내 `[^/]*`) — `dir/*`·`dir/*.ext`·중간 `*` 어디든.
      - leading `**/`·middle `/**/` 경계 `**`, **리터럴-prefix** trailing `/**`(`src/**`·`src/nested/**`).
    **미지원**(→ 호출부 unmappable advisory·오번역 안 함): **비-경계 `**`**(`**.py`·`src/**.py`·`a**b`
    — git 은 `**` 를 세그먼트 못 넘게 처리해 중첩 파일을 **miss=false-green**), `***` 이상,
    **repo-밖/절대 경로**(선행 `/`·`..` 세그먼트·Windows 드라이브 `X:`),
    **wildcard-prefix + trailing `/**`**(`*/**`·`a*b/**`·`src/*/**`·`*/literal/**` — 우리 matcher 의
    trailing `/**`=parent-포함(`(?:/.*)?`)이 prefix-레벨/루트 파일(`.gitignore`)까지 매칭하나 git
    `:(glob)…/**` 는 슬래시 필수라 제외 → **git miss=false-green**). 우리 실사용(전 페이지 =
    정확 경로 + 리터럴-prefix `/**`)은 전부 지원 집합 안(실 git property 테스트)."""
    # repo-밖/절대 경로 선분류 (순수 문자열·git 호출 불요).
    if g.startswith("/"):
        return False  # 절대 경로(POSIX)
    if re.match(r"[A-Za-z]:", g):
        return False  # Windows 드라이브(`C:/`·`C:\`)
    if ".." in re.split(r"[\\/]", g):
        return False  # `..` 세그먼트 = repo 밖 탈출
    # trailing `/**` 는 prefix 가 전부 리터럴일 때만 두 방언 동등 — prefix 에 와일드카드가 있으면
    # 우리 parent-포함 매칭(prefix-레벨/루트 파일)이 git `…/**`(슬래시 필수)와 갈린다.
    if g.endswith("/**") and "*" in g[:-3]:
        return False
    i, n = 0, len(g)
    while i < n:
        if g[i] != "*":
            i += 1
            continue
        j = i
        while j < n and g[j] == "*":
            j += 1
        run = j - i
        if run == 1:
            i = j  # 단일 `*` — 세그먼트 내(`[^/]*`)·두 방언 동등.
        elif run == 2:
            # `**` — 양쪽이 `/`(또는 문자열 경계)일 때만 두 방언 동등(경계 `**`).
            left_boundary = (i == 0) or (g[i - 1] == "/")
            right_boundary = (j == n) or (g[j] == "/")
            if not (left_boundary and right_boundary):
                return False  # 비-경계 `**` — git 이 세그먼트 못 넘어 miss(false-green).
            i = j
        else:
            return False  # `***` 이상 — 미지원.
    return True


def covers_glob_pathspec(glob) -> str | None:
    """covers 글롭을 git **`:(glob)` magic pathspec** 으로 직접 변환한다 (손실 접두사 폐기).

    `git diff`(HEAD 트리)/`git log` 에 `:(glob)<원본 글롭>` 을 넘겨 git 이 `*`(세그먼트 내·`/` 안 넘음)·
    경계 `**`(슬래시 횡단)을 **네이티브로** 처리하게 한다 — 접두사 추출의 **손실**(접두사-없는 글롭
    통째 skip·`src/*.py`→`src/` 과확장)을 없앤다.
    `*` 외 git glob 특수문자(`?`·`[`·`]`·백슬래시)는 `_escape_glob_literals` 로 이스케이프해 리터럴
    보존. **지원 문법만 변환**한다(`_is_supported_covers_glob`) — 빈/공백 또는 두
    방언 의미가 다른 형태(비-경계 `**` 등)는 오번역 대신 `None`(호출부가 unmappable advisory).
    sha-축(`covers_pathspecs`)과 date-축(`page_stale`/`lint_domain`) 이 **공유**하는 단일 판정 기계다
    (date-축도 종전 손실 접두사에서 이 원본-글롭 판정으로 통일)."""
    g = str(glob or "").strip()
    if not g or not _is_supported_covers_glob(g):
        return None
    return f":(glob){_escape_glob_literals(g)}"


class _PageCovers(list):
    """list 호환 covers + 그 페이지의 frontmatter ``repo:`` 소유 채널."""

    def __init__(self, values, owner_repo):
        super().__init__(values)
        self.owner_repo = owner_repo


def _parse_updated_date(updated) -> datetime.date | None:
    """페이지 `updated` 를 date 로 정규화한다 (ticket_store.load_ticket=yaml.safe_load).

    YAML 의 따옴표 없는 `2026-06-19` 는 `datetime.date` 로, 따옴표/기타는 문자열로 온다.
    date/datetime 는 그대로 date 화, 문자열은 ISO 앞 10자(`YYYY-MM-DD`)를 파싱한다. 파싱
    실패/None/기타 타입 → None(호출부 page_stale 이 unknown 으로 흡수·fail-soft).
    """
    if isinstance(updated, datetime.datetime):
        return updated.date()
    if isinstance(updated, datetime.date):
        return updated
    if isinstance(updated, str):
        try:
            return datetime.date.fromisoformat(updated.strip()[:10])
        except ValueError:
            return None
    return None


def _parse_commit_date(out: str) -> datetime.date | None:
    """`git log --format=%cI` ISO 출력에서 커밋 날짜(date 부분)를 뽑는다.

    `%cI` = strict ISO(`2026-06-19T07:59:00+09:00`). 앞 10자(`YYYY-MM-DD`)만 date 비교에
    쓴다(시각/타임존 무시 — updated 가 날짜 단위라 date 끼리 비교). 빈/깨진 출력 → None.
    """
    out = out.strip()
    if not out:
        return None
    try:
        return datetime.date.fromisoformat(out[:10])
    except ValueError:
        return None


# domain 페이지 본문의 wikilink `[[슬러그]]`(별칭 `[[슬러그|텍스트]]` 의 슬러그 부분만).
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)")


def page_slug(page: dict) -> str:
    """페이지 슬러그 = 파일 stem(`dual-gate-review.md` → `dual-gate-review`).

    domain wikilink 표기 관례(`[[ADR-NNNN]]`·`[[T-NNNN]]`·README 의 `[[다른-페이지]]`)는
    파일 stem 을 슬러그로 쓴다. orphan 인링크 카운트의 정규 키 — title 변형이 아닌 안정
    식별자(파일명)로 잡는다(아래 inlink 집합도 stem·title 둘 다 인정해 표기 흔들림 흡수).
    """
    return Path(page["path"]).stem


def _markdown_without_fenced_code(body: str) -> str:
    """Markdown fenced code block을 제거해 예시 marker/커맨드를 실제 claim과 분리한다."""
    visible: list[str] = []
    fence: str | None = None
    for line in body.splitlines(keepends=True):
        match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if match:
            token = match.group(1)
            if fence is None:
                fence = token[0]
            elif token[0] == fence:
                fence = None
            continue
        if fence is None:
            visible.append(line)
    return "".join(visible)


def _wikilink_targets(body: str) -> set[str]:
    """body 의 `[[슬러그]]` 인링크 타깃 집합(소문자 정규화·별칭 `|` 앞부분만)."""
    return {m.strip().lower() for m in _WIKILINK_RE.findall(body)}


def _is_history_leadin(text: str, *, stamp_head: bool,
                       section_title: bool = False) -> bool:
    """lead-in 텍스트가 히스토리 항목(시점으로 라벨된 블록)인지 판정한다.

    `stamp_head=True`(헤딩·blockquote 항목)면 시점 스탬프로 **시작**하는 것만으로 히스토리다 —
    절/항목을 개념이 아니라 변경 단위로 조직한 형태. 굵은 문단 도입부는 티켓 근거를 앞세운 정상
    서술이 흔해 그 축을 빼고, 스탬프와 변경-기록 라벨이 **함께** 있을 때만 잡는다.
    `section_title=True`(헤딩 전용)는 제목이 이력 절 이름으로 시작하면 스탬프 없이도 잡는다 —
    `## 변경 이력` 처럼 절 이름만으로 목적이 히스토리 축적인 경우를 스탬프 축이 통째로 놓친다.
    """
    head = text[:HISTORY_LEADIN_CHARS]
    if section_title and HISTORY_SECTION_TITLE_RE.match(head):
        return True
    if stamp_head and HISTORY_STAMP_HEAD_RE.match(head):
        return True
    return bool(HISTORY_STAMP_RE.search(head) and HISTORY_LABEL_RE.search(head))


def history_leadins(body: str) -> list[str]:
    """페이지 본문에서 히스토리 항목의 lead-in 을 등장 순서대로 뽑는다 (fenced code 제외).

    블록 단위로 lead-in 하나만 본다 — 헤딩 텍스트 · blockquote 항목의 첫 줄(`>` 단독 줄이 항목
    경계) · 줄머리 굵은 도입부. 본문 안쪽에서 날짜/티켓을 *근거로* 인용하는 건 정상 현재-진실
    서술이라 판정 대상이 아니다(`HISTORY_STAMP_RE` 주석의 세 축 참조).

    **알려진 한계 (advisory 라 수용)** — 인용 안에서 항목 경계(`>` 단독 줄) 없이 줄바꿈만으로
    이어진 문장이 마침 시점 스탬프로 *시작*하면 새 항목으로 센다. 개수가 과대 계상될 뿐 판정
    자체(그 인용이 이력인가)는 안 뒤집히고, 반대로 항목을 줄바꿈으로 감춰 회피하는 것도 막는다.
    정확히 가르려면 마크다운 파서가 필요한데 이 축의 값어치(never-block 권고)에 비해 과하다.
    """
    leadins: list[str] = []
    quote_entry_open = False
    for raw in _markdown_without_fenced_code(body).splitlines():
        line = raw.rstrip()
        heading = _HISTORY_HEADING_RE.match(line)
        if heading is not None:
            quote_entry_open = False
            if _is_history_leadin(heading.group(1), stamp_head=True,
                                  section_title=True):
                leadins.append(heading.group(1))
            continue
        quote = _HISTORY_QUOTE_RE.match(line)
        if quote is not None:
            inner = quote.group(1).strip()
            if not inner:
                quote_entry_open = False  # `>` 단독 줄 = 인용 안의 항목 경계.
                continue
            if not quote_entry_open:
                quote_entry_open = True
                if _is_history_leadin(inner, stamp_head=True):
                    leadins.append(inner)
            elif HISTORY_TIME_STAMP_HEAD_RE.match(inner):
                # 구분 빈 줄 없이 `> (YYYY-MM-DD) …` 를 줄줄이 쌓은 이력 목록 — 시점 스탬프로
                # 시작하는 줄은 그 자체가 새 항목의 머리다(줄바꿈 이어짐은 날짜로 안 시작).
                leadins.append(inner)
            continue
        quote_entry_open = False
        if not line.strip():
            continue
        bold = _HISTORY_BOLD_RE.match(line)
        if bold is not None and _is_history_leadin(bold.group(1), stamp_head=False):
            leadins.append(bold.group(1))
    return leadins


def _history_detail(leadins: list[str]) -> str:
    """history finding 의 detail — 개수 + 규칙 한 줄 + 실제 lead-in 인용(상한)."""
    shown = []
    for text in leadins[:HISTORY_DETAIL_LIMIT]:
        flat = " ".join(text.split())
        if len(flat) > HISTORY_DETAIL_WIDTH:
            flat = flat[:HISTORY_DETAIL_WIDTH] + "…"
        shown.append(flat)
    hidden = len(leadins) - len(shown)
    tail = f" 외 {hidden}건" if hidden else ""
    return (
        f"변경 이력 항목 {len(leadins)}개 — 현재-진실 페이지엔 *지금 유효한 것*만 두고 "
        f"'언제 왜 바뀌었나'는 log/ADR 로 옮긴다(페이지는 링크만): "
        + " · ".join(shown) + tail
    )
