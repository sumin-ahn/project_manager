#!/usr/bin/env python3
"""claude ctx 임계 비차단 넛지 + 압축 기록 훅.

Claude Code 가 PreToolUse(도구 실행 전)와 UserPromptSubmit(prompt 처리 전)에서 호출한다. 그 밖의
이벤트 입력은 아무것도 바꾸지 않는다. 훅 입력엔 ``context_window`` 가 **없을 수 있어**(statusline
전용) — 그래서 훅은 ``transcript_path`` JSONL 끝부분을 한 번 읽어 점유 토큰·모델·마지막 압축 경계 uuid 를 얻고
(``ctx_guard``), 엔진 ``judge_context`` 를 한 번 불러 그 결과를 Claude 훅 출력으로 옮긴다.

밴드(nudge/nudge2/stop) 판정·안내 문구·사이클 marker 선점은 엔진 소유다. 이 훅은 엔진이 발화하라고 한
안내만 ``additionalContext`` 로 비차단 주입하고 deny/block/회전 marker 를 만들지 않는다.

압축 감지: PreCompact·PostCompact 입력은 주 세션 압축과 서브에이전트 압축을 구별할 수 없다(둘 다
주 세션 ID·주 대화 기록으로 온다). 그래서 압축 처리는 주 스레드 훅이 한다 — 주 대화 기록의 마지막
압축 경계 항목의 uuid 를 경계로 넘기면 엔진이 ``<세션>.compact-<uuid>`` 를 배타 선점하고 밴드·원칙 marker 를
재무장한다. 최초 처리일 때만 이 훅이 ``pm_log.py checkpoint``(골격·breadcrumb)와 ``pm_log.py snapshot``
주입을 한다. 기록에 실패하면 엔진 사유 한 줄을 ``systemMessage``·``additionalContext`` 로 보인다.

서브에이전트 면제: 입력에 ``agent_id`` 가 있으면(서브에이전트의 도구 호출) 어떤 상태도 바꾸지 않고
출력도 없다. ``agent_type`` 은 주 스레드 ``--agent`` 세션에도 실리므로 보지 않는다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_guard  # noqa: E402  (같은 디렉토리 공유 코어)

_engine_tools = str(Path(__file__).resolve().parents[1] / ".project_manager" / "tools")
if _engine_tools not in sys.path:
    sys.path.insert(0, _engine_tools)
from pm_engine.cli import hooks  # noqa: E402  (설치 루트 엔진의 어댑터 공개 진입)

_SNAPSHOT_TIMEOUT_SECONDS = 3.0
_CHECKPOINT_TIMEOUT_SECONDS = 5.0
_MAIN_THREAD_EVENTS = frozenset({"PreToolUse", "UserPromptSubmit"})
_COMPACTION_FAILURE_PREFIX = "[ctx-checkpoint] 압축 기록 실패: "


def _session_id(stdin: dict) -> str:
    """stdin 의 세션 식별자 원문(없으면 빈 문자열). 파일명 안전화는 엔진이 한다."""
    sid = stdin.get("session_id") or stdin.get("sessionId")
    return sid if isinstance(sid, str) else ""


def _pm_log_path(root: Path) -> Path:
    return Path(root) / ".project_manager" / "tools" / "pm_log.py"


def _run_pm_log(root: Path, argv: list[str], timeout: float) -> tuple[int, str, str]:
    """엔진 ``pm_log.py`` 를 돌려 ``(rc, stdout, stderr)`` 를 받는다 — 압축 기록의 유일한 실행 자리.

    렌더는 엔진 단일 소유다(builder command token: ``pm_log.py snapshot``). 엔진 파일이 없으면
    rc 2, 시간 초과·실행 실패는 rc 1 과 예외 이름·메시지를 stderr 자리에 담아 돌려준다 — 훅은
    비차단이라 예외로 끝나지 않고, 호출부가 그 사유를 실패 신호로 보인다.
    """
    engine = _pm_log_path(root)
    if not engine.is_file():
        return 2, "", "pm_log.py 없음"
    try:
        result = subprocess.run(
            [sys.executable, str(engine), *argv],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout or "", result.stderr or ""


def _compaction_failure_output(event: str, rc: int, stderr: str) -> dict:
    """압축 기록 실패 신호 — 사람(systemMessage)과 모델(additionalContext) 둘 다에 같은 한 줄."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    text = _COMPACTION_FAILURE_PREFIX + (lines[-1] if lines else f"pm_log rc={rc}")
    return {
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": text},
    }


