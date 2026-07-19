"""세션 기본 뷰(무인자 `board list`) + `--all` 단위 테스트 (ADR-0066·T-0385).

ADR-0066: 세션의 기본 화면(무인자 `list`)은 **내 스트림**만 상세로 보이고 무관 open backlog 는
"그 외 open N건" 카운트 1줄로 접는다. 전체 보드는 명시 `--all`(기존 무인자 전체 뷰의 이관).

이 파일이 검증하는 계약:
  1. **내 세션 claim 상세** — claimed_by 가 내 세션(슬롯/task exact)이면 상세, 타 세션 claim 은 skip.
  2. **내 스트림 open 상세** — 현 세션이 task 이고 그 task 가 board prefix 를 지정했으면 그 prefix 의
     open 만 상세, 나머지 open 은 접힘 카운트.
  3. **무스트림 접힘** — 슬롯-모드/무prefix/솔로 단일슬롯은 스트림 없음 → 모든 open 접힘(특례 없음).
  4. **접힘 꼬리 줄** — "그 외 open N건 — 전체는 `board.py list --all`"(N>0 시)·상세 0건이어도 존재.
  5. **`--all`** — 필터 없는 전체 보드(모든 세션·타 사용자)·정체성 무해소·상호 배타.

hermetic 패턴은 `test_board_mine_view.py` 와 동형 — board.py 경로 전역을 tmp 프로젝트로 monkeypatch
하고, 추가로 LEASES_FILE(세션/task prefix 유도)·PM_SESSION_NAME/CLAUDE_SESSION_NAME env 를 통제한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
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
    """hermetic board — 경로 전역 + LEASES_FILE tmp 재지정 + 세션 env 클리어."""
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load_board()
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    (pm / ".local").mkdir(parents=True, exist_ok=True)
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
        "LEASES_FILE": pm / ".local" / "worktree-leases.json",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    # 세션 유도 env 를 실환경에서 격리 — 세션은 local.conf/PM_SESSION_NAME 을 테스트가 명시한다.
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    return mod


def _write_conf(board, **kv) -> None:
    board.LOCAL_CONF.write_text(
        "".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")


def _write_tasks(board, *tasks: dict) -> None:
    """LEASES_FILE 에 top-level `tasks`(name·prefix) 를 박는다 — task_prefix 유도 입력."""
    board.LEASES_FILE.write_text(json.dumps({"tasks": list(tasks)}), encoding="utf-8")


def _seed(board, tid, status, *, claimed_by=None, created_by=None, title="t"):
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "claimed_by": claimed_by, "created_by": created_by,
                             "depends_on": [], "tags": []}, "# seed\n")
    return path


def _run(board, capsys, **flags) -> str:
    """무인자/`--all` cmd_list 를 돌려 전체 stdout 을 반환한다(접힘 꼬리 줄 포함)."""
    args = argparse.Namespace(status=flags.get("status"), tag=flags.get("tag"),
                              mine=flags.get("mine", False),
                              all=flags.get("all", False), task=flags.get("task"),
                              repo=flags.get("repo"), slot=flags.get("slot"))
    rc = board.cmd_list(args)
    assert rc == 0
    return capsys.readouterr().out


def _ids(out: str) -> list[str]:
    ids = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            ids.append(line.split("]", 1)[1].split()[0])
    return ids


_FOLD_TAIL = "전체는 `board.py list --all`"


# ════════════════════════════════════════════════════════════════════════
# ① 내 세션 claim 상세 + 그 외 skip
# ════════════════════════════════════════════════════════════════════════

def test_default_view_shows_my_session_claim(board, capsys):
    """내 세션(project_manager_1) claim 은 상세·타 세션 claim 은 기본 뷰 미표시."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "claimed", claimed_by="alice/project_manager_1")  # 내 세션
    _seed(board, "T-0002", "claimed", claimed_by="bob/project_manager_2")    # 타 세션 → skip
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]


def test_default_view_solo_legacy_slot_claim_shown(board, capsys):
    """솔로 legacy 슬롯-only claim(user 토큰 없음)도 내 세션 exact 매칭이면 상세(not multi_user)."""
    _write_conf(board, session="project_manager_1")   # user 미상(solo)
    _seed(board, "T-0003", "claimed", claimed_by="project_manager_1")   # legacy 슬롯-only·내 슬롯
    out = _run(board, capsys)
    assert _ids(out) == ["T-0003"]


# ════════════════════════════════════════════════════════════════════════
# ② 내 스트림 open (task prefix) + 그 외 open 접힘
# ════════════════════════════════════════════════════════════════════════

