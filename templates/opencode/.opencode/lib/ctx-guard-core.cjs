// opencode 어댑터 — ctx checkpoint 가드 core.
//
// 이 파일(CJS)은 ctx-guard 의 *순수 로직 + 플러그인 팩토리 본체*를 담는다. opencode plugin
// 진입점은 `../plugins/ctx-guard.js`(ESM 얇은 shim)이며 여기서 CtxGuardPlugin 팩토리만 named-export
// 한다 — opencode 의 plugin 로드 규약(각 plugins/ 파일의 export 를 순회해 *모두 함수*이길 요구하고
// 각각을 팩토리로 호출·실측) 때문에, 순수 헬퍼·상수는 plugins/ *바깥*(이 lib/ 모듈)에 둔다
// (opencode 는 plugins/ 만 스캔·lib/ 는 로드 안 함). node 자가검증 test 는 이 CJS 모듈을 require 해
// 순수 함수(parseLocalConf·computeCtxState 등)를 이벤트/opencode 런타임 없이 검증한다.
//
// 무엇:
//   opencode 세션의 컨텍스트 토큰 사용을 추적해 임박 밴드에서 checkpoint 박제를 안내하고,
//   session.compacted 를 병행 관측해 세션-로컬 횟수 누적·밴드 재무장·압축 후 checkpoint 안내를 한다.
//   관측 횟수는 표시하지 않고 plugin 재기동 시 0부터 다시 시작한다.
//
// 모델: 사전 checkpoint 넛지 → native compaction → 사후 checkpoint 넛지.
//
// 임계값: 엔진 압축 한도 기록(local_conf.read_ctx_budget)의 적용 한도 E 와 `ctx.checkpoint_at_pct`/
//   `ctx.precompact_at_pct` = 적용 한도 대비 "쓴 %". 쓴% = used/limit * 100. computeCtxState 의 stop
//   반환은 파리티를 위해 유지하고, plugin 은 stop 밴드를 최종 checkpoint 안내로만 흡수한다(차단·marker
//   없음). 기록 부재·손상·입력 불일치와 세션 모델(`provider/model`) 불일치는 toast 로 알린다.
//   압축 비율(`ctx.compact_at_pct`)은 여기서 읽지 않는다 — 엔진이 그 값으로 `.opencode/opencode.jsonc`
//   의 모델 `limit`·`compaction.reserved` 를 쓰고(pm_import.apply_harness_ctx) 압축은 opencode 가 한다.
//
// 멱등성: nudge/nudge2 는 compaction 사이클당 각 1회. session.compacted 가 둘을 re-arm 한다.
//
// 엔진(pm_handoff/board) 미수정 — shell-out 호출만. 어댑터층(templates/opencode/.opencode/)만.
//
// 결정 로직(computeCtxState·parseLocalConf·accumulateTokens)은
// 순수 함수로 떼어 export — node 로 자가검증(이벤트/opencode 런타임 없이). plugin 함수는 그 wiring.

const fs = require("node:fs");
const path = require("node:path");
const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const { parseLocalConf, supportedPythonCandidates } = require("./delegate-channel-core.cjs");

// 2단(strong) nudge 임계 마진 (%p·파생값). nudge2 밴드 = max(precompact_at − 이 마진,
// checkpoint_at) ≤ 사용 < precompact_at — compaction 임박 전 강한 유도.
// config 노브 신설 없이 precompact_at 에서 파생. claude ctx_guard.CTX_NUDGE2_MARGIN_PCT 와 미러.
const NUDGE2_MARGIN_PCT = 3;

// 엔진 루트는 이 파일 자기 위치에서 나온다 — 훅은 언제나 `<root>/.opencode/lib/*.cjs` 에 설치되고
// (pm_import 가 그 깊이를 못박는다) 이는 파이썬 도구의 `Path(__file__).resolve().parents[2]` 와 같은
// 규칙이다. 조상을 훑으면 중첩 트리에서 바깥 프로젝트의 pm_log.py 를 실행한다.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");
const LOCAL_CONF_REL = path.join(".project_manager", "local.conf");
const CTX_STOP_REL = path.join(".project_manager", ".local", "ctx-stop");
// 세션 parentID 역조회는 ctx guard 의 보조 입력이다. SDK가 응답하지 않아 이벤트 처리가 멈추지
// 않도록 짧게 제한한다.
const SESSION_LOOKUP_TIMEOUT_MS = 1000;
const SNAPSHOT_TIMEOUT_MS = 3000;
const CHECKPOINT_TIMEOUT_MS = 5000;
const COMPACTION_SNAPSHOT_RECEIPT_RETAIN = 8;
// 압축 기록 실패 신호 머리 — claude `ctx_stop_hook` 과 같은 문구(압축 안내 첫 줄).
const COMPACTION_FAILURE_PREFIX = "[ctx-checkpoint] 압축 기록 실패: ";

