"""압축 경계 marker·FIFO 상태와 ctx 밴드 marker 저장."""
from __future__ import annotations

from pathlib import Path
import argparse
import contextlib
import datetime
from pm_engine.domain import ctx_recovery
from pm_engine.infra import fs
import os
import uuid


# ctx 밴드 marker 종류 — 사이클마다 밴드별로 한 번 선점한다(서로 독립).
CTX_BAND_MARKER_KINDS = ("nudge", "nudge2", "final")
# 압축 경계 처리 marker 종류의 접두. 뒤에 경계 ID 의 파일명 안전 키가 붙는다.
CTX_COMPACTION_MARKER_PREFIX = "compact-"


def _safe_marker_key(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in "-_")[:96] or "unknown"


def _ctx_marker_kind(kind: str) -> str:
    """ctx marker 종류를 파일명 접미로 — 밴드 셋 또는 ``compact-<경계>``(경계는 안전 키로)."""
    if kind in CTX_BAND_MARKER_KINDS:
        return kind
    boundary = kind[len(CTX_COMPACTION_MARKER_PREFIX):] if kind.startswith(CTX_COMPACTION_MARKER_PREFIX) else ""
    if not boundary:
        raise ValueError(f"ctx marker 종류가 아님: {kind!r}")
    return CTX_COMPACTION_MARKER_PREFIX + _safe_marker_key(boundary)


def ctx_marker_path(home, session: str, kind: str) -> Path:
    """세션의 ctx marker 경로 — 데이터 Home 작업 디렉터리의 marker 폴더 아래 ``<세션 키>.<종류>``."""
    marker_dir = home.local_dir / "ctx-stop"
    return marker_dir / f"{ctx_recovery.safe_session_id(session)}.{_ctx_marker_kind(kind)}"


def claim_ctx_marker(home, session: str, kind: str) -> bool:
    """ctx marker 를 ``O_EXCL`` 로 선점한다 — True 는 이번 호출의 선점, False 는 이미 있는 marker.

    이미 있는 파일(``FileExistsError``)만 정상 중복이다. 그 밖의 디렉터리 생성·파일 생성 오류는 그대로
    올린다."""
    path = ctx_marker_path(home, session, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def rearm_ctx_cycle(home, session: str) -> None:
    """세션의 밴드 marker 셋을 지워 다음 상승 사이클을 연다. 삭제 오류는 그대로 올린다."""
    for kind in CTX_BAND_MARKER_KINDS:
        ctx_marker_path(home, session, kind).unlink(missing_ok=True)


def _compaction_marker_path(home, task: str, session_id: str | None=None, boundary_id: str | None=None) -> Path:
    key = _safe_marker_key(session_id.strip()) if isinstance(session_id, str) and session_id.strip() \
        else f"task-{_safe_marker_key(task)}"
    boundary = _safe_marker_key(boundary_id.strip()) \
        if isinstance(boundary_id, str) and boundary_id.strip() else "unknown-boundary"
    # 압축 경계 marker는 데이터 Home의 .local/ctx-stop 아래에 둔다.
    marker_dir = home.local_dir / "ctx-stop"
    return (
        marker_dir /
        f"compact-checkpoint.{key}.{boundary}"
    )


def claim_compaction_checkpoint(
    marker: Path,
    *,
    phase: str,
) -> bool | None:
    """경계 marker를 단일 ``O_EXCL``로 선점한다.

    ``True``는 선점 성공, ``False``는 같은 경계의 실제 중복, ``None``은 장부 I/O 실패다.
    marker 경로가 boundary id를 포함하므로 시간창·stat·만료 unlink가 전혀 필요 없다.
    """
    marker = Path(marker)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return None
    try:
        os.write(fd, f"compaction checkpoint claimed\nphase={phase}\n".encode("utf-8"))
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    return True


def _compaction_scope_key(task: str, session_id: str | None) -> str:
    """compaction 상태 파일이 공유하는 세션 우선 scope key."""
    if isinstance(session_id, str) and session_id.strip():
        return _safe_marker_key(session_id.strip())
    return f"task-{_safe_marker_key(task)}"


def _implicit_boundary_state_path(home, task: str, session_id: str | None) -> Path:
    """구 어댑터의 scope prefix 또는 경계별 durable pending 파일 경로."""
    key = _compaction_scope_key(task, session_id)
    marker_dir = home.local_dir / "ctx-stop"
    return marker_dir / f"compact-boundary.{key}"


def _new_implicit_boundary_id() -> str:
    """archive·프로세스 재시작 뒤에도 재사용되지 않는 implicit boundary ID."""
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    return f"implicit-{stamp}-{uuid.uuid4().hex}"


def _write_implicit_boundary_state(prefix: Path, boundary_id: str) -> Path:
    """pre 경계를 덮어쓰지 않는 boundary별 pending 파일로 원자 생성한다."""
    path = prefix.with_name(f"{prefix.name}.{_safe_marker_key(boundary_id)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (boundary_id + "\n").encode("utf-8"))
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    return path


def _read_implicit_boundary_state(prefix: Path) -> tuple[str, Path] | None:
    """scope의 가장 오래된 유효 pending 경계를 FIFO로 고른다.

    checkpoint log lock 안에서 호출되므로 여러 PostCompact도 서로 다른 파일을 소비한다.
    """
    try:
        pending = sorted(prefix.parent.glob(f"{prefix.name}.*"))
    except OSError:
        return None
    for path in pending:
        try:
            boundary_id = fs.read_text_shared(path, encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if boundary_id.startswith("implicit-"):
            return boundary_id, path
    return None


def _clear_implicit_boundary_state(path: Path | None, boundary_id: str) -> None:
    """동일 ID를 가리킬 때만 pre/post hand-off pointer를 best-effort 소거한다."""
    if path is None:
        return
    try:
        if fs.read_text_shared(path, encoding="utf-8").strip() == boundary_id:
            path.unlink()
    except (OSError, UnicodeError):
        pass


def _resolve_compaction_boundary(home, args: argparse.Namespace, *, task: str, current_text: str) -> tuple[str | None, str | None, Path | None]:
    """명시 boundary를 우선하고 구 어댑터 pre/post에는 durable unique ID를 부여한다.

    ``current_text``는 호출 호환을 위해 받지만 ID 재료로 쓰지 않는다. 로그 archive가 entry를
    제거해도 영구 marker와 새 boundary가 충돌하지 않아야 하기 때문이다.
    """
    del current_text
    phase = getattr(args, "phase", None)
    explicit = getattr(args, "boundary_id", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip(), phase or "unspecified", None
    if phase not in {"pre", "post"}:
        return None, None, None

    state_prefix = _implicit_boundary_state_path(home, task, getattr(args, 'session_id', None))
    if phase == "pre":
        boundary_id = _new_implicit_boundary_id()
        try:
            state_path = _write_implicit_boundary_state(state_prefix, boundary_id)
        except OSError:
            # pending 장부 I/O 실패여도 pre checkpoint 자체는 고유 ID로 계속 기록한다.
            state_path = None
        return boundary_id, phase, state_path

    pending = _read_implicit_boundary_state(state_prefix)
    if pending is None:
        # pre가 없거나 pending 장부가 손상된 post도 과거 marker와 재충돌하지 않는다.
        boundary_id = _new_implicit_boundary_id()
        state_path = None
    else:
        boundary_id, state_path = pending
    return boundary_id, phase, state_path
