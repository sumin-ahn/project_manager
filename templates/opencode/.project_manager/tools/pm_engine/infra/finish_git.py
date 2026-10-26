"""기계 파싱용 stdout git 실행과 작업 트리·상태·blame 관측 (층: infra)."""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from pm_engine.domain import finish_rules
from pm_engine.infra import git


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


# ── 잔여 보고 (under-stage / 스코프 밖 staged) ──────────────────────────────
#
# 상태 판독은 infra.git 의 NUL 파서(`git_parse_porcelain_z` — 스코프 전개와 **같은 함수**)를 쓴다.
# 관측 실패를 "잔여 없음"으로 접으면 **거짓 안심**이 되므로 strict 소비자는 실패를 그대로 받는다.


def _path_exists_in(root: Path, relative: str) -> bool:
    """`root` 안에 이 선언 경로가 실재하는가 (판정 불능·경로 마법은 False).

    touches 는 사람이 적는 값이라 glob/pathspec 마법이 섞일 수 있다. 그런 선언은 이 트리에 있다고
    단정하지 않고 False 로 접는다 — 판정 불능을 존재로 세면 몫이 틀린 계획에 실린다. 절대경로
    선언은 pathlib 규칙상 `root` 가 무시돼 두 트리 검사가 같은 답을 낸다 — 실재하면 호출부의
    우선순위대로 코드 몫이 되고, 실재하지 않으면 양쪽 계획에 들어간다.
    """
    candidate = relative.strip()
    if not candidate or candidate.startswith(":(") or any(ch in candidate for ch in "*?["):
        return False
    return (root / candidate).exists()


def _git_blame_short_sha(
    run_git_stdout_at_fn: Callable[[Path, list[str]], tuple[int, str]],
    code_tree: Path, path: str, line: int,
) -> str | None:
    """`path:line` 을 도입한 커밋의 축약 sha (미커밋 줄·git rc≠0 은 None, runner 예외는 전파).

    잔여로 남는 흡수분(claim 이후 다른 커밋이 offender 를 들여온 경우) 경고에만 쓴다 — git 이
    rc≠0 으로 답하면 경고 자체는 그대로 나가고 sha 만 빠진다(진단 강화일 뿐 차단 축이 아니다).

    **작업트리를 보는 `git blame` 이어야 한다** — `git log -L` 은 커밋된 내용만 따라가므로
    아직 커밋되지 않은 줄에 HEAD 의 같은 줄 번호에 있던 **무관한 커밋** sha 를 붙인다(유입
    커밋을 식별하라는 이 경고의 목적과 반대다). blame 이 미커밋 줄에 내는 all-zero sha 는
    도입 커밋이 없다는 뜻이므로 sha 없이 경고한다."""
    rc, out = run_git_stdout_at_fn(
        code_tree,
        ["blame", "--porcelain", f"-L{line},{line}", "--", path],
    )
    if rc != 0 or not out.strip():
        return None
    sha = out.split(maxsplit=1)[0]
    if finish_rules._BLAME_SHA_RE.fullmatch(sha) is None or set(sha) == {"0"}:
        return None  # 미커밋(작업트리에만 있는) 줄 — 도입 커밋이 아직 없다.
    rc, short = run_git_stdout_at_fn(code_tree, ["rev-parse", "--short", sha])
    short = short.strip()
    return short if rc == 0 and short else None


def status_entries(run_git: Callable[[list[str]], tuple[int, str]],
                   *, strict: bool = False) -> tuple[tuple[str, str], ...]:
    """현재 워킹트리 상태 `((XY, 경로), …)` — git rc≠0·비-git 은 `()`(비차단), runner 예외는 전파.

    `strict` 면 rc≠0 을 빈 목록으로 접지 않고 그대로 올린다
    (`StatusObservationError`) — "판정 불능" 과 "관측했더니 clean" 을 가르는 축이다. 접는
    자리는 이 seam 이 아니라 소비자다. 값 파싱은 두 갈래가 같다.

    **`-z` + `--untracked-files=all` 이 둘 다 load-bearing** 이다:
      - `-z` 없으면 git 이 비-ASCII/공백 경로를 인용 + 8진 이스케이프로 낸다. 그 문자열이
        `scope_covers` 비교에 들어가 매칭에 실패하고, *방금 자기가 stage 한 파일* 을 "스코프
        밖 — 빼라" 로 오보한다(reviewer 실측·PM 홈에 한글 경로 실재). 목록도 8진이라 복붙 불가다.
      - `-uall` 없으면 새 untracked 디렉토리가 `?? wiki/raw/` 한 줄로 접혀 **무엇이 빠졌는지
        안 보인다**(legacy 실 구동 실측). 접힌 정보를 주는 loud 채널은 loud 가 아니다.
    NUL 파싱은 infra.git의 `git_parse_porcelain_z`를 디렉토리 전개와 공유한다.

    rename/copy의 원본 경로 토큰까지 공유 파서에서 소비해 가짜 잔여 항목을 방지한다.
    """
    rc, out = run_git(["status", "--porcelain", "-z", "--untracked-files=all"])
    if rc != 0:
        if strict:
            raise finish_rules.StatusObservationError(f"git status 조회 실패 (rc={rc}): {out.strip()}")
        return ()
    return git.git_parse_porcelain_z(out)


