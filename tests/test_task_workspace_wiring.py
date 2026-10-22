"""T-0355 — task-mode 작업공간(F6) 도구 배선 + 해소 절대경로 surface (spike §3b F6·결정 ⑦).

lease_store.resolve_task_workspace 의 단위 검증은 test_identity_args.py 가 담당하고, 여기선
그 F6 해소가 실행-위치 필요 도구(`board.py regression run`·`livegate record`)에
**배선**돼 (a) 특정 슬롯 worktree **절대경로**로 cwd 를 고정하고 (b) 그 절대경로를 surface 하며
(c) cwd 는 해소에 참여하지 않음(T-0345 불변)을 본다. `ticket_finish.py` 는 F6 대상이 아니다 —
`--task` 는 소유 확인 정체성으로만 가고 작업 트리는 종결이 묶음 기록의 브랜치로 찾는다.
실 pytest·git 은 절대 기동하지 않는다(subprocess.run 대역·전량 mock — 프로덕션 라이브 실행 금지).

hermetic — board/ticket_finish 의 경로 전역(`REPO`·`LEASES_FILE` 등)이 import 시점에 실 repo
절대경로로 굳으므로 tmp 프로젝트로 재지정한다(test_board_livegate hermetic 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import subprocess
import types
from pathlib import Path

import pytest
from _pytest_summary import pytest_summary
from pm_engine.infra import areas_store, git, lease_store, pm_paths, regression_store
from pm_engine.app import regression
from pm_engine.cli import regression_cmd

from pm_engine.app import board_dashboard
from pm_engine.app import board_sync
from pm_engine.cli import task_args
from pm_engine.infra import board_git
from pm_engine.infra import lease_store
from pm_engine.infra import ticket_store

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
    # `LOCAL_CONF` 는 import 시점에 **실 repo** 로 굳으므로 REPO 재지정만으론 안 따라온다 —
    # 핀하지 않으면 개발 머신 local.conf 의 선언(test.cmd·qa.platforms)이 스텁 회귀에
    # 새어들어 엉뚱한 명령을 돌린다. tmp 로 핀해 conf 면역을 만든다.
    monkeypatch.setattr(mod, "LOCAL_CONF", proj / ".project_manager" / "local.conf")
    monkeypatch.setattr(mod, "LOCAL_DIR", local)
    monkeypatch.setattr(mod, "LEASES_FILE", local / "worktree-leases.json")
    monkeypatch.setattr(git, "_git_head", lambda _home: "deadbeefdeadbeef")
    monkeypatch.setattr(git, "_git_head_at", lambda cwd: "deadbeefdeadbeef")
    monkeypatch.setattr(git, "_cluster_current_branch", lambda tree: "task/main")
    mod._proj = proj
    return mod


class _FakeProc:
    """`subprocess.Popen` 반환 대역 — 회귀 러너가 두 스트림을 tee 하며 읽는 형태를 만족한다."""

    def __init__(self, rc: int, stdout: str = "", stderr: str = ""):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self._rc = rc

    def wait(self):
        return self._rc


class _FakeRun:
    """regression run 은 `Popen`(tee), livegate record 는 `run`(캡처) — 양쪽 대역을 겸한다."""

    def __init__(self, rc: int = 0, stdout: str = ""):
        self.rc = rc
        self.stdout = stdout
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if kwargs.get("stdout") is subprocess.PIPE and "capture_output" not in kwargs:
            return _FakeProc(self.rc, self.stdout)      # Popen 경로(회귀 tee).
        return types.SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr="")


def _install_fake(board, monkeypatch, fake):
    """회귀(Popen tee)·livegate(run 캡처) 두 seam 을 같은 대역으로 덮는다 (실 자식 0)."""
    # 실행 child 대역에 metadata 조회가 섞이지 않게 가짜 Git fixture를 격리한다.
    monkeypatch.setattr(git, "_git_config_get", lambda cwd, key: None)
    monkeypatch.setattr(board.subprocess, "run", fake)
    monkeypatch.setattr(board.subprocess, "Popen", fake)
    return fake


def _reg_args(task=None, repo=None, slot=None, cmd=None, cwd=None):
    return argparse.Namespace(action="run", cmd=cmd, cwd=cwd, repo=repo, slot=slot,
                              task=task, ticket=None, touches=None)


def test_regression_run_task_threads_absolute_cwd_and_surfaces(board, monkeypatch, capsys):
    """--task 로 F6 해소된 슬롯 worktree 절대경로가 (a) subprocess cwd 로 고정·(b) surface 된다."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job1", "state": "leased",
         "test_cmd": "pytest -q"},
    ])
    target = board.REPO / "work/A_1"
    (target / ".git").mkdir(parents=True)
    fake = _FakeRun(0, pytest_summary(10))
    _install_fake(board, monkeypatch, fake)
    rc = regression_cmd.cmd_regression(board._home(), _reg_args(task="job1"))
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
    _install_fake(board, monkeypatch, _FakeRun(0, "ok"))
    with pytest.raises(SystemExit):
        regression_cmd.cmd_regression(board._home(), _reg_args(task="job2"))


