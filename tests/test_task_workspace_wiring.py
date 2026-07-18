"""T-0355 — task-mode 작업공간(F6) 도구 배선 + 해소 절대경로 surface (spike §3b F6·결정 ⑦).

identity_args.resolve_task_workspace 의 단위 검증은 test_identity_args.py 가 담당하고, 여기선
그 F6 해소가 실행-위치 필요 도구(`board.py regression run`·`livegate record`·`ticket_finish.py`)에
**배선**돼 (a) 특정 슬롯 worktree **절대경로**로 cwd 를 고정하고 (b) 그 절대경로를 surface 하며
(c) cwd 는 해소에 참여하지 않음(T-0345 불변)을 본다. 실 pytest·git 은 절대 기동하지 않는다
(subprocess.run 대역·전량 mock — 프로덕션 라이브 실행 금지).

hermetic — board/ticket_finish 의 경로 전역(`REPO`·`LEASES_FILE` 등)이 import 시점에 실 repo
절대경로로 굳으므로 tmp 프로젝트로 재지정한다(test_board_livegate hermetic 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_leases(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"leases": entries}), encoding="utf-8")


# ── board.py regression run · livegate record 배선 ───────────────────────────


@pytest.fixture
def board(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    local = proj / ".project_manager" / ".local"
    mod = _load("board")
    monkeypatch.setattr(mod, "REPO", proj)
    monkeypatch.setattr(mod, "LOCAL_DIR", local)
    monkeypatch.setattr(mod, "LEASES_FILE", local / "worktree-leases.json")
    monkeypatch.setattr(mod, "REGRESSION_FLAG", local / "regression.json")
    monkeypatch.setattr(mod, "LIVEGATE_FLAG", local / "livegate.json")
    monkeypatch.setattr(mod, "_git_head", lambda: "deadbeefdeadbeef")
    monkeypatch.setattr(mod, "_git_head_at", lambda cwd: "deadbeefdeadbeef")
    mod._proj = proj
    return mod


class _FakeRun:
    def __init__(self, rc: int = 0, stdout: str = ""):
        self.rc = rc
        self.stdout = stdout
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return types.SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr="")


def _reg_args(task=None, repo=None, slot=None, cmd=None, cwd=None):
    return argparse.Namespace(action="run", cmd=cmd, cwd=cwd, repo=repo, slot=slot,
                              task=task, ticket=None, touches=None)


def test_regression_run_task_threads_absolute_cwd_and_surfaces(board, monkeypatch, capsys):
    """--task 로 F6 해소된 슬롯 worktree 절대경로가 (a) subprocess cwd 로 고정·(b) surface 된다."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job1", "state": "leased",
         "test_cmd": "pytest -q"},
    ])
    fake = _FakeRun(0, "10 passed in 1s")
    monkeypatch.setattr(board.subprocess, "run", fake)
    rc = board.cmd_regression(_reg_args(task="job1"))
    assert rc == 0
    expected_cwd = str(board.REPO / "work" / "A_1")
    assert fake.calls, "regression subprocess 미기동"
    assert fake.calls[0]["kwargs"]["cwd"] == expected_cwd
    out = capsys.readouterr().out
    assert f"작업공간(task job1) → {expected_cwd}" in out   # 절대경로 surface


def test_regression_run_task_ambiguous_holding_fails_loud(board, monkeypatch):
    """--task 가 통틀어 2개↑ 보유(모호·⑦) → fail-loud(SystemExit) — 암묵 선택 안 함."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job2", "state": "leased"},
        {"slot": "work/B_1", "repo": "B", "session": "job2", "state": "leased"},
    ])
    monkeypatch.setattr(board.subprocess, "run", _FakeRun(0, "ok"))
    with pytest.raises(SystemExit):
        board.cmd_regression(_reg_args(task="job2"))


def test_regression_run_task_cwd_not_participate(board, monkeypatch, capsys):
    """cwd 비참여(T-0345) — chdir 을 바꿔도 --repo 명시가 해소를 지배(cwd 가 끌어당기지 않음)."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job3", "state": "leased"},
        {"slot": "work/B_1", "repo": "B", "session": "job3", "state": "leased"},
    ])
    monkeypatch.chdir(board._proj)
    monkeypatch.setattr(board.subprocess, "run", _FakeRun(0, "1 passed"))
    board.cmd_regression(_reg_args(task="job3", repo="A"))
    out = capsys.readouterr().out
    assert str(board.REPO / "work" / "A_1") in out
    assert "work/B_1" not in out


def _lg_args(task=None, repo=None, slot=None, cwd=None):
    return argparse.Namespace(action="record", rev=None, cwd=cwd, repo=repo, slot=slot, task=task)


