#!/usr/bin/env python3
"""Remove private work-item references from public prose deterministically.

Python source is inspected with the standard tokenizer.  Only comments and
standalone string expressions used as documentation are rewritten; ordinary
string literals and identifiers are deliberately left untouched.  Markdown
and test inventories are read-only so later phases can be sized without
changing their files.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import random
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO = Path(__file__).resolve().parents[1]
RAW_REF_RE = re.compile(r"(?:T|ADR)-\d{4}")
SPECIFIC_REF_RE = re.compile(r"(?<![A-Za-z0-9_])(?:T|ADR)-\d{4}(?!\d)")
PLACEHOLDER_RE = re.compile(r"(?<![A-Za-z0-9_])(?:T|ADR)-N{4}(?![A-Za-z0-9_])")
_INLINE_CODE_RE = re.compile(r"(?<!`)({})(?!`)(.*?)\1(?!`)".format(r"`+"))
_MECHANICAL_MARKDOWN_RE = re.compile(
    r"\[\[\s*" + SPECIFIC_REF_RE.pattern
    + r"\s*\]\]|\(\s*" + SPECIFIC_REF_RE.pattern
    + r"(?:\s*[·,/;:+]\s*" + SPECIFIC_REF_RE.pattern + r")*\s*\)"
    + r"|(?:^|\s)[··]\s*" + SPECIFIC_REF_RE.pattern,
    re.MULTILINE,
)
_REFERENCE_ONLY_WRAPPER_RE = re.compile(
    r"\([ \t]*"
    + SPECIFIC_REF_RE.pattern
    + r"(?:[ \t]*·[ \t]*"
    + SPECIFIC_REF_RE.pattern
    + r")*[ \t]*\)"
)
_DELTA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("separator-space", re.compile(r"· ")),
    ("double-separator", re.compile(r"··")),
    ("open-separator", re.compile(r"\(·")),
    ("separator-close", re.compile(r"·\)")),
    ("space-close", re.compile(r" \)")),
    ("open-space", re.compile(r"\( ")),
    ("trailing-separator", re.compile(r"·$")),
    ("leading-close", re.compile(r"^[ \t]*\)")),
    ("double-space", re.compile(r"(?= {2})")),
    (
        "orphan-marker-after-separator",
        re.compile(
            r"·[ \t]*(?:[\u2460-\u2473\u3251-\u325f\u32b1-\u32bf]"
            r"|[A-Za-z]\d+|\d+[A-Za-z])(?=$|[ \t·,.;:)\]])"
        ),
    ),
)


@dataclass(frozen=True)
class TokenSpan:
    start: int
    end: int
    line: int
    text: str


@dataclass(frozen=True)
class RemovalSpan:
    start: int
    end: int
    references: int


@dataclass
class FileResult:
    path: str
    before_raw: int
    before_prose_raw: int
    before_prose: int
    allowed_non_prose: int
    allowed_prose_data: int
    replacements: int
    after_prose: int
    actionable_after: int
    changed_lines: list[dict[str, object]]
    residual_lines: list[int]
    transformed_text: str


class SelfSufficiencyError(ValueError):
    """Raised when a rewrite introduces a mechanically detectable prose defect."""


def _load_repo_owned_files():
    """공용 repo 소유 파일 열거 seam을 canonical checkout에서 로드한다."""
    path = REPO / ".project_manager" / "tools" / "repo_owned_files.py"
    module_name = f"_private_context_repo_owned_files:{path.resolve()}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "repo_owned_files.py를 로드할 수 없음 — canonical 도구 사본을 확인하라"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except FileNotFoundError as error:
        sys.modules.pop(module_name, None)
        raise RuntimeError(
            "repo_owned_files.py가 없어 canonical 도구 사본을 확인하라"
        ) from error
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def repo_owned_paths(root: Path) -> list[Path]:
    """현재 tree에 존재하는 repo-owned 파일을 공용 ``OWNED`` 규칙으로 반환한다.

    추적 파일과 미추적·비-ignore 파일만 포함한다. git 없는 소스 아카이브에서는 공용
    seam이 loud warning과 함께 filesystem 폴백한다.
    """
    repo_files = _load_repo_owned_files()
    root = root.resolve()
    return [
        path
        for relative in repo_files.list_repo_owned_files(
            root, Path("."), mode=repo_files.OWNED
        )
        if (path := root / relative).is_file()
    ]


def shipping_paths(root: Path) -> tuple[list[Path], list[Path]]:
    """private-context 출하 표면을 repo-owned 열거 결과에서 분류한다.

    ``dashboard.md``와 ``pm_state.md`` 같은 per-clone 파생 파일은 경로별 예외가
    아니라 git ignore/소유 판정으로 빠진다. 따라서 앞으로 다른 파생 파일이 생겨도
    같은 규칙이 적용되고, 비-ignore 신규 출하 파일은 계속 검사한다.
    """
    python_paths: set[Path] = set()
    markdown_paths: set[Path] = set()
    for path in repo_owned_paths(root):
        relative = path.relative_to(root.resolve())
        parts = relative.parts
        if relative == Path("CLAUDE.md"):
            markdown_paths.add(path)
            continue
        if (
            path.suffix == ".py"
            and relative.parent == Path(".project_manager", "tools")
        ):
            python_paths.add(path)
            continue
        if (
            path.suffix == ".md"
            and len(parts) >= 3
            and parts[0] == ".project_manager"
            and parts[1] == "wiki"
        ):
            markdown_paths.add(path)
            continue
        if path.suffix == ".md" and parts and parts[0] == ".claude":
            markdown_paths.add(path)
            continue
        if len(parts) < 3 or parts[0] != "templates":
            continue
        if path.suffix == ".md":
            markdown_paths.add(path)
        elif (
            path.suffix == ".py"
            and len(parts) == 5
            and parts[2] == ".project_manager"
            and parts[3] == "tools"
        ):
            python_paths.add(path)
    return sorted(python_paths), sorted(markdown_paths)


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", text):
        offsets.append(match.end())
    return offsets


def _offset(offsets: list[int], position: tuple[int, int]) -> int:
    line, column = position
    return offsets[line - 1] + column


def _ast_column_to_character(source_line: str, byte_column: int) -> int:
    return len(source_line.encode("utf-8")[:byte_column].decode("utf-8"))


def _doc_expression_ranges(
    source: str,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    tree = ast.parse(source)
    source_lines = source.splitlines(keepends=True)
    return [
        (
            (
                node.value.lineno,
                _ast_column_to_character(
                    source_lines[node.value.lineno - 1], node.value.col_offset
                ),
            ),
            (
                node.value.end_lineno,
                _ast_column_to_character(
                    source_lines[node.value.end_lineno - 1],
                    node.value.end_col_offset,
                ),
            ),
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.end_lineno is not None
        and node.value.end_col_offset is not None
    ]


def prose_token_spans(source: str) -> list[TokenSpan]:
    """Return comment and documentation-expression spans in Python source."""
    offsets = _line_offsets(source)
    doc_ranges = _doc_expression_ranges(source)
    spans: list[TokenSpan] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        is_prose = token.type == tokenize.COMMENT
        is_prose = is_prose or (
            token.type == tokenize.STRING
            and any(
                start <= token.start and token.end <= end
                for start, end in doc_ranges
            )
        )
        if is_prose:
            spans.append(
                TokenSpan(
                    start=_offset(offsets, token.start),
                    end=_offset(offsets, token.end),
                    line=token.start[0],
                    text=token.string,
                )
            )
    return spans


def _specific_matches(text: str) -> list[re.Match[str]]:
    protected = [
        (match.start(), match.end()) for match in _INLINE_CODE_RE.finditer(text)
    ]
    return [
        match
        for match in SPECIFIC_REF_RE.finditer(text)
        if not any(start <= match.start() < end for start, end in protected)
    ]


def _nearest_left_boundary(text: str, start: int) -> int:
    return max((text.rfind(char, 0, start) for char in "·(\"'#"), default=-1)


def _nearest_right_boundary(text: str, end: int) -> int:
    candidates = [
        position
        for char in "·)\"'"
        if (position := text.find(char, end)) >= 0
    ]
    return min(candidates, default=len(text))


def _is_dot_unit(text: str, match: re.Match[str]) -> bool:
    left = _nearest_left_boundary(text, match.start())
    right = _nearest_right_boundary(text, match.end())
    segment = text[left + 1:right].strip(" \t")
    has_dot_boundary = (
        (left >= 0 and text[left] == "·")
        or (right < len(text) and text[right] == "·")
    )
    return has_dot_boundary and segment == match.group()


def _actionable_matches(text: str) -> list[re.Match[str]]:
    """Return references in delimiter units that pass line safety checks."""
    actionable: list[re.Match[str]] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        _, removed, spans = _line_rewrite_plan(body)
        if not removed:
            continue
        actionable.extend(
            match
            for match in _specific_matches(body)
            if any(span.start <= match.start() < span.end for span in spans)
        )
    return actionable


def line_delta_counts(line: str) -> dict[str, int]:
    """Count mechanically invalid boundary patterns on one line."""
    return {
        name: sum(1 for _ in pattern.finditer(line))
        for name, pattern in _DELTA_PATTERNS
    }


def line_delta(before: str, after: str) -> dict[str, dict[str, int]]:
    """Return before/after/delta counts for one changed line."""
    before_counts = line_delta_counts(before)
    after_counts = line_delta_counts(after)
    return {
        name: {
            "before": before_counts[name],
            "after": after_counts[name],
            "delta": after_counts[name] - before_counts[name],
        }
        for name, _ in _DELTA_PATTERNS
    }


def self_sufficiency_issues(before: str, after: str) -> list[str]:
    """Report boundary-pattern increases on any modified line."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    issues: list[str] = []
    for number, (old, new) in enumerate(zip(before_lines, after_lines), 1):
        if old == new:
            continue
        for name, counts in line_delta(old, new).items():
            if counts["delta"] > 0:
                issues.append(f"line {number} {name}: +{counts['delta']}")
    return issues


