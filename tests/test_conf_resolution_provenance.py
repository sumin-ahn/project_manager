"""Cross-repo local.conf 분기와 송신 프로필 provenance.

두 외부 송신 표면만 대상으로 한다. `_find_repo_root` 보유 도구 전수는 아래 inventory 테스트가
기계로 다시 뽑아, local.conf 로 송신 대상을 고르지 않는 형제의 제외도 명시적으로 고정한다.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external():
    return _load("external_review")


@pytest.fixture
def delegate():
    return _load("pm_delegate")


def _repo(root: Path, conf: dict[str, str] | None) -> Path:
    pm = root / ".project_manager"
    pm.mkdir(parents=True)
    (root / ".git").mkdir()
    if conf is not None:
        (pm / "local.conf").write_text(
            "".join(f"{key}={value}\n" for key, value in conf.items()),
            encoding="utf-8",
        )
    return root


def _delegate_conf(reasoning: str = "medium", *, include_developer: bool = False):
    conf = {
        "delegate_enabled": "true",
        "delegate.researcher.harness": "codex",
        "delegate.researcher.model": "gpt-5.6-terra",
        "delegate.researcher.reasoning": reasoning,
    }
    if include_developer:
        conf.update({
            "delegate.developer.harness": "codex",
            "delegate.developer.model": "gpt-5.6-sol",
        })
    return conf


def _delegate_dry_run(delegate, monkeypatch, engine: Path, target: Path,
                      engine_conf: dict[str, str], *, role: str = "researcher",
                      tier: str | None = None,
                      cli_override: tuple[str, str, str | None] | None = None):
    prompt = target / "prompt.md"
    prompt.write_text("구현을 조사하라.", encoding="utf-8")
    monkeypatch.setattr(delegate, "REPO", engine)
    monkeypatch.setattr(delegate, "local_config", lambda: dict(engine_conf))
    monkeypatch.setattr(delegate, "_cwd_in_git_repo", lambda *args, **kwargs: True)
    argv = [
        "--role", role,
        "--prompt-file", str(prompt),
        "--cwd", str(target),
        "--dry-run",
    ]
    if tier is not None:
        argv.extend(["--tier", tier])
    if cli_override is not None:
        harness, model, reasoning = cli_override
        argv.extend(["--harness", harness, "--model", model])
        if reasoning is not None:
            argv.extend(["--reasoning", reasoning])
    return delegate.main(argv)


def _wire_external(external, monkeypatch, engine: Path, target: Path,
                   engine_conf: dict[str, str]):
    monkeypatch.setattr(external, "REPO", engine)
    monkeypatch.setattr(external, "local_config", lambda: dict(engine_conf))
    monkeypatch.setattr(external, "_pm_home_reanchor", lambda anchor: None)
    monkeypatch.setattr(
        external,
        "extract_diff",
        lambda *args, **kwargs: ("diff --git a/x.py b/x.py\n-old\n+new\n", []),
    )
    monkeypatch.chdir(target)


def test_delegate_cross_repo_same_role_different_value_warns_without_blocking(
        delegate, monkeypatch, tmp_path, capsys):
    engine_conf = _delegate_conf("medium")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", _delegate_conf("low"))

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("[pm-delegate] config provenance:")
    assert "local.conf 프로필 분기" in err
    assert "경고:" in err
    assert "delegate.researcher.reasoning" in err
    assert "실행 엔진 conf가 이깁니다" in err
    assert "차단하지 않고 계속합니다" in err
    assert str(engine / ".project_manager" / "local.conf") in err
    assert str(target / ".project_manager" / "local.conf") in err


def test_delegate_effective_profile_detects_explicit_reasoning_vs_omitted_default(
        delegate, monkeypatch, tmp_path, capsys):
    """원시 키 교집합이면 놓치던 `medium` 대 미지정(None) 실제 tuple 차이를 경고한다."""
    engine_conf = _delegate_conf("medium")
    target_conf = _delegate_conf("low")
    target_conf.pop("delegate.researcher.reasoning")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", target_conf)

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" in err
    assert (
        "delegate.researcher.reasoning: engine='medium', cwd=None"
        in err
    )


def test_delegate_cli_override_skips_conf_divergence_and_marks_source(
        delegate, monkeypatch, tmp_path, capsys):
    """완전 CLI tuple은 local.conf primary 프로필을 쓰지 않으므로 분기 경고 없이 출처를 밝힌다."""
    engine_conf = _delegate_conf("medium")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", _delegate_conf("low"))

    assert _delegate_dry_run(
        delegate, monkeypatch, engine, target, engine_conf,
        cli_override=("codex", "gpt-5.6-sol", "high"),
    ) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err
    assert "source=cli-override" in err
    assert (
        "resolved_profile=(harness=codex, model=gpt-5.6-sol, reasoning=high)"
        in err
    )


def test_delegate_cross_repo_same_values_is_quiet_and_shows_provenance(
        delegate, monkeypatch, tmp_path, capsys):
    engine_conf = _delegate_conf("medium")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", engine_conf)

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    captured = capsys.readouterr()
    assert "local.conf 프로필 분기" not in captured.err
    assert captured.err.splitlines()[0].startswith("[pm-delegate] config provenance:")
    assert (
        f"[pm-delegate] config provenance: "
        f"local_conf={engine / '.project_manager' / 'local.conf'}"
    ) in captured.err
    assert (
        "resolved_profile=(harness=codex, model=gpt-5.6-terra, reasoning=medium)"
        in captured.err
    )


def test_delegate_cross_repo_missing_target_conf_is_quiet(
        delegate, monkeypatch, tmp_path, capsys):
    engine_conf = _delegate_conf("medium")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", None)

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    assert "local.conf 프로필 분기" not in capsys.readouterr().err


def test_delegate_operational_engine_mapping_and_partial_target_enters_normal_path(
        delegate, monkeypatch, tmp_path, capsys):
    engine_conf = _delegate_conf("medium", include_developer=True)
    engine_conf.update({
        "delegate.developer.hard.harness": "codex",
        "delegate.developer.hard.model": "gpt-5.6-sol",
        "delegate.developer.hard.reasoning": "high",
        "delegate.architect.harness": "codex",
        "delegate.architect.model": "gpt-5.6-sol",
        "delegate.code-reviewer.harness": "claude",
        "delegate.code-reviewer.model": "opus",
    })
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", _delegate_conf("medium"))

    assert _delegate_dry_run(
        delegate, monkeypatch, engine, target, engine_conf,
        role="developer", tier="hard",
    ) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err
    assert "resolved_profile=(harness=codex, model=gpt-5.6-sol, reasoning=high)" in err


def test_delegate_difference_in_unused_role_is_quiet(
        delegate, monkeypatch, tmp_path, capsys):
    engine_conf = _delegate_conf("medium", include_developer=True)
    engine = _repo(tmp_path / "engine", engine_conf)
    target_conf = _delegate_conf("low", include_developer=True)
    target = _repo(tmp_path / "target", target_conf)

    assert _delegate_dry_run(
        delegate, monkeypatch, engine, target, engine_conf, role="developer",
    ) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err
    assert "delegate.researcher.reasoning" not in err


def test_delegate_primary_raw_records_conf_and_profile(
        delegate, monkeypatch, tmp_path, capsys):
    conf = _delegate_conf("medium")
    engine = _repo(tmp_path / "engine", conf)
    target = _repo(tmp_path / "target", _delegate_conf("low"))
    prompt = target / "prompt.md"
    prompt.write_text("조사하라.", encoding="utf-8")
    output_dir = tmp_path / "raw"

    class Run:
        def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "DONE"},
                }),
            ])
            return {
                "returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False,
            }

    monkeypatch.setattr(delegate, "REPO", engine)
    monkeypatch.setattr(delegate, "local_config", lambda: dict(conf))
    monkeypatch.setattr(delegate, "_cwd_in_git_repo", lambda *args, **kwargs: True)
    rc = delegate.main([
        "--role", "researcher",
        "--prompt-file", str(prompt),
        "--cwd", str(target),
        "--output-dir", str(output_dir),
    ], run_fn=Run())

    assert rc == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith(
        "[pm-delegate] config provenance:"
    )
    assert "local.conf 프로필 분기" in err
    raw = next(output_dir.glob("pm_delegate_codex_*.txt")).read_text(encoding="utf-8")
    assert f"# local_conf: {engine / '.project_manager' / 'local.conf'}" in raw
    assert (
        "# resolved_profile: (harness=codex, model=gpt-5.6-terra, reasoning=medium)"
        in raw
    )
    assert "# profile_source: local-conf" in raw


def test_delegate_cli_override_raw_records_override_source(
        delegate, monkeypatch, tmp_path, capsys):
    conf = _delegate_conf("medium")
    engine = _repo(tmp_path / "engine", conf)
    target = _repo(tmp_path / "target", _delegate_conf("low"))
    prompt = target / "prompt.md"
    prompt.write_text("조사하라.", encoding="utf-8")
    output_dir = tmp_path / "raw"

    class Run:
        def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "DONE"},
                }),
            ])
            return {
                "returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False,
            }

    monkeypatch.setattr(delegate, "REPO", engine)
    monkeypatch.setattr(delegate, "local_config", lambda: dict(conf))
    monkeypatch.setattr(delegate, "_cwd_in_git_repo", lambda *args, **kwargs: True)
    rc = delegate.main([
        "--role", "researcher",
        "--prompt-file", str(prompt),
        "--cwd", str(target),
        "--harness", "codex",
        "--model", "gpt-5.6-sol",
        "--reasoning", "high",
        "--output-dir", str(output_dir),
    ], run_fn=Run())

    assert rc == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err
    assert "source=cli-override" in err
    raw = next(output_dir.glob("pm_delegate_codex_*.txt")).read_text(encoding="utf-8")
    assert "# profile_source: cli-override" in raw
    assert (
        "# resolved_profile: (harness=codex, model=gpt-5.6-sol, reasoning=high)"
        in raw
    )


def test_external_review_cross_repo_different_reviewer_warns_without_blocking(
        external, monkeypatch, tmp_path, capsys):
    engine_conf = {
        "external_review_enabled": "true",
        "reviewer_cmd": "codex exec --sandbox read-only",
    }
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", {
        "reviewer_cmd": "claude -p --tools Read",
    })
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("[external-review] config provenance:")
    assert "local.conf 프로필 분기" in err
    assert "경고:" in err
    assert "reviewer_cmd" in err
    assert "실행 엔진 conf가 이깁니다" in err
    assert "차단하지 않고 계속합니다" in err
    assert str(engine / ".project_manager" / "local.conf") in err
    assert str(target / ".project_manager" / "local.conf") in err


def test_external_review_same_effective_reviewer_is_quiet_with_provenance(
        external, monkeypatch, tmp_path, capsys):
    # engine 은 reviewer_cmd 미지정, target 은 현행 default 를 명시 — 실제 송신값은 같으므로 무소음.
    engine_conf = {"external_review_enabled": "true"}
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", {
        "reviewer_cmd": external.DEFAULT_REVIEWER_CMD,
    })
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "local.conf 프로필 분기" not in captured.err
    assert captured.err.splitlines()[0].startswith("[external-review] config provenance:")
    assert (
        f"[external-review] config provenance: "
        f"local_conf={engine / '.project_manager' / 'local.conf'}"
    ) in captured.err
    assert f"resolved_profile=(reviewer_cmd={external.DEFAULT_REVIEWER_CMD}" in captured.err


def test_external_review_missing_target_conf_is_quiet(
        external, monkeypatch, tmp_path, capsys):
    engine_conf = {"external_review_enabled": "true"}
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", None)
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    assert "local.conf 프로필 분기" not in capsys.readouterr().err


def test_external_review_actual_execution_keeps_same_provenance(
        external, monkeypatch, tmp_path, capsys):
    conf = {"external_review_enabled": "true"}
    engine = _repo(tmp_path / "engine", conf)
    _wire_external(external, monkeypatch, engine, engine, conf)
    seen = {}

    def _run_review(*args, **kwargs):
        seen.update(kwargs)
        return {
            "reviewer": "codex", "ok": True, "output": "판정: 통과",
            "verdict": {"has_must_fix": False, "has_pass": True},
            "file": None, "failed": False, "started": True,
            "any_must_fix": False, "all_pass": True,
        }

    monkeypatch.setattr(external, "run_review", _run_review)
    assert external.main(["--paths", "x.py"]) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("[external-review] config provenance:")
    assert seen["local_conf_path"] == engine / ".project_manager" / "local.conf"
    assert seen["resolved_profile"].startswith("(reviewer_cmd=codex exec")


def test_external_review_invalid_timeout_warning_follows_first_line_provenance(
        external, monkeypatch, tmp_path, capsys):
    """fail-soft timeout 경고가 있어도 stderr 첫 줄은 항상 config provenance다."""
    conf = {
        "external_review_enabled": "true",
        "harness.codex.wall_timeout": "not-a-timeout",
    }
    engine = _repo(tmp_path / "engine", conf)
    _wire_external(external, monkeypatch, engine, engine, conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("[external-review] config provenance:")
    warning_index = next(
        index for index, line in enumerate(lines)
        if "harness.codex.wall_timeout='not-a-timeout'" in line
    )
    assert warning_index > 0


def test_delegate_fail_loud_resolution_names_absolute_local_conf(
        delegate, monkeypatch, tmp_path, capsys):
    """hard 프로필 미설정 진단은 어떤 엔진 conf를 읽었는지 절대경로를 병기한다."""
    conf = _delegate_conf("medium", include_developer=True)
    engine = _repo(tmp_path / "engine", conf)
    prompt = engine / "prompt.md"
    prompt.write_text("구현하라.", encoding="utf-8")
    monkeypatch.setattr(delegate, "REPO", engine)
    monkeypatch.setattr(delegate, "local_config", lambda: dict(conf))

    assert delegate.main([
        "--role", "developer",
        "--tier", "hard",
        "--prompt-file", str(prompt),
        "--cwd", str(engine),
        "--dry-run",
    ]) == 1
    err = capsys.readouterr().err
    assert "hard 프로필 미설정" in err
    assert f"local.conf: {(engine / '.project_manager' / 'local.conf').resolve()}" in err


def test_delegate_disabled_names_absolute_local_conf(
        delegate, monkeypatch, tmp_path, capsys):
    """opt-in OFF rc=3도 실제로 읽은 엔진 conf 절대경로를 잃지 않는다."""
    conf = {
        "delegate_enabled": "false",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-5.6-sol",
    }
    engine = _repo(tmp_path / "engine", conf)
    prompt = engine / "prompt.md"
    prompt.write_text("구현하라.", encoding="utf-8")
    monkeypatch.setattr(delegate, "REPO", engine)
    monkeypatch.setattr(delegate, "local_config", lambda: dict(conf))

    assert delegate.main([
        "--role", "developer",
        "--prompt-file", str(prompt),
        "--cwd", str(engine),
    ]) == 3
    err = capsys.readouterr().err
    assert f"local.conf: {(engine / '.project_manager' / 'local.conf').resolve()}" in err


def test_delegate_subdirectory_cwd_still_triggers_pm_home_reanchor(
        delegate, monkeypatch, tmp_path, capsys):
    """repo-root 해소값을 재사용해 PM 홈 하위 `--cwd` 우회도 canonical worktree로 재앵커한다."""
    conf = {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-5.6-sol",
    }
    home = _repo(tmp_path / "pm-home", conf)
    ticket_dir = home / ".project_manager" / "board" / "tickets" / "open"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "T-0001.md").write_text("---\nid: T-0001\n---\n", encoding="utf-8")
    worktree = home / "work" / "canonical"
    worktree_tools = worktree / ".project_manager" / "tools"
    worktree_tools.mkdir(parents=True)
    (worktree_tools / "external_review.py").write_text("# marker\n", encoding="utf-8")
    nested_cwd = home / "work-area" / "nested"
    nested_cwd.mkdir(parents=True)
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text(
        "수정 대상: .project_manager/tools/pm_delegate.py",
        encoding="utf-8",
    )
    monkeypatch.setattr(delegate, "REPO", home)
    monkeypatch.setattr(delegate, "local_config", lambda: dict(conf))
    monkeypatch.setattr(delegate, "_cwd_in_git_repo", lambda *args, **kwargs: True)

    assert delegate.main([
        "--role", "developer",
        "--prompt-file", str(prompt),
        "--cwd", str(nested_cwd),
        "--dry-run",
    ]) == 1
    err = capsys.readouterr().err
    assert "재앵커" in err
    assert str(worktree) in err


def test_divergence_helper_without_cwd_repo_or_with_same_repo_is_quiet(
        external, tmp_path):
    engine = _repo(tmp_path / "engine", {"reviewer_cmd": "codex exec"})
    for target_repo in (None, engine):
        assert external.local_conf_divergence(
            engine_repo=engine,
            engine_conf={"reviewer_cmd": "codex exec"},
            target_repo=target_repo,
            selector=external.reviewer_profile_config,
        ) is None


def test_external_review_raw_records_conf_and_profile(external, tmp_path):
    conf_path = tmp_path / "engine" / ".project_manager" / "local.conf"
    result = external.run_review(
        "prompt",
        reviewer_cmd="codex",
        timeout=23,
        idle_timeout=11,
        output_dir=tmp_path,
        run_fn=lambda *args, **kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": "판정: 통과", "stderr": ""}
        )(),
        local_conf_path=conf_path,
        resolved_profile="(reviewer_cmd=codex, wall_timeout_sec=23, idle_timeout_sec=11)",
    )

    raw = result["file"].read_text(encoding="utf-8")
    assert f"# local_conf: {conf_path}" in raw
    assert (
        "# resolved_profile: "
        "(reviewer_cmd=codex, wall_timeout_sec=23, idle_timeout_sec=11)"
    ) in raw


def test_find_repo_root_tool_inventory_and_scope_are_explicit():
    found = {
        path.name
        for path in TOOLS.glob("*.py")
        if "def _find_repo_root()" in path.read_text(encoding="utf-8")
    }
    assert found == {
        "contradiction_lint.py",
        "external_review.py",
        "pm_delegate.py",
        "ticket_finish.py",
    }

    source = {
        name: (TOOLS / name).read_text(encoding="utf-8")
        for name in found
    }
    assert "reviewer_cmd" in source["external_review.py"]
    assert "delegate." in source["pm_delegate.py"]
    assert "local.conf" not in source["contradiction_lint.py"]
    # ticket_finish 는 local.conf 를 읽지만 외부 송신 프로필 키/소비자는 없다.
    assert "LOCAL_CONF" in source["ticket_finish.py"]
    assert "reviewer_cmd" not in source["ticket_finish.py"]
    assert "delegate." not in source["ticket_finish.py"]
