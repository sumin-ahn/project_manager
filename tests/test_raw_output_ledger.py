"""위임·외부리뷰 raw 결정적 장부 회귀 가드."""
from __future__ import annotations

import datetime
import importlib.util
import inspect
import json
import os
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


def _unfinished_record(relay, ledger_path: Path, tmp_path: Path) -> str:
    """raw close 검증용 미마감 레코드를 현재 프로세스 PID로 등록한다."""
    return relay.start_raw_record(
        ledger_path,
        surface="delegate",
        harness="codex",
        model="gpt-x",
        role="developer",
        raw_path=tmp_path / "raw.txt",
        attempt="primary",
        now=datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=2),
    )


def _engine_family(tmp_path: Path) -> tuple[Path, Path]:
    pm_home = tmp_path / "pm-home"
    pm_home.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=pm_home, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=pm_home, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=pm_home, check=True,
    )
    (pm_home / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=pm_home, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=pm_home, check=True)
    worktree = pm_home / "work" / "project_1"
    worktree.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "raw-slot", str(worktree)],
        cwd=pm_home, check=True,
    )
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
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"leases": [{"slot": "work/project_1", "state": "leased"}]}),
        encoding="utf-8",
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


def test_reserved_key_table_covers_every_common_schema_field(relay, tmp_path):
    """예약표가 시작·마감이 쓰는 공통 필드를 전부 담는다 — 표가 뒤처지면 예약이 헐거워진다."""
    ledger_path = tmp_path / "raw_outputs.json"
    record_id = relay.start_raw_record(
        ledger_path, surface="delegate", harness="codex", model="gpt-x",
        role="developer", raw_path=tmp_path / "raw.txt", attempt="primary",
    )
    relay.finish_raw_record(
        ledger_path, record_id, rc=0, elapsed_sec=1.0, silence_sec=None,
    )
    common = set(_ledger(ledger_path)["records"][0])
    assert common <= relay.RAW_LEDGER_RESERVED_KEYS, (
        f"공통 스키마 키가 예약표 밖: {sorted(common - relay.RAW_LEDGER_RESERVED_KEYS)}"
    )


def test_finish_record_rejects_second_completion_without_overwrite(relay, tmp_path):
    """마감 락 안에서 재마감을 거부해 raw close 조회 후 경합도 덮어쓰지 않는다.

    재마감 신호는 전용 타입(`RawRecordAlreadyFinished`)이다 — 원 실행 마감 호출부가
    "이미 수동 마감됨" 충돌만 경고로 강등하고 시작 레코드 유실(fail-loud)과 구분한다."""
    ledger_path = tmp_path / "raw_outputs.json"
    record_id = _unfinished_record(relay, ledger_path, tmp_path)
    relay.finish_raw_record(
        ledger_path, record_id, rc=0, elapsed_sec=2, silence_sec=0,
        finish_note="첫 마감",
    )

    with pytest.raises(relay.RawRecordAlreadyFinished, match="이미 마감"):
        relay.finish_raw_record(
            ledger_path, record_id, rc=-1, elapsed_sec=3, silence_sec=None,
            finish_note="덮어쓰기",
        )

    row = _ledger(ledger_path)["records"][0]
    assert row["rc"] == 0
    assert row["finish_note"] == "첫 마감"


def test_finish_note_is_explicit_field_not_extra_injectable(relay, tmp_path):
    """`finish_note` 는 명시 완료 필드다 — 시작/마감 `extra` 로는 예약 키라 주입 불가."""
    ledger_path = tmp_path / "raw_outputs.json"
    record_id = relay.start_raw_record(
        ledger_path, surface="delegate", harness="codex", model="gpt-x",
        role="developer", raw_path=tmp_path / "raw.txt", attempt="primary",
        extra={"finish_note": "시작 주입 시도"},
    )
    assert "finish_note" not in _ledger(ledger_path)["records"][0]
    relay.finish_raw_record(
        ledger_path, record_id, rc=0, elapsed_sec=1.0, silence_sec=None,
        finish_note="정식 마감 사유", extra={"finish_note": "extra 주입 시도"},
    )
    assert _ledger(ledger_path)["records"][0]["finish_note"] == "정식 마감 사유"


