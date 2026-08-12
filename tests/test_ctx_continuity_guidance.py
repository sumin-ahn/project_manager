"""T-0648 ctx 가드 연속성 문구 단일 진실·금지 표현 회귀 가드."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PM_LOG = ROOT / ".project_manager" / "tools" / "pm_log.py"
CLAUDE_ROOT = ROOT / ".claude" / "ctx_stop_hook.py"
CLAUDE_TEMPLATE = ROOT / "templates" / "claude_code" / ".claude" / "ctx_stop_hook.py"
OPENCODE = ROOT / "templates" / "opencode" / ".opencode" / "lib" / "ctx-guard-core.cjs"
CODEX = ROOT / "templates" / "codex" / ".codex" / "hooks.json"

FORBIDDEN = (
    re.compile(r"마무리"),
    re.compile(r"새\s*(?:큰\s*)?작업.{0,20}(?:시작하지|시작\s*말)"),
    re.compile(r"현재\s*서사\s*기록.{0,20}우선"),
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pm_log():
    return _load(PM_LOG, "t0648_pm_log")


def test_policy_constant_states_auto_continuation_and_user_only_handoff(pm_log):
    policy = pm_log.CTX_GUARD_CONTINUITY_GUIDANCE
    assert len(pm_log.CTX_GUARD_REQUIRED_EXPRESSIONS) == 3
    assert "압축은 자동" in policy
    assert "세션은 그대로 이어진다" in policy
    assert "checkpoint는 압축 후 서사 복구용이지 종료 신호가 아니다" in policy
    assert "핸드오프는 사용자 명시 지시로만 한다" in policy
    assert "컨텍스트 잔량은 작업 범위·중단 결정의 입력이 아니" in policy
    assert "checkpoint 기록은 진행 중 작업과 병행" in policy
    assert "진행 중 작업은 계속한다" in policy
    assert "세션 종료·작업 축소는 사용자 지시로만 한다" in policy


@pytest.mark.parametrize("band", ("nudge", "nudge2", "final", "precompact"))
def test_every_ctx_band_includes_shared_policy_and_no_winding_down_phrase(pm_log, band):
    text = pm_log.build_ctx_guard_guidance(
        band, used_pct=82, remaining_pct=18, stop_pct=20,
    )
    assert pm_log.CTX_GUARD_CONTINUITY_GUIDANCE in text
    assert not any(pattern.search(text) for pattern in FORBIDDEN), text


@pytest.mark.parametrize("band", ("nudge", "nudge2", "final", "precompact"))
def test_required_expression_matrix_matches_direct_renderer_and_raw_cli(pm_log, band):
    """4밴드×필수 3종을 직접 렌더와 실제 raw CLI 출력 양쪽에서 고정한다."""
    direct = pm_log.build_ctx_guard_guidance(
        band, used_pct=82, remaining_pct=18, stop_pct=20,
    )
    result = subprocess.run(
        [
            sys.executable, str(PM_LOG), "ctx-guidance", "--band", band,
            "--used-pct", "82", "--remaining-pct", "18", "--stop-pct", "20",
        ],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    cli = result.stdout.rstrip("\n")

    assert cli == direct
    for required in pm_log.CTX_GUARD_REQUIRED_EXPRESSIONS:
        assert required in direct
        assert required in cli
    for forbidden in pm_log.CTX_GUARD_FORBIDDEN_EXPRESSIONS:
        assert forbidden not in direct
        assert forbidden not in cli


def test_shipping_harnesses_reference_central_command_without_policy_copy(pm_log):
    policy_marker = "핸드오프는 사용자 명시 지시로만 한다"
    sources = {
        "claude": CLAUDE_ROOT.read_text(encoding="utf-8"),
        "claude-template": CLAUDE_TEMPLATE.read_text(encoding="utf-8"),
        "opencode": OPENCODE.read_text(encoding="utf-8"),
        "codex": CODEX.read_text(encoding="utf-8"),
    }
    assert "ctx-guidance" in sources["claude"]
    assert "ctx-guidance" in sources["claude-template"]
    assert "ctx-guidance" in sources["opencode"]
    assert "ctx-guidance --band precompact --json" in sources["codex"]
    # 어댑터의 fail-soft 최소 복제본은 허용하되 정상 경로의 단일 진실은 pm_log 상수다.
    assert PM_LOG.read_text(encoding="utf-8").count(policy_marker) == 1


@pytest.mark.parametrize("band", ("nudge", "nudge2", "final", "precompact"))
def test_forbidden_winding_down_phrases_are_absent_from_rendered_outputs(pm_log, band):
    """검사 상수 선언을 오탐하지 않고 사용자가 받는 최종 출력만 판정한다."""
    output = pm_log.build_ctx_guard_guidance(
        band, used_pct=82, remaining_pct=18, stop_pct=20,
    )
    assert not any(pattern.search(output) for pattern in FORBIDDEN), output
    assert all(
        forbidden not in output
        for forbidden in pm_log.CTX_GUARD_FORBIDDEN_EXPRESSIONS
    )


def test_claude_hook_consumes_pm_log_guidance_verbatim(pm_log):
    hook = _load(CLAUDE_TEMPLATE, "t0648_ctx_stop_hook")
    expected = pm_log.build_ctx_guard_guidance(
        "final", used_pct=82, remaining_pct=18, stop_pct=20,
    )
    actual = hook._build_ctx_guidance(
        ROOT,
        band="final",
        used_pct=82,
        thresholds={"nudge_pct": 30, "stop_pct": 20},
    )
    assert actual == expected


def test_opencode_consumes_pm_log_guidance_verbatim(pm_log):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    script = (
        "const m=require('./ctx-guard-core.cjs');"
        "process.stdout.write(m.buildEngineCtxGuidance("
        f"{json.dumps(str(ROOT))},'nudge',{{usedPct:82,remainingPct:18}},"
        "{nudge_pct:30,stop_pct:20}));"
    )
    result = subprocess.run(
        [node, "-e", script], cwd=OPENCODE.parent,
        capture_output=True, text=True, check=True,
    )
    expected = pm_log.build_ctx_guard_guidance(
        "nudge", used_pct=82, remaining_pct=18, stop_pct=20,
    )
    assert result.stdout == expected


def test_codex_json_payload_is_engine_owned_shared_value(pm_log):
    result = subprocess.run(
        [sys.executable, str(PM_LOG), "ctx-guidance", "--band", "precompact", "--json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "systemMessage": pm_log.build_ctx_guard_guidance("precompact"),
        "suppressOutput": False,
    }


def test_claude_root_and_template_remain_byte_identical():
    assert CLAUDE_ROOT.read_bytes() == CLAUDE_TEMPLATE.read_bytes()
