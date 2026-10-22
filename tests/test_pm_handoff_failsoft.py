"""pm_handoff fresh-clone fail-soft 단위 테스트 (T-0023).

`pm_state.md` 가 없는 clone(`board.py init` 미실행)에서 핸드오프 3단계
(`self._pm_state_file.read_text()`)가 FileNotFoundError 로 크래시하지 않고
**경고 후 3·4단계 skip, 나머지 진행, rc 0** 으로 끝나는지 본다.

실 git/실 파일 비의존 — subprocess 함수는 DI 로 갈아끼우고 log/playbook 은 tmp,
pm_state_file 은 **존재하지 않는 경로**를 주입한다 (test_handoff_trigger 의
PmHandoff DI/주입 패턴 재사용).
"""
from __future__ import annotations

from _pool_seams import bind_pool

import importlib.util
from pathlib import Path

import pytest
from _slot_seed import SLOT, seed_areas, seed_slot
from pm_engine.app import slot_resolution
from pm_engine.infra import pm_paths

TASK = "alpha"


class _TaskPool:
    """task membership·보유 슬롯만 답하는 최소 seam — 장부 조회를 통과시킨다."""

    def find_task(self, name):
        # 기록이 열려 있다 = bootstrap 을 거친 새 세션 — 차수는 직전 log 차수 다음이다.
        return type("_T", (), {"name": name, "open": True})()

    def find_task_strict(self, name):
        """실 엔진과 같은 짝 — 결과는 같고 장부 손상만 올린다(이 대역은 손상 없음)."""
        return self.find_task(name)

    def slots_for_task(self, name):
        return []

    def slots_for_task_strict(self, name):
        """실 엔진과 같은 짝 — 결과는 같고 장부 손상만 올린다(이 대역은 손상 없음)."""
        return self.slots_for_task(name)

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"



def _run_handoff(inst, **kw):
    """핸드오프 실행 — 승인 게이트에 정식 승인값을 실어 통과시킨다.

    이 모듈의 축은 승인 게이트가 아니다(그 축은 ``tests/test_pm_handoff_user_ack.py``가
    소유한다). 승인 대상값은 정체성 그 자체, 곧 task 이름이다.
    """
    task = kw.pop("task", TASK)
    kw.setdefault("user_ack", task)
    return inst.run(task, **kw)

def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def handoff():
    return _load("pm_handoff")


@pytest.fixture(autouse=True)
def _registered_home(handoff, tmp_path, monkeypatch):
    """모든 테스트의 기본 형상 = **등록된 홈**(N=1 슬롯 행) — 정체성이 장부에서 온다.

    다른 형상을 보는 테스트는 같은 파일들을 덮어써서 자기 형상을 만든다(모호·미등록).
    """
    monkeypatch.setattr(handoff, "REPO", tmp_path)
    seed_areas(tmp_path)
    seed_slot(tmp_path, session=TASK, tasks=[{"name": TASK, "prefix": None}])


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


# 가드 단계서 실행 슬롯을 *한 번* 해소해 thread 한다(codex round2 must-fix). 반환 tuple:
# (worktree_slot|None, error_msg|None). 명시 슬롯→그대로 · solo/미해소→(None,None) ·
# default-1/단독/idle→(work/<repo>_<N>, None) · 진짜 모호→(None, msg).













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
        captured["cwd"] = slot_resolution._regression_cwd(pm_paths.home_at(handoff.REPO), inst._worktree_slot)
        return 0, "1 passed in 0.01s"

    inst = handoff.PmHandoff(
        run_pytest_fn=_capture_pytest,
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
    )
    return inst, log_file, captured








# ── 앵커 부재 fail-soft — step3 만 스킵, 핸드오프 완주 (T-0243·finance_dev D3) ──────
# pm_state.md 는 *존재*하나 세션 식별 절 앵커가 없으면 update_session_window→ValueError.
# 종전엔 `return 1` 로 핸드오프 전체가 죽어 채택자가 9세션 연속 수동 우회했다. fail-soft 로
# step3(sliding window)만 스킵하고 나머지(4~7)는 계속 진행한다. 추측 편집 금지 유지 —
# window 를 지어내지 않되(pm_state 무변경) 죽지도 않는다.

