"""pm-config 대화형 콘솔 단위/배선 테스트 (T-0069 · ADR-0011·0014).

무인자(tty) `pm-config` 의 휴먼 프론트엔드(`run_console`)를 검증한다 — 상태 렌더
(repos via areas · slots via 리스) · 메뉴 라우팅(r/w/b/s/q → 기존 핸들러) · 입력 견고성
(빈입력/오타키 재프롬프트 · EOF/KeyboardInterrupt 우아 종료) · 재렌더.

**hermetic 필수**: 실 clone/worktree/`input()` 블록 0. 입력은 시퀀스 주입(io 없이 콜러블),
엔진(board·worktree_pool)은 mock 주입, 액션 핸들러(cmd_repo_add·cmd_worktree_add·
cmd_set_test_cmd)는 monkeypatch 로 가로채 *어떤 핸들러가 어떤 인자로 불리는지*만 친다
(test_pm_config_facade.py 의 DI seam·pm_import 비-tty 폴백 패턴 동류). 콘솔은 얇은 셸이므로
액션 동작 자체는 facade 테스트가 검증하고, 여기선 라우팅/렌더/견고성만 본다.
"""
from __future__ import annotations
from pm_engine.cli import config_console
from pm_engine.cli import config_repo
from pm_engine.cli import config_slots
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths

from _pool_seams import bind_pool

import argparse
import importlib.util
import io
import sys
from pathlib import Path

import pytest
from pm_engine.infra import areas_store

from _pool_seams import bind_board

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pc():
    return _load_pm_config()


@pytest.fixture(autouse=True)
def _fake_bare_root(tmp_path, monkeypatch, pc):
    """`FakeWorktreePool.bare_repo_path` 가 답할 임시 `.repos` 루트를 테스트마다 새로 준다.

    `cmd_worktree_add` 는 그 repo 의 bare `info/exclude` 에 사본 루트 규칙을 심는다
    ([[T-0903]]). 대역이 실 `.repos` 를 돌려주면 이 저장소의 bare 를 건드리게 된다.
    """
    monkeypatch.setattr(pc, "REPO", tmp_path)
    monkeypatch.setattr(FakeWorktreePool, "bare_root", tmp_path / ".repos")


# ── 주입형 입력/엔진 fake (hermetic) ─────────────────────────────────────────