def test_start_extra_cannot_seed_finish_only_schema_keys(relay, tmp_path):
    """시작 `extra` 도 **마감 전용** 공통 키를 심지 못한다 (T-0600 — 두 표면 한 규칙).

    시작을 "지금 이 행에 있는 키"로만 막으면 `finished_at`/`rc` 는 시작 시점에 아직 없어
    통과한다. 그렇게 심긴 `finished_at` 은 실행이 죽어도 레코드를 마감된 것으로 보이게 해
    미마감 sweep 이 못 본다(fail-open).
    """
    ledger_path = tmp_path / "raw_outputs.json"
    record_id = relay.start_raw_record(
        ledger_path, surface="delegate", harness="codex", model="gpt-x",
        role="developer", raw_path=tmp_path / "raw.txt", attempt="primary",
        extra={"finished_at": "2026-01-01T00:00:00+00:00", "rc": 0,
               "elapsed_sec": 0.0, "silence_sec": 0.0, "surface": "spoofed",
               "ticket": "T-" + "0600"},
    )
    row = _ledger(ledger_path)["records"][0]

    assert row["id"] == record_id and row["surface"] == "delegate"
    for finish_only in ("finished_at", "rc", "elapsed_sec", "silence_sec"):
        assert finish_only not in row, f"시작 extra 가 마감 키를 심었다: {finish_only}"
    assert row["ticket"] == "T-" + "0600"          # 예약 밖 필드는 그대로 실린다
    assert relay.unfinished_raw_records(ledger_path) == [row]   # sweep 이 본다


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


def test_raw_close_finishes_unfinished_record_with_fixed_abnormal_schema(
        delegate, relay, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    output_dir = tmp_path / "explicit-output"
    ledger_path = output_dir / "raw_outputs.json"
    record_id = _unfinished_record(relay, ledger_path, tmp_path)
    # pid 부재는 안전 게이트를 통과한다.
    ledger = _ledger(ledger_path)
    ledger["records"][0].pop("pid")
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
    )

    assert delegate.main([
        "raw", "close", record_id, "--output-dir", str(output_dir),
    ]) == 0

    row = _ledger(ledger_path)["records"][0]
    finished = datetime.datetime.fromisoformat(row["finished_at"])
    started = datetime.datetime.fromisoformat(row["started_at"])
    assert row["rc"] == -1
    assert row["elapsed_sec"] == round((finished - started).total_seconds(), 3)
    assert row["silence_sec"] is None
    assert row["finish_note"] == "수동 마감(raw close)"
    output = capsys.readouterr().out
    assert f"raw 레코드 마감: {record_id}" in output
    assert f"마감 장부: {ledger_path.resolve()}" in output


