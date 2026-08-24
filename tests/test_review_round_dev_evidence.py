"""리뷰 라운드 준비면의 dev 증거 판정 — 시드 그대로인 직전 developer 라운드를 값으로 표면화.

리뷰어의 읽기 전용 입력(`rounds/`)은 board 라운드 사본에서 온다. 직전 developer 라운드가
회수되지 않았으면 그 사본은 시드 골격이라, 리뷰어는 dev 의 결함 클래스 전수·검증 근거·빈틈
보고를 하나도 못 본 채 검토한다. 판정은 **준비 시점**에 둔다 — 리뷰가 실행되기 전이라야 PM 이
회수하고 다시 걸 수 있다.

여기서 고정하는 성질은 다섯이다.
  (1) 시드 그대로인 직전 developer 라운드 위에서 리뷰 라운드를 준비하면 loud 하다(rc=0).
  (2) 회수된 developer 라운드 위에서는 아무 표시가 없다(오탐 0).
  (3) developer 라운드가 **아예 없는** 티켓(코드만 보는 독립 검토)은 대상이 아니다.
  (4) 리뷰 라운드를 준비하는 진입점 전부가 같은 규칙을 쓴다(시드 seam 하나를 지난다).
  (5) 시드 판정은 회수면(`harvest_ticket_copy`)이 쓰는 것과 같은 기준이다.

픽스처는 실 board 트리·실 라운드 파일·실 `delegate-rounds.jsonl` 이다(조립 dict 없음).
재현 형상은 board 에 실재하던 두 티켓이다 — `01-architect`(회수) → `02-developer`(시드) →
`03-code-reviewer`(준비), 그리고 `01-developer`(시드) → `02-code-reviewer`(준비).

[[T-0812]] 가 스폰면(`external_review.py` 의 `prepare_ticket_body`)에 같은 축을 배선한다 —
판정 함수는 이 파일이 소유하는 `pd.unharvested_developer_round` 그대로이고, 축이 둘로 갈리지
않게 두 표면 형상표(A0/A1/B/C/D/E)를 이 파일 하나에서 고정한다(§스폰면).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import write_cluster_ledger

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_DELEGATE = TOOLS / "pm_delegate.py"

# 경고 문구의 고정 머리 — 단언이 문장 전체를 베끼지 않도록 이 한 조각만 본다.
WARNING_MARK = "산출 없는 developer 라운드 위에서 준비합니다"
# [[T-0819]] 값 진술 — 준비면 경고가 "실리지 않습니다" 라고 단정하지 않고, 이번 리뷰어 입력에
# 실제로 실리는 developer 산출 라운드 이름(없으면 `없음`)을 말하는 자리의 고정 머리.
LANDED_EVIDENCE_MARK = "리뷰어 입력(`rounds/`)에 실리는 developer 산출 라운드:"


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_dev_evidence", PM_DELEGATE)
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


def _spec_text(ticket: str) -> str:
    return (
        "---\n"
        f"id: {ticket}\n"
        "title: 리뷰 라운드 dev 증거\n"
        "status: claimed\n"
        "created: '2026-08-18'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        "claimed_at: '2026-08-18T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: medium\n"
        "design: done\n"
        "tags: []\n"
        "---\n"
        f"# {ticket}\n\n## 목표\n리뷰 라운드 준비면 판정.\n\n"
        "## 설계\n"
        "- **경계 실측**: 기계 테스트 픽스처\n"
        "- **불변식**: 이 파일의 축 밖\n"
        "- **표면 상한**: 픽스처 1건\n"
        "- **테스트 전략**: 정상·실패 경로\n"
    )


def _fixture_board(pd, pm_home: Path, sync_log: list):
    board = pd._load_module_from_path(
        pm_home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = pm_home
    board.LOCAL_DIR = pm_home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"
    board.BOARD_FILE = pm_home / ".project_manager" / "wiki" / "board.md"

    def _sync(message, paths):
        sync_log.append((message, [Path(item) for item in paths]))
        return True

    board._rounds_mutation_sync_paths = _sync
    board.refresh_board = lambda *args, **kwargs: None
    return board


@pytest.fixture
def rounds_env(tmp_path, pd, monkeypatch):
    """PM 홈(board 데이터 + 엔진 사본)과 ignore 된 슬롯 git 트리 한 쌍."""
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
    monkeypatch.setenv("GIT_AUTHOR_NAME", "rounds")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "rounds@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "rounds")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "rounds@test.invalid")
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    sync_log: list = []
    board = _fixture_board(pd, pm_home, sync_log)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    return pm_home, slot, tickets, board


_FIXED_ROUNDS = ("architect", "developer", "code-reviewer", "developer")


def _architect_contract() -> str:
    return json.dumps({
        "version": 1,
        "tests": [{
            "id": "AT-001",
            "target": "tests/test_review_round_dev_evidence.py",
            "command": "python3 -c 'print(\"passed\")'",
            "expected": "passed",
            "negative": "developer 증거가 없으면 경고한다",
        }],
    }, ensure_ascii=False, separators=(",", ":"))


def _fill_architect_contract(text: str) -> str:
    opening = "```pm-architect-tests-v1\n"
    if opening not in text:
        return text + f"\n{opening}{_architect_contract()}\n```\n"
    start = text.index(opening) + len(opening)
    end = text.index("\n```", start)
    return text[:start] + _architect_contract() + text[end:]


def _write_spec(tickets: Path, ticket: str, *, rounds=("developer",)) -> Path:
    """고정 4단계 장부와, 옛 dev-first 형상의 선행 architect 산출을 함께 쓴다."""
    path = tickets / f"{ticket}-review-round.md"
    path.write_text(_spec_text(ticket), encoding="utf-8", newline="\n")
    write_cluster_ledger(
        tickets.parent.parent, ticket, base_branch="task/main", rounds=_FIXED_ROUNDS,
    )
    if rounds and rounds[0] == "developer":
        rounds_dir = tickets.parent / "rounds" / ticket
        rounds_dir.mkdir(parents=True, exist_ok=True)
        (rounds_dir / "01-architect.md").write_text(
            "## 설계 (architect · 2026-08-24)\n\n"
            "## 실 산출\n- developer 증거 판정용 선행 설계\n\n"
            f"```pm-architect-tests-v1\n{_architect_contract()}\n```\n",
            encoding="utf-8", newline="\n",
        )
    return path


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


def _prepare(pd, pm_home: Path, slot: Path, ticket: str, role: str):
    return pd.prepare_ticket_copy(
        ticket=ticket, role=role, cwd=slot, pm_home=pm_home,
    )


def _land(pd, pm_home: Path, slot: Path, plan) -> None:
    """이 파일의 판정축만 위해 board 라운드를 실 산출 상태로 전환한다."""
    text = plan.path.read_text(encoding="utf-8")
    if plan.role == "architect":
        text = _fill_architect_contract(text)
    text += "\n## 실 산출\n- 값\n"
    plan.path.write_text(text, encoding="utf-8", newline="")
    plan.board_path.write_text(text, encoding="utf-8", newline="")


def _loaded_rounds(pd, pm_home: Path, ticket: str):
    rounds_module = pd._load_ticket_rounds()
    tickets_root = pm_home / ".project_manager" / "wiki" / "tickets"
    return rounds_module.load_rounds(tickets_root, ticket)


# ── (1) 시드 그대로인 직전 developer 라운드 위 ─────────────────────────────

def test_review_prepare_over_a_seed_developer_round_is_loud(pd, rounds_env, capsys):
    """`01-architect`(회수) → `02-developer`(시드) → `03-code-reviewer`(준비) 재현 형상."""
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7801", rounds=("architect", "developer", "code-reviewer"))
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7801", "architect"))
    developer = _prepare(pd, pm_home, slot, "T-7801", "developer")
    capsys.readouterr()

    review = _prepare(pd, pm_home, slot, "T-7801", "code-reviewer")

    error = capsys.readouterr().err
    assert WARNING_MARK in error
    assert "02-developer.md" in error, "경고가 어느 라운드인지 이름으로 말해야 한다"
    # 경고다 — 준비는 끝났고 rc 에 해당하는 예외도 없다.
    assert review.ordinal == 3 and review.path.exists()
    # 재현 형상의 값 단언: 그 라운드는 실제로 시드 그대로이고 장부에 미회수로 남아 있다.
    assert developer.board_path.read_bytes() == developer.path.read_bytes()
    developer_row = [
        row for row in _ledger_rows(pm_home)
        if row["role"] == "developer" and row["ordinal"] == 2
    ][-1]
    assert developer_row["harvested_at"] is None


def test_review_prepare_over_a_seed_first_developer_round_is_loud(pd, rounds_env, capsys):
    """고정 architect 산출 → developer 시드 → code-reviewer 준비 형상도 loud 하다.

    형상 A1(dev 시드뿐 · 앞선 산출 라운드 없음) — 실리는 developer 산출 라운드는 `없음`이다
    ([[T-0819]] DoD 값 단언)."""
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7802", rounds=("developer", "code-reviewer"))
    _prepare(pd, pm_home, slot, "T-7802", "developer")
    capsys.readouterr()

    _prepare(pd, pm_home, slot, "T-7802", "code-reviewer")

    error = capsys.readouterr().err
    assert WARNING_MARK in error and "02-developer.md" in error
    assert f"{LANDED_EVIDENCE_MARK} 없음" in error


# ── (2)(3) 역방향 — 정상 경로가 새로 막히거나 시끄러워지지 않는다 ──────────

def test_review_prepare_over_a_harvested_developer_round_is_silent(pd, rounds_env, capsys):
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7803", rounds=("developer", "code-reviewer"))
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7803", "developer"))
    capsys.readouterr()

    review = _prepare(pd, pm_home, slot, "T-7803", "code-reviewer")

    assert capsys.readouterr().err == ""
    assert review.ordinal == 3 and review.path.exists()


def test_review_prepare_without_any_developer_round_is_silent(pd, rounds_env, capsys):
    """고정 수열은 developer 없는 reviewer 단독 준비를 조용히 허용하지 않는다."""
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7804", rounds=("code-reviewer",))
    capsys.readouterr()

    with pytest.raises(pd.DelegateError, match="다음 라운드는 architect"):
        _prepare(pd, pm_home, slot, "T-7804", "code-reviewer")

    assert WARNING_MARK not in capsys.readouterr().err


def test_developer_round_preparation_is_never_judged(pd, rounds_env, capsys):
    """developer 중복 준비는 경고 축 이전에 고정 수열이 거부한다."""
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7805", rounds=("developer", "developer"))
    _prepare(pd, pm_home, slot, "T-7805", "developer")
    capsys.readouterr()

    with pytest.raises(pd.DelegateError, match="다음 라운드는 code-reviewer"):
        _prepare(pd, pm_home, slot, "T-7805", "developer")

    assert WARNING_MARK not in capsys.readouterr().err


def test_only_the_latest_developer_round_is_the_judgment_input(pd, rounds_env, capsys):
    """시야는 마지막 developer 라운드 하나 — 더 앞 라운드의 미회수는 대상이 아니다.

    두 번째 경고가 재현하는 것이 형상 B(앞 라운드에 dev 산출 있음 · 최신 developer 라운드는
    시드)다 — 실리는 developer 산출 라운드 이름이 그 앞선 산출(`01-developer.md`)임을 값으로
    말해야 한다. "실리지 않습니다" 처럼 조건에 따라 거짓이 되는 단정문을 쓰면 안 된다
    ([[T-0819]] DoD 값 단언)."""
    pm_home, slot, tickets, _board = rounds_env
    # 형상 A — 마지막 developer 라운드가 산출이다(앞 라운드의 미회수는 시야 밖).
    quiet_rounds = ("developer", "code-reviewer", "developer")
    _write_spec(tickets, "T-7806", rounds=quiet_rounds)
    _prepare(pd, pm_home, slot, "T-7806", "developer")            # 02 시드로 남긴다
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7806", "code-reviewer"))
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7806", "developer"))  # 04 산출
    capsys.readouterr()

    pd._warn_unharvested_developer_round(_loaded_rounds(pd, pm_home, "T-7806"))

    assert capsys.readouterr().err == "", "마지막 dev 라운드가 산출이면 조용해야 한다"

    # 형상 B — 마지막 developer 라운드는 시드이고 그 앞 라운드에 산출이 있다. 예산 수열이
    # 리뷰 뒤 리뷰를 열지 않으므로(출구는 재설계) 같은 형상을 다른 티켓에서 재현한다.
    _write_spec(tickets, "T-7817", rounds=quiet_rounds)
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7817", "developer"))  # 02 산출
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7817", "code-reviewer"))
    _prepare(pd, pm_home, slot, "T-7817", "developer")            # 04 시드로 남긴다
    capsys.readouterr()

    pd._warn_unharvested_developer_round(_loaded_rounds(pd, pm_home, "T-7817"))

    error = capsys.readouterr().err
    assert WARNING_MARK in error and "04-developer.md" in error
    assert f"{LANDED_EVIDENCE_MARK} 02-developer.md" in error
    assert "실리지 않습니다" not in error, "형상 B 에서 거짓 문장을 내면 안 된다"


# ── (4) 진입점 파리티 ──────────────────────────────────────────────────────

def test_ticket_prepare_cli_is_rc_zero_and_names_the_round(pd, rounds_env, capsys, monkeypatch):
    """진입점 1 — `ticket prepare --role code-reviewer`(rc + stderr 라운드 이름)."""
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7807", rounds=("developer", "code-reviewer"))
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: pm_home)
    assert pd._cmd_ticket([
        "prepare", "--ticket", "T-7807", "--role", "developer", "--cwd", str(slot),
    ]) == 0
    capsys.readouterr()

    rc = pd._cmd_ticket([
        "prepare", "--ticket", "T-7807", "--role", "code-reviewer", "--cwd", str(slot),
    ])

    captured = capsys.readouterr()
    assert rc == 0, "경고다 — 거부가 아니다"
    assert WARNING_MARK in captured.err and "02-developer.md" in captured.err
    assert json.loads(captured.out.splitlines()[-1])["ordinal"] == 3


def test_cross_auto_prepare_shares_the_prepare_seam(pd, rounds_env, capsys, monkeypatch, tmp_path):
    """진입점 2 — cross 위임 실행(`pd.main`)의 자동 준비가 실제로 시드 dev 라운드를 경고·
    review 라운드로 예약·장부화한다.

    문자열 개수 단언(소스에서 `prepare_ticket_copy(` 를 지운 흉내)이 아니라 `_main` 을 로컬 가짜
    하네스 경계(`run_fn` DI)까지 실제로 실행해 rc·stderr·라운드 파일·장부 행을 값으로 본다. 외부
    프로세스는 스폰하지 않는다 — `run_fn` 이 그 경계다.
    """
    pm_home, slot, tickets, _board = rounds_env
    ticket = "T-7816"
    _write_spec(tickets, ticket, rounds=("developer", "code-reviewer"))
    _prepare(pd, pm_home, slot, ticket, "developer")  # 02-developer.md 를 시드로 남긴다
    capsys.readouterr()

    # config/board 소유 해소 — 실 board 트리는 pm_home 이지 slot(cwd) 이 아니다(rounds_env 형상과
    # 같다). 하네스 스폰 앞의 게이트만 우회하고, 준비·예약·장부는 전부 실 코드가 만든다.
    external = pd._load_external_review()
    monkeypatch.setattr(external, "repo_root_from_cwd", lambda _cwd: slot)
    monkeypatch.setattr(external, "resolve_pm_home_for_repo", lambda *_a, **_kw: pm_home)
    monkeypatch.setattr(external, "_owns_real_board", lambda _path: False)
    monkeypatch.setattr(pd, "_load_external_review", lambda: external)
    monkeypatch.setattr(pd, "local_config", lambda: {pd.DELEGATE_ENABLED_KEY: "true"})
    # 범위 감사·codex egress 승격은 이 진입점 판정과 독립 축이다(전용 회귀가 각각 소유) —
    # 여기서 죽이면 준비/예약/장부 축만 남는다.
    monkeypatch.setattr(pd, "begin_scope_audit", lambda *_a, **_kw: None)
    monkeypatch.setattr(pd, "codex_egress_escalation_required", lambda *_a, **_kw: False)

    # --prompt-file 은 containment 게이트가 --cwd 하위만 허용한다(유출 경로 차단) — slot 안에 둔다.
    prompt = slot / "prompt.md"
    prompt.write_text("이 구현을 검토하라.\n", encoding="utf-8")

    class _FakeRun:
        """`run_fn` seam — 실제 하네스는 절대 스폰하지 않는다(로컬 가짜 경계)."""

        def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "type": "result", "result": "판정: 통과\n\n**must-fix**:\n- 없음\n",
                    "session_id": "fake-session",
                }),
                "stderr": "", "timed_out": False,
            }

    rc = pd.main([
        "--role", "code-reviewer", "--prompt-file", str(prompt), "--cwd", str(slot),
        "--harness", "claude", "--model", "opus", "--ticket", ticket,
        "--output-dir", str(tmp_path / "raw"),
    ], run_fn=_FakeRun())

    error = capsys.readouterr().err
    assert rc == 0, "경고다 — 거부가 아니다"
    assert WARNING_MARK in error and "02-developer.md" in error

    rounds = _loaded_rounds(pd, pm_home, ticket)
    review_rounds = [item for item in rounds if item.role == "code-reviewer"]
    assert len(review_rounds) == 1 and review_rounds[0].ordinal == 3
    review_row = [
        row for row in _ledger_rows(pm_home)
        if row["role"] == "code-reviewer" and row["ordinal"] == 3
    ][-1]
    assert review_row["ticket"] == ticket and review_row["harvested_at"] is None


def test_board_section_add_applies_the_same_rule(pd, rounds_env, capsys):
    """진입점 3 — `board section-add --role code-reviewer` 도 같은 규칙·같은 rc."""
    pm_home, slot, tickets, board = rounds_env
    _write_spec(tickets, "T-7808")
    assert board.main(["section-add", "T-7808", "--role", "developer"]) == 0
    capsys.readouterr()

    rc = board.main(["section-add", "T-7808", "--role", "code-reviewer"])

    error = capsys.readouterr().err
    assert rc == 0
    assert WARNING_MARK in error and "02-developer.md" in error


def test_both_review_channels_get_the_same_rule(pd, rounds_env, capsys):
    """리뷰 채널은 둘 다 같은 판정 표면을 쓴다 — 채널마다 규칙이 갈리면 한쪽으로 샌다.

    추가 리뷰어 라운드는 회수 시점에 실 회신으로 예약되지만, `section-add` 로 미리 자리를 잡는
    경로가 열려 있어 같은 준비면을 지난다.
    """
    pm_home, slot, tickets, board = rounds_env
    for ticket, role in (("T-7814", "code-reviewer"), ("T-7815", "external-reviewer")):
        _write_spec(tickets, ticket)
        assert board.main(["section-add", ticket, "--role", "developer"]) == 0
        capsys.readouterr()

        assert board.main(["section-add", ticket, "--role", role]) == 0

        error = capsys.readouterr().err
        assert WARNING_MARK in error and "02-developer.md" in error, role


def test_board_section_add_is_silent_on_the_normal_path(pd, rounds_env, capsys):
    """역방향 — section-add 의 정상 경로(회수된 dev · dev 없음)는 조용하다."""
    pm_home, slot, tickets, board = rounds_env
    _write_spec(tickets, "T-7809")
    assert board.main(["section-add", "T-7809", "--role", "developer"]) == 0
    landed = _rounds_dir(pm_home, "T-7809") / "02-developer.md"
    landed.write_text(
        landed.read_text(encoding="utf-8") + "\n## 실 산출\n- 값\n",
        encoding="utf-8", newline="",
    )
    capsys.readouterr()

    assert board.main(["section-add", "T-7809", "--role", "code-reviewer"]) == 0
    assert WARNING_MARK not in capsys.readouterr().err

    _write_spec(tickets, "T-7810")
    capsys.readouterr()

    assert board.main(["section-add", "T-7810", "--role", "code-reviewer"]) == 0
    assert WARNING_MARK not in capsys.readouterr().err


# ── (5) 회수면과 같은 기준 ─────────────────────────────────────────────────

def test_seed_judgment_consumes_the_harvest_seed_comparison(pd, rounds_env, capsys):
    """준비면 판정과 회수면 시드 대조가 같은 라운드를 두고 같은 답을 낸다.

    두 표면이 각자 기준을 적으면 한쪽만 갱신돼 어긋난다 — 회수는 "산출 없음"이라 board 를 그대로
    두는데 준비는 "산출 있음"으로 읽어 조용히 지나가는 형상이 그것이다.
    """
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7811")
    developer = _prepare(pd, pm_home, slot, "T-7811", "developer")

    # 회수면: 손대지 않은 산출 → board 미변경(= 시드 그대로).
    unchanged = pd.harvest_ticket_copy(
        copy_path=developer.path, cwd=slot, pm_home=pm_home,
    )
    assert unchanged.changed is False
    # 준비면: 같은 라운드를 시드로 지목한다.
    stale = pd.unharvested_developer_round(_loaded_rounds(pd, pm_home, "T-7811"))
    assert stale is not None and stale.ordinal == developer.ordinal

    # 반대편도 같이 뒤집힌다 — 회수가 board 를 바꾸면 준비면도 더는 지목하지 않는다.
    _land(pd, pm_home, slot, developer)
    landed = pd.unharvested_developer_round(_loaded_rounds(pd, pm_home, "T-7811"))
    assert landed is None
    capsys.readouterr()


def test_seed_judgment_reads_the_round_pending_flag(pd, rounds_env):
    """판정 입력은 라운드가 이미 실은 `pending` 이다(별도 시드 재렌더 없음)."""
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7812")
    developer = _prepare(pd, pm_home, slot, "T-7812", "developer")
    rounds = _loaded_rounds(pd, pm_home, "T-7812")

    assert [item.pending for item in rounds] == [False, True]
    assert pd.unharvested_developer_round(rounds) is rounds[1]
    # 같은 목록에서 pending 만 뒤집으면 판정도 뒤집힌다 — 다른 입력을 보지 않는다는 값 단언.
    assert pd.unharvested_developer_round(
        [item._replace(pending=False) for item in rounds]
    ) is None
    assert developer.board_path.exists()


def test_crlf_seed_round_is_still_judged_as_seed(pd, rounds_env, capsys):
    """개행 표기만 다른 같은 골격을 '산출 있음'으로 읽으면 판정이 조용히 빠진다."""
    pm_home, slot, tickets, _board = rounds_env
    _write_spec(tickets, "T-7813", rounds=("developer", "code-reviewer"))
    developer = _prepare(pd, pm_home, slot, "T-7813", "developer")
    seed = developer.board_path.read_text(encoding="utf-8")
    developer.board_path.write_bytes(seed.replace("\n", "\r\n").encode("utf-8"))
    capsys.readouterr()

    _prepare(pd, pm_home, slot, "T-7813", "code-reviewer")

    assert WARNING_MARK in capsys.readouterr().err


# ── 스폰면([[T-0812]]) — external_review.py 의 `prepare_ticket_body` seam ──────────────
#
# 준비면과 축은 같되(`pd.unharvested_developer_round` 를 그대로 부른다) 문구는 다르다 — 형상 B
# 에서 "실리지 않습니다"라고 단정하지 않고, 이번 프롬프트에 실제로 실리는 developer 산출 라운드
# 이름(없으면 "없음")을 값으로 말한다. 라운드 01-architect 설계의 형상표 6행(A0/A1/B/C/D/E)을
# 여기서 고정한다.

SPAWN_WARNING_MARK = "산출 없는 developer 라운드 위에서 스폰됩니다"
EXTERNAL_REVIEW = TOOLS / "external_review.py"
DIFF = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


def _load_external():
    spec = importlib.util.spec_from_file_location(
        "external_review_dev_evidence", EXTERNAL_REVIEW,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def external():
    return _load_external()


def _wire_external(external, monkeypatch, pm_home: Path, *, conf=None):
    """스폰면 배선 — REPO 를 `rounds_env` 의 pm_home 에 고정하고 diff 추출만 대체한다.

    `_load_pm_delegate()`·`_load_ticket_rounds()` 는 실 로더 그대로 둔다 — 이 표면이 실제로
    같은 판정 함수(`pd.unharvested_developer_round`)를 부르는지가 검증 대상이다. touches 는
    `rounds_env` 픽스처 티켓이 `touches: []` 로 시드되므로(§인터페이스 밖) `parse_ticket_touches`
    만 테스트 seam 으로 고정한다(기존 `test_plain_list_ticket_scope_is_explicitly_a_test_fixture_seam`
    과 같은 축).
    """
    # 추가 리뷰어 대상은 구조화 tuple 이 필수다(미고정 모델 실행 경로가 없다). 이 축은 이 파일의
    # 검증 대상이 아니므로 해소 가능한 최소 tuple 을 기본으로 깔고, 호출자가 준 값이 이긴다.
    resolved = {
        external.ADDITIONAL_REVIEWER_HARNESS_KEY: "codex",
        external.ADDITIONAL_REVIEWER_MODEL_KEY: "test-model",
    }
    resolved.update(conf or {})
    monkeypatch.setattr(external, "REPO", pm_home)
    monkeypatch.setattr(external, "local_config", lambda repo=None: dict(resolved))
    monkeypatch.setattr(external, "extract_diff", lambda *args, **kwargs: (DIFF, []))
    monkeypatch.setattr(
        external, "parse_ticket_touches", lambda ticket_id, pm_home=None: ["x.py"],
    )


def _stub_real_send_external(external, monkeypatch, tmp_path, prompts: list[str]):
    """실 스폰 경계 스텁 — 격리 거울·리뷰 실행·산출 회수·측정 폭 기준점(다른 축 소유)을 자른다.

    측정 폭의 기준점은 묶음 장부가 선언한 통합 브랜치와의 merge-base 다. 이 픽스처의 PM 홈은
    코드 git 이 아니라 그 해소가 성립하지 않으므로 해소된 값을 그 자리에 넣는다 — 기준점
    해소·거부 자체는 전용 파일(`test_external_review_diff_cap.py`)이 실 git 으로 소유한다.
    """
    monkeypatch.setattr(
        external, "cluster_integration_tip", lambda *a, **k: ("task/main", None))
    monkeypatch.setattr(
        external, "integration_anchor", lambda *a, **k: ("a" * 40, None))

    def _workspace(*args, **kwargs):
        root = tmp_path / "reviewer"
        tree = root / "tree"
        home = root / "home"
        tree.mkdir(parents=True, exist_ok=True)
        home.mkdir(exist_ok=True)
        return external.ReviewerWorkspace(
            root=root, tree=tree, home=home,
            files=1, skipped_unsafe=0, git_repo=True,
        )

    def _run_review(prompt, *args, **kwargs):
        prompts.append(prompt)
        return {
            "reviewer": "fixture", "ok": True, "output": "판정: 통과",
            "verdict": {"has_must_fix": False, "has_pass": True}, "file": None,
            "failed": False, "started": True,
            "any_must_fix": False, "all_pass": True,
        }

    monkeypatch.setattr(external, "create_reviewer_workspace", _workspace)
    monkeypatch.setattr(external, "run_review", _run_review)
    monkeypatch.setattr(
        external, "_harvest_external_review_section", lambda *_a, **_k: None,
    )


def test_spawn_face_shape_a0_no_developer_round_is_silent(
    pd, external, rounds_env, monkeypatch, capsys,
):
    """형상 A0 — developer 라운드가 아예 없다(독립 검토). 무음 · 본문은 명세 원문 그대로."""
    pm_home, slot, tickets, _board = rounds_env
    ticket = "T-7820"
    _write_spec(tickets, ticket)
    _wire_external(external, monkeypatch, pm_home)
    capsys.readouterr()

    rc = external.main(["--ticket", ticket, "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert SPAWN_WARNING_MARK not in captured.err
    assert "--- 01-architect ---" in captured.out
    assert "developer ---" not in captured.out
    assert pd.unharvested_developer_round(_loaded_rounds(pd, pm_home, ticket)) is None


def test_spawn_face_shape_a1_seed_only_developer_round_warns(
    pd, external, rounds_env, monkeypatch, capsys,
):
    """형상 A1 — developer 시드뿐. 경고 + 라운드 이름 + '실리는 산출: 없음'. rc=0."""
    pm_home, slot, tickets, _board = rounds_env
    ticket = "T-7821"
    _write_spec(tickets, ticket)
    _prepare(pd, pm_home, slot, ticket, "developer")  # 01-developer.md 시드로 남긴다
    _wire_external(external, monkeypatch, pm_home)
    capsys.readouterr()

    rc = external.main(["--ticket", ticket, "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert SPAWN_WARNING_MARK in captured.err
    assert "02-developer.md" in captured.err
    assert "이번 프롬프트에 실리는 developer 산출 라운드: 없음" in captured.err
    assert "--- 01-architect ---" in captured.out
    assert "--- 02-developer" not in captured.out, "시드 라운드는 선별에서 빠진다"


def test_spawn_face_shape_b_latest_seed_after_landed_names_the_prior_output(
    pd, external, rounds_env, monkeypatch, capsys,
):
    """형상 B — T-0783 의 03-developer 재현(앞선 산출 뒤 최신이 시드). 경고 대상이되 문구는
    "실리지 않습니다"라고 단정하지 않고 실제로 실리는 산출 라운드 이름을 말한다."""
    pm_home, slot, tickets, _board = rounds_env
    ticket = "T-7822"
    _write_spec(tickets, ticket, rounds=("developer", "code-reviewer", "developer"))
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, ticket, "developer"))  # 02 산출
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, ticket, "code-reviewer"))
    _prepare(pd, pm_home, slot, ticket, "developer")  # 04 시드로 남긴다
    _wire_external(external, monkeypatch, pm_home)
    capsys.readouterr()

    rc = external.main(["--ticket", ticket, "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert SPAWN_WARNING_MARK in captured.err
    assert "04-developer.md" in captured.err, "시드 라운드 이름"
    assert "이번 프롬프트에 실리는 developer 산출 라운드: 02-developer.md" in captured.err
    assert "실리지 않습니다" not in captured.err, "형상 B 에서 거짓 문장을 내면 안 된다"
    assert "--- 02-developer ---" in captured.out, "앞 dev 산출은 그대로 실린다"
    assert "--- 04-developer" not in captured.out, "시드는 여전히 선별에서 빠진다"


def test_spawn_face_shape_c_latest_developer_round_is_output_is_silent(
    pd, external, rounds_env, monkeypatch, capsys,
):
    """형상 C — 최신 developer 라운드가 산출. 무음 · 그 산출이 그대로 실린다."""
    pm_home, slot, tickets, _board = rounds_env
    ticket = "T-7823"
    _write_spec(tickets, ticket)
    _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, ticket, "developer"))
    _wire_external(external, monkeypatch, pm_home)
    capsys.readouterr()

    rc = external.main(["--ticket", ticket, "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert SPAWN_WARNING_MARK not in captured.err
    assert "--- 02-developer ---" in captured.out


def test_spawn_face_shape_d_non_developer_seed_role_is_silent(
    pd, external, rounds_env, monkeypatch, capsys,
):
    """형상 D — architect 시드만(developer 아닌 역할). 시야 상한 — 무음."""
    pm_home, slot, tickets, _board = rounds_env
    ticket = "T-7824"
    _write_spec(tickets, ticket, rounds=("architect",))
    _prepare(pd, pm_home, slot, ticket, "architect")  # 시드 그대로
    _wire_external(external, monkeypatch, pm_home)
    capsys.readouterr()

    rc = external.main(["--ticket", ticket, "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert SPAWN_WARNING_MARK not in captured.err


def test_spawn_face_shape_e_paths_gate_without_ticket_is_silent(
    pd, external, rounds_env, monkeypatch, capsys,
):
    """형상 E — `--paths --gate T-NNNN`(티켓 본문 미조립). 이 판정 대상이 아니다 — 무음."""
    pm_home, slot, tickets, _board = rounds_env
    ticket = "T-7825"
    _write_spec(tickets, ticket)
    _prepare(pd, pm_home, slot, ticket, "developer")  # 시드 — 본문 미조립이면 무관해야 한다
    _wire_external(external, monkeypatch, pm_home)
    capsys.readouterr()

    rc = external.main(["--paths", "x.py", "--gate", ticket, "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert SPAWN_WARNING_MARK not in captured.err
    assert "### 게이트 티켓 본문" not in captured.out


def test_spawn_face_real_send_over_a_seed_developer_round_warns_without_seed_body(
    pd, external, rounds_env, monkeypatch, capsys, tmp_path,
):
    """실 스폰 1건(형상 A1) — 미리보기와 같은 seam 이 실전송 경로도 지나는지 값으로 확인한다.

    스텁 경계는 `create_reviewer_workspace`·`run_review`·`_harvest_external_review_section`
    셋뿐이다. rc=0 · stderr 경고 · 프롬프트에 시드 라운드 본문이 실리지 않음을 단언한다.
    """
    pm_home, slot, tickets, _board = rounds_env
    ticket = "T-7826"
    _write_spec(tickets, ticket)
    _prepare(pd, pm_home, slot, ticket, "developer")  # 01-developer.md 시드
    _wire_external(
        external, monkeypatch, pm_home, conf={"additional_reviewer.enabled": "true"},
    )
    prompts: list[str] = []
    _stub_real_send_external(external, monkeypatch, tmp_path, prompts)
    capsys.readouterr()

    rc = external.main([
        "--ticket", ticket, "--output-dir", str(tmp_path / "raw"),
    ])

    captured = capsys.readouterr()
    assert rc == 0, "경고다 — 거부가 아니다"
    assert SPAWN_WARNING_MARK in captured.err
    assert "02-developer.md" in captured.err
    assert len(prompts) == 1
    assert "--- 02-developer" not in prompts[0], "시드 라운드 본문은 실 전송 프롬프트에도 안 실린다"


def test_spawn_face_axis_agrees_with_the_preparation_face_across_the_shape_table(
    pd, external, rounds_env, monkeypatch, capsys,
):
    """축 동답 — 같은 `rounds` 목록에서 준비면 판정과 스폰면 경고 유무가 형상표 전 행에서
    일치하고, `pending` 을 뒤집으면 양쪽이 함께 반전한다([[T-0807]] 의
    `test_seed_judgment_reads_the_round_pending_flag` 와 같은 기법)."""
    pm_home, slot, tickets, _board = rounds_env

    def _shape_rounds(ticket: str, build, rounds=("developer",)) -> list:
        # 장부는 그 형상이 실제로 예약할 역할 순서를 선언한다(예산 수열 = 그 순서).
        _write_spec(tickets, ticket, rounds=rounds)
        build()
        return _loaded_rounds(pd, pm_home, ticket)

    shapes = {
        "A0": _shape_rounds("T-7830", lambda: None),
        "A1": _shape_rounds(
            "T-7831", lambda: _prepare(pd, pm_home, slot, "T-7831", "developer"),
        ),
        "B": _shape_rounds(
            "T-7832",
            lambda: (
                _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7832", "developer")),
                _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7832", "code-reviewer")),
                _prepare(pd, pm_home, slot, "T-7832", "developer"),
            ),
            rounds=("developer", "code-reviewer", "developer"),
        ),
        "C": _shape_rounds(
            "T-7833",
            lambda: _land(pd, pm_home, slot, _prepare(pd, pm_home, slot, "T-7833", "developer")),
        ),
        "D": _shape_rounds(
            "T-7834", lambda: _prepare(pd, pm_home, slot, "T-7834", "architect"),
            rounds=("architect",),
        ),
    }

    for name, rounds in shapes.items():
        capsys.readouterr()
        external._warn_seed_developer_round(rounds, ticket=name)
        warned = SPAWN_WARNING_MARK in capsys.readouterr().err
        judged = pd.unharvested_developer_round(rounds) is not None
        assert warned == judged, name

    # pending 반전 — 축은 같은 목록의 `pending` 만 본다(다른 입력을 보지 않는다는 값 단언).
    seed_rounds = shapes["A1"]
    assert pd.unharvested_developer_round(seed_rounds) is not None
    capsys.readouterr()
    external._warn_seed_developer_round(seed_rounds, ticket="T-7831")
    assert SPAWN_WARNING_MARK in capsys.readouterr().err

    flipped = [item._replace(pending=False) for item in seed_rounds]
    assert pd.unharvested_developer_round(flipped) is None
    capsys.readouterr()
    external._warn_seed_developer_round(flipped, ticket="T-7831")
    assert SPAWN_WARNING_MARK not in capsys.readouterr().err
