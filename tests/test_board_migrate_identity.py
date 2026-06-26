"""`board migrate-identity` backfill 마이그레이션 단위 테스트 (T-0168·ADR-0033 업그레이드 경로).

ADR-0033 이전 데이터(areas 빈 `area_owner`·ticket 부재 `created_by`·슬롯-only `claimed_by`)를
일회성 backfill 한다. 검증하는 계약:
  1. **areas backfill** — 빈 area_owner → user(표/주석 verbatim 보존).
  2. **ticket backfill** — 부재 created_by → user · 슬롯-only claimed_by → `<user>/<slot>`.
  3. **멱등** — 기존 non-empty 값 불변·재실행 no-op.
  4. **비파괴** — frontmatter 키 순서·body 보존.
  5. **dry-run** — 쓰기 0(per-file 보고만).
  6. **--scope** — active(open+claimed) vs all(done 포함) 경계.
  7. **abort** — 식별자 미해소 시 rc≠0·쓰기 0.

**hermetic**: board.py 경로 전역을 tmp 프로젝트로 monkeypatch·git 폴백 stub
(test_board_mine_view.py 패턴 동류).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_project(root: Path) -> None:
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def board(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load_board()
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": pm / ".local",
        "BOARD_LOCK": pm / ".local" / "board.lock",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    return mod


def _write_conf(board, **kv) -> None:
    board.LOCAL_CONF.write_text(
        "".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")


def _seed(board, tid, status, *, fm_extra=None, body="# seed\n"):
    """status 디렉토리에 raw 텍스트로 티켓을 박는다(키 순서 제어를 위해 직접 작성)."""
    fm_extra = fm_extra or {}
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    fm = {"id": tid, "title": "t", "status": status}
    fm.update(fm_extra)
    board.dump_ticket(path, fm, body)
    return path


def _run(board, capsys, **flags):
    args = argparse.Namespace(
        user=flags.get("user"), session=flags.get("session"),
        dry_run=flags.get("dry_run", False), scope=flags.get("scope", "all"))
    rc = board.cmd_migrate_identity(args)
    out = capsys.readouterr()
    return rc, out.out, out.err


_AREAS_EMPTY_OWNER = (
    "# Area Registry\n\n"
    "> per-repo 레지스트리 (주석은 보존돼야 한다).\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| service-a | PAY | g:a | pytest -q | reg | develop | main |  |\n"
    "| service-b | ACC | g:b | pytest -q | reg | develop | main | bob |\n"
)


# ════════════════════════════════════════════════════════════════════════
# abort — 식별자 미해소
# ════════════════════════════════════════════════════════════════════════

def test_abort_when_user_unresolved(board, capsys):
    """user 미해소(local.conf user= 없음·git 폴백 None)면 rc≠0·쓰기 0."""
    _write_conf(board, session="pm-1")  # user 키 없음
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    p = _seed(board, "T-0001", "open")
    before_areas = board.AREAS_FILE.read_text(encoding="utf-8")
    before_ticket = p.read_text(encoding="utf-8")
    rc, out, err = _run(board, capsys)
    assert rc == 1
    assert "user 식별자 미해소" in err
    # 쓰기 0 — abort 는 어떤 파일도 건드리지 않는다.
    assert board.AREAS_FILE.read_text(encoding="utf-8") == before_areas
    assert p.read_text(encoding="utf-8") == before_ticket


def test_user_override_unblocks_abort(board, capsys):
    """--user override 면 user 미해소 abort 를 우회하고 그 값으로 backfill."""
    _write_conf(board, session="pm-1")
    _seed(board, "T-0001", "open")
    rc, out, _ = _run(board, capsys, user="alice")
    assert rc == 0
    fm, _body = board.load_ticket(board.TICKETS_DIR / "open" / "T-0001-seed.md")
    assert fm["created_by"] == "alice"


# ════════════════════════════════════════════════════════════════════════
# areas backfill — 빈 area_owner → user (표/주석 보존)
# ════════════════════════════════════════════════════════════════════════

def test_areas_empty_owner_backfilled(board, capsys):
    """빈 area_owner 행(PAY)만 user 로 채우고 이미 채워진 행(ACC→bob)은 불변."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    # PAY 행 area_owner 채워짐.
    assert board._ticket_area_owner("T-PAY-001") == "alice"
    # ACC 행은 기존 bob 보존(멱등).
    assert board._ticket_area_owner("T-ACC-001") == "bob"
    # 주석·헤더 verbatim 보존.
    assert "주석은 보존돼야 한다" in text
    assert text.startswith("# Area Registry\n")


def test_areas_no_registry_noop(board, capsys):
    """areas.md 부재(솔로)면 areas 단계는 no-op(에러 없음)."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0001", "open")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    assert not board.AREAS_FILE.exists()


# ════════════════════════════════════════════════════════════════════════
# ticket backfill — created_by · slot-only claimed_by
# ════════════════════════════════════════════════════════════════════════

def test_ticket_created_by_backfilled_when_absent(board, capsys):
    """부재(키 없음) created_by → user."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0001", "open")  # created_by 없음
    _run(board, capsys)
    fm, _ = board.load_ticket(board.TICKETS_DIR / "open" / "T-0001-seed.md")
    assert fm["created_by"] == "alice"


