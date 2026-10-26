"""위임 코드 트리 관측과 산출 기록 (층: infra)."""
from __future__ import annotations

from typing import Callable
from collections.abc import Mapping
from pathlib import Path
from pm_engine.domain import delegate_errors
from pm_engine.domain import delegate_rounds
from pm_engine.infra import pm_paths
from pm_engine.domain import review_form
from pm_engine.domain import review_state
import subprocess
from pm_engine.infra import ticket_store



def _ticket_commit_message(home, ticket: str) -> str:
    """그 티켓의 커밋 문안 — 명세 제목(없으면 티켓 ID). 조회 실패는 그대로 올린다."""
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        return ticket
    fm, _body = ticket_store.load_ticket(found[1])
    title = str((fm or {}).get("title") or "").strip() if isinstance(fm, dict) else ""
    return title or ticket


def _assert_developer_commit_tree(cwd: Path) -> None:
    """dev 산출을 커밋할 트리가 코드 트리인지 확인한다 — 장부 루트면 거부한다.

    티켓 제목 커밋은 코드 트리에만 만든다. 장부 루트(PM 홈)를 `--cwd` 로 받으면 board·wiki·
    log 변경이 `add -A` 에 함께 실려 그 티켓 제목의 커밋으로 묶인다. 판정 입력은 형상 하나다 —
    소유 PM 홈이 자기 자신이면 그 트리가 곧 PM 홈이고, 그 홈이 슬롯 풀의 공용 bare
    (`.repos/<repo>.git` · 풀을 만든 도구가 써 넣은 자리)를 가지면 코드 트리는 그 풀의 슬롯이지
    이 트리가 아니다. 임베드·단일 clone(풀 없음)은 PM 홈이 곧 코드 트리라 그대로 통과한다.

    자리는 회수의 다른 developer 게이트와 같다(board 교체 앞) — 커밋 직전으로 밀면 거부 시점에
    board 라운드 교체·장부 append·run-dir 삭제가 이미 끝나 같은 자리로 다시 회수할 수 없다.
    """
    if Path(pm_paths.owning_pm_home(cwd)).resolve() != Path(cwd).resolve():
        return                              # 풀 슬롯 — 소유 PM 홈이 따로 있다.
    bares = sorted((Path(cwd) / ".repos").glob("*.git"))
    if not bares:
        return                              # 임베드·단일 clone — PM 홈이 곧 코드 트리다.
    pools = []
    for bare in bares:
        # git 은 Windows 에서도 `/` 경로를 찍는다 — 장부 루트와 같은 절대경로 표기로 싣는다.
        slots = [
            Path(line[len("worktree "):].strip()).resolve()
            for line in _cluster_git(
                bare, "worktree", "list", "--porcelain",
            ).splitlines()
            if line.startswith("worktree ")
        ]
        slots = [slot for slot in slots if slot != bare.resolve()]
        # 슬롯이 하나도 없는 풀은 나열할 값이 없다 — 그 자리에는 나열 대신 슬롯을 만드는
        # 커맨드를 싣는다(빈 나열로 끝나면 문구만 읽고 `--cwd` 를 무엇으로 바꿀지 알 수 없다).
        pools.append(
            (f"{bare} 의 슬롯 = " + " · ".join(map(str, slots))) if slots
            else (
                f"{bare} 에는 아직 슬롯이 없습니다 — `pm-config worktree add {bare.stem} "
                f"--user-ack {bare.stem}` 로 슬롯을 만드세요"
            )
        )
    raise delegate_errors.DelegateError(
        "developer 라운드 회수 거부 — --cwd 가 슬롯 풀을 운영하는 PM 홈입니다"
        f"(장부 루트: {Path(cwd).resolve()}). 티켓 제목 커밋은 코드 트리에만 만듭니다: "
        + " / ".join(pools)
        + ". board 라운드 파일과 slot run-dir 은 그대로 보존했습니다 — `--cwd` 를 그 슬롯으로 "
        "바꿔 같은 회수를 다시 실행하세요."
    )