def _compaction_output(stdin: dict, home, *, boundary_id: str) -> dict:
    """엔진이 정한 경계 ID 로 checkpoint 골격·breadcrumb 를 남기고 snapshot 을 주입한다(실패면 실패 신호)."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    raw_sid = stdin.get("session_id")
    raw_sid = raw_sid if isinstance(raw_sid, str) else ""
    rc, _stdout, stderr = _run_pm_log(
        home.root,
        ["checkpoint", "--trigger", "compaction", "--session-id", raw_sid,
         "--boundary-id", boundary_id, "--breadcrumb"],
        _CHECKPOINT_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return _compaction_failure_output(event, rc, stderr)
    rc, stdout, stderr = _run_pm_log(
        home.root, ["snapshot", "--session-id", raw_sid], _SNAPSHOT_TIMEOUT_SECONDS,
    )
    if rc != 0 or not stdout:
        return _compaction_failure_output(event, rc, stderr)
    return nudge_output(stdin, stdout)


def nudge_output(stdin: dict, guidance: str) -> dict | None:
    """모델-facing 비차단 안내 주입. deny/block 하지 않는다.

    Claude Code v2.1.9부터 PreToolUse도 ``hookSpecificOutput``의
    ``hookEventName:"PreToolUse"`` + ``additionalContext``를 비차단 주입으로 지원한다.
    ``permissionDecision``을 생략해 정상 권한 흐름과 도구 실행을 그대로 유지한다.
    ``additionalContext`` 10,000자 상한 안에 들도록 아래 세 고정 안내문을 검증한다.

    문서의 주의대로 명령형 out-of-band 지시는 인젝션 방어를 발동시킬 수 있으므로,
    주입 문구는 현행 조건부 권고형을 재사용하고 채널별 변형을 두지 않는다.
    """
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event in {"PreToolUse", "UserPromptSubmit"}:
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": guidance,
            }
        }
    return None




def _principle_recall_signal(stdin: dict) -> tuple[str, str] | None:
    """이 호출의 recall `on` 축 + 대조 텍스트(판별 불가면 None). 도구 이름 → `on` 매핑은
    어댑터 소유 — claude 실제 도구 이름(Bash·Edit/Write·Agent)을 레지스트리의 닫힌 4어휘로 줄인다."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "UserPromptSubmit":
        prompt = stdin.get("prompt")
        return ("prompt", prompt) if isinstance(prompt, str) else None
    if event != "PreToolUse":
        return None
    tool = stdin.get("tool_name") or stdin.get("toolName")
    tool_input = stdin.get("tool_input") or stdin.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool == "Bash":
        command = tool_input.get("command")
        return ("shell", command) if isinstance(command, str) else None
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        file_path = tool_input.get("file_path") or tool_input.get("path")
        return ("edit", file_path) if isinstance(file_path, str) else None
    if tool == "Agent":
        role = tool_input.get("subagent_type") or tool_input.get("description") or ""
        return ("delegate", str(role))
    return None


def _principle_recall_text(stdin: dict, home) -> str:
    """매칭 규칙 주입 문안(없으면 빈 문자열). 판독·기록 실패는 원인이 있는 경고 문안으로 낸다."""
    signal = _principle_recall_signal(stdin)
    if signal is None:
        return ""
    on, text = signal
    try:
        result = hooks.recall_principles(
            home, session=_session_id(stdin), on=on, text=text, main_thread=True,
        )
    except (OSError, UnicodeError) as exc:
        return f"[principle-recall] 판정 실패({type(exc).__name__}: {exc}) — 원칙 recall 없이 진행"
    return result["text"] if result else ""


# claude additionalContext 계약 상한(nudge_output 이 검증하는 것과 같은 값) — recall 채널은
# 자기 문안만 이 상한 안으로 접지만(principle_rules._MAX_INJECT_CHARS), 다른 채널과 합본한 뒤에는
# 재검사하지 않았다. 최종 합본 경계에서 다시 강제한다.
_MAX_ADDITIONAL_CONTEXT_CHARS = 10_000


def _cap_merged_context(existing: str, extra_text: str) -> str:
    """`existing`(ctx 밴드 안내) + `extra_text`(recall 등 부가 채널)를 합본하되 최종 길이가
    상한을 넘으면 원문을 자르지 않고 `extra_text` 를 생략 표시로 접는다 — ctx 밴드 안내가
    안전 경계 안내라 우선순위가 높고, 부가 채널이 짧게 양보한다."""
    combined = f"{existing}\n{extra_text}" if existing else extra_text
    if len(combined) <= _MAX_ADDITIONAL_CONTEXT_CHARS:
        return combined
    fallback = f"[principle-recall] 문안 생략 — 합본 상한({_MAX_ADDITIONAL_CONTEXT_CHARS}자) 초과"
    reduced = f"{existing}\n{fallback}" if existing else fallback
    if len(reduced) <= _MAX_ADDITIONAL_CONTEXT_CHARS:
        return reduced
    # existing 자체가 상한 초과인 극단 방어(ctx 밴드 고정 문구 현재 길이로는 도달하지 않는다).
    return reduced[:_MAX_ADDITIONAL_CONTEXT_CHARS]


