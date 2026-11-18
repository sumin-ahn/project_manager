// opencode 어댑터: ctx checkpoint 플러그인 진입점(얇은 ESM 진입점).
//
// opencode 2.x 적재 규약(실측 2.3.0): `.opencode/plugins/` 의 각 파일은 default export 로
// `{ id, setup }` 플러그인 정의 하나를 내야 한다(아니면 "Plugin must export a default definition" 로 적재 실패).
// 처리기·헬퍼·상수는 plugins/ *바깥* CJS 모듈(`../lib/ctx-guard-core.cjs`)에 두고 여기서는 정의만 다시 낸다
// opencode 는 plugins/ 만 스캔하므로 lib/ 는 적재하지 않고, node 자가검증 시험은 그 CJS 를 require 한다.
import core from "../lib/ctx-guard-core.cjs";

export default core.CtxGuardPlugin;
