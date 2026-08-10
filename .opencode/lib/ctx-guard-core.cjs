// opencode 어댑터 — ctx checkpoint 가드 core (T-0551, ADR-0081 Decision 1·3).
//
// 이 파일(CJS)은 ctx-guard 의 *순수 로직 + 플러그인 팩토리 본체*를 담는다. opencode plugin
// 진입점은 `../plugins/ctx-guard.js`(ESM 얇은 shim)이며 여기서 CtxGuardPlugin 팩토리만 named-export
// 한다 — opencode 의 plugin 로드 규약(각 plugins/ 파일의 export 를 순회해 *모두 함수*이길 요구하고
// 각각을 팩토리로 호출·실측 T-0283) 때문에, 순수 헬퍼·상수는 plugins/ *바깥*(이 lib/ 모듈)에 둔다
// (opencode 는 plugins/ 만 스캔·lib/ 는 로드 안 함). node 자가검증 test 는 이 CJS 모듈을 require 해
// 순수 함수(parseLocalConf·computeCtxState 등)를 이벤트/opencode 런타임 없이 검증한다.
//
// 무엇:
//   opencode 세션의 컨텍스트 토큰 사용을 추적해 임박 밴드에서 checkpoint 박제를 안내하고,
//   session.compacted 를 병행 관측해 세션-로컬 횟수 누적·밴드 재무장·압축 후 checkpoint 안내를 한다.
//   관측 횟수는 표시하지 않고 plugin 재기동 시 0부터 다시 시작한다.
//
// 모델 (ADR-0081): 사전 checkpoint 넛지 → native compaction → 사후 checkpoint 넛지.
//
// 임계값(엔진 T-0013·T-0207 상향): local.conf `ctx_nudge_pct`/`ctx_stop_pct`(기본 30/20) = "잔여 컨텍스트 %".
//   잔여% = (1 - used/limit) * 100. computeCtxState 의 stop 반환은 파리티를 위해 유지하고,
//   plugin 은 stop 밴드를 최종 checkpoint 안내로만 흡수한다(차단·marker 없음).
//   plugin 은 local.conf 를 직접 파싱(의존 적음·board.py shell-out 회피).
//
// 멱등성: nudge/nudge2 는 compaction 사이클당 각 1회. session.compacted 가 둘을 re-arm 한다.
// sanity: 읽은 nudge/stop 이 비정상(음수·stop>nudge)이면 엔진 기본(30/20) 폴백.
//
// 엔진(pm_handoff/board) 미수정 — shell-out 호출만. 어댑터층(templates/opencode/.opencode/)만.
//
// 결정 로직(computeCtxState·resolveThresholds·resolveBudget·parseLocalConf·accumulateTokens)은
// 순수 함수로 떼어 export — node 로 자가검증(이벤트/opencode 런타임 없이). plugin 함수는 그 wiring.

const fs = require("node:fs");
const path = require("node:path");

// ── 엔진 기본값 (board.py CTX_*_PCT_DEFAULT 미러 — 폴백 전용) ──────────────────
// T-0207 상향(20/10→30/20). stop 값은 computeCtxState 파리티와 nudge2 파생에 계속 쓰인다.
const NUDGE_PCT_DEFAULT = 30; // 잔여 ≤ 이 % → 넛지 (일은 계속).
const STOP_PCT_DEFAULT = 20; // 구 stop 경계(판정만 유지·차단 소비 없음).

// 2단(strong) nudge 임계 마진 (%p·파생값·T-0328·ADR-0037). nudge2 밴드 = stop_pct < 잔여 ≤
// min(stop_pct + 이 마진, nudge_pct) — compaction 임박 전 강한 유도.
// config 노브 신설 없이 stop_pct 에서 파생. claude ctx_guard.CTX_NUDGE2_MARGIN_PCT 와 미러.
const NUDGE2_MARGIN_PCT = 3;

// ── ctx 예산 기본 (board.py CTX_WINDOW_TOKENS_DEFAULT 미러 · ADR-0041) ──────────
// 정지/넛지 분모(100% 기준)의 최종 폴백. 물리 window auto-detect 개념 폐기 —
// resolveBudget 이 하네스별 오버라이드 > generic > 이 기본 순으로 예산 하나를 해소한다.
const CTX_WINDOW_TOKENS_DEFAULT = 200000;

