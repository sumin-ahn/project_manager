"""기계 파싱용 stdout git 실행과 작업 트리 관측 (층: infra)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from pm_engine.domain import finish_rules


def _default_run_git_stdout_at(cwd: Path, args: list[str]) -> tuple[int, str]:
    """task worktree의 기계 파싱용 git 실행 — stderr 경고를 결과에 섞지 않는다."""
    result = subprocess.run(
        ["git"] + args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(cwd),
    )
    return result.returncode, result.stdout


def _worktree_list(folder: Path, *, run_git_stdout_at) -> list[tuple[Path, str | None]]:
    """`folder` 가 속한 저장소의 작업 트리 전부 — `(경로, 체크아웃한 브랜치 ref 또는 None)`.

    `git worktree list --porcelain` 해석은 이 한 곳이다 — 작업 트리 해소와 통합 트리 해소가
    같은 해석을 쓴다. detached 트리(재배치가 멈춘 트리 포함)는 `branch` 줄이 없어 None 이다.
    폴더가 디스크에서 사라지고 등록만 남은 항목(prunable)은 작업 트리가 아니라 내지 않는다 —
    git 은 그 등록을 브랜치와 함께 계속 낸다. 조회 실패는 정지다(미해소 위장 금지).
    """
    try:
        rc, out = run_git_stdout_at(folder, ["worktree", "list", "--porcelain"])
    except Exception as exc:  # noqa: BLE001 — 조회 실패는 정지(미해소 위장 금지).
        raise finish_rules._CloseObservationFailure(
            f"작업 트리 목록을 관측하지 못했다 ({folder}) — "
            f"{type(exc).__name__}: {exc}") from exc
    if rc != 0:
        raise finish_rules._CloseObservationFailure(
            f"작업 트리 목록을 관측하지 못했다 ({folder} · rc={rc}): {out.rstrip()}")
    trees: list[tuple[Path, str | None]] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            trees.append((Path(line[len("worktree "):].strip()), None))
        elif line.startswith("branch ") and trees:
            trees[-1] = (trees[-1][0], line[len("branch "):].strip())
    return [(tree, ref) for tree, ref in trees if tree.exists()]
