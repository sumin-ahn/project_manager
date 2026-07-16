// opencode 어댑터 — 대용량 write/edit 신뢰성 가드 core (T-0334, 설계 PM 69/70 리서치).
//
// 이 파일(CJS)은 safe-write 의 *순수 로직 + 플러그인 팩토리 본체*를 담는다. opencode plugin
// 진입점은 `../plugins/safe-write.js`(ESM 얇은 shim)이며 거기서 팩토리(SafeWritePlugin) 하나만
// named-export 한다 — opencode 의 plugin 로드 규약(각 plugins/ 파일의 export 를 순회해 *모두 함수*
// 이길 요구하고 각각을 팩토리로 호출·실측 T-0283) 때문에, 순수 헬퍼·상수는 plugins/ *바깥*(이 lib/
// 모듈)에 둔다(opencode 는 plugins/ 만 스캔·lib/ 는 로드 안 함). node 자가검증 test 는 이 CJS
// 모듈을 require 해 순수 함수(checkOversizeWrite·validateSafeWrite·safeWrite…)를 opencode 런타임
// 없이 검증한다.
//
// ⚠️ 이 core 는 `@opencode-ai/plugin`(tool 헬퍼·zod)을 require 하지 않는다 — 그 패키지는 opencode
//    런타임에만 설치되므로 core 가 직접 import 하면 node 자가검증(plain node require)이 깨진다.
//    대신 ESM shim 이 `tool` 을 import 해 `makeSafeWritePlugin(tool)` 로 주입한다(팩토리 커링).
//
// 무엇 (3층 가드·업계 수렴·PM 70 리서치):
//   opencode 1.17.x~1.18.x 는 대용량 write/edit 를 조용히 절단·유실한다(OUTPUT_TOKEN_MAX=32000
//   하드코딩·silent truncation·auto-continue 부재·upstream #18108/#19604/#17471 미해결). 어댑터층
//   3층 가드로 닫는다:
//     ① deny-and-redirect (tool.execute.before): write/edit 의 대형 args 가 DENY_BYTES 초과 시
//        throw — 에러 메시지가 모델-facing 행동 지시(기존 파일=edit 문자열-치환으로 나눠라 /
//        신규 파일=safe_write 로 8KB chunk create→append). 업계 수렴(Claude Code·Gemini·Cline·
//        aider 전부 대형 재작성→문자열-치환 diff 유도·aider 실측 lazy 누락 3배 감소).
//     ② safe_write custom tool: 신규-대형-파일 갭 전용(anchor 부재로 edit 불가한 케이스). 8KB
//        chunk 상한을 도구가 강제(초과 거부)·create(신규)→append(이어쓰기) 순으로 쌓고 누적
//        바이트/라인을 보고(모델이 진행 파악). apply_patch 로 redirect 하지 않는다 — patch 문법은
//        모델 fluency 의존(비-OpenAI 모델 실패 실측·glm-5.2 신뢰 불가). redirect 대상은 edit·safe_write 둘뿐.
//     ③ 출력 상한 config: opencode.jsonc `limit.output` 명시 — fallback *미정의* 상태를 없앤다.
//        (실효 출력은 여전히 min(limit.output, 32000)=32000 이라 상한 자체를 올리진 못한다 — 32000
//        상향은 env OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX 로만 가능. deny(①)가 실질 억제 레버.)
//
// ⚠️ 한계 (구조): 하드 절단(finish_reason=length·tool-call JSON 자체가 절단)은 plugin 이 못
//    잡는다 — 완성된 tool call 이 애초에 도착하지 않는다(before 훅은 완성된 args 를 전제). 완화 =
//    limit.output 명시(③) + deny(①)로 대형 시도 자체 억제. upstream 미해결(#18108 root-cause·
//    #19604 write silent fail ~1000줄·#17471 auto-continue open·fix PR 전부 closed-unmerged·후속 추적).
// ⚠️ 범위: 이 가드는 write/edit tool 만 가로챈다 — bash heredoc/echo·apply_patch 등 타 쓰기 표면은
//    범위 밖(티켓 결정·redirect 대상은 edit·safe_write 둘뿐). safe_write 는 raw fs 라 아래 root
//    containment(assertContainedPath·realpath)로 프로젝트 밖 쓰기를 봉쇄한다(opencode 권한계층 우회 방어).
//
// 결정 로직(checkOversizeWrite·validateSafeWrite·byteLength·resolve*Bytes)은 순수 함수로 떼어
// export — node 로 자가검증(opencode 런타임 없이). safeWrite 는 fs 부작용 포함(임시 dir 로 검증).

