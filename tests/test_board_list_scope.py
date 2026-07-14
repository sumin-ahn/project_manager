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


def _seed(board, tid, status, *, claimed_by=None, created_by=None, title="t"):
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "created_by": created_by, "claimed_by": claimed_by,
                             "depends_on": [], "tags": []}, "# seed\n")
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


def test_session_filter_includes_open_when_no_area_owner_solo(board, capsys):
    """**solo(distinct user ≤1)에서만** area_owner 미운영 → 전체 open degrade (T-0302·ADR-0053).

    이 보드엔 소유가 실린 티켓이 하나뿐(distinct user ≤1)이라 solo — degrade 로 open 표시가 맞다.
    ⚠ 예전 이 단언은 degrade 자체를 '정답'으로 박제해 다중사용자 유출 버그를 가렸다. 이제 solo
    조건을 명시하고, 다중사용자 seed 는 아래 strict-exclude 테스트가 별도로 못박는다."""
    _seed(board, "T-0003", "open")   # 소유 미상·유일 티켓 → distinct user 0 = solo
    ids = _list_ids(board, capsys, session="myproject_3")
    assert ids == ["T-0003"]


def test_session_filter_multi_user_excludes_unowned_open(board, capsys):
    """다중사용자(distinct ≥2)면 solo degrade 가 확장되지 않는다 — 소유 미해소 open strict-exclude.

    T-0302 근절: 옛 `--session` 은 my_user 를 항상 None 으로 둬 area_owner 미운영 시 전체 open 을
    노출했다(타 사용자 미claim open 유출). alice/bob 두 사용자가 created_by 로 잡히면 다중사용자
    신호가 서고, my_user 를 유도할 areas 가 없어도 미해소 open 은 제외된다."""
    _seed(board, "T-0003", "open", created_by="alice/myproject_3")
    _seed(board, "T-0004", "open", created_by="bob/other_9")
    ids = _list_ids(board, capsys, session="myproject_3")
    assert "T-0004" not in ids   # 타 사용자 미claim open 유출 차단(ADR-0053)


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


# ════════════════════════════════════════════════════════════════════════
# CLI --help 위생 (T-0248·ADR-0043/ADR-0042) — ticket 인자 metavar `T-NNNN`,
# `list --session` 뷰/actor 구분 문구, `new --prefix` 카테고리 help.
# ════════════════════════════════════════════════════════════════════════

# ticket id 를 받는 서브커맨드 전건 (idea promote/kill 은 idea-ID 라 제외).
_TICKET_ID_SUBCOMMANDS = (
    "show", "claim", "complete", "block", "unclaim", "unblock", "promote",
)


def _fresh_parser():
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_help_cli", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_parser()


def _subparsers_action(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparsers action on parser")


def _subparser(parser, name):
    return _subparsers_action(parser).choices[name]


@pytest.mark.parametrize("sub", _TICKET_ID_SUBCOMMANDS)
def test_ticket_id_arg_uses_t_nnnn_metavar(sub):
    """claim/complete/show/… 의 ticket 인자 usage 는 bare `id` 아닌 `T-NNNN` metavar."""
    usage = _subparser(_fresh_parser(), sub).format_usage()
    assert "T-NNNN" in usage, f"{sub} usage 에 T-NNNN metavar 없음: {usage!r}"
    # bare positional `id` 토큰이 metavar 로 남아있지 않다 (공백 경계 매칭).
    assert " id" not in usage.rsplit("[-h]", 1)[-1], (
        f"{sub} usage 에 bare `id` metavar 잔존: {usage!r}")


def test_idea_id_arg_keeps_plain_metavar():
    """idea promote/kill 은 ticket 이 아니라 idea-ID — T-NNNN metavar 를 붙이지 않는다."""
    idea = _subparser(_fresh_parser(), "idea")
    for verb in ("promote", "kill"):
        usage = _subparser(idea, verb).format_usage()
        assert "T-NNNN" not in usage, f"idea {verb} 에 ticket metavar 오적용: {usage!r}"


def test_list_session_help_distinguishes_view_from_actor():
    """`list --session` help 는 뷰 렌즈 ↔ actor `--session` 이 별개임을 명시한다 (ADR-0043)."""
    list_parser = _subparser(_fresh_parser(), "list")
    session_help = None
    for action in list_parser._actions:
        if action.dest == "session":
            session_help = action.help
            break
    assert session_help is not None, "list --session action 이 없다"
    assert "뷰 렌즈" in session_help
    assert "actor" in session_help
    assert "별개" in session_help


def test_new_prefix_help_frames_as_category_not_namespace():
    """`new --prefix` help 는 ADR-0042 작업 카테고리로 재framing — 'namespace' 잔재 없음."""
    new_parser = _subparser(_fresh_parser(), "new")
    prefix_help = None
    for action in new_parser._actions:
        if action.dest == "prefix":
            prefix_help = action.help
            break
    assert prefix_help is not None, "new --prefix action 이 없다"
    assert "작업 카테고리" in prefix_help
    assert "namespace" not in prefix_help.lower(), (
        f"ADR-0042 재정의 후에도 namespace 잔재: {prefix_help!r}")
