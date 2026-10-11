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
import sys
from pathlib import Path

import pytest

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


def _install_core(root: Path) -> Path:
    """core 사본을 픽스처의 `<root>/.opencode/lib/` 에 둔다 — 설치 형상 그대로.

    core 는 엔진 루트를 자기 위치(`path.resolve(__dirname, "..", "..")`)에서 내므로, 소스
    트리에서 require 하면 root 가 `templates/opencode` 가 되어 마커·conf·pm_log subprocess 가
    소스 트리에 착지한다. 팩토리를 구동하는 검증은 이 사본을 cwd 로 돌린다.
    """
    lib = root / ".opencode" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    for name in ("ctx-guard-core.cjs", "delegate-channel-core.cjs", "warning-channel-core.cjs"):
        shutil.copyfile(CORE_DIR / name, lib / name)
    return lib


def _make_ctx_guard_project(tmp_path: Path, *, snapshot: bool = True) -> Path:
    """최소 project 를 pytest tmp_path 아래에 만든다(소스 트리 무오염)."""
    root = tmp_path / "ctx-guard-project"
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    _install_core(root)
    shutil.copyfile(REPO / ".project_manager" / "tools" / "python_floor.py", tools / "python_floor.py")
    source = "import sys\n"
    if snapshot:
        source += (
            "if 'snapshot' in sys.argv:\n"
            "    sys.stdout.buffer.write('## PM 정체성 (compaction 복구)\\n- task: main\\n'.encode('utf-8'))\n"
        )
    (tools / "pm_log.py").write_text(source, encoding="utf-8", newline="\n")
    return root


def test_t007_fixture_payload_bytes_and_marker_consumption(tmp_path):
    assert shutil.which("node") is not None, "Node가 있어야 marker 원문을 검증할 수 있다"
    for label, payload in (("lf", "원문\n둘째\n"), ("crlf", "원문\r\n둘째\r\n")):
        root = _make_ctx_guard_project(tmp_path / label)
        producer = subprocess.run(
            [sys.executable, str(root / ".project_manager" / "tools" / "pm_log.py"), "snapshot"],
            capture_output=True, check=True,
        )
        assert producer.stdout == "## PM 정체성 (compaction 복구)\n- task: main\n".encode("utf-8")
        assert producer.stderr == b""
        marker = _snapshot_marker(root, label, "20260811T000000000Z-00000000-0000-4000-8000-000000000099")
        marker.parent.mkdir(parents=True)
        if label == "lf":
            marker.write_text(payload, encoding="utf-8", newline="\n")
        else:
            marker.write_bytes(payload.encode("utf-8"))
        assert marker.read_bytes() == payload.encode("utf-8")
        script = r'''
const m = require("./ctx-guard-core.cjs");
(async () => {
  const hooks = await m.CtxGuardPlugin({directory:process.argv[1],
    client:{session:{get:async ({path:{id}}) => ({data:{id}})}}});
  const output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:process.argv[2]}, output);
  process.stdout.write(JSON.stringify(output.system));
})().catch((err) => {console.error(err); process.exitCode=1;});
'''
        consumed = _run_node_check(script, root, [str(root), label])
        assert json.loads(consumed) == [payload]
        assert not marker.exists()


def test_t0934_pm_log_spawn_once_options_and_raw_payload(tmp_path):
    assert shutil.which("node") is not None, "Node가 있어야 pm_log spawn 계약을 검증할 수 있다"
    root = _make_ctx_guard_project(tmp_path)
    script = r'''
const assert = require("node:assert");
const path = require("node:path");
const m = require("./ctx-guard-core.cjs");
const leaf = require("./delegate-channel-core.cjs");
const fs = require("node:fs");
const root = process.argv[1];
const conf = path.join(root, ".project_manager", "local.conf");
assert.strictEqual(m.parseLocalConf, leaf.parseLocalConf, "parser must be moved and re-exported");
const floor = path.join(root, ".project_manager", "tools", "python_floor.py");
const engine = path.join(root, ".project_manager", "tools", "pm_log.py");
const missing = {status:null, error:{code:"ENOENT", message:"missing"}};
const raw = "원문\r\n둘째\r\n";
for (const platform of ["linux", "win32"]) {
  for (const configured of [false, true]) {
  const runtime = path.join(root, "Python with space", "python.exe");
  fs.writeFileSync(conf, "runtime.py=" + (configured ? runtime : "") + "\n");
  for (const capture of [true, false]) {
    for (const outcome of ["success", "rc", "missing", "timeout", "null", "throw"]) {
      const calls = [];
      const argv = capture ? ["snapshot", "--session-id", "ses with space"]
        : ["checkpoint", "--trigger", "compaction", "--phase", "post"];
      const expected = outcome === "success" ? {status:0, stdout:raw, stderr:""}
        : outcome === "rc" ? {status:7, stdout:"", stderr:"engine rc"}
        : outcome === "missing" ? missing
        : outcome === "timeout" ? {status:null, error:{code:"ETIMEDOUT", message:"timeout"}}
        : null;
      const fake = (command, args, options) => {
        const probe = args[args.length-1] === floor;
        calls.push({command, args, options, probe});
        if (probe) {
          assert.deepStrictEqual(options, {encoding:"utf8", timeout:137, input:""});
          if (configured) {
            assert.strictEqual(command, runtime);
            assert.deepStrictEqual(args, [floor]);
            return {status:0, stdout:"Python 3.12\n"};
          }
          if (command === "py") return missing;
          if (command === "python3") return {status:9009, error:null, stderr:"Store alias"};
          return {status:0, stdout:"Python 3.12\n", stderr:""};
        }
        assert.strictEqual(command, configured ? runtime : "python");
        assert.deepStrictEqual(args, [engine, ...argv]);
        assert.deepStrictEqual(options, {cwd:root, encoding:"utf8", timeout:137,
          maxBuffer:32768, stdio:capture ? ["ignore","pipe","ignore"] : ["ignore","ignore","ignore"]});
        if (outcome === "throw") throw new Error("engine throw");
        return expected;
      };
      if (outcome === "throw") assert.throws(() => m.runPmLog(root, argv, {capture,timeout:137,platform}, fake), /engine throw/);
      else assert.strictEqual(m.runPmLog(root, argv, {capture,timeout:137,platform}, fake), expected);
      assert.strictEqual(calls.filter((call) => !call.probe).length, 1);
      assert.strictEqual(calls[calls.length-1].probe, false, "no probe after actual engine failure");
    }
  }
  }
}
fs.writeFileSync(conf, "runtime.py=\n");
let prefixCalls = 0;
const prefixResult = m.runPmLog(root, ["snapshot"], {capture:true, platform:"win32"}, (command,args) => {
  prefixCalls += 1;
  assert.strictEqual(command, "py");
  if (args[args.length-1] === floor) {
    assert.deepStrictEqual(args, ["-3", floor]);
    return {status:0, stdout:"Python 3.12\n"};
  }
  assert.deepStrictEqual(args, ["-3", engine, "snapshot"]);
  return {status:0, stdout:raw};
});
assert.strictEqual(prefixResult.stdout, raw);
assert.strictEqual(prefixCalls, 2);
const calls = [];
const selected = leaf.pythonCandidates(process.platform)[0];
const fake = (command, args, options) => {
  calls.push({command,args,options});
  if (args[args.length-1] === floor) return {status:0, stdout:"Python 3.12\n"};
  assert.deepStrictEqual(args, [...selected.prefixArgs, engine, "checkpoint", "--trigger", "compaction",
    "--phase", "post", "--session-id", "SID", "--boundary-id", "boundary"]);
  assert.strictEqual(options.timeout, m.CHECKPOINT_TIMEOUT_MS);
  assert.deepStrictEqual(options.stdio, ["ignore","ignore","ignore"]);
  return {status:9};
};
m.createCompactionCheckpoint(root, " SID ", " boundary ", fake);
assert.strictEqual(calls.length, 2);
assert.strictEqual(calls[0].command, selected.command);
assert.deepStrictEqual(calls[0].args, [...selected.prefixArgs, floor]);
assert.strictEqual(calls[0].options.timeout, m.CHECKPOINT_TIMEOUT_MS);
const snapshot = m.buildCompactionSnapshot(root, "SID", (command,args) =>
  args[args.length-1] === floor ? {status:0, stdout:"Python 3.12\n"} : {status:0, stdout:raw});
assert.deepStrictEqual(snapshot, {ok:true, text:raw});
console.log("T0934_PM_LOG_ONCE_OK");
'''
    out = _run_node_check(script, root, [str(root)])
    assert "T0934_PM_LOG_ONCE_OK" in out, out


def _snapshot_marker_dir(root: Path) -> Path:
    return root / ".project_manager" / ".local" / "ctx-stop"


def _safe_snapshot_key(session_id: str) -> str:
    """pm_log.py `_safe_marker_key`와 같은 ASCII 파일명 allowlist/96자 제한."""
    return re.sub(r"[^A-Za-z0-9_-]", "", session_id.strip())[:96]


def _snapshot_markers(root: Path, session_id: str) -> list[Path]:
    key = _safe_snapshot_key(session_id)
    if not key:
        return []
    return sorted(_snapshot_marker_dir(root).glob(f"compact-snapshot.{key}.*"))


def _snapshot_marker(root: Path, session_id: str, generation: str) -> Path:
    key = _safe_snapshot_key(session_id)
    assert key
    return _snapshot_marker_dir(root) / f"compact-snapshot.{key}.{generation}"


def _snapshot_receipts(root: Path, session_id: str) -> list[Path]:
    key = _safe_snapshot_key(session_id)
    if not key:
        return []
    return sorted(_snapshot_marker_dir(root).glob(f"compact-snapshot-receipt.{key}.*"))


def _snapshot_receipt(root: Path, session_id: str, generation: str) -> Path:
    key = _safe_snapshot_key(session_id)
    assert key
    return _snapshot_marker_dir(root) / f"compact-snapshot-receipt.{key}.{generation}"


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
    assert "buildCompactionSnapshot" in src, "압축 후 엔진 snapshot 호출 없음"
    assert '"snapshot"' in src and '"checkpoint"' in src
    assert "notifyCompacted" in src, "압축 후 사람용 toast 없음"


# ── 2c. ctx 예산 (ADR-0041 · T-0943): 압축 한도 기록 · modelLimit 폐기 ─────────

def test_plugin_reads_budget_and_ratios_from_the_engine_record():
    """분모 E 와 두 비율은 엔진 압축 한도 기록(`local_conf.read_ctx_budget`)에서 온다 — 플러그인이
    local.conf 비율을 따로 해석하거나 기본값으로 바꾸지 않는다(T-0943 · 설계 Q1)."""
    src = _plugin_src()
    assert "function readBudget" in src
    assert 'leaf.read_ctx_budget(root,conf,"opencode",model)' in src
    assert "readBudget(loadLocalConf(root), root, `${info.providerID}/${info.modelID}`)" in src
    assert "const t = result.budget;" in src and "computeCtxState(used, t.E, t)" in src
    for removed in ("readPct(", "resolveThresholds", "resolveBudget", "validatedBudget",
                    "CTX_WINDOW_TOKENS_DEFAULT", "CHECKPOINT_AT_PCT_DEFAULT", "PRECOMPACT_AT_PCT_DEFAULT"):
        assert removed not in src, removed


