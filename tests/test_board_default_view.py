"""세션 기본 뷰(무인자 `board list`) + `--all` 단위 테스트 (ADR-0067·T-0386).

ADR-0067(ADR-0066 amend): 세션의 기본 화면(무인자 `list`·명시 세션 뷰)은 **현재 user가 그 세션에서
생성한 open + claim** 만 출력한다 — 타 user/세션분은 카운트 줄 포함 **완전 비노출**(ADR-0066 의
"그 외 open N건" 접힘 카운트·task-prefix 스트림 판정 폐기). 전체 보드는 명시 `--all`.

이 파일이 검증하는 계약:
  1. **user ∧ 생성-세션 open** — user-qualified open은 `created_by` user와 세션이 모두 일치할 때만
     상세. 타 user 또는 타 세션 생성 open은 완전 비노출(행·카운트 0).
  2. **user ∧ 세션 claim 상세** — user-qualified claim은 user와 세션(슬롯/task exact)이 모두
     일치할 때만 상세.
  3. **세션 부재 created_by**(user-only·backfill 대상) — 바인딩 세션 뷰에서 비노출.
  4. **무바인딩/솔로 폴백** — 세션 미해소면 user-단위(--mine) 폴백(solo=subset·등가).
  5. **접힘 카운트 줄 부재** — "그 외 open" 꼬리 줄이 어떤 경우에도 안 나온다.
  6. **`--all`** — 필터 없는 전체 보드(모든 세션·타 사용자)·정체성 무해소·상호 배타.

hermetic 패턴은 `test_board_mine_view.py` 와 동형 — board.py 경로 전역을 tmp 프로젝트로 monkeypatch
하고, 추가로 LEASES_FILE·PM_SESSION_NAME/CLAUDE_SESSION_NAME env 를 통제한다.
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


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "pm_bootstrap_default_view", TOOLS / "pm_bootstrap.py")
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


def _seed(board, tid, status, *, claimed_by=None, created_by=None, title="t"):
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "claimed_by": claimed_by, "created_by": created_by,
                             "depends_on": [], "tags": []}, "# seed\n")
    return path


def _run(board, capsys, **flags) -> str:
    """무인자/`--all` cmd_list 를 돌려 전체 stdout 을 반환한다."""
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


def _assert_no_fold(out: str) -> None:
    """접힘 카운트 줄이 어떤 형태로도 안 나온다(ADR-0067 — 타 세션분 카운트 포함 완전 비노출)."""
    assert "그 외 open" not in out


# ════════════════════════════════════════════════════════════════════════
# ① 생성-세션 스트림 open — created_by 세션 일치만 상세·타 세션 생성분 완전 비노출
# ════════════════════════════════════════════════════════════════════════

def test_default_view_stream_open_by_created_session(board, capsys):
    """open 은 `created_by` 세션이 현 세션과 일치할 때만 상세 — 타 세션 생성분(같은 사용자 타 슬롯
    포함)은 카운트 줄 없이 완전 비노출(ADR-0067 스트림 판정=생성 세션)."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "open", created_by="alice/project_manager_1")  # 내 세션 생성 → 상세
    _seed(board, "T-0002", "open", created_by="bob/project_manager_2")    # 타 세션 → 비노출
    _seed(board, "T-0003", "open", created_by="alice/project_manager_2")  # 같은 사용자 타 슬롯 → 비노출
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]
    assert "T-0002" not in out and "T-0003" not in out
    _assert_no_fold(out)


def test_default_view_created_session_matches_task_session(board, capsys, monkeypatch):
    """task 세션도 동일 — created_by user ∧ 세션이 현 정체성과 일치한 open만 상세."""
    _write_conf(board, user="alice")
    monkeypatch.setenv("PM_SESSION_NAME", "refactor")   # 세션 = task 이름
    _seed(board, "T-PAY-001", "open", created_by="alice/refactor")   # 내 task 세션 생성 → 상세
    _seed(board, "T-PAY-002", "open", created_by="bob/other")        # 타 세션 → 비노출
    _seed(board, "T-ACC-001", "open", created_by="alice/refactor")   # prefix 무관·내 세션 → 상세
    out = _run(board, capsys)
    assert set(_ids(out)) == {"T-PAY-001", "T-ACC-001"}
    assert "T-PAY-002" not in out
    _assert_no_fold(out)