def test_regression_run_task_repo_selects_unique_owned_workspace(board, monkeypatch):
    """task가 다른 repo 슬롯을 여럿 보유해도 repo 유일 선택은 허용한다."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job3", "state": "leased", "test_cmd": "pytest -q"},
        {"slot": "work/B_1", "repo": "B", "session": "job3", "state": "leased"},
    ])
    (board.REPO / "work/A_1/.git").mkdir(parents=True)
    (board.REPO / "work/A_1/tests").mkdir()
    fake = _install_fake(board, monkeypatch, _FakeRun(0, pytest_summary(10)))
    assert regression_cmd.cmd_regression(board._home(), _reg_args(task="job3", repo="A")) == 0
    assert fake.calls[0]["kwargs"]["cwd"] == str(board.REPO / "work/A_1")


def test_regression_run_rejects_task_cwd_before_subprocess(board, monkeypatch, tmp_path):
    """task 작업공간 표시와 타 경로 실행을 섞는 명시 --cwd 우회를 거부한다."""
    fake = _FakeRun(0, pytest_summary())
    _install_fake(board, monkeypatch, fake)

    with pytest.raises(SystemExit) as exc:
        regression_cmd.cmd_regression(board._home(), _reg_args(task="job3", cwd=str(tmp_path / "outside")))

    assert "외부 경로 override를 허용하지 않는다" in str(exc.value)
    assert "--cwd" in str(exc.value)
    assert fake.calls == []


def test_regression_run_non_task_cwd_remains_override(board, monkeypatch, tmp_path):
    """비-task regression run의 명시 --cwd는 기존처럼 그대로 subprocess에 전달된다."""
    target = tmp_path / "readonly"
    target.mkdir()
    fake = _FakeRun(0, pytest_summary())
    _install_fake(board, monkeypatch, fake)

    assert regression_cmd.cmd_regression(board._home(), _reg_args(cwd=str(target), cmd="pytest -q")) == 0

    assert fake.calls[0]["kwargs"]["cwd"] == str(target)


def _lg_args(task=None, repo=None, slot=None, cwd=None):
    return argparse.Namespace(action="record", rev=None, cwd=cwd, repo=repo, slot=slot, task=task)


def test_livegate_record_task_threads_absolute_cwd(board, monkeypatch, capsys):
    """livegate record --task → F6 슬롯 worktree 절대경로를 cwd 로 고정·surface (release cmd 고정)."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_2", "repo": "A", "session": "job4", "state": "leased"},
    ])
    (board.REPO / "work/A_2/.git").mkdir(parents=True)
    fake = _FakeRun(0, pytest_summary(14, deselected=800))
    _install_fake(board, monkeypatch, fake)
    # engine-root sidecar 해소를 솔로 폴백으로(_resolve_livegate_flag 가 hooksPath 미설정 시 정상).
    board.cmd_livegate(_lg_args(task="job4"))
    expected_cwd = str(board.REPO / "work" / "A_2")
    # 마지막(release) subprocess 호출의 cwd 가 F6 슬롯 절대경로.
    assert fake.calls[-1]["kwargs"]["cwd"] == expected_cwd
    out = capsys.readouterr().out
    assert f"작업공간(task job4) → {expected_cwd}" in out