def test_plugin_uses_engine_budget_not_model_limit():
    """정지/넛지 분모가 모델 물리한도 조회가 아니라 기록의 적용 한도다 (ADR-0041)."""
    src = _plugin_src()
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
    out = _run_node_check(script, project_root)
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
  assert.strictEqual(output.system.length, 1, "post-compaction snapshot missing");
  assert.ok(output.system[0].startsWith("## PM 정체성 (compaction 복구)"), "engine snapshot missing");
  assert.ok(!/compaction\s+\d+회/.test(output.system[0]), "display-only count must not be exposed");
  assert.ok(output.system[0].includes("- task: main"), "snapshot identity missing");
  assert.ok(toasts.some((text) => text.includes("compaction이 방금 일어남")), "human toast missing");
  assert.ok(toasts.every((text) => !/compaction\s+\d+회/.test(text)), "toast exposed count");

  await hooks.event(nudgeEvent());
  output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(output.system.length, 1, "nudge was not re-armed after compaction");
  console.log("JS_COMPACTION_REARM_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script, project_root)
    assert "JS_COMPACTION_REARM_OK" in out, f"compaction 병행 신호 실패. out={out!r}"


def test_plugin_compaction_snapshot_failure_keeps_model_facing_fallback(tmp_path):
    """builder가 null이어도 다음 model turn에는 최소 복구 안내가 1회 남는다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — snapshot fallback 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path, snapshot=False)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const sessionID = "ses_snapshot_null";

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  const output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(output.system.length, 1, "null snapshot erased model-facing notice");
  assert.ok(output.system[0].includes("[ctx-checkpoint]"), "fallback marker missing");
  assert.ok(output.system[0].includes("pm_state"), "fallback recovery pointer missing");
  console.log("JS_COMPACTION_FALLBACK_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script, project_root)
    assert "JS_COMPACTION_FALLBACK_OK" in out, f"snapshot null fallback 실패. out={out!r}"


def test_plugin_compaction_stages_builder_payload_in_durable_marker(tmp_path):
    """session.compacted는 builder stdout을 SID+generation marker로 원자 생성한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — durable snapshot staging 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_durable_stage"
    marker_dir = _snapshot_marker_dir(project_root)
    assert not marker_dir.exists(), "fixture가 marker 디렉터리를 미리 생성함"
    script = r'''
const m = require("./ctx-guard-core.cjs");
const projectRoot = __PROJECT_ROOT__;
const sessionID = __SESSION_ID__;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  console.log("JS_DURABLE_SNAPSHOT_STAGE_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    )
    out = _run_node_check(script, project_root)
    assert "JS_DURABLE_SNAPSHOT_STAGE_OK" in out, f"durable snapshot staging 실패. out={out!r}"
    markers = _snapshot_markers(project_root, session_id)
    assert len(markers) == 1
    assert re.fullmatch(
        rf"compact-snapshot\.{session_id}\.\d{{8}}T\d{{9}}Z-[0-9a-f-]{{36}}",
        markers[0].name,
    )
    assert markers[0].read_text(encoding="utf-8") == (
        "## PM 정체성 (compaction 복구)\n- task: main\n"
    )


def test_engine_root_is_install_tree_not_ancestor_project(tmp_path):
    """중첩 트리에서 훅은 payload directory 나 조상이 아니라 **자기 설치 트리**를 엔진 루트로 쓴다.

    바깥 프로젝트 안에 설치 트리를 두고 payload `directory` 로는 **바깥**을 준다. 조상 탐색이나
    directory 유도라면 바깥 프로젝트의 pm_log.py 를 돌려 바깥에 marker 를 남긴다. 자기 위치
    규칙이면 안쪽 설치 트리의 엔진을 돌려 안쪽에만 남는다.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — 엔진 루트 유도 단위 skip")

    def _project(root: Path, payload: str) -> Path:
        tools = root / ".project_manager" / "tools"
        tools.mkdir(parents=True)
        (tools / "pm_log.py").write_text(
            "import sys\n"
            "if 'snapshot' in sys.argv:\n"
            f"    sys.stdout.buffer.write(({payload!r} + '\\n').encode('utf-8'))\n",
            encoding="utf-8",
        )
        shutil.copyfile(REPO / ".project_manager" / "tools" / "python_floor.py", tools / "python_floor.py")
        return root

    outer = _project(tmp_path / "outer", "OUTER-PROJECT-SNAPSHOT")
    inner = _project(outer / "nested" / "inner", "INNER-INSTALL-SNAPSHOT")
    _install_core(inner)
    session_id = "ses_engine_root_install_tree"

    script = r'''
const m = require("./ctx-guard-core.cjs");
const outerDirectory = __OUTER__;
const sessionID = __SESSION_ID__;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:outerDirectory});
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  console.log("JS_ENGINE_ROOT_SELF_LOCATION_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__OUTER__", json.dumps(str(outer))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    )
    out = _run_node_check(script, inner)
    assert "JS_ENGINE_ROOT_SELF_LOCATION_OK" in out, f"엔진 루트 유도 실패. out={out!r}"

    markers = _snapshot_markers(inner, session_id)
    assert len(markers) == 1, f"설치 트리에 marker 가 없음: {markers}"
    assert markers[0].read_text(encoding="utf-8") == "INNER-INSTALL-SNAPSHOT\n"
    assert not _snapshot_marker_dir(outer).exists(), (
        "바깥 프로젝트에 marker 를 남겼다 — 조상/ payload directory 유도 잔존"
    )
    assert _snapshot_markers(REPO, session_id) == [], "저장소 루트에 marker 를 남겼다"


def test_plugin_snapshot_session_key_cannot_escape_marker_directory(tmp_path):
    """traversal/절대/공백 SID는 단일 안전 파일명이거나 marker skip으로 끝난다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — durable snapshot SID 경로 안전 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    escaped_target = project_root / ".project_manager" / ".local" / "evil"
    escaped_target.parent.mkdir(parents=True)
    escaped_target.write_text("sentinel", encoding="utf-8")
    session_ids = (
        "../../evil",
        str(tmp_path / "absolute target"),
        "session with spaces",
        "../../",
        " / ",
    )
    script = r'''
const m = require("./ctx-guard-core.cjs");
const projectRoot = __PROJECT_ROOT__;
const sessionIDs = __SESSION_IDS__;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  for (const sessionID of sessionIDs) {
    await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  }
  console.log("JS_DURABLE_SNAPSHOT_SID_PATH_SAFE_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__SESSION_IDS__", json.dumps(session_ids)
    )
    out = _run_node_check(script, project_root)
    assert "JS_DURABLE_SNAPSHOT_SID_PATH_SAFE_OK" in out, f"SID 경로 안전화 실패. out={out!r}"

    marker_dir = _snapshot_marker_dir(project_root)
    expected_keys = {_safe_snapshot_key(sid) for sid in session_ids}
    expected_keys.discard("")
    markers = sorted(marker_dir.glob("compact-snapshot.*"))
    assert len(markers) == len(expected_keys)
    assert all(marker.is_file() and marker.parent == marker_dir for marker in markers)
    assert all(
        re.fullmatch(r"compact-snapshot\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", marker.name)
        for marker in markers
    )
    assert {marker.name.split(".", 2)[1] for marker in markers} == expected_keys
    assert not any(path.is_dir() for path in marker_dir.iterdir())
    assert escaped_target.read_text(encoding="utf-8") == "sentinel"


def test_plugin_restarted_transform_consumes_durable_marker_exactly_once(tmp_path):
    """재시작은 최신 generation을 1회 주입하고 구 generation까지 정리한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — durable snapshot restart consume 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_durable_restart"
    marker_dir = _snapshot_marker_dir(project_root)
    engine = project_root / ".project_manager" / "tools" / "pm_log.py"
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const projectRoot = __PROJECT_ROOT__;
const markerDir = __MARKER_DIR__;
const engine = __ENGINE__;
const sessionID = __SESSION_ID__;
const markerPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot.${sessionID}.`))
  .map((name) => path.resolve(markerDir, name));
const receiptPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot-receipt.${sessionID}.`))
  .sort()
  .map((name) => path.resolve(markerDir, name));
const builder = (value) =>
  `import sys\nif 'snapshot' in sys.argv:\n    sys.stdout.buffer.write(('${value}\\n').encode('utf-8'))\n`;

