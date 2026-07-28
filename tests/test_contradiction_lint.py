"""contradiction_lint.py 단위/통합 테스트 (T-0369·ADR-0064) — 모순 lint.

"결정을 바꾼 순간의 잔여" 탐지 — 개정된 결정을 [[wikilink]] 로 참조하는 문서를 기계로 스코프하고,
LLM 탐지(DI·기본 dry=미호출)로 후보를 제시한다. 판정=사람(advisory·차단 아님). pm_adr.py 발행/개정
명령의 트리거 배선도 함께 검증한다(개정에만 발화·refines/plain 은 no-op·fail-soft).

LLM 호출은 전부 DI/mock — 라이브 호출 없음(hermetic). 도구는 패키지가 아니므로 importlib 동적 로드.
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
def cl():
    return _load("contradiction_lint")


@pytest.fixture(scope="module")
def adr():
    return _load("pm_adr")


# ── 스코프 수집 (기계·back-ref 범위) ──────────────────────────────────────────────


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_collect_scope_only_referencing_files(cl, tmp_path):
    refs = _write(tmp_path, "wiki/a.md", "이 문서는 [[ADR-0061]] 을 전제로 한다.\n다른 줄.")
    _write(tmp_path, "wiki/b.md", "무관한 문서 — [[ADR-0099]] 참조.")
    _write(tmp_path, "wiki/c.md", "링크 없음.")
    hits = cl.collect_reference_scope(["ADR-0061"], [refs.parent / "a.md",
                                                     refs.parent / "b.md",
                                                     refs.parent / "c.md"], repo=tmp_path)
    assert len(hits) == 1
    assert hits[0].path == "wiki/a.md"
    assert hits[0].matched_ids == ("ADR-0061",)
    assert any("[[ADR-0061]]" in s for s in hits[0].ref_lines)
    # excerpt = 파일 전체(작은 파일) — 참조 줄뿐 아니라 다른 줄도 담긴다.
    assert "[[ADR-0061]]" in hits[0].excerpt and "다른 줄." in hits[0].excerpt


def test_collect_scope_excerpt_spans_separated_sections(cl, tmp_path):
    """링크는 '참고' 절·모순 서술은 '결정' 절(분리) — excerpt 에 둘 다 담겨야 run_fn 이 근거로 탐지 가능
    (codex must-fix: 참조 줄만 담으면 다른 문단의 잔여 모순은 근거 부재로 탐지 불능)."""
    doc = (
        "## 결정\n"
        "우리는 항상 동기 방식으로 처리한다(옛 결정 전제).\n"
        "이 문장이 새 결정과 모순되는 잔여 서술이다.\n"
        "\n"
        "## 배경\n"
        "여러 문단.\n"
        "\n"
        "## 참고\n"
        "- 근거는 [[ADR-0061]] 을 따른다.\n"
    )
    f = _write(tmp_path, "wiki/split.md", doc)
    hits = cl.collect_reference_scope(["ADR-0061"], [f], repo=tmp_path)
    assert len(hits) == 1
    ex = hits[0].excerpt
    # 링크 줄(참고 절)과 모순 서술 줄(결정 절)이 *둘 다* excerpt 에 있어야 한다(분리된 절).
    assert "[[ADR-0061]]" in ex
    assert "모순되는 잔여 서술" in ex
    # 프롬프트에도 두 절이 함께 실려 run_fn 이 대조 근거를 갖는다.
    prompt = cl.build_prompt("ADR-0062", "새 방향", "이제 비동기 처리한다.", hits)
    assert "모순되는 잔여 서술" in prompt and "[[ADR-0061]]" in prompt


def test_collect_scope_large_file_windows_around_reference(cl, tmp_path):
    """상한 초과 파일 → 참조 주변 윈도 excerpt(전체 아님)·생략 마커·참조 인접 문단 포함."""
    filler_top = "\n".join(f"top-{i}" for i in range(300))
    near = "참조 인접 문단의 모순 서술."
    body = filler_top + "\n" + near + "\n근거 [[ADR-0061]] 준수.\n" + \
        "\n".join(f"bot-{i}" for i in range(300))
    f = _write(tmp_path, "wiki/big.md", body)
    hits = cl.collect_reference_scope(["ADR-0061"], [f], repo=tmp_path)
    assert len(hits) == 1
    ex = hits[0].excerpt
    assert "[[ADR-0061]]" in ex
    assert near in ex                       # 참조 ±윈도 안의 인접 문단
    assert cl._EXCERPT_GAP in ex            # 생략 마커(전체 아님)
    assert "top-0" not in ex                # 멀리 떨어진 줄은 제외(윈도 바깥)
    assert len(ex.splitlines()) <= cl._MAX_EXCERPT_LINES + 2  # 상한(+gap 마커)


def test_collect_scope_id_normalization(cl, tmp_path):
    """`[[ADR-61]]`(선행 0 없음)도 target `ADR-0061` 과 매칭(정규화)."""
    f = _write(tmp_path, "wiki/x.md", "옛 결정 [[ADR-61]] 에 따라...")
    hits = cl.collect_reference_scope(["ADR-0061"], [f], repo=tmp_path)
    assert len(hits) == 1
    assert hits[0].matched_ids == ("ADR-0061",)


def test_collect_scope_multiple_targets_and_refline_dedup(cl, tmp_path):
    f = _write(tmp_path, "wiki/multi.md",
               "[[ADR-0061]] 와 [[ADR-0062]] 둘 다.\n[[ADR-0061]] 재등장(같은 줄 아님).")
    hits = cl.collect_reference_scope(["ADR-0061", "ADR-0062"], [f], repo=tmp_path)
    assert len(hits) == 1
    assert set(hits[0].matched_ids) == {"ADR-0061", "ADR-0062"}
    # ref_lines 는 라인 단위·중복 제거(두 줄 다 잡히되 dedup).
    assert len(hits[0].ref_lines) == 2


def test_collect_scope_empty_targets_returns_empty(cl, tmp_path):
    f = _write(tmp_path, "wiki/a.md", "[[ADR-0061]]")
    assert cl.collect_reference_scope([], [f], repo=tmp_path) == []


def test_collect_scope_sorted_by_path(cl, tmp_path):
    fz = _write(tmp_path, "wiki/z.md", "[[ADR-0061]]")
    fa = _write(tmp_path, "wiki/a.md", "[[ADR-0061]]")
    hits = cl.collect_reference_scope(["ADR-0061"], [fz, fa], repo=tmp_path)
    assert [h.path for h in hits] == ["wiki/a.md", "wiki/z.md"]


# ── 프롬프트 조립 / 후보 파싱 ──────────────────────────────────────────────────────


def test_build_prompt_contains_decision_scope_and_contract(cl):
    scope = [cl.ScopeHit(path="wiki/a.md", matched_ids=("ADR-0061",),
                         excerpt="옛 결정 [[ADR-0061]] 을 전제로 X 한다.\n다른 문단의 서술.",
                         ref_lines=("옛 결정 [[ADR-0061]] 을 전제로 X 한다.",))]
    prompt = cl.build_prompt("ADR-0062", "새 방향", "## Decision\n이제 Y 한다.", scope)
    assert "ADR-0062" in prompt and "새 방향" in prompt
    assert "이제 Y 한다." in prompt              # 새 결정 본문
    assert "wiki/a.md" in prompt                 # 스코프 파일
    assert "다른 문단의 서술." in prompt          # excerpt 본문(참조 줄뿐 아니라 다른 문단)
    assert cl.NO_CONTRADICTIONS in prompt        # 출력 계약
    assert "판정은 사람" in prompt


def test_parse_candidates_extracts_lines(cl):
    out = ("서두.\n"
           "- [wiki/a.md] 옛 전제 X — 새 결정 Y 와 모순\n"
           "- [wiki/b.md] 또 다른 잔여 — 모순\n"
           "잡담.")
    cands = cl.parse_candidates(out)
    assert len(cands) == 2
    assert cands[0].startswith("- [wiki/a.md]")


def test_parse_candidates_no_contradictions(cl):
    assert cl.parse_candidates("NO_CONTRADICTIONS") == []
    assert cl.parse_candidates("전부 정합.\nNO_CONTRADICTIONS\n") == []


# ── ContradictionLinter (DI·dry 기본·LLM opt-in) ──────────────────────────────────


def _linter_with_files(cl, files, run_fn=None):
    return cl.ContradictionLinter(files_fn=lambda: files, run_fn=run_fn)


def test_lint_dry_builds_prompt_no_llm_call(cl, tmp_path):
    """기본(run_fn=None) = dry — 프롬프트만 산출·LLM 미호출·후보 비어있음."""
    f = _write(tmp_path, "wiki/a.md", "[[ADR-0061]] 전제.")
    linter = cl.ContradictionLinter(files_fn=lambda: [f], repo=tmp_path)
    res = linter.lint(new_adr_id="ADR-0062", new_adr_title="T",
                      new_adr_text="body", target_ids=["ADR-0061"])
    assert res.called is False
    assert res.candidates == []
    assert res.prompt is not None and "ADR-0062" in res.prompt
    assert len(res.scope) == 1


def test_lint_with_run_fn_calls_and_parses(cl, tmp_path):
    """run_fn 주입 = LLM 탐지 — 호출됨·후보 파싱."""
    f = _write(tmp_path, "wiki/a.md", "[[ADR-0061]] 전제.")
    captured = {}

    def fake_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return "- [wiki/a.md] 옛 전제 — 모순\nNO_CONTRADICTIONS 아님"

    linter = cl.ContradictionLinter(files_fn=lambda: [f], run_fn=fake_llm, repo=tmp_path)
    res = linter.lint(new_adr_id="ADR-0062", new_adr_title="T",
                      new_adr_text="body", target_ids=["ADR-0061"])
    assert res.called is True
    assert res.candidates == ["- [wiki/a.md] 옛 전제 — 모순"]
    assert "wiki/a.md" in captured["prompt"]


def test_lint_no_scope_returns_empty_no_call(cl, tmp_path):
    """참조 문서 0 → 스코프 없음·프롬프트 None·LLM 미호출(run_fn 있어도)."""
    f = _write(tmp_path, "wiki/a.md", "무관 [[ADR-0099]].")
    called = {"n": 0}

    def fake_llm(prompt: str) -> str:
        called["n"] += 1
        return "x"

    linter = cl.ContradictionLinter(files_fn=lambda: [f], run_fn=fake_llm, repo=tmp_path)
    res = linter.lint(new_adr_id="ADR-0062", new_adr_title="T",
                      new_adr_text="body", target_ids=["ADR-0061"])
    assert res.scope == [] and res.prompt is None and res.called is False
    assert called["n"] == 0  # 스코프 없으면 LLM 호출 안 함


# ── advisory 렌더 (판정=사람·차단 아님) ─────────────────────────────────────────────


def test_format_advisory_dry_lists_scope_and_human_note(cl):
    scope = [cl.ScopeHit(path="wiki/a.md", matched_ids=("ADR-0061",), excerpt="e", ref_lines=("s",))]
    res = cl.LintResult(scope=scope, prompt="p", candidates=[], called=False)
    text = cl.format_advisory("ADR-0062", ["ADR-0061"], res)
    assert "wiki/a.md" in text
    assert "판정=사람" in text
    assert "차단 아님" in text


def test_format_advisory_dry_emits_full_standalone_command(cl):
    """dry note 는 pm_adr 경로에서도 그대로 실행 가능한 standalone 커맨드 전체를 안내한다(codex suggestion)."""
    scope = [cl.ScopeHit(path="wiki/a.md", matched_ids=("ADR-0061",), excerpt="e", ref_lines=("s",))]
    res = cl.LintResult(scope=scope, prompt="p", candidates=[], called=False)
    text = cl.format_advisory("ADR-0062", ["ADR-0061"], res)
    assert "python3 .project_manager/tools/contradiction_lint.py" in text
    assert "--new-adr ADR-0062" in text
    assert "--amends ADR-0061" in text
    assert "--show-prompt" in text


def test_format_advisory_with_candidates(cl):
    scope = [cl.ScopeHit(path="wiki/a.md", matched_ids=("ADR-0061",), excerpt="e", ref_lines=("s",))]
    res = cl.LintResult(scope=scope, prompt="p",
                        candidates=["- [wiki/a.md] 옛 전제 — 모순"], called=True)
    text = cl.format_advisory("ADR-0062", ["ADR-0061"], res)
    assert "LLM 모순 후보" in text
    assert "- [wiki/a.md] 옛 전제 — 모순" in text


def test_format_advisory_empty_scope(cl):
    res = cl.LintResult(scope=[], prompt=None, candidates=[], called=False)
    text = cl.format_advisory("ADR-0062", ["ADR-0061"], res)
    assert "참조하는 문서가 없음" in text


# ── standalone CLI ────────────────────────────────────────────────────────────────


def test_cli_no_targets_is_noop(cl, capsys):
    rc = cl.main(["--new-adr", "ADR-0062"])
    assert rc == 0
    assert "개정 대상" in capsys.readouterr().out


def test_cli_dry_reports_scope(cl, tmp_path, monkeypatch, capsys):
    decisions = tmp_path / "wiki" / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "0062-new.md").write_text(
        "---\ntitle: 새 결정\n---\n# ADR-0062\n본문.", encoding="utf-8")
    ref = _write(tmp_path, "wiki/other.md", "옛 [[ADR-0061]] 전제.")
    monkeypatch.setattr(cl, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(cl, "_default_scope_files", lambda: [ref])
    rc = cl.main(["--new-adr", "ADR-0062", "--amends", "ADR-0061", "--show-prompt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wiki/other.md" in out
    assert "LLM 탐지 프롬프트" in out
    assert "새 결정" in out  # title 로드됨


# ── pm_adr 트리거 배선 (ADR-0064·개정에만·fail-soft·차단 아님) ────────────────────────


@pytest.fixture
def wiki(tmp_path):
    """test_pm_adr 와 동형 임시 decisions/ + README + log 셋업."""
    decisions = tmp_path / "wiki" / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "0060-a.md").write_text(
        "---\ntitle: A\nstatus: accepted\ntype: decision\n---\n# ADR-0060\n", encoding="utf-8")
    target = (
        "---\ntitle: 대상 결정\ncreated: 2026-07-01\nupdated: 2026-07-01\nauthor: u/pm_1\n"
        "type: decision\nstatus: accepted\nscope: internal-process\nrelated: [ADR-0001]\n"
        "tags: [x, y]\n---\n\n# ADR-0061 — 대상 결정\n\n> 본문.\n\n## Context\n- 유지.\n")
    (decisions / "0061-target.md").write_text(target, encoding="utf-8")
    readme = (
        "# Decisions\n\n## Accepted (live)\n\n| # | Title | Date | Tags |\n|---|---|---|---|\n"
        "| [0060](0060-a.md) | A | 2026-07-17 | t |\n"
        "| [0061](0061-target.md) | 대상 결정 | 2026-07-01 | x |\n\n"
        "## Superseded (비권위)\n\n| # | Title | superseded_by | 무엇 |\n|---|---|---|---|\n\n"
        "## Amended (유효)\n\n| # | Title | amended_by | 무엇 |\n|---|---|---|---|\n")
    (decisions / "README.md").write_text(readme, encoding="utf-8")
    log = tmp_path / "wiki" / "log" / "current.md"
    log.parent.mkdir(parents=True)
    log.write_text("# Log\n", encoding="utf-8")
    return decisions, log


def _patch_scope(adr, monkeypatch, scope_files):
    """pm_adr 가 로드하는 contradiction_lint 의 기본 스코프 파일을 주입한다(실 wiki 미접촉)."""
    cl_mod = adr._load_contradiction_lint()
    monkeypatch.setattr(cl_mod, "_default_scope_files", lambda: scope_files)
    # pm_adr 는 매 호출 sibling import 하므로 로더가 같은 인스턴스를 주게 patch.
    monkeypatch.setattr(adr, "_load_contradiction_lint", lambda: cl_mod)
    return cl_mod


def test_pm_adr_amends_triggers_advisory(adr, wiki, tmp_path, monkeypatch, capsys):
    """개정(amends) 발행 → 모순 lint advisory 가 stderr 로 표면화·rc 0(차단 아님)."""
    decisions, log = wiki
    ref = _write(tmp_path, "wiki/ref.md", "이 방침은 [[ADR-0061]] 을 전제로 한다.")
    monkeypatch.setattr(adr, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(adr, "LOG_FILE", log)
    cl_mod = _patch_scope(adr, monkeypatch, [ref])
    monkeypatch.setattr(cl_mod, "REPO", tmp_path)

    rc = adr.main(["new", "--title", "개정 결정", "--slug", "amend-one",
                   "--amends", "ADR-0061", "--dry-run"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[모순 lint]" in err
    assert "wiki/ref.md" in err
    assert "판정=사람" in err


def test_pm_adr_plain_new_no_trigger(adr, wiki, tmp_path, monkeypatch, capsys):
    """신규 plain 발행(개정 없음) → 모순 lint 발화 안 함(참조 스코프 무·트리거 조건 미충족)."""
    decisions, log = wiki
    ref = _write(tmp_path, "wiki/ref.md", "이 방침은 [[ADR-0061]] 을 전제로 한다.")
    monkeypatch.setattr(adr, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(adr, "LOG_FILE", log)
    _patch_scope(adr, monkeypatch, [ref])

    rc = adr.main(["new", "--title", "신규", "--slug", "plain-new", "--dry-run"])
    assert rc == 0
    assert "모순 lint" not in capsys.readouterr().err


def test_pm_adr_refines_no_trigger(adr, wiki, tmp_path, monkeypatch, capsys):
    """refines(대상 불변·확장)만 → 모순 lint 발화 안 함(잔여 모순을 안 만든다)."""
    decisions, log = wiki
    ref = _write(tmp_path, "wiki/ref.md", "[[ADR-0061]] 전제.")
    monkeypatch.setattr(adr, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(adr, "LOG_FILE", log)
    _patch_scope(adr, monkeypatch, [ref])

    rc = adr.main(["new", "--title", "확장", "--slug", "refine-one",
                   "--refines", "ADR-0061", "--dry-run"])
    assert rc == 0
    assert "모순 lint" not in capsys.readouterr().err


def test_pm_adr_trigger_failsoft(adr, wiki, monkeypatch, capsys):
    """contradiction_lint 로드/실행이 터져도 발행은 rc 0 (advisory fail-soft·차단 아님)."""
    decisions, log = wiki
    monkeypatch.setattr(adr, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(adr, "LOG_FILE", log)
    monkeypatch.setattr(adr, "_load_contradiction_lint", lambda: None)  # 로드 실패 시뮬

    rc = adr.main(["new", "--title", "개정", "--slug", "amend-fs",
                   "--amends", "ADR-0061", "--dry-run"])
    assert rc == 0  # 발행 흐름 무영향
