"""pm_log.py 직접 단위테스트 (T-0026).

pm_log.py 는 227줄·직접 테스트 0 이었다. log/current.md 의 entry 분할·archive 봉인·migrate
를 직접 검증한다.

  - 순수 헬퍼(`split_entries`·`next_archive_index`)는 입력으로 직접 호출.
  - 파괴적 cmd(`cmd_archive`·`cmd_migrate`)와 `cmd_tail` 은 모듈-레벨 경로 상수
    (CURRENT_FILE·ARCHIVE_DIR·LEGACY_LOG·LOG_DIR)를 tmp_path 로 monkeypatch 해
    구동한다 — **실 .project_manager/wiki/log/ 미접촉**. args 는 SimpleNamespace 주입.

도구는 패키지가 아니므로 importlib 동적 로드 (test_pm_bootstrap_tz 의 _load_module 관용구).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_LOG_PY = TOOLS / "pm_log.py"


def _load_module(name: str = "pm_log"):
    """pm_log 를 경로 로드한다 (도구는 패키지가 아니므로 importlib)."""
    spec = importlib.util.spec_from_file_location(name, PM_LOG_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _redirect_paths(mod, monkeypatch, root: Path):
    """모듈-레벨 경로 상수를 tmp 루트로 갈아끼운다 (실 log/ 보호)."""
    log_dir = root / "log"
    archive_dir = log_dir / "archive"
    monkeypatch.setattr(mod, "REPO", root)
    monkeypatch.setattr(mod, "WIKI_DIR", root)
    monkeypatch.setattr(mod, "LOG_DIR", log_dir)
    monkeypatch.setattr(mod, "CURRENT_FILE", log_dir / "current.md")
    monkeypatch.setattr(mod, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(mod, "LEGACY_LOG", root / "log.md")
    return log_dir, archive_dir


# 공통 fixture 본문 — preamble + 3 entry (날짜 오름차순).
_HEADER = "# Project Log\n\n> append-only 설명.\n\n"
_ENTRY_A = "## [2026-06-10] ticket | T-0001 첫 작업\n본문 A\n\n"
_ENTRY_B = "## [2026-06-12] handoff | PM 인계 — 한글\n본문 B\n\n"
_ENTRY_C = "## [2026-06-14] lint | board lint clean\n본문 C\n"


# ── split_entries (순수) ─────────────────────────────────────────────────────

def test_split_entries_zero():
    """entry 가 없으면 (전체 텍스트, []) — preamble 에 전체가 남는다."""
    mod = _load_module()
    text = "# Project Log\n\n> entry 없음\n"
    preamble, entries = mod.split_entries(text)
    assert preamble == text
    assert entries == []


def test_split_entries_one():
    mod = _load_module()
    text = _HEADER + _ENTRY_A
    preamble, entries = mod.split_entries(text)
    assert preamble == _HEADER
    assert len(entries) == 1
    assert entries[0][0] == "2026-06-10"
    assert entries[0][1] == _ENTRY_A


def test_split_entries_many_preserves_preamble_and_order():
    """N entry: preamble 보존·날짜 파싱·경계가 다음 `## [..]` 직전까지."""
    mod = _load_module()
    text = _HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C
    preamble, entries = mod.split_entries(text)
    assert preamble == _HEADER
    assert [d for d, _ in entries] == ["2026-06-10", "2026-06-12", "2026-06-14"]
    # 각 entry_text 는 자기 `## [..]` 줄부터 다음 entry 직전까지 — 합치면 원문 복원.
    assert preamble + "".join(e for _, e in entries) == text
    # 마지막 entry 는 파일 끝까지.
    assert entries[-1][1] == _ENTRY_C


def test_split_entries_no_preamble():
    """첫 줄이 곧 entry 면 preamble 은 빈 문자열."""
    mod = _load_module()
    text = _ENTRY_A + _ENTRY_B
    preamble, entries = mod.split_entries(text)
    assert preamble == ""
    assert len(entries) == 2


# ── next_archive_index (순수) ────────────────────────────────────────────────

def test_next_archive_index_empty_dir_reserves_legacy(tmp_path):
    """빈/부재 디렉토리 → 1 (0000 은 legacy 예약이라 최소 1)."""
    mod = _load_module()
    assert mod.next_archive_index(tmp_path / "archive") == 1  # 존재 안 함
    (tmp_path / "archive").mkdir()
    assert mod.next_archive_index(tmp_path / "archive") == 1  # 존재하지만 비어 있음


def test_next_archive_index_after_existing(tmp_path):
    mod = _load_module()
    arch = tmp_path / "archive"
    arch.mkdir()
    (arch / "0000-legacy.md").touch()
    (arch / "0001-2026-06-01_to_2026-06-05.md").touch()
    (arch / "0002-2026-06-06_to_2026-06-10.md").touch()
    assert mod.next_archive_index(arch) == 3


def test_next_archive_index_handles_gaps(tmp_path):
    """gap 이 있어도 max+1 (연속성 아님)."""
    mod = _load_module()
    arch = tmp_path / "archive"
    arch.mkdir()
    (arch / "0001-a.md").touch()
    (arch / "0005-b.md").touch()
    # 4자리 NNNN- 패턴 아닌 파일은 무시.
    (arch / "current.md").touch()
    (arch / "README.md").touch()
    assert mod.next_archive_index(arch) == 6


# ── cmd_tail ─────────────────────────────────────────────────────────────────

def test_cmd_tail_prints_last_entry(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "current.md").write_text(_HEADER + _ENTRY_A + _ENTRY_C, encoding="utf-8")

    rc = mod.cmd_tail(SimpleNamespace())
    assert rc == 0
    out = capsys.readouterr().out
    # 마지막 entry 만 (rstrip). 이전 entry·preamble 은 안 나온다.
    assert "board lint clean" in out
    assert "첫 작업" not in out
    assert out.strip() == _ENTRY_C.rstrip()


def test_cmd_tail_no_entries(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "current.md").write_text("# Project Log\n\n> entry 없음\n", encoding="utf-8")

    rc = mod.cmd_tail(SimpleNamespace())
    assert rc == 0
    assert "(entry 없음)" in capsys.readouterr().out


def test_cmd_tail_missing_current(tmp_path, monkeypatch, capsys):
    """current.md 부재 → rc 2 + stderr 안내 (migrate 먼저)."""
    mod = _load_module()
    _redirect_paths(mod, monkeypatch, tmp_path)  # 파일 생성 안 함
    rc = mod.cmd_tail(SimpleNamespace())
    assert rc == 2
    assert "current.md 없음" in capsys.readouterr().err


# ── cmd_archive (파괴적 — tmp_path 에서만) ────────────────────────────────────

def test_cmd_archive_seals_old_keeps_recent(tmp_path, monkeypatch, capsys):
    """--before cutoff 미만 entry 를 archive 파일로 봉인하고 current.md 엔 잔여만 남긴다."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text(_HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C, encoding="utf-8")

    # 2026-06-13 미만 = ENTRY_A(06-10)·ENTRY_B(06-12) 봉인, ENTRY_C(06-14) 유지.
    rc = mod.cmd_archive(SimpleNamespace(before="2026-06-13", dry_run=False))
    assert rc == 0

    # archive 파일 생성: idx 1(빈 dir·legacy 예약) + 첫/마지막 날짜 범위명.
    slice_path = archive_dir / "0001-2026-06-10_to_2026-06-12.md"
    assert slice_path.exists()
    sealed = slice_path.read_text(encoding="utf-8")
    assert "본문 A" in sealed and "본문 B" in sealed
    assert "본문 C" not in sealed
    assert "Log archive 0001" in sealed
    assert "수정 금지" in sealed

    # current.md: preamble 보존 + ENTRY_C 만 잔여, 봉인된 건 제거.
    remaining = current.read_text(encoding="utf-8")
    assert remaining == _HEADER + _ENTRY_C
    assert "본문 A" not in remaining and "본문 B" not in remaining


