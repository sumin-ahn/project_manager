// opencode 어댑터 — ctx 정지-핸드오프 plugin (T-0014, 설계 T-0012, 엔진 T-0013).
//
// 무엇:
//   opencode 세션의 컨텍스트 토큰 사용을 추적해, 임계 도달 시
//     1) 넛지(이른 경고·1회 toast) → "티켓 마무리·큰 거 새로 시작 마라"
//     2) 하드 정지(필수) → permission.ask deny + pm_handoff --trigger 1회(엔진 박제) + 새 세션 안내
//   하고, lossy 컴팩션은 opencode.jsonc `compaction.auto:false` 로 차단(우리 정지가 먼저 오게).
//
// 모델 (T-0012, 2D): 넛지(이른) → 하드 정지(필수) → 새 세션. compaction 회피.
//
// 임계값(엔진 T-0013): local.conf `ctx_nudge_pct`/`ctx_stop_pct`(기본 20/10) = "잔여 컨텍스트 %".
//   잔여% = (1 - used/limit) * 100.  잔여 ≤ nudge_pct → 넛지,  잔여 ≤ stop_pct → 정지.
//   plugin 은 local.conf 를 직접 파싱(의존 적음·board.py shell-out 회피).
//
// 멱등성(codex T-0013 인계): 넛지·정지·handoff 트리거는 세션당 각 1회만 (중복 호출 가드).
// sanity(codex 인계): 읽은 nudge/stop 이 비정상(음수·stop>nudge)이면 엔진 기본(20/10) 폴백.
//
// 엔진(pm_handoff/board) 미수정 — shell-out 호출만. 어댑터층(templates/opencode/.opencode/)만.
//
// 결정 로직(computeCtxState·resolveThresholds·parseLocalConf·accumulateTokens)은 순수 함수로
// 떼어 export — node 로 자가검증(이벤트/opencode 런타임 없이). plugin 함수는 그 wiring.

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

// ── 엔진 기본값 (board.py CTX_*_PCT_DEFAULT 미러 — 폴백 전용) ──────────────────
const NUDGE_PCT_DEFAULT = 20; // 잔여 ≤ 이 % → 넛지 (일은 계속).
const STOP_PCT_DEFAULT = 10; // 잔여 ≤ 이 % → 정지·핸드오프.

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

// ── thread-tail 추출 (handoff "다음 intent" 자동 채움 — T-0050·claude ctx_guard 미러) ──
//
// claude `ctx_guard.extract_thread_tail`(python)의 JS 재현. claude 는 transcript JSONL 을
// 읽지만 opencode plugin 은 transcript_path 미제공 → `opencode export <sid>` 의 messages
// 배열을 입력으로 받는다(같은 규칙·다른 소스). 엔진은 string 수용·삽입만(harness-agnostic seam).

// 추출 기본값 — lean(ADR-0008·ticket §결정). "방금 뭘" 미끼지 로그 복제 아님 (claude 미러).
const THREAD_TAIL_MAX_TURNS = 3; // 최근 user 발화 N턴까지만.
const THREAD_TAIL_MAX_CHARS = 600; // 결합 결과 총 길이 캡 (민감발화 노출·로그 비대 최소화).

// ── 순수 함수: 한 message 의 text part 결합 → 개행 ` / ` 평탄화 (claude _message_text 미러) ──
// opencode message = { info:{ role }, parts:[{ type, text }] }. `type === "text"` part 의
// `.text` 만 모아 `\n` 결합 후 개행을 ` / ` 로 1줄 평탄화한다. tool/step-start 등 비-text
// part 는 제외. text part 0개(tool_result·synthetic-only turn 동치)면 "".
function messageText(message) {
  if (!message || typeof message !== "object") return "";
  const parts = message.parts;
  if (!Array.isArray(parts)) return "";
  const texts = [];
  for (const part of parts) {
    if (!part || typeof part !== "object") continue;
    if (part.type === "text" && typeof part.text === "string") {
      texts.push(part.text);
    }
  }
  const text = texts.join("\n");
  // 개행을 1줄로 평탄화 (handoff entry 는 줄 단위 슬롯).
  const flat = text
    .split("\n")
    .map((seg) => seg.trim())
    .filter((seg) => seg)
    .join(" / ");
  return flat.trim();
}