const fs = require("node:fs");
const path = require("node:path");

// ── 임계값 상수 (core 상단 명시·env override) ────────────────────────────────
// DENY_BYTES: write content / edit(newString+oldString) 이 이 크기 초과 시 deny-and-redirect.
//   16KB — upstream #19604 실측 실패 구간(~1000줄=30~40KB)보다 보수적(routine write 마찰 최소화).
// CHUNK_BYTES: safe_write 한 chunk 상한(도구가 강제). 8KB — PM 69 발의값(32k 토큰 상한 대비 안전).
// 회사/모델별 튜닝: 각 env override(PM_SAFE_WRITE_DENY_BYTES·PM_SAFE_WRITE_CHUNK_BYTES).
const DENY_BYTES = 16 * 1024; // 16384
const CHUNK_BYTES = 8 * 1024; // 8192
const DENY_BYTES_ENV = "PM_SAFE_WRITE_DENY_BYTES";
const CHUNK_BYTES_ENV = "PM_SAFE_WRITE_CHUNK_BYTES";

// ── 순수 함수: UTF-8 바이트 길이 (문자 수 아님 — 대용량 판정은 바이트 기준) ──────
function byteLength(s) {
  return typeof s === "string" ? Buffer.byteLength(s, "utf-8") : 0;
}

// ── 순수 함수: env override 해소 (>0 정수 sanity·아니면 기본) ─────────────────
// raw(env 값)가 양의 정수면 그걸, 아니면(미설정·비정수·≤0·공백) dflt 을 쓴다.
function resolvePositiveInt(raw, dflt) {
  if (raw === undefined || raw === null) return dflt;
  const n = Number(String(raw).trim());
  return Number.isInteger(n) && n > 0 ? n : dflt;
}

function resolveDenyBytes(env) {
  return resolvePositiveInt(env ? env[DENY_BYTES_ENV] : undefined, DENY_BYTES);
}

function resolveChunkBytes(env) {
  return resolvePositiveInt(env ? env[CHUNK_BYTES_ENV] : undefined, CHUNK_BYTES);
}

// ── 순수 함수: deny 메시지 (모델-facing 행동 지시) ────────────────────────────
// kind: "write-existing"(기존 파일 전체 재작성) / "write-new"(신규 대형 파일) / "edit"(치환 과대).
// 크기·상한을 알려주고 *무엇을 대신 하라*(edit 나눠라 / safe_write chunk)를 명시한다. chunkBytes 는
// safe_write chunk 상한(resolveChunkBytes)을 그대로 안내 — env override 시에도 실값과 어긋나지 않게.
function buildDenyMessage(kind, bytes, denyBytes, chunkBytes) {
  const head =
    `[safe-write] 쓰기 크기 ${bytes}B 가 상한 ${denyBytes}B 초과 — opencode 는 대용량 write/edit 를 ` +
    `조용히 절단·유실할 수 있어 차단했다. `;
  if (kind === "write-existing") {
    return (
      head +
      `이 파일은 이미 존재한다: 전체 재작성(write) 말고 edit(문자열 치환)로 바꿀 부분만 ` +
      `여러 번 나눠서 수정하라 (한 edit 당 ${denyBytes}B 이하).`
    );
  }
  if (kind === "write-new") {
    return (
      head +
      `신규 대형 파일이다: write 한 번에 쓰지 말고 safe_write(filePath, content, mode) 도구로 ` +
      `${chunkBytes}B 이하 chunk 를 mode="create"(첫 조각)→mode="append"(이후 조각) 순서로 나눠 써라.`
    );
  }
  // edit
  return (
    head +
    `이 edit 의 치환 문자열(newString+oldString)이 너무 크다: edit 를 더 작은 문자열 치환 ` +
    `여러 개로 나눠라. 신규 대형 파일을 만드는 중이면 safe_write 도구로 ${chunkBytes}B chunk 를 ` +
    `create→append 로 써라.`
  );
}

