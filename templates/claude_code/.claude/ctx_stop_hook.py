#!/usr/bin/env python3
"""claude ctx 임계 hard-stop 훅 (PreToolUse + UserPromptSubmit · ADR-0038 · stdlib only).

claude Code 가 호출: UserPromptSubmit(prompt 처리 전 — 새 작업 진입 차단) + PreToolUse(도구 호출 전).
한 스크립트가 stdin 의 ``hook_event_name`` 으로 분기. 훅 입력엔 ``context_window`` 가 **없을 수 있어**
(statusline 전용) — 그래서 훅은 ``transcript_path`` JSONL 을 읽어 컨텍스트 점유를 자체 산출한다.

잔여 컨텍스트가 stop 임계 이하면(ADR-0038 D2 — "새 작업만 정지·핸드오프 도구 예외"):
  1. **STOP marker `.done` 직접 박제** (relay 회전 신호·존재만 stat·본문 없음). 무조건 — 이전의
     ``handoff_rc==0`` 게이트(T-0048)는 run_trigger 폐기로 소멸(ADR-0038 D4). 멱등 = **파일 존재**
     (write 실패 시 다음 호출 재시도 self-heal — in-memory 플래그 안 씀).
  2. **PreToolUse**: 진행 중 rich ``/pm-handoff`` 의 **핸드오프 도구는 통과**(hook 결정 없이 None →
     normal permission eval·settings.json standing deny 유지). 그 외 새 작업 도구는 **deny**.
  3. **UserPromptSubmit**: 새 작업 진입 block — 단 **핸드오프 트리거(`/pm-handoff` 로 시작하는
     prompt)는 통과**(T-0205·ADR-0038 D2 amend — 좁은 매칭·자연어는 계속 block+커맨드 안내).

nudge(ADR-0037) 는 그대로 — 모델-facing 비차단 안내를 UserPromptSubmit ``additionalContext`` 로 주입.

claude 네이티브 auto-compact 는 어댑터 settings.json ``autoCompactEnabled:false`` 로 비활성(ADR-0038 D3·
opencode ``compaction.auto:false`` 파리티) — hard-stop 이 단일 게이트.

출력 스키마 (claude hooks):
  PreToolUse deny: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                    "permissionDecision": "deny", "permissionDecisionReason": "..."}}
  UserPromptSubmit block: {"decision": "block", "reason": "..."}
ok / 핸드오프 도구 통과 면 출력 없이 rc0 (정상 진행 — 훅은 핸드오프를 막지 않는다).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_guard  # noqa: E402  (같은 디렉토리 공유 코어)

# 멱등 marker 디렉토리 (git-ignored 상태 영역 — .project_manager/.gitignore 가 .local/ 커버).
# 세션 id 별 1파일. 채택자가 `git add -A` 해도 세션 marker 가 커밋되지 않게 이미-ignored 경로 사용.
_MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"


def _session_id(stdin: dict) -> str:
    """stdin 에서 세션 식별자 (없으면 'unknown')."""
    sid = stdin.get("session_id") or stdin.get("sessionId")
    if isinstance(sid, str) and sid.strip():
        # 경로 traversal 방지 — 파일명에 안전한 문자만.
        return "".join(c for c in sid.strip() if c.isalnum() or c in "-_")[:64] or "unknown"
    return "unknown"


def _marker_path(root: Path, session_id: str) -> Path:
    return root / _MARKER_DIR / f"{session_id}.done"


def _already_triggered(root: Path, session_id: str) -> bool:
    return _marker_path(root, session_id).exists()


def _mark_triggered(root: Path, session_id: str) -> None:
    path = _marker_path(root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ctx-stop handoff triggered\n", encoding="utf-8")
    except OSError:
        # marker 를 못 써도 정지 자체는 유효 — best-effort. 파일이 안 생기면 _already_triggered 가
        # 계속 False → 다음 stop-도구 호출이 재시도(self-heal). in-memory 플래그를 두지 않는 이유.
        pass


# ── nudge 멱등 marker (ADR-0037 graceful nudge) — stop 의 `.done` 과 분리(`.nudge`) ──
# 독립 marker 라 2단 fail-safe 가 서로 간섭하지 않는다(nudge 안내 ⊥ stop 박제).
def _nudge_marker_path(root: Path, session_id: str) -> Path:
    return root / _MARKER_DIR / f"{session_id}.nudge"


def _already_nudged(root: Path, session_id: str) -> bool:
    return _nudge_marker_path(root, session_id).exists()


def _mark_nudged(root: Path, session_id: str) -> None:
    path = _nudge_marker_path(root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ctx-nudge injected\n", encoding="utf-8")
    except OSError:
        # marker 를 못 써도 안내 자체는 유효 — best-effort.
        pass


# ── 핸드오프 도구 allow-list (ADR-0038 D2) ────────────────────────────────────
# hard-stop 중 진행 중인 rich `/pm-handoff` 가 완주하도록 핸드오프 도구를 통과시킨다. 매칭은
# **hook allow 를 반환하지 않고** None(통과) 으로 두어 settings.json standing deny(force-push·rm)를
# 무력화하지 않는다 — 통과 = "normal permission eval 로 넘김"(ADR-0038 설계검증 allow-list 렌즈).
#
# Bash 매칭 규율: (1) 셸 연쇄/치환 연산자 포함 시 거부(허용 head 로 denied tail 밀반입 차단·fail-closed),
# (2) 선행 env 대입(VAR=val)을 정규화한 뒤 (3) 허용 호출로 *시작*(anchored prefix·substring 금지).
_SHELL_OPS = ("&&", "||", ";", "|", "`", "$(", "\n", ">", "<", "&")
_ENV_PREFIX_RE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*")
_HANDOFF_BASH_PATTERNS = (
    re.compile(r"^(?:python3?\s+)?\S*pm_handoff\.py(?:\s|$)"),
    re.compile(r"^(?:python3?\s+)?\S*domain\.py(?:\s|$)"),
    re.compile(r"^git\s+add(?:\s|$)"),
    re.compile(r"^git\s+commit(?:\s|$)"),
    re.compile(r"^(?:python3?\s+-m\s+)?pytest(?:\s|$)"),
)

# Edit/Write/Read 대상 파일 — 핸드오프가 채우는 산출물 + 그 Edit 전제 Read(claude Edit 은
# 사전 in-session Read 요구). 광역 Read/Grep/Glob·source 편집은 매칭 안 돼 deny(새 작업 방어).
_HANDOFF_TARGET_SUBSTR = ("log/current.md", "pm_state", "status.md")
_HANDOFF_TARGET_DIR = "/domain/"  # .project_manager/wiki/domain/*.md (domain capture 채록)
_HANDOFF_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "Read")


def _is_handoff_bash(command: str) -> bool:
    """Bash command 가 핸드오프-allow 호출로 시작하는가 (연쇄 연산자 없이·anchored)."""
    if not isinstance(command, str) or not command.strip():
        return False
    if any(op in command for op in _SHELL_OPS):
        return False  # 복합 명령 — tail 검증 불가 → deny(fail-closed).
    core = _ENV_PREFIX_RE.sub("", command).strip()
    return any(pat.match(core) for pat in _HANDOFF_BASH_PATTERNS)


def _is_handoff_target(path: str) -> bool:
    """Edit/Write/Read 대상이 핸드오프 산출물(log/pm_state/status/domain)인가."""
    if not isinstance(path, str) or not path:
        return False
    p = path.replace("\\", "/")
    if any(sub in p for sub in _HANDOFF_TARGET_SUBSTR):
        return True
    return _HANDOFF_TARGET_DIR in p


def _is_handoff_tool(stdin: dict) -> bool:
    """PreToolUse 도구 호출이 진행 중 핸드오프의 일부인가 (통과 대상)."""
    tool = stdin.get("tool_name") or stdin.get("toolName")
    tool_input = stdin.get("tool_input") or stdin.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool == "Bash":
        return _is_handoff_bash(tool_input.get("command", ""))
    if tool in _HANDOFF_EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("filePath") or ""
        return _is_handoff_target(path)
    return False


def _is_handoff_prompt(stdin: dict) -> bool:
    """UserPromptSubmit prompt 가 핸드오프 트리거인가 (stop 중 통과 대상·T-0205).

    **좁은 매칭**(사용자 결정 2026-07-02·ADR-0038 D2 amend): 정확 트리거 `/pm-handoff` 로
    *시작*하는 prompt 만(인자 허용 — `/pm-handoff --dry-run` 등). 자연어("핸드오프 해줘")는
    계속 block — 키워드 매칭은 오인식("핸드오프 전에 이 버그 고쳐줘")이 새 작업을 통과시켜
    hard-stop 을 무력화한다. 넓은 문의 실패(안전장치 무력화)가 좁은 문의 실패(안내 보고
    커맨드 재입력·몇 초)보다 비싸다 — block reason 이 정확 커맨드를 안내해 락아웃은 없다.
    """
    prompt = stdin.get("prompt")
    if not isinstance(prompt, str):
        return False
    stripped = prompt.strip()
    # 토큰 경계 필수(codex·reviewer 수렴): bare `startswith` 는 `/pm-handoffX` 같은 비정확
    # 커맨드도 통과시킨다 — 정확 커맨드 단독 또는 공백 뒤 인자만 허용(^/pm-handoff(\s|$)).
    return stripped == "/pm-handoff" or stripped.startswith("/pm-handoff ")


def deny_output(reason: str) -> dict:
    """PreToolUse 차단 — 새 작업 도구 호출 직전 deny."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def block_output(reason: str) -> dict:
    """UserPromptSubmit 차단 — prompt 가 모델에 들어가기 *전* 에 block (새 작업 진입 차단)."""
    return {"decision": "block", "reason": reason}


