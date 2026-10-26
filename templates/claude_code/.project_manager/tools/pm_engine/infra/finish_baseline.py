"""완료 대상 작업 트리 자기 축 회귀의 대상 파일·부분 회귀·baseline 사본 (층: infra)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pm_engine.domain import finish_rules
from pm_engine.infra import fs, raw_output


def _self_axis_target_files(code_tree: Path, *, run_git_stdout_at) -> set[str]:
    """대상 tests/*.py 집합 = tracked(staged+unstaged) 변경 ∪ untracked 신규 ∪ ratchet 목록.

    두 git 명령 모두 순수 읽기다 — index 를 갱신하지 않는다(`git status` 는 stat 캐시를
    건드릴 수 있어 안 쓴다)."""
    _rc_t, tracked_out = run_git_stdout_at(
        code_tree, ["diff", "--name-only", "HEAD"])
    _rc_u, untracked_out = run_git_stdout_at(
        code_tree, ["ls-files", "-o", "--exclude-standard"])
    changed = (finish_rules._self_axis_target_paths(tracked_out)
               | finish_rules._self_axis_target_paths(untracked_out))
    return changed | set(finish_rules._SELF_AXIS_RATCHET_GUARD_FILES)


def _run_pytest_subset(cwd: Path, files: Sequence[str], *, venv_python) -> tuple[int, str]:
    """대상 파일만 골라 `cwd` 에서 pytest 를 돌린다(작업 트리·baseline 공용).

    `cwd` 에 실재하지 않는 파일은 건너뛴다(삭제분이 집합에 남아도 수집 에러로 안 죽는다) —
    하나도 안 남으면 무비용 통과 (0, "")."""
    existing = sorted(f for f in files if (cwd / f).is_file())
    if not existing:
        return 0, ""
    result = subprocess.run(
        [str(venv_python), "-m", "pytest", *existing, "-q"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd),
    )
    return result.returncode, result.stdout + result.stderr


def _materialize_tree(home, code_tree: Path, ref: str, *, run_git_stdout_at) -> Path:
    """claim 시점 `ref`(commit-ish) 를 scratch 디렉터리에 추출한 baseline 저장소를 만든다.

    `git archive | tar -x` 로 파일만 옮기고 `git init` 을 더한다 — 환경 의존 테스트(git
    저장소 존재를 요구하는 가드 등)가 "git 저장소 부재"로 오탐하지 않게 한다. 곧바로
    `.git/objects/info/alternates` 에 원 저장소 object DB 경로를 적어 원 이력 객체를
    read-only 로 해소한다 — 실 SHA 를 좌표로 쓰는 테스트가 고립된 scratch 저장소에서
    "유효한 객체 이름이 아닙니다" 로 오탐하지 않는다(민감도 — 이 한 줄을 빼면 그 오탐이
    재현된다). `code_tree` 의 ref·index·worktree 등록은 만들지 않는다 — 원 저장소는
    읽기만 당한다(alternates 는 OID 로 객체를 해소할 뿐 이름으로 ref 를 해소하지 않는다).
    """
    scratch = Path(tempfile.mkdtemp(
        prefix="ticket-finish-baseline-", dir=raw_output.temp_root(home.root)))
    try:
        archive = subprocess.run(
            ["git", "archive", ref], cwd=str(code_tree), capture_output=True)
        if archive.returncode != 0:
            raise RuntimeError(
                f"git archive {ref} 실패: "
                f"{archive.stderr.decode('utf-8', 'replace')}")
        tar = subprocess.run(
            ["tar", "-x"], cwd=str(scratch), input=archive.stdout,
            capture_output=True)
        if tar.returncode != 0:
            raise RuntimeError(f"tar -x 실패: {tar.stderr.decode('utf-8', 'replace')}")
        init = subprocess.run(
            ["git", "init", "-q"], cwd=str(scratch), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if init.returncode != 0:
            raise RuntimeError(f"git init 실패: {init.stderr}")

        common_rc, common_out = run_git_stdout_at(
            code_tree, ["rev-parse", "--git-common-dir"])
        if common_rc != 0 or not common_out.strip():
            raise RuntimeError("원 저장소 object DB 경로(--git-common-dir) 해소 실패")
        common_dir = Path(common_out.strip().splitlines()[0].strip())
        if not common_dir.is_absolute():
            common_dir = (code_tree / common_dir).resolve()
        alternates_path = scratch / ".git" / "objects" / "info" / "alternates"
        alternates_path.parent.mkdir(parents=True, exist_ok=True)
        alternates_path.write_text(
            f"{common_dir / 'objects'}\n", encoding="utf-8", newline="\n")

        for args in (
            ["add", "-A"],
            ["-c", "user.email=ticket-finish-baseline@local",
             "-c", "user.name=ticket-finish-baseline",
             "commit", "-q", "-m", "baseline (materialized — 실 이력 아님)"],
        ):
            result = subprocess.run(
                ["git", *args], cwd=str(scratch), capture_output=True, text=True,
                encoding="utf-8", errors="replace")
            if result.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)} 실패: {result.stderr}")
    except Exception:
        # 정리 실패로 원 예외를 덮지 않는다 — force_rmtree 는 못 지우면 OSError 를 올리는데,
        # 그걸 그대로 두면 여기서 진행 중인 원 예외(git archive/init/commit 실패)를 가린다.
        # 형제 규약(pm_import._force_rmtree 호출부)과 같은 형태: loud 경고 뒤 원 예외 재전파.
        try:
            fs.force_rmtree(scratch)
        except OSError as cleanup_exc:
            print(
                f"경고: baseline scratch 정리 실패 — 직접 지우세요: {scratch} "
                f"({type(cleanup_exc).__name__}: {cleanup_exc})",
                file=sys.stderr,
            )
        raise
    return scratch
