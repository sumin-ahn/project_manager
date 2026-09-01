// OpenCode native task delegation-channel guard.
// Python delegate_channel_guard.py owns normalization, roles, configuration,
// and decisions.  This module owns only the measured OpenCode hook surface and
// subprocess/warning wiring.
const path = require("node:path");
const fs = require("node:fs");
const childProcess = require("node:child_process");
const { createWarningChannel } = require("./warning-channel-core.cjs");

// The engine root comes from this file's own location: hooks always install at
// `<root>/.opencode/lib/*.cjs` (pm_import fixes that depth), the same rule as the
// Python tools' `Path(__file__).resolve().parents[2]`.  Walking ancestors picks the
// outer project inside nested trees.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");
const GUARD_PY = path.join(
  ENGINE_ROOT, ".project_manager", "tools", "delegate_channel_guard.py",
);

const DECIDE_TIMEOUT_MS = 10000;
const MIN_PYTHON = Object.freeze([3, 11]);

// Live probes on OpenCode 1.18.12 and the company baseline 1.18.5 measured the
// same surface: input={tool:"task",sessionID,callID}, output={args}, and
// args={description,prompt,subagent_type}.  subagent_type is the bare agent
// literal (for example "prober", with no prefix), and throwing here blocks the
// spawn (no tool.execute.after; the error reaches the model).
const DEFAULT_SURFACE = Object.freeze({
  toolName: "task",
  roleArgKey: "subagent_type",
  roleValuePrefix: "",
  enforceDeny: true,
});

function warning(reason) {
  return {
    verdict: "allow",
    reason: `[delegate-channel/warn] ${reason} — 통과(fail-open)`,
    harness: "",
    model: "",
    warning: true,
  };
}

function validJudgment(value) {
  return value
    && ["allow", "deny"].includes(value.verdict)
    && ["reason", "harness", "model"].every((key) => typeof value[key] === "string");
}

function pythonCandidates(platform = process.platform) {
  const py = {command: "py", prefixArgs: ["-3"], label: "py -3"};
  const python3 = {command: "python3", prefixArgs: [], label: "python3"};
  const python = {command: "python", prefixArgs: [], label: "python"};
  // Windows contract: the real Python launcher precedes WindowsApps aliases.
  // POSIX retains python3/python preference, with py as the final portable
  // candidate so every installed launcher remains reachable.
  return platform === "win32"
    ? [py, python3, python]
    : [python3, python, py];
}