// ── 순수 함수: write/edit 대형 판정 (deny-and-redirect·tool.execute.before 핵심) ──
// toolName: "write"|"edit"(그 외는 무판정). args: opencode tool args(output.args). fileExists:
// write 대상 파일이 이미 존재하는가(플러그인이 fs.existsSync 로 결정해 주입 — core 는 순수 유지).
// chunkBytes: safe_write 상한(deny 메시지 안내값·env override 반영). 반환: { deny:false } 또는
// { deny:true, kind, bytes, message }.
//   write → byteLength(content) > denyBytes 시 deny. kind = 존재하면 "write-existing" 아니면 "write-new".
//   edit  → byteLength(newString)+byteLength(oldString) > denyBytes 시 deny. kind = "edit".
function checkOversizeWrite(toolName, args, denyBytes, fileExists, chunkBytes) {
  const t = String(toolName || "").toLowerCase();
  const a = args || {};
  if (t === "write") {
    const bytes = byteLength(a.content);
    if (bytes <= denyBytes) return { deny: false };
    const kind = fileExists ? "write-existing" : "write-new";
    return { deny: true, kind, bytes, message: buildDenyMessage(kind, bytes, denyBytes, chunkBytes) };
  }
  if (t === "edit") {
    const bytes = byteLength(a.newString) + byteLength(a.oldString);
    if (bytes <= denyBytes) return { deny: false };
    return { deny: true, kind: "edit", bytes, message: buildDenyMessage("edit", bytes, denyBytes, chunkBytes) };
  }
  return { deny: false };
}

// ── 결정: append 는 기존 파일에도 의식적으로 허용 (codex R4 제안 reject·PM 70) ──────────────
// codex R4 는 "기존 파일 대형 수정을 safe_write append 로 우회 가능"을 must-fix 로 냈으나 reject 한다:
// append 는 append-only 라 기존 내용을 덮거나 자를 수 없고(훼손 불가), chunk 상한으로 절단-실패 모드도
// 없어 데이터 유실 경로가 없다. 기존파일 append 거부·생성경로 상태추적은 중단-후-재개(프로세스 재시작
// 후 이어쓰기·resume)를 파괴하는 퇴행이다. deny 정책(기존파일 재작성→edit 유도)의 목적은 *신뢰성*
// (silent truncation 방어)이지 접근통제가 아니다. 접근통제(프로젝트 밖 쓰기)는 별도로 root
// containment(assertContainedPath·realpath·leaf lstat·O_EXCL/O_NOFOLLOW)가 커널 강제로 닫는다.
//
// ── 순수 함수: safe_write 인자 검증 (fs 부작용 없이·순수) ─────────────────────
// content: 이번 chunk. mode: "create"|"append". fileExists: 대상 파일 존재 여부. chunkBytes: 상한.
// 반환: { ok:true } 또는 { ok:false, message }. 규칙 순서:
//   1) mode 가 create|append 아니면 거부  2) chunk 가 chunkBytes 초과면 거부(도구가 상한 강제)
//   3) create 인데 파일 이미 존재 → 거부(append 를 써라)  4) append 인데 파일 부재 → 거부(먼저 create).
function validateSafeWrite(content, mode, fileExists, chunkBytes) {
  if (mode !== "create" && mode !== "append") {
    return { ok: false, message: `mode 는 "create" 또는 "append" 여야 한다 (받음: ${mode}).` };
  }
  const bytes = byteLength(content);
  if (bytes > chunkBytes) {
    return {
      ok: false,
      message:
        `chunk 크기 ${bytes}B 가 상한 ${chunkBytes}B 초과 — 이 조각을 ${chunkBytes}B 이하로 ` +
        `더 잘게 나눠 여러 번 append 하라.`,
    };
  }
  if (mode === "create" && fileExists) {
    return {
      ok: false,
      message: `create 인데 파일이 이미 존재한다 — 이어쓰려면 mode="append" 를 써라.`,
    };
  }
  if (mode === "append" && !fileExists) {
    return {
      ok: false,
      message: `append 인데 파일이 없다 — 첫 조각은 mode="create" 로 써라.`,
    };
  }
  return { ok: true };
}

// ── 순수 함수: lexical root containment (레퍼런스 없이 문자열 판정) ─────────────
// p 가 root(또는 그 하위)에 있는가 — 둘 다 resolve 후 정확히 같거나 root+sep 접두인지.
function isWithinRoot(root, p) {
  const r = path.resolve(root);
  const t = path.resolve(p);
  return t === r || t.startsWith(r + path.sep);
}

