"""`board list --repo/--slot` 필터 + 기본뷰 done 접기 단위 테스트 (T-0197·ADR-0056 user-first·
ADR-0057 decomposed 인자 통일·T-0314).

`list` 가 `--repo`/`--slot`(decomposed·ADR-0057) 을 거부하지 않고, **현재 사용자 ∩ 그 슬롯**(claim:
user AND slot·open: 슬롯무관 내 backlog·ADR-0056) 렌즈를 **명시 식별자**로 돌린다 — querying identity
는 항상 현재 사용자(local.conf user=)다. + 기본 status 뷰는 활성만(open/claimed/blocked) — done 은
`--status all`(또는 `--status done`)에서만 보인다.

이 파일이 검증하는 계약:
  1. `--repo X --slot N`(kind=slot) — 현재 사용자 ∩ 그 세션(user AND slot claim·완전 일치·구
     `--session <repo>_<N>` 과 동형). 타 사용자 무유출.
  2. `--repo X`(kind=repo·슬롯 무) — 현재 사용자 ∩ **그 repo 의 내 슬롯 전체**(prefix 매칭·신규
     repo-scope 뷰 — 구 bare `--slot N`[cross-repo suffix 매칭]을 대체).
  3. `--slot N` 단독(`--repo` 없음) — fail-loud(ADR-0057 결정 2·uniform·solo 예외 없음).
  4. `--status` 셀렉터 — 기본=활성만 · `all`=전체(done 포함) · 특정값=그것만(기존 동작).
  5. argparse 가 `--repo`/`--slot`(함께)/`--status all`/`--mine` 모두를 에러 없이 받는다
     (opencode PM 실증 회귀 방지 계승) + `--mine`/`--repo` 상호 배타(cmd_list 런타임 검사 — 두 개
     플래그[`--repo`+`--slot`]가 함께 필요해 argparse mutex group 을 못 쓴다).

hermetic 패턴은 `test_board_mine_view.py` 와 동형 — board.py 의 경로 전역을 tmp 프로젝트로
monkeypatch 하고 git 폴백은 stub 한다. querying identity 는 local.conf `user=` 로 명시한다.
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


def _write_conf(board, **kv) -> None:
    """local.conf 를 쓴다 — querying identity(user=) 명시용 (ADR-0056 user-first)."""
    board.LOCAL_CONF.write_text(
        "".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")


def _seed(board, tid, status, *, claimed_by=None, created_by=None, title="t"):
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "created_by": created_by, "claimed_by": claimed_by,
                             "depends_on": [], "tags": []}, "# seed\n")
    return path


def _list_ids(board, capsys, **flags) -> list[str]:
    args = argparse.Namespace(status=flags.get("status"), tag=flags.get("tag"),
                              mine=flags.get("mine", False),
                              all=flags.get("all", False), task=flags.get("task"),
                              repo=flags.get("repo"), slot=flags.get("slot"))
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

def test_list_repo_and_slot_flags_parse_together():
    """`--repo X --slot N`(kind=slot) — 함께 줘도(구 `--session`/bare `--slot` 과 달리) 에러 없다."""
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    args = parser.parse_args(["list", "--repo", "myproject", "--slot", "3"])
    assert args.repo == "myproject"
    assert args.slot == 3
    assert args.cmd == "list"


def test_list_slot_flag_parses_at_argparse_level_without_repo():
    """`--slot 3`(단독) 은 argparse 레벨에선 에러 없이 파싱된다 — "`--repo` 필수" 검증은
    `parse_identity`/`cmd_list` 런타임 몫(카드↔CLI 정합은 유지하되 검사는 한 곳에 모은다)."""
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli2", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    args = parser.parse_args(["list", "--slot", "3"])
    assert args.slot == 3
    assert args.repo is None


def test_list_status_all_flag_parses_without_error():
    import importlib.util as _il
    spec = _il.spec_from_file_location("board_cli3", TOOLS / "board.py")
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()
    args = parser.parse_args(["list", "--status", "all"])
    assert args.status == "all"


def test_list_slot_alone_without_repo_fails_loud(board):
    """해소 규칙 3케이스 — `--slot N` 단독(`--repo` 없음) 은 `cmd_list` 가 fail-loud(ADR-0057 결정 2)."""
    with pytest.raises(SystemExit) as exc:
        board.cmd_list(argparse.Namespace(status=None, tag=None, mine=False,
                                          repo=None, slot=3))
    assert "--repo" in str(exc.value)


def test_list_mine_and_repo_mutually_exclusive_at_dispatch(board):
    """`--mine` 과 `--repo`(+`--slot`) 는 상호 배타 — decomposed 두 플래그가 함께 필요해(kind=slot)
    argparse mutex group 을 못 쓰므로 `cmd_list` 런타임에서 거부한다(구 argparse-level mutex 대체)."""
    with pytest.raises(SystemExit) as exc:
        board.cmd_list(argparse.Namespace(status=None, tag=None, mine=True,
                                          repo="a", slot=None))
    assert "--mine" in str(exc.value)


# ════════════════════════════════════════════════════════════════════════
# --repo X --slot N (kind=slot) — 현재 사용자 ∩ 그 세션(완전 일치·구 --session 동형)
# ════════════════════════════════════════════════════════════════════════

def test_repo_slot_filter_includes_my_claim_in_that_slot(board, capsys):
    """현재 사용자(alice) ∩ 슬롯 — 내 그-슬롯 claim 만(user AND slot). 남의(bob) claim 무포함."""
    _write_conf(board, user="alice")
    _seed(board, "T-0001", "claimed", claimed_by="alice/myproject_3")   # 내 것·그 슬롯
    _seed(board, "T-0002", "claimed", claimed_by="bob/myproject_9")     # 남의 것 → 제외
    ids = _list_ids(board, capsys, repo="myproject", slot=3)
    assert ids == ["T-0001"]


def test_repo_slot_view_claim_axis_is_session_label_user_agnostic(board, capsys):
    """**ADR-0067 (codex R2)**: `--repo X --slot N` 세션 뷰의 claim 축 = session 라벨(user 무관·open
    생성-세션과 대칭). 같은 슬롯의 타 user claim(`bob/myproject_3`)도 세션이 일치하면 보인다.

    user 필터(내 것만)는 `--mine` 렌즈 몫이다 — 세션 뷰와 축이 다름을 대비로 못박는다."""
    _write_conf(board, user="alice")
    _seed(board, "T-0001", "claimed", claimed_by="alice/myproject_3")   # 내 세션 claim
    _seed(board, "T-0002", "claimed", claimed_by="bob/myproject_3")     # 타 user·같은 세션 → 보임(라벨)
    ids = _list_ids(board, capsys, repo="myproject", slot=3)
    assert set(ids) == {"T-0001", "T-0002"}
    # 대비: --mine(user 축)은 alice 것만 — bob claim 은 user 불일치로 제외(축이 다름).
    mine = _list_ids(board, capsys, mine=True)
    assert set(mine) == {"T-0001"}


def test_repo_slot_filter_open_is_created_session_stream(board, capsys):
    """`--repo X --slot N` open = 그 세션(myproject_3) **생성분만** (ADR-0067 생성-세션 스트림).

    세션 뷰(kind=slot)는 `_in_default_view`(생성-세션 스트림)를 탄다 — 옛 user-단위/all-open degrade 는
    무바인딩 default 뷰에만 남고, 명시 세션 뷰엔 안 쓴다. 세션 부재 created_by(legacy·backfill 대상)나
    타 슬롯 생성 open 은 비노출."""
    _seed(board, "T-0003", "open", created_by="alice/myproject_3")   # 그 세션 생성 → 상세
    _seed(board, "T-0005", "open")                                   # created_by 부재 → 비노출(backfill 대상)
    _seed(board, "T-0006", "open", created_by="alice/myproject_9")   # 타 슬롯 생성 → 비노출
    ids = _list_ids(board, capsys, repo="myproject", slot=3)
    assert ids == ["T-0003"]


def test_repo_slot_filter_multi_user_excludes_unowned_open(board, capsys):
    """다중사용자(distinct ≥2)면 solo degrade 가 확장되지 않는다 — 소유 미해소 open strict-exclude.

    T-0302 근절: 옛 `--session` 은 my_user 를 항상 None 으로 둬 area_owner 미운영 시 전체 open 을
    노출했다(타 사용자 미claim open 유출). alice/bob 두 사용자가 created_by 로 잡히면 다중사용자
    신호가 서고, my_user 를 유도할 areas 가 없어도 미해소 open 은 제외된다."""
    _seed(board, "T-0003", "open", created_by="alice/myproject_3")
    _seed(board, "T-0004", "open", created_by="bob/other_9")
    ids = _list_ids(board, capsys, repo="myproject", slot=3)
    assert "T-0004" not in ids   # 타 사용자 미claim open 유출 차단(ADR-0053)


def test_repo_slot_filter_legacy_slot_only_claim(board, capsys):
    """legacy 슬롯-only claim(`claimed_by=<slot>`)도 `--repo X --slot N` 완전 일치로 잡힌다."""
    _seed(board, "T-0004", "claimed", claimed_by="myproject_3")
    ids = _list_ids(board, capsys, repo="myproject", slot=3)
    assert ids == ["T-0004"]


def test_repo_slot_filter_cross_repo_same_number_does_not_leak(board, capsys):
    """**BREAKING 확인**: `--repo myproject --slot 3` 은 `otherproj_3`(다른 repo·같은 번호) 를
    안 끌어온다 — 구 bare `--slot N`(cross-repo suffix 매칭)이 ADR-0057 로 제거된 결과다."""
    _write_conf(board, user="alice")
    _seed(board, "T-0005", "claimed", claimed_by="alice/myproject_3")
    _seed(board, "T-0006", "claimed", claimed_by="alice/otherproj_3")
    ids = _list_ids(board, capsys, repo="myproject", slot=3)
    assert ids == ["T-0005"]


# ════════════════════════════════════════════════════════════════════════
# --repo X (kind=repo·슬롯 무) — 현재 사용자 ∩ 그 repo 의 내 슬롯 전체(prefix 매칭·신규)
# ════════════════════════════════════════════════════════════════════════

def test_repo_alone_view_matches_all_my_slots_in_that_repo(board, capsys):
    """`--repo X` 단독 — 그 repo 의 내 슬롯 전체(어느 N 이든). 다른 repo 는 제외(spike §3.1)."""
    _write_conf(board, user="alice")
    _seed(board, "T-0020", "claimed", claimed_by="alice/myproject_3")
    _seed(board, "T-0021", "claimed", claimed_by="alice/myproject_7")
    _seed(board, "T-0022", "claimed", claimed_by="alice/otherproj_3")   # 다른 repo → 제외
    ids = _list_ids(board, capsys, repo="myproject")
    assert set(ids) == {"T-0020", "T-0021"}


def test_repo_alone_view_excludes_other_user_same_repo(board, capsys):
    """**타 사용자 무유출**: `--repo X` 가 남의 `bob/X_N` claim 을 안 끌어온다(user AND repo-prefix)."""
    _write_conf(board, user="alice")
    _seed(board, "T-0023", "claimed", claimed_by="alice/myproject_3")
    _seed(board, "T-0024", "claimed", claimed_by="bob/myproject_9")     # 남의 것·같은 repo → 제외
    ids = _list_ids(board, capsys, repo="myproject")
    assert ids == ["T-0023"]


def test_repo_alone_view_open_backlog_slot_agnostic(board, capsys):
    """`--repo X` 뷰에서도 open 은 슬롯무관 내 backlog — solo degrade 로 표시(ADR-0056 #3)."""
    _seed(board, "T-0025", "open")   # 소유 미상·유일 티켓 → solo
    ids = _list_ids(board, capsys, repo="myproject")
    assert ids == ["T-0025"]


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


def test_list_repo_slot_uses_canonical_adr0057_wording():
    """`list --repo`/`--slot` help 는 canonical ADR-0057 wording — 구 '뷰 렌즈 ↔ actor 별개'
    문구(ADR-0043 §4)는 폐기됐다: 인자 표면이 전 서브 동형이라 그 구분 자체가 사라졌다."""
    list_parser = _subparser(_fresh_parser(), "list")
    dests = {action.dest: action for action in list_parser._actions}
    assert "session" not in dests, "list 에 구 --session action 잔존 (ADR-0057 grep 잔여 0 위반)"
    assert "repo" in dests, "list --repo action 이 없다"
    assert "slot" in dests, "list --slot action 이 없다"
    assert "ADR-0057" in dests["repo"].help


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
