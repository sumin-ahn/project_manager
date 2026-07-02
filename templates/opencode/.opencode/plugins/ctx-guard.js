// opencode 어댑터 — ctx 정지-핸드오프 plugin (T-0014, 설계 T-0012, 엔진 T-0013).
//
// 무엇:
//   opencode 세션의 컨텍스트 토큰 사용을 추적해, 임계 도달 시
//     1) 넛지(이른 경고·1회 toast + 모델-주입 안내·ADR-0037) → "티켓 마무리·큰 거 새로 시작 마라"
//     2) 하드 정지(ADR-0038 D2) → 새 작업 도구만 차단(tool.execute.before)·진행 중 핸드오프 도구는
//        예외 통과·STOP marker 직접 박제(relay 회전 신호·no pm_handoff --trigger).
//   하고, lossy 컴팩션은 opencode.jsonc `compaction.auto:false` 로 차단(우리 정지가 먼저 오게).
//
// 모델 (T-0012, 2D): 넛지(이른) → 하드 정지(필수) → 새 세션. compaction 회피.
//
// 임계값(엔진 T-0013·T-0207 상향): local.conf `ctx_nudge_pct`/`ctx_stop_pct`(기본 30/20) = "잔여 컨텍스트 %".
//   잔여% = (1 - used/limit) * 100.  잔여 ≤ nudge_pct → 넛지,  잔여 ≤ stop_pct → 정지.
//   plugin 은 local.conf 를 직접 파싱(의존 적음·board.py shell-out 회피).
//
// 멱등성(codex T-0013 인계): 넛지·정지·handoff 트리거는 세션당 각 1회만 (중복 호출 가드).
// sanity(codex 인계): 읽은 nudge/stop 이 비정상(음수·stop>nudge)이면 엔진 기본(30/20) 폴백.
//
// 엔진(pm_handoff/board) 미수정 — shell-out 호출만. 어댑터층(templates/opencode/.opencode/)만.
//
// 결정 로직(computeCtxState·resolveThresholds·parseLocalConf·accumulateTokens)은 순수 함수로
// 떼어 export — node 로 자가검증(이벤트/opencode 런타임 없이). plugin 함수는 그 wiring.

const fs = require("node:fs");
const path = require("node:path");

// ── 엔진 기본값 (board.py CTX_*_PCT_DEFAULT 미러 — 폴백 전용) ──────────────────
// T-0207 상향(20/10→30/20): 잔여 10% 정지는 rich 핸드오프 돌릴 컨텍스트가 아슬(PM 47 실측).
const NUDGE_PCT_DEFAULT = 30; // 잔여 ≤ 이 % → 넛지 (일은 계속).
const STOP_PCT_DEFAULT = 20; // 잔여 ≤ 이 % → 정지·핸드오프.

// 엔진 경로 (plugin 의 directory 기준 .project_manager 까지 거슬러 올라가 해석).
const ENGINE_REL = path.join(".project_manager", "tools");
const LOCAL_CONF_REL = path.join(".project_manager", "local.conf");
// STOP marker 디렉토리 (claude ctx_stop_hook._MARKER_DIR·엔진 pm_relay.MARKER_DIR 미러).
// relay(ADR-0009)가 이 marker 를 stat 해 세션 회전을 트리거한다 — 양 하니스가 동일
// marker 규약을 공유해 같은 Supervisor 코드가 둘 다 구동한다(엔진 무변경·ADR-0009 핵심 불변식).
const MARKER_REL = path.join(".project_manager", ".local", "ctx-stop");
const MARKER_CONTENT = "ctx-stop handoff triggered\n";

// ── 순수 함수: 세션 id sanitize (claude ctx_stop_hook._session_id 규칙 JS 재현) ──
// 파일명 안전 문자([A-Za-z0-9]·`-`·`_`)만 남기고 64자로 잘라 marker 파일명을 짓는다.
// claude hook 과 동일 규칙이어야 relay 의 marker 경로 예측이 양 하니스에서 일치한다.
// 빈 결과(또는 비-문자열)는 "unknown"(hook 폴백 동치).
function sanitizeSessionId(sessionID) {
  if (typeof sessionID !== "string") return "unknown";
  const safe = sessionID
    .trim()
    .split("")
    .filter((c) => /[A-Za-z0-9]/.test(c) || c === "-" || c === "_")
    .join("")
    .slice(0, 64);
  return safe || "unknown";
}

