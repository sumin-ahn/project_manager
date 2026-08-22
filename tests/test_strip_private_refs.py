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


def _marker(stripper, suffix: str = "") -> str:
    return "# " + stripper._DATA_LITERAL_MARKER + suffix


def _guest_marker_source(stripper, ref: str, *, marked: bool) -> str:
    """디스크에 기록되는 guest 마커 선언부 형상을 픽스처로 재현한다."""
    body = [
        f"# 마커 절은 read_manifest 가 무시한다 (local·pm_update-preserved·{ref})",
        f'_GUEST_MANIFEST_BEGIN = "# >>> pm add-harness guest @render ({ref}) >>>"',
    ]
    if marked:
        body = [_marker(stripper, ":begin"), *body, _marker(stripper, ":end")]
    return "\n".join(body) + "\n"


def test_block_marked_wire_declaration_survives_strip(stripper):
    ref = _ticket()
    source = _guest_marker_source(stripper, ref, marked=True)
    assert stripper.rewrite_python(source) == (source, 0)


def test_unmarked_prose_still_loses_the_reference(stripper):
    ref = _ticket()
    source = _guest_marker_source(stripper, ref, marked=False)
    rewritten, count = stripper.rewrite_python(source)
    assert count == 1
    assert "(local·pm_update-preserved)" in rewritten
    assert f'"# >>> pm add-harness guest @render ({ref}) >>>"' in rewritten


def test_same_line_marker_protects_that_line_only(stripper):
    ref = _ticket()
    marked = f'DATA = "wire ({ref})"  {_marker(stripper)} 디스크 wire 문자열 ({ref})\n'
    assert stripper.rewrite_python(marked) == (marked, 0)

    bare = marked.replace(_marker(stripper) + " ", "# ")
    rewritten, count = stripper.rewrite_python(bare)
    assert count == 1
    assert f'DATA = "wire ({ref})"' in rewritten
    assert "# 디스크 wire 문자열\n" in rewritten


def test_standalone_marker_protects_the_following_declaration_line(stripper):
    ref = _ticket()
    source = (
        "\n".join(
            (
                f"{_marker(stripper)} 아래 한 줄은 디스크에 기록되는 wire 문자열이다",
                f'WIRE = "guest ({ref}) begin"  # 데이터 마커 ({ref})',
                f"# 표식 범위 밖 산문 ({ref}) 은 정리된다",
            )
        )
        + "\n"
    )
    rewritten, count = stripper.rewrite_python(source)
    assert f'WIRE = "guest ({ref}) begin"  # 데이터 마커 ({ref})' in rewritten
    assert "# 표식 범위 밖 산문 은 정리된다" in rewritten
    assert count == 1


def test_block_marker_covers_multiline_declarations(stripper):
    current, legacy = _ticket("1"), _ticket("3")
    source = (
        "\n".join(
            (
                _marker(stripper, ":begin"),
                "_WIRE_GENERATIONS = (",
                f'    "guest ({legacy})",   # 옛 세대 ({legacy})',
                f'    "guest ({current})",  # 현 세대 ({current})',
                ")",
                _marker(stripper, ":end"),
                f"# 블록 밖 산문 ({current}) 은 정리된다",
            )
        )
        + "\n"
    )
    rewritten, count = stripper.rewrite_python(source)
    assert count == 1
    assert f'"guest ({legacy})",   # 옛 세대 ({legacy})' in rewritten
    assert f'"guest ({current})",  # 현 세대 ({current})' in rewritten
    assert "# 블록 밖 산문 은 정리된다" in rewritten


def test_data_literal_spans_report_line_and_block_forms(stripper):
    ref = _ticket()
    source = (
        "\n".join(
            (
                f'LINE = "({ref})"  {_marker(stripper)}',
                "PLAIN = 1",
                _marker(stripper, ":begin"),
                f'BLOCK = "({ref})"',
                _marker(stripper, ":end"),
            )
        )
        + "\n"
    )
    covered = [source[start:end] for start, end in stripper._data_literal_spans(source)]
    assert len(covered) == 2
    assert covered[0] == f'LINE = "({ref})"  {_marker(stripper)}\n'
    assert covered[1].splitlines() == [
        _marker(stripper, ":begin"),
        f'BLOCK = "({ref})"',
        _marker(stripper, ":end"),
    ]
    assert "PLAIN = 1" not in "".join(covered)


@pytest.mark.parametrize(
    ("suffixes", "expected"),
    [
        ((":begin",), "닫히지 않았다"),
        ((":end",), "짝이 되는"),
        ((":begin", ":begin", ":end"), "다시 열렸다"),
    ],
    ids=["begin-only", "end-only", "nested-begin"],
)
def test_unbalanced_block_markers_fail_loudly(stripper, suffixes, expected):
    source = "".join(
        f"{_marker(stripper, suffix)}\nVALUE = 1\n" for suffix in suffixes
    )
    with pytest.raises(stripper.DataLiteralMarkerError, match=expected):
        stripper._data_literal_spans(source)
    with pytest.raises(stripper.DataLiteralMarkerError, match=expected):
        stripper.rewrite_python(source)


