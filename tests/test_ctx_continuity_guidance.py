"""T-0648 ctx 가드 연속성 문구 단일 진실·금지 표현 회귀 가드."""
from __future__ import annotations

from pm_engine.infra import pm_paths
import pm_engine.app.session_snapshot
import pm_engine.domain.ctx_recovery

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _textio import utf8_child_env


ROOT = Path(__file__).resolve().parents[1]
PM_LOG = ROOT / ".project_manager" / "tools" / "pm_log.py"
CLAUDE_TEMPLATE = ROOT / "templates" / "claude_code" / ".claude" / "ctx_stop_hook.py"
OPENCODE = ROOT / "templates" / "opencode" / ".opencode" / "lib" / "ctx-guard-core.cjs"
CODEX_DISPATCHER = ROOT / "templates" / "codex" / ".codex" / "pm_orch_codex.py"

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
    policy = pm_engine.domain.ctx_recovery.CTX_GUARD_CONTINUITY_GUIDANCE
    assert len(pm_engine.domain.ctx_recovery.CTX_GUARD_REQUIRED_EXPRESSIONS) == 3
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
    text = pm_engine.domain.ctx_recovery.build_ctx_guard_guidance(
        band, used_pct=82, precompact_at_pct=80,
    )
    assert pm_engine.domain.ctx_recovery.CTX_GUARD_CONTINUITY_GUIDANCE in text
    assert not any(pattern.search(text) for pattern in FORBIDDEN), text


@pytest.mark.parametrize("band", ("nudge", "nudge2", "final", "precompact"))
def test_required_expression_matrix_matches_direct_renderer_and_raw_cli(pm_log, band):
    """4밴드×필수 3종을 직접 렌더와 실제 raw CLI 출력 양쪽에서 고정한다."""
    direct = pm_engine.domain.ctx_recovery.build_ctx_guard_guidance(
        band, used_pct=82, precompact_at_pct=80,
    )
    result = subprocess.run(
        [
            sys.executable, str(PM_LOG), "ctx-guidance", "--band", band,
            "--used-pct", "82", "--precompact-at-pct", "80",
        ],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env=utf8_child_env(), check=True,
    )
    cli = result.stdout.rstrip("\n")

    assert cli == direct
    for required in pm_engine.domain.ctx_recovery.CTX_GUARD_REQUIRED_EXPRESSIONS:
        assert required in direct
        assert required in cli
    for forbidden in pm_engine.domain.ctx_recovery.CTX_GUARD_FORBIDDEN_EXPRESSIONS:
        assert forbidden not in direct
        assert forbidden not in cli


def test_shipping_harnesses_reference_central_command_without_policy_copy(pm_log):
    policy_marker = "핸드오프는 사용자 명시 지시로만 한다"
    # T-0806 이후 codex 축 압축 안내 커맨드는 `hooks.json` 직결이 아니라 디스패처
    #   `CODEX_HOOK_FEATURES` registry 값이다 — 판정 대상은 hooks.json 텍스트가 아니라 그 registry.
    dispatcher = _load(CODEX_DISPATCHER, "t0648_codex_dispatcher")
    compaction_guidance = [feature for feature in dispatcher.CODEX_HOOK_FEATURES
                            if feature.feature_id == "compaction-guidance"]
    assert len(compaction_guidance) == 1, dispatcher.CODEX_HOOK_FEATURES
    sources = {
        "claude-template": CLAUDE_TEMPLATE.read_text(encoding="utf-8"),
        "opencode": OPENCODE.read_text(encoding="utf-8"),
        "codex": " ".join(compaction_guidance[0].argv),
    }
    # Claude 훅은 엔진 공개 진입(`judge_context`)의 안내를 그대로 싣고 정책 문구 사본을 두지 않는다.
    assert "hooks.judge_context(" in sources["claude-template"]
    assert policy_marker not in sources["claude-template"]
    assert "ctx-guidance" in sources["opencode"]
    assert "ctx-guidance --band precompact --json" in sources["codex"]
    # 정상 경로의 단일 진실은 domain.ctx_recovery 상수다.
    assert Path(pm_engine.domain.ctx_recovery.__file__).read_text(encoding="utf-8").count(policy_marker) == 1


@pytest.mark.parametrize("band", ("nudge", "nudge2", "final", "precompact"))
def test_forbidden_winding_down_phrases_are_absent_from_rendered_outputs(pm_log, band):
    """검사 상수 선언을 오탐하지 않고 사용자가 받는 최종 출력만 판정한다."""
    output = pm_engine.domain.ctx_recovery.build_ctx_guard_guidance(
        band, used_pct=82, precompact_at_pct=80,
    )
    assert not any(pattern.search(output) for pattern in FORBIDDEN), output
    assert all(
        forbidden not in output
        for forbidden in pm_engine.domain.ctx_recovery.CTX_GUARD_FORBIDDEN_EXPRESSIONS
    )


