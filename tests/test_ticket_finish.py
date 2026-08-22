"""ticket_finish.py 직접 단위테스트 (T-0042·T-0103).

PM 세션 라이프사이클 자동화 — pytest 출력 파서(`parse_pytest_output`·`is_pytest_green`)·
per-repo 회귀 명령 해소(ADR-0014)·domain soft 알림(ADR-0018 #2)·전체 run() 흐름을 직접
검증한다. (T-0103/ADR-0023 a안: status.md 가 judgment-only 로 바뀌어 ticket_finish 는 더
이상 status.md 스칼라를 갱신하지 않는다 — `update_status`/`_replace_once`/앵커 정규식 제거.
대신 *완료 흐름이 status.md 를 전혀 안 건드림* 을 단언한다.)

도구는 패키지가 아니므로 importlib 동적 로드 (test_engine_smoke·test_pm_log 관용구).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TICKET_FINISH_PY = TOOLS / "ticket_finish.py"


def _load_module(name: str = "ticket_finish"):
    """ticket_finish 를 경로 로드한다 (도구는 패키지가 아니므로 importlib)."""
    spec = importlib.util.spec_from_file_location(name, TICKET_FINISH_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tf():
    return _load_module()


@pytest.fixture(autouse=True)
def _neutralize_misanchor_guard(tf, monkeypatch):
    """T-0345: 이 테스트 파일은 실 worktree 에서 도는데, `tf.main()` 을 부르는 테스트들이 새
    PM-홈 worktree 오실행 가드에 걸린다(실 worktree = 실 linked worktree → 가드 발화). 이 파일은
    가드가 아니라 main() 의 정체성/모호 게이트 등 orthogonal 로직을 검증하므로, 오실행 감지를
    None(미해소)로 격리해 가드를 통과시킨다. 가드 자체의 검증은 전용 파일
    `test_board_worktree_misanchor_guard.py` 가 담당한다."""
    monkeypatch.setattr(tf, "_pm_home_misanchor", lambda: None, raising=False)


# ── parse_pytest_output / is_pytest_green: 조합·rc 가드 ───────────────────────

def test_parse_pytest_output_passed_only(tf):
    assert tf.parse_pytest_output("402 passed in 8.74s") == (402, 0)


def test_parse_pytest_output_passed_and_deselected(tf):
    assert tf.parse_pytest_output("1472 passed, 24 deselected in 12.34s") == (1472, 24)


def test_parse_pytest_output_failed_and_passed(tf):
    """failed 가 있어도 passed/deselected 만 파싱 (red 판정은 호출 측)."""
    assert tf.parse_pytest_output("5 failed, 1467 passed, 24 deselected in 10s") == (1467, 24)


def test_parse_pytest_output_no_passed_returns_none(tf):
    assert tf.parse_pytest_output("collected 0 items / errored") is None


def test_is_pytest_green_passed_no_failed(tf):
    assert tf.is_pytest_green("402 passed in 8.74s", returncode=0) is True


def test_is_pytest_green_passed_with_deselected(tf):
    assert tf.is_pytest_green("1472 passed, 24 deselected in 1s", returncode=0) is True


def test_is_pytest_green_with_failed_is_red(tf):
    assert tf.is_pytest_green("5 failed, 1467 passed in 1s", returncode=1) is False


def test_is_pytest_green_nonzero_returncode_guards(tf):
    """rc != 0 이면 'N passed' 가 있어도 green 으로 오판하지 않는다 (인터럽트·부분 출력)."""
    assert tf.is_pytest_green("402 passed in 8.74s", returncode=2) is False


def test_is_pytest_green_no_passed_is_false(tf):
    assert tf.is_pytest_green("collected 0 items", returncode=0) is False


# ── 회귀 명령 per-repo 해소 (codex must-fix 1·ADR-0014·T-0730) ────────────────
#
# 활성 repo 가 비-Python 이거나 `tests/` 가 없으면 `pytest tests/ -q` 가 틀린다 — 회귀는 그
# repo 의 test_cmd 를 써야 한다. 해소는 4층이다(prefix 행 > repo 행 > local.conf > 솔로 폴백·
# 체인 본체는 pm_handoff `_resolve_gate_cmd` 단일 사본이고 이 도구는 자기 board seam 만 얹어
# 위임한다). 단 **솔로/프레임워크 자기 회귀(어느 층도 미해소)는 현행 `pytest tests/ -q` venv
# 실행을 반드시 보존**(도그푸딩 불변). 층별 우선순위·두 도구 파리티·무prefix 채택자 e2e 는
# `test_regression_gate_resolution.py` 가 소유하고, 여기서는 이 도구 표면의 계약을 본다.
# `tf` 모듈 전역(_load_board_module)을 재바인딩해 hermetic 하게 검증한다.


class _FakeBoard:
    """board.py 대역 — 해소 체인이 읽는 표면을 흉내낸다(areas 해소 가로채기).

    `areas_file()` = board_root 추종 결과(T-0162 A6) — `_resolve_per_repo_test_cmd` 의 존재
    가드가 board 모듈 함수로 위임되므로 대역도 그 함수를 제공한다(실재 areas.md 경로 주입).
    prefix 층 뒤에 repo 행·local.conf 층이 있으므로(T-0730) 그 표면도 대역이 갖는다 — 없으면
    체인이 AttributeError 로 조기 탈출해 "None 이 나온 이유"가 흐려진다.
    """

    def __init__(self, prefix, row, areas_path=None, *, rows=(), repos=(), conf=None):
        self._prefix = prefix
        self._row = row
        self._areas_path = areas_path or Path("/__nonexistent_areas__.md")
        self._rows = list(rows)
        self._repos = set(repos)
        self._conf = dict(conf or {})

    def areas_file(self):
        return self._areas_path

    def id_prefix(self):
        return self._prefix

    def _areas_row_for_prefix(self, prefix):
        return self._row if prefix == self._prefix else None

    def _parse_areas(self):
        return ([], list(self._rows))

    def session_name(self):
        return None

    def _repo_from_session(self, session):
        head, sep, tail = session.rpartition("_")
        if not sep or not head or not tail.isdigit():
            return None
        return head

    def registered_repos(self):
        return set(self._repos)

    def local_config(self):
        return dict(self._conf)


def test_resolve_per_repo_cmd_solo_no_areas_returns_none(tf, tmp_path, monkeypatch):
    """솔로 폴백 — areas.md 부재면 None(호출부가 현행 pytest argv 보존)."""
    monkeypatch.setattr(
        tf, "_load_board_module",
        lambda: _FakeBoard(None, None, areas_path=tmp_path / "no-areas.md"),
    )
    assert tf._resolve_per_repo_test_cmd() is None


def test_resolve_per_repo_cmd_multipm_returns_row_cmd(tf, tmp_path, monkeypatch):
    """multi-PM — areas.md 있고 활성 prefix 행에 test_cmd 있으면 그 문자열(예: go test)."""
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    monkeypatch.setattr(
        tf, "_load_board_module",
        lambda: _FakeBoard("A1", {"test_cmd": "go test ./..."}, areas_path=areas),
    )
    assert tf._resolve_per_repo_test_cmd() == "go test ./..."


def test_resolve_per_repo_cmd_multipm_no_prefix_returns_none(tf, tmp_path, monkeypatch):
    """multi-PM이라도 활성 prefix 없으면 None(솔로 폴백 — 현행 보존)."""
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    monkeypatch.setattr(
        tf, "_load_board_module", lambda: _FakeBoard(None, None, areas_path=areas))
    assert tf._resolve_per_repo_test_cmd() is None


def test_resolve_per_repo_cmd_multipm_empty_test_cmd_returns_none(tf, tmp_path, monkeypatch):
    """multi-PM이라도 행의 test_cmd 빈 값이면 None(부분 등록 — 현행 보존)."""
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    monkeypatch.setattr(
        tf, "_load_board_module",
        lambda: _FakeBoard("A1", {"test_cmd": ""}, areas_path=areas),
    )
    assert tf._resolve_per_repo_test_cmd() is None


def test_resolve_per_repo_cmd_board_load_failure_returns_none(tf, tmp_path, monkeypatch):
    """board import 실패(None)면 None — fail-soft 로 솔로 폴백(현행 보존)."""
    monkeypatch.setattr(tf, "_load_board_module", lambda: None)
    assert tf._resolve_per_repo_test_cmd() is None


def test_resolve_per_repo_cmd_blank_prefix_uses_repo_row(tf, tmp_path, monkeypatch):
    """무prefix 채택자 형상 — prefix 칼럼이 비어도 repo 행의 test_cmd 로 해소한다 (T-0730).

    현행 `pm-config repo add` 는 prefix 를 빈 값으로 등록하므로 `id_prefix()` 가 None 이다.
    prefix 층만 보면 올바른 test_cmd 를 담은 행에 도달하지 못해 하드코딩 pytest 로 폴백하고,
    `tests/` 가 없는 채택자는 회귀가 항상 red 로 오판된다.
    """
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    monkeypatch.setattr(
        tf, "_load_board_module",
        lambda: _FakeBoard(
            None, None, areas_path=areas,
            rows=[{"repo": "heru", "prefix": "", "test_cmd": "viewer bind"}],
            repos={"heru"},
        ),
    )
    assert tf._resolve_per_repo_test_cmd() == "viewer bind"


def test_resolve_per_repo_cmd_reads_local_conf_without_areas(tf, tmp_path, monkeypatch):
    """areas.md 가 아예 없어도 local.conf test_cmd 를 읽는다 (T-0730·3층은 areas 가드 밖)."""
    monkeypatch.setattr(
        tf, "_load_board_module",
        lambda: _FakeBoard(None, None, areas_path=tmp_path / "no-areas.md",
                           conf={"test_cmd": "go test ./..."}),
    )
    assert tf._resolve_per_repo_test_cmd() == "go test ./..."


def test_default_run_pytest_solo_uses_venv_pytest_argv(tf, tmp_path, monkeypatch):
    """솔로 회귀 보존 결정적 확인 — per-repo cmd 없으면 `[venv_python,-m,pytest,tests/,-q]` argv 실행.

    이게 깨지면 도그푸딩(현 repo·areas.md 없음) 자체가 깨진다 — must-fix 1 핵심 보존.
    """
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["shell"] = kw.get("shell", False)
        class R:
            returncode = 0
            stdout = "1 passed in 0.1s"
            stderr = ""
        return R()

    monkeypatch.setattr(tf, "_resolve_per_repo_test_cmd", lambda: None)
    monkeypatch.setattr(tf.subprocess, "run", fake_run)
    finisher = tf.TicketFinisher(venv_python="/venv/bin/python", regression_cwd="/repo")
    rc, out = finisher._default_run_pytest()
    assert rc == 0
    # 솔로 = venv pytest argv(리스트)·shell 미사용·tests/ 경로 보존.
    assert captured["cmd"] == ["/venv/bin/python", "-m", "pytest", "tests/", "-q"]
    assert captured["shell"] is False


def test_default_run_pytest_multipm_uses_per_repo_shell_cmd(tf, tmp_path, monkeypatch):
    """multi-PM 회귀 — per-repo cmd(예: go test)면 그 문자열을 shell 로 실행(활성 repo cwd).

    sensitivity: per-repo 해소를 무력화(None)하면 위 솔로 argv 로 떨어진다 → 해소가 살아있어야
    이 케이스가 성립(아래 assert 가 fail 재현).
    """
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["shell"] = kw.get("shell", False)
        captured["cwd"] = kw.get("cwd")
        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()

    monkeypatch.setattr(tf, "_resolve_per_repo_test_cmd", lambda: "go test ./...")
    monkeypatch.setattr(tf.subprocess, "run", fake_run)
    finisher = tf.TicketFinisher(venv_python="/venv/bin/python", regression_cwd="/wt/A_1")
    rc, out = finisher._default_run_pytest()
    assert rc == 0
    # multi-PM = per-repo 문자열 cmd·shell 실행·활성 repo cwd.
    assert captured["cmd"] == "go test ./..."
    assert captured["shell"] is True
    assert captured["cwd"] == "/wt/A_1"


# ── domain soft 알림 step (ADR-0018 #2·U2·비차단) ─────────────────────────────
#
# 완료 흐름에 *정보·비차단* step 을 더한다 — 영향받는 domain 페이지를 알리되 완료를
# 절대 막지 않는다(rc 불변). subprocess(pytest/board/git) 전부 DI 로 격리해 실 파일·
# board·git 미접촉으로 run() 전체 흐름을 hermetic 하게 돌린다. (T-0103/ADR-0023: status.md
# 는 더 이상 안 건드리므로 status_file 주입이 불요 — log_file 만 tmp 로 격리한다.)


def _green_pytest(passed: int = 100):
    """green pytest 대역 — (returncode 0, '<passed> passed …' 출력)."""
    return lambda: (0, f"{passed} passed, 12 deselected in 0.1s")


def _make_finisher(tf, tmp_path, *, affected):
    """run() 을 hermetic 하게 돌릴 TicketFinisher — 모든 subprocess·board DI.

    affected: _affected_domain_fn 대역(영향 (title, stale) 목록·[]·None). status.md 는
    이 도구가 안 건드린다(ADR-0023) → log_file 만 tmp 로 둔다.
    """
    log_file = tmp_path / "log.md"
    return tf.TicketFinisher(
        run_pytest_fn=_green_pytest(100),
        run_board_fn=lambda args: (0, "board ok"),
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "테스트 티켓",
        affected_domain_fn=lambda tid: affected,
        log_file=log_file,
    )


def test_soft_step_prints_affected_pages_and_does_not_block(tf, tmp_path, capsys):
    """영향 페이지가 있으면 soft 알림으로 출력하되 완료를 막지 않는다(rc 0)."""
    finisher = _make_finisher(
        tf, tmp_path, affected=[("분석 페이지", False), ("코어 페이지", None)]
    )
    rc = finisher.run("T-1234", section=None, dry_run=False)
    assert rc == 0  # 완료 막지 않음
    out = capsys.readouterr().out
    assert "이 ticket 이 건드린 영역 domain 페이지" in out
    assert "분석 페이지" in out and "코어 페이지" in out
    assert "[완료] T-1234 기록 완료." in out  # 완료 흐름 도달


def test_soft_step_no_affected_pages_prints_none_and_completes(tf, tmp_path, capsys):
    """영향 0 → '(영향 domain 페이지 없음)' 한 줄·완료 진행(rc 0)."""
    finisher = _make_finisher(tf, tmp_path, affected=[])
    rc = finisher.run("T-1234", section=None, dry_run=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(영향 domain 페이지 없음)" in out
    assert "[완료] T-1234 기록 완료." in out


def test_soft_step_domain_absent_graceful_skip(tf, tmp_path, capsys):
    """domain 레이어 부재(None) → 조용히 skip·완료 진행(솔로/신규 clone 무영향·rc 0)."""
    finisher = _make_finisher(tf, tmp_path, affected=None)
    rc = finisher.run("T-1234", section=None, dry_run=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(domain 레이어 없음 — skip)" in out
    assert "[완료] T-1234 기록 완료." in out


def test_soft_step_exception_does_not_block_completion(tf, tmp_path, capsys):
    """soft 알림 함수가 예외를 던져도 완료를 막지 않는다(rc 0·비차단 계약)."""
    def boom(_tid):
        raise RuntimeError("domain 조회 폭발")

    finisher = tf.TicketFinisher(
        run_pytest_fn=_green_pytest(100),
        run_board_fn=lambda args: (0, "ok"),
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=boom,
        log_file=tmp_path / "log.md",
    )
    rc = finisher.run("T-1234", section=None, dry_run=False)
    assert rc == 0
    assert "[완료] T-1234 기록 완료." in capsys.readouterr().out


def test_dry_run_label_shows_resolved_gate(tf, tmp_path, monkeypatch, capsys):
    """dry-run 안내는 **실제로 돌 명령**을 보여준다 — 해소된 게이트가 있으면 그 문자열 (T-0730)."""
    monkeypatch.setattr(tf, "_resolve_per_repo_test_cmd", lambda: "go test ./...")
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (0, "ok\n"),
        run_board_fn=lambda args: (0, "board ok"),
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: [],
        log_file=tmp_path / "log.md",
    )
    rc = finisher.run("T-1234", section=None, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run] go test ./... 실행 중" in out
    assert "[dry-run] pytest tests/ -q 실행 중" not in out


def test_dry_run_label_shows_solo_argv_when_unresolved(tf, tmp_path, monkeypatch, capsys):
    """해소 실패(솔로)면 dry-run 안내도 현행 `pytest tests/ -q` 그대로(도그푸딩 불변)."""
    monkeypatch.setattr(tf, "_resolve_per_repo_test_cmd", lambda: None)
    finisher = _make_finisher(tf, tmp_path, affected=[])
    rc = finisher.run("T-1234", section=None, dry_run=True)
    assert rc == 0
    assert "[dry-run] pytest tests/ -q 실행 중" in capsys.readouterr().out


def test_soft_step_runs_in_dry_run_too(tf, tmp_path, capsys):
    """dry-run 에서도 soft 알림은 정보로 출력된다(편집만 생략·정보는 노출)."""
    finisher = _make_finisher(tf, tmp_path, affected=[("데모 페이지", False)])
    rc = finisher.run("T-1234", section=None, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "데모 페이지" in out
    assert "[dry-run] 완료" in out


# ── status judgment-only — ticket_finish 가 status.md 를 안 건드림 (ADR-0023 a안) ──
# T-0103: status.md = judgment-only 로 바뀌어 ticket_finish 는 status.md 스칼라 갱신 단계를
# 잃었다. 완료 흐름이 정상 완주하되 status.md(있어도) 를 전혀 안 건드림을 단언한다.

def test_status_update_machinery_removed(tf):
    """update_status·status_total_style·_replace_once·앵커 정규식이 제거됐다(ADR-0023)."""
    for sym in ("update_status", "status_total_style", "_replace_once",
                "_RE_HEADER", "_RE_REGRESSION", "_RE_TOTAL_ROW",
                "_RE_INLINE_SUBTOTAL", "_build_section_re", "STATUS_FILE"):
        assert not hasattr(tf, sym), f"{sym} 가 남아 있다 (ADR-0023 로 제거됐어야 함)"


def test_run_does_not_touch_status_md(tf, tmp_path, capsys):
    """완료 흐름이 정상 완주(rc 0)하되 status.md 파일을 *전혀* 안 건드린다 (judgment-only).

    실 status.md 위치를 tmp 로 바꿔(있어도) 도구가 그 파일을 읽지도 쓰지도 않는지 byte-동일로 단언.
    """
    status_file = tmp_path / "status.md"
    original = "# 현재 진행 상태\n\n| | mod | f.py | 🟡 | judgment-only 비고 |\n"
    status_file.write_text(original, encoding="utf-8")
    # 모듈 전역 STATUS_FILE 은 이미 제거됐다 — 더 monkeypatch 할 앵커가 없다는 게 핵심.
    finisher = _make_finisher(tf, tmp_path, affected=None)
    rc = finisher.run("T-1234", section=None, dry_run=False)
    assert rc == 0
    # status.md 는 byte-동일 (도구가 안 건드림).
    assert status_file.read_text(encoding="utf-8") == original
    out = capsys.readouterr().out
    assert "[완료] T-1234 기록 완료." in out
    # 단계 카운트가 5/5 로 줄었다 (status 단계 제거).
    assert "[1/5]" in out and "[5/5]" in out and "[6/6]" not in out


def test_run_ignores_section_arg(tf, tmp_path, capsys):
    """--section 은 후방호환 수용만·no-op — 줘도 status 건드림 없이 정상 완주(rc 0)."""
    finisher = _make_finisher(tf, tmp_path, affected=[])
    rc = finisher.run("T-1234", section="엔진 도구 (board.py)", dry_run=False)
    assert rc == 0
    assert "[완료] T-1234 기록 완료." in capsys.readouterr().out


def test_log_skeleton_reports_measured_total_no_old_total(tf):
    """log 스켈레톤은 실측 new_total 1줄만 — old_total/delta 박제 없음(ADR-0023·ADR-0008)."""
    skeleton = tf.build_log_skeleton(
        ticket_id="T-0001", title="t", new_total=42, board_before=1, board_after=2,
    )
    assert "회귀 42 / 42" in skeleton
    assert "board: done 1→2" in skeleton


def test_log_skeleton_adds_task_tag_when_task_is_resolved(tf):
    """task 완료 entry는 handoff와 같은 sentinel 태그를 헤더 말미에 생산한다."""
    skeleton = tf.build_log_skeleton(
        ticket_id="T-0547", title="박제", new_total=42, board_before=1, board_after=2,
        entry_type="feat", date="2026-08-06", task="orch-dev-T0547",
    )
    assert skeleton.splitlines()[0] == (
        "## [2026-08-06] feat | T-0547 — 박제 (task:orch-dev-T0547)"
    )


def test_log_skeleton_adds_canonical_slot_tag_when_session_is_resolved(tf):
    """slot 완료 entry는 handoff와 같은 canonical 세션 태그를 헤더 말미에 생산한다."""
    skeleton = tf.build_log_skeleton(
        ticket_id="T-0547", title="박제", new_total=42, board_before=1, board_after=2,
        entry_type="feat", date="2026-08-06", session="project_manager_2",
    )
    assert skeleton.splitlines()[0] == (
        "## [2026-08-06] feat | T-0547 — 박제 (project_manager_2)"
    )


def test_log_skeleton_without_task_is_byte_compatible(tf):
    """솔로처럼 task/slot 정체성이 미해소면 종전 헤더와 바이트 단위로 동일하다."""
    skeleton = tf.build_log_skeleton(
        ticket_id="T-0547", title="박제", new_total=42, board_before=1, board_after=2,
        entry_type="feat", date="2026-08-06",
    )
    assert skeleton.splitlines()[0] == "## [2026-08-06] feat | T-0547 — 박제"


def test_get_ticket_touches_non_list_returns_empty(tf, tmp_path):
    """get_ticket_touches — board 부재(spec 실패 가능)·touches 비-리스트 graceful []."""
    # 존재하지 않는 board_py → spec 은 만들어지나 exec 실패 → [].
    assert tf.get_ticket_touches(tmp_path / "nope-board.py", "T-0001") == []


# T-0397 — get_ticket_touches 의 board 로더가 로드한 board 모듈의 ENGINE_REV 를 실 엔진 rev 와
# 대조한다. fake board 대역도 유효 rev 스탬프를 지녀야 skew 로 오판되지 않는다(값은 실 engine_rev
# 에서 읽어 baked — 하드코딩·릴리즈 커플링 회피).
def _real_engine_rev() -> str:
    spec = importlib.util.spec_from_file_location("engine_rev", TOOLS / "engine_rev.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ENGINE_REV


def _write_fake_board(tmp_path, touches_repr):
    """조회 seam(find_ticket_exact)/load_ticket 만 정의하는 board.py 대역을 tmp 에 쓴다(hermetic)."""
    board_py = tmp_path / "fake_board.py"
    board_py.write_text(
        f"ENGINE_REV = {_real_engine_rev()!r}\n"  # T-0397 rev 스탬프
        "def find_ticket_exact(ticket_id, **kwargs):\n"
        "    return ('open', 'p')\n"
        "def load_ticket(path):\n"
        f"    return ({{'touches': {touches_repr}}}, 'body')\n",
        encoding="utf-8",
    )
    return board_py


def test_get_ticket_touches_strips_list_whitespace_and_drops_empty(tf, tmp_path):
    """get_ticket_touches — 리스트 원소 strip·빈/비-문자열 drop (silent-miss 방어·T-0081)."""
    board_py = _write_fake_board(tmp_path, "['src/a.py', ' src/b.py', '  ', 123]")
    assert tf.get_ticket_touches(board_py, "T-0001") == ["src/a.py", "src/b.py"]


def test_get_ticket_touches_strips_scalar_string(tf, tmp_path):
    """get_ticket_touches — 스칼라 문자열도 strip·빈 문자열 → []."""
    assert tf.get_ticket_touches(_write_fake_board(tmp_path, "'  src/x.py '"), "T") == ["src/x.py"]
    assert tf.get_ticket_touches(_write_fake_board(tmp_path, "'   '"), "T") == []


def test_affected_domain_titles_domain_absent_returns_none(tf, tmp_path, monkeypatch):
    """affected_domain_titles — domain.py 부재(_load_domain_module None) → None(skip)."""
    monkeypatch.setattr(tf, "_load_domain_module", lambda: None)
    assert tf.affected_domain_titles("T-0001", tf.BOARD_PY) is None


def test_affected_domain_titles_uses_pages_for_touches(tf, tmp_path, monkeypatch):
    """affected_domain_titles — domain.pages_for_touches 재사용·(title, stale) 목록 반환."""
    class _FakeDomain:
        @staticmethod
        def load_pages():
            return [{"path": "p", "title": "x", "covers": ["src/**"]}]

        @staticmethod
        def pages_for_touches(touches, pages):
            return [{"title": "영향페이지"}] if touches else []

        @staticmethod
        def _real_git_runner(repo):
            return lambda argv: (0, "")

        @staticmethod
        def page_stale(page, *, git_runner=None):
            return False  # fresh — soft step 무표시.

    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    assert tf.affected_domain_titles("T-0001", tf.BOARD_PY) == [("영향페이지", False)]


def test_affected_domain_titles_normalizes_pm_home_worktree_touch(
        tf, tmp_path, monkeypatch):
    """handoff 완료 뒤 idle slot도 지속 소유 매핑으로 정규화해 soft 알림 recall을 보존한다."""
    slot = "work/project_manager_1"
    (tmp_path / slot).mkdir(parents=True)
    ledger = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"leases":[{"slot":"work/project_manager_1","repo":"project_manager",'
        '"session":"","state":"idle"}]}',
        encoding="utf-8",
    )
    seen = []

    class _FakeDomain:
        @staticmethod
        def load_pages():
            return []

        @staticmethod
        def pages_for_touches(touches, pages):
            seen.extend(touches)
            return [{"title": "완료 좌표 페이지"}]

        @staticmethod
        def _real_git_runner(repo):
            return lambda argv: (0, "")

        @staticmethod
        def page_stale(page, *, git_runner=None):
            return False

    monkeypatch.setattr(tf, "REPO", tmp_path)
    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(
        tf,
        "get_ticket_touches",
        lambda bp, tid: [f"{slot}/src/x.py"],
    )

    assert tf.affected_domain_titles("T-0473", tf.BOARD_PY) == [
        ("완료 좌표 페이지", False),
    ]
    assert seen == ["src/x.py"]


def test_affected_domain_titles_bad_coordinate_warns_per_path_and_continues(
        tf, tmp_path, monkeypatch, capsys):
    """read-only soft 알림은 한 경로의 귀속 오류 때문에 유효한 나머지를 버리지 않는다."""
    registered = "work/project_manager_1"
    (tmp_path / registered).mkdir(parents=True)
    ledger = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"leases":[{"slot":"work/project_manager_1","repo":"project_manager",'
        '"session":"","state":"idle"}]}',
        encoding="utf-8",
    )
    seen = []

    class _FakeDomain:
        @staticmethod
        def load_pages():
            return []

        @staticmethod
        def pages_for_touches(touches, pages):
            seen.extend(touches)
            return [{"title": "남은 페이지"}] if "src/valid.py" in touches else []

        @staticmethod
        def _real_git_runner(repo):
            return lambda argv: (0, "")

        @staticmethod
        def page_stale(page, *, git_runner=None):
            return False

    monkeypatch.setattr(tf, "REPO", tmp_path)
    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(
        tf, "get_ticket_touches",
        lambda bp, tid: ["work/project_manager_2/src/bad.py", "src/valid.py"],
    )

    assert tf.affected_domain_titles("T-0473", tf.BOARD_PY) == [
        ("남은 페이지", False),
    ]
    assert seen == ["src/valid.py"]
    err = capsys.readouterr().err
    assert "좌표 정규화 경고" in err and "slot 불일치" in err


def test_affected_domain_titles_nonprefixed_touches_skip_coordinate_loader(
        tf, monkeypatch):
    """접두 없는 soft 조회는 부분 동기 adopter에서 normalizer를 요구하지 않는다."""
    class _FakeDomain:
        @staticmethod
        def load_pages():
            return []

        @staticmethod
        def pages_for_touches(touches, pages):
            return []

    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    monkeypatch.setattr(
        tf, "_load_repo_coordinates",
        lambda: (_ for _ in ()).throw(AssertionError("normalizer를 로드하면 안 됨")),
    )
    assert tf.affected_domain_titles("T-0001", tf.BOARD_PY) == []


def test_affected_domain_titles_empty_touches_skips_load_pages(tf, monkeypatch):
    """touches 가 비면 load_pages 스캔(깨진 페이지 warning) 전에 [] 조기 반환 (T-0081 후속)."""
    calls = {"load_pages": 0}

    class _FakeDomain:
        @staticmethod
        def load_pages():
            calls["load_pages"] += 1
            return []

        @staticmethod
        def pages_for_touches(touches, pages):  # 호출되면 안 됨
            raise AssertionError("빈 touches 인데 pages_for_touches 호출됨")

    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: [])
    assert tf.affected_domain_titles("T-0001", tf.BOARD_PY) == []
    assert calls["load_pages"] == 0  # load_pages 스캔 자체를 건너뜀


# ── soft step stale ⚠ (T-0082 재작업 · ADR-0018 #3) ──────────────────────────


def test_soft_step_marks_stale_affected_page_with_warning(tf, tmp_path, capsys):
    """stale(True) 영향 페이지 줄 앞에 ⚠ — fresh/unknown 무표시·완료 막지 않음(rc 0)."""
    finisher = _make_finisher(
        tf, tmp_path,
        affected=[("낡은 페이지", True), ("fresh 페이지", False), ("unknown 페이지", None)],
    )
    rc = finisher.run("T-1234", section=None, dry_run=False)
    assert rc == 0  # stale 도 완료를 막지 않는다(visibility·비차단).
    out = capsys.readouterr().out
    assert "⚠ 낡은 페이지" in out          # stale True → ⚠ 동반
    assert "⚠ fresh 페이지" not in out      # fresh(False) → 무표시
    assert "⚠ unknown 페이지" not in out    # unknown(None) → 무표시
    assert "fresh 페이지" in out and "unknown 페이지" in out  # 이름 자체는 출력
    assert "[완료] T-1234 기록 완료." in out


def test_affected_domain_titles_stale_exception_yields_unknown(tf, tmp_path, monkeypatch):
    """page_stale 이 예외를 던지면 그 페이지 stale=None(무표시)·비차단(목록은 정상 반환)."""
    class _FakeDomain:
        @staticmethod
        def load_pages():
            return [{"path": "p", "title": "x", "covers": ["src/**"]}]

        @staticmethod
        def pages_for_touches(touches, pages):
            return [{"title": "폭발 페이지"}]

        @staticmethod
        def _real_git_runner(repo):
            return lambda argv: (0, "")

        @staticmethod
        def page_stale(page, *, git_runner=None):
            raise RuntimeError("stale 산출 폭발")

    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    # 예외를 흡수해 stale=None 으로 surface — crash 0·목록 정상.
    assert tf.affected_domain_titles("T-0001", tf.BOARD_PY) == [("폭발 페이지", None)]


def test_affected_domain_titles_shares_single_git_runner(tf, tmp_path, monkeypatch):
    """git_runner 는 1회만 생성해 모든 영향 페이지의 page_stale 에 공유한다(reviewer suggestion)."""
    calls = {"runner_built": 0, "runners_seen": []}

    sentinel = object()

    class _FakeDomain:
        @staticmethod
        def load_pages():
            return []

        @staticmethod
        def pages_for_touches(touches, pages):
            return [{"title": "A"}, {"title": "B"}, {"title": "C"}]

        @staticmethod
        def _real_git_runner(repo):
            calls["runner_built"] += 1
            return sentinel

        @staticmethod
        def page_stale(page, *, git_runner=None):
            calls["runners_seen"].append(git_runner)
            return None

    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    result = tf.affected_domain_titles("T-0001", tf.BOARD_PY)
    assert result == [("A", None), ("B", None), ("C", None)]
    assert calls["runner_built"] == 1                       # 페이지 3개여도 runner 1회 생성
    assert calls["runners_seen"] == [sentinel, sentinel, sentinel]  # 같은 runner 공유


def test_affected_domain_titles_uses_each_page_owner_runner(tf, tmp_path, monkeypatch):
    """완료 soft-step의 date freshness도 페이지별 owner checkout runner를 쓴다."""
    owner = tmp_path / "owner"
    self_repo = tmp_path / "self"
    seen_repos = []
    seen_runners = []

    class _FakeBoard:
        @staticmethod
        def _freshness_owner_repo(repo):
            return ((owner if repo == "upstream" else self_repo), None)

    class _FakeDomain:
        @staticmethod
        def load_pages():
            return []

        @staticmethod
        def pages_for_touches(touches, pages):
            return [
                {"title": "외부", "repo": "upstream"},
                {"title": "내부", "repo": "self"},
            ]

        @staticmethod
        def _real_git_runner(repo):
            runner = object()
            seen_repos.append(Path(repo))
            return runner

        @staticmethod
        def page_stale(page, *, git_runner=None):
            seen_runners.append(git_runner)
            return False

    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(tf, "load_board_module", lambda _path: _FakeBoard())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    result = tf.affected_domain_titles("T-0001", tf.BOARD_PY)
    assert result == [("외부", False), ("내부", False)]
    assert seen_repos == [owner, self_repo]
    assert seen_runners[0] is not seen_runners[1]


def test_affected_domain_titles_unresolved_owner_skips_stale_soft_warning(
        tf, tmp_path, monkeypatch, capsys):
    """미해소 owner는 sentinel으로 stale fallback을 막아 soft 경고도 내지 않는다."""
    sentinel = object()

    class _FakeBoard:
        @staticmethod
        def _freshness_owner_repo(_repo):
            return None, "owner checkout unavailable"

    class _FakeDomain:
        _UNRESOLVED_GIT_RUNNER = sentinel

        @staticmethod
        def load_pages():
            return []

        @staticmethod
        def pages_for_touches(touches, pages):
            return [{"title": "미해소 외부", "repo": "upstream"}]

        @staticmethod
        def page_stale(page, *, git_runner=None):
            # None이면 domain.page_stale이 기본 runner로 fallback해 stale 오판하는 재현 조건.
            return None if git_runner is sentinel else True

    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(tf, "load_board_module", lambda _path: _FakeBoard())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    affected = tf.affected_domain_titles("T-0001", tf.BOARD_PY)

    assert affected == [("미해소 외부", None)]
    _make_finisher(tf, tmp_path, affected=affected)._notify_affected_domain("T-0001")
    assert "⚠ 미해소 외부" not in capsys.readouterr().out


def test_affected_domain_titles_legacy_board_does_not_absorb_upstream(
        tf, monkeypatch):
    """resolver 없는 legacy board에서도 `repo: upstream`을 REPO/self runner로 흡수하지 않는다."""
    seen = []

    class _LegacyBoard:
        pass

    class _FakeDomain:
        @staticmethod
        def load_pages():
            return []

        @staticmethod
        def pages_for_touches(touches, pages):
            return [{"title": "외부", "repo": "upstream"}]

        @staticmethod
        def _real_git_runner(repo):
            raise AssertionError(f"upstream을 legacy self runner로 흡수함: {repo}")

        @staticmethod
        def page_stale(page, *, git_runner=None):
            seen.append(git_runner)
            return None

    monkeypatch.setattr(tf, "_load_domain_module", lambda: _FakeDomain())
    monkeypatch.setattr(tf, "load_board_module", lambda _path: _LegacyBoard())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    assert tf.affected_domain_titles("T-0001", tf.BOARD_PY) == [("외부", None)]
    assert seen == [None]


# ── self-host 회귀 cwd 런타임 해소 (T-0149 — ② 홈서 worktree 회귀) ─────────────
#
# 분리된 PM 홈(②·ADR-0027)엔 tests/ 가 없으므로, ② 홈 cwd 에서 ticket_finish 를 돌리면
# 회귀가 활성 worktree 슬롯(①·tests/)에서 돌아야 한다. `__init__` 즉시-REPO-고정 버그를
# 제거하고 `_default_run_pytest` 가 런타임에 `_regression_cwd()` 로 해소한다. ticket_finish
# `_regression_cwd` wrapper 는 pm_handoff `_regression_cwd`(T-0124·bootstrap `_auto_slot`
# 동적로드 재사용)에 위임한다.
#
# seam 주입 주의: pm_handoff `_regression_cwd`·pm_bootstrap `_auto_slot` 의 `areas_file`/
# `leases_file` 은 **def-time 바운드 기본 인자**(실 worktree 경로)다 — 모듈 전역(`AREAS_FILE`)을
# setattr 로 rebind 해도 이미 캡처된 기본값을 못 바꾼다(가짜 seam). 그래서 `hp._regression_cwd`
# 를 areas/leases 를 *명시 인자로* 넘기는 lambda 로 감싸 주입한다(전역 rebind 대신). 또 REPO
# 자체가 `.../work/project_manager_1`(① worktree 에서 pytest 실행)이라 그 suffix 로 self-host
# 를 단언하면 REPO 폴백과 우연 일치한다 — 슬롯명을 `work/myrepo_7` 로 둬 구조적으로 구별한다.

import json as _selfhost_json  # noqa: E402 — T-0149 테스트 전용 로컬 import


def _write_selfhost_areas(path: Path, repos: list[str]) -> None:
    """areas.md (신 스키마·파이프 테이블) — repo 행을 repos 개수만큼. 빈 리스트면 헤더만."""
    lines = [
        "| repo | prefix | git | test_cmd | owner | base | protected |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in repos:
        lines.append(f"| {r} | {r} |  |  | alice |  |  |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_selfhost_leases(path: Path, entries: list[dict]) -> None:
    """worktree-leases.json — {"leases": [...]} 스키마 (worktree_pool.Lease.to_dict 동형)."""
    path.write_text(_selfhost_json.dumps({"leases": entries}), encoding="utf-8")


def _bind_pm_handoff_seams(tf, monkeypatch, areas: Path, leases: Path, repo_root: Path | None = None):
    """pm_handoff 를 로드하고 `_regression_cwd` 를 areas/leases(+repo_root) 명시 인자 lambda 로 감싸 주입.

    pm_handoff `_regression_cwd`(과 그 하부 `_auto_slot`)의 areas/leases 는 def-time 바운드
    기본 인자라 모듈 전역 setattr rebind 가 무효다(가짜 seam). 그래서 원본 `_regression_cwd`
    를 닫아 `worktree_slot` 만 받고 areas/leases/repo_root 를 *명시 인자로* 넘기는 lambda 로
    교체한다 — 이게 실 장부/areas/REPO 미접촉 hermetic 해소의 유효한 seam 이다. ticket_finish
    wrapper 는 `hp._regression_cwd(slot, repo_root=<자기 REPO>)` 로 호출하므로 lambda 도 그
    kwarg 를 받는다 — 테스트가 고정한 repo_root 가 있으면 그것이 우선이고(hermetic 앵커),
    없으면 wrapper 가 넘긴 앵커를 그대로 쓴다.
    """
    hp = tf._load_pm_handoff()
    assert hp is not None  # 동적 로드 성공 전제 (없으면 폴백 테스트로 분리)
    real = hp._regression_cwd
    monkeypatch.setattr(
        hp, "_regression_cwd",
        lambda worktree_slot=None, *, repo_root=None, _pinned=repo_root: real(
            worktree_slot, areas, leases,
            repo_root=_pinned if _pinned is not None else repo_root),
    )
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: hp)
    return hp


def test_regression_cwd_self_host_resolves_worktree_slot(tf, tmp_path, monkeypatch):
    """② 홈 cwd 모사 — 단일 self-host(areas 1 repo + 슬롯 1개) → work/<repo>_<N> 해소.

    이게 깨지면 ② 홈서 ticket_finish 회귀가 tests/ 없는 ② cwd 에서 돌아 "no tests ran" red.
    슬롯명을 `work/myrepo_7` 로 둬 REPO suffix(`work/project_manager_1`)와 구조적으로 구별한다 —
    REPO 폴백과 우연 일치하지 않아야 self-host 해소를 실제로 단언하는 가드가 된다. 자동해소도
    명시 슬롯과 같은 존재 검사를 타므로(장부-파일시스템 out-of-sync 대칭 처리) repo_root 를
    tmp_path 로 두고 그 슬롯 디렉토리를 실제로 만든다(hermetic·실 REPO 미접촉).
    """
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_selfhost_areas(areas, ["myrepo"])
    _write_selfhost_leases(leases, [
        {"slot": "work/myrepo_7", "repo": "myrepo",
         "session": "myrepo_7", "state": "leased"},
    ])
    (tmp_path / "work" / "myrepo_7").mkdir(parents=True)
    _bind_pm_handoff_seams(tf, monkeypatch, areas, leases, repo_root=tmp_path)
    result = tf._regression_cwd()
    assert result.replace(os.sep, "/").endswith("work/myrepo_7")
    assert result != str(tmp_path)  # repo_root 폴백과 구별


def test_regression_cwd_explicit_slot_overrides_auto(tf, tmp_path, monkeypatch):
    """명시 worktree_slot 우선 — auto 판정을 무시하고 그 슬롯 경로 반환.

    L1(ADR-0057 라이더 — stale 슬롯은 REPO 폴백)이 발동하지 않으려면 그 디렉토리가 실존해야
    하므로, `repo_root` 를 tmp_path 로 둬 `work/foo_2` 를 실제로 만든다.
    """
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_selfhost_areas(areas, ["myrepo"])
    _write_selfhost_leases(leases, [
        {"slot": "work/myrepo_7", "repo": "myrepo",
         "session": "myrepo_7", "state": "leased"},
    ])
    (tmp_path / "work" / "foo_2").mkdir(parents=True)
    _bind_pm_handoff_seams(tf, monkeypatch, areas, leases, repo_root=tmp_path)
    result = tf._regression_cwd("work/foo_2")
    assert result.replace(os.sep, "/").endswith("work/foo_2")


def test_regression_cwd_solo_falls_back_to_repo(tf, tmp_path, monkeypatch):
    """솔로(areas 부재·모호) → str(REPO) 폴백 (현행 100% 보존·additive)."""
    areas = tmp_path / "areas.md"           # 미생성 → 부재(솔로)
    leases = tmp_path / "worktree-leases.json"
    _write_selfhost_leases(leases, [
        {"slot": "work/myrepo_7", "repo": "myrepo",
         "session": "myrepo_7", "state": "leased"},
    ])
    _bind_pm_handoff_seams(tf, monkeypatch, areas, leases)
    assert tf._regression_cwd() == str(tf.REPO)


def test_regression_cwd_pm_handoff_absent_falls_back_to_repo(tf, monkeypatch):
    """pm_handoff 동적로드 실패(None) → str(REPO) 폴백 (자동해소 없이 안전·fail-soft)."""
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: None)
    assert tf._regression_cwd() == str(tf.REPO)


def test_default_run_pytest_resolves_cwd_at_runtime_when_not_injected(tf, monkeypatch):
    """regression_cwd 미주입 → _default_run_pytest 가 런타임 _regression_cwd() 로 해소.

    __init__ 즉시-고정 버그(REPO 박제) 제거 회귀 가드 — 미주입 시 self-host 슬롯이
    cwd 로 들어가야 한다(② 홈서 worktree 회귀). 명시 주입은 다음 두 테스트가 보존 검증.
    """
    captured = {}

    def fake_run(cmd, **kw):
        captured["cwd"] = kw.get("cwd")
        class R:
            returncode = 0
            stdout = "1 passed in 0.1s"
            stderr = ""
        return R()

    monkeypatch.setattr(tf, "_resolve_per_repo_test_cmd", lambda: None)
    monkeypatch.setattr(tf, "_regression_cwd", lambda: "/wt/project_manager_1")
    monkeypatch.setattr(tf.subprocess, "run", fake_run)
    finisher = tf.TicketFinisher(venv_python="/venv/bin/python")  # regression_cwd 미주입
    rc, _out = finisher._default_run_pytest()
    assert rc == 0
    assert captured["cwd"] == "/wt/project_manager_1"  # __init__ 박제 아니라 런타임 해소


# ── REPO 앵커 상향 탐색 (T-0242·finance_dev 제보 D2·board_root 동형) ──────────────
#
# 하드코딩 `REPO = Path(__file__).resolve().parents[2]` 는 tools 가 `<root>/.project_manager/
# tools/` 정확히 2단 깊이라고 가정한다 — 채택자 형상(PM 홈/worktree 구조 상이·다른 깊이)에선
# 어긋난다. `_find_repo_root()` 가 `.project_manager` 마커를 품은 첫(최근접) 조상으로 견고 해소
# 하고, 마커 부재 시 현행 `parents[2]` 로 폴백함을 hermetic 하게 단언한다 (external_review 동형).
#
# hermetic seam: 헬퍼는 모듈 전역 `__file__` 을 읽으므로 fresh 모듈 인스턴스의 `__file__` 을
# tmp 합성 경로로 monkeypatch 해 실제 파일 이동 없이 임의 깊이를 모사한다.


def test_find_repo_root_resolves_project_manager_ancestor(tf, tmp_path, monkeypatch):
    """채택자 형상(tools 가 다른 깊이) → REPO == `.project_manager` 를 품은 최근접 조상.

    tools 를 `<root>/.project_manager/tools/nested/` 에 두면 하드코딩 parents[2] 는
    `<root>/.project_manager/tools`(오답)를 준다 — 상향 탐색은 마커로 <root> 를 해소해야 한다."""
    root = tmp_path / "adopter"
    nested = root / ".project_manager" / "tools" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setattr(tf, "__file__", str(nested / "ticket_finish.py"))
    assert tf._find_repo_root() == root


def test_find_repo_root_returns_nearest_ancestor(tf, tmp_path, monkeypatch):
    """중첩 `.project_manager`(PM 홈 안 worktree·ADR-0027) → 최근접 조상을 반환한다(바깥 홈 아님)."""
    outer = tmp_path / "home"
    inner = outer / "work" / "wt1"
    (outer / ".project_manager").mkdir(parents=True)
    tools = inner / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    monkeypatch.setattr(tf, "__file__", str(tools / "ticket_finish.py"))
    assert tf._find_repo_root() == inner


def test_find_repo_root_falls_back_to_parents2_when_marker_absent(tf, tmp_path, monkeypatch):
    """마커 부재 → 현행 `parents[2]` 폴백(회귀 0·board_root 동형 graceful 폴백)."""
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    monkeypatch.setattr(tf, "__file__", str(deep / "ticket_finish.py"))
    assert tf._find_repo_root() == tmp_path / "a" / "b"


def test_find_repo_root_framework_shape_matches_parents2(tf):
    """프레임워크 형상(현 repo) → 상향 탐색 == 하드코딩 parents[2](REPO 상수 불변·additive)."""
    expected = Path(tf.__file__).resolve().parents[2]
    assert tf._find_repo_root() == expected
    assert tf.REPO == expected


# ── _default_python venv 우선 / 부재 폴백 (T-0242 — venv 부재 = 정상 채택자 경로) ────
#
# venv 후보가 존재하면 그대로 우선(도그푸딩 불변·회귀 측정 인터프리터 보존), 없으면
# sys.executable 폴백(venv 없는 채택자 = 정상 경로). 우선순위 불변을 두 갈래로 박제한다.


def test_default_python_venv_present_is_preferred(tf, tmp_path, monkeypatch):
    """venv 후보 존재 → venv 인터프리터 우선(도그푸딩 불변·회귀 인터프리터 보존)."""
    monkeypatch.setattr(tf, "REPO", tmp_path)
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    cand = tmp_path / "venv" / sub
    cand.parent.mkdir(parents=True)
    cand.write_text("", encoding="utf-8")
    assert tf._default_python() == str(cand)


def test_default_python_venv_absent_falls_back_to_sys_executable(tf, tmp_path, monkeypatch):
    """venv 부재(정상 채택자 경로) → sys.executable 폴백 — 에러 아님, 폴백 분기.

    시스템 인터프리터에 pytest 가 깔린 형상은 venv/ 를 안 만든다 → 그 환경 pytest 를 쓴다."""
    monkeypatch.setattr(tf, "REPO", tmp_path)  # tmp 아래 venv/ 없음
    assert tf._default_python() == tf.sys.executable


# ── 두-git 다중슬롯 seam — --repo/--slot/--no-pytest disambiguation (T-0285·ADR-0027·ADR-0057) ──
#
# ADR-0027 다중슬롯 형상(PM 홈 ② + worktree 슬롯 여럿)에선 `_regression_cwd(None)` 자동해소가
# 모호해져 ② 홈(tests/ 부재)으로 폴백 → 회귀 red("no tests ran") → finish 가 *조용히* 차단됐다
# (라이브 발견·PM 59차). `--repo`/`--slot`(ADR-0057 decomposed) 은 그 슬롯을 disambiguate 하고,
# `--no-pytest` 는 **회귀 실행만** skip 한다(코드 트리 해소는 그대로 — diff 서킷브레이커·stage 가
# 같은 트리를 쓴다). 다중슬롯인데 미지정+진짜 모호면 조용한 false-red 대신 fail-loud 로 중단한다.
# pm_handoff `_resolve_explicit_identity_slot`(M3 조인검증)·
# `_resolve_session_worktree_slot` 재사용(DRY).


def _bind_pm_handoff_repo(tf, monkeypatch, repo_root: Path):
    """pm_handoff 를 로드해 `REPO` 를 `repo_root` 로 monkeypatch 하고 그 인스턴스를 고정
    반환하도록 `tf._load_pm_handoff` 를 교체한다 — `_resolve_explicit_identity_slot`(M3 리스
    조인검증)·`_regression_cwd`(L1) 의 leases_file/repo_root 기본 해소가 이 tmp_path 를 보게
    만드는 hermetic seam(실 장부·실 REPO 미접촉). ticket_finish 의 `REPO` 도 같이 옮긴다 —
    해소 앵커를 wrapper 가 `repo_root=REPO` 로 forward 하므로, 한쪽만 옮기면 두 앵커가 갈린다."""
    hp = tf._load_pm_handoff()
    assert hp is not None
    monkeypatch.setattr(hp, "REPO", repo_root)
    monkeypatch.setattr(tf, "REPO", repo_root)
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: hp)
    return hp


class _FakeHandoff:
    """pm_handoff 대역 — ticket_finish glue(_resolve_finish_slot·main forward)를 hermetic 격리.

    `_resolve_explicit_identity_slot`/`_resolve_session_worktree_slot`/`_regression_cwd` 만
    흉내낸다(실 pm_bootstrap·areas/leases 장부 미접촉). `resolve_result` = **--repo/--slot 부재**
    시 `_resolve_session_worktree_slot(None)` 반환((slot,None)|(None,None)|(None,모호메시지)).
    `explicit_result` 미지정이면 `_resolve_explicit_identity_slot`(repo,slot)이 리스 검증 없이
    단순 조립(`work/<repo>_<slot 또는 1>`)한다 — explicit 은 모호 게이트를 우회한다는 성질만 흉내.
    """

    def __init__(self, resolve_result, explicit_result=None):
        self._resolve_result = resolve_result
        self._explicit_result = explicit_result

    def _resolve_explicit_identity_slot(self, repo, slot):
        if self._explicit_result is not None:
            return self._explicit_result
        if repo is None:
            return None, None
        return f"work/{repo}_{slot if slot is not None else 1}", None

    def _resolve_session_worktree_slot(self, worktree_slot=None):
        if worktree_slot:
            return worktree_slot, None  # explicit 우선(모호 게이트 우회).
        return self._resolve_result

    @staticmethod
    def _regression_cwd(worktree_slot=None, *, repo_root=None):
        # 실 wrapper 가 앵커(`repo_root=`)를 forward 한다 — 대역이 그 kwarg 를 안 받으면
        # TypeError 가 fail-soft 폴백으로 삼켜져 해소 자체가 검증되지 않는다.
        base = str(repo_root) if repo_root is not None else "/fake-repo"
        return f"{base}/{worktree_slot}" if worktree_slot else base


def test_repo_slot_resolves_explicit_worktree_slot(tf, tmp_path, monkeypatch):
    """(a) --repo/--slot → explicit worktree 슬롯 `work/<repo>_<N>` 결정론적 해소.

    실 pm_handoff 를 쓰되 리스 장부 부재(`_bind_pm_handoff_repo` 의 tmp_path repo_root)라
    **M3**(세션↔repo 조인검증)는 검증불가·fail-soft(신뢰) — 결정론적. 회귀 cwd =
    REPO/<slot>(main 이 `_regression_cwd` forward) — L1(stale→REPO 폴백)이 발동하지 않게
    그 디렉토리를 실제로 만든다.
    """
    _bind_pm_handoff_repo(tf, monkeypatch, tmp_path)
    (tmp_path / "work" / "project_manager_1").mkdir(parents=True)
    slot, err = tf._resolve_finish_slot("project_manager", 1)
    assert err is None
    assert slot == "work/project_manager_1"
    cwd = tf._regression_cwd("work/project_manager_1")
    assert cwd.replace(os.sep, "/").endswith("work/project_manager_1")


def test_repo_alone_bypasses_ambiguity(tf, monkeypatch):
    """--repo(+--slot) 명시는 자동해소 모호 여부와 무관하게 그 슬롯 사용(explicit 우선·모호 게이트 우회).

    이게 분해형 `--repo`/`--slot` 의 존재 이유 — 다중슬롯 모호를 사용자가 직접 disambiguate 한다.
    `resolve_result`(--repo/--slot 부재 경로용)가 모호를 반환해도 explicit 경로는 그와 무관하다.
    """
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: _FakeHandoff((None, "모호!")))
    slot, err = tf._resolve_finish_slot("project_manager", 2)
    assert err is None            # 명시했으므로 모호 아님(short-circuit)
    assert slot == "work/project_manager_2"


# ── M3(라이더) — 세션↔repo 조인 불일치 fail-loud (pm_handoff `_resolve_explicit_identity_slot` 위임) ──

def test_resolve_finish_slot_m3_rejects_repo_join_mismatch(tf, tmp_path, monkeypatch):
    """명시 --repo/--slot 이 실제 활성 리스와 조인 불일치 → fail-loud(조용히 안 넘김·ADR-0057 M3)."""
    _bind_pm_handoff_repo(tf, monkeypatch, tmp_path)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True)
    leases.write_text(
        '{"leases": [{"slot": "work/myrepo_1", "repo": "myrepo", "session": "myrepo_1", '
        '"state": "leased"}]}',
        encoding="utf-8",
    )
    slot, err = tf._resolve_finish_slot("myrepo", 99)
    assert slot is None
    assert err is not None and "M3" in err and "조인" in err


def test_resolve_finish_slot_repo_alone_resolves_single_active(tf, tmp_path, monkeypatch):
    """--repo 단독 + 그 repo 활성 슬롯 정확히 1개 → actor 자동해소(ADR-0057 결정 3)."""
    _bind_pm_handoff_repo(tf, monkeypatch, tmp_path)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True)
    leases.write_text(
        '{"leases": [{"slot": "work/myrepo_3", "repo": "myrepo", "session": "myrepo_3", '
        '"state": "leased"}]}',
        encoding="utf-8",
    )
    assert tf._resolve_finish_slot("myrepo", None) == ("work/myrepo_3", None)


def test_run_skip_pytest_bypasses_regression_keeps_tests_pass(tf, tmp_path, capsys):
    """(b) --no-pytest → [1/5] 회귀 skip(pytest 미호출)·board complete 는 --tests-pass 유지·rc 0.

    log 스켈레톤 측정 total 은 "?" 로 렌더(PM 이 /pm-qa 별도 측정치로 채움). 회귀 fn 이 호출되면
    AssertionError 로 실패해 skip 을 결정론적으로 단언한다.
    """
    board_calls: list[list[str]] = []

    def boom_pytest():
        raise AssertionError("skip_pytest=True 인데 회귀가 실행됨")

    def spy_board(args):
        board_calls.append(args)
        return 0, "board ok"

    finisher = tf.TicketFinisher(
        run_pytest_fn=boom_pytest,
        run_board_fn=spy_board,
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "테스트 티켓",
        affected_domain_fn=lambda tid: None,
        log_file=tmp_path / "log.md",
    )
    rc = finisher.run("T-1234", section=None, dry_run=False, skip_pytest=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "회귀 측정 skip" in out
    assert board_calls == [["complete", "T-1234", "--tests-pass"]]  # skip 이어도 --tests-pass 유지
    assert "[완료] T-1234 기록 완료." in out
    assert "회귀 ? / ?" in (tmp_path / "log.md").read_text(encoding="utf-8")  # 측정 total 없이 "?"


def test_main_multislot_ambiguous_fails_loud_before_regression(tf, monkeypatch, capsys):
    """(c) 다중슬롯 모호 + --repo/--slot 부재 → 친화 에러·rc 1·회귀 미시작(조용한 ② false-red 제거).

    fail-loud 는 run() 진입 *전*이라 회귀가 시작조차 안 된다([1/5] 미출력). 처방은 `--repo
    <name> [--slot <N>]` 또는 `--task <이름>` 이다 — 회귀 skip 은 처방이 아니다(같은 코드 트리를
    diff 서킷브레이커·stage 가 함께 쓴다).
    """
    monkeypatch.setattr(
        tf, "_load_pm_handoff",
        lambda: _FakeHandoff((None, "등록 repo 2개(A, B) — 어느 repo 인지 모호하다.")),
    )
    rc = tf.main(["T-1234"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "모호" in captured.err
    assert "--repo" in captured.err and "--task" in captured.err
    assert "--no-pytest" not in captured.err   # 회귀 skip 은 트리 해소 우회 수단이 아니다
    assert "[1/5]" not in captured.out  # run()/회귀 진입 전 중단


def test_main_repo_slot_forwards_resolved_regression_cwd(tf, monkeypatch):
    """--repo/--slot (회귀 실행 경로) → main 이 해소된 worktree 를 regression_cwd forward."""
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: _FakeHandoff((None, None)))
    captured: dict = {}

    class _SpyFinisher(tf.TicketFinisher):
        def __init__(self, **kw):
            captured["regression_cwd"] = kw.get("regression_cwd")
            super().__init__(**kw)

        def run(self, **kw):
            captured["skip_pytest"] = kw.get("skip_pytest")
            captured["session"] = kw.get("session")
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)
    rc = tf.main(["T-1234", "--repo", "project_manager", "--slot", "1"])
    assert rc == 0
    assert captured["regression_cwd"].replace(os.sep, "/").endswith("work/project_manager_1")
    assert captured["skip_pytest"] is False
    assert captured["session"] == "project_manager_1"


# ── board 정체성 forward (T-0781) ───────────────────────────────────────────
#
# board 의 complete 는 `claimed_by` 와 실행 세션을 대조한다. ticket_finish 는 자기 정체성을
# 이미 해소해 두고도 board 에 넘기지 않았다 — 그 상태로 소유 게이트가 켜지면 다중 슬롯
# 채택자의 `/pm-wave-finish` 주 경로가 "남의 티켓" 으로 전부 거부된다.

def test_run_forwards_board_identity_args_to_complete(tf, tmp_path):
    """run(board_identity_args=…) → 내부 board complete argv 끝에 그 인자가 실린다(argv 캡처)."""
    board_calls: list[list[str]] = []
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (0, "1 passed"),
        run_board_fn=lambda args: (board_calls.append(args), (0, "board ok"))[1],
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "테스트 티켓",
        affected_domain_fn=lambda tid: None,
        stage_scope_fn=lambda tid: tf.StageScope((), None),
        status_entries_fn=lambda: (),
        dod_block_fn=lambda tid: None,
        log_file=tmp_path / "log.md",
    )

    rc = finisher.run("T-1234", section=None, dry_run=False, skip_pytest=True,
                      session="pay_2", board_identity_args=["--repo", "pay", "--slot", "2"])

    assert rc == 0
    assert board_calls == [
        ["complete", "T-1234", "--tests-pass", "--repo", "pay", "--slot", "2"]]


def test_main_repo_slot_forwards_identity_to_board_complete(tf, monkeypatch):
    """`--repo/--slot` 으로 부른 ticket_finish 는 그 좌표를 board complete 로 forward 한다."""
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: _FakeHandoff((None, None)))
    captured: dict = {}

    class _SpyFinisher(tf.TicketFinisher):
        def run(self, **kw):
            captured.update(kw)
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)

    assert tf.main(["T-1234", "--repo", "project_manager", "--slot", "1"]) == 0
    assert captured["board_identity_args"] == [
        "--repo", "project_manager", "--slot", "1"]


def test_main_without_identity_keeps_the_bare_complete_argv(tf, monkeypatch):
    """정체성 인자가 없으면 forward 도 없다 — board 의 기존 해소 체인(env·단일 lease)이 돈다."""
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: _FakeHandoff((None, None)))
    captured: dict = {}

    class _SpyFinisher(tf.TicketFinisher):
        def run(self, **kw):
            captured.update(kw)
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)

    assert tf.main(["T-1234"]) == 0
    assert captured["board_identity_args"] == []


@pytest.mark.parametrize("identity, slot_identity, expected", [
    (SimpleNamespace(repo="pay", slot=2, task=None), None,
     ["--repo", "pay", "--slot", "2"]),
    (SimpleNamespace(repo="pay", slot=None, task=None),
     SimpleNamespace(repo="pay", number=3), ["--repo", "pay", "--slot", "3"]),
    (SimpleNamespace(repo="pay", slot=None, task=None), None, ["--repo", "pay"]),
    (SimpleNamespace(repo=None, slot=None, task=None), None, []),
])
def test_board_identity_argv_prefers_explicit_then_ledger_coordinates(
        tf, identity, slot_identity, expected):
    """명시 좌표 > 장부 좌표 > repo 단독 > 없음 — 좌표는 장부 행에서 온다(키 문자열을 안 뜯는다)."""
    assert tf._board_identity_argv(identity, slot_identity) == expected


def test_main_no_pytest_still_gates_ambiguity(tf, monkeypatch, capsys):
    """--no-pytest + 진짜 모호 → 슬롯 해소는 수행하고 rc 1 + --repo/--slot·--task 처방.

    옛 규칙은 `--no-pytest` 면 슬롯 해소 자체를 건너뛰었다 — 회귀 cwd 가 pytest 전용 값이던
    시점의 결정이다. 그 뒤 같은 해소 결과(코드 트리)를 diff 서킷브레이커·[4/5] stage·PM-direct
    재검이 소비하게 됐으므로, 회귀 skip 이 해소를 우회하면 그 소비자들이 PM 홈(분리 형상에선
    엔진 import 사본)을 잰다. 그래서 회귀 skip 과 트리 해소를 분리했고, 모호를 푸는 수단은
    `--repo/--slot`·`--task` 명시뿐이다.
    """
    resolve_calls: list = []

    def _spy_resolve(repo, slot):  # --no-pytest 여도 반드시 호출돼야 한다.
        resolve_calls.append((repo, slot))
        return None, "등록 repo 2개(A, B) — 어느 repo 인지 모호하다."

    monkeypatch.setattr(tf, "_resolve_finish_slot", _spy_resolve)
    captured: dict = {}

    class _SpyFinisher(tf.TicketFinisher):
        def __init__(self, **kw):
            captured["regression_cwd"] = kw.get("regression_cwd")
            super().__init__(**kw)

        def run(self, **kw):
            captured["skip_pytest"] = kw.get("skip_pytest")
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)
    rc = tf.main(["T-1234", "--no-pytest"])
    err = capsys.readouterr().err
    assert rc == 1                          # 모호는 회귀 skip 으로 풀리지 않는다
    assert resolve_calls == [(None, None)]  # 회귀 skip 여부와 무관하게 해소 수행
    assert captured == {}                   # TicketFinisher 생성 전에 중단(부작용 0)
    assert "--repo" in err and "--task" in err
    assert "--no-pytest" not in err         # 우회 처방 문구 없음


def test_main_no_pytest_forwards_resolved_code_tree(tf, monkeypatch):
    """--no-pytest + 슬롯 해소 성공 → 해소된 worktree 를 코드 트리로 forward(회귀만 skip).

    diff 서킷브레이커·stage 가 이 값을 쓰므로 회귀를 안 돌려도 주입돼야 한다.
    """
    monkeypatch.setattr(
        tf, "_load_pm_handoff",
        lambda: _FakeHandoff(("work/project_manager_1", None)),
    )
    captured: dict = {}

    class _SpyFinisher(tf.TicketFinisher):
        def __init__(self, **kw):
            captured["regression_cwd"] = kw.get("regression_cwd")
            super().__init__(**kw)

        def run(self, **kw):
            captured["skip_pytest"] = kw.get("skip_pytest")
            captured["session"] = kw.get("session")
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)
    rc = tf.main(["T-1234", "--no-pytest"])
    assert rc == 0
    assert captured["regression_cwd"].replace(os.sep, "/").endswith("work/project_manager_1")
    assert captured["skip_pytest"] is True   # [1/5] 회귀만 skip
    assert captured["session"] == "project_manager_1"


def test_main_no_pytest_solo_keeps_repo_fallback(tf, monkeypatch):
    """--no-pytest + 솔로/미해소 → 모호 아님·regression_cwd 미주입(현행 REPO 런타임 폴백 보존)."""
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: _FakeHandoff((None, None)))
    captured: dict = {}

    class _SpyFinisher(tf.TicketFinisher):
        def __init__(self, **kw):
            captured["regression_cwd"] = kw.get("regression_cwd")
            super().__init__(**kw)

        def run(self, **kw):
            captured["skip_pytest"] = kw.get("skip_pytest")
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)
    rc = tf.main(["T-1234", "--no-pytest"])
    assert rc == 0
    assert captured["regression_cwd"] is None
    assert captured["skip_pytest"] is True


def test_main_slot_without_repo_rejected(tf, capsys):
    """--slot 단독(--repo 없음) — identity_args.parse_identity 가 ValueError → parser.error(fail-loud).

    (pm_handoff `--slot` 단독 거부 동형 — ADR-0057 uniform 규칙.)
    """
    with pytest.raises(SystemExit):
        tf.main(["T-1234", "--slot", "4"])
    assert "--repo" in capsys.readouterr().err


def test_main_solo_forwards_no_regression_cwd(tf, monkeypatch):
    """(d) 솔로/미해소 → main 이 regression_cwd=None 으로 TicketFinisher 생성(런타임 _regression_cwd 폴백 보존)."""
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: _FakeHandoff((None, None)))
    captured: dict = {}

    class _SpyFinisher(tf.TicketFinisher):
        def __init__(self, **kw):
            captured["regression_cwd"] = kw.get("regression_cwd")
            super().__init__(**kw)

        def run(self, **kw):
            captured["skip_pytest"] = kw.get("skip_pytest")
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)
    rc = tf.main(["T-1234"])
    assert rc == 0
    assert captured["regression_cwd"] is None   # 솔로 미주입 → 런타임 폴백(현행 100% 보존)
    assert captured["skip_pytest"] is False


def test_resolve_finish_slot_solo_returns_none(tf, monkeypatch):
    """(d) 솔로(pm_handoff 자동해소 None) → (None, None)·모호 아님(fail-loud 미발동)."""
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: _FakeHandoff((None, None)))
    assert tf._resolve_finish_slot(None, None) == (None, None)


def test_resolve_finish_slot_pm_handoff_absent_fail_soft(tf, monkeypatch):
    """pm_handoff 부재(None) → 부재+무인자는 (None,None)·명시(repo+slot)는 단순 `work/` 조립
    (M3 리스 조인검증 불가하니 신뢰·현행 폴백 패턴·솔로 무변경). repo 단독은 M3 검증(actor 활성슬롯
    해소)이 pm_handoff 없인 불가하니 미해소(None,None)."""
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: None)
    assert tf._resolve_finish_slot(None, None) == (None, None)
    assert tf._resolve_finish_slot("myrepo", 2) == ("work/myrepo_2", None)
    assert tf._resolve_finish_slot("myrepo", None) == (None, None)


# ── engine_written_paths — rounds/T-NNNN/ stage 후보 (T-0753·ADR-0090 R5) ───────

class _FakeRoundsBoard:
    """`engine_written_paths` 전용 최소 board 대역 — 그 함수가 실제로 읽는 속성만 노출한다."""

    STATUS_DIRS = ("open", "claimed", "blocked", "done")

    def __init__(self, tickets_dir: Path, ticket_path: Path, *, board_git_enabled=False):
        self._tickets_dir = tickets_dir
        self._ticket_path = ticket_path
        self._board_git_enabled_value = board_git_enabled

    def tickets_dir(self) -> Path:
        return self._tickets_dir

    def find_ticket(self, ticket_id):
        return ("claimed", self._ticket_path)

    def _board_git_enabled(self) -> bool:
        return self._board_git_enabled_value


def test_engine_written_paths_includes_existing_round_files(tf, tmp_path):
    """rounds/<id>/ 아래 실존하는 라운드 파일이 stage 후보에 들어간다 (디렉터리 통째가 아니라
    파일 단위)."""
    tickets_dir = tmp_path / "tickets"
    ticket_path = tickets_dir / "claimed" / "T-0753.md"
    round_dir = tickets_dir / "rounds" / "T-0753"
    round_dir.mkdir(parents=True)
    dev_round = round_dir / "01-developer.md"
    review_round = round_dir / "02-code-reviewer.md"
    dev_round.write_text("dev\n", encoding="utf-8")
    review_round.write_text("review\n", encoding="utf-8")
    board = _FakeRoundsBoard(tickets_dir, ticket_path)

    paths = tf.engine_written_paths(board, "T-0753", tmp_path / "log.md")

    assert dev_round in paths
    assert review_round in paths
    assert round_dir not in paths  # 디렉터리 자체는 후보로 들어가지 않는다


def test_engine_written_paths_excludes_rounds_when_board_git_separated(tf, tmp_path):
    """board-git 분리 형상에선 티켓과 함께 라운드도 stage 후보에서 빠진다(현행 티켓 제외 규칙
    동일)."""
    tickets_dir = tmp_path / "tickets"
    ticket_path = tickets_dir / "claimed" / "T-0753.md"
    round_dir = tickets_dir / "rounds" / "T-0753"
    round_dir.mkdir(parents=True)
    (round_dir / "01-developer.md").write_text("dev\n", encoding="utf-8")
    board = _FakeRoundsBoard(tickets_dir, ticket_path, board_git_enabled=True)

    paths = tf.engine_written_paths(board, "T-0753", tmp_path / "log.md")

    assert paths == [tmp_path / "log.md"]


def test_engine_written_paths_excludes_non_round_and_temporary_files_by_grammar(
        tf, tmp_path):
    """라운드 판정은 `parse_round_filename` 문법 하나 — 점-접두 임시 잔여뿐 아니라 라운드가
    아닌 `.md`(`notes.md`·`01-dev.md`)도 stage 후보 밖이고, 진짜 라운드만 후보에 남는다
    (리뷰 F-002 — `.md` 접미 + 점-접두 제외 조합으로 되돌리면 `notes.md`·`01-dev.md`가 다시
    후보에 실려 이 테스트가 red 가 된다)."""
    tickets_dir = tmp_path / "tickets"
    ticket_path = tickets_dir / "claimed" / "T-0753.md"
    round_dir = tickets_dir / "rounds" / "T-0753"
    round_dir.mkdir(parents=True)
    dev_round = round_dir / "01-developer.md"
    dev_round.write_text("dev\n", encoding="utf-8")
    non_round_names = [
        ".01-developer.md.4242.deadbeef.tmp",  # 원자 교체 중간 산출(점-접두)
        "notes.md",                            # 라운드 아닌 무관 md
        "01-dev.md",                           # 역할 약칭(문법 밖)
    ]
    for name in non_round_names:
        (round_dir / name).write_text("x\n", encoding="utf-8")
    board = _FakeRoundsBoard(tickets_dir, ticket_path)

    paths = tf.engine_written_paths(board, "T-0753", tmp_path / "log.md")

    assert dev_round in paths
    for name in non_round_names:
        assert round_dir / name not in paths


def test_round_stage_candidates_uses_round_filename_grammar_not_ad_hoc_suffix(
        tf, tmp_path):
    """`_round_stage_candidates` 는 자체 접미/접두 조합이 아니라 `parse_round_filename` 문법
    단일 소유자에만 위임한다 (리뷰 F-001 — reviewer 실측: 9개 이름 배치 중 자체 조합은 4건을
    후보에 실렸는데 진짜 라운드는 2건뿐이었다)."""
    rounds_module = tf._load_ticket_rounds()
    tickets_dir = tmp_path / "tickets"
    round_dir = tickets_dir / "rounds" / "T-0753"
    round_dir.mkdir(parents=True)
    names = [
        "01-developer.md",                      # 진짜 라운드
        "02-code-reviewer.md",                  # 진짜 라운드
        "01-dev.md",                             # 역할 아님(약칭)
        "3-developer.md",                        # zero-pad 없음
        "001-developer.md",                      # 과잉 pad
        "01-developer.txt",                      # 확장자 아님
        "README.md",                             # 무관 md
        "notes.md",                              # 무관 md
        ".01-developer.md.4242.deadbeef.tmp",    # 원자 교체 중간 산출(점-접두)
    ]
    for name in names:
        (round_dir / name).write_text("x\n", encoding="utf-8")

    candidates = tf._round_stage_candidates(rounds_module, tickets_dir, "T-0753")

    assert candidates == [
        round_dir / "01-developer.md", round_dir / "02-code-reviewer.md",
    ]


def test_stage_scope_real_git_add_stages_untracked_new_round_file(tf, tmp_path):
    """untracked 신규 라운드 파일이 실 git repo 에서 `git add` 로 실제 stage 된다 (리뷰 F-005 —
    후보 산출뿐 아니라 real git 경로까지 왕복). board 대역은 조회 세 자리(`tickets_dir`·
    `find_ticket`·`_board_git_enabled`)만 대역하고, `git_scope_stage_pathspec` 자체는 실
    board.py 것을 그대로 빌려 real git 서브프로세스(status/ls-files/check-ignore)를 태운다."""
    repo = tmp_path / "repo"
    tickets_dir = repo / "tickets"
    ticket_path = tickets_dir / "claimed" / "T-0753.md"
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text("ticket\n", encoding="utf-8")
    round_dir = tickets_dir / "rounds" / "T-0753"
    round_dir.mkdir(parents=True)
    round_file = round_dir / "01-developer.md"
    round_file.write_text("dev round\n", encoding="utf-8")

    def git(args):
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        return result.returncode, result.stdout

    assert git(["init", "-q"])[0] == 0

    board_py = tmp_path / "real_round_stage_board.py"
    board_py.write_text(
        f"ENGINE_REV = {_real_engine_rev()!r}\n"
        "import importlib.util as _ilu\n"
        "from pathlib import Path as _Path\n"
        "_spec = _ilu.spec_from_file_location(\n"
        f"    '_t0753_real_board_for_round_stage_test', {str(TOOLS / 'board.py')!r})\n"
        "_real_board = _ilu.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_real_board)\n"
        "git_scope_stage_pathspec = _real_board.git_scope_stage_pathspec\n"
        f"STATUS_DIRS = {tf.load_board_module(tf.BOARD_PY).STATUS_DIRS!r}\n"
        f"_TICKET_PATH = _Path({str(ticket_path)!r})\n"
        f"_TICKETS_DIR = _Path({str(tickets_dir)!r})\n"
        "def tickets_dir():\n"
        "    return _TICKETS_DIR\n"
        "def find_ticket(ticket_id):\n"
        "    return ('claimed', _TICKET_PATH)\n"
        "def _board_git_enabled():\n"
        "    return False\n",
        encoding="utf-8",
    )

    scope = tf.stage_scope(
        "T-0753", board_py, repo / "log.md", git,
        repo=repo, include_touches=False,
    )
    assert scope.error is None
    round_rel = round_file.relative_to(repo).as_posix()
    assert round_rel in scope.pathspec

    rc, _ = git(["add", "-A", "--", *scope.pathspec])
    assert rc == 0
    rc, staged = git(["diff", "--cached", "--name-only"])
    assert rc == 0
    assert round_rel in staged.splitlines()


# ── task-mode two-git stage plan (T-0437) ───────────────────────────────────

def test_task_stage_plan_separates_pm_outputs_and_worktree_touches(tf, tmp_path, monkeypatch, capsys):
    """F6 task worktree touches와 PM-home 산출물은 cwd/pathspec을 섞지 않고 각각 stage/status 한다."""
    home = tmp_path / "pm-home"
    worktree = home / "work" / "project_manager_1"
    worktree.mkdir(parents=True)
    (home / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    monkeypatch.setattr(tf, "REPO", home)
    calls = []

    def fake_scope(ticket_id, board_py, log_file, run_git, *, repo=None,
                   include_touches=True, include_engine_outputs=True,
                   touches_workspace=None, touches=None):
        repo = Path(repo or home)
        calls.append((repo, include_touches, include_engine_outputs))
        if include_engine_outputs:
            return tf.StageScope((".project_manager/wiki/log/current.md",), None)
        return tf.StageScope(("tests/test_real_change.py",), None)

    monkeypatch.setattr(tf, "stage_scope", fake_scope)
    home_git, worktree_git, status_calls = [], [], []
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("--no-pytest must skip")),
        run_board_fn=lambda args: (0, "board ok"),
        run_git_fn=lambda args: (home_git.append(args) or (0, "")),
        run_git_at_fn=lambda cwd, args: (worktree_git.append((cwd, args)) or (0, "")),
        board_count_fn=lambda: 0,
        ticket_title_fn=lambda ticket_id: "two git",
        affected_domain_fn=lambda ticket_id: None,
        status_entries_fn=lambda: (status_calls.append(("home", home)) or
                                   (("M ", ".project_manager/wiki/log/current.md"),)),
        status_entries_at_fn=lambda cwd: (status_calls.append(("task", cwd)) or
                                          (("M ", "others.py"), (" M", "missed.py"))),
        log_file=home / ".project_manager/wiki/log/current.md",
        task_workspace=worktree,
    )
    assert finisher.run("T-0437", section=None, dry_run=False, skip_pytest=True) == 0
    assert calls == [
        (home, False, True),
        (worktree, True, False),
    ]
    assert home_git == [["add", "-A", "--", ".project_manager/wiki/log/current.md"]]
    assert worktree_git == [(worktree, ["add", "-A", "--", "tests/test_real_change.py"])]
    assert status_calls == [("home", home), ("task", worktree)]
    out = capsys.readouterr().out
    assert f"[PM 홈 산출물] cwd={home}" in out
    assert f"[task worktree touches] cwd={worktree}" in out
    assert (f"[PM 홈 산출물] cwd={home}: "
            '`git commit -m "<메시지>" -- .project_manager/wiki/log/current.md`') in out
    assert (f"[task worktree touches] cwd={worktree}: "
            '`git commit -m "<메시지>" -- tests/test_real_change.py`') in out
    assert "⚠ [task worktree touches] 미스테이지 잔여 1건" in out
    assert "⚠ [task worktree touches] 스코프 밖 staged 1건" in out
    assert "[완료] T-0437 기록 완료. ⚠ 미스테이지 잔여 1건 · 스코프 밖 staged 1건" in out


def test_task_stage_plan_dry_run_matches_actual_repo_pathspec(tf, tmp_path, monkeypatch, capsys):
    """dry-run도 actual과 같은 두 repo cwd/pathspec을 출력하고 git mutation은 하지 않는다."""
    home = tmp_path / "pm-home"
    worktree = home / "work" / "project_manager_1"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(tf, "REPO", home)

    def fake_scope(ticket_id, board_py, log_file, run_git, *, repo=None,
                   include_touches=True, include_engine_outputs=True,
                   touches_workspace=None, touches=None):
        return tf.StageScope((("home.md",) if include_engine_outputs else ("code.py",)), None)

    monkeypatch.setattr(tf, "stage_scope", fake_scope)
    mutations = []
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (0, "1 passed in 0.1s"),
        run_board_fn=lambda args: (0, ""),
        run_git_fn=lambda args: (mutations.append(args) or (0, "")),
        run_git_at_fn=lambda cwd, args: (mutations.append((cwd, args)) or (0, "")),
        board_count_fn=lambda: 0, ticket_title_fn=lambda tid: "x",
        affected_domain_fn=lambda tid: None, log_file=home / "log.md",
        task_workspace=worktree,
    )
    assert finisher.run("T-0437", section=None, dry_run=True, skip_pytest=True) == 0
    assert mutations == []
    out = capsys.readouterr().out
    assert f"cwd={home} git add -A -- home.md" in out
    assert f"cwd={worktree} git add -A -- code.py" in out
    assert f"[PM 홈 산출물] cwd={home}: `git commit -m \"<메시지>\" -- home.md`" in out
    assert f"[task worktree touches] cwd={worktree}: `git commit -m \"<메시지>\" -- code.py`" in out


def test_task_machine_parsers_use_stdout_only_not_mutation_stderr(tf, tmp_path, monkeypatch):
    """task worktree의 ls-files/status 파서는 stderr warning이 섞인 mutation seam을 절대 쓰지 않는다."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    parser_calls = []

    def mutation_runner(cwd, args):
        raise AssertionError(f"machine parser가 stderr-합본 mutation seam을 사용함: {args}")

    def stdout_runner(cwd, args):
        parser_calls.append((cwd, args))
        if args[0] == "status":
            return 0, "?? tests/new.py\0"
        return 0, "tests/new.py\0"

    def fake_scope(ticket_id, board_py, log_file, run_git, **kwargs):
        # stderr warning이 붙은 mutation output을 복원하면 이 runner가 아니라 mutation_runner가 호출돼 red.
        assert run_git(["ls-files", "-z"]) == (0, "tests/new.py\0")
        return tf.StageScope(("tests/new.py",), None)

    monkeypatch.setattr(tf, "stage_scope", fake_scope)
    monkeypatch.setattr(tf, "load_board_module", lambda board_py: None)
    finisher = tf.TicketFinisher(
        run_git_at_fn=mutation_runner,
        run_git_stdout_at_fn=stdout_runner,
        task_workspace=worktree,
    )

    assert finisher._code_stage_scope("T-0437", worktree) == tf.StageScope(("tests/new.py",), None)
    assert finisher._default_status_entries_at(worktree) == (("??", "tests/new.py"),)
    assert [args[0] for _, args in parser_calls] == ["ls-files", "status"]