def test_ticket_created_by_null_backfilled(board, capsys):
    """None(YAML null) created_by → user (빈 값도 부재 취급)."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0001", "open", fm_extra={"created_by": None})
    _run(board, capsys)
    fm, _ = board.load_ticket(board.TICKETS_DIR / "open" / "T-0001-seed.md")
    assert fm["created_by"] == "alice"


def test_slot_only_claimed_by_converted(board, capsys):
    """슬롯-only claimed_by(`pm-1`·`/` 없음) → `<user>/pm-1` (슬롯값 보존·user prepend)."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0005", "claimed",
          fm_extra={"created_by": "alice", "claimed_by": "pm-1"})
    _run(board, capsys)
    fm, _ = board.load_ticket(board.TICKETS_DIR / "claimed" / "T-0005-seed.md")
    assert fm["claimed_by"] == "alice/pm-1"


def test_existing_user_slot_claimed_by_unchanged(board, capsys):
    """이미 `<user>/<slot>` 형태인 claimed_by 는 불변(멱등·기존값 보존)."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0005", "claimed",
          fm_extra={"created_by": "bob", "claimed_by": "bob/pm-2"})
    _run(board, capsys)
    fm, _ = board.load_ticket(board.TICKETS_DIR / "claimed" / "T-0005-seed.md")
    assert fm["claimed_by"] == "bob/pm-2"   # user 차원 이미 있음 → 불변
    assert fm["created_by"] == "bob"        # 기존 created_by 보존


# ════════════════════════════════════════════════════════════════════════
# 멱등 — 재실행 no-op
# ════════════════════════════════════════════════════════════════════════

def test_idempotent_rerun_noop(board, capsys):
    """1회 적용 후 재실행 = 변경 0(파일 bytes 불변)."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    p = _seed(board, "T-0005", "claimed", fm_extra={"claimed_by": "pm-1"})
    _run(board, capsys)
    after1_areas = board.AREAS_FILE.read_text(encoding="utf-8")
    after1_ticket = p.read_text(encoding="utf-8")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    assert "변경 없음" in out
    # 재실행은 bytes 불변.
    assert board.AREAS_FILE.read_text(encoding="utf-8") == after1_areas
    assert p.read_text(encoding="utf-8") == after1_ticket


# ════════════════════════════════════════════════════════════════════════
# 비파괴 — frontmatter 키 순서·body 보존
# ════════════════════════════════════════════════════════════════════════

def test_nondestructive_preserves_key_order_and_body(board, capsys):
    """기존 키 순서·body 텍스트 보존 — created_by 만 추가(끝에 append)."""
    _write_conf(board, user="alice", session="pm-1")
    body = "# 본문\n\n임의의 마크다운 본문 — 보존돼야 한다.\n\n## 섹션\n- 항목\n"
    _seed(board, "T-0001", "open",
          fm_extra={"depends_on": ["T-0000"], "tags": ["x"]}, body=body)
    _run(board, capsys)
    text = (board.TICKETS_DIR / "open" / "T-0001-seed.md").read_text(encoding="utf-8")
    # body verbatim 보존.
    assert body in text
    # 기존 키들이 created_by 보다 앞(순서 보존) — id·title·status·depends_on·tags 먼저.
    fm, _ = board.load_ticket(board.TICKETS_DIR / "open" / "T-0001-seed.md")
    keys = list(fm.keys())
    assert keys.index("id") < keys.index("created_by")
    assert keys.index("tags") < keys.index("created_by")
    assert fm["depends_on"] == ["T-0000"]  # 기존 값 불변


# ════════════════════════════════════════════════════════════════════════
# dry-run — 쓰기 0
# ════════════════════════════════════════════════════════════════════════

def test_dry_run_no_writes(board, capsys):
    """--dry-run 은 어떤 파일도 쓰지 않는다(per-file 보고만)."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    p = _seed(board, "T-0005", "claimed", fm_extra={"claimed_by": "pm-1"})
    before_areas = board.AREAS_FILE.read_text(encoding="utf-8")
    before_ticket = p.read_text(encoding="utf-8")
    rc, out, _ = _run(board, capsys, dry_run=True)
    assert rc == 0
    assert "[dry-run]" in out
    # 변경 내용은 보고되지만 파일은 불변.
    assert "area PAY: area_owner → alice" in out
    assert "claimed_by pm-1 → alice/pm-1" in out
    assert board.AREAS_FILE.read_text(encoding="utf-8") == before_areas
    assert p.read_text(encoding="utf-8") == before_ticket


# ════════════════════════════════════════════════════════════════════════
# --scope 경계 — active(open+claimed) vs all(done 포함)
# ════════════════════════════════════════════════════════════════════════

def test_scope_active_skips_done(board, capsys):
    """--scope active 면 done 티켓은 건드리지 않는다(open+claimed 만)."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0001", "open")
    done_p = _seed(board, "T-0009", "done")
    before_done = done_p.read_text(encoding="utf-8")
    _run(board, capsys, scope="active")
    open_fm, _ = board.load_ticket(board.TICKETS_DIR / "open" / "T-0001-seed.md")
    assert open_fm["created_by"] == "alice"      # open 은 채워짐
    assert done_p.read_text(encoding="utf-8") == before_done  # done 불변


