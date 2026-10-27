"""설치 공통 install_git (infra)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable
import shutil
import subprocess

# `git` 호출 seam — argv(list) → (returncode, stdout). 테스트가 stub 주입(라이브 git 미실행).
# git_safe_relpaths 가 work tree 판별·추적집합 조회에 사용한다(_real_models_runner 류 결정적 seam).
GitRunner = Callable[[list], "tuple[int, str]"]


# git 호출 타임아웃 (초) — ls-files/status 는 빠른 로컬 명령이라 짧게(과대 대기 방지·fail-soft 상한).
GIT_SAFE_TIMEOUT_SECONDS = 15


# ── git-safe 판정 (LLM 아님·결정적) ──────────────────────────────
# --into 백업 노이즈를 줄이려, git 이 *추적 중이고 미변경*인 파일은 백업 없이 덮는다(git 이
# 내용을 갖고 있어 복원 가능). 그 외(미추적·dirty·비-git)만 중앙 디렉토리에 백업한다.
# git 호출은 LLM 아님 — git_runner 주입으로 테스트 결정적(_real_models_runner 류 seam 철학).


def _real_git_runner(dest_root: Path) -> GitRunner:
    """실 git 을 dest_root 컨텍스트로 호출하는 GitRunner 를 만든다.

    반환 callable: argv(list) → (returncode, stdout). git 바이너리 부재(shutil.which)는
    (1, "") 로 돌려 git_safe_relpaths 가 None(전부 백업)으로 본다. 실행 예외(시간 초과 등)는 전파한다.
    `git -C <dest> <argv...>` 형태로 항상 dest_root work tree 에 묶는다(_real_models_runner 선례).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple:
        if git_binary is None:
            return 1, ""
        result = subprocess.run(
            [git_binary, "-C", str(dest_root), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_SAFE_TIMEOUT_SECONDS,
        )
        return result.returncode, result.stdout or ""

    return runner


def _parse_status_dirty(porcelain_z: str) -> set:
    """`git status --porcelain -z` 출력 → dirty·untracked relpath(posix) 집합.

    NUL(`\\0`) 구분. 각 엔트리는 `XY <path>`(2-char 상태 + 공백 + 경로). rename(R/C)은
    `<new>\\0<old>` 로 *두 NUL 필드*를 쓰므로(상태 2칸이 R/C 로 시작), old-path 필드를 건너뛴다.
    untracked(`??`)·수정·staged 전부 "git-safe 아님"으로 본다(보수적 — 중앙 백업 대상).
    """
    dirty: set = set()
    parts = porcelain_z.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        # 엔트리 형식: 2-char XY 상태 + 공백 + 경로. 경로 추출(상태 3칸 이후).
        status = entry[:2]
        path = entry[3:] if len(entry) > 3 else ""
        if path:
            dirty.add(path)
        # rename/copy 는 다음 필드가 old-path — 건너뛴다(경로로 오해 방지).
        if status and status[0] in ("R", "C"):
            i += 2
        else:
            i += 1
    return dirty


def git_safe_relpaths(
    dest_root: Path,
    *,
    git_runner: GitRunner | None = None,
) -> set | None:
    """dest_root 가 git work tree 면 '추적 중 & 미변경' relpath(posix) 집합 반환.

    git 아님 / 바이너리 부재 / 오류 → None (= 전부 백업·보수적 fail-soft·안전).

    구현: `git rev-parse --is-inside-work-tree`(work tree 판별) → `ls-files -z`(추적 집합)
    − `status --porcelain -z`(dirty·untracked). 차집합 = 추적&미변경. git_runner 주입으로
    테스트 결정적(`shutil.which("opencode")`·`_real_models_runner` 동일 seam 철학).

    ⚠️ 경로 기준 정규화: `git -C <dest> ls-files` 는 **cwd(=dest_root) 상대**,
    `status --porcelain` 은 **repo-root 상대** 경로를 낸다. dest_root 가 repo 루트가 아닌 *하위
    디렉토리*면 두 기준이 달라 dirty 가 `tracked − dirty` 에서 안 빠진다 → dirty 를 git-safe 로
    오판해 무백업 덮을 위험. `rev-parse --show-prefix` 로 dirty(repo-root 상대)를 dest_root 상대로
    환산(prefix 하위만·prefix 제거)해 tracked(dest_root 상대)와 기준을 맞춘다. 반환 relpath 는
    plan_copy 의 `rel.as_posix()` 비교 기준(dest_root 상대 posix)과 일치한다.
    """
    runner = git_runner if git_runner is not None else _real_git_runner(dest_root)

    rc, out = runner(["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out.strip() != "true":
        return None  # git work tree 아님(또는 호출 실패) — 전부 백업.

    rc_pref, prefix_out = runner(["rev-parse", "--show-prefix"])
    if rc_pref != 0:
        return None  # prefix 조회 실패 — 기준 정규화 불가 → 보수적으로 전부 백업.
    prefix = prefix_out.strip()  # 하위 디렉토리면 'sub/dir/'(posix·trailing slash), repo 루트면 ''.

    rc_tracked, tracked_out = runner(["ls-files", "-z"])
    if rc_tracked != 0:
        return None  # 추적 집합 조회 실패 — 보수적으로 전부 백업.
    tracked = {p for p in tracked_out.split("\0") if p}  # cwd(=dest_root) 상대.

    rc_status, status_out = runner(["status", "--porcelain", "-z"])
    if rc_status != 0:
        return None
    dirty_repo = _parse_status_dirty(status_out)  # repo-root 상대.
    # repo-root 상대 dirty → dest_root 상대(prefix 하위만 남기고 prefix 제거). prefix='' 면 그대로.
    dirty = ({p[len(prefix):] for p in dirty_repo if p.startswith(prefix)}
             if prefix else dirty_repo)

    # 추적 중 & 미변경 = 추적 집합 − dirty/untracked. git 이 이 내용을 복원할 수 있다.
    return tracked - dirty
