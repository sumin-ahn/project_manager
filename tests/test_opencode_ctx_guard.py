"""opencode 어댑터 ctx checkpoint plugin 정합 테스트 (T-0551·ADR-0081 Decision 1·3).

opencode plugin(`.opencode/plugins/ctx-guard.js`)이 컨텍스트 토큰을 추적해 임계 도달 시
checkpoint 박제를 안내하고, `session.compacted`를 병행 관측해 count·re-arm·압축 후 안내를
수행하며, await 중 re-arm된 새 사이클을 구 메시지가 덮지 않는 것을 여러 층위에서 단언한다:

  1. config 정합  — opencode.jsonc 가 `compaction.auto:false`를 재도입하지 않고, 모델 limit 의
       output < context 제약을 만족한다.
  2. plugin 정합  — event 토큰추적·session.compacted 병행 신호·차단/marker 제거·checkpoint 문구.
  3. node 동작    — 밴드 판정, 세션별 관측 count·re-arm·안내, cycle epoch 경합 방지.
  4. 라이브 로드 — (T-0283·release-tier·`PM_OPENCODE_LIVE=1`+opencode 바이너리) 실 opencode 헤드리스가
       플러그인을 autoload 성공(로드 실패 로그 부재 + factory 실행 마커)하는지 실측. 기본 skip(CI green 불변).

로드 규약(실측 T-0283·opencode 1.17.18): `.opencode/plugins/` 안 각 파일의 export 를 순회해 *모두 함수*
이길 요구·각각을 팩토리로 호출한다 — CJS `module.exports`(객체/단일함수)는 거부되고 비함수 export(상수)
하나로도 로드 실패. 그래서 진입점 `plugins/ctx-guard.js` 는 ESM 으로 팩토리 하나만 export 하는 얇은 shim
이고, 순수 헬퍼·상수·팩토리 본체는 plugins/ *바깥* CJS 모듈 `lib/ctx-guard-core.cjs`(opencode 미스캔·node
require 대상)에 둔다. 정적/순수 검증은 그 core 를, autoload 규약은 라이브 로드 게이트가 본다.

JS 로직의 SDK wiring 은 mock client 로 결정적으로 검증한다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OPENCODE = REPO / "templates" / "opencode" / ".opencode"

PROJECT_CONFIG = OPENCODE / "opencode.jsonc"
PLUGIN_DIR = OPENCODE / "plugins"
PLUGIN_FILE = PLUGIN_DIR / "ctx-guard.js"  # ESM 진입점 shim (opencode autoload 대상).
# 순수 헬퍼·상수·팩토리 본체 (CJS·node 자가검증 require 대상·opencode 미스캔·T-0283).
CORE_DIR = OPENCODE / "lib"
CORE_FILE = CORE_DIR / "ctx-guard-core.cjs"


# ── jsonc 파서 (T-0011 test_opencode_permission_guard 선례 동일) ──────────────

def _strip_jsonc_comments(text: str) -> str:
    """jsonc 의 줄 주석(//...)을 제거해 stdlib json 으로 파싱 가능하게 한다.

    문자열 값에 `//` 가 없으므로(URL 은 $schema 한 줄 — `://` 는 보호) 단순 줄 단위 제거.
    """
    out_lines = []
    for line in text.splitlines():
        m = re.search(r"(?<!:)//", line)
        if m:
            line = line[: m.start()]
        out_lines.append(line)
    return "\n".join(out_lines)


def _load_config() -> dict:
    text = PROJECT_CONFIG.read_text(encoding="utf-8")
    return json.loads(_strip_jsonc_comments(text))


def _plugin_src() -> str:
    """팩토리·헬퍼 로직 정적 검증 대상 = core 모듈(로직이 여기 산다·T-0283 이후)."""
    return CORE_FILE.read_text(encoding="utf-8")


def _shim_src() -> str:
    """opencode 진입점 ESM shim(plugins/ctx-guard.js) 원문 — 로드 규약 shape 검증용."""
    return PLUGIN_FILE.read_text(encoding="utf-8")


def _make_ctx_guard_project(tmp_path: Path) -> Path:
    """findEngineRoot가 인식할 최소 project를 pytest tmp_path 아래에 만든다(소스 트리 무오염)."""
    root = tmp_path / "ctx-guard-project"
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "pm_handoff.py").write_text("# ctx-guard test probe\n", encoding="utf-8")
    return root


# ── 1. config 정합: 기본 compaction + 모델 limit 제약 ───────────────────────

def test_accumulate_tokens_prefers_total_with_sum_fallback():
    """plugin 판정도 driver 파서와 동일하게 total 우선 + 합산 폴백 (T-0557 통일)."""
    script = (
        "const m = require(\"./ctx-guard-core.cjs\");"
        "const a = m.accumulateTokens({total: 500, input: 1, output: 1});"
        "const b = m.accumulateTokens({input: 2, output: 3, reasoning: 0,"
        " cache: {read: 4, write: 5}});"
        "if (a !== 500 || b !== 14) throw new Error(`a=${a} b=${b}`);"
        "console.log(['TOTAL','FIRST','GREEN'].join('_'));"
    )
    if _NODE is None:
        pytest.skip("node 없음")
    out = _run_node_check(script)
    # 성공 마커는 소스에 리터럴로 없다(join 조립) — 실패 시 stderr 의 소스 echo 로 위양성 불가.
    assert "TOTAL_FIRST_GREEN" in out


def test_config_exists_and_parses():
    """opencode.jsonc 가 존재하고 jsonc 로 파싱된다."""
    assert PROJECT_CONFIG.exists(), f"project config 없음: {PROJECT_CONFIG}"
    data = _load_config()
    assert isinstance(data, dict)


def test_config_does_not_disable_auto_compaction():
    """전역 자동 컴팩션은 기본값(켜짐)을 유지한다 — 전역 off 재유입 차단.

    컴팩션을 전역으로 끄면 native task 자식 세션의 ctx-guard 면제와 겹쳐 checkpoint 안내도
    native compaction도 없는 상태가 된다 — 위임 서브에이전트가 컨텍스트 초과로 죽는 클래스.
    PM 메인 세션은 사전 checkpoint 안내와 native compaction 사후 안내를 함께 사용한다.
    (jsonc 주석은 파서가 제거하므로 주석 안 문자열은 이 가드를 통과시키지 못한다.)
    """
    data = _load_config()
    compaction = data.get("compaction") or {}
    assert compaction.get("auto") in (None, True), (
        "compaction.auto 는 미지정(기본 켜짐) 또는 true 여야 한다 — false·문자열·null 은 자식 "
        "세션을 컴팩션·ctx-guard 둘 다 없는 무보호로 만든다"
    )


def test_config_model_limits_keep_output_below_context():
    """명시한 모든 모델 limit 은 output < context — 컴팩션 무한 루프 형상 차단.

    자동 컴팩션 트리거가 "입력 > context - output" 이라 output ≥ context 면 조건이 항상 참이 되어
    세션이 실제 작업 대신 요약만 반복한다(라이브 실측). 값 가드 + 그 근거 주석의 존재를 함께 본다.
    """
    data = _load_config()
    checked_limits = 0
    for provider_name, provider in data.get("provider", {}).items():
        for model_name, model in provider.get("models", {}).items():
            limit = model.get("limit")
            if limit is None:
                continue
            checked_limits += 1
            context, output = limit.get("context"), limit.get("output")
            assert isinstance(context, int) and isinstance(output, int), (
                f"{provider_name}/{model_name}: limit.context·limit.output 은 정수여야 함 "
                f"(found: {limit})"
            )
            assert output < context, (
                f"{provider_name}/{model_name}: limit.output({output}) 이 "
                f"limit.context({context}) 이상 — 컴팩션이 매 턴 발화해 요약만 반복함"
            )
    assert checked_limits, "출하 모델에 검증할 limit 설정이 없음"
    # 왜(주석) 정적 확인 — 채택자가 자기 모델 엔트리를 더할 때 이 제약을 읽게 한다.
    raw = PROJECT_CONFIG.read_text(encoding="utf-8")
    assert "context - output" in raw, "limit 주석에 컴팩션 트리거 제약(무한 루프) 경고 없음"


def test_config_keeps_existing_permission_guard():
    """ctx 변경이 기존 bash permission 가드(T-0011)를 깨지 않는다 (회귀 방지)."""
    data = _load_config()
    bash = data.get("permission", {}).get("bash")
    assert isinstance(bash, dict), "permission.bash 패턴맵이 사라짐 (T-0011 회귀)"
    assert bash.get("rm *") == "deny", "기존 deny 가드 손실 (T-0011·T-0160 회귀)"


# ── 2. plugin 정합: 파일 존재 + 필수 호출/구조 ──────────────────────────────

def test_plugin_file_exists():
    """진입점 shim 이 .opencode/plugins/ 에·core 모듈이 .opencode/lib/ 에 존재 (T-0283 분리)."""
    assert PLUGIN_DIR.is_dir(), f"plugin 디렉토리 없음: {PLUGIN_DIR}"
    assert PLUGIN_FILE.exists(), f"ctx-guard 진입점 shim 없음: {PLUGIN_FILE}"
    assert CORE_DIR.is_dir(), f"core lib 디렉토리 없음: {CORE_DIR}"
    assert CORE_FILE.exists(), f"ctx-guard core 모듈 없음: {CORE_FILE}"


def test_plugin_entry_is_esm_single_function_export():
    """opencode 로드 규약(실측 T-0283): 진입점은 ESM 으로 팩토리 하나만 export 하는 shim 이다.

    opencode 는 plugins/ 각 파일의 export 를 순회해 *모두 함수*이길 요구하고 각각을 팩토리로
    호출한다 — CJS `module.exports`(객체·단일함수 불문)는 거부('Plugin export is not a function')
    되고 비함수 export(상수)·헬퍼 함수까지 플러그인으로 오인한다. 이 회귀(T-0283 원 버그)를 막는
    durable 정적 가드: 진입점이 (1) CJS module.exports 를 쓰지 않고 (2) core 를 import 하며
    (3) 정확히 하나의 export 문(팩토리 CtxGuardPlugin)만 갖는다. (실 autoload 는 라이브 게이트가 봄.)
    """
    shim = _shim_src()
    # 주석(//...) 줄을 제외한 실 코드만 검사 (설명 주석에 규약 언급이 있어 오탐 방지).
    code = "\n".join(ln for ln in shim.splitlines() if not ln.lstrip().startswith("//"))
    # (1) CJS export 금지 — 이게 정확히 로드 실패를 낸 원 버그 형태.
    assert "module.exports" not in code, (
        "진입점이 CJS module.exports 사용 (T-0283 회귀 — opencode 가 함수 아니라며 로드 거부)"
    )
    # (2) 팩토리 본체·헬퍼는 core 모듈에서 가져온다.
    assert re.search(r'import\s+core\s+from\s+["\']\.\./lib/ctx-guard-core\.cjs["\']', shim), (
        "진입점이 ../lib/ctx-guard-core.cjs 를 import 하지 않음"
    )
    # (3) export 문은 딱 하나(팩토리) — 상수/헬퍼를 함께 export 하면 opencode 가 오인/로드 실패.
    #     주석(//...)은 제외하고 실 export 문장만 센다(줄 첫 토큰이 export).
    export_lines = [
        ln for ln in shim.splitlines() if re.match(r"\s*export\s", ln)
    ]
    assert len(export_lines) == 1, (
        f"진입점 export 문이 정확히 1개가 아님(팩토리 하나만이어야·상수/헬퍼 export 금지): {export_lines}"
    )
    assert "CtxGuardPlugin" in export_lines[0], (
        f"진입점의 단일 export 가 CtxGuardPlugin 팩토리가 아님: {export_lines[0]!r}"
    )


def test_ctx_guard_shim_and_core_co_present_in_source_tree():
    """소스 트리 co-presence precheck (T-0283): shim(plugins/ctx-guard.js)과 core(lib/ctx-guard-core.cjs)가
    함께 실재한다. shim 이 core 를 import 하므로 한쪽만 있으면 로드가 깨진다.

    ⚠️ 이건 *소스 트리* precheck 일 뿐 — load-bearing 커플링 단언(실 출하 산출물 co-presence)은
    fresh pm_import 산출물에서 본다(test_fresh_adopter_e2e·[[feature-ship-needs-fresh-adopter-gate]]).
    어댑터 파일은 manifest 미등재 pm_import(rglob 전체트리)로만 출하되므로(self-update 채널 없음),
    "함께 landing" 은 import 산출물에서 검증해야 실효다."""
    assert PLUGIN_FILE.exists(), f"진입점 shim 없음: {PLUGIN_FILE}"
    assert CORE_FILE.exists(), f"core 모듈 없음: {CORE_FILE}"


def test_plugin_subscribes_message_events():
    """event 훅으로 message.updated 를 구독해 토큰을 추적한다."""
    src = _plugin_src()
    assert "event:" in src or "event :" in src, "event 훅 없음 — 토큰추적 불가"
    assert "message.updated" in src, "message.updated 이벤트 구독 없음"
    assert "tokens" in src, "tokens 참조 없음 — ctx% 산출 불가"


def test_plugin_removes_stop_consumers_and_allowlist():
    """stop 판정은 유지하되 deny/throw/marker/allow-list 소비 표면은 완전히 제거한다."""
    src = _plugin_src()
    assert 'level = "stop"' in src, "computeCtxState stop 레벨 반환이 사라짐"
    for removed in (
        "fired.stop",
        "writeStopMarker",
        '"permission.ask"',
        '"tool.execute.before"',
        "isHandoffTool",
        "isHandoffBash",
        "isHandoffTarget",
        "isNewWorkPermission",
        "ctx-stop handoff triggered",
    ):
        assert removed not in src, f"폐기된 stop 소비 표면 잔존: {removed}"


def test_plugin_subscribes_compaction_events_with_local_observation_count():
    """session.compacted는 세션-로컬 event count만 올리고 사후 복원은 하지 않는다."""
    src = _plugin_src()
    assert "session.compacted" in src, "session.compacted 이벤트 분기 없음"
    assert "compactionCount" in src, "세션별 compaction count 없음"
    assert re.search(r"compactionCount\s*\+=\s*1", src), "관측 이벤트 단순 증가 없음"
    for removed in (
        "client.session.messages",
        "CompactionPart",
        "compactionRestore",
        "readCompactionSnapshot",
        "mergeCompactionCount",
        "reconcileCompactionEvent",
        "compactionRestoredBaseline",
    ):
        assert removed not in src, f"폐기된 compaction 복원 기계 잔존: {removed}"
    assert "buildCompactedGuidance" in src, "압축 후 모델 안내 빌더 없음"
    assert "notifyCompacted" in src, "압축 후 사람용 toast 없음"


def test_plugin_reads_thresholds_from_local_conf():
    """임계값을 엔진 local.conf 의 ctx_*_pct 에서 읽는다 (T-0013 계약)."""
    src = _plugin_src()
    assert "ctx_nudge_pct" in src, "ctx_nudge_pct 임계 참조 없음"
    assert "ctx_stop_pct" in src, "ctx_stop_pct 임계 참조 없음"
    assert "local.conf" in src, "local.conf 직접 파싱 경로 참조 없음"


# ── 2c. ctx 예산 전환 (ADR-0041 · T-0235): resolveBudget · modelLimit 폐기 ─────

def test_plugin_uses_resolve_budget_not_model_limit():
    """정지/넛지 분모가 모델 물리한도가 아니라 해소된 예산이다 (ADR-0041).

    resolveBudget(conf,"opencode") 신설·event limit 주입원 교체 + modelLimit()/cachedLimit/
    providers 조회가 완전히 사라졌음(물리한도 개념 폐기)을 정적으로 단언한다.
    """
    src = _plugin_src()
    # 예산 해소 순수함수 + 200K 기본 상수 신설.
    assert "function resolveBudget" in src, "resolveBudget 순수함수 신설 없음"
    assert re.search(r"CTX_WINDOW_TOKENS_DEFAULT\s*=\s*200000", src), (
        "CTX_WINDOW_TOKENS_DEFAULT=200000 상수 없음 (board.py 미러)"
    )
    # 하네스별 오버라이드 키 참조 (precedence 상위층).
    assert "ctx_window_tokens_" in src, "하네스별 오버라이드 키(ctx_window_tokens_<harness>) 참조 없음"
    # event 핸들러가 resolveBudget 로 limit 을 주입한다("opencode" 고정·conf 캐시 재사용).
    assert re.search(r'const limit = resolveBudget\([^;]*["\']opencode["\']\)', src), (
        "event limit 주입원이 resolveBudget(...,\"opencode\") 형태가 아님"
    )
    # 물리한도 조회(modelLimit·cachedLimit·config.providers)가 완전히 폐기됐다.
    assert "modelLimit" not in src, "modelLimit() 폐기 안 됨 (ADR-0041 물리한도 개념 제거 위반)"
    assert "cachedLimit" not in src, "cachedLimit 캐시 폐기 안 됨 (ADR-0041 위반)"
    assert "config.providers" not in src, "client.config.providers() 조회 폐기 안 됨 (ADR-0041 위반)"


def test_plugin_has_idempotency_guard():
    """nudge/nudge2 는 세션별·compaction 사이클별 1회이며 compaction 뒤 재무장한다."""
    src = _plugin_src()
    assert re.search(r"sessionStates\s*=\s*new Map", src), "세션별 상태 Map 없음"
    assert "fired.nudge" in src and "fired.nudge2" in src, "밴드 멱등 가드 없음"
    assert re.search(r"fired\.nudge\s*=\s*false", src), "compaction 후 nudge re-arm 없음"
    assert re.search(r"fired\.nudge2\s*=\s*false", src), "compaction 후 nudge2 re-arm 없음"
    assert re.search(r"cycleEpoch\s*\+=\s*1", src), "compaction re-arm cycle epoch 증가 없음"


def test_plugin_exempts_native_subsessions_and_caches_lookup_outcomes(tmp_path):
    """native task 자식은 밴드·compaction·모델 주입 모두 면제하며 lookup 을 캐시한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — sub-session SDK mock 단위 skip (정적 검증만 적용)")

    project_root = _make_ctx_guard_project(tmp_path)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;

(async () => {
  const childID = "ses_child_cache";
  let childCalls = 0;
  const childHooks = await m.CtxGuardPlugin({client:{session:{
    get: async (opts) => {
      childCalls++;
      assert.deepStrictEqual(opts, {path:{id:childID}}, "SDK session.get 호출 형식");
      return {data:{id:childID, parentID:"ses_parent_cache"}};
    },
  }}, directory:projectRoot});
  const nudge = {event:{type:"message.updated", properties:{info:{
    sessionID:childID, role:"assistant", tokens:{input:145000}
  }}}};
  await childHooks.event(nudge);
  await childHooks.event({event:{type:"session.compacted", properties:{sessionID:childID}}});
  const childSystem = {system:[]};
  await childHooks["experimental.chat.system.transform"]({sessionID:childID}, childSystem);
  assert.deepStrictEqual(childSystem.system, [], "child injection must bypass");
  assert.strictEqual(childCalls, 1, "same session lookup must be cached");

  const failureID = "ses_lookup_failure";
  let failureCalls = 0;
  const failureHooks = await m.CtxGuardPlugin({client:{session:{get: async () => {
    failureCalls++;
    throw new Error("SDK unavailable");
  }}}, directory:projectRoot});
  await failureHooks.event({event:{type:"message.updated", properties:{info:{
    sessionID:failureID, role:"assistant", tokens:{input:145000}
  }}}});
  const failureSystem = {system:[]};
  await failureHooks["experimental.chat.system.transform"]({sessionID:failureID}, failureSystem);
  assert.strictEqual(failureSystem.system.length, 1, "lookup failure must not exempt main session");
  assert.strictEqual(failureCalls, 1, "failed lookup result must stay cached");

  const timeoutID = "ses_lookup_timeout";
  let timeoutCalls = 0;
  const timeoutHooks = await m.CtxGuardPlugin({client:{session:{
    get: async () => {
      timeoutCalls++;
      return new Promise(() => {});
    },
  }}, directory:projectRoot});
  const timeoutStarted = Date.now();
  await timeoutHooks.event({event:{type:"message.updated", properties:{info:{
    sessionID:timeoutID, role:"assistant", tokens:{input:145000}
  }}}});
  const timeoutSystem = {system:[]};
  await timeoutHooks["experimental.chat.system.transform"]({sessionID:timeoutID}, timeoutSystem);
  assert.ok(Date.now() - timeoutStarted < 3000, "never-resolving session.get did not time out");
  assert.strictEqual(timeoutSystem.system.length, 1, "lookup timeout must not exempt main session");
  assert.strictEqual(timeoutCalls, 1, "timed-out lookup result must stay cached");

  console.log("JS_SUBSESSION_GUARD_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script)
    assert "JS_SUBSESSION_GUARD_OK" in out, f"sub-session SDK mock 단위 실패. out={out!r}"


def test_plugin_compacted_counts_rearms_and_injects_post_notice(tmp_path):
    """compacted는 로컬 count만 증가하고 re-arm·횟수 없는 사후 안내를 병행한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — compaction 병행 신호 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const sessionID = "ses_compaction_cycle";
const nudgeEvent = () => ({
  event: {type:"message.updated", properties:{info:{
    sessionID, role:"assistant", tokens:{input:145000}
  }}}
});

(async () => {
  const toasts = [];
  const hooks = await m.CtxGuardPlugin({client:{
    session:{
      get: async () => ({data:{id:sessionID}}),
    },
    tui:{showToast: async (toast) => { toasts.push(toast.body.message); }},
  }, directory:projectRoot});

  assert.deepStrictEqual(
    Object.keys(hooks).sort(),
    ["event", "experimental.chat.system.transform"].sort(),
    "stop deny/throw hooks must be absent",
  );
  await hooks.event({event:{type:"message.updated", properties:{info:{
    sessionID, role:"assistant", tokens:{input:160000}
  }}}});
  let output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(output.system.length, 1, "stop band must be absorbed by final nudge guidance");
  assert.ok(output.system[0].includes("[ctx-nudge/강화]"), "stop band did not reuse final guidance");
  assert.ok(output.system[0].includes("pm_log.py checkpoint"), "stop-band guidance lacks checkpoint");

  await hooks.event(nudgeEvent());
  output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(output.system.length, 1, "first-cycle nudge missing");
  assert.ok(output.system[0].includes("pm_log.py checkpoint"), "nudge is not checkpoint instruction");

  await hooks.event(nudgeEvent());
  output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.deepStrictEqual(output.system, [], "same-cycle nudge fired twice");

  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(output.system.length, 1, "post-compaction notice missing");
  assert.ok(output.system[0].includes("compaction이 방금 일어났다"), "event notice missing");
  assert.ok(!/compaction\s+\d+회/.test(output.system[0]), "display-only count must not be exposed");
  assert.ok(output.system[0].includes(
    "python3 .project_manager/tools/pm_log.py checkpoint --task <이름> --trigger compaction"
  ));
  assert.ok(output.system[0].includes("구간·서사"), "post-compaction skeleton fill instruction missing");
  assert.ok(toasts.some((text) => text.includes("compaction이 방금 일어남")), "human toast missing");
  assert.ok(toasts.every((text) => !/compaction\s+\d+회/.test(text)), "toast exposed count");

  await hooks.event(nudgeEvent());
  output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(output.system.length, 1, "nudge was not re-armed after compaction");
  console.log("JS_COMPACTION_REARM_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script)
    assert "JS_COMPACTION_REARM_OK" in out, f"compaction 병행 신호 실패. out={out!r}"


def test_plugin_stages_compacted_notice_before_unawaited_event_hook_settles(tmp_path):
    """dispatcher가 event Promise를 버려도 첫 transform 전에 압축 후 안내를 동기 적재한다.

    upstream opencode v1.18.5 ``plugin/index.ts`` 의 event dispatcher는 각 plugin event hook을
    await하지 않는다. event 훅의 첫 await 전 적재를 정적으로 단언하고, toast가
    pending인 동안에도 transform이 안내를 소비할 수 있는지 동적으로 검증한다.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — non-awaited compacted event 경합 단위 skip")

    src = _plugin_src()
    branch = src.index('if (event.type === "session.compacted")')
    staged = src.index("session.pendingNudgeText = stagedGuidance", branch)
    first_await = src.index("if (await isChildSession", branch)
    assert staged < first_await, "compacted 안내가 첫 await 뒤로 밀렸음"

    project_root = _make_ctx_guard_project(tmp_path)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const sessionID = "ses_unawaited_compacted";

(async () => {
  let resolveToast;
  const toastPending = new Promise((resolve) => { resolveToast = resolve; });
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }, tui:{showToast: async () => toastPending}}, directory:projectRoot});

  // plugin/index.ts dispatcher와 같이 반환 Promise를 기다리지 않는다.
  let eventSettled = false;
  const eventPromise = hooks.event({event:{
    type:"session.compacted", properties:{sessionID}
  }}).then(() => { eventSettled = true; });
  await new Promise((resolve) => setImmediate(resolve));

  const output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(eventSettled, false, "toast fixture did not keep event hook pending");
  assert.strictEqual(output.system.length, 1, "unawaited event did not stage notice");
  assert.ok(output.system[0].includes("[ctx-checkpoint/압축후]"));

  resolveToast();
  await eventPromise;
  console.log("JS_COMPACTED_SYNC_STAGE_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script)
    assert "JS_COMPACTED_SYNC_STAGE_OK" in out, f"compacted 동기 적재 경합 실패. out={out!r}"


def test_plugin_cycle_epoch_rejects_old_message_after_compaction_rearm(tmp_path):
    """lookup await 중 re-arm이 끼어든 구 메시지는 새 사이클 상태를 덮지 않는다.

    epoch guard를 제거한 구 코드에서는 lookup 해제 후 old message.updated가 compacted
    안내를 nudge로 덮고 fired.nudge를 다시 올려 이 테스트가 red가 된다.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — cycle epoch 경합 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const sessionID = "ses_cycle_epoch";
const nudgeEvent = () => ({event:{type:"message.updated", properties:{info:{
  sessionID, role:"assistant", tokens:{input:145000}
}}}});

(async () => {
  let resolveLookup;
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: () => new Promise((resolve) => { resolveLookup = resolve; }),
  }}, directory:projectRoot});

  const oldMessage = hooks.event(nudgeEvent());
  assert.strictEqual(typeof resolveLookup, "function", "lookup await boundary was not reached");
  const compacted = hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  resolveLookup({data:{id:sessionID}});
  await Promise.all([oldMessage, compacted]);

  const output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(output.system.length, 1, "compacted notice was lost");
  assert.ok(output.system[0].includes("[ctx-checkpoint/압축후]"), "old message overwrote new-cycle notice");

  await hooks.event(nudgeEvent());
  const freshOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, freshOutput);
  assert.strictEqual(freshOutput.system.length, 1, "old message dirtied re-armed fired state");
  assert.ok(freshOutput.system[0].includes("[ctx-nudge]"), "fresh-cycle nudge did not fire");
  console.log("JS_CYCLE_EPOCH_GUARD_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script)
    assert "JS_CYCLE_EPOCH_GUARD_OK" in out, f"cycle epoch 경합 방지 실패. out={out!r}"


def test_plugin_attributes_pending_nudge_to_sid_captured_before_await(tmp_path):
    """await 순서가 뒤집혀도 pendingNudgeText는 event 진입 때 캡처한 SID에 귀속된다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — pending nudge SID 경합 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const firstID = "ses_race_first";
const secondID = "ses_race_second";
const resolvers = new Map();
const nudgeEvent = (sessionID, input) => ({event:{type:"message.updated", properties:{info:{
  sessionID, role:"assistant", tokens:{input}
}}}});

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: ({path:{id}}) => new Promise((resolve) => resolvers.set(id, resolve)),
  }}, directory:projectRoot});

  const first = hooks.event(nudgeEvent(firstID, 145000));
  const second = hooks.event(nudgeEvent(secondID, 155000));
  assert.ok(resolvers.has(firstID) && resolvers.has(secondID), "both SID lookups must be pending");
  resolvers.get(secondID)({data:{id:secondID}});
  await second;
  resolvers.get(firstID)({data:{id:firstID}});
  await first;

  const firstOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:firstID}, firstOutput);
  const secondOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:secondID}, secondOutput);
  assert.strictEqual(firstOutput.system.length, 1);
  assert.ok(firstOutput.system[0].includes("[ctx-nudge]"), "first SID lost its nudge payload");
  assert.strictEqual(secondOutput.system.length, 1);
  assert.ok(secondOutput.system[0].includes("[ctx-nudge/강화]"), "second SID lost its nudge2 payload");
  console.log("JS_PENDING_NUDGE_SID_RACE_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script)
    assert "JS_PENDING_NUDGE_SID_RACE_OK" in out, f"pending nudge SID 경합 실패. out={out!r}"


def test_plugin_keeps_pending_nudge_per_session_and_exempts_child_injection(tmp_path):
    """메인 nudge는 자식 system.transform이 소비하지 않고 해당 메인 호출에만 1회 전달된다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — nudge 교차 전달 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const mainID = "ses_main_nudge";
const childID = "ses_child_nudge";
const nudgeEvent = (sessionID) => ({
  event: {type:"message.updated", properties:{info:{
    sessionID, role:"assistant", tokens:{input:145000}
  }}}
});

(async () => {
  const hooks = await m.CtxGuardPlugin({
    client:{
      session:{get: async ({path:{id}}) => ({
        data:{id, ...(id === childID ? {parentID:mainID} : {})}
      })},
      tui:{showToast: async () => {}},
    },
    directory:projectRoot,
  });

  await hooks.event(nudgeEvent(mainID));
  const childOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:childID}, childOutput);
  assert.deepStrictEqual(childOutput.system, [], "child consumed main pending nudge");

  const mainOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:mainID}, mainOutput);
  assert.strictEqual(mainOutput.system.length, 1, "main did not receive its pending nudge");
  assert.ok(mainOutput.system[0].includes("[ctx-nudge]"), "wrong main nudge payload");
  assert.ok(mainOutput.system[0].includes("pm_log.py checkpoint"), "checkpoint command missing");

  const mainAgain = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:mainID}, mainAgain);
  assert.deepStrictEqual(mainAgain.system, [], "main nudge was not consumed exactly once");

  await hooks.event(nudgeEvent(childID));
  const childAgain = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:childID}, childAgain);
  assert.deepStrictEqual(childAgain.system, [], "child event created an exempt nudge");
  console.log("JS_NUDGE_SESSION_ISOLATED_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script)
    assert "JS_NUDGE_SESSION_ISOLATED_OK" in out, f"nudge 세션 격리 실패. out={out!r}"


def test_plugin_emits_nudge():
    """넛지(이른 경고) 경로가 있다 — nudge_pct 초과 시 안내(toast/message)."""
    src = _plugin_src()
    assert "nudge" in src.lower(), "넛지 로직 없음"
    # toast 경고 경로 (best-effort).
    assert "showToast" in src or "toast" in src.lower(), "넛지 안내(toast) 경로 없음"


# ── 3. 순수 결정 로직 자가검증 (node 있으면) ─────────────────────────────────

_NODE = shutil.which("node")


def _run_node_check(script: str) -> str:
    # cwd = core lib 디렉토리 → 스크립트의 `require("./ctx-guard-core.cjs")` 가 해소된다.
    proc = subprocess.run(
        [_NODE, "-e", script],
        cwd=str(CORE_DIR),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode:
        return (proc.stdout or "") + (proc.stderr or "")
    return proc.stdout


def test_plugin_pure_logic_node_selfcheck():
    """node 로 plugin 의 순수 결정 로직을 자가검증 (이벤트/opencode 런타임 없이).

    검증: 임계 분기(nudge/stop 경계·잔여%) · sanity 폴백(stop>nudge·음수) ·
    토큰 누적 · limit 미상 시 정지 보류. node 부재 시 skip (정적 검증으로 게이트).
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — plugin 순수 로직 자가검증 skip (정적 검증만 적용)")

    script = r"""
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");

// export 표면 (테스트 가능한 순수 함수가 떼어져 있어야 한다).
for (const fn of ["parseLocalConf","resolveThresholds","accumulateTokens","computeCtxState","CtxGuardPlugin"]) {
  assert.strictEqual(typeof m[fn], "function", "missing export: " + fn);
}

// 임계 해석 + sanity 폴백 (엔진 기본 30/20 · T-0207).
assert.deepStrictEqual(m.resolveThresholds({}), {nudge_pct:30, stop_pct:20});                       // 미설정→기본
assert.deepStrictEqual(m.resolveThresholds({ctx_nudge_pct:"25",ctx_stop_pct:"12"}), {nudge_pct:25, stop_pct:12});
assert.deepStrictEqual(m.resolveThresholds({ctx_nudge_pct:"5",ctx_stop_pct:"30"}), {nudge_pct:30, stop_pct:20}); // stop>nudge→폴백
assert.deepStrictEqual(m.resolveThresholds({ctx_nudge_pct:"-5",ctx_stop_pct:"3"}), {nudge_pct:30, stop_pct:20}); // 음수→폴백

// 토큰 누적.
assert.strictEqual(m.accumulateTokens({input:100,output:20,reasoning:5,cache:{read:10,write:3}}), 138);
assert.strictEqual(m.accumulateTokens(null), 0);

// ctx 상태 판정 (limit 1000, 20/10 명시 임계 = 잔여% 판정·경계 검증용).
const t = {nudge_pct:20, stop_pct:10};
assert.strictEqual(m.computeCtxState(500, 1000, t).level, "ok");    // 잔여 50%
assert.strictEqual(m.computeCtxState(800, 1000, t).level, "nudge"); // 잔여 20% (경계·<=)
assert.strictEqual(m.computeCtxState(850, 1000, t).level, "nudge"); // 잔여 15%
assert.strictEqual(m.computeCtxState(900, 1000, t).level, "stop");  // 잔여 10% (경계·<=)
assert.strictEqual(m.computeCtxState(950, 1000, t).level, "stop");  // 잔여 5%
assert.strictEqual(m.computeCtxState(999, 0, t).level, "ok");       // limit 미상→정지 보류(안전)

console.log("NODE_SELFCHECK_OK");
"""
    out = _run_node_check(script)
    assert "NODE_SELFCHECK_OK" in out, f"node 순수 로직 자가검증 실패. out={out!r}"


def test_js_resolve_budget_pure_unit():
    """node 로 resolveBudget 순수 함수(ADR-0041 예산 precedence)를 자가검증.

    precedence: ctx_window_tokens_<harness> > generic ctx_window_tokens >
    CTX_WINDOW_TOKENS_DEFAULT(200000). 각 층 >0 정수 sanity(≤0·비정수·미설정 → 다음 층).
    분모 통일 확인: resolveBudget 결과를 computeCtxState 가 stop/nudge/ok 로 판정한다.
    node 부재 시 skip (정적 검증 test_plugin_uses_resolve_budget_not_model_limit 로 게이트).
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — resolveBudget 순수 단위 skip (정적 검증만 적용)")

    script = r"""
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
assert.strictEqual(typeof m.resolveBudget, "function", "missing export: resolveBudget");
assert.strictEqual(m.CTX_WINDOW_TOKENS_DEFAULT, 200000, "CTX_WINDOW_TOKENS_DEFAULT 미러(200000) 아님");

// (a) 하네스별 오버라이드 키가 generic 보다 우선.
assert.strictEqual(m.resolveBudget({ctx_window_tokens_opencode:"500000", ctx_window_tokens:"1000000"}, "opencode"), 500000);
// (b) 오버라이드 없으면 generic ctx_window_tokens (back-compat·② 1M 무변경).
assert.strictEqual(m.resolveBudget({ctx_window_tokens:"1000000"}, "opencode"), 1000000);
// (c) 둘 다 없으면 200000 기본.
assert.strictEqual(m.resolveBudget({}, "opencode"), 200000);
assert.strictEqual(m.resolveBudget(null, "opencode"), 200000);
// (d) ≤0·비정수·공백은 그 층을 건너뛰고 다음 층으로 폴백 (0/음수 특수의미 없음).
assert.strictEqual(m.resolveBudget({ctx_window_tokens_opencode:"0",   ctx_window_tokens:"300000"}, "opencode"), 300000);
assert.strictEqual(m.resolveBudget({ctx_window_tokens_opencode:"-5",  ctx_window_tokens:"300000"}, "opencode"), 300000);
assert.strictEqual(m.resolveBudget({ctx_window_tokens_opencode:"abc", ctx_window_tokens:"300000"}, "opencode"), 300000);
assert.strictEqual(m.resolveBudget({ctx_window_tokens_opencode:"1.5", ctx_window_tokens:"300000"}, "opencode"), 300000);
assert.strictEqual(m.resolveBudget({ctx_window_tokens_opencode:"  ",  ctx_window_tokens:"300000"}, "opencode"), 300000);
// 모든 층 비정상 → 200000 기본.
assert.strictEqual(m.resolveBudget({ctx_window_tokens_opencode:"x", ctx_window_tokens:"y"}, "opencode"), 200000);
// 하네스 독립: opencode 키는 claude 예산에 새지 않는다 (per-harness precedence).
assert.strictEqual(m.resolveBudget({ctx_window_tokens_opencode:"500000"}, "claude"), 200000);

// (e) 분모 통일: resolveBudget 예산으로 computeCtxState 가 stop/nudge/ok 판정.
const t = {nudge_pct:30, stop_pct:20};
const lim = m.resolveBudget({ctx_window_tokens_opencode:"1000"}, "opencode");
assert.strictEqual(lim, 1000);
assert.strictEqual(m.computeCtxState(500, lim, t).level, "ok");    // 잔여 50%
assert.strictEqual(m.computeCtxState(700, lim, t).level, "nudge"); // 잔여 30% (넛지 경계·<=)
assert.strictEqual(m.computeCtxState(750, lim, t).level, "nudge"); // 잔여 25%
assert.strictEqual(m.computeCtxState(800, lim, t).level, "stop");  // 잔여 20% (정지 경계·<=)
assert.strictEqual(m.computeCtxState(850, lim, t).level, "stop");  // 잔여 15%

console.log("JS_RESOLVE_BUDGET_OK");
"""
    out = _run_node_check(script)
    assert "JS_RESOLVE_BUDGET_OK" in out, f"resolveBudget 순수 단위 실패. out={out!r}"


def test_plugin_requires_cleanly_in_node():
    """node 가 core 모듈을 깨끗이 require 한다 (문법·의존 오류 없음). node 부재 시 skip."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — require 검증 skip")

    out = _run_node_check(
        'require("./ctx-guard-core.cjs"); console.log("REQUIRE_OK");'
    )
    assert "REQUIRE_OK" in out, f"core 모듈 require 실패: {out!r}"


