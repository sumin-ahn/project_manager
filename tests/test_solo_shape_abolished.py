"""솔로 형상 폐지 가드 — 홈은 장부의 N=1 슬롯 행이고 형상 특례가 없다.

슬롯을 하나만 쓰는 홈도 다른 홈과 같은 장부 행(`slot="."`)으로 자기 정체성을 갖는다. 그
전에는 "lease 행이 없다"가 형상 신호였고, 소비자마다 그 위에 전용 폴백(정체성 유도·회귀/라이브
게이트 cwd·핸드오프 경로·완료 회귀 트리)이 붙어 있었다. 이 파일은 그 층이 **코드에서 사라졌고
행 하나로 대체됐다**는 사실을 네 축으로 못박는다:

  ① 정적 — 지운 심볼이 엔진 소스(canonical + 출하 사본 3타깃)에 0. 문자열 금지가 아니라
     **이름의 부재** 단언이라, 같은 뜻의 새 이름이 아니라 그 층 자체가 돌아오는 것을 잡는다.
  ② 동적 — 실 장부 파일·실 디렉토리로 세운 홈에서 등록/미등록 판정이 값으로 갈린다.
     등록된 홈은 행이 준 값으로 해소되고, 미등록 홈은 조용한 폴백 없이 fail-loud 한다.
  ③ 마이그레이션 — 등록 원시연산이 등록 repo 정확히 1개에서만 발화하고, 재실행이 장부
     byte 를 바꾸지 않는다(멱등).
  ④ sensitivity — 지운 폴백을 소스에 되살린 사본에서는 ②의 미등록 단언이 red 다. 가드가
     공허하지 않다는 증명이다.

**hermetic 필수**: 엔진 모듈의 경로 전역은 import 시점에 실 repo 절대경로로 굳는다 — tmp PM
홈으로 전부 재지정해 실 `.project_manager` 를 건드리지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from _home_slot import HOME_REPO, HOME_SESSION, HOME_SLOT, seed_areas, seed_home_slot
from test_slot_identity_value_axis import _load, _rebind

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
SHIPPED_TOOLS = tuple(
    REPO / "templates" / target / ".project_manager" / "tools"
    for target in ("claude_code", "codex", "opencode")
)

# ① 지운 심볼 → 그 이름이 소유하던 층(재유입 시 무엇이 되살아나는가).
DELETED_SYMBOLS = {
    "single_registration_session": "등록 repo 1개 && 행 0개 → `<repo>_1` 유도(홈 행이 대신한다)",
    "lease_row_count": "행 수를 형상 신호로 읽던 계수기",
    "IDENTITY_SOURCE_LEGACY_PM_STATE": "legacy `wiki/pm_state.md` 로 정체성을 잡던 층",
    "HANDOFF_COLLECTION_ONLY_IDENTITY_SOURCES": "그 층에만 허용하던 수집-only 예외 집합",
    "_has_leased_row": "행 존재 여부로 형상을 가르던 술어",
    "_LG_SOLO": "라이브 게이트 솔로 전용 라벨",
    "_SOLO_GATE_LABEL": "완료 게이트 솔로 전용 라벨",
}

# 스캔이 실제로 파일을 읽었는지의 하한 — 표면이 통째로 사라져 공허 green 이 되는 것을 막는다.
_MIN_SCANNED_FILES = 80


def _engine_sources() -> list[Path]:
    """canonical + 출하 사본의 엔진 `.py` 전수 (스캔 표면)."""
    roots = (TOOLS, *SHIPPED_TOOLS)
    return sorted(path for root in roots for path in root.glob("*.py"))


def _symbol_hits(paths: list[Path], symbols) -> list[str]:
    hits = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        label = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        hits += [f"{label}:{symbol}" for symbol in symbols if symbol in text]
    return sorted(hits)


# ════════════════════════════════════════════════════════════════════════
# ① 정적 — 지운 심볼의 부재
# ════════════════════════════════════════════════════════════════════════


def test_scan_surface_is_non_vacuous():
    """스캔 표면이 canonical + 출하 3타깃 전부를 실제로 읽는다(공허 green 방지)."""
    sources = _engine_sources()
    assert len(sources) >= _MIN_SCANNED_FILES, f"엔진 소스 스캔 표면이 줄었다: {len(sources)}"
    for root in (TOOLS, *SHIPPED_TOOLS):
        assert any(path.is_relative_to(root) for path in sources), f"미스캔 트리: {root}"


def test_deleted_shape_symbols_are_absent_from_engine_sources():
    """형상 추론·솔로 전용 층의 이름이 canonical 과 출하 사본 어디에도 없다."""
    hits = _symbol_hits(_engine_sources(), DELETED_SYMBOLS)
    assert hits == [], (
        "삭제된 형상 층의 이름이 엔진 소스에 되살아났다: "
        + ", ".join(f"{hit} ({DELETED_SYMBOLS[hit.split(':')[-1]]})" for hit in hits)
    )


def test_symbol_scanner_detects_reintroduction(tmp_path):
    """sensitivity — 심볼을 다시 쓴 파일을 스캐너가 잡는다(단언이 공허하지 않다)."""
    revived = tmp_path / "revived.py"
    revived.write_text(
        "def single_registration_session(repos, leases):\n    return None\n",
        encoding="utf-8",
    )
    hits = _symbol_hits([revived], DELETED_SYMBOLS)
    assert len(hits) == 1 and hits[0].endswith(":single_registration_session")


# ════════════════════════════════════════════════════════════════════════
# ② 동적 — 등록된 홈 / 미등록 홈의 소비자 값
# ════════════════════════════════════════════════════════════════════════


class _Home:
    """tmp PM 홈 — 실 장부·실 areas + 경로 전역을 재지정한 엔진 모듈 묶음."""

    def __init__(self, root: Path):
        self.root = root
        self.pm = root / ".project_manager"
        self.ledger = self.pm / ".local" / "worktree-leases.json"
        self.areas = self.pm / "areas.md"
        self.board = _load("board")
        self.ia = _load("identity_args")
        self.bp = _load("pm_bootstrap")
        self.hf = _load("pm_handoff")
        self.lg = _load("pm_log")
        self.wp = _load("worktree_pool")
        for mod in (self.board, self.bp, self.hf, self.lg, self.wp):
            _rebind(mod, root)


@pytest.fixture
def home(tmp_path, monkeypatch) -> _Home:
    """등록 repo 1개인 tmp 홈 — 장부 행은 각 테스트가 깐다."""
    root = tmp_path / "pm-home"
    (root / ".project_manager" / ".local").mkdir(parents=True)
    (root / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    (root / "work").mkdir()
    seed_areas(root)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    monkeypatch.chdir(root)
    return _Home(root)


def test_registered_home_resolves_every_consumer_from_the_row(home):
    """행이 하나 있는 홈 — 정체성·경로·승인 대상값이 전부 그 행에서 온다."""
    seed_home_slot(home.root)

    assert home.board.session_name() == HOME_SESSION
    assert home.board.session_name(required=True) == HOME_SESSION
    assert home.board._active_slot_path() == str(home.root)
    assert home.board._livegate_cwd() == str(home.root)
    assert home.board._regression_cwd() == str(home.root)

    assert home.bp._auto_slot(home.areas, home.ledger).key == HOME_SESSION
    assert home.hf._resolve_state_slot(None, home.areas, home.ledger) == HOME_SESSION
    slot, error = home.hf._resolve_session_worktree_slot(None, home.areas, home.ledger)
    assert (slot, error) == (HOME_SLOT, None)
    assert home.hf._handoff_user_ack_target(None, slot) == (
        HOME_SESSION, f"slot {HOME_SESSION!r}")
    assert home.hf._pm_state_path(None, home.areas, home.ledger, migrate=False) == (
        home.pm / ".local" / "slots" / HOME_SESSION / "pm_state.md")
    assert home.lg.resolve_snapshot_identity(home.root, home.root) == (
        HOME_SESSION, home.lg.IDENTITY_SOURCE_CWD_LEASE)


def test_unregistered_home_fails_loud_instead_of_falling_back(home):
    """행이 0개인 홈 — 등록 repo 가 1개여도 세션을 유도하지 않고 큰소리로 멈춘다."""
    assert not home.ledger.exists()

    assert home.board.session_name() is None
    with pytest.raises(SystemExit) as abort:
        home.board.session_name(required=True)
    assert "세션" in str(abort.value)

    assert home.board._active_slot_path() is None
    with pytest.raises(SystemExit):
        home.board._livegate_cwd()

    assert home.bp._auto_slot(home.areas, home.ledger) is None
    with pytest.raises(home.bp.SlotResolutionError):
        home.bp._resolve_session_slot(home.areas, home.ledger)
    with pytest.raises(home.hf.identity_args.SlotResolutionError):
        home.hf._pm_state_path(None, home.areas, home.ledger, migrate=False)
    slot, error = home.hf._resolve_session_worktree_slot(None, home.areas, home.ledger)
    assert slot is None
    assert home.hf._handoff_user_ack_target(None, slot) == (None, "정체성 미해소 슬롯")
    assert home.lg.resolve_snapshot_identity(home.root, home.root) == (
        None, home.lg.IDENTITY_SOURCE_UNRESOLVED)


def test_unregistered_home_remedy_names_the_migration(home):
    """미등록 안내가 이관 수단(엔진 흡수 1회)을 값으로 알려준다 — 막다른 fail-loud 금지."""
    with pytest.raises(SystemExit) as abort:
        home.board.session_name(required=True)
    assert home.ia.UNREGISTERED_HOME_REMEDY in str(abort.value)


# ════════════════════════════════════════════════════════════════════════
# ③ 마이그레이션 — 등록 원시연산(발화 조건·멱등)
# ════════════════════════════════════════════════════════════════════════


def test_registration_writes_exactly_one_home_row(home):
    """등록 repo 1개 + 행 0개 → 홈 행 1개. 값은 스키마 확장 없이 현행 키만 쓴다."""
    status, detail = home.wp.register_home_slot(board=home.board)
    assert (status, detail) == (home.wp.HOME_SLOT_REGISTERED, HOME_SESSION)

    payload = json.loads(home.ledger.read_text(encoding="utf-8"))
    assert len(payload["leases"]) == 1
    row = payload["leases"][0]
    assert row["slot"] == HOME_SLOT
    assert row["repo"] == HOME_REPO
    assert row["session"] == HOME_SESSION
    assert row["state"] == "leased"
    assert row["bound"] is True
    # `Path(REPO)/"."` 는 홈 자신 — 경로 소비자가 이 행에서 홈을 얻는다.
    assert home.wp.slot_path(HOME_SLOT) == home.root


def test_registration_is_idempotent_byte_for_byte(home):
    """이미 행이 있으면 읽기만 한다 — 재실행이 장부 byte 를 바꾸지 않는다."""
    home.wp.register_home_slot(board=home.board)
    before = home.ledger.read_bytes()

    status, _detail = home.wp.register_home_slot(board=home.board)
    assert status == home.wp.HOME_SLOT_ROWS_PRESENT
    assert home.ledger.read_bytes() == before


def test_registration_skips_when_the_repo_is_ambiguous(home):
    """등록 repo 가 1개가 아니면 무기록 — 어느 repo 의 슬롯인지 지어내지 않는다."""
    seed_areas(home.root, repo="alpha")
    home.areas.write_text(
        home.areas.read_text(encoding="utf-8") + "| beta |  |  |  |  |  |  |  |\n",
        encoding="utf-8")

    status, detail = home.wp.register_home_slot(board=home.board)
    assert status == home.wp.HOME_SLOT_REPOS_NOT_ONE
    assert "alpha" in detail and "beta" in detail
    assert not home.ledger.exists()


def test_registration_preview_does_not_touch_the_ledger(home):
    """`write=False` 는 같은 판정을 내되 아무것도 쓰지 않는다(미리보기)."""
    status, detail = home.wp.register_home_slot(board=home.board, write=False)
    assert (status, detail) == (home.wp.HOME_SLOT_REGISTERED, HOME_SESSION)
    assert not home.ledger.exists()


@pytest.mark.parametrize(
    "status_name,write,expected",
    [
        ("HOME_SLOT_REGISTERED", True, "첫 슬롯 행으로 등록했다"),
        ("HOME_SLOT_REGISTERED", False, "[dry-run]"),
        ("HOME_SLOT_REPOS_NOT_ONE", True, "홈 슬롯 미등록"),
    ],
)
def test_registration_note_is_owned_by_the_judgment(home, status_name, write, expected):
    """안내 문구는 판정 생산자가 소유한다 — 호출부(셋업·흡수)가 다시 쓰지 않는다."""
    note = home.wp.home_slot_registration_note(
        getattr(home.wp, status_name), HOME_SESSION, write=write)
    assert note is not None
    assert expected in note[0]


def test_registration_note_is_silent_when_rows_already_exist(home):
    """이미 등록된 홈은 정상 흐름이라 조용하다(재실행 소음 0)."""
    assert home.wp.home_slot_registration_note(
        home.wp.HOME_SLOT_ROWS_PRESENT, HOME_SLOT) is None


# ════════════════════════════════════════════════════════════════════════
# ④ sensitivity — 지운 폴백을 되살리면 ②가 red
# ════════════════════════════════════════════════════════════════════════


def _board_with_zero_row_derivation(tmp_path: Path, root: Path):
    """`session_name` 의 행-0 유도층을 되살린 board 사본을 로드한다.

    엔진 모듈은 형제 모듈(`identity_args` 등)을 자기 파일 위치에서 찾으므로 board 한 파일만
    복사하면 로드가 깨진다 — 도구 트리 전체를 복사한 뒤 그 사본의 board 만 되돌린다.
    """
    import importlib.util
    import shutil

    tools = tmp_path / "revived-tools"
    shutil.copytree(TOOLS, tools, ignore=shutil.ignore_patterns("__pycache__"))
    target = tools / "board.py"
    source = target.read_text(encoding="utf-8")
    anchor = "    # leased 0(미등록) 또는 ≥2(모호) → 미해소. 유도 폴백 없음.\n"
    assert source.count(anchor) == 1, "sensitivity 앵커가 board.py 에서 사라졌다"
    target.write_text(
        source.replace(
            anchor,
            anchor + "    if not leased:\n"
                     "        repos = sorted(registered_repos())\n"
                     "        if len(repos) == 1:\n"
                     "            return f'{repos[0]}_1'\n",
            1,
        ),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("board_revived", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _rebind(mod, root)
    return mod


def test_reviving_the_zero_row_derivation_turns_the_guard_red(home, tmp_path):
    """행 0 유도층을 되살린 사본에서는 미등록 홈이 조용히 해소된다 — ②가 잡는 회귀다."""
    revived = _board_with_zero_row_derivation(tmp_path, home.root)

    assert not home.ledger.exists()
    assert revived.session_name() == HOME_SESSION      # 되살아난 유도(=회귀 형상)
    assert home.board.session_name() is None           # 현행 엔진은 미해소


# ════════════════════════════════════════════════════════════════════════
# ③-b 마이그레이션 배선 — 셋업 파사드 / 엔진 흡수 말미
# ════════════════════════════════════════════════════════════════════════


def _adopter_tree(tmp_path: Path, *, repo: str = HOME_REPO) -> Path:
    """실 채택자 홈 — 엔진 사본 + areas 등록 1행, 장부는 없음(등록 전 형상)."""
    import shutil

    dest = tmp_path / "adopter"
    (dest / ".project_manager" / ".local").mkdir(parents=True)
    shutil.copytree(TOOLS, dest / ".project_manager" / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"))
    seed_areas(dest, repo=repo)
    return dest


def _load_from(dest: Path, name: str):
    """채택자 트리의 엔진 모듈을 그 트리 앵커로 로드한다(경로 재지정 불요)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"{name}_adopter", dest / ".project_manager" / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_setup_facade_registers_the_home(tmp_path, capsys):
    """셋업 파사드가 등록을 수행하고 결과를 사람이 읽는 한 줄로 알린다."""
    dest = _adopter_tree(tmp_path)
    ledger = dest / ".project_manager" / ".local" / "worktree-leases.json"

    status = _load_from(dest, "pm_config").register_home_slot()

    assert status == "registered"
    assert json.loads(ledger.read_text(encoding="utf-8"))["leases"][0]["session"] == \
        HOME_SESSION
    assert "첫 슬롯 행으로 등록했다" in capsys.readouterr().out