def test_marked_regions_are_not_prose_for_downstream_consumers(stripper):
    ref = _ticket()
    source = _guest_marker_source(stripper, ref, marked=True)
    assert stripper.prose_token_spans(source) == []
    assert stripper._prose_count(source) == 0
    # 출하 lint 는 산문 span 단위로 소비한다 — 표식 구간은 span 자체가 없어 검사 대상 밖이다.
    assert not [
        match
        for span in stripper.prose_token_spans(source)
        for match in stripper._actionable_matches(span.text)
    ]


def test_marker_text_inside_string_literal_is_not_a_marker(stripper):
    """리터럴 속 표식 글자는 데이터지 표식이 아니다 — 그 줄의 산문은 종전대로 검사한다."""
    ref = _ticket()
    source = f'VALUE = "payload {_marker(stripper)}"  # 산문 참조 ({ref})\n'
    rewritten, count = stripper.rewrite_python(source)
    assert stripper._data_literal_spans(source) == []
    assert count == 1
    assert rewritten == f'VALUE = "payload {_marker(stripper)}"  # 산문 참조\n'


def test_marker_text_inside_triple_quoted_string_does_not_break_pairing(stripper):
    """여러 줄 문자열 속 경계 글자는 짝 판정 대상이 아니다(유효 소스가 실패하면 안 된다)."""
    ref = _ticket()
    source = (
        '"""표식 사용 예시:\n'
        f"{_marker(stripper, ':begin')} 은 여기서 표식이 아니다\n"
        f"표식 밖 참조 ({ref}) 는 정리된다\n"
        '"""\n'
    )
    rewritten, count = stripper.rewrite_python(source)
    assert stripper._data_literal_spans(source) == []
    assert count == 1
    assert "표식 밖 참조 는 정리된다" in rewritten


def test_marked_line_does_not_shield_the_rest_of_a_multiline_token(stripper):
    """표식 줄과 일부만 겹치는 여러 줄 산문은 나머지 줄을 계속 검사한다."""
    ref = _ticket()
    source = (
        f'"""첫 줄 ({ref}) 은 표식 밖이다\n'
        f"둘째 줄 ({ref}) 도 표식 밖이다\n"
        f'"""  {_marker(stripper)}\n'
    )
    rewritten, count = stripper.rewrite_python(source)
    assert count == 2
    assert "첫 줄 은 표식 밖이다" in rewritten
    assert "둘째 줄 도 표식 밖이다" in rewritten
    assert f'"""  {_marker(stripper)}' in rewritten
    # lint 축도 나머지 줄을 본다 — 토큰 전체가 아니라 보호된 줄만 빠진다.
    assert stripper._prose_count(source) == 2


def test_standalone_marker_protects_the_whole_following_statement(stripper):
    """표식만 있는 라인은 다음 문장 전체를 가리킨다 — 여러 줄 선언도 통째로 보호된다."""
    ref = _ticket()
    source = (
        f"{_marker(stripper)}\n"
        "_WIRE_GENERATIONS = (\n"
        f'    "guest ({ref})",   # 옛 세대 ({ref})\n'
        f'    "guest ({ref})",   # 현 세대 ({ref})\n'
        ")\n"
        f"# 문장 밖 산문 ({ref}) 은 정리된다\n"
    )
    rewritten, count = stripper.rewrite_python(source)
    assert count == 1
    assert f'"guest ({ref})",   # 옛 세대 ({ref})' in rewritten
    assert f'"guest ({ref})",   # 현 세대 ({ref})' in rewritten
    assert "# 문장 밖 산문 은 정리된다" in rewritten


def test_block_marker_after_code_is_not_a_boundary_and_does_not_raise(stripper):
    """코드 뒤 경계 표식은 구획을 열지 않는다 — 정상 파일이 짝 불일치로 죽으면 안 된다."""
    ref = _ticket()
    source = (
        f"X = 1  {_marker(stripper, ':begin')}\n"
        f'Y = "wire ({ref})"\n'
        f"Z = 1  # prose ({ref})\n"
    )
    rewritten, count = stripper.rewrite_python(source)
    assert stripper._data_literal_spans(source) == []
    assert count == 1
    assert f'Y = "wire ({ref})"' in rewritten
    assert "Z = 1  # prose\n" in rewritten


