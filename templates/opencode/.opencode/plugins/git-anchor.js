// opencode raw git cwd-anchor 가드 진입점 — plugins/ loader에는 팩토리 하나만 노출한다.
import core from "../lib/git-anchor-core.cjs";

export const GitAnchorPlugin = core.GitAnchorPlugin;
