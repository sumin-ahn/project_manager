// opencode 일반 도구 호출 판정 경계와 사용자 1회 허용 core.
// 판정(git·엔진 호출·파일 삭제·도구 입력·ctx·원칙 recall)은 Python 엔진 `board.py hook-tool` 한 번이
// 한다. 이 모듈은 판정 요청 조립·process 실행·응답 형식 검사·경고 합본과 opencode hook 배선만 소유하고,
// 명령 문자열을 미리 거르거나 정책을 해석하지 않는다. plugins/ 진입점은 팩토리 하나만 export한다.
//
// 일반 도구 호출: 모든 도구가 `tool.execute.before` 를 지난다. 그 before 의 등록 소유자는 ctx-guard-core
// (`createToolBoundary` 소비) 하나이며, 세션의 마지막 토큰 관측을 같은 요청에 실어 Python 을 한 번만
// 띄운다. git 권한·슬롯 판정에는 SDK 부모를 따라 올라간 최상위 세션 ID 를, ctx·recall 에는 호출한
// 세션 자신의 ID 를 쓴다.
//
// 사용자 1회 허용: `chat.message`(사용자 메시지 생성 시점 훅·opencode 1.18.30 바이너리의
// SessionPrompt.createUserMessage 에서 실측)가 사용자가 직접 친 텍스트 조각(synthetic 아님)의
// `git 허용: <명령 원문>` 줄을 허용 기록으로 남긴다.
const path = require("node:path");
const fs = require("node:fs");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");
const { selectPython } = require("./delegate-channel-core.cjs");
const { toolSignal } = require("./principle-recall-core.cjs");

// 엔진 루트는 이 파일 자기 위치에서 나온다 — 훅은 언제나 `<root>/.opencode/lib/*.cjs` 에 설치되고
// (pm_import 가 그 깊이를 못박는다) 이는 파이썬 도구의 `Path(__file__).resolve().parents[2]` 와 같은
// 규칙이다. 조상을 훑으면 중첩 트리에서 바깥 프로젝트의 board.py 를 실행한다.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");
const BOARD_PY = path.join(ENGINE_ROOT, ".project_manager", "tools", "board.py");

// 사용자 프롬프트의 허용 표식 — 허용 기록 진입 여부만 고른다(판정 우회 아님).
// pm_engine/domain/git_policy.py `GIT_GRANT_LINE` 과 같은 값.
const GRANT_MARKER = /git[ \t]+허용[ \t]*:/;
const BOARD_TIMEOUT_MS = 10000;
// 판정 응답 stdout 상한(바이트) — 한 줄 JSON 응답이 넘을 수 없는 크기.
const BOARD_MAX_BUFFER = 1024 * 1024;
// SDK 세션 조회 1회 제한 시간.
const SESSION_LOOKUP_TIMEOUT_MS = 1000;
// 부모 역조회 방문 상한 — 잘못된 SDK 고리·무한 역조회를 끝내는 자원 경계(정상 깊이 정책 아님).
const ROOT_SESSION_VISIT_LIMIT = 32;
// `hook-tool`·`hook-context` 기계 JSON 세대와 키 집합.
const WIRE_VERSION = 1;
const TOOL_RESPONSE_KEYS = ["version", "git", "context", "context_error", "recall", "recall_error"];
const CONTEXT_RESPONSE_KEYS = [
  "version", "status", "measured", "used_pct", "level", "notice", "guidance", "emit",
  "compaction_new", "compaction_guidance", "compaction_boundary_id",
];
const VERDICTS = ["ok", "warn", "deny"];

function errorText(error) {
  return error && error.message ? error.message : String(error);
}

// board.py 하위 명령 1회 실행 — 실행기는 프로세스 없이 고르고, 실행 실패·rc≠0 은 예외로 올린다.
function runBoard(root, argv, input, { spawnSync = childProcess.spawnSync, platform = process.platform, env = process.env } = {}) {
  const python = selectPython(root, { platform, env });
  const board = path.join(root, ".project_manager", "tools", "board.py");
  const result = spawnSync(python.command, [...python.prefixArgs, board, ...argv], {
    encoding: "utf8", timeout: BOARD_TIMEOUT_MS, maxBuffer: BOARD_MAX_BUFFER, input,
  });
  if (!result) throw new Error("board.py 실행 결과 없음");
  if (result.error) throw new Error(`board.py 실행 불가(${errorText(result.error)})`);
  if (result.status !== 0) throw new Error(String(result.stderr || `board.py rc=${result.status}`).trim());
  return String(result.stdout || "");
}