def test_raw_close_rejects_already_finished_record(
        delegate, relay, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    output_dir = tmp_path / "explicit-output"
    ledger_path = output_dir / "raw_outputs.json"
    record_id = _unfinished_record(relay, ledger_path, tmp_path)
    relay.finish_raw_record(
        ledger_path, record_id, rc=0, elapsed_sec=2, silence_sec=0,
    )

    assert delegate._cmd_raw([
        "close", record_id, "--output-dir", str(output_dir),
    ]) == 1
    assert "이미 마감" in capsys.readouterr().err
    assert _ledger(ledger_path)["records"][0]["rc"] == 0


def test_raw_close_rejects_unknown_record_id(
        delegate, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    output_dir = tmp_path / "explicit-output"

    assert delegate._cmd_raw([
        "close", "missing-record", "--output-dir", str(output_dir),
    ]) == 1
    assert "미발견: missing-record" in capsys.readouterr().err
    assert not (output_dir / "raw_outputs.json").exists()


def test_raw_close_rejects_live_pid(
        delegate, relay, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    output_dir = tmp_path / "explicit-output"
    ledger_path = output_dir / "raw_outputs.json"
    record_id = _unfinished_record(relay, ledger_path, tmp_path)
    assert _ledger(ledger_path)["records"][0]["pid"] == os.getpid()

    assert delegate._cmd_raw([
        "close", record_id, "--output-dir", str(output_dir),
    ]) == 1
    assert "PID가 실행 중" in capsys.readouterr().err
    assert "finished_at" not in _ledger(ledger_path)["records"][0]


def test_raw_close_force_bypasses_live_pid_and_preserves_note(
        delegate, relay, monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    output_dir = tmp_path / "explicit-output"
    ledger_path = output_dir / "raw_outputs.json"
    record_id = _unfinished_record(relay, ledger_path, tmp_path)

    assert delegate._cmd_raw([
        "close", record_id,
        "--note", "PM 확인 후 강제 마감",
        "--force",
        "--output-dir", str(output_dir),
    ]) == 0
    row = _ledger(ledger_path)["records"][0]
    assert row["rc"] == -1
    assert row["finish_note"] == "PM 확인 후 강제 마감"


def test_raw_close_warns_when_old_completion_is_pruned(
        delegate, relay, monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(delegate, "REPO", repo)
    output_dir = tmp_path / "explicit-output"
    ledger_path = output_dir / "raw_outputs.json"
    started = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=relay.RAW_LEDGER_COMPLETED_DAYS + 1)
    )
    record_id = relay.start_raw_record(
        ledger_path,
        surface="delegate",
        harness="codex",
        model="gpt-x",
        role="developer",
        raw_path=tmp_path / "old-raw.txt",
        attempt="primary",
        now=started,
    )

    assert delegate._cmd_raw([
        "close", record_id, "--force", "--output-dir", str(output_dir),
    ]) == 0

    lines = capsys.readouterr().out.splitlines()
    warning = [line for line in lines if line.startswith("경고:")]
    assert warning == [
        f"경고: raw 레코드 {record_id}는 완료 보존창"
        f"({relay.RAW_LEDGER_COMPLETED_DAYS}일) 밖이어서 마감과 동시에 장부에서 제거됨"
    ]
    assert relay.raw_records(ledger_path) == []


def test_raw_help_exposes_close_surface(delegate, capsys):
    with pytest.raises(SystemExit) as exc_info:
        delegate._cmd_raw(["--help"])
    assert exc_info.value.code == 0
    assert "close <RECORD-ID> ..." in capsys.readouterr().out


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
    # 마감은 실행 *뒤* 경로를 앵커로 잡는다 — pre-spawn 거부(스폰 없이 rc=1 마감)가 실행보다
    # 앞에 있는 것은 설계 동작이라 첫 출현으로 재면 정상 구조를 역전으로 오판한다.
    completion = source.index("relay.finish_raw_record(", execution)
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
    assert unfinished[0]["model"] == external.UNPINNED_MODEL_LABEL
    assert unfinished[0]["command"] == "codex exec --model gpt-x"
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


# ── 외부리뷰 raw 기록 앵커 = 소유 PM 홈 (기록·조회 단일 앵커) ────────────────────
# 기록이 diff 슬롯 장부로 갈리면 PM 홈 장부를 읽는 `pm_delegate raw` 통합 조회가 게이트 raw 를
# 영구히 못 본다(실측 2건). 조회를 넓히지 않고 기록을 소유 PM 홈으로 수렴시킨 뒤의 회귀다.


def _review_slot_family(tmp_path: Path) -> tuple[Path, Path]:
    """PM 홈 + 등록 슬롯 + 슬롯의 tracked 변경 1건(비어있지 않은 diff)."""
    pm_home, worktree = _engine_family(tmp_path)
    (worktree / "seed.txt").write_text("changed\n", encoding="utf-8")
    return pm_home, worktree


def _stub_reviewer(external, monkeypatch) -> None:
    """외부 프로세스 스폰 없이 run_review 본체(raw 박제·장부 등재)를 그대로 태운다."""
    def _fake_run_reviewer_ex(
        prompt, reviewer_cmd, timeout, run_fn, idle_timeout=None, metrics=None,
        *, cwd=None, env=None, argv=None, stdin_text=None, on_spawn_attempt=None,
    ):
        # 이 파일의 형상은 모두 legacy 대상(구조화 키 없음)이라 wire transport 주입이 없어야
        # 한다 — legacy 실행 형상이 구조화 경로 도입으로 바뀌지 않았음을 여기서도 못박는다.
        assert argv is None and stdin_text is None
        # 스폰 시도 seam 은 러너를 대신 서는 이 대역이 소유한다 — 실 경로와 같은 순서로 한 번
        # 부른다(안 부르면 raw 레코드가 스폰 전 중단으로 닫혀 이 파일의 장부 계약이 갈린다).
        if on_spawn_attempt is not None:
            on_spawn_attempt()
        if metrics is not None:
            metrics.clear()
            metrics.update({"rc": 0, "silence_sec": 0.1})
        return True, "판정: 통과\n\n## must-fix\n- 없음\n", True

    monkeypatch.setattr(external, "_run_reviewer_ex", _fake_run_reviewer_ex)
    # 이 파일의 축은 raw·라운드 장부다. 게이트가 ticket 형상이면 엔진이 리뷰 뒤 그 티켓의
    # external-reviewer 절 회수를 시도하는데, 여기 픽스처의 `T-0001.md` 는 장부 축용 빈 파일이라
    # board 의 티켓 해소를 통과하지 못한다 — 회수 축은 `test_external_review_ticket_harvest.py`
    # 가 소유하므로 여기서는 격리한다.
    monkeypatch.setattr(
        external, "_harvest_external_review_section", lambda *args, **kwargs: None,
    )


def test_external_raw_storage_anchor_is_resolved_pm_home_owner(
        external, monkeypatch, tmp_path):
    """앵커 seam: 주입된 소유 PM 홈 > 엔진 자기 앵커 REPO · 명시 output_dir 격리는 불변."""
    pm_home, worktree = _engine_family(tmp_path)
    monkeypatch.setattr(external, "REPO", worktree)

    assert external._raw_storage() == (
        worktree / ".project_manager" / ".local" / "review",
        worktree / ".project_manager" / ".local" / "raw_outputs.json",
    )

    monkeypatch.setattr(external, "_PM_HOME_OVERRIDE", pm_home)
    assert external._raw_storage() == (
        pm_home / ".project_manager" / ".local" / "review",
        pm_home / ".project_manager" / ".local" / "raw_outputs.json",
    )

    explicit = tmp_path / "explicit-output"
    assert external._raw_storage(explicit) == (
        explicit, explicit / "raw_outputs.json",
    )


@pytest.mark.parametrize("engine_side", ["slot", "pm-home"])
def test_review_run_records_raw_in_pm_home_and_unified_query_shows_it(
        external, delegate, monkeypatch, tmp_path, capsys, engine_side):
    """어느 엔진 사본으로 실행하든 raw 가 소유 PM 홈 장부에 등재되고 통합 조회에 보인다.

    두 실측 형상을 모두 태운다 — 슬롯 사본 + 상대 `--paths`, PM 홈 사본 + 절대 `--paths`.
    diff 앵커는 두 경우 모두 슬롯이므로, 옛 규칙(REPO=diff_root)이면 둘 다 슬롯 장부로 갈린다.
    """
    pm_home, worktree = _review_slot_family(tmp_path)
    engine_repo, review_paths = (
        (worktree, ["--paths", "seed.txt"])
        if engine_side == "slot"
        else (pm_home, ["--paths", str(worktree / "seed.txt")])
    )
    monkeypatch.setattr(external, "REPO", engine_repo)
    _stub_reviewer(external, monkeypatch)

    assert external.main([*review_paths, "--force", "--no-gate"]) == 0
    capsys.readouterr()

    home_ledger = pm_home / ".project_manager" / ".local" / "raw_outputs.json"
    rows = _ledger(home_ledger)["records"]
    assert len(rows) == 1
    assert rows[0]["surface"] == "external-review"
    assert rows[0]["rc"] == 0
    raw_path = Path(rows[0]["raw_path"])
    assert raw_path.parent == pm_home / ".project_manager" / ".local" / "review"
    assert raw_path.is_file()
    # 슬롯에는 raw 도 장부도 쌓이지 않는다 — 옛 raw 축적이 후속 reviewer 컨텍스트를 오염시키던
    # 축(판정 echo)까지 원천 소멸한다.
    assert not (worktree / ".project_manager" / ".local").exists()

    monkeypatch.setattr(delegate, "REPO", pm_home)
    assert delegate._cmd_raw([]) == 0
    query = capsys.readouterr().out
    assert query.splitlines()[0] == f"조회 장부: {home_ledger.resolve()}"
    assert "최근 raw 1건" in query
    assert "external-review" in query
    assert str(raw_path.resolve()) in query


def test_legacy_diff_root_raw_anchor_writes_to_the_slot_ledger(
        external, monkeypatch, tmp_path, capsys):
    """옛 규칙(기록 앵커=diff 슬롯 REPO)의 실제 동작을 핀으로 박제한다 — 슬롯 장부로 갈린다.

    이름이 "오라클 감도"가 아니라 *무슨 동작을 핀했는지*를 말하도록 둔다: 이 테스트가 green 인
    한 위 회귀(소유 PM 홈 등재)는 앵커 규칙 덕분에 통과한 것이지 우연이 아니다.
    """
    pm_home, worktree = _review_slot_family(tmp_path)
    monkeypatch.setattr(external, "REPO", worktree)
    _stub_reviewer(external, monkeypatch)
    relay = external._load_relay()

    def legacy_raw_storage(output_dir=None):
        return relay.raw_storage_paths(
            external.REPO, "review", output_dir, temp_dir=tmp_path / "os-temp",
        )

    monkeypatch.setattr(external, "_raw_storage", legacy_raw_storage)
    assert external.main(["--paths", "seed.txt", "--force", "--no-gate"]) == 0
    capsys.readouterr()

    assert not (
        pm_home / ".project_manager" / ".local" / "raw_outputs.json"
    ).exists()
    slot_rows = _ledger(
        worktree / ".project_manager" / ".local" / "raw_outputs.json"
    )["records"]
    assert len(slot_rows) == 1
    assert slot_rows[0]["surface"] == "external-review"


def test_unresolvable_pm_home_keeps_loud_diff_root_fallback(
        external, monkeypatch, tmp_path, capsys):
    """lease 손상으로 소유자를 확정 못 하면 loud 경고 + diff_root 폴백을 유지한다(자기잠김 금지)."""
    pm_home, worktree = _review_slot_family(tmp_path)
    lease = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    lease.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(external, "REPO", worktree)
    _stub_reviewer(external, monkeypatch)

    assert external.main(["--paths", "seed.txt", "--force", "--no-gate"]) == 0

    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("경고: PM 홈 해소 실패")
    assert "board가 필요 없는 실행" in err
    assert f"pm_home={worktree.resolve()}" in err
    slot_rows = _ledger(
        worktree / ".project_manager" / ".local" / "raw_outputs.json"
    )["records"]
    assert len(slot_rows) == 1
    assert slot_rows[0]["surface"] == "external-review"
    assert not (
        pm_home / ".project_manager" / ".local" / "raw_outputs.json"
    ).exists()


# ── 라운드 장부 앵커 = 소유 PM 홈 (과금 상한 연속성) ──────────────────────────
# 라운드 장부(`review_rounds.json`)는 `--gate` 별 실 전송 횟수로 과금 상한을 강제한다. 그 앵커가
# diff_root 면 게이트 스냅샷 worktree·새로 판 슬롯에서 같은 게이트를 돌릴 때 count 가 0 부터 다시
# 세어져 **상한이 조용히 리셋**된다. 라운드는 이미 `--gate` 키로 분리되므로 슬롯별 장부 분리가 주는
# 추가 격리는 없다 — raw 장부와 같은 소유 PM 홈 앵커로 모은다.


def _second_review_slot(pm_home: Path) -> Path:
    """같은 PM 홈에 등록된 **두 번째** 슬롯 — 게이트 스냅샷/새 worktree 형상의 대역."""
    worktree = pm_home / "work" / "project_2"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "raw-slot-2", str(worktree)],
        cwd=pm_home, check=True,
    )
    (worktree / "seed.txt").write_text("changed elsewhere\n", encoding="utf-8")
    tools = worktree / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "external_review.py").write_text("# engine copy\n", encoding="utf-8")
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    data = _ledger(ledger)
    data["leases"].append({"slot": "work/project_2", "state": "leased"})
    ledger.write_text(json.dumps(data), encoding="utf-8")
    return worktree