def test_scope_all_includes_done(board, capsys):
    """--scope all(기본) 은 done 티켓도 backfill."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0009", "done")
    _run(board, capsys, scope="all")
    fm, _ = board.load_ticket(board.TICKETS_DIR / "done" / "T-0009-seed.md")
    assert fm["created_by"] == "alice"


# ════════════════════════════════════════════════════════════════════════
# 구-헤더 업그레이드 (T-0168 must-fix) — area_owner 칼럼이 없는 ADR-0033 이전 스키마
# ════════════════════════════════════════════════════════════════════════
#
# ADR-0033 이전 areas.md 는 area_owner 칼럼 *자체*가 없다(7/6/5칼럼). 이전엔 헤더에 채울
# 자리가 없어 no-op → `_area_owner_in_use()` 가 영구 False → `--mine` 이 전체-open 으로 degrade.
# migrate 가 헤더를 canonical 8칼럼으로 업그레이드해 area_owner 를 표면화해야 한다.

# 7칼럼 (protected 스키마·T-0076·area_owner 부재).
_AREAS_OLD_7COL = (
    "# Area Registry\n\n"
    "> per-repo 레지스트리 (주석은 보존돼야 한다).\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected |\n"
    "|---|---|---|---|---|---|---|\n"
    "| service-a | PAY | g:a | pytest -q | reg | develop | main |\n"
    "| service-b | ACC | g:b | pytest -q | reg | develop | main |\n"
)

# 6칼럼 (base 스키마·T-0075·protected/area_owner 부재).
_AREAS_OLD_6COL = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base |\n"
    "|---|---|---|---|---|---|\n"
    "| service-a | PAY | g:a | pytest -q | reg | develop |\n"
)

# 5칼럼 (per-repo 레지스트리·ADR-0014·base/protected/area_owner 부재).
_AREAS_OLD_5COL = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner |\n"
    "|---|---|---|---|---|\n"
    "| service-a | PAY | g:a | pytest -q | reg |\n"
)


def test_old_7col_header_upgraded_to_canonical_8col(board, capsys):
    """구 7칼럼(area_owner 부재) → canonical 8칼럼 헤더 업그레이드 + area_owner 채움.

    수정 전엔 헤더에 area_owner 칼럼이 없어 no-op → `_area_owner_in_use()` False → `--mine`
    전체-open degrade. 수정 후엔 헤더가 canonical 8칼럼이 되고 area_owner=user 로 채워진다.
    """
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_OLD_7COL, encoding="utf-8")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    # 헤더가 canonical 8칼럼으로 업그레이드됨.
    assert "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |" in text
    # area_owner 가 채워짐 → `--mine` (a) 풀 입력이 작동.
    assert board._ticket_area_owner("T-PAY-001") == "alice"
    assert board._ticket_area_owner("T-ACC-001") == "alice"
    # 전역 플래그가 True 로 전환(수정 전엔 영구 False).
    assert board._area_owner_in_use() is True


def test_old_7col_mine_narrows_after_migrate(board, capsys):
    """구 7칼럼 migrate 후 `--mine` 이 *좁힌다*(내 area open 만) — 전체-open degrade 해소.

    migrate 전: area_owner 미운영 → `--mine` (a)=전체 open. migrate 후: 내(alice) area 만.
    bob 소유 area(다른 user)의 open 은 `--mine` 에서 빠진다.
    """
    # PAY=alice 가 migrate 로 채워짐 · ACC 는 미리 bob 소유로 박아 둠(타 user).
    areas = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected |\n"
        "|---|---|---|---|---|---|---|\n"
        "| service-a | PAY | g:a | pytest -q | reg | develop | main |\n"
    )
    board.AREAS_FILE.write_text(areas, encoding="utf-8")
    _write_conf(board, user="alice", session="pm-1")
    # migrate 전: area_owner 운영 안 됨 → in_use False.
    assert board._area_owner_in_use() is False
    _run(board, capsys)
    # migrate 후: PAY area_owner=alice → in_use True.
    assert board._area_owner_in_use() is True
    # 이후 ACC 를 bob 소유로 추가(타 user area) — alice `--mine` 에서 빠져야 함.
    board.AREAS_FILE.write_text(
        board.AREAS_FILE.read_text(encoding="utf-8")
        + "| service-b | ACC | g:b | pytest -q | reg | develop | main | bob |\n",
        encoding="utf-8")
    my_pay = board._ticket_is_mine(
        "open", {"id": "T-PAY-009", "claimed_by": ""}, "alice", "pm-1",
        board._area_owner_in_use())
    my_acc = board._ticket_is_mine(
        "open", {"id": "T-ACC-009", "claimed_by": ""}, "alice", "pm-1",
        board._area_owner_in_use())
    assert my_pay is True    # 내 area open → 포함.
    assert my_acc is False   # bob area open → 제외(좁혀짐).


def test_old_6col_header_upgraded(board, capsys):
    """구 6칼럼(base 스키마) → canonical 8칼럼 업그레이드 + area_owner 채움."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_OLD_6COL, encoding="utf-8")
    rc, _, _ = _run(board, capsys)
    assert rc == 0
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |" in text
    assert board._ticket_area_owner("T-PAY-001") == "alice"
    assert board._area_owner_in_use() is True


