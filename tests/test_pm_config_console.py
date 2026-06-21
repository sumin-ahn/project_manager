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

import importlib.util
from pathlib import Path

import pytest

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
    """board 모듈 대역 — _parse_areas(콘솔 repos 렌더) + _areas_row_for_prefix(표시값 resolve)."""

    def __init__(self, *, header=None, rows=None):
        self._header = header or ["repo", "prefix", "git", "test_cmd", "owner"]
        self._rows = rows or []

    def _parse_areas(self):
        return self._header, self._rows

    def _areas_row_for_prefix(self, prefix):
        for row in self._rows:
            if row.get("prefix") == prefix or row.get("repo") == prefix:
                return row
        return None


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
    """worktree_pool 대역 — list_leases(슬롯 surface) + create_slot(빌드명령 경로 검증)."""

    def __init__(self, *, leases=None):
        self.leases = leases or []
        self.calls: list[tuple] = []

    def list_leases(self):
        self.calls.append(("list_leases",))
        return self.leases

    def create_slot(self, repo, *, base=None, test_cmd=None):
        # base (T-0075) — cmd_worktree_add 가 areas 의 그 repo base 를 전달한다. 이 콘솔
        # 테스트는 빌드명령 경로만 검증하므로 base 는 받기만 하고 기록 튜플엔 안 넣는다.
        self.calls.append(("create_slot", repo, test_cmd))
        return FakeLease(f"work/{repo}_1", repo, test_cmd=test_cmd)

    def slot_path(self, slot):
        return f"/tmp/{slot}"


# ── 상태 렌더 — repos(areas) + slots(리스) ───────────────────────────────────


