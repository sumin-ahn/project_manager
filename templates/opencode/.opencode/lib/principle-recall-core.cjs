// opencode 판단 원칙 recall core.
//
// 판정은 Python `pm_principles.judge_recall` 단일 진실을 subprocess 로 호출하고, 이 모듈은
// 선필터(도구 이름 → `on` 매핑) + opencode hook 배선 + 비차단 주입만 소유한다
// (git-anchor-core.cjs 동형 — `judgeCommand`/`makeGitAnchorPlugin` 과 같은 구조).
//
// `on` 4어휘 중 shell/edit/delegate 는 `tool.execute.before`(input.tool·output.args — safe-write
// -core.cjs 실측 필드) 에서 판별한다. prompt 축은 이미 이 저장소에서 검증된 `event` 훅
// (ctx-guard-core.cjs 의 `message.updated` 배선)을 재사용한다 — 미검증 훅 키를 새로 추가하면
// 플러그인 로드 자체가 깨질 위험이 있어(opencode 는 알려진 훅 키만 기대), 이미 라이브로 확인된
// 훅만 쓴다. `info.parts` 텍스트 추출은 방어적이며, 필드 형태가 다르면 조용히 넘어간다(비차단).
// 같은 `event` 훅의 `session.compacted`(ctx-guard-core.cjs 가 압축 재무장에 이미 쓰는 사건축)에서
// (세션, 규칙) marker 를 지워 다음 사이클을 재무장한다 — 압축 직후가 규칙이 컨텍스트에서 사라지는
// 지점이므로 재무장 시점이 정확히 그 자리다(claude `ctx_stop_hook._rearm_principle_recall` 동형).
// 라이브 도달 확인은 PM 이 채택자 환경에서 별도로 수행한다.
const path = require("node:path");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");

// 엔진 루트는 이 파일 자기 위치에서 나온다 — 훅은 언제나 `<root>/.opencode/lib/*.cjs` 에 설치되고
// (pm_import 가 그 깊이를 못박는다) 이는 파이썬 도구의 `Path(__file__).resolve().parents[2]` 와 같은
// 규칙이다. 조상을 훑으면 중첩 트리에서 바깥 프로젝트의 pm_principles.py 를 실행한다.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");

// tool.execute.before 도구 이름(소문자·safe-write-core.cjs 실측) → recall `on` 축 + 대조 텍스트.
// 매핑은 어댑터 소유(§7.3) — claude Bash/Edit·Agent, codex shell/apply_patch/
// collaborationspawn_agent 와 같은 개념을 opencode 소문자 이름으로 줄인다.
function toolSignal(toolName, args) {
  const t = String(toolName || "").toLowerCase();
  args = args || {};
  if (t === "bash") {
    return typeof args.command === "string" ? { on: "shell", text: args.command } : null;
  }
  if (t === "write" || t === "edit") {
    const fp = args.filePath || args.file_path || args.path;
    return typeof fp === "string" ? { on: "edit", text: fp } : null;
  }
  if (t === "task") {
    // delegate-channel-core.cjs DEFAULT_SURFACE 실측 — args.subagent_type 가 role 리터럴.
    const role = args.subagent_type || args.description || "";
    return { on: "delegate", text: String(role) };
  }
  return null;
}

// `event` 훅의 `message.updated`(ctx-guard-core.cjs 가 이미 이 사건축을 쓴다) 에서 사용자
// 프롬프트 텍스트를 방어적으로 뽑는다. 여러 후보 필드를 시도하고 못 찾으면 빈 문자열(비차단).
function extractPromptText(info) {
  if (!info || typeof info !== "object") return "";
  if (typeof info.text === "string") return info.text;
  if (Array.isArray(info.parts)) {
    const texts = info.parts
      .map((part) => (part && typeof part.text === "string" ? part.text : null))
      .filter((text) => typeof text === "string");
    if (texts.length) return texts.join("\n");
  }
  if (typeof info.content === "string") return info.content;
  return "";
}

