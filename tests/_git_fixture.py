"""픽스처 트리를 자기 Git 저장소로 세우는 공용 헬퍼.

pytest 임시 루트가 프로젝트 안(`.project_manager/.local/tmp`)이라 픽스처 트리는 이 저장소의
work tree 안에 있다. `git rev-parse --show-toplevel`·`git ls-files`·`git config` 를 부르는
엔진 코드는 그 질문을 git 에게 그대로 넘기므로, 자기 선언이 없는 픽스처 트리는 **이 저장소**의
답을 받는다(실측: 실 PM 홈 `.local/livegate.json` 이 픽스처 값으로 덮였다).

경계 env(`GIT_CEILING_DIRECTORIES`)나 `.git` 위장으로 막지 않는다 — 픽스처가 자기 저장소라고
**선언**하면 git 이 그 자리에서 멈추고, 답이 픽스처 자신의 함수가 된다. 임시 루트가 어디에
있든 같은 답이 나오는 것이 이 선언의 목적이다.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

_IDENTITY = (
    "-c", "user.name=test",
    "-c", "user.email=test@example.invalid",
)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """픽스처 저장소에서 git 을 돌린다(정체성 주입 · UTF-8 고정 · 실패는 실패)."""
    return subprocess.run(
        ["git", *_IDENTITY, "-C", str(root), *args],
        check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def init_git_repo(root: Path, *, commit: str | None = None) -> Path:
    """`root` 를 자기 Git 저장소로 선언한다. `commit` 을 주면 현재 트리를 그 메시지로 커밋한다."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    if commit is not None:
        git(root, "add", "-A")
        git(root, "commit", "-qm", commit, "--allow-empty")
    return root


def remove_git_tree(root: Path) -> None:
    """픽스처 git 트리를 통째로 지운다 — Windows read-only object(WinError 5)는 chmod +w 후 재시도.

    Windows 는 `.git/objects/` 파일이 read-only 라 `shutil.rmtree` 가 PermissionError 로 멈춘다.
    쓰기권한을 주고 같은 호출을 다시 시도하며, 그래도 안 되면 예외를 그대로 전파한다(실패를
    삼키면 "지워졌다" 를 전제하는 테스트가 거짓 통과한다). POSIX 는 부모 디렉터리 쓰기권한만
    있으면 지워져 이 핸들러에 오지 않는다.
    """
    def _chmod_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=_chmod_and_retry)
    else:  # pragma: no cover — 3.11 이하 호환(onexc 미지원 → onerror)
        shutil.rmtree(root, onerror=_chmod_and_retry)
