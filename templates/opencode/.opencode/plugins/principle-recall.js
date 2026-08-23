// opencode 판단 원칙 recall 진입점 — plugins/ loader 에는 팩토리 하나만 노출한다(git-anchor 동형 규약).
import core from "../lib/principle-recall-core.cjs";

export const PrincipleRecallPlugin = core.PrincipleRecallPlugin;
