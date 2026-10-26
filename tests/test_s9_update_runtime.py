"""갱신 계획·적용·전달·완료의 package 실행 경계.

두 실행 Home 이 한 프로세스에서 계획·타깃 열거·렌더 입력을 섞지 않는지, 전달 프로세스가 엔진 코드를
옮기기 시작한 뒤 엔진 모듈을 새로 적재하지 않고 받은 갱신기를 새 프로세스로 띄우는지 실제 모듈로 본다.
"""
from __future__ import annotations

import argparse
import builtins
import json
import os
import sys
from pathlib import Path

import pytest

from _git_fixture import init_git_repo
from pm_engine.app import conf_apply
from pm_engine.app import engine_transfer
from pm_engine.app import entry_doc_migration
from pm_engine.app import update_adapters
from pm_engine.app import update_finish
from pm_engine.app import update_manifest
from pm_engine.app import update_plan
from pm_engine.app import update_render
from pm_engine.app import upstream_revisions
from pm_engine.infra import pm_paths
from pm_engine.infra import retired_paths
from pm_engine.infra import shipping_targets

_PLAIN_REL = "docs/guide.md"
_RENDER_REL = "docs/title.md"
_OWNED_REL = "adapters/local-only"
_PROBE_REL = ".project_manager/tools/zz_engine_probe.py"
_UPDATER_REL = ".project_manager/tools/pm_update.py"
# 받은 갱신기 — 인계 값·인자를 한글과 함께 두 스트림에 내고 고유 종료 코드로 끝난다.
_RECEIVED_UPDATER = """import json, os, sys
handoff = json.loads(os.environ["PM_UPDATE_COMPLETION"])
print("완료 프로세스 인자=" + json.dumps(sys.argv[1:], ensure_ascii=False))
print("완료 프로세스 인계=" + json.dumps(handoff, sort_keys=True, ensure_ascii=False))
print("완료 프로세스 경고 한글", file=sys.stderr)
sys.exit(7)
"""
# 전달 프로세스가 부르면 안 되는 완료 단계 — 하나라도 불리면 같은 프로세스가 완료를 실행한 것이다.
_COMPLETION_STEPS = (
    (update_render, "apply"), (update_manifest, "_installed_entry_notation_manifests"),
    (conf_apply, "prune_unavailable_reasoning"), (entry_doc_migration, "migrate_entry_doc"),
    (update_finish, "drop_self_lease_rows"), (update_finish, "idle_legacy_slot_session_rows"),
    (entry_doc_migration, "migrate_renamed_conf_keys"), (update_adapters, "reinstall_protected_hooks"),
    (update_adapters, "sync_adapter_configs"), (update_adapters, "apply_harness_ctx_limits"),
    (update_adapters, "check_adapter_hook_sets"), (upstream_revisions, "converge_upstream_revs"),
    (retired_paths, "_run_retired_path_migration"),
)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _source_tree(root: Path, files: dict[str, str], manifest: list[str]) -> Path:
    for rel, text in files.items():
        _write(root, rel, text)
    _write(root, ".project_manager/engine.manifest", "\n".join(manifest) + "\n")
    init_git_repo(root, commit="source")
    return root.resolve()


def _execution_home(root: Path, *, target: str, skill: str, project: str, source: Path):
    """templates 타깃 하나·설치 스킬 하나·local.conf 를 가진 실행 Home(자기 위치 dest 겸용)."""
    (root / "templates" / target).mkdir(parents=True)
    _write(root, f".agents/skills/{skill}/SKILL.md", "fixture\n")
    _write(root, ".project_manager/local.conf",
           f"upstream.path={source}\nproject.name={project}\n")
    return pm_paths.home_at(root)


