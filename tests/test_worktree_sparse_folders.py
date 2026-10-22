"""슬롯 폴더 제한 테스트 — `create_slot`·`alloc` 의 `folders` (git sparse-checkout cone · T-0926).

큰 저장소에서 슬롯마다 전체를 꺼내는 부담을 줄이려 슬롯에 일부 폴더만 꺼낸다. 폴더 목록의 진실은
그 슬롯의 git 설정이고 대여 장부에는 적지 않는다. 이 파일이 고정하는 것:

  - 생성: 전체를 디스크에 쓰지 않는다(`--no-checkout` → `sparse-checkout set --cone` → `checkout`) ·
    폴더 밖 파일은 추적(S 표시)으로 남고 커밋에서 빠지지 않는다.
  - 대여: 목록이면 그 cone, 없으면 전체(앞 제한을 푼다) · 커밋 안 한 변경이 있으면 거부.
  - 폴더 없는 생성·대여는 공용 설정(`extensions.worktreeConfig`·`config.worktree`)을 건드리지 않는다.
  - 잘못된 폴더·git 2.36 미만은 부작용 전에 거부한다.
  - 폴더 밖 submodule 은 초기화하지 않고 현황·sync 가 세지 않는다.
  - 현황의 폴더 목록은 git 이 가진 cone 목록 그대로다(한글 원문·cone 이 아니면 거부).

hermetic: worktree_pool 경로 전역은 import 시점에 실 repo 로 굳으므로 tmp 프로젝트로 재지정한다
(`tests/test_worktree_pool.py` 의 `_load_wp_bound` 동형). 실 git 형상은 로컬 origin → bare clone
(`.repos/A.git` · fetch refspec 보정)이고, 순서·argv 만 보는 테스트는 기록 러너를 쓴다.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from _git import commit_env
from pm_engine.app import branch_checkout
from pm_engine.infra import pm_paths
from pm_engine.infra import pool_store
from pm_engine.infra import slot_snapshot
from pm_engine.infra import sparse_checkout
from pm_engine.infra import submodule_git
from pm_engine.infra import worktree_git
from pm_engine.app import worktree_pool
from pm_engine.domain import worktree_records
from pm_engine.app import worktree_status

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")

# origin 트리 — 폴더 `a`(하위 `a/sub`)·`b`·`c`(하위 `c/deep`)·한글 폴더와 최상위 파일 하나.
_ORIGIN_FILES = {
    "R.md": "root\n",
    "a/x": "x\n",
    "a/sub/z": "z\n",
    "b/y": "y\n",
    "c/deep/f": "f\n",
    "c/top": "top\n",
    "한글/h": "h\n",
}
# 부작용 전에 거부해야 하는 폴더 입력 — 없는 폴더·파일·submodule·루트 밖·절대경로·역슬래시·빈 항목·
# 점 하나·빈 조각. `sub` 은 gitlink 만 커밋한 submodule 경로다.
_INVALID_FOLDERS = ["nope", "R.md", "sub", "../x", "/a", "a\\b", "", ".", "a//b", "a/../b", "C:/a", "a\0b"]


# ── 모듈 로드 + tmp 재배선 ────────────────────────────────────────────────────


def _load_wp_bound(proj: Path):
    """worktree_pool.py 를 새로 로드하고 경로 전역을 `proj` tmp 루트로 재바인딩한다."""
    spec = importlib.util.spec_from_file_location("wp_sparse_folders", TOOLS / "worktree_pool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    local = proj / ".project_manager" / ".local"
    for name, value in {
        "REPO": proj,
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "LEASES_LOCK": local / "worktree-leases.lock",
        "TASKS_FILE": local / "tasks.json",
        "TASKS_DIR": local / "tasks",
        "WORK_DIR": proj / "work",
        "REPOS_DIR": proj / ".repos",
        "REPO_HOOKS_DIR": local / "repo-hooks",
    }.items():
        setattr(mod, name, value)
    return mod


def _git(cwd: Path, *argv: str) -> str:
    """실 git 한 번 — 실패는 rc·stderr 를 실은 단언으로 멈춘다(작성자 정체 고정·UTF-8)."""
    done = subprocess.run([_GIT, "-C", str(cwd), *argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=commit_env(), check=False)
    assert done.returncode == 0, f"git {' '.join(argv)} (rc={done.returncode}): {done.stderr}"
    return done.stdout


def _git_rc(cwd: Path, *argv: str) -> int:
    return subprocess.run([_GIT, "-C", str(cwd), *argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=commit_env(),
                          check=False).returncode


@pytest.fixture
def proj(tmp_path):
    """tmp 프로젝트 루트(자기 git 저장소 선언) — `.project_manager/.local` + `work/` + `.repos/`."""
    root = tmp_path / "proj"
    (root / ".project_manager" / ".local").mkdir(parents=True)
    (root / "work").mkdir()
    (root / ".repos").mkdir()
    if _GIT is not None:
        _git(root, "init", "-q", "-b", "main")
        _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


@pytest.fixture
def wp(proj):
    return _load_wp_bound(proj)


# ── 실 git 형상 ──────────────────────────────────────────────────────────────


def _make_origin(root: Path) -> Path:
    origin = root / "A-origin"
    origin.mkdir(parents=True)
    _git(origin, "init", "-q", "-b", "main")
    for relative, text in _ORIGIN_FILES.items():
        path = origin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "tree")
    return origin


def _allow_file_submodules(monkeypatch) -> None:
    """모든 git 호출에 `protocol.file.allow=always` — 로컬 경로 submodule clone 차단 해제(테스트 전용)."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def _make_origin_with_submodules(root: Path) -> Path:
    """origin 트리 + submodule 셋 — 폴더 안 `a/subm` · 폴더 밖 `c/subm` · 최상위 `rootsub`."""
    sub = root / "sub-origin"
    sub.mkdir(parents=True)
    _git(sub, "init", "-q", "-b", "main")
    (sub / "lib.txt").write_text("lib\n", encoding="utf-8")
    _git(sub, "add", "lib.txt")
    _git(sub, "commit", "-q", "-m", "lib")
    origin = _make_origin(root)
    for path in ("a/subm", "c/subm", "rootsub"):
        _git(origin, "submodule", "add", "-q", str(sub), path)
    _git(origin, "commit", "-q", "-m", "submodules")
    return origin


