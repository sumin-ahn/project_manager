// opencode raw git cwd 대상 저장소 가드 진입점 — plugins/ loader에는 팩토리 하나만 노출한다.
import core from "../lib/git-check-core.cjs";

export const GitCheckPlugin = core.GitCheckPlugin;