def _snapshot_fixture_pm_home(tmp_path):
    """전언 경고 + 진행 중 작업 절을 모두 채우는 최소 PM 홈([[T-0787]])."""
    pm_home = tmp_path / "pm-home"
    local = pm_home / ".project_manager" / ".local"
    task_state = local / "tasks" / "main" / "pm_state.md"
    task_state.parent.mkdir(parents=True)
    task_state.write_text("# main state\n- 남은 작업: FORBIDDEN 회귀\n", encoding="utf-8")
    # 압축 경로 정체성은 task 기록의 세션 ID 로 찾는다(T-0953).
    (local / "worktree-leases.json").write_text(
        json.dumps({"leases": [], "tasks": [{"name": "main", "session": "s-main"}]}),
        encoding="utf-8",
    )
    for status, count in (("open", 1), ("claimed", 1), ("blocked", 0), ("done", 1)):
        directory = pm_home / ".project_manager" / "board" / "tickets" / status
        directory.mkdir(parents=True)
        for index in range(count):
            (directory / f"T-{index:04d}-fixture.md").write_text("fixture\n", encoding="utf-8")
    ledger = local / "delegate-rounds.jsonl"
    row = {
        "ticket": "T-0001", "role": "architect", "ordinal": 1,
        "run_id": "a" * 32, "copy": str(tmp_path / "copy.md"),
        "board_rel": "wiki/tickets/rounds/T-0001/architect-1.md",
        "prepared_at": "2026-08-21T00:00:00.000000+00:00", "harvested_at": None,
    }
    ledger.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    raw_ledger = local / "raw_outputs.json"
    raw_ledger.write_text(json.dumps({"version": 1, "records": [{
        "id": "r1", "surface": "delegate", "harness": "codex", "model": "gpt",
        "role": "architect", "attempt": "1", "pid": 1,
        "started_at": "2026-08-21T00:00:00.000000+00:00",
        "raw_path": str(tmp_path / "raw.txt"),
    }]}), encoding="utf-8")
    return pm_home


def test_forbidden_phrases_absent_from_compaction_snapshot(pm_log, tmp_path):
    """T-0787 — 전언 경고·진행 중 작업 절을 포함한 snapshot 전문에 FORBIDDEN 정규식 3종 0 hit."""
    pm_home = _snapshot_fixture_pm_home(tmp_path)
    text, warning = pm_engine.app.session_snapshot.build_snapshot(pm_paths.home_at(pm_home), "s-main")
    assert warning is None and text is not None
    assert "## 전언 경고" in text and "## 진행 중 작업" in text
    assert not any(pattern.search(text) for pattern in FORBIDDEN), text
    assert all(
        forbidden not in text for forbidden in pm_engine.domain.ctx_recovery.CTX_GUARD_FORBIDDEN_EXPRESSIONS
    ), text


def test_claude_hook_consumes_pm_log_guidance_verbatim(pm_log, tmp_path):
    """Claude 훅 → 엔진 공개 진입 → domain 렌더 — 최종 밴드 안내가 공유 렌더 원문 그대로 실린다."""
    from pm_engine.cli import hooks as cli_hooks
    from pm_engine.infra import conf_store
    from test_harness_ctx_limits import install_ctx_record

    hook = _load(CLAUDE_TEMPLATE, "t0648_ctx_stop_hook")
    install_ctx_record(tmp_path)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {
        "role": "assistant", "usage": {"input_tokens": 190_000}}}) + "\n", encoding="utf-8")
    home = cli_hooks.adapter_home(tmp_path)
    budget = conf_store.read_ctx_budget(tmp_path, conf_store.local_config(home), "claude")
    expected = pm_engine.domain.ctx_recovery.build_ctx_guard_guidance(
        "final", used_pct=95, precompact_at_pct=budget["precompact_at_pct"],
    )
    rc, output = hook.evaluate({"hook_event_name": "UserPromptSubmit", "session_id": "t0648",
                                "transcript_path": str(transcript)}, home)
    assert rc == 0
    assert output["hookSpecificOutput"]["additionalContext"] == expected