def _make_bare(wp, origin: Path) -> Path:
    """`.repos/A.git` bare clone + remote-tracking refspec — 슬롯이 `origin/main` 에서 난다."""
    bare = pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A")
    _git(origin.parent, "clone", "--bare", "-q", str(origin), str(bare))
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")
    return bare


def _tags(slot_dir: Path) -> dict[str, str]:
    """`git ls-files -t` — 경로 → 태그(`H` 꺼냄 · `S` 폴더 제한으로 꺼내지 않음)."""
    records = _git(slot_dir, "ls-files", "-t", "-z").split("\0")
    return {record[2:]: record[0] for record in records if record}


def _lease_row(wp, slot: str):
    return next(lease for lease in pool_store.list_leases(pm_paths.home_at(wp.REPO)) if lease.slot == slot)


def _worktree_config_files(bare: Path) -> list[Path]:
    """공용 저장소와 슬롯별 git 관리 폴더(`worktrees/<이름>/`)의 `config.worktree` 중 실재하는 것."""
    candidates = [bare / "config.worktree"]
    admin = bare / "worktrees"
    if admin.is_dir():
        candidates += [entry / "config.worktree" for entry in admin.iterdir()]
    return [path for path in candidates if path.exists()]


def _assert_shared_config_untouched(bare: Path, baseline: bytes) -> None:
    assert (bare / "config").read_bytes() == baseline, "공용 config 가 바뀌었다"
    assert _git_rc(bare, "config", "--get", "extensions.worktreeConfig") == 1
    assert _worktree_config_files(bare) == [], "config.worktree 가 생겼다"


# ── 기록 러너 (순서·argv 만 본다) ────────────────────────────────────────────


class _RecordingGit:
    """호출을 기록하고 git 버전·폴더 트리·설정 응답만 모델링하는 러너."""

    def __init__(self, *, version: str = "git version 2.43.0", trees: "tuple[str, ...]" = ("a",)):
        self.version = version
        self.trees = set(trees)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list) -> "tuple[int, str]":
        self.calls.append(list(argv))
        if "--is-bare-repository" in argv:
            return 0, "true\n"
        if argv[:2] == ["rev-parse", "--verify"]:
            return 0, "a1b2c3d4" * 5 + "\n"
        if argv == ["version"]:
            return 0, self.version + "\n"
        if argv[:2] == ["ls-tree", "-z"]:
            folder = argv[-1]
            return 0, (f"040000 tree {'0' * 40}\t{folder}\0" if folder in self.trees else "")
        if argv[:1] == ["config"] and "--get" in argv:
            return 1, ""               # 값 없음 — 폴더 제한 없는 슬롯.
        return 0, ""

    def first(self, *prefix: str) -> int:
        return next(index for index, call in enumerate(self.calls)
                    if call[:len(prefix)] == list(prefix))


# ════════════════════════════════════════════════════════════════════════
# 생성
# ════════════════════════════════════════════════════════════════════════


