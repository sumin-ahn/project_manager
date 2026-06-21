"""공유 pm_playbook.md 의 위임 절이 harness-중립 포인터를 갖는지 못박는다 (T-0111·A-③).

pm_playbook.md 는 manifest-synced *공유* 방법론 — 루트·claude_code·opencode 에 동일 파일이 간다.
§"위임 — 두 가지 방식" 본문은 dogfood(claude) 태생이라 claude 어휘(`Agent` 툴·`run_in_background`·
`.claude/agents/`)로 쓰여 있다. opencode 채택자가 같은 파일을 받아도 자기 harness(task tool)를
못 보는 누출을 막기 위해, 절 머리에 **harness 포인터 한 줄**(opencode=task tool·AGENTS.md §3)을 둔다.
이 가드는 그 포인터의 *존재*를 못박는다(claude 어휘 제거가 아니라 opencode 안내 누락 차단).
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

PLAYBOOKS = [
    REPO / ".project_manager" / "wiki" / "pm_playbook.md",
    REPO / "templates" / "claude_code" / ".project_manager" / "wiki" / "pm_playbook.md",
    REPO / "templates" / "opencode" / ".project_manager" / "wiki" / "pm_playbook.md",
]

_DELEGATION_HEADER = "## 위임"


def _delegation_section(text: str) -> str:
    """§'위임 …' 헤더부터 다음 '## ' 헤더 전까지 슬라이스 (해당 절만 검사)."""
    start = text.find(_DELEGATION_HEADER)
    assert start != -1, "pm_playbook 에 '## 위임' 절이 없음"
    rest = text[start + len(_DELEGATION_HEADER):]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: str(p.relative_to(REPO)))
def test_delegation_section_has_opencode_harness_pointer(path):
    """위임 절에 opencode harness 포인터(task tool·AGENTS.md §3)가 있다."""
    assert path.is_file(), f"누락: {path}"
    section = _delegation_section(path.read_text(encoding="utf-8"))
    assert "opencode" in section, (
        f"{path.relative_to(REPO)} 위임 절에 opencode 포인터 없음 — 공유 방법론이 "
        f"claude 전용 어휘로만 쓰임 (harness 누출)")
    assert "task" in section, (
        f"{path.relative_to(REPO)} 위임 절이 opencode 위임 도구(task tool)를 안내하지 않음")
    assert "AGENTS.md" in section, (
        f"{path.relative_to(REPO)} 위임 절이 opencode 위임 단일진실(AGENTS.md §3)을 가리키지 않음")
