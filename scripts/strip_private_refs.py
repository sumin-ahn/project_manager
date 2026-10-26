#!/usr/bin/env python3
"""Remove private work-item references from public prose deterministically.

Python source is inspected with the standard tokenizer.  Only comments and
standalone string expressions used as documentation are rewritten; ordinary
string literals and identifiers are deliberately left untouched.  Markdown
and test inventories are read-only so later phases can be sized without
changing their files.

판정식 자체는 이 스크립트가 아니라 엔진 모듈
``pm_engine.domain.private_refs``가 소유한다. 출하 표면은
``pm_engine.infra.private_surface``를 직접 소비한다. 이 파일은 파일 재작성과 보고서·CLI를 담당한다.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_TOOLS_DIR = Path(__file__).resolve().parents[1] / ".project_manager" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from pm_engine.domain import private_refs
from pm_engine.infra import private_surface

REPO = Path(__file__).resolve().parents[1]


PLACEHOLDER_RE = re.compile(r"(?<![A-Za-z0-9_])(?:T|ADR)-N{4}(?![A-Za-z0-9_])")
_MECHANICAL_MARKDOWN_RE = re.compile(
    r"\[\[\s*" + private_refs.SPECIFIC_REF_RE.pattern
    + r"\s*\]\]|\(\s*" + private_refs.SPECIFIC_REF_RE.pattern
    + r"(?:\s*[·,/;:+]\s*" + private_refs.SPECIFIC_REF_RE.pattern + r")*\s*\)"
    + r"|(?:^|\s)[··]\s*" + private_refs.SPECIFIC_REF_RE.pattern,
    re.MULTILINE,
)


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


def self_sufficiency_issues(before: str, after: str) -> list[str]:
    """Report boundary-pattern increases on any modified line."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    issues: list[str] = []
    for number, (old, new) in enumerate(zip(before_lines, after_lines), 1):
        if old == new:
            continue
        for name, counts in private_refs.line_delta(old, new).items():
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


def _strip_prose_line(text: str) -> tuple[str, int]:
    cleaned, removed, _ = private_refs._line_rewrite_plan(text)
    return cleaned, removed


def rewrite_python(source: str) -> tuple[str, int]:
    """Rewrite only Python prose tokens and return text plus replacement count."""
    original_newlines = source.count("\n")
    spans = private_refs.prose_token_spans(source)
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


def line_invariant(before: str, after: str) -> dict[str, bool]:
    """Verify exact deletion reconstruction and the two line postconditions."""
    return {
        "matched_span_reconstruction": after == private_refs.remove_matched_spans(before),
        "leading_whitespace_preserved": (
            private_refs._leading_whitespace(before) == private_refs._leading_whitespace(after)
        ),
        "meaningful_result": not private_refs._unsafe_result(before, after),
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
                    "delta": private_refs.line_delta(old, new),
                }
            )
    return records


def _delta_summary(
    records: Iterable[dict[str, object]],
) -> dict[str, dict[str, int]]:
    summary = {
        name: {"before": 0, "after": 0, "delta": 0, "increased_lines": 0}
        for name, _ in private_refs._DELTA_PATTERNS
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
    before_raw = len(private_refs.RAW_REF_RE.findall(before))
    before_prose_raw = sum(
        len(private_refs.RAW_REF_RE.findall(span.text)) for span in private_refs.prose_token_spans(before)
    )
    before_prose = private_refs._prose_count(before)
    after_prose = private_refs._prose_count(after)
    actionable_after = sum(
        len(private_refs._actionable_matches(span.text)) for span in private_refs.prose_token_spans(after)
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
            for span in private_refs.prose_token_spans(after)
            for match in private_refs._specific_matches(span.text)
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
        count = len(private_refs.RAW_REF_RE.findall(text))
        if not count:
            continue
        mechanical_count = sum(
            len(private_refs.RAW_REF_RE.findall(match.group()))
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
        count = len(private_refs.RAW_REF_RE.findall(source))
        if not count:
            continue
        prose_count = private_refs._prose_count(source)
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
    _, markdown_paths = private_surface.shipping_paths(root)
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
    python_paths, _ = private_surface.shipping_paths(root)
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