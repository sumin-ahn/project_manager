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

import contextlib
import importlib.util
import json
import re
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from _textio import normalize_newlines, write_lf

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_LOG_PY = TOOLS / "pm_log.py"
PM_HANDOFF_PY = TOOLS / "pm_handoff.py"


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


@pytest.mark.parametrize("band", ["nudge", "nudge2", "final", "precompact"])
def test_ctx_guidance_all_bands_preserve_required_and_forbid_accident_phrase(band):
    """중앙 정책 상수의 필수·금지 표현을 네 band 최종 출력 전체에 고정한다."""
    mod = _load_module(f"pm_log_ctx_policy_{band}")
    guidance = mod.build_ctx_guard_guidance(
        band,
        used_pct=92,
        remaining_pct=8,
        stop_pct=20,
    )

    for required in mod.CTX_GUARD_REQUIRED_EXPRESSIONS:
        assert required in mod.CTX_GUARD_CONTINUITY_GUIDANCE
        assert required in guidance
    for forbidden in mod.CTX_GUARD_FORBIDDEN_EXPRESSIONS:
        assert forbidden not in mod.CTX_GUARD_CONTINUITY_GUIDANCE
        assert forbidden not in guidance


def test_precompact_breadcrumb_is_continuation_not_handoff_completion_signal():
    mod = _load_module("pm_log_precompact_breadcrumb_policy")
    breadcrumb = mod._PRECOMPACT_BREADCRUMB

    assert "auto-compact 발생" in breadcrumb
    assert all(required in breadcrumb for required in mod.CTX_GUARD_REQUIRED_EXPRESSIONS)
    assert "수동 핸드오프" not in breadcrumb
    assert "미완" not in breadcrumb


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
    write_lf(log_dir / "current.md", _HEADER + _ENTRY_A + _ENTRY_C)

    rc = mod.cmd_tail(SimpleNamespace())
    assert rc == 0
    out = normalize_newlines(capsys.readouterr().out)
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


def test_checkpoint_task_header_remains_byte_compatible():
    """T-0686: task 형상의 기존 헤더는 행 바이트까지 동일하다."""
    mod = _load_module("pm_log_t0686_task_header")

    entry = mod.build_checkpoint_entry(
        "orch-dev-T0547", "compaction", date="2026-08-06",
    )

    assert entry.splitlines()[0] == (
        "## [2026-08-06] checkpoint | (task:orch-dev-T0547) — compaction"
    )


def _slot_checkpoint_fixture(mod, monkeypatch, tmp_path):
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    mod.CURRENT_FILE.write_text(_HEADER, encoding="utf-8")
    cwd = tmp_path / "work" / "project_manager_1" / "nested"
    cwd.mkdir(parents=True)
    ledger = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({
            "leases": [{
                "slot": "work/project_manager_1",
                "state": "leased",
                "session": "project_manager_1",
            }],
        }),
        encoding="utf-8",
    )
    return cwd


def test_checkpoint_slot_identity_appends_canonical_header(tmp_path, monkeypatch):
    """T-0686: cwd lease의 canonical slot은 task validator에 거부되지 않고 append된다."""
    mod = _load_module("pm_log_t0686_slot_append")
    cwd = _slot_checkpoint_fixture(mod, monkeypatch, tmp_path)

    rc = mod.cmd_checkpoint(SimpleNamespace(
        task=None, trigger="manual", cwd=str(cwd), breadcrumb=False,
    ))

    entries = mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1]
    assert rc == 0 and len(entries) == 1
    assert entries[0][1].splitlines()[0] == (
        f"## [{mod.datetime.date.today().isoformat()}] checkpoint | "
        "(project_manager_1) — manual"
    )


def test_checkpoint_slot_header_round_trips_through_handoff_collector(
    tmp_path, monkeypatch,
):
    """T-0686: pm_log 생산 slot entry를 pm_handoff가 같은 세션 기록으로 수집한다."""
    mod = _load_module("pm_log_t0686_handoff_producer")
    cwd = _slot_checkpoint_fixture(mod, monkeypatch, tmp_path)
    args = SimpleNamespace(
        task=None, trigger="manual", cwd=str(cwd), breadcrumb=False,
    )
    assert mod.cmd_checkpoint(args) == 0
    log_text = mod.CURRENT_FILE.read_text(encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "pm_handoff_t0686_consumer", PM_HANDOFF_PY,
    )
    handoff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handoff)

    produced_header = mod.split_entries(log_text)[1][0][1].splitlines()[0]
    assert handoff.collect_session_entries(log_text, None, "project_manager_1") == [
        produced_header
    ]

    diagnostic = mod.build_checkpoint_entry(
        None, "compaction", date="2026-08-15", session="project_manager_1",
        ctx_band_checked=True, ctx_band_missed=True,
        ctx_window_tokens=600_000, ctx_observed_tokens=30_000, harness="claude",
    )
    diagnostic_log = (
        tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    )
    diagnostic_log.parent.mkdir(parents=True)
    diagnostic_log.write_text(_HEADER + diagnostic, encoding="utf-8")
    mismatch = mod._latest_ctx_window_mismatch_section(
        tmp_path, None, "project_manager_1",
    )
    assert mismatch is not None and "[ctx-window-mismatch]" in mismatch


def test_checkpoint_parser_unresolved_compaction_warns_without_failing(
    tmp_path, monkeypatch, capsys,
):
    """T-0686: 훅 CLI 계약은 rc 0을 유지하며 무기록을 stderr로 관측시킨다."""
    mod = _load_module("pm_log_t0686_unresolved")
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    mod.CURRENT_FILE.write_text(_HEADER, encoding="utf-8")
    args = mod.build_parser().parse_args([
        "checkpoint", "--trigger", "compaction", "--phase", "pre",
        "--cwd", str(tmp_path),
    ])

    rc = args.fn(args)

    captured = capsys.readouterr()
    assert rc == 0 and captured.out == ""
    assert "checkpoint 정체성 미해소" in captured.err and "기록 생략" in captured.err
    assert mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1] == []


def test_checkpoint_slot_compaction_dedups_same_boundary(tmp_path, monkeypatch):
    """T-0686: slot 형상도 같은 compaction boundary를 중복 append하지 않는다."""
    mod = _load_module("pm_log_t0686_slot_dedup")
    cwd = _slot_checkpoint_fixture(mod, monkeypatch, tmp_path)
    args = SimpleNamespace(
        task=None, trigger="compaction", cwd=str(cwd),
        session_id="harness-session", boundary_id="boundary-1", phase="pre",
        breadcrumb=False,
    )

    assert mod.cmd_checkpoint(args) == 0
    assert mod.cmd_checkpoint(args) == 0

    entries = mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1]
    assert len(entries) == 1
    assert "checkpoint | (project_manager_1) — compaction" in entries[0][1]


def _freeform_task_checkpoint(mod, monkeypatch, tmp_path, *, register_task: bool):
    """`_N` 형상 session(`foo_1`) 이 slot 경로 이름과 어긋난 lease 를 실 장부 파일로 세운다.

    `register_task=True` 면 그 이름을 장부 `tasks` 컬렉션에 **등록**한다 — 축 판정의 단일 진실이
    등록 membership 이라, 등록 여부만 바꿔 두 축을 대조한다(이름 모양은 두 케이스가 동일).
    """
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    mod.CURRENT_FILE.write_text(_HEADER, encoding="utf-8")
    cwd = tmp_path / "work" / "project_manager_1" / "nested"
    cwd.mkdir(parents=True)
    ledger = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    payload = {
        "leases": [{
            "slot": "work/project_manager_1",
            "state": "leased",
            "session": "foo_1",
        }],
    }
    if register_task:
        payload["tasks"] = [{"name": "foo_1", "prefix": None, "pid": 0, "started": "t"}]
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(mod, "_registered_repos", lambda: set())

    rc = mod.cmd_checkpoint(SimpleNamespace(
        task=None, trigger="manual", cwd=str(cwd), breadcrumb=False,
    ))
    entries = mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1]
    assert rc == 0 and len(entries) == 1
    return entries[0][1]


