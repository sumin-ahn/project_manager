// opencode 어댑터 — stall-watchdog 플러그인 core (세션 idle 미완료 감지·처방 넛지).
//
// 이 파일(CJS)은 stall-watchdog 의 *순수 로직 + 플러그인 팩토리 본체*를 담는다. opencode plugin
// 진입점은 `../plugins/stall-watchdog.js`(ESM 얇은 shim)이며 거기서 StallWatchdogPlugin 하나만
// named-export 한다 — opencode 의 plugin 로드 규약(각 plugins/ 파일의 export 를 순회해 *모두 함수*
// 이길 요구하고 각각을 팩토리로 호출·실측) 때문에, 순수 헬퍼·상수는 plugins/ *바깥*(이 lib/ 모듈)에
// 둔다(opencode 는 plugins/ 만 스캔·lib/ 는 로드 안 함). node 자가검증 test 는 이 CJS 모듈을
// require 해 순수 함수(classifyStall·shouldNudge·buildNudge…)를 opencode 런타임 없이 검증한다.
//
// ⚠️ 이 core 는 `@opencode-ai/plugin`(tool 헬퍼·zod)을 require 하지 않는다(safe-write-core.cjs 와
//    동일 계약 — 그 패키지는 opencode 런타임에만 설치되므로 core 가 직접 require 하면 plain node
//    자가검증이 깨진다). 이 플러그인은 custom tool 이 없어 shim 주입도 불필요하다. client(SDK)만
//    makeStallWatchdogPlugin(client) 커링으로 받는다 — client 는 shim import 시점엔 없고 opencode 가
//    팩토리 호출 시점에 건네므로, shim 은 `(ctx) => core.makeStallWatchdogPlugin(ctx.client)(ctx)`
//    로 재위임한다.
//
// 무엇 (T-opencode-003):
//   PM 프라이머리·서브에이전트 공통의 "할 일을 남긴 채 조용히 턴 종료" 멈춤(선행 sweep 실측
//   NO-WRITE 17/30 · upstream auto-continue 부재 #17471)을 session.idle 이벤트에서 감지해 원인별
//   처방 넛지를 client.session.prompt() 로 자동 주입한다. "continue" 가 아니라 진단별 행동 지시다.
//
//   신호 3종 (신규 클래스 추가는 티켓 범위 밖):
//     - declare-no-action : 쓰기/생성 선언("생성하겠습니다", "I'll create...") 후 tool 파트 없음.
//     - truncated         : 문장 중간 종결 휴리스틱(매달린 괄호·미종결 펜스 등). *단일 신호로는
//                           넛지하지 않고* 미완료 todo 와 결합될 때만 처방한다(오검출 억제).
//     - open-todos        : 미완료 todo 잔존 + 결론부 없는 종료.
//   보수적 트리거: todo 부재 + 결론형 종료 = 정상(사용자 대기) → 넛지하지 않는다. abort 직후 종료도
//   넛지하지 않는다(사용자 중단 의사 존중 — isAbortOutcome 으로 배제).
//
// 게이트 (shouldNudge): 연속 무진행 카운터가 MAX_CONSEC(기본 3)에 도달하면 차단한다 — 긴 wave 를
//   막지 않으려는 상한이 아니라 같은 멈춤에 대한 반복 소음 차단이다(스트릭당 최대 3회 넛지). 실제
//   진행(todo 완료 수 변동)이 관측되면 연속 카운터를 리셋한다. 세션 절대 백스톱 MAX_TOTAL(기본 20)
//   은 휴리스틱 오작동 시 토큰 소방용이다. 사용자 메시지 도착(마지막 assistant 이후 user 메시지)
//   시 워치독을 영구 해제한다.
//
// 영속화 (핵심 — headless one-shot 모델): `opencode run --continue` 는 프로세스-당-턴 one-shot
//   (ctx-guard-core.cjs L369 문서화)이라 in-memory 카운터는 매 턴 소멸한다. 그래서 게이트 상태를
//   sessionID 키 durable marker(`<root>/.project_manager/.local/stall-watchdog/state.<sid>.json`,
//   ctx-guard marker 선례 준용)로 디스크에 영속화해 프로세스 간 유지한다. IO 실패는 전부 흡수한다
//   (never-block — 관측·넛지 채널 실패가 세션을 막지 않는다). 엔진 root(.project_manager 발견)를 못
//   찾으면 영속화 없이 이벤트마다 기본 상태로 동작한다(게이트 약화 — PM 어댑터라 root 부재는 비정형).
//
// 진행 감지 v1: todo 기반만(client.session.todo 완료 수 변동). 파일 변경 감지는 비용·경합 불명으로
//   범위 밖(아키텍트 권고 수용). SDK 표면 실측(@opencode-ai/sdk gen):
//     event `session.idle {properties:{sessionID}}` · client.session.messages({sessionID}) ->
//     Array<{info, parts}> · client.session.todo({sessionID}) -> Array<Todo> ·
//     client.session.prompt({path:{id},body:{parts:[...]}}) — 메서드명은 prompt(chat 아님·architect must-fix)
//     ·파라미터는 path 형태여야 한다: flat {sessionID} 는 서버 UnknownError(2026-08-25 프로브 실측).

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

