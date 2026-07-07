"""board.py 티켓 ID 참조 rewriter 코어 단위테스트 (T-0238·ADR-0042 §3.3 step 4).

`rewrite_text_token_aware` / `collect_rewrite_targets` / `rewrite_refs` 세 순수 헬퍼를
검증한다 — hermetic: root 를 tmp 트리로 주입하고 모듈 전역 상태(REPO 등)는 건드리지 않는다.
경계 규칙(뒤 char ∉ [0-9-])·전 표기형(frontmatter·wikilink·bare·산문)·prefix 표기형·
dry-run 카운트를 커버한다.

도구는 패키지가 아니므로 importlib 동적 로드 (test_board_lint 의 _load_module 관용구).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"


def _load_board():
    spec = importlib.util.spec_from_file_location("board", BOARD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board():
    return _load_board()


# ── rewrite_text_token_aware — 경계 규칙 (DoD a) ──────────────────────────

def test_boundary_trailing_digit_not_replaced(board):
    """`T-0063`→X 치환이 `T-00631`(뒤 숫자)을 건드리지 않는다."""
    out, n = board.rewrite_text_token_aware("T-00631", {"T-0063": "T-0900"})
    assert out == "T-00631"
    assert n == 0


def test_boundary_trailing_hyphen_not_replaced(board):
    """`T-0063`→X 치환이 `T-0063-2`(뒤 하이픈)을 건드리지 않는다."""
    out, n = board.rewrite_text_token_aware("T-0063-2", {"T-0063": "T-0900"})
    assert out == "T-0063-2"
    assert n == 0


def test_boundary_exact_match_replaced(board):
    """정확 토큰은 치환된다 (경계 char 가 [0-9-] 아님)."""
    out, n = board.rewrite_text_token_aware("see T-0063.", {"T-0063": "T-0900"})
    assert out == "see T-0900."
    assert n == 1


def test_boundary_end_of_string_replaced(board):
    """문자열 끝(뒤 char 없음)도 유효 경계 — 치환된다."""
    out, n = board.rewrite_text_token_aware("ref T-0063", {"T-0063": "T-0900"})
    assert out == "ref T-0900"
    assert n == 1


# ── 왼쪽/오른쪽 경계 — 식별자 인접 불변 vs 비-식별자 인접 치환 (codex must-fix) ──

@pytest.mark.parametrize("text", [
    "NOT-0063",        # 앞 식별자 문자(O) — 왼쪽 부분매치
    "fooT-0063",       # 앞 식별자 문자(o) — 왼쪽 부분매치
    "T-0063_legacy",   # 뒤 언더스코어 — 오른쪽 부분매치
])
def test_boundary_identifier_adjacent_not_replaced(board, text):
    """양옆 식별자 문자(`[A-Za-z0-9_-]`) 인접이면 치환하지 않는다(부분매치 방지)."""
    out, n = board.rewrite_text_token_aware(text, {"T-0063": "T-0900"})
    assert out == text
    assert n == 0


@pytest.mark.parametrize("text, expected", [
    ("([[T-0063]])", "([[T-0900]])"),                   # 괄호·대괄호 인접
    ("(T-0063)", "(T-0900)"),                           # 괄호 인접
    ("depends_on: T-0063", "depends_on: T-0900"),       # 공백 인접
    ("[[T-0063]]다음", "[[T-0900]]다음"),               # 한글 인접(비-식별자)
])
def test_boundary_non_identifier_adjacent_replaced(board, text, expected):
    """비-식별자(괄호·공백·한글) 인접은 양쪽 경계이므로 정상 치환된다."""
    out, n = board.rewrite_text_token_aware(text, {"T-0063": "T-0900"})
    assert out == expected
    assert n == 1


# ── 전 표기형 각각 치환 (DoD b) ────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("depends_on: T-0063", "depends_on: T-0900"),                    # frontmatter
    ("[[T-0063]]", "[[T-0900]]"),                                    # wikilink
    ("bare T-0063 here", "bare T-0900 here"),                        # bare inline
    ("**A(T-0063 classify)·B(x)**", "**A(T-0900 classify)·B(x)**"),  # 산문 임베드
])
def test_all_notation_forms_replaced(board, text, expected):
    out, n = board.rewrite_text_token_aware(text, {"T-0063": "T-0900"})
    assert out == expected
    assert n == 1


# ── prefix 표기형 (DoD c) ──────────────────────────────────────────────────

def test_prefix_form_replaced(board):
    """prefix 표기형 `T-finance-011`→`T-0700` 치환 (§1.4 전 표기형·wikilink+frontmatter)."""
    out, n = board.rewrite_text_token_aware(
        "[[T-finance-011]] and depends_on: T-finance-011",
        {"T-finance-011": "T-0700"})
    assert out == "[[T-0700]] and depends_on: T-0700"
    assert n == 2


def test_prefix_form_boundary(board):
    """prefix 형도 뒤 char ∉ [0-9-] 경계 — `T-finance-0110`·`T-finance-011-3` 불변."""
    out, n = board.rewrite_text_token_aware(
        "T-finance-0110 T-finance-011-3", {"T-finance-011": "T-0700"})
    assert out == "T-finance-0110 T-finance-011-3"
    assert n == 0


# ── 다중 ID·카운트·단일 pass·빈 맵 ─────────────────────────────────────────

def test_multiple_ids_and_counts(board):
    id_map = {"T-0063": "T-0900", "T-finance-011": "T-0700"}
    out, n = board.rewrite_text_token_aware(
        "T-0063 T-0063 T-finance-011 T-00631", id_map)
    assert out == "T-0900 T-0900 T-0700 T-00631"
    assert n == 3


def test_empty_map_noop(board):
    out, n = board.rewrite_text_token_aware("T-0063", {})
    assert out == "T-0063"
    assert n == 0


def test_single_pass_no_chained_replacement(board):
    """단일 pass — new 값(`T-0100`)이 map 의 다른 old 키여도 재치환되지 않는다."""
    out, n = board.rewrite_text_token_aware(
        "T-0063", {"T-0063": "T-0100", "T-0100": "T-0200"})
    assert out == "T-0100"
    assert n == 1


# ── collect_rewrite_targets — hermetic tmp 트리 ───────────────────────────

def _build_tree(root: Path) -> dict[str, Path]:
    """root 하위에 board/tickets·wiki·log 레이아웃 + 참조 담은 파일들을 생성한다."""
    ticket = root / "board" / "tickets" / "open" / "T-0500-x.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text(
        "---\nid: T-0500\ndepends_on: T-0063\n---\n"
        "# T-0500\nsee [[T-0063]] and T-00631\n",
        encoding="utf-8")
    adr = root / "wiki" / "decisions" / "0001-x.md"
    adr.parent.mkdir(parents=True, exist_ok=True)
    adr.write_text("refs [[T-0063]] and **A(T-0063 x)**\n", encoding="utf-8")
    log = root / "log" / "current.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("T-0063 done\n", encoding="utf-8")
    noise = root / "wiki" / "no-refs.md"
    noise.write_text("nothing here\n", encoding="utf-8")
    return {"ticket": ticket, "adr": adr, "log": log, "noise": noise}


def test_collect_targets_covers_three_roots(board, tmp_path):
    files = _build_tree(tmp_path)
    got = set(board.collect_rewrite_targets(tmp_path))
    assert got == set(files.values())


def test_collect_targets_missing_subdirs_ok(board, tmp_path):
    """하위 dir 이 일부만 있어도 예외 없이 부분 목록을 낸다."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "a.md").write_text("x\n", encoding="utf-8")
    got = board.collect_rewrite_targets(tmp_path)
    assert [p.name for p in got] == ["a.md"]