def split_dirty(entries: Sequence[tuple[str, str]],
                pathspec: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """상태 목록 → (**스코프 밖 staged** 줄, **미스테이지 잔여** 줄) — 두 실패 방향을 함께 본다.

      - 스코프 밖 staged (`X` 가 공백도 `?` 도 아님 ∧ 선언 스코프 밖) — 이 도구는 커밋하지
        않고 PM 에게 넘기므로, 남이 미리 stage 해 둔 변경이 **PM 의 커밋에 그대로 실린다**.
        `add` 를 좁히는 것만으로는 안 닫히고 `commit` 쪽으로 새는 갈래다("add 와
        commit 양쪽").
      - 미스테이지 잔여 (`Y` 가 공백 아님·`??` 포함 ∧ 선언 스코프 밖) — 스코프가 못 덮은
        변경(under-stage).

    **스코프 필터는 두 방향 모두에 걸린다.** stage **후**에 부르면(기존 사후 보고 용법) 스코프
    경로는 이미 `Y == " "` 라 필터가 항등이라 무행동 변화다. stage **전**에 부르면(잔여
    preflight — `TicketFinisher._default_residual_block`) 선언 스코프 안의 미스테이지 변경은
    "곧 stage 될 것"이라 잔여가 아니다 — 필터가 없으면 stage 전 호출마다 스코프 안 변경까지
    거짓 잔여로 잡힌다.
    """
    staged_out: list[str] = []
    unstaged: list[str] = []
    for code, path in entries:
        if finish_rules._is_local_runtime_path(path):
            continue
        if code[0] not in (" ", "?") and not finish_rules.scope_covers(pathspec, path):
            staged_out.append(f"{code} {path}")
        if code[1] != " " and not finish_rules.scope_covers(pathspec, path):
            unstaged.append(f"{code} {path}")
    return tuple(staged_out), tuple(unstaged)


def _unborn_baseline(git_stdout, cwd: Path, branch: str) -> bool:
    """장부가 선언한 통합 브랜치가 **이 트리가 든 바로 그 브랜치이고 커밋이 0**인가.

    새 채택자(`pm_import --new` 직후)의 첫 종결이 이 형상이다 — 그때 통합 tip 은 "없는
    브랜치" 가 아니라 이 종결의 커밋 단계가 첫 커밋을 얹을 자기 브랜치다. 관측 입력은 둘
    뿐이고 **둘 다 참일 때만** 참이다: HEAD 가 커밋을 가리키지 않는다(`rev-parse --verify
    HEAD` 실패) **그리고** HEAD 가 그 브랜치의 ref 를 가리킨다(`symbolic-ref HEAD`).
    detached·다른 브랜치·비-git 트리는 둘째 관측에서 거짓이라 종전 판정(정지·가드 off)이
    그대로 돈다.

    이 판정은 여기 한 곳이 소유한다 — 소비자(`_integration_tip`·`_pre_rebase`·
    `_step_rebase`)가 같은 관측을 각자 쓰면 네 판정이 서로 다른 트리 상태를 본다.
    """
    rc, _out = git_stdout(cwd, ["rev-parse", "--verify", "--quiet", "HEAD"])
    if rc == 0:
        return False
    rc, out = git_stdout(cwd, ["symbolic-ref", "--quiet", "HEAD"])
    return rc == 0 and out.strip() == f"refs/heads/{branch}"
