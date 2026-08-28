"""구 컨테이너(역할 절) → 라운드 파일 일회성 변환 — `board.py rounds migrate`.

변환 전 board 는 역할 산출을 명세 본문 안에 marker 로 감싸 누적했고(`pm-ticket-section` +
봉인 줄) 그 옆에 성장 장부 `tickets/.growth/`, PM 홈에 구 위임 사본 장부·신뢰 사본, 슬롯에
`<티켓>/<역할>/<run>/` 레이아웃을 뒀다. 이 파일은 그 전부가 **한 번의 실행으로 사라지는지**를
실 CLI·실 board-git 으로 잰다 — 두 형식을 영구히 읽는 리더를 두지 않는 것이 변환의 목적이라,
"변환 후 marker 0 · 판정 red 0 · 재실행 변경 0" 이 이 파일의 중심 축이다.

픽스처는 실 board 실측 형상을 따른다: 대상 34건 · 대부분 done · 절 다수 보유 · 봉인 있음 ·
절 사이에 PM 판정 텍스트가 끼어 있음. 픽스처가 그 형상을 잃으면(예: 한 티켓 한 절) 순번·
PM 텍스트 보존·왕복 동일성 축이 공허해진다.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from conftest import anchor_board_module
# 구 형상 픽스처는 라운드 예약 테스트의 board-git 헬퍼를 그대로 쓴다 — board 형상 조립을 두
# 벌 두면 한쪽만 현재화된다.
from test_board_ticket_growth import (
    _GIT_IDENTITY, _git, _make_board_git, _ticket_text,
)

REPO = Path(__file__).resolve().parents[1]
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
PM_UPDATE_PY = REPO / ".project_manager" / "tools" / "pm_update.py"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git 바이너리 부재 — 실 board-git 케이스 skip.")

# 실 board 실측 형상(변환 대상 34건)을 픽스처 크기로 고정한다.
_MEASURED_MIGRATION_TICKETS = 34
_SEAL_SHA = "0" * 64


def _load_board():
    spec = importlib.util.spec_from_file_location("board_rounds_migrate", BOARD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update_rounds_migrate", PM_UPDATE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_section(role: str, ordinal: int, *, body: str, sealed: bool = True,
                    label: str | None = None) -> tuple[str, str]:
    """구 형식 절 블록과 **그 절의 본문**(=변환이 라운드 파일에 담을 bytes)을 함께 낸다."""
    content = (
        f"## {label or role} ({role} · 2026-08-17)\n"
        "\n"
        f"{body}\n"
    )
    block = (
        f"<!-- pm-ticket-section:start role={role} -->\n"
        f"{content}"
        f"<!-- pm-ticket-section:end role={role} -->\n"
    )
    if sealed:
        block += (f"<!-- pm-ticket-seal role={role} ordinal={ordinal} "
                  f"sha256={_SEAL_SHA} by=harvest -->\n")
    return block, content


def _legacy_ticket_text(tid: str, status: str, sections: list[tuple[str, str]], *,
                        pm_notes: bool = True) -> tuple[str, list[str]]:
    """구 형상 티켓 전문과 절 본문 목록 — 절 사이에 PM 판정 텍스트를 끼운다."""
    text = _ticket_text(tid, status)
    contents: list[str] = []
    for index, (role, body) in enumerate(sections):
        block, content = _legacy_section(role, index, body=body)
        text += "\n" + block
        contents.append(content)
        if pm_notes:
            text += f"\n## PM 판정 {index + 1} ({tid})\n판정 텍스트는 명세에 남는다.\n"
    return text, contents


def _write_legacy_ticket(board_dir: Path, tid: str, status: str,
                         sections: list[tuple[str, str]]) -> tuple[Path, list[str]]:
    text, contents = _legacy_ticket_text(
        tid, "open" if status == ".drafts" else status, sections)
    path = board_dir / "tickets" / status / f"{tid}-legacy.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path, contents


def _seed_growth_ledger(board_dir: Path, tickets: list[str]) -> Path:
    growth = board_dir / "tickets" / ".growth"
    growth.mkdir(parents=True, exist_ok=True)
    (growth / ".migrated").write_text("1\n", encoding="utf-8")
    for tid in tickets:
        (growth / f"{tid}.jsonl").write_text(
            json.dumps({"ticket": tid, "role": "developer", "ordinal": 0}) + "\n",
            encoding="utf-8")
    return growth


def _seed_gitattributes(board_dir: Path) -> Path:
    path = board_dir / ".gitattributes"
    path.write_text(
        "areas.md merge=union\n"
        "# Windows checkout에서도 엔진-소유 텍스트의 논리 개행을 LF로 유지한다.\n"
        "*.md text eol=lf\n"
        "*.jsonl text eol=lf\n"
        "# 티켓 성장 장부 = append-only 권위 기록 — 서로 다른 PM 의 append 가 merge 에서 서로를\n"
        "# 지우지 않도록 같은 union 드라이버로 양쪽 줄을 모두 보존한다.\n"
        "tickets/.growth/*.jsonl merge=union\n",
        encoding="utf-8", newline="")
    return path


def _seed_legacy_copies(board, root: Path, *, unharvested: int = 0,
                        slot: str = "work/product_1") -> dict:
    """PM 홈 구 사본 장부·신뢰 사본과 슬롯의 구/현행 레이아웃을 함께 깐다."""
    local = board.LOCAL_DIR
    local.mkdir(parents=True, exist_ok=True)
    slot_path = root / slot
    legacy_dir = slot_path.joinpath(*board._LEGACY_TICKET_COPY_ROOT_RELS) / "T-2001"
    legacy_run = legacy_dir / "developer" / "run0001"
    legacy_run.mkdir(parents=True)
    (legacy_run / "ticket-T-2001.md").write_text("구 사본\n", encoding="utf-8")
    current_run = legacy_dir / "run0002"
    current_run.mkdir(parents=True)
    (current_run / "01-developer.md").write_text("현행 라운드 사본\n", encoding="utf-8")

    rows = [{"ticket": "T-2001", "role": "developer", "ordinal": 0,
             "copy": str(legacy_run / "ticket-T-2001.md"),
             "harvested_at": None if index < unharvested else "2026-08-17T00:00:00+00:00"}
            for index in range(3)]
    ledger = local / board._LEGACY_TICKET_COPY_LEDGER_NAME
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    trust = local / board._LEGACY_TICKET_COPY_TRUST_DIRNAME
    (trust / "run0001").mkdir(parents=True)
    (trust / "run0001" / "metadata.json").write_text("{}\n", encoding="utf-8")
    board.LEASES_FILE.write_text(
        json.dumps({"leases": [{"slot": slot, "repo": "product", "session": "product_1",
                                "state": "idle"}]}), encoding="utf-8")
    return {"ledger": ledger, "trust": trust, "legacy_run": legacy_run,
            "current_run": current_run, "legacy_role_dir": legacy_dir / "developer",
            "slot_path": slot_path}


@pytest.fixture
def legacy_board(tmp_path, monkeypatch):
    """구 형상 board(별도 git·remote 포함) + PM 홈 로컬 산출물을 tmp 로 완전 격리한다.

    `LOCAL_DIR`·`LEASES_FILE` 재앵커는 **필수**다 — 변환은 그 아래 파일을 지우므로, 재앵커가
    빠지면 테스트가 실 트리의 PM 홈 산출물을 지운다.
    """
    bare = tmp_path / "remote.git"
    assert _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path).returncode == 0
    board_dir = _make_board_git(tmp_path, bare)
    board = _load_board()
    anchor_board_module(board, tmp_path, monkeypatch)
    local = tmp_path / ".project_manager" / ".local"
    monkeypatch.setattr(board, "LOCAL_DIR", local)
    monkeypatch.setattr(board, "LEASES_FILE", local / "worktree-leases.json")
    local.mkdir(parents=True, exist_ok=True)
    # 등록 슬롯 해소(lease 장부 + linked worktree + PM 홈 소유)는 git 앵커 가드가 소유하는
    # 판정이고 그 축은 그쪽 게이트가 전수로 잰다. 여기서는 그 **산출**을 주입해 "등록 슬롯이
    # 주어졌을 때 무엇을 지우는가" 만 본다(실 worktree 를 세우면 이 파일의 축이 흐려진다).
    monkeypatch.setattr(
        board, "_registered_slot_paths",
        lambda pm_home, **_kwargs: tuple(
            path for path in ((tmp_path / "work" / "product_1"),) if path.is_dir()))
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    assert str(board.LOCAL_DIR).startswith(str(tmp_path)), "PM 홈 로컬 경로 미격리"
    return board, board_dir, tmp_path


def _commit_board(board_dir: Path, message: str = "seed") -> None:
    assert _git(["add", "-A"], board_dir).returncode == 0
    assert _git(["commit", "-qm", message], board_dir).returncode == 0
    assert _git(["push", "-q"], board_dir).returncode == 0


def _read_exact(path: Path) -> str:
    """줄끝 번역 없이 읽는다 — 회수 bytes 보존이 이 파일의 판정 축이다."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _rounds_dir(board_dir: Path, tid: str) -> Path:
    return board_dir / "tickets" / "rounds" / tid


