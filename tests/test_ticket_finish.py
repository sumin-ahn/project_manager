"""ticket_finish.py 직접 단위테스트 (T-0042·T-0103).

PM 세션 라이프사이클 자동화 — pytest 출력 파서(`parse_pytest_output`·`is_pytest_green`)·
per-repo 회귀 명령 해소(ADR-0014)·domain soft 알림(ADR-0018 #2)·전체 run() 흐름을 직접
검증한다. (T-0103/ADR-0023 a안: status.md 가 judgment-only 로 바뀌어 ticket_finish 는 더
이상 status.md 스칼라를 갱신하지 않는다 — `update_status`/`_replace_once`/앵커 정규식 제거.
대신 *완료 흐름이 status.md 를 전혀 안 건드림* 을 단언한다.)

도구는 패키지가 아니므로 importlib 동적 로드 (test_engine_smoke·test_pm_log 관용구).
"""
from __future__ import annotations

from pm_engine.cli import board_ticket


from pm_engine.domain import ticket_rules
from pm_engine.app import tier_signals

from pm_engine.cli import board_cluster
from pm_engine.domain import cluster_rules
from pm_engine.infra import cluster_store


import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from _git_fixture import init_git_repo
from conftest import write_cluster_ledger
from pm_engine.infra import git, areas_store, cluster_store, conf_store, doc_page_store, pm_paths, python_env, repo_coordinates, ticket_store
from pm_engine.app import doc_affected, doc_staleness, freshness_lint, slot_resolution

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


def _git_seed(root: Path) -> None:
    """커밋 하나를 가진 실 git 트리를 만든다 — 판정 기준(HEAD)이 해소되는 최소 형상.

    완료 기록의 잔여·측정 판정은 코드 트리의 git 상태를 읽는다. 비-git 디렉터리를 코드 트리로
    주면 그 판정이 기준을 해소하지 못해 멈추므로, 축이 다른 케이스도 트리는 실물로 둔다.
    """
    identity = ("-c", "user.email=t@e", "-c", "user.name=t")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True,
                   capture_output=True, text=True, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), *identity, "commit", "-q",
                    "--allow-empty", "-m", "seed"], check=True,
                   capture_output=True, text=True, encoding="utf-8")


@pytest.fixture(autouse=True)
def _declare_the_integration_basis(tf, monkeypatch):
    """이 파일의 hermetic 픽스처에는 묶음 장부가 없다 — 판정 기준 선언만 대역으로 세운다.

    엔진은 판정 기준(묶음 장부 `base_branch`)이 없으면 멈춘다. 여기서 검증하는 축은 소프트
    단계·라벨·stage·회귀 흐름이지 그 기준의 해소가 아니므로, **이 트리의 현재 커밋**을 기준으로
    선언한다(커밋 인구 = 공집합 · 측정 기준점 = HEAD). 기준 해소 자체를 축으로 삼는 케이스는
    실 장부를 가진 tmp 저장소와 그 안의 엔진 사본으로 돌아 이 대역과 무관하다
    (`_load_tool_copy` — 다른 모듈 객체)."""
    monkeypatch.setattr(
        tf, "_cluster_integration_branch", lambda _board_py, _ticket_id: "HEAD")


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
# 체인 본체는 `regression._resolve_gate_cmd` 단일 사본이고 이 도구는 자기 트리의 `Home` 을 넘겨
# 위임한다). 단 **솔로/프레임워크 자기 회귀(어느 층도 미해소)는 현행 `pytest tests/ -q` venv
# 실행을 반드시 보존**(도그푸딩 불변). 층별 우선순위·두 도구 파리티·무prefix 채택자 e2e 는
# `test_regression_gate_resolution.py` 가 소유하고, 여기서는 이 도구 표면의 계약을 본다.
# 해소 체인이 읽는 패키지 함수(`pm_paths.areas_file`·`areas_store`·`conf_store.local_config`)를
# 대역으로 갈아끼워 hermetic 하게 검증한다.


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

    def registered_repos(self):
        return set(self._repos)

    def local_config(self):
        return dict(self._conf)


def _install_stub(stub, monkeypatch):
    """해소 체인이 읽는 패키지 함수 자리에 대역을 꽂는다."""
    monkeypatch.setattr(pm_paths, "areas_file", lambda _home: stub.areas_file())
    monkeypatch.setattr(areas_store, "id_prefix", lambda _home, override=None: stub.id_prefix())
    monkeypatch.setattr(areas_store, "_areas_row_for_prefix",
                        lambda _home, prefix: stub._areas_row_for_prefix(prefix))
    monkeypatch.setattr(areas_store, "_parse_areas", lambda _home, path=None: stub._parse_areas())
    monkeypatch.setattr(areas_store, "registered_repos", lambda _home: stub.registered_repos())
    monkeypatch.setattr(conf_store, "local_config", lambda _home, repo=None: stub.local_config())


def test_resolve_per_repo_cmd_solo_no_areas_returns_default(tf, tmp_path, monkeypatch):
    """솔로 폴백 — areas.md 부재면 4층 기본 명령(`python_env.default_pytest_cmd`)."""
    _install_stub(_FakeBoard(None, None, areas_path=tmp_path / "no-areas.md"), monkeypatch)
    assert tf._resolve_per_repo_test_cmd() == python_env.default_pytest_cmd()


def test_resolve_per_repo_cmd_multipm_returns_row_cmd(tf, tmp_path, monkeypatch):
    """multi-PM — areas.md 있고 활성 prefix 행에 test_cmd 있으면 그 문자열(예: go test)."""
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    _install_stub(_FakeBoard("A1", {"test_cmd": "go test ./..."}, areas_path=areas), monkeypatch)
    assert tf._resolve_per_repo_test_cmd() == "go test ./..."


def test_resolve_per_repo_cmd_multipm_no_prefix_returns_default(tf, tmp_path, monkeypatch):
    """multi-PM이라도 활성 prefix 없으면 4층 기본 명령(솔로 폴백 — 현행 보존)."""
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    _install_stub(_FakeBoard(None, None, areas_path=areas), monkeypatch)
    assert tf._resolve_per_repo_test_cmd() == python_env.default_pytest_cmd()


def test_resolve_per_repo_cmd_multipm_empty_test_cmd_returns_default(tf, tmp_path, monkeypatch):
    """multi-PM이라도 행의 test_cmd 빈 값이면 4층 기본 명령(부분 등록 — 현행 보존)."""
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    _install_stub(_FakeBoard("A1", {"test_cmd": ""}, areas_path=areas), monkeypatch)
    assert tf._resolve_per_repo_test_cmd() == python_env.default_pytest_cmd()


def test_resolve_per_repo_cmd_blank_prefix_uses_repo_row(tf, tmp_path, monkeypatch):
    """무prefix 채택자 형상 — prefix 칼럼이 비어도 repo 행의 test_cmd 로 해소한다 (T-0730).

    현행 `pm-config repo add` 는 prefix 를 빈 값으로 등록하므로 `id_prefix()` 가 None 이다.
    prefix 층만 보면 올바른 test_cmd 를 담은 행에 도달하지 못해 하드코딩 pytest 로 폴백하고,
    `tests/` 가 없는 채택자는 회귀가 항상 red 로 오판된다.
    """
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix | git | test_cmd | owner |\n", encoding="utf-8")
    _install_stub(_FakeBoard(
        None, None, areas_path=areas,
        rows=[{"repo": "heru", "prefix": "", "test_cmd": "viewer bind"}],
        repos={"heru"},
    ), monkeypatch)
    assert tf._resolve_per_repo_test_cmd() == "viewer bind"


def test_resolve_per_repo_cmd_reads_local_conf_without_areas(tf, tmp_path, monkeypatch):
    """areas.md 가 아예 없어도 local.conf test_cmd 를 읽는다 (T-0730·3층은 areas 가드 밖)."""
    _install_stub(_FakeBoard(None, None, areas_path=tmp_path / "no-areas.md",
                             conf={"test.cmd": "go test ./..."}), monkeypatch)
    assert tf._resolve_per_repo_test_cmd() == "go test ./..."