(async () => {
  const client = {session:{get: async ({path:{id}}) => ({data:{id}})}};
  fs.writeFileSync(engine, builder("snapshot-v1"), "utf8");
  const firstProcess = await m.CtxGuardPlugin({client, directory:projectRoot});
  await firstProcess.event({event:{type:"session.compacted", properties:{sessionID}}});
  const firstMarker = markerPaths()[0];
  assert.ok(firstMarker, "first process did not leave marker");

  fs.writeFileSync(engine, builder("snapshot-v2"), "utf8");
  const secondProcess = await m.CtxGuardPlugin({client, directory:projectRoot});
  await secondProcess.event({event:{type:"session.compacted", properties:{sessionID}}});
  const staged = markerPaths();
  assert.strictEqual(staged.length, 2, "independent generations did not coexist");
  const secondMarker = staged.find((marker) => marker !== firstMarker);
  const secondGeneration = path.basename(secondMarker)
    .slice(`compact-snapshot.${sessionID}.`.length);
  fs.utimesSync(firstMarker, new Date(1000), new Date(1000));
  fs.utimesSync(secondMarker, new Date(2000), new Date(2000));

  const restartedProcess = await m.CtxGuardPlugin({client, directory:projectRoot});
  const firstOutput = {system:[]};
  await restartedProcess["experimental.chat.system.transform"]({sessionID}, firstOutput);
  assert.strictEqual(firstOutput.system.length, 1, "restarted process did not inject marker");
  assert.strictEqual(firstOutput.system[0], "snapshot-v2\n", "restart did not choose newest marker");
  assert.deepStrictEqual(markerPaths(), [], "consumed and older generations remain");
  assert.deepStrictEqual(
    receiptPaths(),
    [path.resolve(markerDir, `compact-snapshot-receipt.${sessionID}.${secondGeneration}`)],
    "fallback delivery receipt did not bind only the pushed generation",
  );

  const secondOutput = {system:[]};
  await restartedProcess["experimental.chat.system.transform"]({sessionID}, secondOutput);
  assert.deepStrictEqual(secondOutput.system, [], "marker was injected more than once");
  console.log("JS_DURABLE_SNAPSHOT_RESTART_CONSUME_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__MARKER_DIR__", json.dumps(str(marker_dir))
    ).replace("__ENGINE__", json.dumps(str(engine))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    )
    out = _run_node_check(script, project_root)
    assert "JS_DURABLE_SNAPSHOT_RESTART_CONSUME_OK" in out, (
        f"durable snapshot restart consume 실패. out={out!r}"
    )


def test_plugin_in_memory_consumption_preserves_newer_foreign_generation(tmp_path):
    """A의 stale in-memory 소비는 나중에 B가 stage한 같은 SID marker를 지우지 않는다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — in-memory snapshot marker cleanup 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_memory_precedence"
    marker_dir = _snapshot_marker_dir(project_root)
    engine = project_root / ".project_manager" / "tools" / "pm_log.py"
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const projectRoot = __PROJECT_ROOT__;
const markerDir = __MARKER_DIR__;
const engine = __ENGINE__;
const sessionID = __SESSION_ID__;
const markerPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot.${sessionID}.`))
  .map((name) => path.resolve(markerDir, name));
const receiptPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot-receipt.${sessionID}.`))
  .sort()
  .map((name) => path.resolve(markerDir, name));
const builder = (value) =>
  `import sys\nif 'snapshot' in sys.argv:\n    sys.stdout.buffer.write(('${value}\\n').encode('utf-8'))\n`;

(async () => {
  const client = {session:{get: async ({path:{id}}) => ({data:{id}})}};
  fs.writeFileSync(engine, builder("process-a"), "utf8");
  const processA = await m.CtxGuardPlugin({client, directory:projectRoot});
  await processA.event({event:{type:"session.compacted", properties:{sessionID}}});
  const markerA = markerPaths()[0];
  const generationA = path.basename(markerA).slice(`compact-snapshot.${sessionID}.`.length);

  fs.writeFileSync(engine, builder("process-b"), "utf8");
  const processB = await m.CtxGuardPlugin({client, directory:projectRoot});
  await processB.event({event:{type:"session.compacted", properties:{sessionID}}});
  const staged = markerPaths();
  assert.strictEqual(staged.length, 2);
  const markerB = staged.find((marker) => marker !== markerA);

  const output = {system:[]};
  const receiptA = path.resolve(markerDir, `compact-snapshot-receipt.${sessionID}.${generationA}`);
  const originalWriteFileSync = fs.writeFileSync;
  const receiptWrites = [];
  fs.writeFileSync = (...args) => {
    if (path.resolve(String(args[0])) === receiptA) {
      assert.strictEqual(args[1], "", "receipt data must be empty");
      assert.strictEqual(args[2].mode, 0o600, "receipt requested mode is not 0600");
      assert.strictEqual(args[2].encoding, "utf8");
      assert.strictEqual(args[2].flag === undefined ? "w" : args[2].flag, "w");
      receiptWrites.push(receiptA);
    }
    return originalWriteFileSync(...args);
  };
  try {
    await processA["experimental.chat.system.transform"]({sessionID}, output);
  } finally {
    fs.writeFileSync = originalWriteFileSync;
  }
  assert.deepStrictEqual(receiptWrites, [receiptA], "receipt write options were not observed exactly once");
  assert.strictEqual(output.system.length, 1);
  assert.strictEqual(output.system[0], "process-a\n", "A did not inject its memory payload");
  assert.strictEqual(fs.existsSync(markerA), false, "A left its owned generation");
  assert.strictEqual(fs.readFileSync(markerB, "utf8"), "process-b\n", "A deleted B generation");
  assert.deepStrictEqual(receiptPaths(), [receiptA], "memory delivery receipt generation mismatch");
  assert.strictEqual(fs.statSync(receiptA).isFile(), true, "receipt is not a real file");
  if (process.platform !== "win32") {
    assert.strictEqual(fs.statSync(receiptA).mode & 0o777, 0o600, "receipt mode is not 0600");
  }
  assert.strictEqual(fs.readFileSync(receiptA, "utf8"), "", "receipt must be empty");
  console.log("JS_IN_MEMORY_MARKER_OWNERSHIP_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__MARKER_DIR__", json.dumps(str(marker_dir))
    ).replace("__ENGINE__", json.dumps(str(engine))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    )
    out = _run_node_check(script, project_root)
    assert "JS_IN_MEMORY_MARKER_OWNERSHIP_OK" in out, f"in-memory marker 소유 규율 실패. out={out!r}"


def test_plugin_nudge_overwrite_preserves_snapshot_marker_for_next_transform(tmp_path):
    """nudge2가 memory 슬롯을 덮어도 snapshot marker는 다음 transform까지 생존한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — nudge/snapshot marker 간섭 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_nudge_snapshot_interference"
    marker_dir = _snapshot_marker_dir(project_root)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const projectRoot = __PROJECT_ROOT__;
const markerDir = __MARKER_DIR__;
const sessionID = __SESSION_ID__;
const path = require("node:path");
const snapshotPayload = "## PM 정체성 (compaction 복구)\n- task: main\n";
const markerPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot.${sessionID}.`))
  .map((name) => path.resolve(markerDir, name));
const receiptPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot-receipt.${sessionID}.`))
  .sort()
  .map((name) => path.resolve(markerDir, name));

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});

  // compaction은 동일 payload를 memory 슬롯과 durable marker 양쪽에 적재한다.
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  const staged = markerPaths();
  assert.strictEqual(staged.length, 1, "compaction did not stage exactly one marker");
  assert.strictEqual(fs.readFileSync(staged[0], "utf8"), snapshotPayload);

  // nudge2가 memory 슬롯을 덮을 때 snapshot marker 소유권을 놓아야 한다.
  await hooks.event({event:{type:"message.updated", properties:{info:{
    sessionID, role:"assistant", tokens:{input:155000}
  }}}});

  const firstOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, firstOutput);
  assert.strictEqual(firstOutput.system.length, 1, "nudge2 was not injected once");
  assert.ok(firstOutput.system[0].includes("[ctx-nudge/강화]"), "wrong first payload");
  assert.deepStrictEqual(markerPaths(), staged, "nudge2 transform deleted snapshot marker");
  assert.strictEqual(fs.readFileSync(staged[0], "utf8"), snapshotPayload);
  assert.deepStrictEqual(receiptPaths(), [], "ownership-released nudge created snapshot receipt");

  const secondOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, secondOutput);
  assert.deepStrictEqual(secondOutput.system, [snapshotPayload], "marker fallback lost snapshot");
  assert.deepStrictEqual(markerPaths(), [], "marker remained after fallback consumption");
  assert.strictEqual(receiptPaths().length, 1, "fallback delivery did not create one receipt");

  const thirdOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, thirdOutput);
  assert.deepStrictEqual(thirdOutput.system, [], "snapshot was injected more than once");
  const delivered = [...firstOutput.system, ...secondOutput.system, ...thirdOutput.system];
  assert.strictEqual(
    delivered.filter((payload) => payload === snapshotPayload).length,
    1,
    "snapshot payload delivery count was not exactly one",
  );
  console.log("JS_NUDGE_SNAPSHOT_MARKER_INTERFERENCE_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__MARKER_DIR__", json.dumps(str(marker_dir))
    ).replace("__SESSION_ID__", json.dumps(session_id))
    out = _run_node_check(script, project_root)
    assert "JS_NUDGE_SNAPSHOT_MARKER_INTERFERENCE_OK" in out, (
        f"nudge/snapshot marker 간섭 방지 실패. out={out!r}"
    )


def test_plugin_recompaction_replaces_owned_generation_with_latest_payload(tmp_path):
    """같은 프로세스의 연속 compaction은 새 generation의 최신 payload만 남긴다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — durable snapshot overwrite 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_latest_snapshot"
    marker_dir = _snapshot_marker_dir(project_root)
    engine = project_root / ".project_manager" / "tools" / "pm_log.py"
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const projectRoot = __PROJECT_ROOT__;
const markerDir = __MARKER_DIR__;
const engine = __ENGINE__;
const sessionID = __SESSION_ID__;
const markerPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot.${sessionID}.`))
  .map((name) => path.resolve(markerDir, name));
const builder = (value) =>
  `import sys\nif 'snapshot' in sys.argv:\n    sys.stdout.buffer.write(('${value}\\n').encode('utf-8'))\n`;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  fs.writeFileSync(engine, builder("snapshot-v1"), "utf8");
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  const firstMarker = markerPaths()[0];
  assert.strictEqual(fs.readFileSync(firstMarker, "utf8"), "snapshot-v1\n");

  fs.writeFileSync(engine, builder("snapshot-v2"), "utf8");
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  const latest = markerPaths();
  assert.strictEqual(latest.length, 1, "owned old generation remains");
  assert.notStrictEqual(latest[0], firstMarker, "recompaction reused a generation");
  assert.strictEqual(fs.readFileSync(latest[0], "utf8"), "snapshot-v2\n", "latest payload lost");
  console.log("JS_DURABLE_SNAPSHOT_OVERWRITE_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__MARKER_DIR__", json.dumps(str(marker_dir))
    ).replace("__ENGINE__", json.dumps(str(engine))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    )
    out = _run_node_check(script, project_root)
    assert "JS_DURABLE_SNAPSHOT_OVERWRITE_OK" in out, f"durable snapshot overwrite 실패. out={out!r}"


def test_plugin_recompaction_staging_failure_preserves_owned_generation(tmp_path):
    """재-compaction staging 실패는 구 marker 소유권을 유지해 새 memory payload 전달과 묶는다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — recompaction staging 실패 소유권 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_recompaction_stage_failure"
    marker_dir = _snapshot_marker_dir(project_root)
    engine = project_root / ".project_manager" / "tools" / "pm_log.py"
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const projectRoot = __PROJECT_ROOT__;
const markerDir = __MARKER_DIR__;
const engine = __ENGINE__;
const sessionID = __SESSION_ID__;
const markerPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot.${sessionID}.`))
  .sort()
  .map((name) => path.resolve(markerDir, name));
const receiptPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot-receipt.${sessionID}.`))
  .sort()
  .map((name) => path.resolve(markerDir, name));
const builder = (value) =>
  `import sys\nif 'snapshot' in sys.argv:\n    sys.stdout.buffer.write(('${value}\\n').encode('utf-8'))\n`;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  fs.writeFileSync(engine, builder("snapshot-v1"), "utf8");
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  const firstMarker = markerPaths()[0];
  const firstGeneration = path.basename(firstMarker)
    .slice(`compact-snapshot.${sessionID}.`.length);
  assert.strictEqual(fs.readFileSync(firstMarker, "utf8"), "snapshot-v1\n");

  fs.writeFileSync(engine, builder("snapshot-v2"), "utf8");
  const originalRenameSync = fs.renameSync;
  let stagingFailureObserved = false;
  fs.renameSync = (source, destination) => {
    if (
      path.basename(String(source)).endsWith(".tmp") &&
      path.dirname(path.resolve(String(destination))) === path.resolve(markerDir) &&
      path.basename(String(destination)).startsWith(`compact-snapshot.${sessionID}.`)
    ) {
      stagingFailureObserved = true;
      throw new Error("synthetic recompaction staging failure");
    }
    return originalRenameSync(source, destination);
  };
  try {
    await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  } finally {
    fs.renameSync = originalRenameSync;
  }

  assert.strictEqual(stagingFailureObserved, true, "second staging failure was not induced");
  assert.deepStrictEqual(markerPaths(), [firstMarker], "failed staging discarded the owned marker");
  assert.strictEqual(fs.readFileSync(firstMarker, "utf8"), "snapshot-v1\n");
  assert.deepStrictEqual(receiptPaths(), [], "non-delivery staging path created a receipt");

  const output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.deepStrictEqual(output.system, ["snapshot-v2\n"], "latest in-memory payload was not delivered");
  assert.deepStrictEqual(markerPaths(), [], "owned old marker was not consumed with memory delivery");
  assert.deepStrictEqual(
    receiptPaths(),
    [path.resolve(markerDir, `compact-snapshot-receipt.${sessionID}.${firstGeneration}`)],
    "memory delivery receipt did not retain the owned durable generation",
  );
  console.log("JS_RECOMPACTION_STAGING_FAILURE_OWNERSHIP_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__MARKER_DIR__", json.dumps(str(marker_dir))
    ).replace("__ENGINE__", json.dumps(str(engine))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    )
    out = _run_node_check(script, project_root)
    assert "JS_RECOMPACTION_STAGING_FAILURE_OWNERSHIP_OK" in out, (
        f"recompaction staging 실패 소유권 보존 실패. out={out!r}"
    )


