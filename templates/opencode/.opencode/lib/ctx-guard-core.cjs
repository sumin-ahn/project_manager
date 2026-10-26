// opencode 어댑터 — ctx checkpoint 가드 core.
//
// 이 파일(CJS)은 ctx-guard 의 *native 관측·전달 + 플러그인 팩토리 본체*를 담는다. opencode plugin
// 진입점은 `../plugins/ctx-guard.js`(ESM 얇은 shim)이며 여기서 CtxGuardPlugin 팩토리만 named-export
// 한다 — opencode 의 plugin 로드 규약(각 plugins/ 파일의 export 를 순회해 *모두 함수*이길 요구하고
// 각각을 팩토리로 호출·실측) 때문에, 헬퍼·상수는 plugins/ *바깥*(이 lib/ 모듈)에 둔다
// (opencode 는 plugins/ 만 스캔·lib/ 는 로드 안 함).
//
// 무엇:
//   opencode 세션의 토큰 관측(assistant message.updated)을 엔진 ctx 판정에 넘기고, 엔진이 낸 밴드 안내를
//   다음 모델 호출 system 에 1회 싣는다. session.compacted 는 압축 경계를 엔진에 넘겨(밴드·원칙 recall
//   재무장) snapshot·checkpoint 를 기록하고 압축 후 안내를 싣는다.
//
// 판정 소유: 사용 한도·쓴 %·밴드·마진·안내 문구·구표기 설정 거부·중복 발화 marker 는 엔진
//   (`board.py hook-context`·`hook-tool`)이 한다. 이 모듈은 native 토큰 합산·세션 소유(SDK 부모)·
//   압축 사이클 epoch·pending 안내·snapshot generation/receipt 전달만 한다.
//
// 일반 도구 호출: 이 플러그인의 `tool.execute.before` 하나가 git-anchor-core `createToolBoundary` 를
//   통해 git·삭제·도구 입력·ctx·원칙 recall 을 Python 한 번(`board.py hook-tool`)으로 판정한다. 세션의
//   마지막 토큰 관측을 그 요청에 싣는다. ctx 밴드는 차단하지 않는다.

const fs = require("node:fs");
const path = require("node:path");
const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const { supportedPythonCandidates } = require("./delegate-channel-core.cjs");
const { createToolBoundary, runHookContext } = require("./git-anchor-core.cjs");

// 엔진 루트는 이 파일 자기 위치에서 나온다 — 훅은 언제나 `<root>/.opencode/lib/*.cjs` 에 설치되고
// (pm_import 가 그 깊이를 못박는다) 이는 파이썬 도구의 `Path(__file__).resolve().parents[2]` 와 같은
// 규칙이다. 조상을 훑으면 중첩 트리에서 바깥 프로젝트의 pm_log.py 를 실행한다.
const ENGINE_ROOT = path.resolve(__dirname, "..", "..");
const CTX_STOP_REL = path.join(".project_manager", ".local", "ctx-stop");
const SNAPSHOT_TIMEOUT_MS = 3000;
const CHECKPOINT_TIMEOUT_MS = 5000;
const COMPACTION_SNAPSHOT_RECEIPT_RETAIN = 8;
// 압축 기록 실패 신호 머리 — claude `ctx_stop_hook` 과 같은 문구(압축 안내 첫 줄).
const COMPACTION_FAILURE_PREFIX = "[ctx-checkpoint] 압축 기록 실패: ";

// ── 순수 함수: AssistantMessage.tokens 누적 = 현재 컨텍스트 점유 토큰 ──────────
// opencode 의 컨텍스트 점유 = 직전 어시스턴트 턴의 input + cache(read+write) + output + reasoning.
// (input/cache 가 누적 컨텍스트를 반영 — 매 턴의 최신값을 쓰고, 합산이 아니라 최신 메시지 기준.)
function accumulateTokens(tokens) {
  if (!tokens || typeof tokens !== "object") return 0;
  // total 우선 — opencode 자체 overflow 판정과 동일(1.18.5 overflow.ts·driver 파서와 통일).
  // 부재/비양수면 기존 합산 폴백(보수적 상위집합).
  const total = Number(tokens.total);
  if (Number.isFinite(total) && total > 0) return total;
  const input = Number(tokens.input) || 0;
  const output = Number(tokens.output) || 0;
  const reasoning = Number(tokens.reasoning) || 0;
  const cacheRead = tokens.cache ? Number(tokens.cache.read) || 0 : 0;
  const cacheWrite = tokens.cache ? Number(tokens.cache.write) || 0 : 0;
  return input + output + reasoning + cacheRead + cacheWrite;
}