def _inputs(*lines):
    """입력 시퀀스를 한 줄씩 돌려주는 콜러블(input_fn 대역). 소진 후엔 EOFError.

    run_console 은 메뉴 선택만 읽으면 종료(q)하거나 액션 핸들러가 추가로 읽는다 — 핸들러를
    monkeypatch 로 가로채면(아래) 그 입력은 소비 안 되므로, 메뉴 선택 줄만 넣으면 된다.
    소진 시 EOFError 로 안전 종료(무한 루프 방지·실 input 의 EOF 와 동형).
    """
    it = iter(lines)

    def reader(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError()

    return reader


class FakeBoardAreas:
    """board 모듈 대역 — _repo_base(worktree add 가 슬롯 브랜치 부모로 쓰는 그 repo 행 base)."""

    def __init__(self, *, header=None, rows=None):
        self._header = header or ["repo", "prefix", "git", "test_cmd", "owner"]
        self._rows = rows or []

    def _repo_base(self, repo):
        # board._repo_base 대역 — 그 repo 행의 base 칸. 미등록·빈 칸이면 None(worktree add 거부).
        for row in self._rows:
            if row.get("repo") == repo:
                return row.get("base") or None
        return None


def _set_areas(monkeypatch, board):
    """콘솔 repos 렌더가 읽는 `areas_store._parse_areas` 자리에 대역의 행을 꽂는다."""
    monkeypatch.setattr(areas_store, "_parse_areas",
                        lambda _home, path=None: (board._header, board._rows))


# `[w]` 가 create_slot 까지 가려면 repo 등록 base 가 있어야 한다 — svc 를 base=main 으로 등록한 행.
_SVC_ROWS = [{"repo": "svc", "prefix": "svc", "git": "git@h:me/svc.git", "test_cmd": "",
              "owner": "me", "base": "main"}]


class FakeLease:
    def __init__(self, slot, repo, *, test_cmd=None, state="leased", session="s1",
                 branch=None, pid=1):
        self.slot = slot
        self.repo = repo
        self.test_cmd = test_cmd
        self.state = state
        self.session = session
        self.branch = branch
        self.pid = pid


class FakeWorktreePool:
    class SlotFoldersRefused(RuntimeError):
        pass

    def slot_folders(self, slot, **kwargs):
        return getattr(self, "last_folders", None)

    """worktree_pool 대역 — list_leases(슬롯 surface) + create_slot(빌드명령 경로 검증)."""

    # 공유 bare 대역의 루트 — autouse `_fake_bare_root` 가 테스트별 tmp 로 채운다.
    bare_root = None

    def __init__(self, *, leases=None):
        self.leases = leases or []
        self.calls: list[tuple] = []

    def lease_is_open(self, lease):
        """열림 판정은 엔진 규칙 그대로 — 열림 기록(leased + session)이면 열림."""
        return getattr(lease, "state", None) == "leased" and bool(getattr(lease, "session", ""))

    def list_leases(self):
        self.calls.append(("list_leases",))
        return self.leases

    def list_tasks(self):
        # task 축 대역(T-0361 cockpit·T-0353) — 콘솔 테스트는 leases=[] 상태에서 cmd_status 를
        # 태우므로 명명 task 없음([])이면 충분. task 축 헤더만 렌더된다(회귀 0).
        self.calls.append(("list_tasks",))
        return []

    def slots_for_task(self, name):
        self.calls.append(("slots_for_task", name))
        return [l for l in self.leases
                if getattr(l, "state", None) == "leased" and l.session == name]

    def slot_git_status(self, slot, *, git_runner=None):
        # 슬롯 git 요약 대역(T-0361·§F8) — 최소 dict(부모 기록 없음·branch/head 미조회).
        self.calls.append(("slot_git_status", slot))
        return {"slot": slot, "branch": None, "head": None, "parent": None, "children": [],
                "stashes": 0, "submodules": [], "dirty": False}

    def preserved_branches(self, *, git_runner=None):
        # 슬롯 없는 보존 브랜치 대역 — 콘솔 테스트는 보존 브랜치가 없는 홈이다.
        self.calls.append(("preserved_branches",))
        return []

    def create_slot(self, repo, *, base, test_cmd=None, readonly=False, owner_task=None, folders=None):
        # base(필수·T-0075)·owner_task (ⓓB·ADR-0068) — cmd_worktree_add 가 전달한다. 이 콘솔
        # 테스트는 빌드명령 경로만 검증하므로 base/readonly/owner_task 는 받기만 하고 기록 튜플엔 안 넣는다.
        self.calls.append(("create_slot", repo, test_cmd))
        return FakeLease(f"work/{repo}_1", repo, test_cmd=test_cmd)

    def slot_path(self, slot):
        return f"/tmp/{slot}"

    def bare_repo_path(self, repo):
        # 그 repo 의 공유 bare 대역([[T-0903]]) — `cmd_worktree_add` 가 사본 루트 ignore 규칙을
        # 심는 자리다. autouse `_fake_bare_root` 가 준 임시 루트 안에서만 답한다(실 `.repos`
        # 로 새지 않는다). clone 이 깐 bare 처럼 디렉터리를 갖춰 둔다.
        assert self.bare_root is not None, "bare_root 미설정 — 실 `.repos` 를 건드릴 뻔했다"
        path = Path(self.bare_root) / f"{repo}.git"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def slots_for_task_strict(self, value):
        return self.slots_for_task(value)


# ── 상태 렌더 — repos(areas) + slots(리스) ───────────────────────────────────


def test_console_renders_repos_from_areas(pc, monkeypatch, capsys):
    """콘솔 첫 렌더가 areas per-repo 행(repo·prefix·git·test_cmd·owner)을 surface."""
    board = FakeBoardAreas(rows=[
        {"repo": "svc", "prefix": "svc", "git": "git@h:me/svc.git",
         "test_cmd": "pytest -q", "owner": "me"},
    ])
    _set_areas(monkeypatch, board)
    wp = bind_pool(monkeypatch, FakeWorktreePool(leases=[]))
    bind_pool(monkeypatch, wp)
    bind_board(monkeypatch, board)
    rc = pc.run_console(input_fn=_inputs("q"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "repos" in out
    assert "svc" in out and "pytest -q" in out


def test_console_renders_slots_from_leases(pc, monkeypatch, capsys):
    """콘솔 첫 렌더가 worktree 리스(slot·repo·build(test_cmd)·state)를 surface."""
    board = FakeBoardAreas(rows=[])
    wp = bind_pool(monkeypatch, FakeWorktreePool(leases=[
        FakeLease("work/svc_1", "svc", test_cmd="ctest -R hil", state="leased"),
    ]))
    bind_pool(monkeypatch, wp)
    bind_board(monkeypatch, board)
    rc = pc.run_console(input_fn=_inputs("q"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "slots" in out
    assert "work/svc_1" in out and "ctest -R hil" in out
    assert wp.calls.count(("list_leases",)) >= 1


def test_console_empty_state_does_not_crash(pc, monkeypatch, capsys):
    """등록 repo·슬롯 0 이어도 빈 안내로 surface(크래시 0)."""
    bind_pool(monkeypatch, FakeWorktreePool(leases=[]))
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(
        input_fn=_inputs("q"),

    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "등록된 repo 없음" in out
    assert "슬롯 없음" in out




# ── 메뉴 라우팅 — r/w/b/s/q 각 핸들러 호출(monkeypatch 가로채기) ──────────────


def test_console_q_quits_immediately(pc, monkeypatch, capsys):
    """`q` → 즉시 종료 rc 0 + 종료 메시지(액션 0)."""
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(
        input_fn=_inputs("q"),

    )
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


def test_console_r_routes_to_repo_add(pc, monkeypatch):
    """`r` → _console_repo_add → cmd_repo_add 핸들러 호출(기존 핸들러 재사용)."""
    seen = {}
    monkeypatch.setattr(config_repo, 'cmd_repo_add',
                        lambda _home, args, **kw: seen.update(name=args.name, git=args.git,
                                                        test=args.test, base=args.base) or 0)
    # 메뉴 'r' → repo 이름/git/test/base 4 입력 → 다음 메뉴 'q'.
    reader = _inputs("r", "svc", "git@h:me/svc.git", "pytest -q", "develop", "q")
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert seen == {"name": "svc", "git": "git@h:me/svc.git",
                    "test": "pytest -q", "base": "develop"}


def test_console_r_empty_test_passes_none(pc, monkeypatch):
    """`r` 에서 test 빈 입력 → cmd_repo_add 에 test=None(미지정·worktree/[b] 에서 설정)."""
    seen = {}
    monkeypatch.setattr(config_repo, 'cmd_repo_add',
                        lambda _home, args, **kw: seen.update(test=args.test) or 0)
    # 이름/git/test(빈)/base(빈) → 메뉴 'q'.
    reader = _inputs("r", "svc", "git@h:me/svc.git", "", "", "q")
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert seen == {"test": None}


def test_console_r_base_passes_through(pc, monkeypatch):
    """`r` 에서 base 입력 → cmd_repo_add 에 그 브랜치명 전달(CLI --base parity)."""
    seen = {}
    monkeypatch.setattr(config_repo, 'cmd_repo_add',
                        lambda _home, args, **kw: seen.update(base=args.base) or 0)
    reader = _inputs("r", "svc", "git@h:me/svc.git", "pytest -q", "release/24", "q")
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert seen == {"base": "release/24"}


def test_console_r_empty_base_passes_none(pc, monkeypatch):
    """`r` 에서 base 빈 입력 → cmd_repo_add 에 base=None(기본 브랜치 경로·기존 동작 불변)."""
    seen = {}
    monkeypatch.setattr(config_repo, 'cmd_repo_add',
                        lambda _home, args, **kw: seen.update(base=args.base) or 0)
    reader = _inputs("r", "svc", "git@h:me/svc.git", "pytest -q", "", "q")
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert seen == {"base": None}


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_console_r_base_prompt_abort_returns_sentinel(pc, monkeypatch, exc):
    """`r` base 프롬프트서 EOF/Ctrl-C → `_console_repo_add` 가 `_CONSOLE_ABORT` 반환(우아 종료 계약).

    name/git/test 입력 후 base 프롬프트서 중단 → 핸들러는 cmd_repo_add 미호출(부작용 0)이고
    sentinel 을 반환해 run_console 루프가 우아 종료한다(must-fix 2 계약).
    """
    called = {"n": 0}
    monkeypatch.setattr(config_repo, 'cmd_repo_add', lambda *a, **k: called.update(n=1) or 0)
    reader = _inputs_then_exc("svc", "git@h:me/svc.git", "pytest -q", exc=exc)
    result = config_console._console_repo_add(pm_paths.home_at(pc.REPO), reader, )
    assert result is config_console._CONSOLE_ABORT
    assert called["n"] == 0


def test_console_r_empty_name_cancels(pc, monkeypatch):
    """`r` 에서 repo 이름 빈 입력 → cmd_repo_add 미호출(취소·크래시 0)."""
    called = {"n": 0}
    monkeypatch.setattr(config_repo, 'cmd_repo_add', lambda *a, **k: called.update(n=1) or 0)
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("r", "", "q"))
    assert rc == 0
    assert called["n"] == 0


def test_console_w_routes_to_worktree_add(pc, monkeypatch):
    """`w` → _console_worktree_add → cmd_worktree_add(repo·input_fn·is_tty 전달)."""
    seen = {}

    def fake_wt_add(_home, args, *, input_fn=None, is_tty=None):
        seen["repo"] = args.repo
        seen["test"] = args.test            # 콘솔은 --test 미지정(프롬프트 경로)
        seen["user_ack"] = args.user_ack    # repo 재입력 확인을 값-결속 ack로 소비
        seen["tty"] = is_tty() if is_tty else None
        return 0

    monkeypatch.setattr(config_slots, 'cmd_worktree_add', fake_wt_add)
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("w", "svc", "svc", "q"))
    assert rc == 0
    assert seen["repo"] == "svc"
    assert seen["test"] is None            # 빌드명령은 핸들러가 프롬프트(콘솔 [w] 경로)
    assert seen["user_ack"] == "svc"
    assert seen["tty"] is True             # 콘솔 진입=tty 보장 → 핸들러 프롬프트 띄움


def test_console_w_empty_repo_cancels(pc, monkeypatch):
    """`w` 에서 repo 빈 입력 → cmd_worktree_add 미호출(취소)."""
    called = {"n": 0}
    monkeypatch.setattr(config_slots, 'cmd_worktree_add', lambda *a, **k: called.update(n=1) or 0)
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("w", "", "q"))
    assert rc == 0
    assert called["n"] == 0


class _SkewWorktreePool(FakeWorktreePool):
    """create_slot 이 marked engine skew(엔진 사본 불일치)를 던지는 풀 대역."""

    def create_slot(self, repo, **kwargs):
        exc = RuntimeError("worktree_pool.py 사본 불일치")
        exc._engine_rev_skew = True
        raise exc






def test_console_b_routes_to_set_test_cmd(pc, monkeypatch):
    """`b` → _console_set_test_cmd → cmd_set_test_cmd(slot, cmd) 호출."""
    seen = {}
    monkeypatch.setattr(
        config_slots, 'cmd_set_test_cmd',
        lambda _home, slot, cmd, **kw: seen.update(slot=slot, cmd=cmd) or 0,
    )
    reader = _inputs("b", "work/svc_1", "ctest -R hil2", "q")
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert seen == {"slot": "work/svc_1", "cmd": "ctest -R hil2"}


def test_console_b_empty_cmd_passes_none(pc, monkeypatch):
    """`b` 에서 빌드명령 빈 입력 → cmd_set_test_cmd(cmd=None)(바인딩 해제)."""
    seen = {}
    monkeypatch.setattr(
        config_slots, 'cmd_set_test_cmd',
        lambda _home, slot, cmd, **kw: seen.update(slot=slot, cmd=cmd) or 0,
    )
    reader = _inputs("b", "work/svc_1", "", "q")
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert seen == {"slot": "work/svc_1", "cmd": None}


def test_console_b_empty_slot_cancels(pc, monkeypatch):
    """`b` 에서 슬롯 빈 입력 → cmd_set_test_cmd 미호출(취소)."""
    called = {"n": 0}
    monkeypatch.setattr(config_slots, 'cmd_set_test_cmd', lambda *a, **k: called.update(n=1) or 0)
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("b", "", "q"))
    assert rc == 0
    assert called["n"] == 0


def test_console_s_refreshes_state(pc, monkeypatch, capsys):
    """`s` → 상태 재렌더(액션 없이 list_leases 재호출)."""
    wp = bind_pool(monkeypatch, FakeWorktreePool(leases=[]))
    bind_pool(monkeypatch, wp)
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("s", "q"))
    assert rc == 0
    # 첫 렌더 + s 재렌더 → list_leases 최소 2회.
    assert wp.calls.count(("list_leases",)) >= 2


# ── T-0071: 콘솔 [u] update 액션 — cmd_update 위임 + 재렌더 + surface ──────────


def test_console_u_routes_to_cmd_update(pc, monkeypatch):
    """`u` → _console_update → cmd_update([]) 호출(엔진 갱신 위임·입력 프롬프트 없음)."""
    seen = {}
    monkeypatch.setattr(
        pc, "cmd_update",
        lambda forward_args, **kw: seen.update(args=forward_args) or 0,
    )
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("u", "q"))
    assert rc == 0
    assert seen == {"args": []}                      # forward_args = [] (인자 없는 갱신)


