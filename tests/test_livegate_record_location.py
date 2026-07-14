"""livegate 기록 위치 == 훅 read 위치 가드 (T-0287·two-git footgun 재발 차단).

`livegate record` 는 push 보호훅이 읽는 **바로 그** livegate.json 에 기록해야 한다. 훅은 repo 의
git `core.hooksPath` 옆 sidecar `engine-root`(PM 홈 REPO 절대경로 1줄)로 board.py 를 해소해
`<engine-root>/.project_manager/.local/livegate.json` 을 읽는다. two-git 토폴로지(PM 홈+worktree·
ADR-0027)에서 record 를 호출된 사본의 `REPO/.local` 에 그냥 쓰면, worktree board.py 로 record 할
때 훅이 안 읽는 worktree `.local` 에 조용히 기록→pass 위장→push 순간에야 불일치로 드러났다
(PM 60 v1.1.0 릴리즈 실측). 여기선:

  - `_resolve_livegate_flag` 가 훅과 **동일 engine-root sidecar 해소**로 PM 홈 `.local` 을
    돌려주는지(단일 소스) — 실 git two-git 토폴로지를 모사해 단언.
  - end-to-end `record` 가 worktree board.py 로 돌아도 훅 read 위치(PM 홈 `.local`)에 기록하고
    호출된 사본의 `.local` 에는 안 쓰는지(단일 소스).
  - livegate 훅 없음(단일-repo/솔로)이면 현행 `REPO/.local` 폴백(채택자 무변경).
  - 훅 sidecar 는 있으나 engine-root 무효면 pass 대신 fail-loud(false-green 백스톱).
  - 훅 본문(worktree_pool)이 record 와 같은 engine-root 규약을 읽는지(공유 규약 드리프트 가드).
  - `check` 도 `record` 와 **대칭**으로 `_resolve_livegate_flag` 를 거쳐(모듈상수 직독 제거·
    T-0306) 어느 board.py 사본/cwd 로 check 하든 훅이 기록한 engine-root 파일을 읽는지
    (single-source·wrong-copy false-green/false-red 차단)·engine-root 무효면 record 와 동형
    fail-loud·솔로 폴백 무변경.

**hermetic + 라이브 실행 금지**: 실 `pytest -m release` 는 절대 기동하지 않는다 — `subprocess.run`
대역이 pytest 만 가로채고 git 은 실제로 돌려 `core.hooksPath` 해소를 충실히 검증한다. board.py
경로 전역(`REPO`·`LOCAL_DIR`·`LIVEGATE_FLAG`)은 tmp 로 재지정(test_board_livegate 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")

_HEAD_SHA = "cafef00dcafef00d0011223344556677cafef00d"


def _load_board():
    """board.py 를 (패키지 아님) importlib 로 경로 로드 — test_board_livegate 와 동일."""
    spec = importlib.util.spec_from_file_location("board_lgloc", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(cwd, *argv):
    """테스트용 실 git 헬퍼 — check=True·UTF-8 캡처."""
    return subprocess.run([_GIT, "-C", str(cwd), *argv], check=True,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def _rec_args(cwd=None):
    return argparse.Namespace(action="record", rev=None, cwd=cwd)


def _chk_args(rev, cwd=None):
    return argparse.Namespace(action="check", rev=rev, cwd=cwd)


class _PytestOnlyFake:
    """`subprocess.run` 대역 — `pytest -m release`(shell str)만 가로채고 git(list)은 실제로 돌린다.

    record 안에서 pytest 실행과 `_git_config_get`(core.hooksPath 해소)이 둘 다 `subprocess.run`
    을 타므로, pytest 만 대역하고 git 은 실 바이너리로 위임해야 훅-정렬 해소를 충실히 검증한다.
    """

    def __init__(self, real_run, rc: int, stdout: str):
        self._real = real_run
        self.rc = rc
        self.stdout = stdout
        self.calls: list = []
        self.pytest_ran = False   # 값비싼 `pytest -m release` 라이브런이 실제로 돌았는지(fail-fast 검증용).

    def __call__(self, *args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        self.calls.append({"args": args, "kwargs": kwargs})
        if isinstance(cmd, str) and "pytest" in cmd:
            self.pytest_ran = True
            return types.SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr="")
        return self._real(*args, **kwargs)


def _make_topology(tmp_path, monkeypatch, *, with_hookspath=True,
                   write_engine_root=True, engine_root_value=None,
                   board_in_engine=True):
    """실 git two-git 토폴로지 모사 → (mod, engine, worktree, hooks_dir).

    - engine: PM 홈(engine-root) 역할. board.py 더미 실재(`board_in_engine`) → 훅/record 해소.
    - worktree: 호출된 사본(=슬롯 board.py) 역할의 실 git repo. board.py 경로 전역을 여기로
      재지정 → record 를 worktree 사본으로 돌리는 PM 60 버그 형상. `core.hooksPath` = hooks_dir.
    - hooks_dir: 훅 옆 sidecar `engine-root`(기본=engine 절대경로) + `protected`.
    """
    mod = _load_board()

    engine = tmp_path / "engine"        # PM 홈 (훅이 board.py 를 해소하는 engine-root)
    if board_in_engine:
        (engine / ".project_manager" / "tools").mkdir(parents=True, exist_ok=True)
        (engine / ".project_manager" / "tools" / "board.py").write_text(
            "# dummy engine board.py\n", encoding="utf-8")
    else:
        (engine / ".project_manager").mkdir(parents=True, exist_ok=True)

    hooks_dir = tmp_path / "hooks"      # repo core.hooksPath (훅 + sidecar 위치)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "protected").write_text("main\n", encoding="utf-8", newline="\n")
    if write_engine_root:
        content = str(engine) if engine_root_value is None else engine_root_value
        (hooks_dir / "engine-root").write_text(
            f"{content}\n" if content else content, encoding="utf-8", newline="\n")

    worktree = tmp_path / "wt"          # 호출된 board.py 사본의 REPO (슬롯 checkout)
    worktree.mkdir(parents=True, exist_ok=True)
    _git(worktree, "init", "-q", "-b", "main")
    if with_hookspath:
        _git(worktree, "config", "core.hooksPath", str(hooks_dir))

    local = worktree / ".project_manager" / ".local"
    monkeypatch.setattr(mod, "REPO", worktree)
    monkeypatch.setattr(mod, "LOCAL_DIR", local)
    monkeypatch.setattr(mod, "LIVEGATE_FLAG", local / "livegate.json")
    monkeypatch.setattr(mod, "LEASES_FILE", local / "worktree-leases.json")  # 부재
    monkeypatch.setattr(mod, "_git_head_at", lambda cwd: _HEAD_SHA)
    return mod, engine, worktree, hooks_dir


def _hook_read_flag(hooks_dir: Path) -> Path:
    """훅이 실제로 읽는 livegate.json 을 sidecar 로부터 독립 재구성한다.

    훅은 `<hooks_dir>/engine-root`(PM 홈 절대경로)로 `<root>/.project_manager/tools/board.py`
    를 돌리고, 그 사본의 `LIVEGATE_FLAG = <root>/.project_manager/.local/livegate.json` 을
    읽는다. record 가 정렬해야 할 목표 위치 — `_resolve_livegate_flag` 와 독립 계산해 대조한다.
    """
    root = (hooks_dir / "engine-root").read_text(encoding="utf-8").splitlines()[0].strip()
    return Path(root) / ".project_manager" / ".local" / "livegate.json"


# ── ① 단일 소스 해소: record 위치 == 훅 read 위치 (two-git) ──────────────────

@_git_required
def test_resolve_livegate_flag_engine_root_matches_hook_read(tmp_path, monkeypatch):
    """훅 활성 two-git 토폴로지 → `_resolve_livegate_flag` = 훅 read 위치(PM 홈 .local)·단일 소스."""
    mod, engine, worktree, hooks_dir = _make_topology(tmp_path, monkeypatch)

    flag, mode = mod._resolve_livegate_flag(str(worktree))

    assert mode == mod._LG_ENGINE_ROOT
    # 독립 재구성한 훅-read 위치와 일치(record 위치 == 훅 read 위치).
    assert flag == _hook_read_flag(hooks_dir)
    assert flag == engine / ".project_manager" / ".local" / "livegate.json"
    # 호출된 사본의 순진한 위치(worktree .local)로 쓰지 않는다 — 정확히 이게 PM 60 버그였다.
    assert flag != mod.LIVEGATE_FLAG


@_git_required
def test_record_writes_to_hook_read_location_not_called_copy(tmp_path, monkeypatch):
    """end-to-end: worktree board.py 로 record 해도 훅 read 위치(PM 홈 .local)에 기록·호출 사본엔 안 씀."""
    mod, engine, worktree, hooks_dir = _make_topology(tmp_path, monkeypatch)
    real_run = mod.subprocess.run
    fake = _PytestOnlyFake(real_run, 0, f"{mod.LIVEGATE_RELEASE_PIN} passed, 812 deselected in 1.0s")
    monkeypatch.setattr(mod.subprocess, "run", fake)

    rc = mod.cmd_livegate(_rec_args(cwd=str(worktree)))

    assert rc == 0
    hook_flag = _hook_read_flag(hooks_dir)
    assert hook_flag.exists(), "record 가 훅 read 위치(PM 홈 .local)에 기록하지 않았다"
    data = json.loads(hook_flag.read_text(encoding="utf-8"))
    assert data["status"] == "pass"
    assert data["head"] == _HEAD_SHA
    assert data["n"] == mod.LIVEGATE_RELEASE_PIN
    # 호출된 사본의 .local(worktree)에는 조용한 기록이 없어야 한다(단일 소스·false-green 차단).
    assert not mod.LIVEGATE_FLAG.exists(), \
        "record 가 훅이 안 읽는 호출-사본 .local 에 조용히 기록했다(PM 60 footgun 재발)"


# ── ② 단일-repo/솔로 폴백: 훅 없음 → 현행 REPO/.local (채택자 무변경) ─────────

@_git_required
def test_resolve_solo_no_hookspath_falls_back_to_repo_local(tmp_path, monkeypatch):
    """`core.hooksPath` 미설정(단일-repo/솔로) → LIVEGATE_FLAG(REPO/.local) 폴백·mode solo."""
    mod, engine, worktree, hooks_dir = _make_topology(
        tmp_path, monkeypatch, with_hookspath=False)

    flag, mode = mod._resolve_livegate_flag(str(worktree))

    assert mode == mod._LG_SOLO
    assert flag == mod.LIVEGATE_FLAG


@_git_required
def test_resolve_hookspath_without_engine_root_is_solo_not_broken(tmp_path, monkeypatch):
    """`core.hooksPath` 는 있으나 engine-root sidecar 부재(예: PM 홈 R8 회귀 훅) → solo(오탐 fail-loud 방지)."""
    mod, engine, worktree, hooks_dir = _make_topology(
        tmp_path, monkeypatch, write_engine_root=False)

    flag, mode = mod._resolve_livegate_flag(str(worktree))

    assert mode == mod._LG_SOLO, "livegate 훅 아닌 hooksPath 를 broken 으로 오탐하면 안 됨"
    assert flag == mod.LIVEGATE_FLAG


@_git_required
def test_record_solo_records_at_repo_local(tmp_path, monkeypatch):
    """솔로 폴백 경로에서 record 가 정상 pass 를 REPO/.local(LIVEGATE_FLAG)에 기록(현행 동작 보존)."""
    mod, engine, worktree, hooks_dir = _make_topology(
        tmp_path, monkeypatch, with_hookspath=False)
    real_run = mod.subprocess.run
    fake = _PytestOnlyFake(real_run, 0, f"{mod.LIVEGATE_RELEASE_PIN} passed, 812 deselected in 1.0s")
    monkeypatch.setattr(mod.subprocess, "run", fake)

    rc = mod.cmd_livegate(_rec_args(cwd=str(worktree)))

    assert rc == 0
    assert mod.LIVEGATE_FLAG.exists()
    data = json.loads(mod.LIVEGATE_FLAG.read_text(encoding="utf-8"))
    assert data["status"] == "pass"


# ── ③ fail-loud 백스톱: 훅 sidecar 있으나 engine-root 무효 → false-green 차단 ──

@_git_required
def test_resolve_broken_when_engine_root_empty(tmp_path, monkeypatch):
    """engine-root sidecar 가 빈값 → mode broken(해소 불능·기록 위치 불확정)."""
    mod, engine, worktree, hooks_dir = _make_topology(
        tmp_path, monkeypatch, engine_root_value="")

    _flag, mode = mod._resolve_livegate_flag(str(worktree))

    assert mode == mod._LG_BROKEN


@_git_required
def test_resolve_broken_when_engine_root_lacks_board(tmp_path, monkeypatch):
    """engine-root 가 board.py 없는 경로를 가리킴 → mode broken(훅도 fail-closed 하는 상태)."""
    mod, engine, worktree, hooks_dir = _make_topology(
        tmp_path, monkeypatch, board_in_engine=False)

    _flag, mode = mod._resolve_livegate_flag(str(worktree))

    assert mode == mod._LG_BROKEN


@_git_required
def test_record_broken_engine_root_fails_loud_no_write(tmp_path, monkeypatch, capsys):
    """훅 sidecar 있으나 engine-root 무효 → record 는 pass 대신 rc1 fail-loud·어디에도 기록 안 함."""
    mod, engine, worktree, hooks_dir = _make_topology(
        tmp_path, monkeypatch, board_in_engine=False)
    real_run = mod.subprocess.run
    fake = _PytestOnlyFake(real_run, 0, f"{mod.LIVEGATE_RELEASE_PIN} passed, 812 deselected in 1.0s")
    monkeypatch.setattr(mod.subprocess, "run", fake)

    rc = mod.cmd_livegate(_rec_args(cwd=str(worktree)))

    assert rc == 1, "engine-root 무효인데 pass(0)를 반환하면 false-green 백스톱이 뚫린 것"
    err = capsys.readouterr().err
    assert "engine-root sidecar 무효" in err
    assert "false-green 차단" in err
    # pass 위장을 어디에도 남기지 않는다 — 호출-사본 .local 도, engine .local 도.
    assert not mod.LIVEGATE_FLAG.exists()
    assert not (engine / ".project_manager" / ".local" / "livegate.json").exists()
    # fail-fast: BROKEN 은 실행 전에 알 수 있으니 값비싼 `pytest -m release`(~6.5분 실 LLM·실과금)를
    # 아예 안 돈다. resolve+BROKEN 체크가 run *앞*에 온다 (T-0287 리뷰 should-fix·PM 60 낭비 회피).
    assert not fake.pytest_ran, \
        "BROKEN 인데 값비싼 pytest -m release 라이브런을 돌렸다 — fail-fast 아님(PM 60 낭비 재현)"


# ── ④ 공유 규약 가드: 훅 본문 == record 해소가 읽는 engine-root 규약 ──────────

def test_hook_body_shares_engine_root_livegate_convention():
    """worktree_pool 훅 본문이 record(`_resolve_livegate_flag`)와 같은 engine-root 규약을 읽는다.

    훅이 sidecar 이름(`engine-root`)·board.py 경로·`livegate check` 소비를 바꾸면 record 해소와
    갈라져 단일 소스가 깨진다 — 그 드리프트를 여기서 못박는다(정렬 계약 회귀).
    """
    spec = importlib.util.spec_from_file_location("wp_lgloc", TOOLS / "worktree_pool.py")
    wp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wp)
    body = wp._PROTECTED_PRE_PUSH_HOOK
    assert "engine-root" in body, "훅이 engine-root sidecar 를 안 읽으면 record 정렬 근거가 사라짐"
    assert ".project_manager/tools/board.py" in body
    assert "livegate check" in body


@_git_required
def test_resolve_livegate_flag_solo_when_not_git_repo(tmp_path, monkeypatch):
    """비-git 경로(git config 실패) → solo 폴백(비-repo·격리 환경 graceful).

    `_git_config_get` 이 실 git 바이너리를 호출하므로 git 없는 러너에선 skip(FileNotFoundError
    회피)한다 — 파일의 다른 케이스와 동일 가드(리뷰 nit).
    """
    mod = _load_board()
    proj = tmp_path / "proj"
    local = proj / ".project_manager" / ".local"
    monkeypatch.setattr(mod, "REPO", proj)
    monkeypatch.setattr(mod, "LOCAL_DIR", local)
    monkeypatch.setattr(mod, "LIVEGATE_FLAG", local / "livegate.json")

    flag, mode = mod._resolve_livegate_flag(str(tmp_path / "nonrepo"))

    assert mode == mod._LG_SOLO
    assert flag == mod.LIVEGATE_FLAG


# ── ⑤ 상대 core.hooksPath: git 시맨틱대로 worktree root 기준 해소 (T-0287 방어) ──

@_git_required
def test_resolve_relative_hookspath_resolves_against_worktree_root(tmp_path, monkeypatch):
    """상대 `core.hooksPath` 는 git 시맨틱대로 worktree root(=`cwd`) 기준 해소된다(프로세스 cwd 아님).

    git 은 상대 `core.hooksPath` 를 worktree root 기준으로 돌린다. record 해소가 이를 프로세스 cwd
    기준으로 잘못 해소하면(현 설치는 절대경로라 평시 무영향) 훅과 갈라진다 — 방어 회귀(codex 제안).
    """
    mod = _load_board()
    engine = tmp_path / "engine"
    (engine / ".project_manager" / "tools").mkdir(parents=True, exist_ok=True)
    (engine / ".project_manager" / "tools" / "board.py").write_text(
        "# dummy engine board.py\n", encoding="utf-8")

    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    _git(worktree, "init", "-q", "-b", "main")
    # hooks 디렉토리를 worktree 안에 두고 core.hooksPath 를 **상대경로**("myhooks")로 설정.
    hooks_dir = worktree / "myhooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "engine-root").write_text(f"{engine}\n", encoding="utf-8", newline="\n")
    _git(worktree, "config", "core.hooksPath", "myhooks")   # ← 상대

    local = worktree / ".project_manager" / ".local"
    monkeypatch.setattr(mod, "REPO", worktree)
    monkeypatch.setattr(mod, "LOCAL_DIR", local)
    monkeypatch.setattr(mod, "LIVEGATE_FLAG", local / "livegate.json")

    # 프로세스 cwd(=pytest 실행 위치)가 worktree 가 아니어도 worktree root 기준으로 해소돼야 한다.
    flag, mode = mod._resolve_livegate_flag(str(worktree))

    assert mode == mod._LG_ENGINE_ROOT, "상대 core.hooksPath 를 worktree root 기준 해소하지 못함"
    assert flag == engine / ".project_manager" / ".local" / "livegate.json"


# ── ⑥ check 단일 소스: record 와 대칭 (T-0306·모듈상수 직독 제거·wrong-copy 오독 차단) ──
# check 도 record 와 **동일 해소**(`_resolve_livegate_flag`)를 거쳐 어느 board.py 사본/cwd 로 check
# 하든 훅이 기록한 engine-root 파일을 읽는다(single-source). 모듈상수 `LIVEGATE_FLAG` 직독을 남겨두면
# 호출-사본의 stale/wrong `.local` 을 읽어 false-green/false-red 오독이 난다 — 그걸 못박는다.


def _write_livegate_json(path: Path, *, status: str, head: str, n=None, rc=0):
    """livegate.json 을 직접 기록한다(record 실행 없이 check 소비만 테스트)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"head": head, "status": status,
               "n": n if n is not None else 0, "rc": rc, "ts": "2026-07-14T00:00:00+00:00"}
    path.write_text(json.dumps(payload), encoding="utf-8")