def test_default_view_stream_open_shown_for_task_prefix(board, capsys, monkeypatch):
    """현 세션이 task(refactor·prefix=PAY)면 그 prefix 의 open 만 상세·그 외 open 은 접힘."""
    monkeypatch.setenv("PM_SESSION_NAME", "refactor")   # 세션 = task 이름
    _write_tasks(board, {"name": "refactor", "prefix": "PAY"})
    _seed(board, "T-PAY-001", "open")   # 내 스트림 open → 상세
    _seed(board, "T-PAY-002", "open")   # 내 스트림 open → 상세
    _seed(board, "T-ACC-001", "open")   # 타 스트림 open → 접힘
    _seed(board, "T-0009", "open")      # 무prefix open → 접힘
    out = _run(board, capsys)
    assert set(_ids(out)) == {"T-PAY-001", "T-PAY-002"}
    # 그 외 open 2건(T-ACC-001·T-0009) 접힘 꼬리.
    assert "그 외 open 2건" in out and _FOLD_TAIL in out
    assert "T-ACC-001" not in out and "T-0009" not in out


def test_default_view_task_without_prefix_folds_all_open(board, capsys, monkeypatch):
    """task 세션이지만 prefix 미지정이면 스트림 없음 → 모든 open 접힘(무스트림)."""
    monkeypatch.setenv("PM_SESSION_NAME", "refactor")
    _write_tasks(board, {"name": "refactor", "prefix": None})   # prefix 없음
    _seed(board, "T-PAY-001", "open")
    _seed(board, "T-0009", "open")
    out = _run(board, capsys)
    assert _ids(out) == []                       # 상세 0건
    assert "그 외 open 2건" in out and _FOLD_TAIL in out


# ════════════════════════════════════════════════════════════════════════
# ③ 무스트림(슬롯-모드/솔로 단일슬롯) — 모든 open 접힘·특례 없음
# ════════════════════════════════════════════════════════════════════════

def test_default_view_slot_session_folds_all_open(board, capsys):
    """슬롯-모드 세션(`<repo>_<N>`·무-task)은 스트림 없음 → 내 claim 만 상세·open 전부 접힘."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "claimed", claimed_by="alice/project_manager_1")  # 내 claim → 상세
    _seed(board, "T-0002", "open")   # 접힘
    _seed(board, "T-0003", "open")   # 접힘
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]
    assert "그 외 open 2건" in out and _FOLD_TAIL in out
    assert "T-0002" not in out and "T-0003" not in out


def test_default_view_solo_single_slot_folds_open_uniform(board, capsys):
    """**solo 특례 없음**(ADR-0066): 단일슬롯 솔로도 open 은 접힘(무스트림 규칙 동일)."""
    _write_conf(board, session="project_manager_1")   # solo·무-task
    _seed(board, "T-0002", "open")
    out = _run(board, capsys)
    assert _ids(out) == []
    assert "그 외 open 1건" in out and _FOLD_TAIL in out


# ════════════════════════════════════════════════════════════════════════
# ④ 접힘 꼬리 줄 존재/부재 · (no tickets)
# ════════════════════════════════════════════════════════════════════════

def test_default_view_no_fold_line_when_no_other_open(board, capsys):
    """그 외 open 0건이면 접힘 꼬리 줄 없음 — 내 claim 만 있으면 상세만."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "claimed", claimed_by="alice/project_manager_1")
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]
    assert "그 외 open" not in out


def test_default_view_fold_tail_without_any_detail(board, capsys):
    """상세 0건이라도 접힌 backlog 가 있으면 "(no tickets)" 대신 접힘 꼬리 줄을 낸다(유실 방지)."""
    _write_conf(board, session="project_manager_1")
    _seed(board, "T-0002", "open")
    out = _run(board, capsys)
    assert "(no tickets)" not in out
    assert "그 외 open 1건" in out and _FOLD_TAIL in out


def test_default_view_truly_empty_shows_no_tickets(board, capsys):
    """상세도 접힌 open 도 0이면 "(no tickets)"."""
    _write_conf(board, session="project_manager_1")
    _seed(board, "T-0009", "done", claimed_by="bob/project_manager_2")   # done·타 세션 → skip·비-open
    out = _run(board, capsys)
    assert "(no tickets)" in out
    assert "그 외 open" not in out


# ════════════════════════════════════════════════════════════════════════
# ⑤ --all — 전체 보드 · 상호 배타
# ════════════════════════════════════════════════════════════════════════