def test_livegate_record_rejects_task_slot_mix_before_subprocess(board, monkeypatch):
    """task 실행 선택도 bare-slot은 repo 좌표 부재로 거부한다."""
    fake = _FakeRun(0, pytest_summary(14, deselected=800))
    _install_fake(board, monkeypatch, fake)
    with pytest.raises(SystemExit) as exc:
        board.cmd_livegate(_lg_args(task="job4", slot=2))
    assert "--slot 은 --repo 필수" in str(exc.value)
    assert fake.calls == []


def test_livegate_record_rejects_task_cwd_before_subprocess(board, monkeypatch, tmp_path):
    """task livegate가 task surface를 내면서 타 경로에 green을 기록하는 --cwd 우회를 거부한다."""
    fake = _FakeRun(0, pytest_summary(18))
    _install_fake(board, monkeypatch, fake)

    with pytest.raises(SystemExit) as exc:
        board.cmd_livegate(_lg_args(task="job4", cwd=str(tmp_path / "outside")))

    assert "외부 경로 override를 허용하지 않는다" in str(exc.value)
    assert "--cwd" in str(exc.value)
    assert fake.calls == []
    assert not pm_paths.livegate_flag(board._home()).exists()


def test_livegate_record_non_task_cwd_remains_override(board, monkeypatch, tmp_path):
    """비-task livegate record의 명시 --cwd는 기존처럼 실행·기록 기준 경로로 유지된다."""
    target = tmp_path / "readonly"
    target.mkdir()
    fake = _FakeRun(0, pytest_summary(regression.LIVEGATE_RELEASE_PIN))
    _install_fake(board, monkeypatch, fake)
    monkeypatch.setattr(
        regression_store,
        "_resolve_livegate_flag",
        lambda home, cwd: (pm_paths.livegate_flag(home), regression_store._LG_NO_HOOK),
    )

    assert board.cmd_livegate(_lg_args(cwd=str(target))) == 0

    assert fake.calls[-1]["kwargs"]["cwd"] == str(target)


# ── _actor_session_override — task-mode 귀속(F5b·claimed_by=<user>/<task>) ─────


def test_actor_session_override_task_mode_returns_task(board):
    """task 지정 시 귀속 세션 override = task 이름 — claim/new created_by 가 <user>/<task>."""
    ns = argparse.Namespace(task="myjob")
    assert task_args._actor_session_override(board._home(), ns) == "myjob"
    # 비실행 명령은 task+repo/slot 혼합을 조용히 task 우선하지 않는다 — parser 가 `--repo`/`--slot`
    # 자체를 받지 않는다(T-0962).
    with pytest.raises(SystemExit) as exc:
        board.build_parser().parse_args(
            ["complete", "T-0001", "--task", "myjob", "--repo", "A", "--slot", "1"])
    assert exc.value.code == 2


_PROVENANCE_MIX_ARGV = {
    "init": ["init", "--task", "job", "--repo", "A", "--slot", "1"],
    "migrate-identity": ["migrate-identity", "--task", "job", "--repo", "A", "--slot", "1"],
    "reid": ["reid", "T-0001", "T-0002", "--task", "job", "--repo", "A", "--slot", "1"],
}


@pytest.mark.parametrize("command", ["init", "migrate-identity", "reid"])
def test_board_provenance_commands_reject_task_repo_mix_at_ingress(board, command):
    """세션 provenance 소비 3종은 본체 부작용 전에 parser 에서 위치 인자 `--slot` 을 거부한다(rc 2).

    폴더를 고르지 않는 명령이라 `--slot` 이 없다(init 의 `--repo` 는 등록 이름이라 남는다·T-0962).
    """
    with pytest.raises(SystemExit) as exc:
        board.build_parser().parse_args(_PROVENANCE_MIX_ARGV[command])
    assert exc.value.code == 2


# ── board.py new — task 설정 prefix(F5) + created_by=<user>/<task> ────────────


