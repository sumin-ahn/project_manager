"""대시보드 세션 구획 판독."""
from __future__ import annotations
import re

_DASHBOARD_HEADING_RE = re.compile("^## (.+?)[ \\t]*$", re.MULTILINE)


def parse_dashboard_sections(dashboard_text: str) -> list[tuple[str, str]]:
    """대시보드 텍스트를 `[(session_key, body), ...]` 로 파싱한다 (헤딩 `## <key>` 경계).

    각 섹션 = `## <key>` 헤딩부터 다음 `## ` 헤딩 직전까지. body 는 헤딩 줄 제외 본문에서
    surrounding 빈 줄을 벗긴 것(read-only surface·부트스트랩 light dump 용). preamble(첫 `## `
    앞)은 무시한다. write 측(upsert)과 같은 경계 grammar 를 공유해 read/write 대칭을 보장한다.
    """
    sections: list[tuple[str, str]] = []
    matches = list(_DASHBOARD_HEADING_RE.finditer(dashboard_text))
    for i, mt in enumerate(matches):
        key = mt.group(1).strip()
        body_start = mt.end()
        body_end = (
            matches[i + 1].start() if i + 1 < len(matches) else len(dashboard_text)
        )
        body = dashboard_text[body_start:body_end].strip("\n").rstrip()
        sections.append((key, body))
    return sections

import datetime
from pm_engine.domain import handoff_text

DASHBOARD_SECTION_MAX_LINES = 5


DASHBOARD_LINE_MAX_CHARS = 200


_DASHBOARD_HEADER = (
    "# slot 대시보드 (수정형·현재-상태 스냅샷·타 슬롯 byte 불변)\n"
)


def _flatten_dashboard_value(value: object) -> str:
    """대시보드 본문 한 줄 값으로 안전화 — 개행 평탄화·trim·char cap (섹션 위조 방어).

    render_dashboard_section 인자는 공개라 다중행 입력(wave_summary·next_plan)이 `## ` 헤딩을
    위조하거나 상한 줄수를 우회할 수 있다 — 개행을 공백으로 접고(single line) char cap 을 씌워
    자기 규격(줄단위·경량)을 엔진이 직접 방어한다.
    """
    flat = " ".join(part.strip() for part in str(value).splitlines() if part.strip())
    flat = flat.strip()
    if len(flat) > DASHBOARD_LINE_MAX_CHARS:
        flat = flat[: DASHBOARD_LINE_MAX_CHARS - 1].rstrip() + "…"
    return flat


def _sanitize_dashboard_key(session: object) -> str:
    """대시보드 섹션 헤딩 키(`## <session>`)를 안전화 — 개행·`#`(헤딩 마커) 제거·공백 접기.

    session 키는 `## <session>` 헤딩으로 쓰이고 upsert 의 경계 정규식이 그걸 매칭한다 —
    개행이나 `#` 가 섞이면 가짜 헤딩/섹션 경계를 위조할 수 있다(render/upsert 인자는 공개).
    `#` 를 공백으로 바꾸고 whitespace(개행 포함)를 단일 공백으로 접어 한 줄 안전 토큰으로 만든다.
    render(헤딩 출력)·upsert(경계 검색)가 **같은 정규화**를 써 같은 raw 키에 일관 동작한다.
    정상 키(`<repo>_<N>`)는 공백·`#` 가 없어 항등(무변경).
    """
    return " ".join(str(session).replace("#", " ").split())


