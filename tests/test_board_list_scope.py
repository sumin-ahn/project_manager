"""`board list --session/--slot` 필터 + 기본뷰 done 접기 단위 테스트 (T-0197).

`list` 가 `--session`/`--slot` 을 거부하던 argparse 에러(opencode PM 실증)를 없애고,
`--mine` 과 같은 (a) 내 area open ∨ (b) 내 claim 렌즈를 **명시 식별자**로 돌린다. +
기본 status 뷰는 활성만(open/claimed/blocked) — done 은 `--status all`(또는 `--status done`)
에서만 보인다(done 184개 범람 해소).

이 파일이 검증하는 계약:
  1. `--session NAME` — 그 세션 이름의 open+claim (완전 일치).
  2. `--slot N` — slot 규약(`<repo>_<N>`) suffix 매칭.
  3. `--status` 셀렉터 — 기본=활성만 · `all`=전체(done 포함) · 특정값=그것만(기존 동작).
  4. argparse 가 `--session`/`--slot`/`--status all`/`--mine` 모두를 에러 없이 받는다
     (opencode PM 실증 회귀 방지) + `--mine`/`--session`/`--slot` 상호 배타.

hermetic 패턴은 `test_board_mine_view.py` 와 동형 — board.py 의 경로 전역을 tmp 프로젝트로
monkeypatch 하고 git 폴백은 stub 한다.
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
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)


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


def _seed(board, tid, status, *, claimed_by=None, title="t"):
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "claimed_by": claimed_by, "depends_on": [],
                             "tags": []}, "# seed\n")
    return path


def _list_ids(board, capsys, **flags) -> list[str]:
    args = argparse.Namespace(status=flags.get("status"), tag=flags.get("tag"),
                              mine=flags.get("mine", False),
                              session=flags.get("session"), slot=flags.get("slot"))
    rc = board.cmd_list(args)
    assert rc == 0
    out = capsys.readouterr().out
    ids = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            ids.append(line.split("]", 1)[1].split()[0])
    return ids


# ════════════════════════════════════════════════════════════════════════
# argparse — --session/--slot 에러 부재 (opencode PM 실증 회귀 방지)
# ════════════════════════════════════════════════════════════════════════

def test_list_session_flag_parses_without_error():
    parser = None
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    args = parser.parse_args(["list", "--session", "myproject_3"])
    assert args.session == "myproject_3"
    assert args.cmd == "list"


def test_list_slot_flag_parses_without_error():
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli2", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    args = parser.parse_args(["list", "--slot", "3"])
    assert args.slot == 3


def test_list_status_all_flag_parses_without_error():
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli3", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    args = parser.parse_args(["list", "--status", "all"])
    assert args.status == "all"


def test_list_session_and_slot_mutually_exclusive():
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli4", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--session", "a", "--slot", "1"])


def test_list_mine_and_session_mutually_exclusive():
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli5", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--mine", "--session", "a"])


# ════════════════════════════════════════════════════════════════════════
# --session NAME — 그 세션 이름의 open+claim (완전 일치)
# ════════════════════════════════════════════════════════════════════════

def test_session_filter_includes_matching_claim(board, capsys):
    _seed(board, "T-0001", "claimed", claimed_by="alice/myproject_3")
    _seed(board, "T-0002", "claimed", claimed_by="bob/myproject_9")
    ids = _list_ids(board, capsys, session="myproject_3")
    assert ids == ["T-0001"]


def test_session_filter_includes_open_when_no_area_owner(board, capsys):
    """area_owner 미운영(솔로/미마이그) → (a) 는 전체 open 으로 degrade(--mine 과 동형)."""
    _seed(board, "T-0003", "open")
    ids = _list_ids(board, capsys, session="myproject_3")
    assert ids == ["T-0003"]


def test_session_filter_legacy_slot_only_claim(board, capsys):
    """legacy 슬롯-only claim(`claimed_by=<slot>`)도 --session 완전 일치로 잡힌다."""
    _seed(board, "T-0004", "claimed", claimed_by="myproject_3")
    ids = _list_ids(board, capsys, session="myproject_3")
    assert ids == ["T-0004"]


# ════════════════════════════════════════════════════════════════════════
# --slot N — slot 규약(<repo>_<N>) suffix 매칭
# ════════════════════════════════════════════════════════════════════════

def test_slot_filter_matches_repo_prefixed_session(board, capsys):
    _seed(board, "T-0005", "claimed", claimed_by="alice/myproject_3")
    _seed(board, "T-0006", "claimed", claimed_by="alice/otherproj_3")
    ids = _list_ids(board, capsys, slot=3)
    assert set(ids) == {"T-0005", "T-0006"}


def test_slot_filter_does_not_match_different_number(board, capsys):
    _seed(board, "T-0007", "claimed", claimed_by="alice/myproject_3")
    ids = _list_ids(board, capsys, slot=9)
    assert ids == []


def test_slot_filter_matches_legacy_pure_number_slot(board, capsys):
    """slot 토큰이 순수 숫자(레거시)면 `--slot N` 완전 일치로도 잡힌다."""
    _seed(board, "T-0008", "claimed", claimed_by="alice/3")
    ids = _list_ids(board, capsys, slot=3)
    assert ids == ["T-0008"]


# ════════════════════════════════════════════════════════════════════════
# 기본뷰 done 접기 + --status all/특정값
# ════════════════════════════════════════════════════════════════════════

def test_default_view_hides_done(board, capsys):
    _seed(board, "T-0010", "open")
    _seed(board, "T-0011", "claimed", claimed_by="a/b")
    _seed(board, "T-0012", "blocked")
    _seed(board, "T-0013", "done", claimed_by="a/b")
    ids = _list_ids(board, capsys)
    assert set(ids) == {"T-0010", "T-0011", "T-0012"}


def test_status_all_shows_done(board, capsys):
    _seed(board, "T-0014", "open")
    _seed(board, "T-0015", "done", claimed_by="a/b")
    ids = _list_ids(board, capsys, status="all")
    assert set(ids) == {"T-0014", "T-0015"}


def test_status_done_still_works(board, capsys):
    """기존 `--status done`(특정 status 하나만) 동작은 무변경."""
    _seed(board, "T-0016", "open")
    _seed(board, "T-0017", "done", claimed_by="a/b")
    ids = _list_ids(board, capsys, status="done")
    assert ids == ["T-0017"]


def test_default_view_empty_when_only_done(board, capsys):
    _seed(board, "T-0018", "done", claimed_by="a/b")
    ids = _list_ids(board, capsys)
    assert ids == []
