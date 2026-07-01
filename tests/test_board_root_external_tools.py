"""board_root 추종 — board.py 밖 도구들의 board-path 해소 가드 (T-0162 A6·ADR-0033 ①).

A(board.py board_root + pm_config)가 board/ 분리를 board.py 안에서 해소했지만, **board.py 밖**
도구들(ticket_finish·pm_handoff·pm_bootstrap·external_review)이 board path 를 자체 상수/legacy
별칭으로 굳혀, board/ 분리(B) 후 stale(wiki) 위치를 봐 count 0/미해소가 되던 갭(code-reviewer
포착)을 닫는다. 각 도구가 board_root() 를 *추종*하는지 — board import 도구는 함수 호출, 안 하는
도구는 board.py 동형 자체 해소 — 를 hermetic 하게 단언한다.

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

import importlib.util
from pathlib import Path

import pytest

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
# pm_handoff — _tickets_dir(board_root 추종) · _areas_file / _regression_cwd(_areas_file 추종)
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def handoff(tmp_path, monkeypatch):
    mod = _load("pm_handoff")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def test_handoff_tickets_dir_legacy_is_wiki(handoff):
    """legacy(board/ 부재) → _tickets_dir() == wiki/tickets (현 위치·무변경)."""
    assert handoff._tickets_dir() == handoff._tmp / ".project_manager" / "wiki" / "tickets"


def test_handoff_areas_file_legacy_is_project_manager_areas(handoff):
    """legacy → _areas_file() == .project_manager/areas.md (wiki *밖*·현 위치)."""
    assert handoff._areas_file() == handoff._tmp / ".project_manager" / "areas.md"


def test_handoff_tickets_dir_present_is_board_tickets(handoff):
    """board/ 존재 → _tickets_dir() == board/tickets (board_root 추종).

    *수정 전*: TICKETS_DIR 상수가 wiki/tickets 에 굳어 이 단언이 fail 했다(stale→fail 실증)."""
    _make_board_dir(handoff._tmp)
    assert handoff._tickets_dir() == handoff._tmp / ".project_manager" / "board" / "tickets"


def test_handoff_areas_file_present_moves_inside_board(handoff):
    """board/ 존재 → _areas_file() == board/areas.md (submodule 안·조건분기·legacy 와 다름)."""
    _make_board_dir(handoff._tmp)
    assert handoff._areas_file() == handoff._tmp / ".project_manager" / "board" / "areas.md"


# ════════════════════════════════════════════════════════════════════════
# pm_bootstrap — _registered_repos / _auto_slot / Bootstrapper._areas_file (_areas_file 추종)
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    mod = _load("pm_bootstrap")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def test_bootstrap_areas_file_legacy_is_project_manager_areas(bootstrap):
    """legacy → _areas_file() == .project_manager/areas.md (현 위치·무변경)."""
    assert bootstrap._areas_file() == bootstrap._tmp / ".project_manager" / "areas.md"


def test_bootstrap_areas_file_present_moves_inside_board(bootstrap):
    """board/ 존재 → _areas_file() == board/areas.md (board_root 추종).

    *수정 전*: AREAS_FILE 상수가 .project_manager/areas.md 에 굳어 이 단언이 fail."""
    _make_board_dir(bootstrap._tmp)
    assert bootstrap._areas_file() == bootstrap._tmp / ".project_manager" / "board" / "areas.md"


def _write_areas(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "| repo | prefix | git | test_cmd | owner |\n"
        "|---|---|---|---|---|\n"
        "| project_manager | PM | g | pytest | me |\n",
        encoding="utf-8")


def test_bootstrap_registered_repos_reads_board_not_stale_wiki(bootstrap):
    """_registered_repos() 가 board/areas.md(진실)를 읽는다 — stale legacy areas 를 안 봄.

    *수정 전*: 기본 인자가 AREAS_FILE(legacy·wiki 밖) 라 board 분리 후 등록 repo 0개로
    오판했다(stale→fail). board/areas.md 에 1 repo, legacy areas.md 엔 *없음* → ['project_manager']."""
    _make_board_dir(bootstrap._tmp)
    _write_areas(bootstrap._tmp / ".project_manager" / "board" / "areas.md")
    # legacy 위치(.project_manager/areas.md)는 *비어 있음* — stale 위치를 보면 빈 목록이 된다.
    repos = bootstrap._registered_repos()
    assert repos == ["project_manager"], \
        f"_registered_repos 가 board/areas.md(진실)를 안 읽음 — stale legacy 봄: {repos}"


def test_bootstrap_registered_repos_legacy_reads_project_manager_areas(bootstrap):
    """legacy(board/ 부재) → _registered_repos() 가 .project_manager/areas.md 를 읽는다(현행)."""
    _write_areas(bootstrap._tmp / ".project_manager" / "areas.md")
    assert bootstrap._registered_repos() == ["project_manager"]


def test_bootstrap_auto_slot_uses_board_areas_when_separated(bootstrap):
    """_auto_slot() 가 board/areas.md + 장부로 단일 self-host 슬롯을 해소한다(추종).

    *수정 전*: AREAS_FILE 기본이 stale legacy 라 등록 repo 0개 → 자동바인딩 미해소(None)."""
    _make_board_dir(bootstrap._tmp)
    _write_areas(bootstrap._tmp / ".project_manager" / "board" / "areas.md")
    leases = bootstrap._tmp / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True, exist_ok=True)
    leases.write_text(
        '{"leases": [{"repo": "project_manager", "slot": "work/project_manager_1"}]}',
        encoding="utf-8")
    # leases_file 만 명시(board_root 추종 areas 는 _registered_repos 가 자동해소).
    assert bootstrap._auto_slot(leases_file=leases) == ("project_manager", 1)


def test_bootstrap_instance_areas_file_follows_board_root(bootstrap):
    """Bootstrapper.__init__ areas_file 미지정 → self._areas_file == board/areas.md (board/ 존재).

    *수정 전*: __init__ 기본이 AREAS_FILE(legacy) 라 인스턴스가 stale 위치를 들고 다녔다."""
    _make_board_dir(bootstrap._tmp)
    bs = bootstrap.PmBootstrap()
    assert bs._areas_file == bootstrap._tmp / ".project_manager" / "board" / "areas.md"


def test_bootstrap_instance_areas_file_legacy(bootstrap):
    """legacy → self._areas_file == .project_manager/areas.md (현행 보존)."""
    bs = bootstrap.PmBootstrap()
    assert bs._areas_file == bootstrap._tmp / ".project_manager" / "areas.md"


# ════════════════════════════════════════════════════════════════════════
# external_review — parse_ticket_touches(_tickets_dir 추종)
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def external(tmp_path, monkeypatch):
    mod = _load("external_review")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def test_external_tickets_dir_legacy_is_wiki(external):
    """legacy → _tickets_dir() == wiki/tickets (현 위치·무변경)."""
    assert external._tickets_dir() == external._tmp / ".project_manager" / "wiki" / "tickets"


def test_external_tickets_dir_present_is_board_tickets(external):
    """board/ 존재 → _tickets_dir() == board/tickets (board_root 추종).

    *수정 전*: TICKETS_DIR 상수가 wiki/tickets 에 굳어 이 단언이 fail."""
    _make_board_dir(external._tmp)
    assert external._tickets_dir() == external._tmp / ".project_manager" / "board" / "tickets"


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
    touches = external.parse_ticket_touches("T-0001")
    assert touches == ["src/pay.py", "tests/test_pay.py"], \
        f"board ticket touches 를 board/tickets(진실)에서 안 해소 — stale wiki 봄: {touches}"


def test_external_parse_touches_legacy_reads_wiki(external):
    """legacy(board/ 부재) → parse_ticket_touches 가 wiki/tickets 를 본다(현행 보존)."""
    wiki_tk = _make_wiki_tickets(external._tmp)
    _write_ticket_with_touches(
        wiki_tk / "open" / "T-0001-x.md", "T-0001", ["src/a.py"])
    assert external.parse_ticket_touches("T-0001") == ["src/a.py"]


# ════════════════════════════════════════════════════════════════════════
# ticket_finish — count_board_done(mod.tickets_dir() 추종) · _resolve_per_repo_test_cmd
# ════════════════════════════════════════════════════════════════════════
# count_board_done(board_py) 는 *주어진 board.py 를 동적 로드* 해 그 모듈의 board-path 를 쓴다.
# 그러므로 board_root 추종 검증은 "board 모듈이 board_root 를 따르고, ticket_finish 가 그 모듈의
# legacy 상수(TICKETS_DIR)가 아니라 *함수*(tickets_dir())를 호출함" 을 박제하면 된다. 진실/스테일이
# 다른 경로를 가리키는 미니 fake board.py 를 tmp 에 만들어, ticket_finish 가 어느 쪽을 부르는지
# 결정적으로 가른다(수정 전엔 TICKETS_DIR 를 불러 stale, 수정 후엔 tickets_dir() 로 진실).

_FAKE_BOARD_PY = '''\
from pathlib import Path
_BASE = Path(__file__).resolve().parent
# 진실 = board_dir, 스테일 = wiki_dir — 둘이 *다른* done 디렉토리를 가리킨다.
def tickets_dir():
    return _BASE / "board_dir"        # board_root 추종 결과(진실)
TICKETS_DIR = _BASE / "wiki_dir"      # legacy 상수(스테일) — 부르면 잘못된 done 을 센다
'''


def _write_fake_board(tmp_path: Path) -> Path:
    """진실(board_dir/done=1)과 스테일(wiki_dir/done=99)을 가르는 미니 board.py 를 만든다."""
    bp = tmp_path / "fake_board.py"
    bp.write_text(_FAKE_BOARD_PY, encoding="utf-8")
    (tmp_path / "board_dir" / "done").mkdir(parents=True, exist_ok=True)
    (tmp_path / "board_dir" / "done" / "T-0001-real.md").write_text("# real\n", encoding="utf-8")
    (tmp_path / "wiki_dir" / "done").mkdir(parents=True, exist_ok=True)
    for i in range(99):
        (tmp_path / "wiki_dir" / "done" / f"T-9{i:03d}-stale.md").write_text("# s\n", encoding="utf-8")
    return bp


@pytest.fixture
def ticket_finish():
    return _load("ticket_finish")


def test_ticket_finish_count_board_done_calls_tickets_dir_not_constant(ticket_finish, tmp_path):
    """count_board_done 가 board 모듈의 tickets_dir()(함수)를 부른다 — TICKETS_DIR 상수 아님.

    *수정 전*: `mod.TICKETS_DIR / "done"` 을 셌다 → wiki_dir(스테일·99) 로 드러났다(stale→fail).
    *수정 후*: `mod.tickets_dir() / "done"` → board_dir(진실·1). 99 가 나오면 회귀."""
    bp = _write_fake_board(tmp_path)
    assert ticket_finish.count_board_done(bp) == 1, \
        "count_board_done 가 board 모듈 tickets_dir()(진실)를 안 부름 — TICKETS_DIR 상수(stale) 봄."


def test_ticket_finish_count_board_done_failsoft_on_broken_board(ticket_finish, tmp_path):
    """board.py 로드/속성 실패 → -1 (fail-soft·현행 보존 — tickets_dir() 부재여도 graceful)."""
    bp = tmp_path / "broken_board.py"
    bp.write_text("def tickets_dir():\n    raise RuntimeError('boom')\n", encoding="utf-8")
    assert ticket_finish.count_board_done(bp) == -1


def test_ticket_finish_resolve_test_cmd_uses_board_areas_file(ticket_finish, tmp_path):
    """_resolve_per_repo_test_cmd 가 board 모듈의 areas_file().exists() 가드를 쓴다(board_root 추종).

    *수정 전*: 자체 상수 AREAS_FILE(legacy) 존재 가드라 board/ 분리 후 stale 위치를 봤다.
    fake board 의 areas_file() 가 실재 areas 를 가리키면 test_cmd 가 해소돼야 한다."""
    areas = tmp_path / "board" / "areas.md"
    areas.parent.mkdir(parents=True, exist_ok=True)
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")

    class _FakeBoard:
        def areas_file(self):
            return areas

        def id_prefix(self):
            return "PM"

        def _areas_row_for_prefix(self, prefix):
            return {"test_cmd": "go test ./..."} if prefix == "PM" else None

    import unittest.mock as _mock
    with _mock.patch.object(ticket_finish, "_load_board_module", lambda: _FakeBoard()):
        assert ticket_finish._resolve_per_repo_test_cmd() == "go test ./..."


def test_ticket_finish_resolve_test_cmd_none_when_board_areas_absent(ticket_finish, tmp_path):
    """board 모듈 areas_file() 가 부재 경로면 None — 솔로 폴백(현행 pytest argv 보존)."""
    class _FakeBoard:
        def areas_file(self):
            return tmp_path / "nonexistent" / "areas.md"

    import unittest.mock as _mock
    with _mock.patch.object(ticket_finish, "_load_board_module", lambda: _FakeBoard()):
        assert ticket_finish._resolve_per_repo_test_cmd() is None