def _write_coordinate_stage_board(tmp_path, touches: list[str]) -> Path:
    """touches와 file-only stage primitive를 제공하는 hermetic board 모듈."""
    path = tmp_path / "board_coordinate_stage.py"
    path.write_text(
        f"ENGINE_REV = {_real_engine_rev()!r}\n"
        "def find_ticket_exact(ticket_id, **kwargs):\n"
        "    return ('open', 'ticket')\n"
        "def load_ticket(path):\n"
        f"    return ({{'touches': {touches!r}}}, 'body')\n"
        "def git_scope_stage_pathspec(repo, declared, run_git=None):\n"
        "    return [p.relative_to(repo).as_posix() for p in declared if p.is_file()]\n",
        encoding="utf-8",
    )
    return path


def test_task_stage_scope_normalizes_pm_home_touch_and_stages_real_file(
        tf, tmp_path, monkeypatch):
    """task stage가 PM-home 접두를 제거해 worktree 안의 선언 파일을 실 pathspec으로 산출한다."""
    home = tmp_path / "pm-home"
    workspace = home / "work" / "project_manager_1"
    changed = workspace / "src" / "change.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("changed\n", encoding="utf-8")
    board_py = _write_coordinate_stage_board(
        tmp_path,
        ["work/project_manager_1/src/change.py"],
    )
    monkeypatch.setattr(tf, "REPO", home)
    finisher = tf.TicketFinisher(
        board_py=board_py,
        task_workspace=workspace,
        run_git_stdout_at_fn=lambda cwd, args: (0, ""),
    )

    assert finisher._code_stage_scope("T-0473", workspace) == tf.StageScope(("src/change.py",), None)


