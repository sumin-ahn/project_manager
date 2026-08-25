"""opencode 어댑터 대용량 write/edit 신뢰성 가드 정합 테스트 (T-0334).

opencode 1.17.x~1.18.x 는 대용량 write/edit 를 조용히 절단·유실한다(OUTPUT_TOKEN_MAX=32000
하드코딩·silent truncation·auto-continue 부재·upstream #18108/#19604/#17471 미해결). 어댑터층
3층 가드(safe-write plugin)로 닫는다:
  ① deny-and-redirect (tool.execute.before): write/edit 대형 args 를 DENY_BYTES 초과 시 throw —
     에러 메시지가 모델-facing 행동 지시(기존 파일=edit 로 나눠라 / 신규=safe_write chunk).
  ② safe_write custom tool: 신규-대형-파일 갭 전용. 16KB chunk 상한 강제·create→append 순.
  ③ 출력 상한 config: opencode.jsonc `limit.output` 명시.

여러 층위에서 단언한다:
  1. 파일 존재 + ESM 로드 규약(진입점 shim = 팩토리 단일 named-export·core 는 lib/ CJS·T-0283).
  2. core 상수(DENY=64KB·CHUNK=16KB·T-opencode-002) + env override 키 정적 참조.
  3. opencode.jsonc `limit.output` 명시(왜: 32000 하드코딩 fallback) + 기존 config 무회귀.
  4. engine.manifest 등재(plugins/·lib/ 디렉토리 엔트리가 신규 파일 커버·전파 drift 0).
  5. (node 있으면) core 순수 로직 자가검증 — deny 발화(write/edit >64KB)·safe_write create/
     append/상한거부(16KB 경계·multibyte·code)/기존파일 create 거부(read 후 append 재개 안내)·
     env override·endsWithNewline 경계 피드백. node 부재 시 skip(정적 검증만).

로드 규약(실측 T-0283): `.opencode/plugins/` 각 파일의 export 를 순회해 *모두 함수*이길 요구하고
각각을 팩토리로 호출한다. 그래서 진입점 `plugins/safe-write.js` 는 ESM 으로 팩토리 하나만 export
하는 얇은 shim 이고, 순수 헬퍼·상수·팩토리 본체는 plugins/ *바깥* CJS 모듈 `lib/safe-write-core.cjs`
(opencode 미스캔·node require 대상)에 둔다. custom tool 헬퍼(@opencode-ai/plugin 의 tool·zod)는 shim
이 import 해 makeSafeWritePlugin(tool) 로 주입한다 — core 는 그 패키지를 직접 require 하지 않는다
(그 패키지는 opencode 런타임에만 설치·node 자가검증 보존).

JS 로직 실동작(실제 deny 강제·모델 도달·chunk 재시도)은 라이브 smoke(격리+--pure+--dir+--format
json·glm-5.2)로 별도 실증 — 티켓 메모 기록.
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
PLUGIN_FILE = PLUGIN_DIR / "safe-write.js"  # ESM 진입점 shim (opencode autoload 대상).
CORE_DIR = OPENCODE / "lib"
CORE_FILE = CORE_DIR / "safe-write-core.cjs"  # 순수 로직·팩토리 본체 (CJS·node require 대상).

MANIFEST = REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"


# ── jsonc 파서 (test_opencode_ctx_guard 선례 동일) ───────────────────────────

def _strip_jsonc_comments(text: str) -> str:
    out_lines = []
    for line in text.splitlines():
        m = re.search(r"(?<!:)//", line)
        if m:
            line = line[: m.start()]
        out_lines.append(line)
    return "\n".join(out_lines)


def _load_config() -> dict:
    return json.loads(_strip_jsonc_comments(PROJECT_CONFIG.read_text(encoding="utf-8")))


def _core_src() -> str:
    return CORE_FILE.read_text(encoding="utf-8")


def _shim_src() -> str:
    return PLUGIN_FILE.read_text(encoding="utf-8")


# ── 1. 파일 존재 + ESM 로드 규약 ─────────────────────────────────────────────

def test_safe_write_files_exist():
    """진입점 shim 이 .opencode/plugins/ 에·core 모듈이 .opencode/lib/ 에 존재 (T-0283 분리 규약)."""
    assert PLUGIN_DIR.is_dir(), f"plugin 디렉토리 없음: {PLUGIN_DIR}"
    assert PLUGIN_FILE.exists(), f"safe-write 진입점 shim 없음: {PLUGIN_FILE}"
    assert CORE_DIR.is_dir(), f"core lib 디렉토리 없음: {CORE_DIR}"
    assert CORE_FILE.exists(), f"safe-write core 모듈 없음: {CORE_FILE}"


def test_safe_write_entry_is_esm_single_function_export():
    """opencode 로드 규약(실측 T-0283): 진입점은 ESM 으로 팩토리 하나만 export 하는 shim 이다.

    durable 정적 가드: 진입점이 (1) CJS module.exports 를 쓰지 않고 (2) core 를 import 하며
    (3) 정확히 하나의 export 문(팩토리 SafeWritePlugin)만 갖는다 (상수/헬퍼 export 금지).
    """
    shim = _shim_src()
    code = "\n".join(ln for ln in shim.splitlines() if not ln.lstrip().startswith("//"))
    assert "module.exports" not in code, (
        "진입점이 CJS module.exports 사용 (T-0283 회귀 — opencode 가 함수 아니라며 로드 거부)"
    )
    assert re.search(r'import\s+core\s+from\s+["\']\.\./lib/safe-write-core\.cjs["\']', shim), (
        "진입점이 ../lib/safe-write-core.cjs 를 import 하지 않음"
    )
    export_lines = [ln for ln in shim.splitlines() if re.match(r"\s*export\s", ln)]
    assert len(export_lines) == 1, (
        f"진입점 export 문이 정확히 1개가 아님(팩토리 하나만·상수/헬퍼 export 금지): {export_lines}"
    )
    assert "SafeWritePlugin" in export_lines[0], (
        f"진입점의 단일 export 가 SafeWritePlugin 팩토리가 아님: {export_lines[0]!r}"
    )


def test_shim_injects_tool_helper_and_core_stays_pure():
    """custom tool 헬퍼는 shim 에서 import·주입하고, core 는 @opencode-ai/plugin 을 직접 require 안 함.

    core 가 그 패키지를 require 하면 node 자가검증(plain node·그 패키지 미설치)이 깨진다. 그래서
    shim 이 `import { tool } from "@opencode-ai/plugin"` 후 makeSafeWritePlugin(tool) 로 주입한다.
    """
    shim = _shim_src()
    core = _core_src()
    assert re.search(r'import\s*\{\s*tool\s*\}\s*from\s*["\']@opencode-ai/plugin["\']', shim), (
        "shim 이 @opencode-ai/plugin 에서 tool 을 import 하지 않음"
    )
    assert "makeSafeWritePlugin(tool)" in shim, "shim 이 makeSafeWritePlugin(tool) 로 주입하지 않음"
    # core 는 그 패키지를 *require/import* 하지 않는다 (주석 언급은 허용 — 실 구문만 검사).
    assert not re.search(r'(?:require|import)\s*\(?\s*["\']@opencode-ai/plugin["\']', core), (
        "core 가 @opencode-ai/plugin 을 직접 require/import — node 자가검증 깨짐(주입 규약 위반)"
    )


def test_ctx_guard_untouched_by_safe_write():
    """safe-write 추가가 기존 ctx-guard plugin/core 를 건드리지 않았다 (독립 plugin·회귀 방지)."""
    assert (PLUGIN_DIR / "ctx-guard.js").exists(), "ctx-guard 진입점 shim 이 사라짐 (회귀)"
    assert (CORE_DIR / "ctx-guard-core.cjs").exists(), "ctx-guard core 가 사라짐 (회귀)"


# ── 2. core 상수 + env override 정적 참조 ────────────────────────────────────

def test_core_declares_thresholds_and_env_overrides():
    """core 상단에 DENY_BYTES(64KB)·CHUNK_BYTES(16KB) 상수 + env override 키를 명시한다 (T-opencode-002)."""
    src = _core_src()
    assert re.search(r"DENY_BYTES\s*=\s*64\s*\*\s*1024", src), "DENY_BYTES=64KB 상수 없음"
    assert re.search(r"CHUNK_BYTES\s*=\s*16\s*\*\s*1024", src), "CHUNK_BYTES=16KB 상수 없음"
    assert "PM_SAFE_WRITE_DENY_BYTES" in src, "DENY env override 키 없음"
    assert "PM_SAFE_WRITE_CHUNK_BYTES" in src, "CHUNK env override 키 없음"
    # 근거 주석(DoD): sweep 실측 인용 — 89KB 성공·237KB 실패 구간과 65KB byte-exact sweep.
    assert "89KB" in src and "237KB" in src, "DENY 64KB 근거(무가드 89KB 성공·237KB 실패) 주석 없음"
    assert "65KB" in src, "sweep 65KB 까지 byte-exact 근거 주석 없음"


def test_core_declares_guard_and_tool_wiring():
    """deny-and-redirect(tool.execute.before) + safe_write custom tool 배선이 core 에 있다 (정적)."""
    src = _core_src()
    assert "tool.execute.before" in src, "deny-and-redirect 훅(tool.execute.before) 없음"
    assert "safe_write:" in src, "safe_write custom tool 등록 없음"
    assert "makeSafeWritePlugin" in src, "팩토리 커링(makeSafeWritePlugin) 없음"
    # deny 메시지가 두 redirect 대상(edit·safe_write)을 모두 안내한다.
    assert "safe_write" in src, "신규-파일 redirect(safe_write) 안내 없음"
    assert "edit" in src, "기존-파일 redirect(edit) 안내 없음"


def test_core_declares_root_containment():
    """safe_write raw fs 가 root containment(절대·../·symlink 봉쇄)를 강제한다 (정적·codex must-fix).

    safeWrite 가 assertContainedPath(lexical)+assertRealpathContained(realpath symlink 방어)를
    호출하고, realpath 방어에 fs.realpathSync 를 쓰며, 절대경로 거부 문구가 있음을 정적 단언한다.
    """
    src = _core_src()
    assert "function assertContainedPath" in src, "assertContainedPath(lexical containment) 없음"
    assert "function assertRealpathContained" in src, "assertRealpathContained(symlink 방어) 없음"
    assert "function assertLeafNotSymlink" in src, "assertLeafNotSymlink(leaf symlink 거부) 없음"
    assert "realpathSync" in src, "realpath 기반 symlink 탈출 방어 없음"
    assert "lstatSync" in src, "leaf lstat(dangling symlink 방어) 없음"
    # class-fix (codex R3): create 는 exclusive open(O_CREAT|O_EXCL·커널 강제)으로 symlink follow 차단.
    body = src[src.index("function safeWrite") : src.index("function resolvePath")]
    assert re.search(r'openSync\(\s*target\s*,\s*["\']wx["\']', body), (
        "create 가 exclusive openSync(target,'wx') 를 안 씀 — dangling symlink 우회 미봉쇄"
    )
    # R4: append 도 커널 강제 O_NOFOLLOW — lstat 선검사↔write 사이 symlink 교체 TOCTOU 백스톱.
    assert "O_NOFOLLOW" in body, "append 가 O_NOFOLLOW open 을 안 씀 — symlink 교체 TOCTOU 미봉쇄"
    assert "O_APPEND" in body, "append 가 O_APPEND open 아님"
    # safeWrite 가 세 containment 를 실제로 호출한다(선언만 있고 미배선 방지).
    assert "assertContainedPath(root, filePath)" in body, "safeWrite 가 lexical containment 미호출"
    assert "assertRealpathContained(root, target)" in body, "safeWrite 가 realpath containment 미호출"
    assert "assertLeafNotSymlink(target)" in body, "safeWrite 가 leaf symlink 거부 미호출"


def test_core_documents_append_allowed_decision():
    """core 가 '기존 파일 append 의식적 허용'(codex R4 reject·PM 70) 결정을 주석으로 박제한다.

    append-only·chunk 상한·resume 보전이 근거 — deny 정책 목적은 신뢰성(접근통제 아님)임을 명시.
    후속 리뷰어가 '기존파일 append 거부' 를 재도입하려는 회귀를 막는 durable 근거.
    """
    src = _core_src()
    assert "resume" in src, "resume(중단 후 재개) 보전 근거 주석 없음"
    assert "append-only" in src, "append-only(기존 내용 훼손 불가) 근거 주석 없음"
    assert "접근통제" in src, "deny 정책 목적=신뢰성(접근통제 아님) 명시 없음"
    # 절대경로 거부 = 프로젝트 상대경로만 (모델-facing).
    assert "절대경로" in src and "상대경로" in src, "절대경로 거부(상대경로만) 메시지 없음"
    # safe_write tool 의 filePath 설명이 구현(절대경로 거부)과 일치해야 한다 — 모델이 설명 믿고
    # 절대경로 시도하는 mismatch 방지 (codex R2 suggestion 2). "절대 또는 프로젝트 상대" 잔존 금지.
    assert "절대 또는 프로젝트 상대" not in src, (
        "safe_write filePath 설명이 여전히 '절대 또는 프로젝트 상대' — 구현(절대 거부)과 불일치"
    )
    m_desc = re.search(r'filePath:\s*tool\.schema[\s\S]{0,120}?describe\(([^)]*)\)', src)
    assert m_desc and "상대경로만" in m_desc.group(1), (
        f"safe_write filePath 설명이 '프로젝트 상대경로만' 을 명시 안 함: {m_desc and m_desc.group(1)}"
    )


def test_config_output_limit_is_per_model_documented():
    """opencode.jsonc 가 limit 이 *모델별* 설정임을 채택자에게 안내한다 (codex R2 must-fix·PM 부분수용).

    임의 모델 자동 limit 주입은 안 하되(모델별 context 실값 불명·과설계), {{OPENCODE_PRO_MODEL}}
    교체 시 그 모델 엔트리에 limit 을 직접 명시하라는 채택자-facing 안내가 provider 블록 위에 있다.
    """
    raw = PROJECT_CONFIG.read_text(encoding="utf-8")
    prov_idx = raw.index('"provider"')
    head = raw[:prov_idx]
    assert "OPENCODE_PRO_MODEL" in head, "채택자 안내가 모델 교체(OPENCODE_PRO_MODEL) 시나리오를 안 짚음"
    assert "모델별" in head, "limit 이 모델별 설정임을 안내 안 함"
    assert "32000" in head, "미명시 시 32000 fallback 안내 없음"
    # ⚠️ 리터럴 브레이스 토큰 `{{OPENCODE_PRO_MODEL}}` 은 쓰지 않는다 — opencode.jsonc 는 모델 치환
    # 대상이 아니라(agent frontmatter model: 필드만) 잔존 토큰이 pm_import leak 가드를 깨뜨린다.
    assert "{{OPENCODE_PRO_MODEL}}" not in raw, (
        "opencode.jsonc 에 리터럴 브레이스 토큰 잔존 — pm_import 치환 대상 아님(leak)"
    )


# ── 3. opencode.jsonc limit.output 명시 + 기존 config 무회귀 ──────────────────

def test_config_exists_and_parses():
    assert PROJECT_CONFIG.exists(), f"project config 없음: {PROJECT_CONFIG}"
    assert isinstance(_load_config(), dict)


def test_config_declares_output_limit():
    """ollama 모델 엔트리에 limit.output=32768 명시 (T-0334 ③·32000 하드코딩 fallback 고정)."""
    data = _load_config()
    provider = data.get("provider", {})
    assert isinstance(provider, dict) and provider, "opencode.jsonc 에 provider 블록 없음"
    ollama = provider.get("ollama", {})
    models = ollama.get("models", {})
    assert models, "ollama.models 엔트리 없음"
    # 어떤 모델 엔트리든 limit.output 이 명시돼 있어야 한다.
    entries = [m.get("limit", {}) for m in models.values() if isinstance(m, dict)]
    assert any(lim.get("output") == 32768 for lim in entries), (
        f"limit.output=32768 명시 없음 (found: {[lim.get('output') for lim in entries]})"
    )
    # opencode(≥1.17.19)는 model limit 지정 시 context·output 을 둘 다 요구한다(dynamic ollama
    # 모델은 카탈로그 context 부재·라이브 실측) — 유효 config 회귀 방지(context 누락 재발 차단).
    assert all("context" in lim for lim in entries if "output" in lim), (
        f"limit.output 만 있고 context 누락 — opencode config invalid (found: {entries})"
    )
    # 왜(주석) 정적 확인 — 32000 하드코딩 fallback 근거.
    raw = PROJECT_CONFIG.read_text(encoding="utf-8")
    assert "32000" in raw, "limit.output 주석에 32000 하드코딩 fallback 근거 없음"


def test_config_keeps_existing_blocks():
    """safe-write config 추가가 기존 tool_output/permission 가드를 안 깬다 (회귀).

    (자동 컴팩션 형상 불변은 test_opencode_ctx_guard 가 소유 — 여기선 중복하지 않는다.)"""
    data = _load_config()
    assert data.get("permission", {}).get("bash", {}).get("rm *") == "deny", (
        "bash rm 가드 손실 (T-0011 회귀)"
    )
    assert data.get("tool_output", {}).get("max_lines") == 12000, "tool_output 상한 회귀 (T-0289)"


# ── 4. engine.manifest 등재 (전파 drift 0 전제) ──────────────────────────────

def test_manifest_covers_plugins_and_lib_dirs():
    """opencode 매니페스트가 .opencode/plugins·.opencode/lib 디렉토리를 등재 → 신규 파일 커버.

    두 경로는 디렉토리 엔트리(@source·재귀 동기)라 safe-write.js/safe-write-core.cjs 가 별도 행
    없이 전파된다(PM 25 manifest 미등록 재발 방지·pm_update --target opencode drift 0 전제).
    """
    assert MANIFEST.exists(), f"opencode 매니페스트 없음: {MANIFEST}"
    text = MANIFEST.read_text(encoding="utf-8")
    entry_lines = [ln.split()[0] for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert ".opencode/plugins" in entry_lines, "매니페스트에 .opencode/plugins 디렉토리 등재 없음"
    assert ".opencode/lib" in entry_lines, "매니페스트에 .opencode/lib 디렉토리 등재 없음"


# ── 5. node 순수 로직 자가검증 (node 있으면) ─────────────────────────────────

_NODE = shutil.which("node")


def _run_node_check(script: str) -> str:
    return subprocess.run(
        [_NODE, "-e", script],
        cwd=str(CORE_DIR),
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def test_core_requires_cleanly_in_node():
    """node 가 core 모듈을 깨끗이 require 한다 (@opencode-ai/plugin 미설치여도·주입 규약). node 부재 skip."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — require 검증 skip")
    out = _run_node_check('require("./safe-write-core.cjs"); console.log("REQUIRE_OK");')
    assert "REQUIRE_OK" in out, f"core 모듈 require 실패: {out!r}"


