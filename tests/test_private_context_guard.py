"""Keep private development context out of shipped prose."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCANNER_SCRIPT = REPO / "scripts/strip_private_refs.py"
DATA_DIR = REPO / "tests/data"
HARD_REPORT = DATA_DIR / "private_context_hard_allowlist.json"
RATCHET_BASELINE = DATA_DIR / "private_context_baseline.json"

HARD_PATTERNS = {
    "work_item": re.compile(
        r"(?<![A-Za-z0-9_])(?:T|ADR)-\d{4}(?!\d)"
    ),
    "private_section": re.compile(
        r"(?i:\bspike\s+§\s*[^\s,;:)\]}]+)"
        r"|§\s*[A-Z]\d{1,2}[A-Za-z]?"
    ),
}
RATCHET_PATTERNS = {
    "circled_marker": re.compile(r"[\u2460-\u2473\u3251-\u325f]"),
    "decision_label": re.compile(r"\b[A-Z]\d{1,2}\b"),
    "session_stamp": re.compile(
        r"\bPM\s+\d+\b|\b\d{4}-\d{2}-\d{2}\b"
        r"|(?<!\d)\d+차(?![A-Za-z0-9_])"
    ),
}


def _load_prose_scanner():
    spec = importlib.util.spec_from_file_location(
        "private_context_prose_scanner", SCANNER_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROSE_SCANNER = _load_prose_scanner()


@dataclass(frozen=True)
class Occurrence:
    path: str
    line: int
    kind: str
    match: str
    surface: str
    digest: str


def _normalise_line(text: str) -> str:
    return " ".join(text.strip().split())


def _occurrence(
    *,
    path: str,
    source: str,
    start: int,
    end: int,
    kind: str,
    surface: str,
) -> Occurrence:
    line_start = source.rfind("\n", 0, start) + 1
    line_end = source.find("\n", end)
    if line_end < 0:
        line_end = len(source)
    normalised = _normalise_line(source[line_start:line_end])
    digest = hashlib.sha256(
        f"{kind}\0{normalised}".encode("utf-8")
    ).hexdigest()
    return Occurrence(
        path=path,
        line=source.count("\n", 0, start) + 1,
        kind=kind,
        match=source[start:end],
        surface=surface,
        digest=digest,
    )


def _scan_ranges(
    *,
    path: str,
    source: str,
    ranges: list[tuple[int, int, str]],
    patterns: dict[str, re.Pattern[str]],
) -> list[Occurrence]:
    found: list[Occurrence] = []
    for range_start, range_end, surface in ranges:
        segment = source[range_start:range_end]
        for kind, pattern in patterns.items():
            for match in pattern.finditer(segment):
                start = range_start + match.start()
                end = range_start + match.end()
                found.append(
                    _occurrence(
                        path=path,
                        source=source,
                        start=start,
                        end=end,
                        kind=kind,
                        surface=surface,
                    )
                )
    return found


def _python_prose_ranges(source: str) -> list[tuple[int, int, str]]:
    return [
        (span.start, span.end, "python-prose")
        for span in PROSE_SCANNER.prose_token_spans(source)
    ]


def _python_hard_occurrences(path: str, source: str) -> list[Occurrence]:
    prose_ranges = _python_prose_ranges(source)
    found: list[Occurrence] = []
    for kind, pattern in HARD_PATTERNS.items():
        for match in pattern.finditer(source):
            surface = (
                "python-prose"
                if any(
                    start <= match.start() and match.end() <= end
                    for start, end, _ in prose_ranges
                )
                else "python-non-prose"
            )
            found.append(
                _occurrence(
                    path=path,
                    source=source,
                    start=match.start(),
                    end=match.end(),
                    kind=kind,
                    surface=surface,
                )
            )
    return found


def _python_ratchet_occurrences(path: str, source: str) -> list[Occurrence]:
    return _scan_ranges(
        path=path,
        source=source,
        ranges=_python_prose_ranges(source),
        patterns=RATCHET_PATTERNS,
    )


def _markdown_occurrences(
    path: str,
    source: str,
    patterns: dict[str, re.Pattern[str]],
) -> list[Occurrence]:
    return _scan_ranges(
        path=path,
        source=source,
        ranges=[(0, len(source), "markdown")],
        patterns=patterns,
    )


def _shipping_paths(root: Path) -> tuple[list[Path], list[Path]]:
    python_paths = set((root / ".project_manager/tools").glob("*.py"))
    markdown_paths = set((root / ".project_manager/wiki").rglob("*.md"))
    markdown_paths.update((root / ".claude").rglob("*.md"))
    entry_doc = root / "CLAUDE.md"
    if entry_doc.is_file():
        markdown_paths.add(entry_doc)

    templates = root / "templates"
    if templates.is_dir():
        for template in templates.iterdir():
            if not template.is_dir():
                continue
            python_paths.update(
                (template / ".project_manager/tools").glob("*.py")
            )
            markdown_paths.update(template.rglob("*.md"))
    return sorted(python_paths), sorted(markdown_paths)


@lru_cache(maxsize=None)
def _collect(root: Path = REPO) -> tuple[list[Occurrence], list[Occurrence]]:
    hard: list[Occurrence] = []
    ratchet: list[Occurrence] = []
    python_paths, markdown_paths = _shipping_paths(root)
    for path in python_paths:
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        hard.extend(_python_hard_occurrences(relative, source))
        ratchet.extend(_python_ratchet_occurrences(relative, source))
    for path in markdown_paths:
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        hard.extend(_markdown_occurrences(relative, source, HARD_PATTERNS))
        ratchet.extend(
            _markdown_occurrences(relative, source, RATCHET_PATTERNS)
        )
    key = lambda item: (
        item.path,
        item.line,
        item.kind,
        item.match,
        item.surface,
        item.digest,
    )
    return sorted(hard, key=key), sorted(ratchet, key=key)


def _hard_report(occurrences: list[Occurrence]) -> dict[str, object]:
    surface_counts = Counter(item.surface for item in occurrences)
    return {
        "version": 1,
        "count": len(occurrences),
        "surface_counts": dict(sorted(surface_counts.items())),
        "entries": [
            {
                "path": item.path,
                "line": item.line,
                "kind": item.kind,
                "match": item.match,
                "surface": item.surface,
                "hash": item.digest,
            }
            for item in occurrences
        ],
    }


def _ratchet_counter(
    occurrences: list[Occurrence],
) -> Counter[tuple[str, str, str]]:
    return Counter(
        (item.path, item.kind, item.digest) for item in occurrences
    )


def _ratchet_baseline(occurrences: list[Occurrence]) -> dict[str, object]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for (path, kind, digest), count in sorted(
        _ratchet_counter(occurrences).items()
    ):
        grouped.setdefault(path, []).append(
            {"kind": kind, "hash": digest, "count": count}
        )
    return {
        "version": 1,
        "count": len(occurrences),
        "paths": grouped,
    }


def _baseline_counter(
    baseline: dict[str, object],
) -> Counter[tuple[str, str, str]]:
    counter: Counter[tuple[str, str, str]] = Counter()
    paths = baseline["paths"]
    assert isinstance(paths, dict)
    for path, entries in paths.items():
        assert isinstance(path, str)
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            counter[(path, entry["kind"], entry["hash"])] = entry["count"]
    return counter


def _inventory_delta(
    expected: Counter[tuple[str, str, str]],
    actual: Counter[tuple[str, str, str]],
) -> tuple[Counter[tuple[str, str, str]], Counter[tuple[str, str, str]]]:
    return actual - expected, expected - actual


def _describe_delta(
    unexpected: Counter[tuple[str, str, str]],
    stale: Counter[tuple[str, str, str]],
) -> str:
    def sample(items: Counter[tuple[str, str, str]]) -> list[str]:
        return [
            f"{path}:{kind}:{digest[:12]} x{count}"
            for (path, kind, digest), count in sorted(items.items())[:20]
        ]

    return (
        f"new={sum(unexpected.values())} {sample(unexpected)}; "
        f"stale={sum(stale.values())} {sample(stale)}"
    )


def test_shipping_inventory_covers_primary_and_template_surfaces():
    python_paths, markdown_paths = _shipping_paths(REPO)
    relative_python = {path.relative_to(REPO).as_posix() for path in python_paths}
    relative_markdown = {
        path.relative_to(REPO).as_posix() for path in markdown_paths
    }
    assert ".project_manager/tools/board.py" in relative_python
    assert ".project_manager/wiki/pm_role.md" in relative_markdown
    assert ".claude/agents/developer.md" in relative_markdown
    assert "CLAUDE.md" in relative_markdown
    path_names = sorted(
        path.name for path in (REPO / "templates").iterdir() if path.is_dir()
    )
    for template in path_names:
        prefix = f"templates/{template}/"
        assert any(path.startswith(prefix) for path in relative_python)
        assert any(path.startswith(prefix) for path in relative_markdown)
    assert path_names


def test_hard_private_context_matches_reviewed_allowlist():
    hard, _ = _collect()
    expected = json.loads(HARD_REPORT.read_text(encoding="utf-8"))
    assert _hard_report(hard) == expected, (
        "Hard private context changed. Review every occurrence and edit "
        f"{HARD_REPORT.relative_to(REPO)} explicitly."
    )


def test_ratchet_matches_reviewed_baseline_without_stale_entries():
    _, ratchet = _collect()
    expected_data = json.loads(RATCHET_BASELINE.read_text(encoding="utf-8"))
    expected = _baseline_counter(expected_data)
    actual = _ratchet_counter(ratchet)
    unexpected, stale = _inventory_delta(expected, actual)
    assert not unexpected and not stale, _describe_delta(unexpected, stale)
    assert expected_data["count"] == sum(expected.values())


@pytest.mark.parametrize(
    "sample",
    [
        "T-" + "1" * 4,
        "ADR-" + "2" * 4,
        "spike " + "§" + "topic",
        "§" + "F" + "3" + "b",
    ],
    ids=["ticket", "decision-record", "named-section", "section-code"],
)
def test_hard_pattern_sensitivity(sample):
    found = _markdown_occurrences("injected.md", sample, HARD_PATTERNS)
    unexpected, stale = _inventory_delta(Counter(), _ratchet_counter(found))
    assert unexpected
    assert not stale


@pytest.mark.parametrize(
    "sample",
    [
        chr(0x2460),
        chr(0x3251),
        "D" + "1",
        "PM " + str(7),
        "-".join((str(2025), str(6).zfill(2), str(4).zfill(2))),
        str(9) + "차",
    ],
    ids=[
        "circled-low",
        "circled-high",
        "decision-label",
        "session",
        "date",
        "round",
    ],
)
def test_ratchet_pattern_sensitivity(sample):
    found = _markdown_occurrences("injected.md", sample, RATCHET_PATTERNS)
    unexpected, stale = _inventory_delta(Counter(), _ratchet_counter(found))
    assert unexpected
    assert not stale


def test_python_scanning_reuses_prose_classifier():
    private_ref = "T-" + "4" * 4
    source = f'"""prose {private_ref}"""\nvalue = "{private_ref}"\n'
    prose = _python_ratchet_occurrences("injected.py", source)
    hard = _python_hard_occurrences("injected.py", source)
    assert not prose
    assert [item.surface for item in hard] == [
        "python-prose",
        "python-non-prose",
    ]


def test_inventory_delta_flags_stale_entries():
    key = ("removed.md", "session_stamp", "0" * 64)
    unexpected, stale = _inventory_delta(Counter({key: 1}), Counter())
    assert not unexpected
    assert stale == Counter({key: 1})
