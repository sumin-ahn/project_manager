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
    """보드 티켓 status 대역 — `{ticket_id: status}` 이외의 id 는 부재(None).

    강등 판정이 쓰는 조회 seam 은 정확-일치(`find_ticket_exact`)다 — 대역도 같은 자리에 건다."""
    def _find_exact(tid, **_kwargs):
        if tid not in statuses:
            return None
        return statuses[tid], Path(f"/board/{statuses[tid]}/{tid}.md")

    monkeypatch.setattr(board, "find_ticket_exact", _find_exact)


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


def test_corrupt_ticket_frontmatter_cancels_the_downgrade(board, monkeypatch, capsys):
    """손상 frontmatter 는 강등을 취소하고 FULL 로 간다 — 크래시도, 부분 강등도 아니다."""
    _write_rounds_ledger(board, {"T-0002": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0002": "claimed"})

    def _boom(tid):
        raise ValueError("frontmatter 손상")

    monkeypatch.setattr(board, "_ticket_touches", _boom)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))
    assert board.cmd_regression(_run_args()) == 0
    out, err = capsys.readouterr()
    assert "강등을 취소" in err and "T-0002" in err
    assert "강등합니다" not in out
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


# ── 게이트 조회는 canonical ID 정확 일치 (T-0602 ③) ────────────────────────
# codex R2 지적: 자유 문자열 게이트를 glob 기반 `find_ticket` 에 그대로 넘긴다 — `T-0036` 이
# `T-0036-001-*.md` 에 오인 매칭되면 **무관한 티켓의 touches** 로 FULL 회귀가 강등된다(그 실행은
# 아무것도 검증하지 않은 '가짜 green'). 아래는 그 형상을 실 파일로 재현하고 차단을 단언한다.


def _seed_ticket_file(board, tid: str, *, status: str, slug: str,
                      touches: str = "src/pay.py") -> Path:
    """tmp 보드에 실 티켓 파일을 심는다 (조회는 실 `find_ticket` glob 을 탄다)."""
    path = board.tickets_dir() / status / f"{tid}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    declared = f"touches:\n- {touches}" if touches else "touches: []"
    path.write_text(
        f"---\nid: {tid}\ntitle: 픽스처\nstatus: {status}\n{declared}\n---\n\n# 본문\n",
        encoding="utf-8")
    return path


def test_prefixed_ticket_does_not_answer_for_a_legacy_gate_id(board):
    """재현: 게이트 `T-0036` · 보드엔 `T-0036-001-*` 만 — glob 은 잡지만 canonical ID 가 다르다."""
    _seed_ticket_file(board, "T-0036-001", status="claimed", slug="다른-티켓",
                      touches="src/unrelated.py")
    # glob 은 실제로 오인 매칭한다(차단이 어디서 서는지 못박는다).
    assert board.find_ticket("T-0036")[1].name.startswith("T-0036-001")

    assert board._gate_ticket("T-0036") is None
    assert board._gate_ticket_is_claimed("T-0036") is False
    assert board._ticket_touches("T-0036") == []     # 무관 touches 로 좁히지 않는다


def test_glob_mismatched_gate_never_downgrades_a_full_run(board, monkeypatch, capsys):
    """그 형상에서 FULL 요청은 강등되지 않는다 — 무관 touches 로 좁힌 '가짜 green' 폐쇄 (DoD)."""
    _seed_ticket_file(board, "T-0036-001", status="claimed", slug="다른-티켓",
                      touches="src/unrelated.py")
    _write_rounds_ledger(board, {"T-0036": [(1, 1)]})
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0
    assert board._active_review_gates() == []
    assert "강등" not in capsys.readouterr().out
    assert _flag(board)["scope"] == "full"


def test_exact_ticket_gate_still_downgrades(board, monkeypatch, capsys):
    """정상 경로 무변경 — canonical ID 가 정확히 일치하면 종전대로 그 touches 로 강등한다."""
    _seed_ticket_file(board, "T-0036", status="claimed", slug="본래-티켓")
    _seed_ticket_file(board, "T-0036-001", status="claimed", slug="다른-티켓",
                      touches="src/unrelated.py")
    _write_rounds_ledger(board, {"T-0036": [(1, 1)]})
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="2 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0
    out = capsys.readouterr().out
    assert "활성 리뷰 사이클 [T-0036]" in out and "강등" in out
    assert '-k "pay"' in fake.calls[0]["args"][0]     # 본래 티켓의 touches 로 좁힌다