def test_checkpoint_registered_freeform_task_name_is_not_misclassified_as_slot(
    tmp_path, monkeypatch,
):
    """T-0686 F-003: 장부에 **등록된** `_N` task 는 lease slot 이름과 달라도 task 태그를 쓴다.

    판정 근거는 이름 모양이나 slot 경로 대조가 아니라 장부 `tasks` 등록이다.
    """
    mod = _load_module("pm_log_t0686_freeform_task")
    entry = _freeform_task_checkpoint(mod, monkeypatch, tmp_path, register_task=True)
    assert "checkpoint | (task:foo_1) — manual" in entry


def test_checkpoint_unregistered_slot_key_session_is_slot_axis(tmp_path, monkeypatch):
    """등록 task 가 아닌 슬롯 키 session 은 slot 축이다 — slot 경로 이름과 어긋나도 그렇다.

    옛 규칙은 정체성을 slot 경로 basename 과 대조해(`slot.name == identity`) 이 형상을 task 로
    오분류했고, 그래서 경로에 이름이 없는 슬롯(PM 홈 자신을 가리키는 행)은 slot 축이 될 방법이
    아예 없었다. 위 등록 케이스와 **장부 tasks 등록 여부만** 다르다(대조군).
    """
    mod = _load_module("pm_log_t0686_slotkey_session")
    entry = _freeform_task_checkpoint(mod, monkeypatch, tmp_path, register_task=False)
    assert "checkpoint | (foo_1) — manual" in entry
    assert "task:foo_1" not in entry


def test_build_checkpoint_entry_renders_ctx_window_mismatch_with_observation():
    mod = _load_module()
    entry = mod.build_checkpoint_entry(
        "main",
        "compaction",
        "2026-08-11",
        ctx_band_checked=True,
        ctx_band_missed=True,
        ctx_window_tokens=1_000_000,
        ctx_observed_tokens=655_736,
        harness="claude",
    )
    assert "[ctx-window-mismatch] 설정 창이 실 압축 지점보다 큼" in entry
    assert "설정 1,000,000 tokens" in entry
    assert "PreCompact 관측 655,736 tokens" in entry
    assert "`harness.claude.ctx_window_tokens`" in entry
    assert "관측 사용량 655,736 tokens 이하" in entry


def test_ctx_window_mismatch_without_observation_keeps_qualitative_remedy():
    mod = _load_module()
    advisory = mod.build_ctx_window_mismatch_advisory(
        ctx_window_tokens=600_000,
        ctx_observed_tokens=None,
        harness="opencode",
    )
    assert "설정 600,000 tokens" in advisory
    assert "PreCompact 관측" not in advisory
    assert "관측 사용량 측정 불가" not in advisory
    assert "실 auto-compact 지점 이하" in advisory
    assert "`harness.opencode.ctx_window_tokens`" in advisory
    assert "harness.claude.ctx_window_tokens" not in advisory


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


def test_cmd_checkpoint_missing_identity_manual_fails_loud(
    tmp_path, monkeypatch, capsys,
):
    """--task 없는 manual 호출은 기록 없는 성공으로 가장하지 않는다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    original = _HEADER + _ENTRY_A
    mod.CURRENT_FILE.write_text(original, encoding="utf-8")

    rc = mod.cmd_checkpoint(SimpleNamespace(
        task=None, trigger="manual", cwd=str(tmp_path),
    ))

    captured = capsys.readouterr()
    assert rc == 1 and captured.out == ""
    assert "정체성 미해소" in captured.err and "--task NAME" in captured.err
    assert mod.CURRENT_FILE.read_text(encoding="utf-8") == original


def test_cmd_checkpoint_missing_identity_compaction_warns_and_skips(
    tmp_path, monkeypatch, capsys,
):
    """같은 identity 실패도 compaction 훅은 rc 0·진단·무기록이다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    original = _HEADER + _ENTRY_A
    mod.CURRENT_FILE.write_text(original, encoding="utf-8")

    rc = mod.cmd_checkpoint(SimpleNamespace(
        task=None, trigger="compaction", cwd=str(tmp_path), phase="pre",
    ))

    captured = capsys.readouterr()
    assert rc == 0 and captured.out == "" and "정체성 미해소" in captured.err
    assert mod.CURRENT_FILE.read_text(encoding="utf-8") == original


def test_append_atomic_uses_one_o_append_write(tmp_path, monkeypatch):
    """공유 log append seam은 RMW 없이 O_APPEND 단일 write를 사용한다.

    O_APPEND write 자체는 공용 `file_lock` seam이 소유하므로(T-0565) 관측도 그 seam에서
    한다 — pm_log는 락 경로 규약과 부모 디렉토리 생성만 정한다.
    """
    mod = _load_module()
    lock_mod = mod._load_file_lock()
    calls = []
    monkeypatch.setattr(
        lock_mod.os,
        "open",
        lambda path, flags, mode=0: calls.append(("open", path, flags, mode)) or 41,
    )
    monkeypatch.setattr(
        lock_mod.os, "write", lambda fd, payload: calls.append(("write", fd, payload))
    )
    monkeypatch.setattr(lock_mod.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    monkeypatch.setattr(lock_mod.os, "close", lambda fd: calls.append(("close", fd)))

    target = tmp_path / "current.md"
    lock_mod.append_atomic(target, "\nentry")

    assert calls[0][0:2] == ("open", str(target))
    assert calls[0][2] & lock_mod.os.O_APPEND
    assert calls[0][2] & lock_mod.os.O_CREAT
    assert calls[1:] == [
        ("write", 41, b"\nentry"), ("fsync", 41), ("close", 41),
    ]


def test_append_log_delegates_the_write_to_the_shared_seam(tmp_path, monkeypatch):
    """append_log는 락 구간 *안에서* seam append를 호출한다 (직접 write 0)."""
    mod = _load_module()
    lock_mod = mod._load_file_lock()
    calls = []
    monkeypatch.setattr(
        lock_mod,
        "append_atomic",
        lambda path, text, **kwargs: calls.append((Path(path), text)),
    )
    current = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"

    mod.append_log(current, "\nentry")

    assert calls == [(current, "\nentry")]
    assert (tmp_path / ".project_manager" / ".local" / "log.lock").is_file()
    assert not current.exists()   # 실제 write는 seam이 소유(대역이 가로챔)


def test_append_log_locked_is_the_lock_free_inner_primitive(tmp_path, monkeypatch):
    """판정과 append를 한 외부 lock에 묶는 소비자는 inner primitive를 재락 없이 쓴다."""
    mod = _load_module()
    lock_mod = mod._load_file_lock()
    calls = []
    monkeypatch.setattr(
        lock_mod,
        "append_atomic",
        lambda path, text, **kwargs: calls.append((Path(path), text)),
    )
    current = tmp_path / "current.md"

    mod.append_log_locked(current, "entry")

    assert calls == [(current, "entry")]
    assert not mod._log_lock_path(current).exists()


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
        assert "_load_pm_log(" in source, name
        assert ".append_log(" in source or ".append_log_locked(" in source, name
        assert not re.search(r"self\._log_file\.write_text\(", source), name

    handoff_source = consumer_sources["pm_handoff.py"]
    if ".append_log_locked(" in handoff_source:
        assert "with pm_log.log_write_lock(" in handoff_source

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


# ── 소유 PM 홈 유도 (T-0888) ────────────────────────────────────────────────

def _synthetic_pm_home(root: Path, *, repo: str = "app") -> Path:
    """실 티켓 1건 + lease 장부 + `.repos/<repo>.git` 을 가진 합성 PM 홈."""
    pm_home = root / "pm-home"
    tickets = pm_home / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    (tickets / "T-0001-fixture.md").write_text(
        "---\nid: T-0001\ntitle: fixture\nstatus: open\n---\n", encoding="utf-8",
    )
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"leases": [{"slot": f"work/{repo}_1", "state": "leased"}]}),
        encoding="utf-8",
    )
    slot = pm_home / "work" / f"{repo}_1"
    slot.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, slot, repo)
    return pm_home


