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
import re
import threading
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


# ── cmd_checkpoint ──────────────────────────────────────────────────────────

def test_build_checkpoint_entry_explicit_date_is_deterministic():
    """날짜 결정성은 CLI args 비공개 seam 없이 순수 빌더 입력으로 검증한다."""
    mod = _load_module()
    assert mod.build_checkpoint_entry(
        "orch-dev-T0547", "compaction", date="2026-08-06"
    ).startswith(
        "## [2026-08-06] checkpoint | (task:orch-dev-T0547) — compaction\n"
    )


def test_cmd_checkpoint_appends_explicit_compaction_trigger(tmp_path, monkeypatch):
    """명시 trigger와 task 태그를 가진 골격을 기존 current.md 뒤에 append한다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text(_HEADER + _ENTRY_A, encoding="utf-8")

    before_count = len(mod.split_entries(current.read_text(encoding="utf-8"))[1])
    rc = mod.cmd_checkpoint(
        SimpleNamespace(task="orch-dev-T0547", trigger="compaction")
    )

    assert rc == 0
    text = current.read_text(encoding="utf-8")
    assert _ENTRY_A in text
    assert "checkpoint | (task:orch-dev-T0547) — compaction" in text
    assert "- 구간: <직전 박제 경계 이후>" in text
    assert "- 서사: <PM 손>" in text
    assert len(mod.split_entries(text)[1]) == before_count + 1


def test_cmd_checkpoint_appends_entry_boundary_without_trailing_newline(tmp_path, monkeypatch):
    """EOF LF가 없어도 checkpoint 헤더는 앞 entry 본문과 붙지 않고 새 entry로 분리된다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text((_HEADER + _ENTRY_A).rstrip("\n"), encoding="utf-8")
    before_count = len(mod.split_entries(current.read_text(encoding="utf-8"))[1])

    rc = mod.cmd_checkpoint(
        SimpleNamespace(task="orch-dev-T0547", trigger="manual")
    )

    text = current.read_text(encoding="utf-8")
    assert rc == 0
    assert "본문 A\n## [" in text
    assert len(mod.split_entries(text)[1]) == before_count + 1


def test_cmd_checkpoint_missing_current(tmp_path, monkeypatch, capsys):
    """current.md 부재 → rc 2 + stderr 안내, 파일은 만들지 않는다."""
    mod = _load_module()
    _redirect_paths(mod, monkeypatch, tmp_path)

    rc = mod.cmd_checkpoint(SimpleNamespace(task="orch-dev-T0547", trigger="manual"))

    assert rc == 2
    assert "current.md 없음" in capsys.readouterr().err
    assert not mod.CURRENT_FILE.exists()