def test_task_stage_scope_worktree_slot_mismatch_fails_loud(
        tf, tmp_path, monkeypatch):
    """다른 slot 접두를 task workspace에 조용히 귀속하지 않고 stage error로 표면화한다."""
    home = tmp_path / "pm-home"
    workspace = home / "work" / "project_manager_1"
    workspace.mkdir(parents=True)
    board_py = _write_coordinate_stage_board(
        tmp_path,
        ["work/project_manager_2/src/change.py"],
    )
    monkeypatch.setattr(tf, "REPO", home)
    finisher = tf.TicketFinisher(
        board_py=board_py,
        task_workspace=workspace,
        run_git_stdout_at_fn=lambda cwd, args: (0, ""),
    )

    scope = finisher._code_stage_scope("T-0473", workspace)
    assert scope.pathspec == ()
    assert "좌표 정규화 실패" in scope.error
    assert "slot 불일치" in scope.error


@pytest.mark.parametrize(
    "touch",
    [
        r"work\project_manager_1\src\change.py",
        "./work/project_manager_1/src/change.py",
    ],
)
def test_task_stage_scope_normalizes_coordinate_notation_variants(
        tf, tmp_path, monkeypatch, touch):
    """stage 표면도 separator/leading-dot 표기를 같은 실 파일 좌표로 모은다."""
    home = tmp_path / "pm-home"
    workspace = home / "work" / "project_manager_1"
    changed = workspace / "src" / "change.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("changed\n", encoding="utf-8")
    board_py = _write_coordinate_stage_board(tmp_path, [touch])
    monkeypatch.setattr(tf, "REPO", home)
    finisher = tf.TicketFinisher(
        board_py=board_py,
        task_workspace=workspace,
        run_git_stdout_at_fn=lambda cwd, args: (0, ""),
    )
    assert finisher._code_stage_scope("T-0473", workspace) == tf.StageScope(("src/change.py",), None)