def test_new_task_prefix_and_created_by(board, monkeypatch):
    """new --task: --prefix 생략 시 task 설정 prefix 사용·created_by=<user>/<task> (F5·F5b)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({
        "leases": [],
        "tasks": [{"name": "job5", "prefix": "PAY"}],
    }), encoding="utf-8")
    # prefix/귀속만 검증한다. 제품 기준 조회는 대역이며 실 Git 결속은 전용 계약 사례가 검증한다.
    monkeypatch.setattr(board, "_cluster_publish_base_branch",
                        lambda args: (str(board.REPO / "product"), "task/main")
                        if getattr(args, "task", None) else (None, None))
    seen = {}

    def _fake_id_prefix(_home, override, session=None):
        seen["override"] = override
        return override

    def _fake_tag(_home, session_override=None, user_override=None):
        seen["created_by"] = f"{user_override or 'u'}/{session_override}"
        return seen["created_by"]

    monkeypatch.setattr(areas_store, "id_prefix", _fake_id_prefix)
    monkeypatch.setattr(lease_store, "identity_tag", _fake_tag)
    monkeypatch.setattr(areas_store, "registered_prefixes", lambda _home: [])
    monkeypatch.setattr(board_git, "_board_git_enabled", lambda _home: False)
    monkeypatch.setattr(board_dashboard, "refresh_board", lambda _home: None)
    monkeypatch.setattr(board_sync, "_board_git_sync_best_effort",
                        lambda _home, msg, paths=None: None)   # 스코프 인자(ADR-0073)
    # 발행 prefix 는 `_next_id` 인자가 관측점이다 — T-0660 이후 cmd_new 는 명시/task 값을
    # `id_prefix` 로 canonical화하지 않고 4소스 funnel 로 바로 넘긴다(비결정 선택 제거).
    monkeypatch.setattr(ticket_store, "_next_id",
                        lambda _home, prefix: seen.__setitem__("override", prefix) or "PAY-0001")
    # 티켓 파일 실쓰기는 tmp 로 격리 (tickets_dir/template_file 은 REPO 파생이라 tmp).
    (board.REPO / ".project_manager").mkdir(parents=True, exist_ok=True)
    tmpl = board.REPO / "tmpl.md"
    tmpl.write_text("---\nid: T-NNNN\ntitle: <제목>\n---\n본문\n", encoding="utf-8")
    monkeypatch.setattr(pm_paths, "template_file", lambda _home: tmpl)
    monkeypatch.setattr(pm_paths, "tickets_dir", lambda _home: board.REPO / "tickets")
    (board.REPO / "tickets" / "open").mkdir(parents=True, exist_ok=True)

    ns = argparse.Namespace(title="X", touches=None, depends=None, tag=None,
                            estimate="small", prefix=None, user="smahn", task="job5")
    rc = board.cmd_new(ns)
    assert rc == 0
    assert seen["override"] == "PAY"                 # task 설정 prefix 적용(F5)
    assert seen["created_by"] == "smahn/job5"         # created_by=<user>/<task>(F5b)


def _stub_new_env(board, monkeypatch, *, tasks):
    """cmd_new 3단 해소 테스트용 공통 stub — id_prefix override 캡처 + 부작용 격리(F5·T-0357)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({"leases": [], "tasks": tasks}), encoding="utf-8")
    # prefix/귀속만 검증한다. 제품 기준 조회는 대역이며 실 Git 결속은 전용 계약 사례가 검증한다.
    monkeypatch.setattr(board, "_cluster_publish_base_branch",
                        lambda args: (str(board.REPO / "product"), "task/main")
                        if getattr(args, "task", None) else (None, None))
    seen = {}
    monkeypatch.setattr(areas_store, "id_prefix",
                        lambda _home, override: seen.__setitem__("override", override) or override)
    monkeypatch.setattr(lease_store, "identity_tag",
                        lambda _home, session_override=None, user_override=None: "u/x")
    monkeypatch.setattr(areas_store, "registered_prefixes", lambda _home: [])
    monkeypatch.setattr(board_git, "_board_git_enabled", lambda _home: False)
    monkeypatch.setattr(board_dashboard, "refresh_board", lambda _home: None)
    monkeypatch.setattr(board_sync, "_board_git_sync_best_effort",
                        lambda _home, msg, paths=None: None)   # 스코프 인자(ADR-0073)
    monkeypatch.setattr(ticket_store, "_next_id",
                        lambda _home, prefix: seen.__setitem__("override", prefix)
                        or f"{prefix or 'T'}-0001")
    (board.REPO / ".project_manager").mkdir(parents=True, exist_ok=True)
    tmpl = board.REPO / "tmpl.md"
    tmpl.write_text("---\nid: T-NNNN\ntitle: <제목>\n---\n본문\n", encoding="utf-8")
    monkeypatch.setattr(pm_paths, "template_file", lambda _home: tmpl)
    monkeypatch.setattr(pm_paths, "tickets_dir", lambda _home: board.REPO / "tickets")
    (board.REPO / "tickets" / "open").mkdir(parents=True, exist_ok=True)
    return seen