def _markerless_tree(root: Path) -> Path:
    """`.git` 도 `.project_manager` 실 board 도 없는 합성 트리 — 아무의 worktree 도 아니다."""
    root.mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    return root


def test_pm_home_resolution_is_position_independent(tmp_path):
    """같은 모양의 트리는 파일시스템 어디에 있어도 같은 답을 낸다 — 위치는 판정 입력이 아니다."""
    mod = _load_module()
    pm_home = _synthetic_pm_home(tmp_path)
    slot = pm_home / "work" / "app_1"

    outside = _markerless_tree(tmp_path / "anchor")
    inside = _markerless_tree(slot / ".project_manager" / ".local" / "tmp" / "anchor")

    # 조상 훑기를 되살리면 (b) 만 합성 PM 홈을 돌려줘 두 값이 갈린다.
    assert mod.owning_pm_home(outside) == outside.resolve()
    assert mod.owning_pm_home(inside) == inside.resolve()
    assert mod.owning_pm_home(slot) == pm_home.resolve()


def test_registered_slot_resolves_from_git_pointer_without_subprocess(
    tmp_path, monkeypatch,
):
    """등록 슬롯·PM 홈 밖 절대 슬롯·저장소 밖 스냅샷 셋 다 subprocess 0회로 소유 홈을 낸다."""
    mod = _load_module()
    pm_home = _synthetic_pm_home(tmp_path)
    slot = pm_home / "work" / "app_1"

    absolute_slot = tmp_path / "external-slots" / "app_2"
    absolute_slot.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, absolute_slot, "app")

    snapshot = tmp_path / "scratch" / "gate-X"
    snapshot.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, snapshot, "app")

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("PM 홈 유도는 git subprocess 를 부르지 않는다")

    monkeypatch.setattr(mod.subprocess, "run", _forbidden)

    for anchor in (slot, absolute_slot, snapshot):
        assert mod.owning_pm_home(anchor) == pm_home.resolve(), anchor


def _declare_slot_git_pointer(pm_home: Path, worktree: Path, repo: str = "product") -> None:
    """슬롯이 소유 PM 홈을 선언하는 `.git` 포인터를 세운다(`worktree_pool` 실 형상).

    공용 bare 저장소는 `<pm_home>/.repos/<repo>.git` 이고 슬롯의 `.git` 은 그 안 worktree
    gitdir 을 가리킨다. 소유 판정의 유일한 입력이라 이 선언이 없는 트리는 자기 자신이다.
    """
    git_dir = pm_home / ".repos" / f"{repo}.git" / "worktrees" / worktree.name
    git_dir.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")


def test_main_checkpoint_worktree_anchor_forwards_to_pm_home_engine(
    tmp_path, monkeypatch
):
    """checkpoint는 lease 역참조로 worktree 엔진 대신 PM 홈 엔진을 호출한다."""
    mod = _load_module()
    pm_home = tmp_path / "pm-home"
    worktree = pm_home / "work" / "product_1"
    worktree.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, worktree)
    monkeypatch.setattr(mod, "REPO", worktree)
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"leases":[{"slot":"work/product_1","state":"leased","session":"main"}]}',
        encoding="utf-8",
    )
    canonical = pm_home / ".project_manager" / "tools" / "pm_log.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("# canonical test probe\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=23),
    )

    rc = mod.main([
        "checkpoint", "--task", "main", "--trigger", "compaction",
        "--cwd", str(worktree),
    ])

    assert rc == 0 and len(calls) == 1  # compaction hook은 canonical 실패 rc도 밖으로 전파하지 않는다.
    argv, kwargs = calls[0]
    assert Path(argv[1]) == canonical
    assert argv[2:4] == ["checkpoint", "--task"]
    assert kwargs["cwd"] == str(pm_home)
    assert kwargs["timeout"] == 5.0


@pytest.mark.parametrize(
    ("hook_argv", "new_options"),
    [
        ([
            "checkpoint", "--trigger", "compaction", "--phase", "pre",
            "--ctx-band-checked", "--ctx-band-missed",
            "--ctx-window-tokens", "600000",
            "--ctx-observed-tokens", "30000", "--harness", "claude",
        ], {
            "--ctx-band-checked", "--ctx-band-missed", "--ctx-window-tokens",
            "--ctx-observed-tokens", "--harness",
        }),
    ],
)
def test_main_hook_redispatch_retries_old_pm_home_engine_without_new_options(
    tmp_path, monkeypatch, capsys, hook_argv, new_options,
):
    """worktree 신형 엔진→PM 홈 구형 엔진 skew에서도 snapshot/checkpoint를 한 번 재시도한다."""
    mod = _load_module()
    pm_home = tmp_path / "pm-home"
    worktree = pm_home / "work" / "product_1"
    worktree.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, worktree)
    monkeypatch.setattr(mod, "REPO", worktree)
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"leases":[{"slot":"work/product_1","state":"leased","session":"main"}]}',
        encoding="utf-8",
    )
    canonical = pm_home / ".project_manager" / "tools" / "pm_log.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("# old canonical test probe\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=2 if len(calls) == 1 else 0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod.main([*hook_argv, "--cwd", str(worktree)]) == 0
    assert len(calls) == 2
    assert new_options & set(calls[0][0]) == new_options
    assert new_options.isdisjoint(calls[1][0])
    assert calls[0][1]["cwd"] == calls[1][1]["cwd"] == str(pm_home)
    assert mod._CTX_DIAGNOSTIC_APPEND_FAILED_SIGNAL in capsys.readouterr().err


def test_main_manual_checkpoint_forwards_pm_home_engine_rc(
    tmp_path, monkeypatch,
):
    """수동 checkpoint는 PM 홈 엔진의 이름/로그/엔진 오류 rc를 성공으로 평탄화하지 않는다."""
    mod = _load_module()
    pm_home = tmp_path / "pm-home"
    worktree = pm_home / "work" / "product_1"
    worktree.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, worktree)
    monkeypatch.setattr(mod, "REPO", worktree)
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"leases":[{"slot":"work/product_1","state":"leased","session":"main"}]}',
        encoding="utf-8",
    )
    canonical = pm_home / ".project_manager" / "tools" / "pm_log.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("# canonical test probe\n", encoding="utf-8")
    monkeypatch.setattr(
        mod.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=17),
    )

    assert mod.main([
        "checkpoint", "--task", "invalid/name", "--cwd", str(worktree),
    ]) == 17


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["snapshot"], "snapshot"),
        (["checkpoint", "--trigger", "compaction", "--phase", "pre"], "checkpoint"),
    ],
)
def test_main_external_absolute_lease_uses_file_git_common_dir_pm_home(
    tmp_path, monkeypatch, argv, command,
):
    """PM 홈 밖 absolute slot도 gitdir/commondir를 따라 canonical snapshot/checkpoint로 간다."""
    mod = _load_module()
    pm_home = tmp_path / "pm-home"
    worktree = tmp_path / "external-slots" / "product_1"
    worktree.mkdir(parents=True)
    git_dir = pm_home / ".repos" / "product.git" / "worktrees" / "product_1"
    git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({
            "leases": [{
                "slot": str(worktree), "state": "leased", "session": "main",
            }],
        }),
        encoding="utf-8",
    )
    canonical = pm_home / ".project_manager" / "tools" / "pm_log.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("# canonical test probe\n", encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", worktree)
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda call_argv, **kwargs: calls.append((call_argv, kwargs))
        or SimpleNamespace(returncode=19),
    )

    assert mod.owning_pm_home(worktree) == pm_home
    assert mod.main([*argv, "--cwd", str(worktree)]) == 0
    assert len(calls) == 1  # PM 홈 해소 자체는 git subprocess를 만들지 않는다.
    call_argv, kwargs = calls[0]
    assert Path(call_argv[1]) == canonical and call_argv[2] == command
    assert kwargs["cwd"] == str(pm_home)