// ── 순수 함수: local.conf 파싱 (board.local_config 미러 — KEY=value·# 주석 무시) ──
function parseLocalConf(text) {
  const conf = {};
  if (typeof text !== "string") return conf;
  for (let line of text.split("\n")) {
    line = line.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const idx = line.indexOf("=");
    const key = line.slice(0, idx).trim();
    const val = line.slice(idx + 1).trim();
    conf[key] = val;
  }
  return conf;
}

// ── 순수 함수: 임계값 해석 + sanity 폴백 ────────────────────────────────────
// conf(parseLocalConf 결과)에서 nudge/stop 을 정수로 읽고, 비정상이면 엔진 기본 폴백.
// 비정상 = 비정수 / 음수 / 0이하 / stop > nudge / 100 초과.  하나라도 깨지면 둘 다 기본으로.
function resolveThresholds(conf) {
  const readPct = (key, dflt) => {
    const raw = conf ? conf[key] : undefined;
    if (raw === undefined || raw === null) return dflt;
    const n = Number.parseInt(String(raw).trim(), 10);
    return Number.isNaN(n) ? dflt : n;
  };
  let nudge = readPct("ctx_nudge_pct", NUDGE_PCT_DEFAULT);
  let stop = readPct("ctx_stop_pct", STOP_PCT_DEFAULT);
  // sanity: 잔여% 임계는 0 < stop ≤ nudge ≤ 100 이어야 의미 있다.
  const sane =
    Number.isInteger(nudge) &&
    Number.isInteger(stop) &&
    stop > 0 &&
    nudge > 0 &&
    nudge <= 100 &&
    stop <= 100 &&
    stop <= nudge;
  if (!sane) {
    return { nudge_pct: NUDGE_PCT_DEFAULT, stop_pct: STOP_PCT_DEFAULT };
  }
  return { nudge_pct: nudge, stop_pct: stop };
}

// ── 순수 함수: AssistantMessage.tokens 누적 = 현재 컨텍스트 점유 토큰 ──────────
// opencode 의 컨텍스트 점유 = 직전 어시스턴트 턴의 input + cache(read+write) + output + reasoning.
// (input/cache 가 누적 컨텍스트를 반영 — 매 턴의 최신값을 쓰고, 합산이 아니라 최신 메시지 기준.)
function accumulateTokens(tokens) {
  if (!tokens || typeof tokens !== "object") return 0;
  const input = Number(tokens.input) || 0;
  const output = Number(tokens.output) || 0;
  const reasoning = Number(tokens.reasoning) || 0;
  const cacheRead = tokens.cache ? Number(tokens.cache.read) || 0 : 0;
  const cacheWrite = tokens.cache ? Number(tokens.cache.write) || 0 : 0;
  return input + output + reasoning + cacheRead + cacheWrite;
}

// ── 순수 함수: ctx 상태 판정 (테스트 핵심) ──────────────────────────────────
// used: accumulateTokens 결과, limit: 모델 context window, thresholds: resolveThresholds 결과.
// 반환: { remainingPct, usedPct, level: "ok"|"nudge"|"stop" }.
//   limit 미상(0/음수/NaN)이면 판정 불가 → level "ok"(과도 정지 방지·안전).
function computeCtxState(used, limit, thresholds) {
  const u = Number(used) || 0;
  const lim = Number(limit);
  const { nudge_pct, stop_pct } = thresholds;
  if (!Number.isFinite(lim) || lim <= 0) {
    return { remainingPct: 100, usedPct: 0, level: "ok" };
  }
  const usedPct = (u / lim) * 100;
  const remainingPct = 100 - usedPct;
  let level = "ok";
  if (remainingPct <= stop_pct) level = "stop";
  else if (remainingPct <= nudge_pct) level = "nudge";
  return { remainingPct, usedPct, level };
}

// ── 순수 함수: nudge 안내문 (모델-facing 비차단 주입용·ADR-0037) ──────────────
// claude ctx_guard.build_nudge_guidance 미러. 조건부 권고(지시 아님) — 현 단계 마무리 후 핸드오프
// 유도로 wave 중간 끊김(premature interrupt) 회피. experimental.chat.system.transform 이 이 문자열을
// system[] 에 push 한다(모델 컨텍스트·비차단). hard-stop 과 달리 모델이 살아있어 스스로 /pm-handoff.
function buildNudgeGuidance(state, thresholds) {
  const remaining = Math.round((state && state.remainingPct) || 0);
  const used = Math.round((state && state.usedPct) || 0);
  const stopPct =
    thresholds && thresholds.stop_pct != null ? thresholds.stop_pct : STOP_PCT_DEFAULT;
  return (
    `[ctx-nudge] 컨텍스트 사용 ${used}% (잔여 ${remaining}%) — 핸드오프 준비 구간. ` +
    `지금 진행 중인 단계(ticket/wave)를 마무리한 뒤, 새 큰 작업을 시작하지 말고 ` +
    `/pm-handoff 로 핸드오프하라. 잔여 ${stopPct}% 도달 시 자동 정지된다 (ADR-0037).`
  );
}