// ── 순수 함수: 정지 직전 user 발화 N턴 추출 → 1줄 (claude extract_thread_tail 미러) ──
// 규칙(claude 동치):
//   - info.role === "user" message 만 (assistant 제외).
//   - turn 텍스트 = messageText(message). text part 0개면 skip (tool_result·synthetic 제외 동치).
//   - 배열 끝(최신)에서 역순 max_turns 개 수집 → 시간순(오래된→최신) 복원.
//   - turn 당 텍스트는 max_chars 로 truncate, 결합 결과도 총 max_chars 캡, 개행 ` / ` 평탄화.
//   - 빈/비배열/max_turns<=0/max_chars<=0 → "" (fail-soft — 엔진이 placeholder 유지).
function extractThreadTail(messages, max_turns = THREAD_TAIL_MAX_TURNS, max_chars = THREAD_TAIL_MAX_CHARS) {
  if (max_turns <= 0 || max_chars <= 0) return "";
  if (!Array.isArray(messages)) return "";

  const collected = []; // 역순(최신→오래) — 나중에 reverse.
  for (let i = messages.length - 1; i >= 0; i--) {
    if (collected.length >= max_turns) break;
    const message = messages[i];
    if (!message || typeof message !== "object") continue;
    const info = message.info;
    if (!info || typeof info !== "object" || info.role !== "user") continue;
    const text = messageText(message);
    if (!text) continue;
    collected.push(text.slice(0, max_chars));
  }

  if (collected.length === 0) return "";
  collected.reverse(); // 시간순(오래된→최신) 복원.
  return collected.join(" / ").slice(0, max_chars).trim();
}

