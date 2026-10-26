"""엔진 코드·훅 파일의 원자 복사와 엔진 패키지 디렉터리 교체 (층: infra).

엔진 패키지(`.project_manager/tools/pm_engine`)는 파일 단위로 덮지 않고 디렉터리째 바꾼다. 같은 부모에
새 출하 목록만 담은 완성 대기 트리(stage)를 만들어 검증한 뒤, 기존 패키지 → backup, stage → 패키지
두 번의 rename 으로 게시한다. 상류에서 지우거나 이름을 바꾼 모듈과 대상에만 있던 모듈은 새 트리에
없으므로 함께 사라진다.

보장하는 원자성은 rename 하나씩이다. 두 rename 사이에는 패키지가 없는 구간이 있고, 강제 종료되면
같은 부모에 남은 stage·backup(예약 접두사 `manifest_rules._ENGINE_TRANSFER_*`)으로 수동 복구한다.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import NamedTuple
import contextlib
import hashlib
import os
import shutil
import stat
import tempfile
from pm_engine.domain import manifest_rules
from pm_engine.infra import fs
from pm_engine.infra import repo_files
from pm_engine.infra import retired_paths


def _atomic_copy2(source: Path, dest: Path) -> None:
    """같은 디렉토리의 완성된 임시 사본을 원자적으로 dest에 교체한다.

    dest 가 symlink 면 원자 교체는 **링크 자체를 대체**한다(일반 copy2 는 링크 너머 타깃에
    쓴다) — 의도된 동작이다. 엔진이 관리하는 훅 세트 파일은 manifest 소유 실파일이고, 그 자리를
    링크로 바꿔 둔 트리에 링크 너머로 쓰면 엔진이 자기 관리 밖 경로를 갱신하게 된다."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent,
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp)
        fs.atomic_replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


class EnginePackageFile(NamedTuple):
    """패키지 상대 출하 경로·실제 원천 파일·계획 시점 바이트의 sha256."""
    relative: str
    source: Path
    sha256: str


class EnginePackagePlan(NamedTuple):
    """엔진 패키지 한 개의 관측 결과 — 출하 목록과 대상 대비 추가·변경·삭제 파일.

    `dest_fingerprint` 는 계획 시점 대상의 (상대경로, sha256) 목록이고, None 은 대상이 실제로 없다는
    뜻뿐이다. 게시 직전에 락 안에서 이 값과 출하 파일 해시를 다시 대조한다."""
    source_dir: Path
    dest_dir: Path
    shipping_files: tuple[EnginePackageFile, ...]
    added: tuple[str, ...]
    updated: tuple[str, ...]
    deleted: tuple[str, ...]
    dest_fingerprint: tuple[tuple[str, str], ...] | None


class PackageReplaceError(OSError):
    """엔진 패키지 교체 실패 — 실패 단계·실제 경로·원인을 운반한다.

    `phase` 는 validation·lock·stale-plan·stage·first-rename·publish·rollback·flat-batch·cleanup 중
    하나다. `original` 은 그 단계의 원인 오류, `secondary` 는 이어서 복구·정리까지 실패한 오류다.
    `stage`·`backup` 은 실패 뒤 디스크에 남은 복구 자료 위치다(없으면 None)."""

    def __init__(self, phase: str, message: str, *, source: Path, dest: Path,
                 stage: Path | None = None, backup: Path | None = None,
                 original: BaseException | None = None,
                 secondary: BaseException | None = None) -> None:
        self.phase = phase
        self.source = source
        self.dest = dest
        self.stage = stage
        self.backup = backup
        self.original = original
        self.secondary = secondary
        details = [f"엔진 패키지 교체 실패({phase}) — {message}", f"source={source}", f"dest={dest}"]
        if stage is not None:
            details.append(f"stage={stage}")
        if backup is not None:
            details.append(f"backup={backup}")
        if original is not None:
            details.append(f"원인 {type(original).__name__}: {original}")
        if secondary is not None:
            details.append(f"복구·정리 실패 {type(secondary).__name__}: {secondary}")
        super().__init__("; ".join(details))
        if isinstance(original, OSError):
            self.errno = original.errno


