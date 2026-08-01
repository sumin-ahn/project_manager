#!/usr/bin/env python3
"""Repo가 소유한 파일 엔트리를 git 우선으로 열거하는 공용 seam.

소비자는 필요한 집합을 의미로 선언한다.

* ``tracked_only``: 추적분만. adopter에게 전파하는 출하 경로용.
* ``owned``: 추적분 + 미추적·비무시 파일. gap/검사 경로용.

git 바이너리가 없거나 checkout이 저장소가 아니면 파일시스템 순회로 폴백한다. 이때는
ignore와 추적 여부 보장이 사라지므로 ``RepoFilesFallbackWarning``을 반드시 표면화한다.
그 밖의 TRACKED_ONLY 실패(구 git의 옵션 미지원·손상 index·unmerged)는 추적정보가 있는데
폴백해 누출하지 않도록 loud 실패한다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any, Callable, Literal, NamedTuple

# baked 엔진 rev — 여러 sibling deep-import 경계가 이 공용 seam을 검증한다.
ENGINE_REV = "v1.5.1"


TRACKED_ONLY = "tracked_only"
OWNED = "owned"
RepoFileMode = Literal["tracked_only", "owned"]
GitRunner = Callable[[list[str]], "tuple[int, str]"]
GitOutputMode = Literal["stdout", "stdout_stderr", "stdout_or_error"]

_MODE_GIT_ARGS: dict[RepoFileMode, tuple[str, ...]] = {
    TRACKED_ONLY: ("--stage", "--cached"),
    OWNED: ("--cached", "--others", "--exclude-standard"),
}
_FALLBACK_EXCLUDE_NAMES = frozenset({".git", "__pycache__", ".pytest_cache"})
GIT_TIMEOUT_SECONDS = 120
_GIT_BINARY_MISSING_RC = 127


class RepoFilesFallbackWarning(RuntimeWarning):
    """git 추적/ignore 보장이 사라져 filesystem 전수 순회로 강등됐다는 loud 신호."""


class RepoFilesEmptyWarning(RuntimeWarning):
    """tracked-only가 비어 실제 비어 있지 않은 subtree를 누락할 수 있다는 loud 신호."""


class RepoFilesGitError(RuntimeError):
    """추적정보가 있는 checkout에서 git 열거 보장을 지키지 못해 출하를 중단한 오류."""


class RepoOwnedEntry(NamedTuple):
    """repo 열거 엔트리와 git index mode.

    ``index_mode``는 TRACKED_ONLY 결과에서만 채워진다. OWNED 엔트리와 filesystem
    fallback 엔트리는 ``None``이다.
    """

    path: Path
    index_mode: str | None


# 통합 전 captured runner 의미 차이(조용한 동작 변경 방지):
#
# | 소비처             | git 부재 rc | 출력                         | timeout               | locale |
# | repo_owned_files   | 127         | 성공 stdout / 실패 진단      | 120                   | C      |
# | domain             | 1           | rc와 무관하게 stdout         | 120                   | 상속   |
# | worktree_pool      | 1           | stdout + stderr              | 동적·captured 유한 cap | 상속   |
#
# 차이는 각 소비처의 기존 계약이다. 아래 옵션은 새 정책이 아니라 이 세 의미를 그대로 옮기기 위한
# 호환 축이다. worktree_pool의 console-visible runner와 보호훅 셸 git 환경 격리는 대상이 아니다.
def real_git_runner(
        cwd: Path,
        *,
        missing_binary_rc: int,
        timeout: "float | None",
        output_mode: GitOutputMode,
        force_c_locale: bool = False,
        which: "Callable[[str], str | None] | None" = None,
        run: "Callable[..., Any] | None" = None,
) -> GitRunner:
    """정책 옵션을 보존해 ``git -C <cwd>``를 실행하는 공용 captured runner.

    ``which``/``run`` 주입은 기존 소비처의 monkeypatch seam을 보존한다. 제품 호출부는 각
    모듈의 ``shutil.which``/``subprocess.run``을 넘기며, 테스트 외 동작은 stdlib 그대로다.
    """
    which_git = which if which is not None else shutil.which
    run_git = run if run is not None else subprocess.run
    git_binary = which_git("git")

    def runner(argv: list[str]) -> tuple[int, str]:
        if git_binary is None:
            return missing_binary_rc, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            kwargs: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": timeout,
            }
            if force_c_locale:
                env = os.environ.copy()
                env["LC_ALL"] = "C"
                env["LANGUAGE"] = ""
                kwargs["env"] = env
            result = run_git(
                [git_binary, "-C", str(cwd), *argv],
                **kwargs,
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            if output_mode == "stdout":
                out = stdout
            elif output_mode == "stdout_stderr":
                out = stdout + stderr
            else:
                out = stdout if result.returncode == 0 else stderr or stdout
            return result.returncode, out
        except FileNotFoundError as exc:
            # which 이후 바이너리가 사라지는 exec 경쟁은 각 소비처의 기존 git 부재 rc로 보존한다.
            return missing_binary_rc, str(exc)
        except Exception as exc:  # noqa: BLE001 — 호출 실패도 각 상위 계약이 rc로 처리.
            return 1, str(exc)

    return runner


def _real_git_runner(cwd: Path) -> GitRunner:
    """repo 소유 파일 열거용 ``git -C <cwd>`` runner.

    성공은 stdout만, 실패는 진단 문자열을 반환한다. rc 127은 git 실행 파일 부재를 뜻하는
    seam 예약값이며, 그 밖의 실패는 실제 git rc를 보존한다. 주입 runner도 이 규약과
    ``rev-parse --git-dir``의 rc 128=비-repo 경계를 따라야 한다.
    """
    return real_git_runner(
        cwd,
        missing_binary_rc=_GIT_BINARY_MISSING_RC,
        timeout=GIT_TIMEOUT_SECONDS,
        output_mode="stdout_or_error",
        force_c_locale=True,
        which=shutil.which,
        run=subprocess.run,
    )


def _parse_staged_entries(out: str) -> list[RepoOwnedEntry]:
    """``ls-files -z --stage`` 출력을 stage-0 tracked 엔트리로 파싱한다.

    unmerged 경로는 stage 1~3 레코드가 여러 개일 수 있다. 일부 stage만 골라 출하하면
    충돌 중인 내용을 임의 선택하게 되므로, 하나라도 발견되면 경로를 명시해 전체 조회를
    실패시킨다.
    """
    entries_by_path: dict[Path, RepoOwnedEntry] = {}
    unmerged_paths: set[Path] = set()
    for record in out.split("\0"):
        if not record:
            continue
        meta, separator, candidate_text = record.partition("\t")
        fields = meta.split()
        if separator != "\t" or not candidate_text or len(fields) != 3:
            raise RepoFilesGitError(f"git ls-files --stage 출력이 손상됨: {record!r}")
        index_mode, _object_id, stage = fields
        candidate = Path(candidate_text)
        if stage != "0":
            unmerged_paths.add(candidate)
            continue
        prior = entries_by_path.get(candidate)
        if prior is not None and prior.index_mode != index_mode:
            raise RepoFilesGitError(
                "git ls-files --stage stage-0 mode가 중복·불일치함: "
                f"{candidate_text!r} ({prior.index_mode}, {index_mode})"
            )
        entries_by_path[candidate] = RepoOwnedEntry(candidate, index_mode)

    if unmerged_paths:
        paths = ", ".join(path.as_posix() for path in sorted(unmerged_paths))
        raise RepoFilesGitError(
            "git index에 unmerged 경로가 있어 tracked-only 출하를 중단함: "
            + paths
        )
    return list(entries_by_path.values())


def _probe_no_repository(runner: GitRunner) -> bool:
    """git 진단 문구가 아니라 저장소 구조 프로브 rc로 비-repo를 판정한다."""
    try:
        rc, _git_dir = runner(["rev-parse", "--git-dir"])
    except Exception:
        return False
    return rc == 128


def list_repo_owned_entries(
    checkout: Path | str,
    subtree: Path | str,
    *,
    mode: RepoFileMode,
    git_runner: GitRunner | None = None,
) -> list[RepoOwnedEntry]:
    """``checkout`` 기준 ``subtree`` 아래 repo 소유 엔트리와 index mode를 반환한다.

    TRACKED_ONLY는 오래된 git도 지원하는 ``ls-files --stage --cached``로 mode와 stage를
    함께 보존한다. stage 0만 반환하며 unmerged 경로는 경로를 명시해 실패한다. git 성공 결과는
    working-tree 상태로 재검사하지 않는다. ``ls-files``가 이미 ignore와 pathspec을 적용한
    git 파일형 엔트리의 진실이며, ``is_file()`` 재검사는 symlink와 mode 160000 gitlink를
    거짓 탈락시킨다.

    git 바이너리 부재(rc 127) 또는 실패 뒤 ``rev-parse --git-dir``가 rc 128인 경우는
    추적정보 자체가 없는 비-git 배포 사본이므로 양 mode 모두 filesystem 폴백한다.
    주입 ``git_runner``도 rc 127=git 부재, 구조 프로브 rc 128=비-repo 규약을 지켜야 한다.
    프로브 rc 0은 원 실패 진단 문구와 무관하게 저장소가 있는 것으로 판정한다. 폴백은
    추적/ignore 의미를 보장할 수 없어 경고가 계약의 일부다. TRACKED_ONLY의 그 밖의 실패는
    추적정보가 있는 상태에서 누출하지 않도록 loud 실패한다.
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
    except Exception as exc:  # noqa: BLE001 — mode 계약에 따라 loud 실패/폴백.
        if mode == TRACKED_ONLY:
            raise RepoFilesGitError(
                "repo-owned tracked_only git ls-files 호출 실패 "
                f"(checkout={checkout_path}, subtree={norm!r})"
            ) from exc
        rc, out = 1, str(exc)

    entries: list[RepoOwnedEntry] = []
    fallback_rc: int | None = None
    if rc == 0:
        if mode == TRACKED_ONLY:
            entries = _parse_staged_entries(out)
        else:
            entries = [
                RepoOwnedEntry(Path(candidate_text), None)
                for candidate_text in out.split("\0")
                if candidate_text
            ]
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
    elif rc == _GIT_BINARY_MISSING_RC or _probe_no_repository(runner):
        fallback_rc = rc
    elif mode == TRACKED_ONLY:
        detail = out.strip()
        suffix = f": {detail}" if detail else ""
        raise RepoFilesGitError(
            "repo-owned tracked_only git ls-files 실패 "
            f"(checkout={checkout_path}, subtree={norm!r}, rc={rc}){suffix}"
        )
    else:
        fallback_rc = rc

    if fallback_rc is not None:
        reason = (
            "git 저장소/바이너리 부재"
            if fallback_rc in {_GIT_BINARY_MISSING_RC, 128}
            else "git ls-files 실패"
        )
        warnings.warn(
            f"repo-owned 파일 열거가 {reason}로 filesystem 전수 순회에 강등됨 "
            f"(checkout={checkout_path}, subtree={norm!r}, mode={mode}, rc={fallback_rc}); "
            "추적/ignore 보장을 적용할 수 없음",
            RepoFilesFallbackWarning,
            stacklevel=2,
        )
        directory = checkout_path / norm
        if directory.is_file():
            entries = [RepoOwnedEntry(Path(norm), None)]
        else:
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
