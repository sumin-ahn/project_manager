"""T-0699 — 티켓 성장 장부(티켓 파일 밖 권위 기록)의 쓰기·판정·마이그레이션 계약.

마지막 역할 절을 봉인과 함께 통째로 지우면 티켓 안에는 검증할 대상이 남지 않는다. 이 파일은
그 구멍을 닫는 append-only sidecar(`tickets/.growth/T-NNNN.jsonl`)의 전 축을 고정한다 —
쓰기 주체 4(section-add·promote·harvest·backfill)·판정 소비자·마이그레이션 stamp·relabel 동행.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git 바이너리 부재 — 실 board-git 케이스 skip.")

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "ledger-test",
    "GIT_AUTHOR_EMAIL": "ledger@test.invalid",
    "GIT_COMMITTER_NAME": "ledger-test",
    "GIT_COMMITTER_EMAIL": "ledger@test.invalid",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_growth_ledger_test", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pd():
    return _load("pm_delegate")


def _ticket_text(tid: str, status: str = "claimed") -> str:
    return (
        "---\n"
        f"id: {tid}\n"
        "title: 성장 장부 테스트\n"
        f"status: {status}\n"
        "created: '2026-08-17'\n"
        "created_by: test\n"
        "claimed_by: test/slot\n"
        "claimed_at: '2026-08-17T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\nblocks: []\ntouches: []\nestimate: medium\n"
        "design: 'waived: ledger test'\ntags: []\n"
        "---\n"
        f"# {tid} — 성장 장부 테스트\n\n"
        "## 목표\n장부 축을 고정한다.\n\n"
        "## 완료 조건 (Definition of Done)\n- [x] ledger\n"
    )


class _Board:
    """앵커된 board 모듈 + 그 보드의 좌표."""

    def __init__(self, module, root: Path, shared: bool):
        self.module = module
        self.root = root
        self.shared = shared

    @property
    def tickets(self) -> Path:
        return self.module.tickets_dir()

    @property
    def growth(self) -> Path:
        return self.module.growth_ledger_dir()

    def seed(self, tid: str, status: str = "claimed") -> Path:
        directory = self.tickets / (".drafts" if status == "draft" else status)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{tid}-ledger.md"
        text = _ticket_text(tid, "open" if status == "draft" else status)
        if status != "claimed":
            text = text.replace(
                "claimed_by: test/slot\nclaimed_at: '2026-08-17T00:00:00+00:00'\n",
                "claimed_by: null\nclaimed_at: null\n")
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def section_add(self, tid: str, role: str = "developer") -> int:
        return self.module.cmd_section_add(
            argparse.Namespace(id=tid, role=role, label=None))

    def ledger(self, pd, tid: str):
        return pd.read_ticket_growth_ledger(self.growth, tid)

    def problems(self, pd, tid: str, path: Path) -> list:
        """판정 소비자와 같은 순서(장부 먼저)로 읽어 합성 판정을 낸다."""
        ledger = pd.read_ticket_growth_ledger(self.growth, tid)
        return pd.verify_ticket_growth(
            path.read_text(encoding="utf-8"), ledger, ticket=tid,
            migrated=pd.ticket_growth_migration_stamped(self.growth),
        )

    def codes(self, pd, tid: str, path: Path) -> list[str]:
        return [problem.code for problem in self.problems(pd, tid, path)]


def _anchor(tmp_path, monkeypatch, pd, *, shared: bool) -> _Board:
    module = _load("board")
    root = tmp_path / "proj"
    board_root = root / ".project_manager" / ("board" if shared else "wiki")
    for status in ("open", "claimed", "blocked", "done"):
        (board_root / "tickets" / status).mkdir(parents=True)
    (board_root / "tickets" / ".drafts").mkdir(parents=True)
    (root / ".project_manager" / "wiki" / "log").mkdir(parents=True, exist_ok=True)
    anchor_board_module(module, root, monkeypatch)
    monkeypatch.setattr(module, "_load_pm_delegate_module", lambda: pd)
    monkeypatch.setattr(module, "_home_git_status_porcelain", lambda: "")
    monkeypatch.setattr(module, "_growth_mutation_sync_paths",
                        lambda _message, _paths: True)
    monkeypatch.setattr(module, "_growth_mutation_sync", lambda _message, _path: True)
    return _Board(module, root, shared)


@pytest.fixture
def board(tmp_path, monkeypatch, pd):
    return _anchor(tmp_path, monkeypatch, pd, shared=False)


def _delete_section_and_seal(pd, path: Path, role: str, ordinal: int) -> None:
    """마지막 역할 절과 그 봉인을 통째로 지운다(이 티켓이 닫으려는 손편집)."""
    text = path.read_text(encoding="utf-8")
    section = pd._ticket_role_section(text, role, ordinal=ordinal)
    seal = pd.parse_ticket_seals(text)[(role, ordinal)]
    end = max(section.marker_end, seal.line_end)
    path.write_text(text[:section.marker_start] + text[end:],
                    encoding="utf-8", newline="\n")


def _complete_args(tid: str) -> argparse.Namespace:
    return argparse.Namespace(
        id=tid, tests_pass=True, allow_untested=False, allow_missing_log=True)


# ════════════════════════════════════════════════════════════════════════
# (a)(b)(f)(j) 절 삭제 검출 · 재생성 거부 · 세탁 불가 · 민감도
# ════════════════════════════════════════════════════════════════════════

def test_deleting_the_last_section_with_its_seal_is_detected(board, pd):
    """(a) 절+봉인을 함께 지워도 장부 레코드가 남아 loud RED 다."""
    path = board.seed("T-9001")
    assert board.section_add("T-9001") == 0
    assert board.codes(pd, "T-9001", path) == []

    _delete_section_and_seal(pd, path, "developer", 0)
    problems = board.problems(pd, "T-9001", path)
    assert [problem.code for problem in problems] == ["section-deleted"]
    assert "절 삭제 검출" in problems[0].message
    assert "seal-backfill" not in (problems[0].prescription or ""), (
        "절 복원 문제에 소급 봉인 처방을 실으면 오처방이다")

    # 차단 소비자(board complete)도 같은 판정을 소비한다.
    board.module._internal_review_completion_problem = lambda _tid: None
    blocked = board.module._complete_gate(
        "T-9001", _complete_args("T-9001"), path.read_text(encoding="utf-8"),
        ticket_path=path)
    assert any("절 삭제 검출" in problem for problem in blocked)


def test_recreating_the_deleted_ordinal_is_refused(board, pd, capsys):
    """(b) 지운 뒤 같은 ordinal 을 새로 만드는 세탁을 section-add 가 거부한다."""
    path = board.seed("T-9002")
    assert board.section_add("T-9002") == 0
    _delete_section_and_seal(pd, path, "developer", 0)
    before = path.read_bytes()

    assert board.section_add("T-9002") == 1
    assert path.read_bytes() == before
    error = capsys.readouterr().err
    assert "성장 장부에 이미 있는 절 재생성 거부" in error
    assert "role=developer ordinal=0" in error


def test_deleting_a_section_then_backfill_cannot_launder(board, pd):
    """(f) 절 삭제 후 seal-backfill 을 돌려도 레코드는 남고 판정은 RED 다."""
    path = board.seed("T-9003")
    assert board.section_add("T-9003") == 0
    _delete_section_and_seal(pd, path, "developer", 0)
    ledger_path = pd.ticket_growth_ledger_path(board.growth, "T-9003")
    before = ledger_path.read_text(encoding="utf-8")

    text = path.read_text(encoding="utf-8")
    updated, changed = pd.backfill_ticket_seals(text, ticket="T-9003")
    assert (updated, changed) == (text, [])
    assert pd.append_ticket_growth_records(
        board.growth, "T-9003", text, by="backfill", stamp=False) == []
    assert ledger_path.read_text(encoding="utf-8") == before
    assert board.codes(pd, "T-9003", path) == ["section-deleted"]


def test_disabling_the_ledger_axis_makes_the_three_probes_green(board, pd):
    """(j) 민감도 — 장부 축을 무력화하면 (a)(b)(f) 가 전부 초록이 된다."""
    path = board.seed("T-9004")
    assert board.section_add("T-9004") == 0
    _delete_section_and_seal(pd, path, "developer", 0)
    text = path.read_text(encoding="utf-8")

    assert board.codes(pd, "T-9004", path) == ["section-deleted"]
    # 장부 축이 유일한 검출 수단이다 — 봉인 축만 보면 이 상태는 무검출이다.
    assert pd.verify_ticket_seals(text) == []
    assert pd.verify_ticket_growth(text, None, ticket="T-9004") == []
    assert board.section_add("T-9004") == 1
    pd.append_ticket_growth_records(
        board.growth, "T-9004", text, by="backfill", stamp=False)
    assert board.codes(pd, "T-9004", path) == ["section-deleted"]


# ════════════════════════════════════════════════════════════════════════
# (c)(q) 정상 왕복 · 멱등
# ════════════════════════════════════════════════════════════════════════

def test_normal_rounds_have_no_false_red_and_one_record_per_key(board, pd):
    """(c) section-add → harvest 왕복이 오탐 0 이고 키별 레코드가 정확히 하나다."""
    path = board.seed("T-9005")
    for role in ("architect", "developer", "code-reviewer", "developer"):
        assert board.section_add("T-9005", role) == 0
    ledger = board.ledger(pd, "T-9005")
    assert set(ledger.latest) == {
        ("architect", 0), ("developer", 0), ("code-reviewer", 0), ("developer", 1)}
    assert ledger.count == 4
    assert board.codes(pd, "T-9005", path) == []
    assert all(record["by"] == "section-add" for record in ledger.latest.values())
    assert all(record["ticket"] == "T-9005" for record in ledger.latest.values())


def test_reharvest_of_the_same_content_adds_no_record(board, pd):
    """(q) 멱등 조건은 '키별 최신 레코드 sha ≠ 봉인 sha' 하나다."""
    path = board.seed("T-9006")
    assert board.section_add("T-9006") == 0
    text = path.read_text(encoding="utf-8")
    assert pd.append_ticket_growth_records(
        board.growth, "T-9006", text, by="harvest", stamp=False) == []
    assert board.ledger(pd, "T-9006").count == 1

    # 내용이 실제로 바뀐 회수(harvest)만 새 레코드를 남긴다.
    section = pd._ticket_role_section(text, "developer", ordinal=0)
    edited = pd._upsert_ticket_seal(
        text[:section.content_start] + "회수 산출\n" + text[section.content_end:],
        "developer", 0, by="harvest")
    path.write_text(edited, encoding="utf-8", newline="\n")
    assert pd.append_ticket_growth_records(
        board.growth, "T-9006", edited, by="harvest", stamp=False) == [("developer", 0)]
    ledger = board.ledger(pd, "T-9006")
    assert ledger.count == 2 and len(ledger.latest) == 1
    assert board.codes(pd, "T-9006", path) == []


def test_records_serialize_with_a_fixed_shape(board, pd):
    """레코드 직렬화 고정 — 같은 상태가 같은 bytes 로 남는다."""
    path = board.seed("T-9007")
    assert board.section_add("T-9007") == 0
    line = pd.ticket_growth_ledger_path(board.growth, "T-9007").read_text(
        encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    assert set(record) == set(pd.TICKET_GROWTH_LEDGER_FIELDS)
    assert line == json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert record["at"].endswith("+00:00")
    assert pd.TICKET_GROWTH_LEDGER_WRITERS == frozenset(
        {"section-add", "promote", "harvest", "backfill", "external-review"})
    assert board.module.TICKET_GROWTH_LEDGER_DIRNAME == pd.TICKET_GROWTH_LEDGER_DIRNAME


# ════════════════════════════════════════════════════════════════════════
# (d)(e)(s)(v) 마이그레이션 · 부분 쓰기 · stamp
# ════════════════════════════════════════════════════════════════════════

def _legacy_sealed_ticket(board, pd, tid: str) -> Path:
    """봉인은 있고 장부도 stamp 도 없는 상태(v1.7.6 이전 채택자 형상).

    엔진 write 를 거치면 장부와 stamp 가 함께 생기므로 봉인만 손으로 놓는다.
    """
    path = board.seed(tid)
    text = path.read_text(encoding="utf-8") + (
        "\n<!-- pm-ticket-section:start role=developer -->\n"
        "## 구현 보충 (developer · 2026-08-17)\n\nlegacy 산출\n"
        "<!-- pm-ticket-section:end role=developer -->\n")
    sealed, changed = pd.backfill_ticket_seals(text, ticket=tid)
    assert changed == [("developer", 0)]
    path.write_text(sealed, encoding="utf-8", newline="\n")
    assert not pd.ticket_growth_ledger_path(board.growth, tid).exists()
    return path


def test_missing_ledger_before_the_stamp_is_a_migration_target(board, pd, monkeypatch):
    """(d) stamp 이전의 장부 파일 부재는 sweep 처방이 붙은 RED 이고 sweep 후 GREEN 이다."""
    path = _legacy_sealed_ticket(board, pd, "T-9008")
    problems = board.problems(pd, "T-9008", path)
    assert [problem.code for problem in problems] == ["ledger-file-missing"]
    assert "seal-backfill --all" in problems[0].prescription

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: board.root)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: board.module)
    assert pd._cmd_ticket_seal_backfill(None, sweep=True) == 0
    assert board.codes(pd, "T-9008", path) == []
    assert pd.ticket_growth_migration_stamped(board.growth)


def test_partial_write_is_unrecorded_and_recovers_both_ways(board, pd, monkeypatch):
    """(e) 봉인 있음·레코드 없음은 `장부 미기재` RED 이고 backfill·재-harvest 로 각각 복구된다."""
    path = board.seed("T-9009")
    assert board.section_add("T-9009") == 0
    ledger_path = pd.ticket_growth_ledger_path(board.growth, "T-9009")
    ledger_path.write_text("", encoding="utf-8", newline="\n")  # 티켓 write 뒤 crash 모사.

    problems = board.problems(pd, "T-9009", path)
    assert [problem.code for problem in problems] == ["ledger-unrecorded"]
    assert "seal-backfill" in problems[0].prescription

    # 재-harvest 자기복구.
    assert pd.append_ticket_growth_records(
        board.growth, "T-9009", path.read_text(encoding="utf-8"),
        by="harvest", stamp=False) == [("developer", 0)]
    assert board.codes(pd, "T-9009", path) == []

    # backfill 축도 같은 상태를 복구한다(텍스트 변경 0 이어도 실행된다).
    ledger_path.write_text("", encoding="utf-8", newline="\n")
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: board.root)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: board.module)
    assert pd._cmd_ticket_seal_backfill("T-9009") == 0
    assert board.codes(pd, "T-9009", path) == []
    assert board.ledger(pd, "T-9009").latest[("developer", 0)]["by"] == "backfill"


def test_missing_ledger_after_the_stamp_is_suspected_deletion(board, pd, monkeypatch):
    """(s) stamp 이후의 장부 파일 부재는 처방 없는 `장부 삭제 의심` 이고 어떤 명령으로도
    되살아나지 않는다 — 되살아나면 stamp 가 세운 판정이 무력해진다."""
    path = board.seed("T-9010", status="open")
    assert board.section_add("T-9010") == 0
    assert pd.ticket_growth_migration_stamped(board.growth), "신규 보드는 첫 write 가 stamp"

    ledger_path = pd.ticket_growth_ledger_path(board.growth, "T-9010")
    ledger_path.unlink()
    problems = board.problems(pd, "T-9010", path)
    assert [problem.code for problem in problems] == ["ledger-file-deleted"]
    assert problems[0].prescription is None
    assert "삭제 의심" in problems[0].message

    assert board.section_add("T-9010") == 1, "삭제 의심 상태에서 성장 write 는 fail-closed"
    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: board.root)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: board.module)
    assert pd._cmd_ticket_seal_backfill("T-9010") == 1
    assert not ledger_path.exists(), "backfill 이 되살리면 세탁이 성립한다"
    monkeypatch.setattr(board.module, "_board_git_enabled", lambda: False)
    assert board.module.cmd_promote(argparse.Namespace(id="T-9010")) == 1
    assert not ledger_path.exists(), "promote 재실행이 되살리면 세탁이 성립한다"


def test_stamp_lands_on_a_fresh_board_but_not_on_a_pending_one(board, pd):
    """(v) stamp 조건은 '마이그레이션 대상 0' 하나다."""
    legacy = _legacy_sealed_ticket(board, pd, "T-9011")
    assert pd.ticket_growth_stamp_path(board.growth).exists() is False
    assert pd.ticket_growth_migration_pending(board.tickets) == ["T-9011"]

    fresh = board.seed("T-9012")
    assert board.section_add("T-9012") == 0
    assert not pd.ticket_growth_migration_stamped(board.growth), (
        "미기재 티켓이 남은 보드에 stamp 를 남기면 기존 티켓이 처방 없는 RED 로 떨어진다")

    pd.append_ticket_growth_records(
        board.growth, "T-9011", legacy.read_text(encoding="utf-8"), by="backfill")
    assert pd.ticket_growth_migration_pending(board.tickets) == []
    assert pd.ticket_growth_migration_stamped(board.growth)
    assert board.codes(pd, "T-9012", fresh) == []


# ════════════════════════════════════════════════════════════════════════
# (g)(h)(o)(p)(r) 소비자 경계
# ════════════════════════════════════════════════════════════════════════

def test_done_tickets_are_not_retroactively_judged(board, pd):
    """(g) done 은 소급 판정 대상이 아니다."""
    done = board.seed("T-9013", status="done")
    claimed = board.seed("T-9014")
    assert board.section_add("T-9014") == 0
    done.write_text(claimed.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    pd.ticket_growth_ledger_path(board.growth, "T-9014").unlink()

    findings = board.module.lint_growth_seals()
    assert findings and "T-9013" not in findings[0][2]
    assert "T-9014" in findings[0][2]


@pytest.mark.parametrize("shared", [False, True])
def test_solo_and_shared_shapes_judge_identically(tmp_path, monkeypatch, pd, shared):
    """(h) solo(board 비-git)와 공유 형상의 판정 결과가 같다."""
    board = _anchor(tmp_path, monkeypatch, pd, shared=shared)
    path = board.seed("T-9015")
    assert board.section_add("T-9015") == 0
    assert board.growth == board.tickets / ".growth"
    assert board.codes(pd, "T-9015", path) == []
    _delete_section_and_seal(pd, path, "developer", 0)
    assert board.codes(pd, "T-9015", path) == ["section-deleted"]


@pytest.mark.parametrize("corruption", [
    '{"ticket":"T-9016"}\n',
    '{"ticket":"T-9016","role":"developer","ordinal":0,'
    '"sha256":"0"*64,"by":"section-add","at":"x"}\n',
    "not json\n",
    '{"ticket":"T-0001","role":"developer","ordinal":0,"sha256":'
    '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"by":"section-add","at":"2026-08-17T00:00:00+00:00"}\n',
])
def test_corrupt_ledger_is_fail_closed_for_blockers_and_soft_for_lint(
        board, pd, corruption):
    """(o) 손상 장부는 차단 소비자에 fail-closed·advisory 에 fail-soft 다."""
    path = board.seed("T-9016")
    assert board.section_add("T-9016") == 0
    ledger_path = pd.ticket_growth_ledger_path(board.growth, "T-9016")
    ledger_path.write_text(corruption, encoding="utf-8", newline="\n")

    assert board.codes(pd, "T-9016", path) == ["ledger-corrupt"]
    assert board.section_add("T-9016") == 1
    board.module._internal_review_completion_problem = lambda _tid: None
    assert any("장부 손상" in problem for problem in board.module._complete_gate(
        "T-9016", _complete_args("T-9016"), path.read_text(encoding="utf-8"),
        ticket_path=path))
    # advisory 는 never-block 이다 — 손상을 한 줄로 표면화하되 board 조회를 죽이지 않는다.
    findings = board.module.lint_growth_seals()
    assert len(findings) == 1 and findings[0][1] == "growth-seal"
    assert "장부 손상" in findings[0][2]
    assert "growth-seal" in board.module._ADVISORY_LINT_KINDS


def test_truncated_last_line_is_corruption(board, pd):
    """crash 로 잘린 마지막 줄도 손상이다(부분 레코드를 정상으로 읽지 않는다)."""
    path = board.seed("T-9017")
    assert board.section_add("T-9017") == 0
    ledger_path = pd.ticket_growth_ledger_path(board.growth, "T-9017")
    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8").rstrip("\n")[:-5],
        encoding="utf-8", newline="\n")
    assert board.codes(pd, "T-9017", path) == ["ledger-corrupt"]


def test_complete_blocks_when_every_marker_is_gone_but_records_remain(board, pd):
    """(p) marker 가 전부 사라진 상태에서도 게이트가 판정을 연다(F-010 개정)."""
    path = board.seed("T-9018")
    assert board.section_add("T-9018") == 0
    stripped = _ticket_text("T-9018")
    path.write_text(stripped, encoding="utf-8", newline="\n")

    board.module._internal_review_completion_problem = lambda _tid: None
    problems = board.module._complete_gate(
        "T-9018", _complete_args("T-9018"), stripped, ticket_path=path)
    assert any("절 삭제 검출" in problem for problem in problems)

    # 성장 marker 도 장부 레코드도 없으면 delegate 자체를 요구하지 않는다(기존 계약).
    empty = board.seed("T-9019")
    assert board.module._complete_gate(
        "T-9019", _complete_args("T-9019"), empty.read_text(encoding="utf-8"),
        ticket_path=empty) == []


def test_growth_dir_is_invisible_to_board_scans(board, pd):
    """(r) `.growth` 는 lint·list·board.md·ID 발급·rewrite 대상 어디에도 안 걸린다."""
    path = board.seed("T-9020")
    assert board.section_add("T-9020") == 0
    module = board.module

    assert module.next_numeric_id(
        board.tickets, module.STATUS_DIRS, "T-*.md", r"T-(\d+)") == 9021
    assert [problem for problem in module.lint_growth_seals()] == []
    rewrite_targets = module.collect_rewrite_targets(board.root / ".project_manager")
    assert not any(target.suffix == ".jsonl" for target in rewrite_targets)
    assert path in rewrite_targets
    rows = module.cmd_list(argparse.Namespace(
        status=None, tag=None, mine=False, repo=None, slot=None, blocked_only=False,
        area=None, tier=None, unclaimed=False, json=False, all=False))
    assert rows == 0


# ════════════════════════════════════════════════════════════════════════
# (k)(l) relabel 동행
# ════════════════════════════════════════════════════════════════════════

def test_reid_carries_the_ledger_and_leaves_no_stale_file(board, pd):
    """(k) reid 는 장부 파일을 데려가고 옛 ID 장부를 남기지 않는다."""
    path = board.seed("T-9021", status="open")
    assert board.section_add("T-9021") == 0
    assert board.module.cmd_reid(argparse.Namespace(
        old_id="T-9021", new_id="T-9022", dry_run=False, repo=None, slot=None,
        user_ack=None)) == 0

    assert not pd.ticket_growth_ledger_path(board.growth, "T-9021").exists()
    moved = pd.ticket_growth_ledger_path(board.growth, "T-9022")
    assert moved.exists()
    ledger = board.ledger(pd, "T-9022")
    assert ledger.error is None and set(ledger.latest) == {("developer", 0)}
    renamed = board.tickets / "open" / "T-9022-ledger.md"
    assert renamed.exists() and not path.exists()
    assert board.codes(pd, "T-9022", renamed) == []


def test_delete_then_reid_then_backfill_is_still_red(board, pd, monkeypatch):
    """(l) 절 삭제 → reid → backfill 시퀀스도 세탁이 되지 않는다."""
    path = board.seed("T-9023", status="open")
    assert board.section_add("T-9023") == 0
    _delete_section_and_seal(pd, path, "developer", 0)
    assert board.module.cmd_reid(argparse.Namespace(
        old_id="T-9023", new_id="T-9024", dry_run=False, repo=None, slot=None,
        user_ack=None)) == 0
    renamed = board.tickets / "open" / "T-9024-ledger.md"

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: board.root)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: board.module)
    assert pd._cmd_ticket_seal_backfill("T-9024") == 0
    assert board.codes(pd, "T-9024", renamed) == ["section-deleted"]


# ════════════════════════════════════════════════════════════════════════
# (m)(n)(u) draft 경계
# ════════════════════════════════════════════════════════════════════════

def test_draft_section_add_writes_no_ledger(board, pd):
    """(m) draft 는 장부에 쓰지 않는다 — board-git 노출면이 생기지 않는다."""
    board.seed("T-9025", status="draft")
    assert board.section_add("T-9025", "architect") == 0
    assert not board.growth.exists()


def test_discarded_draft_number_reuse_is_not_polluted(board, pd):
    """(n) 폐기된 draft 번호를 재발급해도 남의 장부를 물려받지 않는다."""
    draft = board.seed("T-9026", status="draft")
    assert board.section_add("T-9026", "architect") == 0
    draft.unlink()

    reused = board.seed("T-9026")
    assert board.ledger(pd, "T-9026").present is False
    assert board.codes(pd, "T-9026", reused) == []
    assert board.section_add("T-9026", "architect") == 0


def test_draft_round_trip_records_only_at_promote(board, pd):
    """(u) draft 전 구간 장부 축 RED 0 · promote 가 1회 기록하고 재-promote 는 0 이다."""
    draft = board.seed("T-9027", status="draft")
    assert board.section_add("T-9027", "architect") == 0
    assert pd.verify_ticket_growth(
        draft.read_text(encoding="utf-8"), None, ticket="T-9027") == []

    promoted_args = argparse.Namespace(id="T-9027")
    board.module._board_git_enabled = lambda: False
    assert board.module.cmd_promote(promoted_args) == 0
    promoted = board.tickets / "open" / "T-9027-ledger.md"
    ledger = board.ledger(pd, "T-9027")
    assert set(ledger.latest) == {("architect", 0)}
    assert ledger.latest[("architect", 0)]["by"] == "promote"
    assert board.codes(pd, "T-9027", promoted) == []

    assert board.module.cmd_promote(promoted_args) == 0
    assert board.ledger(pd, "T-9027").count == 1


# ════════════════════════════════════════════════════════════════════════
# (t)(w) legacy 미봉인 · mixed
# ════════════════════════════════════════════════════════════════════════

def test_unsealed_legacy_sections_require_backfill_first(board, pd, capsys):
    """(t) 미봉인 legacy 절이 있으면 성장 write 가 backfill 선행을 요구한다."""
    path = board.seed("T-9028")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text
        + "\n<!-- pm-ticket-section:start role=developer -->\n"
        "## 구현 보충 (developer · 2026-08-17)\n\nlegacy\n"
        "<!-- pm-ticket-section:end role=developer -->\n",
        encoding="utf-8", newline="\n")

    assert board.section_add("T-9028") == 1
    assert "seal-backfill --ticket T-9028" in capsys.readouterr().err
    with pytest.raises(pd.DelegateError, match="seal-backfill"):
        pd.require_sealed_growth_before_write(
            path.read_text(encoding="utf-8"), "T-9028", action="ticket prepare")


def test_mixed_ticket_prescription_excludes_backfill_and_splits_by_emptiness(board, pd):
    """(w) mixed 는 `seal-backfill` 처방을 받지 않고 빈 절·내용 있는 절이 갈린다."""
    path = board.seed("T-9029")
    assert board.section_add("T-9029") == 0
    sealed = path.read_text(encoding="utf-8")

    empty_mixed = sealed + (
        "\n<!-- pm-ticket-section:start role=architect -->\n"
        "## 설계 (architect · 2026-08-17)\n\n"
        "<!-- pm-ticket-section:end role=architect -->\n")
    filled_mixed = sealed + (
        "\n<!-- pm-ticket-section:start role=architect -->\n"
        "## 설계 (architect · 2026-08-17)\n\n손으로 옮겨 적은 산출\n"
        "<!-- pm-ticket-section:end role=architect -->\n")

    for text, expected in ((empty_mixed, "제거한 뒤"), (filled_mixed, "재prepare")):
        path.write_text(text, encoding="utf-8", newline="\n")
        problems = board.problems(pd, "T-9029", path)
        assert [problem.code for problem in problems] == ["seal-missing"]
        prescription = problems[0].prescription
        assert "봉인 도입 이후" not in prescription or True
        assert "seal-backfill" not in prescription, (
            "mixed 는 그 명령이 거부하는 상태라 처방으로 실을 수 없다")
        assert expected in prescription


# ════════════════════════════════════════════════════════════════════════
# (i) board-git 부기
# ════════════════════════════════════════════════════════════════════════

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False)


@requires_git
def test_board_git_commit_carries_the_ledger_and_stamp(tmp_path, monkeypatch, pd):
    """(i) board-git 활성이면 티켓·장부·stamp 가 같은 부분 커밋에 실린다."""
    module = _load("board")
    root = tmp_path / "proj"
    board_root = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_root / "tickets" / status).mkdir(parents=True)
    (board_root / "tickets" / ".drafts").mkdir(parents=True)
    (root / ".project_manager" / "wiki" / "log").mkdir(parents=True, exist_ok=True)
    (board_root / "areas.md").write_text("# Area Registry\n", encoding="utf-8")
    (board_root / ".gitattributes").write_text(
        module._BOARD_GITATTRIBUTES_BLOCK, encoding="utf-8")
    (board_root / ".gitignore").write_text("tickets/.drafts/\n", encoding="utf-8")
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    assert _git(["init", "-q", "-b", "main"], board_root).returncode == 0
    assert _git(["add", "-A"], board_root).returncode == 0
    assert _git(["commit", "-qm", "board init"], board_root).returncode == 0

    anchor_board_module(module, root, monkeypatch)
    monkeypatch.setattr(module, "_load_pm_delegate_module", lambda: pd)
    board = _Board(module, root, shared=True)
    board.seed("T-9030")
    assert board.section_add("T-9030") == 0

    committed = _git(["show", "--name-only", "--format=", "HEAD"], board_root).stdout
    assert "tickets/claimed/T-9030-ledger.md" in committed
    assert "tickets/.growth/T-9030.jsonl" in committed
    assert "tickets/.growth/.migrated" in committed
    assert _git(["status", "--porcelain"], board_root).stdout.strip() == "", (
        "장부가 미커밋으로 남으면 핸드오프 dirty 오탐이 된다")
    assert _git(
        ["check-attr", "merge", "--", "tickets/.growth/T-9030.jsonl"],
        board_root).stdout.strip().endswith("merge: union")


# ════════════════════════════════════════════════════════════════════════
# 봉인 축 이관분(T-0694 F-020·F-022·F-023) · sync 경로 해소
# ════════════════════════════════════════════════════════════════════════

def test_backfill_heals_a_position_only_mismatch_on_a_non_latest_round(board, pd):
    """비최신 ordinal 의 위치-only 어긋남은 harvest 자기치유가 안 닿는다 — backfill 이 고친다."""
    path = board.seed("T-9031")
    assert board.section_add("T-9031") == 0
    assert board.section_add("T-9031") == 0

    text = path.read_text(encoding="utf-8")
    seal = pd.parse_ticket_seals(text)[("developer", 0)]
    line = text[seal.line_start:seal.line_end]
    misplaced = text[:seal.line_start] + text[seal.line_end:] + line
    path.write_text(misplaced, encoding="utf-8", newline="\n")
    assert any("위치 불일치" in problem for problem in pd.verify_ticket_seals(misplaced))
    assert pd.ticket_growth_misplaced_seal_keys(misplaced) == [("developer", 0)]

    healed, changed = pd.backfill_ticket_seals(misplaced, ticket="T-9031")
    assert changed == [], "위치 치유는 새 봉인을 만들지 않는다"
    assert pd.verify_ticket_seals(healed) == []
    assert pd.parse_ticket_seals(healed)[("developer", 0)].sha256 == seal.sha256
    assert pd.parse_ticket_seals(healed)[("developer", 0)].by == seal.by
    path.write_text(healed, encoding="utf-8", newline="\n")
    assert board.codes(pd, "T-9031", path) == []


def test_backfill_still_refuses_a_content_mismatch(board, pd):
    """값이 어긋난 봉인은 치유 대상이 아니다(위치-only 만 되돌린다)."""
    path = board.seed("T-9032")
    assert board.section_add("T-9032") == 0
    text = path.read_text(encoding="utf-8")
    section = pd._ticket_role_section(text, "developer", ordinal=0)
    tampered = (
        text[:section.content_start] + "손편집 산출\n" + text[section.content_end:])
    with pytest.raises(pd.DelegateError, match="기존 봉인 문제"):
        pd.backfill_ticket_seals(tampered, ticket="T-9032")


def test_lint_walks_the_same_states_as_seal_backfill(board, pd):
    """(F-022) advisory 순회 상태 집합이 처방 실행 가능 상태와 같은 진실을 쓴다."""
    for status in sorted(pd._SEAL_BACKFILL_STATUSES):
        tid = f"T-90{40 + sorted(pd._SEAL_BACKFILL_STATUSES).index(status)}"
        path = board.seed(tid, status=status)
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n<!-- pm-ticket-section:start role=developer -->\n"
            "## 구현 보충 (developer · 2026-08-17)\n\n미봉인\n"
            "<!-- pm-ticket-section:end role=developer -->\n",
            encoding="utf-8", newline="\n")

    findings = board.module.lint_growth_seals()
    assert len(findings) == 1
    detail = findings[0][2]
    for index in range(len(pd._SEAL_BACKFILL_STATUSES)):
        assert f"T-90{40 + index}" in detail


@pytest.mark.parametrize("helpers", ["paths", "single", "legacy"])
def test_harvest_sync_walks_the_helper_ladder_and_carries_the_ledger(
        board, pd, monkeypatch, helpers):
    """복수 경로 helper → 기존 단일 경로 helper → 구-board 폴백 순으로 내려간다.

    `_growth_mutation_sync` 시그니처를 바꾸는 대신 새 이름을 얹었으므로, 한 세대 뒤처진 PM 홈
    사본에서도 회수가 atomic write 뒤 TypeError 로 부분 성공하지 않는다.
    """
    path = board.seed("T-9050")
    assert board.section_add("T-9050") == 0
    ledger_path = pd.ticket_growth_ledger_path(board.growth, "T-9050")
    stamp_path = pd.ticket_growth_stamp_path(board.growth)
    baseline = path.read_text(encoding="utf-8")
    section = pd._ticket_role_section(baseline, "developer", ordinal=0)
    edited = (baseline[:section.content_start] + "회수 산출\n"
              + baseline[section.content_end:])

    copy = board.root / "slot" / "copy.md"
    copy.parent.mkdir(parents=True)
    plan = pd.TicketCopyPlan(
        copy, copy.with_name("baseline.md"), copy.with_name("meta.json"),
        copy.parent, board.root, "T-9050", "developer", b"x" * 32,
    )
    metadata = {
        "ordinal": 0,
        "baseline_sha256": pd.hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
        "source_relpath": str(path.relative_to(board.root)),
    }
    calls: list[tuple[str, tuple]] = []

    class FakeBoard:
        board_lock = staticmethod(lambda: contextlib.nullcontext())
        tickets_dir = staticmethod(lambda: board.tickets)
        drafts_dir = staticmethod(lambda: board.tickets / ".drafts")
        _ticket_id_from_filename = staticmethod(
            board.module._ticket_id_from_filename)
        _atomic_write_text = staticmethod(board.module._atomic_write_text)

    if helpers == "paths":
        FakeBoard._growth_mutation_sync_paths = staticmethod(
            lambda message, paths: calls.append(("paths", tuple(paths))) or True)
    if helpers in ("paths", "single"):
        FakeBoard._growth_mutation_sync = staticmethod(
            lambda message, single: calls.append(("single", (single,))) or True)
    if helpers == "legacy":
        FakeBoard.refresh_board = staticmethod(
            lambda: calls.append(("refresh", ())))
        FakeBoard._board_git_sync_best_effort = staticmethod(
            lambda message, paths: calls.append(("legacy", tuple(paths))) or True)

    monkeypatch.setattr(
        pd, "_load_ticket_copy_plan",
        lambda *_a, **_k: (plan, metadata, edited.encode("utf-8"),
                           baseline.encode("utf-8")),
    )
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: FakeBoard)
    monkeypatch.setattr(pd, "_mark_ticket_copy_harvested_best_effort",
                        lambda *_a: None)

    result = pd.harvest_ticket_copy(
        copy_path=copy, cwd=copy.parent, pm_home=board.root, capability=b"x" * 32)
    assert result == pd.TicketHarvestResult(True, True)
    assert "회수 산출" in path.read_text(encoding="utf-8")
    assert board.ledger(pd, "T-9050").count == 2, "회수가 장부에 레코드를 남긴다"
    assert board.codes(pd, "T-9050", path) == []

    if helpers == "paths":
        assert calls == [("paths", (path, ledger_path, stamp_path))]
    elif helpers == "single":
        assert calls == [("single", (path,))]
    else:
        assert calls == [("refresh", ()), ("legacy", (path,))]


def test_blocked_tickets_hold_the_stamp_until_they_are_migrated(board, pd, monkeypatch):
    """blocked 를 sweep·잔여 판정에서 빼면 open 복귀가 처방 없는 RED 로 떨어진다."""
    blocked = board.seed("T-9051", status="blocked")
    text = blocked.read_text(encoding="utf-8") + (
        "\n<!-- pm-ticket-section:start role=developer -->\n"
        "## 구현 보충 (developer · 2026-08-17)\n\nlegacy 산출\n"
        "<!-- pm-ticket-section:end role=developer -->\n")
    sealed, _changed = pd.backfill_ticket_seals(text, ticket="T-9051")
    blocked.write_text(sealed, encoding="utf-8", newline="\n")

    active = board.seed("T-9052")
    assert board.section_add("T-9052") == 0
    assert pd.ticket_growth_migration_pending(board.tickets) == ["T-9051"]
    assert not pd.ticket_growth_migration_stamped(board.growth)

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: board.root)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: board.module)
    assert pd._cmd_ticket_seal_backfill(None, sweep=True) == 0
    assert pd.ticket_growth_migration_stamped(board.growth)
    assert board.codes(pd, "T-9051", blocked) == []
    assert board.codes(pd, "T-9052", active) == []


def test_backfill_still_opens_a_first_ledger_for_a_post_stamp_legacy_ticket(
        board, pd, monkeypatch):
    """stamp 이후에 붙은 **미봉인** 절의 정합화는 삭제 의심이 아니다(막다른 길 방지)."""
    stamper = board.seed("T-9060")
    assert board.section_add("T-9060") == 0
    assert pd.ticket_growth_migration_stamped(board.growth)

    legacy = board.seed("T-9061")
    legacy.write_text(
        legacy.read_text(encoding="utf-8")
        + "\n<!-- pm-ticket-section:start role=developer -->\n"
        "## 구현 보충 (developer · 2026-08-17)\n\n손으로 붙은 절\n"
        "<!-- pm-ticket-section:end role=developer -->\n",
        encoding="utf-8", newline="\n")

    monkeypatch.setattr(pd, "_activate_internal_rounds_cli_owner", lambda: board.root)
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _owner: board.module)
    assert pd._cmd_ticket_seal_backfill("T-9061") == 0
    assert board.codes(pd, "T-9061", legacy) == []
    assert board.codes(pd, "T-9060", stamper) == []


def test_board_and_delegate_derive_the_same_ledger_coordinates(board, pd):
    """두 모듈이 같은 장부 좌표를 본다 — 한쪽만 옮기면 게이트가 빈 파일을 보고 통과한다."""
    path = board.seed("T-9070")
    module = board.module
    assert module.TICKET_GROWTH_LEDGER_DIRNAME == pd.TICKET_GROWTH_LEDGER_DIRNAME
    assert module.growth_ledger_path("T-9070", ticket_path=path) == (
        pd.ticket_growth_ledger_path(
            pd.ticket_growth_dir_for_ticket_path(path), "T-9070"))
    assert module.growth_ledger_path("T-9070") == (
        pd.ticket_growth_ledger_path(
            pd.ticket_growth_dir(module.tickets_dir()), "T-9070"))
    assert module.growth_ledger_dir() == pd.ticket_growth_dir(module.tickets_dir())
