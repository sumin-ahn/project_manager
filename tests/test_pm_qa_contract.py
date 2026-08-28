"""T-0875 pm-qa three-harness surface contract."""
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SKILLS = (
    REPO / ".claude/skills/pm-qa/SKILL.md",
    REPO / "templates/claude_code/.claude/skills/pm-qa/SKILL.md",
    REPO / "templates/codex/.agents/skills/pm-qa/SKILL.md",
    REPO / "templates/opencode/.claude/skills/pm-qa/SKILL.md",
    REPO / "templates/opencode/.opencode/command/pm-qa.md",
)
DETAILS = (
    REPO / ".claude/skills/pm-qa/references/operational-details.md",
    REPO / "templates/claude_code/.claude/skills/pm-qa/references/operational-details.md",
    REPO / "templates/codex/.agents/skills/pm-qa/references/operational-details.md",
    REPO / "templates/opencode/.claude/skills/pm-qa/references/operational-details.md",
)
REGRESSION_COMMAND = "python3 .project_manager/tools/board.py regression run"
README = REPO / "README.md"


def test_every_pm_qa_surface_uses_one_unscoped_backbone_call_and_no_selector_flags():
    assert all(path.is_file() for path in SKILLS)
    for path in SKILLS:
        text = path.read_text(encoding="utf-8")
        assert text.count(REGRESSION_COMMAND) == 1, path
        assert "--platform" not in text and "--all-platforms" not in text, path
        assert "platform[<name>]" in text and "회귀(core)" in text, path
        assert "red/미실행" in text and "후속" in text and "중단" in text, path


def test_every_pm_qa_report_contract_has_core_and_declared_platform_rows():
    assert all(path.is_file() for path in DETAILS)
    for path in DETAILS:
        text = path.read_text(encoding="utf-8")
        assert "회귀(core):" in text, path
        assert "platform[<name>]:" in text, path
        assert "어느 행이든 red/미실행" in text, path
        assert "한 번이 core와 선언 platform 전부를 직렬 실행" in text, path


def test_shipped_notation_and_details_document_the_wrapper_protocol():
    for path in (README, *DETAILS):
        text = path.read_text(encoding="utf-8")
        assert "PM_QA_RESULT_V1=" in text, path
        assert "PM_QA_PLATFORM" in text, path
        assert "PM_QA_EXPECTED_HEAD" in text, path
        assert all(member in text for member in (
            '"platform"', '"head"', '"status"', '"collected"',
        )), path


def test_shipped_pm_qa_contract_contains_no_machine_specific_vm_or_ssh_details():
    forbidden = ("/home/smahn/vm", "win11", "IdentityFile", "ssh -p", "guest@", "QEMU disk")
    for path in (*SKILLS, *DETAILS):
        text = path.read_text(encoding="utf-8")
        assert not any(value in text for value in forbidden), path
