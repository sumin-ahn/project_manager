"""pm_handoff fresh-clone fail-soft 단위 테스트 (T-0023).

`pm_state.md` 가 없는 clone(`board.py init` 미실행)에서 핸드오프 3단계
(`self._pm_state_file.read_text()`)가 FileNotFoundError 로 크래시하지 않고
**경고 후 3·4단계 skip, 나머지 진행, rc 0** 으로 끝나는지 본다.

실 git/실 파일 비의존 — subprocess 함수는 DI 로 갈아끼우고 log/playbook 은 tmp,
pm_state_file 은 **존재하지 않는 경로**를 주입한다 (test_handoff_trigger 의
PmHandoff DI/주입 패턴 재사용).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def handoff():
    return _load("pm_handoff")


def _make_handoff_missing_state(handoff, tmp_path: Path):
    """pm_state.md 가 부재한 경로를 주입한 PmHandoff 인스턴스를 만든다.

    log/playbook 은 격리된 tmp, subprocess 는 결정론 DI. pm_state_file 은
    tmp_path 아래 **만들지 않은** 경로 → .exists() False 분기를 탄다.
    """
    log_file = tmp_path / "current.md"
    playbook_file = tmp_path / "pm_playbook.md"
    missing_state = tmp_path / "does_not_exist" / "pm_state.md"
    log_file.write_text("# log\n", encoding="utf-8")
    # 인계 프롬프트 템플릿 앵커가 없어도 step 5 는 경고만 내고 크래시하지 않는다.
    playbook_file.write_text("# pm_playbook (no anchor)\n", encoding="utf-8")

    inst = handoff.PmHandoff(
        # 회귀(step 1)는 skip_pytest=True 로 건너뛰므로 호출되면 폭발.
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("skip_pytest 인데 pytest 호출")),
        # git status(step 6)는 호출됨 — 결정론 stub.
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook_file,
        pm_state_file=missing_state,
    )
    return inst, log_file, missing_state


# ── pm_state.md 부재: 3·4단계 skip, 나머지 진행, rc 0 ─────────────────────────

def test_handoff_skips_when_pm_state_absent(handoff, tmp_path):
    """pm_state.md 부재 → 핸드오프가 크래시 없이 rc 0 으로 끝난다 (3·4단계 skip)."""
    inst, _, missing_state = _make_handoff_missing_state(handoff, tmp_path)
    assert not missing_state.exists()  # 전제: pm_state 부재.

    rc = inst.run(
        session_num=5,
        wave_summary="x",
        dry_run=False,
        skip_pytest=True,
    )

    assert rc == 0
    # fail-soft 라 pm_state 파일을 새로 만들지 않는다.
    assert not missing_state.exists()


def test_handoff_warns_on_stderr_when_pm_state_absent(handoff, tmp_path, capsys):
    """부재 시 명확한 경고를 stderr 로 낸다 (board.py init 미실행 clone 안내)."""
    inst, _, _ = _make_handoff_missing_state(handoff, tmp_path)

    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 0
    captured = capsys.readouterr()
    assert "pm_state.md 없음" in captured.err
    assert "board.py init" in captured.err
    assert "skip" in captured.err


def test_handoff_continues_remaining_steps_when_pm_state_absent(handoff, tmp_path, capsys):
    """3·4단계 skip 이후에도 후속 단계(log entry append·잔여 작업)는 진행한다."""
    inst, log_file, _ = _make_handoff_missing_state(handoff, tmp_path)

    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 0
    # step 2: log/current.md 에 handoff entry skeleton 이 실제 append 됐다.
    log_text = log_file.read_text(encoding="utf-8")
    assert "handoff" in log_text and "PM 5차" in log_text
    # step 7: 완료 메시지까지 도달했다 (3·4 skip 이 흐름을 끊지 않음).
    out = capsys.readouterr().out
    assert "PM 5차 핸드오프 자동화 완료" in out


def test_handoff_dry_run_skips_when_pm_state_absent(handoff, tmp_path):
    """dry-run 경로에서도 pm_state 부재가 크래시 없이 skip 되고 rc 0."""
    inst, log_file, _ = _make_handoff_missing_state(handoff, tmp_path)
    before_log = log_file.read_text(encoding="utf-8")

    rc = inst.run(session_num=5, wave_summary="x", dry_run=True, skip_pytest=True)

    assert rc == 0
    # dry-run 은 log 도 건드리지 않는다.
    assert log_file.read_text(encoding="utf-8") == before_log


# ── run_trigger(): pm_state.md 부재 → 빈 문자열 폴백·sliding window skip, rc 0 (b-2) ──

def test_run_trigger_fail_soft_when_pm_state_absent(handoff, tmp_path, capsys):
    """run_trigger() pm_state.md 부재 → 크래시 없이 rc 0, 경고 stderr, sliding window skip.

    빈 문자열 폴백 → infer_next_session_num("")="?" placeholder → 5단계
    isinstance(int) else 분기가 sliding window 편집을 자동 skip → pm_state 파일을
    새로 만들지 않는다. dry_run=True 로 실 log/pm_state 편집을 피한다.
    """
    inst, log_file, missing_state = _make_handoff_missing_state(handoff, tmp_path)
    assert not missing_state.exists()  # 전제: pm_state 부재.

    rc = inst.run_trigger(reason="ctx-threshold", ctx_pct=92, dry_run=True)

    assert rc == 0
    # placeholder 경로 → sliding window 편집 skip → pm_state 파일 생성 안 함.
    assert not missing_state.exists()
    captured = capsys.readouterr()
    # 부재 경고는 stderr.
    assert "pm_state.md 없음" in captured.err
    # placeholder 안내(sliding window skip)는 stdout 에 노출된다.
    assert "session-num 추론 불가" in captured.out