# ── checkpoint 모델-주입 — 정적 + node 순수 검증 ────────────────────────────


def test_plugin_injects_nudge_to_model():
    """nudge 안내를 모델 컨텍스트에 비차단 주입한다 (toast=사람 / system.transform=모델).

    chat.message 의 full Part 구성(id/sessionID/messageID 필수)보다 system[] string push 가
    안전 — experimental.chat.system.transform 채택. event(nudge)서 pendingNudgeText 세팅 →
    다음 모델 호출에 1회 소비.
    """
    src = _plugin_src()
    assert "experimental.chat.system.transform" in src, "모델 주입 훅(system.transform) 없음"
    assert "buildNudgeGuidance" in src, "nudge 안내 빌더 없음"
    assert "pendingNudgeText" in src, "nudge 주입 대기 플래그 없음"
    assert "output.system.push" in src, "system[] 에 push 하는 주입 경로 없음"
    # nudge 분기가 pendingNudgeText 를 세팅한다(toast 와 함께).
    assert "pendingNudgeText = buildNudgeGuidance" in src, "nudge 감지 시 주입 대기 세팅 누락"


def test_js_build_nudge_guidance():
    """node 로 buildNudgeGuidance 가 실제 checkpoint 명령을 포함하는지 검증.

    claude build_nudge_guidance 와 동형 문구. node 부재 시 skip(정적 검증으로 게이트).
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — JS buildNudgeGuidance 순수 단위 skip (정적 검증만 적용)")

    script = r"""
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
assert.strictEqual(typeof m.buildNudgeGuidance, "function", "missing export: buildNudgeGuidance");
const g = m.buildNudgeGuidance({ remainingPct: 18, usedPct: 82 }, { nudge_pct: 20, stop_pct: 10 });
assert.ok(g.includes("ctx-nudge"), "ctx-nudge 누락");
assert.ok(g.includes("잔여 18%"), "잔여% 누락: " + g);
assert.ok(g.includes("ticket 경계"), "ticket 경계 마무리 안내 누락");
assert.ok(g.includes("complete entry"), "complete 박제 안내 누락");
assert.ok(g.includes("python3 .project_manager/tools/pm_log.py checkpoint --task <이름>"), "실 checkpoint 명령 누락");
console.log("JS_NUDGE_GUIDANCE_OK");
"""
    out = _run_node_check(script)
    assert "JS_NUDGE_GUIDANCE_OK" in out, f"JS buildNudgeGuidance 검증 실패. out={out!r}"


# ── checkpoint 2단(strong·compaction 임박) — 정적 + node 순수 검증 ────────────
# 2단 임계는 min(stop_pct+3, nudge_pct) 파생. fired.nudge2 로 사이클당 1회·1단과 독립.


def test_plugin_injects_nudge2_to_model():
    """2단(strong) nudge 도 모델 컨텍스트에 비차단 주입 + 사람용 2단 toast (T-0328).

    1단(soft) 유지 + 2단(strong) 추가: computeCtxState 가 nudge2 레벨을 내고, event 가 그때
    pendingNudgeText 를 2단 안내로 세팅(fired.nudge2 멱등·1단과 독립)하며 사람용 toast 도 2단 표시.
    """
    src = _plugin_src()
    # 2단 임계 파생(마진 상수·nudge2Threshold) — claude CTX_NUDGE2_MARGIN_PCT 미러.
    assert re.search(r"const\s+NUDGE2_MARGIN_PCT\s*=\s*3", src), "NUDGE2_MARGIN_PCT=3 상수(claude 미러) 없음"
    assert "function nudge2Threshold" in src, "2단 임계 파생함수(nudge2Threshold) 없음"
    # computeCtxState 가 nudge2 레벨을 낸다.
    assert '"nudge2"' in src, "computeCtxState 에 nudge2 레벨 없음"
    # 2단 안내 빌더 + 감지 시 주입 대기 세팅 + fired.nudge2 멱등.
    assert "buildNudge2Guidance" in src, "2단 안내 빌더(buildNudge2Guidance) 없음"
    assert "pendingNudgeText = buildNudge2Guidance" in src, "nudge2 감지 시 주입 대기 세팅 누락"
    assert "fired.nudge2" in src, "2단 1회 가드(fired.nudge2) 없음"
    # 사람용 2단 toast.
    assert "notifyNudge2" in src, "2단 toast(notifyNudge2) 경로 없음"


def test_js_nudge2_band_and_guidance():
    """node 로 2단 임계 파생(nudge2Threshold)·4-밴드 판정(computeCtxState)·2단 안내문 검증.

    claude ctx_guard.nudge2_threshold·classify·build_nudge2_guidance 동형. node 부재 시 skip.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — 2단 nudge 순수 단위 skip (정적 검증만 적용)")

    script = r"""
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
for (const fn of ["nudge2Threshold","buildNudge2Guidance","computeCtxState"]) {
  assert.strictEqual(typeof m[fn], "function", "missing export: " + fn);
}
assert.strictEqual(m.NUDGE2_MARGIN_PCT, 3, "NUDGE2_MARGIN_PCT 미러(3) 아님");

// nudge2Threshold = min(stop_pct + 3, nudge_pct) 파생 (nudge_pct 로 캡).
assert.strictEqual(m.nudge2Threshold({nudge_pct:30, stop_pct:20}), 23);
assert.strictEqual(m.nudge2Threshold({nudge_pct:20, stop_pct:10}), 13);
assert.strictEqual(m.nudge2Threshold({nudge_pct:21, stop_pct:20}), 21); // nudge 밴드 좁으면 캡.

// computeCtxState 4-밴드 (limit 1000, 30/20 → nudge2_threshold 23).
const t = {nudge_pct:30, stop_pct:20};
assert.strictEqual(m.computeCtxState(500, 1000, t).level, "ok");     // 잔여 50%
assert.strictEqual(m.computeCtxState(700, 1000, t).level, "nudge");  // 잔여 30% (nudge 경계·<=)
assert.strictEqual(m.computeCtxState(760, 1000, t).level, "nudge");  // 잔여 24% (>23)
assert.strictEqual(m.computeCtxState(770, 1000, t).level, "nudge2"); // 잔여 23% (nudge2 경계·<=)
assert.strictEqual(m.computeCtxState(790, 1000, t).level, "nudge2"); // 잔여 21%
assert.strictEqual(m.computeCtxState(800, 1000, t).level, "stop");   // 잔여 20% (stop 경계·<=)

// buildNudge2Guidance 문구 (compaction 임박 checkpoint 지시).
const g = m.buildNudge2Guidance({remainingPct:18, usedPct:82}, {nudge_pct:20, stop_pct:10});
assert.ok(g.includes("ctx-nudge/강화"), "ctx-nudge/강화 누락: " + g);
assert.ok(g.includes("잔여 18%"), "잔여% 누락: " + g);
assert.ok(g.includes("python3 .project_manager/tools/pm_log.py checkpoint --task <이름>"), "실 checkpoint 명령 누락");
assert.ok(g.includes("구간·서사"), "checkpoint 서사 박제 지시 누락");
// 1단 문구와 구별된다 (별개 강도 표지).
const g1 = m.buildNudgeGuidance({remainingPct:18, usedPct:82}, {nudge_pct:20, stop_pct:10});
assert.ok(g !== g1, "2단 문구가 1단과 동일 — 강도 구별 없음");

console.log("JS_NUDGE2_OK");
"""
    out = _run_node_check(script)
    assert "JS_NUDGE2_OK" in out, f"JS 2단 nudge 검증 실패. out={out!r}"


