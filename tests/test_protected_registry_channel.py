"""보호목록 설정 채널 — `repo add --protected` · `repo protected` · `repo list` · sidecar 정합화
(T-0417·ADR-0072).

areas.md `protected` 칼럼이 **단일 진실**이고 훅 sidecar(`.local/repo-hooks/<repo>/protected`)는
순수 파생 캐시다. 이 파일이 검증하는 계약:

  1. **`repo add --protected`** — 인자가 areas 등록 줄에 반영된다(종전 `protected=""` 하드코딩 제거).
     형식 위반/빈 문자열은 **어떤 부작용보다 앞에서** fail-loud(clone/등록/훅 0).
  2. **`repo protected <name>` 조회** — 실효값 + 출처(명시/기본값 폴백/미등록) + sidecar 정합/drift
     3줄. "빈 값이라 기본값 폴백 중"·"훅은 아직 옛 값으로 동작" 두 사실이 보여야 한다.
  3. **`repo protected <name> <목록>` 설정** — **areas → sidecar 순서 고정**(역순이면 비준되지 않은
     목록을 훅이 강제한다) + board-git best-effort 동기. `default` 리터럴 = 칼럼 비움(폴백 복귀).
  4. **fail-loud** — 중복 repo 행/미등록/형식 위반은 areas 미변경 + sidecar 미호출(부작용 0).
  5. **`repo list`** — 등록 repo 표(빈 protected 는 "기본값" 을 명시해 "보호 없음" 오독 차단).
  6. **bootstrap phase-0 drift-only reconcile** — sidecar 가 areas 와 다를 때만 재설치(정합이면
     subprocess 0), board/worktree_pool 부재는 fail-soft.

**hermetic**: board 는 실 모듈을 tmp 경로로 monkeypatch 해 쓰고(실 areas_set_cell 의미 검증),
worktree_pool·git runner 는 DI mock — 실 clone/훅 설치/네트워크 0.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
REAL_AREAS = REPO / ".project_manager" / "areas.md"

_CANONICAL_AREAS = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| service-a | PAY | git@x:a.git | pytest -q | alice | develop | main | alice |\n"
    "| service-b | ACC | git@x:b.git | go test | bob | main |  | bob |\n"
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    """실 board 모듈 + 경로 전역을 tmp 로 재지정한 hermetic 인스턴스 (test_board_per_repo 동형)."""
    pm = tmp_path / "home" / ".project_manager"
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    mod = _load("board")
    for name, val in {
        "REPO": tmp_path / "home",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_CONF": pm / "local.conf",
        "LOCAL_DIR": pm / ".local",
        "BOARD_LOCK": pm / ".local" / "board.lock",
    }.items():
        monkeypatch.setattr(mod, name, val)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # board-git 동기는 이 파일 범위 밖(별 git) — no-op 으로 고정하고 호출만 기록한다.
    mod._sync_calls = []
    monkeypatch.setattr(mod, "_board_git_sync_best_effort",
                        lambda message: mod._sync_calls.append(message))
    return mod


@pytest.fixture(scope="module")
def pc():
    return _load("pm_config")


@pytest.fixture(scope="module")
def bootstrap():
    return _load("pm_bootstrap")


class FakePool:
    """worktree_pool DI mock — `install_protected_hook` 호출 기록 + 실제 sidecar 파일 갱신.

    `REPO_HOOKS_DIR` 는 pm_config/pm_bootstrap 이 sidecar 경로를 해소하는 seam 이다(둘 다
    getattr·직접 import 금지). `bare_repo_path` 는 `core.hooksPath` 배선 판정 seam.
    `ok=False` 면 설치 실패(bare 부재 등)를 모델링한다.
    """

    def __init__(self, hooks_dir: Path, *, ok: bool = True, events: list | None = None,
                 repos_dir: Path | None = None):
        self.REPO_HOOKS_DIR = hooks_dir
        self.calls: list[tuple[str, list[str]]] = []
        self.events = events if events is not None else []
        self._ok = ok
        self._repos_dir = repos_dir if repos_dir is not None else hooks_dir.parent / ".repos"

    def bare_repo_path(self, repo):
        return self._repos_dir / f"{repo}.git"

    def install_protected_hook(self, repo, protected, *, git_runner=None):
        self.calls.append((repo, list(protected)))
        self.events.append("sidecar")
        if not self._ok:
            return False
        write_sidecar(self.REPO_HOOKS_DIR, repo, protected)
        return True


class HooksPathGit:
    """`core.hooksPath` 조회 대역 — `configured` 값을 돌려주는 최소 git runner.

    `configured=None` = 키 부재(rc 1·미배선), `configured=<path>` = 그 값이 설정됨.
    `broken=True` = git 자체가 못 돎(bare 프로브까지 rc≠0) → 판정 불가(None).
    """

    def __init__(self, *, configured=None, broken=False):
        self._configured = configured
        self._broken = broken
        self.calls: list[list] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self._broken:
            return 1, "fatal: not a git repository"
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return 0, "true\n"
        if "config" in argv and "core.hooksPath" in argv:
            if self._configured is None:
                return 1, ""            # `--get` 키 부재 = rc 1
            return 0, f"{self._configured}\n"
        return 0, ""


def write_sidecar(hooks_dir: Path, repo: str, branches) -> Path:
    """훅 sidecar(`<hooks>/<repo>/protected`·줄당 1브랜치)를 직접 쓴다 (설치된 상태 시드)."""
    path = hooks_dir / repo / "protected"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{b}\n" for b in branches), encoding="utf-8")
    return path


class GitFake:
    """git runner 대역 — clone/config/fetch 는 성공, `show-ref` 는 `existing` 집합으로 판정."""

    def __init__(self, *, existing=("main",), head="main"):
        self.calls: list[list] = []
        self._existing = set(existing)
        self._head = head

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "rev-parse" in argv and "--is-bare-repository" in argv:
            return 0, "true\n"
        if "rev-parse" in argv and "--verify" in argv and argv[-1] == "HEAD":
            return 0, "0123abc\n"
        if "show-ref" in argv:
            ref = argv[-1]
            return (0, "") if ref.rsplit("/", 1)[-1] in self._existing else (1, "")
        if "symbolic-ref" in argv:
            return 0, f"refs/heads/{self._head}\n"
        return 0, ""


def _protected_args(name, value=None):
    return argparse.Namespace(name=name, value=value)


def _add_args(name="svc", git="git@h:me/svc.git", test=None, owner="me",
              base=None, protected=None, user="alice"):
    return argparse.Namespace(name=name, git=git, test=test, owner=owner,
                              base=base, protected=protected, user=user)


# ════════════════════════════════════════════════════════════════════════
# 1. repo add --protected
# ════════════════════════════════════════════════════════════════════════

def test_repo_add_protected_flag_lands_in_areas(pc, board, tmp_path):
    """`--protected "main,develop"` 이 areas.md `protected` 칼럼에 기록된다(하드코딩 제거)."""
    pool = FakePool(tmp_path / "hooks")
    rc = pc.cmd_repo_add(
        _add_args(protected="main,develop"), board=board, clone_runner=GitFake(),
        repos_dir=tmp_path / ".repos", worktree_pool=pool)
    assert rc == 0
    assert board._repo_protected("svc") == ["main", "develop"]


def test_repo_add_without_protected_keeps_default_fallback(pc, board, tmp_path):
    """`--protected` 생략 = 빈 칼럼 — `_repo_protected` 가 DEFAULT_PROTECTED 폴백(기존 동작 무변경)."""
    rc = pc.cmd_repo_add(
        _add_args(), board=board, clone_runner=GitFake(),
        repos_dir=tmp_path / ".repos", worktree_pool=FakePool(tmp_path / "hooks"))
    assert rc == 0
    _header, rows = board._parse_areas()
    assert rows[0]["protected"] == ""
    assert board._repo_protected("svc") == list(board.DEFAULT_PROTECTED)


def test_repo_add_protected_normalizes_spacing(pc, board, tmp_path):
    """`"main, release"` 처럼 쉼표 뒤 공백은 정규화해 기록한다(셀 corruption 0)."""
    pc.cmd_repo_add(
        _add_args(protected="main, release"), board=board, clone_runner=GitFake(),
        repos_dir=tmp_path / ".repos", worktree_pool=FakePool(tmp_path / "hooks"))
    _header, rows = board._parse_areas()
    assert rows[0]["protected"] == "main,release"


@pytest.mark.parametrize("bad", ["", "   ", "main,,develop", "main,", "ma in", "a|b"])
def test_repo_add_bad_protected_rejected_before_side_effects(pc, board, tmp_path, bad, capsys):
    """형식 위반 `--protected` 는 clone/등록/훅 **이전** 거부 — 부작용 0 (rc 1)."""
    gitr = GitFake()
    pool = FakePool(tmp_path / "hooks")
    rc = pc.cmd_repo_add(
        _add_args(protected=bad), board=board, clone_runner=gitr,
        repos_dir=tmp_path / ".repos", worktree_pool=pool)
    assert rc == 1
    assert gitr.calls == [] and pool.calls == []
    assert board.registered_repos() == set()
    assert "[중단]" in capsys.readouterr().err


def test_repo_add_protected_warns_on_branch_absent_from_bare(pc, board, tmp_path, capsys):
    """**must-fix**: 신규 등록 경로도 bare 에 없는 브랜치를 경고한다 (set 경로와 같은 계약).

    `--protected mian` 같은 오타가 기본 보호목록(main/master/develop)을 덮으면서 조용히 통과하면
    **보호 가드가 실질적으로 꺼진다** — 한쪽 경로에만 검증이 있으면 안 된다. 거부는 아니다(rc 0).
    """
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)          # 기존 bare 재사용(clone skip)
    rc = pc.cmd_repo_add(
        _add_args(protected="mian,release"), board=board,
        clone_runner=GitFake(existing=("main", "develop")),
        repos_dir=repos, worktree_pool=FakePool(tmp_path / "hooks"))
    assert rc == 0
    err = capsys.readouterr().err
    assert "없는 브랜치" in err and "mian" in err and "release" in err
    assert board._repo_protected("svc") == ["mian", "release"]   # 기록은 됨(경고이지 거부 아님)


def test_repo_add_protected_no_warning_when_branches_exist(pc, board, tmp_path, capsys):
    """bare 에 있는 브랜치만 지정하면 경고 없음(잡음 0)."""
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)
    pc.cmd_repo_add(_add_args(protected="main,develop"), board=board,
                    clone_runner=GitFake(existing=("main", "develop")),
                    repos_dir=repos, worktree_pool=FakePool(tmp_path / "hooks"))
    assert "없는 브랜치" not in capsys.readouterr().err


def test_repo_add_and_protected_set_share_one_warning_funnel(pc, monkeypatch, board, tmp_path):
    """두 경로가 **같은 헬퍼**(`_warn_missing_protected_branches`)를 탄다 — 검증 비대칭 재발 방지.

    문구 대조가 아니라 *깔때기 동일성*을 못박는다(한쪽만 고치는 회귀를 기계로 잡는다).
    """
    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(pc, "_warn_missing_protected_branches",
                        lambda repo, branches, **_kw: seen.append((repo, list(branches))) or [])
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)
    pc.cmd_repo_add(_add_args(protected="main,release"), board=board,
                    clone_runner=GitFake(), repos_dir=repos,
                    worktree_pool=FakePool(tmp_path / "hooks"))
    pc.cmd_repo_protected(_protected_args("svc", "main,develop"), board=board,
                          worktree_pool=FakePool(tmp_path / "hooks"),
                          clone_runner=GitFake(), repos_dir=repos)
    assert seen == [("svc", ["main", "release"]), ("svc", ["main", "develop"])]


def test_repo_add_hook_install_failure_is_loud(pc, board, tmp_path, capsys):
    """훅 설치 실패(False 반환)를 조용히 넘기지 않는다 — 훅 미설치 침묵 = 보호 가드 무력화."""
    cap_rc = pc.cmd_repo_add(
        _add_args(), board=board, clone_runner=GitFake(),
        repos_dir=tmp_path / ".repos",
        worktree_pool=FakePool(tmp_path / "hooks", ok=False))
    cap = capsys.readouterr()
    assert cap_rc == 0                                   # 등록 자체는 성공(추가 가드라 rc 무영향)
    assert "[경고]" in cap.err and "훅 sidecar" in cap.err
    assert "✓ 보호 브랜치 pre-push 훅" not in cap.out     # 실패인데 성공 알림 금지


def test_repo_add_protected_on_already_registered_repo_is_loud(pc, board, tmp_path, capsys):
    """이미 등록된 repo 에 `--protected` 를 주면 **반영 안 됨**을 loud 안내 + 대체 커맨드.

    등록 줄은 append-only 라 이 경로는 등록을 건너뛴다 — 조용히 삼키면 사용자는 보호목록을
    바꿨다고 믿는데 areas 는 그대로다(값-연결 끊김과 같은 클래스).
    """
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    repos = tmp_path / ".repos"
    (repos / "service-a.git").mkdir(parents=True)
    rc = pc.cmd_repo_add(
        _add_args(name="service-a", protected="release"), board=board,
        clone_runner=GitFake(), repos_dir=repos,
        worktree_pool=FakePool(tmp_path / "hooks"))
    assert rc == 0
    assert board._repo_protected("service-a") == ["main"]     # 실제로 반영 안 됨
    err = capsys.readouterr().err
    assert "반영되지 않았다" in err
    assert "repo protected service-a" in err


def test_bare_probe_failure_suppresses_branch_warnings(pc, tmp_path, capsys):
    """bare 조회 자체가 실패하면(git 부재 등) 경고를 통째로 생략한다 — 오탐 0."""
    repos = tmp_path / ".repos"
    (repos / "svc.git").mkdir(parents=True)

    def broken_git(argv):
        return 1, "git 바이너리를 찾을 수 없음 (PATH)."

    assert pc._bare_missing_branches("svc", ["main"], clone_runner=broken_git,
                                     repos_dir=repos) == []
    assert pc._warn_missing_protected_branches(
        "svc", ["main"], clone_runner=broken_git, repos_dir=repos) == []
    assert capsys.readouterr().err == ""


def test_repo_add_empty_protected_guides_to_default_literal(pc, board, tmp_path, capsys):
    """빈 `--protected` 거부 메시지는 "보호 없음 표현 불가" + 기본값 경로를 안내한다."""
    pc.cmd_repo_add(_add_args(protected=""), board=board, clone_runner=GitFake(),
                    repos_dir=tmp_path / ".repos")
    err = capsys.readouterr().err
    assert "default" in err and "생략" in err


# ════════════════════════════════════════════════════════════════════════
# 2. repo protected — 조회 3줄(실효값·출처·sidecar 정합/drift)
# ════════════════════════════════════════════════════════════════════════

def test_protected_get_explicit_value_with_matching_sidecar(pc, board, tmp_path, capsys):
    """명시값 + sidecar 정합 + 훅 배선 → 실효값·출처(명시)·`✓ 정합` 3줄."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pool, hooks, _repos = _wired_pool(tmp_path)
    write_sidecar(hooks, "service-a", ["main"])
    rc = pc.cmd_repo_protected(
        _protected_args("service-a"), board=board, worktree_pool=pool,
        clone_runner=HooksPathGit(configured=str((hooks / "service-a").resolve())))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out[0] == "service-a · protected = main"
    assert "출처: 명시" in out[1]
    assert "✓ 정합" in out[2] and str(hooks / "service-a" / "protected") in out[2]