// 엔진 경로 (plugin 의 directory 기준 .project_manager 까지 거슬러 올라가 해석).
const LOCAL_CONF_REL = path.join(".project_manager", "local.conf");
// 세션 parentID 역조회는 ctx guard 의 보조 입력이다. SDK가 응답하지 않아 이벤트 처리가 멈추지
// 않도록 짧게 제한한다.
const SESSION_LOOKUP_TIMEOUT_MS = 1000;

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

// ── 순수 함수: ctx 예산 해소 (하네스별 · ADR-0041) ────────────────────────────
// 정지/넛지 분모(100% 기준) = 해소된 예산 하나 (물리한도 개념 폐기). precedence:
//   ctx_window_tokens_<harness>  (하네스별 오버라이드)
//   > ctx_window_tokens          (generic·back-compat)
//   > CTX_WINDOW_TOKENS_DEFAULT  (200000)
// 각 층 >0 정수 sanity — ≤0·비정수·미설정이면 다음 층으로(0/음수 특수의미 없음).
// claude ctx_guard.resolve_budget(conf,"opencode") 동형. conf = parseLocalConf 결과.
function resolveBudget(conf, harness) {
  const readBudget = (key) => {
    const raw = conf ? conf[key] : undefined;
    if (raw === undefined || raw === null) return null;
    const n = Number(String(raw).trim());
    return Number.isInteger(n) && n > 0 ? n : null;
  };
  return (
    readBudget(`ctx_window_tokens_${harness}`) ||
    readBudget("ctx_window_tokens") ||
    CTX_WINDOW_TOKENS_DEFAULT
  );
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

// ── 순수 함수: 2단(strong) nudge 임계 (stop_pct + margin·nudge_pct 캡·T-0328) ────────
// nudge2 밴드 = stop_pct < 잔여 ≤ 이 값. margin(+3)이 nudge 밴드를 넘지 않게 min 으로 캡해
// nudge2 가 nudge 밴드 밖(ok 영역)으로 새지 않는다. claude ctx_guard.nudge2_threshold 와 동형.
function nudge2Threshold(thresholds) {
  const stop = thresholds && thresholds.stop_pct != null ? thresholds.stop_pct : STOP_PCT_DEFAULT;
  const nudge = thresholds && thresholds.nudge_pct != null ? thresholds.nudge_pct : NUDGE_PCT_DEFAULT;
  return Math.min(stop + NUDGE2_MARGIN_PCT, nudge);
}

// ── 순수 함수: ctx 상태 판정 (테스트 핵심) ──────────────────────────────────
// used: accumulateTokens 결과, limit: 해소된 ctx 예산(resolveBudget·ADR-0041·구 물리한도 폐기),
// thresholds: resolveThresholds 결과. 반환: { remainingPct, usedPct, level: "ok"|"nudge"|"nudge2"|"stop" }.
//   nudge2(T-0328) = 구 stop 경계 직전 강한 유도 밴드. limit 미상이면 level "ok".
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
  else if (remainingPct <= nudge2Threshold(thresholds)) level = "nudge2";
  else if (remainingPct <= nudge_pct) level = "nudge";
  return { remainingPct, usedPct, level };
}

// ── 순수 함수: compaction 전 checkpoint 안내문 (모델-facing 비차단 주입) ────────
// 잔여 nudge 밴드에서 온전한 컨텍스트로 ticket 경계를 정리하고 checkpoint 를 박제하게 한다.
// experimental.chat.system.transform 이 이 문자열을 system[] 에 push 한다.
// 두 builder의 thresholds 인자는 Claude 미러 시그니처를 보존하려고 남긴다(현재 문구에서는 미사용).
function buildNudgeGuidance(state, thresholds) {
  const remaining = Math.round((state && state.remainingPct) || 0);
  const used = Math.round((state && state.usedPct) || 0);
  return (
    `[ctx-nudge] 컨텍스트 사용 ${used}% (잔여 ${remaining}%) — 박제 준비 구간. ` +
    `현재 ticket 경계까지만 마무리하고 complete entry 로 결과를 박제하라. 다음 구간에 들어가기 전에 ` +
    `\`python3 .project_manager/tools/pm_log.py checkpoint --task <이름>\` 을 실행해 현재 서사를 보존하라.`
  );
}

