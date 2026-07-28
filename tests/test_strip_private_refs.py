"""Fixtures for deterministic delimiter-unit reference removal."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/strip_private_refs.py"


def _load():
    spec = importlib.util.spec_from_file_location("strip_private_refs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stripper():
    return _load()


def _ticket(digit: str = "1") -> str:
    return "T-" + digit * 4


def _decision(digit: str = "2") -> str:
    return "ADR-" + digit * 4


@pytest.mark.parametrize(
    ("template", "expected", "removed"),
    [
        ("reason ({t})", "reason", 1),
        ("reason ({a})", "reason", 1),
        ("reason ({t}·{a})", "reason", 2),
        ("A·{t}·B", "A·B", 1),
        ("A·{t})", "A)", 1),
        ("({t}·B", "(B", 1),
        ("({t}·{a}·description)", "(description)", 2),
        ("first · {t} · second", "first · second", 1),
        ("reason ·{t}", "reason", 1),
        ("feature({t})로 works", "feature로 works", 1),
    ],
)
def test_only_whole_delimiter_units_are_removed(
    stripper, template, expected, removed
):
    text = template.format(t=_ticket(), a=_decision())
    assert stripper.strip_prose_text(text) == (expected, removed)


@pytest.mark.parametrize(
    "template",
    [
        "{a} 상속",
        "{a} ①",
        "{t} A6",
        "{t} 실측",
        "PM 47 실측",
        "board root (graceful 탐지· {a} ① 분리)",
        "세션 식별자 ({a} D1· 층위 amend·{t} 3모듈)",
        "합성한다 (spike §3.2· {t} ③)",
        "깔때기 검증(must-fix· {t} 게이트)",
        "board root 추종 (board/ 분리· {t} ①· A6)",
        "reason [[{t}]]",
        "reason ({t}/{a})",
        "reason [{t}, {a}]",
        "{t} reason",
    ],
)
def test_reference_sharing_a_unit_with_any_other_token_is_residual(
    stripper, template
):
    text = template.format(t=_ticket(), a=_decision())
    assert stripper.strip_prose_text(text) == (text, 0)
    assert not stripper._actionable_matches(text)


def test_placeholders_are_unchanged(stripper):
    text = "Use T-" + "N" * 4 + " or ADR-" + "N" * 4 + "."
    assert stripper.strip_prose_text(text) == (text, 0)


def test_inline_code_data_is_unchanged(stripper):
    text = f"Parser examples `{_ticket()}` and `prefix{_ticket()}` stay literal."
    assert stripper.strip_prose_text(text) == (text, 0)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("`{t}`", "`{t}`"),
        ("``{t}``", "``{t}``"),
        ("```{t}```", "```{t}```"),
    ],
)
def test_inline_code_blocks_with_multiple_backticks_are_preserved(
    stripper, template, expected
):
    ref = _ticket()
    text = f"See {template.format(t=ref)} now."
    expected_text = f"See {expected.format(t=ref)} now."
    assert stripper.strip_prose_text(text) == (expected_text, 0)


def test_unclosed_inline_code_block_does_not_protect_reference(stripper):
    ref = _ticket()
    text = f"See ``({ref}) now."
    stripped, removed = stripper.strip_prose_text(text)
    assert stripped == "See ``now."
    assert removed == 1


def test_python_rewrite_excludes_ordinary_strings_and_identifiers(stripper):
    ref = _ticket()
    source = (
        f'"""docs ({ref})"""\n'
        f'value = "{ref}"\n'
        f'identifier = "prefix{ref}"\n'
        f"x = 1  # detail ·{ref}\n"
    )
    rewritten, count = stripper.rewrite_python(source)
    assert '"""docs"""' in rewritten
    assert f'value = "{ref}"' in rewritten
    assert f'"prefix{ref}"' in rewritten
    assert "# detail" in rewritten
    assert count == 2


def test_rewrite_is_idempotent(stripper):
    source = f'"""reason ({_ticket()}·{_decision()})"""\n'
    once, count = stripper.rewrite_python(source)
    twice, second_count = stripper.rewrite_python(once)
    assert count == 2
    assert twice == once
    assert second_count == 0


def test_process_python_preserves_crlf_newlines_for_modified_files(stripper, tmp_path):
    path = tmp_path / "sample.py"
    path.write_text('"""reason (T-1111)"""\r\n# retained\r\n', encoding="utf-8")
    result = stripper.process_python(path, tmp_path, write=True)
    assert result.replacements == 1
    with path.open("r", encoding="utf-8", newline="") as stream:
        written = stream.read()
    assert written == '"""reason"""\r\n# retained\r\n'


def test_multiline_rewrite_changes_only_matching_line(stripper):
    source = (
        '"""Summary.\n'
        "    aligned    example stays exact\n"
        f"    reason ({_ticket()})\n"
        '    """\n'
    )
    rewritten, count = stripper.rewrite_python(source)
    assert "    aligned    example stays exact" in rewritten
    assert "    reason" in rewritten
    assert "\n    \"\"\"\n" in rewritten
    assert rewritten.count("\n") == source.count("\n")
    assert count == 1