def test_plugin_late_entry_never_observes_old_while_latest_claim_is_pending(tmp_path):
    """A의 old GC 전환점을 쪼개면 B는 [latest] 또는 []만 관측한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — late-entry snapshot consumer 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    marker_dir = _snapshot_marker_dir(project_root)
    marker_dir.mkdir(parents=True)
    before_session_id = "ses_late_entry_before_claim"
    after_session_id = "ses_late_entry_after_claim"
    before_latest_generation = "20260811T000000001Z-00000000-0000-4000-8000-000000000012"
    before_old_marker = _snapshot_marker(
        project_root, before_session_id,
        "20260811T000000000Z-00000000-0000-4000-8000-000000000011",
    )
    before_latest_marker = _snapshot_marker(
        project_root, before_session_id, before_latest_generation,
    )
    before_latest_payload = "snapshot-latest-before-claim\n"
    before_old_marker.write_text("snapshot-old-before-claim\n", encoding="utf-8", newline="\n")
    before_latest_marker.write_text(before_latest_payload, encoding="utf-8", newline="\n")
    os.utime(before_old_marker, (1, 1))
    os.utime(before_latest_marker, (2, 2))

    after_old_marker = _snapshot_marker(
        project_root, after_session_id,
        "20260811T000000000Z-00000000-0000-4000-8000-000000000013",
    )
    after_latest_marker = _snapshot_marker(
        project_root, after_session_id,
        "20260811T000000001Z-00000000-0000-4000-8000-000000000014",
    )
    after_latest_payload = "snapshot-latest-after-claim\n"
    after_old_marker.write_text("snapshot-old-after-claim\n", encoding="utf-8", newline="\n")
    after_latest_marker.write_text(after_latest_payload, encoding="utf-8", newline="\n")
    os.utime(after_old_marker, (1, 1))
    os.utime(after_latest_marker, (2, 2))

    # A의 단계를 쪼갠다: claim 전에는 old를 지워 [latest], claim 후에는 []가 보인다.
    before_old_marker.unlink()
    after_old_marker.unlink()
    after_claimed = marker_dir / f".{after_latest_marker.name}.claimed"
    os.link(after_latest_marker, after_claimed)
    assert _snapshot_markers(project_root, before_session_id) == [before_latest_marker]
    assert _snapshot_markers(project_root, after_session_id) == [after_latest_marker]

    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const beforeSessionID = __BEFORE_SESSION_ID__;
const afterSessionID = __AFTER_SESSION_ID__;
const beforeLatestPayload = __BEFORE_LATEST_PAYLOAD__;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  const beforeOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:beforeSessionID}, beforeOutput);
  assert.deepStrictEqual(beforeOutput.system, [beforeLatestPayload], "pre-claim B missed latest");
  const afterOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:afterSessionID}, afterOutput);
  assert.deepStrictEqual(afterOutput.system, [], "post-claim B injected a stale snapshot");
  console.log("JS_SNAPSHOT_LATE_ENTRY_GC_FIRST_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__BEFORE_SESSION_ID__", json.dumps(before_session_id)
    ).replace("__AFTER_SESSION_ID__", json.dumps(after_session_id)).replace(
        "__BEFORE_LATEST_PAYLOAD__", json.dumps(before_latest_payload)
    )
    out = _run_node_check(script, project_root)
    assert "JS_SNAPSHOT_LATE_ENTRY_GC_FIRST_OK" in out, (
        f"snapshot late-entry GC-선행 실패. out={out!r}"
    )
    assert _snapshot_markers(project_root, before_session_id) == []
    assert _snapshot_receipts(project_root, before_session_id) == [
        _snapshot_receipt(project_root, before_session_id, before_latest_generation)
    ]
    assert _snapshot_markers(project_root, after_session_id) == [after_latest_marker]
    assert _snapshot_receipts(project_root, after_session_id) == []
    assert after_claimed.read_text(encoding="utf-8") == after_latest_payload


def test_plugin_old_gc_failure_skips_latest_claim_until_retry(tmp_path):
    """old unlink 실패 시 latest를 보존하고, 실패 해제 뒤 latest를 정확히 1회 전달한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — snapshot old GC IO fail-soft 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_snapshot_old_gc_failure"
    marker_dir = _snapshot_marker_dir(project_root)
    marker_dir.mkdir(parents=True)
    old_generation = "20260811T000000000Z-00000000-0000-4000-8000-000000000031"
    latest_generation = "20260811T000000001Z-00000000-0000-4000-8000-000000000032"
    old_marker = _snapshot_marker(project_root, session_id, old_generation)
    latest_marker = _snapshot_marker(project_root, session_id, latest_generation)
    latest_payload = "snapshot-latest-after-old-gc-retry\n"
    old_marker.write_text("snapshot-old-blocked-by-eperm\n", encoding="utf-8", newline="\n")
    latest_marker.write_text(latest_payload, encoding="utf-8", newline="\n")
    os.utime(old_marker, (1, 1))
    os.utime(latest_marker, (2, 2))

    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const projectRoot = __PROJECT_ROOT__;
const markerDir = __MARKER_DIR__;
const sessionID = __SESSION_ID__;
const oldMarker = __OLD_MARKER__;
const latestMarker = __LATEST_MARKER__;
const latestPayload = __LATEST_PAYLOAD__;
const path = require("node:path");
const markerPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot.${sessionID}.`))
  .map((name) => path.resolve(markerDir, name))
  .sort();
const receiptPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot-receipt.${sessionID}.`));

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  const originalUnlinkSync = fs.unlinkSync;
  let oldGcFailureObserved = false;
  fs.unlinkSync = (target) => {
    if (path.resolve(String(target)) === path.resolve(oldMarker)) {
      oldGcFailureObserved = true;
      const error = new Error("synthetic old snapshot GC failure");
      error.code = "EPERM";
      throw error;
    }
    return originalUnlinkSync(target);
  };

  const blockedOutput = {system:[]};
  try {
    await hooks["experimental.chat.system.transform"]({sessionID}, blockedOutput);
  } finally {
    fs.unlinkSync = originalUnlinkSync;
  }
  assert.strictEqual(oldGcFailureObserved, true, "old unlink failure was not induced");
  assert.deepStrictEqual(blockedOutput.system, [], "old GC failure injected a snapshot");
  assert.deepStrictEqual(markerPaths(), [oldMarker, latestMarker].sort(), "old GC failure claimed a marker");
  assert.deepStrictEqual(receiptPaths(), [], "old GC failure created a receipt");
  assert.deepStrictEqual(
    fs.readdirSync(markerDir).filter((name) => name.endsWith(".claimed")),
    [],
    "old GC failure renamed latest to a claim",
  );

  const retryOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, retryOutput);
  assert.deepStrictEqual(retryOutput.system, [latestPayload], "retry did not deliver latest");
  const duplicateOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, duplicateOutput);
  assert.deepStrictEqual(duplicateOutput.system, [], "retry delivered latest more than once");
  console.log("JS_SNAPSHOT_OLD_GC_FAILURE_RETRY_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__MARKER_DIR__", json.dumps(str(marker_dir))
    ).replace("__SESSION_ID__", json.dumps(session_id)).replace(
        "__OLD_MARKER__", json.dumps(str(old_marker))
    ).replace("__LATEST_MARKER__", json.dumps(str(latest_marker))).replace(
        "__LATEST_PAYLOAD__", json.dumps(latest_payload)
    )
    out = _run_node_check(script, project_root)
    assert "JS_SNAPSHOT_OLD_GC_FAILURE_RETRY_OK" in out, (
        f"snapshot old GC 실패 후 재시도 복구 실패. out={out!r}"
    )
    assert _snapshot_markers(project_root, session_id) == []
    assert list(marker_dir.glob(".compact-snapshot.*.claimed")) == []
    assert _snapshot_receipts(project_root, session_id) == [
        _snapshot_receipt(project_root, session_id, latest_generation)
    ]


def _snapshot_claim_fixture(tmp_path, payload="최신 원문\r\n둘째\r\n"):
    root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_snapshot_claim"
    directory = _snapshot_marker_dir(root)
    directory.mkdir(parents=True)
    old = _snapshot_marker(root, session_id, "20260811T000000000Z-00000000-0000-4000-8000-000000000001")
    source = _snapshot_marker(root, session_id, "20260811T000000001Z-00000000-0000-4000-8000-000000000002")
    old.write_bytes(b"old\n")
    source.write_bytes(payload.encode("utf-8"))
    os.utime(old, (1, 1))
    os.utime(source, (2, 2))
    return {"root": str(root), "sid": session_id, "old": str(old), "source": str(source),
            "fence": str(source.with_name("." + source.name + ".claimed")), "payload": payload}


def _run_snapshot_claim_node(data, script, *, expected_rc=0):
    assert _NODE is not None, "Node가 있어야 실제 snapshot claim을 검증할 수 있다"
    done = subprocess.run([_NODE, "-e", script, json.dumps(data)],
                          cwd=Path(data["root"]) / ".opencode" / "lib",
                          capture_output=True, timeout=30)
    assert done.returncode == expected_rc, (done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))
    return json.loads(done.stdout)


@pytest.mark.parametrize("scenario", [
    "old_gc", "link_EEXIST", "link_ENOENT", "link_ENOTSUP", "link_EPERM", "link_EXDEV",
    "source_EACCES", "source_ENOENT", "read", "empty", "cleanup", "receipt",
])
def test_plugin_snapshot_claim_io_failure_matrix(tmp_path, scenario):
    data = _snapshot_claim_fixture(tmp_path, "" if scenario == "empty" else "최신 원문\r\n둘째\r\n")
    data["scenario"] = scenario
    script = r'''
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const m = require("./ctx-guard-core.cjs");
const d = JSON.parse(process.argv[1]);
const native = {link:fs.linkSync, unlink:fs.unlinkSync, read:fs.readFileSync, write:fs.writeFileSync};
const same = (a,b) => path.resolve(String(a)) === path.resolve(b);
const calls = {link:0, read:0, cleanup:0, receipt:0};
let injected = false;
const fail = (code) => {injected=true; const error=new Error("injected " + code); error.code=code; throw error;};
if (d.scenario === "link_EEXIST") native.link(d.source,d.fence);
fs.linkSync = (source,dest) => {
  calls.link += 1;
  assert.ok(same(source,d.source));
  assert.ok(same(dest,d.fence), "claim must use fixed generation path");
  if (d.scenario === "link_ENOENT") {injected=true; native.unlink(d.source);}
  if (["link_ENOTSUP","link_EPERM","link_EXDEV"].includes(d.scenario)) fail(d.scenario.slice(5));
  try {return native.link(source,dest);} catch(error) {injected=true; throw error;}
};
fs.unlinkSync = (target) => {
  if (same(target,d.old) && d.scenario === "old_gc") fail("EACCES");
  if (same(target,d.source) && d.scenario === "source_EACCES") fail("EACCES");
  if (same(target,d.source) && d.scenario === "source_ENOENT") {injected=true; native.unlink(target);}
  if (same(target,d.fence)) {
    calls.cleanup += 1;
    if (d.scenario === "cleanup") fail("EACCES");
  }
  return native.unlink(target);
};
fs.readFileSync = (target,...args) => {
  if (same(target,d.fence)) {calls.read += 1; if (d.scenario === "read") fail("EIO");}
  return native.read(target,...args);
};
fs.writeFileSync = (target,...args) => {
  if (path.basename(String(target)).startsWith("compact-snapshot-receipt.")) {
    calls.receipt += 1;
    if (d.scenario === "receipt") fail("EIO");
  }
  return native.write(target,...args);
};
(async () => {
  const hooks = await m.CtxGuardPlugin({directory:d.root,client:{session:{get:async ({path:{id}})=>({data:{id}})}}});
  const output={system:[]};
  try {await hooks["experimental.chat.system.transform"]({sessionID:d.sid},output);}
  finally {fs.linkSync=native.link; fs.unlinkSync=native.unlink; fs.readFileSync=native.read; fs.writeFileSync=native.write;}
  assert.strictEqual(calls.link,d.scenario === "old_gc" ? 0 : 1);
  if (d.scenario !== "empty") assert.strictEqual(injected,true,"failure seam was not reached");
  const delivered=["cleanup","receipt"].includes(d.scenario);
  assert.deepStrictEqual(output.system,delivered ? [d.payload] : []);
  const receipts=fs.readdirSync(path.dirname(d.source)).filter((n)=>n.startsWith("compact-snapshot-receipt."));
  assert.strictEqual(receipts.length,d.scenario === "cleanup" ? 1 : 0);
  if (d.scenario.startsWith("source_")) {
    assert.strictEqual(calls.read,0);
    assert.strictEqual(calls.cleanup,0);
    assert.strictEqual(calls.receipt,0);
    assert.ok(fs.existsSync(d.fence),"source removal failure must preserve the fence");
    assert.strictEqual(fs.existsSync(d.source),d.scenario === "source_EACCES");
  } else if (d.scenario.startsWith("link_")) {
    assert.strictEqual(calls.read,0); assert.strictEqual(calls.cleanup,0); assert.strictEqual(calls.receipt,0);
    assert.strictEqual(fs.existsSync(d.fence),d.scenario === "link_EEXIST");
    assert.strictEqual(fs.existsSync(d.source),d.scenario !== "link_ENOENT");
  } else if (d.scenario === "old_gc") {
    assert.ok(fs.existsSync(d.old)); assert.ok(fs.existsSync(d.source)); assert.ok(!fs.existsSync(d.fence));
  } else {
    assert.ok(!fs.existsSync(d.source));
    assert.strictEqual(fs.existsSync(d.fence),d.scenario === "cleanup");
    assert.strictEqual(calls.read,1); assert.strictEqual(calls.cleanup,1);
  }
  // persistent fence/removed source must not replay. Transient pre-claim failures may retry.
  const retry={system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:d.sid},retry);
  const retryAllowed=["old_gc","link_ENOTSUP","link_EPERM","link_EXDEV"].includes(d.scenario);
  assert.deepStrictEqual(retry.system,retryAllowed ? [d.payload] : []);
  if (d.scenario === "source_EACCES" || d.scenario === "source_ENOENT" || d.scenario === "link_EEXIST") assert.ok(fs.existsSync(d.fence));
  console.log(JSON.stringify({scenario:d.scenario,calls,output:output.system,retry:retry.system}));
})().catch((e)=>{console.error(e);process.exitCode=1;});
'''
    result = _run_snapshot_claim_node(data, script)
    assert result["scenario"] == scenario


