"""위임 채널 처방 문구 (층: app)."""
from __future__ import annotations

from pm_engine.domain import delegate_channel_rules
from pm_engine.infra.harness import delegate_env
from pm_engine.app.session import relay



def _remediation(role: str, tier: str) -> str:
    if role == delegate_channel_rules._REVIEW_ROLE:
        return (
            f"{relay._runtime_skill_entry('pm-dev-delegate')} 스킬로 위임하라 "
            f"(실행형 처방: backbone `{delegate_env._guard_prescribed_interpreter()} "
            f".project_manager/tools/pm_delegate.py --role {role} "
            f"--cluster {delegate_channel_rules._CLUSTER_PLACEHOLDER} --cwd {delegate_channel_rules._WORKTREE_PLACEHOLDER}`)"
        )
    tier_arg = " --tier hard" if tier == "hard" else ""
    prompt_file = delegate_channel_rules._CODEX_PROMPT_DIR / f"manual-{role}-{tier}-prompt.md"
    return (
        f"{relay._runtime_skill_entry('pm-dev-delegate')} 스킬로 위임하라 "
        f"(실행형 처방: 1) 프롬프트를 파일로 저장한 뒤"
        f"(경로: `{prompt_file.as_posix()}`) "
        f"2) backbone `{delegate_env._guard_prescribed_interpreter()} .project_manager/tools/pm_delegate.py "
        f"--role {role}{tier_arg} --prompt-file {prompt_file.as_posix()} "
        f"--cwd {delegate_channel_rules._WORKTREE_PLACEHOLDER} --ticket {delegate_channel_rules._TICKET_PLACEHOLDER}`)"
    )