// 차단 구키 목록은 엔진이 **생성**한다(어댑터가 매핑표를 복제하면 표와 파서가 갈린다):
//   python3 .project_manager/tools/local_conf.py --render-adapter-block js
// 생성 시작 — 차단 구키 (local_conf.render_adapter_block · 손편집 금지)
const LEGACY_CONF_KEYS = [
  "additional_reviewer.enabled",
  "additional_reviewer_enabled",
  "additional_reviewer_incomplete_round_limit",
  "additional_reviewer_round_limit",
  "additional_reviewer_wave_budget",
  "ctx.checkpoint_pct",
  "ctx.nudge_pct",
  "ctx.precompact_pct",
  "ctx.stop_pct",
  "ctx.window_tokens",
  "ctx_nudge_pct",
  "ctx_stop_pct",
  "ctx_window_tokens",
  "date",
  "delegate.code-reviewer.rounds_max",
  "delegate_enabled",
  "delegate_idle_timeout",
  "delegate_timeout",
  "diff_cap.large",
  "diff_cap.medium",
  "diff_cap.small",
  "external_review_enabled",
  "external_review_idle_timeout",
  "external_review_incomplete_round_limit",
  "external_review_progress_signal",
  "external_review_round_limit",
  "external_review_timeout",
  "external_review_wave_budget",
  "opencode_pro_model",
  "project_name",
  "project_root",
  "project_tagline",
  "py",
  "regression_min_collected",
  "review_denylist_extra",
  "review_paths",
  "review_rounds_max",
  "reviewer_cmd",
  "reviewer_env_keep_extra",
  "reviewer_home_artifacts_extra",
  "test_cmd",
  "upstream",
  "upstream_rev",
  "upstream_seen_rev",
  "user",
];
const LEGACY_CONF_KEY_PREFIX = "ctx_window_tokens_";
// 생성 끝 — 차단 구키

// ── 구표기 conf 차단 (값 해소 **전**) ────────────────────────────────────────
// 개칭된 키가 남은 conf 를 그대로 읽으면 임계·예산이 조용히 엔진 기본값으로 떨어진다 — 채택자는
// conf 를 고쳤는데 아무 일도 안 일어나는 상태를 본다. 어댑터는 엔진을 import 하지 않아 신표기
// 이름을 말하지 못하므로, 무엇이 걸렸는지만 말하고 전수 지목은 엔진 도구에 맡긴다.
function assertNoLegacyConf(conf, confPath) {
  const found = Object.keys(conf || {})
    .filter(
      (key) =>
        LEGACY_CONF_KEYS.includes(key) ||
        (key.startsWith(LEGACY_CONF_KEY_PREFIX) && key.length > LEGACY_CONF_KEY_PREFIX.length),
    )
    .sort();
  if (found.length === 0) return;
  throw new Error(
    `오류: local.conf 에 구표기 키가 남아 있습니다 (${confPath}) — ${found.join(", ")}. ` +
      "값이 조용히 기본값으로 떨어지지 않도록 여기서 멈춥니다. " +
      "전수 지목은 `board.py lint` 또는 `pm_update.py` 안내가 냅니다.",
  );
}

// 파일 → 값 한 경로. 부재·판독 실패는 빈 conf(정상 형상)지만 **구표기 잔존은 멈춘다**.
function loadLocalConf(root) {
  const confPath = path.join(root, LOCAL_CONF_REL);
  let text = null;
  try {
    if (!fs.existsSync(confPath)) return {};
    text = fs.readFileSync(confPath, "utf-8");
  } catch {
    return {};
  }
  const conf = parseLocalConf(text);
  assertNoLegacyConf(conf, confPath);
  return conf;
}

