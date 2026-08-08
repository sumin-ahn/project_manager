"""회귀 FULL 게이트 수집 하한 가드 (T-0581) — 부분수집 false-green 차단.

엔진 회귀 게이트는 rc5(수집 0)만 결함 신호로 봤고, **부분 수집**(rc0 인데 cwd/pythonpath 파손으로
스위트 일부만 돎)은 pass 로 기록했다. 채택자가 로컬 패치로 유지하던 하한 가드를 엔진이 흡수한다 —
local.conf `regression_min_collected`(기본 0 = off) 미만이면 FULL 게이트를 `fail` + 전용 라벨
`partial-collection` 으로 강등하고, check(pre-push)가 그 사유를 실어 push 를 막는다.

**hermetic 필수**: board.py 의 경로 전역(`REPO`·`LOCAL_CONF`·`LOCAL_DIR`·`REGRESSION_FLAG`·
`LEASES_FILE`)은 import 시점에 실 repo 절대경로로 굳는다 — tmp 프로젝트로 monkeypatch 재지정해
실 루트의 local.conf·회귀 플래그를 읽거나 쓰지 않는다(test_board_multipm.py 동형 패턴).
pytest 자식은 절대 실기동하지 않는다 — 회귀 러너 seam(`subprocess.Popen` — 출력을 tee 하며
읽는다)을 rc/스트림 주입 대역으로 갈아끼운다.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest
from _pytest_summary import pytest_summary

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_board():
    """board.py 를 (패키지 아님) importlib 로 경로 로드 — test_board_multipm 과 동일."""
    spec = importlib.util.spec_from_file_location("board_regression", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    """회귀 경로를 hermetic 하게 만든 board — 플래그/conf/장부/HEAD 를 tmp·fake 로 격리.

    LEASES_FILE 은 기본 부재(→ leased 0 = 솔로 · `_active_slot_path` None → cwd=REPO 폴백)이고,
    M>1 테스트만 장부를 직접 심는다. env 세션은 제거해 실 PM 세션이 새지 않게 한다.
    """
    proj = tmp_path / "proj"
    pm = proj / ".project_manager"
    local = pm / ".local"
    local.mkdir(parents=True, exist_ok=True)
    mod = _load_board()
    overrides = {
        "REPO": proj,
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": local,
        "REGRESSION_FLAG": local / "regression.json",
        "LEASES_FILE": local / "worktree-leases.json",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    monkeypatch.setattr(mod, "_git_head", lambda: "deadbeef01234567")
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    mod._proj = proj
    return mod


class _FakeProc:
    """`subprocess.Popen` 반환 대역 — 줄 단위로 읽히는 stdout/stderr + 고정 rc."""

    def __init__(self, rc: int, stdout: str = "", stderr: str = ""):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self._rc = rc

    def wait(self):
        return self._rc


class _FakeRun:
    """board.subprocess.Popen 대역 — 고정 rc + 스트림 출력을 돌려주고 호출 kwargs 를 기록한다.

    수집 하한 가드는 *실행 출력의 pytest 요약행*을 읽으므로 대역도 stdout 을 실어야 한다
    (실 pytest 미기동). `stderr` 는 **파싱이 stdout 단독인지** 보는 함정용이다 — 합쳐서 읽으면
    stderr 로그의 카운트 문자열이 요약행 행세를 해 수집수를 오염시킨다. cwd 별 rc/출력 분기는
    M>1 슬롯 순회 테스트가 쓴다.
    """

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "",
                 by_cwd: dict[str, tuple[int, str]] | None = None):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.by_cwd = by_cwd or {}
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        rc, out = self.by_cwd.get(kwargs.get("cwd"), (self.rc, self.stdout))
        return _FakeProc(rc, out, self.stderr)


def _run_args(**over):
    base = dict(action="run", cmd=None, ticket=None, touches=None)
    base.update(over)
    return argparse.Namespace(**base)


def _set_floor(board, value, tree: Path | None = None) -> None:
    """수집 하한을 선언한다 — 기본은 이 board 사본의 `LOCAL_CONF`, `tree` 주면 그 트리의 conf.

    회귀가 도는 트리(run cwd)가 앵커이므로, 앵커 검증 테스트는 `tree` 로 다른 트리에 심는다.
    """
    conf = (tree / ".project_manager" / "local.conf") if tree else board.LOCAL_CONF
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(f"regression_min_collected={value}\n", encoding="utf-8")


def _flag(board) -> dict:
    return json.loads(board.REGRESSION_FLAG.read_text(encoding="utf-8"))


def _write_flag(board, *, status: str, rc, collected=None, floor=None,
                head: str = "deadbeef01234567", **extra) -> None:
    """공유 회귀 플래그를 직접 심는다 (check 판정의 baseline 주입).

    `conf_anchor` 미기록이 기본 — 옛 플래그(후방호환 폴백) 형상을 그대로 태운다.
    """
    board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    board.REGRESSION_FLAG.write_text(json.dumps(
        {"head": head, "status": status, "rc": rc, "scope": "full",
         "collected": collected, "floor": floor,
         "ts": "2026-08-07T00:00:00+00:00", **extra}), encoding="utf-8")


def _install_run(board, monkeypatch, fake) -> _FakeRun:
    """회귀 러너 seam(`subprocess.Popen` — tee 하며 읽는다)을 대역으로 덮는다."""
    monkeypatch.setattr(board.subprocess, "Popen", fake)
    return fake


# ════════════════════════════════════════════════════════════════════════
# ① 요약행 파서 — 실행 수 = passed + skipped + failed(+error/xfail/xpass)
# ════════════════════════════════════════════════════════════════════════

def test_collected_count_parses_passed_only(board):
    """`N passed in Xs` 표준 요약행 → N."""
    assert board._collected_count("7577 passed in 145.23s\n") == 7577


def test_collected_count_sums_passed_skipped_failed(board):
    """`N passed, M skipped` 형식 — 수집수 = passed + skipped + failed 합 (DoD)."""
    out = "3 failed, 7577 passed, 12 skipped in 149.90s\n"
    assert board._collected_count(out) == 3 + 7577 + 12


def test_collected_count_ignores_deselected_and_warnings(board):
    """deselected(quarantine·마커 필터)는 *돌지 않은* 수 — 세지 않는다. warning 도 아니다."""
    out = "100 passed, 4 deselected, 2 warnings in 3.10s\n"
    assert board._collected_count(out) == 100


def test_collected_count_counts_xfail_and_error_kinds(board):
    """xfailed/xpassed/error 도 실행분 — passed 와 독립 카운트로 합산된다."""
    out = "1 failed, 2 passed, 3 xfailed, 4 xpassed, 5 errors in 1.00s\n"
    assert board._collected_count(out) == 1 + 2 + 3 + 4 + 5


def test_collected_count_uses_last_summary_line(board):
    """테스트 자신이 찍은 `3 passed` 로그가 섞여도 **끝에서부터** 요약행을 찾는다.

    출력 전체 검색이면 앞쪽 로그를 먼저 만나 수집수를 오판 → 하한 가드가 엉뚱하게 발화한다.
    """
    out = "captured log: subprocess reported 3 passed\n7577 passed in 145.23s\n"
    assert board._collected_count(out) == 7577


def test_collected_count_ignores_trailing_wrapper_log(board):
    """실제 요약 **뒤에** 오는 wrapper/plugin 로그를 요약으로 뽑지 않는다 (하한 우회 차단).

    끝에서-탐색만으로는 마지막 카운트 줄이 이기므로, 요약행 **문법 완전 일치**로 거른다
    (codex must-fix — 이게 없으면 후행 `8000 passed` 한 줄로 부분수집이 통과한다).
    """
    out = ("12 passed in 0.30s\n"
           "[wrapper] post-run report: 8000 passed, 1 failed in 900.00s\n")
    assert board._collected_count(out) == 12


def test_summary_line_accepts_real_pytest_forms(board):
    """실 pytest 표기 변형을 모두 요약행으로 받는다 — 문법 강화가 정상 출력을 떨구면 false-RED."""
    for line in (
            "12 passed in 0.30s",
            "=================== 1 failed, 10 passed in 1.00s ===================",
            "7577 passed, 40 skipped, 99 warnings in 388.86s (0:06:28)",
            "3 passed in 0.30 seconds",                     # 구 pytest 표기.
            "\x1b[32m12 passed\x1b[0m in 0.30s",            # --color=yes.
    ):
        assert board._is_pytest_summary_line(line), line
    assert board._collected_count(
        "7577 passed, 40 skipped, 99 warnings in 388.86s (0:06:28)\n") == 7617


def test_summary_line_rejects_prose_and_partial_forms(board):
    """카운트를 품은 산문·미완성 형태는 요약행이 아니다 (`in <초>s` 종결 앵커 요구)."""
    for line in (
            "captured stdout: child runner reported 3 passed, 1 failed in 0.01s",
            "12 passed",                                    # 종결 앵커 없음.
            "collected 7589 items",
            "PASSED tests/test_x.py::test_y",
    ):
        assert not board._is_pytest_summary_line(line), line


def test_collected_count_none_when_no_summary(board):
    """요약행 부재(수집 0 "no tests ran"·비-pytest 출력) → None (파싱 실패·가드 skip 신호)."""
    assert board._collected_count("no tests ran in 0.01s\n") is None
    assert board._collected_count("") is None


# ════════════════════════════════════════════════════════════════════════
# ② conf 키 — 기본 0(off) · 채택자 opt-in · 비정수는 경고 후 off
# ════════════════════════════════════════════════════════════════════════

def test_min_collected_default_off(board):
    """local.conf 미설정 → 0 (가드 off·엔진은 보편 하한을 정하지 않는다)."""
    assert board._regression_min_collected() == 0


def test_min_collected_reads_conf(board):
    """`regression_min_collected=7000` → 7000 (채택자가 자기 스위트 규모로 선언)."""
    _set_floor(board, 7000)
    assert board._regression_min_collected() == 7000


def test_min_collected_malformed_warns_and_disables(board, capsys):
    """비정수/음수 값 → 0(off) + 경고 1줄 (오타로 게이트가 조용히 죽지 않게)."""
    _set_floor(board, "seven-thousand")
    assert board._regression_min_collected() == 0
    assert "regression_min_collected" in capsys.readouterr().err
    _set_floor(board, -5)
    assert board._regression_min_collected() == 0
    assert "비정수/음수" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# ③ FULL 게이트 run 강등 — rc0 인데 수집 < 하한 → fail(partial-collection)
# ════════════════════════════════════════════════════════════════════════

def test_run_below_floor_records_fail_and_check_blocks_push(
        board, monkeypatch, capsys):
    """rc0 + 수집수 < 하한 → status fail 기록 · run rc1 · 후속 check rc1 (push 차단·DoD).

    부분 수집(cwd/pythonpath 파손으로 스위트 일부만 돎)이 pass 로 기록되던 false-green 을 닫는다.
    """
    _set_floor(board, 7000)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="12 passed in 0.30s\n"))
    assert board.cmd_regression(_run_args()) == 1
    data = _flag(board)
    assert data["status"] == "fail", f"부분 수집이 pass 로 기록됨: {data!r}"
    assert data["rc"] == board.REGRESSION_RC_PARTIAL_COLLECTION
    assert data["collected"] == 12
    out = capsys.readouterr().out
    assert "수집 12<하한 7000" in out
    # pre-push 훅 채널 — 기록된 fail 을 check 가 RED 로 차단하고 사유를 드러낸다.
    assert board.cmd_regression(argparse.Namespace(action="check")) == 1
    err = capsys.readouterr().err
    assert "RED" in err
    assert "rc=partial-collection" in err
    assert "수집 12<하한 7000" in err
    assert "push 차단" in err


def test_run_at_floor_passes(board, monkeypatch):
    """수집수 == 하한 → pass (경계는 `<` — 정당한 테스트 add/remove 에 false-RED 금지)."""
    _set_floor(board, 7000)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="7000 passed in 140.00s\n"))
    assert board.cmd_regression(_run_args()) == 0
    data = _flag(board)
    assert data["status"] == "pass"
    assert data["rc"] == 0
    assert data["collected"] == 7000


def test_run_floor_zero_default_leaves_behavior_unchanged(board, monkeypatch, capsys):
    """하한 0(기본·미설정) → 수집수가 아무리 적어도 현행 동작 무변경 (DoD).

    엔진 기본은 off 다 — 하한은 스위트 규모의 함수라 보편값을 정할 수 없다.
    """
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="3 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    data = _flag(board)
    assert data["status"] == "pass"
    assert data["rc"] == 0
    assert data["collected"] == 3        # 진단용 부기는 하되 판정엔 안 쓴다.
    out, err = capsys.readouterr()
    assert "pass (rc=0)" in out
    assert "하한" not in out and "하한" not in err
    assert board.cmd_regression(argparse.Namespace(action="check")) == 0


def test_run_parse_failure_with_floor_records_unverified_and_blocks(
        board, monkeypatch, capsys):
    """파싱 실패 + 하한>0 → `unverified-collection` fail 기록·check 차단 (검증 못 한 결과는 pass 아님).

    경고만 내고 pass 로 기록하면 "설정된 하한을 검증하지 못한 결과"가 push 를 통과해 이 가드가
    막으려던 false-green 이 그대로 재도입된다(codex must-fix — 원 결정 뒤집음).
    """
    _set_floor(board, 7000)
    _install_run(board, monkeypatch,
                 _FakeRun(rc=0, stdout="[custom runner] everything ok\n"))
    assert board.cmd_regression(_run_args()) == 1
    data = _flag(board)
    assert data["status"] == "fail"
    assert data["rc"] == board.REGRESSION_RC_UNVERIFIED_COLLECTION
    assert data["collected"] is None
    assert data["floor"] == 7000
    assert "하한 7000 검증 불가" in capsys.readouterr().out
    assert board.cmd_regression(argparse.Namespace(action="check")) == 1
    err = capsys.readouterr().err
    assert "rc=unverified-collection" in err and "검증 불가" in err


def test_run_parse_failure_without_floor_is_unchanged(board, monkeypatch, capsys):
    """하한 off(기본)면 파싱 실패는 현행대로 pass — 미설정 채택자 동작·소음 0."""
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="[custom runner] ok\n"))
    assert board.cmd_regression(_run_args()) == 0
    data = _flag(board)
    assert data["status"] == "pass" and data["collected"] is None and data["floor"] == 0
    out, err = capsys.readouterr()
    assert "검증 불가" not in out + err


def test_run_real_red_keeps_its_rc(board, monkeypatch):
    """실 red(rc≠0)는 하한 판정 대상이 아니다 — 기록 rc 는 그 rc 그대로(사유 구분 유지)."""
    _set_floor(board, 7000)
    _install_run(board, monkeypatch,
                 _FakeRun(rc=1, stdout="1 failed, 11 passed in 0.40s\n"))
    assert board.cmd_regression(_run_args()) == 1
    data = _flag(board)
    assert data["status"] == "fail"
    assert data["rc"] == 1


def test_scoped_run_is_not_gated_by_floor(board, monkeypatch, capsys):
    """스코프(touches) 실행은 하한 대상이 아니다 — 매칭분만 도는 게 정상(상시 false-RED 방지).

    스코프는 dev 피드백 advisory 라 push 게이트 플래그도 안 쓴다(현행 유지).
    """
    _set_floor(board, 7000)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="2 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args(touches="tests/test_board_regression.py")) == 0
    assert not board.REGRESSION_FLAG.exists()
    out = capsys.readouterr().out
    assert "push 게이트 아님" in out
    assert "하한" not in out


# ════════════════════════════════════════════════════════════════════════
# ③-b 하한 앵커 = 회귀를 도는 트리(run cwd) — 호출 사본 REPO 의 conf 가 새면 안 된다
# ════════════════════════════════════════════════════════════════════════

def test_floor_anchors_on_run_cwd_not_module_repo(board, monkeypatch, tmp_path, capsys):
    """모듈 REPO conf 의 하한은 **다른 트리**의 회귀에 적용되지 않는다 (누출 차단).

    두-git/multi-repo 홈에서 호출 사본 REPO 는 tests/ 없는 PM 홈이거나 남의 repo다 — 그 선언이
    실행 트리에 새면 엉뚱한 스위트를 강등한다(개발 머신 conf 가 tmp 스텁 회귀를 깨뜨린 실측).
    """
    other_tree = tmp_path / "other"
    other_tree.mkdir()
    _set_floor(board, 999999)                        # 호출 사본(REPO) conf — 적용되면 안 됨.
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="12 passed in 0.30s\n"))
    assert board.cmd_regression(_run_args(cwd=str(other_tree))) == 0
    data = _flag(board)
    assert data["status"] == "pass"
    assert data["floor"] == 0                        # 실행 트리엔 선언이 없다.


def test_floor_reads_declaration_of_the_tree_that_runs(board, monkeypatch, tmp_path):
    """반대 방향 — 실행 트리(run cwd)의 선언은 REPO 에 없어도 적용된다."""
    run_tree = tmp_path / "suite"
    run_tree.mkdir()
    _set_floor(board, 7000, tree=run_tree)           # 실행 트리에만 선언.
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="12 passed in 0.30s\n"))
    assert board.cmd_regression(_run_args(cwd=str(run_tree))) == 1
    data = _flag(board)
    assert data["status"] == "fail"
    assert data["rc"] == board.REGRESSION_RC_PARTIAL_COLLECTION
    assert data["floor"] == 7000                     # 실행 당시 하한을 기록.


# ════════════════════════════════════════════════════════════════════════
# ③-c green 재사용 무효화 — 하한 신규/상향은 옛 green 을 통과시키지 않는다
# ════════════════════════════════════════════════════════════════════════

def test_check_invalidates_green_when_floor_raised(board, capsys):
    """green 기록(수집 12) + 하한 신규 7000 → check 가 stale 로 재실행 요구 (통과 금지).

    HEAD·status 만 보면 하한을 켠 뒤에도 옛 green 이 통과해 새 하한이 영영 미적용이다.
    """
    _write_flag(board, status="pass", rc=0, collected=12, floor=0)
    _set_floor(board, 7000)
    assert board.cmd_regression(argparse.Namespace(action="check")) == 1
    err = capsys.readouterr().err
    assert "stale" in err and "현재 하한 7000" in err


def test_check_keeps_green_when_evidence_satisfies_lowered_floor(board):
    """기록 수집수가 현재 하한을 이미 만족하면 green 유지 — 하한 하향에 불필요한 재실행 0."""
    _write_flag(board, status="pass", rc=0, collected=7577, floor=7500)
    _set_floor(board, 7000)
    assert board.cmd_regression(argparse.Namespace(action="check")) == 0


def test_check_invalidates_green_without_collected_when_floor_active(board, capsys):
    """수집수 미기록(옛 플래그) + 하한 활성 → 검증 불가라 stale (무한 재실행 없음: 재run 은 fail 기록)."""
    _write_flag(board, status="pass", rc=0, collected=None, floor=None)
    _set_floor(board, 7000)
    assert board.cmd_regression(argparse.Namespace(action="check")) == 1
    assert "stale" in capsys.readouterr().err


def test_check_old_flag_without_collected_passes_when_floor_off(board):
    """하한 off 면 옛 플래그(수집수 필드 없음)도 그대로 green — 후방호환."""
    _write_flag(board, status="pass", rc=0, collected=None, floor=None)
    assert board.cmd_regression(argparse.Namespace(action="check")) == 0


def test_check_resolves_floor_from_recorded_anchor_not_check_cwd(
        board, monkeypatch, tmp_path, capsys):
    """`run --cwd A` 뒤의 check 는 **A 의** 하한으로 판정한다 — 훅은 `--cwd` 를 못 넘긴다.

    check 가 자기 cwd 로 재해소하면 실행 트리(A)의 하한 상향을 놓쳐 옛 green 이 통과한다
    (false-green·codex must-fix).
    """
    tree_a = tmp_path / "A"
    tree_a.mkdir()
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="12 passed in 0.30s\n"))
    assert board.cmd_regression(_run_args(cwd=str(tree_a))) == 0   # A 하한 0 → green.
    assert _flag(board)["conf_anchor"] == str(tree_a)
    _set_floor(board, 7000, tree=tree_a)      # 실행 트리(A)만 상향. check cwd(REPO)는 하한 0.
    assert board.cmd_regression(argparse.Namespace(action="check")) == 1
    assert "현재 하한 7000" in capsys.readouterr().err


def test_check_ignores_unrelated_floor_of_its_own_cwd(board, monkeypatch, tmp_path):
    """반대 방향 — check 트리에만 있는 무관한 하한으로 막지 않는다(false-RED 차단)."""
    tree_a = tmp_path / "A"
    tree_a.mkdir()
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="12 passed in 0.30s\n"))
    assert board.cmd_regression(_run_args(cwd=str(tree_a))) == 0
    _set_floor(board, 9999)                   # check 자기 트리(REPO)에만 선언 — 무관.
    assert board.cmd_regression(argparse.Namespace(action="check")) == 0


def test_slot_state_uses_recorded_anchor_over_slot_cwd(multi_board, tmp_path):
    """슬롯 판정도 기록된 conf 앵커를 우선한다 — 슬롯 cwd 와 다른 트리에서 돈 기록(`--cwd` 핀)."""
    b = multi_board
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    _set_floor(b, 7000, tree=pinned)          # 앵커 트리만 상향.
    flag = b._regression_flag_for("A_1")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(json.dumps(
        {"head": "HEAD_A", "status": "pass", "rc": 0, "scope": "full", "collected": 12,
         "floor": 0, "conf_anchor": str(pinned), "session": "A_1",
         "ts": "2026-08-07T00:00:00+00:00"}), encoding="utf-8")
    assert b._regression_slot_state("A_1", _slot_cwd(b, "A_1")).state == "stale"


def _seed_anchor_flag(board, anchor: Path, *, status="fail",
                      rc=None, collected=12, floor=7000) -> None:
    """앵커가 박힌 공유 플래그를 심는다 (훅 `check || run` 재실행의 baseline)."""
    _write_flag(board, status=status,
                rc=(board.REGRESSION_RC_PARTIAL_COLLECTION if rc is None else rc),
                collected=collected, floor=floor, conf_anchor=str(anchor))


def test_run_inherits_anchor_of_blocking_record(board, monkeypatch, tmp_path):
    """차단 기록(fail@A)이 있으면 무-`--cwd` 재실행이 **A 에서** 돈다 — 게이트 우회 차단.

    훅은 `check || run` 이고 `--cwd` 를 못 넘긴다. 재실행이 기본 트리 B 에서 돌면 B 의 green 이
    A 의 RED 기록을 덮어 그대로 push 된다(codex must-fix).
    """
    tree_a = tmp_path / "A"
    tree_a.mkdir()
    _seed_anchor_flag(board, tree_a)
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="7577 passed in 140.00s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert fake.calls[0]["kwargs"]["cwd"] == str(tree_a), "기본 트리에서 돌아 A 기록을 덮었다"
    assert _flag(board)["conf_anchor"] == str(tree_a)   # 갱신도 같은 앵커.


def test_run_inherits_anchor_of_floor_stale_green(board, monkeypatch, tmp_path):
    """하한 미달 green(=check 가 stale 로 막는 기록)도 차단 기록 — 같은 앵커에서 재실행."""
    tree_a = tmp_path / "A"
    tree_a.mkdir()
    _set_floor(board, 7000, tree=tree_a)
    _seed_anchor_flag(board, tree_a, status="pass", rc=0, collected=12, floor=0)
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="7577 passed in 140.00s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert fake.calls[0]["kwargs"]["cwd"] == str(tree_a)


def test_run_does_not_inherit_anchor_of_green_record(board, monkeypatch, tmp_path):
    """차단 기록이 아니면 이어받지 않는다 — 릴리즈 `--cwd <readonly>` 핀이 눌러붙지 않게."""
    tree_a = tmp_path / "A"
    tree_a.mkdir()
    _seed_anchor_flag(board, tree_a, status="pass", rc=0, collected=7577, floor=0)
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="7577 passed in 140.00s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert fake.calls[0]["kwargs"]["cwd"] == str(board.REPO)


def test_run_explicit_cwd_wins_over_recorded_anchor(board, monkeypatch, tmp_path):
    """명시 `--cwd` 는 기록 앵커를 이긴다 (사람이 고른 실행 위치가 최우선)."""
    tree_a, tree_b = tmp_path / "A", tmp_path / "B"
    tree_a.mkdir()
    tree_b.mkdir()
    _seed_anchor_flag(board, tree_a)
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="7577 passed in 140.00s\n"))
    assert board.cmd_regression(_run_args(cwd=str(tree_b))) == 0
    assert fake.calls[0]["kwargs"]["cwd"] == str(tree_b)


def test_run_falls_back_when_recorded_anchor_is_gone(board, monkeypatch, tmp_path, capsys):
    """앵커 트리가 정리됐으면 기본 트리로 폴백하고 그 사실을 시끄럽게 알린다 (fail-soft)."""
    _seed_anchor_flag(board, tmp_path / "removed")
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="7577 passed in 140.00s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert fake.calls[0]["kwargs"]["cwd"] == str(board.REPO)
    assert "앵커" in capsys.readouterr().err


def test_recorded_anchor_is_absolute(board, monkeypatch, tmp_path):
    """상대 `--cwd` 도 절대경로로 정규화해 실행·기록한다 — 이후 해소가 프로세스 cwd 에 안 흔들린다."""
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="7577 passed in 140.00s\n"))
    assert board.cmd_regression(_run_args(cwd="sub")) == 0
    assert fake.calls[0]["kwargs"]["cwd"] == str(tmp_path / "sub")
    assert _flag(board)["conf_anchor"] == str(tmp_path / "sub")


def test_corrupt_collected_degrades_instead_of_crashing(board, monkeypatch, capsys):
    """손상된 `collected`(문자열)는 비교에서 죽지 않고 검증 불가로 강등된다 (fail-soft).

    정규화 없이 `<` 비교에 들어가면 TypeError 로 게이트 자체가 죽는다 — 장부 손상이 회귀
    해소를 깨면 안 된다는 기존 규율과 같은 축(codex suggestion).
    """
    _set_floor(board, 7000)
    _write_flag(board, status="pass", rc=0, collected="많음", floor=7000)
    assert board.cmd_regression(argparse.Namespace(action="check")) == 1   # TypeError 아님.
    assert "stale" in capsys.readouterr().err
    # 슬롯 경로도 동형 — 손상값이 green 판정에 들어가지 않는다.
    assert board._flag_collected({"collected": ["12"]}) is None
    assert board._flag_collected({"collected": True}) is None   # bool 은 정수 취급 금지.
    assert board._flag_collected({"collected": 12}) == 12


# ════════════════════════════════════════════════════════════════════════
# ④ tee — 실시간 출력(무출력 대기 근절) + 파싱은 stdout 단독
# ════════════════════════════════════════════════════════════════════════

_VERBOSE_BODY = "collecting ...\nFAILED tests/test_x.py::test_y - AssertionError\n"


def test_tee_stream_echoes_each_line_and_returns_all(board):
    """tee 는 줄 단위로 즉시 echo 하면서 전체를 모아 돌려준다 (가시성 + 파싱 버퍼 동시)."""
    echoed: list[str] = []
    text = board._tee_stream(io.StringIO("a\nb\nc\n"), echoed.append)
    assert echoed == ["a\n", "b\n", "c\n"], "줄이 모이고 나서 한꺼번에 나오면 무출력 대기가 남는다"
    assert text == "a\nb\nc\n"


def test_tee_stream_handles_missing_stream(board):
    """스트림 미개방(None)은 빈 문자열 — 러너가 죽지 않는다."""
    assert board._tee_stream(None, lambda line: None) == ""


def test_run_regression_cmd_with_real_child_splits_streams(board, tmp_path, capsys):
    """실 자식 1개로 러너 배선을 확인한다 — rc·stdout/stderr 분리·교착 없음.

    대역은 스레드 tee 의 실 동작(파이프가 차서 자식이 멈추는 교착)을 증명하지 못한다. pytest 가
    아니라 두 스트림에 쓰고 종료하는 최소 스크립트를 돌린다(하네스 무관·프로덕션 진입점 아님).
    """
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('12 passed in 0.30s\\n')\n"
        "sys.stderr.write('노이즈 8000 passed\\n')\n"
        "sys.exit(3)\n", encoding="utf-8")
    cmd = f'"{sys.executable}" "{script}"'
    rc, out, err = board._run_regression_cmd(cmd, str(tmp_path), dict(os.environ))
    assert rc == 3
    assert "12 passed in 0.30s" in out
    assert "노이즈 8000 passed" in err
    assert "노이즈" not in out, "stderr 가 stdout 버퍼로 섞이면 파싱이 오염된다"
    assert board._collected_count(out) == 12
    streamed = capsys.readouterr()
    assert "12 passed" in streamed.out and "노이즈" in streamed.err   # 실시간 tee.


def test_run_streams_child_output_live(board, monkeypatch, capsys):
    """pass 든 fail 이든 자식 출력이 그대로 흘러나온다 — 캡처가 가시성을 삼키지 않는다.

    캡처만 하던 형태에선 `git push` 회귀가 수 분간 완전 무출력이었다(실측 367s).
    """
    _install_run(board, monkeypatch,
                 _FakeRun(rc=0, stdout=_VERBOSE_BODY + "7577 passed in 140.00s\n"))
    assert board.cmd_regression(_run_args()) == 0
    out = capsys.readouterr().out
    assert "collecting ..." in out and "7577 passed in 140.00s" in out


def test_run_streams_stderr_to_stderr(board, monkeypatch, capsys):
    """자식 stderr 는 stderr 로 흘린다 — 스트림 구분을 뭉개지 않는다(진단 보존)."""
    _install_run(board, monkeypatch, _FakeRun(
        rc=1, stdout="1 failed, 10 passed in 1.00s\n", stderr="ERROR: import 실패\n"))
    assert board.cmd_regression(_run_args()) == 1
    captured = capsys.readouterr()
    assert "ERROR: import 실패" in captured.err
    assert "1 failed, 10 passed" in captured.out


def test_stderr_counts_do_not_pollute_parsing(board, monkeypatch):
    """stderr 의 카운트 문자열(`8000 passed`)이 stdout 요약(`12 passed`)을 이기면 안 된다.

    두 스트림을 합쳐 읽으면 stderr 로그가 "마지막 요약행" 행세를 해 수집수가 오염된다
    (false-green/false-RED 양방향). pytest 요약은 stdout 에만 나오므로 파싱은 stdout 단독이다.
    """
    _set_floor(board, 7000)
    fake = _install_run(board, monkeypatch, _FakeRun(
        rc=0, stdout="12 passed in 0.30s\n", stderr="8000 passed in 900.00s\n"))
    assert board.cmd_regression(_run_args()) == 1
    data = _flag(board)
    assert data["collected"] == 12, "stderr 의 카운트 문자열이 수집수를 오염시켰다"
    assert data["rc"] == board.REGRESSION_RC_PARTIAL_COLLECTION
    # 메커니즘 핀 — 두 스트림을 합치는 형태로 되돌리면 위 오염이 되살아난다.
    kwargs = fake.calls[0]["kwargs"]
    assert kwargs.get("stdout") is board.subprocess.PIPE
    assert kwargs.get("stderr") is board.subprocess.PIPE


# ════════════════════════════════════════════════════════════════════════
# ⑤ M>1 슬롯 순회 — 같은 강등이 슬롯 경로에도 배선되고 check 라벨에 사유가 실린다
# ════════════════════════════════════════════════════════════════════════

def _write_ledger(board, *sessions: str) -> None:
    """리스 장부에 leased 행을 쓴다 (`slot=work/<session>`·worktree_pool 스키마 부분집합)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({"leases": [
        {"slot": f"work/{s}", "repo": "r", "session": s, "state": "leased"}
        for s in sessions]}), encoding="utf-8")


