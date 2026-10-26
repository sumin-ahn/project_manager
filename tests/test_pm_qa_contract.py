"""T-0875 pm-qa three-harness surface contract."""
import shlex
import json
import pytest
from pathlib import Path

from test_board_livegate import t0936_release

from test_board_regression import (
    _marker as regression_marker,
    _run_args as regression_run_args,
    board as regression_board,
)
from pm_engine.infra import git, pm_paths
from pm_engine.cli import regression_cmd
from pm_engine.app import regression


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
        assert "개발 회귀는 core만 실행" in text and "릴리즈 livegate record" in text, path


def test_shipped_notation_and_details_document_the_wrapper_protocol():
    for path in (README, *DETAILS):
        text = path.read_text(encoding="utf-8")
        assert "PM_QA_RESULT_V1=" in text, path
        assert "PM_QA_PLATFORM" in text, path
        assert "PM_QA_EXPECTED_HEAD" in text, path
        assert all(member in text for member in (
            '"platform"', '"head"', '"status"', '"collected"',
        )), path
        # 정리 시점 조항 — 종료 시점 정리는 중단된 실행에서 돌지 않아 잔여가 계속 쌓인다.
        # 프레임워크가 갖는 것은 실행기가 아니라 이 계약이다.
        assert "실행 시작에" in text, path
        assert all(made in text for made in ("번들", "게스트 클론", "호스트 임시")), path


def test_shipped_pm_qa_contract_contains_no_machine_specific_vm_or_ssh_details():
    forbidden = ("/home/smahn/vm", "win11", "IdentityFile", "ssh -p", "guest@", "QEMU disk")
    for path in (*SKILLS, *DETAILS):
        text = path.read_text(encoding="utf-8")
        assert not any(value in text for value in forbidden), path


def test_t0936_project_full_commands_follow_phase_ownership(
        regression_board, t0936_release, monkeypatch):
    """QA full은 프로젝트 ``test.cmd``와 platform marker 전체를 그대로 실행한다.

    특정 AT 파일 목록을 엔진 안에 박으면 그 목록 밖으로 개칭된 reviewer suite가 조용히 빠진다.
    프로젝트의 tests/ 루트 명령을 core에서 그대로 쓰고, 선언 platform wrapper도 같은 HEAD marker로
    완주해야만 aggregate green이 되는 실제 runner 계약을 고정한다.
    """
    board = regression_board
    full_test_cmd = "python3 -m pytest tests/ -q -n auto"
    platform_cmd = "run-windows-project-full-suite"
    pm_paths.home_at(board.REPO).local_conf.write_text(
        "\n".join((
            f"test.cmd={full_test_cmd}",
            "qa.platforms=windows",
            f"test.windows.cmd={platform_cmd}",
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

    monkeypatch.setattr(regression, "_run_regression_cmd", run)
    # HEAD 조회(`git._git_head_at`)는 두 board 가 공유하는 모듈이라 t0936 대역 값을 여기서 덮는다.
    monkeypatch.setattr(git, "_git_head_at", lambda cwd: expected_head)

    assert regression_cmd.cmd_regression(pm_paths.home_at(board.REPO), regression_run_args(final=True)) == 0
    assert [command for command, _cwd, _env in calls] == [full_test_cmd]
    assert "PM_QA_PLATFORM" not in calls[0][2]
    release = t0936_release
    release.anchor = board.REPO
    release.head = expected_head
    pm_paths.home_at(release.board.REPO).local_conf.write_text(
        pm_paths.home_at(board.REPO).local_conf.read_text(encoding="utf-8"), encoding="utf-8")
    core = json.loads(pm_paths.regression_flag(pm_paths.home_at(board.REPO)).read_text(encoding="utf-8"))
    pm_paths.regression_flag(pm_paths.home_at(release.board.REPO)).write_text(json.dumps(core), encoding="utf-8")
    monkeypatch.setattr(regression, "_run_regression_cmd", run)
    from test_board_livegate import _t0936_record, _t0936_check
    assert _t0936_record(release) == 0 and _t0936_check(release) == 0
    assert [command for command, _cwd, _env in calls] == [full_test_cmd, platform_cmd]
    assert calls[1][2]["PM_QA_PLATFORM"] == "windows"
    assert calls[1][2]["PM_QA_EXPECTED_HEAD"] == expected_head

    core_argv = shlex.split(calls[0][0])
    assert "tests/" in core_argv
    assert not any(token.startswith("tests/test_") for token in core_argv)
    assert not ({"-k", "--ignore", "--ignore-glob"} & set(core_argv))
    assert (REPO / "tests/test_additional_reviewer.py").is_file(), (
        "full tests/ 수집에 포함돼야 할 개칭된 reviewer suite가 없다"
    )


_RELEASE_SKILLS = tuple(Path(str(path).replace("pm-qa", "pm-release")) for path in SKILLS)
_REGRESSION_SKILLS = tuple(Path(str(path).replace("pm-qa", "pm-regression")) for path in SKILLS)


def test_t0936_shipped_surfaces_share_phase_contract():
    for path in SKILLS:
        text = path.read_text(encoding="utf-8")
        assert "현재 플랫폼" in text and "wrapper를 호출하지 않는다" in text, path
        assert "릴리즈 검증 예정" in text and "개발 완료 차단 사유가 아니다" in text, path
        assert "livegate record" in text and "livegate check" in text, path
        assert "같은 rev" in text and "현재 선언·명령" in text, path
    for path in _RELEASE_SKILLS:
        text = path.read_text(encoding="utf-8")
        assert "선언 순서대로 실제 실행" in text and "이전 green" in text, path
        assert "PM_ORCH_LIVE_RELEASE=1" in text and "--cwd" in text, path
        assert "사용자 승인" in text and "pin" in text, path
    test_shipped_notation_and_details_document_the_wrapper_protocol()
    test_shipped_pm_qa_contract_contains_no_machine_specific_vm_or_ssh_details()


def test_t0936_background_other_tree_and_completion_boundary():
    for path in (*SKILLS, *_REGRESSION_SKILLS, *_RELEASE_SKILLS):
        text = path.read_text(encoding="utf-8")
        assert "백그라운드" in text and "다른 작업 트리" in text, path
        assert "완료·통합" in text and ("고정" in text or "동결" in text), path
        assert "릴리즈" in text and "플랫폼" in text, path
    for path in SKILLS:
        text = path.read_text(encoding="utf-8")
        assert "pending" in text and "완료된 전체 core green" in text, path
        assert "새 플랫폼 선택 옵션이나 별도 작업 큐를 만들지 않는다" in text, path
        assert "regression run --final" in text and "regression check" in text, path
