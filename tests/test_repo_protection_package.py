"""보호 패키지의 Home·artifact·재설치 계약."""
from pathlib import Path
import importlib.util

import pytest

from pm_engine.infra import pm_paths, protected_hooks, areas_store
from pm_engine.app import repo_protection
from pm_engine.app import update_adapters

def _tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_hook_artifact_bytes_and_paths_match(tmp_path):
    import hashlib
    home = pm_paths.home_at(tmp_path)
    actual = protected_hooks.protected_hook_artifacts(
        home, "repo", ["main"], gate_mode="release", test_cmd="pytest -q")
    assert [a.path.name for a in actual] == [
        "protected", "engine-root", "gate-contract", "pre-push", "pre-commit"]
    assert [a.executable for a in actual] == [False, False, False, True, True]
    assert [a.content for a in actual[:3]] == [
        "main\n", f"{tmp_path.resolve()}\n", "release\npytest -q\n"]
    assert hashlib.sha256(actual[3].content.encode()).hexdigest() == '6aafd4c9d0c82405f72b36eab95b20efa9f2f9826b5a2d674215e4541fe63ddc'
    assert hashlib.sha256(actual[4].content.encode()).hexdigest() == '5611a9c2920efed728152bcc97392090130e6caab8d680a6b8fa564c385f46f2'
    assert all(a.path.parent == pm_paths.repo_hooks_dir(home) / "repo" for a in actual)


def test_failure_reason_is_bound_to_home(tmp_path, monkeypatch):
    left = pm_paths.home_at(tmp_path / "left")
    right = pm_paths.home_at(tmp_path / "right")

    false_reason = ("install_protected_hook이 False를 반환(bare 미러 부재 또는 "
                    "bare core.hooksPath 설정 실패)")

    def fail(home, repo, protected, **kwargs):
        if home == left:
            raise OSError("left installation")
        return False

    monkeypatch.setattr(protected_hooks, "install_protected_hook", fail)
    # 설치 예외는 사유로 접지 않고 전파한다.
    with pytest.raises(OSError, match="left installation"):
        repo_protection._install_protected_hook(left, "repo")
    assert repo_protection.protected_hook_install_failure_reason(left, "repo") is None
    assert repo_protection._install_protected_hook(right, "repo") is False
    assert repo_protection.protected_hook_install_failure_reason(right, "repo") == false_reason
    assert repo_protection.protected_hook_install_failure_reason(left, "repo") is None
    monkeypatch.setattr(protected_hooks, "install_protected_hook", lambda *a, **kw: True)
    assert repo_protection._install_protected_hook(left, "repo") is True
    assert repo_protection.protected_hook_install_failure_reason(left, "repo") is None
    assert repo_protection.protected_hook_install_failure_reason(right, "repo") == false_reason


def test_registry_first_match_and_first_nonempty_are_distinct(tmp_path):
    h = pm_paths.home_at(tmp_path)
    p = pm_paths.areas_file(h)
    p.parent.mkdir(parents=True)
    p.write_text('| repo | git |\n|---|---|\n| repo | |\n| repo | ssh://host/org/repo |\n', encoding='utf-8')
    assert areas_store._areas_git_url(h, 'repo') is None
    assert repo_protection._repo_registry_git(h, 'repo') == 'ssh://host/org/repo'

def test_reinstall_uses_target_data_without_target_flat_modules(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    m = _tool(root / ".project_manager/tools/pm_update.py", "new_update_probe")
    home = pm_paths.home_at(tmp_path)
    area = pm_paths.areas_file(home)
    area.parent.mkdir(parents=True)
    area.write_text("| repo | git | protected | test_cmd |\n|---|---|---|---|\n| repo | ssh://h/org/repo | main | pytest -q |\n", encoding="utf-8")
    result = update_adapters.reinstall_protected_hooks(tmp_path, write=False)
    assert result["status"] == "done" and result["no_bare"] == ["repo"]
    assert not pm_paths.repo_hooks_dir(home).exists()
    bare = pm_paths.bare_repo_path(home, "repo")
    bare.mkdir(parents=True)
    home.local_conf.write_text("upstream.path=ssh://h/org/repo\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(repo_protection, "_install_protected_hook_reporting", lambda h, r, **kw: calls.append((h, r)) or False)
    result = update_adapters.reinstall_protected_hooks(tmp_path, write=False)
    assert result["drifted"] == ["repo"] and result["failed"] == [] and calls == []
    result = update_adapters.reinstall_protected_hooks(tmp_path, write=True)
    assert result["failed"] == ["repo"] and calls == [(home, "repo")]


def test_config_uses_home_local_conf_when_code_root_differs(tmp_path):
    home = pm_paths.home_at(tmp_path / "code", pm_home=tmp_path / "data")
    home.local_conf.parent.mkdir(parents=True)
    home.local_conf.write_text("test.cmd=first\ntest.cmd=\nupstream.path=ssh://h/repo\n", encoding="utf-8")
    assert repo_protection._local_conf_value(home, "test.cmd") == ""
    assert repo_protection._local_conf_value(home, "upstream.path") == "ssh://h/repo"


def test_hook_wiring_does_not_climb_to_an_enclosing_repo(tmp_path):
    """bare 자리가 git 저장소가 아닌 빈 폴더면 연결은 실패하고, 감싼 저장소 설정은 그대로다.

    `git -C <폴더>` 는 폴더가 저장소가 아니면 위로 올라가 감싼 저장소를 찾는다. 회귀 시험의 임시
    폴더가 슬롯 안에 있어, 그 길로 실제 공유 저장소의 `core.hooksPath` 가 시험 폴더로 바뀌어
    보호 훅이 꺼졌다(2026-10-29 실측).
    """
    import subprocess

    outer = tmp_path / "outer"
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    home = pm_paths.home_at(outer / "home")
    pm_paths.bare_repo_path(home, "svc").mkdir(parents=True)

    assert protected_hooks.install_protected_hook(
        home, "svc", ["main"], gate_mode="release", test_cmd="pytest -q") is False
    assert protected_hooks.protected_hook_wired(home, "svc") is None
    outer_hooks = subprocess.run(
        ["git", "-C", str(outer), "config", "--get", "core.hooksPath"],
        capture_output=True, text=True)
    assert outer_hooks.returncode == 1, outer_hooks.stdout
