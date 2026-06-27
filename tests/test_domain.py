"""domain covers 파서 + 코드↔페이지 인덱스 테스트 (T-0080 · ADR-0018 · Phase 1 #1).

`domain.py` 의 파서·스캔·글롭 매칭을 hermetic 하게 검증한다 — tmp domain dir 주입
(`load_pages(domain_dir=tmp)`)으로 실 `.project_manager/wiki/domain/` 을 건드리지 않는다.

커버:
  - parse_page 파싱 정확 — covers list·derived bool·updated·title·type.
  - parse_page 결손 기본값 — covers 부재 → []·derived 부재 → False.
  - load_pages — README.md·_template.md 제외 · frontmatter 깨짐/없음 graceful skip(crash 0) ·
    domain/ 부재 → [].
  - pages_for_path 글롭 — `**` 재귀 매치/미스 · `*` 단일 세그먼트 매치/미스 ·
    리터럴 파일 경로 · 빈 covers 매칭 0 · multi-glob.
  - main(list) — 페이지 1줄 출력 · 비어있으면 "(domain 페이지 없음)".

board.load_ticket 을 재사용하므로(중복 파서 없음) frontmatter 의미는 그 계약을 따른다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_domain():
    spec = importlib.util.spec_from_file_location("domain", TOOLS / "domain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def dm():
    return _load_domain()


# ── 페이지 작성 헬퍼 ─────────────────────────────────────────────────────────


def _write_page(domain_dir: Path, name: str, *, frontmatter: str, body: str = "\nbody\n") -> Path:
    """tmp domain dir 에 frontmatter md 페이지를 쓴다(--- fm --- body)."""
    domain_dir.mkdir(parents=True, exist_ok=True)
    path = domain_dir / name
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


# ── parse_page ───────────────────────────────────────────────────────────────


def test_parse_page_full(dm, tmp_path):
    path = _write_page(
        tmp_path,
        "review.md",
        frontmatter=(
            "title: 이중 게이트 리뷰\n"
            "type: guide\n"
            "covers:\n"
            "  - .project_manager/tools/external_review.py\n"
            "  - .claude/agents/code-reviewer.md\n"
            "derived: false\n"
            "updated: 2026-06-19"
        ),
    )
    page = dm.parse_page(path)
    assert page["path"] == path
    assert page["title"] == "이중 게이트 리뷰"
    assert page["type"] == "guide"
    assert page["covers"] == [
        ".project_manager/tools/external_review.py",
        ".claude/agents/code-reviewer.md",
    ]
    assert page["derived"] is False
    assert str(page["updated"]) == "2026-06-19"


def test_parse_page_derived_true(dm, tmp_path):
    path = _write_page(
        tmp_path, "gen.md",
        frontmatter="title: 자동생성\ntype: concept\nderived: true",
    )
    page = dm.parse_page(path)
    assert page["derived"] is True


def test_parse_page_missing_covers_defaults_empty(dm, tmp_path):
    path = _write_page(
        tmp_path, "concept.md",
        frontmatter="title: 코드-무관\ntype: concept",
    )
    page = dm.parse_page(path)
    assert page["covers"] == []
    assert page["derived"] is False


# ── load_pages ───────────────────────────────────────────────────────────────


def test_load_pages_excludes_readme_and_template(dm, tmp_path):
    _write_page(tmp_path, "README.md", frontmatter="title: idx\ntype: index")
    _write_page(tmp_path, "_template.md", frontmatter="title: tpl\ntype: concept")
    _write_page(tmp_path, "real.md", frontmatter="title: 실페이지\ntype: guide")
    pages = dm.load_pages(domain_dir=tmp_path)
    titles = [p["title"] for p in pages]
    assert titles == ["실페이지"]


def test_load_pages_graceful_skip_broken(dm, tmp_path, capsys):
    # frontmatter 없는 파일 + 안 닫힌 frontmatter — 둘 다 skip, crash 0.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "nofm.md").write_text("그냥 본문\n", encoding="utf-8")
    (tmp_path / "broken.md").write_text("---\ntitle: x\n본문(닫힘 없음)\n", encoding="utf-8")
    _write_page(tmp_path, "ok.md", frontmatter="title: 정상\ntype: concept")
    pages = dm.load_pages(domain_dir=tmp_path)
    assert [p["title"] for p in pages] == ["정상"]
    err = capsys.readouterr().err
    assert "nofm.md" in err and "broken.md" in err


def test_load_pages_missing_dir_returns_empty(dm, tmp_path):
    assert dm.load_pages(domain_dir=tmp_path / "does-not-exist") == []


def test_load_pages_empty_dir_returns_empty(dm, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    assert dm.load_pages(domain_dir=tmp_path) == []


def test_load_pages_recurses_subfolders(dm, tmp_path):
    # T-0126: domain wikitree 를 하위 폴더로 조직해도 페이지가 잡혀야 한다(rglob).
    _write_page(tmp_path, "top.md", frontmatter="title: 최상위\ntype: concept")
    _write_page(tmp_path / "area", "nested.md", frontmatter="title: 하위\ntype: guide")
    _write_page(tmp_path / "area" / "deep", "deeper.md", frontmatter="title: 더깊이\ntype: concept")
    titles = sorted(p["title"] for p in dm.load_pages(domain_dir=tmp_path))
    assert titles == ["더깊이", "최상위", "하위"]


def test_load_pages_excludes_readme_template_in_subfolders(dm, tmp_path):
    # README/_template 는 어느 깊이든 name 으로 제외(rglob 후에도).
    _write_page(tmp_path / "area", "README.md", frontmatter="title: idx\ntype: index")
    _write_page(tmp_path / "area", "_template.md", frontmatter="title: tpl\ntype: concept")
    _write_page(tmp_path / "area", "real.md", frontmatter="title: 실하위\ntype: guide")
    assert [p["title"] for p in dm.load_pages(domain_dir=tmp_path)] == ["실하위"]


# ── pages_for_path (글롭 매칭) ───────────────────────────────────────────────


@pytest.fixture
def matched_pages(dm, tmp_path):
    _write_page(
        tmp_path, "analysis.md",
        frontmatter="title: analysis\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    _write_page(
        tmp_path, "single.md",
        frontmatter="title: single\ntype: concept\ncovers:\n  - src/core/*.py",
    )
    _write_page(
        tmp_path, "literal.md",
        frontmatter="title: literal\ntype: guide\ncovers:\n  - tools/exact.py",
    )
    _write_page(
        tmp_path, "abstract.md",
        frontmatter="title: abstract\ntype: concept",  # covers 없음 = 코드-무관
    )
    return dm.load_pages(domain_dir=tmp_path)


def _titles(pages):
    return sorted(p["title"] for p in pages)


def test_glob_double_star_matches_nested(dm, matched_pages):
    hits = dm.pages_for_path("src/analysis/factor_beta.py", matched_pages)
    assert _titles(hits) == ["analysis"]


def test_glob_double_star_matches_deep_nested(dm, matched_pages):
    hits = dm.pages_for_path("src/analysis/sub/deep/x.py", matched_pages)
    assert _titles(hits) == ["analysis"]


def test_glob_double_star_misses_sibling(dm, matched_pages):
    hits = dm.pages_for_path("src/core/x.py", matched_pages)
    # src/core/x.py 는 src/core/*.py(single) 매치, src/analysis/** 미스.
    assert _titles(hits) == ["single"]


def test_glob_single_star_one_segment_only(dm, matched_pages):
    # src/core/*.py 는 한 세그먼트 — 중첩 src/core/sub/x.py 는 미스.
    assert dm.pages_for_path("src/core/sub/x.py", matched_pages) == []


def test_glob_literal_exact_match(dm, matched_pages):
    assert _titles(dm.pages_for_path("tools/exact.py", matched_pages)) == ["literal"]
    assert dm.pages_for_path("tools/exact_other.py", matched_pages) == []


def test_empty_covers_matches_nothing(dm, matched_pages):
    # 어떤 경로도 covers 없는 abstract 페이지를 매치하지 않는다.
    for probe in ("abstract.md", "src/analysis/x.py", "anything"):
        assert all(p["title"] != "abstract"
                   for p in dm.pages_for_path(probe, matched_pages))


def test_path_matches_multiple_pages(dm, tmp_path):
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: a\ntype: concept\ncovers:\n  - src/**",
    )
    _write_page(
        tmp_path, "b.md",
        frontmatter="title: b\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    pages = dm.load_pages(domain_dir=tmp_path)
    hits = dm.pages_for_path("src/analysis/x.py", pages)
    assert _titles(hits) == ["a", "b"]


def test_glob_double_star_does_not_match_prefix_outside_dir(dm, matched_pages):
    # src/analysis/** 는 src/analysis_other/x.py 를 매치하지 않는다(세그먼트 경계).
    assert dm.pages_for_path("src/analysis_other/x.py", matched_pages) == []


def test_glob_trailing_double_star_matches_dir_itself(dm, tmp_path):
    # 0-세그먼트(차단 must-fix): src/analysis/** 는 src/analysis(디렉토리 자체)도 매치.
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: a\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    pages = dm.load_pages(domain_dir=tmp_path)
    assert _titles(dm.pages_for_path("src/analysis", pages)) == ["a"]
    # 세그먼트 경계는 여전히 지킨다 — 형제 prefix 미스.
    assert dm.pages_for_path("src/analysis_other", pages) == []


def test_glob_middle_double_star_matches_zero_segments(dm, tmp_path):
    # 0-세그먼트(차단 must-fix): src/**/x.py 는 src/x.py(중간 0 세그먼트)도 매치.
    _write_page(
        tmp_path, "mid.md",
        frontmatter="title: mid\ntype: concept\ncovers:\n  - src/**/x.py",
    )
    pages = dm.load_pages(domain_dir=tmp_path)
    assert _titles(dm.pages_for_path("src/x.py", pages)) == ["mid"]      # 0 세그먼트
    assert _titles(dm.pages_for_path("src/a/x.py", pages)) == ["mid"]    # 1 세그먼트
    assert _titles(dm.pages_for_path("src/a/b/x.py", pages)) == ["mid"]  # 2 세그먼트
    # full-match anchored — 접미 변형·다른 루트는 미스.
    assert dm.pages_for_path("src/x.pyy", pages) == []
    assert dm.pages_for_path("other/x.py", pages) == []


def test_glob_leading_double_star_matches_zero_segments(dm, tmp_path):
    # 0-세그먼트(차단 must-fix): **/x.py 는 x.py(선두 0 세그먼트)도 매치.
    _write_page(
        tmp_path, "lead.md",
        frontmatter="title: lead\ntype: concept\ncovers:\n  - '**/x.py'",
    )
    pages = dm.load_pages(domain_dir=tmp_path)
    assert _titles(dm.pages_for_path("x.py", pages)) == ["lead"]       # 0 세그먼트
    assert _titles(dm.pages_for_path("a/x.py", pages)) == ["lead"]     # 1 세그먼트
    assert _titles(dm.pages_for_path("a/b/x.py", pages)) == ["lead"]   # 2 세그먼트


def test_glob_double_star_escapes_regex_meta_in_literal(dm, tmp_path):
    # escape 보존: a.b/** 의 리터럴 '.' 가 임의 문자로 새지 않는다(regex 메타 escape).
    _write_page(
        tmp_path, "meta.md",
        frontmatter="title: meta\ntype: concept\ncovers:\n  - a.b/**",
    )
    pages = dm.load_pages(domain_dir=tmp_path)
    assert _titles(dm.pages_for_path("a.b/x.py", pages)) == ["meta"]  # 리터럴 점 매치
    assert _titles(dm.pages_for_path("a.b", pages)) == ["meta"]       # 디렉토리 자체
    assert dm.pages_for_path("aXb/x.py", pages) == []                 # '.' 가 임의문자로 안 샘


def test_scalar_covers_treated_as_single_glob_not_char_split(dm, tmp_path):
    # 회귀(reviewer must-fix): covers 가 스칼라 문자열(list 들여쓰기 누락)이면 단일
    # 글롭으로 감싸야 한다. 과거 `list("src/x/**")` 가 글자 분해돼 분해된 '*' 가
    # 임의 단일-세그먼트 경로(README·other.py)를 거짓 매칭했다.
    path = _write_page(
        tmp_path, "scalar.md",
        frontmatter="title: scalar\ntype: concept\ncovers: src/x/**",
    )
    page = dm.parse_page(path)
    assert page["covers"] == ["src/x/**"]  # 글자 분해 안 됨

    pages = dm.load_pages(domain_dir=tmp_path)
    # (a) 의도 경로는 매치.
    assert _titles(dm.pages_for_path("src/x/y.py", pages)) == ["scalar"]
    # (b) 무관한 단일-세그먼트 경로는 거짓 매칭 안 함(과거 버그).
    assert dm.pages_for_path("README", pages) == []
    assert dm.pages_for_path("other.py", pages) == []


def test_covers_non_string_list_elements_dropped(dm, tmp_path):
    # 비-문자열 covers 원소(숫자·None 등 frontmatter 오기)는 방어적으로 버린다.
    path = _write_page(
        tmp_path, "mixed.md",
        frontmatter="title: mixed\ntype: concept\ncovers:\n  - src/ok/**\n  - 123\n  - null",
    )
    page = dm.parse_page(path)
    assert page["covers"] == ["src/ok/**"]


# ── pages_for_touches (touches ∩ covers·union·dedup) ─────────────────────────


@pytest.fixture
def touch_pages(dm, tmp_path):
    _write_page(
        tmp_path, "analysis.md",
        frontmatter="title: analysis\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    _write_page(
        tmp_path, "core.md",
        frontmatter="title: core\ntype: concept\ncovers:\n  - src/core/**",
    )
    _write_page(
        tmp_path, "wide.md",
        frontmatter="title: wide\ntype: concept\ncovers:\n  - src/**",
    )
    return dm.load_pages(domain_dir=tmp_path)


def test_pages_for_touches_unions_across_touches(dm, touch_pages):
    # 두 touch → 각기 다른 페이지 union (analysis·core 둘 다·wide 는 둘 다 매치).
    hits = dm.pages_for_touches(
        ["src/analysis/x.py", "src/core/y.py"], touch_pages
    )
    assert _titles(hits) == ["analysis", "core", "wide"]


def test_pages_for_touches_dedups_same_page(dm, touch_pages):
    # 두 touch 가 같은 wide 페이지(src/**)에 걸려도 한 번만(dedup·페이지 path 기준).
    hits = dm.pages_for_touches(
        ["src/analysis/a.py", "src/analysis/b.py"], touch_pages
    )
    # analysis·wide 각각 1번씩(중복 없음).
    assert _titles(hits) == ["analysis", "wide"]
    assert len(hits) == 2


def test_pages_for_touches_dedup_order_stable(dm, touch_pages):
    # 발견 순서 안정 — 첫 touch 가 wide+analysis, 둘째가 wide+core 를 다시 걸어도
    # wide 는 첫 등장 자리(첫 touch)에 한 번만 남는다.
    hits = dm.pages_for_touches(
        ["src/analysis/a.py", "src/core/b.py"], touch_pages
    )
    titles_in_order = [p["title"] for p in hits]
    # 첫 touch: src/**(wide)·src/analysis/**(analysis) — load 순서(파일명 정렬)대로.
    # 둘째 touch: src/**(wide·이미 봄·skip)·src/core/**(core).
    assert titles_in_order[-1] == "core"        # core 는 마지막(둘째 touch 에서 새로)
    assert titles_in_order.count("wide") == 1   # dedup
    assert set(titles_in_order) == {"analysis", "core", "wide"}


def test_pages_for_touches_empty_returns_empty(dm, touch_pages):
    assert dm.pages_for_touches([], touch_pages) == []


def test_pages_for_touches_none_returns_empty(dm, touch_pages):
    assert dm.pages_for_touches(None, touch_pages) == []


def test_pages_for_touches_no_match_returns_empty(dm, touch_pages):
    assert dm.pages_for_touches(["other/unrelated.py"], touch_pages) == []


def test_pages_for_touches_dir_touch_matches_trailing_double_star(dm, tmp_path):
    # dir touch(src/analysis/)도 src/analysis/** covers 에 매치(T-0080 0-세그먼트 시맨틱).
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: a\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    pages = dm.load_pages(domain_dir=tmp_path)
    assert _titles(dm.pages_for_touches(["src/analysis"], pages)) == ["a"]


def test_pages_for_touches_loads_pages_when_not_injected(dm, tmp_path, monkeypatch):
    # pages 미주입 → load_pages() 호출 (모듈 전역 DOMAIN_DIR 을 tmp 로 재바인딩).
    _write_page(
        tmp_path, "p.md",
        frontmatter="title: 데모\ntype: concept\ncovers:\n  - src/x/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    hits = dm.pages_for_touches(["src/x/y.py"])
    assert _titles(hits) == ["데모"]


def test_pages_for_touches_drops_non_string_touch(dm, touch_pages):
    # 비-문자열 touch 원소(오기)는 방어적으로 건너뛴다(crash 0).
    hits = dm.pages_for_touches(["src/analysis/x.py", 123, None], touch_pages)
    assert _titles(hits) == ["analysis", "wide"]


def test_pages_for_touches_strips_whitespace(dm, touch_pages):
    # 선행/후행 공백이 붙은 touch(직접 API 호출 시)도 strip 후 매칭(T-0089·uncovered_paths
    # 동형). strip 없으면 covers 글롭이 정확 경로로만 매치해 silent-miss 한다.
    hits = dm.pages_for_touches(["  src/analysis/x.py  ", "\tsrc/core/y.py\n"], touch_pages)
    assert _titles(hits) == ["analysis", "core", "wide"]


def test_pages_for_touches_skips_blank_touch(dm, touch_pages):
    # 공백/빈 touch 원소는 strip 후 건너뛴다(uncovered_paths 동형·crash 0·매치 0).
    hits = dm.pages_for_touches(["   ", "", "src/analysis/x.py"], touch_pages)
    assert _titles(hits) == ["analysis", "wide"]


# ── affected CLI ─────────────────────────────────────────────────────────────


def test_affected_touches_renders_matched(dm, tmp_path, monkeypatch, capsys):
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: 분석페이지\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["affected", "--touches", "src/analysis/x.py"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "분석페이지" in out
    assert "src/analysis/**" in out


def test_affected_touches_comma_separated_union(dm, tmp_path, monkeypatch, capsys):
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: A\ntype: concept\ncovers:\n  - src/a/**",
    )
    _write_page(
        tmp_path, "b.md",
        frontmatter="title: B\ntype: concept\ncovers:\n  - src/b/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["affected", "--touches", "src/a/x.py, src/b/y.py"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "A" in out and "B" in out


def test_affected_no_match_prints_none_message(dm, tmp_path, monkeypatch, capsys):
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: A\ntype: concept\ncovers:\n  - src/a/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["affected", "--touches", "other/unrelated.py"])
    assert rc == 0
    assert "(영향 domain 페이지 없음)" in capsys.readouterr().out


def test_affected_ticket_reads_touches_via_board(dm, tmp_path, monkeypatch, capsys):
    # --ticket 경로: _touches_from_ticket(board.find_ticket+load_ticket) 를 대역으로
    # 가로채 ticket touches 를 hermetic 하게 주입한다(실 board 미접촉).
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: 티켓영향\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(
        dm, "_touches_from_ticket",
        lambda tid: ["src/analysis/x.py"] if tid == "T-9999" else [],
    )
    rc = dm.main(["affected", "--ticket", "T-9999"])
    assert rc == 0
    assert "티켓영향" in capsys.readouterr().out


def test_affected_ticket_no_touches_prints_none(dm, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "_touches_from_ticket", lambda tid: [])
    rc = dm.main(["affected", "--ticket", "T-0000"])
    assert rc == 0
    assert "(영향 domain 페이지 없음)" in capsys.readouterr().out


def test_affected_requires_ticket_or_touches(dm):
    # 둘 다 없으면 argparse 가 SystemExit (mutually exclusive·required).
    with pytest.raises(SystemExit):
        dm.main(["affected"])


# ── _touches_from_ticket strip (silent-miss 방어·T-0081 후속) ─────────────────


class _FakeBoard:
    """find_ticket/load_ticket 를 흉내내는 hermetic board 대역.

    fake ticket 경로(`/fake/...`)에는 주입한 `touches` frontmatter 를, 실제 .md 페이지
    경로에는 진짜 board.load_ticket 파싱을 위임한다 — 그래야 같은 `_load_board` seam 을
    공유하는 `_touches_from_ticket`(ticket)·`parse_page`(페이지)가 한 대역으로 동작한다.
    """

    def __init__(self, touches, real_board=None):
        self._touches = touches
        self._real = real_board

    def find_ticket(self, ticket_id):
        return ("open", Path(f"/fake/{ticket_id}.md"))

    def load_ticket(self, path):
        if str(path).startswith("/fake/"):
            return ({"touches": self._touches}, "body")
        return self._real.load_ticket(path)


def test_touches_from_ticket_strips_list_whitespace_and_drops_empty(dm, monkeypatch):
    # frontmatter touches 리스트 원소의 앞뒤 공백 strip·빈 토큰 drop — "  src/b.py"
    # 같은 값이 매칭에서 조용히 누락되지 않게 (--touches CLI 와 동형).
    monkeypatch.setattr(
        dm, "_load_board",
        lambda: _FakeBoard(["src/a.py", " src/b.py", "  ", "\tsrc/c.py "]),
    )
    assert dm._touches_from_ticket("T-9999") == ["src/a.py", "src/b.py", "src/c.py"]


def test_touches_from_ticket_strips_scalar_string(dm, monkeypatch):
    # 스칼라 문자열 touches(list 들여쓰기 누락)도 strip — 빈 문자열은 [] 로.
    monkeypatch.setattr(dm, "_load_board", lambda: _FakeBoard("  src/x.py  "))
    assert dm._touches_from_ticket("T-9999") == ["src/x.py"]
    monkeypatch.setattr(dm, "_load_board", lambda: _FakeBoard("   "))
    assert dm._touches_from_ticket("T-9999") == []


def test_affected_ticket_whitespace_touches_match_both(dm, tmp_path, monkeypatch, capsys):
    # 회귀(end-to-end): ticket touches ["src/a.py", " src/b.py"](둘째 앞 공백)이 실제
    # _touches_from_ticket 에서 strip 돼 cmd_affected 가 두 페이지 모두 매칭한다 (strip
    # 전이면 " src/b.py" 가 조용히 누락됐다). _load_board 만 대역(ticket touches 주입·
    # 페이지 파싱은 진짜 board 에 위임)하고 strip 로직은 실제 코드를 거친다.
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: A\ntype: concept\ncovers:\n  - src/a.py",
    )
    _write_page(
        tmp_path, "b.md",
        frontmatter="title: B\ntype: concept\ncovers:\n  - src/b.py",
    )
    real_board = dm._load_board()
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(
        dm, "_load_board",
        lambda: _FakeBoard(["src/a.py", " src/b.py"], real_board=real_board),
    )
    rc = dm.main(["affected", "--ticket", "T-9999"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "A" in out and "B" in out


# ── main(list) ───────────────────────────────────────────────────────────────


def test_main_list_empty(dm, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path / "empty")
    rc = dm.main(["list"])
    assert rc == 0
    assert "(domain 페이지 없음)" in capsys.readouterr().out


def test_main_list_renders_pages(dm, tmp_path, monkeypatch, capsys):
    _write_page(
        tmp_path, "p.md",
        frontmatter=(
            "title: 데모페이지\ntype: guide\n"
            "covers:\n  - src/x/**\nupdated: 2026-06-19"
        ),
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "데모페이지" in out
    assert "guide" in out
    assert "src/x/**" in out
    assert "2026-06-19" in out


# ── covers_to_pathspec (글롭 → git pathspec·보수적 prefix) ───────────────────


def test_covers_to_pathspec_double_star_dir(dm):
    assert dm.covers_to_pathspec("src/analysis/**") == "src/analysis"


def test_covers_to_pathspec_single_star_segment(dm):
    assert dm.covers_to_pathspec("src/*.py") == "src"


def test_covers_to_pathspec_literal_no_wildcard(dm):
    # 와일드카드 없음 = 글롭 전체가 리터럴 pathspec.
    assert dm.covers_to_pathspec("a/b.py") == "a/b.py"


def test_covers_to_pathspec_trailing_slash_stripped(dm):
    # 첫 와일드카드 전 prefix 의 trailing 슬래시 제거.
    assert dm.covers_to_pathspec("src/core/*") == "src/core"


def test_covers_to_pathspec_empty_prefix_returns_none(dm):
    # 글롭이 와일드카드로 시작 = 좁힐 prefix 없음 → None(skip).
    assert dm.covers_to_pathspec("**/x.py") is None
    assert dm.covers_to_pathspec("*.py") is None


# ── page_stale (git 기반·git_runner 주입·fail-soft None) ──────────────────────
# 주입 git_runner 는 `(argv) -> (rc, out)` — argv 는 `["log","-1","--format=%cI","--",ps…]`.


def _fixed_git(out, rc=0):
    """고정 (rc, out) 을 돌려주는 hermetic git_runner 대역."""
    def runner(argv):
        return rc, out
    return runner


def test_page_stale_newer_commit_is_true(dm):
    # 최신 covers 커밋(2026-06-20) > updated(2026-06-19) → stale True.
    page = {"path": Path("/d/p.md"), "title": "p", "covers": ["src/x/**"],
            "updated": "2026-06-19"}
    runner = _fixed_git("2026-06-20T10:00:00+09:00\n")
    assert dm.page_stale(page, git_runner=runner) is True


def test_page_stale_older_commit_is_false(dm):
    # covers 커밋(2026-06-18) <= updated(2026-06-19) → fresh False.
    page = {"path": Path("/d/p.md"), "title": "p", "covers": ["src/x/**"],
            "updated": "2026-06-19"}
    runner = _fixed_git("2026-06-18T10:00:00+09:00\n")
    assert dm.page_stale(page, git_runner=runner) is False


def test_page_stale_same_day_commit_is_false(dm):
    # 같은 날 커밋(updated 와 동일 date)은 stale 아님(> 비교·date 단위).
    page = {"path": Path("/d/p.md"), "covers": ["src/x/**"], "updated": "2026-06-19"}
    runner = _fixed_git("2026-06-19T23:59:00+09:00\n")
    assert dm.page_stale(page, git_runner=runner) is False


def test_page_stale_updated_as_date_object(dm):
    # updated 가 yaml.safe_load 의 datetime.date 객체로 와도 비교한다.
    import datetime
    page = {"path": Path("/d/p.md"), "covers": ["src/x/**"],
            "updated": datetime.date(2026, 6, 19)}
    assert dm.page_stale(page, git_runner=_fixed_git("2026-06-20T00:00:00Z\n")) is True
    assert dm.page_stale(page, git_runner=_fixed_git("2026-06-18T00:00:00Z\n")) is False


def test_page_stale_git_error_rc_returns_none(dm):
    # git rc≠0(에러·미추적) → unknown None.
    page = {"path": Path("/d/p.md"), "covers": ["src/x/**"], "updated": "2026-06-19"}
    assert dm.page_stale(page, git_runner=_fixed_git("", rc=1)) is None


def test_page_stale_git_empty_output_returns_none(dm):
    # git rc 0 이지만 빈 출력(커밋 0·미추적) → unknown None.
    page = {"path": Path("/d/p.md"), "covers": ["src/x/**"], "updated": "2026-06-19"}
    assert dm.page_stale(page, git_runner=_fixed_git("\n")) is None


def test_page_stale_empty_covers_returns_none(dm):
    # covers 빈(코드-무관 개념) → unknown None(평가 대상 없음·git 호출도 안 함).
    page = {"path": Path("/d/p.md"), "covers": [], "updated": "2026-06-19"}
    called = []

    def runner(argv):
        called.append(argv)
        return 0, "2026-06-20T00:00:00Z\n"

    assert dm.page_stale(page, git_runner=runner) is None
    assert called == []  # git 호출하지 않음(빈 covers 조기 반환).


def test_page_stale_broken_updated_returns_none(dm):
    # updated 파싱 실패(깨짐·부재) → unknown None.
    for bad in ("not-a-date", "", None):
        page = {"path": Path("/d/p.md"), "covers": ["src/x/**"], "updated": bad}
        assert dm.page_stale(page, git_runner=_fixed_git("2026-06-20T00:00:00Z\n")) is None


def test_page_stale_all_pathspecs_empty_returns_none(dm):
    # 모든 covers 글롭이 와일드카드로 시작(빈 pathspec) → None(좁힐 prefix 없음).
    page = {"path": Path("/d/p.md"), "covers": ["**/x.py", "*.md"],
            "updated": "2026-06-19"}
    assert dm.page_stale(page, git_runner=_fixed_git("2026-06-20T00:00:00Z\n")) is None


def test_page_stale_runner_raises_returns_none(dm):
    # 주입 runner 가 raise 해도 fail-soft None(crash 0).
    def runner(argv):
        raise RuntimeError("boom")

    page = {"path": Path("/d/p.md"), "covers": ["src/x/**"], "updated": "2026-06-19"}
    assert dm.page_stale(page, git_runner=runner) is None


def test_page_stale_passes_pathspecs_to_git(dm):
    # covers 의 여러 글롭이 prefix pathspec 으로 변환돼 단일 git log 에 합쳐 전달된다.
    captured = {}

    def runner(argv):
        captured["argv"] = argv
        return 0, "2026-06-20T00:00:00Z\n"

    page = {"path": Path("/d/p.md"),
            "covers": ["src/analysis/**", "tools/exact.py"], "updated": "2026-06-19"}
    dm.page_stale(page, git_runner=runner)
    argv = captured["argv"]
    assert argv[:4] == ["log", "-1", "--format=%cI", "--"]
    assert argv[4:] == ["src/analysis", "tools/exact.py"]


# ── domain list ⚠ 표시 (stale 마커) ──────────────────────────────────────────


def test_list_marks_stale_page_with_warning(dm, tmp_path, monkeypatch, capsys):
    # stale(True) 페이지 줄 앞에 ⚠. page_stale 을 대역으로 가로채 결정적으로 stale 화.
    _write_page(
        tmp_path, "p.md",
        frontmatter="title: 낡은페이지\ntype: concept\ncovers:\n  - src/x/**\nupdated: 2026-06-19",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: True)
    rc = dm.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "⚠" in out
    assert "낡은페이지" in out


def test_list_fresh_and_unknown_have_no_warning(dm, tmp_path, monkeypatch, capsys):
    # False(fresh)·None(unknown) 은 ⚠ 무표시.
    _write_page(
        tmp_path, "fresh.md",
        frontmatter="title: 최신\ntype: concept\ncovers:\n  - src/x/**\nupdated: 2026-06-19",
    )
    _write_page(
        tmp_path, "unk.md",
        frontmatter="title: 미상\ntype: concept",  # covers 없음 = unknown
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(
        dm, "page_stale",
        lambda page, **kw: None if not page["covers"] else False,
    )
    rc = dm.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "⚠" not in out
    assert "최신" in out and "미상" in out


def test_affected_marks_stale_page_with_warning(dm, tmp_path, monkeypatch, capsys):
    # affected 출력도 stale 페이지에 ⚠.
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: 영향낡음\ntype: concept\ncovers:\n  - src/analysis/**\nupdated: 2026-06-19",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: True)
    rc = dm.main(["affected", "--touches", "src/analysis/x.py"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "⚠" in out and "영향낡음" in out


# ── domain lint (orphan / oversized / stale / clean·exit 0) ───────────────────


def test_lint_clean_prints_ok(dm, tmp_path, monkeypatch, capsys):
    # 인링크 상호참조 + 작은 본문 + non-stale → clean.
    _write_page(
        tmp_path, "alpha.md",
        frontmatter="title: 알파\ntype: concept",
        body="\n[[beta]] 를 본다.\n",
    )
    _write_page(
        tmp_path, "beta.md",
        frontmatter="title: 베타\ntype: concept",
        body="\n[[alpha]] 로 돌아간다.\n",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    rc = dm.main(["lint"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "✓ domain freshness 양호" in out


def test_lint_detects_orphan(dm, tmp_path, monkeypatch, capsys):
    # orphan = 다른 페이지에서 인링크 0. hub 는 island 를 안 가리킨다.
    _write_page(
        tmp_path, "hub.md",
        frontmatter="title: 허브\ntype: concept",
        body="\n혼자 있다.\n",
    )
    _write_page(
        tmp_path, "island.md",
        frontmatter="title: 외딴섬\ntype: concept",
        body="\n[[hub]] 를 가리킨다(나로의 인링크는 0).\n",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    rc = dm.main(["lint"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan" in out
    assert "외딴섬" in out
    # hub 는 island 가 인링크하므로 orphan 아님.
    assert "허브" not in out


def test_lint_self_reference_does_not_save_from_orphan(dm, tmp_path, monkeypatch, capsys):
    # 자기참조(자기 body 의 자기링크)는 인링크로 안 침 → 여전히 orphan.
    # peer 를 둬 ≥2 페이지(T-0097 single-page skip 회피) — solo 는 self-ref 만이라 여전히 고립.
    _write_page(
        tmp_path, "solo.md",
        frontmatter="title: 솔로\ntype: concept",
        body="\n나는 [[solo]] 다(자기참조). [[peer]] 도 본다.\n",
    )
    _write_page(
        tmp_path, "peer.md",
        frontmatter="title: 피어\ntype: concept",
        body="\n독립 — solo 를 안 가리킨다.\n",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    rc = dm.main(["lint"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan" in out and "솔로" in out
    assert "피어" not in out  # peer 는 solo 가 인링크 → orphan 아님


def test_lint_title_self_reference_does_not_save_from_orphan(dm, tmp_path, monkeypatch, capsys):
    # 자기 *title* 로의 자기참조(`[[알파]]`·stem=alpha·title=알파)도 인링크로 안 침 → orphan.
    # (slug 자기참조는 위 test 가 커버. 여기선 title 자기참조 갭 — slug 만 제외하던 false-negative.)
    # peer 로 ≥2 페이지(T-0097) — alpha 는 title 자기참조뿐이라 여전히 고립.
    _write_page(
        tmp_path, "alpha.md",
        frontmatter="title: 알파\ntype: concept",
        body="\n나는 [[알파]] 다(title 자기참조). [[peer]] 도.\n",
    )
    _write_page(
        tmp_path, "peer.md",
        frontmatter="title: 피어\ntype: concept",
        body="\n독립 — alpha 를 안 가리킨다.\n",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    rc = dm.main(["lint"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan" in out and "알파" in out


def test_lint_orphan_inlink_by_title_counts(dm, tmp_path, monkeypatch, capsys):
    # title 표기(`[[베타]]`)로 인링크해도 orphan 아님(슬러그/title 둘 다 인정).
    _write_page(
        tmp_path, "alpha.md",
        frontmatter="title: 알파\ntype: concept",
        body="\n[[베타]] 를 title 로 가리킨다.\n",
    )
    _write_page(
        tmp_path, "beta.md",
        frontmatter="title: 베타\ntype: concept",
        body="\n[[알파]] 로 돌아간다.\n",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    rc = dm.main(["lint"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "✓ domain freshness 양호" in out


def test_lint_single_page_skips_orphan(dm, tmp_path, monkeypatch, capsys):
    # T-0097 — 페이지가 1개뿐이면 orphan 판정 skip (peer 가 없어 자연 고립·perpetual noise 제거).
    _write_page(
        tmp_path, "solo.md",
        frontmatter="title: 솔로\ntype: concept",
        body="\n혼자 있는 첫 페이지.\n",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    rc = dm.main(["lint"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan" not in out                  # 단일 페이지 → orphan finding 없음
    assert "✓ domain freshness 양호" in out
    # sensitivity — 페이지가 2개(서로 안 링크)가 되면 orphan 이 다시 평가된다.
    _write_page(
        tmp_path, "second.md",
        frontmatter="title: 둘째\ntype: concept",
        body="\n나도 혼자 — solo 를 안 가리킨다.\n",
    )
    rc2 = dm.main(["lint"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "orphan" in out2                      # ≥2 페이지·상호 무링크 → orphan 재등장


def test_lint_detects_oversized(dm, tmp_path, monkeypatch, capsys):
    # 본문 라인수 > 임계 → oversized finding(상호참조로 orphan 은 회피).
    big_body = "\n" + "\n".join(f"line {i}" for i in range(20)) + "\n[[other]]\n"
    _write_page(
        tmp_path, "big.md",
        frontmatter="title: 큰페이지\ntype: concept",
        body=big_body,
    )
    _write_page(
        tmp_path, "other.md",
        frontmatter="title: 기타\ntype: concept",
        body="\n[[big]]\n",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    # 임계를 10 으로 낮춰 hermetic 하게 oversized 유발(상수 기본 200 의존 안 함).
    monkeypatch.setattr(dm, "OVERSIZED_LINES", 10)
    rc = dm.main(["lint"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "oversized" in out and "큰페이지" in out


def test_lint_oversized_threshold_is_strict_gt(dm, tmp_path, monkeypatch):
    # 임계 == 라인수는 finding 아님(> 비교). lint_pages 직접 호출(임계 인자 주입).
    body = "\n".join(f"line {i}" for i in range(5))  # 5줄
    page = _write_page(
        tmp_path, "p.md",
        frontmatter="title: P\ntype: concept",
        body=body,
    )
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    pages = dm.load_pages(domain_dir=tmp_path)
    # 임계 5 → 5줄은 > 아니라 oversized 없음(단일 페이지 — T-0097 로 orphan 도 skip).
    findings = dm.lint_pages(pages, oversized_lines=5)
    assert not any(k == "oversized" for k, _p, _d in findings)
    # 임계 4 → 5줄 > 4 라 oversized.
    findings = dm.lint_pages(pages, oversized_lines=4)
    assert any(k == "oversized" for k, _p, _d in findings)


def test_lint_detects_stale_finding(dm, tmp_path, monkeypatch, capsys):
    # page_stale==True → stale finding (상호참조로 orphan 회피).
    _write_page(
        tmp_path, "stale.md",
        frontmatter="title: 낡음\ntype: concept\ncovers:\n  - src/x/**\nupdated: 2026-06-19",
        body="\n[[peer]]\n",
    )
    _write_page(
        tmp_path, "peer.md",
        frontmatter="title: 동료\ntype: concept",
        body="\n[[stale]]\n",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    # stale 슬러그만 stale True·나머지 None.
    monkeypatch.setattr(
        dm, "page_stale",
        lambda page, **kw: True if dm.page_slug(page) == "stale" else None,
    )
    rc = dm.main(["lint"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stale" in out and "낡음" in out
    # 동료는 stale 아님.
    assert "동료" not in out


def test_lint_unknown_stale_is_not_finding(dm, tmp_path, monkeypatch):
    # page_stale==None(unknown)은 stale finding 아님.
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: A\ntype: concept",
        body="\n[[b]]\n",
    )
    _write_page(
        tmp_path, "b.md",
        frontmatter="title: B\ntype: concept",
        body="\n[[a]]\n",
    )
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: None)
    pages = dm.load_pages(domain_dir=tmp_path)
    findings = dm.lint_pages(pages)
    assert not any(k == "stale" for k, _p, _d in findings)


def test_lint_empty_domain_is_clean(dm, tmp_path, monkeypatch, capsys):
    # 페이지 0 → clean(스캔 대상 없음).
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path / "empty")
    rc = dm.main(["lint"])
    assert rc == 0
    assert "✓ domain freshness 양호" in capsys.readouterr().out


def test_page_slug_is_file_stem(dm, tmp_path):
    page = {"path": tmp_path / "dual-gate-review.md", "title": "이중 게이트"}
    assert dm.page_slug(page) == "dual-gate-review"


# ── uncovered_paths (coverage gap — 담당 covers 없는 touch·T-0084) ────────────


@pytest.fixture
def gap_pages(dm, tmp_path):
    # analysis 페이지가 src/analysis/** 를 covers — 그 밖 경로는 uncovered.
    _write_page(
        tmp_path, "analysis.md",
        frontmatter="title: analysis\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    return dm.load_pages(domain_dir=tmp_path)


def test_uncovered_paths_excludes_covered(dm, gap_pages):
    # covered 경로(src/analysis/x.py)는 gap 아님 → 제외.
    assert dm.uncovered_paths(["src/analysis/x.py"], gap_pages) == []


def test_uncovered_paths_includes_uncovered(dm, gap_pages):
    # 어느 covers 에도 안 잡힌 경로 → gap 으로 포함.
    assert dm.uncovered_paths(["src/core/y.py"], gap_pages) == ["src/core/y.py"]


def test_uncovered_paths_mixed_keeps_only_gaps(dm, gap_pages):
    # covered + uncovered 혼재 → uncovered 만 (covered 제외·발견 순서 보존).
    gaps = dm.uncovered_paths(
        ["src/analysis/x.py", "src/core/y.py", "docs/readme.md"], gap_pages
    )
    assert gaps == ["src/core/y.py", "docs/readme.md"]


def test_uncovered_paths_dedups(dm, gap_pages):
    # 같은 uncovered 경로가 중복돼도 한 번만(dedup·순서 보존).
    assert dm.uncovered_paths(["src/core/y.py", "src/core/y.py"], gap_pages) == ["src/core/y.py"]


def test_uncovered_paths_empty_returns_empty(dm, gap_pages):
    assert dm.uncovered_paths([], gap_pages) == []
    assert dm.uncovered_paths(None, gap_pages) == []


def test_uncovered_paths_drops_non_string_and_blank(dm, gap_pages):
    # 비-문자열·빈/공백 touch 는 방어적으로 건너뛴다(crash 0).
    gaps = dm.uncovered_paths(["src/core/y.py", 123, None, "  ", ""], gap_pages)
    assert gaps == ["src/core/y.py"]


def test_uncovered_paths_no_pages_all_uncovered(dm):
    # domain/ 비면(페이지 0) 모든 touch 가 uncovered(담당 페이지 0).
    assert dm.uncovered_paths(["a/b.py", "c/d.py"], []) == ["a/b.py", "c/d.py"]


def test_uncovered_paths_loads_pages_when_not_injected(dm, tmp_path, monkeypatch):
    # pages 미주입 → load_pages()(모듈 전역 DOMAIN_DIR 재바인딩).
    _write_page(
        tmp_path, "p.md",
        frontmatter="title: p\ntype: concept\ncovers:\n  - src/x/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    # src/x/y.py 는 covered → gap 아님; other/z.py 는 uncovered.
    assert dm.uncovered_paths(["src/x/y.py", "other/z.py"]) == ["other/z.py"]


# ── capture CLI (채록 surface·영향 페이지+⚠+coverage gap·read-only exit0·T-0084) ─


def test_capture_touches_renders_affected_and_gap(dm, tmp_path, monkeypatch, capsys):
    # 영향 페이지 + coverage gap 둘 다 출력 — 매칭은 영향 절, 미매칭은 gap 절.
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: 분석페이지\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["capture", "--touches", "src/analysis/x.py, src/core/new.py"])
    assert rc == 0
    out = capsys.readouterr().out
    # 영향 페이지 절.
    assert "영향 페이지" in out
    assert "분석페이지" in out
    assert "src/analysis/**" in out
    # coverage gap 절(미매칭 경로).
    assert "coverage gap" in out
    assert "src/core/new.py" in out
    # covered 경로는 gap 으로 나오지 않는다.
    assert "src/analysis/x.py" not in out.split("coverage gap")[1]


def test_capture_stale_page_marked(dm, tmp_path, monkeypatch, capsys):
    # 영향 페이지가 stale 이면 ⚠ 마커가 붙는다(T-0082 _stale_marker 재사용).
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: 스테일\ntype: concept\ncovers:\n  - src/analysis/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    # page_stale 를 True 로 강제 — git 무의존 hermetic(_stale_marker 가 ⚠ 를 붙인다).
    monkeypatch.setattr(dm, "page_stale", lambda page, **kw: True)
    rc = dm.main(["capture", "--touches", "src/analysis/x.py"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "⚠" in out
    assert "스테일" in out


def test_capture_no_change_prints_none_message(dm, tmp_path, monkeypatch, capsys):
    # 영향 페이지·gap 둘 다 없으면(touches 비어) "(채록할 domain 변화 없음)".
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: A\ntype: concept\ncovers:\n  - src/a/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    # 빈 토큰만 → touches 비어 → 영향 0·gap 0.
    rc = dm.main(["capture", "--touches", "  ,  "])
    assert rc == 0
    assert "(채록할 domain 변화 없음)" in capsys.readouterr().out


def test_capture_gap_only_omits_affected_section(dm, tmp_path, monkeypatch, capsys):
    # 매칭 0·gap 만 있으면 영향 페이지 절은 생략하고 gap 절만 출력.
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: A\ntype: concept\ncovers:\n  - src/a/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["capture", "--touches", "src/unrelated/z.py"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "coverage gap" in out
    assert "src/unrelated/z.py" in out
    assert "영향 페이지" not in out


def test_capture_affected_only_omits_gap_section(dm, tmp_path, monkeypatch, capsys):
    # gap 0(모든 touch 가 covered)·영향만 있으면 gap 절 생략.
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: A\ntype: concept\ncovers:\n  - src/a/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["capture", "--touches", "src/a/x.py"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "영향 페이지" in out
    assert "coverage gap" not in out


def test_capture_tickets_aggregates_touches(dm, tmp_path, monkeypatch, capsys):
    # --tickets T-a,T-b — 각 ticket touches 를 집계(union)해 채록 대상을 띄운다.
    _write_page(
        tmp_path, "a.md",
        frontmatter="title: A페이지\ntype: concept\ncovers:\n  - src/a/**",
    )
    _write_page(
        tmp_path, "b.md",
        frontmatter="title: B페이지\ntype: concept\ncovers:\n  - src/b/**",
    )
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    # _touches_from_ticket 를 hermetic 가로채 — ticket 별 touches 주입(실 board 미접촉).
    ticket_touches = {"T-1": ["src/a/x.py"], "T-2": ["src/b/y.py", "src/gap/z.py"]}
    monkeypatch.setattr(
        dm, "_touches_from_ticket", lambda tid: ticket_touches.get(tid, [])
    )
    rc = dm.main(["capture", "--tickets", "T-1, T-2"])
    assert rc == 0
    out = capsys.readouterr().out
    # 두 ticket touches union → 두 페이지 모두 영향.
    assert "A페이지" in out and "B페이지" in out
    # T-2 의 미매칭 touch 는 gap.
    assert "src/gap/z.py" in out


def test_capture_tickets_dedups_aggregated_touches(dm, tmp_path, monkeypatch):
    # 두 ticket 이 같은 touch 를 가져도 집계 시 dedup(순서 보존).
    monkeypatch.setattr(
        dm, "_touches_from_ticket",
        lambda tid: ["src/x.py", "src/y.py"] if tid == "T-1" else ["src/x.py", "src/z.py"],
    )
    assert dm._touches_from_tickets("T-1,T-2") == ["src/x.py", "src/y.py", "src/z.py"]


def test_capture_tickets_blank_token_skipped(dm, monkeypatch):
    # 빈 토큰(콤마 잉여)은 건너뛴다.
    calls = []

    def fake(tid):
        calls.append(tid)
        return ["src/x.py"]

    monkeypatch.setattr(dm, "_touches_from_ticket", fake)
    assert dm._touches_from_tickets("T-1, , T-2,") == ["src/x.py"]
    assert calls == ["T-1", "T-2"]  # 빈 토큰엔 _touches_from_ticket 호출 안 함.


def test_capture_requires_tickets_or_touches(dm):
    # 둘 다 없으면 argparse SystemExit (mutually exclusive·required).
    with pytest.raises(SystemExit):
        dm.main(["capture"])


# ── capture-draft (researcher 조사 prose → domain 초안 scaffold·git 무조작·T-0167) ──
# researcher 의 조사 prose 를 domain draft 페이지(status:draft)로 scaffold. no-auto-commit
# 3중: (1) frontmatter status:draft 가 index 제외 진실 (2) git add/commit 절대 호출 안 함
# (3) promote 명령 부재 = draft→정식이 사람 손. prose 는 verbatim 배치(요약/구조화 금지).


def _read_draft(domain_dir: Path, slug: str) -> str:
    """tmp domain dir 의 <slug>.draft.md 본문 전체를 읽는다(frontmatter+body)."""
    return (domain_dir / f"{slug}.draft.md").read_text(encoding="utf-8")


def test_capture_draft_writes_scaffold_with_draft_status(dm, tmp_path):
    # capture-draft 가 <slug>.draft.md 를 status:draft frontmatter + title + source 로 생성.
    path = dm.write_draft_page(
        "Factor Beta Pipeline",
        covers=["src/analysis/**"],
        source=None,
        domain_dir=tmp_path,
        today="2026-06-27",
    )
    assert path == tmp_path / "factor-beta-pipeline.draft.md"
    text = path.read_text(encoding="utf-8")
    assert 'title: "Factor Beta Pipeline"' in text
    assert "status: draft" in text          # index 제외 진실
    assert "derived: false" in text         # 사람 author
    assert "updated: 2026-06-27" in text
    assert "covers: [src/analysis/**]" in text
    assert 'source: "(none)"' in text       # provenance (미지정)
    # _template.md 골격 절.
    assert "# Factor Beta Pipeline" in text
    assert "## 조사 결과" in text


def test_capture_draft_source_file_prose_verbatim(dm, tmp_path):
    # --source <file>: 파일 prose 가 ## 조사 결과 아래 *그대로*(verbatim) 배치된다.
    src = tmp_path / "research.txt"
    prose = "factor beta 는 X 로 추정.\n  - 근거 1\n  - 근거 2 (미해결: Y)\n"
    src.write_text(prose, encoding="utf-8")
    path = dm.write_draft_page(
        "베타", slug="beta", source=str(src), domain_dir=tmp_path,
    )
    text = path.read_text(encoding="utf-8")
    # prose 가 verbatim(요약/구조화 없이) 본문에 들어간다.
    assert prose.strip() in text
    assert "근거 1" in text and "근거 2 (미해결: Y)" in text
    assert 'source: "' in text and "research.txt" in text  # provenance 파일경로


def test_capture_draft_source_stdin(dm, tmp_path, monkeypatch):
    # --source -: stdin prose 를 읽어 verbatim 배치(CLI main 경로).
    import io
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("stdin 으로 들어온 조사 결과 prose.\n"))
    rc = dm.main(["capture-draft", "--title", "Stdin Page", "--slug", "stdin-page", "--source", "-"])
    assert rc == 0
    text = _read_draft(tmp_path, "stdin-page")
    assert "stdin 으로 들어온 조사 결과 prose." in text
    assert "status: draft" in text


def test_capture_draft_no_covers_todo_placeholder(dm, tmp_path):
    # --covers 미지정 → 빈 covers + 본문 TODO placeholder.
    path = dm.write_draft_page("미정", slug="undef", source=None, domain_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "covers: []" in text
    assert "TODO PM: covers 글롭" in text


def test_capture_draft_no_source_todo_placeholder(dm, tmp_path):
    # --source 미지정 → 빈 본문 TODO placeholder(조사 prose 자리).
    path = dm.write_draft_page("빈본문", slug="empty-body", source=None, domain_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "TODO PM: 조사 prose" in text


def test_capture_draft_default_type_research(dm, tmp_path):
    # --type 미지정 → research 기본.
    path = dm.write_draft_page("기본타입", slug="t", source=None, domain_dir=tmp_path)
    assert "type: research" in path.read_text(encoding="utf-8")
    # 명시 type 은 그대로.
    path2 = dm.write_draft_page("개념", slug="c", ptype="concept", source=None, domain_dir=tmp_path)
    assert "type: concept" in path2.read_text(encoding="utf-8")


def test_capture_draft_slug_from_title_when_unspecified(dm, tmp_path):
    # --slug 미지정 → title 에서 kebab 도출.
    path = dm.write_draft_page("Hello World v2", source=None, domain_dir=tmp_path)
    assert path.name == "hello-world-v2.draft.md"


def test_capture_draft_non_ascii_title_falls_back_to_draft_slug(dm, tmp_path):
    # 순한글 title(영숫자 0) → 빈 슬러그 → 기본 'draft' 슬러그 대체(crash 0).
    path = dm.write_draft_page("순한글제목", source=None, domain_dir=tmp_path)
    assert path.name == "draft.draft.md"


def test_capture_draft_no_auto_commit_git_status_unchanged(dm, tmp_path):
    # **no-auto-commit (검토 게이트)**: 임시 git repo 에서 capture-draft 후 staging 변화 0.
    # capture-draft 는 파일을 *쓰되* git add/commit 을 절대 호출하지 않는다 — untracked 로만
    # 남아 사람이 검토(promote)할 때까지 staged 변화가 없다.
    import subprocess
    repo = tmp_path / "repo"
    domain_dir = repo / "domain"
    domain_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    dm.write_draft_page("드래프트", slug="d", source=None, domain_dir=domain_dir)

    # staged(인덱스) 변화 0 — git add 가 호출되지 않았다.
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert staged == ""
    # 파일은 untracked 로 존재(쓰이긴 했다·git 은 add 안 한 디렉토리를 `-u` 로 펼친다).
    untracked = subprocess.run(
        ["git", "status", "--porcelain", "-uall"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "?? domain/d.draft.md" in untracked
    # 커밋도 0(HEAD 부재).
    rc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True).returncode
    assert rc != 0  # 커밋이 없다.


def test_capture_draft_excluded_from_load_pages_index(dm, tmp_path):
    # **draft index 제외**: status:draft scaffold 는 load_pages 가 빼고 정식만 반환.
    dm.write_draft_page("드래프트", slug="drafted", source=None, domain_dir=tmp_path)
    _write_page(
        tmp_path, "official.md",
        frontmatter="title: 정식\ntype: concept\ncovers:\n  - src/x/**",
    )
    pages = dm.load_pages(domain_dir=tmp_path)
    titles = [p["title"] for p in pages]
    assert titles == ["정식"]  # draft 제외·정식만.


def test_load_pages_excludes_draft_status_marker_not_suffix(dm, tmp_path):
    # 필터 기준 = frontmatter status:draft 이지 .draft.md 파일명이 아니다.
    # (a) status:draft 인데 평범한 .md 이름 → 제외.
    _write_page(
        tmp_path, "plain-named.md",
        frontmatter="title: 초안A\ntype: research\nstatus: draft",
    )
    # (b) .draft.md suffix 인데 status 부재(정식) → 포함(suffix 는 필터 아님).
    _write_page(
        tmp_path, "suffixed.draft.md",
        frontmatter="title: 정식B\ntype: concept",
    )
    titles = [p["title"] for p in dm.load_pages(domain_dir=tmp_path)]
    assert titles == ["정식B"]  # status 기준 — A(draft) 제외·B(suffix지만 정식) 포함.


def test_parse_page_status_field(dm, tmp_path):
    # parse_page 가 status frontmatter 를 읽는다(부재 → None = 정식).
    drafted = _write_page(
        tmp_path, "d.md", frontmatter="title: D\ntype: research\nstatus: draft",
    )
    official = _write_page(
        tmp_path, "o.md", frontmatter="title: O\ntype: concept",
    )
    assert dm.parse_page(drafted)["status"] == "draft"
    assert dm.parse_page(official)["status"] is None


def test_promote_simulation_status_removal_includes_page(dm, tmp_path):
    # **promote 시뮬**: status:draft 제거 → load_pages 가 비로소 포함 = draft→정식이 마커 1개.
    path = dm.write_draft_page(
        "승격대상", slug="promote-me", covers=["src/y/**"], source=None, domain_dir=tmp_path,
    )
    # 전: draft → index 제외.
    assert dm.load_pages(domain_dir=tmp_path) == []
    # promote 시뮬 = frontmatter status:draft 줄 제거(파일명 rename 은 필터 무관·생략).
    text = path.read_text(encoding="utf-8")
    promoted = text.replace("status: draft\n", "")
    path.write_text(promoted, encoding="utf-8")
    # 후: 비로소 포함(마커 1개 제거가 draft→정식).
    titles = [p["title"] for p in dm.load_pages(domain_dir=tmp_path)]
    assert titles == ["승격대상"]


def test_capture_draft_cli_exit_code_success(dm, tmp_path, monkeypatch, capsys):
    # CLI 성공 → exit 0 + 생성 경로/promote 안내 출력.
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["capture-draft", "--title", "조사X", "--slug", "research-x"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "draft 생성" in out
    assert "research-x.draft.md" in out
    assert "promote" in out
    assert (tmp_path / "research-x.draft.md").exists()


def test_capture_draft_requires_title(dm):
    # --title 부재 → argparse SystemExit(required).
    with pytest.raises(SystemExit):
        dm.main(["capture-draft"])


def test_capture_draft_missing_source_file_errors(dm, tmp_path, monkeypatch, capsys):
    # --source 파일 부재 → 명시 에러(rc 1·stderr)·잘못된 빈 페이지 생성 안 함.
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["capture-draft", "--title", "X", "--slug", "x",
                  "--source", str(tmp_path / "does-not-exist.txt")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "capture-draft 실패" in err
    assert not (tmp_path / "x.draft.md").exists()  # 페이지 생성 안 됨.


def test_capture_draft_invalid_type_rejected(dm):
    # --type 은 concept|guide|research 만(choices) — 그 외 SystemExit.
    with pytest.raises(SystemExit):
        dm.main(["capture-draft", "--title", "X", "--type", "bogus"])


def test_capture_draft_covers_csv_parsed(dm, tmp_path, monkeypatch):
    # --covers CSV → 콤마분리·공백 trim·빈 토큰 제거된 covers 리스트.
    monkeypatch.setattr(dm, "DOMAIN_DIR", tmp_path)
    rc = dm.main(["capture-draft", "--title", "C", "--slug", "c",
                  "--covers", "src/a/** , src/b/** ,"])
    assert rc == 0
    text = _read_draft(tmp_path, "c")
    assert "covers: [src/a/**, src/b/**]" in text
    assert "TODO PM: covers" not in text  # covers 있으므로 placeholder 없음.