def _slot_cwd(board, session: str) -> str:
    return str(board.REPO / "work" / session)


@pytest.fixture
def multi_board(board, monkeypatch):
    """board + 2 leased 슬롯(A_1·B_1)·슬롯별 HEAD mock — M>1 all-or-nothing 순회 전제."""
    _write_ledger(board, "A_1", "B_1")
    heads = {_slot_cwd(board, "A_1"): "HEAD_A", _slot_cwd(board, "B_1"): "HEAD_B"}
    monkeypatch.setattr(board, "_git_head_at", lambda cwd: heads.get(cwd, "HEAD_?"))
    return board


def _set_slot_floor(board, session: str, value) -> None:
    """그 슬롯 worktree 트리에 하한을 선언한다 (슬롯별 앵커 = 그 슬롯이 회귀를 도는 트리)."""
    _set_floor(board, value, tree=Path(_slot_cwd(board, session)))


def test_multi_run_below_floor_blocks_and_records(multi_board, monkeypatch, capsys):
    """M>1 순회에서도 rc0+부분수집 슬롯은 fail(partial-collection) 기록 + rc1 (all-or-nothing)."""
    b = multi_board
    _set_slot_floor(b, "A_1", 7000)
    _set_slot_floor(b, "B_1", 7000)
    _install_run(b, monkeypatch, _FakeRun(by_cwd={
        _slot_cwd(b, "A_1"): (0, "7577 passed in 140.00s\n"),
        _slot_cwd(b, "B_1"): (0, "12 passed in 0.30s\n"),      # 부분 수집
    }))
    assert b.cmd_regression(_run_args()) == 1
    a_flag = json.loads(b._regression_flag_for("A_1").read_text(encoding="utf-8"))
    b_flag = json.loads(b._regression_flag_for("B_1").read_text(encoding="utf-8"))
    assert a_flag["status"] == "pass" and a_flag["collected"] == 7577
    assert b_flag["status"] == "fail"
    assert b_flag["rc"] == b.REGRESSION_RC_PARTIAL_COLLECTION
    assert b_flag["collected"] == 12
    err = capsys.readouterr().err
    assert "RED" in err and "B_1" in err