@_git_required
def test_check_worktree_cwd_reads_engine_root_record(tmp_path, monkeypatch, capsys):
    """worktree cwd 로 check 해도 훅 read 위치(engine-root .local)의 기록을 읽어 green (단일 소스).

    호출-사본(worktree) LIVEGATE_FLAG 에는 **상충하는 fail** 을 심어, check 가 그 wrong-copy 를
    읽으면 red 로 갈리게 해둔다 — green 이 나오면 engine-root(훅 기록)를 읽은 것이다(false-red 차단).
    """
    mod, engine, worktree, hooks_dir = _make_topology(tmp_path, monkeypatch)
    hook_flag = _hook_read_flag(hooks_dir)
    _write_livegate_json(hook_flag, status="pass", head=_HEAD_SHA,
                         n=mod.LIVEGATE_RELEASE_PIN, rc=0)
    # 호출-사본(worktree)의 순진한 위치엔 상충 fail — check 가 이걸 읽으면 안 된다.
    _write_livegate_json(mod.LIVEGATE_FLAG, status="fail", head=_HEAD_SHA, n=0, rc=1)

    rc = mod.cmd_livegate(_chk_args(rev=_HEAD_SHA, cwd=str(worktree)))

    assert rc == 0, "worktree cwd check 가 engine-root(훅 기록) green 을 읽지 못함"
    assert "green" in capsys.readouterr().out


