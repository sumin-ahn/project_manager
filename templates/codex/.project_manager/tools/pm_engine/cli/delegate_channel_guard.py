"""Native delegation-channel guard public CLI (layer: cli).

``decide`` is the single decision truth shared by harness adapters.  The
argument-based ``decide`` CLI always emits one JSON line and exits successfully,
including usage and infrastructure failures.  With no arguments, the historical
Claude ``PreToolUse`` stdin/stdout hook protocol remains intact.
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
from pm_engine.domain import delegate_channel_rules, home
from pm_engine.infra import conf_store
from pm_engine.infra import console


class _CliUsageError(Exception):
    """Internal argparse failure converted to the fail-open JSON contract."""


class _FailOpenArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliUsageError(message)


def _parse_decide_args(argv: list[str]) -> argparse.Namespace:
    parser = _FailOpenArgumentParser(prog="delegate_channel_guard.py", add_help=False)
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


def _cli_fail_open(reason: str) -> dict[str, str]:
    detail = " ".join(str(reason).splitlines()).strip() or "unknown error"
    return delegate_channel_rules._result(
        "allow",
        f"[delegate-channel/warn] decide CLI 실패({detail}) — 통과(fail-open)",
    )


def _write_machine_line(text: str, stream: TextIO, seam) -> None:
    """호스트가 파싱하는 한 줄을 콘솔 코덱 전환과 무관하게 UTF-8 로 내보낸다.

    ``seam`` 은 ``main`` 이 넘기는 ``pm_engine.infra.console`` 모듈이다. 가드 자신이 죽은
    fail-open 경계는 ``None`` 을 받는다 — 그 자리는 ``configure_console_utf8`` 이 아예 돌지
    않은 상태라 텍스트 레이어가 로케일 코덱(Windows cp949·errors=strict)일 수 있고, 한글과
    em dash 가 실린 엔벨로프가 통째로 ``UnicodeEncodeError`` 로 사라진다. 그래서 형제 없이도
    같은 보장을 준다 — ``.buffer`` 로 UTF-8 bytes 를 직접 쓰고, ``.buffer`` 가 없는 스트림
    (테스트 캡처·``io.StringIO``)만 텍스트 write 로 내려간다.
    """
    if seam is not None:
        seam.write_machine_line(text, stream=stream)
        return
    line = text + "\n"
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(line)
        return
    try:
        # 텍스트 레이어에 남은 출력이 bytes 뒤로 밀리지 않게 먼저 비운다(seam 과 같은 규율).
        stream.flush()
    except (OSError, ValueError):
        pass
    buffer.write(line.encode("utf-8"))
    buffer.flush()


def _run_decide_cli(
    home, argv: list[str],
    *,
    stdout: TextIO,
    config_loader: Callable[[home.Home], Mapping[str, str]],
    machine_seam=None,
) -> int:
    """Run the non-stdin CLI; every path writes exactly one JSON line and rc0."""
    args = _parse_decide_args(argv)
    result = delegate_channel._decide_agent_input(
        home, args.role, args.harness, tier=args.tier, cwd=args.cwd,
        config_loader=config_loader,
    )
    _write_machine_line(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        stdout,
        machine_seam,
    )
    return 0


def _report_fail_open(exc: BaseException, stderr: TextIO) -> None:
    detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
    message = (
        f"[delegate-channel/warn] 가드 판정 실패({type(exc).__name__}: {detail}) "
        "— 통과(fail-open)"
    )
    # 콘솔 인코딩(CP949 등)이 UTF-8 재설정 전에 깨진 경로에서는 ASCII 로 바꿔 다시 쓴다.
    # 그 쓰기도 실패하면 전파한다(stdout 엔벨로프는 이미 쓰였다).
    try:
        print(message, file=stderr)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"), file=stderr)


def _codex_hook_fail_open(
    exc: BaseException, hook_event_name: str = "PreToolUse"
) -> dict[str, object]:
    """Emit a complete warning envelope whose reason names the failure.

    Both hook events share the shape: an unrunnable guard is reported, never
    degraded into a partial object that the host cannot read.
    """
    detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
    reason = (
        f"[delegate-channel/warn] Codex 가드 판정 실패({type(exc).__name__}: {detail}) "
        f"— {hook_event_name} 통과(fail-open)"
    )
    return codex_envelope._validated_codex_envelope({
        "systemMessage": reason,
        "suppressOutput": delegate_channel_rules.CODEX_WARNING_SUPPRESS_OUTPUT,
    })


def _read_stdin_bytes(stream: TextIO) -> bytes:
    """Read the hook payload as the exact bytes the child guard must receive."""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            return buffer.read()
        except (AttributeError, ValueError, OSError):
            pass
    data = stream.read()
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


def main(
    home: home.Home,
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
    state_dir: Path | None = None,
) -> int:
    """Run decide CLI when argv is non-empty, otherwise the legacy Claude hook."""
    try:
        output_stream = sys.stdout if stdout is None else stdout
        # ``None`` preserves the historical direct-call hook seam used by tests
        # and wrappers; the module entry point passes process argv explicitly.
        cli_argv = list(argv or [])
        if cli_argv and cli_argv[0] == "supervise":
            # ``supervise <event> <child argv…>`` — 어댑터가 인용 없는 토큰만 넘긴다.
            if len(cli_argv) < 2:
                raise _CliUsageError(
                    "supervise 는 <event> 와 가드 실행 커맨드를 받는다"
                )
            input_stream = sys.stdin if stdin is None else stdin
            payload_bytes = _read_stdin_bytes(input_stream)
            result = codex_hook.supervise_codex_hook(
                home, cli_argv[1],
                cli_argv[2:],
                payload_bytes=payload_bytes,
                state_dir=state_dir,
            )
            # 이 줄은 PowerShell/bash 캡처를 거쳐 호스트로 간다 — seam 이 콘솔 코덱과
            # 무관하게 UTF-8 로 쓰고, ASCII 이스케이프는 종전 바이트를 유지한다.
            _write_machine_line(
                json.dumps(result, separators=(",", ":")),
                output_stream,
                console,
            )
            return 0
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
            if decision is not None:
                result = codex_envelope._codex_decision_envelope(decision)
                result = codex_hook.observe_codex_pretooluse(
                    home, payload, result, decision=decision, state_dir=state_dir
                )
                _write_machine_line(
                    json.dumps(
                        codex_envelope._validated_codex_envelope(result),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    output_stream,
                    console,
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
            _write_machine_line(
                json.dumps(codex_envelope._validated_codex_envelope(result), ensure_ascii=False),
                output_stream,
                console,
            )
            return 0
        if cli_argv:
            return _run_decide_cli(
                home, cli_argv,
                stdout=output_stream,
                config_loader=config_loader,
                machine_seam=console,
            )

        input_stream = sys.stdin if stdin is None else stdin
        payload = json.loads(input_stream.read())
        if not isinstance(payload, Mapping):
            raise TypeError("hook input JSON must be an object")
        result = delegate_channel.evaluate_hook(home, payload, config_loader=config_loader)
        if result is not None:
            _write_machine_line(
                json.dumps(result, ensure_ascii=False),
                output_stream,
                console,
            )
        return 0
    except BaseException as exc:
        # 호스트 오류는 기존 fail-open envelope로 보고한다.
        output_stream = sys.stdout if stdout is None else stdout
        # 가드 자신이 죽은 경계의 emit 은 seam 에 기대지 않는다 — 콘솔 설정 없이 패키지 main 을
        # 직접 부른 경우도 여기로 오므로, 아래 emit 은 seam 없이 UTF-8 bytes 를 쓰는 폴백을 탄다.
        machine_seam = None
        cli_argv = list(argv or [])
        if cli_argv and cli_argv[0] == "supervise":
            # supervisor 자신이 죽어도 호스트는 완전한 엔벨로프 한 줄을 받는다.
            event = cli_argv[1] if len(cli_argv) > 1 else delegate_channel_rules.CODEX_SUPERVISOR_EVENTS[0]
            if event not in delegate_channel_rules.CODEX_SUPERVISOR_EVENTS:
                event = delegate_channel_rules.CODEX_SUPERVISOR_EVENTS[0]
            detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
            _write_machine_line(
                json.dumps(
                    codex_hook._codex_supervisor_fallback(
                        event, f"{type(exc).__name__}: {detail}"
                    ),
                    separators=(",", ":"),
                ),
                output_stream,
                machine_seam,
            )
            _report_fail_open(exc, sys.stderr if stderr is None else stderr)
            return 0
        if cli_argv and cli_argv[0] in {"codex-hook", "codex-subagent-observe"}:
            hook_event_name = (
                "SubagentStart"
                if cli_argv[0] == "codex-subagent-observe"
                else "PreToolUse"
            )
            _write_machine_line(
                json.dumps(
                    _codex_hook_fail_open(exc, hook_event_name),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                output_stream,
                machine_seam,
            )
            _report_fail_open(exc, sys.stderr if stderr is None else stderr)
            return 0
        if cli_argv:
            _write_machine_line(
                json.dumps(
                    _cli_fail_open(f"{type(exc).__name__}: {exc}"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                output_stream,
                machine_seam,
            )
            return 0
        _report_fail_open(exc, sys.stderr if stderr is None else stderr)
        return 0
