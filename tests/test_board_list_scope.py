"""`board list` 렌즈 + 기본뷰 done 접기 단위 테스트 (T-0197·ADR-0056 user-first·T-0962).

list 의 렌즈는 `--task`·`--mine`·`--all`·무인자 기본 뷰다 — querying identity 는 항상 현재
사용자(local.conf user=)다. 슬롯·repo 렌즈(`--repo`/`--slot`)는 없다(T-0962: parser 가 받지 않음).
기본 status 뷰는 활성만(open/claimed/blocked) — done 은 `--status all`(또는 `--status done`)에서만
보인다.

이 파일이 검증하는 계약:
  1. `--repo`/`--slot` 은 list 에서 rc 2(unrecognized arguments) — `--task` 렌즈가 대신한다.
  2. `--status` 셀렉터 — 기본=활성만 · `all`=전체(done 포함) · 특정값=그것만(기존 동작).
  3. argparse 가 `--status all`/`--mine` 을 에러 없이 받는다(opencode PM 실증 회귀 방지 계승).

hermetic 패턴은 `test_board_mine_view.py` 와 동형 — board.py 의 경로 전역을 tmp 프로젝트로
monkeypatch 하고 git 폴백은 stub 한다. querying identity 는 local.conf `identity.user=` 로 명시한다.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from pm_engine.infra import lease_store
from pm_engine.infra import ticket_store

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

        "LOG_FILE": wiki / "log" / "current.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": pm / ".local",

    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lease_store, "_git_config_email", lambda _home: None)
    return mod


# 테스트 kwargs 는 python 식별자라 dot 표기를 담을 수 없다 — conf 키로 옮겨 쓴다
# (`user=` → `identity.user=`). 구표기로 쓰면 conf 를 읽는 지점이 fail-loud 로 멈춘다.
_CONF_KEY_ALIASES = {"user": "identity.user", "py": "runtime.py",
                     "test_cmd": "test.cmd", "upstream": "upstream.path",
                     "project_name": "project.name"}


def _conf_key(name: str) -> str:
    return _CONF_KEY_ALIASES.get(name, name)


def _write_conf(board, **kv) -> None:
    """local.conf 를 쓴다 — querying identity(user=) 명시용 (ADR-0056 user-first)."""
    board.LOCAL_CONF.write_text(
        "".join(f"{_conf_key(k)}={v}\n" for k, v in kv.items()),
        encoding="utf-8")


def _seed(board, tid, status, *, claimed_by=None, created_by=None, title="t"):
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    ticket_store.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "created_by": created_by, "claimed_by": claimed_by,
                             "depends_on": [], "tags": []}, "# seed\n")
    return path


def _list_ids(board, capsys, **flags) -> list[str]:
    args = argparse.Namespace(status=flags.get("status"), tag=flags.get("tag"),
                              mine=flags.get("mine", False),
                              all=flags.get("all", False), task=flags.get("task"))
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
# argparse — --repo/--slot 에러 부재(opencode PM 실증 회귀 방지 계승) + 해소 규칙 3케이스
# ════════════════════════════════════════════════════════════════════════


def test_list_status_all_flag_parses_without_error():
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli3", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    args = parser.parse_args(["list", "--status", "all"])
    assert args.status == "all"


def test_list_slot_alone_without_repo_fails_loud(board):
    """`--slot N` 단독은 거부된다 — list 에는 슬롯·repo 렌즈가 없어 parser 가 받지 않는다(T-0962, rc 2)."""
    with pytest.raises(SystemExit) as exc:
        board.build_parser().parse_args(["list", "--slot", "3"])
    assert exc.value.code == 2


def test_list_mine_and_repo_mutually_exclusive_at_dispatch(board):
    """`--mine` 과 `--repo` 를 함께 줄 수 없다 — list 에는 `--repo` 렌즈가 없어 parser 가 거부한다(T-0962, rc 2)."""
    with pytest.raises(SystemExit) as exc:
        board.build_parser().parse_args(["list", "--mine", "--repo", "a"])
    assert exc.value.code == 2


# ════════════════════════════════════════════════════════════════════════
# --repo X --slot N (kind=slot) — 현재 사용자 ∩ 그 세션(완전 일치·구 --session 동형)
# ════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════
# --repo X (kind=repo·슬롯 무) — 현재 사용자 ∩ 그 repo 의 내 슬롯 전체(prefix 매칭·신규)
# ════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════
# 기본뷰 done 접기 + --status all/특정값
# ════════════════════════════════════════════════════════════════════════

# status 셀렉터는 전체 뷰(`--all`·ADR-0066 로 기존 무인자 전체 뷰 이관)에 적용된다 — 무인자
# 기본은 이제 세션 스코프(내 스트림)라 status 셀렉터 검증은 `--all` 로 돈다(status 축은 뷰 스코프와
# 직교·`--all` 이 done 접기/전체를 그대로 계승).
def test_all_view_hides_done(board, capsys):
    _seed(board, "T-0010", "open")
    _seed(board, "T-0011", "claimed", claimed_by="a/b")
    _seed(board, "T-0012", "blocked")
    _seed(board, "T-0013", "done", claimed_by="a/b")
    ids = _list_ids(board, capsys, all=True)
    assert set(ids) == {"T-0010", "T-0011", "T-0012"}


def test_status_all_shows_done(board, capsys):
    _seed(board, "T-0014", "open")
    _seed(board, "T-0015", "done", claimed_by="a/b")
    ids = _list_ids(board, capsys, all=True, status="all")
    assert set(ids) == {"T-0014", "T-0015"}


def test_status_done_still_works(board, capsys):
    """기존 `--status done`(특정 status 하나만) 동작은 무변경(`--all` 전체 뷰 위에서)."""
    _seed(board, "T-0016", "open")
    _seed(board, "T-0017", "done", claimed_by="a/b")
    ids = _list_ids(board, capsys, all=True, status="done")
    assert ids == ["T-0017"]


def test_all_view_empty_when_only_done(board, capsys):
    _seed(board, "T-0018", "done", claimed_by="a/b")
    ids = _list_ids(board, capsys, all=True)
    assert ids == []


# ════════════════════════════════════════════════════════════════════════
# CLI --help 위생 (T-0248·ADR-0043/ADR-0042·ADR-0057) — ticket 인자 metavar `T-NNNN`,
# `list --repo`/`--slot` canonical 문구, `new --prefix` 카테고리 help.
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


# ════════════════════════════════════════════════════════════════════════
# T-0962 — list 의 렌즈는 `--task`·`--mine`·`--all`·무인자 기본 뷰다(슬롯·repo 렌즈 없음)
# ════════════════════════════════════════════════════════════════════════

def test_t0962_list_has_no_slot_lens(board, capsys):
    """AT-004 — `list --repo proj [--slot 1]` 은 rc 2, `list --task mytask` 는 그 task 의 내 claim 만."""
    _write_conf(board, user="alice")
    _seed(board, "T-0001", "claimed", claimed_by="alice/mytask", created_by="alice/mytask")
    _seed(board, "T-0002", "claimed", claimed_by="bob/other", created_by="bob/other")
    for argv in (["list", "--repo", "proj", "--slot", "1"], ["list", "--repo", "proj"]):
        with pytest.raises(SystemExit) as exc:
            board.main(argv)
        assert exc.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err
    ids = _list_ids(board, capsys, task="mytask")
    assert "T-0001" in ids
    assert "T-0002" not in ids
