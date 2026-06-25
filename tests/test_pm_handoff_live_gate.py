"""pm_handoff 라이브-게이트 step 단위테스트 (T-0151·A tier·spike harness-test-two-level-gate §3.3).

push 직전(핸드오프) 1회 라이브-게이트 step 을 검증한다 — **미push diff 가 출하경로를
건드릴 때만** `pytest -m live_gate` 를 돌려 red 면 핸드오프·push 를 차단(자동·enforced).
설계 세션(출하변경 0)은 자동 skip.

모두 hermetic — 실 pytest/LLM 미실행. git diff 는 결정론 `git_runner` stub 으로,
라이브 게이트 실행은 `run_live_gate_fn` DI seam 으로 갈아끼운다(실 subprocess 미진입).

커버:
  - 분류 3-way (`_shipping_paths_in_pending_push`): 출하변경→발동 / 비출하→skip / baseline
    해소불가·diff실패·예외→ambiguous(has_unknown).
  - run() 통합: 발동(green→계속) / skip / ambiguous→surface(비실행) / abort-on-red(rc 1).
  - escape: --live-gate 강제발동 · --no-live-gate 강제skip (분류 무시).
  - run_trigger 제외 (라이브 게이트 절대 미호출).
  - sensitivity: 가드 무력화(red 를 무시) 시 테스트가 실패하는지(non-vacuous).

도구는 패키지가 아니므로 importlib 동적 로드 (test_handoff_trigger 관용구).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_HANDOFF_PY = TOOLS / "pm_handoff.py"


def _load_module(name: str = "pm_handoff"):
    spec = importlib.util.spec_from_file_location(name, PM_HANDOFF_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hf():
    return _load_module()


# ── git_runner stub 빌더 ──────────────────────────────────────────────────────
#
# pm_handoff 의 git 호출 패턴 ("지금 push 하면 올라갈 변경 전체"·T-0151 must-fix 1):
#   diff --name-only HEAD              → 작업트리 미커밋(staged+unstaged tracked) 경로
#   ls-files --others --exclude-standard → untracked 신규파일 경로
#   rev-parse --verify --quiet <ref>   → 커밋된-미push baseline 해소 (rc 0 = 존재)
#   diff --name-only <baseline>..HEAD  → 커밋된-미push 변경 경로
# stub 은 네 호출을 구분해 결정론 응답을 돌려준다 — diff 는 baseline(`..HEAD`)/uncommitted
# (`HEAD`)를 인자로 구분한다.


def _git_stub(*, baseline_ok: bool = True, diff_paths: list[str] | None = None,
              diff_rc: int = 0, raise_exc: bool = False,
              uncommitted_paths: list[str] | None = None,
              untracked_paths: list[str] | None = None,
              uncommitted_rc: int = 0, untracked_rc: int = 0):
    """결정론 git_runner stub 을 만든다.

    커밋된-미push 경로:
      baseline_ok=False → 모든 rev-parse 가 비-0 (baseline 해소불가→그 부분 불명).
      diff_paths → `diff --name-only <baseline>..HEAD` 가 돌려줄 경로. diff_rc → 그 종료코드.
    작업트리 경로 (must-fix 1):
      uncommitted_paths → `diff --name-only HEAD`(staged+unstaged tracked). uncommitted_rc → rc.
      untracked_paths → `ls-files --others --exclude-standard`. untracked_rc → rc.
    raise_exc → 첫 호출에서 예외 (fail-soft 경로 검증).

    diff_paths 만 주던 기존 테스트는 그대로 동작한다 — 작업트리 호출은 기본 빈 응답(rc 0).
    """
    committed = diff_paths or []
    uncommitted = uncommitted_paths or []
    untracked = untracked_paths or []

    def _lines(paths: list[str]) -> str:
        return "\n".join(paths) + ("\n" if paths else "")

    def _runner(args: list[str]) -> tuple[int, str]:
        if raise_exc:
            raise RuntimeError("git boom")
        if "ls-files" in args:
            return untracked_rc, _lines(untracked)
        if "rev-parse" in args:
            return (0, "abc123\n") if baseline_ok else (1, "")
        if "diff" in args:
            # 작업트리 diff(`HEAD`)와 커밋된-미push diff(`<baseline>..HEAD`)를 인자로 구분.
            if any(".." in a for a in args):
                return diff_rc, _lines(committed)
            return uncommitted_rc, _lines(uncommitted)
        return 0, ""

    return _runner


# ── _shipping_paths_in_pending_push: 분류 3-way ───────────────────────────────


def test_shipping_paths_fires_on_engine_change(hf):
    """엔진 경로(.project_manager/tools/) 변경 → shipping_hits 비어있지 않음·unknown False."""
    runner = _git_stub(diff_paths=[".project_manager/tools/board.py", "tests/test_x.py"])
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == [".project_manager/tools/board.py"]  # tests/ 는 비-출하라 제외.
    assert unknown is False


def test_shipping_paths_fires_on_template_and_adapter(hf):
    """templates/·어댑터·진입문서·manifest 등 출하 글롭 매칭."""
    runner = _git_stub(diff_paths=[
        "templates/claude_code/CLAUDE.md",
        ".claude/agents/developer.md",
        "CLAUDE.md",
        "engine.manifest",
        "pm-import.sh",
        "requirements-dev.txt",
        ".project_manager/wiki/pm_role.md",
    ])
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert set(hits) == {
        "templates/claude_code/CLAUDE.md",
        ".claude/agents/developer.md",
        "CLAUDE.md",
        "engine.manifest",
        "pm-import.sh",
        "requirements-dev.txt",
        ".project_manager/wiki/pm_role.md",
    }
    assert unknown is False


def test_shipping_paths_skips_non_shipping(hf):
    """비-출하(tests·② wiki board/ADR/spike·status/log) → 빈 hits·unknown False → skip.

    T-0154 정확 경로 글롭 추가(`.project_manager/wiki/tickets/_template.md`·`.gitattributes`
    등) 후에도 ② dev-state wiki(ADR·spike 본문·status·pm_state·log·board)·tests-only 가
    걸리지 않는지 단언한다(과잉발동 회피). ADR-0099 같은 ② wiki 결정/spike 본문은
    출하가 아니므로 게이트가 false-fire 하면 설계 세션이 무용한 라이브 게이트를 돈다.
    """
    runner = _git_stub(diff_paths=[
        "tests/test_pm_handoff.py",
        ".project_manager/wiki/raw/spikes/some-spike.md",
        ".project_manager/wiki/decisions/ADR-0099.md",
        ".project_manager/wiki/status.md",
        ".project_manager/wiki/pm_state.md",
        ".project_manager/wiki/log/current.md",
        # T-0154 과잉발동 회피 — ② wiki board/roadmap·tests fixture 도 새 글롭에 안 걸려야.
        ".project_manager/wiki/board.md",
        ".project_manager/wiki/roadmap.md",
        ".project_manager/wiki/tickets/open/T-9999-some.md",
        "tests/fixtures/sample.template.example",
    ])
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []
    assert unknown is False


# 정확 경로 글롭으로 좁히지 않으면 포괄 글롭(`**/_template.md`·`**/*.template.md`·
# `**/.gitignore`)이 매칭했을 *비-출하* 위험 경로 — manifest 갭이 *아닌* 동명 파일들.
_NON_SHIPPING_TEMPLATE_LOOKALIKES = (
    "tests/fixtures/_template.md",                       # tests fixture — 출하 아님.
    "tests/fixtures/foo.template.md",                    # tests fixture — 출하 아님.
    ".project_manager/wiki/decisions/foo.template.md",   # ② wiki ADR 디렉토리 — 출하 아님.
    "some/nested/dir/.gitignore",                        # 비-manifest .gitignore — 출하 아님.
)


def test_shipping_paths_skips_template_lookalikes(hf):
    """정확 경로 1:1 글롭이라 manifest 갭과 동명인 *비-출하* 파일은 발동 안 함 (must-fix 1).

    포괄 글롭(`**/_template.md`·`**/*.template.md`·`**/.gitignore`)이었다면 tests fixture·②
    wiki ADR 디렉토리의 동명 파일까지 false-fire 했을 것 — 정확 경로로 좁힌 뒤엔 skip.
    """
    runner = _git_stub(diff_paths=list(_NON_SHIPPING_TEMPLATE_LOOKALIKES))
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []  # 정확 경로 글롭이라 동명 비-출하는 미발동.
    assert unknown is False


def test_template_lookalikes_are_not_shipping(hf):
    """must-fix 1 — manifest 갭과 동명인 비-출하 경로가 `_path_is_shipping` False (skip).

    `tests/fixtures/_template.md`·`tests/fixtures/foo.template.md`·② wiki
    `decisions/foo.template.md`·비-manifest `.gitignore` 가 새 정확 경로 글롭에 안 걸려야
    한다. 포괄 글롭이면 True(false-fire)였을 것 — 정밀 스코프 회귀 가드.
    """
    for path in _NON_SHIPPING_TEMPLATE_LOOKALIKES:
        assert not hf._path_is_shipping(path), (
            f"비-출하 경로가 SHIPPING_GLOBS 에 false-match (포괄 글롭 회귀): {path}"
        )


def test_shipping_paths_empty_diff_skips(hf):
    """push 대상 없음(diff 비어있음) → 빈 hits·unknown False → skip."""
    runner = _git_stub(diff_paths=[])
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []
    assert unknown is False


def test_shipping_paths_unknown_when_baseline_unresolved(hf):
    """baseline ref 해소불가(detached/upstream 미설정) → has_unknown=True (ambiguous)."""
    runner = _git_stub(baseline_ok=False)
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []
    assert unknown is True


def test_shipping_paths_unknown_when_diff_fails(hf):
    """diff 명령 자체 실패(rc≠0) → has_unknown=True (ambiguous)."""
    runner = _git_stub(diff_rc=128)
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []
    assert unknown is True


def test_shipping_paths_failsoft_on_exception(hf):
    """git 예외(미설치 등) → 크래시 없이 has_unknown=True (fail-soft)."""
    runner = _git_stub(raise_exc=True)
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []
    assert unknown is True


# ── must-fix 1: 미커밋(working tree·untracked) 출하 변경 감지 (T-0151) ───────────
#
# 핸드오프 [7/7] 은 핸드오프 *후* git commit 을 안내하므로 정상 시점엔 출하 변경이
# 커밋되지 않은 working tree·untracked 에 있다. 커밋된-미push 만 보면 게이트 미발동.


def test_shipping_paths_fires_on_uncommitted_tracked(hf):
    """staged/unstaged tracked 출하파일(diff HEAD) → 발동·unknown False (커밋 안 됐어도)."""
    runner = _git_stub(uncommitted_paths=[".project_manager/tools/pm_handoff.py", "tests/x.py"])
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == [".project_manager/tools/pm_handoff.py"]  # tests/ 는 비-출하 제외.
    assert unknown is False


def test_shipping_paths_fires_on_untracked_new_file(hf):
    """untracked 신규 출하파일(ls-files --others) → 발동·unknown False."""
    runner = _git_stub(untracked_paths=[".claude/agents/new_agent.md", "scratch.txt"])
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == [".claude/agents/new_agent.md"]  # scratch.txt 는 비-출하.
    assert unknown is False


def test_shipping_paths_fires_on_uncommitted_even_when_baseline_unresolved(hf):
    """baseline 해소불가여도 미커밋 출하 hit 이 있으면 **발동**(그 변경은 확실히 올라감).

    must-fix 1 의 ambiguous 정련 — uncommitted/untracked 출하 hit 이 있으면 커밋된-미push
    경계 불명(baseline_ok=False)과 무관하게 발동·unknown False.
    """
    runner = _git_stub(
        baseline_ok=False,
        uncommitted_paths=["templates/opencode/AGENTS.md"],
    )
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == ["templates/opencode/AGENTS.md"]
    assert unknown is False  # 발동 확정 → ambiguous 아님.


def test_shipping_paths_unions_committed_uncommitted_untracked(hf):
    """커밋된-미push ∪ 미커밋 ∪ untracked 출하 hit 을 dedup·정렬해 union 한다."""
    runner = _git_stub(
        diff_paths=[".project_manager/tools/board.py"],          # 커밋된-미push.
        uncommitted_paths=[".project_manager/tools/board.py", "CLAUDE.md"],  # 중복 + 신규.
        untracked_paths=[".opencode/agent/researcher.md"],       # untracked.
    )
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == sorted({
        ".project_manager/tools/board.py",
        "CLAUDE.md",
        ".opencode/agent/researcher.md",
    })
    assert unknown is False


def test_shipping_paths_skips_when_only_uncommitted_non_shipping(hf):
    """미커밋·untracked 가 전부 비-출하 + baseline 해소 → 빈 hits·unknown False → skip."""
    runner = _git_stub(
        uncommitted_paths=["tests/test_x.py"],
        untracked_paths=[".project_manager/wiki/raw/spikes/s.md"],
    )
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []
    assert unknown is False


def test_shipping_paths_unknown_when_uncommitted_diff_fails(hf):
    """작업트리 diff HEAD 자체 실패(rc≠0) → 작업트리 상태 불명 → has_unknown=True."""
    runner = _git_stub(uncommitted_rc=128)
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []
    assert unknown is True


def test_shipping_paths_unknown_when_ls_files_fails(hf):
    """ls-files --others 실패(rc≠0) → 작업트리 상태 불명 → has_unknown=True."""
    runner = _git_stub(untracked_rc=128)
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []
    assert unknown is True


# ── run() 통합 fixture (hermetic·DI) ──────────────────────────────────────────


def _make_handoff(hf, tmp_path: Path, *, git_runner, live_gate_fn):
    """라이브-게이트 + git_runner 를 DI 한 PmHandoff 를 만든다 (실 파일/회귀 미접촉).

    회귀(step 1)는 green stub. log/playbook 은 tmp, pm_state 는 부재 경로(3·4 skip).
    git_runner 는 출하-변경 분류용 diff 응답. live_gate_fn 은 라이브 게이트 실행 stub.
    """
    log_file = tmp_path / "current.md"
    playbook_file = tmp_path / "pm_playbook.md"
    missing_state = tmp_path / "nope" / "pm_state.md"
    log_file.write_text("# log\n", encoding="utf-8")
    playbook_file.write_text("# pm_playbook (no anchor)\n", encoding="utf-8")
    inst = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "120 passed in 1.0s\n"),
        run_git_fn=git_runner,
        run_live_gate_fn=live_gate_fn,
        log_file=log_file,
        pm_playbook_file=playbook_file,
        pm_state_file=missing_state,
    )
    return inst


def _live_gate_recorder(rc: int, out: str):
    """라이브 게이트 실행을 기록하는 stub. .calls 로 호출 여부·worktree 확인."""
    calls: list[str] = []

    def _fn(worktree: str) -> tuple[int, str]:
        calls.append(worktree)
        return rc, out

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


# ── run() 3-way + abort-on-red + escape ───────────────────────────────────────


def test_run_fires_live_gate_on_shipping_change(hf, tmp_path):
    """출하 변경(엔진) → 라이브 게이트 발동·green → rc 0 (계속)."""
    gate = _live_gate_recorder(0, "3 passed in 4.0s\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0
    assert len(gate.calls) == 1  # 발동됨.


def test_run_aborts_on_live_gate_red(hf, tmp_path):
    """출하 변경 + 라이브 게이트 red(failed) → 핸드오프 중단 rc 1."""
    gate = _live_gate_recorder(1, "1 failed, 2 passed in 4.0s\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=["templates/opencode/AGENTS.md"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 1  # 차단.
    assert len(gate.calls) == 1


def test_run_skips_live_gate_on_non_shipping(hf, tmp_path):
    """비-출하 변경(spike/ADR/tests) → 라이브 게이트 미발동 (skip)·rc 0."""
    gate = _live_gate_recorder(0, "")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[
            ".project_manager/wiki/raw/spikes/s.md", "tests/test_x.py",
        ]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0
    assert gate.calls == []  # 미발동.


def test_run_surfaces_ambiguous_without_firing(hf, tmp_path, capsys):
    """baseline 해소불가(ambiguous) → 라이브 게이트 비실행 + PM surface 안내·rc 0."""
    gate = _live_gate_recorder(0, "")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(baseline_ok=False),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0
    assert gate.calls == []  # ambiguous 는 기본 비실행.
    out = capsys.readouterr().out
    assert "분류 불명" in out
    assert "--live-gate" in out and "--no-live-gate" in out  # PM 결정 유도.


def test_run_failsoft_skip_when_live_gate_no_tests(hf, tmp_path):
    """라이브 미가용(0개 selected·rc 5·failed 없음) → green 처리 → fail-soft 통과 rc 0."""
    gate = _live_gate_recorder(5, "no tests ran in 0.1s\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/board.py"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0  # rc 5(no tests) 통과 (CI green 불변·게이트 강제 안 함).
    assert len(gate.calls) == 1


def test_run_passes_when_live_gate_all_passed_rc0(hf, tmp_path):
    """rc 0(all passed·또는 skipped-only) → 통과 rc 0 (fail-soft 통과 보존)."""
    gate = _live_gate_recorder(0, "2 passed, 1 skipped in 4.0s\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/board.py"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0
    assert len(gate.calls) == 1


# ── must-fix 2: collection/import/internal error 등 비-0(≠5)도 red 중단 (T-0151) ──
#
# 이전 `re.search("N failed")` 판정은 "failed" 요약이 없는 rc 2/3/4·collection error 를
# silently green 처리했다. rc∈{0,5} 만 통과로 좁혀 그 외 비-0 은 모두 red 중단.


def test_run_aborts_on_collection_error_no_failed_summary(hf, tmp_path):
    """collection error("1 error"·rc 2·"failed" 요약 없음) → 핸드오프 중단 rc 1."""
    gate = _live_gate_recorder(2, "ERROR collecting test_live.py\n1 error in 0.3s\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 1  # "failed" 없어도 비-0(≠5) → red 중단.
    assert len(gate.calls) == 1


def test_run_aborts_on_interrupted_rc2(hf, tmp_path):
    """rc 2(interrupted) → 핸드오프 중단 rc 1 (이전 판정은 green 처리했음)."""
    gate = _live_gate_recorder(2, "!!! KeyboardInterrupt !!!\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".claude/agents/developer.md"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 1
    assert len(gate.calls) == 1


def test_run_aborts_on_internal_error_rc3(hf, tmp_path):
    """rc 3(internal error·예: import error) → 핸드오프 중단 rc 1."""
    gate = _live_gate_recorder(3, "INTERNALERROR> ImportError: no module\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=["templates/claude_code/CLAUDE.md"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 1
    assert len(gate.calls) == 1


def test_run_aborts_on_usage_error_rc4(hf, tmp_path):
    """rc 4(usage error·예: pytest 미설치/잘못된 인자) → 핸드오프 중단 rc 1."""
    gate = _live_gate_recorder(4, "ERROR: usage: pytest ...\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/board.py"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 1
    assert len(gate.calls) == 1


def test_run_escape_force_fire_ignores_classification(hf, tmp_path):
    """--live-gate (override True) → 비-출하여도 강제 발동."""
    gate = _live_gate_recorder(0, "3 passed in 4.0s\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=["tests/test_x.py"]),  # 비-출하.
        live_gate_fn=gate,
    )
    rc = inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=False,
        live_gate_override=True,
    )
    assert rc == 0
    assert len(gate.calls) == 1  # 분류 무시·강제 발동.


def test_run_escape_force_skip_ignores_classification(hf, tmp_path):
    """--no-live-gate (override False) → 출하 변경이어도 강제 skip (미실행)."""
    gate = _live_gate_recorder(1, "1 failed in 4.0s\n")  # red 라도 안 돌려야.
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
        live_gate_fn=gate,
    )
    rc = inst.run(
        session_num=5, wave_summary="x", dry_run=False, skip_pytest=False,
        live_gate_override=False,
    )
    assert rc == 0  # 강제 skip 이라 red gate 도 무시.
    assert gate.calls == []  # 미발동.


def test_run_dry_run_skips_live_gate(hf, tmp_path):
    """--dry-run → 라이브 게이트 발동 판단·실행 자체 skip (LLM 비용/시간 회피)."""
    gate = _live_gate_recorder(0, "")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
        live_gate_fn=gate,
    )
    rc = inst.run(session_num=5, wave_summary="x", dry_run=True, skip_pytest=False)
    assert rc == 0
    assert gate.calls == []  # dry-run 은 미실행.


def test_run_skips_live_gate_when_machine_regression_red(hf, tmp_path):
    """[1/7] 기계회귀 red → 그 자리에서 중단 → 라이브 게이트 도달 안 함."""
    gate = _live_gate_recorder(0, "")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
        live_gate_fn=gate,
    )
    inst._run_pytest_fn = lambda: (1, "1 failed in 1.0s\n")  # 기계회귀 red.
    rc = inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 1
    assert gate.calls == []  # 기계회귀에서 먼저 중단.


# ── run_trigger 제외 (ctx-STOP·자동정지·LLM 못 띄움) ──────────────────────────


def test_run_trigger_never_fires_live_gate(hf, tmp_path):
    """run_trigger(ctx-STOP) 는 라이브 게이트를 절대 호출하지 않는다."""
    log_file = tmp_path / "current.md"
    playbook_file = tmp_path / "pm_playbook.md"
    missing_state = tmp_path / "nope" / "pm_state.md"
    log_file.write_text("# log\n", encoding="utf-8")
    playbook_file.write_text("# pm_playbook\n", encoding="utf-8")
    gate = _live_gate_recorder(1, "1 failed\n")  # 호출되면 안 됨.
    inst = hf.PmHandoff(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("trigger 가 pytest 호출")),
        run_git_fn=lambda args: (_ for _ in ()).throw(AssertionError("trigger 가 git 호출")),
        run_live_gate_fn=gate,
        log_file=log_file,
        pm_playbook_file=playbook_file,
        pm_state_file=missing_state,
    )
    rc = inst.run_trigger(reason="ctx-stop", ctx_pct=8)
    assert rc == 0
    assert gate.calls == []  # 자동정지 경로는 라이브 게이트 제외.


# ── sensitivity: 가드 무력화 시 테스트가 실패하는가 (non-vacuous) ──────────────


def test_sensitivity_abort_guard_is_load_bearing(hf, tmp_path):
    """abort-on-red 가드를 무력화(red 를 0 으로 흡수)하면 abort 테스트가 깨져야 한다.

    `_fire_live_gate` 가 red 를 무시하고 0 을 돌려주도록 monkeypatch → run() 이 rc 0 을
    돌려 `test_run_aborts_on_live_gate_red` 의 단언(rc==1)이 무너지는지 직접 확인한다.
    가드가 load-bearing(실제 차단 동작)임을 입증 — vacuous pass 방지.
    """
    gate = _live_gate_recorder(1, "1 failed, 2 passed in 4.0s\n")
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=["templates/opencode/AGENTS.md"]),
        live_gate_fn=gate,
    )
    # 정상: red → 중단 rc 1.
    assert inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False) == 1

    # 가드 무력화: _fire_live_gate 가 red 를 무시하고 항상 0.
    inst2 = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=["templates/opencode/AGENTS.md"]),
        live_gate_fn=gate,
    )
    inst2._fire_live_gate = lambda worktree: 0  # type: ignore[method-assign]
    # 무력화하면 red 여도 통과(rc 0) → abort 단언이 의미 있으려면 여기서 0 이 나와야 함.
    assert inst2.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False) == 0


def test_sensitivity_uncommitted_detection_is_load_bearing(hf):
    """must-fix 1 가드 무력화: 작업트리 호출을 옛 동작(빈 응답)으로 되돌리면 미커밋 출하
    감지 테스트가 깨져야 한다(non-vacuous).

    `_uncommitted_and_untracked_paths` 가 항상 빈 목록(=커밋된-미push 만 보던 옛 동작)을
    돌려주도록 monkeypatch → baseline 해소만 가능한 미커밋 출하 변경은 hits 가 비어 발동
    안 함을 직접 확인한다. 정상(패치 전)은 발동(hits 비어있지 않음).
    """
    runner = _git_stub(uncommitted_paths=[".project_manager/tools/pm_handoff.py"])
    # 정상: 미커밋 출하 hit → 발동.
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == [".project_manager/tools/pm_handoff.py"]
    assert unknown is False

    # 가드 무력화: 작업트리/untracked 를 안 보는 옛 동작(항상 빈 목록)으로 되돌린다.
    orig = hf._uncommitted_and_untracked_paths
    hf._uncommitted_and_untracked_paths = lambda worktree, runner: []  # type: ignore[assignment]
    try:
        hits2, unknown2 = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    finally:
        hf._uncommitted_and_untracked_paths = orig  # 모듈 전역 복구.
    # 무력화하면 커밋된-미push 만 보므로(여긴 비어있음) 발동 안 함 → 감지 테스트가 깨질 것.
    assert hits2 == []
    assert unknown2 is False


def test_sensitivity_rc_in_0_5_guard_is_load_bearing(hf, tmp_path):
    """must-fix 2 가드 무력화: 좁힌 rc∈{0,5} 판정을 옛 `"N failed"` 판정으로 되돌리면
    collection error(rc 2·"failed" 요약 없음) abort 테스트가 깨져야 한다(non-vacuous).

    `_fire_live_gate` 를 옛 판정(`rc != 0 and re.search("N failed")`)으로 monkeypatch →
    rc 2·"1 error"(failed 없음)가 silently green(rc 0) 처리되는지 직접 확인한다.
    정상(좁힌 가드)은 rc 1 로 중단.
    """
    import re

    gate = _live_gate_recorder(2, "ERROR collecting test_live.py\n1 error in 0.3s\n")
    # 정상: rc 2(≠0/5) → red 중단 rc 1.
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
        live_gate_fn=gate,
    )
    assert inst.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False) == 1

    # 가드 무력화: 옛 판정("N failed" 요약이 있을 때만 red).
    def _old_fire(worktree):
        rc, out = gate(worktree)
        if rc != 0 and re.search(r"\d+ failed", out):
            return 1
        return 0

    inst2 = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
        live_gate_fn=gate,
    )
    inst2._fire_live_gate = _old_fire  # type: ignore[method-assign]
    # 옛 판정은 "failed" 요약 없는 rc 2 collection error 를 green 처리 → abort 단언 무너짐.
    assert inst2.run(session_num=5, wave_summary="x", dry_run=False, skip_pytest=False) == 0


# ── T-0154: SHIPPING_GLOBS ↔ engine.manifest 정합 가드 ──────────────────────────
#
# 라이브-게이트 발동 판단(SHIPPING_GLOBS)이 출하 진실(engine.manifest)과 drift 하면
# manifest 가 출하한다고 명시한 파일이 어떤 글롭에도 안 잡혀 게이트가 false-skip 한다
# (미검증 출하). manifest 전개 경로 전부가 SHIPPING_GLOBS 로 커버됨을 단언해 다음
# manifest 항목 추가 시 SHIPPING_GLOBS 갱신 누락을 push 전에 잡는다(손목록 drift→가드).

ENGINE_MANIFEST = REPO / ".project_manager" / "engine.manifest"

# PM 36 실측 미커버 6경로 — 글롭 추가 전엔 어떤 SHIPPING_GLOB 에도 안 잡혔다.
_MANIFEST_GAP_PATHS = (
    ".gitattributes",
    ".github/workflows/regression.yml",
    ".project_manager/.gitignore",
    ".project_manager/wiki/pm_state.template.md",
    ".project_manager/wiki/raw/spikes/_template.md",
    ".project_manager/wiki/tickets/_template.md",
)


def _expand_manifest_shipping_paths():
    """engine.manifest 의 출하 경로를 디스크로 전개한다 — 파일은 그대로, 디렉토리는 하위 파일.

    한 줄 = 한 경로(repo 루트 기준·'#' 주석). 디렉토리 항목은 os.walk 로 실 파일 경로로
    전개한다(gap_check.py·PM 36 실측과 동형). 반환: repo-rel 경로 set.
    """
    import os

    paths: set[str] = set()
    for line in ENGINE_MANIFEST.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        abs_p = REPO / entry
        if abs_p.is_dir():
            for dirpath, _dirnames, filenames in os.walk(abs_p):
                for fn in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fn), REPO)
                    paths.add(rel.replace(os.sep, "/"))
        else:
            paths.add(entry)
    return paths


def test_six_manifest_gap_paths_now_shipping(hf):
    """PM 36 실측 미커버 6경로가 이제 `_path_is_shipping` True (갭 6개 닫힘·T-0154)."""
    for path in _MANIFEST_GAP_PATHS:
        assert hf._path_is_shipping(path), f"manifest 출하 경로가 SHIPPING_GLOBS 미커버: {path}"


def test_engine_manifest_subset_of_shipping_globs(hf):
    """engine.manifest 전개 경로 전부가 SHIPPING_GLOBS 로 커버됨 (정합 가드·manifest→globs 단방향).

    출하 진실(engine.manifest) ⊆ SHIPPING_GLOBS 커버. 미커버 1개라도 있으면 fail —
    다음 manifest 항목 추가 시 SHIPPING_GLOBS 동기화 누락을 push 전에 잡는다(drift 차단).
    역방향(글롭이 manifest 밖 잡음)은 의도된 출하(`pm-*.sh` 파사드·진입문서)라 단언 안 함.
    """
    expanded = _expand_manifest_shipping_paths()
    assert expanded, "engine.manifest 전개 경로가 비어있다 (manifest 위치·파싱 확인)."
    uncovered = sorted(p for p in expanded if not hf._path_is_shipping(p))
    assert uncovered == [], (
        f"engine.manifest 출하 경로 {len(uncovered)}개가 SHIPPING_GLOBS 미커버 — "
        f"SHIPPING_GLOBS 갱신 필요(manifest↔globs drift): {uncovered}"
    )


def test_sensitivity_manifest_conformance_guard_is_load_bearing(hf):
    """정합 가드 sensitivity: 새 정확 경로 글롭 1개를 SHIPPING_GLOBS 에서 제거하면 가드가
    fail 재현하는지(non-vacuous) 직접 확인한다.

    정확 경로 글롭 `.project_manager/wiki/tickets/_template.md` 1개 제거 시 그 manifest 갭
    경로가 다시 미커버가 돼야 한다 → 정합 가드의 단언(uncovered == [])이 무너짐. 모듈 전역 복구.
    """
    removed_glob = ".project_manager/wiki/tickets/_template.md"
    orig = hf.SHIPPING_GLOBS
    assert removed_glob in orig, "전제 위반 — 제거 대상 정확 경로 글롭이 SHIPPING_GLOBS 에 없다."
    # 정확 경로 글롭 1개 제거 — 그 manifest 갭 경로(ticket 스캐폴드)가 다시 미커버.
    hf.SHIPPING_GLOBS = tuple(g for g in orig if g != removed_glob)
    try:
        expanded = _expand_manifest_shipping_paths()
        uncovered = [p for p in expanded if not hf._path_is_shipping(p)]
    finally:
        hf.SHIPPING_GLOBS = orig  # 모듈 전역 복구.
    # 글롭 무력화하면 그 정확 경로가 미커버로 드러나야 정합 가드가 load-bearing.
    assert removed_glob in uncovered, (
        "글롭 제거 후에도 그 경로가 미커버로 안 드러나면 정합 가드가 vacuous — "
        f"uncovered={sorted(uncovered)}"
    )
