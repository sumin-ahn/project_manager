"""Cross-repo local.conf 분기와 송신 프로필 provenance.

두 외부 송신 표면만 대상으로 한다. `_find_repo_root` 보유 도구 전수는 아래 inventory 테스트가
기계로 다시 뽑아, local.conf 로 송신 대상을 고르지 않는 형제의 제외도 명시적으로 고정한다.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
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
    return _load("additional_reviewer")


@pytest.fixture
def delegate():
    return _load("pm_delegate")


def _repo(root: Path, conf: dict[str, str] | None) -> Path:
    pm = root / ".project_manager"
    pm.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if conf is not None:
        (pm / "local.conf").write_text(
            "".join(f"{key}={value}\n" for key, value in conf.items()),
            encoding="utf-8",
        )
    return root


# 해소 가능한 추가 리뷰어 대상 — 대상은 `harness`+`model` 구조화 키로만 서므로(엔진 기본 커맨드
# 없음) 리뷰 축 형상은 전부 이 세트를 깔고 시작한다.
_REVIEWER_TARGET = {
    "additional_reviewer.enabled": "true",
    "additional_reviewer.harness": "codex",
    "additional_reviewer.model": "gpt-5.6-sol",
}


def _delegate_conf(reasoning: str = "medium", *, include_developer: bool = False):
    conf = {
        "delegate.enabled": "true",
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


def _with_researcher_fallback(conf: dict[str, str], model: str = "opus"):
    conf.update({
        "delegate.researcher.fallback.harness": "claude",
        "delegate.researcher.fallback.model": model,
        "delegate.researcher.fallback.reasoning": "high",
    })
    return conf


def _delegate_dry_run(delegate, monkeypatch, engine: Path, target: Path,
                      engine_conf: dict[str, str], *, role: str = "researcher",
                      tier: str | None = None,
                      cli_override: tuple[str, str, str | None] | None = None,
                      inject_conf: bool = True):
    prompt = target / "prompt.md"
    prompt.write_text("구현을 조사하라.", encoding="utf-8")
    monkeypatch.setattr(delegate, "REPO", engine)
    if inject_conf:
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
    monkeypatch.setattr(external, "local_config", lambda repo=None: dict(engine_conf))
    monkeypatch.setattr(
        external, "resolve_pm_home_for_repo", lambda anchor, **kwargs: engine,
    )
    monkeypatch.setattr(
        external, "_resolve_diff_root", lambda *args, **kwargs: target,
    )
    monkeypatch.setattr(
        external,
        "extract_diff",
        lambda *args, **kwargs: ("diff --git a/x.py b/x.py\n-old\n+new\n", []),
    )
    # 리뷰어 가시 범위 거울 스텁 (T-0563) — 이 파일은 conf provenance 만 본다. 픽스처 앵커는 실
    # git 저장소가 아니라 거울을 못 만든다. 실 거울 회귀는
    # test_additional_reviewer_reviewer_isolation.py 가 실 저장소로 소유한다.
    monkeypatch.setattr(
        external, "create_reviewer_workspace",
        lambda diff_root, *, base_dir=None, conf=None, source_home=None, denylist=():
        external.ReviewerWorkspace(
            root=Path(tempfile.mkdtemp(prefix="stub_reviewer_mirror_")),
            tree=Path(tempfile.mkdtemp(prefix="stub_reviewer_tree_")),
            home=Path(tempfile.mkdtemp(prefix="stub_reviewer_home_")),
            files=1, skipped_unsafe=0, git_repo=True,
        ),
    )


def test_delegate_uses_derived_target_config_without_engine_divergence(
        delegate, monkeypatch, tmp_path, capsys):
    engine_conf = _delegate_conf("medium")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", _delegate_conf("low"))

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("[pm-delegate] config provenance:")
    assert "local.conf 프로필 분기" not in err
    assert str(target / ".project_manager" / "local.conf") in err


def test_delegate_profile_comparison_uses_derived_config_anchor(
        delegate, monkeypatch, tmp_path, capsys):
    """실행 엔진 값과 달라도 파생 config와 비교 대상을 같은 repo로 유지한다."""
    engine_conf = _delegate_conf("medium")
    target_conf = _delegate_conf("low")
    target_conf.pop("delegate.researcher.reasoning")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", target_conf)

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err


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
        f"local_conf={target / '.project_manager' / 'local.conf'}"
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


def test_delegate_effective_fallback_uses_derived_config_anchor(
        delegate, monkeypatch, tmp_path, capsys):
    """폴백 비교도 파생 config repo를 engine 좌표로 사용한다."""
    engine_conf = _with_researcher_fallback(_delegate_conf(), "opus")
    target_conf = _with_researcher_fallback(_delegate_conf(), "sonnet")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", target_conf)

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err


def test_delegate_target_fallback_equal_to_its_primary_is_not_compared(
        delegate, monkeypatch, tmp_path, capsys):
    """대상 fallback이 자기 primary와 같은 비발동 tuple이면 엔진 fallback과 거짓 비교하지 않는다."""
    engine_conf = _with_researcher_fallback(_delegate_conf(), "opus")
    target_conf = _delegate_conf()
    target_conf.update({
        "delegate.researcher.fallback.harness": "codex",
        "delegate.researcher.fallback.model": "gpt-5.6-terra",
        # reasoning만 달라도 main 규칙상 같은 채널/모델 재타격이라 비발동이다.
        "delegate.researcher.fallback.reasoning": "high",
    })
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", target_conf)

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err
    assert "fallback." not in err


def test_delegate_same_effective_fallback_is_quiet(
        delegate, monkeypatch, tmp_path, capsys):
    conf = _with_researcher_fallback(_delegate_conf(), "opus")
    engine = _repo(tmp_path / "engine", conf)
    target = _repo(tmp_path / "target", dict(conf))

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, conf) == 0
    assert "local.conf 프로필 분기" not in capsys.readouterr().err


def test_delegate_engine_only_fallback_is_quiet(
        delegate, monkeypatch, tmp_path, capsys):
    """한쪽 conf만의 fallback은 흔한 per-clone 부분 mapping이라 비교 경고를 만들지 않는다."""
    engine_conf = _with_researcher_fallback(_delegate_conf(), "opus")
    target_conf = _delegate_conf()
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", target_conf)

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err
    assert "fallback." not in err


def test_delegate_target_only_fallback_is_quiet_for_unconfigured_execution(
        delegate, monkeypatch, tmp_path, capsys):
    """실행 엔진에 폴백이 없는 실행은 대상의 미사용 폴백 차이를 판정하지 않는다."""
    engine_conf = _delegate_conf()
    target_conf = _with_researcher_fallback(_delegate_conf(), "opus")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", target_conf)

    assert _delegate_dry_run(delegate, monkeypatch, engine, target, engine_conf) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err
    assert "fallback." not in err


def test_delegate_cli_override_suppresses_configured_fallback_divergence(
        delegate, monkeypatch, tmp_path, capsys):
    engine_conf = _with_researcher_fallback(_delegate_conf(), "opus")
    target_conf = _with_researcher_fallback(_delegate_conf("low"), "sonnet")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", target_conf)

    assert _delegate_dry_run(
        delegate, monkeypatch, engine, target, engine_conf,
        cli_override=("codex", "gpt-5.6-sol", "high"),
    ) == 0
    err = capsys.readouterr().err
    assert "local.conf 프로필 분기" not in err
    assert "source=cli-override" in err


def test_delegate_valid_secret_ack_suppresses_unreachable_fallback_warning(
        delegate, monkeypatch, tmp_path, capsys):
    """유효 ack가 fallback을 확정 억제한 뒤에는 양쪽 fallback 값이 달라도 경고하지 않는다."""
    engine_conf = _with_researcher_fallback(_delegate_conf(), "opus")
    target_conf = _with_researcher_fallback(_delegate_conf(), "sonnet")
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", target_conf)
    task = "예시 ~/.aws/credentials 를 확인하라."
    prompt_file = target / "prompt.md"
    prompt_file.write_text(task, encoding="utf-8")
    output_dir = tmp_path / "raw"
    monkeypatch.setattr(delegate, "REPO", engine)
    monkeypatch.setattr(delegate, "local_config", lambda: dict(engine_conf))
    monkeypatch.setattr(delegate, "_cwd_in_git_repo", lambda *args, **kwargs: True)
    monkeypatch.setattr(delegate, "_resolved_adapter_directories", lambda: ())
    monkeypatch.setattr(delegate, "begin_scope_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(delegate, "report_scope_audit", lambda *args, **kwargs: None)
    prompt = delegate._role_preamble("researcher", ()) + "\n\n" + task
    digest = delegate.secret_scan_prompt_digest(
        prompt, "codex", "gpt-5.6-terra",
    )

    def _run(*args, **kwargs):
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

    assert delegate.main([
        "--role", "researcher",
        "--prompt-file", str(prompt_file),
        "--cwd", str(target),
        "--output-dir", str(output_dir),
        "--secret-scan-ack", digest,
    ], run_fn=_run) == 0
    err = capsys.readouterr().err
    assert "시크릿 스캔 차단을 명시 승인으로 통과" in err
    assert "local.conf 프로필 분기" not in err
    assert "fallback.model" not in err


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
    assert "local.conf 프로필 분기" not in err
    raw = next(output_dir.glob("pm_delegate_codex_*.txt")).read_text(encoding="utf-8")
    assert f"# local_conf: {target / '.project_manager' / 'local.conf'}" in raw
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


def test_additional_reviewer_cross_repo_different_reviewer_warns_without_blocking(
        external, monkeypatch, tmp_path, capsys):
    engine_conf = dict(_REVIEWER_TARGET)
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", {
        "additional_reviewer.enabled": "true",
        "additional_reviewer.harness": "claude",
        "additional_reviewer.model": "opus",
    })
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("[additional-reviewer] config provenance:")
    assert "local.conf 프로필 분기" in err
    assert "경고:" in err
    assert "reviewer_cmd" in err
    assert "해소된 PM 홈 conf가 적용됩니다" in err
    assert f"해소된 PM 홈 REPO: {engine.resolve()}" in err
    assert f"diff worktree repo: {target.resolve()}" in err
    assert "engine REPO:" not in err
    assert "의도한 local.conf를 가진 엔진 사본을 실행하세요" not in err
    assert "차단하지 않고 계속합니다" in err
    assert str(engine / ".project_manager" / "local.conf") in err
    assert str(target / ".project_manager" / "local.conf") in err


def test_additional_reviewer_same_effective_reviewer_is_quiet_with_provenance(
        external, monkeypatch, tmp_path, capsys):
    # 두 conf 가 같은 구조화 대상을 지정 — 실제 송신값이 같으므로 무소음이다.
    engine_conf = dict(_REVIEWER_TARGET)
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", dict(_REVIEWER_TARGET))
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "local.conf 프로필 분기" not in captured.err
    assert captured.err.splitlines()[0].startswith("[additional-reviewer] config provenance:")
    assert (
        f"[additional-reviewer] config provenance: "
        f"local_conf={engine / '.project_manager' / 'local.conf'}"
    ) in captured.err
    resolved = external.resolve_reviewer_target(engine_conf).command
    assert f"resolved_profile=(reviewer_cmd={resolved}" in captured.err


def test_additional_reviewer_missing_target_conf_is_quiet(
        external, monkeypatch, tmp_path, capsys):
    engine_conf = dict(_REVIEWER_TARGET)
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", None)
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    assert "local.conf 프로필 분기" not in capsys.readouterr().err


def test_additional_reviewer_dangling_target_conf_symlink_fails_closed(
        external, monkeypatch, tmp_path, capsys):
    """dangling local.conf는 부재가 아니라 판독 실패이며 대상 denylist 미확인 송신을 차단한다."""
    engine_conf = dict(_REVIEWER_TARGET)
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", None)
    target_conf = target / ".project_manager" / "local.conf"
    target_conf.symlink_to(target / "missing-target.conf")
    _wire_external(external, monkeypatch, engine, target, engine_conf)
    extracted = []
    monkeypatch.setattr(
        external, "extract_diff", lambda *args, **kwargs: extracted.append(True),
    )

    assert external.main(["--paths", "x.py"]) == 1
    err = capsys.readouterr().err
    assert "대상 local.conf 읽기 실패" in err
    assert "외부 송신 전에 중단" in err
    assert str(target_conf) in err
    assert extracted == []


@pytest.mark.parametrize("failure_kind", ["invalid-utf8", "not-a-file"])
def test_additional_reviewer_existing_unreadable_target_conf_fails_closed(
        external, monkeypatch, tmp_path, capsys, failure_kind):
    """대상 conf의 정상 부재와 달리, 존재하지만 읽기/해석 불가면 diff 추출 전에 중단한다."""
    engine_conf = dict(_REVIEWER_TARGET)
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", None)
    target_conf = target / ".project_manager" / "local.conf"
    if failure_kind == "invalid-utf8":
        target_conf.write_bytes(b"\xff\xfe\x00")
    else:
        target_conf.mkdir()
    _wire_external(external, monkeypatch, engine, target, engine_conf)
    extracted = []
    monkeypatch.setattr(
        external, "extract_diff", lambda *args, **kwargs: extracted.append(True),
    )

    assert external.main(["--paths", "x.py"]) == 1
    err = capsys.readouterr().err
    assert "대상 local.conf 읽기 실패" in err
    assert "외부 송신 전에 중단" in err
    assert str(target_conf) in err
    assert extracted == []


def test_delegate_target_conf_read_error_is_caught_without_traceback(
        delegate, monkeypatch, tmp_path, capsys):
    """raise/catch가 같은 additional_reviewer 모듈 클래스를 써 판독 오류를 rc=1 진단으로 닫는다."""
    engine_conf = _delegate_conf()
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", None)
    target_conf = target / ".project_manager" / "local.conf"
    target_conf.write_bytes(b"\xff\xfe\x00")

    assert _delegate_dry_run(
        delegate, monkeypatch, engine, target, engine_conf, inject_conf=False,
    ) == 1
    err = capsys.readouterr().err
    assert "해소된 local.conf 읽기 실패" in err
    assert "외부 송신 전에 중단" in err
    assert str(target_conf) in err
    assert "Traceback" not in err


def test_additional_reviewer_target_only_denylist_warns_and_is_union_applied(
        external, monkeypatch, tmp_path, capsys):
    """대상 보호 선언을 경고만 하고 무시하지 않고 실제 diff denylist에 합친다."""
    engine_conf = {
        **_REVIEWER_TARGET,
        "additional_reviewer.denylist_extra": "*.engine-vault",
    }
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", {
        **_REVIEWER_TARGET,
        "additional_reviewer.denylist_extra": "*.target-private",
    })
    _wire_external(external, monkeypatch, engine, target, engine_conf)
    seen = {}

    def _extract(*args, **kwargs):
        seen["denylist"] = kwargs["denylist"]
        return "diff --git a/x.py b/x.py\n-old\n+new\n", []

    monkeypatch.setattr(external, "extract_diff", _extract)
    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "additional_reviewer.denylist_extra" in err
    assert "diff-worktree-only=('*.target-private',)" in err
    assert "합집합 적용" in err
    assert "*.engine-vault" in seen["denylist"]
    assert "*.target-private" in seen["denylist"]
    assert external._matching_denylist_pattern(
        "config.target-private", seen["denylist"],
    ) == "*.target-private"


def test_additional_reviewer_same_denylist_is_quiet(
        external, monkeypatch, tmp_path, capsys):
    conf = {
        **_REVIEWER_TARGET,
        "additional_reviewer.denylist_extra": "*.private *.vault",
    }
    engine = _repo(tmp_path / "engine", conf)
    target = _repo(tmp_path / "target", dict(conf))
    _wire_external(external, monkeypatch, engine, target, conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    assert "local.conf 프로필 분기" not in capsys.readouterr().err


def test_additional_reviewer_engine_denylist_superset_is_safe_and_quiet(
        external, monkeypatch, tmp_path, capsys):
    """엔진이 대상 선언을 이미 모두 포함하면 값 문자열이 달라도 안전 방향이라 무소음이다."""
    engine_conf = {
        **_REVIEWER_TARGET,
        "additional_reviewer.denylist_extra": "*.private *.vault",
    }
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", {
        **_REVIEWER_TARGET,
        "additional_reviewer.denylist_extra": "*.private",
    })
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    assert "local.conf 프로필 분기" not in capsys.readouterr().err


def test_additional_reviewer_effective_review_paths_difference_warns_when_used(
        external, monkeypatch, tmp_path, capsys):
    engine_conf = {
        **_REVIEWER_TARGET,
        "additional_reviewer.paths": "src tests",
    }
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", {
        **_REVIEWER_TARGET,
        "additional_reviewer.paths": "src docs",
    })
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--dry-run"]) == 0
    err = capsys.readouterr().err
    assert (
        "additional_reviewer.paths: pm-home=('src', 'tests'), "
        "diff-worktree=('docs', 'src')"
    ) in err
    assert "review_paths의 이번 범위는 해소된 PM 홈 conf가 정했습니다" in err
    assert "두 conf를 맞추거나" not in err
    assert "차단하지 않고 계속합니다" in err


def test_additional_reviewer_same_effective_review_path_set_is_quiet(
        external, monkeypatch, tmp_path, capsys):
    """순서·중복과 src/src/./src 표기만 다르면 같은 Git 경로 집합이라 무소음이다."""
    engine_conf = {
        **_REVIEWER_TARGET,
        "additional_reviewer.paths": "src tests src/",
    }
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", {
        **_REVIEWER_TARGET,
        "additional_reviewer.paths": "tests/,./src",
    })
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--dry-run"]) == 0
    assert "local.conf 프로필 분기" not in capsys.readouterr().err


def test_additional_reviewer_target_denylist_explicit_path_uses_real_filter_and_blocks(
        external, monkeypatch, tmp_path, capsys):
    """대상 전용 합집합 패턴은 실 extract_diff 필터를 거쳐 명시 --paths를 fail-loud 차단한다."""
    engine_conf = {
        **_REVIEWER_TARGET,
        "additional_reviewer.denylist_extra": "*.engine-vault",
    }
    engine = tmp_path / "engine"
    (engine / ".project_manager").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(engine)], check=True)
    protected = engine / "config.target-private"
    protected.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(engine), "add", protected.name], check=True)
    subprocess.run([
        "git", "-C", str(engine),
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "baseline",
    ], check=True)
    protected.write_text("after\n", encoding="utf-8")
    target = _repo(tmp_path / "target", {
        **_REVIEWER_TARGET,
        "additional_reviewer.denylist_extra": "*.engine-vault",
    })
    (engine / ".project_manager" / "local.conf").write_text(
        "additional_reviewer.denylist_extra=*.target-private\n", encoding="utf-8",
    )
    monkeypatch.setattr(external, "REPO", engine)
    monkeypatch.setattr(external, "local_config", lambda repo=None: dict(engine_conf))
    monkeypatch.setattr(
        external, "resolve_pm_home_for_repo", lambda anchor, **kwargs: target,
    )
    monkeypatch.setattr(
        external, "_resolve_diff_root", lambda *args, **kwargs: engine,
    )

    assert external.main([
        "--paths", protected.name, "--dry-run",
    ]) == 1
    captured = capsys.readouterr()
    assert "--paths 로 명시 지정한 경로" in captured.err
    assert protected.name in captured.err
    assert "*.target-private" in captured.err
    assert "외부 호출 생략" not in captured.out


def test_additional_reviewer_explicit_paths_suppresses_unused_review_paths_difference(
        external, monkeypatch, tmp_path, capsys):
    engine_conf = {
        **_REVIEWER_TARGET,
        "additional_reviewer.paths": "src tests",
    }
    engine = _repo(tmp_path / "engine", engine_conf)
    target = _repo(tmp_path / "target", {
        **_REVIEWER_TARGET,
        "additional_reviewer.paths": "private docs",
    })
    _wire_external(external, monkeypatch, engine, target, engine_conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "additional_reviewer.paths:" not in err
    assert "local.conf 프로필 분기" not in err


@pytest.mark.parametrize("target_kind", ["none", "same", "missing-conf"])
def test_review_content_new_axis_preserves_structural_quiet_cases(
        external, tmp_path, target_kind):
    engine_conf = {
        "additional_reviewer.denylist_extra": "*.engine",
        "additional_reviewer.paths": "src tests",
    }
    engine = _repo(tmp_path / "engine", engine_conf)
    if target_kind == "none":
        target = None
    elif target_kind == "same":
        target = engine
    else:
        target = _repo(tmp_path / "target", None)

    resolution = external.resolve_review_content_conf(
        engine_repo=engine,
        engine_conf=engine_conf,
        target_repo=target,
        include_review_paths=True,
    )
    assert resolution.divergence is None
    assert resolution.denylist == external._denylist_patterns(engine_conf)


def test_additional_reviewer_actual_execution_keeps_same_provenance(
        external, monkeypatch, tmp_path, capsys):
    conf = dict(_REVIEWER_TARGET)
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
    assert external.main(["--paths", "x.py", "--no-gate"]) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("[additional-reviewer] config provenance:")
    assert seen["local_conf_path"] == engine / ".project_manager" / "local.conf"
    assert seen["resolved_profile"].startswith(
        f"(reviewer_cmd={external.resolve_reviewer_target(conf).command}")


def test_additional_reviewer_invalid_timeout_warning_follows_first_line_provenance(
        external, monkeypatch, tmp_path, capsys):
    """fail-soft timeout 경고가 있어도 stderr 첫 줄은 항상 config provenance다."""
    conf = {
        **_REVIEWER_TARGET,
        "harness.codex.wall_timeout": "not-a-timeout",
    }
    engine = _repo(tmp_path / "engine", conf)
    _wire_external(external, monkeypatch, engine, engine, conf)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("[additional-reviewer] config provenance:")
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
        "delegate.enabled": "false",
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
        "delegate.enabled": "true",
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
    (worktree_tools / "additional_reviewer.py").write_text("# marker\n", encoding="utf-8")
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


def test_additional_reviewer_raw_records_conf_and_profile(external, tmp_path):
    conf_path = tmp_path / "engine" / ".project_manager" / "local.conf"
    result = external.run_review(
        "prompt",
        target=external.resolve_reviewer_target({
            "additional_reviewer.harness": "codex",
            "additional_reviewer.model": "gpt-5.6-sol",
        }),
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
        "additional_reviewer.py",
        "pm_delegate.py",
        "ticket_finish.py",
    }

    source = {
        name: (TOOLS / name).read_text(encoding="utf-8")
        for name in found
    }
    assert "reviewer_cmd" in source["additional_reviewer.py"]
    # 판정 축은 **conf 키 리터럴**(`"delegate.…"`)이다 — 도구 파일명(`pm_delegate.py`)은 키가
    # 아니다. 부분 문자열로 재면 CLI 를 subprocess 로 부르는 호출부까지 키 소비로 읽힌다.
    def _reads_delegate_conf_keys(text: str) -> bool:
        return '"delegate.' in text or "'delegate." in text

    assert _reads_delegate_conf_keys(source["pm_delegate.py"])
    assert "local.conf" not in source["contradiction_lint.py"]
    # ticket_finish 는 local.conf 를 읽지만 외부 송신 프로필 키/소비자는 없다.
    assert "LOCAL_CONF" in source["ticket_finish.py"]
    assert "reviewer_cmd" not in source["ticket_finish.py"]
    assert not _reads_delegate_conf_keys(source["ticket_finish.py"])
