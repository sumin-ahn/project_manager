"""`pm-config status --json [--with-board] [--with-adapters]` — 관리자 읽기 계약 (T-0907).

판정은 **실물**로 한다: tmp PM 홈에 실 `worktree-leases.json`·실 `local.conf`·실 `install.json`·
실 `adapter_baseline.json`·실 git 저장소와 실 git worktree 슬롯을 만들고, 그 홈의 엔진 사본을
서브프로세스로 돌려 stdout 을 `json.loads` 로 파싱해 필드별로 단언한다. 어댑터 기능 표는 이
저장소의 **실 `templates/<harness>/`** 를 `--from` 소스로 읽어 실 파일끼리 대조한다(수기 dict·
가짜 runner 로 흉내 낸 판정은 근거로 쓰지 않는다).

커버:
  - 가벼운 절 전수(schema·engine_rev·pm_home·generated_at·slots·tasks·git·repos·harnesses·conf).
  - `alive` 세 형태 — 열린 기록(leased+session)·닫힌 기록(idle)·readonly(세션 없음). 값은
    `worktree_pool.lease_is_open` 과 같아야 한다(판정 재구현 0·프로세스 조회 0회).
  - `role` 정규화(장부에 키가 없는 행 → `work` · readonly 행 → `readonly`)와 task 귀속.
  - `conf` 다섯 목록(필수 누락·폐지·미지·표준과 다름·최신 기준 누락 — 마지막은 소스 레지스트리).
  - `--with-board`: `STATUS_DIRS` 다섯 축 counts(초안 미집계)·open 목록·lint 목록(이슈가 있어도
    JSON 산출 계속)·회귀/라이브게이트 sidecar 그대로.
  - `--with-adapters`: 파일별 `mode`/`status` 가 엔진 어휘 그대로(네 상태 각 1)·문서 파일은
    검사 없음 한 줄·기능 표(없음/다름/폐지됨)와 `distance` 정의·기준/최신 diff 둘.
  - 소스 해소: `--from` 없이 local.conf `upstream.path=file://…` 이 T-0908 캐시로 해소되고
    그 결과가 `--from <캐시>` 와 같은 JSON 이다(네트워크 0). 미해소는 rc≠0 이고 stdout 에
    부분 JSON 이 0바이트.
  - JSON 경로가 `.project_manager/` 아래에 아무것도 쓰지 않는다(트리 해시 전후 동일).
  - 텍스트 경로 바이트 불변(골든) · 기본 호출이 board 를 로드하지 않음 · `whoami --json` 없음.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _git_fixture import remove_git_tree
from pm_engine.domain import conf_schema
# T-0908 의 URL 캐시 픽스처(로컬 bare + `file://` · 네트워크 0)를 그대로 쓴다 — 캐시 자리·
# 형상 판정을 두 벌로 만들지 않는다.
from test_pm_update_url_cache import (  # noqa: E402
    _make_bare, _make_framework_repo, _only_cache,
)

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATES = REPO / "templates"

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")


# 홈 사본 엔진으로 task 를 만들고 그 명의로 슬롯을 빌린다(`pm-config alloc` 과 같은 엔진 호출) —
# 열림 기록을 테스트가 조립하지 않고 엔진이 남긴다.
_ALLOC_SOURCE = '''\
import importlib.util, json, sys

spec = importlib.util.spec_from_file_location("worktree_pool_session_alloc", sys.argv[1])
worktree_pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worktree_pool)
task, _action = worktree_pool.bind_task("main", session="s-test")
lease = worktree_pool.alloc("app", owner_task="main")
print(json.dumps({"task_open": task.open, "lease_open": worktree_pool.lease_is_open(lease),
                  "slot": lease.slot}))
'''


# ── 모듈 로드 ────────────────────────────────────────────────────────────────


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pc():
    return _load("pm_config_status_json", "pm_config.py")


@pytest.fixture(scope="module")
def wp():
    return _load("worktree_pool_status_json", "worktree_pool.py")


# ── 실 git 헬퍼 (test_task_cycle_e2e 동형) ────────────────────────────────────


def _git(cwd, *argv):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
    })
    return subprocess.run([_GIT, "-C", str(cwd), *argv], check=True,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


# ── tmp PM 홈 (실 엔진 사본 + 실 장부) ────────────────────────────────────────


def _engine_home(root: Path) -> Path:
    """엔진 사본을 담은 tmp PM 홈 — 실 CLI 를 이 홈에서 돌린다(REPO 앵커 = 이 홈).

    홈을 **자기 git 저장소로 선언**한다(`tests/_git_fixture` 규약). pytest 임시 루트가 이
    저장소 work tree 안이라, 선언이 없으면 홈 안에서 도는 git 질문(`git config core.hooksPath`·
    `rev-parse`)이 이 저장소의 답을 받아 실 PM 홈의 sidecar 를 읽는다(실측: livegate 값이
    픽스처가 아니라 실 홈 것으로 나왔다).
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    shutil.copytree(TOOLS, root / ".project_manager" / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    if _GIT is not None:
        _git(root, "init", "-q", "-b", "main")
        _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(path: Path, document) -> Path:
    return _write(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _ledger(home: Path, leases: list[dict], tasks: list[dict] | None = None) -> Path:
    return _write_json(home / ".project_manager" / ".local" / "worktree-leases.json",
                       {"leases": leases, "tasks": tasks or []})


def _run_cli(home: Path, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_config.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(home), timeout=300, env=env,
    )


def _status_json(home: Path, *argv: str) -> dict:
    """`status --json …` 을 실 서브프로세스로 돌리고 stdout 을 파싱한다(rc 0 강제)."""
    done = _run_cli(home, "status", "--json", *argv)
    assert done.returncode == 0, f"rc={done.returncode}\nout={done.stdout}\nerr={done.stderr}"
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"기계 출력은 한 줄이어야 한다: {done.stdout!r}"
    return json.loads(lines[0])


def _origin_bare(home: Path, repo: str) -> Path:
    """홈 안 로컬 origin(기본 브랜치 trunk)과 그 bare 사본 `.repos/<repo>.git` — 이미 있으면 그대로 쓴다."""
    origin = home / ".origin"
    if not origin.exists():
        origin.mkdir(parents=True)
        _git(origin, "init", "-q", "-b", "trunk")
        _write(origin / "README.md", "seed\n")
        _git(origin, "add", "README.md")
        _git(origin, "commit", "-q", "-m", "init")
    bare = home / ".repos" / f"{repo}.git"
    if not bare.exists():
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git(home, "clone", "--bare", "-q", str(origin), str(bare))
    return bare


def _make_slot(home: Path, repo: str, number: int, branch: str) -> str:
    """실 bare + 실 `git worktree add` 로 만든 슬롯 — 슬롯 git 요약이 실물에서 나온다."""
    bare = _origin_bare(home, repo)
    slot = f"work/{repo}_{number}"
    _git(bare, "worktree", "add", "-q", "-b", branch, str(home / slot))
    return slot


@pytest.fixture
def home(tmp_path):
    return _engine_home(tmp_path / "home")


# ── ① 가벼운 절 — 실 장부·실 git 슬롯 ────────────────────────────────────────


@_git_required
def test_status_json_light_sections_from_real_ledger(home, pc, wp):
    """가벼운 절 전수 — 실 장부/실 슬롯에서 나온 값이 필드마다 실린다."""
    slot = _make_slot(home, "app", 1, "feat/x")
    _write(home / ".project_manager" / "local.conf",
           "project.name=demo\ntest.cmd=pytest -q\nruntime.py=python3\n")
    _write(home / ".project_manager" / "areas.md",
           "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
           "|---|---|---|---|---|---|---|---|\n"
           "| app |  | git@example.invalid:app.git | pytest -q | me | trunk | main | me |\n")
    _write_json(home / ".project_manager" / "install.json",
                {"harnesses": ["claude"], "schema": 2})
    _write(home / "CLAUDE.md", "# demo\n")
    (home / ".claude").mkdir(exist_ok=True)
    _ledger(home, [{
        "slot": slot, "repo": "app", "session": "main", "started": "2026-09-07T00:00:00+00:00", "state": "leased",
        "test_cmd": "pytest -q",
    }], [{"name": "main", "prefix": "app", "session": "s-test",
          "started": "2026-09-07T00:00:00+00:00"}])

    document = _status_json(home)

    assert document["schema"] == pc.STATUS_JSON_SCHEMA == 1
    assert document["engine_rev"] == pc.ENGINE_REV
    assert document["pm_home"] == str(home.resolve())
    assert document["generated_at"].endswith("+00:00")
    assert document["board"] is None and document["adapters"] is None

    assert [row["slot"] for row in document["slots"]] == [slot]
    row = document["slots"][0]
    assert row["repo"] == "app" and row["state"] == "leased" and row["session"] == "main"
    assert row["alive"] is True and "pid" not in row
    assert row["role"] == "work" and row["task"] == "main"
    assert row["branch"] == "feat/x"
    assert row["head"] == _git(home / slot, "rev-parse", "HEAD").stdout.strip()
    # 손 git 으로 만든 슬롯 브랜치라 부모 기록이 없다 — 지어내지 않고 null 이다.
    assert row["parent"] is None and row["children"] == [] and row["stashes"] == 0
    assert row["dirty"] is False

    assert document["tasks"] == [{
        "name": "main", "prefix": "app", "alive": True,
        "started": "2026-09-07T00:00:00+00:00", "slots": [slot], "todos": [],
    }]
    assert document["harnesses"] == ["claude"]
    assert document["repos"] == [{
        "repo": "app", "prefix": None, "git": "git@example.invalid:app.git", "base": "trunk",
        "protected": "main", "test_cmd": "pytest -q", "area_owner": "me",
    }]
    assert row["remote"] is None                  # 네트워크 절은 `--with-remote` 로 부를 때만


_AREAS_TABLE = (
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| app |  | git@example.invalid:app.git |  | me | trunk |  | me |\n")


def test_status_json_repos_reads_the_registry_inside_a_separate_board(home):
    """보드가 별도 저장소인 홈은 레지스트리가 `board/areas.md` 다 — 고정 경로로 읽으면 빈 목록이었다."""
    (home / ".project_manager" / "board" / "tickets").mkdir(parents=True)
    _write(home / ".project_manager" / "board" / "areas.md", _AREAS_TABLE)
    _ledger(home, [])

    repos = _status_json(home)["repos"]

    assert [(row["repo"], row["git"]) for row in repos] == [("app", "git@example.invalid:app.git")]


@_git_required
def test_status_json_with_remote_reports_origin_url_without_a_tracked_branch(home):
    """`--with-remote` — 원격 브랜치에 연결하지 않은 슬롯은 origin 주소만 싣고 `upstream` 은 null 이다."""
    slot = _make_slot(home, "app", 1, "feat/x")
    _ledger(home, [{"slot": slot, "repo": "app", "session": "", "started": "s",
                    "state": "idle", "test_cmd": None}])

    remote = _status_json(home, "--with-remote")["slots"][0]["remote"]

    assert remote == {"url": str(home / ".origin"), "upstream": None}


@_git_required
def test_status_json_with_remote_reports_the_tracked_remote_branch(home):
    """`--with-remote` — 슬롯 브랜치가 연결된 원격 브랜치의 이름·끝 커밋·앞/뒤를 받아 와 싣는다.

    같은 repo 의 두 슬롯 — 원격에 있는 브랜치에 연결(값 전부), 원격에서 지워진 브랜치에 연결
    (이름만·나머지 null). 브랜치 없이 커밋에 멈춘 슬롯은 연결 자체가 없어 null 이다.
    """
    tracked = _make_slot(home, "app", 1, "feat/x")
    gone = _make_slot(home, "app", 2, "feat/gone")
    detached = _make_slot(home, "app", 3, "feat/detached")
    _ledger(home, [{"slot": slot, "repo": "app", "session": "", "started": "s",
                    "state": "idle", "test_cmd": None} for slot in (tracked, gone, detached)])
    origin = home / ".origin"
    # `clone --bare` 는 원격 브랜치를 받는 자리를 설정하지 않는다 — 보통 clone 의 설정을 준다.
    _git(home / ".repos" / "app.git", "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(origin, "branch", "feat/x")
    _git(origin, "branch", "feat/gone")
    for slot, branch in ((tracked, "feat/x"), (gone, "feat/gone")):
        _git(home / slot, "config", f"branch.{branch}.remote", "origin")
        _git(home / slot, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    _git(origin, "branch", "-D", "feat/gone")
    _git(origin, "checkout", "-q", "feat/x")
    _write(origin / "UPSTREAM.md", "upstream\n")
    _git(origin, "add", "UPSTREAM.md")
    _git(origin, "commit", "-q", "-m", "upstream")          # 연결된 원격 브랜치가 한 커밋 앞선다
    _git(origin, "checkout", "-q", "trunk")
    _git(home / detached, "checkout", "-q", "--detach")

    rows = {row["slot"]: row["remote"] for row in _status_json(home, "--with-remote")["slots"]}

    assert rows[tracked]["upstream"] == {
        "name": "origin/feat/x", "head": _git(origin, "rev-parse", "feat/x").stdout.strip(),
        "ahead": 0, "behind": 1,
    }
    assert rows[gone]["upstream"] == {"name": "origin/feat/gone", "head": None, "ahead": None,
                                      "behind": None}
    assert rows[detached]["upstream"] is None


@_git_required
def test_status_json_with_remote_stops_without_a_partial_document(home, wp):
    """원격에 닿지 못하면 빈 값으로 접지 않고 rc 1 · 표준 출력 없음이다."""
    slot = _make_slot(home, "app", 1, "feat/x")
    _ledger(home, [{"slot": slot, "repo": "app", "session": "", "started": "s",
                    "state": "idle", "test_cmd": None}])
    _git(home / ".repos" / "app.git", "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(home / slot, "config", "branch.feat/x.remote", "origin")
    _git(home / slot, "config", "branch.feat/x.merge", "refs/heads/feat/x")  # 원격에 물을 대상이 있다
    wp.fs.force_rmtree(home / ".origin")

    done = _run_cli(home, "status", "--json", "--with-remote")

    assert done.returncode == 1 and done.stdout.strip() == ""
    assert "원격 대조 실패" in done.stderr


@_git_required
def test_status_json_alive_is_the_open_record(home, wp):
    """`alive` 는 열림 기록이다 — 열림(leased+session)·닫힘(idle)·readonly(세션 없음) 셋."""
    slot_open = _make_slot(home, "app", 1, "live")
    slot_closed = _make_slot(home, "app", 2, "closed")
    slot_readonly = _make_slot(home, "app", 3, "readonly")
    _ledger(home, [
        {"slot": slot_open, "repo": "app", "session": "main", "started": "s", "state": "leased", "test_cmd": None},
        {"slot": slot_closed, "repo": "app", "session": "", "started": "s", "state": "idle", "test_cmd": None},
        {"slot": slot_readonly, "repo": "app", "session": "", "started": "s", "state": "leased", "test_cmd": None, "role": "readonly"},
    ])

    rows = {row["slot"]: row for row in _status_json(home)["slots"]}

    assert rows[slot_open]["alive"] is True
    assert rows[slot_closed]["alive"] is False
    assert rows[slot_readonly]["alive"] is False
    for slot, row in rows.items():
        assert "pid" not in row, slot                 # 옛 필드 제거
        lease = wp.Lease(slot=slot, repo="app", session=row["session"] or "",
                         started="s", state=row["state"])
        assert row["alive"] == wp.lease_is_open(lease), slot   # 판정 재구현 0


@_git_required
def test_status_json_alive_follows_the_records_a_real_session_left(home, wp, tmp_path):
    """실 서브프로세스가 남긴 기록을 그대로 읽는다 — 대여 뒤 열림, 닫은 뒤 닫힘.

    엔진 프로세스 pid 를 적던 시절엔 대여 명령이 끝나는 순간 죽어 slots/tasks 가 늘 회수 대기로
    보였다(관리자 현황 탭). 이제 판정 재료는 프로세스가 아니라 기록이라, 명령이 끝난 뒤에도
    열림으로 남고 **닫는 명령**을 받아야 닫힌다. [[T-0960]] AT-006 — 지운 `bind_slot` 대신
    `pm-config alloc` 의 엔진 호출(`alloc(repo, owner_task=task)`)로 기록을 남긴다.
    """
    slot = _make_slot(home, "app", 1, "feat/x")
    # `worktree add` 가 남기는 idle 행 — alloc 은 장부의 idle 행에서만 빌린다.
    _ledger(home, [{"slot": slot, "repo": "app", "session": "", "started": "t", "state": "idle"}])
    template = home / ".project_manager" / "wiki" / "pm_state.template.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / ".project_manager" / "wiki" / "pm_state.template.md", template)
    allocator = tmp_path / "alloc.py"
    allocator.write_text(_ALLOC_SOURCE, encoding="utf-8")
    tools = home / ".project_manager" / "tools" / "worktree_pool.py"

    done = subprocess.run([sys.executable, str(allocator), str(tools)],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(home))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"task_open": True, "lease_open": True, "slot": slot}

    live = _status_json(home)
    assert live["slots"][0]["alive"] is True          # 명령 프로세스는 이미 죽었다
    assert live["tasks"][0]["alive"] is True
    assert live["slots"][0]["task"] == "main"

    assert _run_cli(home, "release", slot, "--force").returncode == 0
    assert _run_cli(home, "task", "close", "main").returncode == 0

    closed = _status_json(home)
    assert closed["slots"][0]["alive"] is False       # 종료 기록
    assert closed["tasks"][0]["alive"] is False


@_git_required
def test_status_json_normalizes_role_and_task_attribution(home):
    """장부에 `role` 키가 없는 행은 `work` · readonly 는 그대로 · task 귀속은 session 축."""
    work_slot = _make_slot(home, "app", 1, "w")
    readonly_slot = _make_slot(home, "app", 2, "r")
    _ledger(home, [
        {"slot": work_slot, "repo": "app", "session": "solo", "started": "s", "state": "idle", "test_cmd": None},
        {"slot": readonly_slot, "repo": "app", "session": "", "started": "s", "state": "leased", "test_cmd": None, "role": "readonly"},
    ], [{"name": "other", "prefix": None, "started": "s"}])

    rows = {row["slot"]: row for row in _status_json(home)["slots"]}

    assert rows[work_slot]["role"] == "work"      # 장부 키 부재 → 기본값 정규화
    assert rows[readonly_slot]["role"] == "readonly"
    assert rows[work_slot]["task"] is None        # session 이 task 명이 아니다
    assert rows[readonly_slot]["session"] is None


@_git_required
def test_status_json_git_section_tracks_pm_home_checkout(home):
    """`git` 절은 PM 홈 checkout 자신 — clean 이면 dirty False, 파일 하나 놓으면 True."""
    _ledger(home, [])
    _write(home / ".gitignore", ".project_manager/\nwork/\n.repos/\n.origin/\n")
    _git(home, "add", "-A")
    _git(home, "commit", "-q", "-m", "tree")

    clean = _status_json(home)["git"]
    assert clean["branch"] == "main"
    assert clean["head"] == _git(home, "rev-parse", "HEAD").stdout.strip()
    assert clean["dirty"] is False

    _write(home / "README.md", "dirty\n")
    assert _status_json(home)["git"]["dirty"] is True


@_git_required
def test_status_json_git_section_is_null_outside_git(home):
    """git checkout 이 아니면 세 값 모두 null — 그 사실이 곧 신호다(추측 금지).

    상위 저장소로 탐색이 새면 남의 브랜치를 이 사본의 것으로 보고하게 되므로, `.git` 을 지운
    홈에 경계 env 를 주어 "조상도 없다" 형상을 만든다.
    """
    _ledger(home, [])
    remove_git_tree(home / ".git")
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(home.parent))

    done = _run_cli(home, "status", "--json", env=env)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["git"] == {"branch": None, "head": None, "dirty": None}


@_git_required
def test_status_json_git_section_handles_zero_commit_clone(home):
    """커밋 0 저장소(fresh clone 직후)는 브랜치만 있고 head 는 null — 정상 형상이다."""
    _ledger(home, [])
    remove_git_tree(home / ".git")
    _git(home, "init", "-q", "-b", "main")

    section = _status_json(home)["git"]

    assert section["branch"] == "main" and section["head"] is None
    assert section["dirty"] is True


@_git_required
def test_status_json_git_section_reports_detached_head_as_null_branch(home):
    """detached 는 `slots[].branch` 와 **같은 null** 이다 — 한 문서 안에서 표현이 갈리지 않는다.

    `rev-parse --abbrev-ref HEAD` 는 이 상태를 브랜치 이름처럼 보이는 문자열 `"HEAD"` 로 줘서,
    읽는 쪽이 detached 와 'HEAD 라는 이름의 브랜치' 를 구분할 수 없게 만든다."""
    slot = _make_slot(home, "app", 1, "feat/x")
    _git(home / slot, "checkout", "-q", "--detach")
    _ledger(home, [{"slot": slot, "repo": "app", "session": "main", "started": "s", "state": "leased", "test_cmd": None}])
    _write(home / ".gitignore", ".project_manager/\nwork/\n.repos/\n.origin/\n")
    _git(home, "add", "-A")
    _git(home, "commit", "-q", "-m", "tree")
    head = _git(home, "rev-parse", "HEAD").stdout.strip()
    _git(home, "checkout", "-q", "--detach")

    document = _status_json(home)

    assert document["git"] == {"branch": None, "head": head, "dirty": False}
    assert document["slots"][0]["branch"] is None, "두 축의 detached 표현이 갈렸다"


@_git_required
def test_status_json_git_section_is_not_shadowed_by_a_tag_of_the_same_name(home):
    """브랜치명과 동명의 태그가 있어도 순수 브랜치명이다 — `heads/<이름>` 으로 실리지 않는다.

    릴리즈가 `v1.7.13` 브랜치를 같은 이름의 태그로 찍는 이 저장소의 실제 규약이다."""
    _ledger(home, [])
    _git(home, "checkout", "-q", "-b", "v1.7.13")
    _git(home, "tag", "v1.7.13")

    assert _status_json(home)["git"]["branch"] == "v1.7.13"


# ── ② conf 다섯 목록 ─────────────────────────────────────────────────────────


@_git_required
def test_status_json_conf_five_lists_from_real_local_conf(home, pc):
    """필수 누락·폐지 키·미지 키·표준과 다른 값 — 실 local.conf 를 읽어 판정한다."""
    local_conf = conf_schema
    required = local_conf.required_keys()
    assert required, "픽스처 전제: 레지스트리에 필수 키가 있어야 한다"
    legacy_key = next(iter(local_conf.LEGACY_KEY_MAP))
    standard_key, standard_spec = next(
        (key, spec) for key, spec in local_conf.KEY_REGISTRY.items()
        if spec.default is not None)
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf",
           f"{legacy_key}=x\n"
           f"{standard_key}={standard_spec.default}9\n"
           "no.such.key=1\n")

    conf = _status_json(home)["conf"]

    assert set(conf["missing_required"]) == set(required)
    assert {"key": legacy_key,
            "replacement": local_conf.LEGACY_KEY_MAP[legacy_key]} in conf["legacy"]
    assert "no.such.key" in conf["unknown"]
    assert {"key": standard_key, "value": f"{standard_spec.default}9",
            "default": standard_spec.default} in conf["nonstandard"]
    assert conf["missing_for_latest"] is None, "소스가 없으면 최신 기준 판정도 없다"


@_git_required
def test_status_json_missing_for_latest_reads_source_registry(home, tmp_path):
    """`--from` 소스의 레지스트리가 요구하는 필수 키 — "갱신하면 필요" 축."""
    source = _engine_home(tmp_path / "source")
    source_conf = source / ".project_manager" / "tools" / "pm_engine" / "domain" / "conf_schema.py"
    source_conf.write_text(
        source_conf.read_text(encoding="utf-8")
        + '\nKEY_REGISTRY["future.knob"] = KeySpec(\n'
          '    "다음 세대 키", "다음 세대가 요구하는 값",\n'
          '    default=None, required=True, group=KEY_GROUPS[0],\n'
          '    unset="다음 세대 없음")\n',
        encoding="utf-8")
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf", "project.name=demo\n")

    conf = _status_json(home, "--from", str(source))["conf"]

    assert "future.knob" in conf["missing_for_latest"]
    assert "future.knob" not in conf["missing_required"], "이 사본의 선언은 그대로다"


# ── ③ --with-board ───────────────────────────────────────────────────────────


def _ticket(home: Path, status: str, ticket_id: str, title: str, **fields) -> Path:
    front = {
        "id": ticket_id, "title": title, "status": status, "created": "2026-09-07",
        "created_by": "me", "claimed_by": None, "claimed_at": None,
        "completed_at": None, "depends_on": [], "blocks": [], "touches": [],
        "estimate": "small", **fields,
    }
    lines = ["---"]
    for key, value in front.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines += ["---", "", f"# {ticket_id} — {title}", "", "## 목표", "본문", ""]
    return _write(
        home / ".project_manager" / "wiki" / "tickets" / status / f"{ticket_id}.md",
        "\n".join(lines))


@_git_required
def test_with_board_counts_open_lint_and_gate_records(home):
    """counts 다섯 축·open 목록·lint 목록·회귀/라이브게이트 sidecar — 실 파일에서."""
    _ledger(home, [])
    _ticket(home, "open", "T-0001", "열린 것")
    _ticket(home, "done", "T-0002", "끝난 것", completed_at="2026-09-07")
    _ticket(home, "discarded", "T-0003", "처분된 것", disposition="dropped")
    _write(home / ".project_manager" / "wiki" / "tickets" / ".drafts" / "T-0004.md",
           "---\nid: T-0004\n---\n\n# 초안\n")
    local = home / ".project_manager" / ".local"
    _write_json(local / "regression.json",
                {"head": "abc123", "status": "pass", "rc": 0, "collected": 12345})
    _write_json(local / "livegate.json", {"head": "abc123", "status": "pass", "n": 18})

    board = _status_json(home, "--with-board")["board"]

    assert board["counts"] == {"open": 1, "claimed": 0, "blocked": 0,
                               "done": 1, "discarded": 1}, "초안은 세지 않는다"
    assert board["open"] == [{"id": "T-0001", "title": "열린 것", "claimed_by": None}]
    assert board["regression"]["collected"] == 12345
    assert board["livegate"]["n"] == 18
    assert isinstance(board["lint"], list)
    assert all({"kind", "message"} == set(row) for row in board["lint"])


@_git_required
def test_with_board_keeps_emitting_json_when_lint_has_issues(home):
    """lint 이슈(=`board lint` rc 1)가 있어도 JSON 산출은 계속된다 — 목록으로만 실린다."""
    _ledger(home, [])
    _ticket(home, "open", "T-0010", "의존성 깨진 것", depends_on=["T-9999"])

    board = _status_json(home, "--with-board")["board"]

    assert any(row["kind"] == "unknown" and "T-9999" in row["message"]
               for row in board["lint"]), f"lint 판정이 실려야 한다: {board['lint']}"
    assert board["counts"]["open"] == 1


def test_default_call_does_not_load_board_engine(pc, monkeypatch, tmp_path, capsys):
    """기본 호출은 board 를 **로드하지 않는다**(티켓 전수 스캔 비용을 지지 않는다)."""
    loaded: list[str] = []
    real_load = pc._load_module

    def spy(name, filename):
        loaded.append(name)
        return real_load(name, filename)

    monkeypatch.setattr(pc, "_load_module", spy)
    monkeypatch.setattr(pc, "_write_machine_line", lambda text: None)

    class _Wp:
        def list_leases(self):
            return []

        def list_tasks(self):
            return []

        def read_task_todos(self, name):
            return []

        def preserved_branches(self):
            return []

        def _pid_alive(self, pid):
            return False

    monkeypatch.setattr(pc, "_status_json_git",
                        lambda runner: {"branch": None, "head": None, "dirty": None})
    args = pc.build_parser().parse_args(["status", "--json"])
    assert pc.cmd_status(args, worktree_pool=_Wp()) == 0
    assert "board" not in loaded, f"기본 호출이 board 를 로드했다: {loaded}"


# ── ④ --with-adapters (실 템플릿 대조) ───────────────────────────────────────


def _adapter_home(root: Path) -> Path:
    """세 하네스 어댑터가 설치된 tmp 인스턴스 — config 4파일이 판정 대상이 된다."""
    home = _engine_home(root)
    _ledger(home, [])
    _write(home / "CLAUDE.md", "# 인스턴스 소유 문서\n")
    _write(home / "AGENTS.md", "# 인스턴스 소유 문서\n")
    (home / ".agents").mkdir(exist_ok=True)
    for relpath in (".claude/settings.json", ".opencode/opencode.jsonc",
                    ".codex/config.toml", ".codex/hooks.json"):
        source = TEMPLATES / _TEMPLATE_DIR[relpath] / relpath
        _write(home / relpath, source.read_text(encoding="utf-8"))
    _write_json(home / ".project_manager" / "install.json", {
        "harnesses": ["claude", "opencode", "codex"],
        "schema": 2,
        "instance_owned_templates": {
            relpath: {"source": f"templates/{_TEMPLATE_DIR[relpath]}/{relpath}",
                      "weight": "full"}
            for relpath in _TEMPLATE_DIR
        },
    })
    return home


_TEMPLATE_DIR = {
    ".claude/settings.json": "claude_code",
    ".opencode/opencode.jsonc": "opencode",
    ".codex/config.toml": "codex",
    ".codex/hooks.json": "codex",
    "CLAUDE.md": "claude_code",
    "AGENTS.md": "codex",
}


def _content_sha256(path: Path) -> str:
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@_git_required
def test_with_adapters_reports_engine_status_vocabulary(tmp_path):
    """네 상태(in-sync·unedited·edited·unrecorded)가 엔진 어휘 그대로 실린다 — 실 파일 대조."""
    home = _adapter_home(tmp_path / "home")
    head = _git(REPO, "rev-parse", "HEAD").stdout.strip()
    # in-sync = 템플릿과 같은 내용(`.codex/hooks.json` 은 그대로 둔다).
    # unedited = 내용이 template 과 다르고 원장이 **지금 dest** 를 기록 → 상류가 바뀐 것.
    _write(home / ".codex/config.toml", "sandbox_mode = \"workspace-write\"\n")
    # edited = 원장이 다른 내용을 기록 → 채택자 편집(보존).
    _write(home / ".opencode/opencode.jsonc", "{\n  // 손편집\n  \"instructions\": []\n}\n")
    # unrecorded = 원장 항목 없음(.claude/settings.json).
    settings = home / ".claude/settings.json"
    _write(settings, settings.read_text(encoding="utf-8").replace(
        '"autoCompactEnabled": true', '"autoCompactEnabled": false'))
    _write_json(home / ".project_manager" / "adapter_baseline.json", {
        "schema": 1,
        "files": {
            ".codex/hooks.json": {"sha256": _content_sha256(home / ".codex/hooks.json"),
                                  "template_rev": head, "recorded_at": "2026-09-07"},
            ".codex/config.toml": {"sha256": _content_sha256(home / ".codex/config.toml"),
                                   "template_rev": head, "recorded_at": "2026-09-07"},
            ".opencode/opencode.jsonc": {"sha256": "0" * 64, "template_rev": head,
                                         "recorded_at": "2026-09-07"},
        },
    })

    adapters = _status_json(home, "--with-adapters", "--from", str(REPO))["adapters"]

    assert adapters["source"] == str(REPO.resolve())
    rows = {row["path"]: row for row in adapters["files"]}
    assert rows[".codex/hooks.json"]["status"] == "in-sync"
    assert rows[".codex/hooks.json"]["mode"] == "managed"
    assert rows[".codex/config.toml"]["status"] == "unedited"
    assert rows[".opencode/opencode.jsonc"]["status"] == "edited"
    assert rows[".claude/settings.json"]["status"] == "unrecorded"
    assert {row["mode"] for row in adapters["files"] if "mode" in row} <= {
        "managed", "report"}
    # 문서 파일은 채널이 없다 — 검사 없음이라는 사실만 한 줄로.
    assert rows["CLAUDE.md"] == {"path": "CLAUDE.md", "ownership": "instance",
                                 "status": "unchecked"}
    assert rows["AGENTS.md"]["status"] == "unchecked"


@_git_required
def test_with_adapters_feature_table_and_distance(tmp_path):
    """기능 표 — 없음·다름·폐지됨 판정과 `distance` 정의(같음이 아닌 행 수)."""
    home = _adapter_home(tmp_path / "home")
    settings = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    settings.pop("autoCompactEnabled")                       # 없음(missing)
    settings["permissions"]["allow"] = ["Bash(ls:*)"]        # 다름(differs)
    settings["hooks"] = {"PostToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/run_tests_hook.sh"}]}]}
    _write_json(home / ".claude/settings.json", settings)

    adapters = _status_json(home, "--with-adapters", "--from", str(REPO))["adapters"]
    row = next(item for item in adapters["files"]
               if item["path"] == ".claude/settings.json")
    features = {feature["name"]: feature for feature in row["features"]}

    assert features["autoCompactEnabled"]["verdict"] == "missing"
    assert features["autoCompactEnabled"]["project"] is None
    assert features["autoCompactEnabled"]["latest"] is True
    assert features["permissions.allow"]["verdict"] == "differs"
    assert features["permissions.allow"]["project"] == 1
    assert features["permissions.allow"]["latest"] > 1
    assert features["hooks.PostToolUse"]["verdict"] == "retired"
    assert features["hooks.PostToolUse"]["project"] == "*:.claude/run_tests_hook.sh"
    assert features["hooks.PostToolUse"]["latest"] is None
    assert features["hooks.PreToolUse"]["verdict"] == "missing"
    assert row["distance"] == sum(1 for feature in row["features"]
                                  if feature["verdict"] != "same")
    assert row["distance"] >= 4


