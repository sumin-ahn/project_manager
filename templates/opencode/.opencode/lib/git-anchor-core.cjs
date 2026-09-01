// opencode raw git/engine cwd-anchor 가드 core.
// 판정은 Python board.judge_git_anchor_command 단일 진실을 subprocess로 호출하고, 이 모듈은
// 선필터 + opencode hook 배선만 소유한다. plugins/ 진입점은 팩토리 하나만 export한다.
const path = require("node:path");
const fs = require("node:fs");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");

// 엔진 루트는 이 파일 자기 위치에서 나온다 — 훅은 언제나 `<root>/.opencode/lib/*.cjs` 에 설치되고
// (pm_import 가 그 깊이를 못박는다) 이는 파이썬 도구의 `Path(__file__).resolve().parents[2]` 와 같은
// 규칙이다. 조상을 훑으면 중첩 트리에서 바깥 프로젝트의 board.py 를 실행한다.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");
const BOARD_PY = path.join(ENGINE_ROOT, ".project_manager", "tools", "board.py");

const GIT_PREFILTER = [
  /(^|[^A-Za-z0-9_.-])git(?=\s|$|[<>])/,
  /\.project_manager\/tools\//,
  /(^|[^A-Za-z0-9_.-])pytest(?=\s|$)/,
  /(^|[;&|])\s*cd(?=\s)/,
];

function containsGitCommand(command) {
  if (typeof command !== "string") return false;
  const prefilter = command.replace(/\\\r?\n/g, "").replace(/["']/g, "");
  const normalized = prefilter.replace(/\\/g, "/").replace(/\/+/g, "/");
  return GIT_PREFILTER.some((pattern, index) => pattern.test(index === 1 ? normalized : prefilter));
}

function judgeCommand(root, cwd, command, spawnSync = childProcess.spawnSync) {
  if (!containsGitCommand(command)) {
    return { verdict: "ok", cwd_identity: "non-repo", reason: "git mutation 없음" };
  }
  const board = path.join(root, ".project_manager", "tools", "board.py");
  let lastError = "Python interpreter 없음";
  for (const py of ["python3", "python"]) {
    const result = spawnSync(
      py,
      [board, "git-anchor", "--cwd", String(cwd || root), "--command", command],
      { encoding: "utf8", timeout: 10000 }
    );
    if (result && result.error && result.error.code === "ENOENT") continue;
    if (!result || result.error) {
      lastError = result && result.error ? String(result.error.message || result.error) : "판정 subprocess 실패";
      break;
    }
    if (result.status !== 0) {
      lastError = String(result.stderr || `board.py rc=${result.status}`).trim();
      break;
    }
    try {
      const lines = String(result.stdout || "").trim().split("\n");
      const parsed = JSON.parse(lines[lines.length - 1]);
      if (["ok", "warn", "deny"].includes(parsed.verdict)) return parsed;
      lastError = "판정 JSON verdict 불명";
    } catch (error) {
      lastError = `판정 JSON 파싱 실패(${error.message})`;
    }
    break;
  }
  return { verdict: "warn", cwd_identity: "non-repo", reason: `${lastError} — cwd를 직접 확인` };
}

function makeGitAnchorPlugin(judge = judgeCommand) {
  return async ({ client, directory, worktree }) => {
  const root = ENGINE_ROOT;
  const warnings = createWarningChannel(client);

  return {
    "tool.execute.before": async (input, output) => {
      const tool = input && input.tool;
      const args = output && output.args;
      const command = args && args.command;
      if (String(tool || "").toLowerCase() !== "bash" || !containsGitCommand(command)) return;
      // 설치 깨짐은 일시 실패가 아니다 — 판정 엔진이 자기 위치 아래 없으면 경고로 낮추지 않고 막는다.
      if (!fs.existsSync(BOARD_PY)) throw new Error(`[git-anchor/설치] 판정 엔진 부재: ${BOARD_PY}`);
      const cwd = (args && (args.workdir || args.cwd)) || directory || worktree || process.cwd();
      let judgment;
      try {
        judgment = await judge(root, cwd, command);
        if (!judgment || !["ok", "warn", "deny"].includes(judgment.verdict)) {
          throw new Error("판정 결과 verdict 불명");
        }
      } catch (error) {
        // 판정 인프라 실패 자체가 사용자 명령을 차단하면 never-block 계약을 뒤집는다.
        const detail = error && error.message ? error.message : String(error);
        judgment = {
          verdict: "warn",
          cwd_identity: "non-repo",
          reason: `판정 인프라 예외(${detail}) — cwd=${cwd}를 직접 확인`,
        };
      }
      const text = `[git-anchor/${judgment.verdict}] ${judgment.reason}`;
      if (judgment.verdict === "deny") throw new Error(text);
      if (judgment.verdict === "warn") {
        const sessionID = (input && input.sessionID) || "__global__";
        await warnings.publish(sessionID, text);
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
  containsGitCommand,
  judgeCommand,
};