def test_task_stage_scope_rejects_traversal_fails_loud(
        tf, tmp_path, monkeypatch):
    home = tmp_path / "pm-home"
    workspace = home / "work" / "project_manager_1"
    workspace.mkdir(parents=True)
    board_py = _write_coordinate_stage_board(
        tmp_path, ["work/project_manager_1/../../outside.py"])
    monkeypatch.setattr(tf, "REPO", home)
    finisher = tf.TicketFinisher(
        board_py=board_py,
        task_workspace=workspace,
        run_git_stdout_at_fn=lambda cwd, args: (0, ""),
    )
    scope = finisher._code_stage_scope("T-0473", workspace)
    assert scope.pathspec == ()
    assert "traversal" in scope.error


@pytest.mark.parametrize(
    ("touch", "message"),
    [
        ("work/project_manager_1/.", "slot 전체"),
        ("work/project_manager_1//abs", "절대경로"),
    ],
)
def test_task_stage_scope_rejects_root_or_absolute_before_real_git_stage(
        tf, tmp_path, monkeypatch, touch, message):
    """위험 상대부는 실제 git repo의 stage pathspec에 들어가기 전에 fail-loud 한다."""
    home = tmp_path / "pm-home"
    workspace = home / "work" / "project_manager_1"
    workspace.mkdir(parents=True)
    (workspace / "root-change.py").write_text("untracked\n", encoding="utf-8")

    def git(args):
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.returncode, result.stdout

    assert git(["init", "-q"])[0] == 0
    monkeypatch.setattr(tf, "REPO", home)
    monkeypatch.setattr(tf, "get_ticket_touches", lambda *args: [touch])

    scope = tf.stage_scope(
        "T-0473",
        tf.BOARD_PY,
        home / "log.md",
        git,
        repo=workspace,
        include_engine_outputs=False,
        touches_workspace=workspace,
    )
    assert scope.pathspec == ()
    assert scope.error is not None
    assert "좌표 정규화 실패" in scope.error
    assert message in scope.error

    # 생산 경로와 같은 add 형식을 실행해도 거부 scope는 argv를 만들지 않으며 index는 비어 있다.
    if scope.pathspec:
        assert git(["add", "-A", "--", *scope.pathspec])[0] == 0
    rc, staged = git(["diff", "--cached", "--name-only"])
    assert rc == 0
    assert staged == ""


