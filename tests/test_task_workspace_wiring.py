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
import io
import json
import subprocess
import types
from pathlib import Path

import pytest
from _pytest_summary import pytest_summary

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
    # 핀하지 않으면 개발 머신 local.conf(예: `regression_min_collected=7000`)가 스텁 회귀
    # (수집 1)에 새어들어 강등시킨다(실측 2 fail). tmp 로 핀해 conf 면역을 만든다.
    monkeypatch.setattr(mod, "LOCAL_CONF", proj / ".project_manager" / "local.conf")
    monkeypatch.setattr(mod, "LOCAL_DIR", local)
    monkeypatch.setattr(mod, "LEASES_FILE", local / "worktree-leases.json")
    monkeypatch.setattr(mod, "REGRESSION_FLAG", local / "regression.json")
    monkeypatch.setattr(mod, "LIVEGATE_FLAG", local / "livegate.json")
    monkeypatch.setattr(mod, "_git_head", lambda: "deadbeefdeadbeef")
    monkeypatch.setattr(mod, "_git_head_at", lambda cwd: "deadbeefdeadbeef")
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
    fake = _FakeRun(0, pytest_summary(10))
    _install_fake(board, monkeypatch, fake)
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
    _install_fake(board, monkeypatch, _FakeRun(0, "ok"))
    with pytest.raises(SystemExit):
        board.cmd_regression(_reg_args(task="job2"))


