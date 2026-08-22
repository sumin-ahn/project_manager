"""Prevent exact private references from returning to shipped engine prose."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/strip_private_refs.py"

# T-0810 — 스트립이 사설 참조 토큰만 지우고 감싸던 괄호를 남긴 잔재(`()`)를 잡는다. 코드 호출/
# 식별자(`foo()`)·백틱 인라인코드(`` `()` ``)·키워드형 표기(`touches=()`)·callable 타입 표기
# (`() ->`)는 정당한 표기라 제외한다.
_EMPTY_PAREN_RE = re.compile(r"(?<![A-Za-z0-9_\]\.\>=])\(\s*\)(?!\s*->)")


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


def _empty_delimiter_offenders(source: str, scanner) -> list[tuple[int, str]]:
    """산문(주석·docstring) 토큰 안 빈 괄호 잔재를 (line, matched_text) 로 낸다."""
    offenders: list[tuple[int, str]] = []
    for span in scanner.prose_token_spans(source):
        text = span.text
        protected = [
            (match.start(), match.end())
            for match in scanner._INLINE_CODE_RE.finditer(text)
        ]
        for match in _EMPTY_PAREN_RE.finditer(text):
            if any(start <= match.start() < end for start, end in protected):
                continue
            line = span.line + text.count("\n", 0, match.start())
            offenders.append((line, match.group()))
    return offenders


def test_shipped_engine_prose_has_no_empty_delimiter_remnants(scanner):
    """스트립 잔재(토큰만 지우고 남은 빈 괄호) 0 — 시야·민감도·실 표면을 한 게이트로 잠근다(T-0810)."""
    # 시야 == 출하 표면: shipping_paths() 파생 목록이 독립 glob 을 전부 포함해야 한다
    # (T-0814 의 _engine_surfaces() 개편과 결합을 만들지 않기 위해 이 표면을 직접 소비한다).
    python_paths, _ = scanner.shipping_paths(REPO)
    python_paths_set = set(python_paths)
    independent_canonical = set((REPO / ".project_manager/tools").glob("*.py"))
    missing = independent_canonical - python_paths_set
    assert not missing, (
        "shipping_paths() 파생 목록이 독립 glob 을 누락한다 — 가드 시야가 출하 표면보다 좁다: "
        f"{sorted(p.as_posix() for p in missing)}"
    )

    # 민감도 — 합성 잔재(반드시 red)와 합성 정상(반드시 green)을 양방향 실측한다.
    positive_samples = [
        "# 갱신 ().",
        "# 확인().",
        "# (): 사유",
        '"""원자 갱신 ()."""',
        "# 정체성(() 명시 전달)은",
        "# **한정** ():",
        "# ( ) 공백 낀 형태",
    ]
    negative_samples = [
        "# board_root() 가 판정한다",
        "# 빈 튜플은 `()` 로 쓴다",
        "# touches=() 라 허용된다",
        "# clock : () -> 초",
        "rows = status()   # 무인자 조회",
        'x = re.compile(r"[^()\\s]+")',
        "# 빈 dict {} · 빈 list []",
    ]
    for sample in positive_samples:
        assert _empty_delimiter_offenders(sample, scanner), f"놓친 잔재: {sample!r}"
    for sample in negative_samples:
        assert not _empty_delimiter_offenders(sample, scanner), f"오탐: {sample!r}"

    # 실 표면 — offender 0 (실패 시 count 와 path:line 표시).
    offenders: list[str] = []
    for path in python_paths:
        source = path.read_text(encoding="utf-8")
        for line, _matched in _empty_delimiter_offenders(source, scanner):
            relative = path.relative_to(REPO).as_posix()
            offenders.append(f"{relative}:{line}")
    assert not offenders, (
        "Empty parentheses left behind by mechanical private-reference removal "
        f"must stay out of shipped engine prose; count={len(offenders)}, first={offenders[:50]}"
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