def test_two_homes_keep_planning_targets_and_render_inputs_separate(tmp_path):
    source = _source_tree(tmp_path / "source", {
        _PLAIN_REL: "같은 본문\n",
        _RENDER_REL: "# {{PROJECT_NAME}}\n",
        "notes/calls.md": "`/alpha-skill` `/beta-skill`\n",
    }, [_PLAIN_REL, f"{_RENDER_REL} @render", "notes", f"{_OWNED_REL} @target-owned"])
    home_a = _execution_home(tmp_path / "a", target="alpha", skill="alpha-skill",
                             project="Alpha", source=source)
    home_b = _execution_home(tmp_path / "b", target="beta", skill="beta-skill",
                             project="Beta", source=source)
    _write(home_b.root, _PLAIN_REL, "같은 본문\n")

    # 타깃 열거·해소는 각 실행 Home 의 templates 만 본다 — 외부 source checkout 이 아니다.
    assert shipping_targets.discover_target_names(home_a) == ["alpha"]
    assert shipping_targets.discover_target_names(home_b) == ["beta"]
    assert shipping_targets.resolve_target_root(home_a, "alpha") == (home_a.root / "templates" / "alpha").resolve()
    with pytest.raises(FileNotFoundError):
        shipping_targets.resolve_target_root(home_a, "beta")

    # 계획 — dest 좌표·inventory·제안 conf 가 각 Home 의 것이다.
    manifest = update_manifest._resolve_planning_manifest(
        home_a.root, source, update_manifest.resolve_manifest_selfheal(home_a.root, source))
    plans = {}
    for home, project in ((home_a, "Alpha"), (home_b, "Beta")):
        inventory: set[str] = set()
        dest_inventory: set[str] = set()
        changes, missing = update_plan.plan(
            home, source, manifest, inventory_out=inventory, dest_inventory_out=dest_inventory,
            conf={"project.name": project})
        assert missing == [_OWNED_REL]
        assert all(Path(dst).is_relative_to(home.root) for _rel, _src, dst, _kind in changes)
        assert dest_inventory == {_PLAIN_REL, _RENDER_REL, "notes/calls.md"}
        assert inventory >= dest_inventory
        plans[home.root] = changes
    assert sorted(str(rel) for rel, *_ in plans[home_a.root]) == sorted(
        [_PLAIN_REL, _RENDER_REL, "notes/calls.md"])
    assert _PLAIN_REL not in {str(rel) for rel, *_ in plans[home_b.root]}

    # drift 질의는 같은 구조화 계획 — target-owned 부재는 판정 불능이 아니다.
    assert sorted(update_plan.drift_changes(home_a, home_a.root)) == sorted(
        str(rel) for rel, *_ in plans[home_a.root])
    assert _PLAIN_REL not in update_plan.drift_changes(home_b, home_b.root)

    # 호출 표기 렌더의 스킬 이름은 실행 Home 의 inventory 다.
    note = source / "notes" / "calls.md"
    assert update_render._render_skill_entry_text(home_a, note, "codex") == "`$alpha-skill` `/beta-skill`\n"
    assert update_render._render_skill_entry_text(home_b, note, "codex") == "`/alpha-skill` `$beta-skill`\n"

    # 적용 — 각 Home 의 dest 에만 쓰고, render 값은 그 dest 의 local.conf 다.
    update_render.apply(home_a, plans[home_a.root])
    assert (home_a.root / _PLAIN_REL).read_bytes() == "같은 본문\n".encode("utf-8")
    assert (home_a.root / _RENDER_REL).read_text(encoding="utf-8") == "# Alpha\n"
    assert not (home_b.root / _RENDER_REL).exists()
    update_render.apply(home_b, plans[home_b.root])
    assert (home_b.root / _RENDER_REL).read_text(encoding="utf-8") == "# Beta\n"
    assert (home_a.root / _RENDER_REL).read_text(encoding="utf-8") == "# Alpha\n"
    assert update_plan.drift_changes(home_a, home_a.root) == []
    assert update_plan.drift_changes(home_b, home_b.root) == []


def _transfer_case(tmp_path: Path, name: str) -> tuple:
    source = _source_tree(tmp_path / f"{name}-source", {
        _PROBE_REL: "# probe\n", _UPDATER_REL: _RECEIVED_UPDATER, "adapterdir/one.md": "# one\n",
    }, [_PROBE_REL, _UPDATER_REL, "adapterdir"])
    dest = tmp_path / name
    _write(dest, ".project_manager/local.conf", f"upstream.path={source}\n")
    return pm_paths.home_at(dest), source


