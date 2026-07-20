"""ticket_finish.py 직접 단위테스트 (T-0042·T-0103).

PM 세션 라이프사이클 자동화 — pytest 출력 파서(`parse_pytest_output`·`is_pytest_green`)·
per-repo 회귀 명령 해소(ADR-0014)·domain soft 알림(ADR-0018 #2)·전체 run() 흐름을 직접
검증한다. (T-0103/ADR-0023 a안: status.md 가 judgment-only 로 바뀌어 ticket_finish 는 더
이상 status.md 스칼라를 갱신하지 않는다 — `update_status`/`_replace_once`/앵커 정규식 제거.
대신 *완료 흐름이 status.md 를 전혀 안 건드림* 을 단언한다.)

도구는 패키지가 아니므로 importlib 동적 로드 (test_engine_smoke·test_pm_log 관용구).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

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


# ── 회귀 명령 per-repo 해소 (codex must-fix 1·ADR-0014) ───────────────────────
#
# multi-PM(multi-PM) 모드에선 활성 repo 가 비-Python 일 수 있어 `pytest tests/ -q` 가 틀린다 —
# 회귀는 활성 repo 의 per-repo test_cmd(areas.md)를 써야 한다. 단 **솔로/프레임워크 자기
# 회귀(areas.md 없음)는 현행 `pytest tests/ -q` venv 실행을 반드시 보존**(도그푸딩 불변).
# `tf` 모듈 전역(AREAS_FILE·_load_board_module)을 재바인딩해 hermetic 하게 검증한다.


class _FakeBoard:
    """board.py 대역 — areas_file / id_prefix / _areas_row_for_prefix 만 흉내(areas 해소 가로채기).

    `areas_file()` = board_root 추종 결과(T-0162 A6) — `_resolve_per_repo_test_cmd` 의 존재
    가드가 board 모듈 함수로 위임되므로 대역도 그 함수를 제공한다(실재 areas.md 경로 주입).
    """

    def __init__(self, prefix, row, areas_path=None):
        self._prefix = prefix
        self._row = row
        self._areas_path = areas_path or Path("/__nonexistent_areas__.md")

    def areas_file(self):
        return self._areas_path

    def id_prefix(self):
        return self._prefix

    def _areas_row_for_prefix(self, prefix):
        return self._row if prefix == self._prefix else None


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
    assert "[완료] T-1234 부기 완료." in out  # 완료 흐름 도달


def test_soft_step_no_affected_pages_prints_none_and_completes(tf, tmp_path, capsys):
    """영향 0 → '(영향 domain 페이지 없음)' 한 줄·완료 진행(rc 0)."""
    finisher = _make_finisher(tf, tmp_path, affected=[])
    rc = finisher.run("T-1234", section=None, dry_run=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(영향 domain 페이지 없음)" in out
    assert "[완료] T-1234 부기 완료." in out


def test_soft_step_domain_absent_graceful_skip(tf, tmp_path, capsys):
    """domain 레이어 부재(None) → 조용히 skip·완료 진행(솔로/신규 clone 무영향·rc 0)."""
    finisher = _make_finisher(tf, tmp_path, affected=None)
    rc = finisher.run("T-1234", section=None, dry_run=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(domain 레이어 없음 — skip)" in out
    assert "[완료] T-1234 부기 완료." in out


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
    assert "[완료] T-1234 부기 완료." in capsys.readouterr().out


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
    assert "[완료] T-1234 부기 완료." in out
    # 단계 카운트가 5/5 로 줄었다 (status 단계 제거).
    assert "[1/5]" in out and "[5/5]" in out and "[6/6]" not in out


def test_run_ignores_section_arg(tf, tmp_path, capsys):
    """--section 은 후방호환 수용만·no-op — 줘도 status 건드림 없이 정상 완주(rc 0)."""
    finisher = _make_finisher(tf, tmp_path, affected=[])
    rc = finisher.run("T-1234", section="엔진 도구 (board.py)", dry_run=False)
    assert rc == 0
    assert "[완료] T-1234 부기 완료." in capsys.readouterr().out


def test_log_skeleton_reports_measured_total_no_old_total(tf):
    """log 스켈레톤은 실측 new_total 1줄만 — old_total/delta 박제 없음(ADR-0023·ADR-0008)."""
    skeleton = tf.build_log_skeleton(
        ticket_id="T-0001", title="t", new_total=42, board_before=1, board_after=2,
    )
    assert "회귀 42 / 42" in skeleton
    assert "board: done 1→2" in skeleton


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
    """find_ticket/load_ticket 만 정의하는 board.py 대역을 tmp 에 쓴다(hermetic)."""
    board_py = tmp_path / "fake_board.py"
    board_py.write_text(
        f"ENGINE_REV = {_real_engine_rev()!r}\n"  # T-0397 rev 스탬프
        "def find_ticket(ticket_id):\n"
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
    assert "[완료] T-1234 부기 완료." in out


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
    교체한다 — 이게 실 장부/areas/REPO 미접촉 hermetic 해소의 유효한 seam 이다(repo_root 미지정
    이면 real 의 기본값=REPO). ticket_finish wrapper 는 `hp._regression_cwd(slot)` 로 호출하므로
    이 교체가 그대로 위임 경로에 먹힌다.
    """
    hp = tf._load_pm_handoff()
    assert hp is not None  # 동적 로드 성공 전제 (없으면 폴백 테스트로 분리)
    real = hp._regression_cwd
    monkeypatch.setattr(
        hp, "_regression_cwd",
        lambda worktree_slot=None: real(worktree_slot, areas, leases, repo_root=repo_root),
    )
    monkeypatch.setattr(tf, "_load_pm_handoff", lambda: hp)
    return hp


