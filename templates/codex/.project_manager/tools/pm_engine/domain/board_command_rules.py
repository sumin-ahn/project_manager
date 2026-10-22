"""보드 명령 분류 (층: domain)."""
from __future__ import annotations

from collections.abc import Sequence


_MUTATION_SUBCOMMANDS: frozenset[str] = frozenset({'new', 'promote', 'claim', 'complete', 'discard', 'reopen', 'block', 'unclaim', 'unblock', 'section-add', 'tier', 'design', 'init', 'migrate-identity', 'promote-scope', 'reid', 'refresh', 'verified-at-repin', 'idea new', 'idea promote', 'idea kill', 'prefix rename', 'prefix strip', 'prefix merge', 'prefix delete', 'cluster new'})

_READ_SUBCOMMANDS: frozenset[str] = frozenset({'list', 'show', 'lint', 'tier-signals', 'idea list', 'prefix list', 'cluster show'})

_READ_PM_INPUTS_BY_SUBCOMMAND: dict[str, str] = {'list': 'board·areas·local.conf·lease', 'show': 'board', 'lint': 'board·areas·wiki(decisions·ideas·specs·domain·current-truth)·hooks·local.conf', 'tier-signals': 'board', 'idea list': 'ideas', 'prefix list': 'board', 'cluster show': 'board'}

_SIDECAR_SUBCOMMANDS: frozenset[str] = frozenset({'regression', 'livegate', 'git-anchor', 'git-grant', 'git-tool-input'})

_SUBCOMMAND_GROUP_DESTS: dict[str, str] = {'idea': 'idea_cmd', 'prefix': 'prefix_cmd', 'cluster': 'cluster_cmd'}

def _board_mutation_subcommand(args: Sequence[str]) -> str:
    """기존 board mutation 분류 키와 같은 점표기 subcommand를 argv에서 얻는다."""
    if not args:
        return ''
    command = args[0]
    if command in {'idea', 'prefix'} and len(args) >= 2:
        command = f'{command} {args[1]}'
    return command if command in _MUTATION_SUBCOMMANDS else ''