function judgeRecall(root, on, text, sessionID, spawnSync = childProcess.spawnSync) {
  const engine = path.join(root, ".project_manager", "tools", "pm_principles.py");
  let lastError = "Python interpreter 없음";
  for (const py of ["python3", "python"]) {
    let result;
    try {
      result = spawnSync(
        py,
        [engine, "judge-recall", "--root", root, "--on", on, "--text", String(text || ""),
         "--session", String(sessionID || "unknown")],
        { encoding: "utf8", timeout: 10000 },
      );
    } catch (error) {
      lastError = error && error.message ? error.message : String(error);
      continue;
    }
    if (result && result.error && result.error.code === "ENOENT") continue;
    if (!result || result.error) {
      lastError = result && result.error ? String(result.error.message || result.error) : "판정 subprocess 실패";
      break;
    }
    if (result.status !== 0) {
      lastError = String(result.stderr || `pm_principles.py rc=${result.status}`).trim();
      break;
    }
    try {
      const lines = String(result.stdout || "").trim().split("\n");
      const parsed = JSON.parse(lines[lines.length - 1]);
      if (parsed && typeof parsed === "object") return parsed;
      lastError = "판정 JSON 형태 불명";
    } catch (error) {
      lastError = `판정 JSON 파싱 실패(${error.message})`;
    }
    break;
  }
  // 판정 인프라 실패는 비차단 침묵 — recall 은 통과/거부가 없는 편의 채널이라 git-anchor 의
  // warn-on-uncertain 계약을 따르지 않는다. 실패 사유는 진단용으로만 싣는다.
  return { count: 0, keys: [], text: "", infra_error: lastError };
}

// `pm_principles.py rearm` subprocess 호출 — 세션 marker 를 지워 다음 사이클을 재무장한다
// (부작용만, 반환값 없음). 실패해도 침묵한다 — 최악의 결과는 다음 압축까지 재주입이 늦는
// 것뿐이라 도구 실행을 막을 이유가 없다.
function rearmRecall(root, sessionID, spawnSync = childProcess.spawnSync) {
  const engine = path.join(root, ".project_manager", "tools", "pm_principles.py");
  for (const py of ["python3", "python"]) {
    let result;
    try {
      result = spawnSync(
        py,
        [engine, "rearm", "--root", root, "--session", String(sessionID || "unknown")],
        { encoding: "utf8", timeout: 10000 },
      );
    } catch (error) {
      continue;
    }
    if (result && result.error && result.error.code === "ENOENT") continue;
    return; // 성공/실패 무관 — 한 인터프리터가 응답했으면 더 시도하지 않는다(비차단).
  }
}

function makePrincipleRecallPlugin(judge = judgeRecall, rearm = rearmRecall) {
  return async ({ client }) => {
    const root = ENGINE_ROOT;
    const warnings = createWarningChannel(client);

    async function inject(sessionID, text) {
      if (!text) return;
      await warnings.publish(sessionID || "__global__", text);
    }

    return {
      "tool.execute.before": async (input, output) => {
        const signal = toolSignal(input && input.tool, output && output.args);
        if (!signal) return;
        let result;
        try {
          result = judge(root, signal.on, signal.text, input && input.sessionID);
        } catch (error) {
          return; // 판정 예외는 비차단 침묵 — 도구 실행을 막지 않는다.
        }
        if (result && result.text) await inject(input && input.sessionID, result.text);
      },
      event: async ({ event }) => {
        if (!event) return;
        if (event.type === "session.compacted") {
          const sessionID = event.properties && event.properties.sessionID;
          try {
            rearm(root, sessionID);
          } catch (error) {
            // 재무장 실패는 비차단 — 다음 압축 경계로 넘긴다.
          }
          return;
        }
        if (event.type !== "message.updated") return;
        const info = event.properties && event.properties.info;
        if (!info || info.role !== "user") return;
        const promptText = extractPromptText(info);
        if (!promptText) return;
        let result;
        try {
          result = judge(root, "prompt", promptText, info.sessionID);
        } catch (error) {
          return;
        }
        if (result && result.text) await inject(info.sessionID, result.text);
      },
      "experimental.chat.system.transform": async (input, output) => {
        warnings.inject(input, output);
      },
    };
  };
}

const PrincipleRecallPlugin = makePrincipleRecallPlugin();

module.exports = {
  PrincipleRecallPlugin,
  makePrincipleRecallPlugin,
  toolSignal,
  extractPromptText,
  judgeRecall,
  rearmRecall,
};