# ── 5. 라이브-로드 게이트 (T-0283 · 실 opencode autoload · release-tier · 기본 skip) ──
# 이 게이트의 존재이유: 유닛/정적 테스트는 순수함수(node require)만 봐서, opencode 가 plugin export
# 형식(CJS 객체)을 거부해 *한 번도 로드 안 되던* 갭을 못 잡았다(T-0283). 실 opencode 를 헤드리스로
# 띄워 로드 성공을 실측 단언한다 — release-tier·on-demand(`PM_OPENCODE_LIVE=1`), CI/기본 regression 은
# opencode 바이너리 부재로 skip(green 불변). node 도 필요(shim→core import 는 bun 이 처리하나 skip 조건 대칭).

_OPENCODE_BIN = shutil.which("opencode")
PM_OPENCODE_LIVE = os.environ.get("PM_OPENCODE_LIVE") == "1"

# factory 가 CTX_GUARD_LOAD_PROBE 세팅 시 stderr 로 내는 로드 마커(core.cjs 와 문자열 일치).
_LOAD_MARKER = "[ctx-guard] plugin factory loaded"
# opencode 가 로드 실패 시 남기는 로그 조각(실측 1.17.18).
_LOAD_FAIL = "failed to load plugin"
_EXPORT_NOT_FN = "Plugin export is not a function"