@pytest.mark.parametrize("suffix", [":begin", ":end"], ids=["begin", "end"])
def test_line_input_of_a_boundary_marker_is_inert_in_exposed_api(stripper, suffix):
    """노출 API 에 라인 하나만 들어와도 표식 판정이 재적용돼 죽지 않는다."""
    line = f"X = 1  {_marker(stripper, suffix)}"
    assert stripper.remove_matched_spans(line) == line
    assert stripper._specific_matches(line) == []
    assert stripper.strip_prose_text(line) == (line, 0)


@pytest.mark.parametrize(
    "suffix",
    [":start", "s", "-ish"],
    ids=["wrong-edge-word", "plural-typo", "suffixed-typo"],
)
def test_near_miss_marker_fails_loudly(stripper, suffix):
    """오탈자 표식은 보호를 못 만든 채 조용히 지나가면 안 된다."""
    ref = _ticket()
    source = f"{_marker(stripper, suffix)}\nV = 1  # prose ({ref})\n"
    with pytest.raises(stripper.DataLiteralMarkerError, match="정확 형태가 아니다"):
        stripper.rewrite_python(source)
    with pytest.raises(stripper.DataLiteralMarkerError, match="정확 형태가 아니다"):
        stripper._data_literal_spans(source)


def test_prose_mentioning_the_marker_mid_comment_is_not_a_marker(stripper):
    """주석 중간의 표식 언급은 표식이 아니다 — near-miss 판정이 산문을 잡으면 안 된다."""
    ref = _ticket()
    source = f"# 표식은 {_marker(stripper)[2:]} 를 쓴다 ({ref})\n"
    rewritten, count = stripper.rewrite_python(source)
    assert stripper._data_literal_spans(source) == []
    assert count == 1
    assert rewritten == f"# 표식은 {_marker(stripper)[2:]} 를 쓴다\n"


def _lone_carriage_return_sources(stripper, ref: str) -> dict[str, str]:
    """표식이 있는데 개행이 LF 가 아닌 소스들 — 라인 판정이 어긋나는 형태."""
    return {
        "line-marker": (
            f'V = "wire ({ref})"  {_marker(stripper)}\r'
            f"W = 1  # 표식 밖 산문 ({ref})\n"
        ),
        # 짝이 맞는 블록이라도 경계 라인의 CR 이 구간을 블록 밖 논리 라인까지 늘린다.
        "closed-block": (
            f"{_marker(stripper, ':begin')}\n"
            f'V = "wire ({ref})"\n'
            f"{_marker(stripper, ':end')}\r"
            f"X = 1  # 표식 밖 산문 ({ref})\n"
        ),
        "unpaired-block": (
            f"{_marker(stripper, ':begin')}\r"
            f'V = "wire ({ref})"\r'
            f"{_marker(stripper, ':end')}\n"
            f"W = 1  # 표식 밖 산문 ({ref})\n"
        ),
    }


@pytest.mark.parametrize(
    "shape", ["line-marker", "closed-block", "unpaired-block"]
)
def test_lone_carriage_return_with_markers_is_rejected(stripper, shape):
    """표식이 있는 소스의 홀로 선 CR 은 거부한다 — 구간이 의도 밖까지 번지면 참조가 샌다."""
    ref = _ticket()
    source = _lone_carriage_return_sources(stripper, ref)[shape]
    for call in (stripper._data_literal_spans, stripper.rewrite_python):
        with pytest.raises(stripper.DataLiteralMarkerError, match="CR"):
            call(source)
    # 거부이므로 산출물이 없다 — 표식 밖 참조가 조용히 사라질 여지 자체가 없다.
    assert source.count(ref) >= 2


def test_lone_carriage_return_without_markers_keeps_previous_behaviour(stripper):
    """표식이 없는 소스는 종전대로다 — 거부는 표식이 있는 입력에만 적용한다."""
    ref = _ticket()
    source = f"A = 1  # 산문 ({ref})\rB = 2\n"
    assert stripper._data_literal_spans(source) == []
    rewritten, count = stripper.rewrite_python(source)
    assert count == 1
    assert rewritten == "A = 1  # 산문\rB = 2\n"


def test_process_python_counts_marked_region_as_allowed_data(stripper, tmp_path):
    ref = _ticket()
    source = _guest_marker_source(stripper, ref, marked=True)
    path = tmp_path / "wire.py"
    path.write_text(source, encoding="utf-8")
    result = stripper.process_python(path, tmp_path, write=True, verify_delta=True)
    assert (result.replacements, result.after_prose, result.actionable_after) == (
        0,
        0,
        0,
    )
    assert result.before_prose_raw == 0
    assert result.allowed_non_prose == result.before_raw == 2
    assert result.residual_lines == []
    assert path.read_text(encoding="utf-8") == source


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
    path.write_bytes(b'"""reason (T-1111)"""\r\n# retained\r\n')
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
        ("# 사유 (T-0146·).", "# 사유 ().", "empty-paren"),
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