def test_existing_comment_alignment_is_preserved(stripper):
    source = f"#     aligned text ({_ticket()}) remains aligned\n"
    rewritten, count = stripper.rewrite_python(source)
    assert rewritten == "#     aligned text remains aligned\n"
    assert count == 1


@pytest.mark.parametrize(
    ("template", "expected", "removed"),
    [
        ("         ({a}).", "         ({a}).", 0),
        ("    # ({a}). 락 안에서만 갱신", "    # ({a}). 락 안에서만 갱신", 0),
        ("    ({a}) — leased ≥2 유지", "    ({a}) — leased ≥2 유지", 0),
        (
            "    {t}·advisory·never-block) +",
            "    advisory·never-block) +",
            1,
        ),
    ],
)
def test_line_prefix_and_orphan_punctuation_are_preserved(
    stripper, template, expected, removed
):
    before = template.format(t=_ticket(), a=_decision())
    after = expected.format(t=_ticket(), a=_decision())
    assert stripper.strip_prose_text(before) == (after, removed)
    assert stripper._leading_whitespace(before) == stripper._leading_whitespace(after)
    if removed:
        assert after == stripper.remove_matched_spans(before)


def test_line_invariant_requires_exact_matched_span_reconstruction(stripper):
    before = f"A·{_ticket()}·B"
    exact = stripper.remove_matched_spans(before)
    assert stripper.line_invariant(before, exact)["matched_span_reconstruction"]
    assert not stripper.line_invariant(
        before, exact.replace("·", "", 1)
    )["matched_span_reconstruction"]


@pytest.mark.parametrize(
    ("before", "after", "expected_kind"),
    [
        ("A·B", "A· B", "separator-space"),
        ("A·B", "A··B", "double-separator"),
        ("(A", "(·A", "open-separator"),
        ("A)", "A·)", "separator-close"),
        ("A)", "A )", "space-close"),
        ("(A", "( A", "open-space"),
        ("A", "A·", "trailing-separator"),
        ("A", ")A", "leading-close"),
        ("A B", "A  B", "double-space"),
        ("A·B", "A·①", "orphan-marker-after-separator"),
        ("A·B", "A·A6", "orphan-marker-after-separator"),
    ],
)
def test_delta_scan_rejects_each_new_pattern(
    stripper, before, after, expected_kind
):
    issues = stripper.self_sufficiency_issues(before, after)
    assert any(expected_kind in issue for issue in issues)


def test_delta_scan_is_per_line_not_aggregate(stripper):
    before = "A· B\nC·D"
    after = "A·B\nC· D"
    issues = stripper.self_sufficiency_issues(before, after)
    assert any("line 2 separator-space" in issue for issue in issues)


def test_delta_scan_ignores_preexisting_notation(stripper):
    text = "call foo(); intentional · spacing; session (PM 47 실측)"
    assert stripper.self_sufficiency_issues(text, text) == []


