#!/usr/bin/env python3
"""claude ctx 임계 비차단 넛지 훅 (PreToolUse·UserPromptSubmit·PostCompact · ADR-0081).

Claude Code 가 PreToolUse(도구 실행 전), UserPromptSubmit(prompt 처리 전),
PostCompact(compaction 완료 후)에서 호출한다.
한 스크립트가 stdin 의 ``hook_event_name`` 으로 분기. 훅 입력엔 ``context_window`` 가 **없을 수 있어**
(statusline 전용) — 그래서 훅은 ``transcript_path`` JSONL 을 읽어 컨텍스트 점유를 자체 산출한다.

세 밴드(nudge/nudge2/stop) 모두 모델-facing 비차단 안내를 PreToolUse/UserPromptSubmit
``additionalContext`` 로 주입한다. stop 밴드는 구 차단형 대신 최종 넛지이며 `.final` marker 로
사이클당 한 번만 주입한다. PostCompact 경계에서 `.nudge`/`.nudge2`/`.final` marker 를 기계적으로
지워 다음 상승 사이클을 재무장한다. 완료 후 발화라 압축 실패/차단 시 재무장하지 않으며,
compaction 뒤 유효한 ok 실측도 보조 재무장 신호로 유지한다.
훅은 deny/block/회전 marker 를 만들지 않는다.

서브에이전트(sidechain) 면제는 유지한다. checkpoint 지시는 메인 PM 세션 전용이며, transcript 의
``isSidechain`` 필드가 확실히 true 일 때만 면제한다(모호하면 메인 취급).
"""
from __future__ import annotations

import json
import os
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


# ── 사이클별 넛지 멱등 marker (ADR-0081) ───────────────────────────────────
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


def _rearm_cycle(root: Path, session_id: str) -> None:
    """PostCompact 완료 경계/유효한 ok 복귀 시 marker 를 지워 다음 상승 사이클을 재무장."""
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


def evaluate(stdin: dict, root: Path, conf: dict) -> tuple[int, dict | None]:
    """훅 핵심 — (rc, output|None). 모든 경로는 비차단이다."""
    transcript = stdin.get("transcript_path")
    # 서브에이전트(sidechain) 면제는 다른 모든 밴드 판정보다 먼저 적용한다. checkpoint 서사는
    # 메인 PM 세션만 쓰므로 서브에이전트는 marker 없이 통과해 native auto-compact 로 자체 정리한다.
    # 면제는 sidechain 이 확실할 때만 — 신호 부재/모호면 False(메인 취급).
    if isinstance(transcript, str) and transcript and ctx_guard.transcript_is_sidechain(transcript):
        return 0, None
    session_id = _session_id(stdin)
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "PostCompact":
        # compaction 완료 경계가 새 상승 사이클의 정본 신호다. PostCompact 는 완료 후 발화하므로
        # 압축 실패/차단 시에는 재무장하지 않는다. transcript 사용률을 판정하지 않고 marker 만
        # best-effort 정리한 뒤 무출력·비차단 통과한다.
        _rearm_cycle(root, session_id)
        return 0, None

    # 분모 = 해소된 claude 예산(ADR-0041·per-harness) — statusLine 과 같은 예산.
    window = ctx_guard.resolve_budget(conf, "claude")
    thresholds = ctx_guard.ctx_thresholds(conf)

    raw_tokens = (
        ctx_guard.context_tokens_from_transcript(transcript)
        if isinstance(transcript, str) and transcript
        else 0
    )
    used = ctx_guard.context_used_pct_from_tokens(raw_tokens, window)
    state = ctx_guard.classify(used, thresholds)
    if state == "ok":
        # 정수 used=0은 큰 예산에서 작은 양의 측정값도 될 수 있다. raw 토큰이 양수일 때만 유효한
        # ok 실측으로 재무장하고, transcript 부재·읽기 실패·usage 미검출(raw 0)은 marker를 보존한다.
        if raw_tokens > 0:
            _rearm_cycle(root, session_id)
        return 0, None

    if state == "nudge":
        # 모델-facing 비차단 안내 주입. marker 는 채널과 무관하게 사이클당 1회이며,
        # PreToolUse/UserPromptSubmit 중 먼저 실제 주입한 채널이 소비한다.
        output = nudge_output(stdin, ctx_guard.build_nudge_guidance(used, thresholds))
        if output is None:
            return 0, None
        if not _claim_marker(
            _nudge_marker_path(root, session_id), "ctx-nudge injected\n"
        ):
            return 0, None
        return 0, output

    if state == "nudge2":
        # 강화 넛지. 멱등(사이클 1회·.nudge2 marker). 1단(.nudge)과 독립이라 1단 창을
        # 건너뛴 세션도 2단은 발화한다.
        # nudge 와 동일하게 두 채널이 공유 marker 로 사이클당 1회만 소비한다.
        output = nudge_output(stdin, ctx_guard.build_nudge2_guidance(used, thresholds))
        if output is None:
            return 0, None
        if not _claim_marker(
            _nudge2_marker_path(root, session_id), "ctx-nudge2 injected\n"
        ):
            return 0, None
        return 0, output

    # state == "stop" — 구 차단형 대신 최종 비차단 넛지(사이클당 1회·`.final`).
    output = nudge_output(stdin, ctx_guard.build_final_guidance(used, thresholds))
    if output is None:
        return 0, None
    if not _claim_marker(
        _final_marker_path(root, session_id), "ctx-final nudge injected\n"
    ):
        return 0, None
    return 0, output


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