def test_livegate_record_task_threads_absolute_cwd(board, monkeypatch, capsys):
    """livegate record --task → F6 슬롯 worktree 절대경로를 cwd 로 고정·surface (release cmd 고정)."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_2", "repo": "A", "session": "job4", "state": "leased"},
    ])
    fake = _FakeRun(0, "14 passed, 800 deselected in 3s")
    monkeypatch.setattr(board.subprocess, "run", fake)
    # engine-root sidecar 해소를 솔로 폴백으로(_resolve_livegate_flag 가 hooksPath 미설정 시 정상).
    board.cmd_livegate(_lg_args(task="job4"))
    expected_cwd = str(board.REPO / "work" / "A_2")
    # 마지막(release) subprocess 호출의 cwd 가 F6 슬롯 절대경로.
    assert fake.calls[-1]["kwargs"]["cwd"] == expected_cwd
    out = capsys.readouterr().out
    assert f"작업공간(task job4) → {expected_cwd}" in out


# ── _actor_session_override — task-mode 귀속(F5b·claimed_by=<user>/<task>) ─────


def test_actor_session_override_task_mode_returns_task(board):
    """task 지정 시 귀속 세션 override = task 이름(F5b) — claim/new created_by 가 <user>/<task>."""
    ns = argparse.Namespace(repo=None, slot=None, task="myjob")
    assert board._actor_session_override(ns) == "myjob"
    # --repo --slot 공존해도 task 가 귀속을 이긴다(⑥ 예약으로 slot 세션과 기계 판별).
    ns2 = argparse.Namespace(repo="A", slot=1, task="myjob")
    assert board._actor_session_override(ns2) == "myjob"


# ── board.py new — task 설정 prefix(F5) + created_by=<user>/<task> ────────────


def test_new_task_prefix_and_created_by(board, monkeypatch):
    """new --task: --prefix 생략 시 task 설정 prefix 사용·created_by=<user>/<task> (F5·F5b)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({
        "leases": [],
        "tasks": [{"name": "job5", "prefix": "PAY"}],
    }), encoding="utf-8")
    seen = {}

    def _fake_id_prefix(override, session=None):
        seen["override"] = override
        return override

    def _fake_tag(session_override=None, user_override=None):
        seen["created_by"] = f"{user_override or 'u'}/{session_override}"
        return seen["created_by"]

    monkeypatch.setattr(board, "id_prefix", _fake_id_prefix)
    monkeypatch.setattr(board, "identity_tag", _fake_tag)
    monkeypatch.setattr(board, "registered_prefixes", lambda: [])
    monkeypatch.setattr(board, "_board_git_enabled", lambda: False)
    monkeypatch.setattr(board, "refresh_board", lambda: None)
    monkeypatch.setattr(board, "_board_git_sync_best_effort", lambda msg: None)
    monkeypatch.setattr(board, "_next_id", lambda prefix: "PAY-0001")
    # 티켓 파일 실쓰기는 tmp 로 격리 (tickets_dir/template_file 은 REPO 파생이라 tmp).
    (board.REPO / ".project_manager").mkdir(parents=True, exist_ok=True)
    tmpl = board.REPO / "tmpl.md"
    tmpl.write_text("---\nid: T-NNNN\ntitle: <제목>\n---\n본문\n", encoding="utf-8")
    monkeypatch.setattr(board, "template_file", lambda: tmpl)
    monkeypatch.setattr(board, "tickets_dir", lambda: board.REPO / "tickets")
    (board.REPO / "tickets" / "open").mkdir(parents=True, exist_ok=True)

    ns = argparse.Namespace(title="X", touches=None, depends=None, tag=None,
                            estimate="small", prefix=None, user="smahn", task="job5")
    rc = board.cmd_new(ns)
    assert rc == 0
    assert seen["override"] == "PAY"                 # task 설정 prefix 적용(F5)
    assert seen["created_by"] == "smahn/job5"         # created_by=<user>/<task>(F5b)


# ── ticket_finish.py — task-mode 회귀 작업공간 F6 배선 ───────────────────────


@pytest.fixture
def tf(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    mod = _load("ticket_finish")
    monkeypatch.setattr(mod, "REPO", proj)
    monkeypatch.setattr(mod, "LEASES_FILE", proj / ".project_manager" / ".local" / "worktree-leases.json")
    monkeypatch.setattr(mod, "_guard_worktree_misanchor", lambda: False)
    monkeypatch.setattr(mod, "_regression_cwd", lambda slot=None: f"CWD::{slot}")
    mod._proj = proj
    return mod


def test_ticket_finish_task_resolves_regression_cwd(tf, monkeypatch, capsys):
    """ticket_finish --task → F6 로 슬롯 특정 후 그 worktree 를 회귀 cwd 로 forward·절대경로 surface."""
    _write_leases(tf.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job6", "state": "leased"},
    ])
    captured = {}

    class _FakeFinisher:
        def __init__(self, regression_cwd=None):
            captured["regression_cwd"] = regression_cwd

        def run(self, **kwargs):
            captured["run"] = kwargs
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _FakeFinisher)
    rc = tf.main(["T-0001", "--task", "job6"])
    assert rc == 0
    assert captured["regression_cwd"] == "CWD::work/A_1"   # F6 슬롯을 회귀 cwd 로 forward
    out = capsys.readouterr().out
    assert str(tf.REPO / "work" / "A_1") in out             # 절대경로 surface


