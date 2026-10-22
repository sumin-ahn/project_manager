"""Codex 위임 관측 로그 저장과 조회 (층: infra)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from pm_engine.domain import codex_envelope
from pm_engine.domain import delegate_channel_rules
import datetime as dt
from pm_engine.infra import fs
import json
import os
import time

_CODEX_OBSERVATION_RELATIVE_PATH = Path(
    ".project_manager/.local/delegate-channel/codex-observations.jsonl"
)
_CODEX_OBSERVATION_MAX_BYTES = 256 * 1024
_CODEX_OBSERVATION_MAX_FILES = 4


def _codex_audit_path(home, state_dir: Path | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir) / _CODEX_OBSERVATION_RELATIVE_PATH.name
    return home.root / _CODEX_OBSERVATION_RELATIVE_PATH


def _codex_audit_lock_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.lock")


def _codex_rotated_audit_paths(target: Path) -> list[Path]:
    candidates = [
        path
        for path in target.parent.glob(f"{target.stem}.*{target.suffix}")
        if path != target
    ]

    def sort_key(path: Path) -> tuple[int, str]:
        try:
            return path.stat().st_mtime_ns, path.name
        except OSError:
            return 0, path.name

    return sorted(candidates, key=sort_key)


def _codex_audit_paths(target: Path) -> list[Path]:
    paths = _codex_rotated_audit_paths(target)
    if target.is_file():
        paths.append(target)
    return paths


def _prune_codex_observation_logs(
    target: Path, *, max_bytes: int, max_files: int
) -> None:
    """Bound retained JSONL segments; the active append target is never pruned."""
    while True:
        paths = _codex_audit_paths(target)
        total_bytes = 0
        for path in paths:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
        if len(paths) <= max_files and total_bytes <= max_bytes * max_files:
            return
        rotated = [path for path in paths if path != target]
        if not rotated:
            return
        try:
            rotated[0].unlink()
        except FileNotFoundError:
            continue


def _bounded_observation_text(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _append_codex_observation(
    home, payload: Mapping[str, object],
    *,
    hook_event_name: str,
    status: str,
    reason: str,
    state_dir: Path | None = None,
) -> None:
    """Append one observation and rotate bounded JSONL segments under a lock."""
    target = _codex_audit_path(home, state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "hook_event_name": hook_event_name,
        "status": _bounded_observation_text(status, 128),
        "reason": _bounded_observation_text(reason, 4096),
        "session_id": _bounded_observation_text(payload.get("session_id"), 512),
        "turn_id": _bounded_observation_text(payload.get("turn_id"), 512),
        "agent_id": _bounded_observation_text(payload.get("agent_id"), 512),
        "role": _bounded_observation_text(
            codex_envelope._codex_spawn_role(payload) if hook_event_name == "PreToolUse" else "",
            256,
        ),
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    line_size = len(line.encode("utf-8"))
    max_bytes = _CODEX_OBSERVATION_MAX_BYTES
    max_files = _CODEX_OBSERVATION_MAX_FILES
    if max_bytes < 1 or max_files < 1:
        raise ValueError("Codex observation rotation bounds must be positive")
    if line_size > max_bytes:
        raise ValueError(
            f"Codex observation entry({line_size} bytes) exceeds segment bound({max_bytes})"
        )

    with fs.exclusive_file_lock(_codex_audit_lock_path(target), mode=0o600):
        try:
            current_size = target.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size and current_size + line_size > max_bytes:
            rotated = target.with_name(
                f"{target.stem}.{time.time_ns()}.{os.getpid()}{target.suffix}"
            )
            fs.atomic_replace(target, rotated)
        fs.append_atomic(target, line, mode=0o600)
        _prune_codex_observation_logs(
            target, max_bytes=max_bytes, max_files=max_files
        )


def _codex_observation_identity(
    entry: Mapping[str, object],
) -> tuple[str, str, str] | None:
    values = tuple(
        value.strip() if isinstance(value, str) else ""
        for value in (entry.get(field) for field in delegate_channel_rules.CODEX_CORRELATION_FIELDS)
    )
    if not all(values):
        return None
    return values


def scan_codex_observation_misses(
    home, *,
    observation_path: Path | str | None = None,
    state_dir: Path | None = None,
) -> list[dict[str, object]]:
    """Idempotently find SubagentStart rows lacking a prior allowed spawn row.

    Every retained JSONL segment participates.  The scan never renames, deletes,
    or marks an event, so repeated scans over the same bytes return the same rows.
    Live Codex payloads give the parent spawn and child start different turn IDs
    and omit ``agent_id`` from the spawn.  Pair them FIFO within ``session_id``;
    identify/deduplicate starts by the measured session/turn/agent triplet.
    """
    if observation_path is not None and state_dir is not None:
        raise ValueError("choose observation_path or state_dir, not both")
    target = (
        Path(observation_path)
        if observation_path is not None
        else _codex_audit_path(home, state_dir)
    )

    def scan_unlocked() -> list[dict[str, object]]:
        available_spawns: dict[str, int] = {}
        seen_starts: set[tuple[str, str, str]] = set()
        misses: list[dict[str, object]] = []
        for path in _codex_audit_paths(target):
            try:
                lines = fs.read_text_shared(path, encoding="utf-8").splitlines()
            except (FileNotFoundError, OSError, UnicodeError):
                continue
            for raw in lines:
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(entry, Mapping):
                    continue
                event = entry.get("hook_event_name")
                identity = _codex_observation_identity(entry)
                if event == "PreToolUse" and entry.get("status") == "decision_allow":
                    session_id = entry.get("session_id")
                    if isinstance(session_id, str) and session_id.strip():
                        session_id = session_id.strip()
                        available_spawns[session_id] = (
                            available_spawns.get(session_id, 0) + 1
                        )
                elif event == "SubagentStart":
                    if identity is not None and identity in seen_starts:
                        continue
                    if identity is not None:
                        seen_starts.add(identity)
                    session_id = identity[0] if identity is not None else ""
                    if session_id and available_spawns.get(session_id, 0):
                        available_spawns[session_id] -= 1
                    else:
                        misses.append(dict(entry))
        return misses

    # Normal hook writes create the persistent lock before the first JSONL row.
    # Avoid creating any file from a read-only lint scan when no hook has run.
    lock_path = _codex_audit_lock_path(target)
    if not lock_path.is_file():
        return scan_unlocked()
    with fs.exclusive_file_lock(lock_path, mode=0o600):
        return scan_unlocked()
