"""Prevent exact private references from returning to shipped engine prose."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/strip_private_refs.py"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("public_reference_scanner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scanner():
    return _load_scanner()


def _engine_surfaces() -> list[Path]:
    roots = [
        REPO,
        REPO / "templates/codex",
        REPO / "templates/opencode",
        REPO / "templates/claude_code",
    ]
    return [
        path
        for root in roots
        for path in sorted((root / ".project_manager/tools").glob("*.py"))
    ]


def test_shipped_engine_prose_has_no_mechanically_removable_private_refs(scanner):
    offenders: list[str] = []
    for path in _engine_surfaces():
        source = path.read_text(encoding="utf-8")
        for span in scanner.prose_token_spans(source):
            if scanner._actionable_matches(span.text):
                relative = path.relative_to(REPO).as_posix()
                offenders.append(f"{relative}:{span.line}")
    assert not offenders, (
        "Mechanically removable private references must stay out of shipped "
        f"engine prose; count={len(offenders)}, first={offenders[:50]}"
    )


def test_lint_allows_placeholders_and_runtime_data_strings(scanner):
    placeholder = "T-" + "N" * 4
    data_ref = "T-" + "7" * 4
    source = f'\"\"\"Use {placeholder}.\"\"\"\nvalue = \"{data_ref}\"\n'
    assert scanner._prose_count(source) == 0