def _round_count(anchor: Path, gate: str) -> int:
    """앵커의 라운드 장부에 기록된 게이트 전송 횟수 (장부 부재 = 0)."""
    path = anchor / ".project_manager" / ".local" / "review_rounds.json"
    if not path.is_file():
        return 0
    return _ledger(path).get(gate, {}).get("count", 0)


def test_round_ledger_counts_continue_across_diff_roots(
        external, monkeypatch, tmp_path, capsys):
    """같은 게이트를 서로 다른 슬롯(=다른 diff_root)에서 돌려도 라운드 수가 이어진다 (리셋 0)."""
    pm_home, worktree = _review_slot_family(tmp_path)
    snapshot = _second_review_slot(pm_home)
    _stub_reviewer(external, monkeypatch)
    gate = "T-" + "0001"

    monkeypatch.setattr(external, "REPO", worktree)
    assert external.main(["--paths", "seed.txt", "--gate", gate, "--force"]) == 0
    capsys.readouterr()
    assert _round_count(pm_home, gate) == 1

    monkeypatch.setattr(external, "REPO", snapshot)
    assert external.main(["--paths", "seed.txt", "--gate", gate, "--force"]) == 0
    capsys.readouterr()

    assert _round_count(pm_home, gate) == 2          # 이어서 센다(상한이 살아 있다)
    assert _round_count(worktree, gate) == 0         # 슬롯 장부로 갈리지 않는다
    assert _round_count(snapshot, gate) == 0
    # 락도 같은 앵커를 따라간다 — 장부만 옮기면 두 실행이 서로 다른 파일을 잠근다.
    assert (
        pm_home / ".project_manager" / ".local" / "review_rounds.lock"
    ).is_file()