def test_default_run_pytest_solo_uses_venv_pytest_argv(tf, tmp_path, monkeypatch):
    """솔로 기본도 xdist 병렬 argv를 쓴다.

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
    assert captured["cmd"] == [
        "/venv/bin/python", "-m", "pytest", "tests/", "-q", "-n", "auto",
    ]
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
    """해소 실패(솔로) dry-run 안내도 병렬 기본을 보여준다."""
    monkeypatch.setattr(tf, "_resolve_per_repo_test_cmd", lambda: None)
    finisher = _make_finisher(tf, tmp_path, affected=[])
    rc = finisher.run("T-1234", section=None, dry_run=True)
    assert rc == 0
    assert "[dry-run] pytest tests/ -q -n auto 실행 중" in capsys.readouterr().out


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
        "## [2026-08-06] feat | [[T-0547]] — 박제 (task:orch-dev-T0547)"
    )


def test_log_skeleton_without_task_uses_canonical_ticket_wikilink(tf):
    """정체성 태그와 무관하게 새 완료 entry의 ticket 참조는 canonical wikilink다."""
    skeleton = tf.build_log_skeleton(
        ticket_id="T-0547", title="박제", new_total=42, board_before=1, board_after=2,
        entry_type="feat", date="2026-08-06",
    )
    assert skeleton.splitlines()[0] == "## [2026-08-06] feat | [[T-0547]] — 박제"


@pytest.mark.parametrize("ticket_marker", ("[[T-0547]]", "T-0547"))
def test_log_entry_detection_accepts_canonical_and_legacy_ticket_markers(
    tf, tmp_path, ticket_marker,
):
    """선작성 canonical entry와 옛 raw-ID entry 모두 재실행 중복 방지 입력이다."""
    log_file = tmp_path / "log.md"
    log_file.write_text(
        f"## [2026-08-29] fix | {ticket_marker} — 완료\n\n- 실제 증거\n",
        encoding="utf-8",
    )
    assert tf.TicketFinisher(log_file=log_file)._log_has_entry("T-0547") is True
    assert tf.TicketFinisher(log_file=log_file)._log_has_entry("T-9999") is False


def test_dry_run_reports_existing_entry_skip_without_append_preview(
    tf, tmp_path, capsys,
):
    """선작성 entry가 있으면 dry-run과 actual이 같은 skip 판정을 내고 파일은 그대로다."""
    log_file = tmp_path / "log.md"
    original = "## [2026-08-29] fix | [[T-1234]] — 완료\n\n- 실제 증거\n"
    log_file.write_text(original, encoding="utf-8")
    finisher = _make_finisher(tf, tmp_path, affected=[])

    assert finisher.run("T-1234", section=None, dry_run=True) == 0

    out = capsys.readouterr().out
    assert "[dry-run] T-1234 스켈레톤 이미 있음 — append 건너뜀" in out
    assert "log/current.md 에 append 할 스켈레톤" not in out
    assert log_file.read_text(encoding="utf-8") == original


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
    """touches 를 가진 티켓 `T-0001` 을 tmp 홈에 쓰고 그 홈의 board.py 경로를 돌려준다(hermetic).

    조회는 board.py 위치(`<홈>/.project_manager/tools/board.py`)에서 만든 Home 의 티켓 디렉터리를 읽는다.
    """
    ticket = tmp_path / ".project_manager" / "wiki" / "tickets" / "open" / "T-0001-fake.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text(f"---\nid: T-0001\ntouches: {touches_repr}\n---\nbody\n", encoding="utf-8")
    return tmp_path / ".project_manager" / "tools" / "board.py"


def test_get_ticket_touches_strips_list_whitespace_and_drops_empty(tf, tmp_path):
    """get_ticket_touches — 리스트 원소 strip·빈/비-문자열 drop (silent-miss 방어·T-0081)."""
    board_py = _write_fake_board(tmp_path, "['src/a.py', ' src/b.py', '  ', 123]")
    assert tf.get_ticket_touches(board_py, "T-0001") == ["src/a.py", "src/b.py"]


def test_get_ticket_touches_strips_scalar_string(tf, tmp_path):
    """get_ticket_touches — 스칼라 문자열도 strip·빈 문자열 → []."""
    assert tf.get_ticket_touches(_write_fake_board(tmp_path, "'  src/x.py '"), "T-0001") == ["src/x.py"]
    assert tf.get_ticket_touches(_write_fake_board(tmp_path, "'   '"), "T-0001") == []


def _patch_domain(monkeypatch, fake):
    """affected_domain_titles 가 부르는 패키지 함수를 대역 객체의 같은 이름 메서드로 바꾼다."""
    monkeypatch.setattr(doc_page_store, "load_pages", lambda _domain_dir: fake.load_pages())
    monkeypatch.setattr(
        doc_affected, "pages_for_touches",
        lambda _home, touches, pages: fake.pages_for_touches(touches, pages))
    if hasattr(fake, "_real_git_runner"):
        monkeypatch.setattr(doc_staleness, "_real_git_runner", fake._real_git_runner)
    if hasattr(fake, "page_stale"):
        monkeypatch.setattr(
            doc_staleness, "page_stale",
            lambda _home, page, *, git_runner=None: fake.page_stale(page, git_runner=git_runner))
    if hasattr(fake, "_UNRESOLVED_GIT_RUNNER"):
        monkeypatch.setattr(doc_staleness, "_UNRESOLVED_GIT_RUNNER", fake._UNRESOLVED_GIT_RUNNER)


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

    _patch_domain(monkeypatch, _FakeDomain())
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
    _patch_domain(monkeypatch, _FakeDomain())
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
    _patch_domain(monkeypatch, _FakeDomain())
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

    _patch_domain(monkeypatch, _FakeDomain())
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    monkeypatch.setattr(
        repo_coordinates, "normalize_repo_path",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("normalizer를 로드하면 안 됨")),
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

    _patch_domain(monkeypatch, _FakeDomain())
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

    _patch_domain(monkeypatch, _FakeDomain())
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

    _patch_domain(monkeypatch, _FakeDomain())
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

    _patch_domain(monkeypatch, _FakeDomain())
    monkeypatch.setattr(
        freshness_lint, "_freshness_owner_repo",
        lambda home, repo: ((owner if repo == "upstream" else self_repo), None),
    )
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    result = tf.affected_domain_titles("T-0001", tf.BOARD_PY)
    assert result == [("외부", False), ("내부", False)]
    assert seen_repos == [owner, self_repo]
    assert seen_runners[0] is not seen_runners[1]


def test_affected_domain_titles_unresolved_owner_skips_stale_soft_warning(
        tf, tmp_path, monkeypatch, capsys):
    """미해소 owner는 sentinel으로 stale fallback을 막아 soft 경고도 내지 않는다."""
    sentinel = object()

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

    _patch_domain(monkeypatch, _FakeDomain())
    monkeypatch.setattr(
        freshness_lint, "_freshness_owner_repo",
        lambda home, _repo: (None, "owner checkout unavailable"),
    )
    monkeypatch.setattr(tf, "get_ticket_touches", lambda bp, tid: ["src/x.py"])
    affected = tf.affected_domain_titles("T-0001", tf.BOARD_PY)

    assert affected == [("미해소 외부", None)]
    _make_finisher(tf, tmp_path, affected=affected)._notify_affected_domain("T-0001")
    assert "⚠ 미해소 외부" not in capsys.readouterr().out


# ── self-host 회귀 cwd 런타임 해소 (T-0149 — ② 홈서 worktree 회귀) ─────────────
#
# 분리된 PM 홈(②·ADR-0027)엔 tests/ 가 없으므로, ② 홈 cwd 에서 ticket_finish 를 돌리면
# 회귀가 활성 worktree 슬롯(①·tests/)에서 돌아야 한다. `__init__` 즉시-REPO-고정 버그를
# 제거하고 `_default_run_pytest` 가 런타임에 `_regression_cwd()` 로 해소한다. ticket_finish
# `_regression_cwd` wrapper 는 `slot_resolution._regression_cwd`(T-0124·`_auto_slot`
# 재사용)에 위임한다.
#
# seam 주입: `slot_resolution._regression_cwd` 를 areas/leases 를 *명시 인자로* 넘기는 lambda 로
# 감싸 주입한다(실 장부·areas 미접촉). 또 REPO
# 자체가 `.../work/project_manager_1`(① worktree 에서 pytest 실행)이라 그 suffix 로 self-host
# 를 단언하면 REPO 폴백과 우연 일치한다 — 슬롯명을 `work/myrepo_7` 로 둬 구조적으로 구별한다.

import json as _selfhost_json  # noqa: E402 — T-0149 테스트 전용 로컬 import

from pm_engine.app import board_sync
from pm_engine.infra import ticket_store


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
    """`slot_resolution._regression_cwd` 를 areas/leases(+repo_root) 명시 인자 lambda 로 감싸 주입.

    원본 `_regression_cwd` 를 닫아 Home 과 `worktree_slot` 만 받고 areas/leases/repo_root 를
    *명시 인자로* 넘기는 lambda 로 교체한다 — 실 장부/areas/REPO 미접촉 hermetic 해소의 seam 이다.
    테스트가 고정한 repo_root 가 있으면 그것이 우선이고(hermetic 앵커), 없으면 ticket_finish
    wrapper 가 넘긴 Home 의 root(자기 REPO)를 쓴다.
    """
    real = slot_resolution._regression_cwd
    monkeypatch.setattr(
        slot_resolution, "_regression_cwd",
        lambda _home, worktree_slot=None, *, repo_root=None, _pinned=repo_root: real(
            _home, worktree_slot, areas, leases,
            repo_root=_pinned if _pinned is not None else repo_root),
    )


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
# 도구는 언제나 `<root>/.project_manager/tools/` 에 설치된다 — `pm_import` 가 그 깊이를 못 박으
# 므로 REPO 는 상향 탐색이 아니라 `Path(__file__).resolve().parents[2]` 다. 상향 탐색은 합성
# 트리에서 자기 위의 실 인스턴스를 답으로 주는 부작용만 냈다 (additional_reviewer 동형).
#
# hermetic seam: 헬퍼는 모듈 전역 `__file__` 을 읽으므로 fresh 모듈 인스턴스의 `__file__` 을
# tmp 합성 경로로 monkeypatch 해 실제 파일 이동 없이 임의 깊이를 모사한다.


def test_repo_root_is_the_own_root_not_the_enclosing_home(tf, tmp_path, monkeypatch):
    """중첩 `.project_manager`(PM 홈 안 worktree·ADR-0027) → 자기 루트를 낸다(바깥 홈 아님)."""
    outer = tmp_path / "home"
    inner = outer / "work" / "wt1"
    (outer / ".project_manager").mkdir(parents=True)
    tools = inner / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    monkeypatch.setattr(tf, "__file__", str(tools / "ticket_finish.py"))
    assert tf._find_repo_root() == inner


def test_repo_constant_anchors_at_the_install_depth(tf):
    """모듈 상수 REPO 는 도구 자기 위치에서 나온다 — 하위 파생 경로가 함께 따라온다."""
    expected = Path(tf.__file__).resolve().parents[2]
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
# `--no-pytest` 는 **회귀 실행만** skip 한다(코드 트리 해소는 그대로 — 사설 참조 측정·stage 가
# 같은 트리를 쓴다). 다중슬롯인데 미지정+진짜 모호면 조용한 false-red 대신 fail-loud 로 중단한다.
# `slot_resolution._resolve_explicit_identity_slot`(M3 조인검증) 재사용(DRY).


def _bind_pm_handoff_repo(tf, monkeypatch, repo_root: Path):
    """ticket_finish 의 `REPO` 를 `repo_root` 로 monkeypatch 한다 — wrapper 가 이 앵커로 만든 Home 을
    넘기므로 `_resolve_explicit_identity_slot`(M3 리스 조인검증)·`_regression_cwd`(L1) 의
    leases_file/repo_root 해소가 이 tmp_path 를 본다(hermetic·실 장부·실 REPO 미접촉)."""
    monkeypatch.setattr(tf, "REPO", repo_root)


def test_repo_slot_resolves_explicit_worktree_slot(tf, tmp_path, monkeypatch):
    """(a) --repo/--slot → explicit worktree 슬롯 `work/<repo>_<N>` 결정론적 해소.

    실 `slot_resolution` 을 쓰되 리스 장부 부재(`_bind_pm_handoff_repo` 의 tmp_path repo_root)라
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
    무인자 경로의 해소 결과와 무관하게 explicit 경로가 short-circuit 한다.
    """
    monkeypatch.setattr(slot_resolution, "_resolve_explicit_identity_slot",
                        lambda _home, repo, slot, leases_file=None: (f"work/{repo}_{slot}", None))
    slot, err = tf._resolve_finish_slot("project_manager", 2)
    assert err is None            # 명시했으므로 모호 아님(short-circuit)
    assert slot == "work/project_manager_2"


# ── M3(라이더) — 세션↔repo 조인 불일치 fail-loud (`slot_resolution._resolve_explicit_identity_slot` 위임) ──

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


def test_main_unresolved_identity_fails_loud_before_regression(tf, monkeypatch, capsys):
    """(c) 무인자 진입(`--task` 없음) → usage error(rc 2)·`--task` 처방·회귀 미시작.

    fail-loud 는 run() 진입 *전*이라 회귀가 시작조차 안 된다([1/5] 미출력). 정상 종결의 정체성은
    `--task` 하나이고 cwd·단일 활성 task 에서 유도하지 않는다(T-0962 F-008).
    """
    with pytest.raises(SystemExit) as exc:
        tf.main(["T-1234"])
    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "--task" in captured.err
    assert "[1/5]" not in captured.out  # run()/회귀 진입 전 중단


def _capture_main_forward(tf, monkeypatch) -> dict:
    """`main` 이 해소해 넘긴 값을 부작용 없이 캡처한다.

    완료 기록 단위는 묶음 하나다(티켓 하나 = 크기 1) — 실행 좌표(세션·정체성·회귀 skip)는 종결
    파이프라인 생성 인자로 간다. 코드 트리는 `main` 이 정하지 않는다(종결이 묶음 기록의 브랜치로
    찾는다) — `TicketFinisher` 생성 인자는 그 사실을 보는 관측점이다. 파이프라인이 그 값을 완료
    기록 단계로 그대로 넘기는 것은 묶음 종결 회귀가 값으로 고정한다 — 여기서는 `main` 의
    해소·forward 만 본다.
    """
    captured: dict = {}

    class _SpyFinisher(tf.TicketFinisher):
        def __init__(self, **kw):
            captured["finisher_kwargs"] = dict(kw)
            super().__init__(**kw)

    class _SpyCloser:
        def __init__(self, cluster, **kw):
            captured["cluster"] = cluster
            captured.update(kw)

        def run(self) -> int:
            return 0

    monkeypatch.setattr(tf, "TicketFinisher", _SpyFinisher)
    monkeypatch.setattr(tf, "ClusterCloser", _SpyCloser)
    return captured


# ── board 정체성 forward (T-0781) ───────────────────────────────────────────
#
# board 의 complete 는 `claimed_by` 와 실행 세션을 대조한다. ticket_finish 는 자기 정체성을
# 이미 해소해 두고도 board 에 넘기지 않았다 — 그 상태로 소유 게이트가 켜지면 다중 슬롯
# 채택자의 `/pm-wave-finish` 주 경로가 "남의 티켓" 으로 전부 거부된다.


def test_main_no_pytest_still_gates_unresolved_identity(tf, monkeypatch, capsys):
    """--no-pytest + `--task` 없음 → usage error(rc 2) + `--task` 처방(회귀 skip 으로 풀리지 않는다).

    옛 규칙은 `--no-pytest` 면 슬롯 해소 자체를 건너뛰었다 — 회귀 cwd 가 pytest 전용 값이던
    시점의 결정이다. 그 뒤 같은 해소 결과(코드 트리)를 사설 참조 측정·[4/5] stage·PM-direct
    재검이 소비하게 됐으므로, 회귀 skip 이 해소를 우회하면 그 소비자들이 PM 홈(분리 형상에선
    엔진 import 사본)을 잰다. 그래서 회귀 skip 과 트리 해소를 분리했고, 모호를 푸는 수단은
    `--repo/--slot`·`--task` 명시뿐이다.
    """
    resolve_calls: list = []

    def _spy_resolve(repo, slot):  # 무인자 진입은 이 갈래로 오지 않는다(정체성=task).
        resolve_calls.append((repo, slot))
        return None, None

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
    with pytest.raises(SystemExit) as exc:
        tf.main(["T-1234", "--no-pytest"])
    err = capsys.readouterr().err
    assert exc.value.code == 2              # `--task` 없음은 회귀 skip 으로 풀리지 않는다
    assert resolve_calls == []              # 슬롯 자동해소 갈래 자체가 없다
    assert captured == {}                   # TicketFinisher 생성 전에 중단(부작용 0)
    assert "--task" in err


def test_main_no_pytest_forwards_the_skip_but_no_code_tree(tf, monkeypatch):
    """--no-pytest + task 정체성 → 회귀 skip 만 넘기고 트리 인자는 없다(정체성은 task 그대로).

    코드 트리는 종결이 묶음 기록의 브랜치로 찾아 넘기므로 회귀 skip 여부와 무관하게 같은 트리다.
    정상 종결의 정체성은 `--task` 다 — 슬롯 세션·슬롯 키를 넘기지 않는다(T-0962).
    """
    captured = _capture_main_forward(tf, monkeypatch)

    rc = tf.main(["T-1234", "--no-pytest", "--task", "alpha"])
    assert rc == 0
    assert captured["finisher_kwargs"] == {}
    assert captured["skip_pytest"] is True   # [1/5] 회귀만 skip
    assert captured["task"] == "alpha"
    assert captured["board_identity_args"] == ["--task", "alpha"]
    assert captured["slot"] is None
    assert "session" not in captured


def test_main_slot_without_repo_rejected(tf, capsys):
    """--slot 단독(--repo 없음) — identity_args.parse_identity 가 ValueError → parser.error(fail-loud).

    (pm_handoff `--slot` 단독 거부 동형 — ADR-0057 uniform 규칙.)
    """
    with pytest.raises(SystemExit):
        tf.main(["T-1234", "--slot", "4"])
    assert "--repo" in capsys.readouterr().err


# ── engine_written_paths — rounds/T-NNNN/ stage 후보 (T-0753·ADR-0090 R5) ───────

class _FakeRoundsBoard:
    """`engine_written_paths` 전용 최소 board 대역 — 그 함수가 실제로 읽는 속성만 노출한다."""

    def __init__(self, tickets_dir: Path, ticket_path: Path, *, board_git_enabled=False):
        self._tickets_dir = tickets_dir
        self._ticket_path = ticket_path
        self._board_git_enabled_value = board_git_enabled

    def find_ticket(self, ticket_id):
        return ("claimed", self._ticket_path)

    def _board_git_enabled(self) -> bool:
        return self._board_git_enabled_value

    def ticket_cluster(self, ticket_id, frontmatter):
        return frontmatter.get("cluster") or f"C-{ticket_id}"

    def cluster_ledger_path(self, cluster):
        name = cluster if cluster.startswith("C-") else f"C-{cluster}"
        return self._tickets_dir / "clusters" / f"{name}.md"


def _rounds_home(tf, tmp_path, monkeypatch) -> tuple[Path, Path]:
    """`tf.REPO` 를 tmp 홈에 묶고 그 Home 의 티켓 디렉터리와 묶음 소속 티켓 파일을 만든다."""
    monkeypatch.setattr(tf, "REPO", tmp_path)
    tickets_dir = tmp_path / ".project_manager" / "wiki" / "tickets"
    ticket_path = tickets_dir / "claimed" / "T-0753-fake.md"
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text("---\nid: T-0753\ncluster: C-fake\n---\n", encoding="utf-8")
    return tickets_dir, ticket_path


def test_engine_written_paths_includes_existing_round_files(tf, tmp_path, monkeypatch):
    """rounds/<id>/ 아래 실존하는 라운드 파일이 stage 후보에 들어간다 (디렉터리 통째가 아니라
    파일 단위)."""
    tickets_dir, ticket_path = _rounds_home(tf, tmp_path, monkeypatch)
    round_dir = tickets_dir / "rounds" / "T-0753"
    round_dir.mkdir(parents=True)
    dev_round = round_dir / "01-developer.md"
    review_round = round_dir / "02-code-reviewer.md"
    dev_round.write_text("dev\n", encoding="utf-8")
    review_round.write_text("review\n", encoding="utf-8")
    board = _FakeRoundsBoard(tickets_dir, ticket_path)

    paths = tf.engine_written_paths(pm_paths.home_at(tmp_path), "T-0753", tmp_path / "log.md")

    assert dev_round in paths
    assert review_round in paths
    assert round_dir not in paths  # 디렉터리 자체는 후보로 들어가지 않는다


def test_engine_written_paths_excludes_rounds_when_board_git_separated(tf, tmp_path, monkeypatch):
    """board-git 분리 형상에선 티켓과 함께 라운드도 stage 후보에서 빠진다(현행 티켓 제외 규칙
    동일)."""
    tickets_dir, ticket_path = _rounds_home(tf, tmp_path, monkeypatch)
    round_dir = tickets_dir / "rounds" / "T-0753"
    round_dir.mkdir(parents=True)
    (round_dir / "01-developer.md").write_text("dev\n", encoding="utf-8")
    (tickets_dir.parent / ".git").mkdir()
    board = _FakeRoundsBoard(tickets_dir, ticket_path)

    paths = tf.engine_written_paths(pm_paths.home_at(tmp_path), "T-0753", tmp_path / "log.md")

    assert paths == [tmp_path / "log.md"]


def test_engine_written_paths_excludes_non_round_and_temporary_files_by_grammar(
        tf, tmp_path, monkeypatch):
    """라운드 판정은 `parse_round_filename` 문법 하나 — 점-접두 임시 잔여뿐 아니라 라운드가
    아닌 `.md`(`notes.md`·`01-dev.md`)도 stage 후보 밖이고, 진짜 라운드만 후보에 남는다
    (리뷰 F-002 — `.md` 접미 + 점-접두 제외 조합으로 되돌리면 `notes.md`·`01-dev.md`가 다시
    후보에 실려 이 테스트가 red 가 된다)."""
    tickets_dir, ticket_path = _rounds_home(tf, tmp_path, monkeypatch)
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

    paths = tf.engine_written_paths(pm_paths.home_at(tmp_path), "T-0753", tmp_path / "log.md")

    assert dev_round in paths
    for name in non_round_names:
        assert round_dir / name not in paths


def test_round_stage_candidates_uses_round_filename_grammar_not_ad_hoc_suffix(
        tf, tmp_path):
    """`_round_stage_candidates` 는 자체 접미/접두 조합이 아니라 `parse_round_filename` 문법
    단일 소유자에만 위임한다 (리뷰 F-001 — reviewer 실측: 9개 이름 배치 중 자체 조합은 4건을
    후보에 실렸는데 진짜 라운드는 2건뿐이었다)."""
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

    candidates = tf._round_stage_candidates(tickets_dir, "T-0753")

    assert candidates == [
        round_dir / "01-developer.md", round_dir / "02-code-reviewer.md",
    ]


def test_stage_scope_real_git_add_stages_untracked_new_round_file(tf, tmp_path, monkeypatch):
    """untracked 신규 라운드 파일이 실 git repo 에서 `git add` 로 실제 stage 된다 (리뷰 F-005 —
    후보 산출뿐 아니라 real git 경로까지 왕복). board 대역은 조회 두 자리(`find_ticket`·
    `_board_git_enabled`)만 대역하고, `git_scope_stage_pathspec` 자체는 실
    board.py 것을 그대로 빌려 real git 서브프로세스(status/ls-files/check-ignore)를 태운다."""
    repo = tmp_path / "repo"
    tickets_dir = repo / ".project_manager" / "wiki" / "tickets"
    ticket_path = tickets_dir / "claimed" / "T-0753-fake.md"
    ticket_path.parent.mkdir(parents=True)
    # 종결은 티켓 frontmatter 로 소속 장부를 찾는다 — 손상 티켓은 이제 그대로 실패한다([[T-0959]]).
    ticket_path.write_text("---\nid: T-0753\nstatus: claimed\n---\n\nticket\n", encoding="utf-8")
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
        "ticket_cluster = _real_board.ticket_cluster\n"
        "def cluster_ledger_path(cluster):\n"
        "    return _TICKETS_DIR / 'clusters' / (cluster if cluster.startswith('C-') else 'C-' + cluster)\n"
        f"_TICKET_PATH = _Path({str(ticket_path)!r})\n"
        f"_TICKETS_DIR = _Path({str(tickets_dir)!r})\n"
        "def find_ticket(ticket_id):\n"
        "    return ('claimed', _TICKET_PATH)\n"
        "def _board_git_enabled():\n"
        "    return False\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tf, "REPO", repo)

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
    _git_seed(worktree)
    (home / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    # add 는 작업 트리에 있는 파일만 받는다 — 선언 변경 파일을 실제로 둔다.
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "test_real_change.py").write_text("changed = 1\n", encoding="utf-8")
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
        # 잔여 preflight 는 별도 관심사 — 이 테스트의 초점은 두-repo stage 분리·[4/5]
        # 사후 loud 보고이지 새 preflight 판정이 아니다. off로 그 초점만 남긴다.
        residual_block_fn=lambda ticket_id: None,
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
    _git_seed(worktree)
    (home / "home.md").write_text("home\n", encoding="utf-8")
    (worktree / "code.py").write_text("code = 1\n", encoding="utf-8")
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


def test_single_ticket_completion_never_deletes_a_branch(tf, tmp_path, monkeypatch, capsys):
    """티켓 하나의 완료 기록은 브랜치를 지우지 않는다 — 브랜치 정리는 슬롯 제거의 몫이다.

    종결도 브랜치를 지우지 않는다(작업 트리가 묶음 브랜치를 쥔 채 머지에 닿으므로 삭제는 늘
    거부됐다 — T-0925 로 그 시도를 지웠다). 완료 기록 경로가 브랜치 삭제를 발행하면 사람이 아직
    쓰고 있는 브랜치를 지우려 든다. 픽스처 트리는 `init_git_repo` 로 자기 저장소를 선언한다(실
    저장소 무접촉).
    """
    home = tmp_path / "pm-home"
    worktree = home / "work" / "project_manager_1"
    worktree.mkdir(parents=True)
    init_git_repo(worktree, commit="seed")
    (home / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    monkeypatch.setattr(tf, "REPO", home)

    def fake_scope(ticket_id, board_py, log_file, run_git, *, repo=None,
                   include_touches=True, include_engine_outputs=True,
                   touches_workspace=None, touches=None):
        if include_engine_outputs:
            return tf.StageScope((".project_manager/wiki/log/current.md",), None)
        return tf.StageScope(("tests/test_real_change.py",), None)

    monkeypatch.setattr(tf, "stage_scope", fake_scope)
    mutations: list[list[str]] = []
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (0, "1 passed in 0.1s"),
        run_board_fn=lambda args: (0, "board ok"),
        run_git_fn=lambda args: (mutations.append(list(args)) or (0, "")),
        run_git_at_fn=lambda cwd, args: (mutations.append(list(args)) or (0, "")),
        board_count_fn=lambda: 0,
        ticket_title_fn=lambda ticket_id: "no branch delete",
        affected_domain_fn=lambda ticket_id: None,
        status_entries_fn=lambda: (),
        status_entries_at_fn=lambda cwd: (),
        residual_block_fn=lambda ticket_id: None,
        log_file=home / ".project_manager/wiki/log/current.md",
        task_workspace=worktree,
    )

    assert finisher.run("T-0902", section=None, dry_run=False, skip_pytest=True) == 0
    assert mutations, "git seam 이 한 번도 안 불렸다(공허한 단언)"
    assert not any(args[:1] == ["branch"] for args in mutations), \
        f"완료 기록이 브랜치를 건드렸다: {mutations!r}"
    capsys.readouterr()


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
    finisher = tf.TicketFinisher(
        run_git_at_fn=mutation_runner,
        run_git_stdout_at_fn=stdout_runner,
        task_workspace=worktree,
    )

    assert finisher._code_stage_scope("T-0437", worktree) == tf.StageScope(("tests/new.py",), None)
    assert finisher._default_status_entries_at(worktree) == (("??", "tests/new.py"),)
    assert [args[0] for _, args in parser_calls] == ["ls-files", "status"]


def _write_coordinate_stage_board(monkeypatch, tmp_path, touches: list[str]) -> Path:
    """touches 를 가진 티켓 `T-0473` 과 file-only stage primitive 를 제공하는 hermetic board 홈.

    티켓 조회는 board.py 위치(`<홈>/.project_manager/tools/board.py`)에서 만든 Home 을 읽는다.
    """
    monkeypatch.setattr(git, "git_scope_stage_pathspec",
                        lambda repo, declared, run_git=None: [p.relative_to(repo).as_posix() for p in declared if p.is_file()])
    pm = tmp_path / "board-home" / ".project_manager"
    ticket = pm / "wiki" / "tickets" / "open" / "T-0473-fake.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(f"---\nid: T-0473\ntouches: {json.dumps(touches)}\n---\nbody\n",
                      encoding="utf-8")
    path = pm / "tools" / "board.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"ENGINE_REV = {_real_engine_rev()!r}\n"
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
        monkeypatch, tmp_path,
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
        monkeypatch, tmp_path,
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
    board_py = _write_coordinate_stage_board(monkeypatch, tmp_path, [touch])
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
        monkeypatch, tmp_path, ["work/project_manager_1/../../outside.py"])
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


# ── 진입 게이트 공용 대역 ───────────────────────────────────────────────────


def _boom_pytest():
    raise AssertionError("차단된 완료는 회귀를 돌리지 않는다")


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
    external = tf._load_additional_reviewer()
    assert external is not None
    assert hasattr(pm_paths, "AnchorResolutionError")
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
        # 잔여 preflight 는 board 사본을 적재하는 묶음 장부 조회를 태운다 — 이 fixture 의 board_py 는
        # ENGINE_REV 가 없는 손상 사본이라 그 적재 자체가 skew fail-loud 다. 이 테스트의 초점은
        # frontmatter 손상 폴백이라 off 로 그 관심사만 남긴다.
        residual_block_fn=lambda tid: None,
        log_file=tmp_path / "log.md",
        board_py=board_py,
    )

    assert finisher.run("T-9001", section=None, dry_run=False) == 0
    assert completed == [["complete", "T-9001", "--tests-pass"]]
    assert "T-9001" in (tmp_path / "log.md").read_text(encoding="utf-8")


def test_measurement_anchor_stops_without_the_anchor_symbol(tf, tmp_path, monkeypatch):
    """사본은 있으나 앵커 seam 이 없는 구형/부분 설치는 멈춘다 — 폭을 물을 수 없다.

    기준점 없이 잰 값으로 판정하면 같은 게이트가 설치 상태에 따라 다른 폭을 재게 되므로,
    가드 off 가 아니라 정지다."""
    external = tf._load_additional_reviewer()
    assert external is not None
    monkeypatch.delattr(external, "integration_anchor", raising=False)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md")

    with pytest.raises(tf._CloseObservationFailure) as caught:
        finisher._measurement_anchor("T-1234", external)

    assert "integration_anchor 부재" in str(caught.value)


# ── 측정 스코프 정규화 · 기계 mirror 제외 (T-0601 ①⑧) ────────────────────────
#
# ⑧ PM 홈 좌표 touches(`work/<repo>_<N>/…`)를 그대로 재면 측정 트리에 그 경로가 없어 diff 가 0 이
#    나온다. 정규화는 stage 경로와 **같은** `repo_coordinates` seam 이다.
# ① 기계 mirror 제외는 additional_reviewer 측정 seam 이 소유한다 — 완료 기록은 그 판정을 빌려 쓴다.


def test_measured_touches_normalizes_pm_home_coordinates(tf, tmp_path, monkeypatch):
    """PM 홈 좌표는 측정 트리 좌표로 정규화된다 (stage 경로와 같은 규칙·사본 0)."""
    workspace = tmp_path / "work" / "proj_1"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(tf, "REPO", tmp_path)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", task_workspace=workspace)
    assert finisher._normalize_measured_touches(
        ["work/proj_1/.project_manager/tools/board.py", "work/proj_1/tests/"]) == [
        ".project_manager/tools/board.py", "tests"]


def test_measured_touches_leaves_plain_repo_paths_untouched(tf, tmp_path, monkeypatch):
    """접두 없는 채택자 형상은 normalizer 를 타지 않는다 (경량 판정·현행 무변경)."""
    monkeypatch.setattr(repo_coordinates, "normalize_repo_paths",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("normalizer 미호출")))
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md")
    assert finisher._normalize_measured_touches(
        [".project_manager/tools/board.py"]) == [".project_manager/tools/board.py"]


def test_measured_touches_slot_mismatch_is_unresolved(tf, tmp_path, monkeypatch):
    """슬롯 불일치는 정규화 불능(None)이다 — 조용한 diff 0 으로 접지 않는다."""
    workspace = tmp_path / "work" / "proj_1"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(tf, "REPO", tmp_path)

    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", task_workspace=workspace)
    assert finisher._normalize_measured_touches(["work/other_2/tools/board.py"]) is None


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


def test_dod_preflight_runs_after_the_private_ref(tf, tmp_path, capsys):
    """진입 게이트 순서 = 사설 참조 → DoD (앞선 거절이 먼저·안내 1개만 뜬다)."""
    finisher = tf.TicketFinisher(
        run_pytest_fn=_boom_pytest,
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: [],
        private_ref_block_fn=lambda tid: "사설 참조 차단",
        dod_block_fn=lambda tid: (_ for _ in ()).throw(
            AssertionError("사설 참조 차단 뒤에는 DoD 판정을 묻지 않는다")),
        log_file=tmp_path / "log.md",
    )
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert "사설 참조 차단" in capsys.readouterr().err


def test_dry_run_is_also_refused_by_the_dod_preflight(tf, tmp_path):
    """dry-run 도 같은 거절이다 — 미리보기는 '부작용 없음'이지 '게이트 면제'가 아니다."""
    finisher = _dod_finisher(tf, tmp_path, dod_block="DoD 미마감")
    assert finisher.run("T-1234", section=None, dry_run=True) == 1


# ── 잔여(코드 트리 dirty ⊄ 선언 스코프) preflight ──────────────────
#
# 경고→차단: DoD preflight 와 같은 순서 결함(차단이 [4/5] 안에만 있으면 board complete 가 이미
# 기록된 뒤에야 막혀 되돌릴 수 없다)을 같은 방식(진입 게이트로 승격)으로 닫는다.


def _residual_finisher(tf, tmp_path, *, residual_block, log_file: Path | None = None):
    """잔여 preflight 만 살아 있는 finisher — `_dod_finisher` 동형(나머지 부작용 seam 폭발 대역)."""
    return tf.TicketFinisher(
        run_pytest_fn=_boom_pytest,
        run_board_fn=lambda args: (_ for _ in ()).throw(AssertionError("board 미호출")),
        run_git_fn=lambda args: (_ for _ in ()).throw(AssertionError("git 미호출")),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: [],
        dod_block_fn=lambda tid: None,
        residual_block_fn=lambda tid: residual_block,
        log_file=log_file if log_file is not None else tmp_path / "log.md",
    )


def test_residual_preflight_blocks_before_the_log_skeleton(tf, tmp_path, capsys):
    """잔여 차단 → rc 1 · 회귀/log/board/git 어느 것도 건드리지 않는다(complete 미실행 · DoD)."""
    log_file = tmp_path / "log.md"
    finisher = _residual_finisher(
        tf, tmp_path, log_file=log_file,
        residual_block="완료 기록 거부 — T-1234 코드 트리에 선언 스코프 밖 변경이 남아 있다.")
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert not log_file.exists(), "차단된 실행이 stray 스켈레톤을 남겼다"
    err = capsys.readouterr().err
    assert "[중단]" in err and "T-1234" in err


def test_residual_preflight_runs_after_the_dod_block(tf, tmp_path, capsys):
    """진입 게이트 순서 = 사설 참조 → DoD → 잔여(더 넓은/앞선 거절이 먼저·안내 1개만 뜬다)."""
    finisher = tf.TicketFinisher(
        run_pytest_fn=_boom_pytest,
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: [],
        dod_block_fn=lambda tid: "DoD 미마감 — T-1234",
        residual_block_fn=lambda tid: (_ for _ in ()).throw(
            AssertionError("DoD 차단 뒤에는 잔여 판정을 묻지 않는다")),
        log_file=tmp_path / "log.md",
    )
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert "DoD 미마감" in capsys.readouterr().err


def test_preflight_seam_order_is_private_ref_dod_residual_self_axis(tf, tmp_path):
    """네 진입 게이트 seam 의 호출 배열을 값으로 고정한다 — private_ref → dod → residual →
    self_axis. residual 이 차단하면 self_axis(poison)는 **불리지 않는다**(순서가 값으로
    확인되지 않으면 이후 게이트 삽입이 뒤로 밀려도 코드 리뷰로만 잡힌다)."""
    calls: list[str] = []
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("회귀 미호출이어야 한다")),
        run_board_fn=lambda args: (_ for _ in ()).throw(AssertionError("board 미호출")),
        run_git_fn=lambda args: (_ for _ in ()).throw(AssertionError("git 미호출")),
        private_ref_block_fn=lambda tid: calls.append("private_ref") or None,
        dod_block_fn=lambda tid: calls.append("dod") or None,
        residual_block_fn=lambda tid: calls.append("residual") or "잔여 1건 — T-1234",
        self_axis_block_fn=lambda tid: calls.append("self_axis") or None,
        log_file=tmp_path / "log.md",
    )
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert calls == ["private_ref", "dod", "residual"], (
        "residual 차단인데 self_axis 가 불렸다"
    )


def test_residual_preflight_dry_run_is_also_refused(tf, tmp_path):
    """dry-run 도 같은 거절이다 — 미리보기는 게이트 면제가 아니다."""
    finisher = _residual_finisher(tf, tmp_path, residual_block="잔여 1건")
    assert finisher.run("T-1234", section=None, dry_run=True) == 1


def test_default_residual_block_treats_scope_error_as_a_block(tf, tmp_path):
    """`scope_error` 는 '판정 불가'가 아니라 차단이다 — 이 실행은 아무것도 stage 못한다는
    확정 사실이라, 여기서 fail-soft 하면 전량 미머지가 조용히 통과한다."""
    finisher = tf.TicketFinisher(
        stage_scope_fn=lambda tid: tf.StageScope((), "board 모듈을 로드하지 못했다"),
        status_entries_fn=lambda: (),
        log_file=tmp_path / "log.md",
    )
    block = finisher._default_residual_block("T-1234")
    assert block is not None
    assert "스코프를 산출하지 못했다" in block and "board 모듈을 로드하지 못했다" in block


def test_default_residual_block_passes_when_nothing_is_dirty(tf, tmp_path):
    """스코프 밖 잔여가 전혀 없으면 None — 정상 완료 흐름은 무영향(역방향 회귀)."""
    finisher = tf.TicketFinisher(
        stage_scope_fn=lambda tid: tf.StageScope(("src/a.py",), None),
        status_entries_fn=lambda: (("M ", "src/a.py"),),
        log_file=tmp_path / "log.md",
    )
    assert finisher._default_residual_block("T-1234") is None


def test_residual_preflight_ignores_the_pm_home_plan_in_separated_configuration(
        tf, tmp_path, monkeypatch, capsys):
    """분리 형상(두-git)에서 PM 홈 산출물 계획(cwd=REPO)의 잔여는 판정에 안 들어간다 — 다른 PM
    세션의 wiki WIP 로 내 완료가 막히지 않는다(I2 · 두-계획 픽스처는
    `test_task_stage_plan_separates_pm_outputs_and_worktree_touches` 재사용).
    """
    home = tmp_path / "pm-home"
    worktree = home / "work" / "project_manager_1"
    worktree.mkdir(parents=True)
    _git_seed(worktree)
    (home / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    monkeypatch.setattr(tf, "REPO", home)

    def fake_scope(ticket_id, board_py, log_file, run_git, *, repo=None,
                   include_touches=True, include_engine_outputs=True,
                   touches_workspace=None, touches=None):
        repo = Path(repo or home)
        if include_engine_outputs:
            return tf.StageScope((".project_manager/wiki/log/current.md",), None)
        return tf.StageScope(("tests/test_real_change.py",), None)

    monkeypatch.setattr(tf, "stage_scope", fake_scope)
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("--no-pytest must skip")),
        run_board_fn=lambda args: (0, "board ok"),
        run_git_fn=lambda args: (0, ""),
        run_git_at_fn=lambda cwd, args: (0, ""),
        board_count_fn=lambda: 0,
        ticket_title_fn=lambda ticket_id: "separated plans",
        affected_domain_fn=lambda ticket_id: None,
        # 홈 계획은 스코프 밖 잔여 1건(남의 wiki WIP) · 코드 트리 계획은 깨끗하다.
        status_entries_fn=lambda: (("M ", ".project_manager/wiki/roadmap.md"),),
        status_entries_at_fn=lambda cwd: (),
        log_file=home / ".project_manager/wiki/log/current.md",
        task_workspace=worktree,
    )
    assert finisher.run("T-0437", section=None, dry_run=False, skip_pytest=True) == 0


def _write_dod_board(tmp_path, body: str) -> Path:
    """명시 Home의 티켓을 준비한다. 실행 코드는 패키지가 소유한다."""
    ticket = tmp_path / ".project_manager/wiki/tickets/claimed/T-1234-dod.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text("---\nid: T-1234\n---\n" + body, encoding="utf-8")
    return tmp_path / ".project_manager/tools/board.py"


def test_default_dod_preflight_consumes_the_board_gate(tf, tmp_path):
    """기본 seam 은 board 의 DoD 판정을 그대로 부른다 (규칙 사본 0·값 형식도 board 것)."""
    board_py = _write_dod_board(
        tmp_path, "## 완료 조건\n- [x] 한 건\n- [ ] 남은 항목\n")
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", board_py=board_py)

    block = finisher._default_dod_block("T-1234")
    assert block is not None
    assert "T-1234" in block and "남은 항목" in block
    assert "(이월: <사유·귀속>)" in block                  # 값 형식은 board 소유분
    assert "board.py complete 의 게이트와 **같은 판정**" in block


def test_default_dod_preflight_passes_a_closed_ticket(tf, tmp_path):
    """전항 마감이면 None — 정상 완료 흐름은 무영향."""
    board_py = _write_dod_board(
        tmp_path, "## 완료 조건\n- [x] 한 건\n- [>] 이월분 (이월: v1.7.1·T-0602)\n")
    finisher = tf.TicketFinisher(log_file=tmp_path / "log.md", board_py=board_py)
    assert finisher._default_dod_block("T-1234") is None




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
        dod_block_fn=lambda _tid: None,
        # 잔여 preflight 는 별도 관심사 — 이 fixture의 scope=() + dirty 1건은 PM-direct
        # 재검용 대역일 뿐 잔여 판정 대상이 아니다. off로 이 테스트의 초점(경고가 rc를 절대
        # 안 바꾼다)만 남긴다.
        residual_block_fn=lambda _tid: None,
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
    expansion = ticket_rules.TouchFileExpansion(
        ("src/a.py", "src/b.py", "src/c.py"), (),
    )
    monkeypatch.setattr(tier_signals, "expand_owned_touch_files", lambda _repo, _touches: expansion)
    finisher = tf.TicketFinisher(
        board_py=tf.BOARD_PY,
        task_workspace=tmp_path,
        status_entries_at_fn=lambda _cwd: (),
    )

    finisher._warn_pm_direct_conditions("T-0677")
    err = capsys.readouterr().err
    assert "PM-direct 조건 (a) 위반" in err
    assert "3개 파일" in err


# ── 완료 기록 변경량 상한 없음 (T-0950) ─────────────────────────────────────
#
# 작업 크기별 변경 줄 수 상한은 사용자 결정(2026-10-10)으로 지웠다. 아래 형상은 실 git 저장소 +
# 엔진 사본(claim · 묶음 장부 · 통합 브랜치)으로 옛 small 상한(300줄)을 넘는 변경을 만들고, 진입
# 게이트의 측정 소비자(사설 참조)가 그대로 도는 채로 완료 기록이 끝나는지 본다.

_WAVE_INTEGRATION = "task/main"   # 묶음 장부가 선언하는 통합 브랜치(판정 기준)
_WAVE_TICKET = "T-7301"
_WAVE_CHANGED_LINES = 400      # claim 뒤 `src/` 에 더한 줄 수 — 옛 small 상한 300 초과


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


def _wave_repo(tmp_path: Path, *, git: bool = True,
               base_branch: str | None = _WAVE_INTEGRATION):
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
        "test.cmd=pytest -q\n", encoding="utf-8")
    _wave_ticket(root, _WAVE_TICKET, status="open", touch="src/")
    if base_branch is not None:
        # 판정 기준은 묶음 장부가 소유한다 — 실 board 는 발행이 이 파일을 만든다.
        write_cluster_ledger(
            root / ".project_manager" / "board", _WAVE_TICKET,
            base_branch=base_branch, branch=f"dev/{_WAVE_TICKET}")
    _wave_write(root / "src" / "app.py", 5)
    _wave_write(root / "docs" / "notes.md", 3)
    if git:
        _wave_git(root, "init", "-q", "-b", _WAVE_INTEGRATION)
        _wave_git(root, "config", "user.email", "t@e")
        _wave_git(root, "config", "user.name", "t")
        _wave_git(root, "add", "-A")
        _wave_git(root, "commit", "-qm", "seed")
    return root, _load_tool_copy(root, "board")


def _wave_claim(board, ticket_id: str = _WAVE_TICKET) -> int:
    # claim 은 `--task` 와 그 task 가 보유한 폴더가 필요하다(T-0962) — 솔로 형상이라 홈 자신이 그
    # task 의 폴더(장부 행 `slot="."`)다. 다른 축엔 무관심하다.
    ledger = pm_paths.leases_file(board._home())
    if not ledger.exists():
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"leases": [
            {"slot": ".", "repo": board.REPO.name, "session": "me",
             "state": "leased", "role": "work"}]}), encoding="utf-8")
    return board_ticket.cmd_claim(board._home(), argparse.Namespace(id=ticket_id, user="me", task="me"))


def test_completion_has_no_change_size_cap(tmp_path, capsys):
    """estimate small 티켓이 선언 경로 안에서 400줄을 바꿔도 진입 게이트가 막지 않는다."""
    root, board = _wave_repo(tmp_path)
    assert _wave_claim(board) == 0
    _wave_git(root, "add", "-A")
    _wave_git(root, "commit", "-qm", "claim 기록")
    _wave_write(root / "src" / "app.py", 5 + _WAVE_CHANGED_LINES)
    finish = _load_tool_copy(root, "ticket_finish")
    completed: list[list[str]] = []
    finisher = finish.TicketFinisher(
        run_pytest_fn=_green_pytest(100),
        run_board_fn=lambda args: (completed.append(args), (0, "board ok"))[1],
        run_git_fn=lambda args: (0, ""),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "wave",
        affected_domain_fn=lambda tid: [],
        dod_block_fn=lambda tid: None,
        residual_block_fn=lambda tid: None,
        self_axis_block_fn=lambda tid: None,
        board_py=root / ".project_manager" / "tools" / "board.py",
        regression_cwd=root,
        log_file=tmp_path / "log.md",
    )

    assert finisher.run(_WAVE_TICKET, section=None, dry_run=False) == 0
    captured = capsys.readouterr()
    assert "서킷브레이커" not in captured.out + captured.err
    assert completed == [["complete", _WAVE_TICKET, "--tests-pass"]]
    assert "diff_cap_block_fn" not in inspect.signature(finish.TicketFinisher).parameters
    for name in ("get_ticket_estimate", "DiffAttribution", "_default_diff_cap_block"):
        assert not hasattr(finish, name) and not hasattr(finish.TicketFinisher, name), name


# ── 완료 대상 작업 트리 자기 축 회귀 ─────────────────────────────────────────
#
# 판정 트리는 완료 대상 작업 트리 자체다 — 합성 병합 트리를 만들지 않는다. 대상 파일 집합은
# 실 git 명령(순수 읽기) 파생이고, baseline 은 claim 시점 커밋을 alternates 로 원 object DB 에
# 연결해 materialize 한다. 아래는 전부 실 git + 실 pytest 서브프로세스로 검증한다(fake
# runner·mock 트리 0).

def _axis_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8")


def _axis_repo(tmp_path: Path) -> Path:
    """자기 축 회귀용 최소 git 저장소 — `tests/test_axis_a.py` 하나를 가진 base 커밋 1개."""
    root = tmp_path / "axis"
    root.mkdir()
    _axis_git(root, "init", "-q")
    _axis_git(root, "config", "user.email", "axis@test")
    _axis_git(root, "config", "user.name", "axis")
    (root / "tests").mkdir()
    (root / "tests" / "test_axis_a.py").write_text(
        "def test_a():\n    assert 1 == 1  # base\n", encoding="utf-8")
    _axis_git(root, "add", "-A")
    _axis_git(root, "commit", "-q", "-m", "base")
    return root


def _axis_head(root: Path) -> str:
    return _axis_git(root, "rev-parse", "HEAD").stdout.strip()


def _axis_write_a(root: Path, body: str) -> None:
    (root / "tests" / "test_axis_a.py").write_text(body, encoding="utf-8")


def _axis_commit(root: Path, message: str) -> str:
    _axis_git(root, "add", "-A")
    _axis_git(root, "commit", "-q", "-m", message)
    return _axis_head(root)


def _axis_board_ticket(root: Path, ticket_id: str, claimed_rev: str) -> Path:
    """`root` 에 엔진 사본 board.py + `claimed_rev` 를 가진 최소 티켓 파일을 만든다.

    자기 축 판정의 claim 앵커 대조가 board.py 를 통해 이 티켓의 frontmatter 를 읽는다 —
    실 프로젝트 board 를 건드리지 않는 hermetic 사본(`_wave_repo` 와 같은 패턴)."""
    tools_dir = root / ".project_manager" / "tools"
    if not tools_dir.exists():
        shutil.copytree(TOOLS, tools_dir)
    for status in ("open", "claimed", "blocked", "done"):
        (root / ".project_manager" / "board" / "tickets" / status).mkdir(
            parents=True, exist_ok=True)
    path = (root / ".project_manager" / "board" / "tickets" / "claimed"
            / f"{ticket_id}-axis.md")
    path.write_text(
        f"---\nid: {ticket_id}\ntitle: axis\nstatus: claimed\n"
        "claimed_by: me\nclaimed_at: '2026-08-23T00:00:00+00:00'\n"
        f"claimed_rev: {claimed_rev}\ncompleted_at: null\n"
        "depends_on: []\nblocks: []\ntouches:\n- tests/\nestimate: small\n"
        "tags: []\n---\n\n# ticket\n\n## 목표\nx\n",
        encoding="utf-8")
    return tools_dir / "board.py"


def _axis_finisher(tf, root: Path, board_py: Path):
    return tf.TicketFinisher(task_workspace=root, board_py=board_py)


# ── 순수 파싱 helper (git/pytest 출력 파싱만 — 서브프로세스 0) ────────────────

def test_self_axis_target_paths_keeps_only_flat_tests_py(tf):
    listing = "\n".join([
        "tests/test_a.py",
        "tests/fixtures/data.py",
        "src/app.py",
        "tests/test_b.py",
        "README.md",
    ])
    assert tf._self_axis_target_paths(listing) == {"tests/test_a.py", "tests/test_b.py"}


def test_self_axis_failed_node_ids_parses_function_level_ids(tf):
    """실패 파싱은 파일이 아니라 **함수 단위 노드 ID** 전체를 남긴다."""
    output = (
        "FAILED tests/test_a.py::test_x - AssertionError\n"
        "ERROR tests/test_b.py::test_y - ImportError\n"
        "ERROR tests/test_c.py - collection error\n"
        "2 failed, 1 error in 0.1s\n"
    )
    assert tf._self_axis_failed_node_ids(output) == {
        "tests/test_a.py::test_x", "tests/test_b.py::test_y", "tests/test_c.py",
    }


def test_self_axis_failed_node_ids_normalizes_the_path_to_posix(tf):
    """자식이 `os.sep` 표기로 낸 노드 ID 도 경로 부분은 POSIX 표기로 돌려준다(Windows 자식이
    상위 `pytest.ini` 를 rootdir 로 잡으면 역슬래시로 낸다) — `::` 뒤 파라미터 ID 는 경로가
    아니므로 손대지 않는다."""
    output = (
        "FAILED tests\\test_a.py::test_x - AssertionError\n"
        "ERROR tests\\test_c.py - collection error\n"
        "FAILED tests/test_d.py::test_p[a\\b] - AssertionError\n"
        "2 failed, 1 error in 0.1s\n"
    )
    assert tf._self_axis_failed_node_ids(output) == {
        "tests/test_a.py::test_x", "tests/test_c.py", "tests/test_d.py::test_p[a\\b]",
    }


# ── `_self_axis_target_files` — 대상 집합 산출 ────────────────────────────────

def test_self_axis_target_files_unions_tracked_untracked_and_ratchet_list(tf, tmp_path):
    root = _axis_repo(tmp_path)
    (root / "tests" / "test_axis_a.py").write_text(
        "def test_a():\n    assert 2 == 2  # unstaged change\n", encoding="utf-8")
    (root / "tests" / "test_axis_untracked.py").write_text(
        "def test_new():\n    assert True\n", encoding="utf-8")

    finisher = tf.TicketFinisher()
    target = finisher._self_axis_target_files(root)

    assert "tests/test_axis_a.py" in target       # tracked(unstaged) 변경
    assert "tests/test_axis_untracked.py" in target  # untracked 신규
    assert set(tf._SELF_AXIS_RATCHET_GUARD_FILES) <= target  # 전-트리 가드 항상 포함


# ── `_default_self_axis_block` 시나리오(T1~T3·T9) ────────────────────────────

def test_default_self_axis_block_does_not_block_on_inherited_red_only(
        tf, tmp_path, monkeypatch, capsys):
    """T1 — base 에서 이미 red 인 실패뿐이면 rc 0 · loud 경고 1줄 · 차단 없음."""
    root = _axis_repo(tmp_path)
    _axis_write_a(root, "def test_a():\n    assert False  # already red at claim time\n")
    base = _axis_commit(root, "base is red")
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev=base)
    monkeypatch.setattr(tf, "_SELF_AXIS_RATCHET_GUARD_FILES", ("tests/test_axis_a.py",))

    finisher = _axis_finisher(tf, root, board_py)
    block = finisher._default_self_axis_block("T-AXIS")

    assert block is None
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "경고" in err and "tests/test_axis_a.py::test_a" in err


def test_default_self_axis_block_blocks_on_new_failure_in_a_file_with_inherited_red(
        tf, tmp_path, monkeypatch):
    """T2 — base·dev 양쪽에 실패가 있는 같은 파일에서도 신규분이 함수 단위로 살아남는다
    (파일 단위 비교였다면 이 신규 실패가 사라졌을 형상)."""
    root = _axis_repo(tmp_path)
    (root / "tests" / "test_axis_a.py").write_text(
        "def test_a():\n    assert False  # already red at claim time\n"
        "def test_b():\n    assert True\n",
        encoding="utf-8")
    base = _axis_commit(root, "base has test_a red")
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev=base)
    monkeypatch.setattr(tf, "_SELF_AXIS_RATCHET_GUARD_FILES", ("tests/test_axis_a.py",))

    (root / "tests" / "test_axis_a.py").write_text(
        "def test_a():\n    assert False  # still red — inherited\n"
        "def test_b():\n    assert False  # dev broke it — new\n",
        encoding="utf-8")
    _axis_commit(root, "dev breaks test_b")

    finisher = _axis_finisher(tf, root, board_py)
    block = finisher._default_self_axis_block("T-AXIS")

    assert block is not None
    assert "✗ tests/test_axis_a.py::test_b" in block  # 신규분만 ✗ 로 나열된다
    assert "✗ tests/test_axis_a.py::test_a" not in block  # 상속 red — 신규분에서 빠져야 한다


def test_default_self_axis_block_costs_nothing_when_the_work_tree_is_green(
        tf, tmp_path, monkeypatch, capsys):
    """T3(역방향 확인 a) — 작업 트리가 green 이면 baseline 은 돌리지 않는다(materialize 0회 ·
    출력 0줄 · 차단 없음)."""
    root = _axis_repo(tmp_path)
    base = _axis_head(root)
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev=base)
    monkeypatch.setattr(tf, "_SELF_AXIS_RATCHET_GUARD_FILES", ("tests/test_axis_a.py",))

    finisher = _axis_finisher(tf, root, board_py)
    monkeypatch.setattr(
        finisher, "_materialize_tree",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("materialize 미호출이어야 한다")))

    block = finisher._default_self_axis_block("T-AXIS")

    assert block is None
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_default_self_axis_block_is_silent_when_claimed_rev_is_absent(
        tf, tmp_path, monkeypatch, capsys):
    """T9 — 채택자 형상(claimed_rev 미기록). 출력 0줄 · rc 0 상당 · pytest 실행 0회."""
    root = _axis_repo(tmp_path)
    _axis_write_a(root, "def test_a():\n    assert False\n")  # red 라도 비교 기준이 없다
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev="")
    finisher = _axis_finisher(tf, root, board_py)
    monkeypatch.setattr(
        finisher, "_run_pytest_subset",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("pytest 미호출이어야 한다")))

    block = finisher._default_self_axis_block("T-AXIS")

    assert block is None
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


# ── 노드 ID 표기 정규화 — 비교 축·진단문 축 ────────────────────────────────
#
# 자식 pytest 는 rootdir 해소에 따라 같은 실패를 `os.sep` 표기로 낸다(Windows 실측 — 상위
# `pytest.ini` 를 rootdir 로 잡는 트리에서 `tests\test_axis_a.py::test_a`). 그 표기를 Linux
# 에서도 값으로 재려면 자식 출력 자체를 주입한다. 정규화가 비교 **전에** 걸리지 않으면 표기만
# 다른 같은 실패가 신규 실패로 둔갑하므로 비교 축과 진단문 축을 따로 본다.

_AXIS_BACKSLASH_NODE_A = "tests\\test_axis_a.py::test_a"
_AXIS_BACKSLASH_NODE_B = "tests\\test_axis_a.py::test_b"


def _axis_child_output_runner(code_tree: Path, work_out: str, base_out: str):
    """`_run_pytest_subset` 대역 — 작업 트리에는 `work_out`, baseline 트리에는 `base_out`."""
    def _run(cwd: Path, files) -> tuple[int, str]:
        return 1, (work_out if Path(cwd) == code_tree else base_out)
    return _run


def test_default_self_axis_block_does_not_invent_a_new_failure_from_separator_notation(
        tf, tmp_path, monkeypatch, capsys):
    """비교 축 — 작업 트리 자식이 역슬래시 표기로, baseline 자식이 POSIX 표기로 같은 실패를
    내면 상속 red 다. 정규화가 비교 전에 걸리지 않으면 없는 신규 실패로 차단한다."""
    root = _axis_repo(tmp_path)
    base = _axis_head(root)
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev=base)

    finisher = _axis_finisher(tf, root, board_py)
    monkeypatch.setattr(
        finisher, "_run_pytest_subset",
        _axis_child_output_runner(
            finisher._code_tree(),
            f"FAILED {_AXIS_BACKSLASH_NODE_A} - AssertionError\n1 failed in 0.1s\n",
            "FAILED tests/test_axis_a.py::test_a - AssertionError\n1 failed in 0.1s\n"))

    block = finisher._default_self_axis_block("T-AXIS")

    assert block is None
    err = capsys.readouterr().err
    assert "경고" in err and "tests/test_axis_a.py::test_a" in err
    assert _AXIS_BACKSLASH_NODE_A not in err


def test_default_self_axis_block_lists_new_failures_in_posix_notation(
        tf, tmp_path, monkeypatch):
    """진단문 축 — 자식이 역슬래시 표기로 낸 신규 실패도 POSIX 표기로 나열하고, 표기만 다른
    상속 red 는 신규분에서 빠진다."""
    root = _axis_repo(tmp_path)
    base = _axis_head(root)
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev=base)

    finisher = _axis_finisher(tf, root, board_py)
    monkeypatch.setattr(
        finisher, "_run_pytest_subset",
        _axis_child_output_runner(
            finisher._code_tree(),
            f"FAILED {_AXIS_BACKSLASH_NODE_A} - AssertionError\n"
            f"FAILED {_AXIS_BACKSLASH_NODE_B} - AssertionError\n2 failed in 0.1s\n",
            "FAILED tests/test_axis_a.py::test_a - AssertionError\n1 failed in 0.1s\n"))

    block = finisher._default_self_axis_block("T-AXIS")

    assert block is not None
    assert "✗ tests/test_axis_a.py::test_b" in block          # 신규분 — POSIX 표기
    assert f"✗ {_AXIS_BACKSLASH_NODE_B}" not in block         # 자식 표기 그대로 새지 않는다
    assert "✗ tests/test_axis_a.py::test_a" not in block      # 표기만 다른 상속 red 는 제외
    assert f"✗ {_AXIS_BACKSLASH_NODE_A}" not in block


# ── T4 — untracked 민감도 ─────────────────────────────────────────────────

def test_default_self_axis_block_catches_an_untracked_new_red_test_file(tf, tmp_path):
    root = _axis_repo(tmp_path)
    base = _axis_head(root)
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev=base)
    (root / "tests" / "test_axis_untracked.py").write_text(
        "def test_new():\n    assert False  # untracked new red\n", encoding="utf-8")
    # 커밋하지 않는다 — untracked 로 남긴다.

    finisher = _axis_finisher(tf, root, board_py)
    block = finisher._default_self_axis_block("T-AXIS")

    assert block is not None
    assert "tests/test_axis_untracked.py::test_new" in block


def test_untracked_scan_omission_would_have_missed_the_new_red(tf, tmp_path):
    """민감도(반대 방향) — tracked diff 만 쓰면(`ls-files -o` 산출을 빼면) 같은 untracked red
    가 대상 집합에서 사라진다."""
    root = _axis_repo(tmp_path)
    (root / "tests" / "test_axis_untracked.py").write_text(
        "def test_new():\n    assert False\n", encoding="utf-8")

    finisher = tf.TicketFinisher()
    _rc, tracked_out = finisher._run_git_stdout_at_fn(root, ["diff", "--name-only", "HEAD"])
    tracked_only = tf._self_axis_target_paths(tracked_out)
    assert "tests/test_axis_untracked.py" not in tracked_only

    full = finisher._self_axis_target_files(root)
    assert "tests/test_axis_untracked.py" in full  # ls-files -o 를 더하면 잡힌다


# ── T5 — staged 민감도 ───────────────────────────────────────────────────

def test_default_self_axis_block_catches_a_staged_uncommitted_red(tf, tmp_path):
    root = _axis_repo(tmp_path)
    base = _axis_head(root)
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev=base)
    _axis_write_a(root, "def test_a():\n    assert False  # staged, not committed\n")
    _axis_git(root, "add", "-A")

    finisher = _axis_finisher(tf, root, board_py)
    block = finisher._default_self_axis_block("T-AXIS")

    assert block is not None
    assert "tests/test_axis_a.py::test_a" in block


def test_head_tree_alone_would_have_missed_the_staged_red(tf, tmp_path):
    """민감도(반대 방향) — HEAD 트리 자체(커밋된 내용)만 보면 이 staged 변경이 안 보인다.
    작업 트리 diff(`git diff --name-only HEAD`)라야 값으로 잡는다."""
    root = _axis_repo(tmp_path)
    _axis_write_a(root, "def test_a():\n    assert False  # staged, not committed\n")
    _axis_git(root, "add", "-A")

    finisher = tf.TicketFinisher()
    rc, out = finisher._run_git_stdout_at_fn(root, ["show", "HEAD:tests/test_axis_a.py"])
    assert rc == 0 and "assert 1 == 1" in out  # HEAD 트리 자체는 여전히 base 내용

    _rc2, diff_out = finisher._run_git_stdout_at_fn(root, ["diff", "--name-only", "HEAD"])
    assert "tests/test_axis_a.py" in tf._self_axis_target_paths(diff_out)


# ── T-0851 — 정리 실패 표면화(호출 형태 검사가 아니라 실제 삭제 실패 주입) ─────
#
# `test_engine_cleanup_never_swallows_delete_failures`(test_file_lock.py)는 `ignore_errors=True`
# 호출 형태가 0건임을 AST로만 본다 — "삭제가 실제로 실패했을 때 그 실패가 눈에 보이는가"는
# 별개 값이다. 아래 둘은 `file_lock.force_rmtree`를 실패하도록 몽키패치해(실제 삭제 실패 주입)
# 두 정리 지점(`_materialize_tree`의 except 경로·`_default_self_axis_block`의 finally 경로)이
# 각각의 형제 불변식을 지키는지 값으로 본다.


def _failing_force_rmtree(recorded: list, error: OSError):
    """삭제를 실패시키되 **어느 경로였는지 기록**하는 주입 대역.

    임시 루트가 저장소 안(`.project_manager/.local/tmp`)이라 실패를 일부러 만든 케이스가
    남긴 트리는 그대로 쌓인다. 그 케이스가 뒷정리를 소유하되, 대상은 **자기가 요청받은
    경로**여야 한다 — 디렉터리 차집합으로 지우면 같은 루트를 쓰는 병렬 케이스의 트리를
    실행 중에 지운다(xdist 실측).
    """
    def _fail(path, **_kwargs):
        recorded.append(Path(path))
        raise error
    return _fail


def test_materialize_tree_exception_cleanup_failure_does_not_mask_the_original_error(
        tf, tmp_path, monkeypatch, capsys):
    """except 경로 — 정리(force_rmtree) 실패가 원래 예외(git archive 실패)를 덮지 않고, 정리
    실패 자체는 loud 경고로 표면화된다(삼키지 않는다)."""
    root = _axis_repo(tmp_path)
    finisher = tf.TicketFinisher()

    file_lock = tf.fs
    real_force_rmtree = file_lock.force_rmtree
    attempted: list[Path] = []
    injected = OSError("디렉터리 정리 실패 — 시뮬레이션(권한 거부)")
    monkeypatch.setattr(
        file_lock, "force_rmtree", _failing_force_rmtree(attempted, injected))

    with pytest.raises(RuntimeError, match="git archive"):
        finisher._materialize_tree(root, "no-such-ref-at-all")

    captured = capsys.readouterr()
    assert "baseline scratch 정리 실패" in captured.err
    assert str(injected) in captured.err
    for path in attempted:
        real_force_rmtree(path)


def test_default_self_axis_block_returns_correctly_despite_a_baseline_cleanup_failure(
        tf, tmp_path, monkeypatch, capsys):
    """finally 경로 — baseline 정리 실패가 정상 판정 결과(반환값)를 깨지 않고, 정리 실패
    자체는 loud 경고로 표면화된다(삼키지 않는다)."""
    root = _axis_repo(tmp_path)
    base = _axis_head(root)
    board_py = _axis_board_ticket(root, "T-AXIS", claimed_rev=base)
    # staged, not committed — `_self_axis_target_files`의 `git diff --name-only HEAD`가 직접
    # 잡는다(ratchet 목록 몽키패치 불필요 · T5 관용구와 동형).
    _axis_write_a(root, "def test_a():\n    assert False  # new red at finish time\n")
    _axis_git(root, "add", "-A")

    finisher = _axis_finisher(tf, root, board_py)

    file_lock = tf.fs
    real_force_rmtree = file_lock.force_rmtree
    attempted: list[Path] = []
    injected = OSError("디렉터리 정리 실패 — 시뮬레이션(잠긴 핸들)")
    monkeypatch.setattr(
        file_lock, "force_rmtree", _failing_force_rmtree(attempted, injected))

    block = finisher._default_self_axis_block("T-AXIS")

    assert block is not None
    assert "✗ tests/test_axis_a.py::test_a" in block  # 정리 실패에도 정상 판정 결과가 나온다

    captured = capsys.readouterr()
    assert "baseline 트리 정리 실패" in captured.err
    assert str(injected) in captured.err
    for path in attempted:
        real_force_rmtree(path)


# ── T6 — alternates 민감도(baseline materialize 의 원 이력 해소) ─────────────
#
# F-002 fix — 존재-판정(`git cat-file -e` 직접 확인)이 아니라 값-대조다: alternates 있는/없는
# 두 baseline 에서 같은 canary 테스트 파일을 **실 pytest** 로 돌려 실패 노드 ID 집합의 차이를
# 값으로 비교한다(자기 축 판정 본체의 `work_failed - base_failed` 와 같은 형태). canary 의
# 어서션 자체는 `git cat-file -e` 를 쓰지만, 이 테스트가 대조하는 대상은 그 결과를 감싼
# **pytest 실패 집합**이지 outer 테스트가 git 명령을 직접 확인하는 게 아니다. 옛
# `test_accident_one_reproduces_on_the_real_merge_preview_tree` 는 병합 미리보기(merge-tree
# oid materialize) 를 전제로 한 3단계 테스트였고 그 개념 자체가 재설계로 없어졌다 — 지금은
# canary 파일을 materialize 된 scratch 에 직접 써 넣어(커밋 불필요) 같은 "고립되면 원 이력
# SHA 를 못 본다" 성질만 남긴다. 같은 테스트에서 직렬 실측 소요도 박제한다(F-001 rejected —
# `-n` 은 붙이지 않는다: 권고 집합 직렬도 수 초 수준이라 불필요하고 xdist 미설치 채택자에서
# `-n` 은 인자 오류로 loud 하게 죽는다).

_ALTERNATES_CANARY_FILE = "tests/test_alternates_canary.py"
_ALTERNATES_CANARY_NODE = f"{_ALTERNATES_CANARY_FILE}::test_resolves_a_real_historical_object"


def _write_alternates_canary(scratch: Path, target_sha: str) -> None:
    """`scratch` 에 원 이력 SHA 를 `git cat-file -e` 로 참조하는 canary 테스트를 써 넣는다.

    materialize 는 committed 이력만 archive 하므로(작업 트리 미커밋 변경은 못 담는다) canary
    는 archive 가 아니라 파일시스템에 직접 쓴다 — 커밋이 전혀 필요 없다."""
    (scratch / "tests").mkdir(parents=True, exist_ok=True)
    (scratch / _ALTERNATES_CANARY_FILE).write_text(
        "import subprocess\n\n\n"
        "def test_resolves_a_real_historical_object():\n"
        f"    result = subprocess.run(['git', 'cat-file', '-e', {target_sha!r}],\n"
        "                            capture_output=True)\n"
        "    assert result.returncode == 0\n",
        encoding="utf-8",
    )


def test_alternates_presence_flips_a_real_pytest_failure_set_by_exactly_one_node(
        tf, tmp_path):
    """alternates 있는/없는 baseline 양쪽에서 같은 canary 를 실 pytest 로 돌려 실패 노드 ID
    집합 차이를 값으로 대조한다 — 차집합은 정확히 canary 노드 1건이어야 한다. 같은 실행에서
    직렬 소요를 박제한다."""
    finisher = tf.TicketFinisher(venv_python=sys.executable)
    rc_old, old_out = finisher._run_git_stdout_at_fn(tf.REPO, ["rev-parse", "HEAD~3"])
    assert rc_old == 0
    target_sha = old_out.strip()

    t0 = time.time()
    on_tree = finisher._materialize_tree(tf.REPO, "HEAD")
    try:
        assert (on_tree / ".git" / "objects" / "info" / "alternates").is_file()
        _write_alternates_canary(on_tree, target_sha)
        on_rc, on_out = finisher._run_pytest_subset(on_tree, [_ALTERNATES_CANARY_FILE])
    finally:
        tf.fs.force_rmtree(on_tree)
    t1 = time.time()

    off_tree = finisher._materialize_tree(tf.REPO, "HEAD")
    try:
        (off_tree / ".git" / "objects" / "info" / "alternates").unlink()
        _write_alternates_canary(off_tree, target_sha)
        off_rc, off_out = finisher._run_pytest_subset(off_tree, [_ALTERNATES_CANARY_FILE])
    finally:
        tf.fs.force_rmtree(off_tree)
    t2 = time.time()

    on_failed = tf._self_axis_failed_node_ids(on_out)
    off_failed = tf._self_axis_failed_node_ids(off_out)

    assert on_rc == 0 and on_failed == set(), on_out
    assert off_rc != 0 and off_failed == {_ALTERNATES_CANARY_NODE}, off_out
    assert off_failed - on_failed == {_ALTERNATES_CANARY_NODE}  # 차집합 = 정확히 1건

    on_wall, off_wall = t1 - t0, t2 - t1
    print(
        f"  [실측·직렬] alternates on={on_wall:.2f}s off={off_wall:.2f}s "
        f"합계={on_wall + off_wall:.2f}s (-n 미부착 — F-001 rejected)"
    )


# ── T7 — 원 저장소 무오염(지문 4종) ────────────────────────────────────────

def _repo_fingerprint(finisher, root: Path) -> tuple[int, str, str, int]:
    """refs 수 · refs 해시 · status 해시 · loose object 수."""
    _rc1, refs_out = finisher._run_git_stdout_at_fn(root, ["show-ref"])
    refs_lines = sorted(line for line in refs_out.splitlines() if line.strip())
    refs_hash = hashlib.sha256("\n".join(refs_lines).encode()).hexdigest()[:16]
    _rc2, status_out = finisher._run_git_stdout_at_fn(root, ["status", "--porcelain"])
    status_hash = hashlib.sha256(status_out.encode()).hexdigest()[:16]
    _rc3, common_out = finisher._run_git_stdout_at_fn(root, ["rev-parse", "--git-common-dir"])
    common_dir = Path(common_out.strip())
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()
    loose_count = sum(
        1 for shard in (common_dir / "objects").glob("*")
        if shard.is_dir() and shard.name not in ("info", "pack")
        for _obj in shard.glob("*")
    )
    return len(refs_lines), refs_hash, status_hash, loose_count


def test_gate_execution_does_not_mutate_the_original_repository(tf):
    """T7 — 게이트 실행 전후 원 저장소 지문 4종(refs 수·refs 해시·status 해시·loose object
    수) 동일. materialize·alternates 가 원 저장소에 아무 것도 쓰지 않음을 값으로 확인한다."""
    finisher = tf.TicketFinisher(venv_python=sys.executable)
    before = _repo_fingerprint(finisher, tf.REPO)

    scratch = finisher._materialize_tree(tf.REPO, "HEAD")
    # `ignore_errors=True` 는 read-only git object 를 남긴다 — baseline scratch 는 이제
    # 저장소 안 임시 루트에 서므로 남으면 그대로 쌓인다. 엔진과 같은 삭제 seam 을 쓴다.
    tf.fs.force_rmtree(scratch)

    after = _repo_fingerprint(finisher, tf.REPO)
    assert before == after


# ── T10 — 전-트리 ratchet 가드 목록 정합 + 신규 가드 커버리지 ─────────────────

def test_self_axis_ratchet_guard_files_are_real_existing_files(tf):
    for rel in tf._SELF_AXIS_RATCHET_GUARD_FILES:
        assert (REPO / rel).is_file(), f"{rel} 가 실재하지 않는다"
    assert len(tf._SELF_AXIS_RATCHET_GUARD_FILES) == len(set(tf._SELF_AXIS_RATCHET_GUARD_FILES))


def test_self_axis_ratchet_guard_list_is_a_subset_of_the_full_test_suite(tf):
    """목록의 각 항목이 `tests/` 안의 실 파일이라는 T10 최소 보증 — 이름만으로 "새 전-트리
    가드" 를 일반적으로 자동판별하는 수단은 이 라운드에서 찾지 못했다(메모 참조). `_guard.py`/
    `_lint.py`/`_e2e.py` 접미사로 시도했더니 이 목록과 무관한 기존 파일 22개가 걸려
    (`test_git_anchor_guard.py` 류의 좁은 범위 가드) 오탐이었다 — 그 시도는 기각하고 최소
    보증만 남긴다."""
    all_test_files = {f"tests/{p.name}" for p in (REPO / "tests").glob("test_*.py")}
    assert set(tf._SELF_AXIS_RATCHET_GUARD_FILES) <= all_test_files


# ── I7 소스 단언 — 통과 시 문구 0줄(pass-phrase 재발 방지) ────────────────────

def test_self_axis_source_never_spells_out_a_pass_phrase(tf):
    source = Path(tf.__file__).read_text(encoding="utf-8")
    start = source.index("_default_self_axis_block")
    end = source.index("def _default_run_board")
    section = source[start:end]
    for banned in ("자기 축 통과", "green 이다", "판정 green", "seam green"):
        assert banned not in section


def test_source_never_calls_merge_tree_for_self_axis_judgment(tf):
    """I1 — 판정 트리는 완료 대상 작업 트리 자체다. 소스에 `merge-tree` 호출이 없다(합성
    병합 트리를 만들지 않는다는 불변식의 소스 단언)."""
    source = Path(tf.__file__).read_text(encoding="utf-8")
    assert "merge-tree" not in source


# ── preflight 튜플 배선 (순서·`--no-pytest`·DI 기본값) ────────────────────────

def test_self_axis_preflight_runs_last_after_private_ref_and_dod_pass(tf, tmp_path):
    # 앞선 preflight(잔여 판정)는 git 을 읽는다 — 막혀야 하는 것은 git 쓰기(add·commit)다.
    git_calls: list[list[str]] = []
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("회귀 미호출이어야 한다")),
        run_board_fn=lambda args: (_ for _ in ()).throw(AssertionError("board 미호출")),
        run_git_fn=lambda args: (git_calls.append(list(args)) or (0, "")),
        dod_block_fn=lambda tid: None,
        self_axis_block_fn=lambda tid: f"자기 축 회귀 신규 실패 — {tid}",
        log_file=tmp_path / "log.md",
    )
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert not [args for args in git_calls if args and args[0] in ("add", "commit")], git_calls


def test_self_axis_preflight_is_never_called_when_dod_blocks(tf, tmp_path):
    calls: list[str] = []
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("회귀 미호출이어야 한다")),
        dod_block_fn=lambda tid: "DoD 미마감",
        self_axis_block_fn=lambda tid: calls.append(tid) or None,
        log_file=tmp_path / "log.md",
    )
    assert finisher.run("T-1234", section=None, dry_run=False) == 1
    assert calls == []


def test_self_axis_preflight_still_fires_with_no_pytest(tf, tmp_path):
    """T8 — `--no-pytest` 로 [1/5] 회귀를 건너뛰어도 자기 축 preflight 는 그대로 발화한다."""
    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("skip_pytest 라 미호출")),
        dod_block_fn=lambda tid: None,
        self_axis_block_fn=lambda tid: f"자기 축 회귀 신규 실패 — {tid}",
        log_file=tmp_path / "log.md",
    )
    assert finisher.run(
        "T-1234", section=None, dry_run=False, skip_pytest=True) == 1


def test_self_axis_block_fn_defaults_to_the_real_implementation(tf):
    """DI 인자 1개 — 미주입이면 `_default_self_axis_block` 이 기본값이다."""
    finisher = tf.TicketFinisher()
    assert finisher._self_axis_block_fn == finisher._default_self_axis_block


def test_t0930_rebound_finish(tmp_path, monkeypatch, capsys):
    from test_cluster_close import _build_close_env, _write_cluster_ledger, _CLUSTER, _CLUSTER_BRANCH, _INTEGRATION_BRANCH, _git, _rev
    env = _build_close_env(_load_module(), tmp_path / "rebound", monkeypatch)
    closer = env.closer()
    board = closer._board_parser_module
    _write_cluster_ledger(env.board_dir, tickets=(env.ticket,), branch=None)
    board_py = env.home / ".project_manager/tools/board.py"
    assert env.tf._cluster_integration_branch(board_py, env.ticket) is None
    assert closer.run() == 1
    _git(env.slot, "checkout", "--", "src/app.py")
    _git(env.slot, "checkout", "--detach")
    _git(env.code, "checkout", "-b", "user-held")
    _git(env.slot, "checkout", _INTEGRATION_BRANCH)
    _git(env.code, "branch", "-D", _CLUSTER_BRANCH)
    assert board_cluster.cmd_cluster(board._home(), argparse.Namespace(cluster_cmd="new", name="rebound",
        tickets=env.ticket, spike=None, repo="code", slot=1, task="t")) == 0
    ledger = cluster_store.cluster_ledger_path(board._home(), "rebound")
    declaration = cluster_store.load_cluster(board._home(), "rebound")
    assert declaration["base_branch"] == _INTEGRATION_BRANCH
    assert cluster_store.cluster_tickets(cluster_store.load_cluster(board._home(), _CLUSTER)) == []
    # 재결속 장부를 보존하면서 실제 draft 승격과 claim을 이어 실행한다.
    path = ticket_store.find_ticket_exact(board._home(), env.ticket)[1]
    fm, _ = ticket_store.load_ticket(path)
    from test_board_cluster import _TEMPLATE_TEXT as filled
    body = filled.split("---", 2)[2].replace("T-NNNN", env.ticket).replace("<제목>", "종결 픽스처").replace("- [ ]", "- [x]")
    fm["status"] = "draft"
    draft = ticket_store.move_ticket(board._home(), path, ".drafts")
    ticket_store.dump_ticket(draft, fm, body)
    assert board_sync._board_git_sync_best_effort(board._home(), "fixture draft conversion", (path,))
    before = ledger.read_bytes()
    assert board_ticket.cmd_promote(board._home(), argparse.Namespace(id=env.ticket)) == 0
    assert ledger.read_bytes() == before
    assert board_ticket.cmd_claim(board._home(), argparse.Namespace(id=env.ticket, repo="code", slot=1, task="t")) == 0
    fork = _git(env.slot, "config", f"branch.{declaration['branch']}.pm-fork").stdout.strip()
    assert fork == _rev(env.slot)
    assert _git(env.slot, "config", f"branch.{declaration['branch']}.pm-parent").stdout.strip() == _INTEGRATION_BRANCH
    _git(env.slot, "checkout", cluster_rules.cluster_branch_name("rebound"))
    _git(env.code, "checkout", _INTEGRATION_BRANCH)
    (env.slot / "src/app.py").write_text("seed = 1\nwork = 2\n", encoding="utf-8")
    leases, home = env.lease_bytes(), env.home_log()
    assert env.tf._cluster_integration_branch(board_py, env.ticket) == _INTEGRATION_BRANCH
    assert env.closer(cluster="rebound").run() == 0
    after = cluster_store.load_cluster(board._home(), "rebound")
    assert after["branch"] == declaration["branch"] and after["base_branch"] == declaration["base_branch"]
    assert env.lease_bytes() == leases
    assert env.home_log()[1:] == home
    assert list((env.board_dir / "tickets/done").glob("T-*.md"))
    assert _git(env.code, "merge-base", "--is-ancestor", _rev(env.slot), _INTEGRATION_BRANCH).returncode == 0


# ── T-0962 — `--repo`/`--slot` 은 복구 문 전용 · log 머리말은 task 태그만 ──────────

def test_t0962_finish_takes_repo_slot_only_for_reconcile(tf, capsys):
    """AT-005 — 정상 종결(티켓·묶음)의 `--repo --slot` 은 rc 2, 세션 인자는 어디에도 없다."""
    for argv in (["T-0001", "--repo", "proj", "--slot", "1", "--dry-run", "--no-pytest"],
                 ["--cluster", "C-x", "--repo", "proj", "--slot", "1", "--dry-run"]):
        with pytest.raises(SystemExit) as exc:
            tf.main(argv)
        assert exc.value.code == 2
        assert "--reconcile-integrated" in capsys.readouterr().err
    skeleton = tf.build_log_skeleton("T-0001", "제목", 1, 1, 1, date="2026-10-18", task="alpha")
    assert "(task:alpha)" in skeleton.splitlines()[0]
    with pytest.raises(TypeError):
        tf.build_log_skeleton("T-0001", "제목", 1, 1, 1, session="proj_1")
    assert "session" not in inspect.signature(tf.TicketFinisher.run).parameters
    assert "session" not in inspect.signature(tf.ClusterCloser).parameters


def test_t0962_finish_without_task_stops_before_writes(tf, tmp_path, monkeypatch, capsys):
    """[[T-0962]] 리뷰 F-008 — `--task` 없는 정상 종결은 rc 2 로 멈추고 board·log 쓰기가 0 이다.

    종결 log 머리말 `(task:이름)`·board complete 소유 대조는 귀속 쓰기라 `--task` 가 필요하다.
    cwd 작업 폴더·단일 활성 task 에서 정체성을 유도하지 않는다 — 대여 기록에 활성 task 가 정확히
    1개여도(옛 자동 해소가 고르던 형상) 멈춘다. 복구 문(`--reconcile-integrated`)은 이 규칙 밖이다.
    """
    log_file = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("# log\n", encoding="utf-8")
    ledger = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({
        "leases": [{"slot": "work/proj_1", "repo": "proj", "session": "solo-task",
                    "state": "leased", "role": "work"}],
        "tasks": [{"name": "solo-task"}]}), encoding="utf-8")
    monkeypatch.setattr(tf, "REPO", tmp_path)
    monkeypatch.setattr(tf, "LOG_FILE", log_file)
    calls: list = []
    monkeypatch.setattr(tf.subprocess, "run", lambda *a, **k: calls.append(a) or pytest.fail(
        "--task 없는 종결이 하위 프로세스(board·git)를 불렀다"))

    def _no_pipeline(**_kwargs):
        pytest.fail("--task 없는 종결이 종결 파이프라인을 만들었다")

    monkeypatch.setattr(tf, "TicketFinisher", _no_pipeline)
    monkeypatch.setattr(tf, "ClusterCloser", _no_pipeline)
    for argv in (["--cluster", "C-grp"], ["--cluster", "C-grp", "--no-pytest"], ["T-0001"]):
        with pytest.raises(SystemExit) as exc:
            tf.main(argv)
        assert exc.value.code == 2, argv
        assert "--task <task>" in capsys.readouterr().err
    assert calls == []
    assert log_file.read_text(encoding="utf-8") == "# log\n"