// ── 임계값 상수 (core 상단 명시·env override) ────────────────────────────────
// MAX_CONSEC: 연속 무진행 허용 상한 — 카운터가 이 횟수에 도달하면 차단(스트릭당 최대 이 횟수만큼
//             넛지·진행 보이면 리셋).
// MAX_TOTAL : 세션 절대 백스톱 — 누적 넛지 상한(휴리스틱 오작동 시 토큰 소방용).
const MAX_CONSEC_DEFAULT = 3;
const MAX_TOTAL_DEFAULT = 20;
const MAX_CONSEC_ENV = "PM_STALL_WATCHDOG_MAX_CONSEC";
const MAX_TOTAL_ENV = "PM_STALL_WATCHDOG_MAX_TOTAL";
const DISABLED_ENV = "PM_STALL_WATCHDOG_DISABLED";

// SDK 조회 타임아웃 — messages/todo 역조회가 이벤트 처리를 붙잡지 않게 짧게 제한(ctx-guard 선례).
const SDK_LOOKUP_TIMEOUT_MS = 5000;

// 마커 위치 — ctx-guard durable marker(.project_manager/.local/ctx-stop/)와 같은 .local 산출물 축.
const MARKER_DIR_REL = path.join(".project_manager", ".local", "stall-watchdog");

// ── 순수 함수: env override 해소 (>0 정수 sanity·아니면 기본 / safe-write resolvePositiveInt 동형) ──
function resolvePositiveInt(raw, dflt) {
  if (raw === undefined || raw === null) return dflt;
  const n = Number(String(raw).trim());
  return Number.isInteger(n) && n > 0 ? n : dflt;
}

function resolveMaxConsec(env) {
  return resolvePositiveInt(env ? env[MAX_CONSEC_ENV] : undefined, MAX_CONSEC_DEFAULT);
}

function resolveMaxTotal(env) {
  return resolvePositiveInt(env ? env[MAX_TOTAL_ENV] : undefined, MAX_TOTAL_DEFAULT);
}

function isDisabled(env) {
  const raw = env ? String(env[DISABLED_ENV] || "").trim().toLowerCase() : "";
  return raw === "1" || raw === "true" || raw === "yes" || raw === "on";
}

// ── 순수 함수: 쓰기/생성 의사 선언 판정 (declare-no-action 첫 신호) ───────────
// 한국어 선언형(~하겠다) + 영어 의도 선언(I'll/Let me + 동사). 과대 매칭보다 미매칭이 나은
// 보수 집합 — declare-no-action 오판은 정상 종료를 방해한다.
const DECLARE_PATTERNS = [
  /생성하겠|작성하겠|만들겠|쓰겠|저장하겠|추가하겠|수정하겠|구현하겠|작업하겠|실행하겠|적용하겠/i,
  /\bI'?ll\s+(create|write|make|generate|add|implement|build|save|fix|update|run|apply)/i,
  /\bLet me\s+(create|write|make|generate|add|implement|build|fix|update|check|apply)/i,
];