@pytest.mark.parametrize("scenario", ["pre_link", "post_link", "post_source", "post_read",
                                     "other_session", "foreign", "memory", "child"])
def test_plugin_snapshot_claim_crash_and_owner_matrix(tmp_path, scenario):
    data = _snapshot_claim_fixture(tmp_path)
    data["scenario"] = scenario
    script = r'''
const assert=require("node:assert"),fs=require("node:fs"),path=require("node:path");
const m=require("./ctx-guard-core.cjs"),d=JSON.parse(process.argv[1]);
const native={link:fs.linkSync,unlink:fs.unlinkSync,read:fs.readFileSync};
const same=(a,b)=>path.resolve(String(a))===path.resolve(b);
const crash=()=>{console.log(JSON.stringify({crash:d.scenario,source:fs.existsSync(d.source),fence:fs.existsSync(d.fence)}));process.exit(17);};
fs.linkSync=(source,dest)=>{
  assert.ok(same(dest,d.fence));
  if(d.scenario==="pre_link") crash();
  const result=native.link(source,dest);
  if(d.scenario==="post_link") crash();
  if(d.scenario==="foreign") fs.writeFileSync(path.join(path.dirname(d.source),"compact-snapshot."+d.sid+".foreign-generation"),"foreign\r\n");
  return result;
};
fs.unlinkSync=(target)=>{const result=native.unlink(target);if(same(target,d.source)&&d.scenario==="post_source")crash();return result;};
fs.readFileSync=(target,...args)=>{const result=native.read(target,...args);if(same(target,d.fence)&&d.scenario==="post_read")crash();return result;};
(async()=>{
  const hooks=await m.CtxGuardPlugin({directory:d.root,client:{session:{get:async({path:{id}})=>({data:{id,parentID:d.scenario==="child"?"parent":undefined}})}}});
  const other=path.join(path.dirname(d.source),"compact-snapshot.other-session.foreign");
  fs.writeFileSync(other,"other\r\n");
  const otherFence=path.join(path.dirname(other),"."+path.basename(other)+".claimed");
  native.link(other,otherFence);
  const output={system:[]};
  if(d.scenario==="memory") {
    // Existing pending memory owns only its staged generation, not another producer's generation.
    fs.unlinkSync=native.unlink;fs.linkSync=native.link;fs.readFileSync=native.read;
    fs.unlinkSync(d.old);fs.unlinkSync(d.source);
    await hooks.event({event:{type:"session.compacted",properties:{sessionID:d.sid}}});
    const foreign=path.join(path.dirname(d.source),"compact-snapshot."+d.sid+".foreign-generation");
    fs.writeFileSync(foreign,"foreign\r\n");
    let links=0;fs.linkSync=()=>{links+=1;throw new Error("memory must not claim");};
    await hooks["experimental.chat.system.transform"]({sessionID:d.sid},output);
    assert.strictEqual(links,0);assert.deepStrictEqual(output.system,["## PM 정체성 (compaction 복구)\n- task: main\n"]);
    assert.strictEqual(fs.readFileSync(foreign,"utf8"),"foreign\r\n");
  } else {
    await hooks["experimental.chat.system.transform"]({sessionID:d.sid},output);
    assert.deepStrictEqual(output.system,d.scenario==="child"?[]:[d.payload]);
    if(d.scenario==="foreign") assert.strictEqual(native.read(path.join(path.dirname(d.source),"compact-snapshot."+d.sid+".foreign-generation"),"utf8"),"foreign\r\n");
    if(d.scenario==="child") {assert.ok(fs.existsSync(d.source));assert.ok(!fs.existsSync(d.fence));}
  }
  assert.strictEqual(native.read(other,"utf8"),"other\r\n");assert.strictEqual(native.read(otherFence,"utf8"),"other\r\n");
  console.log(JSON.stringify({scenario:d.scenario,output:output.system}));
})().catch((e)=>{console.error(e);process.exitCode=1;});
'''
    crashed = scenario in {"pre_link", "post_link", "post_source", "post_read"}
    result = _run_snapshot_claim_node(data, script, expected_rc=17 if crashed else 0)
    if crashed:
        assert result["fence"] is (scenario != "pre_link")
        assert result["source"] is (scenario in {"pre_link", "post_link"})
        assert _snapshot_receipts(Path(data["root"]), data["sid"]) == []
        retry = _run_snapshot_claim_node(data, r'''
const m=require("./ctx-guard-core.cjs"),d=JSON.parse(process.argv[1]);
(async()=>{const h=await m.CtxGuardPlugin({directory:d.root,client:{session:{get:async({path:{id}})=>({data:{id}})}}});
const out={system:[]};await h["experimental.chat.system.transform"]({sessionID:d.sid},out);console.log(JSON.stringify(out.system));})();
''')
        assert retry == ([data["payload"]] if scenario == "pre_link" else [])


def _snapshot_claim_real_process_pair(tmp_path, schedule):
    """동일 latest의 atomic rename 승자는 하나며 패배자는 무주입·무receipt로 skip한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — concurrent snapshot consumer 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_concurrent_snapshot_consumers"
    marker_dir = _snapshot_marker_dir(project_root)
    marker_dir.mkdir(parents=True)
    old_generation = "20260811T000000000Z-00000000-0000-4000-8000-000000000001"
    latest_generation = "20260811T000000001Z-00000000-0000-4000-8000-000000000002"
    old_marker = _snapshot_marker(project_root, session_id, old_generation)
    latest_marker = _snapshot_marker(project_root, session_id, latest_generation)
    old_payload = "snapshot-old\n"
    latest_payload = "최신 원문\r\n둘째\r\n"
    old_marker.write_text(old_payload, encoding="utf-8", newline="\n")
    latest_marker.write_text(latest_payload, encoding="utf-8", newline="\n")
    os.utime(old_marker, (1, 1))
    os.utime(latest_marker, (2, 2))
    rendezvous_dir = tmp_path / "snapshot-claim-rendezvous"
    rendezvous_dir.mkdir()

    consumer_script = r'''
const m = require("./ctx-guard-core.cjs"), fs = require("node:fs"), path = require("node:path");
const projectRoot=__PROJECT_ROOT__,markerDir=__MARKER_DIR__,rendezvousDir=__RENDEZVOUS_DIR__;
const sessionID=__SESSION_ID__,consumerID=__CONSUMER_ID__,oldMarker=__OLD_MARKER__,latestMarker=__LATEST_MARKER__;
const schedule=__SCHEDULE__, fixed=path.join(path.dirname(latestMarker),"."+path.basename(latestMarker)+".claimed");
const native={link:fs.linkSync,unlink:fs.unlinkSync,write:fs.writeFileSync};
const trace=[],linkAttempts=[];
let receiptWrites=0,wonLatest=false,lostLatest=false;
const same=(a,b)=>path.resolve(String(a))===path.resolve(b);
const observe=(event,extra={})=>trace.push({event,time:Date.now(),source:fs.existsSync(latestMarker),fence:fs.existsSync(fixed),...extra});
const sleep=(ms)=>Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,ms);
const signal=(name)=>native.write(path.join(rendezvousDir,name),"","utf8");
const wait=(predicate,label)=>{const deadline=Date.now()+5000;while(!predicate()){if(Date.now()>=deadline)throw new Error("rendezvous timeout: "+label);sleep(10);}};
const exists=(name)=>fs.existsSync(path.join(rendezvousDir,name));
const both=(suffix)=>fs.readdirSync(rendezvousDir).filter(n=>n.endsWith(suffix)).length===2;
(async()=>{
  const hooks=await m.CtxGuardPlugin({directory:projectRoot,client:{session:{get:async({path:{id}})=>({data:{id}})}}});
  fs.linkSync=(source,destination)=>{
    if(!same(source,latestMarker)||!same(destination,fixed))throw new Error("claim path differs from fixed generation");
    observe("wrapper-entry",{sourcePath:String(source),destination:String(destination)});
    if(schedule!=="free_run"){
      signal(consumerID+".ready");wait(()=>both(".ready"),"ready");
      if(consumerID==="consumer-b"&&schedule!=="simultaneous"){
        const phase={fence_held:"linked",source_removed:"source-removed",cleanup:"cleaned"}[schedule];
        wait(()=>exists(phase),phase);
      }
    }
    observe("link-api-entry");
    let error=null;
    try{native.link(source,destination);wonLatest=true;linkAttempts.push({ok:true});}
    catch(e){lostLatest=true;error=e;linkAttempts.push({code:e.code,message:e.message});}
    observe("link-api-return",{ok:!error,code:error&&error.code});
    if(schedule!=="free_run"){
      signal(consumerID+".attempted");
      if(wonLatest){signal("linked");if(schedule==="simultaneous"||schedule==="fence_held")wait(()=>both(".attempted"),"attempted");}
    }
    if(error)throw error;
  };
  fs.unlinkSync=(target)=>{
    observe("unlink-api-entry",{target:String(target)});
    try{
      const result=native.unlink(target);observe("unlink-api-return",{target:String(target),ok:true});
      if(schedule!=="free_run"){
        if(same(target,oldMarker))signal("old-gc-done");
        if(same(target,latestMarker)){signal("source-removed");if(schedule==="source_removed")wait(()=>both(".attempted"),"attempted");}
        if(same(target,fixed)){signal("cleaned");if(schedule==="cleanup")wait(()=>both(".attempted"),"attempted");}
      }
      return result;
    }catch(e){observe("unlink-api-return",{target:String(target),code:e.code});throw e;}
  };
  fs.writeFileSync=(target,...args)=>{
    const receipt=path.basename(String(target)).startsWith("compact-snapshot-receipt.");
    if(receipt)observe("receipt-api-entry");
    const result=native.write(target,...args);
    if(receipt){receiptWrites+=1;observe("receipt-api-return");}
    return result;
  };
  if(schedule!=="free_run"&&consumerID==="consumer-b")wait(()=>exists("old-gc-done"),"old-gc-done");
  const output={system:[]};
  try{await hooks["experimental.chat.system.transform"]({sessionID},output);}
  finally{fs.linkSync=native.link;fs.unlinkSync=native.unlink;fs.writeFileSync=native.write;}
  console.log(JSON.stringify({consumerID,wonLatest,lostLatest,linkAttempts,receiptWrites,trace,output:output.system,
    oldExistsAfterTransform:fs.existsSync(oldMarker),sourceAfter:fs.existsSync(latestMarker),fenceAfter:fs.existsSync(fixed)}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''

    def render_consumer(consumer_id: str) -> str:
        return (
            consumer_script.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
            .replace("__MARKER_DIR__", json.dumps(str(marker_dir)))
            .replace("__RENDEZVOUS_DIR__", json.dumps(str(rendezvous_dir)))
            .replace("__SESSION_ID__", json.dumps(session_id))
            .replace("__CONSUMER_ID__", json.dumps(consumer_id))
            .replace("__OLD_MARKER__", json.dumps(str(old_marker)))
            .replace("__LATEST_MARKER__", json.dumps(str(latest_marker)))
            .replace("__SCHEDULE__", json.dumps(schedule))
        )

    processes = [
        subprocess.Popen(
            [_NODE, "-e", render_consumer(consumer_id)],
            cwd=str(project_root / ".opencode" / "lib"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for consumer_id in ("consumer-a", "consumer-b")
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, f"consumer 실패. stdout={stdout!r} stderr={stderr!r}"
        results.append(json.loads(stdout))

    assert sum(result["wonLatest"] for result in results) == 1, results
    if schedule != "free_run":
        assert sum(result["lostLatest"] for result in results) == 1, results
        assert sum(len(result["linkAttempts"]) for result in results) == 2, results
    winner = next(result for result in results if result["wonLatest"])
    loser = next(result for result in results if not result["wonLatest"])
    assert winner["output"] == [latest_payload]
    assert winner["oldExistsAfterTransform"] is False, "선점 승자가 구 generation을 GC하지 않음"
    assert loser["output"] == [], "latest 선점 패배자가 old snapshot을 stale 주입함"
    assert loser["oldExistsAfterTransform"] is False, "latest claim 전에 old GC가 끝나지 않음"
    assert loser["receiptWrites"] == 0, "선점 패배 actor가 receipt를 작성함"
    assert winner["receiptWrites"] == 1
    assert sum(len(result["output"]) for result in results) == 1
    if schedule in {"fence_held", "source_removed", "cleanup"}:
        late = next(row for row in results if row["consumerID"] == "consumer-b")
        entered = next(event for event in late["trace"] if event["event"] == "link-api-entry")
        assert entered["source"] is (schedule == "fence_held"), results
        assert entered["fence"] is (schedule != "cleanup"), results
        assert late["lostLatest"] is True

    assert _snapshot_markers(project_root, session_id) == []
    assert list(marker_dir.glob(".compact-snapshot.*.claimed")) == []
    assert _snapshot_receipts(project_root, session_id) == [
        _snapshot_receipt(project_root, session_id, latest_generation)
    ]

    return results


@pytest.mark.parametrize("schedule", ["simultaneous", "fence_held", "source_removed", "cleanup", "free_run"])
def test_plugin_snapshot_claim_real_process_race_has_one_winner_and_loser_skips_without_receipt(tmp_path, schedule):
    assert _NODE is not None, "Node가 있어야 실제 프로세스 선점을 검증할 수 있다"
    for iteration in range(3 if schedule == "free_run" else 1):
        results = _snapshot_claim_real_process_pair(tmp_path / str(iteration), schedule)
        assert sum(row["wonLatest"] for row in results) == 1, json.dumps(results, ensure_ascii=False)


def test_plugin_consumer_recovers_latest_after_crash_between_old_gc_and_claim(tmp_path):
    """old GC 직후 소비자가 crash한 중간 상태에서도 다음 소비자는 latest를 1회 전달한다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — snapshot pre-claim crash recovery 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_snapshot_preclaim_crash"
    marker_dir = _snapshot_marker_dir(project_root)
    marker_dir.mkdir(parents=True)
    old_generation = "20260811T000000000Z-00000000-0000-4000-8000-000000000021"
    latest_generation = "20260811T000000001Z-00000000-0000-4000-8000-000000000022"
    old_marker = _snapshot_marker(project_root, session_id, old_generation)
    latest_marker = _snapshot_marker(project_root, session_id, latest_generation)
    latest_payload = "snapshot-latest-after-preclaim-crash\n"
    old_marker.write_text("snapshot-old-before-preclaim-crash\n", encoding="utf-8", newline="\n")
    latest_marker.write_text(latest_payload, encoding="utf-8", newline="\n")
    os.utime(old_marker, (1, 1))
    os.utime(latest_marker, (2, 2))

    # A가 관측한 old만 GC한 직후 crash하고 latest claim은 시작하지 않은 상태를 재현한다.
    old_marker.unlink()
    assert _snapshot_markers(project_root, session_id) == [latest_marker]
    assert _snapshot_receipts(project_root, session_id) == []

    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const sessionID = __SESSION_ID__;