def test_main_checkpoint_unregistered_slot_fails_loud_without_self_demotion(
    tmp_path, monkeypatch,
):
    """소유 PM 홈을 확정 못 하는 슬롯은 자기 트리에 stray log를 만들지 않고 그대로 터진다.

    옛 경로는 lease 미등재를 board detector로 재확인해 안내 rc1(수동)·무음 rc0(hook)으로
    나눴다. 1차 해소가 fail-loud가 되면 그 2차 축은 존재 이유가 없다 — 미해소 상태 자체가
    생기지 않는다.
    """
    mod = _load_module()
    pm_home = tmp_path / "pm-home"
    worktree = pm_home / "work" / "product_1"
    worktree.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, worktree)   # 선언은 있으나 lease 장부가 없다.
    (pm_home / ".project_manager").mkdir(parents=True, exist_ok=True)
    _redirect_paths(mod, monkeypatch, worktree)

    with pytest.raises(mod.PmHomeResolutionError, match="worktree lease 장부 없음"):
        mod.main(["checkpoint", "--task", "main"])
    assert not mod.CURRENT_FILE.exists()


def test_main_tail_remains_ungated(tmp_path, monkeypatch, capsys):
    """read-only tail은 worktree 앵커 판정을 호출하지 않고 현행 동작을 유지한다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "current.md").write_text(_HEADER + _ENTRY_A, encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "owning_pm_home",
        lambda *_: pytest.fail("tail must not resolve a PM-home anchor"),
    )

    assert mod.main(["tail"]) == 0
    assert "첫 작업" in capsys.readouterr().out


# ── compaction snapshot + checkpoint dedup (T-0621) ────────────────────────

def _snapshot_home(tmp_path: Path, *, tasks=("main",), lease_session="main"):
    pm_home = tmp_path / "pm-home"
    local = pm_home / ".project_manager" / ".local"
    for task in tasks:
        state = local / "tasks" / task / "pm_state.md"
        state.parent.mkdir(parents=True)
        state.write_text(f"# {task} state\n- 남은 작업: snapshot 검증\n", encoding="utf-8")
    lease = local / "worktree-leases.json"
    lease.write_text(
        '{"leases":[{"slot":"work/product_1","state":"leased","session":"'
        + lease_session + '"},{"slot":"work/product_2","state":"idle","session":""}]}',
        encoding="utf-8",
    )
    for status, count in (("open", 2), ("claimed", 1), ("blocked", 0), ("done", 3)):
        directory = pm_home / ".project_manager" / "board" / "tickets" / status
        directory.mkdir(parents=True)
        for index in range(count):
            (directory / f"T-{index:04d}-fixture.md").write_text("fixture\n", encoding="utf-8")
    cwd = pm_home / "work" / "product_1" / "nested"
    cwd.mkdir(parents=True)
    return pm_home, cwd


# ── 진행 중 작업 절 픽스처 헬퍼 (T-0787) ────────────────────────────────────

def _seed_delegate_rounds(pm_home: Path, rows: list[dict]) -> Path:
    """`_snapshot_home` 픽스처 위에 delegate-rounds 장부를 얹는다."""
    ledger = pm_home / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    ledger.write_text(body + ("\n" if rows else ""), encoding="utf-8")
    return ledger


def _seed_raw_ledger(pm_home: Path, records: list[dict]) -> Path:
    """`_snapshot_home` 픽스처 위에 raw 장부를 얹는다."""
    ledger = pm_home / ".project_manager" / ".local" / "raw_outputs.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"version": 1, "records": records}, ensure_ascii=False), encoding="utf-8",
    )
    return ledger


def _delegate_row(
    tmp_path: Path, ticket: str, *, ordinal: int = 1, role: str = "architect",
    harvested_at: str | None = None, prepared_at: str = "2026-08-21T00:00:00.000000+00:00",
) -> dict:
    return {
        "ticket": ticket, "role": role, "ordinal": ordinal,
        "run_id": "a" * 32, "copy": str(tmp_path / f"{ticket}-{role}-{ordinal}.md"),
        "board_rel": f"wiki/tickets/rounds/{ticket}/{role}-{ordinal}.md",
        "prepared_at": prepared_at, "harvested_at": harvested_at,
    }


def _raw_record(
    tmp_path: Path, record_id: str, *, started_at: str = "2026-08-21T00:00:00.000000+00:00",
    finished_at: str | None = None,
) -> dict:
    return {
        "id": record_id, "surface": "delegate", "harness": "codex", "model": "gpt",
        "role": "architect", "attempt": "1", "pid": 999, "started_at": started_at,
        "raw_path": str(tmp_path / f"raw-{record_id}.txt"), "finished_at": finished_at,
    }


def test_inflight_section_surfaces_unharvested_raw_claimed_and_wip(tmp_path):
    """T-0787 — 존재 형상: 미회수·미마감 raw·claimed·슬롯 WIP 네 항목이 실값으로 편입된다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    _seed_delegate_rounds(pm_home, [
        _delegate_row(tmp_path, "T-0787"),
        _delegate_row(tmp_path, "T-0786", harvested_at="2026-08-20T00:00:00.000000+00:00"),
    ])
    _seed_raw_ledger(pm_home, [_raw_record(tmp_path, "r1")])
    claimed_dir = pm_home / ".project_manager" / "board" / "tickets" / "claimed"
    (claimed_dir / "T-9999-extra.md").write_text("fixture\n", encoding="utf-8")

    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None
    assert "## 진행 중 작업 (장부 실측)" in text
    # T-0786 은 harvested_at 이 있어 미회수 집계에서 제외된다(1건만).
    assert "미회수 라운드 준비 1건: T-0787 architect#1" in text
    assert "미마감 위임 raw 1건:" in text and "delegate/codex" in text
    assert "claimed 티켓 2건: T-0000-fixture · T-9999-extra" in text
    assert "슬롯 WIP: work/product_1" in text


def test_inflight_section_absent_ledgers_render_zero_counts(tmp_path):
    """T-0787 — 부재 형상: 장부가 없어도 0건으로 표기되고 rc·warning 은 정상이다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)

    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None
    assert "미회수 라운드 준비 0건" in text
    assert "미마감 위임 raw 0건" in text
    assert "claimed 티켓 1건: T-0000-fixture" in text  # _snapshot_home 기본 claimed=1
    assert "슬롯 WIP: work/product_1" in text


def test_inflight_section_absorbs_corrupt_delegate_ledger_as_one_line(tmp_path, capsys):
    """T-0787 — fail-soft(손상): 비-UTF8 장부도 절 하나만 1줄로 접고 다른 절은 온전하다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    ledger = pm_home / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\xff\xfe \xf8\xa1\xa1\xa1\xa1 not valid utf-8\n")

    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None and text is not None
    assert "## 진행 중 작업 (장부 실측)\n- 조회 실패:" in text
    assert "## 장부 포인터" in text and "## pm_state" in text and "## 복구 포인터" in text
    assert "조회 실패" in capsys.readouterr().err


def test_inflight_section_absorbs_module_load_failure_as_one_line(tmp_path, monkeypatch, capsys):
    """T-0787 — fail-soft(형제 부재): 로드 실패도 절 하나만 1줄로 접는다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)

    def _boom():
        raise FileNotFoundError("pm_delegate.py 형제 부재(시뮬레이션)")

    monkeypatch.setattr(mod, "_load_pm_delegate", _boom)
    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None and text is not None
    assert "## 진행 중 작업 (장부 실측)\n- 조회 실패:" in text
    assert "## 장부 포인터" in text and "## pm_state" in text
    assert "조회 실패" in capsys.readouterr().err


def test_inflight_section_absorbs_engine_rev_skew_and_labels_it(tmp_path, monkeypatch, capsys):
    """T-0787 — fail-soft(사본 skew): 재-raise 하지 않고 등록 경계에서 흡수해 표출한다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)

    def _boom():
        err = RuntimeError("엔진 사본 버전 불일치(시뮬레이션)")
        err._engine_rev_skew = True
        raise err

    monkeypatch.setattr(mod, "_load_pm_delegate", _boom)
    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None and text is not None
    assert "엔진 사본 불일치" in text
    stderr = capsys.readouterr().err
    assert "진행 중 작업" in stderr and "엔진 사본 불일치" in stderr


