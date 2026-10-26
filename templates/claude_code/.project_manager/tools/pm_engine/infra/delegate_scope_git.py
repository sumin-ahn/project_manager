"""위임 범위의 Git 상태와 티켓 입력 (층: infra)."""
from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from collections.abc import Sequence
from pm_engine.domain import delegate_run_rules
from pm_engine.domain import delegate_scope_rules
from pm_engine.infra import repo_coordinates
import subprocess
from pm_engine.infra import ticket_store



def _ticket_touches_error(ticket_id: str, exc: Exception) -> delegate_scope_rules.DelegateScopeError:
    """ticket 조회의 로드/호출 실패를 같은 계약 오류 형상으로 감싼다."""
    return delegate_scope_rules.DelegateScopeError(f"ticket touches 읽기 실패({ticket_id}): {exc}")


def _default_git_runner(cwd: Path, args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
    )
    return result.returncode, result.stdout


def resolve_workspace_root(
    workspace: Path | str,
    *,
    run_git: Callable[[Path, list[str]], tuple[int, str]] | None = None,
) -> Path:
    """``--cwd``가 repo **하위 디렉토리**여도 판정 기준을 git toplevel로 맞춘다.

    status/해시 캡처와 touches 좌표 정규화가 서로 다른 루트를 쓰면 경로 비교가 어긋나 판정이
    통째로 꺼진다(하위 디렉토리 위임에서 실측된 사각). 해소 실패는 :class:`DelegateScopeError`로
    올려 호출부가 loud degrade 하게 한다.
    """
    cwd = Path(workspace).resolve()
    runner = run_git or _default_git_runner
    try:
        rc, output = runner(cwd, ["rev-parse", "--show-toplevel"])
    except Exception as exc:  # noqa: BLE001 — 호출부가 loud 경고로 바꿀 단일 오류 계약.
        raise delegate_scope_rules.DelegateScopeError(f"git toplevel 해소 실패: {exc}") from exc
    root = output.strip()
    if rc != 0 or not root:
        raise delegate_scope_rules.DelegateScopeError(f"git toplevel 해소 실패(rc={rc}): {cwd}")
    return Path(root).resolve()