// ── compaction 엔진 호출: 지원 실행기 해소 이후 실제 엔진 1회 호출 ──
function runPmLog(root, args, options = {}, spawnSync = childProcess.spawnSync) {
  const engine = path.join(root, ".project_manager", "tools", "pm_log.py");
  const capture = options.capture === true;
  const captureStderr = options.captureStderr === true;
  const timeout = options.timeout || SNAPSHOT_TIMEOUT_MS;
  const candidate = supportedPythonCandidates(root, {
    spawnSync, platform: options.platform, timeout, attempts: options.attempts,
  }).next().value;
  if (!candidate) return null;
  return spawnSync(candidate.command, [...candidate.prefixArgs, engine, ...args], {
    cwd: root,
    encoding: "utf8",
    timeout,
    maxBuffer: 32 * 1024,
    stdio: ["ignore", capture ? "pipe" : "ignore", captureStderr ? "pipe" : "ignore"],
  }) || null;
}

function buildCompactionSnapshot(root, sessionID, spawnSync = childProcess.spawnSync) {
  // Builder command token: pm_log.py snapshot. stdout payload는 변형 없이 system[]에 전달한다.
  // 정체성은 세션 ID 하나다(그 세션이 연 task). 실패면 엔진 stderr 마지막 줄을 실패 신호로 싣는다.
  const result = runPmLog(
    root,
    ["snapshot", "--session-id", String(sessionID || "")],
    { capture: true, captureStderr: true, timeout: SNAPSHOT_TIMEOUT_MS },
    spawnSync,
  );
  if (result && result.status === 0 && typeof result.stdout === "string" && result.stdout) {
    return { ok: true, text: result.stdout };
  }
  const stderrLines = String((result && result.stderr) || "")
    .split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const reason = stderrLines.length
    ? stderrLines[stderrLines.length - 1]
    : `pm_log status=${result ? result.status : "실행 불가"}`;
  return { ok: false, text: COMPACTION_FAILURE_PREFIX + reason };
}

// ── compaction snapshot durable marker (Claude PostCompact 패턴 대칭) ─────────
// opencode run --continue 는 프로세스-당-턴 one-shot 이므로 턴 말미 session.compacted 에서
// in-memory pendingNudgeText 만 무장하면 다음 프로세스까지 payload가 살아남지 못한다. PM root의
// 기존 ctx-stop 디렉터리(compact-checkpoint.*와 동일)에 SID+generation별 payload를
// 원자 stage한다. SID 필터/길이는 pm_log.py `_safe_marker_key`의 파일명 규약과
// 맞추되 marker 채널에서는 빈 결과를 unknown으로 합치지 않고 skip한다.
function safeCompactionSnapshotSessionKey(sessionID) {
  if (typeof sessionID !== "string" || !sessionID.trim()) return null;
  const key = sessionID.trim().replace(/[^A-Za-z0-9_-]/g, "").slice(0, 96);
  return key || null;
}

function createCompactionSnapshotGeneration() {
  const utc = new Date().toISOString().replace(/[-:.]/g, "");
  return `${utc}-${crypto.randomUUID()}`;
}

function compactionSnapshotMarkerDirectory(root) {
  return path.resolve(root, CTX_STOP_REL);
}

function compactionSnapshotMarkerPath(root, sessionID, generation) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key || typeof generation !== "string") return null;
  if (!/^[A-Za-z0-9_-]+$/.test(generation)) return null;
  return path.join(directory, `compact-snapshot.${key}.${generation}`);
}