def strip_prose_text(text: str) -> tuple[str, int]:
    """Strip references only when their whole delimiter unit is a reference."""
    parts: list[str] = []
    count = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        body, removed = _strip_prose_line(body)
        count += removed
        parts.append(body + ending)
    cleaned = "".join(parts)
    return cleaned, count


def _expand_unit_span(text: str, start: int, end: int) -> tuple[int, int]:
    left = start
    while left > 0 and text[left - 1] in " \t":
        left -= 1
    if left < start and text[:left].strip(" \t"):
        return left, end

    right = end
    while right < len(text) and text[right] in " \t":
        right += 1
    if right > end and text[right:].strip(" \t"):
        return start, right
    return start, end


def _candidate_removal_spans(text: str) -> list[RemovalSpan]:
    protected = [
        (match.start(), match.end()) for match in _INLINE_CODE_RE.finditer(text)
    ]
    spans: list[RemovalSpan] = []
    covered_references: set[int] = set()

    for wrapper in _REFERENCE_ONLY_WRAPPER_RE.finditer(text):
        if any(
            start < wrapper.end() and wrapper.start() < end
            for start, end in protected
        ):
            continue
        matches = list(SPECIFIC_REF_RE.finditer(wrapper.group()))
        if not matches:
            continue
        start, end = _expand_unit_span(text, wrapper.start(), wrapper.end())
        spans.append(RemovalSpan(start, end, len(matches)))
        covered_references.update(
            range(wrapper.start(), wrapper.end())
        )

    for match in _specific_matches(text):
        if match.start() in covered_references or not _is_dot_unit(text, match):
            continue
        left = _nearest_left_boundary(text, match.start())
        right = _nearest_right_boundary(text, match.end())
        if right < len(text) and text[right] == "·":
            start, end = match.start(), right + 1
        elif left >= 0 and text[left] == "·":
            start, end = left, match.end()
        else:
            continue
        start, end = _expand_unit_span(text, start, end)
        spans.append(RemovalSpan(start, end, 1))

    spans.sort(key=lambda span: (span.start, span.end))
    merged: list[RemovalSpan] = []
    for span in spans:
        if merged and span.start < merged[-1].end:
            previous = merged[-1]
            merged[-1] = RemovalSpan(
                previous.start,
                max(previous.end, span.end),
                previous.references + span.references,
            )
        else:
            merged.append(span)
    return merged


