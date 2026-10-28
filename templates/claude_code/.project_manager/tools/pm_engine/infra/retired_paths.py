"""명시 retired-path 지시의 검증·백업·이주 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
from pathlib import PureWindowsPath
import datetime
import hashlib
import os
import stat
import sys
from pm_engine.domain import conf_schema
from pm_engine.domain import manifest_rules
from pm_engine.infra import conf_store
from pm_engine.infra import fs
from pm_engine.infra import install_files
from pm_engine.infra import manifest_store


# 구 updater도 무시하는 manifest 주석 지시문. 일반 상류 부재 파일을
# 지우지 않고, 이 prefix로 명시된 engine-owned 경로만 한 번 backup+퇴역한다.
RETIRED_PATH_DIRECTIVE_PREFIX = "# pm-retired-path:"


RETIRED_PATH_LOCK_REL = ".project_manager/.local/retired-paths.lock"


RETIRED_PATH_BACKUP_ROOT_REL = ".pm_import_backups"


def print_conf_migration_notice(dest_root: Path) -> None:
    """local.conf 표기 통일 교체 안내 — **apply 를 막지 않고** 안내만 낸다.

    엔진 파일은 받게 하고(막으면 채택자가 안내대로 고칠 수단 없이 갇힌다) 값을 실제로 소비하는
    지점에서 fail-loud 한다(§교체 절차). 기본값 변경 1줄은 구표기 잔존 여부와 무관하게 나간다 —
    키를 아예 설정하지 않은 채택자는 fail-loud 가 잡지 못하는 유일한 형상이기 때문이다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.exists():
        return  # init 전 — 채택자 conf 가 아직 없다
    for line in conf_schema.migration_notice(conf_store.load(local_conf)):
        print(f"[pm_update] {line}")


class RetiredPathError(RuntimeError):
    """retired-path 선언·사전조건이 이주를 안전하게 정의하지 못한다."""


def _retired_repo_rel(value: str, *, manifest_path: Path, line_no: int) -> str:
    """retired directive 한 경로를 POSIX repo-relative canonical 표기로 검증한다."""
    raw = value.strip()
    parts = raw.split("/")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        not raw or "\\" in raw or posix.is_absolute() or windows.is_absolute()
        or windows.drive or any(part in ("", ".", "..") for part in parts)
        or posix.as_posix() != raw
    ):
        raise RetiredPathError(
            f"{manifest_path}:{line_no}: repo-relative POSIX 경로가 아님: {value!r}"
        )
    return raw