function compactionSnapshotReceiptPath(root, sessionID, generation) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key || typeof generation !== "string") return null;
  if (!/^[A-Za-z0-9_-]+$/.test(generation)) return null;
  return path.join(directory, `compact-snapshot-receipt.${key}.${generation}`);
}

function compactionSnapshotGenerationFromMarker(root, sessionID, ownedMarker) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key || typeof ownedMarker !== "string") return null;
  const marker = path.resolve(ownedMarker);
  const prefix = `compact-snapshot.${key}.`;
  if (path.dirname(marker) !== directory || !path.basename(marker).startsWith(prefix)) return null;
  const generation = path.basename(marker).slice(prefix.length);
  return generation && /^[A-Za-z0-9_-]+$/.test(generation) ? generation : null;
}

function writeCompactionSnapshotReceipt(root, sessionID, generation) {
  const receipt = compactionSnapshotReceiptPath(root, sessionID, generation);
  if (!receipt) return;
  try {
    // receipt 자체가 전달 증거다. generation이 유일하므로 원자 rename 없이 빈 0600 파일이면 충분하다.
    fs.writeFileSync(receipt, "", { encoding: "utf8", mode: 0o600 });
  } catch {
    /* 관측 채널 IO 실패가 이미 수행된 system 전달을 막지 않는다. */
  }
}

function pruneCompactionSnapshotReceipts(root, sessionID) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key) return;
  const prefix = `compact-snapshot-receipt.${key}.`;
  try {
    const receipts = fs.readdirSync(directory, { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.startsWith(prefix))
      .map((entry) => ({
        receipt: path.join(directory, entry.name),
        generation: entry.name.slice(prefix.length),
      }))
      .filter(({ generation }) => generation && /^[A-Za-z0-9_-]+$/.test(generation))
      .sort((left, right) => left.generation.localeCompare(right.generation));
    for (const { receipt } of receipts.slice(0, -COMPACTION_SNAPSHOT_RECEIPT_RETAIN)) {
      try {
        fs.unlinkSync(receipt);
      } catch {
        /* 동시 정리/권한 오류는 다음 staging GC에 맡긴다. */
      }
    }
  } catch {
    /* receipt 열거 실패도 snapshot staging을 실패로 바꾸지 않는다. */
  }
}

function stageCompactionSnapshot(root, sessionID, payload) {
  if (typeof payload !== "string" || !payload) return null;
  const marker = compactionSnapshotMarkerPath(
    root, sessionID, createCompactionSnapshotGeneration(),
  );
  if (!marker) return null;
  const temp = path.join(
    path.dirname(marker),
    `.${path.basename(marker)}.${process.pid}.${crypto.randomUUID()}.tmp`,
  );
  try {
    fs.mkdirSync(path.dirname(marker), { recursive: true });
    fs.writeFileSync(temp, payload, { encoding: "utf8", mode: 0o600 });
    fs.renameSync(temp, marker);
    pruneCompactionSnapshotReceipts(root, sessionID);
    return marker;
  } catch {
    return null;
  } finally {
    try {
      fs.unlinkSync(temp);
    } catch {
      /* staging 실패/rename 완료 뒤 temp 정리는 best-effort. */
    }
  }
}

function discardCompactionSnapshot(root, sessionID, ownedMarker) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key || typeof ownedMarker !== "string") return;
  const marker = path.resolve(ownedMarker);
  const prefix = `compact-snapshot.${key}.`;
  // 세션 상태가 기억한 자기 generation 정확한 경로만 삭제한다.
  if (path.dirname(marker) !== directory || !path.basename(marker).startsWith(prefix)) return;
  try {
    fs.unlinkSync(marker);
  } catch {
    /* marker 부재/삭제 실패는 모델 호출을 막지 않는다. */
  }
}

