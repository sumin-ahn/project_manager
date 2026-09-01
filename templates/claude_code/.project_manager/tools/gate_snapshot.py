#!/usr/bin/env python3
"""검토 대상 경로의 신선도를 검증하는 격리 worktree 스냅샷 생성기.

사용:
    python3 .project_manager/tools/gate_snapshot.py \
        --repo /path/to/shared-worktree \
        --output /tmp/gate-review --paths src/file.py tests/

명시 경로마다 Git 소유 파일 집합을 확정한 뒤 detached worktree에 index를 overlay한다.
생성 전후의 공유 working tree와 생성된 스냅샷에서 대상 파일의 종류, 실행 비트,
Git 속성 정규화 blob OID를 대조한다. 대상 밖 변경은 비교하지 않는다. 생성된 격리
worktree의 전용 index만 내용과 동기화하며, 입력 working tree의 공유 index는 수정하지
않는다. 검증을 마친 스냅샷에는 생성 사실을 나타내는 로컬 JSON 마커를 남긴다.

병렬 wave에서는 같은 디렉터리의 다른 dev WIP가 검토 범위에 섞이지 않도록 ``--paths``를
파일 단위로 지정한다.

``--paths``가 그 저장소의 staged 변경 집합보다 좁으면 경고한다(리뷰어가 빠진 경로의 HEAD
판을 보게 되는 false-finding 입력). 기본은 rc를 바꾸지 않으며 ``--strict-scope``로 차단한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Sequence


_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                    # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                    # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                    # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                    # ModuleNotFoundError). 블록은 stdlib-only 라 지역 import 로 두되 sys.path 에
                    # parent 를 넣기 전에 가져와 그 트리의 동명 파일이 stdlib 를 가리지 않게 한다.
                    import importlib as _bootstrap_importlib
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        _bootstrap_importlib.invalidate_caches()
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise


# baked stamp. 소비처는 이 값을 자기 rev와 대조해 부분 동기된 구 사본을
# SnapshotError 속성 접근 전에 명시적인 sibling-skew 오류로 막는다.
ENGINE_REV = "v1.7.12"


_GATE_SNAPSHOT_MARKER = Path(
    ".project_manager/.local/gate-snapshot.json"
)


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV를 이 사본과 대조한다(skew만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True
        raise err


class SnapshotError(RuntimeError):
    """스냅샷을 만들거나 대상 경로의 신선도를 증명하지 못한 오류."""


class FileSignature(NamedTuple):
    kind: str
    executable: bool
    digest: str


class IndexEntry(NamedTuple):
    path: str
    mode: str
    oid: str
    stage: int


class Selection(NamedTuple):
    selectors: tuple[str, ...]
    files: tuple[str, ...]
    indexed: frozenset[str]
    head: frozenset[str]
    index_entries: tuple[IndexEntry, ...]
    head_oid: str


def _git(
    repo: Path, *args: str, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
        )
    except OSError as exc:
        raise SnapshotError(f"git 실행 실패: {exc}") from exc


def _checked_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = _git(repo, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "진단 없음"
        raise SnapshotError(
            f"git {' '.join(args)} 실패 (rc={result.returncode}): {detail}"
        )
    return result


def _checked_git_input(
    repo: Path, input_text: str, *args: str
) -> subprocess.CompletedProcess[str]:
    result = _git(repo, *args, input_text=input_text)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "진단 없음"
        raise SnapshotError(
            f"git {' '.join(args)} 실패 (rc={result.returncode}): {detail}"
        )
    return result


def _repo_root(anchor: Path) -> Path:
    result = _checked_git(anchor, "rev-parse", "--show-toplevel")
    value = result.stdout.strip()
    if not value:
        raise SnapshotError(f"git 저장소 루트를 해소하지 못했습니다: {anchor}")
    return Path(value).resolve()


def _relative_to_root(root: Path, absolute: Path) -> Path | None:
    """root 기준 상대 경로(밖이면 None) — 예외 흐름 대신 판정만 돌려준다."""
    try:
        return absolute.relative_to(root)
    except ValueError:
        return None


def _normalize_selector(root: Path, raw: str) -> str:
    candidate = Path(raw)
    absolute = Path(os.path.abspath(candidate if candidate.is_absolute() else root / candidate))
    relative = _relative_to_root(root, absolute)
    if relative is None:
        # `_repo_root`는 resolve()한 경로라, 저장소 경로 prefix에 심볼릭 링크가 끼면
        # lexical abspath가 저장소 밖으로 보여 false-red가 난다. prefix(부모 체인)만
        # 해소하고 leaf 이름은 그대로 둔다 — leaf까지 resolve하면 저장소 안 심볼릭 링크
        # 파일을 지정한 선택자가 링크 대상 경로로 바뀌어 검토 범위 자체가 달라진다.
        prefix_resolved = absolute.parent.resolve(strict=False) / absolute.name
        relative = _relative_to_root(root, prefix_resolved)
    if relative is None:
        raise SnapshotError(f"검토 경로가 저장소 밖입니다: {raw}")
    if not relative.parts:
        raise SnapshotError("저장소 전체('.')는 검토 경로로 지정할 수 없습니다.")
    if relative.parts[0] == ".git":
        raise SnapshotError(f"Git 메타데이터는 검토 경로로 지정할 수 없습니다: {raw}")
    return relative.as_posix()


def _selected_files(root: Path, selectors: Sequence[str]) -> Selection:
    normalized_selectors: set[str] = set()
    selected: set[str] = set()
    indexed: set[str] = set()
    head_files: set[str] = set()
    index_entries: set[IndexEntry] = set()
    head_oid = _checked_git(root, "rev-parse", "--verify", "HEAD").stdout.strip()
    if not head_oid:
        raise SnapshotError("스냅샷 기준 HEAD OID를 해소하지 못했습니다.")
    for raw in selectors:
        relative = _normalize_selector(root, raw)
        normalized_selectors.add(relative)
        pathspec = f":(top,literal){relative}"
        cached = _checked_git(
            root,
            "ls-files",
            "--stage",
            "-z",
            "--cached",
            "--",
            pathspec,
        )
        others = _checked_git(
            root,
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            "--",
            pathspec,
        )
        head = _checked_git(
            root, "ls-tree", "-r", "--name-only", "-z", head_oid, "--", pathspec
        )
        cached_entries: set[IndexEntry] = set()
        for record in cached.stdout.split("\0"):
            if not record:
                continue
            try:
                metadata, path = record.split("\t", 1)
                mode, oid, stage_text = metadata.split()
                cached_entries.add(IndexEntry(path, mode, oid, int(stage_text)))
            except (TypeError, ValueError) as exc:
                raise SnapshotError(
                    f"Git index stage 엔트리를 해석할 수 없습니다: {record!r}"
                ) from exc
        cached_matches = {entry.path for entry in cached_entries}
        other_matches = {path for path in others.stdout.split("\0") if path}
        head_matches = {path for path in head.stdout.split("\0") if path}
        matches = cached_matches | other_matches | head_matches
        if not matches:
            raise SnapshotError(
                f"검토 경로에 비교할 Git 소유 파일이 없습니다: {raw}"
            )
        selected.update(matches)
        indexed.update(cached_matches)
        index_entries.update(cached_entries)
        head_files.update(head_matches)
    if not selected:
        raise SnapshotError("검토 대상 파일 집합이 비었습니다.")
    return Selection(
        tuple(sorted(normalized_selectors)),
        tuple(sorted(selected)),
        frozenset(indexed),
        frozenset(head_files),
        tuple(sorted(index_entries)),
        head_oid,
    )


def _signature(path: Path, *, git_path: tuple[Path, str]) -> FileSignature | None:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise SnapshotError(f"대상 파일을 읽을 수 없습니다: {path} ({exc})") from exc

    if stat.S_ISREG(metadata.st_mode):
        root, relative = git_path
        digest = _checked_git(
            root, "hash-object", "--path", relative, "--", relative
        ).stdout.strip()
        if not digest:
            raise SnapshotError(
                f"Git 정규화 blob OID를 해소하지 못했습니다: {relative}"
            )
        return FileSignature("file", bool(metadata.st_mode & 0o111), digest)
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.readlink(path).encode("utf-8", errors="surrogateescape")
        except OSError as exc:
            raise SnapshotError(f"심볼릭 링크를 읽을 수 없습니다: {path} ({exc})") from exc
        return FileSignature("symlink", False, hashlib.sha256(target).hexdigest())
    if stat.S_ISDIR(metadata.st_mode):
        # selection의 각 항목은 어느 한 Git 상태에서는 blob이다. 그 blob 경로가
        # 다른 상태에서 디렉터리인 것은 file -> directory 전환의 삭제 쪽이다.
        return None
    raise SnapshotError(f"지원하지 않는 대상 파일 종류입니다: {path}")


def _signatures(
    root: Path, files: Sequence[str]
) -> dict[str, FileSignature | None]:
    signatures: dict[str, FileSignature | None] = {}
    for relative in files:
        path = root / relative
        signatures[relative] = _signature(path, git_path=(root, relative))
    return signatures


def _actual_files(root: Path, selectors: Sequence[str]) -> frozenset[str]:
    """선택 범위의 실 파일 집합을 ignored/untracked까지 포함해 열거한다."""
    files: set[str] = set()
    for relative in selectors:
        pathspec = f":(top,literal){relative}"
        listed = _checked_git(
            root, "ls-files", "-z", "--cached", "--others", "--", pathspec
        )
        for candidate in listed.stdout.split("\0"):
            if not candidate:
                continue
            try:
                metadata = (root / candidate).lstat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                raise SnapshotError(
                    f"대상 파일 집합을 열거할 수 없습니다: {candidate} ({exc})"
                ) from exc
            if not stat.S_ISDIR(metadata.st_mode):
                files.add(candidate)
    return frozenset(files)


# 진단 메시지에 나열하는 경로 상한. 넘치면 목록에는 `…`를, 처방 커맨드에는 절단 표시를 붙인다.
_DISPLAY_PATH_LIMIT = 8

# 성공 열거에서 스냅샷에 실리지 않은 대상에 붙이는 사유 표기.
_SNAPSHOT_ABSENT_NOTE = " (스냅샷에 없음 — index 기준 삭제)"


def _format_paths(paths: Sequence[str]) -> str:
    return ", ".join(paths[:_DISPLAY_PATH_LIMIT]) + (
        " …" if len(paths) > _DISPLAY_PATH_LIMIT else ""
    )


_PARALLEL_WAVE_GUIDANCE = (
    " 이 경로가 검토 대상이면 `git add`로 index를 갱신하고, "
    "다른 dev의 WIP이면 `--paths`를 파일 단위로 좁히십시오."
)


def _staged_paths(root: Path) -> tuple[str, ...]:
    """그 저장소의 staged 변경 경로 — HEAD 대비 index diff (정렬·중복 제거).

    ``--no-renames``로 rename 을 삭제+추가로 편다. rename 검출은 설정(`diff.renames`)에 따라
    켜지고 꺼져 같은 index 가 실행마다 다른 목록이 되며, 켜졌을 때는 **옛 경로**가 목록에서
    빠져 그 경로의 범위 누락을 못 본다. untracked 는 index 에 없어 애초에 이 집합 밖이고,
    다른 저장소는 이 ``root`` 로만 실행하므로 계산 입력이 되지 않는다.
    """
    listed = _checked_git(
        root, "diff", "--cached", "--name-only", "-z", "--no-renames",
    ).stdout
    return tuple(sorted({path for path in listed.split("\0") if path}))


def _uncovered_staged_paths(root: Path, selectors: Sequence[str]) -> tuple[str, ...]:
    """staged 변경 중 ``--paths`` 선택 범위가 덮지 않는 경로들."""
    covered = tuple(_normalize_selector(root, raw) for raw in selectors)
    return tuple(
        path for path in _staged_paths(root)
        if not any(
            path == selector or path.startswith(selector + "/")
            for selector in covered
        )
    )


def _scope_gap_message(paths: Sequence[str]) -> str:
    """범위 누락 진단 본문 — 경고와 `--strict-scope` 차단이 같은 문장을 쓴다."""
    return (
        "staged 변경인데 --paths 에 없음(리뷰어가 HEAD 판을 본다): "
        + _format_paths(paths)
        + " · 검토 대상이면 --paths 에 추가하고, 타 티켓 산출이면 그대로 진행"
    )


def check_staged_scope(
    root: Path, selectors: Sequence[str], *, strict: bool = False
) -> tuple[str, ...]:
    """``--paths``가 staged 변경 집합보다 좁으면 알린다(기본 경고·strict 면 차단).

    리뷰어가 dev 산출을 못 보면 이미 고쳐진 것을 must-fix 로 내는 false-finding 이 난다 —
    그 입력 누락을 계산으로 표면화한다. 기본은 rc 를 바꾸지 않는다(타 티켓 산출이 섞인
    공유 트리는 정상 형상이라 차단하면 게이트가 못 돈다). 확실히 좁혀 두고 싶은 호출만
    ``strict``로 차단한다.
    """
    uncovered = _uncovered_staged_paths(root, selectors)
    if not uncovered:
        return ()
    if strict:
        raise SnapshotError(_scope_gap_message(uncovered))
    print("경고: " + _scope_gap_message(uncovered), file=sys.stderr)
    return uncovered


def _ignored_paths(index_checkout: Path, paths: Sequence[str]) -> set[str]:
    ignored: set[str] = set()
    for path in paths:
        result = _git(index_checkout, "check-ignore", "-q", "--", path)
        if result.returncode == 0:
            ignored.add(path)
        elif result.returncode != 1:
            detail = result.stderr.strip() or result.stdout.strip() or "진단 없음"
            raise SnapshotError(f"git check-ignore 실패 (rc={result.returncode}): {detail}")
    return ignored


def _validate_live_selection(
    index_checkout: Path,
    selection: Selection,
    signatures: dict[str, FileSignature | None],
) -> dict[str, FileSignature | None]:
    staged_deletion_residue = sorted(
        path
        for path in selection.files
        if signatures[path] is not None
        and path not in selection.indexed
        and path in selection.head
    )
    # Ignore 면제도 검토 기준과 같은 캡처 index를 checkout한 스냅샷에서
    # 판정한다. 원본 working tree의 unstaged .gitignore는 개입하지 않는다.
    #
    # `--paths` 밖 .gitignore를 안정성 검사 대상에 넣는 안은 실측 후 기각했다. 면제
    # 판정의 기준은 언제나 `index_checkout`(=격리 스냅샷)이고, 스냅샷의 .gitignore는
    # index 복제 시점의 단일 `ls-files --stage` 읽기로 확정된 뒤 더 이상 바뀌지 않는다.
    # 즉 판정 근거와 리뷰어가 실제로 보는 산출물이 같은 트리라, 원본 저장소에서 범위 밖
    # .gitignore가 캡처 전후 어느 시점에 staged되든 판정과 산출물이 어긋나지 않는다.
    # 범위 밖 .gitignore를 비교 대상에 넣으면 검토와 무관한 병렬 dev의 index 변경이
    # 게이트를 막는 false-red만 늘어난다. 실측 회귀는 tests/test_gate_snapshot.py의
    # `test_out_of_scope_gitignore_*` 두 건이 양방향으로 고정한다.
    ignored_residue = _ignored_paths(index_checkout, staged_deletion_residue)
    if ignored_residue:
        # `git rm --cached f` + staged .gitignore는 index 관점의 정상 삭제다. 남아 있는
        # ignored working 파일을 스냅샷에 복사하거나 stale 변경으로 간주하지 않는다.
        signatures = dict(signatures)
        for path in ignored_residue:
            signatures[path] = None
        staged_deletion_residue = [
            path for path in staged_deletion_residue if path not in ignored_residue
        ]
    if staged_deletion_residue:
        raise SnapshotError(
            "index에서 삭제됐지만 working tree에 남은 파일이 있습니다. "
            "삭제를 working tree에도 반영하거나 index에 복원하십시오: "
            + _format_paths(staged_deletion_residue)
            + _PARALLEL_WAVE_GUIDANCE
        )

    untracked = sorted(
        path
        for path in selection.files
        if signatures[path] is not None
        and path not in selection.indexed
        and path not in selection.head
    )
    if untracked:
        raise SnapshotError(
            "스냅샷 index에 없는 untracked 신규 파일입니다: "
            + _format_paths(untracked)
            + _PARALLEL_WAVE_GUIDANCE
        )

    missing_from_worktree = sorted(
        path
        for path in selection.files
        if signatures[path] is None and path in selection.indexed
    )
    if missing_from_worktree:
        command_paths = " ".join(
            shlex.quote(path) for path in missing_from_worktree[:_DISPLAY_PATH_LIMIT]
        )
        # 커맨드도 목록과 같은 상한에서 잘린다. 표시가 없으면 그대로 복사해 실행한 사람이
        # 나머지를 stage했다고 오해한다 — 절단 사실과 전체 개수를 커맨드 밖에 병기한다.
        truncation = (
            f" (커맨드는 앞 {_DISPLAY_PATH_LIMIT}개만 표시 · 전체 "
            f"{len(missing_from_worktree)}개)"
            if len(missing_from_worktree) > _DISPLAY_PATH_LIMIT
            else ""
        )
        raise SnapshotError(
            "working tree에는 없지만 index에 남은 파일이 있습니다. "
            f"삭제를 `git add -u -- {command_paths}`로 stage하십시오{truncation}: "
            + _format_paths(missing_from_worktree)
            + _PARALLEL_WAVE_GUIDANCE
        )
    return signatures


def _verify_selection_stable(before: Selection, after: Selection) -> None:
    if before.head_oid != after.head_oid:
        raise SnapshotError(
            "스냅샷 생성 중 저장소 HEAD OID가 변경됐습니다: "
            f"{before.head_oid} -> {after.head_oid}"
        )
    if before.files != after.files:
        before_set = set(before.files)
        after_set = set(after.files)
        added = sorted(after_set - before_set)
        removed = sorted(before_set - after_set)
        details: list[str] = []
        if added:
            details.append("추가=" + _format_paths(added))
        if removed:
            details.append("제거=" + _format_paths(removed))
        raise SnapshotError(
            "스냅샷 생성 중 검토 대상 파일 집합이 변경됐습니다: "
            + "; ".join(details)
        )
    if before.index_entries != after.index_entries:
        raise SnapshotError(
            "스냅샷 생성 중 검토 대상의 Git index stage 엔트리(mode/OID/stage)가 "
            "변경됐습니다."
        )
    if before.indexed != after.indexed or before.head != after.head:
        raise SnapshotError(
            "스냅샷 생성 중 검토 대상의 Git index 또는 HEAD 집합이 변경됐습니다."
        )


def _verify_snapshot_basis(
    expected: Selection,
    snapshot: Selection,
    expected_actual_files: frozenset[str],
    snapshot_actual_files: frozenset[str],
) -> None:
    if snapshot.head_oid != expected.head_oid:
        raise SnapshotError(
            "격리 스냅샷의 HEAD OID가 생성 기준점과 다릅니다: "
            f"expected={expected.head_oid}, actual={snapshot.head_oid}"
        )
    if snapshot.index_entries != expected.index_entries:
        raise SnapshotError(
            "격리 스냅샷의 Git index stage 엔트리(mode/OID/stage)가 "
            "생성 기준점과 다릅니다."
        )
    if snapshot_actual_files != expected_actual_files:
        added = sorted(snapshot_actual_files - expected_actual_files)
        removed = sorted(expected_actual_files - snapshot_actual_files)
        details: list[str] = []
        if added:
            details.append("추가=" + _format_paths(added))
        if removed:
            details.append("제거=" + _format_paths(removed))
        raise SnapshotError(
            "격리 스냅샷의 선택 범위 실 파일 집합이 "
            "생성 기준점과 다릅니다: "
            + "; ".join(details)
        )


def _format_mismatches(
    expected: dict[str, FileSignature | None],
    actual: dict[str, FileSignature | None],
) -> str:
    changed = [path for path in expected if expected[path] != actual[path]]
    return _format_paths(changed)


def _replicate_index_and_checkout(root: Path, destination: Path) -> None:
    """공유 index 엔트리를 복제하고 gitlink를 제외한 stage-0 파일만 checkout한다."""
    staged = _checked_git(root, "ls-files", "--stage", "-z").stdout
    checkout_paths: list[str] = []
    for record in staged.split("\0"):
        if not record:
            continue
        try:
            metadata, path = record.split("\t", 1)
            mode, _oid, stage_text = metadata.split()
        except ValueError as exc:
            raise SnapshotError(
                f"Git index stage 엔트리를 해석할 수 없습니다: {record!r}"
            ) from exc
        if mode != "160000" and stage_text == "0":
            checkout_paths.append(path)

    # linked worktree의 HEAD index를 공유 index의 mode/OID/stage 엔트리로 교체한다.
    # gitlink는 index에만 복제하고 checkout 입력에서는 제외해 활성 원본 submodule을
    # 절대 이동시키지 않는다.
    _checked_git(destination, "read-tree", "--empty")
    _checked_git_input(destination, staged, "update-index", "-z", "--index-info")
    _checked_git_input(
        destination,
        "".join(path + "\0" for path in checkout_paths),
        "checkout-index",
        "-f",
        "-z",
        "--stdin",
    )


_WORKTREE_PORCELAIN_PREFIX = "worktree "


def _registered_worktrees(root: Path) -> tuple[Path, ...]:
    """등록된 worktree 경로들 (선언 순서·중복 제거 없음).

    `-z`(NUL 구분)로 읽는다. 줄 단위 porcelain은 개행이 든 경로를 두 줄로 흘리고 후행
    공백도 구분자와 섞여, 그런 경로를 가진 worktree가 거부 목록에서 통째로 빠지는
    차단 우회 창이 된다.
    """
    listing = _checked_git(root, "worktree", "list", "--porcelain", "-z").stdout
    return tuple(
        Path(field[len(_WORKTREE_PORCELAIN_PREFIX):]).resolve(strict=False)
        for field in listing.split("\0")
        if field.startswith(_WORKTREE_PORCELAIN_PREFIX)
    )


def _git_common_dir(root: Path) -> Path:
    common = Path(_checked_git(root, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = root / common
    return common.resolve(strict=False)


def _forbidden_output_locations(
    root: Path, registered: Sequence[Path] | None = None
) -> tuple[tuple[Path, str], ...]:
    """스냅샷 출력이 들어가면 안 되는 위치 — (경로, 사람이 읽는 이름) 순서쌍.

    공유 working tree뿐 아니라 같은 저장소의 Git 공용 디렉터리와 다른 등록 worktree까지
    막는다. 셋 중 어디에 만들어도 병렬 작업 트리나 Git 메타데이터를 오염시킨다.
    **다만 그 트리의 git 이 무시하는 자리는 이 목록에 걸려도 통과한다** — 오염 판정의 사실은
    `git check-ignore` 이고, 그 자리는 추적 대상이 아니라 오염 표면이 없다(`_reject_output_location`).

    디렉터리가 사라지고 등록만 남은(prunable) worktree는 제외한다. 그 자리는 지켜야 할
    작업 트리가 아니고, 같은 경로 재실행은 조치 가능한 별도 진단(prune 처방)이 맡는다.
    """
    if registered is None:
        registered = _registered_worktrees(root)
    locations: list[tuple[Path, str]] = [
        (root, "공유 저장소 working tree"),
        (_git_common_dir(root), "Git 공용 디렉터리"),
    ]
    locations.extend(
        (path, "같은 저장소의 다른 worktree")
        for path in registered
        if path.exists()
    )
    seen: set[Path] = set()
    unique: list[tuple[Path, str]] = []
    for location, label in locations:
        if location in seen:
            continue
        seen.add(location)
        unique.append((location, label))
    return tuple(unique)


def _git_ignores(tree: Path, destination: Path) -> bool:
    """그 트리의 git 이 이 자리를 무시하는가 — `git check-ignore` 의 사실만 본다.

    묻는 대상은 **오염될 그 트리 자신**이다. 바깥 저장소에 물으면 그 한 번의 rc 0 이 Git 공용
    디렉터리·다른 등록 worktree 거부까지 통째로 우회시킨다(무시되는 자리에 선 중첩 worktree).

    무시되는 자리는 추적 대상이 아니므로 스냅샷이 거기 서도 오염시킬 표면이 없다. rc 0(무시됨)
    에서만 참이고, rc 1(추적 중이거나 규칙에 안 맞음 — 추적 파일은 패턴이 맞아도 1이다)과
    rc 128(그 트리 밖이라 판정 불능 — Git 공용 디렉터리처럼 working tree 가 아닌 자리를 포함한다)은
    둘 다 거짓이다 — 확정 사실에서만 허용한다.
    """
    return _git(tree, "check-ignore", "-q", "--", str(destination)).returncode == 0


def _reject_output_location(root: Path, destination: Path) -> None:
    registered = _registered_worktrees(root)
    # 실재하는 자산의 오염이 먼저다 — 추적되는 자리는 등록 상태와 무관하게 그 진단을
    # 받아야 prune 뒤 두 번 실패하는 흐름이 안 생긴다.
    for location, label in _forbidden_output_locations(root, registered):
        if not destination.is_relative_to(location):
            continue
        if _git_ignores(location, destination):
            # gitignore 된 자리다 — 추적되지 않으므로 병렬 트리도 메타데이터도 오염되지 않는다.
            # 판정은 매치된 그 자리(`location`)에 묻는다. 오염될 트리가 판정 주체다.
            continue
        raise SnapshotError(
            f"격리 스냅샷은 저장소가 추적하는 자리에 만들 수 없습니다 — {label} 안입니다"
            f"(gitignore 된 자리는 허용): {destination} ({label}: {location})"
        )
    if any(path == destination and not path.exists() for path in registered):
        # `git worktree remove` 없이 디렉터리만 지운 뒤 같은 경로로 다시 도는 흐름이다.
        # '다른 자리에 만들라'는 안내는 조치가 불가능하다 — 등록 정리를 처방한다.
        raise SnapshotError(
            f"같은 경로에 삭제된 worktree 등록이 남아 있습니다: {destination}. "
            f"`git -C {shlex.quote(str(root))} worktree prune`으로 등록을 정리한 뒤 "
            "다시 실행하십시오."
        )


def _rollback(root: Path, output: Path) -> str | None:
    result = _git(root, "worktree", "remove", "--force", str(output))
    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or "진단 없음"
    return f"생성 실패 후 격리 worktree 정리도 실패했습니다: {detail}"


def _write_snapshot_marker(root: Path, destination: Path) -> Path:
    """일회용 게이트 스냅샷 마커를 destination 내부의 symlink 없는 경로에 기록한다."""
    marker = destination / _GATE_SNAPSHOT_MARKER
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_repo": str(root.resolve()),
        "target_path": str(destination.resolve()),
    }
    try:
        destination_root = destination.resolve(strict=True)
        marker_parent_real = marker.parent.resolve(strict=False)
        try:
            marker_parent_real.relative_to(destination_root)
        except ValueError as exc:
            raise SnapshotError(
                "게이트 스냅샷 마커 부모가 destination 밖을 가리킵니다"
                f"(symlink 추종 거부): {marker.parent} -> {marker_parent_real}"
            ) from exc

        # realpath가 destination 안을 가리키는 내부 symlink도 거부한다. 이 마커는 생성기가
        # 소유하는 런타임 파일이므로, 추적된 `.project_manager`/`.local` 링크 아래에는 쓰지 않는다.
        current = destination_root
        for part in _GATE_SNAPSHOT_MARKER.parent.parts:
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise SnapshotError(
                    f"게이트 스냅샷 마커 부모에 symlink 컴포넌트가 있습니다: {current}"
                )
            if not stat.S_ISDIR(mode):
                raise SnapshotError(
                    f"게이트 스냅샷 마커 부모 컴포넌트가 디렉터리가 아닙니다: {current}"
                )

        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        secure_dir_fd = (
            hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
            and os.open in getattr(os, "supports_dir_fd", frozenset())
            and os.mkdir in getattr(os, "supports_dir_fd", frozenset())
        )
        if secure_dir_fd:
            dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            current_fd = os.open(destination_root, dir_flags)
            marker_fd: int | None = None
            try:
                for part in _GATE_SNAPSHOT_MARKER.parent.parts:
                    try:
                        next_fd = os.open(part, dir_flags, dir_fd=current_fd)
                    except FileNotFoundError:
                        try:
                            os.mkdir(part, dir_fd=current_fd)
                        except FileExistsError:
                            pass
                        next_fd = os.open(part, dir_flags, dir_fd=current_fd)
                    os.close(current_fd)
                    current_fd = next_fd
                file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                marker_fd = os.open(
                    _GATE_SNAPSHOT_MARKER.name,
                    file_flags,
                    0o600,
                    dir_fd=current_fd,
                )
                with os.fdopen(
                        marker_fd, "w", encoding="utf-8", newline="\n") as handle:
                    marker_fd = None
                    handle.write(serialized)
            finally:
                if marker_fd is not None:
                    os.close(marker_fd)
                os.close(current_fd)
        else:
            # dir_fd/O_NOFOLLOW 미지원 플랫폼 폴백. 쓰기 직전에 부모를 만든 뒤 realpath와
            # 각 컴포넌트를 다시 검증하고 exclusive-create로 marker 자체 symlink도 거부한다.
            marker.parent.mkdir(parents=True, exist_ok=True)
            if marker.parent.resolve(strict=True) != marker_parent_real:
                raise SnapshotError(
                    f"게이트 스냅샷 마커 부모가 검증 중 변경됐습니다: {marker.parent}"
                )
            current = destination_root
            for part in _GATE_SNAPSHOT_MARKER.parent.parts:
                current /= part
                if current.is_symlink() or not current.is_dir():
                    raise SnapshotError(
                        f"게이트 스냅샷 마커 부모 경로가 안전하지 않습니다: {current}"
                    )
            with marker.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
    except SnapshotError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SnapshotError(f"게이트 스냅샷 마커 기록 실패: {marker} ({exc})") from exc
    return marker


def create_snapshot(
    repo: Path, output: Path, paths: Sequence[str]
) -> tuple[Path, tuple[str, ...]]:
    """스냅샷을 만들고 명시 대상 파일만 working tree와 동일함을 검증한다."""
    root = _repo_root(repo)
    requested_output = Path(os.path.abspath(output))
    if os.path.lexists(requested_output):
        raise SnapshotError(f"스냅샷 출력 경로가 이미 존재합니다: {requested_output}")
    destination = requested_output.resolve(strict=False)
    _reject_output_location(root, destination)
    if not destination.parent.is_dir():
        raise SnapshotError(f"스냅샷 출력 부모 디렉터리가 없습니다: {destination.parent}")

    selection_before = _selected_files(root, paths)
    selectors = selection_before.selectors
    before = _signatures(root, selection_before.files)
    add = _git(
        root,
        "worktree",
        "add",
        "--detach",
        "--no-checkout",
        str(destination),
    )
    if add.returncode != 0:
        detail = add.stderr.strip() or add.stdout.strip() or "진단 없음"
        raise SnapshotError(f"격리 worktree 생성 실패 (rc={add.returncode}): {detail}")

    try:
        _replicate_index_and_checkout(root, destination)
        selection_after = _selected_files(root, selectors)
        _verify_selection_stable(selection_before, selection_after)
        before = _validate_live_selection(destination, selection_before, before)
        after = _signatures(root, selection_after.files)
        after = _validate_live_selection(destination, selection_after, after)
        expected_actual_files = frozenset(
            path for path, signature in after.items() if signature is not None
        )
        snapshot_selection_before = _selected_files(destination, selectors)
        snapshot_actual_before = _actual_files(destination, selectors)
        _verify_snapshot_basis(
            selection_before,
            snapshot_selection_before,
            expected_actual_files,
            snapshot_actual_before,
        )
        snapshot_before = _signatures(destination, selection_after.files)
        if before != after:
            changed = _format_mismatches(before, after)
            raise SnapshotError(
                "스냅샷 생성 중 검토 대상 working tree가 변경됐습니다: " + changed
            )
        if after != snapshot_before:
            changed = _format_mismatches(after, snapshot_before)
            raise SnapshotError(
                "격리 스냅샷이 검토 대상 working tree와 다릅니다: "
                + changed
                + _PARALLEL_WAVE_GUIDANCE
            )

        selection_final = _selected_files(root, selectors)
        _verify_selection_stable(selection_after, selection_final)
        final = _signatures(root, selection_final.files)
        final = _validate_live_selection(destination, selection_final, final)
        final_expected_actual_files = frozenset(
            path for path, signature in final.items() if signature is not None
        )
        snapshot_selection_final = _selected_files(destination, selectors)
        snapshot_actual_final = _actual_files(destination, selectors)
        _verify_snapshot_basis(
            selection_before,
            snapshot_selection_final,
            final_expected_actual_files,
            snapshot_actual_final,
        )
        snapshot_final = _signatures(destination, selection_final.files)
        if after != final:
            changed = _format_mismatches(after, final)
            raise SnapshotError(
                "스냅샷 검증 중 검토 대상 working tree가 변경됐습니다: " + changed
            )
        if snapshot_before != snapshot_final:
            changed = _format_mismatches(snapshot_before, snapshot_final)
            raise SnapshotError(
                "스냅샷 검증 중 격리 스냅샷이 변경됐습니다: " + changed
            )
        if final != snapshot_final:
            changed = _format_mismatches(final, snapshot_final)
            raise SnapshotError(
                "격리 스냅샷이 검토 대상 working tree와 다릅니다: "
                + changed
                + _PARALLEL_WAVE_GUIDANCE
            )
        # 내용·index·HEAD bookend가 모두 닫힌 성공 스냅샷에만 사실 마커를 남긴다. 마커는
        # `.project_manager/.local/` 런타임 메타데이터라 검토 대상 overlay에는 섞이지 않는다.
        # 기록 실패도 성공으로 강등하지 않고 아래 공통 rollback 경로를 탄다.
        _write_snapshot_marker(root, destination)
    except Exception as exc:
        cleanup_error = _rollback(root, destination)
        if cleanup_error is not None:
            raise SnapshotError(f"{exc}\n{cleanup_error}") from exc
        raise

    return destination, selection_final.files


def snapshot_marker_path(destination: Path) -> Path:
    """그 트리가 이 생성기의 산출임을 말하는 마커 경로 — 판독자가 규약을 재조립하지 않게 한다."""
    return Path(destination) / _GATE_SNAPSHOT_MARKER


def is_snapshot(destination: Path) -> bool:
    """이 트리가 검증을 마친 격리 스냅샷인가(마커 실재로 판정)."""
    return snapshot_marker_path(destination).is_file()


def remove_snapshot(repo: Path, output: Path) -> str | None:
    """다 쓴 격리 스냅샷을 등록까지 지운다 — 실패 사유(정상이면 None).

    생성이 등록(`worktree add`)까지 했으므로 제거도 생성기가 소유한다: 호출부가 디렉터리만
    지우면 저장소에 삭제된 worktree 등록이 남아 같은 경로의 다음 생성이 거부된다.
    """
    return _rollback(_repo_root(repo), Path(os.path.abspath(output)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="검토 대상 경로가 최신임을 검증하는 격리 worktree 스냅샷 생성기"
    )
    parser.add_argument("--repo", type=Path, required=True, help="공유 working tree")
    parser.add_argument("--output", type=Path, required=True, help="저장소 밖의 새 스냅샷 경로")
    parser.add_argument(
        "--paths",
        nargs="+",
        required=True,
        help="검토 대상 파일 또는 디렉터리(병렬 wave에서는 파일 단위로 지정)",
    )
    parser.add_argument(
        "--strict-scope",
        action="store_true",
        help="staged 변경이 --paths 밖에 있으면 경고 대신 차단(rc=1)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 인자를 검증하고 격리 게이트 스냅샷을 생성한다."""
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    args = build_parser().parse_args(argv)
    try:
        root = _repo_root(args.repo)
        # 범위 누락 판정은 스냅샷 생성 **전**이다 — strict 차단이 worktree 를 남기지 않고,
        # 경고도 리뷰어에게 무엇을 더 stage 할지 생성 결과보다 먼저 알려준다.
        check_staged_scope(root, args.paths, strict=args.strict_scope)
        output, files = create_snapshot(root, args.output, args.paths)
    except SnapshotError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    print(
        f"격리 스냅샷 생성 완료: {output} "
        f"(repo root: {root}, 검증 대상 {len(files)}개)"
    )
    # 개수만 찍으면 무엇을 검증했는지 확인할 방법이 없다. 호출자가 의도한 검토 범위와
    # 대조할 수 있도록 절단 없이 전량 열거한다. 스냅샷에 없는 항목은 검증 누락이 아니라
    # index 기준 삭제이므로, 리뷰어가 부재를 결함으로 읽지 않도록 사유를 병기한다.
    for path in files:
        note = "" if os.path.lexists(output / path) else _SNAPSHOT_ABSENT_NOTE
        print(f"  - {path}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