def test_check_hint_uses_recorded_floor_not_current_conf(board, capsys):
    """강등 기록 후 conf 하한이 바뀌어도 check 사유는 **기록된** 하한으로 설명한다.

    check 시점 conf 를 다시 읽으면 과거 기록을 지금 값으로 설명하는 오보가 된다.
    """
    _write_flag(board, status="fail", rc=board.REGRESSION_RC_PARTIAL_COLLECTION,
                collected=12, floor=7000)
    _set_floor(board, 9000)          # 기록 이후 하한 변경.
    assert board.cmd_regression(argparse.Namespace(action="check")) == 1
    err = capsys.readouterr().err
    assert "수집 12<하한 7000" in err   # 기록값 기준(9000 아님).


def test_multi_check_label_carries_shortfall_hint(multi_board, capsys):
    """check 라벨에 부분수집 사유 — `B_1=red(rc=partial-collection·수집 N<하한 M)` (rc5 힌트와 동형)."""
    b = multi_board
    _set_slot_floor(b, "A_1", 7000)
    _set_slot_floor(b, "B_1", 7000)
    for session, head, payload in (
            # A_1 = green(증거가 하한 충족) · B_1 = 부분수집 강등 기록.
            ("A_1", "HEAD_A", {"status": "pass", "rc": 0, "collected": 7577, "floor": 7000}),
            ("B_1", "HEAD_B", {"status": "fail",
                               "rc": b.REGRESSION_RC_PARTIAL_COLLECTION,
                               "collected": 12, "floor": 7000}),
    ):
        flag = b._regression_flag_for(session)
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps({"head": head, "scope": "full", "session": session,
                                    "ts": "2026-08-07T00:00:00+00:00", **payload}),
                        encoding="utf-8")
    assert b.cmd_regression(argparse.Namespace(action="check")) == 1
    err = capsys.readouterr().err
    assert "B_1=red(rc=partial-collection·수집 12<하한 7000)" in err
    assert "A_1" not in err          # 증거가 하한을 충족한 슬롯은 green 유지.


