"""Native delegation-channel guard public CLI (layer: cli).

``decide`` is the single decision truth shared by harness adapters.  Every mode
writes one JSON line when it reaches a decision and lets any failure propagate
(traceback, rc≠0); it never turns a failure into a pass.  The adapter boundary
that started the process decides what the host sees.  With no arguments this is
the Claude ``PreToolUse`` stdin/stdout hook protocol.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from pm_engine.app import codex_hook
from pm_engine.app import delegate_channel
from pm_engine.domain import codex_envelope
from pm_engine.domain import home
from pm_engine.infra import conf_store
from pm_engine.infra import console


class _CliUsageError(Exception):
    """Usage error of the argument-based CLI (raised, never converted into a verdict)."""


def _parse_decide_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="delegate_channel_guard.py", add_help=False)
    parser.add_argument("command", choices=("decide",))
    parser.add_argument("--role", required=True)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--tier")
    parser.add_argument("--cwd")
    parser.add_argument("--help", action="store_true")
    args = parser.parse_args(argv)
    if args.help:
        raise _CliUsageError(
            "usage: delegate_channel_guard.py decide --role ROLE --harness HARNESS [--tier TIER] [--cwd ABS]"
        )
    if args.cwd and not delegate_channel._is_absolute_path(args.cwd):
        raise _CliUsageError("--cwd 는 절대경로여야 한다")
    return args


def _run_decide_cli(
    home, argv: list[str],
    *,
    stdout: TextIO,
    config_loader: Callable[[home.Home], Mapping[str, str]],
) -> int:
    """Run the non-stdin ``decide`` CLI and write its one-line verdict JSON."""
    args = _parse_decide_args(argv)
    result = delegate_channel._decide_agent_input(
        home, args.role, args.harness, tier=args.tier, cwd=args.cwd,
        config_loader=config_loader,
    )
    console.write_machine_line(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")), stream=stdout,
    )
    return 0


def main(
    home: home.Home,
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
    state_dir: Path | None = None,
) -> int:
    """Run the argument CLI when argv is non-empty, otherwise the Claude hook.

    Failures propagate (traceback, rc≠0) in every mode.  The adapter boundary that
    started this process decides what the host sees: the Codex dispatcher, the
    opencode plugin, or for the Claude hook the Claude driver ``pm_orch_claude.py``.
    """
    output_stream = sys.stdout if stdout is None else stdout
    cli_argv = list(argv or [])
    if cli_argv and cli_argv[0] == "codex-hook":
        if cli_argv != ["codex-hook"]:
            raise _CliUsageError("codex-hook 는 추가 인자를 받지 않는다")
        input_stream = sys.stdin if stdin is None else stdin
        payload = json.loads(input_stream.read())
        if not isinstance(payload, Mapping):
            raise TypeError("hook input JSON must be an object")
        decision = delegate_channel._evaluate_codex_decision(
            home, payload, config_loader=config_loader
        )
        result = codex_envelope._codex_decision_envelope(decision)
        result = codex_hook.observe_codex_pretooluse(
            home, payload, result, decision=decision, state_dir=state_dir
        )
        console.write_machine_line(
            json.dumps(
                codex_envelope._validated_codex_envelope(result),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            stream=output_stream,
        )
        return 0
    if cli_argv and cli_argv[0] == "codex-subagent-observe":
        if cli_argv != ["codex-subagent-observe"]:
            raise _CliUsageError("codex-subagent-observe 는 추가 인자를 받지 않는다")
        input_stream = sys.stdin if stdin is None else stdin
        payload = json.loads(input_stream.read())
        if not isinstance(payload, Mapping):
            raise TypeError("hook input JSON must be an object")
        result = codex_hook.observe_codex_subagent_start(home, payload, state_dir=state_dir)
        console.write_machine_line(
            json.dumps(codex_envelope._validated_codex_envelope(result), ensure_ascii=False),
            stream=output_stream,
        )
        return 0
    if cli_argv:
        return _run_decide_cli(
            home, cli_argv,
            stdout=output_stream,
            config_loader=config_loader,
        )
    input_stream = sys.stdin if stdin is None else stdin
    payload = json.loads(input_stream.read())
    if not isinstance(payload, Mapping):
        raise TypeError("hook input JSON must be an object")
    result = delegate_channel.evaluate_hook(home, payload, config_loader=config_loader)
    if result is not None:
        console.write_machine_line(json.dumps(result, ensure_ascii=False), stream=output_stream)
    return 0