// ── 압축 한도 기록 읽기 (엔진 local_conf.read_ctx_budget) ─────────────────────
// 기록 형식·local.conf 입력 비교는 엔진 한 벌이다 — JS 에서 다시 구현하지 않고 Python 으로 읽는다.
// 반환은 {budget: {E, checkpoint_at_pct, precompact_at_pct, warning, …}} 또는 {error: 문구}.
function readBudget(conf, root, sessionModel) {
  const candidate = supportedPythonCandidates(root, { timeout: SNAPSHOT_TIMEOUT_MS }).next().value;
  if (!candidate) return { error: "압축 한도 기록을 읽을 Python 실행기를 찾지 못했습니다." };
  const script = `import sys,json
from pathlib import Path
root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root/".project_manager/tools"))
from pm_engine.infra import repo_files
leaf=repo_files.load_module(root/".project_manager/tools/local_conf.py","local_conf.py",allow_unverified=True,cache=True)
conf,model=json.loads(sys.argv[2])
try: print(json.dumps({"budget":leaf.read_ctx_budget(root,conf,"opencode",model)}))
except leaf.CtxBudgetError as exc: print(json.dumps({"error":str(exc)}))
`;
  const result = childProcess.spawnSync(candidate.command, [...candidate.prefixArgs, "-I", "-B", "-c", script,
    root, JSON.stringify([conf, sessionModel])], {
    cwd: root, encoding: "utf8", timeout: SNAPSHOT_TIMEOUT_MS, maxBuffer: 64 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (!result || result.status !== 0) {
    const reason = (result && (String(result.stderr || "").trim() || result.error)) || "실행 실패";
    return { error: `압축 한도 기록을 읽지 못했습니다 (${reason}).` };
  }
  return JSON.parse(result.stdout);
}

// ── 순수 함수: AssistantMessage.tokens 누적 = 현재 컨텍스트 점유 토큰 ──────────
// opencode 의 컨텍스트 점유 = 직전 어시스턴트 턴의 input + cache(read+write) + output + reasoning.
// (input/cache 가 누적 컨텍스트를 반영 — 매 턴의 최신값을 쓰고, 합산이 아니라 최신 메시지 기준.)
function accumulateTokens(tokens) {
  if (!tokens || typeof tokens !== "object") return 0;
  // total 우선 — opencode 자체 overflow 판정과 동일(1.18.5 overflow.ts·driver 파서와 통일).
  // 부재/비양수면 기존 합산 폴백(보수적 상위집합).
  const total = Number(tokens.total);
  if (Number.isFinite(total) && total > 0) return total;
  const input = Number(tokens.input) || 0;
  const output = Number(tokens.output) || 0;
  const reasoning = Number(tokens.reasoning) || 0;
  const cacheRead = tokens.cache ? Number(tokens.cache.read) || 0 : 0;
  const cacheWrite = tokens.cache ? Number(tokens.cache.write) || 0 : 0;
  return input + output + reasoning + cacheRead + cacheWrite;
}

// ── 순수 함수: 2단(strong) nudge 임계 (precompact_at − margin·checkpoint_at 하한) ────────
// nudge2 밴드 = 이 값 ≤ 사용 < precompact_at. margin(3)이 nudge 밴드 아래로 새지 않게 max 로
// 하한을 둔다. claude ctx_guard.nudge2_threshold 와 동형.
function nudge2Threshold(thresholds) {
  return Math.max(
    thresholds.precompact_at_pct - NUDGE2_MARGIN_PCT,
    thresholds.checkpoint_at_pct,
  );
}

// ── 순수 함수: ctx 상태 판정 (테스트 핵심) ──────────────────────────────────
// used: accumulateTokens 결과, limit: 적용 한도 E(readBudget·양의 정수·구 물리한도 폐기),
// thresholds: readBudget 의 budget(두 비율). 반환: { usedPct, level: "ok"|"nudge"|"nudge2"|"stop" }.
//   nudge2 = stop 경계 직전 강한 유도 밴드.
function computeCtxState(used, limit, thresholds) {
  const u = Number(used) || 0;
  const usedPct = (u / limit) * 100;
  let level = "ok";
  if (usedPct >= thresholds.precompact_at_pct) level = "stop";
  else if (usedPct >= nudge2Threshold(thresholds)) level = "nudge2";
  else if (usedPct >= thresholds.checkpoint_at_pct) level = "nudge";
  return { usedPct, level };
}

// ── 순수 함수: 중앙 엔진 부재 시 비종료 fallback 안내 ────────────────────────
// 실 안내는 pm_log.py ctx-guidance가 단일 소유한다. 다만 중앙 엔진을 읽을 수 없는 fail-soft
// 경계에서도 정책이 뒤집히지 않도록 필수 두 사실만 최소 복제한다.
const FALLBACK_CONTINUITY_GUIDANCE =
  "압축은 자동이고 세션은 그대로 이어진다. 핸드오프는 사용자 명시 지시로만 한다.";
function buildNudgeGuidance(state) {
  const used = Math.round((state && state.usedPct) || 0);
  return (
    `[ctx-nudge] 컨텍스트 사용 ${used}%. ` +
    `현재 ticket 경계의 결과와 진행 상태를 complete entry와 ` +
    `\`python3 .project_manager/tools/pm_log.py checkpoint --task <task>\`으로 기록할 수 있다. ` +
    `${FALLBACK_CONTINUITY_GUIDANCE} ` +
    `상세 ctx 연속성 정책은 pm_log.py ctx-guidance 엔진에서 복구한다.`
  );
}

// ── 순수 함수: 2단(strong) checkpoint 안내문 (compaction 임박) ────────────────
// 1단을 놓쳤거나 건너뛴 세션에 checkpoint 실행을 즉시 지시한다. 여전히 비차단 안내다.
function buildNudge2Guidance(state) {
  return (
    `[ctx-nudge/강화] 컨텍스트 사용 ${Math.round((state && state.usedPct) || 0)}%. ` +
    `\`python3 .project_manager/tools/pm_log.py checkpoint --task <task>\`으로 ` +
    `checkpoint entry의 구간·서사를 기록할 수 있다. ` +
    `${FALLBACK_CONTINUITY_GUIDANCE} ` +
    `상세 ctx 연속성 정책은 pm_log.py ctx-guidance 엔진에서 복구한다.`
  );
}

// ── compaction 엔진 호출: 지원 실행기 해소 이후 실제 엔진 1회 호출 ──
function runPmLog(root, args, options = {}, spawnSync = childProcess.spawnSync) {
  const engine = path.join(root, ".project_manager", "tools", "pm_log.py");
  const capture = options.capture === true;
  const captureStderr = options.captureStderr === true;
  const timeout = options.timeout || SNAPSHOT_TIMEOUT_MS;
  const candidate = supportedPythonCandidates(root, {
    spawnSync, platform: options.platform, timeout, attempts: options.attempts,
  }).next().value;
  if (!candidate) return null;
  return spawnSync(candidate.command, [...candidate.prefixArgs, engine, ...args], {
    cwd: root,
    encoding: "utf8",
    timeout,
    maxBuffer: 32 * 1024,
    stdio: ["ignore", capture ? "pipe" : "ignore", captureStderr ? "pipe" : "ignore"],
  }) || null;
}

function buildEngineCtxGuidance(
  root, band, state, thresholds, spawnSync = childProcess.spawnSync,
) {
  const args = ["ctx-guidance", "--band", band];
  if (state && Number.isFinite(Number(state.usedPct))) {
    args.push("--used-pct", String(Math.round(Number(state.usedPct))));
  }
  if (thresholds && Number.isFinite(Number(thresholds.precompact_at_pct))) {
    args.push("--precompact-at-pct", String(Math.round(Number(thresholds.precompact_at_pct))));
  }
  const result = runPmLog(
    root, args, { capture: true, timeout: SNAPSHOT_TIMEOUT_MS }, spawnSync,
  );
  if (result && result.status === 0 && typeof result.stdout === "string" && result.stdout.trim()) {
    return result.stdout.trim();
  }
  if (band === "nudge") return buildNudgeGuidance(state);
  if (band === "nudge2") return buildNudge2Guidance(state);
  if (band === "final") {
    const used = Math.round((state && state.usedPct) || 0);
    const precompact = Math.round(thresholds.precompact_at_pct);
    return (
      `[ctx-nudge/최종] 컨텍스트 사용 ${used}% (압축 임박 경고 ${precompact}% 이상). ` +
      `\`python3 .project_manager/tools/pm_log.py checkpoint --task <task> --trigger compaction\` ` +
      `기록을 사용할 수 있다. ${FALLBACK_CONTINUITY_GUIDANCE} ` +
      `상세 ctx 연속성 정책은 pm_log.py ctx-guidance 엔진에서 복구한다.`
    );
  }
  return buildCompactionFallbackGuidance();
}

function buildCompactionSnapshot(root, sessionID, spawnSync = childProcess.spawnSync) {
  // Builder command token: pm_log.py snapshot. stdout payload는 변형 없이 system[]에 전달한다.
  // 정체성은 세션 ID 하나다(그 세션이 연 task). 실패면 엔진 stderr 마지막 줄을 실패 신호로 싣는다.
  const result = runPmLog(
    root,
    ["snapshot", "--session-id", String(sessionID || "")],
    { capture: true, captureStderr: true, timeout: SNAPSHOT_TIMEOUT_MS },
    spawnSync,
  );
  if (result && result.status === 0 && typeof result.stdout === "string" && result.stdout) {
    return { ok: true, text: result.stdout };
  }
  const stderrLines = String((result && result.stderr) || "")
    .split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const reason = stderrLines.length
    ? stderrLines[stderrLines.length - 1]
    : `pm_log status=${result ? result.status : "실행 불가"}`;
  return { ok: false, text: COMPACTION_FAILURE_PREFIX + reason };
}

function buildCompactionFallbackGuidance() {
  return (
    "[ctx-checkpoint] compaction이 방금 일어남 — PM snapshot을 읽지 못했다. " +
    "현재 task의 pm_state와 log/current.md를 확인하고, 필요하면 pm_log.py checkpoint로 경계를 보충하라. " +
    FALLBACK_CONTINUITY_GUIDANCE
  );
}

// ── compaction snapshot durable marker (Claude PostCompact 패턴 대칭) ─────────
// opencode run --continue 는 프로세스-당-턴 one-shot 이므로 턴 말미 session.compacted 에서
// in-memory pendingNudgeText 만 무장하면 다음 프로세스까지 payload가 살아남지 못한다. PM root의
// 기존 ctx-stop 디렉터리(compact-checkpoint.*와 동일)에 SID+generation별 payload를
// 원자 stage한다. SID 필터/길이는 pm_log.py `_safe_marker_key`의 파일명 규약과
// 맞추되 marker 채널에서는 빈 결과를 unknown으로 합치지 않고 skip한다.
function safeCompactionSnapshotSessionKey(sessionID) {
  if (typeof sessionID !== "string" || !sessionID.trim()) return null;
  const key = sessionID.trim().replace(/[^A-Za-z0-9_-]/g, "").slice(0, 96);
  return key || null;
}

function createCompactionSnapshotGeneration() {
  const utc = new Date().toISOString().replace(/[-:.]/g, "");
  return `${utc}-${crypto.randomUUID()}`;
}

function compactionSnapshotMarkerDirectory(root) {
  return path.resolve(root, CTX_STOP_REL);
}

function compactionSnapshotMarkerPath(root, sessionID, generation) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key || typeof generation !== "string") return null;
  if (!/^[A-Za-z0-9_-]+$/.test(generation)) return null;
  return path.join(directory, `compact-snapshot.${key}.${generation}`);
}

