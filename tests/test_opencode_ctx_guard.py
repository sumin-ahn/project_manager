"""opencode 어댑터 ctx 정지-핸드오프 plugin 정합 테스트 (T-0014).

opencode plugin(`.opencode/plugins/ctx-guard.js`)이 컨텍스트 토큰을 추적해 임계 도달 시
정지(permission deny)·자동 handoff(엔진 T-0013 트리거)하고, lossy 자동 컴팩션을
차단(`compaction.auto:false`)하는 것을 두 층위에서 단언한다:

  1. config 정합  — opencode.jsonc 에 `compaction.auto:false` (T-0012 L1).
  2. plugin 정합  — plugin 파일 존재 + 필수 호출/구조 정적 검증:
       event 토큰추적 · permission.ask deny · tool.execute.before throw ·
       pm_handoff --trigger shell-out · 임계값(ctx_*_pct / local.conf) 참조 · 멱등 가드.
  3. 순수 로직   — node 가 있으면 plugin 의 결정 로직(임계 분기·sanity 폴백·토큰누적)을
       이벤트/opencode 런타임 없이 자가검증. node 부재 시 skip (정적 검증만으로도 게이트).

JS 로직 실동작(실제 deny 강제·세션 정지)은 비결정적(T-0011 메모) → opencode Pro 환경
수동 검증. 여기선 결정적 정적/순수 검증만. stdlib(+ 선택적 node)만 사용 — opencode CLI 미실행.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OPENCODE = REPO / "templates" / "opencode" / ".opencode"

PROJECT_CONFIG = OPENCODE / "opencode.jsonc"
PLUGIN_DIR = OPENCODE / "plugins"
PLUGIN_FILE = PLUGIN_DIR / "ctx-guard.js"


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
    """plugin 단일 파일이 .opencode/plugins/ 에 존재 (opencode autoload 위치)."""
    assert PLUGIN_DIR.is_dir(), f"plugin 디렉토리 없음: {PLUGIN_DIR}"
    assert PLUGIN_FILE.exists(), f"ctx-guard plugin 없음: {PLUGIN_FILE}"


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


def test_plugin_triggers_pm_handoff():
    """정지 시 엔진 T-0013 핸드오프를 shell-out 트리거한다 (pm_handoff --trigger)."""
    src = _plugin_src()
    assert "pm_handoff.py" in src, "pm_handoff.py shell-out 없음"
    assert "--trigger" in src, "pm_handoff --trigger 플래그 없음"
    assert "--reason" in src and "ctx-stop" in src, "trigger reason(ctx-stop) 없음"
    assert "--ctx-pct" in src, "--ctx-pct 전달 없음 (handoff entry 기록)"


def test_plugin_reads_thresholds_from_local_conf():
    """임계값을 엔진 local.conf 의 ctx_*_pct 에서 읽는다 (T-0013 계약)."""
    src = _plugin_src()
    assert "ctx_nudge_pct" in src, "ctx_nudge_pct 임계 참조 없음"
    assert "ctx_stop_pct" in src, "ctx_stop_pct 임계 참조 없음"
    assert "local.conf" in src, "local.conf 직접 파싱 경로 참조 없음"


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
    # writeStopMarker 가 triggerHandoff 에서 호출됨 (handoff 직후 marker).
    assert "writeStopMarker" in src, "STOP marker write 함수 없음"


def test_plugin_marker_gated_on_handoff_success():
    """STOP marker 는 pm_handoff --trigger 성공(rc 0) 시에만 — 실패면 새 PM 이 권위 handoff
    없이 stale context 로 부트스트랩하므로 회전 금지(claude ctx_stop_hook handoff_rc==0 선례·
    codex T-0048 must-fix). 정지(deny)는 rc 무관 유지."""
    src = _plugin_src()
    # handoff 종료코드(status)를 포착해 rc 0 게이트.
    assert ".status" in src, "pm_handoff --trigger 종료코드(res.status) 미포착"
    assert "handoffRc === 0" in src, "marker 가 handoff rc 0 게이트 안 됨 (실패 시 stale 회전 위험)"
    # writeStopMarker 호출이 rc 0 게이트 *뒤* 에 온다 (무조건 호출 아님).
    idx_rc = src.find("handoffRc === 0")
    idx_call = src.find("writeStopMarker()", idx_rc)
    assert idx_rc != -1 and idx_call != -1 and idx_call > idx_rc, (
        "writeStopMarker 가 handoff rc 0 게이트 밖에서 호출됨 (codex T-0048)"
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
const m = require("./ctx-guard.js");
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
        'const m=require("./ctx-guard.js");'
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
    return subprocess.run(
        [_NODE, "-e", script],
        cwd=str(PLUGIN_DIR),
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
const m = require("./ctx-guard.js");
const assert = require("node:assert");

// export 표면 (테스트 가능한 순수 함수가 떼어져 있어야 한다).
for (const fn of ["parseLocalConf","resolveThresholds","accumulateTokens","computeCtxState","CtxGuardPlugin"]) {
  assert.strictEqual(typeof m[fn], "function", "missing export: " + fn);
}

// 임계 해석 + sanity 폴백 (엔진 기본 20/10).
assert.deepStrictEqual(m.resolveThresholds({}), {nudge_pct:20, stop_pct:10});                       // 미설정→기본
assert.deepStrictEqual(m.resolveThresholds({ctx_nudge_pct:"25",ctx_stop_pct:"12"}), {nudge_pct:25, stop_pct:12});
assert.deepStrictEqual(m.resolveThresholds({ctx_nudge_pct:"5",ctx_stop_pct:"30"}), {nudge_pct:20, stop_pct:10}); // stop>nudge→폴백
assert.deepStrictEqual(m.resolveThresholds({ctx_nudge_pct:"-5",ctx_stop_pct:"3"}), {nudge_pct:20, stop_pct:10}); // 음수→폴백

// 토큰 누적.
assert.strictEqual(m.accumulateTokens({input:100,output:20,reasoning:5,cache:{read:10,write:3}}), 138);
assert.strictEqual(m.accumulateTokens(null), 0);

// ctx 상태 판정 (limit 1000, 기본 20/10 = 잔여% 임계).
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


def test_plugin_requires_cleanly_in_node():
    """node 가 plugin 을 깨끗이 require 한다 (문법·의존 오류 없음). node 부재 시 skip."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — require 검증 skip")

    out = _run_node_check(
        'require("./ctx-guard.js"); console.log("REQUIRE_OK");'
    )
    assert "REQUIRE_OK" in out, f"plugin require 실패: {out!r}"