def test_multi_run_reruns_green_slot_when_floor_raised(multi_board, monkeypatch, capsys):
    """green 기록 슬롯도 하한 상향 뒤엔 check-first skip 대상이 아니다 — 재실행돼 새 하한 적용.

    skip 되면 M>1 명시 run 으로도 새 하한이 영영 적용되지 않는다(codex must-fix A).
    """
    b = multi_board
    for session, head in (("A_1", "HEAD_A"), ("B_1", "HEAD_B")):
        flag = b._regression_flag_for(session)
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps(
            {"head": head, "status": "pass", "rc": 0, "scope": "full",
             "collected": 12, "floor": 0, "session": session,
             "ts": "2026-08-07T00:00:00+00:00"}), encoding="utf-8")
    _set_slot_floor(b, "A_1", 7000)          # A_1 만 하한 신규 → 재실행 대상.
    fake = _install_run(b, monkeypatch, _FakeRun(rc=0, stdout="12 passed in 0.30s\n"))
    assert b.cmd_regression(_run_args()) == 1
    assert fake.calls, "green skip 으로 재실행이 아예 없었다"
    assert [c["kwargs"]["cwd"] for c in fake.calls] == [_slot_cwd(b, "A_1")]
    a_flag = json.loads(b._regression_flag_for("A_1").read_text(encoding="utf-8"))
    assert a_flag["status"] == "fail"
    assert a_flag["rc"] == b.REGRESSION_RC_PARTIAL_COLLECTION
    assert "skip(green) 1 · run 1" in capsys.readouterr().err   # B_1 만 skip.


