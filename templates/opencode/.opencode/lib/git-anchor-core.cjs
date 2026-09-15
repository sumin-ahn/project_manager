// opencode raw git/engine cwd-anchor 가드 core.
// 판정은 Python board(`judge_git_anchor_command`·`judge_tool_input`·`record_git_grants`) 단일 진실을
// subprocess로 호출하고, 이 모듈은 선필터 + opencode hook 배선만 소유한다. plugins/ 진입점은
// 팩토리 하나만 export한다.
//
// 사용자 1회 허용: `chat.message`(사용자 메시지 생성 시점 훅·opencode 1.18.30 바이너리의
// SessionPrompt.createUserMessage 에서 실측)가 사용자가 직접 친 텍스트 조각(synthetic 아님)의
// `git 허용: <명령 원문>` 줄을 허용 기록으로 남긴다. 도구 호출은 모든 도구가 `tool.execute.before`
// 를 지나므로, bash 는 명령 판정(1회 허용 소비 포함), 그 밖의 도구는 입력의 허용 위조를 본다.
const path = require("node:path");
const fs = require("node:fs");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");

// 엔진 루트는 이 파일 자기 위치에서 나온다 — 훅은 언제나 `<root>/.opencode/lib/*.cjs` 에 설치되고
// (pm_import 가 그 깊이를 못박는다) 이는 파이썬 도구의 `Path(__file__).resolve().parents[2]` 와 같은
// 규칙이다. 조상을 훑으면 중첩 트리에서 바깥 프로젝트의 board.py 를 실행한다.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");
const BOARD_PY = path.join(ENGINE_ROOT, ".project_manager", "tools", "board.py");

// 허용 표식과 허용 기록 폴더 이름 — board.py `GIT_GRANT_LINE`·`GIT_GRANT_DIR_NAME` 과 같은 값.
const GRANT_MARKER = /git[ \t]+허용[ \t]*:/;
const GRANT_DIR_NAME = "git-grants";

// bash 명령 선필터(성능 전용 — 판정은 board 가 한다). claude `pm_orch_claude.py`·codex
// `pm_orch_codex.py` 의 GIT_ANCHOR_SHELL_PREFILTERS 와 같은 집합이다(테스트가 대조).
const GIT_PREFILTER = [
  /(^|[^A-Za-z0-9_.-])git(?=\s|$|[<>,])/,
  /(^|[^A-Za-z0-9_.-])pytest(?=\s|$)/,
  /(^|[;&|])\s*cd(?=\s)/,
  /(^|[^A-Za-z0-9_.-])(?:rm|rmdir|unlink|shred|mv|find|rsync)(?=\s|$)/,
  /(^|[^A-Za-z0-9_])(?:rmtree|remove|removedirs|unlink|rmdir|rmSync|rmdirSync|unlinkSync|rm_rf|rm_r|rename|replace|move|renameSync)(?![A-Za-z0-9_])/,
  GRANT_MARKER,
];
const BOARD_TIMEOUT_MS = 10000;