function declaresWriteIntent(text) {
  const t = typeof text === "string" ? text : "";
  if (!t.trim()) return false;
  return DECLARE_PATTERNS.some((re) => re.test(t));
}

// ── 순수 함수: 코드 펜스 카운트 (truncated 휴리스틱 — 미종결 코드블록 감지) ────
function countFences(text) {
  const t = typeof text === "string" ? text : "";
  const matches = t.match(/^[ \t]*```/gm);
  return matches ? matches.length : 0;
}

// ── 순수 함수: truncated 휴리스틱 (문장 중간 종결 추정 — 약한 신호) ────────────
// 미종결 펜스(홀수)·매달린 여는 괄호·끝이 이어질 기호(,:+=-*…). 단독으로는 stall 이 아니며
// classifyStall 에서 미완료 todo 와 결합될 때만 처방된다(정상 답변의 코드블록 종결 오탐 억제).
function looksTruncated(text) {
  const t = typeof text === "string" ? text.trim() : "";
  if (!t) return false;
  if (countFences(t) % 2 === 1) return true; // 미종결 코드블록.
  const last = t[t.length - 1];
  if ("{[(".includes(last)) return true; // 매달린 여는 괄호.
  if (",:+-=*>".includes(last)) return true; // 이어질 기호(목록·연산자·인용).
  return false;
}

// ── 순수 함수: 결론형 종료 판정 (open-todos 게이트 — 사용자 대기 정상 구분) ────
// 종결 문장부호 또는 한국어 종결어미로 끝나거나 닫힌 코드블록으로 끝나면 결론으로 본다.
function endsConclusively(text) {
  const t = typeof text === "string" ? text.trim() : "";
  if (!t) return false;
  if (countFences(t) % 2 === 1) return false; // 미종결 코드블록은 비결론.
  if (/```[ \t]*$/.test(t)) return true; // 닫는 펜스로 끝 = 완결 출력.
  const last = t[t.length - 1];
  if (".!?…~".includes(last)) return true;
  return /(다|요|임|함|됨)$/.test(t);
}

// ── 순수 함수: 미완료 todo 카운트 (pending+in_progress = 남은 일) ─────────────
function countUnfinishedTodos(pendingTodos) {
  if (!Array.isArray(pendingTodos)) return 0;
  return pendingTodos.filter(
    (t) => t && (t.status === "pending" || t.status === "in_progress"),
  ).length;
}

function completedTodoCount(todos) {
  if (!Array.isArray(todos)) return 0;
  return todos.filter((t) => t && t.status === "completed").length;
}

// ── 핵심 순수 함수: 멈춤 신호 분류기 ──────────────────────────────────────────
// lastAssistantText: 마지막 assistant 메시지의 text 파트 결합. pendingTodos: session.todo 원본 배열.
// hasToolPartAfter: 마지막 text 파트 *이후* tool 파트 존재(플러그인이 parts 에서 산출해 주입).
// 반환: { stall: boolean, kind: "declare-no-action"|"truncated"|"open-todos"|null }.
// 우선순위: declare-no-action > truncated(결합 시) > open-todos. 판정 근거:
//   1) 빈 텍스트 → 관측 불량, 보수적으로 정상.
//   2) 선언 + tool 파트 없음 → declare-no-action (todo 와 무관 — 실행 없는 종료 자체가 고장).
//   3) 미완료 todo + 비결론 종료 → truncated(truncated 신호 있을 때만·더 구체 진단) 아니면 open-todos.
//      todo 부재·결론형 종료는 정상(사용자 대기) → stall=false. truncated 단독은 stall 아님.
function classifyStall(lastAssistantText, pendingTodos, hasToolPartAfter) {
  const text = typeof lastAssistantText === "string" ? lastAssistantText : "";
  if (!text.trim()) return { stall: false, kind: null };
  const toolAfter = hasToolPartAfter === true;
  if (declaresWriteIntent(text) && !toolAfter) {
    return { stall: true, kind: "declare-no-action" };
  }
  const unfinished = countUnfinishedTodos(pendingTodos);
  if (unfinished > 0 && !endsConclusively(text)) {
    return looksTruncated(text)
      ? { stall: true, kind: "truncated" }
      : { stall: true, kind: "open-todos" };
  }
  return { stall: false, kind: null };
}