def test_old_5col_header_upgraded(board, capsys):
    """구 5칼럼(per-repo 레지스트리) → canonical 8칼럼 업그레이드 + area_owner 채움."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_OLD_5COL, encoding="utf-8")
    rc, _, _ = _run(board, capsys)
    assert rc == 0
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |" in text
    assert board._ticket_area_owner("T-PAY-001") == "alice"
    assert board._area_owner_in_use() is True


def test_old_header_upgrade_preserves_existing_columns_and_comments(board, capsys):
    """업그레이드는 비파괴 — 기존 칼럼 값·표 밖 주석 보존(area_owner 만 append)."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_OLD_7COL, encoding="utf-8")
    _run(board, capsys)
    # 기존 칼럼 값이 그대로 — 행 dict 로 읽어 검증(파서 동형).
    row = board._areas_row_for_prefix("PAY")
    assert row["repo"] == "service-a"
    assert row["git"] == "g:a"
    assert row["test_cmd"] == "pytest -q"
    assert row["owner"] == "reg"
    assert row["base"] == "develop"
    assert row["protected"] == "main"
    assert row["area_owner"] == "alice"
    # 표 밖 주석·제목 보존.
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert text.startswith("# Area Registry\n")
    assert "주석은 보존돼야 한다" in text


def test_old_header_upgrade_idempotent(board, capsys):
    """업그레이드 후 재실행 = no-op(파일 bytes 불변·멱등)."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_OLD_7COL, encoding="utf-8")
    _run(board, capsys)
    after1 = board.AREAS_FILE.read_text(encoding="utf-8")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    assert "변경 없음" in out
    assert board.AREAS_FILE.read_text(encoding="utf-8") == after1


def test_old_header_upgrade_dry_run_no_write(board, capsys):
    """구 헤더라도 --dry-run 은 파일을 쓰지 않는다(미리보기만)."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_OLD_7COL, encoding="utf-8")
    before = board.AREAS_FILE.read_text(encoding="utf-8")
    rc, out, _ = _run(board, capsys, dry_run=True)
    assert rc == 0
    assert "area PAY: area_owner → alice" in out
    assert board.AREAS_FILE.read_text(encoding="utf-8") == before


# ════════════════════════════════════════════════════════════════════════
# 혼재 파일 (T-0168 codex r2) — 비-canonical 구 헤더 + 그 아래 canonical 8칼럼 row
# ════════════════════════════════════════════════════════════════════════
#
# 비-canonical 3칼럼 헤더(`prefix|area|owner` — canonical prefix 와 칼럼 의미가 어긋남)에서는
# 업그레이드가 헤더 끝에 area_owner 칼럼만 append 한다. 그런데 그 헤더 아래에 `areas_append` 가
# 쓴 완전한 canonical 8칼럼 row(area_owner 빈값)가 *함께* 있을 수 있다(append-only 업그레이드
# 프로젝트). 이 wider row 는 `_parse_areas` 가 헤더 무시하고 canonical 순서로 매핑하므로
# area_owner=index 7 이다 — migrate 도 같게 index 7 로 채워야 한다(헤더 폭 3 으로 읽으면 index 3
# = test_cmd 를 area_owner 로 오인해 backfill 을 놓침·codex r2 must-fix).

# 3칼럼 비-canonical 헤더 + 3칼럼 row(degrade append) + canonical 8칼럼 row(area_owner 빈값) 혼재.
_AREAS_MIXED_NONCANON_HEADER = (
    "# Area Registry\n\n"
    "> 멀티-clone variant (주석 보존).\n\n"
    "| prefix | area | owner |\n"
    "|---|---|---|\n"
    "| LEG | legacy-area | reg |\n"
    "| service-a | PAY | g:a | pytest -q | reg | develop | main |  |\n"
)