def test_regression_cwd_self_host_resolves_worktree_slot(tf, tmp_path, monkeypatch):
    """② 홈 cwd 모사 — 단일 self-host(areas 1 repo + 슬롯 1개) → work/<repo>_<N> 해소.

    이게 깨지면 ② 홈서 ticket_finish 회귀가 tests/ 없는 ② cwd 에서 돌아 "no tests ran" red.
    슬롯명을 `work/myrepo_7` 로 둬 REPO suffix(`work/project_manager_1`)와 구조적으로 구별한다 —
    REPO 폴백과 우연 일치하지 않아야 self-host 해소를 실제로 단언하는 가드가 된다.
    """
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_selfhost_areas(areas, ["myrepo"])
    _write_selfhost_leases(leases, [
        {"slot": "work/myrepo_7", "repo": "myrepo",
         "session": "myrepo_7", "state": "leased"},
    ])
    _bind_pm_handoff_seams(tf, monkeypatch, areas, leases)
    result = tf._regression_cwd()
    # REPO 자체가 work/project_manager_1 이므로 myrepo_7 로 끝나면 self-host 해소 확정(폴백 아님).
    assert result.replace(os.sep, "/").endswith("work/myrepo_7")
    assert not result.endswith(str(tf.REPO))  # REPO 폴백과 구별


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
# `--no-pytest` 는 회귀를 skip 한다. 다중슬롯인데 미지정+진짜 모호면 조용한 false-red 대신
# fail-loud 로 중단한다. pm_handoff `_resolve_explicit_identity_slot`(M3 조인검증)·
# `_resolve_session_worktree_slot` 재사용(DRY).


def _bind_pm_handoff_repo(tf, monkeypatch, repo_root: Path):
    """pm_handoff 를 로드해 `REPO` 를 `repo_root` 로 monkeypatch 하고 그 인스턴스를 고정
    반환하도록 `tf._load_pm_handoff` 를 교체한다 — `_resolve_explicit_identity_slot`(M3 리스
    조인검증)·`_regression_cwd`(L1) 의 leases_file/repo_root 기본 해소가 이 tmp_path 를 보게
    만드는 hermetic seam(실 장부·실 REPO 미접촉)."""
    hp = tf._load_pm_handoff()
    assert hp is not None
    monkeypatch.setattr(hp, "REPO", repo_root)
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
    def _regression_cwd(worktree_slot=None):
        return f"/fake-repo/{worktree_slot}" if worktree_slot else "/fake-repo"


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
    assert "[완료] T-1234 부기 완료." in out
    assert "회귀 ? / ?" in (tmp_path / "log.md").read_text(encoding="utf-8")  # 측정 total 없이 "?"


def test_main_multislot_ambiguous_fails_loud_before_regression(tf, monkeypatch, capsys):
    """(c) 다중슬롯 모호 + --repo/--slot 부재 → 친화 에러·rc 1·회귀 미시작(조용한 ② false-red 제거).

    fail-loud 는 run() 진입 *전*이라 회귀가 시작조차 안 된다([1/5] 미출력). 에러는 --repo/
    --no-pytest 안내를 담는다.
    """
    monkeypatch.setattr(
        tf, "_load_pm_handoff",
        lambda: _FakeHandoff((None, "등록 repo 2개(A, B) — 어느 repo 인지 모호하다.")),
    )
    rc = tf.main(["T-1234"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "모호" in captured.err
    assert "--repo" in captured.err and "--no-pytest" in captured.err
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
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)
    rc = tf.main(["T-1234", "--repo", "project_manager", "--slot", "1"])
    assert rc == 0
    assert captured["regression_cwd"].replace(os.sep, "/").endswith("work/project_manager_1")
    assert captured["skip_pytest"] is False


def test_main_no_pytest_bypasses_ambiguity_gate(tf, monkeypatch):
    """(must-fix) --no-pytest → 다중슬롯 모호여도 게이트 우회·슬롯 해소 미호출·rc 0(self-contradiction 제거).

    이전엔 모호 게이트가 args.no_pytest 앞에서 돌아, 에러가 "--no-pytest 로 skip 하라"를 remedy 로
    광고하면서 정작 --no-pytest 를 준 사용자를 rc 1 로 막는 dead-end 였다(codex+reviewer 동일 포착).
    게이트를 `if not args.no_pytest:` 뒤로 옮겨, --no-pytest 면 회귀가 안 도므로 슬롯 해소 자체를
    호출조차 안 하고 regression_cwd=None·skip 로 진행한다.
    """
    resolve_calls: list = []

    def _spy_resolve(repo, slot):  # 호출되면 모호 반환 — 하지만 --no-pytest 면 호출조차 안 돼야.
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
    assert rc == 0                          # 모호여도 우회(rc 1 아님·remedy dead-end 제거)
    assert resolve_calls == []              # --no-pytest → 슬롯 해소 자체를 skip(회귀 cwd 무의미)
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