def test_task_stage_scope_old_coordinate_copy_has_guarded_exception_type(
        tf, tmp_path, monkeypatch):
    """구 사본의 RepoCoordinateError 속성 부재가 AttributeError traceback으로 새지 않는다."""
    home = tmp_path / "pm-home"
    workspace = home / "work" / "project_manager_1"
    workspace.mkdir(parents=True)
    board_py = _write_coordinate_stage_board(
        tmp_path, ["work/project_manager_1/src/change.py"])

    class _OldCoordinates:
        @staticmethod
        def normalize_repo_paths(*args, **kwargs):
            raise RuntimeError("구 좌표 사본")

    monkeypatch.setattr(tf, "REPO", home)
    monkeypatch.setattr(tf, "_load_repo_coordinates", lambda: _OldCoordinates())
    scope = tf.stage_scope(
        "T-0473", board_py, home / "log.md", lambda args: (0, ""),
        repo=workspace, touches_workspace=workspace,
    )
    assert scope.pathspec == ()
    assert "구 좌표 사본" in scope.error


def test_coordinate_loader_old_sibling_reports_explicit_skew(
        tf, tmp_path, monkeypatch):
    """stamp/예외 타입이 없는 구 sibling도 AttributeError가 아닌 엔진 skew로 식별한다."""
    (tmp_path / "repo_coordinates.py").write_text(
        "def normalize_repo_paths(paths, **kwargs):\n"
        "    raise ValueError('old copy')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tf, "TOOLS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="버전 불일치"):
        tf._load_repo_coordinates()