// ── 순수 함수: abort 직후 판정 (사용자 중단 존중 — 넛지 금지) ─────────────────
// AssistantMessage.error.name 이 MessageAbortedError 거나 finish 필드에 abort 표식이 있으면
// 사용자(또는 시스템)가 턴을 중단한 것이다 — 분류 결과와 무관하게 넛지하지 않는다.
function isAbortOutcome(message) {
  if (!message || typeof message !== "object") return false;
  const errName = message.error && message.error.name;
  if (errName === "MessageAbortedError") return true;
  return typeof message.finish === "string" && /abort/i.test(message.finish);
}

// ── 순수 함수: 처방 넛지 문열 (진단별 행동 지시 — deny-and-redirect buildDenyMessage 동철학) ──
// 3종 문어체는 티켓 계약 그대로 유지한다. 알려지지 않은 kind 는 null(발화 스킵).
function buildNudge(kind) {
  if (kind === "declare-no-action") {
    return (
      "[stall-watchdog] 대형 단일 출력 시도로 보인다 — safe_write 8KB 청크(create→append)로 " +
      "나눠 써라. 반복 내용이면 bash 생성이 낫다."
    );
  }
  if (kind === "truncated") {
    return "[stall-watchdog] 응답이 잘렸다 — 이어서 계속하되 남은 산출은 파일로 써라.";
  }
  if (kind === "open-todos") {
    return "[stall-watchdog] 남은 todo 중 가장 작은 것 하나부터 수행하라.";
  }
  return null;
}

// ── 게이트 상태 (순수 전이 함수들 — 영속 JSON 의 shape) ──────────────────────
// consecutiveIdle : 연속 무진행(idle stall) 카운터 — 진행 관측 시 리셋.
// totalNudges     : 세션 누적 넛지 — 절대 백스톱(MAX_TOTAL) 소방용.
// released        : 사용자 메시지 도착으로 영구 해제.
// lastCompletedCount : 마지막 관측 todo 완료 수 — 진행 감지(v1 todo 기반) 기준선.
function freshStallState() {
  return { consecutiveIdle: 0, totalNudges: 0, released: false, lastCompletedCount: 0 };
}

// 영속 값 방어 정규화 — 부분/오염 JSON 을 유효 상태로 접어 게이트 오동작을 막는다.
function normalizeStallState(raw) {
  const base = freshStallState();
  if (!raw || typeof raw !== "object") return base;
  const int = (v, dflt) => {
    const n = Number(v);
    return Number.isFinite(n) && n >= 0 && Number.isInteger(n) ? n : dflt;
  };
  return {
    consecutiveIdle: int(raw.consecutiveIdle, base.consecutiveIdle),
    totalNudges: int(raw.totalNudges, base.totalNudges),
    released: raw.released === true,
    lastCompletedCount: int(raw.lastCompletedCount, base.lastCompletedCount),
  };
}

function recordProgress(state, completedCount) {
  return {
    ...normalizeStallState(state),
    consecutiveIdle: 0,
    lastCompletedCount: completedCount,
  };
}

function releaseWatchdog(state) {
  return { ...normalizeStallState(state), released: true };
}

function recordNudgeFired(state) {
  const s = normalizeStallState(state);
  return { ...s, consecutiveIdle: s.consecutiveIdle + 1, totalNudges: s.totalNudges + 1 };
}