import pytest  # noqa: E402  (라이브 게이트 데코레이터용 — 이 지점 이후만 사용)

_live_skip = pytest.mark.skipif(
    not PM_OPENCODE_LIVE or _OPENCODE_BIN is None or _NODE is None,
    reason="라이브 로드 게이트 — PM_OPENCODE_LIVE=1 + opencode/node CLI 필요(기본 skip·CI green 불변).",
)


def _run_opencode_load(project_dir: str) -> str:
    """헤드리스 opencode 를 project_dir 에서 1회 띄워 stderr+stdout(DEBUG 로그)을 반환한다.

    - CTX_GUARD_LOAD_PROBE=1 → factory 실행 시 로드 마커를 stderr 로 낸다(실 세션엔 무음).
    - 모델은 nonexistent → 플러그인 로드(모델 호출 *전* 단계) 후 즉시 실패(실 API 호출·비용 없음).
    - --dir project_dir → opencode 가 그 트리의 .opencode/ 를 로드(PWD 해석 회피·adopter 경험 미러).
    """
    env = dict(os.environ, CTX_GUARD_LOAD_PROBE="1")
    proc = subprocess.run(
        [
            _OPENCODE_BIN, "run", "--dir", project_dir,
            "--print-logs", "--log-level", "DEBUG",
            "-m", "nonexistent/model-t0283-loadgate", "noop",
        ],
        capture_output=True, text=True, timeout=180, env=env,
    )
    return (proc.stderr or "") + (proc.stdout or "")