def test_mixed_noncanon_header_backfills_both_rows(board, capsys):
    """비-canonical 3칼럼 헤더 아래 3칼럼 row + canonical 8칼럼 row 가 *둘 다* backfill 된다.

    - 3칼럼 row(LEG): 헤더 폭과 같음 → 헤더 append 위치(index 3)에 area_owner=user 패딩.
    - 8칼럼 row(PAY): 헤더보다 넓음 → `_parse_areas` 동형으로 canonical index 7 의 빈 area_owner
      를 user 로 채움(must-fix 전엔 index 3 의 `pytest -q` 를 area_owner 로 오인해 no-op).
    """
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_MIXED_NONCANON_HEADER, encoding="utf-8")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    # 두 행 모두 area_owner=alice 로 backfill — 파서 동형으로 읽어 검증.
    assert board._ticket_area_owner("T-LEG-001") == "alice"   # 3칼럼 row(degrade append).
    assert board._ticket_area_owner("T-PAY-001") == "alice"   # 8칼럼 wider row(index 7).
    # 8칼럼 row 의 기존 칼럼 값은 불변(test_cmd 오인 채움이 아님을 확인).
    pay = board._areas_row_for_prefix("PAY")
    assert pay["test_cmd"] == "pytest -q"   # index 3 보존(area_owner 로 오염 안 됨).
    assert pay["protected"] == "main"        # index 6 보존.
    # 표 밖 주석 보존.
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert "멀티-clone variant" in text


def test_mixed_noncanon_header_idempotent(board, capsys):
    """혼재 파일 backfill 후 재실행 = no-op(파일 bytes 불변·멱등)."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_MIXED_NONCANON_HEADER, encoding="utf-8")
    _run(board, capsys)
    after1 = board.AREAS_FILE.read_text(encoding="utf-8")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    assert "변경 없음" in out
    assert board.AREAS_FILE.read_text(encoding="utf-8") == after1


def test_mixed_noncanon_header_preserves_prefilled_wider_row(board, capsys):
    """canonical 8칼럼 wider row 의 area_owner 가 *이미* 채워져 있으면 불변(멱등·index 7 인식)."""
    _write_conf(board, user="alice", session="pm-1")
    areas = (
        "# Area Registry\n\n"
        "| prefix | area | owner |\n"
        "|---|---|---|\n"
        "| service-a | PAY | g:a | pytest -q | reg | develop | main | bob |\n"
    )
    board.AREAS_FILE.write_text(areas, encoding="utf-8")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    # index 7 이 이미 bob → 불변(alice 로 안 덮음).
    assert board._ticket_area_owner("T-PAY-001") == "bob"


# ════════════════════════════════════════════════════════════════════════
# 파생 board.md 갱신 (T-0168 codex sug) — 비-dry-run 쓰기 후 refresh_board 1회
# ════════════════════════════════════════════════════════════════════════

def test_refresh_board_called_on_write(board, capsys, monkeypatch):
    """실제 쓰기가 있으면 끝에 refresh_board() 1회 호출(claimed 표시 갱신 정합)."""
    calls = []
    monkeypatch.setattr(board, "refresh_board", lambda: calls.append(1))
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0005", "claimed", fm_extra={"claimed_by": "pm-1"})
    rc, _, _ = _run(board, capsys)
    assert rc == 0
    assert calls == [1]   # 정확히 1회.


def test_refresh_board_not_called_on_dry_run(board, capsys, monkeypatch):
    """--dry-run 은 파생물도 안 건드림 — refresh_board 미호출."""
    calls = []
    monkeypatch.setattr(board, "refresh_board", lambda: calls.append(1))
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0005", "claimed", fm_extra={"claimed_by": "pm-1"})
    rc, _, _ = _run(board, capsys, dry_run=True)
    assert rc == 0
    assert calls == []    # 미호출.


def test_refresh_board_not_called_when_noop(board, capsys, monkeypatch):
    """backfill 대상 0(이미 마이그레이션됨)이면 쓰기 없음 → refresh_board 미호출."""
    calls = []
    monkeypatch.setattr(board, "refresh_board", lambda: calls.append(1))
    _write_conf(board, user="alice", session="pm-1")
    # 이미 채워진 티켓(no-op).
    _seed(board, "T-0005", "claimed",
          fm_extra={"created_by": "alice", "claimed_by": "alice/pm-1"})
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    assert "변경 없음" in out
    assert calls == []


# ════════════════════════════════════════════════════════════════════════
# areas.md RMW 가 board_lock() 구간 (T-0168 codex r3 must-fix 2·ADR-0012/0014)
# ════════════════════════════════════════════════════════════════════════
#
# areas.md backfill(read→transform→write_text)이 `areas_append` 와 같은 board_lock 구간
# 밖에서 일어나면, 동시 실행되는 repo 등록 append row 를 RMW 의 write_text 가 통째로 클로버
# 한다(lost update). 검증: (a) areas 변경 시 board_lock 이 정확히 진입하고, areas.md 의
# *상태 변화*가 그 락 구간 *안에서* 발생한다(진입 시점 bytes ≠ 해제 시점 bytes). (b) dry-run
# 은 read-only(쓰기 0)라 락을 잡지 않는다.


@contextlib.contextmanager
def _lock_spy(board, monkeypatch):
    """board_lock 을 래핑해 진입/해제 시점에 AREAS_FILE bytes 를 캡처하는 spy.

    반환 record 의 `entries` 는 (락 진입 시 areas bytes, 락 해제 시 areas bytes) 튜플 리스트.
    락 한 구간 안에서 areas.md 가 바뀌면 그 튜플의 두 값이 달라진다 → write 가 락 안에서
    일어났음을 증명한다. `count` 는 락 진입 횟수.
    """
    real_lock = board.board_lock
    record = {"entries": [], "count": 0}

    def _read_areas():
        try:
            return board.AREAS_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    @contextlib.contextmanager
    def _spied():
        record["count"] += 1
        before = _read_areas()
        with real_lock():
            yield
        record["entries"].append((before, _read_areas()))

    monkeypatch.setattr(board, "board_lock", _spied)
    yield record


def test_areas_write_happens_inside_board_lock(board, capsys, monkeypatch):
    """areas.md backfill 쓰기가 board_lock 구간 *안에서* 일어난다(락이 RMW 를 감쌈)."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    with _lock_spy(board, monkeypatch) as rec:
        rc, _, _ = _run(board, capsys)
    assert rc == 0
    # board_lock 이 적어도 1회 진입했고, 그 중 한 구간 안에서 areas.md 가 *변했다*.
    assert rec["count"] >= 1
    changed_in_lock = [e for e in rec["entries"] if e[0] != e[1] and e[0] is not None]
    assert changed_in_lock, "areas.md write 가 board_lock 구간 안에서 일어나지 않음"
    # 그리고 실제로 backfill 됨(외부 관찰: alice 로 채워짐).
    assert board._ticket_area_owner("T-PAY-001") == "alice"


