"""T-0790 — 공유 트리 wave 의 귀속 창(claimed ∪ 창 안 done).

같은 worktree 에서 여러 티켓이 동시에 돌면 `claimed/` 만 보는 스냅샷은 같은 wave 에서 **먼저
완료된** 티켓을 못 본다. 귀속 창은 내 `claimed_at` 이후 완료된 `done/` 티켓을 함께 본다. 이
창의 소비자는 완료 기록 잔여 판정의 다른 멤버 경로(`_sibling_member_scopes`)다. 이 파일은 실
git 저장소 + 실 board 엔진 사본 + 실 티켓 파일로 창의 구성원과 읽기 실패 보고를 고정한다.
board 대역은 쓰지 않는다 (실 보드 상태도 읽거나 바꾸지 않는다).
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import current_branch, write_cluster_ledger
from pm_engine.domain import finish_rules
from pm_engine.infra import finish_store, pm_paths


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

# 창 판정 입력 — board writer 형식(UTC ISO·인용 스칼라)과 같은 표기다.
_CLAIMED_AT = "2026-08-21T16:29:57+00:00"
_COMPLETED_IN_WINDOW = "2026-08-21T16:31:40+00:00"    # claim 이후 완료 — 창 안
_COMPLETED_BEFORE_WINDOW = "2026-08-21T15:00:00+00:00"  # claim 이전 완료 — 창 밖
_COMPLETED_AT_CUTOFF_Z = "2026-08-21T16:29:57Z"            # claim 시각과 같은 순간(`Z` 표기) — 창 안
_COMPLETED_JUST_BEFORE = "2026-08-21T16:29:56.999999+00:00"  # claim 직전 1µs — 창 밖


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )


def _ticket(touches: list[str], *, status: str = "claimed", estimate: str = "small",
            claimed_at: str | None = None, completed_at: str | None = None,
            claimed_rev: str | None = None, broken: bool = False) -> dict:
    """티켓 한 건의 형상 선언 — 창 축(status·시각)과 표기 축(touches)을 함께 준다."""
    return {
        "touches": touches, "status": status, "estimate": estimate,
        "claimed_at": claimed_at, "completed_at": completed_at,
        "claimed_rev": claimed_rev, "broken": broken,
    }


def _ticket_text(ticket_id: str, spec: dict) -> str:
    fields = [f"id: {ticket_id}", f"title: {ticket_id} shape",
              f"status: {spec['status']}"]
    for key in ("claimed_at", "completed_at", "claimed_rev"):
        if spec.get(key):
            # board writer 와 같은 인용 스칼라 — 인용이 없으면 YAML 이 datetime 으로 접는다.
            fields.append(f"{key}: '{spec[key]}'")
    if spec["broken"]:
        # 실측 손상 형상(인용 없는 콜론) — 전문 파싱이 닿으면 여기서 터진다.
        fields.append("design: required: 인용 없는 콜론")
    fields.append("touches:")
    fields += [f"- {touch}" for touch in spec["touches"]]
    fields.append(f"estimate: {spec['estimate']}")
    return "---\n" + "\n".join(fields) + "\n---\n\n# hermetic wave shape\n"


def _wave_shape(tmp_path: Path, tickets: dict[str, dict], files: dict[str, int]):
    """실 board 엔진 + 실 창 티켓(claimed/done) + 실 git diff 를 가진 tmp repo.

    반환은 `(repo 루트, 변경 전 baseline 커밋)` 이다.
    """
    root = tmp_path / "repo"
    tools = root / ".project_manager" / "tools"
    tools.parent.mkdir(parents=True)
    shutil.copytree(TOOLS, tools)
    for ticket_id, spec in tickets.items():
        status_dir = root / ".project_manager" / "wiki" / "tickets" / spec["status"]
        status_dir.mkdir(parents=True, exist_ok=True)
        (status_dir / f"{ticket_id}-shape.md").write_text(
            _ticket_text(ticket_id, spec), encoding="utf-8")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "wave-attribution-test@example.invalid")
    _git(root, "config", "user.name", "wave attribution test")
    # 판정 기준(통합 브랜치)은 묶음 장부가 소유한다 — 실 board 는 발행이 이 파일을 만든다.
    for ticket_id in tickets:
        write_cluster_ledger(
            root / ".project_manager" / "wiki", ticket_id,
            base_branch=current_branch(root), branch=current_branch(root))
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for relative in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    # 엔진 사본도 baseline 에 넣어 하네스 복사본이 미커밋 작업물로 안 보이게 한다.
    _git(root, "add", "seed.txt", ".project_manager", *files)
    _git(root, "commit", "-q", "-m", "seed")
    anchor = _git(root, "rev-parse", "HEAD").stdout.strip()
    for relative, lines in files.items():
        (root / relative).write_text("x\n" * lines, encoding="utf-8")
    return root, anchor


def _window(root: Path, ticket_id: str):
    """그 티켓의 귀속 창 스냅샷 — 소비자(`_sibling_member_scopes`)와 같은 입력으로 부른다."""
    home = pm_paths.home_at(root)
    return finish_store.get_in_window_ticket_touches(
        home, since=finish_store.get_ticket_claimed_at(home, ticket_id))


# ══ 형상 F — 귀속 창(claimed ∪ 창 안 done) ═════════════════════════════════


@requires_git
def test_window_includes_done_tickets_completed_after_my_claim(tmp_path):
    """내 claim 이후 완료된 티켓은 아직 같은 트리에 diff 를 남기고 있다 — 창 안이다."""
    root, _anchor = _wave_shape(
        tmp_path,
        {"T-1301": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1302": _ticket(["tests/test_b.py"], status="done",
                           claimed_at=_CLAIMED_AT,
                           completed_at=_COMPLETED_IN_WINDOW),
         "T-1303": _ticket(["tests/test_c.py"], status="done",
                           claimed_at=_CLAIMED_AT,
                           completed_at=_COMPLETED_AT_CUTOFF_Z)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17, "tests/test_c.py": 13},
    )

    snapshot = _window(root, "T-1301")

    # cutoff 와 같은 순간의 완료(`Z` 표기 = `+00:00`)도 창 안이다(`completed_at >= since`).
    assert set(snapshot) == {"T-1301", "T-1302", "T-1303"}
    assert snapshot["T-1302"] == ["tests/test_b.py"]

    # 창 시각은 writer 형식 UTC 만 받는 엄격 parser(domain.finish_rules)로 읽는다.
    utc = datetime.timezone.utc
    parse = finish_rules._parse_utc_iso
    assert parse(_CLAIMED_AT) == datetime.datetime(2026, 8, 21, 16, 29, 57, tzinfo=utc)
    assert parse(_COMPLETED_AT_CUTOFF_Z) == parse(_CLAIMED_AT)
    assert parse("2026-08-21T16:29:57.5+00:00") == datetime.datetime(
        2026, 8, 21, 16, 29, 57, 500000, tzinfo=utc)
    assert parse(_COMPLETED_JUST_BEFORE) < parse(_CLAIMED_AT) < parse("2026-08-21T16:29:57.000001Z")
    for rejected in ("2026-08-21 16:29:57+00:00",        # 공백 separator
                     "2026-08-21T16:29:57",              # naive(offset 없음)
                     "2026-08-21T16:29:57+09:00",        # 비UTC offset
                     "2026-02-30T16:29:57+00:00",        # 틀린 달력값
                     "2026-08-21T16:29:57.1234567+00:00",  # 소수점 7자리
                     None, 20260821, datetime.datetime(2026, 8, 21, tzinfo=utc)):
        assert parse(rejected) is None, rejected


@requires_git
def test_window_excludes_done_tickets_completed_before_my_claim(tmp_path):
    """내 claim 이전에 끝난 티켓은 이 wave 의 참여자가 아니다 — 창 밖이다."""
    root, _anchor = _wave_shape(
        tmp_path,
        {"T-1311": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1312": _ticket(["tests/test_b.py"], status="done",
                           completed_at=_COMPLETED_BEFORE_WINDOW),
         "T-1313": _ticket(["tests/test_c.py"], status="done",
                           completed_at=_COMPLETED_JUST_BEFORE)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17, "tests/test_c.py": 13},
    )

    snapshot = _window(root, "T-1311")

    assert set(snapshot) == {"T-1311"}


@requires_git
def test_missing_claimed_at_folds_to_the_claimed_only_window(tmp_path):
    """`claimed_at` 이 없으면 창을 정할 수 없다 — `claimed/` 만 보는 종전 창."""
    root, _anchor = _wave_shape(
        tmp_path,
        {"T-1321": _ticket(["tests"]),
         "T-1322": _ticket(["tests/test_b.py"], status="done",
                           completed_at=_COMPLETED_IN_WINDOW)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17, "tests/test_c.py": 13},
    )

    snapshot = _window(root, "T-1321")

    assert set(snapshot) == {"T-1321"}


@requires_git
def test_out_of_window_broken_ticket_is_never_fully_parsed(tmp_path):
    """창 밖 done 티켓은 frontmatter 머리만 읽는다 — 손상 YAML 이 창을 깨지 않는다."""
    root, _anchor = _wave_shape(
        tmp_path,
        {"T-1331": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1332": _ticket(["tests/test_b.py"], status="done", broken=True,
                           completed_at=_COMPLETED_BEFORE_WINDOW)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17},
    )

    snapshot = _window(root, "T-1331")

    assert set(snapshot) == {"T-1331"}


@requires_git
def test_in_window_broken_ticket_is_reported_as_a_snapshot_error(tmp_path):
    """창 **안** 티켓이 손상되면 조용히 넘기지 않는다 — 읽기 실패가 전파된다."""
    root, _anchor = _wave_shape(
        tmp_path,
        {"T-1341": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1342": _ticket(["tests/test_b.py"], status="done", broken=True,
                           completed_at=_COMPLETED_IN_WINDOW)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17},
    )

    with pytest.raises(yaml.YAMLError):
        _window(root, "T-1341")
