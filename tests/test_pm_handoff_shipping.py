"""pm_handoff 출하 변경 surface 단위테스트 (ADR-0039 D4·spike live-gate-redesign-2026-07-03).

핸드오프 [1b/7] step 은 **비차단 surface** 다 — 라이브 테스트를 돌리지 않고 미push diff ∩
SHIPPING_GLOBS 를 분류해 미검증 출하 변경이 있으면 "릴리즈 전 라이브(release wave) 필요"
1줄을 출력하고 핸드오프를 계속한다(rc 무영향). 라이브 LLM 검증(실 하네스 smoke)은
릴리즈(① main 머지) 단일 지점(release wave)으로 모았다(ADR-0039). 구 차단 게이트
(`_fire_shipping_test`·`_run_shipping_test`·`--shipping-test`/`--no-shipping-test`·outer
timeout)는 폐지됐다 — 분류기(`_shipping_paths_in_pending_push`·`SHIPPING_GLOBS`)만 존치
(surface 의 기반·향후 게이트 복원 가능성의 가역 지점).

모두 hermetic — 실 pytest/LLM 미실행. git diff 는 결정론 `git_runner` stub 으로 갈아끼운다.

커버:
  - 분류 3-way (`_shipping_paths_in_pending_push`): 출하변경→hits / 비출하→skip / baseline
    해소불가·diff실패·예외→ambiguous(has_unknown). (존치 분류기·불변)
  - run() surface 3-way: hits→경고 1줄·비차단 / unknown→가능성 경고 / 무변경→"출하 변경 없음".
  - 비차단: 출하 변경이 있어도 rc 0 + log skeleton append(중단 안 함). dry-run/회귀-red 경로.
  - 폐지: `--shipping-test`/`--no-shipping-test` 인자 에러 · 차단 심볼 부재.
  - 정합 가드: SHIPPING_GLOBS ↔ engine.manifest (존치 분류기·drift 차단).
  - sensitivity: 분류기 가드 무력화 시 테스트가 실패하는지(non-vacuous).

도구는 패키지가 아니므로 importlib 동적 로드 (test_handoff_trigger 관용구).
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from _repo_owned_inventory import TRACKED_ONLY, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_HANDOFF_PY = TOOLS / "pm_handoff.py"
_GIT = shutil.which("git")
requires_git_binary = pytest.mark.skipif(
    _GIT is None,
    reason="git 바이너리 부재 — 임시 repo TRACKED_ONLY sensitivity 실행 불가.",
)



def _run_handoff(inst, **kw):
    """핸드오프 실행 — 승인 게이트에 정식 승인값을 실어 통과시킨다.

    이 모듈의 축은 승인 게이트가 아니다(그 축은 ``tests/test_pm_handoff_user_ack.py``가
    소유한다). 승인 대상값은 task > 슬롯 이름 > legacy solo sentinel 순으로 정해진다.
    """
    if "user_ack" not in kw:
        slot = kw.get("worktree_slot")
        kw["user_ack"] = kw.get("task") or (slot.rsplit("/", 1)[-1] if slot else "solo")
    return inst.run(**kw)

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


# ── _shipping_paths_in_pending_push: 분류 3-way (존치 분류기·불변) ─────────────


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
    출하가 아니므로 게이트가 false-fire 하면 설계 세션이 무용한 출하 테스트를 돈다.
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
    """정확 경로 1:1 글롭이라 manifest 갭과 동명인 *비-출하* 파일은 hits 안 잡힘 (must-fix 1).

    포괄 글롭(`**/_template.md`·`**/*.template.md`·`**/.gitignore`)이었다면 tests fixture·②
    wiki ADR 디렉토리의 동명 파일까지 false-fire 했을 것 — 정확 경로로 좁힌 뒤엔 skip.
    """
    runner = _git_stub(diff_paths=list(_NON_SHIPPING_TEMPLATE_LOOKALIKES))
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == []  # 정확 경로 글롭이라 동명 비-출하는 미매칭.
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
# 커밋되지 않은 working tree·untracked 에 있다. 커밋된-미push 만 보면 분류 미탐지.


def test_shipping_paths_fires_on_uncommitted_tracked(hf):
    """staged/unstaged tracked 출하파일(diff HEAD) → hits·unknown False (커밋 안 됐어도)."""
    runner = _git_stub(uncommitted_paths=[".project_manager/tools/pm_handoff.py", "tests/x.py"])
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == [".project_manager/tools/pm_handoff.py"]  # tests/ 는 비-출하 제외.
    assert unknown is False


def test_shipping_paths_fires_on_untracked_new_file(hf):
    """untracked 신규 출하파일(ls-files --others) → hits·unknown False."""
    runner = _git_stub(untracked_paths=[".claude/agents/new_agent.md", "scratch.txt"])
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == [".claude/agents/new_agent.md"]  # scratch.txt 는 비-출하.
    assert unknown is False


def test_shipping_paths_fires_on_uncommitted_even_when_baseline_unresolved(hf):
    """baseline 해소불가여도 미커밋 출하 hit 이 있으면 **hits 확정**(그 변경은 확실히 올라감).

    must-fix 1 의 ambiguous 정련 — uncommitted/untracked 출하 hit 이 있으면 커밋된-미push
    경계 불명(baseline_ok=False)과 무관하게 hits·unknown False.
    """
    runner = _git_stub(
        baseline_ok=False,
        uncommitted_paths=["templates/opencode/AGENTS.md"],
    )
    hits, unknown = hf._shipping_paths_in_pending_push("/wt", git_runner=runner)
    assert hits == ["templates/opencode/AGENTS.md"]
    assert unknown is False  # hit 확정 → ambiguous 아님.


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


def _make_handoff(hf, tmp_path: Path, *, git_runner):
    """git_runner 를 DI 한 PmHandoff 를 만든다 (실 파일/회귀 미접촉).

    회귀(step 1)는 green stub. log/playbook 은 tmp, pm_state 는 부재 경로(3·4 skip).
    git_runner 는 출하-변경 분류용 diff 응답. ADR-0039 D4 이후 [1b/7] 은 비차단 surface 라
    출하 테스트 실행 seam(구 run_shipping_test_fn)은 없다.
    """
    log_file = tmp_path / "current.md"
    playbook_file = tmp_path / "pm_playbook.md"
    missing_state = tmp_path / "nope" / "pm_state.md"
    log_file.write_text("# log\n", encoding="utf-8")
    playbook_file.write_text("# pm_playbook (no anchor)\n", encoding="utf-8")
    inst = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "120 passed in 1.0s\n"),
        run_git_fn=git_runner,
        log_file=log_file,
        pm_playbook_file=playbook_file,
        pm_state_file=missing_state,
    )
    return inst


# ── run() 출하 surface 3-way (비차단·ADR-0039 D4) ──────────────────────────────
#
# [1b/7] 은 라이브 테스트를 돌리지 않고 미push diff ∩ SHIPPING_GLOBS 를 분류해 미검증 출하
# 변경을 1줄 surface 한다 — 핸드오프를 차단하지 않는다(rc 무영향). 라이브 LLM 검증은
# 릴리즈(① main 머지) 단일 지점(release wave)으로 모았다(ADR-0039).


def test_run_surfaces_shipping_change_nonblocking(hf, tmp_path, capsys):
    """출하 변경(엔진) → "미검증 출하 변경 N파일 … release wave" 1줄 surface·rc 0(비차단)."""
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[
            ".project_manager/tools/pm_handoff.py", "tests/test_x.py",
        ]),
    )
    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0  # 비차단 — 출하 변경이 있어도 핸드오프 진행.
    out = capsys.readouterr().out
    assert "미검증 출하 변경" in out
    assert "1파일" in out            # tests/ 는 비-출하 제외 → 1파일.
    assert "release wave" in out


def test_run_surfaces_ambiguous_nonblocking(hf, tmp_path, capsys):
    """baseline 해소불가(분류불명) → "가능성 … 분류 불명 … release wave" surface·rc 0."""
    inst = _make_handoff(hf, tmp_path, git_runner=_git_stub(baseline_ok=False))
    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "분류 불명" in out
    assert "release wave" in out


def test_run_reports_no_shipping_change(hf, tmp_path, capsys):
    """비-출하 변경(spike/ADR/tests) → "출하 변경 없음" skip 사유·rc 0·경고 미출력."""
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[
            ".project_manager/wiki/raw/spikes/s.md", "tests/test_x.py",
        ]),
    )
    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "출하 변경 없음" in out
    assert "미검증 출하 변경" not in out  # 경고 미출력.


def test_run_shipping_surface_never_blocks_handoff(hf, tmp_path):
    """출하 변경이 있어도 [2/7] log skeleton 이 append 된다 — surface 는 절대 중단 안 함(비차단).

    구 차단 게이트라면 여기서 return 1 로 log 미접촉이었다. 비차단 확증: 출하 변경 hit 이
    있는데도 핸드오프가 진행돼 log skeleton 이 실제로 써진다.
    """
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=["templates/opencode/AGENTS.md"]),
    )
    rc = _run_handoff(inst, session_num=7, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 0
    assert "PM 7차" in (tmp_path / "current.md").read_text(encoding="utf-8")


def test_run_dry_run_skips_shipping_surface(hf, tmp_path, capsys):
    """--dry-run → 출하 surface 분류·출력 자체 skip (미리보기·git 비호출)."""
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
    )
    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=True, skip_pytest=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run] 출하 변경 surface skip" in out
    assert "미검증 출하 변경" not in out  # 분류 자체 안 함.


def test_run_skips_shipping_surface_when_machine_regression_red(hf, tmp_path, capsys):
    """[1/7] 기계회귀 red → 그 자리에서 중단(rc 1) → 출하 surface 도달 안 함."""
    inst = _make_handoff(
        hf, tmp_path,
        git_runner=_git_stub(diff_paths=[".project_manager/tools/pm_handoff.py"]),
    )
    inst._run_pytest_fn = lambda: (1, "1 failed in 1.0s\n")  # 기계회귀 red.
    rc = _run_handoff(inst, session_num=5, wave_summary="x", dry_run=False, skip_pytest=False)
    assert rc == 1
    out = capsys.readouterr().out
    assert "미검증 출하 변경" not in out  # 회귀에서 먼저 중단 → surface 미도달.


# ── 폐지: 차단 게이트 CLI 인자·심볼 제거 (ADR-0039 D4·하위호환 없이 즉시) ─────────
#
# 결정 = 하위호환 없이 즉시 에러(내부 도구·채택자 스크립트 의존 없음 확인·ticket 결정).


@pytest.mark.parametrize("flag", ["--shipping-test", "--no-shipping-test"])
def test_removed_shipping_test_flags_error(hf, flag):
    """폐지된 `--shipping-test`/`--no-shipping-test` 는 argparse 가 즉시 거부한다(SystemExit)."""
    parser = hf.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--session-num", "5", "--wave-summary", "x", flag])


def test_removed_shipping_symbols_absent(hf):
    """폐지 심볼(차단 실행·타임아웃)이 모듈/클래스에서 제거·분류기와 surface step 은 존치."""
    # 폐지 — 차단 실행·outer timeout·escape.
    assert not hasattr(hf, "_run_shipping_test")
    assert not hasattr(hf, "_shipping_test_timeout")
    assert not hasattr(hf, "_SHIPPING_TEST_TIMEOUT_DEFAULT")
    assert not hasattr(hf.PmHandoff, "_fire_shipping_test")
    assert not hasattr(hf.PmHandoff, "_shipping_test_step")
    # 존치 — 분류기(가역 기반)·비차단 surface step.
    assert hasattr(hf, "_shipping_paths_in_pending_push")
    assert hasattr(hf, "SHIPPING_GLOBS")
    assert hasattr(hf.PmHandoff, "_shipping_surface_step")


# ── sensitivity: 분류기 가드 무력화 시 테스트가 실패하는가 (non-vacuous) ────────


def test_sensitivity_uncommitted_detection_is_load_bearing(hf):
    """must-fix 1 가드 무력화: 작업트리 호출을 옛 동작(빈 응답)으로 되돌리면 미커밋 출하
    감지 테스트가 깨져야 한다(non-vacuous).

    `_uncommitted_and_untracked_paths` 가 항상 빈 목록(=커밋된-미push 만 보던 옛 동작)을
    돌려주도록 monkeypatch → baseline 해소만 가능한 미커밋 출하 변경은 hits 가 비어
    미탐지임을 직접 확인한다. 정상(패치 전)은 hits 비어있지 않음.
    """
    runner = _git_stub(uncommitted_paths=[".project_manager/tools/pm_handoff.py"])
    # 정상: 미커밋 출하 hit → 탐지.
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
    # 무력화하면 커밋된-미push 만 보므로(여긴 비어있음) 미탐지 → 감지 테스트가 깨질 것.
    assert hits2 == []
    assert unknown2 is False


# ── T-0154: SHIPPING_GLOBS ↔ engine.manifest 정합 가드 (존치 분류기) ────────────
#
# 출하 변경 분류(SHIPPING_GLOBS)가 출하 진실(engine.manifest)과 drift 하면 manifest 가
# 출하한다고 명시한 파일이 어떤 글롭에도 안 잡혀 surface 가 false-skip 한다(미검증 출하
# 가시성 상실). manifest 전개 경로 전부가 SHIPPING_GLOBS 로 커버됨을 단언해 다음 manifest
# 항목 추가 시 SHIPPING_GLOBS 갱신 누락을 push 전에 잡는다(손목록 drift→가드).

ENGINE_MANIFEST = REPO / ".project_manager" / "engine.manifest"

# PM 36 실측 미커버 경로 — 글롭 추가 전엔 어떤 SHIPPING_GLOB 에도 안 잡혔다.
# regression.yml 은 T-0589에서 adopter manifest 비출하로 전환되어 이 manifest-gap 집합에서 빠졌다.
_MANIFEST_GAP_PATHS = (
    ".gitattributes",
    ".project_manager/.gitignore",
    ".project_manager/wiki/pm_state.template.md",
    ".project_manager/wiki/raw/spikes/_template.md",
    ".project_manager/wiki/tickets/_template.md",
)


def _expand_manifest_shipping_paths(
    repo: Path = REPO,
    manifest: Path = ENGINE_MANIFEST,
):
    """engine.manifest 의 출하 경로를 디스크로 전개한다 — 파일은 그대로, 디렉토리는 하위 파일.

    한 줄 = 한 경로(repo 루트 기준·'#' 주석). 디렉토리 항목은 출하 의미인 TRACKED_ONLY
    repo-owned seam으로 전개한다. 반환: repo-rel 경로 set.
    """
    paths: set[str] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        abs_p = repo / entry
        if abs_p.is_dir():
            paths.update(
                path.relative_to(repo).as_posix()
                for path in repo_owned_paths(repo, entry, mode=TRACKED_ONLY)
            )
        else:
            paths.add(entry)
    return paths


def test_manifest_gap_paths_now_shipping(hf):
    """현재 manifest gap 경로가 모두 `_path_is_shipping` True다(T-0154·T-0589)."""
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


def test_retired_root_regression_workflow_is_not_shipping(hf):
    """T-0589 역방향 가드 — manifest 제거 후 stale SHIPPING_GLOBS 재유입 차단."""
    assert not hf._path_is_shipping(".github/workflows/regression.yml")


@requires_git_binary
def test_manifest_shipping_inventory_ignores_untracked_inflow(tmp_path):
    """TRACKED_ONLY 전환 후 manifest 출하 판정은 미추적 파일 유입에 흔들리지 않는다."""
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    manifest = tmp_path / "engine.manifest"
    manifest.write_text("ship\n", encoding="utf-8")
    tracked = tmp_path / "ship" / "tracked.md"
    tracked.parent.mkdir()
    tracked.write_text("tracked\n", encoding="utf-8")
    untracked = tracked.with_name("local-only.md")
    untracked.write_text("untracked\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "engine.manifest", "ship/tracked.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    assert _expand_manifest_shipping_paths(tmp_path, manifest) == {
        "ship/tracked.md"
    }
    assert untracked.is_file(), "미추적 유입 sensitivity fixture가 실 디스크에 있어야 함"


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