def test_default_view_created_session_requires_same_user(board, capsys, monkeypatch):
    """생성 세션 라벨이 같아도 타 user open은 제외 — user ∧ session 복합축."""
    monkeypatch.setenv("PM_SESSION_NAME", "refactor")
    _write_conf(board, user="alice")
    _seed(board, "T-PAY-001", "open", created_by="bob/refactor")   # 타 user·동일 세션 → 비노출
    _seed(board, "T-PAY-002", "open", created_by="alice/refactor") # 내 user·동일 세션 → 상세
    _seed(board, "T-ACC-001", "open", created_by="alice/other")    # 내 user·타 세션 → 비노출
    out = _run(board, capsys)
    assert _ids(out) == ["T-PAY-002"]
    _assert_no_fold(out)


# ════════════════════════════════════════════════════════════════════════
# ② 내 세션 claim 상세 + 그 외 skip
# ════════════════════════════════════════════════════════════════════════

def test_default_view_shows_my_session_claim(board, capsys):
    """내 세션(project_manager_1) claim 은 상세·타 세션 claim 은 기본 뷰 미표시."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "claimed", claimed_by="alice/project_manager_1")  # 내 세션
    _seed(board, "T-0002", "claimed", claimed_by="bob/project_manager_2")    # 타 세션 → skip
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]
    _assert_no_fold(out)


def test_default_view_solo_legacy_slot_claim_shown(board, capsys):
    """솔로 legacy 슬롯-only claim(user 토큰 없음)도 내 세션 exact 매칭이면 상세(not multi_user)."""
    _write_conf(board, session="project_manager_1")   # user 미상(solo)
    _seed(board, "T-0003", "claimed", claimed_by="project_manager_1")   # legacy 슬롯-only·내 슬롯
    out = _run(board, capsys)
    assert _ids(out) == ["T-0003"]


def test_default_view_claim_axis_requires_same_user(board, capsys):
    """user-qualified claim은 user ∧ session. multi-user에서 legacy
    슬롯-only는 모호하므로 strict-exclude한다."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "claimed", claimed_by="alice/project_manager_1")  # 내 user·내 세션
    _seed(board, "T-0002", "claimed", claimed_by="bob/project_manager_1")    # 타 user·같은 세션 → 제외
    _seed(board, "T-0003", "claimed", claimed_by="project_manager_1")        # legacy·multi-user → strict 제외
    _seed(board, "T-0004", "claimed", claimed_by="alice/project_manager_2")  # 타 세션 → 제외
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]
    assert all(tid not in out for tid in ("T-0002", "T-0003", "T-0004"))
    _assert_no_fold(out)


def test_default_view_same_named_task_isolated_by_user(board, capsys, monkeypatch):
    """자연스러운 동명 task `main`을 alice/bob이 각각 써도 현재 user 것만 보인다."""
    _write_conf(board, user="alice")
    monkeypatch.setenv("PM_SESSION_NAME", "main")
    _seed(board, "T-0001", "open", created_by="alice/main")
    _seed(board, "T-0002", "open", created_by="bob/main")
    _seed(board, "T-0003", "claimed", claimed_by="alice/main")
    _seed(board, "T-0004", "claimed", claimed_by="bob/main")
    out = _run(board, capsys)
    assert set(_ids(out)) == {"T-0001", "T-0003"}
    assert "T-0002" not in out and "T-0004" not in out


def test_default_view_released_slot_hides_previous_holder_open(board, capsys):
    """동일 슬롯을 bob에게 재대여한 뒤 alice가 그 슬롯에서 만든 예전 open은 제외."""
    _write_conf(board, user="bob", session="project_manager_1")
    _seed(board, "T-0001", "open", created_by="alice/project_manager_1")  # 이전 보유자
    _seed(board, "T-0002", "open", created_by="bob/project_manager_1")    # 현재 보유자
    out = _run(board, capsys)
    assert _ids(out) == ["T-0002"]
    assert "T-0001" not in out