def remove_matched_spans(
    text: str, spans: Iterable[RemovalSpan] | None = None
) -> str:
    """Delete only the exact matched unit spans from the original text."""
    if spans is None:
        spans = _candidate_removal_spans(text)
    cleaned = text
    for span in reversed(list(spans)):
        cleaned = cleaned[:span.start] + cleaned[span.end:]
    return cleaned


def _leading_whitespace(text: str) -> str:
    match = re.match(r"[ \t]*", text)
    assert match is not None
    return match.group()


def _result_payload(text: str) -> str:
    payload = text.lstrip(" \t")
    if payload.startswith("#"):
        payload = payload[1:].lstrip(" \t")
    quote = re.match(r"(?i:[rubf]*)(?:'''|\"\"\"|'|\")", payload)
    if quote:
        payload = payload[quote.end():].lstrip(" \t")
    return payload


def _unsafe_result(before: str, after: str) -> bool:
    if _leading_whitespace(before) != _leading_whitespace(after):
        return True
    payload = _result_payload(after)
    if not any(character.isalnum() or character == "_" for character in payload):
        return True
    spans = _candidate_removal_spans(before)
    prefix = before[:spans[0].start] if spans else before
    prefix_has_content = any(
        character.isalnum() or character == "_"
        for character in _result_payload(prefix)
    )
    return not prefix_has_content and payload.startswith(
        (".", ",", ";", ":", "!", "?", "·", "—", "–", "…", ")", "]", "}")
    )