def _package_tree_state(root: Path) -> dict[str, bytes] | None:
    """대상 패키지의 일반 파일 상태(패키지 상대경로 → 바이트). 대상이 실제로 없을 때만 None.

    캐시·저장소 메타 디렉터리(`repo_files` 의 강등 열거 제외 이름)는 비교·게시 대상이 아니다. 링크·
    reparse point·일반 파일 아닌 항목은 그 이름이어도 통과시키지 않고 `OSError` 로 실패한다."""
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    if retired_paths._path_is_link_or_reparse(root) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"엔진 패키지 자리가 실제 디렉터리가 아니다: {root}")
    state: dict[str, bytes] = {}
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if retired_paths._path_is_link_or_reparse(path):
                    raise OSError(f"엔진 패키지 안에 링크·reparse point 가 있다: {path}")
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    if entry.name not in repo_files._FALLBACK_EXCLUDE_NAMES:
                        pending.append((path, f"{prefix}{entry.name}/"))
                elif stat.S_ISREG(mode):
                    state[f"{prefix}{entry.name}"] = fs.read_bytes_shared(path)
                else:
                    raise OSError(f"엔진 패키지 안에 일반 파일이 아닌 항목이 있다: {path}")
    return state


def _validate_package_files(
    source_dir: Path, shipping_files: tuple[tuple[str, Path], ...],
) -> tuple[EnginePackageFile, ...]:
    """출하 목록을 검증하고 원천 바이트의 해시를 묶는다 — 잘못된 입력은 `ValueError`·`OSError`.

    상대 경로는 패키지 안의 posix 파일 경로여야 하고(절대·`..`·빈 조각·역슬래시 거부), 현재
    플랫폼에서 같은 이름으로 접히거나 파일·디렉터리로 겹치면 거부한다. 원천은 링크가 아닌 일반
    파일이어야 한다. 원천 디렉터리 자체도 실제 디렉터리여야 한다(자식 `@source` 원천은 그 밖일 수 있다).
    캐시·저장소 메타 디렉터리(`_package_tree_state` 와 같은 제외 이름) 아래 파일은 추적돼 있어도
    비교·stage 대상이 아니다 — 대상 쪽에서 보지 않는 파일을 게시하면 다음 계획이 수렴하지 않는다."""
    if retired_paths._path_is_link_or_reparse(source_dir) or not source_dir.is_dir():
        raise OSError(f"엔진 패키지 원천이 실제 디렉터리가 아니다: {source_dir}")
    files: list[EnginePackageFile] = []
    taken_files: set[str] = set()
    taken_directories: set[str] = set()
    for relative, source in shipping_files:
        pure = PurePosixPath(relative)
        if any(part in repo_files._FALLBACK_EXCLUDE_NAMES for part in pure.parts[:-1]):
            continue
        if ("\\" in relative or pure.is_absolute()
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise ValueError(f"엔진 패키지 출하 경로가 패키지 상대 파일 경로가 아니다: {relative!r}")
        key = os.path.normcase(relative)
        parents = {os.path.normcase(str(parent)) for parent in pure.parents if str(parent) != "."}
        if key in taken_files or key in taken_directories or parents & taken_files:
            raise ValueError(f"엔진 패키지 출하 경로가 다른 출하 경로와 겹친다: {relative}")
        taken_files.add(key)
        taken_directories |= parents
        source = Path(source)
        if retired_paths._path_is_link_or_reparse(source) or not stat.S_ISREG(source.lstat().st_mode):
            raise OSError(f"엔진 패키지 출하 원천이 일반 파일이 아니다: {source}")
        files.append(EnginePackageFile(
            relative, source, hashlib.sha256(fs.read_bytes_shared(source)).hexdigest()))
    if not files:
        raise ValueError(f"엔진 패키지 출하 목록이 비었다: {source_dir}")
    return tuple(sorted(files))


def prepare_engine_package(
    source_dir: Path, dest_dir: Path, shipping_files: tuple[tuple[str, Path], ...],
) -> EnginePackagePlan:
    """출하 목록과 대상 트리를 바이트로 비교해 추가·변경·삭제와 대상 지문을 만든다(쓰기 없음).

    source 와 dest 가 같은 디렉터리인 것은 허용하지만(완성 stage 뒤 게시), 한쪽이 다른 쪽의 엄격한
    조상·자손이면 거부한다. 검증·읽기 실패는 `PackageReplaceError(phase="validation")` 다."""
    source_dir, dest_dir = Path(source_dir), Path(dest_dir)
    try:
        source_real, dest_real = source_dir.resolve(), dest_dir.resolve()
        if source_real != dest_real and (
                source_real in dest_real.parents or dest_real in source_real.parents):
            raise ValueError("source 와 dest 가 서로의 조상·자손이다")
        files = _validate_package_files(source_dir, shipping_files)
        state = _package_tree_state(dest_dir)
    except (OSError, ValueError) as exc:
        raise PackageReplaceError("validation", str(exc), source=source_dir, dest=dest_dir,
                                  original=exc) from exc
    planned = {item.relative: item.sha256 for item in files}
    current = {relative: hashlib.sha256(data).hexdigest() for relative, data in (state or {}).items()}
    return EnginePackagePlan(
        source_dir=source_dir,
        dest_dir=dest_dir,
        shipping_files=files,
        added=tuple(sorted(relative for relative in planned if relative not in current)),
        updated=tuple(sorted(relative for relative in planned
                             if relative in current and current[relative] != planned[relative])),
        deleted=tuple(sorted(relative for relative in current if relative not in planned)),
        dest_fingerprint=None if state is None else tuple(sorted(current.items())),
    )


def replace_engine_package(
    source_dir: Path, dest_dir: Path, shipping_files: tuple[EnginePackageFile, ...],
) -> Path | None:
    """stage 를 완성·검증한 뒤 두 rename 으로 게시한다 — 기존 패키지 backup 경로, 기존 패키지가 없었으면 None.

    stage 가 완성되기 전에는 기존 패키지를 건드리지 않는다. 첫 rename 이 실패하면 기존 패키지가 그대로
    남고, 게시가 실패하면 backup 을 되돌린다. 되돌리기까지 실패하면 stage·backup 을 지우지 않고 두
    원인과 함께 올린다. stage 정리가 실패하면 그 원인도 함께 올린다. backup 정리는 호출자 몫이다."""
    source_dir, dest_dir = Path(source_dir), Path(dest_dir)
    parent = dest_dir.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=manifest_rules._ENGINE_TRANSFER_STAGE_PREFIX, dir=parent))
    except OSError as exc:
        raise PackageReplaceError("stage", "완성 대기 트리를 만들지 못했다",
                                  source=source_dir, dest=dest_dir, original=exc) from exc
    backup = parent / (manifest_rules._ENGINE_TRANSFER_BACKUP_PREFIX
                       + stage.name[len(manifest_rules._ENGINE_TRANSFER_STAGE_PREFIX):])

    def fail_after_stage_cleanup(phase: str, message: str, original: BaseException):
        try:
            _cleanup_package_tree(stage)
        except OSError as cleanup_error:
            raise PackageReplaceError(phase, message, source=source_dir, dest=dest_dir, stage=stage,
                                      original=original, secondary=cleanup_error) from original
        raise PackageReplaceError(phase, message, source=source_dir, dest=dest_dir,
                                  original=original) from original

    try:
        for item in shipping_files:
            target = stage / item.relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.source, target)
            if hashlib.sha256(fs.read_bytes_shared(target)).hexdigest() != item.sha256:
                raise ValueError(f"stage 사본이 계획한 바이트와 다르다: {item.relative}")
    except (OSError, ValueError) as exc:
        fail_after_stage_cleanup("stage", "완성 대기 트리를 채우지 못했다", exc)
    except BaseException as exc:
        exc.add_note(f"엔진 패키지 교체 중단 — 기존 패키지는 그대로다. 남은 stage: {stage}")
        raise

    try:
        dest_dir.lstat()
        existed = True
    except FileNotFoundError:
        existed = False
    except OSError as exc:
        fail_after_stage_cleanup("first-rename", "기존 패키지 자리를 확인하지 못했다", exc)
    if existed:
        try:
            backup.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            fail_after_stage_cleanup("first-rename", "backup 자리를 확인하지 못했다", exc)
        else:
            fail_after_stage_cleanup("first-rename", "backup 자리가 이미 있어 덮지 않는다",
                                     FileExistsError(f"backup 자리가 이미 있다: {backup}"))
        try:
            os.rename(dest_dir, backup)
        except OSError as exc:
            fail_after_stage_cleanup("first-rename", "기존 패키지를 backup 으로 옮기지 못했다", exc)
    try:
        os.rename(stage, dest_dir)
    except OSError as exc:
        if not existed:
            fail_after_stage_cleanup("publish", "새 패키지를 게시하지 못했다(기존 패키지 없음)", exc)
        try:
            os.rename(backup, dest_dir)
        except OSError as rollback_error:
            raise PackageReplaceError(
                "rollback", "게시와 backup 되돌리기가 모두 실패했다 — 패키지 자리가 비어 있을 수 있어 "
                "stage·backup 을 남긴다", source=source_dir, dest=dest_dir, stage=stage,
                backup=backup, original=exc, secondary=rollback_error) from exc
        fail_after_stage_cleanup("publish", "새 패키지를 게시하지 못해 기존 패키지를 되돌렸다", exc)
    return backup if existed else None


