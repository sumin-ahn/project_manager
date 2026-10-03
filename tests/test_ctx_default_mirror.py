"""ctx 임계 디폴트 미러 정합 가드 (T-0207·T-0550·T-0770·T-0906·T-0923).

쓴 % 임계(중간 기록 권고·압축 임박 경고)와 사용 한도 기본값은 다섯 곳에 있다 — 엔진 레지스트리
(`local_conf.KEY_REGISTRY` · fresh init 이 local.conf 에 쓰는 값의 정본) + 어댑터 네 파일(claude
ctx_guard.py·codex pm_orch_codex.py·opencode ctx-guard-core.cjs·opencode pm_orch_opencode.py). 어댑터는
엔진을 import 하지 않고(의존 최소) 리터럴을 보유하므로, 한 곳만 바꾸고 미러를 잊으면 엔진이 기록한
값과 훅 판정 임계가 어긋난다.

이 가드는 어댑터 파일을 정규식으로 파싱(언어 무관·hermetic·라이브 없음)하고 엔진 사이트는 레지스트리
조회로 읽어 사이트들의 디폴트가 서로 **일치**함을 강제한다. opencode relay 드라이버(pm_orch_opencode.py)는
회전 임계(압축 임박 경고)와 사용 한도만 소비하므로 그 둘만 대조한다. 압축 비율(`ctx.compact_at_pct`)은
어느 어댑터도 읽지 않는다 — 엔진이 그 값으로 하네스 설정 파일의 압축 지점을 쓴다.

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
# opencode relay 드라이버 — 회전 임계·사용 한도의 네 번째 어댑터 사이트(T-0923).
OPENCODE_DRIVER = REPO / "templates" / "opencode" / ".opencode" / "pm_orch_opencode.py"
# codex 는 옆에 ctx_guard 모듈이 없어 relay driver 파일이 곧 어댑터 ctx 사이트다 — 같은 파일의
# 훅 축(ctx_thresholds·classify)과 relay 회전 축이 이 상수들을 소비한다(T-0770).
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


def test_ctx_checkpoint_at_default_mirrors_across_all_sites():
    engine = _engine("ctx.checkpoint_at_pct")
    claude = _grab(CLAUDE_GUARD, r"CTX_CHECKPOINT_AT_PCT_DEFAULT\s*=\s*(\d+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+CHECKPOINT_AT_PCT_DEFAULT\s*=\s*(\d+)")
    codex = _grab(CODEX_GUARD, r"CTX_CHECKPOINT_AT_PCT_DEFAULT\s*=\s*(\d+)")
    assert engine == claude == opencode == codex, (
        f"중간 기록 권고 디폴트 미러 불일치: engine={engine} claude={claude} "
        f"opencode={opencode} codex={codex}"
    )


def test_ctx_precompact_at_default_mirrors_across_all_sites():
    engine = _engine("ctx.precompact_at_pct")
    claude = _grab(CLAUDE_GUARD, r"CTX_PRECOMPACT_AT_PCT_DEFAULT\s*=\s*(\d+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+PRECOMPACT_AT_PCT_DEFAULT\s*=\s*(\d+)")
    codex = _grab(CODEX_GUARD, r"CTX_PRECOMPACT_AT_PCT_DEFAULT\s*=\s*(\d+)")
    driver = _grab(OPENCODE_DRIVER, r"CTX_PRECOMPACT_AT_PCT_DEFAULT\s*=\s*(\d+)")
    assert engine == claude == opencode == codex == driver, (
        f"압축 임박 경고 디폴트 미러 불일치: engine={engine} claude={claude} "
        f"opencode={opencode} codex={codex} opencode-driver={driver}"
    )


def test_ctx_window_tokens_default_mirrors_across_all_sites():
    """사용 한도 디폴트(200000)도 미러 — 엔진 사이트는 하네스 구성원 기본값이다(T-0923).

    공통 분모 키는 사라졌고 사용 한도는 하네스마다 `harness.<name>.ctx_window_tokens` 하나다.
    세 하네스 구성원의 선언값이 같고, 어댑터 네 사이트가 그 값을 품는다."""
    engine = {_engine(f"harness.{name}.ctx_window_tokens") for name in ("claude", "codex", "opencode")}
    assert len(engine) == 1, engine
    (engine_value,) = engine
    claude = _grab(CLAUDE_GUARD, r"CTX_WINDOW_TOKENS_DEFAULT\s*=\s*([\d_]+)")
    opencode = _grab(OPENCODE_GUARD, r"const\s+CTX_WINDOW_TOKENS_DEFAULT\s*=\s*(\d+)")
    codex = _grab(CODEX_GUARD, r"CTX_WINDOW_TOKENS_DEFAULT\s*=\s*([\d_]+)")
    driver = _grab(OPENCODE_DRIVER, r"CTX_WINDOW_TOKENS_DEFAULT\s*=\s*([\d_]+)")
    assert engine_value == claude == opencode == codex == driver, (
        f"사용 한도 디폴트 미러 불일치: engine={engine_value} claude={claude} "
        f"opencode={opencode} codex={codex} opencode-driver={driver}"
    )


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
    """디폴트 자체가 sanity(0 < checkpoint_at < precompact_at < compact_at < 100)를 만족 —
    어댑터 sanity 폴백이 엔진 기본으로 떨어질 때 무한/역전이 없도록."""
    checkpoint = _engine("ctx.checkpoint_at_pct")
    precompact = _engine("ctx.precompact_at_pct")
    compact = _engine("ctx.compact_at_pct")
    assert 0 < checkpoint < precompact < compact < 100, (checkpoint, precompact, compact)


# ── 세 어댑터 밴드 대조 (T-0923 AT-017) ──────────────────────────────────────
# 정수 사용률 표 × 비율 70·80 → 기대 밴드. 경계(70·77·80)를 양쪽에서 태운다.
_BAND_TABLE = ((69, "ok"), (70, "nudge"), (76, "nudge"), (77, "nudge2"), (79, "nudge2"),
               (80, "stop"), (100, "stop"))
_BAND_CONF = {"ctx.checkpoint_at_pct": "70", "ctx.precompact_at_pct": "80"}
_BAND_BUDGET = 200000


def test_three_adapters_classify_the_same_band_table():
    """같은 정수 사용률에 claude·codex·opencode 가 같은 밴드를 낸다 — 판정 함수를 실제로 실행한다."""
    claude = _load_module("claude_ctx_guard_band_table", CLAUDE_GUARD)
    codex = _load_module("codex_ctx_guard_band_table", CODEX_GUARD)
    claude_bands = [claude.classify(used, claude.ctx_thresholds(_BAND_CONF))
                    for used, _band in _BAND_TABLE]
    codex_bands = [codex.classify(used, codex.ctx_thresholds(_BAND_CONF))
                   for used, _band in _BAND_TABLE]

    node = shutil.which("node")
    assert node is not None, (
        "Node를 설치하고 pytest 실행 환경의 PATH에 추가해야 합니다. "
        "세 하네스 밴드 비교에서 OpenCode의 실제 판정을 생략할 수 없습니다."
    )
    script = (
        f"const m = require({json.dumps(str(OPENCODE_GUARD))});"
        f"const t = m.resolveThresholds({json.dumps(_BAND_CONF)});"
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


@pytest.mark.parametrize("harness", ("claude", "codex", "opencode"))
def test_t0932_default_window_is_not_capacity_evidence(tmp_path, harness):
    """출하 W 기본값은 보존하지만 미적용 환경의 C/E를 생성하지 않는다."""
    leaf = _load_module("ctx_default_unknown_capacity", LOCAL_CONF_PY)
    paths = {
        ".project_manager/local.conf": b"# existing local configuration\n",
        ".project_manager/adapter_baseline.json": b'{"schema":1}\n',
        ".claude/settings.json": b"{}\n",
        ".codex/config.toml": b"# existing profile\n",
        ".opencode/opencode.jsonc": b"{}\n",
    }
    for rel, data in paths.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    record = tmp_path / leaf.CTX_BUDGET_RELPATH
    assert not record.exists()
    before = {rel: (tmp_path / rel).read_bytes() for rel in paths}
    identity = {"model": "fixture-model", "provider": "fixture-provider", "profile": ""}
    result = leaf.read_ctx_budget(tmp_path, {}, harness, identity)
    assert leaf.ctx_input_values({})[f"harness.{harness}.ctx_window_tokens"] == "200000"
    assert result["status"] == "unknown" and result["W"] == 200000
    assert result["C"] is None and result["E"] is None and result["P"] is None
    assert {rel: (tmp_path / rel).read_bytes() for rel in paths} == before
    assert not record.exists()


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

    assert claude.context_tokens_from_transcript(str(transcript)) == 0
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
