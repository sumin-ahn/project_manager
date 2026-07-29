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
from typing import Callable, Literal


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


def list_repo_owned_files(
    checkout: Path | str,
    subtree: Path | str,
    *,
    mode: RepoFileMode,
    git_runner: GitRunner | None = None,
) -> list[Path]:
    """``checkout`` 기준 ``subtree`` 아래 repo 소유 파일 엔트리를 POSIX 상대경로로 반환한다.

    git 성공 결과는 working-tree 상태로 재검사하지 않는다. ``ls-files``가 이미 ignore와
    pathspec을 적용한 git 파일형 엔트리의 진실이며, ``is_file()`` 재검사는 symlink와 mode
    160000 gitlink를 거짓 탈락시킨다.

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
    try:
        rc, out = runner([
            "ls-files",
            "-z",
            *mode_args,
            "--",
            f":(literal){norm}",
        ])
    except Exception as exc:  # noqa: BLE001 — 주입 runner 실패도 filesystem fallback.
        rc, out = 1, str(exc)

    relative_files: list[Path] = []
    if rc == 0:
        for candidate_text in out.split("\0"):
            if not candidate_text:
                continue
            # ls-files가 이미 ignore와 pathspec을 적용한 git 파일형 엔트리의 진실이다.
            # working-tree is_file() 재검사는 symlink와 mode 160000 gitlink를 거짓 탈락시킨다.
            relative_files.append(Path(candidate_text))
    else:
        warnings.warn(
            "repo-owned 파일 열거가 git ls-files 실패로 filesystem 전수 순회에 강등됨 "
            f"(checkout={checkout_path}, subtree={norm!r}, mode={mode}, rc={rc}); "
            "추적/ignore 보장을 적용할 수 없음",
            RepoFilesFallbackWarning,
            stacklevel=2,
        )
        directory = checkout_path / norm
        relative_files = [
            path.relative_to(checkout_path)
            for path in directory.rglob("*")
            if path.is_file()
            and not any(
                part in _FALLBACK_EXCLUDE_NAMES
                for part in path.relative_to(directory).parts
            )
        ]

    return sorted(dict.fromkeys(relative_files), key=lambda path: path.as_posix())