def _stage_opencode_project(root: Path, plugin_body: str | None = None) -> Path:
    """임시 project dir 에 .opencode/{plugins,lib} 를 깐다(템플릿 트리 오염 방지 — opencode 는
    package.json 수정·node_modules 설치를 project dir 에 한다). plugin_body 지정 시 진입점만 대체
    (sensitivity 대조군용)·미지정 시 실제 출하 shim+core 를 복사한다."""
    oc = root / ".opencode"
    (oc / "plugins").mkdir(parents=True)
    (oc / "lib").mkdir(parents=True)
    shutil.copy2(CORE_FILE, oc / "lib" / "ctx-guard-core.cjs")
    if plugin_body is None:
        shutil.copy2(PLUGIN_FILE, oc / "plugins" / "ctx-guard.js")
    else:
        (oc / "plugins" / "ctx-guard.js").write_text(plugin_body, encoding="utf-8")
    return root


@_live_skip
def test_live_opencode_loads_ctx_guard_plugin():
    """실 opencode 헤드리스가 출하 ctx-guard 플러그인을 autoload 성공하는지 실측 (T-0283 핵심 게이트).

    단언: (1) "failed to load plugin" 부재 (2) "Plugin export is not a function" 부재
    (3) factory 실행 마커 존재 = 플러그인이 실제로 로드되고 팩토리가 돌았다. 유닛 green 만으론
    못 잡던 갭(실 세션 로드 실패)을 이 게이트가 닫는다.
    """
    with tempfile.TemporaryDirectory() as td:
        _stage_opencode_project(Path(td))
        log = _run_opencode_load(td)
    assert _LOAD_FAIL not in log, (
        f"opencode 가 ctx-guard 플러그인 로드 실패 (T-0283 회귀). 로그 꼬리:\n{log[-2500:]}"
    )
    assert _EXPORT_NOT_FN not in log, (
        f"export 형식 회귀 — 함수 아님. 로그 꼬리:\n{log[-2500:]}"
    )
    assert _LOAD_MARKER in log, (
        f"플러그인 factory 실행 마커 부재 — autoload 안 됨. 로그 꼬리:\n{log[-2500:]}"
    )