function compactionSnapshotReceiptPath(root, sessionID, generation) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key || typeof generation !== "string") return null;
  if (!/^[A-Za-z0-9_-]+$/.test(generation)) return null;
  return path.join(directory, `compact-snapshot-receipt.${key}.${generation}`);
}

function compactionSnapshotGenerationFromMarker(root, sessionID, ownedMarker) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key || typeof ownedMarker !== "string") return null;
  const marker = path.resolve(ownedMarker);
  const prefix = `compact-snapshot.${key}.`;
  if (path.dirname(marker) !== directory || !path.basename(marker).startsWith(prefix)) return null;
  const generation = path.basename(marker).slice(prefix.length);
  return generation && /^[A-Za-z0-9_-]+$/.test(generation) ? generation : null;
}

function writeCompactionSnapshotReceipt(root, sessionID, generation) {
  const receipt = compactionSnapshotReceiptPath(root, sessionID, generation);
  if (!receipt) return;
  try {
    // receipt 자체가 전달 증거다. generation이 유일하므로 원자 rename 없이 빈 0600 파일이면 충분하다.
    fs.writeFileSync(receipt, "", { encoding: "utf8", mode: 0o600 });
  } catch {
    /* 관측 채널 IO 실패가 이미 수행된 system 전달을 막지 않는다. */
  }
}

