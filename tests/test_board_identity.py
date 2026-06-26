"""user/pm identity 레이어 단위 테스트 (T-0161·ADR-0033 ③·refines ADR-0014).

multi-user 보드 공유의 기반층 — board 산출(ticket frontmatter·areas)에 *누가*(user) 차원을
박는다. `pm`(슬롯·`session_name`)과 직교하는 user 식별자를 푸는 seam 과, 그 값이 ticket
`created_by`(provenance)·`claimed_by`(assignee) 로 흐르는지 검증한다.

이 파일이 검증하는 계약:
  1. **user 해소** `user_name` — local.conf `user=` 우선 → `git config user.email` 폴백 →
     둘 다 없으면 None(graceful·fail-soft).
  2. **identity 합성** `identity_tag` — `<user>/<pm-slot>`·user 미상이면 슬롯만(하위호환).
  3. **ticket created_by** — `cmd_new` 가 생성 시 set(provenance).
  4. **ticket claimed_by** — `cmd_claim` 이 user/slot 차원으로 set·구 슬롯-only 값 graceful.

**hermetic 필수**: board.py 의 경로 전역(`REPO`·`LOCAL_CONF`·`TICKETS_DIR` 등)은 import
시점에 실 repo 절대경로로 고정된다 — tmp 프로젝트로 monkeypatch 재지정하고 git 폴백은
`_git_config_email` 을 monkeypatch 해 실 git config/실 루트를 절대 건드리지 않는다
(test_board_multipm.py·test_board_per_repo.py 의 hermetic 패턴 동류).
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
        "PM_STATE_FILE": wiki / "pm_state.md",
        "PM_STATE_TEMPLATE": wiki / "pm_state.template.md",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    (pm / ".local").mkdir(parents=True, exist_ok=True)  # board_lock 의 lock 파일 위치
    # 기본: git 폴백 미설정(None) — 실 git config 누출 차단. 명시 테스트가 덮는다.
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    mod._proj = proj
    return mod


def _write_conf(board, **kv) -> None:
    board.LOCAL_CONF.write_text(
        "".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")


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
    _write_conf(board, user="", session="slot")
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
    _write_conf(board, user="alice", session="pm-1")
    assert board.identity_tag() == "alice/pm-1"


def test_identity_tag_slot_only_when_user_unknown(board):
    """user 미상(None)이면 슬롯만 — 기존 슬롯-only 값과 형태 동일(graceful)."""
    _write_conf(board, session="pm-1")  # user 키 없음·git 폴백 None(fixture)
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
    _write_conf(board, user="alice", session="pm-1")
    assert board.cmd_new(_new_args()) == 0
    created = list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["created_by"] == "alice/pm-1"


def test_cmd_new_created_by_slot_only_when_user_unknown(board):
    """user 미상이면 created_by = 슬롯만 (graceful·하위호환)."""
    _write_conf(board, session="pm-1")
    assert board.cmd_new(_new_args()) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))[0])
    assert fm["created_by"] == "pm-1"


def test_cmd_new_created_by_honors_explicit_user(board):
    """args.user 명시가 local.conf 보다 우선해 created_by 에 반영된다."""
    _write_conf(board, user="alice", session="pm-1")
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


def _claim_args(tid="T-0001", session=None, user=None):
    return argparse.Namespace(id=tid, session=session, user=user)


def test_cmd_claim_sets_claimed_by_user_slot(board):
    """cmd_claim 이 claimed_by 를 `<user>/<slot>` 으로 박는다 (assignee)."""
    _write_conf(board, user="alice", session="pm-1")
    _seed_open(board)
    assert board.cmd_claim(_claim_args()) == 0
    claimed = list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))
    assert len(claimed) == 1
    fm, _ = board.load_ticket(claimed[0])
    assert fm["claimed_by"] == "alice/pm-1"


def test_cmd_claim_claimed_by_slot_only_when_user_unknown(board):
    """user 미상이면 claimed_by = 슬롯만 — 기존 슬롯-only 동작 보존(graceful)."""
    _write_conf(board, session="pm-1")
    _seed_open(board)
    assert board.cmd_claim(_claim_args()) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))[0])
    assert fm["claimed_by"] == "pm-1"


def test_cmd_claim_session_override_still_works(board):
    """args.session override 가 슬롯 차원을, args.user 가 user 차원을 채운다."""
    _seed_open(board)
    assert board.cmd_claim(_claim_args(session="pay-pm", user="bob")) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))[0])
    assert fm["claimed_by"] == "bob/pay-pm"
