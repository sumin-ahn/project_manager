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


class _TaskPool:
    """task membership·보유 슬롯만 답하는 최소 seam — 승인 게이트 앞의 장부 조회를 통과시킨다."""

    def find_task(self, name):
        return type("_T", (), {"name": name})()

    def find_task_strict(self, name):
        """실 엔진과 같은 짝 — 결과는 같고 장부 손상만 올린다(이 대역은 손상 없음)."""
        return self.find_task(name)

    def slots_for_task(self, name):
        return []

    def slots_for_task_strict(self, name):
        """실 엔진과 같은 짝 — 결과는 같고 장부 손상만 올린다(이 대역은 손상 없음)."""
        return self.slots_for_task(name)


def _capture_run(monkeypatch, module):
    calls = []

    def fake_run(self, task, **kwargs):
        calls.append({"task": task, **kwargs})
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




def test_direct_run_missing_ack_is_rejected_before_pipeline(monkeypatch, tmp_path, capsys):
    handoff = _load_handoff()
    runner = handoff.PmHandoff(
        pm_state_file=tmp_path / "pm_state.md", worktree_pool=_TaskPool(),
    )
    monkeypatch.setattr(
        runner,
        "_dirty_tree_gate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ack 뒤 pipeline 진입 금지")),
    )

    rc = runner.run("alpha", dry_run=False, skip_pytest=True)

    assert rc == 1
    err = capsys.readouterr().err
    assert "1순위: 사용자에게 핸드오프 여부를 확인하라" in err
    assert "부차 수단: 승인한 사용자만 `--user-ack alpha`" in err
    assert "세션 자동 부착 금지" in err


def test_direct_run_mismatched_ack_is_value_bound(monkeypatch, tmp_path, capsys):
    handoff = _load_handoff()
    runner = handoff.PmHandoff(
        pm_state_file=tmp_path / "pm_state.md", worktree_pool=_TaskPool(),
    )
    monkeypatch.setattr(
        runner,
        "_dirty_tree_gate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("mismatch 뒤 pipeline 진입 금지")),
    )

    rc = runner.run("alpha", dry_run=False, skip_pytest=True, user_ack="other")

    assert rc == 1
    assert "제공된 값 'other'은 대상값 'alpha'에 결속되지 않았다" in capsys.readouterr().err








def test_compaction_checkpoint_parser_has_no_handoff_ack_gate():
    spec = importlib.util.spec_from_file_location("t0648_pm_log_checkpoint", PM_LOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.build_parser().parse_args([
        "checkpoint", "--task", "main", "--trigger", "compaction",
    ])

    assert args.cmd == "checkpoint"
    assert not hasattr(args, "user_ack")