@pytest.mark.parametrize("task", ["foo)bar", "foo\nbar"])
def test_cmd_checkpoint_rejects_invalid_task_before_write(tmp_path, monkeypatch, capsys, task):
    """공유 validator가 태그 종료·개행 주입 task를 rc 1로 거부하고 log를 보존한다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    original = _HEADER + _ENTRY_A
    current.write_text(original, encoding="utf-8")

    rc = mod.cmd_checkpoint(SimpleNamespace(task=task, trigger="manual"))

    assert rc == 1
    assert "부적합 task 명" in capsys.readouterr().err
    assert current.read_text(encoding="utf-8") == original


def test_append_atomic_uses_one_o_append_write(tmp_path, monkeypatch):
    """공유 log append seam은 RMW 없이 O_APPEND 단일 write를 사용한다."""
    mod = _load_module()
    calls = []
    monkeypatch.setattr(
        mod.os,
        "open",
        lambda path, flags, mode=0: calls.append(("open", path, flags, mode)) or 41,
    )
    monkeypatch.setattr(mod.os, "write", lambda fd, payload: calls.append(("write", fd, payload)))
    monkeypatch.setattr(mod.os, "close", lambda fd: calls.append(("close", fd)))

    target = tmp_path / "current.md"
    mod._append_atomic(target, "\nentry")

    assert calls[0][0:2] == ("open", str(target))
    assert calls[0][2] & mod.os.O_APPEND
    assert calls[0][2] & mod.os.O_CREAT
    assert calls[1:] == [("write", 41, b"\nentry"), ("close", 41)]


def test_log_write_lock_uses_single_project_local_lock(tmp_path, monkeypatch):
    """운영 log 경로의 모든 writer는 `.project_manager/.local/log.lock` 하나를 쓴다.

    플랫폼 분기는 공용 `file_lock` seam이 소유하므로(T-0561) 획득/해제 관측도 그 seam에서
    한다 — pm_log는 경로 규약만 정한다.
    """
    mod = _load_module()
    lock_mod = mod._load_file_lock()
    current = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    calls = []
    monkeypatch.setattr(
        lock_mod.os,
        "open",
        lambda path, flags, mode=0: calls.append(("open", Path(path), flags, mode)) or 43,
    )
    monkeypatch.setattr(
        lock_mod, "acquire_exclusive", lambda fd: calls.append(("acquire", fd))
    )
    monkeypatch.setattr(
        lock_mod, "release_exclusive", lambda fd: calls.append(("release", fd))
    )
    monkeypatch.setattr(lock_mod.os, "close", lambda fd: calls.append(("close", fd)))

    with mod.log_write_lock(current):
        calls.append(("critical",))

    assert calls[0][0:2] == (
        "open",
        tmp_path / ".project_manager" / ".local" / "log.lock",
    )
    assert calls[1:] == [
        ("acquire", 43),
        ("critical",),
        ("release", 43),
        ("close", 43),
    ]


def test_all_current_log_writers_use_shared_seam_grep_guard():
    """complete·handoff·decide·archive/checkpoint에 직접 current RMW가 재발하지 않는다."""
    consumer_sources = {
        name: (TOOLS / name).read_text(encoding="utf-8")
        for name in ("ticket_finish.py", "pm_handoff.py", "pm_adr.py")
    }
    for name, source in consumer_sources.items():
        assert "_load_pm_log(" in source and ".append_log(" in source, name
        assert not re.search(r"self\._log_file\.write_text\(", source), name

    pm_log_source = PM_LOG_PY.read_text(encoding="utf-8")
    archive_source = pm_log_source.split("def cmd_archive", 1)[1].split(
        "def cmd_migrate", 1
    )[0]
    checkpoint_source = pm_log_source.split("def cmd_checkpoint", 1)[1].split(
        "# ── 유틸", 1
    )[0]
    assert "with log_write_lock(CURRENT_FILE):" in archive_source
    assert "_replace_atomic(CURRENT_FILE, new_current)" in archive_source
    assert "CURRENT_FILE.write_text(" not in archive_source
    assert "append_log(CURRENT_FILE," in checkpoint_source
    assert "CURRENT_FILE.write_text(" not in checkpoint_source


def test_checkpoint_parser_default_manual_appends_entry(tmp_path, monkeypatch):
    """--trigger 생략 시 기본값 manual로 실제 checkpoint entry를 append한다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text(_HEADER, encoding="utf-8")

    args = mod.build_parser().parse_args(["checkpoint", "--task", "orch-dev-T0547"])
    assert args.task == "orch-dev-T0547"
    assert args.trigger == "manual"
    assert args.fn is mod.cmd_checkpoint
    assert args.fn(args) == 0
    assert "checkpoint | (task:orch-dev-T0547) — manual" in current.read_text(encoding="utf-8")


def test_registered_repos_non_utf8_areas_failure_is_fail_soft(monkeypatch):
    """board areas 파싱 실패는 None으로 강등해 기본 task 구문 검증을 유지한다."""
    mod = _load_module()

    class BrokenBoard:
        @staticmethod
        def registered_repos():
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(mod, "_load_module_from_path", lambda *args, **kwargs: BrokenBoard)
    assert mod._registered_repos() is None


def test_registered_repos_engine_rev_skew_is_fail_loud(monkeypatch):
    """일반 오류와 달리 ENGINE_REV skew는 배포 손상이므로 그대로 전파한다."""
    mod = _load_module()
    skew = RuntimeError("skew")
    skew._engine_rev_skew = True

    def raise_skew(*args, **kwargs):
        raise skew

    monkeypatch.setattr(mod, "_load_module_from_path", raise_skew)
    with pytest.raises(RuntimeError, match="skew"):
        mod._registered_repos()