def _round_names(board_dir: Path, tid: str) -> list[str]:
    directory = _rounds_dir(board_dir, tid)
    return sorted(item.name for item in directory.iterdir()) if directory.is_dir() else []


def _seed_measured_board(board_dir: Path) -> dict[str, list[str]]:
    """실측 형상(변환 대상 34건 · 절 다수 · 봉인)을 그대로 깐 픽스처."""
    expected: dict[str, list[str]] = {}
    for index in range(_MEASURED_MIGRATION_TICKETS):
        tid = f"T-3{index:03d}"
        sections = [("developer", f"{tid} 구현 산출.")]
        if index % 2 == 0:
            sections.append(("code-reviewer", f"{tid} 리뷰 산출.\n\n- F-001 must-fix"))
        if index % 5 == 0:
            sections.append(("developer", f"{tid} 재작업 산출."))
        if index % 11 == 0:
            sections.append(("external-reviewer",
                             "<!-- pm-review-refused role=external-reviewer -->\n\n"
                             f"{tid} 거부된 추가 리뷰 산출."))
        _path, contents = _write_legacy_ticket(board_dir, tid, "done", sections)
        expected[tid] = contents
    _seed_growth_ledger(board_dir, sorted(expected)[:13])
    _seed_gitattributes(board_dir)
    _commit_board(board_dir, "legacy shape")
    return expected