def test_free_form_gate_label_skips_the_ticket_lookup(board, monkeypatch):
    """티켓 ID 문법이 아닌 라벨(`wave4-b1`·glob 메타)은 조회 자체를 생략한다."""
    calls: list[str] = []

    def _find(tid, **_kwargs):
        calls.append(tid)
        return None

    monkeypatch.setattr(board, "find_ticket_exact", _find)
    for label in ("wave4-b1", "T-*", "T-0036-001-fix", "리뷰", ""):
        assert board._gate_ticket(label) is None
        assert board._gate_ticket_is_claimed(label) is False
    assert calls == [], f"비-ID 라벨이 보드 조회를 탔다: {calls}"

    # canonical ID 는 종전대로 조회한다 (생략 규칙이 정상 게이트를 삼키지 않는다).
    assert board._gate_ticket("T-0036-001") is None
    assert calls == ["T-0036-001"]


def test_ticket_id_gate_with_empty_touches_stays_full(board, monkeypatch, capsys):
    """canonical ID 게이트인데 touches 가 비면 강등하지 않는다 — 스코프 없는 좁힘 금지(불변)."""
    _seed_ticket_file(board, "T-0037", status="claimed", slug="빈-touches", touches="")
    _write_rounds_ledger(board, {"T-0037": [(1, 1)]})
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0
    assert "강등" not in capsys.readouterr().out
    assert _flag(board)["scope"] == "full"


# ── 강등 확정은 전-게이트 fail-closed (T-0603 ①) ────────────────────────────
# codex R3 지적: 활성 게이트가 여럿일 때 하나만 touches 를 확정하지 못해도 나머지 touches 로
# 강등이 성사됐다 — 그 실행은 미해소 게이트가 건드린 범위를 **한 번도 돌지 않은 채** rc0 을 내고,
# 그 green 은 활성 게이트 전부에 대한 통과로 읽힌다(false-green). 아래는 두 미해소 형상(빈
# touches·읽기 실패)을 재현하고 **강등 전체 취소**를 단언한다.