def test_ticket_finish_task_ambiguous_fails_loud(tf, monkeypatch, capsys):
    """--task 가 2개↑ 보유(모호·⑦) → fail-loud rc1 (암묵 선택 안 함)."""
    _write_leases(tf.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job7", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "job7", "state": "leased"},
    ])
    monkeypatch.setattr(tf, "TicketFinisher",
                        lambda **kw: types.SimpleNamespace(run=lambda **k: 0))
    rc = tf.main(["T-0001", "--task", "job7"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "작업공간 해소" in err


# ── MUST-FIX (T-0355 게이트): 정체성 깔때기 task 명 검증 — 영속 전 fail-loud ──────
# 무검증 task 명이 created_by/claimed_by/lease-session 으로 새는 클래스를 _actor_session_override
# 깔때기(claim/new/regression/livegate/migrate/reid 공유)에서 한 번에 닫는다. 부작용(파일 write·
# 부기) 이전 fail-loud + 정상 task 는 통과.


@pytest.mark.parametrize("bad", ["../evil", "a/b", "my task", "foo)bar", ".hidden"])
def test_actor_override_rejects_unsafe_task_before_side_effect(board, bad):
    """_actor_session_override 깔때기가 불법 task 명을 SystemExit 로 거부(char/traversal 검증)."""
    ns = argparse.Namespace(repo=None, slot=None, task=bad)
    with pytest.raises(SystemExit):
        board._actor_session_override(ns)


def test_actor_override_rejects_reserved_slot_pattern_task(board, monkeypatch):
    """등록 repo 의 `<repo>_<N>` 예약 패턴 task 명 거부(⑥·registered_repos fail-soft 해소)."""
    monkeypatch.setattr(board, "registered_repos", lambda: {"project_manager"})
    ns = argparse.Namespace(repo=None, slot=None, task="project_manager_1")
    with pytest.raises(SystemExit):
        board._actor_session_override(ns)


def test_actor_override_registered_repos_failsoft(board, monkeypatch):
    """areas 파싱 실패(registered_repos raise)여도 char/traversal 검증은 유지·정상 task 통과."""
    def _boom():
        raise RuntimeError("areas broken")
    monkeypatch.setattr(board, "registered_repos", _boom)
    # 정상 task 는 통과(예약패턴만 완화·char 검증 유지).
    assert board._actor_session_override(argparse.Namespace(repo=None, slot=None, task="job1")) == "job1"
    # 불법 char 는 여전히 거부.
    with pytest.raises(SystemExit):
        board._actor_session_override(argparse.Namespace(repo=None, slot=None, task="../evil"))


def test_new_rejects_unsafe_task_no_file_written(board, monkeypatch):
    """new --task <불법> → rc≠0(SystemExit) · 티켓 파일/부기 부작용 0 (깔때기 경유)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({"leases": [], "tasks": []}), encoding="utf-8")
    tickets_open = board.REPO / "tickets" / "open"
    monkeypatch.setattr(board, "tickets_dir", lambda: board.REPO / "tickets")
    tickets_open.mkdir(parents=True, exist_ok=True)
    # ID 발행/파일 write 이전에 거부되므로 template 등 미도달.
    ns = argparse.Namespace(title="X", touches=None, depends=None, tag=None,
                            estimate="small", prefix=None, user="smahn", task="../evil")
    with pytest.raises(SystemExit):
        board.cmd_new(ns)
    assert list(tickets_open.glob("*.md")) == []   # 부작용 0


def test_claim_rejects_unsafe_task_before_mutation(board, monkeypatch):
    """claim --task <불법> → SystemExit(깔때기) · claimed_by 영속 이전 (board mutation 미도달)."""
    ns = argparse.Namespace(id="T-0001", repo=None, slot=None, task="foo)bar", user=None)
    with pytest.raises(SystemExit):
        board.cmd_claim(ns)


def test_ticket_finish_rejects_unsafe_task(tf, monkeypatch, capsys):
    """ticket_finish --task <불법> → rc1 (F6 해소 이전 공유 validator·부기 부작용 0)."""
    called = {"finisher": False}
    monkeypatch.setattr(tf, "TicketFinisher",
                        lambda **kw: called.__setitem__("finisher", True))
    rc = tf.main(["T-0001", "--task", "../evil"])
    assert rc == 1
    assert called["finisher"] is False          # finisher 미도달(부기 부작용 0)
    err = capsys.readouterr().err
    assert "부적합 task 명" in err
