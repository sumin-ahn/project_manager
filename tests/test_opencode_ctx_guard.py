"""opencode 어댑터 ctx 정지-핸드오프 plugin 정합 테스트 (T-0014·ADR-0038 D2).

opencode plugin(`.opencode/plugins/ctx-guard.js`)이 컨텍스트 토큰을 추적해 임계 도달 시
하드 정지(새 작업 도구만 차단·진행 중 핸드오프 도구는 예외 통과)하고 STOP marker 를 직접
박제(relay 회전 신호·no pm_handoff --trigger·ADR-0038 D2/D4)하며, lossy 자동 컴팩션을
차단(`compaction.auto:false`)하는 것을 여러 층위에서 단언한다:

  1. config 정합  — opencode.jsonc 에 `compaction.auto:false` (T-0012 L1).
  2. plugin 정합  — plugin 파일 존재 + 필수 호출/구조 정적 검증:
       event 토큰추적 · permission.ask deny(새 작업만) · tool.execute.before throw(allow-list) ·
       임계값(ctx_*_pct / local.conf) 참조 · 멱등 가드 · STOP marker 직접 박제.
  3. 순수 로직   — node 가 있으면 core 모듈의 결정 로직(임계 분기·sanity 폴백·토큰누적·핸드오프
       allow-list)을 이벤트/opencode 런타임 없이 자가검증. node 부재 시 skip (정적 검증만으로도 게이트).
  4. 라이브 로드 — (T-0283·release-tier·`PM_OPENCODE_LIVE=1`+opencode 바이너리) 실 opencode 헤드리스가
       플러그인을 autoload 성공(로드 실패 로그 부재 + factory 실행 마커)하는지 실측. 기본 skip(CI green 불변).

로드 규약(실측 T-0283·opencode 1.17.18): `.opencode/plugins/` 안 각 파일의 export 를 순회해 *모두 함수*
이길 요구·각각을 팩토리로 호출한다 — CJS `module.exports`(객체/단일함수)는 거부되고 비함수 export(상수)
하나로도 로드 실패. 그래서 진입점 `plugins/ctx-guard.js` 는 ESM 으로 팩토리 하나만 export 하는 얇은 shim
이고, 순수 헬퍼·상수·팩토리 본체는 plugins/ *바깥* CJS 모듈 `lib/ctx-guard-core.cjs`(opencode 미스캔·node
require 대상)에 둔다. 정적/순수 검증은 그 core 를, autoload 규약은 라이브 로드 게이트가 본다.

JS 로직 실동작(실제 deny 강제·세션 정지)은 비결정적(T-0011 메모) → opencode Pro 환경 수동 검증.
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


# ── 1. config 정합: compaction.auto false ──────────────────────────────────

def test_config_exists_and_parses():
    """opencode.jsonc 가 존재하고 jsonc 로 파싱된다."""
    assert PROJECT_CONFIG.exists(), f"project config 없음: {PROJECT_CONFIG}"
    data = _load_config()
    assert isinstance(data, dict)


def test_config_disables_auto_compaction():
    """compaction.auto 가 false — lossy 자동 컴팩션 차단(우리 정지가 먼저 오게)."""
    data = _load_config()
    assert "compaction" in data, "opencode.jsonc 에 compaction 블록 없음"
    assert data["compaction"].get("auto") is False, (
        f"compaction.auto 가 false 가 아님: {data['compaction'].get('auto')!r}"
    )


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


def test_plugin_hard_stops_via_permission_deny():
    """하드 정지 = permission.ask → status deny (T-0012 필수 정지 레버)."""
    src = _plugin_src()
    assert "permission.ask" in src, "permission.ask 훅 없음 — 하드 정지 레버 없음"
    assert re.search(r'status\s*=\s*["\']deny["\']', src), (
        "permission.ask 에서 status='deny' 설정 없음"
    )


def test_plugin_has_tool_execute_guard():
    """보조 하드 정지 = tool.execute.before throw (permission deny 우회 대비)."""
    src = _plugin_src()
    assert "tool.execute.before" in src, "보조 정지(tool.execute.before) 없음"


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
    """넛지·정지·트리거는 세션당 1회만 — 멱등 가드(중복 handoff 방지·codex 인계)."""
    src = _plugin_src()
    # fired 상태 객체로 1회 가드 (nudge/stop 각 1회).
    assert re.search(r"fired", src), "멱등 상태 플래그(fired) 없음"
    assert "fired.stop" in src and "fired.nudge" in src, (
        "정지/넛지 1회 가드(fired.stop/fired.nudge) 없음"
    )


def test_plugin_emits_nudge():
    """넛지(이른 경고) 경로가 있다 — nudge_pct 초과 시 안내(toast/message)."""
    src = _plugin_src()
    assert "nudge" in src.lower(), "넛지 로직 없음"
    # toast 경고 경로 (best-effort).
    assert "showToast" in src or "toast" in src.lower(), "넛지 안내(toast) 경로 없음"


# ── 2b. STOP sentinel marker write (ADR-0009 · T-0048) 정적 검증 ─────────────

def test_plugin_writes_stop_marker_path():
    """정지 시 STOP marker 를 claude ctx_stop_hook 와 동일 경로/내용으로 쓴다 (ADR-0009 sentinel).

    relay(pm_orch_opencode.py)가 이 marker 를 stat 해 세션 회전을 트리거한다 —
    경로 `.project_manager/.local/ctx-stop/<sanitize(sid)>.done`·내용 동일이어야 같은
    Supervisor 코드가 양 하니스를 구동한다(엔진 무변경·ADR-0009 핵심 불변식).
    """
    src = _plugin_src()
    # marker 디렉토리 규약 (claude _MARKER_DIR·엔진 MARKER_DIR 미러).
    assert "ctx-stop" in src, "STOP marker 디렉토리(ctx-stop) 참조 없음"
    assert ".local" in src, "marker 경로(.local) 참조 없음"
    assert ".done" in src, "marker 파일 확장자(.done) 없음"
    # marker 내용 = claude hook 과 동일 문자열.
    assert "ctx-stop handoff triggered" in src, "marker 내용(claude hook 동일) 없음"
    # 실제 파일 write 경로 (fs.writeFileSync).
    assert "writeFileSync" in src, "marker 파일 write(fs.writeFileSync) 없음"
    # mkdir -p (recursive) — marker 디렉토리 자동 생성(claude hook 의 mkdir parents 동치).
    assert re.search(r"mkdirSync\([^)]*recursive", src), (
        "marker 디렉토리 recursive mkdir 없음(claude hook mkdir parents 동치)"
    )


def test_plugin_marker_uses_session_id_from_event():
    """marker 파일명용 sid 를 event hook 의 info.sessionID 로 캡처한다(PluginInput 엔 sid 없음·실측)."""
    src = _plugin_src()
    assert "info.sessionID" in src, "event hook 의 info.sessionID 캡처 없음"
    assert "sanitizeSessionId" in src, "marker 파일명 sanitize 호출 없음"


def test_plugin_marker_fail_soft_and_sid_guard():
    """marker write 는 fail-soft(정지 유지) + sid 미상 시 skip+경고(침묵 금지)."""
    src = _plugin_src()
    # sid 미상 경고 (stderr) — silent-fail 방어.
    assert "stderr" in src, "sid 미상 경고(stderr) 없음 — 침묵 금지 위반"
    # STOP marker write 함수 존재 (event 정지 경로에서 직접 호출·ADR-0038 D2).
    assert "writeStopMarker" in src, "STOP marker write 함수 없음"


def test_plugin_marker_written_unconditionally_on_stop():
    """STOP marker 는 정지 감지 시 무조건 박제된다 — pm_handoff --trigger·handoffRc 게이트 없음
    (ADR-0038 D2/D4: relay 회전은 marker 로만 신호·엔진 --trigger spawn 폐기).

    writeStopMarker() 호출이 event(message.updated) 정지 분기의 `fired.stop = true` 와 나란히
    (co-located·무조건) 온다. 어떤 handoffRc/--trigger 게이트도 재도입되지 않았음을 함께 단언한다.
    """
    src = _plugin_src()
    # --trigger spawn·handoffRc 게이트가 완전히 사라졌다 (ADR-0038 회귀 방지).
    # (설명 주석에 "no --trigger"·findEngineRoot 의 pm_handoff.py *경로 probe* 는 남으므로
    #  substring 이 아니라 실 배선 토큰[spawn 함수·rc 게이트]으로 부재를 단언한다.)
    assert "triggerHandoff" not in src, "triggerHandoff spawn 함수 재도입 (ADR-0038 D4 위반)"
    assert "handoffRc" not in src, "handoffRc 게이트 재도입 (ADR-0038 D2 위반·marker 무조건이어야)"
    assert not re.search(r"spawnSync\s*\(", src), (
        "pm_handoff --trigger spawnSync() 호출 재도입 (ADR-0038 D4 위반·marker 로만 신호)"
    )
    # fired.stop = true 세팅 직후 writeStopMarker() 무조건 호출 (co-located).
    idx_fired = src.find("fired.stop = true")
    idx_call = src.find("writeStopMarker()", idx_fired)
    assert idx_fired != -1, "정지 분기(fired.stop = true) 없음"
    assert idx_call != -1 and idx_call > idx_fired, (
        "writeStopMarker() 가 정지 분기(fired.stop = true) 직후에 무조건 호출되지 않음"
    )


# ── 3b. node 순수 검증: JS sanitizeSessionId == _session_id 규칙 동치 ─────────

def test_js_sanitize_matches_hook_session_id_rule():
    """node 로 JS sanitizeSessionId 가 claude `_session_id` 규칙(ticket 정의)과 동치임을 검증.

    규칙: `[A-Za-z0-9]`/`-`/`_` 만 남기고 `.slice(0,64)`·빈→"unknown". opencode sid 는 ASCII
    (`ses_...`·실측)라 marker 예측이 양 하니스에서 일치한다(driver-파싱 sid == plugin-write sid
    의 sanitize 동일성 = 핵심 가정·silent-fail 방어). node 부재 시 skip(정적 검증으로 게이트).

    NOTE(실측·보고): Python `_session_id` 는 `str.isalnum()` 을 써 유니코드 문자(한글·악센트)도
    보존하나 JS `[A-Za-z0-9]` 는 ASCII 만 매칭 — 유니코드 입력에서만 갈린다. opencode sid 는
    ASCII 전용이라 실무상 무관(아래 케이스는 ASCII·경계 규칙만 검증).
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — JS sanitize 동치 검증 skip (정적 검증만 적용)")

    script = r"""
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
assert.strictEqual(typeof m.sanitizeSessionId, "function", "missing export: sanitizeSessionId");

// 실 opencode sid (ASCII·`ses_<base62>`) 는 그대로 보존(`_` 안전 문자).
assert.strictEqual(m.sanitizeSessionId("ses_1326c468affeQKslN5GD1FbHRR"), "ses_1326c468affeQKslN5GD1FbHRR");
// 특수문자 제거 (path traversal 방어 — `/`·`.` drop).
assert.strictEqual(m.sanitizeSessionId("a/b"), "ab");
assert.strictEqual(m.sanitizeSessionId("../../etc/passwd"), "etcpasswd");
// uuid4 형태 보존 (하이픈 안전).
assert.strictEqual(m.sanitizeSessionId("11111111-2222-3333-4444-555555555555"), "11111111-2222-3333-4444-555555555555");
// 빈 / 공백 / 비-문자열 → "unknown" (hook 폴백 동치).
assert.strictEqual(m.sanitizeSessionId("  "), "unknown");
assert.strictEqual(m.sanitizeSessionId(""), "unknown");
assert.strictEqual(m.sanitizeSessionId(null), "unknown");
assert.strictEqual(m.sanitizeSessionId(undefined), "unknown");
// 64자 초과 → 절단.
assert.strictEqual(m.sanitizeSessionId("z".repeat(100)).length, 64);
assert.strictEqual(m.sanitizeSessionId("z".repeat(100)), "z".repeat(64));

console.log("JS_SANITIZE_EQUIV_OK");
"""
    out = _run_node_check(script)
    assert "JS_SANITIZE_EQUIV_OK" in out, f"JS sanitize 동치 검증 실패. out={out!r}"