// stdout 전체가 LF 로 끝나는 JSON 한 줄이어야 한다 — 진단이 섞인 마지막 줄만 고르지 않는다.
function machineObject(stdout, label) {
  if (!stdout.endsWith("\n") || stdout.indexOf("\n") !== stdout.length - 1) {
    throw new Error(`${label} 출력이 JSON 한 줄이 아님`);
  }
  return JSON.parse(stdout);
}

function exact(value, keys, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 이 JSON 객체가 아님`);
  }
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.join(",") !== expected.join(",")) {
    throw new Error(`${label} 키 집합이 다름: ${actual.join(",")}`);
  }
  return value;
}

function nullableString(value, label) {
  if (value !== null && typeof value !== "string") throw new Error(`${label} 이 문자열/null 이 아님`);
}

// ctx 판정 응답 11키 형식 검사.
function contextResult(value) {
  exact(value, CONTEXT_RESPONSE_KEYS, "ctx 응답");
  if (value.version !== WIRE_VERSION) throw new Error(`ctx 응답 version 불명: ${value.version}`);
  if (typeof value.emit !== "boolean" || typeof value.compaction_new !== "boolean") {
    throw new Error("ctx 응답 emit/compaction_new 가 bool 이 아님");
  }
  for (const key of ["status", "notice", "guidance", "compaction_guidance", "compaction_boundary_id"]) {
    nullableString(value[key], `ctx 응답 ${key}`);
  }
  if (value.emit && typeof value.guidance !== "string") throw new Error("ctx 응답 emit 인데 guidance 없음");
  return value;
}

// 일반 도구 호출 판정 — `board.py hook-tool --session ROOT_SID` 1회와 6키 응답 형식 검사.
function runHookTool(root, rootSID, value, options = {}) {
  const stdout = runBoard(root, ["hook-tool", "--session", String(rootSID)], JSON.stringify(value), options);
  const response = exact(machineObject(stdout, "hook-tool"), TOOL_RESPONSE_KEYS, "hook-tool 응답");
  if (response.version !== WIRE_VERSION) throw new Error(`hook-tool 응답 version 불명: ${response.version}`);
  const git = response.git;
  if (!git || !VERDICTS.includes(git.verdict) || typeof git.reason !== "string") {
    throw new Error("hook-tool 응답 git 판정 형식 불명");
  }
  nullableString(response.context_error, "context_error");
  nullableString(response.recall_error, "recall_error");
  if (response.context !== null) contextResult(response.context);
  if (response.recall !== null && (typeof response.recall !== "object" || Array.isArray(response.recall))) {
    throw new Error("hook-tool 응답 recall 형식 불명");
  }
  if (git.verdict === "deny") {
    if (["context", "context_error", "recall", "recall_error"].some((key) => response[key] !== null)) {
      throw new Error("hook-tool 거부 응답에 부가 결과가 있음");
    }
  } else if (value.context !== null && (response.context === null) === (response.context_error === null)) {
    throw new Error("hook-tool 응답 context/context_error 중 정확히 하나가 있어야 함");
  }
  return response;
}

// 별도 native 이벤트(assistant message.updated·session.compacted)의 ctx 판정 — `hook-context` 1회.
function runHookContext(root, request, options = {}) {
  const { session, ...observation } = request;
  const stdout = runBoard(
    root, ["hook-context", "--harness", "opencode", "--session", String(session)],
    JSON.stringify({ version: WIRE_VERSION, ...observation }), options,
  );
  return contextResult(machineObject(stdout, "hook-context"));
}

// 사용자 메시지의 허용 줄을 기록한다. 기록 실패(형식 오류·인프라)는 조용히 넘기지 않고 올린다 —
// 허용 없이 대화가 진행되면 같은 거부가 반복될 뿐 사용자는 이유를 모른다.
function recordGrants(root, sessionID, cwd, prompt, options = {}) {
  let parsed;
  try {
    const stdout = runBoard(
      root, ["git-grant", "--session", String(sessionID || ""), "--cwd", String(cwd || root)], prompt, options,
    );
    parsed = machineObject(stdout, "git-grant");
  } catch (error) {
    throw new Error(`[git-anchor/허용 거부] ${errorText(error)}`);
  }
  if (!parsed || !Array.isArray(parsed.granted)) {
    throw new Error("[git-anchor/허용 거부] 허용 기록 결과 JSON 형식 불명");
  }
  return parsed.granted;
}

// 사용자가 직접 친 텍스트 조각만 모은다 — synthetic 조각(첨부 파일 내용·도구 안내)은 사용자 입력이 아니다.
function userPromptText(parts) {
  if (!Array.isArray(parts)) return "";
  return parts
    .filter((part) => part && part.type === "text" && !part.synthetic && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
}

// 일반 도구 호출 판정 경계. ctx-guard-core 가 세션 관측(`contextFor`)과 ctx 응답 전달(`acceptContext`)을
// 넘겨 만든다. 반환은 before(도구 호출 판정)·rootSessionID(SDK 최상위 세션 역조회)·publish/inject
// (세션별 경고 1칸 채널)다.
function createToolBoundary({
  root = ENGINE_ROOT, client, directory, worktree, contextFor, acceptContext,
  spawnSync = childProcess.spawnSync, platform = process.platform,
}) {
  const warnings = createWarningChannel(client);
  const board = path.join(root, ".project_manager", "tools", "board.py");

  // native task 는 자식 세션을 새 ID 로 만든다(실측 opencode 1.18.31 `session.create({parentID})`).
  // SDK 표면: client.session.get({ path: { id } }) -> { data: Session }. 조회 실패는 삼키지 않고 올린다.
  async function sessionRecord(id) {
    if (!client || !client.session || typeof client.session.get !== "function") {
      throw new Error("SDK client.session.get 없음");
    }
    let timeoutID;
    const timeout = new Promise((_resolve, reject) => {
      timeoutID = setTimeout(() => reject(new Error(`세션 ${id} 조회 시간 초과`)), SESSION_LOOKUP_TIMEOUT_MS);
    });
    try {
      const result = await Promise.race([client.session.get({ path: { id } }), timeout]);
      if (!result || !result.data) throw new Error(`세션 ${id} 조회 결과 없음`);
      return result.data;
    } finally {
      clearTimeout(timeoutID);
    }
  }

  // 세션별 역조회 Promise 는 같은 세션의 다음 이벤트가 재사용한다(판정 결과가 아닌 SDK 조회 대장).
  // 실패한 Promise 는 지워 다음 이벤트가 실제 조회를 다시 한다.
  const rootSessions = new Map();
  function rootSessionID(sessionID) {
    if (!sessionID) return Promise.resolve("");
    if (!rootSessions.has(sessionID)) {
      const lookup = (async () => {
        const visited = new Set();
        let current = sessionID;
        for (;;) {
          if (visited.has(current)) throw new Error(`세션 부모 고리(${current})`);
          if (visited.size >= ROOT_SESSION_VISIT_LIMIT) {
            throw new Error(`세션 부모 역조회 ${ROOT_SESSION_VISIT_LIMIT}개 초과`);
          }
          visited.add(current);
          const record = await sessionRecord(current);
          const parent = typeof record.parentID === "string" ? record.parentID.trim() : "";
          if (!parent) return current;
          current = parent;
        }
      })();
      lookup.catch(() => rootSessions.delete(sessionID));
      rootSessions.set(sessionID, lookup);
    }
    return rootSessions.get(sessionID);
  }

  async function before(input, output) {
    const tool = String((input && input.tool) || "").toLowerCase();
    const args = (output && output.args) || {};
    const sessionID = (input && input.sessionID) || "";
    const channel = sessionID || "__global__";
    const cwd = String(args.workdir || args.cwd || directory || worktree || process.cwd());
    // 설치 깨짐은 일시 실패가 아니다 — 판정 엔진이 자기 위치 아래 없으면 경고로 낮추지 않고 막는다.
    if (!fs.existsSync(board)) throw new Error(`[git-anchor/설치] 판정 엔진 부재: ${board}`);
    let rootSID;
    try {
      rootSID = await rootSessionID(sessionID);
    } catch (error) {
      await warnings.publish(
        channel, `[git-anchor/warn] 판정 인프라 예외(세션 확인 실패: ${errorText(error)}) — cwd=${cwd}를 직접 확인`,
      );
      return;
    }
    const mainThread = rootSID === sessionID;
    const observed = sessionID && mainThread ? contextFor(sessionID) : null;
    const signal = sessionID ? toolSignal(tool, args) : null;
    const shell = tool === "bash" && typeof args.command === "string";
    const request = {
      version: WIRE_VERSION,
      cwd,
      tool_name: tool,
      tool_input: args,
      shell_command: shell ? args.command : null,
      context: observed
        ? { session: sessionID, tokens: observed.tokens, model: observed.model, main_thread: true, boundary: null }
        : null,
      recall: signal ? { session: sessionID, on: signal.on, text: signal.text, main_thread: mainThread } : null,
    };
    let response;
    try {
      response = runHookTool(root, rootSID, request, { spawnSync, platform });
    } catch (error) {
      // 판정 인프라 실패 자체가 사용자 명령을 차단하면 never-block 계약을 뒤집는다.
      await warnings.publish(channel, `[git-anchor/warn] 판정 인프라 예외(${errorText(error)}) — cwd=${cwd}를 직접 확인`);
      return;
    }
    if (response.git.verdict === "deny") throw new Error(`[git-anchor/deny] ${response.git.reason}`);
    if (response.context) await acceptContext(sessionID, response.context, observed);
    // 세션 경고 채널은 1칸이다 — 앞 경고를 뒤 recall 문구가 덮지 않게 한 문자열로 합친다.
    const notes = [];
    if (response.git.verdict === "warn") notes.push(`[git-anchor/warn] ${response.git.reason}`);
    if (response.context_error) notes.push(`[ctx-guard] 판정 실패(${response.context_error})`);
    if (response.recall_error) notes.push(`[principle-recall] 판정 실패(${response.recall_error})`);
    if (response.recall && response.recall.text) notes.push(response.recall.text);
    if (notes.length) await warnings.publish(channel, notes.join("\n"));
  }

  return { before, rootSessionID, publish: warnings.publish, inject: warnings.inject };
}

function makeGitAnchorPlugin(recordGrant = recordGrants) {
  return async ({ client, directory, worktree }) => {
    const root = ENGINE_ROOT;
    const warnings = createWarningChannel(client);

    return {
      "chat.message": async (input, output) => {
        const prompt = userPromptText(output && output.parts);
        if (!GRANT_MARKER.test(prompt)) return;
        if (!fs.existsSync(BOARD_PY)) throw new Error(`[git-anchor/설치] 판정 엔진 부재: ${BOARD_PY}`);
        const sessionID = input && input.sessionID;
        const cwd = directory || worktree || process.cwd();
        const granted = await recordGrant(root, sessionID, cwd, prompt);
        if (granted.length) {
          const listed = granted.map((command) => `\`${command}\``).join(", ");
          await warnings.publish(
            sessionID,
            `[git-anchor/허용] 사용자가 폴더 ${cwd} 에서 다음 명령의 1회 실행을 허용했다: ${listed} — 그 명령을 원문 그대로 한 번만 실행한다`,
          );
        }
      },
      "experimental.chat.system.transform": async (input, output) => {
        warnings.inject(input, output);
      },
    };
  };
}

const GitAnchorPlugin = makeGitAnchorPlugin();

module.exports = {
  GitAnchorPlugin,
  makeGitAnchorPlugin,
  createToolBoundary,
  runHookTool,
  runHookContext,
  recordGrants,
  userPromptText,
  SESSION_LOOKUP_TIMEOUT_MS,
};