# ── diff 서킷브레이커 진입 검사 (T-0593) ─────────────────────────────────────
#
# 리뷰 라운드가 수렴하지 않는 두 번째 원인은 구현 스코프 팽창이다(실측 5,018줄 단일 티켓).
# estimate 를 넘긴 스코프는 회귀가 green 이어도 완료 대상이 아니라 분할 대상이라, 어떤 부작용
# (회귀 실행·log append·board complete·git stage)도 나기 전에 멈춘다. 상한 표와 측정식은
# external_review 가 소유하고 여기서는 판정만 소비한다(사본 0).


def _boom_pytest():
    raise AssertionError("차단된 완료는 회귀를 돌리지 않는다")


def test_diff_cap_block_stops_before_any_side_effect(tf, tmp_path, capsys):
    """상한 초과 → rc 1 · 회귀/log/board/git 어느 것도 건드리지 않는다 (진입 검사·DoD)."""
    log_file = tmp_path / "log.md"
    finisher = tf.TicketFinisher(
        run_pytest_fn=_boom_pytest,
        run_board_fn=lambda args: (_ for _ in ()).throw(AssertionError("board 미호출")),
        run_git_fn=lambda args: (_ for _ in ()).throw(AssertionError("git 미호출")),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: [],
        diff_cap_block_fn=lambda tid: f"diff 서킷브레이커 차단 — {tid} · 분할하세요",
        log_file=log_file,
    )
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert not log_file.exists()
    err = capsys.readouterr().err
    assert "[중단]" in err and "서킷브레이커" in err and "T-1234" in err


def test_diff_cap_pass_completes_normally(tf, tmp_path, capsys):
    """상한 이내(None)면 종전 흐름 그대로 완료된다 (정상 경로 무영향·DoD)."""
    finisher = tf.TicketFinisher(
        run_pytest_fn=_green_pytest(100),
        run_board_fn=lambda args: (0, "board ok"),
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: [],
        diff_cap_block_fn=lambda tid: None,
        log_file=tmp_path / "log.md",
    )
    assert finisher.run("T-1234", section=None, dry_run=False) == 0
    assert "[완료] T-1234 기록 완료." in capsys.readouterr().out


def test_unterminated_frontmatter_fallback_keeps_finish_flow_open(
        tf, tmp_path, monkeypatch):
    """손상 ticket + board 불능은 측정 가드 off라 완료 기록을 막지 않는다."""
    repo = tmp_path / "repo"
    board_py = repo / ".project_manager" / "tools" / "board.py"
    board_py.parent.mkdir(parents=True)
    board_py.write_text("# board unavailable fixture\n", encoding="utf-8")
    ticket = repo / ".project_manager" / "board" / "tickets" / "claimed" / "T-9001.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "---\nid: T-9001\nestimate: small\ntouches:\n- src/app.py\n",
        encoding="utf-8",
    )
    external = tf._load_external_review()
    assert external is not None
    assert hasattr(external, "AnchorResolutionError")
    monkeypatch.setattr(
        tf, "_ticket_frontmatter_snapshot",
        lambda _board, _ticket: tf.TicketFrontmatterSnapshot(
            {}, "board fixture unavailable",
        ),
    )

    class _Log:
        @staticmethod
        def append_log(path, text):
            path.write_text(text, encoding="utf-8")

    completed: list[list[str]] = []
    monkeypatch.setattr(tf, "_load_pm_log", lambda: _Log)
    finisher = tf.TicketFinisher(
        run_pytest_fn=_green_pytest(100),
        run_board_fn=lambda args: (completed.append(args), (0, "board ok"))[1],
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "손상 ticket",
        affected_domain_fn=lambda tid: [],
        stage_scope_fn=lambda tid: tf.StageScope((), None),
        status_entries_fn=lambda: (),
        dod_block_fn=lambda tid: None,
        log_file=tmp_path / "log.md",
        board_py=board_py,
    )

    assert finisher.run("T-9001", section=None, dry_run=False) == 0
    assert completed == [["complete", "T-9001", "--tests-pass"]]
    assert "T-9001" in (tmp_path / "log.md").read_text(encoding="utf-8")


def test_default_diff_cap_seam_consumes_the_external_review_policy(
        tf, tmp_path, monkeypatch):
    """기본 seam 은 external_review 의 상한 표·측정식을 그대로 쓴다 (사본 0·배선 고정)."""
    external = tf._load_external_review()
    assert external is not None
    monkeypatch.setattr(tf, "get_ticket_touches", lambda board_py, tid: ["src/pay.py"])
    monkeypatch.setattr(tf, "get_ticket_estimate", lambda board_py, tid: "small")
    monkeypatch.setattr(external, "local_config", lambda repo=None: {})
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 301)
    monkeypatch.setattr(tf, "_load_external_review", lambda: external)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md")
    block = finisher._default_diff_cap_block("T-1234")
    assert block is not None and "small" in block and "301줄" in block and "300줄" in block

    # 상한 이내면 통과, estimate 미선언이면 가드 off (엔진이 상한을 지어내지 않는다).
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 300)
    assert finisher._default_diff_cap_block("T-1234") is None
    monkeypatch.setattr(tf, "get_ticket_estimate", lambda board_py, tid: None)
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 99999)
    assert finisher._default_diff_cap_block("T-1234") is None