def _two_active_gates(board, monkeypatch, resolver) -> None:
    """활성 게이트 둘(T-0002·T-0003)을 심고 touches 해소기를 주입한다."""
    _write_rounds_ledger(board, {"T-0002": [(1, 1)], "T-0003": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0002": "claimed", "T-0003": "claimed"})
    monkeypatch.setattr(board, "_ticket_touches", resolver)


def test_one_gate_without_touches_cancels_the_whole_downgrade(
        board, monkeypatch, capsys):
    """게이트 하나가 빈 touches 면 다른 게이트의 touches 로 부분 강등하지 않는다 (DoD)."""
    _two_active_gates(board, monkeypatch,
                      lambda tid: ["src/pay.py"] if tid == "T-0002" else [])
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0
    out, err = capsys.readouterr()
    assert "강등합니다" not in out                       # 부분 강등 안내가 뜨지 않는다
    assert "T-0003" in err and "강등을 취소" in err       # 미해소 게이트를 지목한 사유 1줄
    assert "-k" not in fake.calls[0]["args"][0]         # FULL 그대로 돈다
    assert _flag(board)["scope"] == "full"              # push 게이트 플래그 = FULL


def test_one_unreadable_gate_cancels_the_whole_downgrade(board, monkeypatch, capsys):
    """touches 읽기 실패(손상 frontmatter)도 같은 취급 — 확정 못 한 게이트가 있으면 FULL."""
    def _resolve(tid):
        if tid == "T-0003":
            raise ValueError("frontmatter 손상")
        return ["src/pay.py"]

    _two_active_gates(board, monkeypatch, _resolve)
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0
    out, err = capsys.readouterr()
    assert "강등합니다" not in out
    assert "T-0003" in err and "ValueError" in err      # 사유에 실패 형상을 남긴다
    assert "-k" not in fake.calls[0]["args"][0]
    assert _flag(board)["scope"] == "full"


def test_all_gates_resolved_still_downgrade_to_the_union(board, monkeypatch, capsys):
    """정상 경로 무변경 — 활성 게이트 전부가 touches 를 확정하면 종전대로 합집합 강등이다."""
    _two_active_gates(
        board, monkeypatch,
        lambda tid: ["src/pay.py"] if tid == "T-0002" else ["src/ledger.py"])
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="2 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0
    out = capsys.readouterr().out
    assert "활성 리뷰 사이클 [T-0002, T-0003]" in out and "강등합니다" in out
    scoped = fake.calls[0]["args"][0]
    assert '-k "ledger or pay"' in scoped               # 두 게이트의 합집합
    assert not board.REGRESSION_FLAG.exists()           # 강등 실행은 게이트 플래그를 안 쓴다


# ── 티켓 조회 공용 seam (T-0603 ④) ──────────────────────────────────────────
# 정확-일치 판정을 board 공개 함수로 승격했다 — 강등(board)·estimate(추가 리뷰어)·완료 게이트
# (ticket_finish)가 **같은 함수**를 쓴다. 사본 판정을 두면 그 사본이 다시 첫-매칭으로 흘러
# half-fix 가 재발한다(이 클래스의 재발 이력).


def test_find_ticket_exact_answers_only_for_the_canonical_id(board):
    """공존 픽스처에서 정확-일치 seam 은 각 ID 에 그 ID 의 파일만 돌려준다."""
    _seed_ticket_file(board, "T-0036", status="claimed", slug="본래-티켓")
    _seed_ticket_file(board, "T-0036-001", status="open", slug="다른-티켓",
                      touches="src/unrelated.py")

    legacy = board.find_ticket_exact("T-0036")
    prefixed = board.find_ticket_exact("T-0036-001")
    assert legacy is not None and legacy[0] == "claimed"
    assert legacy[1].name.startswith("T-0036-본래")
    assert prefixed is not None and prefixed[0] == "open"
    assert board.find_ticket_exact("T-0036-002") is None      # 없는 ID 는 None


def test_find_ticket_prefers_the_exact_match_over_the_first_glob_hit(board):
    """`find_ticket` 도 정확 일치를 먼저 본다 — 이 조회를 쓰는 모든 소비처가 상속한다."""
    _seed_ticket_file(board, "T-0036", status="done", slug="본래-티켓")
    _seed_ticket_file(board, "T-0036-001", status="open", slug="다른-티켓")

    status, path = board.find_ticket("T-0036")
    assert (status, path.name.startswith("T-0036-본래")) == ("done", True)


def test_find_ticket_still_falls_back_when_no_exact_candidate_exists(board):
    """정확 일치 후보가 없으면 종전 glob 첫 매칭 그대로 — legacy 티켓 조회가 회귀하지 않는다."""
    path = board.tickets_dir() / "open" / "T-0038-fix-123.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # frontmatter `id:` 부재 + 숫자로 끝나는 slug — 파일명 파서가 `T-0038-fix-123` 으로 읽어
    # canonical 이 `T-0038` 과 어긋나는 legacy 형상(`_canonical_ticket_id` 주석의 그 모호성).
    path.write_text("---\ntitle: 픽스처\nstatus: open\n---\n\n# 본문\n", encoding="utf-8")

    assert board.find_ticket_exact("T-0038") is None
    assert board.find_ticket("T-0038")[1] == path


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


# ── 구형 서명 훅 = 차단 (fail-closed) ───────────────────────────────────────
# 훅 본문은 설치 시점에 박제되므로, 엔진이 게이트 명령을 바꿔도 이미 설치된 훅은 옛 명령을 돈다.
# 실측 클래스: `--final` 없는 구버전 훅이 강등 실행의 rc0 을 push 허가로 읽어 FULL 플래그 없이
# push 가 열린다. 게이트는 그 훅을 고치지 않고 **막는다** — 처방은 `board.py init` 재실행 1회다.
#
# 이 군은 옛 자기치유 테스트 군을 **차단 의미로 대체**한 것이다. 케이스 수가 준 만큼 커버리지가
# 준 게 아니라 검증할 대상이 없어졌다: 표식 TTL·일회성 소비·표식 탈취·치유 실패 fail-soft 는
# 전부 "치유 사실을 다음 프로세스에 전달하는" 상태 기계의 케이스였고, 그 기계가 사라졌다. 남은
# 축은 판정 하나(현행 세대인가)와 그 결론(차단 / 무간섭)뿐이라 여기 케이스도 그 둘만 센다.


def _legacy_hook(py: str = "python3") -> str:
    return board_legacy_body(py)


def board_legacy_body(py: str) -> str:
    """구세대 본문은 엔진 registry 가 소유한다 — 테스트가 사본을 두면 registry 가 죽어도 green."""
    mod = _load_board()
    return mod._legacy_pre_push_hook_bodies(py)[0]


def _hooked_repo(board, monkeypatch, tmp_path, body: str | None):
    """`.git` 디렉토리를 갖춘 tmp REPO + (선택) 설치된 훅 — 세대 판정 진입 조건 재현.

    훅 경로는 실제 해소 규칙(`REPO/.git/hooks`)을 그대로 태운다 — `_hooks_dir` 를 스텁하면
    "git 호출 없이 해소한다"는 이 판정의 성질이 검증되지 않는다."""
    hooks = board.REPO / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    hook = hooks / "pre-push"
    if body is not None:
        hook.write_text(body, encoding="utf-8")
        hook.chmod(0o755)
    return hook


def test_regression_run_refuses_a_legacy_hook(board, monkeypatch, tmp_path, capsys):
    """구버전 훅(`--final` 부재)이면 회귀를 돌리지 않고 rc 1 로 막고 `init` 을 안내한다.

    차단은 **어떤 부작용보다 앞**이다 — pytest 를 돌리지도, 게이트 플래그를 쓰지도 않는다
    (기록이 남으면 그게 다음 실행의 green 재사용 입력이 된다)."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 1

    assert hook.read_text(encoding="utf-8") == _legacy_hook()   # 고치지 않는다
    assert fake.calls == []                                     # 회귀 미실행
    assert not board.REGRESSION_FLAG.exists()                   # 기록 없음
    err = capsys.readouterr().err
    assert "구버전" in err and "board.py init" in err and "py -3" in err


def test_regression_check_refuses_a_legacy_hook_too(board, monkeypatch, tmp_path, capsys):
    """`check` 진입도 같은 판정을 탄다 — 훅이 부르는 첫 명령이 check 다."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    _write_flag(board, status="pass", rc=0)                     # green 기록이 있어도 막힌다

    assert board.cmd_regression(argparse.Namespace(action="check")) == 1

    assert hook.read_text(encoding="utf-8") == _legacy_hook()
    assert "board.py init" in capsys.readouterr().err


def test_the_check_then_run_sequence_stays_blocked(board, monkeypatch, tmp_path, capsys):
    """훅 본문은 `check || run` **2프로세스**다 — 두 단계 모두 차단으로 끝난다.

    구형 훅이 실행 중인 push 시도에서 2단계 run 이 통과하면(강등이든 FULL 이든) 옛 훅이 그 rc0 을
    push 허가로 읽는다. 차단은 두 단계에 같은 결론을 주므로 그 창 자체가 없다."""
    _write_rounds_ledger(board, {"T-0002": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0002": "claimed"})
    monkeypatch.setattr(board, "_ticket_touches", lambda tid: ["src/pay.py"])
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(argparse.Namespace(action="check")) == 1   # 훅 1단계
    assert board.cmd_regression(_run_args()) == 1                          # 훅 2단계

    assert hook.read_text(encoding="utf-8") == _legacy_hook()
    assert fake.calls == []
    assert not board.REGRESSION_FLAG.exists()
    err = capsys.readouterr().err
    assert err.count("board.py init") == 2      # 두 단계 모두 같은 처방을 말한다


def test_an_unknown_pm_hook_body_is_refused_too(board, monkeypatch, tmp_path, capsys):
    """서명은 있는데 **엔진이 모르는 본문**(채택자 커스터마이즈)도 차단이다.

    아는 세대만 골라 다루던 옛 경계는 *덮어쓰기*가 있을 때의 것이었다. 덮지 않는 지금은
    "이 훅이 무엇을 강제하는지 확정할 수 없다"가 곧 차단 사유고, 조용한 통과 경로를 남기지 않는다.
    """
    custom = _legacy_hook() + "python3 scripts/company_policy_check.py || exit 1\n"
    hook = _hooked_repo(board, monkeypatch, tmp_path, custom)
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 1

    assert hook.read_text(encoding="utf-8") == custom           # 미접촉
    assert fake.calls == []
    err = capsys.readouterr().err
    assert "엔진이 모르는 본문" in err and "board.py init" in err


def test_a_current_generation_hook_passes_untouched(board, monkeypatch, tmp_path, capsys):
    """현행 세대(`board.py init` 이 방금 설치한 본문)는 무소음 통과다 — 설치↔판정이 닫힌다."""
    hooks = board.REPO / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    monkeypatch.setattr(board, "_hooks_dir", lambda: hooks)
    assert board.install_pre_push_hook() is True
    hook = hooks / "pre-push"
    inode = hook.stat().st_ino
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0

    assert hook.stat().st_ino == inode                          # 다시 쓰지 않는다
    assert _flag(board)["scope"] == "full"
    out, err = capsys.readouterr()
    assert "구버전" not in err and "구버전" not in out


def test_a_current_hook_with_the_adopters_launcher_passes(board, monkeypatch, tmp_path):
    """채택자의 런처 선택(`py -3.12`)이 세대 판정을 뒤집지 않는다 — 설치된 인터프리터로 대조."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, None)
    hook.write_text(board.pre_push_hook_body("py -3.12"), encoding="utf-8")
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0
    assert board._stale_pre_push_hook_refusal() is None


def test_a_foreign_hook_is_not_our_business(board, monkeypatch, tmp_path, capsys):
    """남의 pre-push 훅(pm 서명 없음)은 판정 대상이 아니다 — 차단도 경고도 없다."""
    foreign = "#!/bin/sh\n# someone else's gate\nexit 0\n"
    hook = _hooked_repo(board, monkeypatch, tmp_path, foreign)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0

    assert hook.read_text(encoding="utf-8") == foreign
    assert capsys.readouterr().err == ""


def test_a_missing_hook_is_not_our_business(board, monkeypatch, tmp_path):
    """훅 미설치 형상(솔로 legacy·의도적 미설치)은 무영향 — 심지도, 막지도 않는다."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, None)
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0
    assert not hook.exists()


def test_an_unreadable_hook_is_refused(board, monkeypatch, tmp_path, capsys):
    """훅 파일은 있는데 읽지 못하면 **차단**이다 — 판정 실패도 차단 방향(조용한 통과 0).

    세대를 확정하지 못한 훅을 통과시키는 것이 곧 옛 게이트가 조용히 사는 채널이다."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    real_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self == hook:
            raise OSError("읽기 실패")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    fake = _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="9 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 1

    assert fake.calls == []
    err = capsys.readouterr().err
    assert "세대를 판정할 수 없습니다" in err and "board.py init" in err


def test_the_refusal_resolves_hooks_without_calling_git(board, monkeypatch, tmp_path):
    """경로 해소는 순수 파이썬이다 — 회귀 진입 경로에 subprocess 를 추가하지 않는다.

    `_hooks_dir`(git `rev-parse`)를 호출하면 실패하도록 두고, 그래도 판정이 성립함을 단언한다."""
    monkeypatch.setattr(board, "_hooks_dir",
                        lambda: (_ for _ in ()).throw(AssertionError("git 조회 금지")))
    _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    assert board._stale_pre_push_hook_refusal() is not None


def test_non_git_trees_are_left_alone(board, monkeypatch):
    """`.git` 없는 트리는 해소 자체가 None — 판정 밖(비-훅 형상을 차단하지 않는다)."""
    monkeypatch.setattr(board, "_hooks_dir",
                        lambda: (_ for _ in ()).throw(AssertionError("git 조회 금지")))
    assert board._pure_hooks_dir() is None
    assert board._stale_pre_push_hook_refusal() is None


def test_failed_replace_leaves_no_tmp_residue(board, monkeypatch, tmp_path):
    """교체가 실패해도 `pre-push.<pid>.tmp` 를 남기지 않는다 (T-0600 — 실패는 그대로 올린다).

    잔재는 git 이 실행하지 않는 이름이라 무해했지만, 실패마다 훅 디렉토리에 쌓인다.
    """
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    monkeypatch.setattr(
        board.os, "replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("교체 실패")),
    )

    with pytest.raises(OSError):
        board._write_hook_atomic(hook, board.pre_push_hook_body("python3"))

    assert not list(hook.parent.glob("pre-push.*.tmp"))
    assert hook.read_text(encoding="utf-8") == _legacy_hook()   # 원본 미변경


def test_failed_chmod_leaves_no_tmp_residue(board, monkeypatch, tmp_path):
    """교체 **앞 단계**(쓰기·chmod) 실패도 tmp 를 남기지 않는다 (T-0603 suggestion).

    정리 범위가 `os.replace` 뿐이면 chmod 가 죽은 실행마다 `pre-push.<pid>.tmp` 가 쌓인다 —
    문서 서술("교체 전 tmp 에 권한을 준다")과 실제 정리 범위가 어긋난 자리다."""
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    real_chmod = Path.chmod

    def _boom(self, mode, **kwargs):
        if self.name.endswith(".tmp"):
            raise OSError("chmod 실패")
        return real_chmod(self, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", _boom)

    with pytest.raises(OSError):
        board._write_hook_atomic(hook, board.pre_push_hook_body("python3"))

    assert not list(hook.parent.glob("pre-push.*.tmp"))
    assert hook.read_text(encoding="utf-8") == _legacy_hook()   # 원본 미변경


def test_pure_hooks_dir_follows_a_linked_worktree_to_the_shared_hooks(board, tmp_path):
    """linked worktree(`.git` 파일 + `commondir`)는 **공용** 훅 디렉토리로 해소된다."""
    main_git = tmp_path / "main" / ".git"
    (main_git / "hooks").mkdir(parents=True)
    wt_git = main_git / "worktrees" / "slot1"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    (board.REPO / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert board._pure_hooks_dir() == (main_git / "hooks").resolve()


# ── `core.hooksPath` 값 파싱 ────────────────────────────────────────────────
# 존재-only 감지는 선언이 있기만 하면 판정을 포기했다 — 기본 위치를 가리키는 선언까지 세대 관리
# 밖에 남는다. 값을 순수 파이썬으로 읽어 그 경로를 훅 위치로 쓴다(subprocess 0).
# git 은 섹션·키를 대소문자 무관으로 다루고, 상대 경로는 worktree 루트 기준으로 해소한다.


def _write_git_config(board, text: str) -> None:
    git_dir = board.REPO / ".git"
    (git_dir / "hooks").mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text(text, encoding="utf-8")


def test_pure_hooks_dir_parses_a_relative_hooks_path(board):
    """상대 `core.hooksPath` 는 worktree 루트(REPO) 기준으로 해소한다 (프로세스 cwd 아님)."""
    _write_git_config(board, "[core]\n\thooksPath = .githooks\n")
    assert board._pure_hooks_dir() == (board.REPO / ".githooks").resolve()


def test_pure_hooks_dir_parses_an_absolute_hooks_path(board, tmp_path):
    """절대 `core.hooksPath`(보호훅 형상)는 그 경로 그대로다."""
    elsewhere = tmp_path / "repo-hooks" / "proj"
    elsewhere.mkdir(parents=True)
    _write_git_config(board, f"[core]\n\thooksPath = {elsewhere}\n")
    assert board._pure_hooks_dir() == elsewhere.resolve()


@pytest.mark.parametrize("text", [
    "[CORE]\n\thooksPath = .githooks\n",          # 섹션 대문자
    "[core]\n\tHOOKSPATH = .githooks\n",          # 키 대문자
    "[Core]\n\tHooksPath=.githooks\n",            # 혼합 + 공백 없음
    '[core]\n\thooksPath = ".githooks"\n',        # 인용값
    "[core]\n\thooksPath = .old\n[core]\n\thooksPath = .githooks\n",   # last-wins
    "# 주석\n[core]\n\t; 세미콜론 주석\n\thooksPath = .githooks\n",
    "[core]\n\thooksPath = .githooks  # 사내 표준\n",                  # 인용 밖 후행 주석
])
def test_hooks_path_key_is_case_insensitive_and_last_wins(board, text):
    """git config 키/섹션은 대소문자 무관이고 마지막 선언이 이긴다."""
    _write_git_config(board, text)
    assert board._pure_hooks_dir() == (board.REPO / ".githooks").resolve()


@pytest.mark.parametrize("text", [
    "[core]\n\tbare = false\n",                       # 선언 없음
    "[receive]\n\thooksPath = .githooks\n",           # 다른 섹션의 동명 키
    "[core]\n\thooksPathExtra = .githooks\n",         # 다른 키
])
def test_absent_hooks_path_keeps_the_default_location(board, text):
    """`core.hooksPath` 선언이 없으면 기본 `.git/hooks` — 오탐 0."""
    _write_git_config(board, text)
    assert board._pure_hooks_dir() == (board.REPO / ".git" / "hooks").resolve()


@pytest.mark.parametrize("text", [
    "[include]\n\tpath = ../shared.config\n",
    '[includeIf "gitdir:~/work/"]\n\tpath = ../work.config\n',
    "[core]\n\thooksPath =\n",                        # 빈 값
])
def test_unresolvable_config_leaves_the_gate_alone(board, text):
    """include/includeIf·빈 값은 git 없이 확정 불가 — 훅 위치를 모르면 판정 자체가 없다.

    여기서 막으면 **훅을 안 쓰는 형상까지** 회귀가 멈춘다(차단 사유는 '구형 훅이 실재한다'뿐)."""
    _write_git_config(board, text)
    assert board._pure_hooks_dir() is None
    assert board._stale_pre_push_hook_refusal() is None


def test_a_hooks_path_at_the_default_location_is_covered(board, monkeypatch, tmp_path):
    """기본 위치를 가리키는 선언도 판정이 커버한다 — 선언 형태가 세대 관리를 끊지 않는다."""
    _write_git_config(board, "[core]\n\thooksPath = .git/hooks\n")
    hook = _hooked_repo(board, monkeypatch, tmp_path, _legacy_hook())
    assert board._pure_hooks_dir() == (board.REPO / ".git" / "hooks").resolve()
    assert "구버전" in (board._stale_pre_push_hook_refusal() or "")
    assert hook.read_text(encoding="utf-8") == _legacy_hook()


def test_a_hook_under_a_custom_hooks_path_is_covered(board, monkeypatch, tmp_path):
    """커스텀 위치의 엔진 훅도 판정 대상이다 — 위치 선언이 세대 관리를 끊지 않는다."""
    custom = board.REPO / ".githooks"
    custom.mkdir(parents=True)
    _write_git_config(board, "[core]\n\thooksPath = .githooks\n")
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    hook = custom / "pre-push"
    hook.write_text(_legacy_hook(), encoding="utf-8")
    hook.chmod(0o755)

    assert "구버전" in (board._stale_pre_push_hook_refusal() or "")
    assert hook.read_text(encoding="utf-8") == _legacy_hook()


def test_current_hook_still_downgrades_during_an_active_cycle(
        board, monkeypatch, tmp_path, capsys):
    """현행 세대 훅에서는 활성 사이클 강등이 종전대로 돈다 — 차단은 구형 훅에만 걸린다.

    강등이 push 게이트를 열지 못하는 근거가 바로 이것이다: 강등 rc0 을 허가로 읽는 훅은 애초에
    진입에서 막히므로, 여기 남는 강등은 항상 `--final` 을 싣는 현행 훅과 짝을 이룬다."""
    _write_rounds_ledger(board, {"T-0002": [(1, 1)]})
    _ticket_statuses(board, monkeypatch, {"T-0002": "claimed"})
    monkeypatch.setattr(board, "_ticket_touches", lambda tid: ["src/pay.py"])
    hook = _hooked_repo(board, monkeypatch, tmp_path, None)
    hook.write_text(board.pre_push_hook_body("python3"), encoding="utf-8")
    _install_run(board, monkeypatch, _FakeRun(rc=0, stdout="2 passed in 0.10s\n"))

    assert board.cmd_regression(_run_args()) == 0

    assert "targeted 로 강등합니다" in capsys.readouterr().out
    assert not board.REGRESSION_FLAG.exists()
