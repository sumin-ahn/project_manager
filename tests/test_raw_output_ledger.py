"""위임·외부리뷰 raw 결정적 장부 회귀 가드."""
from __future__ import annotations

import datetime
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"raw_ledger_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def relay():
    return _load("pm_relay")


@pytest.fixture
def delegate():
    return _load("pm_delegate")


@pytest.fixture
def external():
    return _load("external_review")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".project_manager").mkdir(parents=True)
    return repo


def _ledger(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _engine_family(tmp_path: Path) -> tuple[Path, Path]:
    pm_home = tmp_path / "pm-home"
    worktree = pm_home / "work" / "project_1"
    ticket = (
        pm_home
        / ".project_manager"
        / "wiki"
        / "tickets"
        / "open"
        / ("T-" + "0001.md")
    )
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    for repo in (pm_home, worktree):
        tools = repo / ".project_manager" / "tools"
        tools.mkdir(parents=True)
        (tools / "pm_delegate.py").write_text("# engine copy\n", encoding="utf-8")
        (tools / "external_review.py").write_text(
            "# engine copy\n", encoding="utf-8"
        )
    return pm_home, worktree


def test_default_storage_is_pm_local_and_explicit_destination_is_unchanged(
        relay, tmp_path):
    repo = _repo(tmp_path)
    raw, ledger = relay.raw_storage_paths(
        repo, "delegate", temp_dir=tmp_path / "os-temp"
    )
    assert raw == repo / ".project_manager" / ".local" / "delegate"
    assert ledger == repo / ".project_manager" / ".local" / "raw_outputs.json"

    explicit = tmp_path / "injected-output"
    raw, ledger = relay.raw_storage_paths(
        repo, "review", explicit, temp_dir=tmp_path / "os-temp"
    )
    assert raw == explicit
    assert ledger == explicit / "raw_outputs.json"


def test_unresolved_pm_home_falls_back_to_injected_tempdir(relay, tmp_path):
    unresolved = tmp_path / "adopter-without-pm-home"
    tempdir = tmp_path / "injected-tempdir"
    raw, ledger = relay.raw_storage_paths(
        unresolved, "delegate", temp_dir=tempdir
    )
    assert raw == tempdir
    assert ledger == tempdir / "pm_raw_outputs.json"


def test_start_and_finish_record_preserve_audit_fields(relay, tmp_path):
    ledger_path = tmp_path / "raw_outputs.json"
    raw_path = tmp_path / "raw.txt"
    started = datetime.datetime(2026, 7, 30, 1, 2, tzinfo=datetime.timezone.utc)
    record_id = relay.start_raw_record(
        ledger_path,
        surface="delegate",
        harness="codex",
        model="gpt-x",
        role="developer",
        raw_path=raw_path,
        attempt="primary",
        now=started,
    )
    rows = _ledger(ledger_path)["records"]
    assert len(rows) == 1
    assert rows[0]["id"] == record_id
    assert rows[0]["raw_path"] == str(raw_path.resolve())
    assert "finished_at" not in rows[0]

    relay.finish_raw_record(
        ledger_path,
        record_id,
        rc=0,
        elapsed_sec=12.3456,
        silence_sec=0.4,
        now=started + datetime.timedelta(seconds=13),
    )
    rows = _ledger(ledger_path)["records"]
    assert len(rows) == 1
    assert rows[0]["rc"] == 0
    assert rows[0]["elapsed_sec"] == 12.346
    assert rows[0]["silence_sec"] == 0.4
    assert rows[0]["finished_at"]


def test_retention_is_bounded_and_keeps_unfinished_separate(relay):
    now = datetime.datetime(2026, 7, 30, tzinfo=datetime.timezone.utc)

    def row(index: int, *, finished: bool, age_days: int = 0) -> dict:
        item = {
            "id": f"row-{finished}-{index}",
            "started_at": (
                now - datetime.timedelta(days=age_days, seconds=index)
            ).isoformat(),
        }
        if finished:
            item["finished_at"] = now.isoformat()
        return item

    records = [
        *(row(i, finished=False) for i in range(relay.RAW_LEDGER_MAX_UNFINISHED + 9)),
        *(row(i, finished=True) for i in range(relay.RAW_LEDGER_MAX_COMPLETED + 11)),
        row(900, finished=False, age_days=relay.RAW_LEDGER_UNFINISHED_DAYS + 1),
        row(901, finished=True, age_days=relay.RAW_LEDGER_COMPLETED_DAYS + 1),
    ]
    kept = relay._prune_raw_records(records, now=now)
    unfinished = [item for item in kept if "finished_at" not in item]
    completed = [item for item in kept if "finished_at" in item]
    assert len(unfinished) == relay.RAW_LEDGER_MAX_UNFINISHED
    assert len(completed) == relay.RAW_LEDGER_MAX_COMPLETED
    assert all(item["id"] not in {"row-False-900", "row-True-901"} for item in kept)


def test_parallel_processes_append_without_record_loss(tmp_path):
    ledger_path = tmp_path / "raw_outputs.json"
    module_path = TOOLS / "pm_relay.py"
    script = """
import importlib.util
import pathlib
import sys
spec = importlib.util.spec_from_file_location("parallel_raw_ledger", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
index = sys.argv[3]
module.start_raw_record(
    pathlib.Path(sys.argv[2]),
    surface="delegate",
    harness="codex",
    model=f"model-{index}",
    role="developer",
    raw_path=pathlib.Path(sys.argv[2]).parent / f"raw-{index}.txt",
    attempt="primary",
)
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(module_path),
                str(ledger_path),
                str(index),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(4)
    ]
    results = [process.communicate(timeout=20) for process in processes]
    assert [process.returncode for process in processes] == [0, 0, 0, 0], results
    rows = _ledger(ledger_path)["records"]
    assert len(rows) == 4
    assert {row["model"] for row in rows} == {
        "model-0", "model-1", "model-2", "model-3"
    }


class SimulatedHarnessKill(BaseException):
    pass


def test_delegate_kill_leaves_discoverable_unfinished_record_before_runner(
        delegate, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    _raw_dir, ledger_path = delegate._raw_storage()

    def killed_runner(*_args, **_kwargs):
        rows = _ledger(ledger_path)["records"]
        assert len(rows) == 1
        assert "finished_at" not in rows[0]
        raise SimulatedHarnessKill()

    with pytest.raises(SimulatedHarnessKill):
        delegate._execute_attempt(
            harness="codex",
            model="gpt-x",
            reasoning=None,
            role="developer",
            cwd=repo,
            prompt="implement",
            timeout=30,
            output_dir=None,
            run_fn=killed_runner,
            attempt="primary",
        )

    unfinished = delegate._load_relay().unfinished_raw_records(ledger_path)
    assert len(unfinished) == 1
    raw_path = Path(unfinished[0]["raw_path"])
    assert raw_path.is_file()
    assert delegate._cmd_raw(["--unfinished"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0] == f"조회 장부: {ledger_path.resolve()}"
    assert "미마감 raw 1건" in output
    assert str(raw_path.resolve()) in output


def test_raw_empty_still_prints_resolved_ledger_on_first_line(
        delegate, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    _raw_dir, ledger_path = delegate._raw_storage()

    assert not ledger_path.exists()
    assert delegate._cmd_raw([]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"조회 장부: {ledger_path.resolve()}",
        "최근 raw 없음",
    ]


def test_raw_output_dir_reads_that_ledger(
        delegate, relay, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    output_dir = tmp_path / "explicit-output"
    ledger_path = output_dir / "raw_outputs.json"
    raw_path = output_dir / "explicit.txt"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text("raw\n", encoding="utf-8")
    relay.start_raw_record(
        ledger_path,
        surface="delegate",
        harness="codex",
        model="explicit-model",
        role="developer",
        raw_path=raw_path,
        attempt="primary",
    )

    assert delegate._cmd_raw(["--output-dir", str(output_dir)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"조회 장부: {ledger_path.resolve()}"
    assert lines[1] == "최근 raw 1건"
    assert "explicit-model" not in "\n".join(lines)
    assert str(raw_path.resolve()) in lines[2]


@pytest.mark.parametrize("query_side", ["pm-home", "worktree"])
def test_raw_warns_about_peer_engine_ledger_without_reading_it(
        delegate, monkeypatch, tmp_path, capsys, query_side):
    pm_home, worktree = _engine_family(tmp_path)
    query_repo, peer_repo = (
        (pm_home, worktree)
        if query_side == "pm-home"
        else (worktree, pm_home)
    )
    monkeypatch.setattr(delegate, "REPO", query_repo)
    current_ledger = (
        query_repo / ".project_manager" / ".local" / "raw_outputs.json"
    )
    peer_ledger = (
        peer_repo / ".project_manager" / ".local" / "raw_outputs.json"
    )
    peer_raw = peer_repo / "peer-only.txt"
    delegate._load_relay().start_raw_record(
        peer_ledger,
        surface="delegate",
        harness="codex",
        model="peer-only-model",
        role="developer",
        raw_path=peer_raw,
        attempt="primary",
    )

    assert not current_ledger.exists()
    assert delegate._cmd_raw([]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"조회 장부: {current_ledger.resolve()}"
    assert lines[1] == (
        "경고: 다른 엔진 사본 장부가 있습니다"
        f"(이 조회에서는 읽지 않음): {peer_ledger.resolve()}"
    )
    assert lines[2] == "최근 raw 없음"
    assert "peer-only-model" not in "\n".join(lines)
    assert str(peer_raw.resolve()) not in "\n".join(lines)


def test_delegate_registration_is_structurally_before_runner(delegate):
    """등재 제거와 종료시점 이동을 모두 잡는 비공허 순서 sensitivity."""
    source = inspect.getsource(delegate._execute_attempt)
    registration = source.index("record_id = relay.start_raw_record(")
    execution = source.index("result = run_fn(")
    completion = source.index("relay.finish_raw_record(")
    assert registration < execution < completion


def test_external_review_kill_leaves_discoverable_unfinished_record_before_runner(
        external, monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    monkeypatch.setattr(external, "REPO", repo)
    _raw_dir, ledger_path = external._raw_storage()

    def killed_runner(*_args, **_kwargs):
        rows = _ledger(ledger_path)["records"]
        assert len(rows) == 1
        assert rows[0]["surface"] == "external-review"
        assert "finished_at" not in rows[0]
        raise SimulatedHarnessKill()

    with pytest.raises(SimulatedHarnessKill):
        external.run_review(
            "review",
            reviewer_cmd="codex exec --model gpt-x",
            run_fn=killed_runner,
        )

    unfinished = external._load_relay().unfinished_raw_records(ledger_path)
    assert len(unfinished) == 1
    assert unfinished[0]["model"] == "gpt-x"
    assert Path(unfinished[0]["raw_path"]).is_file()


def test_completed_delegate_keeps_existing_raw_audit_header(
        delegate, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread"}),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done"},
        }),
    ])

    result = delegate._execute_attempt(
        harness="codex",
        model="gpt-x",
        reasoning=None,
        role="developer",
        cwd=repo,
        prompt="implement",
        timeout=30,
        output_dir=None,
        run_fn=lambda *_a, **_k: {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "timed_out": False,
            delegate.RUN_RESULT_SILENCE_SEC: 0.2,
        },
        attempt="primary",
    )
    content = result.raw_path.read_text(encoding="utf-8")
    assert "# rc: 0" in content
    assert "# elapsed_sec:" in content
    assert "# silence_sec: 0.2" in content
    _raw_dir, ledger_path = delegate._raw_storage()
    rows = _ledger(ledger_path)["records"]
    assert len(rows) == 1
    assert rows[0]["rc"] == 0
    assert rows[0]["silence_sec"] == 0.2
    assert delegate._cmd_raw([]) == 0
    query = capsys.readouterr().out
    assert "최근 raw 1건" in query
    assert "완료(rc=0)" in query
    assert str(result.raw_path.resolve()) in query