// ── 순수 함수: 2단(strong) checkpoint 안내문 (compaction 임박) ────────────────
// 1단을 놓쳤거나 건너뛴 세션에 checkpoint 실행을 즉시 지시한다. 여전히 비차단 안내다.
function buildNudge2Guidance(state, thresholds) {
  const remaining = Math.round((state && state.remainingPct) || 0);
  return (
    `[ctx-nudge/강화] 컨텍스트 사용 ${Math.round((state && state.usedPct) || 0)}% ` +
    `(잔여 ${remaining}%) — 지금 ` +
    `\`python3 .project_manager/tools/pm_log.py checkpoint --task <이름>\` 을 실행하고 ` +
    `checkpoint entry 의 구간·서사를 채워 현재 상태를 즉시 박제하라.`
  );
}

// ── 순수 함수: compaction 후 checkpoint 안내문 ─────────────────────────────
function buildCompactedGuidance() {
  return (
    `[ctx-checkpoint/압축후] compaction이 방금 일어났다. ` +
    `직전 박제 경계 이후 구간을 ` +
    `\`python3 .project_manager/tools/pm_log.py checkpoint --task <이름> --trigger compaction\` ` +
    `으로 기록하고, 생성된 골격의 구간·서사 불릿을 즉시 채워 요약 직후 ` +
    `결정·진행·검증 상태를 보충 박제하라. ` +
    `(ADR-0081).`
  );
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

// ── plugin 팩토리 (진입점 ../plugins/ctx-guard.js 가 ESM named-export 로 재노출·opencode autoload) ──
const CtxGuardPlugin = async ({ client, directory, worktree, $ }) => {
  // 로드 검증 마커 (env-gated·라이브-로드 게이트 T-0283 전용·실 세션엔 무음). opencode 는 성공 로드
  // 시 positive 로그를 남기지 않아(실측 1.17.18) — factory 실행을 관측하려면 스스로 마커를 낸다.
  // 훅 로직 무관 — CTX_GUARD_LOAD_PROBE 미설정 시 완전 무음(프로덕션 기본).
  if (process.env.CTX_GUARD_LOAD_PROBE) {
    process.stderr.write("[ctx-guard] plugin factory loaded (CTX_GUARD_LOAD_PROBE)\n");
  }
  let cachedThresholds = null;
  let cachedConf = null;
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
        compactionCount: 0,
        cycleEpoch: 0,
        childLookup: null,
      };
      sessionStates.set(sessionID, state);
    }
    return state;
  }

  const root = findEngineRoot(directory || worktree || process.cwd());

  // local.conf 직접 파싱 (thresholds·budget 공용·1회 캐시). root 없거나 실패 시 {}.
  function loadConf() {
    if (cachedConf) return cachedConf;
    let conf = {};
    if (root) {
      try {
        const p = path.join(root, LOCAL_CONF_REL);
        if (fs.existsSync(p)) conf = parseLocalConf(fs.readFileSync(p, "utf-8"));
      } catch {
        conf = {};
      }
    }
    cachedConf = conf;
    return cachedConf;
  }

  // local.conf → 임계값 (1회 캐시).
  function thresholds() {
    if (cachedThresholds) return cachedThresholds;
    cachedThresholds = resolveThresholds(loadConf());
    return cachedThresholds;
  }

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
  async function notifyNudge(state, t) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message:
              `[ctx-guard] 잔여 컨텍스트 ~${Math.round(state.remainingPct)}% ` +
              `(넛지 임계 ${t.nudge_pct}%). ticket/wave 경계를 마무리하고 checkpoint 박제를 ` +
              `준비하라.`,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 넛지는 best-effort. */
    }
  }

  // 2단 넛지: 사이클당 1회 강한 toast(compaction 임박). 사람용 2단 표시(없으면 무음).
  async function notifyNudge2(state, t) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message:
              `[ctx-guard/임박] 잔여 컨텍스트 ~${Math.round(state.remainingPct)}% — ` +
              `auto-compaction 임박. 지금 pm_log.py checkpoint 를 실행하고 새 큰 작업은 ` +
              `시작하지 마라.`,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 넛지는 best-effort. */
    }
  }

  // compaction 직후 사람용 toast. 모델 안내는 pendingNudgeText/system.transform 채널을 병행한다.
  async function notifyCompacted() {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message:
              `[ctx-guard] compaction이 방금 일어남 — ` +
              `지금 pm_log.py checkpoint 로 상태를 보충 박제하라.`,
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
        // opencode v1.18.5 packages/opencode/src/plugin/index.ts 의 event dispatcher는 hook Promise를
        // await하지 않는다. auto-continue의 첫 system.transform보다 앞서도록 첫 await 전에 적재한다.
        const stagedGuidance = buildCompactedGuidance();
        session.pendingNudgeText = stagedGuidance;

        if (await isChildSession(sessionID)) {
          if (session.pendingNudgeText === stagedGuidance) session.pendingNudgeText = null;
          return;
        }
        await notifyCompacted();
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
      const limit = resolveBudget(loadConf(), "opencode"); // ctx 예산 (물리한도 폐기·ADR-0041).
      const t = thresholds();
      const state = computeCtxState(used, limit, t);

      // stop의 차단 소비만 폐지했다. stop 밴드 자체는 최종(nudge2) checkpoint 안내로 무음 흡수한다.
      if ((state.level === "nudge2" || state.level === "stop") && !session.fired.nudge2) {
        // 2단(strong·compaction 임박) — 1단 발화 여부와 독립(1단 창을 건너뛴 세션도 발화).
        session.fired.nudge2 = true;
        // 2단 모델-주입 안내 대기 — 1단과 동일 채널(system.transform 1회 소비).
        session.pendingNudgeText = buildNudge2Guidance(state, t);
        await notifyNudge2(state, t); // 2단 강한 toast (사람 UI·1회).
      } else if (state.level === "nudge" && !session.fired.nudge) {
        session.fired.nudge = true;
        // 다음 모델 호출의 system.transform 이 checkpoint 안내를 소비한다.
        session.pendingNudgeText = buildNudgeGuidance(state, t);
        await notifyNudge(state, t); // 넛지 toast (사람 UI·1회).
      }
    },

    // ── checkpoint 모델-주입: pending 안내를 system 에 비차단 1회 주입 ────────
    // experimental.chat.system.transform 은 모델 호출 전 system[] 을 비차단 수정한다(@opencode-ai
    // /plugin Hooks·opencode 1.17.11 타입 확인). chat.message 의 full Part 구성(id/sessionID/
    // messageID 필수)보다 string push 가 안전·정확. ⚠️ experimental namespace — opencode 가 이 surface
    // 를 바꾸면 *조용히* 주입이 멈출 수 있다. 호환성 게이트 = T-0183 Tier2
    // 라이브 smoke(버전 회귀 포착)·codex 권고 반영. 변동 시 안정 chat.message 전환 검토.
    // 멱등: 해당 SID의 pendingNudgeText 만 1회 소비(push 후 null). 자식 세션은 주입도 면제한다.
    "experimental.chat.system.transform": async (input, output) => {
      const sessionID = input && input.sessionID;
      if (await isChildSession(sessionID)) return;
      const session = sessionState(sessionID);
      if (session && session.pendingNudgeText && output && Array.isArray(output.system)) {
        output.system.push(session.pendingNudgeText);
        session.pendingNudgeText = null;
      }
    },
  };
};

// CommonJS export — node 자가검증(require)·ESM shim(../plugins/ctx-guard.js) 양쪽 소비.
// opencode 는 이 모듈을 직접 로드하지 않는다(plugins/ 만 스캔) — 얇은 ESM shim 이 CtxGuardPlugin 만
// named-export 해 로드 규약(export=단일 함수·실측 T-0283)을 만족한다. 여기 export 는 순수함수+상수+팩토리.
module.exports = {
  CtxGuardPlugin,
  // 순수 결정 로직 (테스트·자가검증용 export).
  parseLocalConf,
  resolveThresholds,
  resolveBudget,
  accumulateTokens,
  nudge2Threshold,
  computeCtxState,
  buildNudgeGuidance,
  buildNudge2Guidance,
  buildCompactedGuidance,
  findEngineRoot,
  SESSION_LOOKUP_TIMEOUT_MS,
  NUDGE_PCT_DEFAULT,
  STOP_PCT_DEFAULT,
  NUDGE2_MARGIN_PCT,
  CTX_WINDOW_TOKENS_DEFAULT,
};
