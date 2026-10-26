"""설치 공통 skill_inventory (infra)."""
from __future__ import annotations

from pm_engine.domain import home
import os

def _runtime_skill_entry(skill: str) -> str:
    """현재 실행 하네스의 사용자 호출 표기(Codex env marker 외 slash)."""
    prefix = "$" if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI") else "/"
    return f"{prefix}{skill}"


def _installed_skill_entry_names(home: home.Home) -> tuple[str, ...]:
    """실행 Home의 모든 실제 스킬 카드 이름을 파생한다.

    canonical/Claude/OpenCode는 ``.claude/skills``, Codex는 ``.agents/skills``를 쓴다.
    두 root를 모두 열어 특정 접두사나 손 목록 없이 ``*/SKILL.md`` 실재 카드만 소비한다.
    """
    project_root = home.root
    return tuple(sorted({
        card.parent.name
        for skills_root in (
            project_root / ".claude" / "skills",
            project_root / ".agents" / "skills",
        )
        for card in skills_root.glob("*/SKILL.md")
    }))
