// opencode 어댑터 — 대용량 write/edit 신뢰성 가드 plugin 진입점 (얇은 ESM shim).
//
// opencode plugin 로드 규약(실측 opencode 1.17.18): `.opencode/plugins/` 안 각 파일의
// export 를 순회(Object.values)해 *모두 함수*이길 요구하고, 그 각각을 플러그인 팩토리로 호출한다.
//   - CJS `module.exports`(객체든 단일 함수든)는 거부된다 — `error="Plugin export is not a function"`.
//   - 비함수 export(상수) 하나라도 있으면 로드 실패하고, 헬퍼 함수까지 각각 플러그인으로 오인·호출한다.
// 따라서 이 파일은 **ESM 으로 팩토리 하나만 named-export** 하고, 순수 헬퍼·상수·팩토리 본체는
// plugins/ *바깥* CJS 모듈(`../lib/safe-write-core.cjs`)에 둔다 — opencode 는 plugins/ 만 스캔하므로
// lib/ 는 로드하지 않고, node 자가검증 test 는 그 CJS 를 require 해 순수함수를 검증한다.
// (ESM `.js` 는 bun 이 syntax 로 자동 감지 — package.json `type:module` 불요·form8 실측.)
//
// 커스텀 도구(safe_write)는 `@opencode-ai/plugin` 의 tool 헬퍼(+zod schema)가 필요하다. core 는 그
// 패키지를 직접 require 하지 않고(그러면 node 자가검증이 깨진다·그 패키지는 opencode 런타임에만 설치),
// 여기서 tool 을 import 해 `makeSafeWritePlugin(tool)` 로 주입한다(팩토리 커링). 반환값 = 실제 팩토리.
import { tool } from "@opencode-ai/plugin";
import core from "../lib/safe-write-core.cjs";

// opencode autoload 대상 = 단일 함수 export(팩토리). 헬퍼/상수는 여기서 export하지 않는다(위 규약).
export const SafeWritePlugin = core.makeSafeWritePlugin(tool);
