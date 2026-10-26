// opencode 판단 원칙 recall core.
//
// 판정은 Python 원칙 recall 단일 진실이 하고, 이 모듈은 도구 이름 → `on` 매핑(`toolSignal`)과 사용자
// 프롬프트 recall 배선만 소유한다.
//
// `on` 4어휘 중 shell/edit/delegate 는 일반 도구 호출 판정(git-anchor-core.cjs `createToolBoundary`)이
// `toolSignal` 로 recall 입력을 만들어 `board.py hook-tool` 한 번에 함께 싣는다. prompt 축은 이미 이
// 저장소에서 검증된 `event` 훅(ctx-guard-core.cjs 의 `message.updated` 배선)을 재사용한다 — 미검증 훅
// 키를 새로 추가하면 플러그인 로드 자체가 깨질 위험이 있어(opencode 는 알려진 훅 키만 기대), 이미
// 라이브로 확인된 훅만 쓴다. `info.parts` 텍스트 추출은 방어적이며, 필드 형태가 다르면 조용히
// 넘어간다(비차단). 압축 경계의 recall 재무장은 ctx 판정(`hook-context` 의 압축 경계 처리)이 한다.
// 라이브 도달 확인은 PM 이 채택자 환경에서 별도로 수행한다.
const path = require("node:path");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");
const { supportedPythonCandidates } = require("./delegate-channel-core.cjs");

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

function judgeRecall(root, on, text, sessionID, spawnSync = childProcess.spawnSync, platform = process.platform) {
  const engine = path.join(root, ".project_manager", "tools", "pm_principles.py");
  const attempts = [];
  const candidate = supportedPythonCandidates(root, {spawnSync, platform, attempts}).next().value;
  let lastError = attempts.map(({label, failure}) => `${label}=${failure}`).join("; ")
    || "Python interpreter 없음";
  if (candidate) {
    lastError = "판정 subprocess 실패";
    let result;
    try {
      result = spawnSync(
        candidate.command,
        [...candidate.prefixArgs, engine, "judge-recall", "--root", root, "--on", on, "--text", String(text || ""),
         "--session", String(sessionID || "unknown")],
        { encoding: "utf8", timeout: 10000 },
      );
    } catch (error) {
      lastError = error && error.message ? error.message : String(error);
    }
    if (!result || result.error) {
      if (result && result.error) lastError = String(result.error.message || result.error);
    } else if (result.status !== 0) {
      lastError = String(result.stderr || `pm_principles.py rc=${result.status}`).trim();
    } else {
      try {
        const lines = String(result.stdout || "").trim().split("\n");
        const parsed = JSON.parse(lines[lines.length - 1]);
        if (parsed && typeof parsed === "object") return parsed;
        lastError = "판정 JSON 형태 불명";
      } catch (error) {
        lastError = `판정 JSON 파싱 실패(${error.message})`;
      }
    }
  }
  // 판정 인프라 실패는 비차단 침묵 — recall 은 통과/거부가 없는 편의 채널이라 git-anchor 의
  // warn-on-uncertain 계약을 따르지 않는다. 실패 사유는 진단용으로만 싣는다.
  return { count: 0, keys: [], text: "", infra_error: lastError };
}

function makePrincipleRecallPlugin(judge = judgeRecall) {
  return async ({ client }) => {
    const root = ENGINE_ROOT;
    const warnings = createWarningChannel(client);

    return {
      event: async ({ event }) => {
        if (!event || event.type !== "message.updated") return;
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
        if (result && result.text) await warnings.publish(info.sessionID || "__global__", result.text);
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
};
