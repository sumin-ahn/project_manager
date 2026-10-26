"""ctx 임계 기본값·미측정 sentinel 정합 가드 (T-0207·T-0550·T-0770·T-0906·T-0923·T-0943).

쓴 % 임계(중간 기록 권고·압축 임박 경고)와 사용 한도의 기본값은 엔진 레지스트리 한 곳뿐이다. Python
어댑터의 밴드·마진 판정은 엔진 공개 진입(`cli.hooks`)이 소유하므로 어댑터 사이 사본 대조는 없다 —
이 파일은 레지스트리 sanity 와 세 하네스의 첫 응답 전 미측정 규칙만 본다.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from pm_engine.domain import conf_schema

REPO = Path(__file__).resolve().parents[1]
# 엔진 사이트는 리터럴이 아니라 키 레지스트리다(T-0906 · `conf_schema`) — board 의 ctx 상수는 거기로 옮겨졌다.
CLAUDE_GUARD = REPO / "templates" / "claude_code" / ".claude" / "ctx_guard.py"
# opencode ctx-guard 상수는 진입점 shim(plugins/ctx-guard.js)이 아니라 core 모듈에 산다 — 진입점은
# opencode 로드 규약(export=단일 함수·T-0283)을 위해 팩토리만 export 하는 얇은 shim 이고, 순수 헬퍼·
# 상수·팩토리 본체는 lib/ctx-guard-core.cjs(opencode 미스캔·node require 대상)로 분리됐다.
OPENCODE_GUARD = REPO / "templates" / "opencode" / ".opencode" / "lib" / "ctx-guard-core.cjs"
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
# 없이는 ctx 판정 진입 자체가 없다(가드 코드 신설 0 — 세 사이트 모두 처방 전부터 이미 이 규칙).

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

    assert claude.context_tokens_from_transcript(str(transcript)) == (0, None, 0)
    assert codex.rollout_context_tokens(str(rollout)) == 0


def test_opencode_gates_ctx_evaluation_on_an_assistant_message_with_tokens():
    """opencode 는 `message.updated` + `role==assistant` + `tokens` 없이는 ctx 판정
    (`computeCtxState`) 진입 자체가 없다 — 구조 대조로 codex/claude 의 0-sentinel 과 같은 결론
    (첫 모델 응답 전에는 점유를 모른다)."""
    source = OPENCODE_GUARD.read_text(encoding="utf-8")
    gate = re.search(r'if \(info\.role !== "assistant" \|\| !info\.tokens\) return;', source)
    assert gate, "opencode: assistant/tokens 게이트 문구를 못 찾음 — 소스가 바뀌었으면 이 가드도 재검토"
    # `function computeCtxState(...)` 정의가 아니라 **호출부**(`= computeCtxState(used, ...)`)를
    # 찾는다 — 정의는 게이트보다 항상 앞이라(파일 상단 순수 함수 블록) 정의 위치로는 순서를
    # 못 본다.
    evaluation = re.search(r"=\s*computeCtxState\(", source)
    assert evaluation and evaluation.start() > gate.start(), (
        "ctx 판정(computeCtxState)이 assistant/tokens 게이트보다 앞에 있다 — "
        "첫 응답 전에도 평가될 수 있다"
    )
