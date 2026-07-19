"""board.py tags 타입세이프 전 경로 회귀 (T-0264).

비-문자열 YAML tag(예: `tags: [2026, cleanup]` — YAML 이 `2026` 을 int 로 로드)가
tags 를 다루는 세 표시 경로 중 둘을 **TypeError 로 크래시**시켰다:

  * `cmd_list`            (board.py list — 가장 많이 쓰는 경로)
  * `_refresh_board_locked` (마크다운 board.md 렌더)
  * `cmd_idea_list`      (이미 str() 캐스팅돼 안전 — 회귀로 고정)

과거 수정이 `cmd_idea_list` 한 곳에만 적용되고 나머지 둘은 회귀 테스트 0 으로 남았다.
이 파일은 세 경로 + 두 태그 필터 경로(문자열 `--tag` 를 int 태그와 비교해 조용히 매치
실패)를 모두 못박는다. 출력 문자열 형식(구분자·공백)은 byte 그대로 유지 — 문자열 태그
케이스가 회귀 없음을 확인한다.

hermetic 패턴은 `test_board_list_scope.py`/`test_board_mine_view.py` 와 동형 — board.py 의
경로 전역을 tmp 프로젝트로 monkeypatch 하고 git 폴백은 stub 한다.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_project(root: Path) -> None:
    wiki = root / ".project_manager" / "wiki"
    for status in ("open", "claimed", "blocked", "done"):
        (wiki / "tickets" / status).mkdir(parents=True, exist_ok=True)
    for status in ("open", "promoted", "killed"):
        (wiki / "ideas" / status).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + IO 전역을 tmp 프로젝트로 재지정한 hermetic 인스턴스."""
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load_board()
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "IDEAS_DIR": wiki / "ideas",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": pm / ".local",
        "BOARD_LOCK": pm / ".local" / "board.lock",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    return mod


def _seed_ticket(board, tid, status, *, tags, title="t"):
    """int 를 포함할 수 있는 tags 로 티켓 파일 하나를 심는다.

    dump_ticket 은 yaml.safe_dump → 로드 시 int 는 int 로 라운드트립 보존되므로
    비-문자열 태그를 그대로 재현한다.
    """
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "claimed_by": None, "depends_on": [], "touches": [],
                             "tags": tags}, "# seed\n")
    return path


def _seed_idea(board, iid, status, *, tags, title="t"):
    path = board.IDEAS_DIR / status / f"{iid}-seed.md"
    board.dump_ticket(path, {"id": iid, "title": title, "status": status,
                             "tags": tags}, "# idea\n")
    return path


# `all=True` — 태그 렌더/필터는 뷰 스코프와 직교한 검증이라 전체 뷰(`--all`·ADR-0066)에서 돈다
# (무인자 기본은 이제 세션 스코프라 무관 open 을 접어 단건 seed 가 상세 행으로 안 뜬다).
def _list_args(*, tag=None, status=None):
    return argparse.Namespace(status=status, tag=tag, mine=False,
                              all=True, task=None, repo=None, slot=None)


# ════════════════════════════════════════════════════════════════════════
# cmd_list (board.py list) — 비-문자열 태그 크래시 방지 + 형식 불변
# ════════════════════════════════════════════════════════════════════════

def test_cmd_list_numeric_tags_no_crash(board, capsys):
    """`tags: [2026, cleanup]` 티켓이 list 를 크래시시키지 않고 `2026,cleanup` 표시."""
    _seed_ticket(board, "T-0001", "open", tags=[2026, "cleanup"])
    rc = board.cmd_list(_list_args())
    assert rc == 0
    assert "2026,cleanup" in capsys.readouterr().out


def test_cmd_list_all_numeric_tags_no_crash(board, capsys):
    """전부 숫자인 태그(`[2026, 7]`)도 안전 — 구분자 `,`(공백 없음) 유지."""
    _seed_ticket(board, "T-0002", "open", tags=[2026, 7])
    rc = board.cmd_list(_list_args())
    assert rc == 0
    assert "2026,7" in capsys.readouterr().out


def test_cmd_list_string_tags_unchanged(board, capsys):
    """문자열 태그 경로는 형식 무변경 (byte 그대로·회귀 없음)."""
    _seed_ticket(board, "T-0003", "open", tags=["bug", "crash"])
    rc = board.cmd_list(_list_args())
    assert rc == 0
    assert "bug,crash" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════════════
# 마크다운 board.md 렌더 (_refresh_board_locked) — 비-문자열 태그 크래시 방지
# ════════════════════════════════════════════════════════════════════════

def test_refresh_board_numeric_tags_no_crash(board):
    """open 표에 int 태그 티켓이 있어도 board.md 렌더가 크래시 안 함 — `2026, cleanup` 표기."""
    _seed_ticket(board, "T-0004", "open", tags=[2026, "cleanup"])
    board.refresh_board()
    text = board.BOARD_FILE.read_text(encoding="utf-8")
    # 마크다운 렌더 구분자는 `, `(comma-space) — cmd_list(`,`) 와 다름·둘 다 불변.
    assert "2026, cleanup" in text


def test_refresh_board_string_tags_unchanged(board):
    """문자열 태그 board.md 렌더 형식 무변경."""
    _seed_ticket(board, "T-0005", "open", tags=["bug", "crash"])
    board.refresh_board()
    text = board.BOARD_FILE.read_text(encoding="utf-8")
    assert "bug, crash" in text


# ════════════════════════════════════════════════════════════════════════
# cmd_idea_list — 이미 안전했으나 회귀로 고정 (셋 중 하나만 고쳤던 결함 재발 방지)
# ════════════════════════════════════════════════════════════════════════

def test_cmd_idea_list_numeric_tags_no_crash(board, capsys):
    _seed_idea(board, "0001", "open", tags=[2026, "cleanup"])
    rc = board.cmd_idea_list(argparse.Namespace(status=None, tag=None))
    assert rc == 0
    assert "2026,cleanup" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════════════
# 태그 필터 경로 — 문자열 `--tag` 를 int 태그와 타입세이프 비교 (같은 결함 축)
#   pre-fix: `"2026" in [2026, ...]` → False → 조용히 매치 실패(크래시 아님).
# ════════════════════════════════════════════════════════════════════════

def test_cmd_list_tag_filter_matches_numeric(board, capsys):
    """`--tag 2026` 이 int 태그 `2026` 을 가진 티켓을 매치한다 (타입세이프 비교)."""
    _seed_ticket(board, "T-0006", "open", tags=[2026, "cleanup"])
    rc = board.cmd_list(_list_args(tag="2026"))
    assert rc == 0
    assert "T-0006" in capsys.readouterr().out


def test_cmd_list_tag_filter_string_still_matches(board, capsys):
    """문자열 태그 필터는 기존대로 매치 (회귀 없음)."""
    _seed_ticket(board, "T-0007", "open", tags=["bug", 2026])
    rc = board.cmd_list(_list_args(tag="bug"))
    assert rc == 0
    assert "T-0007" in capsys.readouterr().out


def test_cmd_idea_list_tag_filter_matches_numeric(board, capsys):
    """idea list 의 `--tag` 필터도 int 태그와 타입세이프 비교."""
    _seed_idea(board, "0002", "open", tags=[2026])
    rc = board.cmd_idea_list(argparse.Namespace(status=None, tag="2026"))
    assert rc == 0
    assert "0002" in capsys.readouterr().out