# ── 변환 왕복 (실측 형상) ────────────────────────────────────────────────────


@requires_git
def test_migrate_converts_the_measured_board_shape_round_trip(legacy_board, capsys):
    """실측 34건 형상이 라운드 파일로 옮겨지고 명세엔 marker 도 봉인도 남지 않는다.

    라운드 bytes 는 절 본문 **그대로**여야 한다 — 변환이 내용을 손대면 옛 산출의 판정
    표면(리뷰 블록·거부 표식)이 조용히 달라진다.
    """
    board, board_dir, _root = legacy_board
    expected = _seed_measured_board(board_dir)

    assert board.main(["rounds", "migrate"]) == 0

    out = capsys.readouterr().out
    assert f"변환 대상 티켓 {_MEASURED_MIGRATION_TICKETS}건" in out
    assert f"변환 완료 {_MEASURED_MIGRATION_TICKETS}건" in out
    for tid, contents in expected.items():
        names = _round_names(board_dir, tid)
        assert len(names) == len(contents), f"{tid} 라운드 수 불일치: {names}"
        texts = [_read_exact(_rounds_dir(board_dir, tid) / name) for name in names]
        assert texts == contents, f"{tid} 라운드 bytes 불일치"
        spec = (board_dir / "tickets" / "done" / f"{tid}-legacy.md").read_text(
            encoding="utf-8")
        assert "pm-ticket-section" not in spec and "pm-ticket-seal" not in spec
        assert f"## PM 판정 1 ({tid})" in spec, "절 밖 PM 텍스트가 사라졌다"
    assert board.lint_legacy_growth_sections() == []


@requires_git
def test_migrate_keeps_role_order_and_names_rounds_by_appearance(legacy_board):
    """순번은 **등장 순서**다 — 파일 이름만 봐도 시간 순서를 읽을 수 있어야 한다."""
    board, board_dir, _root = legacy_board
    _write_legacy_ticket(board_dir, "T-3101", "done", [
        ("architect", "설계 산출."),
        ("developer", "구현 산출."),
        ("code-reviewer", "리뷰 산출."),
        ("developer", "재작업 산출."),
    ])
    _commit_board(board_dir)

    assert board.main(["rounds", "migrate"]) == 0

    assert _round_names(board_dir, "T-3101") == [
        "01-architect.md", "02-developer.md", "03-code-reviewer.md", "04-developer.md",
    ]


@requires_git
def test_show_renders_the_same_section_bodies_before_and_after(legacy_board, capsys):
    """조회 렌더는 절 본문 기준으로 변환 전후가 같다(자리만 명세 안 → 라운드 파일)."""
    board, board_dir, _root = legacy_board
    _path, contents = _write_legacy_ticket(board_dir, "T-3200", "done", [
        ("developer", "구현 산출 본문."),
        ("code-reviewer", "리뷰 산출 본문."),
    ])
    _commit_board(board_dir)
    assert board.main(["show", "T-3200"]) == 0
    before = capsys.readouterr().out

    assert board.main(["rounds", "migrate"]) == 0
    capsys.readouterr()
    assert board.main(["show", "T-3200"]) == 0
    after = capsys.readouterr().out

    for content in contents:
        assert content in before and content in after, "절 본문이 조회에서 사라졌다"
    assert "--- 01-developer ---" in after and "--- 02-code-reviewer ---" in after
    assert "pm-ticket-section" not in after


# ── 잔여 제거 · board 커밋 ───────────────────────────────────────────────────


@requires_git
def test_migrate_removes_growth_ledger_and_union_declaration_in_one_commit(legacy_board):
    """장부 디렉터리와 그 union 선언이 사라지고, 변경 전부가 커밋 하나에 실린다."""
    board, board_dir, _root = legacy_board
    _write_legacy_ticket(board_dir, "T-3300", "done", [("developer", "구현 산출.")])
    growth = _seed_growth_ledger(board_dir, ["T-3300"])
    attributes = _seed_gitattributes(board_dir)
    _commit_board(board_dir)
    before = int(_git(["rev-list", "--count", "HEAD"], board_dir).stdout)

    assert board.main(["rounds", "migrate"]) == 0

    assert not growth.exists()
    text = attributes.read_text(encoding="utf-8")
    assert "tickets/.growth" not in text
    assert "*.jsonl text eol=lf" in text, "무관한 텍스트 선언까지 지웠다"
    assert "areas.md merge=union" in text
    assert int(_git(["rev-list", "--count", "HEAD"], board_dir).stdout) == before + 1
    subject = _git(["log", "-1", "--format=%s"], board_dir).stdout.strip()
    assert subject == "rounds migrate: 1 tickets · .growth removed"
    tracked = _git(["ls-files"], board_dir).stdout.splitlines()
    assert "tickets/rounds/T-3300/01-developer.md" in tracked
    assert not any(name.startswith("tickets/.growth/") for name in tracked)