def render_dashboard_section(
    session: str,
    *,
    session_num: int | str,
    wave_summary: str,
    claimed_tickets: list[str] | None = None,
    next_plan: str | None = None,
    date: str | None = None,
    max_lines: int | None = None,
) -> str:
    """대시보드 자기 섹션(헤딩 `## <session>` + 본문 3~5줄)을 빌드한다.

    본문 줄(순서·상한 `max_lines`=DASHBOARD_SECTION_MAX_LINES):
      - 차수: PM <N>차 (<date>)
      - wave: <wave_summary 1줄 평탄화>
      - claimed: <T-…, …>   (claimed_tickets 비면 줄 생략)
      - 다음: <next_plan>    (없으면 줄 생략)
    각 값은 `_flatten_dashboard_value`(개행 평탄화·char cap)로 경량화하고, 본문을 `max_lines`
    줄로 truncate 한다(초과분 drop). 반환은 헤딩 + 빈 줄 + 본문 + 후행 개행 1개 —
    upsert_dashboard_section 이 파일에 끼워 넣는 단위 블록이다.
    """
    if date is None:
        date = datetime.date.today().isoformat()
    if max_lines is None:
        max_lines = DASHBOARD_SECTION_MAX_LINES
    body_lines = [
        f"- 차수: PM {handoff_text._normalize_session_num(session_num)}차 ({date})",
        f"- wave: {_flatten_dashboard_value(wave_summary)}",
    ]
    if claimed_tickets:
        body_lines.append(
            f"- claimed: {_flatten_dashboard_value(', '.join(claimed_tickets))}"
        )
    if next_plan and str(next_plan).strip():
        body_lines.append(f"- 다음: {_flatten_dashboard_value(next_plan)}")
    body = "\n".join(body_lines[:max_lines])
    # 헤딩 키도 안전화 — 개행/`#` 위조로 섹션 경계를 오염시키지 못하게(upsert 검색과 동일 정규화).
    return f"## {_sanitize_dashboard_key(session)}\n\n{body}\n"


def upsert_dashboard_section(
    dashboard_text: str, session: str, section: str
) -> str:
    """대시보드에서 `session` 섹션을 `section` 으로 교체하고, 없으면 append 한다 (수정형).

    - 기존 섹션 교체: `## <session>` 헤딩부터 다음 `## ` 헤딩 직전(또는 파일 끝)까지를 `section`
      으로 offset splice 치환한다 — 헤딩 앞/다음 섹션 이후 bytes 를 그대로 복사하므로 **타 슬롯
      섹션은 byte 불변**(테스트로 못박음). 다음 섹션이 있으면 빈 줄 1개로 구분한다.
    - 신규(섹션 부재): 파일 끝에 append(lazy). 파일이 비면 `_DASHBOARD_HEADER` + 자기 섹션.
    - 멱등: 같은 `section` 재실행은 같은 결과. 순수 텍스트 변환 — 직렬화(파일락)는 파일 I/O 를
      쥔 호출부(`_write_dashboard_section` 의 `_dashboard_lock`)가 read→upsert→write 전체에
      건다(cross-slot lost update 차단·MF-2).

    헤딩 매칭은 정확 줄(`^## <session>$`·trailing 공백 허용)이라 `my_app_1` 이
    `my_app_10` 섹션을 오매치하지 않는다. `session` 은 `_sanitize_dashboard_key` 로
    정규화해 검색한다 — render 가 헤딩에 쓰는 것과 **같은 정규화**라 같은 raw 키에 일관 매칭한다.
    """
    core = section if section.endswith("\n") else section + "\n"
    session = _sanitize_dashboard_key(session)
    heading_re = re.compile(rf"^## {re.escape(session)}[ \t]*$", re.MULTILINE)
    m = heading_re.search(dashboard_text)
    if m is not None:
        start = m.start()
        after = dashboard_text[m.end():]
        nxt = re.search(r"^## ", after, re.MULTILINE)
        if nxt is None:
            # 자기 섹션이 마지막(또는 유일) — 파일 끝까지 교체(후행 단일 개행).
            return dashboard_text[:start] + core
        end = m.end() + nxt.start()
        # 다음 섹션 이후는 verbatim 복사(byte 불변) — 사이에 빈 줄 1개 삽입.
        return dashboard_text[:start] + core + "\n" + dashboard_text[end:]
    # 섹션 부재 → append(lazy 생성).
    if not dashboard_text.strip():
        return _DASHBOARD_HEADER + "\n" + core
    return dashboard_text.rstrip("\n") + "\n\n" + core