def test_inflight_raw_query_never_enters_ledger_lock(tmp_path, monkeypatch):
    """T-0787 — 무락 단언: 진행 중 작업 절의 raw 조회는 `_raw_ledger_lock` 에 진입하지 않는다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    _seed_raw_ledger(pm_home, [_raw_record(tmp_path, "r1")])
    real_relay = mod._load_pm_relay()

    @contextlib.contextmanager
    def _fail_if_locked(*_a, **_k):
        pytest.fail("진행 중 작업 절의 raw 조회가 배타 파일락에 진입함(lock=False 계약 위반)")
        yield  # pragma: no cover — 도달하면 이미 실패

    monkeypatch.setattr(real_relay, "_raw_ledger_lock", _fail_if_locked)
    monkeypatch.setattr(mod, "_load_pm_relay", lambda: real_relay)

    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None
    assert "미마감 위임 raw 1건" in text


def test_inflight_section_folds_pathological_counts_within_line_and_section_caps(tmp_path):
    """T-0787 — cap/truncate: 미회수 500·claimed 300 에서도 줄·절·전체 상한을 지킨다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    rows = [_delegate_row(tmp_path, f"T-{index:04d}") for index in range(500)]
    _seed_delegate_rounds(pm_home, rows)
    claimed_dir = pm_home / ".project_manager" / "board" / "tickets" / "claimed"
    for index in range(300):
        (claimed_dir / f"T-{index + 5000:04d}-extra.md").write_text("f\n", encoding="utf-8")

    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None and text is not None
    assert len(text) <= mod.SNAPSHOT_MAX_CHARS
    assert len(text.encode("utf-8")) <= mod.SNAPSHOT_MAX_BYTES
    section = text.split("## 진행 중 작업 (장부 실측)\n", 1)[1].split("\n## ", 1)[0]
    for line in section.splitlines():
        assert len(line) <= mod._INFLIGHT_MAX_LINE_CHARS
    assert section.count("\n") <= mod._INFLIGHT_MAX_LINES
    assert "외 497건" in text  # 500건 중 3건만 나열
    assert "claimed 티켓 301건" in text  # 기본 1 + 신규 300
    assert "## pm_state" in text and "## 복구 포인터" in text


def test_snapshot_cap_keeps_hearsay_when_identity_alone_is_oversized(tmp_path):
    """T-0787 F-001 — 총량 cap 경로: identity 가 거의 상한을 채워도 전언 경고는 always-keep."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    oversized = mod._SNAPSHOT_IDENTITY_HEADING + "\n" + ("x" * 7_900)  # 리뷰 repro(7,942c) 규모
    monkeypatch_target = mod._identity_section
    mod._identity_section = lambda *_a, **_k: oversized
    try:
        text, warning = mod.build_snapshot(pm_home, cwd)
    finally:
        mod._identity_section = monkeypatch_target

    assert warning is None and text is not None
    assert "전언 경고" in text
    assert len(text) <= mod.SNAPSHOT_MAX_CHARS
    assert len(text.encode("utf-8")) <= mod.SNAPSHOT_MAX_BYTES


def test_snapshot_cap_keeps_hearsay_when_ctx_diagnostic_is_oversized(tmp_path):
    """T-0787 F-001 — oversized ctx 진단(전체 상한보다 큰 mismatch)에서도 전언 경고가 산다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    huge_mismatch = "## ctx 설정 진단 (compaction 경계)\n" + ("한" * 9_000) + "\n"
    monkeypatch_target = mod._latest_ctx_window_mismatch_section
    mod._latest_ctx_window_mismatch_section = lambda *_a, **_k: huge_mismatch
    try:
        text, warning = mod.build_snapshot(pm_home, cwd)
    finally:
        mod._latest_ctx_window_mismatch_section = monkeypatch_target

    assert warning is None and text is not None
    assert "전언 경고" in text
    assert len(text) <= mod.SNAPSHOT_MAX_CHARS
    assert len(text.encode("utf-8")) <= mod.SNAPSHOT_MAX_BYTES


def test_wip_slot_line_recomputes_remaining_budget_per_probe_call(tmp_path):
    """T-0787 F-002 — 각 프로브 직전 잔여를 재계산 — 누적 timeout 합이 최초 잔여를 넘지 않는다."""
    mod = _load_module()
    fake_targets = [
        ("slot-a", tmp_path / "a"), ("slot-b", tmp_path / "b"), ("slot-c", tmp_path / "c"),
    ]
    orig_targets = mod._wip_probe_targets
    orig_probe = mod._git_status_counts
    clock = [0.0]

    def fake_monotonic():
        return clock[0]

    calls: list[float] = []

    def fake_git_status_counts(_path, timeout):
        calls.append(timeout)
        clock[0] += timeout  # 최악의 hang(=timeout 전량 소요)을 흉내낸다.
        return None

    mod._wip_probe_targets = lambda *_a, **_k: (fake_targets, 0)
    mod._git_status_counts = fake_git_status_counts
    try:
        initial_remaining = 0.5
        line = mod._wip_slot_line(
            tmp_path, "main", deadline=initial_remaining, monotonic=fake_monotonic,
        )
    finally:
        mod._wip_probe_targets = orig_targets
        mod._git_status_counts = orig_probe

    assert sum(calls) <= initial_remaining + 1e-9
    assert len(calls) < len(fake_targets)  # 첫 호출이 예산을 다 써 나머지는 spawn 되지 않는다.
    assert "예산 소진" in line


def test_inflight_raw_field_newlines_do_not_forge_headings_or_exceed_line_cap(tmp_path):
    """T-0787 F-003 — raw 장부 필드의 개행이 가짜 heading 주입·줄 수 상한 우회를 못 한다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    forged_started_at = "2026-08-21T00:00:00\n" + "\n".join(
        f"## forged-{index}" for index in range(8)
    )
    _seed_raw_ledger(pm_home, [{
        "id": "r1", "surface": "delegate\n## forged-surface", "harness": "codex",
        "model": "gpt", "role": "architect", "attempt": "1", "pid": 1,
        "started_at": forged_started_at, "raw_path": str(tmp_path / "raw.txt"),
    }])

    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None and text is not None
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        mod._SNAPSHOT_IDENTITY_HEADING,
        "## 전언 경고 (요약 속 단언은 미검증)",
        "## 장부 포인터",
        "## 진행 중 작업 (장부 실측)",
        f"## pm_state 머리 ({mod.SNAPSHOT_PM_STATE_LINES}줄 상한)",
        "## 복구 포인터",
    ]
    section = text.split("## 진행 중 작업 (장부 실측)\n", 1)[1].split("\n## ", 1)[0]
    assert section.count("\n") <= mod._INFLIGHT_MAX_LINES
    for line in section.splitlines():
        assert len(line) <= mod._INFLIGHT_MAX_LINE_CHARS


def test_inflight_section_multiline_exception_does_not_forge_headings(tmp_path, monkeypatch):
    """T-0787 F-003 — fail-soft 절의 예외 사유에 개행이 섞여도 heading 주입이 없다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)

    def _boom():
        raise RuntimeError("bad ledger\n## forged-exc-1\n## forged-exc-2")

    monkeypatch.setattr(mod, "_load_pm_delegate", _boom)
    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None and text is not None
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert "## forged-exc-1" not in headings and "## forged-exc-2" not in headings
    section = text.split("## 진행 중 작업 (장부 실측)\n", 1)[1].split("\n## ", 1)[0]
    assert section.count("\n") <= mod._INFLIGHT_MAX_LINES


