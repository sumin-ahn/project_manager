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

import subprocess
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
