"""구 역할 절(명세 본문 marker) 탐지 시야 — `legacy-growth-section` lint.

라운드 사이드카 이전 board 는 역할 산출을 명세 본문 안에 marker 로 감싸 누적했다
(`pm-ticket-section` + 봉인 줄). 변환 명령(`board.py rounds migrate`)은 v1.8.1 까지 있었고 이
엔진에는 없다. 남은 절은 `legacy-growth-section` lint 가 차단으로 보인다. 이 파일은 그 판정
시야(column 0 주석 줄 · ``` 펜스 밖)가 산문 인용·들여쓰기·문법 예시를 데이터로 읽지 않는지와,
신규 board 의 배포 선언에 구 성장 장부가 없는지를 잰다. 차단 여부·안내 문구는
`tests/test_board_lint.py` 의 legacy-growth-section 시험이 본다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from conftest import anchor_board_module
# 티켓 전문 조립은 라운드 예약 테스트의 헬퍼를 그대로 쓴다 — 두 벌 두면 한쪽만 현재화된다.
from test_board_ticket_growth import _ticket_text
from pm_engine.app import ticket_lint

from pm_engine.domain import board_git_rules
from pm_engine.infra import pm_paths

REPO = Path(__file__).resolve().parents[1]
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"

_SEAL_SHA = "0" * 64


def _load_board():
    spec = importlib.util.spec_from_file_location("board_rounds_migrate", BOARD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_section(role: str, ordinal: int, *, body: str) -> str:
    """구 형식 절 블록(marker·봉인 줄 포함)을 낸다."""
    return (
        f"<!-- pm-ticket-section:start role={role} -->\n"
        f"## {role} ({role} · 2026-08-17)\n"
        "\n"
        f"{body}\n"
        f"<!-- pm-ticket-section:end role={role} -->\n"
        f"<!-- pm-ticket-seal role={role} ordinal={ordinal} "
        f"sha256={_SEAL_SHA} by=harvest -->\n"
    )


def test_legacy_markers_inside_a_fenced_code_block_are_not_data():
    """``` 로 감싼 marker 예시(문법을 문서화한 티켓)는 lint 판정에서 빠진다.

    문서 티켓이 실 문법을 예시로 펜스에 넣으면, column-0 판정만으로는 그 예시가 진짜 절로
    읽혀 lint 가 영영 red 로 남는다(F-003).
    """
    board = _load_board()
    body = (
        "## 문법 예시\n"
        "```\n"
        "<!-- pm-ticket-section:start role=developer -->\n"
        "산출.\n"
        "<!-- pm-ticket-section:end role=developer -->\n"
        "```\n"
    )

    assert ticket_lint._has_legacy_growth_markers(body) is False


def test_shipped_gitattributes_declarations_no_longer_seed_the_growth_ledger():
    """신규 board 는 처음부터 라운드 레이아웃이다 — 배포 선언에 장부 union 이 없다."""
    board = _load_board()
    pm_import_source = (
        REPO / ".project_manager" / "tools" / "pm_import.py"
    ).read_text(encoding="utf-8")

    assert "tickets/.growth" not in board_git_rules._BOARD_GITATTRIBUTES_BLOCK
    assert "tickets/.growth" not in pm_import_source


def test_board_lint_skips_quoted_indented_and_fenced_markers(tmp_path, monkeypatch):
    """인용/들여쓰기/펜스 marker 는 0건, column 0 의 진짜 marker 는 1건이다(F-001).

    실 board T-0694 처럼 들여쓰기+backtick 인용된 marker 와 펜스 예시를 섞어 lint 의
    **판정 결과**를 잰다.
    """
    board = _load_board()
    anchor_board_module(board, tmp_path, monkeypatch)
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "done"
    tickets.mkdir(parents=True)

    quoted_body = (
        "## 문법 설명\n"
        "구 형식은 이렇게 생겼다:\n\n"
        "  `<!-- pm-ticket-seal role=developer ordinal=0 "
        f"sha256={_SEAL_SHA} by=harvest -->`\n"
    )
    (tickets / "T-4200-quoted.md").write_text(
        _ticket_text("T-4200", "done") + "\n" + quoted_body,
        encoding="utf-8", newline="")

    real_block = _legacy_section("developer", 0, body="산출.")
    (tickets / "T-4201-real.md").write_text(
        _ticket_text("T-4201", "done") + "\n" + real_block,
        encoding="utf-8", newline="")
    fenced_body = "## 문법 예시\n\n```\n" + real_block + "```\n"
    (tickets / "T-4202-fenced.md").write_text(
        _ticket_text("T-4202", "done") + "\n" + fenced_body,
        encoding="utf-8", newline="")

    board_findings = sorted(
        name for name, _kind, _detail in ticket_lint.lint_legacy_growth_sections(pm_paths.home_at(board.REPO)))

    assert board_findings == ["T-4201"], "인용/들여쓰기/펜스 marker 가 board lint 에 잡혔다"