def _capture_modes(cwd: Path, runner: Callable[[Path, list[str]], tuple[int, str]]) -> tuple[tuple[str, str], ...]:
    """porcelain v2 지문 캡처 — 실패하면 빈 값(상태코드/해시 비교로 강등)."""
    rc, output = runner(cwd, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
    if rc != 0:
        return ()
    return delegate_scope_rules.parse_porcelain_v2_modes(output)


def _capture_head(cwd: Path, runner: Callable[[Path, list[str]], tuple[int, str]]) -> str:
    """HEAD sha — 커밋 없는 repo/해소 실패는 빈 문자열(그 경우 커밋 판정은 비활성)."""
    rc, output = runner(cwd, ["rev-parse", "HEAD"])
    return output.strip() if rc == 0 else ""


def _hash_dirty_paths(
    cwd: Path,
    paths: Sequence[str],
    runner: Callable[[Path, list[str]], tuple[int, str]],
) -> tuple[tuple[str, str], ...]:
    """dirty 경로의 blob 해시를 산출한다(실재하는 파일만·배치 실패는 파일 단위로 재시도).

    해시는 상태코드 비교를 **보강**하는 신호라 산출 실패가 판정 전체를 죽이면 안 된다 — 못 구한
    경로는 목록에서 빠지고 그 경로는 상태코드만으로 비교된다. ``HASH_MAX_FILE_BYTES`` 초과 파일은
    건너뛴다(대용량 산출물은 상태코드로 이미 보인다).

    ``git hash-object``는 (``core.autocrlf``·clean 필터 등) **정규화를 거친** blob 을 내므로, 줄끝만
    바뀐 재수정은 설정에 따라 같은 해시로 보일 수 있다 — 그 경우 상태코드/mode 신호가 백스톱이다.
    """
    targets = [path for path in paths if _hashable(cwd / path)]
    digests: list[tuple[str, str]] = []
    for start in range(0, len(targets), delegate_scope_rules.HASH_BATCH_SIZE):
        batch = targets[start:start + delegate_scope_rules.HASH_BATCH_SIZE]
        hashed = _hash_object(cwd, batch, runner)
        if hashed is None:
            for single in batch:  # 배치 실패(권한·삭제 경합 등) → 나머지를 살린다.
                one = _hash_object(cwd, [single], runner)
                if one is not None:
                    digests.extend(one)
            continue
        digests.extend(hashed)
    return tuple(digests)


def _hashable(target: Path) -> bool:
    """해시 대상 판정 — 실재하는 파일이고 상한 이하 크기."""
    try:
        if not target.is_file():
            return False
        return target.stat().st_size <= delegate_scope_rules.HASH_MAX_FILE_BYTES
    except OSError:
        return False


def _hash_object(
    cwd: Path,
    batch: Sequence[str],
    runner: Callable[[Path, list[str]], tuple[int, str]],
) -> list[tuple[str, str]] | None:
    """``git hash-object -- <paths>`` 1회. 실패/출력 불일치는 None(호출부가 강등)."""
    if not batch:
        return []
    rc, output = runner(cwd, ["hash-object", "--", *batch])
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if rc != 0 or len(lines) != len(batch):
        return None
    return list(zip(batch, lines))


def capture_worktree_state(workspace: Path | str) -> delegate_scope_rules.WorktreeState:
    """untracked 파일까지 펼친 worktree 상태 + dirty 경로 내용 해시를 캡처한다."""
    cwd = Path(workspace).resolve()
    runner = _default_git_runner
    try:
        rc, output = runner(
            cwd,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )
    except Exception as exc:  # noqa: BLE001 — 호출부가 loud 경고로 바꿀 단일 오류 계약.
        raise delegate_scope_rules.DelegateScopeError(f"git status 실행 실패: {exc}") from exc
    if rc != 0:
        raise delegate_scope_rules.DelegateScopeError(f"git status 실패(rc={rc}): {cwd}")
    entries = delegate_scope_rules.parse_porcelain_z(output)
    digests = _hash_dirty_paths(cwd, [entry.path for entry in entries], runner)
    return delegate_scope_rules.WorktreeState(entries, digests, _capture_modes(cwd, runner), _capture_head(cwd, runner))


def content_signal_missing(state: delegate_scope_rules.WorktreeState, workspace: Path | str) -> bool:
    """해시 대상이 있는데 지문을 하나도 못 구했으면 True(재수정 감지 불가 = 조용한 강등 방지 신호)."""
    if state.digests:
        return False
    cwd = Path(workspace)
    return any(_hashable(cwd / entry.path) for entry in state.entries)


def committed_paths(
    before: delegate_scope_rules.WorktreeState,
    after: delegate_scope_rules.WorktreeState,
    *,
    workspace: Path | str,
    run_git: Callable[[Path, list[str]], tuple[int, str]] | None = None,
) -> tuple[str, ...]:
    """위임 중 HEAD 가 움직였으면 그 커밋 범위에서 변한 경로를 낸다.

    범위 밖 파일을 고치고 **커밋**하면 전·후 worktree 가 둘 다 clean 이라 상태 비교로는 흔적이 0
    이다. 위임 역할 프롬프트가 커밋을 금지하지만 이 가드의 존재 이유가 바로 "그 금지를 믿지 않는
    것"이므로, HEAD 이동 시 트리 diff 를 판정 입력에 합산한다. diff 산출 실패는 빈 값(상태 비교
    결과는 그대로 살아 있다).
    """
    if not delegate_scope_rules.head_moved(before, after) or not after.head:
        return ()
    runner = run_git or _default_git_runner
    args = ["diff-tree", "-r", "--no-commit-id", "--name-only", "-z"]
    args += [before.head, after.head] if before.head else ["--root", after.head]
    rc, output = runner(Path(workspace), args)
    if rc != 0:
        return ()
    return tuple(sorted({token for token in output.split("\0") if token}))


def ticket_touches(
    home,
    ticket_id: str,
) -> tuple[str, ...]:
    """board의 ticket frontmatter에서 문자열 ``touches``를 읽는다.

    위임 훅이 프롬프트 문장을 재파싱하지 않도록 ticket ID를 구조화 입력으로 받는다. ticket 부재,
    잘못된 frontmatter는 판정 입력 불능이므로 :class:`DelegateScopeError`다.

    board 조회는 ``<id>-*.md`` **glob** 이라 ID에 메타문자가 들어오면 다른 ticket의 ``touches``를
    집어올 수 있다(엉뚱한 허용 범위 = 조용한 오판). 그래서 메타문자/경로 구분자를 먼저 거부하고,
    조회된 frontmatter의 ``id``가 요청 ID와 **정확히 일치**하는지 재확인한다.
    """
    if not str(ticket_id).strip() or delegate_scope_rules._TICKET_ID_UNSAFE.search(str(ticket_id)):
        raise delegate_scope_rules.DelegateScopeError(
            f"ticket id 형식 거부(glob 메타/경로 구분자 포함): {ticket_id!r}"
        )
    ticket_id = str(ticket_id).strip()
    try:
        _status, ticket_path = ticket_store.find_ticket(home, ticket_id)
        frontmatter, _body = ticket_store.load_ticket(ticket_path)
    except Exception as exc:  # noqa: BLE001 — 잘못된 ticket 입력을 허용 0으로 오인하지 않는다.
        raise _ticket_touches_error(ticket_id, exc) from exc

    found_id = str(frontmatter.get("id") or "").strip()
    if found_id != ticket_id:
        raise delegate_scope_rules.DelegateScopeError(
            f"ticket id 불일치: 요청={ticket_id!r} 조회={found_id!r} ({ticket_path})"
        )

    raw = frontmatter.get("touches")
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, list):
        return tuple(
            item.strip() for item in raw
            if isinstance(item, str) and item.strip()
        )
    if raw is None:
        return ()
    raise delegate_scope_rules.DelegateScopeError(f"ticket touches 형식이 문자열/목록이 아니다: {ticket_id}")