function pruneCompactionSnapshotReceipts(root, sessionID) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key) return;
  const prefix = `compact-snapshot-receipt.${key}.`;
  try {
    const receipts = fs.readdirSync(directory, { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.startsWith(prefix))
      .map((entry) => ({
        receipt: path.join(directory, entry.name),
        generation: entry.name.slice(prefix.length),
      }))
      .filter(({ generation }) => generation && /^[A-Za-z0-9_-]+$/.test(generation))
      .sort((left, right) => left.generation.localeCompare(right.generation));
    for (const { receipt } of receipts.slice(0, -COMPACTION_SNAPSHOT_RECEIPT_RETAIN)) {
      try {
        fs.unlinkSync(receipt);
      } catch {
        /* 동시 정리/권한 오류는 다음 staging GC에 맡긴다. */
      }
    }
  } catch {
    /* receipt 열거 실패도 snapshot staging을 실패로 바꾸지 않는다. */
  }
}

function stageCompactionSnapshot(root, sessionID, payload) {
  if (typeof payload !== "string" || !payload) return null;
  const marker = compactionSnapshotMarkerPath(
    root, sessionID, createCompactionSnapshotGeneration(),
  );
  if (!marker) return null;
  const temp = path.join(
    path.dirname(marker),
    `.${path.basename(marker)}.${process.pid}.${crypto.randomUUID()}.tmp`,
  );
  try {
    fs.mkdirSync(path.dirname(marker), { recursive: true });
    fs.writeFileSync(temp, payload, { encoding: "utf8", mode: 0o600 });
    fs.renameSync(temp, marker);
    pruneCompactionSnapshotReceipts(root, sessionID);
    return marker;
  } catch {
    return null;
  } finally {
    try {
      fs.unlinkSync(temp);
    } catch {
      /* staging 실패/rename 완료 뒤 temp 정리는 best-effort. */
    }
  }
}

function discardCompactionSnapshot(root, sessionID, ownedMarker) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key || typeof ownedMarker !== "string") return;
  const marker = path.resolve(ownedMarker);
  const prefix = `compact-snapshot.${key}.`;
  // 세션 상태가 기억한 자기 generation 정확한 경로만 삭제한다.
  if (path.dirname(marker) !== directory || !path.basename(marker).startsWith(prefix)) return;
  try {
    fs.unlinkSync(marker);
  } catch {
    /* marker 부재/삭제 실패는 모델 호출을 막지 않는다. */
  }
}

function listCompactionSnapshots(root, sessionID) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key) return [];
  const prefix = `compact-snapshot.${key}.`;
  const snapshots = [];
  try {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (!entry.isFile() || !entry.name.startsWith(prefix)) continue;
      const generation = entry.name.slice(prefix.length);
      if (!generation || !/^[A-Za-z0-9_-]+$/.test(generation)) continue;
      const marker = path.join(directory, entry.name);
      try {
        const stat = fs.statSync(marker, { bigint: true });
        if (stat.isFile()) snapshots.push({ marker, generation, mtimeNs: stat.mtimeNs });
      } catch {
        /* 동시 소비/정리로 사라진 entry는 다음 스냅샷에 맡긴다. */
      }
    }
  } catch {
    return [];
  }
  snapshots.sort((left, right) => {
    if (left.mtimeNs < right.mtimeNs) return -1;
    if (left.mtimeNs > right.mtimeNs) return 1;
    return left.generation.localeCompare(right.generation);
  });
  return snapshots;
}

