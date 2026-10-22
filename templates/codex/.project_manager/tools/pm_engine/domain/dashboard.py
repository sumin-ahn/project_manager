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