@_git_required
def test_with_adapters_keeps_a_deleted_managed_file_as_a_null_status_row(tmp_path):
    """관리 대상 config 를 지워도 행이 남는다 — `status=null`(파일 없음)이다.

    행이 사라지면 관리자 화면에서 "없는 파일" 과 "판정할 것이 없는 파일" 이 같은 그림이 된다
    (선언된 대상이 통째로 시야에서 빠진다)."""
    home = _adapter_home(tmp_path / "home")
    (home / ".codex/hooks.json").unlink()

    adapters = _status_json(home, "--with-adapters", "--from", str(REPO))["adapters"]

    row = next(item for item in adapters["files"] if item["path"] == ".codex/hooks.json")
    assert row["status"] is None
    assert "present" not in row, "같은 사실을 두 필드로 말한다"
    assert row["mode"] == "managed"
    assert row["harness"] == "codex"
    assert row["features"] == [] and row["distance"] == 0
    assert row["diff_vs_baseline"] is None and row["diff_vs_latest"] == ""
    assert row["diff_lines"] == {"added": None, "removed": None}
    # 남은 대상은 종전 판정 그대로다(부재 한 건이 표를 통째로 바꾸지 않는다).
    assert next(item for item in adapters["files"]
                if item["path"] == ".claude/settings.json")["status"] == "in-sync"


