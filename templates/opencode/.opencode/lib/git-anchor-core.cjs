// opencode raw git/engine cwd-anchor 가드 core.
// 판정은 Python board(`judge_git_anchor_command`·`judge_tool_input`·`record_git_grants`) 단일 진실을
// subprocess로 호출하고, 이 모듈은 선필터 + opencode hook 배선만 소유한다. plugins/ 진입점은
// 팩토리 하나만 export한다.
//
// 사용자 1회 허용: `chat.message`(사용자 메시지 생성 시점 훅·opencode 1.18.30 바이너리의
// SessionPrompt.createUserMessage 에서 실측)가 사용자가 직접 친 텍스트 조각(synthetic 아님)의
// `git 허용: <명령 원문>` 줄을 허용 기록으로 남긴다. 도구 호출은 모든 도구가 `tool.execute.before`
// 를 지나므로, bash 는 명령 판정(1회 허용 소비 포함), 파일 쓰기 도구는 슬롯 쓰기 주인, 그 밖의 도구는
// 입력의 허용 위조를 본다. 세션 ID 는 훅 입력의 `sessionID` 다.
const path = require("node:path");
const fs = require("node:fs");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");
const { supportedPythonCandidates } = require("./delegate-channel-core.cjs");

// 엔진 루트는 이 파일 자기 위치에서 나온다 — 훅은 언제나 `<root>/.opencode/lib/*.cjs` 에 설치되고
// (pm_import 가 그 깊이를 못박는다) 이는 파이썬 도구의 `Path(__file__).resolve().parents[2]` 와 같은
// 규칙이다. 조상을 훑으면 중첩 트리에서 바깥 프로젝트의 board.py 를 실행한다.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");
const BOARD_PY = path.join(ENGINE_ROOT, ".project_manager", "tools", "board.py");

// 허용 표식과 허용 기록 폴더 이름 — board.py `GIT_GRANT_LINE`·`GIT_GRANT_DIR_NAME` 과 같은 값.
const GRANT_MARKER = /git[ \t]+허용[ \t]*:/;
const GRANT_DIR_NAME = "git-grants";
// 슬롯 경로 표식 — 셸 명령이 슬롯(`<PM 홈>/work/<repo>_<N>`) 경로를 담으면 판정한다.
const SLOT_PATH_MARK = "/work/";
// 파일 쓰기 도구(실측 opencode 1.18.31: write·edit 은 filePath, apply_patch 는 patchText) — 대상은 board 가 푼다.
const WRITE_TOOLS = new Set(["write", "edit", "apply_patch"]);

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
// 세션 부모 조회 제한 — ctx-guard-core 의 SESSION_LOOKUP_TIMEOUT_MS 와 같은 값.
const SESSION_LOOKUP_TIMEOUT_MS = 1000;