@_git_required
def test_create_slot_with_folders_checks_out_only_the_cone(wp, tmp_path):
    """`folders=[a]` 생성 — 최상위 파일과 `a/**` 만 디스크에 있고 나머지는 추적(S)으로 남는다."""
    _make_bare(wp, _make_origin(tmp_path))

    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False,
                           folders=["a"])

    slot_dir = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    for relative in ("R.md", "a/x", "a/sub/z"):
        assert (slot_dir / relative).is_file(), relative
    for relative in ("b/y", "c/deep/f", "c/top"):
        assert not (slot_dir / relative).exists(), relative
    tags = _tags(slot_dir)
    assert tags["b/y"] == "S" and tags["c/deep/f"] == "S" and tags["a/x"] == "H"
    assert _git(slot_dir, "status", "--porcelain") == ""
    assert _git(slot_dir, "sparse-checkout", "list").splitlines() == ["a"]
    # 폴더 밖 파일은 커밋에서 빠지지 않는다 — 브랜치 내용은 그대로다.
    (slot_dir / "a" / "new.txt").write_text("new\n", encoding="utf-8")
    _git(slot_dir, "add", "a/new.txt")
    _git(slot_dir, "commit", "-q", "-m", "slot work")
    committed = set(_git(slot_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    assert {"b/y", "c/deep/f", "c/top", "a/new.txt"} <= committed


def test_create_slot_with_folders_never_writes_the_full_tree(wp):
    """호출 순서 — 버전 확인 → 폴더 검증 → `worktree add --no-checkout` → 규칙 → 인자 없는 checkout.

    폴더 없는 생성은 현행 argv 그대로이고 폴더 제한 호출이 하나도 없다."""
    pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A").mkdir(parents=True)
    git = _RecordingGit()
    worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", git_runner=git, folders=["a"])

    order = [
        git.first("version"),
        git.first("ls-tree", "-z", "origin/main", "--", "a"),
        git.first("worktree", "add", "--no-checkout"),
        git.first("sparse-checkout", "set", "--cone", "--", "a"),
        git.calls.index(["checkout", "--no-recurse-submodules"]),
    ]
    assert order == sorted(order), git.calls
    add = next(call for call in git.calls if call[:2] == ["worktree", "add"])
    assert add == ["worktree", "add", "--no-checkout", "--no-track", "-b", "A_1",
                   str(pm_paths.slot_path(pm_paths.home_at(wp.REPO), "work/A_1")), "origin/main"]

    plain = _RecordingGit()
    worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", git_runner=plain)
    add = next(call for call in plain.calls if call[:2] == ["worktree", "add"])
    assert add == ["worktree", "add", "--no-track", "-b", "A_2",
                   str(pm_paths.slot_path(pm_paths.home_at(wp.REPO), "work/A_2")), "origin/main"]
    assert not any(call[:1] in (["sparse-checkout"], ["ls-tree"], ["version"])
                   for call in plain.calls), plain.calls
    assert ["checkout", "--no-recurse-submodules"] not in plain.calls
    assert ["submodule", "update", "--init", "--recursive", "--force"] in plain.calls


@_git_required
def test_slots_without_folders_never_touch_the_shared_config(wp, tmp_path):
    """폴더 없는 생성·대여·반납·재대여는 공용 config 바이트를 바꾸지 않는다(제한 없던 슬롯에 disable 없음)."""
    bare = _make_bare(wp, _make_origin(tmp_path))
    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False)
    baseline = (bare / "config").read_bytes()
    _assert_shared_config_untouched(bare, baseline)

    leased = worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t")
    assert leased.slot == lease.slot and leased.state == "leased"
    _assert_shared_config_untouched(bare, baseline)
    worktree_pool.release(pm_paths.home_at(wp.REPO), lease.slot, owner_task="t")
    _assert_shared_config_untouched(bare, baseline)
    worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t2")
    _assert_shared_config_untouched(bare, baseline)


@_git_required
def test_folder_limit_stays_in_its_slot(wp, tmp_path):
    """폴더 규칙은 그 슬롯에만 걸린다 — 공용 저장소는 bare 그대로, 옆 슬롯은 전체·깨끗하고 제한을 풀어도
    옆 슬롯은 바뀌지 않는다."""
    bare = _make_bare(wp, _make_origin(tmp_path))
    limited = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False,
                             folders=["a"])
    full = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False)
    full_dir = pm_paths.slot_path(pm_paths.home_at(wp.REPO), full.slot)
    # 처음 폴더 제한을 걸면 git 이 bare 의 `core.bare` 를 `config.worktree` 로 옮긴다(공용 설정 변화).
    assert bare / "config.worktree" in _worktree_config_files(bare)

    def assert_full_slot_untouched():
        assert _git(bare, "rev-parse", "--is-bare-repository").strip() == "true"
        assert _git(full_dir, "rev-parse", "--is-bare-repository").strip() == "false"
        for relative in _ORIGIN_FILES:
            assert (full_dir / relative).is_file(), relative
        assert _git(full_dir, "status", "--porcelain") == ""
        assert _git_rc(full_dir, "config", "--get", "core.sparseCheckout") == 1

    assert_full_slot_untouched()
    leased = worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t")          # 최소 번호 = 폴더 제한 슬롯 — 제한을 푼다
    assert leased.slot == limited.slot
    assert (pm_paths.slot_path(pm_paths.home_at(wp.REPO), limited.slot) / "b" / "y").is_file()
    assert_full_slot_untouched()


# ════════════════════════════════════════════════════════════════════════
# 대여
# ════════════════════════════════════════════════════════════════════════


@_git_required
def test_alloc_with_folders_restricts_an_idle_full_slot(wp, tmp_path):
    """전체로 만든 idle 슬롯을 `folders=[a]` 로 빌리면 그 cone 만 남는다."""
    _make_bare(wp, _make_origin(tmp_path))
    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False)
    slot_dir = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    assert (slot_dir / "b" / "y").is_file()

    leased = worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=["a"])

    assert (leased.slot, leased.state, leased.session) == (lease.slot, "leased", "t")
    assert not (slot_dir / "b" / "y").exists()
    assert _tags(slot_dir)["b/y"] == "S"
    assert _git(slot_dir, "status", "--porcelain") == ""


@_git_required
def test_alloc_without_folders_restores_the_full_checkout(wp, tmp_path):
    """폴더 제한으로 만든 idle 슬롯을 폴더 없이 빌리면 전체가 돌아온다 — 앞 제한을 물려받지 않는다."""
    _make_bare(wp, _make_origin(tmp_path))
    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False,
                           folders=["a"])
    slot_dir = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    assert not (slot_dir / "b" / "y").exists()

    worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t")

    assert (slot_dir / "b" / "y").is_file() and (slot_dir / "c" / "deep" / "f").is_file()
    assert _git(slot_dir, "config", "--type=bool", "--get", "core.sparseCheckout").strip() == "false"
    assert _git(slot_dir, "status", "--porcelain") == ""