def nudge_output(stdin: dict, guidance: str) -> dict | None:
    """graceful nudge — 모델-facing 비차단 안내 주입 (ADR-0037). 정지(deny/block) 아님.

    UserPromptSubmit 의 ``additionalContext`` 만 모델 컨텍스트에 비차단 주입한다(claude-code-guide
    실측 확인). PreToolUse 는 모델-컨텍스트 주입 채널이 없고(permissionDecision 만) 도구 중간
    끊김을 피하려 None → 호출부가 통과(도구 정상 진행·statusline 이 사람용 표시 담당).
    """
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "UserPromptSubmit":
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": guidance,
            }
        }
    return None


def build_stop_deny_reason(used_pct: int, remaining: int) -> str:
    """PreToolUse 새 작업 deny 사유 — 핸드오프 도구는 통과함을 안내."""
    return (
        f"[ctx-stop] 컨텍스트 사용 {used_pct}% (잔여 {remaining}%) — 정지 임계 도달. "
        "새 작업 도구는 정지됐다. 진행 중인 `/pm-handoff`(핸드오프 도구: pm_handoff.py·git add/commit·"
        "domain.py·log/current.md·pm_state·status.md·domain 편집)는 통과한다 — rich 핸드오프를 완료하고 "
        "이 세션을 종료한 뒤 **새 세션을 시작**하라."
    )


