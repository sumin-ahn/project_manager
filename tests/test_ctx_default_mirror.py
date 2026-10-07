"""ctx 임계 미러 정합 가드 (T-0207·T-0550·T-0770·T-0906·T-0923·T-0943).

쓴 % 임계(중간 기록 권고·압축 임박 경고)와 사용 한도의 기본값은 엔진 레지스트리 한 곳뿐이다 — 어댑터는
압축 한도 기록(`local_conf.read_ctx_budget`)이 돌려준 비율과 적용 한도를 쓰고 기본값 사본을 두지 않는다
(T-0943). 어댑터에 남은 미러는 레지스트리에 없는 파생 상수(2단 넛지 마진) 하나다.

밴드 대조(`test_three_adapters_classify_the_same_band_table`)는 세 어댑터의 판정 함수를 **실제로
실행**해 같은 입력 표로 비교한다(opencode 는 node 로 core 를 require).
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# 엔진 사이트는 리터럴이 아니라 키 레지스트리다(T-0906) — board 의 ctx 상수는 거기로 옮겨졌다.
LOCAL_CONF_PY = REPO / ".project_manager" / "tools" / "local_conf.py"
CLAUDE_GUARD = REPO / "templates" / "claude_code" / ".claude" / "ctx_guard.py"
# opencode ctx-guard 상수는 진입점 shim(plugins/ctx-guard.js)이 아니라 core 모듈에 산다 — 진입점은
# opencode 로드 규약(export=단일 함수·T-0283)을 위해 팩토리만 export 하는 얇은 shim 이고, 순수 헬퍼·
# 상수·팩토리 본체는 lib/ctx-guard-core.cjs(opencode 미스캔·node require 대상)로 분리됐다.
OPENCODE_GUARD = REPO / "templates" / "opencode" / ".opencode" / "lib" / "ctx-guard-core.cjs"
# codex 는 옆에 ctx_guard 모듈이 없어 relay driver 파일이 곧 어댑터 ctx 사이트다 — 같은 파일의
# 훅 축(classify)과 relay 회전 축이 이 상수들을 소비한다(T-0770).
CODEX_GUARD = REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py"


def _grab(path: Path, pattern: str) -> int:
    match = re.search(pattern, path.read_text(encoding="utf-8"))
    assert match, f"{path.name}: 디폴트 상수 못 찾음 (패턴 {pattern!r})"
    return int(match.group(1).replace("_", ""))  # 파이썬 숫자 리터럴 언더스코어(200_000) 허용.


def _engine(key: str) -> int:
    """엔진 사이트 = 레지스트리가 선언한 이 키의 기본값 (어댑터 리터럴의 대조 기준)."""
    declared = _load_module("local_conf_ctx_mirror", LOCAL_CONF_PY).default_for(key)
    assert declared is not None, f"레지스트리에 `{key}` 기본값 선언이 없다"
    return int(declared)


def test_ctx_nudge2_margin_mirrors_across_adapter_sites():
    """2단 넛지 마진(%p)도 세 어댑터가 같은 값을 갖는다 (T-0328·T-0770).

    nudge2 밴드 = max(precompact_at − 마진, checkpoint_at) <= 사용 < precompact_at 이라 마진이 어긋나면
    세 하네스의 밴드 경계가 갈린다. 엔진엔 이 파생 상수가 없어(레지스트리는 두 비율만 선언)
    어댑터 3사이트만 대조한다."""
    claude = _grab(CLAUDE_GUARD, r"CTX_NUDGE2_MARGIN_PCT\s*=\s*(\d+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+NUDGE2_MARGIN_PCT\s*=\s*(\d+)")
    codex = _grab(CODEX_GUARD, r"CTX_NUDGE2_MARGIN_PCT\s*=\s*(\d+)")
    assert claude == opencode == codex, (
        f"nudge2 마진 미러 불일치: claude={claude} opencode={opencode} codex={codex}"
    )


def test_ctx_default_sanity_checkpoint_below_precompact_below_compact():
    """레지스트리 기본값 자체가 적용 검사(0 < checkpoint_at < precompact_at < compact_at < 100)를 통과한다."""
    checkpoint = _engine("ctx.checkpoint_at_pct")
    precompact = _engine("ctx.precompact_at_pct")
    compact = _engine("ctx.compact_at_pct")
    assert 0 < checkpoint < precompact < compact < 100, (checkpoint, precompact, compact)


# ── 세 어댑터 밴드 대조 (T-0923 AT-017) ──────────────────────────────────────
# 정수 사용률 표 × 비율 70·80 → 기대 밴드. 경계(70·77·80)를 양쪽에서 태운다.
_BAND_TABLE = ((69, "ok"), (70, "nudge"), (76, "nudge"), (77, "nudge2"), (79, "nudge2"),
               (80, "stop"), (100, "stop"))
_BAND_THRESHOLDS = {"checkpoint_at_pct": 70, "precompact_at_pct": 80}
_BAND_BUDGET = 200000


def test_three_adapters_classify_the_same_band_table():
    """같은 정수 사용률에 claude·codex·opencode 가 같은 밴드를 낸다 — 판정 함수를 실제로 실행한다."""
    claude = _load_module("claude_ctx_guard_band_table", CLAUDE_GUARD)
    codex = _load_module("codex_ctx_guard_band_table", CODEX_GUARD)
    claude_bands = [claude.classify(used, _BAND_THRESHOLDS) for used, _band in _BAND_TABLE]
    codex_bands = [codex.classify(used, _BAND_THRESHOLDS) for used, _band in _BAND_TABLE]

    node = shutil.which("node")
    assert node is not None, (
        "Node를 설치하고 pytest 실행 환경의 PATH에 추가해야 합니다. "
        "세 하네스 밴드 비교에서 OpenCode의 실제 판정을 생략할 수 없습니다."
    )
    script = (
        f"const m = require({json.dumps(str(OPENCODE_GUARD))});"
        f"const t = {json.dumps(_BAND_THRESHOLDS)};"
        f"const used = {json.dumps([used for used, _band in _BAND_TABLE])};"
        f"console.log(JSON.stringify(used.map((u) => "
        f"m.computeCtxState(u * {_BAND_BUDGET} / 100, {_BAND_BUDGET}, t).level)));"
    )
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True,
                               encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stderr
    opencode_bands = json.loads(completed.stdout)

    expected = [band for _used, band in _BAND_TABLE]
    assert claude_bands == codex_bands == opencode_bands == expected, {
        "claude": claude_bands, "codex": codex_bands, "opencode": opencode_bands,
        "expected": expected}


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

    assert claude.context_tokens_from_transcript(str(transcript)) == (0, None)
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