def _line_rewrite_plan(text: str) -> tuple[str, int, list[RemovalSpan]]:
    spans = _candidate_removal_spans(text)
    if not spans:
        return text, 0, []
    cleaned = remove_matched_spans(text, spans)
    assert cleaned == remove_matched_spans(text)
    if _unsafe_result(text, cleaned):
        return text, 0, []
    if any(counts["delta"] > 0 for counts in line_delta(text, cleaned).values()):
        return text, 0, []
    return cleaned, sum(span.references for span in spans), spans


def _strip_prose_line(text: str) -> tuple[str, int]:
    cleaned, removed, _ = _line_rewrite_plan(text)
    return cleaned, removed


def rewrite_python(source: str) -> tuple[str, int]:
    """Rewrite only Python prose tokens and return text plus replacement count."""
    original_newlines = source.count("\n")
    spans = prose_token_spans(source)
    replacements: list[tuple[int, int, str]] = []
    count = 0
    for span in spans:
        cleaned, removed = strip_prose_text(span.text)
        if removed:
            replacements.append((span.start, span.end, cleaned))
            count += removed
    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    if source.count("\n") != original_newlines:
        raise ValueError("prose rewrite must preserve the source line count")
    return source, count


def _prose_count(source: str) -> int:
    return sum(
        len(_specific_matches(span.text))
        for span in prose_token_spans(source)
    )


def line_invariant(before: str, after: str) -> dict[str, bool]:
    """Verify exact deletion reconstruction and the two line postconditions."""
    return {
        "matched_span_reconstruction": after == remove_matched_spans(before),
        "leading_whitespace_preserved": (
            _leading_whitespace(before) == _leading_whitespace(after)
        ),
        "meaningful_result": not _unsafe_result(before, after),
    }