@_git_required
def test_check_default_cwd_resolves_engine_root_not_module_const(tmp_path, monkeypatch):
    """`--cwd` 미지정 → 이 board.py 사본의 REPO(=worktree)로 해소·engine-root 기록을 읽는다.

    default cwd 가 모듈상수 LIVEGATE_FLAG 직독으로 폴백하면 worktree 의 상충 fail 을 읽어 red 가 난다.
    green 이면 default(REPO)→hooksPath→engine-root 정렬이 살아있는 것(모듈상수 직독 제거 회귀).
    """
    mod, engine, worktree, hooks_dir = _make_topology(tmp_path, monkeypatch)
    _write_livegate_json(_hook_read_flag(hooks_dir), status="pass", head=_HEAD_SHA,
                         n=mod.LIVEGATE_RELEASE_PIN, rc=0)
    _write_livegate_json(mod.LIVEGATE_FLAG, status="fail", head=_HEAD_SHA, n=0, rc=1)

    rc = mod.cmd_livegate(_chk_args(rev=_HEAD_SHA, cwd=None))   # default → str(REPO)=worktree

    assert rc == 0, "default cwd check 가 모듈상수(worktree 상충 fail)를 직독함 — single-source 깨짐"


@_git_required
def test_check_rev_mismatch_against_engine_root(tmp_path, monkeypatch, capsys):
    """engine-root 기록이 pass 라도 push rev 가 다르면 rc1 rev 불일치(engine-root 값으로 판정)."""
    mod, engine, worktree, hooks_dir = _make_topology(tmp_path, monkeypatch)
    _write_livegate_json(_hook_read_flag(hooks_dir), status="pass", head=_HEAD_SHA,
                         n=mod.LIVEGATE_RELEASE_PIN, rc=0)

    rc = mod.cmd_livegate(_chk_args(rev="deadbeef" * 5, cwd=str(worktree)))

    assert rc == 1
    assert "rev 불일치" in capsys.readouterr().err