def test_legacy_diff_root_round_anchor_resets_the_count_per_slot(
        external, monkeypatch, tmp_path, capsys):
    """옛 규칙(라운드 앵커=diff_root)의 실제 동작을 핀으로 박제한다 — 슬롯마다 count 가 1 로 리셋.

    이 핀이 green 인 한 위 연속성 회귀는 앵커 이동 덕분에 통과한 것이다(감도 실증).
    """
    pm_home, worktree = _review_slot_family(tmp_path)
    snapshot = _second_review_slot(pm_home)
    _stub_reviewer(external, monkeypatch)
    gate = "T-" + "0001"

    def legacy_round_ledger_path():
        return (
            external.REPO / ".project_manager" / ".local" / "review_rounds.json"
        )

    monkeypatch.setattr(external, "_round_ledger_path", legacy_round_ledger_path)

    monkeypatch.setattr(external, "REPO", worktree)
    assert external.main(["--paths", "seed.txt", "--gate", gate, "--force"]) == 0
    monkeypatch.setattr(external, "REPO", snapshot)
    assert external.main(["--paths", "seed.txt", "--gate", gate, "--force"]) == 0
    capsys.readouterr()

    assert _round_count(worktree, gate) == 1         # 각 슬롯이 자기 장부에서 1 부터
    assert _round_count(snapshot, gate) == 1         # → 상한이 슬롯마다 리셋
    assert _round_count(pm_home, gate) == 0