def test_default_diff_cap_seam_is_off_without_touches(tf, tmp_path, monkeypatch):
    """touches 가 없으면 측정 범위가 없다 — 가드 off(완료를 막지 않는다)."""
    monkeypatch.setattr(tf, "get_ticket_touches", lambda board_py, tid: [])
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md")
    assert finisher._default_diff_cap_block("T-1234") is None


def test_default_diff_cap_seam_is_off_when_external_review_is_absent(
        tf, tmp_path, monkeypatch):
    """external_review 사본이 없으면 가드 off — 부분 설치가 완료를 벽돌로 만들지 않는다."""
    monkeypatch.setattr(tf, "_load_external_review", lambda: None)
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md")
    assert finisher._default_diff_cap_block("T-1234") is None


def test_default_diff_cap_seam_absorbs_a_missing_claim_anchor_symbol(
        tf, tmp_path, monkeypatch, capsys):
    """사본은 있으나 `claim_anchor` seam 이 없는 구형/부분 설치도 벽돌 대신 옛 폭+경고로 접힌다.

    `_diff_numstat_by_path` 의 required 가드와 짝이 되는 케이스 — 사본 부재(위 테스트)와 달리
    사본은 있고 다른 seam(측정 numstat)은 갖췄는데 이 seam 만 없는 형상이다."""
    external = tf._load_external_review()
    assert external is not None
    monkeypatch.delattr(external, "claim_anchor", raising=False)
    monkeypatch.setattr(tf, "get_ticket_touches", lambda board_py, tid: ["src/pay.py"])
    monkeypatch.setattr(tf, "get_ticket_estimate", lambda board_py, tid: "small")
    monkeypatch.setattr(external, "local_config", lambda repo=None: {})
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 301)
    monkeypatch.setattr(tf, "_load_external_review", lambda: external)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md")
    block = finisher._default_diff_cap_block("T-1234")

    assert block is not None and "301줄" in block and "300줄" in block
    err = capsys.readouterr().err
    assert "claim_anchor 부재" in err and "T-1234" in err


# ── 측정 스코프 정규화 · 기계 mirror 제외 (T-0601 ①⑧) ────────────────────────
#
# ⑧ PM 홈 좌표 touches(`work/<repo>_<N>/…`)를 그대로 재면 측정 트리에 그 경로가 없어 diff 가 0 이
#    나오고 상한이 조용히 우회된다. 정규화는 stage 경로와 **같은** `repo_coordinates` seam 이다.
# ① 기계 mirror 제외는 external_review 측정 seam 이 소유한다 — 완료 기록은 그 판정을 빌려 쓴다.


def test_measured_touches_normalizes_pm_home_coordinates(tf, tmp_path, monkeypatch):
    """PM 홈 좌표는 측정 트리 좌표로 정규화된다 (stage 경로와 같은 규칙·사본 0)."""
    workspace = tmp_path / "work" / "proj_1"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(tf, "REPO", tmp_path)
    monkeypatch.setattr(
        tf, "get_ticket_touches",
        lambda board_py, tid: ["work/proj_1/.project_manager/tools/board.py",
                               "work/proj_1/tests/"])

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", task_workspace=workspace)
    assert finisher._measured_touches("T-1") == [
        ".project_manager/tools/board.py", "tests"]


def test_measured_touches_leaves_plain_repo_paths_untouched(tf, tmp_path, monkeypatch):
    """접두 없는 채택자 형상은 normalizer 를 타지 않는다 (경량 판정·현행 무변경)."""
    monkeypatch.setattr(tf, "get_ticket_touches",
                        lambda board_py, tid: [".project_manager/tools/board.py"])
    monkeypatch.setattr(tf, "_load_repo_coordinates",
                        lambda: (_ for _ in ()).throw(AssertionError("normalizer 미호출")))
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md")
    assert finisher._measured_touches("T-1") == [".project_manager/tools/board.py"]


def test_measured_touches_slot_mismatch_is_loud_guard_off(tf, tmp_path, monkeypatch, capsys):
    """슬롯 불일치는 **경고 1줄 + 가드 off** 다 — 조용한 diff 0 으로 접지 않는다."""
    workspace = tmp_path / "work" / "proj_1"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(tf, "REPO", tmp_path)
    monkeypatch.setattr(tf, "get_ticket_touches",
                        lambda board_py, tid: ["work/other_2/tools/board.py"])

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", task_workspace=workspace)
    assert finisher._measured_touches("T-1") is None
    assert "좌표 정규화 실패" in capsys.readouterr().err


def test_diff_cap_measures_the_normalized_scope(tf, tmp_path, monkeypatch):
    """서킷브레이커가 **정규화된 스코프**로 잰다 — 접두 불일치로 상한이 우회되던 구멍 폐쇄."""
    workspace = tmp_path / "work" / "proj_1"
    workspace.mkdir(parents=True)
    external = tf._load_external_review()
    seen: dict[str, object] = {}

    def _measure(root, base, paths):
        seen["root"], seen["paths"] = root, list(paths)
        return 301

    monkeypatch.setattr(tf, "REPO", tmp_path)
    monkeypatch.setattr(tf, "get_ticket_touches",
                        lambda board_py, tid: ["work/proj_1/src/pay.py"])
    monkeypatch.setattr(tf, "get_ticket_estimate", lambda board_py, tid: "small")
    monkeypatch.setattr(external, "local_config", lambda repo=None: {})
    monkeypatch.setattr(external, "diff_line_total", _measure)
    monkeypatch.setattr(tf, "_load_external_review", lambda: external)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", task_workspace=workspace)
    block = finisher._default_diff_cap_block("T-1")
    assert seen["paths"] == ["src/pay.py"], "PM 홈 좌표 그대로 측정하면 diff 0 으로 우회된다"
    assert seen["root"] == workspace
    assert block is not None and "301줄" in block


# ── untracked 신규 파일 측정 (T-0604 ③) ─────────────────────────────────────
#
# 완료 기록은 **[0/5] 서킷브레이커 → … → [4/5] git stage** 순서다. `git diff` 가 아직 add 되지
# 않은 파일을 못 보던 동안에는 그 순서 자체가 결함이었다 — 대형 신규 파일이 0 줄로 측정돼 상한을
# 통과한 뒤, 바로 다음 단계에서 stage 됐다. 측정이 untracked 를 포함하면 그 창이 닫힌다.


def _git_tree_with_untracked(tmp_path: Path, relpath: str, lines: int) -> Path:
    """커밋 하나 + **add 되지 않은** 신규 파일이 있는 실 git 트리 (측정 입력)."""
    root = tmp_path / "code"
    root.mkdir()
    for args in (["init"], ["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "seed.py").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "seed"], check=True,
                   capture_output=True)
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"n{i}\n" for i in range(lines)), encoding="utf-8")
    return root


def test_a_large_untracked_file_is_measured_before_staging(tf, tmp_path, monkeypatch):
    """stage 전에 잰다 — 상한을 넘는 신규 파일은 **측정 시점에** 잡혀 완료가 막힌다.

    실 git 트리로 `diff_line_total` 을 그대로 태운다(측정 스텁 없음) — 이 순서 결함은 측정식이
    아니라 '무엇이 diff 에 들어오나'의 문제였기 때문이다."""
    root = _git_tree_with_untracked(tmp_path, "src/new_module.py", 400)
    external = tf._load_external_review()
    monkeypatch.setattr(tf, "get_ticket_touches", lambda board_py, tid: ["src/"])
    monkeypatch.setattr(tf, "get_ticket_estimate", lambda board_py, tid: "small")
    monkeypatch.setattr(external, "local_config", lambda repo=None: {})
    monkeypatch.setattr(tf, "_load_external_review", lambda: external)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", regression_cwd=root)
    block = finisher._default_diff_cap_block("T-0604")

    assert block is not None and "400줄" in block and "300줄" in block


def test_an_untracked_file_within_the_cap_still_completes(tf, tmp_path, monkeypatch):
    """상한 이내면 종전대로 통과한다 — 포함이 정당한 완료를 오차단하지 않는다."""
    root = _git_tree_with_untracked(tmp_path, "src/new_module.py", 40)
    external = tf._load_external_review()
    monkeypatch.setattr(tf, "get_ticket_touches", lambda board_py, tid: ["src/"])
    monkeypatch.setattr(tf, "get_ticket_estimate", lambda board_py, tid: "small")
    monkeypatch.setattr(external, "local_config", lambda repo=None: {})
    monkeypatch.setattr(tf, "_load_external_review", lambda: external)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", regression_cwd=root)

    assert finisher._default_diff_cap_block("T-0604") is None


def test_completion_surface_carries_the_shared_measurement_meaning(
        tf, tmp_path, monkeypatch):
    """완료 기록 안내도 '측정=손작업 스코프(기계 mirror 제외)'를 싣는다 (문구 단일 출처)."""
    external = tf._load_external_review()
    monkeypatch.setattr(tf, "get_ticket_touches", lambda board_py, tid: ["templates/"])
    monkeypatch.setattr(tf, "get_ticket_estimate", lambda board_py, tid: "small")
    monkeypatch.setattr(external, "local_config", lambda repo=None: {})
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 301)
    monkeypatch.setattr(tf, "_load_external_review", lambda: external)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md")
    assert external.MEASURED_SCOPE_NOTE in finisher._default_diff_cap_block("T-1")


# ── DoD 기록 게이트 preflight (T-0601 ②) ─────────────────────────────────────
#
# DoD 판정이 [3/5] `board.py complete` 안에만 있던 동안에는, 차단될 때마다 [2/5] 가 이미 append 한
# stray log 스켈레톤이 남고 재실행이 그것을 중복 append 했다(실측). 판정을 append **앞**으로 당겨
# 순서 결함 자체를 없앤다 — 권위 있는 차단은 여전히 board 가 소유한다(여기는 같은 판정의 조기 호출).


def _dod_finisher(tf, tmp_path, *, dod_block, log_file: Path | None = None):
    """DoD preflight 만 살아 있는 finisher — 나머지 부작용 seam 은 전부 폭발 대역."""
    return tf.TicketFinisher(
        run_pytest_fn=_boom_pytest,
        run_board_fn=lambda args: (_ for _ in ()).throw(AssertionError("board 미호출")),
        run_git_fn=lambda args: (_ for _ in ()).throw(AssertionError("git 미호출")),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: [],
        diff_cap_block_fn=lambda tid: None,
        dod_block_fn=lambda tid: dod_block,
        log_file=log_file if log_file is not None else tmp_path / "log.md",
    )


def test_dod_preflight_blocks_before_the_log_skeleton(tf, tmp_path, capsys):
    """DoD 미마감 → rc 1 · 회귀/log/board/git 어느 것도 건드리지 않는다 (순서 결함 폐쇄·DoD)."""
    log_file = tmp_path / "log.md"
    finisher = _dod_finisher(tf, tmp_path, log_file=log_file,
                            dod_block="완료 조건(DoD) 미마감 — T-1234 (1건)")
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert not log_file.exists(), "차단된 실행이 stray 스켈레톤을 남겼다"
    err = capsys.readouterr().err
    assert "[중단]" in err and "DoD" in err and "T-1234" in err


def test_repeated_dod_block_never_duplicates_the_skeleton(tf, tmp_path):
    """재실행해도 log 는 그대로다 — 차단마다 중복 append 되던 실측 결함의 직접 재현."""
    log_file = tmp_path / "log.md"
    log_file.write_text("# log\n", encoding="utf-8")
    finisher = _dod_finisher(tf, tmp_path, log_file=log_file, dod_block="DoD 미마감")
    for _ in range(3):
        assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert log_file.read_text(encoding="utf-8") == "# log\n"


def test_dod_preflight_runs_after_the_diff_cap(tf, tmp_path, capsys):
    """진입 게이트 순서 = 서킷브레이커 → DoD (더 넓은 거절이 먼저·안내 1개만 뜬다)."""
    finisher = tf.TicketFinisher(
        run_pytest_fn=_boom_pytest,
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: [],
        diff_cap_block_fn=lambda tid: "서킷브레이커 차단",
        dod_block_fn=lambda tid: (_ for _ in ()).throw(
            AssertionError("cap 차단 뒤에는 DoD 판정을 묻지 않는다")),
        log_file=tmp_path / "log.md",
    )
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert "서킷브레이커 차단" in capsys.readouterr().err


def test_dry_run_is_also_refused_by_the_dod_preflight(tf, tmp_path):
    """dry-run 도 같은 거절이다 — 미리보기는 '부작용 없음'이지 '게이트 면제'가 아니다."""
    finisher = _dod_finisher(tf, tmp_path, dod_block="DoD 미마감")
    assert finisher.run("T-1234", section=None, dry_run=True) == 1


def _write_dod_board(tmp_path, body_repr: str, *, with_gate: bool = True) -> Path:
    """`_dod_open_items` 를 노출하는 board.py 대역 (판정 규칙은 실 board 문법 그대로)."""
    gate = (
        "def _dod_open_items(body):\n"
        "    return [line.strip() for line in body.splitlines()\n"
        "            if line.strip().startswith('- [ ]')]\n"
        if with_gate else ""
    )
    board_py = tmp_path / "dod_board.py"
    board_py.write_text(
        f"ENGINE_REV = {_real_engine_rev()!r}\n"
        "REPO = None\n"
        "_DOD_VALUE_FORMS = '`- [x] <원문>` 또는 `- [>] <원문> (이월: <사유·귀속>)`'\n"
        "def find_ticket(ticket_id):\n"
        "    return ('claimed', ticket_id)\n"
        f"def load_ticket(path):\n"
        f"    return ({{}}, {body_repr})\n"
        + gate,
        encoding="utf-8",
    )
    return board_py


def test_default_dod_preflight_consumes_the_board_gate(tf, tmp_path):
    """기본 seam 은 board 의 DoD 판정을 그대로 부른다 (규칙 사본 0·값 형식도 board 것)."""
    board_py = _write_dod_board(
        tmp_path, repr("## 완료 조건\n- [x] 한 건\n- [ ] 남은 항목\n"))
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", board_py=board_py)

    block = finisher._default_dod_block("T-1234")
    assert block is not None
    assert "T-1234" in block and "남은 항목" in block
    assert "(이월: <사유·귀속>)" in block                  # 값 형식은 board 소유분
    assert "board.py complete 의 게이트와 **같은 판정**" in block


def test_default_dod_preflight_passes_a_closed_ticket(tf, tmp_path):
    """전항 마감이면 None — 정상 완료 흐름은 무영향."""
    board_py = _write_dod_board(
        tmp_path, repr("## 완료 조건\n- [x] 한 건\n- [>] 이월분 (이월: v1.7.1·T-0602)\n"))
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", board_py=board_py)
    assert finisher._default_dod_block("T-1234") is None


def test_default_dod_preflight_is_off_without_the_board_gate(tf, tmp_path):
    """구세대 board 사본(게이트 부재)은 preflight 가 조용히 off — 진짜 게이트가 뒤에 있다."""
    board_py = _write_dod_board(tmp_path, repr("- [ ] 남은 항목\n"), with_gate=False)
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", board_py=board_py)
    assert finisher._default_dod_block("T-1234") is None


def test_ticket_estimate_reads_frontmatter(tf, tmp_path):
    """`estimate` 는 board frontmatter 에서 읽고, 부재/비-문자열은 None 이다."""
    board_py = tmp_path / "estimate_board.py"
    board_py.write_text(
        f"ENGINE_REV = {_real_engine_rev()!r}\n"   # T-0397 rev 스탬프
        "def find_ticket_exact(ticket_id, **kwargs):\n"
        "    return ('claimed', ticket_id)\n"
        "def load_ticket(path):\n"
        "    table = {'T-1': {'estimate': ' medium '}, 'T-2': {},\n"
        "             'T-3': {'estimate': 7}}\n"
        "    return (table[path], 'body')\n",
        encoding="utf-8",
    )
    assert tf.get_ticket_estimate(board_py, "T-1") == "medium"
    assert tf.get_ticket_estimate(board_py, "T-2") is None
    assert tf.get_ticket_estimate(board_py, "T-3") is None


# ── PM-direct 완료 재검 (T-0677) ───────────────────────────────────────────────

