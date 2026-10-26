"""board_root 추종 — board.py 밖 도구들의 board-path 해소 가드 (T-0162 A6·ADR-0033 ①).

A(board.py board_root + pm_config)가 board/ 분리를 board.py 안에서 해소했지만, **board.py 밖**
도구들(ticket_finish·pm_handoff·pm_bootstrap·additional_reviewer)이 board path 를 자체 상수/legacy
별칭으로 굳혀, board/ 분리(B) 후 stale(wiki) 위치를 봐 count 0/미해소가 되던 갭(code-reviewer
포착)을 닫는다. 각 도구가 board 루트(`pm_paths` 의 `Home.board_root`·`areas_file`)를 *추종*하는지를
hermetic 하게 단언한다.

검증 두 축(각 도구):
  - **legacy 무변경**: board/ 부재 → 현 위치(wiki/tickets·.project_manager/areas.md)로 해소
    (기존 회귀 green 보존·상수→함수 전환이 legacy 경로를 안 바꿈).
  - **board/ 존재 추종**: board/tickets·board/areas.md 를 둔 hermetic tree 에서 각 도구가
    board/ 를 본다(wiki stale 안 봄). 이 단언은 *수정 전엔 legacy 상수가 wiki/ 를 가리켜
    fail* 하는 것을 직접 박제한다(stale→fail 실증·아래 docstring 의 '수정 전' 주석).

**hermetic 필수**: 각 도구의 모듈-레벨 `REPO`(import 시점 실 repo 절대경로로 굳음)를 tmp 로
monkeypatch 한 fresh 모듈 인스턴스를 매 테스트마다 로드한다(test_board_root 동류 패턴). 도구의
board-path 해소가 *함수*(또는 None-default 호출 시점 해소)라 monkeypatch 된 tmp REPO 를 추종한다.
"""
from __future__ import annotations
from pm_engine.infra import pm_paths

from pm_engine.cli import bootstrap as bootstrap_cli

from pm_engine.infra import round_store

import importlib.util
from pathlib import Path

import pytest
from pm_engine.infra import areas_store, pm_paths, python_env
from pm_engine.app import slot_resolution
from pm_engine.app import finish_context
from pm_engine.infra import finish_store

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — test_board_root 동일 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_board_dir(root: Path) -> Path:
    """`.project_manager/board/tickets/{open,...}` 를 만들어 board/ 분리 형상을 모사한다."""
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)
    return board_dir


def _make_wiki_tickets(root: Path) -> Path:
    """legacy `.project_manager/wiki/tickets/{open,...}` 를 만든다(stale 후보 위치)."""
    wiki_tk = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (wiki_tk / status).mkdir(parents=True, exist_ok=True)
    return wiki_tk


# ════════════════════════════════════════════════════════════════════════
# pm_handoff — _tickets_dir(board 루트 추종)
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def handoff(tmp_path, monkeypatch):
    mod = _load("pm_handoff")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def test_handoff_tickets_dir_legacy_is_wiki(handoff):
    """legacy(board/ 부재) → _tickets_dir() == wiki/tickets (현 위치·무변경)."""
    assert pm_paths.tickets_dir(pm_paths.home_at(handoff._tmp)) == handoff._tmp / ".project_manager" / "wiki" / "tickets"


def test_handoff_tickets_dir_present_is_board_tickets(handoff):
    """board/ 존재 → _tickets_dir() == board/tickets (board_root 추종).

    *수정 전*: TICKETS_DIR 상수가 wiki/tickets 에 굳어 이 단언이 fail 했다(stale→fail 실증)."""
    _make_board_dir(handoff._tmp)
    assert pm_paths.tickets_dir(pm_paths.home_at(handoff._tmp)) == handoff._tmp / ".project_manager" / "board" / "tickets"


# ════════════════════════════════════════════════════════════════════════
# slot_resolution — _registered_repos / _auto_slot · pm_bootstrap — Bootstrapper._areas_file (pm_paths.areas_file 추종)
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    mod = _load("pm_bootstrap")
    mod._tmp = tmp_path
    return mod


def _write_areas(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "| repo | prefix | git | test_cmd | owner |\n"
        "|---|---|---|---|---|\n"
        "| project_manager | PM | g | pytest | me |\n",
        encoding="utf-8")