def test_new_explicit_prefix_wins_over_task(board, monkeypatch):
    """3단 해소 tier1 — 명시 `--prefix` 가 task 설정 prefix 를 이긴다(1회 오버라이드·F5)."""
    seen = _stub_new_env(board, monkeypatch, tasks=[{"name": "job5", "prefix": "PAY"}])
    ns = argparse.Namespace(title="X", touches=None, depends=None, tag=None,
                            estimate="small", prefix="ACC", user="smahn", task="job5",
                            user_ack="ACC")
    assert board.cmd_new(ns) == 0
    assert seen["override"] == "ACC"                 # 명시 --prefix 가 task 설정(PAY)을 이김(tier1)


def test_new_no_prefix_no_task_defaults_none(board, monkeypatch):
    """3단 해소 tier3 — `--prefix` 없음 + task prefix 없음 → 무prefix(기본 없음)."""
    seen = _stub_new_env(board, monkeypatch, tasks=[{"name": "job0"}])
    ns = argparse.Namespace(title="X", touches=None, depends=None, tag=None,
                            estimate="small", prefix=None, user="smahn", task="job0")
    assert board.cmd_new(ns) == 0
    assert seen["override"] is None                  # 무prefix → id_prefix(None) → legacy T-NNNN(tier3)


# ── ticket_finish.py — `--task` 는 소유 확인 정체성으로만 간다 ───────────────


@pytest.fixture
def tf(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    mod = _load("ticket_finish")
    monkeypatch.setattr(mod, "REPO", proj)
    monkeypatch.setattr(mod, "LEASES_FILE", proj / ".project_manager" / ".local" / "worktree-leases.json")
    monkeypatch.setattr(mod, "_guard_worktree_misanchor", lambda: False)
    mod._proj = proj
    return mod


def _capture_finish_forward(tf, monkeypatch) -> dict:
    """`main` 이 해소해 넘긴 값을 부작용 없이 캡처한다.

    완료 기록 단위는 묶음 하나다(티켓 하나 = 크기 1) — 실행 좌표(세션·정체성·회귀 skip)는 종결
    파이프라인 생성 인자로 간다. 코드 트리는 `main` 이 정하지 않는다(종결이 묶음 기록의 브랜치로
    찾는다) — `TicketFinisher` 생성 인자는 그 사실을 보는 관측점이다. 여기서는 `main` 의
    해소·forward 만 본다.
    """
    captured: dict = {}

    class _FakeFinisher:
        def __init__(self, **kwargs):
            captured["finisher_kwargs"] = kwargs

    class _SpyCloser:
        def __init__(self, cluster, **kwargs):
            captured["cluster"] = cluster
            captured.update(kwargs)

        def run(self) -> int:
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _FakeFinisher)
    monkeypatch.setattr(tf, "ClusterCloser", _SpyCloser)
    return captured