def test_protected_get_empty_column_surfaces_default_fallback(pc, board, tmp_path, capsys):
    """빈 칼럼이면 실효값=기본값 + 출처에 "칼럼 비어 있음" 을 명시한다(핵심 사용자-가치)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "service-b", board.DEFAULT_PROTECTED)
    pc.cmd_repo_protected(_protected_args("service-b"), board=board,
                          worktree_pool=FakePool(hooks))
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "service-b · protected = main, master, develop"
    assert "기본값 폴백" in out[1] and "비어 있음" in out[1]
    assert "✓ 목록 정합" in out[2]      # bare 부재라 배선은 "확인 불가"(정직 표기)


def test_protected_get_drift_surfaces_old_list_and_remedy(pc, board, tmp_path, capsys):
    """sidecar 가 다르면 `⚠ 옛 목록(...)` + "훅은 아직 옛 값으로 동작" + 재실행 안내."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "service-a", ["master"])
    pc.cmd_repo_protected(_protected_args("service-a"), board=board,
                          worktree_pool=FakePool(hooks))
    out = capsys.readouterr().out
    assert "⚠ 옛 목록(master)" in out
    assert "아직 옛 값으로 동작" in out
    assert "repo protected service-a main" in out


def test_protected_get_missing_sidecar_is_surfaced(pc, board, tmp_path, capsys):
    """sidecar 미설치도 조용하지 않다 — `(미설치)` + 설치 안내."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pc.cmd_repo_protected(_protected_args("service-a"), board=board,
                          worktree_pool=FakePool(tmp_path / "hooks"))
    out = capsys.readouterr().out
    assert "(미설치)" in out and "repo add service-a" in out


def test_protected_get_unregistered_repo_says_so(pc, board, tmp_path, capsys):
    """미등록 repo 조회도 실효값(기본값)을 내되 출처를 "미등록" 으로 정직하게 밝힌다(rc 0)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    rc = pc.cmd_repo_protected(_protected_args("nope"), board=board,
                               worktree_pool=FakePool(tmp_path / "hooks"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "nope · protected = main, master, develop" in out
    assert "미등록" in out


def test_protected_get_writes_nothing(pc, board, tmp_path):
    """조회는 순수 읽기 — areas.md·sidecar 어느 것도 쓰지 않는다."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    hooks = tmp_path / "hooks"
    pool = FakePool(hooks)
    pc.cmd_repo_protected(_protected_args("service-a"), board=board, worktree_pool=pool)
    assert pool.calls == []
    assert board.AREAS_FILE.read_text(encoding="utf-8") == _CANONICAL_AREAS


# ── hooksPath 배선 축 (should-fix — sidecar 최신 + 훅 미배선 = 보호 꺼짐) ────────

def _wired_pool(tmp_path, repo="service-a"):
    """sidecar 가 실재하고 bare 도 실재하는 풀 — 배선 판정만 git 대역으로 가른다."""
    hooks = tmp_path / "hooks"
    repos = tmp_path / ".repos"
    (repos / f"{repo}.git").mkdir(parents=True, exist_ok=True)
    return FakePool(hooks, repos_dir=repos), hooks, repos


def test_protected_hook_wired_true_when_hookspath_matches(pc, tmp_path):
    """`core.hooksPath` 가 우리 훅 디렉토리를 가리키면 True (읽기만·설치 호출 0)."""
    pool, hooks, _repos = _wired_pool(tmp_path)
    git = HooksPathGit(configured=str((hooks / "service-a").resolve()))
    assert pc.protected_hook_wired("service-a", worktree_pool=pool, git_runner=git) is True
    assert pool.calls == []          # 읽기 전용 — install_protected_hook 미호출


def test_protected_hook_wired_false_when_unset_or_foreign(pc, tmp_path):
    """hooksPath 가 비었거나 다른 디렉토리면 False (= 훅 미배선)."""
    pool, _hooks, _repos = _wired_pool(tmp_path)
    assert pc.protected_hook_wired(
        "service-a", worktree_pool=pool, git_runner=HooksPathGit()) is False
    assert pc.protected_hook_wired(
        "service-a", worktree_pool=pool,
        git_runner=HooksPathGit(configured="/somewhere/else")) is False


def test_protected_hook_wired_none_when_undeterminable(pc, tmp_path):
    """bare 부재·git 실패는 None("모름") — False 로 단정하지 않는다(오탐 0)."""
    hooks = tmp_path / "hooks"
    no_bare = FakePool(hooks, repos_dir=tmp_path / "absent")
    assert pc.protected_hook_wired("service-a", worktree_pool=no_bare,
                                   git_runner=HooksPathGit()) is None
    pool, _hooks, _repos = _wired_pool(tmp_path)
    assert pc.protected_hook_wired("service-a", worktree_pool=pool,
                                   git_runner=HooksPathGit(broken=True)) is None


def test_protected_get_flags_unwired_hook_despite_fresh_sidecar(pc, board, tmp_path, capsys):
    """**should-fix**: sidecar 는 최신인데 hooksPath 가 안 걸렸으면 `✓ 정합` 이라고 하면 안 된다.

    `install_protected_hook` 은 sidecar 기록(2) → hooksPath 배선(3) 순서라 3 만 실패하면
    "목록은 최신인데 훅은 아예 안 도는" 부분성공이 된다 — 내용만 보면 정합으로 보이는 그 상태.
    """
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pool, hooks, _repos = _wired_pool(tmp_path)
    write_sidecar(hooks, "service-a", ["main"])          # 내용은 실효값과 동일
    pc.cmd_repo_protected(_protected_args("service-a"), board=board, worktree_pool=pool,
                          clone_runner=HooksPathGit())   # hooksPath 미설정
    out = capsys.readouterr().out
    assert "✓ 정합" not in out, "훅이 안 걸렸는데 정합으로 보고했다(거짓 정합)"
    assert "⚠ 훅 미배선" in out
    assert "보호 꺼짐" in out
    assert "repo protected service-a main" in out       # 재배선 안내


def test_protected_get_reports_wired_when_hookspath_set(pc, board, tmp_path, capsys):
    """배선까지 확인되면 `✓ 정합 (목록·hooksPath 배선)` — 두 축을 다 봤음을 밝힌다."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pool, hooks, _repos = _wired_pool(tmp_path)
    write_sidecar(hooks, "service-a", ["main"])
    pc.cmd_repo_protected(
        _protected_args("service-a"), board=board, worktree_pool=pool,
        clone_runner=HooksPathGit(configured=str((hooks / "service-a").resolve())))
    out = capsys.readouterr().out
    assert "✓ 정합 (목록·hooksPath 배선)" in out


def test_protected_get_wiring_unknown_is_honest(pc, board, tmp_path, capsys):
    """배선을 확인할 수 없으면(bare 부재) 그렇게 말한다 — 단정도 침묵도 아님."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "service-a", ["main"])
    pc.cmd_repo_protected(_protected_args("service-a"), board=board,
                          worktree_pool=FakePool(hooks, repos_dir=tmp_path / "absent"))
    out = capsys.readouterr().out
    assert "✓ 목록 정합 (배선 확인 불가" in out
    assert "⚠ 훅 미배선" not in out


def test_bootstrap_reconciles_when_sidecar_fresh_but_unwired(bootstrap, tmp_path,
                                                             monkeypatch, capsys):
    """**should-fix**: 내용이 같아도 hooksPath 가 끊겼으면 reconcile 이 침묵하면 안 된다.

    내용만 비교하던 조건은 이 상태에서 **영구 침묵**한다(보호가 꺼진 채로). 배선 축을 함께 봐
    재설치하고, 원인을 "목록이 바뀌어" 가 아니라 "배선돼 있지 않아" 로 정확히 보고한다.
    """
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["main"])
    pool = FakePool(hooks)
    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["main"]), pool=pool)
    monkeypatch.setattr(inst, "_protected_hook_wired", lambda repo: False)
    assert inst._reconcile_protected_sidecar("svc") is True
    assert pool.calls == [("svc", ["main"])]
    err = capsys.readouterr().err
    assert "배선돼 있지 않아" in err


def test_bootstrap_silent_when_content_and_wiring_both_ok(bootstrap, tmp_path,
                                                          monkeypatch, capsys):
    """두 축 모두 정합이면 여전히 subprocess 0·침묵(정상 상태에서 잡음 0)."""
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["main"])
    pool = FakePool(hooks)
    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["main"]), pool=pool)
    monkeypatch.setattr(inst, "_protected_hook_wired", lambda repo: True)
    assert inst._reconcile_protected_sidecar("svc") is False
    assert pool.calls == [] and capsys.readouterr().err == ""


def test_bootstrap_wiring_unknown_keeps_current_behaviour(bootstrap, tmp_path,
                                                          monkeypatch, capsys):
    """배선 판정이 None("모름")이면 드리프트로 치지 않는다 — 오탐 0(내용 비교만)."""
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["main"])
    pool = FakePool(hooks)
    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["main"]), pool=pool)
    monkeypatch.setattr(inst, "_protected_hook_wired", lambda repo: None)
    assert inst._reconcile_protected_sidecar("svc") is False
    assert pool.calls == []


def test_bootstrap_wiring_judgment_delegates_to_pm_config(bootstrap, pc, tmp_path):
    """배선 판정은 `pm_config.protected_hook_wired` 를 **소비**한다(재구현/복붙 아님).

    부트스트랩이 `_load_tool("pm_config")` 로 실제 그 심볼을 잡는지 + 같은 입력에서 판정이
    **동일**한지를 본다(로직이 갈라지면 값이 갈린다). ADR-0072 의 "두 벌 금지" 원칙 동형.
    """
    loaded = bootstrap._load_tool("pm_config")
    assert loaded is not None and hasattr(loaded, "protected_hook_wired"), \
        "pm_bootstrap 이 소비할 판정 심볼이 pm_config 에 없다(재구현으로 흘렀나?)"
    pool, hooks, _repos = _wired_pool(tmp_path, repo="svc")
    write_sidecar(hooks, "svc", ["main"])
    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["main"]), pool=pool)
    assert inst._protected_hook_wired("svc") == pc.protected_hook_wired(
        "svc", worktree_pool=pool)


# ════════════════════════════════════════════════════════════════════════
# 3. repo protected — 설정(areas → sidecar 순서·default·동기)
# ════════════════════════════════════════════════════════════════════════

def test_protected_set_updates_areas_then_sidecar(pc, board, tmp_path):
    """설정은 **areas → sidecar 순서 고정** — 역순이면 훅이 비준되지 않은 목록을 강제한다."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    events: list[str] = []
    pool = FakePool(tmp_path / "hooks", events=events)
    real_set = board.areas_set_cell

    def spy_set(repo, column, value):
        events.append("areas")
        return real_set(repo, column, value)

    board.areas_set_cell = spy_set
    try:
        rc = pc.cmd_repo_protected(_protected_args("service-a", "main,release"),
                                   board=board, worktree_pool=pool,
                                   repos_dir=tmp_path / ".repos")
    finally:
        board.areas_set_cell = real_set
    assert rc == 0
    assert events == ["areas", "sidecar"]
    assert board._repo_protected("service-a") == ["main", "release"]
    assert pool.calls == [("service-a", ["main", "release"])]


def test_protected_set_sidecar_receives_resolved_list(pc, board, tmp_path):
    """sidecar 는 areas 를 다시 읽어 resolve 된 목록을 받는다(파생 캐시·단일 진실 areas)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    hooks = tmp_path / "hooks"
    pool = FakePool(hooks)
    pc.cmd_repo_protected(_protected_args("service-b", "develop"), board=board,
                          worktree_pool=pool, repos_dir=tmp_path / ".repos")
    sidecar = (hooks / "service-b" / "protected").read_text(encoding="utf-8")
    assert sidecar.split() == ["develop"]


def test_protected_set_default_literal_clears_column(pc, board, tmp_path, capsys):
    """`default`(fold) = 칼럼 비움 → DEFAULT_PROTECTED 폴백 복귀(보호 해제가 아님)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    hooks = tmp_path / "hooks"
    pool = FakePool(hooks)
    rc = pc.cmd_repo_protected(_protected_args("service-a", "DEFAULT"), board=board,
                               worktree_pool=pool, repos_dir=tmp_path / ".repos")
    assert rc == 0
    _header, rows = board._parse_areas()
    assert rows[0]["protected"] == ""
    assert board._repo_protected("service-a") == list(board.DEFAULT_PROTECTED)
    assert pool.calls == [("service-a", list(board.DEFAULT_PROTECTED))]
    assert "기본값 폴백" in capsys.readouterr().out


def test_protected_set_triggers_board_git_sync(pc, board, tmp_path):
    """설정은 board-git best-effort 동기를 부른다(공유 정책 변경은 즉시 공유돼야)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pc.cmd_repo_protected(_protected_args("service-a", "release"), board=board,
                          worktree_pool=FakePool(tmp_path / "hooks"),
                          repos_dir=tmp_path / ".repos")
    assert board._sync_calls == ["repo protected"]


def test_protected_set_is_idempotent(pc, board, tmp_path):
    """같은 값 재설정은 rc 0 이고 결과가 동일하다(sidecar 재설치는 멱등 자가치유)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pool = FakePool(tmp_path / "hooks")
    for _ in range(2):
        assert pc.cmd_repo_protected(_protected_args("service-a", "main"), board=board,
                                     worktree_pool=pool,
                                     repos_dir=tmp_path / ".repos") == 0
    assert board._repo_protected("service-a") == ["main"]


def test_protected_set_upgrades_old_header(pc, board, tmp_path):
    """구 헤더(칼럼 부재) 레지스트리는 첫 설정에서 canonical 8칼럼으로 업그레이드된다(비파괴)."""
    board.AREAS_FILE.write_text(
        "| repo | prefix | git | test_cmd | owner |\n"
        "|---|---|---|---|---|\n"
        "| service-a | PAY | g | pytest -q | alice |\n",
        encoding="utf-8")
    rc = pc.cmd_repo_protected(_protected_args("service-a", "main,release"), board=board,
                               worktree_pool=FakePool(tmp_path / "hooks"),
                               repos_dir=tmp_path / ".repos")
    assert rc == 0
    assert board._areas_header_line() in board.AREAS_FILE.read_text(encoding="utf-8")
    assert board._repo_protected("service-a") == ["main", "release"]


def test_protected_set_warns_on_branch_absent_from_bare(pc, board, tmp_path, capsys):
    """bare 에 없는 브랜치는 **거부하지 않고** 경고 1줄(미래 브랜치 선-보호가 정상)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    repos = tmp_path / ".repos"
    (repos / "service-a.git").mkdir(parents=True)
    rc = pc.cmd_repo_protected(
        _protected_args("service-a", "main,release"), board=board,
        worktree_pool=FakePool(tmp_path / "hooks"),
        clone_runner=GitFake(existing=("main",)), repos_dir=repos)
    assert rc == 0                                    # 경고이지 거부 아님
    err = capsys.readouterr().err
    assert "release" in err and "없는 브랜치" in err
    assert board._repo_protected("service-a") == ["main", "release"]


def test_protected_set_no_warning_when_all_branches_exist(pc, board, tmp_path, capsys):
    """모든 브랜치가 bare 에 있으면 경고 없음(잡음 0)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    repos = tmp_path / ".repos"
    (repos / "service-a.git").mkdir(parents=True)
    pc.cmd_repo_protected(_protected_args("service-a", "main,develop"), board=board,
                          worktree_pool=FakePool(tmp_path / "hooks"),
                          clone_runner=GitFake(existing=("main", "develop")),
                          repos_dir=repos)
    assert "없는 브랜치" not in capsys.readouterr().err


def test_protected_set_sidecar_failure_is_loud_but_areas_kept(pc, board, tmp_path, capsys):
    """sidecar 설치 실패는 loud 경고 + 재실행 안내 — areas 는 이미 비준됐으므로 rc 0."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pool = FakePool(tmp_path / "hooks", ok=False)
    rc = pc.cmd_repo_protected(_protected_args("service-a", "release"), board=board,
                               worktree_pool=pool, repos_dir=tmp_path / ".repos")
    cap = capsys.readouterr()
    assert rc == 0
    assert board._repo_protected("service-a") == ["release"]
    assert "[경고]" in cap.err and "재실행" in cap.err
    # 실패인데 성공(✓) 알림이 나가면 안 된다 — 거짓 정합 보고 금지(must-fix 클래스).
    assert "✓ 보호 브랜치 pre-push 훅 정합화" not in cap.out


# ── fail-loud (부작용 0) ─────────────────────────────────────────────────────

def test_protected_set_duplicate_rows_fail_loud_no_sidecar(pc, board, tmp_path, capsys):
    """중복 repo 행 → rc 1 · areas 미변경 · sidecar 미호출(추측해서 한쪽만 고치지 않는다)."""
    dup = (
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| service-a | PAY | g | pytest -q | alice | develop | main | alice |\n"
        "| service-a | PAY | g | pytest -q | alice | develop | develop | alice |\n"
    )
    board.AREAS_FILE.write_text(dup, encoding="utf-8")
    pool = FakePool(tmp_path / "hooks")
    rc = pc.cmd_repo_protected(_protected_args("service-a", "release"), board=board,
                               worktree_pool=pool, repos_dir=tmp_path / ".repos")
    assert rc == 1
    assert pool.calls == [] and board._sync_calls == []
    assert board.AREAS_FILE.read_text(encoding="utf-8") == dup
    err = capsys.readouterr().err
    assert "중복" in err and "areas-duplicate-repo" in err


def test_protected_set_unregistered_repo_fail_loud(pc, board, tmp_path, capsys):
    """미등록 repo 설정 → rc 1 + `repo add` 안내(설정이 등록을 만들지 않는다)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pool = FakePool(tmp_path / "hooks")
    rc = pc.cmd_repo_protected(_protected_args("nope", "main"), board=board,
                               worktree_pool=pool, repos_dir=tmp_path / ".repos")
    assert rc == 1
    assert pool.calls == []
    assert "repo add nope" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["", "  ", "main,,develop", "ma in", "a|b"])
def test_protected_set_bad_value_rejected_before_write(pc, board, tmp_path, bad, capsys):
    """형식 위반 값은 areas 쓰기 **이전** 거부 — 파일 미변경 · sidecar 미호출."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pool = FakePool(tmp_path / "hooks")
    rc = pc.cmd_repo_protected(_protected_args("service-a", bad), board=board,
                               worktree_pool=pool, repos_dir=tmp_path / ".repos")
    assert rc == 1
    assert pool.calls == []
    assert board.AREAS_FILE.read_text(encoding="utf-8") == _CANONICAL_AREAS
    assert "[중단]" in capsys.readouterr().err


def test_protected_set_empty_value_guides_to_default(pc, board, tmp_path, capsys):
    """빈 값 거부 메시지가 `default` 리터럴을 안내한다("보호 없음" 표현 불가)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pc.cmd_repo_protected(_protected_args("service-a", ""), board=board,
                          worktree_pool=FakePool(tmp_path / "hooks"))
    err = capsys.readouterr().err
    assert "default" in err and "표현 불가" in err


def test_protected_bad_repo_name_rejected(pc, board, tmp_path, capsys):
    """repo 이름 형식 위반은 areas 를 전혀 건드리지 않는다(폴더탈출·줄 corruption 방지)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    rc = pc.cmd_repo_protected(_protected_args("../evil", "main"), board=board,
                               worktree_pool=FakePool(tmp_path / "hooks"))
    assert rc == 1
    assert board.AREAS_FILE.read_text(encoding="utf-8") == _CANONICAL_AREAS
    assert "형식 위반" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# 4. repo list
# ════════════════════════════════════════════════════════════════════════

def test_repo_list_renders_registry_columns(pc, board, capsys):
    """등록 repo 표에 repo·prefix·base·protected·test_cmd·area_owner 가 나온다."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    rc = pc.cmd_repo_list(argparse.Namespace(), board=board)
    out = capsys.readouterr().out
    assert rc == 0
    for column in ("repo", "prefix", "base", "protected", "test_cmd", "area_owner"):
        assert column in out
    assert "service-a" in out and "service-b" in out
    assert "develop" in out and "pytest -q" in out


def test_repo_list_marks_empty_protected_as_default(pc, board, capsys):
    """빈 `protected` 는 "기본값" 을 명시한다 — 빈 칸을 "보호 없음" 으로 오독하지 않게."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pc.cmd_repo_list(argparse.Namespace(), board=board)
    out = capsys.readouterr().out
    assert "main,master,develop · 기본값" in out


def test_repo_list_aligns_columns_by_display_width(pc, board, capsys):
    """한글 셀이 섞여도 표 정렬이 안 깨진다 — 폭은 `len()` 이 아니라 **표시 폭**(전각 2칸).

    이 프로젝트 출력은 사실상 전부 한국어라 실사용에서 계속 보이는 문제다(reviewer 실측).
    """
    board.AREAS_FILE.write_text(
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| aa | P | g | 테스트 명령 | o | main | main | 한글이름 |\n"
        "| bb | Q | g | pytest -q | o | main | main | ascii |\n",
        encoding="utf-8")
    pc.cmd_repo_list(argparse.Namespace(), board=board)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(("aa", "bb"))]
    assert len(lines) == 2
    # 두 데이터 행에서 마지막 칼럼(area_owner)의 시작 표시 폭이 같아야 정렬이 맞다.
    starts = [pc._display_width(ln[:ln.index("한글이름" if "한글이름" in ln else "ascii")])
              for ln in lines]
    assert starts[0] == starts[1], f"한글 셀에서 정렬이 깨졌다: {lines}"


def test_display_width_counts_wide_chars_as_two(pc):
    """`_display_width` 단위 — 전각 2칸·반각 1칸(패딩 헬퍼도 그 기준)."""
    assert pc._display_width("abc") == 3
    assert pc._display_width("한글") == 4
    assert pc._display_width("한a") == 3
    assert pc._display_width("") == 0
    assert pc._display_width(pc._pad_display("한글", 8)) == 8
    assert pc._pad_display("abcdef", 3) == "abcdef"   # 이미 넘치면 자르지 않는다


def test_repo_list_empty_registry_guides_to_repo_add(pc, board, capsys):
    """등록이 없으면 빈 표 대신 `repo add` 안내(rc 0)."""
    assert pc.cmd_repo_list(argparse.Namespace(), board=board) == 0
    assert "등록된 repo 없음" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════════════
# 5. CLI 배선 (파서 → 핸들러)
# ════════════════════════════════════════════════════════════════════════

def test_cli_repo_protected_get_parses_without_value(pc):
    """`repo protected <name>` 은 value 없이 파싱돼 조회 핸들러로 간다."""
    args = pc.build_parser().parse_args(["repo", "protected", "svc"])
    assert args.func is pc.cmd_repo_protected
    assert args.name == "svc" and args.value is None


def test_cli_repo_protected_set_parses_value(pc):
    """`repo protected <name> <목록>` 은 value 를 받아 설정 경로로 간다."""
    args = pc.build_parser().parse_args(["repo", "protected", "svc", "main,release"])
    assert args.value == "main,release"


def test_cli_repo_add_accepts_protected_flag(pc):
    """`repo add --protected` 플래그가 파서에 있다(종전 "후속 플래그 여지" 자인 해소)."""
    args = pc.build_parser().parse_args(
        ["repo", "add", "svc", "--git", "u", "--protected", "main,develop"])
    assert args.protected == "main,develop"


def test_cli_repo_list_parses(pc):
    """`repo list` 가 조회 핸들러로 간다."""
    args = pc.build_parser().parse_args(["repo", "list"])
    assert args.func is pc.cmd_repo_list


# ════════════════════════════════════════════════════════════════════════
# 6. bootstrap phase-0 drift-only reconcile (ADR-0072 트리거 ②)
# ════════════════════════════════════════════════════════════════════════

_CELL_DEFAULT = object()   # `_ReconcileBoard(cell=…)` 미지정 sentinel (None=미등록과 구별).


class _ReconcileBoard:
    """board 대역 — 보호목록 seam(`_repo_protected`) + areas raw 셀 + 앵커 가드(phase-0 통과용).

    `cell` 은 areas `protected` **raw 셀** 대역 — 재실행 안내 분기(명시/폴백/미등록)의 입력이다.
    기본은 명시 설정(그 목록), `cell=""` 면 기본값 폴백 상태, `cell=None` 이면 미등록(행 없음).
    """

    def __init__(self, protected, *, repo="svc", cell=_CELL_DEFAULT):
        self._protected = list(protected)
        self._repo = repo
        self._cell = ",".join(self._protected) if cell is _CELL_DEFAULT else cell

    def _repo_protected(self, repo):
        return list(self._protected)

    def _parse_areas(self):
        if self._cell is None:
            return [], []                      # 미등록(행 없음)
        return [], [{"repo": self._repo, "protected": self._cell}]

    def _pm_home_worktree_misanchor(self, anchor, **_kw):
        return None


def _reconciler(bootstrap, tmp_path, *, board, pool):
    """`_reconcile_protected_sidecar` 만 구동하는 최소 PmBootstrap (실 IO 0)."""
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("| repo | prefix |\n|---|---|\n| X | X |\n", encoding="utf-8")
    return bootstrap.PmBootstrap(
        run_board_fn=lambda args: (0, ""),
        run_pytest_fn=lambda: (0, ""),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        areas_file=areas_file,
        worktree_pool=pool,
        board=board,
    )


def test_bootstrap_reconciles_sidecar_on_drift(bootstrap, tmp_path, capsys):
    """sidecar 가 areas 와 다르면 재설치한다 — *다른 clone* 의 변경을 세션 시작에 흡수."""
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["main"])
    pool = FakePool(hooks)
    inst = _reconciler(bootstrap, tmp_path,
                       board=_ReconcileBoard(["main", "release"]), pool=pool)
    assert inst._reconcile_protected_sidecar("svc") is True
    assert pool.calls == [("svc", ["main", "release"])]
    assert (hooks / "svc" / "protected").read_text(encoding="utf-8").split() == [
        "main", "release"]
    assert "정합화" in capsys.readouterr().err


def test_bootstrap_reconcile_failure_is_loud_and_not_reported_as_success(
        bootstrap, tmp_path, capsys):
    """**must-fix**: `install_protected_hook` 이 (예외 없이) False 면 성공 알림이 나가면 안 된다.

    `install_protected_hook` 은 bare 부재·`core.hooksPath` 설정 실패를 **False 반환**으로 알린다.
    그걸 무시하고 "정합화했습니다" 를 내면 이 티켓이 닫으려던 값-연결 끊김을 *정합하다고 거짓
    보고*하는 것 — 사용자가 확인할 이유를 없앤다(원래 문제보다 나쁘다). fail-soft(진입 무차단)는
    유지하되 실패는 loud: "정합화하지 못했습니다" + 옛 목록 + 재실행 커맨드.
    """
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["main"])
    pool = FakePool(hooks, ok=False)
    inst = _reconciler(bootstrap, tmp_path,
                       board=_ReconcileBoard(["main", "release"]), pool=pool)
    assert inst._reconcile_protected_sidecar("svc") is False
    assert pool.calls == [("svc", ["main", "release"])]      # 시도는 했다
    err = capsys.readouterr().err
    assert "정합화했습니다" not in err, "실패인데 성공 보고가 나갔다(거짓 정합)"
    assert "정합화하지 못했습니다" in err
    assert "옛 목록" in err and "main" in err                 # 아직 강제되는 목록을 밝힌다
    assert "repo protected svc main,release" in err          # 재실행 커맨드
    # sidecar 는 실제로 stale 그대로다(거짓 보고가 아니라는 실증).
    assert (hooks / "svc" / "protected").read_text(encoding="utf-8").split() == ["main"]


def test_bootstrap_reconcile_exception_is_loud_too(bootstrap, tmp_path, capsys):
    """drift 확정 후 재설치가 raise 해도 조용하지 않다 — 같은 실패 안내(fail-soft ≠ 침묵)."""
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["main"])

    class _Boom(FakePool):
        def install_protected_hook(self, repo, protected, *, git_runner=None):
            raise RuntimeError("hooksPath 설정 실패")

    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["release"]),
                       pool=_Boom(hooks))
    assert inst._reconcile_protected_sidecar("svc") is False
    err = capsys.readouterr().err
    assert "정합화하지 못했습니다" in err and "RuntimeError" in err


def test_bootstrap_no_reconcile_when_in_sync(bootstrap, tmp_path, capsys):
    """정합이면 아무것도 하지 않는다 — subprocess 0(매 부트스트랩이 git config 를 안 때린다)."""
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["main", "release"])
    pool = FakePool(hooks)
    inst = _reconciler(bootstrap, tmp_path,
                       board=_ReconcileBoard(["main", "release"]), pool=pool)
    assert inst._reconcile_protected_sidecar("svc") is False
    assert pool.calls == []
    assert capsys.readouterr().err == ""


def test_bootstrap_reconcile_skips_when_sidecar_absent(bootstrap, tmp_path):
    """훅 미설치(sidecar 부재)면 설치하지 않는다 — 설치는 repo add/worktree add 축."""
    pool = FakePool(tmp_path / "hooks")
    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["main"]), pool=pool)
    assert inst._reconcile_protected_sidecar("svc") is False
    assert pool.calls == []


def test_bootstrap_reconcile_failsoft_without_board(bootstrap, tmp_path, monkeypatch):
    """board 부재는 fail-soft — 판정 생략(세션 진입 무영향)."""
    monkeypatch.setattr(bootstrap, "_load_board", lambda: None)
    pool = FakePool(tmp_path / "hooks")
    inst = _reconciler(bootstrap, tmp_path, board=None, pool=pool)
    assert inst._reconcile_protected_sidecar("svc") is False
    assert pool.calls == []


def test_bootstrap_reconcile_failsoft_without_pool(bootstrap, tmp_path, monkeypatch):
    """worktree_pool 부재도 fail-soft — 재설치 seam 이 없으면 조용히 생략."""
    monkeypatch.setattr(bootstrap, "_load_worktree_pool", lambda: None)
    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["main"]), pool=None)
    assert inst._reconcile_protected_sidecar("svc") is False


def test_bootstrap_reconcile_failsoft_on_install_exception(bootstrap, tmp_path):
    """재설치가 raise 해도 삼킨다(보호 훅은 추가 가드 — 진입을 막지 않는다)."""
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["main"])

    class _Boom(FakePool):
        def install_protected_hook(self, repo, protected, *, git_runner=None):
            raise RuntimeError("boom")

    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["release"]),
                       pool=_Boom(hooks))
    assert inst._reconcile_protected_sidecar("svc") is False


def test_phase0_invokes_sidecar_reconcile(bootstrap, tmp_path, monkeypatch):
    """0단계가 (슬롯 검사 전에) reconcile 을 부른다 — 그 세션의 첫 커밋보다 앞."""
    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["main"]),
                       pool=FakePool(tmp_path / "hooks"))
    seen: list[str] = []
    monkeypatch.setattr(inst, "_reconcile_protected_sidecar",
                        lambda repo: seen.append(repo) or False)
    # slot=None(alloc 경로) — 슬롯 검사는 자연 no-op 이라 reconcile 배선만 남는다.
    assert inst._phase0_preflight("svc", None) == 0
    assert seen == ["svc"]


def test_phase0_solo_does_not_reconcile(bootstrap, tmp_path, monkeypatch):
    """solo(repo 없음)는 대상 repo 가 없어 reconcile 도 자연 no-op."""
    inst = _reconciler(bootstrap, tmp_path, board=_ReconcileBoard(["main"]),
                       pool=FakePool(tmp_path / "hooks"))
    seen: list[str] = []
    monkeypatch.setattr(inst, "_reconcile_protected_sidecar",
                        lambda repo: seen.append(repo) or False)
    assert inst._phase0_preflight(None, None) == 0
    assert seen == []


# ════════════════════════════════════════════════════════════════════════
# 7. 재실행 안내가 현재 상태를 반영한다 (명시 / 기본값 폴백 / 미등록 · 단일 분기)
# ════════════════════════════════════════════════════════════════════════

def test_retry_command_uses_default_literal_when_falling_back(pc, board):
    """폴백 상태(빈 칼럼)의 안내는 `default` — 명시 커맨드를 안내하면 출처가 조용히 바뀐다."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    cmd = pc.protected_retry_command("service-b", board=board)
    assert cmd.endswith("repo protected service-b default")
    assert "main,master,develop" not in cmd


def test_retry_command_uses_current_list_when_explicit(pc, board):
    """명시 설정 상태면 그 목록을 그대로 재적용하는 커맨드(멱등)."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    assert pc.protected_retry_command("service-a", board=board).endswith(
        "repo protected service-a main")


def test_retry_command_points_to_repo_add_when_unregistered(pc, board):
    """미등록이면 셀을 고칠 대상이 없다 — `repo add` 가 먼저다."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    assert pc.protected_retry_command("nope", board=board).endswith("repo add nope")


def test_default_reset_failure_retry_is_not_repo_add(pc, board, tmp_path, capsys):
    """`default` 로 되돌린 뒤 sidecar 실패 시 안내가 `repo add` 로 떨어지면 안 된다.

    빈 토큰 리스트가 falsy 라 `repo add` 로 새던 경로 — 기본값 복귀는 `repo protected … default`
    로 재실행해야 같은 상태가 복원된다.
    """
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    rc = pc.cmd_repo_protected(_protected_args("service-a", "default"), board=board,
                               worktree_pool=FakePool(tmp_path / "hooks", ok=False),
                               repos_dir=tmp_path / ".repos")
    assert rc == 0
    err = capsys.readouterr().err
    assert "repo protected service-a default" in err
    assert "repo add service-a" not in err


def test_query_guidance_in_fallback_state_offers_default(pc, board, tmp_path, capsys):
    """폴백 상태의 drift 안내는 `default` — `main,master,develop` 명시 커맨드가 나오면 안 된다."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "service-b", ["master"])        # sidecar drift
    pc.cmd_repo_protected(_protected_args("service-b"), board=board,
                          worktree_pool=FakePool(hooks))
    out = capsys.readouterr().out
    assert "repo protected service-b default" in out
    assert "repo protected service-b main,master,develop" not in out


def test_query_guidance_in_explicit_state_keeps_list(pc, board, tmp_path, capsys):
    """명시 상태의 drift 안내는 종전대로 그 목록을 싣는다."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "service-a", ["master"])
    pc.cmd_repo_protected(_protected_args("service-a"), board=board,
                          worktree_pool=FakePool(hooks))
    assert "repo protected service-a main" in capsys.readouterr().out


def test_unwired_guidance_in_fallback_state_offers_default(pc, board, tmp_path, capsys):
    """미배선 안내(배선 축)도 같은 분기를 탄다 — 폴백이면 `default`."""
    board.AREAS_FILE.write_text(_CANONICAL_AREAS, encoding="utf-8")
    pool, hooks, _repos = _wired_pool(tmp_path, repo="service-b")
    write_sidecar(hooks, "service-b", list(board.DEFAULT_PROTECTED))
    pc.cmd_repo_protected(_protected_args("service-b"), board=board, worktree_pool=pool,
                          clone_runner=HooksPathGit())
    out = capsys.readouterr().out
    assert "⚠ 훅 미배선" in out
    assert "repo protected service-b default" in out
    assert "repo protected service-b main,master,develop" not in out


def test_bootstrap_failure_guidance_in_fallback_state_offers_default(
        bootstrap, tmp_path, capsys):
    """bootstrap reconcile 실패 안내도 폴백 상태면 `default` — 같은 단일 분기를 소비한다.

    폴백(빈 칼럼)인데 `main,master,develop` 명시 커맨드를 안내하면, 그걸 실행한 사용자가
    출처를 "명시" 로 바꾸게 된다(안내가 상태를 조용히 바꾼다).
    """
    hooks = tmp_path / "hooks"
    write_sidecar(hooks, "svc", ["master"])
    inst = _reconciler(
        bootstrap, tmp_path,
        board=_ReconcileBoard(["main", "master", "develop"], cell=""),   # 폴백 상태
        pool=FakePool(hooks, ok=False))
    assert inst._reconcile_protected_sidecar("svc") is False
    err = capsys.readouterr().err
    assert "repo protected svc default" in err
    assert "repo protected svc main,master,develop" not in err


def test_bootstrap_retry_command_delegates_to_pm_config(bootstrap, pc, tmp_path):
    """부트스트랩이 분기를 재구현하지 않고 `pm_config.protected_retry_command` 를 소비한다."""
    loaded = bootstrap._load_tool("pm_config")
    assert loaded is not None and hasattr(loaded, "protected_retry_command"), \
        "부트스트랩이 소비할 안내 분기 심볼이 pm_config 에 없다(재구현으로 흘렀나?)"
    fake_board = _ReconcileBoard(["main"], cell="")
    inst = _reconciler(bootstrap, tmp_path, board=fake_board,
                       pool=FakePool(tmp_path / "hooks"))
    assert inst._protected_retry_command("svc") == pc.protected_retry_command(
        "svc", board=fake_board)


# ════════════════════════════════════════════════════════════════════════
# 8. 훅 설치 보고 단일 깔때기 (must-fix 클래스 기계 가드)
# ════════════════════════════════════════════════════════════════════════

def test_install_reporting_funnel_success_and_failure(pc, monkeypatch, capsys):
    """공용 깔때기: 성공은 `✓` 1줄, 실패는 stderr 경고 + 재실행 커맨드(둘 다 조용하지 않다)."""
    monkeypatch.setattr(pc, "_install_protected_hook", lambda repo, **_kw: True)
    assert pc._install_protected_hook_reporting("svc", action="설치") is True
    cap = capsys.readouterr()
    # 설치자는 두 훅(pre-push·T-0076 / pre-commit·T-0415)을 함께 깐다 — 성공 문구도 둘 다 밝힌다.
    assert "✓ 보호 브랜치 pre-push + pre-commit 훅 설치: svc" in cap.out and cap.err == ""

    monkeypatch.setattr(pc, "_install_protected_hook", lambda repo, **_kw: False)
    assert pc._install_protected_hook_reporting(
        "svc", action="정합화", retry="pm-config repo protected svc main,release") is False
    cap = capsys.readouterr()
    assert cap.out == "", "실패인데 성공 알림이 나갔다"
    assert "[경고]" in cap.err
    assert "repo protected svc main,release" in cap.err


def test_no_raw_install_protected_hook_call_sites_remain(pc):
    """`_install_protected_hook` 직접 호출부가 남아 있지 않다 — 전부 보고 깔때기를 탄다.

    실패를 `if _install_protected_hook(...): print(성공)` 으로 소비하면 False 가 침묵한다
    (must-fix 1 과 같은 클래스). 새 호출부가 그 패턴으로 다시 들어오는 것을 기계로 막는다 —
    유일한 raw 호출부는 깔때기(`_install_protected_hook_reporting`) 안이어야 한다.
    """
    source = (TOOLS / "pm_config.py").read_text(encoding="utf-8").splitlines()
    raw = [
        (i + 1, line.strip()) for i, line in enumerate(source)
        if "_install_protected_hook(" in line
        and "_install_protected_hook_reporting(" not in line
        and not line.lstrip().startswith(("def ", "#", "`"))
        and "getattr" not in line
    ]
    # 깔때기 본문의 1회 호출만 허용.
    assert len(raw) == 1, f"보고 깔때기를 우회하는 직접 호출부: {raw}"
    assert raw[0][1].startswith("if _install_protected_hook(repo, board=board")


# ════════════════════════════════════════════════════════════════════════
# hermetic 입증 — 실 루트 areas.md 무오염
# ════════════════════════════════════════════════════════════════════════

def test_real_root_areas_md_untouched():
    """이 모듈 실행이 실 루트 areas.md 를 만들지 않았음을 입증한다 (hermetic 가드)."""
    assert not REAL_AREAS.exists(), (
        f"실 루트 areas.md 가 생성됨 ({REAL_AREAS}) — hermetic 격리 위반")