# ── 4. thread-tail 추출 (T-0050 — claude extract_thread_tail 미러) ───────────

def test_plugin_wires_thread_tail_into_handoff():
    """triggerHandoff 가 정지 시 opencode export → extractThreadTail → --thread-tail 배선한다.

    claude `ctx_stop_hook.run_handoff` 가 `--thread-tail` 을 배선하는 패턴의 opencode 동치.
    정적검증: 추출/배선 토큰이 plugin 소스에 존재 (실 opencode 무호출). 빈/미상이면 미추가
    (하위호환)는 node 단위 테스트(아래 runner DI)에서 검증.
    """
    src = _plugin_src()
    # 추출·배선 토큰 — pm_handoff 에 thread-tail 전달.
    assert "--thread-tail" in src, "pm_handoff --thread-tail 플래그 배선 없음"
    assert "extractThreadTail" in src, "thread-tail 추출 함수(extractThreadTail) 없음"
    assert "exportSessionMessages" in src, "세션 transcript 추출(exportSessionMessages) 없음"
    # opencode export shell-out (--pure 로 plugin 재진입 방지).
    assert "opencode" in src, "opencode export shell-out 없음"
    assert '"export"' in src or "'export'" in src, "export 서브커맨드 없음"
    assert "--pure" in src, "--pure 플래그 없음 (plugin 재진입 방지)"
    # 상수 (claude 미러).
    assert "THREAD_TAIL_MAX_TURNS" in src, "THREAD_TAIL_MAX_TURNS 상수 없음"
    assert "THREAD_TAIL_MAX_CHARS" in src, "THREAD_TAIL_MAX_CHARS 상수 없음"