function listCompactionSnapshots(root, sessionID) {
  const directory = compactionSnapshotMarkerDirectory(root);
  const key = safeCompactionSnapshotSessionKey(sessionID);
  if (!key) return [];
  const prefix = `compact-snapshot.${key}.`;
  const snapshots = [];
  try {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (!entry.isFile() || !entry.name.startsWith(prefix)) continue;
      const generation = entry.name.slice(prefix.length);
      if (!generation || !/^[A-Za-z0-9_-]+$/.test(generation)) continue;
      const marker = path.join(directory, entry.name);
      try {
        const stat = fs.statSync(marker, { bigint: true });
        if (stat.isFile()) snapshots.push({ marker, generation, mtimeNs: stat.mtimeNs });
      } catch {
        /* 동시 소비/정리로 사라진 entry는 다음 스냅샷에 맡긴다. */
      }
    }
  } catch {
    return [];
  }
  snapshots.sort((left, right) => {
    if (left.mtimeNs < right.mtimeNs) return -1;
    if (left.mtimeNs > right.mtimeNs) return 1;
    return left.generation.localeCompare(right.generation);
  });
  return snapshots;
}

function takeCompactionSnapshot(root, sessionID) {
  const snapshots = listCompactionSnapshots(root, sessionID);
  if (!snapshots.length) return null;
  const latest = snapshots[snapshots.length - 1];
  // old를 latest보다 먼저 지워 claim 진행 중 [old]만 관측되는 상태를 만들지 않는다.
  // old 삭제 뒤 claim 전에 crash해도 latest는 생존한다. 고정 generation 이름의 배타 link가 선점이다.
  // 선점 패배자는 구 generation 재탐색/fallback 없이 skip한다.
  for (const snapshot of snapshots.slice(0, -1)) {
    try {
      fs.unlinkSync(snapshot.marker);
    } catch (error) {
      // 동시 GC로 이미 사라진 old는 성공 동치다. 그 밖의 실패에서는 latest를 남겨
      // 다음 소비자가 old와 함께 다시 관측하도록 claim 없이 fail-soft skip한다.
      if (!error || error.code !== "ENOENT") return null;
    }
  }
  const claimed = path.join(
    path.dirname(latest.marker),
    `.${path.basename(latest.marker)}.claimed`,
  );
  try {
    fs.linkSync(latest.marker, claimed);
  } catch {
    return null;
  }
  try {
    fs.unlinkSync(latest.marker);
  } catch {
    // ENOENT도 전달하지 않는다. 원본 제거에 실패한 선점 fence는 재진입을 막기 위해 남긴다.
    return null;
  }
  try {
    const payload = fs.readFileSync(claimed, "utf8");
    return payload ? { payload, generation: latest.generation } : null;
  } catch {
    return null;
  } finally {
    try {
      fs.unlinkSync(claimed);
    } catch {
      /* claimed marker 정리 실패도 주입 경계를 막지 않는다. */
    }
  }
}

function createCompactionBoundaryID(sessionID) {
  // count는 plugin 재시작 때 0으로 돌아가므로 dedup ID에 쓰지 않는다. UTC+UUID 축을 앞에 두어
  // pm_log의 marker 길이 상한에서도 재시작 불변 유일 성분이 잘리지 않게 한다.
  const utc = new Date().toISOString().replace(/[-:.]/g, "");
  return `opencode-${utc}-${crypto.randomUUID()}-${String(sessionID || "unknown")}`;
}

function createCompactionCheckpoint(
  root, sessionID, boundaryID, spawnSync = childProcess.spawnSync,
) {
  const args = ["checkpoint", "--trigger", "compaction", "--phase", "post"];
  if (typeof sessionID === "string" && sessionID.trim()) {
    args.push("--session-id", sessionID.trim());
  }
  if (typeof boundaryID === "string" && boundaryID.trim()) {
    args.push("--boundary-id", boundaryID.trim());
  }
  runPmLog(root, args, { capture: false, timeout: CHECKPOINT_TIMEOUT_MS }, spawnSync);
}

