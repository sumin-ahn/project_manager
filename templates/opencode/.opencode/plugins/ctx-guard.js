// opencode 어댑터 — ctx checkpoint plugin 진입점 (얇은 ESM shim · T-0551 · T-0283 로드 fix).
//
// opencode plugin 로드 규약(실측 T-0283 · opencode 1.17.18): `.opencode/plugins/` 안 각 파일의
// export 를 순회(Object.values)해 *모두 함수*이길 요구하고, 그 각각을 플러그인 팩토리로 호출한다.
//   - CJS `module.exports`(객체든 단일 함수든)는 거부된다 — `error="Plugin export is not a function"`
//     (bun 이 만든 CJS import namespace 를 opencode 로더가 받지 않음·form1~4 실측).
//   - 비함수 export(상수) 하나라도 있으면 로드 실패하고, 헬퍼 함수까지 각각 플러그인으로 오인·호출한다
//     (form7 실측).
// 따라서 이 파일은 **ESM 으로 팩토리 하나만 named-export** 하고, 순수 헬퍼·상수·팩토리 본체는
// plugins/ *바깥* CJS 모듈(`../lib/ctx-guard-core.cjs`)에 둔다 — opencode 는 plugins/ 만 스캔하므로
// lib/ 는 로드하지 않고(mis-detect 없음), node 자가검증 test 는 그 CJS 를 require 해 순수함수를 검증한다.
// (ESM `.js` 는 bun 이 syntax 로 자동 감지 — package.json `type:module` 불요·form8 실측.)
import core from "../lib/ctx-guard-core.cjs";

// opencode autoload 대상 = 단일 함수 export(팩토리). 헬퍼/상수는 여기서 export하지 않는다(위 규약).
export const CtxGuardPlugin = core.CtxGuardPlugin;