def _changed_line_records(before: str, after: str, path: str) -> list[dict[str, object]]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    records: list[dict[str, object]] = []
    for number, (old, new) in enumerate(zip(before_lines, after_lines), 1):
        if old != new:
            records.append(
                {
                    "path": path,
                    "line": number,
                    "before": old,
                    "after": new,
                    "invariant": line_invariant(old, new),
                    "delta": line_delta(old, new),
                }
            )
    return records


def _delta_summary(
    records: Iterable[dict[str, object]],
) -> dict[str, dict[str, int]]:
    summary = {
        name: {"before": 0, "after": 0, "delta": 0, "increased_lines": 0}
        for name, _ in _DELTA_PATTERNS
    }
    for record in records:
        delta = record["delta"]
        assert isinstance(delta, dict)
        for name, counts in delta.items():
            assert isinstance(counts, dict)
            summary[name]["before"] += int(counts["before"])
            summary[name]["after"] += int(counts["after"])
            summary[name]["delta"] += int(counts["delta"])
            summary[name]["increased_lines"] += int(counts["delta"] > 0)
    return summary


def _delta_failures(
    records: Iterable[dict[str, object]],
) -> list[str]:
    failures: list[str] = []
    for record in records:
        delta = record["delta"]
        assert isinstance(delta, dict)
        for name, counts in delta.items():
            assert isinstance(counts, dict)
            if int(counts["delta"]) > 0:
                failures.append(
                    f"{record['path']}:{record['line']} {name} "
                    f"+{counts['delta']}"
                )
    return failures


def _invariant_failures(
    records: Iterable[dict[str, object]],
) -> list[str]:
    failures: list[str] = []
    for record in records:
        invariant = record["invariant"]
        assert isinstance(invariant, dict)
        for name, passed in invariant.items():
            if not passed:
                failures.append(
                    f"{record['path']}:{record['line']} {name}"
                )
    return failures


def process_python(
    path: Path, root: Path, *, write: bool, verify_delta: bool = False
) -> FileResult:
    with path.open("r", encoding="utf-8", newline="") as stream:
        before = stream.read()
    after, replacements = rewrite_python(before)
    relative = path.relative_to(root).as_posix()
    before_raw = len(RAW_REF_RE.findall(before))
    before_prose_raw = sum(
        len(RAW_REF_RE.findall(span.text)) for span in prose_token_spans(before)
    )
    before_prose = _prose_count(before)
    after_prose = _prose_count(after)
    actionable_after = sum(
        len(_actionable_matches(span.text)) for span in prose_token_spans(after)
    )
    changed_lines = _changed_line_records(before, after, relative)
    failures = _invariant_failures(changed_lines) + _delta_failures(changed_lines)
    if verify_delta and failures:
        raise SelfSufficiencyError("; ".join(failures))
    if write and before != after:
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(after)
    residual_lines = sorted(
        {
            span.line + span.text[: match.start()].count("\n")
            for span in prose_token_spans(after)
            for match in _specific_matches(span.text)
        }
    )
    return FileResult(
        path=relative,
        before_raw=before_raw,
        before_prose_raw=before_prose_raw,
        before_prose=before_prose,
        allowed_non_prose=before_raw - before_prose_raw,
        allowed_prose_data=before_prose_raw - before_prose,
        replacements=replacements,
        after_prose=after_prose,
        actionable_after=actionable_after,
        changed_lines=changed_lines,
        residual_lines=residual_lines,
        transformed_text=after,
    )