def test_wip_probe_targets_reserve_pm_home_seat_when_leased_slots_exceed_cap(
    tmp_path, monkeypatch,
):
    """T-0787 F-004 — leased 슬롯이 상한보다 많아도 PM 홈이 밀려나지 않고 생략 수를 돌려준다."""
    mod = _load_module()
    pm_home = tmp_path / "pm-home-distinct"
    fake_slots = [(f"slot-{letter}", tmp_path / f"slot-{letter}") for letter in "abcd"]
    monkeypatch.setattr(mod, "_lease_task_slots", lambda *_a, **_k: fake_slots)

    targets, skipped = mod._wip_probe_targets(pm_home, "main")

    assert len(targets) == mod._WIP_PROBE_MAX_CALLS
    assert targets[-1][0] == "PM 홈"
    assert skipped == len(fake_slots) - (mod._WIP_PROBE_MAX_CALLS - 1)


def test_wip_slot_line_surfaces_skipped_slot_count_without_spawning(tmp_path, monkeypatch):
    """T-0787 F-004 — 상한 때문에 못 본 leased 슬롯은 spawn 없이 "외 N개(프로브 생략)"로 남는다."""
    mod = _load_module()
    pm_home = tmp_path / "pm-home-distinct"
    fake_slots = [(f"slot-{letter}", tmp_path / f"slot-{letter}") for letter in "abcd"]
    monkeypatch.setattr(mod, "_lease_task_slots", lambda *_a, **_k: fake_slots)
    probe_calls: list[Path] = []

    def fake_git_status_counts(path, _timeout):
        probe_calls.append(path)
        return (0, 0, 0)

    monkeypatch.setattr(mod, "_git_status_counts", fake_git_status_counts)

    line = mod._wip_slot_line(pm_home, "main", deadline=10.0, monotonic=lambda: 0.0)

    assert "PM 홈" in line
    assert f"외 2개(프로브 생략)" in line
    assert len(probe_calls) == mod._WIP_PROBE_MAX_CALLS


def test_snapshot_max_bytes_fits_opencode_channel_cap():
    """T-0787 — 채널 상한 파리티: SNAPSHOT_MAX_BYTES ≤ opencode spawnSync `maxBuffer` 실값."""
    mod = _load_module()
    source = (
        REPO / "templates" / "opencode" / ".opencode" / "lib" / "ctx-guard-core.cjs"
    ).read_text(encoding="utf-8")
    match = re.search(r"maxBuffer:\s*(\d+)\s*\*\s*(\d+)", source)
    assert match, "opencode maxBuffer 상수를 찾지 못함 — ctx-guard-core.cjs 형식 확인"
    opencode_max_buffer = int(match.group(1)) * int(match.group(2))
    assert mod.SNAPSHOT_MAX_BYTES <= opencode_max_buffer


def test_snapshot_resolves_cwd_lease_before_multiple_active_tasks(tmp_path):
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path, tasks=("doc", "main"), lease_session="main")

    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None
    assert text.startswith(mod._SNAPSHOT_IDENTITY_HEADING)
    assert "- task: main" in text and "- 해소: cwd→lease" in text
    assert "활성 tasks (2): doc, main" in text
    assert "open 2 / claimed 1 / blocked 0 / done 3" in text
    assert "# main state" in text and "# doc state" not in text
    assert len(text) <= 8_000 and len(text.encode("utf-8")) <= 24_000


def test_snapshot_single_active_task_fallback_and_multi_task_skip(tmp_path):
    mod = _load_module()
    pm_home, _cwd = _snapshot_home(tmp_path / "single", tasks=("only",), lease_session="")
    outside = tmp_path / "outside"
    outside.mkdir()
    text, warning = mod.build_snapshot(pm_home, outside)
    assert warning is None and "- task: only" in text and "단일 활성 task" in text

    pm_home2, _ = _snapshot_home(tmp_path / "multi", tasks=("a", "b"), lease_session="")
    text2, warning2 = mod.build_snapshot(pm_home2, outside)
    assert text2 is None and warning2.count("\n") == 0
    assert "정체성 미해소" in warning2