def test_collect_targets_empty_root(board, tmp_path):
    assert board.collect_rewrite_targets(tmp_path) == []


# ── rewrite_refs — dry-run 카운트·적용 (DoD d) ────────────────────────────

def test_rewrite_refs_dry_run_counts(board, tmp_path):
    files = _build_tree(tmp_path)
    before = {p: p.read_text(encoding="utf-8") for p in files.values()}
    res = board.rewrite_refs(tmp_path, {"T-0063": "T-0900"}, dry_run=True)
    # ticket: depends_on + [[T-0063]] = 2 (T-00631 경계 제외),
    # adr: [[T-0063]] + **A(T-0063 x)** = 2, log: 1 → refs 5·files 3(noise 제외)·ids 1.
    assert res == {"ids": 1, "refs": 5, "files": 3}
    for p, txt in before.items():           # dry_run → 파일 미변경
        assert p.read_text(encoding="utf-8") == txt


def test_rewrite_refs_applies_and_counts(board, tmp_path):
    files = _build_tree(tmp_path)
    res = board.rewrite_refs(tmp_path, {"T-0063": "T-0900"}, dry_run=False)
    assert res == {"ids": 1, "refs": 5, "files": 3}
    tk = files["ticket"].read_text(encoding="utf-8")
    assert "depends_on: T-0900" in tk
    assert "[[T-0900]]" in tk
    assert "T-00631" in tk                       # 경계 — 미변경
    assert "T-0063" not in tk.replace("T-00631", "")  # 다른 T-0063 잔존 없음
    assert "T-0900" in files["adr"].read_text(encoding="utf-8")
    assert files["log"].read_text(encoding="utf-8") == "T-0900 done\n"
    assert files["noise"].read_text(encoding="utf-8") == "nothing here\n"