# ── 앵커 이동 1회 승계(backfill) ────────────────────────────────────────────
# 앵커만 옮기면 옛 diff_root 장부의 **차단 중인 게이트**(rc 4·사용자 승인 대기)가 새 앵커에서
# 0 으로 되살아나 승인 게이트를 무통보로 연다. 예약 접근 시점에 그 게이트를 1회 승계(이관)해
# 차단 상태를 정직하게 유지한다 — 런타임 합산 폴백이 아니라 원천 마이그레이션이다.


def _write_legacy_round_ledger(anchor: Path, gate: str, entry: dict) -> Path:
    """옛 규칙(diff 앵커) 장부를 그 슬롯에 심는다 — 앵커 이동 이전 상태의 대역."""
    path = anchor / ".project_manager" / ".local" / "review_rounds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({gate: entry}), encoding="utf-8")
    return path


def test_legacy_blocking_gate_is_inherited_and_still_blocks(
        external, monkeypatch, tmp_path, capsys):
    """차단 중이던 legacy 게이트는 앵커 이동 후에도 rc 4 — 승계 고지 + PM 홈으로 이관된다."""
    pm_home, worktree = _review_slot_family(tmp_path)
    _stub_reviewer(external, monkeypatch)
    gate = "T-" + "0001"
    _write_legacy_round_ledger(worktree, gate, {"count": 4, "acked_through": 0})
    monkeypatch.setattr(external, "REPO", worktree)

    rc = external.main(["--paths", "seed.txt", "--gate", gate, "--force"])

    err = capsys.readouterr().err
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert "legacy 라운드 장부 승계" in err
    assert f"gate={gate} verdicts=4 incomplete=0" in err
    home_ledger = pm_home / ".project_manager" / ".local" / "review_rounds.json"
    assert str(home_ledger) in err                # 상한 안내가 실 장부 절대경로를 찍는다
    assert "오염 진단으로 무효화된" in err        # 미완 축에 오염이 포함됨을 고지
    assert _round_count(pm_home, gate) == 4       # 차단 상태가 새 앵커로 이관됨(재승계 불필요)

    # 2회차 — 이미 이관됐으므로 재승계 없이(고지 1회) 계속 차단한다.
    assert external.main(
        ["--paths", "seed.txt", "--gate", gate, "--force"]
    ) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert "legacy 라운드 장부 승계" not in capsys.readouterr().err


