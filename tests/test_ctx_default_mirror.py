"""ctx 임계 기본값·미측정 sentinel 정합 가드 (T-0207·T-0550·T-0770·T-0906·T-0923·T-0943).

쓴 % 임계(중간 기록 권고·압축 임박 경고)와 사용 한도의 기본값은 엔진 레지스트리 한 곳뿐이다. Python
어댑터의 밴드·마진 판정은 엔진 공개 진입(`cli.hooks`)이 소유하므로 어댑터 사이 사본 대조는 없다 —
이 파일은 레지스트리 sanity 와 세 하네스의 첫 응답 전 미측정 규칙만 본다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from pm_engine.domain import conf_schema

REPO = Path(__file__).resolve().parents[1]
# 엔진 사이트는 리터럴이 아니라 키 레지스트리다(T-0906 · `conf_schema`) — board 의 ctx 상수는 거기로 옮겨졌다.
CLAUDE_GUARD = REPO / "templates" / "claude_code" / ".claude" / "ctx_guard.py"
# codex 는 옆에 ctx_guard 모듈이 없어 relay driver 파일이 rollout 점유를 관측한다(T-0770).
CODEX_GUARD = REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py"


def _engine(key: str) -> int:
    """엔진 사이트 = 레지스트리가 선언한 이 키의 기본값 (어댑터 리터럴의 대조 기준)."""
    declared = conf_schema.default_for(key)
    assert declared is not None, f"레지스트리에 `{key}` 기본값 선언이 없다"
    return int(declared)


def test_ctx_default_sanity_checkpoint_below_precompact_below_compact():
    """레지스트리 기본값 자체가 적용 검사(0 < checkpoint_at < precompact_at < compact_at < 100)를 통과한다."""
    checkpoint = _engine("ctx.checkpoint_at_pct")
    precompact = _engine("ctx.precompact_at_pct")
    compact = _engine("ctx.compact_at_pct")
    assert 0 < checkpoint < precompact < compact < 100, (checkpoint, precompact, compact)


# ── 첫 모델 응답 전 침묵 (T-0835) ────────────────────────────────────────────
# 세 하네스 모두 첫 모델 응답 전에는 점유를 모른다는 구조가 이미 공통 규칙(미측정=침묵)임을
# 대조한다 — codex/claude 는 같은 sentinel(0)을 돌려주고, opencode 는 assistant 메시지+tokens
# 관측 전에는 엔진 판정 요청에 ctx 를 싣지 않는다(context=null).

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_claude_and_codex_return_the_same_unmeasured_sentinel_before_first_response(tmp_path):
    """claude `context_tokens_from_transcript`·codex `rollout_context_tokens` 는 첫 응답 전
    (usage/token_count 0건) 같은 sentinel(0)로 수렴한다 — 어느 쪽도 실측치로 오인하지 않는다."""
    claude = _load_module("claude_ctx_guard_t0835_mirror", CLAUDE_GUARD)
    codex = _load_module("codex_ctx_guard_t0835_mirror", CODEX_GUARD)

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n', encoding="utf-8")
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        '{"type":"event_msg","payload":{"type":"agent_message","message":"hi"}}\n',
        encoding="utf-8")

    assert claude.context_tokens_from_transcript(str(transcript)) == (0, None, None)
    assert codex.rollout_context_tokens(str(rollout)) == 0


def test_opencode_gates_ctx_evaluation_on_an_assistant_message_with_tokens(tmp_path):
    """opencode 는 `message.updated` + `role==assistant` + `tokens` 관측 전에는 일반 도구 판정 요청에
    ctx 를 싣지 않는다(context=null) — 첫 모델 응답 전에는 점유를 모르므로 ctx 안내·marker 가 없다
    (codex/claude 의 0-sentinel 과 같은 결론). 관측 뒤에는 같은 요청에 그 토큰이 실린다."""
    import json
    import shutil

    from test_opencode_ctx_guard import _make_ctx_guard_project, _run_node_check

    if shutil.which("node") is None:
        import pytest

        pytest.skip("node 없음")
    root = _make_ctx_guard_project(tmp_path)
    script = r"""
const childProcess = require("node:child_process");
const realSpawn = childProcess.spawnSync;
const contexts = [];
childProcess.spawnSync = (command, args, options) => {
  if (args.includes("hook-tool")) contexts.push(JSON.parse(options.input).context);
  return realSpawn(command, args, options);
};
const m = require("./ctx-guard-core.cjs");
(async () => {
  const sessionID = "ses_first_response";
  const hooks = await m.CtxGuardPlugin({client:{session:{get: async ({path:{id}}) => ({data:{id}})}},
    directory:process.argv[1]});
  const read = () => hooks["tool.execute.before"]({tool:"read", sessionID}, {args:{filePath:"README.md"}});
  const delivered = async () => {
    const output = {system:[]};
    await hooks["experimental.chat.system.transform"]({sessionID}, output);
    return output.system;
  };
  await hooks.event({event:{type:"message.updated", properties:{info:{sessionID, role:"user", tokens:{input:150000}}}}});
  await hooks.event({event:{type:"message.updated", properties:{info:{sessionID, role:"assistant"}}}});
  await read();
  const before = await delivered();
  await hooks.event({event:{type:"message.updated", properties:{info:{sessionID, role:"assistant",
    tokens:{input:150000}}}}});
  await read();
  const after = await delivered();
  console.log(JSON.stringify({contexts, before, after}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    out = _run_node_check(script, root, [str(root)])
    result = json.loads(out)
    assert result["contexts"][0] is None, "첫 assistant 관측 전에 ctx 를 실었다"
    assert result["before"] == [], "첫 응답 전에 ctx 안내가 나갔다"
    assert result["contexts"][1]["tokens"] == 150_000 and result["contexts"][1]["boundary"] is None
    assert len(result["after"]) == 1 and result["after"][0].startswith("[ctx-nudge] 컨텍스트 사용 75%")
