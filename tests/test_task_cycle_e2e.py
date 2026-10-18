"""task 사이클 e2e — 생성→편입→작업→핸드오프→재개 (W4·ADR-0068 사이클 게이트·release-marked).

실 파일시스템/실 git/실 엔진(worktree_pool·pm_bootstrap·pm_handoff)으로 **사용자 사이클을 한
케이스로 완주**한다:

  ① `--task` 신규 부트스트랩 — 보유 집합 전수 열거(0슬롯 = "작업공간: (없음)"·진입·I2).
  ② `worktree add --task`(생성+편입·create_slot owner_task) + alloc(추가 대여·I3 항상-신규) —
     같은 task 가 같은 repo 2슬롯 보유(집합 성장) → 재부트스트랩이 "보유 2 — 전수 검증" 열거.
  ③ 작업(한 슬롯 커밋·다른 슬롯 무변경) 후 핸드오프 — **변경 슬롯만 회귀**(무변경 슬롯 skip)·
     **전 슬롯 재스냅**(집합 전체 두고 간다·T-0388 루프)·**트리거 `--task`**(T-0394)·정상-종료
     task 종료 기록(닫힘).
  ④ 트리거 그대로 재개 — 닫힌 기록 `resumed`·집합 재수령(보유 2)·
     0단계 record↔live ✓(재스냅이 diverged 오탐을 닫음·PM 78 실증).

release-marked — 릴리즈마다 "단위게이트 green·사이클 단절"(PM 78 실증) 클래스를 기계 차단한다
(livegate 수집 pin 편입). 라이브 LLM 불요(순수 기계 e2e).

**hermetic**: tmp scratch 루트에서 돌며 실 장부/실 PM 홈을 절대 건드리지 않는다 — worktree_pool·
pm_handoff 모듈 전역(REPO·LEASES_FILE·TASKS_DIR·WORK_DIR 등)을 tmp 로 재바인딩한다(test_worktree_
pool.py·test_pm_handoff.py 의 real-git 픽스처 동류). 검증은 실 git worktree·실 리스/tasks 장부·실
엔진 함수 호출로 하고, board/log/pytest 러너만 hermetic seam 으로 주입한다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")


# ── 실 git 헬퍼 (test_worktree_pool.py 동형) ──────────────────────────────────


def _git(cwd, *argv):
    """테스트용 실 git 헬퍼 — check=True·UTF-8 캡처·결정론 author/committer."""
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    return subprocess.run([_GIT, "-C", str(cwd), *argv], check=True,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def _init_repo(path):
    """초기 커밋 있는 git repo (worktree add base 가 되도록·비보호 브랜치명)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "trunk")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


# ── 모듈 로드 + tmp 재배선 ────────────────────────────────────────────────────


