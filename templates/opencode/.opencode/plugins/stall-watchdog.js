// opencode 어댑터 — stall-watchdog plugin 진입점 (얇은 ESM shim).
//
// opencode plugin 로드 규약(safe-write shim 과 같은 실측): `.opencode/plugins/` 안 각 파일의
// export 를 순회해 *모두 함수*이길 요구하고, 그 각각을 플러그인 팩토리로 호출한다. CJS module.exports·
// 비함수 export 는 로드 거부다. 따라서 이 파일은 **팩토리 하나만 named-export** 하고 순수 헬퍼·상수·
// 팩토리 본체는 plugins/ *바깥* CJS 모듈(`../lib/stall-watchdog-core.cjs`)에 둔다(safe-write 와
// 동일 이중구조 — opencode 는 plugins/ 만 스캔하고 node 자가검증은 그 CJS 를 require 한다).
//
// client 주입: SDK client 는 shim import 시점엔 없고 opencode 가 팩토리 호출 시 ctx 로 건네므로
// (`{client, directory, worktree}`), 이 shim 은 받은 ctx.client 로 core 의 커링 팩토리를 만들어
// 같은 ctx 로 다시 호출한다(재위임). custom tool 이 없어 `@opencode-ai/plugin` import 도 불필요.
import core from "../lib/stall-watchdog-core.cjs";

// opencode autoload 대상 = 단일 함수 export(팩토리). 헬퍼/상수는 여기서 export하지 않는다(위 규약).
export const StallWatchdogPlugin = (ctx) =>
  core.makeStallWatchdogPlugin(ctx && ctx.client)(ctx);