def test_bootstrap_registered_repos_reads_board_not_stale_wiki(bootstrap, *, tmp_path):
    """_registered_repos() 가 board/areas.md(진실)를 읽는다 — stale legacy areas 를 안 봄.

    *수정 전*: 기본 인자가 AREAS_FILE(legacy·wiki 밖) 라 board 분리 후 등록 repo 0개로
    오판했다(stale→fail). board/areas.md 에 1 repo, legacy areas.md 엔 *없음* → ['project_manager']."""
    _make_board_dir(tmp_path)
    _write_areas(tmp_path / ".project_manager" / "board" / "areas.md")
    # legacy 위치(.project_manager/areas.md)는 *비어 있음* — stale 위치를 보면 빈 목록이 된다.
    repos = slot_resolution._registered_repos(pm_paths.home_at(tmp_path))
    assert repos == ["project_manager"], \
        f"_registered_repos 가 board/areas.md(진실)를 안 읽음 — stale legacy 봄: {repos}"


def test_bootstrap_registered_repos_legacy_reads_project_manager_areas(bootstrap, *, tmp_path):
    """legacy(board/ 부재) → _registered_repos() 가 .project_manager/areas.md 를 읽는다(현행)."""
    _write_areas(tmp_path / ".project_manager" / "areas.md")
    assert slot_resolution._registered_repos(pm_paths.home_at(tmp_path)) == ["project_manager"]


def test_bootstrap_auto_slot_uses_board_areas_when_separated(bootstrap, *, tmp_path):
    """_auto_slot() 가 board/areas.md + 장부로 단일 self-host 슬롯을 해소한다(추종).

    *수정 전*: AREAS_FILE 기본이 stale legacy 라 등록 repo 0개 → 자동바인딩 미해소(None)."""
    _make_board_dir(tmp_path)
    _write_areas(tmp_path / ".project_manager" / "board" / "areas.md")
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True, exist_ok=True)
    leases.write_text(
        '{"leases": [{"repo": "project_manager", "slot": "work/project_manager_1"}]}',
        encoding="utf-8")
    # leases_file 만 명시(board_root 추종 areas 는 _registered_repos 가 자동해소).
    resolved = slot_resolution._auto_slot(pm_paths.home_at(tmp_path), leases_file=leases)
    assert (resolved.key, resolved.slot) == ("project_manager_1", "work/project_manager_1")


def test_bootstrap_instance_areas_file_follows_board_root(bootstrap, tmp_path):
    """Bootstrapper.__init__ areas_file 미지정 → self._areas_file == board/areas.md (board/ 존재).

    *수정 전*: __init__ 기본이 AREAS_FILE(legacy) 라 인스턴스가 stale 위치를 들고 다녔다."""
    _make_board_dir(tmp_path)
    bs = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path))
    assert bs._areas_file == tmp_path / ".project_manager" / "board" / "areas.md"


def test_bootstrap_instance_areas_file_legacy(bootstrap, tmp_path):
    """legacy → self._areas_file == .project_manager/areas.md (현행 보존)."""
    bs = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path))
    assert bs._areas_file == tmp_path / ".project_manager" / "areas.md"


# ════════════════════════════════════════════════════════════════════════
# additional_reviewer — parse_ticket_touches(_tickets_dir 추종)
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def external(tmp_path, monkeypatch):
    mod = _load("additional_reviewer")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def test_external_tickets_dir_legacy_is_wiki(external):
    """legacy → _tickets_dir() == wiki/tickets (현 위치·무변경)."""
    assert pm_paths.tickets_dir(pm_paths.home_at(external.REPO)) == external._tmp / ".project_manager" / "wiki" / "tickets"


def test_external_tickets_dir_present_is_board_tickets(external):
    """board/ 존재 → _tickets_dir() == board/tickets (board_root 추종).

    *수정 전*: TICKETS_DIR 상수가 wiki/tickets 에 굳어 이 단언이 fail."""
    _make_board_dir(external._tmp)
    assert pm_paths.tickets_dir(pm_paths.home_at(external.REPO)) == external._tmp / ".project_manager" / "board" / "tickets"


def _write_ticket_with_touches(path: Path, tid: str, touches: list[str]) -> None:
    body = "---\nid: %s\ntitle: t\nstatus: open\ntouches:\n" % tid
    body += "".join(f"  - {t}\n" for t in touches)
    body += "---\n\n# %s\n" % tid
    path.write_text(body, encoding="utf-8")