const latestPayload = __LATEST_PAYLOAD__;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  const firstOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, firstOutput);
  assert.deepStrictEqual(firstOutput.system, [latestPayload], "surviving latest was not delivered");
  const secondOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, secondOutput);
  assert.deepStrictEqual(secondOutput.system, [], "surviving latest was delivered more than once");
  console.log("JS_SNAPSHOT_PRECLAIM_CRASH_RECOVERY_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    ).replace("__LATEST_PAYLOAD__", json.dumps(latest_payload))
    out = _run_node_check(script, project_root)
    assert "JS_SNAPSHOT_PRECLAIM_CRASH_RECOVERY_OK" in out, (
        f"snapshot pre-claim crash 복구 실패. out={out!r}"
    )
    assert _snapshot_markers(project_root, session_id) == []
    assert list(marker_dir.glob(".compact-snapshot.*.claimed")) == []
    assert _snapshot_receipts(project_root, session_id) == [
        _snapshot_receipt(project_root, session_id, latest_generation)
    ]


def test_plugin_staging_prunes_receipts_to_latest_eight_without_new_receipt(tmp_path):
    """성공한 staging은 같은 SID receipt만 최신 8개로 줄이고 자체 receipt는 만들지 않는다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — snapshot receipt GC 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_receipt_gc"
    other_session_id = "ses_receipt_gc_other"
    marker_dir = _snapshot_marker_dir(project_root)
    marker_dir.mkdir(parents=True)
    for index in range(10):
        receipt = _snapshot_receipt(project_root, session_id, f"g{index:02d}")
        receipt.write_text("", encoding="utf-8")
        # GC 순서가 파일 mtime/경과시간이 아니라 generation과 개수에만 의존하는지 고정한다.
        os.utime(receipt, (100 - index, 100 - index))
    other_receipt = _snapshot_receipt(project_root, other_session_id, "g00")
    other_receipt.write_text("", encoding="utf-8")

    script = r'''
const m = require("./ctx-guard-core.cjs");
const projectRoot = __PROJECT_ROOT__;
const sessionID = __SESSION_ID__;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  console.log("JS_SNAPSHOT_RECEIPT_GC_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    )
    out = _run_node_check(script, project_root)
    assert "JS_SNAPSHOT_RECEIPT_GC_OK" in out, f"snapshot receipt GC 실패. out={out!r}"

    receipts = _snapshot_receipts(project_root, session_id)
    assert [receipt.name.rsplit(".", 1)[-1] for receipt in receipts] == [
        f"g{index:02d}" for index in range(2, 10)
    ]
    assert len(receipts) == 8
    assert len(_snapshot_markers(project_root, session_id)) == 1
    assert other_receipt.exists(), "receipt GC가 다른 safeKey를 삭제함"


def test_plugin_receipt_write_failure_does_not_block_in_memory_delivery(tmp_path):
    """receipt 쓰기 실패는 이미 수행된 system push와 owned marker 정리를 되돌리지 않는다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — snapshot receipt IO fail-soft 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    session_id = "ses_receipt_io_failure"
    marker_dir = _snapshot_marker_dir(project_root)
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const projectRoot = __PROJECT_ROOT__;
const markerDir = __MARKER_DIR__;
const sessionID = __SESSION_ID__;
const markerPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot.${sessionID}.`));
const receiptPaths = () => fs.readdirSync(markerDir)
  .filter((name) => name.startsWith(`compact-snapshot-receipt.${sessionID}.`));

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async ({path:{id}}) => ({data:{id}}),
  }}, directory:projectRoot});
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  assert.strictEqual(markerPaths().length, 1, "snapshot was not staged");

  const originalWriteFileSync = fs.writeFileSync;
  fs.writeFileSync = (...args) => {
    if (String(args[0]).includes("compact-snapshot-receipt.")) {
      throw new Error("synthetic receipt write failure");
    }
    return originalWriteFileSync(...args);
  };
  const output = {system:[]};
  try {
    await hooks["experimental.chat.system.transform"]({sessionID}, output);
  } finally {
    fs.writeFileSync = originalWriteFileSync;
  }
  assert.strictEqual(output.system.length, 1, "receipt IO failure blocked system delivery");
  assert.deepStrictEqual(markerPaths(), [], "receipt IO failure blocked marker cleanup");
  assert.deepStrictEqual(receiptPaths(), [], "failed receipt write left a receipt");
  console.log("JS_SNAPSHOT_RECEIPT_IO_FAIL_SOFT_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__MARKER_DIR__", json.dumps(str(marker_dir))
    ).replace("__SESSION_ID__", json.dumps(session_id))
    out = _run_node_check(script, project_root)
    assert "JS_SNAPSHOT_RECEIPT_IO_FAIL_SOFT_OK" in out, (
        f"snapshot receipt IO fail-soft 실패. out={out!r}"
    )


def test_plugin_snapshot_marker_io_failure_is_fail_soft(tmp_path):
    """ctx-stop 경로가 디렉터리가 아니어도 event/transform은 예외 없이 기존 memory 경로로 간다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — durable snapshot IO fail-soft 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    marker_parent = _snapshot_marker_dir(project_root)
    marker_parent.parent.mkdir(parents=True)
    marker_parent.write_text("directory collision", encoding="utf-8")
    session_id = "ses_marker_io_failure"
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const projectRoot = __PROJECT_ROOT__;
const sessionID = __SESSION_ID__;

(async () => {
  const client = {session:{get: async ({path:{id}}) => ({data:{id}})}};
  const hooks = await m.CtxGuardPlugin({client, directory:projectRoot});
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  const output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, output);
  assert.strictEqual(output.system.length, 1, "IO failure blocked in-memory delivery");

  const restarted = await m.CtxGuardPlugin({client, directory:projectRoot});
  const freshOutput = {system:[]};
  await restarted["experimental.chat.system.transform"]({sessionID}, freshOutput);
  assert.deepStrictEqual(freshOutput.system, [], "failed marker path produced phantom payload");
  console.log("JS_DURABLE_SNAPSHOT_IO_FAIL_SOFT_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__SESSION_ID__", json.dumps(session_id)
    )
    out = _run_node_check(script, project_root)
    assert "JS_DURABLE_SNAPSHOT_IO_FAIL_SOFT_OK" in out, f"marker IO fail-soft 실패. out={out!r}"
    assert marker_parent.is_file()
    assert not _snapshot_receipts(project_root, session_id), "staging 실패가 receipt를 생성함"


def test_plugin_child_session_neither_stages_nor_consumes_snapshot_marker(tmp_path):
    """면제 자식은 compaction marker를 만들지 않고 이미 있는 marker도 소비하지 않는다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — child durable snapshot exemption 단위 skip")

    project_root = _make_ctx_guard_project(tmp_path)
    staged_child_id = "ses_child_no_marker"
    preserved_child_id = "ses_child_keep_marker"
    preserved_marker = _snapshot_marker(
        project_root,
        preserved_child_id,
        "20260811T000000000Z-00000000-0000-4000-8000-000000000000",
    )
    preserved_marker.parent.mkdir(parents=True)
    preserved_marker.write_text("must remain", encoding="utf-8")
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const projectRoot = __PROJECT_ROOT__;
const stagedChildID = __STAGED_CHILD_ID__;
const preservedChildID = __PRESERVED_CHILD_ID__;
const markerDir = __MARKER_DIR__;
const preservedMarker = __PRESERVED_MARKER__;

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{get: async ({path:{id}}) => ({
    data:{id, parentID:"ses_parent"},
  })}}, directory:projectRoot});
  await hooks.event({event:{type:"session.compacted", properties:{sessionID:stagedChildID}}});
  const stagedMarkers = fs.readdirSync(markerDir)
    .filter((name) => name.startsWith(`compact-snapshot.${stagedChildID}.`));
  assert.deepStrictEqual(stagedMarkers, [], "child compaction staged marker");

  const output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:preservedChildID}, output);
  assert.deepStrictEqual(output.system, [], "child transform injected durable marker");
  assert.strictEqual(fs.readFileSync(preservedMarker, "utf8"), "must remain", "child consumed marker");
  console.log("JS_CHILD_DURABLE_SNAPSHOT_EXEMPT_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__STAGED_CHILD_ID__", json.dumps(staged_child_id)
    ).replace("__PRESERVED_CHILD_ID__", json.dumps(preserved_child_id)).replace(
        "__MARKER_DIR__", json.dumps(str(_snapshot_marker_dir(project_root)))
    ).replace("__PRESERVED_MARKER__", json.dumps(str(preserved_marker)))
    out = _run_node_check(script, project_root)
    assert "JS_CHILD_DURABLE_SNAPSHOT_EXEMPT_OK" in out, (
        f"child durable snapshot exemption 실패. out={out!r}"
    )


def test_create_compaction_checkpoint_passes_boundary_and_post_phase():
    """경계 ID·post phase·세션 ID 가 엔진 argv 에 보존되고 cwd 는 넘기지 않는다(AT-018)."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — checkpoint argv 단위 skip")
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
let seen = null;
m.createCompactionCheckpoint("/tmp/project", "sid-1", "opencode-sid-1-3",
  (command, args) => {
    if (args[args.length-1].endsWith("python_floor.py")) return {status:0, stdout:"Python 3.12\n"};
    seen = {command, args}; return {status:0, stdout:""};
  });
assert.ok(seen, "spawn was not called");
const engine = seen.args.findIndex((arg) => arg.endsWith("pm_log.py"));
assert.deepStrictEqual(seen.args.slice(engine + 1), ["checkpoint", "--trigger", "compaction",
  "--phase", "post", "--session-id", "sid-1", "--boundary-id", "opencode-sid-1-3"]);
assert.ok(!seen.args.includes("--cwd"));
console.log("JS_COMPACTION_BOUNDARY_ARGV_OK");
'''
    out = _run_node_check(script)
    assert "JS_COMPACTION_BOUNDARY_ARGV_OK" in out


def test_t0953_compaction_snapshot_uses_session_id_and_stages_failure_reason():
    """snapshot 은 세션 ID 로 부르고 실패면 엔진 사유를 실패 신호 첫 줄로 돌려준다(AT-019)."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — snapshot argv 단위 skip")
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
function fakeWith(result, seen) {
  return (command, args, options) => {
    if (args[args.length-1].endsWith("python_floor.py")) return {status:0, stdout:"Python 3.12\n"};
    seen.push({args, options}); return result;
  };
}
const seen = [];
const ok = m.buildCompactionSnapshot("/tmp/project", "ses_x",
  fakeWith({status:0, stdout:"## PM 정체성 (compaction 복구)\n", stderr:""}, seen));
const engine = seen[0].args.findIndex((arg) => arg.endsWith("pm_log.py"));
assert.deepStrictEqual(seen[0].args.slice(engine + 1), ["snapshot", "--session-id", "ses_x"]);
assert.deepStrictEqual(seen[0].options.stdio, ["ignore", "pipe", "pipe"]);
assert.deepStrictEqual(ok, {ok:true, text:"## PM 정체성 (compaction 복구)\n"});
const failed = m.buildCompactionSnapshot("/tmp/project", "ses_x", fakeWith({status:1, stdout:"",
  stderr:"a\n[pm-snapshot] 세션 task 미해소: 이 세션이 연 task 없음 (세션 ses_x). 재주입 생략.\n"}, []));
assert.strictEqual(failed.ok, false);
assert.ok(failed.text.startsWith("[ctx-checkpoint] 압축 기록 실패: [pm-snapshot] 세션 task 미해소"),
  failed.text);
console.log("JS_COMPACTION_SNAPSHOT_SESSION_OK");
'''
    out = _run_node_check(script)
    assert "JS_COMPACTION_SNAPSHOT_SESSION_OK" in out


def test_compaction_boundary_id_remains_unique_after_plugin_restart():
    """같은 SID·같은 millisecond에도 재로드 뒤 UUID 축이 달라 marker를 재사용하지 않는다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — compaction restart boundary 단위 skip")
    script = r'''
const assert = require("node:assert");
const modulePath = require.resolve("./ctx-guard-core.cjs");
const RealDate = Date;
global.Date = class extends RealDate {
  constructor() { super("2026-08-11T00:00:00.000Z"); }
};
const firstModule = require(modulePath);
const first = firstModule.createCompactionBoundaryID("same-session");
delete require.cache[modulePath];
const restartedModule = require(modulePath);
const second = restartedModule.createCompactionBoundaryID("same-session");
assert.match(first, /^opencode-\d{8}T\d{6}\d{3}Z-[0-9a-f-]{36}-same-session$/);
assert.notStrictEqual(first, second, "plugin restart reused a compaction boundary ID");
console.log("JS_COMPACTION_RESTART_BOUNDARY_OK");
'''
    out = _run_node_check(script)
    assert "JS_COMPACTION_RESTART_BOUNDARY_OK" in out


def test_child_compaction_is_classified_before_any_snapshot_subprocess(tmp_path):
    """면제 자식의 session.compacted는 동기 pm_log snapshot/checkpoint를 한 번도 실행하지 않는다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — child compaction subprocess 단위 skip")
    project_root = _make_ctx_guard_project(tmp_path)
    probe = project_root / "pm-log-was-run"
    engine = project_root / ".project_manager" / "tools" / "pm_log.py"
    engine.write_text(
        "from pathlib import Path\n"
        f"Path({str(probe)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    script = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const projectRoot = __PROJECT_ROOT__;
const probe = __PROBE__;
const sessionID = "ses_child_no_snapshot";

(async () => {
  const hooks = await m.CtxGuardPlugin({client:{session:{
    get: async () => ({data:{id:sessionID, parentID:"ses_parent"}}),
  }}, directory:projectRoot});
  await hooks.event({event:{type:"session.compacted", properties:{sessionID}}});
  assert.strictEqual(fs.existsSync(probe), false, "child compaction spawned pm_log");
  console.log("JS_CHILD_COMPACTION_NO_SUBPROCESS_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root))).replace(
        "__PROBE__", json.dumps(str(probe))
    )
    out = _run_node_check(script, project_root)
    assert "JS_CHILD_COMPACTION_NO_SUBPROCESS_OK" in out


def test_plugin_stages_compacted_notice_after_child_lookup_before_event_hook_settles(tmp_path):
    """자식 판정을 먼저 하되 즉시 해소된 메인 조회는 첫 transform 전에 안내를 적재한다.

    upstream opencode v1.18.5 ``plugin/index.ts`` 의 event dispatcher는 각 plugin event hook을
    await하지 않는다. snapshot이 자식 판정 뒤임을 정적으로 단언하고, 즉시 해소되는 메인 조회와
    pending toast 사이에도 transform이 안내를 소비할 수 있는지 동적으로 검증한다.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — non-awaited compacted event 경합 단위 skip")

    src = _plugin_src()
    branch = src.index('if (event.type === "session.compacted")')
    child_check = src.index("if (await isChildSession", branch)
    snapshot = src.index("buildCompactionSnapshot(root, sessionID)", branch)
    assert child_check < snapshot, "자식 판정 전에 동기 snapshot subprocess를 실행함"

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
  assert.ok(output.system[0].includes("## PM 정체성 (compaction 복구)"));

  resolveToast();
  await eventPromise;
  console.log("JS_COMPACTED_SYNC_STAGE_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script, project_root)
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
  assert.ok(output.system[0].includes("## PM 정체성 (compaction 복구)"), "old message overwrote snapshot");

  await hooks.event(nudgeEvent());
  const freshOutput = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID}, freshOutput);
  assert.strictEqual(freshOutput.system.length, 1, "old message dirtied re-armed fired state");
  assert.ok(freshOutput.system[0].includes("[ctx-nudge]"), "fresh-cycle nudge did not fire");
  console.log("JS_CYCLE_EPOCH_GUARD_OK");
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''.replace("__PROJECT_ROOT__", json.dumps(str(project_root)))
    out = _run_node_check(script, project_root)
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
    out = _run_node_check(script, project_root)
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
    out = _run_node_check(script, project_root)
    assert "JS_NUDGE_SESSION_ISOLATED_OK" in out, f"nudge 세션 격리 실패. out={out!r}"


def test_plugin_emits_nudge():
    """넛지(이른 경고) 경로가 있다 — checkpoint_at_pct 이상이면 안내(toast/message)."""
    src = _plugin_src()
    assert "nudge" in src.lower(), "넛지 로직 없음"
    # toast 경고 경로 (best-effort).
    assert "showToast" in src or "toast" in src.lower(), "넛지 안내(toast) 경로 없음"


# ── 3. 순수 결정 로직 자가검증 (node 있으면) ─────────────────────────────────

_NODE = shutil.which("node")


def _run_node_check(
    script: str,
    project_root: Path | None = None,
    node_args: list[str] | None = None,
) -> str:
    # cwd = core lib 디렉토리 → 스크립트의 `require("./ctx-guard-core.cjs")` 가 해소된다.
    # project_root 를 주면 그 픽스처의 설치 사본(`<root>/.opencode/lib/`)에서 돌린다 —
    # 팩토리가 쓰는 엔진 루트가 픽스처 루트여야 산출물이 소스 트리에 남지 않는다.
    if project_root is not None:
        # 분모 = 기본 입력(사용 한도 200,000)으로 적용한 압축 한도 기록. 메시지에는 기록 모델의
        # providerID·modelID(opencode AssistantMessage 실제 필드)를 싣는다.
        from test_harness_ctx_limits import install_ctx_record
        install_ctx_record(project_root, harness="opencode", capacity=2000000)
        script=re.sub(r'role\s*:\s*(["\'])assistant\1',
            'modelID:"glm-5.2:cloud", providerID:"ollama", role:"assistant"',script)
    proc = subprocess.run(
        [_NODE, "-e", script, *(node_args or [])],
        cwd=str(CORE_DIR if project_root is None else project_root / ".opencode" / "lib"),
        capture_output=True,
        timeout=30,
    )
    if proc.returncode:
        return ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8")
    return proc.stdout.decode("utf-8")


def test_plugin_pure_logic_node_selfcheck():
    """node 로 plugin 의 순수 결정 로직을 자가검증 (이벤트/opencode 런타임 없이).

    검증: 임계 분기(nudge/stop 경계·쓴 %) · 토큰 누적 · 비율 기본값 대체 함수 부재(T-0943).
    node 부재 시 skip (정적 검증으로 게이트).
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — plugin 순수 로직 자가검증 skip (정적 검증만 적용)")

    script = r"""
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");

// export 표면 (테스트 가능한 순수 함수가 떼어져 있어야 한다).
for (const fn of ["parseLocalConf","readBudget","accumulateTokens","computeCtxState","CtxGuardPlugin"]) {
  assert.strictEqual(typeof m[fn], "function", "missing export: " + fn);
}
for (const removed of ["resolveThresholds","resolveBudget","CHECKPOINT_AT_PCT_DEFAULT","CTX_WINDOW_TOKENS_DEFAULT"]) {
  assert.strictEqual(m[removed], undefined, "removed export present: " + removed);
}

// 토큰 누적.
assert.strictEqual(m.accumulateTokens({input:100,output:20,reasoning:5,cache:{read:10,write:3}}), 138);
assert.strictEqual(m.accumulateTokens(null), 0);

// ctx 상태 판정 (limit 1000, 쓴 % 80/90 명시 임계 · nudge2 = max(87, 80) = 87 · 경계 검증용).
const t = {checkpoint_at_pct:80, precompact_at_pct:90};
assert.strictEqual(m.computeCtxState(500, 1000, t).level, "ok");    // 쓴 50%
assert.strictEqual(m.computeCtxState(800, 1000, t).level, "nudge"); // 쓴 80% (경계·>=)
assert.strictEqual(m.computeCtxState(850, 1000, t).level, "nudge"); // 쓴 85%
assert.strictEqual(m.computeCtxState(900, 1000, t).level, "stop");  // 쓴 90% (경계·>=)
assert.strictEqual(m.computeCtxState(950, 1000, t).level, "stop");  // 쓴 95%
assert.strictEqual("remainingPct" in m.computeCtxState(500, 1000, t), false); // 남은 % 는 없다

console.log("NODE_SELFCHECK_OK");
"""
    out = _run_node_check(script)
    assert "NODE_SELFCHECK_OK" in out, f"node 순수 로직 자가검증 실패. out={out!r}"


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
    assert 'buildEngineCtxGuidance(root, "nudge", state, t)' in src
    assert "session.pendingNudgeText = guidance" in src, "nudge 감지 시 중앙 안내 세팅 누락"


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
const g = m.buildNudgeGuidance({ usedPct: 82 });
assert.ok(g.includes("ctx-nudge"), "ctx-nudge 누락");
assert.ok(g.includes("컨텍스트 사용 82%"), "쓴 % 누락: " + g);
assert.ok(!g.includes("잔여"), "남은 % 가 남았다: " + g);
assert.ok(g.includes("ticket 경계"), "ticket 경계 마무리 안내 누락");
assert.ok(g.includes("complete entry"), "complete 박제 안내 누락");
assert.ok(g.includes("python3 .project_manager/tools/pm_log.py checkpoint --task <이름>"), "실 checkpoint 명령 누락");
console.log("JS_NUDGE_GUIDANCE_OK");
"""
    out = _run_node_check(script)
    assert "JS_NUDGE_GUIDANCE_OK" in out, f"JS buildNudgeGuidance 검증 실패. out={out!r}"


def test_js_engine_failure_fallback_keeps_continuity_policy_in_all_bands(tmp_path):
    """중앙 pm_log 호출 실패 시 네 band fallback도 필수 정책 두 사실을 보존한다.

    실패 형상은 *엔진이 그 자리에 없는* 루트다(pm_log.py 없는 픽스처 → spawn rc!=0).
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — ctx fallback 순수 단위 skip")

    engineless_root = tmp_path / "no-engine"
    engineless_root.mkdir()
    script = r"""
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const root = process.argv[1];
const state = {usedPct:92};
const thresholds = {checkpoint_at_pct:70, precompact_at_pct:80};
for (const band of ["nudge", "nudge2", "final", "precompact"]) {
  const guidance = m.buildEngineCtxGuidance(root, band, state, thresholds);
  assert.ok(guidance.includes("압축은 자동이고 세션은 그대로 이어진다"), band + ": continuity missing");
  assert.ok(guidance.includes("핸드오프는 사용자 명시 지시로만 한다"), band + ": handoff policy missing");
  assert.ok(!guidance.includes("새 큰 작업보다 현재 서사 기록을 우선"), band + ": forbidden phrase present");
}
console.log("JS_CTX_FALLBACK_POLICY_OK");
"""
    out = _run_node_check(script, node_args=[str(engineless_root)])
    assert "JS_CTX_FALLBACK_POLICY_OK" in out


# ── checkpoint 2단(strong·compaction 임박) — 정적 + node 순수 검증 ────────────
# 2단 임계는 max(precompact_at − 3, checkpoint_at) 파생. fired.nudge2 로 사이클당 1회·1단과 독립.


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
    assert 'buildEngineCtxGuidance(root, "nudge2", state, t)' in src
    assert "session.pendingNudgeText = guidance" in src, "nudge2 감지 시 중앙 안내 세팅 누락"
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

// nudge2Threshold = max(precompact_at − 3, checkpoint_at) 파생 (checkpoint_at 이 하한).
assert.strictEqual(m.nudge2Threshold({checkpoint_at_pct:70, precompact_at_pct:80}), 77);
assert.strictEqual(m.nudge2Threshold({checkpoint_at_pct:80, precompact_at_pct:90}), 87);
assert.strictEqual(m.nudge2Threshold({checkpoint_at_pct:79, precompact_at_pct:80}), 79); // nudge 밴드 좁으면 하한.

// computeCtxState 4-밴드 (limit 1000, 70/80 → nudge2_threshold 77).
const t = {checkpoint_at_pct:70, precompact_at_pct:80};
assert.strictEqual(m.computeCtxState(500, 1000, t).level, "ok");     // 쓴 50%
assert.strictEqual(m.computeCtxState(700, 1000, t).level, "nudge");  // 쓴 70% (nudge 경계·>=)
assert.strictEqual(m.computeCtxState(760, 1000, t).level, "nudge");  // 쓴 76% (<77)
assert.strictEqual(m.computeCtxState(770, 1000, t).level, "nudge2"); // 쓴 77% (nudge2 경계·>=)
assert.strictEqual(m.computeCtxState(790, 1000, t).level, "nudge2"); // 쓴 79%
assert.strictEqual(m.computeCtxState(800, 1000, t).level, "stop");   // 쓴 80% (stop 경계·>=)

// buildNudge2Guidance 문구 (compaction 임박 checkpoint 지시).
const g = m.buildNudge2Guidance({usedPct:82});
assert.ok(g.includes("ctx-nudge/강화"), "ctx-nudge/강화 누락: " + g);
assert.ok(g.includes("컨텍스트 사용 82%"), "쓴 % 누락: " + g);
assert.ok(g.includes("python3 .project_manager/tools/pm_log.py checkpoint --task <이름>"), "실 checkpoint 명령 누락");
assert.ok(g.includes("구간·서사"), "checkpoint 서사 박제 지시 누락");
// 1단 문구와 구별된다 (별개 강도 표지).
const g1 = m.buildNudgeGuidance({usedPct:82});
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
    for name in ("ctx-guard-core.cjs", "delegate-channel-core.cjs", "warning-channel-core.cjs"):
        shutil.copy2(CORE_DIR / name, oc / "lib" / name)
    if plugin_body is None:
        shutil.copy2(PLUGIN_FILE, oc / "plugins" / "ctx-guard.js")
    else:
        (oc / "plugins" / "ctx-guard.js").write_text(plugin_body, encoding="utf-8")
    return root


@_live_skip
def test_live_opencode_loads_ctx_guard_plugin(tmp_path):
    """실 opencode 헤드리스가 출하 ctx-guard 플러그인을 autoload 성공하는지 실측 (T-0283 핵심 게이트).

    단언: (1) "failed to load plugin" 부재 (2) "Plugin export is not a function" 부재
    (3) factory 실행 마커 존재 = 플러그인이 실제로 로드되고 팩토리가 돌았다. 유닛 green 만으론
    못 잡던 갭(실 세션 로드 실패)을 이 게이트가 닫는다.
    """
    td = tmp_path / "live-load"
    td.mkdir()
    _stage_opencode_project(td)
    log = _run_opencode_load(str(td))
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
def test_live_gate_rejects_broken_object_export(tmp_path):
    """sensitivity 대조군: T-0283 원 버그 형태(CJS 객체 export)를 심으면 이 게이트가 *실제로* 로드 실패를
    잡는지 실측 — false-green(무조건 통과) 방지. 대조군 진입점만 교체하고 core 는 그대로 둔다.

    이 대조군이 없으면 게이트가 opencode 로그 형식 변화 등으로 조용히 무력화돼도 초록으로 통과할 수 있다.
    """
    broken = (
        "// T-0283 원 버그 재현(대조군) — CJS 객체 export = opencode 가 함수 아니라며 로드 거부.\n"
        'const core = require("../lib/ctx-guard-core.cjs");\n'
        "module.exports = { CtxGuardPlugin: core.CtxGuardPlugin, "
        "parseLocalConf: core.parseLocalConf, accumulateTokens: core.accumulateTokens };\n"
    )
    td = tmp_path / "live-broken"
    td.mkdir()
    _stage_opencode_project(td, plugin_body=broken)
    log = _run_opencode_load(str(td))
    assert _LOAD_FAIL in log or _EXPORT_NOT_FN in log, (
        "게이트 무력화 위험 — 깨진 CJS 객체 export 를 opencode 가 로드 실패로 보고하지 않았다 "
        f"(로그 형식 변화?). 로그 꼬리:\n{log[-2500:]}"
    )
    assert _LOAD_MARKER not in log, (
        f"대조군인데 factory 가 실행됨 — 로드 실패 재현 안 됨. 로그 꼬리:\n{log[-2500:]}"
    )


# ── T-0943 세션 모델 집합 확인과 기록 오류 (AT-008) ───────────────────────────


_T0943_CONF = {"harness.opencode.ctx_window_tokens": "600000", "ctx.checkpoint_at_pct": "80",
               "ctx.precompact_at_pct": "90", "ctx.compact_at_pct": "94"}
_T0943_SCRIPT = r'''
const m = require("./ctx-guard-core.cjs");
const assert = require("node:assert");
const [providerID, modelID, input] = [process.argv[1], process.argv[2], Number(process.argv[3])];
(async () => {
  const toasts = [];
  const hooks = await m.CtxGuardPlugin({client:{
    session:{get: async ({path}) => ({data:{id:path.id}})},
    tui:{showToast: async (toast) => { toasts.push(toast.body.message); }},
  }, directory: process.cwd()});
  const event = {event:{type:"message.updated", properties:{info:{
    sessionID:"t0943", role:"assistant", providerID, modelID, tokens:{input}}}}};
  await hooks.event(event);
  await hooks.event(event);
  const output = {system:[]};
  await hooks["experimental.chat.system.transform"]({sessionID:"t0943"}, output);
  console.log(JSON.stringify({toasts, system: output.system}));
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''


def _t0943_project(tmp_path: Path) -> Path:
    """설치 형상의 최소 트리 — pm_log 는 받은 인자를 그대로 내는 대역(안내 문구에 밴드·% 가 보인다)."""
    root = _make_ctx_guard_project(tmp_path, snapshot=False)
    (root / ".project_manager" / "tools" / "pm_log.py").write_text(
        "import sys\nsys.stdout.write(' '.join(sys.argv[1:]))\n", encoding="utf-8")
    return root


def _t0943_plugin_run(root: Path, provider: str, model: str, tokens: int) -> dict:
    proc = subprocess.run([_NODE, "-e", _T0943_SCRIPT, provider, model, str(tokens)],
                          cwd=str(root / ".opencode" / "lib"), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(_NODE is None, reason="node 없음")
def test_t0943_plugin_recorded_model_nudges_other_model_warns(tmp_path):
    """기록에 있는 `provider/model` 이면 경고 없이 넛지, 다른 모델이면 같은 넛지에 toast 경고 한 번."""
    from test_harness_ctx_limits import install_ctx_record
    root = _t0943_project(tmp_path)
    assert install_ctx_record(root, _T0943_CONF, harness="opencode", capacity=200000) == "ollama/glm-5.2:cloud"
    same = _t0943_plugin_run(root, "ollama", "glm-5.2:cloud", 162000)              # 81% of E 200,000
    assert len(same["system"]) == 1 and "--used-pct 81" in same["system"][0]
    assert not [text for text in same["toasts"] if "[ctx-budget]" in text]
    other = _t0943_plugin_run(root, "openrouter", "glm-5.2:cloud", 162000)
    assert len(other["system"]) == 1 and "--used-pct 81" in other["system"][0]
    warnings = [text for text in other["toasts"] if "[ctx-budget]" in text]
    assert len(warnings) == 1 and "모델이 바뀌었습니다" in warnings[0] and "openrouter/glm-5.2:cloud" in warnings[0]


@pytest.mark.skipif(_NODE is None, reason="node 없음")
def test_t0943_plugin_missing_record_toasts_error(tmp_path):
    """기록이 없으면 toast 로 오류를 알리고(무음 반환 아님) 넛지는 만들지 않는다. 대기 결속 상태는 없다."""
    root = _t0943_project(tmp_path)
    (root / ".project_manager" / "local.conf").write_text("", encoding="utf-8")
    for name in ("local_conf.py", "repo_owned_files.py", "file_lock.py", "console_encoding.py"):
        shutil.copyfile(REPO / ".project_manager" / "tools" / name, root / ".project_manager" / "tools" / name)
    result = _t0943_plugin_run(root, "ollama", "glm-5.2:cloud", 190000)
    assert result["system"] == []
    errors = [text for text in result["toasts"] if "[ctx-budget]" in text]
    assert len(errors) == 1 and "압축 한도 기록이 없습니다" in errors[0] and "`pm_config.py apply`" in errors[0]
    assert "pendingBandBinding" not in _plugin_src() and "observedIdentity" not in _plugin_src()