_PM_STATE_NO_ANCHOR = (
    "# PM State\n\n"
    "## 현재 세션 요약\n\n"
    "세션 식별 절이 없는 채택자 pm_state.\n\n"
    "## 진행 중인 의사결정\n\n"
    "표 내용.\n"
)


def _make_handoff_with_state(handoff, tmp_path: Path, state_text: str, *, monkeypatch):
    """세션 식별 앵커가 없는 pm_state.md 를 *실재*하게 주입한 PmHandoff 인스턴스.

    log 에는 이 task 의 직전 handoff entry 를 깔아 둔다 — 차수는 state 와 log 에서 함께
    복구되므로, state 가 깨진 채택자도 log 가 있으면 차수(4차→5차)가 나오고 그제야 step3
    앵커 부재가 드러난다(둘 다 없으면 차수 미해소로 그 앞에서 중단이다).
    """
    log_file = tmp_path / "current.md"
    playbook_file = tmp_path / "pm_playbook.md"
    pm_state = tmp_path / "pm_state.md"
    log_file.write_text(
        "# log\n\n## [2026-07-05] handoff | PM 4차 (task:alpha) → 다음 PM 세션\n\n"
        "- 이 세션 박제 entries: (이 세션 박제 entry 없음)\n",
        encoding="utf-8",
    )
    playbook_file.write_text("# pm_playbook (no anchor)\n", encoding="utf-8")
    pm_state.write_text(state_text, encoding="utf-8")
    bind_pool(monkeypatch, _TaskPool())
    inst = handoff.PmHandoff(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("skip_pytest 인데 pytest 호출")),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook_file,
        pm_state_file=pm_state,

    )
    return inst, log_file, pm_state


def test_extract_session_section_none_when_anchor_absent(handoff):
    """세션 식별 절이 완전 부재하면 _extract_session_section 은 None (추측 편집 금지)."""
    assert handoff._extract_session_section(_PM_STATE_NO_ANCHOR) is None


def test_update_session_window_raises_when_anchor_absent(handoff):
    """부재 시 update_session_window 는 종전대로 ValueError (앵커 못 찾으면 지어내지 않음)."""
    with pytest.raises(ValueError):
        handoff.update_session_window(
            _PM_STATE_NO_ANCHOR, 5, "2026-07-06", "요약"
        )


def test_run_failsoft_skips_step3_when_anchor_absent(handoff, monkeypatch, tmp_path, capsys):
    """앵커 부재 pm_state → step3 스킵 경고 + rc 0(완주)·pm_state 무변경 (return 1 아님)."""
    inst, _, pm_state = _make_handoff_with_state(handoff, tmp_path, _PM_STATE_NO_ANCHOR, monkeypatch=monkeypatch)
    before = pm_state.read_text(encoding="utf-8")

    rc = _run_handoff(inst, dry_run=False, skip_pytest=True)

    assert rc == 0  # fail-soft — 전체 핸드오프가 죽지 않는다.
    err = capsys.readouterr().err
    assert "세션 식별 sliding window 스킵" in err
    assert "계속" in err
    # step3 스킵 — window 를 지어내지 않으므로 pm_state 는 편집되지 않는다.
    assert pm_state.read_text(encoding="utf-8") == before


def test_run_failsoft_continues_remaining_steps_when_anchor_absent(handoff, monkeypatch, tmp_path, capsys):
    """step3 스킵 이후에도 나머지 단계(log entry append·완료 메시지)는 진행한다."""
    inst, log_file, _ = _make_handoff_with_state(handoff, tmp_path, _PM_STATE_NO_ANCHOR, monkeypatch=monkeypatch)

    rc = _run_handoff(inst, dry_run=False, skip_pytest=True)

    assert rc == 0
    # step 2: log/current.md handoff entry skeleton 이 실제 append 됐다.
    log_text = log_file.read_text(encoding="utf-8")
    assert "handoff" in log_text and "PM 5차" in log_text
    # step 7 이후: 완료 메시지까지 도달(step3 fail-soft 가 흐름을 끊지 않음).
    out = capsys.readouterr().out
    assert "PM 5차 핸드오프 자동화 완료" in out