def _commit_developer_round_output(cwd: Path, *, ticket: str, home) -> str | None:
    """dev 산출을 그 슬롯에서 커밋한다 — 커밋했으면 문안, 커밋할 변경이 없으면 None.

    dev 산출의 커밋 자리는 여기 하나다(손 커밋 0). 리뷰 입력은 묶음 브랜치 tip 이고 리뷰 준비는
    작업 트리 변경 0 을 요구하므로, 회수한 산출이 커밋되지 않으면 리뷰가 서지 않는다. 변경
    관측은 리뷰 준비·돌려받기 트리 조건과 같은 하나다(`_slot_pending_changes`). 실패는 삼키지 않는다: 커밋되지
    않은 산출을 커밋된 것처럼 보고하면 그 라운드가 리뷰에서 통째로 빠진다.
    """
    if not _slot_pending_changes(cwd):
        return None
    message = _ticket_commit_message(home, ticket)
    _cluster_git(cwd, "add", "-A")
    _cluster_git(cwd, "commit", "-m", message)
    return message


def _assert_review_tree_unchanged(cwd: Path, row: Mapping[str, object], *, copy: Path) -> None:
    """리뷰 돌려받기 트리 조건 — 슬롯 HEAD 가 준비 기록 `base_rev` 와 같고 작업 트리 변경이 0 인가.

    리뷰어가 본 트리와 판정할 트리가 같아야 라운드가 증거다. 라운드 파일과 ignore 대상(사본
    루트·테스트 산출)은 작업 트리 변경이 아니다. 어긋나면 board·장부·run-dir 을 바꾸지 않고
    멈춘다. `base_rev` 가 없는 행(이 엔진 이전 준비)은 HEAD 로 대신하지 않는다.
    """
    base_rev = str(row.get("base_rev") or "")
    ticket = row["ticket"]
    ordinal = int(row["ordinal"])
    if not base_rev:
        raise delegate_errors.DelegateError(
            f"리뷰 라운드 돌려받기 거부 — 장부 행에 base_rev 가 없습니다(이 엔진 이전 준비라 "
            f"준비 뒤 슬롯 변경을 판정할 수 없음): {ticket} ordinal={ordinal} · copy={copy}. "
            f"`pm_delegate.py ticket abandon --copy {copy} --cwd {cwd} "
            f"{delegate_rounds.ABANDON_ASSUME_DEAD_FLAG}`(사본에 산출이 있으면 "
            f"`{delegate_rounds.ABANDON_DISCARD_REASON_FLAG} <사유>` 추가)로 닫은 뒤, board 에 그 순번 라운드가 "
            f"남아 있으면 `pm_delegate.py ticket prepare --cluster C-<ticket-group> --role "
            f"{review_form.INTERNAL_REVIEW_ROLE} {review_state.PREPARE_REOPEN_ORDINAL_FLAG} {ordinal} --cwd {cwd}` "
            f"(슬롯 사본의 `{review_form.PM_REVIEW_REFUSED_MARKER}` 표식 줄은 지운다), 없으면 "
            f"{review_state.PREPARE_REOPEN_ORDINAL_FLAG} 없이 다시 준비하세요"
        )
    head = _cluster_git(cwd, "rev-parse", "HEAD").strip()
    pending = _slot_pending_changes(cwd)
    if head == base_rev and not pending:
        return
    changes = " · ".join(line.strip() for line in pending) or "없음"
    raise delegate_errors.DelegateError(
        f"리뷰 라운드 돌려받기 거부 — 준비 뒤 슬롯이 바뀌었습니다: {ticket} "
        f"ordinal={ordinal} · 준비 HEAD={base_rev} · 현재 HEAD={head} · 작업 트리 변경: "
        f"{changes}. board 라운드·장부·run-dir 은 그대로입니다. 리뷰어 산출이 아닌 잔여면 "
        f"되돌린 뒤 같은 `--copy` 로 다시 돌려받고, 리뷰가 바뀐 트리를 봤으면 "
        f"`pm_delegate.py ticket abandon --copy {copy} --cwd {cwd} {delegate_rounds.ABANDON_ASSUME_DEAD_FLAG} "
        f"{delegate_rounds.ABANDON_DISCARD_REASON_FLAG} <사유>` 로 폐기한 뒤 `ticket prepare --cluster C-<ticket-group> "
        f"--role {review_form.INTERNAL_REVIEW_ROLE} {review_state.PREPARE_REOPEN_ORDINAL_FLAG} {ordinal} --cwd {cwd}` 로 "
        "같은 순번을 다시 여세요"
    )


