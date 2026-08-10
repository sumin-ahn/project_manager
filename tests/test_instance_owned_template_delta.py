"""T-0617 — instance-owned 파일의 template 세대 델타 관측 표면."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pm_import():
    return _load("pm_import")


@pytest.fixture()
def pm_config():
    return _load("pm_config")


@pytest.fixture()
def pm_update():
    return _load("pm_update")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _case(tmp_path: Path, *, with_baseline: bool = True,
          with_coordinates: bool = True):
    source = tmp_path / "source"
    template_root = source / "templates" / "codex"
    (template_root / ".codex").mkdir(parents=True)
    current = {
        "AGENTS.md": "# Current entry\n\nstable\n",
        ".codex/config.toml": "sandbox = 'workspace-write'\n",
        ".codex/hooks.json": '{"hooks": {}}\n',
    }
    for relpath, text in current.items():
        path = template_root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    dest = tmp_path / "adopter"
    (dest / ".codex").mkdir(parents=True)
    (dest / ".agents").mkdir()
    (dest / ".project_manager").mkdir()
    (dest / "AGENTS.md").write_text("# adopter owned\n", encoding="utf-8")
    (dest / ".codex" / "config.toml").write_text(
        current[".codex/config.toml"], encoding="utf-8")
    (dest / ".codex" / "hooks.json").write_text(
        current[".codex/hooks.json"], encoding="utf-8")
    receipt = {"schema": 2, "harnesses": ["codex"]}
    if with_coordinates:
        receipt["instance_owned_templates"] = {
            relpath: {
                "weight": "full",
                "source": f"templates/codex/{relpath}",
            }
            for relpath in current
        }
    (dest / ".project_manager" / "install.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if with_baseline:
        (dest / ".project_manager" / "local.conf").write_text(
            "upstream_rev=entry-base\n", encoding="utf-8")
        ledger = {
            "schema": 1,
            "files": {
                relpath: {
                    "sha256": _sha(current[relpath]),
                    "recorded_at": "2026-01-01T00:00:00+09:00",
                    "template_rev": "config-base",
                }
                for relpath in (".codex/config.toml", ".codex/hooks.json")
            },
        }
        (dest / ".project_manager" / "adapter_baseline.json").write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest, source, current


class FakeGenerationGit:
    def __init__(self, historical: dict[tuple[str, str], str]):
        self.historical = historical
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]):
        self.calls.append(argv)
        if "rev-parse" in argv:
            if "--show-prefix" in argv:
                return 0, ""
            return 0, "true\n"
        if "cat-file" in argv:
            return 0, ""
        if "show" in argv:
            revision, _, relpath = argv[-1].partition(":")
            key = (revision, relpath)
            return (0, self.historical[key]) if key in self.historical else (1, "missing")
        if "ls-tree" in argv:
            revision, relpath = argv[-3], argv[-1]
            return (0, relpath + "\0") if (revision, relpath) in self.historical else (0, "")
        raise AssertionError(f"unexpected git argv: {argv}")


def _unchanged_history(current: dict[str, str]) -> dict[tuple[str, str], str]:
    return {
        ("entry-base", "templates/codex/AGENTS.md"): current["AGENTS.md"],
        ("config-base", "templates/codex/.codex/config.toml"):
            current[".codex/config.toml"],
        ("config-base", "templates/codex/.codex/hooks.json"):
            current[".codex/hooks.json"],
    }


def test_entry_document_template_change_is_loud_with_manual_remedy(
        pm_import, tmp_path):
    """채널 ``none``인 AGENTS.md도 규모·첫 차이·백업/수동 병합 처방으로 잡힌다."""
    dest, source, current = _case(tmp_path)
    history = _unchanged_history(current)
    history[("entry-base", "templates/codex/AGENTS.md")] = "# Old entry\n\nstable\n"

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(history))

    assert len(lines) == 1, lines
    line = lines[0]
    assert "AGENTS.md" in line and "+1/-1줄" in line and "첫 차이 1줄" in line
    assert "백업" in line and "수동 병합" in line and "재-import" in line
    assert "sync-adapter-config --accept" not in line


def test_config_template_change_points_to_explicit_accept(pm_import, tmp_path):
    """report/managed config 처방은 채택자 명시 수용 커맨드로 분기한다."""
    dest, source, current = _case(tmp_path)
    history = _unchanged_history(current)
    history[("config-base", "templates/codex/.codex/config.toml")] = (
        "sandbox = 'read-only'\n")

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(history))

    assert len(lines) == 1, lines
    assert ".codex/config.toml" in lines[0]
    assert "pm-config sync-adapter-config --accept .codex/config.toml" in lines[0]
    assert "세대 변경" in lines[0] and "config 채널 보고는 내용 drift" in lines[0]
    assert "수동 병합" not in lines[0]


def test_unchanged_template_generation_is_completely_silent(pm_import, tmp_path):
    """baseline과 현행 template byte가 같으면 요약 라인은 0개다."""
    dest, source, current = _case(tmp_path)

    assert pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(_unchanged_history(current))) == []


def test_terminal_newline_only_change_is_not_folded_as_unchanged(pm_import, tmp_path):
    """말미 개행도 template byte 세대 차이다 — ``splitlines()`` 회귀를 막는다."""
    dest, source, current = _case(tmp_path)
    history = _unchanged_history(current)
    history[("entry-base", "templates/codex/AGENTS.md")] = current["AGENTS.md"].removesuffix("\n")

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(history))

    assert len(lines) == 1, lines
    assert "AGENTS.md" in lines[0]
    assert "+1/-1줄" in lines[0] and "첫 차이 3줄" in lines[0]


def test_edited_managed_delta_repeats_until_explicit_accept(pm_import, tmp_path):
    """report-drift·edited managed 기준은 수용 전엔 두 check 모두 같이 보인다."""
    dest, source, current = _case(tmp_path)
    config_rel = ".codex/config.toml"
    (dest / config_rel).write_text("sandbox = 'danger-full-access'\n", encoding="utf-8")
    history = _unchanged_history(current)
    history[("config-base", "templates/codex/.codex/config.toml")] = (
        "sandbox = 'read-only'\n")
    ledger = dest / ".project_manager" / "adapter_baseline.json"
    ledger_before = ledger.read_bytes()

    judgments = {item.relpath: item for item in pm_import.judge_adapter_configs(dest, source)}
    assert judgments[config_rel].status == "edited"
    first = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(history))
    second = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(history))

    assert first == second
    assert len(first) == 1 and config_rel in first[0]
    assert ledger.read_bytes() == ledger_before, "--check 관측이 원장을 전진시켰다"


def test_converged_managed_delta_does_not_prescribe_accept(pm_import, tmp_path):
    """현행 template과 원장까지 수렴한 managed는 과거 세대 차이가 있어도 재수용 처방이 없다."""
    dest, source, current = _case(tmp_path)
    managed_rel = ".codex/hooks.json"
    history = _unchanged_history(current)
    history[("config-base", "templates/codex/.codex/hooks.json")] = (
        '{"hooks": {"old": true}}\n')

    judgments = {item.relpath: item for item in pm_import.judge_adapter_configs(dest, source)}
    assert pm_import.adapter_config_convergence_status(judgments[managed_rel]) == "converged"
    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(history))

    assert not any(managed_rel in line for line in lines), lines
    assert f"--accept {managed_rel}" not in "\n".join(lines)


def _lite_entry_case(tmp_path: Path):
    dest, source, current = _case(tmp_path)
    lite = "# Current lite entry\n\nlite stable\n"
    (source / "templates" / "codex" / "AGENTS.lite.md").write_text(
        lite, encoding="utf-8")
    receipt_path = dest / ".project_manager" / "install.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["instance_owned_templates"]["AGENTS.md"] = {
        "weight": "lite",
        "source": "templates/codex/AGENTS.lite.md",
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    history = _unchanged_history(current)
    history.pop(("entry-base", "templates/codex/AGENTS.md"))
    history[("entry-base", "templates/codex/AGENTS.lite.md")] = lite
    return dest, source, current, history


def test_lite_entry_uses_recorded_lite_coordinate_and_detects_change(
        pm_import, tmp_path):
    """lite 설치본은 rename된 dst가 아니라 실제 `AGENTS.lite.md` 두 세대를 비교한다."""
    dest, source, _current, history = _lite_entry_case(tmp_path)
    history[("entry-base", "templates/codex/AGENTS.lite.md")] = (
        "# Old lite entry\n\nlite stable\n")

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(history))

    assert len(lines) == 1, lines
    assert "AGENTS.md" in lines[0] and "+1/-1줄" in lines[0]


def test_lite_entry_ignores_full_only_template_change(pm_import, tmp_path):
    """full `AGENTS.md`만 달라져도 lite 설치본에는 변경 요약을 만들지 않는다."""
    dest, source, _current, history = _lite_entry_case(tmp_path)
    (source / "templates" / "codex" / "AGENTS.md").write_text(
        "# Full-only new generation\n", encoding="utf-8")
    runner = FakeGenerationGit(history)

    assert pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=runner) == []
    assert not any(
        call[-1].endswith(":templates/codex/AGENTS.md")
        for call in runner.calls if "show" in call), runner.calls


def test_lite_import_receipt_records_actual_renamed_entry_source(pm_import, tmp_path):
    """설치 경로가 `CLAUDE.lite.md → CLAUDE.md`의 weight/source 좌표를 영수증에 남긴다."""
    dest = tmp_path / "lite-installed"

    assert pm_import.main([
        "--new", str(dest), "--harness", "claude", "--weight", "lite",
        "--from", str(REPO), "--name", "Lite Coordinate",
    ]) == 0

    receipt = json.loads(
        (dest / ".project_manager" / "install.json").read_text(encoding="utf-8"))
    assert receipt["instance_owned_templates"]["CLAUDE.md"] == {
        "weight": "lite",
        "source": "templates/claude_code/CLAUDE.lite.md",
    }


def test_legacy_receipt_without_template_coordinate_is_one_manual_line(
        pm_import, tmp_path):
    """구 설치본은 full 좌표를 추측해 오탐하지 않고 좌표 미기록 한 줄로 강등한다."""
    dest, source, current = _case(tmp_path, with_coordinates=False)

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(_unchanged_history(current)))

    assert lines == [
        "⚠️  인스턴스 소유 템플릿 좌표 미기록·수동 확인 — "
        "자동 비교 생략(변경 없음 아님)."
    ]


def test_missing_baseline_is_one_fail_soft_line_not_false_unchanged(pm_import, tmp_path):
    """원장 rev와 직전 upstream_rev가 모두 없으면 전량 확인 경고 정확히 한 줄."""
    dest, source, _current = _case(tmp_path, with_baseline=False)

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit({}))

    assert lines == [
        "⚠️  인스턴스 소유 템플릿 기준 미기록 — "
        "판정 불가(변경 없음 아님)·전량 확인 권장."
    ]


def test_non_git_source_is_generation_out_of_scope_not_permanent_unreachable_warning(
        pm_import, tmp_path):
    """path upstream이 비-git이면 매번 실패 경고 대신 세대 판정 비대상 한 줄이다."""
    dest, source, _current = _case(tmp_path)

    class NonGitSource(FakeGenerationGit):
        def __call__(self, argv: list[str]):
            if "rev-parse" in argv:
                self.calls.append(argv)
                return 128, "fatal: not a git repository"
            return super().__call__(argv)

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=NonGitSource({}))

    assert lines == ["ℹ️  이 소스는 git 이 아니라 세대 판정 비대상."]
    assert "기준 해소 불가" not in "\n".join(lines)


def test_historical_file_absence_is_reported_as_full_add(pm_import, tmp_path):
    """commit은 정상이고 과거 tree에 파일만 없으면 신규 template 전량 추가로 계산한다."""
    dest, source, current = _case(tmp_path)
    history = _unchanged_history(current)
    history.pop(("entry-base", "templates/codex/AGENTS.md"))

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=FakeGenerationGit(history))

    assert len(lines) == 1, lines
    assert "AGENTS.md" in lines[0] and "+3/-0줄" in lines[0]


def test_source_root_subdirectory_uses_git_toplevel_prefix(pm_import, tmp_path):
    """상위 저장소의 하위 source도 과거 blob을 찾아 전량 추가로 오인하지 않는다."""
    dest, source, current = _case(tmp_path)
    repo = tmp_path / "upstream-repo"
    repo.mkdir()
    nested_source = repo / "framework"
    source.rename(nested_source)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "add", "framework/templates"], check=True)
    subprocess.run([
        "git", "-C", str(repo), "-c", "user.name=PM Test",
        "-c", "user.email=pm@example.invalid", "commit", "-qm", "baseline",
    ], check=True)
    baseline = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    (dest / ".project_manager" / "local.conf").write_text(
        f"upstream_rev={baseline}\n", encoding="utf-8")
    ledger_path = dest / ".project_manager" / "adapter_baseline.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    for entry in ledger["files"].values():
        entry["template_rev"] = baseline
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (nested_source / "templates" / "codex" / ".codex" / "config.toml").write_text(
        "sandbox = 'read-only'\n", encoding="utf-8")

    lines = pm_import.instance_owned_template_delta_lines(dest, nested_source)

    config_lines = [line for line in lines if ".codex/config.toml" in line]
    assert len(config_lines) == 1, lines
    assert "+1/-1줄" in config_lines[0]
    assert "+1/-0줄" not in config_lines[0]


def test_git_prefix_resolution_failure_is_basis_unavailable(pm_import, tmp_path):
    """show-prefix 실패는 저장소 루트 경로를 추측하지 않고 기준 해소 불가로 내린다."""
    dest, source, current = _case(tmp_path)

    class PrefixFailure(FakeGenerationGit):
        def __call__(self, argv: list[str]):
            if "--show-prefix" in argv:
                self.calls.append(argv)
                return 128, "fatal: prefix unavailable"
            return super().__call__(argv)

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=PrefixFailure(_unchanged_history(current)))

    assert lines == [
        "⚠️  인스턴스 소유 템플릿 기준 해소 불가 — "
        "판정 불가(변경 없음 아님)·전량 확인 권장."
    ]


@pytest.mark.parametrize("detail", (
    "fatal: loose object read error",
    "git command timed out after 60 seconds",
    "fatal: not a git repository",
))
def test_git_show_error_is_basis_unavailable_not_full_add(
        pm_import, tmp_path, detail):
    """show I/O·timeout·repo 실패는 tree에 있는 파일을 부재로 접어 전량 추가하지 않는다."""
    dest, source, current = _case(tmp_path)

    class GitIOError(FakeGenerationGit):
        def __call__(self, argv: list[str]):
            if "show" in argv and argv[-1].endswith(":templates/codex/AGENTS.md"):
                self.calls.append(argv)
                return 128, detail
            return super().__call__(argv)

    lines = pm_import.instance_owned_template_delta_lines(
        dest, source, git_runner=GitIOError(_unchanged_history(current)))

    assert lines == [
        "⚠️  인스턴스 소유 템플릿 기준 해소 불가 — "
        "판정 불가(변경 없음 아님)·전량 확인 권장."
    ]
    assert "인스턴스 소유 템플릿 변경" not in "\n".join(lines)


def test_sync_adapter_config_check_exposes_the_same_delta_lines(
        pm_import, pm_config, tmp_path, monkeypatch, capsys):
    """``sync-adapter-config --check``가 공통 세대 요약을 읽기 전용으로 그대로 출력한다."""
    dest, source, _current = _case(tmp_path)
    expected = [
        "⚠️  인스턴스 소유 템플릿 변경 — AGENTS.md · +2/-1줄 · 첫 차이 1줄 · "
        "처방(세대 변경; 기존 config 채널 보고는 내용 drift): "
        "현재 파일을 백업한 뒤 수동 병합 또는 ADOPT.md 절차로 재-import"
    ]
    monkeypatch.setattr(
        pm_import, "instance_owned_template_delta_lines",
        lambda _dest, _source: expected,
    )
    args = argparse.Namespace(
        list=False, check=True, accept=None, accept_all=False, source=str(source))

    rc = pm_config.cmd_sync_adapter_config(
        args, pm_import=pm_import, dest_root=dest)

    captured = capsys.readouterr()
    assert rc == 0, captured
    assert expected[0] in captured.out
    assert captured.out.rstrip().endswith(
        "보존·미수용 managed 파일은 --accept 전까지 매 검사 반복; "
        "지난 세대는 백업/git 로 확인")
    assert not (dest / ".pm_import_backups").exists(), "--check가 write를 수행했다"


def test_pm_update_exit_exposes_final_delta_lines(
        pm_update, tmp_path, monkeypatch, capsys):
    """pm-update의 변경 0 종료 경로도 스냅샷 요약을 잃지 않는다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    relpath = ".project_manager/tools/sentinel.py"
    for root in (source, dest):
        path = root / relpath
        path.parent.mkdir(parents=True)
        path.write_text("# same engine byte\n", encoding="utf-8")
        (root / ".project_manager" / "engine.manifest").write_text(
            relpath + "\n", encoding="utf-8")
    expected = [
        "⚠️  인스턴스 소유 템플릿 변경 — AGENTS.md · +2/-1줄 · 첫 차이 1줄 · "
        "처방(세대 변경; 기존 config 채널 보고는 내용 drift): "
        "현재 파일을 백업한 뒤 수동 병합 또는 ADOPT.md 절차로 재-import"
    ]
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(
        pm_update, "_instance_owned_template_delta_lines",
        lambda _dest, _source: expected,
    )

    rc = pm_update.main(["--from", str(source), "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0, captured
    assert expected[0] in captured.out
    assert "최신 — 변경 없음" in captured.out


def test_pm_update_managed_auto_update_does_not_prescribe_accept(
        pm_update, tmp_path, monkeypatch, capsys):
    """managed 자동 갱신 뒤 최종 요약은 같은 파일에 --accept를 다시 처방하지 않는다."""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    relpath = ".project_manager/tools/sentinel.py"
    for root in (source, dest):
        path = root / relpath
        path.parent.mkdir(parents=True)
        path.write_text("# same engine byte\n", encoding="utf-8")
        (root / ".project_manager" / "engine.manifest").write_text(
            relpath + "\n", encoding="utf-8")
    (dest / ".codex").mkdir()
    (dest / ".codex" / "hooks.json").write_text("{}\n", encoding="utf-8")
    synced = {"done": False}

    def sync_configs(_dest, _source, *, write):
        assert write is True
        synced["done"] = True
        return {
            "status": "ok", "managed_converged": True,
            "updated": [{"relpath": ".codex/hooks.json", "backup_rel": "backup",
                         "note": None}],
            "preserved": [], "degraded": [], "blocking": [], "drift": [],
        }

    def final_delta(_dest, _source):
        assert synced["done"], "세대 요약이 managed 최종 판정보다 먼저 계산됐다"
        return []

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(pm_update, "sync_adapter_configs", sync_configs)
    monkeypatch.setattr(pm_update, "_instance_owned_template_delta_lines", final_delta)
    monkeypatch.setattr(
        pm_update, "migrate_entry_doc", lambda *_args, **_kwargs: {"status": "current"})
    monkeypatch.setattr(pm_update, "_print_entry_doc_migration_finding", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pm_update, "reinstall_protected_hooks", lambda *_args, **_kwargs: {"status": "ok"})
    monkeypatch.setattr(pm_update, "_print_protected_hook_reinstall_finding", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pm_update, "check_adapter_hook_sets",
        lambda *_args, **_kwargs: {"status": "ok", "findings": []})
    monkeypatch.setattr(pm_update, "converge_upstream_revs", lambda *_args: False)

    rc = pm_update.main(["--from", str(source)])

    captured = capsys.readouterr()
    assert rc == 0, captured
    assert "어댑터 config 갱신 — .codex/hooks.json" in captured.out
    assert "--accept .codex/hooks.json" not in captured.out + captured.err
