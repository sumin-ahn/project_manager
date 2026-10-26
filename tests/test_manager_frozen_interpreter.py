"""번들 안에서 엔진을 부를 때의 인터프리터와 소스 자리 (T-0912 AT-003).

합의 문장(T-0914 와 같은 문구):

    관리자는 파이썬 인터프리터를 번들하지 않는다. 프로젝트 사본을 부를 때는 그 프로젝트
    `local.conf` 의 `runtime.py`(빈값이면 OS 기본 `python3` / Windows `py -3`)를 쓰고, 아직
    프로젝트가 없는 `pm_import.py --new` 는 같은 규칙의 OS 기본 인터프리터로 번들 안 소스를
    실행한다. 번들에서 `pm_import` 를 in-process 로 돌리는 길은 없다.

이 파일이 그 문장을 코드로 고정한다. `sys.executable` 을 쓰면 frozen 에서 그것이 관리자 실행
파일 자신이고, `pm_import` 가 `board.py init` 을 `sys.executable` 로 부르므로 관리자가 자기
자신을 다시 띄운다(실측 `pm_import.py:3224`).
"""
from __future__ import annotations

import ast
import importlib.machinery
import os
import sys
import types
from pathlib import Path

import pytest

from _manager_home import git_required, make_bare_with_tags, make_pm_home, set_conf_key

from manager import REPO_ROOT, runner
from pm_engine.infra import upstream_git
from manager.bridge_update import (
    BUILD_INFO_MODULE, bundle_root, import_argv, imprinted_upstream, init_argv, update_argv,
)

REPO = Path(__file__).resolve().parents[1]

# PyInstaller onefile 이 소스를 푸는 자리를 가리키는 이름. frozen 여부의 단일 신호다.
UNPACK_ATTRIBUTE = "_MEIPASS"
# OS 기본 인터프리터 — 엔진 `pm_bootstrap._resolve_python_argv` 와 같은 두 갈래.
OS_DEFAULT_ARGV = {"posix": ("python3",), "nt": ("py", "-3")}


@pytest.fixture
def unpacked(tmp_path, monkeypatch) -> Path:
    """frozen 형상 — 번들이 풀린 트리 하나를 세우고 두 표식을 건다."""
    root = tmp_path / "_MEI123456"
    (root / ".project_manager" / "tools").mkdir(parents=True)
    (root / "templates").mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, UNPACK_ATTRIBUTE, str(root), raising=False)
    return root


# ── ① 번들 루트 ──────────────────────────────────────────────────────────────


def test_bundle_root_is_the_repo_tree_when_not_frozen():
    assert not hasattr(sys, UNPACK_ATTRIBUTE), "이 회귀는 frozen 이 아닌 인터프리터에서 돈다"

    assert bundle_root() == REPO_ROOT == REPO


def test_bundle_root_follows_the_pyinstaller_unpack_directory(unpacked):
    assert bundle_root() == unpacked


# ── ② frozen 에서 만들어지는 적용 명령 ───────────────────────────────────────


def test_the_import_command_points_into_the_bundle_when_frozen(unpacked, tmp_path):
    dest = tmp_path / "new-project"

    argv = import_argv(dest, "claude", "new-project", "https://example.invalid/x.git")

    prefix = OS_DEFAULT_ARGV[os.name]
    assert tuple(argv[:len(prefix)]) == prefix
    assert argv[len(prefix)] == str(unpacked / ".project_manager" / "tools" / "pm_import.py")
    assert argv[len(prefix) + 1] == "--new"
    assert argv[argv.index("--from") + 1] == str(unpacked)


def test_the_import_command_never_uses_the_managers_own_executable(unpacked, tmp_path):
    """frozen 에서 `sys.executable` 은 관리자 실행 파일이다 — 그 자리를 쓰면 자기 재기동이다."""
    dest = tmp_path / "new-project"

    argv = import_argv(dest, "claude", "new-project", "https://example.invalid/x.git")

    expected = OS_DEFAULT_ARGV[os.name]
    assert argv[0] != sys.executable
    assert tuple(argv[:len(expected)]) == expected


def test_no_manager_module_reads_sys_executable():
    """서술이 아니라 코드로 잰다 — `sys.executable` 접근 노드가 하나도 없어야 한다."""
    offenders = []
    for module in sorted((REPO / "manager").glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        offenders += [module.name for node in ast.walk(tree)
                      if isinstance(node, ast.Attribute) and node.attr == "executable"
                      and isinstance(node.value, ast.Name) and node.value.id == "sys"]

    assert offenders == []


# ── ③ 프로젝트가 없는 자리 = OS 기본 / 있는 자리 = 그 사본의 runtime.py ──────


@pytest.mark.parametrize("os_name", sorted(OS_DEFAULT_ARGV))
def test_a_folder_without_a_conf_resolves_to_the_os_default(tmp_path, os_name):
    """`pm_import --new` 시점의 dest 에는 conf 가 없다 — 규칙을 두 번 쓰지 않고 같은 함수를 지난다."""
    assert runner.interpreter(tmp_path / "not-yet", os_name=os_name) == OS_DEFAULT_ARGV[os_name]


@git_required
def test_a_project_copy_is_called_with_its_own_runtime_py(tmp_path):
    """사본을 부르는 두 자리(갱신·init)는 그 사본이 선언한 인터프리터를 쓴다."""
    home = make_pm_home(tmp_path / "demo", upstream=make_bare_with_tags(tmp_path / "remote"))
    set_conf_key(home, "runtime.py", "python3.11")

    assert update_argv(home, dry_run=True)[0] == "python3.11"
    assert init_argv(home, "", "", "", "")[0] == "python3.11"


# ── ④ 각인 upstream — 빌드가 남긴 값이 먼저다 ────────────────────────────────


def test_the_imprinted_upstream_wins_when_the_build_left_one(monkeypatch):
    imprinted = types.ModuleType(BUILD_INFO_MODULE)
    imprinted.__spec__ = importlib.machinery.ModuleSpec(BUILD_INFO_MODULE, None)
    imprinted.UPSTREAM_URL = "https://mirror.example.invalid/project_manager.git"
    monkeypatch.setitem(sys.modules, BUILD_INFO_MODULE, imprinted)

    assert imprinted_upstream() == "https://mirror.example.invalid/project_manager.git"


@git_required
def test_without_an_imprint_the_bundle_trees_origin_is_read():
    """개발 트리에는 각인 모듈이 없다 — 설치가 쓰는 바로 그 도출 함수(`upstream_git`)로 간다."""
    assert BUILD_INFO_MODULE not in sys.modules

    derived = upstream_git.derive_origin_url(bundle_root())
    assert derived, "번들 트리가 git checkout 이 아니다 — 이 회귀의 전제 붕괴"
    assert imprinted_upstream() == derived