def test_cmd_archive_cutoff_boundary_is_strict_keeps_on_date(tmp_path, monkeypatch, capsys):
    """cutoff 경계 못박기: "DATE 미만"(strict <) 의미 — cutoff 와 *정확히 같은* 날짜
    entry 는 current.md 에 유지(봉인 안 함), *엄격히 이전* entry 만 봉인.

    pm_log.py:114-115 가 `< cutoff`(봉인) / `>= cutoff`(유지) 라서 경계 entry 는 keep
    쪽이다. 이걸 못박지 않으면 비교를 `<= / >`(inclusive)로 뒤집어도 테스트가 통과한다.
    """
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    # ENTRY_A(06-10) = cutoff 미만, BOUNDARY(06-13) = cutoff 와 동일.
    entry_boundary = "## [2026-06-13] update | cutoff 와 같은 날\n본문 경계\n"
    current.write_text(_HEADER + _ENTRY_A + entry_boundary, encoding="utf-8")

    # --before 2026-06-13: 06-10 만 봉인, 06-13(경계)은 유지돼야 한다.
    rc = mod.cmd_archive(SimpleNamespace(before="2026-06-13", dry_run=False))
    assert rc == 0

    # 봉인 슬라이스: 엄격히 이전(06-10)만 — 경계 날짜는 범위명·본문에 없음.
    slice_path = archive_dir / "0001-2026-06-10_to_2026-06-10.md"
    assert slice_path.exists()
    sealed = slice_path.read_text(encoding="utf-8")
    assert "본문 A" in sealed
    assert "본문 경계" not in sealed
    # 경계 entry 의 `## [..]` 앵커는 봉인본에 없다 (cutoff 문자열은 헤더에 echo 되므로
    # 날짜 substring 이 아니라 entry 앵커로 확인).
    assert "## [2026-06-13]" not in sealed

    # current.md: 경계(cutoff 와 동일) entry 만 유지, 엄격히 이전 entry 는 제거.
    remaining = current.read_text(encoding="utf-8")
    assert remaining == _HEADER + entry_boundary
    assert "본문 경계" in remaining
    assert "본문 A" not in remaining


