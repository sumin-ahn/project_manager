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


# ── guarded 슬롯해소 — bare handoff 멀티-PM 모호 → fail-loud (T-0178·ADR-0035) ──────
# session-entry(bare handoff·worktree_slot 미지정)가 멀티-PM 모호 셋업이면 없는 legacy 로
# 조용히 폴백하지 않고 명시 에러로 중단한다. solo·단일 self-host 는 현행 폴백 유지(무변경).


def _write_areas(path: Path, repos: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["| repo | prefix | git | test_cmd | owner |", "|---|---|---|---|---|"]
    for r in repos:
        lines.append(f"| {r} | {r} | g | pytest | me |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_leases(path: Path, entries: list[tuple[str, int]]) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    leases = [{"slot": f"work/{r}_{n}", "repo": r, "session": f"{r}_{n}"} for r, n in entries]
    path.write_text(json.dumps({"leases": leases}), encoding="utf-8")


# ── _resolve_session_worktree_slot 단위 — (resolved_slot, error_msg) (hermetic seam) ──
# 가드 단계서 실행 슬롯을 *한 번* 해소해 thread 한다(codex round2 must-fix). 반환 tuple:
# (worktree_slot|None, error_msg|None). 명시 슬롯→그대로 · solo/미해소→(None,None) ·
# default-1/단독/idle→(work/<repo>_<N>, None) · 진짜 모호→(None, msg).

def test_resolve_session_wt_slot_explicit_passthrough(handoff, tmp_path):
    """worktree_slot 명시 → 그대로 반환(downstream explicit 우선)·에러 없음."""
    assert handoff._resolve_session_worktree_slot("work/project_manager_2") == ("work/project_manager_2", None)


def test_resolve_session_wt_slot_solo_is_none(handoff, tmp_path):
    """등록 repo 0개(solo·멀티-PM 미셋업) → (None, None) (현행 폴백 유지)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, [])
    _write_leases(leases, [("project_manager", 1)])
    assert handoff._resolve_session_worktree_slot(None, areas, leases) == (None, None)


def test_resolve_session_wt_slot_single_self_host_resolves(handoff, tmp_path):
    """repo 1개 + 슬롯 1개(단일 self-host) → (work/<repo>_1, None) (실행 슬롯 해소)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [("project_manager", 1)])
    assert handoff._resolve_session_worktree_slot(None, areas, leases) == ("work/project_manager_1", None)


def test_resolve_session_wt_slot_default_1_resolves_slot1(handoff, tmp_path):
    """repo 1개 + `{1,2}` → (work/<repo>_1, None) (default-1·실행 슬롯 thread)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [("project_manager", 1), ("project_manager", 2)])
    assert handoff._resolve_session_worktree_slot(None, areas, leases) == ("work/project_manager_1", None)


def test_resolve_session_wt_slot_two_repos_returns_message(handoff, tmp_path):
    """등록 repo ≥2 → (None, 모호 메시지) — fail-loud 트리거(--repo 안내)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["A", "B"])
    _write_leases(leases, [("A", 1)])
    slot, msg = handoff._resolve_session_worktree_slot(None, areas, leases)
    assert slot is None
    assert msg is not None and "repo 2개" in msg and "--repo" in msg


def test_resolve_session_wt_slot_slot1_absent_returns_message(handoff, tmp_path):
    """repo 1개 + `{2,3}`(1 부재·비단독) → (None, 모호 메시지·--slot 안내)."""
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [("project_manager", 2), ("project_manager", 3)])
    slot, msg = handoff._resolve_session_worktree_slot(None, areas, leases)
    assert slot is None
    assert msg is not None and "슬롯 2개" in msg and "--slot" in msg


# ── run() e2e guard — REPO monkeypatch 로 모호 형상 ──────────────