// ── 순수 함수: filePath 를 root 안으로 봉쇄 해소 (모델-facing 거부·부작용 없음) ──
// 절대경로 거부(프로젝트 상대경로만) + directory 기준 resolve 후 lexical 하게 root 밖(../ 탈출)이면
// 거부. 통과 시 resolve 된 절대 경로 반환. root 미상이면 fail-closed. realpath(symlink) 방어는
// 실 파일시스템이 필요해 assertRealpathContained(아래·safeWrite 안)에서 별도로 강제한다.
function assertContainedPath(root, filePath) {
  if (typeof root !== "string" || !root) {
    throw new Error("[safe-write] 프로젝트 루트를 해소할 수 없다 — 쓰기를 거부한다.");
  }
  if (typeof filePath !== "string" || !filePath) {
    throw new Error("[safe-write] filePath 가 비었다 — 프로젝트 상대경로를 지정하라.");
  }
  if (path.isAbsolute(filePath)) {
    throw new Error(
      "[safe-write] 절대경로는 허용하지 않는다 — 프로젝트 상대경로만 써라 " +
        `(받음: ${filePath}).`,
    );
  }
  const resolved = path.resolve(root, filePath);
  if (!isWithinRoot(root, resolved)) {
    throw new Error(
      "[safe-write] 프로젝트 루트를 벗어나는 경로다(../ 등) — 루트 안 상대경로만 써라 " +
        `(받음: ${filePath}).`,
    );
  }
  return resolved;
}

// ── fs: 존재하는 최상위 조상 디렉터리 (mkdir 전 realpath 검사 대상) ────────────
function nearestExistingAncestor(p) {
  let cur = path.resolve(p);
  for (let i = 0; i < 4096; i++) {
    if (fs.existsSync(cur)) return cur;
    const parent = path.dirname(cur);
    if (parent === cur) return cur; // 파일시스템 루트 도달.
    cur = parent;
  }
  return cur;
}

// ── fs: realpath 기반 symlink 탈출 방어 (lexical 통과 후 물리 경로 재확인) ──────
// lexical containment(assertContainedPath)는 문자열만 봐 symlink 로 우회될 수 있다 — 존재하는
// 최상위 조상 디렉터리의 realpath 가 root(realpath) 안인지 + target 파일이 이미 존재하면 그 파일
// realpath 도 root 안인지 강제한다. 위반·확인불가는 fail-closed(throw). create/append 공통.
function assertRealpathContained(root, target) {
  let rootReal;
  try {
    rootReal = fs.realpathSync(root);
  } catch {
    throw new Error("[safe-write] 프로젝트 루트를 확인할 수 없다 — 쓰기를 거부한다.");
  }
  const anc = nearestExistingAncestor(target);
  let ancReal;
  try {
    ancReal = fs.realpathSync(anc);
  } catch {
    throw new Error("[safe-write] 대상 경로를 확인할 수 없다 — 쓰기를 거부한다.");
  }
  if (!isWithinRoot(rootReal, ancReal)) {
    throw new Error(
      "[safe-write] 대상 경로가 프로젝트 밖을 가리킨다(symlink 탈출?) — 쓰기를 거부한다.",
    );
  }
  if (fs.existsSync(target)) {
    let tReal;
    try {
      tReal = fs.realpathSync(target);
    } catch {
      throw new Error("[safe-write] 대상 파일을 확인할 수 없다 — 쓰기를 거부한다.");
    }
    if (!isWithinRoot(rootReal, tReal)) {
      throw new Error(
        "[safe-write] 대상 파일이 프로젝트 밖을 가리킨다(symlink 탈출?) — 쓰기를 거부한다.",
      );
    }
  }
}

// ── fs: leaf 가 symlink 면 거부 (create/append 공통·dangling 포함·명확한 메시지) ──
// lstatSync 는 symlink 를 follow 하지 않는다 — dangling symlink(가리키는 대상 부재)도 leaf 자체는
// 존재하므로 isSymbolicLink()=true 로 잡힌다(existsSync 는 follow 해 false → 이 검사가 그 갭을 닫음).
// leaf 부재(진짜 없음)면 ENOENT → OK(create 가 만든다). create 는 아래 openSync("wx")가 커널 강제로
// 이중 봉쇄(O_CREAT|O_EXCL 은 symlink leaf 에 EEXIST·TOCTOU-safe).
function assertLeafNotSymlink(target) {
  let st;
  try {
    st = fs.lstatSync(target);
  } catch {
    return; // leaf 부재 → symlink 아님 (create 가 새로 만든다).
  }
  if (st.isSymbolicLink()) {
    throw new Error(
      "[safe-write] 대상 경로의 마지막 요소가 symlink 다 — symlink 로의 쓰기는 거부한다(프로젝트 밖 탈출 방어).",
    );
  }
}