// ── 핵심 순수 함수: 진행 게이트 ───────────────────────────────────────────────
// 통과 조건: 해제 안 됨 && 누적 < MAX_TOTAL && 연속 무진행 카운터가 MAX_CONSEC 에 *도달 전*.
// 카운터가 MAX_CONSEC 에 도달하면 같은 멈춤의 반복 넛지를 차단한다(스트릭당 최대 MAX_CONSEC 회
// 넛지 — architect AT-001 "연속 무진행 3 차단"). 진행(recordProgress)이 관측돼야 재무장된다.
// limits 미주입 시 process.env 에서 해소한다(기본 3/20).
function shouldNudge(state, limits) {
  const s = normalizeStallState(state);
  if (s.released) return false;
  const maxConsec =
    limits && Number.isInteger(limits.maxConsec)
      ? limits.maxConsec
      : resolveMaxConsec(typeof process !== "undefined" ? process.env : undefined);
  const maxTotal =
    limits && Number.isInteger(limits.maxTotal)
      ? limits.maxTotal
      : resolveMaxTotal(typeof process !== "undefined" ? process.env : undefined);
  if (s.totalNudges >= maxTotal) return false;
  return s.consecutiveIdle < maxConsec;
}

// ── 영속화: sessionID 키 durable marker (ctx-guard stage/receipt 선례 준용) ────
// 파일명 안전 키 — SID 비-[A-Za-z0-9_-] 제거·96 자 컷(ctx-guard safeCompactionSnapshotSessionKey
// 동형·pm_log _safe_marker_key 파일명 규약 정합).
function safeSessionKey(sessionID) {
  if (typeof sessionID !== "string" || !sessionID.trim()) return null;
  const key = sessionID.trim().replace(/[^A-Za-z0-9_-]/g, "").slice(0, 96);
  return key || null;
}