def test_node_inherits_pytest_session_tempdir_redirect():
    """자식 node의 os.tmpdir()가 Python 세션 전용 tempdir와 같은 위치를 가리킨다."""
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — tempdir 상속 검증 skip")
    out = _run_node_check(
        'const os = require("node:os"); console.log(os.tmpdir());'
    ).strip()

    assert Path(out).resolve() == Path(tempfile.gettempdir()).resolve()
    assert Path(os.environ["TMPDIR"]).resolve() == Path(out).resolve()


def test_core_pure_logic_node_selfcheck():
    """node 로 core 순수 로직 자가검증 (opencode 런타임 없이·ctx-guard 자가검증 패턴).

    검증: env override · deny 발화(write>16KB new/existing·edit newString+oldString>16KB) ·
    safe_write create/append(누적 보고) · chunk 상한 거부 · 기존파일 create 거부 · append 부재 거부 ·
    fake tool 주입 팩토리 배선(safe_write 등록 + before 훅 deny). node 부재 시 skip.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — 순수 로직 자가검증 skip (정적 검증만 적용)")

    script = r"""
const m = require("./safe-write-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// export 표면.
for (const fn of ["makeSafeWritePlugin","byteLength","resolveDenyBytes","resolveChunkBytes",
                  "checkOversizeWrite","validateSafeWrite","safeWrite",
                  "isWithinRoot","assertContainedPath","assertRealpathContained","assertLeafNotSymlink"]) {
  assert.strictEqual(typeof m[fn], "function", "missing export: " + fn);
}
assert.strictEqual(m.DENY_BYTES, 65536, "DENY_BYTES 기본 64KB 아님");
assert.strictEqual(m.CHUNK_BYTES, 16384, "CHUNK_BYTES 기본 16KB 아님");

// env override (>0 정수만·아니면 기본).
assert.strictEqual(m.resolveDenyBytes({}), 65536);
assert.strictEqual(m.resolveDenyBytes({PM_SAFE_WRITE_DENY_BYTES:"4096"}), 4096);
assert.strictEqual(m.resolveDenyBytes({PM_SAFE_WRITE_DENY_BYTES:"0"}), 65536);   // ≤0→기본
assert.strictEqual(m.resolveDenyBytes({PM_SAFE_WRITE_DENY_BYTES:"1.5"}), 65536); // 비정수→기본
assert.strictEqual(m.resolveChunkBytes({PM_SAFE_WRITE_CHUNK_BYTES:"2048"}), 2048);

// ── ① deny 발화: write 대형 (신규→safe_write 유도 / 기존→edit 유도) ──────────
const big = "x".repeat(70000);
assert.strictEqual(m.checkOversizeWrite("write", {content:"small"}, 65536, false, 16384).deny, false);
let v = m.checkOversizeWrite("write", {content:big}, 65536, false, 16384);
assert.strictEqual(v.deny, true); assert.strictEqual(v.kind, "write-new");
assert.ok(v.message.includes("safe_write"), "신규 파일 메시지가 safe_write 유도 아님: " + v.message);
assert.ok(v.message.includes("16384B"), "신규 파일 메시지가 chunkBytes 값을 안내 안 함: " + v.message);
v = m.checkOversizeWrite("write", {content:big}, 65536, true, 16384);
assert.strictEqual(v.deny, true); assert.strictEqual(v.kind, "write-existing");
assert.ok(v.message.includes("edit"), "기존 파일 메시지가 edit 유도 아님: " + v.message);
// edit: newString+oldString 합 초과.
v = m.checkOversizeWrite("edit", {newString:"y".repeat(40000), oldString:"z".repeat(30000)}, 65536, true, 16384);
assert.strictEqual(v.deny, true); assert.strictEqual(v.kind, "edit");
assert.strictEqual(m.checkOversizeWrite("edit", {newString:"a".repeat(30000), oldString:"b".repeat(30000)}, 65536, true, 16384).deny, false);
// deny 메시지의 chunk 안내가 env override(chunkBytes) 값을 동적 반영 (하드 "16KB" 아님).
const vOverride = m.checkOversizeWrite("write", {content:big}, 65536, false, 4096);
assert.ok(vOverride.message.includes("4096B"), "chunkBytes override 가 메시지에 반영 안 됨: " + vOverride.message);
assert.ok(!vOverride.message.includes("16384B"), "override 인데 기본 16384 가 잔존: " + vOverride.message);
// 경계: 정확히 상한이면 통과(<=)·+1B 는 deny.
assert.strictEqual(m.checkOversizeWrite("write", {content:"z".repeat(65536)}, 65536, false, 16384).deny, false);
assert.strictEqual(m.checkOversizeWrite("write", {content:"z".repeat(65537)}, 65536, false, 16384).deny, true);
// 무관 도구는 무판정.
assert.strictEqual(m.checkOversizeWrite("read", {content:big}, 65536, false, 16384).deny, false);

// ── ② validateSafeWrite (순수) ─────────────────────────────────────────────
assert.strictEqual(m.validateSafeWrite("hi","create",false,16384).ok, true);
assert.strictEqual(m.validateSafeWrite("x".repeat(17000),"create",false,16384).ok, false); // 상한 초과
assert.strictEqual(m.validateSafeWrite("hi","create",true,16384).ok, false);              // 기존 파일 create
assert.strictEqual(m.validateSafeWrite("hi","append",false,16384).ok, false);             // 파일 부재 append
assert.strictEqual(m.validateSafeWrite("hi","bogus",false,16384).ok, false);              // 잘못된 mode
assert.strictEqual(m.validateSafeWrite("z".repeat(16384),"create",false,16384).ok, true); // 경계(<=)
// create-on-existing 거부 메시지에 재개 안내(read 후 append)가 있다 (T-opencode-002).
const vResume = m.validateSafeWrite("hi","create",true,16384);
assert.ok(vResume.message.includes("read"), "재개 안내(read) 없음: " + vResume.message);
assert.ok(vResume.message.includes('mode="append"'), "재개 안내(append) 없음: " + vResume.message);

// ── ② safeWrite fs: create → append (root containment·상대경로·누적 바이트/라인 보고) ──
const td = fs.mkdtempSync(path.join(os.tmpdir(), "safewrite-"));
try {
const fp = path.join(td, "sub", "out.txt");   // 부모 dir 자동 생성 확인.
let r = m.safeWrite(td, "sub/out.txt", "line1\nline2\n", "create", 16384);
assert.strictEqual(r.mode, "create");
assert.strictEqual(r.totalLines, 2, "create 후 줄 수: " + r.totalLines);
assert.strictEqual(r.endsWithNewline, true, "\\n 종단 파일의 endsWithNewline");
r = m.safeWrite(td, "sub/out.txt", "line3\n", "append", 16384);
assert.strictEqual(r.totalLines, 3, "append 후 줄 수: " + r.totalLines);
assert.strictEqual(fs.readFileSync(fp, "utf-8"), "line1\nline2\nline3\n");
assert.ok(r.totalBytes > r.wroteBytes, "누적 바이트가 이번 조각보다 커야");
assert.ok(m.isWithinRoot(td, r.filePath), "결과 경로가 root 안이어야");
// 비종단 조각 → endsWithNewline=false (경계 피드백·T-opencode-002).
r = m.safeWrite(td, "partial.txt", "no-trailing-newline", "create", 16384);
assert.strictEqual(r.endsWithNewline, false);
assert.strictEqual(r.totalLines, 1);
// create on existing → throw (재개 안내 포함).
assert.throws(() => m.safeWrite(td, "sub/out.txt", "z", "create", 16384),
              /이미 존재[\s\S]*read[\s\S]*mode="append"/);
// append on absent → throw.
assert.throws(() => m.safeWrite(td, "nope.txt", "z", "append", 16384), /없다/);
// chunk 상한 초과 → throw.
assert.throws(() => m.safeWrite(td, "big.txt", "x".repeat(20000), "create", 16384), /상한/);

// ── ② CHUNK 16KB 채택 근거 자가검증(T-opencode-002): 정확히 16,384B 완성 args 도달 ──
// ASCII 경계.
const asciiExact = "x".repeat(16384);
assert.strictEqual(Buffer.byteLength(asciiExact, "utf-8"), 16384);
r = m.safeWrite(td, "exact-ascii.txt", asciiExact, "create", 16384);
assert.strictEqual(r.wroteBytes, 16384, "ASCII 정확히 16384B wroteBytes");
assert.strictEqual(fs.readFileSync(path.join(td, "exact-ascii.txt"), "utf-8"), asciiExact);
// UTF-8 multibyte(CJK 3B) 경계 — '가'*5461(16383B) + 'x'(1B) = 정확히 16384B·문자 경계 보존.
const cjkExact = "\uAC00".repeat(5461) + "x";
assert.strictEqual(Buffer.byteLength(cjkExact, "utf-8"), 16384, "CJK 구성 바이트");
r = m.safeWrite(td, "exact-cjk.txt", cjkExact, "create", 16384);
assert.strictEqual(r.wroteBytes, 16384, "multibyte 경계 wroteBytes");
assert.strictEqual(fs.readFileSync(path.join(td, "exact-cjk.txt"), "utf-8"), cjkExact,
                   "multibyte byte-exact");
// quote/backslash 많은 코드 내용 — JSON tool-call args 직렬화 왕복 후 byte-exact 도달.
// (따옴표·백슬래시는 fromCharCode 로 조립해 이스케이프 모호성 제거)
let codeContent = "";
const DQ = String.fromCharCode(34);   // 큰따옴표
const SQ = String.fromCharCode(39);   // 작은따옴표
const BS = String.fromCharCode(92);   // 백슬래시
const codeFrag = "if (a === " + DQ + "b" + DQ + " && c === " + SQ + "d" + SQ +
                 ") { p.q(" + DQ + BS + "n" + DQ + "); } // comment\n";
while (Buffer.byteLength(codeContent, "utf-8") +
       Buffer.byteLength(codeFrag, "utf-8") <= 16384) codeContent += codeFrag;
while (Buffer.byteLength(codeContent, "utf-8") < 16384) codeContent += ";";
assert.strictEqual(Buffer.byteLength(codeContent, "utf-8"), 16384, "code 구성 바이트");
const argsJson = JSON.stringify({ filePath: "exact-code.txt", content: codeContent, mode: "create" });
const parsedArgs = JSON.parse(argsJson);
assert.strictEqual(parsedArgs.content, codeContent, "JSON args 왕복 내용 일치");
r = m.safeWrite(td, parsedArgs.filePath, parsedArgs.content, parsedArgs.mode, 16384);
assert.strictEqual(r.wroteBytes, 16384, "code 경계 wroteBytes");
// 마지막 개행 뒤 ";" 패딩이 붙으므로 비종단(false)이 정상 — 경계 피드백 대상.
assert.strictEqual(r.endsWithNewline, false);

// F1 강제 게이트(T-opencode-002): 비종단 파일+비개행 chunk append 는 거부(처방 포함)·
// 개행으로 시작하는 chunk 는 허용 — line-aligned 계약 강제.
assert.throws(() => m.safeWrite(td, "partial.txt", "more", "append", 16384),
              /개행[\s\S]*첫 문자/);
r = m.safeWrite(td, "partial.txt", "\nmore\n", "append", 16384);
assert.strictEqual(fs.readFileSync(path.join(td, "partial.txt"), "utf-8"),
                   "no-trailing-newline\nmore\n");

// F-006 정적 가드(T-opencode-002): core src 의 mode enum 배선 존재 — schema 회귀 무감시 방지.
const coreSrc = fs.readFileSync(path.join(process.cwd(), "safe-write-core.cjs"), "utf-8");
assert.ok(/create\|[\s]*"append"|enum\(/.test(coreSrc) || /"create".{0,40}"append"/.test(coreSrc), "mode enum 배선이 core src 에 없다");

console.log("SAFE_WRITE_SELFCHECK_OK");
} finally {
  fs.rmSync(td, {recursive:true, force:true});
}
"""
    out = _run_node_check(script)
    assert "SAFE_WRITE_SELFCHECK_OK" in out, f"core 순수 로직 자가검증 실패. out={out!r}"


def test_factory_wiring_node_selfcheck():
    """node 로 fake tool 주입 팩토리 배선 검증 — safe_write 등록 + before 훅 deny/통과 (opencode 없이).

    makeSafeWritePlugin(fakeTool) 로 팩토리를 만들어 hooks.tool.safe_write.execute(create→append)
    와 hooks["tool.execute.before"](대형 deny·소형 통과)를 opencode 런타임 없이 실행한다. node 부재 skip.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — 팩토리 배선 자가검증 skip")

    script = r"""
const m = require("./safe-write-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// opencode 없이 tool 헬퍼 흉내 (identity + zod-like schema stub).
const fakeSchema = {
  string: () => ({ describe: () => ({}) }),
  enum: () => ({ describe: () => ({}) }),   // mode enum 강제(T-opencode-002) stub.
};
const fakeTool = (def) => def; fakeTool.schema = fakeSchema;

(async () => {
  const td = fs.mkdtempSync(path.join(os.tmpdir(), "safewrite-fac-"));
  try {
    const factory = m.makeSafeWritePlugin(fakeTool);
    const hooks = await factory({ directory: td });
    assert.ok(hooks.tool && hooks.tool.safe_write, "safe_write custom tool 미등록");
    assert.strictEqual(typeof hooks.tool.safe_write.execute, "function", "safe_write.execute 없음");
    assert.strictEqual(typeof hooks["tool.execute.before"], "function", "tool.execute.before 훅 없음");

    // custom tool 을 통해 create → append (상대 경로·directory 해소).
    const out1 = await hooks.tool.safe_write.execute({filePath:"doc.txt", content:"a\n", mode:"create"}, {directory:td});
    assert.ok(out1.includes("create ok"), "create 보고 아님: " + out1);
    const out2 = await hooks.tool.safe_write.execute({filePath:"doc.txt", content:"b\n", mode:"append"}, {directory:td});
    assert.ok(out2.includes("append ok"), "append 보고 아님: " + out2);
    assert.strictEqual(fs.readFileSync(path.join(td, "doc.txt"), "utf-8"), "a\nb\n");
    // 종단 파일엔 경계 지시가 없다.
    assert.ok(!out2.includes("끝나지 않는다"), "종단 파일인데 경계 지시 발화: " + out2);

    // 비종단(\n 없는) 조각 → 응답에 다음 조각 첫 문자 \n 지시 포함(T-opencode-002).
    const out3 = await hooks.tool.safe_write.execute(
      {filePath:"part.txt", content:"tail-without-newline", mode:"create"}, {directory:td});
    assert.ok(out3.includes("끝나지 않는다"), "비종단 경계 절 누락: " + out3);
    assert.ok(out3.includes("\\n 으로 시작하라"), "\\n 시작 지시 누락: " + out3);

    // before 훅: 대형 신규 write → throw(safe_write 유도).
    let threw = false;
    try {
      await hooks["tool.execute.before"]({tool:"write"}, {args:{content:"x".repeat(70000), filePath:path.join(td,"new.txt")}});
    } catch (e) { threw = true; assert.ok(e.message.includes("safe_write"), "deny 메시지 아님: " + e.message); }
    assert.ok(threw, "대형 신규 write 가 deny 안 됨");

    // before 훅: 대형 기존 write → throw(edit 유도). 먼저 파일을 만든다.
    fs.writeFileSync(path.join(td, "exists.txt"), "seed");
    let threw2 = false;
    try {
      await hooks["tool.execute.before"]({tool:"write"}, {args:{content:"x".repeat(70000), filePath:path.join(td,"exists.txt")}});
    } catch (e) { threw2 = true; assert.ok(e.message.includes("edit"), "기존 파일 deny 가 edit 유도 아님: " + e.message); }
    assert.ok(threw2, "대형 기존 write 가 deny 안 됨");

    // before 훅: 소형 write → 통과(throw 없음).
    await hooks["tool.execute.before"]({tool:"write"}, {args:{content:"tiny", filePath:path.join(td,"t.txt")}});

    // safe_write 도구 경로도 root containment 강제 — 절대경로·../ 탈출은 tool 을 통해서도 거부.
    let sec1 = false;
    try { await hooks.tool.safe_write.execute({filePath:"/tmp/pwn.txt", content:"x", mode:"create"}, {directory:td}); }
    catch (e) { sec1 = true; assert.ok(e.message.includes("절대경로"), "절대경로 거부 메시지 아님: " + e.message); }
    assert.ok(sec1, "tool 경로가 절대경로를 거부 안 함");
    let sec2 = false;
    try { await hooks.tool.safe_write.execute({filePath:"../escape.txt", content:"x", mode:"create"}, {directory:td}); }
    catch (e) { sec2 = true; }
    assert.ok(sec2, "tool 경로가 ../ 탈출을 거부 안 함");

    console.log("FACTORY_WIRING_OK");
  } finally {
    fs.rmSync(td, {recursive:true, force:true});
  }
})();
"""
    out = _run_node_check(script)
    assert "FACTORY_WIRING_OK" in out, f"팩토리 배선 자가검증 실패. out={out!r}"


def test_root_containment_node_selfcheck():
    """node 로 root containment 보안 가드 검증 (codex must-fix — raw fs write 봉쇄).

    safe_write 의 raw fs 호출이 opencode write 권한계층을 우회하므로, lexical(절대경로·../ 탈출
    거부) + realpath(symlink 탈출 거부) 이중 봉쇄를 강제한다. 정상 상대경로 create/append 는 회귀
    유지. node 부재 skip.
    """
    if _NODE is None:
        import pytest

        pytest.skip("node 없음 — root containment 자가검증 skip")

    script = r"""
const m = require("./safe-write-core.cjs");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

let td;
let outside;
try {
td = fs.mkdtempSync(path.join(os.tmpdir(), "swroot-"));
outside = fs.mkdtempSync(path.join(os.tmpdir(), "swout-"));

// (회귀) 정상 상대경로 create+append 는 통과.
let r = m.safeWrite(td, "sub/ok.txt", "one\n", "create", 16384);
assert.ok(m.isWithinRoot(td, r.filePath), "정상 결과가 root 안이어야");
r = m.safeWrite(td, "sub/ok.txt", "two\n", "append", 16384);
assert.strictEqual(fs.readFileSync(path.join(td,"sub","ok.txt"),"utf-8"), "one\ntwo\n");

// ── lexical: 절대경로 거부 (모델-facing: 프로젝트 상대경로만) ──────────────────
assert.throws(() => m.safeWrite(td, "/tmp/abs.txt", "x", "create", 16384), /절대경로|상대경로/);
assert.throws(() => m.safeWrite(td, path.join(outside,"z.txt"), "x", "create", 16384), /절대경로/);
// ── lexical: ../ 프로젝트 밖 탈출 거부 ──────────────────────────────────────
assert.throws(() => m.safeWrite(td, "../escape.txt", "x", "create", 16384), /벗어나|프로젝트/);
assert.throws(() => m.safeWrite(td, "a/../../escape.txt", "x", "create", 16384), /벗어나|프로젝트/);

// ── realpath: symlink 디렉터리 탈출 거부 (create) ────────────────────────────
fs.symlinkSync(outside, path.join(td, "link"));
assert.throws(() => m.safeWrite(td, "link/file.txt", "x", "create", 16384), /밖을 가리|symlink/);

// ── realpath: symlink 파일 탈출 거부 (append) — 프로젝트 밖 victim 을 보호 ────
const victim = path.join(outside, "victim.txt");
fs.writeFileSync(victim, "orig\n");
fs.symlinkSync(victim, path.join(td, "evil.txt"));
assert.throws(() => m.safeWrite(td, "evil.txt", "PWNED\n", "append", 16384), /밖을 가리|symlink/);
assert.strictEqual(fs.readFileSync(victim,"utf-8"), "orig\n", "victim 이 변조되면 안 됨");

// ── R4: append 대상이 symlink 로 교체된 경우 거부 (leaf lstat 메시지 유지·O_NOFOLLOW TOCTOU 백스톱) ──
// root 안 real.txt 를 가리키는 within-root symlink 라도 append leaf 가 symlink 면 거부(symlink 로의
// 쓰기 금지). lstat 선검사가 잡고, 통과 시엔 openSync(O_NOFOLLOW)가 ELOOP 로 커널 거부(race-safe).
fs.writeFileSync(path.join(td, "real.txt"), "real\n");
fs.symlinkSync(path.join(td, "real.txt"), path.join(td, "link-append.txt"));
assert.throws(() => m.safeWrite(td, "link-append.txt", "x\n", "append", 16384), /symlink/);
assert.strictEqual(fs.readFileSync(path.join(td,"real.txt"),"utf-8"), "real\n", "symlink 통한 append 로 real 훼손 금지");

// ── R4 결정(PM 70): 기존 *일반* 파일 append 는 의식적 허용 — resume(중단 후 재개) 보전 ──────
fs.writeFileSync(path.join(td, "resume.txt"), "head\n");
const rr = m.safeWrite(td, "resume.txt", "tail\n", "append", 16384);
assert.strictEqual(fs.readFileSync(path.join(td,"resume.txt"),"utf-8"), "head\ntail\n", "기존 파일 append 허용(resume)");
assert.strictEqual(rr.totalLines, 2);

// ── class-fix (codex R3): dangling symlink 로의 create 우회 봉쇄 ──────────────
// root 안 evil2 → 프로젝트 밖 *부재* 경로 (dangling). existsSync 는 follow 해 false 라 realpath
// 검사를 스킵하던 갭 — leaf lstat + openSync("wx") 커널 강제로 닫는다. create 거부 + 밖 파일 미생성.
const outsideNew = path.join(outside, "created-via-dangling.txt");
assert.ok(!fs.existsSync(outsideNew), "precondition: 밖 파일 부재");
fs.symlinkSync(outsideNew, path.join(td, "evil2.txt"));
assert.ok(!fs.existsSync(path.join(td, "evil2.txt")), "dangling → existsSync follow 시 false");
assert.throws(() => m.safeWrite(td, "evil2.txt", "PWNED\n", "create", 16384), /symlink|이미 존재/);
assert.ok(!fs.existsSync(outsideNew), "보안 실패 — dangling symlink 로 밖 파일이 생성됨!");
// leaf lstat 순수 단언: symlink leaf 거부 · 진짜 부재/일반 파일 통과.
assert.throws(() => m.assertLeafNotSymlink(path.join(td, "evil2.txt")), /symlink/);
m.assertLeafNotSymlink(path.join(td, "no-such.txt"));         // 부재 → OK(throw 없음)
m.assertLeafNotSymlink(path.join(td, "sub", "ok.txt"));       // 일반 파일 → OK

// ── 순수 assertContainedPath / isWithinRoot 경계 ─────────────────────────────
assert.strictEqual(m.assertContainedPath(td, "a/b.txt"), path.resolve(td, "a/b.txt"));
assert.throws(() => m.assertContainedPath(td, "/abs"), /절대경로/);
assert.throws(() => m.assertContainedPath(td, "../x"), /벗어나/);
assert.throws(() => m.assertContainedPath("", "x"), /루트/);
assert.throws(() => m.assertContainedPath(td, ""), /비었/);
assert.ok(m.isWithinRoot("/a/b", "/a/b/c"));
assert.ok(m.isWithinRoot("/a/b", "/a/b"));
assert.ok(!m.isWithinRoot("/a/b", "/a/bc"));  // 접두 문자열 오탐 방지
assert.ok(!m.isWithinRoot("/a/b", "/a"));

console.log("ROOT_CONTAINMENT_OK");
} finally {
  if (outside) fs.rmSync(outside, {recursive:true, force:true});
  if (td) fs.rmSync(td, {recursive:true, force:true});
}
"""
    out = _run_node_check(script)
    assert "ROOT_CONTAINMENT_OK" in out, f"root containment 자가검증 실패. out={out!r}"