@_git_required
def test_with_adapters_diffs_against_baseline_rev_and_latest(tmp_path):
    """diff 둘 — 설치 기준(`template_rev` 시점)↔현재, 현재↔최신 template."""
    home = _adapter_home(tmp_path / "home")
    head = _git(REPO, "rev-parse", "HEAD").stdout.strip()
    _write_json(home / ".project_manager" / "adapter_baseline.json", {
        "schema": 1,
        "files": {".claude/settings.json": {"sha256": "0" * 64, "template_rev": head,
                                            "recorded_at": "2026-09-07"}},
    })
    settings = home / ".claude/settings.json"
    # 설치 기준은 `template_rev`(HEAD) 시점의 template 이다 — 그 blob 을 깐 뒤 한 줄만 고친다.
    #   작업 트리 template 을 깔면 커밋 전 template 변경까지 편집으로 세어진다.
    installed = _git(REPO, "show", f"{head}:templates/claude_code/.claude/settings.json").stdout
    _write(settings, installed.replace(
        '"autoCompactEnabled": true', '"autoCompactEnabled": false'))

    row = next(item for item in
               _status_json(home, "--with-adapters", "--from", str(REPO))["adapters"]["files"]
               if item["path"] == ".claude/settings.json")

    assert row["template_rev"] == head
    assert row["diff_lines"] == {"added": 1, "removed": 1}
    assert '-  "autoCompactEnabled": true' in row["diff_vs_baseline"]
    assert '+  "autoCompactEnabled": false' in row["diff_vs_baseline"]
    assert '+  "autoCompactEnabled": true' in row["diff_vs_latest"]