def test_cmd_archive_noop_when_nothing_old(tmp_path, monkeypatch, capsys):
    """cutoff 미만 entry 0개면 no-op — archive 파일 미생성·current.md 무변."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    original = _HEADER + _ENTRY_B + _ENTRY_C
    current.write_text(original, encoding="utf-8")

    rc = mod.cmd_archive(SimpleNamespace(before="2026-06-01", dry_run=False))
    assert rc == 0
    assert "옮길 entry 없음" in capsys.readouterr().out
    assert not archive_dir.exists() or not list(archive_dir.glob("*.md"))
    assert current.read_text(encoding="utf-8") == original


def test_cmd_archive_dry_run_no_write(tmp_path, monkeypatch, capsys):
    """--dry-run: 봉인 계획만 출력하고 파일은 안 건드린다."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    original = _HEADER + _ENTRY_A + _ENTRY_C
    current.write_text(original, encoding="utf-8")

    rc = mod.cmd_archive(SimpleNamespace(before="2026-06-13", dry_run=True))
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out
    # 아무 것도 쓰지 않음.
    assert not archive_dir.exists()
    assert current.read_text(encoding="utf-8") == original


def test_cmd_archive_bad_date(tmp_path, monkeypatch, capsys):
    """--before 형식 오류 → rc 1 (current.md 존재 여부 판정 전 검증)."""
    mod = _load_module()
    _redirect_paths(mod, monkeypatch, tmp_path)
    rc = mod.cmd_archive(SimpleNamespace(before="2026/06/13", dry_run=False))
    assert rc == 1
    assert "날짜 형식 오류" in capsys.readouterr().err