@requires_git
def test_migrate_removes_legacy_delegate_artifacts_but_keeps_the_current_layout(
        legacy_board):
    """구 사본 장부·신뢰 사본·슬롯의 역할 디렉터리는 사라지고 현행 run 디렉터리는 남는다."""
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3400", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)
    artifacts = _seed_legacy_copies(board, root)

    assert board.main(["rounds", "migrate"]) == 0

    assert not artifacts["ledger"].exists()
    assert not artifacts["trust"].exists()
    assert not artifacts["legacy_role_dir"].exists()
    assert (artifacts["current_run"] / "01-developer.md").is_file(), (
        "현행 레이아웃 run 디렉터리까지 지웠다")


@requires_git
def test_migrate_finds_legacy_copies_in_the_repo_itself_when_solo(
        legacy_board, capsys, monkeypatch):
    """solo 형상(등록 슬롯 0 · PM 홈 == 코드 트리)에서도 REPO 자신의 구 레이아웃을 지운다(F-002).

    등록 슬롯 판정만 보면 solo 는 항상 슬롯 0 이라, 구 사본이 REPO 자신에 남아도 순회·삭제
    양쪽에서 조용히 빠지고 "정리 완료" 가 거짓이 된다.
    """
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3995", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)
    monkeypatch.setattr(board, "_registered_slot_paths", lambda pm_home, **_kwargs: ())
    artifacts = _seed_legacy_copies(board, root, slot="")

    assert board.main(["rounds", "migrate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "슬롯 구 레이아웃 사본 1개" in out, "등록 슬롯 0 인데 REPO 자신의 구 사본을 못 봤다"

    assert board.main(["rounds", "migrate"]) == 0

    assert not artifacts["ledger"].exists()
    assert not artifacts["trust"].exists()
    assert not artifacts["legacy_role_dir"].exists()
    assert (artifacts["current_run"] / "01-developer.md").is_file(), (
        "현행 레이아웃 run 디렉터리까지 지웠다")


@requires_git
def test_migrate_holds_legacy_copy_deletion_when_the_board_commit_fails(
        legacy_board, capsys, monkeypatch):
    """board 커밋이 서지 못하면(ready=False) 비가역 사본 삭제를 보류하고, 재실행이 마저
    지운다(F-004 · 멱등).
    """
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3990", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)
    artifacts = _seed_legacy_copies(board, root)
    # `monkeypatch` 는 `legacy_board` 픽스처와 이 테스트가 **같은 인스턴스**를 공유한다
    # (function-scope fixture caching) — `monkeypatch.undo()` 를 쓰면 픽스처가 앵커한
    # REPO·LOCAL_DIR·LEASES_FILE 까지 실 경로로 되돌아가 재실행이 실 worktree 를 건드린다.
    # 이 속성 하나만 되돌리도록 명시 재대입한다(전역 undo 금지).
    real_sync = board._rounds_mutation_sync_paths
    monkeypatch.setattr(board, "_rounds_mutation_sync_paths", lambda *_a, **_k: False)

    assert board.main(["rounds", "migrate"]) == 0

    err = capsys.readouterr().err
    assert "커밋 실패" in err and "보류" in err
    assert artifacts["ledger"].exists(), "커밋 실패인데 구 사본 장부를 지웠다"
    assert artifacts["trust"].exists(), "커밋 실패인데 신뢰 사본을 지웠다"
    assert artifacts["legacy_role_dir"].exists(), "커밋 실패인데 슬롯 구 사본을 지웠다"
    assert _round_names(board_dir, "T-3990") == ["01-developer.md"], (
        "라운드 파일은 board 커밋 성패와 무관하게 이미 디스크에 있어야 한다")

    monkeypatch.setattr(board, "_rounds_mutation_sync_paths", real_sync)  # 재실행이 마저 지운다.
    assert board.main(["rounds", "migrate"]) == 0

    assert not artifacts["ledger"].exists()
    assert not artifacts["trust"].exists()
    assert not artifacts["legacy_role_dir"].exists()


@requires_git
def test_migrate_stops_on_unharvested_legacy_copies_without_the_discard_flag(
        legacy_board, capsys):
    """미회수 사본이 남아 있으면 **아무것도 지우지 않고** rc 1 로 멈춘다(삭제는 비가역)."""
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3500", "done", [("developer", "구현 산출.")])
    growth = _seed_growth_ledger(board_dir, ["T-3500"])
    _commit_board(board_dir)
    artifacts = _seed_legacy_copies(board, root, unharvested=2)

    assert board.main(["rounds", "migrate"]) == 1

    err = capsys.readouterr().err
    assert "미회수 2건" in err
    assert (artifacts["legacy_run"] / "ticket-T-2001.md").as_posix() in err.replace("\\", "/"), (
        "미회수 사본 경로가 안내에 없다")
    assert artifacts["ledger"].exists() and artifacts["trust"].exists()
    assert growth.is_dir(), "게이트가 서기 전에 장부를 지웠다"
    assert _round_names(board_dir, "T-3500") == [], "게이트가 서기 전에 라운드를 만들었다"


@requires_git
def test_migrate_discard_flag_removes_unharvested_copies_too(legacy_board, capsys):
    """`--discard-unharvested` 는 미회수 사본까지 함께 지우고 변환을 끝낸다(목록은 표시).

    등록 슬롯 **밖** 경로는 이 명령의 소유가 아니라 지우지 않는다 — 그 사실이 출력에서
    숨으면 사용자가 "잔여 0" 으로 오독한다.
    """
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3600", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)
    artifacts = _seed_legacy_copies(board, root, unharvested=2)
    outside = root / "옛-임시-트리" / "ticket-T-2001.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("등록 슬롯 밖 사본\n", encoding="utf-8")
    with artifacts["ledger"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ticket": "T-2001", "role": "developer", "ordinal": 9,
                                 "copy": str(outside), "harvested_at": None}) + "\n")

    assert board.main(["rounds", "migrate", "--discard-unharvested"]) == 0

    err = capsys.readouterr().err
    assert "등록 슬롯 밖 경로의 파일은 지우지 않는다" in err
    assert outside.as_posix() in err.replace("\\", "/")
    assert outside.is_file(), "소유하지 않은 경로를 지웠다"
    assert not artifacts["ledger"].exists()
    assert not artifacts["legacy_role_dir"].exists()
    assert _round_names(board_dir, "T-3600") == ["01-developer.md"]


