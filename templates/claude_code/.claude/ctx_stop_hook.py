#!/usr/bin/env python3
"""claude ctx 임계 비차단 넛지 + 압축 기록 훅.

Claude Code 가 PreToolUse(도구 실행 전)와 UserPromptSubmit(prompt 처리 전)에서 호출한다. 그 밖의
이벤트 입력은 아무것도 바꾸지 않는다. 훅 입력엔 ``context_window`` 가 **없을 수 있어**(statusline
전용) — 그래서 훅은 ``transcript_path`` JSONL 을 읽어 컨텍스트 점유와 압축 경계 수를 자체 산출한다.

세 밴드(nudge/nudge2/stop) 모두 모델-facing 비차단 안내를 ``additionalContext`` 로 주입한다. stop
밴드는 구 차단형 대신 최종 넛지이며 `.final` marker 로 사이클당 한 번만 주입한다. 훅은
deny/block/회전 marker 를 만들지 않는다.

압축 감지: PreCompact·PostCompact 입력은 주 세션 압축과 서브에이전트 압축을 구별할 수 없다(둘 다
주 세션 ID·주 대화 기록으로 온다). 그래서 압축 처리는 주 스레드 훅이 한다 — 주 대화 기록의 압축
경계 수 N 이 늘면(서브에이전트 경계는 서브에이전트 기록에 남는다) 처리 표식
``<세션>.compact-<N>`` 을 배타 선점한 호출 하나가 밴드 marker 재무장, ``pm_log.py checkpoint``
(골격·breadcrumb), ``pm_log.py snapshot`` 주입을 경계마다 한 번 한다. 기록에 실패하면 엔진 사유
한 줄을 ``systemMessage``·``additionalContext`` 로 보인다.

서브에이전트 면제: 입력에 ``agent_id`` 가 있으면(서브에이전트의 도구 호출) 어떤 상태도 바꾸지 않고
출력도 없다. ``agent_type`` 은 주 스레드 ``--agent`` 세션에도 실리므로 보지 않는다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_guard  # noqa: E402  (같은 디렉토리 공유 코어)

# 멱등 marker 디렉토리 (git-ignored 상태 영역 — .project_manager/.gitignore 가 .local/ 커버).
# 세션 id 별 1파일. 채택자가 `git add -A` 해도 세션 marker 가 커밋되지 않게 이미-ignored 경로 사용.
_MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"
_SNAPSHOT_TIMEOUT_SECONDS = 3.0
_CHECKPOINT_TIMEOUT_SECONDS = 5.0
_MAIN_THREAD_EVENTS = frozenset({"PreToolUse", "UserPromptSubmit"})
_COMPACTION_FAILURE_PREFIX = "[ctx-checkpoint] 압축 기록 실패: "


def _session_id(stdin: dict) -> str:
    """stdin 에서 세션 식별자 (없으면 'unknown')."""
    sid = stdin.get("session_id") or stdin.get("sessionId")
    if isinstance(sid, str) and sid.strip():
        # 경로 traversal 방지 — 파일명에 안전한 문자만.
        return "".join(c for c in sid.strip() if c.isalnum() or c in "-_")[:64] or "unknown"
    return "unknown"


# ── 사이클별 넛지 멱등 marker ───────────────────────────────────
def _nudge_marker_path(root: Path, session_id: str) -> Path:
    return root / _MARKER_DIR / f"{session_id}.nudge"


def _claim_marker(path: Path, content: str) -> bool:
    """marker 를 배타 생성해 이번 호출이 사이클별 주입권을 얻었는지 반환한다.

    ``exists()`` 뒤 생성하는 TOCTOU를 피한다. 병렬 hook 호출 중 ``O_EXCL`` 생성에 성공한
    하나만 True이며, 이미 선점됐거나 marker 디렉토리를 쓸 수 없으면 안내를 주입하지 않는다.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False

    # 생성 성공 자체가 선점의 정본이다. 진단용 본문 쓰기 실패가 선점을 무효화하지는 않는다.
    try:
        os.write(fd, content.encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


# 2단은 1단 발화 여부와 독립이라 별도 marker 로 사이클당 1회 주입한다.
def _nudge2_marker_path(root: Path, session_id: str) -> Path:
    return root / _MARKER_DIR / f"{session_id}.nudge2"


def _final_marker_path(root: Path, session_id: str) -> Path:
    return root / _MARKER_DIR / f"{session_id}.final"


def _compaction_marker_path(root: Path, session_id: str, boundaries: int) -> Path:
    """주 대화 기록 경계 N 의 처리 표식 — 경계 하나에 처리 한 번(배타 선점)."""
    return root / _MARKER_DIR / f"{session_id}.compact-{boundaries}"


def _pm_log_path(root: Path) -> Path:
    return Path(root) / ".project_manager" / "tools" / "pm_log.py"


def _build_ctx_guidance(
    root: Path,
    *,
    band: str,
    used_pct: int,
    thresholds: dict[str, int],
) -> str:
    """pm_log.py의 단일 ctx 정책 문구를 읽는다. 실패 시 비종료 fallback만 반환한다."""
    engine = _pm_log_path(root)
    labels = {
        "nudge": "ctx-nudge",
        "nudge2": "ctx-nudge/강화",
        "final": "ctx-nudge/최종",
    }
    command = [
        sys.executable,
        str(engine),
        "ctx-guidance",
        "--band",
        band,
        "--used-pct",
        str(used_pct),
        "--precompact-at-pct",
        str(thresholds["precompact_at_pct"]),
    ]
    if engine.is_file():
        try:
            result = subprocess.run(
                command,
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                timeout=_SNAPSHOT_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass

    # 구 엔진/실행 실패에서도 winding-down 함의를 되살리지 않는다. 중앙 명령을 쓸 수 없는
    # fail-soft 경계라 연속성 정책의 필수 두 사실만 최소 복제한다.
    checkpoint = "python3 .project_manager/tools/pm_log.py checkpoint --task <task>"
    if band == "final":
        checkpoint += " --trigger compaction"
    return (
        f"[{labels[band]}] 컨텍스트 사용 {used_pct}%. "
        f"auto-compact 관측 구간이다. `{checkpoint}` 기록을 사용할 수 있다. "
        "`<task>`에는 현재 task 이름을 사용한다. "
        "압축은 자동이고 세션은 그대로 이어진다. "
        "핸드오프는 사용자 명시 지시로만 한다. "
        "상세 ctx 연속성 정책은 pm_log.py ctx-guidance 엔진에서 복구한다."
    )


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


def _compaction_output(stdin: dict, root: Path, session_id: str, boundaries: int) -> dict:
    """경계 N 의 checkpoint 골격·breadcrumb 를 남기고 snapshot 을 주입한다(실패면 실패 신호)."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    raw_sid = stdin.get("session_id")
    raw_sid = raw_sid if isinstance(raw_sid, str) else ""
    rc, _stdout, stderr = _run_pm_log(
        root,
        ["checkpoint", "--trigger", "compaction", "--session-id", raw_sid,
         "--boundary-id", f"claude-{session_id}-{boundaries}", "--breadcrumb"],
        _CHECKPOINT_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return _compaction_failure_output(event, rc, stderr)
    rc, stdout, stderr = _run_pm_log(
        root, ["snapshot", "--session-id", raw_sid], _SNAPSHOT_TIMEOUT_SECONDS,
    )
    if rc != 0 or not stdout:
        return _compaction_failure_output(event, rc, stderr)
    return nudge_output(stdin, stdout)


def _rearm_cycle(root: Path, session_id: str) -> None:
    """압축 경계 처리/유효한 ok 복귀 시 marker 를 지워 다음 상승 사이클을 재무장."""
    for marker in (
        _nudge_marker_path(root, session_id),
        _nudge2_marker_path(root, session_id),
        _final_marker_path(root, session_id),
    ):
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            # 상태 파일 정리는 best-effort. 삭제 실패 시 기존 marker 가 중복 주입을 막는다.
            pass


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


def _load_principles(root: Path):
    """`pm_principles.py` 를 root 기준으로 로드한다(codex `_load_board`·claude git-anchor 동형).

    부재·파손은 예외를 삼키고 None — 레지스트리가 없거나 깨져도 훅은 도구 실행을 막지 않는다."""
    path = root / ".project_manager" / "tools" / "pm_principles.py"
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("pm_principles", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — 레지스트리 로드 실패는 비차단 침묵.
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


def _principle_recall_text(stdin: dict, root: Path) -> str:
    """매칭 규칙 주입 문안(없거나 판정 불가면 빈 문자열). 어떤 실패도 예외로 새지 않는다."""
    signal = _principle_recall_signal(stdin)
    if signal is None:
        return ""
    on, text = signal
    module = _load_principles(root)
    if module is None:
        return ""
    session_id = _session_id(stdin)
    try:
        seen = module.load_seen_marker(root, session_id)
        result = module.judge_recall(root, on=on, text=text, seen=seen)
    except Exception:  # noqa: BLE001 — 판정 실패는 비차단 침묵(도구 실행을 막지 않는다).
        return ""
    if not result:
        return ""
    if result.get("keys"):
        try:
            module.record_seen_marker(root, session_id, result["keys"])
        except Exception:  # noqa: BLE001 — marker 기록 실패는 소음(재주입)일 뿐.
            pass
    return result.get("text") or ""


def _rearm_principle_recall(stdin: dict, root: Path) -> None:
    """압축 경계 처리에서 원칙 recall marker 를 지워 다음 사이클을 재무장한다."""
    module = _load_principles(root)
    if module is None:
        return
    try:
        module.rearm_seen_marker(root, _session_id(stdin))
    except Exception:  # noqa: BLE001 — 재무장 실패는 다음 경계로 넘긴다(최악 = 재주입 skip).
        pass


# claude additionalContext 계약 상한(nudge_output 이 검증하는 것과 같은 값) — recall 채널은
# 자기 문안만 이 상한 안으로 접지만(pm_principles._MAX_INJECT_CHARS), 다른 채널과 합본한 뒤에는
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


def evaluate(stdin: dict, root: Path, conf: dict) -> tuple[int, dict | None]:
    """훅 핵심 진입 — 압축 경계 처리, ctx 밴드 판정, 원칙 recall 채널을 합본한다(모두 비차단).

    주 스레드 훅(PreToolUse·UserPromptSubmit, ``agent_id`` 없음)만 판정한다. 대화 기록은 한 번
    읽어 점유 토큰·모델·경계 수를 함께 얻는다. recall 은 독립 채널이라 밴드 판정과 무관하게
    매칭되면 additionalContext 에 누적된다 — 레지스트리 부재·파손이어도 다른 출력은 그대로 실린다."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event not in _MAIN_THREAD_EVENTS or _is_subagent_call(stdin):
        return 0, None
    transcript = stdin.get("transcript_path")
    raw_tokens, session_model, boundaries = (
        ctx_guard.context_tokens_from_transcript(transcript)
        if isinstance(transcript, str) and transcript
        else (0, None, 0)
    )
    session_id = _session_id(stdin)
    output = None
    if boundaries >= 1 and _claim_marker(
        _compaction_marker_path(root, session_id, boundaries), "compaction handled\n"
    ):
        _rearm_cycle(root, session_id)
        _rearm_principle_recall(stdin, root)
        output = _compaction_output(stdin, root, session_id, boundaries)
    output = _combine_outputs(
        output, _evaluate_ctx_bands(stdin, root, conf, raw_tokens, session_model),
    )
    recall_text = _principle_recall_text(stdin, root)
    if recall_text:
        output = _merge_additional_context(output, event, recall_text)
    return 0, output


def _evaluate_ctx_bands(
    stdin: dict, root: Path, conf: dict, raw_tokens: int, session_model: str | None,
) -> dict | None:
    """ctx 밴드 판정 — 출력 또는 None. 모든 경로는 비차단이다."""
    session_id = _session_id(stdin)
    # 분모 = 적용 한도 E(압축 한도 기록) — statusLine 과 같은 기록. 훅 입력에는 모델이 없어 세션
    # 모델은 사용량을 읽은 transcript 항목의 것이다.
    try:
        budget = ctx_guard.read_budget(root, conf, session_model)
    except ValueError as exc:  # 기록 부재·손상·입력 불일치 — 밴드를 판정하지 못한 사실을 알린다.
        return _budget_notice(stdin, None, f"[ctx-budget] {exc}", error=True)
    output = _band_output(stdin, root, session_id, raw_tokens, budget)
    if budget["warning"]:
        output = _budget_notice(stdin, output, f"[ctx-budget] {budget['warning']}", error=False)
    return output


def _budget_notice(stdin: dict, output: dict | None, text: str, *, error: bool) -> dict | None:
    """압축 한도 기록 경고·에러 — UserPromptSubmit 은 `systemMessage`, 그 밖 이벤트의 에러는 stderr."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "UserPromptSubmit":
        return {**(output or {}), "systemMessage": text}
    if error:
        print(text, file=sys.stderr)
    return output


def _band_output(stdin: dict, root: Path, session_id: str, raw_tokens: int, budget: dict) -> dict | None:
    """적용 한도 대비 사용 % 의 밴드 안내 — 사이클당 한 번(marker)."""
    used = ctx_guard.context_used_pct_from_tokens(raw_tokens, budget["E"])
    state = ctx_guard.classify(used, budget)
    if state == "ok":
        # 정수 used=0은 큰 예산에서 작은 양의 측정값도 될 수 있다. raw 토큰이 양수일 때만 유효한
        # ok 실측으로 재무장하고, transcript 부재·읽기 실패·usage 미검출(raw 0)은 marker를 보존한다.
        if raw_tokens > 0:
            _rearm_cycle(root, session_id)
        return None

    if state == "nudge":
        # 모델-facing 비차단 안내 주입. marker 는 채널과 무관하게 사이클당 1회이며,
        # PreToolUse/UserPromptSubmit 중 먼저 실제 주입한 채널이 소비한다.
        output = nudge_output(
            stdin,
            _build_ctx_guidance(
                root, band="nudge", used_pct=used, thresholds=budget,
            ),
        )
        if output is None:
            return None
        if not _claim_marker(
            _nudge_marker_path(root, session_id), "ctx-nudge injected\n"
        ):
            return None
        return output

    if state == "nudge2":
        # 강화 넛지. 멱등(사이클 1회·.nudge2 marker). 1단(.nudge)과 독립이라 1단 창을
        # 건너뛴 세션도 2단은 발화한다.
        # nudge 와 동일하게 두 채널이 공유 marker 로 사이클당 1회만 소비한다.
        output = nudge_output(
            stdin,
            _build_ctx_guidance(
                root, band="nudge2", used_pct=used, thresholds=budget,
            ),
        )
        if output is None:
            return None
        if not _claim_marker(
            _nudge2_marker_path(root, session_id), "ctx-nudge2 injected\n"
        ):
            return None
        return output

    # state == "stop" — 구 차단형 대신 최종 비차단 넛지(사이클당 1회·`.final`).
    output = nudge_output(
        stdin,
        _build_ctx_guidance(
            root, band="final", used_pct=used, thresholds=budget,
        ),
    )
    if output is None:
        return None
    if not _claim_marker(
        _final_marker_path(root, session_id), "ctx-final nudge injected\n"
    ):
        return None
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
    conf = ctx_guard.load_local_config(root)
    rc, output = evaluate(stdin, root, conf)
    if output is not None:
        sys.stdout.write(json.dumps(output))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