def test_plugin_thread_tail_backward_compat_omits_flag():
    """threadTail 이 비어있으면 cmd 에 --thread-tail 미추가 (claude run_handoff 동치·하위호환).

    정적검증: --thread-tail push 가 `if (threadTail)` 게이트 안에 있다 (무조건 추가 아님).
    """
    src = _plugin_src()
    # threadTail 진릿값 게이트가 --thread-tail push 앞에 온다.
    idx_gate = src.find("if (threadTail)")
    idx_push = src.find('"--thread-tail"', idx_gate)
    assert idx_gate != -1, "threadTail 진릿값 게이트(if (threadTail)) 없음 — 하위호환 깨짐"
    assert idx_push != -1 and idx_push > idx_gate, (
        "--thread-tail push 가 threadTail 게이트 밖 (빈/미상에도 추가됨 — 엔진 placeholder 파손)"
    )


def test_plugin_thread_tail_marker_gate_unchanged():
    """thread-tail 배선이 marker 게이트(handoffRc === 0)·멱등 로직을 깨지 않는다 (회귀 방지)."""
    src = _plugin_src()
    # handoff rc 0 게이트 뒤에 writeStopMarker (T-0048 불변식 — thread-tail 배선이 손대면 안 됨).
    idx_rc = src.find("handoffRc === 0")
    idx_call = src.find("writeStopMarker()", idx_rc)
    assert idx_rc != -1 and idx_call != -1 and idx_call > idx_rc, (
        "thread-tail 배선이 marker rc 0 게이트를 깼다 (T-0048 회귀)"
    )
    # 멱등 가드 보존.
    assert "fired.stop" in src, "정지 멱등 가드(fired.stop) 손실 (회귀)"


def test_js_extract_thread_tail_pure_unit():
    """node 로 JS extractThreadTail 순수 동작 검증 — user-only·N턴 캡·text-part 0 turn 제외·
    개행 평탄화·max_chars 캡·빈 입력 fail-soft. node 부재 시 skip (정적 검증으로 게이트)."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — JS extractThreadTail 순수 단위 skip (정적 검증만)")

    script = r"""
const m = require("./ctx-guard.js");
const assert = require("node:assert");
assert.strictEqual(typeof m.extractThreadTail, "function", "missing export: extractThreadTail");
assert.strictEqual(typeof m.exportSessionMessages, "function", "missing export: exportSessionMessages");
assert.strictEqual(m.THREAD_TAIL_MAX_TURNS, 3);
assert.strictEqual(m.THREAD_TAIL_MAX_CHARS, 600);

// helper: opencode export message ({ info:{role}, parts:[{type:"text",text}] }).
const mk = (role, ...texts) => ({ info: { role }, parts: texts.map((t) => ({ type: "text", text: t })) });

// user-only·역순→시간순 복원 (assistant 제외).
assert.strictEqual(
  m.extractThreadTail([mk("user","첫 요청"), mk("assistant","응답 제외"), mk("user","두 번째 요청"), mk("assistant","또"), mk("user","마지막 요청")]),
  "첫 요청 / 두 번째 요청 / 마지막 요청");

// N턴 초과 시 최근 3 (max_turns).
assert.strictEqual(
  m.extractThreadTail([mk("user","t1"),mk("user","t2"),mk("user","t3"),mk("user","t4"),mk("user","t5")], 3),
  "t3 / t4 / t5");

// text part 0개 turn 제외 (tool_result·synthetic-only 동치).
assert.strictEqual(
  m.extractThreadTail([mk("user","진짜 발화"), { info:{role:"user"}, parts:[{ type:"tool", text:"무시" }] }]),
  "진짜 발화");

// 비-text part 섞여도 text part 만 수집.
assert.strictEqual(
  m.extractThreadTail([{ info:{role:"user"}, parts:[{ type:"step-start" }, { type:"text", text:"블록 텍스트" }] }]),
  "블록 텍스트");