// ── 순수 함수: 핸드오프 도구 allow-list (ADR-0038 D2 — claude ctx_stop_hook._is_handoff_* 미러) ──
// hard-stop 중 진행 중인 rich /pm-handoff 가 완주하도록 핸드오프 도구를 통과시키고 그 외 새 작업은
// 정지한다. tool.execute.before(clean schema: input.tool + output.args)가 authoritative gate,
// permission.ask(Permission best-effort)는 fail-open 보조(핸드오프 절대 오차단 안 함).
const _SHELL_OPS = ["&&", "||", ";", "|", "`", "$(", "\n", ">", "<", "&"];
const _ENV_PREFIX_RE = /^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*/;
const _HANDOFF_BASH_PATTERNS = [
  /^(?:python3?\s+)?\S*pm_handoff\.py(?:\s|$)/,
  /^(?:python3?\s+)?\S*domain\.py(?:\s|$)/,
  /^git\s+add(?:\s|$)/,
  /^git\s+commit(?:\s|$)/,
  /^(?:python3?\s+-m\s+)?pytest(?:\s|$)/,
];
const _HANDOFF_TARGET_SUBSTR = ["log/current.md", "pm_state", "status.md"];
const _HANDOFF_TARGET_DIR = "/domain/";

// Bash command 가 핸드오프-allow 호출로 *시작*하는가 (연쇄 연산자 없이·anchored·claude _is_handoff_bash 미러).
function isHandoffBash(command) {
  if (typeof command !== "string" || !command.trim()) return false;
  if (_SHELL_OPS.some((op) => command.includes(op))) return false; // 복합 명령 → deny(fail-closed).
  const core = command.replace(_ENV_PREFIX_RE, "").trim();
  return _HANDOFF_BASH_PATTERNS.some((re) => re.test(core));
}

// Edit/Write/Read 대상이 핸드오프 산출물(log/pm_state/status/domain)인가 (claude _is_handoff_target 미러).
function isHandoffTarget(filePath) {
  if (typeof filePath !== "string" || !filePath) return false;
  const p = filePath.replace(/\\/g, "/");
  if (_HANDOFF_TARGET_SUBSTR.some((s) => p.includes(s))) return true;
  return p.includes(_HANDOFF_TARGET_DIR);
}

// tool.execute.before 용 — input.tool(이름)+output.args 로 판정 (authoritative·clean schema).
// bash → args.command, edit/write/read/patch → args.filePath(방어적: file_path/path 도). 그 외 → false(deny).
function isHandoffTool(input, output) {
  const tool = String((input && input.tool) || "").toLowerCase();
  const args = (output && output.args) || {};
  if (tool === "bash") return isHandoffBash(args.command);
  if (tool === "edit" || tool === "write" || tool === "read" || tool === "patch") {
    return isHandoffTarget(args.filePath || args.file_path || args.path || "");
  }
  return false;
}

// permission.ask 용 — Permission{type,pattern,title,metadata} best-effort. tool.execute.before 가
// authoritative 라 여기선 *확실한 새 작업만* deny(prompt 회피)하고 핸드오프/불명은 통과(fail-open →
// tool.execute.before 위임·핸드오프 false-block 방지). 반환 true = 새 작업(deny), false = 통과.
function isNewWorkPermission(permission) {
  if (!permission || typeof permission !== "object") return false;
  const cand = [];
  if (typeof permission.pattern === "string") cand.push(permission.pattern);
  else if (Array.isArray(permission.pattern))
    cand.push(...permission.pattern.filter((x) => typeof x === "string"));
  if (typeof permission.title === "string") cand.push(permission.title);
  const meta = permission.metadata;
  if (meta && typeof meta === "object") {
    for (const v of Object.values(meta)) if (typeof v === "string") cand.push(v);
  }
  if (cand.length === 0) return false; // 추출 불가 → 통과(fail-open).
  // 후보 중 하나라도 핸드오프 신호면 통과(false). 전부 비-핸드오프면 새 작업(true·deny).
  return !cand.some((c) => isHandoffBash(c) || isHandoffTarget(c));
}