# ════════════════════════════════════════════════════════════════════════
# ⑥ 요약행 파서 공용 seam — 소비처 전수가 끝에서-탐색을 쓴다
# ════════════════════════════════════════════════════════════════════════
#
# 첫-매칭 `re.search` 파서가 도구마다 복제돼 있었다 — 캡처 출력에 테스트/자식 러너가 찍은
# `N passed`·`N failed` 로그가 섞이면 그걸 요약행으로 오판한다(수집수 오염·green↔red 전도).
# 올바른 구현(board 의 끝에서-탐색)을 공용 seam 으로 승격하고 소비처 넷을 갈아탔다. 아래는
# **소비처마다** "중간에 가짜 로그 + 끝에 실제 요약" 을 먹여 끝-탐색이 실제로 배선됐는지
# 본다 — 어느 소비처든 자기 사본으로 되돌아가면 red 다.

# 가짜 로그: `3 passed`(수집수 오판 유발)와 `1 failed`(green→red 전도 유발)를 함께 싣는다.
_FAKE_LOG = (
    "collecting ... collected 7589 items\n"
    "captured stdout: child runner reported 3 passed, 1 failed in 0.01s\n"
)

# seam 을 소비하는 도구 — 여기에 자기 요약행 정규식이 남아 있으면 승격이 반쪽이다.
_SEAM_CONSUMER_TOOLS = ("board.py", "pm_bootstrap.py", "ticket_finish.py", "pm_handoff.py")
# 소스에 박힌 outcome 정규식 리터럴(`(\d+) passed`·`\d+ failed` …) 탐지용.
_LOCAL_OUTCOME_REGEX = re.compile(r"\\d\+\)? (?:passed|failed|deselected|skipped|errors?)")


