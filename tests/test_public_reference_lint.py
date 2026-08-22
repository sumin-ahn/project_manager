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


def test_shipped_engine_prose_has_no_mechanically_removable_private_refs(scanner):
    offenders: list[str] = []
    python_paths, _ = scanner.shipping_paths(REPO)
    for path in python_paths:
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


# G3 예외 3건 — 채택자가 실제로 읽는 argparse `help=` 문자열이라 데이터 리터럴 표식으로
# 면제하지 않는다(설계 §결정). 스트립 도구·lint 는 문자열 리터럴을 안 건드리므로 대신 이
# 값 단언으로 재유입을 막는다(§I6).
_ORCH_DRIVERS_WITH_HELP_EXCEPTION = (
    "templates/claude_code/.claude/pm_orch_claude.py",
    "templates/codex/.codex/pm_orch_codex.py",
    "templates/opencode/.opencode/pm_orch_opencode.py",
)


def test_orch_driver_help_text_has_no_private_work_item_refs(scanner):
    """3개 하네스 driver 의 `--help` 출력(``--task`` help= 문자열)에 사설 참조가 없다."""
    for relpath in _ORCH_DRIVERS_WITH_HELP_EXCEPTION:
        path = REPO / relpath
        spec = importlib.util.spec_from_file_location(f"orch_driver:{relpath}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        help_text = module.build_parser().format_help()
        assert not scanner.RAW_REF_RE.search(help_text), (
            f"{relpath} --help 출력에 사설 참조 잔존: {help_text}"
        )