def test_main_checkpoint_worktree_misanchor_fails_before_write(
    tmp_path, monkeypatch, capsys
):
    """checkpoint는 worktree 앵커에서 PM 홈 안내 후 append 전에 중단한다."""
    mod = _load_module()
    _redirect_paths(mod, monkeypatch, tmp_path / "work" / "product_1")
    pm_home = tmp_path / "pm-home"
    monkeypatch.setattr(mod, "_pm_home_misanchor", lambda: pm_home)

    rc = mod.main(["checkpoint", "--task", "orch-dev-T0547"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "PM 홈에서 실행하세요" in err
    assert str(pm_home) in err
    assert not mod.CURRENT_FILE.exists()


def test_main_tail_remains_ungated(tmp_path, monkeypatch, capsys):
    """read-only tail은 worktree 앵커 판정을 호출하지 않고 현행 동작을 유지한다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "current.md").write_text(_HEADER + _ENTRY_A, encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "_guard_worktree_misanchor",
        lambda: pytest.fail("tail must not invoke the write-only guard"),
    )

    assert mod.main(["tail"]) == 0
    assert "첫 작업" in capsys.readouterr().out


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


def test_archive_and_append_interleave_preserves_both_writers(tmp_path, monkeypatch):
    """archive 교체 직전 append가 경합해도 lock 순서대로 실행돼 신규 entry를 잃지 않는다."""
    mod = _load_module()
    log_dir, _archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text(_HEADER + _ENTRY_A + _ENTRY_C, encoding="utf-8")
    appended = "## [2026-08-06] ticket | 동시 append\n본문 D\n"

    replace_entered = threading.Event()
    allow_replace = threading.Event()
    append_done = threading.Event()
    errors = []
    original_replace = mod._replace_atomic

    def paused_replace(path, text):
        replace_entered.set()
        if not allow_replace.wait(2):
            raise AssertionError("archive replace release timeout")
        original_replace(path, text)

    def run_archive():
        try:
            assert mod.cmd_archive(
                SimpleNamespace(before="2026-06-13", dry_run=False)
            ) == 0
        except BaseException as exc:  # thread 예외를 주 테스트로 전달.
            errors.append(exc)

    def run_append():
        try:
            mod.append_log(current, "\n" + appended)
        except BaseException as exc:  # thread 예외를 주 테스트로 전달.
            errors.append(exc)
        finally:
            append_done.set()

    monkeypatch.setattr(mod, "_replace_atomic", paused_replace)
    archive_thread = threading.Thread(target=run_archive)
    append_thread = threading.Thread(target=run_append)
    archive_thread.start()
    try:
        assert replace_entered.wait(2), "archive did not reach replace seam"
        append_thread.start()
        # archive가 lock을 가진 동안 append는 완료할 수 없다. 이 순서가 과거 lost-update
        # interleave(read archive → append → stale replace)를 닫는다.
        assert not append_done.wait(0.1)
    finally:
        allow_replace.set()
    archive_thread.join(2)
    append_thread.join(2)

    assert not archive_thread.is_alive() and not append_thread.is_alive()
    assert errors == []
    remaining = current.read_text(encoding="utf-8")
    assert _ENTRY_C in remaining
    assert appended in remaining
    assert _ENTRY_A not in remaining


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


# ── cmd_archive --keep-last (개수 기반 슬라이스·T-0244) ───────────────────────

def test_cmd_archive_keep_last_seals_old_keeps_recent(tmp_path, monkeypatch, capsys):
    """--keep-last N: 최근 N entry(tail)만 유지, 나머지 오래된 쪽을 연번 봉인."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text(_HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C, encoding="utf-8")

    # 최근 1개(ENTRY_C)만 유지 → ENTRY_A·ENTRY_B 봉인.
    rc = mod.cmd_archive(SimpleNamespace(before=None, keep_last=1, dry_run=False))
    assert rc == 0

    # 슬라이스: idx 1(빈 dir·legacy 예약) + 봉인 첫/마지막 날짜 범위명.
    slice_path = archive_dir / "0001-2026-06-10_to_2026-06-12.md"
    assert slice_path.exists()
    sealed = slice_path.read_text(encoding="utf-8")
    assert "본문 A" in sealed and "본문 B" in sealed
    assert "본문 C" not in sealed
    # 봉인 헤더 유래 줄이 --keep-last 모드를 반영.
    assert "--keep-last 1" in sealed
    assert "--before" not in sealed
    assert "수정 금지" in sealed

    # current.md: preamble 보존 + 최근 N(ENTRY_C)만 잔여.
    remaining = current.read_text(encoding="utf-8")
    assert remaining == _HEADER + _ENTRY_C
    assert "본문 A" not in remaining and "본문 B" not in remaining


def test_cmd_archive_keep_last_noop_when_n_ge_len(tmp_path, monkeypatch, capsys):
    """N ≥ entry 수면 봉인할 게 없다 — no-op (archive 파일 미생성·current.md 무변)."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    original = _HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C  # entry 3개.
    current.write_text(original, encoding="utf-8")

    # N == len(entries): no-op.
    rc = mod.cmd_archive(SimpleNamespace(before=None, keep_last=3, dry_run=False))
    assert rc == 0
    assert "옮길 entry 없음" in capsys.readouterr().out
    assert not archive_dir.exists() or not list(archive_dir.glob("*.md"))
    assert current.read_text(encoding="utf-8") == original

    # N > len(entries): 역시 no-op.
    rc = mod.cmd_archive(SimpleNamespace(before=None, keep_last=99, dry_run=False))
    assert rc == 0
    assert "옮길 entry 없음" in capsys.readouterr().out
    assert not archive_dir.exists() or not list(archive_dir.glob("*.md"))
    assert current.read_text(encoding="utf-8") == original


def test_cmd_archive_requires_exactly_one_mode(tmp_path, monkeypatch, capsys):
    """--before/--keep-last 둘 다 또는 둘 다 없음 → rc 1 (정확히 하나 필수)."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "current.md").write_text(_HEADER + _ENTRY_A + _ENTRY_C, encoding="utf-8")

    # 둘 다 지정 → rc 1.
    rc = mod.cmd_archive(
        SimpleNamespace(before="2026-06-13", keep_last=2, dry_run=False))
    assert rc == 1
    assert "정확히 하나" in capsys.readouterr().err

    # 둘 다 없음 → rc 1.
    rc = mod.cmd_archive(SimpleNamespace(before=None, keep_last=None, dry_run=False))
    assert rc == 1
    assert "정확히 하나" in capsys.readouterr().err


def test_cmd_archive_keep_last_lossless_preserves_all_entries(tmp_path, monkeypatch):
    """무손실 불변식: 봉인 entry + 잔여 entry = 원본 entry (순서·본문 보존)."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text(_HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C, encoding="utf-8")

    rc = mod.cmd_archive(SimpleNamespace(before=None, keep_last=2, dry_run=False))
    assert rc == 0

    slice_path = archive_dir / "0001-2026-06-10_to_2026-06-10.md"
    sealed = slice_path.read_text(encoding="utf-8")
    remaining = current.read_text(encoding="utf-8")

    # 봉인본에서 entry 만 추출 + 잔여 current 의 entry = 원본 entry (순서 보존).
    _pre_sealed, sealed_entries = mod.split_entries(sealed)
    remaining_preamble, remaining_entries = mod.split_entries(remaining)
    recombined = [e for _d, e in sealed_entries] + [e for _d, e in remaining_entries]
    assert recombined == [_ENTRY_A, _ENTRY_B, _ENTRY_C]
    # preamble 은 current 에만 남는다 (봉인본엔 봉인 헤더만).
    assert remaining_preamble == _HEADER


def test_cmd_archive_keep_last_index_continues_after_existing(tmp_path, monkeypatch):
    """슬라이스 연번은 기존 archive 뒤로 이어진다 (모드 무관·max+1 동일 채널)."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    (archive_dir / "0000-legacy.md").touch()
    (archive_dir / "0001-old.md").touch()
    (log_dir / "current.md").write_text(_HEADER + _ENTRY_A + _ENTRY_C, encoding="utf-8")

    rc = mod.cmd_archive(SimpleNamespace(before=None, keep_last=1, dry_run=False))
    assert rc == 0
    # 기존 max(0001) 뒤 → 0002.
    assert (archive_dir / "0002-2026-06-10_to_2026-06-10.md").exists()


def test_cmd_archive_keep_last_dry_run_no_write(tmp_path, monkeypatch, capsys):
    """--keep-last --dry-run: 봉인 계획만 출력, 파일 무변 (멱등·비편집)."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    original = _HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C
    current.write_text(original, encoding="utf-8")

    rc = mod.cmd_archive(SimpleNamespace(before=None, keep_last=1, dry_run=True))
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert not archive_dir.exists()
    assert current.read_text(encoding="utf-8") == original


# ── build_parser: archive 모드 상호배타·양의 int (T-0244) ─────────────────────

def test_parser_archive_keep_last_and_before_mutually_exclusive():
    """argparse mutex 그룹: --before 와 --keep-last 동시 지정은 CLI 에서 거부(SystemExit)."""
    mod = _load_module()
    parser = mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["archive", "--before", "2026-06-13", "--keep-last", "2"])


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5"])
def test_parser_archive_keep_last_rejects_non_positive_int(bad):
    """--keep-last 는 양의 정수만 — 0·음수·비정수는 argparse 거부."""
    mod = _load_module()
    parser = mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["archive", "--keep-last", bad])


def test_parser_archive_each_mode_parses_alone():
    """각 모드 단독은 정상 파싱 — 반대 모드는 None (기본)."""
    mod = _load_module()
    parser = mod.build_parser()

    args = parser.parse_args(["archive", "--keep-last", "3"])
    assert args.keep_last == 3
    assert args.before is None

    args = parser.parse_args(["archive", "--before", "2026-06-13"])
    assert args.before == "2026-06-13"
    assert args.keep_last is None


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