def _load_tool(name: str):
    """엔진 도구를 경로 로드한다 (도구는 패키지가 아니므로 importlib · 다른 테스트 관용구 동형).

    소비처는 자기 board 형제 로더로 **실제** seam 을 읽으므로 board 대역을 심지 않는다 —
    배선 자체(로더→seam)가 검증 대상이다.
    """
    spec = importlib.util.spec_from_file_location(f"seam_consumer_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bootstrap():
    return _load_tool("pm_bootstrap")


@pytest.fixture(scope="module")
def ticket_finish():
    return _load_tool("ticket_finish")


@pytest.fixture(scope="module")
def handoff():
    return _load_tool("pm_handoff")


# ── seam 자체 (승계 + 승격으로 늘어난 표면) ────────────────────────────────

def test_seam_outcome_count_reads_one_kind(board):
    """한 종류만 읽는다 — 부재는 None(수 0 과 구분해야 `deselected` 기본 0 이 성립)."""
    line = "5 failed, 1467 passed, 24 deselected in 10.00s"
    assert board._pytest_outcome_count(line, "passed") == 1467
    assert board._pytest_outcome_count(line, "failed") == 5
    assert board._pytest_outcome_count(line, "deselected") == 24
    assert board._pytest_outcome_count(line, "skipped") is None


def test_seam_outcome_counts_narrows_kinds(board):
    """`kinds` 로 셀 종류를 좁힌다 — 소비처마다 '무엇이 실행인가' 가 다르다."""
    line = "1 failed, 6 passed, 3 skipped, 812 deselected in 40.0s"
    assert sum(board._pytest_outcome_counts(line, board._LIVEGATE_RAN_KINDS)) == 7
    assert sum(board._pytest_outcome_counts(line)) == 10   # 기본 6종 = skipped 포함.


def test_seam_summary_tail_cuts_from_kind(board):
    """요약 문자열은 `N passed` 지점부터 줄 끝까지 — 대상 줄은 끝에서-탐색으로 고른다."""
    out = _FAKE_LOG + "5 failed, 1467 passed, 24 deselected in 10.00s\n"
    assert board._pytest_summary_tail(out) == "1467 passed, 24 deselected in 10.00s"


def test_seam_summary_tail_none_when_unresolved(board):
    """요약행 부재·그 종류 부재는 None — 호출부가 자기 폴백(출력 꼬리)을 고른다."""
    assert board._pytest_summary_tail("no tests ran in 0.01s\n") is None
    assert board._pytest_summary_tail("2 errors in 1.00s\n") is None
    assert board._pytest_summary_tail("") is None


# ── 소비처 ① 릴리즈 라이브 게이트 (board 자신도 seam 소비) ─────────────────

def test_livegate_ran_count_ignores_mid_output_log(board):
    """중간 로그의 `3 passed, 1 failed` 를 세면 pin 대조가 통째로 어긋난다."""
    out = _FAKE_LOG + "18 passed, 810 deselected in 45.67s\n"
    assert board._livegate_ran_count(out) == 18


def test_livegate_ran_count_keeps_kind_selection(board):
    """세는 종류는 불변 — passed+failed+error 만(deselected·skipped 는 실행분 아님)."""
    out = "2 errors, 5 passed, 3 skipped, 812 deselected in 3.0s\n"
    assert board._livegate_ran_count(out) == 7


# ── 소비처 ② 부트스트랩 회귀 dump ─────────────────────────────────────────

def test_bootstrap_counts_use_last_summary_line(bootstrap):
    """(passed, total) 은 끝의 요약행 기준 — 중간 로그를 읽으면 (3, 4) 로 오판한다."""
    out = _FAKE_LOG + "3 failed, 7574 passed in 149.90s\n"
    assert bootstrap.parse_pytest_counts(out) == (7574, 7577)


def test_bootstrap_counts_contract_unchanged(bootstrap):
    """반환 계약 불변 — total = passed + failed, 요약행 부재는 None."""
    assert bootstrap.parse_pytest_counts("279 passed in 6.55s") == (279, 279)
    assert bootstrap.parse_pytest_counts("ERROR: no tests collected") is None


def test_bootstrap_counts_without_seam_is_parse_failure(bootstrap, monkeypatch):
    """seam 부재(엔진 사본 불완전)는 파싱 실패로 흐른다 — 첫-매칭 사본으로 폴백하지 않는다."""
    monkeypatch.setattr(bootstrap, "_load_board", lambda: None)
    assert bootstrap.parse_pytest_counts("7577 passed in 145.23s") is None


# ── 소비처 ③ ticket 마감 ─────────────────────────────────────────────────

def test_ticket_finish_output_uses_last_summary_line(ticket_finish):
    """(passed, deselected) 도 끝의 요약행 기준."""
    out = _FAKE_LOG + "1467 passed, 24 deselected in 12.34s\n"
    assert ticket_finish.parse_pytest_output(out) == (1467, 24)


def test_ticket_finish_green_ignores_mid_output_failed_log(ticket_finish):
    """중간 로그의 `1 failed` 로 green 회귀를 red 로 뒤집지 않는다(false-RED 폐쇄)."""
    out = _FAKE_LOG + "7577 passed in 145.23s\n"
    assert ticket_finish.is_pytest_green(out, returncode=0) is True


def test_ticket_finish_green_still_red_on_summary_failure(ticket_finish):
    """요약행에 failed 가 있으면 여전히 red — 판정 방향은 바뀌지 않는다."""
    out = "3 failed, 7574 passed in 149.90s\n"
    assert ticket_finish.is_pytest_green(out, returncode=0) is False


def test_ticket_finish_without_seam_is_fail_closed(ticket_finish, monkeypatch):
    """seam 부재는 파싱 실패 + red — 마감은 fail-closed 로 흐른다."""
    monkeypatch.setattr(ticket_finish, "_load_board_module", lambda: None)
    assert ticket_finish.parse_pytest_output("1472 passed in 12.34s") is None
    assert ticket_finish.is_pytest_green("1472 passed in 12.34s", returncode=0) is False


# ── 소비처 ④ 핸드오프 ────────────────────────────────────────────────────

def test_handoff_summary_uses_last_summary_line(handoff):
    """인계문 회귀 1줄은 끝의 요약행에서 뽑는다 — 중간 로그를 실으면 오보가 인계된다."""
    out = _FAKE_LOG + "5 failed, 1467 passed, 24 deselected in 10.00s\n"
    assert handoff.parse_pytest_summary(out) == "1467 passed, 24 deselected in 10.00s"


def test_handoff_summary_falls_back_to_output_tail(handoff):
    """요약행이 없으면 현행대로 출력 꼬리, 빈 출력은 빈 문자열(계약 불변)."""
    assert handoff.parse_pytest_summary("no tests ran in 0.01s") == "no tests ran in 0.01s"
    assert handoff.parse_pytest_summary("   ") == ""


def test_handoff_green_ignores_mid_output_failed_log(handoff):
    """중간 로그의 `1 failed` 로 인계 게이트를 막지 않는다."""
    out = _FAKE_LOG + "7577 passed in 145.23s\n"
    assert handoff.is_pytest_green(out, returncode=0) is True
    assert handoff.is_pytest_green(out, returncode=1) is False   # rc 가드는 불변.


# ── 소비처 전수 — 요약행 **뒤** 꼬리 오염도 자동 상속으로 걸러진다 ─────────
# 섹션 위쪽 짝(`_FAKE_LOG`)은 "가짜 → 진짜" 순서(요약 *앞* 로그)만 본다. 실측된 반대 방향은
# 소비처 넷이 파서에 **stdout+stderr 병합** 출력을 먹이는 데서 온다 — 자식 하네스가 stderr 에
# 찍은 한 줄이 진짜 요약 *뒤* 꼬리로 붙어 그게 요약으로 채택됐다(ticket_finish (5,0)·bootstrap
# (5,5)·livegate 5·handoff 엉뚱한 줄). 방어는 seam 의 문법 완전 일치 하나이므로 소비처가 자동
# 상속한다 — 아래가 소비처별 짝이다.

# 실측 오염 줄: 접두 산문 + `in X.XXs` 종결까지 갖춰서, 줄머리 앵커 없이는 요약으로 뽑힌다.
_TRAILING_FAKE = "child harness: 5 passed in 1.00s\n"
# 변형: 수집수 부풀리기 + green→red 전도를 함께 노리는 wrapper 리포트.
_TRAILING_FAKE_WRAPPER = "[wrapper] post-run report: 8000 passed, 1 failed in 900.00s\n"


def test_livegate_ran_count_ignores_trailing_fake(board):
    """릴리즈 pin 대조 — 꼬리 오염을 세면 수집 N 이 어긋나 pin 검증이 통째로 무너진다(실측 5)."""
    real = "18 passed, 810 deselected in 45.67s\n"
    assert board._livegate_ran_count(real + _TRAILING_FAKE) == 18
    assert board._livegate_ran_count(real + _TRAILING_FAKE_WRAPPER) == 18


def test_bootstrap_counts_ignore_trailing_fake(bootstrap):
    """부트스트랩 회귀 dump — 꼬리 오염이면 (5, 5) 로 오판하던 자리(실측)."""
    real = "7577 passed in 145.23s\n"
    assert bootstrap.parse_pytest_counts(real + _TRAILING_FAKE) == (7577, 7577)
    assert bootstrap.parse_pytest_counts(real + _TRAILING_FAKE_WRAPPER) == (7577, 7577)


def test_ticket_finish_ignores_trailing_fake(ticket_finish):
    """마감 판정 — 꼬리 오염이면 (5, 0) 으로 오판하고 wrapper 의 `1 failed` 는 green 을 뒤집는다."""
    real = "1467 passed, 24 deselected in 12.34s\n"
    assert ticket_finish.parse_pytest_output(real + _TRAILING_FAKE) == (1467, 24)
    assert ticket_finish.parse_pytest_output(real + _TRAILING_FAKE_WRAPPER) == (1467, 24)
    assert ticket_finish.is_pytest_green(real + _TRAILING_FAKE_WRAPPER, returncode=0) is True


def test_handoff_summary_ignores_trailing_fake(handoff):
    """인계문 회귀 1줄 — 꼬리 오염이면 엉뚱한 줄이 인계문에 실린다(실측)."""
    real = "5 failed, 1467 passed, 24 deselected in 10.00s\n"
    assert handoff.parse_pytest_summary(real + _TRAILING_FAKE) == \
        "1467 passed, 24 deselected in 10.00s"
    assert handoff.parse_pytest_summary(real + _TRAILING_FAKE_WRAPPER) == \
        "1467 passed, 24 deselected in 10.00s"
    assert handoff.is_pytest_green("7577 passed in 145.23s\n" + _TRAILING_FAKE_WRAPPER,
                                   returncode=0) is True


def test_fixture_helper_output_satisfies_seam_grammar(board):
    """픽스처 헬퍼가 만든 요약행을 seam 이 요약행으로 인정한다 — 헬퍼↔문법 합치 못박기.

    픽스처가 `"1 passed"` 같은 축약형을 쓰면 그 경로가 파서를 타는 순간 무더기 red 가 되는
    시한 픽스처가 된다(실측). 형식을 `_pytest_summary.pytest_summary` 한 곳에 두고, 그 산출이
    엔진 문법을 만족한다는 사실을 여기서 고정한다 — 문법을 조이면 이 테스트가 먼저 깨진다.
    """
    for kwargs, expected in (
            ({}, 1),
            ({"passed": 7577, "skipped": 40}, 7617),
            ({"passed": 1467, "failed": 5, "deselected": 24}, 1472),
    ):
        line = pytest_summary(**kwargs)
        assert board._is_pytest_summary_line(line.strip()), line
        assert board._collected_count(line) == expected


def test_seam_rejects_measured_child_harness_tail(board):
    """실측 오염 줄 자체를 seam 이 요약행으로 인정하지 않는다 (줄머리 앵커의 존재 이유)."""
    assert not board._is_pytest_summary_line(_TRAILING_FAKE.strip())
    assert board._collected_count("12 passed in 0.30s\n" + _TRAILING_FAKE) == 12


# ── 사본 0 — 승격이 반쪽으로 남지 않게 못 박는다 ───────────────────────────

def test_no_consumer_keeps_local_outcome_regex():
    """소비처 소스에 outcome 정규식 리터럴이 남으면 red.

    seam 은 종류 이름을 인자로 받아 패턴을 만든다(리터럴 0). 어느 도구든 `\\d+ passed` 류를
    다시 박으면 그 지점이 첫-매칭 오판을 되살리므로 여기서 막는다.
    """
    offenders = [
        name for name in _SEAM_CONSUMER_TOOLS
        if _LOCAL_OUTCOME_REGEX.search((TOOLS / name).read_text(encoding="utf-8"))
    ]
    assert not offenders, offenders


# ════════════════════════════════════════════════════════════════════════
# ⑥ 회귀 스테이징 (T-0593) — 활성 리뷰 사이클 중 FULL → targeted 강등
# ════════════════════════════════════════════════════════════════════════
# 리뷰 라운드마다 FULL 회귀를 도는 것이 라운드 비용의 세 번째 축이었다. 리뷰가 아직 닫히지 않은
# 티켓의 FULL 요청은 그 티켓 touches 로 강등하고, FULL 은 `--final`(수렴 후)·핸드오프·pre-push
# 경로만 돈다. 판정 입력은 이미 있는 데이터 둘뿐이다(사람 선언 0): 외부 리뷰 라운드 장부 +
# 보드 티켓 status. 장부는 append-only 라 자체 마감 이벤트가 없어, **티켓이 현재 claimed 인지**를
# 함께 봐야 done 티켓의 옛 반려가 영원히 강등을 유발하지 않는다(실측: done 10건 잔존).


def _write_rounds_ledger(board, gates: dict) -> None:
    """외부 리뷰 라운드 장부를 심는다 — 값은 `{gate: [(sequence, verdict), ...]}`."""
    board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        gate: {"rounds": [{"sequence": seq, "verdict": verdict, "must_fix": None}
                          for seq, verdict in rounds]}
        for gate, rounds in gates.items()
    }
    payload["wave"] = {"id": "gen", "started": None, "spent": 0}
    board._review_rounds_ledger().write_text(json.dumps(payload), encoding="utf-8")