def test_single_active_task_identity_emits_snapshot_and_records_compaction_checkpoint(
    tmp_path, monkeypatch, capsys,
):
    """cwd 가 어느 lease 에도 안 걸리는 단일 활성 task 형상은 두 경계를 모두 살린다.

    [[T-0793]] 이후 정체성 해소는 cwd→lease·단일 활성 task 두 층뿐이다 — 장부(task/lease)가
    아예 없는 "legacy pm_state" 층은 삭제됐다(`resolve_snapshot_identity` 독스트링: "그 층은
    없다"). `_pm_state_path` 도 이제 `source` 와 무관하게 task 스코프 경로 하나뿐이라 legacy
    wiki 최상위 `pm_state.md` 로 갈 곳이 없다 — 이 테스트는 남은 두 층 중 cwd 가 lease 밖인
    형상(단일 활성 task 폴백)에서 snapshot systemMessage 봉투와 checkpoint 기록이 함께
    사는지를 고정한다(cwd→lease 층은 `test_snapshot_resolves_cwd_lease_before_multiple_
    active_tasks` 가 이미 고정한다).
    """
    mod = _load_module()
    pm_home = tmp_path / "solo"
    manager = pm_home / ".project_manager"
    wiki = manager / "wiki"
    log_dir = wiki / "log"
    log_dir.mkdir(parents=True)
    (manager / "local.conf").write_text(
        "# fresh solo adopter\nsession=pm\n", encoding="utf-8",
    )
    task_state = manager / ".local" / "tasks" / "pm" / "pm_state.md"
    task_state.parent.mkdir(parents=True)
    task_state.write_text(
        "# pm state\n- 남은 작업: solo compaction 복구\n", encoding="utf-8",
    )
    current = log_dir / "current.md"
    current.write_text(_HEADER, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", pm_home)
    monkeypatch.setattr(mod, "WIKI_DIR", wiki)
    monkeypatch.setattr(mod, "LOG_DIR", log_dir)
    monkeypatch.setattr(mod, "CURRENT_FILE", current)

    snapshot_args = SimpleNamespace(cwd=str(pm_home), state_lines=24, json=True)
    assert mod.cmd_snapshot(snapshot_args) == 0
    payload = json.loads(capsys.readouterr().out)
    snapshot = payload["systemMessage"]
    assert payload["suppressOutput"] is False
    assert "- task: pm" in snapshot and "- 해소: 단일 활성 task" in snapshot
    assert f"- pm_state: {task_state}" in snapshot
    assert "# pm state" in snapshot

    before = len(mod.split_entries(current.read_text(encoding="utf-8"))[1])
    checkpoint_args = SimpleNamespace(
        task=None, trigger="compaction", cwd=str(pm_home), session_id="solo-session",
        boundary_id="solo-boundary", phase="post", breadcrumb=False,
    )
    assert mod.cmd_checkpoint(checkpoint_args) == 0
    entries = mod.split_entries(current.read_text(encoding="utf-8"))[1]
    assert len(entries) == before + 1
    assert "checkpoint | (task:pm) — compaction" in entries[-1][1]


def test_snapshot_timeout_returns_identity_and_hearsay_only(tmp_path):
    """T-0787 — timeout 우선순위: 첫 deadline 초과에도 전언 경고는 always-keep 접두로 산다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    ticks = iter((10.0, 13.1))
    text, warning = mod.build_snapshot(pm_home, cwd, monotonic=lambda: next(ticks))
    assert warning is None
    assert text.startswith(mod._SNAPSHOT_IDENTITY_HEADING)
    assert mod.SNAPSHOT_HEARSAY_WARNING.rstrip("\n") in text
    assert "## 장부 포인터" not in text and "## 진행 중 작업" not in text
    assert "## pm_state" not in text


def test_snapshot_distinguishes_log_read_failure_and_explicit_retry_payload(
    tmp_path, monkeypatch, capsys,
):
    """원장 판독 실패는 no-diagnostic이 아니며 CLI rc1, 명시 retry 진단은 원장 없이도 렌더한다."""
    mod = _load_module("pm_log_snapshot_read_failure")
    pm_home, cwd = _snapshot_home(tmp_path)
    current = pm_home / ".project_manager" / "wiki" / "log" / "current.md"
    current.mkdir(parents=True)  # file read에 IsADirectoryError를 내는 결정적 판독 실패 fixture.

    assert mod._latest_ctx_window_mismatch_section(pm_home, "main") \
        is mod._CTX_WINDOW_MISMATCH_READ_FAILED
    text, warning = mod.build_snapshot(pm_home, cwd)
    assert text is None and warning == mod._CTX_WINDOW_MISMATCH_READ_WARNING

    monkeypatch.setattr(mod, "REPO", pm_home)
    args = SimpleNamespace(cwd=str(cwd), state_lines=24, json=False)
    assert mod.cmd_snapshot(args) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and mod._CTX_WINDOW_MISMATCH_READ_WARNING in captured.err

    retry, retry_warning = mod.build_snapshot(
        pm_home,
        cwd,
        ctx_band_missed=True,
        ctx_window_tokens=600_000,
        ctx_observed_tokens=30_000,
        harness="claude",
    )
    assert retry_warning is None
    assert retry.count("[ctx-window-mismatch]") == 1
    assert "설정 600,000 tokens" in retry and "PreCompact 관측 30,000 tokens" in retry


def test_snapshot_caps_from_back_by_whole_section_under_char_and_byte_limits():
    mod = _load_module()
    identity = mod._SNAPSHOT_IDENTITY_HEADING + "\n- task: main\n"
    sections = ["## keep\nsmall\n", "## drop\n" + ("한" * 9_000) + "\n"]
    text = mod.cap_snapshot_sections(identity, sections)
    assert "## keep" in text and "## drop" not in text
    assert len(text) <= mod.SNAPSHOT_MAX_CHARS
    assert len(text.encode("utf-8")) <= mod.SNAPSHOT_MAX_BYTES


def test_snapshot_caps_keep_ctx_mismatch_ahead_of_oversized_lower_priority_sections(
    tmp_path, monkeypatch,
):
    """상한 tail-drop에서도 loud mismatch 절은 장부·state·복구 절보다 먼저 보존된다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    monkeypatch.setattr(
        mod, "_latest_ctx_window_mismatch_section",
        lambda *_args: (
            "## ctx 설정 진단 (compaction 경계)\n"
            + mod.build_ctx_window_mismatch_advisory(
                ctx_window_tokens=600_000,
                ctx_observed_tokens=None,
                harness="claude",
            )
        ),
    )
    monkeypatch.setattr(mod, "_ledger_section", lambda *_args: "## huge\n" + "한" * 9_000)

    text, warning = mod.build_snapshot(pm_home, cwd)

    assert warning is None
    assert "## ctx 설정 진단 (compaction 경계)" in text
    assert "[ctx-window-mismatch] 설정 창이 실 압축 지점보다 큼" in text
    assert "실 auto-compact 지점 이하" in text
    assert "## huge" not in text
    assert len(text) <= mod.SNAPSHOT_MAX_CHARS
    assert len(text.encode("utf-8")) <= mod.SNAPSHOT_MAX_BYTES


def test_ctx_mismatch_checkpoint_snapshot_flow_is_durable_and_re_evaluated(
    tmp_path, monkeypatch,
):
    """매 PreCompact append를 복구 원천으로 쓰고 다음 fired 평가가 앞 경고를 가린다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    wiki = pm_home / ".project_manager" / "wiki"
    log_dir = wiki / "log"
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    current.write_text(_HEADER, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", pm_home)
    monkeypatch.setattr(mod, "WIKI_DIR", wiki)
    monkeypatch.setattr(mod, "LOG_DIR", log_dir)
    monkeypatch.setattr(mod, "CURRENT_FILE", current)

    common = dict(
        task="main", trigger="compaction", cwd=str(cwd),
        session_id="ctx-flow-session", boundary_id="ctx-flow-boundary",
        breadcrumb=False, harness="claude",
    )
    pre = SimpleNamespace(
        **common, phase="pre", ctx_band_checked=True, ctx_band_missed=True,
        ctx_window_tokens=600_000, ctx_observed_tokens=30_000,
    )
    assert mod.cmd_checkpoint(pre) == 0
    assert mod.cmd_checkpoint(pre) == 0
    assert "[ctx-window-mismatch]" in current.read_text(encoding="utf-8")
    assert current.read_text(encoding="utf-8").count("[ctx-window-mismatch]") == 2
    state_dir = pm_home / ".project_manager" / ".local" / "ctx-stop"
    assert list(state_dir.glob("ctx-window-mismatch.*")) == []

    snapshot, warning = mod.build_snapshot(pm_home, cwd)
    assert warning is None
    assert "## ctx 설정 진단 (compaction 경계)" in snapshot
    assert "설정 600,000 tokens" in snapshot
    assert "PreCompact 관측 30,000 tokens" in snapshot
    assert "`harness.claude.ctx_window_tokens`" in snapshot

    fired_common = common | {"boundary_id": "ctx-flow-boundary-2"}
    fired = SimpleNamespace(
        **fired_common, phase="pre", ctx_band_checked=True, ctx_band_missed=False,
        ctx_window_tokens=None, ctx_observed_tokens=None,
    )
    assert mod.cmd_checkpoint(fired) == 0
    restored, warning = mod.build_snapshot(pm_home, cwd)
    assert warning is None
    assert "ctx-window-mismatch" not in restored


def test_snapshot_timeout_safely_caps_oversized_identity_on_early_return(
    tmp_path, monkeypatch,
):
    """timeout 조기 반환도 초대형 identity 절로 이중 상한을 우회하지 못한다."""
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    oversized = mod._SNAPSHOT_IDENTITY_HEADING + "\n" + ("🧭" * 30_000)
    monkeypatch.setattr(mod, "_identity_section", lambda *_args: oversized)
    ticks = iter((10.0, 13.1))

    text, warning = mod.build_snapshot(
        pm_home, cwd, monotonic=lambda: next(ticks),
    )

    assert warning is None and text.startswith(mod._SNAPSHOT_IDENTITY_HEADING)
    assert len(text) <= mod.SNAPSHOT_MAX_CHARS
    assert len(text.encode("utf-8")) <= mod.SNAPSHOT_MAX_BYTES


def test_compaction_checkpoint_claim_is_boundary_scoped_and_atomic(tmp_path, monkeypatch):
    mod = _load_module()
    _redirect_paths(mod, monkeypatch, tmp_path)
    marker = mod._compaction_marker_path("main", "session/unsafe", "boundary/7")
    assert marker.name == "compact-checkpoint.sessionunsafe.boundary7"
    assert mod.claim_compaction_checkpoint(marker, phase="pre") is True
    assert mod.claim_compaction_checkpoint(marker, phase="post") is False
    assert "phase=pre" in marker.read_text(encoding="utf-8")

    # 시간 간격과 무관하게 다른 경계는 즉시 독립 선점된다. 만료 marker unlink는 없다.
    next_marker = mod._compaction_marker_path("main", "session/unsafe", "boundary/8")
    assert mod.claim_compaction_checkpoint(next_marker, phase="pre") is True
    fallback = mod._compaction_marker_path("main", boundary_id="boundary/7")
    assert fallback.name == "compact-checkpoint.task-main.boundary7"


def test_compaction_checkpoint_parallel_claim_has_one_winner(tmp_path, monkeypatch):
    mod = _load_module()
    _redirect_paths(mod, monkeypatch, tmp_path)
    marker = mod._compaction_marker_path("main", "session-1", "boundary-1")
    barrier = threading.Barrier(2)
    results = []

    def claim():
        barrier.wait()
        results.append(mod.claim_compaction_checkpoint(marker, phase="pre"))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_compaction_checkpoint_claim_io_failure_is_not_duplicate(tmp_path, monkeypatch):
    mod = _load_module()
    marker = tmp_path / "blocked" / "marker"
    monkeypatch.setattr(mod.os, "open", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError()))
    assert mod.claim_compaction_checkpoint(marker, phase="pre") is None


def test_cmd_checkpoint_compaction_appends_at_most_one_entry_per_boundary(
        tmp_path, monkeypatch):
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    mod.CURRENT_FILE.write_text(_HEADER, encoding="utf-8")
    args = SimpleNamespace(
        task="main", trigger="compaction", session_id="session-1", cwd=str(tmp_path),
        boundary_id="boundary-1", phase="pre", breadcrumb=False,
    )
    assert mod.cmd_checkpoint(args) == 0
    assert mod.cmd_checkpoint(args) == 0
    entries = mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1]
    assert len(entries) == 1


def test_cmd_checkpoint_phase_fallback_pairs_pre_post_and_allows_next_boundary(
        tmp_path, monkeypatch):
    """boundary ID가 없는 구 어댑터도 pre/post를 한 경계로 묶고 다음 pre는 새 경계다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    mod.CURRENT_FILE.write_text(_HEADER, encoding="utf-8")
    common = dict(
        task="main", trigger="compaction", session_id="session-1", cwd=str(tmp_path),
        boundary_id=None, breadcrumb=False,
    )
    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="pre")) == 0
    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="post")) == 0
    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="pre")) == 0
    entries = mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1]
    assert len(entries) == 2


