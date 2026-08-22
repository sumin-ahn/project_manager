"""`board list` freshness 표면화 단위 테스트 (T-0379·PM 69 stale-read 잔여 폐쇄).

세션 중간의 board 읽기(`list`)에도 board submodule 최신도를 1줄 표면화해, 부트스트랩 이후
시점의 stale 오독(타 슬롯이 stale board 스냅샷을 최신처럼 신뢰)을 없앤다. 판정은 **신규
구현 0** — 부트스트랩(T-0341)이 소비하는 pm_bootstrap 순수 판정(`_format_freshness`·
`parse_git_ahead_behind`·`_behind_warning`)을 재사용한다(판정 단일화).

이 파일이 검증하는 계약:
  1. 최신(online·behind0·ahead0) → `board-git: 최신`.
  2. offline(fetch 실패) → `판정불가 — 스냅샷일 수 있음` fail-soft(stale 을 최신으로 오단정 안 함).
  3. behind>0(online) → `behind N` + `수동 동기 필요` 경고.
  4. solo/board 비-git(`_board_git_enabled()` False) → 표면화 생략(None·무출력·오탐 0).
  5. `cmd_list` 가 freshness 를 **stderr** 로 낸다(stdout 목록 포맷 무오염) + 각 변형 공통.

hermetic: board 모듈을 fresh 로드하고 board-git 함수(`_board_git`)를 fake 로 갈아, 실 git/
네트워크 없이 판정 분기를 친다. pm_bootstrap 순수 판정은 실 모듈을 로드해 단일-소스를 검증한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board():
    """fresh board 모듈 인스턴스 (freshness 함수 단위 테스트용·IO 재지정 불요)."""
    return _load_board()


class _FakeBoardGit:
    """board.py `_board_git` 대역 — git 서브커맨드별 canned 결과 + 호출 기록 (실 git·네트워크 0).

    `fetch`·`rev-list`(ahead/behind)·`symbolic-ref`(detached: rc==1)·`status`(dirty)·`rev-parse`
    (`--git-path FETCH_HEAD` → `fetch_head_path` 반환)를 fake 로 처리한다. 매 호출의 args·timeout 을
    `calls` 에 기록해 fetch 생략(TTL)·timeout 인자를 단언할 수 있게 한다.
    """

    def __init__(self, *, fetch_rc=0, ab_out="0\t0", ab_rc=0, detached=False,
                 dirty="", fetch_head_path=None):
        self.fetch_rc = fetch_rc
        self.ab_out = ab_out
        self.ab_rc = ab_rc
        self.detached = detached
        self.dirty = dirty
        self.fetch_head_path = fetch_head_path
        self.calls: list[dict] = []

    def __call__(self, args, *, check=False, timeout=None):
        self.calls.append({"args": list(args), "timeout": timeout})
        cmd = args[0]
        if cmd == "fetch":
            return types.SimpleNamespace(returncode=self.fetch_rc, stdout="")
        if cmd == "rev-list":
            return types.SimpleNamespace(returncode=self.ab_rc, stdout=self.ab_out)
        if cmd == "symbolic-ref":
            return types.SimpleNamespace(returncode=1 if self.detached else 0, stdout="")
        if cmd == "status":
            return types.SimpleNamespace(returncode=0, stdout=self.dirty)
        if cmd == "rev-parse":  # --git-path FETCH_HEAD → FETCH_HEAD 경로(TTL 가드).
            return types.SimpleNamespace(
                returncode=0, stdout=str(self.fetch_head_path or ""))
        return types.SimpleNamespace(returncode=0, stdout="")

    @property
    def fetch_calls(self) -> list[dict]:
        return [c for c in self.calls if c["args"] and c["args"][0] == "fetch"]


def _fake_board_git(**kw):
    """`_FakeBoardGit` 콜러블 인스턴스 (기존 테스트 호환 팩토리·TTL 기본 미상=매번 fetch)."""
    return _FakeBoardGit(**kw)


def _wire_board_git(board, monkeypatch, **kw):
    monkeypatch.setattr(board, "_board_git_enabled", lambda: True)
    monkeypatch.setattr(board, "_board_git", _fake_board_git(**kw))


# ── _board_git_freshness — 판정 3분기 + solo 생략 ────────────────────────

def test_freshness_line_online_up_to_date(board, monkeypatch):
    """online·behind0·ahead0 → `board-git: 최신` (부트스트랩 판정 재사용)."""
    _wire_board_git(board, monkeypatch, fetch_rc=0, ab_out="0\t0")
    line = board._board_git_freshness().line
    assert line == "board-git: 최신"


def test_freshness_line_offline_undetermined(board, monkeypatch):
    """offline(fetch 실패) → 판정불가 fail-soft (stale 스냅샷을 최신으로 오단정 안 함·T-0341 상속)."""
    _wire_board_git(board, monkeypatch, fetch_rc=1, ab_out="0\t0")
    line = board._board_git_freshness().line
    assert line is not None
    assert "판정불가 — 스냅샷일 수 있음" in line
    assert "최신" not in line              # offline 은 "최신" 을 주장하지 않는다.
    assert "fetch 실패" in line            # offline 사유 표면.


def test_freshness_line_behind_warns(board, monkeypatch):
    """online·behind>0 → `behind N` + 수동 동기 필요 경고 (behind 시 경고·advisory)."""
    _wire_board_git(board, monkeypatch, fetch_rc=0, ab_out="0\t3")
    line = board._board_git_freshness().line
    assert line is not None
    assert "behind 3" in line
    assert "수동 동기 필요" in line


def test_freshness_line_solo_non_git_omitted(board, monkeypatch):
    """solo/board 비-git(`_board_git_enabled()` False) → None (표면화 생략·오탐 0)."""
    monkeypatch.setattr(board, "_board_git_enabled", lambda: False)
    assert board._board_git_freshness().line is None


def test_freshness_line_pm_bootstrap_load_fail_omitted(board, monkeypatch):
    """pm_bootstrap 로드 실패 → None (fail-soft·advisory 라 무발화)."""
    monkeypatch.setattr(board, "_board_git_enabled", lambda: True)
    monkeypatch.setattr(board, "_load_pm_bootstrap_module", lambda: None)
    assert board._board_git_freshness().line is None


def test_freshness_judgment_is_single_sourced(board, monkeypatch):
    """판정 단일화 — board.py 가 pm_bootstrap 의 `_format_freshness` 를 실제로 소비한다.

    board 자체 판정을 새로 구현하지 않았음을 못박는다(중복 판정 금지·T-0379 결정): pm_bootstrap
    의 순수 판정을 sentinel 로 갈면 board freshness 출력이 그 sentinel 을 그대로 실어 나른다.
    """
    _wire_board_git(board, monkeypatch, fetch_rc=0, ab_out="0\t0")
    real_pmb = board._load_pm_bootstrap_module()
    monkeypatch.setattr(real_pmb, "_format_freshness", lambda scope: "SENTINEL")
    monkeypatch.setattr(board, "_load_pm_bootstrap_module", lambda: real_pmb)
    assert board._board_git_freshness().line == "board-git: SENTINEL"


# ── advisory fetch 조율 (T-0379 should-fix): FETCH_HEAD TTL 가드 + 5s timeout ──

def _fetch_head(tmp_path: Path, *, age_seconds: float) -> Path:
    """mtime 이 `age_seconds` 전인 임시 FETCH_HEAD 파일을 만든다 (TTL 판정 입력)."""
    p = tmp_path / "FETCH_HEAD"
    p.write_text("x", encoding="utf-8")
    ts = time.time() - age_seconds
    os.utime(p, (ts, ts))
    return p


def test_fetch_head_fresh_true_within_ttl(board, monkeypatch, tmp_path):
    """FETCH_HEAD mtime 이 TTL 이내 → `_board_fetch_head_fresh` True (직전 fetch 재사용)."""
    fh = _fetch_head(tmp_path, age_seconds=5)
    monkeypatch.setattr(board, "_board_git", _FakeBoardGit(fetch_head_path=fh))
    assert board._board_fetch_head_fresh(60) is True


def test_fetch_head_fresh_false_when_stale(board, monkeypatch, tmp_path):
    """FETCH_HEAD mtime 이 TTL 경과 → False (fetch 진행)."""
    fh = _fetch_head(tmp_path, age_seconds=120)
    monkeypatch.setattr(board, "_board_git", _FakeBoardGit(fetch_head_path=fh))
    assert board._board_fetch_head_fresh(60) is False


def test_fetch_head_fresh_false_when_absent(board, monkeypatch, tmp_path):
    """FETCH_HEAD 부재(한 번도 fetch 안 함·경로 stat 실패) → False (fetch 진행)."""
    missing = tmp_path / "FETCH_HEAD"   # 생성 안 함
    monkeypatch.setattr(board, "_board_git", _FakeBoardGit(fetch_head_path=missing))
    assert board._board_fetch_head_fresh(60) is False


def test_freshness_skips_fetch_when_fetch_head_fresh(board, monkeypatch, tmp_path):
    """TTL 이내면 freshness 가 fetch 를 생략하고 직전 결과 재사용(fetched=True → 최신)."""
    fh = _fetch_head(tmp_path, age_seconds=5)
    fake = _FakeBoardGit(fetch_head_path=fh, ab_out="0\t0")
    monkeypatch.setattr(board, "_board_git_enabled", lambda: True)
    monkeypatch.setattr(board, "_board_git", fake)
    line = board._board_git_freshness().line
    assert line == "board-git: 최신"
    assert fake.fetch_calls == []          # fetch 생략(TTL 재사용).


def test_freshness_runs_fetch_when_fetch_head_stale(board, monkeypatch, tmp_path):
    """TTL 경과면 freshness 가 fetch 를 수행한다 (신선도 재실측)."""
    fh = _fetch_head(tmp_path, age_seconds=120)
    fake = _FakeBoardGit(fetch_head_path=fh, ab_out="0\t0")
    monkeypatch.setattr(board, "_board_git_enabled", lambda: True)
    monkeypatch.setattr(board, "_board_git", fake)
    board._board_git_freshness()
    assert len(fake.fetch_calls) == 1      # stale → fetch 1회.


def test_advisory_fetch_timeout_is_5s(board, monkeypatch, tmp_path):
    """advisory freshness fetch 는 5s timeout 으로 호출된다(대화형 hang 완화·기존 30s 불변)."""
    fh = _fetch_head(tmp_path, age_seconds=120)   # stale → fetch 발생
    fake = _FakeBoardGit(fetch_head_path=fh)
    monkeypatch.setattr(board, "_board_git_enabled", lambda: True)
    monkeypatch.setattr(board, "_board_git", fake)
    board._board_git_freshness()
    assert fake.fetch_calls, "fetch 가 호출되지 않았다"
    assert fake.fetch_calls[0]["timeout"] == board._FRESHNESS_FETCH_TIMEOUT_SECONDS == 5


def test_pm_bootstrap_not_loaded_when_non_git(board, monkeypatch):
    """비-git 솔로는 board-git 확인에서 곧장 None — pm_bootstrap(4천줄) 로드도 안 한다."""
    monkeypatch.setattr(board, "_board_git_enabled", lambda: False)
    def _boom():
        raise AssertionError("비-git 인데 pm_bootstrap 로드가 일어남")
    monkeypatch.setattr(board, "_load_pm_bootstrap_module", _boom)
    assert board._board_git_freshness().line is None


# ── cmd_list 통합: freshness → stderr(stdout 무오염) · solo 생략 ───────────────

def _make_project(root: Path) -> None:
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def list_board(tmp_path, monkeypatch):
    """cmd_list hermetic 인스턴스 — tmp 프로젝트로 IO 재지정 + git 폴백 stub (list_scope 동형)."""
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
        "LEASES_FILE": pm / ".local" / "worktree-leases.json",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    return mod


def _list_args(**over):
    base = dict(mine=False, repo=None, slot=None, task=None, tag=None, status=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_list_surfaces_freshness_on_stderr(list_board, monkeypatch, capsys):
    """cmd_list(board-git enabled) → freshness 1줄이 **stderr**, stdout 은 목록 포맷 무오염."""
    _wire_board_git(list_board, monkeypatch, fetch_rc=0, ab_out="0\t0")
    rc = list_board.cmd_list(_list_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert "board-git: 최신" in cap.err     # freshness = stderr.
    assert "board-git:" not in cap.out      # stdout 은 오염 안 됨(파서 안전).


def test_cmd_list_solo_no_freshness_line(list_board, capsys):
    """solo/board 비-git → cmd_list 가 freshness 를 내지 않는다(조용히 생략·회귀 0)."""
    # _board_git_enabled() 는 tmp 프로젝트에 board/.git 이 없어 기본 False.
    rc = list_board.cmd_list(_list_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert "board-git:" not in cap.err
    assert "board-git:" not in cap.out


def test_cmd_list_freshness_covers_no_tickets_path(list_board, monkeypatch, capsys):
    """freshness 는 "(no tickets)" 경로에서도 표면화된다(양 return 경로 공통 소환)."""
    _wire_board_git(list_board, monkeypatch, fetch_rc=1)   # offline
    rc = list_board.cmd_list(_list_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert "(no tickets)" in cap.out
    assert "board-git:" in cap.err
    assert "판정불가" in cap.err
