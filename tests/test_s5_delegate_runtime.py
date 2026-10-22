"""위임 공통 공급자의 경로·환경·운반·범위·재개 계약."""
from pathlib import Path
import os
import stat

from pm_engine.domain import delegate_run_rules, delegate_scope_rules
from pm_engine.infra import codex_observation, conf_store, delegate_config, pm_paths
from pm_engine.infra.harness import delegate_env, delegate_temp


def test_home_separates_code_and_pm_config(tmp_path):
    code, owner = tmp_path / "code", tmp_path / "owner"
    (code / ".project_manager").mkdir(parents=True)
    (owner / ".project_manager").mkdir(parents=True)
    (code / ".project_manager/local.conf").write_text("delegate.enabled=true\n", encoding="utf-8")
    (owner / ".project_manager/local.conf").write_text("delegate.enabled=false\n", encoding="utf-8")
    home = pm_paths.home_at(code, pm_home=owner)
    assert home.root == code
    assert conf_store.local_config(home)["delegate.enabled"] == "false"
    assert not delegate_config._is_enabled(conf_store.local_config(home))
    assert home.local_dir == owner / ".project_manager/.local"
    assert codex_observation._codex_audit_path(home) == code / ".project_manager/.local/delegate-channel/codex-observations.jsonl"


def test_read_temp_uses_pm_owner_context(tmp_path):
    code, owner = tmp_path / "code", tmp_path / "owner"
    code.mkdir()
    owner.mkdir()
    home = pm_paths.home_at(code, pm_home=owner)
    root = delegate_temp._read_tmp_root(home)
    assert root == home.local_dir / "tmp"
    assert not root.is_relative_to(code)


def test_child_environment_drops_unlisted_values(tmp_path):
    env = delegate_env.build_env("codex", source={
        "PATH": "/bin", "HOME": str(tmp_path),
        "OPENAI_API_KEY": "test-key", "UNRELATED_SECRET": "do-not-forward",
    }, windows=False)
    assert env["PATH"] == "/bin"
    assert env["OPENAI_API_KEY"] == "test-key"
    assert "UNRELATED_SECRET" not in env


def test_transport_keeps_unicode_prompt_and_removes_owned_files(tmp_path):
    prompt = "검토 대상\n공백과 한글 유지"
    artifact = delegate_temp._save_opencode_transport_prompt(tmp_path, prompt)
    assert Path(artifact.path).read_text(encoding="utf-8") == prompt
    if os.name != "nt":
        assert stat.S_IMODE(Path(artifact.path).stat().st_mode) == 0o600
    assert delegate_temp._cleanup_attempt_transport(artifact) is None
    assert not Path(artifact.path).exists()


def test_scope_tracks_rename_source_and_same_status_content_change():
    parsed = delegate_scope_rules.parse_porcelain_z("R  new.py\0old.py\0")
    assert {p.path for p in parsed} == {"new.py", "old.py"}
    entry = (delegate_scope_rules.StatusEntry(" M", "file.py"),)
    before = delegate_scope_rules.WorktreeState(entry, (("file.py", "old"),))
    after = delegate_scope_rules.WorktreeState(entry, (("file.py", "new"),))
    assert delegate_scope_rules.changed_status_paths(before, after) == ("file.py",)


def test_resume_excludes_newer_run_with_unextracted_reply():
    older = {"surface": "delegate", "id": "older", "ticket": "T-2001",
             "role": "developer", "harness": "codex", "rc": 0,
             "session_id": "session-old", "started_at": "2026-10-21T00:00:00Z",
             "reply_extracted": True}
    newer = dict(older, id="newer", started_at="2026-10-22T00:00:00Z", reply_extracted=False)
    assert delegate_run_rules.select_resume_record([newer, older], selector="T-2001", role="developer", harness="codex") == older