// 개행 ` / ` 평탄화.
assert.strictEqual(m.extractThreadTail([mk("user","첫 줄\n둘째 줄\n셋째 줄")]), "첫 줄 / 둘째 줄 / 셋째 줄");

// 총 max_chars 캡.
assert.ok(m.extractThreadTail([mk("user","가".repeat(50)), mk("user","나".repeat(50))], 3, 30).length <= 30);

// fail-soft: 빈/비배열/assistant-only/max<=0 → "".
assert.strictEqual(m.extractThreadTail([]), "");
assert.strictEqual(m.extractThreadTail(null), "");
assert.strictEqual(m.extractThreadTail([mk("assistant","응답만")]), "");
assert.strictEqual(m.extractThreadTail([mk("user","x")], 0), "");
assert.strictEqual(m.extractThreadTail([mk("user","x")], 3, 0), "");

console.log("JS_THREAD_TAIL_OK");
"""
    out = _run_node_check(script)
    assert "JS_THREAD_TAIL_OK" in out, f"JS extractThreadTail 순수 단위 실패. out={out!r}"


def test_js_export_session_messages_fail_soft_unit():
    """node 로 exportSessionMessages fail-soft 검증 (runner DI — 실 opencode 무호출).

    spawn throw·비-JSON·messages 비배열·!root·sid 미상 → [] (fail-soft). 정상 JSON →
    .messages 배열. node 부재 시 skip.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — exportSessionMessages fail-soft 단위 skip")

    script = r"""
const m = require("./ctx-guard.js");
const assert = require("node:assert");

// spawn throw → [] (try/catch fail-soft).
assert.deepStrictEqual(m.exportSessionMessages("ses_x", "/root", () => { throw new Error("boom"); }), []);
// 비-JSON stdout → [].
assert.deepStrictEqual(m.exportSessionMessages("ses_x", "/root", () => ({ stdout: "not json" })), []);
// stdout 미상(undefined) → [].
assert.deepStrictEqual(m.exportSessionMessages("ses_x", "/root", () => ({})), []);
// messages 비배열 → [].
assert.deepStrictEqual(m.exportSessionMessages("ses_x", "/root", () => ({ stdout: JSON.stringify({ messages: "x" }) })), []);
// sid 미상 → [] (runner 미호출).
let called = false;
assert.deepStrictEqual(m.exportSessionMessages("", "/root", () => { called = true; return { stdout: "{}" }; }), []);
assert.strictEqual(called, false, "sid 미상인데 runner 호출됨 (불필요 shell-out)");
// !root → [].
assert.deepStrictEqual(m.exportSessionMessages("ses_x", null, () => ({ stdout: "{}" })), []);
// 정상 → .messages 배열 반환.
const ok = m.exportSessionMessages("ses_x", "/root", () => ({ stdout: JSON.stringify({ messages: [{ info: { role: "user" }, parts: [] }] }) }));
assert.strictEqual(Array.isArray(ok), true);
assert.strictEqual(ok.length, 1);
assert.strictEqual(ok[0].info.role, "user");
// runner 가 opencode export <sid> --pure 로 호출되는지.
let capturedCmd = null, capturedArgs = null, capturedOpts = null;
m.exportSessionMessages("ses_abc", "/root", (cmd, args, opts) => {
  capturedCmd = cmd; capturedArgs = args; capturedOpts = opts;
  return { stdout: JSON.stringify({ messages: [] }) };
});
assert.strictEqual(capturedCmd, "opencode");
assert.deepStrictEqual(capturedArgs, ["export", "ses_abc", "--pure"]);
assert.strictEqual(capturedOpts.cwd, "/root");
assert.strictEqual(capturedOpts.encoding, "utf-8");
// maxBuffer 명시 — ctx-STOP 시점 export JSON 은 기본 1 MiB 를 넘기 쉬워 잘리면 thread-tail 이
// 조용히 무력화된다(codex T-0050 must-fix). 기본(~1 MiB)보다 충분히 커야 한다.
assert.ok(capturedOpts.maxBuffer >= 16 * 1024 * 1024,
  "exportSessionMessages must set explicit maxBuffer >= 16 MiB (default ~1 MiB truncates near-full export)");

console.log("JS_EXPORT_FAILSOFT_OK");
"""
    out = _run_node_check(script)
    assert "JS_EXPORT_FAILSOFT_OK" in out, f"exportSessionMessages fail-soft 단위 실패. out={out!r}"