def build_stop_block_reason(used_pct: int, remaining: int) -> str:
    """UserPromptSubmit 새 작업 진입 block 사유 (+통과 가능한 정확 커맨드 안내·T-0205)."""
    return (
        f"[ctx-stop] 컨텍스트 사용 {used_pct}% (잔여 {remaining}%) — 정지 임계 도달. "
        "새 작업 진입은 차단된다. **지금 입력 가능한 것은 `/pm-handoff` 뿐** — 그대로 입력하면 "
        "rich 핸드오프가 통과·진행되고, 완료 후 **새 세션을 시작**하라."
    )


def evaluate(stdin: dict, root: Path, conf: dict) -> tuple[int, dict | None]:
    """훅 핵심 — (rc, output|None). output None = 통과(도구/prompt 진행).

    stop 시: STOP marker 무조건 박제(relay 신호·ADR-0038 D4) + PreToolUse 핸드오프 도구 통과·
    새 작업 deny / UserPromptSubmit block(핸드오프 트리거 prompt 예외·T-0205).
    """
    transcript = stdin.get("transcript_path")
    window = ctx_guard.ctx_window_tokens(conf)
    thresholds = ctx_guard.ctx_thresholds(conf)

    used = (
        ctx_guard.context_used_pct_from_transcript(transcript, window)
        if isinstance(transcript, str) and transcript
        else 0
    )
    state = ctx_guard.classify(used, thresholds)
    if state == "ok":
        return 0, None

    if state == "nudge":
        # graceful nudge (ADR-0037) — 모델-facing 비차단 안내 주입(엔진 박제 X·흐름 안 끊음).
        # 멱등(세션 1회·.nudge marker). UserPromptSubmit 만 주입 채널(nudge_output) — PreToolUse
        # 면 None → 통과. marker 는 *실제 주입* 시에만 남겨, PreToolUse 선행으로 marker 만 박혀
        # UserPromptSubmit 주입이 누락되는 일을 막는다.
        session_id = _session_id(stdin)
        if _already_nudged(root, session_id):
            return 0, None
        output = nudge_output(stdin, ctx_guard.build_nudge_guidance(used, thresholds))
        if output is None:
            return 0, None
        _mark_nudged(root, session_id)
        return 0, output

    # state == "stop" — hard-stop(ADR-0038 D2: 새 작업만 정지·핸드오프 도구 예외).
    remaining = ctx_guard.remaining_pct(used)
    session_id = _session_id(stdin)

    # STOP marker 직접 박제 — relay 회전 신호. **무조건**(handoff_rc 게이트 없음·T-0048 반전·
    # ADR-0038 D4). 멱등은 파일 존재 기반: 이미 있으면 재작성 안 함, write 실패면 파일 부재로
    # 남아 다음 stop-도구 호출이 재시도(self-heal). marker 는 event 종류(PreToolUse/UserPromptSubmit)
    # 무관하게 stop 도달 즉시 박제 — stop 후 도구를 안 써도 회전 신호 누락 방지.
    if not _already_triggered(root, session_id):
        _mark_triggered(root, session_id)

    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "UserPromptSubmit":
        # 핸드오프-intent 예외(T-0205·ADR-0038 D2 amend): 정확 트리거 `/pm-handoff` prompt 는
        # 통과 — 턴이 끝난 뒤 stop 되면 사용자 prompt 가 유일한 핸드오프 진입 수단인데 전면
        # block 이 그것까지 막아 락아웃(사용자 실측)이었다. 그 외 prompt 는 새 작업 진입
        # 차단 유지(block reason 이 정확 커맨드 안내).
        if _is_handoff_prompt(stdin):
            return 0, None
        return 0, block_output(build_stop_block_reason(used, remaining))

    # PreToolUse — 진행 중 핸드오프 도구는 통과(None → normal permission eval·settings deny 유지).
    if _is_handoff_tool(stdin):
        return 0, None
    # 그 외 새 작업 도구는 deny.
    return 0, deny_output(build_stop_deny_reason(used, remaining))


def main(argv: list[str] | None = None) -> int:
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
