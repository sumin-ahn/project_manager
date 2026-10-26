"""설치 공급자의 실행 Home, 평면 적재 금지, JSONC 보존 계약."""
from __future__ import annotations

import json
from pathlib import Path

from _git_fixture import init_git_repo
from pm_engine.app import adapter_render, import_render, install_plan
from pm_engine.domain import jsonc
from pm_engine.infra import install_files, pm_paths, repo_files, skill_inventory
from pm_engine.infra.harness import contract


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _source(tmp_path):
    source = tmp_path / "source"
    _write(source, ".project_manager/engine.manifest", ".claude/agents @render\n")
    _write(source, ".claude/agents/developer.md", "description: {{PROJECT_NAME}}\n")
    init_git_repo(source, commit="seed")
    return source


def _install(home, source, dest, name):
    dest.mkdir()
    actions = install_plan.plan_copy([source], dest, None, backup_dir_name=".pm_import_backups")
    identity = install_files.dest_root_identity(dest)
    outcome = install_files.apply_copy_plan(actions, dest, identity)
    assert not outcome.refused and not outcome.changed
    copied = {action.dst.relative_to(dest) for action in actions}
    import_render.render_managed_files(home, dest, {"{{PROJECT_NAME}}": name}, copied)
    return (dest / ".claude/agents/developer.md").read_text(encoding="utf-8")


def test_two_execution_homes_keep_destinations_and_reasoning_cwd_separate(tmp_path, monkeypatch):
    source = _source(tmp_path)
    calls = []
    monkeypatch.setattr(contract, "reasoning_allowed", lambda harness, model, *, cwd, env: calls.append(cwd) or ("high",))
    homes = [pm_paths.home_at(tmp_path / name) for name in ("engine-a", "engine-b")]
    for index, home in enumerate(homes):
        _write(home.root, f".agents/skills/card-{index}/SKILL.md", "fixture")
        assert _install(home, source, tmp_path / f"dest-{index}", f"Project-{index}") == f"description: Project-{index}\n"
        rendered = adapter_render.render_adapter(home, f"`/card-{index}`\neffort: {{{{DELEGATE_REASONING_DEVELOPER}}}}\n", {"DELEGATE_REASONING_DEVELOPER": "high"}, template_dir="codex", card_path=".claude/agents/developer.md")
        assert rendered == f"`$card-{index}`\neffort: high\n"
    assert calls == [home.root for home in homes]


def test_install_plan_and_render_work_with_flat_library_lookup_forbidden(tmp_path, monkeypatch):
    source = _source(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("평면 엔진 적재")
    monkeypatch.setattr(repo_files, "load_module", forbidden)
    home = pm_paths.home_at(tmp_path / "engine")
    assert _install(home, source, tmp_path / "dest", "Isolated") == "description: Isolated\n"


def test_target_export_uses_execution_code_inventory(tmp_path, monkeypatch):
    update = repo_files.load_module(Path(__file__).resolve().parents[1] / ".project_manager/tools/pm_update.py", "pm_update.py", allow_unverified=True)
    engine = tmp_path / "engine"
    target = tmp_path / "target"
    _write(engine, ".agents/skills/engine-call/SKILL.md", "fixture")
    _write(target, ".agents/skills/target-call/SKILL.md", "fixture")
    source = _write(target, "note.md", "`/engine-call` `/target-call`\n")
    monkeypatch.setattr(update, "REPO", engine)
    assert update._render_skill_entry_text(source, "codex") == "`$engine-call` `/target-call`\n"
    assert skill_inventory._installed_skill_entry_names(pm_paths.home_at(engine)) == ("engine-call",)


def test_shared_jsonc_preserves_offsets_comments_and_unrelated_bytes():
    original = '{\r\n  // 보존 주석\r\n  "limits": {"input": 10,},\r\n  "untouched": "a,//b",\r\n}\r\n'
    masked = jsonc._mask_jsonc_comments(original)
    assert len(masked) == len(original)
    assert masked.index('"limits"') == original.index('"limits"')
    edited, old = jsonc._jsonc_set_value(original, ("limits", "input"), 20)
    assert old == "10"
    assert edited == original.replace('"input": 10', '"input": 20')
    parsed = json.loads(jsonc._strip_jsonc_trailing_commas(jsonc._mask_jsonc_comments(edited)))
    assert parsed == {"limits": {"input": 20}, "untouched": "a,//b"}