def _cleanup_package_tree(path: Path) -> None:
    """이번 교체가 만든 stage·backup 하나를 지우고 실제로 없어졌는지 lstat 으로 확인한다.

    `fs.force_rmtree` 는 시작 시점에 닿지 못한 경로도 부재로 보므로 반환만으로 성공을 판정하지
    않는다 — lstat 의 `FileNotFoundError` 만 성공이고, 남아 있거나 접근 오류면 `OSError` 다."""
    fs.force_rmtree(path)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise OSError(f"정리한 뒤에도 남아 있다: {path}")


@contextlib.contextmanager
def engine_package_delivery(package_plan: EnginePackagePlan) -> Iterator[None]:
    """같은 대상의 교체 writer 를 락으로 직렬화하고 계획을 다시 확인한 뒤 패키지를 게시하는 구간.

    구간 본문은 나머지 평면 엔진 파일 전달이다. 본문이 정상 종료하면 이번 호출의 backup 만 정리한다.
    본문의 IO 실패는 새 패키지와 backup 을 남긴 채 `flat-batch` 실패로 올린다(패키지만 되돌리면
    평면 파일과 세대가 더 어긋난다). 그 밖의 예외에는 보존 경로를 메모로 붙여 원래 예외를 올린다.
    락은 패키지 부모의 고정 파일이며 지우지 않는다. 락 primitive 가 없으면 무락으로 진행하지 않는다."""
    source_dir, dest_dir = package_plan.source_dir, package_plan.dest_dir

    def failure(phase: str, message: str, original: BaseException | None = None,
                backup: Path | None = None) -> PackageReplaceError:
        return PackageReplaceError(phase, message, source=source_dir, dest=dest_dir,
                                   backup=backup, original=original)

    if not fs.exclusive_lock_supported():
        raise failure("lock", "이 플랫폼에는 OS 배타 파일락이 없어 패키지 교체를 하지 않는다")
    lock_path = dest_dir.parent / manifest_rules._ENGINE_TRANSFER_LOCK_NAME
    try:
        with fs.exclusive_file_lock(lock_path):
            try:
                state = _package_tree_state(dest_dir)
                changed_sources = [
                    item.relative for item in package_plan.shipping_files
                    if hashlib.sha256(fs.read_bytes_shared(item.source)).hexdigest() != item.sha256]
            except OSError as exc:
                raise failure("stale-plan", "락 안에서 계획을 다시 확인하지 못했다", exc) from exc
            fingerprint = None if state is None else tuple(sorted(
                (relative, hashlib.sha256(data).hexdigest()) for relative, data in state.items()))
            if fingerprint != package_plan.dest_fingerprint or changed_sources:
                raise failure("stale-plan", "계획한 뒤 source 또는 dest 패키지가 바뀌었다 — 같은 갱신을 "
                              "다시 실행하라")
            backup = replace_engine_package(source_dir, dest_dir, package_plan.shipping_files)
            try:
                yield
            except OSError as exc:
                raise failure("flat-batch", "새 패키지는 게시됐지만 나머지 엔진 파일 전달이 실패했다 — "
                              "새 패키지와 backup 을 남긴다", exc, backup) from exc
            except BaseException as exc:
                exc.add_note(f"엔진 패키지는 게시됐다: {dest_dir} · 이전 패키지 backup: {backup}")
                raise
            if backup is not None:
                try:
                    _cleanup_package_tree(backup)
                except OSError as exc:
                    raise failure("cleanup", "전달은 끝났지만 이전 패키지 backup 을 정리하지 못했다",
                                  exc, backup) from exc
    except PackageReplaceError:
        raise
    except OSError as exc:
        raise failure("lock", "패키지 교체 락을 잡거나 풀지 못했다", exc) from exc


