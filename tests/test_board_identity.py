"""user/pm identity 레이어 단위 테스트 (T-0161·ADR-0033 ③·refines ADR-0014).

multi-user 보드 공유의 기반층 — board 산출(ticket frontmatter·areas)에 *누가*(user) 차원을
박는다. `pm`(슬롯·`session_name`)과 직교하는 user 식별자를 푸는 seam 과, 그 값이 ticket
`created_by`(provenance)·`claimed_by`(assignee) 로 흐르는지 검증한다.

이 파일이 검증하는 계약:
  1. **user 해소** `user_name` — local.conf `identity.user=` 우선 → `git config user.email` 폴백 →
     둘 다 없으면 None(graceful·fail-soft).
  2. **identity 합성** `identity_tag` — `<user>/<pm-slot>`·user 미상이면 슬롯만(하위호환).
  3. **ticket created_by** — `cmd_new` 가 생성 시 set(provenance).
  4. **ticket claimed_by** — `cmd_claim` 이 user/slot 차원으로 set·구 슬롯-only 값 graceful.
  5. **actor 정체성 인자** `--repo`/`--slot`(ADR-0057) — `cmd_claim` 의 3 해소 케이스(kind=
     slot/repo/none) + `--slot` 단독 fail-loud (T-0314).
  6. **cmd_block/cmd_unblock 상태-필드 정합**(T-0783) — `block` 은 claimed_by/claimed_at 무접촉
     (소유는 작업 중단으로 안 풀린다) · `unblock` 은 claimed_by 유무로 목적지(claimed/open)를
     갈라 "open + claimed_by 잔존" 모순을 원천 차단한다. 왕복(block→unblock) 멱등·뷰 멤버십
     보존까지 포함.

**hermetic 필수**: board.py 의 경로 전역(`REPO`·`LOCAL_CONF`·`TICKETS_DIR`·`LEASES_FILE` 등)은
import 시점에 실 repo 절대경로로 고정된다 — tmp 프로젝트로 monkeypatch 재지정하고 git 폴백은
`_git_config_email` 을 monkeypatch 해 실 git config/실 루트를 절대 건드리지 않는다
(test_board_multipm.py·test_board_per_repo.py 의 hermetic 패턴 동류).
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
    """cmd_new/cmd_claim 가 필요로 하는 tickets 레이아웃 + 최소 template (multipm 동형)."""
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    (tickets / "_template.md").write_text(
        "---\n"
        "id: T-NNNN\n"
        "title: <제목>\n"
        "status: open\n"
        "created: YYYY-MM-DD\n"
        "claimed_by:\n"
        "claimed_at:\n"
        "completed_at:\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "tags: []\n"
        "---\n\n"
        "# T-NNNN — <제목>\n\n## 목표\n채워라.\n",
        encoding="utf-8",
    )


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + IO 전역을 tmp 프로젝트로 재지정한 hermetic 인스턴스.

    git 폴백(`_git_config_email`)은 기본적으로 None 으로 stub 해 실 git config 를 안 읽는다 —
    git 폴백 경로를 명시 검증하는 테스트만 그 stub 을 덮는다.
    """
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load_board()
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": pm / ".local",
        "BOARD_LOCK": pm / ".local" / "board.lock",
        "LEASES_FILE": pm / ".local" / "worktree-leases.json",  # `--repo` 단독 actor 해소용(T-0314)
        "PM_STATE_FILE": wiki / "pm_state.md",
        "PM_STATE_TEMPLATE": wiki / "pm_state.template.md",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    (pm / ".local").mkdir(parents=True, exist_ok=True)  # board_lock 의 lock 파일 위치
    # 기본: git 폴백 미설정(None) — 실 git config 누출 차단. 명시 테스트가 덮는다.
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    # 세션 바인딩은 env 명시 — per-clone conf `session=` 폴백은 폐지됐다(T-0779).
    monkeypatch.setenv("PM_SESSION_NAME", "pm-1")
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    mod._proj = proj
    return mod


# 테스트 kwargs 는 python 식별자라 dot 표기를 담을 수 없다 — conf 키로 옮겨 쓴다
# (`user=` → `identity.user=`). 구표기로 쓰면 conf 를 읽는 지점이 fail-loud 로 멈춘다.
_CONF_KEY_ALIASES = {"user": "identity.user", "py": "runtime.py",
                     "test_cmd": "test.cmd", "upstream": "upstream.path",
                     "project_name": "project.name"}