@_git_required
def test_invalid_folders_are_refused_before_any_side_effect(wp, tmp_path):
    """잘못된 폴더는 생성·대여 모두 부작용 없이 거부 — 슬롯 폴더·대여 기록·공용 config 그대로."""
    origin = _make_origin(tmp_path)
    head = _git(origin, "rev-parse", "HEAD").strip()
    _git(origin, "update-index", "--add", "--cacheinfo", f"160000,{head},sub")
    _git(origin, "commit", "-q", "-m", "gitlink only")
    bare = _make_bare(wp, origin)
    baseline = (bare / "config").read_bytes()

    for bad in [*([value] for value in _INVALID_FOLDERS), []]:
        with pytest.raises(worktree_records.SlotFoldersRefused):
            worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False,
                           folders=bad)
        assert not (wp.WORK_DIR / "A_1").exists(), bad
        assert pool_store.list_leases(pm_paths.home_at(wp.REPO)) == [], bad
        assert not wp.LEASES_FILE.exists(), bad
        _assert_shared_config_untouched(bare, baseline)

    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False)
    slot_dir = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    baseline = (bare / "config").read_bytes()
    for bad in [*([value] for value in _INVALID_FOLDERS), []]:
        with pytest.raises(worktree_records.SlotFoldersRefused):
            worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=bad)
        row = _lease_row(wp, lease.slot)
        assert (row.state, row.session) == ("idle", ""), bad
        assert (slot_dir / "b" / "y").is_file(), bad
        _assert_shared_config_untouched(bare, baseline)


@_git_required
def test_alloc_refuses_to_change_folders_of_a_dirty_slot(wp, tmp_path):
    """커밋 안 한 변경이 있는 idle 슬롯의 폴더는 바꾸지 않는다 — git 은 반쪽 적용하고 rc 0 이다."""
    _make_bare(wp, _make_origin(tmp_path))
    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False)
    slot_dir = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    (slot_dir / "b" / "y").write_text("edited\n", encoding="utf-8")

    with pytest.raises(worktree_records.SlotFoldersRefused, match="커밋 안 한 변경"):
        worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=["a"])

    assert _lease_row(wp, lease.slot).state == "idle"
    assert (slot_dir / "b" / "y").read_text(encoding="utf-8") == "edited\n"
    assert _git_rc(slot_dir, "config", "--get", "core.sparseCheckout") == 1


# ════════════════════════════════════════════════════════════════════════
# submodule
# ════════════════════════════════════════════════════════════════════════


@_git_required
def test_submodules_outside_the_folders_are_neither_initialized_nor_reported(
        wp, tmp_path, monkeypatch, capsys):
    """폴더 밖 submodule(`c/subm`)은 초기화하지 않고 현황·sync 가 세지 않는다 — 판별은 S 표시다."""
    _allow_file_submodules(monkeypatch)
    _make_bare(wp, _make_origin_with_submodules(tmp_path))

    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", folders=["a"])

    slot_dir = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    assert (slot_dir / "a" / "subm" / "lib.txt").is_file()
    assert (slot_dir / "rootsub" / "lib.txt").is_file()
    assert not (slot_dir / "c" / "subm").exists()
    submodules = {entry.path: entry for entry in worktree_status.status(pm_paths.home_at(wp.REPO), slot=lease.slot)[0]["submodules"]}
    assert set(submodules) == {"a/subm", "rootsub"}
    assert not any(entry.warning for entry in submodules.values())
    capsys.readouterr()
    branch_checkout.sync(pm_paths.home_at(wp.REPO), lease.slot)
    assert "c/subm" not in capsys.readouterr().err
    assert not (slot_dir / "c" / "subm").exists()


# ════════════════════════════════════════════════════════════════════════
# git 버전 · 현황
# ════════════════════════════════════════════════════════════════════════


def test_folders_require_git_2_36_or_newer(wp):
    """git 2.36 미만이면 생성은 worktree add 없이, 대여는 폴더 제한 호출 없이 거부 — 대여 기록 그대로."""
    pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A").mkdir(parents=True)
    old = _RecordingGit(version="git version 2.35.8")
    with pytest.raises(worktree_records.SlotFoldersRefused, match="2.36"):
        worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", git_runner=old, folders=["a"])
    assert not any(call[:2] == ["worktree", "add"] for call in old.calls)
    assert pool_store.list_leases(pm_paths.home_at(wp.REPO)) == []

    with pool_store._lease_lock(pm_paths.home_at(wp.REPO)):
        pool_store._write_ledger(pm_paths.home_at(wp.REPO), [worktree_records.Lease(slot="work/A_1", repo="A", session="", started="t",
                                   state="idle")])
    before = wp.LEASES_FILE.read_bytes()
    old = _RecordingGit(version="git version 2.35.8")
    with pytest.raises(worktree_records.SlotFoldersRefused, match="2.36"):
        worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=["a"], git_runner=old)
    assert not any(call[:1] == ["sparse-checkout"] for call in old.calls)
    assert wp.LEASES_FILE.read_bytes() == before


@_git_required
def test_slot_git_status_reports_the_folders_git_holds(wp, tmp_path):
    """현황의 폴더 = git 정규화 목록(`a a/sub b` → `a b`) · 전체 슬롯 None · 한글 원문 · cone 아님은 거부."""
    _make_bare(wp, _make_origin(tmp_path))
    limited = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False,
                             folders=["a", "a/sub", "b"])
    full = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False)
    korean = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False,
                            folders=["한글"])

    assert worktree_status.slot_git_status(pm_paths.home_at(wp.REPO), limited.slot)["folders"] == ["a", "b"]
    assert worktree_status.slot_git_status(pm_paths.home_at(wp.REPO), full.slot)["folders"] is None
    assert worktree_status.slot_git_status(pm_paths.home_at(wp.REPO), korean.slot)["folders"] == ["한글"]

    _git(pm_paths.slot_path(pm_paths.home_at(wp.REPO), limited.slot), "sparse-checkout", "set", "--no-cone", "/a/*")
    with pytest.raises(worktree_records.SlotFoldersRefused, match="cone"):
        worktree_status.slot_git_status(pm_paths.home_at(wp.REPO), limited.slot)