function containsGitCommand(command) {
  if (typeof command !== "string") return false;
  const prefilter = command.replace(/\\\r?\n/g, "").replace(/["']/g, "");
  const normalized = prefilter.replace(/\\/g, "/").replace(/\/+/g, "/");
  return (
    GIT_PREFILTER.some((pattern) => pattern.test(prefilter))
    || normalized.includes(".project_manager/tools/")
    || normalized.includes(GRANT_DIR_NAME)
  );
}

// 셸이 아닌 도구 입력 선필터 — 허용 기록 폴더 이름이나 허용 표식이 어느 값에든 있을 때만 판정한다.
function toolInputNeedsJudgment(args) {
  const text = JSON.stringify(args === undefined ? {} : args);
  return typeof text === "string" && (text.includes(GRANT_DIR_NAME) || GRANT_MARKER.test(text));
}

// board.py 하위 명령 실행 — { ok, stdout } 또는 { ok:false, error }.
function runBoard(root, argv, input, spawnSync) {
  const board = path.join(root, ".project_manager", "tools", "board.py");
  let lastError = "Python interpreter 없음";
  for (const py of ["python3", "python"]) {
    const result = spawnSync(py, [board, ...argv], {
      encoding: "utf8", timeout: BOARD_TIMEOUT_MS, input,
    });
    if (result && result.error && result.error.code === "ENOENT") continue;
    if (!result || result.error) {
      lastError = result && result.error ? String(result.error.message || result.error) : "판정 subprocess 실패";
      break;
    }
    if (result.status !== 0) {
      lastError = String(result.stderr || `board.py rc=${result.status}`).trim();
      break;
    }
    return { ok: true, stdout: String(result.stdout || "") };
  }
  return { ok: false, error: lastError };
}

function lastJsonLine(stdout) {
  const lines = String(stdout || "").trim().split("\n");
  return JSON.parse(lines[lines.length - 1]);
}

function judgmentFromBoard(run) {
  if (!run.ok) {
    return { verdict: "warn", cwd_identity: "non-repo", reason: `${run.error} — cwd를 직접 확인` };
  }
  let lastError;
  try {
    const parsed = lastJsonLine(run.stdout);
    if (["ok", "warn", "deny"].includes(parsed.verdict)) return parsed;
    lastError = "판정 JSON verdict 불명";
  } catch (error) {
    lastError = `판정 JSON 파싱 실패(${error.message})`;
  }
  return { verdict: "warn", cwd_identity: "non-repo", reason: `${lastError} — cwd를 직접 확인` };
}

// grantSession 을 주면 board 가 이 엔진 루트의 1회 허용을 쓸 수 있는 하네스로 판정한다.
function judgeCommand(root, cwd, command, { grantSession, spawnSync = childProcess.spawnSync } = {}) {
  if (!containsGitCommand(command)) {
    return { verdict: "ok", cwd_identity: "non-repo", reason: "git mutation 없음" };
  }
  const argv = ["git-anchor", "--cwd", String(cwd || root), "--command", command];
  if (grantSession) argv.push("--grant-session", String(grantSession));
  return judgmentFromBoard(runBoard(root, argv, undefined, spawnSync));
}

function judgeToolInput(root, args, { spawnSync = childProcess.spawnSync } = {}) {
  if (!toolInputNeedsJudgment(args)) {
    return { verdict: "ok", cwd_identity: "non-repo", reason: "허용 위조 표식 없음" };
  }
  return judgmentFromBoard(
    runBoard(root, ["git-tool-input"], JSON.stringify(args === undefined ? {} : args), spawnSync)
  );
}

// 사용자 메시지의 허용 줄을 기록한다. 기록 실패(형식 오류·인프라)는 조용히 넘기지 않고 올린다 —
// 허용 없이 대화가 진행되면 같은 거부가 반복될 뿐 사용자는 이유를 모른다.
function recordGrants(root, sessionID, cwd, prompt, { spawnSync = childProcess.spawnSync } = {}) {
  const run = runBoard(
    root, ["git-grant", "--session", String(sessionID || ""), "--cwd", String(cwd || root)],
    prompt, spawnSync,
  );
  if (!run.ok) throw new Error(`[git-anchor/허용 거부] ${run.error}`);
  const parsed = lastJsonLine(run.stdout);
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

function makeGitAnchorPlugin(judge = judgeCommand, judgeInput = judgeToolInput, recordGrant = recordGrants) {
  return async ({ client, directory, worktree }) => {
  const root = ENGINE_ROOT;
  const warnings = createWarningChannel(client);

  async function guardedJudgment(run, cwd) {
    try {
      const judgment = await run();
      if (!judgment || !["ok", "warn", "deny"].includes(judgment.verdict)) {
        throw new Error("판정 결과 verdict 불명");
      }
      return judgment;
    } catch (error) {
      // 판정 인프라 실패 자체가 사용자 명령을 차단하면 never-block 계약을 뒤집는다.
      const detail = error && error.message ? error.message : String(error);
      return {
        verdict: "warn",
        cwd_identity: "non-repo",
        reason: `판정 인프라 예외(${detail}) — cwd=${cwd}를 직접 확인`,
      };
    }
  }

  return {
    "tool.execute.before": async (input, output) => {
      const tool = String((input && input.tool) || "").toLowerCase();
      const args = (output && output.args) || {};
      const sessionID = input && input.sessionID;
      const shell = tool === "bash";
      if (shell ? !containsGitCommand(args.command) : !toolInputNeedsJudgment(args)) return;
      // 설치 깨짐은 일시 실패가 아니다 — 판정 엔진이 자기 위치 아래 없으면 경고로 낮추지 않고 막는다.
      if (!fs.existsSync(BOARD_PY)) throw new Error(`[git-anchor/설치] 판정 엔진 부재: ${BOARD_PY}`);
      const cwd = args.workdir || args.cwd || directory || worktree || process.cwd();
      const judgment = await guardedJudgment(
        () => (shell
          ? judge(root, cwd, args.command, { grantSession: sessionID })
          : judgeInput(root, args)),
        cwd,
      );
      const text = `[git-anchor/${judgment.verdict}] ${judgment.reason}`;
      if (judgment.verdict === "deny") throw new Error(text);
      if (judgment.verdict === "warn") {
        await warnings.publish(sessionID || "__global__", text);
      }
    },
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
  containsGitCommand,
  toolInputNeedsJudgment,
  judgeCommand,
  judgeToolInput,
  recordGrants,
  userPromptText,
};
