"""엔진 도구 스모크 테스트 — canonical 엔진(루트 .project_manager/tools/)을 직접 검증.

도구들이 패키지가 아니므로 importlib 로 경로 로드한다. 무거운 외부 호출 없이
순수 로직(파싱·필터·status 갱신)만 본다.
"""
from __future__ import annotations

import importlib.util
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
def external_review():
    return _load("external_review")


# ── import + 핵심 심볼 존재 (R14·R15·R16·R17) ──────────────────────────

def test_board_exposes_lint_seams(board):
    assert callable(board.lint_wikilinks)        # R14
    assert callable(board._run_lint_hooks)       # R15
    assert callable(board.prompt_external_review_optin)  # R17 opt-in


def test_ticket_finish_pytest_parser_seams(ticket_finish):
    # ADR-0023(T-0103): status 스칼라 갱신 단계 제거 — ticket_finish 의 남은 순수 로직
    # (회귀 green 게이트·pytest 출력 파서)만 노출 확인. update_status/status_total_style 제거.
    assert callable(ticket_finish.parse_pytest_output)
    assert callable(ticket_finish.is_pytest_green)
    assert not hasattr(ticket_finish, "update_status")
    assert not hasattr(ticket_finish, "status_total_style")


def test_external_review_symbols(external_review):
    for sym in ("run_review", "parse_verdict", "filter_secret_hunks", "build_prompt"):
        assert callable(getattr(external_review, sym))


# ── 순수 로직 (R16·R17) ─────────────────────────────────────────────────

def test_pytest_output_parse_green(ticket_finish):
    # status 갱신 대신 — green 게이트·파서 순수 로직 (status.md = judgment-only·ADR-0023).
    assert ticket_finish.parse_pytest_output("12 passed, 3 deselected in 1s") == (12, 3)
    assert ticket_finish.is_pytest_green("12 passed in 1s", returncode=0) is True
    assert ticket_finish.is_pytest_green("1 failed, 11 passed in 1s", returncode=1) is False


def test_verdict_and_exit(external_review):
    def mock(output, rc=0):
        def run_fn(argv, **kw):
            return subprocess.CompletedProcess(argv, rc, stdout=output, stderr="")
        return run_fn

    r = external_review.run_review("p", reviewer_cmd="x", run_fn=mock("판정: 통과\n\n**must-fix**:\n- 없음\n"))
    assert r["all_pass"] and external_review.determine_exit_code(r) == 0

    r = external_review.run_review("p", reviewer_cmd="x", run_fn=mock("판정: 반려\n\n**must-fix**:\n- foo\n"))
    assert r["any_must_fix"] and external_review.determine_exit_code(r) == 1

    r = external_review.run_review("p", reviewer_cmd="x", run_fn=mock("boom", rc=1))
    assert r["failed"] and external_review.determine_exit_code(r) == 1


def test_secret_denylist(external_review):
    diff = "diff --git a/x.py b/x.py\n+ok\ndiff --git a/.env b/.env\n+SECRET=1\n"
    filtered, excluded = external_review.filter_secret_hunks(diff, external_review._SECRET_DENYLIST_PATTERNS)
    assert excluded == [".env"] and "SECRET" not in filtered