def _claude_extract_thread_tail(transcript_messages, max_turns=3, max_chars=600) -> str:
    """claude `extract_thread_tail` 을 importlib 로 로드해 transcript fixture 로 실행.

    transcript_messages = [(role, content), ...] (content 는 str 또는 block list).
    동치 검증의 reference 측 — JS 와 같은 논리 시나리오를 claude transcript 포맷으로 돌린다.
    """
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "claude_ctx_guard_equiv",
        REPO / "templates" / "claude_code" / ".claude" / "ctx_guard.py",
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    lines = []
    for role, content in transcript_messages:
        lines.append(json.dumps({"type": role, "message": {"role": role, "content": content}}))
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", encoding="utf-8", delete=False
    ) as fh:
        fh.write("\n".join(lines) + "\n")
        path = fh.name
    try:
        return guard.extract_thread_tail(path, max_turns=max_turns, max_chars=max_chars)
    finally:
        Path(path).unlink(missing_ok=True)


def test_js_extract_thread_tail_equiv_claude():
    """JS extractThreadTail 가 claude extract_thread_tail 규칙과 동치 (공통 시나리오).

    같은 논리 대화를 양 포맷으로 구성 — claude transcript(content str/block) vs opencode
    export(parts text) — 그 결과 문자열이 일치해야 한다. user-only·N턴 초과 최근 3·turn
    truncate·text-part 0 turn 제외·개행 평탄화·빈 입력. node 부재 시 skip(정적 게이트).
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — JS/claude 동치 검증 skip (정적 검증으로 게이트)")

    # 공통 논리 시나리오: [(role, [발화 텍스트 part 들], claude_content)].
    # JS 입력은 parts(text) 로, claude 입력은 content(str|block) 로 같은 의미를 표현.
    # 각 케이스: (label, claude_transcript, js_messages, max_turns, max_chars).
    cases = [
        (
            "user-only-chronological",
            [("user", "첫 요청"), ("assistant", "응답"), ("user", "두 번째"), ("user", "마지막")],
            [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "첫 요청"}]},
                {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "응답"}]},
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "두 번째"}]},
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "마지막"}]},
            ],
            3,
            600,
        ),
        (
            "max-turns-recent-3",
            [("user", "t1"), ("user", "t2"), ("user", "t3"), ("user", "t4"), ("user", "t5")],
            [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": f"t{i}"}]}
                for i in range(1, 6)
            ],
            3,
            600,
        ),
        (
            "exclude-text-0-turn",
            [
                ("user", "진짜 발화"),
                ("user", [{"type": "tool_result", "tool_use_id": "x", "content": "도구 결과"}]),
            ],
            [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "진짜 발화"}]},
                {"info": {"role": "user"}, "parts": [{"type": "tool", "text": "도구 결과"}]},
            ],
            3,
            600,
        ),
        (
            "newline-flatten",
            [("user", "첫 줄\n둘째 줄\n셋째 줄")],
            [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "첫 줄\n둘째 줄\n셋째 줄"}]}],
            3,
            600,
        ),
        (
            "empty-no-user",
            [("assistant", "응답만")],
            [{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "응답만"}]}],
            3,
            600,
        ),
    ]

    for label, claude_tx, js_messages, mt, mc in cases:
        claude_result = _claude_extract_thread_tail(claude_tx, max_turns=mt, max_chars=mc)
        js_script = (
            'const m=require("./ctx-guard.js");'
            "const msgs=" + json.dumps(js_messages) + ";"
            f"console.log(JSON.stringify(m.extractThreadTail(msgs, {mt}, {mc})));"
        )
        js_result = json.loads(_run_node_check(js_script).strip())
        assert js_result == claude_result, (
            f"[{label}] JS/claude thread-tail 불일치 — JS={js_result!r} claude={claude_result!r}"
        )


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
const m = require("./ctx-guard.js");
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