def _merge_additional_context(output: dict | None, event: str, extra_text: str) -> dict:
    """additionalContext 문자열 누적(codex `merge_hook_envelopes` 와 같은 의미론).

    ctx 밴드 채널이 이미 안내를 냈으면 줄바꿈으로 이어붙이고, 없었으면 recall 문안 단독으로 낸다.
    최종 길이가 상한을 넘으면 `_cap_merged_context` 가 원문 절단 대신 생략 표시로 접는다."""
    if output is None:
        capped = _cap_merged_context("", extra_text)
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": capped}}
    hook_output = output.setdefault("hookSpecificOutput", {"hookEventName": event})
    existing = hook_output.get("additionalContext")
    existing = existing if isinstance(existing, str) else ""
    hook_output["additionalContext"] = _cap_merged_context(existing, extra_text)
    return output


def _is_subagent_call(stdin: dict) -> bool:
    """서브에이전트 안에서 발화한 훅인가 — ``agent_id`` 하나로 판정한다.

    서브에이전트 훅 입력의 ``session_id``·``transcript_path`` 는 주 세션과 같아 그 둘로는 가를 수
    없다. ``agent_type`` 은 주 스레드 ``--agent`` 세션에도 실리므로 판정에 쓰지 않는다."""
    agent_id = stdin.get("agent_id")
    return isinstance(agent_id, str) and bool(agent_id.strip())


def _combine_outputs(first: dict | None, second: dict | None) -> dict | None:
    """두 훅 출력을 합친다 — ``additionalContext``·``systemMessage`` 는 앞 것 뒤에 줄바꿈으로 잇는다."""
    if first is None:
        return second
    if second is None:
        return first
    combined = dict(first)
    second_message = second.get("systemMessage")
    if isinstance(second_message, str) and second_message:
        first_message = combined.get("systemMessage")
        combined["systemMessage"] = (
            f"{first_message}\n{second_message}" if first_message else second_message
        )
    second_hook = second.get("hookSpecificOutput")
    if isinstance(second_hook, dict):
        hook_output = dict(combined.get("hookSpecificOutput") or {})
        hook_output.setdefault("hookEventName", second_hook.get("hookEventName"))
        second_context = second_hook.get("additionalContext")
        if isinstance(second_context, str) and second_context:
            first_context = hook_output.get("additionalContext")
            hook_output["additionalContext"] = (
                f"{first_context}\n{second_context}" if first_context else second_context
            )
        combined["hookSpecificOutput"] = hook_output
    return combined


def evaluate(stdin: dict, home) -> tuple[int, dict | None]:
    """훅 핵심 진입 — 압축 경계 처리, ctx 밴드 안내, 원칙 recall 채널을 합본한다(모두 비차단).

    주 스레드 훅(PreToolUse·UserPromptSubmit, ``agent_id`` 없음)만 판정한다. 대화 기록은 한 번
    읽어 점유 토큰·모델·마지막 경계 uuid 를 함께 얻고 엔진 ``judge_context`` 를 한 번 부른다. recall 은 독립
    채널이라 밴드 판정과 무관하게 매칭되면 additionalContext 에 누적된다."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event not in _MAIN_THREAD_EVENTS or _is_subagent_call(stdin):
        return 0, None
    transcript = stdin.get("transcript_path")
    raw_tokens, session_model, boundary = (
        ctx_guard.context_tokens_from_transcript(transcript)
        if isinstance(transcript, str) and transcript
        else (0, None, None)
    )
    decision = hooks.judge_context(
        home, harness="claude", session=_session_id(stdin), tokens=raw_tokens,
        model=session_model, main_thread=True,
        boundary=boundary,
    )
    output = None
    if decision["compaction_new"]:
        output = _compaction_output(stdin, home, boundary_id=decision["compaction_boundary_id"])
    output = _combine_outputs(output, _evaluate_ctx_bands(stdin, decision))
    recall_text = _principle_recall_text(stdin, home)
    if recall_text:
        output = _merge_additional_context(output, event, recall_text)
    return 0, output


def _evaluate_ctx_bands(stdin: dict, decision: dict) -> dict | None:
    """엔진 ctx 판정 → Claude 훅 출력 또는 None. 모든 경로는 비차단이다."""
    if decision["status"] == "unavailable":  # 압축 한도 기록 부재·손상·입력 불일치 — 판정 불능을 알린다.
        return _budget_notice(stdin, None, f"[ctx-budget] {decision['notice']}", error=True)
    output = nudge_output(stdin, decision["guidance"]) if decision["emit"] else None
    if decision["notice"]:
        output = _budget_notice(stdin, output, f"[ctx-budget] {decision['notice']}", error=False)
    return output


def _budget_notice(stdin: dict, output: dict | None, text: str, *, error: bool) -> dict | None:
    """압축 한도 기록 경고·에러 — UserPromptSubmit 은 `systemMessage`, 그 밖 이벤트의 에러는 stderr."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "UserPromptSubmit":
        return {**(output or {}), "systemMessage": text}
    if error:
        print(text, file=sys.stderr)
    return output


def main() -> int:
    raw = sys.stdin.read()
    try:
        stdin = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        stdin = {}
    if not isinstance(stdin, dict):
        stdin = {}
    root = ctx_guard.repo_root(Path(__file__).resolve().parent)
    rc, output = evaluate(stdin, hooks.adapter_home(root))
    if output is not None:
        sys.stdout.write(json.dumps(output))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
