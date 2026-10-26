"""위임 격리 임시 경로와 운반 파일 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.infra.harness import contract
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_errors
from pm_engine.domain import delegate_run_rules
import errno
from pm_engine.infra import fs
import os
from pm_engine.infra import raw_output
import shutil
import stat
import sys
import uuid

_READ_TMP_FD_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in getattr(os, "supports_dir_fd", frozenset())
    and os.mkdir in getattr(os, "supports_dir_fd", frozenset())
    and os.rmdir in getattr(os, "supports_dir_fd", frozenset())
    and os.stat in getattr(os, "supports_dir_fd", frozenset())
    and bool(getattr(shutil.rmtree, "avoids_symlink_attacks", False))
)


def _assert_opencode_transport_path(cwd: Path, prompt_file: Path) -> None:
    """공용 ``--file``/``--dir`` containment 계약을 위임 표면 예외로 번역한다."""
    try:
        contract.assert_opencode_prompt_in_cwd(cwd, prompt_file)
    except contract.HarnessContractError as exc:
        raise delegate_errors.DelegateError(str(exc)) from exc
    # 공용 하네스 계약(contract.assert_opencode_prompt_in_cwd)을 먼저 호출해 기존 하네스 진단을
    # 보존한 뒤 attempt 공통 strict-child 불변식도 같은 경로에 적용한다.
    _assert_attempt_child_path(cwd, prompt_file, label="opencode prompt 전달 사본")


_OPENCODE_TRANSPORT_REL_DIR = Path(".project_manager") / ".local" / "delegate"
_OPENCODE_TRANSPORT_IGNORE = ".gitignore"
_OPENCODE_TRANSPORT_IGNORE_BODY = "*\n"


class _OpenCodeTransportPrompt:
    """opencode wire 경로와 이번 attempt가 만든 생성물의 소유권 묶음."""

    def __init__(self, path: Path, sandbox: Path):
        self.path = path
        self.sandbox = sandbox
        self.launch_binding_mode: str | None = None
        self.ignore_created = False
        self.prompt_created = False

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def __getattr__(self, name: str):
        # 내부 테스트/장부 코드가 Path 표면(parent/resolve/read_text 등)을 그대로 쓸 수 있게 한다.
        return getattr(self.path, name)


class _ReadRoleTemp:
    """read attempt 하나가 소유한 임시 디렉터리.

    `parent_fd` 는 fd 결속 수단(POSIX)에서만 값이 있고, 소유자 ACL 수단(Windows)에서는 None 이다 —
    회수/재검증이 그 축을 보고 같은 보장을 내는 다른 수단을 고른다.
    """

    def __init__(
        self,
        path: Path,
        *,
        parent_fd: int | None,
        identity: tuple[int, int],
        owned_fds: list[int],
        writable_path: Path | None = None,
        created_parent: tuple[int | None, str, Path, tuple[int, int]] | None = None,
    ):
        self.path = path
        self.writable_path = writable_path or path
        self.parent_fd = parent_fd
        self.identity = identity
        self.owned_fds = owned_fds
        # opencode 전용 temp 부모를 이번 attempt가 만들었을 때만, 생성 inode가 그대로인 경우에만
        # 빈 부모까지 회수한다.
        self.created_parent = created_parent

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)


def _assert_attempt_child_path(root: Path, child: Path, *, label: str) -> None:
    """실경로 기준 strict-child containment를 전달 사본과 read tmp가 공유한다."""
    try:
        resolved_root = root.resolve()
        resolved_child = child.resolve()
    except (OSError, RuntimeError) as exc:
        raise delegate_errors.DelegateError(
            f"{label} 실경로 해소 실패: root={root} · child={child}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if resolved_child == resolved_root or not delegate_env._is_relative_to(
        resolved_child, resolved_root,
    ):
        raise delegate_errors.DelegateError(
            f"{label} containment 위반: {resolved_child} 는 {resolved_root}의 "
            "strict child가 아님"
        )


def _open_fixed_directory(path: Path, *, label: str) -> int:
    """stat 뒤 nofollow open한 디렉터리의 identity를 고정한다."""
    try:
        expected = path.stat()
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise delegate_errors.DelegateError(f"{label}를 안전하게 열 수 없음: {path}: {exc}") from exc
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        _close_transport_fd(fd)
        raise delegate_errors.DelegateError(f"{label}가 검사 뒤 교체됨: {path}")
    return fd


def _read_tmp_root(home) -> Path:
    """read 역할 격리 자리의 루트 — PM 홈이 소유한 프로젝트 안 임시 루트 하나다.

    자리 규약을 여기 다시 적지 않고 `raw_output.temp_root` 를 그대로 쓴다(원자 쓰기를 뺀 엔진의
    모든 임시물이 같은 부모를 쓴다). 앵커는 raw 박제와 같은 소유자다 — 위임 산출과 그 실행의
    임시물이 다른 clone 으로 흩어지지 않는다. 테스트는 이 함수 하나를 주입해 자리를 옮긴다.
    """
    return raw_output.temp_root(home.local_dir.parent.parent)


def _read_tmp_attempt_name() -> str:
    """attempt 디렉터리 이름 하나 — 접두 + pid + 짧은 16진 꼬리(폭은 `_READ_TMP_SUFFIX_HEX`)."""
    return f"{delegate_run_rules._READ_TMP_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:delegate_run_rules._READ_TMP_SUFFIX_HEX]}"


def _read_tmp_parent(home, harness: str) -> tuple[Path, str | None]:
    """attempt 생성 부모를 반환한다(opencode는 공유 골격 아래에서 호출별 root를 만든다)."""
    temp_root = _read_tmp_root(home).resolve()
    component = delegate_run_rules._READ_TMP_PARENT_COMPONENT_BY_HARNESS[harness]
    return (
        (temp_root / component, component)
        if component is not None
        else (temp_root, None)
    )


# 검토 표면 밖 판정의 접두 — clone-local 런타임 자리. gitignore(`.project_manager/.gitignore` 의
# `.local/`)·잔여 인구 제외(`ticket_finish._is_local_runtime_path`)·pytest 수집 제외가 **이미** 이
# 접두에 걸려 있다. 새 접두를 만들면 같은 뜻의 제외를 세 곳에 더 달아야 한다.
_LOCAL_RUNTIME_PREFIX_PARTS: tuple[str, ...] = (".project_manager", ".local")


def _assert_read_tmp_outside_review_surface(attempt_path: Path, cwd: Path) -> None:
    """격리 자리가 검토 대상의 **추적 표면** 안이면 거부한다.

    판정 주어는 "cwd 트리 밖"이 아니라 "검토 표면 밖"이다. 격리 요건은 리뷰 실행 root 에
    검토 대상의 source·index·산출이 섞이지 않는 것이고, clone-local 런타임 접두
    (`.project_manager/.local/`)에는 gitignore·잔여 인구 제외·pytest 수집 제외가 이미 걸려 있어
    같은 트리 안이어도 검토 표면이 아니다(묶음 run-dir 이 이미 그 접두를 쓰고 codex `--add-dir`
    로 열린다). 주어를 cwd 트리로 두면 cwd 가 PM 홈이거나 그 하위인 형상(단일 clone 채택자·
    PM 문서 검토)에서 read 역할 위임이 전부 죽는다.
    """
    try:
        resolved_cwd = cwd.resolve()
    except (OSError, RuntimeError) as exc:
        raise delegate_errors.DelegateError(
            f"read 역할 worktree 실경로 해소 실패: {cwd}: {exc}"
        ) from exc
    resolved_attempt = attempt_path.resolve()
    if not delegate_env._is_relative_to(resolved_attempt, resolved_cwd):
        return
    relative = resolved_attempt.relative_to(resolved_cwd).parts
    if relative[:len(_LOCAL_RUNTIME_PREFIX_PARTS)] == _LOCAL_RUNTIME_PREFIX_PARTS:
        return
    raise delegate_errors.DelegateError(
        f"read 역할 temp가 검토 표면 안으로 해소되어 거부: {resolved_attempt} "
        f"(worktree={resolved_cwd})"
    )


def _read_tmp_owner_acl_platform() -> bool:
    """소유자 전용 ACL 로 fd 결속과 등가인 격리를 낼 수 있는 플랫폼인가(주입 가능 seam)."""
    return bool(fs.windows_acl_platform())


def _read_tmp_strategy() -> str | None:
    """이 플랫폼에서 read tmp 를 **안전하게** 만들 수단을 고른다(둘 다 없으면 None)."""
    if _READ_TMP_FD_SUPPORTED:
        return delegate_run_rules._READ_TMP_STRATEGY_FD
    if _read_tmp_owner_acl_platform():
        return delegate_run_rules._READ_TMP_STRATEGY_OWNER_ACL
    return None


def _remove_read_tmp_tree(path: Path, *, parent_fd: int | None) -> None:
    """attempt 트리를 회수한다 — 강제 삭제 seam 하나에 fd 결속을 실어 보낸다.

    맨 `shutil.rmtree` 는 read-only 하위가 하나만 있어도 남긴다(실측: 리뷰어 회귀가 만드는
    0o500 디렉터리에서 `PermissionError [Errno 13] ... 'inner.txt'`). `fs.force_rmtree` 는
    속성을 풀고 그 항목만 재시도하되 끝내 남으면 예외를 올리고, `dir_fd` 를 받으면 삭제를 고정
    부모 fd 에 결속해 symlink 공격 회피도 그대로 유지한다.
    """
    fs.force_rmtree(path, dir_fd=parent_fd)


def _restrict_read_tmp_to_owner(path: Path) -> None:
    """생성한 temp 디렉터리를 소유자 전용 접근으로 만든다(ACL 플랫폼은 mode 비트가 무효)."""
    try:
        fs.restrict_to_owner(path)
    except fs.AccessRestrictionError as exc:
        raise delegate_errors.DelegateError(
            f"read 역할 temp 소유자 전용 제한 실패: {path}: {exc} — 격리되지 않은 "
            "디렉터리를 쓰기 대상으로 넘기지 않습니다."
        ) from exc


def _create_read_role_temp_owner_acl(home, harness: str, cwd: Path) -> _ReadRoleTemp:
    """dir_fd 가 없는 플랫폼의 등가 경로 — 배타 생성 + 소유자 ACL + identity 재검증.

    fd 결속 대신 (a) 예측 불가 이름의 배타 mkdir(선점된 이름이면 FileExistsError 로 끝난다),
    (b) 생성 직후 symlink/reparse 아님 확인, (c) 소유자 전용 ACL, (d) 회수 시 생성 identity 재대조로
    같은 보장을 낸다. 어느 단계든 실패하면 권한을 낮춰 계속하지 않고 loud 로 끝낸다.
    """
    temp_parent, optional_parent_name = _read_tmp_parent(home, harness)
    temp_root = (
        temp_parent.parent if optional_parent_name is not None else temp_parent
    )
    if not temp_root.is_dir():
        raise delegate_errors.DelegateError(f"read 역할 임시 루트가 없음: {temp_root}")

    created_parent: tuple[int | None, str, Path, tuple[int, int]] | None = None
    attempt_created = False
    attempt_name = _read_tmp_attempt_name()
    attempt_path = temp_parent / attempt_name
    try:
        _assert_path_entry_not_symlink(temp_root, label="read 역할 임시 루트")
        if optional_parent_name is not None:
            try:
                temp_parent.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise delegate_errors.DelegateError(
                    f"read 역할 temp 부모 생성 실패: {temp_parent}: {exc}"
                ) from exc
            else:
                _restrict_read_tmp_to_owner(temp_parent)
                parent_stat = os.stat(temp_parent, follow_symlinks=False)
                created_parent = (
                    None,
                    optional_parent_name,
                    temp_parent,
                    (parent_stat.st_dev, parent_stat.st_ino),
                )
            _assert_path_entry_not_symlink(temp_parent, label="read 역할 temp 부모")

        try:
            attempt_path.mkdir(mode=0o700)
        except OSError as exc:
            raise delegate_errors.DelegateError(
                f"read 역할 temp 생성 실패: {attempt_path}: {exc}"
            ) from exc
        attempt_created = True
        _assert_path_entry_not_symlink(attempt_path, label="read 역할 temp")
        _restrict_read_tmp_to_owner(attempt_path)
        attempt_stat = os.stat(attempt_path, follow_symlinks=False)
        attempt_identity = (attempt_stat.st_dev, attempt_stat.st_ino)
        _assert_attempt_child_path(temp_parent, attempt_path, label="read 역할 temp")
        _assert_read_tmp_outside_review_surface(attempt_path, cwd)
        writable_path = attempt_path
        writable_component = delegate_run_rules._READ_TMP_WRITABLE_COMPONENT_BY_HARNESS[harness]
        if writable_component is not None:
            writable_path = attempt_path / writable_component
            try:
                writable_path.mkdir(mode=0o700)
            except OSError as exc:
                raise delegate_errors.DelegateError(
                    f"read 역할 쓰기 하위 디렉터리 생성 실패: {writable_path}: {exc}"
                ) from exc
            _restrict_read_tmp_to_owner(writable_path)
        return _ReadRoleTemp(
            attempt_path,
            parent_fd=None,
            identity=attempt_identity,
            owned_fds=[],
            writable_path=writable_path,
            created_parent=created_parent,
        )
    except BaseException:
        if attempt_created:
            try:
                _remove_read_tmp_tree(attempt_path, parent_fd=None)
            except (OSError, RuntimeError) as cleanup_exc:
                # 롤백 정리 실패는 주 예외를 덮지 않는다(아래 `raise`). 다만 침묵하지도 않는다 —

                _warn_read_tmp_cleanup_failure(
                    attempt_path, cleanup_exc)
        if created_parent is not None:
            _cleanup_owned_read_tmp_parent(created_parent)
        raise


def _create_read_role_temp(home, harness: str, cwd: Path) -> _ReadRoleTemp | None:
    """read attempt 전용 0700 tmp를 mkdir(dir_fd)로 만들고 parent fd에 결속한다.

    fd/nofollow/race 규율은 바로 아래 opencode 전달 사본 seam과 동일하다. 그 primitive 가 없는
    플랫폼은 넓은 경로 기반 삭제로 강등하지 않고 소유자 ACL 등가 경로를 탄다. 두 수단이 모두
    없을 때만 preamble의 명시적 회귀-불가 경로를 탄다.
    """
    strategy = _read_tmp_strategy()
    if strategy is None:
        return None
    if strategy == delegate_run_rules._READ_TMP_STRATEGY_OWNER_ACL:
        return _create_read_role_temp_owner_acl(home, harness, cwd)
    temp_parent, optional_parent_name = _read_tmp_parent(home, harness)
    temp_root = (
        temp_parent.parent if optional_parent_name is not None else temp_parent
    )
    if not temp_root.is_dir():
        raise delegate_errors.DelegateError(f"read 역할 임시 루트가 없음: {temp_root}")

    owned_fds: list[int] = []
    created_parent: tuple[int, str, Path, tuple[int, int]] | None = None
    attempt_created = False
    attempt_identity: tuple[int, int] | None = None
    parent_fd: int | None = None
    attempt_name = _read_tmp_attempt_name()
    attempt_path = temp_parent / attempt_name
    try:
        root_fd = _open_fixed_directory(temp_root, label="read 역할 임시 루트")
        owned_fds.append(root_fd)
        parent_fd = root_fd
        if optional_parent_name is not None:
            try:
                child_fd = os.open(
                    optional_parent_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(optional_parent_name, 0o700, dir_fd=root_fd)
                    parent_stat = os.stat(
                        optional_parent_name,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                    created_parent = (
                        root_fd,
                        optional_parent_name,
                        temp_parent,
                        (parent_stat.st_dev, parent_stat.st_ino),
                    )
                except FileExistsError:
                    pass
                child_fd = os.open(
                    optional_parent_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise delegate_errors.DelegateError(
                    f"read 역할 temp 부모가 symlink이거나 안전한 디렉터리가 아님: "
                    f"{temp_parent}: {exc}"
                ) from exc
            owned_fds.append(child_fd)
            if created_parent is not None:
                opened_parent = os.fstat(child_fd)
                if (opened_parent.st_dev, opened_parent.st_ino) != created_parent[3]:
                    raise delegate_errors.DelegateError(
                        f"read 역할 temp 부모가 생성 뒤 교체됨: {temp_parent}"
                    )
            parent_fd = child_fd

        os.mkdir(attempt_name, 0o700, dir_fd=parent_fd)
        attempt_created = True
        attempt_stat = os.stat(
            attempt_name, dir_fd=parent_fd, follow_symlinks=False,
        )
        attempt_identity = (attempt_stat.st_dev, attempt_stat.st_ino)
        _assert_attempt_child_path(temp_parent, attempt_path, label="read 역할 temp")
        _assert_read_tmp_outside_review_surface(attempt_path, cwd)
        writable_path = attempt_path
        writable_component = delegate_run_rules._READ_TMP_WRITABLE_COMPONENT_BY_HARNESS[harness]
        if writable_component is not None:
            attempt_fd = os.open(
                attempt_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            owned_fds.append(attempt_fd)
            opened_attempt = os.fstat(attempt_fd)
            if (opened_attempt.st_dev, opened_attempt.st_ino) != attempt_identity:
                raise delegate_errors.DelegateError(f"read 역할 temp가 생성 뒤 교체됨: {attempt_path}")
            os.mkdir(writable_component, 0o700, dir_fd=attempt_fd)
            writable_path = attempt_path / writable_component
        return _ReadRoleTemp(
            attempt_path,
            parent_fd=parent_fd,
            identity=attempt_identity,
            owned_fds=owned_fds,
            writable_path=writable_path,
            created_parent=created_parent,
        )
    except BaseException:
        if attempt_created and parent_fd is not None:
            try:
                current = os.stat(
                    attempt_name, dir_fd=parent_fd, follow_symlinks=False,
                )
                if attempt_identity == (current.st_dev, current.st_ino):
                    _remove_read_tmp_tree(attempt_path, parent_fd=parent_fd)
            except (OSError, RuntimeError) as cleanup_exc:
                # 롤백 정리 실패는 주 예외를 덮지 않는다(아래 `raise` 가 원인을 그대로 올린다).
                # 다만 침묵하지도 않는다 — 삭제 수단이 형제 모듈이라 사본 불일치가 여기 닿을 수
                # 있고, 그게 조용히 사라지면 재동기 처방이 함께 사라진다.

                _warn_read_tmp_cleanup_failure(
                    attempt_path, cleanup_exc)
        if created_parent is not None:
            _cleanup_owned_read_tmp_parent(created_parent)
        for fd in reversed(owned_fds):
            _close_transport_fd(fd)
        raise


def _warn_read_tmp_cleanup_failure(
    path: Path, exc: OSError | RuntimeError,
) -> str:
    """read tmp 정리 실패를 주 결과를 덮지 않고 loud 하게 남기고, 같은 사유를 돌려준다.

    반환값은 실행 결과를 실패로 내릴 때 그대로 싣는 사유다(문구를 두 번 쓰지 않는다).
    """
    cause = f"{exc}"
    reason = f"read 역할 임시 디렉터리 정리 실패 — 잔존 가능 경로: {path} · 오류: {cause}"
    print(f"경고: {reason}", file=sys.stderr)
    return reason


def _cleanup_owned_read_tmp_parent(
    created_parent: tuple[int | None, str, Path, tuple[int, int]],
) -> None:
    """생성 때 고정한 inode가 이름 공간에 그대로 있을 때만 공유 temp 부모를 rmdir한다."""
    ancestor_fd, name, parent_path, expected_identity = created_parent
    try:
        try:
            current = (
                os.stat(name, dir_fd=ancestor_fd, follow_symlinks=False)
                if ancestor_fd is not None
                else os.stat(parent_path, follow_symlinks=False)
            )
        except FileNotFoundError:
            return
        current_identity = (current.st_dev, current.st_ino)
        if current_identity != expected_identity:
            raise RuntimeError(
                f"temp 부모 생성 identity={expected_identity}, "
                f"정리 identity={current_identity}"
            )
        if ancestor_fd is not None:
            os.rmdir(name, dir_fd=ancestor_fd)
        else:
            os.rmdir(parent_path)
    except OSError as exc:
        if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST}:
            _warn_read_tmp_cleanup_failure(parent_path, exc)
    except RuntimeError as exc:
        _warn_read_tmp_cleanup_failure(parent_path, exc)


def _cleanup_read_role_temp(read_tmp: _ReadRoleTemp | None) -> str | None:
    """고정 parent fd와 inode를 재검증한 뒤 attempt 트리만 symlink-safe 회수한다.

    끝내 지우지 못하면 사유를 돌려준다 — 호출부가 그 실행의 결과를 실패로 내린다. 이 자리는
    `finally` 안에서 불리므로 여기서 raise 하면 주 결과와 전파 중인 예외를 덮는다.
    """
    if read_tmp is None:
        return None
    residue: str | None = None
    try:
        try:
            current = (
                os.stat(
                    read_tmp.path.name,
                    dir_fd=read_tmp.parent_fd,
                    follow_symlinks=False,
                )
                if read_tmp.parent_fd is not None
                else os.stat(read_tmp.path, follow_symlinks=False)
            )
        except FileNotFoundError:
            current = None
        if current is not None:
            identity = (current.st_dev, current.st_ino)
            if identity != read_tmp.identity:
                raise RuntimeError(
                    f"생성 identity={read_tmp.identity}, 정리 identity={identity}"
                )
            _remove_read_tmp_tree(read_tmp.path, parent_fd=read_tmp.parent_fd)
        if read_tmp.created_parent is not None:
            _cleanup_owned_read_tmp_parent(read_tmp.created_parent)
    except (OSError, RuntimeError) as exc:

        residue = _warn_read_tmp_cleanup_failure(
            read_tmp.path, exc)
    finally:
        for fd in reversed(read_tmp.owned_fds):
            _close_transport_fd(fd)
        read_tmp.owned_fds.clear()
    return residue


def _warn_transport_cleanup_failure(
    cleanup_path: Path,
    exc: OSError | RuntimeError,
    *,
    action: str = "합성 프롬프트 삭제",
) -> None:
    """transport 정리 실패를 주 결과를 덮지 않고 대상별 문구로 loud 하게 남긴다."""
    print(
        f"경고: opencode {action} 실패 — 잔존 가능 경로: {cleanup_path} · 오류: {exc}",
        file=sys.stderr,
    )


def _close_transport_fd(fd: int | None) -> None:
    """보조 fd close 실패가 생성/전달의 주 예외를 덮지 않게 한다."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _cleanup_attempt_transport(
    prompt: _OpenCodeTransportPrompt | None,
    read_tmp: _ReadRoleTemp | None = None,
) -> str | None:
    """attempt wire 사본·read tmp를 한 소유권 seam에서 되감는다.

    prompt 삭제가 실패하면 자기-은닉 ignore를 보존해 민감 사본이 untracked로 노출되지 않게 한다.
    정상 삭제 뒤에는 ignore→고유 attempt 디렉터리→delegate/.local/.project_manager를 역순
    제거한다. 겹친 attempt가 사용하는 공유 골격은 ENOTEMPTY로 자연스럽게 보존된다.

    반환값은 read tmp 잔존 사유다(없으면 None) — 실행 경로가 그 값으로 결과를 실패로 내린다.
    """
    # read tmp는 pytest가 만든 임의 하위 트리까지 회수한다. prompt 정리 실패와 서로 독립적으로
    # 시도해야 둘 중 하나가 다른 하나의 잔여 0 보장을 가리지 않는다.
    read_tmp_residue = _cleanup_read_role_temp(read_tmp)
    if prompt is None:
        return read_tmp_residue
    prompt_path = prompt.path
    cleanup_structure = True
    try:
        if prompt.prompt_created:
            os.unlink(prompt_path)
            prompt.prompt_created = False
    except OSError as exc:
        _warn_transport_cleanup_failure(prompt_path, exc)
        cleanup_structure = False

    if cleanup_structure and prompt.ignore_created:
        try:
            os.unlink(prompt_path.parent / _OPENCODE_TRANSPORT_IGNORE)
            prompt.ignore_created = False
        except OSError as exc:
            _warn_transport_cleanup_failure(
                prompt_path.parent / _OPENCODE_TRANSPORT_IGNORE,
                exc,
                action="자기-은닉 ignore 삭제",
            )
            cleanup_structure = False

    if cleanup_structure:
        for cleanup_path in prompt_path.parents:
            if cleanup_path == prompt.sandbox:
                break
            if not cleanup_path.is_relative_to(prompt.sandbox):
                break
            try:
                os.rmdir(cleanup_path)
            except OSError as exc:
                if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST}:
                    _warn_transport_cleanup_failure(
                        cleanup_path, exc, action="전달 디렉터리 정리",
                    )
    return read_tmp_residue