// ── 엔진 루트 탐색: directory 에서 위로 .project_manager 를 찾는다 ───────────
function findEngineRoot(startDir) {
  let dir = startDir;
  for (let i = 0; i < 12 && dir; i++) {
    if (fs.existsSync(path.join(dir, ".project_manager", "tools", "pm_handoff.py"))) {
      return dir;
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

// ── plugin export (opencode 가 .opencode/plugins/ 의 이 파일을 autoload) ───────
const CtxGuardPlugin = async ({ client, directory, worktree, $ }) => {
  // 세션당 1회 가드 (멱등성).
  const fired = { nudge: false, stop: false };
  // graceful nudge 모델-주입 대기 텍스트 (ADR-0037). event(message.updated)서 nudge 감지 시
  // 세팅 → 다음 모델 호출의 experimental.chat.system.transform 이 1회 소비(push 후 null).
  let pendingNudgeText = null;
  let cachedThresholds = null;
  let cachedLimit = null;
  // 현재 세션 id — event hook 의 info.sessionID 로 캡처(PluginInput 엔 sid 없음·실측). STOP
  // marker 파일명을 짓는 데 쓴다(relay 가 그 marker 를 stat 해 회전 트리거).
  let currentSessionID = null;

  const root = findEngineRoot(directory || worktree || process.cwd());

  // local.conf 직접 파싱 → 임계값 (1회 캐시).
  function thresholds() {
    if (cachedThresholds) return cachedThresholds;
    let conf = {};
    if (root) {
      try {
        const p = path.join(root, LOCAL_CONF_REL);
        if (fs.existsSync(p)) conf = parseLocalConf(fs.readFileSync(p, "utf-8"));
      } catch {
        conf = {};
      }
    }
    cachedThresholds = resolveThresholds(conf);
    return cachedThresholds;
  }

  // 모델 context window 한도 조회 (providers → models[id].limit.context). 실패 시 null.
  async function modelLimit(providerID, modelID) {
    if (cachedLimit !== null) return cachedLimit;
    if (!client || !providerID || !modelID) return null;
    try {
      const res = await client.config.providers();
      const providers = (res && res.data && res.data.providers) || res.providers || [];
      for (const prov of providers) {
        if (prov.id !== providerID) continue;
        const m = prov.models && prov.models[modelID];
        if (m && m.limit && m.limit.context) {
          cachedLimit = m.limit.context;
          return cachedLimit;
        }
      }
    } catch {
      /* fail-soft: 한도 미상이면 정지 판정 보류 (computeCtxState 가 ok 반환). */
    }
    return cachedLimit;
  }

  // 넛지: 1회 toast(없으면 무음 — fail-soft).
  async function notifyNudge(state, t) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message:
              `[ctx-guard] 잔여 컨텍스트 ~${Math.round(state.remainingPct)}% ` +
              `(넛지 임계 ${t.nudge_pct}%). 지금 티켓을 마무리하고, 큰 작업을 새로 ` +
              `시작하지 마라. 잔여 ${t.stop_pct}% 도달 시 자동 정지·핸드오프.`,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 넛지는 best-effort. */
    }
  }

  // STOP marker 박제 (ADR-0009 sentinel) — relay(pm_orch_opencode.py)가 이 marker 를
  // stat 해 세션 회전을 트리거한다. claude ctx_stop_hook._mark_triggered 와 동일 포맷
  // (경로 `<root>/.project_manager/.local/ctx-stop/<sanitize(sid)>.done`·내용 동일)로 써서
  // 같은 Supervisor 코드가 양 하니스를 구동한다(엔진 무변경). fail-soft — marker 실패해도
  // 정지(deny)는 유지. sid 미상이면 skip + stderr 경고(침묵 금지·silent-fail 방어).
  function writeStopMarker() {
    if (!root) return;
    if (typeof currentSessionID !== "string" || !currentSessionID.trim()) {
      process.stderr.write(
        "[ctx-guard] sessionID 미상 — STOP marker skip (relay 회전 트리거 누락 가능).\n",
      );
      return;
    }
    try {
      const dir = path.join(root, MARKER_REL);
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(
        path.join(dir, `${sanitizeSessionId(currentSessionID)}.done`),
        MARKER_CONTENT,
        "utf-8",
      );
    } catch {
      /* marker write 실패해도 정지(deny)는 유지 — best-effort(claude hook 동일). */
    }
  }

  return {
    // ── 토큰 추적: 어시스턴트 메시지 갱신마다 ctx% 재평가 ──────────────────
    event: async ({ event }) => {
      if (!event || event.type !== "message.updated") return;
      const info = event.properties && event.properties.info;
      if (!info) return;
      // sessionID 캡처 — STOP marker 파일명용(PluginInput 엔 sid 없음·실측). 어떤 메시지든
      // info.sessionID 가 실리면 보관(role 무관·marker 는 정지 시점에 이 값으로 쓴다).
      if (typeof info.sessionID === "string" && info.sessionID) {
        currentSessionID = info.sessionID;
      }
      if (info.role !== "assistant" || !info.tokens) return;

      const used = accumulateTokens(info.tokens);
      const limit = await modelLimit(info.providerID, info.modelID);
      const t = thresholds();
      const state = computeCtxState(used, limit, t);

      if (state.level === "stop" && !fired.stop) {
        fired.stop = true;
        writeStopMarker(); // STOP marker 직접 박제 (ADR-0038 D2·무조건·relay 회전 신호·no --trigger).
      } else if (state.level === "nudge" && !fired.nudge) {
        fired.nudge = true;
        await notifyNudge(state, t); // 넛지 toast (사람 UI·1회).
        // 모델-주입 안내 대기 (ADR-0037) — 다음 모델 호출의 system.transform 이 소비. toast(사람)
        // 와 별개로 모델이 실제로 받아 스스로 /pm-handoff 하게 한다(claude UserPromptSubmit 등가).
        pendingNudgeText = buildNudgeGuidance(state, t);
      }
    },

    // ── graceful nudge 모델-주입 (ADR-0037): nudge 안내를 system 에 비차단 1회 주입 ────
    // experimental.chat.system.transform 은 모델 호출 전 system[] 을 비차단 수정한다(@opencode-ai
    // /plugin Hooks·opencode 1.17.11 타입 확인). chat.message 의 full Part 구성(id/sessionID/
    // messageID 필수)보다 string push 가 안전·정확. ⚠️ experimental namespace — opencode 가 이 surface
    // 를 바꾸면 *조용히* 주입이 멈출 수 있다(hard-stop 은 무관·안전). 호환성 게이트 = T-0183 Tier2
    // 라이브 smoke(버전 회귀 포착)·codex 권고 반영. 변동 시 안정 chat.message 전환 검토.
    // 멱등: pendingNudgeText 를 1회 소비(push 후 null). hard-stop·deny 와 무관(안내만).
    "experimental.chat.system.transform": async (_input, output) => {
      if (pendingNudgeText && output && Array.isArray(output.system)) {
        output.system.push(pendingNudgeText);
        pendingNudgeText = null;
      }
    },

    // ── 하드 정지 (ADR-0038 D2): 새 작업만 deny·진행 중 핸드오프 도구는 예외 통과 ────
    // permission.ask 는 Permission best-effort 라 *확실한 새 작업만* 미리 deny(prompt 회피)하고
    // 핸드오프/불명은 통과시킨다(fail-open — 핸드오프를 절대 오차단하지 않는다). 실제 authoritative
    // gate 는 아래 tool.execute.before(clean schema).
    "permission.ask": async (input, output) => {
      if (fired.stop && isNewWorkPermission(input)) {
        output.status = "deny";
      }
    },

    // ── authoritative 하드 정지 gate: 정지 후 새 작업 도구 실행만 차단 (핸드오프 도구는 통과) ────
    "tool.execute.before": async (input, output) => {
      if (fired.stop && !isHandoffTool(input, output)) {
        throw new Error(
          "[ctx-guard] 컨텍스트 정지 임계 도달 — 새 작업 중단. 진행 중인 rich /pm-handoff " +
            "(핸드오프 도구)는 통과한다 — 핸드오프를 완료하고 이 세션을 종료한 뒤 새 세션에서 이어받아라.",
        );
      }
    },
  };
};

// CommonJS + (default) export — opencode autoload·node 자가검증 양쪽 지원.
module.exports = {
  CtxGuardPlugin,
  // 순수 결정 로직 (테스트·자가검증용 export).
  parseLocalConf,
  resolveThresholds,
  accumulateTokens,
  computeCtxState,
  buildNudgeGuidance,
  findEngineRoot,
  sanitizeSessionId,
  // 핸드오프 도구 allow-list (ADR-0038 D2 — claude _is_handoff_* 미러·테스트용 export).
  isHandoffBash,
  isHandoffTarget,
  isHandoffTool,
  isNewWorkPermission,
  NUDGE_PCT_DEFAULT,
  STOP_PCT_DEFAULT,
};