def allowed_paths(
    touches: Sequence[str],
    *,
    role: str,
    pm_root: Path | str,
    workspace: Path | str,
    write_roles: Iterable[str] | None = None,
    on_drop: Callable[[str, str], None] | None = None,
) -> tuple[str, ...]:
    """역할과 ticket ``touches``를 repo-relative 허용 경로로 바꾼다.

    ``touches``는 **항목별로** 정규화한다 — multi-PM ticket은 여러 슬롯을 touch 할 수 있고,
    리스트 단위로 정규화하면 이 workspace와 무관한 한 항목 때문에 전체가 예외가 되어 허용 경로가
    통째로 사라진다(경고 폭증 또는 판정 실종). 해소 불가/타 슬롯 항목은 **드롭**하되 ``on_drop``
    으로 호출부가 loud 하게 알린다. 전부 드롭되면 허용 0(보수 방향)이다.
    """
    roles = frozenset(write_roles) if write_roles is not None else delegate_run_rules.WRITE_ROLES
    if role not in roles or not touches:   # 쓰기 역할이 아니면 허용 0(신규 역할 = 안전 방향)
        return delegate_scope_rules.ALWAYS_ALLOWED_PATHS

    canonical: set[str] = set()
    for item in touches:
        try:
            normalized = repo_coordinates.normalize_repo_paths(
                [item],
                pm_root=Path(pm_root),
                workspace=Path(workspace),
            )
        except repo_coordinates.RepoCoordinateError as exc:  # 해소 불가 touches 항목만 드롭(on_drop 보고).
            if on_drop is not None:
                on_drop(item, str(exc))
            continue
        for path in normalized:
            value = repo_coordinates.canonicalize_path_notation(str(path)).rstrip("/")
            if value and value != ".":
                canonical.add(value)
    return tuple(sorted(canonical))


def out_of_scope_changes(
    before: delegate_scope_rules.WorktreeState,
    after: delegate_scope_rules.WorktreeState,
    *,
    touches: Sequence[str],
    role: str,
    pm_root: Path | str,
    workspace: Path | str,
    write_roles: Iterable[str] | None = None,
    on_drop: Callable[[str, str], None] | None = None,
) -> tuple[str, ...]:
    """위임 중 상태가 바뀐 경로(+커밋된 경로) 중 허용 범위 밖 경로만 반환한다."""
    allowed = allowed_paths(
        touches,
        role=role,
        pm_root=pm_root,
        workspace=workspace,
        write_roles=write_roles,
        on_drop=on_drop,
    )
    changed = set(delegate_scope_rules.changed_status_paths(before, after))
    changed.update(committed_paths(before, after, workspace=workspace))
    return tuple(
        path for path in sorted(changed)
        if not delegate_scope_rules._covered(path, allowed)
    )