function compactOutput(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function processFailure(result, phase) {
  if (!result) return `${phase} subprocess 결과 없음`;
  if (result.error && (result.status === null || result.status === undefined)) {
    const code = result.error.code ? String(result.error.code) : "오류";
    const detail = compactOutput(result.error.message || result.error);
    return `${phase} 실행 불가(${code}${detail ? `: ${detail}` : ""})`;
  }
  // A concrete status is authoritative even when a sandbox annotates the
  // completed child with EPERM.
  if (result.status !== 0) {
    const detail = compactOutput(result.stderr || result.stdout);
    return `${phase} rc=${result.status}${detail ? `(${detail})` : ""}`;
  }
  return null;
}

function validatedVersion(result) {
  const failure = processFailure(result, "버전 검증");
  if (failure) return {failure};
  const output = `${result.stdout || ""}\n${result.stderr || ""}`;
  const match = output.match(/\bPython\s+(\d+)\.(\d+)/);
  if (!match) return {failure: "버전 검증 출력 불일치"};
  const version = [Number(match[1]), Number(match[2])];
  if (
    version[0] < MIN_PYTHON[0]
    || (version[0] === MIN_PYTHON[0] && version[1] < MIN_PYTHON[1])
  ) {
    return {failure: `Python ${version.join(".")} < ${MIN_PYTHON.join(".")}`};
  }
  return {version};
}

function judgeDelegation(
  root,
  cwd,
  agentName,
  tier,
  spawnSync = childProcess.spawnSync,
  platform = process.platform,
) {
  const guard = path.join(
    root, ".project_manager", "tools", "delegate_channel_guard.py",
  );
  const floorProbe = path.join(
    root, ".project_manager", "tools", "python_floor.py",
  );
  const args = [
    guard, "decide", "--role", String(agentName || ""),
    "--harness", "opencode", "--cwd", String(cwd || root),
  ];
  if (tier) args.push("--tier", String(tier));

  const attempts = [];
  const spawnOptions = {
    encoding: "utf8",
    timeout: DECIDE_TIMEOUT_MS,
    input: "",
  };
  for (const candidate of pythonCandidates(platform)) {
    let probeResult;
    try {
      probeResult = spawnSync(
        candidate.command,
        [...candidate.prefixArgs, floorProbe],
        spawnOptions,
      );
    } catch (error) {
      const detail = error && error.message ? error.message : String(error);
      attempts.push({label: candidate.label, failure: `버전 검증 예외(${detail})`});
      continue;
    }
    const probe = validatedVersion(probeResult);
    if (probe.failure) {
      attempts.push({label: candidate.label, failure: probe.failure});
      continue;
    }

    let result;
    try {
      result = spawnSync(
        candidate.command,
        [...candidate.prefixArgs, ...args],
        spawnOptions,
      );
    } catch (error) {
      const detail = error && error.message ? error.message : String(error);
      attempts.push({label: candidate.label, failure: `판정 예외(${detail})`});
      continue;
    }
    const executionFailure = processFailure(result, "판정");
    if (executionFailure) {
      attempts.push({label: candidate.label, failure: executionFailure});
      continue;
    }
    try {
      const lines = String(result.stdout || "").trim().split(/\r?\n/);
      if (lines.length !== 1 || !lines[0]) throw new Error("한 줄 JSON 아님");
      const parsed = JSON.parse(lines[0]);
      if (!validJudgment(parsed)) throw new Error("판정 JSON 계약 불일치");
      return parsed;
    } catch (error) {
      attempts.push({
        label: candidate.label,
        failure: `판정 JSON 파싱 실패(${error.message})`,
      });
    }
  }
  const labels = attempts.map((item) => item.label).join(", ");
  const failures = attempts
    .map((item) => `${item.label}=${item.failure}`)
    .join("; ");
  return warning(
    `Python 판정 후보 소진(시도 후보: ${labels}; 실패: ${failures})`,
  );
}

function resolveSurface(overrides = {}) {
  return Object.freeze({
    ...DEFAULT_SURFACE,
    ...overrides,
  });
}

function agentNameFromArgs(args, surface) {
  const raw = args && args[surface.roleArgKey];
  if (typeof raw !== "string") return null;
  const prefix = String(surface.roleValuePrefix || "");
  if (prefix && raw.startsWith(prefix)) return raw.slice(prefix.length);
  return raw;
}

function makeDelegateChannelPlugin(
  judge = judgeDelegation,
  surfaceOverrides = {},
) {
  const surface = resolveSurface(surfaceOverrides);
  return async ({ client, directory, worktree }) => {
    // Live 1.18.12/1.18.5 probes observed worktree="/" even when directory
    // identified the actual scratch project, so directory must stay first.
    const cwd = directory || worktree || process.cwd();
    const root = ENGINE_ROOT;
    const warnings = createWarningChannel(client);

    return {
      "tool.execute.before": async (input, output) => {
        const tool = String((input && input.tool) || "").toLowerCase();
        if (tool !== String(surface.toolName).toLowerCase()) return;
        // A broken install is not a transient failure, and enforceDeny only governs
        // role denials, so a missing decision engine blocks unconditionally.
        if (!fs.existsSync(GUARD_PY)) {
          throw new Error(`[delegate-channel/설치] 판정 엔진 부재: ${GUARD_PY}`);
        }
        const args = output && output.args;
        const agentName = agentNameFromArgs(args, surface);
        const sessionID = (input && input.sessionID) || "__global__";

        let judgment;
        try {
          // Tool name is the only prefilter.  Missing/unknown role values still
          // reach Python so normalization and the allow+reason record stay in
          // the single decision truth.
          judgment = await judge(root, cwd, agentName === null ? "" : agentName, null);
          if (!validJudgment(judgment)) throw new Error("판정 결과 계약 불일치");
        } catch (error) {
          const detail = error && error.message ? error.message : String(error);
          judgment = warning(`판정 인프라 예외(${detail})`);
        }

        if (judgment.verdict === "deny" && surface.enforceDeny) {
          throw new Error(judgment.reason);
        }
        if (
          judgment.warning
          || judgment.reason.startsWith("[delegate-channel/warn]")
          || (judgment.verdict === "deny" && !surface.enforceDeny)
        ) {
          const text = judgment.verdict === "deny"
            ? `[delegate-channel/warn] advisory 강등: ${judgment.reason}`
            : judgment.reason;
          await warnings.publish(sessionID, text);
        }
      },
      "experimental.chat.system.transform": async (input, output) => {
        warnings.inject(input, output);
      },
    };
  };
}

const DelegateChannelPlugin = makeDelegateChannelPlugin();

module.exports = {
  DECIDE_TIMEOUT_MS,
  DEFAULT_SURFACE,
  MIN_PYTHON,
  DelegateChannelPlugin,
  agentNameFromArgs,
  judgeDelegation,
  makeDelegateChannelPlugin,
  pythonCandidates,
  resolveSurface,
  validJudgment,
};
