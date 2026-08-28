"""T-0875 pm-qa three-harness surface contract."""
import shlex
from pathlib import Path

from test_board_regression import (
    _marker as regression_marker,
    _run_args as regression_run_args,
    board as regression_board,
)


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


def test_full_core_and_platform_same_head_use_project_full_commands_not_a_test_allowlist(
        regression_board, monkeypatch):
    """QA full은 프로젝트 ``test.cmd``와 platform marker 전체를 그대로 실행한다.

    특정 AT 파일 목록을 엔진 안에 박으면 그 목록 밖으로 개칭된 reviewer suite가 조용히 빠진다.
    프로젝트의 tests/ 루트 명령을 core에서 그대로 쓰고, 선언 platform wrapper도 같은 HEAD marker로
    완주해야만 aggregate green이 되는 실제 runner 계약을 고정한다.
    """
    board = regression_board
    full_test_cmd = "python3 -m pytest tests/ -q -n auto"
    platform_cmd = "run-windows-project-full-suite"
    board.LOCAL_CONF.write_text(
        "\n".join((
            f"test.cmd={full_test_cmd}",
            "qa.platforms=windows",
            f"test.windows.cmd={platform_cmd}",
            "regression.min_collected=2",
        )) + "\n",
        encoding="utf-8",
    )
    expected_head = "deadbeef01234567"
    calls = []

    def run(command, cwd, env):
        calls.append((command, cwd, dict(env)))
        if command == platform_cmd:
            return 0, regression_marker("windows", expected_head, collected=3), ""
        assert command == full_test_cmd
        return 0, "3 passed in 0.01s\n", ""

    monkeypatch.setattr(board, "_run_regression_cmd", run)

    assert board.cmd_regression(regression_run_args(final=True)) == 0
    assert [command for command, _cwd, _env in calls] == [full_test_cmd, platform_cmd]
    assert "PM_QA_PLATFORM" not in calls[0][2]
    assert calls[1][2]["PM_QA_PLATFORM"] == "windows"
    assert calls[1][2]["PM_QA_EXPECTED_HEAD"] == expected_head

    core_argv = shlex.split(calls[0][0])
    assert "tests/" in core_argv
    assert not any(token.startswith("tests/test_") for token in core_argv)
    assert not ({"-k", "--ignore", "--ignore-glob"} & set(core_argv))
    assert (REPO / "tests/test_additional_reviewer.py").is_file(), (
        "full tests/ 수집에 포함돼야 할 개칭된 reviewer suite가 없다"
    )
