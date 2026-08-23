#!/usr/bin/env python3
"""claude ctx 임계 비차단 넛지 + compaction snapshot 훅.

Claude Code 가 PreToolUse(도구 실행 전), UserPromptSubmit(prompt 처리 전),
PostCompact(compaction 완료 후)에서 호출한다.
한 스크립트가 stdin 의 ``hook_event_name`` 으로 분기. 훅 입력엔 ``context_window`` 가 **없을 수 있어**
(statusline 전용) — 그래서 훅은 ``transcript_path`` JSONL 을 읽어 컨텍스트 점유를 자체 산출한다.

세 밴드(nudge/nudge2/stop) 모두 모델-facing 비차단 안내를 PreToolUse/UserPromptSubmit
``additionalContext`` 로 주입한다. stop 밴드는 구 차단형 대신 최종 넛지이며 `.final` marker 로
사이클당 한 번만 주입한다. PostCompact 경계에서는 엔진 snapshot 최종 텍스트를 payload marker에
저장하고 checkpoint 골격을 생성한 뒤 `.nudge`/`.nudge2`/`.final` marker를 재무장한다. 직후 첫
PreToolUse/UserPromptSubmit이 payload를 verbatim additionalContext로 1회 주입하고 marker를 소거한다.
훅은 deny/block/회전 marker 를 만들지 않는다.

서브에이전트(sidechain) 면제는 유지한다. checkpoint 지시는 메인 PM 세션 전용이며, transcript 의
``isSidechain`` 필드가 확실히 true 일 때만 면제한다(모호하면 메인 취급).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_guard  # noqa: E402  (같은 디렉토리 공유 코어)

# 멱등 marker 디렉토리 (git-ignored 상태 영역 — .project_manager/.gitignore 가 .local/ 커버).
# 세션 id 별 1파일. 채택자가 `git add -A` 해도 세션 marker 가 커밋되지 않게 이미-ignored 경로 사용.
_MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"
_SNAPSHOT_TIMEOUT_SECONDS = 3.0
_CHECKPOINT_TIMEOUT_SECONDS = 5.0
_PENDING_CHECKPOINT_DIAGNOSTIC = "<!-- ctx-checkpoint-pending: append-failed -->\n"
_CHECKPOINT_DIAGNOSTIC_APPEND_FAILED_SIGNAL = "[pm-checkpoint] ctx-diagnostic-append-failed"


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


def _snapshot_marker_path(root: Path, session_id: str) -> Path:
    return root / _MARKER_DIR / f"compact-snapshot.{session_id}"


def _arm_snapshot(root: Path, session_id: str, payload: str) -> bool:
    """PostCompact snapshot payload를 원자 교체해 다음 주입 가능 채널을 무장한다."""
    if not payload:
        return False
    marker = _snapshot_marker_path(root, session_id)
    try:
        existing = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = None
    except (OSError, UnicodeError):
        # 기존 marker 상태를 모르면 보수적으로 보존한다. replace가 성공해도 진단을 지울 수 있다.
        return False
    if (
        existing is not None
        and _PENDING_CHECKPOINT_DIAGNOSTIC in existing
        and "[ctx-window-mismatch]" not in payload
    ):
        # PreCompact append 실패 시 만든 전달 payload는 원장에 아직 없는 진단의 유일한 사본이다.
        return True
    temp = marker.with_name(f".{marker.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, marker)
        return True
    except OSError:
        return False
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _take_snapshot(root: Path, session_id: str) -> str | None:
    """payload marker를 rename으로 배타 선점하고 읽은 뒤 소거한다 (채널 합산 1회)."""
    marker = _snapshot_marker_path(root, session_id)
    claimed = marker.with_name(f".{marker.name}.{os.getpid()}.{uuid.uuid4().hex}.claimed")
    try:
        os.replace(marker, claimed)
    except OSError:
        return None
    try:
        payload = claimed.read_text(encoding="utf-8")
        return payload or None
    except (OSError, UnicodeError):
        return None
    finally:
        try:
            claimed.unlink(missing_ok=True)
        except OSError:
            pass


def _hook_cwd(stdin: dict, root: Path) -> Path:
    raw = stdin.get("cwd")
    return Path(raw).resolve(strict=False) if isinstance(raw, str) and raw.strip() \
        else Path(root).resolve(strict=False)


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
    remaining = ctx_guard.remaining_pct(used_pct)
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
        "--remaining-pct",
        str(remaining),
        "--stop-pct",
        str(thresholds["stop_pct"]),
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
    checkpoint = "python3 .project_manager/tools/pm_log.py checkpoint --task <이름>"
    if band == "final":
        checkpoint += " --trigger compaction"
    return (
        f"[{labels[band]}] 컨텍스트 사용 {used_pct}% (잔여 {remaining}%). "
        f"auto-compact 관측 구간이다. `{checkpoint}` 기록을 사용할 수 있다. "
        "`<이름>`에는 현재 task 이름을 사용한다. "
        "압축은 자동이고 세션은 그대로 이어진다. "
        "핸드오프는 사용자 명시 지시로만 한다. "
        "상세 ctx 연속성 정책은 pm_log.py ctx-guidance 엔진에서 복구한다."
    )


def _build_snapshot(
    root: Path,
    stdin: dict,
    *,
    ctx_band_missed: bool = False,
    ctx_window_tokens: int = 0,
    ctx_observed_tokens: int = 0,
    harness: str | None = None,
) -> str | None:
    """엔진 builder stdout만 받는다. 렌더는 pm_log.py 단일 소유, stderr는 훅 프로토콜 밖으로 폐기.

    Builder command token: ``pm_log.py snapshot``.
    """
    engine = _pm_log_path(root)
    if not engine.is_file():
        return None
    command = [
        sys.executable, str(engine), "snapshot", "--cwd", str(_hook_cwd(stdin, root)),
    ]
    if ctx_band_missed:
        command.append("--ctx-band-missed")
        if ctx_window_tokens > 0:
            command.extend(("--ctx-window-tokens", str(ctx_window_tokens)))
        if ctx_observed_tokens > 0:
            command.extend(("--ctx-observed-tokens", str(ctx_observed_tokens)))
        if isinstance(harness, str) and harness.strip():
            command.extend(("--harness", harness.strip()))
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
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def _compaction_boundary_id(stdin: dict, phase: str) -> str | None:
    """Claude transcript의 compact boundary 순번으로 pre/post 공통 경계 ID를 만든다."""
    transcript = stdin.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        lines = Path(transcript).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    count = 0
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (
            isinstance(entry, dict)
            and entry.get("type") == "system"
            and entry.get("subtype") == "compact_boundary"
        ):
            count += 1
    ordinal = count + 1 if phase == "pre" else max(count, 1)
    return f"claude-{_session_id(stdin)}-{ordinal}"


def _create_checkpoint(
    root: Path,
    stdin: dict,
    *,
    phase: str,
    breadcrumb: bool = False,
    ctx_band_checked: bool = False,
    ctx_band_missed: bool = False,
    ctx_window_tokens: int = 0,
    ctx_observed_tokens: int = 0,
    harness: str | None = None,
) -> bool:
    """compaction 골격 생성 성공 여부. 출력은 흡수하되 실패를 성공으로 합치지 않는다."""
    engine = _pm_log_path(root)
    if not engine.is_file():
        return False
    command = [
        sys.executable, str(engine), "checkpoint", "--trigger", "compaction",
        "--cwd", str(_hook_cwd(stdin, root)), "--phase", phase,
    ]
    raw_sid = stdin.get("session_id") or stdin.get("sessionId")
    if isinstance(raw_sid, str) and raw_sid.strip():
        command.extend(("--session-id", _session_id(stdin)))
    boundary_id = _compaction_boundary_id(stdin, phase)
    if boundary_id is not None:
        command.extend(("--boundary-id", boundary_id))
    if breadcrumb:
        command.append("--breadcrumb")
    legacy_command = list(command)
    if ctx_band_checked:
        command.append("--ctx-band-checked")
    if ctx_band_missed:
        command.append("--ctx-band-missed")
        if ctx_window_tokens > 0:
            command.extend(("--ctx-window-tokens", str(ctx_window_tokens)))
        if ctx_observed_tokens > 0:
            command.extend(("--ctx-observed-tokens", str(ctx_observed_tokens)))
        if isinstance(harness, str) and harness.strip():
            command.extend(("--harness", harness.strip()))
    try:
        result = subprocess.run(
            command,
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=_CHECKPOINT_TIMEOUT_SECONDS,
            check=False,
        )
        succeeded = (
            result.returncode == 0
            and _CHECKPOINT_DIAGNOSTIC_APPEND_FAILED_SIGNAL
            not in (getattr(result, "stderr", "") or "")
        )
        # 진단 capability가 없는 구 엔진이어도 compaction checkpoint 자체는 보존한다. 다만 신형
        # 진단 append는 실패했으므로 False를 돌려 snapshot 전달 fallback도 함께 무장한다.
        if result.returncode != 0 and command != legacy_command:
            subprocess.run(
                legacy_command,
                cwd=str(root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CHECKPOINT_TIMEOUT_SECONDS,
                check=False,
            )
        return succeeded
    except (OSError, subprocess.TimeoutExpired):
        return False


def capture_precompact(stdin: dict, root: Path) -> int:
    """sidechain 판정 뒤 PM-home 엔진에서 breadcrumb와 checkpoint를 함께 기록한다."""
    transcript = stdin.get("transcript_path")
    if isinstance(transcript, str) and transcript and ctx_guard.transcript_is_sidechain(transcript):
        return 0
    session_id = _session_id(stdin)
    # Claude PreCompact의 trigger가 auto라고 명시된 경우만 실제 auto-compact 지점의
    # 관측으로 취급한다. manual /compact와 trigger 부재·미지값은 밴드 불일치에 대한
    # 정보가 없으므로 breadcrumb/checkpoint만 남기고 진단은 보수적으로 침묵한다.
    auto_compact = stdin.get("trigger") == "auto"
    band_fired = False
    observed_tokens = 0
    window_tokens = 0
    if auto_compact:
        band_fired = any(
            marker.is_file()
            for marker in (
                _nudge_marker_path(root, session_id),
                _nudge2_marker_path(root, session_id),
                _final_marker_path(root, session_id),
            )
        )
        transcript_path = stdin.get("transcript_path")
        observed_tokens = (
            ctx_guard.context_tokens_from_transcript(transcript_path)
            if isinstance(transcript_path, str) and transcript_path else 0
        )
        window_tokens = ctx_guard.resolve_budget(ctx_guard.load_local_config(root), "claude")
    checkpointed = _create_checkpoint(
        root,
        stdin,
        phase="pre",
        breadcrumb=True,
        ctx_band_checked=auto_compact,
        ctx_band_missed=auto_compact and not band_fired,
        ctx_window_tokens=window_tokens,
        ctx_observed_tokens=observed_tokens,
        harness="claude",
    )
    if auto_compact and not band_fired and not checkpointed:
        # append 실패여도 세션은 막지 않는다. 엔진이 렌더한 동일 진단을 기존 snapshot 채널에 두어
        # 다음 prompt 또는 쓰기 복구 뒤 경계가 전달/재시도할 수 있게 한다.
        fallback = _build_snapshot(
            root,
            stdin,
            ctx_band_missed=True,
            ctx_window_tokens=window_tokens,
            ctx_observed_tokens=observed_tokens,
            harness="claude",
        )
        armed = bool(fallback) and _arm_snapshot(
            root, session_id, _PENDING_CHECKPOINT_DIAGNOSTIC + fallback,
        )
        print(
            "[ctx-checkpoint-append-failure] PreCompact checkpoint append 실패 — "
            + ("pending 진단 payload로 보존" if armed else "pending 진단 payload 무장도 실패"),
            file=sys.stderr,
        )
    return 0


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
    """PostCompact 경계에서 원칙 recall marker 를 지워 다음 사이클을 재무장한다."""
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


def evaluate(stdin: dict, root: Path, conf: dict) -> tuple[int, dict | None]:
    """훅 핵심 진입 — ctx 밴드 판정에 원칙 recall 채널을 합본한다(둘 다 비차단).

    ctx 밴드(snapshot/nudge/nudge2/stop)는 `_evaluate_ctx_bands` 가 그대로 소유한다. recall 은
    독립 채널이라 밴드 판정과 무관하게 매칭되면 additionalContext 에 누적된다 — 레지스트리 부재·
    파손이어도 ctx 밴드 출력은 그대로 실린다(비차단 계약)."""
    rc, output = _evaluate_ctx_bands(stdin, root, conf)
    transcript = stdin.get("transcript_path")
    if isinstance(transcript, str) and transcript and ctx_guard.transcript_is_sidechain(transcript):
        return rc, output
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "PostCompact":
        _rearm_principle_recall(stdin, root)
        return rc, output
    if event in {"PreToolUse", "UserPromptSubmit"}:
        recall_text = _principle_recall_text(stdin, root)
        if recall_text:
            output = _merge_additional_context(output, event, recall_text)
    return rc, output


def _evaluate_ctx_bands(stdin: dict, root: Path, conf: dict) -> tuple[int, dict | None]:
    """ctx 밴드 판정 — (rc, output|None). 모든 경로는 비차단이다."""
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
        # 압축 실패/차단 시에는 재무장하지 않는다. transcript 사용률을 판정하지 않고 marker만
        # best-effort 정리한다. 정상 경로에서 ctx-window-mismatch의 단일 진실은 append-only
        # checkpoint log이고 snapshot은 그 최신 밴드 평가에서 매번 만드는 파생물이다. 원장 판독
        # 실패는 pm_log가 무진단과 구분해 payload 생성을 거부하므로 기존 marker를 보존한다. append
        # 실패 때만 snapshot 채널이 pending 진단의 임시 단일 진실이 된다(subprocess 회귀 대응).
        _rearm_cycle(root, session_id)
        snapshot = _build_snapshot(root, stdin)
        if snapshot and not _arm_snapshot(root, session_id, snapshot):
            print(
                "[ctx-snapshot-rearm-failure] snapshot 재무장 실패 — 기존 payload/append-only 진단을 다음 경계에 보존",
                file=sys.stderr,
            )
        elif not snapshot and _snapshot_marker_path(root, session_id).exists():
            print(
                "[ctx-snapshot-rearm-failure] snapshot 재생성 불가 — 기존 armed payload를 다음 경계에 보존",
                file=sys.stderr,
            )
        if not _create_checkpoint(root, stdin, phase="post"):
            print(
                "[ctx-checkpoint-append-failure] PostCompact checkpoint append 실패 — 다음 경계에서 재시도",
                file=sys.stderr,
            )
        return 0, None

    if event in {"PreToolUse", "UserPromptSubmit"}:
        snapshot = _take_snapshot(root, session_id)
        if snapshot is not None:
            return 0, nudge_output(stdin, snapshot)

    # 분모 = 해소된 claude 예산(per-harness) — statusLine 과 같은 예산.
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
        output = nudge_output(
            stdin,
            _build_ctx_guidance(
                root, band="nudge", used_pct=used, thresholds=thresholds,
            ),
        )
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
        output = nudge_output(
            stdin,
            _build_ctx_guidance(
                root, band="nudge2", used_pct=used, thresholds=thresholds,
            ),
        )
        if output is None:
            return 0, None
        if not _claim_marker(
            _nudge2_marker_path(root, session_id), "ctx-nudge2 injected\n"
        ):
            return 0, None
        return 0, output

    # state == "stop" — 구 차단형 대신 최종 비차단 넛지(사이클당 1회·`.final`).
    output = nudge_output(
        stdin,
        _build_ctx_guidance(
            root, band="final", used_pct=used, thresholds=thresholds,
        ),
    )
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
    if argv is None:
        argv = sys.argv[1:]
    if "--precompact-capture" in argv:
        return capture_precompact(stdin, root)
    conf = ctx_guard.load_local_config(root)
    rc, output = evaluate(stdin, root, conf)
    if output is not None:
        sys.stdout.write(json.dumps(output))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