def test_t0934_guidance_execution_real_runtime_and_raw_utf8(tmp_path):
    from _manager_home import make_pm_home, set_conf_key
    from test_opencode_ctx_guard import _install_core

    node = shutil.which("node")
    assert node is not None, "Node가 있어야 실제 guidance 소비를 검증할 수 있다"
    root = make_pm_home(tmp_path / "engine")
    set_conf_key(root, "runtime.py", sys.executable)
    lib = _install_core(root)
    floor = root / ".project_manager" / "tools" / "python_floor.py"
    assert floor.read_bytes() == PM_LOG.with_name("python_floor.py").read_bytes()
    command = [sys.executable, str(root / ".project_manager" / "tools" / "pm_log.py"),
               "ctx-guidance", "--band", "nudge", "--used-pct", "82", "--precompact-at-pct", "80"]
    producer = subprocess.run(command, cwd=root, capture_output=True, env=utf8_child_env(), check=True)
    script = r'''
const m = require("./ctx-guard-core.cjs");
process.stdout.write(m.buildEngineCtxGuidance(process.argv[1], "nudge", {usedPct:82}, {precompact_at_pct:80}));
'''
    consumed = subprocess.run([node, "-e", script, str(root)], cwd=lib,
                              capture_output=True, check=True, timeout=30, env=utf8_child_env())
    assert consumed.stdout.decode("utf-8") == producer.stdout.decode("utf-8").strip()
    assert "압축은 자동" in consumed.stdout.decode("utf-8")

    # 기존 fake engine 채널에 의도한 CRLF 원문을 만들고 실제 Python/Node 경로로 소비한다.
    payload = "원문 안내\r\n둘째 줄\r\n"
    engine = root / ".project_manager" / "tools" / "pm_log.py"
    engine.write_text("import sys\nsys.stdout.buffer.write(" + repr(payload.encode("utf-8")) + ")\n",
                      encoding="utf-8", newline="\n")
    producer = subprocess.run([sys.executable, str(engine), "snapshot"], cwd=root,
                              capture_output=True, check=True)
    assert producer.stdout == payload.encode("utf-8")
    script = r'''
const m = require("./ctx-guard-core.cjs");
const root = process.argv[1];
process.stdout.write(JSON.stringify({
  guidance:m.buildEngineCtxGuidance(root,"nudge",{usedPct:82},{precompact_at_pct:80}),
  snapshot:m.buildCompactionSnapshot(root,"ses_t0934"),
}));
'''
    result = subprocess.run([node, "-e", script, str(root)], cwd=lib,
                            capture_output=True, check=True, timeout=30)
    parsed = json.loads(result.stdout)
    assert parsed == {"guidance": payload.strip(), "snapshot": {"ok": True, "text": payload}}
    assert parsed["snapshot"]["text"].encode("utf-8") == producer.stdout
    set_conf_key(root, "runtime.py", str(root / "absent-python"))
    failed = subprocess.run([node, "-e", script, str(root)], cwd=lib,
                            capture_output=True, check=True, timeout=30)
    failed_snapshot = json.loads(failed.stdout)["snapshot"]
    assert failed_snapshot["ok"] is False
    assert failed_snapshot["text"].startswith("[ctx-checkpoint] 압축 기록 실패: ")
    assert json.loads(failed.stdout)["guidance"] != payload.strip()


def test_opencode_consumes_pm_log_guidance_verbatim(pm_log):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    script = (
        "const m=require('./ctx-guard-core.cjs');"
        "process.stdout.write(m.buildEngineCtxGuidance("
        f"{json.dumps(str(ROOT))},'nudge',{{usedPct:82}},"
        "{checkpoint_at_pct:70,precompact_at_pct:80}));"
    )
    result = subprocess.run(
        [node, "-e", script], cwd=OPENCODE.parent,
        capture_output=True, check=True,
    )
    producer = subprocess.run(
        [sys.executable, str(PM_LOG), "ctx-guidance", "--band", "nudge", "--used-pct", "82", "--precompact-at-pct", "80"],
        cwd=ROOT, capture_output=True, env=utf8_child_env(), check=True,
    )
    assert result.stdout.decode("utf-8") == producer.stdout.decode("utf-8").strip()


def test_codex_json_payload_is_engine_owned_shared_value(pm_log):
    result = subprocess.run(
        [sys.executable, str(PM_LOG), "ctx-guidance", "--band", "precompact", "--json"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env=utf8_child_env(), check=True,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "systemMessage": pm_engine.domain.ctx_recovery.build_ctx_guard_guidance("precompact"),
        "suppressOutput": False,
    }


def test_ctx_guidance_takes_used_and_precompact_at(pm_log):
    """T-0923 AT-019 — 안내는 쓴 % 와 압축 임박 경고 비율만 받는다(남은 % 인자·표기 없음)."""
    result = subprocess.run(
        [sys.executable, str(PM_LOG), "ctx-guidance", "--band", "final",
         "--used-pct", "82", "--precompact-at-pct", "80"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env=utf8_child_env(),
    )
    assert result.returncode == 0, result.stderr
    assert "컨텍스트 사용 82% (압축 임박 경고 80% 이상)" in result.stdout
    assert "잔여" not in result.stdout
    assert result.stdout.rstrip("\n") == pm_engine.domain.ctx_recovery.build_ctx_guard_guidance(
        "final", used_pct=82, precompact_at_pct=80)
    # nudge 밴드는 비율을 받아도 싣지 않는다(최종 밴드만의 표기).
    assert "압축 임박 경고" not in pm_engine.domain.ctx_recovery.build_ctx_guard_guidance(
        "nudge", used_pct=72, precompact_at_pct=80)

    for retired in (["--remaining-pct", "18"], ["--stop-pct", "20"]):
        refused = subprocess.run(
            [sys.executable, str(PM_LOG), "ctx-guidance", "--band", "final",
             "--used-pct", "82", *retired],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            env=utf8_child_env(),
        )
        assert refused.returncode == 2, (retired, refused.stdout)
        assert retired[0] in refused.stderr