@requires_git
def test_migrate_treats_unreadable_ledger_rows_as_unharvested(legacy_board, capsys):
    """판독 불가 행이 있는 장부는 미회수와 같은 축으로 막는다(못 읽는 것을 조용히 지우지 않는다)."""
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3650", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)
    artifacts = _seed_legacy_copies(board, root)
    with artifacts["ledger"].open("a", encoding="utf-8") as handle:
        handle.write("{손상된 행\n")

    assert board.main(["rounds", "migrate"]) == 1

    assert "판독 불가 행 1개" in capsys.readouterr().err
    assert artifacts["ledger"].exists()


# ── 멱등 · dry-run · 판정 ────────────────────────────────────────────────────


@requires_git
def test_migrate_is_idempotent_on_a_converted_board(legacy_board, capsys):
    """변환된 board 에서 재실행하면 변경 0·커밋 0·rc 0 이다."""
    board, board_dir, _root = legacy_board
    _write_legacy_ticket(board_dir, "T-3700", "done", [("developer", "구현 산출.")])
    _seed_growth_ledger(board_dir, ["T-3700"])
    _seed_gitattributes(board_dir)
    _commit_board(board_dir)
    assert board.main(["rounds", "migrate"]) == 0
    capsys.readouterr()
    head = _git(["rev-parse", "HEAD"], board_dir).stdout.strip()
    names = _round_names(board_dir, "T-3700")

    assert board.main(["rounds", "migrate"]) == 0

    assert "변경 없음" in capsys.readouterr().out
    assert _git(["rev-parse", "HEAD"], board_dir).stdout.strip() == head
    assert _round_names(board_dir, "T-3700") == names