def _ticket_statuses(board, monkeypatch, statuses: dict) -> None:
    """보드 티켓 status 대역 — `{ticket_id: status}` 이외의 id 는 부재(FileNotFoundError)."""
    def _find(tid):
        if tid not in statuses:
            raise FileNotFoundError(tid)
        return statuses[tid], Path(f"/board/{statuses[tid]}/{tid}.md")

    monkeypatch.setattr(board, "find_ticket", _find)


def test_active_review_cycle_is_the_last_round_not_a_pass(board, monkeypatch):
    """미마감 = (claimed 티켓) ∧ (마지막 라운드가 통과 rc 0 이 아님). 순서는 **예약 순번**."""
    _write_rounds_ledger(board, {
        "T-0001": [(1, 1), (2, 0)],                 # 통과로 끝남 → 마감
        "T-0002": [(1, 0), (2, 1)],                 # 반려로 끝남 → 활성
        "T-0003": [(2, 1), (1, 0)],                 # append 역순 — 순번상 마지막은 반려
        "T-0004": [],                               # 산출 없음 → 판정 불가·활성 아님
    })
    _ticket_statuses(board, monkeypatch, {
        "T-0001": "claimed", "T-0002": "claimed",
        "T-0003": "claimed", "T-0004": "claimed",
    })
    assert board._active_review_gates() == ["T-0002", "T-0003"]


def test_done_ticket_ledger_residue_never_activates(board, monkeypatch):
    """마감된 티켓의 옛 반려 라운드는 활성으로 세지 않는다 — 장부에 마감 이벤트가 없어서다.

    이게 없으면 done 티켓이 쌓일수록 `regression run` 이 영구 targeted 강등이 된다(실측 10건)."""
    _write_rounds_ledger(board, {
        "T-0001": [(1, 1)], "T-0002": [(1, 1)], "T-0003": [(1, 1)], "T-0004": [(1, 1)],
    })
    _ticket_statuses(board, monkeypatch, {
        "T-0001": "done", "T-0002": "open", "T-0003": "blocked",
        # T-0004 는 보드에서 아예 사라진 게이트(자유 문자열·삭제) — 부재도 활성 아님.
    })
    assert board._active_review_gates() == []


