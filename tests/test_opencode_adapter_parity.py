"""T-0674 — opencode 두 진입 채널과 pm_update 생성 계약."""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"
# AGENTS.md 는 harness-neutral 공통 코어(ADR-0069·codex 와 byte-parity)라 하네스 고유 경로를
# 담지 않는다 — 두 표면 서술은 opencode 전용 채널인 pm-instructions.md 와 진입 문서가 진다.
DOCS = (
    REPO / "templates" / "opencode" / ".opencode" / "pm-instructions.md",
    REPO / "templates" / "opencode" / "AGENTS.lite.md",
    REPO / "templates" / "opencode" / "README.md",
)
PM_DEV_DELEGATE_SOURCE = (
    "templates/opencode/.claude/skills/pm-dev-delegate/SKILL.md"
)


def _load_pm_update():
    path = REPO / ".project_manager" / "tools" / "pm_update.py"
    spec = importlib.util.spec_from_file_location("t0674_pm_update", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _command_entries(pm_update):
    return [
        entry for entry in pm_update.read_manifest(MANIFEST)
        if str(entry).startswith(".opencode/command/")
    ]


def _expected_command(canonical: Path, name: str) -> bytes:
    text = canonical.read_text(encoding="utf-8")
    return text.replace(
        "(references/operational-details.md)",
        f"(../../.claude/skills/{name}/references/operational-details.md)",
    ).encode("utf-8")


def test_manifest_maps_every_command_to_root_canonical_skill():
    pm_update = _load_pm_update()
    entries = _command_entries(pm_update)
    skills = sorted(p.parent.name for p in (REPO / ".claude/skills").glob("*/SKILL.md"))
    assert len(entries) == len(skills) == 15
    actual = {str(entry): (entry.render, entry.source_rel) for entry in entries}
    expected = {
        f".opencode/command/{name}.md": (
            True,
            PM_DEV_DELEGATE_SOURCE if name == "pm-dev-delegate"
            else f".claude/skills/{name}/SKILL.md",
        ) for name in skills
    }
    assert actual == expected
    assert sum(source == PM_DEV_DELEGATE_SOURCE for _render, source in actual.values()) == 1
    assert sum(source.startswith(".claude/skills/") for _render, source in actual.values()) == 14


def test_pm_update_plan_generates_and_updates_flat_command_copies(tmp_path):
    pm_update = _load_pm_update()
    entries = _command_entries(pm_update)
    changes, missing = pm_update.plan(REPO, entries, dest_root=tmp_path, render_enabled=False)
    assert missing == []
    assert len(changes) == 15 and {kind for _rel, _src, _dst, kind in changes} == {"new"}
    pm_update.apply(changes)
    generated = tmp_path / ".opencode/command/pm-bootstrap.md"
    canonical = REPO / ".claude/skills/pm-bootstrap/SKILL.md"
    assert generated.read_bytes() == _expected_command(canonical, "pm-bootstrap")
    override = REPO / PM_DEV_DELEGATE_SOURCE
    delegated = tmp_path / ".opencode/command/pm-dev-delegate.md"
    assert delegated.read_bytes() == _expected_command(override, "pm-dev-delegate")

    generated.write_text("stale\n", encoding="utf-8")
    changes, missing = pm_update.plan(REPO, entries, dest_root=tmp_path, render_enabled=False)
    assert missing == []
    assert [(rel, kind) for rel, _src, _dst, kind in changes] == [
        (".opencode/command/pm-bootstrap.md", "update")
    ]


def test_entry_docs_describe_both_distinct_surfaces():
    stale = ("단일 소비", "채널 은퇴", "slash command를 뜻하지 않")
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        assert ".claude/skills/" in text and ".opencode/command" in text, path
        assert not any(phrase in text for phrase in stale), (path, stale)