def _url_upstream_home(tmp_path: Path) -> Path:
    """URL upstream(로컬 bare + `file://`·네트워크 0)을 가리키는 codex 인스턴스 홈.

    T-0908 의 캐시 픽스처를 그대로 쓴다(캐시 자리·형상 판정을 두 벌로 만들지 않는다). 상류
    checkout 에는 최신 설정 레지스트리를 얹는다 — `conf.missing_for_latest` 의 비교 기준이라
    없으면 소스 레지스트리 로드가 그대로 터진다.
    """
    framework = _make_framework_repo(tmp_path / "fw")
    shutil.copyfile(TOOLS / "local_conf.py",
                    framework / ".project_manager" / "tools" / "local_conf.py")
    shutil.copytree(TOOLS / "pm_engine", framework / ".project_manager" / "tools" / "pm_engine",
                    ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    subprocess.run(["git", "-C", str(framework), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(framework), "commit", "-qm", "registry"],
                   check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.invalid",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.invalid"))
    url = _make_bare(framework, tmp_path / "fw.git")

    home = _engine_home(tmp_path / "home")
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf", f"upstream.path={url}\n")
    _write(home / ".codex" / "hooks.json",
           (framework / "templates" / "codex" / ".codex" / "hooks.json").read_text(
               encoding="utf-8"))
    _write(home / ".codex" / "config.toml",
           (framework / "templates" / "codex" / ".codex" / "config.toml").read_text(
               encoding="utf-8"))
    _write(home / "AGENTS.md", "# adopter\n")
    (home / ".agents").mkdir(exist_ok=True)
    _write_json(home / ".project_manager" / "install.json",
                {"schema": 2, "harnesses": ["codex"]})
    return home