@requires_git
def test_migrate_dry_run_writes_nothing_and_names_the_plan(legacy_board, capsys):
    """계획 실행은 티켓·라운드 파일명·삭제 목록만 낸다(쓰기 0)."""
    board, board_dir, root = legacy_board
    path, _contents = _write_legacy_ticket(board_dir, "T-3800", "done", [
        ("developer", "구현 산출."), ("code-reviewer", "리뷰 산출.")])
    growth = _seed_growth_ledger(board_dir, ["T-3800"])
    _commit_board(board_dir)
    artifacts = _seed_legacy_copies(board, root)
    before = path.read_bytes()

    assert board.main(["rounds", "migrate", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "T-3800 (done/): 01-developer.md, 02-code-reviewer.md" in out
    assert "삭제 대상:" in out and ".growth" in out
    assert "[dry-run] 쓰기 0" in out
    assert path.read_bytes() == before
    assert _round_names(board_dir, "T-3800") == []
    assert growth.is_dir() and artifacts["ledger"].exists()


@requires_git
def test_migrate_dry_run_says_the_apply_run_would_stop_on_unharvested(
        legacy_board, capsys):
    """계획 실행은 미회수 때문에 적용이 멈춘다는 사실을 숨기지 않는다(쓰기 0·rc 0)."""
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3850", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)
    _seed_legacy_copies(board, root, unharvested=1)

    assert board.main(["rounds", "migrate", "--dry-run"]) == 0

    err = capsys.readouterr().err
    assert "미회수 1건" in err and "rc 1 로 멈춘다" in err


@requires_git
def test_migrate_reports_unconvertible_tickets_and_leaves_them_alone(
        legacy_board, capsys):
    """문법이 깨진 marker 는 조용히 건너뛰지 않는다 — rc 1 + 그 티켓 무변경."""
    board, board_dir, _root = legacy_board
    good, _contents = _write_legacy_ticket(
        board_dir, "T-3900", "done", [("developer", "구현 산출.")])
    broken = board_dir / "tickets" / "done" / "T-3901-legacy.md"
    broken.write_text(
        _ticket_text("T-3901", "done")
        + "\n<!-- pm-ticket-section:start role=developer -->\n## 구현\n산출.\n",
        encoding="utf-8", newline="")
    before = broken.read_bytes()
    _commit_board(board_dir)

    assert board.main(["rounds", "migrate"]) == 1

    err = capsys.readouterr().err
    assert "변환 불가" in err and "T-3901" in err
    assert broken.read_bytes() == before
    assert _round_names(board_dir, "T-3901") == []
    assert _round_names(board_dir, "T-3900") == ["01-developer.md"]
    assert good.read_text(encoding="utf-8").count("pm-ticket-section") == 0


@requires_git
def test_migrate_converts_drafts_without_committing_them(legacy_board):
    """draft 는 변환하되 board-git 에 싣지 않는다(promote 가 그 출하를 소유)."""
    board, board_dir, _root = legacy_board
    _write_legacy_ticket(board_dir, "T-3950", ".drafts", [("architect", "설계 초안.")])
    _write_legacy_ticket(board_dir, "T-3951", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)

    assert board.main(["rounds", "migrate"]) == 0

    assert _round_names(board_dir, "T-3950") == ["01-architect.md"]
    tracked = _git(["ls-files"], board_dir).stdout.splitlines()
    assert "tickets/rounds/T-3951/01-developer.md" in tracked
    assert not any("T-3950" in name for name in tracked), "draft 산출이 커밋됐다"


@requires_git
def test_migrated_external_reviewer_round_keeps_the_engine_refusal_marker(legacy_board):
    """옛 거부 산출의 표식은 라운드로 그대로 옮겨진다 — 판정 표면 제외 근거가 그 줄이다."""
    board, board_dir, _root = legacy_board
    _write_legacy_ticket(board_dir, "T-3960", "done", [
        ("external-reviewer",
         "<!-- pm-review-refused role=external-reviewer -->\n\n거부된 산출."),
    ])
    _commit_board(board_dir)

    assert board.main(["rounds", "migrate"]) == 0

    text = (_rounds_dir(board_dir, "T-3960") / "01-external-reviewer.md").read_text(
        encoding="utf-8")
    assert "<!-- pm-review-refused role=external-reviewer -->" in text


# ── 단위: 절 파서·명세 정리·배포 선언 ────────────────────────────────────────


def test_strip_leaves_one_blank_line_at_the_removal_seam():
    """절을 걷어낸 자리에서 빈 줄이 겹치지 않는다(절 밖 텍스트는 그대로)."""
    board = _load_board()
    block, _content = _legacy_section("developer", 0, body="산출.")
    body = f"## 메모\n앞 텍스트.\n\n{block}\n## PM 판정\n뒤 텍스트.\n"
    sections = board._legacy_growth_sections(body)

    stripped = board._strip_legacy_sections(body, sections)

    assert stripped == "## 메모\n앞 텍스트.\n\n## PM 판정\n뒤 텍스트.\n"


def test_strip_removes_orphan_seal_lines():
    """절이 없는 고아 봉인도 명세에 남기지 않는다(변환 뒤 lint red 잔존 방지)."""
    board = _load_board()
    body = ("## 메모\n텍스트.\n"
            f"<!-- pm-ticket-seal role=developer ordinal=7 sha256={_SEAL_SHA} "
            "by=backfill -->\n")

    stripped = board._strip_legacy_sections(body, [])

    assert stripped == "## 메모\n텍스트.\n"


@pytest.mark.parametrize("body", [
    "<!-- pm-ticket-section:start role=developer -->\n## 절\n",
    "<!-- pm-ticket-section:end role=developer -->\n",
    ("<!-- pm-ticket-section:start role=developer -->\n"
     "<!-- pm-ticket-section:start role=architect -->\n## 절\n"
     "<!-- pm-ticket-section:end role=architect -->\n"
     "<!-- pm-ticket-section:end role=developer -->\n"),
    ("<!-- pm-ticket-section:start role=developer -->\n## 절\n"
     "<!-- pm-ticket-section:end role=architect -->\n"),
    "<!-- pm-ticket-section:start role=developer --> 꼬리\n",
])
def test_legacy_section_parser_is_loud_on_broken_syntax(body):
    """손상·중첩·역할 불일치·꼬리가 붙은 marker 는 전부 loud (조용한 부분 변환 금지)."""
    board = _load_board()
    with pytest.raises(ValueError):
        board._legacy_growth_sections(body)


def test_legacy_section_parser_ignores_prose_mentions():
    """산문이 인용한 marker 표기는 데이터가 아니다."""
    board = _load_board()
    body = "## 메모\n`pm-ticket-section:start` 문법은 사라진다.\n"

    assert board._legacy_growth_sections(body) == []


def test_legacy_markers_inside_a_fenced_code_block_are_not_data():
    """``` 로 감싼 marker 예시(문법을 문서화한 티켓)는 변환·lint 판정 모두에서 빠진다.

    문서 티켓이 실 문법을 예시로 펜스에 넣으면, column-0 판정만으로는 그 예시가 진짜 절로
    읽혀 변환 대상이 되거나 lint 가 영영 red 로 남는다(F-003).
    """
    board = _load_board()
    body = (
        "## 문법 예시\n"
        "```\n"
        "<!-- pm-ticket-section:start role=developer -->\n"
        "산출.\n"
        "<!-- pm-ticket-section:end role=developer -->\n"
        "```\n"
    )

    assert board._has_legacy_growth_markers(body) is False
    assert board._legacy_growth_sections(body) == []


def test_growth_union_cleanup_keeps_unrelated_declarations():
    """장부 전용 선언만 걷고 일반 텍스트 선언과 사용자 주석은 남긴다."""
    board = _load_board()
    text = ("# 사용자 주석\n"
            "areas.md merge=union\n"
            "*.jsonl text eol=lf\n"
            "# 티켓 성장 장부 = append-only 권위 기록 — 서로 다른 PM 의 append 가 merge 에서 서로를\n"
            "# 지우지 않도록 같은 union 드라이버로 양쪽 줄을 모두 보존한다.\n"
            "tickets/.growth/*.jsonl merge=union\n")

    assert board._legacy_growth_attr_cleanup(text) == (
        "# 사용자 주석\nareas.md merge=union\n*.jsonl text eol=lf\n")
    assert board._legacy_growth_attr_cleanup(
        "areas.md merge=union\n") is None


def test_shipped_gitattributes_declarations_no_longer_seed_the_growth_ledger():
    """신규 board 는 처음부터 라운드 레이아웃이다 — 배포 선언에 장부 union 이 없다."""
    board = _load_board()
    pm_import_source = (
        REPO / ".project_manager" / "tools" / "pm_import.py"
    ).read_text(encoding="utf-8")

    assert "tickets/.growth" not in board._BOARD_GITATTRIBUTES_BLOCK
    assert "tickets/.growth" not in pm_import_source


# ── dispatch 분류 · 흡수 안내 ────────────────────────────────────────────────


def test_migrate_is_classified_as_a_board_mutation():
    """board 상태를 쓰는 명령이라 worktree 오실행 가드가 걸리는 분류여야 한다."""
    board = _load_board()
    resolved = board._resolved_subcommand(
        board.build_parser().parse_args(["rounds", "migrate", "--dry-run"]))

    assert resolved == "rounds migrate"
    assert resolved in board._MUTATION_SUBCOMMANDS
    assert resolved not in board._READ_SUBCOMMANDS


@requires_git
def test_migrate_warns_when_a_leased_slot_is_not_covered_by_registered_or_repo(
        legacy_board, capsys, monkeypatch):
    """판정은 존재-키(장부 유무)가 아니라 **값 대조**다(T-0845·`_legacy_copy_slot_roots` 주석의
    전환) — 리스 장부의 `leased` 행이 가리키는 슬롯이 등록 슬롯 집합에도 REPO 에도 없을 때만
    경고한다. `_seed_legacy_copies` 의 기본 리스 행은 `state=idle` 이라 `_leased_slot_paths`
    가 애초에 걸러 내므로(옛 존재-판정 시절 기대와 달리 지금은 트리거되지 않는다), 값
    불일치를 실제로 만들려면 `leased` 행 + 등록/REPO 밖 슬롯 경로가 필요하다."""
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3980", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)
    _seed_legacy_copies(board, root)
    monkeypatch.setattr(board, "_registered_slot_paths", lambda pm_home, **_kwargs: ())
    board.LEASES_FILE.write_text(
        json.dumps({"leases": [{"slot": "work/product_1", "repo": "product",
                                "session": "product_1", "state": "leased"}]}),
        encoding="utf-8")

    assert board.main(["rounds", "migrate"]) == 0

    err = capsys.readouterr().err
    assert "등록 슬롯 해소 누락 1건" in err
    assert str(root / "work" / "product_1") in err


@requires_git
def test_migrate_is_silent_when_the_leased_slot_is_covered_by_registered_slots(
        legacy_board, capsys):
    """비-트리거 대조 — 리스 슬롯이 등록 슬롯 집합에 이미 있으면(정상 커버) 경고가 없다."""
    board, board_dir, root = legacy_board
    _write_legacy_ticket(board_dir, "T-3981", "done", [("developer", "구현 산출.")])
    _commit_board(board_dir)
    _seed_legacy_copies(board, root)  # slot="work/product_1" — legacy_board 기본 등록 슬롯과 일치.
    board.LEASES_FILE.write_text(
        json.dumps({"leases": [{"slot": "work/product_1", "repo": "product",
                                "session": "product_1", "state": "leased"}]}),
        encoding="utf-8")

    assert board.main(["rounds", "migrate"]) == 0

    assert "등록 슬롯 해소 누락" not in capsys.readouterr().err


def test_pm_update_hint_matches_the_board_lint_wording():
    """흡수 안내와 lint 판정은 같은 문구여야 한다(두 사본 drift 잠금)."""
    board = _load_board()
    pm_update = _load_pm_update()

    assert pm_update.LEGACY_GROWTH_MIGRATION_HINT == board._LEGACY_GROWTH_MIGRATION_HINT
    assert pm_update.LEGACY_GROWTH_MARKERS == board._LEGACY_GROWTH_MARKERS


def test_pm_update_and_board_lint_share_the_same_column_zero_judgment(
        tmp_path, monkeypatch):
    """인용/들여쓰기 marker 는 두 사본 모두 0건, 진짜 marker 는 둘 다 1건이다(F-001).

    문구 parity(`test_pm_update_hint_matches_the_board_lint_wording`)는 판정 **시야**의
    substring vs column-0 차이를 못 잡는다 — 실 board T-0694 처럼 들여쓰기+backtick 인용된
    marker 를 섞어 두 사본의 **판정 결과**를 대조한다.
    """
    board = _load_board()
    pm_update = _load_pm_update()
    anchor_board_module(board, tmp_path, monkeypatch)
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "done"
    tickets.mkdir(parents=True)

    quoted_body = (
        "## 문법 설명\n"
        "구 형식은 이렇게 생겼다:\n\n"
        "  `<!-- pm-ticket-seal role=developer ordinal=0 "
        f"sha256={_SEAL_SHA} by=harvest -->`\n"
    )
    (tickets / "T-4200-quoted.md").write_text(
        _ticket_text("T-4200", "done") + "\n" + quoted_body,
        encoding="utf-8", newline="")

    real_block, _content = _legacy_section("developer", 0, body="산출.")
    (tickets / "T-4201-real.md").write_text(
        _ticket_text("T-4201", "done") + "\n" + real_block,
        encoding="utf-8", newline="")
    fenced_body = "## 문법 예시\n\n```\n" + real_block + "```\n"
    (tickets / "T-4202-fenced.md").write_text(
        _ticket_text("T-4202", "done") + "\n" + fenced_body,
        encoding="utf-8", newline="")

    board_findings = sorted(
        name for name, _kind, _detail in board.lint_legacy_growth_sections())
    pm_update_found = sorted(
        path.name for path in pm_update._legacy_growth_ticket_files(tmp_path))

    assert board_findings == ["T-4201"], "인용/들여쓰기/펜스 marker 가 board lint 에 잡혔다"
    assert pm_update_found == ["T-4201-real.md"], "인용/들여쓰기/펜스 marker 가 흡수 안내에 잡혔다"


@pytest.mark.parametrize("layout", ["board", "wiki"])
def test_pm_update_finds_legacy_tickets_in_both_board_layouts(tmp_path, layout):
    """분리 board 든 legacy wiki 형상이든 잔존 티켓을 같은 판정으로 찾는다."""
    pm_update = _load_pm_update()
    tickets = tmp_path / ".project_manager" / layout / "tickets" / "done"
    tickets.mkdir(parents=True)
    (tickets / "T-4000-legacy.md").write_text(
        _legacy_ticket_text("T-4000", "done", [("developer", "산출.")])[0],
        encoding="utf-8", newline="")
    (tickets / "T-4001-clean.md").write_text(
        _ticket_text("T-4001", "done"), encoding="utf-8", newline="")

    found = pm_update._legacy_growth_ticket_files(tmp_path)

    assert [path.name for path in found] == ["T-4000-legacy.md"]


def test_pm_update_prints_nothing_for_a_migrated_board(tmp_path, capsys):
    """변환된 board 에서는 안내가 완전히 무출력이다(잡음 0)."""
    pm_update = _load_pm_update()
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "done"
    tickets.mkdir(parents=True)
    (tickets / "T-4100-clean.md").write_text(
        _ticket_text("T-4100", "done"), encoding="utf-8", newline="")

    pm_update._print_legacy_growth_finding(
        pm_update._legacy_growth_ticket_files(tmp_path))

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