def test_rewrite_refs_multiple_ids(board, tmp_path):
    tk = tmp_path / "board" / "tickets" / "open" / "T-0001-a.md"
    tk.parent.mkdir(parents=True)
    tk.write_text("[[T-finance-011]] and T-0063 and T-finance-0110\n", encoding="utf-8")
    res = board.rewrite_refs(
        tmp_path, {"T-finance-011": "T-0700", "T-0063": "T-0900"}, dry_run=False)
    # T-finance-011→1·T-0063→1·T-finance-0110 경계→0.
    assert res == {"ids": 2, "refs": 2, "files": 1}
    assert tk.read_text(encoding="utf-8") == "[[T-0700]] and T-0900 and T-finance-0110\n"


def test_rewrite_refs_unreferenced_id_not_counted(board, tmp_path):
    """N=id_map 중 *실제 참조된* ID 수 — 미참조 old(T-9999)는 세지 않는다."""
    w = tmp_path / "wiki" / "a.md"
    w.parent.mkdir(parents=True)
    w.write_text("only T-0063 here\n", encoding="utf-8")
    res = board.rewrite_refs(
        tmp_path, {"T-0063": "T-0900", "T-9999": "T-8888"}, dry_run=True)
    assert res["ids"] == 1
    assert res["refs"] == 1


def test_rewrite_refs_empty_map(board, tmp_path):
    _build_tree(tmp_path)
    res = board.rewrite_refs(tmp_path, {}, dry_run=False)
    assert res == {"ids": 0, "refs": 0, "files": 0}


def test_boundary_trailing_alpha_not_replaced(board):
    """codex T-0239 R2: 오른쪽 알파벳 인접(`T-0063abc`)은 다른 토큰 — 불변. 한글 인접은 치환."""
    new, n = board.rewrite_text_token_aware(
        "T-0063abc 그리고 [[T-0063]]다음", {"T-0063": "T-0900"})
    assert "T-0063abc" in new and "T-0900abc" not in new   # 알파벳 인접 = 다른 토큰·불변.
    assert "[[T-0900]]다음" in new and n == 1               # 비-ASCII(한글) 인접은 정상 치환.