def change_file_count(changes: Sequence[tuple], package_plan: EnginePackagePlan | None) -> int:
    """변경 파일 수 — 일반 행 수 + 패키지 교체 행의 추가·변경·삭제 파일 수(교체 행 하나를 1로 세지 않는다).

    교체 행이 둘 이상이거나, 계획 값이 없거나, 행의 source·dest 가 계획과 다르거나, 계획의 변경이
    0 이면 `PackageReplaceError(phase="validation")` 다(적용·건수 계산이 같은 판정을 쓴다)."""
    package_rows = [change for change in changes if change[3] == "replace-dir"]
    if not package_rows:
        return len(changes)
    _rel, source, dest, _kind = package_rows[0]
    if package_plan is None:
        raise PackageReplaceError("validation", "교체 행에 패키지 계획 값이 없다",
                                  source=Path(source), dest=Path(dest))
    package_files = (len(package_plan.added) + len(package_plan.updated)
                     + len(package_plan.deleted))
    if (len(package_rows) > 1 or Path(source) != package_plan.source_dir
            or Path(dest) != package_plan.dest_dir or not package_files):
        raise PackageReplaceError("validation", "교체 행과 패키지 계획 값이 맞지 않는다",
                                  source=Path(source), dest=Path(dest))
    return len(changes) - 1 + package_files