@_git_required
def test_with_adapters_resolves_url_upstream_via_cache(tmp_path):
    """URL upstream 은 `--from` 없이 T-0908 캐시로 해소되고 결과가 `--from <캐시>` 와 같다.

    픽스처는 로컬 bare + `file://` 라 네트워크가 0 이다(T-0908 헬퍼 재사용). 캐시 checkout 이
    소스가 됐음을 `adapters.source` 절대경로로 단언하고, 같은 캐시를 `--from` 으로 준 산출과
    문서 전체를 대조한다(`generated_at` 만 시각이라 제외).
    """
    home = _url_upstream_home(tmp_path)

    cached = _status_json(home, "--with-adapters")
    cache = _only_cache(home)

    assert cached["adapters"]["source"] == str(cache.resolve())
    assert {row["path"] for row in cached["adapters"]["files"]} >= {
        ".codex/config.toml", ".codex/hooks.json"}
    assert cached["conf"]["missing_for_latest"] is not None, \
        "캐시가 최신 레지스트리 비교 기준으로도 쓰여야 한다"

    explicit = _status_json(home, "--with-adapters", "--from", str(cache))

    cached.pop("generated_at"), explicit.pop("generated_at")
    assert cached == explicit, "캐시 해소 산출이 `--from <캐시>` 와 다르다"


