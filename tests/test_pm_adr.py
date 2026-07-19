"""pm_adr.py 단위테스트 (T-0368) — ADR 발행/개정 명령어化 backbone.

채번·frontmatter scaffold·lifecycle back-ref surgical 부기·README 색인 이동·log decide entry
+ end-to-end 발행 계획/적용을 hermetic(임시 decisions/) 로 검증한다. 마지막에 board.py 의
`lint_adr_lifecycle`(ADR-0021 advisory)를 발행 산출물에 돌려 **back-ref clean**(DoD 정합)을 실측한다.

도구는 패키지가 아니므로 importlib 동적 로드 (test_pm_handoff 관용구).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, filename: str | None = None):
    path = TOOLS / (filename or f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def adr():
    return _load("pm_adr")


# ── id/채번 helpers ───────────────────────────────────────────────────────────


def test_adr_id_zero_pads(adr):
    assert adr.adr_id(65) == "ADR-0065"
    assert adr.adr_id(1) == "ADR-0001"


def test_parse_adr_num_accepts_variants(adr):
    assert adr.parse_adr_num("ADR-0061") == 61
    assert adr.parse_adr_num("adr-61") == 61
    assert adr.parse_adr_num("0043") == 43
    assert adr.parse_adr_num("7") == 7
    with pytest.raises(ValueError):
        adr.parse_adr_num("not-an-adr")


def test_next_adr_number(adr, tmp_path):
    # 빈/부재 디렉토리 → 1.
    assert adr.next_adr_number(tmp_path / "nope") == 1
    d = tmp_path / "decisions"
    d.mkdir()
    assert adr.next_adr_number(d) == 1
    (d / "0001-a.md").write_text("x", encoding="utf-8")
    (d / "0007-b.md").write_text("x", encoding="utf-8")
    (d / "README.md").write_text("x", encoding="utf-8")  # 비-숫자 무시
    (d / "_template.md").write_text("x", encoding="utf-8")
    assert adr.next_adr_number(d) == 8


# ── slug 검증 (파일 주입/traversal 방지·T-0355 클래스) ─────────────────────────


@pytest.mark.parametrize("slug", ["ok", "a-b-c", "with_underscore", "adr0049", "x1"])
def test_validate_slug_accepts_valid(adr, slug):
    adr.validate_slug(slug)  # 예외 없음


@pytest.mark.parametrize("slug", [
    "../escape", "a/b", "a\\b", "..", ".hidden", " leading-space",
    "trailing ", "with space", "Upper", "has.dot", "", "  ", "-leading-hyphen",
    "special!", "colon:here",
])
def test_validate_slug_rejects_dangerous(adr, slug):
    with pytest.raises(ValueError):
        adr.validate_slug(slug)


def test_cli_rejects_bad_slug_before_side_effects(adr, capsys, monkeypatch, wiki):
    """CLI new --slug ../evil → rc 2·에러·decisions 무변경(부작용 이전 거부)."""
    decisions, log = wiki
    monkeypatch.setattr(adr, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(adr, "LOG_FILE", log)
    before = sorted(p.name for p in decisions.iterdir())
    rc = adr.main(["new", "--title", "T", "--slug", "../evil"])
    assert rc == 2
    assert "slug" in capsys.readouterr().err
    assert sorted(p.name for p in decisions.iterdir()) == before


def test_cli_rejects_bad_amends_id_rc2(adr, capsys, monkeypatch, wiki):
    """CLI new --amends <오형식> → rc 2·한 줄 오류(traceback 아님)·decisions 무변경 (T-0376).

    옛엔 parse_adr_num ValueError 가 uncaught traceback 으로 노출 — slug 게이트와 동형의
    입구 거부로 전환. 정상 ID 경로는 기존 e2e 가 커버(과차단 아님)."""
    decisions, log = wiki
    monkeypatch.setattr(adr, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(adr, "LOG_FILE", log)
    before = sorted(p.name for p in decisions.iterdir())
    rc = adr.main(["new", "--title", "T", "--slug", "t", "--amends", "ADR-oops"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "개정대상 ID" in err and "Traceback" not in err
    assert sorted(p.name for p in decisions.iterdir()) == before


def test_cli_author_defaults_to_identity_resolution(adr, capsys, monkeypatch, wiki):
    """--author 생략 → board identity_tag 해소값 채움·명시 인자 우선·해소 불가는 현행 빈 값 (T-0376)."""
    decisions, log = wiki
    monkeypatch.setattr(adr, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(adr, "LOG_FILE", log)
    monkeypatch.setattr(adr, "_resolve_default_author", lambda: "alice/pm_1")
    rc = adr.main(["new", "--title", "T1", "--slug", "auto-author"])
    assert rc == 0
    capsys.readouterr()
    made = next(p for p in decisions.glob("*auto-author*"))
    assert "author: alice/pm_1" in made.read_text(encoding="utf-8")
    # 명시 인자 우선.
    rc = adr.main(["new", "--title", "T2", "--slug", "explicit-author", "--author", "bob/pm_2"])
    assert rc == 0
    capsys.readouterr()
    made2 = next(p for p in decisions.glob("*explicit-author*"))
    assert "author: bob/pm_2" in made2.read_text(encoding="utf-8")
    # 해소 불가(None) → 현행 빈 값 경로 유지(발행은 성공).
    monkeypatch.setattr(adr, "_resolve_default_author", lambda: None)
    rc = adr.main(["new", "--title", "T3", "--slug", "no-author"])
    assert rc == 0
    capsys.readouterr()


# ── frontmatter / 본문 scaffold ────────────────────────────────────────────────


def test_build_frontmatter_lifecycle_and_related_merge(adr):
    fm = adr.build_frontmatter(
        title="테스트 결정", scope="internal-process", author="u/pm_1",
        date="2026-07-19", status="accepted",
        amends=[61, 62], supersedes=[], refines=[33],
        related=["ADR-0019", "ADR-0061"], tags=["board", "adr"],
    )
    assert "title: 테스트 결정" in fm
    assert "type: decision" in fm
    assert "status: accepted" in fm
    assert "scope: internal-process" in fm
    assert "author: u/pm_1" in fm
    assert "amends: [ADR-0061, ADR-0062]" in fm
    assert "refines: [ADR-0033]" in fm
    assert "supersedes:" not in fm  # 빈 동사는 줄 없음
    # related = 개정 대상 자동편입(amends+refines) + 사용자 related, dedup(ADR-0061 1회).
    related_line = [l for l in fm.splitlines() if l.startswith("related:")][0]
    assert related_line.count("ADR-0061") == 1
    assert "ADR-0062" in related_line and "ADR-0033" in related_line and "ADR-0019" in related_line
    assert "tags: [board, adr]" in fm


@pytest.mark.parametrize("title", [
    "A: B",                       # 콜론 — flow mapping 오해
    'has "quote" inside',         # 큰따옴표
    "has 'apostrophe'",           # 작은따옴표
    "# starts with hash",         # 해시(주석 오해)
    "한글 제목: 부제",             # 한글 + 콜론
    "brackets [x] and {y}",       # flow 구분자
    "value # with: many: colons",
])
def test_build_frontmatter_adversarial_title_roundtrips(adr, title):
    """적대 제목(콜론·따옴표·해시·한글·flow 구분자)이 YAML 파싱을 안 깨고 정확히 round-trip.

    문자열 보간(`title: {title}`)이던 코드가 정상 제목의 `:`/`#`/따옴표에 frontmatter YAML 을 깨서
    lint 가 그 ADR 을 조용히 skip 하던 클래스(codex must-fix)를 닫음을 실측한다."""
    import yaml
    fm = adr.build_frontmatter(
        title=title, scope="internal-process", author="u/pm_1", date="2026-07-19",
        status="accepted", amends=[], supersedes=[], refines=[],
        related=["ADR-0001"], tags=["a", "b"],
    )
    # frontmatter 블록(--- 사이) 파싱 → title 정확 round-trip.
    inner = fm.split("---\n", 2)[1]
    loaded = yaml.safe_load(inner)
    assert loaded["title"] == title
    assert loaded["tags"] == ["a", "b"]
    assert loaded["related"] == ["ADR-0001"]


def test_build_adr_file_adversarial_title_parses_via_board(adr):
    """적대 제목의 전체 ADR 파일이 board.load_ticket(lint 이 쓰는 파서)로 파싱된다(skip 안 됨)."""
    board = _load("board")
    text = adr.build_adr_file(
        number=70, title='나쁜: 제목 "따옴표" # 해시', scope="internal-process",
        author="u/pm_1", date="2026-07-19", status="accepted",
        amends=[61], supersedes=[], refines=[], related=[], tags=["x"],
    )
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "0070-x.md"
        p.write_text(text, encoding="utf-8")
        fm, _body = board.load_ticket(p)
    assert fm["title"] == '나쁜: 제목 "따옴표" # 해시'
    assert fm["status"] == "accepted"
    assert fm["amends"] == ["ADR-0061"]


def test_build_adr_file_has_body_scaffold(adr):
    text = adr.build_adr_file(
        number=70, title="본문 골격", scope="internal-process", author="u/pm_1",
        date="2026-07-19", status="accepted",
        amends=[], supersedes=[], refines=[], related=[], tags=["x"],
    )
    assert text.startswith("---\n")
    assert "# ADR-0070 — 본문 골격" in text
    for section in ("## Context", "## Decision", "## Consequences", "## References"):
        assert section in text
    assert "PM 서술" in text  # placeholder 존재


# ── lifecycle back-ref surgical 부기 ──────────────────────────────────────────


_TARGET_ADR = """\
---
title: 대상 결정
created: 2026-07-01
updated: 2026-07-01
author: u/pm_1
type: decision
status: accepted
scope: internal-process
related: [ADR-0001]
tags: [x, y]
---

