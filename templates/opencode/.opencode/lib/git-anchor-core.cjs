// opencode raw git cwd-anchor 가드 core (T-0587).
// 판정은 Python board.judge_git_anchor_command 단일 진실을 subprocess로 호출하고, 이 모듈은
// 비-git 선필터 + opencode hook 배선만 소유한다. plugins/ 진입점은 팩토리 하나만 export한다.
const path = require("node:path");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");

const GIT_PREFILTER = /(^|[^A-Za-z0-9_.-])git(?=\s|$|[<>])/;

function containsGitCommand(command) {
  return typeof command === "string" && GIT_PREFILTER.test(
    command.replace(/\\\r?\n/g, "").replace(/["']/g, ""),
  );
}

function findEngineRoot(startDir, fs = require("node:fs")) {
  let dir = path.resolve(startDir || process.cwd());
  for (let i = 0; i < 12; i += 1) {
    if (fs.existsSync(path.join(dir, ".project_manager", "tools", "board.py"))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function judgeCommand(root, cwd, command, spawnSync = childProcess.spawnSync) {
  if (!containsGitCommand(command)) {
    return { verdict: "ok", cwd_identity: "non-repo", reason: "git mutation 없음" };
  }
  if (!root) {
    return { verdict: "warn", cwd_identity: "non-repo", reason: "엔진 root 미해소 — cwd를 직접 확인" };
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
  const root = findEngineRoot(directory || worktree || process.cwd());
  const warnings = createWarningChannel(client);

  return {
    "tool.execute.before": async (input, output) => {
      const tool = input && input.tool;
      const args = output && output.args;
      const command = args && args.command;
      if (String(tool || "").toLowerCase() !== "bash" || !containsGitCommand(command)) return;
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
  findEngineRoot,
  judgeCommand,
};