function findEngineRoot(startDir) {
  let dir = startDir;
  for (let i = 0; i < 12 && dir; i++) {
    if (fs.existsSync(path.join(dir, ".project_manager", "tools", "pm_log.py"))) {
      return dir;
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function stallStatePath(root, sessionID) {
  const key = safeSessionKey(sessionID);
  if (!root || !key) return null;
  return path.join(path.resolve(root), MARKER_DIR_REL, `state.${key}.json`);
}

function loadStallState(root, sessionID) {
  const marker = stallStatePath(root, sessionID);
  if (!marker) return freshStallState();
  try {
    if (!fs.existsSync(marker)) return freshStallState();
    return normalizeStallState(JSON.parse(fs.readFileSync(marker, "utf-8")));
  } catch {
    return freshStallState(); // 판독 실패도 흡수 — 게이트는 fail-open(관측 채널이 세션을 막지 않음).
  }
}

// 원자 기록(temp→rename·0600) — one-shot 프로세스 간 부분 기록 노출 방지. 실패는 false 흡수.
function saveStallState(root, sessionID, state) {
  const marker = stallStatePath(root, sessionID);
  if (!marker) return false;
  const temp = path.join(
    path.dirname(marker),
    `.${path.basename(marker)}.${process.pid}.${crypto.randomUUID()}.tmp`,
  );
  try {
    fs.mkdirSync(path.dirname(marker), { recursive: true });
    fs.writeFileSync(temp, `${JSON.stringify(state)}\n`, { encoding: "utf-8", mode: 0o600 });
    fs.renameSync(temp, marker);
    return true;
  } catch {
    return false;
  } finally {
    try {
      fs.unlinkSync(temp);
    } catch {
      /* rename 성공 뒤 temp 정리 실패는 무시. */
    }
  }
}

// ── SDK guarded 호출 (ctx-guard timeout race 패턴 동형 — 실패·타임아웃은 null 흡수) ──
async function guardedSdkCall(fn, timeoutMs) {
  try {
    let timeoutID;
    const timeout = new Promise((resolve) => {
      timeoutID = setTimeout(() => resolve(null), timeoutMs);
    });
    let result;
    try {
      result = await Promise.race([Promise.resolve(fn()), timeout]);
    } finally {
      clearTimeout(timeoutID);
    }
    return result === undefined ? null : result;
  } catch {
    return null;
  }
}

// RequestResult({data}) 와 벌크 배열 양쪽을 흡수하는 배열 추출 — 불명 형태는 null(처리 skip).
function extractArray(result) {
  if (result == null) return null;
  if (Array.isArray(result)) return result;
  if (result.data && Array.isArray(result.data)) return result.data;
  return null;
}

// ── 순수 함수: messages entries 파생 입력 산출 ────────────────────────────────
// entry = {info: Message, parts: Part[]}. 마지막 assistant entry 를 골라 (entry, index) 반환.
function lastAssistantEntry(entries) {
  if (!Array.isArray(entries)) return null;
  for (let i = entries.length - 1; i >= 0; i--) {
    const info = entries[i] && entries[i].info;
    if (info && info.role === "assistant") return { entry: entries[i], index: i };
  }
  return null;
}

// 마지막 text 파트 이후 tool 파트 존재 — declare-no-action 의 "이후 tool 파트 없음" 신호.
// (text 파트가 아예 없으면 tool 파트 여부와 무관하게 false — classify 가 빈 텍스트로 정상 처리.)
function hasToolPartAfterText(parts) {
  if (!Array.isArray(parts)) return false;
  let lastTextIdx = -1;
  for (let i = 0; i < parts.length; i++) {
    if (parts[i] && parts[i].type === "text") lastTextIdx = i;
  }
  for (let i = lastTextIdx + 1; i < parts.length; i++) {
    if (parts[i] && parts[i].type === "tool") return true;
  }
  return false;
}

// text 파트 결합 — synthetic(시스템 합성)·ignored 파트는 제외(모델 발화만 판정 대상).
function extractAssistantText(parts) {
  if (!Array.isArray(parts)) return "";
  return parts
    .filter((p) => p && p.type === "text" && p.synthetic !== true && p.ignored !== true)
    .map((p) => (typeof p.text === "string" ? p.text : ""))
    .join("\n")
    .trim();
}

// ── plugin 팩토리 커링: client 주입 → opencode 팩토리 반환 ────────────────────
// ESM shim(../plugins/stall-watchdog.js)이 opencode 가 건네준 ctx.client 로 이 함수를 호출해
// 만든 팩토리를 재노출한다. 반환값이 opencode autoload 가 호출하는 실제 팩토리.
function makeStallWatchdogPlugin(client) {
  return async ({ directory, worktree } = {}) => {
    const root = findEngineRoot(directory || worktree || process.cwd());

    // 넛지 주입 — fire-and-forget(prompt 는 모델 턴을 트리거해 완응까지 수 분 가능). 이벤트 핸들러를
    // 붙잡지 않게 즉석 호출+흡수. 게이트 소모(delivery 확정 전 기록)는 반복 소음 방지가 우선이다.
    function firePrompt(sessionID, text) {
      try {
        if (!client || !client.session || typeof client.session.prompt !== "function") return;
        Promise.resolve(
          client.session.prompt({ path: { id: sessionID }, body: { parts: [{ type: "text", text }] } }),
        ).catch(() => {}); // 주입 실패는 흡수 — never-block(파일 IO 실패 흡수 계약과 동일).
      } catch {
        /* 동기 throw 도 흡수. */
      }
    }

    return {
      // ── session.idle 구독: 턴 종료 → 분류 → 게이트 → 처방 주입 ──────────────
      event: async ({ event }) => {
        try {
          if (!event || event.type !== "session.idle") return;
          if (isDisabled(process.env)) return;
          const sessionID = event.properties && event.properties.sessionID;
          if (typeof sessionID !== "string" || !sessionID.trim()) return;

          // SDK 조회 2건 — 실패·타임아웃 시 보수적 skip(게이트 예산 소모 없음).
          const messageList = extractArray(
            await guardedSdkCall(
              () =>
                client &&
                client.session &&
                typeof client.session.messages === "function"
                  ? client.session.messages({ path: { id: sessionID } })
                  : null,
              SDK_LOOKUP_TIMEOUT_MS,
            ),
          );
          if (!messageList) return;
          const todos = extractArray(
            await guardedSdkCall(
              () =>
                client && client.session && typeof client.session.todo === "function"
                  ? client.session.todo({ path: { id: sessionID } })
                  : null,
              SDK_LOOKUP_TIMEOUT_MS,
            ),
          );
          if (!todos) return;

          const found = lastAssistantEntry(messageList);
          if (!found) return;

          // abort 직후 종료 — 넛지 금지(사용자 중단 의사). 게이트 예산도 소모하지 않는다.
          if (isAbortOutcome(found.entry.info)) return;

          let state = loadStallState(root, sessionID);

          // 사용자 메시지 도착(마지막 assistant 이후 user) — 워치독 영구 해제 후 저장.
          const userAfter = messageList
            .slice(found.index + 1)
            .some((e) => e && e.info && e.info.role === "user");
          const completedNow = completedTodoCount(todos);
          if (userAfter) {
            saveStallState(root, sessionID, releaseWatchdog(state));
            return;
          }

          // 진행 감지(v1 todo 기반) — 완료 수 변동 시 연속 카운터 리셋.
          if (completedNow !== state.lastCompletedCount) {
            state = recordProgress(state, completedNow);
          }

          const parts = found.entry.parts;
          const verdict = classifyStall(
            extractAssistantText(parts),
            todos,
            hasToolPartAfterText(parts),
          );

          if (!verdict.stall) {
            saveStallState(root, sessionID, state); // 기준선 갱신만 영속.
            return;
          }

          if (!shouldNudge(state)) {
            saveStallState(root, sessionID, state);
            return;
          }

          const text = buildNudge(verdict.kind);
          if (!text) {
            saveStallState(root, sessionID, state);
            return;
          }
          firePrompt(sessionID, text);
          // delivery 확정 전 기록 — 주입 유실 시 재시도는 다음 idle 이 담당, 소음 상한은 게이트가 닫는다.
          saveStallState(root, sessionID, recordNudgeFired(state));
        } catch {
          /* 관측·판정·영속화 그 무엇도 세션 이벤트 처리를 막지 않는다(never-block). */
        }
      },
    };
  };
}

// CommonJS export — node 자가검증(require)·ESM shim(../plugins/stall-watchdog.js) 양쪽 소비.
// opencode 는 이 모듈을 직접 로드하지 않는다(plugins/ 만 스캔) — 얇은 ESM shim 이 client 를 주입해
// 만든 팩토리 하나만 named-export 해 로드 규약(export=단일 함수·실측)을 만족한다.
module.exports = {
  makeStallWatchdogPlugin,
  // 순수 결정 로직 (테스트·자가검증용 export).
  classifyStall,
  buildNudge,
  shouldNudge,
  isAbortOutcome,
  declaresWriteIntent,
  countFences,
  looksTruncated,
  endsConclusively,
  countUnfinishedTodos,
  completedTodoCount,
  freshStallState,
  normalizeStallState,
  recordProgress,
  releaseWatchdog,
  recordNudgeFired,
  safeSessionKey,
  findEngineRoot,
  stallStatePath,
  loadStallState,
  saveStallState,
  guardedSdkCall,
  extractArray,
  lastAssistantEntry,
  hasToolPartAfterText,
  extractAssistantText,
  resolvePositiveInt,
  resolveMaxConsec,
  resolveMaxTotal,
  isDisabled,
  MAX_CONSEC_DEFAULT,
  MAX_TOTAL_DEFAULT,
  MAX_CONSEC_ENV,
  MAX_TOTAL_ENV,
  DISABLED_ENV,
  SDK_LOOKUP_TIMEOUT_MS,
};