def _make_multipm_ambiguous_repo(tmp_path: Path) -> None:
    """REPO 에 멀티-PM 모호 형상(등록 repo 2개)을 깐다 — bare handoff fail-loud 전제."""
    _write_areas(tmp_path / ".project_manager" / "areas.md", ["A", "B"])
    _write_leases(
        tmp_path / ".project_manager" / ".local" / "worktree-leases.json",
        [("A", 1), ("B", 1)],
    )


def _bare_handoff(handoff, tmp_path: Path):
    """pm_state_file 미주입(=explicit False) PmHandoff — run() 진입부 guard 경로를 탄다."""
    log_file = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("# log\n", encoding="utf-8")
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text("# pm_playbook\n", encoding="utf-8")
    return handoff.PmHandoff(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("guard 전에 pytest 호출 안 됨")),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
    ), log_file


def test_run_aborts_loud_on_ambiguous_multipm(handoff, tmp_path, monkeypatch, capsys):
    """bare run() + 멀티-PM 모호(repo 2개) → rc 1·명시 에러·log 무접촉 (침묵 폴백 부재)."""
    monkeypatch.setattr(handoff, "REPO", tmp_path)
    _make_multipm_ambiguous_repo(tmp_path)
    inst, log_file = _bare_handoff(handoff, tmp_path)
    before_log = log_file.read_text(encoding="utf-8")

    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 1  # fail-loud 중단.
    err = capsys.readouterr().err
    assert "슬롯 해소 모호" in err and "repo 2개" in err
    # log/current.md 무접촉(skeleton append 전에 중단).
    assert log_file.read_text(encoding="utf-8") == before_log


def test_run_solo_not_aborted_no_multipm_setup(handoff, tmp_path, monkeypatch, capsys):
    """bare run() + solo(멀티-PM 미셋업·areas 부재) → guard 통과(fail-soft·현행 무변경).

    solo 는 모호 아님 → 중단 안 함. pm_state legacy 부재라 3·4 skip 후 rc 0 으로 끝난다
    (기존 fail-soft 동작·guard 가 솔로를 깨지 않음을 입증)."""
    monkeypatch.setattr(handoff, "REPO", tmp_path)
    # areas/leases 를 *깔지 않음* → 등록 repo 0개 = solo.
    inst, log_file = _bare_handoff(handoff, tmp_path)

    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=True)

    assert rc == 0  # solo 는 중단 없이 진행.
    err = capsys.readouterr().err
    assert "슬롯 해소 모호" not in err  # 모호 에러 안 남.
    # solo 폴백으로 log skeleton 은 정상 append(현행 동작 보존).
    assert "PM 5차" in log_file.read_text(encoding="utf-8")


# ── 해소 슬롯 threading 일관성 — pm_state·회귀cwd·handoff entry 한 슬롯 (codex round2) ──
# bare handoff 가 default-1/idle-필터 슬롯을 한 번 해소해 실행 슬롯에 박으면, downstream
# 전부(pm_state read 위치·회귀 cwd·entry worktree 줄)가 *같은* 슬롯을 일관되게 쓴다. 특히
# self-split(② 홈엔 tests/ 없음)에서 회귀 cwd 가 활성 worktree(slot1)로 가야 한다(REPO 폴백 X).

def _bare_handoff_capturing_cwd(handoff, tmp_path: Path):
    """pm_state 미주입 PmHandoff — run_pytest stub 이 _regression_cwd(self._worktree_slot)를 캡처."""
    log_file = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("# log\n", encoding="utf-8")
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text("# pm_playbook\n", encoding="utf-8")
    captured = {}

    def _capture_pytest():
        # 회귀 단계에서 실제 cwd 해소 경로(_regression_cwd)를 그대로 호출해 캡처(green 반환).
        captured["cwd"] = handoff._regression_cwd(inst._worktree_slot)
        return 0, "1 passed in 0.01s"

    inst = handoff.PmHandoff(
        run_pytest_fn=_capture_pytest,
        run_git_fn=lambda args: (0, ""),
        run_shipping_test_fn=lambda wt: (0, "(no shipping)"),
        log_file=log_file,
        pm_playbook_file=playbook,
    )
    return inst, log_file, captured


