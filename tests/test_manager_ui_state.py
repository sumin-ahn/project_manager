"""Run the real T-0942 UI contracts through the Python acceptance entry point."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "manager" / "ui"


def _run_ui_contract(group: str, test_file: str, temporary_parent: Path) -> None:
    npm = shutil.which("npm")
    if npm is None or not (UI / "node_modules" / "vitest" / "vitest.mjs").is_file():
        pytest.skip("UI contract requires the optional local npm/Vitest installation")
    with tempfile.TemporaryDirectory(prefix="t0942-vitest-", dir=temporary_parent) as temporary:
        report = Path(temporary) / "results.json"
        command = [npm, "--prefix", "manager/ui", "test", "--", test_file,
                   "-t", group, "--reporter=json", f"--outputFile={report}"]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=120, check=False)
        diagnostics = completed.stdout + completed.stderr
        assert completed.returncode == 0, diagnostics
        assert report.is_file(), f"Missing Vitest JSON report: {diagnostics}"
        result = json.loads(report.read_text(encoding="utf-8"))
        assert result["success"] is True, diagnostics
        selected = [case for suite in result["testResults"] for case in suite["assertionResults"]
                    if group in case["fullName"]]
        assert selected, f"No real selected assertions for {group}: {diagnostics}"
        assert all(case["status"] == "passed" for case in selected), selected


def test_t0942_at1_automatic_recurrence(tmp_path):
    _run_ui_contract("T0942_AT1", "src/__tests__/App.test.tsx", tmp_path)


def test_t0942_at2_response_admission(tmp_path):
    _run_ui_contract("T0942_AT2", "src/__tests__/App.test.tsx", tmp_path)


def test_t0942_at3_product_state_matrix(tmp_path):
    _run_ui_contract("T0942_AT3", "src/__tests__/SettingsTab.test.tsx", tmp_path)


def test_t0942_at4_unchanged_wire(tmp_path):
    _run_ui_contract("T0942_AT4", "src/__tests__/api.test.tsx", tmp_path)