# ADR-0061 — 대상 결정

> 본문.

## Context
- 유지.
"""


def test_apply_lifecycle_backref_amends_inserts_field(adr):
    out = adr.apply_lifecycle_backref(_TARGET_ADR, "ADR-0070", "amends")
    assert "status: amended" in out
    assert "status: accepted" not in out
    assert "amended_by: [ADR-0070]" in out
    # 본문·다른 필드 불변.
    assert "# ADR-0061 — 대상 결정" in out
    assert "## Context\n- 유지." in out
    assert "tags: [x, y]" in out


def test_apply_lifecycle_backref_supersedes(adr):
    out = adr.apply_lifecycle_backref(_TARGET_ADR, "ADR-0070", "supersedes")
    assert "status: superseded" in out
    assert "superseded_by: [ADR-0070]" in out


def test_apply_lifecycle_backref_appends_to_existing_and_idempotent(adr):
    already = _TARGET_ADR.replace(
        "status: accepted\n", "status: amended\namended_by: [ADR-0065]\n"
    )
    out = adr.apply_lifecycle_backref(already, "ADR-0070", "amends")
    line = [l for l in out.splitlines() if l.startswith("amended_by:")][0]
    assert "ADR-0065" in line and "ADR-0070" in line
    # 멱등 — 다시 적용해도 중복 안 붙음.
    out2 = adr.apply_lifecycle_backref(out, "ADR-0070", "amends")
    line2 = [l for l in out2.splitlines() if l.startswith("amended_by:")][0]
    assert line2.count("ADR-0070") == 1


def test_apply_lifecycle_backref_malformed_frontmatter_returns_original(adr):
    bad = "no frontmatter here\n"
    assert adr.apply_lifecycle_backref(bad, "ADR-0070", "amends") == bad


# ── README 색인 ───────────────────────────────────────────────────────────────


_README = """\
# Decisions (ADR)