def test_console_renders_repos_from_areas(pc, monkeypatch, capsys):
    """콘솔 첫 렌더가 areas per-repo 행(repo·prefix·git·test_cmd·owner)을 surface."""
    board = FakeBoardAreas(rows=[
        {"repo": "svc", "prefix": "svc", "git": "git@h:me/svc.git",
         "test_cmd": "pytest -q", "owner": "me"},
    ])
    wp = FakeWorktreePool(leases=[])
    rc = pc.run_console(input_fn=_inputs("q"), board=board, worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "repos" in out
    assert "svc" in out and "pytest -q" in out


def test_console_renders_slots_from_leases(pc, capsys):
    """콘솔 첫 렌더가 worktree 리스(slot·repo·build(test_cmd)·state)를 surface."""
    board = FakeBoardAreas(rows=[])
    wp = FakeWorktreePool(leases=[
        FakeLease("work/svc_1", "svc", test_cmd="ctest -R hil", state="leased"),
    ])
    rc = pc.run_console(input_fn=_inputs("q"), board=board, worktree_pool=wp)
    assert rc == 0
    out = capsys.readouterr().out
    assert "slots" in out
    assert "work/svc_1" in out and "ctest -R hil" in out
    assert wp.calls.count(("list_leases",)) >= 1


def test_console_empty_state_does_not_crash(pc, capsys):
    """등록 repo·슬롯 0 이어도 빈 안내로 surface(크래시 0)."""
    rc = pc.run_console(
        input_fn=_inputs("q"),
        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool(leases=[]),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "등록된 repo 없음" in out
    assert "슬롯 없음" in out


def test_console_renders_without_board_engine(pc, monkeypatch, capsys):
    """board 엔진 부재(로드 실패)여도 repos 렌더가 안내만 하고 크래시 0.

    board 미주입 + _load_module 이 None(board.py 부재/로드실패) → 렌더가 안내만 한다.
    worktree_pool 만 주입해 그쪽은 정상, board 쪽 부재 경로를 격리해 친다.
    """
    wp = FakeWorktreePool(leases=[])
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: None)  # board 로드 None
    rc = pc.run_console(input_fn=_inputs("q"), board=None, worktree_pool=wp)
    assert rc == 0
    assert "repo 등록 상태 조회 불가" in capsys.readouterr().out


def test_console_renders_without_worktree_engine(pc, monkeypatch, capsys):
    """worktree_pool 엔진 부재(로드 실패)여도 slots 렌더가 안내만 하고 크래시 0."""
    board = FakeBoardAreas(rows=[])
    monkeypatch.setattr(pc, "_load_module",
                        lambda name, filename: None)  # worktree_pool 로드 None
    rc = pc.run_console(input_fn=_inputs("q"), board=board, worktree_pool=None)
    assert rc == 0
    assert "슬롯 상태 조회 불가" in capsys.readouterr().out


# ── 메뉴 라우팅 — r/w/b/s/q 각 핸들러 호출(monkeypatch 가로채기) ──────────────


def test_console_q_quits_immediately(pc, capsys):
    """`q` → 즉시 종료 rc 0 + 종료 메시지(액션 0)."""
    rc = pc.run_console(
        input_fn=_inputs("q"),
        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool(),
    )
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


def test_console_r_routes_to_repo_add(pc, monkeypatch):
    """`r` → _console_repo_add → cmd_repo_add 핸들러 호출(기존 핸들러 재사용)."""
    seen = {}
    monkeypatch.setattr(pc, "cmd_repo_add",
                        lambda args, **kw: seen.update(name=args.name, git=args.git,
                                                        test=args.test, base=args.base) or 0)
    # 메뉴 'r' → repo 이름/git/test/base 4 입력 → 다음 메뉴 'q'.
    reader = _inputs("r", "svc", "git@h:me/svc.git", "pytest -q", "develop", "q")
    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert seen == {"name": "svc", "git": "git@h:me/svc.git",
                    "test": "pytest -q", "base": "develop"}


def test_console_r_empty_test_passes_none(pc, monkeypatch):
    """`r` 에서 test 빈 입력 → cmd_repo_add 에 test=None(미지정·worktree/[b] 에서 설정)."""
    seen = {}
    monkeypatch.setattr(pc, "cmd_repo_add",
                        lambda args, **kw: seen.update(test=args.test) or 0)
    # 이름/git/test(빈)/base(빈) → 메뉴 'q'.
    reader = _inputs("r", "svc", "git@h:me/svc.git", "", "", "q")
    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert seen == {"test": None}


def test_console_r_base_passes_through(pc, monkeypatch):
    """`r` 에서 base 입력 → cmd_repo_add 에 그 브랜치명 전달(CLI --base parity)."""
    seen = {}
    monkeypatch.setattr(pc, "cmd_repo_add",
                        lambda args, **kw: seen.update(base=args.base) or 0)
    reader = _inputs("r", "svc", "git@h:me/svc.git", "pytest -q", "release/24", "q")
    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert seen == {"base": "release/24"}


def test_console_r_empty_base_passes_none(pc, monkeypatch):
    """`r` 에서 base 빈 입력 → cmd_repo_add 에 base=None(기본 브랜치 경로·기존 동작 불변)."""
    seen = {}
    monkeypatch.setattr(pc, "cmd_repo_add",
                        lambda args, **kw: seen.update(base=args.base) or 0)
    reader = _inputs("r", "svc", "git@h:me/svc.git", "pytest -q", "", "q")
    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert seen == {"base": None}


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_console_r_base_prompt_abort_returns_sentinel(pc, monkeypatch, exc):
    """`r` base 프롬프트서 EOF/Ctrl-C → `_console_repo_add` 가 `_CONSOLE_ABORT` 반환(우아 종료 계약).

    name/git/test 입력 후 base 프롬프트서 중단 → 핸들러는 cmd_repo_add 미호출(부작용 0)이고
    sentinel 을 반환해 run_console 루프가 우아 종료한다(must-fix 2 계약).
    """
    called = {"n": 0}
    monkeypatch.setattr(pc, "cmd_repo_add", lambda *a, **k: called.update(n=1) or 0)
    reader = _inputs_then_exc("svc", "git@h:me/svc.git", "pytest -q", exc=exc)
    result = pc._console_repo_add(reader, board_mod=FakeBoardAreas(rows=[]))
    assert result is pc._CONSOLE_ABORT
    assert called["n"] == 0


def test_console_r_empty_name_cancels(pc, monkeypatch):
    """`r` 에서 repo 이름 빈 입력 → cmd_repo_add 미호출(취소·크래시 0)."""
    called = {"n": 0}
    monkeypatch.setattr(pc, "cmd_repo_add", lambda *a, **k: called.update(n=1) or 0)
    rc = pc.run_console(input_fn=_inputs("r", "", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert called["n"] == 0


def test_console_w_routes_to_worktree_add(pc, monkeypatch):
    """`w` → _console_worktree_add → cmd_worktree_add(repo·input_fn·is_tty 전달)."""
    seen = {}

    def fake_wt_add(args, *, worktree_pool=None, board=None, input_fn=None, is_tty=None):
        seen["repo"] = args.repo
        seen["test"] = args.test            # 콘솔은 --test 미지정(프롬프트 경로)
        seen["tty"] = is_tty() if is_tty else None
        seen["board"] = board               # 콘솔이 로드한 board 전달(areas 표시값 재사용)
        return 0

    monkeypatch.setattr(pc, "cmd_worktree_add", fake_wt_add)
    rc = pc.run_console(input_fn=_inputs("w", "svc", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert seen["repo"] == "svc"
    assert seen["test"] is None            # 빌드명령은 핸들러가 프롬프트(콘솔 [w] 경로)
    assert seen["tty"] is True             # 콘솔 진입=tty 보장 → 핸들러 프롬프트 띄움


def test_console_w_empty_repo_cancels(pc, monkeypatch):
    """`w` 에서 repo 빈 입력 → cmd_worktree_add 미호출(취소)."""
    called = {"n": 0}
    monkeypatch.setattr(pc, "cmd_worktree_add", lambda *a, **k: called.update(n=1) or 0)
    rc = pc.run_console(input_fn=_inputs("w", "", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert called["n"] == 0


def test_console_b_routes_to_set_test_cmd(pc, monkeypatch):
    """`b` → _console_set_test_cmd → cmd_set_test_cmd(slot, cmd) 호출."""
    seen = {}
    monkeypatch.setattr(
        pc, "cmd_set_test_cmd",
        lambda slot, cmd, **kw: seen.update(slot=slot, cmd=cmd) or 0,
    )
    reader = _inputs("b", "work/svc_1", "ctest -R hil2", "q")
    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert seen == {"slot": "work/svc_1", "cmd": "ctest -R hil2"}


def test_console_b_empty_cmd_passes_none(pc, monkeypatch):
    """`b` 에서 빌드명령 빈 입력 → cmd_set_test_cmd(cmd=None)(바인딩 해제)."""
    seen = {}
    monkeypatch.setattr(
        pc, "cmd_set_test_cmd",
        lambda slot, cmd, **kw: seen.update(slot=slot, cmd=cmd) or 0,
    )
    reader = _inputs("b", "work/svc_1", "", "q")
    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert seen == {"slot": "work/svc_1", "cmd": None}


def test_console_b_empty_slot_cancels(pc, monkeypatch):
    """`b` 에서 슬롯 빈 입력 → cmd_set_test_cmd 미호출(취소)."""
    called = {"n": 0}
    monkeypatch.setattr(pc, "cmd_set_test_cmd", lambda *a, **k: called.update(n=1) or 0)
    rc = pc.run_console(input_fn=_inputs("b", "", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert called["n"] == 0


def test_console_s_refreshes_state(pc, capsys):
    """`s` → 상태 재렌더(액션 없이 list_leases 재호출)."""
    wp = FakeWorktreePool(leases=[])
    rc = pc.run_console(input_fn=_inputs("s", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=wp)
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
    rc = pc.run_console(input_fn=_inputs("u", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert seen == {"args": []}                      # forward_args = [] (인자 없는 갱신)


def test_console_u_rerenders_after_update(pc, monkeypatch):
    """`u` 후 상태 재렌더 — list_leases 가 첫 렌더 + update후 재렌더로 2회+ 불림."""
    monkeypatch.setattr(pc, "cmd_update", lambda *a, **k: 0)
    wp = FakeWorktreePool(leases=[])
    rc = pc.run_console(input_fn=_inputs("u", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=wp)
    assert rc == 0
    assert wp.calls.count(("list_leases",)) >= 2     # 첫 렌더 + update후 재렌더


def test_console_u_case_insensitive(pc, monkeypatch):
    """메뉴키 대문자 'U' 도 update 로 라우팅(.lower() 정규화 — 입력 견고성)."""
    called = {"n": 0}
    monkeypatch.setattr(pc, "cmd_update", lambda *a, **k: called.update(n=called["n"] + 1) or 0)
    rc = pc.run_console(input_fn=_inputs("U", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert called["n"] == 1


def test_console_u_eof_after_update_quits_gracefully(pc, monkeypatch, capsys):
    """`u` 후 다음 메뉴서 EOF(입력 소진) → 우아 종료 rc 0(abort 정합·크래시 0)."""
    monkeypatch.setattr(pc, "cmd_update", lambda *a, **k: 0)
    # "u" 후 입력 소진 → 다음 메뉴 프롬프트서 EOFError → 우아 종료.
    rc = pc.run_console(input_fn=_inputs("u"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


def test_console_menu_surfaces_update_action(pc, capsys):
    """메뉴 출력에 `[u]`/`update`(엔진 갱신) surface — 사용자 노출."""
    rc = pc.run_console(input_fn=_inputs("q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    out = capsys.readouterr().out
    assert "[u]" in out and "갱신" in out


def test_console_unknown_key_help_lists_update(pc, capsys):
    """오타 메뉴키 안내가 `r/w/b/u/s/q`(update 포함)를 surface."""
    rc = pc.run_console(input_fn=_inputs("z", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    out = capsys.readouterr().out
    assert "r/w/b/u/s/q" in out


# ── 입력 견고성 — 빈입력/오타키 재프롬프트 · EOF/KeyboardInterrupt 우아 종료 ──


def test_console_blank_input_reprompts(pc, monkeypatch):
    """빈 메뉴 입력(엔터만) → 액션 0·재프롬프트(다음 입력으로 진행·크래시 0)."""
    called = {"r": 0}
    monkeypatch.setattr(pc, "cmd_repo_add", lambda *a, **k: called.update(r=1) or 0)
    # "" (빈) → 재프롬프트 → "q" 종료. 빈 입력에 어떤 액션도 안 일어남.
    rc = pc.run_console(input_fn=_inputs("", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert called["r"] == 0


def test_console_unknown_key_reprompts(pc, capsys):
    """오타 메뉴키(예: 'z') → 안내 + 재프롬프트(다음 입력으로·크래시 0)."""
    rc = pc.run_console(input_fn=_inputs("z", "q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    out = capsys.readouterr().out
    assert "알 수 없는 선택" in out


def test_console_eof_quits_gracefully(pc, capsys):
    """메뉴 선택에서 EOFError(EOF·파이프 끝) → 우아 종료 rc 0 + 메시지(크래시 0)."""
    def reader(prompt=""):
        raise EOFError()

    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


def test_console_keyboardinterrupt_quits_gracefully(pc, capsys):
    """메뉴 선택에서 KeyboardInterrupt(Ctrl-C) → 우아 종료 rc 0 + 메시지(크래시 0)."""
    def reader(prompt=""):
        raise KeyboardInterrupt()

    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
    assert rc == 0
    assert "콘솔 종료" in capsys.readouterr().out


def test_console_case_insensitive_menu_key(pc, capsys):
    """메뉴키 대문자 'Q' 도 종료(.lower() 정규화 — 입력 견고성)."""
    rc = pc.run_console(input_fn=_inputs("Q"),
                        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool())
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
def test_console_action_prompt_abort_graceful(pc, capsys, lead, exc):
    """액션 내부 프롬프트(어느 위치든)서 EOF/Ctrl-C → traceback 0·우아 종료 rc 0.

    메뉴뿐 아니라 [r]/[w]/[b] 의 *액션 내부* 프롬프트서 중단해도 예외가 전파돼 크래시하지
    않고 콘솔이 우아 종료해야 한다(must-fix 2·codex).

    모든 lead 케이스는 *액션 핸들러 도달 전*(이름/git/repo/slot/test 프롬프트)에서 중단되므로
    실 cmd_* 핸들러는 안 불린다(fake 엔진 주입·부작용 0). 빌드명령 프롬프트 중단(핸들러 내부)은
    별도 테스트(test_console_build_cmd_prompt_abort_graceful)에서 본다.
    """
    reader = _inputs_then_exc(*lead, exc=exc)
    rc = pc.run_console(
        input_fn=reader,
        board=FakeBoardAreas(rows=[]), worktree_pool=FakeWorktreePool(),
    )
    assert rc == 0                                  # traceback 0·우아 종료
    assert "콘솔 종료" in capsys.readouterr().out


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_console_build_cmd_prompt_abort_graceful(pc, monkeypatch, capsys, exc):
    """[w] 빌드명령 프롬프트(cmd_worktree_add 내부)서 EOF/Ctrl-C → 크래시 0.

    [w] 에서 repo 까지 입력 후 빌드명령 프롬프트(cmd_worktree_add→_prompt_test_cmd)서 중단되면
    None 폴백으로 슬롯은 생성 시도되고(create_slot test_cmd=None) 루프는 정상 지속·종료(traceback 0).
    """
    wp = FakeWorktreePool()
    # repo 입력 후 빌드명령 프롬프트서 중단 → _prompt_test_cmd 가 None 흡수 → create_slot(None).
    # 그 뒤 메뉴 'q' 로 종료.
    reader = _inputs_then_exc_resume("w", "svc", build_exc=exc, after=("q",))
    rc = pc.run_console(input_fn=reader,
                        board=FakeBoardAreas(rows=[]), worktree_pool=wp)
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
    monkeypatch.setattr(pc, "cmd_repo_add", lambda *a, **k: 0)
    wp = FakeWorktreePool(leases=[])
    reader = _inputs("r", "svc", "git@h:me/svc.git", "pytest -q", "q")
    rc = pc.run_console(input_fn=reader, board=FakeBoardAreas(rows=[]), worktree_pool=wp)
    assert rc == 0
    assert wp.calls.count(("list_leases",)) >= 2   # 첫 렌더 + 액션후 재렌더


# ── 엔진 자동 로드 (주입 없이 _load_module 경유) ──────────────────────────────


def test_console_autoloads_engines_when_not_injected(pc, monkeypatch, capsys):
    """board/worktree_pool 미주입 → _load_module 로 자동 로드(주입 seam 폴백)."""
    board = FakeBoardAreas(rows=[])
    wp = FakeWorktreePool(leases=[])

    def fake_load(name, filename):
        if name == "board":
            return board
        if name == "worktree_pool":
            return wp
        return None

    monkeypatch.setattr(pc, "_load_module", fake_load)
    rc = pc.run_console(input_fn=_inputs("q"))
    assert rc == 0
    assert wp.calls.count(("list_leases",)) >= 1   # 자동 로드된 wp 가 렌더에 쓰임