def test_regression_run_rejects_task_repo_mix_before_subprocess(board, monkeypatch):
    """regression run은 독립 task 정체성과 repo 실행 위치의 혼합을 fail-loud 거부한다."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job3", "state": "leased"},
        {"slot": "work/B_1", "repo": "B", "session": "job3", "state": "leased"},
    ])
    fake = _FakeRun(0, pytest_summary())
    _install_fake(board, monkeypatch, fake)
    with pytest.raises(SystemExit) as exc:
        board.cmd_regression(_reg_args(task="job3", repo="A"))
    assert (
        "--repo/--slot 과 함께 쓸 수 없다" in str(exc.value)
        and "장부에서 자동 해소" in str(exc.value)
    )
    assert fake.calls == []


def test_regression_run_rejects_task_cwd_before_subprocess(board, monkeypatch, tmp_path):
    """task 작업공간 표시와 타 경로 실행을 섞는 명시 --cwd 우회를 거부한다."""
    fake = _FakeRun(0, pytest_summary())
    _install_fake(board, monkeypatch, fake)

    with pytest.raises(SystemExit) as exc:
        board.cmd_regression(_reg_args(task="job3", cwd=str(tmp_path / "outside")))

    assert "외부 경로 override를 허용하지 않는다" in str(exc.value)
    assert "--cwd" in str(exc.value)
    assert fake.calls == []


def test_regression_run_non_task_cwd_remains_override(board, monkeypatch, tmp_path):
    """비-task regression run의 명시 --cwd는 기존처럼 그대로 subprocess에 전달된다."""
    target = tmp_path / "readonly"
    target.mkdir()
    fake = _FakeRun(0, pytest_summary())
    _install_fake(board, monkeypatch, fake)

    assert board.cmd_regression(_reg_args(cwd=str(target), cmd="pytest -q")) == 0

    assert fake.calls[0]["kwargs"]["cwd"] == str(target)


def _lg_args(task=None, repo=None, slot=None, cwd=None):
    return argparse.Namespace(action="record", rev=None, cwd=cwd, repo=repo, slot=slot, task=task)


def test_livegate_record_task_threads_absolute_cwd(board, monkeypatch, capsys):
    """livegate record --task → F6 슬롯 worktree 절대경로를 cwd 로 고정·surface (release cmd 고정)."""
    _write_leases(board.LEASES_FILE, [
        {"slot": "work/A_2", "repo": "A", "session": "job4", "state": "leased"},
    ])
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
    """livegate record의 task+slot 혼합은 bare-slot repo 힌트보다 혼합 금지를 먼저 표면화한다."""
    fake = _FakeRun(0, pytest_summary(14, deselected=800))
    _install_fake(board, monkeypatch, fake)
    with pytest.raises(SystemExit) as exc:
        board.cmd_livegate(_lg_args(task="job4", slot=2))
    assert "--task 는 독립 정체성" in str(exc.value)
    assert "--slot 은 --repo 필수" not in str(exc.value)
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
    assert not board.LIVEGATE_FLAG.exists()


def test_livegate_record_non_task_cwd_remains_override(board, monkeypatch, tmp_path):
    """비-task livegate record의 명시 --cwd는 기존처럼 실행·기록 기준 경로로 유지된다."""
    target = tmp_path / "readonly"
    target.mkdir()
    fake = _FakeRun(0, pytest_summary(board.LIVEGATE_RELEASE_PIN))
    _install_fake(board, monkeypatch, fake)
    monkeypatch.setattr(
        board,
        "_resolve_livegate_flag",
        lambda cwd: (board.LIVEGATE_FLAG, board._LG_SOLO),
    )

    assert board.cmd_livegate(_lg_args(cwd=str(target))) == 0

    assert fake.calls[-1]["kwargs"]["cwd"] == str(target)


# ── _actor_session_override — task-mode 귀속(F5b·claimed_by=<user>/<task>) ─────


def test_actor_session_override_task_mode_returns_task(board):
    """task 지정 시 귀속 세션 override = task 이름 — claim/new created_by 가 <user>/<task>."""
    ns = argparse.Namespace(repo=None, slot=None, task="myjob")
    assert board._actor_session_override(ns) == "myjob"
    # provenance 연산은 task+repo/slot 혼합을 조용히 task 우선하지 않고 거부한다.
    ns2 = argparse.Namespace(repo="A", slot=1, task="myjob")
    with pytest.raises(SystemExit) as exc:
        board._actor_session_override(ns2)
    assert "--repo/--slot 과 함께 쓸 수 없다" in str(exc.value)


@pytest.mark.parametrize("command", ["claim", "new", "init", "migrate-identity", "reid"])
def test_board_provenance_commands_reject_task_repo_mix_at_ingress(
    board, monkeypatch, command
):
    """actor provenance 소비 5종이 본체 부작용 전에 동일 혼합 금지 검증을 탄다."""
    args = {
        "claim": argparse.Namespace(id="T-0001", repo="A", slot=1, task="job", user=None),
        "new": argparse.Namespace(
            title="X", touches=None, depends=None, tag=None, estimate="small",
            prefix=None, user=None, repo="A", slot=1, task="job",
        ),
        "init": argparse.Namespace(
            prefix=None, area=None, owner=None, user=None, repo="A", slot=1, task="job",
        ),
        "migrate-identity": argparse.Namespace(
            dry_run=True, user="u", scope="all", repo="A", slot=1, task="job",
        ),
        "reid": argparse.Namespace(
            old_id="T-0001", new_id="T-0002", dry_run=True,
            repo="A", slot=1, task="job",
        ),
    }[command]
    fn = {
        "claim": board.cmd_claim,
        "new": board.cmd_new,
        "init": board.cmd_init,
        "migrate-identity": board.cmd_migrate_identity,
        "reid": board.cmd_reid,
    }[command]
    with pytest.raises(SystemExit) as exc:
        fn(args)
    assert "--repo/--slot 과 함께 쓸 수 없다" in str(exc.value)


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
    monkeypatch.setattr(board, "_board_git_sync_best_effort",
                        lambda msg, paths=None: None)   # 스코프 인자(ADR-0073)
    # 발행 prefix 는 `_next_id` 인자가 관측점이다 — T-0660 이후 cmd_new 는 명시/task 값을
    # `id_prefix` 로 canonical화하지 않고 4소스 funnel 로 바로 넘긴다(비결정 선택 제거).
    monkeypatch.setattr(board, "_next_id",
                        lambda prefix: seen.__setitem__("override", prefix) or "PAY-0001")
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


def _stub_new_env(board, monkeypatch, *, tasks):
    """cmd_new 3단 해소 테스트용 공통 stub — id_prefix override 캡처 + 부작용 격리(F5·T-0357)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({"leases": [], "tasks": tasks}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(board, "id_prefix",
                        lambda override, session=None: seen.__setitem__("override", override) or override)
    monkeypatch.setattr(board, "identity_tag",
                        lambda session_override=None, user_override=None: "u/x")
    monkeypatch.setattr(board, "registered_prefixes", lambda: [])
    monkeypatch.setattr(board, "_board_git_enabled", lambda: False)
    monkeypatch.setattr(board, "refresh_board", lambda: None)
    monkeypatch.setattr(board, "_board_git_sync_best_effort",
                        lambda msg, paths=None: None)   # 스코프 인자(ADR-0073)
    monkeypatch.setattr(board, "_next_id",
                        lambda prefix: seen.__setitem__("override", prefix)
                        or f"{prefix or 'T'}-0001")
    (board.REPO / ".project_manager").mkdir(parents=True, exist_ok=True)
    tmpl = board.REPO / "tmpl.md"
    tmpl.write_text("---\nid: T-NNNN\ntitle: <제목>\n---\n본문\n", encoding="utf-8")
    monkeypatch.setattr(board, "template_file", lambda: tmpl)
    monkeypatch.setattr(board, "tickets_dir", lambda: board.REPO / "tickets")
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
    """3단 해소 tier3 — `--prefix` 없음 + task 없음(또는 task prefix None) → 무prefix(기본 없음)."""
    seen = _stub_new_env(board, monkeypatch, tasks=[])
    ns = argparse.Namespace(title="X", touches=None, depends=None, tag=None,
                            estimate="small", prefix=None, user="smahn", task=None)
    assert board.cmd_new(ns) == 0
    assert seen["override"] is None                  # 무prefix → id_prefix(None) → legacy T-NNNN(tier3)


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
        def __init__(self, regression_cwd=None, task_workspace=None):
            captured["regression_cwd"] = regression_cwd
            captured["task_workspace"] = task_workspace

        def run(self, **kwargs):
            captured["run"] = kwargs
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _FakeFinisher)
    rc = tf.main(["T-0001", "--task", "job6"])
    assert rc == 0
    assert captured["regression_cwd"] == "CWD::work/A_1"   # F6 슬롯을 회귀 cwd 로 forward
    assert captured["task_workspace"] == tf.REPO / "work" / "A_1"
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