def test_js_sanitize_equiv_python_on_ascii():
    """ASCII 입력에서 JS sanitizeSessionId == Python _sanitize_session_id 결과 동일(cross-check).

    양 하니스가 각자 자기 sid 를 sanitize 하므로(claude→claude sid·opencode→opencode sid),
    ASCII sid 에서 두 구현이 같은 marker 파일명을 내야 supervisor 의 marker 예측이 성립한다.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — JS/Python sanitize cross-check skip")

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pm_relay", REPO / ".project_manager" / "tools" / "pm_relay.py"
    )
    orch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orch)

    cases = [
        "ses_1326c468affeQKslN5GD1FbHRR",
        "a/b",
        "../../etc/passwd",
        "11111111-2222-3333-4444-555555555555",
        "  ",
        "",
        "z" * 100,
        "ses_With.Dots-and_Under",
    ]
    # JS 결과를 node 로 일괄 산출.
    js_script = (
        'const m=require("./ctx-guard-core.cjs");'
        "const cs=" + json.dumps(cases) + ";"
        'console.log(JSON.stringify(cs.map(c=>m.sanitizeSessionId(c))));'
    )
    js_out = _run_node_check(js_script).strip()
    js_results = json.loads(js_out)
    py_results = [orch._sanitize_session_id(c) for c in cases]
    assert js_results == py_results, (
        f"ASCII sanitize 불일치 — JS={js_results} Python={py_results}"
    )


# ── 3. 순수 결정 로직 자가검증 (node 있으면) ─────────────────────────────────

_NODE = shutil.which("node")


def _run_node_check(script: str) -> str:
    # cwd = core lib 디렉토리 → 스크립트의 `require("./ctx-guard-core.cjs")` 가 해소된다.
    return subprocess.run(
        [_NODE, "-e", script],
        cwd=str(CORE_DIR),
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


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


# ── 4. 핸드오프 도구 allow-list (ADR-0038 D2 — claude _is_handoff_* 미러) ─────
# hard-stop 중 진행 중인 rich /pm-handoff 도구는 통과·그 외 새 작업은 정지. tool.execute.before
# (input.tool + output.args)가 authoritative gate·permission.ask(Permission best-effort)는 fail-open 보조.


def test_plugin_has_handoff_allowlist_hooks():
    """정지 후 hard-stop 이 새 작업만 deny 하고 핸드오프 도구는 통과시키는 allow-list 배선 (정적)."""
    src = _plugin_src()
    # permission.ask 는 확실한 새 작업만 deny (isNewWorkPermission 게이트).
    assert "isNewWorkPermission" in src, "permission.ask 의 새 작업 판정(isNewWorkPermission) 없음"
    # tool.execute.before 는 핸드오프 도구가 아니면 throw (authoritative allow-list gate).
    assert "isHandoffTool" in src, "tool.execute.before 의 allow-list 판정(isHandoffTool) 없음"
    assert re.search(r"fired\.stop\s*&&\s*!isHandoffTool", src), (
        "tool.execute.before 가 (fired.stop && !isHandoffTool) throw 형태가 아님"
    )
    assert re.search(r"fired\.stop\s*&&\s*isNewWorkPermission", src), (
        "permission.ask 가 (fired.stop && isNewWorkPermission) deny 형태가 아님"
    )


def test_js_handoff_allowlist_pure_unit():
    """node 로 핸드오프 allow-list 순수 함수(isHandoffBash/Target/Tool/isNewWorkPermission) 검증.

    claude ctx_stop_hook._is_handoff_* 미러 — 핸드오프 도구는 통과(true/allow)·새 작업은 정지
    (false/deny)·셸 연쇄 밀반입은 fail-closed·Permission 추출불가는 fail-open. node 부재 시 skip.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — 핸드오프 allow-list 순수 단위 skip (정적 검증만 적용)")

    script = r"""
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
for (const fn of ["isHandoffBash","isHandoffTarget","isHandoffTool","isNewWorkPermission"]) {
  assert.strictEqual(typeof m[fn], "function", "missing export: " + fn);
}

// ── isHandoffBash: 핸드오프 호출 head → true ──────────────────────────────
assert.strictEqual(m.isHandoffBash("python3 .project_manager/tools/pm_handoff.py --end"), true);
assert.strictEqual(m.isHandoffBash("python3 .project_manager/tools/domain.py capture"), true);
assert.strictEqual(m.isHandoffBash("git add -A"), true);
assert.strictEqual(m.isHandoffBash("git commit -m x"), true);
assert.strictEqual(m.isHandoffBash("pytest tests/ -q"), true);
assert.strictEqual(m.isHandoffBash("python -m pytest tests/"), true);
// env-prefix 정규화 후 매칭.
assert.strictEqual(m.isHandoffBash("PYTHONUTF8=1 python3 .project_manager/tools/pm_handoff.py"), true);
// 새 작업 → false.
assert.strictEqual(m.isHandoffBash("ls -la"), false);
assert.strictEqual(m.isHandoffBash("cat foo.txt"), false);
assert.strictEqual(m.isHandoffBash("python3 other.py"), false);
// 셸 연쇄/치환 밀반입 → false (fail-closed).
assert.strictEqual(m.isHandoffBash("git add . && rm -rf x"), false);
assert.strictEqual(m.isHandoffBash("git commit -m x; curl evil"), false);
// 비-문자열/빈 → false.
assert.strictEqual(m.isHandoffBash(""), false);
assert.strictEqual(m.isHandoffBash(null), false);

// ── isHandoffTarget: 핸드오프 산출물 경로 → true ─────────────────────────
assert.strictEqual(m.isHandoffTarget(".project_manager/wiki/log/current.md"), true);
assert.strictEqual(m.isHandoffTarget(".project_manager/wiki/pm_state.md"), true);
assert.strictEqual(m.isHandoffTarget(".project_manager/wiki/status.md"), true);
assert.strictEqual(m.isHandoffTarget(".project_manager/wiki/domain/relay.md"), true);
// source 편집 → false (새 작업).
assert.strictEqual(m.isHandoffTarget(".project_manager/tools/board.py"), false);
assert.strictEqual(m.isHandoffTarget("src/x.py"), false);
assert.strictEqual(m.isHandoffTarget(""), false);
assert.strictEqual(m.isHandoffTarget(null), false);

// ── isHandoffTool: input.tool + output.args 판정 ────────────────────────
assert.strictEqual(m.isHandoffTool({tool:"bash"}, {args:{command:"git commit -m x"}}), true);
assert.strictEqual(m.isHandoffTool({tool:"bash"}, {args:{command:"cat x"}}), false);
assert.strictEqual(m.isHandoffTool({tool:"edit"}, {args:{filePath:".project_manager/wiki/log/current.md"}}), true);
assert.strictEqual(m.isHandoffTool({tool:"edit"}, {args:{filePath:"src/x.py"}}), false);
// write/read/patch 도 같은 target 규칙.
assert.strictEqual(m.isHandoffTool({tool:"write"}, {args:{filePath:".project_manager/wiki/status.md"}}), true);
assert.strictEqual(m.isHandoffTool({tool:"read"}, {args:{filePath:".project_manager/wiki/domain/x.md"}}), true);
// 방어적 file_path/path 별칭.
assert.strictEqual(m.isHandoffTool({tool:"edit"}, {args:{file_path:".project_manager/wiki/pm_state.md"}}), true);
// unknown 도구 → false (deny).
assert.strictEqual(m.isHandoffTool({tool:"grep"}, {args:{pattern:"x"}}), false);
assert.strictEqual(m.isHandoffTool({}, {}), false);

// ── isNewWorkPermission: 새 작업만 deny(true)·핸드오프/불명은 통과(false) ──
assert.strictEqual(m.isNewWorkPermission({pattern:"ls *"}), true);        // 새 작업 → deny
assert.strictEqual(m.isNewWorkPermission({pattern:"git commit *"}), false); // 핸드오프 → allow
assert.strictEqual(m.isNewWorkPermission({}), false);                     // 추출불가 → fail-open
assert.strictEqual(m.isNewWorkPermission({pattern:undefined}), false);    // 추출불가 → fail-open
assert.strictEqual(m.isNewWorkPermission(null), false);                   // 비객체 → fail-open
// 후보 하나라도 핸드오프면 통과(false). title 은 anchored 매칭 — 핸드오프 head 로 *시작*해야.
assert.strictEqual(m.isNewWorkPermission({title:"git add -A"}), false);
// title 이 핸드오프 head 로 시작 안 하면(단순 포함) 매칭 안 됨 → 새 작업(true·deny).
assert.strictEqual(m.isNewWorkPermission({title:"Run git add -A"}), true);
// pattern 배열·metadata 값도 후보.
assert.strictEqual(m.isNewWorkPermission({pattern:["rm *","ls *"]}), true);
assert.strictEqual(m.isNewWorkPermission({metadata:{cmd:"pytest tests/"}}), false);

console.log("JS_HANDOFF_ALLOWLIST_OK");
"""
    out = _run_node_check(script)
    assert "JS_HANDOFF_ALLOWLIST_OK" in out, f"핸드오프 allow-list 순수 단위 실패. out={out!r}"


# ── graceful nudge 모델-주입 (ADR-0037) — 정적 + node 순수 검증 ────────────────


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
    """node 로 buildNudgeGuidance 가 조건부 안내문(/pm-handoff·ADR-0037)을 만드는지 검증.

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
assert.ok(g.includes("/pm-handoff"), "/pm-handoff 누락");
assert.ok(g.includes("10%"), "stop_pct 안내 누락");
assert.ok(g.includes("ADR-0037"), "ADR-0037 누락");
console.log("JS_NUDGE_GUIDANCE_OK");
"""
    out = _run_node_check(script)
    assert "JS_NUDGE_GUIDANCE_OK" in out, f"JS buildNudgeGuidance 검증 실패. out={out!r}"


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