def test_engine_absorb_migrates_the_home_once(tmp_path, capsys):
    """엔진 흡수 말미가 행 0 홈을 1회 이행하고, 재실행이 장부 byte 를 바꾸지 않는다."""
    dest = _adopter_tree(tmp_path)
    ledger = dest / ".project_manager" / ".local" / "worktree-leases.json"
    pm_update = _load("pm_update")

    assert pm_update.register_home_slot(dest, write=True) == "registered"
    after_first = ledger.read_bytes()

    assert pm_update.register_home_slot(dest, write=True) == "rows-present"
    assert ledger.read_bytes() == after_first
    capsys.readouterr()


def test_engine_absorb_dry_run_reports_without_writing(tmp_path, capsys):
    """dry-run 흡수는 같은 판정을 내되 장부를 만들지 않는다."""
    dest = _adopter_tree(tmp_path)
    ledger = dest / ".project_manager" / ".local" / "worktree-leases.json"

    assert _load("pm_update").register_home_slot(dest, write=False) == "registered"

    assert not ledger.exists()
    assert "[dry-run]" in capsys.readouterr().out


def test_engine_absorb_leaves_a_pool_home_untouched(tmp_path, capsys):
    """행이 이미 있는 풀 홈은 흡수가 건드리지 않는다(무기록·조용)."""
    dest = _adopter_tree(tmp_path)
    ledger = seed_home_slot(dest)
    before = ledger.read_bytes()

    assert _load("pm_update").register_home_slot(dest, write=True) == "rows-present"

    assert ledger.read_bytes() == before
    assert capsys.readouterr().out == ""