def test_console_u_rerenders_after_update(pc, monkeypatch):
    """`u` 후 상태 재렌더 — list_leases 가 첫 렌더 + update후 재렌더로 2회+ 불림."""
    monkeypatch.setattr(pc, "cmd_update", lambda *a, **k: 0)
    wp = bind_pool(monkeypatch, FakeWorktreePool(leases=[]))
    bind_pool(monkeypatch, wp)
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("u", "q"))
    assert rc == 0
    assert wp.calls.count(("list_leases",)) >= 2     # 첫 렌더 + update후 재렌더


def test_console_u_case_insensitive(pc, monkeypatch):
    """메뉴키 대문자 'U' 도 update 로 라우팅(.lower() 정규화 — 입력 견고성)."""
    called = {"n": 0}
    monkeypatch.setattr(pc, "cmd_update", lambda *a, **k: called.update(n=called["n"] + 1) or 0)
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("U", "q"))
    assert rc == 0
    assert called["n"] == 1


def test_console_u_eof_after_update_quits_gracefully(pc, monkeypatch, capsys):
    """`u` 후 다음 메뉴서 EOF(입력 소진) → 우아 종료 rc 0(abort 정합·크래시 0)."""
    monkeypatch.setattr(pc, "cmd_update", lambda *a, **k: 0)
    # "u" 후 입력 소진 → 다음 메뉴 프롬프트서 EOFError → 우아 종료.
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("u"))
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