// ── wiring: opencode export <sid> → messages 배열 (fail-soft) ─────────────────
// claude 는 transcript_path 를 hook 이 받지만 opencode plugin 은 미제공(T-0048 §결정) → 정지
// 시점에 `opencode export <sid> --pure` 로 세션 transcript 를 추출한다. `--pure` 는 export
// subprocess 가 plugin 을 재load 하지 않게 해 ctx-guard 재진입/부수효과를 막는다. `--sanitize`
// 미사용(claude 와 동일 — truncate+N턴 캡으로 노출 최소화·과도 redaction 회피). 실패/비-JSON/
// sid 미상/!root → [] (fail-soft — 정지·handoff 박제는 유지, thread-tail 만 빈 슬롯).
//
// maxBuffer 명시(64 MiB) — ctx-STOP 은 *정의상 컨텍스트가 가득 찬* 시점에 발화하므로 export JSON 이
// spawnSync 기본 maxBuffer(~1 MiB)를 쉽게 초과한다. 초과 시 stdout 이 잘려 JSON.parse 가 실패하고
// thread-tail 이 *항상 빈 값*으로 빠져 기능이 가장 필요한 긴 세션에서 조용히 무력화된다(codex T-0050
// must-fix). 버퍼는 STOP 1회·필요분만 할당이라 비용은 transient.
function exportSessionMessages(sid, root, runner = spawnSync) {
  if (!root || typeof sid !== "string" || !sid.trim()) return [];
  try {
    const res = runner("opencode", ["export", sid, "--pure"], {
      cwd: root,
      encoding: "utf-8",
      timeout: 30000,
      maxBuffer: 64 * 1024 * 1024,
    });
    if (!res || typeof res.stdout !== "string") return [];
    const data = JSON.parse(res.stdout);
    const messages = data && data.messages;
    return Array.isArray(messages) ? messages : [];
  } catch {
    /* spawn 실패·타임아웃·비-JSON 모두 [] — 정지 경로는 깨지 않는다(fail-soft). */
    return [];
  }
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

  // 정지: pm_handoff --trigger 1회 박제 (엔진 T-0013) + STOP marker(ADR-0009). 세션 종료/툴
  // 차단은 permission.ask 가 담당.
  function triggerHandoff(state) {
    if (!root) return;
    // 정지 직전 user 발화 추출 → handoff "다음 intent" thread-tail 슬롯 자동 채움(T-0050).
    // opencode export 로 세션 transcript 를 끌어와 claude 동치 규칙으로 N턴 추출한다. sid 미상·
    // 추출 빈이면 빈 문자열 → 인자 미추가(엔진 placeholder 유지·하위호환). export 실패해도
    // fail-soft(exportSessionMessages 가 [] 반환) — 정지·handoff 박제는 영향 없다.
    const threadTail = currentSessionID
      ? extractThreadTail(
          exportSessionMessages(currentSessionID, root),
          THREAD_TAIL_MAX_TURNS,
          THREAD_TAIL_MAX_CHARS,
        )
      : "";
    let handoffRc = null; // null = spawn 실패/타임아웃/킬 (rc 미상).
    try {
      const handoffArgs = [
        path.join(root, ENGINE_REL, "pm_handoff.py"),
        "--trigger",
        "--reason",
        "ctx-stop",
        "--ctx-pct",
        String(Math.max(0, Math.round(state.remainingPct))),
      ];
      // thread-tail 은 raw 전달 — 엔진 _flatten_thread_tail 이 단일 계약지점에서 sanitize
      // (claude run_handoff 동치·어댑터 재방어 안 함). 비어있으면 미추가(하위호환).
      if (threadTail) {
        handoffArgs.push("--thread-tail", threadTail);
      }
      const res = spawnSync("python3", handoffArgs, {
        cwd: root,
        stdio: "ignore",
        timeout: 30000,
      });
      handoffRc = res.status; // 0=박제 성공.
    } catch {
      /* handoff 박제 실패해도 정지(deny)는 유지 — 안전측. */
    }
    // STOP marker 는 handoff *성공*(rc 0) 시에만 — 실패면 새 PM 이 *권위 handoff 없이* stale
    // context 로 부트스트랩하므로 회전을 트리거하지 않는다(claude ctx_stop_hook 가 handoff_rc==0
    // 시에만 marker 남기는 선례·codex T-0048 must-fix). 정지(deny)는 rc 무관 유지.
    if (handoffRc === 0) {
      writeStopMarker(); // claude ctx_stop_hook 가 _mark_triggered 하는 자리.
    } else {
      process.stderr.write(
        `[ctx-guard] pm_handoff --trigger 실패(rc=${handoffRc}) — STOP marker 보류 ` +
          "(권위 handoff 없는 회전 방지). 정지(deny)는 유지.\n",
      );
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
        triggerHandoff(state); // 핸드오프 박제 (1회).
      } else if (state.level === "nudge" && !fired.nudge) {
        fired.nudge = true;
        await notifyNudge(state, t); // 넛지 (1회).
      }
    },

    // ── 하드 정지 레버: 정지 트리거 후 모든 툴 권한 deny ───────────────────
    "permission.ask": async (_input, output) => {
      if (fired.stop) {
        output.status = "deny";
      }
    },

    // ── 보조 하드 정지: 정지 후 툴 실행 차단 (permission deny 우회 대비) ────
    "tool.execute.before": async () => {
      if (fired.stop) {
        throw new Error(
          "[ctx-guard] 컨텍스트 정지 임계 도달 — 작업 중단. pm_handoff 핸드오프가 " +
            "박제되었다. 이 세션을 종료하고 새 세션에서 핸드오프를 이어받아라.",
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
  findEngineRoot,
  sanitizeSessionId,
  // thread-tail 추출 (T-0050 — claude extract_thread_tail 미러·테스트용 export).
  messageText,
  extractThreadTail,
  exportSessionMessages,
  NUDGE_PCT_DEFAULT,
  STOP_PCT_DEFAULT,
  THREAD_TAIL_MAX_TURNS,
  THREAD_TAIL_MAX_CHARS,
};