// ── plugin 팩토리 (진입점 ../plugins/ctx-guard.js 가 ESM named-export 로 재노출·opencode autoload) ──
const CtxGuardPlugin = async ({ client, $, directory, worktree }) => {
  // 로드 검증 마커 (env-gated·라이브-로드 게이트 전용·실 세션엔 무음). opencode 는 성공 로드
  // 시 positive 로그를 남기지 않아(실측 1.17.18) — factory 실행을 관측하려면 스스로 마커를 낸다.
  // 훅 로직 무관 — CTX_GUARD_LOAD_PROBE 미설정 시 완전 무음(프로덕션 기본).
  if (process.env.CTX_GUARD_LOAD_PROBE) {
    process.stderr.write("[ctx-guard] plugin factory loaded (CTX_GUARD_LOAD_PROBE)\n");
  }
  // 모든 가드 상태는 hook 인스턴스 전역이 아니라 sessionID 별로 분리한다. observation 은 SDK 가 알려 준
  // 마지막 토큰 관측(tokens·model)이며 판정 결과를 담지 않는다.
  const sessionStates = new Map();

  function sessionState(sessionID) {
    if (typeof sessionID !== "string" || !sessionID.trim()) return null;
    let state = sessionStates.get(sessionID);
    if (!state) {
      state = {
        observation: null,
        pendingNudgeText: null,
        pendingSnapshotMarker: null,
        budgetNotice: null,
        compactionCount: 0,
        cycleEpoch: 0,
      };
      sessionStates.set(sessionID, state);
    }
    return state;
  }

  const root = ENGINE_ROOT;

  // 넛지: 사이클당 1회 toast(없으면 무음 — fail-soft).
  async function notifyNudge(message) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 넛지는 best-effort. */
    }
  }

  // 2단·최종 넛지: 사이클당 1회 강한 toast(compaction 임박). 사람용 2단 표시(없으면 무음).
  async function notifyNudge2(message) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 넛지는 best-effort. */
    }
  }

  // compaction 직후 사람용 toast. 모델 안내는 pendingNudgeText/system.transform 채널을 병행한다.
  async function notifyCompacted(message) {
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: {
            message,
            variant: "warning",
          },
        });
      }
    } catch {
      /* toast 실패는 무시 — 모델-facing checkpoint 안내는 별도 채널이다. */
    }
  }

  // 일반 도구 호출 판정 요청에 실을 세션의 마지막 관측. epoch 는 응답을 받을 때 사이클이 바뀌었는지 본다.
  function contextFor(sessionID) {
    const state = sessionState(sessionID);
    if (!state || !state.observation) return null;
    return { ...state.observation, epoch: state.cycleEpoch };
  }

  // 엔진 ctx 응답을 native 채널로 전달한다 — 기록 알림은 같은 문구를 세션당 한 번 toast, 밴드 안내는
  // 다음 모델 호출 system 에 1회. 관측 뒤 압축이 끼었으면(epoch 변화) 옛 사이클 응답은 버린다.
  async function acceptContext(sessionID, decision, observed) {
    const state = sessionState(sessionID);
    if (!state || !observed || observed.epoch !== state.cycleEpoch) return;
    if (decision.notice && state.budgetNotice !== decision.notice) {
      state.budgetNotice = decision.notice;
      await notifyNudge(`[ctx-budget] ${decision.notice}`);
    }
    if (!decision.emit) return;
    state.pendingNudgeText = decision.guidance;
    state.pendingSnapshotMarker = null;
    if (decision.level === "nudge") await notifyNudge(decision.guidance);
    else await notifyNudge2(decision.guidance);
  }

  const boundary = createToolBoundary({ root, client, directory, worktree, contextFor, acceptContext });

  // 세션 소유 — "main"·"child", SDK 로 확인하지 못하면 경고하고 null(main·child 어느 쪽으로도 추정하지 않는다).
  // Native task 자식은 독립 컨텍스트로 구동되므로 PM 메인 세션용 ctx 판정·snapshot 적재/소비를 면제한다.
  async function sessionOwner(sessionID) {
    try {
      return (await boundary.rootSessionID(sessionID)) === sessionID ? "main" : "child";
    } catch (error) {
      const detail = error && error.message ? error.message : String(error);
      await boundary.publish(sessionID, `[ctx-guard] 세션 확인 실패(${detail}) — 이 이벤트의 ctx 판정·압축 기록을 건너뜀`);
      return null;
    }
  }

  async function publishFailure(sessionID, label, error) {
    const detail = error && error.message ? error.message : String(error);
    await boundary.publish(sessionID, `[ctx-guard] ${label}(${detail})`);
  }

  return {
    // ── 일반 도구 호출 판정: git·삭제·도구 입력·ctx·원칙 recall 을 Python 한 번으로 ────────
    "tool.execute.before": boundary.before,

    // ── 병행 신호: compaction 사후 안내 + 어시스턴트 메시지 임박 밴드 ────────
    event: async ({ event }) => {
      if (!event) return;

      if (event.type === "session.compacted") {
        const sessionID = event.properties && event.properties.sessionID;
        const session = sessionState(sessionID);
        if (!session) return;
        session.compactionCount += 1;
        session.cycleEpoch += 1;
        // 압축 전 토큰 관측은 새 사이클의 사용량이 아니다 — 다음 assistant 관측까지 ctx 를 싣지 않는다.
        session.observation = null;
        // 자식 세션은 snapshot/checkpoint 모두 면제다. 최대 3초 동기 subprocess 전에 판정해
        // 면제 대상 compaction 이벤트가 메인 세션용 snapshot 때문에 멈추지 않게 한다.
        if ((await sessionOwner(sessionID)) !== "main") return;
        // 압축 경계 ID 를 엔진 ctx 판정(밴드·원칙 recall 재무장)과 checkpoint 에 같은 값으로 넘긴다.
        const boundaryID = createCompactionBoundaryID(sessionID);
        let compactedGuidance = null;
        try {
          const decision = runHookContext(root, {
            session: sessionID, tokens: 0, model: null, main_thread: true, boundary: boundaryID,
          });
          compactedGuidance = decision.compaction_guidance;
        } catch (error) {
          await publishFailure(sessionID, "압축 경계 판정 실패", error);
        }
        // snapshot 본문/렌더는 pm_log.py 단일 소유. plugin은 builder stdout을 verbatim 적재한다.
        const stagedSnapshot = buildCompactionSnapshot(root, sessionID);
        const stagedNotice = stagedSnapshot.ok || !compactedGuidance
          ? stagedSnapshot.text
          : `${stagedSnapshot.text}\n${compactedGuidance}`;
        session.pendingNudgeText = stagedNotice;
        // 같은 payload를 프로세스 경계 너머에도 보존한다. 같은 프로세스의
        // 재-compaction은 새 generation stage 성공 후 자기 구 generation만 정리한다.
        const previousMarker = session.pendingSnapshotMarker;
        const stagedMarker = stageCompactionSnapshot(root, sessionID, stagedNotice);
        if (stagedMarker) {
          session.pendingSnapshotMarker = stagedMarker;
          if (previousMarker && previousMarker !== stagedMarker) {
            discardCompactionSnapshot(root, sessionID, previousMarker);
          }
        }
        // staging 실패 시 memory는 새 payload이어도 구 durable marker 소유권은 보수적으로 유지한다.
        createCompactionCheckpoint(root, sessionID, boundaryID);
        if (compactedGuidance) await notifyCompacted(compactedGuidance);
        return;
      }

      if (event.type !== "message.updated") return;
      const info = event.properties && event.properties.info;
      if (!info) return;
      if (info.role !== "assistant" || !info.tokens) return;
      // SID는 아래 await들보다 먼저 event-local로 캡처한다(pendingNudgeText 귀속 경합 방지).
      const sessionID = info.sessionID;
      const session = sessionState(sessionID);
      if (!session) return;
      // await 중 session.compacted가 사이클을 넘기면 이 이벤트는 구 사이클 소속이다.
      // 진입 시 epoch를 캡처해 새 사이클의 관측/pending 상태를 덮지 않는다.
      const cycleEpoch = session.cycleEpoch;
      if ((await sessionOwner(sessionID)) !== "main") return;
      if (session.cycleEpoch !== cycleEpoch) return;

      const tokens = accumulateTokens(info.tokens);
      if (!Number.isSafeInteger(tokens) || tokens < 0) {
        await boundary.publish(sessionID, `[ctx-guard] 토큰 관측 형식 오류(${tokens}) — 이 관측의 ctx 판정을 건너뜀`);
        return;
      }
      // 모델 식별자가 둘 다 문자열일 때만 `provider/model`, 아니면 미관측(null)이다.
      const model = typeof info.providerID === "string" && typeof info.modelID === "string"
        ? `${info.providerID}/${info.modelID}`
        : null;
      session.observation = { tokens, model };
      const observed = { tokens, model, epoch: cycleEpoch };
      let decision;
      try {
        decision = runHookContext(root, { session: sessionID, tokens, model, main_thread: true, boundary: null });
      } catch (error) {
        await publishFailure(sessionID, "판정 실패", error);
        return;
      }
      await acceptContext(sessionID, decision, observed);
    },

    // ── checkpoint 모델-주입: pending 안내를 system 에 비차단 1회 주입 ────────
    // experimental.chat.system.transform 은 모델 호출 전 system[] 을 비차단 수정한다(@opencode-ai
    // /plugin Hooks·opencode 1.17.11 타입 확인). chat.message 의 full Part 구성(id/sessionID/
    // messageID 필수)보다 string push 가 안전·정확. ⚠️ experimental namespace — opencode 가 이 surface
    // 를 바꾸면 *조용히* 주입이 멈출 수 있다. 호환성 게이트 = Tier2
    // 라이브 smoke(버전 회귀 포착)·codex 권고 반영. 변동 시 안정 chat.message 전환 검토.
    // 도구 호출 판정 경고(세션별 1칸)를 먼저 싣는다 — 자식 세션의 경고도 그 세션에 전달한다.
    // 멱등: in-memory payload는 자기가 stage한 generation만 함께 지운다.
    // 새 프로세스라 in-memory payload가 없으면 최신 marker를 선점·읽기·소거하고,
    // 그 관측 스냅샷에 함께 있던 구 generation들을 중복 주입 없이 정리한다.
    // 자식 세션은 적재와 마찬가지로 marker 소비도 면제한다.
    "experimental.chat.system.transform": async (input, output) => {
      boundary.inject(input, output);
      const sessionID = input && input.sessionID;
      const session = sessionState(sessionID);
      if (!session) return;
      if ((await sessionOwner(sessionID)) !== "main") return;
      if (session.pendingNudgeText && output && Array.isArray(output.system)) {
        output.system.push(session.pendingNudgeText);
        const ownedMarker = session.pendingSnapshotMarker;
        session.pendingNudgeText = null;
        session.pendingSnapshotMarker = null;
        if (ownedMarker) {
          const generation = compactionSnapshotGenerationFromMarker(root, sessionID, ownedMarker);
          if (generation) writeCompactionSnapshotReceipt(root, sessionID, generation);
          discardCompactionSnapshot(root, sessionID, ownedMarker);
        }
        return;
      }
      if (output && Array.isArray(output.system)) {
        const stagedSnapshot = takeCompactionSnapshot(root, sessionID);
        if (stagedSnapshot) {
          output.system.push(stagedSnapshot.payload);
          writeCompactionSnapshotReceipt(root, sessionID, stagedSnapshot.generation);
        }
      }
    },
  };
};

// CommonJS export — node 자가검증(require)·ESM shim(../plugins/ctx-guard.js) 양쪽 소비.
// opencode 는 이 모듈을 직접 로드하지 않는다(plugins/ 만 스캔) — 얇은 ESM shim 이 CtxGuardPlugin 만
// named-export 해 로드 규약(export=단일 함수·실측)을 만족한다. 여기 export 는 native 헬퍼+상수+팩토리.
module.exports = {
  CtxGuardPlugin,
  accumulateTokens,
  runPmLog,
  buildCompactionSnapshot,
  safeCompactionSnapshotSessionKey,
  createCompactionSnapshotGeneration,
  createCompactionBoundaryID,
  createCompactionCheckpoint,
  SNAPSHOT_TIMEOUT_MS,
  CHECKPOINT_TIMEOUT_MS,
};
