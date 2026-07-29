#!/usr/bin/env python3
"""Repo가 소유한 파일 엔트리를 git 우선으로 열거하는 공용 seam.

소비자는 필요한 집합을 의미로 선언한다.

* ``tracked_only``: 추적분만. adopter에게 전파하는 출하 경로용.
* ``owned``: 추적분 + 미추적·비무시 파일. gap/검사 경로용.

git을 쓸 수 없거나 ``ls-files``가 실패하면 파일시스템 순회로 폴백한다. 이때는 ignore와
추적 여부 보장이 사라지므로 ``RepoFilesFallbackWarning``을 반드시 표면화한다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Callable, Literal, NamedTuple


TRACKED_ONLY = "tracked_only"
OWNED = "owned"
RepoFileMode = Literal["tracked_only", "owned"]
GitRunner = Callable[[list], "tuple[int, str]"]

_MODE_GIT_ARGS: dict[RepoFileMode, tuple[str, ...]] = {
    TRACKED_ONLY: ("--cached",),
    OWNED: ("--cached", "--others", "--exclude-standard"),
}
_FALLBACK_EXCLUDE_NAMES = frozenset({".git", "__pycache__", ".pytest_cache"})
GIT_TIMEOUT_SECONDS = 120


class RepoFilesFallbackWarning(RuntimeWarning):
    """git 열거 보장이 사라져 filesystem 전수 순회로 강등됐다는 loud 신호."""


class RepoFilesEmptyWarning(RuntimeWarning):
    """tracked-only가 비어 실제 비어 있지 않은 subtree를 누락할 수 있다는 loud 신호."""


class RepoOwnedEntry(NamedTuple):
    """repo 열거 엔트리와 git index mode.

    ``index_mode``는 git tracked 결과에서만 채워진다. OWNED의 untracked 엔트리와
    filesystem fallback 엔트리는 ``None``이다.
    """

    path: Path
    index_mode: str | None


def _real_git_runner(cwd: Path) -> GitRunner:
    """``git -C <cwd>`` argv runner. 실패는 rc!=0으로 돌려 폴백 경로에 맡긴다."""
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            result = subprocess.run(
                [git_binary, "-C", str(cwd), *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
            )
            return result.returncode, result.stdout or ""
        except Exception as exc:  # noqa: BLE001 — 호출 실패도 loud filesystem fallback 대상.
            return 1, str(exc)

    return runner


def list_repo_owned_entries(
    checkout: Path | str,
    subtree: Path | str,
    *,
    mode: RepoFileMode,
    git_runner: GitRunner | None = None,
) -> list[RepoOwnedEntry]:
    """``checkout`` 기준 ``subtree`` 아래 repo 소유 엔트리와 index mode를 반환한다.

    TRACKED_ONLY는 ``ls-files --format``으로 mode를 함께 보존한다. git 성공 결과는
    working-tree 상태로 재검사하지 않는다. ``ls-files``가 이미 ignore와 pathspec을 적용한
    git 파일형 엔트리의 진실이며, ``is_file()`` 재검사는 symlink와 mode 160000 gitlink를
    거짓 탈락시킨다.

    git 실패 시에는 기존 domain 구현과 같은 파일시스템 폴백을 사용한다. 폴백은 추적/ignore
    의미를 보장할 수 없어 경고가 계약의 일부다.
    """
    try:
        mode_args = _MODE_GIT_ARGS[mode]
    except KeyError as exc:
        choices = ", ".join(sorted(_MODE_GIT_ARGS))
        raise ValueError(f"알 수 없는 repo 파일 열거 mode {mode!r}; 허용: {choices}") from exc

    checkout_path = Path(checkout)
    norm = str(subtree).replace(os.sep, "/").replace("\\", "/").rstrip("/") or "."
    runner = git_runner if git_runner is not None else _real_git_runner(checkout_path)
    format_args = (
        ["--format=%(objectmode)%x09%(path)"]
        if mode == TRACKED_ONLY
        else []
    )
    try:
        rc, out = runner([
            "ls-files",
            "-z",
            *format_args,
            *mode_args,
            "--",
            f":(literal){norm}",
        ])
    except Exception as exc:  # noqa: BLE001 — 주입 runner 실패도 filesystem fallback.
        rc, out = 1, str(exc)

    entries: list[RepoOwnedEntry] = []
    if rc == 0:
        for candidate_text in out.split("\0"):
            if not candidate_text:
                continue
            # ls-files가 이미 ignore와 pathspec을 적용한 git 파일형 엔트리의 진실이다.
            # working-tree is_file() 재검사는 symlink와 mode 160000 gitlink를 거짓 탈락시킨다.
            if mode == TRACKED_ONLY:
                index_mode, separator, candidate_text = candidate_text.partition("\t")
                if separator != "\t" or not index_mode:
                    raise RuntimeError(
                        "git ls-files index mode 출력이 손상됨: "
                        f"{candidate_text!r}"
                    )
            else:
                index_mode = None
            entries.append(RepoOwnedEntry(Path(candidate_text), index_mode))
        directory = checkout_path / norm
        if mode == TRACKED_ONLY and not entries and directory.is_dir():
            try:
                disk_nonempty = next(directory.iterdir(), None) is not None
            except OSError:
                disk_nonempty = False
            if disk_nonempty:
                warnings.warn(
                    "repo-owned tracked_only 열거가 빈 결과지만 디스크 subtree는 비어 있지 않음 "
                    f"(checkout={checkout_path}, subtree={norm!r}); git add/ignore 상태와 "
                    "checkout 루트 정합을 확인하라",
                    RepoFilesEmptyWarning,
                    stacklevel=2,
                )
    else:
        warnings.warn(
            "repo-owned 파일 열거가 git ls-files 실패로 filesystem 전수 순회에 강등됨 "
            f"(checkout={checkout_path}, subtree={norm!r}, mode={mode}, rc={rc}); "
            "추적/ignore 보장을 적용할 수 없음",
            RepoFilesFallbackWarning,
            stacklevel=2,
        )
        directory = checkout_path / norm
        entries = [
            RepoOwnedEntry(path.relative_to(checkout_path), None)
            for path in directory.rglob("*")
            if path.is_file()
            and not any(
                part in _FALLBACK_EXCLUDE_NAMES
                for part in path.relative_to(directory).parts
            )
        ]

    return sorted(
        dict.fromkeys(entries),
        key=lambda entry: entry.path.as_posix(),
    )


def list_repo_owned_files(
    checkout: Path | str,
    subtree: Path | str,
    *,
    mode: RepoFileMode,
    git_runner: GitRunner | None = None,
) -> list[Path]:
    """호환 API: repo 소유 엔트리에서 상대 ``Path`` 목록만 반환한다."""
    return [
        entry.path
        for entry in list_repo_owned_entries(
            checkout,
            subtree,
            mode=mode,
            git_runner=git_runner,
        )
    ]


def tracked_index_mode(
    checkout: Path | str,
    path: Path | str,
    *,
    git_runner: GitRunner | None = None,
) -> str | None:
    """단일 경로의 git index mode를 반환한다; 미추적/non-git이면 ``None``."""
    checkout_path = Path(checkout)
    norm = str(path).replace(os.sep, "/").replace("\\", "/").rstrip("/") or "."
    runner = git_runner if git_runner is not None else _real_git_runner(checkout_path)
    try:
        rc, out = runner([
            "ls-files",
            "-z",
            "--format=%(objectmode)%x09%(path)",
            "--cached",
            "--",
            f":(literal){norm}",
        ])
    except Exception:  # noqa: BLE001 — 조회 불능은 non-git/untracked와 같은 None.
        return None
    if rc != 0 or not out:
        return None
    record = out.split("\0", 1)[0]
    index_mode, separator, _candidate = record.partition("\t")
    if separator != "\t" or not index_mode:
        raise RuntimeError(f"git ls-files index mode 출력이 손상됨: {record!r}")
    return index_mode