// ── fs 부작용: safe_write 한 chunk 수행 (root containment → 검증 → create/append + 누적 보고) ──
// root: 프로젝트 디렉터리(모든 쓰기의 봉쇄 경계). filePath: 프로젝트 상대경로(절대·../ 탈출 거부).
// raw fs 가 opencode write 권한계층을 우회하므로 다층 containment 로 프로젝트 밖 쓰기를 봉쇄한다:
//   ① lexical(절대·../ 거부)  ② realpath(조상 dir + 존재 target 이 root 밖 가리키면 거부)
//   ③ leaf lstat(leaf 가 symlink[dangling 포함]이면 거부)  ④ 커널 강제 open — create=openSync("wx")
//      (O_CREAT|O_EXCL·EEXIST), append=openSync(O_WRONLY|O_APPEND|O_NOFOLLOW)(symlink leaf 면 ELOOP).
//      lstat 선검사↔write 사이 symlink 교체 TOCTOU 를 커널이 닫는다. 검증 실패 시 throw(모델-facing).
function safeWrite(root, filePath, content, mode, chunkBytes) {
  const target = assertContainedPath(root, filePath); // 절대·../ 탈출 거부 + resolve.
  assertRealpathContained(root, target); // symlink 탈출 거부 (조상 dir + 존재 target realpath).
  assertLeafNotSymlink(target); // leaf symlink(dangling 포함) 거부 (create/append 공통).
  const exists = fs.existsSync(target);
  const v = validateSafeWrite(content, mode, exists, chunkBytes);
  if (!v.ok) throw new Error("[safe-write] " + v.message);
  const dir = path.dirname(target);
  if (dir) fs.mkdirSync(dir, { recursive: true });
  if (mode === "create") {
    // exclusive open (O_CREAT|O_EXCL) — leaf 가 symlink(dangling 포함)거나 이미 존재하면 커널이
    // EEXIST 로 거부한다(POSIX: O_EXCL+O_CREAT 는 symlink leaf 를 follow 하지 않고 실패). raw
    // writeFileSync 의 symlink-follow 밖-쓰기·TOCTOU 를 커널 강제로 원천 차단한다.
    let fd;
    try {
      fd = fs.openSync(target, "wx");
    } catch (e) {
      if (e && e.code === "EEXIST") {
        throw new Error(
          '[safe-write] create 대상이 이미 존재하거나 symlink 다 — 이어쓰려면 mode="append", ' +
            "프로젝트 밖 symlink 면 다른 경로를 써라.",
        );
      }
      throw e;
    }
    try {
      fs.writeSync(fd, content, null, "utf-8");
    } finally {
      fs.closeSync(fd);
    }
  } else {
    // append 도 커널 강제 O_NOFOLLOW — leaf lstat 선검사↔여기 사이 symlink 로 교체되는 TOCTOU 를
    // 닫는다(create 의 "wx" 와 동형). O_APPEND 로 이어쓰기, symlink leaf 면 ELOOP 로 거부.
    let fd;
    try {
      fd = fs.openSync(target, fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_NOFOLLOW);
    } catch (e) {
      if (e && e.code === "ELOOP") {
        throw new Error(
          "[safe-write] 대상 경로의 마지막 요소가 symlink 다 — symlink 로의 쓰기는 거부한다(프로젝트 밖 탈출 방어).",
        );
      }
      throw e;
    }
    try {
      fs.writeSync(fd, content, null, "utf-8");
    } finally {
      fs.closeSync(fd);
    }
  }
  const full = fs.readFileSync(target, "utf-8");
  const totalBytes = byteLength(full);
  // 줄 수 = 개행 개수 + (마지막 개행 없는 잔여줄 1). 빈 파일은 0.
  const totalLines = full.length === 0 ? 0 : full.split("\n").length - (full.endsWith("\n") ? 1 : 0);
  return { filePath: target, mode, wroteBytes: byteLength(content), totalBytes, totalLines };
}

// ── 순수 함수: 파일 경로 해소 (상대→directory 기준 절대·read-only 휴리스틱 전용) ──
// before-hook 이 write 대상 존재 여부(fileExists)를 heuristic 으로 볼 때만 쓴다(쓰기 아님).
// 실 쓰기(safeWrite)는 위 assertContainedPath/realpath 로 봉쇄한다.
function resolvePath(directory, filePath) {
  if (typeof filePath !== "string" || !filePath) return filePath;
  if (path.isAbsolute(filePath)) return filePath;
  return path.resolve(directory || process.cwd(), filePath);
}