def _conf_key(name: str) -> str:
    return _CONF_KEY_ALIASES.get(name, name)


def _write_conf(board, **kv) -> None:
    board.LOCAL_CONF.write_text(
        "".join(f"{_conf_key(k)}={v}\n" for k, v in kv.items()),
        encoding="utf-8")


def _write_leases(board, *rows) -> None:
    """리스 장부(`LEASES_FILE`)에 (repo, slot 정수) 행을 leased 상태로 쓴다 — `identity_args.
    resolve_actor_slot` 이 읽는 실 스키마(repo·slot=`work/<repo>_<N>`·session·state) 동형
    (test_identity_args.py `_write_leases` 동형)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    leases = [{"repo": r["repo"], "slot": f"work/{r['repo']}_{r['slot']}",
               "session": f"{r['repo']}_{r['slot']}", "state": r.get("state", "leased")}
              for r in rows]
    board.LEASES_FILE.write_text(json.dumps({"leases": leases}), encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════
# user_name — local.conf 우선 · git 폴백 · graceful None
# ════════════════════════════════════════════════════════════════════════

def test_user_name_local_conf_wins(board, monkeypatch):
    """local.conf user= 가 있으면 그것(git 폴백보다 우선)."""
    _write_conf(board, user="alice")
    # git 폴백이 다른 값을 줘도 local.conf 가 이긴다.
    monkeypatch.setattr(board, "_git_config_email", lambda: "bob@x.com")
    assert board.user_name() == "alice"


def test_user_name_override_wins(board):
    """명시 override 가 local.conf 보다도 우선(session_name 패턴 동형)."""
    _write_conf(board, user="alice")
    assert board.user_name("carol") == "carol"


def test_user_name_falls_back_to_git_email(board, monkeypatch):
    """local.conf user= 부재 → git config user.email 로 폴백."""
    # local.conf 없음(user 키 부재).
    monkeypatch.setattr(board, "_git_config_email", lambda: "dev@example.com")
    assert board.user_name() == "dev@example.com"


def test_user_name_none_when_neither(board):
    """local.conf user= 도 git email 도 없으면 None (graceful·user 미상 허용)."""
    # fixture 가 _git_config_email→None stub·local.conf 부재.
    assert board.user_name() is None


def test_user_name_empty_conf_value_ignored(board, monkeypatch):
    """local.conf user= 가 빈 값이면 미설정 취급 → git 폴백으로 내려간다."""
    _write_conf(board, user="")
    monkeypatch.setattr(board, "_git_config_email", lambda: "fallback@x.com")
    assert board.user_name() == "fallback@x.com"


# ── _git_config_email fail-soft (실 git 미주입 — git 부재/실패 graceful) ──────

def test_git_config_email_fail_soft_when_git_absent(board, monkeypatch):
    """git 바이너리 부재(`shutil.which` None) → None (크래시 0).

    fixture 가 `_git_config_email` 자체를 stub 하므로, 원본 구현의 fail-soft 를 검증하려면
    fresh 모듈을 로드(전역 stub 회피)하고 REPO 만 tmp 로 핀한 뒤 `which` 를 None 으로 막는다.
    """
    fresh = _load_board()
    monkeypatch.setattr(fresh, "REPO", board.REPO)
    monkeypatch.setattr(fresh.shutil, "which", lambda _name: None)
    assert fresh._git_config_email() is None


# ════════════════════════════════════════════════════════════════════════
# identity_tag — <user>/<pm-slot> 합성 · user 미상이면 슬롯만
# ════════════════════════════════════════════════════════════════════════

def test_identity_tag_user_slash_slot(board):
    """user 해소되면 `<user>/<pm-slot>`."""
    _write_conf(board, user="alice")
    assert board.identity_tag() == "alice/pm-1"


def test_identity_tag_slot_only_when_user_unknown(board):
    """user 미상(None)이면 슬롯만 — 기존 슬롯-only 값과 형태 동일(graceful)."""
    _write_conf(board)  # user 키 없음·git 폴백 None(fixture)
    assert board.identity_tag() == "pm-1"


def test_identity_tag_honors_overrides(board):
    """session/user override 를 둘 다 존중한다."""
    assert board.identity_tag(session_override="s2", user_override="u2") == "u2/s2"


# ════════════════════════════════════════════════════════════════════════
# cmd_new — created_by (provenance·생성 시 set)
# ════════════════════════════════════════════════════════════════════════

def _new_args(title="t", prefix=None, user=None, session=None):
    return argparse.Namespace(title=title, prefix=prefix, touches=None,
                              depends=None, tag=None, estimate="small",
                              user=user, session=session)


def test_cmd_new_sets_created_by_user_slot(board):
    """cmd_new 가 created_by 를 `<user>/<pm-slot>` 으로 박는다 (provenance)."""
    _write_conf(board, user="alice")
    assert board.cmd_new(_new_args()) == 0
    created = list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["created_by"] == "alice/pm-1"


def test_cmd_new_created_by_slot_only_when_user_unknown(board):
    """user 미상이면 created_by = 슬롯만 (graceful·하위호환)."""
    _write_conf(board)
    assert board.cmd_new(_new_args()) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))[0])
    assert fm["created_by"] == "pm-1"


def test_cmd_new_created_by_honors_explicit_user(board):
    """args.user 명시가 local.conf 보다 우선해 created_by 에 반영된다."""
    _write_conf(board, user="alice")
    assert board.cmd_new(_new_args(user="carol")) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))[0])
    assert fm["created_by"] == "carol/pm-1"


# ════════════════════════════════════════════════════════════════════════
# cmd_claim — claimed_by user/slot 차원
# ════════════════════════════════════════════════════════════════════════

def _seed_open(board, tid="T-0001"):
    path = board.TICKETS_DIR / "open" / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": "seed", "status": "open",
                             "claimed_by": None, "depends_on": []}, "# seed\n")
    return path


def _claim_args(tid="T-0001", repo=None, slot=None, user=None):
    return argparse.Namespace(id=tid, repo=repo, slot=slot, user=user)


def test_cmd_claim_sets_claimed_by_user_slot(board):
    """cmd_claim 이 claimed_by 를 `<user>/<slot>` 으로 박는다 (assignee)."""
    _write_conf(board, user="alice")
    _seed_open(board)
    assert board.cmd_claim(_claim_args()) == 0
    claimed = list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))
    assert len(claimed) == 1
    fm, _ = board.load_ticket(claimed[0])
    assert fm["claimed_by"] == "alice/pm-1"


def test_cmd_claim_claimed_by_slot_only_when_user_unknown(board):
    """user 미상이면 claimed_by = 슬롯만 — 기존 슬롯-only 동작 보존(graceful)."""
    _write_conf(board)
    _seed_open(board)
    assert board.cmd_claim(_claim_args()) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))[0])
    assert fm["claimed_by"] == "pm-1"


# ════════════════════════════════════════════════════════════════════════
# cmd_claim — `--repo`/`--slot`(ADR-0057) 해소 3케이스 + `--slot` 단독 fail-loud (T-0314)
# ════════════════════════════════════════════════════════════════════════

def test_cmd_claim_repo_and_slot_resolve_to_composed_session(board):
    """kind=slot(`--repo X --slot N`) — 리스 조회 없이 즉시 `"<repo>_<N>"` 으로 완전 해소.

    구 `args.session` 임의 문자열 override(예: `"pay-pm"`)는 decomposed 인자로는 재현 불가 —
    ADR-0057 이후 슬롯 정체성은 항상 `<repo>_<N>` 형태(internal 표현 불변)다."""
    _seed_open(board)
    assert board.cmd_claim(_claim_args(repo="pay", slot=3, user="bob")) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))[0])
    assert fm["claimed_by"] == "bob/pay_3"


def test_cmd_claim_repo_alone_resolves_single_active_lease(board):
    """kind=repo(`--repo X` 단독) — 그 repo 의 활성 리스가 정확히 1개면 그 세션으로 해소
    (`identity_args.resolve_actor_slot`·ADR-0057 결정 3)."""
    _write_leases(board, {"repo": "pay", "slot": 3})
    _seed_open(board)
    assert board.cmd_claim(_claim_args(repo="pay", user="bob")) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))[0])
    assert fm["claimed_by"] == "bob/pay_3"


def test_cmd_claim_repo_alone_ambiguous_fails_loud(board):
    """kind=repo — 그 repo 의 활성 리스가 ≥2 개면 SlotResolutionError 로 fail-loud
    (기존 SlotResolutionError 의미 보존·신규 solo 분기 아님)."""
    _write_leases(board, {"repo": "pay", "slot": 1}, {"repo": "pay", "slot": 2})
    _seed_open(board)
    with pytest.raises(SystemExit) as exc:
        board.cmd_claim(_claim_args(repo="pay", user="bob"))
    assert "pay" in str(exc.value)


def test_cmd_claim_repo_alone_zero_active_slots_fails_loud(board):
    """kind=repo — 명시 `--repo X` 인데 그 repo 활성 리스가 0개면 **fail-loud** (codex r2·ADR-0057).
    None 폴백하면 kind=none 과 구분 못 해 env/단일-lease 로 silent 오귀속(`--repo typo` → 다른 repo
    세션으로 claim) — 명시 repo 는 해소되거나 명시적으로 실패한다. 여기선 다른 repo 활성 lease 를 둬
    (폴백 유혹) `--repo pay`(0슬롯)가 그 세션으로 오귀속되지 않고 fail-loud 함을 lock."""
    _write_leases(board, {"repo": "other", "slot": 1})  # 단일 lease(다른 repo) — 옛 폴백 오귀속 유혹
    _seed_open(board)
    with pytest.raises(SystemExit) as exc:
        board.cmd_claim(_claim_args(repo="pay", user="bob"))
    assert "pay" in str(exc.value) and "활성 슬롯" in str(exc.value)


def test_cmd_claim_slot_alone_without_repo_fails_loud(board):
    """`--slot N` 단독(`--repo` 없음) — `parse_identity` 가 ValueError·`cmd_claim` 이 fail-loud
    (ADR-0057 결정 2 — uniform·solo 예외 없음)."""
    _seed_open(board)
    with pytest.raises(SystemExit) as exc:
        board.cmd_claim(_claim_args(slot=3, user="bob"))
    assert "--repo" in str(exc.value)


# ════════════════════════════════════════════════════════════════════════
# cmd_block / cmd_unblock — 상태-필드 정합 (T-0783)
# ════════════════════════════════════════════════════════════════════════
#
# 전이표(실측 대상 전부 — 각 셀은 실 CLI 커맨드(`cmd_block`/`cmd_unblock`)가 실제로 쓴
# frontmatter 로 확인한다. `cmd_unclaim`(claimed→open 소유 해제)·`cmd_claim`(open→claimed)·
# `cmd_complete`(claimed→done)는 이 티켓이 손대지 않아 별도 매트릭스 불필요 — 기존 동작 그대로):
#
#   출발 status · claimed_by 유무  →(block)→  blocked  →(unblock)→  도착 status
#   ─────────────────────────────────────────────────────────────────────────
#   open    · claimed_by=null      → blocked(무접촉)  → open(무접촉)     [현행 무변경]
#   claimed · claimed_by=<u>/<s>   → blocked(무접촉)  → claimed(보존)    [T-0783 수정 — 이전엔 open]
#
# 어느 경로도 "open + claimed_by 잔존"(I1 위반)을 만들지 않는다.

def _block_args(tid="T-0001", reason="테스트 차단"):
    return argparse.Namespace(id=tid, reason=reason)


def _unblock_args(tid="T-0001"):
    return argparse.Namespace(id=tid)


def _ticket_in_status(board, tid, status):
    """`status` 디렉터리에서 `tid` 티켓 정확히 1개를 찾아 frontmatter/본문을 반환한다."""
    files = list((board.TICKETS_DIR / status).glob(f"{tid}-*.md"))
    assert len(files) == 1, f"{tid} 가 {status}/ 에 정확히 1개 있어야 한다: {files}"
    return board.load_ticket(files[0])


def test_claimed_block_unblock_round_trip_preserves_identity(board):
    """claimed(u) → block → unblock 은 claimed(u) 로 돌아온다 — claimed_by·claimed_at 값 보존."""
    _write_conf(board, user="alice")
    _seed_open(board)
    assert board.cmd_claim(_claim_args()) == 0
    fm_before, _ = _ticket_in_status(board, "T-0001", "claimed")
    claimed_by, claimed_at = fm_before["claimed_by"], fm_before["claimed_at"]

    assert board.cmd_block(_block_args()) == 0
    fm_blocked, _ = _ticket_in_status(board, "T-0001", "blocked")
    assert fm_blocked["claimed_by"] == claimed_by, "block 이 claimed_by 를 건드렸다(I4 위반)"
    assert fm_blocked["claimed_at"] == claimed_at, "block 이 claimed_at 을 건드렸다(I4 위반)"

    assert board.cmd_unblock(_unblock_args()) == 0
    fm_after, _ = _ticket_in_status(board, "T-0001", "claimed")
    assert fm_after["claimed_by"] == claimed_by
    assert fm_after["claimed_at"] == claimed_at
    assert not list((board.TICKETS_DIR / "open").glob("T-0001-*.md")), \
        "unblock 이 claimed_by 보유 티켓을 open/ 으로 잘못 옮겼다"


def test_open_block_unblock_round_trip_unchanged(board):
    """open → block → unblock 은 현행과 동일 — open 으로 돌아오고 claimed_by 는 계속 null."""
    _seed_open(board)
    assert board.cmd_block(_block_args()) == 0
    fm_blocked, _ = _ticket_in_status(board, "T-0001", "blocked")
    assert fm_blocked["claimed_by"] is None

    assert board.cmd_unblock(_unblock_args()) == 0
    fm_after, _ = _ticket_in_status(board, "T-0001", "open")
    assert fm_after["claimed_by"] is None
    assert fm_after.get("claimed_at") is None


def test_open_block_unblock_round_trip_is_idempotent_twice(board):
    """open ↔ blocked 왕복을 두 번 반복해도 매번 open 으로 안정 수렴한다(멱등)."""
    _seed_open(board)
    for _ in range(2):
        assert board.cmd_block(_block_args()) == 0
        assert board.cmd_unblock(_unblock_args()) == 0
    fm, _ = _ticket_in_status(board, "T-0001", "open")
    assert fm["claimed_by"] is None


def test_claimed_block_unblock_round_trip_is_idempotent_twice(board):
    """claimed(u) ↔ blocked 왕복을 두 번 반복해도 매번 claimed(u) 로 안정 수렴한다(멱등)."""
    _write_conf(board, user="alice")
    _seed_open(board)
    assert board.cmd_claim(_claim_args()) == 0
    for _ in range(2):
        assert board.cmd_block(_block_args()) == 0
        assert board.cmd_unblock(_unblock_args()) == 0
    fm, _ = _ticket_in_status(board, "T-0001", "claimed")
    assert fm["claimed_by"] == "alice/pm-1"


def test_no_transition_leaves_open_with_claimed_by_residue(board):
    """상태×전이 매트릭스 전수 순회 — 두 경로(open 기원·claimed 기원) 끝에 "open + claimed_by
    잔존"(I1 위반) 형상이 하나도 없다."""
    _write_conf(board, user="alice")
    _seed_open(board, tid="T-0001")
    assert board.cmd_claim(_claim_args(tid="T-0001")) == 0
    assert board.cmd_block(_block_args(tid="T-0001")) == 0
    assert board.cmd_unblock(_unblock_args(tid="T-0001")) == 0

    _seed_open(board, tid="T-0002")
    assert board.cmd_block(_block_args(tid="T-0002")) == 0
    assert board.cmd_unblock(_unblock_args(tid="T-0002")) == 0

    for status, fm in board._all_tickets():
        if status == "open":
            assert fm.get("claimed_by") is None, \
                f"{fm.get('id')} 가 open/ 인데 claimed_by 잔존: {fm.get('claimed_by')!r}"


def test_blocked_with_claimed_by_stays_visible_in_default_and_mine_views(board, capsys):
    """blocked + claimed_by 티켓이 소유자의 무인자 기본 뷰·`--mine` 에서 사라지지 않는다
    (I5 — 전이 규칙 변경이 뷰 멤버십을 줄이지 않는다. `_ticket_is_mine`/`_in_default_view` 는
    이 티켓에서 손대지 않았지만 회귀 보증으로 lock)."""
    from types import SimpleNamespace

    _write_conf(board, user="alice")
    _seed_open(board)
    assert board.cmd_claim(_claim_args()) == 0
    assert board.cmd_block(_block_args()) == 0
    capsys.readouterr()  # claim/block 안내 출력 비우기

    rc = board.cmd_list(SimpleNamespace(
        status=None, tag=None, mine=False, all=False, task=None, repo=None, slot=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "T-0001" in out, "blocked+claimed_by 티켓이 무인자 기본 뷰에서 사라졌다"

    rc = board.cmd_list(SimpleNamespace(
        status=None, tag=None, mine=True, all=False, task=None, repo=None, slot=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "T-0001" in out, "blocked+claimed_by 티켓이 --mine 뷰에서 사라졌다"