def test_verification_mode_fails_before_write(stripper, tmp_path, monkeypatch):
    path = tmp_path / "sample.py"
    before = '"""A B"""\n'
    path.write_text(before, encoding="utf-8")
    monkeypatch.setattr(
        stripper, "rewrite_python", lambda source: (source.replace("A B", "A  B"), 1)
    )
    with pytest.raises(stripper.SelfSufficiencyError):
        stripper.process_python(path, tmp_path, write=True, verify_delta=True)
    assert path.read_text(encoding="utf-8") == before


def test_prose_spans_include_comments_and_documentation_only(stripper):
    ref = _decision()
    source = f'"""docs {ref}"""\nvalue = "{ref}"  # note {ref}\n'
    spans = stripper.prose_token_spans(source)
    assert len(spans) == 2
    assert sum(len(stripper.SPECIFIC_REF_RE.findall(span.text)) for span in spans) == 2


def test_all_implicitly_concatenated_doc_strings_are_prose(stripper):
    ref = _ticket()
    source = f'"""head""" """reason ({ref})"""\n'
    spans = stripper.prose_token_spans(source)
    assert [span.text for span in spans] == ['"""head"""', f'"""reason ({ref})"""']
    assert stripper._prose_count(source) == 1
    rewritten, count = stripper.rewrite_python(source)
    assert rewritten == '"""head""" """reason"""\n'
    assert count == 1


def _mini_repo(root: Path) -> None:
    tools = root / ".project_manager/tools"
    tools.mkdir(parents=True)
    (tools / "sample.py").write_text(
        f'"""reason ({_ticket()})"""\nvalue = "{_ticket()}"\n',
        encoding="utf-8",
    )
    (root / ".project_manager/wiki").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / "tests").mkdir()


def test_cli_dry_run_writes_reports_without_source_changes(tmp_path):
    _mini_repo(tmp_path)
    report_dir = tmp_path / "reports"
    path = tmp_path / ".project_manager/tools/sample.py"
    before = path.read_text(encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--report-dir",
            str(report_dir),
            "--dry-run",
            "--verify-delta",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == before
    counters = json.loads(
        (report_dir / "p1-counters.json").read_text(encoding="utf-8")
    )
    assert counters["replacements"] == 1
    assert counters["after_prose"] == 0
    assert counters["allowed_non_prose"] == 1
    assert counters["delta_verification"]["verdict"] == "PASS"
    sample = (report_dir / "p1-sample.md").read_text(encoding="utf-8")
    assert "Mechanical delta verification" in sample
    assert "zero per-line pattern increases" in sample


def test_cli_apply_then_dry_run_reports_zero_drift(tmp_path):
    _mini_repo(tmp_path)
    report_dir = tmp_path / "reports"
    command = [
        sys.executable,
        str(SCRIPT),
        "--root",
        str(tmp_path),
        "--report-dir",
        str(report_dir),
        "--verify-delta",
    ]
    assert subprocess.run(command, check=False).returncode == 0
    command.append("--dry-run")
    assert subprocess.run(command, check=False).returncode == 0
    counters = json.loads(
        (report_dir / "p1-counters.json").read_text(encoding="utf-8")
    )
    assert counters["replacements"] == 0
    assert counters["after_prose"] == 0


def test_residual_report_and_counter_agree(stripper, tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(
        f'"""contract ({_decision()} 계약)"""\n', encoding="utf-8"
    )
    result = stripper.process_python(path, tmp_path, write=False)
    output = tmp_path / "residuals.md"
    count = stripper._write_residual_report(tmp_path, [result], output)
    assert count == result.after_prose == 1
    assert "sample.py:1" in output.read_text(encoding="utf-8")


def test_dry_run_residual_report_uses_transformed_snapshot(stripper, tmp_path):
    removable = _ticket()
    residual = _decision()
    path = tmp_path / "sample.py"
    path.write_text(
        f'"""reason ({removable}); contract ({residual} 계약)"""\n',
        encoding="utf-8",
    )
    result = stripper.process_python(path, tmp_path, write=False)
    assert result.replacements == 1
    assert result.after_prose == 1
    output = tmp_path / "residuals.md"
    count = stripper._write_residual_report(tmp_path, [result], output)
    report = output.read_text(encoding="utf-8")
    assert count == 1
    assert removable not in report
    assert residual in report
    assert removable in path.read_text(encoding="utf-8")