def test_ticket_finish_task_forwards_identity_only(tf, monkeypatch, capsys):
    """AT-008: task 가 슬롯을 여럿 쥐어도 `main` 은 멈추지 않고 정체성만 넘긴다.

    옛 경로는 task 보유 슬롯에서 작업공간을 골라 같은 task 대여가 둘이면 "통틀어 모호" 로
    멈췄다. 작업 트리는 종결이 묶음 기록의 브랜치로 찾으므로 `TicketFinisher` 에 트리 인자가
    없고, task 는 board complete 소유 대조(T-0781)의 `--task` 축으로만 간다.
    """
    _write_leases(tf.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job7", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "job7", "state": "leased"},
    ])
    captured = _capture_finish_forward(tf, monkeypatch)

    rc = tf.main(["T-0001", "--task", "job7"])

    assert rc == 0
    assert captured["finisher_kwargs"] == {}
    assert captured["board_identity_args"] == ["--task", "job7"]
    assert captured["task"] == "job7" and captured["slot"] is None
    assert "작업공간 해소" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [["--repo", "A"], ["--repo", "A", "--slot", "1"], ["--slot", "1"]],
)
def test_ticket_finish_rejects_task_workspace_pin_mix_before_finisher(
    tf, monkeypatch, capsys, extra
):
    """ticket_finish task+repo/slot 혼합은 usage error이며 finisher 부작용에 도달하지 않는다(E-b)."""
    called = {"finisher": False}
    monkeypatch.setattr(
        tf,
        "TicketFinisher",
        lambda **kw: called.__setitem__("finisher", True),
    )
    with pytest.raises(SystemExit) as exc:
        tf.main(["T-0001", "--task", "job10", *extra])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--repo/--slot 은 --reconcile-integrated 의 제품 트리 지정에만 쓴다" in err
    if extra == ["--slot", "1"]:
        assert "--slot 은 --repo 필수" not in err
    assert called["finisher"] is False


# ── MUST-FIX (T-0355 게이트): 정체성 깔때기 task 명 검증 — 영속 전 fail-loud ──────
# 무검증 task 명이 created_by/claimed_by/lease-session 으로 새는 클래스를 _actor_session_override
# 깔때기(claim/new/regression/livegate/migrate/reid 공유)에서 한 번에 닫는다. 부작용(파일 write·
# 기록) 이전 fail-loud + 정상 task 는 통과.


@pytest.mark.parametrize("bad", ["../evil", "a/b", "my task", "foo)bar", ".hidden"])
def test_actor_override_rejects_unsafe_task_before_side_effect(board, bad):
    """_actor_session_override 깔때기가 불법 task 명을 SystemExit 로 거부(char/traversal 검증)."""
    ns = argparse.Namespace(repo=None, slot=None, task=bad)
    with pytest.raises(SystemExit):
        task_args._actor_session_override(board._home(), ns)


def test_actor_override_rejects_reserved_slot_pattern_task(board, monkeypatch):
    """등록 repo 의 `<repo>_<N>` 예약 패턴 task 명 거부(⑥·registered_repos fail-soft 해소)."""
    monkeypatch.setattr(areas_store, "registered_repos", lambda _home: {"project_manager"})
    ns = argparse.Namespace(repo=None, slot=None, task="project_manager_1")
    with pytest.raises(SystemExit):
        task_args._actor_session_override(board._home(), ns)


def test_actor_override_registered_repos_failsoft(board, monkeypatch):
    """areas 파싱 실패(registered_repos raise)여도 char/traversal 검증은 유지·정상 task 통과."""
    def _boom(_home):
        raise RuntimeError("areas broken")
    monkeypatch.setattr(areas_store, "registered_repos", _boom)
    # 정상 task 는 통과(예약패턴만 완화·char 검증 유지).
    assert task_args._actor_session_override(board._home(), argparse.Namespace(repo=None, slot=None, task="job1")) == "job1"
    # 불법 char 는 여전히 거부.
    with pytest.raises(SystemExit):
        task_args._actor_session_override(board._home(), argparse.Namespace(repo=None, slot=None, task="../evil"))