def test_inherited_gate_has_no_round_extension_path(
        external, monkeypatch, tmp_path, capsys):
    """승계된 차단 게이트에는 재개 경로가 없다 — 라운드 연장 승인은 폐지됐다(T-0593).

    승계는 차단 상태를 그대로 이관하는 마이그레이션이므로, 이관된 상한도 새 상한과 같은 규율을
    따른다: 출구는 재설계·티켓 분할이다."""
    pm_home, worktree = _review_slot_family(tmp_path)
    _stub_reviewer(external, monkeypatch)
    gate = "T-" + "0001"
    _write_legacy_round_ledger(worktree, gate, {"count": 4, "acked_through": 0})
    monkeypatch.setattr(external, "REPO", worktree)

    assert external.main(
        ["--paths", "seed.txt", "--gate", gate, "--ack-rounds", "--force"]
    ) == 1
    err = capsys.readouterr().err
    assert "폐지" in err and "재설계" in err
    assert "legacy 라운드 장부 승계" not in err   # 거부는 장부에 손대지 않는다
    assert _round_count(pm_home, gate) == 0       # 승계조차 하지 않는다(부작용 0)


def test_gate_absent_from_legacy_starts_from_zero(
        external, monkeypatch, tmp_path, capsys):
    """legacy 에 없는 게이트는 승계 없이 0 에서 시작한다 (없는 카운트를 만들어내지 않는다)."""
    pm_home, worktree = _review_slot_family(tmp_path)
    _stub_reviewer(external, monkeypatch)
    _write_legacy_round_ledger(worktree, "T-" + "0001", {"count": 4, "acked_through": 0})
    fresh_gate = "T-" + "0002"
    monkeypatch.setattr(external, "REPO", worktree)

    assert external.main(
        ["--paths", "seed.txt", "--gate", fresh_gate, "--force"]
    ) == 0

    err = capsys.readouterr().err
    assert "legacy 라운드 장부 승계" not in err
    assert _round_count(pm_home, fresh_gate) == 1


