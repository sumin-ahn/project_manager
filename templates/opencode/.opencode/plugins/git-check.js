// opencode git 명령 검사 플러그인 진입점: plugins/ 적재기에는 플러그인 정의 하나만 default export 한다.
import core from "../lib/git-check-core.cjs";

export default core.GitCheckPlugin;