def test_run_default_1_threads_slot_to_pm_state_cwd_and_entry(handoff, tmp_path, monkeypatch):
    """`{1,2}` bare run(): pm_state·회귀cwd·handoff entry 가 *모두* work/<repo>_1 로 일관."""
    monkeypatch.setattr(handoff, "REPO", tmp_path)
    _write_areas(tmp_path / ".project_manager" / "areas.md", ["project_manager"])
    _write_leases(
        tmp_path / ".project_manager" / ".local" / "worktree-leases.json",
        [("project_manager", 1), ("project_manager", 2)])
    inst, log_file, captured = _bare_handoff_capturing_cwd(handoff, tmp_path)

    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)

    assert rc == 0
    # (1) 실행 슬롯이 work/project_manager_1 로 thread 됐다.
    assert inst._worktree_slot == "work/project_manager_1"
    # (2) 회귀 cwd 가 slot1 worktree(tests/ 있는 곳)로 — REPO 폴백 아님(self-split 핵심).
    assert captured["cwd"] == str(tmp_path / "work/project_manager_1")
    assert captured["cwd"] != str(tmp_path)
    # (3) pm_state read 위치가 slot1 per-slot 경로.
    assert inst._pm_state_file == (
        tmp_path / ".project_manager" / ".local" / "slots" / "project_manager_1" / "pm_state.md")
    # (4) handoff entry 의 worktree 줄에 같은 슬롯 기록.
    assert "slot=`work/project_manager_1`" in log_file.read_text(encoding="utf-8")


def test_run_idle_slot1_threads_to_leased_slot2(handoff, tmp_path, monkeypatch):
    """`{1:idle, 2:leased}` bare run(): 실행 슬롯·회귀cwd 가 활성 slot2 (idle 1 아님)."""
    monkeypatch.setattr(handoff, "REPO", tmp_path)
    _write_areas(tmp_path / ".project_manager" / "areas.md", ["project_manager"])
    import json
    (tmp_path / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".project_manager" / ".local" / "worktree-leases.json").write_text(
        json.dumps({"leases": [
            {"repo": "project_manager", "slot": "work/project_manager_1", "state": "idle"},
            {"repo": "project_manager", "slot": "work/project_manager_2", "state": "leased"},
        ]}), encoding="utf-8")
    inst, log_file, captured = _bare_handoff_capturing_cwd(handoff, tmp_path)

    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)

    assert rc == 0
    assert inst._worktree_slot == "work/project_manager_2"
    assert captured["cwd"] == str(tmp_path / "work/project_manager_2")
    assert "slot=`work/project_manager_2`" in log_file.read_text(encoding="utf-8")


def test_run_solo_no_slot_threaded_repo_cwd_unchanged(handoff, tmp_path, monkeypatch):
    """solo `{1:leased}` 미셋업(areas 부재) → 실행 슬롯 미세팅·회귀cwd REPO 폴백(현행 무변경).

    solo 는 _resolve_session_worktree_slot→(None,None) → worktree_slot 안 박힘 →
    _regression_cwd 가 REPO 폴백(self-host solo 회귀 위치 보존). entry 에 worktree 줄 없음."""
    monkeypatch.setattr(handoff, "REPO", tmp_path)
    # areas 미설치 → 등록 repo 0개 = solo.
    inst, log_file, captured = _bare_handoff_capturing_cwd(handoff, tmp_path)

    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)

    assert rc == 0
    assert inst._worktree_slot is None  # 미세팅(현행 유지).
    assert captured["cwd"] == str(tmp_path)  # REPO 폴백(슬롯 미해소).
    assert "slot=`" not in log_file.read_text(encoding="utf-8")  # worktree 줄 생략(lean).