def _write_residual_report(root: Path, results: Iterable[FileResult], output: Path) -> int:
    sections: list[str] = [
        "# Residual private-reference batch",
        "",
        "Each entry includes three surrounding lines on either side.",
        "",
    ]
    count = sum(result.after_prose for result in results)
    for result in results:
        if not result.residual_lines:
            continue
        lines = result.transformed_text.splitlines()
        for line_number in result.residual_lines:
            start = max(1, line_number - 3)
            end = min(len(lines), line_number + 3)
            sections.append(f"## {result.path}:{line_number}")
            sections.append("")
            sections.append("```text")
            for current in range(start, end + 1):
                sections.append(f"{current:>6}: {lines[current - 1]}")
            sections.extend(["```", ""])
    if count == 0:
        sections.extend(["No residual prose references.", ""])
    output.write_text("\n".join(sections), encoding="utf-8")
    return count


def _write_sample_report(
    records: list[dict[str, object]], output: Path, size: int, seed: int
) -> int:
    rng = random.Random(seed)
    chosen = rng.sample(records, min(size, len(records)))
    lines = [
        "# Deterministic random transformation sample",
        "",
        f"- requested: {size}",
        f"- emitted: {len(chosen)}",
        f"- seed: {seed}",
        "",
        "## Mechanical delta verification",
        "",
        "| pattern | before | after | delta | increased lines |",
        "|---|---:|---:|---:|---:|",
    ]
    summary = _delta_summary(records)
    for name, counts in summary.items():
        lines.append(
            f"| {name} | {counts['before']} | {counts['after']} | "
            f"{counts['delta']:+d} | {counts['increased_lines']} |"
        )
    failures = _delta_failures(records)
    invariant_failures = _invariant_failures(records)
    lines.extend(
        [
            "",
            "- deletion invariant: "
            + (
                f"FAIL — {len(invariant_failures)} violations"
                if invariant_failures
                else "PASS — zero violations"
            ),
            "- mechanical verdict: "
            + (
                f"FAIL — {len(failures)} per-line pattern increases"
                if failures
                else "PASS — zero per-line pattern increases"
            ),
            "",
        ]
    )
    for index, record in enumerate(chosen, 1):
        issues = [
            f"{name} +{counts['delta']}"
            for name, counts in record["delta"].items()
            if counts["delta"] > 0
        ]
        verdict = (
            "FAIL — mechanical delta: " + ", ".join(issues)
            if issues
            else "PASS — mechanical delta has no pattern increase"
        )
        lines.extend(
            [
                f"## Sample {index}: {record['path']}:{record['line']}",
                "",
                f"- before: {record['before']}",
                f"- after:  {record['after']}",
                f"- verification: {verdict}",
                "",
            ]
        )
    output.write_text("\n".join(lines), encoding="utf-8")
    return len(chosen)


def _inventory_markdown(paths: Iterable[Path], root: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    total = mechanical = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        count = len(RAW_REF_RE.findall(text))
        if not count:
            continue
        mechanical_count = sum(
            len(RAW_REF_RE.findall(match.group()))
            for match in _MECHANICAL_MARKDOWN_RE.finditer(text)
        )
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "total": count,
                "mechanical": mechanical_count,
                "rewrite": count - mechanical_count,
            }
        )
        total += count
        mechanical += mechanical_count
    return {
        "files": files,
        "total": total,
        "mechanical": mechanical,
        "rewrite": total - mechanical,
    }


def _inventory_tests(paths: Iterable[Path], root: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    total = prose = 0
    for path in paths:
        source = path.read_text(encoding="utf-8")
        count = len(RAW_REF_RE.findall(source))
        if not count:
            continue
        prose_count = _prose_count(source)
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "total": count,
                "mechanical": prose_count,
                "rewrite": count - prose_count,
            }
        )
        total += count
        prose += prose_count
    return {
        "files": files,
        "total": total,
        "mechanical": prose,
        "rewrite": total - prose,
    }