def _args(source: Path, *, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(source=str(source), target=None, dry_run=dry_run,
                              count_only=False, log=False)


def _tree(root: Path) -> dict[str, bytes]:
    from test_fresh_adopter_e2e import _snapshot_tree
    return _snapshot_tree(root)


def test_transfer_uses_fresh_completion_without_import_after_copy(tmp_path, monkeypatch, capfd):
    for owner, name in _COMPLETION_STEPS:
        def trapped(*_args, _name=name, **_kwargs):
            raise AssertionError(f"전달 프로세스가 완료 단계 {_name} 를 불렀다")
        monkeypatch.setattr(owner, name, trapped)

    # 코드 복사가 시작된 뒤의 엔진 import 를 모두 기록한다(이미 적재된 모듈의 재참조도 포함).
    copying: list[bool] = []
    late_imports: list[str] = []
    real_copy2 = engine_transfer.shutil.copy2
    real_import = builtins.__import__

    def copy2(src, dst, *args, **kwargs):
        copying.append(True)
        return real_copy2(src, dst, *args, **kwargs)

    def tracing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if copying and (name.startswith("pm_engine") or name in {"pm_update", "pm_import", "pm_config"}):
            late_imports.append(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(engine_transfer.shutil, "copy2", copy2)
    monkeypatch.setattr(builtins, "__import__", tracing_import)
    monkeypatch.setenv("PM_UPDATE_COMPLETION", "")
    monkeypatch.delenv("PM_UPDATE_COMPLETION")

    home, source = _transfer_case(tmp_path, "dest")
    argv = ["--from", str(source), "--보존-인자"]
    rc = engine_transfer._transfer_engine_code(home, _args(source), argv, [])
    monkeypatch.setattr(builtins, "__import__", real_import)

    assert rc == 7, "완료 프로세스의 종료 코드가 그대로 올라오지 않았다"
    assert copying and late_imports == [], late_imports
    assert (home.root / _PROBE_REL).read_text(encoding="utf-8") == "# probe\n"
    assert not (home.root / "adapterdir").exists(), "전달 프로세스가 엔진 코드 밖 행을 옮겼다"
    out, err = capfd.readouterr()
    assert "완료 프로세스 인자=" + json.dumps(argv, ensure_ascii=False) in out
    handoff = {"dest": str(home.root), "source": str(source), "transferred": 2}
    assert "완료 프로세스 인계=" + json.dumps(handoff, sort_keys=True, ensure_ascii=False) in out
    assert "완료 프로세스 경고 한글" in err
    assert "PM_UPDATE_COMPLETION" not in os.environ

    # 엔진 코드 변경이 있는 dry-run 과 부분 전파는 쓰지 않고 완료 프로세스도 띄우지 않는다.
    monkeypatch.setattr(engine_transfer, "_launch_completion",
                        lambda *args: pytest.fail("완료 프로세스를 띄웠다"))
    for name, dry_run, scope, expected in (("dry", True, [], 0), ("scoped", False, ["adapterdir"], 1)):
        case_home, case_source = _transfer_case(tmp_path, name)
        before = _tree(case_home.root)
        assert engine_transfer._transfer_engine_code(
            case_home, _args(case_source, dry_run=dry_run), [], scope) == expected
        assert _tree(case_home.root) == before, f"{name}: 쓰기 전에 멈추지 않았다"

    # 인계 값은 정확한 세 필드만 받고, 꺼낸 뒤에는 환경에 남지 않는다.
    for raw in ("{", json.dumps({"dest": str(home.root), "source": str(source)}),
                json.dumps({"dest": "relative", "source": str(source), "transferred": 0}),
                json.dumps({"dest": str(home.root), "source": str(source), "transferred": True}),
                json.dumps({"dest": str(home.root), "source": str(source), "transferred": -1})):
        monkeypatch.setenv(engine_transfer.COMPLETION_ENV, raw)
        with pytest.raises(engine_transfer.CompletionHandoffError):
            engine_transfer._take_completion_handoff()
        assert engine_transfer.COMPLETION_ENV not in os.environ
    good = {"dest": str(home.root), "source": str(source), "transferred": 3}
    monkeypatch.setenv(engine_transfer.COMPLETION_ENV, json.dumps(good))
    assert engine_transfer._take_completion_handoff() == good
    assert engine_transfer._take_completion_handoff() is None

    monkeypatch.undo()

    # 기동 실패는 성공으로 바뀌지 않는다.
    monkeypatch.setattr(sys, "executable", str(tmp_path / "missing-python"))
    with pytest.raises(OSError):
        engine_transfer._launch_completion(home.root, source, 0, [])