function takeCompactionSnapshot(root, sessionID) {
  const snapshots = listCompactionSnapshots(root, sessionID);
  if (!snapshots.length) return null;
  const latest = snapshots[snapshots.length - 1];
  // old를 latest보다 먼저 지워 claim 진행 중 [old]만 관측되는 상태를 만들지 않는다.
  // old 삭제 뒤 claim 전에 crash해도 latest는 생존한다. 고정 generation 이름의 배타 link가 선점이다.
  // 선점 패배자는 구 generation 재탐색/fallback 없이 skip한다.
  for (const snapshot of snapshots.slice(0, -1)) {
    try {
      fs.unlinkSync(snapshot.marker);
    } catch (error) {
      // 동시 GC로 이미 사라진 old는 성공 동치다. 그 밖의 실패에서는 latest를 남겨
      // 다음 소비자가 old와 함께 다시 관측하도록 claim 없이 fail-soft skip한다.
      if (!error || error.code !== "ENOENT") return null;
    }
  }
  const claimed = path.join(
    path.dirname(latest.marker),
    `.${path.basename(latest.marker)}.claimed`,
  );
  try {
    fs.linkSync(latest.marker, claimed);
  } catch {
    return null;
  }
  try {
    fs.unlinkSync(latest.marker);
  } catch {
    // ENOENT도 전달하지 않는다. 원본 제거에 실패한 선점 fence는 재진입을 막기 위해 남긴다.
    return null;
  }
  try {
    const payload = fs.readFileSync(claimed, "utf8");
    return payload ? { payload, generation: latest.generation } : null;
  } catch {
    return null;
  } finally {
    try {
      fs.unlinkSync(claimed);
    } catch {
      /* claimed marker 정리 실패도 주입 경계를 막지 않는다. */
    }
  }
}

function createCompactionBoundaryID(sessionID) {
  // count는 plugin 재시작 때 0으로 돌아가므로 dedup ID에 쓰지 않는다. UTC+UUID 축을 앞에 두어
  // pm_log의 marker 길이 상한에서도 재시작 불변 유일 성분이 잘리지 않게 한다.
  const utc = new Date().toISOString().replace(/[-:.]/g, "");
  return `opencode-${utc}-${crypto.randomUUID()}-${String(sessionID || "unknown")}`;
}

function createCompactionCheckpoint(
  root, sessionID, boundaryID, spawnSync = childProcess.spawnSync,
) {
  const args = ["checkpoint", "--trigger", "compaction", "--phase", "post"];
  if (typeof sessionID === "string" && sessionID.trim()) {
    args.push("--session-id", sessionID.trim());
  }
  if (typeof boundaryID === "string" && boundaryID.trim()) {
    args.push("--boundary-id", boundaryID.trim());
  }
  runPmLog(root, args, { capture: false, timeout: CHECKPOINT_TIMEOUT_MS }, spawnSync);
}