def test_areas_dry_run_does_not_acquire_lock(board, capsys, monkeypatch):
    """--dry-run 은 read-only(쓰기 0) — areas 미리보기에 board_lock 을 잡지 않는다."""
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    before = board.AREAS_FILE.read_text(encoding="utf-8")
    with _lock_spy(board, monkeypatch) as rec:
        rc, out, _ = _run(board, capsys, dry_run=True)
    assert rc == 0
    assert "area PAY: area_owner → alice" in out   # 미리보기는 보고됨.
    assert rec["count"] == 0                        # 그러나 락은 미획득.
    assert board.AREAS_FILE.read_text(encoding="utf-8") == before  # 쓰기 0.


def test_areas_noop_does_not_write_in_lock(board, capsys, monkeypatch):
    """areas backfill 대상 0(이미 채워짐)이면 락은 잡되 write 는 없다(bytes 불변)."""
    _write_conf(board, user="alice", session="pm-1")
    # 모든 area_owner 가 이미 채워진 areas.md(no-op).
    areas = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| service-a | PAY | g:a | pytest -q | reg | develop | main | alice |\n"
    )
    board.AREAS_FILE.write_text(areas, encoding="utf-8")
    before = board.AREAS_FILE.read_text(encoding="utf-8")
    with _lock_spy(board, monkeypatch) as rec:
        rc, _, _ = _run(board, capsys)
    assert rc == 0
    # 락을 잡았더라도 areas.md 는 바뀌지 않는다(no-op).
    assert board.AREAS_FILE.read_text(encoding="utf-8") == before
    assert all(e[0] == e[1] for e in rec["entries"])  # 어느 락 구간도 areas 를 안 바꿈.


# ════════════════════════════════════════════════════════════════════════
# 동시성 모델 — areas=락 보호(참)·티켓=best-effort 재조회 skip (T-0168 교정·r4 override)
# ════════════════════════════════════════════════════════════════════════
#
# r4 의 "변경 단계 전체를 단일 board_lock 으로 원자화하면 티켓 이동이 막힌다"는 거짓 전제였다
# (codex 실증): cmd_claim/complete/block/unclaim 은 board_lock 을 *안* 잡고 lock-free
# atomic-rename(`move_ticket`)만 쓴다(ADR-0012). migration 이 board_lock 을 쥐어도 티켓 이동을
# 못 막는다 — 그 락은 거짓 보장이고 차단만 유발한다. 그래서:
#   - areas write 는 board_lock 보호(`areas_append` 와의 lost-update 방지·진짜 공유 mutation).
#   - 티켓 backfill 은 글로벌락 *없이* best-effort — 각 티켓을 쓰기 직전 ID 로 재조회해
#     이동/완료됐으면 skip(stale 쓰기 0), 살아 있으면 atomic write(부분쓰기 0).
# 잔여 미세 TOCTOU(재조회↔replace)는 *하드 보장 아님* — migrate-identity 는 단일-세션 op 다.