def _write_inventory(root: Path, output: Path) -> dict[str, object]:
    _, markdown_paths = shipping_paths(root)
    # v1.5.0보다 넓은 출하 표면(CLAUDE.md·templates 포함)을 보고용으로 집계한다.
    tests = sorted((root / "tests").glob("*.py"))
    report = {
        "p2_shipping_markdown": _inventory_markdown(markdown_paths, root),
        "p3_tests": _inventory_tests(tests, root),
    }
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    python_paths, _ = shipping_paths(root)
    canonical_tools = root / ".project_manager" / "tools"
    paths = [path for path in python_paths if path.parent == canonical_tools]
    try:
        results = [
            process_python(
                path, root, write=False, verify_delta=args.verify_delta
            )
            for path in paths
        ]
    except SelfSufficiencyError as error:
        print(f"ERROR mechanical delta verification failed: {error}", file=sys.stderr)
        return 2
    if not args.dry_run:
        for path in paths:
            process_python(
                path, root, write=True, verify_delta=args.verify_delta
            )

    residual_path = report_dir / "p1-residuals.md"
    counter_path = report_dir / "p1-counters.json"
    sample_path = report_dir / "p1-sample.md"
    inventory_path = report_dir / "remaining-inventory.json"
    residual_count = _write_residual_report(root, results, residual_path)
    changed_records = [
        record for result in results for record in result.changed_lines
    ]
    delta_summary = _delta_summary(changed_records)
    delta_failures = _delta_failures(changed_records)
    invariant_failures = _invariant_failures(changed_records)
    emitted = _write_sample_report(
        changed_records, sample_path, args.sample_size, args.seed
    )
    inventory = _write_inventory(root, inventory_path)

    counters = {
        "mode": "dry-run" if args.dry_run else "write",
        "files_scanned": len(results),
        "files_changed": sum(bool(result.replacements) for result in results),
        "before_raw": sum(result.before_raw for result in results),
        "before_prose_raw": sum(result.before_prose_raw for result in results),
        "before_prose": sum(result.before_prose for result in results),
        "allowed_non_prose": sum(result.allowed_non_prose for result in results),
        "allowed_prose_data": sum(result.allowed_prose_data for result in results),
        "replacements": sum(result.replacements for result in results),
        "after_prose": sum(result.after_prose for result in results),
        "residual_entries": residual_count,
        "sample_entries": emitted,
        "invariant_verification": {
            "enabled": args.verify_delta,
            "verdict": "PASS" if not invariant_failures else "FAIL",
            "failure_count": len(invariant_failures),
        },
        "delta_verification": {
            "enabled": args.verify_delta,
            "verdict": "PASS" if not delta_failures else "FAIL",
            "failure_count": len(delta_failures),
            "patterns": delta_summary,
        },
        "per_file": [
            {
                "path": result.path,
                "replacements": result.replacements,
                "residuals": result.after_prose,
            }
            for result in results
            if result.replacements or result.after_prose
        ],
        "reports": {
            "residuals": str(residual_path),
            "sample": str(sample_path),
            "inventory": str(inventory_path),
        },
        "remaining": {
            key: {
                field: value[field]
                for field in ("total", "mechanical", "rewrite")
            }
            for key, value in inventory.items()
        },
    }
    counter_path.write_text(
        json.dumps(counters, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for result in results:
        if result.replacements or result.after_prose:
            print(
                f"{result.path}: replacements={result.replacements} "
                f"residuals={result.after_prose}"
            )
    print(
        "TOTAL "
        f"replacements={counters['replacements']} "
        f"residuals={counters['after_prose']} "
        f"allowed_data={counters['allowed_non_prose'] + counters['allowed_prose_data']}"
    )
    print(f"counter_report={counter_path}")
    actionable_after = sum(result.actionable_after for result in results)
    if actionable_after:
        print(
            f"ERROR removable prose references remain: {actionable_after}",
            file=sys.stderr,
        )
        return 2
    return 0 if counters["after_prose"] == residual_count else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--verify-delta",
        action="store_true",
        help="fail if any changed line gains a mechanical boundary pattern",
    )
    parser.add_argument("--sample-size", type=int, default=30)
    parser.add_argument("--seed", type=int, default=486)
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