def test_cmd_archive_missing_current(tmp_path, monkeypatch, capsys):
    """current.md 부재 → rc 2 (날짜는 valid)."""
    mod = _load_module()
    _redirect_paths(mod, monkeypatch, tmp_path)
    rc = mod.cmd_archive(SimpleNamespace(before="2026-06-13", dry_run=False))
    assert rc == 2
    assert "current.md 없음" in capsys.readouterr().err


def test_cmd_archive_index_increments_with_existing(tmp_path, monkeypatch):
    """기존 archive 슬라이스가 있으면 다음 인덱스로 봉인 (max+1)."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    (archive_dir / "0000-legacy.md").touch()
    (archive_dir / "0001-old.md").touch()
    (log_dir / "current.md").write_text(_HEADER + _ENTRY_A + _ENTRY_C, encoding="utf-8")

    rc = mod.cmd_archive(SimpleNamespace(before="2026-06-13", dry_run=False))
    assert rc == 0
    assert (archive_dir / "0002-2026-06-10_to_2026-06-10.md").exists()


# ── cmd_migrate (파괴적 — tmp_path 에서만) ────────────────────────────────────

def test_cmd_migrate_seals_legacy_and_creates_current(tmp_path, monkeypatch, capsys):
    """기존 log.md → archive/0000-legacy.md 봉인 + current.md(표준 헤더) 생성."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    legacy = tmp_path / "log.md"
    legacy_body = "# Project Log\n\n## [2026-05-01] create | 옛 entry\n옛 본문\n"
    legacy.write_text(legacy_body, encoding="utf-8")

    rc = mod.cmd_migrate(SimpleNamespace(dry_run=False))
    assert rc == 0

    # legacy 원본은 봉인 후 삭제.
    assert not legacy.exists()
    sealed = (archive_dir / "0000-legacy.md").read_text(encoding="utf-8")
    assert "Log archive 0000" in sealed
    assert "옛 본문" in sealed  # 원문 그대로 봉인.
    assert "수정 금지" in sealed

    # current.md 는 표준 헤더로 새로 생성 (legacy 내용 미포함).
    current = (log_dir / "current.md").read_text(encoding="utf-8")
    assert current == mod.CURRENT_HEADER
    assert "옛 본문" not in current
    # archive/.gitkeep 도 생성.
    assert (archive_dir / ".gitkeep").exists()


def test_cmd_migrate_no_legacy_creates_empty_current(tmp_path, monkeypatch, capsys):
    """기존 log.md 가 없으면 빈(헤더만) current.md 만 생성, legacy 봉인 없음."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)

    rc = mod.cmd_migrate(SimpleNamespace(dry_run=False))
    assert rc == 0
    assert (log_dir / "current.md").read_text(encoding="utf-8") == mod.CURRENT_HEADER
    assert not (archive_dir / "0000-legacy.md").exists()
    assert "기존 log.md 없음" in capsys.readouterr().out


def test_cmd_migrate_idempotent_when_current_exists(tmp_path, monkeypatch, capsys):
    """current.md 가 이미 있으면 no-op — legacy 미접촉."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text("# 기존 current\n", encoding="utf-8")
    legacy = tmp_path / "log.md"
    legacy.write_text("legacy 본문\n", encoding="utf-8")

    rc = mod.cmd_migrate(SimpleNamespace(dry_run=False))
    assert rc == 0
    assert "이미 마이그레이션됨" in capsys.readouterr().out
    # current.md 무변·legacy 보존.
    assert current.read_text(encoding="utf-8") == "# 기존 current\n"
    assert legacy.exists()


def test_cmd_migrate_dry_run_no_write(tmp_path, monkeypatch, capsys):
    """--dry-run: 계획만 출력, log.md·current.md 무변."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    legacy = tmp_path / "log.md"
    legacy.write_text("legacy 본문\n", encoding="utf-8")

    rc = mod.cmd_migrate(SimpleNamespace(dry_run=True))
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert legacy.exists()  # 삭제 안 됨.
    assert not (log_dir / "current.md").exists()
    assert not (archive_dir / "0000-legacy.md").exists()