def test_tickets_do_not_acquire_global_lock(board, capsys, monkeypatch):
    """티켓 backfill 은 글로벌 board_lock 을 잡지 않는다(areas 없으면 락 0회).

    티켓 이동이 lock-free atomic-rename 이라 락은 이동을 못 막는다 → 거짓 안전을 두지 않고
    티켓 루프를 락 밖에 둔다. areas.md 가 없으면(솔로) 어떤 board_lock 도 진입하지 않는다.
    """
    real_lock = board.board_lock
    count = {"n": 0}

    @contextlib.contextmanager
    def _spied():
        count["n"] += 1
        with real_lock():
            yield

    monkeypatch.setattr(board, "board_lock", _spied)
    # refresh_board 는 끝에 자체 board_lock 을 잡는다(파생물·락 밖) — 티켓 backfill *자체*가
    # 락을 잡는지만 보려고 no-op 으로 둔다(refresh 락밖 1회는 별 테스트가 검증).
    monkeypatch.setattr(board, "refresh_board", lambda: None)
    _write_conf(board, user="alice", session="pm-1")
    # areas.md 없음(솔로) — 티켓만 backfill.
    _seed(board, "T-0001", "open")
    _seed(board, "T-0005", "claimed", fm_extra={"claimed_by": "pm-1"})
    rc, _, _ = _run(board, capsys)
    assert rc == 0
    # 티켓이 backfill 됐는데도 board_lock 은 한 번도 안 잡힘(락-free 티켓 경로).
    assert count["n"] == 0, "티켓 backfill 이 글로벌 board_lock 을 잡았다(거짓 안전)"
    assert board.load_ticket(
        board.TICKETS_DIR / "open" / "T-0001-seed.md")[0]["created_by"] == "alice"


def test_ticket_moved_before_write_is_skipped(board, capsys, monkeypatch):
    """쓰기 직전 티켓이 *이동*되면 재조회→skip — stale 쓰기 0(이동된 파일 안 건드림).

    best-effort 동시성: 다른 세션이 claim 으로 open/→claimed/ 이동시킨 상황을 모사한다.
    `find_ticket`(쓰기 직전 재조회)을 래핑해 스캔 경로(open/)가 아닌 새 경로(claimed/)를
    돌려주면, 경로 불일치로 skip 되고 스캔 경로엔 절대 쓰지 않는다. (하드 보장 아님 — 재조회와
    replace 사이 미세 창은 단일-세션 전제로 수용. 여기선 *재조회가 잡는* 케이스를 검증.)
    """
    _write_conf(board, user="alice", session="pm-1")
    scan_path = _seed(board, "T-0001", "open")  # 스캔이 보는 open/ 경로.
    before_scan = scan_path.read_text(encoding="utf-8")
    # 다른 세션이 claim 으로 open/→claimed/ 옮긴 상태를 모사 — 재조회가 claimed/ 경로를
    # 반환하게 한다(물리 사본은 안 둠 — skip 은 쓰기 *전* 경로 불일치로 발동하므로 충분).
    moved_path = board.TICKETS_DIR / "claimed" / "T-0001-seed.md"
    real_find = board.find_ticket

    def _find_moved(tid):
        # 재조회는 항상 이동된(claimed/) 경로를 반환 → 스캔 경로(open/)와 불일치 → skip.
        if tid == "T-0001":
            return "claimed", moved_path
        return real_find(tid)

    monkeypatch.setattr(board, "find_ticket", _find_moved)
    rc, _, err = _run(board, capsys)
    assert rc == 0
    # skip 경고가 stderr 에 떴고, 스캔 경로(open/)엔 stale 쓰기가 없다(bytes 불변).
    assert "skip T-0001" in err and "이동됨" in err
    assert scan_path.read_text(encoding="utf-8") == before_scan
    # 이동된 경로(claimed/)에도 stale 쓰기를 하지 않는다(재조회 케이스는 backfill 보류).
    assert not moved_path.exists()