def test_default_view_unresolved_user_keeps_solo_qualified_claim(board, capsys):
    """git email/user 미해소여도 solo의 자기 세션 qualified claim은 보인다."""
    _write_conf(board, session="project_manager_1")  # my_user=None
    _seed(board, "T-0001", "claimed", claimed_by="alice/project_manager_1")
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]


def test_default_view_unresolved_user_multi_user_strict_excludes(board, capsys, monkeypatch):
    """user 미해소 + multi-user는 같은 task 세션이어도 open/claim 모두 strict-exclude한다."""
    monkeypatch.setenv("PM_SESSION_NAME", "main")  # my_user=None, my_session=main
    _seed(board, "T-0001", "open", created_by="alice/main")
    _seed(board, "T-0002", "open", created_by="bob/main")
    _seed(board, "T-0003", "claimed", claimed_by="alice/main")
    _seed(board, "T-0004", "claimed", claimed_by="bob/main")
    out = _run(board, capsys)
    assert _ids(out) == []
    assert "(no tickets)" in out


def test_default_view_solo_legacy_created_session_open_shown(board, capsys):
    """solo legacy session-only `created_by`도 기존 degrade대로 자기 슬롯 open을 보존한다."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "open", created_by="project_manager_1")
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]


def test_default_view_solo_resolved_user_legacy_slot_claim_shown(board, capsys):
    """조회 user가 해소된 solo에서도 legacy 슬롯-only claim은 자기 세션에 보인다.

    solo 게이트는 `my_user is None` 대용값이 아니라 실제 `multi_user` 판정이어야 한다.
    """
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "claimed", claimed_by="project_manager_1")
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]


def test_bootstrap_parsers_consume_user_session_filtered_slot_view(board, capsys):
    """부트스트랩의 bound-slot 렌즈가 소비하는 count/open 목록도 board 필터 결과와 자동 정합."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "open", created_by="alice/project_manager_1")
    _seed(board, "T-0002", "open", created_by="bob/project_manager_1")
    _seed(board, "T-0003", "claimed", claimed_by="alice/project_manager_1")
    _seed(board, "T-0004", "claimed", claimed_by="bob/project_manager_1")

    default_out = _run(board, capsys)
    slot_out = _run(board, capsys, repo="project_manager", slot=1)
    assert set(_ids(default_out)) == {"T-0001", "T-0003"}
    assert set(_ids(slot_out)) == {"T-0001", "T-0003"}

    bootstrap = _load_bootstrap()
    assert bootstrap.parse_board_counts(slot_out) == {
        "done": 0, "open": 1, "claimed": 1, "blocked": 0}
    assert bootstrap.parse_open_tickets(slot_out) == ["T-0001"]


# ════════════════════════════════════════════════════════════════════════
# ③ 세션 부재 created_by(user-only·backfill 대상) — 바인딩 세션 뷰에서 비노출
# ════════════════════════════════════════════════════════════════════════