def test_new_rejects_unsafe_task_no_file_written(board, monkeypatch):
    """new --task <불법> → rc≠0(SystemExit) · 티켓 파일/기록 부작용 0 (깔때기 경유)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({"leases": [], "tasks": []}), encoding="utf-8")
    tickets_open = board.REPO / "tickets" / "open"
    monkeypatch.setattr(pm_paths, "tickets_dir", lambda _home: board.REPO / "tickets")
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
    """ticket_finish --task <불법> → rc1 (F6 해소 이전 공유 validator·기록 부작용 0)."""
    called = {"finisher": False}
    monkeypatch.setattr(tf, "TicketFinisher",
                        lambda **kw: called.__setitem__("finisher", True))
    rc = tf.main(["T-0001", "--task", "../evil"])
    assert rc == 1
    assert called["finisher"] is False          # finisher 미도달(기록 부작용 0)
    err = capsys.readouterr().err
    assert "부적합 task 명" in err


def _t0929_mock_workspaces(board, monkeypatch):
    rows = []
    for n in (1, 5):
        path = board.REPO / f'work/A_{n}'
        path.mkdir(parents=True, exist_ok=True)
        (path / '.git').mkdir()
        (path / 'tests').mkdir()
        rows.append({'slot': f'work/A_{n}', 'repo': 'A', 'session': 'main',
                     'role': 'work', 'state': 'leased', 'test_cmd': f'pytest -q -k slot{n}'})
    _write_leases(board.LEASES_FILE, rows)
    monkeypatch.setattr(git, '_cluster_current_branch', lambda tree: f'task/slot{Path(tree).name[-1]}')
    monkeypatch.setattr(git, '_git_head_at', lambda tree: f'head{Path(tree).name[-1]}')
    return rows


@pytest.mark.parametrize('consumer', ['regression', 'livegate'])
def test_t0929_consumers_selected_workspace(board, monkeypatch, consumer):
    _t0929_mock_workspaces(board, monkeypatch)
    fake = _install_fake(board, monkeypatch, _FakeRun(0, pytest_summary(22)))
    args = (_reg_args(task='main', repo='A', slot=5) if consumer == 'regression'
            else _lg_args(task='main', repo='A', slot=5))
    call = (lambda a: regression_cmd.cmd_regression(board._home(), a)) if consumer == 'regression' else board.cmd_livegate
    assert call(args) == 0
    assert next(call for call in fake.calls if call['kwargs'].get('shell'))['kwargs']['cwd'] == str(board.REPO / 'work/A_5')
    assert lease_store.session_name(board._home(), 'main') == 'main'


@pytest.mark.parametrize('consumer', ['regression', 'livegate'])
def test_t0929_consumers_readonly_write_refused(board, monkeypatch, consumer):
    rows = _t0929_mock_workspaces(board, monkeypatch)
    rows[-1]['role'] = 'readonly'
    _write_leases(board.LEASES_FILE, rows)
    fake = _install_fake(board, monkeypatch, _FakeRun(0, pytest_summary(22)))
    args = (_reg_args(task='main', repo='A', slot=5) if consumer == 'regression'
            else _lg_args(task='main', repo='A', slot=5))
    with pytest.raises(SystemExit, match='readonly'):
        ((lambda a: regression_cmd.cmd_regression(board._home(), a)) if consumer == 'regression' else board.cmd_livegate)(args)
    assert fake.calls == []
    assert not pm_paths.regression_flag(board._home()).exists()
    assert not pm_paths.livegate_flag(board._home()).exists()


@pytest.mark.parametrize('command', ['init', 'reid', 'migrate-identity'])
def test_t0929_consumers_session_commands_keep_mix_refusal(board, monkeypatch, command):
    _t0929_mock_workspaces(board, monkeypatch)
    argv = {'init': ['init'], 'reid': ['reid', 'T-0001', 'T-0002'],
            'migrate-identity': ['migrate-identity']}[command]
    with pytest.raises(SystemExit) as exc:   # 위치 인자 `--slot` 은 parser 가 받지 않는다(T-0962)
        board.build_parser().parse_args([*argv, '--task', 'main', '--repo', 'A', '--slot', '5'])
    assert exc.value.code == 2


def test_t0929_consumers_finish_keeps_mix_refusal(tf, monkeypatch):
    _capture_finish_forward(tf, monkeypatch)
    with pytest.raises(SystemExit):
        tf.main(['--cluster', 'C-wave', '--task', 'main', '--repo', 'A', '--slot', '5'])


@pytest.mark.parametrize('name', ['pm_bootstrap', 'pm_handoff'])
def test_t0929_consumers_bootstrap_handoff_keep_mix_refusal(name):
    module = _load(name)
    with pytest.raises(SystemExit):
        module.main(['--task', 'main', '--repo', 'A', '--slot', '5'])