def test_console_menu_surfaces_update_action(pc, monkeypatch, capsys):
    """메뉴 출력에 `[u]`/`update`(엔진 갱신) surface — 사용자 노출."""
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("q"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[u]" in out and "갱신" in out


def test_console_unknown_key_help_lists_update(pc, monkeypatch, capsys):
    """오타 메뉴키 안내가 `r/w/b/m/u/s/q`(모델 안내·update 포함)를 surface."""
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("z", "q"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "r/w/b/m/u/s/q" in out


# ── 입력 견고성 — 빈입력/오타키 재프롬프트 · EOF/KeyboardInterrupt 우아 종료 ──


def test_console_blank_input_reprompts(pc, monkeypatch):
    """빈 메뉴 입력(엔터만) → 액션 0·재프롬프트(다음 입력으로 진행·크래시 0)."""
    called = {"r": 0}
    monkeypatch.setattr(config_repo, 'cmd_repo_add', lambda *a, **k: called.update(r=1) or 0)
    # "" (빈) → 재프롬프트 → "q" 종료. 빈 입력에 어떤 액션도 안 일어남.
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("", "q"))
    assert rc == 0
    assert called["r"] == 0


def test_console_unknown_key_reprompts(pc, monkeypatch, capsys):
    """오타 메뉴키(예: 'z') → 안내 + 재프롬프트(다음 입력으로·크래시 0)."""
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("z", "q"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "알 수 없는 선택" in out


def test_console_eof_quits_gracefully(pc, monkeypatch, capsys):
    """메뉴 선택에서 EOFError(EOF·파이프 끝) → 우아 종료 rc 0 + 메시지(크래시 0)."""
    def reader(prompt=""):
        raise EOFError()

    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


def test_console_keyboardinterrupt_quits_gracefully(pc, monkeypatch, capsys):
    """메뉴 선택에서 KeyboardInterrupt(Ctrl-C) → 우아 종료 rc 0 + 메시지(크래시 0)."""
    def reader(prompt=""):
        raise KeyboardInterrupt()

    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


def test_console_case_insensitive_menu_key(pc, monkeypatch, capsys):
    """메뉴키 대문자 'Q' 도 종료(.lower() 정규화 — 입력 견고성)."""
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=_inputs("Q"))
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


# ── 액션 내부 프롬프트 중단 — EOF/Ctrl-C 가 메뉴뿐 아니라 액션에서도 우아 처리 ──
# (must-fix 2·codex — 액션 내부 프롬프트서 Ctrl-C/EOF 가 나도 traceback 0·rc 0)


def _inputs_then_exc(*lines, exc):
    """N개 정상 입력 후 그 다음 프롬프트에서 `exc`(EOFError/KeyboardInterrupt) 던지는 reader.

    각 액션의 *특정 프롬프트 위치*에서 중단을 시뮬레이션한다 — 예: [r] 의 git URL 프롬프트서
    Ctrl-C 면 lines=("r","svc") 후 다음(git URL) 프롬프트에서 raise.
    """
    it = iter(lines)

    def reader(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise exc()

    return reader


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
@pytest.mark.parametrize("lead", [
    # [r] — 각 프롬프트 위치에서 중단(이름·git·test·base).
    ("r",),                                          # 이름 프롬프트서 중단
    ("r", "svc"),                                    # git URL 프롬프트서 중단
    ("r", "svc", "git@h:me/svc.git"),                # test 명령 프롬프트서 중단
    ("r", "svc", "git@h:me/svc.git", "pytest -q"),   # base 브랜치 프롬프트서 중단
    # [w] — repo 프롬프트서 중단.
    ("w",),
    # [b] — slot·빌드명령 프롬프트서 중단.
    ("b",),
    ("b", "work/svc_1"),
])
def test_console_action_prompt_abort_graceful(pc, monkeypatch, capsys, lead, exc):
    """액션 내부 프롬프트(어느 위치든)서 EOF/Ctrl-C → traceback 0·우아 종료 rc 0.

    메뉴뿐 아니라 [r]/[w]/[b] 의 *액션 내부* 프롬프트서 중단해도 예외가 전파돼 크래시하지
    않고 콘솔이 우아 종료해야 한다(must-fix 2·codex).

    모든 lead 케이스는 *액션 핸들러 도달 전*(이름/git/repo/slot/test 프롬프트)에서 중단되므로
    실 cmd_* 핸들러는 안 불린다(fake 엔진 주입·부작용 0). 빌드명령 프롬프트 중단(핸들러 내부)은
    별도 테스트(test_console_build_cmd_prompt_abort_graceful)에서 본다.
    """
    reader = _inputs_then_exc(*lead, exc=exc)
    bind_pool(monkeypatch, FakeWorktreePool())
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(
        input_fn=reader,

    )
    assert rc == 0                                  # traceback 0·우아 종료
    assert "콘솔 종료" in capsys.readouterr().out


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_console_build_cmd_prompt_abort_graceful(pc, monkeypatch, capsys, exc):
    """[w] 빌드명령 프롬프트(cmd_worktree_add 내부)서 EOF/Ctrl-C → 크래시 0.

    [w] 에서 repo 까지 입력 후 빌드명령 프롬프트(cmd_worktree_add→_prompt_test_cmd)서 중단되면
    None 폴백으로 슬롯은 생성 시도되고(create_slot test_cmd=None) 루프는 정상 지속·종료(traceback 0).
    """
    wp = bind_pool(monkeypatch, FakeWorktreePool())
    # repo 입력 후 빌드명령 프롬프트서 중단 → _prompt_test_cmd 가 None 흡수 → create_slot(None).
    # 그 뒤 메뉴 'q' 로 종료.
    reader = _inputs_then_exc_resume("w", "svc", "svc", build_exc=exc, after=("q",))
    bind_pool(monkeypatch, wp)
    bind_board(monkeypatch, FakeBoardAreas(rows=_SVC_ROWS))
    bind_board(monkeypatch, FakeBoardAreas(rows=_SVC_ROWS))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    # 빌드명령 중단은 None 폴백(크래시 0) — 슬롯 생성은 진행(create_slot test_cmd=None).
    assert ("create_slot", "svc", None) in wp.calls
    assert "콘솔 종료" in capsys.readouterr().out


def _inputs_then_exc_resume(*lead, build_exc, after):
    """lead 입력 → 다음(빌드명령) 프롬프트서 build_exc 1회 → 이후 after 입력으로 재개.

    빌드명령 프롬프트 중단(_prompt_test_cmd 가 None 흡수)은 *액션을 끝내지 않고* 슬롯 생성으로
    진행하므로(크래시 0), 그 뒤 메뉴가 다시 돌아온다 — after=("q",) 로 우아 종료시킨다.
    """
    state = {"raised": False}
    lead_it = iter(lead)
    after_it = iter(after)

    def reader(prompt=""):
        for it in (lead_it,):
            try:
                return next(it)
            except StopIteration:
                pass
        if not state["raised"]:
            state["raised"] = True
            raise build_exc()
        try:
            return next(after_it)
        except StopIteration:
            raise EOFError()

    return reader


def test_console_rerenders_after_action(pc, monkeypatch):
    """액션(`r`) 수행 후 상태 재렌더 — list_leases 가 첫 렌더 + 액션후 재렌더로 2회+ 불림."""
    monkeypatch.setattr(config_repo, 'cmd_repo_add', lambda *a, **k: 0)
    wp = bind_pool(monkeypatch, FakeWorktreePool(leases=[]))
    reader = _inputs("r", "svc", "git@h:me/svc.git", "pytest -q", "q")
    bind_pool(monkeypatch, wp)
    bind_board(monkeypatch, FakeBoardAreas(rows=[]))
    rc = pc.run_console(input_fn=reader)
    assert rc == 0
    assert wp.calls.count(("list_leases",)) >= 2   # 첫 렌더 + 액션후 재렌더


# ── 엔진 자동 로드 (주입 없이 _load_module 경유) ──────────────────────────────


def test_console_autoloads_engines_when_not_injected(pc, monkeypatch, capsys):
    """평면 loader 주입 없이 실제 패키지 슬롯 조회로 콘솔을 렌더한다."""
    wp = bind_pool(monkeypatch, FakeWorktreePool(leases=[]))

    rc = pc.run_console(input_fn=_inputs("q"))
    assert rc == 0
    assert wp.calls.count(("list_leases",)) >= 1   # 패키지에 연결한 슬롯 조회가 렌더에 쓰임


# ════════════════════════════════════════════════════════════════════════
# ADR-0053 #4 — cmd_status 세션격리 posture surface (T-0307).
#
# resolved user + isolation 상태(strict/degrade/solo) + remedy 를 노출한다. board.py 를 import
# 하지 않고([[ADR-0013]]) user 는 `_default_user`(자체 해소), 다중사용자 여부는 areas.md
# `area_owner` 자체 파싱(`_distinct_area_owners`)으로 판정한다 — 최소 신호만(board 격리 판정 무복제).
# ════════════════════════════════════════════════════════════════════════

_MARK_SOLO = "세션격리(registry/area_owner 기준): single-user"
_MARK_STRICT = "세션격리(registry/area_owner 기준): strict"
_MARK_DEGRADE = "degrade-risk"          # degrade 상태 전용 토큰(strict/solo 문구엔 부재)
_MARK_AUTHORITATIVE = "board list --mine"   # authoritative 신호 pointer (전 상태 노출·오안심 방지)
_CANON_HEADER = "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |"
_CANON_SEP = "|---|---|---|---|---|---|---|---|"


def _status_out(pc, monkeypatch, capsys, *, user, area_owners):
    """cmd_status 를 hermetic 하게 돌려 stdout 을 돌려준다 (두 신호를 직접 주입)."""
    monkeypatch.setattr(lease_store, 'user_name', lambda _home: user)
    monkeypatch.setattr(pc, "_distinct_area_owners", lambda: area_owners)
    monkeypatch.setattr(lease_store, 'session_name', lambda _home: "s1")
    wp = bind_pool(monkeypatch, FakeWorktreePool(leases=[]))
    bind_pool(monkeypatch, wp)
    rc = pc.cmd_status(argparse.Namespace(command="status"))
    assert rc == 0
    return capsys.readouterr().out


def test_status_surface_single_user(pc, monkeypatch, capsys):
    """단일사용자(distinct area_owner ≤1) → solo·resolved user 노출·degrade-risk 아님.

    should-fix: 무조건 "정상" 단언 금지 + registry coarse 신호 명시 + authoritative pointer 노출."""
    out = _status_out(pc, monkeypatch, capsys, user="alice@x", area_owners=1)
    assert _MARK_SOLO in out
    assert "alice@x" in out                 # resolved user surface
    assert _MARK_DEGRADE not in out
    assert "정상" not in out                  # 오안심 단언 제거(정직화)
    assert _MARK_AUTHORITATIVE in out        # authoritative 신호 pointer 노출


def test_status_surface_strict(pc, monkeypatch, capsys):
    """다중사용자 + 정체성 해소 → strict(격리 활성)·resolved user 노출·degrade-risk 아님."""
    out = _status_out(pc, monkeypatch, capsys, user="alice@x", area_owners=2)
    assert _MARK_STRICT in out
    assert "alice@x" in out
    assert _MARK_DEGRADE not in out
    assert _MARK_AUTHORITATIVE in out        # authoritative 신호 pointer 노출


def test_status_surface_degrade_with_remedy(pc, monkeypatch, capsys):
    """다중사용자 + 정체성 미해소 → degrade-risk + remedy(board init --owner / migrate-identity)."""
    out = _status_out(pc, monkeypatch, capsys, user=None, area_owners=2)
    assert _MARK_DEGRADE in out
    assert "board init --owner" in out       # remedy 1
    assert "migrate-identity" in out         # remedy 2
    assert "미해소" in out                    # resolved user = (미해소) surface
    assert _MARK_AUTHORITATIVE in out        # authoritative 신호 pointer 노출


def test_status_surface_single_user_unresolved_user_not_falsely_reassure(pc, monkeypatch, capsys):
    """solo + 정체성 미해소 → registry 기준 solo(격리 판정 아님)이되 "정상" 오안심 없이 한계·
    authoritative pointer 노출(should-fix). solo 는 remedy 로 nag 하지 않되 warn 확인처는 가리킨다."""
    out = _status_out(pc, monkeypatch, capsys, user=None, area_owners=0)
    assert _MARK_SOLO in out
    assert _MARK_DEGRADE not in out
    assert "정상" not in out                  # 오안심 단언 없음
    assert _MARK_AUTHORITATIVE in out        # 한계 인지 → authoritative 신호로 유도
    assert "board init --owner" not in out   # solo 는 remedy 로 nag 하지 않음


def test_status_surface_never_loads_board(pc, monkeypatch, capsys):
    """cmd_status 는 isolation posture 를 board.py 로드 없이 낸다(ADR-0013 isolation·자체 해소)."""
    loaded: list[str] = []
    orig = pc._load_module

    def spy(name, filename):
        loaded.append(name)
        return orig(name, filename)

    monkeypatch.setattr(pc, "_load_module", spy)
    monkeypatch.setattr(lease_store, 'user_name', lambda _home: None)
    monkeypatch.setattr(pc, "_distinct_area_owners", lambda: 2)
    monkeypatch.setattr(lease_store, 'session_name', lambda _home: "s1")
    wp = bind_pool(monkeypatch, FakeWorktreePool(leases=[]))
    bind_pool(monkeypatch, wp)
    rc = pc.cmd_status(argparse.Namespace(command="status"))
    assert rc == 0
    out = capsys.readouterr().out
    assert _MARK_DEGRADE in out              # isolation surface 렌더됨
    assert "board" not in loaded             # board.py 로드 0(자체 해소로 신호 산출)


# ── _distinct_area_owners 자체 파싱 단위 (board import 없이 다중사용자 신호) ────


def _write_areas(pc, monkeypatch, tmp_path, text):
    proj = tmp_path / ".project_manager"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "areas.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(pc, "REPO", tmp_path)


def test_distinct_area_owners_multi(pc, monkeypatch, tmp_path):
    _write_areas(pc, monkeypatch, tmp_path,
                 f"# Areas\n\n{_CANON_HEADER}\n{_CANON_SEP}\n"
                 "| alpha | AL | g:a | pytest -q | reg | develop | main | alice |\n"
                 "| beta | BE | g:b | pytest -q | reg | develop | main | bob |\n")
    assert pc._distinct_area_owners() == 2


def test_distinct_area_owners_single(pc, monkeypatch, tmp_path):
    _write_areas(pc, monkeypatch, tmp_path,
                 f"{_CANON_HEADER}\n{_CANON_SEP}\n"
                 "| alpha | AL | g:a | pytest -q | reg | develop | main | alice |\n")
    assert pc._distinct_area_owners() == 1


def test_distinct_area_owners_dedup(pc, monkeypatch, tmp_path):
    """같은 area_owner 가 여러 area 를 소유해도 distinct 는 1(사람 수 신호)."""
    _write_areas(pc, monkeypatch, tmp_path,
                 f"{_CANON_HEADER}\n{_CANON_SEP}\n"
                 "| alpha | AL | g:a | pytest -q | reg | develop | main | alice |\n"
                 "| gamma | GA | g:g | pytest -q | reg | develop | main | alice |\n")
    assert pc._distinct_area_owners() == 1


def test_distinct_area_owners_empty_cells_ignored(pc, monkeypatch, tmp_path):
    """빈 area_owner 셀(미마이그 채택자)은 안 센다."""
    _write_areas(pc, monkeypatch, tmp_path,
                 f"{_CANON_HEADER}\n{_CANON_SEP}\n"
                 "| alpha | AL | g:a | pytest -q | reg | develop | main |  |\n"
                 "| beta | BE | g:b | pytest -q | reg | develop | main | bob |\n")
    assert pc._distinct_area_owners() == 1   # bob 만(빈 셀 제외)


def test_distinct_area_owners_absent_is_zero(pc, monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "REPO", tmp_path)   # areas.md 부재 → solo 취급
    assert pc._distinct_area_owners() == 0


def test_distinct_area_owners_corrupt_utf8_is_zero(pc, monkeypatch, tmp_path):
    """손상 UTF-8 바이트는 크래시 없이 0 — docstring fail-soft 계약 정합(suggestion)."""
    proj = tmp_path / ".project_manager"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "areas.md").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    monkeypatch.setattr(pc, "REPO", tmp_path)
    assert pc._distinct_area_owners() == 0


def test_distinct_area_owners_old_schema_no_column_zero(pc, monkeypatch, tmp_path):
    """구 스키마(area_owner 칼럼 없음)·데이터 폭도 canonical 미만 → 0(solo·미마이그)."""
    _write_areas(pc, monkeypatch, tmp_path,
                 "| repo | prefix | git | test_cmd | owner |\n"
                 "|---|---|---|---|---|\n"
                 "| alpha | AL | g:a | pytest -q | reg |\n")
    assert pc._distinct_area_owners() == 0


def test_distinct_area_owners_upgrade_wider_row(pc, monkeypatch, tmp_path):
    """구 헤더(area_owner 없음) + 신 canonical row(8칸) → 마지막(index 7)에서 area_owner read."""
    _write_areas(pc, monkeypatch, tmp_path,
                 "| repo | prefix | git | test_cmd | owner |\n"
                 "|---|---|---|---|---|\n"
                 "| alpha | AL | g:a | pytest -q | reg | develop | main | alice |\n"
                 "| beta | BE | g:b | pytest -q | reg | develop | main | bob |\n")
    assert pc._distinct_area_owners() == 2


# ── 위임 모델 안내 (`[m]`) — 설치된 하네스만·조회 실패는 loud 강등 ──────────────
# 모델 설정에 검증도 가이드도 없었다: 키 형식은 문서에 있지만 *유효한 값의 목록·확인 방법*이
# 어디에도 없었다. 위임 시점 검증 함수는 만들지 않고(codex 값은 자유 문자열·alias 미선언
# 채택자에선 멤버십 검증이 아무것도 잡지 않는다) 이 안내로 비대칭을 닫는다.


class _FakePmImport:
    """`REGISTERED_HARNESSES` + `_harness_binary_available` 만 갖는 최소 대역."""

    REGISTERED_HARNESSES = ("claude", "opencode", "codex")

    def __init__(self, installed):
        self._installed = set(installed)
        self.queried = 0

    def _harness_binary_available(self, harness):
        return harness in self._installed

    def _real_models_runner(self, source=None):
        self.queried += 1
        return True, ["provider/real-a", "provider/real-b"]


def test_model_guidance_lists_only_installed_harnesses(pc, capsys):
    """설치되지 않은 하네스의 모델은 안내하지 않는다 — 못 쓰는 값을 고르게 만들지 않는다."""
    fake = _FakePmImport(installed=("claude",))
    rc = pc._print_delegate_model_guidance(pm_import=fake, conf={})
    out = capsys.readouterr().out

    assert rc == 0
    assert "claude" in out
    assert "opencode" not in out and "codex" not in out
    assert fake.queried == 0, "미설치 opencode 인데 목록을 조회했다"


def test_model_guidance_shows_opencode_real_list(pc, capsys):
    """조회 수단이 있는 하네스(opencode)만 실조회 목록을 보여준다 — 하드코딩 0."""
    fake = _FakePmImport(installed=("opencode",))
    pc._print_delegate_model_guidance(pm_import=fake, conf={})
    out = capsys.readouterr().out

    assert fake.queried == 1
    assert "provider/real-a" in out and "provider/real-b" in out
    assert "가용 모델 2개" in out


def test_model_guidance_truncates_long_lists_and_points_at_the_query(pc, capsys):
    """목록이 길면 앞부분만 보여주고 전량 확인 명령을 지목한다(한 화면 상한)."""
    total = pc._DELEGATE_MODEL_LIST_LIMIT + 3
    models = [f"provider/model-{index}" for index in range(total)]
    fake = _FakePmImport(installed=("opencode",))
    fake._real_models_runner = lambda source=None: (True, models)

    pc._print_delegate_model_guidance(pm_import=fake, conf={})
    out = capsys.readouterr().out

    assert f"가용 모델 {total}개" in out
    assert models[pc._DELEGATE_MODEL_LIST_LIMIT - 1] in out
    assert models[pc._DELEGATE_MODEL_LIST_LIMIT] not in out
    assert "opencode models" in out


@pytest.mark.parametrize("outcome", [(False, []), (True, [])])
def test_model_guidance_degrades_loudly_when_the_query_fails(pc, capsys, outcome):
    """조회 실패·빈 목록은 형식 안내로 **강등**하되 그 사실을 말한다(조용한 강등 금지)."""
    fake = _FakePmImport(installed=("opencode",))
    fake._real_models_runner = lambda source=None: outcome

    pc._print_delegate_model_guidance(pm_import=fake, conf={})
    out = capsys.readouterr().out

    assert "목록 조회 실패" in out and "강등" in out
    assert "provider/model" in out, "형식 안내까지 사라지면 강등이 아니라 침묵이다"


def test_model_guidance_shows_declared_alias_members(pc, capsys):
    """`delegate.model_alias` 를 선언했으면 그 멤버를 이 환경의 모델로 함께 보여준다."""
    fake = _FakePmImport(installed=("claude",))
    pc._print_delegate_model_guidance(
        pm_import=fake,
        conf={"delegate.model_alias.high": "opus, claude-opus-5"},
    )
    out = capsys.readouterr().out

    assert "high" in out and "claude-opus-5" in out


def test_model_guidance_without_injected_conf_reads_the_real_local_conf(pc, capsys, monkeypatch, tmp_path):
    """콘솔 `[m]` 경로(conf 주입 없음)는 실 local.conf 의 alias 선언을 읽는다(T-0964)."""
    conf = tmp_path / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("delegate.model_alias.high=opus, claude-opus-5\n", encoding="utf-8")
    monkeypatch.setattr(pc, "REPO", tmp_path)
    fake = _FakePmImport(installed=("claude",))

    assert pc._print_delegate_model_guidance(pm_import=fake) == 0
    out = capsys.readouterr().out

    assert "high" in out and "claude-opus-5" in out


def test_model_guidance_does_not_require_alias_declaration(pc, capsys):
    """alias 미선언은 정상 형상 — 목록을 위해 alias 를 만들라고 하지 않는다."""
    fake = _FakePmImport(installed=("claude",))
    pc._print_delegate_model_guidance(pm_import=fake, conf={})
    out = capsys.readouterr().out

    assert "선언 없음" in out
    assert "목록을 위해 만들 필요는 없다" in out


def test_model_guidance_handles_no_installed_harness(pc, capsys):
    """설치된 하네스가 하나도 없으면 빈 안내가 아니라 그 사실을 말한다."""
    fake = _FakePmImport(installed=())
    pc._print_delegate_model_guidance(pm_import=fake, conf={})
    out = capsys.readouterr().out

    assert "설치된 하네스 없음" in out


def test_alias_members_parses_comma_list_and_ignores_blanks(pc):
    """alias 파싱 — 콤마 분리·공백 제거·빈 선언 무시(의미 변경 0)."""
    assert pc._model_alias_members({
        "delegate.model_alias.a": " x , y ,, ",
        "delegate.model_alias.b": "   ",
        "delegate.developer.model": "opus",
    }) == {"a": ["x", "y"]}


def test_console_menu_routes_m_to_model_guidance(pc, monkeypatch, capsys):
    """`[m]` 이 모델 안내 핸들러로 라우팅되고 장부는 건드리지 않는다."""
    calls = []
    monkeypatch.setattr(pc, "_print_delegate_model_guidance", lambda: calls.append(1))
    board = FakeBoardAreas(rows=[])
    wp = bind_pool(monkeypatch, FakeWorktreePool())

    bind_pool(monkeypatch, wp)
    bind_board(monkeypatch, board)
    rc = pc.run_console(input_fn=_inputs("m", "q"))

    assert rc == 0
    assert calls == [1]
    assert "[m] 위임 모델 안내" in capsys.readouterr().out