> 서문.

## Accepted (live)

| # | Title | Date | Tags |
|---|---|---|---|
| [0060](0060-a.md) | A 결정 | 2026-07-17 | tag-a |
| [0061](0061-target.md) | 대상 결정 | 2026-07-01 | x, y |

## Superseded (비권위)

| # | Title | superseded_by | 무엇이 대체됐나 |
|---|---|---|---|
| [0043](0043-old.md) | 옛 결정 | [[ADR-0057]] | 대체됨 |

## Amended (유효)

| # | Title | amended_by | 무엇이 바뀌었나 |
|---|---|---|---|
| [0002](0002-b.md) | B 결정 | [[ADR-0016]] | 바뀜 |

## 새 ADR 추가 절차

1. ...
"""


def test_insert_accepted_row_appends(adr):
    out, w = adr.insert_accepted_row(
        _README, number=70, slug="new-one", title="새 결정", date="2026-07-19", tags=["p", "q"],
    )
    assert w is None
    assert "| [0070](0070-new-one.md) | 새 결정 | 2026-07-19 | p, q |" in out
    # Accepted 표 안(다음 ## 앞)에 삽입됐는지 — 0061 뒤·Superseded 앞.
    idx_row = out.index("[0070]")
    idx_superseded = out.index("## Superseded")
    assert idx_row < idx_superseded


def test_insert_accepted_row_no_section_failsoft(adr):
    out, w = adr.insert_accepted_row(
        "# no tables\n", number=70, slug="x", title="t", date="2026-07-19", tags=[],
    )
    assert w is not None
    assert out == "# no tables\n"


def test_move_row_accepted_to_amended(adr):
    out, w = adr.move_or_append_backref_row(_README, target_num=61, new_id="ADR-0070", verb="amends")
    assert w is None
    # Accepted 에서 0061 제거.
    accepted_block = out.split("## Superseded")[0]
    assert "[0061]" not in accepted_block
    # Amended 표에 이동 + back-ref + placeholder.
    amended_block = out.split("## Amended")[1].split("## 새 ADR")[0]
    assert "[0061](0061-target.md)" in amended_block
    assert "대상 결정" in amended_block
    assert "[[ADR-0070]]" in amended_block
    assert "<개정 요약 — PM 서술>" in amended_block


def test_supersede_moves_to_superseded_table(adr):
    out, w = adr.move_or_append_backref_row(_README, target_num=61, new_id="ADR-0070", verb="supersedes")
    assert w is None
    superseded_block = out.split("## Superseded")[1].split("## Amended")[0]
    assert "[0061](0061-target.md)" in superseded_block
    assert "[[ADR-0070]]" in superseded_block
    assert "<대체 요약 — PM 서술>" in superseded_block


def test_move_row_already_in_amended_appends_backref(adr):
    # 대상이 이미 Amended(0002)면 back-ref cell 에 append.
    out, w = adr.move_or_append_backref_row(_README, target_num=2, new_id="ADR-0070", verb="amends")
    assert w is None
    line = [l for l in out.splitlines() if l.startswith("| [0002]")][0]
    assert "[[ADR-0016]]" in line and "[[ADR-0070]]" in line


def test_move_row_target_absent_failsoft(adr):
    out, w = adr.move_or_append_backref_row(_README, target_num=999, new_id="ADR-0070", verb="amends")
    assert w is not None
    assert out == _README


# ── log decide entry ──────────────────────────────────────────────────────────


def test_build_decide_log_entry(adr):
    entry = adr.build_decide_log_entry(70, "새 결정 제목", "2026-07-19")
    assert entry.startswith("## [2026-07-19] decide | ADR-0070 — 새 결정 제목")
    assert "PM:" in entry  # placeholder 본문


# ── end-to-end 발행 (AdrIssuer plan/apply·hermetic) ──────────────────────────


@pytest.fixture
def wiki(tmp_path):
    """임시 decisions/ + README + log/current.md 셋업."""
    decisions = tmp_path / "wiki" / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "0060-a.md").write_text(
        "---\ntitle: A\nstatus: accepted\ntype: decision\n---\n# ADR-0060\n", encoding="utf-8")
    (decisions / "0061-target.md").write_text(_TARGET_ADR, encoding="utf-8")
    (decisions / "README.md").write_text(_README, encoding="utf-8")
    log = tmp_path / "wiki" / "log" / "current.md"
    log.parent.mkdir(parents=True)
    log.write_text("# Log\n\n## [2026-07-01] create | x\n\n- seed.\n", encoding="utf-8")
    return decisions, log


def test_issue_new_adr_amending_target_end_to_end(adr, wiki):
    decisions, log = wiki
    issuer = adr.AdrIssuer(decisions_dir=decisions, log_file=log, date="2026-07-19")
    plan = issuer.plan(
        title="원자화 결정", slug="atomize", scope="internal-process", author="u/pm_1",
        status="accepted", amends=[61], supersedes=[], refines=[],
        related=[], tags=["v1.3.0", "adr"],
    )
    assert plan["number"] == 62
    assert plan["warnings"] == []
    issuer.apply(plan)

    # 1. 신규 ADR 파일.
    new_file = decisions / "0062-atomize.md"
    assert new_file.exists()
    new_text = new_file.read_text(encoding="utf-8")
    assert "amends: [ADR-0061]" in new_text
    assert "# ADR-0062 — 원자화 결정" in new_text

    # 2. 대상 back-ref (load-bearing).
    target_text = (decisions / "0061-target.md").read_text(encoding="utf-8")
    assert "status: amended" in target_text
    assert "amended_by: [ADR-0062]" in target_text

    # 3. README — Accepted 에 신규·Amended 로 대상 이동.
    readme = (decisions / "README.md").read_text(encoding="utf-8")
    assert "[0062](0062-atomize.md)" in readme.split("## Superseded")[0]  # Accepted
    amended_block = readme.split("## Amended")[1]
    assert "[0061](0061-target.md)" in amended_block and "[[ADR-0062]]" in amended_block

    # 4. log decide entry append.
    log_text = log.read_text(encoding="utf-8")
    assert "## [2026-07-19] decide | ADR-0062 — 원자화 결정" in log_text
    assert log_text.startswith("# Log")  # 기존 내용 보존


def test_issue_is_lint_clean_against_board_adr_lifecycle(adr, wiki, monkeypatch):
    """발행 산출물이 board.lint_adr_lifecycle(ADR-0021) advisory clean (DoD 정합·실측)."""
    decisions, log = wiki
    board = _load("board")
    monkeypatch.setattr(board, "DECISIONS_DIR", decisions)

    # 발행 전: 대상은 accepted·back-ref 없음 → 사전 findings 0(기존은 정합).
    assert board.lint_adr_lifecycle() == []

    issuer = adr.AdrIssuer(decisions_dir=decisions, log_file=log, date="2026-07-19")
    plan = issuer.plan(
        title="개정", slug="amend-it", scope="internal-process", author="u/pm_1",
        status="accepted", amends=[61], supersedes=[], refines=[],
        related=[], tags=["adr"],
    )
    issuer.apply(plan)

    # 발행 후: amends 걸었지만 back-ref/status 를 발행 시점에 채웠으므로 여전히 clean.
    findings = board.lint_adr_lifecycle()
    assert findings == [], f"발행 산출이 lint dirty — {findings}"


def test_dry_run_writes_nothing(adr, wiki):
    decisions, log = wiki
    before_files = sorted(p.name for p in decisions.iterdir())
    before_target = (decisions / "0061-target.md").read_text(encoding="utf-8")
    before_log = log.read_text(encoding="utf-8")

    issuer = adr.AdrIssuer(decisions_dir=decisions, log_file=log, date="2026-07-19")
    plan = issuer.plan(
        title="미적용", slug="noop", scope="internal-process", author="u/pm_1",
        status="accepted", amends=[61], supersedes=[], refines=[], related=[], tags=[],
    )
    # plan 만 하고 apply 안 함 → 파일 무변경.
    assert sorted(p.name for p in decisions.iterdir()) == before_files
    assert (decisions / "0061-target.md").read_text(encoding="utf-8") == before_target
    assert log.read_text(encoding="utf-8") == before_log
    assert plan["number"] == 62


def test_apply_partial_failure_reports_done_and_remaining(adr, wiki, monkeypatch):
    """apply 중 README 단계 실패 시 RuntimeError 가 수행/미수행 단계를 명시하고, 신규 ADR 파일은
    가장 먼저 기록돼 보존된다 (codex suggestion② — 부분 반영 복구 안내)."""
    decisions, log = wiki
    issuer = adr.AdrIssuer(decisions_dir=decisions, log_file=log, date="2026-07-19")
    plan = issuer.plan(
        title="부분실패", slug="partial", scope="internal-process", author="u/pm_1",
        status="accepted", amends=[61], supersedes=[], refines=[], related=[], tags=[],
    )

    def _boom(_plan):
        raise OSError("disk full (test injection)")
    monkeypatch.setattr(issuer, "_write_readme", _boom)

    with pytest.raises(RuntimeError) as ei:
        issuer.apply(plan)
    msg = str(ei.value)
    assert "README 색인" in msg          # 실패 단계 = 미수행
    assert "신규 ADR 파일" in msg         # 수행 완료로 보고
    assert "대상 back-ref" in msg         # README 앞 단계도 수행 완료
    # 신규 ADR 파일은 먼저 기록돼 디스크에 보존(결정 본문 유실 없음).
    assert (decisions / "0062-partial.md").exists()
    # log 단계(README 뒤)는 미수행 — log 에 새 decide entry 없음.
    assert "decide | ADR-0062" not in log.read_text(encoding="utf-8")


def test_cli_main_dry_run(adr, capsys, monkeypatch, wiki):
    """CLI new --dry-run — 미리보기 출력·쓰기 없음(모듈 상수 DECISIONS_DIR/LOG_FILE monkeypatch)."""
    decisions, log = wiki
    monkeypatch.setattr(adr, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(adr, "LOG_FILE", log)
    before = sorted(p.name for p in decisions.iterdir())
    rc = adr.main(["new", "--title", "CLI 결정", "--slug", "cli-one",
                   "--tags", "adr", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "ADR-0062" in out
    assert sorted(p.name for p in decisions.iterdir()) == before  # 무변경
