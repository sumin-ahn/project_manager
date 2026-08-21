"""T-0648 pm_handoff 사용자-명시 값-결속 ack 게이트."""
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / ".project_manager" / "tools" / "pm_handoff.py"
PM_LOG = ROOT / ".project_manager" / "tools" / "pm_log.py"


def _load_handoff():
    spec = importlib.util.spec_from_file_location("t0648_pm_handoff", HANDOFF)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture_run(monkeypatch, module):
    calls = []

    def fake_run(self, **kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(module.PmHandoff, "run", fake_run)
    return calls


def test_cli_passes_missing_user_ack_to_engine_without_validating(monkeypatch):
    handoff = _load_handoff()
    calls = _capture_run(monkeypatch, handoff)

    rc = handoff.main(["--task", "main"])

    assert rc == 0
    assert calls[0]["task"] == "main"
    assert calls[0]["user_ack"] is None


def test_cli_passes_mismatched_user_ack_to_engine_without_validating(monkeypatch):
    handoff = _load_handoff()
    calls = _capture_run(monkeypatch, handoff)

    rc = handoff.main(["--task", "main", "--user-ack", "other"])

    assert rc == 0
    assert calls[0]["task"] == "main"
    assert calls[0]["user_ack"] == "other"


def test_matching_task_ack_reaches_existing_handoff_pipeline(monkeypatch, capsys):
    handoff = _load_handoff()
    calls = _capture_run(monkeypatch, handoff)

    rc = handoff.main(["--task", "main", "--user-ack", "main"])

    assert rc == 0
    assert len(calls) == 1 and calls[0]["task"] == "main"
    assert calls[0]["user_ack"] == "main"
    assert "[승인 감사]" not in capsys.readouterr().out


def test_slot_ack_binds_to_canonical_repo_slot(monkeypatch, capsys, tmp_path):
    handoff = _load_handoff()
    calls = _capture_run(monkeypatch, handoff)
    monkeypatch.setattr(handoff, "REPO", tmp_path)
    argv = [
        "--repo", "alpha", "--slot", "2", "--session-seq", "7",
        "--wave-summary", "x", "--no-pytest",
    ]

    assert handoff.main([*argv, "--user-ack", "alpha_1"]) == 0
    assert handoff.main([*argv, "--user-ack", "alpha_2"]) == 0
    assert [call["worktree_slot"] for call in calls] == ["work/alpha_2", "work/alpha_2"]
    assert [call["user_ack"] for call in calls] == ["alpha_1", "alpha_2"]
    assert "대상값 'alpha_2'" not in capsys.readouterr().out


def test_dry_run_without_ack_is_allowed_but_prints_execution_approval_notice(
    monkeypatch, capsys,
):
    handoff = _load_handoff()
    calls = _capture_run(monkeypatch, handoff)

    rc = handoff.main(["--task", "main", "--dry-run"])

    assert rc == 0
    assert len(calls) == 1 and calls[0]["dry_run"] is True
    assert calls[0]["user_ack"] is None
    assert "쓰기 0 미리보기는 사용자 ack 없이 허용" not in capsys.readouterr().out


def test_legacy_solo_cli_only_passes_ack_surface(monkeypatch):
    handoff = _load_handoff()
    calls = _capture_run(monkeypatch, handoff)
    argv = ["--session-seq", "3", "--wave-summary", "x", "--no-pytest"]

    resolver = lambda: ("solo", None, "legacy pm_state")
    assert handoff.main(argv, identity_resolver=resolver) == 0
    assert handoff.main(
        [*argv, "--user-ack", "solo"], identity_resolver=resolver,
    ) == 0
    assert [call["worktree_slot"] for call in calls] == [None, None]
    assert [call["task"] for call in calls] == [None, None]
    assert [call["collection_task"] for call in calls] == ["solo", "solo"]
    assert [call["user_ack"] for call in calls] == [None, "solo"]


def test_direct_run_missing_ack_is_rejected_before_pipeline(monkeypatch, tmp_path, capsys):
    handoff = _load_handoff()
    runner = handoff.PmHandoff(pm_state_file=tmp_path / "pm_state.md")
    monkeypatch.setattr(
        runner,
        "_dirty_tree_gate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ack 뒤 pipeline 진입 금지")),
    )

    rc = runner.run(
        session_num=3,
        wave_summary="x",
        dry_run=False,
        skip_pytest=True,
        worktree_slot="work/alpha_2",
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "1순위: 사용자에게 핸드오프 여부를 확인하라" in err
    assert "부차 수단: 승인한 사용자만 `--user-ack alpha_2`" in err
    assert "세션 자동 부착 금지" in err


def test_direct_run_mismatched_ack_is_value_bound(monkeypatch, tmp_path, capsys):
    handoff = _load_handoff()
    runner = handoff.PmHandoff(pm_state_file=tmp_path / "pm_state.md")
    monkeypatch.setattr(
        runner,
        "_dirty_tree_gate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("mismatch 뒤 pipeline 진입 금지")),
    )

    rc = runner.run(
        session_num=3,
        wave_summary="x",
        dry_run=False,
        skip_pytest=True,
        worktree_slot="work/alpha_2",
        user_ack="alpha_1",
    )

    assert rc == 1
    assert "제공된 값 'alpha_1'은 대상값 'alpha_2'에 결속되지 않았다" in capsys.readouterr().err


def test_bare_slot_is_resolved_once_and_same_value_binds_ack(
    monkeypatch, tmp_path, capsys,
):
    handoff = _load_handoff()
    resolutions = []

    def resolve_once(worktree_slot):
        resolutions.append(worktree_slot)
        return ("work/alpha_1", None) if len(resolutions) == 1 else ("work/alpha_2", None)

    monkeypatch.setattr(handoff, "_resolve_session_worktree_slot", resolve_once)
    runner = handoff.PmHandoff()
    monkeypatch.setattr(runner, "_dirty_tree_gate", lambda *a, **k: (1, None))

    rc = runner.run(
        session_num=3,
        wave_summary="x",
        dry_run=False,
        skip_pytest=True,
        user_ack="alpha_1",
    )

    assert rc == 1
    assert resolutions == [None]
    assert runner._worktree_slot == "work/alpha_1"
    assert "대상값 'alpha_1'" in capsys.readouterr().out


def test_bare_solo_none_resolution_is_frozen_for_ack_and_downstream(
    monkeypatch, tmp_path, capsys,
):
    """첫 해소 None도 실행 스냅샷이다 — 뒤 lease 변화로 slot을 재해소하지 않는다."""
    handoff = _load_handoff()
    resolutions = []

    def resolve_entry_once(worktree_slot):
        resolutions.append(("entry", worktree_slot))
        return None, None

    def resolve_state_after_lease_change(*args, **kwargs):
        resolutions.append(("state", args[0] if args else None))
        return "alpha_2"

    def resolve_cwd_after_lease_change(worktree_slot, *args, **kwargs):
        resolutions.append(("cwd", worktree_slot))
        return str(tmp_path / "work" / "alpha_2")

    monkeypatch.setattr(handoff, "REPO", tmp_path)
    monkeypatch.setattr(handoff, "_resolve_session_worktree_slot", resolve_entry_once)
    monkeypatch.setattr(handoff, "_resolve_state_slot", resolve_state_after_lease_change)
    monkeypatch.setattr(handoff, "_regression_cwd", resolve_cwd_after_lease_change)
    runner = handoff.PmHandoff()
    dirty_trees = []

    def stop_after_observing_dirty_trees(*args, **kwargs):
        dirty_trees.extend(runner._dirty_gate_trees(None))
        return 1, None

    monkeypatch.setattr(runner, "_dirty_tree_gate", stop_after_observing_dirty_trees)

    rc = runner.run(
        session_num=3,
        wave_summary="x",
        dry_run=False,
        skip_pytest=True,
        user_ack="solo",
    )

    assert rc == 1
    assert resolutions == [("entry", None)]
    assert dirty_trees == [str(tmp_path)]
    assert runner._worktree_slot is None
    assert runner._pm_state_file == tmp_path / ".project_manager" / "wiki" / "pm_state.md"
    assert "대상값 'solo'" in capsys.readouterr().out


def test_direct_run_rejects_malformed_worktree_slot_before_ack_or_pipeline(
    monkeypatch, tmp_path, capsys,
):
    handoff = _load_handoff()
    runner = handoff.PmHandoff(pm_state_file=tmp_path / "pm_state.md")
    pipeline_calls = []

    def dirty_gate(*args, **kwargs):
        pipeline_calls.append((args, kwargs))
        return 1, None

    monkeypatch.setattr(runner, "_dirty_tree_gate", dirty_gate)

    rc = runner.run(
        session_num=3,
        wave_summary="x",
        dry_run=False,
        skip_pytest=True,
        worktree_slot="work/not-a-canonical-slot",
        user_ack="solo",
    )

    assert rc == 1
    assert pipeline_calls == []
    captured = capsys.readouterr()
    assert "[승인 감사]" not in captured.out
    err = captured.err
    assert "worktree_slot" in err
    assert "canonical `work/<repo>_<N>`" in err


def test_compaction_checkpoint_parser_has_no_handoff_ack_gate():
    spec = importlib.util.spec_from_file_location("t0648_pm_log_checkpoint", PM_LOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.build_parser().parse_args([
        "checkpoint", "--task", "main", "--trigger", "compaction",
    ])

    assert args.cmd == "checkpoint"
    assert not hasattr(args, "user_ack")
