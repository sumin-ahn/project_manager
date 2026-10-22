"""등록된 native agent 카드의 안전한 판독 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import delegate_channel_rules
import os
import stat

CLAUDE_NATIVE_AGENT_CARDS: dict[tuple[str, str], Path] = {
    ("developer", "normal"): Path(".claude/agents/developer.md"),
    ("code-reviewer", "normal"): Path(".claude/agents/code-reviewer.md"),
    ("researcher", "normal"): Path(".claude/agents/researcher.md"),
    ("architect", "normal"): Path(".claude/agents/architect.md"),
    ("developer", "hard"): Path(f".claude/agents/{delegate_channel_rules.HARD_TIER_AGENT_NAME}.md"),
}


def _metadata_is_linklike(metadata: os.stat_result) -> bool:
    """Recognize POSIX symlinks and Windows reparse points without following.

    ``st_file_attributes`` is a Windows-only *optional* structseq field: POSIX
    builds do not expose the name at all, and a ``stat_result`` assembled from a
    bare 10-tuple exposes the name with the value ``None`` on Windows.  Both
    shapes expose no attribute bits, so the value is normalized before the
    bitwise test — applying ``&`` to ``None`` raised ``TypeError`` and leaked a
    code defect into the same warning channel as a genuine judgment failure.
    Every real ``os.stat``/``os.lstat``/``os.fstat`` result on Windows still
    carries the integer attributes, so the reparse-point check is unweakened.
    """
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", None)
    if not isinstance(attributes, int):
        attributes = 0
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _file_identity(metadata: os.stat_result, label: str) -> tuple[int, int]:
    """One filesystem object's identity for the pre-read replacement judgment.

    POSIX uses ``(st_dev, st_ino)``; CPython fills the same two fields on Windows
    from ``GetFileInformationByHandle`` (volume serial number + 128-bit file
    index), so the swap judgment is one implementation on both platforms.  A
    volume that reports no index (``st_ino == 0``: FAT/exFAT and some network
    mounts) cannot express identity at all — comparing zeros would silently
    accept a replacement, so this is a loud judgment failure, not a fallback.
    """
    if not metadata.st_ino:
        raise ValueError(
            f"파일 정체성(st_ino) 미제공 볼륨 — 교체 판정 불능 거부: {label}"
        )
    return (metadata.st_dev, metadata.st_ino)


def _secure_dir_fd_supported() -> bool:
    """Whether the interpreter exposes every primitive used by the strong path."""
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    supports_follow = getattr(os, "supports_follow_symlinks", set())
    return (
        os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.stat in supports_follow
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    )


def _read_fd_bytes(file_fd: int) -> str:
    """Decode only after the caller has completed every metadata check."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def _root_directory_identity(
    root: Path,
    expected: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Bind a lexical engine root to one non-link directory identity."""
    metadata = root.lstat()
    if _metadata_is_linklike(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("agent card engine root symlink/reparse/비-directory 거부")
    identity = _file_identity(metadata, "agent card engine root")
    if expected is not None and identity != expected:
        raise ValueError("agent card engine root 교체 거부")
    return identity


def _read_known_regular_file_dir_fd(
    root: Path,
    relative: Path,
    root_identity: tuple[int, int],
) -> str:
    """Strong POSIX path: component-wise dir-fd traversal with ``O_NOFOLLOW``."""
    directory_flag = os.O_DIRECTORY
    nofollow_flag = os.O_NOFOLLOW
    nonblock_flag = getattr(os, "O_NONBLOCK", 0)
    cloexec_flag = getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(
        os.fspath(root), os.O_RDONLY | directory_flag | nofollow_flag
    )
    try:
        opened_root = os.fstat(current_fd)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or _file_identity(opened_root, "agent card engine root") != root_identity
        ):
            raise ValueError("agent card engine root 교체 거부")
        for part in relative.parts[:-1]:
            before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            if _metadata_is_linklike(before) or not stat.S_ISDIR(before.st_mode):
                raise ValueError(
                    f"agent card 경로 symlink/reparse/비-directory 거부: {relative.as_posix()}"
                )
            child_fd = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow_flag | cloexec_flag,
                dir_fd=current_fd,
            )
            try:
                after = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(after.st_mode)
                    or _file_identity(before, part) != _file_identity(after, part)
                ):
                    raise ValueError(
                        f"agent card 경로 교체 거부: {relative.as_posix()}"
                    )
            except BaseException:
                # 판정 실패(교체·정체성 미제공)도 새로 연 fd 를 남기지 않는다.
                os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd

        leaf = relative.parts[-1]
        before = os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
        if (
            _metadata_is_linklike(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise ValueError(
                f"agent card symlink/reparse/hardlink/비-regular 거부: {relative.as_posix()}"
            )
        file_fd = os.open(
            leaf,
            os.O_RDONLY | nofollow_flag | nonblock_flag | cloexec_flag,
            dir_fd=current_fd,
        )
        try:
            after = os.fstat(file_fd)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or _file_identity(before, leaf) != _file_identity(after, leaf)
            ):
                raise ValueError(
                    f"agent card inode 교체/비-regular 거부: {relative.as_posix()}"
                )
            return _read_fd_bytes(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(current_fd)


def _read_known_regular_file_portable(
    root: Path,
    relative: Path,
    lexical_root: Path,
    root_identity: tuple[int, int],
) -> str:
    """Portable path for Windows/interpreters without dir-fd traversal.

    All components are lstat'd before open.  Parent identities and the leaf's
    regular/single-link identity are checked again after open but before the
    first content read, so a path replacement cannot redirect bytes into the
    warning channel.
    """
    _root_directory_identity(lexical_root, root_identity)
    _root_directory_identity(root, root_identity)
    parent_identities: list[tuple[Path, tuple[int, int]]] = []
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        observed = current.lstat()
        if _metadata_is_linklike(observed) or not stat.S_ISDIR(observed.st_mode):
            raise ValueError(
                f"agent card 경로 symlink/reparse/비-directory 거부: {relative.as_posix()}"
            )
        parent_identities.append((current, _file_identity(observed, part)))

    target = root / relative
    before = target.lstat()
    if (
        _metadata_is_linklike(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError(
            f"agent card symlink/reparse/hardlink/비-regular 거부: {relative.as_posix()}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    leaf_label = relative.as_posix()
    file_fd = os.open(os.fspath(target), flags)
    try:
        after = os.fstat(file_fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or _file_identity(before, leaf_label)
            != _file_identity(after, leaf_label)
        ):
            raise ValueError(
                f"agent card inode 교체/비-regular 거부: {relative.as_posix()}"
            )
        for parent, identity in parent_identities:
            observed = parent.lstat()
            if (
                _metadata_is_linklike(observed)
                or not stat.S_ISDIR(observed.st_mode)
                or _file_identity(observed, parent.name) != identity
            ):
                raise ValueError(
                    f"agent card 경로 교체 거부: {relative.as_posix()}"
                )
        current_leaf = target.lstat()
        if (
            _metadata_is_linklike(current_leaf)
            or not stat.S_ISREG(current_leaf.st_mode)
            or current_leaf.st_nlink != 1
            or _file_identity(current_leaf, leaf_label)
            != _file_identity(after, leaf_label)
        ):
            raise ValueError(
                f"agent card inode 교체/비-regular 거부: {relative.as_posix()}"
            )
        _root_directory_identity(lexical_root, root_identity)
        _root_directory_identity(root, root_identity)
        return _read_fd_bytes(file_fd)
    finally:
        os.close(file_fd)


def _read_known_regular_file(root: Path, relative: Path) -> str:
    """Read a known repo-relative regular file without following links.

    Capable POSIX interpreters use component-wise dir-fd traversal.  Portable
    interpreters lstat every lexical component and recheck parent/leaf identity
    after open.  Both paths require a single-link regular leaf and complete all
    checks before the first content read.
    """
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError(f"agent card 상대경로 거부: {relative.as_posix()}")
    lexical_root = Path(os.path.abspath(os.fspath(root)))
    root_identity = _root_directory_identity(lexical_root)
    resolved_root = lexical_root.resolve(strict=True)
    _root_directory_identity(resolved_root, root_identity)
    _root_directory_identity(lexical_root, root_identity)
    target = resolved_root.joinpath(*relative.parts)
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"agent card repo containment 밖: {relative.as_posix()}"
        ) from exc

    if _secure_dir_fd_supported():
        return _read_known_regular_file_dir_fd(
            resolved_root, relative, root_identity
        )
    return _read_known_regular_file_portable(
        resolved_root, relative, lexical_root, root_identity
    )


def _read_claude_native_agent_model(
    home, role: str,
    tier: str,
) -> tuple[str, Path]:
    """Read one explicitly mapped Claude card and return its strict model scalar."""
    relative = CLAUDE_NATIVE_AGENT_CARDS.get((role, tier))
    if relative is None:
        raise ValueError(f"명시 agent card 매핑 없음({role}/{tier})")
    text = _read_known_regular_file(home.root, relative)
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("frontmatter 시작 fence 없음")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("frontmatter 종료 fence 없음") from exc

    values: list[str] = []
    for line in lines[1:end]:
        match = delegate_channel_rules._FRONTMATTER_MODEL_RE.fullmatch(line)
        if match is not None:
            values.append(match.group(1))
    if not values:
        raise ValueError("frontmatter model 없음")
    if len(values) != 1:
        raise ValueError("frontmatter model 중복")
    return delegate_channel_rules._frontmatter_model_scalar(values[0]), relative


# 마스터 스위치가 off 일 때의 처방 — 매핑을 고치라고 하면 안 된다(막는 것은 매핑이 아니다).
# `pm_delegate` CLI 로 재지향해도 그 CLI 가 같은 스위치로 rc=3 을 내므로 순환이 된다.
_ENABLED_REMEDIATION = (
    "local.conf 에 `delegate.enabled=true` 를 두거나 그 줄을 지우세요(기본 허용)."
)