// ── plugin 팩토리 커링: tool 헬퍼 주입 → opencode 팩토리 반환 ──────────────────
// ESM shim(../plugins/safe-write.js)이 `@opencode-ai/plugin` 의 tool 을 import 해 이 함수로 주입한다
// (core 가 그 패키지를 직접 require 하면 node 자가검증이 깨지므로). 반환값이 opencode autoload 가
// 호출하는 실제 팩토리(async ({directory,...}) => Hooks).
function makeSafeWritePlugin(tool) {
  return async ({ directory } = {}) => {
    const denyBytes = resolveDenyBytes(process.env);
    const chunkBytes = resolveChunkBytes(process.env);
    return {
      // ── ② 신규-대형-파일 전용 custom tool: 8KB chunk create→append ──────────
      tool: {
        safe_write: tool({
          description:
            `대형 신규 파일을 ${chunkBytes}B 이하 chunk 로 안전하게 쓰는 도구. mode="create" 로 ` +
            `첫 조각을 쓰고 mode="append" 로 이어 붙인다. 한 번에 ${chunkBytes}B 이하만 허용(초과 ` +
            `거부)·create 는 기존 파일 있으면 거부·append 는 파일 없으면 거부. 반환은 누적 바이트/라인 ` +
            `(진행 파악용). 기존 파일 대형 수정에는 이 도구 대신 edit(문자열 치환)를 써라.`,
          args: {
            filePath: tool.schema
              .string()
              .describe("쓸 파일 경로 — 프로젝트 상대경로만 (절대경로·../ 프로젝트 밖 탈출은 거부됨)"),
            content: tool.schema.string().describe(`이번 chunk 내용 (${chunkBytes}B 이하)`),
            mode: tool.schema
              .string()
              .describe('"create"(첫 조각·신규 파일) 또는 "append"(이어쓰기)'),
          },
          async execute(args, context) {
            const root = (context && context.directory) || directory;
            // root(프로젝트 디렉터리) + 모델이 준 상대 filePath 를 그대로 넘긴다 — safeWrite 가
            // 절대·../·symlink 탈출을 봉쇄한다(raw fs 가 opencode 권한계층을 우회하므로).
            const res = safeWrite(root, args.filePath, args.content, args.mode, resolveChunkBytes(process.env));
            return (
              `[safe-write] ${res.mode} ok — 이번 ${res.wroteBytes}B 썼고 누적 ` +
              `${res.totalBytes}B / ${res.totalLines}줄 (${args.filePath}). ` +
              `다음 조각은 mode="append" 로 이어 써라.`
            );
          },
        }),
      },

      // ── ① deny-and-redirect: 대형 write/edit args 를 차단하고 모델을 유도 ────
      "tool.execute.before": async (input, output) => {
        const toolName = String((input && input.tool) || "").toLowerCase();
        if (toolName !== "write" && toolName !== "edit") return;
        const args = (output && output.args) || {};
        const deny = resolveDenyBytes(process.env);
        const chunk = resolveChunkBytes(process.env); // deny 메시지 안내값(env override 반영).
        let fileExists = false;
        if (toolName === "write") {
          const fp = args.filePath || args.file_path || args.path;
          if (fp) {
            try {
              fileExists = fs.existsSync(resolvePath(directory, fp));
            } catch {
              fileExists = false; // 판정 불가 → 신규로 취급(safe_write 유도·보수적).
            }
          }
        }
        const verdict = checkOversizeWrite(toolName, args, deny, fileExists, chunk);
        if (verdict.deny) throw new Error(verdict.message);
      },
    };
  };
}

// CommonJS export — node 자가검증(require)·ESM shim(../plugins/safe-write.js) 양쪽 소비.
// opencode 는 이 모듈을 직접 로드하지 않는다(plugins/ 만 스캔) — 얇은 ESM shim 이 tool 을 주입해
// 만든 팩토리 하나만 named-export 해 로드 규약(export=단일 함수·실측 T-0283)을 만족한다.
module.exports = {
  makeSafeWritePlugin,
  // 순수 결정 로직 (테스트·자가검증용 export).
  byteLength,
  resolvePositiveInt,
  resolveDenyBytes,
  resolveChunkBytes,
  buildDenyMessage,
  checkOversizeWrite,
  validateSafeWrite,
  isWithinRoot,
  assertContainedPath,
  nearestExistingAncestor,
  assertRealpathContained,
  assertLeafNotSymlink,
  safeWrite,
  resolvePath,
  DENY_BYTES,
  CHUNK_BYTES,
  DENY_BYTES_ENV,
  CHUNK_BYTES_ENV,
};