def test_pm_direct_warns_when_touches_exceed_two_files(tf):
    warnings = tf.pm_direct_finish_warnings(
        "pm-direct", ["a.py", "b.py", "c.py"], [],
    )
    assert len(warnings) == 1
    assert "(a)" in warnings[0] and "3개" in warnings[0]


def test_pm_direct_directory_touch_uses_expanded_file_count(tf):
    warnings = tf.pm_direct_finish_warnings(
        "pm-direct", ["src/"], [], touched_file_count=3,
    )
    assert len(warnings) == 1
    assert "(a)" in warnings[0] and "3개 파일" in warnings[0]


def test_pm_direct_unresolved_directory_warns_conservatively(tf):
    warnings = tf.pm_direct_finish_warnings(
        "pm-direct", ["src/"], [], touched_file_count=0,
        unresolved_directories=["src"],
    )
    assert len(warnings) == 1
    assert "(a)" in warnings[0] and "상향 기본값" in warnings[0]


def test_pm_direct_warns_for_code_diff_without_changed_test(tf):
    warnings = tf.pm_direct_finish_warnings(
        "pm-direct", ["src/app.py"], ["src/app.py"],
    )
    assert len(warnings) == 1
    assert "(b)" in warnings[0] and "테스트" in warnings[0]


def test_pm_direct_recheck_normal_pass_and_non_pm_direct_are_silent(tf):
    assert tf.pm_direct_finish_warnings(
        "pm-direct", ["src/app.py", "tests/test_app.py"],
        ["src/app.py", "tests/test_app.py"],
    ) == ()
    assert tf.pm_direct_finish_warnings(
        "normal", ["a.py", "b.py", "c.py"], ["a.py"],
    ) == ()


def test_pm_direct_warning_is_never_block_and_preserves_finish_rc(
        tf, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        tf, "_ticket_frontmatter_snapshot",
        lambda _board, _tid: tf.TicketFrontmatterSnapshot({
            "tier": "pm-direct", "touches": ["src/app.py"],
        }, None),
    )
    finisher = tf.TicketFinisher(
        run_pytest_fn=_green_pytest(7),
        run_board_fn=lambda _args: (0, "board ok"),
        run_git_fn=lambda _args: (0, ""),
        board_count_fn=lambda: 3,
        ticket_title_fn=lambda _tid: "PM direct",
        affected_domain_fn=lambda _tid: [],
        status_entries_fn=lambda: ((" M", "src/app.py"),),
        stage_scope_fn=lambda _tid: tf.StageScope((), None),
        diff_cap_block_fn=lambda _tid: None,
        dod_block_fn=lambda _tid: None,
        log_file=tmp_path / "log.md",
    )

    assert finisher.run("T-0677", section=None, dry_run=False) == 0
    captured = capsys.readouterr()
    assert "PM-direct 조건 (b)" in captured.err
    assert "[완료] T-0677" in captured.out


def test_pm_direct_task_coordinate_failure_is_loud_and_never_uses_raw_touches(
        tf, tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        tf, "_ticket_frontmatter_snapshot",
        lambda _board, _tid: tf.TicketFrontmatterSnapshot({
            "tier": "pm-direct",
            "touches": ["work/project_manager_1/src/app.py"],
        }, None),
    )
    monkeypatch.setattr(tf, "_load_repo_coordinates", lambda: None)
    finisher = tf.TicketFinisher(
        board_py=tf.BOARD_PY,
        task_workspace=workspace,
        status_entries_at_fn=lambda _cwd: ((" M", "src/app.py"),),
    )

    finisher._warn_pm_direct_conditions("T-0677")
    err = capsys.readouterr().err
    assert "PM-direct 재검 skip" in err
    assert "raw PM-home 경로로 판정하지 않습니다" in err
    assert "조건 (b)" not in err


def test_pm_direct_finish_wires_directory_expansion_into_condition_a(
        tf, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        tf, "_ticket_frontmatter_snapshot",
        lambda _board, _tid: tf.TicketFrontmatterSnapshot({
            "tier": "pm-direct", "touches": ["src/"],
        }, None),
    )
    expansion = tf.load_board_module(tf.BOARD_PY).TouchFileExpansion(
        ("src/a.py", "src/b.py", "src/c.py"), (),
    )
    fake_board = type("Board", (), {
        "expand_owned_touch_files": staticmethod(lambda _repo, _touches: expansion),
    })()
    monkeypatch.setattr(tf, "load_board_module", lambda _path: fake_board)
    finisher = tf.TicketFinisher(
        board_py=tf.BOARD_PY,
        task_workspace=tmp_path,
        status_entries_at_fn=lambda _cwd: (),
    )

    finisher._warn_pm_direct_conditions("T-0677")
    err = capsys.readouterr().err
    assert "PM-direct 조건 (a) 위반" in err
    assert "3개 파일" in err


# ── 측정 폭 = claim 시점 rev 앵커 (merge 기반 wave) ─────────────────────────
#
# 옛 폭(작업트리 → 비면 직전 커밋 한 칸)은 dev 브랜치를 `--no-ff` merge 로 흡수하는 wave 에서
# 0 에 수렴했다 — 완료 기록 시점 트리는 clean 이고 마지막 커밋이 전파/기록이라 티켓 경로 교집합이
# 비기 때문이다(상한을 넘긴 wave 가 그대로 통과한 실측). claim 이 그 시점 **코드 트리 HEAD** 를
# frontmatter 에 박제하고, 완료 기록이 그것을 앵커로 "claim 이후 누적"을 잰다. 아래 형상은 실
# git 저장소 + 엔진 사본으로 claim → dev 커밋 → merge → 전파 커밋 순서를 그대로 재현한다.
#
# claim 표면(박제 위치·코드 트리 해소 3형상·비-git 경고) 자체의 계약은 여기서 재현하지 않는다 —
# 그 축은 `tests/test_board_claim_strict.py` 가 소유한다(claim 계약 회귀는 그 파일에서 찾는다).
# 아래는 **이미 박제된 앵커를 소비하는 측정 폭** 케이스만이다.

_WAVE_TICKET = "T-7301"
_WAVE_OTHER_TICKET = "T-7302"
_WAVE_SRC_LINES = 320          # claim 이후 `src/` 누적(dev 커밋 2건) — small 상한 300 초과
_WAVE_DOCS_LINES = 50          # 같은 wave 가 `docs/` 에 남긴 변경(타 claimed 티켓 전용 몫)
_WAVE_PROPAGATION_LINES = 4    # merge 뒤 전파 커밋이 만든 신규 파일 1건당 줄 수


def _wave_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8")


def _wave_write(path: Path, lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line{index}\n" for index in range(lines)),
                    encoding="utf-8")


def _wave_ticket(root: Path, ticket_id: str, *, status: str, touch: str) -> Path:
    path = (root / ".project_manager" / "board" / "tickets" / status
            / f"{ticket_id}-wave.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {ticket_id}\ntitle: wave\nstatus: {status}\n"
        "claimed_by: null\nclaimed_at: null\ncompleted_at: null\n"
        f"depends_on: []\nblocks: []\ntouches:\n- {touch}\nestimate: small\n"
        f"tags: []\n---\n\n# {ticket_id} — wave\n\n## 목표\nx\n",
        encoding="utf-8")
    return path


def _load_tool_copy(root: Path, name: str):
    """tmp 저장소 안 엔진 사본을 로드한다 — 그 사본의 REPO 가 곧 이 저장소다(hermetic)."""
    spec = importlib.util.spec_from_file_location(
        f"wave_{name}", root / ".project_manager" / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wave_repo(tmp_path: Path, *, git: bool = True):
    """엔진 사본 + (선택) 실 git 저장소인 솔로 형상 — claim 직전 상태와 board 모듈.

    측정 폭 케이스는 코드 트리 해소 자체가 관심사가 아니다(그 축은 F-004 가 별도로 값 단언한다
    — `test_board_claim_strict.py`) — 여기서는 REPO 가 곧 코드 트리인 **솔로** 형상(인자 전무·
    kind="none")으로 고정해 claim/finish 가 항상 같은 트리를 본다는 전제만 지킨다. local.conf
    `session=`(solo-legacy 폴백)로 리스 장부/env 없이도 `session_name(required=True)` 가
    결정론적으로 해소되게 한다."""
    root = tmp_path / "wave"
    shutil.copytree(TOOLS, root / ".project_manager" / "tools")
    (root / ".project_manager" / "wiki").mkdir(parents=True, exist_ok=True)
    for status in ("open", "claimed", "blocked", "done"):
        (root / ".project_manager" / "board" / "tickets" / status).mkdir(
            parents=True, exist_ok=True)
    (root / ".project_manager" / "local.conf").write_text(
        "test_cmd=pytest -q\n", encoding="utf-8")
    _wave_ticket(root, _WAVE_TICKET, status="open", touch="src/")
    _wave_write(root / "src" / "app.py", 5)
    _wave_write(root / "docs" / "notes.md", 3)
    if git:
        _wave_git(root, "init", "-q", "-b", "main")
        _wave_git(root, "config", "user.email", "t@e")
        _wave_git(root, "config", "user.name", "t")
        _wave_git(root, "add", "-A")
        _wave_git(root, "commit", "-qm", "seed")
    return root, _load_tool_copy(root, "board")


def _wave_claim(board, ticket_id: str = _WAVE_TICKET) -> int:
    # 인자 전무(kind="none") — 세션은 env 명시로 결정론적으로 바인딩한다(per-clone conf
    # `session=` 폴백은 폐지·T-0779). 다른 축엔 무관심하다.
    saved = os.environ.get("PM_SESSION_NAME")
    os.environ["PM_SESSION_NAME"] = "me"
    os.environ.pop("CLAUDE_SESSION_NAME", None)
    try:
        return board.cmd_claim(argparse.Namespace(id=ticket_id, user="me"))
    finally:
        # 같은 워커의 다음 테스트로 새지 않게 원복한다(ambient env 오염 0).
        if saved is None:
            os.environ.pop("PM_SESSION_NAME", None)
        else:
            os.environ["PM_SESSION_NAME"] = saved


def _wave_ticket_path(root: Path, ticket_id: str = _WAVE_TICKET) -> Path:
    return (root / ".project_manager" / "board" / "tickets" / "claimed"
            / f"{ticket_id}-wave.md")


def _wave_work(root: Path) -> None:
    """claim 기록 커밋 → dev 커밋 2건 → `merge --no-ff` → 전파 커밋 2건.

    끝난 트리는 clean 이고 마지막 커밋(전파)은 `src/` 를 안 건드린다 — 옛 폭이 0 으로 접히는
    바로 그 배치다."""
    _wave_git(root, "add", "-A")
    _wave_git(root, "commit", "-qm", "claim 기록")
    _wave_git(root, "checkout", "-q", "-b", f"dev/{_WAVE_TICKET}")
    _wave_write(root / "src" / "app.py", 5 + 200)
    _wave_git(root, "commit", "-qam", "dev 1")
    _wave_write(root / "src" / "app.py", 5 + _WAVE_SRC_LINES)
    _wave_write(root / "docs" / "notes.md", 3 + _WAVE_DOCS_LINES)
    _wave_git(root, "commit", "-qam", "dev 2")
    _wave_git(root, "checkout", "-q", "main")
    _wave_git(root, "merge", "-q", "--no-ff", "-m", "merge dev", f"dev/{_WAVE_TICKET}")
    for index in (1, 2):
        _wave_write(root / "docs" / f"propagation{index}.md", _WAVE_PROPAGATION_LINES)
        _wave_git(root, "add", "-A")
        _wave_git(root, "commit", "-qm", f"propagation {index}")


def _wave_finisher(root: Path, tmp_path: Path):
    """tmp 저장소를 코드 트리로 쓰는 완료 기록 인스턴스와 그 external_review 사본."""
    finish = _load_tool_copy(root, "ticket_finish")
    finisher = finish.TicketFinisher(
        board_py=root / ".project_manager" / "tools" / "board.py",
        regression_cwd=root,
        log_file=tmp_path / "log.md",
    )
    external = finish._load_external_review()
    assert external is not None
    return finisher, external


def _wave_set_claimed_rev(board, root: Path, value: str | None) -> None:
    """박제된 앵커를 구 티켓(부재)·타 저장소 rev 형상으로 바꾼다."""
    path = _wave_ticket_path(root)
    fm, body = board.load_ticket(path)
    if value is None:
        fm.pop("claimed_rev", None)
    else:
        fm["claimed_rev"] = value
    board.dump_ticket(path, fm, body)


def test_merge_wave_is_measured_from_the_claim_anchor(tmp_path):
    """merge 로 흡수한 dev 누적이 완료 기록 서킷브레이커에 잡힌다(옛 폭에선 0 이던 형상)."""
    root, board = _wave_repo(tmp_path)
    assert _wave_claim(board) == 0
    _wave_work(root)
    finisher, external = _wave_finisher(root, tmp_path)

    assert external.diff_line_total(root, "HEAD", ["src/"]) == 0, \
        "옛 폭이 이 형상을 0 으로 재던 사실이 사라졌다 — 대조군 소실."
    block = finisher._default_diff_cap_block(_WAVE_TICKET)

    assert block is not None
    assert f"{_WAVE_SRC_LINES}줄" in block and "300줄" in block


def test_uncommitted_work_still_rides_the_measured_width(tmp_path):
    """작업트리 미커밋 변경도 같은 폭이다 — 커밋 누적 + 작업트리 한 폭으로 잰다."""
    root, board = _wave_repo(tmp_path)
    assert _wave_claim(board) == 0
    _wave_work(root)
    _wave_write(root / "src" / "app.py", 5 + _WAVE_SRC_LINES + 11)
    _finisher, external = _wave_finisher(root, tmp_path)
    fm, _body = board.load_ticket(_wave_ticket_path(root))

    assert external.diff_line_total(
        root, "HEAD", ["src/"], claimed_rev=fm["claimed_rev"]
    ) == _WAVE_SRC_LINES + 11


def test_other_claimed_ticket_share_is_excluded_under_the_anchor(tmp_path):
    """타 claimed 티켓 전용 몫 제외는 새 폭에서도 그대로다 (귀속 보정 무변경)."""
    root, board = _wave_repo(tmp_path)
    assert _wave_claim(board) == 0
    _wave_ticket(root, _WAVE_OTHER_TICKET, status="claimed", touch="docs/")
    _wave_work(root)
    finisher, _external = _wave_finisher(root, tmp_path)

    block = finisher._default_diff_cap_block(_WAVE_TICKET)

    excluded = _WAVE_DOCS_LINES + 2 * _WAVE_PROPAGATION_LINES
    assert block is not None and f"{_WAVE_SRC_LINES}줄" in block
    assert f"귀속 제외: {excluded}줄" in block and _WAVE_OTHER_TICKET in block


def test_ticket_without_an_anchor_falls_back_to_the_old_width_loudly(
        tmp_path, capsys):
    """구 티켓(앵커 부재)은 옛 폭으로 재되 통과가 조용하지 않다 — 경고 1줄."""
    root, board = _wave_repo(tmp_path)
    assert _wave_claim(board) == 0
    _wave_work(root)
    _wave_set_claimed_rev(board, root, None)
    finisher, _external = _wave_finisher(root, tmp_path)

    assert finisher._default_diff_cap_block(_WAVE_TICKET) is None
    err = capsys.readouterr().err
    assert "claimed_rev 없음" in err and "과소 측정" in err


def test_unresolvable_anchor_falls_back_to_the_old_width_loudly(tmp_path, capsys):
    """이 트리에서 해소되지 않는 rev 도 같은 처방이다 — 조용한 0 줄로 접지 않는다."""
    root, board = _wave_repo(tmp_path)
    assert _wave_claim(board) == 0
    _wave_work(root)
    _wave_set_claimed_rev(board, root, "b" * 40)
    finisher, _external = _wave_finisher(root, tmp_path)

    assert finisher._default_diff_cap_block(_WAVE_TICKET) is None
    assert "해소하지 못함" in capsys.readouterr().err
