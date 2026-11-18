// opencode 판단 원칙 recall core.
//
// 판정은 Python 원칙 recall 단일 진실이 하고, 이 모듈은 도구 이름 → `on` 매핑(`toolSignal`)과 사용자
// 프롬프트 recall 등록만 소유한다.
//
// `on` 4어휘 중 shell/edit/delegate 는 일반 도구 호출 판정(git-check-core.cjs `createToolBoundary`)이
// `toolSignal` 로 recall 입력을 만들어 `board.py hook-tool` 한 번에 함께 싣는다. prompt 기준은 session
// `prompt` 훅의 프롬프트 글(`prompt.text`, 실측 opencode 2.3.0)로 판정하고, 일치한 원칙 문구를 다음 모델
// 호출의 시스템 문구에 한 번 싣는다(비차단). 압축 경계의 recall 다시 켜기는 ctx 판정(`hook-context` 의 압축
// 경계 처리)이 한다.
const path = require("node:path");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");
const { supportedPythonCandidates } = require("./delegate-channel-core.cjs");

// 엔진 루트는 이 파일 자기 위치에서 나온다 — 훅은 언제나 `<root>/.opencode/lib/*.cjs` 에 설치되고
// (pm_import 가 그 깊이를 못박는다) 이는 파이썬 도구의 `Path(__file__).resolve().parents[2]` 와 같은
// 규칙이다. 조상을 훑으면 중첩 트리에서 바깥 프로젝트의 pm_principles.py 를 실행한다.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");

// tool `execute.before` 도구 이름(실측 opencode 2.3.0 shell·write·edit·subagent) → recall `on` 기준 + 대조 텍스트.
// 매핑은 어댑터 소유(§7.3) — claude Bash/Edit·Agent, codex shell/apply_patch/
// collaborationspawn_agent 와 같은 개념을 opencode 이름으로 줄인다.
function toolSignal(toolName, args) {
  const t = String(toolName || "").toLowerCase();
  args = args || {};
  if (t === "shell") {
    return typeof args.command === "string" ? { on: "shell", text: args.command } : null;
  }
  if (t === "write" || t === "edit") {
    return typeof args.path === "string" ? { on: "edit", text: args.path } : null;
  }
  if (t === "subagent") {
    // delegate-channel-core.cjs DEFAULT_SURFACE 실측: input.agent 가 role 리터럴.
    const role = args.agent || args.description || "";
    return { on: "delegate", text: String(role) };
  }
  return null;
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
        { encoding: "utf8" },
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
  // 판정 인프라 실패는 비차단 침묵 — recall 은 통과/거부가 없는 편의 채널이라 git-check 의
  // warn-on-uncertain 계약을 따르지 않는다. 실패 사유는 진단용으로만 싣는다.
  return { count: 0, keys: [], text: "", infra_error: lastError };
}

// 플러그인 정의 { id, setup }: setup 이 session `prompt`(원칙 판정)·`context`(일치 문구 싣기) 훅을 등록한다.
function makePrincipleRecallPlugin(judge = judgeRecall) {
  return {
    id: "pm.principle-recall",
    async setup(ctx) {
      const root = ENGINE_ROOT;
      const warnings = createWarningChannel();
      await ctx.session.hook("prompt", (event) => {
        const promptText = String(event.prompt.text || "");
        if (!promptText) return;
        const result = judge(root, "prompt", promptText, event.sessionID);
        if (result && result.text) warnings.publish(event.sessionID, result.text);
      });
      await ctx.session.hook("context", (event) => warnings.inject(event));
    },
  };
}

const PrincipleRecallPlugin = makePrincipleRecallPlugin();

module.exports = {
  PrincipleRecallPlugin,
  makePrincipleRecallPlugin,
  toolSignal,
  judgeRecall,
};