def test_cmd_checkpoint_implicit_boundaries_preserve_two_overlapping_sessions(
    tmp_path, monkeypatch,
):
    """ID 없는 Codex 두 Pre가 겹쳐도 각 pending 경계를 두 Post가 하나씩 소비한다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    mod.CURRENT_FILE.write_text(_HEADER, encoding="utf-8")
    common = dict(
        task="main", trigger="compaction", session_id=None, cwd=str(tmp_path),
        boundary_id=None, breadcrumb=False,
    )

    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="pre")) == 0
    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="pre")) == 0
    state_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
    assert len(list(state_dir.glob("compact-boundary.task-main.*"))) == 2

    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="post")) == 0
    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="post")) == 0

    entries = mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1]
    assert len(entries) == 2
    assert len(list(state_dir.glob("compact-checkpoint.task-main.*"))) == 2
    assert list(state_dir.glob("compact-boundary.task-main.*")) == []


def test_implicit_compaction_boundary_is_not_reused_after_log_archive(
        tmp_path, monkeypatch):
    """current.md archive로 순번이 0으로 돌아가도 영구 marker와 새 경계가 충돌하지 않는다."""
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    mod.CURRENT_FILE.write_text(_HEADER, encoding="utf-8")
    common = dict(
        task="main", trigger="compaction", session_id="session-archive",
        cwd=str(tmp_path), boundary_id=None, breadcrumb=False,
    )

    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="pre")) == 0
    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="post")) == 0
    first_markers = set(
        (tmp_path / ".project_manager" / ".local" / "ctx-stop").glob(
            "compact-checkpoint.session-archive.*"
        )
    )
    assert len(first_markers) == 1

    assert mod.cmd_archive(SimpleNamespace(before="9999-12-31", keep_last=None, dry_run=False)) == 0
    assert mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1] == []
    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="pre")) == 0
    assert mod.cmd_checkpoint(SimpleNamespace(**common, phase="post")) == 0

    markers = set(
        (tmp_path / ".project_manager" / ".local" / "ctx-stop").glob(
            "compact-checkpoint.session-archive.*"
        )
    )
    assert len(markers) == 2 and markers > first_markers
    assert len(mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1]) == 1
    assert not mod._implicit_boundary_state_path("main", "session-archive").exists()


def test_cmd_checkpoint_dedup_io_failure_warns_but_appends(
        tmp_path, monkeypatch, capsys):
    mod = _load_module()
    log_dir, _ = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    mod.CURRENT_FILE.write_text(_HEADER, encoding="utf-8")
    monkeypatch.setattr(mod, "claim_compaction_checkpoint", lambda *_a, **_k: None)
    args = SimpleNamespace(
        task="main", trigger="compaction", session_id="session-1", cwd=str(tmp_path),
        boundary_id="boundary-io-failure", phase="post", breadcrumb=False,
    )

    assert mod.cmd_checkpoint(args) == 0
    captured = capsys.readouterr()
    assert "dedup 장부 I/O 실패" in captured.err
    assert len(mod.split_entries(mod.CURRENT_FILE.read_text(encoding="utf-8"))[1]) == 1


def test_cmd_snapshot_reads_ledgers_without_subprocess_and_json_envelope_is_single_object(
        tmp_path, monkeypatch, capsys):
    """장부 조회(ticket copies·raw·claimed)는 spawn 0 — WIP 프로브만 상한 내 예외로 허용한다.

    T-0787 이전엔 어떤 ``subprocess.run`` 호출도 즉시 fail 이었다. WIP git 프로브 도입 이후에도
    가드는 죽지 않는다 — "장부 조회는 spawn 0" 으로 **좁혀서** 재단언한다(가드 삭제 금지).
    """
    mod = _load_module()
    pm_home, cwd = _snapshot_home(tmp_path)
    monkeypatch.setattr(mod, "REPO", pm_home)
    probe_calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        if argv[:1] != ["git"]:
            pytest.fail(f"장부 조회가 subprocess 를 spawn 함(spawn 0 계약 위반): {argv!r}")
        assert "--no-optional-locks" in argv, "WIP 프로브는 --no-optional-locks 필수"
        assert argv[-2:] == ["status", "--porcelain"]
        timeout = kwargs.get("timeout")
        assert timeout is not None and 0 < timeout <= mod._WIP_PROBE_TIMEOUT_SECONDS
        probe_calls.append(argv)
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: not a git repository")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    args = SimpleNamespace(cwd=str(cwd), state_lines=24, json=True)
    assert mod.cmd_snapshot(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["suppressOutput"] is False
    assert payload["systemMessage"].startswith(mod._SNAPSHOT_IDENTITY_HEADING)
    assert "조회 불가" in payload["systemMessage"]  # WIP 프로브가 fail-soft 로 표기됐는지
    assert 0 < len(probe_calls) <= mod._WIP_PROBE_MAX_CALLS


def test_snapshot_missing_all_ledgers_is_fail_soft_one_line_warning(tmp_path):
    mod = _load_module()
    pm_home = tmp_path / "empty"
    pm_home.mkdir()
    text, warning = mod.build_snapshot(pm_home, pm_home)
    assert text is None
    assert warning and "\n" not in warning and "재주입 생략" in warning


# ── cmd_archive (파괴적 — tmp_path 에서만) ────────────────────────────────────

def test_cmd_archive_seals_old_keeps_recent(tmp_path, monkeypatch, capsys):
    """--before cutoff 미만 entry 를 archive 파일로 봉인하고 current.md 엔 잔여만 남긴다."""
    mod = _load_module()
    log_dir, archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    write_lf(current, _HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C)

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


def test_cmd_archive_preserves_crlf_bytes_in_kept_current(tmp_path, monkeypatch):
    """archive read/replace는 유니버설 개행 변환 없이 남은 current CRLF를 보존한다."""
    mod = _load_module()
    log_dir, _archive_dir = _redirect_paths(mod, monkeypatch, tmp_path)
    log_dir.mkdir(parents=True)
    current = log_dir / "current.md"
    original = (_HEADER + _ENTRY_A + _ENTRY_C).replace("\n", "\r\n").encode("utf-8")
    current.write_bytes(original)

    assert mod.cmd_archive(
        SimpleNamespace(before="2026-06-13", keep_last=None, dry_run=False)
    ) == 0

    expected = (_HEADER + _ENTRY_C).replace("\n", "\r\n").encode("utf-8")
    remaining = current.read_bytes()
    assert remaining == expected
    assert all(index > 0 and remaining[index - 1] == 0x0D
               for index, byte in enumerate(remaining) if byte == 0x0A)


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
    write_lf(current, _HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C)

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
    write_lf(current, _HEADER + _ENTRY_A + _ENTRY_B + _ENTRY_C)

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