// ── plugin 팩토리 (진입점 ../plugins/ctx-guard.js 가 ESM named-export 로 재노출·opencode autoload) ──
const CtxGuardPlugin = async ({ client, $ }) => {
  // 로드 검증 마커 (env-gated·라이브-로드 게이트 전용·실 세션엔 무음). opencode 는 성공 로드
  // 시 positive 로그를 남기지 않아(실측 1.17.18) — factory 실행을 관측하려면 스스로 마커를 낸다.
  // 훅 로직 무관 — CTX_GUARD_LOAD_PROBE 미설정 시 완전 무음(프로덕션 기본).
  if (process.env.CTX_GUARD_LOAD_PROBE) {
    process.stderr.write("[ctx-guard] plugin factory loaded (CTX_GUARD_LOAD_PROBE)\n");
  }
  // 모든 가드 상태는 hook 인스턴스 전역이 아니라 sessionID 별로 분리한다. childLookup
  // Promise 도 같은 레코드에 캐시해 동시 이벤트의 중복 SDK 조회를 막는다.
  const sessionStates = new Map();

  function sessionState(sessionID) {
    if (typeof sessionID !== "string" || !sessionID.trim()) return null;
    let state = sessionStates.get(sessionID);
    if (!state) {
      state = {
        fired: { nudge: false, nudge2: false },
        pendingNudgeText: null,
        pendingSnapshotMarker: null,
        budgetNotice: null,
        compactionCount: 0,
        cycleEpoch: 0,
        childLookup: null,
      };
      sessionStates.set(sessionID, state);
    }
    return state;
  }

  const root = ENGINE_ROOT;

  // OpenCode SDK 표면: client.session.get({ path: { id: sessionID } }) -> { data: Session }.
  // Session.parentID가 있으면 native task가 만든 자식 세션이다. 이때만 Claude의 sub-session
  // 등가 정책처럼 checkpoint 안내를 생략하고, 생존은 전역 자동 컴팩션에 맡긴다. 역조회
  // 불확실성은 절대 면제 사유가 아니다.
  function isChildSession(sessionID) {
    const state = sessionState(sessionID);
    if (!state) return Promise.resolve(false);
    if (state.childLookup) return state.childLookup;

    const lookup = (async () => {
      try {
        if (!client || !client.session || typeof client.session.get !== "function") return false;
        let timeoutID;
        const timeout = new Promise((resolve) => {
          timeoutID = setTimeout(() => resolve(null), SESSION_LOOKUP_TIMEOUT_MS);
        });
        let result;
        try {
          result = await Promise.race([
            client.session.get({ path: { id: sessionID } }),
            timeout,
          ]);
        } finally {
          clearTimeout(timeoutID);
        }
        const session = result && result.data;
        return Boolean(session && typeof session.parentID === "string" && session.parentID.trim());
      } catch {
        return false;
      }
    })();
    state.childLookup = lookup;
    return lookup;
  }

  // 넛지: 사이클당 1회 toast(없으면 무음 — fail-soft).
  async function notifyNudge(message) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 넛지는 best-effort. */
    }
  }

  // 2단 넛지: 사이클당 1회 강한 toast(compaction 임박). 사람용 2단 표시(없으면 무음).
  async function notifyNudge2(message) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 넛지는 best-effort. */
    }
  }

  // compaction 직후 사람용 toast. 모델 안내는 pendingNudgeText/system.transform 채널을 병행한다.
  async function notifyCompacted(message) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 모델-facing checkpoint 안내는 별도 채널이다. */
    }
  }

  return {
    // ── 병행 신호: compaction 사후 안내 + 어시스턴트 메시지 임박 밴드 ────────
    event: async ({ event }) => {
      if (!event) return;

      if (event.type === "session.compacted") {
        const sessionID = event.properties && event.properties.sessionID;
        const session = sessionState(sessionID);
        if (!session) return;
        session.compactionCount += 1;
        session.cycleEpoch += 1;
        // compaction 이후 사용량이 낮아지는 새 사이클에서 사전 밴드 안내가 다시 발화하도록 재무장.
        session.fired.nudge = false;
        session.fired.nudge2 = false;
        // snapshot 본문/렌더는 pm_log.py 단일 소유. plugin은 builder stdout을 verbatim 적재한다.
        // 자식 세션은 snapshot/checkpoint 모두 면제다. 최대 3초 동기 subprocess 전에 판정해
        // 면제 대상 compaction 이벤트가 메인 세션용 snapshot 때문에 멈추지 않게 한다.
        if (await isChildSession(sessionID)) return;
        const compactedGuidance = buildEngineCtxGuidance(
          root, "precompact", null, null,
        );
        const stagedSnapshot = buildCompactionSnapshot(root, sessionID);
        const stagedNotice = stagedSnapshot.ok
          ? stagedSnapshot.text
          : `${stagedSnapshot.text}\n${compactedGuidance}`;
        session.pendingNudgeText = stagedNotice;
        // 같은 payload를 프로세스 경계 너머에도 보존한다. 같은 프로세스의
        // 재-compaction은 새 generation stage 성공 후 자기 구 generation만 정리한다.
        const previousMarker = session.pendingSnapshotMarker;
        const stagedMarker = stageCompactionSnapshot(root, sessionID, stagedNotice);
        if (stagedMarker) {
          session.pendingSnapshotMarker = stagedMarker;
          if (previousMarker && previousMarker !== stagedMarker) {
            discardCompactionSnapshot(root, sessionID, previousMarker);
          }
        }
        // staging 실패 시 memory는 새 payload이어도 구 durable marker 소유권은 보수적으로 유지한다.
        createCompactionCheckpoint(
          root, sessionID, createCompactionBoundaryID(sessionID),
        );
        await notifyCompacted(compactedGuidance);
        return;
      }

      if (event.type !== "message.updated") return;
      const info = event.properties && event.properties.info;
      if (!info) return;
      if (info.role !== "assistant" || !info.tokens) return;
      // SID는 아래 await들보다 먼저 event-local로 캡처한다(pendingNudgeText 귀속 경합 방지).
      const sessionID = info.sessionID;
      const session = sessionState(sessionID);
      if (!session) return;
      // await 중 session.compacted가 re-arm하면 이 이벤트는 구 사이클 소속이다.
      // 진입 시 epoch를 캡처해 새 사이클의 fired/pending 상태를 덮지 않는다.
      const cycleEpoch = session.cycleEpoch;

      // Native task 자식은 독립 컨텍스트로 구동되므로 PM 메인 세션용 checkpoint 가드를 면제한다.
      if (await isChildSession(sessionID)) return;
      if (session.cycleEpoch !== cycleEpoch) return;

      const used = accumulateTokens(info.tokens);
      // 매 이벤트의 local.conf 로 읽는다 — 값이 적용 기록과 달라지면 기록 쪽이 에러로 알린다.
      const result = readBudget(loadLocalConf(root), root, `${info.providerID}/${info.modelID}`);
      const notice = result.error || result.budget.warning;
      if (notice && session.budgetNotice !== notice) {
        session.budgetNotice = notice; // 같은 알림은 세션당 한 번만 toast 한다.
        await notifyNudge(`[ctx-budget] ${notice}`);
      }
      if (result.error) return;
      const t = result.budget;
      const state = computeCtxState(used, t.E, t);

      // stop의 차단 소비만 폐지했다. stop 밴드 자체는 최종(nudge2) checkpoint 안내로 무음 흡수한다.
      if ((state.level === "nudge2" || state.level === "stop") && !session.fired.nudge2) {
        // 2단(strong·compaction 임박) — 1단 발화 여부와 독립(1단 창을 건너뛴 세션도 발화).
        session.fired.nudge2 = true;
        // 2단 모델-주입 안내 대기 — 1단과 동일 채널(system.transform 1회 소비).
        const guidance = buildEngineCtxGuidance(root, "nudge2", state, t);
        session.pendingNudgeText = guidance;
        session.pendingSnapshotMarker = null;
        await notifyNudge2(guidance); // 2단 강한 toast (사람 UI·1회).
      } else if (state.level === "nudge" && !session.fired.nudge) {
        session.fired.nudge = true;
        // 다음 모델 호출의 system.transform 이 checkpoint 안내를 소비한다.
        const guidance = buildEngineCtxGuidance(root, "nudge", state, t);
        session.pendingNudgeText = guidance;
        session.pendingSnapshotMarker = null;
        await notifyNudge(guidance); // 넛지 toast (사람 UI·1회).
      }
    },

    // ── checkpoint 모델-주입: pending 안내를 system 에 비차단 1회 주입 ────────
    // experimental.chat.system.transform 은 모델 호출 전 system[] 을 비차단 수정한다(@opencode-ai
    // /plugin Hooks·opencode 1.17.11 타입 확인). chat.message 의 full Part 구성(id/sessionID/
    // messageID 필수)보다 string push 가 안전·정확. ⚠️ experimental namespace — opencode 가 이 surface
    // 를 바꾸면 *조용히* 주입이 멈출 수 있다. 호환성 게이트 = Tier2
    // 라이브 smoke(버전 회귀 포착)·codex 권고 반영. 변동 시 안정 chat.message 전환 검토.
    // 멱등: in-memory payload는 자기가 stage한 generation만 함께 지운다.
    // 새 프로세스라 in-memory payload가 없으면 최신 marker를 선점·읽기·소거하고,
    // 그 관측 스냅샷에 함께 있던 구 generation들을 중복 주입 없이 정리한다.
    // 자식 세션은 적재와 마찬가지로 marker 소비도 면제한다.
    "experimental.chat.system.transform": async (input, output) => {
      const sessionID = input && input.sessionID;
      if (await isChildSession(sessionID)) return;
      const session = sessionState(sessionID);
      if (session && session.pendingNudgeText && output && Array.isArray(output.system)) {
        output.system.push(session.pendingNudgeText);
        const ownedMarker = session.pendingSnapshotMarker;
        session.pendingNudgeText = null;
        session.pendingSnapshotMarker = null;
        if (ownedMarker) {
          const generation = compactionSnapshotGenerationFromMarker(root, sessionID, ownedMarker);
          if (generation) writeCompactionSnapshotReceipt(root, sessionID, generation);
          discardCompactionSnapshot(root, sessionID, ownedMarker);
        }
        return;
      }
      if (output && Array.isArray(output.system)) {
        const stagedSnapshot = takeCompactionSnapshot(root, sessionID);
        if (stagedSnapshot) {
          output.system.push(stagedSnapshot.payload);
          writeCompactionSnapshotReceipt(root, sessionID, stagedSnapshot.generation);
        }
      }
    },
  };
};