def _developer_round_changed_paths(
    repo_root: Path, *, base_rev: str,
) -> frozenset[str]:
    """직전 developer 커밋 후 현재 라운드가 바꾼 경로.

    developer 산출은 harvest 성공 뒤에만 커밋되므로 ``HEAD..작업트리``가
    해당 단계의 자연스런 기준선이다. 존재 이유가 사라진 코드·테스트를 지우는 것도
    그 라운드의 산출이라 같이 세고, untracked 테스트는 별도로 포함한다.

    삭제(``D``)를 세는 이 집합은 **두 축이 공유한다** — reviewer fix 계약 대상 검사와
    architect 필수 테스트 대상 검사가 같은 값을 본다. 그래서 architect 계약이 지목한 테스트
    파일을 **지운** 라운드도 대상 검사만으로는 통과한다. 그 자리를 인자로 가르지 않는 이유는
    바로 뒤에서 그 계약의 command 를 실제로 실행하기 때문이다 — 지목된 파일이 없으면 명령
    자체가 red 라 같은 라운드가 그 자리에서 멈춘다. 축을 가르는 인자는 이미 실행이 내는 답을
    한 번 더 재는 장치다.
    """
    changed = {
        line.strip()
        for line in _cluster_git(
            repo_root, "diff", "--name-only", "--diff-filter=ACMRTUXBD",
            base_rev, "--",
        ).splitlines()
        if line.strip()
    }
    changed.update(
        line.strip()
        for line in _cluster_git(
            repo_root, "ls-files", "--others", "--exclude-standard", "--",
        ).splitlines()
        if line.strip()
    )
    return frozenset(changed)


def _cluster_git(
    repo: Path, *args: str, git_run_fn: Callable | None = None,
) -> str:
    """묶음 브랜치 조회 git — 실패는 그대로 올린다(조용한 빈 값 금지)."""
    runner = git_run_fn or subprocess.run
    result = runner(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if getattr(result, "returncode", 1) != 0:
        detail = (
            (getattr(result, "stderr", "") or "").strip()
            or (getattr(result, "stdout", "") or "").strip()
            or "원인 미상"
        )
        raise delegate_errors.DelegateError(f"git {' '.join(args)} 실패: {detail}")
    return getattr(result, "stdout", "") or ""


def _slot_pending_changes(
    cwd: Path, *, git_run_fn: Callable | None = None,
) -> tuple[str, ...]:
    """슬롯 작업 트리 변경 — `git status --porcelain` 줄(ignore 대상 제외).

    추적 파일 수정·추적 안 된 새 파일이 모두 잡힌다. 서브모듈 내부 dirty 는 세지 않는다 — 상위
    repo 가 커밋할 수 있는 것은 포인터뿐이다(종결 단계와 같은 관측). 리뷰 준비·리뷰
    돌려받기의 트리 조건과 developer 돌려받기 커밋이 이 한 관측을 쓴다.
    """
    return tuple(
        line for line in _cluster_git(
            cwd, "status", "--porcelain", "--untracked-files=all",
            "--ignore-submodules=dirty", git_run_fn=git_run_fn,
        ).splitlines() if line.strip()
    )
