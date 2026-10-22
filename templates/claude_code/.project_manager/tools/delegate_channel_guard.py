#!/usr/bin/env python3
"""Harness-neutral native delegation-channel decision core and Claude hook.

``decide`` is the single decision truth shared by harness adapters.  The
argument-based ``decide`` CLI always emits one JSON line and exits successfully,
including usage and infrastructure failures.  With no arguments, the historical
Claude ``PreToolUse`` stdin/stdout hook protocol remains intact.
"""

from __future__ import annotations


import argparse
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO


_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.domain import codex_envelope
from pm_engine.infra import codex_observation
from pm_engine.infra import conf_store
from pm_engine.app import delegate_channel
from pm_engine.domain import delegate_channel_rules, home
from pm_engine.infra import delegate_config
from pm_engine.infra.harness import delegate_env
from pm_engine.infra import native_agent_card
from pm_engine.infra import pm_paths
from pm_engine.infra import console  # noqa: E402
from pm_engine.infra.harness import contract  # noqa: E402


# baked stamp. 소비처는 이 값을 자기 rev와 대조해 부분 동기된 구 사본을 사용하기 전에
# 명시적인 sibling-skew 오류로 막는다.
ENGINE_REV = "v1.8.1"


# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "hook_fail_open": (
        "PreToolUse/decide 공용 경계는 가드 자신의 고장(사본 skew 포함)으로 정상 위임을 막지 "
        "않는 것이 계약이다 — 훅은 skew 진단(pm-update 처방이 담긴 예외 문구)을 stderr 한 줄로 "
        "남기고, CLI 는 allow JSON 사유로 남겨 rc0 통과한다. 여기서 fail-loud 로 올리면 부분 "
        "동기 하나가 native 위임을 락아웃한다"
    ),
    "observation_append_fail_open": (
        "관측 append 는 기록일 뿐 차단 판정이 아니다 — 장부 쓰기 실패(사본 skew 포함)로 이미 "
        "내려진 allow/deny 를 뒤집거나 훅을 죽이면 정상 위임이 막힌다. 흡수하되 조용하지 않게 "
        "경고 문구(matcher drift 관측 불완전)를 결과 envelope 에 실어 표면화한다"
    ),

}


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지 (fail-soft 흡수 식별)."""
    return getattr(exc, "_engine_rev_skew", False)


def _absorb_engine_rev_skew_for_recovery(exc, boundary: str) -> bool:
    """훅 fail-open 경계가 marked skew 를 의도적으로 흡수했음을 표시한다 (사유 등록 필수).

    반환값으로 일반 실패와 사본 불일치를 구분한다 — 흡수는 하되 조용하지는 않다
    (additional_reviewer 동형·self-contained 복제)."""
    reason = _ENGINE_REV_SKEW_RECOVERY_REASONS.get(boundary, "").strip()
    if not reason:
        raise ValueError(f"등록되지 않았거나 사유가 빈 복구 경계: {boundary!r}")
    return _is_engine_rev_skew(exc)


def _claude_native_model_warning(home, role: str, tier: str, model: str) -> str:
    """Return an allow+loud warning for card drift, otherwise an empty string."""
    relative = native_agent_card.CLAUDE_NATIVE_AGENT_CARDS.get((role, tier))
    shown_path = relative.as_posix() if relative is not None else "(명시 매핑 없음)"
    try:
        card_model, relative = native_agent_card._read_claude_native_agent_model(home, role, tier)
    except Exception as exc:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        return (
            f"[delegate-channel/warn] Claude native model 검사 실패"
            f"(role={role}, tier={tier}, conf_model={model}, card={shown_path}, "
            f"error={type(exc).__name__}: {detail}) — native 통과(fail-open)"
        )
    if delegate_channel_rules._is_unrendered_model_token(card_model):
        # 카드가 아직 렌더되지 않았다 — 렌더 소스 트리(프레임워크 루트·templates)이거나 채택자
        # 렌더가 실패한 상태다. 이건 conf 를 고칠 일이 아니라 `pm-update` 로 카드를 다시 렌더할
        # 일이므로 "불일치" 와 다른 처방을 낸다. 판정은 여전히 fail-open.
        return (
            f"[delegate-channel/warn] Claude native model 카드 미렌더"
            f"(role={role}, tier={tier}, conf_model={model}, "
            f"card={relative.as_posix()}, card_model={card_model}) — "
            "카드가 렌더 소스 상태다: 채택자 트리면 `pm-update` 로 카드를 다시 렌더하라. "
            "native 통과(fail-open)"
        )
    if card_model != model:
        return (
            f"[delegate-channel/warn] Claude native model 불일치"
            f"(role={role}, tier={tier}, conf_model={model}, "
            f"card={relative.as_posix()}, card_model={card_model}) — "
            "native 통과(fail-open)"
        )
    return ""


def _delegate_is_enabled(conf: Mapping[str, object]) -> bool:
    """위임 마스터 스위치 — **키 부재·빈값은 허용**(기본 ON)이고 명시 거부만 차단한다.

    빈값을 거부로 읽으면 `delegate.enabled=` 한 줄이 위임 전체를 조용히 끈다 — conf 파싱의
    "마지막 값이 비면 미설정" 의미를 그대로 이어받아 기본값으로 간다.
    엔진(`pm_delegate._is_enabled`)과 같은 기본값·같은 참 낱말 집합이다.
    """
    raw = str(conf.get(delegate_channel_rules._DELEGATE_SWITCH_KEY, "")).strip().lower()
    if not raw:
        return True
    return raw in ("true", "1", "yes", "on")


def decide(
    home, role: object,
    tier: object,
    conf: Mapping[str, object],
    self_harness: object,
) -> dict[str, str]:
    """Apply decision-table rows 0 through 5 without a hook-specific envelope.

    Row 0.5 (master switch) sits between name normalisation and mapping
    resolution: ``delegate.enabled=false`` denies every role spawn regardless of
    channel.  The guard's own failures stay fail-open — a broken guard must not
    lock delegation out, because the recovery path itself needs delegation.
    """
    known_harnesses = delegate_env._known_harnesses()
    self_name = self_harness.strip().lower() if isinstance(self_harness, str) else ""

    # Row 0: a broken/misidentified adapter must never false-deny delegation.
    if not self_name or self_name not in known_harnesses:
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/warn] self_harness={self_name or '(empty)'} 미상 — 통과(fail-open)",
        )

    role_name = role.strip() if isinstance(role, str) else ""
    tier_name = tier.strip().lower() if isinstance(tier, str) else ""

    # Row 1: unknown names are recorded instead of becoming a silent no-op.
    # ``record`` is intentionally not ``warn``: adapters reserve user-facing
    # warnings/system-context injection for decision infrastructure failures.
    if role_name not in delegate_channel_rules.DELEGATE_ROLES or tier_name not in delegate_channel_rules.DELEGATE_TIERS:
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/record] 역할/티어 정규화 실패({role_name or '(empty)'}/{tier_name or '(empty)'}) — 통과(fail-open)",
        )
    if tier_name == "hard" and role_name != "developer":
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/record] 미지원 역할/티어({role_name}/{tier_name}) — 통과(fail-open)",
        )
    if not isinstance(conf, Mapping):
        raise TypeError("delegate config must be a mapping")

    # Row 0.5: the master switch is decided **before** any mapping resolution.
    # ``delegate.enabled`` is channel-agnostic (default on), so an explicit off
    # denies native and cross alike — a mapping-shaped remediation would be the
    # wrong prescription because the mapping is not what is blocking.
    if not _delegate_is_enabled(conf):
        return delegate_channel_rules._result(
            "deny",
            f"[delegate-channel/deny] 위임이 꺼져 있습니다 — {native_agent_card._ENABLED_REMEDIATION}",
        )

    if delegate_channel_rules._delegate_mapping_config_is_absent(conf):
        return delegate_channel_rules._result("allow", "[delegate-channel/allow] 역할 매핑 미설정")
    mapping_unset = delegate_channel_rules._mapping_is_unset(role_name, tier_name, conf)
    harness, model, resolution_error = delegate_config._resolved_mapping(role_name, tier_name, conf)

    # The engine rejects an absent/incomplete profile (hard included) instead
    # of inheriting another tier.  Keep native available because this guard's
    # infrastructure boundary is fail-open, but surface the same engine fact
    # and never prescribe an execution-impossible ``--tier hard`` redirect.
    if resolution_error:
        channel = "record" if mapping_unset else "warn"
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/{channel}] 설정 해소 실패"
            f"({resolution_error}) — native 통과(fail-open)",
        )

    # Row 3: no configured destination leaves native delegation available.
    if not harness:
        return delegate_channel_rules._result("allow", "[delegate-channel/allow] 역할 harness 미설정")

    # Row 4: the native surface matches configuration.
    if harness == self_name:
        if self_name == "claude":
            warning = _claude_native_model_warning(home, role_name, tier_name, model)
            if warning:
                return delegate_channel_rules._result(
                    "allow", warning, harness=harness, model=model
                )
        return delegate_channel_rules._result(
            "allow",
            f"[delegate-channel/allow] conf 와 native harness 일치({harness})",
            harness=harness,
            model=model,
        )

    # Row 5: cross-harness native spawn redirects to the ledgered CLI.
    shown_model = model or "(model 미설정)"
    return delegate_channel_rules._result(
        "deny",
        f"[delegate-channel/deny] conf 는 {harness}/{shown_model} — "
        f"{delegate_channel._remediation(role_name, tier_name)}",
        harness=harness,
        model=model,
    )


def _is_absolute_path(value: str) -> bool:
    """Whether the hook-supplied cwd is absolute **by the running platform's rules**.

    The payload comes from the host process on this machine, so the rule is the
    local one: POSIX needs a leading ``/`` while Windows needs a drive or UNC
    root (``C:\\work`` is absolute there, ``/work`` is drive-relative and
    ``C:\\work`` is not absolute on POSIX).  Anything else keeps the caller on
    the measured fail-open path instead of prescribing an unpastable cwd.  Kept
    as one injectable seam so the other platform's branch is testable here.
    """
    return Path(value).is_absolute()


def _rendered_prescription_token(value: str) -> str:
    """Quote one prescription argument with the engine's single shell-render seam.

    The prescription is pasted into the operator's shell, so POSIX quoting on
    Windows turns ``--cwd`` and its path into literal arguments and the command
    fails.  ``pm_delegate`` owns the platform rules; this guard never
    assembles quoting of its own.
    """
    return delegate_env.render_shell_token(
        value, windows=contract.running_on_windows()
    )


def _materialize_cwd(result: Mapping[str, str], cwd: object | None) -> dict[str, str]:
    """Replace only the known worktree placeholder with a concrete adapter cwd."""
    materialized = dict(result)
    if not isinstance(cwd, str) or not cwd.strip():
        return materialized
    reason = materialized["reason"]
    if delegate_channel_rules._WORKTREE_PLACEHOLDER not in reason:
        # Allow rows have no prescription; keep the engine load off that path.
        return materialized
    materialized["reason"] = reason.replace(
        delegate_channel_rules._WORKTREE_PLACEHOLDER, _rendered_prescription_token(cwd.strip())
    )
    return materialized


def evaluate_hook(
    home, payload: Mapping[str, object],
    *,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, object] | None:
    """Return a Claude deny/warning envelope, or ``None`` for a quiet allow."""
    tool_name = payload.get("tool_name")
    if tool_name != "Agent":
        return None

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, Mapping):
        return None
    normalized = delegate_channel_rules.normalize_agent_name(tool_input.get("subagent_type"), "claude")
    if normalized is None:
        return None
    role, tier = normalized

    conf = config_loader(home)
    result = decide(home, role, tier, conf, "claude")
    if result["verdict"] != "deny" and not result["reason"].startswith(
        "[delegate-channel/warn]"
    ):
        return None
    result = _materialize_cwd(result, payload.get("cwd"))
    if result["verdict"] != "deny":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": result["reason"],
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": result["reason"],
        }
    }


def _codex_observation_failure(
    result: Mapping[str, object], exc: BaseException
) -> dict[str, object]:
    detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
    warning = (
        f"[delegate-channel/warn] Codex 관측 기록 실패({type(exc).__name__}: {detail}); "
        "차단 판정은 유지하고 matcher drift 관측은 불완전함"
    )
    # A recording failure must not be the moment a partial envelope slips out:
    # validate the block shape first so the deny keys are preserved as a set.
    visible = codex_envelope._validated_codex_envelope(result)
    hook_output = visible.get("hookSpecificOutput")
    if visible.get("decision") == "block" and isinstance(hook_output, Mapping):
        combined = f"{visible.get('reason') or ''}; {warning}".lstrip("; ")
        hook = dict(hook_output)
        hook["permissionDecisionReason"] = combined
        visible["reason"] = combined
        visible["hookSpecificOutput"] = hook
        visible["systemMessage"] = combined
        visible["suppressOutput"] = delegate_channel_rules.CODEX_DENY_SUPPRESS_OUTPUT
        return codex_envelope._validated_codex_envelope(visible)
    visible["systemMessage"] = warning
    visible["suppressOutput"] = delegate_channel_rules.CODEX_WARNING_SUPPRESS_OUTPUT
    return codex_envelope._validated_codex_envelope(visible)


def observe_codex_pretooluse(
    home, payload: Mapping[str, object],
    result: Mapping[str, object],
    *,
    decision: Mapping[str, str],
    state_dir: Path | None = None,
) -> dict[str, object]:
    """Append the internal decision while returning the host envelope unchanged.

    ``decision`` is required: the host envelope cannot express an allow verdict
    (allow emits ``{}`` so the tool proceeds untouched), so deriving the recorded
    status from the envelope silently degrades every allow row to
    ``decision_unknown`` and makes the spawn/start pairing scan report false
    matcher misses.
    """
    reason = str(decision.get("reason") or result.get("reason") or "")
    permission = str(decision.get("verdict") or "")
    if not permission:
        hook_output = result.get("hookSpecificOutput")
        if isinstance(hook_output, Mapping):
            permission = str(hook_output.get("permissionDecision") or "")
    try:
        codex_observation._append_codex_observation(
            home, payload,
            hook_event_name="PreToolUse",
            status=f"decision_{permission or 'unknown'}",
            reason=reason,
            state_dir=state_dir,
        )
    except BaseException as exc:
        _absorb_engine_rev_skew_for_recovery(exc, "observation_append_fail_open")
        return _codex_observation_failure(result, exc)
    return codex_envelope._validated_codex_envelope(result)


def observe_codex_subagent_start(
    home, payload: Mapping[str, object], *, state_dir: Path | None = None
) -> dict[str, object]:
    """Append an actual start; correlation is deferred to ``board.py lint``."""
    reason = (
        "[delegate-channel/record] SubagentStart 관측 기록 — "
        "PreToolUse 대조는 board.py lint의 멱등 스캔이 수행"
    )
    result: dict[str, object] = {
        "suppressOutput": delegate_channel_rules.CODEX_OBSERVATION_SUPPRESS_OUTPUT
    }

    try:
        codex_observation._append_codex_observation(
            home, payload,
            hook_event_name="SubagentStart",
            status="observed",
            reason=reason,
            state_dir=state_dir,
        )
    except BaseException as exc:
        _absorb_engine_rev_skew_for_recovery(exc, "observation_append_fail_open")
        return _codex_observation_failure(result, exc)
    return codex_envelope._validated_codex_envelope(result)


def _codex_supervisor_fallback(event: str, detail: str) -> dict[str, object]:
    """The complete warning envelope a failed/absent guard run is replaced with."""
    reason = (
        f"[delegate-channel/warn] Codex {event} 가드 실행 실패({detail}) — "
        f"{delegate_channel_rules.CODEX_SUPERVISOR_FALLBACK_MARKER}: native 통과(fail-open)"
    )
    return codex_envelope._validated_codex_envelope({
        "systemMessage": reason,
        "suppressOutput": delegate_channel_rules.CODEX_WARNING_SUPPRESS_OUTPUT,
    })


def _record_supervisor_fallback(
    home, event: str, payload_bytes: bytes, detail: str, state_dir: Path | None
) -> None:
    """Leave a durable trace that this hook answered without running the guard.

    stdout alone disappears with the turn, and a fallback that looks like a normal
    pass is what let a permanently-failing wrapper hide.  The row also makes the
    unpaired spawn visible to the existing ``board.py lint`` miss scan, because no
    ``decision_allow`` was ever recorded for it.
    """
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, Mapping):
            payload = {}
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}
    try:
        codex_observation._append_codex_observation(
            home, payload,
            hook_event_name=event,
            status=delegate_channel_rules.CODEX_SUPERVISOR_FALLBACK_STATUS,
            reason=(
                f"[delegate-channel/warn] {delegate_channel_rules.CODEX_SUPERVISOR_FALLBACK_MARKER}: "
                f"{detail} — 가드 판정 없이 통과(fail-open)"
            ),
            state_dir=state_dir,
        )
    except BaseException as exc:
        # The audit trail is best effort; a failing ledger must not turn the
        # fail-open answer into a hook crash.
        _absorb_engine_rev_skew_for_recovery(exc, "observation_append_fail_open")


def supervise_codex_hook(
    home, event: str,
    child_argv: Sequence[str],
    *,
    payload_bytes: bytes,
    state_dir: Path | None = None,
) -> dict[str, object]:
    """Run the guard as a bounded child and answer with a valid envelope either way.

    The guard owns the decision; this layer only bounds its runtime, checks the
    envelope against the engine's own key sets, and substitutes a complete
    warning envelope when the child cannot answer.  Nothing here re-types the
    envelope schema, and nothing here needs shell quoting.

    ``child_argv`` comes from the adapter hook config, which the host already
    executes verbatim, so this adds no authority beyond that trust boundary.
    """
    if event not in delegate_channel_rules.CODEX_SUPERVISOR_EVENTS:
        return _codex_supervisor_fallback(
            delegate_channel_rules.CODEX_SUPERVISOR_EVENTS[0], f"미지 hook 이벤트 {event!r}"
        )
    argv = [str(token) for token in child_argv]
    if not argv:
        detail = "가드 실행 커맨드 없음"
        _record_supervisor_fallback(home, event, payload_bytes, detail, state_dir)
        return _codex_supervisor_fallback(event, detail)
    try:
        completed = subprocess.run(
            argv,
            input=payload_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=delegate_channel_rules.CODEX_SUPERVISOR_TIMEOUT_SECONDS,
        )
        returncode = getattr(completed, "returncode", None)
        if returncode != 0:
            raise ValueError(f"가드 rc={returncode}")
        stdout = getattr(completed, "stdout", b"") or b""
        candidate = json.loads(stdout.decode("utf-8"))
        if not isinstance(candidate, dict):
            raise ValueError("가드 stdout 이 JSON 객체가 아님")
        envelope = codex_envelope._validated_codex_envelope(candidate)
    except BaseException as exc:
        detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
        detail = f"{type(exc).__name__}: {detail}"
        _record_supervisor_fallback(home, event, payload_bytes, detail, state_dir)
        return _codex_supervisor_fallback(event, detail)
    return envelope


def _evaluate_codex_decision(
    home, payload: Mapping[str, object],
    *,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, object] | None:
    """Adapt Codex native spawn input to the shared ``decide`` CLI seam."""
    if payload.get("tool_name") != delegate_channel_rules.CODEX_SPAWN_TOOL_NAME:
        return None

    tool_input = payload.get("tool_input") or {}
    agent_name = (
        tool_input.get(delegate_channel_rules.CODEX_ROLE_INPUT_FIELD)
        if isinstance(tool_input, Mapping)
        else None
    )
    cwd = payload.get("cwd")
    cli_argv = [
        "decide", "--role", agent_name if isinstance(agent_name, str) else "",
        "--harness", "codex",
    ]
    if isinstance(cwd, str) and cwd.strip() and _is_absolute_path(cwd.strip()):
        cli_argv.extend(("--cwd", cwd.strip()))
    cli_stdout = io.StringIO()
    _run_decide_cli(
        home, cli_argv,
        stdout=cli_stdout,
        config_loader=config_loader,
    )
    result = json.loads(cli_stdout.getvalue())

    if result["verdict"] == "deny":
        if (
            not isinstance(cwd, str)
            or not cwd.strip()
            or not _is_absolute_path(cwd.strip())
        ):
            result = delegate_channel_rules._result(
                "allow",
                "[delegate-channel/warn] Codex PreToolUse cwd 절대경로 누락 — 통과(fail-open)",
            )
        else:
            result = _materialize_cwd(result, cwd)
    return result


def evaluate_codex_hook(
    home, payload: Mapping[str, object],
    *,
    config_loader: Callable[[home.Home], Mapping[str, str]] = conf_store.local_config,
) -> dict[str, object] | None:
    """Return only the host envelope; keep allow decisions out of host output."""
    result = _evaluate_codex_decision(home, payload, config_loader=config_loader)
    return None if result is None else codex_envelope._codex_decision_envelope(result)


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
    if args.cwd and not _is_absolute_path(args.cwd):
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
    normalized = delegate_channel_rules.normalize_agent_name(args.role, args.harness, args.tier)
    if normalized is None:
        # Preserve row 0 precedence for an unknown self harness.
        if args.harness.strip().lower() not in delegate_env._known_harnesses():
            result = decide(home, args.role, args.tier or "normal", {}, args.harness)
        else:
            result = delegate_channel_rules._result(
                "allow",
                f"[delegate-channel/record] 에이전트명 정규화 실패({args.harness}/{args.role}) — 통과(fail-open)",
            )
    else:
        role, tier = normalized
        result = decide(home, role, tier, config_loader(home), args.harness)
    result = _materialize_cwd(result, args.cwd)
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
    # fail-open 보고 자체가 죽으면 안 된다 — 콘솔 인코딩(CP949 등)이 UTF-8 재설정 전에
    # 깨진 경로에서도 ASCII 폴백으로 강등하고, 그마저 실패하면 조용히 삼킨다.
    try:
        print(message, file=stderr)
    except Exception:
        try:
            print(message.encode("ascii", "replace").decode("ascii"), file=stderr)
        except Exception:
            pass


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
        console.configure_console_utf8()
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
            result = supervise_codex_hook(
                pm_paths.home_at(Path(__file__).resolve().parents[2]), cli_argv[1],
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
            decision = _evaluate_codex_decision(
                pm_paths.home_at(Path(__file__).resolve().parents[2]), payload, config_loader=config_loader
            )
            if decision is not None:
                result = codex_envelope._codex_decision_envelope(decision)
                result = observe_codex_pretooluse(
                    pm_paths.home_at(Path(__file__).resolve().parents[2]), payload, result, decision=decision, state_dir=state_dir
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
            result = observe_codex_subagent_start(pm_paths.home_at(Path(__file__).resolve().parents[2]), payload, state_dir=state_dir)
            _write_machine_line(
                json.dumps(codex_envelope._validated_codex_envelope(result), ensure_ascii=False),
                output_stream,
                console,
            )
            return 0
        if cli_argv:
            return _run_decide_cli(
                pm_paths.home_at(Path(__file__).resolve().parents[2]), cli_argv,
                stdout=output_stream,
                config_loader=config_loader,
                machine_seam=console,
            )

        input_stream = sys.stdin if stdin is None else stdin
        payload = json.loads(input_stream.read())
        if not isinstance(payload, Mapping):
            raise TypeError("hook input JSON must be an object")
        result = evaluate_hook(pm_paths.home_at(Path(__file__).resolve().parents[2]), payload, config_loader=config_loader)
        if result is not None:
            _write_machine_line(
                json.dumps(result, ensure_ascii=False),
                output_stream,
                console,
            )
        return 0
    except BaseException as exc:
        # The shared hook/CLI boundary intentionally absorbs marked engine skew:
        # a broken guard must not lock out either native delegation surface.
        _absorb_engine_rev_skew_for_recovery(exc, "hook_fail_open")
        output_stream = sys.stdout if stdout is None else stdout
        # 가드 자신이 죽은 경계의 emit 은 seam 에 기대지 않는다 — 진입 콘솔 설정 전에 실패한
        # 경우도 여기로 오므로, 아래 emit 은 seam 없이 UTF-8 bytes 를 쓰는 폴백을 탄다.
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
                    _codex_supervisor_fallback(
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