def test_ticket_finish_task_no_pytest_still_resolves_workspace(tf, monkeypatch, capsys):
    """--no-pytest 는 회귀만 생략: F6 worktree는 stage/status 계획을 위해 그대로 forward한다.

    해소 자체도 회귀 skip 여부와 무관하게 돈다 — 비-task 경로와 동형이고, 해소가 stale 슬롯
    (장부에는 있고 디스크에는 없음) 존재검사를 태운다. 결과 트리는 양쪽 같다.
    """
    _write_leases(tf.LEASES_FILE, [
        {"slot": "work/A_3", "repo": "A", "session": "job8", "state": "leased"},
    ])
    captured = {}

    class _FakeFinisher:
        def __init__(self, regression_cwd=None, task_workspace=None):
            captured["regression_cwd"] = regression_cwd
            captured["task_workspace"] = task_workspace

        def run(self, **kwargs):
            captured["run"] = kwargs
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _FakeFinisher)
    assert tf.main(["T-0001", "--task", "job8", "--no-pytest"]) == 0
    assert captured["regression_cwd"] == "CWD::work/A_3"   # 회귀 skip 여도 해소는 수행
    assert captured["task_workspace"] == tf.REPO / "work" / "A_3"
    assert captured["run"]["skip_pytest"] is True
    assert str(tf.REPO / "work" / "A_3") in capsys.readouterr().out


def test_ticket_finish_task_no_pytest_ambiguous_fails_before_finisher(tf, monkeypatch, capsys):
    """task 보유 worktree가 모호하면 --no-pytest라도 stage 전에 fail-loud 한다."""
    _write_leases(tf.LEASES_FILE, [
        {"slot": "work/A_1", "repo": "A", "session": "job9", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "job9", "state": "leased"},
    ])
    called = {"finisher": False}
    monkeypatch.setattr(tf, "TicketFinisher",
                        lambda **kw: called.__setitem__("finisher", True))
    assert tf.main(["T-0001", "--task", "job9", "--no-pytest"]) == 1
    assert called["finisher"] is False
    assert "작업공간 해소" in capsys.readouterr().err


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
    assert "--task 는 독립 정체성" in err
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
    """new --task <불법> → rc≠0(SystemExit) · 티켓 파일/기록 부작용 0 (깔때기 경유)."""
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
    """ticket_finish --task <불법> → rc1 (F6 해소 이전 공유 validator·기록 부작용 0)."""
    called = {"finisher": False}
    monkeypatch.setattr(tf, "TicketFinisher",
                        lambda **kw: called.__setitem__("finisher", True))
    rc = tf.main(["T-0001", "--task", "../evil"])
    assert rc == 1
    assert called["finisher"] is False          # finisher 미도달(기록 부작용 0)
    err = capsys.readouterr().err
    assert "부적합 task 명" in err