@_git_required
def test_check_broken_engine_root_fails_loud(tmp_path, monkeypatch, capsys):
    """훅 sidecar 있으나 engine-root 무효(board.py 미해소) → check 는 record 와 동형 fail-loud rc1.

    조용한 통과(false-green)도, 조용한 red 도 아닌 명시 거부 — 기록/훅 read 위치가 갈릴 수 있어
    판정을 신뢰 못 하기 때문(record `test_record_broken_engine_root_fails_loud` 와 대칭).
    """
    mod, engine, worktree, hooks_dir = _make_topology(
        tmp_path, monkeypatch, board_in_engine=False)

    rc = mod.cmd_livegate(_chk_args(rev=_HEAD_SHA, cwd=str(worktree)))

    assert rc == 1, "engine-root 무효인데 check 가 조용히 통과/red 판정하면 fail-loud 백스톱이 뚫린 것"
    err = capsys.readouterr().err
    assert "engine-root sidecar 무효" in err
    assert "false-green 차단" in err


@_git_required
def test_check_solo_no_hookspath_reads_repo_local(tmp_path, monkeypatch, capsys):
    """`core.hooksPath` 미설정(단일-repo/솔로) → 현행 REPO/.local(LIVEGATE_FLAG) 폴백 read 무변경."""
    mod, engine, worktree, hooks_dir = _make_topology(
        tmp_path, monkeypatch, with_hookspath=False)
    _write_livegate_json(mod.LIVEGATE_FLAG, status="pass", head=_HEAD_SHA,
                         n=mod.LIVEGATE_RELEASE_PIN, rc=0)

    rc = mod.cmd_livegate(_chk_args(rev=_HEAD_SHA, cwd=str(worktree)))

    assert rc == 0, "솔로 폴백에서 check 가 REPO/.local 기록을 못 읽음(회귀)"
    assert "green" in capsys.readouterr().out


@_git_required
def test_check_absent_record_at_engine_root_blocks(tmp_path, monkeypatch, capsys):
    """engine-root 로 정렬됐으나 아직 기록 없음 → rc1 '기록 없음'(호출-사본 stale 을 pass 로 오독 안 함).

    호출-사본(worktree) LIVEGATE_FLAG 에 stale pass 를 심어도, check 는 engine-root(부재)를 봐서
    '기록 없음' 을 낸다 — wrong-copy stale 을 green 으로 오독하지 않는다(false-green 차단).
    """
    mod, engine, worktree, hooks_dir = _make_topology(tmp_path, monkeypatch)
    _write_livegate_json(mod.LIVEGATE_FLAG, status="pass", head=_HEAD_SHA,
                         n=mod.LIVEGATE_RELEASE_PIN, rc=0)   # 호출-사본 stale pass

    rc = mod.cmd_livegate(_chk_args(rev=_HEAD_SHA, cwd=str(worktree)))

    assert rc == 1, "engine-root 무기록인데 호출-사본 stale pass 를 green 으로 오독함(single-source 깨짐)"
    assert "기록 없음" in capsys.readouterr().err