// CommonJS export — node 자가검증(require)·ESM shim(../plugins/ctx-guard.js) 양쪽 소비.
// opencode 는 이 모듈을 직접 로드하지 않는다(plugins/ 만 스캔) — 얇은 ESM shim 이 CtxGuardPlugin 만
// named-export 해 로드 규약(export=단일 함수·실측)을 만족한다. 여기 export 는 순수함수+상수+팩토리.
module.exports = {
  CtxGuardPlugin,
  // 순수 결정 로직 (테스트·자가검증용 export).
  parseLocalConf,
  loadLocalConf,
  assertNoLegacyConf,
  LEGACY_CONF_KEYS,
  LEGACY_CONF_KEY_PREFIX,
  readBudget,
  accumulateTokens,
  nudge2Threshold,
  computeCtxState,
  buildNudgeGuidance,
  buildNudge2Guidance,
  runPmLog,
  buildEngineCtxGuidance,
  buildCompactionSnapshot,
  buildCompactionFallbackGuidance,
  safeCompactionSnapshotSessionKey,
  createCompactionSnapshotGeneration,
  createCompactionBoundaryID,
  createCompactionCheckpoint,
  SESSION_LOOKUP_TIMEOUT_MS,
  SNAPSHOT_TIMEOUT_MS,
  CHECKPOINT_TIMEOUT_MS,
  NUDGE2_MARGIN_PCT,
};