def test_all_flag_shows_full_board(board, capsys):
    """`--all` = 필터 없는 전체 보드(모든 세션·타 사용자)·접힘 없음."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "claimed", claimed_by="alice/project_manager_1")
    _seed(board, "T-0002", "claimed", claimed_by="bob/project_manager_2")   # 타 세션도 표시
    _seed(board, "T-0003", "open")
    out = _run(board, capsys, all=True)
    assert set(_ids(out)) == {"T-0001", "T-0002", "T-0003"}
    assert "그 외 open" not in out


def test_all_does_not_resolve_identity(board, capsys, monkeypatch):
    """`--all` 은 정체성 무해소(필터 없음) — user_name 미호출(default_view 와 대비)."""
    _seed(board, "T-0001", "open")

    def _boom(*a, **k):
        raise AssertionError("user_name must not be called with --all")

    monkeypatch.setattr(board, "user_name", _boom)
    out = _run(board, capsys, all=True)
    assert _ids(out) == ["T-0001"]


def test_all_mutually_exclusive_with_mine(board):
    """`--all` + `--mine` 는 상호 배타 — fail-loud(뷰 스코프는 하나만)."""
    with pytest.raises(SystemExit) as exc:
        board.cmd_list(argparse.Namespace(status=None, tag=None, mine=True,
                                          all=True, task=None, repo=None, slot=None))
    assert "--all" in str(exc.value)


def test_all_mutually_exclusive_with_repo_slot(board):
    """`--all` + `--repo/--slot` 도 상호 배타 — fail-loud."""
    with pytest.raises(SystemExit) as exc:
        board.cmd_list(argparse.Namespace(status=None, tag=None, mine=False,
                                          all=True, task=None, repo="proj", slot=1))
    assert "--all" in str(exc.value)


def test_all_mutually_exclusive_with_task(board):
    """`--all` + `--task` 도 상호 배타 — fail-loud (뷰 스코프는 하나만·codex suggestion 3b)."""
    with pytest.raises(SystemExit) as exc:
        board.cmd_list(argparse.Namespace(status=None, tag=None, mine=False,
                                          all=True, task="refactor", repo=None, slot=None))
    assert "--all" in str(exc.value)


# ════════════════════════════════════════════════════════════════════════
# ⑥ 다중사용자 거동 — 접힘 모수=공유 풀 전량·prefix=소유 무관 라벨 (ADR-0066 명확화·codex 3a)
# ════════════════════════════════════════════════════════════════════════

def test_default_view_multiuser_team_open_in_fold_count(board, capsys):
    """다중사용자·무스트림: 타 사용자(bob) 소유 open 도 접힘 카운트에 포함 — strict-exclude 는
    `--mine` 렌즈 declutter 이지 기본 뷰 접힘 모수의 필터가 아니다(접힘 모수=공유 풀 전량)."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "open", created_by="alice/project_manager_1")   # 내 소유
    _seed(board, "T-0002", "open", created_by="bob/project_manager_2")     # 타 사용자 → distinct 2=multi_user
    out = _run(board, capsys)
    assert _ids(out) == []                         # 슬롯 세션(무스트림) → 상세 0
    assert "그 외 open 2건" in out and _FOLD_TAIL in out   # 소유 무관 전량 접힘(bob 것도 포함)


def test_default_view_multiuser_stream_open_ownership_agnostic(board, capsys, monkeypatch):
    """다중사용자·task 세션: 내 스트림 prefix(PAY) open 이 타 사용자(bob) 소유여도 스트림 상세 —
    prefix 는 스트림 라벨이지 소유 경계가 아니다(ADR-0066 명확화)."""
    monkeypatch.setenv("PM_SESSION_NAME", "refactor")
    _write_tasks(board, {"name": "refactor", "prefix": "PAY"})
    _seed(board, "T-PAY-001", "open", created_by="bob/other")     # bob 소유·내 스트림
    _seed(board, "T-ACC-001", "open", created_by="alice/x")       # alice·타 스트림 (distinct 2=multi_user)
    out = _run(board, capsys)
    assert set(_ids(out)) == {"T-PAY-001"}         # bob 소유여도 스트림 상세(소유 무관)
    assert "그 외 open 1건" in out and _FOLD_TAIL in out


# ════════════════════════════════════════════════════════════════════════
# 파서 — `--all` 등록
# ════════════════════════════════════════════════════════════════════════

def test_list_all_flag_parses(board):
    """`list --all` 이 argparse 레벨에서 파싱된다(파서 등록·카드↔CLI 정합 입력)."""
    args = board.build_parser().parse_args(["list", "--all"])
    assert args.all is True