def test_legacy_round_inheritance_happens_once_per_gate(
        external, monkeypatch, tmp_path, capsys):
    """승계는 게이트당 1회 — 이후 legacy 장부가 어떻게 변하든 PM 홈 장부가 유일한 진실이다."""
    pm_home, worktree = _review_slot_family(tmp_path)
    _stub_reviewer(external, monkeypatch)
    gate = "T-" + "0001"
    _write_legacy_round_ledger(worktree, gate, {"count": 1, "acked_through": 0})
    monkeypatch.setattr(external, "REPO", worktree)

    assert external.main(["--paths", "seed.txt", "--gate", gate, "--force"]) == 0
    assert "legacy 라운드 장부 승계" in capsys.readouterr().err
    assert _round_count(pm_home, gate) == 2       # 승계 1 + 이번 예약 1

    # legacy 를 차단 수위로 바꿔도 두 번째 실행은 쳐다보지 않는다(재승계면 rc 4 로 막혔을 값).
    _write_legacy_round_ledger(worktree, gate, {"count": 99, "acked_through": 0})

    assert external.main(["--paths", "seed.txt", "--gate", gate, "--force"]) == 0
    assert "legacy 라운드 장부 승계" not in capsys.readouterr().err
    assert _round_count(pm_home, gate) == 3


def test_corrupt_lease_registered_slot_keeps_loud_round_fallback(
        external, monkeypatch, tmp_path, capsys):
    """lease 손상 관리 슬롯은 마커가 없으면 종전 diff-root 회계 폴백을 유지한다([[T-0643]]).

    T-0634의 강등 사유 기반 차단은 lease 손상 관리 슬롯까지 닫았다. 스냅샷 생성 사실은 이제
    전용 마커가 증명하므로, 마커 없는 단일 관리 후보는 강등 사유가 같아도 실 라운드와 회계 밖
    자문 모두 종전 loud 폴백으로 통과한다. 이 대비가 사유 문자열 단독 판정을 막는다."""
    pm_home, worktree = _review_slot_family(tmp_path)
    lease = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    lease.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(external, "REPO", worktree)
    _stub_reviewer(external, monkeypatch)
    gate = "T-" + "0001"

    assert external.main(["--paths", "seed.txt", "--gate", gate, "--force"]) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("경고: PM 홈 해소 실패")
    assert "게이트 스냅샷 마커" not in err
    assert _round_count(worktree, gate) == 1        # 손상 중에는 강등 앵커에 loud 기록
    assert _round_count(pm_home, gate) == 0

    # 회계 밖 자문은 종전대로 통과하며 raw 장부는 강등 앵커 폴백을 유지한다.
    assert external.main(["--paths", "seed.txt", "--force", "--no-gate"]) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("경고: PM 홈 해소 실패")


def test_main_clears_raw_anchor_between_calls(
        external, monkeypatch, tmp_path, capsys):
    """main() 의 finally 원복을 검증 — 호출 뒤 override 잔존이면 다음 호출/라이브러리
    호출이 남의 PM 홈에 박제한다. (_main 진입 초기화는 pm_delegate 대칭 방어로 별도 유지 —
    이 테스트의 오라클은 main() 원복이다.)"""
    _pm_home, worktree = _review_slot_family(tmp_path)
    monkeypatch.setattr(external, "REPO", worktree)
    _stub_reviewer(external, monkeypatch)

    assert external.main(["--paths", "seed.txt", "--force", "--no-gate"]) == 0
    capsys.readouterr()
    assert external._PM_HOME_OVERRIDE is None
