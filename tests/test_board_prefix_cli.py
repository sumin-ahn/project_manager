"""board.py `prefix rename/strip/merge/delete` + `none` 1급 단위테스트 (T-0239·ADR-0042 §3.2/§3.3).

카테고리 개명/통합 동사를 CLI 레벨로 검증한다 — old→new 맵·collision abort·본문 rewrite +
slug 파일명 rename·dry-run·홈 git 가드·board-git 백업. `_prefix_relabel` 이 소비하는
T-0238 rewriter(`rewrite_refs`)는 별도 파일(test_board_rewrite)에서 커버되므로 여기선 동사
파이프라인·번호 산술·경계 결정에 집중한다.

**hermetic 필수**: board.py 경로 전역(REPO·TICKETS_DIR·BOARD_FILE·LOG_FILE·BOARD_LOCK·
LOCAL_DIR)을 tmp 프로젝트로 monkeypatch 재지정한다(test_board_multipm 패턴 동류). 홈 git
가드는 기본적으로 clean(`""`)로 monkeypatch 해 환경 git 유무와 무관하게 결정적이다 —
가드 자체의 dirty-abort 는 전용 테스트가 override 로 검증한다.
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
    (root / ".project_manager" / "wiki" / "log").mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / "wiki" / "decisions").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def board(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load_board()
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    local = pm / ".local"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": local,
        "BOARD_LOCK": local / "board.lock",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # 홈 git 가드는 기본 clean — 환경 git 유무와 무관하게 relabel 이 진행되게(가드 자체는 전용 테스트).
    monkeypatch.setattr(mod, "_home_git_status_porcelain", lambda: "")
    mod._proj = proj
    return mod


# ── 티켓/참조 심기 헬퍼 ────────────────────────────────────────────────────

def _seed_ticket(board, tid: str, *, created: str = "2026-07-01",
                 status: str = "open", body: str = "") -> Path:
    """`{tid}-slug.md` 티켓을 심는다 (created 지정 가능 — merge 정렬용)."""
    path = board.TICKETS_DIR / status / f"{tid}-slug.md"
    board.dump_ticket(
        path,
        {"id": tid, "title": "t", "status": status, "created": created},
        f"# {tid} — t\n{body}\n")
    return path


def _seed_wiki(board, name: str, text: str) -> Path:
    p = board.REPO / ".project_manager" / "wiki" / "decisions" / name
    p.write_text(text, encoding="utf-8")
    return p


def _ids_on_disk(board) -> set[str]:
    """디스크의 모든 티켓 파일명에서 canonical ID 를 추출한다 (파일 rename 검증용)."""
    out = set()
    for status in board.STATUS_DIRS:
        for p in (board.TICKETS_DIR / status).glob("T-*.md"):
            tid = board._ticket_id_from_filename(p.name)
            if tid:
                out.add(tid)
    return out


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# ════════════════════════════════════════════════════════════════════════
# 순수 헬퍼 — 번호 산술·맵 빌더·collision
# ════════════════════════════════════════════════════════════════════════

def test_format_ticket_id_none_is_four_digit(board):
    assert board._format_ticket_id(None, 5) == "T-0005"


def test_format_ticket_id_prefix_is_three_digit(board):
    assert board._format_ticket_id("foo", 5) == "T-foo-005"


def test_parse_prefix_arg_none_reserved(board):
    assert board._parse_prefix_arg("none") is None
    assert board._parse_prefix_arg("foo") == "foo"


def test_rename_map_preserves_number(board):
    tickets = [
        {"id": "T-foo-001", "num": 1, "prefix": "foo", "created": "2026-01-01"},
        {"id": "T-foo-012", "num": 12, "prefix": "foo", "created": "2026-01-02"},
        {"id": "T-0009", "num": 9, "prefix": None, "created": "2026-01-03"},
    ]
    m = board._rename_map("foo", "bar", tickets)
    assert m == {"T-foo-001": "T-bar-001", "T-foo-012": "T-bar-012"}


def test_rename_map_to_none_reformats_width(board):
    tickets = [{"id": "T-foo-007", "num": 7, "prefix": "foo", "created": "x"}]
    assert board._rename_map("foo", None, tickets) == {"T-foo-007": "T-0007"}


def test_rename_map_none_to_prefix(board):
    tickets = [{"id": "T-0007", "num": 7, "prefix": None, "created": "x"}]
    assert board._rename_map(None, "foo", tickets) == {"T-0007": "T-foo-007"}


def test_merge_append_map_appends_after_target_max_in_created_order(board):
    tickets = [
        {"id": "T-0001", "num": 1, "prefix": None, "created": "2026-01-01"},
        {"id": "T-0002", "num": 2, "prefix": None, "created": "2026-01-02"},
        # source foo — created 순 002(앞) → 001(뒤). append 는 created 순으로 재부여.
        {"id": "T-foo-001", "num": 1, "prefix": "foo", "created": "2026-06-10"},
        {"id": "T-foo-002", "num": 2, "prefix": "foo", "created": "2026-06-05"},
    ]
    m = board._merge_append_map(["foo"], None, tickets)
    # 대상 max=2 → append 3,4. created 순: foo-002(06-05) 먼저 → T-0003, foo-001(06-10) → T-0004.
    assert m == {"T-foo-002": "T-0003", "T-foo-001": "T-0004"}


def test_merge_append_tiebreak_preserves_existing_order(board):
    """같은 created 안에선 기존 번호 상대순서 보존 (tiebreak)."""
    tickets = [
        {"id": "T-0005", "num": 5, "prefix": None, "created": "2026-01-01"},
        {"id": "T-foo-002", "num": 2, "prefix": "foo", "created": "2026-06-01"},
        {"id": "T-foo-001", "num": 1, "prefix": "foo", "created": "2026-06-01"},
    ]
    m = board._merge_append_map(["foo"], None, tickets)
    # 대상 max=5 → 같은 created 라 기존 번호순 001 먼저(T-0006)·002(T-0007).
    assert m == {"T-foo-001": "T-0006", "T-foo-002": "T-0007"}


def test_merge_append_multi_source_interleaves_by_created(board):
    tickets = [
        {"id": "T-0001", "num": 1, "prefix": None, "created": "2026-01-01"},
        {"id": "T-a-001", "num": 1, "prefix": "a", "created": "2026-06-03"},
        {"id": "T-b-001", "num": 1, "prefix": "b", "created": "2026-06-01"},
        {"id": "T-a-002", "num": 2, "prefix": "a", "created": "2026-06-05"},
    ]
    m = board._merge_append_map(["a", "b"], None, tickets)
    # created 순: b-001(06-01)→2·a-001(06-03)→3·a-002(06-05)→4 (대상 max=1).
    assert m == {"T-b-001": "T-0002", "T-a-001": "T-0003", "T-a-002": "T-0004"}


def test_merge_reorder_map_renumbers_all_from_one(board):
    tickets = [
        {"id": "T-0009", "num": 9, "prefix": None, "created": "2026-06-04"},
        {"id": "T-foo-001", "num": 1, "prefix": "foo", "created": "2026-06-01"},
        {"id": "T-foo-002", "num": 2, "prefix": "foo", "created": "2026-06-08"},
    ]
    m = board._merge_reorder_map(["foo"], None, tickets)
    # 전체 interleave created 순: foo-001(06-01)→1·T-0009(06-04)→2·foo-002(06-08)→3.
    assert m == {"T-foo-001": "T-0001", "T-0009": "T-0002", "T-foo-002": "T-0003"}


def test_detect_collisions_flags_duplicate_final_id(board):
    id_map = {"T-foo-003": "T-bar-003"}
    all_ids = {"T-foo-003", "T-bar-003"}  # T-bar-003 이미 존재 → 최종 충돌.
    assert board._detect_collisions(id_map, all_ids) == ["T-bar-003"]


def test_detect_collisions_none_when_unique(board):
    id_map = {"T-foo-001": "T-bar-001", "T-foo-002": "T-bar-002"}
    all_ids = {"T-foo-001", "T-foo-002"}
    assert board._detect_collisions(id_map, all_ids) == []


def test_detect_collisions_normalizes_zero_pad_width(board):
    """폭만 다른 같은 논리번호(`T-001` vs `T-0001`) 공존도 충돌로 잡는다 (문자열 비교 아님)."""
    id_map = {"T-foo-001": "T-001"}       # foo-001 → 무prefix num 1(폭 3)
    all_ids = {"T-foo-001", "T-0001"}     # 이미 T-0001(폭 4·같은 논리번호) 존재 → 논리충돌.
    assert board._detect_collisions(id_map, all_ids) == ["T-0001", "T-001"]


def test_collision_key_normalizes_prefix_and_number(board):
    """`_collision_key` — 폭 무관 같은 (prefix, 논리번호) 키·malformed 는 문자열 폴백."""
    assert board._collision_key("T-001") == board._collision_key("T-0001")
    assert board._collision_key("T-foo-007") == ("foo", 7)
    assert board._collision_key("garbage") == "garbage"  # 순번 미파싱 → 리터럴


# ════════════════════════════════════════════════════════════════════════
# rename — 무충돌 교체 / 충돌 안내 / none 양방향
# ════════════════════════════════════════════════════════════════════════

def test_rename_no_collision_swaps_prefix_and_files(board, capsys):
    _seed_ticket(board, "T-foo-001")
    _seed_ticket(board, "T-foo-002")
    wiki = _seed_wiki(board, "0001-x.md", "refs [[T-foo-001]] and T-foo-002\n")

    rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=False))
    assert rc == 0
    # 파일명 rename — foo → bar (번호 유지).
    assert _ids_on_disk(board) == {"T-bar-001", "T-bar-002"}
    # 본문 frontmatter id 도 rewrite.
    renamed = board.TICKETS_DIR / "open" / "T-bar-001-slug.md"
    fm, _ = board.load_ticket(renamed)
    assert fm["id"] == "T-bar-001"
    # wiki 참조까지 rewrite.
    assert wiki.read_text(encoding="utf-8") == "refs [[T-bar-001]] and T-bar-002\n"


def test_rename_collision_guides_to_merge_and_aborts(board, capsys):
    _seed_ticket(board, "T-foo-003")
    _seed_ticket(board, "T-bar-003")  # bar 네임스페이스에 003 이미 존재 → 충돌.
    before = _ids_on_disk(board)

    rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "충돌" in err
    assert "merge foo --into bar" in err
    # 파일 무변경(abort).
    assert _ids_on_disk(board) == before


def test_rename_to_none_strips_name(board):
    _seed_ticket(board, "T-foo-005")
    rc = board.cmd_prefix_rename(_ns(src="foo", dst="none", dry_run=False))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-0005"}


def test_rename_none_to_prefix_applies_name(board):
    _seed_ticket(board, "T-0005")
    rc = board.cmd_prefix_rename(_ns(src="none", dst="foo", dry_run=False))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-foo-005"}


def test_rename_same_src_dst_rejected(board, capsys):
    _seed_ticket(board, "T-foo-001")
    rc = board.cmd_prefix_rename(_ns(src="foo", dst="foo", dry_run=False))
    assert rc == 1
    assert "같다" in capsys.readouterr().err


def test_rename_invalid_dst_format_rejected(board, capsys):
    _seed_ticket(board, "T-foo-001")
    rc = board.cmd_prefix_rename(_ns(src="foo", dst="Bad-Name", dry_run=False))
    assert rc == 1
    assert "형식" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-foo-001"}  # 무변경


def test_rename_no_matching_tickets_is_noop(board, capsys):
    _seed_ticket(board, "T-0001")
    rc = board.cmd_prefix_rename(_ns(src="ghost", dst="bar", dry_run=False))
    assert rc == 0
    assert "티켓이 없다" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════════════
# strip — rename <A> none 별칭 등가
# ════════════════════════════════════════════════════════════════════════

def test_strip_equals_rename_to_none(board):
    _seed_ticket(board, "T-foo-005")
    _seed_ticket(board, "T-foo-006")
    rc = board.cmd_prefix_strip(_ns(prefix="foo", dry_run=False))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-0005", "T-0006"}


def test_strip_dry_run_no_write(board, capsys):
    _seed_ticket(board, "T-foo-005")
    rc = board.cmd_prefix_strip(_ns(prefix="foo", dry_run=True))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-foo-005"}  # 무변경
    assert "[dry-run]" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════════════
# merge — append 기본 / reorder opt-in / collision safety
# ════════════════════════════════════════════════════════════════════════

def test_merge_append_default_numbering(board, capsys):
    _seed_ticket(board, "T-0001", created="2026-01-01")
    _seed_ticket(board, "T-0002", created="2026-01-02")
    _seed_ticket(board, "T-foo-001", created="2026-06-10")
    _seed_ticket(board, "T-foo-002", created="2026-06-05")

    rc = board.cmd_prefix_merge(
        _ns(sources=["foo"], into="none", reorder_chronological=False, dry_run=False))
    assert rc == 0
    # 대상(none) 원본 무변경 + source 는 max(2) 뒤 created 순 append(foo-002→3·foo-001→4).
    assert _ids_on_disk(board) == {"T-0001", "T-0002", "T-0003", "T-0004"}


def test_merge_append_preserves_target_numbers(board):
    """append 는 대상 네임스페이스 기존 번호를 절대 안 바꾼다."""
    _seed_ticket(board, "T-acc-005", created="2026-01-01")
    _seed_ticket(board, "T-foo-001", created="2026-06-01")
    rc = board.cmd_prefix_merge(
        _ns(sources=["foo"], into="acc", reorder_chronological=False, dry_run=False))
    assert rc == 0
    # acc-005 유지 + foo-001 → acc-006 (max 5 뒤).
    assert _ids_on_disk(board) == {"T-acc-005", "T-acc-006"}


def test_merge_reorder_chronological_renumbers_all(board):
    _seed_ticket(board, "T-0009", created="2026-06-04")
    _seed_ticket(board, "T-foo-001", created="2026-06-01")
    _seed_ticket(board, "T-foo-002", created="2026-06-08")
    rc = board.cmd_prefix_merge(
        _ns(sources=["foo"], into="none", reorder_chronological=True, dry_run=False))
    assert rc == 0
    # 전체 interleave: foo-001→0001·T-0009→0002·foo-002→0003.
    assert _ids_on_disk(board) == {"T-0001", "T-0002", "T-0003"}


def test_apply_file_renames_survives_swap(board):
    """2단계(src→tmp→dst) rename — 번호 맞바꿈(001↔002)에도 두 파일 무손실."""
    a = _seed_ticket(board, "T-foo-001")
    b = _seed_ticket(board, "T-foo-002")
    board._apply_file_renames([(a, a.with_name("T-foo-002-slug.md")),
                               (b, b.with_name("T-foo-001-slug.md"))])
    assert _ids_on_disk(board) == {"T-foo-001", "T-foo-002"}
    # 원본 두 파일이 다 살아남았는지(잔여 tmp 없음).
    open_dir = board.TICKETS_DIR / "open"
    assert sorted(p.name for p in open_dir.glob("*")) == \
        ["T-foo-001-slug.md", "T-foo-002-slug.md"]


def test_merge_reorder_swap_survives_file_rename(board):
    """reorder 가 대상 네임스페이스 번호를 뒤섞어(target 재사용) clobber 위험이 있어도 무손실."""
    # foo(대상) 두 티켓 + bar(source) 한 티켓을 created 순으로 interleave — foo-001 의 대상
    # 번호(003)와 foo-002→foo-001 이 겹쳐 naive rename 이면 중간 clobber 가 난다.
    _seed_ticket(board, "T-foo-001", created="2026-06-08")  # → T-foo-003
    _seed_ticket(board, "T-foo-002", created="2026-06-01")  # → T-foo-001
    _seed_ticket(board, "T-bar-001", created="2026-06-04")  # → T-foo-002
    rc = board.cmd_prefix_merge(
        _ns(sources=["bar"], into="foo", reorder_chronological=True, dry_run=False))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-foo-001", "T-foo-002", "T-foo-003"}
    # foo-002(가장 이른 created)가 T-foo-001 로.
    fm, _ = board.load_ticket(board.TICKETS_DIR / "open" / "T-foo-001-slug.md")
    assert fm["id"] == "T-foo-001"


def test_plan_file_renames_excludes_unrewritable_ticket(board, capsys):
    """비-UTF-8(rewrite 스킵) 티켓 파일은 _plan_file_renames 에서도 제외(파일명↔content id 불일치 방지)."""
    _seed_ticket(board, "T-foo-001")
    bad = board.TICKETS_DIR / "open" / "T-foo-002-slug.md"
    bad.write_bytes(b"\xff\xfe id: T-foo-002 \x00\x80")  # 유효 UTF-8 아님 → rewrite skip 대상
    id_map = {"T-foo-001": "T-bar-001", "T-foo-002": "T-bar-002"}
    renames = board._plan_file_renames(id_map)
    got = {(src.name, dst.name) for src, dst in renames}
    # good 파일만 rename 계획에 포함·bad(비-UTF-8)는 제외(파일명 유지).
    assert got == {("T-foo-001-slug.md", "T-bar-001-slug.md")}
    err = capsys.readouterr().err
    assert "rename skip" in err
    assert "T-foo-002" in err


def test_merge_into_self_rejected(board, capsys):
    _seed_ticket(board, "T-foo-001")
    rc = board.cmd_prefix_merge(
        _ns(sources=["foo"], into="foo", reorder_chronological=False, dry_run=False))
    assert rc == 1
    assert "source 목록에 있다" in capsys.readouterr().err


def test_merge_no_source_tickets_is_noop(board, capsys):
    _seed_ticket(board, "T-0001")
    rc = board.cmd_prefix_merge(
        _ns(sources=["ghost"], into="none", reorder_chronological=False, dry_run=False))
    assert rc == 0
    assert "변경 없음" in capsys.readouterr().out


def test_merge_dry_run_reports_scale_no_write(board, capsys):
    _seed_ticket(board, "T-0001")
    _seed_ticket(board, "T-foo-001")
    _seed_wiki(board, "0001-x.md", "see T-foo-001\n")
    rc = board.cmd_prefix_merge(
        _ns(sources=["foo"], into="none", reorder_chronological=False, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "ID 변경" in out and "refs" in out
    # 파일·참조 무변경.
    assert _ids_on_disk(board) == {"T-0001", "T-foo-001"}


# ════════════════════════════════════════════════════════════════════════
# delete — 빈 것 통과 / 비빈 것 fail-loud
# ════════════════════════════════════════════════════════════════════════

def test_delete_unregistered_prefix_confirm_only(board, capsys):
    """0 티켓·areas 미등록이면 지울 등록이 없어 '확인만'(변경 0)으로 정직하게 보고한다."""
    _seed_ticket(board, "T-0001")  # none 네임스페이스만 존재; foo=0 티켓·미등록.
    rc = board.cmd_prefix_delete(_ns(prefix="foo", dry_run=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 티켓" in out
    assert "등록 없음" in out and "확인만" in out


def test_delete_registered_prefix_clears_areas_cell(board, capsys):
    """0 티켓·areas 등록됨 → 그 행의 prefix 셀을 실제로 비운다(행·repo 등록 보존·promise=do)."""
    board.areas_append("foo", "area-foo", "reg", repo="foo", git="g:foo",
                       test_cmd="pytest -q", area_owner="alice")
    assert board._areas_row_for_prefix("foo") is not None
    rc = board.cmd_prefix_delete(_ns(prefix="foo", dry_run=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "셀" in out and "비움" in out
    # prefix 셀만 비웠고(등록 이름 해제) 행 자체와 다른 셀(repo·test_cmd·area_owner)은 보존.
    assert board._areas_row_for_prefix("foo") is None
    _h, rows = board._parse_areas()
    foo_rows = [r for r in rows if r.get("repo") == "foo"]
    assert len(foo_rows) == 1                       # 행 유지(무손실)
    assert foo_rows[0]["prefix"] == ""              # prefix 셀만 비움
    assert foo_rows[0]["test_cmd"] == "pytest -q"   # 무관 셀 보존
    assert foo_rows[0]["area_owner"] == "alice"


def test_delete_dry_run_registered_no_write(board, capsys):
    """--dry-run 은 등록 셀 비움 예정만 preview·쓰기 0(등록 유지)."""
    board.areas_append("foo", "area-foo", "reg", repo="foo")
    rc = board.cmd_prefix_delete(_ns(prefix="foo", dry_run=True))
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert board._areas_row_for_prefix("foo") is not None   # 쓰기 0 — 등록 유지


def test_areas_clear_prefix_cell_preserves_other_rows(board):
    """`_areas_clear_prefix_cell` — 대상 prefix 셀만 비우고 무관 행은 원문 그대로 보존한다."""
    board.areas_append("foo", "af", "reg", repo="foo", test_cmd="pt-foo")
    board.areas_append("bar", "ab", "reg", repo="bar", test_cmd="pt-bar")
    cleared = board._areas_clear_prefix_cell("foo")
    assert cleared == 1
    _h, rows = board._parse_areas()
    foo = [r for r in rows if r.get("repo") == "foo"][0]
    bar = [r for r in rows if r.get("repo") == "bar"][0]
    assert foo["prefix"] == ""              # foo 만 비움
    assert bar["prefix"] == "bar"           # bar 는 무변경
    assert bar["test_cmd"] == "pt-bar"


def test_delete_nonempty_prefix_fails_loud(board, capsys):
    _seed_ticket(board, "T-foo-001")
    rc = board.cmd_prefix_delete(_ns(prefix="foo", dry_run=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "티켓 1개" in err
    assert "rename" in err and "merge" in err
    assert _ids_on_disk(board) == {"T-foo-001"}  # 물리삭제 없음


def test_delete_none_rejected(board, capsys):
    rc = board.cmd_prefix_delete(_ns(prefix="none", dry_run=False))
    assert rc == 1
    assert "delete 불가" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# 홈 git clean 가드 — dirty 면 abort·무변경
# ════════════════════════════════════════════════════════════════════════

def test_home_git_dirty_aborts_relabel(board, monkeypatch, capsys):
    _seed_ticket(board, "T-foo-001")
    monkeypatch.setattr(board, "_home_git_status_porcelain", lambda: " M wiki/x.md\n")
    rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=False))
    assert rc == 1
    assert "홈 git" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-foo-001"}  # 무변경


def test_home_git_dirty_does_not_block_dry_run(board, monkeypatch, capsys):
    """dry-run 은 쓰기 0 이므로 홈 git 이 dirty 여도 규모 preview 는 낸다."""
    _seed_ticket(board, "T-foo-001")
    monkeypatch.setattr(board, "_home_git_status_porcelain", lambda: " M wiki/x.md\n")
    rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=True))
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════════════
# board-git 백업 commit — 분리 형상은 commit·legacy 는 skip 안내
# ════════════════════════════════════════════════════════════════════════

def test_board_git_backup_commit_when_separated(board, monkeypatch, capsys):
    calls = {"commit": []}
    monkeypatch.setattr(board, "_board_git_enabled", lambda: True)
    monkeypatch.setattr(board, "_board_git_head", lambda: "deadbeefcafe0000")
    monkeypatch.setattr(
        board, "_board_git_stage_and_commit",
        lambda msg: calls["commit"].append(msg) or True)
    _seed_ticket(board, "T-foo-001")

    rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=False))
    assert rc == 0
    # 분리 형상 → board-git 백업 commit 1회(relabel 메시지) + 백업 rev 안내.
    assert calls["commit"] == ["prefix rename foo → bar"]
    out = capsys.readouterr().out
    assert "백업 rev" in out
    assert "deadbeefcafe" in out


def test_board_git_legacy_skips_with_guidance(board, monkeypatch, capsys):
    monkeypatch.setattr(board, "_board_git_enabled", lambda: False)
    _seed_ticket(board, "T-foo-001")
    rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=False))
    assert rc == 0
    assert "legacy" in capsys.readouterr().out
    assert _ids_on_disk(board) == {"T-bar-001"}  # relabel 은 그래도 적용


# ════════════════════════════════════════════════════════════════════════
# 락 직렬화 — relabel mutation 이 단일 board_lock 구간·재진입 없음 (codex must-fix)
# ════════════════════════════════════════════════════════════════════════
#
# rewrite→rename→refresh 전체를 board 의 기존 OS flock(cmd_new ID 발행·cmd_claim 이 쓰는
# 그 락·ADR-0012)으로 감싸 동시 new/claim 과 직렬화한다. 내부 refresh 는 lock-held 변형
# (`_refresh_board_locked`)을 직접 불러 board_lock 재진입(데드락)을 피한다. real_lock 을 감싸는
# spy 로 (a) 락 진입, (b) 구간 내 mutation, (c) 중첩 depth 1(재진입 없음)을 확인한다.


@contextlib.contextmanager
def _lock_depth_spy(board, monkeypatch):
    """board_lock 을 real_lock 으로 감싸 진입 횟수·중첩 depth·구간 내 티켓 변화를 기록하는 spy."""
    real_lock = board.board_lock
    rec = {"count": 0, "depth": 0, "max_depth": 0, "changed_in_lock": False}

    @contextlib.contextmanager
    def _spied():
        rec["count"] += 1
        rec["depth"] += 1
        rec["max_depth"] = max(rec["max_depth"], rec["depth"])
        before = _ids_on_disk(board)
        try:
            with real_lock():
                yield
        finally:
            if _ids_on_disk(board) != before:
                rec["changed_in_lock"] = True
            rec["depth"] -= 1

    monkeypatch.setattr(board, "board_lock", _spied)
    yield rec


def test_relabel_mutation_runs_under_single_board_lock(board, monkeypatch):
    """relabel(rewrite→rename→refresh)이 board_lock 을 잡고, 그 구간 안에서 티켓 ID 가 바뀐다.

    중첩 depth ≤ 1 확인 — refresh 가 lock-held 변형(_refresh_board_locked)을 직접 불러 board_lock
    재진입(데드락)을 피한다는 회귀 가드(reentrant 면 real flock 이 hang 또는 depth 2).
    """
    _seed_ticket(board, "T-foo-001")
    with _lock_depth_spy(board, monkeypatch) as rec:
        rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=False))
    assert rc == 0
    assert rec["count"] >= 1               # board_lock 을 잡았다(직렬화)
    assert rec["max_depth"] == 1           # 재진입 없음(데드락 위험 0)
    assert rec["changed_in_lock"]          # 그 락 구간 안에서 relabel 이 적용됐다
    assert _ids_on_disk(board) == {"T-bar-001"}


def test_relabel_dry_run_takes_no_lock(board, monkeypatch, capsys):
    """dry-run 은 read-only(쓰기 0)라 board_lock 을 전혀 잡지 않는다."""
    _seed_ticket(board, "T-foo-001")
    with _lock_depth_spy(board, monkeypatch) as rec:
        rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=True))
    assert rc == 0
    assert rec["count"] == 0                # 락 미획득
    assert "[dry-run]" in capsys.readouterr().out
    assert _ids_on_disk(board) == {"T-foo-001"}


# ════════════════════════════════════════════════════════════════════════
# hardening (T-0238 함수) — 개행 보존 + 읽기실패 graceful skip
# ════════════════════════════════════════════════════════════════════════

def test_rewrite_preserves_crlf_line_endings(board, tmp_path):
    """CRLF 파일을 rewrite 해도 개행이 LF 로 무단 정규화되지 않는다 (Windows 채택자)."""
    wiki = tmp_path / "wiki" / "a.md"
    wiki.parent.mkdir(parents=True)
    wiki.write_bytes(b"see T-0063 here\r\nnext line\r\n")
    res = board.rewrite_refs(tmp_path, {"T-0063": "T-0900"}, dry_run=False)
    assert res["refs"] == 1
    assert wiki.read_bytes() == b"see T-0900 here\r\nnext line\r\n"


def test_rewrite_skips_unreadable_file_with_warning(board, tmp_path, capsys):
    """비-UTF-8(디코드 불가) 파일은 graceful skip + stderr 경고 1줄(silent 누락 금지)."""
    good = tmp_path / "wiki" / "good.md"
    good.parent.mkdir(parents=True)
    good.write_text("ref T-0063\n", encoding="utf-8")
    bad = tmp_path / "wiki" / "bad.md"
    bad.write_bytes(b"\xff\xfe T-0063 \x00\x80")  # 유효 UTF-8 아님 → UnicodeDecodeError
    res = board.rewrite_refs(tmp_path, {"T-0063": "T-0900"}, dry_run=False)
    # good 는 rewrite 되고 bad 는 skip.
    assert good.read_text(encoding="utf-8") == "ref T-0900\n"
    assert res["files"] == 1
    err = capsys.readouterr().err
    assert "rewrite skip" in err
    assert "bad.md" in err


def test_relabel_includes_drafts_dir(board, capsys):
    """codex T-0239 R2 must-fix 회귀-lock: `.drafts` 티켓도 relabel 대상.

    draft 도 이미 발행된 ID(find_ticket/_next_id 인지·T-0198) — relabel 이 놓치면 old-prefix
    draft 가 잔존, promote 시 혼재가 보드로 재유입된다. rename 이 .drafts 파일명·frontmatter id
    까지 바꿈을 단언한다."""
    _seed_ticket(board, "T-foo-001")
    (board.TICKETS_DIR / ".drafts").mkdir(exist_ok=True)  # 엔진은 on-demand 생성(drafts_dir).
    draft = _seed_ticket(board, "T-foo-002", status=".drafts")
    rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=False))
    assert rc == 0
    renamed_draft = board.TICKETS_DIR / ".drafts" / "T-bar-002-slug.md"
    assert renamed_draft.exists() and not draft.exists()    # .drafts 파일명 rename 됨.
    fm, _ = board.load_ticket(renamed_draft)
    assert fm["id"] == "T-bar-002"                           # frontmatter id 도 rewrite.
    assert not (board.TICKETS_DIR / ".drafts" / "T-foo-002-slug.md").exists()


def test_relabel_dst_occupied_aborts_before_any_write(board, monkeypatch, capsys):
    """codex T-0239 R3 회귀-lock: 계획 밖 파일이 dst 를 점유하면 **아무것도 쓰기 전에** abort.

    collision 검사가 stale(검사↔적용 사이 발행)이었던 상황을 `_detect_collisions` 무력화로
    시뮬레이션 — dst 선검증(belt)이 rewrite/rename 전에 rc1 로 끊고 본문·파일명 모두 무변경
    (덮어쓰기 원천 차단)을 단언한다."""
    src_path = _seed_ticket(board, "T-foo-001", body="src body")
    dst_path = _seed_ticket(board, "T-bar-001", body="dst body")   # dst 점유자.
    monkeypatch.setattr(board, "_detect_collisions", lambda *a, **k: [])  # stale 검사 시뮬.
    rc = board.cmd_prefix_rename(_ns(src="foo", dst="bar", dry_run=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "이미 존재" in err and "쓰기 0" in err
    # 본문·파일명 전부 무변경 — 점유자 본문 보존(덮어쓰기 없음).
    assert src_path.exists() and dst_path.exists()
    assert "dst body" in dst_path.read_text(encoding="utf-8")
    fm, _ = board.load_ticket(src_path)
    assert fm["id"] == "T-foo-001"                     # rewrite 도 안 일어남(쓰기 0).