def parse_retired_path_directives(manifest_path: Path) -> list[tuple[str, str]]:
    """manifest의 `# pm-retired-path: OLD -> NEW` 주석만 strict하게 파싱한다."""
    declarations: list[tuple[str, str]] = []
    seen_old: set[str] = set()
    text = fs.read_text_shared(manifest_path, encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith(RETIRED_PATH_DIRECTIVE_PREFIX):
            continue
        body = stripped[len(RETIRED_PATH_DIRECTIVE_PREFIX):].strip()
        if body.count(" -> ") != 1:
            raise RetiredPathError(
                f"{manifest_path}:{line_no}: retired-path는 `OLD -> NEW` 형식이어야 함"
            )
        old_raw, new_raw = body.split(" -> ", 1)
        old = _retired_repo_rel(old_raw, manifest_path=manifest_path, line_no=line_no)
        new = _retired_repo_rel(new_raw, manifest_path=manifest_path, line_no=line_no)
        if old == new:
            raise RetiredPathError(
                f"{manifest_path}:{line_no}: retired-path의 OLD와 NEW가 같음: {old}"
            )
        if old in seen_old:
            raise RetiredPathError(
                f"{manifest_path}:{line_no}: retired-path OLD 중복: {old}"
            )
        seen_old.add(old)
        declarations.append((old, new))

    edges = dict(declarations)
    for start in edges:
        visited: set[str] = set()
        cursor = start
        while cursor in edges:
            if cursor in visited:
                raise RetiredPathError(
                    f"{manifest_path}: retired-path 순환 선언: {start}"
                )
            visited.add(cursor)
            cursor = edges[cursor]
    return declarations


def validated_retired_path_directives(
    source_root: Path,
    manifest_paths: list[Path],
) -> list[tuple[str, str]]:
    """선택 upstream manifest들의 strict·출하 가능한 retired-path 선언 합집합.

    flavor 선택과 실제 이주가 같은 판정을 소비한다. OLD는 upstream 소유집합에서 빠져 있어야 하고,
    NEW는 선택 manifest 행이 소유(행 경로와 같거나 디렉터리 행 하위·`_path_owned_by`)해야 하며 그 소유
    행이 모두 bare byte-copy 행이고, source_root의 regular file이어야 한다.
    """
    source_root = Path(source_root)
    declarations: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    entries_by_path: dict[str, list[manifest_rules.ManifestEntry]] = {}
    for manifest_path in (Path(path) for path in manifest_paths):
        for entry in manifest_store.read_manifest(manifest_path):
            entries_by_path.setdefault(
                str(entry).replace("\\", "/"), []
            ).append(entry)
        for old, new in parse_retired_path_directives(manifest_path):
            previous = seen.get(old)
            if previous is not None and previous != new:
                raise RetiredPathError(
                    f"retired-path 선언 충돌: {old} -> {previous} / {new}"
                )
            if previous is None:
                seen[old] = new
                declarations.append((old, new))

    edges = dict(declarations)
    for start in edges:
        visited: set[str] = set()
        cursor = start
        while cursor in edges:
            if cursor in visited:
                raise RetiredPathError(
                    f"선택 manifest retired-path 순환 선언: {start}"
                )
            visited.add(cursor)
            cursor = edges[cursor]

    owned = set(entries_by_path)
    old_paths = set(edges)
    for old, new in declarations:
        if old in owned:
            raise RetiredPathError(
                f"retired OLD가 upstream manifest-owned 경로로 남아 있음: {old}"
            )
        if new in old_paths:
            raise RetiredPathError(
                f"retired-path chain/conflict는 허용하지 않음: {old} -> {new}"
            )
        replacement_entries = [
            entry for path, entries in entries_by_path.items()
            if manifest_rules._path_owned_by(new, (path,)) for entry in entries
        ]
        if not replacement_entries:
            raise RetiredPathError(
                f"replacement가 선택 upstream manifest-owned 경로가 아님: {new}"
            )
        if any(
            manifest_rules._entry_render_flag(entry)
            or manifest_rules._entry_target_owned_flag(entry)
            or getattr(entry, "source_rel", None)
            for entry in replacement_entries
        ):
            raise RetiredPathError(
                f"replacement가 bare byte-copy manifest 행이 아님: {new}"
            )
        source_new = source_root / new
        _assert_no_link_components(
            source_root, source_new, label="upstream replacement"
        )
        if not source_new.is_file() or _path_is_link_or_reparse(source_new):
            raise RetiredPathError(
                f"upstream replacement가 regular file이 아님: {source_new}"
            )
    return declarations


def _path_is_link_or_reparse(path: Path) -> bool:
    """symlink와 Windows reparse point를 같은 탈출 경로로 본다."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attrs & reparse_flag)


def _assert_no_link_components(root: Path, path: Path, *, label: str) -> None:
    """`root` 아래 존재 조상 중 symlink/reparse가 있으면 탈출 전에 막는다."""
    root = Path(root).absolute()
    path = Path(path).absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RetiredPathError(f"{label} 경로가 repo 밖임: {path}") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if _path_is_link_or_reparse(cursor):
                raise RetiredPathError(f"{label} 경로에 symlink/reparse point가 있음: {cursor}")


def _retired_backup_path(dest_root: Path, old_rel: str) -> Path:
    """오늘 backup 좌표를 고르되 기존 backup을 덮지 않는다."""
    base = (
        Path(dest_root) / RETIRED_PATH_BACKUP_ROOT_REL
        / datetime.date.today().isoformat() / old_rel
    )
    candidate = base
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = base.with_name(f"{base.name}.{suffix}")
        suffix += 1
    return candidate


def _validate_retired_path_plan(
    dest_root: Path,
    source_root: Path,
    manifest: list,
    declarations: list[tuple[str, str]],
    prospective_replacements: dict[str, Path] | None = None,
) -> list[tuple[str, str, Path]]:
    """선언 전부를 쓰기 전에 검증하고, 실재 old만 backup 계획으로 돌린다."""
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    owned = {str(entry).replace("\\", "/") for entry in manifest}
    prospective_replacements = prospective_replacements or {}
    old_paths = {old for old, _new in declarations}
    for old, new in declarations:
        if not manifest_rules._path_owned_by(new, owned):
            raise RetiredPathError(f"replacement가 manifest-owned 경로가 아님: {new}")
        if new in old_paths:
            raise RetiredPathError(f"retired-path chain/conflict는 허용하지 않음: {old} -> {new}")
        source_new = source_root / new
        dest_new = dest_root / new
        _assert_no_link_components(source_root, source_new, label="upstream replacement")
        if not source_new.is_file() or _path_is_link_or_reparse(source_new):
            raise RetiredPathError(f"upstream replacement가 regular file이 아님: {source_new}")
        _assert_no_link_components(dest_root, dest_new, label="destination replacement")
        source_hash = hashlib.sha256(fs.read_bytes_shared(source_new)).digest()
        prospective = prospective_replacements.get(new)
        if prospective is not None:
            if not prospective.is_file() or _path_is_link_or_reparse(prospective):
                raise RetiredPathError(
                    f"적용 계획의 replacement source가 regular file이 아님: {prospective}"
                )
            dest_hash = hashlib.sha256(fs.read_bytes_shared(prospective)).digest()
        else:
            if not dest_new.is_file() or _path_is_link_or_reparse(dest_new):
                raise RetiredPathError(
                    f"destination replacement가 regular file이 아님: {dest_new}"
                )
            dest_hash = hashlib.sha256(fs.read_bytes_shared(dest_new)).digest()
        if source_hash != dest_hash:
            raise RetiredPathError(
                f"replacement hash 불일치(적용 미완료): {dest_new}"
            )

    planned: list[tuple[str, str, Path]] = []
    for old, new in declarations:
        old_path = dest_root / old
        _assert_no_link_components(dest_root, old_path, label="retired source")
        if not old_path.exists() and not old_path.is_symlink():
            continue
        if not old_path.is_file() or _path_is_link_or_reparse(old_path):
            raise RetiredPathError(f"retired source가 regular file이 아님: {old_path}")
        backup = _retired_backup_path(dest_root, old)
        _assert_no_link_components(dest_root, backup.parent, label="retired backup")
        planned.append((old, new, backup))
    return planned


def retire_manifest_paths(
    dest_root: Path,
    source_root: Path,
    manifest: list,
    manifest_paths: list[Path],
    *,
    write: bool,
    prospective_replacements: dict[str, Path] | None = None,
) -> list[tuple[str, Path]]:
    """full self-sync의 명시 퇴역 경로를 backup으로 원자 이동한다."""
    declarations = validated_retired_path_directives(
        source_root, [Path(path) for path in manifest_paths]
    )
    if not declarations:
        return []

    dest_root = Path(dest_root)
    source_root = Path(source_root)
    if not write:
        plan = _validate_retired_path_plan(
            dest_root, source_root, manifest, declarations,
            prospective_replacements=prospective_replacements,
        )
    else:
        # lock 파일도 쓰기이므로 선언·경로·hash를 먼저 전부 검증한다.
        _validate_retired_path_plan(dest_root, source_root, manifest, declarations)
        lock_path = dest_root / RETIRED_PATH_LOCK_REL
        _assert_no_link_components(dest_root, lock_path.parent, label="retired lock")
        with fs.exclusive_file_lock(lock_path):
            plan = _validate_retired_path_plan(
                dest_root, source_root, manifest, declarations,
            )
            if plan:
                install_files.ensure_backup_dir_ignored(dest_root)
            for old, _new, backup in plan:
                backup.parent.mkdir(parents=True, exist_ok=True)
                # overwrite 금지: 위에서 고른 좌표가 락 구간에서도 비어 있다.
                if backup.exists() or backup.is_symlink():
                    raise RetiredPathError(f"retired backup 충돌: {backup}")
                os.replace(dest_root / old, backup)
                if (dest_root / old).exists() or (dest_root / old).is_symlink():
                    raise RetiredPathError(f"retired source 사후조건 실패: {old}")
    return [(old, backup) for old, _new, backup in plan]


def _run_retired_path_migration(
    effective_dest: Path,
    source_root: Path,
    manifest: list,
    selfheal: dict,
    *,
    write: bool,
    changes: list[tuple] | None = None,
) -> bool:
    """retired migration 실행+보고. 실패는 호출부가 baseline 억제로 바꾼다."""
    manifest_paths = [Path(path) for path in selfheal.get("upstream_manifests", [])]
    if not manifest_paths:
        manifest_paths = [Path(source_root) / ".project_manager" / "engine.manifest"]
    # 구 upstream은 manifest 자체가 없을 수 있다. 명시 이주 채널이 없는
    # 세대를 기존과 같이 fail-soft로 지나간다(임의 삭제로 추측하지 않음).
    manifest_paths = [path for path in manifest_paths if path.is_file()]
    if not manifest_paths:
        return True
    # legacy_preserved는 로컬 manifest를 의도적으로 승격하지 않은 형상이다.
    # 그 형상의 계획 manifest가 replacement를 소유하지 않으면 신 파일도
    # 설치하지 않으므로 퇴역을 유보한다(구 파일 단독 제거 금지).
    if selfheal.get("status") == "legacy_preserved":
        owned = {str(entry).replace("\\", "/") for entry in manifest}
        try:
            declared = [
                pair
                for path in manifest_paths
                for pair in parse_retired_path_directives(path)
            ]
        except (OSError, UnicodeError, RetiredPathError) as exc:
            print(f"[중단] retired-path 이주 실패 — {exc}", file=sys.stderr)
            return False
        if declared and not any(manifest_rules._path_owned_by(new, owned) for _old, new in declared):
            return True
    try:
        prospective = {
            str(rel).replace("\\", "/"): Path(src)
            for rel, src, _dst, _kind in (changes or [])
        }
        planned = retire_manifest_paths(
            effective_dest, source_root, manifest, manifest_paths, write=write,
            prospective_replacements=prospective if not write else None,
        )
    except (OSError, UnicodeError, RetiredPathError, install_files.UnsafeDestPathError) as exc:
        print(f"[중단] retired-path 이주 실패 — {exc}", file=sys.stderr)
        return False
    verb = "퇴역 예정" if not write else "퇴역"
    for old, backup in planned:
        print(f"  [{verb}] {old} -> {backup}")
    return True
