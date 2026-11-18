// opencode 판단 원칙 recall 진입점: plugins/ 적재기에는 플러그인 정의 하나만 default export 한다(git-check 와 같은 규약).
import core from "../lib/principle-recall-core.cjs";

export default core.PrincipleRecallPlugin;