def test_ticket_gone_before_write_is_skipped(board, capsys, monkeypatch):
    """쓰기 직전 티켓이 *사라지면*(재조회 FileNotFoundError) skip — 다른 파일 안 건드림."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0001", "open")
    _seed(board, "T-0002", "open")  # 살아있는 다른 티켓.

    real_find = board.find_ticket

    def _find_gone(tid):
        if tid == "T-0001":
            raise FileNotFoundError("ticket not found: T-0001")  # 완료/삭제 모사.
        return real_find(tid)

    monkeypatch.setattr(board, "find_ticket", _find_gone)
    rc, _, err = _run(board, capsys)
    assert rc == 0
    assert "skip T-0001" in err and "없음" in err
    # 살아있는 T-0002 는 정상 backfill(한 티켓 skip 이 나머지를 막지 않음).
    fm, _ = board.load_ticket(board.TICKETS_DIR / "open" / "T-0002-seed.md")
    assert fm["created_by"] == "alice"


def test_surviving_ticket_atomic_write(board, capsys, monkeypatch):
    """정상 경로(이동 없음)는 atomic write 로 backfill 된다(`dump_ticket_atomic` 경유).

    best-effort 가 정상 케이스를 막지 않음 + 쓰기가 temp+os.replace 의 원자 교체임을 확인한다
    (`dump_ticket_atomic` 가 정확히 1회 호출되고, 결과 파일이 정상 backfill 됨).
    """
    calls = []
    real_atomic = board.dump_ticket_atomic

    def _spied_atomic(path, fm, body):
        calls.append(str(path))
        real_atomic(path, fm, body)

    monkeypatch.setattr(board, "dump_ticket_atomic", _spied_atomic)
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0001", "open")
    rc, _, err = _run(board, capsys)
    assert rc == 0
    fm, _b = board.load_ticket(board.TICKETS_DIR / "open" / "T-0001-seed.md")
    assert fm["created_by"] == "alice"
    # atomic write 가 살아있는 티켓에 정확히 1회.
    assert len(calls) == 1 and calls[0].endswith("T-0001-seed.md")


def test_dry_run_acquires_no_lock(board, capsys, monkeypatch):
    """--dry-run 은 read-only — board_lock 을 전혀 잡지 않고 어떤 파일도 안 쓴다."""
    real_lock = board.board_lock
    count = {"n": 0}

    @contextlib.contextmanager
    def _spied():
        count["n"] += 1
        with real_lock():
            yield

    monkeypatch.setattr(board, "board_lock", _spied)
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    p = _seed(board, "T-0005", "claimed", fm_extra={"claimed_by": "pm-1"})
    before_areas = board.AREAS_FILE.read_text(encoding="utf-8")
    before_ticket = p.read_text(encoding="utf-8")
    rc, out, _ = _run(board, capsys, dry_run=True)
    assert rc == 0
    assert "area PAY: area_owner → alice" in out
    assert "claimed_by pm-1 → alice/pm-1" in out
    assert count["n"] == 0                                     # 락 미획득.
    assert board.AREAS_FILE.read_text(encoding="utf-8") == before_areas
    assert p.read_text(encoding="utf-8") == before_ticket


def test_refresh_board_runs_outside_lock(board, capsys, monkeypatch):
    """refresh_board 는 areas board_lock 보유 *밖에서* 1회 호출된다(데드락 회귀 가드).

    refresh_board 자체가 board_lock 을 잡는데(non-reentrant), areas 락 구간 *안에서* 부르면
    재진입 데드락이다. board_lock 진입 깊이를 추적해 refresh 호출 시점 깊이가 0 임을 확인한다.
    """
    real_lock = board.board_lock
    depth = {"active": 0}

    @contextlib.contextmanager
    def _spied():
        depth["active"] += 1
        try:
            with real_lock():
                yield
        finally:
            depth["active"] -= 1

    monkeypatch.setattr(board, "board_lock", _spied)
    real_refresh = board.refresh_board
    depth_at_refresh = []

    def _spied_refresh():
        depth_at_refresh.append(depth["active"])
        real_refresh()

    monkeypatch.setattr(board, "refresh_board", _spied_refresh)
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    _seed(board, "T-0005", "claimed", fm_extra={"claimed_by": "pm-1"})
    rc, _, _ = _run(board, capsys)
    assert rc == 0
    assert depth_at_refresh == [0], "refresh_board 가 락 밖에서 정확히 1회 호출되지 않음"


def test_full_run_completes_without_deadlock(board, capsys):
    """hermetic 전체 실행(areas + 티켓 다수)이 데드락/행 없이 끝나고 board.md 가 refresh 된다.

    areas board_lock 재진입(refresh_board 가 areas 락 안에서 호출되거나 헬퍼가 락을 다시 잡음)이
    있으면 여기서 OS flock 데드락(hang)이나 에러로 잡힌다. 실제 refresh_board 를 그대로
    돌려(monkeypatch 없이) board.md 산출까지 확인한다.
    """
    _write_conf(board, user="alice", session="pm-1")
    board.AREAS_FILE.write_text(_AREAS_EMPTY_OWNER, encoding="utf-8")
    _seed(board, "T-0001", "open")
    _seed(board, "T-0005", "claimed", fm_extra={"claimed_by": "pm-1"})
    _seed(board, "T-0009", "done")
    rc, out, _ = _run(board, capsys)
    assert rc == 0
    # 데드락 없이 완료 — 모든 backfill 이 적용됨.
    assert board.load_ticket(
        board.TICKETS_DIR / "open" / "T-0001-seed.md")[0]["created_by"] == "alice"
    assert board.load_ticket(
        board.TICKETS_DIR / "claimed" / "T-0005-seed.md")[0]["claimed_by"] == "alice/pm-1"
    assert board._ticket_area_owner("T-PAY-001") == "alice"
    # 파생 board.md 가 refresh 됨(락 밖 1회 호출·실제 실행).
    assert board.BOARD_FILE.exists()
    assert "# Ticket Board" in board.BOARD_FILE.read_text(encoding="utf-8")


def test_surviving_ticket_still_backfilled(board, capsys):
    """정상 경로(아무것도 안 움직임)는 backfill 된다(best-effort 가 정상 케이스를 막지 않음)."""
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-0001", "open")
    rc, _, err = _run(board, capsys)
    assert rc == 0
    fm, _b = board.load_ticket(board.TICKETS_DIR / "open" / "T-0001-seed.md")
    assert fm["created_by"] == "alice"