def _load_wp_bound(proj: Path):
    """worktree_pool.py 를 새로 로드하고 경로 전역을 `proj` tmp 루트로 재바인딩한다.

    test_worktree_pool.py `_load_wp_bound` 와 동형 — import 시점에 굳은 실 REPO 경로(리스장부·
    락·tasks 서술 공간·work/·bare 원)를 tmp 로 전부 덮어 실 장부 격리를 보장한다.
    """
    spec = importlib.util.spec_from_file_location("wp_cycle_e2e", TOOLS / "worktree_pool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    local = proj / ".project_manager" / ".local"
    for name, val in {
        "REPO": proj,
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "LEASES_LOCK": local / "worktree-leases.lock",
        "TASKS_DIR": local / "tasks",
        "WORK_DIR": proj / "work",
        "REPOS_DIR": proj / ".repos",
        "REPO_HOOKS_DIR": local / "repo-hooks",
    }.items():
        setattr(mod, name, val)
    return mod


def _load_handoff_bound(proj: Path):
    """pm_handoff.py 를 로드하고 REPO 를 tmp 로 재바인딩한다.

    task 모드 핸드오프의 REPO-파생 경로(task pm_state `_task_pm_state_file`·template·대시보드 락
    `_dashboard_lock`·회귀 cwd)를 tmp 로 격리한다 — 이들은 호출 시점 REPO 를 추종하는 함수라
    import 후 재바인딩으로 충분하다(TOOLS_DIR 상수는 실 위치 유지 = task 명 검증용 read-only 로드).
    """
    spec = importlib.util.spec_from_file_location("hf_cycle_e2e", TOOLS / "pm_handoff.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REPO = proj
    return mod


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_cycle_e2e", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk_real_bare(wp, repo: str, tmp_path: Path) -> Path:
    """실 bare repo `.repos/<repo>.git` (`pm-config repo add` 동형·family origin → clone --bare)."""
    origin = _init_repo(tmp_path / f"{repo}-origin")
    bare = wp.bare_repo_path(repo)
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "--bare", "-q", str(origin), str(bare))
    return bare


# ── 부트스트랩 hermetic 대역 (test_pm_bootstrap_task_slot_set.py `_make` 동류) ─


class _FakeBoard:
    """board 대역 — 엔진 앵커(통과) + 보호브랜치 목록(`_repo_protected`)."""

    def _pm_home_worktree_misanchor(self, anchor, **_kw):
        return None  # PM 홈(통과) — 앵커 거부 안 함.

    def _repo_protected(self, repo):
        return ["main", "master", "trunk"]


def _fake_board_fn(args):
    if args[:1] == ["lint"]:
        return 0, "✓ no lint issues\n"
    return 0, "  [open   ] T-0001  x  pm  tag\n"


def _fake_git_fn(args):
    if args[:2] == ["rev-parse", "--abbrev-ref"]:
        return 0, "cyc-1\n"
    if args[:2] == ["log", "--oneline"]:
        return 0, "abc123 subj\n"
    return 0, ""


def _make_bootstrap(bootstrap_mod, wp, tmp_path):
    """실 worktree_pool 을 주입한 격리 PmBootstrap — board/git/log/areas/pm_state 는 hermetic."""
    log_file = tmp_path / "boot_current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("| repo | prefix |\n|---|---|\n| app | app |\n", encoding="utf-8")
    pm_state_file = tmp_path / "boot_pm_state.md"
    pm_state_file.write_text("", encoding="utf-8")
    return bootstrap_mod.PmBootstrap(
        run_board_fn=_fake_board_fn,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨")),
        run_git_fn=_fake_git_fn,
        log_file=log_file,
        areas_file=areas_file,
        worktree_pool=wp,
        board=_FakeBoard(),
        pm_state_file=pm_state_file,
        board_dir=tmp_path / "noboard",   # board submodule 부재(솔로) — freshness graceful skip.
    )


_PROMPT_ANCHOR = "## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)"
_TRIGGER_PLAYBOOK = (
    "# pm_playbook\n\n"
    f"{_PROMPT_ANCHOR}\n\n"
    "설명 문단.\n\n"
    "```\n"
    "당신은 이 프로젝트의 PM 세션입니다.\n"
    "지금 /pm-bootstrap 을 실행하세요.\n"
    "```\n\n"
    "## 다른 절\n"
)


# ════════════════════════════════════════════════════════════════════════════
# 사이클 완주 e2e (①~④·release-marked)
# ════════════════════════════════════════════════════════════════════════════


def test_task_new_identity_then_bootstrap_resumes(tmp_path, capsys):
    """`task new`(정체성만) → 부트스트랩이 **resume 으로 이어받는다** (T-0909·사이클 앞단 분리).

    생성 진입점이 둘이 된 뒤에도 사이클이 끊기지 않는지 재는 축이다 — 정체성만 만든 task(닫힘·
    슬롯 0)에 `--task` 로 진입하면 신규 생성(`created`)이 아니라 clean `resumed` 여야 한다(회수 경고
    없음·행 1개). 실 장부·실 pm_state·실 엔진(worktree_pool·pm_bootstrap)으로 판정한다.
    """
    proj = tmp_path / "proj"
    (proj / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    template_dst = proj / ".project_manager" / "wiki" / "pm_state.template.md"
    template_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / ".project_manager" / "wiki" / "pm_state.template.md", template_dst)
    (proj / "work").mkdir(parents=True, exist_ok=True)
    (proj / ".repos").mkdir(parents=True, exist_ok=True)

    wp = _load_wp_bound(proj)
    bootstrap_mod = _load_module("pm_bootstrap")
    task = "idle-made"

    # ① 정체성만 생성 — 세션에 묶지 않는다(닫힘·슬롯 0·pm_state 즉시 생성).
    created = wp.create_task(task)
    assert created is not None and created.open is False and created.prefix is None
    state_after_create = wp.task_pm_state_file(task).read_bytes()

    # ② 그 정체성으로 부트스트랩 진입 — created 가 아니라 resumed 여야 한다.
    wp.record_task_session_claim(task, "s-e2e")
    rc = _make_bootstrap(bootstrap_mod, wp, tmp_path).run(task=task)
    assert rc == 0
    out = capsys.readouterr().out
    # 상태 표기는 identity surface 한 줄이 진실이다(카드 정적 안내문에도 "신규 task" 가 있어
    # 부분 문자열 부재로는 못 가른다).
    assert f"task `{task}`** PM · prefix=(없음) · 상태=재개(resume)." in out, (
        "정체성만 만든 task 진입인데 clean resume 으로 분류되지 않았다"
    )
    assert "상태=신규 task" not in out, "이미 만든 정체성인데 신규 생성으로 분류됐다"
    assert "회수 진입" not in out, "닫힌 기록 재개인데 회수 경고 오탐"
    assert "작업 슬롯: (없음)" in out, "슬롯 0 진입 열거가 사라졌다"

    # ③ 장부는 행 하나(중복 생성 없음)이고 열림 표시만 바뀐다. pm_state 는 재개가 덮지 않는다.
    rows = json.loads(
        (proj / ".project_manager" / ".local" / "worktree-leases.json").read_text(encoding="utf-8")
    )["tasks"]
    assert [row["name"] for row in rows] == [task]
    assert rows[0]["session"] == "s-e2e" and rows[0]["prefix"] is None
    assert wp.task_pm_state_file(task).read_bytes() == state_after_create


@pytest.mark.release
@_git_required
def test_task_cycle_create_incorporate_work_handoff_resume(tmp_path, capsys):
    """task 세션 전체 사이클 완주 — 실 git/실 장부/실 엔진 (ADR-0068 사이클 게이트·PM 78 재현).

    "단위게이트 green·사이클 단절"(진입/실행/할당/퇴장/인계 5표면 각각 통과해도 사용자 사이클이
    끊기던 PM 78 실증)을 릴리즈마다 기계 차단한다 — W1(alloc 항상-신규+add --task 이음)·W2(핸드오프
    집합 퇴장)·W3(진입 집합 열거) 표면을 한 흐름으로 태운다.
    """
    proj = tmp_path / "proj"
    (proj / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    template_dst = proj / ".project_manager" / "wiki" / "pm_state.template.md"
    template_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / ".project_manager" / "wiki" / "pm_state.template.md", template_dst)
    (proj / "work").mkdir(parents=True, exist_ok=True)
    (proj / ".repos").mkdir(parents=True, exist_ok=True)

    wp = _load_wp_bound(proj)
    _mk_real_bare(wp, "app", tmp_path)
    bootstrap_mod = _load_module("pm_bootstrap")
    handoff_mod = _load_handoff_bound(proj)

    task = "cyc"

    # ── ① `--task` 신규 부트스트랩 — 집합 열거(0슬롯) ─────────────────────────────
    wp.record_task_session_claim(task, "s-e2e")
    rc = _make_bootstrap(bootstrap_mod, wp, tmp_path).run(task=task)
    assert rc == 0
    out1 = capsys.readouterr().out
    assert "작업 슬롯: (없음)" in out1, "0슬롯 진입인데 '(없음)' 열거 없음(I2)"
    assert "신규 task" in out1, "신규 task 정체성 surface 없음(F1)"
    # 부트스트랩이 task 정체성과 state를 함께 생성했다(tasks 장부·created·slot 0개와 무관).
    assert any(t.name == task for t in wp.list_tasks()), "① 부트스트랩이 task 를 생성하지 않았다"
    task_state = wp.task_pm_state_file(task)
    assert task_state.exists(), "① task 생성 시 pm_state 즉시 생성 계약 위반"
    assert wp.TASK_PM_STATE_EMPTY_MARKER in task_state.read_text(encoding="utf-8")

    # ── ② worktree add --task(생성+편입) + alloc(추가 대여·I3) ───────────────────
    # add --task = create_slot(owner_task): 새 슬롯 생성 + 그 슬롯을 task 명의 대여(ⓓB). 슬롯 브랜치
    # app_1 은 등록 base(trunk)에서 파고, 작업 브랜치 cyc-1 은 `task branch` 로 부모(trunk)를 기록하며 만든다.
    slot1 = wp.create_slot("app", base="trunk", owner_task=task, init_submodules=False)
    made = wp.create_task_branch(slot1.slot, "cyc-1", parent="trunk", owner_task=task,
                                 protected=_FakeBoard()._repo_protected("app"))
    assert made.branch == "cyc-1" and wp.current_branch(slot1.slot) == "cyc-1"
    # 추가 대여용 idle 슬롯 seed — 명의 없는 생성은 곧바로 무소유 idle 이다.
    seed = wp.create_slot("app", base="trunk", init_submodules=False)
    assert seed.state == "idle"
    # alloc(owner_task) = 항상 신규 idle 대여(I3·멱등 폐기) — 같은 repo 2번째 슬롯을 task 가 보유.
    slot2 = wp.alloc("app", owner_task=task)
    assert slot1.slot == "work/app_1" and slot2.slot == "work/app_2"
    held = {l.slot for l in wp.slots_for_task(task)}
    assert held == {"work/app_1", "work/app_2"}, f"집합 성장 실패(I1) — 보유={held}"

    # 도착 스냅 기록(부트스트랩 bind 도착 시점 모사) — 이후 변경 판정/0단계의 baseline.
    wp.record_git_snapshot("work/app_1")
    wp.record_git_snapshot("work/app_2")

    # 성장한 집합이 재부트스트랩에서 전수 열거된다("보유 2 — 전수 검증"·전부 ✓·record↔live match).
    # ①이 연 기록이 아직 열려 있다(핸드오프 전) — 이어받기는 사용자 응답(`--force`)이다.
    wp.record_task_session_claim(task, "s-e2e")
    rc = _make_bootstrap(bootstrap_mod, wp, tmp_path).run(task=task, force=True)
    assert rc == 0
    out2 = capsys.readouterr().out
    assert "보유 2 — 전수 검증" in out2, "성장한 집합(2슬롯)이 열거되지 않음(I2)"
    assert "work/app_1" in out2 and "work/app_2" in out2
    assert "기록↔live ✓" in out2, "도착 스냅 직후 record↔live match 여야 함(0단계 green)"

    # ── ③ 작업(app_1 커밋·app_2 무변경) 후 핸드오프 ──────────────────────────────
    slot1_dir = wp.slot_path("work/app_1")
    (slot1_dir / "work.txt").write_text("session work\n", encoding="utf-8")
    _git(slot1_dir, "add", "work.txt")
    _git(slot1_dir, "commit", "-q", "-m", "session commit on cyc-1")
    head_after = _git(slot1_dir, "rev-parse", "HEAD").stdout.strip()

    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_TRIGGER_PLAYBOOK, encoding="utf-8")
    log_file = tmp_path / "hf_current.md"
    dashboard_file = tmp_path / "dashboard.md"
    handoff = None
    regressed_slots: list[str] = []

    def _fake_pytest():
        # `_run_regression_for_slot` 이 self._regression_slot 을 그 슬롯으로 세팅 후 호출 —
        # 어느 슬롯이 회귀 대상인지 관찰(변경 슬롯만 회귀 검증).
        regressed_slots.append(handoff._regression_slot)
        return 0, "1 passed in 0.01s\n"

    handoff = handoff_mod.PmHandoff(
        run_pytest_fn=_fake_pytest,
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        dashboard_file=dashboard_file,
        worktree_pool=wp,
        # pm_state_file 미주입 — task 모드(task_mode=True)가 per-task pm_state 를 자체 해소.
    )
    rc = handoff.run(task, dry_run=False, user_ack="cyc", skip_pytest=False)
    assert rc == 0, "핸드오프 실패"
    out3 = capsys.readouterr().out

    # (a) 변경 슬롯만 회귀 — app_1(커밋 전진)만 회귀, app_2(무변경)는 skip.
    assert regressed_slots == ["work/app_1"], (
        f"변경 슬롯만 회귀 위반(ⓑB) — 회귀 슬롯={regressed_slots} (app_1 만이어야)"
    )
    assert "work/app_2" in out3 and "변경 흔적 없음" in out3, "무변경 슬롯 skip 사유 surface 없음"

    # (b) 전 슬롯 재스냅 — app_1 lease.git 이 커밋 후 head 로 갱신(집합 전체 두고 간다).
    leases = {l.slot: l for l in wp.list_leases()}
    assert leases["work/app_1"].git["head"] == head_after, "app_1 재스냅 미갱신(퇴장 재스냅 누락)"
    assert leases["work/app_2"].git is not None, "app_2 도 재스냅 대상(전 슬롯)"

    # (c) 인계 트리거 = `--task` 앵커(T-0394) — 슬롯 열거 없이 task-only.
    expected_trigger = handoff_mod._runtime_skill_entry("pm-bootstrap") + f" --task {task}"
    assert expected_trigger in out3, "인계 트리거에 --task 앵커 없음"

    # (d) 정상 종료 기록(닫힘) — 차기 재개가 묻지 않고 이어지도록.
    task_rec = next(t for t in wp.list_tasks() if t.name == task)
    assert task_rec.open is False, "정상 종료 기록 미기록(다음 진입이 거부된다)"

    # ── ④ 트리거 그대로 재부트스트랩 — clean resume·집합 재수령·0단계 green ─────────
    wp.record_task_session_claim(task, "s-e2e")
    rc = _make_bootstrap(bootstrap_mod, wp, tmp_path).run(task=task)
    assert rc == 0, "재개 부트스트랩 실패(사이클 단절)"
    out4 = capsys.readouterr().out
    # 집합 재수령(보유 2 전수 열거·재스냅으로 diverged 오탐 없이 0단계 green).
    assert "보유 2 — 전수 검증" in out4, "재개 시 집합 재수령 실패(I2)"
    assert "기록↔live ✓" in out4, "재스냅 후 0단계 record↔live match 아님(PM 78 오탐 재발)"
    assert "diverged" not in out4, "외부-개입 오탐 재발(재스냅 실패)"
    # 닫힌 기록 재개 — 묻지 않고 이어진다.
    assert "재개(resume)" in out4, "clean resume 아님(닫힌 기록 재개 미표기)"
    assert "회수" not in out4, "정상 인계인데 crash 회수 경고 오탐(T-0392)"
