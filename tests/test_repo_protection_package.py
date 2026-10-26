"""보호 패키지의 Home·artifact·재설치 계약."""
from pathlib import Path
import importlib.util
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

    def fail(home, repo, protected, **kwargs):
        if home == left:
            raise OSError("left installation")
        raise RuntimeError("right installation")

    monkeypatch.setattr(protected_hooks, "install_protected_hook", fail)
    assert repo_protection._install_protected_hook(left, "repo") is False
    assert repo_protection.protected_hook_install_failure_reason(right, "repo") is None
    assert repo_protection._install_protected_hook(right, "repo") is False
    assert repo_protection.protected_hook_install_failure_reason(left, "repo") == (
        "OSError: left installation")
    assert repo_protection.protected_hook_install_failure_reason(right, "repo") == (
        "RuntimeError: right installation")
    monkeypatch.setattr(protected_hooks, "install_protected_hook", lambda *a, **kw: True)
    assert repo_protection._install_protected_hook(left, "repo") is True
    assert repo_protection.protected_hook_install_failure_reason(left, "repo") is None
    assert repo_protection.protected_hook_install_failure_reason(right, "repo") == (
        "RuntimeError: right installation")


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
