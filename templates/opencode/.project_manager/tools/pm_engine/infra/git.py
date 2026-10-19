"""git 저장소 조회 (층: infra).

`git rev-parse`·alias·명령 목록·미커밋 여부 조회. 판정 규칙은 `pm_engine.domain.git_policy` 가 소유한다.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from pm_engine.domain import git_policy


def _git_repo_root(anchor: Path, *, runner: Any = subprocess.run) -> Path | None:
    root = _git_rev_parse(anchor, "--show-toplevel", runner=runner)
    return Path(root).resolve() if root else None


def _shared_repository_root(anchor: Path, *, runner: Any = subprocess.run) -> Path | None:
    """`anchor` 가 PM 홈 공용 저장소(bare `<PM 홈>/.repos/<repo>.git`) 안이면 그 저장소 경로.

    bare 저장소는 작업 폴더가 없어 ``--show-toplevel`` 이 실패하므로 git-dir 로 따로 해소한다.
    형상 규칙은 슬롯 소유 유도(`pm_paths.owning_pm_home`)와 같다 — `.repos` 아래 `.git` 접미
    디렉터리이고 그 위가 `.project_manager` 를 가진 PM 홈이어야 한다.
    """
    answer = _git_rev_parse(anchor, "--is-bare-repository", "--absolute-git-dir", runner=runner)
    lines = answer.splitlines() if answer else []
    if len(lines) != 2 or lines[0] != "true":
        return None
    git_dir = Path(lines[1]).resolve()
    if git_dir.suffix != ".git" or git_dir.parent.name != ".repos":
        return None
    return git_dir if (git_dir.parent.parent / ".project_manager").is_dir() else None


def _git_commit_exists(anchor: Path, name: str, *, runner: Any) -> bool:
    return _git_rev_parse(
        anchor, "--verify", "--quiet", f"{name}^{{commit}}", runner=runner,
    ) is not None


def _git_local_branch_exists(anchor: Path, name: str, *, runner: Any) -> bool:
    return _git_rev_parse(
        anchor, "--verify", "--quiet", f"refs/heads/{name}", runner=runner,
    ) is not None


# `git config --get` 의 "키 없음" 종료 코드. 그 밖의 실패(설정 파일 손상 등)는 판정 실패로 올린다.
_GIT_CONFIG_KEY_MISSING_RC = 1


def _git_alias_value(invocation: git_policy._GitInvocation, *, runner: Any) -> str | None:
    config = [part for value in invocation.config for part in ("-c", value)]
    result = runner(
        ["git", "-C", str(invocation.cwd), *config, "config", "--get",
         f"alias.{invocation.subcommand}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode == _GIT_CONFIG_KEY_MISSING_RC:
        return None
    if result.returncode != 0:
        raise RuntimeError(
            f"git alias 조회 실패(rc={result.returncode}): {(result.stderr or '').strip()}"
        )
    return (result.stdout or "").strip() or None


def _git_command_names(anchor: Path, *, runner: Any) -> frozenset[str]:
    """git 명령 이름(내장 + PATH 의 ``git-*``). alias 가 이 이름을 가리면 git 은 alias 를 무시한다."""
    result = runner(
        ["git", "-C", str(anchor), "--list-cmds=main,others"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return frozenset((result.stdout or "").split())


def _git_path_uncommitted(
    anchor: Path, relative: Path, *, runner: Any = subprocess.run
) -> bool | None:
    """`anchor` 저장소에서 `relative` 파일이 **미커밋** 상태인지 (git 조회 1회).

    `git status --porcelain --ignored -- <path>` 의 출력이 있으면 그 사본은 커밋으로 확정되지
    않았다(작업트리 변경·스테이지·미추적·무시됨). git 아님/오류면 None — **판정 불가**이며
    호출부는 이를 단정으로 메우지 않는다.
    """
    try:
        result = runner(
            ["git", "-C", str(anchor), "status", "--porcelain", "--ignored",
             "--", relative.as_posix()],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    return bool((result.stdout or "").strip())


def _git_rev_parse(anchor: Path, *args: str, runner: Any = subprocess.run) -> str | None:
    """`git -C <anchor> rev-parse <args>` 결과(strip)를 반환. git 아님/오류/빈 값이면 None
    (fail-soft — standalone·비-git 트리 무영향). `runner` 는 hermetic 테스트 주입 seam."""
    try:
        r = runner(["git", "-C", str(anchor), "rev-parse", *args],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def _is_linked_worktree(anchor: Path, *, runner: Any = subprocess.run) -> bool:
    """`anchor` 가 linked git worktree(main checkout 아님)인가.

    linked worktree 는 `git rev-parse --git-dir` 가 `<common>/worktrees/<name>` 을 가리켜
    `--git-common-dir`(=`<common>`)와 **다르다**. 일반 checkout·PM 홈은 둘 다 `.git` 로 동일
    (→ False). git 아님/오류면 False(fail-soft — standalone 무영향).
    """
    git_dir = _git_rev_parse(anchor, "--git-dir", runner=runner)
    common_dir = _git_rev_parse(anchor, "--git-common-dir", runner=runner)
    if git_dir is None or common_dir is None:
        return False

    def _abs(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (anchor / pp).resolve()

    return _abs(git_dir) != _abs(common_dir)


def _git_head_at(cwd: str) -> str:
    """주어진 작업 디렉토리의 git HEAD sha 를 반환한다 (실패/비-repo 면 '').

    `_git_head` 는 board 프로세스의 `REPO` 기준이지만, livegate 기록은 테스트가 실제로
    돈 **활성 slot worktree**(=`_livegate_cwd`)의 HEAD 를 키로 삼아야 한다 — 보호훅이
    push 하는 sha(=worktree HEAD)와 대조되기 때문. 이 함수가 그 cwd-매개 HEAD 를 낸다.
    """
    r = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


# board git 호출 timeout — pull/push 는 네트워크 왕복이라 user-email 폴백(5s)보다 길게
# 둔다. 환경 이상(hang·offline DNS)에서 무한 대기를 막는 상한(엔진 subprocess 관례).
_BOARD_GIT_TIMEOUT_SECONDS = 30


def _cluster_current_branch(tree: str) -> str | None:
    """코드 트리의 현재 브랜치 — detached·비-git·git 부재는 None."""
    result = _git_run(["symbolic-ref", "--quiet", "--short", "HEAD"], repo=Path(tree))
    if result is None or result[0] != 0:
        return None
    return (result[1] or "").strip() or None


def _git_run(argv: list[str], *, runner=None, repo: Path) -> tuple[int, str] | None:
    """git 명령의 `(rc, stdout)` 반환·git 부재/예외 → None. runner seam(hermetic 테스트)·미주입
    시 실 subprocess(`git -C repo`). rev-parse 해소 OID(prefix 검증용)가 필요한 곳이 쓴다."""
    if runner is None:
        if shutil.which("git") is None:
            return None
        repo = Path(repo)
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), *argv],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_BOARD_GIT_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):  # 타임아웃/바이너리 이상은 None(환경적).
            return None
        return r.returncode, r.stdout or ""
    try:
        rc, out = runner(argv)
    except Exception:  # noqa: BLE001 — 주입 runner raise 도 None.
        return None
    return rc, out