def test_done_ticket_residue_does_not_downgrade_a_full_run(board, monkeypatch, capsys):
    """done 티켓 장부 잔존 형상에서 FULL 요청은 강등되지 않고 게이트 플래그를 쓴다 (DoD)."""
    _write_rounds_ledger(board, {"T-0001": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0001": "done"})
    monkeypatch.setattr(board, "_ticket_touches", lambda tid: ["src/pay.py"])
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert "강등" not in capsys.readouterr().out
    assert _flag(board)["scope"] == "full"


def test_missing_or_corrupt_ledger_never_downgrades(board):
    """장부 부재·손상은 강등하지 않는다 — 이 축의 실패가 회귀 게이트를 좁히면 안 된다."""
    assert board._active_review_gates() == []
    board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    board._review_rounds_ledger().write_text("{ not json", encoding="utf-8")
    assert board._active_review_gates() == []


def test_corrupt_ticket_frontmatter_skips_that_gate(board, monkeypatch, capsys):
    """손상 frontmatter 한 건이 모든 `regression run` 을 크래시시키지 않는다 (fail-soft)."""
    _write_rounds_ledger(board, {"T-0002": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0002": "claimed"})

    def _boom(tid):
        raise ValueError("frontmatter 손상")

    monkeypatch.setattr(board, "_ticket_touches", _boom)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    out, err = capsys.readouterr()
    assert "강등 판정에서 제외" in err and "T-0002" in err
    assert "강등" not in out
    assert _flag(board)["scope"] == "full"


def test_full_run_is_downgraded_during_an_active_cycle(board, monkeypatch, capsys):
    """활성 사이클 중 FULL 요청 → touches targeted 강등 + 안내 1줄 (DoD).

    강등된 실행은 스코프 실행과 같다 — push 게이트 플래그를 쓰지 않는다."""
    _write_rounds_ledger(board, {"T-0002": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0002": "claimed"})
    monkeypatch.setattr(board, "_ticket_touches", lambda tid: ["src/pay.py"])
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="2 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    out = capsys.readouterr().out
    assert "활성 리뷰 사이클 [T-0002]" in out and "강등" in out
    assert "--final" in out                       # 수렴 후 FULL 경로를 함께 안내
    assert "regression(scoped, 1 touches)" in out
    assert '-k "pay"' in fake.calls[0]["args"][0]
    assert not board.REGRESSION_FLAG.exists()     # FULL 게이트 플래그는 안 쓴다


def test_final_flag_keeps_the_full_gate(board, monkeypatch, capsys):
    """`--final` 은 활성 사이클 중에도 FULL 을 그대로 돈다 (수렴 후·pre-push 경로·DoD)."""
    _write_rounds_ledger(board, {"T-0002": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0002": "claimed"})
    monkeypatch.setattr(board, "_ticket_touches", lambda tid: ["src/pay.py"])
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args(final=True)) == 0
    out = capsys.readouterr().out
    assert "강등" not in out
    assert "-k" not in fake.calls[0]["args"][0]
    assert _flag(board)["scope"] == "full"        # push 게이트 플래그 기록


def test_closed_cycle_runs_full_without_the_flag(board, monkeypatch, capsys):
    """마지막 라운드가 통과면 사이클은 닫힌 것 — `--final` 없이도 FULL 이다."""
    _write_rounds_ledger(board, {"T-0001": [(1, 1), (2, 0)]})
    _ticket_statuses(board, monkeypatch, {"T-0001": "claimed"})
    monkeypatch.setattr(board, "_ticket_touches", lambda tid: ["src/pay.py"])
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert "강등" not in capsys.readouterr().out
    assert _flag(board)["scope"] == "full"


def test_downgrade_needs_resolvable_touches(board, monkeypatch, capsys):
    """touches 를 모르면 강등하지 않는다 — 스코프 없는 좁힘은 '가짜 green' 이다.

    게이트 이름은 자유 문자열이 실사용이라(`wave4-b1`) 티켓이 아닌 게이트가 정상적으로 있다."""
    _write_rounds_ledger(board, {"wave4-b1": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"wave4-b1": "claimed"})
    monkeypatch.setattr(board, "_ticket_touches", lambda tid: [])
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert "강등" not in capsys.readouterr().out
    assert _flag(board)["scope"] == "full"


def test_explicit_scope_is_untouched_by_the_staging_rule(board, monkeypatch, capsys):
    """명시 스코프(`--ticket`/`--touches`)는 종전 그대로다 (강등 판정 자체를 타지 않는다)."""
    _write_rounds_ledger(board, {"T-0002": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0002": "claimed"})
    monkeypatch.setattr(board, "_ticket_touches", lambda tid: ["src/other.py"])
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="2 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args(touches="src/pay.py")) == 0
    out = capsys.readouterr().out
    assert "강등" not in out and "regression(scoped, 1 touches)" in out


def test_pre_push_hook_runs_the_final_full_gate(board, monkeypatch, tmp_path):
    """훅은 FULL 을 요구한다 — 강등되면 게이트 플래그가 안 써져 push 가 통과할 수 없다."""
    hooks = tmp_path / "hooks"
    monkeypatch.setattr(board, "_hooks_dir", lambda: hooks)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    assert board.install_pre_push_hook() is True
    text = (hooks / "pre-push").read_text(encoding="utf-8")
    assert "regression run --final" in text
    assert text == board.pre_push_hook_body()      # 설치 본문 = drift 대조 본문(단일 진실)


# ── 설치된 훅 drift 자기치유 ────────────────────────────────────────────────
# 훅 본문은 설치 시점에 박제되므로, 엔진이 게이트 명령을 바꿔도 이미 설치된 훅은 옛 명령을 돈다.
# 실측 클래스: `--final` 없는 구버전 훅이 강등 실행의 rc0 을 push 허가로 읽어 FULL 플래그 없이
# push 가 열린다. 릴리즈 체크리스트가 아니라 게이트 자신이 진입할 때 고친다. 경계 셋을 함께 본다:
# 아는 세대만 교체(커스터마이즈 보호) · 원자 교체(실행 중인 훅 자기파손 방지) · 순수 경로 해소
# (회귀 진입에 subprocess 추가 0).


def _legacy_hook(py: str = "python3") -> str:
    return board_legacy_body(py)


def board_legacy_body(py: str) -> str:
    """구세대 본문은 엔진 registry 가 소유한다 — 테스트가 사본을 두면 registry 가 죽어도 green."""
    mod = _load_board()
    return mod._legacy_pre_push_hook_bodies(py)[0]


def _hooked_repo(board, monkeypatch, tmp_path, body: str | None):
    """`.git` 디렉토리를 갖춘 tmp REPO + (선택) 설치된 훅 — drift 치유 진입 조건 재현.

    훅 경로는 실제 해소 규칙(`REPO/.git/hooks`)을 그대로 태운다 — `_hooks_dir` 를 스텁하면
    "git 호출 없이 해소한다"는 이번 수정의 핵심이 검증되지 않는다."""
    hooks = board.REPO / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    hook = hooks / "pre-push"
    if body is not None:
        hook.write_text(body, encoding="utf-8")
        hook.chmod(0o755)
    return hook


def test_regression_run_heals_a_legacy_hook(board, monkeypatch, tmp_path, capsys):
    """구버전 훅(`--final` 부재)은 회귀 진입에서 현행 본문으로 교체된다 (자기치유·DoD)."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    healed = hook.read_text(encoding="utf-8")
    assert healed == board.pre_push_hook_body("python3")
    assert "regression run --final" in healed
    assert f"pm-hook-rev: {board.PM_HOOK_REV}" in healed
    assert os.access(hook, os.X_OK)                # 실행 권한 보존
    assert "구버전 pre-push 훅" in capsys.readouterr().out


def test_regression_check_heals_a_legacy_hook_too(board, monkeypatch, tmp_path):
    """`check` 진입도 같은 치유를 탄다 — 훅이 부르는 첫 명령이 check 다."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    board.cmd_regression(argparse.Namespace(action="check"))
    assert hook.read_text(encoding="utf-8") == board.pre_push_hook_body("python3")


def test_healing_replaces_the_hook_atomically(board, monkeypatch, tmp_path):
    """교체는 **새 inode** 다 — 실행 중인 훅이 자기 자신을 truncate-rewrite 하면 shell 이
    바뀐 오프셋을 이어 읽어 구문이 깨진다(치유가 게이트를 부수는 방향)."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    before_inode = hook.stat().st_ino
    with hook.open("r", encoding="utf-8") as running:      # 실행 중 shell 의 열린 fd 흉내
        _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
        assert board.cmd_regression(_run_args()) == 0
        assert running.read() == _legacy_hook()            # 옛 fd 는 옛 본문을 온전히 읽는다
    assert hook.stat().st_ino != before_inode
    assert not list(hook.parent.glob("pre-push.*.tmp"))    # tmp 잔재 없음


def test_healing_preserves_the_installed_interpreter(board, monkeypatch, tmp_path):
    """채택자의 런처 선택(`py -3.12`)을 치유가 바꾸지 않는다 — 설치된 인터프리터를 보존."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook("py -3.12"))
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert hook.read_text(encoding="utf-8") == board.pre_push_hook_body("py -3.12")


def test_healing_is_idempotent_and_quiet_for_a_current_hook(
        board, monkeypatch, tmp_path, capsys):
    """현행 세대 스탬프면 다시 쓰지 않고 안내도 없다 (멱등·소음 0)."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, None)
    hook.write_text(board.pre_push_hook_body("python3"), encoding="utf-8")
    inode = hook.stat().st_ino
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert hook.stat().st_ino == inode
    out, err = capsys.readouterr()
    assert "구버전" not in out and "알려진 세대" not in err


def test_healing_leaves_a_customized_pm_hook_untouched(
        board, monkeypatch, tmp_path, capsys):
    """서명은 있지만 **알려진 세대가 아닌** 본문(채택자 커스터마이즈)은 건드리지 않고 경고만.

    본문 차이만 보고 덮으면 채택자가 손으로 더한 단계가 조용히 사라진다."""
    custom = _legacy_hook() + "python3 scripts/company_policy_check.py || exit 1\n"
    hook = _hooked_repo(board, monkeypatch, tmp_path, custom)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert hook.read_text(encoding="utf-8") == custom
    err = capsys.readouterr().err
    assert "알려진 세대와 다릅니다" in err and "board.py init" in err


def test_healing_never_installs_a_missing_hook(board, monkeypatch, tmp_path):
    """훅 미설치 형상(솔로 legacy·의도적 미설치)은 무영향 — 새로 심지 않는다."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, None)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert not hook.exists()


def test_healing_leaves_a_foreign_hook_alone(board, monkeypatch, tmp_path, capsys):
    """남의 pre-push 훅(pm 서명 없음)은 덮지도, 경고하지도 않는다 (대상 자체가 아니다)."""
    foreign = "#!/bin/sh\n# someone else's gate\nexit 0\n"
    hook = _hooked_repo(board, monkeypatch, tmp_path, foreign)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    assert hook.read_text(encoding="utf-8") == foreign
    assert "알려진 세대" not in capsys.readouterr().err


def test_healing_resolves_hooks_without_calling_git(board, monkeypatch, tmp_path):
    """경로 해소는 순수 파이썬이다 — 회귀 진입 경로에 subprocess 를 추가하지 않는다.

    `_hooks_dir`(git `rev-parse`)를 호출하면 실패하도록 두고, 그래도 치유가 동작함을 단언한다."""
    monkeypatch.setattr(board, "_hooks_dir",
                        lambda: (_ for _ in ()).throw(AssertionError("git 조회 금지")))
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    assert board._heal_pre_push_hook_drift() is True
    assert hook.read_text(encoding="utf-8") == board.pre_push_hook_body("python3")


def test_healing_skips_non_git_trees(board, monkeypatch):
    """`.git` 없는 트리는 해소 자체가 None — 조용히 skip."""
    monkeypatch.setattr(board, "_hooks_dir",
                        lambda: (_ for _ in ()).throw(AssertionError("git 조회 금지")))
    assert board._pure_hooks_dir() is None
    assert board._heal_pre_push_hook_drift() is False


def test_pure_hooks_dir_follows_a_linked_worktree_to_the_shared_hooks(board, tmp_path):
    """linked worktree(`.git` 파일 + `commondir`)는 **공용** 훅 디렉토리로 해소된다."""
    main_git = tmp_path / "main" / ".git"
    (main_git / "hooks").mkdir(parents=True)
    wt_git = main_git / "worktrees" / "slot1"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    (board.REPO / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert board._pure_hooks_dir() == (main_git / "hooks").resolve()


def test_pure_hooks_dir_skips_custom_hooks_path(board):
    """`core.hooksPath` 커스텀(보호훅 형상)은 git 없이 확정 불가 — 치유를 건너뛴다(fail-open)."""
    git_dir = board.REPO / ".git"
    (git_dir / "hooks").mkdir(parents=True)
    (git_dir / "config").write_text(
        "[core]\n\thooksPath = .githooks\n", encoding="utf-8")
    assert board._pure_hooks_dir() is None