def test_cli_status_prints_the_folders_line(wp, monkeypatch, capsys):
    """`worktree_pool.py status` 는 폴더 한 줄 — 목록 또는 `(제한 없음)`."""
    with pool_store._lease_lock(pm_paths.home_at(wp.REPO)):
        pool_store._write_ledger(pm_paths.home_at(wp.REPO), [worktree_records.Lease(slot="work/A_1", repo="A", session="s", started="t",
                                   state="leased")])
    rows = {"work/A_1": ["a", "b"]}
    monkeypatch.setattr(worktree_status, "slot_git_status", lambda _home, slot, **_: {
        "slot": slot, "branch": "A_1", "head": "a1b2c3d4" * 5, "parent": None, "children": [],
        "stashes": 0, "submodules": [], "dirty": False, "folders": rows[slot]})

    assert wp.main(["status", "work/A_1"]) == 0
    assert "folders: a, b" in capsys.readouterr().out
    rows["work/A_1"] = None
    assert wp.main(["status", "work/A_1"]) == 0
    assert "folders: (제한 없음)" in capsys.readouterr().out


def test_readonly_slot_refuses_folders(wp):
    git = _RecordingGit()
    with pytest.raises(ValueError, match="readonly"):
        worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", readonly=True, folders=["a"], git_runner=git)
    assert git.calls == []
    assert pool_store.list_leases(pm_paths.home_at(wp.REPO)) == []
    assert list(wp.WORK_DIR.iterdir()) == []


@_git_required
def test_alloc_initializes_submodules_in_the_folders_it_checks_out(wp, tmp_path, monkeypatch):
    _allow_file_submodules(monkeypatch)
    _make_bare(wp, _make_origin_with_submodules(tmp_path))
    slots = [worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", folders=["a"]) for _ in range(3)]
    for slot in slots:
        _git(pm_paths.slot_path(pm_paths.home_at(wp.REPO), slot.slot) / "a/subm", "checkout", "-b", "dev-a")
    for lease, folders in zip(slots, [None, ["c"], ["a", "b"]]):
        worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=folders)
        path = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
        assert _git(path / "a/subm", "symbolic-ref", "--short", "HEAD").strip() == "dev-a"
        statuses = {s.path: s for s in worktree_status.status(pm_paths.home_at(wp.REPO), slot=lease.slot)[0]["submodules"]}
        if folders == ["a", "b"]:
            assert not (path / "c/subm").exists()
            assert _tags(path)["c/subm"] == "S"
            assert "c/subm" not in statuses
        else:
            assert (path / "c/subm/lib.txt").is_file()
            assert statuses["c/subm"].kind == "pinned"
        assert not any(s.warning for s in statuses.values())


