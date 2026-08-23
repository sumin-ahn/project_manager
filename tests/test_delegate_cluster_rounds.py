"""묶음 라운드 왕복 — `ticket prepare --cluster` (run-dir 1 · 라운드 파일 N) 과 run-dir 회수.

여기서 지키는 성질은 다섯이다.
  (1) 준비 단위는 묶음이다 — run-dir 하나 안에서 티켓마다 자기 자리를 갖는다.
  (2) 크기 1 묶음은 별도 코드 경로가 아니다 — `--ticket` 은 그 묶음을 가리키는 표기다.
  (3) 회수는 **티켓별 독립**이다 — 파일마다 교체·경고·거부를 따로 내고 요약 1줄로 집계한다.
  (4) 인가는 여전히 장부 행이 한다 — 디렉터리 인자는 행을 찾는 입력 형식일 뿐이다.
  (5) 묶음 키가 없는 옛 장부 행은 종전 경로로 그대로 회수된다(엔진 교체가 열린 run 을 죽이지
      않는다).

hermetic 패턴은 `test_pm_delegate_rounds.py` 와 동형 — PM 홈(board 데이터 + 엔진 사본)과
ignore 된 슬롯 git 트리 한 쌍을 tmp 에 세운다.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_DELEGATE = TOOLS / "pm_delegate.py"

# 설계 근거 게이트가 인정하는 `design: done` 의 짝 — 4항목이 값으로 채워진 설계 절.
_FILLED_DESIGN_SECTION = (
    "## 설계\n"
    "- **경계 실측**: 묶음 라운드 왕복 픽스처\n"
    "- **불변식**: run-dir 하나에 티켓 자리 N\n"
    "- **표면 상한**: 라운드 파일 티켓당 1개\n"
    "- **테스트 전략**: 정상 왕복·부분 시드·위조 경로\n"
)


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_cluster", PM_DELEGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load_pd()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def _spec_text(ticket: str, *, design: str = "done", status: str = "claimed") -> str:
    section = f"\n{_FILLED_DESIGN_SECTION}" if design == "done" else ""
    cluster_line = ""
    return (
        "---\n"
        f"id: {ticket}\n"
        "title: 묶음 라운드\n"
        f"status: {status}\n"
        "created: '2026-08-18'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        "claimed_at: '2026-08-18T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: medium\n"
        f"design: {design}\n"
        f"{cluster_line}"
        "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\n묶음 라운드 왕복.\n" + section
    )


def _fixture_board(pd, pm_home: Path, sync_log: list | None = None):
    board = pd._load_module_from_path(
        pm_home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = pm_home
    board.LOCAL_DIR = pm_home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"

    def _sync(message, paths):
        if sync_log is not None:
            sync_log.append((message, [Path(item) for item in paths]))
        return True

    board._rounds_mutation_sync_paths = _sync
    return board


@pytest.fixture
def cluster_env(tmp_path, pd, monkeypatch):
    """PM 홈(board 데이터+엔진 사본)과 ignore 된 슬롯 git 트리 한 쌍."""
    pm_home = tmp_path / "pm-home"
    slot = tmp_path / "slot"
    pm_tools = pm_home / ".project_manager" / "tools"
    pm_tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, pm_tools / source.name)
    tickets = pm_home / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (pm_home / ".project_manager" / ".local").mkdir(parents=True)
    slot.mkdir()
    assert _git(slot, "init", "-q").returncode == 0
    slot_ignore = slot / ".project_manager" / ".gitignore"
    slot_ignore.parent.mkdir()
    slot_ignore.write_text(".local/\n", encoding="utf-8", newline="\n")
    (slot / "tracked.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    assert _git(slot, "add", "tracked.txt", ".project_manager/.gitignore").returncode == 0
    monkeypatch.setenv("GIT_AUTHOR_NAME", "cluster")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "cluster@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "cluster")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "cluster@test.invalid")
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    monkeypatch.setattr(
        pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, pm_home),
    )
    return pm_home, slot, tickets


def _write_spec(tickets: Path, ticket: str, **kwargs) -> Path:
    path = tickets / f"{ticket}-cluster.md"
    path.write_text(_spec_text(ticket, **kwargs), encoding="utf-8", newline="\n")
    return path


def _write_cluster(pm_home: Path, cluster: str, tickets: list[str]) -> Path:
    """묶음 장부 1건 — board 가 읽는 자리에 frontmatter 만 쓴다."""
    directory = pm_home / ".project_manager" / "wiki" / "tickets" / "clusters"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cluster}.md"
    path.write_text(
        "---\n"
        f"id: {cluster}\n"
        "tickets:\n" + "".join(f"- {item}\n" for item in tickets)
        + "base_branch: task/main\n"
        f"branch: task/{cluster[2:]}\n"
        "spike: null\n"
        "budget:\n  architect: 1\n"
        "replans: []\n"
        "status: open\n"
        "---\n",
        encoding="utf-8", newline="\n")
    return path


def _seed_cluster(pm_home: Path, tickets_dir: Path, cluster: str,
                  members: list[str], **kwargs) -> Path:
    for ticket in members:
        path = _write_spec(tickets_dir, ticket, **kwargs)
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("tags: []\n", f"cluster: {cluster}\ntags: []\n", 1),
            encoding="utf-8", newline="\n")
    return _write_cluster(pm_home, cluster, members)


def _rounds_dir(pm_home: Path, ticket: str) -> Path:
    return pm_home / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket


def _ledger_rows(pm_home: Path) -> list[dict]:
    path = pm_home / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _fill(path: Path, marker: str) -> None:
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n{marker}\n",
        encoding="utf-8", newline="")


# ════════════════════════════════════════════════════════════════════════
# 준비 — run-dir 1 · 라운드 파일 N
# ════════════════════════════════════════════════════════════════════════

def test_cluster_prepare_lays_one_run_dir_with_a_seat_per_ticket(pd, cluster_env):
    pm_home, slot, tickets = cluster_env
    _seed_cluster(pm_home, tickets, "C-wave", ["T-6001", "T-6002"])

    plan = pd.prepare_cluster_copy(
        cluster="C-wave", role="architect", cwd=slot, pm_home=pm_home,
    )

    assert plan.cluster == "C-wave" and plan.role == "architect"
    assert len(plan.rounds) == 2
    # run-dir 은 하나이고 티켓 자리가 그 안에 산다(쓰기 허용 범위 = run-dir 전체).
    assert {round_plan.run_dir.parent for round_plan in plan.rounds} == {plan.run_dir}
    assert sorted(item.name for item in plan.run_dir.iterdir()) == ["T-6001", "T-6002"]
    for round_plan in plan.rounds:
        names = sorted(item.name for item in round_plan.run_dir.iterdir())
        assert names == ["01-architect.md", "rounds", "spec.md"]
        assert round_plan.path.name == "01-architect.md"
        assert round_plan.board_path == (
            _rounds_dir(pm_home, round_plan.ticket) / "01-architect.md")
        assert round_plan.path.read_bytes() == round_plan.board_path.read_bytes()
        assert (round_plan.run_dir / "spec.md").read_text(encoding="utf-8") == (
            (tickets / f"{round_plan.ticket}-cluster.md").read_text(encoding="utf-8"))
    # 장부는 티켓당 한 행이고 같은 run 을 가리킨다(묶음 키 포함).
    rows = _ledger_rows(pm_home)
    assert len(rows) == 2
    assert {row["run_id"] for row in rows} == {plan.run_id}
    assert {row["cluster"] for row in rows} == {"C-wave"}
    assert {row["ticket"] for row in rows} == {"T-6001", "T-6002"}


def test_cluster_prepare_reserves_nothing_when_one_member_fails_the_judgment(
        pd, cluster_env):
    """부분 예약 금지 — 멤버 하나가 상태 판정에서 막히면 board 도 슬롯도 무변경이다."""
    pm_home, slot, tickets = cluster_env
    _seed_cluster(pm_home, tickets, "C-part", ["T-6003"])
    _write_spec(tickets, "T-6004", status="claimed")
    blocked = pm_home / ".project_manager" / "wiki" / "tickets" / "blocked"
    blocked.mkdir(parents=True)
    (tickets / "T-6004-cluster.md").rename(blocked / "T-6004-cluster.md")
    _write_cluster(pm_home, "C-part", ["T-6003", "T-6004"])

    with pytest.raises(pd.DelegateError, match="open/claimed 또는 draft×architect만 허용"):
        pd.prepare_cluster_copy(
            cluster="C-part", role="architect", cwd=slot, pm_home=pm_home,
        )

    assert not _rounds_dir(pm_home, "T-6003").exists()
    assert _ledger_rows(pm_home) == []
    assert not (slot / pd.TICKET_COPY_REL_ROOT / "C-part").exists()


def test_single_ticket_prepare_uses_the_same_cluster_keyed_path(pd, cluster_env):
    """크기 1 = 별도 코드 경로 0 — `--ticket` 도 묶음 키 run-dir 에 앉고 장부가 그 키를 싣는다.

    필드도 장부도 없는 구세대 티켓은 `C-<티켓 ID>` 크기 1 묶음으로 읽힌다(마이그레이션 0).
    """
    pm_home, slot, tickets = cluster_env
    _write_spec(tickets, "T-6005")

    plan = pd.prepare_ticket_copy(
        ticket="T-6005", role="architect", cwd=slot, pm_home=pm_home,
    )

    assert plan.cluster == "C-T-6005"
    assert plan.run_dir == (
        slot / pd.TICKET_COPY_REL_ROOT / "C-T-6005" / plan.run_id / "T-6005")
    # board 라운드 자리는 종전과 같다(라운드 파일 위치 불변).
    assert plan.board_path == _rounds_dir(pm_home, "T-6005") / "01-architect.md"
    row = _ledger_rows(pm_home)[-1]
    assert row["cluster"] == "C-T-6005" and row["ticket"] == "T-6005"

    _fill(plan.path, "## 경계 실측\n- 실측")
    result = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home)

    assert result.changed is True
    # 자리를 닫으면 빈 run-dir 도 함께 걷힌다 — 크기 1 이면 곧 run 닫힘이다.
    assert not plan.run_dir.exists()
    assert not plan.run_dir.parent.exists()


# ════════════════════════════════════════════════════════════════════════
# 회수 — 티켓별 독립(교차 원자성 없음)
# ════════════════════════════════════════════════════════════════════════

def test_run_dir_harvest_replaces_every_filled_round_and_closes_the_run(
        pd, cluster_env):
    pm_home, slot, tickets = cluster_env
    _seed_cluster(pm_home, tickets, "C-full", ["T-6010", "T-6011"])
    plan = pd.prepare_cluster_copy(
        cluster="C-full", role="architect", cwd=slot, pm_home=pm_home,
    )
    for round_plan in plan.rounds:
        _fill(round_plan.path, f"## 경계 실측\n- {round_plan.ticket} 실측")

    outcomes = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )

    assert [item.ticket for item in outcomes] == ["T-6010", "T-6011"]
    assert all(item.refusal is None and item.result.changed for item in outcomes)
    for round_plan in plan.rounds:
        board_text = round_plan.board_path.read_text(encoding="utf-8")
        assert f"{round_plan.ticket} 실측" in board_text
    assert not plan.run_dir.exists()        # 마지막 자리가 닫히면 run 도 닫힌다.
    assert all(row["harvested_at"] is not None
               for row in _ledger_rows(pm_home) if row["harvested_at"] is not None)
    assert len([row for row in _ledger_rows(pm_home)
                if row["harvested_at"] is not None]) == 2


def test_run_dir_harvest_warns_per_ticket_when_one_seat_is_still_the_seed(
        pd, cluster_env, capsys):
    """부분 시드 — 그 티켓만 경고·board 무변경이고 나머지는 교체된다(교차 원자성 없음)."""
    pm_home, slot, tickets = cluster_env
    _seed_cluster(pm_home, tickets, "C-partial", ["T-6012", "T-6013"])
    plan = pd.prepare_cluster_copy(
        cluster="C-partial", role="architect", cwd=slot, pm_home=pm_home,
    )
    filled, seeded = plan.rounds[0], plan.rounds[1]
    seed_bytes = seeded.board_path.read_bytes()
    _fill(filled.path, "## 경계 실측\n- 채운 산출")
    capsys.readouterr()

    outcomes = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )

    by_ticket = {item.ticket: item for item in outcomes}
    assert by_ticket[filled.ticket].result.changed is True
    assert by_ticket[seeded.ticket].result.changed is False
    assert by_ticket[seeded.ticket].refusal is None      # 거부가 아니라 경고다.
    assert seeded.board_path.read_bytes() == seed_bytes  # board 무변경.
    err = capsys.readouterr().err
    assert "산출 없음" in err and seeded.ticket in err
    # 채운 자리는 닫히고 시드 자리는 남는다 — 같은 세션을 이어 시킬 수 있다.
    assert not filled.run_dir.exists()
    assert seeded.run_dir.is_dir()
    assert plan.run_dir.is_dir()


def test_run_dir_harvest_refuses_a_seat_the_ledger_does_not_authorize(
        pd, cluster_env):
    """디렉터리 인자는 입력 형식일 뿐 — 인가는 장부 행이 한다(위조 자리는 회수되지 않는다)."""
    pm_home, slot, tickets = cluster_env
    _seed_cluster(pm_home, tickets, "C-forge", ["T-6014"])
    plan = pd.prepare_cluster_copy(
        cluster="C-forge", role="architect", cwd=slot, pm_home=pm_home,
    )
    smuggled_dir = plan.run_dir / "T-9999"
    smuggled_dir.mkdir()
    smuggled = smuggled_dir / "01-architect.md"
    smuggled.write_text("## 설계\n밀반입\n", encoding="utf-8", newline="\n")
    _fill(plan.rounds[0].path, "## 경계 실측\n- 실측")

    outcomes = pd.harvest_cluster_copy(
        run_dir=plan.run_dir, cwd=slot, pm_home=pm_home,
    )

    assert [item.ticket for item in outcomes] == ["T-6014"]   # 장부 행만 회수 대상이다.
    assert not _rounds_dir(pm_home, "T-9999").exists()
    with pytest.raises(pd.DelegateError, match="준비 기록 없음"):
        pd.harvest_ticket_copy(copy_path=smuggled, cwd=slot, pm_home=pm_home)


def test_run_dir_harvest_refuses_when_no_unharvested_row_matches(pd, cluster_env):
    pm_home, slot, tickets = cluster_env
    _seed_cluster(pm_home, tickets, "C-empty", ["T-6015"])
    plan = pd.prepare_cluster_copy(
        cluster="C-empty", role="architect", cwd=slot, pm_home=pm_home,
    )
    _fill(plan.rounds[0].path, "## 경계 실측\n- 실측")
    pd.harvest_cluster_copy(run_dir=plan.run_dir, cwd=slot, pm_home=pm_home)

    with pytest.raises(pd.DelegateError, match="미회수 준비가 없습니다"):
        pd.harvest_cluster_copy(run_dir=plan.run_dir, cwd=slot, pm_home=pm_home)


def test_a_ledger_row_without_the_cluster_key_keeps_the_legacy_path(pd, cluster_env):
    """마이그레이션 불변식 — 묶음 키가 없는 옛 행은 종전 경로로 그대로 회수된다."""
    pm_home, slot, tickets = cluster_env
    _write_spec(tickets, "T-6016")
    board = _fixture_board(pd, pm_home)
    rounds_module = pd._load_ticket_rounds()
    seed = rounds_module.render_round_seed(
        "architect", (tickets / "T-6016-cluster.md").read_text(encoding="utf-8"),
        today="2026-01-01")
    board_path = rounds_module.reserve_round(
        board.tickets_dir(), "T-6016", "architect", content=seed,
        lock=board.board_lock(),
    )
    legacy_dir = slot / pd.TICKET_COPY_REL_ROOT / "T-6016" / ("b" * 32)
    legacy_dir.mkdir(parents=True)
    copy_path = legacy_dir / "01-architect.md"
    copy_path.write_text(seed + "\n## 경계 실측\n- 옛 세대 산출\n",
                         encoding="utf-8", newline="")
    pd._append_delegate_rounds_ledger(pm_home, {
        "ticket": "T-6016", "role": "architect", "ordinal": 1, "run_id": "b" * 32,
        "copy": str(copy_path), "board_rel": str(
            board_path.relative_to(pm_home)).replace("\\", "/"),
        "prepared_at": "2026-01-01T00:00:00+00:00", "harvested_at": None,
    })

    result = pd.harvest_ticket_copy(copy_path=copy_path, cwd=slot, pm_home=pm_home)

    assert result.changed is True
    assert "옛 세대 산출" in board_path.read_text(encoding="utf-8")
    assert not legacy_dir.exists()


# ════════════════════════════════════════════════════════════════════════
# architect 선행 게이트 — 묶음 산출이 곧 티켓 파일이다(판정식 불변)
# ════════════════════════════════════════════════════════════════════════

def test_cluster_architect_round_satisfies_the_per_ticket_design_gate(pd, cluster_env):
    pm_home, slot, tickets = cluster_env
    _seed_cluster(pm_home, tickets, "C-gate", ["T-6020", "T-6021"], design="n/a")

    # 설계 단계를 건너뛴 요청 — 순서 축이 먼저 말한다(예약 자체가 없으므로 근거를 볼 것도 없다).
    for ticket in ("T-6020", "T-6021"):
        with pytest.raises(pd.ClusterRoundBudgetExceeded, match="다음 라운드는 architect"):
            pd.prepare_ticket_copy(
                ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
            )

    plan = pd.prepare_cluster_copy(
        cluster="C-gate", role="architect", cwd=slot, pm_home=pm_home,
    )

    # 예약만 되고 회수 전인 시드 라운드는 근거가 아니다 — 이번엔 설계 근거 축이 거부한다.
    with pytest.raises(pd.DelegateError, match="developer 라운드 준비 거부"):
        pd.prepare_ticket_copy(
            ticket="T-6020", role="developer", cwd=slot, pm_home=pm_home,
        )

    for round_plan in plan.rounds:
        _fill(round_plan.path, f"## 경계 실측\n- {round_plan.ticket} 실측")
    pd.harvest_cluster_copy(run_dir=plan.run_dir, cwd=slot, pm_home=pm_home)

    for ticket in ("T-6020", "T-6021"):
        developer = pd.prepare_ticket_copy(
            ticket=ticket, role="developer", cwd=slot, pm_home=pm_home,
        )
        assert developer.board_path.name == "02-developer.md"


# ════════════════════════════════════════════════════════════════════════
# CLI — 기계 줄과 요약
# ════════════════════════════════════════════════════════════════════════

def _machine_lines(out: str) -> list[dict]:
    rows: list[dict] = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            rows.append(json.loads(stripped))
    return rows


def test_cli_prepare_and_harvest_round_trip_over_a_run_dir(
        pd, cluster_env, monkeypatch, capsys):
    pm_home, slot, tickets = cluster_env
    _seed_cluster(pm_home, tickets, "C-cli", ["T-6030", "T-6031"])
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _repo: pm_home)
    monkeypatch.setattr(pd, "_repo_root_for_cwd", lambda cwd: Path(cwd))
    monkeypatch.setattr(pd, "local_config", lambda *_a, **_k: {})
    monkeypatch.setattr(pd, "_reject_cross_role_prepare", lambda *_a, **_k: None)

    rc = pd.main(["ticket", "prepare", "--cluster", "C-cli", "--role", "architect",
                  "--cwd", str(slot)])

    assert rc == 0
    out = capsys.readouterr().out
    rows = _machine_lines(out)
    assert len(rows) == 2
    assert {row["ticket"] for row in rows} == {"T-6030", "T-6031"}
    assert {row["cluster"] for row in rows} == {"C-cli"}
    assert len({row["run_dir"] for row in rows}) == 1
    assert "라운드 2건" in out
    run_dir = Path(rows[0]["run_dir"])
    for row in rows:
        _fill(Path(row["copy"]), f"## 경계 실측\n- {row['ticket']} 실측")

    rc = pd.main(["ticket", "harvest", "--copy", str(run_dir), "--cwd", str(slot)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "교체 2 · 산출 없음 0 · 거부 0" in out
    assert len(_machine_lines(out)) == 2
    assert not run_dir.exists()