@_live_skip
def test_live_gate_rejects_broken_object_export():
    """sensitivity 대조군: T-0283 원 버그 형태(CJS 객체 export)를 심으면 이 게이트가 *실제로* 로드 실패를
    잡는지 실측 — false-green(무조건 통과) 방지. 대조군 진입점만 교체하고 core 는 그대로 둔다.

    이 대조군이 없으면 게이트가 opencode 로그 형식 변화 등으로 조용히 무력화돼도 초록으로 통과할 수 있다.
    """
    broken = (
        "// T-0283 원 버그 재현(대조군) — CJS 객체 export = opencode 가 함수 아니라며 로드 거부.\n"
        'const core = require("../lib/ctx-guard-core.cjs");\n'
        "module.exports = { CtxGuardPlugin: core.CtxGuardPlugin, "
        "parseLocalConf: core.parseLocalConf, NUDGE_PCT_DEFAULT: core.NUDGE_PCT_DEFAULT };\n"
    )
    with tempfile.TemporaryDirectory() as td:
        _stage_opencode_project(Path(td), plugin_body=broken)
        log = _run_opencode_load(td)
    assert _LOAD_FAIL in log or _EXPORT_NOT_FN in log, (
        "게이트 무력화 위험 — 깨진 CJS 객체 export 를 opencode 가 로드 실패로 보고하지 않았다 "
        f"(로그 형식 변화?). 로그 꼬리:\n{log[-2500:]}"
    )
    assert _LOAD_MARKER not in log, (
        f"대조군인데 factory 가 실행됨 — 로드 실패 재현 안 됨. 로그 꼬리:\n{log[-2500:]}"
    )