function containsGitCommand(command) {
  if (typeof command !== "string") return false;
  const prefilter = command.replace(/\\\r?\n/g, "").replace(/["']/g, "");
  const normalized = prefilter.replace(/\\/g, "/").replace(/\/+/g, "/");
  return (
    GIT_PREFILTER.some((pattern) => pattern.test(prefilter))
    || normalized.includes(".project_manager/tools/")
    || normalized.includes(GRANT_DIR_NAME)
    || normalized.includes(SLOT_PATH_MARK)
  );
}

// 셸이 아닌 도구 입력 선필터 — 파일 쓰기 도구이거나, 허용 기록 폴더 이름·허용 표식이 어느 값에든 있을 때만 판정한다.
function toolInputNeedsJudgment(tool, args) {
  if (WRITE_TOOLS.has(tool)) return true;
  const text = JSON.stringify(args === undefined ? {} : args);
  return typeof text === "string" && (text.includes(GRANT_DIR_NAME) || GRANT_MARKER.test(text));
}

// board.py 하위 명령 실행 — { ok, stdout } 또는 { ok:false, error }.
function runBoard(root, argv, input, spawnSync, platform) {
  const board = path.join(root, ".project_manager", "tools", "board.py");
  const attempts = [];
  const candidate = supportedPythonCandidates(root, {spawnSync, platform, attempts}).next().value;
  if (!candidate) return {ok: false, error: attempts.map(
    ({label, failure}) => `${label}=${failure}`,
  ).join("; ") || "Python interpreter 없음"};
  const result = spawnSync(candidate.command, [...candidate.prefixArgs, board, ...argv], {
    encoding: "utf8", timeout: BOARD_TIMEOUT_MS, input,
  });
  if (!result || result.error) return {
    ok: false, error: result && result.error ? String(result.error.message || result.error) : "판정 subprocess 실패",
  };
  if (result.status !== 0) return {
    ok: false, error: String(result.stderr || `board.py rc=${result.status}`).trim(),
  };
  return {ok: true, stdout: String(result.stdout || "")};
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

// grants 를 주면 board 가 이 엔진 루트의 1회 허용(이 세션의 것)을 쓸 수 있는 하네스로 판정한다.
function judgeCommand(root, cwd, command, { session, grants = false, spawnSync = childProcess.spawnSync, platform } = {}) {
  if (!containsGitCommand(command)) {
    return { verdict: "ok", cwd_identity: "non-repo", reason: "git mutation 없음" };
  }
  const argv = ["git-anchor", "--session", String(session || ""), "--cwd", String(cwd || root), "--command", command];
  if (grants) argv.push("--grants");
  return judgmentFromBoard(runBoard(root, argv, undefined, spawnSync, platform));
}

function judgeToolInput(root, tool, args, { session, cwd, spawnSync = childProcess.spawnSync, platform } = {}) {
  if (!toolInputNeedsJudgment(tool, args)) {
    return { verdict: "ok", cwd_identity: "non-repo", reason: "허용 위조 표식 없음" };
  }
  const argv = ["git-tool-input", "--session", String(session || ""), "--cwd", String(cwd || root), "--tool", String(tool)];
  return judgmentFromBoard(runBoard(root, argv, JSON.stringify(args === undefined ? {} : args), spawnSync, platform));
}

// 사용자 메시지의 허용 줄을 기록한다. 기록 실패(형식 오류·인프라)는 조용히 넘기지 않고 올린다 —
// 허용 없이 대화가 진행되면 같은 거부가 반복될 뿐 사용자는 이유를 모른다.
function recordGrants(root, sessionID, cwd, prompt, { spawnSync = childProcess.spawnSync, platform } = {}) {
  const run = runBoard(
    root, ["git-grant", "--session", String(sessionID || ""), "--cwd", String(cwd || root)],
    prompt, spawnSync, platform,
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

  // native task 는 자식 세션을 새 ID 로 만든다(실측 opencode 1.18.31 `session.create({parentID})`). task 기록의
  // 세션 ID 는 부트스트랩한 최상위 세션이므로 자식 세션은 parentID 를 따라 올라간 ID 로 판정받는다.
  // SDK 표면은 ctx-guard-core 와 같다: client.session.get({ path: { id } }) -> { data: Session }.
  // 조회 실패는 삼키지 않고 올린다 — guardedJudgment 가 판정 인프라 실패로 다룬다.
  const rootSessions = new Map();

  async function sessionRecord(id) {
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

  function rootSessionID(sessionID) {
    if (!sessionID) return Promise.resolve("");
    if (!rootSessions.has(sessionID)) {
      const lookup = (async () => {
        let current = sessionID;
        for (;;) {
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
      const cwd = args.workdir || args.cwd || directory || worktree || process.cwd();
      if (shell ? !containsGitCommand(args.command) : !toolInputNeedsJudgment(tool, args)) return;
      // 설치 깨짐은 일시 실패가 아니다 — 판정 엔진이 자기 위치 아래 없으면 경고로 낮추지 않고 막는다.
      if (!fs.existsSync(BOARD_PY)) throw new Error(`[git-anchor/설치] 판정 엔진 부재: ${BOARD_PY}`);
      const judgment = await guardedJudgment(
        async () => {
          const session = await rootSessionID(sessionID);
          return shell
            ? judge(root, cwd, args.command, { session, grants: true })
            : judgeInput(root, tool, args, { session, cwd });
        },
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