@_git_required
def test_empty_submodule_directory_is_uninitialized_not_dev(wp, tmp_path, monkeypatch):
    _allow_file_submodules(monkeypatch)
    origin = _make_origin_with_submodules(tmp_path)
    _make_bare(wp, origin)
    slots = [worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main") for _ in range(2)]
    _git(origin, "checkout", "-b", "feat-newsub")
    _git(origin, "submodule", "add", "-q", str(tmp_path / "sub-origin"), "a/newsub")
    _git(origin, "commit", "-qm", "new submodule")
    _git(pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A"), "fetch", "origin")
    _git(pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A"), "branch", "--no-track", "feat-newsub", "origin/feat-newsub")
    branch_checkout.switch(pm_paths.home_at(wp.REPO), slots[0].slot, "feat-newsub", protected=[])
    assert (pm_paths.slot_path(pm_paths.home_at(wp.REPO), slots[0].slot) / "a/newsub/lib.txt").is_file()
    path = pm_paths.slot_path(pm_paths.home_at(wp.REPO), slots[1].slot)
    _git(path, "checkout", "--no-recurse-submodules", "-b", "with-empty", "origin/feat-newsub")
    assert (path / "a/newsub").is_dir()
    assert _git(path / "a/newsub", "rev-parse", "--show-prefix").strip() == "a/newsub/"
    before = _git(path, "symbolic-ref", "HEAD")
    statuses = {s.path: s for s in worktree_status.status(pm_paths.home_at(wp.REPO), slot=slots[1].slot)[0]["submodules"]}
    assert (statuses["a/newsub"].kind, statuses["a/newsub"].warning, statuses["a/newsub"].dirty) == (
        "uninitialized", True, False)
    assert branch_checkout.dev(pm_paths.home_at(wp.REPO), slots[1].slot, "a/newsub", "feat-x")[0] == 1
    assert _git(path, "symbolic-ref", "HEAD") == before
    branch_checkout.sync(pm_paths.home_at(wp.REPO), slots[1].slot)
    assert (path / "a/newsub/lib.txt").is_file()
    assert next(s for s in worktree_status.status(pm_paths.home_at(wp.REPO), slot=slots[1].slot)[0]["submodules"] if s.path == "a/newsub").kind == "pinned"


@_git_required
def test_hand_widening_is_judged_like_any_empty_submodule(wp, tmp_path, monkeypatch):
    _allow_file_submodules(monkeypatch)
    _make_bare(wp, _make_origin_with_submodules(tmp_path))
    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", folders=["a"])
    path = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    before = _git(path, "symbolic-ref", "HEAD")
    assert branch_checkout.dev(pm_paths.home_at(wp.REPO), lease.slot, "c/subm", "feat-x")[0] == 1
    _git(path, "sparse-checkout", "add", "c")
    statuses = {s.path: s for s in worktree_status.status(pm_paths.home_at(wp.REPO), slot=lease.slot)[0]["submodules"]}
    assert (statuses["c/subm"].kind, statuses["c/subm"].warning) == ("uninitialized", True)
    assert branch_checkout.dev(pm_paths.home_at(wp.REPO), lease.slot, "c/subm", "feat-x")[0] == 1
    assert _git(path, "symbolic-ref", "HEAD") == before
    branch_checkout.sync(pm_paths.home_at(wp.REPO), lease.slot)
    assert (path / "c/subm/lib.txt").is_file()
    assert next(s for s in worktree_status.status(pm_paths.home_at(wp.REPO), slot=lease.slot)[0]["submodules"] if s.path == "c/subm").kind == "pinned"


@_git_required
def test_populated_inactive_submodule_keeps_its_dev_work(wp, tmp_path, monkeypatch):
    _allow_file_submodules(monkeypatch)
    _make_bare(wp, _make_origin_with_submodules(tmp_path))
    first, second = [worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main") for _ in range(2)]
    worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t")
    path = pm_paths.slot_path(pm_paths.home_at(wp.REPO), second.slot)
    sub = path / "c/subm"
    _git(sub, "checkout", "-b", "dev-c")
    (sub / "lib.txt").write_text("dev-uncommitted\n", encoding="utf-8")
    _git(pm_paths.slot_path(pm_paths.home_at(wp.REPO), first.slot), "submodule", "deinit", "-f", "c/subm")
    assert any(line.startswith("-") and "c/subm" in line for line in _git(path, "submodule", "status").splitlines())
    assert _git(sub, "rev-parse", "--show-prefix").strip() == ""
    entry = next(s for s in worktree_status.status(pm_paths.home_at(wp.REPO), slot=second.slot)[0]["submodules"] if s.path == "c/subm")
    assert (entry.kind, entry.dirty) == ("dev-ahead", True)
    branch_checkout.sync(pm_paths.home_at(wp.REPO), second.slot)
    assert _git(sub, "symbolic-ref", "--short", "HEAD").strip() == "dev-c"
    assert (sub / "lib.txt").read_text(encoding="utf-8") == "dev-uncommitted\n"
    (sub / "lib.txt").write_text("lib\n", encoding="utf-8")
    worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=["a", "c"])
    assert _git(sub, "symbolic-ref", "--short", "HEAD").strip() == "dev-c"
    assert _git_rc(pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A"), "config", "--get", "submodule.c/subm.url") == 1


def test_classification_failure_refuses_status_and_dev_but_sync_warns(wp, capsys):
    calls = []
    def runner(argv):
        calls.append(argv)
        if argv == ["submodule", "status"]:
            return 0, "-" + "a" * 40 + " a/subm\n"
        if argv[:1] == ["ls-files"]:
            return 1, "index failed"
        return 0, ""
    with pytest.raises(worktree_records.SlotFoldersRefused):
        submodule_git._submodule_statuses(runner, target="slot")
    with pytest.raises(worktree_records.SlotFoldersRefused):
        branch_checkout.dev(pm_paths.home_at(wp.REPO), "work/A_1", "a/subm", "dev", git_runner=runner)
    submodule_git._resync_submodules_selective(pm_paths.slot_path(pm_paths.home_at(wp.REPO), "work/A_1"), git_runner=runner)
    assert "[경고]" in capsys.readouterr().err
    assert not any("checkout" in call or "update" in call for call in calls)


def test_alloc_full_slot_adds_only_the_sparse_config_probe(wp, monkeypatch):
    with pool_store._lease_lock(pm_paths.home_at(wp.REPO)):
        pool_store._write_ledger(pm_paths.home_at(wp.REPO), [worktree_records.Lease(slot="work/A_1", repo="A", session="", started="t", state="idle")])
    git = _RecordingGit()
    monkeypatch.setattr(slot_snapshot, "_apply_git_snapshot", lambda _home, *args, **kwargs: git.calls.append(["snapshot"]))
    worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", git_runner=git)
    assert git.calls == [["config", "--type=bool", "--get", "core.sparseCheckout"], ["snapshot"]]


def test_populated_active_submodules_add_no_classification_probes(wp, monkeypatch):
    git = _RecordingGit()
    base = git.__call__
    def runner(argv):
        if argv == ["submodule", "status"]:
            git.calls.append(argv)
            return 0, " " + "a" * 40 + " a/subm\n"
        return base(argv)
    worktree_status.status(pm_paths.home_at(wp.REPO), slot="work/A_1", git_runner=runner)
    assert ["submodule", "status"] in git.calls
    assert not any(call[:1] == ["ls-files"] or "--show-prefix" in call for call in git.calls)
    assert git.calls.count(["config", "--type=bool", "--get", "core.sparseCheckout"]) == 1


@_git_required
@pytest.mark.parametrize("dirty", [False, True])
def test_populated_inactive_detached_submodule_is_preserved(wp, tmp_path, monkeypatch, dirty):
    _allow_file_submodules(monkeypatch)
    _make_bare(wp, _make_origin_with_submodules(tmp_path))
    first, second = [worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main") for _ in range(2)]
    worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t")
    sub = pm_paths.slot_path(pm_paths.home_at(wp.REPO), second.slot) / "c/subm"
    (sub / "lib.txt").write_text("other pin\n", encoding="utf-8")
    _git(sub, "commit", "-qam", "local detached commit")
    before = _git(sub, "rev-parse", "HEAD")
    if dirty:
        (sub / "lib.txt").write_text("uncommitted\n", encoding="utf-8")
    _git(pm_paths.slot_path(pm_paths.home_at(wp.REPO), first.slot), "submodule", "deinit", "-f", "c/subm")
    entry = next(s for s in worktree_status.status(pm_paths.home_at(wp.REPO), slot=second.slot)[0]["submodules"] if s.path == "c/subm")
    assert (entry.kind, entry.warning, entry.dirty) == ("drift", True, dirty)
    branch_checkout.sync(pm_paths.home_at(wp.REPO), second.slot)
    assert _git(sub, "rev-parse", "HEAD") == before
    assert (sub / "lib.txt").read_text(encoding="utf-8") == ("uncommitted\n" if dirty else "other pin\n")
    # submodule HEAD drift는 상위 슬롯의 커밋 안 한 변경으로 판정되어 대여가 거부된다.
    with pytest.raises(worktree_records.SlotFoldersRefused, match="커밋 안 한 변경"):
        worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=["a", "c"])
    assert _git(sub, "rev-parse", "HEAD") == before
    assert _git_rc(pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A"), "config", "--get", "submodule.c/subm.url") == 1


@_git_required
def test_deinitialized_empty_directory_is_initialized_by_sync(wp, tmp_path, monkeypatch):
    _allow_file_submodules(monkeypatch)
    _make_bare(wp, _make_origin_with_submodules(tmp_path))
    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main")
    path = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    _git(path, "submodule", "deinit", "-f", "c/subm")
    before = _git(path, "symbolic-ref", "HEAD")
    assert next(s for s in worktree_status.status(pm_paths.home_at(wp.REPO), slot=lease.slot)[0]["submodules"] if s.path == "c/subm").kind == "uninitialized"
    assert branch_checkout.dev(pm_paths.home_at(wp.REPO), lease.slot, "c/subm", "dev")[0] == 1
    assert _git(path, "symbolic-ref", "HEAD") == before
    branch_checkout.sync(pm_paths.home_at(wp.REPO), lease.slot)
    assert (path / "c/subm/lib.txt").is_file()


@_git_required
def test_dirty_sparse_slot_refuses_disable_and_preserves_release_folders(wp, tmp_path):
    _make_bare(wp, _make_origin(tmp_path))
    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", folders=["a"], init_submodules=False, owner_task="t")
    worktree_pool.release(pm_paths.home_at(wp.REPO), lease.slot, owner_task="t")
    assert sparse_checkout.slot_folders(pm_paths.home_at(wp.REPO), lease.slot) == ["a"]
    (pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot) / "a/x").write_text("edited\n", encoding="utf-8")
    with pytest.raises(worktree_records.SlotFoldersRefused, match="커밋 안 한 변경"):
        worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t")
    assert sparse_checkout.slot_folders(pm_paths.home_at(wp.REPO), lease.slot) == ["a"]
    assert _lease_row(wp, lease.slot).state == "idle"
    assert (pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot) / "a/x").read_text(encoding="utf-8") == "edited\n"


@pytest.mark.parametrize("stage", ["set", "checkout", "ls-files", "update"])
def test_sparse_creation_failure_rolls_back_the_lease_and_worktree(wp, stage):
    pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A").mkdir(parents=True)
    git = _RecordingGit()
    def runner(argv):
        if ((stage == "set" and argv[:1] == ["sparse-checkout"])
                or (stage == "checkout" and argv == ["checkout", "--no-recurse-submodules"])
                or (stage == "ls-files" and argv[:1] == ["ls-files"])
                or (stage == "update" and argv[:2] == ["submodule", "update"])):
            git.calls.append(argv)
            return 1, "failed"
        if argv[:1] == ["ls-files"]:
            git.calls.append(argv)
            return 0, f"H 160000 {'a' * 40} 0\ta/subm\0"
        return git(argv)
    with pytest.raises(RuntimeError):
        worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", folders=["a"], git_runner=runner)
    assert pool_store.list_leases(pm_paths.home_at(wp.REPO)) == []
    assert any(call[:2] == ["worktree", "remove"] for call in git.calls)


def test_slot_folders_round_trips_git_quoted_names(wp, tmp_path):
    import sys
    # 실제 파일명 제약과 무관하게 모든 플랫폼에서 Git C 인용의 바이트 복원을 확인한다.
    encoded = ['"a\\\"b"', '"a\\tb"', '"a\\nb"', '"a\\ab\\bb\\fb\\rb\\vb"',
               '"a\\\\b"', '"\\355\\225\\234\\352\\270\\200"', 'plain', '한글', 'a\u0085b']
    expected = ['a"b', 'a\tb', 'a\nb', 'a\ab\bb\fb\rb\vb', 'a\\b', '한글', 'plain', '한글', 'a\u0085b']
    calls = []
    def runner(argv):
        calls.append(argv)
        if argv[:1] == ["config"]:
            return 0, "true\n"
        assert argv == ["-c", "core.quotePath=false", "sparse-checkout", "list"]
        return 0, "\n".join(encoded) + "\n"
    assert sparse_checkout.slot_folders(pm_paths.home_at(wp.REPO), "work/A_1", git_runner=runner) == expected
    assert calls == [["config", "--type=bool", "--get", "core.sparseCheckout"],
                     ["config", "--type=bool", "--get", "core.sparseCheckoutCone"],
                     ["-c", "core.quotePath=false", "sparse-checkout", "list"]]
    # Windows 파일시스템은 큰따옴표 이름을 받지 않는다. Linux에서 실제 생성·대여·현황을 확인한다.
    if sys.platform == "win32":
        return
    from test_pm_config_status_json import _run_cli, _status_json
    origin = _make_origin(tmp_path)
    names = ['a"b', '"한글"']
    for name in names:
        path = origin / name
        path.mkdir()
        (path / "file").write_text("quoted\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "quoted folders")
    _make_bare(wp, origin)
    shutil.copytree(TOOLS, wp.REPO / ".project_manager/tools",
                    ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    (wp.REPO / ".project_manager/local.conf").write_text(
        f"project.name=quoted\nruntime.py={sys.executable}\ntest.cmd=pytest -q\n", encoding="utf-8")
    (wp.REPO / ".project_manager/areas.md").write_text(
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| A | A | {origin} | pytest -q | me | main | main | me |\n", encoding="utf-8")
    created = _run_cli(wp.REPO, "worktree", "add", "A", "--folders", ",".join(names), "--user-ack", "A")
    assert created.returncode == 0, created.stderr
    assert 'a"b' in created.stdout
    lease = pool_store.list_leases(pm_paths.home_at(wp.REPO))[0]
    assert sparse_checkout.slot_folders(pm_paths.home_at(wp.REPO), lease.slot) == sorted(names)
    assert worktree_status.slot_git_status(pm_paths.home_at(wp.REPO), lease.slot)["folders"] == sorted(names)
    (wp.REPO / ".project_manager/wiki").mkdir()
    (wp.REPO / ".project_manager/wiki/pm_state.template.md").write_text(
        "# 상태\n\n## 세션 식별 (현재까지 사용된 이름)\n최근 N 차 (sliding window, 기본 3 차):\n",
        encoding="utf-8")
    assert _run_cli(wp.REPO, "task", "new", "t").returncode == 0
    allocated = _run_cli(wp.REPO, "alloc", "A", "--task", "t", "--folders", ",".join(names))
    assert allocated.returncode == 0, allocated.stderr
    assert 'a"b' in allocated.stdout
    assert _status_json(wp.REPO)["slots"][0]["folders"] == sorted(names)
    status = _run_cli(wp.REPO, "status")
    assert status.returncode == 0 and 'a"b' in status.stdout, status.stderr


def test_alloc_folder_validation_precedes_branch_checkout(wp, tmp_path):
    _make_bare(wp, _make_origin(tmp_path))
    lease = worktree_pool.create_slot(pm_paths.home_at(wp.REPO), "A", base="main", init_submodules=False)
    path = pm_paths.slot_path(pm_paths.home_at(wp.REPO), lease.slot)
    _git(path, "branch", "other")
    _git(path, "checkout", "other")
    (path / "only-other").mkdir()
    (path / "only-other/file").write_text("target\n", encoding="utf-8")
    _git(path, "add", "only-other")
    _git(path, "commit", "-qm", "target folders")
    _git(path, "checkout", "A_1")
    bare = pm_paths.bare_repo_path(pm_paths.home_at(wp.REPO), "A")
    def snapshot():
        configs = {str(p.relative_to(bare)): p.read_bytes()
                   for p in [bare / "config", *_worktree_config_files(bare)]}
        names = _git(path, "ls-files", "-z", "--cached", "--others").split("\0")
        files = {name: (path / name).read_bytes() for name in [".git", *names]
                 if name and (path / name).is_file()}
        return (_git(path, "symbolic-ref", "HEAD"), _git(path, "rev-parse", "HEAD"),
                files, configs, wp.LEASES_FILE.read_bytes())
    real = worktree_git._real_git_runner(path)
    for key in ["branch", "resume"]:
        for failure in ["bad-folder", "dirty", "old-git"]:
            if failure == "dirty":
                (path / "b/y").write_text("uncommitted\n", encoding="utf-8")
            before = snapshot()
            calls = []
            def runner(argv):
                calls.append(argv)
                if failure == "old-git" and argv == ["version"]:
                    return 0, "git version 2.35.8\n"
                return real(argv)
            folders = ["nope"] if failure == "bad-folder" else ["a"]
            with pytest.raises(worktree_records.SlotFoldersRefused):
                worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=folders, git_runner=runner, **{key: "other"})
            assert snapshot() == before, (key, failure)
            assert not any(call[:1] in (["checkout"], ["sparse-checkout"]) for call in calls)
            if failure == "dirty":
                (path / "b/y").write_text("y\n", encoding="utf-8")
        calls = []
        def recording(argv):
            calls.append(argv)
            return real(argv)
        result = worktree_pool.alloc(pm_paths.home_at(wp.REPO), "A", owner_task="t", folders=["only-other"], git_runner=recording, **{key: "other"})
        assert result.state == "leased" and worktree_git.current_branch(pm_paths.home_at(wp.REPO), lease.slot) == "other"
        assert (path / "only-other/file").is_file()
        checkout = next(i for i, call in enumerate(calls) if call[:1] == ["checkout"])
        assert calls.index(["version"]) < checkout
        assert calls.index(["ls-tree", "-z", "other", "--", "only-other"]) < checkout
        assert next(i for i, call in enumerate(calls) if call[:1] == ["status"]) < checkout
        assert calls.count(["version"]) == 1
        assert calls.count(["ls-tree", "-z", "other", "--", "only-other"]) == 1
        worktree_pool.release(pm_paths.home_at(wp.REPO), lease.slot, owner_task="t")
        _git(path, "sparse-checkout", "disable")
        _git(path, "checkout", "A_1")
