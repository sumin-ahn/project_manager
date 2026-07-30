"""Keep private development context out of shipped prose."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
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