def _assert_path_entry_not_symlink(path: Path, *, label: str) -> None:
    """경로 기반 생성/전달 직전 entry가 symlink가 아닌지 best-effort로 확인한다."""
    try:
        entry = os.lstat(path)
    except OSError as exc:
        raise delegate_errors.DelegateError(f"{label} 경로 재검증 실패: {path}: {exc}") from exc
    if stat.S_ISLNK(entry.st_mode):
        raise delegate_errors.DelegateError(f"{label}가 symlink라 거부: {path}")


def _portable_exclusive_write(path: Path, content: str) -> None:
    """O_EXCL·0600으로 새 파일을 쓰고, 지원 플랫폼에서 O_NOFOLLOW를 더한다.

    `os.open` 의 mode 인자는 **ACL 플랫폼에서 무효**라 0600 만으로는 소유자 전용이 되지 않는다
    (Windows 는 부모 디렉터리의 상속 ACL 을 그대로 받는다). 쓰기 직후 공용 소유자-제한
    seam(`fs.restrict_to_owner`)이 두 플랫폼에서 같은 보장을 낸다."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    created = False
    try:
        fd = os.open(path, flags, 0o600)
        created = True
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        fd = None
        with handle:
            handle.write(content)
        fs.restrict_to_owner(path)
    except BaseException as write_exc:
        _close_transport_fd(fd)
        fd = None
        if created:
            try:
                os.unlink(path)
            except OSError as unlink_exc:
                # 원래 쓰기 예외를 롤백 실패로 덮지 않는다 — 아래 bare raise가 그대로 전파한다.
                # 정리 실패는 관측만 loud 하게 남긴다. 잔여 파일 경로는 원 예외에
                # 실어 둔다 — 호출자가 cleanup 재시도·자기-은닉 ignore 보존 판단에 쓴다.
                # ENOENT 는 잔여가 없다(누가 먼저 지웠다) — 표식을 붙이면 cleanup 이 오탐 경고를 낸다.
                # 표식은 예외 객체에 붙어 재사용 예외에서도 남을 수 있어, 호출자는 attempt 고유(uuid)
                # 경로와 등가비교로만 소비한다(무접두 이름 충돌 방지 위해 `_pm_` 접두).
                if unlink_exc.errno != errno.ENOENT:
                    write_exc._pm_residual_path = path
                _warn_transport_cleanup_failure(
                    path, unlink_exc, action="쓰기 실패 롤백 삭제",
                )
        raise
    finally:
        _close_transport_fd(fd)


def _save_opencode_transport_prompt(
    cwd: Path, prompt: str,
) -> _OpenCodeTransportPrompt:
    """합성 프롬프트의 opencode 전달 사본을 sandbox 안에 O_EXCL·0600으로 쓴다.

    이 파일은 wire 전용이며 PM 홈 감사 저장소를 해소하지 않는다. 코드 worktree가 아닌 기존
    ``--cwd`` 디렉터리도 같은 위치를 만들 수 있다. ``.project_manager``/``.local`` 중간 경로가
    sandbox 밖 symlink인 경우에는 mkdir/write 전에 공용 containment 가드로 거부한다.
    """
    # argv 문자열은 사용자 cwd의 미해소 절대경로를 보존한다. --dir과 --file이 같은 lexical
    # prefix를 공유해야 opencode의 문자열 containment가 symlink cwd를 auto-reject하지 않는다.
    sandbox = Path(os.path.abspath(cwd))
    try:
        resolved_sandbox = sandbox.resolve()
    except (OSError, RuntimeError) as exc:
        raise delegate_errors.DelegateError(
            f"opencode --dir 실경로 해소 실패: {sandbox}: {type(exc).__name__}: {exc}"
        ) from exc
    if not resolved_sandbox.is_dir():
        raise delegate_errors.DelegateError(f"opencode --dir 디렉터리가 없음: {sandbox}")
    attempt_id = f"pm_delegate_{os.getpid()}_{uuid.uuid4().hex}"
    dest = sandbox / _OPENCODE_TRANSPORT_REL_DIR / attempt_id / "prompt.md"
    _assert_opencode_transport_path(sandbox, dest)
    transport = _OpenCodeTransportPrompt(dest, sandbox)
    # 호출별 UUID 디렉터리는 반드시 새로 만든다. 이미 존재하면 충돌을 숨기거나 재사용하지 않고
    # FileExistsError를 그대로 올린다.
    current = sandbox
    for part in (*_OPENCODE_TRANSPORT_REL_DIR.parts, attempt_id):
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            if current == dest.parent:
                raise
        except OSError as exc:
            raise delegate_errors.DelegateError(
                f"opencode prompt 전달 디렉터리 생성 실패: {current}: {exc}"
            ) from exc
    ignore_path = dest.parent / _OPENCODE_TRANSPORT_IGNORE
    try:
        _assert_path_entry_not_symlink(
            dest.parent, label="opencode prompt 전달 부모",
        )
        _portable_exclusive_write(ignore_path, _OPENCODE_TRANSPORT_IGNORE_BODY)
        transport.ignore_created = True
        _portable_exclusive_write(dest, prompt)
        transport.prompt_created = True
    except BaseException as exc:
        # 롤백 unlink까지 실패하면(정리 경고) 잔여 파일이 디스크에 남는데도 `*_created`가
        # False라 cleanup이 그 존재를 몰랐다 — 원 예외의 잔여 경로 표식으로 해당 플래그를 세워
        # 기존 cleanup 순서(프롬프트 재시도 → 성공 시만 ignore 삭제 → 실패 시 ignore 보존+loud)를
        # 타게 한다.
        residual_path = getattr(exc, "_pm_residual_path", None)
        if residual_path == dest:
            transport.prompt_created = True
        elif residual_path == ignore_path:
            transport.ignore_created = True
        _cleanup_attempt_transport(transport)
        raise
    return transport