def test_external_parse_touches_reads_board_not_stale_wiki(external):
    """parse_ticket_touches 가 board/tickets 의 ticket touches 를 해소 — stale wiki 안 봄.

    *수정 전*: TICKETS_DIR(wiki) 만 봐 board ticket 을 못 찾아 빈 touches 를 줬다(stale→fail).
    board/tickets 에 ticket(touches 있음), wiki/tickets(stale)엔 *없음* → board 를 봐야 touches 해소."""
    board_dir = _make_board_dir(external._tmp)
    _make_wiki_tickets(external._tmp)  # wiki/tickets 는 비어 있음(stale 위치)
    _write_ticket_with_touches(
        board_dir / "tickets" / "open" / "T-0001-x.md", "T-0001",
        ["src/pay.py", "tests/test_pay.py"])
    touches = round_store.parse_ticket_touches(pm_paths.home_at(external.REPO), "T-0001")
    assert touches == ["src/pay.py", "tests/test_pay.py"], \
        f"board ticket touches 를 board/tickets(진실)에서 안 해소 — stale wiki 봄: {touches}"


def test_external_parse_touches_legacy_reads_wiki(external):
    """legacy(board/ 부재) → parse_ticket_touches 가 wiki/tickets 를 본다(현행 보존)."""
    wiki_tk = _make_wiki_tickets(external._tmp)
    _write_ticket_with_touches(
        wiki_tk / "open" / "T-0001-x.md", "T-0001", ["src/a.py"])
    assert round_store.parse_ticket_touches(pm_paths.home_at(external.REPO), "T-0001") == ["src/a.py"]


# ════════════════════════════════════════════════════════════════════════
# ticket_finish — count_board_done(명시 Home 추종) · _resolve_per_repo_test_cmd
# ════════════════════════════════════════════════════════════════════════
# finish_store.count_board_done(home) 은 명시 Home 의 티켓 디렉터리를 센다. 진실(board/tickets/done=1)과 스테일(wiki/tickets/done=99)을 함께 두어
# 어느 쪽을 세는지 결정적으로 가른다.


def _write_fake_board(tmp_path: Path) -> Path:
    """board/ 분리 홈(진실 board/tickets/done=1·스테일 wiki/tickets/done=99)의 board.py 경로."""
    board_dir = _make_board_dir(tmp_path)
    (board_dir / "tickets" / "done" / "T-0001-real.md").write_text("# real\n", encoding="utf-8")
    wiki_done = _make_wiki_tickets(tmp_path) / "done"
    for i in range(99):
        (wiki_done / f"T-9{i:03d}-stale.md").write_text("# s\n", encoding="utf-8")
    return tmp_path / ".project_manager" / "tools" / "board.py"


@pytest.fixture
def ticket_finish():
    return _load("ticket_finish")


def test_ticket_finish_count_board_done_calls_tickets_dir_not_constant(ticket_finish, tmp_path):
    """count_board_done 가 Home 의 board 루트(`pm_paths.tickets_dir`)를 센다 — 스테일 wiki 아님.

    board/tickets(진실·1)를 세야 한다. 99 가 나오면 회귀."""
    _write_fake_board(tmp_path)
    assert finish_store.count_board_done(pm_paths.home_at(tmp_path)) == 1, \
        "count_board_done 가 board 모듈 tickets_dir()(진실)를 안 부름 — TICKETS_DIR 상수(stale) 봄."


def test_ticket_finish_resolve_test_cmd_uses_board_areas_file(ticket_finish, tmp_path, monkeypatch):
    """_resolve_per_repo_test_cmd 의 areas.md 존재 가드가 Home 의 board 루트를 따른다(board/ 분리).

    board/areas.md 가 실재하면 prefix 행의 test_cmd 가 해소돼야 한다."""
    _make_board_dir(tmp_path)
    (tmp_path / ".project_manager" / "board" / "areas.md").write_text(
        "| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    monkeypatch.setattr(areas_store, "id_prefix", lambda _home, override=None: "PM")
    monkeypatch.setattr(
        areas_store, "_areas_row_for_prefix",
        lambda _home, prefix: {"test_cmd": "go test ./..."} if prefix == "PM" else None)
    assert finish_context._resolve_per_repo_test_cmd(pm_paths.home_at(tmp_path)) == "go test ./..."


def test_ticket_finish_resolve_test_cmd_default_when_board_areas_absent(
        ticket_finish, tmp_path, monkeypatch):
    """Home 의 areas.md 가 없으면 4층 기본 명령 — 솔로 폴백(현행 pytest 보존)."""
    assert finish_context._resolve_per_repo_test_cmd(
        pm_paths.home_at(tmp_path / "nonexistent")) == python_env.default_pytest_cmd()
