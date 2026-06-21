"""pm-wave-finish 스킬 문서가 ADR-0023(status judgment-only) 모델과 정합함을 못박는다.

배경: ADR-0023 으로 `ticket_finish.py` 는 status.md 합계표/스칼라를 더 이상 갱신하지 않고
`--section` 을 deprecated no-op 로 받는다. 그런데 pm-wave-finish 스킬 문서(claude SKILL ×2 +
opencode command)와 pm_role 카탈로그가 한동안 "CLI 가 status.md 스칼라 갱신" 이라는 *거짓 서술*을
남겨 채택자를 오도했다(T-0108·이 세션 통합 메타 "redefine 후 기존 자산 갱신 누락" 클래스). 이
가드는 그 정확한 stale 문구의 재발과 `--section` deprecated 문서화의 회귀를 차단한다.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# pm-wave-finish 출하 문서 3종 (root dogfood + claude_code 템플릿 + opencode command).
WAVE_FINISH_DOCS = [
    REPO / ".claude" / "skills" / "pm-wave-finish" / "SKILL.md",
    REPO / "templates" / "claude_code" / ".claude" / "skills" / "pm-wave-finish" / "SKILL.md",
    REPO / "templates" / "opencode" / ".opencode" / "command" / "pm-wave-finish.md",
]

TICKET_FINISH = REPO / ".project_manager" / "tools" / "ticket_finish.py"


def test_wave_finish_docs_have_no_status_scalar_claim():
    """pm-wave-finish 문서에 'status.md 스칼라' (ADR-0023 으로 제거된 거짓 서술) 가 없다."""
    for doc in WAVE_FINISH_DOCS:
        assert doc.is_file(), f"누락: {doc}"
        text = doc.read_text(encoding="utf-8")
        assert "status.md 스칼라" not in text, (
            f"{doc.relative_to(REPO)} 에 stale 'status.md 스칼라' 서술이 남음 — "
            f"ticket_finish 는 ADR-0023 으로 status.md 를 갱신하지 않는다")
        # 합계표 갱신을 CLI 단계로 주장하지 않는다 (judgment-only).
        assert "스칼라 갱신" not in text, (
            f"{doc.relative_to(REPO)} 에 stale 'status.md 스칼라 갱신' 단계가 남음 (ADR-0023)")


def test_claude_wave_finish_skill_copies_byte_identical():
    """root 와 claude_code 템플릿의 pm-wave-finish SKILL.md 가 byte-동일 (파리티)."""
    root = WAVE_FINISH_DOCS[0].read_bytes()
    tmpl = WAVE_FINISH_DOCS[1].read_bytes()
    assert root == tmpl, (
        ".claude/skills/pm-wave-finish/SKILL.md 의 root 와 claude_code 템플릿 사본이 "
        "갈렸다 — 한 쪽만 고치고 다른 쪽을 놓치는 drift 클래스 (둘 다 같이 갱신)")


def test_ticket_finish_documents_section_deprecated_noop():
    """ticket_finish.py 의 --section 이 deprecated/no-op 로 문서화돼 있다 (모델 정합 회귀 보호)."""
    text = TICKET_FINISH.read_text(encoding="utf-8")
    assert "--section" in text, "ticket_finish.py 가 --section 인자를 더 이상 정의하지 않음"
    assert ("deprecated" in text) or ("no-op" in text), (
        "ticket_finish.py 의 --section 이 deprecated/no-op 로 표시돼 있지 않음 (ADR-0023)")
