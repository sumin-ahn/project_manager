"""엔진 도구 스모크 테스트 — canonical 엔진(루트 .project_manager/tools/)을 직접 검증.

도구들이 패키지가 아니므로 importlib 로 경로 로드한다. 무거운 외부 호출 없이
순수 로직(파싱·필터·status 갱신)만 본다.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def board():
    return _load("board")


@pytest.fixture(scope="module")
def ticket_finish():
    return _load("ticket_finish")


@pytest.fixture(scope="module")
def additional_reviewer():
    return _load("additional_reviewer")


# ── import + 핵심 심볼 존재 (R14·R15·R16·R17) ──────────────────────────

def test_board_exposes_lint_seams(board):
    assert callable(board.lint_wikilinks)        # R14
    assert callable(board._run_lint_hooks)       # R15


def test_ticket_finish_pytest_parser_seams(ticket_finish):
    # ADR-0023(T-0103): status 스칼라 갱신 단계 제거 — ticket_finish 의 남은 순수 로직
    # (회귀 green 게이트·pytest 출력 파서)만 노출 확인. update_status/status_total_style 제거.
    assert callable(ticket_finish.parse_pytest_output)
    assert callable(ticket_finish.is_pytest_green)
    assert not hasattr(ticket_finish, "update_status")
    assert not hasattr(ticket_finish, "status_total_style")


def test_additional_reviewer_symbols(additional_reviewer):
    for sym in ("run_review", "parse_verdict", "extract_diff", "build_prompt"):
        assert callable(getattr(additional_reviewer, sym))


# ── 순수 로직 (R16·R17) ─────────────────────────────────────────────────

def test_pytest_output_parse_green(ticket_finish):
    # status 갱신 대신 — green 게이트·파서 순수 로직 (status.md = judgment-only·ADR-0023).
    assert ticket_finish.parse_pytest_output("12 passed, 3 deselected in 1s") == (12, 3)
    assert ticket_finish.is_pytest_green("12 passed in 1s", returncode=0) is True
    assert ticket_finish.is_pytest_green("1 failed, 11 passed in 1s", returncode=1) is False


def test_verdict_and_exit(additional_reviewer, tmp_path):
    def mock(output, rc=0):
        def run_fn(argv, **kw):
            return subprocess.CompletedProcess(argv, rc, stdout=output, stderr="")
        return run_fn

    def reply(text):
        """codex 구조화 wire 의 최종 회신 이벤트 — 판정 파서의 유일한 입력."""
        return json.dumps({"type": "item.completed",
                           "item": {"type": "agent_message", "text": text}},
                          ensure_ascii=False) + "\n"

    # 실행 대상은 해소된 구조화 tuple 하나다(모델 미고정 커맨드 직접 실행 경로는 폐지).
    target = additional_reviewer.resolve_reviewer_target({
        "additional_reviewer.harness": "codex",
        "additional_reviewer.model": "gpt-5.6-sol",
    })

    r = additional_reviewer.run_review(
        "p", target=target, output_dir=tmp_path,
        run_fn=mock(reply("판정: 통과\n\n**must-fix**:\n- 없음\n")),
    )
    assert r["all_pass"] and additional_reviewer.determine_exit_code(r) == 0

    r = additional_reviewer.run_review(
        "p", target=target, output_dir=tmp_path,
        run_fn=mock(reply("판정: 반려\n\n**must-fix**:\n- foo\n")),
    )
    assert r["any_must_fix"] and additional_reviewer.determine_exit_code(r) == 1

    r = additional_reviewer.run_review(
        "p", target=target, output_dir=tmp_path, run_fn=mock("boom", rc=1),
    )
    assert r["failed"] and additional_reviewer.determine_exit_code(r) == 1