@_git_required
def test_with_adapters_source_unresolved_errors(home):
    """소스 미해소 — rc≠0 이고 stdout 에 부분 JSON 이 0바이트(AT-003)."""
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf", "project.name=demo\n")
    _write_json(home / ".project_manager" / "install.json",
                {"harnesses": ["claude"], "schema": 2})
    _write(home / "CLAUDE.md", "# demo\n")
    (home / ".claude").mkdir(exist_ok=True)

    done = _run_cli(home, "status", "--json", "--with-adapters")

    assert done.returncode != 0
    assert done.stdout == "", f"부분 산출이 있으면 안 된다: {done.stdout!r}"
    assert "어댑터 config 소스 미해소" in done.stderr or "[중단]" in done.stderr


# ── ⑤ 부작용 0 · 텍스트 불변 · 표면 상한 ─────────────────────────────────────


def _tree_digest(root: Path, *, exclude: Path | None = None) -> str:
    """`.project_manager/` 트리의 경로+내용 해시 — 생성·수정·삭제를 모두 잡는다.

    `exclude` 는 부분트리 하나를 해시에서 뺀다 — 읽기 전용 계약이 쓰기를 허용하는 유일한 자리
    (엔진 소유 스크래치 `.local/upstream-cache/`)를 **명시적 예외**로 선언하기 위한 축이다.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if exclude is not None and (path == exclude or exclude in path.parents):
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@_git_required
def test_json_path_writes_nothing(tmp_path):
    """JSON 경로가 `.project_manager/` 아래 어떤 파일도 만들지 않고 고치지 않는다(AT-004)."""
    home = _adapter_home(tmp_path / "home")
    slot = _make_slot(home, "app", 1, "feat")
    _ledger(home, [{"slot": slot, "repo": "app", "session": "main", "started": "s", "state": "leased", "test_cmd": None}])
    _ticket(home, "open", "T-0001", "열린 것")
    # 텍스트 경로를 먼저 한 번 돌려 공유 읽기 seam(리스 락 파일)을 만든 상태를 기준으로 삼는다 —
    # JSON 이 **텍스트 경로보다 더** 쓰지 않음을 재는 것이 이 단언의 축이다.
    assert _run_cli(home, "status").returncode == 0
    before = _tree_digest(home / ".project_manager")

    _status_json(home)
    _status_json(home, "--with-board")
    _status_json(home, "--with-adapters", "--from", str(REPO))

    assert _tree_digest(home / ".project_manager") == before, \
        "JSON 조회가 PM 홈 .project_manager/ 트리를 건드렸다"


@_git_required
def test_json_path_url_upstream_writes_only_the_engine_cache(tmp_path):
    """URL upstream 형상의 무쓰기 범위 — 예외는 엔진 소유 `.local/upstream-cache/` 하나다.

    `--from` 없이 URL upstream 을 쓰면 소스 해소가 그 캐시를 clone(최초)/fetch(이후)하므로 전체
    트리 해시로는 잴 수 없다. 허용된 쓰기 자리가 그 부분트리 하나뿐임을 값으로 고정한다 — 캐시를
    뺀 `.project_manager/` 는 전후 동일해야 하고, 캐시는 이 실행이 실제로 만들었어야 한다(예외가
    선언된 것임을 함께 못박는다·`--from` 형상의 전체 트리 무변경 단언은 그대로 둔다)."""
    home = _url_upstream_home(tmp_path)
    cache_root = home / ".project_manager" / ".local" / "upstream-cache"
    assert not cache_root.exists(), "픽스처 전제: 캐시가 아직 없어야 한다"
    # 텍스트 경로를 먼저 돌려 공유 읽기 seam(리스 락 파일)이 선 상태를 기준선으로 삼는다.
    assert _run_cli(home, "status").returncode == 0
    before = _tree_digest(home / ".project_manager", exclude=cache_root)

    _status_json(home, "--with-adapters")   # 최초 — clone
    _status_json(home, "--with-adapters")   # 이후 — fetch

    assert _tree_digest(home / ".project_manager", exclude=cache_root) == before, \
        "URL 형상의 JSON 조회가 캐시 밖 .project_manager/ 를 건드렸다"
    assert _only_cache(home).is_dir(), \
        "캐시가 만들어지지 않았다면 이 예외 선언은 공허하다(형상이 URL 로 해소되지 않았다)"


@_git_required
def test_text_path_output_is_byte_identical(home):
    """텍스트 경로 골든 — `--json` 추가가 사람 출력의 바이트를 바꾸지 않는다(불변식 1)."""
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf", "identity.user=me@example.invalid\n")

    done = _run_cli(home, "status")

    assert done.returncode == 0
    assert done.stdout == (
        "# pm-config status — 세션: (비바인딩)\n"
        "## 정체성(user): me@example.invalid\n"
        "## 세션격리(registry/area_owner 기준): single-user (단일/미등록 registry) — 단, 티켓 "
        "귀속(created_by/claimed_by)이 다중이면 세션 뷰가 strict-exclude 될 수 있다. 실 격리는 "
        "`board list --mine`(strict-exclude loud-warn)이 authoritative.\n"
        "## 이 세션의 리스: (없음)\n"
        "## task 상황 (사람이 명명한 작업스트림):\n"
        "  (task 없음 — 명명한 task 미등록·세션은 아래 풀 축에만 나타남)\n"
        "## slot 풀 (풀 전체 리스 장부 · 슬롯별 state·보유 task·role + git 요약):\n"
        "  (리스 없음 — 아직 worktree 슬롯이 생성되지 않음)\n"
    )


@_git_required
@pytest.mark.parametrize("flag", ["--with-board", "--with-adapters"])
def test_scope_flags_without_json_are_usage_error(home, flag):
    """`--with-*` 를 `--json` 없이 주면 usage error(rc 2) — 조용히 무시하지 않는다."""
    _ledger(home, [])
    done = _run_cli(home, "status", flag)
    assert done.returncode == 2
    assert flag in done.stderr and "--json" in done.stderr
    assert done.stdout == ""


@_git_required
def test_from_without_json_is_usage_error(home):
    """`--from` 도 `--json` 전용이다 — 텍스트 경로엔 그 소스를 쓰는 절이 없다."""
    _ledger(home, [])
    done = _run_cli(home, "status", "--from", str(REPO))
    assert done.returncode == 2
    assert "--from" in done.stderr


def test_whoami_has_no_json_flag(pc):
    """`--json` 은 `status` 전용 — `whoami --json` 은 파서가 거부한다(계약 하나)."""
    with pytest.raises(SystemExit) as excinfo:
        pc.build_parser().parse_args(["whoami", "--json"])
    assert excinfo.value.code == 2
    assert pc.build_parser().parse_args(["status", "--json"]).json is True


def test_status_json_schema_constant_is_single_source(pc):
    """스키마 번호는 상수 하나다 — 문서의 값이 그 상수와 같아야 한다."""
    assert pc.STATUS_JSON_SCHEMA == 1


# ── 슬롯 없는 보존 브랜치 — 홈 엔진이 슬롯을 만들고 지워 남긴 기록 ─────────────────


# 홈 사본 엔진으로 슬롯을 만들고 지운다 — 보존 기록을 테스트가 조립하지 않고 슬롯 제거가 남긴다.
_SLOT_LIFECYCLE_SOURCE = '''\
import importlib.util, sys

spec = importlib.util.spec_from_file_location("worktree_pool_preserved", sys.argv[1])
worktree_pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worktree_pool)
if sys.argv[2] == "create":
    print(worktree_pool.create_slot("app", base="trunk", init_submodules=False).slot)
else:
    print(worktree_pool.remove_slot(sys.argv[3]).branch_deletion.reason)
'''


def _slot_lifecycle(home: Path, script: Path, *argv: str) -> str:
    """`_SLOT_LIFECYCLE_SOURCE` 를 홈에서 돌리고 stdout 마지막 줄(슬롯 또는 판정 사유)을 돌려준다."""
    done = subprocess.run(
        [sys.executable, str(script), str(home / ".project_manager" / "tools" / "worktree_pool.py"),
         *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(home), timeout=300)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()[-1]


@pytest.fixture
def preserved_home(home, tmp_path):
    """홈 엔진으로 슬롯 둘을 만들고 지운 홈 — app_1 은 커밋 2개(합쳐지지 않음)·app_2 는 부모 기록 없음."""
    bare = _origin_bare(home, "app")
    _ledger(home, [])
    script = tmp_path / "slot_lifecycle.py"
    script.write_text(_SLOT_LIFECYCLE_SOURCE, encoding="utf-8")
    kept = _slot_lifecycle(home, script, "create")
    loose = _slot_lifecycle(home, script, "create")
    for name in ("one.txt", "two.txt"):
        _write(home / kept / name, f"{name}\n")
        _git(home / kept, "add", name)
        _git(home / kept, "commit", "-q", "-m", name)
    for key in ("pm-parent", "pm-fork"):
        _git(bare, "config", "--unset", f"branch.app_2.{key}")
    assert _slot_lifecycle(home, script, "remove", kept) == "unmerged"
    assert _slot_lifecycle(home, script, "remove", loose) == "no-parent"
    return home


@_git_required
def test_status_json_preserved_branches_from_engine_slot_removal(preserved_home):
    """`preserved_branches` — 슬롯 제거가 남긴 기록 그대로 · 합쳐지지 않은 커밋 수 · 부모 기록 없음은 null."""
    document = _status_json(preserved_home)

    assert document["slots"] == []
    rows = document["preserved_branches"]
    for row in rows:
        assert datetime.datetime.fromisoformat(row.pop("preserved_at")).utcoffset() == datetime.timedelta(0)
    assert rows == [
        {"repo": "app", "name": "app_1", "slot": "work/app_1", "reason": "unmerged",
         "parent": "trunk", "unmerged": 2},
        {"repo": "app", "name": "app_2", "slot": "work/app_2", "reason": "no-parent",
         "parent": None, "unmerged": None},
    ]


@_git_required
def test_status_text_lists_preserved_branches_section(preserved_home):
    """사람용 `status` — 보존 브랜치 절에 행마다 repo·이름·쓰던 슬롯·보존 시각·사유·부모·커밋 수."""
    stamps = {row["name"]: row["preserved_at"]
              for row in _status_json(preserved_home)["preserved_branches"]}

    done = _run_cli(preserved_home, "status")

    assert done.returncode == 0, done.stderr
    header = "## 슬롯 없는 보존 브랜치 (슬롯 제거 때 삭제 판정이 남긴 브랜치 · 정리는 사용자):\n"
    assert header in done.stdout
    assert done.stdout.split(header)[1].splitlines()[:2] == [
        f"  - app · app_1 · 쓰던 슬롯 work/app_1 · 보존 {stamps['app_1']} · 사유 unmerged · "
        "부모 trunk · 합쳐지지 않은 커밋 2개",
        f"  - app · app_2 · 쓰던 슬롯 work/app_2 · 보존 {stamps['app_2']} · 사유 no-parent · "
        "부모 기록 없음 · 합쳐짐 판정 불가",
    ]


# ── T-0961 슬롯 수에 비례하는 git 호출 상한 ─────────────────────────────────────

# `pm_config.py` 를 자식 프로세스에서 돌리며 띄운 git 프로세스의 argv 를 모은다 — `Popen` 생성을
# 감싸 실행 파일 이름이 `git` 으로 시작하는 호출만 남기고 rc 와 목록을 JSON 파일에 쓴다.
_GIT_COUNT_SOURCE = '''\
import json, os, runpy, subprocess, sys

out_path, script, *rest = sys.argv[1:]
calls = []
_original_init = subprocess.Popen.__init__


def _recording_init(self, args, *positional, **keywords):
    argv = [os.fspath(item) for item in args] if isinstance(args, (list, tuple)) else [str(args)]
    if os.path.basename(argv[0]).lower().startswith("git"):
        calls.append(argv[1:])
    return _original_init(self, args, *positional, **keywords)


subprocess.Popen.__init__ = _recording_init
sys.argv = [script, *rest]
rc = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump({"rc": rc, "calls": calls}, handle)
'''

_PARENT_RECORDS_READ = r"^branch\..*\.pm-(parent|fork)$"
_STASH_LIST_READ = ["stash", "list", "--format=%gs"]


def _status_git_calls(home: Path, tmp_path: Path, mode: str, tag: str) -> list[list[str]]:
    """`status`(text) 또는 `status --json`(json) 1회가 띄운 git 호출의 argv 목록(rc 0 강제)."""
    driver = tmp_path / "git_count.py"
    driver.write_text(_GIT_COUNT_SOURCE, encoding="utf-8")
    out = tmp_path / f"git-calls-{mode}-{tag}.json"
    argv = ["status", "--json"] if mode == "json" else ["status"]
    done = subprocess.run(
        [sys.executable, str(driver), str(out),
         str(home / ".project_manager" / "tools" / "pm_config.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(home),
        timeout=300)
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["rc"] == 0, done.stderr
    return record["calls"]


@_git_required
@pytest.mark.parametrize("mode, per_slot", [("json", 5), ("text", 6)])
def test_status_git_calls_per_slot_are_bounded(home, tmp_path, mode, per_slot):
    """슬롯 2개를 더하면 git 호출은 슬롯당 고정 수만큼만 는다 — `git submodule status` 0회, 부모 기록과
    stash 목록은 저장소 공용 값이라 조회 1회에 한 번씩만 읽는다(T-0961 · 엔진 create_slot 슬롯)."""
    _origin_bare(home, "app")
    _ledger(home, [])
    script = tmp_path / "slot_lifecycle.py"
    script.write_text(_SLOT_LIFECYCLE_SOURCE, encoding="utf-8")
    _slot_lifecycle(home, script, "create")
    one_slot = _status_git_calls(home, tmp_path, mode, "1")
    _slot_lifecycle(home, script, "create")
    _slot_lifecycle(home, script, "create")
    three_slots = _status_git_calls(home, tmp_path, mode, "3")

    assert len(three_slots) - len(one_slot) <= 2 * per_slot, three_slots
    for calls in (one_slot, three_slots):
        assert not any("submodule" in argv for argv in calls)
        assert sum(1 for argv in calls if _PARENT_RECORDS_READ in argv) == 1
        assert sum(1 for argv in calls if argv[-3:] == _STASH_LIST_READ) == 1


# 홈 사본 엔진으로 인자로 받은 저장소의 슬롯을 만든다(`_SLOT_LIFECYCLE_SOURCE` 의 create 와 같은 호출).
_CREATE_REPO_SLOT_SOURCE = '''\
import importlib.util, sys

spec = importlib.util.spec_from_file_location("worktree_pool_repo_slot", sys.argv[1])
worktree_pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worktree_pool)
print(worktree_pool.create_slot(sys.argv[2], base="trunk", init_submodules=False).slot)
'''


@_git_required
def test_status_repo_reads_stay_per_repo(home, tmp_path):
    """저장소 공용 값(부모 기록·stash 목록)은 저장소마다 따로 읽힌다 — 두 저장소 홈의 조회 1회에서 app 슬롯과
    lib 슬롯이 각자 저장소의 stash 수·자식 기록을 받고, 두 값은 저장소마다 한 번씩만 읽힌다(T-0961)."""
    for repo in ("app", "lib"):
        _origin_bare(home, repo)
    _ledger(home, [])
    script = tmp_path / "create_repo_slot.py"
    script.write_text(_CREATE_REPO_SLOT_SOURCE, encoding="utf-8")
    app_slot, lib_slot = (_slot_lifecycle(home, script, repo) for repo in ("app", "lib"))
    for slot, count in ((app_slot, 1), (lib_slot, 2)):
        for index in range(count):
            _write(home / slot / "README.md", f"stash {index}\n")
            _git(home / slot, "stash", "push", "-q", "-m", f"stash {index}")
    lib_bare = home / ".repos" / "lib.git"
    _git(lib_bare, "branch", "lib-child", "lib_1")
    _git(lib_bare, "config", "branch.lib-child.pm-parent", "lib_1")
    _git(lib_bare, "config", "branch.lib-child.pm-fork", _git(lib_bare, "rev-parse", "lib_1").stdout.strip())

    driver = tmp_path / "git_count.py"
    driver.write_text(_GIT_COUNT_SOURCE, encoding="utf-8")
    out = tmp_path / "git-calls-two-repos.json"
    done = subprocess.run(
        [sys.executable, str(driver), str(out),
         str(home / ".project_manager" / "tools" / "pm_config.py"), "status", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(home), timeout=300)
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["rc"] == 0, done.stderr
    rows = {row["slot"]: row for row in json.loads(done.stdout)["slots"]}
    assert (rows[app_slot]["stashes"], rows[app_slot]["children"]) == (1, [])
    assert (rows[lib_slot]["stashes"], rows[lib_slot]["children"]) == (2, ["lib-child"])
    assert sum(1 for argv in record["calls"] if _PARENT_RECORDS_READ in argv) == 2
    assert sum(1 for argv in record["calls"] if argv[-3:] == _STASH_LIST_READ) == 2


@_git_required
def test_status_json_slot_rows_include_folders(home):
    _origin_bare(home, "app")
    origin = home / ".origin"
    _write(origin / "docs/guide.md", "guide\n")
    _write(origin / "src/app.py", "pass\n")
    _git(origin, "add", "docs", "src")
    _git(origin, "commit", "-qm", "folders")
    bare = home / ".repos/app.git"
    _git(bare, "fetch", "origin", "trunk:trunk")
    limited = _make_slot(home, "app", 1, "limited")
    full = _make_slot(home, "app", 2, "full")
    _git(home / limited, "sparse-checkout", "set", "--cone", "docs")
    _ledger(home, [{"slot": slot, "repo": "app", "state": "idle", "session": "", "started": "t"}
                   for slot in [limited, full]])
    rows = {row["slot"]: row for row in _status_json(home)["slots"]}
    assert rows[limited]["folders"] == ["docs"]
    assert rows[full]["folders"] is None
    _git(home / limited, "sparse-checkout", "set", "--no-cone", "/docs/*")
    done = _run_cli(home, "status", "--json")
    assert done.returncode == 1
    assert done.stdout == ""
    assert "[중단]" in done.stderr and "cone" in done.stderr


# ── T-0943 상태 JSON 의 압축 한도 표시와 `--preview` 삭제 (AT-010) ──────────────

_T0943_CONF = {"harness.claude.ctx_window_tokens": "600000", "ctx.checkpoint_at_pct": "80",
               "ctx.precompact_at_pct": "90", "ctx.compact_at_pct": "94"}


def test_t0943_status_ctx_budget_shape_and_readonly(tmp_path, monkeypatch):
    """`conf.ctx_budget` 은 도구마다 ok(표시 자료) 또는 error(사유)이고 `ctx_applied` 는 없다. 조회는 쓰지 않는다."""
    from test_harness_ctx_limits import install_ctx_record, _load as load_engine, _snapshot_fixture_bytes
    module = load_engine("pm_config")
    install_ctx_record(tmp_path, _T0943_CONF, capacity=200000)
    monkeypatch.setattr(module, "REPO", tmp_path)
    before = _snapshot_fixture_bytes(tmp_path)
    conf = module._status_json_conf(load_engine("pm_import"), None)
    assert "ctx_applied" not in conf
    claude = conf["ctx_budget"]["claude"]
    assert set(claude) == {"status", "E", "P", "checkpoint", "precompact", "checkpoint_at_pct", "precompact_at_pct",
                           "models", "applied_at", "warning", "C", "basis_text", "settings_mismatch"}
    assert (claude["status"], claude["C"], claude["E"], claude["P"]) == ("ok", 200000, 200000, 188000)
    assert claude["settings_mismatch"] == []
    for harness in ("codex", "opencode"):
        row = conf["ctx_budget"][harness]
        assert set(row) == {"status", "reason"} and row["status"] == "error" and "`pm_config.py apply`" in row["reason"]
    assert _snapshot_fixture_bytes(tmp_path) == before


def test_t0949_status_ctx_budget_settings_mismatch(tmp_path, monkeypatch):
    """T-0949 AT-004 — 상태 JSON 의 압축 한도 ok 항목은 설정 파일 값과 기록 settings 의 차이를 싣는다.
    반영이 쓴 `.claude/settings.json` 값을 바꾸면 그 경로 한 건이고, 조회는 아무 파일도 쓰지 않는다."""
    from test_harness_ctx_limits import install_ctx_record, _load as load_engine, _snapshot_fixture_bytes
    module = load_engine("pm_config")
    install_ctx_record(tmp_path, _T0943_CONF, capacity=200000)
    monkeypatch.setattr(module, "REPO", tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    text = settings.read_text(encoding="utf-8")
    assert text.count('"autoCompactWindow": 188000') == 1
    settings.write_text(text.replace('"autoCompactWindow": 188000', '"autoCompactWindow": 190000'), encoding="utf-8")
    before = _snapshot_fixture_bytes(tmp_path)
    claude = module._status_json_conf(load_engine("pm_import"), None)["ctx_budget"]["claude"]
    assert claude["status"] == "ok" and "settings_mismatch" in claude
    assert claude["settings_mismatch"] == [{"path": "autoCompactWindow", "file": 190000, "record": 188000}]
    assert _snapshot_fixture_bytes(tmp_path) == before


def test_t0943_apply_preview_is_an_argument_error(tmp_path, monkeypatch, capsys):
    """`apply --preview` 는 없는 인자다(rc 2) — 조회만 하고 기록을 쓰던 길이 없다."""
    from test_harness_ctx_limits import install_ctx_record, _load as load_engine, _snapshot_fixture_bytes
    module = load_engine("pm_config")
    install_ctx_record(tmp_path, _T0943_CONF, capacity=200000)
    monkeypatch.setattr(module, "REPO", tmp_path)
    before = _snapshot_fixture_bytes(tmp_path)
    with pytest.raises(SystemExit) as caught:
        module.main(["apply", "--json", "--preview"])
    assert caught.value.code == 2 and "--preview" in capsys.readouterr().err
    assert _snapshot_fixture_bytes(tmp_path) == before