def test_default_view_created_by_without_session_hidden(board, capsys):
    """`created_by` 에 세션 부분이 없는 legacy open(user-only)은 바인딩 세션 스트림에 안 든다 —
    backfill 대상(런타임 fallback 없음·ADR-0067). 카운트 줄도 없다."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "open", created_by="alice/project_manager_1")  # 내 세션 생성 → 상세
    _seed(board, "T-0005", "open", created_by="alice")   # 세션 부재(user-only) → 비노출
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]
    assert "T-0005" not in out
    _assert_no_fold(out)


# ════════════════════════════════════════════════════════════════════════
# ④ 무바인딩/솔로 — user-단위(--mine) 폴백 (solo=subset·특례 아님)
# ════════════════════════════════════════════════════════════════════════

def test_default_view_unbound_falls_back_to_user_stream(board, capsys):
    """세션 미해소(무바인딩)면 user-단위 폴백 — 내 소유(user) open + 타 사용자 open 은 strict-exclude."""
    _write_conf(board, user="alice")   # session= 없음 → 무바인딩
    _seed(board, "T-0001", "open", created_by="alice/whatever")  # owner alice → 상세
    _seed(board, "T-0002", "open", created_by="bob/x")           # owner bob·multi_user → 제외
    out = _run(board, capsys)
    assert _ids(out) == ["T-0001"]
    _assert_no_fold(out)


def test_default_view_solo_no_identity_degrades_to_all_open(board, capsys):
    """진짜 솔로(user·session 둘 다 미상·단일 사용자)면 all-open degrade — 소유 미해소 open 도 상세."""
    # conf 없음 → user 미상·session 미상. distinct user ≤1 → not multi_user → degrade.
    _seed(board, "T-0002", "open", created_by=None)
    out = _run(board, capsys)
    assert _ids(out) == ["T-0002"]
    _assert_no_fold(out)


# ════════════════════════════════════════════════════════════════════════
# ⑤ 명시 세션 뷰(--repo/--slot)도 생성-세션 스트림 (user-단위 아님)
# ════════════════════════════════════════════════════════════════════════

def test_slot_view_uses_created_session_stream(board, capsys):
    """`--repo X --slot N` 명시 세션 뷰: open 은 그 세션 생성분 + 그 세션 claim 만 — 같은 사용자
    타 슬롯 생성 open 은 안 나온다(옛 user-단위 open 누출 근절·ADR-0067)."""
    _write_conf(board, user="alice")
    _seed(board, "T-0001", "open", created_by="alice/proj_1")   # 그 슬롯 생성 → 상세
    _seed(board, "T-0002", "open", created_by="alice/proj_2")   # 같은 사용자 타 슬롯 → 비노출
    _seed(board, "T-0003", "claimed", claimed_by="alice/proj_1")  # 그 슬롯 claim → 상세
    out = _run(board, capsys, repo="proj", slot=1)
    assert set(_ids(out)) == {"T-0001", "T-0003"}
    assert "T-0002" not in out


# ════════════════════════════════════════════════════════════════════════
# ⑥ (no tickets) · 접힘 줄 부재
# ════════════════════════════════════════════════════════════════════════

def test_default_view_no_stream_shows_no_tickets(board, capsys):
    """스트림에 아무것도 없으면 (no tickets) — 타 세션 open 이 있어도 카운트 줄 없이 그냥 비어있다."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0002", "open", created_by="bob/project_manager_2")   # 타 세션 → 완전 비노출
    out = _run(board, capsys)
    assert "(no tickets)" in out
    _assert_no_fold(out)


def test_default_view_truly_empty_shows_no_tickets(board, capsys):
    """상세도 없고 open 도 없으면 (no tickets)."""
    _write_conf(board, session="project_manager_1")
    _seed(board, "T-0009", "done", claimed_by="bob/project_manager_2")   # done·타 세션 → skip
    out = _run(board, capsys)
    assert "(no tickets)" in out
    _assert_no_fold(out)


# ════════════════════════════════════════════════════════════════════════
# ⑦ --all — 전체 보드 · 상호 배타
# ════════════════════════════════════════════════════════════════════════

def test_all_flag_shows_full_board(board, capsys):
    """`--all` = 필터 없는 전체 보드(모든 세션·타 사용자)·접힘 없음."""
    _write_conf(board, user="alice", session="project_manager_1")
    _seed(board, "T-0001", "claimed", claimed_by="alice/project_manager_1")
    _seed(board, "T-0002", "claimed", claimed_by="bob/project_manager_2")   # 타 세션도 표시
    _seed(board, "T-0003", "open", created_by="bob/project_manager_2")      # 타 세션 생성 open 도 표시
    out = _run(board, capsys, all=True)
    assert set(_ids(out)) == {"T-0001", "T-0002", "T-0003"}
    _assert_no_fold(out)


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
    """`--all` + `--task` 도 상호 배타 — fail-loud (뷰 스코프는 하나만)."""
    with pytest.raises(SystemExit) as exc:
        board.cmd_list(argparse.Namespace(status=None, tag=None, mine=False,
                                          all=True, task="refactor", repo=None, slot=None))
    assert "--all" in str(exc.value)


# ════════════════════════════════════════════════════════════════════════
# ⑧ 파서 — `--all` 등록
# ════════════════════════════════════════════════════════════════════════

def test_list_all_flag_parses(board):
    """`list --all` 이 argparse 레벨에서 파싱된다(파서 등록·카드↔CLI 정합 입력)."""
    args = board.build_parser().parse_args(["list", "--all"])
    assert args.all is True
