"""Keep private development context out of shipped prose."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tokenize
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

import pytest


REPO = Path(__file__).resolve().parents[1]
SCANNER_SCRIPT = REPO / "scripts/strip_private_refs.py"
DATA_DIR = REPO / "tests/data"
HARD_REPORT = DATA_DIR / "private_context_hard_allowlist.json"
RATCHET_BASELINE = DATA_DIR / "private_context_baseline.json"
HARD_REPORT_VERSION = 2
# 대장 재생성은 이 모듈을 직접 실행한다 — pytest 안에서는 절대 자기치유하지 않는다.
REGENERATE_COMMAND = (
    f"python3 {Path(__file__).resolve().relative_to(REPO).as_posix()} --regenerate"
)

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


def test_repo_owned_files_exec_failure_removes_partial_cache_and_allows_retry(
    tmp_path, monkeypatch
):
    target = tmp_path / ".project_manager/tools/repo_owned_files.py"
    target.parent.mkdir(parents=True)
    target.write_text("raise RuntimeError('exec boom')\n", encoding="utf-8")
    monkeypatch.setattr(PROSE_SCANNER, "REPO", tmp_path)
    module_name = f"_private_context_repo_owned_files:{target.resolve()}"
    sys.modules.pop(module_name, None)

    with pytest.raises(RuntimeError, match="exec boom"):
        PROSE_SCANNER._load_repo_owned_files()

    assert module_name not in sys.modules

    target.write_text("RETRY_MARKER = 'loaded'\n", encoding="utf-8")
    loaded = PROSE_SCANNER._load_repo_owned_files()

    assert loaded.RETRY_MARKER == "loaded"


@dataclass(frozen=True)
class Occurrence:
    path: str
    line: int
    kind: str
    match: str
    surface: str
    digest: str


# (path, kind, match, surface, context-digest) — 대장 엔트리의 라인 무관 신원.
HardKey = tuple[str, str, str, str, str]
InventoryKey = TypeVar("InventoryKey")


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


def _is_data_literal(
    spans: Sequence[tuple[int, int]], start: int, end: int
) -> bool:
    """데이터 구간에 **완전히 포함된** 출현만 면제한다.

    hard/ratchet 패턴의 ``\\s`` 는 개행을 삼키므로 표식 경계를 걸친 출현이 나올 수 있다.
    걸친 출현은 표식 밖 문맥을 함께 담으므로 데이터가 아니다 — 탐지를 유지한다.
    """
    return any(
        span_start <= start and end <= span_end for span_start, span_end in spans
    )


def _scan_ranges(
    *,
    path: str,
    source: str,
    ranges: list[tuple[int, int, str]],
    patterns: dict[str, re.Pattern[str]],
    exempt: Sequence[tuple[int, int]] = (),
) -> list[Occurrence]:
    found: list[Occurrence] = []
    for range_start, range_end, surface in ranges:
        segment = source[range_start:range_end]
        for kind, pattern in patterns.items():
            for match in pattern.finditer(segment):
                start = range_start + match.start()
                end = range_start + match.end()
                if _is_data_literal(exempt, start, end):
                    continue
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
    # 분할 전 토큰 문맥을 읽는다 — 데이터 구간에서 잘라낸 조각만 보면 경계를 걸친 다중행
    # 출현(개행을 삼키는 패턴)이 어느 조각에도 온전히 안 담겨 탐지를 빠져나간다.
    return [
        (span.start, span.end, "python-prose")
        for span in PROSE_SCANNER.prose_context_spans(source)
    ]


def _python_data_literal_spans(source: str) -> list[tuple[int, int]]:
    # 스트립 도구와 같은 판정 함수를 소비한다 — 표식이 붙은 리터럴은 채택자 디스크에
    # 기록되는 wire 문자열이라 사설 참조 재유입이 아니다(판정 사본을 두지 않는다).
    return PROSE_SCANNER._data_literal_spans(source)


def _python_hard_occurrences(path: str, source: str) -> list[Occurrence]:
    prose_ranges = _python_prose_ranges(source)
    data_spans = _python_data_literal_spans(source)
    found: list[Occurrence] = []
    for kind, pattern in HARD_PATTERNS.items():
        for match in pattern.finditer(source):
            if _is_data_literal(data_spans, match.start(), match.end()):
                continue
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
        exempt=_python_data_literal_spans(source),
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
    # 테스트와 재생성 스크립트가 같은 공용 OWNED 열거·표면 분류를 사용해야
    # 한쪽에서만 ignored 파생 파일을 baseline에 다시 넣는 판정 어긋남이 없다.
    return PROSE_SCANNER.shipping_paths(root)


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


def _hard_key(item: Occurrence) -> HardKey:
    """대장 엔트리의 라인 무관 신원.

    자리(``line``)는 키에서 뺀다 — 무관한 라인 이동만으로 대장을 재생성하게 만들던
    결합이 여기서 끊긴다. 대신 ``hash``(그 참조가 놓인 정규화 라인 문맥의 digest)가
    자리 역할을 대신하므로 문구가 바뀌면 여전히 red 다. 같은 파일에서 같은 키가 여러 번
    나오는 경우(한 라인의 반복 출현·문맥이 동일한 라인 중복)는 순번이 아니라 ``count``로
    구분한다 — 순번은 다시 파일 안 순서에 결합되기 때문이다.
    """
    return (item.path, item.kind, item.match, item.surface, item.digest)


def _hard_counter(occurrences: list[Occurrence]) -> Counter[HardKey]:
    return Counter(_hard_key(item) for item in occurrences)


def _hard_baseline_counter(report: dict[str, object]) -> Counter[HardKey]:
    """검토된 대장 파일을 라인 무관 multiset으로 읽는다.

    ``count`` 없는 옛 형식(엔트리마다 ``line``을 들고 출현 1건씩 나열)도 같은 함수로
    읽혀 동일한 multiset이 된다 — 옛 대장에서 새 형식으로의 무손실 마이그레이션 경로다.
    """
    entries = report["entries"]
    assert isinstance(entries, list)
    counter: Counter[HardKey] = Counter()
    for entry in entries:
        assert isinstance(entry, dict)
        key = (
            entry["path"],
            entry["kind"],
            entry["match"],
            entry["surface"],
            entry["hash"],
        )
        counter[key] += int(entry.get("count", 1))
    return counter


def _hard_report(occurrences: list[Occurrence]) -> dict[str, object]:
    surface_counts = Counter(item.surface for item in occurrences)
    return {
        "version": HARD_REPORT_VERSION,
        "count": len(occurrences),
        "surface_counts": dict(sorted(surface_counts.items())),
        "entries": [
            {
                "path": path,
                "kind": kind,
                "match": match,
                "surface": surface,
                "hash": digest,
                "count": count,
            }
            for (path, kind, match, surface, digest), count in sorted(
                _hard_counter(occurrences).items()
            )
        ],
    }


def _format_hard_key(key: HardKey) -> str:
    path, kind, match, surface, digest = key
    return f"{path}:{kind}:{match}:{surface}:{digest[:12]}"


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
    expected: Counter[InventoryKey],
    actual: Counter[InventoryKey],
) -> tuple[Counter[InventoryKey], Counter[InventoryKey]]:
    # ratchet baseline과 hard 대장이 같은 델타 판정을 쓴다(키 모양만 다르다).
    return actual - expected, expected - actual


def _format_ratchet_key(key: tuple[str, str, str]) -> str:
    path, kind, digest = key
    return f"{path}:{kind}:{digest[:12]}"


def _describe_delta(
    unexpected: Counter[InventoryKey],
    stale: Counter[InventoryKey],
    format_key: Callable[[InventoryKey], str],
) -> str:
    # ratchet baseline과 hard 대장이 같은 델타 서술을 쓴다(키 표기만 다르다).
    def sample(items: Counter[InventoryKey]) -> list[str]:
        return [
            f"{format_key(key)} x{count}"
            for key, count in sorted(items.items())[:20]
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


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_shipping_inventory_uses_owned_scope_and_is_deterministic(tmp_path):
    _git(tmp_path, "init", "-q")
    ignore = tmp_path / ".gitignore"
    ignore.write_text(
        "\n".join(
            (
                ".project_manager/wiki/log/dashboard.md",
                "templates/opencode/.opencode/node_modules/",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    tracked = tmp_path / ".project_manager/wiki/pm_role.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked\n", encoding="utf-8")
    template = (
        tmp_path
        / "templates"
        / "opencode"
        / ".project_manager"
        / "tools"
        / "board.py"
    )
    template.parent.mkdir(parents=True)
    template.write_text('"""tracked"""\n', encoding="utf-8")
    _git(
        tmp_path,
        "add",
        ".gitignore",
        str(tracked.relative_to(tmp_path)),
        str(template.relative_to(tmp_path)),
    )

    untracked = tmp_path / ".claude/agents/local.md"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("owned untracked\n", encoding="utf-8")
    dashboard = tmp_path / ".project_manager/wiki/log/dashboard.md"
    dashboard.parent.mkdir(parents=True)
    dashboard.write_text("PM 99\n", encoding="utf-8")
    dependency = (
        tmp_path
        / "templates"
        / "opencode"
        / ".opencode"
        / "node_modules"
        / "package"
        / "README.md"
    )
    dependency.parent.mkdir(parents=True)
    dependency.write_text("D1\n", encoding="utf-8")

    first = _shipping_paths(tmp_path)
    second = _shipping_paths(tmp_path)
    python_paths = set(first[0])
    markdown_paths = set(first[1])
    assert first == second
    assert template in python_paths
    assert tracked in markdown_paths
    assert untracked in markdown_paths
    assert dashboard not in markdown_paths
    assert dependency not in markdown_paths

    # 민감도: 두 파일은 디스크에 실제로 존재하므로 옛 rglob 범위로 되돌리면
    # 위의 ``not in markdown_paths`` 두 단언이 즉시 red가 된다.
    assert dashboard.is_file()
    assert dependency.is_file()


def test_shipping_inventory_git_missing_archive_falls_back_loudly(
    tmp_path, monkeypatch
):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(
        ".project_manager/wiki/log/\n", encoding="utf-8"
    )
    doc = tmp_path / ".project_manager/wiki/pm_role.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("archive\n", encoding="utf-8")
    ignored_derived = tmp_path / ".project_manager/wiki/log/generated.md"
    ignored_derived.parent.mkdir(parents=True)
    ignored_derived.write_text("derived\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", str(doc.relative_to(tmp_path)))
    repo_files = PROSE_SCANNER._load_repo_owned_files()

    _, owned_markdown = _shipping_paths(tmp_path)
    monkeypatch.setattr(repo_files.shutil, "which", lambda _name: None)

    with pytest.warns(repo_files.RepoFilesFallbackWarning, match=r"rc=127"):
        _, fallback_markdown = _shipping_paths(tmp_path)

    assert set(owned_markdown) <= set(fallback_markdown)
    assert doc in fallback_markdown
    assert ignored_derived not in owned_markdown
    assert ignored_derived in fallback_markdown


def test_hard_private_context_matches_reviewed_allowlist():
    hard, _ = _collect()
    expected = json.loads(HARD_REPORT.read_text(encoding="utf-8"))
    unexpected, stale = _inventory_delta(
        _hard_baseline_counter(expected), _hard_counter(hard)
    )
    assert not unexpected and not stale, (
        "Hard private context changed. Review every occurrence, then rerun "
        f"`{REGENERATE_COMMAND}`. "
        + _describe_delta(unexpected, stale, _format_hard_key)
    )
    # 델타가 없어도 구조(정렬·집계 필드·version)까지 정규 덤프와 같아야 한다. 값 비교라
    # 들여쓰기 같은 순수 재포맷은 여기서 통과하고, byte 동일성은 재생성 헬퍼 테스트가 본다.
    assert _hard_report(hard) == expected, (
        "대장 내용은 같지만 구조가 정규 덤프와 다르다 — "
        f"`{REGENERATE_COMMAND}` 로 다시 덤프하라."
    )


_SYNTHETIC_REF = "T-" + "9" * 4
_SYNTHETIC_OTHER = "ADR-" + "8" * 4
_SYNTHETIC_INFLOW = "T-" + "7" * 4


def _synthetic_hard_document() -> str:
    """라인 이동·내용 변경을 나눠 실험하기 위한 합성 출하 문서.

    한 라인 안 반복 출현과 문맥이 같은 중복 라인을 모두 담아 다중 출현 집계까지 태운다.
    """
    return "\n".join(
        (
            f"- 첫 항목은 {_SYNTHETIC_REF} 를 참조한다",
            "",
            f"- 두 번째 항목은 {_SYNTHETIC_OTHER} 를 참조한다",
            f"- 한 라인에 {_SYNTHETIC_REF} 와 {_SYNTHETIC_REF} 가 함께 나온다",
            f"- 반복되는 문맥에서 {_SYNTHETIC_OTHER} 를 본다",
            f"- 반복되는 문맥에서 {_SYNTHETIC_OTHER} 를 본다",
        )
    )


def _synthetic_report(source: str) -> dict[str, object]:
    return _hard_report(_markdown_occurrences("doc.md", source, HARD_PATTERNS))


def _legacy_hard_report(occurrences: list[Occurrence]) -> dict[str, object]:
    """자리(``line``)를 키에 담던 옛 대장 형식 — 마이그레이션 입력 재현용."""
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


def test_hard_ledger_is_invariant_to_line_only_moves():
    """빈 줄 삽입·블록 재배치처럼 자리만 바뀌는 리팩터에서 대장 델타는 0이다."""
    source = _synthetic_hard_document()
    baseline_occurrences = _markdown_occurrences("doc.md", source, HARD_PATTERNS)
    baseline = _hard_report(baseline_occurrences)
    padded_occurrences = _markdown_occurrences(
        "doc.md", "\n\n\n" + source.replace("\n", "\n\n"), HARD_PATTERNS
    )
    lines = source.splitlines()
    reordered = "\n".join(lines[3:] + lines[:3])

    # 민감도: 자리는 실제로 이동했다(불변식이 vacuous 하지 않다).
    assert [item.line for item in baseline_occurrences] != [
        item.line for item in padded_occurrences
    ]
    assert _hard_report(padded_occurrences) == baseline
    assert _synthetic_report(reordered) == baseline
    assert baseline["count"] == len(baseline_occurrences)
    # 자리 중복이 실제로 접혔다 — 집계 축이 태워졌다는 근거.
    assert len(baseline["entries"]) < baseline["count"]


def test_hard_ledger_flags_reference_inflow_removal_and_rewording():
    """참조 유입·삭제·문구 변경은 여전히 red 다(ratchet 성질 유지)."""
    source = _synthetic_hard_document()
    baseline = _synthetic_report(source)
    lines = source.splitlines()

    inflow = _synthetic_report(source + f"\n- 신규 참조 {_SYNTHETIC_INFLOW} 유입")
    unexpected, stale = _inventory_delta(
        _hard_baseline_counter(baseline), _hard_baseline_counter(inflow)
    )
    assert inflow["count"] == baseline["count"] + 1
    assert unexpected and not stale

    removal = _synthetic_report("\n".join(lines[:2] + lines[3:]))
    unexpected, stale = _inventory_delta(
        _hard_baseline_counter(baseline), _hard_baseline_counter(removal)
    )
    assert removal["count"] == baseline["count"] - 1
    assert stale and not unexpected

    reworded = _synthetic_report(
        source.replace("첫 항목은", "첫 항목(문구가 바뀐 항목)은")
    )
    unexpected, stale = _inventory_delta(
        _hard_baseline_counter(baseline), _hard_baseline_counter(reworded)
    )
    # 문구 변경은 출현 수가 아니라 문맥 digest 로 잡힌다.
    assert reworded["count"] == baseline["count"]
    assert unexpected and stale

    # 파일만 바뀌는 이동: 문맥 digest 는 그대로고 path 축만 갈라져 red 가 된다.
    single = f"- 이 항목만 {_SYNTHETIC_REF} 를 참조한다"
    origin = _hard_report(_markdown_occurrences("doc.md", single, HARD_PATTERNS))
    relocated = _hard_report(
        _markdown_occurrences("moved.md", single, HARD_PATTERNS)
    )
    unexpected, stale = _inventory_delta(
        _hard_baseline_counter(origin), _hard_baseline_counter(relocated)
    )
    assert (sum(unexpected.values()), sum(stale.values())) == (1, 1)
    assert origin["entries"][0]["hash"] == relocated["entries"][0]["hash"]

    # 표면만 바뀌는 전환: 같은 라인 텍스트를 prose 에서 non-prose 로 옮겨도 red 다.
    literal = f'"""{_SYNTHETIC_REF} 를 본다"""'
    prose = _hard_report(_python_hard_occurrences("mod.py", literal + "\n"))
    non_prose = _hard_report(
        _python_hard_occurrences("mod.py", f"VALUE = (\n{literal}\n)\n")
    )
    unexpected, stale = _inventory_delta(
        _hard_baseline_counter(prose), _hard_baseline_counter(non_prose)
    )
    assert prose["surface_counts"] == {"python-prose": 1}
    assert non_prose["surface_counts"] == {"python-non-prose": 1}
    assert prose["entries"][0]["hash"] == non_prose["entries"][0]["hash"]
    assert (sum(unexpected.values()), sum(stale.values())) == (1, 1)


def test_hard_ledger_separates_multiple_occurrences_by_count():
    """같은 파일의 동일 match 다중 출현은 자리가 아니라 count 로 구분한다."""
    source = _synthetic_hard_document()
    baseline = _synthetic_report(source)
    entries = baseline["entries"]
    assert isinstance(entries, list)

    assert all("line" not in entry for entry in entries)
    assert sum(entry["count"] for entry in entries) == baseline["count"]
    # 한 라인 안 반복(2회)과 문맥이 같은 중복 라인(2회)이 각각 하나의 엔트리로 접힌다.
    assert sorted(entry["count"] for entry in entries if entry["count"] > 1) == [2, 2]

    lines = source.splitlines()
    dropped = _synthetic_report("\n".join(lines[:-1]))
    _, stale = _inventory_delta(
        _hard_baseline_counter(baseline), _hard_baseline_counter(dropped)
    )
    assert stale


def test_reviewed_ledger_uses_line_independent_schema():
    data = json.loads(HARD_REPORT.read_text(encoding="utf-8"))
    entries = data["entries"]
    keys = [
        (
            entry["path"],
            entry["kind"],
            entry["match"],
            entry["surface"],
            entry["hash"],
        )
        for entry in entries
    ]

    assert data["version"] == HARD_REPORT_VERSION
    assert all("line" not in entry for entry in entries)
    assert data["count"] == sum(entry["count"] for entry in entries)
    assert data["count"] == sum(data["surface_counts"].values())
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_legacy_line_keyed_ledger_migrates_without_loss():
    """옛 (path, line, match) 대장은 같은 multiset 으로 읽혀 출현 총계를 보존한다."""
    occurrences = _markdown_occurrences(
        "doc.md", _synthetic_hard_document(), HARD_PATTERNS
    )
    legacy = _legacy_hard_report(occurrences)
    migrated = _hard_report(occurrences)

    assert _hard_baseline_counter(legacy) == _hard_baseline_counter(migrated)
    assert legacy["count"] == migrated["count"]
    assert legacy["surface_counts"] == migrated["surface_counts"]
    assert len(migrated["entries"]) < len(legacy["entries"])


def test_regeneration_helper_reproduces_reviewed_ledger(tmp_path):
    destination = tmp_path / "regenerated.json"

    assert main(["--regenerate", "--output", str(destination)]) == 0
    assert destination.read_text(encoding="utf-8") == HARD_REPORT.read_text(
        encoding="utf-8"
    )
    assert main(["--output", str(destination)]) == 0

    tampered = json.loads(destination.read_text(encoding="utf-8"))
    entries = tampered["entries"]
    assert isinstance(entries, list)
    entries.pop()
    destination.write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 검사 모드는 drift 를 보고만 하고 파일을 고치지 않는다(pytest 자기치유 차단).
    assert main(["--output", str(destination)]) == 1
    assert json.loads(destination.read_text(encoding="utf-8")) == tampered


def test_ratchet_matches_reviewed_baseline_without_stale_entries():
    _, ratchet = _collect()
    expected_data = json.loads(RATCHET_BASELINE.read_text(encoding="utf-8"))
    expected = _baseline_counter(expected_data)
    actual = _ratchet_counter(ratchet)
    unexpected, stale = _inventory_delta(expected, actual)
    assert not unexpected and not stale, _describe_delta(
        unexpected, stale, _format_ratchet_key
    )
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


def test_marked_data_literals_are_not_reference_inflow():
    """표식이 붙은 wire 리터럴은 재유입이 아니다 — 스트립과 같은 구간 판정을 소비한다."""
    marker = "# " + PROSE_SCANNER._DATA_LITERAL_MARKER
    source = "\n".join(
        (
            marker + ":begin",
            f'_WIRE_BEGIN = "<!-- guest {_SYNTHETIC_REF} begin -->"',
            marker + ":end",
            f'_WIRE_END = "<!-- guest {_SYNTHETIC_OTHER} end -->"  {marker}',
            "",
        )
    )

    assert not _python_hard_occurrences("wire.py", source)
    assert not _python_ratchet_occurrences("wire.py", source)

    # 민감도: 표식을 떼면 같은 리터럴이 즉시 재유입으로 잡힌다.
    bare = (
        source.replace(marker + ":begin\n", "")
        .replace(marker + ":end\n", "")
        .replace("  " + marker, "")
    )
    assert [item.match for item in _python_hard_occurrences("wire.py", bare)] == [
        _SYNTHETIC_REF,
        _SYNTHETIC_OTHER,
    ]


def test_marker_text_inside_string_literal_does_not_hide_hard_reference():
    """리터럴 속 표식 글자로는 재유입 검사를 우회하지 못한다 — 표식은 주석 토큰만이다."""
    marker = "# " + PROSE_SCANNER._DATA_LITERAL_MARKER
    source = f'VALUE = "payload {marker} {_SYNTHETIC_REF}"\n'

    assert PROSE_SCANNER._data_literal_spans(source) == []
    assert [item.match for item in _python_hard_occurrences("wire.py", source)] == [
        _SYNTHETIC_REF
    ]

    # 같은 참조를 진짜 주석 표식으로 덮으면 그때는 데이터로 빠진다(민감도 대조).
    covered = f'VALUE = "payload {_SYNTHETIC_REF}"  {marker}\n'
    assert not _python_hard_occurrences("wire.py", covered)


_SECTION_MARK = "§"
_SECTION_CODE = "F" + "3"
_SESSION_LABEL = "PM"


def test_data_literal_exemption_requires_full_containment():
    """면제는 완전 포함만 — 경계를 걸친 출현은 표식 밖 문맥을 담아 데이터가 아니다."""
    spans = [(10, 20)]

    assert _is_data_literal(spans, 10, 20)
    assert _is_data_literal(spans, 12, 18)
    # 앞에서 시작해 안으로 들어오는 출현 / 안에서 시작해 밖으로 나가는 출현.
    assert not _is_data_literal(spans, 8, 12)
    assert not _is_data_literal(spans, 18, 24)
    assert not _is_data_literal(spans, 8, 24)
    assert not _is_data_literal([], 12, 18)


def test_hard_match_straddling_a_data_boundary_stays_detected():
    """표식 경계를 걸친 hard 출현은 면제되지 않는다(양방향)."""
    marker = "# " + PROSE_SCANNER._DATA_LITERAL_MARKER
    entering = f'A = """{_SECTION_MARK}\n{_SECTION_CODE}"""  {marker}\n'
    leaving = f"B = 1  {marker} 설명 {_SECTION_MARK}\n{_SECTION_CODE} = 2\n"
    straddle = f"{_SECTION_MARK}\n{_SECTION_CODE}"

    assert [item.match for item in _python_hard_occurrences("wire.py", entering)] == [
        straddle
    ]
    assert [item.match for item in _python_hard_occurrences("wire.py", leaving)] == [
        straddle
    ]

    # 민감도: 같은 출현이 표식 구간 안에 온전히 들어가면 그때는 면제된다.
    contained = f"C = 1  {marker} 설명 {_SECTION_MARK} {_SECTION_CODE}\n"
    assert not _python_hard_occurrences("wire.py", contained)


def test_ratchet_match_straddling_a_data_boundary_stays_detected():
    """표식 경계를 걸친 ratchet 출현도 유지된다 — 판정은 분할 전 문맥에서 한다.

    반대 방향(구간 안에서 시작해 밖으로 나가는 출현)은 ratchet 축에서 만들 수 없다.
    구간이 라인 단위이고 산문 범위가 토큰 단위라, 한 토큰 안에서 앞줄만 보호되고 뒷줄이
    열리는 형태가 나오지 않는다. 그 방향은 완전 포함 판정 테스트가 직접 덮는다.
    """
    marker = "# " + PROSE_SCANNER._DATA_LITERAL_MARKER
    source = f'"""문맥 {_SESSION_LABEL}\n{7} 회차"""  {marker}\n'

    assert [item.match for item in _python_ratchet_occurrences("wire.py", source)] == [
        f"{_SESSION_LABEL}\n{7}"
    ]

    # 민감도: 출현 전체가 표식 라인 안에 들어가면 면제된다.
    contained = f'"""문맥\n{_SESSION_LABEL} {7} 회차"""  {marker}\n'
    assert not _python_ratchet_occurrences("wire.py", contained)


def test_inventory_delta_flags_stale_entries():
    key = ("removed.md", "session_stamp", "0" * 64)
    unexpected, stale = _inventory_delta(Counter({key: 1}), Counter())
    assert not unexpected
    assert stale == Counter({key: 1})


# ── circled 마커·F-라벨 판정면 확장 (STRING·FSTRING_MIDDLE·COMMENT) ─────────────
#
# 현행 사설-참조 가드(``prose_context_spans`` 계열)는 COMMENT·doc-expression STRING(모듈/함수
# docstring)만 본다 — 함수-호출 인자 문자열도, f-string 내용(Python 3.12+ 는 ``FSTRING_MIDDLE``
# 로 토큰화돼 ``STRING`` 이 아니다)도 구조적으로 못 본다. circled 번호·``F1``·``F6`` 류 사설
# fault 라벨은 그 시야 밖(런타임 문자열)에 몰려 있었다. 아래는 이 클래스 전용 판정면 —
# 기존 hard/ratchet 대장(``private_context_baseline.json``·``private_context_hard_allowlist.json``)
# 이 소비하는 ``_python_prose_ranges``/``RATCHET_PATTERNS`` 는 건드리지 않는다(그 대장은 T-번호·
# ADR-번호·세션 표기 등 다른 클래스를 추적하며, 이 판정면과 공유하면 대장 재생성이 강제된다).

_MARKER_LABEL_PATTERNS = dict(PROSE_SCANNER._DELTA_PATTERNS)
_CIRCLED_MARKER_RE = _MARKER_LABEL_PATTERNS["circled-marker"]
_DESIGN_LABEL_RE = _MARKER_LABEL_PATTERNS["design-label"]
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)
# Python 3.12+ 토큰 모델 — f-string 내용이 ``FSTRING_MIDDLE`` 로 분리된다. 3.11 은 f-string
# 전체가 단일 ``STRING`` 이라 그 축이 없다(지원 하한이 3.11 이므로 두 모델 다 통과해야 한다).
_HAS_FSTRING_TOKENS = _FSTRING_MIDDLE is not None
_MARKER_LABEL_TOOLS_DIR = REPO / ".project_manager" / "tools"

# 문자열 리터럴 escape 1개. 채택자가 보는 것은 소스 표기가 아니라 **런타임 상수 값**이므로
# ``"\u2460"``·``"\N{CIRCLED DIGIT ONE}"`` 도 ``"①"`` 과 같은 잔재다(주석은 소스 표기 그대로
# 읽히므로 해석하지 않는다).
_STRING_ESCAPE_RE = re.compile(
    r"\\(?:N\{[^{}]*\}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}|[0-7]{1,3}|.)",
    re.DOTALL,
)
_SIMPLE_ESCAPE_VALUES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
    "\\": "\\", "'": "'", '"': '"', "\n": "",  # 마지막은 행 이음(line continuation).
}


def _escape_value(escape: str) -> str:
    """escape 시퀀스 1개의 런타임 값. Python 이 해석하지 않는 표기는 원문 그대로 둔다."""
    body = escape[1:]
    try:
        if body.startswith("N{"):
            return unicodedata.lookup(body[2:-1])
        if body[0] in "uUx":
            return chr(int(body[1:], 16))
        if body[0] in "01234567":
            return chr(int(body, 8))
    except (KeyError, ValueError, OverflowError):
        return escape
    return _SIMPLE_ESCAPE_VALUES.get(body, escape)


def _decoded_with_offsets(text: str) -> tuple[str, list[int]]:
    """(escape 해석된 값, 각 문자의 원문 offset) — 해석 후에도 원문 좌표를 잃지 않는다."""
    decoded: list[str] = []
    offsets: list[int] = []
    index = 0
    for match in _STRING_ESCAPE_RE.finditer(text):
        for raw_index in range(index, match.start()):
            decoded.append(text[raw_index])
            offsets.append(raw_index)
        for char in _escape_value(match.group()):
            decoded.append(char)
            offsets.append(match.start())
        index = match.end()
    for raw_index in range(index, len(text)):
        decoded.append(text[raw_index])
        offsets.append(raw_index)
    return "".join(decoded), offsets


def _is_raw_string_prefix(text: str) -> bool:
    """``r"..."``·``rf"..."`` 처럼 escape 를 해석하지 않는 리터럴인가."""
    prefix = text[: len(text) - len(text.lstrip("bBfFrRuU"))]
    return "r" in prefix.lower()


def _marker_label_token_spans(
    source: str, *, include_fstring: bool = True
) -> list[tuple[int, str, bool]]:
    """(시작 line, 토큰 원문, escape 해석 여부) — STRING·COMMENT(+FSTRING_MIDDLE) 넓은 판정면.

    ``include_fstring=False`` 로 f-string 축만 뺀 판정면을 재현해 그 축 단독 기여를 잰다
    (민감도 반사실). Python 3.11(``FSTRING_MIDDLE`` 미존재)에서는 f-string 이 이미 단일
    ``STRING`` 토큰이라 그 축이 존재하지 않는다(토큰 모델 차이 — 두 모델의 값은 각 테스트가
    버전별로 정확히 단언한다).

    escape 해석 여부는 런타임 값이 소스 표기와 갈리는 토큰에서만 참이다 — 주석은 소스 표기가
    곧 읽히는 값이라 거짓, raw 리터럴(``r"..."``)은 escape 가 값이 아니라 문자라 거짓.
    """
    spans: list[tuple[int, str, bool]] = []
    fstring_raw: list[bool] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if _FSTRING_START is not None and token.type == _FSTRING_START:
            fstring_raw.append(_is_raw_string_prefix(token.string))
        elif _FSTRING_END is not None and token.type == _FSTRING_END:
            if fstring_raw:
                fstring_raw.pop()
        elif token.type == tokenize.STRING:
            spans.append(
                (token.start[0], token.string, not _is_raw_string_prefix(token.string))
            )
        elif token.type == tokenize.COMMENT:
            spans.append((token.start[0], token.string, False))
        elif (
            include_fstring
            and _FSTRING_MIDDLE is not None
            and token.type == _FSTRING_MIDDLE
        ):
            raw = fstring_raw[-1] if fstring_raw else False
            spans.append((token.start[0], token.string, not raw))
    return spans


def _marker_label_hits(
    source: str, *, include_fstring: bool = True
) -> list[tuple[int, str, str]]:
    """(line, kind, match) — 넓힌 판정면에서 circled 마커·design-label 적중을 낸다.

    ``line`` 은 토큰 시작 라인이 아니라 **적중 문자가 실제로 있는 물리 라인**이다(여러 줄
    STRING·FSTRING_MIDDLE 에서 토큰 시작 라인을 재사용하면 진단 좌표가 틀린다).
    """
    hits: list[tuple[int, str, str]] = []
    for start_line, text, decode in _marker_label_token_spans(
        source, include_fstring=include_fstring
    ):
        if decode:
            judged, offsets = _decoded_with_offsets(text)
        else:
            judged, offsets = text, None
        for kind, pattern in (
            ("circled-marker", _CIRCLED_MARKER_RE),
            ("design-label", _DESIGN_LABEL_RE),
        ):
            for match in pattern.finditer(judged):
                raw_offset = (
                    match.start() if offsets is None else offsets[match.start()]
                )
                hits.append(
                    (start_line + text.count("\n", 0, raw_offset), kind, match.group())
                )
    return hits


def marker_label_residuals(root: Path = REPO) -> list[tuple[str, int, str, str]]:
    """(path, line, kind, match) — canonical 출하 python 표면의 실 잔재 (합성 픽스처 아님).

    ``test_public_reference_lint.py`` 가 여러 출하 타깃(루트·templates 3벌)에 재사용한다.
    """
    residuals: list[tuple[str, int, str, str]] = []
    for path in sorted((root / ".project_manager" / "tools").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(root).as_posix()
        for line, kind, match in _marker_label_hits(source, include_fstring=True):
            residuals.append((relative, line, kind, match))
    return residuals


def test_marker_label_scan_surface_matches_independent_glob_of_shipping_tools():
    """가드가 스캔하는 canonical python 파일 집합이 독립 glob 결과와 같다(시야==표면)."""
    python_paths, _ = _shipping_paths(REPO)
    canonical_python = {
        path for path in python_paths if path.parent == _MARKER_LABEL_TOOLS_DIR
    }
    assert canonical_python == set(sorted(_MARKER_LABEL_TOOLS_DIR.glob("*.py")))


def test_marker_label_scan_fstring_axis_toggle_before_after():
    """f-string 안 마커 — 기능 커버리지는 두 토큰 모델 공통, 축 반사실은 모델별 정확한 값.

    Python 3.12+ 는 f-string 내용을 ``FSTRING_MIDDLE`` 로 내므로 그 축을 빼면 0건이 된다
    (축이 실제로 하중을 받는다). Python 3.11 은 f-string 전체가 단일 ``STRING`` 이라 그 축이
    존재하지 않고, 빼도 같은 1건이 ``STRING`` 으로 그대로 잡힌다. 두 모델 다 *잡는다*는
    결과(기능 커버리지)는 같고 반사실만 다르다 — 어느 쪽도 skip 하지 않는다."""
    marker = chr(0x2460)  # ①
    source = f'call(f"fstring marker {marker}")\n'
    before = _marker_label_hits(source, include_fstring=False)
    after = _marker_label_hits(source, include_fstring=True)
    assert after == [(1, "circled-marker", marker)]
    if _HAS_FSTRING_TOKENS:
        assert before == []
    else:
        assert before == [(1, "circled-marker", marker)]


def test_marker_label_scan_reads_escape_interpreted_string_constants():
    """문자열·f-string 은 escape 가 해석된 **런타임 값**으로 판정한다(주석은 원문 그대로).

    채택자가 보는 것은 소스 표기가 아니라 출력된 값이라, ``"\\u2460"``·
    ``"\\N{CIRCLED DIGIT ONE}"`` 도 ``"①"`` 과 같은 잔재다. 반대로 주석·raw 리터럴·이중
    백슬래시는 그 표기가 곧 읽히는 값이라 마커가 아니다(과확장 0)."""
    marker = chr(0x2460)  # ①
    caught = [(1, "circled-marker", marker), (1, "design-label", "F1")]
    label_only = [(1, "design-label", "F1")]
    cases = [
        (f'x = "{marker} F1"\n', caught),                        # plain 리터럴
        ('x = "\\u2460 F1"\n', caught),                          # plain \u escape
        ('x = "\\N{CIRCLED DIGIT ONE} F1"\n', caught),           # plain \N escape
        (f'x = f"{marker} F1 {{y}}"\n', caught),                 # f-string 리터럴
        ('x = f"\\u2460 F1 {y}"\n', caught),                     # f-string \u escape
        ('x = f"\\N{CIRCLED DIGIT ONE} F1 {y}"\n', caught),      # f-string \N escape
        ('# \\u2460 F1\n', label_only),                          # 주석은 원문 그대로
        (f'# {marker} F1\n', caught),                            # 주석 리터럴은 그대로 잡힘
        ('x = r"\\u2460 F1"\n', label_only),                     # raw 리터럴 = 값이 아님
        ('x = "\\\\u2460 F1"\n', label_only),                    # 이중 백슬래시 = 값이 아님
    ]
    for source, expected in cases:
        assert _marker_label_hits(source) == expected, source


def test_marker_label_hits_report_line_of_match_inside_multiline_token():
    """진단 좌표 — 여러 줄 STRING·FSTRING_MIDDLE 에서도 적중 문자의 실제 물리 라인을 낸다.

    토큰 시작 라인을 재사용하면 사람이 그 자리를 못 찾는다. escape 가 값에 개행을 만들어도
    (``"a\\nb"``) 물리 라인은 원문 기준으로 셈한다."""
    marker = chr(0x2460)  # ①
    source = (
        f'x = """one\ntwo\nthree {marker} F1\n"""\n'      # 1~4 행 (적중은 3 행)
        f'y = f"""alpha\nbeta {{z}}\ngamma {marker}"""\n'  # 5~7 행 (적중은 7 행)
        'z = "line A\\nline B \\u2460"\n'                  # 8 행 (값 개행에 흔들리지 않음)
    )
    assert _marker_label_hits(source) == [
        (3, "circled-marker", marker),
        (3, "design-label", "F1"),
        (7, "circled-marker", marker),
        (8, "circled-marker", marker),
    ]


def test_marker_label_scan_ignores_identifier_quotes_and_code_names():
    """정상 파이썬 식별자는 주석·문자열 인용에서도, 코드 NAME 으로도 0건(오차단 0).

    ``F1_score``·``F1_foo``·``_F1`` 은 채택자에게 뜻이 서는 식별자이지 사설 fault 라벨이
    아니다. 코드 NAME 토큰은 애초에 판정면 밖이라, 그 식별자를 산문에서 인용해도 같은 0 이
    나와야 방향이 일관된다."""
    assert _marker_label_hits("# F1_score 계산 로직\n") == []
    assert _marker_label_hits('x = "F1_foo"\n') == []
    assert _marker_label_hits("# _F1 접두 식별자\n") == []
    assert _marker_label_hits("F1_score = compute()\n") == []
    assert _marker_label_hits('x = "중단 F1 이유"\n') == [(1, "design-label", "F1")]


def test_marker_label_scan_covers_comment_plain_string_and_fstring():
    """세 표면(주석·plain 문자열·f-string) 모두에서 circled 마커·design-label 을 잡는다."""
    marker = chr(0x2460)  # ①
    source = (
        f"# comment {marker}\n"
        f'call("plain-arg {marker} F1")\n'
        f'call(f"fstring {marker}")\n'
    )
    hits = _marker_label_hits(source, include_fstring=True)
    assert sorted(line for line, _, _ in hits) == [1, 2, 2, 3]
    kinds = Counter(kind for _, kind, _ in hits)
    assert kinds["circled-marker"] == 3
    assert kinds["design-label"] == 1


def test_marker_label_axis_sensitivity_reverting_one_marker_trips_and_clears():
    """민감도(양방향, 실 canonical 파일) — circled 마커 1건을 되돌리면 가드 red, f-string 축을
    판정면에서 빼면 그 red 가 사라진다(시야 축의 반사실).

    복원 대상은 이 티켓에서 지운 실제 f-string 자리(``pm_bootstrap.py`` 의
    ``f"공개 제품 worktree (...)"`` ← 원문 ``f"① task worktree (...)"``)."""
    path = _MARKER_LABEL_TOOLS_DIR / "pm_bootstrap.py"
    current = path.read_text(encoding="utf-8")
    fixed_snippet = 'scopes.append((f"공개 제품 worktree ({task_slot})", wt_dir, False))'
    assert fixed_snippet in current, "고정 스니펫이 소스에 없다 — 라인이 바뀌면 갱신하라"
    reverted = current.replace(
        fixed_snippet,
        'scopes.append((f"① task worktree ({task_slot})", wt_dir, False))',
    )
    assert reverted != current

    red = [
        (line, match)
        for line, kind, match in _marker_label_hits(reverted, include_fstring=True)
        if kind == "circled-marker"
    ]
    assert len(red) == 1, f"복원한 1건만 잡혀야 한다 — {red}"

    cleared = [
        (line, match)
        for line, kind, match in _marker_label_hits(reverted, include_fstring=False)
        if kind == "circled-marker"
    ]
    if _HAS_FSTRING_TOKENS:
        assert cleared == [], "f-string 축을 뺐는데도 여전히 잡힘 — 축 제거가 무효"
    else:
        # Python 3.11 — f-string 전체가 단일 ``STRING`` 이라 뺄 축이 없다. 같은 1건 그대로.
        assert cleared == red


def test_marker_label_residuals_are_zero_on_real_canonical_tree():
    """circled 마커·design-label 잔재 0 을 실 canonical 트리에서 값으로 단언(합성 픽스처 아님)."""
    residuals = marker_label_residuals(REPO)
    assert not residuals, (
        f"count={len(residuals)}; "
        + ", ".join(f"{path}:{line}:{kind}:{match}" for path, line, kind, match in residuals[:20])
    )


def test_temp_output_guard_scopes_concurrent_process_output_to_session_directory(
    tmp_path,
):
    """외부 프로세스의 동형 이름은 무시하고 세션 디렉터리의 산출물만 관찰한다."""
    suite_conftest = sys.modules["conftest"]
    session_dir = tmp_path / "session-temp"
    external_dir = tmp_path / "external-temp"
    session_dir.mkdir()
    external_dir.mkdir()
    before = suite_conftest._snapshot_project_temp_outputs(session_dir)
    writer = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1], sys.argv[2]).write_text('raw', encoding='utf-8')"
    )

    subprocess.run(
        [
            sys.executable,
            "-c",
            writer,
            str(external_dir),
            "pm_delegate_codex_999999_probe.txt",
        ],
        check=True,
    )

    assert suite_conftest._snapshot_project_temp_outputs(session_dir) == before
    assert len(suite_conftest._snapshot_project_temp_outputs(external_dir)) == 1

    subprocess.run(
        [
            sys.executable,
            "-c",
            writer,
            str(session_dir),
            "external_review_codex_probe.txt",
        ],
        check=True,
    )
    after_owned_write = suite_conftest._snapshot_project_temp_outputs(session_dir)
    assert len(after_owned_write) == 1
    assert after_owned_write != before


def test_repo_raw_output_guard_observes_default_destination_and_ledger(tmp_path):
    """repo 기본 raw 목적지 축이 실제로 관찰한다 — tempdir 격리만으로는 못 보는 경로.

    위임·외부리뷰 raw 의 **기본** 목적지는 tempdir 가 아니라 해소된 repo 의
    `.project_manager/.local/` 하위로 옮겼다. 이 축이 없으면 기본 경로를 밟는 신규 테스트가
    실 작업 트리를 오염시켜도 세션 가드가 조용히 통과한다(vacuous). 빈 트리에서 스냅샷이
    비어 있는지까지 단언해 "아무것도 안 보고 통과"를 배제한다.
    """
    suite_conftest = sys.modules["conftest"]
    local = tmp_path / ".project_manager" / ".local"
    (local / "delegate").mkdir(parents=True)
    (local / "review").mkdir(parents=True)

    empty = suite_conftest._snapshot_repo_raw_outputs(tmp_path)
    assert empty == {}, "빈 목적지에서 비-빈 스냅샷이 나오면 판정 입력이 오염됐다"

    raw = local / "delegate" / "pm_delegate_codex_1_probe.txt"
    raw.write_text("raw", encoding="utf-8")
    after_raw = suite_conftest._snapshot_repo_raw_outputs(tmp_path)
    assert set(after_raw) == {raw}

    review = local / "review" / "external_review_codex_probe.txt"
    review.write_text("raw", encoding="utf-8")
    ledger = local / "raw_outputs.json"
    ledger.write_text("{}", encoding="utf-8")
    after_all = suite_conftest._snapshot_repo_raw_outputs(tmp_path)
    assert set(after_all) == {raw, review, ledger}

    # 동일 이름 덮어쓰기도 델타로 잡아야 한다(경로 집합 비교만으로는 놓친다).
    ledger.write_text('{"records": []}', encoding="utf-8")
    assert suite_conftest._snapshot_repo_raw_outputs(tmp_path)[ledger] != after_all[ledger]

    # `.local` 자체가 없는 트리(신규 clone)는 빈 스냅샷이며 예외를 내지 않는다.
    assert suite_conftest._snapshot_repo_raw_outputs(tmp_path / "absent") == {}


def _dump_hard_report(report: dict[str, object]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    """검토된 hard 대장을 현재 트리에서 다시 덤프한다.

    기본은 비파괴 검사(드리프트를 델타로 출력하고 rc=1)이고, ``--regenerate`` 일 때만
    파일을 다시 쓴다. 재생성 경로를 pytest 밖에 두어 회귀가 대장을 자기치유하지 못하게 한다.
    """
    if hasattr(sys.stdout, "reconfigure"):
        # 델타 샘플에 비-ASCII match(§ 절 표기·한글 문맥)가 섞일 수 있다.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="드리프트가 있으면 대장 파일을 다시 쓴다 (기본은 검사만)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=HARD_REPORT,
        help=f"덤프 대상 경로 (기본: {HARD_REPORT.relative_to(REPO).as_posix()})",
    )
    args = parser.parse_args(argv)

    hard, _ = _collect()
    report = _hard_report(hard)
    payload = _dump_hard_report(report)
    entries = report["entries"]
    assert isinstance(entries, list)
    summary = f"count={report['count']} entries={len(entries)}"
    current = (
        args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
    )
    if payload == current:
        print(f"unchanged {args.output} — {summary}")
        return 0

    expected: Counter[HardKey] = (
        _hard_baseline_counter(json.loads(current)) if current else Counter()
    )
    unexpected, stale = _inventory_delta(expected, _hard_counter(hard))
    print(_describe_delta(unexpected, stale, _format_hard_key))
    if not args.regenerate:
        print(f"drift {args.output} — {summary}; rerun with --regenerate")
        return 1
    args.output.write_text(payload, encoding="utf-8")
    print(f"regenerated {args.output} — {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
