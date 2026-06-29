#!/usr/bin/env python3
"""claude ctx 임계 하드 정지 훅 (PreToolUse + UserPromptSubmit) + 자동 handoff (T-0015 · stdlib only).

claude Code 가 호출: UserPromptSubmit(prompt 처리 전 — 새 작업 진입 차단) + PreToolUse(도구 호출 전).
한 스크립트가 stdin 의 `hook_event_name` 으로 분기 — UserPromptSubmit→prompt block, PreToolUse→tool deny.
훅 입력엔 ``context_window`` 가 **없을 수 있어**(statusline 전용) — 그래서 훅은
``transcript_path`` JSONL 을 읽어 컨텍스트 점유를 자체 산출한다.

잔여 컨텍스트가 stop 임계 이하면:
  1. 도구 호출을 **deny** (PreToolUse permissionDecision:"deny" — 유일한 차단 수단).
  2. ``pm_handoff.py --trigger --reason ctx-stop --ctx-pct <N>`` shell-out
     (권위 handoff 박제). 멱등 — 세션당 **1회**만 트리거(중복 deny 로 handoff
     여러 번 안 나게 marker 파일로 가드).
  3. "새 세션 시작" 안내를 deny reason 으로 돌려준다.

claude native auto-compact 보다 우리 stop 이 먼저 오게 — stop_pct 잔여(기본 10%)에서
선점한다 (명시 disable config 없으면 임계 선점).

출력 스키마 (claude hooks):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "deny"|"allow",
                          "permissionDecisionReason": "..."}}
ok/nudge 면 출력 없이 rc0 (도구 정상 진행 — 훅은 정상 작업을 막지 않는다).
"""
from __future__ import annotations

import json
import subprocess
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
        # marker 를 못 써도 정지 자체는 유효 — best-effort.
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


def run_handoff(root: Path, ctx_pct: int, runner=subprocess.run, thread_tail: str = "") -> int:
    """pm_handoff --trigger shell-out. rc 반환 (0=박제 성공). 실패해도 정지는 유효.

    thread_tail(정지 직전 사용자 발화)이 비어있지 않으면 ``--thread-tail`` 로 배선해
    handoff entry "다음 intent" 의 대화 thread-tail 슬롯을 자동 채운다(T-0047).
    빈 문자열이면 인자를 붙이지 않아 엔진이 placeholder 를 유지한다(하위호환).
    """
    cmd = [
        sys.executable,
        str(root / ".project_manager" / "tools" / "pm_handoff.py"),
        "--trigger",
        "--reason", "ctx-stop",
        "--ctx-pct", str(ctx_pct),
    ]
    if thread_tail:
        cmd += ["--thread-tail", thread_tail]
    try:
        completed = runner(cmd, cwd=str(root), capture_output=True, text=True)
        return completed.returncode
    except (OSError, ValueError):
        return 1


def deny_output(reason: str) -> dict:
    """PreToolUse 차단 — 도구 호출 직전 deny."""
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


def _stop_output(stdin: dict, reason: str) -> dict:
    """훅 이벤트별 정지 출력 — UserPromptSubmit=prompt block / PreToolUse=tool deny.

    UserPromptSubmit 은 prompt 처리 *전* 실행돼 새 작업 진입 자체를 막고(T-0012 정지 경계),
    PreToolUse 는 이미 진행 중인 턴의 도구 호출을 막는다. 둘 다 배선해 빈틈을 없앤다.
    """
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "UserPromptSubmit":
        return block_output(reason)
    return deny_output(reason)  # 기본 = PreToolUse


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


def build_stop_reason(used_pct: int, remaining: int, handoff_rc: int) -> str:
    handoff_note = (
        "권위 handoff 가 박제됐다 (pm_handoff --trigger)."
        if handoff_rc == 0
        else "handoff 박제 실패 — 수동으로 `pm_handoff --trigger` 실행 권장."
    )
    return (
        f"[ctx-stop] 컨텍스트 사용 {used_pct}% (잔여 {remaining}%) — 정지 임계 도달. "
        f"이 세션에서 더 진행하지 말고 핸드오프 후 **새 세션을 시작**하라. {handoff_note}"
    )


def evaluate(stdin: dict, root: Path, conf: dict, handoff_fn=None) -> tuple[int, dict | None]:
    """훅 핵심 — (rc, output|None). output None = 통과(도구 진행).

    handoff_fn(root, ctx_pct) -> rc 를 주입하면 shell-out 을 대체 (테스트용).
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
        # 면 None → 통과(도구 정상 진행·statusline 이 사람용 표시). marker 는 *실제 주입* 시에만
        # 남겨, PreToolUse 선행으로 marker 만 박혀 UserPromptSubmit 주입이 누락되는 일을 막는다.
        session_id = _session_id(stdin)
        if _already_nudged(root, session_id):
            return 0, None
        output = nudge_output(stdin, ctx_guard.build_nudge_guidance(used, thresholds))
        if output is None:
            return 0, None
        _mark_nudged(root, session_id)
        return 0, output

    # state == "stop" — hard-stop(fail-safe·기계 박제·deny). nudge 와 독립(별개 marker).
    remaining = ctx_guard.remaining_pct(used)
    session_id = _session_id(stdin)

    # 멱등: 이미 트리거된 세션이면 handoff 재실행 없이 차단만 (반복·중복 박제 방지).
    if _already_triggered(root, session_id):
        reason = (
            f"[ctx-stop] 컨텍스트 사용 {used}% (잔여 {remaining}%) — 정지 임계 (이미 핸드오프 박제됨). "
            "새 세션을 시작하라."
        )
        return 0, _stop_output(stdin, reason)

    # 정지 직전 사용자 발화를 transcript 에서 추출해 handoff "다음 intent" thread-tail
    # 슬롯에 자동 채운다(T-0047). 추출은 fail-soft("" 폴백) — 못 뽑아도 정지는 유효.
    thread_tail = (
        ctx_guard.extract_thread_tail(transcript)
        if isinstance(transcript, str) and transcript
        else ""
    )
    if handoff_fn is not None:
        # 테스트 DI seam — 주입 runner 는 (root, pct) 2인자 계약 유지(하위호환).
        handoff_rc = handoff_fn(root, remaining)
    else:
        handoff_rc = run_handoff(root, remaining, thread_tail=thread_tail)
    # 멱등 marker 는 handoff *성공*(rc0=박제 계약) 시에만 남긴다. 실패면 marker 없이 차단만 →
    # 다음 훅 호출이 handoff 를 재시도(미박제 상태로 "이미 박제됨" 처리되는 버그 방지).
    if handoff_rc == 0:
        _mark_triggered(root, session_id)
    reason = build_stop_reason(used, remaining, handoff_rc)
    return 0, _stop_output(stdin, reason)


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
